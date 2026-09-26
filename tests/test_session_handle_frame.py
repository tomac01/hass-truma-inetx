#!/usr/bin/env python3
"""Die Auswerteseite der Sitzung: was ein eingehendes Frame im Bus anrichtet.

Keine Hardware, kein Home Assistant: ``truma.const`` und ``truma.protocol``
werden echt geladen, ``session.handle_frame``, ``session.discover_params``,
``session.run_startup`` und ``session.request_measurements`` laufen gegen einen
Aufzeichner statt gegen eine GATT-Verbindung.

Warum diese Datei existiert: Der Registrierungszweig in ``session.handle_frame``
war vollständig ungetestet. Kein Test hat dem Frame-Pfad je ein Frame mit
Kontrollbyte 0x01 gegeben -- alle Frames in ``tests/`` trugen MBP (0x03), und
die Adresszuweisung wurde nur dadurch nachgestellt, dass ein Test
``client.assigned_addr`` am Fake direkt setzte. ``handle_frame`` ist aber die
einzige Stelle im Repository, die die zugewiesene Busadresse überhaupt lernt:
Ohne sie läuft ``run_startup`` in jeder Sitzung in ``StartupFailed``, und keine
Entität bekommt je Werte. ``test_protocol_frames`` behauptet die Unterscheidung
von Anfrage (Subtyp 0x01) und Antwort (0x02) bis heute nur auf der Bauseite;
hier steht die Leseseite dazu.

Der zweite Anlass ist das ``probe``-Flag der Gerätesuche. Die Sonden gehen an
einen Seed, von dem auf jedem Fahrzeug die meisten Adressen leer sind -- das
Schweigen ist der erwartete Fall. Ohne das Flag macht ``ble._send_locked``
daraus eine ungültige Verbindung und trennt, die Sitzung stirbt also bei jedem
Verbindungsaufbau. Beide vorhandenen Aufzeichner nehmen ``probe`` entgegen,
behaupten es aber nie.

Was diese Datei festnagelt:

1. die Registrierungsantwort (Kontrolle 0x01, Subtyp 0x02) übernimmt die
   zugewiesene Adresse in Transport *und* Bus, und nichts anderes tut das --
   weder die Anfrage (0x01/0x01) noch ein weitergeleitetes Subscribe-Frame
   (0x03/0x02),
2. eine Antwort ohne ``addr`` und eine Antwort ohne Transport ändern nichts
   und stürzen nicht ab,
3. ein Frame, das einen Parameter nur *beschreibt*, wird als Beschreibung
   gelernt, aber nicht als Messwert -- und erzeugt darum auch keine Entität,
4. ein halbes Namenspaar beschreibt nichts, auf beiden Wegen (Info-Frame und
   Discovery-Antwort),
5. nur die Discovery-Antwort (0x84) meldet eine Änderung; eine
   Subscribe-Bestätigung (0x82) und ein fremdes Kontrollbyte melden keine,
6. jedes Frame der Gerätesuche geht als Sonde hinaus und trägt Korrelation 0,
   auch der einleitende Broadcast und auch dann, wenn niemand bestätigt,
7. die Gerätesuche läuft genau zwei Runden: wer erst in Runde 2 spricht,
   wartet auf seinen eigenen Push,
8. das Registrierungsfenster dauert so lange, wie die Warnung behauptet,
9. und das Aufgeben eines stummen Messfühlers wird genau einmal laut gesagt.

Lauf: ``python3 tests/test_session_handle_frame.py`` (braucht ``cbor2``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# Busadressen, nicht Bluetooth-Adressen: ``Klasse << 8 | Instanz``, so wie das
# Panel selbst adressiert.
#
# APP_ADDR ist eine erfundene zugewiesene App-Adresse und liegt bewusst
# außerhalb des Geräte-Seeds (``discover_params`` schließt die eigene Adresse
# aus, sonst fragten wir uns selbst). REASSIGNED ist die Adresse, die die
# Registrierungsantwort in ihrem CBOR trägt: eine zweite, davon
# unterscheidbare, damit "übernommen" und "unverändert" nicht dasselbe
# aussehen.
APP_ADDR = 0x0501
REASSIGNED = 0x0502
# Eine dritte, die in keiner Antwort stehen darf, die keine Zuweisung ist.
NOT_OURS = 0x0999
PANEL = 0x0101
HEATER = 0x0201
# Der Elektroblock: auf den berichteten Fahrzeugen hängen die Tanks an ihm,
# und er ist weder Panel noch Heizgerät.
BLOCK = 0x0405
# Bewusst außerhalb jeder Seed-Klasse, als Platzhalter für Hardware, die noch
# niemand gemeldet hat. LATE spricht erst in der zweiten Runde.
UNSEEDED = 0x0801
LATE = 0x0802

stubs.install_homeassistant()
# Der Transport wird hier nicht ausgeführt -- der Aufzeichner unten steht
# dafür --, aber bt.py zieht HAs Bluetooth-Komponente nach, also ganz stubben.
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
CONST = stubs.load("const")
SESSION = stubs.load("session")
# Die Entitätsseite, für eine einzige Frage: was richtet ein Wert ``None`` im
# Bus an? Der Coordinator wird dafür gestubbt, weil hier der Weg vom Frame bis
# zur Entität geprüft wird und nicht sein Aufrufer.
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")

import cbor2  # noqa: E402  - nach den Stubs, die das Paket auf sys.path legen

# Die Identität, die ``run_startup`` weiterreicht. Erfundene Werte: der
# Registrierungstest unten kommt nie so weit, sie zu senden.
IDENTITY = {"username": "placeholder", "muid": "MUID", "uuid": "uuid"}


# -- Hilfsmittel -------------------------------------------------------------


class _NoSleep:
    """``asyncio``-Ersatz, dessen ``sleep`` sofort zurückkommt, aber mitschreibt.

    Das Mitschreiben ist der Punkt: das Registrierungsfenster und die Wartezeit
    je Discovery-Runde sind Sekunden, und beide sind ohne echte Uhr prüfbar,
    solange die Dauern aufgezeichnet werden.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __getattr__(self, name):  # alles andere ist das echte asyncio
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self.slept.append(delay)


