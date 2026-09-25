#!/usr/bin/env python3
"""Prüft die drei Bedienelemente des Live-Modus.

Warum das hier steht: Es sind lokale Bedienelemente, keine Messwerte.
Wären sie an den BLE-Link gekoppelt, wäre ausgerechnet der Knopf
verschwunden, mit dem man die Verbindung holt.

Was der Test festnagelt:

1. beide Buttons und die Dauer-Number entstehen unbedingt, an keiner Row
   hängend, auf dem Panel und mit je eigener Identität,
2. alle drei bleiben verfügbar, während BLE unten ist -- und zwar nachweislich
   anders als eine gewöhnliche Parameter-Entität, die dabei verschwindet,
3. die Dauer ist eine ganzzahlige Box von 0 bis 999 Minuten, die Grenzwerte
   annimmt, alles andere abweist und dabei den zuletzt gültigen Wert behält,
4. das Stellen der Dauer fasst BLE nicht an,
5. sie wird nach einem Neustart wiederhergestellt -- sie meldet sich dafür
   nachweislich bei der Zustandssicherung an und kommt auch dann noch hoch,
   wenn das Wiederhergestellte unbrauchbar ist,
6. der Sync-Button reicht genau die eingestellte Dauer weiter,
7. die Upstream-Entitäten (Fehler-Reset, Parameter-Numbers) bleiben erhalten,
8. alle drei sind benannt, übersetzt und beikoniert.

Run: ``python3 tests/test_live_mode_entities.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

PANEL = 0x0101
HEATER = 0x0201

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
BUTTON = stubs.load("button")
NUMBER = stubs.load("number")


class _Coordinator(stubs.FakeCoordinator):
    """Der Coordinator, so weit die Bedienelemente ihn anfassen."""

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self.manual_live_minutes = 0
        self.requests: list[int] = []
        self.stops = 0

    async def async_request_manual_session(self, minutes: int) -> None:
        self.requests.append(minutes)

    async def async_end_manual_session(self) -> None:
        self.stops += 1


def _names(entities) -> list[str]:
    return [type(e).__name__ for e in entities]


def _by_class(entities, name: str):
    for entity in entities:
        if type(entity).__name__ == name:
            return entity
    raise AssertionError(f"{name} fehlt in {_names(entities)}")


def _duration(coordinator):
    return _by_class(stubs.setup_platform(NUMBER, coordinator),
                     "TrumaManualLiveMinutes")


def test_both_buttons_exist_from_setup() -> None:
    """Sie hängen an keinem Busparameter."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(BUTTON, coordinator)

    sync = _by_class(made, "TrumaManualSyncButton")
    stop = _by_class(made, "TrumaManualStopButton")
    duration = _duration(coordinator)

    # Alle drei gehören dem Panel -- es ist der Link, den sie bedienen, und
    # nicht ein Gerät dahinter.
    assert (sync._addr, stop._addr, duration._addr) == (PANEL, PANEL, PANEL)
    # Und jede hat ihre eigene, dauerhafte Identität: eine geteilte unique_id
    # verwaist eine der drei beim nächsten Start.
    ids = {e.unique_id for e in (sync, stop, duration)}
    assert len(ids) == 3, ids
    for entity, key in ((sync, "manual_sync"), (stop, "manual_stop"),
                        (duration, "manual_live_minutes")):
        assert entity.unique_id.endswith(f"_{PANEL:04X}_{key}"), entity.unique_id
        assert entity._attr_translation_key == key


def test_the_upstream_reset_button_still_works() -> None:
    """Die neuen Buttons dürfen den Fehler-Reset nicht verdrängen."""
    bus = BUS.Bus()
    coordinator = _Coordinator(bus)
    made = stubs.setup_platform(BUTTON, coordinator)

    coordinator.describe("ErrorReset", "Req", HEATER, perm=1, v=0)
    reset = _by_class(made, "TrumaResetButton")
    assert reset._addr == HEATER


