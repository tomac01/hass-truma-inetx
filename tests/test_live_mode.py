#!/usr/bin/env python3
"""Prüft den zeitgesteuerten Live-Modus im Poll-Betrieb.

Warum das hier steht: Im Poll-Betrieb sind die Werte zwischen zwei
Abfragen alt. Wer am Panel etwas nachstellt, will das im Dashboard sehen,
ohne fünf Minuten zu warten -- aber auch nicht dauerhaft einen der drei
Verbindungsplätze des Proxys belegen.

Was der Test festnagelt:

1. 0 Minuten heißt einmal synchronisieren, nicht unendlich,
2. die Haltezeit beginnt erst nach dem Handshake,
3. eine neue Anfrage verlängert ab jetzt, nicht kumulativ,
4. unsinnige Dauern werden abgewiesen,
5. ein schweigendes Panel gilt nicht als geglückte Synchronisation,
6. ein gescheiterter Refresh gibt sein eigenes Fenster frei -- und löscht
   dabei kein neueres,
7. ein Handshake, der nie zustande kommt, wird als Fehler gemeldet,
8. eine wartende Anfrage lässt sich abbrechen,
9. „Beenden" schließt ein laufendes Fenster wirklich -- und setzt keinen
   Wunsch ab, wenn gar keines läuft,
10. ein Befehl und eine frische Anfrage widerrufen einen Release-Wunsch,
11. und vor allem: die **Prüfreihenfolge** der Verweilschleife.

Zu Punkt 11, der der eigentliche Grund für diese Datei ist. Die Schleife
prüft
``Writes/manuelle Anfragen -> Release-Wunsch -> Command-Hold ->
Live-Fenster -> Stille -> Max-Dwell``
und jede Vertauschung ist ein Fehler mit Gesicht: wird der Release vor den
Writes geprüft, legt die Schleife mitten im Befehl auf; wird er nach dem
Command-Hold geprüft, ignoriert „Live-Modus beenden" eine Minute lang den
Nutzer; wird das Live-Fenster vor dem Command-Hold geprüft, reißt der
Stall-Watchdog den Link mitten im Nachlauf ab. Die vier Tests darunter
messen deshalb die **Verweildauer**, nicht bloß gesetzte Flags: ein Flag
sagt nichts darüber, in welcher Reihenfolge es gelesen wird.

Run: ``python3 tests/test_live_mode.py``
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

APP_ADDR = 0x0501
HEATER = 0x0201

stubs.install_homeassistant()
stubs.stub_transport()
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          discover_params=None, StartupFailed=RuntimeError, handle_frame=None)
stubs.mod("truma_pkg.truma.protocol", build_write_frame=None,
          build_v3_frame=None)
stubs.load_truma("const")
BUS = stubs.load("bus")
stubs.load("const")
stubs.load("operations")
COORD = stubs.load("coordinator")


class _Clock:
    """Virtuelle Loop-Uhr, von den Sleeps des Codes selbst weitergestellt.

    Die Verweilschleife tickt im Sekundentakt um Fenster von Minuten herum;
    in echter Zeit wäre das hier ein Zehn-Minuten-Test.
    """

    def __init__(self) -> None:
        self.now = 100.0

    def time(self) -> float:
        return self.now


class _FastForward:
    """``asyncio``-Ersatz, dessen ``sleep`` die Uhr weiterstellt.

    Jeder Tick ruft zusätzlich den ``on_tick``-Haken des Coordinators auf --
    damit ein Test einen laufenden Befehl mitten in der Schleife beenden
    lassen kann, so wie es in echt ein zweiter Task täte.
    """

    def __init__(self, clock: _Clock, coord: "_Coord | None" = None) -> None:
        self._clock = clock
        self._coord = coord

    def __getattr__(self, name):  # alles andere ist das echte asyncio
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self._clock.now += delay
        if self._coord is not None:
            self._coord.tick()
        await asyncio.sleep(0)


class _Client:
    """Ein Link, der steht und schweigt -- mehr braucht die Verweilschleife."""

    assigned_addr = APP_ADDR
    transport = "local"
    connected = True


class _Coord:
    """Ein Coordinator, der nur den Live-Modus-Pfad trägt."""

    unique_id = "Truma iNetX-15E02F"

    def __init__(
        self,
        *,
        connected: bool = False,
        poll_interval: int = 300,
        panel_talks: bool = False,
        handshake_seconds: float = 0.0,
    ) -> None:
        self.clock = _Clock()
        self.panel_talks = panel_talks
        # Was der Startup an Zeit kostet. Auf dem Fahrzeug sind das rund 25 s,
        # und ein Double, das ihn gratis erledigt, kann gar nicht sehen, ob
        # die Live-Zeit davor oder danach anläuft.
        self.handshake_seconds = handshake_seconds
        COORD.asyncio = _FastForward(self.clock, self)
        self.hass = stubs.SimpleNamespace(loop=self.clock)
        self.poll_interval = poll_interval
        self._bus = BUS.Bus()
        self._operations = COORD.OperationRegistry(lambda: None)
        self._writes_pending = 0
        self._command_hold_until = 0.0
        self._manual_wake_pending = False
        self._manual_hold_request_minutes = None
        self._manual_hold_until = 0.0
        self._manual_release_requested = False
        self._manual_requests = {}
        self._manual_operation = None
        self._write_feedback = {}
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._client = stubs.SimpleNamespace(connected=connected)
        self._stop = False
        self._stop_event = asyncio.Event()
        self._last_frame = 0.0
        self._last_kind = None
        self._session_ok = False
        self._session_transport = None
        if connected:
            self._connected_event.set()
        self.discovered = 0
        self.measured = 0
        # Was bei jedem Sekundentick der Verweilschleife passieren soll --
        # der Platzhalter für alles, was nebenher läuft.
        self.on_tick = lambda _coord: None
        self.ticks = 0
        # Der Abschluss-Schalter des geliehenen Schreibpfads (siehe
        # ``_write_confirmed``) und der Platz für den Task, der ihn fährt.
        self.write_done = asyncio.Event()
        self.pending_task = None

    def tick(self) -> None:
        self.ticks += 1
        if self.panel_talks:
            self._last_frame = self.clock.now
        self.on_tick(self)

    def async_set_updated_data(self, _data) -> None:
        pass

    def async_sync_device_names(self) -> None:
        pass

    async def _run_startup(self, _client) -> None:
        """Steht für Registrierung und Discovery -- haben eigene Dateien.

        Er kostet hier aber Zeit, denn genau darum geht es beim Live-Modus:
        ein Handshake, der die Uhr nicht weiterstellt, macht „vor dem
        Handshake" und „nach dem Handshake" zum selben Zeitpunkt -- und jede
        Prüfung darüber blind.
        """
        self.clock.now += self.handshake_seconds

    async def _client_for_write(self):
        """Der Link steht schon -- das Warten darauf hat eine eigene Datei."""
        return self._client

    async def _write_confirmed(self, _client, *_a, **_kw) -> None:
        """Der Befehl selbst; seine Bestätigung prüft test_confirmed_writes.

        Er dauert hier so lange, wie der Test ihn dauern lässt: ``write_done``
        wird von einem Tick der Verweilschleife gesetzt.
        """
        await self.write_done.wait()

    async def _discover_params(self, _client) -> None:
        self.discovered += 1
        self._bus.update("AirHeating", "CurrentTemp", 195, HEATER)

    async def _request_measurements(self, _client) -> None:
        self.measured += 1

    manual_session_active = COORD.TrumaCoordinator.manual_session_active
    async_request_manual_session = COORD.TrumaCoordinator.async_request_manual_session
    _request_manual_session = COORD.TrumaCoordinator._request_manual_session
    async_end_manual_session = COORD.TrumaCoordinator.async_end_manual_session
    _finish_startup = COORD.TrumaCoordinator._finish_startup
    _wait_before_retry = COORD.TrumaCoordinator._wait_before_retry
    # Geliehen statt nachgebaut: der Test unten fragt, was ein *echter*
    # Befehl mit einem Release-Wunsch macht.
    async_write_many = COORD.TrumaCoordinator.async_write_many
    _hold_after_command = COORD.TrumaCoordinator._hold_after_command
    _infer_action = staticmethod(COORD.TrumaCoordinator._infer_action)


class _RunCoord:
    """Nur so viel Coordinator, wie die Wiederanwahl-Schleife anfasst.

    ``_connect_and_run`` scheitert jedes Mal -- die Schleife soll ja gerade
    zeigen, wie sie sich zwischen zwei Fehlversuchen verhält.
    """

    unique_id = "Truma iNetX-15E02F"
    poll_interval = 300

    def __init__(self, rounds: int = 4) -> None:
        self.clock = _Clock()
        self.hass = stubs.SimpleNamespace(loop=self.clock)
        self._stop = False
        self._manual_wake_pending = False
        self._manual_hold_until = 0.0
        self._command_hold_until = 0.0
        self._connected_event = asyncio.Event()
        self._avoid = set()
        self.rounds = 0
        self.limit = rounds
        self.delays: list[float] = []

    async def _connect_and_run(self) -> bool:
        self.rounds += 1
        if self.rounds >= self.limit:
            self._stop = True
        return False

    async def _disconnect_client(self) -> None:
        pass

    def _mark_disconnected(self) -> None:
        pass

    def _note_attempt_failed(self) -> None:
        pass

    async def _wait_before_retry(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.now += delay

    _run = COORD.TrumaCoordinator._run
    _reconnect_delay = COORD.TrumaCoordinator._reconnect_delay
    manual_session_active = COORD.TrumaCoordinator.manual_session_active


def _dwell(coord: _Coord) -> float:
    """Einen Poll fahren und zurückgeben, wie lange der Link offen blieb."""
    started = coord.clock.now
    assert asyncio.run(coord._finish_startup(_Client())) is True
    return coord.clock.now - started


# -- Die Anfrage selbst ---------------------------------------------------


def test_zero_minutes_is_one_refresh_not_forever() -> None:
    """Die häufigste Fehlbedienung wäre ein Dauer-Link aus Versehen."""
    coord = _Coord(connected=True)
    asyncio.run(coord.async_request_manual_session(0))

    assert coord._manual_hold_until == 0.0
    assert coord.manual_session_active is False
    assert coord.discovered == 1, "der Refresh hat gar nicht gelesen"
    assert coord.measured == 1, "die Messfühler wurden nicht gefragt"


def test_a_connected_request_holds_from_now() -> None:
    """Eine zweite Anfrage verlängert ab jetzt, sie addiert nicht."""
    coord = _Coord(connected=True)
    asyncio.run(coord.async_request_manual_session(2))
    assert coord._manual_hold_until == 220.0
    assert coord.manual_session_active is True

    coord.clock.now = 110.0
    asyncio.run(coord.async_request_manual_session(5))
    assert coord._manual_hold_until == 410.0, "kumuliert statt ab jetzt"


def test_a_disconnected_request_defers_the_clock() -> None:
    """Ein langsamer Handshake darf die Live-Zeit nicht auffressen."""
    coord = _Coord(connected=False)
    # Der Nutzer hat eben noch „beenden" gedrückt. Der Wunsch muss sofort
    # fallen, nicht erst nach dem Handshake: bis dahin kann noch eine
    # Verweilschleife eines gewöhnlichen Polls laufen, und die läse ihn.
    coord._manual_release_requested = True

    async def _drive() -> None:
        request = asyncio.ensure_future(coord.async_request_manual_session(2))
        await asyncio.sleep(0)
        assert coord._manual_hold_until == 0.0, "Uhr lief schon vor dem Handshake"
        assert coord._manual_hold_request_minutes == 2, "die Dauer ging verloren"
        assert coord._manual_wake_pending is True, "die Schleife wird nicht geweckt"
        assert coord._manual_release_requested is False, (
            "der Wunsch stand noch, während die Anfrage auf den Link wartet"
        )
        assert coord._wake_event.is_set()
        request.cancel()
        try:
            await request
        except BaseException:  # noqa: BLE001 - der Abbruch ist hier das Ziel
            pass

    asyncio.run(_drive())


def test_the_deferred_clock_starts_at_the_end_of_the_handshake() -> None:
    """Erst der geglückte Startup tritt das Fenster an -- und zwar ab dann.

    Der Handshake kostet hier die 25 s, die er auf dem Fahrzeug kostet, und
    die Uhr läuft währenddessen mit. Genau das ist der Punkt: würde das
    Fenster vor dem Startup angetreten, stünde es schon bei 220 statt bei
    245, und der Nutzer bekäme 95 statt 120 Sekunden Live-Zeit. Ein Double,
    dessen Startup keine Zeit kostet, kann diesen Unterschied nicht sehen.
    """
    handshake = 25.0
    coord = _Coord(connected=False, panel_talks=True, handshake_seconds=handshake)
    coord._manual_hold_request_minutes = 2
    coord._manual_wake_pending = True
    start = coord.clock.now

    held = _dwell(coord)

    assert coord._manual_hold_request_minutes is None, "die Anfrage blieb stehen"
    assert coord._manual_wake_pending is False
    assert coord._manual_hold_until == start + handshake + 120, (
        f"Fenster bis {coord._manual_hold_until} statt "
        f"{start + handshake + 120} -- der Handshake wurde mitgerechnet"
    )
    live = held - handshake
    assert live >= 120, (
        f"nur {live}s live -- der Handshake wurde von der Live-Zeit abgezogen"
    )
    assert live <= 122, f"{live}s live statt der angeforderten 120"


def test_rejects_nonsense_durations() -> None:
    """Ein Tippfehler darf keine 999-Stunden-Verbindung ergeben."""
    coord = _Coord(connected=True)
    for bad in (-1, 1.5, COORD._MANUAL_LIVE_MINUTES_MAX + 1, True, "5", None):
        try:
            asyncio.run(coord.async_request_manual_session(bad))
        except Exception as exc:  # noqa: BLE001
            assert "whole number" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"{bad!r} wurde angenommen")
    assert coord.discovered == 0, "eine abgewiesene Dauer hat trotzdem gelesen"


def test_a_silent_panel_is_not_a_successful_refresh() -> None:
    """Ein „live" ohne frische Werte wäre die Unwahrheit im Dashboard."""
    coord = _Coord(connected=True)

    async def _silent(_client) -> None:
        coord.discovered += 1

    coord._discover_params = _silent

    try:
        asyncio.run(coord.async_request_manual_session(5))
    except Exception as exc:  # noqa: BLE001
        assert "no fresh panel parameters" in str(exc), exc
    else:
        raise AssertionError("ein stummes Panel wurde als Erfolg verbucht")

    assert coord._manual_hold_until == 0.0, "das Fenster blieb stehen"