def _run_catching(coro) -> tuple[BaseException | None, list[float]]:
    """Eine Koroutine mit sofortigen Schlafphasen fahren.

    Liefert die Ausnahme (oder None) *und* die aufgezeichneten Wartezeiten:
    Ein Lauf, der absichtlich scheitert, wirft seine Zeitmessung sonst mit der
    Ausnahme weg, und genau über diese Zeiten fällt das Urteil beim
    Registrierungsfenster.
    """
    shim = _NoSleep()
    real = getattr(SESSION, "asyncio")
    setattr(SESSION, "asyncio", shim)
    raised: BaseException | None = None
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001 - StartupFailed, hier absichtlich
        raised = exc
    finally:
        setattr(SESSION, "asyncio", real)
    return raised, shim.slept


def _run(coro) -> list[float]:
    """Wie ``_run_catching``, aber für die Läufe, die gelingen müssen."""
    raised, slept = _run_catching(coro)
    assert raised is None, f"der Lauf scheiterte unerwartet: {raised!r}"
    return slept


class _LogCatcher(logging.Handler):
    """Sammelt die Logzeilen des Moduls, statt sie auszudrucken."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _caught_logs():
    """Die Logzeilen von ``const.LOGGER`` abfangen.

    Zwei Gründe: Ein Test, der absichtlich eine Warnung auslöst, soll sie nicht
    als Rauschen in die Ausgabe stellen -- und bei der aufgegebenen
    Messanforderung ist die Zeile selbst der Prüfgegenstand. Stufe DEBUG, damit
    auch die leisen Zeilen ankommen: dass die ersten beiden Fehlschläge *nicht*
    warnen, ist nur dann eine Aussage, wenn sie überhaupt etwas sagen.
    """
    catcher = _LogCatcher()
    logger = CONST.LOGGER
    handlers, propagate, level = logger.handlers[:], logger.propagate, logger.level
    logger.handlers = [catcher]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield catcher
    finally:
        logger.handlers = handlers
        logger.propagate = propagate
        logger.setLevel(level)


class _Recorder:
    """Nimmt jedes gesendete Frame samt ``probe``-Flag auf.

    ``answers`` bildet ab "ein Frame an diese Adresse" -> "danach spricht diese
    Adresse zu uns", so wie ein echter Bus sich verhält: ein Gerät, das eine
    Anfrage bekommt, antwortet, und die Antwort trägt die Adresse ihres
    Absenders. Gelernt wird sie über ``handle_frame``, also auf demselben Weg
    wie im Betrieb.

    ``acks`` sind die Adressen, die den Transport noch bestätigen. ``None``
    heißt "alle" -- der echte Transport meldet mit ``probe=True`` über den
    Rückgabewert, ob das Panel das Frame angenommen hat, und für eine leere
    Adresse hält es die Bestätigung zurück.
    """

    def __init__(self, bus, answers: dict[int, int] | None = None,
                 acks: set[int] | None = None) -> None:
        self.assigned_addr = APP_ADDR
        # Eine Verbindung, die nichts trägt, gilt dem Stack weiterhin als
        # verbunden; nur der Transport selbst räumt das ab.
        self.connected = True
        self.sent: list[dict] = []
        self._bus = bus
        self._answers = answers or {}
        self._acks = acks

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        parsed = PROTO.parse_v3_frame(frame)
        assert parsed is not None, "der eigene Parser verwirft das eigene Frame"
        # Mitgeschrieben, weil es das Flag ist, an dem der echte Transport
        # seinen Abbau festmacht: ein unbeantwortetes Senden ohne ``probe``
        # macht den Stream ungültig und trennt (``ble._send_locked``).
        parsed["probe"] = probe
        self.sent.append(parsed)
        speaker = self._answers.get(parsed["dest"])
        if speaker is not None:
            # Ein Frame ohne Nutzlast: mehr braucht es nicht, damit der Bus
            # seinen Absender lernt -- das ist der Vertrag von ``handle_frame``.
            SESSION.handle_frame(self._bus, {"src": speaker, "dest": APP_ADDR})
        return self._acks is None or parsed["dest"] in self._acks


def _frame(control: int, sub_type: int, payload: dict, src: int = HEATER,
           corr_id: int = 0) -> dict:
    """Ein echtes Frame bauen und mit dem echten Parser zurücklesen."""
    frame = PROTO.build_v3_frame(
        APP_ADDR, src, control, sub_type, corr_id, cbor2.dumps(payload)
    )
    parsed = PROTO.parse_v3_frame(frame)
    assert parsed is not None, "der eigene Parser verwirft das eigene Frame"
    return parsed


def _registered() -> tuple:
    """Ein Bus und ein Transport, die beide schon eine Adresse tragen.

    Genau der Zustand, in dem eine zweite Registrierungsantwort ankommt: nur
    so ist "übernommen" von "unverändert" zu unterscheiden.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR
    return bus, _Recorder(bus)


