"""Read-only preconditioning explanations and bounded opportunity history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .climate_inputs import finite, instant
from .const import (
    HVAC_CAPABILITY_ISSUES,
    HVAC_DRY_TAKEOVER_SUMMARY,
    HVAC_DRY_ZONE_TARGETS_ABSENT,
    HVAC_ROLLBACK_TARGET_FAULTS,
)
from .models import ActionAsset, DecisionContext, InputHealth, OccupancyState, PlanAction, PlannerMode, to_jsonable
from .planner_confidence import _confidence_rejection_reason
from .safety import strict_bool
from .zone_temperature import control_temperature

_DRY_MODE_NEXT_STEP = "Planner reassesses HVAC takeover after Dry mode ends."
_BLOCKED_NEXT_STEP = "Resolve the reported blocker; the next planner refresh reassesses eligibility."
_ROLLBACK_TARGET_BLOCK_REASON = "rollback_target_unavailable"
_ROLLBACK_TARGET_BLOCK_SUMMARY = (
    "A required thermostat or zone restoration target is unavailable; takeover is withheld."
)


def _setpoint_block_details(input_issues: list[str]) -> tuple[str, str]:
    """Describe a known setpoint block, giving actual faults precedence."""
    if HVAC_ROLLBACK_TARGET_FAULTS.intersection(input_issues):
        return _ROLLBACK_TARGET_BLOCK_REASON, _ROLLBACK_TARGET_BLOCK_SUMMARY
    return HVAC_DRY_ZONE_TARGETS_ABSENT, HVAC_DRY_TAKEOVER_SUMMARY


def planning_status(
    context: DecisionContext, actions: list[PlanAction], mode: PlannerMode, rejection: str, options: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe selected policy without claiming that a planned command ran."""
    economic = context.climate_decision
    legacy = context.climate_legacy_decision
    controls = [
        a for a in actions if a.asset == ActionAsset.DAIKIN and a.desired_state.get("phase") == "preconditioning"
    ]
    next_start = min((a.execute_not_before for a in controls), default=None)
    status, reason = "no_opportunity", rejection
    code = legacy.get("reason", "no_candidate")
    if economic.get("summary") and not economic.get("legacy_fallback"):
        reason = economic["summary"]
        code = economic.get("reason", "no_candidate")
        status = economic.get("preconditioning_status", "no_opportunity")
    if status == "scheduled" and not controls:
        status, code, reason = (
            "blocked",
            "schedule_not_selected",
            "An opportunity was evaluated, but no preconditioning command was selected; "
            "review policy and existing ownership.",
        )
    explaining_fallback = (
        not economic.get("summary") or economic.get("legacy_fallback")
        or (economic.get("reason") == "zone_subset_economic_model_unvalidated"
            and not context.climate_engine.get("ever_active"))
    )
    if explaining_fallback:
        confidence_rejection = _confidence_rejection_reason(ActionAsset.DAIKIN, context, options)
        if confidence_rejection is not None:
            status, code, reason = "blocked", "insufficient_confidence", confidence_rejection
        elif any(
            o.kind == "manual_hvac" and (o.expires_at is None or context.created_at < o.expires_at)
            for o in context.active_overrides
        ):
            status, code, reason = "blocked", "manual_hvac_override", "A manual climate override is active."
        elif context.occupancy_state == OccupancyState.UNKNOWN:
            status, code, reason = "blocked", "occupancy_unknown", "Occupancy is unknown; preconditioning is withheld."
        elif context.occupancy_state == OccupancyState.AWAY and not strict_bool(
            options.get("hvac_precondition_while_away"), default=False
        ):
            status, code, reason = (
                "blocked", "occupancy_away", "Away preconditioning is disabled and nobody is currently home."
            )
        elif any(
            value is None
            for value in (
                control_temperature(context),
                context.occupied_temperature_low_c,
                context.occupied_temperature_high_c,
            )
        ):
            status, code, reason = "blocked", "comfort_inputs_missing", "Required room comfort inputs are unavailable."
        elif code in {"forecast_gap", "forecast_too_short", "release_hold", "minimum_cycle", "lead_time_disabled",
                      "zone_temperature_unavailable", "zone_comfort_boundary"}:
            status = "blocked"
    if controls:
        status, code, reason = (
            "scheduled",
            "schedule_selected",
            "Preconditioning is scheduled; execution gates still apply.",
        )
    thermal = legacy.get("thermal_timing", {})
    if controls and thermal and not thermal.get("rates_ready"):
        reason = (f"Scheduled using the {thermal['base_lead_minutes']:g}-minute fallback; "
                  "finishing temperature is unknown. Execution gates still apply.")
    if explaining_fallback and status != "blocked" and not controls and legacy.get("reason") in {
        "room_demand_unproven", "conflicting_room_demand", "room_target_reached", "preconditioning_mode_not_allowed",
    }:
        status, code, reason = "no_opportunity", legacy["reason"], legacy["summary"]
    dry_mode_blocked = HVAC_DRY_ZONE_TARGETS_ABSENT in context.input_issues
    if dry_mode_blocked or HVAC_ROLLBACK_TARGET_FAULTS.intersection(context.input_issues):
        status = "blocked"
        code, reason = _setpoint_block_details(context.input_issues)
    if not strict_bool(options.get("climate_control_enabled"), default=False):
        status, code, reason = "blocked", "climate_control_disabled", "The Climate control switch is off."
    current_review = mode == PlannerMode.DRY_RUN and context.input_health in {InputHealth.HEALTHY, InputHealth.DEGRADED}
    if mode != PlannerMode.ACTIVE_HEALTHY and not (dry_mode_blocked and current_review):
        status, code, reason = (
            "blocked",
            "planner_not_active",
            "Automatic execution is disabled, in review, or awaiting healthy inputs.",
        )
    next_step = {
        "scheduled": "Wait for the scheduled start; current safety gates are checked again before execution.",
        "blocked": _BLOCKED_NEXT_STEP,
        "learning": "Allow normal climate operation to collect the missing validation evidence.",
        "observation": "Allow the observation period to finish, or review the observation-only policy setting.",
        "no_opportunity": "No action is needed; the next refresh checks updated prices and thermal conditions.",
    }.get(status, "Review the reported evidence before changing settings.")
    if code == HVAC_DRY_ZONE_TARGETS_ABSENT:
        next_step = _DRY_MODE_NEXT_STEP
    return {
        "status": status,
        "reason": code,
        "summary": reason,
        "evaluated_at": context.created_at.isoformat(),
        "next_start": to_jsonable(next_start),
        "thermal_timing": thermal,
        "direction": legacy.get("direction", {}),
        "legacy_rejections": [
            {"reason": key, "count": item["count"], **item["evidence"]}
            for key, item in legacy.get("rejected", {}).items()
        ],
        "next_step": next_step,
    }


