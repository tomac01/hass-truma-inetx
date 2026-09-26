#!/usr/bin/env python3
"""Prüft die Zustandsflags, die Entitäten für den Nutzer verfügbar machen.

Warum das hier steht: ``coordinator.py`` trägt ein halbes Dutzend Flags, an
denen hängt, ob im Dashboard etwas zu sehen ist -- ``bus.connected`` und
``bus.discovered`` an erster Stelle. Eine Mutationsmessung am 2026-09-26 hat
dreissig Stellen darin gefunden, die kein Test bemerkt, und der Grund war
immer derselbe: **kein Test baut den echten ``TrumaCoordinator``.** Jede
Testdatei leiht sich einzelne Methoden auf ein Doppel, das die Flags von Hand
setzt -- und ein Doppel, das ``self._stop = False`` selbst schreibt, kann
nicht sehen, dass der echte Konstruktor ``True`` schreibt.

Diese Datei baut ihn deshalb im Original. Ersetzt sind nur die Nähte nach
draussen: Home Assistants ``DataUpdateCoordinator`` und ``Store``, die BLE-
Sitzung (``session.run_startup``) und der Link selbst. Alles, was Flags setzt
oder liest, ist echt -- einschliesslich ``__init__``.

Und geprüft wird, wo möglich, am **beobachtbaren Ergebnis**: nicht „das Flag
steht auf True", sondern „nach vollständigem Startup gibt es den
Temperatursensor des Heizgeräts, er ist verfügbar und liest 19,5 °C". Dafür
laufen hier die echten Plattformen ``sensor`` und ``binary_sensor`` mit. Ein
Umbau, der ein Flag umbenennt, bleibt so grün; einer, der die Entität
verschwinden lässt, wird rot.

Was die Datei festnagelt:

1.  Die Identität ist der Panelname, nicht die rotierende Adresse -- sonst
    wandert jede Entitäts- und Geräte-ID (coordinator.py:201),
2.  ein frisch gebauter Coordinator kommt überhaupt zu einem Anwahlversuch,
    ein gestoppter nicht mehr (:260),
3.  sein erster Poll läuft bis zur Stille, statt nach einer Sekunde
    abzubrechen oder für immer an einem Befehl zu hängen (:268, :295),
4.  die gespeicherte App-Identität wird ganz und ohne Buchhaltungsschlüssel
    weitergegeben, und ein Plattenformat der Version 1 bleibt lesbar (:721,
    ``_STORAGE_VERSION``),
5.  ein Abriss im Dauerverbindungs-Betrieb nimmt die Entitäten mit, ein
    planmässig beendeter Poll nicht (:783),
6.  nach vollständigem Startup ist das Entitäten-Tor offen (:1194) und die
    Entität verfügbar (:1207),
7.  ``_mark_disconnected`` meldet den Abriss wirklich, und genau einmal
    (:1426),
8.  dazu die mittleren und niedrigen Fundstellen desselben Bündels: der
    Panel-Link-Sensor startet auf „getrennt", die Adressart-Erinnerung
    überlebt einen Abriss, der Backoff wächst nur bei echten Fehlversuchen,
    die Reparatur-Meldung wartet ihre Fehlgriffe ab, ein wertloser Frame
    entwertet keine Bestätigung, ein Wartepfad ohne brauchbaren Link wird
    abgewiesen, auf einem toten Link wird nicht weitergeschrieben und ein
    Einzelbefehl wird nicht gegen den Bus-Cache gehalten.

Run: ``python3 tests/test_coordinator_state_flags.py``
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# Erfundene Werte, keine aus einem Aufbau: jedes Adress-Oktett besteht aus
# doppelten Zeichen (siehe tests/test_placeholder_addresses.py).
PANEL = "Truma iNetX-BBCCDD"
ADDRESS = "AA:BB:CC:DD:EE:FF"
PHANTOM = "11:22:33:44:55:66"
# Die Adresse, die das Panel *uns* bei der Registrierung zuweist. Absichtlich
# nicht ``DEV_APP_DEFAULT`` (0x0500), damit die Zusicherung darüber nicht den
# Vorgabewert des Busses gegen sich selbst stellt.
APP_ADDR = 0x0501
HEATER = 0x0201
PANEL_ADDR = 0x0101

# Die gespeicherte App-Identität plus einen Buchhaltungsschlüssel, der dem
# Protokoll nichts zu sagen hat. Keine Doppelpunkte darin: eine UUID mit
# Bindestrichen ist von einer Adresse zu unterscheiden.
STORED = {
    "muid": "11111111-2222-3333-4444-555555555555",
    "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "username": "Home Assistant",
    "address_kind": "identity",
}

stubs.install_homeassistant()
stubs.stub_transport()
# Die Sitzungsschritte haben eigene Dateien; hier ersetzt ``_Coord`` sie.
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          discover_params=None, StartupFailed=RuntimeError, handle_frame=None)


def _frame(*_a, **_kw) -> bytes:
    """Steht für einen gebauten V3-Frame; sein Byte-Layout prüft Task 1."""
    return b"frame"


stubs.mod("truma_pkg.truma.protocol", build_write_frame=_frame,
          build_v3_frame=_frame)


# -- Die zwei Home-Assistant-Klassen, die der echte __init__ anfasst -------


class _Base:
    """``DataUpdateCoordinator``, so weit der Coordinator sie benutzt.

    Der gemeinsame Stub in ``stubs.py`` ist bewusst eine Klasse, die man nur
    unterklassen kann -- er reicht jeder Datei, die den Konstruktor nie ruft.
    Hier wird er gerufen, also muss die Basis die vier Argumente annehmen und
    das eine Verhalten mitbringen, das für Entitäten zählt:
    ``async_set_updated_data`` legt die Daten hin und weckt die Zuhörer. Genau
    daran hängt, ob eine Plattform eine Entität baut und ob eine gebaute
    Entität ihren neuen Zustand sieht.

    ``published`` zählt die Veröffentlichungen mit: in echtem Home Assistant
    ist jede eine Zustandsschreibung an alle Entitäten, und „wie oft wurde der
    Abriss gemeldet" ist eine Frage an diese Zahl.
    """

    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None,
                 update_interval=None) -> None:
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.name = name
        self.update_interval = update_interval
        # Wie in echtem Home Assistant: leer, bis zum ersten Refresh.
        self.data = None
        self.last_update_success = True
        self.published = 0
        self._listeners: list = []

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self.published += 1
        for callback in list(self._listeners):
            callback()

    def async_add_listener(self, callback, context=None):
        self._listeners.append(callback)

        def _drop() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _drop


# Was in diesem Augenblick „auf der Platte" liegt: ``None`` für eine frische
# Installation, sonst ein Blob in Home Assistants eigener Form.
DISK: dict | None = None


def _seed_disk(blob: dict | None) -> dict | None:
    """Den Plattenstand für den nächsten gebauten ``Store`` festlegen."""
    global DISK
    DISK = blob
    return blob


class _Store:
    """``Store`` mit Home Assistants Versionsvertrag, nicht nur mit dessen Form.

    Der Vertrag ist der Punkt: ``Store.async_load`` ruft bei einer abweichenden
    gespeicherten Version ``_async_migrate_func``, und die Basisklasse wirft
    dort ``NotImplementedError``. Der Coordinator leitet ``Store`` nicht ab und
    hat keine Migrationsfunktion -- eine hochgezählte ``_STORAGE_VERSION``
    macht damit jede bereits beschriebene Platte unlesbar, und weil
    ``async_start`` in ``__init__.py`` ohne ``try`` gerufen wird, scheitert das
    Setup jeder bestehenden Installation nach dem Update.

    Ein Doppel, das jede Version annimmt, könnte das nicht zeigen. Darum wirft
    dieses, und ``test_a_version_the_store_cannot_migrate_is_not_silently_reset``
    belegt, dass es das wirklich tut.
    """

    def __init__(self, _hass, version: int, key: str, **kwargs) -> None:
        self.version = version
        self.key = key
        self.migrate = kwargs.get("migrate_func")
        # Der Stand beim Bauen -- so wie eine echte Datei schon daliegt.
        self.blob = _seed_disk(DISK)
        # Was dieser Lauf neu geschrieben hat. ``None`` heisst: nichts.
        self.saved: dict | None = None

    async def async_load(self) -> dict | None:
        if self.blob is None:
            return None
        if self.blob["version"] != self.version and self.migrate is None:
            raise NotImplementedError(
                f"{self.key}: gespeicherte Version {self.blob['version']} "
                f"gegen erwartete {self.version}, und keine Migration"
            )
        return dict(self.blob["data"])

    async def async_save(self, data: dict) -> None:
        self.saved = dict(data)
        self.blob = {"version": self.version, "data": dict(data)}


sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = _Base
sys.modules["homeassistant.helpers.storage"].Store = _Store

stubs.load_truma("const")
stubs.load("bus")
stubs.load("const")
stubs.load("operations")
COORD = stubs.load("coordinator")
# Die echten Plattformen: sie sind es, die aus einem Flag eine Entität machen
# oder eben nicht. Nach dem Coordinator geladen, damit ihr ``from .coordinator
# import ...`` genau die Klasse trifft, die hier gebaut wird.
SENSOR = stubs.load("sensor")
BINARY = stubs.load("binary_sensor")

# Die Kennung, unter der Home Assistant den Temperatursensor des Heizgeräts
# führt -- also das, was der Nutzer behält, wenn sich die Identität verschiebt.
TEMP_SENSOR = "_0201_AirHeating.Temp_sensor"
PANEL_LINK = "_0101_connection"


# -- Uhr, Link und Entry --------------------------------------------------


class _Clock:
    """Virtuelle Loop-Uhr, von den Sleeps des Codes selbst weitergestellt.

    Die Verweilschleife tickt im Sekundentakt um Fenster von Minuten herum;
    in echter Zeit wäre jede Prüfung hier ein Minutentest.
    """

    def __init__(self) -> None:
        self.now = 100.0

    def time(self) -> float:
        return self.now


class _FastForward:
    """``asyncio``-Ersatz, dessen ``sleep`` die Uhr weiterstellt.

    Jeder Tick ruft zusätzlich den Haken des Coordinators -- damit ein Test
    mitten in der Verweilschleife den Link fallen lassen kann, so wie es in
    echt ein zweiter Task täte. Alles andere ist das echte ``asyncio``.
    """

    def __init__(self, clock: _Clock, coord: "_Coord") -> None:
        self._clock = clock
        self._coord = coord

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self._clock.now += delay
        self._coord.tick()
        await asyncio.sleep(0)


class _Client:
    """Ein Link, der steht und schweigt -- mehr braucht der Startup nicht."""

    transport = "local"

    def __init__(self, *, connected: bool = True) -> None:
        self.assigned_addr = APP_ADDR
        self.connected = connected


class _WriteClient(_Client):
    """Ein Link, der Schreib-Frames mitschreibt und Sonden schluckt.

    ``on_write`` ist der Haken für das, was *während* eines Befehls passiert:
    ein fallender Link, eine eintreffende Rückmeldung. Er steht hier und nicht
    im Test, weil beides in echt genau in diesem Augenblick geschieht.
    """

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.probes = 0
        self.on_write = lambda _client: None

    async def send(self, frame: bytes, probe: bool = False) -> bool:
        if probe:
            self.probes += 1
            return True
        self.writes.append(frame)
        self.on_write(self)
        return True


class _Entry:
    """Der Config-Entry, so weit der Coordinator ihn liest."""

    def __init__(self, *, unique_id: str | None, options: dict | None = None,
                 entry_id: str = "01") -> None:
        self.unique_id = unique_id
        self.entry_id = entry_id
        self.options = dict(options or {})
        self.runtime_data = None
        self.unloads: list = []

    def async_on_unload(self, unsub) -> None:
        self.unloads.append(unsub)


class _Coord(COORD.TrumaCoordinator):
    """Der echte Coordinator; ersetzt sind nur die Nähte nach draussen.

    ``__init__`` läuft im Original -- das ist der ganze Zweck dieser Datei.
    Überschrieben sind die vier Koroutinen, die in echt über BLE gehen, und
    jede von ihnen tut, was die echte an Zustand hinterlässt: der Startup
    setzt ``bus.discovered`` (wie ``session.discover_params``, session.py:253)
    und legt den Wert ab, den das Heizgerät im Startup meldet.
    """

    handshake_seconds = 0.0
    panel_talks = False

    def __init__(self, hass, entry, address) -> None:
        super().__init__(hass, entry, address)
        self.clock = hass.loop
        self.ticks = 0
        self.startups = 0
        self.discoveries = 0
        self.measured = 0
        self.on_tick = lambda _coord: None

    def tick(self) -> None:
        """Eine Sekunde der Verweilschleife."""
        self.ticks += 1
        if self.panel_talks:
            self._last_frame = self.clock.now
        self.on_tick(self)

    async def _run_startup(self, _client) -> None:
        """Steht für ``session.run_startup``; hat eigene Testdateien.

        Er kostet hier Zeit und hinterlässt Zustand, weil beides zählt: ein
        Startup, der gratis nichts tut, macht „vor" und „nach" ihm zum selben
        Zeitpunkt und jede Prüfung darüber blind.
        """
        self.startups += 1
        self.clock.now += self.handshake_seconds
        self._bus.discovered = True
        self._bus.update("AirHeating", "Temp", 195, HEATER)

    async def _discover_params(self, _client) -> None:
        self.discoveries += 1
        self._bus.update("AirHeating", "Temp", 196, HEATER)

    async def _request_measurements(self, _client) -> None:
        self.measured += 1


def _make_coord(*, poll_interval: int = 0, unique_id: str | None = PANEL,
                address: str = ADDRESS, disk: dict | None = None) -> _Coord:
    """Einen echten Coordinator bauen, wie ``__init__.py`` es täte."""
    _seed_disk(disk)
    clock = _Clock()
    entry = _Entry(unique_id=unique_id,
                   options={COORD.CONF_POLL_INTERVAL: poll_interval})
    hass = SimpleNamespace(loop=clock, data={})
    coord = _Coord(hass, entry, address)
    entry.runtime_data = coord
    COORD.asyncio = _FastForward(clock, coord)
    return coord


# -- Werkzeug -------------------------------------------------------------


def _hold(coord: _Coord, client: _Client | None = None, *,
          drop_at: int | None = None, limit: int = 400) -> tuple[bool, float]:
    """Einen vollständigen Startup fahren.

    Liefert den Rückgabewert und die Verweildauer, also wie lange der Link
    danach offen blieb. ``drop_at`` lässt ihn nach so vielen Sekunden fallen.

    ``limit`` ist die Notbremse: eine Mutation, die eine Abbruchbedingung
    aushebelt, soll den Testlauf nicht anhalten, sondern eine viel zu lange
    Verweildauer melden. Sie setzt beides -- Stop *und* gefallener Link --,
    damit sie auch dann greift, wenn die Schleifenbedingung selbst mutiert ist.
    """
    client = client if client is not None else _Client()
    earlier = coord.on_tick

    def _hook(c: _Coord) -> None:
        earlier(c)
        if drop_at is not None and c.ticks >= drop_at:
            client.connected = False
        if c.ticks >= limit:
            c._stop = True
            client.connected = False

    coord.on_tick = _hook
    started = coord.clock.now
    result = asyncio.run(coord._finish_startup(client))
    return result, coord.clock.now - started


def _drive_run(coord: _Coord, outcomes: list) -> tuple[list, list]:
    """Die Wiederanwahl-Schleife im Original fahren.

    ``outcomes`` sagt je Runde, wie die Sitzung endet: ``True`` sie kam
    zustande, ``False`` sie kam nicht zustande, ``"raise"`` der Versuch platzte
    mit einer Ausnahme.

    Der Stop wird in der *Pause* gesetzt, nicht im Versuch: die Schleife liest
    ``_stop`` in der Verzweigung über ``_mark_disconnected``, und ein Stop, der
    schon während der letzten Runde stünde, verfälschte genau diese Prüfung.
    """
    rounds: list = []
    delays: list = []

    async def _connect_and_run() -> bool:
        outcome = outcomes[len(rounds)]
        rounds.append(outcome)
        if outcome == "raise":
            raise RuntimeError("kein Link")
        return outcome

    async def _wait_before_retry(delay: float) -> None:
        delays.append(delay)
        coord.clock.now += delay
        if len(delays) >= len(outcomes):
            coord._stop = True

    coord._connect_and_run = _connect_and_run
    coord._wait_before_retry = _wait_before_retry
    asyncio.run(coord._run())
    return rounds, delays


def _entities(platform, coord: _Coord) -> list:
    """Eine echte Plattform einrichten und liefern, was sie gebaut hat."""
    made: list = []
    asyncio.run(
        platform.async_setup_entry(
            coord.hass, coord.config_entry, lambda new: made.extend(new)
        )
    )
    return made


def _one(made: list, suffix: str) -> object:
    """Genau die Entität, deren Kennung so endet."""
    found = [e for e in made if str(e.unique_id).endswith(suffix)]
    assert len(found) == 1, (
        f"{len(found)} Entitäten enden auf {suffix} -- gebaut wurden "
        f"{[str(e.unique_id) for e in made]}"
    )
    return found[0]


def _temp_sensor(coord: _Coord) -> object:
    """Den Temperatursensor des Heizgeräts, den der Nutzer im Dashboard sieht."""
    return _one(_entities(SENSOR, coord), TEMP_SENSOR)


async def _settle() -> None:
    """Dem Loop ein paar Runden geben, damit ein Task sein ``await`` erreicht."""
    for _ in range(10):
        await asyncio.sleep(0)


def _expect_error(coro) -> BaseException:
    """Die Koroutine fahren und die Ausnahme liefern, die sie melden muss."""
    async def _run():
        try:
            await coro
        except BaseException as exc:  # noqa: BLE001 - der Fehler ist das Ziel
            return exc
        return None

    exc = asyncio.run(_run())
    assert exc is not None, "der Vorgang meldete Erfolg"
    return exc


# -- Die Identität, aus der jede Entitäts- und Geräte-ID gebaut wird ------


def test_the_panel_name_is_the_identity_not_the_rotating_address() -> None:
    """coordinator.py:201 -- sonst wandert jede Entitäts-ID mit der MAC.

    Die Adresse des Panels ist eine rotierende RPA. Wird sie zur Identität,
    heisst der Temperatursensor nach dem nächsten Rotationswechsel anders, und
    Home Assistant hängt die Historie an der alten Kennung ab.
    """
    coord = _make_coord(poll_interval=0)
    _hold(coord, drop_at=3)

    temp = _temp_sensor(coord)
    assert temp.unique_id == f"{PANEL}{TEMP_SENSOR}", (
        f"die Entität heisst {temp.unique_id!r} -- erwartet war der Panelname "
        f"{PANEL!r} als Identität, nicht die rotierende Adresse"
    )
    assert ADDRESS not in str(temp.unique_id), (
        "die BLE-Adresse steckt in der Entitäts-Kennung und wandert mit jeder "
        "Rotation"
    )
    # Und dasselbe für das Gerät, an dem sie hängt: ``device_info`` ist, was
    # der Coordinator der Geräteregistrierung übergibt.
    identifiers = coord.device_info(HEATER)["identifiers"]
    assert (COORD.DOMAIN, f"{PANEL}_0201") in identifiers, (
        f"das Gerät hängt an {identifiers} statt am Panelnamen"
    )


def test_without_a_stored_panel_name_the_address_is_the_identity() -> None:
    """Die Gegenprobe: ohne Panelname bleibt nur die Adresse.

    Sie ist die schlechtere Identität, aber die einzige -- ``None`` wäre keine
    und stände als Zeichenkette in jeder Kennung.
    """
    coord = _make_coord(poll_interval=0, unique_id=None)
    _hold(coord, drop_at=3)

    temp = _temp_sensor(coord)
    assert temp.unique_id == f"{ADDRESS}{TEMP_SENSOR}", (
        f"die Entität heisst {temp.unique_id!r} -- ohne Panelname muss die "
        f"Adresse einspringen"
    )


# -- Der Anfangszustand, den jedes Doppel bisher selbst gesetzt hat -------


def test_a_fresh_coordinator_runs_a_session_and_a_stopped_one_does_not() -> None:
    """coordinator.py:260 -- mit ``_stop = True`` kommt gar keine Sitzung.

    ``while not self._stop`` in ``_run`` träte dann nie ein: die Integration
    käme hoch, hielte eine leere Bus-Struktur und versuchte nie, das Panel
    anzuwählen. Geprüft wird darum nicht der Anfangswert, sondern ob ein
    Anwahlversuch stattfindet.
    """
    coord = _make_coord(poll_interval=300)

    rounds, _ = _drive_run(coord, ["raise"])
    assert rounds == ["raise"], (
        "ein frisch gebauter Coordinator kam zu keinem einzigen "
        "Anwahlversuch -- es kommt nie eine BLE-Sitzung zustande"
    )

    asyncio.run(coord.async_stop())
    stopped, _ = _drive_run(coord, ["raise"])
    assert stopped == [], (
        "nach dem Stop wurde weiter angewählt -- das Entladen des Entry "
        "lässt eine Sitzung zurück"
    )


def test_the_first_poll_of_a_fresh_coordinator_ends_on_silence() -> None:
    """coordinator.py:268 und :295 -- der erste Poll nach jedem Neustart.

    Zwei Anfangswerte treffen sich in dieser einen Zahl. Ein beim Bauen
    gesetzter Release-Wunsch bricht den Poll nach einer Sekunde ab, und der
    brächte nur noch die Startup-Werte statt allem, was das Panel danach
    meldet. Ein vorgetäuschter laufender Befehl (``_writes_pending``) legt
    umgekehrt überhaupt nicht mehr auf -- der Poll-Betrieb, der den
    Verbindungsplatz gerade freigeben soll, hielte den Link für immer.
    """
    coord = _make_coord(poll_interval=300)

    result, dwell = _hold(coord)

    assert result is True, "ein gelaufener Poll wurde als Fehlversuch gemeldet"
    assert dwell == COORD._POLL_QUIET, (
        f"der erste Poll hielt den Link {dwell}s statt {COORD._POLL_QUIET}s -- "
        f"zu kurz heisst abgeschnitten (Release-Wunsch), zu lang heisst, er "
        f"glaubt an einen laufenden Befehl"
    )


def test_the_panel_link_sensor_starts_disconnected() -> None:
    """coordinator.py:214 -- der Sensor liest genau dieses Feld.

    Mit ``True`` meldet er „verbunden", bevor je eine Sitzung lief: das
    Dashboard zeigt einen Link, den es nicht gibt.
    """
    coord = _make_coord(poll_interval=300)
    # Was Home Assistants erster Refresh tut: den Bus veröffentlichen.
    # ``_async_update_data`` gibt genau ihn zurück.
    coord.async_set_updated_data(asyncio.run(coord._async_update_data()))

    link = _one(_entities(BINARY, coord), PANEL_LINK)
    assert link.is_on is False, (
        'der Panel-Link-Sensor meldet „verbunden", bevor eine Sitzung lief'
    )

    coord._set_panel_link_connected(True)
    assert link.is_on is True, (
        "der Sensor folgt dem Link nicht -- dann sagte die Zusicherung oben "
        "nichts über den Anfangswert"
    )


def test_the_repair_issue_waits_for_the_agreed_number_of_misses() -> None:
    """coordinator.py:255 -- der Zähler startet bei 0, nicht bei 1.

    Startete er bei 1, erschiene die Reparatur-Meldung nach jedem Neustart
    einen Fehlgriff zu früh -- und ein einzelner Fehlversuch beim Hochfahren
    ist genau das, was sie *nicht* meldet.
    """
    coord = _make_coord(poll_interval=300)
    created: list = []
    was_advertising = COORD.async_panel_advertising
    was_ir = COORD.ir
    # Das Panel ist hörbar; nur so zählt ein Fehlgriff überhaupt.
    COORD.async_panel_advertising = lambda *_a: True
    COORD.ir = SimpleNamespace(
        async_create_issue=lambda *a, **kw: created.append(kw.get("translation_key")),
        async_delete_issue=lambda *a, **kw: None,
        IssueSeverity=SimpleNamespace(WARNING="warning"),
    )
    try:
        for _ in range(COORD.NO_ROUTE_MISSES_BEFORE_WARNING - 1):
            coord._async_note_no_route()
        assert created == [], (
            f"die Meldung erschien schon nach "
            f"{COORD.NO_ROUTE_MISSES_BEFORE_WARNING - 1} Fehlgriffen"
        )
        coord._async_note_no_route()
        assert len(created) == 1, (
            f"nach {COORD.NO_ROUTE_MISSES_BEFORE_WARNING} Fehlgriffen wurde "
            f"{created} gemeldet"
        )
    finally:
        COORD.async_panel_advertising = was_advertising
        COORD.ir = was_ir


def test_ending_a_session_that_never_ran_leaves_no_wake_pulse() -> None:
    """coordinator.py:435 -- „Beenden" löscht den Weck-Impuls, es setzt ihn nicht.

    Der Knopf ist absichtlich immer bedienbar, wird also auch gedrückt, wenn
    gar kein Fenster läuft. Bliebe danach ein Weck-Impuls stehen, kehrte
    ``_wait_before_retry`` sofort zurück: es folgte gleich der nächste Poll
    statt der Pause, und das auf dem geteilten Bluetooth-Adapter.

    Gemessen wird deshalb die Pause selbst, nicht das Flag.
    """
    coord = _make_coord(poll_interval=300)

    async def _drive() -> float:
        assert coord.manual_session_active is False, "hier läuft absichtlich nichts"
        await coord.async_end_manual_session()
        started = time.monotonic()
        await coord._wait_before_retry(0.05)
        return time.monotonic() - started

    waited = asyncio.run(_drive())
    assert waited >= 0.04, (
        f'die Pause dauerte {waited:.3f}s statt 0,05s -- nach „Beenden" steht '
        f"ein Weck-Impuls, und der nächste Poll kommt ohne Pause"
    )


# -- Die gespeicherte Identität -------------------------------------------


def test_the_stored_identity_is_handed_over_whole_and_nothing_else() -> None:
    """coordinator.py:721 -- die drei Protokollschlüssel, und nur die.

    ``build_identity_frames`` greift hart auf ``identity['username']`` zu: ein
    leeres ``_identity`` lässt jeden Startup mit ``KeyError`` sterben, die
    Integration kommt nie hoch. Umgekehrt hat die eigene Buchhaltung im selben
    Blob auf dem Draht nichts zu suchen.

    Gleich mit geprüft: das Plattenformat. Der Blob liegt auf Version 1 --
    genau der, die eine bestehende Installation geschrieben hat. Eine
    hochgezählte ``_STORAGE_VERSION`` ohne Migration macht diese Prüfung rot,
    statt beim Nutzer das Setup zu zerlegen.
    """
    coord = _make_coord(poll_interval=300,
                        disk={"version": 1, "data": dict(STORED)})

    asyncio.run(coord._load_stored_state())

    assert coord._identity == {key: STORED[key]
                               for key in ("muid", "uuid", "username")}, (
        f"dem Protokoll wurde {coord._identity} übergeben statt der "
        f"gespeicherten App-Identität"
    )
    assert "address_kind" not in (coord._identity or {}), (
        "die eigene Buchhaltung wird mit auf den Draht gegeben"
    )
    assert coord._store.saved is None, (
        "die gespeicherte Identität wurde beim Laden neu geschrieben -- ein "
        "Update verliert damit muid und uuid"
    )
    assert coord._prefer_identity() is True, (
        "die gespeicherte Adressart wurde nicht befolgt: der Host wählt wieder "
        "die Route, die auf ihm nie funktioniert (#13)"
    )


def test_a_version_the_store_cannot_migrate_is_not_silently_reset() -> None:
    """Gegenprobe zum Store-Doppel: ohne diese Schärfe prüfte es nichts.

    Ein Doppel, das jede Version annimmt, liesse eine hochgezählte
    ``_STORAGE_VERSION`` durch -- und damit genau den Fall, der beim Nutzer das
    Setup scheitern lässt.
    """
    _seed_disk({"version": 1, "data": dict(STORED)})
    store = _Store(None, 2, "probe")

    raised = None
    try:
        asyncio.run(store.async_load())
    except NotImplementedError as exc:
        raised = exc

    assert raised is not None, (
        "das Store-Doppel nimmt jede Version an -- dann sagt der Test über "
        "_STORAGE_VERSION nichts"
    )


# -- Der vollständige Startup: was der Nutzer danach sieht ----------------


def test_a_full_startup_makes_a_real_entity_available() -> None:
    """coordinator.py:1194 und :1207 -- das Entitäten-Tor und die Verfügbarkeit.

    :1207 ist die **einzige** Stelle, die ``bus.connected`` auf ``True`` setzt.
    Auf ``False`` gedreht bleiben nach einem vollständigen Startup alle
    Entitäten unavailable -- das Fahrzeug meldet sich, und das Dashboard ist
    grau.

    :1194 wiederholt ``bus.discovered``, das die Discovery schon gesetzt hat.
    Auf ``False`` gedreht **löscht** sie es wieder: das Tor bleibt zu, Geräte
    behalten „Bus device 0xNNNN" und ihre Entitäten entstehen nie (#23).

    Beides wird hier am Ergebnis geprüft: ein echter Sensor, verfügbar, mit
    dem Wert, den das Heizgerät im Startup gemeldet hat.
    """
    coord = _make_coord(poll_interval=0)

    result, _ = _hold(coord, drop_at=3)

    assert result is True, (
        "eine gelaufene Dauerverbindung wurde als Fehlversuch gemeldet -- der "
        "Backoff wächst und die Adressen werden demotiert"
    )
    made = _entities(SENSOR, coord)
    temp = [e for e in made if str(e.unique_id).endswith(TEMP_SENSOR)]
    assert temp, (
        "nach vollständigem Startup gibt es keinen Temperatursensor für das "
        "Heizgerät: das Entitäten-Tor blieb zu, obwohl die Discovery durch "
        f"ist -- gebaut wurden nur {[str(e.unique_id) for e in made]}"
    )
    assert temp[0].available is True, (
        "die Entität ist nach vollständigem Startup nicht verfügbar -- für den "
        "Nutzer ist die Integration damit tot"
    )
    assert temp[0].native_value == 19.5, (
        f"der Sensor liest {temp[0].native_value} statt der gemeldeten 19,5 °C"
    )
    assert coord._bus.assigned_addr == APP_ADDR, (
        f"der Bus kennt unsere zugewiesene Adresse als "
        f"0x{coord._bus.assigned_addr:04X} statt 0x{APP_ADDR:04X} -- Frames "
        f"von uns werden als Gerät verbucht"
    )


def test_a_session_that_ran_and_then_fell_keeps_the_address_memory() -> None:
    """coordinator.py:1195 -- ein Abriss sagt nichts über die Adressart.

    Mit ``_session_ok = False`` gilt eine Sitzung, die lief und dann fiel, als
    „nie gut gewesen": ``_note_attempt_failed`` dreht die Erinnerung um, und
    der Host wählt ab dann die Route, die auf ihm nie funktioniert (#13).
    """
    coord = _make_coord(poll_interval=0,
                        disk={"version": 1, "data": dict(STORED)})
    asyncio.run(coord._load_stored_state())
    assert coord._prefer_identity() is True, "Vorbedingung: die Erinnerung gilt"
    # Die Sitzung ist auf der Identitätsadresse zustande gekommen.
    coord._last_kind = COORD.ADDR_IDENTITY

    _hold(coord, drop_at=3)
    coord._note_attempt_failed()

    assert coord._prefer_identity() is True, (
        "der Abriss einer gelaufenen Sitzung hat die Adressart-Erinnerung "
        "umgedreht"
    )


def test_a_connected_session_that_fell_marks_the_entities_unavailable() -> None:
    """coordinator.py:783 -- sonst bleiben veraltete Werte als aktuelle stehen.

    Mit ``or`` wird ``_mark_disconnected`` übersprungen, sobald nicht gestoppt
    ist. Nach einem Link-Abriss im Dauerverbindungs-Betrieb zeigt das Dashboard
    dann weiter Werte an, als wären sie von jetzt.
    """
    coord = _make_coord(poll_interval=0)
    _hold(coord, drop_at=3)
    temp = _temp_sensor(coord)
    assert temp.available is True, "Vorbedingung: die Sitzung lief"

    rounds, _ = _drive_run(coord, [True])

    assert rounds == [True], f"die Runde lief nicht: {rounds}"
    assert temp.available is False, (
        "nach dem Abriss im Dauerverbindungs-Betrieb ist die Entität weiter "
        "verfügbar und zeigt veraltete Werte als aktuelle"
    )


def test_a_finished_poll_leaves_the_entities_available() -> None:
    """Die Gegenprobe: im Poll-Betrieb ist das Auflegen der Plan.

    Der eben gelesene Wert ist der aktuelle Zustand. Würde hier getrennt
    gemeldet, blinkte jeder Wert auf und ginge bis zum nächsten Poll auf
    „unavailable" -- gemessen, und der Grund für die Verzweigung.
    """
    coord = _make_coord(poll_interval=300)
    _hold(coord)
    temp = _temp_sensor(coord)
    assert temp.available is True, "Vorbedingung: der Poll lief"

    _drive_run(coord, [True])

    assert temp.available is True, (
        "ein planmässig beendeter Poll hat die Entitäten unavailable gemacht"
    )


def test_a_poll_lets_go_of_a_link_that_fell_under_it() -> None:
    """coordinator.py:1233 -- die Verweilschleife verlässt einen toten Link.

    Ein Befehl besitzt hier die Sitzung, die Schleife endet also nicht auf
    Stille: der einzige Ausweg ist der gefallene Link. Mit ``or`` dreht sie im
    Befehls-Nachlauf bis zu einer Minute auf einem toten Client weiter und
    verzögert das Entladen.
    """
    coord = _make_coord(poll_interval=300)
    coord._writes_pending = 1

    _result, dwell = _hold(coord, drop_at=3)

    assert dwell == 3, (
        f"die Schleife hielt {dwell}s an einem Link fest, der nach 3s gefallen "
        f"war"
    )


def test_a_dropped_link_takes_the_entities_with_it_exactly_once() -> None:
    """coordinator.py:1426 -- ``_mark_disconnected`` meldet den Abriss wirklich.

    Setzt die Zeile das Flag auf denselben Wert, den sie eben geprüft hat, wird
    der Abriss nie gemeldet: alle Entitäten bleiben dauerhaft „verfügbar" mit
    veralteten Werten. Und genau einmal geweckt, weil ein zweiter Aufruf sonst
    jede Entität ohne Anlass neu schreibt.
    """
    coord = _make_coord(poll_interval=0)
    _hold(coord, drop_at=3)
    temp = _temp_sensor(coord)
    assert temp.available is True, "Vorbedingung: die Sitzung lief"

    before = coord.published
    coord._mark_disconnected()

    assert temp.available is False, (
        "der Link-Abriss wurde nicht gemeldet -- die Entität bleibt verfügbar "
        "und zeigt veraltete Werte"
    )
    assert coord.published == before + 1, (
        f"die Entitäten wurden {coord.published - before}-mal geweckt statt "
        f"genau einmal"
    )

    coord._mark_disconnected()
    assert coord.published == before + 1, (
        "ein zweiter Abriss hat die Entitäten erneut geweckt, obwohl sich "
        "nichts geändert hat"
    )


# -- Die Wiederanwahl-Leiter ----------------------------------------------


def test_an_attempt_that_never_connected_grows_the_backoff() -> None:
    """coordinator.py:769 -- ein geplatzter Versuch ist keine gelungene Sitzung.

    Mit ``connected = True`` gälte er als eine: kein wachsender Backoff, und
    ``_avoid.clear()`` vergisst die Phantom-RPA, gegen die die Menge da ist.
    Auf dem geteilten Adapter ist das Hämmern genau das, was die Leiter
    verhindert.
    """
    coord = _make_coord(poll_interval=300)
    coord._avoid.add(PHANTOM)

    rounds, delays = _drive_run(coord, ["raise", "raise", "raise"])

    assert len(rounds) == 3, f"die Schleife lief {len(rounds)} Runden"
    assert delays == [
        COORD._RECONNECT_DELAY_BASE,
        COORD._RECONNECT_DELAY_BASE * 2,
        COORD._RECONNECT_DELAY_MAX,
    ], f"der Backoff wuchs nicht: {delays}"
    assert PHANTOM in coord._avoid, (
        "die Phantom-Adresse wurde nach einem Fehlversuch vergessen -- der "
        "nächste Anlauf läuft wieder in ihren ~20-s-Timeout"
    )


def test_a_poll_that_succeeded_then_failed_restarts_the_backoff() -> None:
    """Der Poll-Takt gilt nach einer gelungenen Runde, und der Backoff fängt neu an.

    Mit ``or`` wird der Backoff auch nach einer **gelungenen** Sitzung
    fortgeschrieben: der Poll-Takt wird auf die Backoff-Obergrenze abgesenkt,
    und das Fahrzeug klopft deutlich früher am geteilten Adapter an, als der
    Nutzer eingestellt hat.
    """
    coord = _make_coord(poll_interval=300)

    _rounds, delays = _drive_run(coord, [True, False, False])

    assert delays == [300, 300, COORD._RECONNECT_DELAY_MAX], (
        f"die Wartezeiten waren {delays} statt [300, 300, "
        f"{COORD._RECONNECT_DELAY_MAX}] -- nach einem gelungenen Poll gilt der "
        f"Takt, und der erste Fehlversuch danach startet die Leiter beim Takt"
    )


# -- Der Frame-Pfad während eines Schreibvorgangs -------------------------


def test_a_frame_without_a_value_does_not_spoil_a_pending_confirmation() -> None:
    """coordinator.py:1389 und der Wachposten über tn/pn/v.

    Zwei Fundstellen in einer Prüfung, weil es dieselbe Zustellung ist.

    Ohne den Wachposten über ``tn``/``pn``/``v`` überschreibt eine reine
    Beschreibung (die es gibt: session.py trennt ``learn_param`` von
    ``bus.update`` genau deshalb) eine schon eingetroffene richtige Meldung mit
    ``None``. Der Anlauf läuft dann in den 12-s-Timeout und endet mit „did not
    confirm", obwohl das Gerät ausgeführt hat.

    Und ohne den ``or``-Wachposten in Zeile 1389 fliegt bei jedem Frame ohne
    CBOR ein ``AttributeError`` in den Notify-Pfad -- ``parse_v3_frame``
    liefert für kurze Frames und Nicht-CBOR-Nutzlast ``None``.
    """
    coord = _make_coord(poll_interval=0)
    book = {(HEATER, "WaterHeating", "Active"): 1}
    coord._write_feedback = {7: book}
    expected = dict(book)

    # Eine Beschreibung: tn und pn da, kein Wert.
    coord._note_frame_values({"src": HEATER, "cbor": {
        "tn": "WaterHeating", "pn": "Active", "perm": 1}})
    assert book == expected, (
        f"ein Frame ohne Wert hat die gebuchte Bestätigung entwertet: {book}"
    )

    # Ein Wert ohne Topic und Parameter gehört niemandem.
    coord._note_frame_values({"src": HEATER, "cbor": {"v": 0}})
    assert book == expected, (
        f"ein Wert ohne Topic/Parameter wurde gebucht: {book}"
    )

    # Und beides ohne CBOR: still übergehen, nicht werfen.
    coord._note_frame_values({"src": HEATER})
    coord._note_frame_values({"src": HEATER, "cbor": None})
    assert book == expected, f"ein Frame ohne CBOR hat gebucht: {book}"

    # Gegenprobe: ein vollständiger Frame wird sehr wohl gebucht.
    coord._note_frame_values({"src": HEATER, "cbor": {
        "tn": "WaterHeating", "pn": "Active", "v": 2}})
    assert book[(HEATER, "WaterHeating", "Active")] == 2, (
        f"ein vollständiger Frame wurde nicht gebucht: {book}"
    )


# -- Der Weg eines Befehls zum Link --------------------------------------


def test_a_wait_that_wakes_without_a_usable_link_is_refused() -> None:
    """coordinator.py:1464 -- der Vertrag von ``_client_for_write``.

    Entweder ein **verbundener** Client oder eine saubere Absage. Mit ``and``
    fällt beides: ein toter Client wird herausgegeben und ``_write_confirmed``
    sendet auf einem als tot gemeldeten Link, und bei ``None`` fliegt ein
    ``AttributeError`` statt einer Meldung -- in Home Assistant also ein
    unbehandelter Traceback.

    Der Aufbau grenzt sich über den Wartepfad ab, nicht über den Meldungstext:
    die frühere Absage bei ``poll_interval == 0`` trägt denselben Wortlaut.
    """
    for label, client in (("ein toter Client", _Client(connected=False)),
                          ("gar kein Client", None)):
        coord = _make_coord(poll_interval=300)

        async def _drive(target=client):
            task = asyncio.ensure_future(coord._client_for_write())
            await _settle()
            assert coord._wake_event.is_set(), (
                "der Wartepfad wurde nicht genommen -- die Prüfung trifft die "
                "frühere Absage"
            )
            coord._client = target
            coord._connected_event.set()
            try:
                got = await task
            except BaseException as exc:  # noqa: BLE001 - der Fehler ist das Ziel
                return exc
            raise AssertionError(
                f"{label}: ein unbrauchbarer Link wurde herausgegeben ({got!r})"
            )

        exc = asyncio.run(_drive())
        assert isinstance(exc, COORD.HomeAssistantError), (
            f"{label}: der Befehl endete in {type(exc).__name__}({exc}) statt "
            f"in einer Meldung an den Nutzer"
        )
        assert "not connected" in str(exc), (label, exc)


def test_a_link_that_fell_during_a_command_is_not_written_to_again() -> None:
    """coordinator.py -- auf einem toten Link wird nicht weitergearbeitet.

    Fällt der Link während der Rückmeldefrist, bricht das Original ab. Mit
    ``and`` folgte ein weiterer Anlauf, dessen ``send`` am Transport-Gate
    scheitert -- der Nutzer sähe „did not acknowledge write" statt „did not
    confirm", und ein Write-Frame ginge noch auf den Draht. Diese Fehlertexte
    sind Nutzeroberfläche (Attribut ``error`` des Vorgangs-Sensors).
    """
    coord = _make_coord(poll_interval=0)
    client = _WriteClient()
    # Der Link fällt genau in dem Augenblick, in dem der Befehl draussen ist.
    client.on_write = lambda c: setattr(c, "connected", False)
    started = coord.clock.now

    exc = _expect_error(
        coord._write_confirmed(client, HEATER, "EnergySrc", "ElectricLevel", 1, {})
    )

    assert "did not confirm" in str(exc), (
        f"gemeldet wurde {exc!r} -- erwartet war die Absage des Befehls, nicht "
        f"ein Transportfehler"
    )
    assert len(client.writes) == 1, (
        f"{len(client.writes)} Anläufe -- auf dem toten Link wurde "
        f"weitergeschrieben"
    )
    elapsed = coord.clock.now - started
    assert elapsed < COORD._WRITE_FEEDBACK_TIMEOUT + COORD._WRITE_RETRY_PAUSE, (
        f"der Befehl brauchte {elapsed}s -- nach dem Abbruch darf keine "
        f"Wiederholungspause mehr folgen"
    )


def test_a_single_command_is_not_held_against_the_bus_cache() -> None:
    """coordinator.py:1531 -- die Endprüfung gehört der Mehrfach-Transaktion.

    Mit ``>=`` liefe auch ein Einzelbefehl durch Beruhigungspause und
    Endprüfung gegen den Bus-Cache. Er kostete eine Sekunde mehr und
    scheiterte mit „did not retain", sobald die bestätigende Meldung zwar
    eingetroffen, aber nicht im Bus gelandet ist -- und der Bus verwirft
    einiges, was als Rückmeldung sehr wohl zählt.
    """
    coord = _make_coord(poll_interval=0)
    coord._bus.device(HEATER).param_meta["AirHeating.TgtTemp"] = {
        "perm": 1, "min": 50, "max": 300,
    }
    client = _WriteClient()
    # Das Gerät bestätigt -- aber nur ins Rückmeldungsbuch, nicht in den Bus.
    client.on_write = lambda _c: coord.on_frame_value(
        HEATER, "AirHeating", "TgtTemp", 210
    )
    coord._client = client

    try:
        asyncio.run(coord.async_write(HEATER, "AirHeating", "TgtTemp", 210))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"ein bestätigter Einzelbefehl scheiterte: {exc}"
        ) from None

    assert len(client.writes) == 1, f"{len(client.writes)} Anläufe"
    assert coord._bus.device(HEATER).get("AirHeating", "TgtTemp") is None, (
        "der Wert landete doch im Bus -- dann sagt diese Prüfung nichts"
    )


def test_the_live_window_limit_is_a_literal() -> None:
    """coordinator.py:169 -- die Obergrenze gegen den Vertipper.

    Sie muss literal festgenagelt sein: eine Prüfung, die den unerlaubten Wert
    als ``_MANUAL_LIVE_MINUTES_MAX + 1`` bildet, verschiebt ihre Grenze mit der
    Mutation mit und sieht nichts.
    """
    coord = _make_coord(poll_interval=300)
    coord._client = _Client()
    coord._connected_event.set()

    asyncio.run(coord.async_request_manual_session(999))
    assert coord.discoveries == 1, "die erlaubte Dauer hat nicht gelesen"

    exc = _expect_error(coord.async_request_manual_session(1000))
    assert "whole number" in str(exc), exc
    assert coord.discoveries == 1, "die abgewiesene Dauer hat trotzdem gelesen"


if __name__ == "__main__":
    stubs.run_tests(globals(), "coordinator state flags")
