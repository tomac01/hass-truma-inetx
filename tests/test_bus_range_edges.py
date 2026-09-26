#!/usr/bin/env python3
"""Die Ränder der Wertebereichsprüfung in ``bus.py``, offline geprüft.

Kein Home Assistant, keine Hardware: HA ist gestubbt, ``bus.py`` braucht
ohnehin nichts davon, und die Plattformen werden echt gegen einen
Stub-Coordinator geladen -- dessen ``async_write`` validiert wie der echte,
also fällt ein Schreibvorgang auf, den der eigene Validator ablehnt.

Warum diese Datei existiert. ``bus.validate_write`` ist die Validierung
letzter Instanz: hat kein Gerät den Parameter beschrieben, entscheidet
``PARAM_VALIDATION``. Geprüft wurde diese Tabelle bisher nur in der Mitte
(200) und weit daneben (900) -- also nie an der Kante. Genau die Kante ist
aber der *Anschlag, den die Entität dem Nutzer selbst anbietet*:

* ``climate`` bietet ohne Panel-Beschreibung ``off``/``heat``/``fan_only`` an,
  und ``fan_only`` ist die 5 aus ``RoomClimate.Mode``,
* die Lüfterstufe ``off`` ist die 0 aus ``AirCirculation.FanLevel``, und der
  Schieber daneben trägt dieselbe 0 als ``native_min_value``,
* 5 °C im Heizen und 16 °C im Kühlen bzw. Automatik sind die unteren
  Anschläge aus ``climate._FALLBACK_SETPOINT_RANGE``, 30 °C der obere,
* ein Schalter bietet immer beides an, Ein *und* Aus -- ``FreshWater.Autofill``
  wird von keinem anderen Test je geschrieben.

Deshalb stellt hier jede Zusicherung den **angebotenen** Wert gegen den
**akzeptierten**: nicht die Konstante gegen sich selbst, sondern die Entität
gegen den Validator. Läuft eine der beiden Stellen weg, wird der Test rot --
das ist die Bugklasse aus #11 und #23, bei der die Entität etwas anbot, das
der eigene Validator dann verweigerte.

Dazu kommen die übrigen ungepinnten Zusicherungen desselben Moduls: eine halb
beschriebene und eine entartete Range (beide an echten Panel-Daten gemessen),
der Rückgabewert von ``learn_param``, die Vorgabe von ``Bus.connected``, die
Kopfzeile des Dumps und der Live-Pfad des Dump-Werkzeugs, den bisher kein Test
betreten hat.

Run: ``python3 tests/test_bus_range_edges.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201
# Der Elektroblock mit Pumpe und Tanks, das Dachklimagerät und die
# Bluetooth-Seite des Panels -- nur benannt, weil kein Test eine Adresse aus
# der Quelle beziehen darf; jede wird beim Neu-Anlernen umnummeriert.
BOARD = 0x0405
ROOF_AC = 0x0406
BLE_MGMT = 0x0601

stubs.install_homeassistant()
BUS = stubs.load("bus")
CONST = stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
PROFILES = stubs.load("profiles")
stubs.load("entity")
SWITCH = stubs.load("switch")
NUMBER = stubs.load("number")
CLIMATE = stubs.load("climate")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def _climate(coordinator):
    """Die Klima-Entität, so gebaut wie die Plattform sie baut."""
    made = stubs.setup_platform(CLIMATE, coordinator)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    assert len(made) == 1, made
    return made[0]


def _enum(names: dict, unavailable: tuple = ()) -> list:
    """Ein Enum in der Gestalt, in der das Panel es wirklich sendet."""
    return [
        {"n": name, "a": value not in unavailable, "v": value}
        for value, name in names.items()
    ]


def _owner_for(topic: str) -> int:
    """Das Gerät, auf dem ein Topic plausibel sitzt.

    Für den Validator ist die Adresse gleichgültig, solange nichts beschrieben
    ist -- die Tabelle greift ohnehin. Sie wird trotzdem plausibel gewählt,
    damit die Tests nicht behaupten, der Heizer führe die Wasserpumpe.
    """
    if topic in ("FreshWater", "GreyWater", "Switches"):
        return BOARD
    if topic in ("Panel", "TimerConfig", "RoomClimate"):
        return PANEL
    if topic == "AirCooling":
        return ROOF_AC
    return HEATER


# -- die Anschläge, die die Entitäten selbst anbieten ---------------------


def test_every_mode_the_climate_offers_survives_its_own_validator() -> None:
    """#11/#23 in allgemeiner Form, auf einem Bus, auf dem nichts beschrieben ist.

    Ohne Panel-Beschreibung bietet die Entität ``_DEFAULT_HVAC_MODES`` an. Was
    sie anbietet, muss sie auch schreiben können: ``fan_only`` ist die 5 in
    ``PARAM_VALIDATION["RoomClimate.Mode"]``, und eine Tabelle ohne diese 5
    macht genau den Modus unerreichbar, den die Entität selbst im Frontend
    zeigt.
    """
    coordinator = _coordinator()
    climate = _climate(coordinator)

    offered = climate.hvac_modes
    assert offered, "die Klima-Entität bietet gar keinen Modus an"
    written = []
    for mode in offered:
        coordinator.writes.clear()
        # Schlägt der Validator zu, wirft der Stub-Coordinator -- genau das
        # wäre der Fehler.
        asyncio.run(climate.async_set_hvac_mode(mode))
        assert coordinator.writes, f"{mode} wurde angeboten und nicht geschrieben"
        written.append(coordinator.writes[-1])

    assert written == [
        (HEATER, "RoomClimate", "Mode", CLIMATE._HVAC_TO_MODE[mode])
        for mode in offered
    ], written
    # Und die Gegenrichtung, die dasselbe Paar von der anderen Seite hält: die
    # Tabelle erlaubt genau die Werte, die die Entität anbietet. Der Kommentar
    # in bus.py sagt es ausdrücklich -- ein weiterer Wert liesse nur einen
    # Write auf ein Fahrzeug durch, das den Modus nicht hat.
    assert sorted(BUS.PARAM_VALIDATION["RoomClimate.Mode"]) == sorted(
        {CLIMATE._HVAC_TO_MODE[mode] for mode in CLIMATE._DEFAULT_HVAC_MODES}
    ), (
        "PARAM_VALIDATION['RoomClimate.Mode'] und climate._DEFAULT_HVAC_MODES "
        "sind auseinandergelaufen -- die beiden gehören zusammen bewegt"
    )


def test_every_fan_step_the_climate_offers_survives_its_own_validator() -> None:
    """Die Lüfterstufe ``off`` ist die 0, und die ist der untere Rand der Range.

    ``_FALLBACK_FAN_LEVELS`` ist (0, 10), solange das Gerät keine eigene Range
    beschreibt; die Entität bietet daraus ``off`` und 1..10 an. Beide Enden
    müssen durch ``PARAM_VALIDATION["AirCirculation.FanLevel"]`` gehen, sonst
    lehnt der eigene Validator die Stufe ab, die im Frontend steht.
    """
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.report("AirCirculation", "FanLevel", 2, HEATER)

    offered = climate.fan_modes
    assert offered, "die Klima-Entität bietet keine Lüfterstufe an"
    levels = []
    for fan_mode in offered:
        coordinator.writes.clear()
        asyncio.run(climate.async_set_fan_mode(fan_mode))
        assert coordinator.writes, f"Stufe {fan_mode} angeboten und nicht geschrieben"
        addr, topic, param, value = coordinator.writes[-1]
        assert (addr, topic, param) == (HEATER, "AirCirculation", "FanLevel")
        levels.append(value)

    low, high = CLIMATE._FALLBACK_FAN_LEVELS
    assert levels == list(range(int(low), int(high) + 1)), levels
    # Die Enden ausdrücklich: das ist der Anschlag, an dem die Mutation sitzt.
    assert levels[0] == 0, "die Entität bietet kein Aus mehr an"
    assert levels[-1] == high


def test_the_fan_slider_can_be_moved_to_both_of_its_own_ends() -> None:
    """Derselbe Parameter als Schieber: was er anbietet, muss er schreiben können."""
    coordinator = _coordinator()
    made = stubs.setup_platform(NUMBER, coordinator)
    coordinator.report("AirCirculation", "FanLevel", 4, HEATER)
    fan = _by_key(
        [e for e in made if type(e).__name__ == "TrumaNumber"], "fan_level"
    )

    for end in (fan.native_min_value, fan.native_max_value):
        coordinator.writes.clear()
        asyncio.run(fan.async_set_native_value(end))
        assert coordinator.writes == [
            (HEATER, "AirCirculation", "FanLevel", int(end))
        ], f"der Schieber bietet {end} an und der Validator nimmt es nicht"

    assert (fan.native_min_value, fan.native_max_value) == (0, 10)


def test_every_slider_end_any_row_offers_is_accepted() -> None:
    """Dieselbe Kopplung für jede Schieber-Zeile der Tabelle, nicht nur den Lüfter."""
    coordinator = _coordinator()
    made = stubs.setup_platform(NUMBER, coordinator)
    rows = [
        (topic, param)
        for (topic, param), row_list in PROFILES.ROWS.items()
        for row in row_list
        if row.platform == "number"
    ]
    assert rows, "keine Schieber-Zeile gefunden -- die Tabelle hat sich bewegt"
    for topic, param in rows:
        coordinator.report(topic, param, 0, _owner_for(topic))

    sliders = [e for e in made if type(e).__name__ == "TrumaNumber"]
    assert len(sliders) == len(rows), [
        getattr(e, "_attr_translation_key", None) for e in sliders
    ]
    for slider in sliders:
        for end in (slider.native_min_value, slider.native_max_value):
            coordinator.writes.clear()
            asyncio.run(slider.async_set_native_value(end))
            assert coordinator.writes, (
                f"{slider._topic}.{slider._param} bietet {end} an, und der "
                "eigene Validator lehnt es ab"
            )


def test_every_slider_row_brings_its_own_fallback_range() -> None:
    """Keine Schieber-Zeile darf auf den namenlosen Bereich 0..100 fallen.

    ``TrumaNumber._bounds`` endet mit ``self.row.fallback_bounds or (0, 100)``.
    Heute ist die rechte Seite unerreichbar -- jede der vier Zeilen bringt
    ihren eigenen Bereich mit --, und ein Mutationstest meldet sie folglich
    als unerreichte Stelle. Das ist richtig und soll so bleiben: geprüft wird
    hier nicht die 100, sondern dass niemand sie nötig hat.

    Ohne diese Prüfung fiele eine neu eingetragene Zeile ohne eigenen Bereich
    still auf 0..100 -- ein Schieber mit falschen Anschlägen, der nichts
    kaputt macht und deshalb niemandem auffällt, bis jemand ihn bis zum
    Anschlag zieht und der eigene Validator den Wert ablehnt. Genau die
    Bugklasse, um die es in dieser Datei geht.
    """
    rows = [
        (topic, param, row)
        for (topic, param), row_list in PROFILES.ROWS.items()
        for row in row_list
        if row.platform == "number"
    ]
    assert rows, "keine Schieber-Zeile gefunden -- die Tabelle hat sich bewegt"
    for topic, param, row in rows:
        assert row.fallback_bounds is not None, (
            f"{topic}.{param} bringt keinen eigenen Bereich mit und fiele "
            f"auf 0..100 zurück"
        )
        low, high = row.fallback_bounds
        assert low < high, f"{topic}.{param} hat den entarteten Bereich {low}..{high}"


def test_each_setpoint_end_the_climate_offers_is_accepted() -> None:
    """Die Anschläge des Sollwert-Schiebers, je Modus, gegen die Tabelle.

    Der Schieber holt seine Grenzen aus ``_FALLBACK_SETPOINT_RANGE``, solange
    das Feld sein eigenes Gerät nicht beschrieben hat: 5 °C im Heizen, 16 °C
    im Kühlen und in Automatik, 30 °C oben. Auf dem Draht sind das 50, 160 und
    300 -- also genau die Ränder von ``AirHeating.TgtTemp``,
    ``AirCooling.TgtTemp`` und ``RoomClimate.TgtTemp``. Der Nutzer kann diese
    Anschläge wählen; der Write darf nicht am eigenen Validator scheitern.
    """
    # (Modus-Wert, wer den Sollwert führt, Topic) -- gemessen in
    # tests/test_cooling_entities.py, hier nur wiederverwendet.
    cases = (
        (3, HEATER, "AirHeating"),
        (2, ROOF_AC, "AirCooling"),
        (1, PANEL, "RoomClimate"),
    )
    for mode, addr, topic in cases:
        coordinator = _coordinator()
        climate = _climate(coordinator)
        # Das Feld muss veröffentlicht sein, sonst bleibt der Sollwert beim
        # Heizer -- siehe climate._setpoint.
        coordinator.report(topic, "TgtTemp", 220, addr)
        coordinator.report("RoomClimate", "Mode", mode, PANEL)

        offered = (climate.min_temp, climate.max_temp)
        assert offered == CLIMATE._FALLBACK_SETPOINT_RANGE[topic], (
            f"{topic}: die Entität bietet {offered} an, der Rückfall sagt "
            f"{CLIMATE._FALLBACK_SETPOINT_RANGE[topic]}"
        )
        for degrees in offered:
            coordinator.writes.clear()
            asyncio.run(climate.async_set_temperature(temperature=degrees))
            assert coordinator.writes == [
                (addr, topic, "TgtTemp", int(round(degrees * 10)))
            ], (
                f"{topic}: {degrees} °C steht am Anschlag des Schiebers und "
                "wird vom eigenen Validator abgelehnt"
            )


def test_the_table_edge_matches_the_climate_fallback_it_guards() -> None:
    """Die Tabellenkante gegen die Entitätsgrenze, in Zahlen statt über Writes.

    Der Teil, der die Mutation dauerhaft tötet, ohne eine Konstante gegen sich
    selbst zu prüfen: für jedes Topic mit einem Rückfallbereich in ``climate``
    muss der Draht-Wert beider Enden durch die Tabelle gehen und je ein Schritt
    darüber hinaus scheitern. Verschiebt jemand die eine Konstante ohne die
    andere, schlägt das hier fehl.
    """
    bus = BUS.Bus()
    for topic, (low, high) in CLIMATE._FALLBACK_SETPOINT_RANGE.items():
        addr = _owner_for(topic)
        for degrees, offset, expected in (
            (low, 0, True), (low, -1, False), (high, 0, True), (high, +1, False),
        ):
            wire = int(degrees * 10) + offset
            ok, msg = bus.validate_write(addr, topic, "TgtTemp", wire)
            assert ok is expected, (
                f"{topic}.TgtTemp {wire}: erwartet {expected}, bekam "
                f"{ok} ({msg})"
            )


def test_every_range_in_the_table_accepts_both_of_its_ends() -> None:
    """Jede Tupelregel an ihren beiden Kanten, und je einen Schritt daneben.

    Die Funktionsseite derselben Sache: ``validate_against_table`` vergleicht
    mit ``<`` und ``>``, nicht mit ``<=``/``>=``. Geprüft wurde bisher nur
    mitten im Bereich und weit daneben.
    """
    bus = BUS.Bus()
    ranges = {
        key: rule
        for key, rule in BUS.PARAM_VALIDATION.items()
        if isinstance(rule, tuple)
    }
    assert ranges, "keine Tupelregel gefunden -- PARAM_VALIDATION hat sich bewegt"
    for key, (low, high) in ranges.items():
        topic, _, param = key.partition(".")
        addr = _owner_for(topic)
        assert bus.validate_write(addr, topic, param, low)[0] is True, (
            f"{key}: der untere Rand {low} wird abgelehnt"
        )
        assert bus.validate_write(addr, topic, param, high)[0] is True, (
            f"{key}: der obere Rand {high} wird abgelehnt"
        )
        assert bus.validate_write(addr, topic, param, low - 1)[0] is False, (
            f"{key}: {low - 1} liegt unter dem Bereich und geht durch"
        )
        assert bus.validate_write(addr, topic, param, high + 1)[0] is False, (
            f"{key}: {high + 1} liegt über dem Bereich und geht durch"
        )


def test_a_refused_range_write_names_the_range_it_broke() -> None:
    """Die Meldung liest der Nutzer als HomeAssistantError.

    Der Geräte-Pfad sichert seinen Text zu ("accepts 1-6"), der Tabellen-Pfad
    bisher nicht -- eine Meldung, die "300-300" nennt, benennt eine Grenze,
    die es nicht gibt.
    """
    bus = BUS.Bus()
    ok, msg = bus.validate_write(PANEL, "RoomClimate", "TgtTemp", 900)
    assert not ok
    low, high = BUS.PARAM_VALIDATION["RoomClimate.TgtTemp"]
    assert f"{low}-{high}" in msg, msg
    assert "900" in msg, msg


# -- Schalter: beide Richtungen, auch die, die kein Test schreibt ---------


def test_the_pump_switch_can_be_turned_off_as_well_as_on() -> None:
    """Ein Schalter, der sich nicht ausschalten lässt, ist kein Schalter.

    Bisher schaltete nur ein Test die Pumpe *ein* und prüfte, dass die 2
    abgelehnt wird -- die 0, die ``async_turn_off`` schreibt, kam nie vor.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    coordinator.report("Switches", "FreshWaterPump", 0, BOARD)
    pump = _by_key(made, "water_pump")

    asyncio.run(pump.async_turn_on())
    asyncio.run(pump.async_turn_off())
    assert coordinator.writes == [
        (BOARD, "Switches", "FreshWaterPump", 1),
        (BOARD, "Switches", "FreshWaterPump", 0),
    ], coordinator.writes


