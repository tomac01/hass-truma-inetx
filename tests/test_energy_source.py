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
4. innerhalb dieser Transaktion wird die neue Quelle eingeschaltet, bevor die
   alte abgeschaltet wird -- nach jedem einzelnen Befehl ist mindestens eine
   Quelle an, aus jedem Ausgangszustand,
5. der aktuelle Zustand wird aus beiden Pegeln abgeleitet,
6. während der Umstellung meldet sie "changing", und die Optionsliste ist
   sonst genau die der drei Quellen,
7. eine unbekannte Option wird abgewiesen, statt mit KeyError zu platzen,
8. die Entität entsteht genau einmal, egal wie viele Updates folgen
   (zweimal hieße doppelte unique_id),
9. sie wartet auf den Namen ihres Geräts (#23),
10. der Upstream-Diesel-Schalter bleibt erhalten -- er ist die einzige
    Möglichkeit, den Brenner einzeln zu schalten,
11. sie entsteht einmal für den ganzen Bus, am ersten Gerät, das beide Pegel
    meldete -- die Eigenschaft von ``addr=None``, festgehalten, damit ein Bus
    mit zwei Publishern hier auffällt und nicht am Fahrzeug.

Run: ``python3 tests/test_energy_source.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

HEATER = 0x0201
# Das Panel wird als erstes Gerät auf dem Bus entdeckt. Es meldet ``EnergySrc``
# auf keinem belegten Fahrzeug -- in test_one_select_for_the_whole_bus ist es
# der erfundene zweite Publisher, der zeigt, was dann passierte.
PANEL = 0x0101

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
    """1800 W wirft an schwachen Landanschlüssen den Automaten.

    Die Reihenfolge steht hier mit drin, weil sie in der erwarteten Liste
    nicht zu umgehen ist; worum es bei ihr geht, prüft
    test_the_new_source_is_on_before_the_old_one_goes_off.
    """
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)

    commands, action, target = _selected(coordinator, made, "electric")
    assert commands == [
        (HEATER, "EnergySrc", "ElectricLevel", 1),
        (HEATER, "EnergySrc", "DieselLevel", 0),
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


def test_the_new_source_is_on_before_the_old_one_goes_off() -> None:
    """Zwischen den beiden Pegelbefehlen darf nie beides aus sein.

    Die beiden Writes einer Transaktion gehen nacheinander raus, und zwischen
    ihnen liegen bis zu 40 Sekunden (``_WRITE_SETTLE`` plus die Wartezeit auf
    die Bestätigung jedes einzelnen). Scheitert der zweite, bleibt stehen, was
    der erste angerichtet hat. Schaltet der erste die bisherige Quelle ab,
    steht die Heizung danach dauerhaft ohne Energiequelle da -- genau der
    Zustand, den diese Auswahl verhindern soll.

    Dass der zweite Befehl scheitert, ist nicht theoretisch: der eben
    abgeschaltete Brenner geht ins Nachlüften, und für diese Phase ist in
    ``coordinator.async_write_many`` dokumentiert, dass das Panel Frames
    quittiert, die die Heizung nicht ausführt.

    Deshalb prüft dieser Test jeden Zwischenstand und nicht nur das Ergebnis:
    nach JEDEM einzelnen Befehl muss mindestens eine Quelle an sein. Geprüft
    aus jedem Ausgangszustand, den es geben kann -- auch aus "beide aus", das
    beim Einschalten der Heizung vorkommt, und mit 1800 W als Elektropegel.
    """
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)
    _both_reported(coordinator)

    for start in ((1, 0), (0, 1), (0, 2), (1, 1), (1, 2), (0, 0)):
        for option, expected in (
            ("diesel", {"DieselLevel": 1, "ElectricLevel": 0}),
            ("electric", {"DieselLevel": 0, "ElectricLevel": 1}),
            ("hybrid", {"DieselLevel": 1, "ElectricLevel": 1}),
        ):
            coordinator.report("EnergySrc", "DieselLevel", start[0], HEATER)
            coordinator.report("EnergySrc", "ElectricLevel", start[1], HEATER)
            commands, _, _ = _selected(coordinator, made, option)

            levels = {"DieselLevel": start[0], "ElectricLevel": start[1]}
            for step, (_addr, _topic, param, value) in enumerate(commands, start=1):
                levels[param] = value
                assert any(levels.values()), (
                    f"{start} -> {option}: nach Befehl {step} ({param}={value}) "
                    f"hat die Heizung keine Energiequelle mehr"
                )

            # Und das Ziel wird trotzdem erreicht: ein Abschaltbefehl, der
            # einfach wegfiele, käme durch die Prüfung oben ebenfalls durch.
            assert levels == expected, f"{start} -> {option}: {levels}"
            assert sorted(param for _a, _t, param, _v in commands) == [
                "DieselLevel",
                "ElectricLevel",
            ], commands


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


