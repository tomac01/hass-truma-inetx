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

1. der nackte Schritt globt weiterhin, statt Dateien aufzuzählen -- und das
   nicht nur dem Text nach: sein ``run``-Skript wird aus der YAML gezogen und
   in einem Wegwerf-Verzeichnis gegen Attrappen ausgeführt. Es muss die
   gefundenen Dateien wirklich starten, die Liste ``needs_library`` wirklich
   überspringen, einen Fehlschlag wirklich durchreichen und wirklich abbrechen,
   wenn der Glob nichts trifft,
2. weder ein Schritt noch der Job selbst trägt ``continue-on-error`` oder
   ``if:``. Beides macht aus einem roten Test eine grüne CI, ``if:`` sogar,
   ohne den Test überhaupt zu starten; auf Job-Ebene hebelt es alle Schritte
   auf einmal aus,
3. ein Schritt, der eine einzelne Datei startet, ist genau dieser Aufruf:
   ``run: python3 tests/<datei>.py`` bzw. ``run: node tests/<datei>.cjs``.
   Kein ``echo`` davor, kein ``|| true`` dahinter -- sonst stünde der Schritt
   da, ohne je rot werden zu können,
4. jede Datei ``tests/test_*.py`` läuft entweder im Glob oder in einem eigenen
   Schritt, jede Datei ``tests/test_*.cjs`` in einem eigenen Node-Schritt,
5. die ``needs_library``-Liste und die eigenen Python-Schritte nennen genau
   dieselben Dateien -- eine Datei mehr auf der Liste heißt: läuft nie, eine
   Datei mehr an Schritten heißt: läuft zweimal, davon einmal nackt,
6. jeder Eintrag der Liste greift auch wirklich, gemessen an der
   Teilstring-Logik der Shell (``case "$needs_library" in *"$base"*``) -- ein
   Tippfehler oder ein abgeschnittener Name steht sonst wirkungslos da,
7. jede Datei mit direktem ``import cbor2``/``import voluptuous`` steht auf
   der Liste,
8. der nackte Schritt und der Node-Schritt laufen vor dem ersten
   ``pip install`` -- nur so beweist der Durchlauf, dass sie ohne
   Drittbibliothek auskommen,
9. jeder eigene Python-Schritt läuft nach einem ``pip install``, und wer
   ``cbor2`` importiert, nach dem ``pip install`` von ``cbor2``,
10. der Workflow schränkt die Rechte des ``GITHUB_TOKEN`` auf Lesen ein: ein
    ``permissions``-Block steht auf oberster Ebene und gibt ``contents: read``
    her, und kein Block im Workflow -- oben oder je Job -- vergibt mehr als
    ``read``. Fehlt der Block, läuft jeder Job wieder mit dem Repo-Standard,
    und das fällt sonst nirgends auf: Die CI bleibt grün, weil kein Schritt
    die zusätzlichen Rechte braucht.

Nicht abgedeckt: ob ein Test inhaltlich etwas prüft. Hier geht es allein
darum, dass er überhaupt gestartet wird.

Run: ``python3 tests/test_ci_workflow.py``
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"

# Bibliotheken, die der nackte Schritt nicht hat.
THIRD_PARTY = ("cbor2", "voluptuous")

