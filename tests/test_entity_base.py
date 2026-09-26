#!/usr/bin/env python3
"""Offline-Prüfungen für die Bauteile in ``entity.py`` selbst.

Kein Gerät, kein Home Assistant: HA ist gedoubelt, ``entity.py`` und
``climate.py`` werden echt geladen.

Warum diese Datei. ``entity.py`` trägt die drei Regeln, nach denen jede
Entität dieser Integration entsteht und verschwindet, und ein Mutationstest
fand sie ungeprüft -- nicht weil niemand sie benutzt, sondern weil die
Plattformtests sie nur auf dem geraden Weg berühren:

1. ``async_add_per_device`` läuft bei *jeder* Aktualisierung erneut. Die
   Plattformtests bauen einmal auf und sehen eine Entität; dass beim zweiten
   Durchlauf keine zweite entsteht, prüfte keiner. Genau das war die Mutation
   ``or`` -> ``and``: erst eine Klimaentität je Busgerät, dann bei jedem
   Refresh eine weitere.
2. ``available`` verbirgt eine Entität, während der BLE-Link unten ist --
   ausser die Entität meldet den Link selbst. Dass die Vorgabe wirklich
   verbirgt, stand nirgends.
3. ``async_add_when_all_reported`` nimmt eine Adresse entgegen, wenn der
   Aufrufer weiss, welches Gerät gemeint ist. Produktiv tut das im Moment
   keiner (``select.py`` übergibt ``None``), und ohne Adresse ist der
   Adressvergleich wirkungslos -- die Prüfung unten fährt darum die
   dokumentierte Schnittstelle direkt an. Siehe die Anmerkung dort.

Run: ``python3 tests/test_entity_base.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201
ROOF_AC = 0x0406
BOILER = 0x0405

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
ENTITY = stubs.load("entity")
CLIMATE = stubs.load("climate")


def _coordinator() -> stubs.FakeCoordinator:
    """Ein Bus nach dem Start: jedes Gerät hat sich benannt."""
    return stubs.FakeCoordinator(BUS.Bus())


# -- 1. eine Entität je meldendem Gerät, und nur einmal ---------------------


def test_only_the_device_that_reports_the_parameter_gets_the_entity() -> None:
    """Vier Geräte am Bus, eines meldet ``AirHeating.Temp`` -- eine Entität.

    Der erste Durchlauf, mit mehr als einem Gerät auf dem Bus. Ohne den
    Vorbehalt ``not device.reports(...)`` bekäme jedes Gerät eine
    Klimaentität, auch der Boiler und das Dachklimagerät.
    """
    coordinator = _coordinator()
    for addr in (PANEL, HEATER, ROOF_AC, BOILER):
        coordinator.report("Identify", "Name", f"Gerät {addr:04X}", addr)
    coordinator.report("AirHeating", "Temp", 228, HEATER)

    made = stubs.setup_platform(CLIMATE, coordinator)

    assert len(made) == 1, [entity._addr for entity in made]
    assert made[0]._addr == HEATER, f"{made[0]._addr:#06x}"


def test_a_second_update_does_not_build_the_entity_again() -> None:
    """Der Durchlauf, den die Plattformtests nie machen.

    ``async_add_per_device`` hängt sich als Listener ein und läuft bei jeder
    Aktualisierung erneut; ``made`` ist das Gedächtnis, das die zweite
    Entität verhindert. Ist es wirkungslos, wächst die Entitätenliste mit
    jedem Frame vom Bus -- unbemerkt, weil ein Aufbau allein noch stimmt.
    """
    coordinator = _coordinator()
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("AirHeating", "Temp", 228, HEATER)

    made = stubs.setup_platform(CLIMATE, coordinator)
    assert len(made) == 1, made

    # Derselbe Wert noch einmal, dann ein anderer, dann ein zweites Gerät, das
    # den Parameter nicht meldet: nichts davon rechtfertigt eine neue Entität.
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    coordinator.report("AirHeating", "Temp", 231, HEATER)
    coordinator.report("Identify", "Name", "Boiler", BOILER)
    coordinator.report("ElectricHeating", "Power", 1, BOILER)

    assert len(made) == 1, [entity._addr for entity in made]


def test_a_device_that_starts_reporting_later_still_gets_one() -> None:
    """Gegenprobe: das Gedächtnis darf nicht *jede* spätere Entität verhindern.

    Ohne diese Prüfung wäre ``async_add_per_device`` auch dann noch grün,
    wenn es nach dem ersten Durchlauf gar nichts mehr baute -- und die
    Prüfung darüber würde das nicht bemerken.
    """
    coordinator = _coordinator()
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("AirHeating", "Temp", 228, HEATER)

    made = stubs.setup_platform(CLIMATE, coordinator)
    assert len(made) == 1, made

    # Ein zweites Gerät, das dieselbe Rolle hat -- auf einem Fahrzeug mit zwei
    # Heizkreisen -- meldet den Parameter erst jetzt.
    coordinator.report("Identify", "Name", "Combi 4", ROOF_AC)
    coordinator.report("AirHeating", "Temp", 215, ROOF_AC)

    assert len(made) == 2, [entity._addr for entity in made]
    assert {entity._addr for entity in made} == {HEATER, ROOF_AC}


# -- 2. verfügbar nur, solange der Link steht ------------------------------


def test_an_entity_is_unavailable_while_the_link_is_down() -> None:
    """Die Vorgabe ``_gate_on_connected = True`` muss wirklich verbergen.

    Ohne sie zeigte die Oberfläche den letzten bekannten Wert, als wäre er
    aktuell, während die Verbindung zum Fahrzeug längst weg ist.
    """
    coordinator = _coordinator()
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    entity = CLIMATE.TrumaClimate(coordinator, HEATER)

    coordinator.data.connected = True
    assert entity.available is True

    coordinator.data.connected = False
    assert entity.available is False, (
        "Entität meldet sich verfügbar, obwohl der BLE-Link unten ist"
    )


def test_an_entity_that_reports_the_link_itself_stays_available() -> None:
    """Gegenprobe: ``_gate_on_connected = False`` hebt die Sperre auf.

    Sonst verbärge sich der Verbindungssensor genau dann, wenn er das einzige
    ist, was noch etwas zu sagen hat. Diese Prüfung hält die Sperre davon ab,
    zu grob zu werden.
    """
    coordinator = _coordinator()
    coordinator.data.connected = False

    class _Ungated(ENTITY.TrumaEntity):
        _gate_on_connected = False

    assert _Ungated(coordinator, HEATER, "link").available is True


# -- 3. der Gerätename im Entitätsnamen ------------------------------------


def test_an_entity_carries_its_device_name() -> None:
    """``has_entity_name`` ist die Voraussetzung für alles, was danach kommt.

    Home Assistant setzt den Anzeigenamen aus Gerätename und Entitätsname
    zusammen, solange das wahr ist. Ist es das nicht, heisst jede Entität nur
    noch nach ihrem Parameter -- auf einem Fahrzeug mit zwei Heizkreisen
    zweimal gleich, und welche zu welchem Gerät gehört, steht nirgends mehr.
    """
    coordinator = _coordinator()
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    assert CLIMATE.TrumaClimate(coordinator, HEATER).has_entity_name is True


def test_the_climate_entity_may_go_nameless_because_of_it() -> None:
    """Die Kopplung, die daran hängt, und der Grund, warum sie zusammen steht.

    Die Klimaentität führt ``_attr_name = None`` und trägt damit den
    Gerätenamen allein -- das ist nur zulässig, solange ``has_entity_name``
    wahr ist. Fiele es weg, bliebe eine Entität ohne jeden Namen übrig. Die
    beiden gehören darum in eine Prüfung.
    """
    coordinator = _coordinator()
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    climate = CLIMATE.TrumaClimate(coordinator, HEATER)

    assert climate._attr_name is None, "die Klimaentität trägt den Gerätenamen"
    assert climate.has_entity_name is True, (
        "_attr_name = None ohne has_entity_name lässt die Entität namenlos"
    )


# -- 4. die Adresse, wenn der Aufrufer eine nennt ---------------------------


def test_a_named_address_gets_the_entity_and_the_others_do_not() -> None:
    """``async_add_when_all_reported`` mit einer Adresse trifft genau sie.

    Anmerkung zum Wert dieser Prüfung: produktiv ruft nur ``select.py`` diese
    Funktion auf, und zwar mit ``None`` -- dann ist der Adressvergleich
    wirkungslos, und die Mutation ``!=`` -> ``==`` bliebe folgenlos. Geprüft
    wird hier also die dokumentierte Schnittstelle, nicht ein Weg, den der
    Produktivcode heute geht. Wer den Parameter für tot hält, entferne ihn --
    dann fällt diese Prüfung mit ihm, und das ist richtig so.
    """
    coordinator = _coordinator()
    for addr in (HEATER, BOILER):
        coordinator.report("Identify", "Name", f"Gerät {addr:04X}", addr)
        coordinator.report("EnergySrc", "DieselLevel", 1, addr)
        coordinator.report("EnergySrc", "ElectricLevel", 0, addr)

    made: list = []
    ENTITY.async_add_when_all_reported(
        coordinator,
        made.extend,
        BOILER,
        {("EnergySrc", "DieselLevel"), ("EnergySrc", "ElectricLevel")},
        lambda addr: f"entity@{addr:#06x}",
    )

    assert made == [f"entity@{BOILER:#06x}"], made


def test_without_an_address_the_first_complete_device_wins() -> None:
    """Der Weg, den ``select.py`` wirklich geht: ``None`` nimmt das erste.

    Gegenprobe zur Prüfung darüber -- der Adressvergleich darf nicht so
    scharf werden, dass ohne Adresse gar nichts mehr entsteht.
    """
    coordinator = _coordinator()
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)

    made: list = []
    ENTITY.async_add_when_all_reported(
        coordinator,
        made.extend,
        None,
        {("EnergySrc", "DieselLevel"), ("EnergySrc", "ElectricLevel")},
        lambda addr: f"entity@{addr:#06x}",
    )

    assert made == [f"entity@{HEATER:#06x}"], made


def _main() -> None:
    stubs.run_tests(globals(), "Entity base")


if __name__ == "__main__":
    _main()