def test_the_autofill_switch_works_in_both_directions() -> None:
    """``FreshWater.Autofill`` wird von keinem anderen Test geschrieben.

    Und gerade dieser Schalter soll nicht angelassen werden (siehe den
    Kommentar an der Zeile in ``profiles.py``), das Ausschalten ist also der
    wichtigere der beiden Writes.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    coordinator.report("FreshWater", "Autofill", 0, BOARD)
    autofill = _by_key(made, "water_autofill")

    asyncio.run(autofill.async_turn_on())
    assert coordinator.writes == [(BOARD, "FreshWater", "Autofill", 1)], (
        "der Nachfüll-Knopf des Panels ist nicht mehr auslösbar"
    )
    asyncio.run(autofill.async_turn_off())
    assert coordinator.writes[-1] == (BOARD, "FreshWater", "Autofill", 0)


def test_every_switch_row_accepts_both_of_its_writes() -> None:
    """Die allgemeine Form: jeder Schalter bietet Ein und Aus an, immer beides."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    rows = [
        (topic, param)
        for (topic, param), row_list in PROFILES.ROWS.items()
        for row in row_list
        if row.platform == "switch"
    ]
    assert rows, "keine Schalter-Zeile gefunden -- die Tabelle hat sich bewegt"
    for topic, param in rows:
        coordinator.report(topic, param, 0, _owner_for(topic))
    assert len(made) == len(rows), [
        getattr(e, "_attr_translation_key", None) for e in made
    ]

    for entity in made:
        for direction, expected in ((entity.async_turn_on, 1),
                                    (entity.async_turn_off, 0)):
            coordinator.writes.clear()
            asyncio.run(direction())
            assert coordinator.writes == [
                (entity._addr, entity._topic, entity._param, expected)
            ], (
                f"{entity._topic}.{entity._param}: der Schalter bietet "
                f"{expected} an und der eigene Validator lehnt es ab"
            )