# Ein Einzelschritt startet eine Datei nur dann, wenn die ganze ``run``-Zeile
# aus genau diesem Aufruf besteht. Ein Teilstring-Treffer würde ``echo python3
# tests/x.py`` und ``python3 tests/x.py || true`` als "läuft" durchgehen lassen
# -- beides ist immer grün und prüft nichts.
RUN_PY = re.compile(r"^ *(?:- )?run: python3 tests/(test_[A-Za-z0-9_]+\.py) *$", re.M)
RUN_NODE = re.compile(r"^ *(?:- )?run: node tests/(test_[A-Za-z0-9_]+\.cjs) *$", re.M)
# Und hier die Gegenrichtung: jede Erwähnung einer Testdatei, wie verpackt auch
# immer. Was erwähnt, aber nicht sauber gestartet wird, fällt damit auf.
NAMES_A_TEST_FILE = re.compile(r"tests/(test_[A-Za-z0-9_]+\.(?:py|cjs))")
PIP_INSTALL = re.compile(r"pip install ([^\n]+)")
# Schlüssel auf Schritt- bzw. Job-Ebene -- Einrückung 8 bzw. 4. Die Shell-Zeilen
# in den ``run``-Blöcken stehen auf 10 und werden davon nicht erfasst.
STEP_KEY = re.compile(r"^(?:      - |        )([A-Za-z0-9_-]+):", re.M)
JOB_KEY = re.compile(r"^    ([A-Za-z0-9_-]+):", re.M)
# Schlüssel, die einen Schritt oder Job lautlos wirkungslos machen.
MUFFLERS = frozenset({"continue-on-error", "if"})
# Der Glob-Schritt: daran erkennen wir ihn, und genau das soll er bleiben.
GLOB_LOOP = "for path in tests/test_*.py"

# Ein Rechte-Eintrag, der nichts erlaubt, was über Lesen hinausgeht. ``write``
# steht hier bewusst nicht als Verbot, sondern ``read``/``none`` als einzige
# Erlaubnis: ``write-all``, ``read-all`` und jeder Tippfehler fallen damit auf.
PERMISSION_LINE = re.compile(r"^[a-z][a-z-]*: (?:read|none)$")

# Attrappen für den Verhaltenstest des Glob-Schritts. Die grüne schreibt ihren
# Namen mit, damit sich belegen lässt, dass sie wirklich gelaufen ist.
DUMMY_GREEN = (
    "import pathlib, sys\n"
    "with pathlib.Path('ran.txt').open('a') as fh:\n"
    "    fh.write(pathlib.Path(sys.argv[0]).name + '\\n')\n"
)
DUMMY_RED = "raise SystemExit(1)\n"


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


