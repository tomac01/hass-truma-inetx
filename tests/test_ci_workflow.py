#!/usr/bin/env python3
"""Prüft, ob die CI wirklich jede Testdatei im Repo ausführt.

Warum das hier steht: Der Testschritt in ``validate.yml`` läuft per Glob über
``tests/test_*.py`` und überspringt dabei eine Liste ``needs_library``. Diese
Konstruktion entstand, nachdem fünf Tests eine ganze Beta-Serie lang nie
gelaufen waren, weil sie niemand in den Workflow eingetragen hatte. Der Glob
schließt diese Lücke aber nur für Python-Dateien ohne Drittbibliothek. Zwei
Löcher bleiben, und beide schweigen:

1. Eine Datei auf der ``needs_library``-Liste, für die niemand einen eigenen
   ``run``-Schritt anlegt, läuft **nirgends**. Der Glob überspringt sie, und
   einen eigenen Schritt gibt es nicht -- die CI wird grün, ohne sie je
   angefasst zu haben.
2. Ein Test, der nicht unter Python läuft -- ``test_operation_card.cjs`` --
   wird vom Python-Glob nie erfasst und braucht zwingend einen eigenen
   Node-Schritt.

Die umgekehrte Richtung ist ungefährlich und wird hier deshalb nicht
erzwungen: Wer eine Datei auf der Liste vergisst, deren Test die Bibliothek
braucht, bekommt im nackten Schritt einen lauten ``ImportError``. Nur was
direkt ``import cbor2``/``import voluptuous`` schreibt, prüfen wir vorab --
das ist statisch sicher erkennbar und spart einen CI-Durchlauf.

Was der Test festnagelt:

1. der nackte Schritt globt weiterhin, statt Dateien aufzuzählen, startet die
   gefundenen Dateien auch wirklich, behält ihren Fehlschlag und bricht ab,
   wenn der Glob nichts trifft; kein Schritt trägt ``continue-on-error``,
2. jede Datei ``tests/test_*.py`` läuft entweder im Glob oder in einem eigenen
   Schritt, jede Datei ``tests/test_*.cjs`` in einem eigenen Node-Schritt,
3. die ``needs_library``-Liste und die eigenen Python-Schritte nennen genau
   dieselben Dateien -- eine Datei mehr auf der Liste heißt: läuft nie, eine
   Datei mehr an Schritten heißt: läuft zweimal, davon einmal nackt,
4. jeder Eintrag der Liste greift auch wirklich, gemessen an der
   Teilstring-Logik der Shell (``case "$needs_library" in *"$base"*``) -- ein
   Tippfehler oder ein abgeschnittener Name steht sonst wirkungslos da,
5. jede Datei mit direktem ``import cbor2``/``import voluptuous`` steht auf
   der Liste,
6. der nackte Schritt und der Node-Schritt laufen vor dem ersten
   ``pip install`` -- nur so beweist der Durchlauf, dass sie ohne
   Drittbibliothek auskommen,
7. jeder eigene Python-Schritt läuft nach einem ``pip install``, und wer
   ``cbor2`` importiert, nach dem ``pip install`` von ``cbor2``.

Nicht abgedeckt: ob ein Test inhaltlich etwas prüft. Hier geht es allein
darum, dass er überhaupt gestartet wird.

Run: ``python3 tests/test_ci_workflow.py``
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"

# Bibliotheken, die der nackte Schritt nicht hat.
THIRD_PARTY = ("cbor2", "voluptuous")

RUN_PY = re.compile(r"python3 tests/(test_[A-Za-z0-9_]+\.py)")
RUN_NODE = re.compile(r"\bnode tests/(test_[A-Za-z0-9_]+\.cjs)")
PIP_INSTALL = re.compile(r"pip install ([^\n]+)")
# Der Glob-Schritt: daran erkennen wir ihn, und genau das soll er bleiben.
GLOB_LOOP = "for path in tests/test_*.py"


def _tests_job() -> str:
    """Der Block des Jobs ``tests`` aus dem Workflow, ohne YAML-Kommentare.

    YAML-Kommentare stehen auf Einrückung 6 oder weniger, die Shell-Zeilen in
    den ``run``-Blöcken auf 10 oder mehr. Die Kommentare fliegen raus, damit
    ein erklärender Satz über einen Schritt nicht als Schritt gelesen wird.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^  tests:$\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match, "der Job 'tests' steht nicht mehr in validate.yml"
    body = match.group(1)
    return "\n".join(
        line for line in body.splitlines() if not re.match(r"^ {0,8}#", line)
    )