def _discovery_frames(client: _Recorder) -> list[dict]:
    """Jedes Frame der Parameter-Gerätesuche, das der Transport bekam."""
    return [
        parsed for parsed in client.sent
        if parsed["control_raw"] == TC.CTRL_MBP
        and parsed.get("sub_type") == TC.MBP_PARAM_DISC
    ]


def _discovery_dests(client: _Recorder) -> list[int]:
    return [parsed["dest"] for parsed in _discovery_frames(client)]


def _tank_sensors(bus) -> list[str]:
    """Die Tank-Sensoren, die die echte Sensor-Plattform aus diesem Bus baut."""
    coordinator = stubs.FakeCoordinator(bus)
    made = stubs.setup_platform(SENSOR, coordinator)
    return [
        key for key in (
            getattr(entity, "_attr_translation_key", None) for entity in made
        )
        if key in ("fresh_water_level", "grey_water_level")
    ]


# -- 1. Die Registrierungsantwort -------------------------------------------


def test_the_registration_response_teaches_us_the_address_we_send_from() -> None:
    """Die Adresszuweisung, erstmals auf der Leseseite angesehen.

    Die Literale stehen hier so, wie ``session.handle_frame`` sie prüft:
    Kontrolle 0x01 (``CTRL_REGISTRATION``; 0x02 wäre ``CTRL_DISCOVERY``) und
    Subtyp 0x02 -- die *Antwort*, wogegen 0x01 die Anfrage ist. Der Docstring
    von ``test_registration_frame_is_the_documented_request`` begründet das
    Anfrage-Literal schon mit dieser Unterscheidung, prüft sie aber nur beim
    Bauen des Frames.

    Es ist die einzige Stelle im Repository, die die zugewiesene Busadresse
    lernt. Sie muss in beiden Haltern landen: der Transport setzt sie als
    ``src`` jedes ausgehenden Frames, und der Bus erkennt an ihr, dass ein
    Frame von uns selbst kein neues Gerät ist. Trifft dieser Zweig nicht,
    bleibt die Standardadresse stehen, die der Message Broker nicht routet --
    ``run_startup`` wirft ``StartupFailed``, und keine Entität bekommt Werte.
    """
    bus, client = _registered()

    changed = SESSION.handle_frame(
        bus, _frame(0x01, 0x02, {"addr": REASSIGNED}, src=PANEL), client
    )

    assert changed is False, "eine Adresszuweisung bewegt keinen Messwert"
    assert client.assigned_addr == REASSIGNED, (
        f"der Transport sendet weiter von 0x{client.assigned_addr:04X}"
    )
    assert bus.assigned_addr == REASSIGNED, (
        f"der Bus hält weiter 0x{bus.assigned_addr:04X} für unsere Adresse"
    )


