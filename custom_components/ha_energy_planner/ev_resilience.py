"""Bounded EV operation and stable recovery during household-meter outages."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .const import CONF_LOAD_RECOVERY_MAX_AGE_MINUTES
from .ev_policy import finite
from .ev_runtime import timestamp

# Additional reserve on top of the model's calibrated upper bound. This is an
# uncertainty allowance, never a replacement for electrical protection.
LOAD_UNCERTAINTY_KW = 1.0
RECOVERY_STABLE_SECONDS = 60
RECOVERY_SAMPLE_MAX_AGE_SECONDS = 900
RECOVERY_PAIR_WINDOW_SECONDS = 1800
RECOVERY_OBSERVATION_SECONDS = 90


def recovering_load_source(
    state: Any, previous: Any, entity_id: str, now: datetime, options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate a fresh source sample from bounded observation and full recovery."""
    if not isinstance(previous, dict) or previous.get("entity_id") != entity_id:
        return {}
    prior = dict(previous)
    attrs = state.attributes or {}
    raw = attrs.get("sampled_at_utc")
    sample = timestamp(raw) if raw is not None else timestamp(
        getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        or getattr(state, "last_changed", None)
    )
    max_age = float((options or {}).get(
        CONF_LOAD_RECOVERY_MAX_AGE_MINUTES, RECOVERY_SAMPLE_MAX_AGE_SECONDS / 60,
    )) * 60
    prior.update(recovering=True, recovery_stage="waiting_for_sample", recovery_degraded_ready=False)
    prior["sample_max_age_seconds"] = max_age
    if sample is None or not 0 <= (now - sample).total_seconds() <= max_age:
        prior.pop("recovery_observed_since", None)
        return prior
    outage_start = timestamp(prior.get("started_at"))
    if outage_start is not None and sample < outage_start:
        prior.pop("recovery_observed_since", None)
        return prior
    first = timestamp(prior.get("recovery_first_sample"))
    if first is None or sample < first or (now - first).total_seconds() > RECOVERY_PAIR_WINDOW_SECONDS:
        prior["recovery_first_sample"] = sample.isoformat()
        if first is not None and sample < first:
            prior.pop("recovery_observed_since", None)
    elif (sample - first).total_seconds() >= RECOVERY_STABLE_SECONDS:
        return {}
    observed = timestamp(prior.get("recovery_observed_since"))
    if observed is None or observed > now:
        observed = now
        prior["recovery_observed_since"] = now.isoformat()
    ready = (now - observed).total_seconds() >= RECOVERY_OBSERVATION_SECONDS
    prior.update(recovery_stage="bounded_degraded" if ready else "stabilizing",
                 recovery_degraded_ready=ready)
    return prior


def fallback_power(context: Any, options: Mapping[str, Any]) -> float:
    """Cap power by the existing setpoint and conservative household headroom."""
    if not context.slots:
        return 0.0
    slot = context.slots[0]
    load = finite(slot.baseline_load_forecast_upper_kw)
    pv = finite(slot.pv_forecast_lower_kw if slot.pv_forecast_lower_kw is not None else slot.pv_forecast_kw)
    configured = finite(options.get("ev_charge_rate_kw"))
    limit = finite(options.get("grid_import_limit_kw"))
    commanded = finite(context.ev_evidence.get("commanded_kw"))
    if any(value is None for value in (load, pv, configured, limit, commanded)):
        return 0.0
    assert load is not None and pv is not None and configured is not None
    assert limit is not None and commanded is not None
    headroom = max(limit - load - max(slot.projected_hvac_load_kw, 0) + pv, 0)
    ceiling = min(configured, commanded, headroom)
    capability = context.ev_evidence.get("power_capability")
    if capability is not None:
        return float(capability.power(ceiling))
    # A fixed-power charger cannot enforce a smaller projected draw.
    return configured if configured > 0 and configured <= ceiling + 1e-6 else 0.0