# -- Beschreibungen, die keine sind --------------------------------------


def test_a_half_described_range_falls_back_to_the_table() -> None:
    """Ein Panel, das nur ein Ende beschreibt, beschreibt keine Range.

    Gemessen: das Fixture ``Weird`` in ``test_param_meta.py`` trägt genau
    ``{"max": 3}``. Gäbe ``bounds()`` dafür ``(None, 3)`` zurück, stürbe
    ``validate_write`` am Vergleich ``None <= int``, und der Schieber daneben
    bekäme ``None`` als unteren Anschlag.
    """
    bus = BUS.Bus()
    bus.learn_param("AirCirculation", "FanLevel", {"max": 3}, HEATER)
    heater = bus.device(HEATER)

    assert heater.meta("AirCirculation", "FanLevel") == {"max": 3}
    assert heater.bounds("AirCirculation", "FanLevel") is None
    # Und der Write fällt auf die Tabelle zurück, statt zu stürzen.
    ok, msg = bus.validate_write(HEATER, "AirCirculation", "FanLevel", 4)
    assert ok, msg


def test_a_degenerate_range_is_no_range() -> None:
    """min == max ist an echten Panel-Daten gemessen und keine Beschreibung.

    In ``dumps/combi4-inetx-pro/water-boost.json`` beschreibt das Panel
    ``ACCAirHeating.Mode`` mit ``min: 1, max: 1``. Gälte das als Range, hätte
    der Schieber einen einzigen Wert, die Klima-Karte eine einzige Lüfterstufe
    und der Validator lehnte alles andere mit "accepts 1-1" ab.
    """
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.describe("AirCirculation", "FanLevel", HEATER, v=1, min=1, max=1)

    assert coordinator.data.device(HEATER).bounds(
        "AirCirculation", "FanLevel"
    ) is None
    # Die Klima-Karte fällt auf die Rückfall-Liste zurück, nicht auf ["1"].
    low, high = CLIMATE._FALLBACK_FAN_LEVELS
    assert climate.fan_modes == [
        "off", *[str(n) for n in range(int(low) + 1, int(high) + 1)]
    ], climate.fan_modes
    # ...und der Write läuft wieder gegen die Tabelle.
    ok, msg = coordinator.data.validate_write(
        HEATER, "AirCirculation", "FanLevel", 4
    )
    assert ok, msg


