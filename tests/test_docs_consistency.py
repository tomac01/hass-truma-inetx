#!/usr/bin/env python3
"""Prüft, ob die Dokumentation dasselbe sagt wie der Code.

Warum das hier steht: Der Sprung auf den Upstream 0.9 hat die Entity-IDs
zweimal umbenannt, und eine Doku, die weiter die alten Namen nennt, ist
schlimmer als gar keine -- sie schickt jemanden in ein Dashboard, das
schweigend leere Karten zeigt. Die Beispiel-YAML nannte nach dem Sprung
drei Entitäten, die es nicht mehr gibt, und keiner der bestehenden Tests
hat das gesehen, weil kein Test Text liest.

Dieser Test vergleicht deshalb die Doku gegen den Code, nicht gegen eine
zweite Kopie der Doku. Alle Erwartungen werden aus ``translations/``,
``profiles.py``, ``manifest.json`` und ``ble.py`` abgeleitet: Wer im Code
umbenennt, bekommt hier Rot, bis die Doku nachzieht.

Was der Test festnagelt:

1. jeder Datei-Verweis in README, ``docs/`` und ``examples/`` zeigt auf eine
   Datei, die es gibt (der Fork-Hinweis im README nannte
   ``docs/upgrading-fork.md``, lange bevor es die Datei gab),
2. die Benutzeranleitung verweist auf den Umstiegs-Hinweis des Forks,
3. der Umstiegs-Hinweis nennt die Entitäten mit genau den Namen, die die
   deutsche Übersetzung vergibt, die Zahl der Timer-Slots aus
   ``profiles.py`` -- unmittelbar vor dem Wort "Timer", sonst belegt die
   Vier aus "vier Kopplungen" jede beliebige Slot-Zahl -- und die
   Upstream-Version aus ``manifest.json``,
4. Benutzeranleitung und Lovelace-Beispiel nennen die Bedienelemente in
   beiden Sprachfassungen so, wie die Übersetzung sie nennt,
5. kein Dokument dehnt einen Entitätsnamen zu einem, den es nicht gibt
   ("Electric heating output"), und tauscht auch kein Wort gleicher Länge
   aus ("Elektrische Heizleistung"),
6. im Lovelace-Beispiel steht die Warnung "IDs stehen erst nach der
   Installation fest" VOR dem ersten Platzhalter, in beiden Sprachfassungen
   -- danach hätte sie niemand mehr gelesen,
7. jede Entity-ID in ``truma-controls.yaml`` lässt sich auf etwas
   zurückführen, das die Integration heute noch erzeugen kann, und alle
   tragen denselben Platzhalter-Präfix (die Anleitung verlangt eine
   globale Ersetzung -- mit zwei Präfixen bliebe die Hälfte stehen),
8. kein Dokument behauptet, der Diesel-Schalter sei ersetzt oder entfernt,
   solange der Code ihn anbietet,
9. die Transport-Doku erwähnt die ``probe``-Ausnahme, solange ``ble.py``
   sie kennt -- sonst steht dort, jeder Fehlschlag trenne die Sitzung, und
   das stimmt für die Parameter-Suche nicht.

Nicht abgedeckt: ein Dokument, das den richtigen Namen an einer Stelle
nennt und den alten an einer anderen, ohne dass beide ein Wort teilen.

Run: ``python3 tests/test_docs_consistency.py``
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "truma_inetx"
DOCS = ROOT / "docs"
EXAMPLES = ROOT / "examples"
UPGRADE_NOTE = DOCS / "upgrading-fork.md"
LOVELACE_README = EXAMPLES / "lovelace" / "README.md"
LOVELACE_YAML = EXAMPLES / "lovelace" / "truma-controls.yaml"

# Zahlwörter nur so weit, wie ein Panel Timer-Slots haben kann.
GERMAN_NUMBERS = {
    1: "ein",
    2: "zwei",
    3: "drei",
    4: "vier",
    5: "fünf",
    6: "sechs",
    7: "sieben",
    8: "acht",
    9: "neun",
    10: "zehn",
}

# Worte, mit denen ein Satz behauptet, etwas gebe es nicht mehr. "removes"
# fehlt absichtlich: "The integration never removes entities itself" ist das
# Gegenteil einer solchen Behauptung.
GONE_WORDS = re.compile(
    r"\b(entfernt|ersetzt|abgelöst|replaced|removed|no longer)\b|nicht mehr",
    re.IGNORECASE,
)


def _markdown_files() -> list[Path]:
    """Alle Markdown-Dateien, die zur Doku dieses Repos gehören."""
    files = [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]
    files += sorted(EXAMPLES.rglob("*.md"))
    dumps = ROOT / "dumps" / "README.md"
    if dumps.exists():
        files.append(dumps)
    return [f for f in files if f.exists()]


def _slugify(text: str) -> str:
    """Näherung an Home Assistants ``slugify`` für Entitätsnamen.

    Genau genug für den Vergleich hier: Umlaute werden zerlegt und die
    Akzente verworfen, alles andere außer Buchstaben und Ziffern wird zum
    Trennzeichen. "ß" muss vorher weg, weil es keine Zerlegung hat.
    """
    text = text.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def _translations(lang: str) -> dict[str, dict[str, dict]]:
    """Der ``entity``-Block einer Übersetzungsdatei."""
    path = SRC / "translations" / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8"))["entity"]


def _entity_name(lang: str, platform: str, key: str) -> str:
    """Der übersetzte Name einer Entität, ohne Platzhalter wie ``{slot}``."""
    name = _translations(lang)[platform][key]["name"]
    return name.replace("{slot}", "").strip()


def _upstream_version() -> str:
    """Die Upstream-Version aus der Fork-Version, also ohne ``-live.N``."""
    version = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))["version"]
    return version.split("-live.")[0]


def _timer_slot_count() -> int:
    """Die Zahl der Timer-Slots, aus ``profiles.py`` gelesen statt geraten."""
    tree = ast.parse((SRC / "profiles.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "TIMER_SLOTS" not in targets:
            continue
        call = node.value
        assert isinstance(call, ast.Call), ast.dump(call)
        start, stop = (ast.literal_eval(arg) for arg in call.args)
        return stop - start
    raise AssertionError("TIMER_SLOTS steht nicht mehr in profiles.py")


def _section(text: str, heading: str) -> str:
    """Der Rumpf eines ``##``-Abschnitts bis zur nächsten Überschrift."""
    lines = text.splitlines()
    assert heading in lines, f"Überschrift fehlt: {heading!r}"
    start = lines.index(heading) + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def _leading_quote(body: str) -> str:
    """Der Blockzitat-Block am Anfang eines Abschnitts, sonst ``""``."""
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    quote: list[str] = []
    for line in lines:
        if not line.startswith(">"):
            break
        quote.append(line.lstrip("> ").rstrip())
    return "\n".join(quote)


