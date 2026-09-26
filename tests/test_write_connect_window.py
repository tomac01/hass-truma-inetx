#!/usr/bin/env python3
"""Prüft, dass ein Befehl im Abbaufenster eines Polls nicht scheitert.

Warum das hier steht: Im Poll-Betrieb liegt zwischen zwei Abfragen kein
Link. ``_client_for_write`` ist genau dafür da -- es weckt die Schleife
und wartet auf die nächste Sitzung, statt den Befehl abzuweisen. Dieses
Warten hing an ``_connected_event``, und das Event beschrieb eine Weile
lang eine Sitzung, die es nicht mehr gab: ``_disconnect_client`` wirft den
Client als erste Anweisung weg und wartet danach auf ``disconnect()``.

Im ganzen Fenster galt ``_client is None`` bei gesetztem
``_connected_event``. Ein Befehl darin wartete auf ein bereits gesetztes
Event, kehrte nach 0 ms zurück, fand denselben fehlenden Client -- und
meldete „Truma panel is not connected". Für den Nutzer: Knopf gedrückt,
sofort Fehler, obwohl der nächste Poll den Befehl getragen hätte.

Was der Test festnagelt:

1. ein Befehl bei fehlendem Client und noch gesetztem Event wartet und
   wird vom nächsten Poll bedient,
2. dasselbe im echten Abbau, während ``_disconnect_client`` noch läuft,
3. auch ein toter Client mit veraltetem Event lässt warten -- das Fenster
   davor gehört zur selben Ursache,
4. ``_disconnect_client`` lässt das Event nie gesetzt zurück,
5. im Dauerverbindungs-Betrieb (``poll_interval = 0``) wird ohne Link
   weiterhin sofort abgewiesen -- das ist gewollt,
6. ein stehender Link wird unverändert sofort herausgegeben, auch bevor
   der Startup das Event setzt,
7. ein Poll, der nie kommt, endet als Zeitüberschreitung, nicht als
   „nicht verbunden".

Run: ``python3 tests/test_write_connect_window.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

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


class _Client:
    """Ein Link, der steht -- mehr fragt der Schreibpfad nicht ab."""

    transport = "local"

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.disconnects = 0

    async def disconnect(self) -> None:
        self.disconnects += 1


class _SlowClient(_Client):
    """Ein Link, dessen Abbau hängt -- er hält das Fenster auf.

    Auf dem Fahrzeug sind das bis zu fünf Sekunden, in denen der Proxy den
    Verbindungsplatz noch nicht freigegeben hat. Ein Double, das sofort
    zurückkehrt, hätte gar kein Fenster.
    """

    def __init__(self) -> None:
        super().__init__()
        self.released = asyncio.Event()

    async def disconnect(self) -> None:
        await self.released.wait()
        self.disconnects += 1


class _Coord:
    """Nur so viel Coordinator, wie der Wartepfad und der Abbau anfassen."""

    unique_id = "Truma iNetX-BBCCDD"

    def __init__(self, *, poll_interval: int = 300) -> None:
        self.poll_interval = poll_interval
        self._bus = BUS.Bus()
        self._client: _Client | None = None
        self._panel_link_connected = True
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self.updates = 0

    def async_set_updated_data(self, _data) -> None:
        self.updates += 1

    _client_for_write = COORD.TrumaCoordinator._client_for_write
    _disconnect_client = COORD.TrumaCoordinator._disconnect_client
    _set_panel_link_connected = COORD.TrumaCoordinator._set_panel_link_connected


async def _settle() -> None:
    """Dem gestarteten Task Gelegenheit geben, bis zu seinem Warten zu kommen."""
    for _ in range(10):
        await asyncio.sleep(0)


def _assert_still_waiting(task: asyncio.Future, what: str) -> None:
    """Der Befehl darf hier nicht fertig sein -- schon gar nicht als Fehler."""
    if not task.done():
        return
    outcome = task.exception() or task.result()
    raise AssertionError(f"{what}: {outcome!r}")


def _expect_refusal(coro, message: str) -> str:
    """Einen Aufruf laufen lassen, der scheitern muss."""
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001 - genau das ist die Erwartung
        assert message in str(exc), exc
        return str(exc)
    raise AssertionError(f"kein Fehlschlag, erwartet war {message!r}")


# -- Das Fenster ----------------------------------------------------------


def test_a_command_in_the_teardown_window_waits_for_the_next_poll() -> None:
    """Der Zustand des Befunds, direkt gestellt: kein Client, Event gesetzt."""
    coord = _Coord()
    coord._client = None
    coord._connected_event.set()

    async def scenario() -> None:
        task = asyncio.ensure_future(coord._client_for_write())
        await _settle()
        _assert_still_waiting(
            task, "der Befehl endete im Abbaufenster sofort statt zu warten"
        )
        assert coord._wake_event.is_set(), "die Schleife wurde nicht geweckt"

        # Der geweckte Poll steht: erst der Client, dann das Event -- die
        # Reihenfolge aus _finish_startup.
        fresh = _Client()
        coord._client = fresh
        coord._connected_event.set()
        assert await asyncio.wait_for(task, 1) is fresh

    asyncio.run(scenario())


def test_the_real_teardown_window_does_not_refuse_a_command() -> None:
    """Dasselbe, aber vom echten Abbau aufgespannt statt von Hand gestellt."""
    coord = _Coord()
    link = _SlowClient()
    coord._client = link
    coord._connected_event.set()

    async def scenario() -> None:
        teardown = asyncio.ensure_future(coord._disconnect_client())
        await _settle()
        assert coord._client is None, "der Abbau hat den Client noch nicht abgelegt"
        assert not teardown.done(), "der Abbau hängt nicht, also gibt es kein Fenster"

        write = asyncio.ensure_future(coord._client_for_write())
        await _settle()
        _assert_still_waiting(
            write, "der Befehl scheiterte, während der Abbau noch lief"
        )

        link.released.set()
        await asyncio.wait_for(teardown, 1)
        fresh = _Client()
        coord._client = fresh
        coord._connected_event.set()
        assert await asyncio.wait_for(write, 1) is fresh

    asyncio.run(scenario())


def test_a_dead_client_with_a_stale_event_also_waits() -> None:
    """Das Fenster beginnt schon, bevor der Abbau den Client ablegt."""
    coord = _Coord()
    coord._client = _Client(connected=False)
    coord._connected_event.set()

    async def scenario() -> None:
        task = asyncio.ensure_future(coord._client_for_write())
        await _settle()
        _assert_still_waiting(
            task, "ein toter Client mit gesetztem Event ließ den Befehl scheitern"
        )
        fresh = _Client()
        coord._client = fresh
        coord._connected_event.set()
        assert await asyncio.wait_for(task, 1) is fresh

    asyncio.run(scenario())


def test_dropping_the_client_clears_the_event() -> None:
    """Die Zusicherung selbst: das Event überlebt seinen Client nicht."""
    for link in (None, _Client(), _Client(connected=False)):
        coord = _Coord()
        coord._client = link
        coord._connected_event.set()

        asyncio.run(coord._disconnect_client())

        assert coord._client is None
        assert not coord._connected_event.is_set(), (
            "nach dem Abbau behauptet das Event noch eine Verbindung"
        )


# -- Was sich nicht ändern darf -------------------------------------------


def test_connected_mode_still_refuses_a_command_with_no_link() -> None:
    """Ohne Poll gibt es nichts zu wecken -- da ist die Absage richtig."""
    coord = _Coord(poll_interval=0)
    coord._client = None
    coord._connected_event.set()

    _expect_refusal(coord._client_for_write(), "not connected")

    assert not coord._wake_event.is_set(), "im Dauerbetrieb gibt es nichts zu wecken"


def test_a_live_link_is_handed_out_at_once() -> None:
    """Auch vor dem Event: der Startup setzt es erst ganz zum Schluss."""
    for poll_interval in (0, 300):
        coord = _Coord(poll_interval=poll_interval)
        client = _Client()
        coord._client = client
        assert not coord._connected_event.is_set()

        assert asyncio.run(coord._client_for_write()) is client
        assert not coord._wake_event.is_set(), "ein stehender Link weckt niemanden"


def test_a_poll_that_never_answers_is_a_timeout() -> None:
    """Die Absage bleibt -- sie kommt nur nicht mehr nach 0 ms."""
    was = COORD._WRITE_CONNECT_TIMEOUT
    COORD._WRITE_CONNECT_TIMEOUT = 0.05
    try:
        coord = _Coord()
        coord._connected_event.set()
        _expect_refusal(coord._client_for_write(), "did not answer in time")
    finally:
        COORD._WRITE_CONNECT_TIMEOUT = was


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
            print(f"ok  {name}")
    print("test_write_connect_window: alle Prüfungen bestanden")
