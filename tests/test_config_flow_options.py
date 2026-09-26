#!/usr/bin/env python3
"""Der Options-Dialog und der Bond-Pfad des Einrichtungsflusses, offline.

Warum diese Datei existiert: ``TrumaOptionsFlow`` wurde von keinem Test dieses
Projekts je instanziiert, und ``async_step_pair`` von keinem je aufgerufen. Das
sind zusammen die beiden Stellen, an denen ein Nutzer die Integration überhaupt
in Betrieb nimmt und danach einstellt -- und ein Mutationslauf am 2026-09-26 hat
belegt, was daran hängt: 21 Änderungen an ``config_flow.py``, die kein Test
bemerkt. Die drei teuersten:

* Zeile 55 invertiert zeigt den Options-Dialog nie und legt sofort einen
  Eintrag mit ``data=None`` an -- die Einstellung ist weg, ohne Fehlermeldung.
* Zeile 253 invertiert meldet eine **gescheiterte** Kopplung als Erfolg: der
  Eintrag entsteht, das Panel ist nicht gebondet, nichts funktioniert, und
  ``pairing_failed`` erreicht den Nutzer nie.
* Zeile 255 invertiert übergibt die lebende Pairing-Verbindung nicht an den
  Coordinator, der sie adoptieren soll, um den RPA-Wedge direkt nach dem Bond
  zu vermeiden -- stattdessen landet ``None`` unter ``pending_clients``.

Geprüft wird durchgehend beobachtbares Verhalten des Flusses, nicht wie er
intern heißt: Wird eine Form gezeigt, und welche? Entsteht ein Eintrag, und mit
welchen Daten? Was akzeptiert das Schema? Mit welchem ``adapter_path`` wird
gebondet? Kein Home Assistant, kein BlueZ, keine Hardware -- die Fremdteile
sind gestubbt, die Flussschritte selbst laufen echt.

Was die Datei festnagelt:

1. der Options-Dialog zeigt die Form ``init`` und übernimmt beim Absenden genau
   die eingegebenen Optionen,
2. sein Schema akzeptiert ``poll_interval = 0`` -- das ist
   ``DEFAULT_POLL_INTERVAL`` und bedeutet Dauerverbindung, eine Untergrenze von
   1 würde den eigenen Vorgabewert ablehnen,
3. ``VERSION`` bleibt 1, solange das Paket kein ``async_migrate_entry``
   mitbringt (sonst sucht Home Assistant einen Migrations-Handler, findet
   keinen, und alle Entitäten verschwinden),
4. die Rediscovery nach einer RPA-Rotation aktualisiert die Adresse **ohne**
   Reload des Eintrags (gemessen 85-93 % statt ~99 % Verfügbarkeit),
5. der Bestätigungsschritt kommt vor dem Pair-Schritt,
6. der manuelle Schritt darf ein Panel wählen, für das schon ein
   Discovery-Flow offensteht,
7. der Pair-Schritt bondet erst beim Absenden, nicht beim Anzeigen,
8. ein gescheiterter Bond zeigt die Form erneut mit ``pairing_failed`` und legt
   keinen Eintrag an,
9. eine lebende Pairing-Verbindung wird unter der großgeschriebenen Adresse
   hinterlegt, ``None`` dagegen gar nicht,
10. Erst-Setup legt einen Eintrag an, Reconfigure aktualisiert den bestehenden,
11. ``_connectable_adapter_path`` liefert genau den Adapterpfad -- aus der
    lokalen BlueZ-Sicht, ersatzweise über die Adresse -- und ``None`` für ein
    Panel, das nur über einen Proxy zu hören ist, ohne zu werfen.

Run: ``/tmp/truma-ci/bin/python3 tests/test_config_flow_options.py``
(braucht ``voluptuous``: der Config-Flow baut sein Schema beim Import)
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

# Erfundene Adressen, jedes Oktett aus doppelten Zeichen -- siehe
# tests/test_placeholder_addresses.py.
PANEL_NAME = "Truma iNetX-AABBCC"
# Die Identitätsadresse, die im Eintrag steht. Klein geschrieben, weil der
# Flow sie großschreiben muss, bevor er die Pairing-Verbindung ablegt.
IDENTITY = "aa:bb:cc:dd:ee:ff"
# Die rotierende Adresse (RPA), mit der das Panel gerade wirbt.
RPA = "11:22:33:44:55:66"
# Die Quelle eines ESPHome-Proxys: so sieht ein Gerät aus, das kein lokaler
# BlueZ-Adapter trägt -- der Normalfall dieses Projekts.
PROXY_SOURCE = "EE:EE:EE:EE:EE:EE"

ADAPTER_PATH = "/org/bluez/hci0"
# BlueZ schreibt die Adresse im Objektpfad mit Unterstrichen; der Pfad hat
# bewusst vier Segmente, damit ein falscher maxsplit auffällt.
DEVICE_PATH = f"{ADAPTER_PATH}/dev_AA_BB_CC_DD_EE_FF"


def _coordinator_constant(name: str):
    """Einen Modulkonstanten-Wert aus ``coordinator.py`` lesen, ohne Import.

    Der Coordinator zieht das halbe Home Assistant nach, der Dialog braucht
    aber genau zwei Namen daraus. Der Wert kommt aus der Quelle statt aus einer
    Kopie, damit die Zusicherung „das Schema akzeptiert seinen eigenen
    Vorgabewert" wirklich am echten Vorgabewert hängt.
    """
    tree = ast.parse((SRC / "coordinator.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} steht nicht mehr am Modulrand von coordinator.py")


class _Hass:
    """Die Teile von HomeAssistant, die diese Schritte anfassen."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.background: list[str] = []

    def async_create_background_task(self, coro, name, eager_start=True):
        self.background.append(name)
        coro.close()


