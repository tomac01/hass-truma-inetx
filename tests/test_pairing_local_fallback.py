#!/usr/bin/env python3
"""Offline check that a panel which refuses every unbonded link still pairs.

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed
so the real ``pairing.ensure_bonded`` loop and the real
``pairing._ensure_bonded_bluez`` loop run against fakes.

Why this exists (issue #26, a Raspi 3B with a USB dongle and no proxy,
2026-09-17): the panel was heard on its identity address and nothing else, and
every connect died the same way 750 ms in --

    Failed to connect after 1 attempt(s): failed to discover services,
    device disconnected

-- twenty-six times in sixty seconds. ``ensure_bonded`` dispatches on the
client it gets, so a connect that never returns one never reaches the dispatch:
``Device1.Pair()`` was never called, the Just Works agent was never registered,
and the one procedure that could have bonded the panel sat behind the connect
that bonding was supposed to make possible.

What it pins:

1. a connect that has failed on every advertised address hands over to the
   BlueZ path instead of re-dialling,
2. every address gets its turn first, so the RPA rotation that cures a stale
   proxy bond is not cut short,
3. a *bond* failure is still the rotation's business and does not trigger it,
4. the hand-over needs a BlueZ object path to pair on, and without one nothing
   is claimed,
5. the bond is dropped only after the panel has refused it -- try, then
   remove,
6. and only when the caller could not establish a link at all, so Reconfigure
   on a working bond is reported rather than destroyed, whether the fast path
   or the loop is what reads it,
7. one removal attempt per call, so a RemoveDevice BlueZ refuses cannot eat
   the timeout,
8. a bond this host holds alone is never reported as success,
9. and the link ``Pair()`` opened is dropped before the bond is reported --
   BlueZ keeps it, and the kernel then refuses the session's every connect.

Run: ``python3 tests/test_pairing_local_fallback.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

PANEL = "Truma iNetX-BBCCDD"
HCI0 = "/org/bluez/hci0"

# Erfundene Adressen: jedes Byte stammt aus den Platzhalter-Bytes, die
# ``test_placeholder_addresses.py`` zulässt. Ihre Form ist trotzdem
# bedeutungstragend -- wer sie ändert, ändert, was diese Datei prüft:
#
# * ``IDENTITY`` ist die Identitätsadresse aus #26. Ihre letzten drei Bytes
#   ohne Doppelpunkte sind der Namenssuffix von ``PANEL``, und nur daran
#   erkennt ``bt.address_kind`` sie als Identität statt als rotierende RPA.
#   Ihr erstes Byte (0x88) trägt oben nicht das Bitmuster ``01``, liest sich
#   also als public -- passend zum ``AddressType`` ``public`` an ``DEV``, von
#   dem die Erwartung ``address_type == 1`` weiter unten hängt.
# * ``OTHER`` ist die phantomhafte zweite Adresse neben der lebenden, ``RPA``
#   die, auf der ein Panel im Add-Device-Modus wirbt. Beide sind Resolvable
#   Private Addresses: die oberen zwei Bits des ersten Bytes sind ``01``
#   (0x66, 0x44). Sie dürfen NICHT auf den Namenssuffix enden, sonst gelten
#   sie als Identitätsadresse und die Rotation prüft etwas anderes.
# * Die ``dev_``-Pfade sind abgeleitet, nicht abgeschrieben: BlueZ schreibt
#   Adressen darin mit Unterstrichen, und diese Schreibweise sieht der Guard
#   nicht -- ein Tippfehler fiele dort nicht auf.
IDENTITY = "88:99:AA:BB:CC:DD"
OTHER = "66:77:88:99:AA:BB"
DEV = f"{HCI0}/dev_" + IDENTITY.replace(":", "_")
# The RPA a panel in add-device mode advertises on, and the service UUID that
# is all it offers to recognise it by.
RPA = "44:55:66:77:88:99"
RPA_DEV = f"{HCI0}/dev_" + RPA.replace(":", "_")
TRUMA_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"

# The wording bleak_retry_connector produced on the reporter's host, verbatim.
NO_SERVICES = (
    "Failed to connect after 1 attempt(s): "
    "failed to discover services, device disconnected"
)


class _Logger:
    """Swallow the integration's log calls."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _V:
    """dbus_fast Variant stand-in, as read back off a property dict."""

    def __init__(self, value):
        self.value = value


class _ServiceInterface:
    """dbus_fast ServiceInterface stand-in that accepts its interface name."""

    def __init__(self, _name: str | None = None) -> None:
        pass


class _Mgmt:
    """habluetooth's MGMT socket: records what gets loaded for which address."""

    def __init__(self) -> None:
        self.loads: list[tuple[int, str, int, str]] = []
        # A host where habluetooth has no socket to offer.
        self.available = True

    def load_conn_params(self, index, address, address_type, params) -> bool:
        self.loads.append((index, address, address_type, params.value))
        return True


MGMT = _Mgmt()


class _Manager:
    """habluetooth's central manager, for the one method pairing asks it."""

    def get_bluez_mgmt_ctl(self):
        return MGMT if MGMT.available else None


