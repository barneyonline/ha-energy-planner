"""Hard-constraint validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .const import (
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_DRY_RUN,
    CONF_ENPHASE_MIN_SAVINGS,
    CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_GRID_EXPORT_LIMIT_KW,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_HVAC_MIN_CYCLE_MINUTES,
    CONF_HVAC_PRECONDITION_WHILE_AWAY,
    CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT,
    CONF_PLANNER_ENABLED,
)
from .models import (
    ActionAsset,
    ActionKind,
    ConstraintViolation,
    DecisionContext,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    PlanAction,
    PlannerMode,
)
from .ownership import EnphaseProfileGuard, OwnershipState
from .safety import strict_bool


class ConstraintValidator:
    """Validate shared planning and execution hard constraints."""

    def __init__(self, options: Mapping[str, Any]) -> None:
        """Initialize validator."""
        self.options = options

    def validate_plan(self, context: DecisionContext, plan: EnergyPlan) -> list[str]:
        """Return hard-constraint violations for a plan."""
        return [violation.code for violation in self.evaluate_plan(context, plan)]

    def evaluate_plan(self, context: DecisionContext, plan: EnergyPlan) -> list[ConstraintViolation]:
        """Return hard-constraint violations for a plan."""
        violations: list[ConstraintViolation] = []
        if context.input_health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
            violations.append(_violation("input_health_unsafe", "Required inputs are stale, missing, or invalid."))
        battery_floor = float(self.options[CONF_BATTERY_MIN_SOC_PERCENT])
        if context.current_battery_soc_percent is not None and context.current_battery_soc_percent < battery_floor:
            violations.append(
                _violation(
                    "battery_soc_below_floor",
                    (
                        f"Current battery SOC {context.current_battery_soc_percent:.1f}% "
                        f"is below floor {battery_floor:.1f}%."
                    ),
                    ActionAsset.ENPHASE,
                )
            )
        ev_min = float(self.options[CONF_EV_MIN_SOC_PERCENT])
        ev_max = float(self.options[CONF_EV_MAX_SOC_PERCENT])
        if ev_min > ev_max:
            violations.append(_violation("ev_min_above_ev_max", "EV minimum SOC is above maximum SOC.", ActionAsset.EV))
        if plan.actions and plan.mode == PlannerMode.DISABLED:
            violations.append(
                _violation(
                    "disabled_plan_must_not_generate_control_actions",
                    "Disabled plans must not emit control actions.",
                )
            )
        violations.extend(self._evaluate_grid_limits(context))
        return violations

    def _evaluate_grid_limits(self, context: DecisionContext) -> list[ConstraintViolation]:
        violations: list[ConstraintViolation] = []
        import_limit_kw = float(self.options[CONF_GRID_IMPORT_LIMIT_KW])
        export_limit_kw = float(self.options[CONF_GRID_EXPORT_LIMIT_KW])
        import_exceeded = False
        export_exceeded = False
        for slot in context.slots:
            import_kw, export_kw = _projected_grid_flows_kw(slot)
            if not import_exceeded and import_kw is not None and import_kw > import_limit_kw:
                violations.append(
                    _violation(
                        "grid_import_limit_exceeded",
                        f"Projected grid import {import_kw:.2f} kW exceeds limit {import_limit_kw:.2f} kW.",
                    )
                )
                import_exceeded = True
            if not export_exceeded and export_kw is not None and export_kw > export_limit_kw:
                violations.append(
                    _violation(
                        "grid_export_limit_exceeded",
                        f"Projected grid export {export_kw:.2f} kW exceeds limit {export_limit_kw:.2f} kW.",
                    )
                )
                export_exceeded = True
            if import_exceeded and export_exceeded:
                break
        return violations

    def validate_action(
        self,
        context: DecisionContext,
        plan: EnergyPlan,
        action: PlanAction,
        *,
        now: datetime,
        ownership: OwnershipState | None = None,
    ) -> list[str]:
        """Return hard-constraint violation codes for an action."""
        return [
            violation.code for violation in self.evaluate_action(context, plan, action, now=now, ownership=ownership)
        ]

    def evaluate_action(
        self,
        context: DecisionContext,
        plan: EnergyPlan,
        action: PlanAction,
        *,
        now: datetime,
        ownership: OwnershipState | None = None,
    ) -> list[ConstraintViolation]:
        """Validate a single action immediately before execution."""
        ownership = ownership or OwnershipState()
        violations: list[ConstraintViolation] = []
        if not strict_bool(self.options.get(CONF_PLANNER_ENABLED), default=False):
            violations.append(_action_violation(action, "planner_disabled", "Planner execution is disabled."))
        if strict_bool(self.options.get(CONF_DRY_RUN), default=True):
            violations.append(_action_violation(action, "dry_run_enabled", "Dry run is enabled."))
        if context.input_health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
            violations.append(_action_violation(action, "input_health_not_healthy", "Required inputs are unsafe."))
        if not _action_window_contains(action, now):
            violations.append(
                _action_violation(action, "action_outside_execution_window", "Action is not currently due.")
            )
        if _plan_is_expired(plan, now):
            violations.append(_action_violation(action, "plan_expired", "Plan is older than the configured horizon."))
        # Plan-wide grid projections and unrelated asset constraints belong to
        # plan health, not to an individual command rejection. In particular,
        # a neutral Enphase restore must not be blamed for an existing grid
        # limit violation that it did not cause.
        if action.kind != ActionKind.RESTORE_AI:
            violations.extend(
                violation
                for violation in self.evaluate_plan(context, replace(plan, actions=[]))
                if violation.asset == action.asset
            )
        if action.asset == ActionAsset.ENPHASE:
            violations.extend(self._evaluate_enphase_action(action, now, ownership))
        manual_hvac_conflict = action.asset == ActionAsset.DAIKIN and (
            ownership.manual_hvac_override_active(now)
            or any(
                override.kind == "manual_hvac" and (override.expires_at is None or now < override.expires_at)
                for override in context.active_overrides
            )
        )
        if manual_hvac_conflict:
            violations.append(
                _action_violation(action, "manual_hvac_override_active", "Manual HVAC override is active.")
            )
        if action.asset == ActionAsset.DAIKIN and not manual_hvac_conflict:
            violations.extend(self._evaluate_hvac_action(context, action, now, ownership))
        if action.asset == ActionAsset.EV:
            violations.extend(self._evaluate_ev_action(context, action))
        return violations

    def _evaluate_enphase_action(
        self,
        action: PlanAction,
        now: datetime,
        ownership: OwnershipState,
    ) -> list[ConstraintViolation]:
        if action.kind not in {ActionKind.SET_PROFILE, ActionKind.RESTORE_AI}:
            return []
        violations: list[ConstraintViolation] = []
        if action.kind == ActionKind.SET_PROFILE:
            expected_savings = action.expected_cost_delta
            min_savings = float(self.options[CONF_ENPHASE_MIN_SAVINGS])
            if expected_savings is None or expected_savings < min_savings:
                violations.append(
                    _action_violation(
                        action,
                        "enphase_takeover_savings_below_threshold",
                        "Expected Enphase takeover savings are below the configured threshold.",
                    )
                )
        guard = EnphaseProfileGuard(
            min_hold=timedelta(minutes=int(self.options[CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES])),
            last_changed_at=ownership.enphase_profile_changed_at,
        )
        if guard.can_change(now):
            return violations
        violations.append(
            _action_violation(
                action,
                "enphase_profile_hold_active",
                "Enphase profile minimum hold period has not elapsed.",
            )
        )
        return violations

    def _evaluate_ev_action(self, context: DecisionContext, action: PlanAction) -> list[ConstraintViolation]:
        violations: list[ConstraintViolation] = []
        if context.ev_connected is False:
            violations.append(
                _action_violation(
                    action,
                    "ev_not_connected",
                    "EV action cannot run while the vehicle is disconnected.",
                )
            )
        desired_soc = action.desired_state.get("target_soc_percent")
        if desired_soc is None:
            return violations
        ev_min = float(self.options[CONF_EV_MIN_SOC_PERCENT])
        ev_max = float(self.options[CONF_EV_MAX_SOC_PERCENT])
        infeasible_evidence = (
            bool(action.desired_state.get("infeasible")) and action.desired_state.get("allocated_slots") is not None
        )
        if not ev_min <= float(desired_soc) <= ev_max and not (infeasible_evidence and float(desired_soc) <= ev_max):
            violations.append(
                _action_violation(
                    action,
                    "ev_target_soc_outside_bounds",
                    f"EV target SOC {float(desired_soc):.1f}% is outside configured bounds.",
                )
            )
        if (
            not action.desired_state.get("keep_charger_on")
            and context.current_ev_soc_percent is not None
            and float(desired_soc) < context.current_ev_soc_percent
        ):
            violations.append(
                _action_violation(
                    action,
                    "ev_target_soc_below_current",
                    "EV target SOC is below current SOC.",
                )
            )
        return violations

    def _evaluate_hvac_action(
        self,
        context: DecisionContext,
        action: PlanAction,
        now: datetime,
        ownership: OwnershipState,
    ) -> list[ConstraintViolation]:
        violations: list[ConstraintViolation] = []
        desired_mode = action.desired_state.get("hvac_mode")
        if context.occupancy_state == OccupancyState.UNKNOWN:
            return [
                _action_violation(
                    action,
                    "occupancy_unknown_for_hvac",
                    "HVAC action cannot run while occupancy is unknown.",
                )
            ]
        away_preconditioning_allowed = (
            context.occupancy_state == OccupancyState.AWAY
            and strict_bool(self.options.get(CONF_HVAC_PRECONDITION_WHILE_AWAY, False), default=False)
            and _is_verified_away_preconditioning_action(action)
        )
        if (
            context.occupancy_state == OccupancyState.AWAY
            and desired_mode != "off"
            and not away_preconditioning_allowed
        ):
            violations.append(
                _action_violation(
                    action,
                    "hvac_action_not_allowed_while_away",
                    "HVAC comfort action cannot run while all configured people are away.",
                )
            )
        lifecycle_continuation = _is_same_hvac_lifecycle(context, action, ownership)
        if desired_mode != "off" and not lifecycle_continuation and _hvac_min_cycle_active(
            now,
            ownership,
            timedelta(minutes=int(self.options[CONF_HVAC_MIN_CYCLE_MINUTES])),
        ):
            violations.append(
                _action_violation(
                    action,
                    "hvac_min_cycle_active",
                    "HVAC minimum cycle/rest period has not elapsed.",
                )
            )
        if action.desired_state.get("suppress_automations") and not _hvac_suppression_is_directionally_safe(
            context,
            desired_mode,
            float(self.options[CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT]),
        ):
            violations.append(
                _action_violation(
                    action,
                    "hvac_comfort_not_valid_for_suppression",
                    "HVAC automation suppression requires occupied comfort to remain valid.",
                )
            )
        target = action.desired_state.get("target_temperature")
        if (context.occupancy_state == OccupancyState.OCCUPIED or away_preconditioning_allowed) and target is not None:
            if context.occupied_temperature_low_c is None or context.occupied_temperature_high_c is None:
                violations.append(
                    _action_violation(
                        action,
                        "hvac_comfort_bounds_unavailable",
                        "Occupied HVAC comfort target helpers are unavailable.",
                    )
                )
            else:
                tolerance = float(self.options[CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT]) / 100.0
                low = context.occupied_temperature_low_c * (1 - tolerance)
                high = context.occupied_temperature_high_c * (1 + tolerance)
                if not low <= float(target) <= high:
                    violations.append(
                        _action_violation(
                            action,
                            "hvac_target_outside_comfort_bounds",
                            "HVAC target temperature is outside the configured occupied comfort bounds.",
                        )
                    )
        return violations


def _violation(
    code: str,
    message: str,
    asset: ActionAsset | None = None,
) -> ConstraintViolation:
    return ConstraintViolation(code=code, message=message, asset=asset)


def _action_violation(action: PlanAction, code: str, message: str) -> ConstraintViolation:
    return ConstraintViolation(
        code=code,
        message=message,
        asset=action.asset,
        action_id=action.action_id,
    )


def _is_verified_away_preconditioning_action(action: PlanAction) -> bool:
    """Return whether an HVAC-on action carries a complete tariff lifecycle."""
    desired = action.desired_state
    phase = desired.get("phase")
    mode = desired.get("mode")
    period_start = desired.get("period_start")
    period_end = desired.get("period_end")
    precondition_end = desired.get("precondition_end")
    lifecycle_numbers = (
        desired.get("target_temperature"),
        desired.get("baseline_price"),
        desired.get("precondition_min_price_delta"),
        desired.get("suppression_min_price_delta"),
    )
    valid_numbers = all(
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(float(value))
        for value in lifecycle_numbers
    )
    valid_window = False
    if all(
        isinstance(value, datetime) and value.tzinfo is not None
        for value in (period_start, period_end, precondition_end)
    ):
        valid_window = precondition_end <= period_start < period_end
    return bool(
        phase in {"preconditioning", "pre_peak_coast", "peak_coast"}
        and desired.get("hvac_mode") == mode
        and mode in {"heat", "cool"}
        and valid_numbers
        and desired.get("suppress_automations") is True
        and valid_window
        and "away_preconditioning_enabled" in action.hard_constraints
        and f"hvac_{phase}" in action.reason_codes
    )


def _is_same_hvac_lifecycle(
    context: DecisionContext,
    action: PlanAction,
    ownership: OwnershipState,
) -> bool:
    """Return whether an action continues the exact persisted tariff lifecycle."""
    persisted = context.hvac_control
    desired = action.desired_state
    lifecycle_phases = {"preconditioning", "pre_peak_coast", "peak_coast"}
    if not (
        ownership.planner_takeover_started_at is not None
        and ownership.hvac_control_phase in lifecycle_phases
        and desired.get("phase") in lifecycle_phases
        and persisted.get("phase") in lifecycle_phases
        and persisted.get("mode") in {"heat", "cool"}
        and desired.get("mode") == persisted.get("mode")
        and desired.get("hvac_mode") == persisted.get("mode")
    ):
        return False
    return all(
        _lifecycle_datetime(persisted.get(key)) == _lifecycle_datetime(desired.get(key)) is not None
        for key in ("period_start", "period_end", "precondition_end")
    )


def _lifecycle_datetime(value: Any) -> datetime | None:
    """Normalize stored and in-memory lifecycle timestamps for comparison."""
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _action_window_contains(action: PlanAction, now: datetime) -> bool:
    return action.execute_not_before <= now <= action.execute_not_after


def _plan_is_expired(plan: EnergyPlan, now: datetime) -> bool:
    return now > plan.created_at + timedelta(hours=plan.horizon_hours)


def _hvac_min_cycle_active(now: datetime, ownership: OwnershipState, min_cycle: timedelta) -> bool:
    if min_cycle <= timedelta(0) or ownership.planner_takeover_started_at is None:
        return False
    return now < ownership.planner_takeover_started_at + min_cycle


def _hvac_suppression_is_directionally_safe(
    context: DecisionContext,
    desired_mode: Any,
    tolerance_percent: float,
) -> bool:
    """Return whether direct HVAC control moves away from a dangerous extreme."""
    if (
        context.current_hvac_temperature_c is None
        or context.occupied_temperature_low_c is None
        or context.occupied_temperature_high_c is None
    ):
        return False
    tolerance = tolerance_percent / 100.0
    low = context.occupied_temperature_low_c * (1 - tolerance)
    high = context.occupied_temperature_high_c * (1 + tolerance)
    current = float(context.current_hvac_temperature_c)
    if desired_mode == "heat":
        return current <= high
    if desired_mode == "cool":
        return current >= low
    if desired_mode == "off":
        return context.occupancy_state == OccupancyState.AWAY
    return low <= current <= high


def _projected_grid_flows_kw(slot: Any) -> tuple[float | None, float | None]:
    flexible_load_kw = max(float(slot.projected_ev_load_kw or 0.0), 0.0) + max(
        float(slot.projected_hvac_load_kw or 0.0),
        0.0,
    )
    expected_load_kw = slot.baseline_load_forecast_kw
    upper_load_kw = getattr(slot, "baseline_load_forecast_upper_kw", None)
    expected_pv_kw = slot.pv_forecast_kw
    lower_pv_kw = getattr(slot, "pv_forecast_lower_kw", None)
    if expected_load_kw is None or expected_pv_kw is None:
        return None, None
    conservative_import_kw = (
        float(upper_load_kw if upper_load_kw is not None else expected_load_kw)
        + flexible_load_kw
        - float(lower_pv_kw if lower_pv_kw is not None else expected_pv_kw)
    )
    expected_net_kw = float(expected_load_kw) + flexible_load_kw - float(expected_pv_kw)
    return max(conservative_import_kw, 0.0), max(-expected_net_kw, 0.0)
