#!/usr/bin/env python3
"""Golden-Frame-Prüfungen: welches Byte an welcher Stelle im Rahmen steht.

Keine Hardware, kein Home Assistant: ``truma.protocol`` und ``truma.const``
werden echt geladen, ``session.handle_frame`` und ``session.discover_params``
laufen gegen einen Aufzeichner statt gegen eine GATT-Verbindung.

Warum diese Datei existiert: Die Werte in ``truma/const.py`` und in den
Frame-Bauern sind aus Mitschnitten der Original-App rekonstruiert. Sie stehen
im ganzen Repository an genau einer Stelle -- kein zweites Dokument, kein
Rohframe-Mitschnitt, keine Spezifikation von Truma. Ohne das Fahrzeug lässt
sich dieses Wissen nicht neu herleiten, und bis zu dieser Datei hat es keine
Zusicherung berührt: ``build_register_frame`` wurde von keinem Test je
aufgerufen (in ``tests/stubs.py`` auf ``None`` gestubbt), und die vorhandenen
Protokolltests vergleichen durchweg ``parsed["sub_type"] == TC.MBP_WRITE`` --
Konstante gegen sich selbst, was mit jeder Änderung mitwandert.

Deshalb stehen die Sollwerte hier als **Literale**. Ihre Begründung steht
jeweils am Test: entweder im Docstring der Produktionsfunktion, die den Wert
unabhängig von ``const.py`` nennt (``build_register_frame``: "ctrl=0x01,
sub=0x01, corr=0x42, dest=0xFFFF"; ``build_subscribe_frame``: "ctrl=0x03,
sub=0x02, dest=0x0000"; ``build_write_frame``: die CBOR-Form), oder in
``session.py``, das auf der Leseseite hart gegen Literale prüft.

Was diese Datei festnagelt:

1. das Registrierungs-Frame -- die erste Nachricht jeder Sitzung -- mit Ziel,
   Quelle, Kontrollbyte, Subtyp, Korrelation und der angekündigten
   Protokollversion als ganzem CBOR-Dict,
2. das Subscribe-Frame je Topic-Stapel, an den Message Broker,
3. das Write-Frame samt der vollständigen Nutzlastform inklusive ``id``,
4. die Identitätssequenz als sechs Writes in fester Reihenfolge,
5. die Längeninvariante des Längenfelds auf dem Draht, auf Bau- und Leseseite
   getrennt,
6. die Untergrenzen des Parsers: 15 Byte werden verworfen, 16 und 17 Byte
   nicht -- und ein Frame mit leerer Nutzlast lehrt den Bus trotzdem seinen
   Absender,
7. die Kopplung von Anfrage- und Antwort-Subtyp der Parameter-Discovery,
8. und den Adressbereich der Gerätesuche, Instanz für Instanz.

Lauf: ``python3 tests/test_protocol_frames.py`` (braucht ``cbor2``).
"""

from __future__ import annotations

import asyncio
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# Busadressen, nicht Bluetooth-Adressen: ``class << 8 | instance``, wie das
# Panel selbst adressiert. 0x0501 ist eine erfundene zugewiesene App-Adresse
# -- sie darf nicht im Geräte-Seed liegen, sonst würde die Gerätesuche sie
# herausfiltern (session.discover_params schließt die eigene Adresse aus).
APP_ADDR = 0x0501
PANEL = 0x0101
HEATER = 0x0201

stubs.install_homeassistant()
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
stubs.load("const")
SESSION = stubs.load("session")

import cbor2  # noqa: E402  - nach den Stubs, die das Paket auf sys.path legen


# -- Hilfsmittel -------------------------------------------------------------


class _NoSleep:
    """``asyncio``-Ersatz, dessen ``sleep`` sofort zurückkommt.

    Die Gerätesuche wartet nach jeder Runde mehrere Sekunden. Für die Frage,
    welche Adresse ein Frame bekommt, ist das reine Wartezeit.
    """

    def __getattr__(self, name):  # alles andere ist das echte asyncio
        return getattr(asyncio, name)

    async def sleep(self, _delay, *_a, **_kw):
        return None


