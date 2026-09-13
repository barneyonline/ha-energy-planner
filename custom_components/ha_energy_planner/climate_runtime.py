"""Pure readiness, observation cadence and economic lifecycle orchestration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .climate_inputs import instant
from .climate_models import (
    BASELINE_VERSION,
    LIFECYCLE_VERSION,
    MIN_HISTORY_DAYS,
    PHYSICAL_VERSION,
    VALIDATION_EXPIRY_DAYS,
    VALIDATION_VERSION,
)
from .climate_optimizer import candidate_summary, optimise, revalidate_schedule, supported_targets
from .const import (
    CONF_HVAC_OBSERVATION_CADENCE,
    CONF_HVAC_POLICY,
    CONF_HVAC_PRECONDITION_WHILE_AWAY,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from .models import ActionAsset, ActionKind, DecisionContext, PlanAction
from .safety import strict_bool


def update_readiness(
    state: dict[str, Any], model: dict[str, Any], context: DecisionContext, options: dict[str, Any]
) -> dict[str, Any]:
    """Two distinct consecutive validation dates, never two refreshes, grant readiness."""
    state = dict(state)
    policy = options.get(CONF_HVAC_POLICY, "automatic")
    statuses = {key: dict(value) for key, value in state.get("modes", {}).items()}
    trained = instant(model.get("trained_at"))
    current_date = trained.date().isoformat() if trained else None
    versions_valid = (
        model.get("baseline_version") == BASELINE_VERSION
        and model.get("validation_version") == VALIDATION_VERSION
        and model.get("physical", {}).get("version") == PHYSICAL_VERSION
    )
    live_valid = (
        versions_valid
        and context.current_hvac_power_kw is not None
        and context.climate_inputs.get("configuration_valid", True)
        and not context.input_issues
        and all(source.get("fresh") for source in context.climate_inputs.get("sources", {}).values())
        and model.get("identity") == context.climate_inputs.get("identity")
    )
    for mode in ("heat", "cool"):
        item = statuses.setdefault(mode, {})
        evidence = model.get("validation", {}).get(mode, {})
        blockers = list(evidence.get("blockers", ["model_unavailable"]))
        for entity in context.climate_inputs.get("zones", {}):
            room = model.get("rooms", {}).get(entity, {})
            if (
                room.get("days", 0) < MIN_HISTORY_DAYS
                or not room.get("physical", {}).get(mode)
                or room.get("validation", {}).get(mode, {}).get("blockers", ["validation_missing"])
            ):
                blockers.append(f"room_model_not_ready:{entity}")

        if current_date and item.get("validation_date") != current_date:
            previous = instant(f"{item.get('validation_date')}T00:00:00+00:00")
            consecutive = (
                previous is not None and trained is not None and trained.date() - previous.date() == timedelta(days=1)
            )
            success = not blockers
            item["passes"] = (int(item.get("passes", 0)) + 1 if consecutive else 1) if success else 0
            item["failures"] = (int(item.get("failures", 0)) + 1 if consecutive else 1) if not success else 0
            item["validation_date"] = current_date
            if success:
                item["last_good"] = evidence.get("last_window_at")
        last_good = instant(item.get("last_good"))
        stale = last_good is None or context.created_at - last_good > timedelta(days=VALIDATION_EXPIRY_DAYS)
        ready = bool(item.get("ready"))
        if not live_valid or stale or int(item.get("failures", 0)) >= 2:
            ready = False
            item["passes"] = 0
        elif int(item.get("passes", 0)) >= 2 and not (
            context.hvac_control and set(context.hvac_control) != {"released_until"}
        ):
            ready = True
        item["ready"] = ready
        item["blockers"] = (
            blockers + ([] if live_valid else ["live_evidence_missing"]) + (["validation_expired"] if stale else [])
        )
    state["modes"] = statuses
    any_ready = any(item.get("ready") for item in statuses.values())
    if policy == "legacy":
        state["status"] = "disabled"
    elif any_ready:
        state["status"] = "ready_observing" if policy == "observe" else "active"
        if policy == "automatic":
            state["ever_active"] = True
    else:
        state["status"] = "degraded" if state.get("ever_active") else "learning"
    state["model"] = model
    return state


def economic_actions(context: DecisionContext, options: dict[str, Any], legacy: list[PlanAction]) -> list[PlanAction]:
    """Return one policy's actions; authority still belongs to the existing executor."""
    state = dict(context.climate_engine)
    policy = options.get(CONF_HVAC_POLICY, "automatic")
    if policy == "legacy" or not context.climate_inputs:
        if context.hvac_control.get("economic_policy_version"):
            return [
                release_action(
                    context, "economic_policy_changed", timedelta(minutes=int(options[CONF_PLANNING_INTERVAL_MINUTES]))
                )
            ]
        return legacy
    model = dict(state.get("model", {}))
    state["model"] = model
    eligible_model = {
        **model,
        "validation": {
            mode: {
                **result,
                "blockers": [
                    *result.get("blockers", []),
                    *([] if state.get("modes", {}).get(mode, {}).get("ready") else ["mode_not_ready"]),
                ],
            }
            for mode, result in model.get("validation", {}).items()
        },
    }
    active = context.hvac_control
    owned_economic = active.get("economic_policy_version") == LIFECYCLE_VERSION
    interval = timedelta(minutes=int(options[CONF_PLANNING_INTERVAL_MINUTES]))
    now = context.created_at
    if active.get("economic_policy_version") and (not owned_economic or active.get("required_evidence_lost")):
        state.pop("scheduled", None)
        context.climate_engine = state
        return [release_action(context, "hvac_required_evidence_lost", interval)]
    if set(active) == {"released_until"}:
        held_until = instant(active.get("released_until"))
        if held_until is None or now < held_until:
            return []
        active = {}
    if owned_economic:
        current = context.current_hvac_temperature_c
        low, high = context.occupied_temperature_low_c, context.occupied_temperature_high_c
        stop = instant(active.get("precondition_end"))
        preconditioning = active.get("phase") == "preconditioning" and stop is not None and now < stop
        warming = preconditioning and active.get("mode") == "heat"
        cooling = preconditioning and active.get("mode") == "cool"
        if (
            current is not None
            and low is not None
            and high is not None
            and ((current <= low and not warming) or (current >= high and not cooling))
        ):
            state.pop("scheduled", None)
            context.climate_engine = state
            return [
                release_action(
                    context, "hvac_comfort_handoff", interval, released_until=instant(active.get("period_end"))
                )
            ]
    blocked = (
        any(o.kind == "manual_hvac" and (o.expires_at is None or now < o.expires_at) for o in context.active_overrides)
        or str(context.occupancy_state) == "unknown"
        or (
            str(context.occupancy_state) == "away"
            and not strict_bool(options.get(CONF_HVAC_PRECONDITION_WHILE_AWAY), default=False)
        )
    )
    candidate = None
    decision: dict[str, Any] = {
        "status": state.get("status", "learning"),
        "readiness": state.get("modes", {}),
        "estimated": True,
    }
    decision["validation"] = {
        mode: {key: value for key, value in result.items() if key != "residuals"}
        for mode, result in model.get("validation", {}).items()
    }
    decision["model_trained_at"] = model.get("trained_at")
    decision["capabilities"] = {
        "arrival": bool(context.climate_inputs.get("arrival")),
        "rooms": sorted(model.get("rooms", {})),
        "humidity": bool(model.get("humidity", {}).get("ready")),
        "solar_exposure": bool(context.climate_inputs.get("irradiance_forecast")),
        "equipment_curve": bool(context.climate_inputs.get("cop_table")),
    }
    incumbent = state.get("scheduled", {})
    retained = None
    if not blocked and state.get("status") in {"active", "ready_observing"}:
        if incumbent and state.get("modes", {}).get(incumbent.get("mode"), {}).get("ready"):
            retained = revalidate_schedule(context, options, eligible_model, incumbent)
        if not owned_economic:
            candidate, comparison = optimise(context, options, eligible_model)
            decision.update(comparison)
        if retained is not None and (
            owned_economic
            or candidate is None
            or candidate.conservative_saving - retained.conservative_saving
            < max(0.10, retained.conservative_saving * 0.20)
        ):
            candidate = retained
            decision.update(candidate_summary(context, retained))
            decision.update({key: incumbent[key] for key in ("lifecycle_id", "start", "stop", "release")})
    observation_until = instant(state.get("observation_until"))
    observing = observation_until is not None and now < observation_until
    opportunity = decision.get("release")
    if opportunity is None:
        opportunity = next(
            (
                str(action.desired_state.get("period_end"))
                for action in legacy
                if action.desired_state.get("phase") == "preconditioning"
            ),
            None,
        )
    opportunity_end = instant(opportunity)
    if opportunity_end is not None:
        opportunity_end = opportunity_end.replace(minute=opportunity_end.minute // 5 * 5, second=0, microsecond=0)
        opportunity = opportunity_end.isoformat()
    previous_opportunity = instant(state.get("opportunity"))
    if (
        opportunity
        and not active
        and state.get("opportunity") != opportunity
        and (previous_opportunity is None or now >= previous_opportunity)
    ):
        state["opportunity"] = opportunity
        state["opportunities"] = int(state.get("opportunities", 0)) + 1
        day = now.astimezone(ZoneInfo(context.local_timezone)).date().isoformat()
        cadence = max(int(options.get(CONF_HVAC_OBSERVATION_CADENCE, 10)), 1)
        if state["opportunities"] % cadence == 0 and state.get("observation_date") != day:
            state["observation_date"] = day
            observation_start = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
            if observation_start < now:
                observation_start += timedelta(minutes=5)
            state["observation_started_at"] = observation_start.isoformat()
            state["observation_until"] = opportunity
            observing = True
            state["observation_prediction"] = {
                **decision,
                "baseline_slots": [slot.valid_at.isoformat() for slot in context.slots],
                "interval_minutes": int(options[CONF_PLANNING_INTERVAL_MINUTES]),
            }
    decision["observation_until"] = state.get("observation_until")
    decision["observing"] = observing
    decision["summary"] = (
        "Normal climate controls are running for a scheduled observation period."
        if observing
        else "Waiting for supported, validated climate and energy evidence."
        if state.get("status") == "learning"
        else "Normal climate controls retain authority because economic evidence is degraded."
        if state.get("status") == "degraded"
        else "No candidate met the economic and comfort requirements."
        if candidate is None
        else "A supported preconditioning schedule has positive estimated and conservative savings."
    )
    context.climate_decision = decision
    context.climate_engine = state
    if owned_economic:
        end = instant(active.get("period_end"))
        arrival_changed = active.get("arrival") != context.climate_inputs.get("arrival")
        if (
            blocked
            or policy != "automatic"
            or state.get("status") != "active"
            or arrival_changed
            or end is None
            or now >= end
        ):
            return [release_action(context, "economic_evidence_lost_or_cycle_ended", interval)]
        # Until the exact remaining incumbent has passed a new simulation, return control.
        if candidate is None or decision.get("mode") != active.get("mode"):
            return [release_action(context, "economic_revalidation_failed", interval)]
    if candidate is None or policy == "observe" or observing or blocked:
        if owned_economic:
            return [release_action(context, "economic_control_released", interval)]
        return legacy if not state.get("ever_active") and not observing else []
    if state.get("status") != "active":
        return legacy if not state.get("ever_active") else []
    # Legacy ownership must complete or restore before a new lifecycle acquires devices.
    if active and not owned_economic:
        return legacy
    decision["commands_selected"] = True
    state["scheduled"] = dict(decision)
    comparisons = list(state.get("comparisons", []))
    if not comparisons or comparisons[-1].get("lifecycle_id") != decision.get("lifecycle_id"):
        comparisons.append({**decision, "created_at": now.isoformat()})
    state["comparisons"] = comparisons[-100:]
    for slot, power in zip(context.slots, candidate.trajectory.powers_kw, strict=True):
        slot.projected_hvac_load_kw = power
    start = instant(decision["start"])
    stop = instant(decision["stop"])
    end = instant(decision["release"])
    assert start is not None and stop is not None and end is not None
    device_targets = supported_targets(context)
    coast = device_targets[0] if candidate.mode == "heat" else device_targets[-1]
    actions: list[PlanAction] = []
    incumbent_stop = instant(incumbent.get("stop"))
    for phase, at, target in (("preconditioning", start, candidate.target), ("peak_coast", stop, coast)):
        if owned_economic and phase == "preconditioning" and incumbent_stop is not None and incumbent_stop <= now:
            continue
        actions.append(
            PlanAction(
                action_id=f"{context.plan_id}-economic-{phase}",
                plan_id=context.plan_id,
                execute_not_before=max(at, now),
                execute_not_after=max(at, now) + interval,
                asset=ActionAsset.DAIKIN,
                kind=ActionKind.SET_HVAC,
                desired_state={
                    "hvac_mode": candidate.mode,
                    "power": "on",
                    "target_temperature": target,
                    "phase": phase,
                    "mode": candidate.mode,
                    "period_start": stop,
                    "period_end": end,
                    "precondition_end": stop,
                    "precondition_target": candidate.target,
                    "coast_target": coast,
                    "baseline_price": 0.0,
                    "precondition_min_price_delta": 0.0,
                    "suppression_min_price_delta": 0.0,
                    "economic_policy_version": LIFECYCLE_VERSION,
                    "lifecycle_id": decision["lifecycle_id"],
                    "configuration_identity": context.climate_inputs.get("identity"),
                    "configured_zones_only": strict_bool(
                        options.get("hvac_precondition_configured_zones_only"), default=False
                    ),
                    "arrival": context.climate_inputs.get("arrival"),
                    "suppress_automations": True,
                    "enable_zones": True,
                    "controlled_zones": list(context.climate_zone_entities),
                },
                hard_constraints=[
                    "away_preconditioning_enabled"
                    if str(context.occupancy_state) == "away"
                    else "occupied_comfort_within_bounds",
                    "manual_hvac_override_inactive",
                    "hvac_min_cycle",
                ],
                reason_codes=[f"hvac_{phase}", "economic_climate_saving"],
                expected_cost_delta=candidate.expected_saving
                if phase == "preconditioning" and not owned_economic
                else None,
                confidence=context.forecast_confidence,
            )
        )
    actions.append(release_action(context, "economic_cycle_ended", interval, at=end))
    return actions


def release_action(
    context: DecisionContext, reason: str, interval: timedelta, *, at: Any = None, released_until: Any = None
) -> PlanAction:
    """Use the existing release transaction to restore automation and device state."""
    start = at or context.created_at
    return PlanAction(
        f"{context.plan_id}-economic-release",
        context.plan_id,
        start,
        start + interval,
        ActionAsset.DAIKIN,
        ActionKind.RELEASE_HVAC,
        {"release_reason": reason, "released_until": released_until},
        [],
        [reason],
        None,
        1.0,
    )


def command_rejection(
    hass: Any,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    engine: dict[str, Any],
    desired: dict[str, Any],
    now: Any,
) -> str | None:
    """Check persisted authority and currently mapped input state immediately before I/O."""
    from .climate_inputs import climate_identity, read_climate_inputs

    if not desired.get("economic_policy_version"):
        return None
    if desired["economic_policy_version"] != LIFECYCLE_VERSION:
        return "economic_climate_policy_version_unknown"
    if options.get(CONF_HVAC_POLICY, "automatic") != "automatic" or engine.get("status") != "active":
        return "economic_climate_not_active"
    identity = climate_identity(entry_data, options)
    if desired.get("configuration_identity") != identity or engine.get("identity") != identity:
        return "economic_climate_configuration_changed"
    if engine.get("scheduled", {}).get("lifecycle_id") != desired.get("lifecycle_id"):
        return "economic_climate_schedule_superseded"
    if hass is not None:
        end = instant(desired.get("period_end"))
        if end is None or now >= end:
            return "economic_climate_window_ended"
        live = read_climate_inputs(
            hass,
            entry_data,
            options,
            now,
            max(end, now + timedelta(hours=float(options.get(CONF_PLANNING_HORIZON_HOURS, 12)))),
            {},
        )
        if live.get("arrival") != desired.get("arrival"):
            return "economic_climate_arrival_changed"
        if not live.get("configuration_valid") or any(not source["fresh"] for source in live["sources"].values()):
            return "economic_climate_live_evidence_missing"
    return None
