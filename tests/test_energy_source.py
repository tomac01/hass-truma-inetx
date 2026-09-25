#!/usr/bin/env python3
"""Prüft die kombinierte Energiequellen-Auswahl.

Warum das hier steht: Diesel-Schalter und Elektro-Auswahl getrennt
bedienbar heißt, man kann beide auf null stellen -- die Heizung steht dann
ohne Energiequelle da und tut nichts, ohne dass irgendwo ein Fehler
erschiene. Eine Auswahl mit drei Zuständen kann das nicht.

Was der Test festnagelt:

1. die Auswahl entsteht nur, wenn BEIDE Hardwareparameter gemeldet wurden --
   in beiden Reihenfolgen geprüft, sonst bliebe ein "required", das nur einen
   der beiden Pegel fordert, unbemerkt,
2. Elektro und Hybrid werden immer bei 900 W betreten,
3. beide Parameter gehen als GENAU EINE Transaktion raus, mit action und
   target, und kein Einzelwrite daneben,
4. der aktuelle Zustand wird aus beiden Pegeln abgeleitet,
5. während der Umstellung meldet sie "changing", und die Optionsliste ist
   sonst genau die der drei Quellen,
6. eine unbekannte Option wird abgewiesen, statt mit KeyError zu platzen,
7. die Entität entsteht genau einmal, egal wie viele Updates folgen
   (zweimal hieße doppelte unique_id),
8. sie wartet auf den Namen ihres Geräts (#23),
9. der Upstream-Diesel-Schalter bleibt erhalten -- er ist die einzige
   Möglichkeit, den Brenner einzeln zu schalten.

Run: ``python3 tests/test_energy_source.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

HEATER = 0x0201

# Die drei Quellen, in der Reihenfolge, in der Home Assistant sie anbietet.
# Bewusst als Literale und nicht aus dem Modul geholt: das sind die
# Zustandsstrings, auf die Automationen matchen, also Vertrag und nicht
# Implementierungsdetail.
SOURCES = ["diesel", "electric", "hybrid"]
CHANGING = "changing"

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SELECT = stubs.load("select")
SWITCH = stubs.load("switch")


class _Coordinator(stubs.FakeCoordinator):
    def __init__(self, bus) -> None:
        super().__init__(bus)
        self.transactions: list = []
        self.energy_source_changing = False

    async def async_write_many(self, commands, *, action=None, target=None) -> None:
        for addr, topic, param, value in commands:
            ok, msg = self.data.validate_write(addr, topic, param, value)
            if not ok:
                raise RuntimeError(msg)
        self.transactions.append((commands, action, target))


def _by_class(entities, name: str):
    for entity in entities:
        if type(entity).__name__ == name:
            return entity
    raise AssertionError(f"{name} fehlt in {[type(e).__name__ for e in entities]}")


def _count(entities, name: str) -> int:
    return sum(1 for entity in entities if type(entity).__name__ == name)


def _both_reported(coordinator) -> None:
    coordinator.describe("EnergySrc", "DieselLevel", HEATER, perm=1, v=0)
    coordinator.describe("EnergySrc", "ElectricLevel", HEATER, perm=1, v=0)


def _selected(coordinator, made, option: str):
    """Eine Option wählen und die eine Transaktion zurückgeben, die entstand."""
    before = len(coordinator.transactions)
    asyncio.run(_by_class(made, "TrumaEnergySourceSelect").async_select_option(option))
    new = coordinator.transactions[before:]
    assert len(new) == 1, f"{option}: {len(new)} Transaktionen statt einer"
    # Ein Pegel, der am async_write_many vorbei einzeln geschrieben wird, ist
    # keine Transaktion mehr -- FakeCoordinator.async_write schriebe hierhin.
    assert coordinator.writes == [], f"{option}: Einzelwrite {coordinator.writes}"
    return new[0]


def test_it_waits_for_both_hardware_parameters() -> None:
    """Ein Fahrzeug ohne Elektroelement bekommt keine Hybridauswahl."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    names = [type(e).__name__ for e in made]
    assert "TrumaEnergySourceSelect" not in names, names

    coordinator.describe("EnergySrc", "DieselLevel", HEATER, perm=1, v=1)
    names = [type(e).__name__ for e in made]
    assert "TrumaEnergySourceSelect" not in names, "ein Pegel allein genügt nicht"

    coordinator.describe("EnergySrc", "ElectricLevel", HEATER, perm=1, v=0)
    assert _by_class(made, "TrumaEnergySourceSelect")


def test_the_electric_level_alone_does_not_do_either() -> None:
    """Die Gegenprobe: auch der Elektropegel allein ist keine Auswahl.

    Ohne sie überlebte ein ``required``, das nur ``ElectricLevel`` fordert,
    den Test oben -- und ein Fahrzeug ohne Dieselbrenner bekäme eine
    Hybridauswahl, die es nicht bedienen kann.
    """
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)

    coordinator.describe("EnergySrc", "ElectricLevel", HEATER, perm=1, v=0)
    names = [type(e).__name__ for e in made]
    assert "TrumaEnergySourceSelect" not in names, "ein Pegel allein genügt nicht"

    coordinator.describe("EnergySrc", "DieselLevel", HEATER, perm=1, v=0)
    assert _by_class(made, "TrumaEnergySourceSelect")