class _Recorder:
    """Nimmt gesendete Frames auf, so wie es der echte Transport täte."""

    def __init__(self) -> None:
        self.assigned_addr = APP_ADDR
        self.connected = True
        self.sent: list[bytes] = []

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        self.sent.append(frame)
        # Der echte Transport meldet, ob das Panel das Frame angenommen hat.
        return True


def _roundtrip(frame: bytes) -> dict:
    """Ein gebautes Frame mit dem echten Parser zurücklesen."""
    parsed = PROTO.parse_v3_frame(frame)
    assert parsed is not None, "der eigene Parser verwirft das eigene Frame"
    return parsed


def _assert_length_invariant(frame: bytes, cbor_payload: bytes) -> None:
    """Das Längenfeld auf dem Draht gegen die tatsächliche Länge stellen.

    ``packet_size`` steht in Byte 4-5 und zählt ab Byte 7, also alles nach
    Kontrollbyte: 9 Byte Segmentierungskopf + 2 Byte Sub-Header + CBOR. Auf
    echter Hardware verwirft das Panel jedes Frame mit falscher Länge, und der
    Bau→Parse-Rundlauf allein ist gegen den Wert blind, weil der Parser ihn
    nur liest und nie prüft.
    """
    assert struct.unpack_from("<H", frame, 4)[0] == len(cbor_payload) + 11, frame.hex()
    assert struct.unpack_from("<H", frame, 4)[0] == len(frame) - 7, frame.hex()
    # 4 Byte dest/src + 2 Byte Länge + 1 Byte ctrl + 9 Byte Segmentierung
    # + 2 Byte Sub-Header = 18 Byte Kopf vor der CBOR-Nutzlast.
    assert len(frame) == 18 + len(cbor_payload), frame.hex()


# -- 1. Das Registrierungs-Frame --------------------------------------------


def test_registration_frame_is_the_documented_request() -> None:
    """Die erste Nachricht jeder Sitzung, erstmals angesehen.

    Sollwerte aus dem Docstring von ``build_register_frame``: "CBOR:
    {"pv": [5, 1]}, ctrl=0x01, sub=0x01, corr=0x42, dest=0xFFFF".

    Der Subtyp steht als Literal 0x01 da, weil 0x02 die *Antwort* ist:
    ``session.handle_frame`` erkennt die Adresszuweisung an
    ``control == 0x01 and sub_type == 0x02`` (session.py, Registrierungs-
    Antwort). 0x01 ist die Anfrage. Wer diese Erwartung an eine geänderte
    Implementierung anpasst, hebt genau diese Unterscheidung auf -- und dann
    weist das Panel keine Adresse zu, ``run_startup`` wirft ``StartupFailed``,
    und keine Entität bekommt je Werte.
    """
    frame = PROTO.build_register_frame(APP_ADDR)
    parsed = _roundtrip(frame)

    assert parsed["dest"] == 0xFFFF, "die Registrierung geht an den Broadcast"
    assert parsed["src"] == APP_ADDR
    assert parsed["control_raw"] == 0x01, "0x02 wäre CTRL_DISCOVERY"
    assert parsed["control"] == "REGISTRATION", parsed["control"]
    assert parsed["sub_type"] == 0x01, "0x02 ist die Antwort, nicht die Anfrage"
    assert parsed["corr_id"] == 0x42
    # Als ganzes Dict, damit auch die Minor-Stelle festgenagelt ist: die 5 ist
    # eine reverse-engineerte Magic Number und steht nirgends sonst im Repo.
    assert parsed["cbor"] == {"pv": [5, 1]}, parsed["cbor"]

    # Das Kontrollbyte an seiner Stelle im Draht-Layout, unabhängig vom Parser.
    assert frame[6] == 0x01, frame.hex()
    _assert_length_invariant(frame, cbor2.dumps({"pv": [5, 1]}))


# -- 2. Das Subscribe-Frame --------------------------------------------------


