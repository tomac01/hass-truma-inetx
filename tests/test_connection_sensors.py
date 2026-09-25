#!/usr/bin/env python3
"""Prüft, dass Panel-Link und Proxy-Registrierung getrennt gemeldet werden.

Warum das hier steht: ``bus.connected`` bleibt im Poll-Betrieb zwischen
zwei Polls absichtlich ``True`` — sonst würde jede Entität im
Fünf-Minuten-Takt kurz unavailable. Damit taugt es aber nicht als Anzeige
dafür, ob gerade ein Link offen ist, und erst recht nicht, um "Proxy weg"
von "Panel schweigt" zu unterscheiden.

Gefahren wird hier ausdrücklich der **echte** Coordinator-Code: die beiden
Properties, ``_set_panel_link_connected`` und ``_remember_proxy_for_address``
werden von ``TrumaCoordinator`` geborgt, und ``_disconnect_client`` wie
``_connect_and_run`` laufen im Original. Ein Doppel, dessen
``panel_link_connected`` nur ein Attribut ist, prüft nichts weiter als die
Durchreiche in der Entität — und genau die Zeilen, auf denen das Feature
steht, dürften dann ersatzlos verschwinden, ohne dass etwas rot wird.

Was der Test festnagelt:

1. die Plattform legt beide Sensoren unbedingt an,
2. ``bus.connected = True`` macht den Panel-Link-Sensor nicht an,
3. beide Sensoren bleiben verfügbar, während der Link unten ist,
4. unbekannte Proxy-Registrierung ergibt ``None``, nicht ``False``,
5. ``proxy_available`` kommt vom Tracker und behauptet nichts von sich aus,
6. ein echter Linkwechsel weckt die Entitäten, ein Nicht-Wechsel nicht,
7. ``_disconnect_client`` nimmt den Link an **beiden** Stellen zurück:
   im Frühausstieg "kein Client" und im ``finally`` nach dem Disconnect —
   auch wenn der Disconnect selbst scheitert,
8. ``_connect_and_run`` meldet den Link auf beiden Wegen (adoptierte
   Pairing-Verbindung und frischer Connect) und füttert den Proxy-Tracker
   mit der Quelle, über die die Route tatsächlich lief.

Run: ``python3 tests/test_connection_sensors.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
# Die Adresse, über die in den Connect-Prüfungen die Route läuft, und der
# entfernte Scanner, der sie geliefert hat.
ADDRESS = "AA:BB:CC:DD:EE:FF"
SOURCE = "esphome-proxy-holly"

stubs.install_homeassistant()
stubs.stub_transport()
# Die Sitzungssequenz und die Frame-Builder brauchen cbor2, und nichts hier
# schickt einen Frame: beide werden gestubbt, damit der echte Coordinator
# ohne Drittbibliothek importierbar bleibt.
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          StartupFailed=RuntimeError, handle_frame=None)
stubs.mod("truma_pkg.truma.protocol", build_write_frame=None)
BUS = stubs.load("bus")
stubs.load("const")
# Der echte Coordinator, nicht das Platzhalter-Modul: die Entitäten lesen
# unten seine echten Properties.
COORD = stubs.load("coordinator")
stubs.load("profiles")
stubs.load("entity")
BINARY = stubs.load("binary_sensor")


class _Tracker:
    """Steht für ``proxy.TrumaProxyTracker``, den test_proxy_route.py prüft.

    Hier zählt nur, was der Coordinator mit ihm macht: welche Quelle er ihm
    gibt und dass er dessen Antwort unverändert weiterreicht.
    """

    def __init__(self) -> None:
        self.available: bool | None = None
        self.sources: list[str] = []

    def remember_source(self, source: str) -> None:
        self.sources.append(source)


class _Coordinator(stubs.FakeCoordinator):
    """FakeCoordinator plus die **echten** Zustände aus Task 5.

    Die vier Member sind wörtlich die des ``TrumaCoordinator``; damit prüft
    jede Zeile unten das ausgelieferte Verhalten und nicht ein Doppel davon.
    """

    panel_link_connected = COORD.TrumaCoordinator.panel_link_connected
    proxy_available = COORD.TrumaCoordinator.proxy_available
    _set_panel_link_connected = COORD.TrumaCoordinator._set_panel_link_connected
    _remember_proxy_for_address = COORD.TrumaCoordinator._remember_proxy_for_address
    _disconnect_client = COORD.TrumaCoordinator._disconnect_client
    _connect_and_run = COORD.TrumaCoordinator._connect_and_run

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self._bus = bus
        self._panel_link_connected = False
        self._proxy_tracker = _Tracker()
        self.hass = SimpleNamespace()
        self.pushes = 0
        # Was ``_connect_and_run`` an Sitzungsbuchhaltung anfasst.
        self._client = None
        self._initial_client = None
        self._identity = {"muid": "m", "uuid": "u", "username": "n"}
        self._avoid: set[str] = set()
        self._last_addr: str | None = None
        self._last_kind: str | None = None
        self._session_ok = False
        self.started: list = []

    def async_set_updated_data(self, data) -> None:
        """Wie der echte: neue Daten setzen und die Entitäten wecken."""
        self.data = data
        self.pushes += 1
        self._notify()

    # Ab hier nur Umgebung des Connect-Pfads, nicht sein Gegenstand.
    def _on_frame(self, *_args) -> None:
        pass

    def _prefer_identity(self) -> bool:
        return False

    def _async_clear_no_route(self) -> None:
        pass

    async def _finish_startup(self, client) -> bool:
        self.started.append(client)
        return True


class _Link:
    """Eine BLE-Verbindung, die mitschreibt — und auf Wunsch beim Trennen fliegt."""

    def __init__(self, *, is_connected: bool = True, raises: bool = False) -> None:
        self.address = ADDRESS
        self.is_connected = is_connected
        self.raises = raises
        self.closed = 0
        self.adopted: list = []
        self.connected_to: list = []

    def on_data(self, _cb) -> None:
        pass

    async def adopt(self, other) -> None:
        self.adopted.append(other)

    async def connect(self, device) -> None:
        self.connected_to.append(device)

    async def disconnect(self) -> None:
        self.closed += 1
        if self.raises:
            raise RuntimeError("the adapter went away mid-teardown")


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
    assert link.is_on is False, "gecachte Werte wurden als offener Link gemeldet"

    coordinator._set_panel_link_connected(True)
    assert link.is_on is True

    bus.connected = False
    coordinator._set_panel_link_connected(False)
    assert link.is_on is False


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

    coordinator._proxy_tracker.available = False
    assert proxy.is_on is False

    coordinator._proxy_tracker.available = True
    assert proxy.is_on is True


def test_proxy_state_is_the_trackers_answer_not_our_own() -> None:
    """Der Coordinator entscheidet nichts über den Proxy, er gibt weiter.

    Eine Property, die sich ihre Antwort selbst ausdenkt (``True``, oder der
    Buszustand), meldet einen Proxy, der gar nicht mehr registriert ist —
    und macht den zweiten Sensor wertlos.
    """
    bus = BUS.Bus()
    bus.connected = True
    coordinator = _Coordinator(bus)
    coordinator._set_panel_link_connected(True)

    assert coordinator.proxy_available is None, "ohne Route wird nichts behauptet"

    for answer in (True, False):
        coordinator._proxy_tracker.available = answer
        assert coordinator.proxy_available is answer


def test_only_a_real_link_change_wakes_the_entities() -> None:
    """Sonst schreibt jeder Poll den gleichen Zustand erneut in den State."""
    coordinator = _Coordinator(BUS.Bus())

    coordinator._set_panel_link_connected(True)
    assert coordinator.pushes == 1
    coordinator._set_panel_link_connected(True)
    assert coordinator.pushes == 1, "unveränderter Zustand wurde erneut gemeldet"
    coordinator._set_panel_link_connected(False)
    assert coordinator.pushes == 2


def test_a_closed_link_is_reported_as_closed() -> None:
    """Der ``finally``-Zweig in ``_disconnect_client``.

    Ohne ihn bliebe der Panel-Link-Sensor nach dem Trennen für immer "an" —
    der Grund, aus dem es diesen Sensor überhaupt gibt.
    """
    async def run() -> None:
        coordinator = _Coordinator(BUS.Bus())
        link = _Link()
        coordinator._client = link
        coordinator._set_panel_link_connected(True)

        await coordinator._disconnect_client()

        assert link.closed == 1, "die Verbindung wurde nicht getrennt"
        assert coordinator.panel_link_connected is False, (
            "nach dem Trennen wurde weiter ein offener Link gemeldet"
        )

    asyncio.run(run())


def test_a_disconnect_that_fails_still_reports_the_link_down() -> None:
    """Auch ein gescheiterter Disconnect lässt keinen Link zurück."""
    async def run() -> None:
        coordinator = _Coordinator(BUS.Bus())
        coordinator._client = _Link(raises=True)
        coordinator._set_panel_link_connected(True)

        await coordinator._disconnect_client()

        assert coordinator.panel_link_connected is False, (
            "der Sensor hing an einem Link, den niemand mehr hält"
        )

    asyncio.run(run())


def test_a_teardown_without_a_client_clears_the_link_too() -> None:
    """Der Frühausstieg "kein Client" ist die zweite der beiden Stellen.

    Hierher kommt jede Sitzung, deren Verbindungsversuch scheiterte, bevor
    ein Client stand — der Teardown läuft trotzdem, und ein Flag, das dort
    stehen bleibt, ist genau der Fall, den niemand von Hand bemerkt.
    """
    async def run() -> None:
        coordinator = _Coordinator(BUS.Bus())
        coordinator._set_panel_link_connected(True)
        coordinator._client = None

        await coordinator._disconnect_client()

        assert coordinator.panel_link_connected is False

    asyncio.run(run())


def _patched(**attrs):
    """Modulattribute des Coordinators vorübergehend ersetzen."""
    was = {name: getattr(COORD, name) for name in attrs}
    for name, value in attrs.items():
        setattr(COORD, name, value)
    return was


def _restore(was: dict) -> None:
    for name, value in was.items():
        setattr(COORD, name, value)


def test_adopting_the_pairing_link_reports_the_panel_link() -> None:
    """Nach dem Pairing wird übernommen statt neu verbunden — auch ein Link."""
    async def run() -> None:
        coordinator = _Coordinator(BUS.Bus())
        client = _Link()
        handoff = _Link()
        coordinator._initial_client = handoff

        was = _patched(TrumaBleClient=lambda _identity: client)
        try:
            assert await coordinator._connect_and_run() is True
        finally:
            _restore(was)

        assert client.adopted == [handoff], "die Pairing-Verbindung wurde nicht adoptiert"
        assert coordinator.panel_link_connected is True, (
            "eine adoptierte Sitzung ist ein offener Link und muss gemeldet werden"
        )

    asyncio.run(run())


def test_a_fresh_connect_reports_the_link_and_the_proxy_that_carried_it() -> None:
    """Der gewöhnliche Weg: auflösen, den Proxy merken, verbinden, melden."""
    async def run() -> None:
        coordinator = _Coordinator(BUS.Bus())
        client = _Link()
        device = SimpleNamespace(address=ADDRESS)

        async def _heard(_hass, _name) -> bool:
            return True

        was = _patched(
            TrumaBleClient=lambda _identity: client,
            async_resolve_device=lambda _hass, _name, **_kw: device,
            async_wait_until_heard=_heard,
            async_remote_scanner_source=(
                lambda _hass, address: SOURCE if address == ADDRESS else None
            ),
        )
        try:
            assert await coordinator._connect_and_run() is True
        finally:
            _restore(was)

        assert client.connected_to == [device]
        assert coordinator.panel_link_connected is True, (
            "die offene Sitzung wurde nicht gemeldet"
        )
        assert coordinator._proxy_tracker.sources == [SOURCE], (
            "der Tracker kennt den Scanner nicht, über den die Route lief — "
            "dann meldet der Proxy-Sensor auf ewig 'unbekannt'"
        )

    asyncio.run(run())


def _main() -> None:
    stubs.run_tests(globals(), "Connection sensors")


if __name__ == "__main__":
    _main()