def fallback_charging_decision(
    context: Any, options: Mapping[str, Any], override: Any
) -> tuple[bool, float, str] | None:
    """Continue only an existing session or an explicit, unexpired charge-now request."""
    if not context.ev_evidence.get("load_fallback", {}).get("fallback_applied"):
        return None
    manual_start = bool(override is not None and override.reason in {"manual_start", "charge_now"}
                        and override.expires_at is not None and override.expires_at > context.created_at)
    manual_stop = override is not None and override.reason == "manual_stop"
    target_reached = (context.current_ev_soc_percent is not None
                      and context.ev_target_soc_percent is not None
                      and context.current_ev_soc_percent >= context.ev_target_soc_percent)
    if manual_stop or target_reached or (context.ev_charging is not True and not manual_start):
        return False, 0.0, "ev_load_fallback_waiting"
    power = fallback_power(context, options)
    return power > 0, power, "ev_load_fallback_continue" if power > 0 else "ev_load_fallback_capacity_pause"


def cap_manual_fallback(action: Any, context: Any, options: Mapping[str, Any]) -> str | None:
    """Apply identical capacity evidence to explicit starts during the bounded fallback."""
    if not context.ev_evidence.get("load_fallback", {}).get("fallback_applied"):
        return None
    fallback = context.ev_evidence["load_fallback"]
    if fallback.get("recovery_pending") and not fallback.get("recovery_degraded_ready"):
        return "ev_load_recovery_stabilizing"
    power = fallback_power(context, options)
    if power <= 0:
        return "ev_load_fallback_capacity_pause"
    action.desired_state["projected_load_kw_now"] = power
    remaining = finite(context.ev_evidence["load_fallback"].get("fallback_remaining_seconds"))
    if remaining is None or remaining <= 0:
        return "ev_load_fallback_expired"
    action.desired_state["load_fallback_until"] = (
        context.created_at + timedelta(seconds=remaining)
    ).isoformat()
    capability = context.ev_evidence.get("power_capability")
    if capability is not None:
        action.desired_state["power_limit"] = {
            "entity_id": capability.entity_id, "value": capability.setpoint(power),
            "unit": capability.unit, "physical_power_kw": power,
        }
    return None


def charging_status(context: Any, overrides: list[Any], now: datetime) -> dict[str, Any]:
    """Describe evidence and operator intent without claiming command confirmation."""
    if context is None:
        return {"summary": "Waiting for charging evidence"}
    fallback = context.ev_evidence.get("load_fallback", {})
    override = next((o for o in overrides if o.kind == "manual_ev_charging"
                     and o.reason == "charge_now" and o.expires_at is not None and o.expires_at > now), None)
    if fallback.get("recovery_stage") == "bounded_degraded" and fallback.get("fallback_applied"):
        summary = "Limited recovery; conservative charging limits remain active"
    elif fallback.get("recovery_pending"):
        summary = "Consumption recovering; waiting for stable readings"
    elif fallback.get("fallback_applied"):
        summary = ("Consumption unavailable; charging with conservative forecast limits"
                   if context.ev_charging is True else "Consumption unavailable; charging paused")
    elif fallback.get("live_source_status") == "unavailable":
        summary = "Consumption unavailable; automatic charging blocked"
    elif override:
        summary = "Charge now active; safety checks remain enabled"
    elif context.ev_charging is True:
        summary = "Charging"
    else:
        summary = "Not charging"
    return {"summary": summary, "charging_observed": context.ev_charging,
            "charge_now_until": override.expires_at.isoformat() if override else None,
            "cost_estimates_degraded": bool(fallback.get("cost_estimates_degraded")),
            "fallback_remaining_seconds": fallback.get("fallback_remaining_seconds"),
            "recovery_pending": bool(fallback.get("recovery_pending")),
            "recovery_stage": fallback.get("recovery_stage", "normal"),
            "uncertainty_margin_kw": fallback.get("uncertainty_margin_kw", 0)}


def retain_outage_power_ceiling(outage: dict[str, Any], commanded_kw: Any) -> float | None:
    """Persist a ceiling that can only decrease until stable source recovery."""
    current = finite(commanded_kw)
    if not outage:
        return current
    prior = finite(outage.get("ev_power_ceiling_kw"))
    ceiling = min(current, prior) if current is not None and prior is not None else current
    if ceiling is not None:
        outage["ev_power_ceiling_kw"] = ceiling
    return ceiling