def _example_entity_ids() -> list[tuple[str, str]]:
    """Alle Entity-IDs aus der Beispiel-YAML, als (Domain, Objekt-ID).

    Nur die Werte von ``entity``, ``entity_id`` und ``operation_entity``:
    ``button.press`` unter ``perform_action`` ist ein Dienst, keine Entität.
    """
    ids: list[tuple[str, str]] = []
    pattern = re.compile(r"^\s*(?:entity|entity_id|operation_entity):\s*(\S+)\s*$")
    for line in LOVELACE_YAML.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match and "." in match.group(1):
            domain, _, object_id = match.group(1).partition(".")
            ids.append((domain, object_id))
    assert ids, "keine Entity-IDs in der Beispiel-YAML gefunden"
    return ids


def _sentences(text: str) -> list[str]:
    """Grobe Satztrennung -- genug, um eine Aussage zu isolieren."""
    return [s.strip() for s in re.split(r"(?<=[.!?:])\s+|\n\n+|\n[-*|]", text) if s.strip()]


def test_every_document_the_docs_point_at_exists() -> None:
    """Kein Verweis ins Leere, weder als Link noch in Backticks."""
    link = re.compile(r"\]\(([^)\s]+)\)")
    # In Backticks steht oft nur ein Kurzname als Linktext. Als Pfad gilt
    # deshalb nur, was von der Wurzel des Repos aus geschrieben ist.
    backtick = re.compile(r"`((?:docs|examples|dumps|tests|tools)/[^`\s]+\.(?:md|yaml))`")
    missing: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for target in sorted(set(link.findall(text))):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = target.split("#", 1)[0]
            if target and not (path.parent / target).exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
        for target in sorted(set(backtick.findall(text))):
            if not (ROOT / target).exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not missing, missing