def window_key(desired: dict[str, Any]) -> list[Any]:
    """Match a climate window across regenerated plan/action IDs."""
    return (
        ["lifecycle", desired["lifecycle_id"]]
        if desired.get("lifecycle_id")
        else [to_jsonable(desired.get(key)) for key in ("period_start", "period_end", "mode")]
    )


def record_plan(history: dict[str, Any], plan: dict[str, Any], control: Any = None) -> dict[str, Any]:
    """Retain one pending window and the last unexecuted window that could run."""
    history = dict(history)
    control = control if isinstance(control, dict) else {}
    missed = history.get("last_missed", {})
    # Repair persisted premature replacement reports using their original time,
    # even when the next refresh is after the scheduled start. Later, untimed
    # and in-flight attempt evidence must survive an older planning timestamp.
    if missed.get("reason") == "window_replaced" and _unstarted(missed, missed.get("recorded_at"), control):
        history.pop("last_missed")
    previous_windows = [history.get(key, {}) for key in ("pending", "last_missed")]
    # Preserve confirmation across overlapping replacements, including after
    # early coast changes the ownership deadline or a late audit predates the
    # replacement's regenerated start. Neither a plan nor overlap proves success.
    confirmed = [
        evidence for key in ("pending", "last_missed")
        if (evidence := _confirmed_record(history.get(key, {}), control)) is not None
    ]
    missed = history.get("last_missed", {})
    if missed and (
        _confirmed_window(control, missed)
        or any(_same_episode(missed, record) for record in confirmed)
    ):
        history.pop("last_missed")
    now = instant(plan["created_at"])
    candidates = [
        a
        for a in plan["actions"]
        if a["asset"] == "daikin" and a["kind"] == "set_hvac" and a["desired_state"].get("phase") == "preconditioning"
    ]
    upcoming = min(candidates, key=lambda a: a["execute_not_before"], default=None)
    pending = history.get("pending", {})
    if pending:
        pending = {
            **pending,
            "fulfilled": bool(
                pending.get("fulfilled") or _confirmed_window(control, pending)
                or any(_same_episode(pending, record) for record in confirmed)
            ),
        }
        history["pending"] = pending
        changed = upcoming is None or window_key(upcoming["desired_state"]) != pending["window"]
        future = _unstarted(pending, now, control)
        if future and not pending.get("fulfilled") and upcoming is not None:
            changed = changed or (
                instant(upcoming["execute_not_before"]) != instant(pending["start"])
                or (pending.get("preparation") is not None
                    and _preparation(upcoming["desired_state"]) != pending["preparation"])
            )
        if upcoming is not None and not changed:
            pending = {**pending, "end": upcoming["desired_state"].get("precondition_end", pending["end"])}
            if pending.get("preparation") is not None:
                pending["preparation"] = _preparation(upcoming["desired_state"])
            history["pending"] = pending
        for record in confirmed:
            if _same_episode(pending, record):
                pending = {**pending, "confirmation": _confirmation(record, record["attempted_at"])}
                history["pending"] = pending
        end = instant(pending["end"])
        if changed or (now is not None and end is not None and now >= end):
            if not pending.get("fulfilled") and not future:
                history["last_missed"] = {
                    **pending,
                    "recorded_at": plan["created_at"],
                    "reason": "window_expired"
                    if now is not None and end is not None and now >= end
                    else "window_replaced",
                    "replacement_summary": plan.get("device_plans", {})
                    .get("climate", {})
                    .get("preconditioning", {})
                    .get("summary"),
                }
            history.pop("pending", None)
    if upcoming is not None and "pending" not in history:
        desired = upcoming["desired_state"]
        end = instant(desired.get("precondition_end"))
        if now is not None and end is not None and now < end:
            history["pending"] = {
                "window": window_key(desired),
                "plan_id": plan["plan_id"],
                "action_id": upcoming["action_id"],
                "planned_at": plan["created_at"],
                "start": upcoming["execute_not_before"],
                "end": end.isoformat(),
                "preparation": _preparation(desired),
                "fulfilled": False,
            }
            for previous in previous_windows:
                previous_start = instant(previous.get("episode_start", previous.get("start")))
                current_start = instant(history["pending"].get("episode_start", upcoming["execute_not_before"]))
                if (previous_start is not None and current_start is not None
                        and (not _unstarted(previous, now, control) or previous_start == current_start)
                        and _same_episode(history["pending"], previous)):
                    history["pending"]["episode_start"] = min(current_start, previous_start).isoformat()
                    if previous.get("attempt_started") is True:
                        history["pending"]["attempt_started"] = True
            history["pending"]["fulfilled"] = _confirmed_window(control, history["pending"])
            for record in confirmed:
                if _same_episode(history["pending"], record):
                    history["pending"]["fulfilled"] = True
                    history["pending"]["confirmation"] = _confirmation(record, record["attempted_at"])
                    if record.get("last_outcome"):
                        history["pending"]["last_outcome"] = record["last_outcome"]
    return history


