#!/usr/bin/env python3
"""Prüft, dass ein Schreibvorgang die Rückmeldung des Geräts abwartet.

Warum das hier steht: Der Transport quittiert, dass das Panel den Frame
genommen hat -- nicht, dass es danach gehandelt hat. Gemessen wurde ein
quittierter Befehl, den die Heizung während des Nachlüftens schlicht
nicht ausführte. Für ein Bedienelement im Wohnmobil ist "quittiert, aber
nicht ausgeführt" der schlechteste Zustand.

Was der Test festnagelt:

1. ein reiner Transport-ACK ohne Geräteantwort gilt als Fehlschlag,
2. ein gecachter Wert im Bus bestätigt nicht -- es zählt nur eine Meldung,
3. eine Meldung von *vor* dem Frame bestätigt nicht,
4. eine Antwort vom falschen Busgerät bestätigt nicht,
5. WaterHeating.Active 1 ODER 2 bestätigt "ein", nur frisches 0 "aus",
6. die Ausnahme gilt ausschließlich für WaterHeating.Active,
7. ein schlafendes Gerät wird wiederholt, aber begrenzt,
8. beide Frameformen, die Werte tragen, zählen als Rückmeldung,
9. eine Mehrfach-Transaktion prüft am Ende den tatsächlichen Endzustand,
10. ein ungültiger Befehl wird geprüft, bevor irgendetwas gesendet wird,
11. die Rückfrage geht als Sonde hinaus, damit Schweigen die Sitzung nicht
    abreißt und aus drei Anläufen nicht lautlos einer wird,
12. der Befehl geht an das Gerät der Entität -- außer bei den Topics, die
    das Panel weiterreicht (#10),
13. zwei gleichzeitige Schreibvorgänge räumen sich nicht gegenseitig die
    Rückmeldung weg.

Run: ``python3 tests/test_confirmed_writes.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201

stubs.install_homeassistant()
stubs.stub_transport()
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          StartupFailed=RuntimeError,
          # Das Einsortieren in den Bus macht im Test das Gerätedouble selbst,
          # damit dieser Test nur vom Schreibpfad handelt.
          handle_frame=lambda *a, **kw: False)
stubs.stub_protocol(
    build_write_frame=lambda src, dest, topic, param, value: (
        "write", dest, topic, param, value
    ),
    build_v3_frame=lambda dest, src, ctrl, sub_type, corr_id, payload: (
        "discovery", dest
    ),
)
BUS = stubs.load("bus")
stubs.load("const")
stubs.load("operations")
COORD = stubs.load("coordinator")


class _Clock:
    """Virtuelle Uhr, damit Timeouts nicht in Echtzeit ablaufen."""

    def __init__(self) -> None:
        self.now = 100.0

    def time(self) -> float:
        return self.now


class _FastAsyncio:
    """``asyncio`` für den Coordinator, aber ohne echte Wartezeit.

    Ein Schreibvorgang gibt dem Gerät zwölf Sekunden je Anlauf. Echt
    abgewartet wäre diese Datei eine Minute lang beschäftigt; stattdessen
    rückt jedes ``sleep`` die virtuelle Uhr vor, an der der Coordinator
    seine Frist misst. Alles andere reicht das Modul unverändert durch.
    """

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        self._clock.now += seconds
        await asyncio.sleep(0)


def _discovery_response(src: int, topic: str, param: str, value: int) -> dict:
    """Die Antwort auf eine Parameter-Abfrage: verschachtelte Topics."""
    return {
        "src": src,
        "control_raw": 0x03,
        "sub_type": 0x84,
        "cbor": {"topics": [{"tn": topic, "parameters": [{"pn": param, "v": value}]}]},
    }


def _info_message(src: int, topic: str, param: str, value: int) -> dict:
    """Die unaufgeforderte Einzelmeldung: Werte direkt im CBOR."""
    return {
        "src": src,
        "control_raw": 0x03,
        "sub_type": 0x00,
        "cbor": {"tn": topic, "pn": param, "v": value},
    }


class _Client:
    """Ein Panel, das antwortet, wie der jeweilige Test es vorgibt."""

    connected = True
    assigned_addr = 0x0500

    def __init__(self, coordinator, responses, *, source=HEATER, answer_from=0,
                 shape=_discovery_response, aftermath=None) -> None:
        self.coordinator = coordinator
        self.responses = responses
        self.source = source
        self.answer_from = answer_from
        self.shape = shape
        self.aftermath = aftermath
        self.sends: list = []
        self.writes: list = []
        # Womit jeder Frame gesendet wurde. Ein Double, das ``probe`` nur
        # entgegennimmt, prüft die Regel nicht -- und sie trägt: ein nicht als
        # Sonde gesendeter Frame ohne Antwort beendet in ble.py die Sitzung.
        self.probes: list[tuple] = []
        self._pending: tuple | None = None

    async def send(self, frame, probe: bool = False) -> bool:
        self.sends.append(frame)
        self.probes.append((frame[0], probe))
        if frame[0] == "write":
            _kind, _dest, topic, param, _value = frame
            self.writes.append(frame)
            self._pending = (topic, param)
            if self.aftermath is not None:
                self.aftermath(self.coordinator, len(self.writes))
            return True
        # Die Parameter-Abfrage: hier meldet das Gerät seinen Stand -- sofern
        # es wach ist. Ein schlafender Brenner quittiert nur den Transport.
        pending, self._pending = self._pending, None
        if pending is None or len(self.writes) <= self.answer_from:
            return True
        reply = self.responses.get(pending)
        if reply is not None:
            self.coordinator.report(self.shape(self.source, *pending, reply))
        return True


class _Coord:
    """Nur das, was der Schreibpfad anfasst."""

    unique_id = "Truma iNetX-15E02F"
    poll_interval = 300

    def __init__(self, responses, *, source=HEATER, answer_from=0,
                 shape=_discovery_response, aftermath=None,
                 pre_report=None) -> None:
        self.clock = _Clock()
        COORD.asyncio = _FastAsyncio(self.clock)
        self.hass = stubs.SimpleNamespace(loop=self.clock)
        self._bus = BUS.Bus()
        self._bus.connected = True
        self._operations = COORD.OperationRegistry(lambda: None)
        self._writes_pending = 0
        self._write_feedback = {}
        self._command_hold_until = 0.0
        self._last_frame = 0.0
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._connected_event.set()
        self._pre_report = pre_report
        self._client = _Client(self, responses, source=source,
                               answer_from=answer_from, shape=shape,
                               aftermath=aftermath)

    def async_set_updated_data(self, _data) -> None:
        pass

    def async_sync_device_names(self) -> None:
        pass

    def report(self, parsed: dict) -> None:
        """Einen Frame so zustellen, wie die Sitzung es täte.

        ``session.handle_frame`` legt den Wert in den Bus; das ist hier
        nachgebildet, damit dieser Test nur den Schreibpfad prüft. Der
        Coordinator bekommt denselben Frame durch ``_on_frame``.
        """
        src = parsed["src"]
        cbor = parsed["cbor"]
        if "topics" in cbor:
            for entry in cbor["topics"]:
                for item in entry["parameters"]:
                    self._bus.update(entry["tn"], item["pn"], item["v"], src)
        else:
            self._bus.update(cbor["tn"], cbor["pn"], cbor["v"], src)
        self._on_frame(parsed)

    async def _client_for_write(self):
        if self._pre_report is not None:
            # Eine Meldung, die eintrifft, während wir noch auf den Link
            # warten -- also vor unserem Frame.
            report, self._pre_report = self._pre_report, None
            self.report(report)
        return self._client

    _on_frame = COORD.TrumaCoordinator._on_frame
    _note_frame_values = COORD.TrumaCoordinator._note_frame_values
    _request_param_discovery = COORD.TrumaCoordinator._request_param_discovery
    async_write_many = COORD.TrumaCoordinator.async_write_many
    async_write = COORD.TrumaCoordinator.async_write
    _write_confirmed = COORD.TrumaCoordinator._write_confirmed
    _await_feedback = COORD.TrumaCoordinator._await_feedback
    on_frame_value = COORD.TrumaCoordinator.on_frame_value
    _feedback_satisfied = staticmethod(COORD.TrumaCoordinator._feedback_satisfied)
    _infer_action = staticmethod(COORD.TrumaCoordinator._infer_action)


def _describe(coord, topic, param, addr=HEATER, **meta) -> None:
    """Dem Bus sagen, dass ein Gerät diesen Parameter kennt und schreiben darf."""
    coord._bus.device(addr).param_meta[f"{topic}.{param}"] = {"perm": 1, **meta}


def _expect_refusal(coord, *args, message="did not confirm", **kwargs) -> str:
    """Einen Schreibvorgang laufen lassen, der scheitern muss."""
    try:
        asyncio.run(coord.async_write(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - genau das ist die Erwartung
        assert message in str(exc), exc
        return str(exc)
    raise AssertionError(f"kein Fehlschlag, erwartet war {message!r}")


def test_transport_ack_alone_is_not_confirmation() -> None:
    """Der Frame kam an -- mehr sagt ein ACK nicht."""
    coord = _Coord({})
    _describe(coord, "WaterHeating", "Active")

    _expect_refusal(coord, HEATER, "WaterHeating", "Active", 1)

    assert coord._client.writes, "es wurde gar nicht erst geschrieben"
    assert coord._writes_pending == 0
    assert coord._write_feedback == {}, coord._write_feedback
    # Auch ein Fehlschlag hält den Link für die nächste Bedienung offen.
    assert coord._command_hold_until == coord.clock.now + COORD._COMMAND_HOLD_SECONDS


def test_a_cached_value_does_not_confirm() -> None:
    """Sonst bestätigt sich jeder Befehl selbst, der nichts bewirkt."""
    coord = _Coord({})
    _describe(coord, "WaterHeating", "Active")
    coord._bus.update("WaterHeating", "Active", 1, HEATER)

    _expect_refusal(coord, HEATER, "WaterHeating", "Active", 1)


def test_a_report_from_before_the_write_does_not_confirm() -> None:
    """Nur was nach unserem Frame eintrifft, ist eine Antwort darauf."""
    coord = _Coord(
        {},
        pre_report=_discovery_response(HEATER, "WaterHeating", "Active", 1),
    )
    _describe(coord, "WaterHeating", "Active")

    _expect_refusal(coord, HEATER, "WaterHeating", "Active", 1)


def test_an_answer_from_the_wrong_device_does_not_confirm() -> None:
    """Das Panel spricht für viele Geräte -- bestätigen darf nur das gemeinte."""
    coord = _Coord({("WaterHeating", "Active"): 1}, source=PANEL)
    _describe(coord, "WaterHeating", "Active")

    _expect_refusal(coord, HEATER, "WaterHeating", "Active", 1)


def test_water_active_one_or_two_both_confirm_on() -> None:
    """2 heißt 'ein, heizt gerade nicht' -- das ist kein Fehlschlag."""
    for reply in (1, 2):
        coord = _Coord({("WaterHeating", "Active"): reply})
        _describe(coord, "WaterHeating", "Active")
        asyncio.run(coord.async_write(HEATER, "WaterHeating", "Active", 1))
        assert coord._bus.device(HEATER).get("WaterHeating", "Active") == reply
        assert len(coord._client.writes) == 1, coord._client.writes


def test_water_off_accepts_only_a_fresh_zero() -> None:
    """Zum Ausschalten zählt nur die Null."""
    coord = _Coord({("WaterHeating", "Active"): 0})
    _describe(coord, "WaterHeating", "Active")
    asyncio.run(coord.async_write(HEATER, "WaterHeating", "Active", 0))

    for reply in (1, 2):
        coord = _Coord({("WaterHeating", "Active"): reply})
        _describe(coord, "WaterHeating", "Active")
        _expect_refusal(coord, HEATER, "WaterHeating", "Active", 0)


def test_the_exception_is_only_for_water_active() -> None:
    """Überall sonst muss der Wert exakt zurückkommen."""
    for topic, param in (("WaterHeating", "Mode"), ("EnergySrc", "ElectricLevel"),
                         ("AirCirculation", "Active")):
        coord = _Coord({(topic, param): 2})
        _describe(coord, topic, param)
        _expect_refusal(coord, HEATER, topic, param, 1)


def test_the_rule_itself_is_exactly_this_one_exception() -> None:
    """Die Regel direkt befragt, damit kein Nachbarfall mitrutscht."""
    satisfied = COORD.TrumaCoordinator._feedback_satisfied
    assert satisfied("WaterHeating", "Active", 1, 1)
    assert satisfied("WaterHeating", "Active", 1, 2)
    assert not satisfied("WaterHeating", "Active", 1, 0)
    assert satisfied("WaterHeating", "Active", 0, 0)
    assert not satisfied("WaterHeating", "Active", 0, 2)
    assert not satisfied("WaterHeating", "Mode", 1, 2)
    assert not satisfied("AirCirculation", "Active", 1, 2)
    assert satisfied("AirCirculation", "FanLevel", 3, 3)


def test_a_sleeping_heater_is_retried_but_bounded() -> None:
    """Ein zweiter Anlauf muss sein -- und danach ist bald Schluss."""
    coord = _Coord({("EnergySrc", "ElectricLevel"): 1}, answer_from=1)
    _describe(coord, "EnergySrc", "ElectricLevel")

    asyncio.run(coord.async_write(HEATER, "EnergySrc", "ElectricLevel", 1))
    assert len(coord._client.writes) == 2, coord._client.writes

    coord = _Coord({("EnergySrc", "ElectricLevel"): 1}, answer_from=99)
    _describe(coord, "EnergySrc", "ElectricLevel")
    started = coord.clock.now
    _expect_refusal(coord, HEATER, "EnergySrc", "ElectricLevel", 1)
    assert len(coord._client.writes) == COORD._WRITE_ATTEMPTS, coord._client.writes
    assert COORD._WRITE_ATTEMPTS >= 2, (
        "ein schlafendes Gerät braucht einen zweiten Anlauf"
    )
    # Die eigentliche Grenze ist keine Anzahl, sondern die Zeit, die jemand
    # vor dem Panel auf eine Fehlermeldung wartet. Am Rechner gemessen an der
    # virtuellen Uhr, an der der Coordinator seine Fristen nimmt.
    assert coord.clock.now - started <= 60, coord.clock.now - started


def test_an_unsolicited_info_message_also_confirms() -> None:
    """Beide Frameformen tragen Werte; beide sind eine Rückmeldung."""
    for shape in (_discovery_response, _info_message):
        coord = _Coord({("AirCirculation", "FanLevel"): 3}, shape=shape)
        _describe(coord, "AirCirculation", "FanLevel")
        asyncio.run(coord.async_write(HEATER, "AirCirculation", "FanLevel", 3))


def test_a_transaction_checks_the_state_it_leaves_behind() -> None:
    """Jeder Befehl wurde bestätigt -- und am Ende stimmt es trotzdem nicht."""

    def _revert_first(coord, n_writes: int) -> None:
        if n_writes == 2:
            # Die Heizung nimmt die erste Einstellung zurück, während der
            # zweite Befehl läuft.
            coord._bus.update("EnergySrc", "ElectricLevel", 0, HEATER)

    coord = _Coord(
        {("EnergySrc", "ElectricLevel"): 1, ("EnergySrc", "GasMode"): 1},
        aftermath=_revert_first,
    )
    _describe(coord, "EnergySrc", "ElectricLevel")
    _describe(coord, "EnergySrc", "GasMode")

    try:
        asyncio.run(coord.async_write_many([
            (HEATER, "EnergySrc", "ElectricLevel", 1),
            (HEATER, "EnergySrc", "GasMode", 1),
        ]))
    except Exception as exc:  # noqa: BLE001 - genau das ist die Erwartung
        assert "did not retain" in str(exc), exc
    else:
        raise AssertionError("die zurückgenommene Einstellung blieb unbemerkt")

    # Ohne Rücknahme geht dieselbe Transaktion durch.
    coord = _Coord({("EnergySrc", "ElectricLevel"): 1, ("EnergySrc", "GasMode"): 1})
    _describe(coord, "EnergySrc", "ElectricLevel")
    _describe(coord, "EnergySrc", "GasMode")
    asyncio.run(coord.async_write_many([
        (HEATER, "EnergySrc", "ElectricLevel", 1),
        (HEATER, "EnergySrc", "GasMode", 1),
    ]))


def test_an_invalid_command_is_refused_before_anything_is_sent() -> None:
    """Eine Transaktion prüft alles, bevor sie das Erste losschickt."""
    coord = _Coord({("EnergySrc", "ElectricLevel"): 1})
    _describe(coord, "EnergySrc", "ElectricLevel", enum={1: "on"})
    _describe(coord, "WaterHeating", "Mode", enum={1: "eco"})

    try:
        asyncio.run(coord.async_write_many([
            (HEATER, "EnergySrc", "ElectricLevel", 1),
            (HEATER, "WaterHeating", "Mode", 7),
        ]))
    except Exception as exc:  # noqa: BLE001 - genau das ist die Erwartung
        assert "Invalid Truma command" in str(exc), exc
    else:
        raise AssertionError("ein ungültiger Befehl wurde geschrieben")

    assert coord._client.sends == [], coord._client.sends


def test_the_failed_operation_is_named_for_what_the_user_did() -> None:
    """Der Vorgangs-Sensor muss sagen können, was schiefging."""
    coord = _Coord({})
    _describe(coord, "WaterHeating", "Active")
    _expect_refusal(coord, HEATER, "WaterHeating", "Active", 1)
    assert coord._operations.attributes["action"] == "water_mode"

    coord = _Coord({})
    _describe(coord, "EnergySrc", "ElectricLevel")
    try:
        asyncio.run(coord.async_write_many(
            [(HEATER, "EnergySrc", "ElectricLevel", 1)],
            action="energy_source", target="gas+electric",
        ))
    except Exception:  # noqa: BLE001 - genau das ist die Erwartung
        pass
    else:
        raise AssertionError("kein Fehlschlag")
    assert coord._operations.attributes["action"] == "energy_source"
    assert coord._operations.attributes["target"] == "gas+electric"


def test_the_parameter_query_is_sent_as_a_probe() -> None:
    """Die Abfrage darf unbeantwortet bleiben, ohne die Sitzung zu beenden.

    ``ble.py`` beendet im ``finally`` von ``_send_locked`` jede Sitzung, deren
    Frame nicht beantwortet wurde -- außer der Frame war als Sonde markiert.
    Genau ein schlafendes Gerät ist der Fall, für den ``_write_confirmed``
    überhaupt mehrere Anläufe hat: ginge die Abfrage ohne ``probe=True``
    hinaus, risse ihr Schweigen die Sitzung ab, ``if not client.connected``
    bräche ab, und aus drei Anläufen würde lautlos einer.
    """
    coord = _Coord({("EnergySrc", "ElectricLevel"): 1})
    _describe(coord, "EnergySrc", "ElectricLevel")
    asyncio.run(coord.async_write(HEATER, "EnergySrc", "ElectricLevel", 1))

    assert coord._client.probes == [("write", False), ("discovery", True)], (
        coord._client.probes
    )


def test_a_relayed_topic_is_addressed_to_the_panel() -> None:
    """Die Zieladressierung von #10, auf dem bestätigten Schreibpfad.

    Der Befehl geht an das Gerät der Entität -- außer bei den Topics, die das
    Panel für den Bus weiterreicht. Dann ist auch das Panel die Quelle, die
    bestätigen darf: eine Antwort der Heizung wäre keine Antwort auf diesen
    Frame.
    """
    coord = _Coord({("RoomClimate", "Mode"): 3}, source=PANEL)
    _describe(coord, "RoomClimate", "Mode", addr=PANEL, enum={0: "off", 3: "heat"})

    asyncio.run(coord.async_write(HEATER, "RoomClimate", "Mode", 3))

    assert [frame[1] for frame in coord._client.writes] == [PANEL], (
        coord._client.writes
    )
    # ...und der nicht weitergereichte Nachbar geht weiter ans eigene Gerät.
    coord = _Coord({("AirCirculation", "FanLevel"): 3})
    _describe(coord, "AirCirculation", "FanLevel")
    asyncio.run(coord.async_write(HEATER, "AirCirculation", "FanLevel", 3))
    assert [frame[1] for frame in coord._client.writes] == [HEATER], (
        coord._client.writes
    )


class _SlowPanel:
    """Ein Panel, das erst meldet, wenn der Test es sagt.

    Anders als ``_Client`` merkt es sich mehrere offene Schreibvorgänge und
    beantwortet sie nicht schon in der Parameter-Abfrage: nur so lässt sich
    der Zeitpunkt festlegen, an dem die Meldung eintrifft, und genau darauf
    kommt es hier an.
    """

    connected = True
    assigned_addr = 0x0500

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.writes: list = []
        self._pending: list = []

    async def send(self, frame, probe: bool = False) -> bool:
        if frame[0] == "write":
            _kind, _dest, topic, param, value = frame
            self.writes.append(frame)
            self._pending.append((topic, param, value))
        return True

    def answer(self) -> None:
        """Alles melden, was angenommen wurde -- so wie ein Gerät aufwacht."""
        pending, self._pending = self._pending, []
        for topic, param, value in pending:
            self.coordinator.report(_discovery_response(HEATER, topic, param, value))


async def _until(condition, what: str) -> None:
    """Die anderen Tasks laufen lassen, bis ``condition`` gilt."""
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"kam nie so weit: {what}")


def test_two_writes_at_once_do_not_erase_each_other() -> None:
    """Zwei gleichzeitige Befehle führen nicht dazu, dass einer scheitert.

    Home Assistant serialisiert Service-Aufrufe nicht: eine Szene, ein
    ``parallel:``-Skript, zwei Automationen oder schlicht eine zweite
    Bedienung innerhalb der bis zu 40 s, die ein Befehl jetzt dauern darf,
    lassen zwei Schreibvorgänge zugleich auf ihre Bestätigung warten. Mit
    einem gemeinsamen Rückmeldungsbuch räumte der später gestartete dem
    früheren seines weg -- der erste lief in den Timeout und meldete "did not
    confirm" für einen Befehl, den das Gerät ausgeführt und zurückgemeldet
    hatte. Also genau die Fehlklasse, die dieser Schreibpfad beseitigen soll,
    nur spiegelverkehrt.

    Der Reihenfolge nach gestellt, weil erst sie den Fall trifft: das Gerät
    meldet, *nachdem* der zweite Vorgang begonnen hat. Ein Buch, das der
    zweite dem ersten weggeräumt hat, nimmt diese Meldung nicht mehr an.
    """
    coord = _Coord({})
    panel = _SlowPanel(coord)
    coord._client = panel
    _describe(coord, "EnergySrc", "ElectricLevel")
    _describe(coord, "WaterHeating", "Mode")

    async def _both() -> None:
        first = asyncio.create_task(
            coord.async_write(HEATER, "EnergySrc", "ElectricLevel", 1)
        )
        await _until(lambda: len(panel.writes) == 1, "erster Befehl gesendet")
        second = asyncio.create_task(
            coord.async_write(HEATER, "WaterHeating", "Mode", 2)
        )
        await _until(lambda: len(panel.writes) == 2, "zweiter Befehl gesendet")
        # Beide warten jetzt; erst hier meldet das Gerät.
        panel.answer()
        await asyncio.gather(first, second)

    asyncio.run(_both())

    # Je ein Frame: keiner der beiden musste einen Anlauf wiederholen, also
    # ging auch keiner der beiden Befehle ein zweites Mal ans Fahrzeug.
    assert len(panel.writes) == 2, panel.writes
    assert coord._bus.device(HEATER).get("EnergySrc", "ElectricLevel") == 1
    assert coord._bus.device(HEATER).get("WaterHeating", "Mode") == 2
    assert coord._write_feedback == {}, coord._write_feedback
    assert coord._writes_pending == 0


def _main() -> None:
    stubs.run_tests(globals(), "Confirmed writes")


if __name__ == "__main__":
    _main()
