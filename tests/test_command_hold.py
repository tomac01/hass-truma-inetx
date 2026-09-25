#!/usr/bin/env python3
"""Prüft den 60-Sekunden-Nachlauf nach einem Befehl.

Warum das hier steht: Nach einer Bedienung kommen meist weitere, und die
Heizung meldet ihre Folgeänderungen verzögert nach. Würde die Verbindung
nach den üblichen 4 Sekunden Stille fallen, müsste jeder zweite Tastendruck
einen ganzen Verbindungsaufbau bezahlen.

Was der Test festnagelt:

1. ohne Nachlauf legt der Poll nach der üblichen Stille auf -- die
   Gegenprobe, ohne die alles Folgende auch für eine Schleife gälte, die
   grundsätzlich nie auflegt,
2. im Nachlauffenster überstimmt der Hold beides: die Stille *und* die
   maximale Verweildauer,
3. er überstimmt sie nicht länger als das Fenster,
4. reißt der Link im Fenster, wird schnell neu verbunden statt im Poll-Takt,
5. ohne Fenster gilt wieder Poll-Takt bzw. der gewachsene Backoff,
6. ein laufendes Live-Fenster wirkt genauso,
7. das Setzen ist absolut, nicht additiv: zehn schnelle Befehle ergeben
   nicht zehn Minuten,
8. die Konstante ist größer als die Fenster, die sie überstimmen soll --
   sonst wäre der ganze Nachlauf wirkungslos.

Run: ``python3 tests/test_command_hold.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

APP_ADDR = 0x0501

stubs.install_homeassistant()
stubs.stub_transport()
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          StartupFailed=RuntimeError, handle_frame=None)
stubs.mod("truma_pkg.truma.protocol", build_write_frame=None,
          build_v3_frame=None)
stubs.load_truma("const")
BUS = stubs.load("bus")
stubs.load("const")
stubs.load("operations")
COORD = stubs.load("coordinator")


class _Clock:
    """Virtuelle Loop-Uhr, von den Sleeps des Codes selbst weitergestellt.

    Die Verweilschleife tickt im Sekundentakt um ein Minutenfenster herum;
    in echter Zeit wäre das hier ein Minutentest.
    """

    def __init__(self) -> None:
        self.now = 100.0

    def time(self) -> float:
        return self.now


class _FastForward:
    """``asyncio``-Ersatz, dessen ``sleep`` die Uhr weiterstellt."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __getattr__(self, name):  # alles andere ist das echte asyncio
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self._clock.now += delay
        await asyncio.sleep(0)


class _Client:
    """Ein Link, der steht und schweigt -- mehr braucht die Verweilschleife."""

    assigned_addr = APP_ADDR
    transport = "local"
    connected = True


class _Coord:
    """Trägt nur, was die Verweil- und Verzögerungslogik anfasst."""

    unique_id = "Truma iNetX-15E02F"

    def __init__(self, poll_interval: int = 300) -> None:
        self.clock = _Clock()
        COORD.asyncio = _FastForward(self.clock)
        self.hass = stubs.SimpleNamespace(loop=self.clock)
        self.poll_interval = poll_interval
        self._bus = BUS.Bus()
        self._command_hold_until = 0.0
        self._manual_hold_until = 0.0
        self._writes_pending = 0
        self._last_frame = 0.0
        self._last_kind = None
        self._session_ok = False
        self._session_transport = None
        self._stop = False
        self._connected_event = asyncio.Event()

    def async_set_updated_data(self, _data) -> None:
        pass

    def async_sync_device_names(self) -> None:
        pass

    async def _run_startup(self, _client) -> None:
        """Steht für Registrierung und Discovery -- haben eigene Dateien."""

    _finish_startup = COORD.TrumaCoordinator._finish_startup
    _hold_after_command = COORD.TrumaCoordinator._hold_after_command
    _reconnect_delay = COORD.TrumaCoordinator._reconnect_delay
    manual_session_active = COORD.TrumaCoordinator.manual_session_active


def _dwell(coord: _Coord) -> float:
    """Einen Poll fahren und zurückgeben, wie lange der Link offen blieb."""
    started = coord.clock.now
    assert asyncio.run(coord._finish_startup(_Client())) is True
    return coord.clock.now - started


