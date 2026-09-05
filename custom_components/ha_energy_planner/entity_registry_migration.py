"""Idempotent registry cleanup for retired planner entity surfaces."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .type_defs import EnergyPlannerConfigEntry

RETIRED_ENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "sensor": (
        "next_action",
        "plan_status",
        "estimated_daily_cost",
        "forecast_confidence",
        "confidence_breakdown",
        "decision_audit",
        "rejected_actions",
        "upcoming_timeline",
        "production_readiness",
        "control_block_reason",
        "execution_audit",
        "dry_run_comparison",
        "support_bundle_summary",
        "ai_advice",
        "climate_plan",
        "climate_decision",
        "climate_current_state",
        "climate_next_state",
        "presence_state",
        "enphase_plan",
        "enphase_decision",
        "enphase_current_state",
        "enphase_next_state",
        "ev_charging_plan",
        "ev_decision",
        "ev_current_state",
        "ev_next_state",
        "ev_current_charge_state",
        "ev_next_charge_state",
    ),
    "number": (
        "ev_target_soc",
        "ev_opportunistic_charging_price_threshold",
    ),
    "time": ("ev_ready_by",),
    "switch": (
        "enabled",
        "dry_run",
        "ai_enabled",
        "ev_control_enabled",
        "climate_control_enabled",
        "enphase_control_enabled",
        "ev_connected_helper",
        "ev_opportunistic_charging",
        "ev_keep_charger_on",
    ),
    "button": (
        "ev_start_charging",
        "ev_stop_charging",
        "pause_control_1h",
        "pause_control_4h",
    ),
    "binary_sensor": (
        "data_healthy",
        "takeover_active",
    ),
}


def async_migrate_entity_registry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove exactly this entry's retired unique IDs, preserving user entities."""
    registry = er.async_get(hass)
    for platform, keys in RETIRED_ENTITY_KEYS.items():
        for key in keys:
            entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
            if entity_id is not None:
                registry.async_remove(entity_id)