def test_subscribe_frame_carries_one_topic_batch_to_the_broker() -> None:
    """Sollwerte aus dem Docstring von ``build_subscribe_frame``.

    Dort steht "ctrl=0x03, sub=0x02, dest=0x0000" -- eine Angabe, die
    unabhängig von ``const.py`` ist, weshalb das Literal 0x02 hier keine
    Tautologie ist. Mit einem anderen Subtyp wird kein Topic abonniert, es
    kommen keine Notifications, und alle Entitäten bleiben dauerhaft leer.
    """
    batch = TC.TOPIC_BATCHES[0]
    frame = PROTO.build_subscribe_frame(APP_ADDR, batch)
    parsed = _roundtrip(frame)

    assert parsed["dest"] == 0x0000, "Subscriptions gehen an den Message Broker"
    assert parsed["src"] == APP_ADDR, "von der Default-App-Adresse routet er nicht"
    assert parsed["control_raw"] == 0x03, "0x03 ist MBP"
    assert parsed["control"] == "MBP", parsed["control"]
    assert parsed["sub_type"] == 0x02, "sub=0x02 laut Docstring; 0x01 wäre ein Write"
    assert parsed["corr_id"] == 0x00
    assert parsed["cbor"] == {"tn": batch}, parsed["cbor"]

    _assert_length_invariant(frame, cbor2.dumps({"tn": batch}))


def test_every_topic_batch_survives_the_wire_unchanged() -> None:
    """Kein Stapel darf unterwegs verloren gehen oder sich umsortieren.

    ``run_startup`` sendet die vier Stapel aus ``TOPIC_BATCHES`` einzeln; ein
    Stapel, dessen Namen der Bau verschluckt, kostet alle Entitäten dieser
    Topics, ohne dass die Sitzung auffällig scheitert.
    """
    assert len(TC.TOPIC_BATCHES) == 4, TC.TOPIC_BATCHES
    for batch in TC.TOPIC_BATCHES:
        parsed = _roundtrip(PROTO.build_subscribe_frame(APP_ADDR, batch))
        assert parsed["cbor"] == {"tn": batch}, parsed["cbor"]
        assert parsed["sub_type"] == 0x02, parsed["sub_type"]


# -- 3. Das Write-Frame -----------------------------------------------------


def test_write_frame_is_the_documented_payload_shape() -> None:
    """Sollwerte aus dem Docstring von ``build_write_frame``.

    Dort steht die Nutzlast als ``{"tn": topic, "pn": param, "v": value,
    "id": 0}``. Das ``id``-Feld liest im Component niemand zurück -- es sind
    aber Bytes, die das Gerät *verlassen*, und die Funktion hat keinen anderen
    Zweck, als das aus Mitschnitten rekonstruierte Draht-Format zu
    reproduzieren. Deshalb wird die CBOR-Map als Ganzes geprüft.

    Der Subtyp steht als Literal 0x01 da: ``build_subscribe_frame`` belegt
    laut eigenem Docstring 0x02, und ein Write, das als Subscribe hinausgeht,
    schreibt nichts -- weder Solltemperatur noch Schalter noch die
    MeasureRequest aus ``session.request_measurements``.
    """
    frame = PROTO.build_write_frame(APP_ADDR, PANEL, "AirHeating", "TargetTemp", 21)
    parsed = _roundtrip(frame)

    assert parsed["dest"] == PANEL, "ein Write ist gerichtet, nicht Broadcast"
    assert parsed["src"] == APP_ADDR
    assert parsed["control_raw"] == 0x03, "0x03 ist MBP"
    assert parsed["sub_type"] == 0x01, "0x02 wäre ein Subscribe, kein Write"
    assert parsed["corr_id"] == 0x00
    assert parsed["cbor"] == {
        "tn": "AirHeating",
        "pn": "TargetTemp",
        "v": 21,
        "id": 0,
    }, parsed["cbor"]
    # Getrennt noch einmal die Form allein, damit ein zusätzliches oder
    # fehlendes Feld auch dann auffällt, wenn jemand die Werte anpasst.
    assert set(parsed["cbor"]) == {"tn", "pn", "v", "id"}, parsed["cbor"]

    _assert_length_invariant(
        frame,
        cbor2.dumps({"tn": "AirHeating", "pn": "TargetTemp", "v": 21, "id": 0}),
    )