def test_poll_hangs_up_on_silence_without_a_hold() -> None:
    """Die Gegenprobe: ohne Nachlauf endet der Poll nach der Stille."""
    coord = _Coord()

    assert _dwell(coord) == COORD._POLL_QUIET


def test_hold_outlasts_both_silence_and_max_dwell() -> None:
    """Im Fenster wird nicht aufgelegt -- auch nicht nach 40 Sekunden."""
    coord = _Coord()
    coord._command_hold_until = coord.clock.now + COORD._COMMAND_HOLD_SECONDS

    held = _dwell(coord)

    assert held >= COORD._COMMAND_HOLD_SECONDS, (
        f"nach {held}s aufgelegt, das Fenster war "
        f"{COORD._COMMAND_HOLD_SECONDS}s lang"
    )
    assert held > COORD._POLL_MAX_DWELL, "die Verweilgrenze hat gewonnen"


def test_hold_does_not_outlast_its_own_window() -> None:
    """Der Nachlauf hält den Link nicht länger als das Fenster."""
    coord = _Coord()
    coord._command_hold_until = coord.clock.now + COORD._COMMAND_HOLD_SECONDS

    held = _dwell(coord)

    # Ein Tick Toleranz: die Schleife merkt den Ablauf erst beim nächsten.
    assert held <= COORD._COMMAND_HOLD_SECONDS + 1, f"{held}s festgehalten"


def test_hold_shortens_the_reconnect_delay() -> None:
    """Reißt der Link im Nachlauffenster, wird schnell neu verbunden."""
    coord = _Coord()
    coord._command_hold_until = 160.0

    assert coord._reconnect_delay(True, 30) == COORD._RECONNECT_DELAY_BASE

    coord.clock.now = 161.0
    assert coord._reconnect_delay(True, 30) == 300, (
        "nach Ablauf gilt wieder der Poll-Takt"
    )


def test_reconnect_delay_without_a_hold() -> None:
    """Die drei Fälle, die der Nachlauf überstimmt, gelten sonst weiter."""
    coord = _Coord()
    assert coord._reconnect_delay(True, 30) == 300, "Poll-Takt"

    dauerlink = _Coord(poll_interval=0)
    assert dauerlink._reconnect_delay(True, 30) == COORD._RECONNECT_DELAY_BASE, (
        "ein gesunder Dauerlink kommt schnell zurück"
    )
    assert dauerlink._reconnect_delay(False, 30) == 30, (
        "ein Fehlversuch behält seinen gewachsenen Backoff"
    )


def test_a_live_window_shortens_it_too() -> None:
    """Live-Modus und Nachlauf wirken auf dieselbe Verzögerung."""
    coord = _Coord()
    coord._manual_hold_until = 160.0

    assert coord.manual_session_active is True
    assert coord._reconnect_delay(True, 30) == COORD._RECONNECT_DELAY_BASE

    coord.clock.now = 161.0
    assert coord.manual_session_active is False
    assert coord._reconnect_delay(True, 30) == 300

    # Am Dauerlink ist die Frage gegenstandslos: der ist ohnehin immer live.
    dauerlink = _Coord(poll_interval=0)
    dauerlink._manual_hold_until = 160.0
    assert dauerlink.manual_session_active is False


def test_hold_is_absolute_not_cumulative() -> None:
    """Zehn schnelle Befehle ergeben nicht zehn Minuten."""
    coord = _Coord()
    for _ in range(10):
        coord.clock.now += 1.0
        coord._hold_after_command()

    assert coord._command_hold_until == (
        coord.clock.now + COORD._COMMAND_HOLD_SECONDS
    ), f"{coord._command_hold_until} statt {coord.clock.now + 60}"


def test_hold_constant_is_longer_than_the_quiet_window() -> None:
    """Sonst wäre der Nachlauf wirkungslos."""
    assert COORD._COMMAND_HOLD_SECONDS > COORD._POLL_QUIET
    assert COORD._COMMAND_HOLD_SECONDS > COORD._POLL_MAX_DWELL


def _main() -> None:
    stubs.run_tests(globals(), "Command hold")


if __name__ == "__main__":
    _main()