def test_the_upstream_number_rows_still_work() -> None:
    """Dasselbe für die Number-Plattform: die Dauer kommt hinzu, ersetzt nicht."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(NUMBER, coordinator)

    coordinator.describe("Panel", "Intst", PANEL, min=10, max=100, v=100)
    bright = _by_class(made, "TrumaNumber")
    assert bright._attr_translation_key == "panel_brightness"
    assert (bright.native_min_value, bright.native_max_value) == (10, 100)


def test_all_three_stay_available_while_ble_is_down() -> None:
    """Sonst wäre der Knopf weg, mit dem man die Verbindung holt."""
    bus = BUS.Bus()
    bus.connected = False
    coordinator = _Coordinator(bus)

    buttons = stubs.setup_platform(BUTTON, coordinator)
    numbers = stubs.setup_platform(NUMBER, coordinator)

    assert _by_class(buttons, "TrumaManualSyncButton").available
    assert _by_class(buttons, "TrumaManualStopButton").available
    assert _by_class(numbers, "TrumaManualLiveMinutes").available

    # Gegenprobe, damit die drei Zusicherungen oben nicht bloß davon leben,
    # dass in diesem Testaufbau ohnehin alles verfügbar ist: eine gewöhnliche
    # Parameter-Entität verschwindet bei unten liegendem Link sehr wohl.
    coordinator.describe("Panel", "Intst", PANEL, min=10, max=100, v=100)
    assert _by_class(numbers, "TrumaNumber").available is False


def test_duration_is_a_whole_number_box_from_zero_to_999() -> None:
    coordinator = _Coordinator(BUS.Bus())
    duration = _duration(coordinator)

    assert duration._attr_native_min_value == 0
    assert duration._attr_native_max_value == 999
    assert duration._attr_native_step == 1
    assert duration._attr_mode == "box"
    assert duration._attr_native_unit_of_measurement == "min"
    # Voreinstellung ist das sichere einmalige Lesen, nicht ein Fenster.
    assert duration._attr_native_value == 0

    # Die Grenzen selbst gehören dazu, und ein angenommener Wert landet in
    # beiden Ablagen: der Entität und dem Coordinator, den der Knopf liest.
    for good in (0, 999, 42):
        asyncio.run(duration.async_set_native_value(good))
        assert duration._attr_native_value == good
        assert coordinator.manual_live_minutes == good

    # ``True`` ist in Python eine 1 und käme sonst als eine Minute durch.
    # Zahlen in Zeichenketten stehen hier bewusst nicht: Home Assistant wandelt
    # den Dienstaufruf vor uns in ein float um, hier kommt nie eine an.
    for bad in (-1, 1.5, 1000, True, None):
        try:
            asyncio.run(duration.async_set_native_value(bad))
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"{bad!r} wurde angenommen")
        # Abgewiesen heißt unverändert: die Prüfung steht vor dem Merken.
        assert duration._attr_native_value == 42, bad
        assert coordinator.manual_live_minutes == 42, bad


def test_setting_the_duration_touches_no_ble() -> None:
    """Es ist eine lokale Einstellung -- sie darf das Panel nicht wecken."""
    coordinator = _Coordinator(BUS.Bus())
    duration = _duration(coordinator)

    asyncio.run(duration.async_set_native_value(5))

    assert coordinator.writes == []
    assert coordinator.requests == []


def test_duration_restores_and_forwards() -> None:
    """Nach einem Neustart soll die gewählte Dauer wieder dastehen."""
    coordinator = _Coordinator(BUS.Bus())
    duration = _duration(coordinator)

    duration._restored = stubs.SimpleNamespace(native_value=37)
    asyncio.run(duration.async_added_to_hass())

    # Erst die Verdrahtung: der Aufruf an die Basisklasse ist in echtem Home
    # Assistant die Anmeldung bei RestoreStateData. Fällt er weg, wird nie
    # etwas gespeichert und es gibt beim nächsten Start nichts zu holen --
    # ein Ausfall, den die beiden Zusicherungen darunter allein nicht sehen,
    # weil sie den Zweig *nach* dem Wiederherstellen prüfen.
    assert duration.restore_registered
    assert duration._attr_native_value == 37
    assert coordinator.manual_live_minutes == 37


def test_a_restart_without_a_usable_value_falls_back_to_zero() -> None:
    """Weder ein fehlender noch ein kaputter Stand darf den Start kosten."""
    for restored in (None, stubs.SimpleNamespace(native_value=None),
                     stubs.SimpleNamespace(native_value=4000)):
        coordinator = _Coordinator(BUS.Bus())
        # Ein Wert, der nur vom Wiederherstellen stammen kann: er muss weg.
        coordinator.manual_live_minutes = 55
        duration = _duration(coordinator)
        duration._attr_native_value = 55
        if restored is not None:
            duration._restored = restored

        asyncio.run(duration.async_added_to_hass())

        # Auch der Rückfall auf 0 zählt nur, wenn die Entität überhaupt
        # angemeldet ist -- sonst ist die 0 bloß die Voreinstellung.
        assert duration.restore_registered, restored
        assert duration._attr_native_value == 0, restored
        assert coordinator.manual_live_minutes == 0, restored


def test_sync_button_forwards_the_configured_duration() -> None:
    coordinator = _Coordinator(BUS.Bus())
    coordinator.manual_live_minutes = 12
    made = stubs.setup_platform(BUTTON, coordinator)

    asyncio.run(_by_class(made, "TrumaManualSyncButton").async_press())
    asyncio.run(_by_class(made, "TrumaManualStopButton").async_press())

    assert coordinator.requests == [12]
    assert coordinator.stops == 1

    # Die eingestellte Dauer wird beim Druck gelesen, nicht beim Bauen.
    coordinator.manual_live_minutes = 3
    asyncio.run(_by_class(made, "TrumaManualSyncButton").async_press())
    assert coordinator.requests == [12, 3]


def test_the_three_controls_are_named_and_iconed() -> None:
    """Ohne Namen und Icon steht ein Knopf ohne Beschriftung im Dashboard."""
    strings = json.loads((SRC / "strings.json").read_text())["entity"]
    icons = json.loads((SRC / "icons.json").read_text())["entity"]
    de = json.loads((SRC / "translations" / "de.json").read_text())["entity"]
    en = json.loads((SRC / "translations" / "en.json").read_text())["entity"]

    for platform, key in (
        ("button", "manual_sync"),
        ("button", "manual_stop"),
        ("number", "manual_live_minutes"),
    ):
        for label, table in (("strings", strings), ("de", de), ("en", en)):
            assert key in table.get(platform, {}), f"{platform}.{key} fehlt in {label}"
            assert table[platform][key].get("name"), f"{platform}.{key} ohne Namen"
        assert key in icons.get(platform, {}), f"{platform}.{key} ohne Icon"

    # Der Fehler-Reset des Upstreams bleibt dabei, wo er war.
    assert "error_reset" in strings["button"]
    assert "error_reset" in icons["button"]
    assert "error_reset" in de["button"]


def _main() -> None:
    stubs.run_tests(globals(), "Live mode entities")


if __name__ == "__main__":
    _main()