def _load_pairing():
    """Import ``pairing.py`` with every external dependency stubbed out."""
    MGMT.loads.clear()
    MGMT.available = True

    def _mod(name: str, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules[name] = module
        return module

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object)
    _mod(
        "bleak_retry_connector",
        BleakClientWithServiceCache=object,
        establish_connection=None,
    )
    # BusType needs the member pairing actually asks for: the rotation test
    # gets away with `object` because it never reaches the D-Bus path, and this
    # file is the one that does.
    _mod(
        "dbus_fast",
        __path__=[],
        BusType=types.SimpleNamespace(SYSTEM="system"),
        Variant=lambda *args: args,
    )
    _mod("dbus_fast.aio", MessageBus=_Bus)
    _mod(
        "dbus_fast.service",
        ServiceInterface=_ServiceInterface,
        method=lambda *a, **k: (lambda f: f),
    )

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod(
        "truma_pkg.bt",
        async_resolve_device=lambda *a, **k: None,
        # Default: no proxy in earshot, which is the #26 host. Tests that
        # need the other kind set pairing.async_has_proxy_route themselves.
        async_has_proxy_route=lambda *a, **k: False,
    )
    _mod("truma_pkg.ble", client_is_proxy=lambda _client: True)
    _mod(
        "truma_pkg.const",
        LOGGER=_Logger(),
        has_truma_uuid=lambda uuids: any(
            str(u).lower().startswith("fc31") for u in uuids
        ),
    )
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", CHAR_CMD="cmd-char")
    # What Home Assistant loads into the kernel before every dial of its own,
    # and what the bond has to load for itself because nothing else will.
    habluetooth = _mod("habluetooth", __path__=[], get_manager=lambda: _Manager())
    habluetooth.const = _mod(
        "habluetooth.const",
        BDADDR_LE_PUBLIC=1,
        BDADDR_LE_RANDOM=2,
        ConnectParams=types.SimpleNamespace(
            FAST=types.SimpleNamespace(value="fast"),
            MEDIUM=types.SimpleNamespace(value="medium"),
        ),
    )

    spec = stubs.spec_from_source("truma_pkg.pairing", SRC / "pairing.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["truma_pkg.pairing"] = module
    spec.loader.exec_module(module)
    return module


# --- the BlueZ side, as far as _ensure_bonded_bluez can see -------------------


class _AlreadyExists(Exception):
    """What BlueZ answers Pair() with when it already holds a bond."""

    def __str__(self) -> str:
        return "[org.bluez.Error.AlreadyExists] Already Exists"


class _AuthFailed(Exception):
    """What BlueZ answers Pair() with when the panel refuses."""

    def __str__(self) -> str:
        return "[org.bluez.Error.AuthenticationFailed] Authentication Failed"


class _Busy(Exception):
    """What BlueZ answers Pair() with while a pairing it took is still running.

    Deliberately *not* named after the error: the module recognises this case by
    the message BlueZ sends, and a fixture whose class name carries the same
    word cannot tell "the message arrived" from "the message was lost and the
    class name stood in for it" -- ``_is_in_progress("_Busy")`` is true.
    A whole failure mode hid behind that name.
    """

    def __str__(self) -> str:
        return "[org.bluez.Error.InProgress] In Progress"


class _Bluez:
    """BlueZ plus the panel behind it, recording every call it is asked for."""

    def __init__(
        self,
        *,
        paired: bool,
        accepts: bool,
        removable: bool = True,
        pairs_after: int | None = None,
        hangs_on: str | None = None,
        stale_after: int | None = None,
    ):
        self.paired = paired
        self.accepts = accepts
        self.removable = removable
        # An object whose Pair() the daemon takes and never finishes, the way
        # the identity address behaves once the bond -- and with it the IRK
        # that reached the panel's RPA -- has been dropped. Cancelling it is
        # what lets go.
        self.hangs_on = hangs_on
        # The poll after which the identity object goes stale: the drop is not
        # visible in the objects until BlueZ stops seeing anything behind it.
        self.stale_after = stale_after
        # The object BlueZ is carrying a Pair() for. While it holds one, every
        # other object answers InProgress -- one pairing per adapter.
        self.pending_on: str | None = None
        self.pair_paths: list[str] = []
        # Polls after which a Pair() the daemon is still working on completes.
        # Until then every further call earns InProgress, as on the van.
        self.pairs_after = pairs_after
        self.polls = 0
        self.discovering = False
        self.paired_path: str | None = None
        # Whether BlueZ currently holds a link to the panel. Pair() opens one
        # and keeps it, which is what _release_link exists to undo.
        self.connected = False
        # A leftover object for the identity address with no RSSI behind it,
        # beside the RPA the panel is really on -- the van's state after a
        # bond was dropped.
        self.stale_identity = False
        self.calls: list[str] = []

    def objects(self) -> dict:
        self.polls += 1
        if self.pairs_after is not None and self.polls > self.pairs_after:
            self.paired = True
        if self.stale_after is not None and self.polls > self.stale_after:
            self.stale_identity = True
        if not self.stale_identity:
            return {
                DEV: {
                    "org.bluez.Device1": {
                        "Address": _V(IDENTITY),
                        "AddressType": _V("public"),
                        "Name": _V(PANEL),
                        "Paired": _V(self.paired),
                        "RSSI": _V(-52),
                    }
                }
            }
        # The identity object is a leftover: matched by address, but BlueZ is
        # not seeing it, so it carries no RSSI. The live RPA is nameless, as a
        # panel in add-device mode is, and only BlueZ's own discovery creates
        # an object for it at all.
        objects = {
            DEV: {
                "org.bluez.Device1": {
                    "Address": _V(IDENTITY),
                    "AddressType": _V("public"),
                    "Name": _V(PANEL),
                    "Paired": _V(self.paired),
                }
            }
        }
        if self.discovering:
            objects[RPA_DEV] = {
                "org.bluez.Device1": {
                    "Address": _V(RPA),
                    "AddressType": _V("random"),
                    "Paired": _V(self.paired),
                    "RSSI": _V(-51),
                    "UUIDs": _V([TRUMA_UUID]),
                }
            }
        return objects

    async def pair(self, path: str) -> None:
        self.calls.append("pair")
        self.pair_paths.append(path)
        if self.pending_on is not None and self.pending_on != path:
            # One pairing per adapter: the object the daemon is carrying a call
            # for is the only one that can make progress, and every other one
            # is answered InProgress however reachable it is.
            raise _Busy
        if self.pairs_after is not None and not self.paired:
            # The daemon took the first call and is still working on it.
            raise _Busy
        if self.paired:
            # BlueZ will not pair a device it already has a key for, which is
            # exactly the state a panel that forgot its half leaves behind.
            raise _AlreadyExists
        if path == self.hangs_on:
            self.pending_on = path
            raise _Busy
        if not self.accepts:
            raise _AuthFailed
        self.paired = True
        # Pair() bonds over a link of its own, and BlueZ keeps it afterwards.
        self.connected = True

    async def cancel_pairing(self, path: str) -> None:
        self.calls.append("cancel_pairing")
        if self.pending_on == path:
            self.pending_on = None
            # The daemon has let go, so the address is pairable again.
            self.hangs_on = None

    async def remove(self, path: str) -> None:
        self.calls.append("remove")
        assert path == DEV, f"removed the wrong object: {path}"
        if not self.removable:
            raise RuntimeError("[org.bluez.Error.Failed] Does Not Exist")
        self.paired = False


class _Bus:
    """dbus_fast MessageBus stand-in; the interfaces come from _get_interface."""

    current: "_Bluez | None" = None

    def __init__(self, **_kw) -> None:
        pass

    async def connect(self) -> "_Bus":
        return self

    def export(self, _path, _obj) -> None:
        assert _Bus.current is not None
        _Bus.current.calls.append("export_agent")

    def disconnect(self) -> None:
        assert _Bus.current is not None
        _Bus.current.calls.append("bus_disconnect")


class _ObjectManager:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_get_managed_objects(self) -> dict:
        return self._bluez.objects()


class _AgentManager:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_register_agent(self, _path, capability) -> None:
        assert capability == "NoInputNoOutput", capability
        self._bluez.calls.append("register_agent")

    async def call_request_default_agent(self, _path) -> None:
        self._bluez.calls.append("request_default_agent")

    async def call_unregister_agent(self, _path) -> None:
        self._bluez.calls.append("unregister_agent")


class _Device1:
    def __init__(self, bluez: _Bluez, path: str) -> None:
        self._bluez = bluez
        self._path = path

    async def call_pair(self) -> None:
        self._bluez.paired_path = self._path
        await self._bluez.pair(self._path)

    async def call_cancel_pairing(self) -> None:
        await self._bluez.cancel_pairing(self._path)

    async def call_disconnect(self) -> None:
        self._bluez.calls.append("disconnect")
        self._bluez.connected = False


class _Properties:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_set(self, _interface, prop, _value) -> None:
        self._bluez.calls.append(f"set:{prop}")

    async def call_get(self, _interface, prop) -> _V:
        assert prop == "Connected", prop
        return _V(self._bluez.connected)


class _Adapter1:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_remove_device(self, path) -> None:
        await self._bluez.remove(path)

    async def call_set_discovery_filter(self, _filter) -> None:
        self._bluez.calls.append("discovery_filter")

    async def call_start_discovery(self) -> None:
        self._bluez.calls.append("start_discovery")
        self._bluez.discovering = True

    async def call_stop_discovery(self) -> None:
        self._bluez.calls.append("stop_discovery")
        self._bluez.discovering = False


def _interfaces(bluez: _Bluez):
    """A ``_get_interface`` that answers from ``bluez``, asserting the paths."""

    async def _get_interface(_bus, path, interface):
        if interface == "org.freedesktop.DBus.ObjectManager":
            assert path == "/", path
            return _ObjectManager(bluez)
        if interface == "org.bluez.AgentManager1":
            assert path == "/org/bluez", path
            return _AgentManager(bluez)
        if interface == "org.bluez.Device1":
            assert path in (DEV, RPA_DEV), path
            return _Device1(bluez, path)
        if interface == "org.freedesktop.DBus.Properties":
            return _Properties(bluez)
        if interface == "org.bluez.Adapter1":
            # The bond must be dropped on the adapter being paired, not on
            # whichever adapter happens to know the device.
            assert path == HCI0, path
            return _Adapter1(bluez)
        raise AssertionError(f"unexpected interface {interface}")

    return _get_interface


class _BluezDevice:
    """A BLEDevice as the resolver hands it back from a local adapter."""

    def __init__(self, path: str) -> None:
        self.details = {"path": path}
        self.address = IDENTITY


# --- part one: ensure_bonded hands over when no link can be made -------------


class _Device:
    def __init__(self, address: str) -> None:
        self.address = address


class _StopTest(Exception):
    """Ends a loop that is meant to run until its deadline."""


def _run_ensure_bonded(
    pairing,
    *,
    addresses: list[str],
    bluez_sees: bool,
    has_proxy: bool = True,
    connects: bool = False,
    connect_delay: float = 0.0,
    fresh_addresses: bool = False,
    bonds: bool = False,
    stop_after: int = 200,
):
    """Drive ``ensure_bonded`` and report what it tried and where it went."""
    log: dict = {"tried": [], "handover": [], "budget": [], "bonded_over_link": 0}

    def resolve(_hass, _name, *, avoid=(), local_only=False, prefer_identity=False):
        if local_only:
            # What _live_device_path asks: a local adapter's device, which is
            # the only kind carrying the BlueZ object path Pair() needs.
            return _BluezDevice(DEV) if bluez_sees else None
        if len(log["tried"]) >= stop_after:
            # Only a guard against a loop that never reaches its deadline; the
            # tests below are meant to end on the deadline, not here.
            raise _StopTest
        if fresh_addresses:
            # A panel whose RPA rotates faster than the rotation can wrap: no
            # candidate is ever offered twice, so nothing but the reserve can
            # end the dialling.
            n = len(log["tried"])
            return _Device(f"4{n % 10}:00:00:00:{n // 10:02X}:{n % 10:02X}")
        demoted = {a.upper() for a in avoid}
        ranked = sorted(addresses, key=lambda a: a.upper() in demoted)
        return _Device(ranked[0]) if ranked else None

    async def connect(_cls, device, _address, **_kw):
        log["tried"].append(device.address)
        if connect_delay:
            await asyncio.sleep(connect_delay)
        if not connects:
            raise RuntimeError(NO_SERVICES)
        return object()

    async def bond_over_link(_name, _client):
        log["bonded_over_link"] += 1
        return bonds

    async def bluez_bond(_name, _address, **kwargs):
        log["handover"].append(kwargs.get("trust_existing_bond"))
        log["budget"].append(kwargs.get("timeout"))
        return True

    # Put every one of these back afterwards. _ensure_bonded_bluez is stubbed
    # here and exercised for real in part two, and a leak between the two reads
    # as the real loop passing when it never ran.
    patches = {
        "async_resolve_device": resolve,
        "establish_connection": connect,
        "client_is_proxy": lambda _client: True,
        "_bond_over_link": bond_over_link,
        "_ensure_bonded_bluez": bluez_bond,
        "async_has_proxy_route": lambda *_a, **_k: has_proxy,
        # The loop's back-off is wall-clock seconds and its deadline is not;
        # skip most of the waiting without touching asyncio.wait_for, which the
        # D-Bus helpers still need.
        "asyncio": types.SimpleNamespace(sleep=lambda _s: asyncio.sleep(0.01)),
    }
    saved = {name: getattr(pairing, name) for name in patches}
    for name, value in patches.items():
        setattr(pairing, name, value)

    async def main():
        try:
            return await pairing.ensure_bonded(
                object(), PANEL, IDENTITY, adapter_path=HCI0, timeout=0.25
            )
        except _StopTest:
            return None, None

    try:
        return asyncio.run(main()), log
    finally:
        for name, value in saved.items():
            setattr(pairing, name, value)


def test_a_panel_that_refuses_every_link_is_bonded_through_bluez(pairing) -> None:
    """#26: the connect bonding is *for* cannot be a precondition of bonding.

    One address on air, every connect dying before services resolve. The old
    loop re-dialled it for the whole timeout and never registered an agent.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=True, has_proxy=True
    )
    assert bonded is True
    assert client is None, "the BlueZ path holds no link to hand off"
    assert log["handover"] == [False], (
        f"expected one hand-over with the bond unproven: {log['handover']}"
    )
    # Dialled once, then handed over rather than spending the timeout.
    assert log["tried"] == [IDENTITY], f"kept re-dialling: {log['tried']}"


def test_every_address_is_tried_before_handing_over(pairing) -> None:
    """The rotation keeps its turn: a second address may be the live one.

    A panel fresh out of pairing advertises a phantom RPA beside a working
    one, and the phantom fails to establish. Handing over on the first failure
    would bond locally while a proxy-carried address was still untried.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY, OTHER], bluez_sees=True, has_proxy=True
    )
    assert bonded is True
    assert log["tried"] == [IDENTITY, OTHER], f"rotation cut short: {log['tried']}"
    assert log["handover"] == [False]


