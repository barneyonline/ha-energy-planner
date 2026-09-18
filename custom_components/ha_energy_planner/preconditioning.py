"""Read-only preconditioning explanations and bounded opportunity history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .climate_inputs import instant
from .models import ActionAsset, DecisionContext, OccupancyState, PlanAction, PlannerMode, to_jsonable
from .planner_confidence import _confidence_rejection_reason
from .safety import strict_bool


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
    if not economic or economic.get("legacy_fallback"):
        if any(
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
        elif _confidence_rejection_reason(ActionAsset.DAIKIN, context, options) is not None:
            status, code = "blocked", "insufficient_confidence"
        elif any(
            value is None
            for value in (
                context.current_hvac_temperature_c,
                context.occupied_temperature_low_c,
                context.occupied_temperature_high_c,
            )
        ):
            status, code = "blocked", "comfort_inputs_missing"
        elif code in {"forecast_gap", "forecast_too_short", "release_hold", "minimum_cycle", "lead_time_disabled"}:
            status = "blocked"
    if controls:
        status, code, reason = (
            "scheduled",
            "schedule_selected",
            "Preconditioning is scheduled; execution gates still apply.",
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
    return [to_jsonable(desired.get(key)) for key in ("period_start", "period_end", "mode")]


def record_plan(history: dict[str, Any], plan: dict[str, Any], control: Any = None) -> dict[str, Any]:
    """Retain one pending window and the last unexecuted expired/withdrawn window."""
    history = dict(history)
    control = control if isinstance(control, dict) else {}
    now = instant(plan["created_at"])
    candidates = [
        a
        for a in plan["actions"]
        if a["asset"] == "daikin" and a["kind"] == "set_hvac" and a["desired_state"].get("phase") == "preconditioning"
    ]
    upcoming = min(candidates, key=lambda a: a["execute_not_before"], default=None)
    pending = history.get("pending", {})
    if pending:
        changed = upcoming is None or window_key(upcoming["desired_state"]) != pending["window"]
        if upcoming is not None and not changed:
            pending = {**pending, "end": upcoming["desired_state"].get("precondition_end", pending["end"])}
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
                "fulfilled": bool(control.get("main_state_committed") and window_key(control) == window_key(desired)),
            }
    return history


def record_outcome(history: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Correlate actual execution evidence before the bounded general audit truncates it."""
    pending = history.get("pending", {})
    desired = outcome.get("desired_state") or {}
    if (
        not pending
        or outcome.get("asset") != "daikin"
        or outcome.get("kind") != "set_hvac"
        or desired.get("phase") != "preconditioning"
        or window_key(desired) != pending["window"]
    ):
        return history
    history = dict(history)
    if _fulfilled(outcome) and history.get("last_missed", {}).get("window") == pending["window"]:
        history.pop("last_missed")
    return {
        **history,
        "pending": {
            **pending,
            "fulfilled": bool(pending.get("fulfilled") or _fulfilled(outcome)),
            "last_outcome": {
                key: outcome.get(key) for key in ("attempted_at", "plan_id", "action_id", "result", "reason")
            },
        },
    }


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