def test_a_failed_refresh_releases_its_own_hold() -> None:
    """Sonst bliebe nach einem Fehler ein 999-Minuten-Fenster stehen."""
    coord = _Coord(connected=True)

    async def _boom(_client) -> None:
        raise RuntimeError("Panel schweigt")

    coord._discover_params = _boom

    try:
        asyncio.run(coord.async_request_manual_session(999))
    except Exception:  # noqa: BLE001
        pass
    else:
        raise AssertionError("der Fehlschlag wurde verschluckt")

    assert coord._manual_hold_until == 0.0
    assert coord._manual_requests == {}, "der Abbruchkanal blieb offen"


def test_a_failed_refresh_does_not_cancel_a_newer_window() -> None:
    """Der langsame Fehlschlag darf das Fenster seines Nachfolgers nicht löschen.

    Zwei Anfragen überlappen sich leicht: eine Automation und ein
    Tastendruck. Räumte die erste beim Scheitern blind auf, verlöre der
    Nutzer das Fenster, das er gerade eben angefordert hat.
    """
    coord = _Coord(connected=True)

    async def _drive() -> None:
        reached = asyncio.Event()
        release = asyncio.Event()

        async def _slow_boom(_client) -> None:
            reached.set()
            await release.wait()
            raise RuntimeError("Panel schweigt")

        coord._discover_params = _slow_boom
        first = asyncio.ensure_future(coord.async_request_manual_session(2))
        await reached.wait()
        assert coord._manual_hold_until == 220.0

        # Die zweite Anfrage kommt durch, während die erste noch hängt.
        coord.clock.now = 130.0
        coord._discover_params = _Coord._discover_params.__get__(coord)
        await coord.async_request_manual_session(5)
        assert coord._manual_hold_until == 430.0

        release.set()
        try:
            await first
        except Exception:  # noqa: BLE001
            pass
        else:
            raise AssertionError("der Fehlschlag wurde verschluckt")

        assert coord._manual_hold_until == 430.0, (
            "die gescheiterte Anfrage hat das neuere Fenster gelöscht"
        )

    asyncio.run(_drive())


