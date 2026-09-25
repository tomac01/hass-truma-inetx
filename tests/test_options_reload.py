#!/usr/bin/env python3
"""Prüft, dass eine Optionsänderung einen Reload des Config-Entries auslöst.

Warum das hier steht: Ohne Update-Listener übernimmt eine laufende
BLE-Session eine geänderte ``poll_interval_seconds`` nicht. Im
Dauerverbindungs-Modus hängt die Schleife in ``while client.connected``
und liest die Option nie wieder — die Umstellung bleibt wirkungslos, bis
jemand von Hand neu lädt oder Home Assistant neu startet.

Was der Test festnagelt:

1. ``async_setup_entry`` meldet den Listener wirklich an — geprüft über den
   echten Aufruf von ``async_setup_entry``, nicht über die Hilfsfunktion
   allein. Fiele der Einbau aus dem Setup heraus, bliebe die Verdrahtung
   sonst unbemerkt kaputt, obwohl beide Hilfsfunktionen für sich weiter
   funktionieren,
2. der so angemeldete Listener ruft ``hass.config_entries.async_reload`` mit
   der Entry-ID auf,
3. der Listener wird fürs Entladen vorgemerkt, damit ein Reload nicht bei
   jedem Durchlauf einen weiteren Listener anhäuft.

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
stubs.stub_protocol()
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
        # Das Setup sucht hier die übergebene Pairing-Verbindung; leer heißt
        # "es gab keine", und genau das ist hier der Normalfall.
        self.data: dict = {}


class _Entry:
    entry_id = "abc123"

    def __init__(self) -> None:
        self.listeners: list = []
        self.unloads: list = []
        self.data = {"address": "aa:bb:cc:dd:ee:ff"}
        self.runtime_data = None

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None

    def async_on_unload(self, unsub) -> None:
        self.unloads.append(unsub)


class _Coordinator:
    """Setzt an die Stelle des echten Coordinators, ohne BLE anzufassen."""

    def __init__(self, *_a, **_kw) -> None:
        pass

    async def async_config_entry_first_refresh(self) -> None:
        pass

    async def async_start(self) -> None:
        pass

    async def async_stop(self) -> None:
        pass


async def _noop(*_a, **_kw) -> None:
    pass


async def _run_setup(hass: _Hass, entry: _Entry) -> None:
    """``async_setup_entry`` fahren, ohne BLE, Karte und Plattformen.

    Ersetzt wird nur, was ohne Home Assistant nicht laufen kann. Der Pfad
    zwischen Coordinator-Start und Plattform-Forward — und damit der Einbau
    des Listeners — bleibt der echte Code.
    """
    original = (
        ENTRY.TrumaCoordinator,
        ENTRY._async_register_card,
        ENTRY._async_finish_setup,
    )
    ENTRY.TrumaCoordinator = _Coordinator
    ENTRY._async_register_card = _noop
    ENTRY._async_finish_setup = _noop
    try:
        assert await ENTRY.async_setup_entry(hass, entry) is True
    finally:
        (
            ENTRY.TrumaCoordinator,
            ENTRY._async_register_card,
            ENTRY._async_finish_setup,
        ) = original


def test_setup_wires_an_option_change_to_a_reload() -> None:
    """Das Setup meldet den Listener an, und der lädt den Entry neu.

    Geprüft wird die Verdrahtung, nicht die Hilfsfunktion: Verschwindet der
    Aufruf aus ``async_setup_entry``, bleibt hier nichts angemeldet und eine
    Optionsänderung erreicht die laufende Session nie.
    """
    hass = _Hass()
    entry = _Entry()

    asyncio.run(_run_setup(hass, entry))

    assert len(entry.listeners) == 1, entry.listeners
    # Nicht bloß "irgendwas angemeldet": der angemeldete Listener selbst muss
    # den Reload auslösen.
    asyncio.run(entry.listeners[0](hass, entry))
    assert hass.config_entries.reloaded == ["abc123"], hass.config_entries.reloaded
    # Ohne Abmeldung käme bei jedem Reload ein weiterer Listener dazu.
    assert len(entry.unloads) == 1, entry.unloads


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
