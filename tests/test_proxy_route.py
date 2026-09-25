#!/usr/bin/env python3
"""Prüft die Identifikation des konkret genutzten Bluetooth-Proxys.

Warum das hier steht: Home Assistant kann mehrere Proxys haben. Welcher
davon das Truma-Panel erreicht, weiß nur die Route, über die tatsächlich
eine Verbindung zustande kam. Geraten wird hier nichts — und ein lokaler
Adapter zählt ausdrücklich nicht als Proxy.

Was der Test festnagelt:

1. nur ein entfernter Scanner liefert eine Quelle, ein lokaler nicht,
2. der Tracker meldet "unbekannt", solange keine Route gelaufen ist,
3. nur Registrierungsereignisse des gemerkten Scanners lösen ein Update aus.

Run: ``python3 tests/test_proxy_route.py``
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402


class _RemoteScanner:
    def __init__(self, source: str) -> None:
        self.source = source


class _LocalScanner:
    def __init__(self, source: str) -> None:
        self.source = source


SCANNERS: dict[str, list] = {}


class _Manager:
    """Steht für habluetooth's Manager."""

    def __init__(self) -> None:
        self.sources: dict[str, object] = {}
        self.callback = None

    def async_scanner_by_source(self, source: str):
        return self.sources.get(source)

    def async_register_scanner_registration_callback(self, callback, source):
        assert source is None, source
        self.callback = callback
        return lambda: None


MANAGER = _Manager()

stubs.install_homeassistant()
# bleak liegt hier nicht vor; ``bt`` importiert nur den Typ BLEDevice daraus.
stubs.mod("bleak", __path__=[])
stubs.mod("bleak.backends", __path__=[])
stubs.mod("bleak.backends.device", BLEDevice=object)
stubs.mod("habluetooth", BaseHaRemoteScanner=_RemoteScanner,
          get_manager=lambda: MANAGER)
stubs.mod("homeassistant.components.bluetooth",
          async_scanner_devices_by_address=lambda hass, address, connectable: SCANNERS.get(address, []))
stubs.load("const")
PROXY = stubs.load("proxy")
BT = stubs.load("bt")


def test_only_a_remote_scanner_counts_as_a_proxy() -> None:
    """Ein lokaler Adapter ist keine Proxy-Route."""
    SCANNERS.clear()
    SCANNERS["AA:BB"] = [
        SimpleNamespace(scanner=_LocalScanner("local")),
        SimpleNamespace(scanner=_RemoteScanner("A0:A3:B3:90:C5:3E")),
    ]
    assert BT.async_remote_scanner_source(None, "AA:BB") == "A0:A3:B3:90:C5:3E"

    SCANNERS["CC:DD"] = [SimpleNamespace(scanner=_LocalScanner("local"))]
    assert BT.async_remote_scanner_source(None, "CC:DD") is None


def test_tracker_reports_unknown_until_a_route_has_run() -> None:
    """Vor der ersten Route ist der Zustand weder an noch aus."""
    changes: list = []
    tracker = PROXY.TrumaProxyTracker(lambda: changes.append(True))

    assert tracker.available is None, "ohne Route darf nichts behauptet werden"

    tracker.remember_source("A0:A3:B3:90:C5:3E")
    assert tracker.available is False
    assert len(changes) == 1

    MANAGER.sources["A0:A3:B3:90:C5:3E"] = object()
    assert tracker.available is True


def test_only_the_remembered_scanner_notifies() -> None:
    """Ein fremder Proxy, der kommt oder geht, geht uns nichts an."""
    changes: list = []
    tracker = PROXY.TrumaProxyTracker(lambda: changes.append(True))
    tracker.async_setup()
    tracker.remember_source("A0:A3:B3:90:C5:3E")
    changes.clear()

    MANAGER.callback(SimpleNamespace(scanner=_RemoteScanner("something:else")))
    assert changes == [], "fremder Scanner hat ein Update ausgelöst"

    MANAGER.callback(SimpleNamespace(scanner=_RemoteScanner("A0:A3:B3:90:C5:3E")))
    assert changes == [True]


def _main() -> None:
    stubs.run_tests(globals(), "Proxy route")


if __name__ == "__main__":
    _main()