def test_ending_a_session_cancels_a_waiting_request() -> None:
    """Wer auf einen Handshake wartet, soll abbrechen können."""
    coord = _Coord(connected=False)
    # Aus der vorigen Sitzung steht noch ein Fenster; der Link ist gerissen
    # und die Anfrage wartet auf den neuen Handshake. Ohne dieses Fenster
    # prüfte die Zusicherung unten wieder nur, dass 0.0 gleich 0.0 ist.
    coord._manual_hold_until = coord.clock.now + 600

    async def _drive() -> None:
        request = asyncio.ensure_future(coord.async_request_manual_session(5))
        await asyncio.sleep(0)
        assert coord._manual_requests, "die Anfrage ist nicht abbrechbar"

        await coord.async_end_manual_session()
        try:
            await asyncio.wait_for(request, timeout=1)
        except asyncio.TimeoutError:
            raise AssertionError("die Anfrage wartet weiter") from None
        except Exception as exc:  # noqa: BLE001
            assert "cancelled" in str(exc), exc
        else:
            raise AssertionError("die Anfrage meldete Erfolg")

        assert coord._manual_hold_until == 0.0, "das Fenster blieb stehen"
        assert coord._manual_hold_request_minutes is None
        assert coord._manual_wake_pending is False

    asyncio.run(_drive())


