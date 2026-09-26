"""Read-only recovery explanations using the same evidence as the safety gate."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from .const import CONF_HOUSEHOLD_LOAD, CONF_LOAD_RECOVERY_MAX_AGE_MINUTES
from .ev_resilience import (
    RECOVERY_PAIR_WINDOW_SECONDS,
    RECOVERY_SAMPLE_MAX_AGE_SECONDS,
    RECOVERY_STABLE_SECONDS,
)
from .ev_runtime import timestamp
from .load_forecast import normalize_power_kw
from .safety import parse_production_state
from .startup_recovery import (
    STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES,
    STARTUP_AUTO_RECOVERY_REQUIRED_RUNS,
    STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from .coordinator import EnergyPlannerCoordinator


def recovery_details(coordinator: EnergyPlannerCoordinator, now: datetime) -> dict[str, Any]:
    """Explain sample acceptance separately from startup authority and plan health."""
    stored = coordinator.store.data
    production = parse_production_state(stored.get("production"))
    raw = production.raw.get("startup_auto_recovery")
    startup = raw if isinstance(raw, dict) else {}
    status = startup.get("status", "inactive")
    requested = coordinator.automatic_control_requested
    retrying = requested and status in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES
    entity_id = coordinator.entry_data.get(CONF_HOUSEHOLD_LOAD)
    raw = stored.get("load_source_outage")
    outage = raw if isinstance(raw, dict) and raw.get("entity_id") == entity_id else {}
    states = getattr(coordinator.hass, "states", None)
    state = states.get(entity_id) if states is not None and entity_id else None
    attrs = getattr(state, "attributes", {}) or {}
    sample_raw = attrs.get("sampled_at_utc")
    sample = timestamp(sample_raw) if sample_raw is not None else timestamp(
        getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        or getattr(state, "last_changed", None)
    )
    first = timestamp(outage.get("recovery_first_sample"))
    started = timestamp(outage.get("started_at"))
    age = (now - sample).total_seconds() if sample else None
    max_age = float(coordinator.options.get(
        CONF_LOAD_RECOVERY_MAX_AGE_MINUTES, RECOVERY_SAMPLE_MAX_AGE_SECONDS / 60,
    )) * 60
    pending = bool(outage) or bool(getattr(coordinator, "_load_recovery_pending", False))
    reason, summary = "inactive", "No recovery pending"
    if pending and not outage:
        # The source pair has already passed; only the healthy-plan handoff remains.
        reason, summary = "waiting_for_plan", "Consumption readings accepted; waiting for a healthy plan"
    elif outage:
        unit = str(attrs.get("unit_of_measurement") or attrs.get("unit") or "")
        if normalize_power_kw(getattr(state, "state", None), unit) is None:
            reason, summary = "sample_unavailable", "Waiting for a valid consumption reading"
        elif sample is None:
            reason, summary = "sample_timestamp_missing", "Consumption sample timestamp is missing or invalid"
        elif age is not None and age < 0:
            reason, summary = "sample_in_future", "Consumption sample timestamp is in the future"
        elif age is not None and age > max_age:
            reason, summary = "sample_too_old", "Consumption sample is too old"
        elif started is not None and sample < started:
            reason, summary = "sample_before_outage", "Waiting for a consumption sample taken after the outage"
        elif outage and (
            first is None or sample < first
            or (now - first).total_seconds() > RECOVERY_PAIR_WINDOW_SECONDS
            or (sample - first).total_seconds() < RECOVERY_STABLE_SECONDS
        ):
            reason, summary = "waiting_for_next_sample", "Waiting for a second advancing consumption sample"
        else:
            reason, summary = "waiting_for_plan", "Consumption readings ready; waiting for a healthy plan"
    elif retrying:
        summaries = {
            "waiting": "Waiting for startup recovery",
            "waiting_for_home_assistant": "Waiting for Home Assistant to start",
            "grace": "Startup grace period",
            "validating": "Checking whether automatic control can resume",
            "restoring": "Restoring devices before automatic control resumes",
            "waiting_for_safe": "Waiting for a safe plan and ready devices",
        }
        reason, summary = str(status), summaries[str(status)]
    elif status == "recovered" and coordinator.effective_control:
        reason, summary = "recovered", "Recovered; automatic control is active"
    plan = coordinator.data
    return {
        "summary": summary,
        "reason": reason,
        "evaluated_at": now,
        "automatic_control_requested": requested,
        "armed": coordinator.effective_control,
        "automatic_retry": retrying,
        "retry_interval_seconds": (
            STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS
            if retrying and status in {"waiting_for_safe", "validating", "restoring"}
            else None
        ),
        "startup_status": status,
        "startup_last_reason": startup.get("last_reason"),
        "successful_checks": startup.get("successful_runs", 0),
        "required_checks": startup.get("required_runs", STARTUP_AUTO_RECOVERY_REQUIRED_RUNS),
        "startup_started_at": startup.get("started_at"),
        "startup_grace_deadline": startup.get("deadline"),
        "recovered_at": startup.get("completed_at"),
        "plan_issues": list(plan.input_issues) if plan else [],
        "load_recovery_pending": pending,
        "load_recovery_stage": outage.get("recovery_stage"),
        "source_entity_id": entity_id,
        "source_state": getattr(state, "state", None),
        "source_unit": attrs.get("unit_of_measurement") or attrs.get("unit"),
        "sample_timestamp_source": "sampled_at_utc" if sample_raw is not None else "entity_report_time",
        "sampled_at": sample,
        "sample_age_seconds": round(age, 1) if age is not None else None,
        "sample_max_age_seconds": max_age,
        "first_sample_at": first,
        "first_sample_age_seconds": round((now - first).total_seconds(), 1) if first else None,
        "sample_advance_seconds": round((sample - first).total_seconds(), 1) if sample and first else None,
        "required_sample_advance_seconds": RECOVERY_STABLE_SECONDS,
        "sample_pair_window_seconds": RECOVERY_PAIR_WINDOW_SECONDS,
        "outage_started_at": started,
    }
