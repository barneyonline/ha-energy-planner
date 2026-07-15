"""Number controls for Energy Planner."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_EV_FALLBACK_TARGET_SOC_PERCENT, CONF_EV_MAX_SOC_PERCENT, CONF_EV_MIN_SOC_PERCENT
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .type_defs import EnergyPlannerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up native numeric controls."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(entry, async_add_entities, [EVTargetSOCNumber(coordinator)])


class EVTargetSOCNumber(EnergyPlannerEntity, NumberEntity):
    """Native target SOC control for smart charging."""

    _attr_translation_key = "ev_target_soc"
    _attr_icon = "mdi:battery-charging-80"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        """Initialize the target SOC control."""
        super().__init__(coordinator, "ev_target_soc")

    @property
    def native_value(self) -> float:
        """Return the configured target SOC."""
        return float(self.coordinator.options[CONF_EV_FALLBACK_TARGET_SOC_PERCENT])

    @property
    def native_min_value(self) -> float:
        """Return the configured safe minimum."""
        return float(self.coordinator.options[CONF_EV_MIN_SOC_PERCENT])

    @property
    def native_max_value(self) -> float:
        """Return the configured safe maximum."""
        return float(self.coordinator.options[CONF_EV_MAX_SOC_PERCENT])

    async def async_set_native_value(self, value: float) -> None:
        """Set target SOC and request a fresh plan."""
        await self.coordinator.async_set_ev_target_soc(value)
        self.async_write_ha_state()