def test_a_measure_request_is_a_write_of_one() -> None:
    """Der Weg, auf dem ein Tanksensor zu einer frischen Messung kommt.

    ``session.request_measurements`` schreibt 1 auf
    ``<Topic>.MeasureRequest``; ohne diesen Write liefert die Discovery die
    letzte Messung, die auf einem Tank Stunden alt sein kann (Issue #4).
    """
    frame = PROTO.build_write_frame(
        APP_ADDR, HEATER, "FreshWater", TC.MEASURE_REQUEST_PARAM, 1
    )
    parsed = _roundtrip(frame)

    assert parsed["sub_type"] == 0x01, "eine Messanforderung ist ein MBP-Write"
    assert parsed["cbor"] == {
        "tn": "FreshWater",
        "pn": "MeasureRequest",
        "v": 1,
        "id": 0,
    }, parsed["cbor"]


# -- 4. Die Identitätssequenz ----------------------------------------------


def test_the_identity_sequence_is_six_writes_in_a_fixed_order() -> None:
    """Sechs echte Schreibbefehle, die bei jedem Connect hinausgehen.

    ``session.run_startup`` sendet jedes Element von
    ``build_identity_frames`` einzeln. In jedem bisherigen Test war die
    Funktion auf ``None`` gestubbt, also waren Inhalt UND Reihenfolge
    unbehauptet -- einschließlich des Abschluss-Sentinels, an dem das Panel
    das Ende der Sequenz erkennt.

    Der Docstring der Funktion verspricht bis heute eine Liste von
    ``(frame_bytes, delay_after)``-Tupeln. Geliefert werden nackte Frames, und
    ``session.py`` sendet sie genau so -- ein Widerspruch, der nur deshalb
    niemandem auffiel, weil nichts die Funktion je ausgeführt hat. Der Test
    hält den tatsächlichen Vertrag fest.
    """
    identity = {"username": "u", "muid": "m", "uuid": "x"}
    before = int(time.time())
    frames = PROTO.build_identity_frames(APP_ADDR, identity)
    after = int(time.time())

    assert len(frames) == 6, f"{len(frames)} Frames: ein Write kam dazu oder fiel weg"
    for frame in frames:
        assert isinstance(frame, bytes), type(frame)

    parsed = [_roundtrip(frame) for frame in frames]
    for entry in parsed:
        assert entry["dest"] == PANEL, "die Identität geht an das Panel"
        assert entry["src"] == APP_ADDR
        assert entry["control_raw"] == 0x03, "0x03 ist MBP"
        assert entry["sub_type"] == 0x01, "jedes Element ist ein Write"
        assert entry["corr_id"] == 0x00

    # 1. Die Systemzeit, als Epoch-Sekunden. Der Wert ist die einzige
    #    variable Stelle der Sequenz, deshalb wird er eingeklammert statt
    #    verglichen.
    assert parsed[0]["cbor"]["tn"] == "SystemTime"
    assert parsed[0]["cbor"]["pn"] == "Time"
    assert isinstance(parsed[0]["cbor"]["v"], int)
    assert before <= parsed[0]["cbor"]["v"] <= after, parsed[0]["cbor"]
    assert parsed[0]["cbor"]["id"] == 0, parsed[0]["cbor"]

    # 2. Lot direkt danach, mit 0. Was "Lot" bedeutet, erklärt das Repo
    #    nirgends und liest es nirgends zurück -- deshalb ist dieser Test die
    #    einzige Stelle, an der der gesendete Wert überhaupt festgehalten ist.
    assert parsed[1]["cbor"] == {
        "tn": "SystemTime",
        "pn": "Lot",
        "v": 0,
        "id": 0,
    }, parsed[1]["cbor"]

    # 3.-5. Die Identität, in dieser Reihenfolge und durchgereicht.
    assert parsed[2]["cbor"] == {
        "tn": "MobileIdentity",
        "pn": "UserName",
        "v": "u",
        "id": 0,
    }, parsed[2]["cbor"]
    assert parsed[3]["cbor"] == {
        "tn": "MobileIdentity",
        "pn": "Muid",
        "v": "m",
        "id": 0,
    }, parsed[3]["cbor"]
    assert parsed[4]["cbor"] == {
        "tn": "MobileIdentity",
        "pn": "Uuid",
        "v": "x",
        "id": 0,
    }, parsed[4]["cbor"]

    # 6. Der Sentinel als letztes Frame, mit genau dieser einen Nutzlast.
    assert parsed[-1]["cbor"] == {"LastMessage": 1}, parsed[-1]["cbor"]


# -- 5. Die Längeninvariante ------------------------------------------------