def test_an_enum_value_without_the_available_flag_is_still_offered() -> None:
    """Ohne ``a`` gilt ein Enum-Wert als verfügbar -- alle Fixtures setzen es.

    Gälte er als nicht verfügbar, lieferte ``allowed_values`` None, und die
    Select-Optionen bzw. ``climate.hvac_modes`` fielen auf die Rückfall-Listen
    zurück: genau der Verlust, gegen den #23 geschrieben wurde.
    """
    bus = BUS.Bus()
    bus.learn_param(
        "WaterHeating", "Mode",
        {"enum": [{"n": "Eco", "v": 0}, {"n": "Comfort", "v": 1}]},
        HEATER,
    )
    heater = bus.device(HEATER)

    assert "enum_unavailable" not in heater.meta("WaterHeating", "Mode")
    assert heater.allowed_values("WaterHeating", "Mode") == [0, 1]
    # Die Gegenprobe: ein ausdrückliches ``a: 0`` wirkt weiterhin.
    bus.learn_param(
        "WaterHeating", "Mode",
        {"enum": _enum({0: "Eco", 1: "Comfort"}, unavailable=(1,))},
        HEATER,
    )
    assert heater.allowed_values("WaterHeating", "Mode") == [0]


def test_learn_param_reports_only_what_is_actually_new() -> None:
    """Der dokumentierte Rückgabewert: "True if that is new".

    Das Log-Gate in ``session.learn_param`` hängt daran. Invertiert loggt es
    die Debug-Zeile pro Parameter pro Frame dauerhaft, statt einmal je
    Installation -- und eine nur einmal beschriebene Beschreibung nie.
    """
    bus = BUS.Bus()
    entry = {"type": 1, "perm": 1, "enum": _enum({0: "Off", 1: "On"})}

    assert bus.learn_param("System", "DescribedState", entry, HEATER) is True
    assert bus.learn_param("System", "DescribedState", entry, HEATER) is False
    assert bus.learn_param(
        "System", "DescribedState", {**entry, "min": 0, "max": 10}, HEATER
    ) is True


