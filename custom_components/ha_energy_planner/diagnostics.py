"""Diagnostics for Energy Planner."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .entry_data import combined_entry_data
from .models import to_jsonable
from .safety import control_pause_status
from .storage import audit_records
from .type_defs import EnergyPlannerConfigEntry

REDACT_KEYS = {
    "access_token",
    "address",
    "api_key",
    "auth",
    "credential",
    "latitude",
    "location",
    "longitude",
    "password",
    "prompt",
    "raw_response",
    "secret",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    store_data = dict(coordinator.store.data)
    plan = coordinator.data
    entry_data = combined_entry_data(entry)
    automatic_control_running = bool(getattr(coordinator, "effective_control", False))
    automatic_control_requested = bool(
        getattr(coordinator, "automatic_control_requested", automatic_control_running)
    )
    data = {
        "entry": {
            "data": _redact(entry_data),
            "options": _redact(dict(entry.options)),
        },
        "entity_mapping": _redact(_entity_mapping(entry_data)),
        "input_health": None
        if plan is None
        else {
            "health": str(plan.health),
            "confidence": plan.confidence,
            "issues": plan.input_issues[:20],
        },
        "plan": None
        if plan is None
        else {
            "plan_id": plan.plan_id,
            "created_at": plan.created_at.isoformat(),
            "status": plan.status,
            "health": str(plan.health),
            "mode": str(plan.mode),
            "confidence": plan.confidence,
            "summary": plan.summary,
            "estimated_daily_cost": plan.estimated_daily_cost,
            "estimated_cost_horizon_hours": plan.estimated_cost_horizon_hours,
            "action_count": len(plan.actions),
            "next_action": None
            if plan.next_action is None
            else {
                "action_id": plan.next_action.action_id,
                "asset": str(plan.next_action.asset),
                "kind": str(plan.next_action.kind),
                "execute_not_before": plan.next_action.execute_not_before.isoformat(),
                "execute_not_after": plan.next_action.execute_not_after.isoformat(),
                "confidence": plan.next_action.confidence,
                "reason_codes": plan.next_action.reason_codes,
                "desired_state": to_jsonable(plan.next_action.desired_state),
            },
            "issues": plan.input_issues[:20],
        },
        "refresh_performance": _redact(_refresh_performance(coordinator)),
        "weather_forecast": _redact(
            dict(getattr(coordinator, "weather_forecast_diagnostics", {}) or {})
        ),
        "automatic_control": {
            "requested": automatic_control_requested,
            "running": automatic_control_running,
        },
        "startup_auto_recovery": _redact(
            dict(store_data.get("production", {})).get("startup_auto_recovery", {})
            if isinstance(store_data.get("production"), dict)
            else {}
        ),
        "recent_outcomes": _redact(audit_records(store_data)[-10:]),
        "recent_audit": _redact(audit_records(store_data)[-20:]),
        "recent_dry_run_comparisons": _redact(_recent_items(store_data, "dry_run_comparisons", limit=10)),
        "store": _redact(_store_summary(store_data)),
    }
    return data


def _refresh_performance(coordinator: Any) -> dict[str, Any]:
    """Return rolling runtime metrics when provided, with legacy fallback."""
    rolling = getattr(coordinator, "refresh_metrics", None)
    latest = getattr(coordinator, "last_refresh_metadata", None)
    result = dict(rolling) if isinstance(rolling, dict) else {}
    if isinstance(latest, dict):
        result.setdefault("latest", dict(latest))
    return result


def _redact(value: Any) -> Any:
    """Redact secrets and sensitive location keys."""
    if isinstance(value, dict):
        return {
            key: "**REDACTED**" if any(secret in str(key).lower() for secret in REDACT_KEYS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _entity_mapping(entry_data: dict[str, Any]) -> dict[str, Any]:
    """Return configured entity and service mappings without unrelated config."""
    return {
        key: value
        for key, value in entry_data.items()
        if key.endswith("_entity") or key.endswith("_entities") or key.endswith("_service") or "service" in key
    }


def _store_summary(store_data: dict[str, Any]) -> dict[str, Any]:
    """Return bounded Store metadata instead of the full Store payload."""
    return {
        "active_plan_present": bool(store_data.get("active_plan")),
        "outcome_count": len(audit_records(store_data)),
        "forecast_snapshot_count": (
            len(store_data.get("forecast_snapshots", []))
            if isinstance(store_data.get("forecast_snapshots"), list)
            else 0
        ),
        "dry_run_comparison_count": (
            len(store_data.get("dry_run_comparisons", []))
            if isinstance(store_data.get("dry_run_comparisons"), list)
            else 0
        ),
        "ai_recommendation_count": (
            len(store_data.get("ai_recommendations", []))
            if isinstance(store_data.get("ai_recommendations"), list)
            else 0
        ),
        "discovery": store_data.get("discovery", {}),
        "ownership": store_data.get("ownership", {}),
        "production": store_data.get("production", {}),
        "control_pause": control_pause_status(
            store_data.get("control_pause", {}),
            dt_util.utcnow(),
        ),
        "ev_charge_calibration": _ev_charge_calibration_summary(
            store_data.get("ev_charge_calibration", {})
        ),
        "forecast_calibration": store_data.get("forecast_calibration", {}),
        "built_in_load_forecast": _load_forecast_summary(store_data.get("built_in_load_forecast", {})),
        "load_source_outage": store_data.get("load_source_outage", {}),
        "thermal_model": store_data.get("thermal_model", {}),
    }


def _load_forecast_summary(value: Any) -> dict[str, Any]:
    """Return model health without large per-slot profiles."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key != "profiles"}


def _ev_charge_calibration_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key != "samples"}


def _recent_items(store_data: dict[str, Any], key: str, *, limit: int) -> list[Any]:
    value = store_data.get(key, [])
    if not isinstance(value, list):
        return []
    return value[-limit:]
