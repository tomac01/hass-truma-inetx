#!/usr/bin/env python3
"""Prüft, ob Tests und ausgelieferte Dateien nur erfundene Adressen nennen.

Warum das hier steht: Dieses Repository ist öffentlich. Eine Adresse, die
ein Test aus einem echten Aufbau übernimmt, ist nach einem Push dauerhaft in
der Git-Historie -- zusammen mit der Dokumentation im selben Repository, die
beschreibt, wo dieser Aufbau steht und was er tut. Beides einzeln ist harmlos,
beides zusammen nicht.

Genau das ist passiert: ``test_proxy_route.py`` hat die Quelle des Scanners
sechsmal aus einem laufenden Aufbau abgeschrieben, obwohl der Test von diesem
Wert nichts weiter braucht als seine Wiedererkennbarkeit -- irgendein
undurchsichtiger Schlüssel hätte es genauso getan. Kein Test hat das gesehen,
weil kein Test die Testdateien selbst liest.

Die Regel, die dieser Test durchsetzt, ist bewusst eine **positive**: Nicht
eine Liste verbotener Werte entscheidet -- die müsste die echten Adressen
hier im Klartext wiederholen und damit genau das tun, was sie verhindern soll
--, sondern eine Liste erlaubter Bytes. Eine Adresse gilt als Platzhalter,
wenn jedes ihrer Bytes aus ``00 11 22 ... FF`` stammt, also aus den Bytes,
die niemand versehentlich hinschreibt. ``AA:BB:CC:DD:EE:FF`` besteht, jede
Adresse aus einem realen Gerät fällt durch, ohne dass sie hier stehen muss.

Was der Test festnagelt:

1. in den unten aufgeführten Dateien ist jede Kette hexadezimaler Bytepaare
   -- ob ganze Adresse ``AA:BB:CC:DD:EE:FF`` oder gekürzt ``AA:BB`` -- aus
   Platzhalter-Bytes aufgebaut, und zwar im ganzen Dateitext: ein Wert in
   einem Kommentar oder Docstring steht nach einem Push genauso öffentlich da
   wie einer im Code,
2. die aufgeführten Dateien gibt es überhaupt -- eine umbenannte Datei soll
   die Prüfung nicht stillschweigend leerlaufen lassen,
3. in ``custom_components/`` (samt der Karte in ``.js``), ``docs/`` und
   ``examples/`` trägt jeder Entity-ID-Präfix der Form ``truma_inetx_XXXXXX``
   einen Suffix aus drei doppelten Zeichenpaaren wie ``bbccdd``, gleich in
   welcher Schreibung -- und diese drei Bäume gibt es, samt je einer
   Stichprobe, damit auch diese Prüfung nicht leerläuft.

Warum nicht das ganze Verzeichnis: Mehrere ältere Testdateien führen weiterhin
Adressen, die dieser Regel nicht genügen. Jede einzelne umzustellen kostet
eigenen Prüfaufwand -- eine Adresse trägt oft eine Form, die der Test braucht
(eine RPA muss oben ``01`` stehen haben, eine Identitätsadresse muss auf den
Namenssuffix des Panels enden), und ein blind ersetzter Wert prüft danach
etwas anderes. Die Liste wächst deshalb Datei für Datei. Wer eine umstellt,
trägt sie hier nach; zuletzt geschehen am 2026-09-26, in mehreren Durchgängen:
zunächst ``test_no_route_issue.py`` und ``test_device_from_bluez.py``, in denen
ein Review echte Adressen des Panels gefunden hatte, dann
``test_pairing_transport_dispatch.py``, ``test_pairing_rotation.py``,
``test_pairing_local_fallback.py``, ``test_bus_dump_tool.py``,
``test_config_flow_options.py`` und ``test_bus_range_edges.py``, und zuletzt
``test_panel2_discovery.py`` und ``test_passive_scan_discovery.py``, die beide
dieselbe echte RPA trugen -- die letzten zwei vollständigen echten Adressen im
Verzeichnis. ``test_entry_teardown.py`` kommt mit hinzu, weil es nach demselben
Durchgang nur noch eine Platzhalteradresse führt. Damit stehen dreizehn Dateien
auf der Liste.

Nicht aufgenommen, obwohl adressfrei: ``test_param_discovery.py``. Seine
Messprotokolle nennen Uhrzeiten der Form ``HH:MM``, die von zwei Bytepaaren
nicht zu unterscheiden sind (siehe unten). Die Datei führt keine Adresse, also
gewinnt die Aufnahme nichts, was den Umbau ihrer Prosa aufwöge.

Was dieser Test NICHT sieht: eine Adresse, die nicht mit Doppelpunkten
geschrieben ist. BlueZ schreibt sie in seinen D-Bus-Pfaden mit Unterstrichen
(``dev_AA_BB_CC_DD_EE_FF``), und ein solcher Pfad fällt hier durch keine
Prüfung -- wer einen einträgt, prüfe die Bytes selbst. Wo eine Datei auf der
Liste solche Pfade braucht, leitet sie sie darum aus der Adresskonstante ab,
statt sie ein zweites Mal hinzuschreiben.

Was dieser Test ebenso NICHT sieht: den Namenssuffix eines Panels. Es heißt
``Truma iNetX-XXXXXX``, und diese sechs Hexzeichen sind die letzten drei Bytes
seiner Identitätsadresse -- nur eben ohne Doppelpunkte, also greift das Muster
oben nicht. Zusammen mit der öffentlichen OUI des Herstellers liegt damit ein
großer Teil einer echten Adresse offen; deshalb sind auch die Suffixe am
2026-09-26 Platzhalter geworden: ``BBCCDD`` für das Panel des Besitzers,
``556677`` für das zweite, fremde Panel aus #6. Wer einen Suffix ändert, prüfe
die Adressen derselben Datei mit -- ``bt.address_kind`` erkennt eine
Identitätsadresse ausschließlich daran, dass der Name auf ihre letzten sechs
Hexzeichen endet, und ein allein geänderter Suffix macht den Test am falschen
Ende grün.

Eine Form dieses Suffixes sieht er seit dem 2026-09-30 doch: den
Entity-ID-Präfix. Home Assistant bildet ihn aus dem Gerätenamen -- aus
``Truma iNetX-BBCCDD`` wird ``truma_inetx_bbccdd`` --, er trägt also dieselben
sechs Hexzeichen. In Karte, Doku und Beispielen stand bis dahin der Präfix
eines fremden Panels, übernommen aus dem Original-Repo; kein Test hatte ihn
gesehen, weil die Prüfung oben nur Testdateien liest und nur Bytepaare mit
Trennzeichen kennt. Diese Prüfung (Punkt 3) liest ganze Bäume statt einer
Liste: Anders als in den Testdateien braucht dort kein Präfix eine bestimmte
Form, er ist nur ein Name, den der Leser durch seinen eigenen ersetzt. Den
Gerätenamen in der Form ``Truma iNetX-XXXXXX`` prüft sie nicht, und ein
Entity-Name aus genau sechs Hexbuchstaben direkt hinter ``truma_inetx_``
(``decade``, ``facade``) schlüge an -- beides gibt es dort heute nicht.

Und umgekehrt: Eine Uhrzeit der Form ``HH:MM:SS`` ist von drei Bytepaaren
nicht zu unterscheiden und schlägt hier an. Das ist kein Fehlalarm, den man
wegkonfiguriert -- die Regel darf nicht aufgeweicht werden, nur weil Prosa
bequemer wäre --, sondern eine Einschränkung an die Prosa der geprüften
Dateien: Messprotokolle darin nennen das Datum, nicht die Sekunde.

Nicht abgedeckt: alles, was keine Adresse ist. Ein Hostname, eine Seriennummer
oder ein Schlüssel aus einem echten Aufbau fällt hier nicht auf -- ein Test,
der danach suchte, müsste die gesuchten Namen im Klartext nennen.

Run: ``python3 tests/test_placeholder_addresses.py``
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

TESTS = Path(__file__).resolve().parent

# Dateien, die durchgehend Platzhalter führen und es bleiben sollen.
GUARDED = (
    "test_proxy_route.py",
    "test_connection_sensors.py",
    "test_no_route_issue.py",
    "test_device_from_bluez.py",
    "test_pairing_transport_dispatch.py",
    "test_pairing_rotation.py",
    "test_pairing_local_fallback.py",
    "test_bus_dump_tool.py",
    "test_config_flow_options.py",
    "test_bus_range_edges.py",
    "test_panel2_discovery.py",
    "test_passive_scan_discovery.py",
    "test_entry_teardown.py",
    "test_coordinator_state_flags.py",
)

# Bytes, die als Platzhalter durchgehen: doppelte Ziffern und doppelte
# Buchstaben. Sie ergeben die Adressen, die man auf Anhieb als erfunden liest.
PLACEHOLDER_BYTES = frozenset(f"{d}{d}" for d in "0123456789ABCDEF")

# Hexadezimale Bytepaare in allen drei Schreibweisen, die im Projekt
# vorkommen: mit Doppelpunkten (zwei bis sechs Paare, damit auch ein
# abgeschnittener Rest auffällt), mit Unterstrichen wie in BlueZ-Objektpfaden
# und mit Bindestrichen, die BlueZ als Ersatznamen liefert. Die beiden
# letzten Formen sind nachträglich dazugekommen: am 2026-09-26 hatten sie je
# einen echten Wert überleben lassen, weil das Muster nur Doppelpunkte kannte
# -- einen davon in ``custom_components/truma_inetx/bt.py``, also im Code, der
# an Nutzer geht. Die Wortgrenzen halten längere Zeichenketten heraus, in
# denen zufällig ein solches Muster steckt.
ADDRESS = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}:){1,5}[0-9A-Fa-f]{2}\b"
    r"|\b(?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}\b"
    r"|\b(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}\b"
)

# Wo der Entity-Präfix geprüft wird: alles, was an Nutzer geht oder sie
# anleitet. Ganze Bäume statt einer Liste, damit eine neue Datei ohne
# Nachtrag mitgeprüft wird.
ROOT = TESTS.parent
PREFIX_TREES = ("custom_components", "docs", "examples")

# Der Entity-ID-Präfix eines Panels, ``truma_inetx_`` und sechs Hexzeichen.
# Danach darf weder Buchstabe noch Ziffer folgen, wohl aber ``_`` und der Name
# der Entität (``..._bbccdd_vorgang``) -- deshalb eine Vorausschau statt einer
# Wortgrenze, die zwischen zwei Wortzeichen wie ``d`` und ``_`` nicht greift.
ENTITY_PREFIX = re.compile(r"truma_inetx_([0-9a-f]{6})(?![0-9a-z])", re.IGNORECASE)


def _offenders(text: str) -> list[tuple[int, str]]:
    """Alle Adressen im Text, die keine Platzhalter-Bytes führen."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in ADDRESS.finditer(line):
            octets = re.split("[:_-]", match.group(0).upper())
            if not all(octet in PLACEHOLDER_BYTES for octet in octets):
                found.append((number, match.group(0)))
    return found