def test_ending_a_session_leaves_a_running_command_alone() -> None:
    """Auflegen mitten im Befehl wäre der schlimmste Zeitpunkt."""
    coord = _Coord(connected=True)
    # Es läuft wirklich ein Fenster -- sonst prüfte die Zeile unten, dass
    # eine Null eine Null bleibt.
    coord._manual_hold_until = coord.clock.now + 600
    assert coord.manual_session_active is True
    coord._writes_pending = 1
    coord._wake_event.set()

    asyncio.run(coord.async_end_manual_session())

    assert coord._manual_hold_until == 0.0, "das Fenster blieb stehen"
    assert coord.manual_session_active is False
    assert coord._manual_release_requested is True
    assert coord._writes_pending == 1, "der laufende Befehl wurde angetastet"
    assert coord._wake_event.is_set() is True, (
        "der Weck-Impuls des laufenden Befehls wurde gelöscht"
    )


def test_ending_a_session_that_never_ran_does_not_poison_the_next_poll() -> None:
    """Der Knopf ist immer bedienbar -- auch wenn gar nichts läuft.

    Er muss das sein: verschwände er, wenn BLE unten ist, wäre ausgerechnet
    die Bedienung weg, mit der man aus dem Live-Modus wieder herauskommt.
    Also wird er auch im gewöhnlichen Poll-Betrieb gedrückt, wo der Link
    gerade zwischen zwei Abfragen liegt. Bliebe der Wunsch dann stehen,
    schnitte er den *nächsten* Poll nach einer Sekunde ab, und der brächte
    nur noch die Startup-Werte statt allem, was das Panel danach meldet.
    """
    coord = _Coord(connected=True, panel_talks=True)
    assert coord.manual_session_active is False, "hier läuft absichtlich nichts"

    asyncio.run(coord.async_end_manual_session())

    assert coord._manual_release_requested is False, (
        "der Wunsch blieb für den nächsten Poll liegen"
    )

    held = _dwell(coord)

    assert held == COORD._POLL_MAX_DWELL, (
        f"der nächste Poll dauerte {held}s statt {COORD._POLL_MAX_DWELL}s"
    )


