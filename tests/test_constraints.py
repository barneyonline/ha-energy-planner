"""Tests for shared hard-constraint validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    PlanAction,
    PlannerMode,
)
from custom_components.ha_energy_planner.ownership import OwnershipState


def _context(now: datetime) -> DecisionContext:
    return DecisionContext(
        created_at=now,
        plan_id="plan-1",
        slots=[
            DecisionSlot(
                valid_at=now,
                import_price=0.2,
                export_price=0.05,
                pv_forecast_kw=1,
                baseline_load_forecast_kw=2,
            )
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=50,
        occupancy_state=OccupancyState.OCCUPIED,
        input_health=InputHealth.HEALTHY,
    )


def _plan(now: datetime, action: PlanAction) -> EnergyPlan:
    return EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )


def _action(now: datetime, asset: ActionAsset, kind: ActionKind, desired_state: dict[str, object]) -> PlanAction:
    return PlanAction(
        action_id=f"{asset}-{kind}",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=asset,
        kind=kind,
        desired_state=desired_state,
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )


def test_ev_action_target_outside_bounds_is_rejected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 95})
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "ev_max_soc_percent": 90}
    violations = ConstraintValidator(options).validate_action(_context(now), _plan(now, action), action, now=now)
    assert "ev_target_soc_outside_bounds" in violations


def test_grid_import_limit_uses_conservative_load_and_pv_bounds() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 2.0
    context.slots[0].baseline_load_forecast_upper_kw = 5.0
    context.slots[0].pv_forecast_kw = 2.0
    context.slots[0].pv_forecast_lower_kw = 1.0
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 70})
    options = {**DEFAULT_OPTIONS, "grid_import_limit_kw": 3.5}

    violations = ConstraintValidator(options).validate_plan(context, _plan(now, action))

    assert "grid_import_limit_exceeded" in violations


def test_ev_action_rejected_when_vehicle_disconnected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 70})
    context = _context(now)
    context.ev_connected = False
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "ev_not_connected" in violations


def test_enphase_hold_rejects_profile_change() -> None:
    now = datetime(2026, 6, 27, 0, 10, tzinfo=UTC)
    action = _action(now, ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {"profile": "Full Backup"})
    ownership = OwnershipState(enphase_profile="Savings", enphase_profile_changed_at=now - timedelta(minutes=10))
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "enphase_profile_min_hold_minutes": 30}
    violations = ConstraintValidator(options).validate_action(
        _context(now),
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )
    assert "enphase_profile_hold_active" in violations


def test_enphase_takeover_savings_threshold_rejects_low_value_change() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {"profile": "Full Backup"})
    action.expected_cost_delta = 0.05
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
    }
    violations = ConstraintValidator(options).validate_action(_context(now), _plan(now, action), action, now=now)
    assert "enphase_takeover_savings_below_threshold" in violations


def test_manual_hvac_override_rejects_daikin_action() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat"})
    ownership = OwnershipState(manual_hvac_override_expires_at=now + timedelta(hours=1))
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    violations = ConstraintValidator(options).validate_action(
        _context(now),
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )
    assert "manual_hvac_override_active" in violations


def test_hvac_comfort_action_rejected_while_away() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat"})
    context = _context(now)
    context.occupancy_state = OccupancyState.AWAY
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)
    assert "hvac_action_not_allowed_while_away" in violations


def test_hvac_off_allowed_while_away() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "off"})
    context = _context(now)
    context.occupancy_state = OccupancyState.AWAY
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)
    assert "hvac_action_not_allowed_while_away" not in violations
    assert "occupancy_unknown_for_hvac" not in violations


@pytest.mark.parametrize("phase", ["preconditioning", "pre_peak_coast", "peak_coast"])
def test_tariff_preconditioning_can_be_explicitly_allowed_while_away(phase: str) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "hvac_mode": "heat",
            "mode": "heat",
            "phase": phase,
            "target_temperature": 24,
            "suppress_automations": True,
            "period_start": now + timedelta(minutes=5),
            "period_end": now + timedelta(minutes=15),
            "precondition_end": now + timedelta(minutes=5),
            "baseline_price": 0.10,
            "precondition_min_price_delta": 0.20,
            "suppression_min_price_delta": 0.20,
        },
    )
    action.hard_constraints = ["away_preconditioning_enabled"]
    action.reason_codes = [f"hvac_{phase}"]
    context = _context(now)
    context.occupancy_state = OccupancyState.AWAY
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_while_away": True,
    }

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "hvac_action_not_allowed_while_away" not in violations


def test_away_preconditioning_opt_in_does_not_allow_other_or_out_of_bounds_hvac_actions() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = _context(now)
    context.occupancy_state = OccupancyState.AWAY
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_while_away": True,
        "occupied_temperature_tolerance_percent": 10,
    }
    ordinary_action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat"})
    unsafe_preconditioning = _action(
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "hvac_mode": "heat",
            "mode": "heat",
            "phase": "preconditioning",
            "target_temperature": 30,
            "suppress_automations": True,
            "period_start": now + timedelta(minutes=5),
            "period_end": now + timedelta(minutes=15),
            "precondition_end": now + timedelta(minutes=5),
            "baseline_price": 0.10,
            "precondition_min_price_delta": 0.20,
            "suppression_min_price_delta": 0.20,
        },
    )
    unsafe_preconditioning.hard_constraints = ["away_preconditioning_enabled"]
    unsafe_preconditioning.reason_codes = ["hvac_preconditioning"]

    ordinary_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, ordinary_action),
        ordinary_action,
        now=now,
    )
    unsafe_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, unsafe_preconditioning),
        unsafe_preconditioning,
        now=now,
    )

    assert "hvac_action_not_allowed_while_away" in ordinary_violations
    assert "hvac_action_not_allowed_while_away" not in unsafe_violations
    assert "hvac_target_outside_comfort_bounds" in unsafe_violations

    unsafe_preconditioning.desired_state["period_end"] = datetime(2026, 6, 27, 0, 15)
    malformed_window_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, unsafe_preconditioning),
        unsafe_preconditioning,
        now=now,
    )
    assert "hvac_action_not_allowed_while_away" in malformed_window_violations


def test_hvac_min_cycle_rejects_planner_comfort_action() -> None:
    now = datetime(2026, 6, 27, 0, 10, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat", "target_temperature": 18})
    context = _context(now)
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    ownership = OwnershipState(planner_takeover_started_at=now - timedelta(minutes=10))
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_min_cycle_minutes": 20,
    }

    violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )

    assert "hvac_min_cycle_active" in violations


def test_hvac_min_cycle_does_not_block_away_off_action() -> None:
    now = datetime(2026, 6, 27, 0, 10, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "off"})
    context = _context(now)
    context.occupancy_state = OccupancyState.AWAY
    ownership = OwnershipState(planner_takeover_started_at=now - timedelta(minutes=10))
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_min_cycle_minutes": 20,
    }

    violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )

    assert "hvac_min_cycle_active" not in violations
    assert "hvac_action_not_allowed_while_away" not in violations


def test_hvac_min_cycle_does_not_block_persisted_preconditioning() -> None:
    now = datetime(2026, 6, 27, 0, 10, tzinfo=UTC)
    action = _action(
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "hvac_mode": "heat",
            "mode": "heat",
            "target_temperature": 24,
            "phase": "preconditioning",
            "suppress_automations": True,
            "period_start": now + timedelta(minutes=5),
            "period_end": now + timedelta(minutes=15),
            "precondition_end": now + timedelta(minutes=5),
            "baseline_price": 0.10,
            "precondition_min_price_delta": 0.20,
            "suppression_min_price_delta": 0.20,
        },
    )
    action.hard_constraints = ["occupied_comfort_within_bounds"]
    action.reason_codes = ["hvac_preconditioning"]
    context = _context(now)
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.hvac_control = {
        "phase": "preconditioning",
        "mode": "heat",
        "period_start": (now + timedelta(minutes=5)).isoformat(),
        "period_end": (now + timedelta(minutes=15)).isoformat(),
        "precondition_end": (now + timedelta(minutes=5)).replace(tzinfo=None),
    }
    ownership = OwnershipState(
        hvac_control_phase="preconditioning",
        planner_takeover_started_at=now - timedelta(minutes=10),
    )
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_min_cycle_minutes": 20,
    }

    violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )

    assert "hvac_min_cycle_active" not in violations

    context.hvac_control["period_end"] = (now + timedelta(minutes=20)).isoformat()
    different_lifecycle_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )
    assert "hvac_min_cycle_active" in different_lifecycle_violations

    context.hvac_control["period_end"] = "not-a-date"
    malformed_lifecycle_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )
    assert "hvac_min_cycle_active" in malformed_lifecycle_violations

    context.hvac_control["period_end"] = 123
    unsupported_lifecycle_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )
    assert "hvac_min_cycle_active" in unsupported_lifecycle_violations


def test_hvac_min_cycle_blocks_away_off_transition_to_preconditioning() -> None:
    now = datetime(2026, 6, 27, 0, 10, tzinfo=UTC)
    action = _action(
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "hvac_mode": "heat",
            "mode": "heat",
            "target_temperature": 24,
            "phase": "preconditioning",
            "suppress_automations": True,
            "period_start": now + timedelta(minutes=5),
            "period_end": now + timedelta(minutes=15),
            "precondition_end": now + timedelta(minutes=5),
            "baseline_price": 0.10,
            "precondition_min_price_delta": 0.20,
            "suppression_min_price_delta": 0.20,
        },
    )
    action.hard_constraints = ["away_preconditioning_enabled"]
    action.reason_codes = ["hvac_preconditioning"]
    context = _context(now)
    context.occupancy_state = OccupancyState.AWAY
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    ownership = OwnershipState(
        hvac_control_phase="away_off",
        planner_takeover_started_at=now - timedelta(minutes=1),
    )
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_min_cycle_minutes": 20,
        "hvac_precondition_while_away": True,
    }

    violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
        ownership=ownership,
    )

    assert "hvac_min_cycle_active" in violations
    assert "hvac_action_not_allowed_while_away" not in violations


def test_occupied_hvac_target_outside_comfort_bounds_is_rejected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat", "target_temperature": 28})
    context = _context(now)
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "occupied_temperature_tolerance_percent": 10,
    }

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "hvac_target_outside_comfort_bounds" in violations


def test_occupied_hvac_target_inside_comfort_bounds_is_allowed() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat", "target_temperature": 22})
    context = _context(now)
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "occupied_temperature_tolerance_percent": 10,
    }

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "hvac_target_outside_comfort_bounds" not in violations
    assert "hvac_comfort_bounds_unavailable" not in violations


def test_hvac_suppression_rejected_when_comfort_not_valid() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"suppress_automations": True})
    context = _context(now)
    context.current_hvac_temperature_c = 30
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "occupied_temperature_tolerance_percent": 10,
    }

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "hvac_comfort_not_valid_for_suppression" in violations


def test_hvac_suppression_allows_commands_that_recover_toward_comfort() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "occupied_temperature_tolerance_percent": 10,
    }
    for mode, current, target in (("heat", 15, 24), ("cool", 29, 18)):
        action = _action(
            now,
            ActionAsset.DAIKIN,
            ActionKind.SET_HVAC,
            {
                "hvac_mode": mode,
                "target_temperature": target,
                "suppress_automations": True,
            },
        )
        context = _context(now)
        context.current_hvac_temperature_c = current
        context.occupied_temperature_low_c = 18
        context.occupied_temperature_high_c = 24

        violations = ConstraintValidator(options).validate_action(
            context,
            _plan(now, action),
            action,
            now=now,
        )

        assert "hvac_comfort_not_valid_for_suppression" not in violations

    away_off = _action(
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"hvac_mode": "off", "suppress_automations": True},
    )
    away_context = _context(now)
    away_context.occupancy_state = OccupancyState.AWAY
    away_context.current_hvac_temperature_c = 30
    away_context.occupied_temperature_low_c = 18
    away_context.occupied_temperature_high_c = 24

    away_violations = ConstraintValidator(options).validate_action(
        away_context,
        _plan(now, away_off),
        away_off,
        now=now,
    )

    assert "hvac_comfort_not_valid_for_suppression" not in away_violations


def test_plan_validation_reports_config_and_grid_limit_issues() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    ev_action = _action(now, ActionAsset.EV, ActionKind.EV_START, {})
    context = _context(now)
    context.current_battery_soc_percent = 5
    context.slots = [
        DecisionSlot(now, 0.20, 0.05, 0.0, 20.0),
        DecisionSlot(now + timedelta(minutes=5), 0.20, 0.05, 20.0, 0.0),
    ]
    options = {
        **DEFAULT_OPTIONS,
        "battery_min_soc_percent": 10,
        "ev_min_soc_percent": 90,
        "ev_max_soc_percent": 80,
        "grid_import_limit_kw": 5,
        "grid_export_limit_kw": 5,
        "dry_run": True,
    }
    plan = _plan(now, ev_action)
    plan.mode = PlannerMode.DRY_RUN

    violations = ConstraintValidator(options).validate_plan(context, plan)

    assert "battery_soc_below_floor" in violations
    assert "ev_min_above_ev_max" in violations
    assert "disabled_plan_must_not_generate_control_actions" not in violations
    assert "grid_import_limit_exceeded" in violations
    assert "grid_export_limit_exceeded" in violations

def test_disabled_plan_validation_rejects_control_actions() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = _context(now)
    plan = _plan(now, _action(now, ActionAsset.EV, ActionKind.EV_START, {}))
    plan.mode = PlannerMode.DISABLED

    violations = ConstraintValidator(DEFAULT_OPTIONS).validate_plan(context, plan)

    assert "disabled_plan_must_not_generate_control_actions" in violations


def test_action_validation_reports_global_and_time_window_issues() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 70})
    action.execute_not_before = now + timedelta(minutes=5)
    context = _context(now)
    context.input_health = InputHealth.UNSAFE
    plan = _plan(now - timedelta(hours=25), action)
    options = {**DEFAULT_OPTIONS, "planner_enabled": False, "dry_run": True}

    violations = ConstraintValidator(options).validate_action(context, plan, action, now=now)

    assert "planner_disabled" in violations
    assert "dry_run_enabled" in violations
    assert "input_health_not_healthy" in violations
    assert "action_outside_execution_window" in violations
    assert "plan_expired" in violations


def test_action_validation_allows_degraded_inputs_for_asset_specific_planning() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat"})
    context = _context(now)
    context.input_health = InputHealth.DEGRADED
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}

    violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
    )

    assert "input_health_not_healthy" not in violations


def test_plan_and_action_validation_reject_unclassified_input_health() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat"})
    context = _context(now)
    context.input_health = "mystery"  # type: ignore[assignment]
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    validator = ConstraintValidator(options)

    assert "input_health_unsafe" in validator.validate_plan(context, _plan(now, action))
    assert "input_health_not_healthy" in validator.validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
    )


def test_action_validation_rejects_truthy_string_safety_options() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_START, {})
    context = _context(now)
    plan = _plan(now, action)
    options = {**DEFAULT_OPTIONS, "planner_enabled": "true", "dry_run": "false"}

    violations = ConstraintValidator(options).validate_action(context, plan, action, now=now)

    assert "planner_disabled" in violations
    assert "dry_run_enabled" in violations


def test_enphase_restore_ai_has_no_savings_threshold_violation() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.ENPHASE, ActionKind.RESTORE_AI, {"profile": "AI Optimisation"})
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}

    violations = ConstraintValidator(options).validate_action(_context(now), _plan(now, action), action, now=now)

    assert "enphase_takeover_savings_below_threshold" not in violations


def test_enphase_restore_is_not_rejected_for_existing_grid_violation() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.ENPHASE, ActionKind.RESTORE_AI, {"profile": "AI Optimisation"})
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 20.0
    context.slots[0].pv_forecast_kw = 0.0
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "grid_import_limit_kw": 5.0,
    }

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "grid_import_limit_exceeded" not in violations


def test_ev_target_below_current_and_infeasible_evidence_exception() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = _context(now)
    context.current_ev_soc_percent = 70
    action = _action(
        now,
        ActionAsset.EV,
        ActionKind.EV_SCHEDULE,
        {"target_soc_percent": 60, "infeasible": True, "allocated_slots": []},
    )
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_min_soc_percent": 80,
        "ev_max_soc_percent": 90,
    }

    violations = ConstraintValidator(options).validate_action(context, _plan(now, action), action, now=now)

    assert "ev_target_soc_outside_bounds" not in violations
    assert "ev_target_soc_below_current" in violations

    action.desired_state = {
        "target_soc_percent": 80,
        "keep_charger_on": True,
    }
    context.current_ev_soc_percent = 100

    keep_on_violations = ConstraintValidator(options).validate_action(
        context,
        _plan(now, action),
        action,
        now=now,
    )

    assert "ev_target_soc_below_current" not in keep_on_violations


def test_hvac_unknown_occupancy_and_missing_comfort_bounds_are_rejected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat", "target_temperature": 20})
    context = _context(now)
    context.occupancy_state = OccupancyState.UNKNOWN
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}

    assert "occupancy_unknown_for_hvac" in ConstraintValidator(options).validate_action(
        context, _plan(now, action), action, now=now
    )

    context.occupancy_state = OccupancyState.OCCUPIED
    context.occupied_temperature_low_c = None
    context.occupied_temperature_high_c = None
    assert "hvac_comfort_bounds_unavailable" in ConstraintValidator(options).validate_action(
        context, _plan(now, action), action, now=now
    )


def test_plan_rejects_projected_grid_import_above_configured_limit() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 70})
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 4.0
    context.slots[0].pv_forecast_kw = 0.5
    context.slots[0].projected_ev_load_kw = 2.0
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "grid_import_limit_kw": 5.0,
    }

    violations = ConstraintValidator(options).validate_plan(context, _plan(now, action))

    assert "grid_import_limit_exceeded" in violations


def test_plan_rejects_projected_grid_export_above_configured_limit() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {"profile": "Savings"})
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 1.0
    context.slots[0].pv_forecast_kw = 7.0
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "grid_export_limit_kw": 5.0,
    }

    violations = ConstraintValidator(options).validate_plan(context, _plan(now, action))

    assert "grid_export_limit_exceeded" in violations
