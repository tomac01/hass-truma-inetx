#!/usr/bin/env python3
"""Prüft, dass der Vorgangs-Sensor wirklich am Register hängt.

Warum das hier steht: Die Vorrangregeln selbst prüft
``tests/test_operations.py`` am nackten Register. Gesehen wird davon aber
nichts, solange die Entität den Registerzustand nicht liest und ein
Registerwechsel die Entitäten nicht weckt. Genau diese Verdrahtung war
zuerst ungeprüft: ein ``native_value``, das eine Konstante liefert, ein
leeres Attribut-Dict und ein Register, das mit einem toten Callback angelegt
wird, ließen die gesamte Suite grün — festgenagelt war nur, *dass* die
Entität entsteht.

Gefahren wird hier ausdrücklich der **echte** Coordinator-Code, dasselbe
Muster wie in ``tests/test_connection_sensors.py``: der FakeCoordinator
borgt ``operation_state``, ``operation_attributes``,
``energy_source_changing`` und ``_async_operations_changed`` wörtlich von
``TrumaCoordinator`` und legt sein Register genau so an wie dessen
``__init__``. Ein Doppel, dessen ``operation_state`` nur ein Attribut wäre,
prüfte nichts als sich selbst.

Was der Test festnagelt:

1. die Plattform legt den Vorgangs-Sensor an, ohne dass ein Busparameter
   gemeldet wurde,
2. sein Wert ist der Zustand des Registers — idle, syncing, changing, error,
3. seine Attribute sind die des Registers (action, target, error),
4. jeder erreichbare Zustand steht in ``_attr_options``; ein Enum-Sensor mit
   nicht deklariertem Wert wird von Home Assistant verworfen,
5. er bleibt verfügbar, während der BLE-Link unten ist — sonst könnte er
   einen Fehler gar nicht zeigen,
6. ein Registerwechsel weckt über den Coordinator die Entitäten,
7. der Konstruktor hängt das Register an genau diesen Weck-Callback,
8. ``energy_source_changing`` ist die Antwort des Registers und keine eigene.

Run: ``python3 tests/test_operation_sensor.py``
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

stubs.install_homeassistant()
stubs.stub_transport()
# Die Sitzungssequenz und die Frame-Builder brauchen cbor2, und hier wird
# kein Frame geschickt: beide werden gestubbt, damit der echte Coordinator
# ohne Drittbibliothek importierbar bleibt.
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          StartupFailed=RuntimeError, handle_frame=None)
stubs.stub_protocol()
BUS = stubs.load("bus")
stubs.load("const")
OPS = stubs.load("operations")
# Der echte Coordinator, nicht das Platzhalter-Modul: der Sensor unten liest
# seine echten Properties.
COORD = stubs.load("coordinator")
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")


class _Coordinator(stubs.FakeCoordinator):
    """FakeCoordinator plus die **echten** Vorgangs-Member aus Task 6.

    Die vier Member sind wörtlich die des ``TrumaCoordinator``, und das
    Register wird angelegt wie in dessen ``__init__`` — mit dem Callback, der
    die Entitäten weckt. Damit prüft jede Zeile unten das ausgelieferte
    Verhalten und nicht ein Doppel davon.
    """

    operation_state = COORD.TrumaCoordinator.operation_state
    operation_attributes = COORD.TrumaCoordinator.operation_attributes
    energy_source_changing = COORD.TrumaCoordinator.energy_source_changing
    _async_operations_changed = COORD.TrumaCoordinator._async_operations_changed

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self._bus = bus
        self.hass = SimpleNamespace()
        self.pushes = 0
        self._operations = OPS.OperationRegistry(self._async_operations_changed)

    def async_set_updated_data(self, data) -> None:
        """Wie der echte: neue Daten setzen und die Entitäten wecken."""
        self.data = data
        self.pushes += 1
        self._notify()


def _by_class(entities, name: str):
    for entity in entities:
        if type(entity).__name__ == name:
            return entity
    raise AssertionError(
        f"keine Entität {name} in {[type(e).__name__ for e in entities]}"
    )


def _sensor(coordinator):
    return _by_class(stubs.setup_platform(SENSOR, coordinator), "TrumaOperationSensor")


def test_the_platform_creates_it_without_any_bus_parameter() -> None:
    """Er hängt an keiner gemeldeten Hardware und darf auf keine warten."""
    made = stubs.setup_platform(SENSOR, _Coordinator(BUS.Bus()))

    assert [type(entity).__name__ for entity in made] == ["TrumaOperationSensor"]


def test_the_sensor_reads_the_register() -> None:
    """idle → syncing → changing → error, am echten Register entlang."""
    coordinator = _Coordinator(BUS.Bus())
    sensor = _sensor(coordinator)
    register = coordinator._operations

    assert sensor.native_value == "idle"
    assert sensor.extra_state_attributes == {
        "action": None,
        "target": None,
        "error": None,
    }

    sync = register.begin("sync")
    assert sensor.native_value == "syncing"

    command = register.begin("water_mode", "Eco (40 °C)")
    assert sensor.native_value == "changing", "der Befehl blieb hinter dem Sync"
    assert sensor.extra_state_attributes == {
        "action": "water_mode",
        "target": "Eco (40 °C)",
        "error": None,
    }

    register.end(command, "Truma did not confirm WaterHeating.Mode=eco")
    register.end(sync)
    assert sensor.native_value == "error"
    assert sensor.extra_state_attributes == {
        "action": "water_mode",
        "target": "Eco (40 °C)",
        "error": "Truma did not confirm WaterHeating.Mode=eco",
    }


def test_every_reachable_state_is_a_declared_option() -> None:
    """Einen Enum-Wert, der nicht deklariert ist, verwirft Home Assistant."""
    coordinator = _Coordinator(BUS.Bus())
    sensor = _sensor(coordinator)
    register = coordinator._operations
    seen = {sensor.native_value}

    sync = register.begin("sync")
    seen.add(sensor.native_value)
    command = register.begin("temperature", 21)
    seen.add(sensor.native_value)
    register.end(command, "Truma did not confirm RoomClimate.TargetTemp=21")
    register.end(sync)
    seen.add(sensor.native_value)

    assert seen == {"idle", "syncing", "changing", "error"}
    assert seen <= set(sensor._attr_options), (
        f"{sorted(seen - set(sensor._attr_options))} ist erreichbar, steht aber "
        "nicht in _attr_options"
    )


def test_it_stays_available_while_the_link_is_down() -> None:
    """Der Fehler, den er melden soll, entsteht gerade dann, wenn nichts geht."""
    bus = BUS.Bus()
    bus.connected = False
    coordinator = _Coordinator(bus)
    sensor = _sensor(coordinator)
    register = coordinator._operations

    register.end(register.begin("energy_source", "hybrid"), "no route to the panel")

    assert sensor.available is True, "der Sensor verschwand mit dem Link"
    assert sensor.native_value == "error"
    assert sensor.extra_state_attributes["error"] == "no route to the panel"


def test_a_register_change_wakes_the_entities() -> None:
    """Ohne den Callback stünde im Dashboard der Zustand von vorhin."""
    coordinator = _Coordinator(BUS.Bus())
    _sensor(coordinator)

    assert coordinator.pushes == 0
    token = coordinator._operations.begin("temperature", 21)
    assert coordinator.pushes == 1, "der Beginn erreichte die Entitäten nicht"
    coordinator._operations.end(token)
    assert coordinator.pushes == 2, "das Ergebnis erreichte die Entitäten nicht"


def test_the_constructor_hangs_the_register_on_the_wake_callback() -> None:
    """Die eine Zeile, die hier keine Prüfung fahren kann — also gelesen.

    ``TrumaCoordinator.__init__`` verlangt ein halbes Home Assistant
    (Store, DataUpdateCoordinator, Config-Entry); die ganze Suite borgt
    darum seine Methoden, statt ihn zu bauen. Damit bliebe ausgerechnet die
    Zeile ungeprüft, die das Register an den Weck-Callback hängt — ein
    Register mit ``lambda: None`` bestünde jede Prüfung oben und ließe das
    Dashboard trotzdem auf dem Zustand von vorhin stehen, bis zufällig
    etwas anderes einen Push auslöst.

    Dass der Callback selbst wirkt, wird oben echt gefahren; hier wird nur
    festgenagelt, dass er auch eingehängt wird.
    """
    source = inspect.getsource(COORD.TrumaCoordinator.__init__)

    assert "OperationRegistry(self._async_operations_changed)" in source, (
        "der Konstruktor legt das Register nicht mehr mit "
        "_async_operations_changed an — ohne diesen Callback bemerkt keine "
        "Entität einen Vorgangswechsel"
    )


def test_energy_source_changing_is_the_registers_answer() -> None:
    """Task 11 gattert daran seine Transaktion — sie darf nichts erfinden."""
    coordinator = _Coordinator(BUS.Bus())
    register = coordinator._operations

    assert coordinator.energy_source_changing is False

    water = register.begin("water_mode", "off")
    assert coordinator.energy_source_changing is False, (
        "jeder laufende Befehl galt als Energiequellen-Wechsel"
    )

    energy = register.begin("energy_source", "hybrid")
    assert coordinator.energy_source_changing is True

    register.end(energy)
    assert coordinator.energy_source_changing is False
    register.end(water)


def _main() -> None:
    stubs.run_tests(globals(), "Vorgangs-Sensor")


if __name__ == "__main__":
    _main()
