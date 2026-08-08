"""Base entities for Energy Planner."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_NAME
from .coordinator import EnergyPlannerCoordinator
from .type_defs import EnergyPlannerConfigEntry


def planner_device_identifier(entry_id: str) -> tuple[str, str]:
    """Return the single device-registry identifier for a planner entry."""
    return DOMAIN, entry_id


def async_add_planner_entities(
    entry: EnergyPlannerConfigEntry,
    async_add_entities: Any,
    entities: Iterable[Any],
) -> None:
    """Add all planner entities to the main config entry."""
    del entry
    async_add_entities(list(entities))


class EnergyPlannerEntity(CoordinatorEntity[EnergyPlannerCoordinator]):
    """Base coordinator entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        key: str,
        device_key: str | None = None,
    ) -> None:
        """Initialize entity on the single Energy Planner device."""
        super().__init__(coordinator)
        del device_key
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={planner_device_identifier(entry.entry_id)},
            manufacturer=INTEGRATION_NAME,
            model=INTEGRATION_NAME,
            name=str(getattr(entry, "title", "") or INTEGRATION_NAME),
        )
