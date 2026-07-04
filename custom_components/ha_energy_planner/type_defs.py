"""Type aliases for Energy Planner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import EnergyPlannerCoordinator

    type EnergyPlannerConfigEntry = ConfigEntry[EnergyPlannerCoordinator]
else:
    type EnergyPlannerConfigEntry = Any