def _steps() -> list[str]:
    """Die Schritte des Jobs in ihrer Reihenfolge, je als Textblock."""
    steps: list[list[str]] = []
    for line in _tests_job().splitlines():
        if re.match(r"^      - ", line):
            steps.append([line])
        elif steps:
            steps[-1].append(line)
    assert steps, "der Job 'tests' hat keine Schritte mehr"
    return ["\n".join(step) for step in steps]


def _glob_step_index(steps: list[str]) -> int:
    matches = [i for i, step in enumerate(steps) if GLOB_LOOP in step]
    assert len(matches) == 1, (
        f"genau ein Schritt soll über {GLOB_LOOP!r} globen, gefunden: {len(matches)}"
    )
    return matches[0]


def _needs_library_block(steps: list[str]) -> str:
    """Der rohe Inhalt der Shell-Variablen ``needs_library``.

    Roh, weil die Shell darauf einen Teilstring-Vergleich fährt: Für die Frage,
    ob eine Datei übersprungen wird, zählt genau dieser Text.
    """
    step = steps[_glob_step_index(steps)]
    match = re.search(r'needs_library="\n(.*?)"', step, re.S)
    assert match, "die Liste needs_library steht nicht mehr im Glob-Schritt"
    return match.group(1)


def _declared_skips(steps: list[str]) -> list[str]:
    block = _needs_library_block(steps)
    return [line.strip() for line in block.splitlines() if line.strip()]


def _explicit_python_runs(steps: list[str]) -> list[tuple[int, str]]:
    """``(Schrittindex, Dateiname)`` je Schritt, der eine Datei namentlich startet."""
    glob_at = _glob_step_index(steps)
    found: list[tuple[int, str]] = []
    for index, step in enumerate(steps):
        if index == glob_at:
            continue
        for name in RUN_PY.findall(step):
            found.append((index, name))
    return found


def _node_runs(steps: list[str]) -> list[tuple[int, str]]:
    return [
        (index, name)
        for index, step in enumerate(steps)
        for name in RUN_NODE.findall(step)
    ]


def _pip_installs(steps: list[str]) -> list[tuple[int, str]]:
    return [
        (index, packages)
        for index, step in enumerate(steps)
        for packages in PIP_INSTALL.findall(step)
    ]


def _python_test_files() -> list[str]:
    return sorted(path.name for path in TESTS.glob("test_*.py"))


def _node_test_files() -> list[str]:
    return sorted(path.name for path in TESTS.glob("test_*.cjs"))


