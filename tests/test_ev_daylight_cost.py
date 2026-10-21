"""Regressions for peak-price daylight charging with cheaper ready-by capacity."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.ev import EVChargeAllocation, EVChargeSchedule
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    DaylightWindow,
    DecisionContext,
    DecisionSlot,
    InputHealth,
    OccupancyState,
)
from custom_components.ha_energy_planner.planner import DryRunPlanner
from custom_components.ha_energy_planner.planner_ev import _daylight_preferred_ev_schedule

NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)  # 19:00 AEDT
READY = datetime(2026, 10, 21, 21, tzinfo=UTC)  # Next morning 08:00 AEDT


def evening():
    slots = []
    for i in range(52):
        at = NOW + timedelta(minutes=15 * i)
        cheap = datetime(2026, 10, 21, 19, 30, tzinfo=UTC) <= at < READY
        slots.append(DecisionSlot(at, 0.093 if cheap else 0.40, 0.05, 0, 1))
    return DecisionContext(
        NOW, "peak-price", slots, None, 72, OccupancyState.OCCUPIED, InputHealth.HEALTHY,
        ev_connected=True, ev_charging=False, ev_target_soc_percent=80, ev_ready_by="08:00",
        local_timezone="Australia/Melbourne", daylight_windows=[
            DaylightWindow(NOW - timedelta(hours=10), NOW + timedelta(minutes=45)),
            DaylightWindow(READY - timedelta(hours=1, minutes=30), READY + timedelta(hours=12)),
        ],
    )


def action(ctx, mode="adaptive", **overrides):
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False,
               "ev_charging_strategy": mode, "ev_daylight_lowest_cost_charging_enabled": True,
               "planning_interval_minutes": 15, "ev_earliest_start": "None",
               "ev_charge_rate_kw": 7, "ev_soc_per_kwh": 1.1825, "max_daily_ev_actions": 0,
               **overrides}
    return next(a for a in DryRunPlanner(options).create_plan(ctx).actions if a.asset == ActionAsset.EV)


@pytest.mark.parametrize("mode", ["continuous", "split", "adaptive"])
@pytest.mark.parametrize("retained", [False, True])
def test_peak_evening_waits_for_cheaper_morning_even_with_daylight_enabled(mode, retained):
    ctx = evening()
    if retained:
        ctx.ev_evidence["retained_schedule"] = [
            {"valid_at": s.valid_at.isoformat(), "physical_power_kw": 7} for s in ctx.slots[:4]
        ]
    desired = action(ctx, mode).desired_state
    assert desired["charging_required_now"] is False
    assert all(row["import_price"] == 0.093 for row in desired["allocated_slots"])
    assert desired["optimization"]["projected_departure_soc"] == 80
    assert datetime.fromisoformat(desired["optimization"]["conservative_completion"]) <= READY - timedelta(minutes=30)
    assert not desired["infeasible"]


def test_solar_export_opportunity_cost_is_not_overridden_by_daylight():
    ctx = evening()
    for slot in ctx.slots[:3]:
        slot.pv_forecast_kw, slot.export_price = 10, 0.35
    desired = action(ctx).desired_state
    assert not desired["charging_required_now"]
    assert all(row["import_price"] == 0.093 for row in desired["allocated_slots"])


@pytest.mark.parametrize("mode", ["continuous", "split", "adaptive"])
@pytest.mark.parametrize("late_capacity", [False, True])
def test_daylight_survives_seed_cost_comparison_until_physical_readiness_is_checked(mode, late_capacity):
    ctx = evening()
    ctx.ev_target_soc_percent = 73
    ctx.daylight_windows = ctx.daylight_windows[:1]
    for slot in ctx.slots[3:]:
        if not late_capacity or slot is not ctx.slots[-1]:
            slot.baseline_load_forecast_upper_kw = 20
    desired = action(ctx, mode, ev_earliest_start="23:00").desired_state
    daylight = desired["daylight_lowest_cost"]
    assert not desired["infeasible"]
    assert desired["charging_required_now"]
    assert daylight["selected"]
    assert daylight["daylight_candidate_cost"] > daylight["ready_by_candidate_cost"]
    assert datetime.fromisoformat(desired["optimization"]["conservative_completion"]) <= READY - timedelta(minutes=30)
    assert desired["optimization"]["projected_departure_soc"] == 73


def test_partial_daylight_seed_is_displaced_when_cheaper_night_can_finish():
    ctx = evening()
    ctx.daylight_windows = ctx.daylight_windows[:1]
    desired = action(ctx, "split", ev_earliest_start="23:00").desired_state
    daylight = desired["daylight_lowest_cost"]
    assert not daylight["selected"]
    assert daylight["reason"] == "ev_daylight_candidate_not_selected"
    assert daylight["daylight_candidate_cost"] > daylight["ready_by_candidate_cost"]
    assert not desired["charging_required_now"]
    assert all(row["import_price"] == 0.093 for row in desired["allocated_slots"])


def test_daylight_selection_evidence_tracks_capacity_revalidation():
    ctx = evening()
    ctx.daylight_windows = ctx.daylight_windows[:1]
    for slot in ctx.slots:
        if slot.valid_at < NOW + timedelta(minutes=45):
            slot.import_price = 0.01
            slot.baseline_load_forecast_upper_kw = 20
    desired = action(ctx).desired_state
    assert not desired["daylight_lowest_cost"]["selected"]
    assert desired["daylight_lowest_cost"]["reason"] == "ev_daylight_candidate_not_selected"
    assert not desired["charging_required_now"]


@pytest.mark.parametrize("effective_price", [None, 0])
def test_legacy_daylight_cost_comparison_preserves_missing_and_zero_effective_price(effective_price):
    ctx = evening()
    ctx.current_ev_soc_percent = 79
    standard = EVChargeSchedule([
        EVChargeAllocation(ctx.slots[-2].valid_at, 1, 1, 0.1, effective_price=effective_price),
    ], 80, 80, 1, False, "ready_by")
    selected, evidence = _daylight_preferred_ev_schedule(
        ctx, standard_schedule=standard, enabled=True, current_soc_percent=79,
        target_soc_percent=80, ready_by=READY, earliest_start=NOW, charge_rate_kw=7,
        soc_per_kwh=1.1825, interval_minutes=15, carbon_weight=0,
        continuous=False, continue_current=False, force_current=False, max_import_price=None,
    )
    assert selected is not standard
    assert evidence["selected"]
    assert evidence["ready_by_candidate_cost"] == pytest.approx((0.1 if effective_price is None else 0) / 1.1825,
                                                                abs=0.00005)
