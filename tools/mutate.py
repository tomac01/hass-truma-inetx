#!/usr/bin/env python3
"""Mutationstest: messen, was die Tests wirklich fangen.

Ändert den Produktivcode an einer Stelle und schaut, ob ein Test rot wird.
Merkt es keiner, ist diese Stelle nicht abgedeckt -- oder die Änderung ist
bedeutungslos. Welches von beidem, entscheidet nur ein Blick in den Code;
dieses Werkzeug liefert die Liste, nicht das Urteil.

Warum es im Repo liegt. Am 2026-09-26 lag die Erkennungsquote bei 62,2 %, und
niemand wusste das, weil es kein Werkzeug gab, das nachmisst. Nach dem
Schließen von 97 Lücken sind es 74,3 % (siehe ``REFERENCE`` unten). Ohne ein
Werkzeug im Baum ist diese Zahl eine Behauptung in einem Sitzungsprotokoll:
Sie kann fallen, und niemand merkt es. Genau dieselbe Lage wie bei den vier
Prüfungen, die damals "alles in Ordnung" sagten, ohne etwas zu prüfen.

Zwei Eigenheiten, die das Messen hier überhaupt erst ehrlich machen:

Erstens holt sich jeder Arbeiter eine eigene Kopie des Baums. Das echte
Arbeitsverzeichnis wird nie verändert -- auch nicht kurz.

Zweitens *muss* ``tests/stubs.py`` aus dem Quelltext übersetzen, und das tut
es (``AlwaysFresh``). Vor diesem Fix hielt Python den ``.pyc``-Cache für
gültig, solange mtime auf ganze Sekunden und Dateigröße unverändert waren --
und genau so eine Änderung macht eine Mutation. Drei echte Mutationen in
``select.py`` galten deshalb als überlebt, obwohl die Tests sie sehr wohl
sehen. Wer dieses Werkzeug fährt und plötzlich viel mehr Überlebende sieht,
prüfe zuerst, ob dieser Schutz noch steht: ``python3 tests/test_fresh_compile.py``.

Aufrufe:

    tools/mutate.py                      # voller Lauf, Bericht am Ende
    tools/mutate.py --only bus.py        # nur ein Modul
    tools/mutate.py --limit 40           # gleichmäßige Stichprobe
    tools/mutate.py --workers 4          # weniger Last
    tools/mutate.py --out ergebnis.jsonl # Ergebnisse behalten

Die Tests brauchen teils ``cbor2`` und ``voluptuous``. Ohne sie scheitern jene
Dateien am Import und melden jede Mutation als erkannt, was die Quote zu gut
aussehen lässt -- das Werkzeug warnt davor und nennt den Interpreter, mit dem
es laufen sollte.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components"
TESTS = ROOT / "tests"

# Die letzte dokumentierte Messung. Ein Lauf stellt sich dagegen, damit ein
# Rückschritt auffällt statt unbemerkt zu bleiben -- siehe
# docs/REV-005-abdeckung-mutationstest.md im Proxy-Projekt.
REFERENCE = {"date": "2026-09-26", "caught": 733, "total": 987}

# Tests, die Quelldateien mit ``ast`` lesen statt sie zu importieren. Sie sehen
# eine Mutation, ohne das Modul zu laden, und laufen darum immer mit.
ALWAYS = ("test_ci_workflow.py", "test_docs_consistency.py",
          "test_manifest_version.py", "test_placeholder_addresses.py")

COMPARE = {
    ast.Lt: (b"<", b"<="), ast.LtE: (b"<=", b"<"),
    ast.Gt: (b">", b">="), ast.GtE: (b">=", b">"),
    ast.Eq: (b"==", b"!="), ast.NotEq: (b"!=", b"=="),
    ast.Is: (b"is", b"is not"), ast.IsNot: (b"is not", b"is"),
    ast.In: (b"in", b"not in"), ast.NotIn: (b"not in", b"in"),
}


def mutations_for(path: Path) -> list[dict]:
    """Jede Stelle in ``path``, die sich eindeutig ändern lässt.

    Auf Bytes, nicht auf Text: ``ast`` liefert Spalten als UTF-8-Offsets, und
    die Dateien tragen Umlaute in Kommentaren. Aufgenommen wird nur, was den
    Syntaxbaum wirklich verändert -- eine Mutation ohne Wirkung wäre ein
    garantierter Überlebender und würde die Quote verfälschen.
    """
    raw = path.read_bytes()
    starts, pos = [0], 0
    for line in raw.splitlines(keepends=True):
        pos += len(line)
        starts.append(pos)
    tree = ast.parse(raw)
    original = ast.dump(tree)
    found: list[dict] = []

    def off(lineno: int, col: int) -> int:
        return starts[lineno - 1] + col

    def add(kind: str, begin: int, end: int, old: bytes, new: bytes) -> None:
        if raw[begin:end] != old:
            return
        try:
            if ast.dump(ast.parse(raw[:begin] + new + raw[end:])) == original:
                return
        except SyntaxError:
            return
        found.append({
            "file": str(path.relative_to(ROOT)), "kind": kind, "offset": begin,
            "length": end - begin, "old": old.decode(), "new": new.decode(),
            "line": raw[:begin].count(b"\n") + 1,
        })

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            b = off(node.lineno, node.col_offset)
            e = off(node.end_lineno, node.end_col_offset)
            if node.value is True:
                add("bool", b, e, b"True", b"False")
            elif node.value is False:
                add("bool", b, e, b"False", b"True")
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                add("int", b, e, raw[b:e], str(node.value + 1).encode())
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            pair = COMPARE.get(type(node.ops[0]))
            if not pair:
                continue
            gap_b = off(node.left.end_lineno, node.left.end_col_offset)
            gap_e = off(node.comparators[0].lineno, node.comparators[0].col_offset)
            idx = raw.find(pair[0], gap_b, gap_e)
            if idx >= 0:
                add("compare", idx, idx + len(pair[0]), pair[0], pair[1])
        elif isinstance(node, ast.BoolOp) and len(node.values) >= 2:
            old = b"and" if isinstance(node.op, ast.And) else b"or"
            new = b"or" if old == b"and" else b"and"
            gap_b = off(node.values[0].end_lineno, node.values[0].end_col_offset)
            gap_e = off(node.values[1].lineno, node.values[1].col_offset)
            idx = raw.find(old, gap_b, gap_e)
            if idx >= 0:
                add("boolop", idx, idx + len(old), old, new)

    return found


def collect(only: str | None) -> list[dict]:
    """Alle Mutationen, überlappungsfrei, optional auf eine Datei begrenzt."""
    out: list[dict] = []
    for f in sorted(SRC.rglob("*.py")):
        if only and f.name != only and str(f.relative_to(ROOT)) != only:
            continue
        out.extend(mutations_for(f))
    seen, unique = set(), []
    for m in out:
        key = (m["file"], m["offset"], m["length"])
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def module_of(rel: str) -> str:
    """custom_components/truma_inetx/truma/const.py -> truma_pkg.truma.const"""
    parts = Path(rel).relative_to("custom_components/truma_inetx").with_suffix("").parts
    return "truma_pkg." + ".".join(parts)


def build_matrix(python: str) -> dict[str, list[str]]:
    """Welcher Test lädt welches Modul? Bestimmt, was je Mutation läuft.

    Ohne das liefe jede Mutation gegen alle Tests, was den Lauf um ein
    Vielfaches verlängert. Ermittelt wird es, indem jede Testdatei einmal
    vollständig läuft und danach ``sys.modules`` gelesen wird.
    """
    probe = (
        "import json, runpy, sys\n"
        "t = sys.argv[1]\n"
        "sys.argv = [t]\n"
        "try:\n"
        "    runpy.run_path(t, run_name='__main__')\n"
        "except BaseException:\n"
        "    pass\n"
        "print('MODS ' + json.dumps(sorted(\n"
        "    n for n, m in list(sys.modules.items())\n"
        "    if n.startswith('truma_pkg') and getattr(m, '__file__', None)\n"
        "    and 'custom_components' in str(m.__file__))))\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        helper = Path(tmp) / "which.py"
        helper.write_text(probe, encoding="utf-8")

        def ask(test: Path) -> tuple[str, list[str]]:
            done = subprocess.run([python, str(helper), str(test)],
                                  capture_output=True, text=True, cwd=ROOT)
            for line in done.stdout.splitlines():
                if line.startswith("MODS "):
                    return test.name, json.loads(line[5:])
            return test.name, []

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = dict(pool.map(ask, sorted(TESTS.glob("test_*.py"))))

    by_module: dict[str, list[str]] = {}
    for test, mods in rows.items():
        for m in mods:
            by_module.setdefault(m, []).append(test)
    return by_module


def run(mutations: list[dict], matrix: dict[str, list[str]], python: str,
        workers: int, timeout: int) -> list[dict]:
    """Jede Mutation in einer eigenen Kopie fahren, beim ersten roten Test aufhören."""
    stage = Path(tempfile.mkdtemp(prefix="mutate-"))
    pool: Queue[Path] = Queue()
    keep = ("custom_components", "tests", "tools", "docs", "dumps", "examples",
            ".github", "README.md", "LICENSE", "hacs.json")
    for i in range(workers):
        work = stage / f"w{i}"
        work.mkdir(parents=True)
        for item in keep:
            src = ROOT / item
            if src.is_dir():
                shutil.copytree(src, work / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            elif src.exists():
                shutil.copy2(src, work / item)
        pool.put(work)

    def tests_for(rel: str) -> list[str]:
        loaders = matrix.get(module_of(rel), [])
        return list(ALWAYS) + [t for t in loaders if t not in ALWAYS]

    def one(mut: dict) -> dict:
        work = pool.get()
        target = work / mut["file"]
        raw = target.read_bytes()
        begin = mut["offset"]
        end = begin + mut["length"]
        if raw[begin:end] != mut["old"].encode():
            pool.put(work)
            return {**mut, "verdict": "stale", "by": None}
        try:
            target.write_bytes(raw[:begin] + mut["new"].encode() + raw[end:])
            for name in tests_for(mut["file"]):
                try:
                    done = subprocess.run([python, f"tests/{name}"], cwd=work,
                                          capture_output=True, timeout=timeout)
                except subprocess.TimeoutExpired:
                    return {**mut, "verdict": "timeout", "by": name}
                if done.returncode != 0:
                    return {**mut, "verdict": "caught", "by": name}
            return {**mut, "verdict": "survived", "by": None}
        finally:
            target.write_bytes(raw)
            pool.put(work)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = []
            for n, res in enumerate(ex.map(one, mutations), 1):
                results.append(res)
                if n % 50 == 0 or n == len(mutations):
                    print(f"  {n}/{len(mutations)}", file=sys.stderr, flush=True)
        return results
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def report(results: list[dict]) -> int:
    """Bericht, gegen die letzte dokumentierte Messung gestellt."""
    counts = collections.Counter(r["verdict"] for r in results)
    caught = counts["caught"] + counts["timeout"]
    total = len(results)
    if not total:
        print("keine Mutationen -- nichts zu messen")
        return 1
    quote = caught / total * 100
    print(f"\nerkannt   {caught}/{total} = {quote:.1f} %")
    print(f"überlebt  {counts['survived']}")
    if counts["stale"]:
        print(f"übersprungen {counts['stale']} (Datei hat sich geändert)")

    ref = REFERENCE["caught"] / REFERENCE["total"] * 100
    if total == REFERENCE["total"]:
        delta = quote - ref
        print(f"\nletzte Messung ({REFERENCE['date']}): {ref:.1f} % "
              f"-- {'+' if delta >= 0 else ''}{delta:.1f} Punkte")
        if delta < -0.5:
            print("ACHTUNG: schlechter als dokumentiert. Steht der frische Loader "
                  "noch? python3 tests/test_fresh_compile.py")
    else:
        print(f"\n(nicht mit der Referenz vergleichbar: {total} statt "
              f"{REFERENCE['total']} Mutationen)")

    print("\nje Datei:")
    per: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for r in results:
        per[r["file"]][1] += 1
        if r["verdict"] not in ("survived", "stale"):
            per[r["file"]][0] += 1
    for f, (c, t) in sorted(per.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        short = f.replace("custom_components/truma_inetx/", "")
        print(f"  {short:24} {c:3}/{t:3}  {c / t * 100:3.0f} %")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", metavar="DATEI",
                    help="nur dieses Modul, z.B. bus.py")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="gleichmäßige Stichprobe von N Mutationen")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallele Arbeiter, je eine Repo-Kopie (Vorgabe 8)")
    ap.add_argument("--timeout", type=int, default=90,
                    help="Sekunden je Testlauf (Vorgabe 90)")
    ap.add_argument("--python", default=sys.executable,
                    help="Interpreter für die Tests; braucht cbor2 und voluptuous")
    ap.add_argument("--out", type=Path, metavar="DATEI",
                    help="Ergebnisse als JSONL behalten")
    args = ap.parse_args()

    missing = [lib for lib in ("cbor2", "voluptuous")
               if subprocess.run([args.python, "-c", f"import {lib}"],
                                 capture_output=True).returncode != 0]
    if missing:
        print(f"WARNUNG: {args.python} hat {', '.join(missing)} nicht. Die Tests, "
              "die das brauchen, scheitern am Import und melden jede Mutation "
              "als erkannt -- die Quote fällt damit zu gut aus.\n"
              "         Mit --python auf eine Umgebung zeigen, die beides hat.",
              file=sys.stderr)

    mutations = collect(args.only)
    if args.limit and len(mutations) > args.limit:
        step = len(mutations) // args.limit
        mutations = mutations[::step][:args.limit]
    kinds = collections.Counter(m["kind"] for m in mutations)
    print(f"{len(mutations)} Mutationen: "
          + ", ".join(f"{n} {k}" for k, n in kinds.most_common()), file=sys.stderr)

    matrix = build_matrix(args.python)
    print(f"{len(matrix)} Module werden von mindestens einem Test geladen",
          file=sys.stderr)

    results = run(mutations, matrix, args.python, args.workers, args.timeout)
    if args.out:
        args.out.write_text("".join(json.dumps(r) + "\n" for r in results),
                            encoding="utf-8")
        print(f"Ergebnisse in {args.out}", file=sys.stderr)
    return report(results)


if __name__ == "__main__":
    raise SystemExit(main())
