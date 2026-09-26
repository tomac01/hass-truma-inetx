#!/usr/bin/env python3
"""Prüft, dass jeder Weg in die Integration aus dem Quelltext übersetzt.

Warum das hier steht: ``SourceFileLoader`` hält einen ``.pyc``-Eintrag für
gültig, solange mtime (auf ganze Sekunden gerundet) und Byte-Größe der Quelle
unverändert sind. Eine Mutation macht genau so eine Änderung -- ``1`` gegen
``2`` tauschen, gleich lang, innerhalb derselben Sekunde -- und lief dann nie:
am 2026-09-26 galten drei echte Mutationen in ``select.py`` als überlebt, weil
der alte Bytecode statt des neuen Quelltextes lief. Ein Test, der die Mutation
nicht sieht, beweist nichts, und ein Mutationslauf, der das nicht merkt,
bescheinigt eine Abdeckung, die es nicht gibt.

``stubs.AlwaysFresh`` behebt das. Der Schutz kann aber still wieder
verschwinden -- genau das war die Sorge beim Vorgänger ``f51673d``: dort hing
der Eingriff an einem Monkeypatch hinter einer ``getattr``-Prüfung, die sich
stillschweigend abgeschaltet hätte, ohne dass ein Test rot geworden wäre. Ein
Schutz gegen falsch grüne Tests braucht selbst einen Test, und dieser hier ist
er: Er prüft nicht, *wie* der Loader gebaut ist, sondern nur, dass eine gleich
lange Änderung hinter einem gültigen Cache-Eintrag tatsächlich ankommt. Jede
Umsetzung, die das leistet, besteht ihn; jede, die es aufgibt, fällt durch.

**Der Cache-Eintrag muss vorher existieren.** Ohne vorhandene ``.pyc``-Datei
übersetzt auch ein kaputter Loader aus dem Quelltext -- der Test wäre grün und
würde nichts prüfen. Deshalb legt jeder Testfall die ``.pyc`` selbst an
(``py_compile``, der die Datei unabhängig von ``sys.dont_write_bytecode``
schreibt) und belegt mit einer Gegenprobe, dass dieser Eintrag wirklich greift:
ein unveränderter ``SourceFileLoader`` liest an derselben Stelle noch den alten
Wert. Fällt diese Gegenprobe, ist die Voraussetzung des Tests weggefallen
(CPython cacht anders als beschrieben) -- nicht der Fix kaputt.

**Keine Produktionsdatei wird angefasst.** Die Mutation trifft ein
Wegwerf-Modul in einem ``tempfile``-Verzeichnis, nie eine Datei unter
``custom_components/``. Das ist die einzige Variante, deren Sicherheit nicht
davon abhängt, dass ein ``finally``-Block noch läuft: Auch ein Abbruch mitten
im Test (``SIGKILL``, Stromausfall) kann keine Quelldatei des Projekts
beschädigt zurücklassen, weil keine zum Schreiben geöffnet wird. Eine Sicherung
per ``shutil.copy2`` und Rückkopieren im ``finally`` leistet das nicht -- sie
setzt voraus, dass der Test sein Ende erreicht. Nebenbei entfällt so auch jede
Streudatei im Arbeitsbaum und jedes ``__pycache__`` im Produktionsverzeichnis.

* ``load`` nimmt das Verzeichnis als ``path``-Argument, zeigt also direkt auf
  das Wegwerfverzeichnis.
* ``load_truma`` nimmt kein ``path``, liest ``stubs.SRC`` aber bei jedem Aufruf
  neu -- der Test biegt diese Variable um und setzt sie im ``finally`` zurück.
  Das ist ein Eingriff im Speicher dieses Prozesses, keiner auf der Platte.
  Griffe der Umbau nicht mehr, suchte ``load_truma`` das Wegwerf-Modul im
  echten Baum, fände es nicht und scheiterte laut -- nicht still grün.
* Der relative Import, den ein geladenes Modul selbst auslöst: er geht über
  ``stubs._FreshFinder``, der dafür am ``__path__`` von ``truma_pkg`` hängt.
* ``stubs.spec_from_source``, der Weg für Testdateien, die ihr Modul selbst
  öffnen. Dass keine von ihnen daran vorbeigeht, bewacht der letzte Testfall
  über den Quelltext -- eine Liste gepflegter Ausnahmen wäre genau das, dessen
  Verrotten hier überhaupt das Problem war.

``test_the_production_tree_is_untouched`` nagelt das zum Schluss fest: Es
vergleicht die Inhalts-Hashes aller ``*.py`` unter ``custom_components/`` mit
dem Stand beim Import dieser Datei. Ein Hash, weil die Mutation hier gerade
Größe und mtime absichtlich unverändert lässt -- ein Vergleich über ``stat``
würde genau die Änderung übersehen, um die es geht.

Run: ``python3 tests/test_fresh_compile.py``
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import os
import py_compile
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "custom_components"

# Name des Wegwerf-Moduls. Nichts im Projekt heißt so, damit der Testlauf
# keinen echten Modulnamen in ``sys.modules`` verdeckt.
PROBE = "fresh_compile_probe"


@contextlib.contextmanager
def _sys_modules_restored(*names: str):
    """``sys.modules`` für ``names`` wiederherstellen, was auch passiert.

    Die Wegwerf-Module sollen den Lauf nicht überdauern, und ``truma_pkg``
    selbst -- das der Relativ-Import-Fall auf ein Tempverzeichnis umbiegt --
    schon gar nicht. ``finally``, damit auch ein fehlgeschlagener Fall
    aufräumt und die folgenden nicht auf seinen Resten arbeiten.
    """
    saved = {name: sys.modules.get(name) for name in names}
    try:
        yield
    finally:
        for name, was in saved.items():
            if was is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = was


def _probe_source(value: int) -> str:
    """Quelltext des Wegwerf-Moduls -- ein Wert, sonst nichts.

    Die Form einer echten Mutation: ``ENTER_ELECTRIC = 1`` gegen ``= 2``. Weil
    beide Fassungen aus derselben Vorlage kommen, sind sie zwangsläufig gleich
    lang; ``test_the_probe_edit_holds_length_and_mtime`` prüft es zusätzlich.
    """
    return f"ENTER_ELECTRIC = {value}\n"


def _snapshot(directory: Path) -> dict[str, str]:
    """Inhalts-Hash jeder ``*.py`` unter ``directory``."""
    return {
        str(file.relative_to(directory)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(directory.rglob("*.py"))
    }


# Beim Import, also vor jedem Testfall.
_PRODUCTION_AT_START = _snapshot(SRC)


def _plant_probe(directory: Path) -> Path:
    """Wegwerf-Modul mit Wert 1 anlegen und einen gültigen ``.pyc`` erzeugen.

    ``py_compile`` statt eines Ladevorgangs: Es schreibt die ``.pyc`` auch
    dann, wenn ``sys.dont_write_bytecode`` gesetzt ist, und schreibt sie mit
    genau dem Kopf (mtime, Größe), den der Loader später zum Vergleich liest.
    """
    file = directory / f"{PROBE}.py"
    file.write_text(_probe_source(1), encoding="utf-8")
    cached = Path(py_compile.compile(
        str(file), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    ))
    assert cached.is_file(), (
        f"{cached} wurde nicht angelegt -- ohne Cache-Eintrag prüft dieser Test "
        "nichts, weil dann auch ein kaputter Loader aus dem Quelltext übersetzt"
    )
    return file


def _mutate_in_place(file: Path) -> None:
    """Auf Wert 2 ändern, ohne Größe und mtime anzutasten.

    Damit sieht die Datei für den Loader unverändert aus: Der Cache-Eintrag aus
    ``_plant_probe`` gilt weiter, obwohl der Quelltext ein anderer ist.
    """
    before = file.stat()
    file.write_text(_probe_source(2), encoding="utf-8")
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = file.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns), (
        "die Mutation hat Größe oder mtime verändert -- dann wäre der "
        "Cache-Eintrag ohnehin ungültig und der Test ohne Aussage"
    )


def _read_with_plain_loader(file: Path) -> int:
    """Denselben Wert mit einem unveränderten ``SourceFileLoader`` lesen.

    Die Gegenprobe: So sah es vor dem Fix aus. Das Modul landet bewusst nicht
    in ``sys.modules`` -- es soll nichts hinterlassen, was den eigentlichen
    Ladevorgang danach beeinflussen könnte.
    """
    fullname = f"plain_{file.stem}"
    # fresh-import-exempt: der blanke Loader IST hier die Gegenprobe.
    loader = importlib.machinery.SourceFileLoader(fullname, str(file))
    spec = importlib.util.spec_from_file_location(fullname, file, loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ENTER_ELECTRIC


def _stale_read_precondition(file: Path) -> None:
    """Belegen, dass der angelegte Cache-Eintrag hier wirklich greift."""
    stale = _read_with_plain_loader(file)
    assert stale == 1, (
        f"ein unveränderter SourceFileLoader liest {stale} statt des alten Wertes 1. "
        "Damit ist die Voraussetzung dieses Regressionstests weggefallen: entweder "
        "ist der angelegte .pyc keiner nach Zeitstempel -- dann steht ein Hash "
        "dahinter, der die Quelle wirklich vergleicht -- oder CPython hält einen "
        "solchen Eintrag nicht mehr für gültig, wenn mtime und Größe unverändert "
        "bleiben. Beides sagt nichts über stubs.AlwaysFresh; der Test muss dann "
        "auf die neue Cache-Regel umgestellt werden"
    )


def test_load_sees_a_same_length_edit_behind_a_valid_cache_entry() -> None:
    """``load`` übersetzt aus dem Quelltext, nicht aus dem alten Bytecode."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        file = _plant_probe(directory)
        _mutate_in_place(file)
        _stale_read_precondition(file)

        with _sys_modules_restored(f"truma_pkg.{PROBE}"):
            module = stubs.load(PROBE, "truma_pkg", directory)
        assert module.ENTER_ELECTRIC == 2, (
            f"load() liest {module.ENTER_ELECTRIC} statt 2: Der alte Bytecode lief "
            "trotz geändertem Quelltext. Eine Mutation gleicher Länge innerhalb "
            "derselben Sekunde käme in keinem Test an -- siehe stubs.AlwaysFresh"
        )


