"""Tests for Energy Planner sensor entities."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.ha_energy_planner import sensor as sensor_module
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    EnergyPlan,
    InputHealth,
    PlanAction,
    PlannerMode,
)
from custom_components.ha_energy_planner.sensor import SENSORS, PlannerSensor


def test_sensors_expose_safe_empty_values_without_plan() -> None:
    coordinator = _coordinator(None)
    values = {description.key: description.value_fn(coordinator) for description in SENSORS}
    attrs = {description.key: description.attrs_fn(coordinator) for description in SENSORS}

    assert values == {
        "next_action": "None",
        "plan_status": "Unknown",
        "estimated_daily_cost": None,
        "forecast_confidence": None,
        "confidence_breakdown": "Unknown",
        "decision_audit": "Unknown",
        "rejected_actions": "Unknown",
        "upcoming_timeline": "Unknown",
        "production_readiness": "Not Ready",
        "control_block_reason": "Production Gate Not Armed",
        "execution_audit": "No Activity",
        "dry_run_comparison": "No Dry Run",
        "support_bundle_summary": "No Plan",
        "ai_advice": "Disabled",
        "climate_plan": "Unknown",
        "climate_decision": "Unknown",
        "climate_current_state": "Unknown",
        "climate_next_state": "Unknown",
        "presence_state": "Unknown",
        "enphase_plan": "Unknown",
        "enphase_decision": "Unknown",
        "enphase_current_state": "Unknown",
        "enphase_next_state": "Unknown",
        "ev_charging_plan": "Unknown",
        "ev_decision": "Unknown",
        "ev_current_state": "Unknown",
        "ev_next_state": "Unknown",
        "ev_current_charge_state": "Unknown",
        "ev_next_charge_state": "Unknown",
    }
    assert attrs["next_action"] == {}
    assert attrs["plan_status"] == {}
    assert attrs["ai_advice"] == {"enabled": False, "latest": None}
    assert attrs["presence_state"] == {"person_entities": []}


def test_operational_summary_sensors_expose_production_audit_and_support_context() -> None:
    plan = _plan(
        input_issues=[
            "pv_forecast_entity_unavailable",
            "ev_soc_entity_unavailable",
            "weather_entity_unavailable",
        ]
    )
    plan.health = InputHealth.UNSAFE
    coordinator = _coordinator(
        plan,
        options={
            "ev_control_enabled": True,
            "climate_control_enabled": True,
            "enphase_control_enabled": True,
            "ai_enabled": True,
        },
        entry_data={
            "ev_smart_charging_start_entity": "button.ev_start",
            "daikin_climate_entity": "climate.home",
            "enphase_profile_entity": "select.enphase_profile",
        },
        store_data={
            "production": {
                "armed": True,
                "dry_run_ready_cycles": 3,
                "last_dry_run_ready_at": "2026-06-27T00:00:00+00:00",
            },
            "control_pause": {
                "active": True,
                "until": "2026-06-27T01:00:00+00:00",
                "assets": ["ev"],
            },
            "execution_audit": [
                {"result": "applied", "action_id": "ev-start"},
                {"result": "rejected", "action_id": "ev-stop"},
            ],
            "dry_run_comparisons": [
                {"plan_id": "plan-1", "planned_action_count": 2},
            ],
            "ai_recommendations": [
                {
                    "status": "accepted",
                    "accepted": {"reasoning_summary": "Looks OK"},
                }
            ],
        },
    )

    confidence = next(item for item in SENSORS if item.key == "confidence_breakdown")
    production = next(item for item in SENSORS if item.key == "production_readiness")
    block = next(item for item in SENSORS if item.key == "control_block_reason")
    audit = next(item for item in SENSORS if item.key == "execution_audit")
    comparison = next(item for item in SENSORS if item.key == "dry_run_comparison")
    support = next(item for item in SENSORS if item.key == "support_bundle_summary")

    assert confidence.value_fn(coordinator) == "87.5%"
    assert confidence.attrs_fn(coordinator)["breakdown"]["pv"]["status"] == "degraded"
    assert production.value_fn(coordinator) == "Armed"
    assert production.attrs_fn(coordinator)["ready_to_arm"] is True
    assert production.attrs_fn(coordinator)["dry_run_evidence_complete"] is True
    assert block.value_fn(coordinator) == "Planner Paused"
    assert block.attrs_fn(coordinator)["reasons"] == [
        "planner_paused",
        "pv_forecast_entity_unavailable",
        "ev_soc_entity_unavailable",
        "weather_entity_unavailable",
    ]
    assert audit.value_fn(coordinator) == "Rejected"
    assert audit.attrs_fn(coordinator)["outcome_count"] == 2
    assert comparison.value_fn(coordinator) == "2 Planned"
    assert comparison.attrs_fn(coordinator)["latest"]["plan_id"] == "plan-1"
    assert support.value_fn(coordinator) == "Needs Review"
    assert support.attrs_fn(coordinator)["latest_ai"]["reasoning_summary"] == "Looks OK"


def test_decision_audit_sensors_expose_accepted_rejected_and_timeline_rows() -> None:
    plan = _plan()
    plan.decision_audit = {
        "summary": "Selected 1 action.",
        "policy_order": ["cost", "comfort"],
        "marginal_budget": {"forecast_surplus_kwh": 2.5},
        "accepted": [
            {
                "action_id": "ev-1",
                "device": "EV",
                "action": "EV Schedule",
                "score": 0.8,
                "reason": "the EV needs charge before its ready-by time",
            }
        ],
    }
    plan.rejected_actions = [
        {
            "device": "Enphase",
            "action": "Change battery profile",
            "reason": "Skipped Enphase profile change because EV charging had higher marginal value.",
        }
    ]
    plan.timeline_card = [
        {
            "time": "12:00-12:30",
            "device": "EV",
            "action": "Charging",
            "reason": "Solar surplus",
            "estimated_kwh": 3.5,
            "estimated_value": 0.2,
        }
    ]
    coordinator = _coordinator(plan)

    assert next(item for item in SENSORS if item.key == "decision_audit").value_fn(coordinator) == "1 Accepted"
    assert next(item for item in SENSORS if item.key == "rejected_actions").value_fn(coordinator) == "1 Rejected"
    assert next(item for item in SENSORS if item.key == "upcoming_timeline").value_fn(coordinator) == "1 Upcoming"
    assert next(item for item in SENSORS if item.key == "ev_decision").value_fn(coordinator) == "Accepted"
    assert next(item for item in SENSORS if item.key == "enphase_decision").value_fn(coordinator) == "Rejected"
    ev_attrs = next(item for item in SENSORS if item.key == "ev_decision").attrs_fn(coordinator)
    timeline_attrs = next(item for item in SENSORS if item.key == "upcoming_timeline").attrs_fn(coordinator)
    assert ev_attrs["summary"] == "EV action was selected because the EV needs charge before its ready-by time."
    assert timeline_attrs["rows"][0]["estimated_kwh"] == 3.5

    empty_plan = _plan()
    empty_coordinator = _coordinator(empty_plan)
    assert next(item for item in SENSORS if item.key == "decision_audit").attrs_fn(empty_coordinator)["accepted"] == []
    assert (
        next(item for item in SENSORS if item.key == "rejected_actions").attrs_fn(empty_coordinator)["rejected"] == []
    )
    assert (
        sensor_module._device_decision_summary(ActionAsset.DAIKIN, None, {"device": "Climate"})
        == "Climate action was considered but not selected."
    )
    assert (
        sensor_module._device_decision_summary(ActionAsset.DAIKIN, None, None)
        == "Climate was not considered in this planning run."
    )


def test_confidence_breakdown_explains_score_and_improvement_actions() -> None:
    plan = _plan()
    plan.confidence = 0.7
    coordinator = _coordinator(
        plan,
        store_data={
            "forecast_snapshots": [
                {
                    "plan_id": "plan-1",
                    "confidence": {
                        "overall": 0.7,
                        "forecast_source_confidence": 0.7,
                        "sources": [
                            {
                                "config_key": "pv_forecast_entity",
                                "entity_id": "sensor.pv",
                                "source": "point_value_repeated",
                                "confidence": 0.7,
                            },
                            {
                                "config_key": "amber_import_price_entity",
                                "entity_id": "sensor.import",
                                "source": "forecast_series",
                                "confidence": 1.0,
                            },
                        ],
                    },
                }
            ]
        },
    )
    confidence = next(item for item in SENSORS if item.key == "confidence_breakdown")

    attrs = confidence.attrs_fn(coordinator)

    assert attrs["calculation"] == {
        "formula": "overall = min(input_health_score, forecast_source_confidence)",
        "overall": 0.7,
        "overall_percent": 70.0,
        "input_health_score": 1.0,
        "input_health_percent": 100.0,
        "forecast_source_confidence": 0.7,
        "forecast_source_percent": 70.0,
        "limiting_factor": "forecast_sources",
    }
    assert attrs["source_confidence"][0]["reason"] == (
        "Only a current point value was found, so it is repeated across the planning horizon at 70% confidence."
    )
    assert attrs["improvement_actions"] == [
        "Replace PV forecast (sensor.pv) with an entity that exposes forecast data for the planning horizon, "
        "or add source confidence metadata."
    ]


def test_confidence_helper_edge_cases_are_readable() -> None:
    assert sensor_module._confidence_health_score(InputHealth.DEGRADED) == 0.65
    assert sensor_module._forecast_source_confidence(_coordinator(None)) is None

    plan = _plan()
    plan.confidence = 0.8
    assert sensor_module._forecast_source_confidence(_coordinator(plan)) == 0.8
    assert sensor_module._latest_forecast_snapshot(_coordinator(plan, store_data={"forecast_snapshots": "bad"})) == {}
    assert (
        sensor_module._confidence_sources(
            _coordinator(
                plan,
                store_data={"forecast_snapshots": [{"plan_id": "plan-1", "confidence": {"sources": "bad"}}]},
            )
        )
        == []
    )
    assert sensor_module._confidence_limiting_factor(0.0, 0.0, 0.0) == "unsafe_inputs"
    assert sensor_module._confidence_limiting_factor(0.65, 0.65, None) == "input_health"
    assert sensor_module._confidence_limiting_factor(1.0, 1.0, None) == "unknown"
    assert sensor_module._confidence_limiting_factor(0.65, 0.65, 0.65) == "input_health_and_forecast_sources"
    assert sensor_module._confidence_limiting_factor(0.65, 0.65, 0.9) == "input_health"
    assert sensor_module._confidence_limiting_factor(0.8, 0.9, 0.7) == "unknown"
    assert sensor_module._confidence_source_reason({"source": "invalid_state"}) == (
        "The entity state could not be converted into usable forecast data."
    )
    assert sensor_module._confidence_source_reason({"source": "other"}) == "Confidence source was not classified."

    assert sensor_module._confidence_improvement_actions(
        0.4,
        1.0,
        0.4,
        [{"input": "Load", "entity_id": "sensor.load", "source": "Invalid State", "confidence": 0.4}],
        {},
    ) == ["Fix Load (sensor.load) so it has a numeric usable state."]
    assert sensor_module._confidence_improvement_actions(
        0.5,
        1.0,
        0.5,
        [{"input": "PV", "entity_id": "sensor.pv", "source": "Forecast Series", "confidence": 0.5}],
        {},
    ) == ["Improve PV (sensor.pv) source confidence or data quality."]
    assert sensor_module._confidence_improvement_actions(
        0.65,
        0.65,
        1.0,
        [],
        {"pv": {"issues": ["pv_forecast_entity_unavailable", "pv_forecast_entity_stale"]}},
    ) == ["Resolve pv input issue(s): pv_forecast_entity_unavailable, pv_forecast_entity_stale."]
    assert sensor_module._confidence_improvement_actions(0.8, 1.0, None, [], {}) == [
        "Use forecast-capable entities with confidence metadata for price, PV, load, and weather inputs."
    ]
    assert sensor_module._confidence_improvement_actions(1.0, 1.0, 1.0, [], {}) == [
        "Confidence is already at 100%; no action is needed."
    ]


def test_operational_summary_sensors_handle_edge_shapes() -> None:
    ready = _coordinator(
        _plan(),
            options={
            "ev_control_enabled": True,
            "climate_control_enabled": True,
                "enphase_control_enabled": True,
            },
            entry_data={
                "ev_smart_charging_start_entity": "button.ev_start",
                "daikin_climate_entity": "climate.home",
                "enphase_profile_entity": "select.enphase_profile",
            },
        store_data={
            "production": {"dry_run_ready_cycles": 3},
            "execution_audit": ["invalid"],
            "dry_run_comparisons": ["invalid"],
        },
    )
    production = next(item for item in SENSORS if item.key == "production_readiness")
    audit = next(item for item in SENSORS if item.key == "execution_audit")
    comparison = next(item for item in SENSORS if item.key == "dry_run_comparison")
    support = next(item for item in SENSORS if item.key == "support_bundle_summary")

    assert production.value_fn(ready) == "Evidence Complete"
    assert audit.value_fn(ready) == "Unknown"
    assert audit.attrs_fn(_coordinator(_plan(), store_data={"execution_audit": "bad"})) == {
        "outcome_count": 0,
        "latest": None,
        "recent": [],
    }
    assert comparison.value_fn(ready) == "Unknown"
    assert comparison.attrs_fn(_coordinator(_plan(), store_data={"dry_run_comparisons": []})) == {}
    assert support.value_fn(ready) == "Ready"
    assert sensor_module._pause_active({}) is False


def test_production_readiness_supports_ev_only_installation() -> None:
    coordinator = _coordinator(
        _plan(),
        options={
            "ev_control_enabled": True,
            "climate_control_enabled": False,
            "enphase_control_enabled": False,
        },
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
        store_data={"production": {"dry_run_ready_cycles": 3}},
    )
    production = next(item for item in SENSORS if item.key == "production_readiness")

    assert production.value_fn(coordinator) == "Evidence Complete"
    assert production.attrs_fn(coordinator)["required_control_areas"] == ["ev"]


def test_sensor_platform_setup_groups_planner_sensors(monkeypatch: object) -> None:
    coordinator = _coordinator(_plan())
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[tuple[object, object, object]] = []

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.append((entry_arg, add_entities, list(entities)))

    monkeypatch.setattr(sensor_module, "async_add_planner_entities", fake_add_planner_entities)

    asyncio.run(sensor_module.async_setup_entry(None, entry, "add_entities"))

    assert added[0][0] is entry
    assert added[0][1] == "add_entities"
    assert len(added[0][2]) == len(SENSORS)


def test_planner_sensor_delegates_value_and_attributes() -> None:
    coordinator = _coordinator(_plan())
    description = next(item for item in SENSORS if item.key == "plan_status")
    sensor = PlannerSensor(coordinator, description)

    assert sensor.native_value == "Current"
    assert sensor.native_unit_of_measurement is None
    assert sensor.extra_state_attributes["plan_id"] == "plan-1"


def test_estimated_cost_sensor_uses_home_assistant_currency_and_horizon() -> None:
    plan = _plan()
    plan.estimated_cost_horizon_hours = 6.5
    coordinator = _coordinator(plan, hass=SimpleNamespace(config=SimpleNamespace(currency="NZD")))
    description = next(item for item in SENSORS if item.key == "estimated_daily_cost")
    sensor = PlannerSensor(coordinator, description)

    assert sensor.native_unit_of_measurement == "NZD"
    assert sensor.extra_state_attributes == {"cost_horizon_hours": 6.5}


def test_forecast_confidence_exposes_compact_calibration_uncertainty() -> None:
    coordinator = _coordinator(
        _plan(),
        store_data={
            "forecast_calibration": {
                "pv_forecast_kw": {
                    "sample_count": 52,
                    "buckets": {
                        "0": {
                            "enabled": True,
                            "factor": 0.9,
                            "lower_factor": 0.7,
                            "upper_factor": 1.1,
                            "holdout_sample_count": 12,
                            "raw_abs_pct_error_sum": 4.0,
                            "calibrated_abs_pct_error_sum": 3.0,
                        }
                    },
                }
            }
        },
    )
    description = next(item for item in SENSORS if item.key == "forecast_confidence")

    attrs = description.attrs_fn(coordinator)

    assert attrs["calibration_enabled"] is True
    assert attrs["fields"]["pv_forecast_kw"]["enabled_lead_buckets"] == 1
    assert attrs["fields"]["pv_forecast_kw"]["lead_buckets"]["0"]["lower_factor"] == 0.7


def test_forecast_calibration_attributes_reject_malformed_store_shapes() -> None:
    assert sensor_module._forecast_calibration_attrs(
        _coordinator(_plan(), store_data={"forecast_calibration": "invalid"})
    ) == {"calibration_enabled": False, "fields": {}}

    attrs = sensor_module._forecast_calibration_attrs(
        _coordinator(
            _plan(),
            store_data={
                "forecast_calibration": {
                    "pv_forecast_kw": "invalid",
                    "baseline_load_forecast_kw": {"sample_count": 1, "buckets": "invalid"},
                }
            },
        )
    )
    assert attrs == {
        "calibration_enabled": False,
        "fields": {
            "baseline_load_forecast_kw": {
                "sample_count": 1,
                "enabled_lead_buckets": 0,
                "uncertainty_enabled_lead_buckets": 0,
                "lead_buckets": {},
            }
        },
    }


def test_latest_store_item_rejects_malformed_history() -> None:
    assert sensor_module._latest_store_item(None) is None
    assert sensor_module._latest_store_item([]) is None
    assert sensor_module._latest_store_item(["invalid"]) is None
    assert sensor_module._latest_store_item([{"status": "ready"}]) == {"status": "ready"}


def test_plan_status_attributes_are_json_friendly_and_bounded() -> None:
    plan = _plan(
        preview=[{"slot": index} for index in range(20)],
        input_issues=[f"issue_{index}" for index in range(30)],
    )
    coordinator = _coordinator(plan)
    description = next(item for item in SENSORS if item.key == "plan_status")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Current"
    assert attrs["mode"] == "ACTIVE_HEALTHY"
    assert attrs["health"] == "healthy"
    assert len(attrs["issues"]) == 20
    assert len(attrs["preview"]) == 12


def test_next_action_sensor_exposes_plain_english_action() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = PlanAction(
        action_id="ev-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"target_soc_percent": 80},
        hard_constraints=["ev_bounds"],
        reason_codes=["least_cost_slots_before_ready_by"],
        expected_cost_delta=-0.25,
        confidence=0.8,
        requires_haeo_plan_id=None,
    )
    plan = _plan(actions=[action])
    coordinator = _coordinator(plan)
    description = next(item for item in SENSORS if item.key == "next_action")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Schedule EV charging"
    assert attrs == {
        "action": "Schedule EV charging",
        "decision": "Schedule EV charging to 80%.",
        "when": "00:00-00:05",
        "why": "Charging was placed in the cheapest slots before the ready-by time.",
        "constraints": ["EV Bounds"],
        "desired_state": {"Target SOC percent": 80},
        "estimated_value": -0.25,
        "confidence": "80.0%",
        "requires_haeo_plan": False,
    }


def test_asset_plan_sensors_expose_device_specific_actions() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    climate_action = PlanAction(
        action_id="climate-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"hvac_mode": "cool", "target_temperature": 22},
        hard_constraints=["comfort"],
        reason_codes=["hvac_precondition_before_expensive_period"],
        expected_cost_delta=0.4,
        confidence=0.7,
        requires_haeo_plan_id=None,
    )
    ev_action = PlanAction(
        action_id="ev-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"target_soc_percent": 80, "allocated_slots": [{"slot": index} for index in range(20)]},
        hard_constraints=["ev_min_soc"],
        reason_codes=["least_cost_slots_before_ready_by"],
        expected_cost_delta=None,
        confidence=0.8,
        requires_haeo_plan_id="plan-1",
    )
    plan = _plan(actions=[climate_action, ev_action])
    coordinator = _coordinator(plan, store_data={"trip_history": {"records": [{"soc": 10}, {"soc": 20}]}})

    climate = next(item for item in SENSORS if item.key == "climate_plan")
    ev = next(item for item in SENSORS if item.key == "ev_charging_plan")

    assert climate.value_fn(coordinator) == "Change climate state"
    climate_attrs = climate.attrs_fn(coordinator)
    assert climate_attrs["planned_actions"][0]["decision"] == "Set climate to Cool at 22 C."
    assert climate_attrs["planned_actions"][0]["why"] == "Preconditioning before a more expensive electricity period."
    assert climate_attrs["planned_actions"][0]["desired_state"]["Target temperature C"] == 22
    assert ev.value_fn(coordinator) == "Schedule EV charging"
    ev_attrs = ev.attrs_fn(coordinator)
    assert ev_attrs["trip_history_record_count"] == 2
    assert ev_attrs["planned_actions"][0]["desired_state"]["Charging windows"] == 20


def test_asset_plan_sensors_filter_issues_to_device() -> None:
    plan = _plan(
        input_issues=[
            "amber_import_price_entity_unavailable",
            "amber_export_price_entity_unavailable",
            "pv_forecast_entity_unavailable",
            "baseline_load_forecast_entity_unavailable",
            "battery_soc_entity_unavailable",
            "ev_soc_entity_unavailable",
            "ev_connected_entity_unavailable",
            "enphase_profile_entity_unavailable",
            "daikin_climate_entity_unavailable",
            "daikin_power_entity_unavailable",
        ]
    )
    coordinator = _coordinator(plan)
    ev = next(item for item in SENSORS if item.key == "ev_charging_plan")
    climate = next(item for item in SENSORS if item.key == "climate_plan")
    enphase = next(item for item in SENSORS if item.key == "enphase_plan")

    assert ev.attrs_fn(coordinator)["issues"] == [
        "Amber Import Price Entity Unavailable",
        "EV SOC Entity Unavailable",
        "EV Connected Entity Unavailable",
    ]
    assert climate.attrs_fn(coordinator)["issues"] == [
        "Amber Import Price Entity Unavailable",
        "Daikin Climate Entity Unavailable",
        "Daikin Power Entity Unavailable",
    ]
    assert enphase.attrs_fn(coordinator)["issues"] == [
        "Amber Import Price Entity Unavailable",
        "Amber Export Price Entity Unavailable",
        "PV Forecast Entity Unavailable",
        "Baseline Load Forecast Entity Unavailable",
        "Battery SOC Entity Unavailable",
        "Enphase Profile Entity Unavailable",
    ]


def test_asset_plan_sensors_expose_device_timeline() -> None:
    plan = _plan(
        device_plans={
            "climate": {
                "horizon_hours": 24,
                "interval_minutes": 5,
                "total_estimated_energy_kwh": 1.2,
                "current_state": {
                    "state": "heat",
                    "hvac_mode": "heat",
                    "current_temperature": 21.5,
                },
                "current_state_label": "Heat (21.5 C)",
                "next_planned_state": {
                    "state": "preconditioning",
                    "hvac_mode": "heat",
                    "target_temperature": 22,
                },
                "next_planned_state_label": "Preconditioning: Heat to 22 C",
                "timeline": [
                    {
                        "start": "2026-06-27T00:00:00+00:00",
                        "end": "2026-06-27T00:30:00+00:00",
                        "state": "preconditioning",
                        "target_temperature": 22,
                    }
                ],
            }
        }
    )
    coordinator = _coordinator(plan)
    climate = next(item for item in SENSORS if item.key == "climate_plan")

    attrs = climate.attrs_fn(coordinator)

    assert attrs["horizon_hours"] == 24
    assert attrs["interval_minutes"] == 5
    assert attrs["total_estimated_energy_kwh"] == 1.2
    assert "current_state_label" not in attrs
    assert "current_state" not in attrs
    assert "next_planned_state_label" not in attrs
    assert "next_planned_state" not in attrs
    assert attrs["timeline_segment_count"] == 1
    assert attrs["timeline_summary"] == ["00:00-00:30: Preconditioning."]


def test_asset_state_sensors_expose_current_and_next_labels() -> None:
    plan = _plan(
        device_plans={
            "climate": {
                "current_state": {"state": "heat", "hvac_mode": "heat"},
                "current_state_label": "Heat (21.5 C)",
                "next_planned_state": {"state": "off", "hvac_mode": "off"},
                "next_planned_state_label": "Off",
                "timeline": [{"state": "idle"}],
            },
            "enphase": {
                "timeline": [
                    {"state": "idle", "start": "2026-06-27T00:00:00+00:00", "end": "2026-06-27T00:30:00+00:00"},
                    {
                        "state": "charge_battery",
                        "battery_charge_kw": 2.5,
                        "battery_soc_percent": 50,
                        "start": "2026-06-27T00:30:00+00:00",
                        "end": "2026-06-27T01:00:00+00:00",
                    },
                ],
            },
            "ev": {
                "timeline": [
                    {
                        "state": "charging",
                        "charge_kw": 7,
                        "target_soc_percent": 80,
                        "start": "2026-06-27T00:00:00+00:00",
                        "end": "2026-06-27T00:30:00+00:00",
                    },
                    {"state": "idle", "start": "2026-06-27T00:30:00+00:00", "end": "2026-06-27T01:00:00+00:00"},
                ],
            },
        }
    )
    coordinator = _coordinator(plan)

    assert (
        next(item for item in SENSORS if item.key == "climate_current_state").value_fn(coordinator) == "Heat (21.5 C)"
    )
    assert next(item for item in SENSORS if item.key == "climate_next_state").value_fn(coordinator) == "Off"
    assert next(item for item in SENSORS if item.key == "enphase_current_state").value_fn(coordinator) == "Idle"
    assert (
        next(item for item in SENSORS if item.key == "enphase_next_state").value_fn(coordinator)
        == "Charge Battery (2.5 kW)"
    )
    assert next(item for item in SENSORS if item.key == "ev_current_state").value_fn(coordinator) == "Charging to 80%"
    assert next(item for item in SENSORS if item.key == "ev_next_state").value_fn(coordinator) == "Idle"


def test_asset_state_sensors_fall_back_to_timeline_labels() -> None:
    plan = _plan(
        device_plans={
            "climate": {"timeline": []},
            "enphase": {
                "timeline": [
                    "invalid",
                    {
                        "state": "consume_battery",
                        "battery_discharge_kw": 1.25,
                        "start": "2026-06-27T00:30:00+00:00",
                        "end": "2026-06-27T01:00:00+00:00",
                    },
                ],
            },
            "ev": {
                "timeline": [
                    {"state": "charging", "charge_kw": 7},
                    {"state": "charging", "charge_kw": 7},
                ],
            },
        }
    )
    coordinator = _coordinator(plan)

    assert next(item for item in SENSORS if item.key == "climate_current_state").value_fn(coordinator) == "Unknown"
    assert next(item for item in SENSORS if item.key == "enphase_current_state").value_fn(coordinator) == "Unknown"
    assert (
        next(item for item in SENSORS if item.key == "enphase_next_state").value_fn(coordinator)
        == "Consume Battery (1.25 kW)"
    )
    assert next(item for item in SENSORS if item.key == "ev_next_state").value_fn(coordinator) == "Idle"


def test_asset_state_attributes_prefer_explicit_current_and_next_state() -> None:
    plan = _plan(
        device_plans={
            "climate": {
                "current_state": {"state": "cool", "hvac_mode": "cool"},
                "next_planned_state": {"state": "preconditioning", "hvac_mode": "cool", "target_temperature": 23},
                "timeline": [{"state": "idle"}],
            }
        }
    )
    coordinator = _coordinator(plan)
    current = next(item for item in SENSORS if item.key == "climate_current_state")
    next_state = next(item for item in SENSORS if item.key == "climate_next_state")

    assert current.attrs_fn(coordinator)["details"] == {"State": "Cool", "Climate mode": "Cool"}
    assert next_state.attrs_fn(coordinator)["details"]["Target temperature C"] == 23


def test_ev_charge_state_sensors_expose_live_and_planned_charge_state() -> None:
    plan = _plan(
        device_plans={
            "ev": {
                "timeline": [
                    {"state": "idle", "start": "2026-06-27T00:00:00+00:00", "end": "2026-06-27T00:30:00+00:00"},
                    {
                        "state": "charging",
                        "charge_kw": 7,
                        "target_soc_percent": 80,
                        "start": "2026-06-27T00:30:00+00:00",
                        "end": "2026-06-27T01:00:00+00:00",
                    },
                ],
            }
        }
    )
    coordinator = _coordinator(
        plan,
        entry_data={"ev_charging_entity": "binary_sensor.ev_charging"},
        hass=_hass_with_states({"binary_sensor.ev_charging": "off"}),
    )
    current_charge = next(item for item in SENSORS if item.key == "ev_current_charge_state")
    next_charge = next(item for item in SENSORS if item.key == "ev_next_charge_state")

    assert current_charge.value_fn(coordinator) == "Not Charging"
    assert current_charge.attrs_fn(coordinator)["live_state"] == "off"
    assert next_charge.value_fn(coordinator) == "Charging to 80%"


def test_ev_charge_state_sensors_handle_live_and_plan_fallbacks() -> None:
    plan = _plan(
        device_plans={
            "ev": {
                "timeline": [
                    {"state": "charging", "charge_kw": 7},
                    {"state": "charging"},
                ],
            }
        }
    )
    current = next(item for item in SENSORS if item.key == "ev_current_charge_state")
    next_charge = next(item for item in SENSORS if item.key == "ev_next_charge_state")

    assert current.value_fn(_coordinator(None, entry_data={"ev_charging_entity": "binary_sensor.missing"})) == "Unknown"
    assert (
        current.value_fn(_coordinator(plan, entry_data={"ev_charging_entity": "binary_sensor.ev"}, hass=None))
        == "Charging (7 kW)"
    )
    assert (
        current.value_fn(
            _coordinator(
                plan,
                entry_data={"ev_charging_entity": "binary_sensor.ev"},
                hass=_hass_with_states({"binary_sensor.ev": "connected_not_charging"}),
            )
        )
        == "Connected Not Charging"
    )
    assert (
        current.value_fn(
            _coordinator(
                plan,
                entry_data={"ev_charging_entity": "binary_sensor.ev"},
                hass=_hass_with_states({"binary_sensor.ev": "vehicle_sleeping"}),
            )
        )
        == "Vehicle Sleeping"
    )
    assert next_charge.value_fn(_coordinator(plan)) == "Charging"


def test_ev_plan_attributes_include_trip_history_summary() -> None:
    coordinator = _coordinator(
        _plan(),
        store_data={"trip_history": {"summary": {"observed_days": 3, "daily_soc": [10, 20]}}},
    )
    ev = next(item for item in SENSORS if item.key == "ev_charging_plan")

    assert ev.attrs_fn(coordinator)["trip_history_summary"] == {"observed_days": 3, "daily_soc": [10, 20]}


def test_presence_sensor_exposes_inferred_occupancy_context() -> None:
    coordinator = _coordinator(
        _plan(preview=[{"start": "2026-06-27T00:00:00+00:00", "occupied": "away"}]),
        entry_data={"person_entities": ["person.james", "person.cath"]},
    )
    presence = next(item for item in SENSORS if item.key == "presence_state")

    attrs = presence.attrs_fn(coordinator)

    assert presence.value_fn(coordinator) == "Away"
    assert attrs["occupancy_state"] == "away"
    assert attrs["person_entities"] == ["person.james", "person.cath"]
    assert attrs["preview"] == [{"start": "2026-06-27T00:00:00+00:00", "occupied": "away"}]


def test_presence_sensor_handles_list_and_unknown_preview() -> None:
    coordinator = _coordinator(
        _plan(preview=[{"occupied": ""}, "invalid"]),
        entry_data={"person_entities": ["person.james"]},
    )
    presence = next(item for item in SENSORS if item.key == "presence_state")

    assert presence.value_fn(coordinator) == "Unknown"
    assert presence.attrs_fn(coordinator)["person_entities"] == ["person.james"]


def test_ai_advice_sensor_exposes_latest_accepted_response() -> None:
    coordinator = _coordinator(
        _plan(),
        options={"ai_enabled": True},
        store_data={
            "ai_recommendations": [
                {
                    "created_at": "2026-06-27T00:00:00+00:00",
                    "plan_id": "plan-1",
                    "status": "accepted",
                    "service_called": "ai_task.generate_data",
                    "ai_task_entity": "ai_task.extended_openai_ai_task",
                    "rejected_reason": None,
                    "accepted": {
                        "alerts": ["PV forecast confidence is low"],
                        "reasoning_summary": "Use extra forecast buffer.",
                        "confidence": 0.74,
                        "suggested_forecast_buffer_percent": 12,
                    },
                }
            ]
        },
    )
    description = next(item for item in SENSORS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Accepted"
    assert attrs["enabled"] is True
    assert attrs["ai_task_entity"] == "ai_task.extended_openai_ai_task"
    assert attrs["alerts"] == ["PV forecast confidence is low"]
    assert attrs["reasoning_summary"] == "Use extra forecast buffer."
    assert attrs["suggested_forecast_buffer_percent"] == 12


def test_ai_advice_sensor_handles_enabled_without_response_and_non_dict_payloads() -> None:
    no_response = _coordinator(_plan(), options={"ai_enabled": True})
    description = next(item for item in SENSORS if item.key == "ai_advice")

    assert description.value_fn(no_response) == "No response"

    coordinator = _coordinator(
        _plan(),
        options={"ai_enabled": True},
        store_data={
            "ai_recommendations": [
                {
                    "status": None,
                    "accepted": "invalid",
                    "rejected_detail": "invalid",
                    "rejected_reason": None,
                }
            ]
        },
    )
    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Unknown"
    assert attrs["alerts"] == []
    assert attrs["rejected_detail"] == {}


def test_ai_advice_sensor_exposes_rejection_detail() -> None:
    coordinator = _coordinator(
        _plan(),
        options={"ai_enabled": True},
        store_data={
            "ai_recommendations": [
                {
                    "created_at": "2026-06-27T00:00:00+00:00",
                    "plan_id": "plan-1",
                    "status": "rejected",
                    "service_called": "ai_task.generate_data",
                    "ai_task_entity": "ai_task.extended_openai_ai_task",
                    "rejected_reason": "ai_response_forbidden_fields",
                    "rejected_detail": {
                        "reason": "ai_response_forbidden_fields",
                        "message": "The AI response included forbidden fields.",
                        "fields": ["hard_constraint_changes"],
                    },
                    "accepted": {},
                }
            ]
        },
    )
    description = next(item for item in SENSORS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Rejected"
    assert attrs["service_called"] == "ai_task.generate_data"
    assert attrs["ai_task_entity"] == "ai_task.extended_openai_ai_task"
    assert attrs["rejected_reason"] == "ai_response_forbidden_fields"
    assert attrs["rejected_detail"]["message"] == "The AI response included forbidden fields."
    assert attrs["rejected_detail"]["fields"] == ["hard_constraint_changes"]


def test_ai_advice_sensor_builds_rejection_detail_for_legacy_history() -> None:
    coordinator = _coordinator(
        _plan(),
        options={"ai_enabled": True},
        store_data={
            "ai_recommendations": [
                {
                    "status": "rejected",
                    "service_called": "ai_task.generate_data",
                    "rejected_reason": "ai_response_not_json",
                    "accepted": {},
                }
            ]
        },
    )
    description = next(item for item in SENSORS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert attrs["rejected_detail"] == {
        "reason": "ai_response_not_json",
        "message": "The AI service did not return a JSON object.",
    }


def test_sensor_helper_edge_cases_for_labels_and_timeline() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    profile_action = PlanAction(
        action_id="enphase-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.ENPHASE,
        kind=ActionKind.SET_PROFILE,
        desired_state={"profile": "Self-Consumption"},
        hard_constraints=[],
        reason_codes=["enphase_price_spread_above_threshold"],
        expected_cost_delta=0.3,
        confidence=0.7,
        requires_haeo_plan_id="plan-1",
    )
    restore_action = PlanAction(
        action_id="enphase-restore",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.ENPHASE,
        kind=ActionKind.RESTORE_AI,
        desired_state={"profile": "AI Optimisation"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    climate_action = PlanAction(
        action_id="climate-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"hvac_mode": "off"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    start_action = PlanAction(
        action_id="ev-start",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )

    assert sensor_module._asset_plan_state(_plan(), ActionAsset.EV) == "Idle"
    assert sensor_module._asset_timeline_state({"timeline": []}, "current") == {"state": "unknown"}
    assert sensor_module._asset_timeline_state({"timeline": ["bad", {"state": "charging"}]}, "current") == {
        "state": "unknown"
    }
    assert sensor_module._asset_timeline_state(
        {"timeline": [{"state": "idle"}, "bad", {"state": "idle"}, {"state": "charging", "target_soc_percent": 80}]},
        "next",
    ) == {"state": "charging", "target_soc_percent": 80}
    assert sensor_module._timeline_state_label({"state": "charging", "charge_kw": 7}) == "Charging (7 kW)"
    assert sensor_module._timeline_state_label({"state": "charging", "battery_charge_kw": 3}) == "Charging (3 kW)"
    assert (
        sensor_module._timeline_state_label({"state": "discharging", "battery_discharge_kw": 2}) == "Discharging (2 kW)"
    )
    assert (
        sensor_module._timeline_state_label({"state": "preconditioning", "hvac_mode": "heat"})
        == "Preconditioning: Heat"
    )
    assert sensor_module._timeline_summary(["bad"] + [{"state": "idle"} for _ in range(13)])[-1] == (
        "2 more segment(s) omitted."
    )
    assert sensor_module._plain_action(profile_action)["decision"] == "Switch Enphase profile to Self-Consumption."
    assert sensor_module._plain_action(profile_action)["requires_haeo_plan"] is True
    assert sensor_module._action_sentence(restore_action) == "Restore Enphase to AI Optimisation."
    assert sensor_module._action_sentence(climate_action) == "Set climate to Off."
    assert sensor_module._action_sentence(start_action) == "Start EV charging"
    assert sensor_module._plain_state_details(
        {
            "state": "set_hvac",
            "reason_codes": ["hvac_thermal_shift_before_expensive_period"],
            "execute_not_before": now.isoformat(),
            "bad_time": "not-a-time",
            "ignored": None,
        }
    ) == {
        "State": "Set HVAC",
        "Reasons": [
            "Heating or cooling now because electricity is cheap and the home can coast through a later "
            "expensive period."
        ],
        "Start": "00:00",
        "Bad Time": "not-a-time",
    }
    assert sensor_module._reason_summary("away_hvac_policy") == "Nobody is home, so climate control can be reduced."
    assert sensor_module._reason_summary(123) == ""
    assert sensor_module._time_label(None) is None
    assert sensor_module._time_label("not-a-time") == "not-a-time"
    assert sensor_module._charge_state_label_from_raw("unknown") is None
    assert (
        sensor_module._charge_timeline_state_label({"state": "charging", "target_soc_percent": 80}) == "Charging to 80%"
    )
    assert sensor_module._charge_timeline_state_label({"state": "idle"}) == "Not Charging"
    assert sensor_module._display_state("") == "Unknown"
    assert sensor_module._display_state("ev_soc_ai_hvac") == "EV SOC AI HVAC"
    assert sensor_module._bounded_json({"a": {"b": {"c": {"d": {"e": 1}}}}}) == {
        "a": {"b": {"c": {"d": "<truncated>"}}}
    }
    assert sensor_module._bounded_json(list(range(13)))[-1] == {"truncated_count": 1}


def test_sensor_configured_state_and_presence_helpers_cover_fallbacks() -> None:
    assert sensor_module._configured_state_value(_coordinator(_plan()), "missing") is None
    assert (
        sensor_module._configured_state_value(
            _coordinator(_plan(), entry_data={"ev_charging_entity": "sensor.ev"}, hass=_hass_with_states({})),
            "ev_charging_entity",
        )
        is None
    )
    assert (
        sensor_module._configured_state_value(
            _coordinator(
                _plan(),
                entry_data={"ev_charging_entity": "sensor.ev"},
                hass=_hass_with_states({"sensor.ev": "charging"}),
            ),
            "ev_charging_entity",
        )
        == "charging"
    )

    assert sensor_module._presence_attrs(
        _coordinator(None, entry_data={"person_entities": ["person.a", "person.b"]})
    ) == {"person_entities": ["person.a", "person.b"]}
    assert (
        sensor_module._presence_attrs(_coordinator(_plan(), entry_data={"person_entities": 123}))["person_entities"]
        == []
    )


def test_sensor_asset_attrs_handle_missing_actions_and_non_dict_device_plan() -> None:
    plan = _plan(device_plans={"ev": "bad"}, input_issues=["ev_soc_entity_unavailable"])
    attrs = sensor_module._asset_plan_attrs(plan, ActionAsset.EV)

    assert attrs["planned_action_count"] == 0
    assert attrs["timeline_segment_count"] == 0
    assert attrs["issues"] == ["EV SOC Entity Unavailable"]
    assert sensor_module._first_asset_action(plan, ActionAsset.EV) is None


def _coordinator(
    plan: EnergyPlan | None,
    *,
    store_data: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
    entry_data: dict[str, object] | None = None,
    hass: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        data=plan,
        store=SimpleNamespace(data=store_data or {}),
        options=options or {},
        entry_data=entry_data or {},
        entry=SimpleNamespace(entry_id="test_entry"),
        hass=hass,
    )


def _hass_with_states(values: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: None if entity_id not in values else SimpleNamespace(state=values[entity_id])
        )
    )


def _plan(
    *,
    actions: list[PlanAction] | None = None,
    preview: list[dict[str, object]] | None = None,
    input_issues: list[str] | None = None,
    device_plans: dict[str, object] | None = None,
) -> EnergyPlan:
    return EnergyPlan(
        plan_id="plan-1",
        created_at=datetime(2026, 6, 27, tzinfo=UTC),
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test summary",
        confidence=0.875,
        estimated_daily_cost=3.25,
        actions=actions or [],
        preview=preview or [],
        input_issues=input_issues or [],
        device_plans=device_plans or {},
    )
