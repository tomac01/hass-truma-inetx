"""Select platform: every bus parameter the table presents as a choice."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import (
    TrumaEntity,
    TrumaParamEntity,
    async_add_rows,
    async_add_when_all_reported,
)
from .profiles import Row

# Diese Entitäten werden vom Coordinator versorgt und haben kein *synchrones*
# ``update()``; Home Assistant legt darum ohnehin kein Semaphor an -- es fragt
# in ``EntityPlatform._async_add_entity`` nach ``hasattr(entity, "update")``.
# Dass ``CoordinatorEntity`` ein ``async_update`` mitbringt, ändert daran
# nichts: danach wird nicht gefragt. Die 0 ist also gleichbedeutend mit dem
# Weglassen, steht aber ausdrücklich da -- und unter Aufsicht, denn jeder Wert
# darüber wäre sehr wohl Verhalten (``tests/test_parallel_updates.py``).
PARALLEL_UPDATES = 0

OFF = "off"

ENERGY_DIESEL = "diesel"
ENERGY_ELECTRIC = "electric"
ENERGY_HYBRID = "hybrid"
ENERGY_OPTIONS = [ENERGY_DIESEL, ENERGY_ELECTRIC, ENERGY_HYBRID]
ENERGY_CHANGING = "changing"

# Eintritt in Elektro oder Hybrid immer bei 900 W: 1800 W wirft an schwachen
# Landanschlüssen den Automaten. Die höhere Stufe bleibt ein bewusster
# zweiter Schritt über die Leistungsauswahl.
_ENTER_ELECTRIC = 1

# Beide Pegel müssen gemeldet sein, sonst gibt es die Auswahl nicht: eine
# Combi D ohne Elektroelement beschreibt EnergySrc.ElectricLevel gar nicht.
_REQUIRED = {("EnergySrc", "DieselLevel"), ("EnergySrc", "ElectricLevel")}

# Was jede Auswahl in die beiden Hardwarepegel übersetzt.
_ENERGY_WRITES = {
    ENERGY_DIESEL: (1, 0),
    ENERGY_ELECTRIC: (0, _ENTER_ELECTRIC),
    ENERGY_HYBRID: (1, _ENTER_ELECTRIC),
}


def _ordered_levels(diesel: int, electric: int) -> list[tuple[str, int]]:
    """Die beiden Pegelbefehle so ordnen, dass nie beide Quellen aus sind.

    Eine Transaktion heißt nicht, dass beide Pegel gleichzeitig ankommen: die
    Befehle gehen nacheinander raus, jeder wird einzeln bestätigt, und
    dazwischen liegen bis zu 40 Sekunden. Scheitert der zweite, bleibt stehen,
    was der erste angerichtet hat.

    Käme das Abschalten zuerst, wäre das übrig bleibende genau der Zustand,
    den diese Auswahl verhindern soll -- eine Heizung ohne Energiequelle, ohne
    dass irgendwo ein Fehler erschiene. Und er ist nicht unwahrscheinlich: der
    eben abgeschaltete Brenner geht ins Nachlüften, und für diese Phase ist in
    ``coordinator.async_write_many`` dokumentiert, dass das Panel Frames
    quittiert, die die Heizung nicht ausführt.

    Also erst einschalten, dann abschalten. Keine der drei Auswahlen schaltet
    beide Pegel ab, deshalb ist nach dem ersten Befehl immer eine Quelle an --
    aus jedem Ausgangszustand, auch aus "beide aus", das beim Einschalten der
    Heizung vorkommt. Der Preis ist ein kurzer Hybridbetrieb, wenn die alte
    Quelle noch läuft: ein Zustand, den die Anlage ohnehin kennt, und der
    schlechtere von zwei Zwischenständen ist er nicht.

    ``sorted`` ist stabil, Diesel bleibt also vor Elektro, wo beide einschalten.
    """
    levels = [("DieselLevel", diesel), ("ElectricLevel", electric)]
    return sorted(levels, key=lambda level: level[1] == 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma select entities.

    Each appears on the device that publishes its parameter. The supplemental
    electric element is an option, not standard: a Combi D has none and its
    panel does not describe EnergySrc.ElectricLevel at all, which is how that
    select came to be offering off / 900 W / 1800 W against hardware that can
    do none of them.
    """
    coordinator = entry.runtime_data
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.SELECT,
        lambda addr, topic, param, row: TrumaSelect(
            coordinator, addr, topic, param, row
        ),
    )
    # ``None`` statt einer Adresse: welches Gerät die Energiepegel führt, ist
    # nicht vorab bekannt, und eine Adresstabelle wäre nach dem nächsten
    # Neupaaren falsch (siehe ``bus.COMMAND_DEST``). Der Preis dafür steht im
    # Docstring von ``async_add_when_all_reported``: genau eine Auswahl für den
    # ganzen Bus, am ersten Gerät, das beide Pegel gemeldet hat. Belegt ist nur
    # ein Publisher -- ``EnergySrc`` kommt in ``dumps/combi4-inetx-pro/``
    # allein von 0x0201 -- und ans Panel relayed wird nur ``RoomClimate``.
    async_add_when_all_reported(
        coordinator,
        async_add_entities,
        None,
        _REQUIRED,
        lambda addr: TrumaEnergySourceSelect(coordinator, addr),
    )


