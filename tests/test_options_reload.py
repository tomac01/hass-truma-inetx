#!/usr/bin/env python3
"""Prüft, dass *nur* eine Optionsänderung einen Reload des Entries auslöst.

Warum es den Listener gibt: Ohne ihn übernimmt eine laufende BLE-Session
eine geänderte ``poll_interval_seconds`` nicht. Im Dauerverbindungs-Modus
hängt die Schleife in ``while client.connected`` und liest die Option nie
wieder — die Umstellung bleibt wirkungslos, bis jemand von Hand neu lädt
oder Home Assistant neu startet.

Warum er vergleichen muss: Home Assistant ruft Update-Listener bei *jeder*
Änderung des Config-Entries auf, also auch, wenn die Bluetooth-Discovery
bloß ``entry.data[CONF_ADDRESS]`` auf die neue RPA nachzieht. Ein Reload
darauf hebelt ``reload_on_update=False`` aus dem Discovery-Pfad in
``config_flow.py`` (Zeilen 138–141) aus. Gemessen auf dem Fahrzeug
(REV-007): ein vollständiger Reload rund alle 15 Minuten, jeder mit
Entitäts-Ausfall und Sitzungsabbruch.

Was der Test festnagelt:

1. ``async_setup_entry`` meldet den Listener wirklich an — geprüft über den
   echten Aufruf von ``async_setup_entry``, nicht über die Hilfsfunktion
   allein. Fiele der Einbau aus dem Setup heraus, bliebe die Verdrahtung
   sonst unbemerkt kaputt, obwohl beide Hilfsfunktionen für sich weiter
   funktionieren,
2. dabei wird eine echte Kopie der Optionen am Coordinator hinterlegt,
3. unveränderte Optionen lösen **keinen** Reload aus,
4. eine geänderte ``poll_interval_seconds`` löst **genau einen** aus — auch
   wenn der Listener danach noch einmal feuert,
5. eine Adressaktualisierung wie aus der Discovery löst **keinen** aus,
6. der Listener wird fürs Entladen vorgemerkt, damit ein Reload nicht bei
   jedem Durchlauf einen weiteren Listener anhäuft,
7. ein Listener, der erst läuft, wenn der Entry schon entladen wird
   (``runtime_data`` fehlt), kehrt still zurück, statt mit einem
   ``AttributeError`` in einem Hintergrund-Task zu enden.
8. läuft gerade eine BLE-Sitzung, lädt der Listener **nicht** selbst neu,
   sondern übergibt den Wunsch genau einmal dem Coordinator, der erst nach
   dem Ende der Sitzung neu lädt (REV-007).

Run: ``python3 tests/test_options_reload.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MappingProxyType

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

    def __init__(self, options: dict | None = None) -> None:
        self.listeners: list = []
        self.unloads: list = []
        self.data = {"address": "aa:bb:cc:dd:ee:ff"}
        # Wie im echten Config-Entry ein unveränderliches Mapping. Das fängt
        # nicht die fehlende ``dict()``-Kopie ab — ein ``mappingproxy``
        # vergleicht sich gleich zu einem ``dict`` mit denselben Einträgen;
        # dafür gibt es die Typprüfung in
        # ``test_setup_takes_a_copy_of_the_options``. Es fängt ab, dass der
        # Produktionscode die Optionen des Entries an Ort und Stelle ändert.
        self.options = MappingProxyType(dict(options or {}))
        self.runtime_data = None

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None

    def async_on_unload(self, unsub) -> None:
        self.unloads.append(unsub)

    def set_options(self, options: dict) -> None:
        """Eine Optionsänderung so nachstellen, wie Home Assistant sie macht.

        ``async_update_entry`` hängt ein *neues* Mapping ein, statt das alte
        zu verändern — sonst wäre jeder Vergleich mit einer Kopie sinnlos.
        """
        self.options = MappingProxyType(dict(options))


class _Coordinator:
    """Setzt an die Stelle des echten Coordinators, ohne BLE anzufassen."""

    def __init__(self, *_a, **_kw) -> None:
        # Wie im echten Coordinator: das Feld existiert ab dem ersten Moment,
        # gefüllt wird es erst beim Anmelden des Listeners.
        self.known_options: dict = {}
        # Ohne laufende Sitzung lädt der Listener sofort neu -- so wie vor dem
        # Aufschieben. Die Tests, die eine laufende Sitzung brauchen, setzen
        # das Feld selbst.
        self.session_running = False
        self.reload_requests = 0

    def async_request_reload(self) -> None:
        self.reload_requests += 1

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
    des Listeners samt Optionskopie — bleibt der echte Code.
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


def _started(options: dict) -> tuple[_Hass, _Entry]:
    """Einen aufgesetzten Entry samt angemeldetem Listener herstellen."""
    hass = _Hass()
    entry = _Entry(options)
    asyncio.run(_run_setup(hass, entry))
    assert len(entry.listeners) == 1, entry.listeners
    return hass, entry


def _fire(hass: _Hass, entry: _Entry) -> None:
    """Den angemeldeten Listener so aufrufen, wie Home Assistant es tut."""
    asyncio.run(entry.listeners[0](hass, entry))


def test_setup_takes_a_copy_of_the_options() -> None:
    """Das Setup legt die Vergleichsgrundlage am Coordinator ab.

    Ohne sie hat der Listener nichts, woran er eine echte Änderung von einer
    bloßen Adressaktualisierung unterscheiden könnte. Und es muss eine echte
    Kopie sein: Wer sich das Mapping des Entries bloß merkt, vergleicht es
    später mit sich selbst, sobald Home Assistant es doch einmal an Ort und
    Stelle ändert.
    """
    _hass, entry = _started({"poll_interval_seconds": 300})

    assert entry.runtime_data.known_options == {"poll_interval_seconds": 300}, (
        entry.runtime_data.known_options
    )
    assert type(entry.runtime_data.known_options) is dict, (
        type(entry.runtime_data.known_options)
    )


def test_unchanged_options_do_not_reload() -> None:
    """Feuert der Listener ohne Optionsänderung, passiert nichts.

    Das ist der Normalfall auf dem Fahrzeug: die Discovery trägt die neue RPA
    nach, Home Assistant ruft daraufhin jeden Update-Listener auf.
    """
    hass, entry = _started({"poll_interval_seconds": 300})

    _fire(hass, entry)

    assert hass.config_entries.reloaded == [], hass.config_entries.reloaded


def test_a_changed_poll_interval_reloads_exactly_once() -> None:
    """Der eigentliche Zweck bleibt: eine neue Abtastrate wirkt sofort.

    Der zweite Aufruf gehört dazu: Nach dem Reload darf derselbe Listener
    nicht noch einmal nachlegen, sonst tauscht man einen Reload-Sturm gegen
    einen anderen.
    """
    hass, entry = _started({"poll_interval_seconds": 300})

    entry.set_options({"poll_interval_seconds": 600})
    _fire(hass, entry)
    _fire(hass, entry)

    assert hass.config_entries.reloaded == ["abc123"], hass.config_entries.reloaded
    assert entry.runtime_data.reload_requests == 0, (
        "ohne laufende Sitzung gibt es nichts abzuwarten"
    )


def test_a_change_during_a_session_waits_for_the_session() -> None:
    """Läuft die Sitzung, lädt der Listener nicht selbst neu.

    Ein Reload mitten in einer Sitzung ließ das Panel zweimal keine Verbindung
    mehr annehmen, bis es stromlos war (REV-007: 25.09. und 30.09.2026, beim
    zweiten Mal ausgelöst durch genau diese Option). Der Listener übergibt den
    Wunsch deshalb dem Coordinator, der den Reload erst nach dem Ende der
    Sitzung auslöst -- und zwar genau einmal, auch wenn er zweimal feuert.
    """
    hass, entry = _started({"poll_interval_seconds": 300})
    entry.runtime_data.session_running = True

    entry.set_options({"poll_interval_seconds": 600})
    _fire(hass, entry)
    _fire(hass, entry)

    assert hass.config_entries.reloaded == [], (
        f"Reload mitten in der Sitzung: {hass.config_entries.reloaded}"
    )
    assert entry.runtime_data.reload_requests == 1, entry.runtime_data.reload_requests
    assert entry.runtime_data.known_options == {"poll_interval_seconds": 600}, (
        entry.runtime_data.known_options
    )


def test_an_address_update_from_discovery_does_not_reload() -> None:
    """Die RPA-Rotation ist keine Optionsänderung.

    ``_abort_if_unique_id_configured(updates={CONF_ADDRESS: ...},
    reload_on_update=False)`` in ``config_flow.py``:138–141 schreibt genau das
    in den Entry und löst damit die Update-Listener aus.
    """
    hass, entry = _started({"poll_interval_seconds": 300})

    entry.data["address"] = "11:22:33:44:55:66"
    _fire(hass, entry)

    assert hass.config_entries.reloaded == [], hass.config_entries.reloaded


def test_a_listener_that_runs_during_a_reload_returns_quietly() -> None:
    """Zwei dicht aufeinander folgende Änderungen: der zweite Task kommt zu spät.

    Home Assistant startet den Listener als eigenen Task. Läuft er erst,
    während der Reload des ersten Aufrufs den Eintrag schon entladen hat,
    ist ``runtime_data`` weg -- und ein Zugriff darauf wäre ein
    ``AttributeError`` in einem Hintergrund-Task. Ein Reload ist dann ohnehin
    unterwegs, es gibt nichts mehr zu tun.
    """
    hass, entry = _started({"poll_interval_seconds": 300})

    entry.set_options({"poll_interval_seconds": 600})
    # Beide Formen, in denen "weg" vorkommt: gelöscht (so entlädt Home
    # Assistant) und auf None gesetzt.
    del entry.runtime_data
    _fire(hass, entry)
    entry.runtime_data = None
    _fire(hass, entry)

    assert hass.config_entries.reloaded == [], hass.config_entries.reloaded


def test_setup_wires_an_option_change_to_a_reload() -> None:
    """Das Setup meldet den Listener an, und der lädt den Entry neu.

    Geprüft wird die Verdrahtung, nicht die Hilfsfunktion: Verschwindet der
    Aufruf aus ``async_setup_entry``, bleibt hier nichts angemeldet und eine
    Optionsänderung erreicht die laufende Session nie.
    """
    hass, entry = _started({"poll_interval_seconds": 300})

    entry.set_options({"poll_interval_seconds": 0})
    _fire(hass, entry)

    assert hass.config_entries.reloaded == ["abc123"], hass.config_entries.reloaded
    # Ohne Abmeldung käme bei jedem Reload ein weiterer Listener dazu.
    assert len(entry.unloads) == 1, entry.unloads


def test_setup_registers_the_listener_for_unload() -> None:
    """Der Listener wird registriert und beim Entladen wieder abgemeldet."""
    entry = _Entry()
    entry.runtime_data = _Coordinator()

    ENTRY._async_register_update_listener(entry)

    assert entry.listeners == [ENTRY._async_update_listener], entry.listeners
    assert len(entry.unloads) == 1, entry.unloads


def _main() -> None:
    stubs.run_tests(globals(), "Options reload")


if __name__ == "__main__":
    _main()