def test_a_bond_failure_is_not_a_connect_failure(pairing) -> None:
    """Connected and then refused is the rotation's case, not the fallback's.

    This is the error-97 path: the proxy holds a bond the panel has dropped,
    the panel tears the link down on the protected write, and the cure is the
    panel's next RPA -- not a local bond on a host whose sessions run over the
    proxy, which is the mistake c91f711 was written to stop.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing,
        addresses=[IDENTITY, OTHER],
        bluez_sees=True,
        has_proxy=True,
        connects=True,
        bonds=False,
    )
    assert bonded is False
    assert log["handover"] == [], "a bond failure must not hand over to BlueZ"
    assert log["bonded_over_link"] >= 2, "the link path should have kept trying"


def test_nothing_is_claimed_without_a_bluez_path(pairing) -> None:
    """No object path, no Pair(): the failure is somewhere else.

    A proxy-only host whose connects fail transiently must not be told a local
    bond was made, because there is no local adapter to make one on.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=False, has_proxy=True
    )
    assert bonded is False
    assert client is None
    assert log["handover"] == [], "handed over with no adapter to pair on"
    assert len(log["tried"]) > 1, "should have kept retrying the connect"


def test_a_proxyless_host_bonds_before_it_dials(pairing) -> None:
    """The regression c91f711 left behind, and what 0.7.1b5 did instead.

    A panel with no bond drops every link it is offered, so on a host where no
    proxy can hear it a connect cannot succeed and only spends the budget --
    ~20 s per candidate address, of a 60 s timeout. Before connect-first this
    host went straight to BlueZ with the whole of it.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY, OTHER], bluez_sees=True, has_proxy=False
    )
    assert bonded is True
    assert client is None
    assert log["tried"] == [], f"dialled a panel that cannot answer: {log['tried']}"
    assert len(log["handover"]) == 1


def test_a_predial_bond_does_not_trust_what_bluez_reports(pairing) -> None:
    """Bonding before the first dial has proven nothing, so it trusts nothing.

    Observed on the van (2026-09-18) with the panel's own bond freshly
    deleted: the adapter still held a record with the signature keys and no
    LongTermKey, BlueZ reported it as Paired *and* Bonded, and this path took
    the fast path out in under a millisecond. No SMP frame reached the air,
    the panel stayed in add-device mode, Home Assistant reported the pairing a
    success, and every session after it timed out connecting.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=True, has_proxy=False
    )
    assert bonded is True
    assert log["handover"] == [False], (
        f"pre-dial bond trusted a bond it never proved: {log['handover']}"
    )


