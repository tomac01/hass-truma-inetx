#!/usr/bin/env python3
"""Offline-Prüfungen der Modustabellen und der Bedienelemente am Klimaentity.

Kein Gerät, kein Home Assistant: HA ist gedoubelt, ``climate.py`` wird echt
geladen.

Warum diese Datei. Ein Mutationstest fand die beiden Modustabellen und die
Auswahl der Bedienelemente ungeprüft. Die bestehenden Klimatests fahren den
Sollwert und die Betriebsart (``test_cooling_entities``), die Wertebereiche
(``test_bus_range_edges``) und die vom Panel aufgezählten Optionen
(``test_panel_declared_options``) -- was ein Drahtwert *bedeutet* und welche
Zahl beim Umschalten *hinausgeht*, prüfte keiner.

Zwei Dinge sind dabei absichtlich so geschrieben, wie sie hier stehen:

Erstens wird das Verhalten geprüft, nicht der Tabelleninhalt. ``_MODE_TO_HVAC``
liest mit ``.get(mode, HVACMode.OFF)``, und ``hvac_modes`` schiebt ``OFF``
ohnehin nach vorn, wenn es fehlt -- der Eintrag ``0: HVACMode.OFF`` ist darum
nachweislich gleichwertig und *soll* von keiner Prüfung getroffen werden. Eine
Prüfung, die die Tabelle abschreibt, wäre zu grob: sie erzwänge eine Zeile,
deren Fehlen nichts ändert.

Zweitens zählt das Panel in ``hvac_modes`` die Null mit auf, so wie es ein
echtes Panel tut. Bei einer Aufzählung, die mit einem anderen Wert beginnt,
wanderte ``OFF`` an eine andere Stelle -- auch das träfe die gleichwertige
Zeile, ohne etwas Wirkliches zu prüfen.

Run: ``python3 tests/test_climate_modes.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
CLIMATE = stubs.load("climate")

HVACMode = CLIMATE.HVACMode
Feature = CLIMATE.ClimateEntityFeature


def _climate() -> tuple[stubs.FakeCoordinator, object]:
    """Ein benanntes Heizgerät am Bus, dazu seine Klimaentität."""
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    coordinator.data.connected = True
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    return coordinator, CLIMATE.TrumaClimate(coordinator, HEATER)


def _declare_every_mode(coordinator: stubs.FakeCoordinator) -> None:
    """Das Panel zählt alle sechs Betriebsarten auf.

    Nötig, weil der Doppelgänger jeden Schreibbefehl so prüft, wie der echte
    Coordinator es tut: ohne Aufzählung gilt die Vorgabe ``[0, 3, 5]``, und
    ein Umschalten auf Automatik flöge als ungültig heraus. Kein Fahrzeug
    dieser Sammlung zählt wirklich alle sechs auf -- hier geht es um die
    Tabelle, nicht um ein Fahrzeug.
    """
    coordinator.describe(
        "RoomClimate",
        "Mode",
        PANEL,
        v=0,
        enum=[
            {"n": str(value), "a": True, "v": value}
            for value in (0, 1, 2, 3, 4, 5, 6)
        ],
    )


# -- was ein Drahtwert bedeutet --------------------------------------------


def test_every_wire_value_means_what_the_table_says() -> None:
    """Die Betriebsart, wie sie vom Draht gelesen wird.

    4 und 6 stehen hier mit Absicht: sie sind allein aus der Entschlüsselung
    benannt, kein Fahrzeug hat sie bisher aufgezählt, und genau deshalb fiele
    ihr Verlust sonst niemandem auf.
    """
    coordinator, climate = _climate()
    for wire, expected in (
        (0, HVACMode.OFF),
        (1, HVACMode.AUTO),
        (2, HVACMode.COOL),
        (3, HVACMode.HEAT),
        (4, HVACMode.HEAT),  # Heizen mit Klimaunterstützung -- immer noch Heizen
        (5, HVACMode.FAN_ONLY),
        (6, HVACMode.DRY),
    ):
        coordinator.report("RoomClimate", "Mode", wire, PANEL)
        assert climate.hvac_mode is expected, (
            f"Drahtwert {wire} las sich als {climate.hvac_mode}, erwartet {expected}"
        )


def test_an_unknown_wire_value_reads_as_off() -> None:
    """Ein Wert, den die Tabelle nicht kennt, darf nicht durchschlagen.

    Das ist zugleich der Grund, warum der Eintrag für die Null gleichwertig
    ist: der Vorgabewert fängt ihn ohnehin.
    """
    coordinator, climate = _climate()
    coordinator.report("RoomClimate", "Mode", 99, PANEL)
    assert climate.hvac_mode is HVACMode.OFF


def test_no_mode_at_all_reads_as_nothing() -> None:
    """Ohne gemeldete Betriebsart ist die Antwort ``None``, nicht ``OFF``.

    Ein Panel, das noch nichts gesagt hat, ist nicht dasselbe wie eine
    Heizung, die aus ist.
    """
    _, climate = _climate()
    assert climate.hvac_mode is None


# -- welche Zahl beim Umschalten hinausgeht --------------------------------


def test_switching_a_mode_sends_the_value_that_mode_has() -> None:
    """Die Rückrichtung, gemessen am geschriebenen Drahtwert.

    Die Tabelle ist nicht die Umkehrung der Lesetabelle: 4 wird nie
    geschrieben -- Heizen mit Klimaunterstützung schickt das einfache Heizen.
    """
    coordinator, climate = _climate()
    _declare_every_mode(coordinator)
    for mode, wire in (
        (HVACMode.OFF, 0),
        (HVACMode.AUTO, 1),
        (HVACMode.COOL, 2),
        (HVACMode.HEAT, 3),
        (HVACMode.FAN_ONLY, 5),
        (HVACMode.DRY, 6),
    ):
        asyncio.run(climate.async_set_hvac_mode(mode))
        assert coordinator.writes[-1] == (HEATER, "RoomClimate", "Mode", wire), (
            f"{mode} schrieb {coordinator.writes[-1]}, erwartet Drahtwert {wire}"
        )


def test_turning_on_and_off_goes_through_the_same_table() -> None:
    """Die beiden Schalter, die Home Assistant selbst bedient."""
    coordinator, climate = _climate()
    _declare_every_mode(coordinator)
    asyncio.run(climate.async_turn_on())
    assert coordinator.writes[-1] == (HEATER, "RoomClimate", "Mode", 3)
    asyncio.run(climate.async_turn_off())
    assert coordinator.writes[-1] == (HEATER, "RoomClimate", "Mode", 0)


# -- welches Bedienelement die laufende Betriebsart bekommt ----------------


def test_each_mode_offers_only_the_control_it_uses() -> None:
    """Heizen bekommt den Sollwert, Lüften die Lüfterstufe -- nicht umgekehrt.

    Beim Vertauschen bietet die Karte im Heizen eine Lüfterstufe, die das
    Panel selbst wählt, und im Lüften einen Sollwert, den es nicht gibt. Beide
    Richtungen werden geprüft, sonst genügte einer Prüfung ein Element, das
    immer da ist.
    """
    coordinator, climate = _climate()

    coordinator.report("RoomClimate", "Mode", 3, PANEL)
    features = climate.supported_features
    assert features & Feature.TARGET_TEMPERATURE, "Heizen ohne Sollwert"
    assert not features & Feature.FAN_MODE, (
        "Heizen bot die Lüfterstufe an, die das Panel selbst wählt"
    )

    coordinator.report("RoomClimate", "Mode", 5, PANEL)
    features = climate.supported_features
    assert features & Feature.FAN_MODE, "Lüften ohne Lüfterstufe"
    assert not features & Feature.TARGET_TEMPERATURE, (
        "Lüften bot einen Sollwert an, den diese Betriebsart nicht hat"
    )


def test_off_and_the_unknown_case_keep_the_setpoint() -> None:
    """Aus ist der Ruhe-Sollwert, auf den man zurückkommt.

    So verhält sich jeder andere Thermostat in Home Assistant; und solange
    das Panel nichts gesagt hat, ist der Sollwert die bessere Vorgabe als
    eine Lüfterstufe.
    """
    coordinator, climate = _climate()

    coordinator.report("RoomClimate", "Mode", 0, PANEL)
    assert climate.supported_features & Feature.TARGET_TEMPERATURE

    _, fresh = _climate()
    assert fresh.hvac_mode is None
    assert fresh.supported_features & Feature.TARGET_TEMPERATURE


def test_on_and_off_are_offered_in_every_mode() -> None:
    """Ein- und Ausschalten hängt an keiner Betriebsart."""
    coordinator, climate = _climate()
    for wire in (0, 1, 2, 3, 5, 6):
        coordinator.report("RoomClimate", "Mode", wire, PANEL)
        features = climate.supported_features
        assert features & Feature.TURN_ON and features & Feature.TURN_OFF, wire


# -- der Schritt, in dem der Sollwert bewegt wird --------------------------


def test_the_setpoint_moves_in_whole_degrees() -> None:
    """Ein Schritt von 1 °C, obwohl der Draht in Zehnteln zählt.

    ``wire_to_celsius`` teilt durch zehn, ein Schieberegler mit 0,1er-Schritten
    wäre also technisch möglich -- das Panel selbst stellt aber in ganzen Grad,
    und ein feinerer Regler verspräche eine Genauigkeit, die das Gerät nicht
    einlöst.
    """
    _, climate = _climate()
    assert climate.target_temperature_step == 1


def _main() -> None:
    stubs.run_tests(globals(), "Climate modes")


if __name__ == "__main__":
    _main()
