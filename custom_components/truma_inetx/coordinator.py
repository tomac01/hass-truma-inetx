"""Coordinator for the Truma iNet X (BLE) integration.

Owns the shared :class:`Bus` and a background session that connects to the
panel over HA's Bluetooth stack, runs the register/subscribe/identity/
param-discovery startup, and files every notification under the device that
sent it. Reconnects with backoff on drop.
"""

from __future__ import annotations

import asyncio
import uuid

from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import session
from .ble import TrumaBleClient, close_link, device_from_bluez
from .bt import (
    ADDR_IDENTITY,
    address_kind,
    async_panel_advertising,
    async_remote_scanner_source,
    async_resolve_device,
    async_wait_until_heard,
)
from .bus import Bus
from .const import (
    DOMAIN,
    ENCRYPTION_FAILURES_BEFORE_WARNING,
    ISSUE_LOST_BOND,
    ISSUE_NO_PROXY_ROUTE_LEGACY,
    ISSUE_NO_ROUTE,
    LOGGER,
    MANUFACTURER,
    MODEL,
    NO_ROUTE_MISSES_BEFORE_WARNING,
    is_encryption_failure,
)
from .operations import OperationRegistry
from .proxy import TrumaProxyTracker
from .truma.const import CTRL_MBP, DEV_BLE_MGMT, DEV_PANEL, MBP_PARAM_DISC
from .truma.protocol import build_v3_frame, build_write_frame

type TrumaConfigEntry = ConfigEntry[TrumaCoordinator]

# Names for bus addresses whose function is known but which publish no
# Identify.Name of their own.
#
# Consulted only *after* the bus's own name, so nothing that names itself can
# be mislabelled by this -- which is the whole reason it is safe, because
# 0x06 is a class the gas-bottle sensors share and they do name themselves.
# It is not a class-to-product mapping: those are unverifiable (a Dometic roof
# air conditioner and a Schaudt electrical block share a class), whereas this
# address is the panel's own Bluetooth side and is already named in
# DEVICE_SEED for the same reason.
_KNOWN_NAMES = {DEV_BLE_MGMT: "Bluetooth management"}

# Whether this Home Assistant hangs a device off its hub by the hub's registry
# id rather than by its identifiers.
#
# ``via_device`` is deprecated as of 2026.8 and stops working in 2027.8;
# ``via_device_id`` replaced it. Asked of the type rather than of the version
# number, because the question is exactly "does this DeviceInfo take that
# key" -- and an older one does not merely ignore it, it rejects the device
# info whole and leaves the vehicle with no devices at all.
_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__

# Reconnect backoff. Start quick (a healthy link that just dropped should come
# back fast) and grow exponentially to a cap when the panel stays unreachable,
# so an out-of-range/unbonded device does not hammer — and monopolize — the
# shared Bluetooth adapter. The delay resets after any session that connected.
_RECONNECT_DELAY_BASE = 15  # seconds
# Keep the cap short. A dial that times out is not the end of the story on a
# host without address resolution: the kernel keeps trying and the link can
# come up seconds after we gave up, owned by nobody -- and a panel that thinks
# it has a central stops advertising, so the longer we wait the deeper that
# hole gets. Retrying soon is what re-attaches to such a link.
_RECONNECT_DELAY_MAX = 45  # seconds
# A healthy panel pushes frames every few seconds. If a connection goes quiet
# for this long the link is wedged (half-open, or a ghost the proxy has not
# noticed): drop it and reconnect rather than sit "connected" forever with
# stale data. This is what recovers the session without a manual power-cycle.
_DATA_STALL_TIMEOUT = 90  # seconds
# ...and the same question for the startup that runs before that watchdog does.
# Startup is the only part of a session with no deadline of its own: every step
# in it waits on the panel, and the watchdog above only starts once all of them
# have finished. Measured on the van, a healthy startup takes about 25 s from
# the link coming up to parameter discovery finishing, so this is four times
# the longest one seen and is not a latency budget -- it is the backstop for a
# step that hangs in a way its own timeout does not cover, so that the session
# ends and is retried instead of sitting "connected" and delivering nothing.
_STARTUP_TIMEOUT = 120  # seconds

# Poll mode: 0 keeps the link open (the default and what most people want --
# state arrives the instant the panel changes it). A non-zero interval connects,
# takes a reading and hangs up again, which matters when the adapter's
# connection slots are contended: a held link occupies one permanently, and on a
# single dongle shared with other devices that can starve them out entirely
# (van 2026-08-20: the DC-DC charger lost its slot and went unavailable).
#
# Seconds, not minutes: a minute is already coarse next to a poll that takes
# only a few seconds, and the interesting settings are near the bottom of the
# range. The delay is applied *after* a poll finishes, so a short interval
# cannot make polls overlap -- it just leaves less idle time between them.
CONF_POLL_INTERVAL = "poll_interval_seconds"
DEFAULT_POLL_INTERVAL = 0

# In poll mode, stop waiting once the panel has been quiet this long -- its
# startup burst arrives in one go, so silence means the reading is complete.
_POLL_QUIET = 4  # seconds
# ...but never hold the link longer than this, however chatty the panel is.
_POLL_MAX_DWELL = 40  # seconds
# A write in poll mode has to wait for a whole connect plus startup handshake
# (~20 s measured), so allow generously more than that before giving up.
_WRITE_CONNECT_TIMEOUT = 75  # seconds
# How long a stop waits for the session task to end before cancelling it. The
# loop's own waits all watch the stop event, so this is only ever spent on a
# task parked inside a connect attempt -- and it is spent by Home Assistant
# unloading the config entry, which is why it is short rather than generous.
_SESSION_EXIT_TIMEOUT = 5.0  # seconds
_STORAGE_VERSION = 1

# How often to ask the on-demand sensors for a fresh measurement while the
# link is held open (see session.request_measurements for why asking is needed
# at all). A minute is what the reporter's own build used, which is the only
# cadence anyone has run against the hardware; it is also about as often as a
# tank level can meaningfully change, and it costs two frames.
#
# In poll mode this is not used: every poll re-runs startup, which asks once,
# so the reading is as fresh as the poll it came with.
_MEASURE_INTERVAL = 60  # seconds

# Ein Schreibvorgang gilt erst als erfolgt, wenn das Gerät selbst den neuen
# Wert meldet. Der Transport-ACK sagt nur, dass das Panel den Frame genommen
# hat -- gemessen wurde ein quittierter Befehl, den die Heizung während des
# Nachlüftens nicht ausführte.
#
# Zeit, die wir dem Gerät je Anlauf für seine Antwort geben. Ein aufwachender
# Brenner meldet verzögert; zwölf Sekunden liegen über dem gemessenen Maximum
# und noch unter der Geduld eines Bedieners vor dem Panel.
_WRITE_FEEDBACK_TIMEOUT = 12  # seconds
# Ein schlafendes Gerät antwortet oft erst beim zweiten Anlauf.
_WRITE_ATTEMPTS = 3
_WRITE_RETRY_PAUSE = 2  # seconds
# Vor der Endprüfung einer Mehrfach-Transaktion, damit Folgemeldungen
# ankommen, die das Gerät erst nach dem letzten Befehl schickt.
_WRITE_SETTLE = 1  # seconds

# WaterHeating.Active meldet 1 (heizt) oder 2 (ein, Solltemperatur erreicht).
# Beides bestätigt "ein"; nur eine frische 0 bestätigt "aus". Die
# Dreiwertigkeit ist die des Protokolls (siehe bus.ActiveState) -- neu ist
# hier nur, sie als Bestätigung gelten zu lassen.
_ENABLED_STATES = (1, 2)

# Nach einem Befehl bleibt der Link mindestens so lange offen. Deutlich mehr
# als _POLL_QUIET, weil nach einer Bedienung meist weitere folgen und die
# Heizung ihre Folgeänderungen verzögert nachmeldet. Der Wert ist absolut,
# nicht additiv: zehn schnelle Befehle ergeben nicht zehn Minuten.
_COMMAND_HOLD_SECONDS = 60  # seconds

# Obergrenze für ein angefordertes Live-Fenster. Nicht als Komfortgrenze
# gedacht, sondern gegen den Vertipper: eine Dauer, die aus einem Skript
# kommt, soll das Fahrzeug nicht tagelang an einem Verbindungsplatz halten.
_MANUAL_LIVE_MINUTES_MAX = 999