class _Entry:
    """Stand-in für einen ConfigEntry: Daten und Optionen, sonst nichts."""

    def __init__(self, data: dict | None = None, options: dict | None = None) -> None:
        self.data = dict(data or {})
        self.options = dict(options or {})


class _FlowBase:
    """Die Ergebnis-Fabriken, die Home Assistant beiden Flüssen mitgibt.

    Sie geben zurück statt zu werfen, damit ein Schritt genau eine Sache
    bezeugt: das Ergebnis, das der Nutzer zu sehen bekäme.
    """

    def async_show_form(
        self,
        *,
        step_id,
        data_schema=None,
        errors=None,
        description_placeholders=None,
        **kw,
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
            "description_placeholders": description_placeholders,
        }

    def async_create_entry(self, *, title=None, data=None, **kw):
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}


class _OptionsFlowBase(_FlowBase):
    """Stand-in für homeassistant.config_entries.OptionsFlow.

    ``config_entry`` ist hier ein einfaches Klassenattribut: der Dialog liest
    daraus nur ``options``, und ein Test soll den Eintrag setzen können.
    """

    config_entry: _Entry | None = None


class _ConfigFlowBase(_FlowBase):
    """Stand-in für homeassistant.config_entries.ConfigFlow.

    Alles, was Home Assistant sonst still täte -- Unique-ID setzen, den
    bestehenden Eintrag aktualisieren, den Bestätigungsschritt abkürzen --,
    wird hier mitgeschrieben statt getan. Genau daran hängen die Zusicherungen
    über ``reload_on_update`` und ``raise_on_progress``, die sonst niemand
    sieht.
    """

    # Klassenattribute, nicht __init__: TrumaConfigFlow bringt sein eigenes
    # __init__ mit und ruft super() nicht, genau wie unter der echten
    # Basisklasse.
    unique_id: str | None = None
    source = "user"
    reconfigure_entry: _Entry | None = None

    def __init_subclass__(cls, **kwargs) -> None:  # domain=DOMAIN
        super().__init_subclass__()

    @property
    def calls(self) -> dict:
        """Was die Basisklasse zu tun bekam, in Aufrufreihenfolge."""
        if not hasattr(self, "_calls"):
            self._calls: dict = {
                "unique_id": [],
                "abort_if_configured": [],
                "confirm_only": 0,
            }
        return self._calls

    @property
    def context(self) -> dict:
        if not hasattr(self, "_context"):
            self._context: dict = {}
        return self._context

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        self.calls["unique_id"].append(
            {"unique_id": unique_id, "raise_on_progress": raise_on_progress}
        )
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        self.calls["abort_if_configured"].append(
            {"updates": updates, "reload_on_update": reload_on_update}
        )
        return None

    def _async_current_ids(self):
        return set()

    def _set_confirm_only(self):
        self.calls["confirm_only"] += 1

    def _get_reconfigure_entry(self) -> _Entry:
        # Wie Home Assistant: außerhalb eines Reconfigure-Kontexts gibt es
        # keinen Eintrag, und der Zugriff ist ein Fehler statt eines Ratens.
        if self.source != SOURCE_RECONFIGURE:
            raise RuntimeError(
                "_get_reconfigure_entry ohne Reconfigure-Kontext aufgerufen"
            )
        assert self.reconfigure_entry is not None
        return self.reconfigure_entry

    def async_update_reload_and_abort(self, entry, *, data_updates=None, **kw):
        return {
            "type": "update_reload_and_abort",
            "entry": entry,
            "data_updates": data_updates,
        }


