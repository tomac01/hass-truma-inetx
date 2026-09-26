#!/usr/bin/env python3
"""Prüft, dass jedes Plattformmodul ``PARALLEL_UPDATES = 0`` deklariert.

Warum das geprüft wird. Home Assistant legt je Plattform ein Semaphor an, das
die Aktualisierungen der Entitäten serialisiert, und entscheidet über die
Vorgabe an einer einzigen Stelle -- ``EntityPlatform._async_add_entity`` ruft
``_get_parallel_updates_semaphore(hasattr(entity, "update"))``. Gemessen an
Home Assistant 2025.1.4:

* Ohne Deklaration und ohne synchrones ``update()`` entsteht kein Semaphor.
* ``PARALLEL_UPDATES = 0`` wird intern zu ``None`` und ergibt ebenfalls keines.
* ``PARALLEL_UPDATES = 1`` legt ein ``Semaphore(1)`` an und setzt
  ``_update_in_sequence``.

Die Deklaration ``= 0`` ist hier also gleichbedeutend mit dem Weglassen -- die
Mutation auf ``1`` ist es nicht, und genau deshalb steht die Zahl hier unter
Aufsicht statt nur als Kommentar im Modul.

Ein verbreiteter Trugschluss, der diese Prüfung ausgelöst hat: ``CoordinatorEntity``
bringt sehr wohl ein ``async_update`` mit (es reicht eine Anforderung an den
Coordinator durch). Daraus folgt aber nichts, denn Home Assistant fragt nach
``update`` -- der *synchronen* Methode --, und die hat ``CoordinatorEntity``
nicht. Wer den Kommentar in den Plattformmodulen gegen die Wirklichkeit prüft,
möge bei ``hasattr`` anfangen, nicht bei ``async_update``.

Die Modulliste wird nicht gepflegt, sondern erhoben: Plattformmodul ist, was
ein ``async_setup_entry`` mit ``async_add_entities`` als drittem Parameter
hat. Eine gepflegte Liste hätte denselben Fehler wie die Testliste im
Workflow, die fünf Testdateien eine ganze Beta-Serie lang übersah.

Dieser Test liest den Quelltext mit ``ast`` und lädt kein Modul. Er steht
darum in der ``ALWAYS``-Liste von ``tools/mutate.py``.

Run: ``python3 tests/test_parallel_updates.py``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"


def _is_platform(tree: ast.Module) -> bool:
    """Ein Plattformmodul nimmt ``async_add_entities`` entgegen."""
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            names = [a.arg for a in node.args.args]
            return len(names) >= 3 and names[2] == "async_add_entities"
    return False


def _declared(tree: ast.Module) -> int | None:
    """Der Wert von ``PARALLEL_UPDATES`` auf Modulebene, falls deklariert."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PARALLEL_UPDATES":
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, int
                ):
                    return node.value.value
                return None
    return None


def _platforms() -> dict[str, ast.Module]:
    """Jedes Plattformmodul der Integration, nach Dateiname."""
    found = {}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _is_platform(tree):
            found[path.name] = tree
    return found


def test_every_platform_module_declares_no_parallel_updates() -> None:
    """Kein Plattformmodul darf die Deklaration weglassen oder ändern."""
    platforms = _platforms()
    for name, tree in sorted(platforms.items()):
        value = _declared(tree)
        assert value is not None, (
            f"{name} deklariert kein PARALLEL_UPDATES. Ohne Deklaration hängt "
            f"das Verhalten daran, ob die erste Entität ein synchrones "
            f"update() hat -- das soll hier nicht implizit sein."
        )
        assert value == 0, (
            f"{name} deklariert PARALLEL_UPDATES = {value}, erwartet 0. "
            f"Jeder Wert über 0 legt ein Semaphor an und serialisiert die "
            f"Aktualisierungen; diese Entitäten werden vom Coordinator "
            f"versorgt und haben nichts zu serialisieren."
        )


def test_the_platform_modules_are_the_ones_we_think() -> None:
    """Gegenprobe zur Erhebung: sie findet sieben, und die richtigen sieben.

    Ohne diese Prüfung könnte ``_is_platform`` stillschweigend nichts mehr
    finden -- und die Prüfung darüber liefe dann über die leere Menge und
    meldete Erfolg, ohne etwas geprüft zu haben. Dieselbe Bauart wie die
    ``ran == 0``-Klausel im Workflow.
    """
    found = set(_platforms())
    expected = {
        "binary_sensor.py",
        "button.py",
        "climate.py",
        "number.py",
        "select.py",
        "sensor.py",
        "switch.py",
    }
    assert found == expected, (
        f"Plattformmodule erhoben: {sorted(found)}, erwartet {sorted(expected)}. "
        f"Kommt ein Modul hinzu, gehört es in beide Mengen; verschwindet eines, "
        f"prüfe zuerst _is_platform."
    )


def test_the_survey_rejects_a_non_platform() -> None:
    """``_is_platform`` darf nicht auf jedes ``async_setup_entry`` anspringen.

    ``__init__.py`` hat eines mit zwei Parametern und ist keine Plattform.
    """
    init = ast.parse((SRC / "__init__.py").read_text(encoding="utf-8"))
    assert not _is_platform(init), "__init__.py ist keine Plattform"

    zwei = ast.parse("async def async_setup_entry(hass, entry) -> bool: ...")
    assert not _is_platform(zwei)
    drei = ast.parse("async def async_setup_entry(hass, entry, async_add_entities): ...")
    assert _is_platform(drei)


def test_the_reader_sees_the_value_it_is_given() -> None:
    """Gegenprobe zu ``_declared``: liest die Zahl, nicht die Anwesenheit."""
    assert _declared(ast.parse("PARALLEL_UPDATES = 0")) == 0
    assert _declared(ast.parse("PARALLEL_UPDATES = 1")) == 1
    assert _declared(ast.parse("ANDERES = 0")) is None
    assert _declared(ast.parse("")) is None


def _main() -> None:
    stubs.run_tests(globals(), "Parallel updates")


if __name__ == "__main__":
    _main()
