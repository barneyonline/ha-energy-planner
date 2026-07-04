"""Unit tests for deterministic planner helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    HAEOStatus,
    InputHealth,
    OccupancyState,
    PlannerMode,
)
from custom_components.ha_energy_planner.planner import (
    DryRunPlanner,
    _arbitrage_spread,
    _haeo_battery_arbitrage_value,
    _next_ready_by,
)
from custom_components.ha_energy_planner.thermal_model import update_thermal_model


def _context(health: InputHealth = InputHealth.HEALTHY) -> DecisionContext:
    now = datetime.now(UTC)
    return DecisionContext(
        created_at=now,
        plan_id="plan-1",
        slots=[
            DecisionSlot(
                valid_at=now + timedelta(minutes=offset),
                import_price=0.20,
                export_price=0.05,
                pv_forecast_kw=1.0,
                baseline_load_forecast_kw=2.0,
                outdoor_temperature_forecast_c=18.5,
            )
            for offset in range(0, 24 * 60, 5)
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=60,
        occupancy_state=OccupancyState.OCCUPIED,
        haeo_status=HAEOStatus.READY,
        input_health=health,
    )


def test_dry_run_plan_has_no_actions() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    plan = DryRunPlanner(options).create_plan(_context())
    assert plan.mode == PlannerMode.DRY_RUN
    assert plan.actions == []
    assert plan.status == "current"
    assert plan.confidence == 1.0


def test_plan_preview_includes_weather_forecast_temperature() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    plan = DryRunPlanner(options).create_plan(_context())

    assert plan.preview[0]["outdoor_temperature_forecast_c"] == 18.5


def test_estimated_cost_uses_configured_planning_interval() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
        "planning_interval_minutes": 15,
    }
    context = _context()
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.30,
            export_price=0.05,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in (0, 15, 30, 45)
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.estimated_daily_cost == 0.6


def test_estimated_cost_subtracts_export_credit_for_surplus_solar() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    context = _context()
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.18,
            export_price=0.07,
            pv_forecast_kw=3.2,
            baseline_load_forecast_kw=1.35,
        )
        for offset in range(0, 24 * 60, 5)
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.estimated_daily_cost == -3.108


def test_estimated_cost_includes_projected_flexible_load() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_min_soc_percent": 70,
        "default_ready_by": "00:10",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "ev_fallback_target_soc_percent": 70,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 60
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in (0, 5, 10)
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert [slot.projected_ev_load_kw for slot in context.slots] == [6, 6, 0.0]
    assert plan.estimated_daily_cost == 0.25


def test_unsafe_context_suppresses_plan() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    plan = DryRunPlanner(options).create_plan(_context(InputHealth.UNSAFE))
    assert plan.mode == PlannerMode.ACTIVE_DEGRADED
    assert plan.status == "unsafe"
    assert plan.confidence == 0.0


def test_disabled_planner_suppresses_actions_and_marks_disabled() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": False, "dry_run": False}
    context = _context()
    context.occupancy_state = OccupancyState.AWAY

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.mode == PlannerMode.DISABLED
    assert plan.actions == []


def test_plan_confidence_is_capped_by_forecast_confidence() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.occupancy_state = OccupancyState.AWAY
    context.forecast_confidence = 0.62

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.confidence == 0.62
    assert plan.actions[0].confidence == 0.62


def test_active_plan_turns_hvac_off_when_away() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.occupancy_state = OccupancyState.AWAY
    plan = DryRunPlanner(options).create_plan(context)
    assert plan.mode == PlannerMode.ACTIVE_HEALTHY
    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.actions[0].desired_state == {"hvac_mode": "off"}


def test_active_plan_schedules_ev_when_below_minimum_soc() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "ev_min_soc_percent": 70}
    context = _context()
    context.current_ev_soc_percent = 60
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.20), (15, 0.01)]
    ]
    options = {
        **options,
        "default_ready_by": "00:20",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "ev_fallback_target_soc_percent": 70,
        "planning_interval_minutes": 5,
    }
    plan = DryRunPlanner(options).create_plan(context)
    assert plan.actions[0].asset == ActionAsset.EV
    assert plan.actions[0].kind == ActionKind.EV_SCHEDULE
    assert plan.actions[0].desired_state["target_soc_percent"] == 70.0
    assert plan.actions[0].requires_haeo_plan_id == context.plan_id
    assert [slot.projected_ev_load_kw for slot in context.slots] == [0.0, 6, 0.0, 6]
    assert plan.device_plans["ev"]["total_estimated_energy_kwh"] == 1.0
    assert plan.device_plans["ev"]["timeline"] == [
        {
            "state": "idle",
            "start": "2026-06-27T00:00:00+00:00",
            "end": "2026-06-27T00:05:00+00:00",
        },
        {
            "state": "charging",
            "charge_kw": 6,
            "estimated_energy_kwh": 0.5,
            "reason_codes": [
                "ev_soc_below_target",
                "fallback_until_history_sufficient",
                "least_cost_slots_before_ready_by",
            ],
            "target_soc_percent": 70.0,
            "ready_by": "00:20",
            "infeasible": False,
            "start": "2026-06-27T00:05:00+00:00",
            "end": "2026-06-27T00:10:00+00:00",
        },
        {
            "state": "idle",
            "start": "2026-06-27T00:10:00+00:00",
            "end": "2026-06-27T00:15:00+00:00",
        },
        {
            "state": "charging",
            "charge_kw": 6,
            "estimated_energy_kwh": 0.5,
            "reason_codes": [
                "ev_soc_below_target",
                "fallback_until_history_sufficient",
                "least_cost_slots_before_ready_by",
            ],
            "target_soc_percent": 70.0,
            "ready_by": "00:20",
            "infeasible": False,
            "start": "2026-06-27T00:15:00+00:00",
            "end": "2026-06-27T00:20:00+00:00",
        },
    ]


def test_ev_target_at_or_below_current_soc_does_not_create_charge_action() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "ev_min_soc_percent": 40}
    context = _context()
    context.current_ev_soc_percent = 80

    plan = DryRunPlanner(options).create_plan(context)

    assert [action for action in plan.actions if action.asset == ActionAsset.EV] == []


def test_hvac_suppression_and_preconditioning_guard_branches_return_no_action() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_suppression_min_price_delta": 0.5,
        "hvac_precondition_min_price_delta": 0.5,
        "hvac_precondition_lead_minutes": 0,
    }
    context = _context()
    context.current_hvac_temperature_c = None
    context.occupied_temperature_low_c = 20
    context.occupied_temperature_high_c = 24

    plan = DryRunPlanner(options).create_plan(context)

    assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


def test_hvac_preconditioning_returns_none_without_future_slots_or_current_price() -> None:
    planner = DryRunPlanner({**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False})
    context = _context()
    context.current_hvac_temperature_c = 17
    context.occupied_temperature_low_c = 20
    context.occupied_temperature_high_c = 24
    context.slots = [DecisionSlot(context.created_at, None, 0.05, 0, 1)]

    assert (
        planner._hvac_preconditioning_action(context, context.created_at, context.created_at + timedelta(minutes=5))
        is None
    )

    context.slots = [DecisionSlot(context.created_at, 0.10, 0.05, 0, 1)]
    assert (
        planner._hvac_preconditioning_action(context, context.created_at, context.created_at + timedelta(minutes=5))
        is None
    )


def test_estimated_cost_returns_none_when_slots_lack_required_data() -> None:
    context = _context()
    context.slots = [DecisionSlot(context.created_at, None, 0.05, 0, None)]

    assert DryRunPlanner(DEFAULT_OPTIONS)._estimate_cost(context) is None


def test_planner_small_helpers_cover_invalid_ready_by_and_empty_prices() -> None:
    now = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    context = _context()
    context.slots = [DecisionSlot(now, None, None, None, None)]

    assert _next_ready_by(now, "bad") == datetime(2026, 6, 28, 7, 0, tzinfo=UTC)
    assert _haeo_battery_arbitrage_value(context, 5) is None
    assert _arbitrage_spread(context) == 0.0
    context.input_health = InputHealth.DEGRADED
    assert DryRunPlanner(DEFAULT_OPTIONS)._confidence(context) == 0.65


def test_device_plans_include_climate_timeline() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21.5
    context.current_hvac_power_kw = 0.8
    context.current_outdoor_temperature_c = 12.0
    context.occupied_temperature_low_c = 20.0
    context.occupied_temperature_high_c = 24.0
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in (0, 5, 10)
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.device_plans["climate"]["total_estimated_energy_kwh"] is None
    assert plan.device_plans["climate"]["current_state"] == {
        "state": "heat",
        "hvac_mode": "heat",
        "current_temperature": 21.5,
        "current_power_kw": 0.8,
        "outdoor_temperature": 12.0,
        "occupied_temperature_low": 20.0,
        "occupied_temperature_high": 24.0,
        "occupancy": "away",
    }
    assert plan.device_plans["climate"]["current_state_label"] == "Heat (21.5 C)"
    assert plan.device_plans["climate"]["next_planned_state"] == {
        "state": "off",
        "action": "set_hvac",
        "execute_not_before": "2026-06-27T00:00:00+00:00",
        "execute_not_after": "2026-06-27T00:05:00+00:00",
        "reason_codes": ["away_hvac_policy"],
        "hvac_mode": "off",
    }
    assert plan.device_plans["climate"]["next_planned_state_label"] == "Off"
    assert plan.device_plans["climate"]["timeline"] == [
        {
            "state": "off",
            "action": "set_hvac",
            "reason_codes": ["away_hvac_policy"],
            "hvac_mode": "off",
            "start": "2026-06-27T00:00:00+00:00",
            "end": "2026-06-27T00:05:00+00:00",
        },
        {
            "state": "idle",
            "start": "2026-06-27T00:05:00+00:00",
            "end": "2026-06-27T00:15:00+00:00",
        },
    ]


def test_device_plans_include_enphase_haeo_timeline() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    context = _context()
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_battery_charge_forecast_kw=charge,
            haeo_battery_discharge_forecast_kw=discharge,
            haeo_battery_soc_forecast_percent=soc,
        )
        for offset, charge, discharge, soc in (
            (0, 2.0, 0.0, 50),
            (5, 2.0, 0.0, 51),
            (10, 0.0, 1.5, 50),
        )
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.device_plans["enphase"]["total_estimated_battery_charge_kwh"] == 0.3334
    assert plan.device_plans["enphase"]["total_estimated_battery_discharge_kwh"] == 0.125
    assert plan.device_plans["enphase"]["timeline"] == [
        {
            "state": "charge_battery",
            "battery_charge_kw": 2.0,
            "estimated_battery_charge_kwh": 0.1667,
            "battery_soc_percent": 50,
            "start": "2026-06-27T00:00:00+00:00",
            "end": "2026-06-27T00:05:00+00:00",
        },
        {
            "state": "charge_battery",
            "battery_charge_kw": 2.0,
            "estimated_battery_charge_kwh": 0.1667,
            "battery_soc_percent": 51,
            "start": "2026-06-27T00:05:00+00:00",
            "end": "2026-06-27T00:10:00+00:00",
        },
        {
            "state": "consume_battery",
            "battery_discharge_kw": 1.5,
            "estimated_battery_discharge_kwh": 0.125,
            "battery_soc_percent": 50,
            "start": "2026-06-27T00:10:00+00:00",
            "end": "2026-06-27T00:15:00+00:00",
        },
    ]


def test_active_plan_uses_runtime_ready_by_option_for_ev_schedule() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_min_soc_percent": 70,
        "default_ready_by": "00:10",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "ev_fallback_target_soc_percent": 70,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 60
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.01), (15, 0.01)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].desired_state["ready_by"] == "00:10"
    assert [slot.projected_ev_load_kw for slot in context.slots] == [6, 6, 0.0, 0.0]
    assert plan.actions[0].desired_state["infeasible"] is False


def test_active_plan_does_not_schedule_ev_when_disconnected() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "ev_min_soc_percent": 70}
    context = _context()
    context.current_ev_soc_percent = 60
    context.ev_connected = False

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions == []


def test_active_plan_uses_trip_history_for_ev_target() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_min_soc_percent": 40,
        "ev_max_soc_percent": 90,
        "ev_fallback_target_soc_percent": 80,
        "default_ready_by": "03:00",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 50
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in range(0, 3 * 60, 5)
    ]
    context.ev_trip_observed_days = 3
    context.ev_trip_max_daily_soc_percent = 15
    context.ev_trip_average_daily_soc_percent = 10
    context.ev_trip_history_sufficient = True

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.EV
    assert plan.actions[0].desired_state["target_soc_percent"] == 55.0
    assert plan.actions[0].desired_state["trip_history_sufficient"] is True
    assert "history_max_daily_consumption" in plan.actions[0].reason_codes


def test_active_plan_sets_enphase_arbitrage_profile_when_spread_exceeds_threshold() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
    }
    context = _context()
    context.current_enphase_profile = "AI Optimisation"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=import_price,
            export_price=export_price,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, import_price, export_price in [(0, 0.05, 0.08), (5, 0.15, 0.42)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].kind == ActionKind.SET_PROFILE
    assert plan.actions[0].desired_state["profile"] == "Self-Consumption"
    assert plan.actions[0].desired_state["arbitrage_source"] == "price_spread"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "consume"
    assert plan.actions[0].expected_cost_delta == 0.37
    assert plan.actions[0].requires_haeo_plan_id == context.plan_id


def test_active_plan_restores_enphase_ai_when_arbitrage_spread_below_threshold() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
    }
    context = _context()
    context.current_enphase_profile = "Self-Consumption"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].kind == ActionKind.RESTORE_AI
    assert plan.actions[0].desired_state["profile"] == "AI Optimisation"
    assert plan.actions[0].requires_haeo_plan_id is None


def test_active_plan_suppresses_hvac_automation_during_expensive_period_when_comfort_valid() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_suppression_min_price_delta": 0.20,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.60), (5, 0.55), (10, 0.20)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.actions[0].desired_state["suppress_automations"] is True
    assert plan.actions[0].expected_cost_delta == 0.4


def test_active_plan_does_not_suppress_hvac_when_comfort_not_valid() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_suppression_min_price_delta": 0.20,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 30
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots[0].import_price = 0.60
    context.slots[1].import_price = 0.20

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions == []


def test_active_plan_preconditions_hvac_before_expensive_period_when_near_comfort_bound() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 17.5
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.12), (10, 0.15), (15, 0.45)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.actions[0].desired_state["hvac_mode"] == "heat"
    assert plan.actions[0].desired_state["target_temperature"] == 18.0
    assert plan.actions[0].expected_cost_delta == 0.35
    assert "hvac_precondition_before_expensive_period" in plan.actions[0].reason_codes
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [1.0, 1.0, 1.0, 0.0]


def test_active_plan_uses_thermal_model_for_hvac_precondition_projection() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {
            "sample_count": 12,
            "average": 1.8,
        },
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 17.5
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.12), (10, 0.15), (15, 0.45)]
    ]

    plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    assert plan.actions[0].desired_state["projected_hvac_load_kw"] == 1.8
    assert plan.actions[0].desired_state["thermal_model_enabled"] is True
    assert plan.actions[0].desired_state["thermal_model_sample_count"] == 12
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [1.8, 1.8, 1.8, 0.0]
    assert plan.device_plans["climate"]["total_estimated_energy_kwh"] == 0.45
    assert plan.device_plans["climate"]["timeline"][0]["estimated_energy_kwh"] == 0.15


def test_active_plan_uses_replayed_cold_thermal_samples_for_heat_preconditioning() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model: dict[str, object] = {}
    sample_start = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    for index in range(12):
        thermal_model, _changed = update_thermal_model(
            thermal_model,
            {
                "sampled_at": sample_start + timedelta(minutes=5 * index),
                "indoor_temperature_c": 17.2 + index * 0.03,
                "outdoor_temperature_c": 5.0,
                "hvac_power_kw": 2.2,
            },
            {
                "sampled_at": sample_start + timedelta(minutes=5 * (index + 1)),
                "indoor_temperature_c": 17.4 + index * 0.03,
                "outdoor_temperature_c": 5.2,
                "hvac_power_kw": 2.1,
            },
        )
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 17.4
    context.current_outdoor_temperature_c = 5.2
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for offset, price in [(0, 0.10), (5, 0.12), (10, 0.15), (15, 0.45)]
    ]

    plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].desired_state["hvac_mode"] == "heat"
    assert plan.actions[0].desired_state["projected_hvac_load_kw"] == 2.2
    assert plan.actions[0].desired_state["thermal_model_enabled"] is True
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [2.2, 2.2, 2.2, 0.0]


def test_active_plan_uses_replayed_warm_thermal_samples_for_cool_preconditioning() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model: dict[str, object] = {}
    sample_start = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    for index in range(12):
        thermal_model, _changed = update_thermal_model(
            thermal_model,
            {
                "sampled_at": sample_start + timedelta(minutes=5 * index),
                "indoor_temperature_c": 25.2 - index * 0.02,
                "outdoor_temperature_c": 34.0,
                "hvac_power_kw": 1.6,
            },
            {
                "sampled_at": sample_start + timedelta(minutes=5 * (index + 1)),
                "indoor_temperature_c": 25.0 - index * 0.02,
                "outdoor_temperature_c": 33.5,
                "hvac_power_kw": 1.7,
            },
        )
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 24.6
    context.current_outdoor_temperature_c = 33.5
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=34.0,
        )
        for offset, price in [(0, 0.10), (5, 0.12), (10, 0.15), (15, 0.45)]
    ]

    plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].desired_state["hvac_mode"] == "cool"
    assert plan.actions[0].desired_state["target_temperature"] == 24.0
    assert plan.actions[0].desired_state["projected_hvac_load_kw"] == 1.6
    assert plan.actions[0].desired_state["thermal_model_enabled"] is True
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [1.6, 1.6, 1.6, 0.0]


def test_active_plan_does_not_precondition_hvac_outside_lead_window() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 10,
        "hvac_precondition_min_price_delta": 0.20,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 17.5
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.12), (10, 0.15), (15, 0.45)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions == []


def test_active_plan_prefers_haeo_export_value_for_enphase_arbitrage() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
        "planning_interval_minutes": 30,
    }
    context = _context()
    context.current_enphase_profile = "AI Optimisation"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.05,
            export_price=0.40,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=2.0,
        ),
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=30),
            import_price=0.05,
            export_price=0.35,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=1.0,
        ),
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].desired_state["profile"] == "Self-Consumption"
    assert plan.actions[0].desired_state["arbitrage_source"] == "haeo_export_value"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "consume"
    assert plan.actions[0].expected_cost_delta == 0.575


def test_active_plan_prefers_haeo_battery_arbitrage_value_for_enphase() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
        "planning_interval_minutes": 30,
    }
    context = _context()
    context.current_enphase_profile = "AI Optimisation"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.10,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_import_forecast_kw=2.0,
            haeo_battery_charge_forecast_kw=2.0,
        ),
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=30),
            import_price=0.50,
            export_price=0.40,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=2.0,
            haeo_battery_discharge_forecast_kw=2.0,
        ),
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].desired_state["profile"] == "Full Backup"
    assert plan.actions[0].desired_state["arbitrage_source"] == "haeo_battery_arbitrage_value"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "charge"
    assert plan.actions[0].expected_cost_delta == 0.3


def test_enphase_arbitrage_ignores_non_finite_direct_haeo_evidence() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
        "planning_interval_minutes": 30,
    }
    context = _context()
    context.current_enphase_profile = "AI Optimisation"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.05,
            export_price=0.08,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_import_forecast_kw=float("nan"),
            haeo_battery_charge_forecast_kw=float("nan"),
        ),
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=30),
            import_price=0.15,
            export_price=0.42,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=float("inf"),
            haeo_battery_discharge_forecast_kw=float("-inf"),
        ),
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].desired_state["profile"] == "Self-Consumption"
    assert plan.actions[0].desired_state["arbitrage_source"] == "price_spread"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "consume"
    assert plan.actions[0].expected_cost_delta == 0.37


def test_enphase_restore_not_suppressed_by_non_finite_direct_haeo_evidence() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "enphase_minimum_savings": 0.25,
        "planning_interval_minutes": 30,
    }
    context = _context()
    context.current_enphase_profile = "Self-Consumption"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.10,
            export_price=0.12,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=float("nan"),
            haeo_battery_discharge_forecast_kw=float("nan"),
        )
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].kind == ActionKind.RESTORE_AI
    assert plan.actions[0].desired_state["arbitrage_source"] == "price_spread"
    assert plan.actions[0].expected_cost_delta == 0.0
