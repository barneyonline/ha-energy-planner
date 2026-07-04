"""Tests for shared hard-constraint validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    HAEOStatus,
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
        haeo_status=HAEOStatus.READY,
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
        requires_haeo_plan_id=None,
    )


def test_ev_action_target_outside_bounds_is_rejected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 95})
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "ev_max_soc_percent": 90}
    violations = ConstraintValidator(options).validate_action(_context(now), _plan(now, action), action, now=now)
    assert "ev_target_soc_outside_bounds" in violations


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
    assert "dry_run_plan_must_not_generate_control_actions" in violations
    assert "grid_import_limit_exceeded" in violations
    assert "grid_export_limit_exceeded" in violations


def test_action_validation_reports_global_and_time_window_issues() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 70})
    action.requires_haeo_plan_id = "haeo-plan"
    action.execute_not_before = now + timedelta(minutes=5)
    context = _context(now)
    context.input_health = InputHealth.DEGRADED
    context.haeo_status = HAEOStatus.STALE
    plan = _plan(now - timedelta(hours=25), action)
    options = {**DEFAULT_OPTIONS, "planner_enabled": False, "dry_run": True}

    violations = ConstraintValidator(options).validate_action(context, plan, action, now=now)

    assert "planner_disabled" in violations
    assert "dry_run_enabled" in violations
    assert "input_health_not_healthy" in violations
    assert "haeo_not_ready" in violations
    assert "action_outside_execution_window" in violations
    assert "plan_expired" in violations


def test_enphase_restore_ai_has_no_savings_threshold_violation() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.ENPHASE, ActionKind.RESTORE_AI, {"profile": "AI Optimisation"})
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}

    violations = ConstraintValidator(options).validate_action(_context(now), _plan(now, action), action, now=now)

    assert "enphase_takeover_savings_below_threshold" not in violations


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


def test_plan_applies_flexible_load_to_haeo_grid_limit_evidence() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = _action(now, ActionAsset.EV, ActionKind.EV_SCHEDULE, {"target_soc_percent": 70})
    context = _context(now)
    context.slots[0].haeo_grid_export_forecast_kw = 1.0
    context.slots[0].projected_ev_load_kw = 3.0
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "grid_import_limit_kw": 1.5,
    }

    violations = ConstraintValidator(options).validate_plan(context, _plan(now, action))

    assert "grid_import_limit_exceeded" in violations