def test_a_description_with_no_source_invents_no_device() -> None:
    """Ein Frame ohne verwertbare Quelle darf kein Phantom-Gerät anlegen.

    ``session.learn_param`` wertet seine Log-Argumente unbedingt aus, und
    ``bus.device(src)`` legt fehlende Geräte an -- ein True für einen Frame,
    der keinem Gerät zuzuordnen ist, erfindet also einen leeren Eintrag auf
    dem Bus.
    """
    bus = BUS.Bus()
    assert bus.learn_param("System", "Homeless", {"min": 0, "max": 3}, None) is False
    assert bus.devices == {}, bus.devices
    # Und ein Frame ohne jede Beschreibung ist ebenfalls nichts Neues.
    assert bus.learn_param("System", "Undescribed", {"v": 1}, HEATER) is False
    assert bus.devices == {}, bus.devices


def test_a_fresh_bus_reports_no_link_and_its_entities_are_unavailable() -> None:
    """Vor der ersten Verbindung ist nichts verfügbar.

    ``entity.available`` ist ``super().available and self.bus.connected``.
    Wäre die Vorgabe True, wären alle Entitäten schon verfügbar, bevor je ein
    Link stand -- und kein Test prüft die Vorgabe, weil der eine, der in diese
    Nähe kommt, ``connected = False`` vorher selbst setzt.
    """
    assert BUS.Bus().connected is False

    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    coordinator.report("Switches", "FreshWaterPump", 0, BOARD)
    pump = _by_key(made, "water_pump")

    assert pump.available is False, "verfügbar, ohne dass je ein Link stand"
    coordinator.data.connected = True
    assert pump.available is True