def test_a_handshake_that_never_lands_is_reported_as_a_failure() -> None:
    """Ein Sync, der nie zustande kam, darf nicht als „fertig" gelten.

    Der Vorgangs-Sensor liest das Ergebnis dieser Koroutine. Verschluckte
    sie den Zeitablauf, zeigte er „fertig" für eine Synchronisation, die
    nie stattgefunden hat -- die Werte im Dashboard wären so alt wie zuvor.
    """
    coord = _Coord(connected=False)
    was = COORD._WRITE_CONNECT_TIMEOUT
    COORD._WRITE_CONNECT_TIMEOUT = 0.05
    try:
        asyncio.run(coord.async_request_manual_session(5))
    except Exception as exc:  # noqa: BLE001
        assert "did not answer in time" in str(exc), exc
    else:
        raise AssertionError("der ausgebliebene Handshake wurde als Erfolg verbucht")
    finally:
        COORD._WRITE_CONNECT_TIMEOUT = was

    assert coord._manual_hold_until == 0.0, "das Fenster blieb stehen"
    assert coord._manual_requests == {}, "der Abbruchkanal blieb offen"
    assert coord._manual_hold_request_minutes is None, "die Dauer blieb stehen"
    assert coord._manual_wake_pending is False, "der Weck-Impuls blieb stehen"