def test_a_level_that_stops_being_a_number_makes_the_answer_unknown() -> None:
    """Ein Pegel ohne Zahl heißt "unbekannt", nicht "der andere gilt".

    Bei der Geburt der Entität sind beide Pegel Zahlen -- sie entsteht ja
    erst, wenn beide gemeldet wurden. Danach nicht mehr zwingend: meldet ein
    Frame für einen der beiden ``null``, steht nur noch die halbe Wahrheit im
    Bus. Ohne den Typtest beantwortete die Auswahl das mit dem Pegel, der
    noch eine Zahl hat -- also "diesel", obwohl über den Netzbetrieb gerade
    nichts bekannt ist. Beide Richtungen, damit keine durch die eine Hälfte
    des Tests gedeckt scheint.
    """
    for gone, kept in (("DieselLevel", "ElectricLevel"),
                       ("ElectricLevel", "DieselLevel")):
        coordinator = _Coordinator(BUS.Bus())
        made = stubs.setup_platform(SELECT, coordinator)
        _both_reported(coordinator)
        select = _by_class(made, "TrumaEnergySourceSelect")

        coordinator.report("EnergySrc", gone, 1, HEATER)
        coordinator.report("EnergySrc", kept, 1, HEATER)
        assert select.current_option == "hybrid", gone

        coordinator.report("EnergySrc", gone, None, HEATER)
        assert select.current_option is None, (
            f"{gone} ist keine Zahl mehr, die Auswahl zeigte trotzdem "
            f"{select.current_option!r}"
        )


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


def test_one_select_for_the_whole_bus() -> None:
    """Zwei Publisher, eine Auswahl -- am ersten, der beide Pegel meldete.

    Das ist keine gemessene Lage, sondern die Eigenschaft von ``addr=None``,
    hier ausgeschrieben, damit sie nicht erst am Fahrzeug auffällt: die
    Auswahl entsteht einmal für den ganzen Bus, an dem Gerät, das in der
    Einfügereihenfolge von ``devices`` zuerst beide Pegel führte -- und sie
    schreibt danach auch dorthin. Ein zweites Gerät mit denselben Pegeln
    bekäme still keine eigene.

    Belegt ist das Gegenteil: ``EnergySrc`` kommt in
    ``dumps/combi4-inetx-pro/`` allein von 0x0201, und relayed wird ans Panel
    nur ``RoomClimate`` (``bus.COMMAND_DEST``). Taucht je ein Bus auf, auf dem
    zwei Geräte beide Pegel melden, schlägt dieser Test fehl -- dann braucht
    ``select.py`` eine echte Adresse statt ``None`` und eine Auswahl pro
    Gerät.
    """
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(SELECT, coordinator)

    # Das Panel ist zuerst da und meldet -- hypothetisch -- beide Pegel.
    coordinator.describe("EnergySrc", "DieselLevel", PANEL, perm=1, v=1)
    coordinator.describe("EnergySrc", "ElectricLevel", PANEL, perm=1, v=0)
    _both_reported(coordinator)

    assert _count(made, "TrumaEnergySourceSelect") == 1, [
        e.unique_id for e in made if type(e).__name__ == "TrumaEnergySourceSelect"
    ]
    select = _by_class(made, "TrumaEnergySourceSelect")
    assert select.unique_id == f"{coordinator.unique_id}_0101_energy_source", (
        select.unique_id
    )

    # Und sie schreibt an das Gerät, an dem sie hängt: genau das ist der Preis.
    commands, _, _ = _selected(coordinator, made, "diesel")
    assert {addr for addr, *_rest in commands} == {PANEL}, commands


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
