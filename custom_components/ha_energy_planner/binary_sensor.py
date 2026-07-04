"""Binary sensor platform for Energy Planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .models import InputHealth
from .type_defs import EnergyPlannerConfigEntry


@dataclass(frozen=True, kw_only=True)
class PlannerBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor description."""

    value_fn: Callable[[EnergyPlannerCoordinator], bool]


BINARY_SENSORS: tuple[PlannerBinarySensorDescription, ...] = (
    PlannerBinarySensorDescription(
        key="data_healthy",
        translation_key="data_healthy",
        icon="mdi:database-check-outline",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: not bool(coordinator.data and coordinator.data.health == InputHealth.HEALTHY),
    ),
    PlannerBinarySensorDescription(
        key="takeover_active",
        translation_key="takeover_active",
        icon="mdi:hand-back-right-outline",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _planner_ownership_active(coordinator.store.data),
    ),
)


def _planner_ownership_active(store_data: dict[str, Any]) -> bool:
    """Return whether persisted ownership means the planner owns a control."""
    ownership = dict(store_data.get("ownership", {}))
    if dict(ownership.get("ev_smart_charging_state", {})):
        return True
    if dict(ownership.get("climate_automations", {})):
        return True
    return any(
        key in ownership
        for key in (
            "enphase_profile",
            "enphase_profile_changed_at",
            "planner_hvac_action_expires_at",
            "planner_takeover_started_at",
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(
        entry,
        async_add_entities,
        (PlannerBinarySensor(coordinator, description) for description in BINARY_SENSORS),
    )


class PlannerBinarySensor(EnergyPlannerEntity, BinarySensorEntity):
    """Planner binary sensor."""

    entity_description: PlannerBinarySensorDescription

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        description: PlannerBinarySensorDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return binary sensor state."""
        return self.entity_description.value_fn(self.coordinator)
