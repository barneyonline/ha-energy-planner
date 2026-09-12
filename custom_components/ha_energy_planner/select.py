"""Shared charger vehicle selection."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EnergyPlannerEntity
from .entry_data import combined_entry_data
from .type_defs import EnergyPlannerConfigEntry
from .vehicles import AUTO, MANUAL, VEHICLES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose manual identity selection only for configured vehicle profiles."""
    if VEHICLES in combined_entry_data(entry):
        async_add_entities([VehicleSelect(entry.runtime_data, "ev_vehicle")])


PARALLEL_UPDATES = 0


class VehicleSelect(EnergyPlannerEntity, SelectEntity):
    """Select Auto, a tracked vehicle, or unmanaged guest charging."""

    _attr_translation_key = "ev_vehicle"

    @property
    def options(self) -> list[str]:
        return [AUTO, *(p["name"] for p in combined_entry_data(self.coordinator.entry)[VEHICLES]), MANUAL]

    @property
    def current_option(self) -> str:
        session = self.coordinator.vehicle_session
        return next(
            (p["name"] for p in combined_entry_data(self.coordinator.entry)[VEHICLES] if p["id"] == session.selection),
            session.selection,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self.coordinator.vehicle_session
        return {
            "active_vehicle": session.profile["name"] if session.profile else None,
            "detection_reason": session.reason,
            "vehicle_inputs_ready": session.allowed,
        }

    async def async_select_option(self, option: str) -> None:
        selection = next(
            (p["id"] for p in combined_entry_data(self.coordinator.entry)[VEHICLES] if p["name"] == option), option
        )
        await self.coordinator.async_select_vehicle(selection)