def _unstarted(record: dict[str, Any], at: Any, control: dict[str, Any]) -> bool:
    """Require a future episode without later, untimed or in-flight attempts."""
    start, evaluated = instant(record.get("episode_start", record.get("start"))), instant(at)
    outcome = record.get("last_outcome", {})
    attempted = instant(outcome.get("attempted_at"))
    return bool(
        start is not None and evaluated is not None and evaluated < start
        and (not outcome or (attempted is not None and attempted < start))
        and record.get("attempt_started") is not True
        and not _attempt_in_progress(control, record)
    )


def _attempt_in_progress(control: dict[str, Any], record: dict[str, Any]) -> bool:
    """Keep uncertain acquisition history without treating it as fulfillment."""
    preparation = _preparation(control)
    return bool(
        control.get("main_state") and not control.get("main_state_committed")
        and control.get("phase") == "preconditioning"
        and (record.get("preparation") is None or record["preparation"] == preparation)
        and (
            window_key(control) == record.get("window")
            or _same_preparation(record, preparation, control.get("command_confirmation", {}).get("issued_at"))
        )
    )


def record_acquisition(history: dict[str, Any], control: Any) -> dict[str, Any]:
    """Retain attempted preparation before rollback can retire ownership."""
    history = dict(history)
    control = control if isinstance(control, dict) else {}
    for key in ("pending", "last_missed"):
        record = history.get(key, {})
        if record and _attempt_in_progress(control, record):
            history[key] = {**record, "attempt_started": True}
    return history


