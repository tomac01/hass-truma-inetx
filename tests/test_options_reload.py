#!/usr/bin/env python3
"""Prüft, dass eine Optionsänderung einen Reload des Config-Entries auslöst.

Warum das hier steht: Ohne Update-Listener übernimmt eine laufende
BLE-Session eine geänderte ``poll_interval_seconds`` nicht. Im
Dauerverbindungs-Modus hängt die Schleife in ``while client.connected``
und liest die Option nie wieder — die Umstellung bleibt wirkungslos, bis
jemand von Hand neu lädt oder Home Assistant neu startet.

Was der Test festnagelt:

1. ``async_setup_entry`` registriert einen Update-Listener,
2. der Listener ruft ``hass.config_entries.async_reload`` mit der
   Entry-ID auf.

Run: ``python3 tests/test_options_reload.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

stubs.install_homeassistant()
stubs.mod("homeassistant.components.bluetooth",
          async_ble_device_from_address=lambda *a, **kw: object())
stubs.mod("homeassistant.components.frontend", add_extra_js_url=lambda *a: None)
stubs.mod("homeassistant.components.http", StaticPathConfig=object)
stubs.stub_transport()
sys.modules["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          StartupFailed=RuntimeError, handle_frame=None)
stubs.mod("truma_pkg.truma.protocol", build_write_frame=None)
stubs.load("bus")
stubs.load("const")
stubs.load("coordinator")
ENTRY = stubs.load("__init__")


class _ConfigEntries:
    """Nimmt Reload-Aufrufe entgegen."""

    def __init__(self) -> None:
        self.reloaded: list[str] = []

    async def async_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)


class _Hass:
    def __init__(self) -> None:
        self.config_entries = _ConfigEntries()


class _Entry:
    entry_id = "abc123"

    def __init__(self) -> None:
        self.listeners: list = []
        self.unloads: list = []

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None

    def async_on_unload(self, unsub) -> None:
        self.unloads.append(unsub)


def test_update_listener_reloads_the_entry() -> None:
    """Der Listener lädt genau den Entry neu, zu dem er gehört."""
    hass = _Hass()
    entry = _Entry()

    asyncio.run(ENTRY._async_update_listener(hass, entry))

    assert hass.config_entries.reloaded == ["abc123"], hass.config_entries.reloaded


def test_setup_registers_the_listener_for_unload() -> None:
    """Der Listener wird registriert und beim Entladen wieder abgemeldet."""
    entry = _Entry()

    ENTRY._async_register_update_listener(entry)

    assert entry.listeners == [ENTRY._async_update_listener], entry.listeners
    assert len(entry.unloads) == 1, entry.unloads


def _main() -> None:
    stubs.run_tests(globals(), "Options reload")


if __name__ == "__main__":
    _main()