def test_only_the_response_assigns_an_address_and_nothing_else_does() -> None:
    """Die Gegenprobe: Kontrolle 0x01 *und* Subtyp 0x02, beides zusammen.

    Beide Frames hier tragen ein gültiges ``addr`` im CBOR und dürfen es
    trotzdem nicht durchlassen. 0x01/0x01 ist die Registrierungs-*Anfrage*,
    0x03/0x02 das weitergeleitete Subscribe-Frame -- eine falsch übernommene
    Adresse ist der ``src`` jedes ausgehenden Frames und der Filter in
    ``note_seen``: das Panel antwortet danach nicht mehr, und alle Entitäten
    bleiben leer. Was das Panel mit Kontrolle 0x01 sonst senden kann
    (Abmeldung, Neuzuweisung, Fehler), ist unbekannt, und genau deshalb ist die
    Wache absichtlich eng.
    """
    for control, sub_type, what in (
        (0x01, 0x01, "die Registrierungs-Anfrage"),
        (0x03, 0x02, "ein weitergeleitetes Subscribe-Frame"),
    ):
        bus, client = _registered()

        changed = SESSION.handle_frame(
            bus, _frame(control, sub_type, {"addr": NOT_OURS}, src=PANEL), client
        )

        assert changed is False, f"{what} bewegt keinen Messwert"
        assert client.assigned_addr == APP_ADDR, (
            f"{what} hat dem Transport 0x{client.assigned_addr:04X} zugewiesen"
        )
        assert bus.assigned_addr == APP_ADDR, (
            f"{what} hat dem Bus 0x{bus.assigned_addr:04X} zugewiesen"
        )


def test_a_response_that_names_no_address_leaves_ours_alone() -> None:
    """Ohne ``addr`` gibt es nichts zu übernehmen.

    Übernommen würde sonst ``None``, und aus ``None`` baut ``build_v3_frame``
    kein Frame mehr: die Sitzung endet im nächsten ausgehenden Paket statt an
    einer erkennbaren Stelle.
    """
    bus, client = _registered()

    changed = SESSION.handle_frame(
        bus, _frame(0x01, 0x02, {"pv": [5, 1]}, src=PANEL), client
    )

    assert changed is False
    assert client.assigned_addr == APP_ADDR, client.assigned_addr
    assert bus.assigned_addr == APP_ADDR, bus.assigned_addr