def outcome_capability_issues(outcome: dict[str, Any]) -> list[str]:
    """Read only known HVAC capability codes from persisted execution evidence."""
    issues = outcome.get("capability_issues")
    if not isinstance(issues, list):
        return []
    return list(dict.fromkeys(
        issue for issue in issues if isinstance(issue, str) and issue in HVAC_CAPABILITY_ISSUES
    ))


def _attempt_summary(outcome: dict[str, Any]) -> dict[str, Any]:
    issues = outcome_capability_issues(outcome)
    return {
        **{field: outcome.get(field) for field in ("attempted_at", "plan_id", "action_id", "result", "reason")},
        **({"capability_issues": issues} if issues else {}),
    }


def record_outcome(history: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Correlate actual execution evidence before the bounded general audit truncates it."""
    desired = outcome.get("desired_state") or {}
    if (
        outcome.get("asset") != "daikin"
        or outcome.get("kind") != "set_hvac"
        or desired.get("phase") != "preconditioning"
    ):
        return history
    history = dict(history)
    matched = [
        record for key in ("pending", "last_missed")
        if (record := history.get(key, {}))
        and _same_preparation(record, _preparation(desired), outcome.get("attempted_at"))
    ]
    for key in ("pending", "last_missed"):
        record = history.get(key, {})
        if not record or not (
            window_key(desired) == record["window"]
            or any(_same_episode(record, source) for source in matched)
        ):
            continue
        if key == "last_missed" and _fulfilled(outcome):
            history.pop(key)
        else:
            history[key] = {
                **record,
                "fulfilled": bool(record.get("fulfilled") or _fulfilled(outcome)),
                "last_outcome": _attempt_summary(outcome),
            }
            if _fulfilled(outcome):
                for source in matched:
                    if _same_episode(record, source):
                        history[key]["confirmation"] = _confirmation(source, outcome["attempted_at"])
                        break
    return history


def current_status(store: dict[str, Any], plan: Any) -> dict[str, Any]:
    """Combine planning evidence with current ownership and matching execution evidence."""
    result = dict(plan.device_plans.get("climate", {}).get("preconditioning", {})) if plan is not None else {}
    history = store.get("preconditioning_history", {})
    result["last_missed_opportunity"] = history.get("last_missed")
    control = store.get("ownership", {}).get("hvac_control", {})
    if not isinstance(control, dict):
        control = {}
    pending = history.get("pending", {})
    outcome = pending.get("last_outcome") or {}
    plan_id = getattr(plan, "plan_id", None)
    audit = store.get("execution_audit")
    if plan_id is not None and isinstance(audit, list):
        for attempt in reversed(audit):
            if (isinstance(attempt, dict) and attempt.get("plan_id") == plan_id and attempt.get("asset") == "daikin"
                    and attempt.get("kind") == "set_hvac"):
                # A coast or direct-control refusal does not belong to preparation history.
                if HVAC_DRY_ZONE_TARGETS_ABSENT in outcome_capability_issues(attempt):
                    outcome = _attempt_summary(attempt)
                break
    matched_refusal = bool(outcome and not _fulfilled(outcome) and plan_id is not None
                           and outcome.get("plan_id") == plan_id)
    reason = outcome.get("reason")
    live_issues = outcome_capability_issues(outcome) or (reason.split(",") if isinstance(reason, str) else [])
    live_dry_block = matched_refusal and HVAC_DRY_ZONE_TARGETS_ABSENT in live_issues
    planned_dry_block = HVAC_DRY_ZONE_TARGETS_ABSENT in getattr(plan, "input_issues", ())
    if control.get("required_evidence_lost"):
        result.update(
            status="restoring",
            reason="pending_restore",
            summary="Previous climate settings still need restoration.",
            next_step="Restore the unavailable device or valid target; restoration is retried before new control.",
        )
    elif control.get("main_state") and not control.get("main_state_committed"):
        result.update(
            status="restoring",
            reason="ownership_unconfirmed",
            summary="Saved climate ownership is awaiting command confirmation or recovery.",
            next_step="Wait for reconciliation; check device availability if recovery remains pending.",
        )
    elif control.get("phase") in {"preconditioning", "pre_peak_coast", "peak_coast"} and not (
        planned_dry_block or live_dry_block
    ):
        result.update(
            status="running",
            reason=control["phase"],
            summary=f"The planner owns the {control['phase']} phase.",
            next_step="The planner continues to check comfort, evidence, and restoration requirements.",
        )
    elif matched_refusal and (live_dry_block or not planned_dry_block):
        if HVAC_DRY_ZONE_TARGETS_ABSENT not in live_issues:
            result.update(
                status="blocked",
                reason=reason,
                summary="The scheduled preconditioning command did not run.",
                next_step="Resolve the execution blocker shown in reason; eligibility is checked again on refresh.",
            )
        elif result.get("reason") not in {
            "climate_control_disabled", "planner_not_active", _ROLLBACK_TARGET_BLOCK_REASON,
        }:
            reason, summary = _setpoint_block_details([*live_issues, *plan.input_issues])
            result.update(
                status="blocked",
                reason=reason,
                summary=summary,
                next_step=_DRY_MODE_NEXT_STEP if reason == HVAC_DRY_ZONE_TARGETS_ABSENT else _BLOCKED_NEXT_STEP,
            )
    elif result.get("status") == "scheduled" and getattr(plan, "mode", None) != PlannerMode.ACTIVE_HEALTHY:
        result.update(
            status="blocked",
            reason="planner_not_active",
            summary="Automatic execution is disabled, in review, or awaiting healthy inputs.",
            next_step="Review the current plan health and validation issues before enabling control.",
        )
    elif result.get("status") == "scheduled" and store.get("production", {}).get("armed") is False:
        result.update(
            status="blocked",
            reason="production_gate_not_armed",
            summary="Automatic control is not armed.",
            next_step="Review readiness and enable Automatic control.",
        )
    result["last_attempt"] = outcome or None
    return result


def _fulfilled(outcome: dict[str, Any]) -> bool:
    """An idempotent confirmed target is fulfilled without claiming a service call."""
    return outcome.get("result") == "applied" or (
        outcome.get("result") == "skipped" and outcome.get("reason") == "already_in_desired_hvac_state"
    )


def _preparation(desired: dict[str, Any]) -> dict[str, Any] | None:
    """Describe a legacy preparation without changing the control lifecycle ID."""
    configuration = desired.get("configuration_identity")
    mode = desired.get("hvac_mode", desired.get("mode"))
    target = finite(desired.get("precondition_target") if desired.get("equipment_compensation")
                    else desired.get("target_temperature", desired.get("precondition_target")))
    peak = instant(desired.get("period_start"))
    deadline = instant(desired.get("precondition_end"))
    if (
        not isinstance(configuration, str) or not configuration.startswith("legacy:")
        or not desired.get("lifecycle_id") or mode not in {"heat", "cool"}
        or target is None or peak is None or deadline is None or deadline > peak
    ):
        return None
    return {
        "configuration": configuration,
        "mode": mode,
        "target": target,
        "peak": peak.isoformat(),
        "deadline": deadline.isoformat(),
    }


def _same_preparation(record: dict[str, Any], preparation: Any, at: Any) -> bool:
    """Bound cross-ID reporting correlation to execution inside the saved window."""
    start = instant(record.get("episode_start", record.get("start")))
    end, attempted = instant(record.get("end")), instant(at)
    return bool(
        preparation and preparation == record.get("preparation")
        and start is not None and end is not None and attempted is not None
        and start <= attempted < end
    )


def _same_episode(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Transfer existing proof only between complete, overlapping preparations."""
    left_start = instant(left.get("episode_start", left.get("start")))
    right_start = instant(right.get("episode_start", right.get("start")))
    left_end, right_end = instant(left.get("end")), instant(right.get("end"))
    return bool(
        left.get("preparation") and left["preparation"] == right.get("preparation")
        and left_start is not None and left_end is not None
        and right_start is not None and right_end is not None
        and max(left_start, right_start) < min(left_end, right_end)
    )


def _confirmation(record: dict[str, Any], at: Any) -> dict[str, Any]:
    """Keep the bounded source window when proof predates a regenerated start."""
    return {
        "preparation": record["preparation"],
        "start": record.get("episode_start", record["start"]),
        "end": record["end"],
        "attempted_at": to_jsonable(at),
    }


def _confirmed_record(record: dict[str, Any], control: dict[str, Any]) -> dict[str, Any] | None:
    """General strict-ID fulfillment alone is not bounded cross-ID evidence."""
    proof = record.get("confirmation", {})
    if _same_episode(record, proof) and _same_preparation(proof, proof.get("preparation"), proof.get("attempted_at")):
        return {**record, **proof}
    outcome = record.get("last_outcome", {})
    if (record.get("fulfilled") and _fulfilled(outcome)
            and _same_preparation(record, record.get("preparation"), outcome.get("attempted_at"))):
        return {**record, "attempted_at": outcome["attempted_at"]}
    if _confirmed_window(control, record):
        at = _preparation_started_at(control)
        if _same_preparation(record, record.get("preparation"), at):
            return {**record, "attempted_at": at}
    return None


def _preparation_started_at(control: dict[str, Any]) -> Any:
    """A later coast timestamp cannot establish when preparation began."""
    if control.get("preconditioning_started_at") is not None:
        return control["preconditioning_started_at"]
    return (
        control.get("phase_started_at", control.get("started_at"))
        if control.get("phase") == "preconditioning" else None
    )


def _confirmed_window(control: dict[str, Any], record: dict[str, Any]) -> bool:
    """Use committed preconditioning ownership, never a later coasting-only acquisition."""
    return bool(
        control.get("main_state_committed")
        and (control.get("phase") == "preconditioning" or control.get("preconditioning_confirmed") is True)
        and (
            window_key(control) == record.get("window")
            or _same_preparation(record, _preparation(control), _preparation_started_at(control))
        )
    )