def _prefix_offenders(text: str) -> list[tuple[int, str]]:
    """Alle Entity-Präfixe im Text, deren Suffix kein Platzhalter ist."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in ENTITY_PREFIX.finditer(line):
            suffix = match.group(1).upper()
            pairs = (suffix[0:2], suffix[2:4], suffix[4:6])
            if not all(pair in PLACEHOLDER_BYTES for pair in pairs):
                found.append((number, match.group(0)))
    return found


def _prefix_files() -> list[Path]:
    """Jede Datei der geprüften Bäume, ohne Pythons Bytecode-Ablage."""
    files: list[Path] = []
    for tree in PREFIX_TREES:
        files += sorted(
            path
            for path in (ROOT / tree).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return files


def test_the_guarded_files_exist() -> None:
    """Eine umbenannte Datei darf die Prüfung nicht leerlaufen lassen."""
    for name in GUARDED:
        assert (TESTS / name).is_file(), f"{name} steht auf der Liste, fehlt aber"


def test_guarded_tests_name_only_placeholder_addresses() -> None:
    """Keine Adresse aus einem echten Aufbau in den geprüften Dateien."""
    for name in GUARDED:
        offenders = _offenders((TESTS / name).read_text(encoding="utf-8"))
        assert not offenders, (
            f"{name} nennt eine Adresse, die kein Platzhalter ist: "
            + ", ".join(f"Zeile {number}" for number, _ in offenders)
            + " -- erwartet werden Bytes aus "
            + " ".join(sorted(PLACEHOLDER_BYTES))
        )


def test_the_prefix_trees_exist() -> None:
    """Ein umbenannter Baum darf die Präfixprüfung nicht leerlaufen lassen."""
    for tree in PREFIX_TREES:
        assert (ROOT / tree).is_dir(), f"{tree}/ steht auf der Liste, fehlt aber"
    scanned = {path.relative_to(ROOT).as_posix() for path in _prefix_files()}
    # Je eine Stichprobe, darunter die Karte: Sie geht als ``.js`` an Nutzer,
    # und eine Prüfung, die nur Python läse, ginge an ihr vorbei.
    for name in (
        "custom_components/truma_inetx/frontend/truma-climate-dial-card.js",
        "docs/card.md",
        "examples/lovelace/truma-controls.yaml",
    ):
        assert name in scanned, f"{name} wird nicht mitgeprüft"


def test_shipped_files_name_only_placeholder_prefixes() -> None:
    """Kein Entity-Präfix eines echten Panels in Code, Karte, Doku, Beispielen."""
    failures = []
    for path in _prefix_files():
        # Auch Bilder gehen durch: ein SVG ist Text, und ob eine Datei Text
        # ist, soll nicht ihre Endung entscheiden. Was nicht UTF-8 ist, wird
        # ersetzt statt übersprungen.
        text = path.read_bytes().decode("utf-8", errors="replace")
        for number, _ in _prefix_offenders(text):
            failures.append(f"{path.relative_to(ROOT).as_posix()} Zeile {number}")
    assert not failures, (
        "Entity-Präfix, dessen Suffix kein Platzhalter ist: "
        + ", ".join(failures)
        + " -- erwartet wird ``truma_inetx_`` mit drei doppelten Zeichenpaaren,"
        " etwa ``truma_inetx_bbccdd``"
    )


def test_the_prefix_rule_would_notice_a_real_suffix() -> None:
    """Die Präfixregel selbst: Platzhalter bestehen, alles andere fällt durch."""
    assert _prefix_offenders("entity: climate.truma_inetx_bbccdd") == []
    assert _prefix_offenders("sensor.truma_inetx_BBCCDD_vorgang") == []
    assert _prefix_offenders("sensor.truma_inetx_556677_vorgang") == []
    # Der Domänenname allein, und ein Name ohne sechs Hexzeichen dahinter.
    assert _prefix_offenders("custom_components/truma_inetx/card.js") == []
    assert _prefix_offenders("tools/truma_inetx_bus_dump.py") == []
    assert _prefix_offenders("climate.truma_inetx_a1b2c3") == [(1, "truma_inetx_a1b2c3")]
    # Groß geschrieben, und mit dem Namen der Entität dahinter.
    assert _prefix_offenders("button.TRUMA_INETX_A1B2C3_live") == [
        (1, "TRUMA_INETX_A1B2C3")
    ]
    # Zwei doppelte Paare reichen nicht, es müssen alle drei sein.
    assert _prefix_offenders("x: truma_inetx_bbccd1") == [(1, "truma_inetx_bbccd1")]


def test_the_rule_would_notice_a_real_address() -> None:
    """Die Regel selbst: Platzhalter bestehen, alles andere fällt durch."""
    assert _offenders("addr = 'AA:BB:CC:DD:EE:FF'") == []
    assert _offenders("addr = 'AA:BB'") == []
    assert _offenders("source = 'something:else'") == []
    assert _offenders("# kein Byte: 1:2:3") == []
    assert _offenders("addr = '01:23:45:67:89:AB'") == [(1, "01:23:45:67:89:AB")]
    assert _offenders("# auch im Kommentar: A1:B2") == [(1, "A1:B2")]


def _main() -> None:
    stubs.run_tests(globals(), "Placeholder addresses")


if __name__ == "__main__":
    _main()
