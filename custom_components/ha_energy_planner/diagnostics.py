"""Diagnostics for Energy Planner."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .entry_data import combined_entry_data
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
            },
            "issues": plan.input_issues[:20],
        },
        "haeo": _redact(_latest_haeo_status(store_data)),
        "refresh_performance": _redact(getattr(coordinator, "last_refresh_metadata", None)),
        "recent_outcomes": _redact(_recent_items(store_data, "outcomes", limit=10)),
        "recent_audit": _redact(_recent_items(store_data, "execution_audit", limit=20)),
        "recent_dry_run_comparisons": _redact(_recent_items(store_data, "dry_run_comparisons", limit=10)),
        "store": _redact(_store_summary(store_data)),
    }
    return data


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


def _latest_haeo_status(store_data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest compact HAEO run summary."""
    latest_run = _latest_item(store_data, "haeo_runs")
    if latest_run is not None:
        return latest_run
    latest_snapshot = _latest_item(store_data, "forecast_snapshots")
    if isinstance(latest_snapshot, dict):
        return latest_snapshot.get("haeo")
    return None


def _store_summary(store_data: dict[str, Any]) -> dict[str, Any]:
    """Return bounded Store metadata instead of the full Store payload."""
    return {
        "active_plan_present": bool(store_data.get("active_plan")),
        "outcome_count": len(store_data.get("outcomes", [])) if isinstance(store_data.get("outcomes"), list) else 0,
        "forecast_snapshot_count": (
            len(store_data.get("forecast_snapshots", []))
            if isinstance(store_data.get("forecast_snapshots"), list)
            else 0
        ),
        "haeo_run_count": len(store_data.get("haeo_runs", [])) if isinstance(store_data.get("haeo_runs"), list) else 0,
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
        "control_pause": store_data.get("control_pause", {}),
        "forecast_calibration": store_data.get("forecast_calibration", {}),
        "thermal_model": store_data.get("thermal_model", {}),
        "trip_history": _trip_history_summary(store_data.get("trip_history", {})),
    }


def _trip_history_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    records = value.get("records", [])
    return {key: item for key, item in value.items() if key != "records"} | {
        "record_count": len(records) if isinstance(records, list) else 0,
    }


def _recent_items(store_data: dict[str, Any], key: str, *, limit: int) -> list[Any]:
    value = store_data.get(key, [])
    if not isinstance(value, list):
        return []
    return value[-limit:]


def _latest_item(store_data: dict[str, Any], key: str) -> Any:
    value = store_data.get(key, [])
    if not isinstance(value, list) or not value:
        return None
    return value[-1]