# -- Ein Befehl widerruft den Release-Wunsch -------------------------------


def test_a_command_revokes_a_release_wish() -> None:
    """„Beenden", dann doch noch ein Befehl: der Nachlauf muss gelten.

    Die Verweilschleife liest den Release-Wunsch *vor* dem Command-Hold --
    aus gutem Grund, sonst ignorierte „Beenden" den Nutzer eine Minute lang
    (siehe unten). Genau deshalb muss ein Befehl den Wunsch widerrufen: er
    hinge sonst über dem Nachlauf und legte unmittelbar nach dem Befehl auf,
    noch bevor das Gerät seine Folgeänderungen gemeldet hat.

    Gemessen wird die Verweildauer, nicht das Flag: ob der Widerruf an der
    richtigen Stelle steht, sagt nur der Link.
    """
    coord = _Coord(connected=True)
    coord._bus.device(HEATER).param_meta["AirHeating.TgtTemp"] = {
        "perm": 1, "min": 50, "max": 300,
    }
    coord._manual_release_requested = True

    def _drive_the_command(c: _Coord) -> None:
        if c.ticks == 1:
            c.pending_task = asyncio.ensure_future(
                c.async_write_many([(HEATER, "AirHeating", "TgtTemp", 210)])
            )
        elif c.ticks == 5:
            c.write_done.set()

    coord.on_tick = _drive_the_command

    held = _dwell(coord)

    assert coord.pending_task is not None and coord.pending_task.done()
    assert held >= COORD._COMMAND_HOLD_SECONDS, (
        f"nach {held}s aufgelegt -- der Release-Wunsch hat den "
        f"{COORD._COMMAND_HOLD_SECONDS}s-Nachlauf des Befehls ausgehebelt"
    )
    assert coord._manual_release_requested is False, "der Wunsch blieb stehen"


def test_a_deferred_window_is_not_cut_short_by_an_older_release_wish() -> None:
    """Dieselben zwei Tastendrücke, diesmal ohne stehenden Link.

    Die Anfrage wird bis zum Handshake aufgeschoben. Überlebte der alte
    Wunsch ihn, legte die Verweilschleife in der ersten Sekunde des eben
    angetretenen Fensters wieder auf -- der Nutzer bekäme genau einen Poll
    statt seiner zwei Minuten.
    """
    handshake = 25.0
    coord = _Coord(connected=False, panel_talks=True, handshake_seconds=handshake)
    coord._manual_release_requested = True
    coord._manual_hold_request_minutes = 2
    coord._manual_wake_pending = True

    held = _dwell(coord)

    assert coord._manual_release_requested is False, "der alte Wunsch überlebte"
    live = held - handshake
    assert live >= 120, (
        f"nur {live}s live -- der alte Release-Wunsch hat das aufgeschobene "
        f"Fenster abgeschnitten"
    )


