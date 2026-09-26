#!/usr/bin/env python3
"""Prüft, ob die Tests nur erfundene Adressen nennen.

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
   die Prüfung nicht stillschweigend leerlaufen lassen.

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
)

# Bytes, die als Platzhalter durchgehen: doppelte Ziffern und doppelte
# Buchstaben. Sie ergeben die Adressen, die man auf Anhieb als erfunden liest.
PLACEHOLDER_BYTES = frozenset(f"{d}{d}" for d in "0123456789ABCDEF")

# Zwei bis sechs hexadezimale Bytepaare, durch Doppelpunkte getrennt. Die
# Wortgrenzen halten längere Zeichenketten heraus, in denen zufällig ein
# solches Muster steckt.
ADDRESS = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){1,5}[0-9A-Fa-f]{2}\b")


def _offenders(text: str) -> list[tuple[int, str]]:
    """Alle Adressen im Text, die keine Platzhalter-Bytes führen."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in ADDRESS.finditer(line):
            octets = match.group(0).upper().split(":")
            if not all(octet in PLACEHOLDER_BYTES for octet in octets):
                found.append((number, match.group(0)))
    return found


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