def test_a_response_with_no_transport_behind_it_is_survivable() -> None:
    """``client`` ist laut Signatur optional, und zeitweise ist es auch None.

    ``coordinator._client`` ist zwischen zwei Verbindungen None, und der
    Notification-Handler läuft weiter. Ein Zugriff auf den Transport, der nicht
    da ist, würde mitten im Handler mit ``AttributeError`` abbrechen -- ohne
    Transport ist also nichts zu tun, und auch der Bus darf nicht umziehen:
    seine Adresse wäre dann eine, von der nichts sendet.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    changed = SESSION.handle_frame(
        bus, _frame(0x01, 0x02, {"addr": REASSIGNED}, src=PANEL)
    )

    assert changed is False
    assert bus.assigned_addr == APP_ADDR, (
        "der Bus zog auf eine Adresse um, von der kein Transport sendet"
    )


# -- 2. Beschreiben ist nicht melden ----------------------------------------


def test_a_parameter_that_is_only_described_becomes_no_entity() -> None:
    """Ein Frame ohne ``v`` beschreibt Hardware, es meldet keine.

    ``Device.reports`` prüft allein die Schlüssel-Mitgliedschaft, und genau
    daran entscheidet ``entity.async_add_rows``, ob es die Hardware gibt (der
    Wert *ist* der Beweis). Landet ``None`` als Wert im Bus, entsteht daraus
    eine Entität für einen Tank, den das Fahrzeug nicht hat -- dauerhaft
    unbekannt, und nicht von einer defekten Integration zu unterscheiden. Die
    Beschreibung selbst gehört dagegen behalten: dass der Parameter nicht
    verfügbar ist, ist gerade die interessante Aussage.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    changed = SESSION.handle_frame(bus, _frame(0x03, 0x00, {
        "tn": "FreshWater", "pn": "Level",
        "avail": 0, "type": 105, "perm": 0, "min": 0, "max": 100,
    }, src=BLOCK))

    assert changed is False, "ein Frame ohne Wert bewegt keinen Messwert"
    device = bus.device(BLOCK)
    assert device.meta("FreshWater", "Level")["avail"] == 0, (
        "die Beschreibung ging verloren"
    )
    assert device.reports("FreshWater", "Level") is False, (
        "ein beschriebener Parameter gilt als gemeldet"
    )
    assert device.get("FreshWater", "Level") is None, device.params
    assert _tank_sensors(bus) == [], (
        "eine Tank-Entität für Hardware, die das Fahrzeug nicht hat"
    )

    # Gegenprobe, damit die Aussage oben nicht bloß daran hängt, dass die
    # Plattform hier gar nichts baute: mit Wert entsteht der Sensor.
    SESSION.handle_frame(bus, _frame(0x03, 0x00, {
        "tn": "FreshWater", "pn": "Level", "v": 25, "avail": 1,
    }, src=BLOCK))
    assert _tank_sensors(bus) == ["fresh_water_level"], _tank_sensors(bus)


def test_a_half_named_parameter_describes_nothing() -> None:
    """Eine Beschreibung ohne beide Namen ist keine Beschreibung.

    ``tn`` und ``pn`` kommen im Info-Zweig aus ``cbor.get()`` ohne Default,
    also None. Der Bus baut seinen Schlüssel als f-String, es entstünde also
    ``"System.None"`` bzw. ``"None.DescribedState"`` -- Müll, der im
    Diagnose-Download auftaucht und über den gespeicherten Zustand einen
    Neustart überlebt. Behauptet wird deshalb das ganze Dict und nicht ein
    einzelner Schlüssel: der Punkt ist gerade, dass *kein* Eintrag entsteht.
    Beschreibende Felder müssen im Payload stehen, sonst lehnt der Bus die
    Beschreibung schon aus eigenem Antrieb ab und der Test wäre leer.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    # Topic ohne Parameter...
    SESSION.handle_frame(bus, _frame(0x03, 0x00, {
        "tn": "System", "type": 4, "perm": 1, "min": 0, "max": 2,
    }, src=HEATER))
    # ...und Parameter ohne Topic.
    SESSION.handle_frame(bus, _frame(0x03, 0x00, {
        "pn": "DescribedState", "type": 4, "perm": 1, "max": 2,
    }, src=HEATER))

    device = bus.device(HEATER)
    assert device.param_meta == {}, device.param_meta
    assert device.params == {}, device.params


def test_a_discovery_answer_with_no_names_describes_nothing() -> None:
    """Dasselbe im verschachtelten Zweig, wo ``tn`` per Default leer ist.

    Hier heißt der Müll-Schlüssel ``".Level"`` und entsteht ohne jedes Zutun
    der Gegenseite, allein aus einem ``topics``-Eintrag ohne Namen.
    ``test_junk_is_ignored_rather_than_stored`` prüft die Schwester dieser
    Aussage ("ein Wert ohne jede Beschreibung lässt keine leere Hülle
    zurück"), aber nichts im Verzeichnis prüft den Schlüsselraum.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    SESSION.handle_frame(bus, _frame(0x03, 0x84, {"topics": [
        # Topic ohne "tn", Parameter vollständig beschrieben.
        {"parameters": [{"pn": "Level", "v": 3, "min": 0, "max": 3}]},
        # Topic benannt, Parameter ohne "pn".
        {"tn": "System", "parameters": [{"type": 4, "min": 0, "max": 2}]},
    ]}, src=BLOCK))

    device = bus.device(BLOCK)
    assert device.param_meta == {}, device.param_meta
    assert device.params == {}, device.params