def test_a_host_with_a_proxy_still_dials_first(pairing) -> None:
    """The case c91f711 is actually about, and it keeps its behaviour.

    Where a proxy can hear the panel, a local bond can land on a path nothing
    connects over, and every later session then fails at encryption. So the
    link in hand still decides, and BlueZ is the fallback it always was.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=True, has_proxy=True
    )
    assert bonded is True
    assert log["tried"] == [IDENTITY], "bonded locally without dialling"


def test_the_connect_rotation_cannot_starve_the_bond(pairing) -> None:
    """The hand-over is guaranteed its reserve, however slow the dialling.

    Measured on the van (2026-09-18): two addresses, ~20 s each to give up on,
    and the hand-over inherited 15 s of a 60 s budget and timed out. The same
    bond took 3.2 s when the rotation had left it room. Here the panel offers
    a fresh address every time, so the rotation can never wrap and only the
    reserve can end it.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing,
        addresses=[],
        bluez_sees=True,
        has_proxy=True,
        connect_delay=0.02,
        fresh_addresses=True,
    )
    assert bonded is True
    assert log["tried"], "handed over without dialling at all"
    assert len(log["handover"]) == 1, f"never handed over: {log['handover']}"
    assert log["budget"][0] > 0, f"handed over with nothing left: {log['budget']}"


# --- part two: try, then remove ----------------------------------------------


def _run_bluez_bond(
    pairing,
    bluez: _Bluez,
    *,
    trust: bool,
    timeout: float = 1.0,
    adapter_path: str | None = HCI0,
    pending_limit: float | None = None,
):
    """Drive the real ``_ensure_bonded_bluez`` against a fake BlueZ."""
    saved = {
        name: getattr(pairing, name)
        for name in (
            "_get_interface",
            "async_resolve_device",
            "_POLL_INTERVAL",
            "_PAIR_PENDING_LIMIT",
        )
    }
    pairing._get_interface = _interfaces(bluez)
    pairing.async_resolve_device = lambda *a, **k: _BluezDevice(DEV)
    pairing._POLL_INTERVAL = 0
    if pending_limit is not None:
        pairing._PAIR_PENDING_LIMIT = pending_limit
    _Bus.current = bluez
    try:
        return asyncio.run(
            pairing._ensure_bonded_bluez(
                PANEL,
                IDENTITY,
                adapter_path=adapter_path,
                timeout=timeout,
                hass=object(),
                trust_existing_bond=trust,
            )
        )
    finally:
        for name, value in saved.items():
            setattr(pairing, name, value)
        _Bus.current = None