def test_packet_size_counts_everything_after_the_control_byte() -> None:
    """Das Längenfeld, gegen mehrere Nutzlastgrößen gestellt.

    Gebaut wird über ``build_v3_frame`` selbst, damit die Invariante auch für
    Nutzlasten gilt, die kein Bauer dieses Moduls erzeugt -- unter anderem für
    die leere Nutzlast, mit der ``session.discover_params`` arbeitet.
    """
    for payload in (b"", cbor2.dumps({"a": 1}), cbor2.dumps({"tn": "x" * 200})):
        frame = PROTO.build_v3_frame(
            PANEL, APP_ADDR, TC.CTRL_MBP, TC.MBP_WRITE, 0, payload
        )
        _assert_length_invariant(frame, payload)


# Ein von Hand geschriebenes Frame: es pinnt die Byte-Offsets des Parsers
# unabhängig vom Bauer. Läse der Parser die Länge ab Offset 5, käme aus
# 0x00 0x03 der Wert 768 statt 30 heraus -- der Bau→Parse-Rundlauf würde das
# nie zeigen, weil beide Seiten gemeinsam wegdriften.
WIRE_SAMPLE = bytes([
    0x01, 0x01,                          # [0-1] dest = 0x0101 (Panel)
    0x01, 0x02,                          # [2-3] src  = 0x0201 (Heizer)
    0x1E, 0x00,                          # [4-5] packet_size = 30
    0x03,                                # [6]   control = MBP
    0, 0, 0, 0, 0, 0, 0, 0, 0,           # [7-15] Segmentierungskopf
    0x00,                                # [16]  sub_type = MBP_INFO
    0x00,                                # [17]  corr_id
])


def test_the_header_fields_are_read_from_their_documented_offsets() -> None:
    """Das Layout aus dem Docstring von ``build_v3_frame``, von Hand gelegt."""
    parsed = _roundtrip(WIRE_SAMPLE)

    assert parsed["dest"] == 0x0101, parsed
    assert parsed["src"] == 0x0201, parsed
    assert parsed["pkt_size"] == 30, parsed
    assert parsed["control_raw"] == 0x03, parsed
    assert parsed["control"] == "MBP", parsed
    assert parsed["sub_type"] == 0x00, parsed
    assert parsed["corr_id"] == 0x00, parsed


# -- 6. Die Untergrenzen des Parsers ---------------------------------------


# Ein regulärer V3-Frame mit leerer Nutzlast: 4 Byte dest/src, 2 Byte Länge,
# 1 Byte Kontrolle, 9 Byte Segmentierungskopf -- genau 16 Byte, kein Sub-Header.
# ``ble._handle_data`` gibt jede Notification unverändert an den Parser, auch
# ein Fragment ohne vorangehende 0x83-Ankündigung, und der Docstring dort
# nennt diese Untergrenze ausdrücklich als Vertrag.
EMPTY_PAYLOAD_FRAME = (
    struct.pack("<HH", APP_ADDR, HEATER)
    + struct.pack("<H", 9)
    + bytes([0x03])
    + bytes(9)
)


def test_fifteen_bytes_are_not_a_frame() -> None:
    """Unterhalb des Kopfes gibt es nichts zu lesen."""
    assert len(EMPTY_PAYLOAD_FRAME) == 16, len(EMPTY_PAYLOAD_FRAME)
    assert PROTO.parse_v3_frame(bytes(15)) is None
    assert PROTO.parse_v3_frame(b"") is None


def test_a_frame_with_an_empty_payload_is_accepted() -> None:
    """16 Byte sind kein Müll, sondern der Kopf ohne Nutzlast.

    Die Grenze liegt bei 16, nicht bei 17: wird sie verschoben, verwirft der
    Parser einen gültigen Frame; wird der Sub-Header trotzdem gelesen, wirft
    er ``IndexError`` mitten im Notification-Handler.
    """
    parsed = PROTO.parse_v3_frame(EMPTY_PAYLOAD_FRAME)

    assert parsed is not None, "ein gültiger 16-Byte-Frame wurde verworfen"
    assert parsed["dest"] == APP_ADDR, parsed
    assert parsed["src"] == HEATER, parsed
    assert parsed["control_raw"] == 0x03, parsed
    # Ohne Sub-Header gibt es keinen Subtyp -- und der Parser erfindet keinen.
    assert "sub_type" not in parsed, parsed
    assert "corr_id" not in parsed, parsed
    assert "cbor" not in parsed, parsed


