#!/usr/bin/env python3
"""Offline-Prüfungen der Kartenregistrierung in ``__init__.py``.

Kein Home Assistant, keine Hardware: die drei HA-Module, die nur diese eine
Funktion braucht, stehen unten als Doppelgänger, und ``__init__.py`` wird echt
dagegen geladen.

Warum diese Datei. ``__init__.py`` war das am schlechtesten abgedeckte Modul
der Integration -- ein Mutationstest erkannte eine von fünf Änderungen. Der
Grund ist nicht Nachlässigkeit, sondern Zuschnitt: ``async_setup_entry`` legt
einen echten Coordinator an und startet eine BLE-Sitzung, und ein Test dafür
wäre ein Integrationstest mit einem Dutzend Doppelgängern. ``_async_register_card``
dagegen hängt an nichts davon und ist für sich prüfbar.

Was hier festgehalten wird:

1. Die Karte wird einmal registriert und nicht bei jedem Konfigurationseintrag
   erneut -- Home Assistant lehnt einen doppelt registrierten statischen Pfad
   ab, und ein zweites Fahrzeug am selben Server ist kein Sonderfall.
2. Die URL trägt die Version, und die Datei darf zwischengespeichert werden.
   Beides gehört zusammen: der Zwischenspeicher ist nur deshalb vertretbar,
   weil die URL sich bei jeder Aktualisierung ändert.

Offen und hier nicht geprüft: die Erreichbarkeitsprüfung in
``async_setup_entry`` (die Warnung, wenn das Panel gerade nicht funkt). Sie
sitzt mitten im Aufbau und wäre erst zu prüfen, wenn sie in einer eigenen
Funktion stünde.

Run: ``python3 tests/test_card_registration.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

stubs.install_homeassistant()

# Was nur ``__init__.py`` braucht, steht hier und nicht in
# ``install_homeassistant``: so ist an der Testdatei zu sehen, wovon sie
# abhängt, und keine andere Datei erbt es ungefragt.
JS_URLS: list[str] = []
STATIC_PATHS: list = []


class _StaticPathConfig:
    """Der Datensatz, den ``async_register_static_paths`` entgegennimmt."""

    def __init__(self, url_path: str, path: str, cache_headers: bool) -> None:
        self.url_path = url_path
        self.path = path
        self.cache_headers = cache_headers


async def _async_get_integration(_hass, _domain):
    return SimpleNamespace(version="0.9.0")


stubs.mod("homeassistant.components.bluetooth",
          async_ble_device_from_address=lambda *a, **k: None)
stubs.mod("homeassistant.components.frontend",
          add_extra_js_url=lambda _hass, url: JS_URLS.append(url))
stubs.mod("homeassistant.components.http", StaticPathConfig=_StaticPathConfig)
stubs.mod("homeassistant.loader", async_get_integration=_async_get_integration)

stubs.load("bus")
CONST = stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
INIT = stubs.load("__init__")


class _Http:
    async def async_register_static_paths(self, configs) -> None:
        # Home Assistant lehnt einen bereits vergebenen Pfad ab; der
        # Doppelgänger tut dasselbe, sonst bliebe die doppelte Registrierung
        # hier folgenlos und die Prüfung darauf wertlos.
        for config in configs:
            for seen in STATIC_PATHS:
                if seen.url_path == config.url_path:
                    raise RuntimeError(f"path already registered: {config.url_path}")
            STATIC_PATHS.append(config)


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.http = _Http()


def _fresh() -> _Hass:
    JS_URLS.clear()
    STATIC_PATHS.clear()
    return _Hass()


def test_the_card_is_served_and_the_frontend_told_to_load_it() -> None:
    """Der gerade Weg: einmal aufgerufen, einmal registriert."""
    hass = _fresh()
    asyncio.run(INIT._async_register_card(hass))

    assert len(STATIC_PATHS) == 1, STATIC_PATHS
    assert STATIC_PATHS[0].url_path == INIT.CARD_URL
    assert STATIC_PATHS[0].path.endswith(INIT.CARD_FILENAME)
    assert JS_URLS == [f"{INIT.CARD_URL}?v=0.9.0"], JS_URLS


def test_a_second_config_entry_does_not_register_it_again() -> None:
    """Zwei Fahrzeuge an einem Server sind kein Sonderfall.

    Ohne das Merken läuft die Registrierung beim zweiten Eintrag erneut --
    Home Assistant lehnt den bereits vergebenen Pfad ab, und der Aufbau des
    zweiten Fahrzeugs scheitert an einer Karte, die längst da ist.
    """
    hass = _fresh()
    asyncio.run(INIT._async_register_card(hass))
    asyncio.run(INIT._async_register_card(hass))
    asyncio.run(INIT._async_register_card(hass))

    assert len(STATIC_PATHS) == 1, STATIC_PATHS
    assert len(JS_URLS) == 1, JS_URLS


def test_the_url_carries_the_version_and_the_file_may_be_cached() -> None:
    """Die beiden Hälften einer Entscheidung, darum in einer Prüfung.

    Der Dienst-Worker des Frontends hält diese Dateien wochenlang, und ein
    hartes Neuladen im Browser kommt daran nicht vorbei. Zwischenspeichern zu
    erlauben ist deshalb nur vertretbar, solange die URL sich bei jeder
    Aktualisierung ändert -- fällt eine der beiden Hälften weg, bekommt
    niemand die neue Karte, oder alle holen sie bei jedem Laden neu.
    """
    hass = _fresh()
    asyncio.run(INIT._async_register_card(hass))

    assert JS_URLS == [f"{INIT.CARD_URL}?v=0.9.0"], (
        "ohne Version in der URL erreicht eine aktualisierte Karte niemanden"
    )
    assert STATIC_PATHS[0].cache_headers is True, (
        "ohne Zwischenspeicher holt jeder Seitenaufruf die Karte neu, obwohl "
        "die URL sich nur bei einer Aktualisierung ändert"
    )


def test_the_double_would_notice_a_second_registration() -> None:
    """Gegenprobe zum Doppelgänger selbst.

    Meldet ``async_register_static_paths`` einen doppelten Pfad nicht, ist die
    Prüfung darauf wertlos -- sie zählte dann nur mit, statt den Fehler zu
    sehen, den Home Assistant wirklich wirft.
    """
    hass = _fresh()
    asyncio.run(INIT._async_register_card(hass))
    doppelt = _StaticPathConfig(INIT.CARD_URL, "/irgendwo", True)
    try:
        asyncio.run(hass.http.async_register_static_paths([doppelt]))
    except RuntimeError:
        return
    raise AssertionError("der Doppelgänger nahm denselben Pfad zweimal an")


def _main() -> None:
    stubs.run_tests(globals(), "Card registration")


if __name__ == "__main__":
    _main()
