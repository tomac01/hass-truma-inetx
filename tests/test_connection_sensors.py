#!/usr/bin/env python3
"""Prüft, dass Panel-Link und Proxy-Registrierung getrennt gemeldet werden.

Warum das hier steht: ``bus.connected`` bleibt im Poll-Betrieb zwischen
zwei Polls absichtlich ``True`` — sonst würde jede Entität im
Fünf-Minuten-Takt kurz unavailable. Damit taugt es aber nicht als Anzeige
dafür, ob gerade ein Link offen ist, und erst recht nicht, um "Proxy weg"
von "Panel schweigt" zu unterscheiden.

Was der Test festnagelt:

1. die Plattform legt beide Sensoren unbedingt an,
2. ``bus.connected = True`` macht den Panel-Link-Sensor nicht an,
3. beide Sensoren bleiben verfügbar, während der Link unten ist,
4. unbekannte Proxy-Registrierung ergibt ``None``, nicht ``False``.

Run: ``python3 tests/test_connection_sensors.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
BINARY = stubs.load("binary_sensor")


class _Coordinator(stubs.FakeCoordinator):
    """FakeCoordinator plus die beiden neuen Zustände."""

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self.panel_link_connected = False
        self.proxy_available = None


def _by_class(entities, name: str):
    for entity in entities:
        if type(entity).__name__ == name:
            return entity
    raise AssertionError(f"keine Entität {name} in {[type(e).__name__ for e in entities]}")


def test_both_sensors_exist_from_setup() -> None:
    """Sie hängen an keinem Busparameter und dürfen auf keinen warten."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(BINARY, coordinator)

    assert _by_class(made, "TrumaConnectionSensor")
    assert _by_class(made, "TrumaProxySensor")


def test_panel_link_is_independent_of_bus_connected() -> None:
    """Zwischen zwei Polls sind Werte da, aber kein Link offen."""
    bus = BUS.Bus()
    coordinator = _Coordinator(bus)
    made = stubs.setup_platform(BINARY, coordinator)
    link = _by_class(made, "TrumaConnectionSensor")

    bus.connected = True
    coordinator.panel_link_connected = False
    assert link.is_on is False, "gecachte Werte wurden als offener Link gemeldet"

    coordinator.panel_link_connected = True
    assert link.is_on is True


def test_both_stay_available_while_the_link_is_down() -> None:
    """Ein Sensor über den Link darf nicht vom Link abhängen."""
    bus = BUS.Bus()
    bus.connected = False
    coordinator = _Coordinator(bus)
    made = stubs.setup_platform(BINARY, coordinator)

    assert _by_class(made, "TrumaConnectionSensor").available
    assert _by_class(made, "TrumaProxySensor").available


def test_unknown_proxy_registration_is_none() -> None:
    """Solange keine Route lief, wird nichts behauptet."""
    coordinator = _Coordinator(BUS.Bus())
    made = stubs.setup_platform(BINARY, coordinator)
    proxy = _by_class(made, "TrumaProxySensor")

    assert proxy.is_on is None

    coordinator.proxy_available = False
    assert proxy.is_on is False


def _main() -> None:
    stubs.run_tests(globals(), "Connection sensors")


if __name__ == "__main__":
    _main()
