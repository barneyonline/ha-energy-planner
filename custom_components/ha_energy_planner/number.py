"""Number controls for Energy Planner."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_EV_LOW_PRICE_THRESHOLD,
    DOMAIN,
)
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
    _remove_retired_numbers(hass, entry)
    async_add_planner_entities(
        entry,
        async_add_entities,
        [
            EVOpportunisticChargingPriceThresholdNumber(coordinator),
        ],
    )


def _remove_retired_numbers(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove the retired Target SOC entity from the entity registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_ev_target_soc")
    if entity_id is not None:
        registry.async_remove(entity_id)


class EVOpportunisticChargingPriceThresholdNumber(EnergyPlannerEntity, NumberEntity):
    """Native import-price threshold control for opportunistic charging."""

    _attr_translation_key = "ev_opportunistic_charging_price_threshold"
    _attr_icon = "mdi:cash"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = -10.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.01

    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        """Initialize the opportunistic charging price threshold control."""
        super().__init__(coordinator, "ev_opportunistic_charging_price_threshold")
        currency = getattr(getattr(coordinator, "hass", None), "config", None)
        currency_code = getattr(currency, "currency", None)
        if currency_code:
            self._attr_native_unit_of_measurement = f"{currency_code}/kWh"

    @property
    def native_value(self) -> float:
        """Return the configured import-price threshold."""
        return float(self.coordinator.options[CONF_EV_LOW_PRICE_THRESHOLD])

    async def async_set_native_value(self, value: float) -> None:
        """Persist the import-price threshold and request a fresh plan."""
        await self.coordinator.async_set_ev_low_price_threshold(value)
        self.async_write_ha_state()