def test_a_manual_refresh_revokes_a_release_wish() -> None:
    """„Beenden", dann „jetzt synchronisieren" bei stehender Verbindung.

    Die frische Anfrage ist die jüngere Willensäußerung. Widerriefe sie den
    alten Wunsch nicht, legte die Schleife auf, kaum dass der Refresh durch
    ist -- das angeforderte Fenster wäre schon wieder zu.
    """
    coord = _Coord(connected=True, panel_talks=True)
    coord._manual_release_requested = True

    def _ask_again(c: _Coord) -> None:
        if c.ticks == 1:
            # Zwei Tastendrücke kurz hintereinander: „beenden" hat den Wunsch
            # gesetzt, „jetzt synchronisieren" kommt eine Sekunde später.
            c.pending_task = asyncio.ensure_future(
                c.async_request_manual_session(2)
            )

    coord.on_tick = _ask_again

    held = _dwell(coord)

    assert coord.pending_task is not None and coord.pending_task.done()
    assert coord.pending_task.exception() is None, coord.pending_task.exception()
    assert held >= 120, (
        f"nach {held}s aufgelegt -- der alte Release-Wunsch hat das eben "
        f"angeforderte Fenster von 120s abgeschnitten"
    )


# -- Die Prüfreihenfolge der Verweilschleife ------------------------------
#
# Jeder dieser vier Tests misst die Verweildauer eines Polls. Wäre die
# geprüfte Reihenfolge vertauscht, bräche die Schleife beim ersten Tick ab
# (oder eben zu spät) -- und die Zahl stimmte nicht mehr.


def test_a_release_waits_for_a_running_write() -> None:
    """Writes vor Release: sonst legt die Schleife mitten im Befehl auf."""
    coord = _Coord()
    coord._writes_pending = 1
    # Das Fenster, das der Release-Zweig zu räumen hat. Stünde es schon auf
    # 0.0, sagte die letzte Zusicherung nichts.
    coord._manual_hold_until = coord.clock.now + 600
    coord._manual_release_requested = True

    def _finish_the_write(c: _Coord) -> None:
        if c.ticks == 5:
            c._writes_pending = 0

    coord.on_tick = _finish_the_write

    held = _dwell(coord)

    assert held == 5, (
        f"nach {held}s aufgelegt -- der Befehl lief bis Sekunde 5"
    )
    assert coord._manual_release_requested is False, "der Wunsch blieb liegen"
    assert coord._manual_hold_until == 0.0, (
        "der Release-Zweig hat das Fenster stehen lassen"
    )


def test_a_release_waits_for_a_running_manual_request() -> None:
    """Ein manueller Lesevorgang besitzt die Sitzung genauso wie ein Befehl."""
    coord = _Coord()
    coord._manual_requests = {7: asyncio.Event()}

    def _finish_the_read(c: _Coord) -> None:
        if c.ticks == 5:
            c._manual_requests = {}

    coord.on_tick = _finish_the_read

    held = _dwell(coord)

    assert held == 5, f"nach {held}s aufgelegt -- der Lesevorgang lief noch"


def test_a_release_ends_the_dwell_inside_a_command_hold() -> None:
    """Release vor Command-Hold: „Beenden" darf nicht eine Minute warten."""
    coord = _Coord()
    coord._command_hold_until = coord.clock.now + COORD._COMMAND_HOLD_SECONDS
    coord._manual_release_requested = True

    held = _dwell(coord)

    assert held == 1, (
        f"{held}s festgehalten -- der Nachlauf hat den Release überstimmt"
    )


def test_a_command_hold_outranks_the_live_window() -> None:
    """Command-Hold vor Live-Fenster: sonst reißt der Watchdog den Nachlauf ab.

    Das Panel schweigt hier länger als der Stall-Watchdog erlaubt. Solange
    der Nachlauf läuft, darf ihn das nicht interessieren -- er ist genau für
    die Sekunden nach einem Befehl da, in denen das Gerät erst nachdenkt.
    """
    coord = _Coord()
    coord._command_hold_until = coord.clock.now + COORD._COMMAND_HOLD_SECONDS
    coord._manual_hold_until = coord.clock.now + 600
    # Der Startup setzt den Watchdog auf jetzt zurueck; dieses Panel schweigt
    # ab der ersten Sekunde laenger, als er erlaubt.
    coord.on_tick = lambda c: setattr(c, "_last_frame", 0.0)

    held = _dwell(coord)

    assert held >= COORD._COMMAND_HOLD_SECONDS, (
        f"nach {held}s aufgelegt -- der Stall-Watchdog hat den Nachlauf "
        f"abgeschnitten"
    )
    assert held <= COORD._COMMAND_HOLD_SECONDS + 1, f"{held}s festgehalten"


