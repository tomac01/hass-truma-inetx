#!/usr/bin/env python3
"""Prüft, dass die Manifest-Version für hassfest gültig bleibt.

Warum das hier steht: Der Fork zählte bisher mit ``-live.N``, und das ging
durch, solange die Basis ``0.7.3`` war -- AwesomeVersion liest das als SemVer.
Mit einer Beta-Basis wie ``0.9.0b23`` kippt die Erkennung auf PEP 440, und dort
ist ein ``-live.1``-Suffix ungültig: hassfest lehnte genau das am 26.09.2026 ab,
nachdem Tests und HACS längst grün waren. Der Fehler kostet einen CI-Durchlauf
und fällt sonst erst nach dem Push auf.

Dieser Test braucht ``awesomeversion`` nicht: er bildet die Regel nach, an der
es scheiterte -- eine Version mit Prerelease-Kennung darf kein Bindestrich-
Suffix tragen.

Run: ``python3 tests/test_manifest_version.py``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

MANIFEST = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx" / "manifest.json"

# PEP 440, so weit die Versionen dieses Projekts es brauchen: Release-Segment,
# optionale Prerelease-Kennung (a/b/rc), optionales .postN, optionales .devN.
PEP440 = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")
# SemVer mit Bindestrich-Vorabkennung -- das alte Fork-Schema 0.7.3-live.10.
SEMVER_PRERELEASE = re.compile(r"^\d+\.\d+\.\d+-[0-9A-Za-z.-]+$")


def _version() -> str:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]


def test_the_manifest_version_is_one_hassfest_accepts() -> None:
    """Entweder sauberes PEP 440 oder sauberes SemVer -- keine Mischung."""
    version = _version()
    assert PEP440.match(version) or SEMVER_PRERELEASE.match(version), (
        f"{version!r} ist weder PEP 440 noch SemVer -- hassfest lehnt das ab"
    )


def test_a_prerelease_base_must_not_carry_a_dash_suffix() -> None:
    """Die Regel, an der 0.9.0b23-live.1 gescheitert ist."""
    version = _version()
    has_prerelease = re.search(r"\d+(a|b|rc)\d+", version) is not None
    assert not (has_prerelease and "-" in version), (
        f"{version!r} mischt eine Prerelease-Kennung mit einem Bindestrich-Suffix. "
        "AwesomeVersion liest das als PEP 440, und dort ist der Bindestrich "
        "unzulässig -- hassfest schlägt fehl. Zähle stattdessen mit .postN."
    )


def test_the_check_would_catch_the_version_that_failed() -> None:
    """Gegenprobe, damit dieser Test nicht bloß alles durchwinkt."""
    kaputt = "0.9.0b23-live.1"
    assert not PEP440.match(kaputt)
    assert re.search(r"\d+(a|b|rc)\d+", kaputt) and "-" in kaputt

    for gut in ("0.9.0b23", "0.9.0b23.post1", "0.7.3-live.10", "1.2.3"):
        assert PEP440.match(gut) or SEMVER_PRERELEASE.match(gut), gut


def _main() -> None:
    stubs.run_tests(globals(), "Manifest version")


if __name__ == "__main__":
    _main()