class TrumaCoordinator(DataUpdateCoordinator[Bus]):
    """Hold the panel's bus and run the live BLE session."""

    config_entry: TrumaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TrumaConfigEntry,
        address: str,
        initial_client: BleakClientWithServiceCache | None = None,
    ) -> None:
        """Initialize the coordinator (push model, no polling interval).

        ``initial_client`` is a live, encrypted connection handed off from a
        just-completed pairing; the first session adopts it instead of
        reconnecting (which wedges the just-bonded RPA). Consumed once.
        """
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {address}",
            update_interval=None,
        )
        self.address = address
        self._initial_client = initial_client
        # Stable identity for entity/device unique IDs. The BLE address rotates
        # (resolvable private address), so it must NOT be used as identity.
        self.unique_id = entry.unique_id or address
        # The device registry's own id for the panel, filled in by setup once
        # it has registered it (see __init__.py). None until then, and on any
        # Home Assistant too old to want it.
        self.hub_device_id: str | None = None
        self._bus = Bus()
        self._proxy_tracker = TrumaProxyTracker(self._async_proxy_changed)
        entry.async_on_unload(self._proxy_tracker.async_setup())
        self._operations = OperationRegistry(self._async_operations_changed)
        # Der tatsächliche BLE-Sitzungszustand. ``bus.connected`` bleibt im
        # Poll-Betrieb zwischen den Polls bewusst true, damit gecachte
        # Bedienelemente verfügbar bleiben; dieses Flag ist das, was der
        # Panel-Link-Sensor stattdessen meldet.
        self._panel_link_connected = False
        self._client: TrumaBleClient | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._identity: dict | None = None
        # Loop-clock timestamp of the last frame received; drives the stall
        # watchdog in the hold loop. Set on connect, refreshed on every frame.
        self._last_frame: float = 0.0
        # RPA addresses that failed to establish a connection, so the resolver
        # rotates to another advertised address instead of hammering a dead one
        # (see the phantom-RPA explanation in bt.async_resolve_device).
        # Cleared on a successful connection and when it would block every
        # candidate, so a transiently-bad address gets retried later.
        self._avoid: set[str] = set()
        # Address of the most recent connection attempt, so _run knows which
        # one to blame if the attempt fails.
        self._last_addr: str | None = None
        # Which kind of address (identity vs rotating RPA) the panel last
        # answered on, persisted with the identity. Hosts differ in which one
        # works -- it depends on their kernel and controller, not on anything
        # we can see -- so let the host prove its own answer once instead of
        # walking the addresses that never work on every single connect
        # (issue #13). ``None`` until a session has proved something.
        self._address_kind: str | None = None
        # The kind the current attempt is dialling, and whether the memory
        # above just failed us (in which case the next attempt tries the other
        # kind -- see _prefer_identity).
        self._last_kind: str | None = None
        self._kind_stale = False
        # Which transport carried the last session that came up ("proxy" or
        # "local"), for diagnostics only. Not persisted and never dialled on:
        # HA re-scores the paths at every connect, so last session's transport
        # predicts nothing about the next one (issue #13).
        self._session_transport: str | None = None
        # Whether the current attempt ever reached "connected and subscribed".
        # A link that dropped after hours of good service says nothing about
        # which address kind is right, so only failures before that flip the
        # memory.
        self._session_ok = False
        # Consecutive resolves that found the panel advertising with nothing
        # able to connect to it. Debounces the repair issue (see
        # _async_note_no_route).
        self._no_route_misses = 0
        # Consecutive sessions that got a GATT connection up and were then
        # refused encryption. Debounces the lost-bond issue (see
        # _async_note_encryption_failure).
        self._encryption_failures = 0
        self._store: Store = Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Everything the store holds: the app identity plus our own
        # bookkeeping. Kept whole so a save never drops a key it did not know.
        self._stored: dict = {}
        self._stop = False
        # Set on stop to interrupt the reconnect wait immediately (so unload is
        # not blocked for up to the full backoff delay).
        self._stop_event = asyncio.Event()
        # Poll mode: a write cannot wait for the next scheduled poll, so it
        # nudges the loop awake and holds the link open until it has been sent.
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._writes_pending = 0
        # Ein Rückmeldungsbuch je laufendem Schreibvorgang, unter dessen
        # Vorgangs-Token: Token -> {(addr, topic, param): Wert}. Eines je
        # Vorgang und nicht eines für alle, weil Home Assistant Service-Aufrufe
        # nicht serialisiert -- eine Szene, ein `parallel:`-Skript oder schlicht
        # eine zweite Bedienung innerhalb der bis zu 40 s, die ein Befehl
        # braucht, lässt zwei Vorgänge gleichzeitig warten. Mit einem
        # gemeinsamen Buch löschte der zweite dem ersten seine Meldungen weg,
        # und ein ausgeführter Befehl würde als "did not confirm" gemeldet.
        # Unter dem Token und nicht in einer Liste, weil zwei noch leere Bücher
        # gleich sind und ``list.remove`` dann das falsche träfe.
        # Leer heißt: es wird gerade nicht geschrieben, und der Frame-Pfad
        # spart sich die Buchführung.
        self._write_feedback: dict[int, dict[tuple[int, str, str], int]] = {}
        # Loop-Zeitpunkt, bis zu dem nach einem Befehl nicht aufgelegt wird.
        self._command_hold_until = 0.0
        # Dasselbe für ein angefordertes Live-Fenster.
        self._manual_hold_until = 0.0
        # Eine Anfrage aus dem Dashboard kann den Poll-Betrieb wecken, ohne
        # einen Parameter-Write vorzutäuschen. Die gewünschte Haltezeit
        # beginnt erst nach dem Handshake, damit ein langsamer BLE-Aufbau die
        # Live-Zeit des Nutzers nicht verbraucht.
        self._manual_wake_pending = False
        self._manual_hold_request_minutes: int | None = None
        # Einweg-Wunsch "bitte auflegen". Die Verweilschleife liest ihn erst
        # *nach* den Writes, damit ein laufender Befehl nicht abgeschnitten
        # wird.
        self._manual_release_requested = False
        # token -> Event. Solange nicht leer, besitzt ein manueller
        # Lesevorgang die Sitzung; das Event ist sein Abbruchkanal.
        self._manual_requests: dict[int, asyncio.Event] = {}
        # Das Token der jüngsten manuellen Anfrage. Ein spät scheiternder
        # Vorgang erkennt daran, dass er nicht mehr der Besitzer des Fensters
        # ist, und räumt dann nichts weg.
        self._manual_operation: int | None = None
        # Von der Number-Entität gesetzt, vom Sync-Button gelesen (Task 10).
        self.manual_live_minutes = 0
        # Stand der Optionen, gegen den der Update-Listener vergleicht. Gefüllt
        # wird er erst beim Anmelden des Listeners (siehe __init__.py): nur
        # dort beginnen Kopie und Zuhören im selben Moment.
        self.known_options: dict = {}

    def _hold_after_command(self) -> None:
        """Den Link nach einem Befehl offen halten.

        Absolut, nicht additiv: jeder Befehl setzt dasselbe Fenster neu ab
        *jetzt*. Zehn schnelle Befehle ergeben also eine Minute Nachlauf, nicht
        zehn -- aufaddiert hinge das Wohnmobil nach einer Bedienfolge minutenlang
        am Panel, obwohl längst niemand mehr etwas erwartet.
        """
        self._command_hold_until = self.hass.loop.time() + _COMMAND_HOLD_SECONDS

    @property
    def manual_session_active(self) -> bool:
        """Ob ein angefordertes Live-Fenster noch läuft.

        Nur im Poll-Betrieb eine sinnvolle Frage: ein Dauerlink ist ohnehin
        immer live.
        """
        return (
            bool(self.poll_interval)
            and self.hass.loop.time() < self._manual_hold_until
        )

    async def async_request_manual_session(self, minutes: int) -> None:
        """Jetzt synchronisieren und den Poll-Betrieb optional offen halten.

        ``minutes`` ist die Zeit, die der Link *nach* der Synchronisation noch
        offen bleiben soll. 0 heißt genau einmal lesen -- nicht unendlich.
        """
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int)
            or not 0 <= minutes <= _MANUAL_LIVE_MINUTES_MAX
        ):
            raise HomeAssistantError(
                f"Live mode duration must be a whole number from 0 to "
                f"{_MANUAL_LIVE_MINUTES_MAX} minutes"
            )

        with self._operations.operation("sync") as token:
            self._manual_operation = token
            cancelled = asyncio.Event()
            self._manual_requests[token] = cancelled
            try:
                await self._request_manual_session(minutes, cancelled)
                if cancelled.is_set():
                    raise HomeAssistantError("Manual refresh cancelled")
            except (Exception, asyncio.CancelledError):
                # Ein Refresh bei stehender Verbindung setzt sein Fenster vor
                # dem Lesen. Ein Fehlschlag muss es freigeben -- darf aber eine
                # neuere Anfrage nicht rückgängig machen.
                if self._manual_operation == token:
                    self._manual_hold_until = 0.0
                raise
            finally:
                self._manual_requests.pop(token, None)
                if self._manual_operation == token:
                    self._manual_hold_request_minutes = None
                    self._manual_wake_pending = False

    async def _request_manual_session(
        self, minutes: int, cancelled: asyncio.Event
    ) -> None:
        """Den Refresh unter dem Besitzer seines Vorgangs ausführen."""
        client = self._client
        if client is not None and client.connected and self._connected_event.is_set():
            self._manual_release_requested = False
            self._manual_hold_until = (
                self.hass.loop.time() + minutes * 60
                if self.poll_interval and minutes
                else 0.0
            )
            # Der Startup hat die gewöhnlichen Parameter eben aufgefrischt.
            # Bei bereits offener Verbindung auch die Sensoren fragen, die nur
            # auf Anfrage messen.
            before = self._bus.last_update
            await self._discover_params(client)
            await self._request_measurements(client)
            if self._bus.last_update == before:
                raise HomeAssistantError(
                    "Truma refresh received no fresh panel parameters"
                )
            return

        # Keine Verbindung: die Schleife wecken und die Dauer aufheben, bis
        # der Handshake steht (siehe _finish_startup).
        self._manual_hold_request_minutes = minutes
        self._manual_wake_pending = True
        self._manual_release_requested = False
        self._connected_event.clear()
        self._wake_event.set()
        LOGGER.debug(
            "Truma %s: manual session requested (%d minute live hold)",
            self.unique_id,
            minutes,
        )
        connected = asyncio.ensure_future(self._connected_event.wait())
        stopped = asyncio.ensure_future(cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {connected, stopped},
                timeout=_WRITE_CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped in done:
                raise HomeAssistantError("Manual refresh cancelled")
            if connected not in done:
                raise HomeAssistantError(
                    "Truma panel did not answer in time for the manual refresh"
                )
        finally:
            connected.cancel()
            stopped.cancel()
            await asyncio.gather(connected, stopped, return_exceptions=True)

    async def async_end_manual_session(self) -> None:
        """Ein Live-Fenster freigeben, ohne einen laufenden Befehl abzuschneiden."""
        # Nur freigeben, was es auch gibt: der Wunsch gilt genau einem
        # laufenden Fenster. Der Knopf ist absichtlich immer bedienbar (er
        # darf nicht verschwinden, wenn BLE unten ist), wird also auch im
        # gewöhnlichen Poll-Betrieb gedrückt, wo der Link zwischen zwei
        # Abfragen liegt. Bliebe der Wunsch dort stehen, schnitte er den
        # *nächsten* Poll nach einer Sekunde ab -- gemessen 1 s statt 40 s
        # bei durchredendem Panel, und der brächte nur noch die
        # Startup-Werte. Eine wartende Anfrage braucht den Wunsch nicht: sie
        # wird über ihr Event abgebrochen, und die gelöschte
        # ``_manual_hold_request_minutes`` lässt den Startup gar kein Fenster
        # erst antreten.
        releasing = self.manual_session_active
        self._manual_hold_request_minutes = None
        self._manual_wake_pending = False
        self._manual_hold_until = 0.0
        if releasing:
            self._manual_release_requested = True
        for cancelled in self._manual_requests.values():
            cancelled.set()
        if not self._writes_pending:
            self._wake_event.clear()
        # Die Poll-Verweilschleife prüft das einmal pro Sekunde, und zwar nach
        # den Writes -- sie legt daher nie unter einem laufenden Befehl auf.
        LOGGER.debug("Truma %s: manual live mode released", self.unique_id)

    def _reconnect_delay(self, connected: bool, current: float) -> float:
        """Die Wartezeit vor der nächsten Sitzung.

        Ein Live-Modus-Fenster und ein Befehls-Nachlauf überleben einen
        unerwarteten BLE-Abriss: solange eines von beiden läuft, wird schnell
        neu verbunden statt im Poll-Takt oder mit gewachsenem Backoff.

        ``current`` ist der gewachsene Backoff des Aufrufers; er gilt nur für
        einen Versuch, der gar nicht erst zustande kam.
        """
        if (
            self.manual_session_active
            or self.hass.loop.time() < self._command_hold_until
        ):
            return _RECONNECT_DELAY_BASE
        if connected and self.poll_interval:
            # A completed poll is not a failure to back off from; the next one
            # is simply due later.
            return self.poll_interval
        if connected:
            return _RECONNECT_DELAY_BASE
        return current

    async def _async_update_data(self) -> Bus:
        """Return the shared bus (updated by BLE notifications)."""
        return self._bus

    @property
    def proxy_available(self) -> bool | None:
        """Ob der für dieses Panel benutzte ESPHome-Proxy registriert ist."""
        return self._proxy_tracker.available

    @property
    def panel_link_connected(self) -> bool:
        """Ob gerade eine physische BLE-Sitzung zum Panel offen ist."""
        return self._panel_link_connected

    @callback
    def _set_panel_link_connected(self, connected: bool) -> None:
        """Einen echten Link-Wechsel an Entitäten und Logbuch veröffentlichen."""
        if self._panel_link_connected == connected:
            return
        self._panel_link_connected = connected
        self.async_set_updated_data(self._bus)

    @callback
    def _async_proxy_changed(self) -> None:
        """Geänderte Proxy-Registrierung an die Entitäten geben."""
        self.async_set_updated_data(self._bus)

    @callback
    def _async_operations_changed(self) -> None:
        """Einen Vorgangswechsel an die Entitäten geben."""
        self.async_set_updated_data(self._bus)

    @property
    def operation_state(self) -> str:
        """Der Zustand für den Vorgangs-Sensor."""
        return self._operations.state

    @property
    def operation_attributes(self) -> dict:
        """Die Attribute für den Vorgangs-Sensor."""
        return self._operations.attributes

    @property
    def energy_source_changing(self) -> bool:
        """Ob eine Energiequellen-Transaktion läuft oder wartet."""
        return self._operations.changing("energy_source")

    @callback
    def _remember_proxy_for_address(self, address: str) -> None:
        """Den entfernten Scanner merken, der diese Panel-Route geliefert hat."""
        if source := async_remote_scanner_source(self.hass, address):
            self._proxy_tracker.remember_source(source)

    def _async_note_no_route(self) -> None:
        """Warn the user when the panel is audible but nothing can connect.

        The panel uses a rotating private address, and it only answers a
        connect that puts its current address on air. Whether a given adapter
        does that is a property of the host: a controller with LL Privacy and
        the panel's key in its resolving list does it, a kernel below 6.19 does
        it in software, an ESPHome proxy's controller does it. A host where
        none of them applies pairs once and then never reconnects, which looks
        like a broken integration rather than a Bluetooth setup that cannot
        serve this panel. Say so instead of failing silently.

        What to *do* about it is deliberately not prescribed here beyond the
        facts: this integration does not choose the adapter -- Home Assistant
        scores every path it has and picks -- so it is in no position to say
        which piece of the user's setup is the wrong one.
        """
        if not async_panel_advertising(self.hass, self.unique_id):
            # We cannot hear the panel at all -- off, asleep or out of range.
            # That is a different fault with different advice, so stay quiet
            # and do not let it count towards the warning either.
            return
        self._no_route_misses += 1
        if self._no_route_misses != NO_ROUTE_MISSES_BEFORE_WARNING:
            # Fires exactly once on the way up, so repeated failures do not
            # re-create the issue and re-notify every reconnect attempt.
            return
        LOGGER.warning(
            "Truma %s is advertising but nothing Home Assistant can reach it "
            "with is able to connect",
            self.unique_id,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NO_ROUTE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_ROUTE,
            learn_more_url=(
                "https://github.com/rpodgorny/hass-truma-inetx"
                "#reaching-the-panel"
            ),
        )

    def _async_clear_no_route(self) -> None:
        """Reset the miss counter and drop the issue if it was raised."""
        self._no_route_misses = 0
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_ROUTE)
        # An issue this integration raised under the old id would otherwise
        # outlive the rename, showing the user a card with no text behind it.
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_PROXY_ROUTE_LEGACY)

    def _async_note_encryption_failure(self, exc: Exception) -> None:
        """Warn when session after session reaches the panel and cannot encrypt.

        The fault this names is one-sided and silent. The adapter or proxy
        holding the bond has lost its key; the panel still lists it as paired,
        so it demands encryption we cannot provide, and it refuses to pair
        afresh unless a human puts it into add-device mode. Nothing on this
        side can heal it, and nothing on this side said so: on 2026-09-28 it
        ran for 41 hours with every entity unavailable and no word anywhere
        (REV-007).

        Two gates, and both carry weight:

        * the session has to have ended *in* an encryption refusal (ATT 0x0f),
          not in any of the dozen ordinary ways a session ends, and
        * a GATT connection has to have come up. That is what keeps this apart
          from ISSUE_NO_ROUTE, whose fault is "nothing can get near it" and
          whose advice is about adapters and range.

        The run is broken by exactly one thing: a subscribe that encrypted
        (``_async_clear_encryption_failure``). A session that reached GATT and
        died of anything else leaves the count where it was -- one cycle
        produces several kinds of failure (REV-007) and the fault lasts days,
        so a run that any stray error could zero would keep starting over.

        The count lives on this coordinator and every reload rebuilds it at
        zero, so the notice can only rise reliably because an address update
        no longer reloads the entry (the comparison in
        ``__init__._async_update_listener``); whoever simplifies that
        comparison makes it unreliable again, and the one test that would
        object, test_options_reload.py, pins the behaviour but not this
        reason.

        ``client.transport`` is the second gate. ``TrumaBleClient`` assigns its
        inner bleak client only once ``establish_connection`` has returned
        (ble.py:247), so a transport that is not ``None`` means a link was up.
        Not ``client.connected``, which asks whether it is up *now*: it
        usually is not, because the proxy tears the link down on the refused
        write (ble.py:277).
        """
        client = self._client
        if client is None or client.transport is None:
            # Never reached GATT, so nothing refused us anything. Not counted
            # either -- otherwise a spell out of range pre-loads the counter
            # and the first real refusal trips the warning.
            return
        if not is_encryption_failure(exc):
            # Reached GATT, ended in something we cannot name. The count stays
            # where it is: this says nothing about whether the fault is gone
            # (see the docstring), so neither counting nor resetting is right.
            #
            # It is logged because otherwise it vanishes. If a library changes
            # the wording of the refusal, the classifier stops matching and
            # this notice goes quiet again -- the very failure it exists to
            # end. This warning is where that shows up, and it carries the
            # text a person needs to correct the marker.
            LOGGER.warning(
                "Truma %s: session reached the panel and ended in an error "
                "not recognised as an encryption refusal (%s: %s)",
                self.unique_id,
                type(exc).__name__,
                exc,
            )
            return
        self._encryption_failures += 1
        if self._encryption_failures != ENCRYPTION_FAILURES_BEFORE_WARNING:
            # Fires exactly once on the way up, like _async_note_no_route: the
            # panel refuses every reconnect, and re-creating the issue each
            # time would re-notify for as long as the fault lasts.
            return
        LOGGER.warning(
            "Truma %s refuses to encrypt: our key for it is gone while it "
            "still holds its key for us -- the bond has to be renewed",
            self.unique_id,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_LOST_BOND,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LOST_BOND,
        )

    def _async_clear_encryption_failure(self) -> None:
        """Reset the run and drop the issue: encryption just worked."""
        self._encryption_failures = 0
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_LOST_BOND)

    async def async_start(self) -> None:
        """Load identity and launch the background BLE session."""
        await self._load_stored_state()
        # Kept so async_stop can end the session itself. Home Assistant
        # cancels an entry's background tasks on unload, but only *after*
        # async_unload_entry has returned -- which is too late to be the thing
        # that closes our link (see async_stop).
        self._session_task = self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} session {self.address}"
        )

    async def async_stop(self) -> None:
        """Stop the session and close every link this coordinator holds.

        The order is the fix for an entry that reloads into nothing. A task
        cancelled mid-flight cannot run its own teardown -- the first ``await``
        in its ``finally`` raises ``CancelledError`` straight away -- so
        anything it still held is left connected, feeding a coordinator that
        has stopped and holding one of the panel's ~4 connection slots. Home
        Assistant cancels the entry's background tasks itself, right after
        async_unload_entry returns, so unless the task is ended *here* that
        cancellation is what ends it, at the one moment we can no longer close
        what it was holding.

        So: stop the task and wait for it, and only then disconnect. Every
        await below is bounded, because this is awaited by the unload itself
        and a hang here is an entry that never comes back.
        """
        self._stop = True
        self._stop_event.set()
        await self._stop_session_task()
        await self._disconnect_client()
        await self._release_initial_client()

    async def _stop_session_task(self) -> None:
        """End the background session, cancelling it if it will not end.

        ``_stop`` and the stop event are already set, and every wait in the
        session loop watches one of them, so the ordinary path here is a few
        milliseconds. What needs the bound is a task parked inside a connect
        attempt: bleak's establish_connection can sit for tens of seconds, and
        the unload is waiting on this.
        """
        task = self._session_task
        self._session_task = None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, _SESSION_EXIT_TIMEOUT)
        except TimeoutError:
            # wait_for has cancelled it and waited for the cancellation to
            # land. Say so: a link the task was still establishing is one
            # nothing can close afterwards, and this line is the only way to
            # tell that case apart later.
            LOGGER.warning(
                "Truma %s: the session did not stop within %ss and was "
                "cancelled; a link it was still opening may stay up until the "
                "panel drops it",
                self.unique_id,
                _SESSION_EXIT_TIMEOUT,
            )
        except asyncio.CancelledError:
            if not task.cancelled():
                # Not the session's cancellation but our own: something is
                # cancelling the unload that called this. Swallowing it would
                # hide that from Home Assistant, which is worse than the links
                # this leaves behind -- and the panel drops those in its own
                # time.
                raise
            LOGGER.debug("Truma %s: session task was already cancelled", self.unique_id)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            LOGGER.debug("Truma %s session task ended: %s", self.unique_id, exc)

    async def _release_initial_client(self) -> None:
        """Close a handed-off pairing link that no session ever adopted.

        The config flow hands its live, encrypted connection to setup instead
        of letting the session reconnect, because reconnecting is what wedges
        the just-bonded RPA. Between setup and the first connect attempt this
        coordinator is the only thing holding that link, and an entry reloaded
        in that window -- enabling or disabling one entity is enough, Home
        Assistant reloads on that by itself -- used to drop the reference with
        the link still up.
        """
        client = self._initial_client
        self._initial_client = None
        if client is None:
            return
        LOGGER.debug(
            "Truma %s: closing the handed-off pairing link, never adopted",
            self.unique_id,
        )
        await close_link(client, self.unique_id)

    async def _disconnect_client(self) -> None:
        """Disconnect and drop the current BLE client, best effort.

        Frees the connection slot on whichever adapter or proxy carried it,
        so the next attempt starts clean.

        ``_connected_event`` fällt in derselben Anweisungsfolge wie der
        Client, und zwar **vor** dem ``await`` auf ``disconnect()``. Das Event
        heißt „es gibt eine Sitzung, durch die geschrieben werden kann"; wer
        es länger stehen lässt als den Client, belügt jeden, der darauf
        wartet. Genau das war das Fenster: Der Abbau kann Sekunden dauern,
        und ein Befehl darin wartete auf ein bereits gesetztes Event, kehrte
        sofort zurück, fand denselben fehlenden Client und wurde mit „not
        connected" abgewiesen -- statt auf den nächsten Poll zu warten.
        """
        client = self._client
        self._client = None
        self._connected_event.clear()
        if client is None:
            LOGGER.debug("Truma %s: no live BLE link to close", self.unique_id)
            self._set_panel_link_connected(False)
            return
        try:
            await client.disconnect()
            LOGGER.debug("Truma %s: BLE link closed cleanly", self.unique_id)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            LOGGER.debug("Truma %s disconnect: %s", self.unique_id, exc)
        finally:
            # Auch ein gescheiterter Disconnect lässt keinen Link zurück,
            # den wir noch melden dürften.
            self._set_panel_link_connected(False)

    async def _load_stored_state(self) -> None:
        """Load the persisted app identity and address-kind memory.

        The identity is created and stored on first run; the memory is written
        only once a session has proved one (see _remember_address_kind), so it
        is absent on a fresh install and the resolver keeps its default order.
        """
        data = await self._store.async_load()
        if not data:
            data = {
                "muid": str(uuid.uuid4()).upper(),
                "uuid": str(uuid.uuid4()).lower(),
                "username": "Home Assistant",
            }
            await self._store.async_save(data)
        self._stored = data
        # Hand the protocol only what it writes to the panel: the stored blob
        # also carries our own bookkeeping, which is none of its business.
        self._identity = {
            key: data[key] for key in ("muid", "uuid", "username") if key in data
        }
        self._address_kind = data.get("address_kind")

    def _prefer_identity(self) -> bool:
        """Whether to dial the identity address before the RPAs.

        With no memory, keep the old order (RPAs first). With one, follow it --
        unless it just failed, in which case try the other kind next. That
        alternation is what makes a wrong memory cost one attempt rather than
        the connection: a host that loses the route it learned (a kernel
        upgrade taking the local adapter away, a proxy that moved) finds the
        other one by itself instead of looking like broken hardware.
        """
        if self._address_kind is None:
            return False
        prefer = self._address_kind == ADDR_IDENTITY
        return not prefer if self._kind_stale else prefer

    async def _remember_address_kind(self, kind: str) -> None:
        """Persist the kind of address that just carried a session.

        Persisted rather than kept in memory because the cost this avoids is
        paid at startup: the reporter in issue #13 measured 11.5 minutes from
        HA start to a live session, against 1.2 minutes when the working
        address was dialled first.
        """
        self._kind_stale = False
        if kind == self._address_kind:
            return
        LOGGER.debug(
            "Truma %s: connects on the %s address; remembering it",
            self.unique_id,
            kind,
        )
        self._address_kind = kind
        self._stored["address_kind"] = kind
        await self._store.async_save(self._stored)

    async def _run(self) -> None:
        """Maintain the BLE session, reconnecting with exponential backoff."""
        delay = _RECONNECT_DELAY_BASE
        while not self._stop:
            # Nur den Weck-Impuls verbrauchen. Die gewünschte Dauer bleibt
            # offen, bis der Startup gelungen ist -- ein gescheiterter Anwahl-
            # versuch muss aber trotzdem den Backoff respektieren, statt ohne
            # Pause durchzudrehen.
            self._manual_wake_pending = False
            connected = False
            try:
                connected = await self._connect_and_run()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Truma session ended: %s", exc)
                # First: an exception here ends ``_run``, so the older and
                # essential bookkeeping goes ahead of the newer call.
                self._note_attempt_failed()
                # Before the ``finally`` below: _disconnect_client drops
                # ``_client``, and the client is what says whether a GATT
                # connection ever came up.
                self._async_note_encryption_failure(exc)
            finally:
                # Always tear the client down before the next attempt so a
                # half-open link never lingers holding a connection slot (the
                # ghost that otherwise needs a manual power-cycle).
                # ``_disconnect_client`` löscht dabei ``_connected_event``,
                # und zwar vor seinem eigenen ``await`` -- hier nachträglich
                # zu löschen kam zu spät (siehe dort).
                await self._disconnect_client()
            if connected and self.poll_interval and not self._stop:
                # Poll mode: the link going away is the plan, not a fault. The
                # reading we just took is still the current state, so leave the
                # entities alone -- flagging disconnected here made every value
                # flash up and then go "unavailable" until the next poll.
                LOGGER.debug(
                    "Truma %s: poll finished, staying available until the next one",
                    self.unique_id,
                )
            else:
                self._mark_disconnected()
            if self._stop:
                break
            # A session that actually connected resets the backoff (a healthy
            # link that just dropped should return fast); a failed attempt grows
            # it after the wait, so a persistently unreachable panel backs off
            # the shared adapter instead of hammering it.
            delay = self._reconnect_delay(connected, delay)
            if connected:
                # A real connection means our address set is healthy; forget any
                # past failures so a later reconnect starts from a clean slate.
                # Auch nach einem Poll: dass der Link danach planmäßig fällt,
                # macht die Adresse nicht schlechter.
                self._avoid.clear()
            LOGGER.debug("Truma %s reconnecting in %ss", self.unique_id, delay)
            await self._wait_before_retry(delay)
            if not connected and not self.manual_session_active:
                # Im Live-Fenster wartet ein Mensch: dann lieber gleich wieder
                # anklopfen als den Backoff verdoppeln.
                delay = min(delay * 2, _RECONNECT_DELAY_MAX)

    def _note_attempt_failed(self) -> None:
        """Learn from an attempt that ended badly, at both levels.

        The address: if the attempt never got a link up, demote it so the
        resolver rotates to another advertised RPA next round instead of
        hammering a post-pairing phantom (see bt.py). It is a demotion, not a
        ban -- when it is the only route left the resolver still hands it back,
        which is what a transient failure (the panel holding the slot of a
        just-closed session) needs. Only a failed *connect* leaves
        ``_last_addr`` set; a later failure clears it.

        The address KIND: if the kind we remember just failed to carry a
        session, ignore the memory next time and try the other one; if it was
        the other kind that failed, go back to the memory. Alternating is what
        keeps a stale memory from wedging a host whose working route changed --
        a kernel upgrade taking the local adapter away, say. A session that ran
        and then dropped teaches nothing here, so ``_session_ok`` gates it.
        """
        if self._last_addr:
            self._avoid.add(self._last_addr)
        if not self._session_ok and self._last_kind is not None:
            self._kind_stale = self._last_kind == self._address_kind

    async def _wait_before_retry(self, delay: float) -> None:
        """Sleep ``delay`` seconds; wake early on stop, write or live request.

        ``_writes_pending`` und ``_manual_wake_pending`` sind die Wahrheit,
        ``_wake_event`` ist nur der Anstoß: ein Befehl oder eine Live-Anfrage,
        die in die Lücke zwischen zwei Sitzungen fällt, kann so nicht dadurch
        verlorengehen, dass das Event im falschen Moment gelöscht wird.
        """
        if self._writes_pending or self._manual_wake_pending:
            return
        self._wake_event.clear()
        if self._writes_pending or self._manual_wake_pending:  # set while clearing
            return
        stop = asyncio.ensure_future(self._stop_event.wait())
        wake = asyncio.ensure_future(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stop, wake}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop.cancel()
            wake.cancel()

    async def _connect_and_run(self) -> bool:
        """Connect, run startup, then hold until the link drops.

        Returns ``True`` once the connection was established (so the caller
        resets the backoff). Raises if the connection could not be established.
        """
        assert self._identity is not None
        # Nothing has been dialled or proved yet this round. Clearing the kind
        # matters: an attempt that ends before it picks an address (the panel
        # silent, nothing connectable) is not evidence about address kinds, and
        # last round's kind left lying here would flip the memory on it.
        self._session_ok = False
        self._last_kind = None
        client = TrumaBleClient(self._identity)
        client.on_data(self._on_frame)
        # Track the client before connecting so a failed/partial connect is
        # still torn down by _run's finally (freeing the connection slot).
        self._client = client

        # First attempt after a fresh pairing: adopt the live connection the
        # config flow handed off, instead of reconnecting. This is what avoids
        # the post-pairing RPA wedge — never disconnect the bonded link.
        initial = self._initial_client
        self._initial_client = None  # consume: adopt only once
        if initial is not None:
            if initial.is_connected:
                LOGGER.debug(
                    "Truma %s: adopting handed-off pairing connection %s",
                    self.unique_id,
                    initial.address,
                )
                self._last_addr = None
                # An adopted link was dialled by the config flow, not by us,
                # so it says nothing about which address kind this host
                # connects on.
                self._last_kind = None
                await client.adopt(initial)
                self._set_panel_link_connected(True)
                # Both this path and the dial below subscribe, and the panel's
                # characteristics are protected, so getting this far proves our
                # key is good -- the one fact the lost-bond issue turns on.
                # Here and not beside _async_clear_no_route: a resolve that
                # succeeded says nothing about encryption, and clearing there
                # would drop the issue on the very attempt that is about to be
                # refused again.
                self._async_clear_encryption_failure()
                return await self._finish_startup(client)
            # Handed-off link dropped in the setup gap — discard and connect
            # fresh below.
            LOGGER.debug(
                "Truma %s: handed-off connection was already closed; "
                "connecting fresh",
                self.unique_id,
            )
            try:
                await initial.disconnect()
            except Exception as exc:  # noqa: BLE001 - best effort
                LOGGER.debug("Truma %s stale handoff disconnect: %s", self.unique_id, exc)

        ble_device = async_resolve_device(
            self.hass,
            self.unique_id,
            avoid=self._avoid,
            prefer_identity=self._prefer_identity(),
        )
        if ble_device is None and self._avoid:
            # Nothing is on air at all, so the grudges are about addresses the
            # panel no longer uses. Drop them: a set that only ever grew would
            # keep demoting whatever the panel comes back on. (It cannot be
            # "everything was avoided" — avoid only demotes, so a reachable
            # address is always returned; see bt.async_resolve_device.)
            LOGGER.debug(
                "Truma %s: nothing advertising; forgetting past failures",
                self.unique_id,
            )
            self._avoid.clear()
        if ble_device is None:
            # Silence usually means the opposite of unreachable: BlueZ is
            # already holding a link, so the panel has a central and stops
            # advertising. Take BlueZ's own device object and attach to it.
            ble_device = await device_from_bluez(self.unique_id)
            if ble_device is not None:
                LOGGER.debug(
                    "Truma %s: not advertising, but BlueZ has the device; "
                    "attaching to its object",
                    self.unique_id,
                )
        if ble_device is None:
            self._async_note_no_route()
            raise HomeAssistantError(
                f"Truma {self.unique_id} not currently advertising"
            )
        # A resolve that succeeded disproves the issue outright: something
        # connectable reached the panel. (The adopted-handoff path above needs
        # no equivalent -- it only happens straight after pairing, which itself
        # required a working route, so the issue cannot already be raised.)
        self._async_clear_no_route()
        self._last_addr = ble_device.address
        self._last_kind = address_kind(self.unique_id, ble_device.address)
        self._remember_proxy_for_address(ble_device.address)
        # Dial while the panel is still audible: the resolved address is only
        # good for as long as the host's cache of it is (see
        # bt.async_wait_until_heard). A stale dial costs a ~20 s timeout during
        # which nothing scans, so it keeps itself stale.
        if not await async_wait_until_heard(self.hass, self.unique_id):
            # Silence is not a reason to give up: the commonest cause of it is
            # that something already holds a link to the panel, and a panel
            # with a central does not advertise. Connecting then costs nothing
            # and attaches to that link instead of leaving it unused, which is
            # exactly the hole a wait-only gate digs. A genuinely absent panel
            # costs one connect timeout.
            LOGGER.debug(
                "Truma %s: connecting without a fresh advert", self.unique_id
            )
        await client.connect(ble_device)
        self._set_panel_link_connected(True)
        # Subscribed, so our key is good -- see the adopted path above.
        self._async_clear_encryption_failure()
        # The connection established, so this address is not the phantom —
        # clear the blame marker so a later failure (startup, a mid-session
        # drop) does not wrongly banish a perfectly good address.
        self._last_addr = None

        return await self._finish_startup(client)

    @property
    def address_kind(self) -> str | None:
        """Which kind of address the panel last answered on, if known.

        Exposed for diagnostics: it is the one piece of per-host state this
        integration learns, and a download that does not say which address kind
        a host settled on cannot explain its connect times.
        """
        return self._address_kind

    @property
    def session_transport(self) -> str | None:
        """Which transport carried the last session that came up, if any.

        Exposed for diagnostics beside :attr:`address_kind`: the address kind
        says which address answered, this says which adapter it answered on.
        Neither is dialled on (see ble.TrumaBleClient.transport).
        """
        return self._session_transport

    def device_info(self, addr: int) -> DeviceInfo:
        """The Home Assistant device an entity at a bus address belongs to.

        One HA device per bus address that has published something, hanging
        off the panel. That is what the panel is: a gateway onto a TIN bus of
        Truma appliances, a CI bus of vehicle electrics and third-party air
        conditioners, a CAN bus and Bluetooth gas sensors. Folding all of it
        into a single device -- which is what this did, with the model
        hard-coded to "iNet X (Combi)" -- meant a roof air conditioner's fan
        and a Combi's fan were two entities on one device with nothing saying
        which was which.

        The panel itself is that hub rather than a device below it. It is the
        thing this integration holds a Bluetooth link to, so giving it a
        second HA device beside the config entry's own would list the same
        appliance twice. DEV_PANEL is assumed to be the panel's address, as it
        is everywhere else here; if some installation numbers it differently
        the cost is one extra device, not a broken one.

        Names come from what the bus says: the panel's own name for a device
        under Identify.Name, plus the owner's own label for it where there is
        one -- GasBtl.Name reads "Links" on one bottle and "Rechts" on the
        other, so the two devices are "Truma LevelControl Links" and "Truma
        LevelControl Rechts". That is the one identity that survives a
        re-pairing: the instance is part of the address and is reassigned,
        while the label is stored in the device. Where no label is published,
        or two devices share one, the class instance separates them instead --
        but only where there is something to separate. Two gas-bottle sensors
        publishing "Truma LevelControl" get 0x0603's and 0x0604's instances,
        because "Truma LevelControl" twice would be no better than the flat
        reading that mixed them up; a bus whose names are already distinct
        keeps them as they are. Measured on the bus of #23, where the suffix
        used to fire on every instance above 1: a Schaudt block and a Dometic
        roof unit share device class 0x04 and nothing else, and came up as
        "EBL25x 5" and "FreshJet 6" -- two numbers answering a question the
        names had already answered.

        A device that publishes no name is named by its address rather than by
        a class-to-product mapping that cannot be verified -- those same two
        share a device class. The one exception is _KNOWN_NAMES, and it is an
        exception only for addresses that name nothing themselves.
        """
        if addr == DEV_PANEL:
            panel = self._bus.devices.get(addr)
            return DeviceInfo(
                identifiers={(DOMAIN, self.unique_id)},
                # The advertised BLE name, not the panel's Identify.Name:
                # two panels in one Home Assistant would otherwise be two
                # devices with the same name.
                name=self.unique_id,
                manufacturer=MANUFACTURER,
                model=(panel.name if panel else None) or MODEL,
                serial_number=panel.serial if panel else None,
            )
        device = self._bus.device(addr)
        base = device.name or _KNOWN_NAMES.get(addr)
        label = device.label
        if base is None and label is None:
            # Already unique, and already says where it is.
            name = f"Bus device 0x{addr:04X}"
        elif base is None:
            name = label
        elif label and label != base and self._bus.label_is_unique(addr, label):
            name = f"{base} {label}"
        elif self._bus.name_is_unique(addr, base):
            name = base
        else:
            name = f"{base} {device.instance}"
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.unique_id}_{addr:04X}")},
            name=name,
            model=device.name,
            serial_number=device.serial,
        )
        # Hung off the panel, by whichever of the two names for that this
        # Home Assistant takes.
        #
        # ``via_device`` names the hub by its identifiers and is deprecated as
        # of 2026.8 -- it stops working in 2027.8. ``via_device_id`` names it
        # by the registry's own id for it, which is why the panel is
        # registered before any platform is forwarded (see __init__.py): the
        # id does not exist until it is. Both are kept because this runs on
        # whatever Home Assistant the vehicle has, and the older one does not
        # know the new key at all -- it would reject the whole device info and
        # leave the vehicle with no devices rather than with a flat list.
        if _VIA_DEVICE_ID and self.hub_device_id is not None:
            info["via_device_id"] = self.hub_device_id
        else:
            info["via_device"] = (DOMAIN, self.unique_id)
        return info

    def device_is_named(self, addr: int) -> bool:
        """Whether this address's device name is the one it will keep.

        Home Assistant builds an entity id out of the device's name at the
        moment the entity is created and never revises it, so an entity built
        while a device is still "Bus device 0x0201" carries that placeholder
        for good. Measured on the bus of #23: nine of about seventy entities
        came up as ``climate.bus_device_0x0201``,
        ``sensor.bus_device_0x0405_storungscode`` and so on, while their
        siblings on the same devices came up as ``combi_6_e_*`` and
        ``ebl25x_5_*`` -- a race, not a rule, and one the owner could only fix
        by renaming nine entities by hand.

        The race is in the startup order rather than in the bus: subscribing
        (session.run_startup step 2) makes the panel push values, and the
        parameter descriptions that carry Identify.Name are not asked for
        until step 4. So a heater's room temperature routinely arrives a few
        seconds before the heater's name.

        What the caller waits for is therefore the name, not the value:

        * the panel is named after the config entry and never waits,
        * a device that has published Identify.Name or its own label is named,
        * an address in _KNOWN_NAMES is named without publishing anything,
        * and once discovery has finished, every device that was going to
          name itself has -- so the address placeholder is the final answer
          for the rest (0x0601 publishes BleDeviceManagement and no Identify
          at all) rather than a value still in flight.

        Waiting is safe in a way that not waiting is not: the entities appear
        seconds later, while a wrong entity id is permanent.
        """
        if addr == DEV_PANEL or addr in _KNOWN_NAMES or self._bus.discovered:
            return True
        device = self._bus.devices.get(addr)
        return device is not None and (
            device.name is not None or device.label is not None
        )

    @callback
    def async_sync_device_names(self) -> None:
        """Rename registered devices whose identity arrived after they were.

        The gate above closes the window for a device that names itself during
        startup, and this closes it for one that names itself later: a
        battery-powered gas sensor that wakes up minutes in gets its entities
        (and therefore its Home Assistant device) as soon as discovery is
        over, under the address placeholder, and would otherwise keep that
        name until something else caused an entity to be created.

        The same call is what stops a name going backwards. A device is
        registered afresh by every entity built on it, so on the vehicle of
        #23 three named devices fell back to "Bus device 0xNNNN" and lost
        their model with the name -- an entity created early in a session
        re-registered the device under the placeholder, and nothing created
        later put it back.

        ``name_by_user`` is untouched, so a device the owner has renamed keeps
        the owner's name: Home Assistant shows that in preference to ours.
        """
        registry = dr.async_get(self.hass)
        for addr in list(self._bus.devices):
            if not self.device_is_named(addr):
                continue
            entry = registry.async_get_device(
                identifiers={(DOMAIN, f"{self.unique_id}_{addr:04X}")}
            )
            if entry is None:
                continue
            info = self.device_info(addr)
            if entry.name == info.get("name") and entry.model == info.get("model"):
                continue
            registry.async_update_device(
                entry.id,
                name=info.get("name"),
                model=info.get("model"),
            )

    @property
    def poll_interval(self) -> int:
        """Seconds between polls, or 0 to hold the connection open."""
        return int(
            self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )

    async def _finish_startup(self, client: TrumaBleClient) -> bool:
        """Run startup on a connected client, then hold until the link drops.

        Shared by the fresh-connect and adopted-handoff paths. Returns ``True``
        (the connection is up, so the caller resets the backoff).
        """
        await self._run_startup(client)
        if self._manual_hold_request_minutes is not None:
            # Erst jetzt läuft die Live-Zeit an: der Handshake kostet auf dem
            # Fahrzeug rund 25 s, und die gehören nicht dem Nutzer weggerechnet.
            minutes = self._manual_hold_request_minutes
            self._manual_hold_request_minutes = None
            self._manual_wake_pending = False
            self._manual_release_requested = False
            self._manual_hold_until = (
                self.hass.loop.time() + minutes * 60
                if self.poll_interval and minutes
                else 0.0
            )
        # Every device on the bus has now been asked to describe itself, so
        # whatever has not named itself by here is not going to: anything
        # waiting for a device's identity may stop waiting (see
        # device_is_named).
        self._bus.discovered = True
        self._session_ok = True
        self._session_transport = client.transport
        if self._last_kind is not None:
            # The panel answered, encrypted and subscribed on this address, so
            # this is the kind that works here. Connecting alone would not have
            # been proof: a path can establish a link and then fail to encrypt.
            await self._remember_address_kind(self._last_kind)

        # In poll mode this stays True between polls: it means "we are in
        # touch with the panel", not "a link is open this instant". The link
        # coming and going every interval is an implementation detail and
        # should not flap the connectivity sensor or blank every entity.
        self._bus.connected = True
        self._bus.assigned_addr = client.assigned_addr
        self.async_set_updated_data(self._bus)
        self.async_sync_device_names()
        LOGGER.info("Truma %s connected and subscribed", self.unique_id)
        self._connected_event.set()

        # Startup just delivered frames, so seed the watchdog from now.
        self._last_frame = self.hass.loop.time()

        # Auch der Poll-Zweig misst nach, solange ein Live-Fenster läuft.
        next_measure = self.hass.loop.time() + _MEASURE_INTERVAL

        if self.poll_interval:
            # Poll mode: the reading is in hand, so let the link go and free the
            # connection slot. Wait only until the panel stops talking.
            started = self.hass.loop.time()
            # Die Reihenfolge dieser Prüfungen ist bindend, und jede
            # Vertauschung hat ein Gesicht:
            #   Writes/manuelle Anfragen -> Release-Wunsch -> Command-Hold ->
            #   Live-Fenster -> Stille -> Verweilgrenze
            # Der Release vor den Writes legte mitten im Befehl auf; der
            # Command-Hold vor dem Release ließe "Live-Modus beenden" eine
            # Minute lang wirkungslos; das Live-Fenster vor dem Command-Hold
            # ließe den Stall-Watchdog den Nachlauf abreißen, der gerade für
            # ein schweigendes Gerät da ist.
            while not self._stop and client.connected:
                await asyncio.sleep(1)
                if self._writes_pending or self._manual_requests:
                    # Ein Befehl oder ein manueller Lesevorgang besitzt die
                    # Sitzung, bis er fertig ist.
                    started = self.hass.loop.time()
                    continue
                if self._manual_release_requested:
                    self._manual_release_requested = False
                    self._manual_hold_until = 0.0
                    break
                now = self.hass.loop.time()
                if now < self._command_hold_until:
                    # Nachlauf nach einem Befehl: weder Stille noch die
                    # Verweilgrenze beenden den Poll, solange das Fenster läuft.
                    # ``started`` bleibt stehen, damit der Hold den Link nicht
                    # über sein eigenes Ende hinaus offen hält.
                    continue
                if self.manual_session_active:
                    # Live-Fenster: der Link bleibt, auch wenn niemand redet.
                    # Beenden kann ihn nur der Stall-Watchdog -- die Stille
                    # ist hier ja gerade kein Grund aufzulegen.
                    if now >= next_measure:
                        next_measure = now + _MEASURE_INTERVAL
                        await self._request_measurements(client)
                    if now - self._last_frame > _DATA_STALL_TIMEOUT:
                        LOGGER.warning(
                            "Truma %s: no data for %ss during manual live mode; "
                            "reconnecting",
                            self.unique_id,
                            _DATA_STALL_TIMEOUT,
                        )
                        break
                    continue
                quiet = now - self._last_frame
                if quiet >= _POLL_QUIET:
                    break
                if self.hass.loop.time() - started >= _POLL_MAX_DWELL:
                    LOGGER.debug(
                        "Truma %s: still talking after %ss; ending the poll anyway",
                        self.unique_id,
                        _POLL_MAX_DWELL,
                    )
                    break
            LOGGER.debug(
                "Truma %s: poll complete, disconnecting for %ss",
                self.unique_id,
                self.poll_interval,
            )
            return True

        # Connected mode: hold the link, watching for a data stall and keeping
        # the on-demand sensors measuring.
        while not self._stop and client.connected:
            await asyncio.sleep(1)
            now = self.hass.loop.time()
            if now >= next_measure:
                # Schedule from now rather than from the previous slot: a send
                # that blocks on its acknowledgement must not leave a backlog
                # of missed slots to fire back to back.
                next_measure = now + _MEASURE_INTERVAL
                await self._request_measurements(client)
            if self.hass.loop.time() - self._last_frame > _DATA_STALL_TIMEOUT:
                LOGGER.warning(
                    "Truma %s: no data for %ss; link is stale, reconnecting",
                    self.unique_id,
                    _DATA_STALL_TIMEOUT,
                )
                break
        return True

    async def _run_startup(self, client: TrumaBleClient) -> None:
        """Run the session's startup sequence on a connected client.

        The sequence itself lives in ``session.py``, which imports no Home
        Assistant, so that it can also be driven from a terminal against real
        hardware (``tools/dump_bus.py --live``).
        What is left here is the translation into Home Assistant's own
        exception, so that the session loop keeps seeing one kind of failure.
        """
        try:
            await asyncio.wait_for(
                session.run_startup(
                    client,
                    self._bus,
                    self._identity,
                    self.unique_id,
                    self.hass.loop.time,
                ),
                _STARTUP_TIMEOUT,
            )
        except TimeoutError as exc:
            message = (
                f"Truma {self.unique_id}: startup did not finish within "
                f"{_STARTUP_TIMEOUT}s; the session is being dropped and retried"
            )
            LOGGER.warning(message)
            raise HomeAssistantError(message) from exc
        except session.StartupFailed as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _discover_params(self, client: TrumaBleClient) -> None:
        """Jedes Busgerät nach seinen aktuellen Werten fragen; siehe session.py.

        Dasselbe, was der Startup als Schritt 4 tut -- hier für eine bereits
        offene Verbindung, die niemand dafür neu aufbauen soll.
        """
        await session.discover_params(
            client, self._bus, self.unique_id, self.hass.loop.time
        )

    async def _request_measurements(self, client: TrumaBleClient) -> None:
        """Ask the on-demand sensors for a fresh reading; see session.py."""
        await session.request_measurements(client, self._bus, self.unique_id)

    @callback
    def _on_frame(self, parsed: dict) -> None:
        """Handle a decoded V3 frame and update state.

        The decoding itself is in ``session.handle_frame``, which imports no
        Home Assistant, so the same frames can be filed into a bus from a
        terminal. What is left here is the two things only Home Assistant
        cares about: the stall watchdog, and telling the entities.
        """
        # Any frame proves the link is alive; feed the stall watchdog.
        self._last_frame = self.hass.loop.time()
        # ...and it may be the answer a pending write is waiting for.
        self._note_frame_values(parsed)

        if session.handle_frame(self._bus, parsed, self._client, self.unique_id):
            self.async_set_updated_data(self._bus)
            # A frame can carry the Identify.Name of a device that is already
            # registered, which is the one thing the entity-creation gate
            # cannot cover.
            self.async_sync_device_names()

    @callback
    def _note_frame_values(self, parsed: dict) -> None:
        """Jeden Wert eines Frames für eine wartende Schreibbestätigung anbieten.

        Zwei Frameformen tragen Werte, und beide zählen: die unaufgeforderte
        Einzelmeldung (``tn``/``pn``/``v`` direkt im CBOR) und die Antwort auf
        die Parameter-Abfrage, mit der ``_write_confirmed`` ein schlafendes
        Gerät weckt (``topics`` mit verschachtelten ``parameters``). Nur die
        erste zu lesen hieße, genau die Antwort zu verpassen, um die wir eben
        gebeten haben.

        Das Auseinandernehmen der Frames ist sonst ``session.handle_frame``s
        Sache; hier steht es, weil nur der Coordinator weiß, welcher Wert
        gerade erwartet wird -- und weil außerhalb eines Schreibvorgangs
        nichts davon getan wird.
        """
        if not self._write_feedback:
            return
        src = parsed.get("src")
        cbor = parsed.get("cbor")
        if not isinstance(src, int) or not isinstance(cbor, dict):
            return
        topic, param, value = cbor.get("tn"), cbor.get("pn"), cbor.get("v")
        if topic and param and value is not None:
            self.on_frame_value(src, topic, param, value)
        for entry in cbor.get("topics") or []:
            if not isinstance(entry, dict):
                continue
            topic = entry.get("tn", "")
            for item in entry.get("parameters") or []:
                if not isinstance(item, dict):
                    continue
                param, value = item.get("pn"), item.get("v")
                if topic and param and value is not None:
                    self.on_frame_value(src, topic, param, value)

    @callback
    def on_frame_value(self, addr: int, topic: str, param: str, value: int) -> None:
        """Einen eingetroffenen Wert für eine wartende Schreibbestätigung merken.

        Der Eintrag wird beim Prüfen per ``pop`` entfernt, und
        ``_write_confirmed`` räumt ihn vor jedem Anlauf weg: bestätigen darf
        nur eine Meldung, die nach dem Frame eingetroffen ist. Der Bus taugt
        dafür nicht -- der hält auch den Wert von vorher, und ein Befehl, der
        nichts bewirkt, würde sich aus dem Cache selbst bestätigen.

        Jeder laufende Vorgang bekommt denselben Wert in sein eigenes Buch:
        eine Meldung kann die Antwort auf zwei gleichzeitig wartende Befehle
        sein, und keiner von beiden darf sie dem anderen wegnehmen.
        """
        for book in self._write_feedback.values():
            book[(addr, topic, param)] = value

    @callback
    def _mark_disconnected(self) -> None:
        """Flag the link as down and notify entities."""
        if self._bus.connected:
            self._bus.connected = False
            self.async_set_updated_data(self._bus)

    async def _client_for_write(self) -> TrumaBleClient:
        """A connected client to write through, waking a poll if need be.

        In connected mode there is always a live link. In poll mode there
        usually is not: hanging up between readings is the point. Rather than
        refuse the command -- which is what a button press got, "Truma panel is
        not connected" -- ask the loop for a session now and wait for it. The
        poll will not hang up while the write is outstanding.
        """
        client = self._client
        if client is not None and client.connected:
            return client
        if not self.poll_interval:
            raise HomeAssistantError("Truma panel is not connected")

        # Wir haben eben festgestellt, dass kein brauchbarer Client da ist --
        # also darf auch das Event keinen behaupten, sonst kehrt das Warten
        # unten sofort zurück und der Befehl scheitert nach 0 ms. Dieselbe
        # Vorsichtsmaßnahme wie in ``_request_manual_session``, und sie deckt
        # den Fall ab, den ``_disconnect_client`` noch nicht erreicht hat: ein
        # Client, der bereits tot ist, aber noch hängt. Zwischen der Prüfung
        # oben und diesem ``clear`` liegt kein ``await``, der Stand kann uns
        # also nicht unter den Händen veralten.
        self._connected_event.clear()
        LOGGER.debug("Truma %s: write requested; waking a poll", self.unique_id)
        self._wake_event.set()
        try:
            await asyncio.wait_for(
                self._connected_event.wait(), timeout=_WRITE_CONNECT_TIMEOUT
            )
        except TimeoutError:
            raise HomeAssistantError(
                "Truma panel did not answer in time for the command"
            ) from None
        client = self._client
        if client is None or not client.connected:
            raise HomeAssistantError("Truma panel is not connected")
        return client

    async def async_write(
        self, addr: int, topic: str, param: str, value: int
    ) -> None:
        """Einen einzelnen Parameter schreiben und bestätigen lassen."""
        await self.async_write_many([(addr, topic, param, value)])

    async def async_write_many(
        self,
        commands: list[tuple[int, str, str, int]],
        *,
        action: str | None = None,
        target: object = None,
    ) -> None:
        """Mehrere Parameter als eine Nutzeraktion schreiben und bestätigen lassen.

        ``addr`` ist jeweils die Busadresse des Geräts der Entität, und dorthin
        geht der Befehl -- einzige Ausnahme sind die wenigen Topics, die das
        Panel für den Bus weiterreicht (siehe ``COMMAND_DEST``). Einen Befehl
        an ein Gerät zu adressieren, das in der Quelle genannt wird, war die
        Fehlerklasse von #10: ein AirCooling.TgtTemp an die Combi wurde vom
        Transport quittiert und dann still verworfen, weil auf jenem Fahrzeug
        ein Dachgerät kühlt.

        Und genau dort hört ein Transport-ACK auf zu taugen: er sagt, dass das
        Panel den Frame genommen hat, nicht dass danach etwas geschehen ist.
        Gemessen wurde ein quittierter Befehl, den die Heizung während des
        Nachlüftens nicht ausführte. Bestätigt ist ein Schreibvorgang erst,
        wenn das Zielgerät den neuen Wert selbst meldet.

        Alle Befehle werden zuerst geprüft und erst dann gesendet: eine
        Transaktion, die auf halbem Weg an der eigenen Validierung scheitert,
        ließe das Fahrzeug in einem Zustand zurück, den niemand angefordert
        hat.
        """
        for addr, topic, param, value in commands:
            ok, msg = self._bus.validate_write(addr, topic, param, value)
            if not ok:
                raise HomeAssistantError(f"Invalid Truma command: {msg}")

        with self._operations.operation(
            action or self._infer_action(commands), target
        ) as token:
            # Über den ganzen Vorgang gehalten, nicht nur über das Warten auf
            # einen Link: im Poll-Betrieb prüft die Schleife das, bevor sie
            # auflegt, und ein früh freigegebenes Flag ließe sie zwischen
            # Client-Holen und Senden auflegen.
            self._writes_pending += 1
            # Ein Befehl widerruft einen Release-Wunsch: wer gerade bedient,
            # will die Verbindung, auch wenn er eben noch "beenden" gedrückt
            # hat.
            self._manual_release_requested = False
            # Das eigene Buch dieses Vorgangs. Ein zweiter Vorgang, der
            # währenddessen anläuft, legt sein eigenes daneben und lässt
            # dieses unberührt.
            feedback: dict[tuple[int, str, str], int] = {}
            self._write_feedback[token] = feedback
            try:
                client = await self._client_for_write()
                for addr, topic, param, value in commands:
                    dest = self._bus.command_dest(addr, topic)
                    await self._write_confirmed(
                        client, dest, topic, param, value, feedback
                    )
                if len(commands) > 1:
                    # Jeder einzelne Befehl wurde bestätigt -- was nicht heißt,
                    # dass am Ende alle zugleich gelten. Eine Heizung kann eine
                    # frühere Einstellung zurücknehmen, während die nächste
                    # ankommt (Gas und Strom schließen sich je nach Modus aus).
                    await asyncio.sleep(_WRITE_SETTLE)
                    for addr, topic, param, value in commands:
                        dest = self._bus.command_dest(addr, topic)
                        got = self._bus.device(dest).get(topic, param)
                        if not isinstance(got, int) or not self._feedback_satisfied(
                            topic, param, value, got
                        ):
                            raise HomeAssistantError(
                                f"Truma did not retain the requested setting "
                                f"{topic}.{param}={value}"
                            )
            finally:
                self._write_feedback.pop(token, None)
                self._writes_pending -= 1
                # Auch nach einem Fehlschlag: der Nutzer soll sofort
                # nachsteuern können, ohne auf den nächsten Poll zu warten.
                self._hold_after_command()

    async def _write_confirmed(
        self,
        client: TrumaBleClient,
        dest: int,
        topic: str,
        param: str,
        value: int,
        feedback: dict[tuple[int, str, str], int],
    ) -> None:
        """Einen Parameter schreiben und auf die Bestätigung des Geräts warten.

        ``feedback`` ist das Buch des eigenen Vorgangs und wird durchgereicht
        statt über ``self`` geholt: ein gleichzeitiger zweiter Schreibvorgang
        soll hier nichts anfassen können.
        """
        for attempt in range(_WRITE_ATTEMPTS):
            # Alles, was vor diesem Anlauf gemeldet wurde, zählt nicht: es kann
            # den Stand von vor dem Befehl tragen, und eine Meldung, die zufällig
            # schon den Wunschwert trug, würde den Befehl bestätigen, ohne dass
            # er je ausgeführt wurde.
            feedback.pop((dest, topic, param), None)
            frame = build_write_frame(client.assigned_addr, dest, topic, param, value)
            LOGGER.debug("Truma write %s.%s = %s -> 0x%04X", topic, param, value, dest)
            if not await client.send(frame):
                raise HomeAssistantError(
                    f"Truma did not acknowledge write {topic}.{param}={value}"
                )
            # Ein schlafender oder gerade aufwachender Brenner schickt den
            # geänderten Wert nicht von selbst -- also danach fragen.
            await self._request_param_discovery(client, dest)
            if await self._await_feedback(dest, topic, param, value, feedback):
                return
            if not client.connected or attempt == _WRITE_ATTEMPTS - 1:
                break
            await asyncio.sleep(_WRITE_RETRY_PAUSE)
        raise HomeAssistantError(f"Truma did not confirm {topic}.{param}={value}")

    async def _await_feedback(
        self,
        dest: int,
        topic: str,
        param: str,
        value: int,
        feedback: dict[tuple[int, str, str], int],
    ) -> bool:
        """Auf eine frische Meldung des Zielgeräts warten."""
        deadline = self.hass.loop.time() + _WRITE_FEEDBACK_TIMEOUT
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(0.2)
            got = feedback.pop((dest, topic, param), None)
            if got is None:
                continue
            if self._feedback_satisfied(topic, param, value, got):
                return True
        return False

    @staticmethod
    def _feedback_satisfied(topic: str, param: str, wanted: int, got: int) -> bool:
        """Ob die Rückmeldung den gewünschten Wert bestätigt.

        Überall exakt -- mit genau einer Ausnahme: ``WaterHeating.Active``
        meldet 1 (heizt) oder 2 (ein, Solltemperatur erreicht), und beides
        heißt "ein". Die Temperaturstufe ``WaterHeating.Mode`` und jedes
        andere ``Active`` sind davon nicht berührt.
        """
        if (topic, param) == ("WaterHeating", "Active") and wanted == 1:
            return got in _ENABLED_STATES
        return got == wanted

    async def _request_param_discovery(
        self, client: TrumaBleClient, dest: int
    ) -> None:
        """Ein Gerät bitten, seine Parameter erneut zu melden.

        Als Sonde gesendet: ein schlafendes Gerät darf schweigen, ohne dass
        der Transport die Sitzung für mehrdeutig erklärt -- das Ausbleiben der
        Antwort behandelt der Anlauf selbst.
        """
        frame = build_v3_frame(
            dest, client.assigned_addr, CTRL_MBP, MBP_PARAM_DISC, 0, b""
        )
        await client.send(frame, probe=True)

    @staticmethod
    def _infer_action(commands: list[tuple[int, str, str, int]]) -> str:
        """Aus dem letzten Befehl einen Namen für die Anzeige ableiten."""
        _addr, topic, param, _value = commands[-1]
        return {
            ("RoomClimate", "Mode"): "hvac_mode",
            ("AirHeating", "TgtTemp"): "temperature",
            ("AirCirculation", "FanLevel"): "fan_level",
            ("EnergySrc", "ElectricLevel"): "electric_heating",
            ("WaterHeating", "Active"): "water_mode",
            ("WaterHeating", "Mode"): "water_mode",
            ("WaterHeating", "FasterHeatingMode"): "water_priority",
        }.get((topic, param), f"{topic}.{param}")