def test_the_user_guide_points_at_the_fork_upgrade_note() -> None:
    """Wer die Anleitung liest, muss vom Umstieg erfahren, bevor er koppelt."""
    guide = (DOCS / "user-guide.md").read_text(encoding="utf-8")
    assert "upgrading-fork.md" in guide, "user-guide.md verweist nicht auf den Umstieg"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "upgrading-fork.md" in readme, "README verweist nicht auf den Umstieg"
    assert UPGRADE_NOTE.exists(), f"{UPGRADE_NOTE} fehlt"


def test_the_upgrade_note_names_what_the_code_offers() -> None:
    """Die Tabelle im Umstieg nennt echte Entitäten, keine erhofften."""
    note = UPGRADE_NOTE.read_text(encoding="utf-8")

    # Genau die Entitäten, die der Hinweis als neu oder umbenannt verkauft.
    claimed = [
        ("binary_sensor", "heating_active"),
        ("sensor", "heating_status"),
        ("binary_sensor", "error"),
        ("sensor", "error_code"),
        ("button", "error_reset"),
        ("switch", "timer"),
        ("sensor", "timer_schedule"),
        ("sensor", "panel_clock"),
        ("sensor", "ble_free_slots"),
        ("binary_sensor", "line_power"),
        ("number", "panel_brightness"),
    ]
    for platform, key in claimed:
        name = _entity_name("de", platform, key)
        assert name in note, f"{platform}.{key} heißt {name!r}, der Hinweis nennt das nicht"

    # Die Zahl muss VOR dem Wort "Timer" stehen, nicht irgendwo im Text: der
    # Hinweis erwähnt auch vier Bluetooth-Kopplungen, und eine freie Suche
    # nach "vier" hielte deshalb jede falsche Slot-Zahl für belegt.
    slots = _timer_slot_count()

    def counted(number: int) -> bool:
        pattern = rf"\b({GERMAN_NUMBERS[number]}|{number})\b[^.\n]{{0,20}}Timer"
        return re.search(pattern, note) is not None

    assert counted(slots), f"{slots} Timer-Slots stehen nicht im Hinweis"
    wrong = [n for n in GERMAN_NUMBERS if n != slots and counted(n)]
    assert not wrong, f"der Hinweis nennt auch {wrong} Timer-Slots"

    version = _upstream_version()
    assert version in note, f"Zielversion {version} steht nicht im Hinweis"


