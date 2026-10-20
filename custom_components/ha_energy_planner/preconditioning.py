"""Read-only preconditioning explanations and bounded opportunity history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .climate_inputs import finite, instant
from .models import ActionAsset, DecisionContext, OccupancyState, PlanAction, PlannerMode, to_jsonable
from .planner_confidence import _confidence_rejection_reason, _hvac_rollback_capability_unavailable
from .safety import strict_bool
from .zone_temperature import control_temperature


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
    if not economic.get("summary") or economic.get("legacy_fallback"):
        if _confidence_rejection_reason(ActionAsset.DAIKIN, context, options) is not None:
            status, code = "blocked", "insufficient_confidence"
        elif any(
            o.kind == "manual_hvac" and (o.expires_at is None or context.created_at < o.expires_at)
            for o in context.active_overrides
        ):
            status, code = "blocked", "manual_hvac_override"
        elif context.occupancy_state == OccupancyState.UNKNOWN:
            status, code, reason = "blocked", "occupancy_unknown", "Occupancy is unknown; preconditioning is withheld."
        elif context.occupancy_state == OccupancyState.AWAY and not strict_bool(
            options.get("hvac_precondition_while_away"), default=False
        ):
            status, code = "blocked", "occupancy_away"
        elif any(
            value is None
            for value in (
                control_temperature(context),
                context.occupied_temperature_low_c,
                context.occupied_temperature_high_c,
            )
        ):
            status, code = "blocked", "comfort_inputs_missing"
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
    if _hvac_rollback_capability_unavailable(context):
        status, code, reason = (
            "blocked",
            "rollback_target_unavailable",
            "A required thermostat or zone restoration target is unavailable; takeover is withheld.",
        )
    if not strict_bool(options.get("climate_control_enabled"), default=False):
        status, code, reason = "blocked", "climate_control_disabled", "The Climate control switch is off."
    if mode != PlannerMode.ACTIVE_HEALTHY:
        status, code, reason = (
            "blocked",
            "planner_not_active",
            "Automatic execution is disabled, in review, or awaiting healthy inputs.",
        )
    return {
        "status": status,
        "reason": code,
        "summary": reason,
        "evaluated_at": context.created_at.isoformat(),
        "next_start": to_jsonable(next_start),
        "thermal_timing": thermal,
        "legacy_rejections": [
            {"reason": key, "count": item["count"], **item["evidence"]}
            for key, item in legacy.get("rejected", {}).items()
        ],
        "next_step": {
            "scheduled": "Wait for the scheduled start; current safety gates are checked again before execution.",
            "blocked": "Resolve the reported blocker; the next planner refresh reassesses eligibility.",
            "learning": "Allow normal climate operation to collect the missing validation evidence.",
            "observation": "Allow the observation period to finish, or review the observation-only policy setting.",
            "no_opportunity": "No action is needed; the next refresh checks updated prices and thermal conditions.",
        }.get(status, "Review the reported evidence before changing settings."),
    }


def window_key(desired: dict[str, Any]) -> list[Any]:
    """Match a climate window across regenerated plan/action IDs."""
    return (
        ["lifecycle", desired["lifecycle_id"]]
        if desired.get("lifecycle_id")
        else [to_jsonable(desired.get(key)) for key in ("period_start", "period_end", "mode")]
    )


def record_plan(history: dict[str, Any], plan: dict[str, Any], control: Any = None) -> dict[str, Any]:
    """Retain one pending window and the last unexecuted expired/withdrawn window."""
    history = dict(history)
    control = control if isinstance(control, dict) else {}
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
            if not pending.get("fulfilled"):
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
                        and _same_episode(history["pending"], previous)):
                    history["pending"]["episode_start"] = min(current_start, previous_start).isoformat()
            history["pending"]["fulfilled"] = _confirmed_window(control, history["pending"])
            for record in confirmed:
                if _same_episode(history["pending"], record):
                    history["pending"]["fulfilled"] = True
                    history["pending"]["confirmation"] = _confirmation(record, record["attempted_at"])
                    if record.get("last_outcome"):
                        history["pending"]["last_outcome"] = record["last_outcome"]
    return history


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
                "last_outcome": {
                    field: outcome.get(field) for field in ("attempted_at", "plan_id", "action_id", "result", "reason")
                },
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
    outcome = pending.get("last_outcome", {})
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
    elif control.get("phase") in {"preconditioning", "pre_peak_coast", "peak_coast"}:
        result.update(
            status="running",
            reason=control["phase"],
            summary=f"The planner owns the {control['phase']} phase.",
            next_step="The planner continues to check comfort, evidence, and restoration requirements.",
        )
    elif outcome and not _fulfilled(outcome) and plan is not None and outcome.get("plan_id") == plan.plan_id:
        result.update(
            status="blocked",
            reason=outcome.get("reason"),
            summary="The scheduled preconditioning command did not run.",
            next_step="Resolve the execution blocker shown in reason; eligibility is checked again on refresh.",
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
    target = finite(desired.get("target_temperature", desired.get("precondition_target")))
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
