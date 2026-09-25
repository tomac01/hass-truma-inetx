#!/usr/bin/env python3
"""Prüft die Vorrangregeln des Vorgangs-Registers.

Warum das hier steht: Ein Reconnect kann nach dem Befehl beginnen, der
ihn ausgelöst hat. Wenn der Hintergrund-Sync dann später fertig wird,
darf er den gescheiterten Nutzerbefehl nicht wegräumen — sonst meldet
das Dashboard "Bereit", obwohl die Heizung nicht getan hat, was sie
sollte.

Was der Test festnagelt:

1. ein laufender Befehl hat Vorrang vor einem laufenden Sync,
2. ein Befehlsfehler überlebt nachfolgende erfolgreiche Syncs,
3. nur ein neuerer Befehl kann ein Befehlsergebnis ablösen,
4. ein älterer Befehl, der später endet, löst es nicht ab,
5. Abbruch ist ein Ergebnis, kein Verschwinden.

Run: ``python3 tests/test_operations.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

OPS = stubs.load("operations")


def _registry() -> object:
    return OPS.OperationRegistry(lambda: None)


def test_a_running_command_outranks_a_running_sync() -> None:
    """Der Nutzer sieht seinen Befehl, nicht die Hintergrundarbeit."""
    reg = _registry()
    reg.begin("sync")
    reg.begin("water_mode", "Eco (40 °C)")

    assert reg.state == "changing"
    assert reg.attributes == {"action": "water_mode", "target": "Eco (40 °C)", "error": None}


def test_a_command_failure_survives_later_syncs() -> None:
    """Ein Reconnect darf den gescheiterten Befehl nicht wegräumen."""
    reg = _registry()
    command = reg.begin("energy_source", "hybrid")
    sync = reg.begin("sync")
    reg.end(command, "Truma did not confirm EnergySrc.ElectricLevel=1")
    reg.end(sync)

    assert reg.state == "error"
    assert reg.attributes["action"] == "energy_source"
    assert reg.attributes["error"] == "Truma did not confirm EnergySrc.ElectricLevel=1"

    # Auch eine weitere, vollständig erfolgreiche Runde ändert daran nichts.
    reg.end(reg.begin("sync"))
    assert reg.state == "error"


def test_a_newer_command_clears_the_older_error() -> None:
    """Erst ein neuer Befehl setzt die Anzeige zurück."""
    reg = _registry()
    reg.end(reg.begin("water_mode", "off"), "kaputt")
    assert reg.state == "error"

    reg.end(reg.begin("water_mode", "Eco (40 °C)"))
    assert reg.state == "idle"
    assert reg.attributes == {"action": None, "target": None, "error": None}


def test_an_older_command_that_ends_later_does_not_win() -> None:
    """Zwei Befehle, die außer der Reihe enden: der neuere bleibt stehen.

    Der Lastfall ist eine Energie-Transaktion, die auf ihre Bestätigung
    wartet, während der Nutzer daneben die Wassertemperatur stellt. Endet
    der ältere Vorgang später, darf sein Ergebnis das des neueren nicht
    überschreiben — sonst meldet das Dashboard "Bereit", obwohl der zuletzt
    gegebene Befehl gescheitert ist. Ohne den Serienvergleich in ``end``
    wäre das genau der Fall, und die übrigen Prüfungen hier merken es
    nicht: keine andere lässt zwei Befehle außer der Reihe enden.
    """
    reg = _registry()
    older = reg.begin("energy_source", "hybrid")
    newer = reg.begin("water_mode", "Eco (40 °C)")

    reg.end(newer, "Truma did not confirm WaterHeating.Mode=eco")
    reg.end(older)

    assert reg.state == "error", "der ältere Erfolg räumte den neueren Fehler weg"
    assert reg.attributes == {
        "action": "water_mode",
        "target": "Eco (40 °C)",
        "error": "Truma did not confirm WaterHeating.Mode=eco",
    }

    # Und andersherum: der ältere Fehler verdrängt den neueren Erfolg nicht.
    reg = _registry()
    older = reg.begin("energy_source", "hybrid")
    newer = reg.begin("water_mode", "Eco (40 °C)")

    reg.end(newer)
    reg.end(older, "Truma did not confirm EnergySrc.ElectricLevel=1")

    assert reg.state == "idle", "ein überholter Befehl meldete seinen Fehler nach"
    assert reg.attributes == {"action": None, "target": None, "error": None}


def test_cancellation_is_a_result() -> None:
    """Ein abgebrochener Vorgang verschwindet nicht stillschweigend."""
    import asyncio

    reg = _registry()
    try:
        with reg.operation("temperature", 21):
            raise asyncio.CancelledError
    except asyncio.CancelledError:
        pass

    assert reg.state == "error"
    assert reg.attributes["error"] == "Operation cancelled"


def test_empty_exception_text_still_names_the_error() -> None:
    """Ein leerer Fehlertext wäre im Dashboard nutzlos."""
    reg = _registry()
    try:
        with reg.operation("fan_level", 3):
            raise TimeoutError
    except TimeoutError:
        pass

    assert reg.attributes["error"] == "TimeoutError"


def test_energy_source_change_is_visible_while_queued() -> None:
    """Auch eine wartende Transaktion zählt als 'wird umgestellt'."""
    reg = _registry()
    reg.begin("water_mode", "off")
    reg.begin("energy_source", "hybrid")

    assert reg.changing("energy_source") is True
    assert reg.changing("water_mode") is True
    assert reg.changing("temperature") is False


def _main() -> None:
    stubs.run_tests(globals(), "Operations")


if __name__ == "__main__":
    _main()