def test_a_seventeen_byte_frame_has_a_sub_type_and_no_correlation() -> None:
    """Dieselbe Grenze von der anderen Seite: Subtyp ja, Korrelation nein."""
    frame = EMPTY_PAYLOAD_FRAME + bytes([0x04])
    parsed = PROTO.parse_v3_frame(frame)

    assert parsed is not None, "ein gültiger 17-Byte-Frame wurde verworfen"
    assert parsed["sub_type"] == 0x04, parsed
    assert parsed["corr_id"] == 0, "ohne Byte 17 ist die Korrelation 0"
    assert parsed["cbor"] is None, parsed


def test_an_empty_payload_frame_still_teaches_the_bus_its_sender() -> None:
    """Warum die Untergrenze zählt: der Absender wird gelernt.

    ``session.handle_frame`` ruft ``bus.note_seen(src)``, *bevor* es die
    CBOR-Nutzlast prüft. Die gelernte Adresse landet in ``bus.addresses``,
    wird nie wieder gelöscht und ist es, was die Parameter-Discovery direkt
    anspricht. Ein verworfener 16-Byte-Frame hieße: dieses Gerät wird nie
    gefragt.
    """
    bus = BUS.Bus()
    parsed = PROTO.parse_v3_frame(EMPTY_PAYLOAD_FRAME)
    assert parsed is not None

    changed = SESSION.handle_frame(bus, parsed)

    assert changed is False, "ein Frame ohne Nutzlast bewegt keinen Messwert"
    assert HEATER in bus.addresses, bus.addresses


# -- 7. Anfrage- und Antwort-Subtyp der Parameter-Discovery ----------------


def test_the_discovery_request_pairs_with_the_answer_the_parser_accepts() -> None:
    """0x04 hinaus, 0x84 zurück -- ein Paar, das ganz im Repo liegt.

    ``session.handle_frame`` filtert die verschachtelte Discovery-Antwort hart
    per Literal (``control == 0x03 and sub_type == 0x84``), und nur dieser
    Zweig trägt topics/parameters in den Bus. Der ausgehende Subtyp ist
    0x04; wandert er, passt die Antwort nicht mehr zum Parserzweig, und
    Tank-, Gasflaschen- und Stromwerte bleiben nach jedem Neustart leer,
    während das Panel sie anzeigt (Issue #7).
    """
    assert TC.MBP_PARAM_DISC == 0x04, TC.MBP_PARAM_DISC
    assert TC.MBP_PARAM_DISC | 0x80 == 0x84, TC.MBP_PARAM_DISC
    assert TC.MBP_PARAM_DISC_RESP == 0x84, TC.MBP_PARAM_DISC_RESP
    assert TC.MBP_SUBSCRIBE | 0x80 == TC.MBP_SUBSCRIBE_RESP == 0x82

    # Die Anfrage, gebaut wie in session.discover_params: leere Nutzlast.
    request = PROTO.build_v3_frame(
        HEATER, APP_ADDR, TC.CTRL_MBP, TC.MBP_PARAM_DISC, 0, b""
    )
    assert _roundtrip(request)["sub_type"] == 0x04, "die Anfrage trägt 0x04"

    # Die Antwort, mit dem aus der Anfrage abgeleiteten Subtyp: sie muss den
    # Zweig treffen, der Werte in den Bus trägt.
    answer = PROTO.build_v3_frame(
        APP_ADDR,
        HEATER,
        TC.CTRL_MBP,
        TC.MBP_PARAM_DISC | 0x80,
        0,
        cbor2.dumps(
            {"topics": [{"tn": "FreshWater", "parameters": [{"pn": "Level", "v": 42}]}]}
        ),
    )
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    changed = SESSION.handle_frame(bus, _roundtrip(answer))

    assert changed is True, "die Discovery-Antwort erreichte den Bus nicht"
    assert bus.device(HEATER).get("FreshWater", "Level") == 42


# -- 8. Der Adressbereich der Gerätesuche ----------------------------------


