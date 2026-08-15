"""Binary sensor platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EV_RESERVATION_EXTERNAL_BASELINE
from .coordinator import STARTUP_AUTO_RECOVERY_REQUIRED_RUNS, EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities, recorder_safe_attributes
from .models import InputHealth
from .preflight import _control_area_report, production_evidence_fingerprint
from .safety import DRY_RUN_READY_CYCLES_REQUIRED, control_pause_reason, parse_production_state
from .type_defs import EnergyPlannerConfigEntry


@dataclass(frozen=True, kw_only=True)
class PlannerBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor description."""

    value_fn: Callable[[EnergyPlannerCoordinator], bool]
    attrs_fn: Callable[[EnergyPlannerCoordinator], dict[str, Any]] = lambda coordinator: {}


LEGACY_BINARY_SENSOR_DESCRIPTIONS: tuple[PlannerBinarySensorDescription, ...] = (
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

BINARY_SENSORS: tuple[PlannerBinarySensorDescription, ...] = (
    PlannerBinarySensorDescription(
        key="armed",
        translation_key="armed",
        icon="mdi:shield-check-outline",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda coordinator: parse_production_state(coordinator.store.data.get("production")).armed,
        attrs_fn=lambda coordinator: _armed_attrs(coordinator),
    ),
)


def _planner_ownership_active(store_data: dict[str, Any]) -> bool:
    """Return whether persisted ownership means the planner owns a control."""
    ownership = dict(store_data.get("ownership", {}))
    reservation = store_data.get("ev_grid_reservation")
    if (
        isinstance(reservation, dict)
        and reservation.get("active") is True
        and reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is not True
    ):
        return True
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
            "ev_smart_charging_command_entity_id",
            "ev_smart_charging_control_topology",
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    _remove_retired_binary_sensors(hass, entry)
    async_add_planner_entities(
        entry,
        async_add_entities,
        (PlannerBinarySensor(coordinator, description) for description in BINARY_SENSORS),
    )


def _remove_retired_binary_sensors(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove fragmented binary status entities from the entity registry."""
    registry = er.async_get(hass)
    for description in LEGACY_BINARY_SENSOR_DESCRIPTIONS:
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_{description.key}")
        if entity_id is not None:
            registry.async_remove(entity_id)


def _armed_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the complete but compact automatic-control gate state."""
    production = parse_production_state(coordinator.store.data.get("production"))
    startup_recovery = production.raw.get("startup_auto_recovery")
    startup_recovery = dict(startup_recovery) if isinstance(startup_recovery, dict) else {}
    pause_reason = control_pause_reason(coordinator.store.data.get("control_pause"), dt_util.utcnow())
    control_areas = _control_area_report(dict(coordinator.entry_data), coordinator.options)
    evidence_matches = production.dry_run_evidence_fingerprint == production_evidence_fingerprint(
        dict(coordinator.entry_data), coordinator.options
    )
    evidence_complete = (
        production.dry_run_ready_cycles >= DRY_RUN_READY_CYCLES_REQUIRED
        and bool(control_areas["required"])
        and evidence_matches
    )
    if pause_reason:
        reason = pause_reason
    elif coordinator.dry_run:
        reason = "review_mode"
    elif not production.armed:
        reason = "safety_gate_not_armed"
    else:
        reason = "armed"
    return {
        "armed": production.armed,
        "automatic_control": coordinator.active_control,
        "automatic_control_requested": getattr(
            coordinator,
            "automatic_control_requested",
            coordinator.active_control,
        ),
        "mode": "active" if coordinator.active_control else "review",
        "reason": reason,
        "control_paused": pause_reason is not None,
        "required_control_areas": list(control_areas["required"]),
        "dry_run_ready_cycles": production.dry_run_ready_cycles,
        "dry_run_cycles_required": DRY_RUN_READY_CYCLES_REQUIRED,
        "dry_run_evidence_complete": evidence_complete,
        "dry_run_evidence_matches_configuration": evidence_matches,
        "startup_auto_recovery_status": startup_recovery.get("status", "inactive"),
        "startup_auto_recovery_successful_runs": startup_recovery.get("successful_runs", 0),
        "startup_auto_recovery_required_runs": startup_recovery.get(
            "required_runs", STARTUP_AUTO_RECOVERY_REQUIRED_RUNS
        ),
        "startup_auto_recovery_grace_started_at": startup_recovery.get("started_at"),
        "startup_auto_recovery_deadline": startup_recovery.get("deadline"),
        "startup_auto_recovery_last_reason": startup_recovery.get("last_reason"),
    }


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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return binary sensor attributes."""
        return recorder_safe_attributes(self.entity_description.attrs_fn(self.coordinator))