class _Advert:
    """Stand-in für einen BluetoothServiceInfoBleak-Advert."""

    def __init__(self, name: str = PANEL_NAME, address: str = RPA) -> None:
        self.name = name
        self.address = address
        self.service_uuids: list[str] = []
        self.rssi = -70
        self.connectable = True


class _Device:
    """Stand-in für ein BLEDevice, erkennbar an seinen ``details``."""

    def __init__(self, details) -> None:
        self.details = details


def _bluez_device(path: str = DEVICE_PATH) -> _Device:
    """Die lokale BlueZ-Sicht: sie allein trägt einen Objektpfad."""
    return _Device({"path": path})


def _proxy_device() -> _Device:
    """Die Sicht eines Proxys auf dieselbe Adresse: Quelle, aber kein Pfad."""
    return _Device({"source": PROXY_SOURCE, "address": IDENTITY})


class _BondSpy:
    """Fake für ``pairing.ensure_bonded``, das jeden Aufruf mitschreibt."""

    def __init__(self, result=(True, None), error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def __call__(
        self, hass, name, address, *, adapter_path=None, timeout=None
    ):
        self.calls.append(
            {
                "name": name,
                "address": address,
                "adapter_path": adapter_path,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


class _Resolver:
    """Fake für ``bt.async_resolve_device``, kwarg-treu.

    Die Unterscheidung ist der Punkt: nur die lokale Abfrage
    (``local_only=True``) darf das BlueZ-Gerät liefern, jede andere die
    pfadlose Proxy-Sicht derselben Adresse.
    """

    def __init__(self, local=None, remote=None) -> None:
        self.local = local
        self.remote = remote
        self.calls: list[dict] = []

    def __call__(self, hass, name, *, avoid=(), local_only=False, prefer_identity=False):
        self.calls.append({"name": name, "local_only": local_only})
        return self.local if local_only else self.remote


class _AddressLookup:
    """Fake für ``async_ble_device_from_address``, kwarg-treu.

    Home Assistant führt zwei getrennte Verlaufs-Dicts: ``connectable=True``
    schaut in den verbindbaren Verlauf, ``connectable=False`` in den gesamten,
    in dem ein nicht verbindbarer Scanner die lokale Sicht überschreiben kann.
    Deshalb liefert dieser Fake je Wert etwas anderes.
    """

    def __init__(self, connectable=None, unconnectable=None) -> None:
        self.connectable = connectable
        self.unconnectable = unconnectable
        self.calls: list[dict] = []

    def __call__(self, hass, address, connectable=True):
        self.calls.append({"address": address, "connectable": connectable})
        return self.connectable if connectable else self.unconnectable


def _advert_name(info) -> str | None:
    """Stand-in für ``bt.advert_name``: was der Advert an Namen trägt."""
    return info.name or None


async def _nothing_remembered(_address) -> None:
    """Ein BlueZ, das zu dieser Adresse kein Gerät kennt."""
    return None


def _no_sweep_soon(_hass) -> None:
    """Stand-in für ``bt.async_sweep_for_names_soon``: hier ohne Wirkung."""


async def _no_sweep(_hass, until=None) -> None:
    """Stand-in für ``bt.async_sweep_for_names``: findet nichts Neues."""


async def _unexpected_bond(*args, **kwargs):
    """Kein Test darf versehentlich echt bonden wollen."""
    raise AssertionError("ensure_bonded wurde nicht durch ein Fake ersetzt")


def _load():
    """Die echten ``const``/``config_flow`` laden, alles Fremde gestubbt."""
    stubs.mod("homeassistant", __path__=[])
    stubs.mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    stubs.mod(
        "homeassistant.config_entries",
        SOURCE_RECONFIGURE="reconfigure",
        ConfigEntry=_Entry,
        ConfigFlow=_ConfigFlowBase,
        ConfigFlowResult=dict,
        OptionsFlow=_OptionsFlowBase,
    )
    stubs.mod("homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name")
    stubs.mod("homeassistant.components", __path__=[])
    stubs.mod(
        "homeassistant.components.bluetooth",
        BluetoothServiceInfoBleak=_Advert,
        async_ble_device_from_address=lambda *a, **kw: None,
        async_discovered_service_info=lambda _hass, connectable=True: [],
    )
    stubs.mod("truma_pkg", __path__=[str(SRC)])
    # Aus dem Coordinator liest der Flow genau zwei Namen -- mit den echten
    # Werten, aus der Quelle gelesen.
    stubs.mod(
        "truma_pkg.coordinator",
        CONF_POLL_INTERVAL=_coordinator_constant("CONF_POLL_INTERVAL"),
        DEFAULT_POLL_INTERVAL=_coordinator_constant("DEFAULT_POLL_INTERVAL"),
    )
    # ``bt`` ist hier Werkzeug, nicht Gegenstand: die Tests biegen einzelne
    # dieser Namen im Modulraum des Flows um (siehe ``_patched``).
    stubs.mod(
        "truma_pkg.bt",
        advert_name=_advert_name,
        any_panel_named=lambda _hass: True,
        async_known_name=_nothing_remembered,
        async_resolve_device=lambda *a, **kw: None,
        async_sweep_for_names=_no_sweep,
        async_sweep_for_names_soon=_no_sweep_soon,
        is_panel_advert=lambda _info: True,
    )
    stubs.mod("truma_pkg.pairing", ensure_bonded=_unexpected_bond)
    stubs.load("const")
    return stubs.load("config_flow")


CF = _load()
DOMAIN = CF.DOMAIN
CONF_ADDRESS = CF.CONF_ADDRESS
CONF_NAME = CF.CONF_NAME
SOURCE_RECONFIGURE = CF.SOURCE_RECONFIGURE
POLL = CF.CONF_POLL_INTERVAL
DEFAULT_POLL = CF.DEFAULT_POLL_INTERVAL


@contextlib.contextmanager
def _patched(**names):
    """Namen im Modulraum des Config-Flows vorübergehend ersetzen.

    Der Flow bindet seine Helfer beim Import (``from .bt import ...``), also
    ist sein eigener Modulraum die Stelle, an der ein Test sie umbiegt -- und
    nicht das gestubbte Herkunftsmodul.
    """
    saved = {name: getattr(CF, name) for name in names}
    for name, value in names.items():
        setattr(CF, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(CF, name, value)


def _options_flow(options: dict | None = None):
    """Den Options-Dialog mit einem Eintrag dahinter."""
    flow = CF.TrumaOptionsFlow()
    flow.config_entry = _Entry(options=options)
    return flow


def _flow(*, source="user", name=None, address=None, entry=None):
    """Einen Config-Flow im gewünschten Kontext."""
    flow = CF.TrumaConfigFlow()
    flow.hass = _Hass()
    flow.source = source
    flow.reconfigure_entry = entry
    if name is not None:
        flow._name = name
    if address is not None:
        flow._address = address
    return flow


def _field(schema, key):
    """Den Marker eines Schlüssels aus einem voluptuous-Schema holen."""
    for marker in schema.schema:
        if marker.schema == key:
            return marker
    raise AssertionError(f"{key} steht nicht im Schema: {list(schema.schema)}")


def _pending_clients(flow) -> dict:
    """Was der Flow für den Coordinator hinterlegt hat."""
    return flow.hass.data.get(DOMAIN, {}).get("pending_clients", {})


class _Client:
    """Stand-in für die lebende Pairing-Verbindung."""


class _LogCatcher(logging.Handler):
    """Sammelt die Logzeilen des Flusses, statt sie auszudrucken."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _caught_logs():
    """Die Logzeilen abfangen, damit der erwartete Trace prüfbar wird.

    Ein Test, der eine Ausnahme absichtlich auslöst, soll sie nicht als
    Rauschen in die Ausgabe stellen -- und die Zeile ist ohnehin das, was den
    Fall für einen Betreiber überhaupt nachvollziehbar macht.
    """
    catcher = _LogCatcher()
    logger = CF.LOGGER
    handlers, propagate = logger.handlers[:], logger.propagate
    logger.handlers = [catcher]
    logger.propagate = False
    try:
        yield catcher
    finally:
        logger.handlers = handlers
        logger.propagate = propagate


# --- Der Options-Dialog -----------------------------------------------------


def test_the_config_flow_exposes_the_options_dialog() -> None:
    """Ohne diese Weiche gibt es in der UI kein Zahnrad."""
    flow = CF.TrumaConfigFlow.async_get_options_flow(_Entry())
    assert isinstance(flow, CF.TrumaOptionsFlow)


def test_the_options_dialog_shows_the_poll_interval_form() -> None:
    """Ohne Eingabe eine Form, und zwar die mit dem aktuellen Wert darin."""
    result = asyncio.run(_options_flow({POLL: 600}).async_step_init())
    assert result["type"] == "form", result
    assert result["step_id"] == "init"
    schema = result["data_schema"]
    assert schema is not None, "der Dialog zeigt eine Form ohne Schema"
    # Beobachtbar statt intern: eine leere Eingabe ergibt den vorbelegten Wert.
    assert schema({}) == {POLL: 600}


def test_the_options_dialog_prefills_the_default_when_nothing_is_stored() -> None:
    """Ein frischer Eintrag hat keine Optionen -- dann gilt der Vorgabewert."""
    result = asyncio.run(_options_flow().async_step_init())
    assert result["data_schema"]({}) == {POLL: DEFAULT_POLL}


def test_submitting_the_options_form_stores_exactly_those_options() -> None:
    """Absenden legt die Optionen an -- genau die eingegebenen, nicht None."""
    result = asyncio.run(_options_flow({POLL: 600}).async_step_init({POLL: 300}))
    assert result["type"] == "create_entry", result
    assert result["data"] == {POLL: 300}


def test_the_options_schema_accepts_the_live_mode_default() -> None:
    """0 heißt Dauerverbindung und ist der Vorgabewert -- er muss wählbar sein.

    Eine Untergrenze von 1 wäre ein Dialog, der seinen eigenen Vorgabewert
    ablehnt: der Coordinator prüft ``poll_interval`` überall nur auf seinen
    Wahrheitswert, 0 ist also kein ungültiger Wert, sondern der Live-Modus.
    """
    schema = asyncio.run(_options_flow().async_step_init())["data_schema"]
    assert DEFAULT_POLL == 0, "der Vorgabewert ist nicht mehr der Live-Modus"
    assert schema({POLL: 0}) == {POLL: 0}
    assert schema({POLL: DEFAULT_POLL}) == {POLL: DEFAULT_POLL}
    # Der Rest des Bereichs, damit die Prüfung nicht ganz wegfallen darf.
    assert schema({POLL: 86400}) == {POLL: 86400}
    assert schema({POLL: "300"}) == {POLL: 300}
    # ``CF.vol`` ist das voluptuous, das der Flow selbst importiert hat -- so
    # braucht diese Datei keinen eigenen Import der Bibliothek.
    for rejected in (-1, 86401):
        try:
            schema({POLL: rejected})
        except CF.vol.Invalid:
            continue
        raise AssertionError(f"{rejected} Sekunden wurden angenommen")


# --- Discovery, Bestätigung, manuelle Auswahl -------------------------------


def test_discovery_keeps_the_entry_alive_across_an_address_rotation() -> None:
    """Die RPA rotiert viermal je Stunde; ein Reload je Rotation kostet 6-14 %.

    Die Adresse wird aktualisiert (sie wird vor dem Test auf
    ``reload_on_update`` angewandt), der Eintrag aber nicht neu geladen.
    """
    advert = _Advert()
    flow = _flow(source="bluetooth")
    result = asyncio.run(flow.async_step_bluetooth(advert))
    assert result["type"] == "form", result
    assert flow.calls["abort_if_configured"] == [
        {"updates": {CONF_ADDRESS: advert.address}, "reload_on_update": False}
    ]
    assert flow.unique_id == PANEL_NAME
    assert flow.context["title_placeholders"] == {"name": PANEL_NAME}


def test_discovery_asks_for_confirmation_before_pairing() -> None:
    """Erst die Bestätigung, dann der Pair-Schritt -- und erst dort die Adresse."""
    advert = _Advert()
    flow = _flow(source="bluetooth")
    shown = asyncio.run(flow.async_step_bluetooth(advert))
    assert shown["step_id"] == "confirm", shown
    assert shown["description_placeholders"] == {"name": PANEL_NAME}
    assert flow.calls["confirm_only"] == 1, "der Schritt braucht keinen Eingabewert"
    assert flow._address is None, "die Adresse steht erst nach der Bestätigung fest"

    spy = _BondSpy()
    with _patched(ensure_bonded=spy):
        confirmed = asyncio.run(flow.async_step_confirm({}))
    assert confirmed["step_id"] == "pair", confirmed
    assert flow._address == advert.address
    assert spy.calls == [], "der Pair-Schritt bondet erst beim Absenden"


def test_the_manual_step_may_pick_a_panel_with_a_discovery_flow_open() -> None:
    """Der manuelle Schritt darf ein Panel wählen, das schon als Karte liegt.

    Mit ``raise_on_progress=True`` bräche das Hinzufügen von Hand mit
    ``already_in_progress`` ab -- der Discovery-Flow für dasselbe Panel steht
    ja offen, und genau deshalb greift der Nutzer zum manuellen Weg.
    """
    advert = _Advert(address=RPA)
    flow = _flow()
    flow._discovered = {PANEL_NAME: advert}
    spy = _BondSpy()
    with _patched(ensure_bonded=spy):
        result = asyncio.run(flow.async_step_user({CONF_ADDRESS: PANEL_NAME}))
    assert result["step_id"] == "pair", result
    assert flow.calls["unique_id"] == [
        {"unique_id": PANEL_NAME, "raise_on_progress": False}
    ]
    assert flow._address == RPA

    # Gegenprobe: im Discovery-Schritt gilt der Vorgabewert, weil dort gerade
    # dieser Flow der laufende ist.
    discovered = _flow(source="bluetooth")
    asyncio.run(discovered.async_step_bluetooth(advert))
    assert discovered.calls["unique_id"] == [
        {"unique_id": PANEL_NAME, "raise_on_progress": True}
    ]


# --- Der Pair-Schritt -------------------------------------------------------


def test_the_pair_step_waits_for_the_user_before_bonding() -> None:
    """Anzeigen bondet nicht, Absenden bondet genau einmal.

    Umgekehrt wäre der Schritt 60 s blockiert, bevor das Panel überhaupt im
    Add-Device-Modus ist -- und ein zweiter Versuch täte dann nichts mehr.
    """
    spy = _BondSpy(result=(True, None))
    flow = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(ensure_bonded=spy, async_resolve_device=lambda *a, **kw: None):
        shown = asyncio.run(flow.async_step_pair())
        assert shown["type"] == "form", shown
        assert shown["step_id"] == "pair"
        assert shown["errors"] == {}, "noch ist nichts schiefgegangen"
        assert shown["description_placeholders"] == {"name": PANEL_NAME}
        assert spy.calls == [], "der Bond lief los, ohne dass jemand absandte"

        submitted = asyncio.run(flow.async_step_pair({}))
    assert submitted["type"] == "create_entry", submitted
    assert len(spy.calls) == 1, spy.calls
    assert spy.calls[0]["name"] == PANEL_NAME
    assert spy.calls[0]["address"] == IDENTITY


def test_a_failed_bond_is_reported_instead_of_creating_an_entry() -> None:
    """Scheitert die Kopplung, sieht der Nutzer ``pairing_failed`` -- kein Eintrag.

    Ein Eintrag ohne Bond ist die schlimmste Lage: die Einrichtung meldet
    Erfolg, das Panel ist nicht gebondet, und nichts funktioniert danach.
    """
    cases = (
        # Eine Ausnahme aus dem Bond gehört ins Log, damit ein Betreiber den
        # Fall nachvollziehen kann -- und darf den Flow nicht mitreißen.
        (_BondSpy(error=RuntimeError("bond schiefgegangen")), 1),
        (_BondSpy(result=(False, None)), 0),
    )
    for spy, logged in cases:
        flow = _flow(name=PANEL_NAME, address=IDENTITY)
        with _caught_logs() as logs, _patched(
            ensure_bonded=spy, async_resolve_device=lambda *a, **kw: None
        ):
            result = asyncio.run(flow.async_step_pair({}))
        assert result["type"] == "form", result
        assert result["step_id"] == "pair"
        assert result["errors"] == {"base": "pairing_failed"}, result["errors"]
        assert _pending_clients(flow) == {}
        assert len(logs.records) == logged, [r.getMessage() for r in logs.records]
        if logged:
            assert logs.records[0].exc_info is not None, "ohne Trace kein Hinweis"


def test_a_live_pairing_connection_is_handed_to_the_coordinator() -> None:
    """Die offene Verbindung geht an den Coordinator, nicht in den Müll.

    Er adoptiert sie, damit die Sitzung nicht neu verbindet -- ein Reconnect
    direkt nach dem Bond wedget die RPA. Abgelegt wird unter der
    großgeschriebenen Adresse, weil der Coordinator dort nachsieht.
    """
    client = _Client()
    flow = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        ensure_bonded=_BondSpy(result=(True, client)),
        async_resolve_device=lambda *a, **kw: None,
    ):
        result = asyncio.run(flow.async_step_pair({}))
    assert result["type"] == "create_entry", result
    assert _pending_clients(flow) == {IDENTITY.upper(): client}

    # Ohne Verbindung darf auch kein Platzhalter liegen bleiben: der
    # Coordinator würde ``None`` als Link adoptieren.
    bare = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        ensure_bonded=_BondSpy(result=(True, None)),
        async_resolve_device=lambda *a, **kw: None,
    ):
        asyncio.run(bare.async_step_pair({}))
    assert _pending_clients(bare) == {}


def test_initial_setup_creates_an_entry_and_reconfigure_updates_it() -> None:
    """Zwei Kontexte, zwei Abschlüsse -- vertauscht stürzt der Erst-Setup ab."""
    fresh = _flow(source="user", name=PANEL_NAME, address=IDENTITY)
    with _patched(
        ensure_bonded=_BondSpy(result=(True, None)),
        async_resolve_device=lambda *a, **kw: None,
    ):
        created = asyncio.run(fresh.async_step_pair({}))
    assert created["type"] == "create_entry", created
    assert created["title"] == PANEL_NAME
    assert created["data"] == {CONF_ADDRESS: IDENTITY, CONF_NAME: PANEL_NAME}

    entry = _Entry(data={CONF_NAME: PANEL_NAME, CONF_ADDRESS: IDENTITY})
    again = _flow(
        source=SOURCE_RECONFIGURE, name=PANEL_NAME, address=IDENTITY, entry=entry
    )
    with _patched(
        ensure_bonded=_BondSpy(result=(True, None)),
        async_resolve_device=lambda *a, **kw: None,
    ):
        updated = asyncio.run(again.async_step_pair({}))
    assert updated["type"] == "update_reload_and_abort", updated
    assert updated["entry"] is entry, "ein zweiter Eintrag für dasselbe Panel"
    assert updated["data_updates"] == {CONF_ADDRESS: IDENTITY, CONF_NAME: PANEL_NAME}


def test_reconfigure_takes_name_and_address_from_the_entry() -> None:
    """Das Re-Pairing kennt das Panel schon: Name und Adresse aus dem Eintrag."""
    entry = _Entry(data={CONF_NAME: PANEL_NAME, CONF_ADDRESS: IDENTITY})
    flow = _flow(source=SOURCE_RECONFIGURE, entry=entry)
    spy = _BondSpy()
    with _patched(ensure_bonded=spy):
        result = asyncio.run(flow.async_step_reconfigure())
    assert result["step_id"] == "pair", result
    assert flow._name == PANEL_NAME
    assert flow._address == IDENTITY
    assert spy.calls == []


# --- Der Adapterpfad, auf den gebondet wird ---------------------------------


def test_the_adapter_path_needs_a_name() -> None:
    """Ohne Namen gibt es nichts aufzulösen -- mit Namen den Adapterpfad."""
    nameless = _flow(address=IDENTITY)
    with _patched(async_resolve_device=_Resolver(local=_bluez_device())):
        assert nameless._connectable_adapter_path() is None

    named = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(async_resolve_device=_Resolver(local=_bluez_device())):
        assert named._connectable_adapter_path() == ADAPTER_PATH


def test_the_adapter_path_comes_from_the_local_bluez_view() -> None:
    """Nur ein lokaler Adapter trägt einen Objektpfad, also wird lokal gefragt.

    Ohne ``local_only`` antwortet die Auflösung auf einem Host mit Proxy mit
    dessen pfadloser Sicht derselben Adresse -- und die Funktion sagt „kein
    Adapter" für ein Panel, das BlueZ vor sich sieht.
    """
    resolver = _Resolver(local=_bluez_device(), remote=_proxy_device())
    flow = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        async_resolve_device=resolver,
        async_ble_device_from_address=_AddressLookup(),
    ):
        # Genau der Adapter, nicht der Gerätepfad und nicht sein Elternteil.
        assert flow._connectable_adapter_path() == ADAPTER_PATH
    assert resolver.calls, "die Auflösung wurde gar nicht gefragt"
    assert resolver.calls[0] == {"name": PANEL_NAME, "local_only": True}


def test_the_adapter_path_falls_back_to_the_address() -> None:
    """Fand die Namensauflösung nichts, entscheidet der verbindbare Verlauf.

    ``connectable=True`` ist dabei kein Filter, sondern die Wahl des
    Verlaufs-Dicts: im gesamten Verlauf kann ein nicht verbindbarer Scanner
    die lokale Sicht überschrieben haben, und dann läge der Bond ungescoped.
    """
    lookup = _AddressLookup(
        connectable=_bluez_device(), unconnectable=_proxy_device()
    )
    flow = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        async_resolve_device=_Resolver(), async_ble_device_from_address=lookup
    ):
        assert flow._connectable_adapter_path() == ADAPTER_PATH
    assert lookup.calls == [{"address": IDENTITY, "connectable": True}]

    # Ohne Adresse gibt es keinen Ersatzweg.
    unaddressed = _flow(name=PANEL_NAME)
    unaddressed._address = None
    with _patched(
        async_resolve_device=_Resolver(),
        async_ble_device_from_address=_AddressLookup(connectable=_bluez_device()),
    ):
        assert unaddressed._connectable_adapter_path() is None


def test_a_proxy_only_panel_has_no_adapter_path() -> None:
    """Der Normalfall dieses Projekts: ESPHome-Proxy, kein lokaler Adapter.

    Beide Wege liefern eine Sicht ohne ``path``. Das Ergebnis ist ``None`` --
    und vor allem: es wird keine Ausnahme daraus, denn der Pair-Schritt läuft
    unmittelbar danach.
    """
    flow = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        async_resolve_device=_Resolver(local=_proxy_device(), remote=_proxy_device()),
        async_ble_device_from_address=_AddressLookup(
            connectable=_proxy_device(), unconnectable=_proxy_device()
        ),
    ):
        assert flow._connectable_adapter_path() is None

    # Auch wenn gar nichts auflösbar ist.
    with _patched(
        async_resolve_device=_Resolver(),
        async_ble_device_from_address=_AddressLookup(),
    ):
        assert flow._connectable_adapter_path() is None


def test_the_bond_is_scoped_to_the_adapter_that_will_carry_it() -> None:
    """Der beobachtbare Zweck des Adapterpfads: so wird wirklich gebondet.

    Ungescoped fand die Bond-Suche einen alten Bond auf einem fremden -- sogar
    einem in HA abgeschalteten -- Adapter und meldete „already bonded", ohne
    dass das Panel je beteiligt war.
    """
    spy = _BondSpy(result=(True, None))
    flow = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        ensure_bonded=spy,
        async_resolve_device=_Resolver(local=_bluez_device()),
        async_ble_device_from_address=_AddressLookup(),
    ):
        asyncio.run(flow.async_step_pair({}))
    assert len(spy.calls) == 1, spy.calls
    assert spy.calls[0]["adapter_path"] == ADAPTER_PATH
    assert spy.calls[0]["timeout"] == CF._PAIR_TIMEOUT

    # Und der Proxy-Fall: dort gibt es keinen Adapter, und das darf so
    # durchgereicht werden.
    proxy_spy = _BondSpy(result=(True, None))
    proxied = _flow(name=PANEL_NAME, address=IDENTITY)
    with _patched(
        ensure_bonded=proxy_spy,
        async_resolve_device=_Resolver(local=_proxy_device()),
        async_ble_device_from_address=_AddressLookup(unconnectable=_proxy_device()),
    ):
        asyncio.run(proxied.async_step_pair({}))
    assert proxy_spy.calls[0]["adapter_path"] is None


# --- Die Schemaversion des Eintrags -----------------------------------------


def _package_exports_a_migration_handler() -> bool:
    """Bringt ``__init__.py`` ein ``async_migrate_entry`` mit?

    Über den Syntaxbaum statt über einen Import: das Paket-``__init__`` zieht
    Home Assistant wirklich nach, und hier zählt allein, ob der Name da ist.
    """
    tree = ast.parse((SRC / "__init__.py").read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "async_migrate_entry"
        for node in tree.body
    )


def test_the_flow_version_stays_at_one_without_a_migration_handler() -> None:
    """Ein Versionssprung ohne Migration nimmt dem Nutzer alle Entitäten.

    Home Assistant sucht für jeden bestehenden Eintrag mit kleinerer Version
    einen Handler, findet keinen und lässt das Setup scheitern. Beides steht
    bewusst in einer Zusicherung, damit der Wächter beim bewussten Bump
    mitwandert statt im Weg zu stehen.
    """
    if _package_exports_a_migration_handler():
        assert CF.TrumaConfigFlow.VERSION >= 2, (
            "es gibt ein async_migrate_entry, aber nichts zu migrieren"
        )
        return
    assert CF.TrumaConfigFlow.VERSION == 1, (
        "VERSION wurde erhöht, ohne dass das Paket ein async_migrate_entry "
        "mitbringt -- jeder bestehende Eintrag scheitert dann beim Setup"
    )


def _main() -> None:
    stubs.run_tests(globals(), "Config flow options and pairing")


if __name__ == "__main__":
    _main()
