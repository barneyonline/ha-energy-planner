"""Room demand must precede tariff acquisition regardless of device mode or energy opportunity."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_constraints import _action, _plan
from test_hvac_direction import options, passive, replay
from test_zone_control import BED, MAIN

from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.hvac_direction import mapped_direction
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, DecisionSlot, OccupancyState, Override
from custom_components.ha_energy_planner.ownership import OwnershipState
from custom_components.ha_energy_planner.planner import DryRunPlanner
from custom_components.ha_energy_planner.planner_hvac import (
    HVACPlanningPolicy,
    _effective_passive_drift_c_per_hour,
    _future_hvac_mode,
    _room_coast_hours,
)

LIVE = json.loads((Path(__file__).parent / "fixtures/climate/demand_oct_26_2026.json").read_text())


def today(mode="heat", *, main=21.52, bed=19.48):
    # Unmapped head feedback is a synthetic placeholder: direction uses the measured rooms.
    c = replay({**LIVE["case"], "head_temperature": 19})
    c.current_hvac_mode = mode
    c.climate_inputs["zones"][MAIN]["temperature"] = main
    c.climate_inputs["zones"][BED]["temperature"] = bed
    return c


@pytest.mark.parametrize("mode", ["heat", "cool", "off"])
@pytest.mark.parametrize("outdoor", [-10, 16, 40])
def test_october_26_mixed_rooms_do_not_justify_whole_unit_preheating(mode, outdoor):
    c = today(mode)
    c.current_outdoor_temperature_c = outdoor
    for slot in c.slots:
        slot.outdoor_temperature_forecast_c = outdoor
        slot.pv_forecast_kw = 10
    c.current_battery_soc_percent = 100
    c.climate_inputs.update(irradiance=740.17, humidity=55)
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) is None
    direction = c.climate_legacy_decision["direction"]
    assert direction["reason"] == "conflicting_room_demand"
    assert direction["equipment_mode"] == mode
    assert direction["evaluated_peak_at"] == c.slots[16].valid_at.isoformat()
    assert all(r["projected_peak_temperature"] is None for r in direction["rooms"].values())
    plan = DryRunPlanner(options(planner_enabled=True, dry_run=False, climate_control_enabled=True)).create_plan(c)
    assert not [a for a in plan.actions if a.asset == ActionAsset.DAIKIN and a.kind == ActionKind.SET_HVAC]
    assert plan.device_plans["climate"]["preconditioning"]["reason"] == "conflicting_room_demand"


@pytest.mark.parametrize("mode", ["heat", "cool", "off"])
def test_comfortable_rooms_cannot_acquire_just_because_equipment_is_running(mode):
    c = today(mode, main=22, bed=22)
    assert HVACPlanningPolicy(options(), {})._next_hvac_period(c) is None
    assert c.climate_legacy_decision["reason"] == "room_demand_unproven"


@pytest.mark.parametrize("mode,temperature", [("heat", 20), ("cool", 25)])
def test_actual_demand_can_select_the_opposite_device_mode(mode, temperature):
    c = today("cool" if mode == "heat" else "heat", main=temperature, bed=temperature)
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) == mode


@pytest.mark.parametrize("mode,drift", [("heat", -0.5), ("cool", 0.6)])
def test_qualified_indoor_prediction_can_create_demand_but_device_mode_cannot(mode, drift):
    c = today("cool" if mode == "heat" else "heat", main=22, bed=22)
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) is None
    assert mapped_direction(c, options(), passive(c, drift), c.slots[16].valid_at) == mode


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_new_acquisition_rechecks_direction_while_same_lifecycle_can_continue(mode):
    c = today(mode, main=22, bed=22)
    desired = {
        "hvac_mode": mode,
        "mode": mode,
        "phase": "preconditioning",
        "lifecycle_id": "owned",
        "period_start": c.slots[16].valid_at,
        "target_temperature": 24 if mode == "heat" else 21,
    }
    a = _action(c.created_at, ActionAsset.DAIKIN, ActionKind.SET_HVAC, desired)
    validator = ConstraintValidator(options(planner_enabled=True, dry_run=False))
    reason = "hvac_heating_need_unproven" if mode == "heat" else "hvac_cooling_need_unproven"
    assert reason in validator.validate_action(c, _plan(c.created_at, a), a, now=c.created_at)
    c.hvac_control = deepcopy(desired)
    ownership = OwnershipState(
        planner_takeover_started_at=c.created_at - timedelta(minutes=1), hvac_control_phase="preconditioning"
    )
    assert reason not in validator.validate_action(c, _plan(c.created_at, a), a, now=c.created_at, ownership=ownership)


@pytest.mark.parametrize("mode,outdoor", [("heat", 5), ("cool", 40)])
def test_outdoor_weather_cannot_fabricate_mapped_indoor_coast(mode, outdoor):
    c = today(mode)
    c.current_outdoor_temperature_c = outdoor
    start, boundary = (24, 21) if mode == "heat" else (21, 24)
    assert _room_coast_hours(c, {}, mode, start, boundary) is None
    assert _effective_passive_drift_c_per_hour(c, mode, {"passive_indoor_drift_c_per_hour": -0.5}) is None
    model = passive(c, -0.5 if mode == "heat" else 0.5)
    assert _room_coast_hours(c, model, mode, start, boundary) == 6
    model["sources"][BED]["buckets"].clear()
    assert _room_coast_hours(c, model, mode, start, boundary) is None


@pytest.mark.parametrize("mode", ["heat", "cool", "off"])
def test_unmapped_mode_is_not_itself_a_direction_signal(mode):
    c = today(mode)
    c.climate_inputs = {}
    c.current_outdoor_temperature_c = None
    c.slots[16].outdoor_temperature_forecast_c = None
    assert _future_hvac_mode(c, c.slots[16], 22.5, 21, 24) is None


def test_current_manual_override_and_unknown_occupancy_retain_authority():
    c = today(main=20, bed=20)
    opts = options(planner_enabled=True, dry_run=False, climate_control_enabled=True)
    for override, occupancy in [(True, OccupancyState.OCCUPIED), (False, OccupancyState.UNKNOWN)]:
        c.active_overrides = [Override("manual_hvac", "test", None, "manual")] if override else []
        c.occupancy_state = occupancy
        plan = DryRunPlanner(opts).create_plan(c)
        assert not [a for a in plan.actions if a.asset == ActionAsset.DAIKIN and a.kind == ActionKind.SET_HVAC]


def test_exact_recorded_tariff_snapshot_does_not_authorise_the_deployed_heat_24():
    c = today()
    # Retain the observed half-hour forecast and query-time distinction.
    c.slots = [DecisionSlot(datetime.fromisoformat(row["time"]), row["value"], 0, 0, 1) for row in LIVE["tariffs"]]
    c.current_outdoor_temperature_c = None
    assert mapped_direction(c, options(), {}, c.slots[2].valid_at) == LIVE["expected_mode"]
    assert c.climate_legacy_decision["direction"]["reason"] == LIVE["expected_reason"]
    assert LIVE["deployed_selection"]["reason"] == "existing_equipment_mode"
    assert HVACPlanningPolicy(options(), {})._next_hvac_period(c) is None


def test_qualified_passive_cooling_and_missing_prices_do_not_create_unnecessary_acquisition():
    from test_zone_temperature import ZONE, context, mature

    c = context()
    model = mature(value=1)
    model["sources"][ZONE]["buckets"]["passive"] = {
        "count": 12,
        "minutes": 60,
        "last_at": c.created_at.isoformat(),
        "rate_c_per_hour": -5,
    }
    for index, slot in enumerate(c.slots):
        if index < 12:
            slot.import_price = None
    # Cooling reaches the room goal unaided after an hour. Earlier starts have
    # incomplete prices; waiting must not turn this into a pointless command.
    opts = options(
        hvac_preconditioning_mode="cooling_only",
        planning_interval_minutes=5,
        hvac_precondition_lead_minutes=180,
        hvac_precondition_max_lead_minutes=180,
    )
    assert HVACPlanningPolicy(opts, model)._next_hvac_period(c) is None
    assert "comfort_target_reached" in c.climate_legacy_decision["rejected"]
    assert "forecast_gap" in c.climate_legacy_decision["rejected"]


def test_only_preparation_slots_with_sufficient_price_difference_qualify():
    from test_zone_temperature import ZONE, context, mature

    c = context()
    model = mature(value=5)
    model["sources"][ZONE]["buckets"]["passive"] = {
        "count": 12,
        "minutes": 60,
        "last_at": c.created_at.isoformat(),
        "rate_c_per_hour": 0.5,
    }
    for index, slot in enumerate(c.slots):
        slot.import_price = 0.05 if index < 6 else 0.2 if index < 36 else 0.35
    opts = options(
        hvac_preconditioning_mode="cooling_only",
        planning_interval_minutes=5,
        hvac_precondition_lead_minutes=180,
        hvac_precondition_max_lead_minutes=180,
    )
    candidate = HVACPlanningPolicy(opts, model)._next_hvac_period(c)
    assert candidate is not None
    assert candidate["mode"] == "cool"
    assert candidate["precondition_start"] < c.created_at + timedelta(minutes=30)
    assert candidate["period_start"] == c.created_at + timedelta(hours=3)