def test_load_truma_sees_a_same_length_edit_behind_a_valid_cache_entry() -> None:
    """Dasselbe für ``load_truma``, das seinen Pfad aus ``stubs.SRC`` bildet."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "truma"
        directory.mkdir()
        file = _plant_probe(directory)
        _mutate_in_place(file)
        _stale_read_precondition(file)

        original_src = stubs.SRC
        try:
            stubs.SRC = Path(tmp)
            with _sys_modules_restored(f"truma_pkg.truma.{PROBE}"):
                module = stubs.load_truma(PROBE)
        finally:
            stubs.SRC = original_src
        assert stubs.SRC == original_src, "stubs.SRC wurde nicht zurückgesetzt"
        assert module.ENTER_ELECTRIC == 2, (
            f"load_truma() liest {module.ENTER_ELECTRIC} statt 2: Der alte Bytecode "
            "lief trotz geändertem Quelltext -- siehe stubs.AlwaysFresh"
        )


def test_a_relative_import_also_sees_a_same_length_edit() -> None:
    """Auch das nachgezogene Modul wird übersetzt, nicht aus dem Cache gelesen.

    ``load`` öffnet nur die eine Datei. Was diese per ``from .x import y`` holt,
    geht über Pythons normalen Pfad-Finder -- und damit wieder über den Cache,
    solange ``stubs._FreshFinder`` nicht davor sitzt. Gemessen am 2026-09-26:
    31 der 41 Testdateien luden so mindestens ein Integrationsmodul.
    """
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)

        # Das nachgezogene Modul: Cache-Eintrag anlegen, dann gleich lang ändern.
        pulled = directory / f"{PROBE}_pulled.py"
        pulled.write_text(_probe_source(1), encoding="utf-8")
        py_compile.compile(
            str(pulled), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
        )
        _mutate_in_place(pulled)
        _stale_read_precondition(pulled)

        # Das Modul, das ``load`` öffnet, holt den Wert nur herüber.
        front = directory / f"{PROBE}_front.py"
        front.write_text(
            f"from .{PROBE}_pulled import ENTER_ELECTRIC\n", encoding="utf-8"
        )

        # ``truma_pkg`` auf das Wegwerfverzeichnis zeigen lassen, damit der
        # relative Import dort sucht. Danach wieder wegräumen.
        with _sys_modules_restored("truma_pkg", f"truma_pkg.{PROBE}_front",
                                   f"truma_pkg.{PROBE}_pulled"):
            stubs.mod("truma_pkg", __path__=[str(directory)])
            module = stubs.load(f"{PROBE}_front", "truma_pkg", directory)
            assert module.ENTER_ELECTRIC == 2, (
                f"der relative Import liest {module.ENTER_ELECTRIC} statt 2: das "
                "nachgezogene Modul kam aus dem alten Bytecode -- siehe "
                "stubs._FreshFinder"
            )


def test_spec_from_source_sees_a_same_length_edit() -> None:
    """Der Weg der Testdateien, die ihre Module selbst öffnen.

    Acht Testdateien stellen sich Home Assistant anders zusammen als ``stubs``
    und rufen darum nicht ``load``, sondern öffnen ihr Modul selbst. Sie tun
    das über ``spec_from_source``, das denselben Loader einsetzt.
    """
    with tempfile.TemporaryDirectory() as tmp:
        file = _plant_probe(Path(tmp))
        _mutate_in_place(file)
        _stale_read_precondition(file)

        fullname = f"truma_pkg.{PROBE}_own"
        spec = stubs.spec_from_source(fullname, file)
        module = importlib.util.module_from_spec(spec)
        with _sys_modules_restored(fullname):
            sys.modules[fullname] = module
            spec.loader.exec_module(module)
            assert module.ENTER_ELECTRIC == 2, (
                f"spec_from_source() liest {module.ENTER_ELECTRIC} statt 2: der "
                "alte Bytecode lief -- siehe stubs.AlwaysFresh"
            )


def test_the_probe_edit_holds_length_and_mtime() -> None:
    """Die Vorrichtung selbst: gleiche Länge, anderer Inhalt, gleiche mtime.

    Wäre die Änderung unterschiedlich lang, wäre der Cache-Eintrag ungültig und
    die Tests oben grün, ohne etwas über den Loader zu sagen.
    """
    before, after = _probe_source(1), _probe_source(2)
    assert before != after
    assert len(before) == len(after) == len(before.encode("utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        file = _plant_probe(Path(tmp))
        stat_before = file.stat()
        assert file.read_text(encoding="utf-8") == before
        _mutate_in_place(file)
        stat_after = file.stat()
        assert file.read_text(encoding="utf-8") == after
        assert stat_after.st_size == stat_before.st_size
        assert stat_after.st_mtime_ns == stat_before.st_mtime_ns


def test_a_subpackage_also_sees_a_same_length_edit() -> None:
    """Auch ein Unterpaket wird übersetzt, nicht nur ein einzelnes Modul.

    ``find_spec`` sieht ``<tail>/__init__.py`` nur, weil es ausdrücklich danach
    sucht -- prüfte es allein ``<tail>.py``, fiele jedes Unterpaket an den
    normalen Pfad-Finder und damit an den Cache. Heute deckt der Fehler nichts
    auf, weil jede Testdatei ``truma_pkg.truma`` stubbt und der echte
    ``__init__`` nur einen Docstring trägt; morgen trägt er Code, oder eine
    Testdatei lässt den Stub weg.
    """
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        package = directory / f"{PROBE}_pkg"
        package.mkdir()
        init = package / "__init__.py"
        init.write_text(_probe_source(1), encoding="utf-8")
        py_compile.compile(
            str(init), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
        )
        _mutate_in_place(init)
        _stale_read_precondition(init)

        fullname = f"truma_pkg.{PROBE}_pkg"
        with _sys_modules_restored("truma_pkg", fullname):
            stubs.mod("truma_pkg", __path__=[str(directory)])
            module = importlib.import_module(fullname)
            assert module.ENTER_ELECTRIC == 2, (
                f"das Unterpaket liest {module.ENTER_ELECTRIC} statt 2: sein "
                "__init__ kam aus dem alten Bytecode -- siehe stubs._FreshFinder"
            )
            assert module.__spec__.submodule_search_locations is not None, (
                "als Paket nicht erkannt -- ein Untermodul davon wäre nicht mehr "
                "auffindbar"
            )


def test_no_test_file_slips_past_the_fresh_loader() -> None:
    """Wer sein Modul selbst öffnet, nimmt ``spec_from_source`` -- oder begründet es.

    ``spec_from_file_location`` umgeht ``sys.meta_path`` und damit den Finder.
    Eine neue Testdatei im alten Stil bekäme also wieder alten Bytecode, ohne
    dass etwas rot wird -- dieselbe Fehlerklasse, gegen die diese Datei
    angetreten ist. Am 2026-09-26 nachgestellt: eine Testdatei mit blankem
    ``spec_from_file_location`` lud ``truma_pkg.bus`` mit dem Standardloader,
    hinterließ ein ``.pyc`` im Produktionsbaum, und kein Test sagte etwas.

    Die Ausnahme steht dort, wo sie gilt, nicht in einer Liste hier: eine
    Liste, die neben der Wirklichkeit herläuft, ist genau das, was diesen
    Schutz überhaupt nötig gemacht hat.
    """
    exemption = "fresh-import-exempt:"
    offenders = []
    for file in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        text = file.read_text(encoding="utf-8")
        lines = text.splitlines()
        # Über den Syntaxbaum, nicht per Textsuche: sonst fände dieser Test
        # die Namen in seinem eigenen Quelltext und in seiner Fehlermeldung.
        for node in ast.walk(ast.parse(text)):
            called = getattr(node, "func", None)
            if not isinstance(node, ast.Call) or not isinstance(called, ast.Attribute):
                continue
            if called.attr != "spec_from_file_location":
                continue
            # Die Markierung steht in der Zeile selbst oder kurz darüber.
            window = lines[max(0, node.lineno - 4):node.lineno]
            if any(exemption in near for near in window):
                continue
            offenders.append(f"{file.name}:{node.lineno}")

    assert not offenders, (
        f"diese Stellen öffnen ein Modul am frischen Loader vorbei: {offenders}. "
        "stubs.spec_from_source nehmen -- oder, wenn dort kein Integrationsmodul "
        f"geladen wird, die Zeile mit '# {exemption} <Grund>' ausnehmen"
    )


def test_the_production_tree_is_untouched() -> None:
    """Kein Testfall hat eine Datei unter ``custom_components/`` verändert.

    Über Inhalts-Hashes, nicht über ``stat``: Die Mutation oben hält Größe und
    mtime absichtlich fest, ein ``stat``-Vergleich würde sie also gerade nicht
    sehen. Läuft dieser Testfall als Letzter durch, hat der Lauf der Quelle des
    Projekts nichts angetan.
    """
    assert _snapshot(SRC) == _PRODUCTION_AT_START, (
        "der Inhalt einer Produktionsdatei hat sich während des Testlaufs geändert"
    )


def _main() -> None:
    stubs.run_tests(globals(), "Fresh compile")


if __name__ == "__main__":
    _main()
