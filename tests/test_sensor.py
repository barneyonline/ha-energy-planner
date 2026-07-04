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
        "production_readiness": "Not Ready",
        "control_block_reason": "Production Gate Not Armed",
        "execution_audit": "No Activity",
        "dry_run_comparison": "No Dry Run",
        "support_bundle_summary": "No Plan",
        "ai_advice": "Disabled",
        "climate_plan": "Unknown",
        "climate_current_state": "Unknown",
        "climate_next_state": "Unknown",
        "presence_state": "Unknown",
        "enphase_plan": "Unknown",
        "enphase_current_state": "Unknown",
        "enphase_next_state": "Unknown",
        "ev_charging_plan": "Unknown",
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


def test_operational_summary_sensors_handle_edge_shapes() -> None:
    ready = _coordinator(
        _plan(),
        options={
            "ev_control_enabled": True,
            "climate_control_enabled": True,
            "enphase_control_enabled": True,
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

    assert production.value_fn(ready) == "Ready To Arm"
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
    assert sensor.extra_state_attributes["plan_id"] == "plan-1"


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


def test_next_action_sensor_exposes_compact_json_action() -> None:
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

    assert description.value_fn(coordinator) == "EV Schedule"
    assert attrs == {
        "action": {
            "action_id": "ev-1",
            "plan_id": "plan-1",
            "execute_not_before": "2026-06-27T00:00:00+00:00",
            "execute_not_after": "2026-06-27T00:05:00+00:00",
            "asset": "ev",
            "kind": "ev_schedule",
            "desired_state": {"target_soc_percent": 80},
            "hard_constraints": ["ev_bounds"],
            "reason_codes": ["least_cost_slots_before_ready_by"],
            "expected_cost_delta": -0.25,
            "confidence": 0.8,
            "requires_haeo_plan_id": None,
        }
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

    assert climate.value_fn(coordinator) == "Set HVAC"
    assert climate.attrs_fn(coordinator)["planned_actions"][0]["desired_state"]["target_temperature"] == 22
    assert ev.value_fn(coordinator) == "EV Schedule"
    ev_attrs = ev.attrs_fn(coordinator)
    assert ev_attrs["trip_history_record_count"] == 2
    assert ev_attrs["planned_actions"][0]["desired_state"]["allocated_slots"][-1] == {"truncated_count": 8}


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
        "amber_import_price_entity_unavailable",
        "ev_soc_entity_unavailable",
        "ev_connected_entity_unavailable",
    ]
    assert climate.attrs_fn(coordinator)["issues"] == [
        "amber_import_price_entity_unavailable",
        "daikin_climate_entity_unavailable",
        "daikin_power_entity_unavailable",
    ]
    assert enphase.attrs_fn(coordinator)["issues"] == [
        "amber_import_price_entity_unavailable",
        "amber_export_price_entity_unavailable",
        "pv_forecast_entity_unavailable",
        "baseline_load_forecast_entity_unavailable",
        "battery_soc_entity_unavailable",
        "enphase_profile_entity_unavailable",
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
    assert attrs["current_state_label"] == "Heat (21.5 C)"
    assert attrs["current_state"]["hvac_mode"] == "heat"
    assert attrs["next_planned_state_label"] == "Preconditioning: Heat to 22 C"
    assert attrs["next_planned_state"]["target_temperature"] == 22
    assert attrs["timeline_segment_count"] == 1
    assert attrs["timeline"] == [
        {
            "start": "2026-06-27T00:00:00+00:00",
            "end": "2026-06-27T00:30:00+00:00",
            "state": "preconditioning",
            "target_temperature": 22,
        }
    ]


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

    assert current.attrs_fn(coordinator)["state"] == {"state": "cool", "hvac_mode": "cool"}
    assert next_state.attrs_fn(coordinator)["state"]["target_temperature"] == 23


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
                    "service_called": "conversation.process",
                    "ai_agent_id": "conversation.extended_openai_conversation",
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
    assert attrs["ai_agent_id"] == "conversation.extended_openai_conversation"
    assert attrs["alerts"] == ["PV forecast confidence is low"]
    assert attrs["reasoning_summary"] == "Use extra forecast buffer."
    assert attrs["accepted"]["suggested_forecast_buffer_percent"] == 12


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
    assert attrs["accepted"] == {}


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
                    "service_called": "conversation.process",
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
    assert attrs["issues"] == ["ev_soc_entity_unavailable"]
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