def test_a_stale_bond_is_dropped_only_after_the_panel_refuses(pairing) -> None:
    """Try, then remove -- and in that order.

    BlueZ holds a key the panel discarded, so Pair() comes back AlreadyExists.
    Only then is the bond dropped, and the pass after it bonds clean.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    pairs = [c for c in bluez.calls if c in ("pair", "remove")]
    assert pairs[0] == "pair", f"removed before asking the panel: {bluez.calls}"
    assert pairs.count("remove") == 1, f"removed more than once: {bluez.calls}"
    assert pairs[-1] == "pair", "nothing re-paired after the bond was dropped"
    # The procedure the reporter's host never reached at all.
    assert "register_agent" in bluez.calls
    assert "unregister_agent" in bluez.calls


def test_a_pairing_left_on_the_old_address_is_called_off(pairing) -> None:
    """Dropping the bond moves the panel, and the running call has to follow.

    Measured on the van (2026-09-18). Pair() came back AlreadyExists, the
    host's bond was dropped -- and that took the panel's IRK with it, so the
    identity address stopped resolving to the RPA the panel was advertising
    on. The Pair() already running against the identity could no longer reach
    anything (51 s, not one connection attempt on air), and because BlueZ
    pairs one device at a time it answered InProgress for the live RPA too.
    The loop sat out the whole 60 s watching a call that could not finish.
    """
    bluez = _Bluez(paired=True, accepts=True, hangs_on=DEV, stale_after=2)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    assert "cancel_pairing" in bluez.calls, (
        f"waited out a pairing on an address nothing answers: {bluez.calls}"
    )
    assert bluez.pair_paths[-1] == RPA_DEV, (
        f"never paired where the panel actually is: {bluez.pair_paths}"
    )


def test_the_bond_dials_with_home_assistants_connection_parameters(pairing) -> None:
    """``Pair()`` makes BlueZ dial, and BlueZ dials with the kernel's defaults.

    Measured on the van (2026-09-18), one adapter, same minutes, one btmon
    capture. Every connection habluetooth made went out at a 7.50 ms interval
    with a 10000 ms supervision timeout, because it loads those over MGMT
    before each dial: 155 of them, all successful. The bond's own dial went out
    at 30.00-50.00 ms with a 420 ms timeout -- nine connection events of budget
    -- and died 274 ms after ``LE Connection Complete, Status: Success``, which
    is six events, the link-layer limit for establishment. 73 attempts that
    day, not one SMP frame, and from D-Bus it read as
    ``le-connection-abort-by-local``: our side, not the panel.
    """
    MGMT.loads.clear()
    bluez = _Bluez(paired=False, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    assert MGMT.loads, "paired with whatever the kernel had lying around"
    index, address, address_type, params = MGMT.loads[0]
    assert index == 0, f"loaded for the wrong adapter: {MGMT.loads}"
    assert address == IDENTITY
    assert address_type == 1, "an identity address is public"
    assert params == "fast"


def test_the_parameters_follow_the_panel_and_are_not_reloaded_per_poll(
    pairing,
) -> None:
    """They are stored per address, so a rotation needs its own load -- one.

    The loop polls about once a second for a minute; loading on every pass
    would put sixty MGMT commands on the socket to say the same thing twice.
    """
    MGMT.loads.clear()
    bluez = _Bluez(paired=True, accepts=True, hangs_on=DEV, stale_after=2)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    loaded = [(addr, kind) for _i, addr, kind, _p in MGMT.loads]
    assert (RPA, 2) in loaded, f"never loaded for the live RPA: {loaded}"
    assert loaded.count((RPA, 2)) == 1, f"reloaded per poll: {loaded}"
    assert loaded.count((IDENTITY, 1)) == 1, f"reloaded per poll: {loaded}"


def test_a_host_with_no_mgmt_socket_still_pairs(pairing) -> None:
    """The parameters are an improvement, not a dependency.

    Stock parameters are what this did before and they did sometimes work, so
    a habluetooth that has moved the API costs us only what we already had.
    """
    MGMT.loads.clear()
    MGMT.available = False
    try:
        bluez = _Bluez(paired=False, accepts=True)
        assert _run_bluez_bond(pairing, bluez, trust=False) is True
        assert MGMT.loads == []
        assert "pair" in bluez.calls
    finally:
        MGMT.available = True


def test_a_pairing_that_never_finishes_is_not_waited_out(pairing) -> None:
    """InProgress is not a reason to wait forever, even on the right object.

    A bond that is going to take does so in seconds. One that answers
    InProgress past the limit is a call the daemon cannot finish, and asking
    again is only possible once it has been called off.
    """
    bluez = _Bluez(paired=False, accepts=True, hangs_on=DEV)
    assert (
        _run_bluez_bond(pairing, bluez, trust=True, pending_limit=0.01) is True
    )
    assert "cancel_pairing" in bluez.calls, f"never let go: {bluez.calls}"
    assert bluez.calls.count("pair") >= 2, f"never asked again: {bluez.calls}"


def test_the_panel_is_paired_where_bluez_can_see_it(pairing) -> None:
    """A leftover object must not outrank the address the panel is on.

    Measured on the van (2026-09-18): after the bond was dropped, BlueZ kept a
    Device1 for the identity address -- matched by address, carrying no RSSI
    because nothing was behind it -- while the panel advertised fresh RPAs.
    Pair() went to the leftover and spent the whole 60 s on it. BlueZ creates
    no object for those RPAs at all unless it is discovering, which Home
    Assistant's MGMT-socket scanner does not make it do.
    """
    bluez = _Bluez(paired=False, accepts=True)
    bluez.stale_identity = True
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "start_discovery" in bluez.calls, "never asked BlueZ to look"
    assert "stop_discovery" in bluez.calls, "left discovery running"
    assert bluez.paired_path == RPA_DEV, (
        f"paired the leftover instead of the live address: {bluez.paired_path}"
    )


def test_a_pairing_still_running_is_not_asked_again(pairing) -> None:
    """InProgress is BlueZ working, not the panel refusing.

    ``Device1.Pair()`` is abandoned at _PAIR_CALL_TIMEOUT but the daemon keeps
    going, and answers every further call with InProgress. Measured on the van
    (2026-09-18): two InProgress answers were the whole of a 15 s window, and
    the bond that completed underneath them was never noticed. The loop has to
    watch Paired instead of re-issuing the call.
    """
    bluez = _Bluez(paired=False, accepts=True, pairs_after=3)
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert bluez.calls.count("pair") == 1, (
        f"re-issued a call BlueZ was still running: {bluez.calls}"
    )
    assert "remove" not in bluez.calls, "InProgress was read as a refusal"


def test_a_proven_bond_is_taken_from_the_loop_too(pairing) -> None:
    """Trust is about the caller's evidence, not about which check saw it.

    Without an adapter to scope to, ``_already_bonded`` refuses to answer --
    it cannot tell this panel's bond from one on a disabled dongle -- so the
    loop is where a proven bond gets read. It must be read the same way there:
    the caller established a link before handing over, so the bond works, and
    re-pairing it would put a panel that is not in add-device mode at risk for
    nothing.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=True, adapter_path=None) is True
    assert "remove" not in bluez.calls, f"destroyed a working bond: {bluez.calls}"
    assert "pair" not in bluez.calls, f"re-paired needlessly: {bluez.calls}"