def test_the_docs_call_the_controls_by_their_real_names() -> None:
    """Wer im Code umbenennt, muss die Doku mitnehmen.

    Geprüft wird je Sprachfassung, weil beide Fassungen eigene Namen haben
    und nur eine davon zu pflegen der übliche Fehler ist.
    """
    guide_controls = [
        ("select", "energy_source"),
        ("select", "electric_level"),
        ("select", "water_mode"),
        ("sensor", "operation"),
        ("button", "manual_sync"),
        ("button", "manual_stop"),
        ("number", "manual_live_minutes"),
        ("binary_sensor", "connection"),
        ("binary_sensor", "proxy_connection"),
    ]
    # Das Lovelace-Beispiel beschreibt nur die Bedienelemente auf der Karte.
    lovelace_controls = [
        ("select", "energy_source"),
        ("select", "electric_level"),
        ("sensor", "operation"),
        ("button", "manual_stop"),
        ("binary_sensor", "connection"),
        ("binary_sensor", "proxy_connection"),
    ]
    documents = [
        (DOCS / "user-guide.md", '<a id="english"></a>', guide_controls),
        (LOVELACE_README, '<a id="english-version"></a>', lovelace_controls),
    ]
    for path, marker, controls in documents:
        text = path.read_text(encoding="utf-8")
        german, sep, english = text.partition(marker)
        assert sep, f"{path.name}: die Sprachmarke {marker} fehlt"
        for platform, key in controls:
            for lang, part in (("de", german), ("en", english)):
                name = _entity_name(lang, platform, key)
                assert name in part, (
                    f"{path.name} ({lang}): {platform}.{key} heißt {name!r}, "
                    "so steht es dort nicht"
                )


def test_no_document_stretches_an_entity_name_into_one_that_is_not() -> None:
    """Fett heißt hier: so steht es in Home Assistant.

    Ein angehängtes Wort erfindet einen Namen, den es nicht gibt -- so ist
    aus "Electric heating" einmal "Electric heating output" geworden, und
    wer danach in der Entitätenliste sucht, findet nichts. Dasselbe leistet
    ein ausgetauschtes Wort gleicher Länge: "Elektrische Heizleistung"
    neben "Elektrische Heizung". Geprüft wird nur Fettdruck: "schalten
    Warmwasser ein" ist ein Satz, keine Behauptung über einen Namen, und
    der alte Name in der Umstiegstabelle ("Flamme") teilt mit keinem
    heutigen das erste Wort.
    """
    names = set()
    for lang in ("de", "en"):
        for platform, keys in _translations(lang).items():
            for entry in keys.values():
                if entry.get("name"):
                    names.add(entry["name"].replace("{slot}", "").strip())

    invented: list[str] = []
    for path in (DOCS / "user-guide.md", LOVELACE_README, UPGRADE_NOTE):
        text = path.read_text(encoding="utf-8")
        for raw in re.findall(r"\*\*(.+?)\*\*", text, re.S):
            phrase = " ".join(raw.split()).strip(" .,:;")
            if phrase in names:
                continue
            words = phrase.split()
            near = [
                n
                for n in names
                if phrase.startswith(n)
                or (n.split()[:1] == words[:1] and len(n.split()) == len(words))
            ]
            if near:
                invented.append(f"{path.name}: {phrase!r} statt {near[0]!r}")
    assert not invented, invented


def test_the_lovelace_note_leads_with_the_unpredictable_ids() -> None:
    """Die Warnung steht vor den Platzhaltern, nicht hinter ihnen."""
    text = LOVELACE_README.read_text(encoding="utf-8")
    # Woher die ID kommt, dass unten nur ein Beispiel steht, und dass dieses
    # Beispiel nichts vorgibt -- die drei Aussagen, ohne die der Hinweis
    # wieder als Bauanleitung gelesen wird.
    sections = [
        ("## Entity-IDs anpassen", ("Busadresse", "Beispiele", "keine Vorgabe")),
        ("## Replace the entity prefix", ("bus address", "examples", "not a specification")),
    ]
    for heading, required in sections:
        body = _section(text, heading)
        quote = _leading_quote(body)
        assert quote, f"{heading}: der Abschnitt beginnt nicht mit einem Warnhinweis"
        # Umbrüche sind Satzsache, nicht Aussage.
        flat = " ".join(quote.split())
        for word in required:
            assert word in flat, f"{heading}: {word!r} fehlt im Warnhinweis"
        # Der Platzhalter darf erst nach dem Hinweis auftauchen.
        placeholder = _placeholder_prefix()
        if placeholder in body:
            quote_end = body.index(quote.splitlines()[-1])
            assert body.index(placeholder) > quote_end, (
                f"{heading}: der Platzhalter steht vor dem Warnhinweis"
            )