# -- 3. Welches Frame eine Änderung meldet ---------------------------------


def test_a_subscribe_acknowledgement_moves_nothing() -> None:
    """Nur der Subtyp 0x84 trägt Werte, nicht jedes MBP-Frame.

    Die Subscribe-Bestätigung 0x82 kommt bei jedem Verbindungsaufbau vier Mal
    und trägt ein CBOR-Dict, aber keine ``topics``. Als Änderung gemeldet, löst
    sie ``async_set_updated_data`` und einen Namensabgleich für ein Frame aus,
    das nichts trug.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    changed = SESSION.handle_frame(
        bus, _frame(0x03, 0x82, {"tn": TC.TOPIC_BATCHES[0]}, src=PANEL)
    )
    assert changed is False, "eine Subscribe-Bestätigung meldete eine Änderung"

    # Gegenprobe auf demselben Kontrollbyte: die Discovery-Antwort bewegt etwas.
    changed = SESSION.handle_frame(bus, _frame(0x03, 0x84, {
        "topics": [{"tn": "FreshWater", "parameters": [{"pn": "Level", "v": 42}]}],
    }, src=BLOCK))
    assert changed is True, "die Discovery-Antwort erreichte den Bus nicht"
    assert bus.device(BLOCK).get("FreshWater", "Level") == 42


def test_a_frame_on_an_unknown_control_byte_moves_nothing() -> None:
    """Der Schlussfall heißt "dieses Frame bewegt nichts".

    0x05 ist laut Parser SECURITY; das Repository wertet es nirgends aus. Ein
    solches Frame darf keine Zustandsmeldung an die Entitäten auslösen --
    gelernt wird davon nur, dass es an dieser Adresse ein Gerät gibt, und das
    passiert vor jeder Auswertung der Nutzlast.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    changed = SESSION.handle_frame(bus, _frame(
        0x05, 0x01, {"tn": "FreshWater", "pn": "Level", "v": 42}, src=BLOCK
    ))

    assert changed is False, "ein fremdes Kontrollbyte meldete eine Änderung"
    assert bus.device(BLOCK).reports("FreshWater", "Level") is False
    assert BLOCK in bus.addresses, "der Absender wurde nicht gelernt"


# -- 4. Die Gerätesuche als Sonde ------------------------------------------


