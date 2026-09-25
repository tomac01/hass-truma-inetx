"""Binary sensor platform: bus flags, plus the BLE link itself."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, TrumaParamEntity, async_add_rows
from .profiles import Row
from .truma.const import DEV_PANEL

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma binary sensors."""
    coordinator = entry.runtime_data
    # Neither is a bus parameter and neither can wait for one: they are what
    # says whether anything on the bus can be heard at all, so they exist from
    # setup and report the panel, which is the thing we are connected to. Two
    # questions, two sensors -- the open BLE session to the panel, and the
    # Bluetooth proxy that carries it.
    async_add_entities([TrumaConnectionSensor(coordinator), TrumaProxySensor(coordinator)])
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.BINARY_SENSOR,
        lambda addr, topic, param, row: TrumaBinarySensor(
            coordinator, addr, topic, param, row
        ),
    )


class TrumaBinarySensor(TrumaParamEntity, BinarySensorEntity):
    """One bus parameter, read as on or off."""

    def __init__(
        self,
        coordinator: TrumaCoordinator,
        addr: int,
        topic: str,
        param: str,
        row: Row,
    ) -> None:
        """Initialize from the row."""
        super().__init__(coordinator, addr, topic, param, row)
        self._attr_device_class = row.device_class

    @property
    def is_on(self) -> bool | None:
        """Whether the parameter reads as on, by the row's own definition.

        A row that names the values that count as on gets exactly those; the
        rest are plain flags where anything non-zero is on. The difference is
        the tri-state Active family, where 2 is the appliance standing by --
        see the FlameStatus row in profiles.py.

        A row that reduces its wire value reduces it first, the same way a
        sensor does: an error list is not a flag until something has counted
        it.
        """
        value = self.value
        if self.row.reduce is not None:
            value = self.row.reduce(value)
        if not isinstance(value, (int, float)):
            return None
        if self.row.on_values is None:
            return bool(value)
        return int(value) in self.row.on_values


class TrumaConnectionSensor(TrumaEntity, BinarySensorEntity):
    """BLE link status to the panel."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = None
    _gate_on_connected = False  # reports the link itself → never gated

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, DEV_PANEL, "connection")

    @property
    def is_on(self) -> bool:
        """Ob gerade eine BLE-Sitzung zum Panel offen ist."""
        return self.coordinator.panel_link_connected


class TrumaProxySensor(TrumaEntity, BinarySensorEntity):
    """Verfügbarkeit des für dieses Panel benutzten ESPHome-Proxys."""

    _attr_translation_key = "proxy_connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = None
    _gate_on_connected = False

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialisieren."""
        super().__init__(coordinator, DEV_PANEL, "proxy_connection")

    @property
    def is_on(self) -> bool | None:
        """Ob der identifizierte Bluetooth-Proxy in HA registriert ist."""
        return self.coordinator.proxy_available
