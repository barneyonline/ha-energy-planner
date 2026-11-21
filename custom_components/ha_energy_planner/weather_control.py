"""Bound advisory-weather fallback to the existing confirmed HVAC lifecycle."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, cast

from .climate_inputs import finite, instant
from .models import ActionKind, DecisionContext, PlanAction

PHASES = {"preconditioning": 0, "pre_peak_coast": 1, "peak_coast": 2}
INTENT_KEYS = (
    "lifecycle_id", "configuration_identity", "mode", "hvac_mode", "phase", "target_temperature",
    "period_start", "period_end", "precondition_end", "precondition_target", "coast_target",
    "controlled_zones", "zone_intent",
)


def weather_degraded(context: DecisionContext) -> bool:
    """Absent optional weather configuration retains its previous semantics."""
    return getattr(context, "weather_evidence", {}).get("availability") in {"cached", "unavailable"}


def continuation_rejection(context: DecisionContext, now: datetime) -> str | None:
    """Check original weather evidence and confirmed ownership, never grant acquisition."""
    evidence, active = context.weather_evidence, context.hvac_control
    expiry = instant(evidence.get("grace_expires_at"))
    if expiry is not None and now >= expiry:
        return "hvac_weather_grace_expired"
    if evidence.get("availability") != "cached" or expiry is None:
        return "hvac_weather_unavailable"
    if (not active.get("main_state_committed") or active.get("command_confirmation")
            or active.get("required_evidence_lost") or context.hvac_cycle.get("pending")
            or not active.get("weather_safe_intent") or active.get("phase") not in PHASES):
        return "hvac_weather_ownership_unconfirmed"
    end = instant(active.get("period_end"))
    covered_until = instant(evidence.get("continuous_until"))
    if end is None or covered_until is None or covered_until <= now or covered_until < end:
        return "hvac_weather_coverage_lost"
    mode = active.get("mode")
    if mode not in {"heat", "cool"} or (
        active.get("phase") == "preconditioning" and context.current_hvac_mode != mode
    ):
        return "hvac_weather_cycle_changed"
    return None


def intent_rejection(desired: Mapping[str, Any], safe: Mapping[str, Any], now: datetime,
                     current_mode: str | None) -> str | None:
    """Only forward phases and equal or lower demand fit the last confirmed intent."""
    if (not safe or any(desired.get(k) != safe.get(k) for k in
                        ("lifecycle_id", "configuration_identity", "mode", "controlled_zones"))
            or desired.get("mode") not in {"heat", "cool"}):
        return "hvac_weather_intent_changed"
    phase, previous_phase = desired.get("phase"), safe.get("phase")
    if phase not in PHASES or previous_phase not in PHASES or PHASES[phase] < PHASES[previous_phase]:
        return "hvac_weather_phase_changed"
    for key in ("period_end", "precondition_end", "period_start"):
        end, original = instant(desired.get(key)), instant(safe.get(key))
        if end is None or original is None or end > original:
            return "hvac_weather_window_extended"
    if now >= cast(datetime, instant(safe["period_end"])):
        return "hvac_weather_window_ended"
    mode = str(desired["mode"])
    requested_mode = desired.get("hvac_mode")
    if requested_mode != "off" and (requested_mode != mode or current_mode != mode):
        return "hvac_weather_cycle_changed"
    if phase == "preconditioning":
        if now >= cast(datetime, instant(safe["precondition_end"])) or safe.get("hvac_mode") == "off":
            return "hvac_weather_cycle_changed"
        target, original_target = finite(desired.get("target_temperature")), finite(safe.get("target_temperature"))
        if target is None or original_target is None or (
            target > original_target if mode == "heat" else target < original_target
        ):
            return "hvac_weather_demand_increased"
    elif requested_mode != "off" and desired.get("target_temperature") != safe.get("coast_target"):
        return "hvac_weather_demand_increased"
    prior_zones, zones = safe.get("zone_intent") or {}, desired.get("zone_intent") or {}
    if set(zones.get("power", {})) != set(prior_zones.get("power", {})):
        return "hvac_weather_zones_changed"
    for entity, power in zones.get("power", {}).items():
        if power == "on" and prior_zones["power"].get(entity) != "on":
            return "hvac_weather_zones_changed"
    for entity, target in zones.get("targets", {}).items():
        previous = finite(prior_zones.get("targets", {}).get(entity))
        value = finite(target)
        if previous is None or value is None or (value > previous if mode == "heat" else value < previous):
            return "hvac_weather_demand_increased"
    return None


def bound_weather_actions(context: DecisionContext, options: Mapping[str, Any],
                          actions: list[PlanAction]) -> list[PlanAction]:
    """Annotate fresh intent; restrict fallback through existing release/restore actions."""
    from .planner_confidence import _confidence_breakdown, hvac_continuation_confidence

    if not context.weather_evidence:
        return actions
    if not weather_degraded(context):
        for action in actions:
            if action.kind == ActionKind.SET_HVAC:
                action.desired_state["weather_safe_intent"] = {
                    **{key: deepcopy(action.desired_state.get(key)) for key in INTENT_KEYS},
                    "climate_confidence": _confidence_breakdown(context, [])["climate"],
                }
        return actions
    reason = continuation_rejection(context, context.created_at)
    if reason is None and not hvac_continuation_confidence(context, options):
        reason = "hvac_confidence_below_threshold"
    if reason is None:
        for action in actions:
            if action.kind == ActionKind.SET_HVAC:
                reason = intent_rejection(action.desired_state, context.hvac_control["weather_safe_intent"],
                                          action.execute_not_before, context.current_hvac_mode)
                if reason:
                    break
    # Existing safety releases have priority over advisory weather diagnostics.
    immediate_release = next((a for a in actions if a.kind == ActionKind.RELEASE_HVAC
                              and a.execute_not_before <= context.created_at), None)
    if immediate_release is not None:
        reason = str(immediate_release.desired_state.get("release_reason"))
    retained = reason is None and bool(actions)
    context.weather_evidence.update(ownership_retained=retained,
                                    retention_reason="bounded_confirmed_lifecycle" if retained else reason)
    if reason is not None:
        context.climate_decision.update(commands_selected=False, weather_release_reason=reason)
        if not context.hvac_control or set(context.hvac_control) == {"released_until"}:
            return []
        if immediate_release is not None:
            return [immediate_release]
        from .climate_runtime import release_action
        return [release_action(context, reason, timedelta(minutes=int(options["planning_interval_minutes"])))]
    expiry = cast(datetime, instant(context.weather_evidence["grace_expires_at"]))
    bounded = []
    for action in actions:
        if action.kind == ActionKind.SET_HVAC and action.execute_not_before < expiry:
            action.execute_not_after = min(action.execute_not_after, expiry)
            action.desired_state["weather_continuation"] = {
                "expires_at": expiry.isoformat(), "entity_id": context.weather_evidence.get("entity_id"),
            }
            bounded.append(action)
        elif action.kind == ActionKind.RELEASE_HVAC and action.execute_not_before < expiry:
            bounded.append(action)
    from .climate_runtime import release_action
    bounded.append(release_action(context, "hvac_weather_grace_expired",
                                  timedelta(minutes=int(options["planning_interval_minutes"])), at=expiry))
    return bounded