def test_every_discovery_frame_is_sent_as_a_probe() -> None:
    """Schweigen ist hier eine der Antworten, also darf es nicht trennen.

    Das Panel ist der Peer, der bestätigt, und es hält die Bestätigung für ein
    Frame an eine Adresse zurück, hinter der nichts steht -- was auf den
    meisten Fahrzeugen für die meisten Seed-Adressen gilt. Ohne ``probe`` macht
    ``ble._send_locked`` daraus einen ungültigen Transport und trennt: die
    Sitzung stirbt bei jedem Verbindungsaufbau, gleich beim einleitenden
    Broadcast, den nur Heizgerät und Panel beantworten.

    Die Korrelation wird gleich mitgenagelt. Die Integration liest sie nie
    zurück, deshalb ist ein Test die einzige Stelle, an der der gesendete Wert
    überhaupt festgehalten ist -- ``test_protocol_frames`` hält es für
    Register-, Subscribe- und Identitäts-Frames genauso, und die Gerätesuche
    baut ihre Frames inline in ``session.py``, hat also keinen Frame-Bauer, an
    dem das sonst hängen könnte.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR
    client = _Recorder(bus)

    _run(SESSION.discover_params(client, bus, "placeholder", lambda: 0.0))

    frames = _discovery_frames(client)
    assert frames, "die Gerätesuche hat nichts gesendet"
    assert TC.DEV_BROADCAST in _discovery_dests(client), (
        "der einleitende Broadcast fehlt, also prüft dies seine Sonde nicht"
    )
    for parsed in frames:
        assert parsed["probe"] is True, (
            f"die Sonde an 0x{parsed['dest']:04X} ging ohne probe hinaus und "
            "beendet die Sitzung, sobald das Panel die Bestätigung zurückhält"
        )
        assert parsed["corr_id"] == 0, (
            f"die Anfrage an 0x{parsed['dest']:04X} trägt Korrelation "
            f"{parsed['corr_id']}, nicht 0"
        )


def test_a_bus_that_barely_answers_is_still_asked_to_the_end() -> None:
    """Der Normalfall: fast alles schweigt, und die Suche läuft durch.

    ``send(probe=True)`` meldet ein fehlendes Transport-Ack durch einen
    Rückgabewert von False statt durch eine Ausnahme -- von der Suche aus sieht
    eine Verbindung, die nichts mehr trägt, also genauso aus wie ein Bus, auf
    dem niemand zu Hause ist. Nur das Panel bestätigt hier, und das reicht: ein
    *teilweise* beantworteter Lauf ist kein Fehler, und trotz der
    unbeantworteten Frames muss jede Seed-Adresse gefragt werden, jede als
    Sonde.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR
    client = _Recorder(bus, acks={TC.DEV_PANEL})

    _run(SESSION.discover_params(client, bus, "placeholder", lambda: 0.0))

    dests = set(_discovery_dests(client))
    assert set(TC.DEVICE_SEED) <= dests, sorted(
        f"0x{addr:04X}" for addr in set(TC.DEVICE_SEED) - dests
    )
    for parsed in _discovery_frames(client):
        assert parsed["probe"] is True, (
            f"die unbeantwortete Sonde an 0x{parsed['dest']:04X} ging ohne "
            "probe hinaus und trennt die Verbindung"
        )


