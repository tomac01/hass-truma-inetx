#!/usr/bin/env python3
"""Was ein Sensor tut, wenn das Panel etwas schickt, das nicht hineinpasst.

Warum das hier steht: Eine ENUM-Entität darf in Home Assistant nur einen der
Zustände melden, die sie angemeldet hat — alles andere wirft *aus dem
State-Write heraus*, also einmal pro Coordinator-Aktualisierung für immer. Das
ist am Fahrzeug gemessen worden: ``BleDeviceManagement.NrFreeSlots`` erwies
sich als Liste und füllte das Log mit Tracebacks. ``sensor.py`` fängt das ab
und meldet stattdessen unknown, mit dem Rohwert in den Attributen.

Diese Abfangnetze sind selbst ungeprüft gewesen. Ein Mutationstest zeigte,
dass man jedes einzelne entfernen kann, ohne dass eine Prüfung rot wird.

Was der Test festnagelt:

1. eine Zeile mit Namen liest ``True`` **nicht** als den Zustand 1 — in Python
   ist ``True == 1``, und ohne den bool-Schutz zeigt der Sensor einen falschen
   Zustandsnamen, ohne jeden Hinweis im Log,
2. dieselbe Zeile mit einer Liste liest unknown statt zu werfen,
3. eine Struktur an einer Einzelwert-Zeile warnt **genau einmal**, nicht nie
   und nicht bei jeder Aktualisierung,
4. ein unbenannter Wert an einer ENUM-Zeile warnt ebenfalls genau einmal,
5. und jeder *neue* unbenannte Wert bekommt seine eigene Meldung.

Run: ``python3 tests/test_sensor_value_guards.py``
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# Der Dachklimaanlage aus #23 — hier nur, weil ihre Zeilen die beiden Formen
# tragen, um die es geht: eine mit Namen und eine ohne. Jede Adresse auf
# diesem Bus wird neu vergeben, wenn ihr Gerät neu gekoppelt wird.
ROOF_AC = 0x0406

stubs.install_homeassistant()
BUS = stubs.load("bus")
CONST = stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")


class _Records(logging.Handler):
    """Mitschreiben, was das Modul zu melden hätte."""

    def __init__(self) -> None:
        super().__init__()
        self.warnings: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.warnings.append(record.getMessage())


def _sensor(key: str, topic: str, param: str, seed: object):
    """Eine Entität, so gebaut wie die Plattform sie baut, plus Log-Mitschrift.

    ``seed`` ist ein Wert, den die Zeile verträgt: die Entitäten entstehen
    erst, wenn ihr Parameter einmal angekommen ist.
    """
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    coordinator.report(topic, param, seed, ROOF_AC)
    made = stubs.setup_platform(SENSOR, coordinator)
    for entity in made:
        if getattr(entity, "_attr_translation_key", None) == key:
            records = _Records()
            CONST.LOGGER.addHandler(records)
            CONST.LOGGER.setLevel(logging.DEBUG)
            return coordinator, entity, records
    raise AssertionError(f"keine Entität mit dem Übersetzungsschlüssel {key}")


def _release(records: _Records) -> None:
    CONST.LOGGER.removeHandler(records)


def test_a_labelled_row_does_not_read_a_bool_as_a_state() -> None:
    """``True == 1`` in Python — der Nachschlag würde den Zustand 1 liefern.

    Die Zeile benennt {0: off, 1: running, 2: idle}. Fällt der bool-Schutz,
    liest ``labels.get(True)`` denselben Eintrag wie ``labels.get(1)``, und der
    Sensor zeigt "running" für etwas, das das Panel gar nicht so gemeint hat —
    ohne Warnung, denn die Zeile *hatte* ja einen Namen. Das Panel typisiert
    schwach; es gibt auf diesem Bus Enums mit "Off/On/On and configured".
    """
    coordinator, status, records = _sensor("cooling_status", "AirCooling", "Active", 2)
    try:
        coordinator.report("AirCooling", "Active", True, ROOF_AC)

        assert status.native_value is None, (
            "ein bool wurde als der Zustand 1 gelesen"
        )
        # Der Rohwert bleibt sichtbar, damit der Fall auffindbar ist.
        assert status.extra_state_attributes == {"raw": True}
        assert len(records.warnings) == 1
    finally:
        _release(records)


def test_a_labelled_row_survives_a_list() -> None:
    """Eine Liste ist nicht hashbar — der Nachschlag würde werfen.

    Und zwar mitten im State-Write, einmal pro Coordinator-Aktualisierung.
    Genau der Fehlermodus, gegen den das Modul angetreten ist: Listen sind
    hier keine Erfindung, ``BleDeviceManagement.NrFreeSlots`` ist am Fahrzeug
    als eine gemessen worden.
    """
    coordinator, status, records = _sensor("cooling_status", "AirCooling", "Active", 2)
    try:
        coordinator.report("AirCooling", "Active", [1, 2], ROOF_AC)

        # Der Zugriff selbst ist die Prüfung: mutiert wirft er TypeError.
        assert status.native_value is None
        assert status.extra_state_attributes == {"raw": [1, 2]}
        assert len(records.warnings) == 1
    finally:
        _release(records)


def test_an_unpresentable_value_is_reported_once() -> None:
    """Die Entprellung ist der ganze Zweck der Abfangstelle.

    Ohne sie steht die Meldung entweder nie im Log — dann sucht niemand die
    Zeile, die den Wert nicht reduziert — oder bei jeder Aktualisierung, also
    genau die Log-Flut, die am Fahrzeug gemessen wurde.
    """
    coordinator, temp, records = _sensor("cooling_temp", "AirCooling", "Temp", 245)
    try:
        assert temp.native_value == 24.5
        coordinator.report("AirCooling", "Temp", {"kind": 12, "value": 1}, ROOF_AC)

        for _ in range(3):
            assert temp.native_value is None

        assert len(records.warnings) == 1, (
            f"genau eine Meldung erwartet, {len(records.warnings)} bekommen"
        )
        assert "cooling_temp" in records.warnings[0]
    finally:
        _release(records)


def test_an_unnamed_value_is_reported_once_per_value() -> None:
    """Dieselbe Entprellung für die Zeile *mit* Namen, und je Wert einmal.

    Der Schlüssel ist die ``repr`` des Wertes, nicht der Wert: was hier
    unbenannt auftaucht, ist nicht unbedingt etwas, das ein set annimmt — der
    Parameter, der das lehrte, war eine Liste.
    """
    coordinator, status, records = _sensor("cooling_status", "AirCooling", "Active", 2)
    try:
        for _ in range(3):
            coordinator.report("AirCooling", "Active", 3, ROOF_AC)
            assert status.native_value is None

        assert len(records.warnings) == 1, (
            f"genau eine Meldung erwartet, {len(records.warnings)} bekommen"
        )
        assert "reported 3" in records.warnings[0]

        # Ein *anderer* unbenannter Wert ist eine eigene Meldung wert.
        coordinator.report("AirCooling", "Active", 4, ROOF_AC)
        assert status.native_value is None
        assert len(records.warnings) == 2
    finally:
        _release(records)


def _main() -> None:
    stubs.run_tests(globals(), "sensor value guards")


if __name__ == "__main__":
    _main()