def test_a_working_bond_is_reported_not_destroyed(pairing) -> None:
    """Reconfigure on a healthy bond must stay cheap and non-destructive.

    The caller got as far as a link before handing over, so a bond on this
    adapter is one the panel honours. Removing it up front would be the
    cheaper code and would leave a panel that is not in add-device mode with
    no bond at all.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "remove" not in bluez.calls, f"destroyed a working bond: {bluez.calls}"
    assert "pair" not in bluez.calls, f"re-paired needlessly: {bluez.calls}"


def test_an_unpaired_panel_is_not_removed_first(pairing) -> None:
    """Nothing to drop: the host holds no bond, so this is a plain pairing."""
    bluez = _Bluez(paired=False, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    assert "remove" not in bluez.calls, (
        f"removed a bond that was not there: {bluez.calls}"
    )
    assert bluez.calls.count("pair") == 1


def test_a_refused_removal_is_attempted_once_and_claims_nothing(pairing) -> None:
    """A RemoveDevice BlueZ will not do must not eat the timeout, or lie.

    The bond stays suspect, so the call runs out rather than reporting a
    success it cannot see -- and it does not spend the remaining seconds
    retrying a D-Bus call that has already said no.
    """
    bluez = _Bluez(paired=True, accepts=True, removable=False)
    assert _run_bluez_bond(pairing, bluez, trust=False) is False
    assert bluez.calls.count("remove") == 1, f"retried the removal: {bluez.calls}"
    assert bluez.calls.count("pair") > 1, "should have kept asking the panel"


def test_the_link_the_bond_was_made_over_is_dropped(pairing) -> None:
    """The bond is not the end of it: what pairing opened has to be closed.

    ``Device1.Pair()`` bonds over a link of its own and BlueZ keeps it, so the
    session that follows finds the panel already connected -- and the kernel
    refuses a second link to the same peer, instantly, every time. Measured on
    the van (2026-09-18): the bond went through, then 32 connects failed with
    ``[org.bluez.Error.Failed] Input/output error`` while that link sat idle
    for fifteen minutes. One device, one entity, disconnected.

    Die Uhrzeit des Bonds stand hier einmal auf die Sekunde genau. Sie ist
    raus, weil ``test_placeholder_addresses.py`` eine Uhrzeit der Form
    ``HH:MM:SS`` nicht von drei Bytepaaren unterscheiden kann und sie zu Recht
    anschlug -- das Datum verankert die Messung genauso.
    """
    bluez = _Bluez(paired=False, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "disconnect" in bluez.calls, f"left the pairing link up: {bluez.calls}"
    assert bluez.connected is False
    assert bluez.calls.index("pair") < bluez.calls.index("disconnect"), (
        f"disconnected before pairing: {bluez.calls}"
    )


def test_a_bond_that_was_already_there_leaves_no_link_either(pairing) -> None:
    """The fast path is where a *retry* lands, which is where this bit.

    A pairing attempt that left a link behind makes the next attempt cheap --
    BlueZ still reports Paired, so the loop is never entered -- and the link is
    still there afterwards. That is the second failed pairing in a row the
    reporter sees, with nothing in the log to say why.
    """
    bluez = _Bluez(paired=True, accepts=True)
    bluez.connected = True
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "pair" not in bluez.calls, f"re-paired needlessly: {bluez.calls}"
    assert "disconnect" in bluez.calls, f"left the link up: {bluez.calls}"
    assert bluez.connected is False


def test_nothing_is_disconnected_when_no_link_is_up(pairing) -> None:
    """A bond reported with no link of its own is left entirely alone.

    Over a proxy the coordinator adopts the live client, and a Disconnect()
    fired on the way past would drop the session the caller is about to hand
    over. So the property is read first and the call only follows a link that
    is actually there.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert bluez.connected is False
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "disconnect" not in bluez.calls, (
        f"disconnected a link nobody had: {bluez.calls}"
    )


# --- part three: what counts as the panel, and what does not -----------------
#
# Diese Prüfungen richten sich gegen eine Lücke, die ein Mutationstest am
# 2026-09-26 sichtbar gemacht hat: die Attrappen oben enthalten ausschließlich
# Objekte, die das Panel *sind*. ``GetManagedObjects`` liefert aber den ganzen
# Baum des Adapters — jeden Kopfhörer und jede Lampe. Das Prädikat, das
# entscheidet, ob ein Objekt das Panel ist, war gegen Fremdgeräte ungeprüft,
# und ``Device1.Pair()`` auf dem falschen Objekt ruft an fremder Hardware an.

# Ein Fremdgerät am selben Adapter. Sein Pfad sortiert absichtlich *vor* dem
# des Panels, damit ein Gleichstand in der Rangfolge auffällt statt zufällig
# richtig auszugehen: bei gleichem Rang entscheidet der Pfadname.
STRANGER = "22:33:44:55:66:77"
STRANGER_DEV = f"{HCI0}/dev_" + STRANGER.replace(":", "_")
# Ein zweites, namenloses Fremdgerät ganz ohne UUIDs-Eintrag.
NAMELESS = "33:44:55:66:77:88"
NAMELESS_DEV = f"{HCI0}/dev_" + NAMELESS.replace(":", "_")
OTHER_DEV = f"{HCI0}/dev_" + OTHER.replace(":", "_")


def _device(address: str, **extra) -> dict:
    """Ein ``org.bluez.Device1`` mit genau den Eigenschaften, die genannt sind.

    BlueZ trägt RSSI nur, solange es das Gerät wirklich sieht; ein Objekt ohne
    RSSI ist ein Überrest. Der Vorgabewert hier ist darum "wird gesehen".
    """
    dev = {"Address": _V(address), "RSSI": _V(-55)}
    for key, value in extra.items():
        if value is None:
            dev.pop(key, None)
        else:
            dev[key] = _V(value)
    return {"org.bluez.Device1": dev}


