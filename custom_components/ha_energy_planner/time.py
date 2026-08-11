"""Retired time controls for Energy Planner."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .type_defs import EnergyPlannerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Remove the ready-by control that moved into EV settings."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("time", DOMAIN, f"{entry.entry_id}_ev_ready_by")
    if entity_id is not None:
        registry.async_remove(entity_id)