# -- der Dump, Zeile für Zeile -------------------------------------------


def _dumped(bus) -> dict[str, str]:
    """Die Kopfzeile je Gerät, nach Adresse aufgeschlüsselt."""
    lines = BUS.dump(bus).splitlines()
    return {
        line.split()[0]: line for line in lines if line.startswith("0x")
    }


def test_the_dump_titles_each_device_by_its_own_name() -> None:
    """Die Kopfzeile selbst, nicht irgendein Vorkommen des Namens im Text.

    ``test_bus_dump_tool`` bleibt grün, wenn jedes Gerät "unnamed" heißt: der
    Panel-Name steht in derselben Ausgabe auch als *Wert* von
    ``Identify.Name``. Geprüft wird deshalb die Zeile, die mit der Adresse
    beginnt.
    """
    bus = BUS.Bus()
    bus.update("Identify", "Name", "iNet X Panel", PANEL)
    bus.update("Identify", "SerialNr", "23456789", PANEL)
    bus.update("System", "FlameStatus", 1, HEATER)
    bus.update("Identify", "Name", "Combi D 4 GEN2", HEATER)
    # Die Bluetooth-Seite des Panels benennt sich selbst nicht.
    bus.update("BleDeviceManagement", "NrFreeSlots", 1, BLE_MGMT)

    headers = _dumped(bus)
    assert set(headers) == {"0x0101", "0x0201", "0x0601"}, headers
    assert "iNet X Panel" in headers["0x0101"], headers["0x0101"]
    assert "23456789" in headers["0x0101"], headers["0x0101"]
    assert "unnamed" not in headers["0x0101"], headers["0x0101"]
    assert "Combi D 4 GEN2" in headers["0x0201"], headers["0x0201"]
    assert "unnamed" not in headers["0x0201"], headers["0x0201"]
    # Nur das namenlose Gerät trägt den Platzhalter, und nicht "None".
    assert "unnamed" in headers["0x0601"], headers["0x0601"]
    assert "None" not in headers["0x0601"], headers["0x0601"]


def test_the_dump_survives_a_half_described_range() -> None:
    """Ein Parameter mit nur ``max`` darf den Dump nicht abbrechen.

    Dass solche Beschreibungen vorkommen, belegt das Fixture ``Weird`` in
    ``test_param_meta.py``. Ein Dump, der daran mit KeyError stirbt, ist genau
    dann weg, wenn man ihn braucht.
    """
    bus = BUS.Bus()
    bus.learn_param("AirCirculation", "FanLevel", {"max": 3}, HEATER)
    bus.update("AirCirculation", "FanLevel", 2, HEATER)

    text = BUS.dump(bus)
    assert "AirCirculation.FanLevel" in text, text
    # Und mit beiden Enden steht die Notiz weiterhin da.
    bus.learn_param("AirCirculation", "FanLevel", {"min": 0, "max": 3}, HEATER)
    assert "0..3" in BUS.dump(bus)


