"""No weather-only preheating of comfortable rooms; explicit direction safety."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_constraints import _action, _plan
from test_zone_control import BED, MAIN, room_context

from custom_components.ha_energy_planner.climate_optimizer import optimise, revalidate_schedule
from custom_components.ha_energy_planner.climate_runtime import economic_actions
from custom_components.ha_energy_planner.config_flow import OptionsFlow, _option_selector, _validate_options
from custom_components.ha_energy_planner.const import CONF_HVAC_PRECONDITIONING_MODE, DEFAULT_OPTIONS
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.hvac_direction import _recent_drift, mapped_direction, mode_allowed
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, DecisionSlot
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.preconditioning import planning_status

FIXTURE = json.loads((Path(__file__).parent / "fixtures/climate/fallback_direction_oct_24_2026.json").read_text())


def options(**values):
    return {
        **DEFAULT_OPTIONS,
        "planning_interval_minutes": 15,
        "hvac_precondition_lead_minutes": 240,
        "hvac_precondition_max_lead_minutes": 240,
        **values,
    }


def replay(case):
    c = room_context(mode=case["head_mode"], main=case["main"], bed=case["bedrooms"])
    c.created_at = datetime.fromisoformat(case["at"])
    c.current_hvac_temperature_c = case["head_temperature"]
    c.current_outdoor_temperature_c = 18.3
    c.slots = [
        DecisionSlot(
            c.created_at + timedelta(minutes=15 * i),
            0.14 if i < 16 else 0.38,
            0.05,
            0,
            1,
            outdoor_temperature_forecast_c=10,
        )
        for i in range(40)
    ]
    return c


def passive(c, drift=-0.5, age=0, count=12, duration=5):
    return {
        "model_version": 3,
        "learning_enabled": True,
        "sources": {
            zone: {
                "regime": "idle",
                "buckets": {
                    "passive": {
                        "windows": [
                            {
                                "at": (c.created_at - timedelta(minutes=age + i * 5)).isoformat(),
                                "rate": drift,
                                "minutes": duration,
                                "regime": "idle",
                            }
                            for i in reversed(range(count))
                        ]
                    }
                },
            }
            for zone in c.climate_inputs["zones"]
        },
    }


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_live_snapshots_choose_room_evidence_over_cold_forecast(case):
    c = replay(case)
    mode = mapped_direction(c, options(), {}, c.slots[16].valid_at)
    assert mode == case["expected_mode"]
    assert c.climate_legacy_decision["direction"]["reason"] == case["reason"]
    candidate = HVACPlanningPolicy(options(), {})._next_hvac_period(c)
    if mode is None:
        assert candidate is None
        assert case["reason"] in c.climate_legacy_decision["rejected"]
    else:
        assert candidate["mode"] == "cool"


def test_cooling_only_remains_available_from_idle_in_mild_weather():
    c = replay(FIXTURE["cases"][0])
    opts = options(hvac_preconditioning_mode="cooling_only")
    candidate = HVACPlanningPolicy(opts, {})._next_hvac_period(c)
    assert candidate["mode"] == "cool"
    action = HVACPlanningPolicy(opts, {})._hvac_lifecycle_actions(c, c.created_at, c.slots[1].valid_at)[0]
    assert action.desired_state["precondition_target"] == 21
    assert action.desired_state["zone_targets"]
    c.climate_inputs["zones"][MAIN]["temperature"] = 21
    c.climate_inputs["zones"][BED]["temperature"] = 21
    assert mapped_direction(c, opts, {}, c.slots[16].valid_at) is None
    assert c.climate_legacy_decision["direction"]["reason"] == "room_target_reached"


@pytest.mark.parametrize(("drift", "expected"), [(-0.5, "heat"), (0.6, "cool"), (0, None)])
def test_qualified_recent_room_drift_supports_a_future_need(drift, expected):
    c = replay(FIXTURE["cases"][0])
    model = passive(c, drift)
    assert _recent_drift(model, MAIN, c) == drift
    assert mapped_direction(c, options(), model, c.slots[16].valid_at) == expected
    assert c.climate_legacy_decision["direction"]["rooms"][MAIN]["projected_peak_temperature"] == pytest.approx(
        c.climate_inputs["zones"][MAIN]["temperature"] + 4 * drift
    )
    assert mapped_direction(c, options(hvac_thermal_learning_enabled=False), model, c.slots[16].valid_at) is None


@pytest.mark.parametrize(("age", "count", "duration"), [(31, 12, 5), (0, 11, 5), (0, 12, 4), (90, 12, 5)])
def test_old_sparse_or_unqualified_drift_does_not_justify_preheating(age, count, duration):
    c = replay(FIXTURE["cases"][0])
    model = passive(c, age=age, count=count, duration=duration)
    assert _recent_drift(model, MAIN, c) is None
    assert mapped_direction(c, options(), model, c.slots[16].valid_at) is None


def test_drift_filter_rejects_incompatible_future_and_malformed_windows():
    c = replay(FIXTURE["cases"][0])
    model = passive(c)
    rows = model["sources"][MAIN]["buckets"]["passive"]["windows"]
    for value in [
        None,
        {"regime": "different"},
        {"regime": "idle", "at": "invalid"},
        {"regime": "idle", "at": (c.created_at + timedelta(hours=1)).isoformat(), "rate": -1, "minutes": 5},
        {"regime": "idle", "at": c.created_at.isoformat(), "rate": None, "minutes": 5},
    ]:
        rows.append(value)
    # The underlying qualification function also rejects malformed rows. Call
    # the recent filter with valid qualification evidence through a local fake.
    from unittest.mock import patch

    with patch("custom_components.ha_energy_planner.hvac_direction.rate", return_value=-0.5):
        assert _recent_drift(model, MAIN, c) == -0.5
        model["sources"][MAIN]["buckets"]["passive"]["windows"] = []
        assert _recent_drift(model, MAIN, c) is None


def test_current_mode_needs_no_weather_switch_and_idle_hot_rooms_can_cool():
    c = replay(FIXTURE["cases"][0])
    c.current_hvac_mode = "heat"
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) == "heat"
    c.current_hvac_mode = "off"
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 20
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) == "heat"
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 25
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) == "cool"
    assert mapped_direction(c, options(hvac_preconditioning_mode="heating_only"), {}, c.slots[16].valid_at) is None
    assert c.climate_legacy_decision["direction"]["reason"] == "preconditioning_mode_not_allowed"


def test_failed_room_feedback_and_missing_bounds_fail_closed():
    c = replay(FIXTURE["cases"][0])
    c.climate_inputs["zones"][MAIN]["temperature"] = None
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) is None
    c.climate_inputs["zones"] = {}
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) is None
    c = replay(FIXTURE["cases"][0])
    c.occupied_temperature_low_c = None
    assert mapped_direction(c, options(), {}, c.slots[16].valid_at) is None


@pytest.mark.parametrize(
    ("policy", "mode", "expected"),
    [
        ("automatic", "heat", True),
        ("automatic", "cool", True),
        ("cooling_only", "heat", False),
        ("cooling_only", "cool", True),
        ("heating_only", "heat", True),
        ("heating_only", "cool", False),
        ("invalid", "cool", False),
        ("invalid", "off", True),
        ("automatic", None, False),
    ],
)
def test_direction_permissions(policy, mode, expected):
    assert mode_allowed(options(hvac_preconditioning_mode=policy), mode) == expected


def test_old_heating_plan_is_rejected_before_execution_but_release_and_stop_survive():
    c = replay(FIXTURE["cases"][0])
    a = _action(c.created_at, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat", "target_temperature": 24})
    opts = options(planner_enabled=True, dry_run=False, hvac_preconditioning_mode="cooling_only")
    validator = ConstraintValidator(opts)
    assert "preconditioning_mode_not_allowed" in validator.validate_action(
        c, _plan(c.created_at, a), a, now=c.created_at
    )
    for kind, desired in [(ActionKind.RELEASE_HVAC, {}), (ActionKind.SET_HVAC, {"hvac_mode": "off"})]:
        a.kind, a.desired_state = kind, desired
        assert "preconditioning_mode_not_allowed" not in validator.validate_action(
            c, _plan(c.created_at, a), a, now=c.created_at
        )


@pytest.mark.parametrize("deadline", [None, "invalid", "peak"])
def test_automatic_execution_rechecks_the_rooms_before_heating_acquisition(deadline):
    c = replay(FIXTURE["cases"][0])
    desired = {"hvac_mode": "heat", "target_temperature": 24, "phase": "preconditioning"}
    if deadline is not None:
        desired["period_start"] = c.slots[16].valid_at if deadline == "peak" else deadline
    a = _action(c.created_at, ActionAsset.DAIKIN, ActionKind.SET_HVAC, desired)
    validator = ConstraintValidator(options(planner_enabled=True, dry_run=False))
    assert "hvac_heating_need_unproven" in validator.validate_action(c, _plan(c.created_at, a), a, now=c.created_at)
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 20
    assert "hvac_heating_need_unproven" not in validator.validate_action(c, _plan(c.created_at, a), a, now=c.created_at)


@pytest.mark.parametrize("economic", [False, True])
def test_policy_change_releases_existing_ownership(economic):
    c = replay(FIXTURE["cases"][0])
    c.hvac_control = {"mode": "heat", "phase": "preconditioning"}
    opts = options(hvac_preconditioning_mode="cooling_only")
    if economic:
        c.hvac_control["economic_policy_version"] = 1
        actions = economic_actions(c, opts, [])
    else:
        actions = HVACPlanningPolicy(opts, {})._hvac_lifecycle_actions(c, c.created_at, c.slots[1].valid_at)
    assert actions[0].kind == ActionKind.RELEASE_HVAC
    assert actions[0].desired_state["release_reason"] == "hvac_preconditioning_mode_changed"


def test_economic_candidates_and_incumbents_respect_direction_permissions():
    c = replay(FIXTURE["cases"][0])
    c.climate_inputs.update(load_excludes_hvac=True)
    model = {"rooms": {k: {} for k in c.climate_inputs["zones"]}, "validation": {"heat": {"blockers": []}}}
    _, decision = optimise(c, options(hvac_preconditioning_mode="cooling_only"), model)
    assert decision["rejected"]["preconditioning_mode_not_allowed"] == 1
    assert revalidate_schedule(c, options(hvac_preconditioning_mode="cooling_only"), {}, {"mode": "heat"}) is None
    # Permission alone cannot retain an incumbent with no validated time window.
    assert revalidate_schedule(c, options(hvac_preconditioning_mode="cooling_only"), {}, {"mode": "cool"}) is None


def test_unmapped_feedback_retains_legacy_policy_and_cooling_only_can_override_weather():
    c = replay(FIXTURE["cases"][0])
    c.climate_inputs = {}
    c.current_hvac_temperature_c = 23
    assert HVACPlanningPolicy(options(), {})._next_hvac_period(c)["mode"] == "heat"
    opts = options(hvac_preconditioning_mode="cooling_only")
    assert HVACPlanningPolicy(opts, {})._next_hvac_period(c)["mode"] == "cool"
    c.current_hvac_temperature_c = 20
    assert HVACPlanningPolicy(opts, {})._next_hvac_period(c) is None
    c.current_hvac_temperature_c = 23
    c.current_hvac_mode = "cool"
    c.slots = [
        DecisionSlot(s.valid_at, s.import_price, s.export_price, 0, 1, outdoor_temperature_forecast_c=30)
        for s in c.slots
    ]
    assert HVACPlanningPolicy(options(hvac_preconditioning_mode="heating_only"), {})._next_hvac_period(c) is None


def test_option_validation_and_direction_diagnostics():
    opts = deepcopy(DEFAULT_OPTIONS)
    assert opts[CONF_HVAC_PRECONDITIONING_MODE] == "automatic"
    assert not _validate_options(opts)
    opts[CONF_HVAC_PRECONDITIONING_MODE] = "invalid"
    assert _validate_options(opts)[CONF_HVAC_PRECONDITIONING_MODE] == "invalid_preconditioning_mode"
    assert _option_selector(CONF_HVAC_PRECONDITIONING_MODE).serialize()["selector"]["select"]["options"] == [
        "automatic",
        "cooling_only",
        "heating_only",
    ]
    c = replay(FIXTURE["cases"][0])
    HVACPlanningPolicy(options(), {})._next_hvac_period(c)
    from custom_components.ha_energy_planner.models import PlannerMode

    result = planning_status(c, [], PlannerMode.ACTIVE_HEALTHY, "", options())
    assert result["direction"]["reason"] == "room_demand_unproven"


def test_direction_setting_round_trips_and_survives_unrelated_edits():
    entry = SimpleNamespace(data={}, options=options())
    for submitted in [{CONF_HVAC_PRECONDITIONING_MODE: "cooling_only"}, {"hvac_precondition_lead_minutes": 180}]:
        flow = OptionsFlow(entry)
        flow.hass = SimpleNamespace(config_entries=SimpleNamespace())
        result = asyncio.run(flow.async_step_settings({"climate_inputs": submitted}))
        assert result["type"] == "create_entry"
        assert result["data"][CONF_HVAC_PRECONDITIONING_MODE] == "cooling_only"
        assert result["data"]["hvac_precondition_max_lead_minutes"] == 240
        entry.options = result["data"]
        reopened = OptionsFlow(entry)
        climate = next(
            group for marker, group in reopened._settings_schema().schema.items() if marker.schema == "climate_inputs"
        )
        marker = next(marker for marker in climate.schema.schema if marker.schema == CONF_HVAC_PRECONDITIONING_MODE)
        assert marker.default() == "cooling_only"


@pytest.mark.parametrize("economic_reason", [None, "zone_subset_economic_model_unvalidated", "model_degraded"])
def test_direction_reason_does_not_hide_economic_safety_or_learning_state(economic_reason):
    from custom_components.ha_energy_planner.models import PlannerMode

    c = replay(FIXTURE["cases"][0])
    opts = options(climate_control_enabled=True)
    HVACPlanningPolicy(opts, {})._next_hvac_period(c)
    if economic_reason:
        c.climate_decision = {
            "reason": economic_reason,
            "summary": "Economic evidence status.",
            "preconditioning_status": "blocked" if economic_reason == "model_degraded" else "learning",
        }
    status = planning_status(c, [], PlannerMode.ACTIVE_HEALTHY, "", opts)
    assert status["reason"] == ("model_degraded" if economic_reason == "model_degraded" else "room_demand_unproven")