def _without_comments(text: str) -> str:
    """Ohne reine Kommentarzeilen -- auch die im Inneren eines ``run``-Blocks."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _run_script(step: str) -> str:
    """Das Shell-Skript aus dem ``run: |``-Block eines Schritts, ausgerückt."""
    lines = step.splitlines()
    start = next(
        (i + 1 for i, line in enumerate(lines) if re.match(r"^ *(?:- )?run: \|\s*$", line)),
        None,
    )
    assert start is not None, f"dieser Schritt hat keinen 'run: |'-Block:\n{step}"
    body = lines[start:]
    indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    assert indents, f"der 'run: |'-Block ist leer:\n{step}"
    pad = min(indents)
    return "\n".join(line[pad:] if line.strip() else "" for line in body) + "\n"


def _run_glob_script(files: dict[str, str]) -> tuple[int, str, list[str]]:
    """Das Skript des Glob-Schritts gegen ``files`` laufen lassen.

    Der Verhaltenstest, den kein Textvergleich ersetzt: ``status=0`` kurz vor
    ``exit $status`` oder ein ``if false; then ... fi`` um den Aufruf lassen
    jede Zeile, auf die wir sonst prüfen, wörtlich stehen -- der Schritt endet
    trotzdem grün. Also führen wir ihn aus.

    Gibt ``(Exit-Code, Ausgabe, Namen der wirklich gelaufenen Dateien)`` zurück.
    """
    steps = _steps()
    script = _run_script(steps[_glob_step_index(steps)])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "tests").mkdir()
        for name, body in files.items():
            (work / "tests" / name).write_text(body, encoding="utf-8")
        (work / "step.sh").write_text(script, encoding="utf-8")
        # Wie GitHub den Standard-Shell-Schritt startet.
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "step.sh"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        trace = work / "ran.txt"
        ran = sorted(trace.read_text(encoding="utf-8").split()) if trace.is_file() else []
    return result.returncode, result.stdout + result.stderr, ran


def _pip_installs(steps: list[str]) -> list[tuple[int, str]]:
    return [
        (index, packages)
        for index, step in enumerate(steps)
        for packages in PIP_INSTALL.findall(step)
    ]


def _permission_blocks() -> list[tuple[str, str]]:
    """``(Ort, Inhalt)`` für jeden ``permissions``-Schlüssel im Workflow.

    Der Ort ist die Einrückung: 0 heißt oberste Ebene und gilt damit für jeden
    Job, 4 heißt Job-Ebene und überschreibt sie für diesen einen Job. Der
    Inhalt ist der Kurzwert hinter dem Doppelpunkt (``{}``, ``read-all``,
    ``write-all``) oder, wenn dort nichts steht, die eingerückten Zeilen
    darunter.
    """
    lines = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    blocks: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^( *)permissions:(.*)$", line)
        if not match:
            continue
        indent, inline = match.group(1), match.group(2).strip()
        where = "oberste Ebene" if not indent else f"Einrückung {len(indent)}"
        if inline:
            blocks.append((where, inline))
            continue
        body: list[str] = []
        for follow in lines[index + 1 :]:
            if not follow.strip():
                continue
            if len(follow) - len(follow.lstrip()) <= len(indent):
                break
            body.append(follow.strip())
        blocks.append((where, "\n".join(body)))
    return blocks


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


def test_the_glob_step_really_starts_what_it_finds() -> None:
    """Der Textvergleich oben sieht nur, dass der Aufruf dasteht."""
    code, output, ran = _run_glob_script(
        {"test_zz_first.py": DUMMY_GREEN, "test_zz_second.py": DUMMY_GREEN}
    )
    assert ran == ["test_zz_first.py", "test_zz_second.py"], (
        "der Glob-Schritt hat die gefundenen Dateien nicht gestartet, gelaufen "
        f"sind {ran}:\n{output}"
    )
    assert code == 0, (
        f"der Glob-Schritt wird rot, obwohl jede Datei grün ist:\n{output}"
    )


def test_the_glob_step_starts_every_real_test_file() -> None:
    """Dieselbe Probe, aber mit den Namen, die wirklich in tests/ liegen.

    Der Test darüber speist Kunstnamen ein (``test_zz_*.py``). Eine zusätzliche
    Skip-Klausel im Schleifenrumpf, die auf einen ECHTEN Dateinamen gemünzt ist,
    bleibt darin unsichtbar -- und genau die wäre der stille Ausfall, gegen den
    dieser Test überhaupt existiert: eine Datei liefe nirgends mehr, und die CI
    bliebe grün. Also einmal mit Attrappen unter den echten Namen laufen und
    nachzählen.

    Die Attrappen sind grün und tun nichts: geprüft wird, WELCHE Dateien der
    Schritt startet, nicht was sie tun.
    """
    steps = _steps()
    skipped = set(_declared_skips(steps))
    real = sorted(path.name for path in (ROOT / "tests").glob("test_*.py"))
    assert real, "in tests/ liegt keine einzige Testdatei -- der Glob wäre sinnlos"

    code, output, ran = _run_glob_script({name: DUMMY_GREEN for name in real})

    expected = sorted(name for name in real if name not in skipped)
    assert ran == expected, (
        "der Glob-Schritt startet nicht genau die Dateien ohne Drittbibliothek.\n"
        f"erwartet: {expected}\n"
        f"gelaufen: {ran}\n"
        f"nicht gelaufen: {sorted(set(expected) - set(ran))}\n"
        f"zusätzlich gelaufen: {sorted(set(ran) - set(expected))}\n{output}"
    )
    assert code == 0, f"der Schritt wird rot, obwohl jede Attrappe grün ist:\n{output}"


def test_the_glob_step_really_goes_red_when_a_file_goes_red() -> None:
    """Die Probe, die ``status=0`` vor ``exit $status`` nicht überlebt.

    Dieselbe Probe fängt ein ``if false; then python3 "$path" ...; fi``: Alle
    Zeilen, auf die der Textvergleich prüft, stehen dann noch wörtlich da, der
    Schritt endet aber mit 0.
    """
    code, output, _ = _run_glob_script(
        {"test_zz_green.py": DUMMY_GREEN, "test_zz_red.py": DUMMY_RED}
    )
    assert code != 0, (
        "eine fehlgeschlagene Testdatei macht den Glob-Schritt nicht rot -- die "
        f"CI wird grün, obwohl ein Test kaputt ist:\n{output}"
    )


def test_the_glob_step_really_goes_red_with_nothing_to_run() -> None:
    """Ein leeres ``tests/`` darf keinen Erfolg melden."""
    code, output, _ = _run_glob_script({})
    assert code != 0, (
        "der Glob-Schritt meldet Erfolg, ohne eine einzige Datei getestet zu "
        f"haben:\n{output}"
    )


def test_the_glob_step_really_goes_red_when_the_skip_list_swallows_everything() -> None:
    """Die zweite Hälfte der Bremse gegen einen Durchlauf, der nichts testet.

    Ein leerer Glob fällt schon deshalb auf, weil die Shell das Muster
    unverändert an ``python3`` reicht. Verschluckt dagegen die Liste jede
    gefundene Datei, läuft die Schleife sauber durch und nur der Zähler merkt
    es -- ein ``ran=1`` als Startwert macht die Bremse lautlos wirkungslos.
    Hier liegen darum ausschließlich Dateien der Liste, alle davon grün: Rot
    werden kann der Schritt dann nur noch über den Zähler.
    """
    declared = _declared_skips(_steps())
    assert declared, "die Liste needs_library ist leer"
    code, output, ran = _run_glob_script(dict.fromkeys(declared, DUMMY_GREEN))
    assert ran == [], f"die Liste needs_library greift nicht mehr, gelaufen: {ran}"
    assert code != 0, (
        "der Glob-Schritt meldet Erfolg, obwohl die Liste needs_library jede "
        f"gefundene Datei übersprungen hat:\n{output}"
    )


def test_the_glob_step_really_skips_the_library_tests() -> None:
    """Die Kehrseite: Was auf der Liste steht, darf hier nicht nackt starten.

    Jede Datei der Liste liegt als Attrappe bereit, die sofort fehlschlägt.
    Greift die ``case``-Logik, läuft keine davon und der Schritt bleibt grün.
    """
    declared = _declared_skips(_steps())
    assert declared, "die Liste needs_library ist leer"
    files = dict.fromkeys(declared, DUMMY_RED)
    files["test_zz_green.py"] = DUMMY_GREEN
    code, output, ran = _run_glob_script(files)
    assert ran == ["test_zz_green.py"], (
        "der Glob-Schritt startet Dateien der Liste needs_library nackt, "
        f"gelaufen sind {ran}:\n{output}"
    )
    assert code == 0, (
        f"der Glob-Schritt wird rot, obwohl er nur Übersprungenes vorfand:\n{output}"
    )


def test_no_step_is_allowed_to_fail_quietly() -> None:
    """Zwei Schlüssel machen aus einem roten Test eine grüne CI.

    ``continue-on-error`` lässt den Schritt fehlschlagen, ohne dass es zählt.
    ``if:`` ist der schärfere Fall: Ein falsy Ausdruck -- ``false`` genügt, aber
    auch ein ``github.event_name == 'push'`` beim wöchentlichen Cron -- und der
    Test startet gar nicht erst. Beides bleibt sonst still.
    """
    for step in _steps():
        muffled = set(STEP_KEY.findall(step)) & MUFFLERS
        assert not muffled, (
            f"ein Schritt im Job 'tests' trägt {sorted(muffled)} und kann damit "
            "fehlschlagen oder übersprungen werden, ohne den Job rot zu "
            f"machen:\n{step}"
        )


def test_the_job_itself_cannot_be_switched_off() -> None:
    """Auf Job-Ebene schalten dieselben zwei Schlüssel alle Schritte auf einmal ab."""
    muffled = set(JOB_KEY.findall(_tests_job())) & MUFFLERS
    assert not muffled, (
        f"der Job 'tests' trägt {sorted(muffled)} auf Job-Ebene -- damit ist "
        "jeder Schritt darin wirkungslos, so streng er auch geprüft wird"
    )


def test_a_single_file_step_starts_that_file_and_nothing_else() -> None:
    """Ein Einzelschritt ist der Aufruf selbst, nicht ein Text, der ihn enthält.

    ``echo python3 tests/x.py`` und ``python3 tests/x.py || true`` nennen die
    Datei beide und sind beide immer grün. Wer den Namen nennt, muss ihn auch
    starten.
    """
    steps = _steps()
    glob_at = _glob_step_index(steps)
    for index, step in enumerate(steps):
        if index == glob_at:
            continue  # Der globt, statt Namen zu nennen; er hat eigene Tests.
        named = set(NAMES_A_TEST_FILE.findall(_without_comments(step)))
        started = set(RUN_PY.findall(step)) | set(RUN_NODE.findall(step))
        assert named == started, (
            f"ein Schritt nennt {sorted(named - started)}, startet die Datei "
            "aber nicht als vollständige run-Zeile -- ein Präfix wie 'echo' "
            "oder ein angehängtes '|| true' lässt den Schritt grün bleiben, "
            f"egal wie der Test ausgeht:\n{step}"
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


def test_the_workflow_declares_the_token_rights_at_all() -> None:
    """Ohne Block gilt der Repo-Standard, und den setzt nicht dieser Workflow.

    Er steht absichtlich oben und nicht je Job: So erbt auch ein Job, den
    später jemand hinzufügt, die Leserechte, statt still mit dem Standard zu
    laufen. ``contents: read`` ist die Untergrenze, weil jeder Job das Repo
    auscheckt.
    """
    top = [text for where, text in _permission_blocks() if where == "oberste Ebene"]
    assert len(top) == 1, (
        "validate.yml soll genau einen permissions-Block auf oberster Ebene "
        f"haben, gefunden: {len(top)} -- ohne ihn läuft jeder Job mit dem "
        "Repo-Standard des GITHUB_TOKEN"
    )
    assert "contents: read" in top[0], (
        "der permissions-Block oben gibt kein 'contents: read' her, das "
        f"actions/checkout in jedem Job braucht:\n{top[0]}"
    )


def test_no_permissions_block_grants_more_than_reading() -> None:
    """Kein Schritt schreibt, also darf kein Block Schreibrechte vergeben.

    Geprüft wird jeder Block, auch ein Block auf Job-Ebene: Der überschreibt
    die Rechte von oben und wäre die stille Lücke, die der Block oben nicht
    schließt.
    """
    blocks = _permission_blocks()
    assert blocks, "validate.yml hat keinen permissions-Block mehr"
    for where, content in blocks:
        entries = [line for line in content.splitlines() if line.strip()]
        if content.strip() == "{}":
            continue  # gar keine Rechte -- enger geht es nicht.
        assert entries, f"der permissions-Block ({where}) ist leer"
        for entry in entries:
            assert PERMISSION_LINE.match(entry), (
                f"der permissions-Block ({where}) enthält {entry!r} -- erlaubt "
                "ist nur '<scope>: read' oder '<scope>: none'; alles andere "
                "gibt dem GITHUB_TOKEN mehr, als dieser Workflow braucht"
            )


def _main() -> None:
    stubs.run_tests(globals(), "CI workflow")


if __name__ == "__main__":
    _main()
