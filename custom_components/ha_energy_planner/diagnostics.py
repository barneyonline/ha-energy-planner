"""Diagnostics for Energy Planner."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .action_limits import action_budget, budget_history
from .climate_models import (
    MAX_ENERGY_ERROR,
    MAX_TEMPERATURE_MAE,
    MAX_TEMPERATURE_P90,
    MIN_ACTIVE_EPISODES,
    MIN_ACTIVE_RECALL,
    MIN_HISTORY_DAYS,
    MIN_STATE_ACCURACY,
    MIN_VALIDATION_WINDOWS,
)
from .const import DEFAULT_OPTIONS
from .entry_data import combined_entry_data
from .models import to_jsonable
from .plan_presentation import built_in_load_forecast_attrs
from .preconditioning import current_status
from .safety import control_pause_status
from .storage import audit_records, climate_audit_records
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
        "load_forecast": _redact(built_in_load_forecast_attrs(coordinator)),
        "weather_forecast": _redact(
            dict(getattr(coordinator, "weather_forecast_diagnostics", {}) or {})
        ),
        "climate": _redact(climate_diagnostics(store_data, plan, {**DEFAULT_OPTIONS, **entry.options})),
        "action_budgets": {asset: action_budget(budget_history(store_data), {**DEFAULT_OPTIONS, **entry.options},
                                                dt_util.utcnow(), asset)
                           for asset in ("ev", "daikin", "enphase", "enphase_export_limit")},
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


def climate_diagnostics(store_data: dict[str, Any], plan: Any, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Separate model readiness from the last actual climate control outcome."""
    climate_audit = climate_audit_records(store_data, dt_util.utcnow())
    engine = store_data.get("climate_engine", {})
    model = engine.get("model", {})
    observations = engine.get("observations", [])
    normal = [row for row in observations if row.get("provenance") == "normal"]
    canonical = {item.get("action_id"): item for item in audit_records(store_data) if item.get("asset") == "daikin"}
    outcomes = [
        canonical.get(item.get("transaction_id"), item.get("outcome", item))
        for item in climate_audit
        if item.get("stage") == "outcome"
    ] or [item for item in audit_records(store_data) if item.get("asset") == "daikin"]
    control = store_data.get("ownership", {}).get("hvac_control", {})
    control = control if isinstance(control, dict) else {}
    status = current_status(store_data, plan)
    budget = action_budget(
        budget_history(store_data), {**DEFAULT_OPTIONS, **(options or {})}, dt_util.utcnow(), "daikin",
    )
    status["action_budget"] = budget
    if status.get("reason") == "climate_daily_action_cap_reached":
        if budget["allowance_available_at"] is not None:
            status["summary"] = f"Climate action limit reached: {budget['used']} of {budget['limit']} used."
            status["next_step"] = (
                f"Allowance becomes available at {budget['allowance_available_at']}. "
                "Review Maximum daily climate actions in Safety and troubleshooting."
            )
        else:
            status["summary"] = "The previous plan hit the climate action limit; allowance is now available."
            status["next_step"] = "Replan to reassess the remaining execution gates before climate control resumes."
    return {
        "preconditioning": status,
        "lifecycle_id": control.get("lifecycle_id"),
        "phase_transition_reason": control.get("phase_transition_reason"),
        "demand_explanation": (
            "Lower thermostat demand while keeping schedules suppressed and preserving original restoration settings. "
            "Compressor activity may continue."
        )
        if control.get("phase") in {"pre_peak_coast", "peak_coast"}
        else "Heating or cooling toward the preconditioning target."
        if control.get("phase") == "preconditioning"
        else "Normal climate controls or restoration retain authority.",
        "restoration_stage": "pending" if control.get("required_evidence_lost") else "owned" if control else "released",
        "unresolved_settings": {
            key: control.get(key)
            for key in ("main_state", "zone_states")
            if control.get("required_evidence_lost") and control.get(key)
        },
        "unresolved_automations": store_data.get("ownership", {}).get("climate_automations", {})
        if control.get("required_evidence_lost")
        else {},
        "climate_audit": climate_audit[-100:],
        "last_failure_category": next(
            (item["failure_category"] for item in reversed(climate_audit) if item.get("failure_category")),
            None,
        ),
        "last_failure_stage": next(
            (item.get("stage") for item in reversed(climate_audit) if item.get("failure_category")),
            None,
        ),
        "last_override_attribution": next(
            (item for item in reversed(climate_audit) if item.get("override_source")), None
        ),
        "power_source_type": engine.get("power_source_type", "unknown"),
        "power_source_reason": engine.get("power_source_reason"),
        "power_source_conflict": engine.get("power_source_conflict", False),
        "economic_eligibility_reason": "measured_power_required"
        if engine.get("power_source_type") != "measured"
        else "mode_validation_required"
        if engine.get("status") not in {"active", "ready_observing"}
        else "eligible",
        "validation_requirements": {
            "days": MIN_HISTORY_DAYS,
            "windows": MIN_VALIDATION_WINDOWS,
            "active_episodes": MIN_ACTIVE_EPISODES,
            "temperature_mae": MAX_TEMPERATURE_MAE,
            "temperature_p90": MAX_TEMPERATURE_P90,
            "energy_error": MAX_ENERGY_ERROR,
            "state_accuracy": MIN_STATE_ACCURACY,
            "active_recall": MIN_ACTIVE_RECALL,
            "daily_passes": 2,
        },
        "observation_deadline": engine.get("observation_until"),
        "observation_invalid_reason": engine.get("observation_invalid_reason"),
        "economic_status": engine.get("status", "learning"),
        "ever_active": bool(engine.get("ever_active")),
        "readiness": engine.get("modes", {}),
        "observation_count": len(observations),
        "normal_observation_count": len(normal),
        "normal_history_days": len({str(row.get("at", ""))[:10] for row in normal if row.get("at")}),
        "model_trained_at": model.get("trained_at"),
        "validation": {
            mode: {key: value for key, value in result.items() if key != "residuals"}
            for mode, result in model.get("validation", {}).items()
        },
        "decision": {} if plan is None else plan.device_plans.get("climate", {}).get("economics", {}),
        "last_outcome": outcomes[-1] if outcomes else None,
        "last_release": next((item for item in reversed(outcomes) if item.get("kind") == "release_hvac"), None),
        "pending_restore": control if isinstance(control, dict) and control.get("required_evidence_lost") else {},
    }


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
        "climate_engine": {key: value for key, value in store_data.get("climate_engine", {}).items()
                           if key not in {"observations", "model"}},
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
