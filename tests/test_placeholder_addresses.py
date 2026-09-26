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

Warum nicht das ganze Verzeichnis: Mehrere ältere Testdateien führen
Adressen, die dieser Regel nicht genügen. Sie umzustellen ist eine eigene
Aufgabe mit eigenem Prüfaufwand und gehört nicht in die Behebung, für die
dieser Test geschrieben wurde. Wer eine Datei umstellt, trägt sie hier nach.

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
