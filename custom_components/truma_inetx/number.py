"""Number platform: sliders for bus parameters, plus the live-mode duration.

Die Dauer ist der eine Eintrag hier, der kein Busparameter ist: sie sagt,
wie lange der Link nach einer angeforderten Synchronisation offen bleibt,
und wird rein lokal gehalten.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import Platform, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, TrumaParamEntity, async_add_rows
from .profiles import Row
from .truma.const import DEV_PANEL

# Diese Entitäten werden vom Coordinator versorgt und haben kein *synchrones*
# ``update()``; Home Assistant legt darum ohnehin kein Semaphor an -- es fragt
# in ``EntityPlatform._async_add_entity`` nach ``hasattr(entity, "update")``.
# Dass ``CoordinatorEntity`` ein ``async_update`` mitbringt, ändert daran
# nichts: danach wird nicht gefragt. Die 0 ist also gleichbedeutend mit dem
# Weglassen, steht aber ausdrücklich da -- und unter Aufsicht, denn jeder Wert
# darüber wäre sehr wohl Verhalten (``tests/test_parallel_updates.py``).
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Truma numbers."""
    coordinator = entry.runtime_data
    # Hängt an keinem Busparameter: die Dauer ist eine lokale Einstellung für
    # den Link selbst und entsteht deshalb unbedingt.
    async_add_entities([TrumaManualLiveMinutes(coordinator)])
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.NUMBER,
        lambda addr, topic, param, row: TrumaNumber(
            coordinator, addr, topic, param, row
        ),
    )


class TrumaNumber(TrumaParamEntity, NumberEntity):
    """One bus parameter, written within its own device's range."""

    _attr_mode = NumberMode.SLIDER

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
        self._attr_native_unit_of_measurement = row.unit
        self._attr_native_step = row.step

    def _bounds(self) -> tuple[float, float]:
        """The device's own range, or the row's fallback.

        The circulation fan used to be 0-10 for every device that published
        it, which is a Combi's range handed to a roof air conditioner as well.
        The device that owns the parameter describes its own limits, and where
        it has, they win.
        """
        described = self.device.bounds(self._topic, self._param)
        if described is None:
            return self.row.fallback_bounds or (0, 100)
        limit = self.row.bounds_limit
        if limit is None:
            return described
        # A description that is the width of the field rather than a range of
        # values -- the panel's display timeout is 0 to 4294967295 seconds --
        # is clipped into what the row says is worth offering. It still only
        # ever narrows: a device that describes less than the limit keeps its
        # own answer.
        return (max(described[0], limit[0]), min(described[1], limit[1]))

    @property
    def native_min_value(self) -> float:
        """Lowest value this device accepts."""
        return self._bounds()[0]

    @property
    def native_max_value(self) -> float:
        """Highest value this device accepts."""
        return self._bounds()[1]

    @property
    def native_value(self) -> float | None:
        """The device's current value."""
        value = self.value
        if not isinstance(value, (int, float)):
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the device that owns the parameter."""
        await self.async_write(self._param, int(value))


class TrumaManualLiveMinutes(TrumaEntity, RestoreNumber):
    """Vom Nutzer gewählte Dauer für eine sofortige BLE-Sitzung."""

    _attr_translation_key = "manual_live_minutes"
    _attr_native_min_value = 0
    _attr_native_max_value = 999
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    # Eine Box, kein Schieber: 999 Minuten auf einem Schieber trifft niemand.
    _attr_mode = NumberMode.BOX
    # Nicht am Link hängen: die Dauer stellt man ein, *bevor* die Verbindung
    # steht, und sie fasst BLE selbst nie an.
    _gate_on_connected = False

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Mit der sicheren Ein-Refresh-Voreinstellung initialisieren."""
        super().__init__(coordinator, DEV_PANEL, "manual_live_minutes")
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """Die zuletzt gewählte Dauer nach einem Neustart wiederherstellen."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_number_data()
        if restored is not None:
            try:
                await self.async_set_native_value(restored.native_value)
            except (TypeError, ValueError):
                # Ein Stand, den diese Version nicht mehr annimmt, darf den
                # Start nicht kosten -- zurück auf das sichere einmalige Lesen.
                self._attr_native_value = 0
                self.coordinator.manual_live_minutes = 0
        else:
            self._attr_native_value = 0
            self.coordinator.manual_live_minutes = 0

    async def async_set_native_value(self, value: float) -> None:
        """Ganze Minuten lokal merken; BLE wird dabei nicht angefasst."""
        # ``True`` ist in Python eine 1 und käme sonst als eine Minute durch.
        if isinstance(value, bool):
            raise ValueError("Live mode duration must be a whole number")
        numeric = float(value)
        if not numeric.is_integer() or not 0 <= numeric <= 999:
            raise ValueError(
                "Live mode duration must be a whole number from 0 to 999 minutes"
            )
        minutes = int(numeric)
        # Erst prüfen, dann merken: ein abgewiesener Wert lässt den zuletzt
        # gültigen stehen.
        self._attr_native_value = minutes
        self.coordinator.manual_live_minutes = minutes
        self.async_write_ha_state()