def test_a_device_that_is_not_the_panel_is_never_taken_for_it(pairing) -> None:
    """Fremde Hardware am selben Adapter darf nicht als Panel gelten.

    Der Lastfall ist jeder echte Adapter: neben dem Panel hängen dort die
    Kopfhörer und die Lampen des Nutzers. Fällt das Prädikat, liefert
    ``_find_device`` ein Fremdgerät, und die Bond-Schleife ruft
    ``Device1.Pair()`` darauf — das Panel wird nie gekoppelt, und ein fremdes
    Gerät bekommt ohne Anlass eine Kopplungsanfrage.

    Die Attrappen der übrigen Prüfungen enthalten nur Objekte, die das Panel
    sind; ein Störer kommt in keiner von ihnen vor.
    """
    strangers = {
        # Einer mit eigenen Diensten, einer ganz ohne UUIDs-Eintrag: ohne den
        # zweiten bliebe ungeprüft, was ein fehlendes Feld auslöst.
        STRANGER_DEV: _device(STRANGER, Name="Someone's Headphones",
                              UUIDs=["0000110b-0000-1000-8000-00805f9b34fb"]),
        NAMELESS_DEV: _device(NAMELESS),
    }

    assert pairing._find_device(
        strangers, name=PANEL, address=IDENTITY, adapter_path=HCI0
    ) is None, "ein fremdes Gerät wurde für das Panel genommen"

    # Gegenprobe, damit die Behauptung nicht von einer Funktion erfüllt wird,
    # die grundsätzlich nichts findet: steht das Panel daneben, wird es geliefert.
    with_panel = dict(strangers)
    with_panel[DEV] = _device(IDENTITY, AddressType="public", Name=PANEL)
    assert pairing._find_device(
        with_panel, name=PANEL, address=IDENTITY, adapter_path=HCI0
    ) == DEV, "das Panel verlor gegen ein Fremdgerät"


def test_the_three_ways_of_matching_are_ranked(pairing) -> None:
    """Identitätsadresse vor Name vor Dienst-UUID — in dieser Reihenfolge.

    BlueZ kann für dasselbe Panel gleichzeitig mehrere Objekte tragen: seine
    Identität, eine früher aufgelöste RPA, die den Namen behalten hat, und die
    lebende RPA, die im Add-Device-Modus nur noch die Dienst-UUID anbietet.
    Fällt die Rangfolge zu einer Stufe zusammen, entscheidet der Pfadname, also
    der Zufall — und der Bond landet auf einem Objekt, hinter dem nichts ist.
    """
    identity = _device(IDENTITY, AddressType="public", Name=PANEL)
    named = _device(OTHER, AddressType="random", Name=PANEL)
    live = _device(RPA, AddressType="random", UUIDs=[TRUMA_UUID])

    both = {OTHER_DEV: named, RPA_DEV: live}
    assert pairing._find_device(
        {DEV: identity, **both}, name=PANEL, address=IDENTITY, adapter_path=HCI0
    ) == DEV, "die Identitätsadresse verlor gegen einen Namens- oder UUID-Treffer"

    # Ohne Identitätsobjekt gewinnt der Name vor der reinen Dienst-UUID.
    assert pairing._find_device(
        both, name=PANEL, address=IDENTITY, adapter_path=HCI0
    ) == OTHER_DEV, "der Namenstreffer verlor gegen den UUID-Treffer"

    # Und gesehen zu werden schlägt beides: ein Überrest ohne RSSI verliert
    # gegen die lebende RPA, auch wenn er die Identitätsadresse trägt.
    stale = _device(IDENTITY, AddressType="public", Name=PANEL, RSSI=None)
    assert pairing._find_device(
        {DEV: stale, RPA_DEV: live}, name=PANEL, address=IDENTITY,
        adapter_path=HCI0,
    ) == RPA_DEV, "ein Überrest schlug das Gerät, das BlueZ wirklich sieht"


def test_an_address_the_objects_do_not_carry_comes_from_the_path(pairing) -> None:
    """Der Rückfall auf den Pfad, und zwar als *random* gelesen.

    Der Lastfall: der Resolver hat ein Objekt gefunden, das dieser Durchgang
    von ``GetManagedObjects`` nicht trägt. Ein Panel im Add-Device-Modus wirbt
    nur auf einer Resolvable Private Address, die random ist — als public
    gelesen bekommt der Kernel die Verbindungsparameter am falschen Adresstyp,
    und eine verstümmelte Adresse gar keine.
    """
    assert pairing._device_address({}, DEV) == (IDENTITY, True), (
        "die Adresse aus dem Pfad war falsch oder galt als public"
    )

    # Ein Pfad, der kein Geräteobjekt benennt, liefert nichts.
    assert pairing._device_address({}, HCI0) is None

    # Gegenprobe: was BlueZ selbst sagt, schlägt den Pfad — auch im Adresstyp.
    objects = {DEV: _device(IDENTITY, AddressType="public", Name=PANEL)}
    assert pairing._device_address(objects, DEV) == (IDENTITY, False)


def test_a_path_that_is_not_a_bluez_object_is_refused(pairing) -> None:
    """Der Resolver darf keinen Pfad durchreichen, der keiner ist.

    Was hier zurückkommt, wird an ``Device1.Pair()`` weitergegeben. Ein Pfad
    außerhalb von ``/org/bluez/`` ist kein Geräteobjekt, und ein Wert, der
    überhaupt keine Zeichenkette ist, lässt die Prüfung selbst werfen —
    mitten in der Kopplungsschleife.
    """
    saved = pairing.async_resolve_device
    try:
        for details in ({"path": "/dev/null/dev_x"}, {"path": 42}, {}, None):
            pairing.async_resolve_device = (
                lambda *_a, _d=details, **_k: types.SimpleNamespace(details=_d)
            )
            assert pairing._live_device_path(object(), PANEL, HCI0) is None, (
                f"ein untauglicher Pfad wurde durchgereicht: {details!r}"
            )

        # Gegenprobe: ein echter BlueZ-Pfad kommt durch.
        pairing.async_resolve_device = lambda *_a, **_k: _BluezDevice(DEV)
        assert pairing._live_device_path(object(), PANEL, HCI0) == DEV
    finally:
        pairing.async_resolve_device = saved