# Der Seed, Instanz für Instanz ausgeschrieben. Alle vorhandenen Aussagen über
# ``DEVICE_SEED`` sind Teilmengen-Aussagen (``set(TC.DEVICE_SEED) <= dests``),
# die ein geschrumpftes Seed nie bemerken -- deshalb hier die Gleichheit.
EXPECTED_SEED = {
    0x0101,  # Panel
    0x0201,  # Heizgerät
    # Strom und Klima: 0x0405 war ein Schaudt-Elektroblock, 0x0406 eine
    # Dometic-FreshJet-Dachklimaanlage.
    0x0401, 0x0402, 0x0403, 0x0404, 0x0405, 0x0406, 0x0407, 0x0408,
    # 0x0603/0x0604 waren die linke und rechte Gasflasche, 0x0601 die eigene
    # BLE-Verwaltung des Panels.
    0x0601, 0x0602, 0x0603, 0x0604, 0x0605, 0x0606, 0x0607, 0x0608,
}


def test_the_device_seed_covers_every_instance_of_both_swept_classes() -> None:
    """Die beiden Klassen werden durchgekehrt, nicht aufgezählt.

    Fällt die erste Instanz aus einer Kehrung heraus, kostet das 0x0401 bzw.
    0x0601 -- und 0x0601 ist die eigene Bluetooth-Seite des Panels, die laut
    ``const.py`` keinen Broadcast beantwortet und nur bei Änderung
    publiziert. Sie trägt sich also nie selbst nach: ohne den Seed kommen die
    drei Diagnose-Entitäten (freie Bond-Plätze, Verbindungs- und
    Verwaltungszustand) nach einem Neustart nie zustande.
    """
    assert set(TC.DEVICE_SEED) == EXPECTED_SEED, sorted(
        f"0x{a:04X}" for a in set(TC.DEVICE_SEED) ^ EXPECTED_SEED
    )
    # Einzeln benannt, damit die Fehlermeldung sagt, was fehlt.
    assert 0x0201 in TC.DEVICE_SEED, "das Heizgerät wird nicht mehr direkt gefragt"
    assert 0x0401 in TC.DEVICE_SEED, "die erste Instanz der Stromklasse fehlt"
    assert 0x0601 in TC.DEVICE_SEED, "die Bluetooth-Verwaltung des Panels fehlt"


def _discovery_dests(client: _Recorder) -> set[int]:
    """Die Adressen, an die eine Parameter-Discovery gesendet wurde."""
    dests = set()
    for frame in client.sent:
        parsed = PROTO.parse_v3_frame(frame)
        if parsed["control_raw"] == 0x03 and parsed.get("sub_type") == 0x04:
            dests.add(parsed["dest"])
    return dests


def test_discovery_addresses_every_seeded_device_on_the_wire() -> None:
    """Nicht nur der Seed, sondern die Frames, die daraus entstehen.

    Der Seed könnte vollständig sein und die Suche ihn trotzdem nicht
    ausschöpfen. Hier läuft ``session.discover_params`` echt durch und die
    Ziele werden aus den gesendeten Bytes zurückgelesen.
    """
    client = _Recorder()
    bus = BUS.Bus()
    bus.assigned_addr = APP_ADDR

    real_asyncio = SESSION.asyncio
    SESSION.asyncio = _NoSleep()
    try:
        asyncio.run(SESSION.discover_params(client, bus, "placeholder", lambda: 0.0))
    finally:
        SESSION.asyncio = real_asyncio

    dests = _discovery_dests(client)
    assert 0xFFFF in dests, "der einleitende Broadcast fehlt"
    assert 0x0601 in dests, "die Bluetooth-Verwaltung des Panels wird nicht gefragt"
    assert 0x0401 in dests, "die erste Instanz der Stromklasse wird nicht gefragt"
    assert EXPECTED_SEED <= dests, sorted(
        f"0x{a:04X}" for a in EXPECTED_SEED - dests
    )
    # Die eigene zugewiesene Adresse ist kein Gerät: das Panel setzt sie in
    # den src mancher Frames, und ohne den Ausschluss fragten wir uns selbst.
    assert APP_ADDR not in dests, "wir haben uns selbst nach Parametern gefragt"


def _main() -> None:
    stubs.run_tests(globals(), "protocol frames")


if __name__ == "__main__":
    _main()