def _direct_imports(name: str) -> set[str]:
    """Die Drittbibliotheken, die ``name`` selbst importiert."""
    tree = ast.parse((TESTS / name).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    return imported & set(THIRD_PARTY)


def test_the_bare_step_still_globs_instead_of_listing_files() -> None:
    """Der Glob ist der Grund, warum ein neuer Test nicht vergessen wird."""
    steps = _steps()
    step = steps[_glob_step_index(steps)]
    # Ein Glob, der jede Datei findet und dann nur ihren Namen ausgibt, wäre
    # grün und wertlos -- die Schleife muss die Datei auch starten.
    assert re.search(r'python3 "\$path"', step), (
        "die Glob-Schleife startet die gefundenen Dateien nicht mehr mit python3"
    )
    # Und sie muss den Fehlschlag behalten: ohne das hier ist der Schritt
    # immer grün, weil der Exit-Code der letzten Datei zählt.
    assert re.search(r'python3 "\$path" \|\| status=1', step), (
        "ein Fehlschlag im Glob-Schritt wird nicht mehr in status gemerkt"
    )
    assert "exit $status" in step, "der Glob-Schritt gibt status nicht mehr weiter"


def test_no_step_is_allowed_to_fail_quietly() -> None:
    """``continue-on-error`` macht aus einem roten Test eine grüne CI."""
    for step in _steps():
        assert "continue-on-error" not in step, (
            "ein Schritt im Job 'tests' darf fehlschlagen, ohne den Job rot zu "
            f"machen:\n{step}"
        )


def test_the_bare_step_fails_when_the_glob_matches_nothing() -> None:
    """Ohne diese Bremse meldet ein leerer Glob Erfolg, ohne etwas zu testen."""
    steps = _steps()
    step = steps[_glob_step_index(steps)]
    assert re.search(r'\[\s*"\$ran"\s+-eq\s+0\s*\]', step), (
        "die Absicherung gegen einen leeren Glob fehlt im nackten Schritt"
    )
    assert "exit 1" in step, "der leere Glob bricht nicht mehr ab"


def test_every_python_test_runs_somewhere() -> None:
    """Keine ``.py``-Datei darf zwischen Glob und Einzelschritten durchfallen."""
    steps = _steps()
    block = _needs_library_block(steps)
    explicit = {name for _, name in _explicit_python_runs(steps)}
    for name in _python_test_files():
        skipped = name in block  # dieselbe Teilstring-Logik wie die Shell
        assert not skipped or name in explicit, (
            f"{name} steht auf needs_library, hat aber keinen eigenen Schritt "
            "-- die Datei läuft in der CI nirgends"
        )


def test_every_node_test_has_its_own_step() -> None:
    """Ein Node-Test wird vom Python-Glob nie erfasst."""
    steps = _steps()
    started = {name for _, name in _node_runs(steps)}
    for name in _node_test_files():
        assert name in started, (
            f"{name} wird in validate.yml von keinem Schritt gestartet "
            "-- der Python-Glob sieht eine .cjs-Datei nicht"
        )


def test_every_started_test_file_exists() -> None:
    """Ein Schritt auf eine gelöschte Datei macht den Job rot, nicht sicherer."""
    steps = _steps()
    for _, name in _explicit_python_runs(steps) + _node_runs(steps):
        assert (TESTS / name).is_file(), f"validate.yml startet {name}, die es nicht gibt"


def test_the_skip_list_matches_the_explicit_python_steps() -> None:
    """Liste und Einzelschritte sind zwei Hälften derselben Entscheidung."""
    steps = _steps()
    declared = set(_declared_skips(steps))
    explicit = {name for _, name in _explicit_python_runs(steps)}
    assert declared == explicit, (
        "needs_library und die eigenen Python-Schritte nennen nicht dieselben "
        f"Dateien: nur auf der Liste {sorted(declared - explicit)}, "
        f"nur als Schritt {sorted(explicit - declared)}"
    )


def test_every_skip_list_entry_actually_skips_a_file() -> None:
    """Die Shell vergleicht Teilstrings; ein halber Name steht wirkungslos da."""
    steps = _steps()
    block = _needs_library_block(steps)
    declared = set(_declared_skips(steps))
    effective = {name for name in _python_test_files() if name in block}
    assert declared == effective, (
        "needs_library greift nicht so, wie sie gelesen wird: "
        f"ohne Wirkung {sorted(declared - effective)}, "
        f"unerwartet übersprungen {sorted(effective - declared)}"
    )


def test_tests_that_import_a_library_are_on_the_skip_list() -> None:
    """Sonst startet der nackte Schritt sie und stirbt am ImportError."""
    steps = _steps()
    block = _needs_library_block(steps)
    for name in _python_test_files():
        needed = _direct_imports(name)
        if not needed:
            continue
        assert name in block, (
            f"{name} importiert {sorted(needed)} und gehört auf needs_library"
        )


def test_the_bare_stage_runs_before_the_first_install() -> None:
    """Nackt heißt nackt -- sobald pip gelaufen ist, beweist der Schritt nichts."""
    steps = _steps()
    installs = _pip_installs(steps)
    if not installs:
        return  # Keine Installation, keine Reihenfolge zu wahren.
    first_install = min(index for index, _ in installs)
    assert _glob_step_index(steps) < first_install, (
        "der nackte Schritt läuft nach einem pip install und ist damit nicht nackt"
    )
    for index, name in _node_runs(steps):
        assert index < first_install, (
            f"{name} läuft nach einem pip install; der Node-Test braucht kein pip "
            "und gehört in die nackte Phase"
        )


def test_each_library_test_runs_after_its_install() -> None:
    """Ein Einzelschritt vor seiner Installation ist nur ein langsamer Fehlschlag."""
    steps = _steps()
    installs = _pip_installs(steps)
    for index, name in _explicit_python_runs(steps):
        earlier = [packages for at, packages in installs if at < index]
        assert earlier, (
            f"{name} läuft als eigener Schritt, aber vor jedem pip install "
            "-- dann hätte der Glob ihn genauso gut nehmen können"
        )
        for library in _direct_imports(name):
            assert any(library in packages for packages in earlier), (
                f"{name} importiert {library}, aber kein pip install davor nennt es"
            )


def _main() -> None:
    stubs.run_tests(globals(), "CI workflow")


if __name__ == "__main__":
    _main()