def test_a_device_that_speaks_in_the_second_round_waits_for_its_own_push() -> None:
    """Zwei Runden, nicht mehr -- die Obergrenze, nicht nur die Untergrenze.

    Die Antworten der ersten Runde tragen die Adressen ihrer Absender, und
    daraus baut die zweite Runde ihre Ziele. Wer erst in Runde 2 spricht,
    wartet auf seinen eigenen Push: eine dritte Runde kostet Frames und eine
    weitere mehrsekündige Wartezeit bei jedem Verbindungsaufbau, und genau
    darum geht es dieser Prüfung (vergleiche
    ``test_startup_runs_discovery_without_paying_per_device``). Die vorhandenen
    Tests verketten keine Antworten und klammern die Rundenzahl deshalb nur von
    unten ein.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR
    assert UNSEEDED not in TC.DEVICE_SEED, "erst in Runde 2 bekannt, bitte"
    assert LATE not in TC.DEVICE_SEED, "erst in Runde 3 bekannt, bitte"
    client = _Recorder(bus, answers={TC.DEV_PANEL: UNSEEDED, UNSEEDED: LATE})

    slept = _run(SESSION.discover_params(client, bus, "placeholder", lambda: 0.0))

    dests = _discovery_dests(client)
    assert UNSEEDED in dests, "die zweite Runde findet nicht mehr statt"
    assert LATE not in dests, "es gibt eine dritte Runde"
    assert slept.count(SESSION._PARAM_DISC_SETTLE) == 2, (
        f"{slept.count(SESSION._PARAM_DISC_SETTLE)} Runden-Wartezeiten "
        f"von {SESSION._PARAM_DISC_SETTLE}s statt 2"
    )


# -- 5. Das Registrierungsfenster ------------------------------------------


def test_the_registration_window_lasts_as_long_as_the_warning_claims() -> None:
    """Der Tick der Registrierungsschleife ist eine Sekunde.

    ``for _ in range(_REGISTER_TIMEOUT)`` mal ``sleep(1)`` ergibt das Fenster,
    das die Warnung als "within {_REGISTER_TIMEOUT}s" ausgibt und das der
    Kommentar an der Konstante mit "seconds" beschreibt. Mit einem anderen Tick
    hält die Registrierung den Verbindungsplatz des Adapters ein Vielfaches der
    behaupteten Zeit auf einer toten Verbindung fest und nennt im Log weiterhin
    die alte Zahl.

    Behauptet wird die Summe gegen die Konstante, nicht gegen 20: eine bewusst
    geänderte Wartezeit soll diesen Test nicht rot machen, ein geänderter Tick
    schon.
    """
    bus = BUS.Bus()
    client = _Recorder(bus)
    # Keine Adresse zugewiesen: genau so sieht eine Verbindung aus, die steht
    # und nichts trägt.
    client.assigned_addr = TC.DEV_APP_DEFAULT

    with _caught_logs():
        raised, slept = _run_catching(
            SESSION.run_startup(client, bus, IDENTITY, "placeholder", lambda: 0.0)
        )

    assert raised is not None, "die Registrierung wartete nicht auf eine Adresse"
    assert sum(slept) == SESSION._REGISTER_TIMEOUT, (
        f"das Fenster dauerte {sum(slept)}s, die Warnung nennt "
        f"{SESSION._REGISTER_TIMEOUT}s"
    )
    assert f"within {SESSION._REGISTER_TIMEOUT}s" in str(raised), str(raised)


# -- 6. Das Aufgeben eines stummen Messfühlers -----------------------------


def test_giving_up_on_a_publisher_is_said_once_out_loud() -> None:
    """Drei Versuche, dann Ruhe -- und genau dieses Aufgeben wird gesagt.

    Ein Tankpegel, der nicht mehr aufgefrischt wird, zeigt weiter seine letzte
    Messung: die Entität sieht in Ordnung aus, der Wert ist nur alt. Das ist
    Issue #4, das leise zurückkommt, und diese eine Warnzeile ist das Einzige,
    was es sagt. Die beiden Fehlschläge davor dürfen dagegen nicht warnen -- in
    derselben Minute wird wieder gefragt, eine Warnung wäre also schlicht
    falsch und im Minutentakt Rauschen im Log.
    """
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR
    # Beide Tanks haben ihren Pegel einmal gemeldet: das ist der Beweis, auf
    # den ``request_measurements`` seine Frage stützt.
    bus.update("FreshWater", "Level", 25, BLOCK)
    bus.update("GreyWater", "Level", 0, BLOCK)
    # Nichts wird bestätigt: der Messfühler ist fort oder wurde neu gepaart.
    client = _Recorder(bus, acks=set())

    def _warnings(records) -> list:
        return [record for record in records if record.levelno >= logging.WARNING]

    rounds = []
    with _caught_logs() as caught:
        for _ in range(5):
            _run(SESSION.request_measurements(client, bus, "placeholder"))
            rounds.append((_warnings(caught.records), len(caught.records)))

    # Runde 1 und 2: gesagt, aber nicht gewarnt.
    assert rounds[0][0] == [], [record.getMessage() for record in rounds[0][0]]
    assert rounds[1][0] == [], [record.getMessage() for record in rounds[1][0]]
    assert rounds[1][1] > rounds[0][1] > 0, "die Fehlschläge blieben unerwähnt"

    # Runde 3: genau eine Warnung je Tank, und sie nennt den dritten Versuch.
    given_up = rounds[2][0]
    assert len(given_up) == 2, [record.getMessage() for record in given_up]
    assert {record.args[3] for record in given_up} == {"FreshWater", "GreyWater"}, (
        [record.args for record in given_up]
    )
    for record in given_up:
        assert record.args[2] == SESSION._MEASURE_MISSES_BEFORE_GIVING_UP, record.args

    # Runde 4 und 5: nicht mehr gefragt, also auch nicht mehr gewarnt.
    assert len(rounds[3][0]) == 2, [record.getMessage() for record in rounds[3][0]]
    assert len(rounds[4][0]) == 2, [record.getMessage() for record in rounds[4][0]]


def _main() -> None:
    stubs.run_tests(globals(), "session frame handling")


if __name__ == "__main__":
    _main()
