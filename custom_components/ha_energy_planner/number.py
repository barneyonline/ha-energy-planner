"""Retired number controls for Energy Planner."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .type_defs import EnergyPlannerConfigEntry

_RETIRED_NUMBER_KEYS = (
    "ev_target_soc",
    "ev_opportunistic_charging_price_threshold",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Remove numeric controls that moved into EV settings."""
    _remove_retired_numbers(hass, entry)


def _remove_retired_numbers(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove retired EV numbers from the entity registry."""
    registry = er.async_get(hass)
    for key in _RETIRED_NUMBER_KEYS:
        entity_id = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id is not None:
            registry.async_remove(entity_id)
