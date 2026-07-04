"""Base entities for Energy Planner."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_NAME
from .coordinator import EnergyPlannerCoordinator
from .type_defs import EnergyPlannerConfigEntry

DEVICE_SYSTEM = "system"
DEVICE_ENERGY = "energy"
DEVICE_CLIMATE = "climate"
DEVICE_PRESENCE = "presence"
DEVICE_ENPHASE = "enphase"
DEVICE_AI = "ai"
DEVICE_EV = "ev"

DEVICE_NAMES = {
    DEVICE_SYSTEM: "System",
    DEVICE_ENERGY: "Energy",
    DEVICE_CLIMATE: "Climate",
    DEVICE_PRESENCE: "Presence",
    DEVICE_ENPHASE: "Enphase",
    DEVICE_AI: "AI",
    DEVICE_EV: "EV",
}

DEVICE_MODELS = {
    DEVICE_SYSTEM: "System",
    DEVICE_ENERGY: "Energy",
    DEVICE_CLIMATE: "Climate",
    DEVICE_PRESENCE: "Presence",
    DEVICE_ENPHASE: "Enphase",
    DEVICE_AI: "AI",
    DEVICE_EV: "EV",
}

ENTITY_DEVICE_BY_KEY = {
    "ai_enabled": DEVICE_AI,
    "ai_advice": DEVICE_AI,
    "climate_control_enabled": DEVICE_CLIMATE,
    "climate_current_state": DEVICE_CLIMATE,
    "climate_next_state": DEVICE_CLIMATE,
    "climate_plan": DEVICE_CLIMATE,
    "enphase_control_enabled": DEVICE_ENPHASE,
    "presence_state": DEVICE_PRESENCE,
    "enphase_current_state": DEVICE_ENPHASE,
    "enphase_next_state": DEVICE_ENPHASE,
    "enphase_plan": DEVICE_ENPHASE,
    "estimated_daily_cost": DEVICE_ENERGY,
    "ev_control_enabled": DEVICE_EV,
    "ev_current_charge_state": DEVICE_EV,
    "ev_current_state": DEVICE_EV,
    "ev_charging_plan": DEVICE_EV,
    "ev_next_charge_state": DEVICE_EV,
    "ev_next_state": DEVICE_EV,
    "forecast_confidence": DEVICE_ENERGY,
}

OPTIONAL_DEVICE_KEYS = {
    DEVICE_AI,
    DEVICE_CLIMATE,
    DEVICE_ENERGY,
    DEVICE_ENPHASE,
    DEVICE_EV,
    DEVICE_PRESENCE,
}


def planner_device_key_for_entity(entity_key: str) -> str:
    """Return the planner device group for an integration-created entity."""
    return ENTITY_DEVICE_BY_KEY.get(entity_key, DEVICE_SYSTEM)


def planner_device_identifier(entry_id: str, device_key: str) -> tuple[str, str]:
    """Return the device-registry identifier for a planner device group."""
    return DOMAIN, f"{entry_id}_{device_key}"


def planner_config_subentry_id(entry: EnergyPlannerConfigEntry, device_key: str) -> str | None:
    """Return the config subentry ID for a planner device group."""
    for subentry in getattr(entry, "subentries", {}).values():
        if subentry.subentry_type == device_key:
            return subentry.subentry_id
    return None


def planner_device_configured(entry: EnergyPlannerConfigEntry, device_key: str) -> bool:
    """Return whether a planner device group should be exposed."""
    return device_key not in OPTIONAL_DEVICE_KEYS or planner_config_subentry_id(entry, device_key) is not None


def async_add_planner_entities(
    entry: EnergyPlannerConfigEntry,
    async_add_entities: Any,
    entities: Iterable[Any],
) -> None:
    """Add planner entities under their matching config subentry."""
    grouped: dict[str | None, list[Any]] = {}
    for entity in entities:
        device_key = entity.planner_device_key
        if not planner_device_configured(entry, device_key):
            continue
        subentry_id = planner_config_subentry_id(entry, device_key)
        grouped.setdefault(subentry_id, []).append(entity)
    for subentry_id, group in grouped.items():
        async_add_entities(group, config_subentry_id=subentry_id)


class EnergyPlannerEntity(CoordinatorEntity[EnergyPlannerCoordinator]):
    """Base coordinator entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        key: str,
        device_key: str | None = None,
    ) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        device_key = device_key or planner_device_key_for_entity(key)
        self.planner_device_key = device_key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={planner_device_identifier(coordinator.entry.entry_id, device_key)},
            manufacturer=INTEGRATION_NAME,
            model=DEVICE_MODELS[device_key],
            name=DEVICE_NAMES[device_key],
        )
