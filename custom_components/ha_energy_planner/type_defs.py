"""Type aliases for Energy Planner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import EnergyPlannerCoordinator

    EnergyPlannerConfigEntry: TypeAlias = ConfigEntry[EnergyPlannerCoordinator]
else:
    EnergyPlannerConfigEntry: TypeAlias = Any
