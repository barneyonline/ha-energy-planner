"""Unit tests for deterministic planner helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ha_energy_planner import planner as planner_module
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED,
    CONF_HOUSEHOLD_LOAD,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    CONF_PV_FORECAST,
    CONF_WEATHER,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DaylightWindow,
    DecisionContext,
    DecisionSlot,
    InputHealth,
    OccupancyState,
    Override,
    PlanAction,
    PlannerMode,
)
from custom_components.ha_energy_planner.planner import (
    DryRunPlanner,
    _arbitrage_spread,
    _ev_earliest_start,
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
        ev_target_soc_percent=80,
        occupancy_state=OccupancyState.OCCUPIED,
        input_health=health,
    )


def _daylight_ev_context() -> DecisionContext:
    """Return a complete daytime tariff horizon before the next ready-by."""
    context = _context()
    context.created_at = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    context.current_ev_soc_percent = 40
    context.ev_target_soc_percent = 50
    context.current_hvac_temperature_c = None
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.30,
            export_price=0.05,
            pv_forecast_kw=0.0,
            baseline_load_forecast_kw=1.0,
        )
        for offset in range(0, 21 * 60, 5)
    ]
    context.daylight_windows = [
        DaylightWindow(
            start=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
            end=datetime(2026, 6, 27, 17, 0, tzinfo=UTC),
        )
    ]
    return context


def test_daylight_lowest_cost_selects_cheapest_complete_contiguous_window() -> None:
    context = _daylight_ev_context()
    for slot in context.slots:
        if slot.valid_at in {
            datetime(2026, 6, 27, 12, 0, tzinfo=UTC),
            datetime(2026, 6, 27, 12, 5, tzinfo=UTC),
        }:
            slot.import_price = 0.05
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED: True,
        "ev_earliest_start": "23:00",
        "default_ready_by": "07:00",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
    }

    action = next(
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )

    daylight = action.desired_state["daylight_lowest_cost"]
    assert daylight["selected"] is True
    assert daylight["forecast_complete"] is True
    assert [item["valid_at"] for item in action.desired_state["allocated_slots"]] == [
        "2026-06-27T12:00:00+00:00",
        "2026-06-27T12:05:00+00:00",
    ]
    assert {item["allocation_source"] for item in action.desired_state["allocated_slots"]} == {
        "daylight"
    }


def test_daylight_schedule_does_not_allocate_a_slot_past_sunset() -> None:
    context = _daylight_ev_context()
    context.current_ev_soc_percent = 49.5
    context.daylight_windows = [
        DaylightWindow(
            start=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
            end=datetime(2026, 6, 27, 17, 3, tzinfo=UTC),
        )
    ]
    for slot in context.slots:
        if slot.valid_at == datetime(2026, 6, 27, 17, 0, tzinfo=UTC):
            slot.import_price = -1.0
        elif slot.valid_at == datetime(2026, 6, 27, 16, 55, tzinfo=UTC):
            slot.import_price = 0.01
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED: True,
        "default_ready_by": "07:00",
        "ev_continuous_charging": False,
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
    }

    action = next(
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )
    allocations = action.desired_state["allocated_slots"]

    assert action.desired_state["daylight_lowest_cost"]["selected"] is True
    assert allocations[0]["valid_at"] == "2026-06-27T16:55:00+00:00"
    assert all(
        datetime.fromisoformat(item["valid_at"]) + timedelta(minutes=5)
        <= context.daylight_windows[0].end
        for item in allocations
        if item["allocation_source"] == "daylight"
    )


def test_disabled_daylight_policy_preserves_compact_legacy_ev_evidence() -> None:
    context = _daylight_ev_context()
    action = next(
        action
        for action in DryRunPlanner(
            {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
        ).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )

    assert "daylight_lowest_cost" not in action.desired_state
    assert "allocation_source_now" not in action.desired_state
    assert all(
        "allocation_source" not in item
        for item in action.desired_state["allocated_slots"]
    )


def test_split_daylight_schedule_uses_ready_by_fallback_without_duplicates() -> None:
    context = _daylight_ev_context()
    context.current_ev_soc_percent = 35
    context.daylight_windows = [
        DaylightWindow(
            start=datetime(2026, 6, 27, 10, 0, tzinfo=UTC),
            end=datetime(2026, 6, 27, 10, 10, tzinfo=UTC),
        )
    ]
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED: True,
        "ev_continuous_charging": False,
        "ev_earliest_start": "23:00",
        "default_ready_by": "07:00",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
    }

    action = next(
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )
    allocations = action.desired_state["allocated_slots"]

    assert action.desired_state["daylight_lowest_cost"]["selected"] is True
    assert [item["allocation_source"] for item in allocations] == [
        "daylight",
        "daylight",
        "ready_by_fallback",
    ]
    assert len({item["valid_at"] for item in allocations}) == len(allocations)
    assert action.desired_state["infeasible"] is False


def test_incomplete_daylight_forecast_preserves_ready_by_schedule() -> None:
    context = _daylight_ev_context()
    context.slots = context.slots[:12]
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED: True,
        "default_ready_by": "07:00",
        "ev_continuous_charging": False,
    }

    action = next(
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )

    daylight = action.desired_state["daylight_lowest_cost"]
    assert daylight["applicable"] is True
    assert daylight["forecast_complete"] is False
    assert daylight["selected"] is False
    assert daylight["reason"] == "ev_daylight_forecast_incomplete"
    assert {item["allocation_source"] for item in action.desired_state["allocated_slots"]} == {
        "ready_by"
    }


@pytest.mark.parametrize(
    ("scenario", "expected_reason"),
    [
        ("active_session", "ev_daylight_deferred_active_session"),
        ("opportunistic", "ev_daylight_deferred_opportunistic_charge"),
        ("no_window", "ev_daylight_window_not_before_ready_by"),
        ("already_at_target", "already_at_target"),
        ("no_eligible", "ev_daylight_no_eligible_charge"),
        ("continuous_shortfall", "ev_daylight_continuous_capacity_insufficient"),
    ],
)
def test_daylight_preference_preserves_higher_priority_and_fallback_paths(
    scenario: str,
    expected_reason: str,
) -> None:
    context = _daylight_ev_context()
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED: True,
        "default_ready_by": "07:00",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
    }
    if scenario == "active_session":
        context.ev_charging = True
    elif scenario == "opportunistic":
        options.update(
            {
                "ev_low_price_charging_enabled": True,
                "ev_low_price_threshold": 1.0,
            }
        )
    elif scenario == "no_window":
        context.daylight_windows = []
    elif scenario == "already_at_target":
        context.current_ev_soc_percent = context.ev_target_soc_percent
    elif scenario == "no_eligible":
        options.update(
            {
                "ev_price_limit_enabled": True,
                "ev_max_import_price": 0.10,
            }
        )
    else:
        context.daylight_windows = [
            DaylightWindow(
                start=context.created_at,
                end=context.created_at + timedelta(minutes=5),
            )
        ]

    action = next(
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )

    daylight = action.desired_state["daylight_lowest_cost"]
    assert daylight["selected"] is False
    assert daylight["reason"] == expected_reason
    if scenario == "already_at_target":
        assert action.reason_codes == [
            "ev_outside_allocated_charging_window",
            "vehicle_target_soc",
            "already_at_target",
        ]


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


def test_hvac_rollback_capability_issue_suppresses_takeover_but_keeps_release() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "minimum_climate_confidence": 0,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.input_issues = ["main_climate_target_unavailable"]

    blocked = DryRunPlanner(options).create_plan(context)

    assert [
        action for action in blocked.actions if action.asset == ActionAsset.DAIKIN
    ] == []

    context.hvac_control = {
        "phase": "preconditioning",
        "required_evidence_lost": "main_climate_target_unavailable",
    }
    release_actions = [
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert len(release_actions) == 1
    assert release_actions[0].kind == ActionKind.RELEASE_HVAC


def test_unclassified_input_health_produces_an_unsafe_actionless_plan() -> None:
    context = _context()
    context.input_health = "mystery"  # type: ignore[assignment]

    plan = DryRunPlanner(
        {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    ).create_plan(context)

    assert plan.mode == PlannerMode.ACTIVE_DEGRADED
    assert plan.status == "unsafe"
    assert plan.confidence == 0.0
    assert plan.actions == []


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
            "allocated_slots": [{"carbon_intensity_g_per_kwh": 100, "grid_import_used_kw": 5.0}],
        },
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
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
        "default_ready_by": "00:10",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 60
    context.ev_target_soc_percent = 70
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


def test_planner_mode_rejects_truthy_string_safety_options() -> None:
    context = _context()

    disabled = DryRunPlanner({**DEFAULT_OPTIONS, "planner_enabled": "true", "dry_run": False}).create_plan(context)
    dry_run = DryRunPlanner({**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": "false"}).create_plan(context)

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


def test_optional_forecast_confidence_does_not_cap_required_plan_or_unrelated_action() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.occupancy_state = OccupancyState.AWAY
    context.forecast_confidence = 0.1
    context.forecast_confidence_by_source = {
        CONF_AMBER_IMPORT_PRICE: 0.9,
        CONF_AMBER_EXPORT_PRICE: 0.8,
        CONF_PV_FORECAST: 0.7,
        CONF_HOUSEHOLD_LOAD: 1.0,
        CONF_WEATHER: 0.9,
        CONF_CARBON_INTENSITY_FORECAST: 0.1,
    }

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.confidence == 0.7
    assert plan.confidence_breakdown["climate"] == 0.9
    assert plan.confidence_breakdown["limited_by"] == "solar"
    assert any("away_hvac_policy" in action.reason_codes for action in plan.actions)


def test_climate_confidence_uses_weather_source_instead_of_unrelated_global_minimum() -> None:
    context = _context()
    context.forecast_confidence = 0.1
    context.forecast_confidence_by_source = {
        CONF_AMBER_IMPORT_PRICE: 0.9,
        CONF_AMBER_EXPORT_PRICE: 0.8,
        CONF_PV_FORECAST: 0.7,
        CONF_HOUSEHOLD_LOAD: 1.0,
        CONF_WEATHER: 0.4,
        CONF_CARBON_INTENSITY_FORECAST: 0.1,
    }
    action = PlanAction(
        action_id="climate-confidence",
        plan_id=context.plan_id,
        execute_not_before=context.created_at,
        execute_not_after=context.created_at,
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"hvac_mode": "heat"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )

    assert not planner_module._action_meets_confidence_threshold(action, context, DEFAULT_OPTIONS)
    assert not planner_module.plan_asset_meets_confidence_threshold(
        ActionAsset.DAIKIN,
        object(),
        DEFAULT_OPTIONS,
    )
    reason = planner_module._confidence_rejection_reason(ActionAsset.DAIKIN, context, DEFAULT_OPTIONS)
    assert reason is not None
    assert "climate 40.0%" in reason


def test_active_plan_turns_hvac_off_when_away() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.occupancy_state = OccupancyState.AWAY
    plan = DryRunPlanner(options).create_plan(context)
    assert plan.mode == PlannerMode.ACTIVE_HEALTHY
    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.actions[0].desired_state == {"hvac_mode": "off"}


def test_active_plan_schedules_ev_to_vehicle_target_soc() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = 60
    context.ev_target_soc_percent = 70
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
        "planning_interval_minutes": 5,
    }
    plan = DryRunPlanner(options).create_plan(context)
    assert plan.actions[0].asset == ActionAsset.EV
    assert plan.actions[0].kind == ActionKind.EV_SCHEDULE
    assert plan.actions[0].desired_state["target_soc_percent"] == 70.0
    assert plan.actions[0].desired_state["charging_required_now"] is False
    assert plan.actions[0].desired_state["continuous_charging"] is True
    assert [slot.projected_ev_load_kw for slot in context.slots] == [0.0, 0.0, 6, 6]
    assert plan.device_plans["ev"]["total_estimated_energy_kwh"] == 1.0
    timeline = plan.device_plans["ev"]["timeline"]
    assert [item["state"] for item in timeline] == ["idle", "charging"]
    assert timeline[1]["start"] == "2026-06-27T00:10:00+00:00"
    assert timeline[1]["end"] == "2026-06-27T00:20:00+00:00"


def test_active_plan_keeps_observed_continuous_ev_session_running() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "default_ready_by": "00:15",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.created_at = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    context.current_ev_soc_percent = 64
    context.ev_target_soc_percent = 74
    context.ev_charging = True
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=0,
            baseline_load_forecast_kw=1,
        )
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.10)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    action = next(action for action in plan.actions if action.asset == ActionAsset.EV)
    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["continued_active_session"] is True
    assert action.desired_state["charging_reason"] == "ev_continuous_charging_in_progress"
    assert [slot.projected_ev_load_kw for slot in context.slots] == [6, 6, 0.0]


def test_continued_pre_window_slot_counts_toward_ev_target_feasibility() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "default_ready_by": "00:15",
        "ev_earliest_start": "00:05",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.created_at = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    context.current_ev_soc_percent = 64
    context.ev_target_soc_percent = 79
    context.ev_charging = True
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.15,
            export_price=0.05,
            pv_forecast_kw=0,
            baseline_load_forecast_kw=1,
        )
        for offset in (0, 5, 10)
    ]

    plan = DryRunPlanner(options).create_plan(context)

    action = next(action for action in plan.actions if action.asset == ActionAsset.EV)
    assert action.desired_state["continued_active_session"] is True
    assert action.desired_state["max_attainable_soc_percent"] == 79
    assert action.desired_state["infeasible"] is False


def test_ev_target_at_or_below_current_soc_creates_native_stop_decision() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = 80
    context.ev_target_soc_percent = 70

    plan = DryRunPlanner(options).create_plan(context)

    action = next(action for action in plan.actions if action.asset == ActionAsset.EV)
    assert action.desired_state["charging_required_now"] is False
    assert action.desired_state["target_soc_percent"] == 70
    assert action.desired_state["allocated_slots"] == []
    assert action.desired_state["charging_reason"] == "ev_outside_allocated_charging_window"
    assert "ev_target_soc_below_current" not in ConstraintValidator(options).validate_plan(context, plan)


def test_keep_charger_on_reserves_grid_power_for_preconditioning_after_target() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_keep_charger_on": True,
    }
    context = _context()
    context.current_ev_soc_percent = 80
    context.ev_connected = True

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["keep_charger_on"] is True
    assert action.desired_state["charging_reason"] == "ev_keep_charger_on_for_preconditioning"
    assert action.desired_state["projected_load_kw_now"] == options["ev_charge_rate_kw"]
    assert context.slots[0].projected_ev_load_kw == options["ev_charge_rate_kw"]


def test_keep_charger_on_policy_does_not_change_confirmation_for_normal_charging() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_keep_charger_on": True,
    }
    context = _context()
    context.current_ev_soc_percent = 40
    context.ev_target_soc_percent = 45
    context.ev_connected = True

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["keep_charger_on"] is False
    assert action.desired_state["projected_load_kw_now"] == options["ev_charge_rate_kw"]


def test_vehicle_target_soc_replaces_legacy_maximum_soc_policy() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_keep_charger_on": True,
        "ev_max_soc_percent": 90,
    }
    context = _context()
    context.current_ev_soc_percent = 90
    context.ev_connected = True
    context.ev_target_soc_percent = 100

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["keep_charger_on"] is False
    assert action.desired_state["target_soc_percent"] == 100
    assert action.desired_state["vehicle_target_soc_percent"] == 100
    assert action.desired_state["target_soc_source"] == "vehicle_sensor"


def test_vehicle_target_soc_is_the_sole_soc_target() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
    }
    context = _context()
    context.current_ev_soc_percent = 20
    context.ev_connected = True
    context.ev_target_soc_percent = 30

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["target_soc_percent"] == 30
    assert action.desired_state["target_soc_source"] == "vehicle_sensor"


def test_keep_charger_on_uses_vehicle_target_when_current_soc_is_higher() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_keep_charger_on": True,
    }
    context = _context()
    context.current_ev_soc_percent = 100
    context.ev_connected = True
    context.ev_target_soc_percent = 80

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["keep_charger_on"] is True
    assert action.desired_state["target_soc_percent"] == 80


def test_native_ev_low_price_charging_starts_current_interval() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_low_price_charging_enabled": True,
        "ev_low_price_threshold": 0,
        "ev_continuous_charging": False,
    }
    context = _context()
    context.current_ev_soc_percent = 60
    context.slots[0].import_price = -0.1

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["charging_reason"] == "ev_low_price_charge_now"


def test_native_ev_low_price_charging_bypasses_earliest_start() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_low_price_charging_enabled": True,
        "ev_low_price_threshold": 0.05,
        "ev_earliest_start": "23:00",
    }
    context = _context()
    context.created_at = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.01 if offset == 0 else 0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in range(0, 24 * 60, 5)
    ]

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    allocated = [datetime.fromisoformat(item["valid_at"]) for item in action.desired_state["allocated_slots"]]
    earliest_start = datetime.fromisoformat(action.desired_state["earliest_start_utc"])
    assert action.desired_state["charging_required_now"] is True
    assert action.desired_state["charging_reason"] == "ev_low_price_charge_now"
    assert allocated[0] == context.created_at
    assert all(valid_at >= earliest_start for valid_at in allocated[1:])


def test_native_ev_price_above_threshold_does_not_bypass_earliest_start() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_low_price_charging_enabled": True,
        "ev_low_price_threshold": 0.05,
        "ev_earliest_start": "23:00",
    }
    context = _context()
    context.created_at = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.06,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in range(0, 24 * 60, 5)
    ]

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is False
    assert action.desired_state["charging_reason"] == "ev_outside_allocated_charging_window"
    assert context.slots[0].projected_ev_load_kw == 0.0


def test_native_ev_opportunistic_charge_honors_maximum_import_price() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "ev_low_price_charging_enabled": True,
        "ev_low_price_threshold": 0.50,
        "ev_price_limit_enabled": True,
        "ev_max_import_price": 0.20,
        "ev_earliest_start": "23:00",
    }
    context = _context()
    context.created_at = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.30,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in range(0, 24 * 60, 5)
    ]

    action = next(
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.EV
    )

    assert action.desired_state["charging_required_now"] is False
    assert action.desired_state["charging_reason"] == "ev_outside_allocated_charging_window"
    assert context.slots[0].projected_ev_load_kw == 0.0


def test_native_ev_manual_overrides_survive_immediate_replan() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    start_context = _context()
    start_context.current_ev_soc_percent = 80
    start_context.active_overrides = [Override("manual_ev_charging", "button", None, "manual_start")]

    start = next(
        action for action in DryRunPlanner(options).create_plan(start_context).actions if action.asset == ActionAsset.EV
    )
    stop_context = _context()
    stop_context.current_ev_soc_percent = 80
    stop_context.active_overrides = [Override("manual_ev_charging", "button", None, "manual_stop")]
    stop = next(
        action for action in DryRunPlanner(options).create_plan(stop_context).actions if action.asset == ActionAsset.EV
    )

    assert start.desired_state["charging_required_now"] is True
    assert start.desired_state["charging_reason"] == "ev_manual_start_override"
    assert start.desired_state["projected_load_kw_now"] == options["ev_charge_rate_kw"]
    assert start_context.slots[0].projected_ev_load_kw == options["ev_charge_rate_kw"]
    assert stop.desired_state["charging_required_now"] is False
    assert stop.desired_state["charging_reason"] == "ev_manual_stop_override"
    assert stop_context.slots[0].projected_ev_load_kw == 0.0


def test_ev_earliest_start_handles_window_timezone_and_invalid_values() -> None:
    created = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    ready = datetime(2026, 6, 27, 21, 0, tzinfo=UTC)

    assert _ev_earliest_start(created, ready, "None", "UTC") == created
    assert _ev_earliest_start(created, ready, "bad", "UTC") == created
    assert _ev_earliest_start(created, ready, "20:00", "Missing/Timezone") == datetime(2026, 6, 27, 20, 0, tzinfo=UTC)


def test_active_plan_exposes_solar_aware_ev_charge_allocation() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "default_ready_by": "00:15",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 40
    context.ev_target_soc_percent = 45
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
        "ev_outside_allocated_charging_window",
        "vehicle_target_soc",
        "continuous_charging_window_before_ready_by",
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
        planner_module._carbon_action_score(action(ActionAsset.ENPHASE, {"arbitrage_direction": "consume"}), context)
        == 1.0
    )
    assert planner_module._carbon_action_score(action(ActionAsset.ENPHASE, {}), context) == 0.0


def test_planner_small_helpers_cover_invalid_ready_by_and_empty_prices() -> None:
    now = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    context = _context()
    context.slots = [DecisionSlot(now, None, None, None, None)]

    assert _next_ready_by(now, "bad") == datetime(2026, 6, 28, 7, 0, tzinfo=UTC)
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

    assert _next_ready_by(winter_now, "07:00", "Australia/Melbourne") == datetime(2026, 7, 11, 21, 0, tzinfo=UTC)
    assert _next_ready_by(summer_now, "07:00", "Australia/Melbourne") == datetime(2026, 1, 11, 20, 0, tzinfo=UTC)


def test_ev_schedule_preserves_absolute_ready_by_timestamp() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
        "default_ready_by": "07:00",
    }
    context = _context()
    context.created_at = datetime(2026, 7, 11, 20, 30, tzinfo=UTC)
    context.local_timezone = "Australia/Melbourne"
    context.current_ev_soc_percent = 60
    context.ev_target_soc_percent = 70
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

    assert _next_ready_by(after_ready, "07:00", "Australia/Melbourne") == datetime(2026, 7, 12, 21, 0, tzinfo=UTC)
    # 02:30 does not exist on this date, so the wall-clock deadline advances to 03:00.
    assert _next_ready_by(before_spring_gap, "02:30", "Australia/Melbourne") == datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert _next_ready_by(after_ready, "07:00", "Invalid/Timezone") == datetime(2026, 7, 12, 7, 0, tzinfo=UTC)


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
    rejected = planner_module._rejected_climate_decision(context, DEFAULT_OPTIONS)
    assert rejected["reason"] == "Skipped comfort preconditioning because no tariff forecast slots are available."
    assert planner_module._timeline_card_rows({"bad": "value"}) == []

    context.slots = [DecisionSlot(context.created_at, 0.2, 0.05, None, 1.0)]
    rejected = planner_module._rejected_climate_decision(context, DEFAULT_OPTIONS)
    assert rejected["reason"] == (
        "No climate preconditioning was selected because the forecast contained no price window "
        "that both met the configured price difference and could be shifted within the thermal limits. "
        "This is a normal no-action planning outcome."
    )
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


def test_active_plan_uses_runtime_ready_by_option_for_ev_schedule() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "default_ready_by": "00:10",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 60
    context.ev_target_soc_percent = 70
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
    assert plan.actions[0].desired_state["ready_by"] == "08:00"
    assert plan.actions[0].desired_state["required_charge_percent"] == 8
    assert any(entry["state"] == "charging" for entry in plan.device_plans["ev"]["timeline"])


def test_active_plan_does_not_schedule_ev_when_disconnected() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = 60
    context.ev_connected = False

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions == []


def test_active_plan_uses_vehicle_target_without_deriving_a_trip_target() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "default_ready_by": "03:00",
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = 50
    context.ev_target_soc_percent = 65
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
    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.EV
    assert plan.actions[0].desired_state["target_soc_percent"] == 65.0
    assert plan.actions[0].desired_state["target_soc_source"] == "vehicle_sensor"
    assert "vehicle_target_soc" in plan.actions[0].reason_codes


def test_active_plan_uses_recorder_calibrated_soc_per_kwh() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "default_ready_by": "03:00",
        "ev_charge_rate_kw": 7,
        "ev_soc_per_kwh": 5,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.created_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    context.current_ev_soc_percent = 60
    context.ev_target_soc_percent = 74
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.20,
            export_price=0.05,
            pv_forecast_kw=0,
            baseline_load_forecast_kw=1,
        )
        for offset in range(0, 3 * 60, 5)
    ]
    calibration = {
        "model_version": 1,
        "status": "ready",
        "charging_entity_id": "sensor.ev_charging",
        "soc_entity_id": "sensor.ev_soc",
        "charge_rate_kw": 7.0,
        "soc_per_kwh": 1.8,
        "sample_count": 3,
    }

    action = next(
        action
        for action in DryRunPlanner(
            options,
            ev_charge_calibration=calibration,
            ev_charging_entity_id="sensor.ev_charging",
            ev_soc_entity_id="sensor.ev_soc",
        ).create_plan(context).actions
        if action.asset == ActionAsset.EV
    )

    assert action.desired_state["soc_per_kwh"] == 1.8
    assert action.desired_state["soc_per_kwh_source"] == "recorder_charging_history"
    assert action.desired_state["charge_calibration_sample_count"] == 3
    assert len(action.desired_state["allocated_slots"]) == 14


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
    assert plan.device_plans["enphase"]["current_state_label"] == "Self-Consumption"
    assert plan.device_plans["enphase"]["next_planned_state_label"] == "Restore AI: AI Optimisation"


def test_active_plan_does_not_start_takeover_after_expensive_period_has_begun() -> None:
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

    assert plan.actions == []


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


def test_active_plan_does_not_release_hvac_without_ownership_when_outside_bounds() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
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

    assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


def test_degraded_ev_issue_does_not_block_asset_eligible_hvac_preconditioning() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
    }
    context = _context()
    context.input_health = InputHealth.DEGRADED
    context.input_issues = ["ev_soc_unavailable"]
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.hvac_control = {}
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (15, 0.10), (30, 0.50)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    climate_actions = [action for action in plan.actions if action.asset == ActionAsset.DAIKIN]
    assert climate_actions
    assert climate_actions[0].kind == ActionKind.SET_HVAC
    assert climate_actions[0].desired_state["phase"] == "preconditioning"


def test_dry_run_does_not_hand_comfort_back_without_active_ownership() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 24
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.hvac_control = {}

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.mode == PlannerMode.DRY_RUN
    assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


def test_degraded_dry_run_does_not_hand_comfort_back_without_active_ownership() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": True,
    }
    context = _context()
    context.input_health = InputHealth.DEGRADED
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 24
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.hvac_control = {}

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.mode == PlannerMode.DRY_RUN
    assert plan.actions == []


def test_disabled_climate_control_only_releases_persisted_ownership() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": False,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 24
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.hvac_control = {}

    unowned_plan = DryRunPlanner(options).create_plan(context)

    assert [action for action in unowned_plan.actions if action.asset == ActionAsset.DAIKIN] == []

    period_end = context.created_at + timedelta(hours=1)
    context.hvac_control = {
        "phase": "peak_coast",
        "period_end": period_end,
    }

    owned_plan = DryRunPlanner(options).create_plan(context)

    assert owned_plan.actions[0].kind == ActionKind.RELEASE_HVAC
    assert owned_plan.actions[0].desired_state == {
        "release_reason": "hvac_comfort_handoff",
        "released_until": period_end,
    }


def test_active_plan_preconditions_from_comfort_boundary_before_price_rise() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 18.0
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
        for offset, price in [
            (0, 0.10),
            (5, 0.12),
            (10, 0.15),
            (15, 0.14),
            (20, 0.13),
            (25, 0.11),
            (30, 0.45),
        ]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.actions[0].desired_state["phase"] == "preconditioning"
    assert plan.actions[0].desired_state["hvac_mode"] == "heat"
    assert plan.actions[0].desired_state["target_temperature"] == 24.0
    assert plan.actions[0].execute_not_before == context.created_at
    assert plan.actions[1].desired_state["phase"] == "peak_coast"


def test_active_plan_can_precondition_while_away_when_explicitly_enabled() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
        "hvac_precondition_while_away": True,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_temperature_c = 18.0
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
        for offset, price in [
            (0, 0.10),
            (5, 0.12),
            (10, 0.15),
            (15, 0.14),
            (20, 0.13),
            (25, 0.11),
            (30, 0.45),
        ]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].asset == ActionAsset.DAIKIN
    assert plan.actions[0].desired_state["phase"] == "preconditioning"
    assert plan.actions[0].desired_state["target_temperature"] == 24.0
    assert plan.actions[0].hard_constraints[0] == "away_preconditioning_enabled"
    assert plan.actions[1].desired_state["phase"] == "peak_coast"


def test_future_away_preconditioning_keeps_hvac_off_until_window_starts() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
        "hvac_precondition_while_away": True,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {"sample_count": 12, "average": 2.0},
        "active_heat_rate_c_per_hour": {"sample_count": 3, "average": 12.0},
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 23.0
    context.occupied_temperature_low_c = 18.0
    context.occupied_temperature_high_c = 24.0
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [
            (0, 0.10),
            (5, 0.12),
            (10, 0.15),
            (15, 0.14),
            (20, 0.13),
            (25, 0.11),
            (30, 0.45),
        ]
    ]

    climate_actions = [
        action
        for action in DryRunPlanner(options, thermal_model=thermal_model).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]
    away_off = next(action for action in climate_actions if "away_hvac_policy" in action.reason_codes)
    preconditioning = next(
        action for action in climate_actions if action.desired_state.get("phase") == "preconditioning"
    )

    assert away_off.execute_not_before == context.created_at
    assert away_off.desired_state == {"hvac_mode": "off"}
    assert preconditioning.execute_not_before > context.created_at


def test_future_away_preconditioning_can_start_immediately_without_away_off_takeover() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
        "hvac_precondition_while_away": True,
        "hvac_min_cycle_minutes": 20,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {"sample_count": 12, "average": 2.0},
        "active_heat_rate_c_per_hour": {"sample_count": 3, "average": 12.0},
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 23.0
    context.occupied_temperature_low_c = 18.0
    context.occupied_temperature_high_c = 24.0
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.30), (5, 0.20), (10, 0.10), (15, 0.45), (20, 0.10)]
    ]

    climate_actions = [
        action
        for action in DryRunPlanner(options, thermal_model=thermal_model).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions[0].desired_state["phase"] == "preconditioning"
    assert climate_actions[0].execute_not_before == context.created_at
    assert climate_actions[0].desired_state["hvac_mode"] == "heat"
    assert context.slots[0].projected_hvac_load_kw == 2.0


def test_away_off_ownership_transitions_to_opted_in_preconditioning() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
        "hvac_precondition_while_away": True,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_temperature_c = 18.0
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.hvac_control = {
        "phase": "away_off",
        "started_at": context.created_at - timedelta(minutes=30),
    }
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [
            (0, 0.10),
            (5, 0.12),
            (10, 0.15),
            (15, 0.14),
            (20, 0.13),
            (25, 0.11),
            (30, 0.45),
        ]
    ]

    climate_actions = [
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions[0].kind == ActionKind.SET_HVAC
    assert climate_actions[0].desired_state["phase"] == "preconditioning"
    assert climate_actions[0].desired_state["target_temperature"] == 24.0


def test_away_off_ownership_remains_stable_without_preconditioning_candidate() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_while_away": True,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.hvac_control = {"phase": "away_off"}

    climate_actions = [
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions == []


def test_away_off_ownership_does_not_release_at_comfort_boundary_without_candidate() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_while_away": True,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_temperature_c = 18.0
    context.occupied_temperature_low_c = 18.0
    context.occupied_temperature_high_c = 24.0
    context.hvac_control = {"phase": "away_off"}

    climate_actions = [
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions == []


def test_active_plan_does_not_precondition_further_past_mode_target_boundary() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "climate_control_enabled": True,
        "hvac_precondition_lead_minutes": 30,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {"sample_count": 12, "average": 2.0},
        "active_heat_rate_c_per_hour": {"sample_count": 3, "average": 6.0},
        "active_cool_rate_c_per_hour": {"sample_count": 3, "average": 6.0},
    }
    for mode, current in (("heat", 24.0), ("cool", 18.0)):
        context = _context()
        context.current_ev_soc_percent = None
        context.current_hvac_mode = mode
        context.current_hvac_temperature_c = current
        context.occupied_temperature_low_c = 18.0
        context.occupied_temperature_high_c = 24.0
        context.slots = [
            DecisionSlot(
                valid_at=context.created_at + timedelta(minutes=offset),
                import_price=price,
                export_price=0.05,
                pv_forecast_kw=1.0,
                baseline_load_forecast_kw=2.0,
            )
            for offset, price in [
                (0, 0.10),
                (5, 0.12),
                (10, 0.15),
                (15, 0.14),
                (20, 0.13),
                (25, 0.11),
                (30, 0.45),
            ]
        ]

        plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

        assert [action for action in plan.actions if action.asset == ActionAsset.DAIKIN] == []


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
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
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
        for offset, price in [
            (0, 0.10),
            (5, 0.12),
            (10, 0.15),
            (15, 0.14),
            (20, 0.13),
            (25, 0.11),
            (30, 0.45),
        ]
    ]

    plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    assert plan.actions[0].desired_state["projected_hvac_load_kw"] == 1.8
    assert plan.actions[0].desired_state["thermal_model_enabled"] is True
    assert plan.actions[0].desired_state["thermal_model_sample_count"] == 12
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [
        1.8,
        1.8,
        1.8,
        1.8,
        1.8,
        1.8,
        0.0,
    ]
    assert plan.device_plans["climate"]["total_estimated_energy_kwh"] == 0.9
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
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 12.0},
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
    assert plan.actions[0].desired_state["phase"] == "preconditioning"
    assert plan.actions[0].desired_state["coast_target"] == 19.0
    assert plan.actions[0].desired_state["active_heat_rate_c_per_hour"] == 12.0
    assert "hvac_preconditioning" in plan.actions[0].reason_codes
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [2.0, 2.0, 2.0, 0.0]
    assert plan.device_plans["climate"]["next_planned_state_label"] == "Preconditioning: Heat to 23.0 C"


def test_active_plan_uses_partial_preconditioning_when_full_target_is_infeasible() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 30,
        "hvac_precondition_lead_minutes": 120,
        "hvac_precondition_min_price_delta": 0.20,
    }
    thermal_model = {
        "enabled": True,
        "active_hvac_load_kw": {"sample_count": 12, "average": 2.0},
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 0.5},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -5.0},
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 19.1
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
        for offset, price in [(0, 0.10), (30, 0.12), (60, 0.45), (90, 0.45)]
    ]

    plan = DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    climate_actions = [action for action in plan.actions if action.asset == ActionAsset.DAIKIN]
    assert climate_actions[0].desired_state["phase"] == "preconditioning"
    assert climate_actions[0].execute_not_before == context.created_at
    assert climate_actions[0].desired_state["target_temperature"] == 23.0
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [2.0, 2.0, 0.0, 2.0]


def test_thermal_coast_helpers_cover_defensive_branches() -> None:
    context = _context()
    context.current_hvac_mode = None
    context.current_hvac_temperature_c = None
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = None

    assert planner_module._effective_passive_drift_c_per_hour(context, "heat", {}) is None
    context.current_outdoor_temperature_c = 5
    assert planner_module._effective_passive_drift_c_per_hour(context, "heat", {}) == -0.5
    context.current_outdoor_temperature_c = 30
    assert planner_module._effective_passive_drift_c_per_hour(context, "cool", {}) == 0.5
    context.current_outdoor_temperature_c = 5
    assert planner_module._effective_passive_drift_c_per_hour(context, "cool", {}) is None

    assert (
        planner_module._effective_passive_drift_c_per_hour(
            context,
            "heat",
            {"passive_indoor_drift_c_per_hour": {"average": -0.25}},
        )
        == -0.25
    )
    assert (
        planner_module._thermal_coast_hours(
            mode="heat",
            target_temperature=23,
            comfort_boundary=19,
            passive_drift_c_per_hour=None,
        )
        is None
    )
    assert (
        planner_module._thermal_coast_hours(
            mode="cool",
            target_temperature=19,
            comfort_boundary=23,
            passive_drift_c_per_hour=0.5,
        )
        == 8
    )
    assert (
        planner_module._thermal_coast_hours(
            mode="heat",
            target_temperature=23,
            comfort_boundary=19,
            passive_drift_c_per_hour=0.5,
        )
        is None
    )


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
    context.occupied_temperature_low_c = 17
    context.occupied_temperature_high_c = 17.5
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
    assert plan.actions[0].desired_state["target_temperature"] == 17.5
    assert plan.actions[0].desired_state["projected_hvac_load_kw"] == 2.2
    assert plan.actions[0].desired_state["thermal_model_enabled"] is True
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [2.2, 0.0, 0.0, 0.0]


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
    context.occupied_temperature_low_c = 24.5
    context.occupied_temperature_high_c = 25
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
    assert plan.actions[0].desired_state["target_temperature"] == 24.5
    assert plan.actions[0].desired_state["projected_hvac_load_kw"] == 1.6
    assert plan.actions[0].desired_state["thermal_model_enabled"] is True
    assert [slot.projected_hvac_load_kw for slot in context.slots] == [1.6, 0.0, 0.0, 0.0]


def test_active_plan_discovers_peak_beyond_immediate_lead_window() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_lead_minutes": 10,
        "hvac_precondition_min_price_delta": 0.20,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
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
        for offset, price in [(0, 0.10), (5, 0.12), (10, 0.15), (15, 0.45)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    assert plan.actions[0].kind == ActionKind.SET_HVAC
    assert plan.actions[0].execute_not_before == context.slots[1].valid_at
    assert plan.actions[1].desired_state["phase"] == "peak_coast"
    assert plan.actions[2].kind == ActionKind.RELEASE_HVAC


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
    context.current_hvac_mode = "heat"
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
        for offset, price in [(0, 0.10), (15, 0.45), (30, 0.50)]
    ]

    plan = DryRunPlanner(options).create_plan(context)

    climate_actions = [action for action in plan.actions if action.asset == ActionAsset.DAIKIN]
    assert climate_actions[0].execute_not_before == context.slots[0].valid_at
    assert climate_actions[1].execute_not_before == context.slots[1].valid_at


def test_zero_hvac_precondition_lead_disables_tariff_takeover() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
        "hvac_precondition_lead_minutes": 0,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.50), (10, 0.50)]
    ]

    climate_actions = [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions == []


def test_fallback_hvac_preconditioning_catches_up_with_remaining_lead_window() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
        "hvac_precondition_lead_minutes": 30,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.10), (10, 0.50), (15, 0.50)]
    ]

    planner = DryRunPlanner(options)
    climate_actions = [
        action for action in planner.create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions[0].desired_state["phase"] == "preconditioning"
    assert climate_actions[0].execute_not_before == context.created_at
    assert climate_actions[0].desired_state["target_temperature"] == 23.0
    assert climate_actions[0].desired_state["projected_precondition_end_temperature"] == 19.0
    assert all(slot.projected_hvac_load_kw > 0 for slot in context.slots[2:])

    persisted_keys = {
        "phase",
        "period_start",
        "period_end",
        "precondition_end",
        "baseline_price",
        "precondition_min_price_delta",
        "suppression_min_price_delta",
        "mode",
        "precondition_target",
        "coast_target",
        "projected_precondition_end_temperature",
    }
    context.hvac_control = {
        key: value
        for key, value in climate_actions[0].desired_state.items()
        if key in persisted_keys
    }
    for slot in context.slots:
        slot.projected_hvac_load_kw = 0.0

    continuation = [
        action for action in planner.create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert continuation[0].desired_state["projected_precondition_end_temperature"] == 19.0
    assert all(slot.projected_hvac_load_kw > 0 for slot in context.slots[2:])


def test_fallback_hvac_preconditioning_scans_past_a_tariff_gap() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
        "hvac_precondition_lead_minutes": 30,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [
            (0, 0.10),
            (5, None),
            (10, 0.10),
            (15, 0.50),
            (20, 0.50),
        ]
    ]

    climate_actions = [
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert climate_actions[0].desired_state["phase"] == "preconditioning"
    assert climate_actions[0].execute_not_before == context.created_at + timedelta(minutes=10)
    assert climate_actions[0].desired_state["precondition_end"] == context.created_at + timedelta(
        minutes=15
    )


@pytest.mark.parametrize(
    "thermal_model",
    [
        None,
        {
            "enabled": True,
            "active_heat_rate_c_per_hour": {"sample_count": 3, "average": 10.0},
        },
    ],
)
def test_hvac_preconditioning_requalifies_prices_after_a_tariff_gap(
    thermal_model: dict[str, object] | None,
) -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
        "hvac_precondition_lead_minutes": 30,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 22.9
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [
            (0, 0.10),
            (5, None),
            (10, 0.29),
            (15, 0.40),
        ]
    ]

    preconditioning_actions = [
        action
        for action in DryRunPlanner(options, thermal_model=thermal_model).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
        and action.desired_state.get("phase") == "preconditioning"
    ]

    assert preconditioning_actions == []


def test_hvac_preconditioning_accounts_for_passive_drift_before_delayed_run() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 60,
        "hvac_precondition_lead_minutes": 240,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(hours=index),
            import_price=0.60 if index == 4 else 0.10,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for index in range(6)
    ]
    thermal_model = {
        "enabled": True,
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 2.0},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -1.0},
    }

    actions = [
        action
        for action in DryRunPlanner(options, thermal_model=thermal_model).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert actions[0].desired_state["phase"] == "preconditioning"
    assert actions[0].execute_not_before == context.slots[0].valid_at
    assert actions[0].desired_state["precondition_end"] == context.slots[1].valid_at
    assert [slot.projected_hvac_load_kw for slot in context.slots[:4]] == [1.0, 0.0, 0.0, 0.0]


def test_hvac_lifecycle_scans_full_twenty_four_hour_horizon() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_horizon_hours": 24,
        "planning_interval_minutes": 60,
        "hvac_precondition_lead_minutes": 120,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.climate_zone_entities = ["switch.living", "input_boolean.bedrooms"]
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(hours=index),
            import_price=0.50 if 20 <= index < 23 else 0.10,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for index in range(24)
    ]

    actions = DryRunPlanner(options).create_plan(context).actions

    assert actions[0].desired_state["phase"] == "preconditioning"
    assert actions[0].desired_state["controlled_zones"] == [
        "switch.living",
        "input_boolean.bedrooms",
    ]
    assert actions[0].desired_state["configured_zones_only"] is False
    assert actions[0].execute_not_before == context.slots[18].valid_at
    assert actions[1].desired_state["phase"] == "peak_coast"
    assert actions[1].execute_not_before == context.slots[20].valid_at
    assert actions[2].kind == ActionKind.RELEASE_HVAC
    assert actions[2].execute_not_before == context.slots[23].valid_at


def test_hvac_preconditioning_preserves_exact_comfort_target() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 60,
        "hvac_precondition_lead_minutes": 120,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
    context.occupied_temperature_low_c = 19.25
    context.occupied_temperature_high_c = 23.25
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(hours=index),
            import_price=0.50 if 2 <= index < 4 else 0.10,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for index in range(5)
    ]

    actions = DryRunPlanner(options).create_plan(context).actions

    assert actions[0].desired_state["target_temperature"] == 23.25
    assert actions[0].desired_state["precondition_target"] == 23.25
    assert actions[1].desired_state["target_temperature"] == 19.25


def test_active_preconditioning_releases_when_persisted_tariff_period_disappears() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.10), (10, 0.20), (15, 0.20)]
    ]
    context.hvac_control = {
        "phase": "preconditioning",
        "period_start": context.created_at + timedelta(minutes=10),
        "period_end": context.created_at + timedelta(minutes=20),
        "baseline_price": 0.10,
        "mode": "heat",
        "precondition_target": 23.0,
        "coast_target": 19.0,
    }

    action = DryRunPlanner(options).create_plan(context).actions[0]

    assert action.kind == ActionKind.RELEASE_HVAC
    assert action.desired_state["release_reason"] == "hvac_tariff_period_changed"


def test_active_hvac_lifecycle_uses_persisted_tariff_thresholds() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "hvac_precondition_min_price_delta": 0.50,
        "hvac_suppression_min_price_delta": 0.50,
        "hvac_precondition_while_away": True,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset, price in [(0, 0.10), (5, 0.10), (10, 0.40), (15, 0.40)]
    ]
    context.hvac_control = {
        "phase": "preconditioning",
        "period_start": context.created_at + timedelta(minutes=10),
        "period_end": context.created_at + timedelta(minutes=20),
        "precondition_end": context.created_at + timedelta(minutes=10),
        "baseline_price": 0.10,
        "precondition_min_price_delta": 0.20,
        "suppression_min_price_delta": 0.20,
        "mode": "heat",
        "precondition_target": 23.0,
        "coast_target": 19.0,
    }

    action = DryRunPlanner(options).create_plan(context).actions[0]

    assert action.kind == ActionKind.SET_HVAC
    assert action.desired_state["precondition_min_price_delta"] == 0.20
    assert action.desired_state["suppression_min_price_delta"] == 0.20


def test_active_peak_releases_when_tariff_horizon_no_longer_covers_period() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=offset),
            import_price=0.50,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
        )
        for offset in (0, 5)
    ]
    context.hvac_control = {
        "phase": "peak_coast",
        "period_start": context.created_at - timedelta(minutes=5),
        "period_end": context.created_at + timedelta(minutes=15),
        "baseline_price": 0.10,
        "mode": "heat",
        "precondition_target": 23.0,
        "coast_target": 19.0,
    }

    action = DryRunPlanner(options).create_plan(context).actions[0]

    assert action.kind == ActionKind.RELEASE_HVAC
    assert action.desired_state["release_reason"] == "hvac_tariff_evidence_lost"


def test_peak_projection_adds_maintenance_load_after_comfort_coast() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 60,
        "hvac_precondition_lead_minutes": 120,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(hours=index),
            import_price=0.50 if 2 <= index < 6 else 0.10,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for index in range(7)
    ]
    thermal_model = {
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 12.0},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -2.0},
    }

    DryRunPlanner(options, thermal_model=thermal_model).create_plan(context)

    assert context.slots[2].projected_hvac_load_kw == 0.0
    assert context.slots[3].projected_hvac_load_kw == 0.0
    assert context.slots[4].projected_hvac_load_kw == 1.0
    assert context.slots[5].projected_hvac_load_kw == 1.0


def test_early_preconditioning_consumes_coast_reserve_before_peak() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 60,
        "hvac_precondition_lead_minutes": 240,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(hours=index),
            import_price=0.60 if 4 <= index < 9 else 0.05 if index == 0 else 0.20,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for index in range(10)
    ]
    thermal_model = {
        "enabled": True,
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 4.0},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -1.0},
    }
    planner = DryRunPlanner(options, thermal_model=thermal_model)

    initial_actions = [action for action in planner.create_plan(context).actions if action.asset == ActionAsset.DAIKIN]

    assert initial_actions[0].execute_not_before == context.slots[0].valid_at
    assert context.slots[4].projected_hvac_load_kw == 0.0
    assert context.slots[5].projected_hvac_load_kw == 1.0

    context.hvac_control = {
        "phase": "preconditioning",
        "period_start": context.slots[4].valid_at,
        "period_end": context.slots[9].valid_at,
        "precondition_end": context.slots[1].valid_at,
        "baseline_price": 0.05,
        "mode": "heat",
        "precondition_target": 23.0,
        "coast_target": 19.0,
    }
    for slot in context.slots:
        slot.projected_hvac_load_kw = 0.0

    active_actions = [action for action in planner.create_plan(context).actions if action.asset == ActionAsset.DAIKIN]

    assert [action.desired_state.get("phase") for action in active_actions[:-1]] == [
        "preconditioning",
        "pre_peak_coast",
        "peak_coast",
    ]
    assert active_actions[-1].kind == ActionKind.RELEASE_HVAC
    assert context.slots[4].projected_hvac_load_kw == 0.0
    assert context.slots[5].projected_hvac_load_kw == 1.0


def test_active_hvac_lifecycle_coasts_then_releases_at_period_end() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots[0].import_price = 0.50
    context.hvac_control = {
        "phase": "preconditioning",
        "period_start": context.created_at - timedelta(minutes=5),
        "period_end": context.created_at + timedelta(minutes=30),
        "baseline_price": 0.10,
        "mode": "heat",
        "precondition_target": 23.0,
        "coast_target": 19.0,
    }

    peak_action = DryRunPlanner(options).create_plan(context).actions[0]
    context.hvac_control["period_end"] = context.created_at
    release_action = DryRunPlanner(options).create_plan(context).actions[0]

    assert peak_action.kind == ActionKind.SET_HVAC
    assert peak_action.desired_state["phase"] == "peak_coast"
    assert peak_action.desired_state["target_temperature"] == 19.0
    assert release_action.kind == ActionKind.RELEASE_HVAC
    assert release_action.desired_state["release_reason"] == "hvac_expensive_period_ended"


def test_hvac_release_bypasses_low_forecast_confidence() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.forecast_confidence = 0.2
    context.hvac_control = {
        "period_start": context.created_at - timedelta(hours=1),
        "period_end": context.created_at,
        "baseline_price": 0.10,
        "mode": "heat",
    }

    climate_actions = [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert len(climate_actions) == 1
    assert climate_actions[0].kind == ActionKind.RELEASE_HVAC


def test_active_hvac_lifecycle_releases_when_weather_confidence_falls() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.forecast_confidence_by_source = {
        CONF_AMBER_IMPORT_PRICE: 1.0,
        CONF_AMBER_EXPORT_PRICE: 1.0,
        CONF_PV_FORECAST: 1.0,
        CONF_HOUSEHOLD_LOAD: 1.0,
        CONF_WEATHER: 0.0,
    }
    context.hvac_control = {
        "phase": "preconditioning",
        "period_start": context.created_at + timedelta(minutes=15),
        "period_end": context.created_at + timedelta(minutes=30),
        "baseline_price": 0.10,
        "mode": "heat",
    }

    climate_actions = [
        action
        for action in DryRunPlanner(options).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert len(climate_actions) == 1
    assert climate_actions[0].kind == ActionKind.RELEASE_HVAC
    assert climate_actions[0].desired_state["release_reason"] == "hvac_confidence_below_threshold"


def test_away_transition_releases_active_hvac_lifecycle() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.hvac_control = {
        "phase": "peak_coast",
        "period_start": context.created_at - timedelta(minutes=5),
        "period_end": context.created_at + timedelta(hours=1),
        "baseline_price": 0.10,
        "mode": "heat",
    }

    climate_actions = [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert len(climate_actions) == 1
    assert climate_actions[0].kind == ActionKind.RELEASE_HVAC
    assert climate_actions[0].desired_state["release_reason"] == "hvac_required_evidence_lost"


def test_away_off_ownership_is_maintained_until_occupancy_returns() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.hvac_control = {"phase": "away_off"}

    away_actions = [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert away_actions == []

    context.input_health = InputHealth.UNSAFE
    degraded_actions = [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]

    assert degraded_actions == []

    context.occupancy_state = OccupancyState.OCCUPIED
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    unsafe_occupied_action = DryRunPlanner(options).create_plan(context).actions[0]

    assert unsafe_occupied_action.kind == ActionKind.RELEASE_HVAC

    context.input_health = InputHealth.HEALTHY
    occupied_action = DryRunPlanner(options).create_plan(context).actions[0]
    assert occupied_action.kind == ActionKind.RELEASE_HVAC


def test_away_off_ownership_releases_for_override_or_failed_restore() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.occupancy_state = OccupancyState.AWAY
    context.hvac_control = {
        "phase": "away_off",
        "required_evidence_lost": "hvac_release_failed",
    }

    failed_restore_release = DryRunPlanner(options).create_plan(context).actions[0]

    assert failed_restore_release.kind == ActionKind.RELEASE_HVAC
    assert failed_restore_release.desired_state["release_reason"] == "hvac_required_evidence_lost"

    context.hvac_control.pop("required_evidence_lost")
    context.active_overrides = [
        Override(
            "manual_hvac",
            "service",
            context.created_at + timedelta(minutes=15),
            "manual",
        )
    ]
    manual_release = DryRunPlanner(options).create_plan(context).actions[0]

    assert manual_release.kind == ActionKind.RELEASE_HVAC
    assert manual_release.desired_state["release_reason"] == "manual_hvac_override"

    context.input_health = InputHealth.UNSAFE
    degraded_release = DryRunPlanner(options).create_plan(context).actions[0]
    assert degraded_release.kind == ActionKind.RELEASE_HVAC
    assert degraded_release.desired_state["release_reason"] == "manual_hvac_override"


def test_comfort_release_holds_out_reacquisition_until_period_end() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 19
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    period_end = context.created_at + timedelta(hours=1)
    context.hvac_control = {
        "period_start": context.created_at - timedelta(minutes=5),
        "period_end": period_end,
        "baseline_price": 0.10,
        "mode": "heat",
    }

    release = DryRunPlanner(options).create_plan(context).actions[0]
    context.current_hvac_temperature_c = 21
    context.hvac_control = {"released_until": period_end}
    held = DryRunPlanner(options).create_plan(context).actions

    assert release.desired_state["released_until"] == period_end
    assert held == []


def test_hvac_lifecycle_fail_safe_release_branches() -> None:
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}
    planner = DryRunPlanner(options)
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.hvac_control = {
        "period_start": context.created_at - timedelta(minutes=5),
        "period_end": context.created_at + timedelta(minutes=30),
        "baseline_price": 0.10,
        "mode": "heat",
    }
    context.hvac_control["required_evidence_lost"] = "climate_zone_unavailable"
    assert planner.create_plan(context).actions[0].desired_state["release_reason"] == ("hvac_required_evidence_lost")
    context.hvac_control.pop("required_evidence_lost")

    context.active_overrides = [Override("manual_hvac", "service", context.created_at + timedelta(minutes=5), "manual")]
    assert planner.create_plan(context).actions[0].desired_state["release_reason"] == "manual_hvac_override"

    context.active_overrides = []
    context.slots = []
    assert planner.create_plan(context).actions[0].desired_state["release_reason"] == "hvac_tariff_evidence_lost"

    context.slots = [DecisionSlot(context.created_at, 0.10, 0.05, 1, 2)]
    assert planner.create_plan(context).actions[0].desired_state["release_reason"] == "hvac_expensive_period_ended"

    context.slots[0].import_price = 0.50
    context.hvac_control["mode"] = "fan_only"
    assert planner.create_plan(context).actions[0].desired_state["release_reason"] == "hvac_mode_evidence_lost"

    context.hvac_control = {"released_until": context.created_at - timedelta(minutes=1)}
    assert [action for action in planner.create_plan(context).actions if action.asset == ActionAsset.DAIKIN] == []

    context.input_health = InputHealth.UNSAFE
    context.hvac_control = {"phase": "peak_coast"}
    assert planner.create_plan(context).actions[0].desired_state["release_reason"] == "hvac_required_evidence_lost"


def test_hvac_lifecycle_period_and_mode_helpers_cover_forecast_edges() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = _context()
    context.created_at = now
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    planner = DryRunPlanner({**DEFAULT_OPTIONS, "planning_interval_minutes": 5})

    assert planner_module._datetime_value("2026-06-27T01:00:00Z") == datetime(2026, 6, 27, 1, tzinfo=UTC)
    assert planner_module._datetime_value(datetime(2026, 6, 27, 1)) == datetime(2026, 6, 27, 1, tzinfo=UTC)
    assert planner_module._datetime_value("bad") is None
    assert planner._next_hvac_period(context) is None

    context.slots = [
        DecisionSlot(now, 0.10, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=5), None, 0.05, 1, 2),
    ]
    assert planner._next_hvac_period(context) is None
    context.slots = [
        DecisionSlot(now, None, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=5), 0.60, 0.05, 1, 2),
    ]
    assert planner._next_hvac_period(context) is None

    context.current_hvac_mode = "heat"
    context.current_outdoor_temperature_c = 5
    context.slots = [
        DecisionSlot(now, 0.10, 0.05, 1, 2, outdoor_temperature_forecast_c=5),
        DecisionSlot(now + timedelta(minutes=5), 0.10, 0.05, 1, 2, outdoor_temperature_forecast_c=5),
        DecisionSlot(now + timedelta(minutes=10), 0.60, 0.05, 1, 2, outdoor_temperature_forecast_c=5),
    ]
    catch_up = planner._next_hvac_period(context, earliest_start=now + timedelta(minutes=5))
    assert catch_up is not None
    assert catch_up["precondition_start"] == now + timedelta(minutes=5)

    peak = DecisionSlot(now + timedelta(minutes=5), 0.60, 0.05, 1, 2, outdoor_temperature_forecast_c=21)
    context.current_hvac_mode = "off"
    context.current_outdoor_temperature_c = 5
    assert planner_module._future_hvac_mode(context, peak, 21, 19, 23) == "heat"
    context.current_outdoor_temperature_c = 30
    assert planner_module._future_hvac_mode(context, peak, 21, 19, 23) == "cool"
    context.current_outdoor_temperature_c = 21
    assert planner_module._future_hvac_mode(context, peak, 20, 19, 23) == "heat"
    assert planner_module._future_hvac_mode(context, peak, 22, 19, 23) == "cool"
    assert planner_module._future_hvac_mode(context, peak, 21, 19, 23) is None

    context.slots = []
    assert not planner_module._tariff_evidence_covers_period(
        context,
        now + timedelta(minutes=5),
        timedelta(minutes=5),
    )
    assert not planner_module._persisted_hvac_period_qualifies(
        context, now, now + timedelta(minutes=5), 0.10, 0.20, 0.20
    )
    context.slots = [DecisionSlot(now, 0.20, 0.05, 1, 2)]
    assert not planner_module._tariff_evidence_covers_period(
        context,
        now + timedelta(minutes=5),
        timedelta(0),
    )
    assert not planner_module._persisted_hvac_period_qualifies(
        context, now, now + timedelta(minutes=5), 0.10, 0.20, 0.20
    )
    context.slots = [
        DecisionSlot(now, 0.50, 0.05, 1, 2),
        DecisionSlot(now, 0.50, 0.05, 1, 2),
    ]
    assert not planner_module._persisted_hvac_period_qualifies(
        context,
        now,
        now + timedelta(minutes=5),
        0.10,
        0.20,
        0.20,
    )
    context.slots = [
        DecisionSlot(now, 0.50, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=5), 0.50, 0.05, 1, 2),
    ]
    assert not planner_module._persisted_hvac_period_qualifies(
        context,
        now + timedelta(minutes=20),
        now + timedelta(minutes=25),
        0.10,
        0.20,
        0.20,
    )
    context.slots = [
        DecisionSlot(now, 0.50, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=5), 0.20, 0.05, 1, 2),
    ]
    assert not planner_module._persisted_hvac_period_qualifies(
        context, now, now + timedelta(minutes=10), 0.10, 0.20, 0.20
    )
    context.slots = [DecisionSlot(now, 0.50, 0.05, 1, 2)]
    assert not planner_module._persisted_hvac_period_qualifies(
        context, now, now + timedelta(minutes=5), 0.10, 0.20, 0.20
    )
    context.slots = [DecisionSlot(now, None, 0.05, 1, 2)]
    assert not planner_module._tariff_evidence_covers_period(
        context,
        now + timedelta(minutes=5),
        timedelta(minutes=5),
    )
    context.slots = [
        DecisionSlot(now, 0.50, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=10), 0.50, 0.05, 1, 2),
    ]
    assert not planner_module._tariff_evidence_covers_period(
        context,
        now + timedelta(minutes=15),
        timedelta(minutes=5),
    )
    context.slots = [
        DecisionSlot(now, 0.50, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=5), 0.50, 0.05, 1, 2),
    ]
    assert planner_module._persisted_hvac_period_qualifies(context, now, now + timedelta(minutes=10), 0.10, 0.20, 0.20)
    context.slots = [
        DecisionSlot(now + timedelta(seconds=1), 0.50, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=5, seconds=1), 0.50, 0.05, 1, 2),
        DecisionSlot(now + timedelta(minutes=10, seconds=1), 0.50, 0.05, 1, 2),
    ]
    assert planner_module._persisted_hvac_period_qualifies(
        context,
        now + timedelta(minutes=5),
        now + timedelta(minutes=15),
        0.10,
        0.20,
        0.20,
    )


def test_hvac_lifecycle_rejects_tariff_gaps_but_can_catch_up_without_coasting() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
        "hvac_precondition_lead_minutes": 15,
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "off"
    context.current_hvac_temperature_c = 21
    context.current_outdoor_temperature_c = 5
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.slots = [
        DecisionSlot(
            context.created_at + timedelta(minutes=index * 5),
            price,
            0.05,
            1,
            2,
            outdoor_temperature_forecast_c=21,
        )
        for index, price in enumerate([0.10, None, 0.10, 0.60])
    ]
    gap_tail = [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ]
    assert gap_tail[0].desired_state["phase"] == "preconditioning"
    assert gap_tail[0].execute_not_before == context.created_at + timedelta(minutes=10)

    context.current_hvac_mode = "heat"
    context.current_outdoor_temperature_c = 5
    context.slots = [
        DecisionSlot(
            context.created_at + timedelta(hours=index),
            price,
            0.05,
            1,
            2,
            outdoor_temperature_forecast_c=5,
        )
        for index, price in enumerate([0.10, 0.10, 0.60])
    ]
    coast_limited = DryRunPlanner(
        {
            **options,
            "planning_interval_minutes": 60,
            "hvac_precondition_lead_minutes": 120,
        },
        thermal_model={
            "enabled": True,
            "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 4.0},
            "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -10.0},
        },
    ).create_plan(context)
    climate_actions = [action for action in coast_limited.actions if action.asset == ActionAsset.DAIKIN]
    assert climate_actions[0].desired_state["phase"] == "preconditioning"
    assert climate_actions[0].execute_not_before == context.created_at
    context.current_hvac_mode = "off"
    context.current_outdoor_temperature_c = 21
    for slot in context.slots:
        slot.outdoor_temperature_forecast_c = 21
    assert [
        action for action in DryRunPlanner(options).create_plan(context).actions if action.asset == ActionAsset.DAIKIN
    ] == []


def test_hvac_lifecycle_transitions_to_pre_peak_coast_after_selected_run() -> None:
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        "planning_interval_minutes": 5,
        "hvac_precondition_lead_minutes": 15,
        CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY: True,
    }
    thermal_model = {
        "enabled": True,
        "active_heat_rate_c_per_hour": {"sample_count": 4, "average": 12.0},
        "passive_indoor_drift_c_per_hour": {"sample_count": 4, "average": -1.0},
    }
    context = _context()
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "heat"
    context.current_hvac_temperature_c = 22
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.climate_zone_entities = ["climate.living_zone"]
    context.slots = [
        DecisionSlot(
            valid_at=context.created_at + timedelta(minutes=index * 5),
            import_price=price,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.0,
            outdoor_temperature_forecast_c=5.0,
        )
        for index, price in enumerate([0.05, 0.10, 0.15, 0.60, 0.10])
    ]

    actions = [
        action
        for action in DryRunPlanner(options, thermal_model=thermal_model).create_plan(context).actions
        if action.asset == ActionAsset.DAIKIN
    ]

    assert [action.desired_state.get("phase") for action in actions[:-1]] == [
        "preconditioning",
        "pre_peak_coast",
        "peak_coast",
    ]
    assert all(action.desired_state["configured_zones_only"] is True for action in actions[:-1])
    precondition_end = context.slots[2].valid_at
    assert actions[0].desired_state["precondition_end"] == precondition_end
    assert actions[1].execute_not_before == precondition_end

    period_start = context.slots[3].valid_at
    period_end = context.slots[4].valid_at
    context.hvac_control = {
        "phase": "preconditioning",
        "period_start": period_start,
        "period_end": period_end,
        "precondition_end": precondition_end,
        "baseline_price": 0.05,
        "mode": "heat",
        "precondition_target": 23.0,
        "coast_target": 19.0,
    }
    for slot in context.slots:
        slot.projected_hvac_load_kw = 0.0

    preconditioning_plan = DryRunPlanner(
        options,
        thermal_model=thermal_model,
    ).create_plan(context)

    assert preconditioning_plan.actions[0].desired_state["phase"] == "preconditioning"
    assert context.slots[0].projected_hvac_load_kw > 0.0
    assert context.slots[1].projected_hvac_load_kw > 0.0
    assert context.slots[2].projected_hvac_load_kw == 0.0

    context.created_at = precondition_end
    context.slots = context.slots[2:]

    active_action = (
        DryRunPlanner(
            options,
            thermal_model=thermal_model,
        )
        .create_plan(context)
        .actions[0]
    )

    assert active_action.desired_state["phase"] == "pre_peak_coast"
    assert active_action.desired_state["target_temperature"] == 19.0
    assert context.slots[0].projected_hvac_load_kw == 0.0