def test_a_pairing_failure_is_the_message_not_the_class(pairing) -> None:
    """Die Entscheidung "warten oder als Ablehnung lesen" hängt am Fehlertext.

    ``_is_in_progress`` sucht in dem Text, den ``_try_pair`` zurückgibt. Kommt
    dort der Klassenname der Ausnahme statt der BlueZ-Meldung an, ist bei
    ``dbus_fast`` "DBusError" zu lesen — und die Schleife ruft jede Sekunde
    erneut ``Pair()``, erntet jedes Mal InProgress und liest es als Ablehnung,
    also als Grund, den Bond zu entfernen. Am Fahrzeug gemessen: zwei
    InProgress-Antworten füllten ein Fenster von 15 Sekunden.

    Der Klassenname der Attrappe darf das Wort darum nicht tragen — sonst ist
    "die Meldung kam an" nicht von "die Meldung ging verloren" zu
    unterscheiden.
    """
    assert pairing._is_in_progress("[org.bluez.Error.InProgress] In Progress")
    assert pairing._is_in_progress("org.bluez.Error.InProgress")
    assert not pairing._is_in_progress("DBusError"), (
        "der Klassenname von dbus_fast wurde als InProgress gelesen"
    )
    assert not pairing._is_in_progress(
        "[org.bluez.Error.AuthenticationFailed] Authentication Failed"
    )

    # Und ``_try_pair`` gibt die Meldung zurück, nicht den Namen der Klasse.
    bluez = _Bluez(paired=False, accepts=False)
    saved = pairing._get_interface
    pairing._get_interface = _interfaces(bluez)
    try:
        failure = asyncio.run(pairing._try_pair(object(), DEV))
    finally:
        pairing._get_interface = saved
    assert failure is not None
    assert "org.bluez.Error" in failure, (
        f"der Fehlertext war nicht die BlueZ-Meldung: {failure!r}"
    )


def test_the_bond_drops_on_the_adapter_of_the_device_path(pairing) -> None:
    """Ohne genannten Adapter kommt er aus dem Gerätepfad — ganz, und richtig.

    Der Lastfall ist ein Aufruf ohne ``adapter_path``: dann ist der Adapter der
    Kopf des Gerätepfads. Wird er falsch abgeschnitten, geht
    ``Adapter1.RemoveDevice`` an ein Objekt, das keinen Adapter benennt, der
    alte Bond bleibt liegen, und die Kopplung scheitert für die ganze Laufzeit.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=False, adapter_path=None) is True, (
        f"ohne genannten Adapter kam kein Bond zustande: {bluez.calls}"
    )
    assert "remove" in bluez.calls, (
        f"der alte Bond wurde nicht entfernt: {bluez.calls}"
    )


def test_a_bond_that_completes_under_an_in_progress_is_reported(pairing) -> None:
    """Der Bond entsteht, während BlueZ noch an der ersten Anfrage arbeitet.

    Drei Dinge fallen hier zusammen: der alte Bond wird entfernt, die neue
    Anfrage wird mit InProgress beantwortet, und darunter kommt sie zustande.
    Meldet ``_forget`` dann keinen Erfolg oder bleibt der Bond "verdächtig",
    läuft die Schleife bis zum Ende ihres Zeitbudgets und meldet
    "nicht gekoppelt" für eine Kopplung, die es gibt.
    """
    bluez = _Bluez(paired=True, accepts=True, pairs_after=1)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True, (
        f"eine zustande gekommene Kopplung wurde nicht gemeldet: {bluez.calls}"
    )


def test_a_bond_made_over_the_proxy_link_is_reported_as_made(pairing) -> None:
    """Der einzige Erfolgsausgang des Proxy-Pfads — und er wird zurückgegeben.

    Der Config-Flow entscheidet an diesem Flag. Steht dort ``False``, sieht der
    Nutzer "Kopplung fehlgeschlagen", obwohl das Panel den Bond quittiert hat
    — und die offene, verschlüsselte Verbindung, die der Aufrufer laut
    Docstring nun besitzt, wird bei einem Misserfolg nicht getrennt, leckt also.

    Die bestehenden Prüfungen sehen das nicht: eine behauptet nur
    ``result is not None`` über den Client, die andere stubt ``_bond_over_link``
    im einzigen Fall, der überhaupt verbindet, auf ``False``.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=True, has_proxy=True,
        connects=True, bonds=True,
    )

    assert bonded is True, "ein gelungener Bond wurde als Misserfolg gemeldet"
    assert client is not None, (
        "die lebende, verschlüsselte Verbindung wurde nicht herausgegeben"
    )
    assert log["bonded_over_link"] == 1, (
        f"nicht genau einmal über die Verbindung gebondet: {log}"
    )
    # Und ohne den Umweg über BlueZ: der Proxy-Pfad hat es selbst erledigt.
    assert log["handover"] == [], f"unnötig an BlueZ übergeben: {log['handover']}"


def test_a_bond_bluez_already_holds_is_trusted_by_default(pairing) -> None:
    """Der Vorgabewert wird von genau einem Aufrufer benutzt — und er zählt.

    Er gilt für den Zweig, in dem Home Assistant über einen lokalen Adapter
    verbunden *hat* und die Verbindung an BlueZ übergibt. Dieser Aufrufer hat
    gesehen, dass das Panel einen Link annimmt, darf einem gemeldeten Bond also
    glauben. Kippt der Vorgabewert auf Misstrauen, steht ein Nutzer, der
    "Neu konfigurieren" auf einem gesunden Bond auslöst, danach ohne Bond da —
    der im Docstring ausdrücklich benannte Schaden.

    Jede andere Prüfung übergibt ``trust_existing_bond`` ausdrücklich, und die
    beiden Pfade, die den Vorgabewert erreichen könnten, ersetzen
    ``_ensure_bonded_bluez`` durch einen Stub.
    """
    bluez = _Bluez(paired=True, accepts=True)
    saved = {
        name: getattr(pairing, name)
        for name in ("_get_interface", "async_resolve_device", "_POLL_INTERVAL")
    }
    pairing._get_interface = _interfaces(bluez)
    pairing.async_resolve_device = lambda *a, **k: _BluezDevice(DEV)
    pairing._POLL_INTERVAL = 0
    _Bus.current = bluez
    try:
        # Ohne das Schlüsselwort: genau so ruft der Übergabezweig auf.
        bonded = asyncio.run(
            pairing._ensure_bonded_bluez(
                PANEL, IDENTITY, adapter_path=HCI0, timeout=1.0, hass=object()
            )
        )
    finally:
        for name, value in saved.items():
            setattr(pairing, name, value)
        _Bus.current = None

    assert bonded is True
    assert "remove" not in bluez.calls, (
        f"ein funktionierender Bond wurde gelöscht: {bluez.calls}"
    )
    assert "pair" not in bluez.calls, (
        f"ein gemeldeter Bond wurde neu geschlossen: {bluez.calls}"
    )


def main() -> None:
    pairing = _load_pairing()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(pairing)
            print(f"ok  {name}")
    print("all passed")


try:  # pytest drives the same checks through a fixture
    import pytest
except ImportError:  # pragma: no cover
    pass
else:

    @pytest.fixture
    def pairing():
        return _load_pairing()


if __name__ == "__main__":
    main()
