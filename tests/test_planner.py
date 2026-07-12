"""Unit tests for deterministic planner helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner import planner as planner_module
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    HAEOStatus,
    InputHealth,
    OccupancyState,
    PlanAction,
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


def test_dry_run_plan_has_candidate_actions_without_active_control() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    context = _context()
    context.occupancy_state = OccupancyState.AWAY

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.mode == PlannerMode.DRY_RUN
    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.status == "current"
    assert plan.confidence == 1.0
    assert plan.decision_audit["accepted"][0]["device"] == "Climate"
    assert plan.rejected_actions


def test_plan_preview_includes_weather_forecast_temperature() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    plan = DryRunPlanner(options).create_plan(_context())

    assert plan.preview[0]["outdoor_temperature_forecast_c"] == 18.5


def test_plan_preview_exposes_uncertainty_and_carbon_inputs() -> None:
    context = _context()
    context.slots[0].pv_forecast_lower_kw = 0.7
    context.slots[0].baseline_load_forecast_upper_kw = 2.4
    context.slots[0].carbon_intensity_g_per_kwh = 325

    plan = DryRunPlanner({**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}).create_plan(context)

    assert plan.preview[0]["pv_forecast_lower_kw"] == 0.7
    assert plan.preview[0]["baseline_load_forecast_upper_kw"] == 2.4
    assert plan.preview[0]["carbon_intensity_g_per_kwh"] == 325


def test_carbon_objective_scores_low_carbon_ev_schedule_and_changes_weight_by_priority() -> None:
    context = _context()
    context.slots = [
        DecisionSlot(context.created_at, 0.1, 0.05, 0.0, 1.0, carbon_intensity_g_per_kwh=800),
        DecisionSlot(
            context.created_at + timedelta(minutes=5),
            0.2,
            0.05,
            0.0,
            1.0,
            carbon_intensity_g_per_kwh=100,
        ),
    ]
    action = PlanAction(
        action_id="ev-carbon",
        plan_id=context.plan_id,
        execute_not_before=context.created_at,
        execute_not_after=context.created_at + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "required_charge_percent": 10,
            "allocated_slots": [
                {"carbon_intensity_g_per_kwh": 100, "grid_import_used_kw": 5.0}
            ],
        },
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )

    components = planner_module._score_components(action, context)
    carbon_first = {
        **DEFAULT_OPTIONS,
        "priority_weights": "carbon,cost,comfort,ev_readiness,battery_reserve,solar_self_consumption",
    }

    assert components["carbon"] == 1.0
    assert planner_module._carbon_schedule_weight(carbon_first) > planner_module._carbon_schedule_weight(
        DEFAULT_OPTIONS
    )


def test_carbon_score_rewards_load_reduction_during_high_carbon_period() -> None:
    context = _context()
    context.slots = [
        DecisionSlot(context.created_at, 0.2, 0.05, 0.0, 2.0, carbon_intensity_g_per_kwh=800),
        DecisionSlot(
            context.created_at + timedelta(minutes=5),
            0.2,
            0.05,
            0.0,
            2.0,
            carbon_intensity_g_per_kwh=100,
        ),
    ]

    def climate_action(mode: str) -> PlanAction:
        return PlanAction(
            action_id=f"climate-{mode}",
            plan_id=context.plan_id,
            execute_not_before=context.created_at,
            execute_not_after=context.created_at + timedelta(minutes=5),
            asset=ActionAsset.DAIKIN,
            kind=ActionKind.SET_HVAC,
            desired_state={"mode": mode},
            hard_constraints=[],
            reason_codes=[],
            expected_cost_delta=None,
            confidence=1.0,
            requires_haeo_plan_id=None,
        )

    assert planner_module._carbon_action_score(climate_action("off"), context) == 1.0
    assert planner_module._carbon_action_score(climate_action("heat"), context) == 0.0


def test_estimated_cost_uses_configured_planning_interval() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
        "planning_interval_minutes": 15,
    }
    context = _context()
    context.current_ev_soc_percent = None
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
    context.current_ev_soc_percent = None
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


def test_estimated_cost_prefers_haeo_grid_flow_forecasts() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    context = _context()
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.30,
            export_price=0.10,
            pv_forecast_kw=8.0,
            baseline_load_forecast_kw=1.0,
            haeo_grid_import_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=0.0,
        ),
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=5),
            import_price=0.30,
            export_price=0.10,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=8.0,
            haeo_grid_import_forecast_kw=0.0,
            haeo_grid_export_forecast_kw=3.0,
        ),
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.estimated_daily_cost == 0.025


def test_estimated_cost_accounts_for_haeo_battery_power_without_grid_flow() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    context = _context()
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.24,
            export_price=0.06,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=1.0,
            haeo_battery_charge_forecast_kw=2.0,
        ),
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=5),
            import_price=0.24,
            export_price=0.06,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=2.0,
            haeo_battery_discharge_forecast_kw=2.0,
        ),
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.estimated_daily_cost == 0.04


def test_estimated_cost_falls_back_when_haeo_grid_flow_is_partial() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True}
    context = _context()
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at,
            import_price=0.24,
            export_price=0.06,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_import_forecast_kw=None,
            haeo_grid_export_forecast_kw=0.0,
        )
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.estimated_daily_cost == 0.04


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


def test_planner_mode_rejects_truthy_string_safety_options() -> None:
    context = _context()

    disabled = DryRunPlanner(
        {**DEFAULT_OPTIONS, "planner_enabled": "true", "dry_run": False}
    ).create_plan(context)
    dry_run = DryRunPlanner(
        {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": "false"}
    ).create_plan(context)

    assert disabled.mode == PlannerMode.DISABLED
    assert dry_run.mode == PlannerMode.DRY_RUN


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


def test_active_plan_exposes_solar_aware_ev_charge_allocation() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_min_soc_percent": 45,
        "default_ready_by": "00:15",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "ev_fallback_target_soc_percent": 45,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 40
    context.current_hvac_temperature_c = None
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=import_price,
            export_price=export_price,
            pv_forecast_kw=pv,
            baseline_load_forecast_kw=load,
        )
        for offset, import_price, export_price, pv, load in [
            (0, 0.10, 0.05, 0.0, 2.0),
            (5, 0.30, 0.02, 8.0, 2.0),
            (10, 0.12, 0.05, 0.0, 2.0),
        ]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    allocation = plan.actions[0].desired_state["allocated_slots"][0]
    assert plan.actions[0].reason_codes == [
        "ev_soc_below_target",
        "fallback_until_history_sufficient",
        "least_cost_solar_aware_slots_before_ready_by",
    ]
    assert allocation["valid_at"] == "2026-06-27T00:05:00+00:00"
    assert allocation["import_price"] == 0.3
    assert allocation["effective_price"] == 0.02
    assert allocation["solar_surplus_used_kw"] == 6
    assert allocation["grid_import_used_kw"] == 0
    assert [slot.projected_ev_load_kw for slot in context.slots] == [0.0, 6, 0.0]


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
    assert DryRunPlanner(DEFAULT_OPTIONS)._estimated_cost_horizon_hours(context) is None


def test_carbon_action_score_covers_allocation_and_asset_edges() -> None:
    context = _context()
    context.slots = [
        DecisionSlot(context.created_at, 0.2, 0.05, 0, 1, carbon_intensity_g_per_kwh=100),
        DecisionSlot(
            context.created_at + timedelta(minutes=5),
            0.2,
            0.05,
            0,
            1,
            carbon_intensity_g_per_kwh=500,
        ),
    ]

    def action(asset: ActionAsset, desired_state: dict[str, object]) -> PlanAction:
        return PlanAction(
            action_id="carbon-test",
            plan_id=context.plan_id,
            execute_not_before=context.created_at,
            execute_not_after=context.created_at + timedelta(minutes=5),
            asset=asset,
            kind=ActionKind.EV_SCHEDULE if asset == ActionAsset.EV else ActionKind.SET_PROFILE,
            desired_state=desired_state,
            hard_constraints=[],
            reason_codes=[],
            expected_cost_delta=None,
            confidence=1.0,
            requires_haeo_plan_id=None,
        )

    assert planner_module._carbon_action_score(action(ActionAsset.EV, {}), context) == 0.0
    assert (
        planner_module._carbon_action_score(
            action(
                ActionAsset.EV,
                {
                    "allocated_slots": [
                        {
                            "carbon_intensity_g_per_kwh": 100,
                            "grid_import_used_kw": 0,
                        }
                    ]
                },
            ),
            context,
        )
        == 1.0
    )
    assert (
        planner_module._carbon_action_score(
            action(
                ActionAsset.EV,
                {
                    "allocated_slots": [
                        {
                            "carbon_intensity_g_per_kwh": 300,
                            "grid_import_used_kw": 2,
                        }
                    ]
                },
            ),
            context,
        )
        == 0.5
    )

    context.slots.append(
        DecisionSlot(
            context.created_at + timedelta(minutes=10),
            0.2,
            0.05,
            0,
            1,
            carbon_intensity_g_per_kwh=100,
        )
    )
    context.slots[0].carbon_intensity_g_per_kwh = None
    assert planner_module._carbon_action_score(action(ActionAsset.ENPHASE, {}), context) == 0.0
    context.slots[0].carbon_intensity_g_per_kwh = 500
    context.slots[1].carbon_intensity_g_per_kwh = 100
    assert (
        planner_module._carbon_action_score(
            action(ActionAsset.ENPHASE, {"arbitrage_direction": "consume"}), context
        )
        == 1.0
    )
    assert planner_module._carbon_action_score(action(ActionAsset.ENPHASE, {}), context) == 0.0


def test_planner_small_helpers_cover_invalid_ready_by_and_empty_prices() -> None:
    now = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    context = _context()
    context.slots = [DecisionSlot(now, None, None, None, None)]

    assert _next_ready_by(now, "bad") == datetime(2026, 6, 28, 7, 0, tzinfo=UTC)
    assert _haeo_battery_arbitrage_value(context, 5) is None
    assert _arbitrage_spread(context) == 0.0
    assert planner_module._forecast_solar_export_value(context, 5) is None
    context.slots = [DecisionSlot(now, 0.25, 0.50, None, None)]
    assert _arbitrage_spread(context) == 0.25
    context.input_health = InputHealth.DEGRADED
    assert DryRunPlanner(DEFAULT_OPTIONS)._confidence(context) == 0.65
    assert planner_module._display_text("   ") == "Unknown"


def test_next_ready_by_uses_melbourne_standard_and_daylight_offsets() -> None:
    winter_now = datetime(2026, 7, 11, 20, 30, tzinfo=UTC)  # 06:30 local (+10)
    summer_now = datetime(2026, 1, 11, 19, 30, tzinfo=UTC)  # 06:30 local (+11)

    assert _next_ready_by(winter_now, "07:00", "Australia/Melbourne") == datetime(
        2026, 7, 11, 21, 0, tzinfo=UTC
    )
    assert _next_ready_by(summer_now, "07:00", "Australia/Melbourne") == datetime(
        2026, 1, 11, 20, 0, tzinfo=UTC
    )


def test_ev_schedule_preserves_absolute_ready_by_timestamp() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
        "default_ready_by": "07:00",
        "ev_fallback_target_soc_percent": 70,
    }
    context = _context()
    context.created_at = datetime(2026, 7, 11, 20, 30, tzinfo=UTC)
    context.local_timezone = "Australia/Melbourne"
    context.current_ev_soc_percent = 60
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=1.0,
        )
        for offset in range(0, 60, 5)
    ]

    plan = DryRunPlanner(options).create_plan(context)
    ev_action = next(action for action in plan.actions if action.asset == ActionAsset.EV)

    assert ev_action.desired_state["ready_by_utc"] == "2026-07-11T21:00:00+00:00"
    assert ev_action.desired_state["ready_by_timezone"] == "Australia/Melbourne"


def test_next_ready_by_rolls_over_in_local_calendar_and_handles_dst_gap() -> None:
    after_ready = datetime(2026, 7, 11, 22, 0, tzinfo=UTC)  # 08:00 local
    before_spring_gap = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 01:00 local

    assert _next_ready_by(after_ready, "07:00", "Australia/Melbourne") == datetime(
        2026, 7, 12, 21, 0, tzinfo=UTC
    )
    # 02:30 does not exist on this date, so the wall-clock deadline advances to 03:00.
    assert _next_ready_by(before_spring_gap, "02:30", "Australia/Melbourne") == datetime(
        2026, 10, 3, 16, 0, tzinfo=UTC
    )
    assert _next_ready_by(after_ready, "07:00", "Invalid/Timezone") == datetime(
        2026, 7, 12, 7, 0, tzinfo=UTC
    )


def test_estimated_cost_reports_its_non_daily_horizon() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planning_horizon_hours": 6,
        "planning_interval_minutes": 15,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.30,
            export_price=0.05,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=1.0,
        )
        for offset in range(0, 6 * 60, 15)
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.horizon_hours == 6
    assert plan.estimated_daily_cost == 1.8
    assert plan.estimated_cost_horizon_hours == 6


def test_planner_new_decision_helpers_cover_confidence_and_budget_edges() -> None:
    context = _context()
    context.input_issues = ["pv_forecast_entity_unavailable", "ev_soc_entity_unavailable"]
    assert planner_module._subsystem_confidence(1.0, "pv_forecast_entity_unavailable", ("pv_forecast",)) == 0.4
    assert planner_module._battery_reserve_score(context) == 0.1
    context.current_battery_soc_percent = 35
    assert planner_module._battery_reserve_score(context) == 0.5
    context.current_battery_soc_percent = 15
    assert planner_module._battery_reserve_score(context) == 1.0
    context.current_battery_soc_percent = None
    assert planner_module._battery_reserve_score(context) == 0.0

    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = []
    rejected = planner_module._rejected_climate_decision(context, DEFAULT_OPTIONS, {})
    assert rejected["reason"] == "Skipped comfort preconditioning because no tariff forecast slots are available."
    assert planner_module._timeline_card_rows({"bad": "value"}) == []

    context.slots = [DecisionSlot(context.created_at, 0.2, 0.05, None, 1.0)]
    assert planner_module._forecast_surplus_kwh(context, 5) == 0.0

    action = PlanAction(
        action_id="ev",
        plan_id=context.plan_id,
        execute_not_before=context.created_at,
        execute_not_after=context.created_at,
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    assert not planner_module._action_meets_confidence_threshold(
        action,
        context,
        {**DEFAULT_OPTIONS, "minimum_ev_confidence": 90.0},
    )


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


def test_dry_run_plan_uses_ev_target_and_ready_by_helpers_for_schedule() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
        "ev_min_soc_percent": 40,
        "ev_max_soc_percent": 90,
        "ev_fallback_target_soc_percent": 70,
        "default_ready_by": "07:00",
        "ev_charge_rate_kw": 7,
        "ev_soc_per_kwh": 5,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.current_ev_soc_percent = 72
    context.ev_connected = True
    context.ev_target_soc_percent = 80
    context.ev_ready_by = "08:00"
    context.ev_trip_history_sufficient = True
    context.ev_trip_max_daily_soc_percent = 5
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.01), (15, 0.20)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.mode == PlannerMode.DRY_RUN
    assert plan.actions[0].asset == ActionAsset.EV
    assert plan.actions[0].desired_state["target_soc_percent"] == 80
    assert plan.actions[0].desired_state["configured_target_soc_percent"] == 80
    assert plan.actions[0].desired_state["ready_by"] == "08:00"
    assert plan.actions[0].desired_state["required_charge_percent"] == 8
    assert any(entry["state"] == "charging" for entry in plan.device_plans["ev"]["timeline"])


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


def test_active_plan_sets_enphase_arbitrage_profile_when_forecast_solar_export_value_exceeds_threshold() -> None:
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
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=import_price,
            export_price=export_price,
            pv_forecast_kw=pv_forecast_kw,
            baseline_load_forecast_kw=2.0,
        )
        for offset, import_price, export_price, pv_forecast_kw in [
            (0, 0.05, 0.20, 4.0),
            (30, 0.15, 0.20, 3.0),
        ]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].kind == ActionKind.SET_PROFILE
    assert plan.actions[0].desired_state["profile"] == "Self-Consumption"
    assert plan.actions[0].desired_state["arbitrage_source"] == "forecast_solar_export_value"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "consume"
    assert plan.actions[0].expected_cost_delta == 0.27
    assert plan.actions[0].desired_state["arbitrage_details"]["accepted_surplus_kwh"] == 1.5
    assert plan.actions[0].desired_state["arbitrage_details"]["battery_round_trip_efficiency"] == 0.9
    assert plan.actions[0].requires_haeo_plan_id == context.plan_id
    assert plan.device_plans["enphase"]["current_state"] == {
        "state": "AI Optimisation",
        "profile": "AI Optimisation",
        "ai_profile": "AI Optimisation",
        "self_consumption_profile": "Self-Consumption",
        "full_backup_profile": "Full Backup",
    }
    assert plan.device_plans["enphase"]["current_state_label"] == "AI Optimisation"
    assert plan.device_plans["enphase"]["next_planned_state"] == {
        "state": "set_profile",
        "action": "set_profile",
        "execute_not_before": plan.actions[0].execute_not_before.isoformat(),
        "execute_not_after": plan.actions[0].execute_not_after.isoformat(),
        "reason_codes": ["enphase_forecast_solar_export_value_above_threshold"],
        "profile": "Self-Consumption",
        "arbitrage_direction": "consume",
        "arbitrage_source": "forecast_solar_export_value",
        "arbitrage_value": 0.27,
    }
    assert plan.device_plans["enphase"]["next_planned_state_label"] == "Set Profile: Self-Consumption"


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
    assert plan.actions[0].desired_state["arbitrage_source"] == "insufficient_arbitrage_evidence"
    assert plan.actions[0].reason_codes == ["enphase_insufficient_arbitrage_evidence_below_threshold"]
    assert plan.actions[0].requires_haeo_plan_id is None
    assert plan.device_plans["enphase"]["current_state_label"] == "Self-Consumption"
    assert plan.device_plans["enphase"]["next_planned_state_label"] == "Restore AI: AI Optimisation"


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


def test_hvac_suppression_uses_two_hour_duration_at_non_default_interval() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 30,
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
        for offset, price in [(0, 0.60), (30, 0.55), (60, 0.55), (90, 0.55), (120, 0.10)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


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


def test_active_plan_thermal_shifts_heat_during_low_tariff_period() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {"sample_count": 12, "average": 2.0},
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 2.0},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -0.5},
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21.0
    context.current_outdoor_temperature_c = 5.0
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
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
    assert plan.actions[0].desired_state["target_temperature"] == 23.0
    assert plan.actions[0].desired_state["thermal_shift"] is True
    assert plan.actions[0].desired_state["comfort_coast_boundary"] == 19.0
    assert plan.actions[0].desired_state["estimated_coast_hours"] == 8.0
    assert plan.actions[0].desired_state["active_heat_rate_c_per_hour"] == 2.0
    assert plan.actions[0].desired_state["precondition_slot_count"] == 3
    assert "hvac_thermal_shift_before_expensive_period" in plan.actions[0].reason_codes
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [2.0, 2.0, 2.0, 0.0]
    assert plan.device_plans["climate"]["next_planned_state_label"] == "Set HVAC: Heat to 23.0 C"


def test_active_plan_skips_thermal_shift_when_coast_time_is_too_short() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 120,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {"sample_count": 12, "average": 2.0},
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 2.0},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -5.0},
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21.0
    context.current_outdoor_temperature_c = 5.0
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for offset, price in [(0, 0.10), (30, 0.12), (60, 0.45)]
    ]

    plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


def test_thermal_shift_helpers_cover_defensive_branches() -> None:
    context = _context()
    context.current_hvac_mode = None
    context.current_hvac_temperature_c = None
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    peak = DecisionSlot(
        valid_at=context.created_at + timedelta(minutes=30),
        import_price=0.50,
        export_price=0.05,
        pv_forecast_kw=1.0,
        baseline_load_forecast_kw=2.0,
    )

    assert planner_module._thermal_shift_target(context, peak, 5, {}) is None

    context.current_hvac_temperature_c = 18
    assert planner_module._thermal_shift_target(context, peak, 5, {}) is None

    context.current_hvac_temperature_c = 22.8
    context.current_hvac_mode = "heat"
    assert planner_module._thermal_shift_target(context, peak, 5, {}) is None

    context.current_hvac_mode = None
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
    assert planner_module._thermal_shift_mode(context, 21, 19, 23) == "heat"
    context.current_outdoor_temperature_c = 30
    assert planner_module._thermal_shift_mode(context, 21, 19, 23) == "cool"
    context.current_outdoor_temperature_c = None
    assert planner_module._thermal_shift_mode(context, 20, 19, 23) == "heat"
    assert planner_module._thermal_shift_mode(context, 22, 19, 23) == "cool"

    assert planner_module._effective_passive_drift_c_per_hour(context, "heat", {}) is None
    context.current_outdoor_temperature_c = 5
    assert planner_module._effective_passive_drift_c_per_hour(context, "heat", {}) == -0.5
    context.current_outdoor_temperature_c = 30
    assert planner_module._effective_passive_drift_c_per_hour(context, "cool", {}) == 0.5
    context.current_outdoor_temperature_c = 21
    assert planner_module._effective_passive_drift_c_per_hour(context, "cool", {}) is None

    assert (
        planner_module._effective_passive_drift_c_per_hour(
            context,
            "heat",
            {"passive_indoor_drift_c_per_hour": {"average": -0.25}},
        )
        == -0.25
    )
    assert planner_module._thermal_coast_hours(
        mode="heat",
        target_temperature=23,
        comfort_boundary=19,
        passive_drift_c_per_hour=None,
    ) is None
    assert planner_module._thermal_coast_hours(
        mode="cool",
        target_temperature=19,
        comfort_boundary=23,
        passive_drift_c_per_hour=0.5,
    ) == 8
    assert planner_module._thermal_coast_hours(
        mode="heat",
        target_temperature=23,
        comfort_boundary=19,
        passive_drift_c_per_hour=0.5,
    ) is None
    assert planner_module._precondition_slot_count(
        current_temperature=21,
        target_temperature=23,
        mode="heat",
        interval_minutes=5,
        max_slots=0,
        thermal_model={},
    ) == 0
    assert planner_module._precondition_slot_count(
        current_temperature=21,
        target_temperature=23,
        mode="heat",
        interval_minutes=30,
        max_slots=4,
        thermal_model={},
    ) == 4
    assert planner_module._precondition_slot_count(
        current_temperature=21,
        target_temperature=22,
        mode="heat",
        interval_minutes=30,
        max_slots=4,
        thermal_model={
            "enabled": True,
            "active_heat_rate_c_per_hour": {"sample_count": 3, "average": 1.0},
        },
    ) == 2


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
                "hvac_mode": "heat",
                "indoor_temperature_c": 17.2 + index * 0.03,
                "outdoor_temperature_c": 5.0,
                "hvac_power_kw": 2.2,
            },
            {
                "sampled_at": sample_start + timedelta(minutes=5 * (index + 1)),
                "hvac_mode": "heat",
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
                "hvac_mode": "cool",
                "indoor_temperature_c": 25.2 - index * 0.02,
                "outdoor_temperature_c": 34.0,
                "hvac_power_kw": 1.6,
            },
            {
                "sampled_at": sample_start + timedelta(minutes=5 * (index + 1)),
                "hvac_mode": "cool",
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


def test_hvac_precondition_lead_window_does_not_include_partial_next_slot() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 15,
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
        for offset, price in [(0, 0.10), (15, 0.45), (30, 0.50)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


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
            export_price=0.20,
            pv_forecast_kw=4.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_import_forecast_kw=float("nan"),
            haeo_battery_charge_forecast_kw=float("nan"),
        ),
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=30),
            import_price=0.15,
            export_price=0.20,
            pv_forecast_kw=3.0,
            baseline_load_forecast_kw=2.0,
            haeo_grid_export_forecast_kw=float("inf"),
            haeo_battery_discharge_forecast_kw=float("-inf"),
        ),
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].desired_state["profile"] == "Self-Consumption"
    assert plan.actions[0].desired_state["arbitrage_source"] == "forecast_solar_export_value"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "consume"
    assert plan.actions[0].expected_cost_delta == 0.27


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
    assert plan.actions[0].desired_state["arbitrage_source"] == "insufficient_arbitrage_evidence"
    assert plan.actions[0].reason_codes == ["enphase_insufficient_arbitrage_evidence_below_threshold"]
    assert plan.actions[0].expected_cost_delta == 0.0
