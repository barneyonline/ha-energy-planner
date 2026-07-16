"""Time controls for Energy Planner."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEFAULT_READY_BY
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .type_defs import EnergyPlannerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up native time controls."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(entry, async_add_entities, [EVReadyByTime(coordinator)])


class EVReadyByTime(EnergyPlannerEntity, TimeEntity):
    """Native ready-by control for smart charging."""

    _attr_translation_key = "ev_ready_by"
    _attr_icon = "mdi:clock-check-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        """Initialize the ready-by control."""
        super().__init__(coordinator, "ev_ready_by")

    @property
    def native_value(self) -> time:
        """Return the configured local ready-by time."""
        hour, minute = str(self.coordinator.planner_options[CONF_DEFAULT_READY_BY]).split(":", 1)
        return time(hour=int(hour), minute=int(minute[:2]))

    async def async_set_value(self, value: time) -> None:
        """Set ready-by and request a fresh plan."""
        await self.coordinator.async_set_ready_by(value.strftime("%H:%M"))
        self.async_write_ha_state()