class TrumaSelect(TrumaParamEntity, SelectEntity):
    """One bus parameter, chosen from the values its device offers."""

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
        self._labels: dict[int, str] = dict(row.labels or {})
        # Every option resolves through here, to the parameter and the value
        # that selects it. The row's own labels are laid down first, so a
        # device that enumerates its own off keeps it: EnergySrc.ElectricLevel
        # names 0 "Electric off" on a gas/electric Combi, and writing that 0
        # is what switches the element off (#28).
        self._writes: dict[str, tuple[str, int]] = {
            label: (param, value) for value, label in self._labels.items()
        }
        # An off the parameter itself has no value for. Water heating is
        # switched off by WaterHeating.Active while WaterHeating.Mode
        # enumerates the three temperature steps and nothing else, so that
        # option is ours to invent -- and it goes in beside the rest rather
        # than being recognised by its spelling on the way back in. Home
        # Assistant hands a select one flat list of strings and hands the same
        # strings back, so an invented option and a label are the same kind of
        # thing by the time it returns; a sentinel that outranked the labels
        # is how off became unselectable on the one vehicle whose own enum
        # offered it (#28).
        self._off: str | None = None
        if row.off_param is not None and OFF not in self._writes:
            self._off = OFF
            self._writes[OFF] = (row.off_param, 0)

    @property
    def options(self) -> list[str]:
        """The steps this device offers, in value order, plus any invented off.

        The device enumerates the parameter for the vehicle it is installed
        in, so it is the authority on which steps exist. Its *names* for them
        are not used: they arrive in the panel's display language, and the
        same three water steps come back as 40 / 60 / 70 on one vehicle and as
        Eco / Comfort / Hot on another (#12). Taking them as they come would
        make the option strings -- which automations match on -- differ per
        vehicle and per panel language.

        A device that describes nothing gets the whole table, which is what
        every vehicle was offered before the panel was asked.
        """
        values = self.device.allowed_values(self._topic, self._param)
        if values is None:
            values = list(self._labels)
        offered = [self._labels[value] for value in values if value in self._labels]
        if self._off is None:
            return offered
        return [self._off, *offered]

    @property
    def current_option(self) -> str | None:
        """The step currently selected, or off."""
        if self._off is not None:
            off_param, _ = self._writes[self._off]
            if self.device.get(self._topic, off_param) == 0:
                return self._off
        value = self.value
        if not isinstance(value, int):
            return None
        return self._labels.get(value)

    async def async_select_option(self, option: str) -> None:
        """Select a step, switching the function on first where it has an off."""
        param, value = self._writes[option]
        off_param = self.row.off_param
        if off_param is not None and param == self._param:
            # A step is being chosen, and this function is switched off in its
            # own right: switch it on before saying which step. Selecting the
            # invented off writes off_param itself, and nothing else.
            await self.async_write(off_param, 1)
        await self.async_write(param, value)


class TrumaEnergySourceSelect(TrumaEntity, SelectEntity):
    """Diesel, Elektro oder beides — als eine Entscheidung.

    Diesel-Schalter und Elektro-Auswahl sind getrennt bedienbar, und damit
    lässt sich versehentlich "beide aus" einstellen: die Heizung hat dann
    keine Energiequelle und tut nichts, ohne dass irgendwo ein Fehler
    erschiene. Drei Zustände können das nicht ausdrücken.

    Der Upstream-Schalter ``switch.diesel`` bleibt daneben bestehen -- er ist
    die einzige Möglichkeit, den Brenner einzeln zu schalten.
    """

    _attr_translation_key = "energy_source"

    def __init__(self, coordinator: TrumaCoordinator, addr: int) -> None:
        """Initialisieren."""
        super().__init__(coordinator, addr, "energy_source")

    @property
    def _changing(self) -> bool:
        return getattr(self.coordinator, "energy_source_changing", False)

    @property
    def options(self) -> list[str]:
        """Während der Umstellung ist der Zwischenzustand ein eigener Eintrag.

        Home Assistant weist jeden Zustand ab, der nicht in dieser Liste
        steht; "changing" muss also wirklich darin auftauchen, solange es
        gemeldet wird -- und danach wieder verschwinden, damit niemand es
        auswählen kann.
        """
        return [*ENERGY_OPTIONS, ENERGY_CHANGING] if self._changing else ENERGY_OPTIONS

    @property
    def current_option(self) -> str | None:
        """Die Quelle aus beiden Hardwarepegeln ableiten."""
        if self._changing:
            return ENERGY_CHANGING
        diesel = self.device.get("EnergySrc", "DieselLevel")
        electric = self.device.get("EnergySrc", "ElectricLevel")
        if not isinstance(diesel, int) or not isinstance(electric, int):
            return None
        if diesel and electric:
            return ENERGY_HYBRID
        if diesel:
            return ENERGY_DIESEL
        if electric:
            return ENERGY_ELECTRIC
        # Beide aus: genau der Zustand, den diese Auswahl nicht herstellen
        # kann, aber vorfinden darf -- etwa wenn die Heizung ganz aus ist.
        return None

    async def async_select_option(self, option: str) -> None:
        """Beide Pegel als eine Transaktion schreiben, Einschalten zuerst.

        Eine Transaktion und nicht zwei getrennte Befehle: nur so steht am
        Ende entweder der angeforderte Zustand oder ein Fehler. Innerhalb der
        Transaktion legt ``_ordered_levels`` die Reihenfolge fest -- dort steht
        auch, warum sie vom Ziel abhängt.
        """
        if option not in _ENERGY_WRITES:
            raise HomeAssistantError(f"Unknown energy source {option}")
        diesel, electric = _ENERGY_WRITES[option]
        await self.coordinator.async_write_many(
            [
                (self._addr, "EnergySrc", param, value)
                for param, value in _ordered_levels(diesel, electric)
            ],
            action="energy_source",
            target=option,
        )