# -- der Reader und das Werkzeug -----------------------------------------


def _download(devices: dict, unattributed: dict, assigned: str = "0x0501") -> dict:
    """Ein 0.9-Download in der Gestalt, die ``diagnostics.py`` schreibt.

    Von Hand, aber selbstsichernd: läse ``from_diagnostics`` diese Gestalt
    nicht mehr, käme ein leerer Bus zurück und die Zusicherungen unten fielen
    -- ein stillschweigendes Durchlaufen ist nicht möglich. Die Gestalt selbst
    ist in ``test_bus_dump_tool.py`` am echten Schreiber festgenagelt.
    """
    return {
        "bus": {
            "connected": True,
            "discovered": True,
            "last_update": 0.0,
            "assigned_addr": assigned,
            "devices": {
                addr: {
                    "addr": addr,
                    "params": params,
                    "param_meta": {},
                    "last_seen": 0.0,
                }
                for addr, params in devices.items()
            },
            "contested_topics": {},
            "unattributed": unattributed,
        }
    }


def test_a_download_with_devices_but_nothing_unattributed_is_read() -> None:
    """Ein völlig normaler Download -- Geräte da, nichts Unattributiertes.

    Der Testdownload trägt zufällig beides, deshalb fiele es nicht auf, wenn
    das Werkzeug einen solchen Download mit "no bus in this file" und
    Rückgabewert 1 abwiese.
    """
    payload = _download({"0x0101": {"Identify.Name": "iNet X Panel"}}, {})
    assert BUS.from_diagnostics(payload).devices, (
        "der Reader liest diese Gestalt nicht mehr -- siehe test_bus_dump_tool"
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "diagnostics.json"
        path.write_text(json.dumps(payload), "utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = BUS._main([str(path)])

    assert code == 0, out.getvalue()
    assert "0x0101" in out.getvalue(), out.getvalue()
    assert "iNet X Panel" in out.getvalue(), out.getvalue()


def test_the_assigned_address_stays_an_int() -> None:
    """Der Reader sagt ``assigned_addr: int`` zu, und drei Tests glauben ihm.

    Erreichbar, weil ``from_diagnostics`` bei leerem Gerätespeicher die
    0.9-Section selbst als "state" durch den Rückfall schickt -- und dort
    steht die Adresse als Hexstring, wie ``diagnostics.py`` sie schreibt.
    """
    payload = _download({}, {"System.Plugged": 1}, assigned="0x0502")
    bus = BUS.from_diagnostics(payload)

    assert isinstance(bus.assigned_addr, int), (
        f"assigned_addr kam als {type(bus.assigned_addr).__name__} zurück"
    )
    assert bus.assigned_addr == 0x0502
    # Und eine 0 ist keine zugewiesene Adresse: sie darf die Voreinstellung
    # nicht ersetzen.
    older = BUS.from_diagnostics(
        {"state": {"raw_params": {"System.Plugged": 1}, "assigned_addr": 0}}
    )
    assert older.assigned_addr == BUS.DEV_APP_DEFAULT


# -- der Live-Pfad des Werkzeugs, den kein Test betreten hat -------------


class _Advert:
    """Eine Werbebotschaft, so weit ``looks_like_panel`` sie liest."""

    def __init__(self, service_uuids: tuple = ()) -> None:
        self.service_uuids = list(service_uuids)


class _Seen:
    """Ein gefundenes BLE-Gerät. Adressen nur aus doppelten Zeichen."""

    def __init__(self, name: str | None, address: str) -> None:
        self.name = name
        self.address = address


class _FakeClient:
    """``TrumaBleClient``, so weit ``_live`` ihn benutzt."""

    def __init__(self, identity: dict) -> None:
        self.identity = identity
        self.connected_to = None
        self.disconnected = False

    def on_data(self, _callback) -> None:
        pass

    async def connect(self, device) -> None:
        self.connected_to = device

    async def disconnect(self) -> None:
        self.disconnected = True


async def _no_startup(*_args) -> None:
    """``session.run_startup`` ohne Panel dahinter."""


def _install_live_stubs() -> None:
    """Die drei Module, die ``_live`` erst beim Aufruf importiert."""
    stubs.mod("truma_pkg.ble", TrumaBleClient=_FakeClient)
    session = stubs.mod(
        "truma_pkg.session", handle_frame=lambda *a: None, run_startup=_no_startup
    )
    # ``from . import session`` fragt zuerst das Paket selbst.
    sys.modules["truma_pkg"].session = session


def _scan(candidates: tuple, found=None) -> list:
    """Ein gefälschter Scanner; liefert die Liste der akzeptierten Geräte."""
    accepted: list = []

    class _Scanner:
        @staticmethod
        async def find_device_by_filter(predicate, timeout=None, **_kwargs):
            for device, advert in candidates:
                if predicate(device, advert):
                    accepted.append(device)
            return found

    stubs.mod("bleak", BleakScanner=_Scanner)
    return accepted


def _run_live(name, candidates, found=None):
    """``_live`` laufen lassen, ohne Ausgaben in den Testlauf zu schütten."""
    _install_live_stubs()
    accepted = _scan(candidates, found)
    with contextlib.redirect_stderr(io.StringIO()):
        bus = asyncio.run(BUS._live(name, {"username": "truma dump"}, 0))
    return accepted, bus


def _panel(suffix: str) -> _Seen:
    """Ein Panel mit einem zur Laufzeit gebauten Namen.

    Zur Laufzeit gebaut, damit der Name kein mit dem Suchbegriff identisches
    Objekt ist: ein Vergleich, der auf Identität statt auf Gleichheit
    umkippt, fiele sonst nicht auf.
    """
    return _Seen(f"{CONST.LOCAL_NAME_PREFIX}-{suffix}", "AA:BB:CC:DD:EE:FF")


def test_only_a_panel_is_accepted_by_the_live_scan() -> None:
    """Sonst verbindet sich das Werkzeug mit dem erstbesten Fremdgerät.

    ``_is_panel`` ist reine Prädikatlogik ohne Hardware dahinter -- unerreicht
    nur, weil kein Test ``_live`` betreten hat.
    """
    panel = _panel("AABBCC")
    # Ein Fremdgerät, das eine UUID ausserhalb von Trumas Raum wirbt.
    foreign = _Seen("Nicht-Truma", "11:22:33:44:55:66")
    foreign_advert = _Advert(("0000180f-0000-1000-8000-00805f9b34fb",))

    # Ohne --name: das Panel wird genommen, das Fremdgerät nicht.
    accepted, _bus = _run_live(
        None, ((panel, _Advert()), (foreign, foreign_advert)), found=panel
    )
    assert accepted == [panel], [device.name for device in accepted]

    # Mit --name: nur das gleichnamige Panel, und ein anders benanntes nicht.
    other = _panel("DDEEFF")
    accepted, _bus = _run_live(
        panel.name, ((panel, _Advert()), (other, _Advert())), found=panel
    )
    assert accepted == [panel], [device.name for device in accepted]

    accepted, _bus = _run_live(
        panel.name, ((other, _Advert()),), found=panel
    )
    assert accepted == [], "der --name-Filter nimmt ein fremdes Panel an"


def test_an_empty_scan_explains_itself_and_a_found_panel_does_not() -> None:
    """Kein Panel gefunden ist eine Erklärung, kein AttributeError."""
    panel = _panel("AABBCC")
    _install_live_stubs()
    _scan(((panel, _Advert()),), found=None)
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            asyncio.run(BUS._live(None, {"username": "truma dump"}, 0))
        except SystemExit as stop:
            assert "no panel found" in str(stop), stop
        except Exception as other:  # noqa: BLE001 -- gerade der Punkt
            # Ohne die Prüfung läuft der leere Scan in ``device.name`` und
            # damit in einen AttributeError statt in die Erklärung.
            raise AssertionError(
                f"ein leerer Scan endet in {type(other).__name__}: {other}"
            ) from other
        else:
            raise AssertionError("ein leerer Scan lief einfach weiter")

    # Und umgekehrt: ein gefundenes Panel darf nicht abbrechen.
    _accepted, bus = _run_live(None, ((panel, _Advert()),), found=panel)
    assert isinstance(bus, BUS.Bus)


def test_the_stored_identity_is_handed_to_the_live_dump() -> None:
    """``--identity`` ist die einzige Art, einen Live-Dump zu autorisieren.

    Home Assistant wickelt seinen Speicher in ``{"version", "data"}``; aus dem
    Inneren gehören genau die drei Felder weitergegeben, die das Panel kennt.
    """
    stored = {
        "version": 1,
        "data": {
            "muid": "muid-placeholder",
            "uuid": "uuid-placeholder",
            "username": "placeholder",
            "irrelevant": "bleibt draußen",
        },
    }
    captured: dict = {}

    async def _fake_live(name, identity, settle):
        captured.update(name=name, identity=identity, settle=settle)
        return BUS.Bus()

    original = BUS._live
    BUS._live = _fake_live
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "truma_inetx_entry.json"
            path.write_text(json.dumps(stored), "utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = BUS._main(["--live", "--identity", str(path)])
    finally:
        BUS._live = original

    assert code == 0
    assert captured["identity"] == {
        "muid": "muid-placeholder",
        "uuid": "uuid-placeholder",
        "username": "placeholder",
    }, captured["identity"]


def _main() -> None:
    stubs.run_tests(globals(), "bus range edges")


if __name__ == "__main__":
    _main()
