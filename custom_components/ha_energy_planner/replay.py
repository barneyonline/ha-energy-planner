"""Historical replay harness for sanitized planner fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .const import DEFAULT_OPTIONS
from .constraints import ConstraintValidator
from .models import (
    ActionAsset,
    ActionKind,
    ConstraintViolation,
    DaylightWindow,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    PlanAction,
    PlannerMode,
)
from .ownership import OwnershipState


@dataclass(slots=True)
class ReplayActionResult:
    """Constraint result for one replayed action."""

    action_id: str
    violations: list[ConstraintViolation]

    @property
    def rejected(self) -> bool:
        """Return whether the action was rejected."""
        return any(violation.blocking for violation in self.violations)


@dataclass(slots=True)
class ReplayResult:
    """Replay result summary."""

    name: str
    plan_violations: list[ConstraintViolation]
    action_results: list[ReplayActionResult]

    @property
    def rejected_action_count(self) -> int:
        """Return number of rejected actions."""
        return sum(1 for result in self.action_results if result.rejected)

    def to_summary(self) -> dict[str, Any]:
        """Return JSON-friendly replay summary."""
        return {
            "name": self.name,
            "plan_violations": [violation.code for violation in self.plan_violations],
            "actions": [
                {
                    "action_id": result.action_id,
                    "rejected": result.rejected,
                    "violations": [violation.code for violation in result.violations],
                }
                for result in self.action_results
            ],
            "rejected_action_count": self.rejected_action_count,
        }


def run_replay_file(path: str | Path) -> ReplayResult:
    """Load and run a sanitized replay fixture."""
    fixture_path = Path(path)
    return run_replay(json.loads(fixture_path.read_text(encoding="utf-8")))


def run_replay(fixture: dict[str, Any]) -> ReplayResult:
    """Run one replay fixture through hard-constraint validation."""
    options = {**DEFAULT_OPTIONS, **fixture.get("options", {})}
    context = _context_from_fixture(fixture["context"])
    plan = _plan_from_fixture(fixture["plan"], context)
    ownership = _ownership_from_fixture(fixture.get("ownership", {}))
    now = _parse_datetime(fixture.get("now", context.created_at.isoformat()))
    validator = ConstraintValidator(options)
    plan_violations = validator.evaluate_plan(context, plan)
    action_results = [
        ReplayActionResult(
            action_id=action.action_id,
            violations=validator.evaluate_action(context, plan, action, now=now, ownership=ownership),
        )
        for action in plan.actions
    ]
    return ReplayResult(
        name=str(fixture.get("name", "unnamed")),
        plan_violations=plan_violations,
        action_results=action_results,
    )


def _context_from_fixture(data: dict[str, Any]) -> DecisionContext:
    return DecisionContext(
        created_at=_parse_datetime(data["created_at"]),
        plan_id=str(data["plan_id"]),
        slots=[
            DecisionSlot(
                valid_at=_parse_datetime(slot["valid_at"]),
                import_price=slot.get("import_price"),
                export_price=slot.get("export_price"),
                pv_forecast_kw=slot.get("pv_forecast_kw"),
                baseline_load_forecast_kw=slot.get("baseline_load_forecast_kw"),
                projected_ev_load_kw=float(slot.get("projected_ev_load_kw", 0.0)),
                projected_hvac_load_kw=float(slot.get("projected_hvac_load_kw", 0.0)),
                outdoor_temperature_forecast_c=slot.get("outdoor_temperature_forecast_c"),
                occupied=slot.get("occupied"),
            )
            for slot in data["slots"]
        ],
        current_battery_soc_percent=data.get("current_battery_soc_percent"),
        current_ev_soc_percent=data.get("current_ev_soc_percent"),
        occupancy_state=OccupancyState(data["occupancy_state"]),
        input_health=InputHealth(data["input_health"]),
        current_hvac_mode=data.get("current_hvac_mode"),
        current_hvac_temperature_c=data.get("current_hvac_temperature_c"),
        current_hvac_power_kw=data.get("current_hvac_power_kw"),
        current_outdoor_temperature_c=data.get("current_outdoor_temperature_c"),
        hvac_control=dict(data.get("hvac_control", {})),
        climate_zone_entities=list(data.get("climate_zone_entities", [])),
        input_issues=list(data.get("input_issues", [])),
        local_timezone=str(data.get("local_timezone", "UTC")),
        daylight_windows=[
            DaylightWindow(
                start=_parse_datetime(window["start"]),
                end=_parse_datetime(window["end"]),
            )
            for window in data.get("daylight_windows", [])
        ],
    )


def _plan_from_fixture(data: dict[str, Any], context: DecisionContext) -> EnergyPlan:
    return EnergyPlan(
        plan_id=str(data.get("plan_id", context.plan_id)),
        created_at=_parse_datetime(data.get("created_at", context.created_at.isoformat())),
        horizon_hours=int(data.get("horizon_hours", 24)),
        interval_minutes=int(data.get("interval_minutes", 5)),
        status=str(data.get("status", "current")),
        health=InputHealth(data.get("health", context.input_health)),
        mode=PlannerMode(data.get("mode", PlannerMode.ACTIVE_HEALTHY)),
        summary=str(data.get("summary", "replay")),
        confidence=float(data.get("confidence", 1.0)),
        estimated_daily_cost=data.get("estimated_daily_cost"),
        actions=[_action_from_fixture(action, context.plan_id) for action in data.get("actions", [])],
        preview=list(data.get("preview", [])),
        input_issues=list(data.get("input_issues", [])),
    )


def _action_from_fixture(data: dict[str, Any], plan_id: str) -> PlanAction:
    return PlanAction(
        action_id=str(data["action_id"]),
        plan_id=str(data.get("plan_id", plan_id)),
        execute_not_before=_parse_datetime(data["execute_not_before"]),
        execute_not_after=_parse_datetime(data["execute_not_after"]),
        asset=ActionAsset(data["asset"]),
        kind=ActionKind(data["kind"]),
        desired_state=dict(data.get("desired_state", {})),
        hard_constraints=list(data.get("hard_constraints", [])),
        reason_codes=list(data.get("reason_codes", [])),
        expected_cost_delta=data.get("expected_cost_delta"),
        confidence=float(data.get("confidence", 1.0)),
    )


def _ownership_from_fixture(data: dict[str, Any]) -> OwnershipState:
    hvac_control = data.get("hvac_control")
    return OwnershipState(
        enphase_profile=data.get("enphase_profile"),
        enphase_profile_changed_at=_parse_datetime_or_none(data.get("enphase_profile_changed_at")),
        climate_automations=dict(data.get("climate_automations", {})),
        ev_smart_charging_state=dict(data.get("ev_smart_charging_state", {})),
        hvac_control_phase=(
            str(hvac_control.get("phase"))
            if isinstance(hvac_control, dict) and hvac_control.get("phase") is not None
            else None
        ),
        planner_takeover_started_at=_parse_datetime_or_none(data.get("planner_takeover_started_at")),
        manual_hvac_override_expires_at=_parse_datetime_or_none(data.get("manual_hvac_override_expires_at")),
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_datetime_or_none(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)