def test_electric_and_hybrid_enter_at_900w() -> None:
    """1800 W wirft an schwachen Landanschlüssen den Automaten."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)

    commands, action, target = _selected(coordinator, made, "electric")
    assert commands == [
        (HEATER, "EnergySrc", "DieselLevel", 0),
        (HEATER, "EnergySrc", "ElectricLevel", 1),
    ], commands
    assert action == "energy_source"
    assert target == "electric"

    commands, action, target = _selected(coordinator, made, "hybrid")
    assert commands == [
        (HEATER, "EnergySrc", "DieselLevel", 1),
        (HEATER, "EnergySrc", "ElectricLevel", 1),
    ], commands
    assert action == "energy_source"
    assert target == "hybrid"


def test_diesel_switches_the_electric_element_off() -> None:
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)

    commands, action, target = _selected(coordinator, made, "diesel")
    assert commands == [
        (HEATER, "EnergySrc", "DieselLevel", 1),
        (HEATER, "EnergySrc", "ElectricLevel", 0),
    ], commands
    assert action == "energy_source"
    assert target == "diesel"


def test_an_unknown_option_is_refused() -> None:
    """Kein KeyError aus dem Inneren, und kein halb geschriebener Zustand."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)
    select = _by_class(made, "TrumaEnergySourceSelect")

    try:
        asyncio.run(select.async_select_option("gas"))
    except Exception as exc:  # HomeAssistantError ist hier RuntimeError
        assert isinstance(exc, RuntimeError), type(exc)
    else:
        raise AssertionError("eine erfundene Quelle wurde angenommen")
    assert coordinator.transactions == []


def test_current_option_is_derived_from_both_levels() -> None:
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)
    select = _by_class(made, "TrumaEnergySourceSelect")

    for diesel, electric, expected in (
        (1, 0, "diesel"),
        (0, 1, "electric"),
        (0, 2, "electric"),
        (1, 1, "hybrid"),
        (1, 2, "hybrid"),
        (0, 0, None),
    ):
        coordinator.report("EnergySrc", "DieselLevel", diesel, HEATER)
        coordinator.report("EnergySrc", "ElectricLevel", electric, HEATER)
        assert select.current_option == expected, (diesel, electric)


def test_it_says_changing_while_the_transaction_runs() -> None:
    """Zwei Writes sind ein Moment, in dem der Zustand nicht stimmt."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)
    select = _by_class(made, "TrumaEnergySourceSelect")
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)

    coordinator.energy_source_changing = True
    assert select.current_option == CHANGING
    # Home Assistant weist jeden Zustand ab, der nicht in options steht --
    # die Liste muss den Zwischenzustand also wirklich enthalten.
    assert select.options == [*SOURCES, CHANGING], select.options

    coordinator.energy_source_changing = False
    assert select.current_option == "diesel"
    assert select.options == SOURCES, select.options


def test_the_select_is_created_exactly_once() -> None:
    """Eine zweite Entität hieße zweimal dieselbe unique_id."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)
    for value in (1, 0, 1):
        coordinator.report("EnergySrc", "DieselLevel", value, HEATER)
        coordinator.describe("EnergySrc", "ElectricLevel", HEATER, perm=1, v=value)

    assert _count(made, "TrumaEnergySourceSelect") == 1, [
        type(e).__name__ for e in made
    ]
    select = _by_class(made, "TrumaEnergySourceSelect")
    assert select.unique_id == f"{coordinator.unique_id}_0201_energy_source", (
        select.unique_id
    )


def test_it_waits_for_the_device_to_be_named() -> None:
    """Sonst brennt "bus_device_0x0201" dauerhaft in die entity_id (#23)."""
    coordinator = _Coordinator(BUS.Bus())
    coordinator.data.discovered = False
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)
    names = [type(e).__name__ for e in made]
    assert "TrumaEnergySourceSelect" not in names, "vor dem Namen gebaut"

    coordinator.data.discovered = True
    coordinator._notify()
    assert _by_class(made, "TrumaEnergySourceSelect")


def test_the_upstream_diesel_switch_stays() -> None:
    """Die einzige Möglichkeit, den Brenner einzeln zu schalten.

    Der Fork hatte ihn entfernt; hier bleibt er bewusst stehen. Wer ihn nicht
    will, deaktiviert ihn in Home Assistant.
    """
    coordinator = _Coordinator(BUS.Bus())
    switches = stubs.setup_platform(SWITCH, coordinator)
    _both_reported(coordinator)

    diesel = [
        entity
        for entity in switches
        if entity.unique_id.endswith("_EnergySrc.DieselLevel_switch")
    ]
    assert len(diesel) == 1, [e.unique_id for e in switches]


def _main() -> None:
    stubs.run_tests(globals(), "Energy source")


if __name__ == "__main__":
    _main()