def test_the_live_window_outranks_both_silence_and_max_dwell() -> None:
    """Live-Fenster vor Stille: ein Live-Link hält, auch wenn niemand redet.

    Beendet wird er trotzdem -- vom Stall-Watchdog, der hier der einzige
    Ausweg ist. Damit ist gleich mit geprüft, dass der Live-Zweig seinen
    Watchdog behält und die Messfühler weiter fragt.
    """
    coord = _Coord()
    coord._manual_hold_until = coord.clock.now + 600

    held = _dwell(coord)

    assert held > COORD._POLL_QUIET, f"nach {held}s auf Stille aufgelegt"
    assert held > COORD._POLL_MAX_DWELL, f"nach {held}s auf die Verweilgrenze"
    assert held <= COORD._DATA_STALL_TIMEOUT + 1, (
        f"{held}s festgehalten -- der Stall-Watchdog fehlt im Live-Zweig"
    )
    assert coord.measured >= 1, "im Live-Fenster wurde nie nachgemessen"


def test_an_expired_live_window_hangs_up_on_silence_again() -> None:
    """Die Gegenprobe: ohne Fenster gilt wieder die gewöhnliche Stille."""
    coord = _Coord()
    coord._manual_hold_until = coord.clock.now + 10

    held = _dwell(coord)

    assert held > COORD._POLL_QUIET, (
        f"nach {held}s aufgelegt -- das Fenster galt gar nicht"
    )
    assert held <= 11, (
        f"{held}s festgehalten -- nach dem Fenster gilt wieder die Stille"
    )


# -- Der Weg zwischen zwei Sitzungen --------------------------------------


def test_a_live_request_in_the_gap_wakes_the_loop_at_once() -> None:
    """Eine Anfrage zwischen zwei Polls darf nicht den Poll-Takt abwarten.

    Der Weck-Impuls ist die Wahrheit, das Event nur der Anstoß -- wer nur
    das Event prüfte, löschte es hier als Erstes und wartete dann darauf.
    """
    coord = _Coord()
    coord._manual_wake_pending = True
    coord._wake_event.set()

    started = time.monotonic()
    asyncio.run(coord._wait_before_retry(3.0))

    assert time.monotonic() - started < 1.0, "die Anfrage wartet den Takt ab"
    assert coord._wake_event.is_set(), "der Anstoß wurde beim Warten gelöscht"

    # Gegenprobe: ohne Anfrage wird wirklich gewartet.
    idle = _Coord()
    started = time.monotonic()
    asyncio.run(idle._wait_before_retry(0.05))
    assert time.monotonic() - started >= 0.04, "es wurde gar nicht gewartet"


def test_the_wake_pulse_is_consumed_by_the_attempt_it_woke() -> None:
    """Sonst bliebe er stehen und die Schleife drehte ohne Pause durch.

    ``_wait_before_retry`` kehrt bei gesetztem Impuls sofort zurück (siehe
    oben) -- ein Impuls, den niemand verbraucht, hebelt also den ganzen
    Backoff aus, gerade wenn das Panel unerreichbar ist.
    """
    coord = _RunCoord()
    coord._manual_wake_pending = True

    asyncio.run(coord._run())

    assert coord._manual_wake_pending is False, "der Impuls blieb stehen"
    assert coord.delays == [
        COORD._RECONNECT_DELAY_BASE,
        COORD._RECONNECT_DELAY_BASE * 2,
        COORD._RECONNECT_DELAY_MAX,
    ], f"der Backoff wuchs nicht: {coord.delays}"


def _main() -> None:
    stubs.run_tests(globals(), "Live mode")


if __name__ == "__main__":
    _main()