def _placeholder_prefix() -> str:
    """Der eine Platzhalter-Präfix, den alle Beispiel-IDs teilen."""
    object_ids = [object_id for _, object_id in _example_entity_ids()]
    prefix = min(object_ids, key=len)
    for object_id in object_ids:
        assert object_id == prefix or object_id.startswith(prefix + "_"), (
            f"{object_id} trägt nicht den Präfix {prefix} -- eine globale "
            "Ersetzung würde ihn stehen lassen"
        )
    return prefix


def test_every_example_entity_id_is_one_the_integration_can_produce() -> None:
    """Jede Beispiel-ID muss sich auf eine heutige Entität zurückführen lassen.

    Der Slug einer Entität entsteht aus ihrem übersetzten Namen, und welche
    Sprache bei der Anlage aktiv war, entscheidet der Benutzer -- deshalb
    zählt jeder Übersetzungsschlüssel und jeder Name aus beiden Sprachen als
    gültig. Was in keiner davon vorkommt, gibt es nicht mehr.
    """
    known: dict[str, set[str]] = {}
    for lang in ("de", "en"):
        for platform, keys in _translations(lang).items():
            slugs = known.setdefault(platform, set())
            for key, entry in keys.items():
                slugs.add(key)
                if entry.get("name"):
                    slugs.add(_slugify(entry["name"].replace("{slot}", "")))

    prefix = _placeholder_prefix()
    stale: list[str] = []
    for domain, object_id in _example_entity_ids():
        if object_id == prefix:
            # Die Klimaentität trägt nur den Gerätenamen (``_attr_name = None``).
            assert domain == "climate", f"{domain}.{object_id} hat keinen Namensteil"
            continue
        suffix = object_id[len(prefix) + 1 :]
        if suffix not in known.get(domain, set()):
            stale.append(f"{domain}.…_{suffix}")
    assert not stale, f"Beispiel nennt Entitäten, die es nicht mehr gibt: {sorted(set(stale))}"


def test_no_document_claims_the_diesel_switch_is_gone() -> None:
    """Solange der Code den Schalter anbietet, darf die Doku ihn nicht beerdigen."""
    switches = _translations("de")["switch"]
    assert "diesel" in switches, "Vorbedingung entfallen: switch.diesel gibt es nicht mehr"
    assert "DieselLevel" in (SRC / "profiles.py").read_text(encoding="utf-8")

    offenders: list[str] = []
    for path in _markdown_files():
        for sentence in _sentences(path.read_text(encoding="utf-8")):
            if "diesel" in sentence.lower() and GONE_WORDS.search(sentence):
                offenders.append(f"{path.relative_to(ROOT)}: {sentence}")
    assert not offenders, offenders


def test_the_transport_doc_mentions_the_probe_exception() -> None:
    """``probe`` hebt die Regel auf, die dort als ausnahmslos beschrieben ist."""
    source = (SRC / "ble.py").read_text(encoding="utf-8")
    if "probe: bool = False" not in source:
        return  # Die Ausnahme gibt es nicht mehr; dann gehört sie auch nicht in die Doku.
    assert "not success and not probe" in source, "probe wirkt nicht mehr auf die Sitzung"

    text = (DOCS / "ble-transport.md").read_text(encoding="utf-8")
    german, _, english = text.partition("## English")
    assert "probe" in german, "der deutsche Teil erwähnt die probe-Ausnahme nicht"
    assert "probe" in english, "der englische Teil erwähnt die probe-Ausnahme nicht"


def _main() -> None:
    stubs.run_tests(globals(), "Docs consistency")


if __name__ == "__main__":
    _main()
