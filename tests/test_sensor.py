"""Tests for Energy Planner sensor entities."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import CoreState

from custom_components.ha_energy_planner import plan_presentation as presentation_module
from custom_components.ha_energy_planner import sensor as sensor_module
from custom_components.ha_energy_planner.coordinator import _material_plan_fingerprint
from custom_components.ha_energy_planner.entity import RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
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
        "mode": "review",
        "current_state": "No controls configured",
        "next_actions": "Unknown",
        "load_forecast_coverage_score": None,
        "decision_summary": "Unknown",
        "plan_health": None,
        "current_load_forecast": None,
        "planning_duration": None,
    }
    assert attrs["current_state"] == {
        "mode": "Unknown",
        "health": "Unknown",
        "controlled_assets": [],
    }
    assert attrs["next_actions"] == {"actions": []}
    assert attrs["load_forecast_coverage_score"] == {
        "required_threshold_percent": 90.0,
        "meets_threshold": None,
        "score_source": "unavailable",
        "score_evaluated_at": None,
        "active_model_score_percent": None,
        "bypass_enabled": False,
        "bypass_applied_to_model": False,
        "model_status": "unknown",
        "quality_failures": [],
    }
    assert attrs["decision_summary"] == {}
    assert attrs["plan_health"] == {}
    assert attrs["current_load_forecast"] == {}
    assert attrs["planning_duration"] == {}

    coordinator.data = _plan()
    assert next(item for item in SENSORS if item.key == "next_actions").value_fn(coordinator) == (
        "No controls configured"
    )

    enabled_without_actuator = _coordinator(_plan(), options={"climate_control_enabled": True})
    assert (
        next(item for item in SENSORS if item.key == "current_state").value_fn(enabled_without_actuator)
        == "No controls configured"
    )


def test_mode_sensor_exposes_operational_control_mode() -> None:
    description = next(item for item in SENSORS if item.key == "mode")
    review = _coordinator(
        _plan(),
        options={"planner_enabled": True, "dry_run": True},
        store_data={"production": {"armed": True}},
    )
    active = _coordinator(
        _plan(),
        options={"planner_enabled": True, "dry_run": False},
        store_data={"production": {"armed": True}},
    )

    assert description.device_class == SensorDeviceClass.ENUM
    assert description.options == ["review", "recovery", "active"]
    assert description.value_fn(review) == "review"
    assert description.value_fn(active) == "active"

    active.effective_control = False
    assert active.active_control is True
    assert description.value_fn(active) == "review"


def test_mode_sensor_reports_post_startup_auto_recovery_and_retains_intent() -> None:
    description = next(item for item in SENSORS if item.key == "mode")
    coordinator = _coordinator(
        _plan(),
        options={"planner_enabled": True, "dry_run": False},
        store_data={
            "production": {
                "armed": False,
                "startup_auto_recovery": {
                    "status": "waiting_for_safe",
                    "successful_runs": 0,
                },
            }
        },
        hass=SimpleNamespace(state=CoreState.running),
    )

    assert coordinator.automatic_control_requested is True
    assert coordinator.active_control is False
    for status in sensor_module.STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES:
        coordinator.store.data["production"]["startup_auto_recovery"]["status"] = status
        assert description.value_fn(coordinator) == "recovery"

    coordinator.hass.state = CoreState.starting
    assert description.value_fn(coordinator) == "review"

    coordinator.hass.state = CoreState.running
    coordinator.store.data["production"]["startup_auto_recovery"]["status"] = "recovered"
    assert description.value_fn(coordinator) == "review"


def test_load_forecast_coverage_sensor_exposes_score_threshold_and_bypass() -> None:
    assert sensor_module._load_forecast_coverage_details("invalid") == (
        None,
        "unavailable",
        None,
    )
    coordinator = _coordinator(
        None,
        options={"bypass_safety_gates": True},
        store_data={
            "built_in_load_forecast": {
                "status": "ready",
                "safety_gates_bypassed": True,
                "quality_failures": [],
                "validation": {"upper_coverage": 0.864198},
            }
        },
    )
    description = next(item for item in SENSORS if item.key == "load_forecast_coverage_score")

    assert description.value_fn(coordinator) == 86.4
    assert description.attrs_fn(coordinator) == {
        "required_threshold_percent": 90.0,
        "meets_threshold": False,
        "score_source": "active_model",
        "score_evaluated_at": None,
        "active_model_score_percent": 86.4,
        "bypass_enabled": True,
        "bypass_applied_to_model": True,
        "model_status": "ready",
        "quality_failures": [],
    }
    coordinator.store.data["built_in_load_forecast"]["validation"]["upper_coverage"] = 1.1
    assert description.value_fn(coordinator) is None


def test_load_forecast_coverage_sensor_uses_latest_retained_training_score() -> None:
    coordinator = _coordinator(
        None,
        store_data={
            "built_in_load_forecast": {
                "status": "ready",
                "trained_at": "2026-08-12T00:00:00+00:00",
                "last_attempt_at": "2026-08-12T06:00:00+00:00",
                "last_training_status": "failed",
                "validation": {"upper_coverage": 0.94},
                "last_training_validation": {"upper_coverage": 0.8736},
                "quality_failures": [],
            }
        },
    )
    description = next(item for item in SENSORS if item.key == "load_forecast_coverage_score")

    assert description.value_fn(coordinator) == 87.4
    assert description.attrs_fn(coordinator) == {
        "required_threshold_percent": 90.0,
        "meets_threshold": False,
        "score_source": "latest_training_attempt",
        "score_evaluated_at": "2026-08-12T06:00:00+00:00",
        "active_model_score_percent": 94.0,
        "bypass_enabled": False,
        "bypass_applied_to_model": False,
        "model_status": "ready",
        "quality_failures": [],
    }


def test_diagnostic_decision_and_health_sensors_explain_current_plan() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = PlanAction(
        action_id="ev-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={"charge_kw": 7.0},
        hard_constraints=["grid_import_limit"],
        reason_codes=["least_cost_slots_before_ready_by"],
        expected_cost_delta=-0.4,
        confidence=0.9,
    )
    plan = _plan(actions=[action], input_issues=["weather_entity_unavailable"])
    plan.health = InputHealth.DEGRADED
    plan.decision_audit = {
        "summary": "EV charging uses the lowest-cost feasible slot.",
        "policy_order": ["safety", "cost"],
        "marginal_budget": {"grid_headroom_kw": 4.5},
        "accepted": [{"action_id": "ev-1", "device": "EV", "reason": "lowest cost"}],
    }
    plan.rejected_actions = [{"device": "Climate", "reason": "nobody is home"}]
    coordinator = _coordinator(plan)
    decision = next(item for item in SENSORS if item.key == "decision_summary")
    health = next(item for item in SENSORS if item.key == "plan_health")

    assert decision.entity_category == EntityCategory.DIAGNOSTIC
    assert decision.value_fn(coordinator) == "1 planned, 1 rejected"
    assert decision.attrs_fn(coordinator)["summary"] == ("EV charging uses the lowest-cost feasible slot.")
    assert (
        decision.attrs_fn(coordinator)["planned_actions"][0]["determination"]["accepted_decision"]["reason"]
        == "lowest cost"
    )
    assert decision.attrs_fn(coordinator)["rejected_actions"] == [{"device": "Climate", "reason": "nobody is home"}]

    assert health.device_class == SensorDeviceClass.ENUM
    assert health.options == ["healthy", "degraded", "unsafe"]
    assert health.value_fn(coordinator) == "degraded"
    assert health.attrs_fn(coordinator)["confidence_percent"] == 87.5
    assert health.attrs_fn(coordinator)["issues"] == [
        {
            "code": "weather_entity_unavailable",
            "description": "Weather Entity Unavailable",
        }
    ]
    plan.health = "corrupt"
    assert health.value_fn(coordinator) is None


def test_current_load_forecast_sensor_exposes_expected_and_conservative_power() -> None:
    plan = _plan()
    coordinator = _coordinator(
        plan,
        store_data={
            "forecast_snapshots": [
                {
                    "plan_id": plan.plan_id,
                    "created_at": "2026-06-27T00:00:00+00:00",
                    "built_in_load_forecast": {
                        "status": "ready",
                        "model_age_hours": 2.5,
                        "trained_at": "2026-06-26T21:30:00+00:00",
                        "source_entity_id": "sensor.whole_home_power",
                        "first_expected_kw": 1.2345,
                        "first_upper_kw": 1.8,
                        "forecast_coverage": 0.995,
                        "recent_correction_factor": 1.1,
                        "live_source_status": "available",
                        "current_correction_applied": True,
                        "fallback_applied": False,
                        "update_reason": "load_forecast_ready",
                        "quality_failures": [],
                    },
                }
            ]
        },
    )
    description = next(item for item in SENSORS if item.key == "current_load_forecast")

    assert description.icon is None
    assert description.device_class == SensorDeviceClass.POWER
    assert description.native_unit_of_measurement == UnitOfPower.KILO_WATT
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.value_fn(coordinator) == 1.2345
    assert description.attrs_fn(coordinator) == {
        "plan_id": "plan-1",
        "valid_at": "2026-06-27T00:00:00+00:00",
        "forecast_interval_minutes": 5,
        "conservative_forecast_kw": 1.8,
        "forecast_horizon_coverage_percent": 99.5,
        "model_status": "ready",
        "model_age_hours": 2.5,
        "trained_at": "2026-06-26T21:30:00+00:00",
        "source_entity_id": "sensor.whole_home_power",
        "live_source_status": "available",
        "recent_correction_factor": 1.1,
        "current_correction_applied": True,
        "fallback_applied": False,
        "update_reason": "load_forecast_ready",
        "quality_failures": [],
    }

    coordinator.store.data["forecast_snapshots"][0]["built_in_load_forecast"]["first_expected_kw"] = float("nan")
    assert description.value_fn(coordinator) is None


def test_planning_duration_sensor_exposes_bounded_refresh_performance() -> None:
    coordinator = _coordinator(_plan())
    coordinator.last_refresh_metadata = {
        "duration_ms": 27.25,
        "succeeded": True,
        "completed_at": datetime(2026, 6, 27, 0, 5, tzinfo=UTC),
        "trigger": "state_change",
        "phases": {"planner_ms": 4.5},
    }
    coordinator.refresh_metrics = {
        "last_duration_ms": 27.25,
        "last_trigger": "state_change",
        "refreshes_last_hour": 8,
        "requested": 12,
        "completed": 10,
        "succeeded": 9,
        "failed": 1,
        "coalesced": 2,
        "fingerprint_skipped": 5,
        "computed": 4,
        "trigger_counts": {"state_change": 7, "boundary": 3},
        "phase_durations_ms": {"inputs_ms": 20.0, "planner_ms": 4.5},
    }
    description = next(item for item in SENSORS if item.key == "planning_duration")

    assert description.device_class == SensorDeviceClass.DURATION
    assert description.native_unit_of_measurement == UnitOfTime.MILLISECONDS
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.value_fn(coordinator) == 27.25
    assert description.attrs_fn(coordinator) == {
        "last_refresh_succeeded": True,
        "last_completed_at": "2026-06-27T00:05:00+00:00",
        "last_trigger": "state_change",
        "refreshes_last_hour": 8,
        "counters": {
            "requested": 12,
            "completed": 10,
            "succeeded": 9,
            "failed": 1,
            "coalesced": 2,
            "fingerprint_skipped": 5,
            "computed": 4,
        },
        "trigger_counts": {"state_change": 7, "boundary": 3},
        "phase_durations_ms": {"inputs_ms": 20.0, "planner_ms": 4.5},
    }

    del coordinator.refresh_metrics
    assert description.value_fn(coordinator) == 27.25


def test_retired_sensor_helpers_remain_safe_for_diagnostics_without_a_plan() -> None:
    """Keep non-entity diagnostic formatters safe while their registry IDs retire."""

    assert sensor_module._asset_current_state(None, ActionAsset.EV) == "Unknown"
    assert sensor_module._asset_next_state(None, ActionAsset.EV) == "Unknown"


def test_consolidated_ownership_covers_enphase_and_disabled_control_reason() -> None:
    assert (
        sensor_module._asset_owned(
            {"ownership": {"enphase_profile_changed_at": "2026-08-08T00:00:00+00:00"}},
            ActionAsset.ENPHASE,
        )
        is True
    )


def test_consolidated_status_entities_show_live_state_and_action_determination() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    climate_action = PlanAction(
        action_id="climate-1",
        plan_id="plan-1",
        execute_not_before=now + timedelta(hours=1),
        execute_not_after=now + timedelta(hours=1, minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"hvac_mode": "heat", "target_temperature": 21},
        hard_constraints=["occupied_comfort"],
        reason_codes=["precondition_before_peak"],
        expected_cost_delta=0.42,
        confidence=0.9,
    )
    ev_action = replace(
        climate_action,
        action_id="ev-1",
        execute_not_before=now + timedelta(hours=2),
        execute_not_after=now + timedelta(hours=2, minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={"charge_kw": 7},
        reason_codes=["ev_soc_below_target"],
    )
    plan = _plan(
        actions=[ev_action, climate_action],
        device_plans={
            "climate": {
                "current_state_label": "Heat (19 C)",
                "next_planned_state_label": "Preconditioning: Heat to 21 C",
            },
            "ev": {"current_state_label": "Connected, not charging"},
        },
    )
    plan.decision_audit = {
        "summary": "Climate preconditioning won the lowest-cost feasible slot.",
        "policy_order": ["cost", "comfort", "ev_readiness"],
        "marginal_budget": {"forecast_surplus_kwh": 2.0},
        "accepted": [
            {
                "action_id": "climate-1",
                "device": "Climate",
                "score": 0.9,
                "reason": "lower pre-peak price",
            }
        ],
    }
    states = {
        "climate.home": SimpleNamespace(
            state="heat",
            attributes={
                "friendly_name": "Home climate",
                "current_temperature": 19,
                "temperature": 19,
                "hvac_action": "heating",
            },
        ),
        "switch.ev": SimpleNamespace(state="off", attributes={"friendly_name": "EV charger"}),
        "binary_sensor.ev_charging": SimpleNamespace(state="off", attributes={}),
        "sensor.ev_soc": SimpleNamespace(state="53", attributes={"unit_of_measurement": "%"}),
    }
    coordinator = _coordinator(
        plan,
        options={
            "climate_control_enabled": True,
            "ev_control_enabled": True,
        },
        entry_data={
            "daikin_climate_entity": "climate.home",
            "ev_charger_entity": "switch.ev",
            "ev_charging_entity": "binary_sensor.ev_charging",
            "ev_soc_entity": "sensor.ev_soc",
            "weather_entity": "weather.home",
        },
        hass=SimpleNamespace(states=SimpleNamespace(get=states.get)),
        store_data={
            "ownership": {"hvac_control": {"phase": "precondition"}},
            "forecast_snapshots": [
                {
                    "plan_id": "plan-1",
                    "built_in_load_forecast": {
                        "source": "built_in_recorder_history",
                        "source_entity_id": "sensor.whole_home_power",
                        "status": "ready",
                        "first_expected_kw": 1.0,
                        "first_upper_kw": 1.3,
                        "live_source_status": "model_fallback",
                        "live_source_outage_seconds": 120,
                        "outage_grace_minutes": 10,
                        "current_correction_applied": False,
                        "fallback_applied": True,
                    },
                    "action_load_forecasts": [
                        {
                            "action_id": "climate-1",
                            "valid_at": (now + timedelta(hours=1)).isoformat(),
                            "expected_kw": 2.0,
                            "conservative_kw": 2.4,
                        },
                        {
                            "action_id": "ev-1",
                            "valid_at": (now + timedelta(hours=2)).isoformat(),
                            "expected_kw": 1.5,
                            "conservative_kw": 1.9,
                        },
                    ],
                }
            ],
        },
    )
    coordinator.weather_forecast_diagnostics = {
        "fetch_status": "cached_after_error",
        "source_type": "weather_service_hourly_cache",
        "cache_age_seconds": 901,
        "failure_reason": "HomeAssistantError:unavailable",
    }
    current = next(item for item in SENSORS if item.key == "current_state")
    next_actions = next(item for item in SENSORS if item.key == "next_actions")

    assert current.value_fn(coordinator) == "Climate: Heat (19 C) | EV: Not Charging"
    current_attrs = current.attrs_fn(coordinator)
    assert current_attrs["controlled_assets"][0]["planner_owns_control"] is True
    assert current_attrs["controlled_assets"][0]["entities"][0]["details"]["current_temperature"] == 19
    assert current_attrs["controlled_assets"][1]["entities"][0]["state"] == "off"
    assert current_attrs["weather_forecast"]["fetch_status"] == "cached_after_error"
    assert current_attrs["load_forecast"]["live_source_status"] == "model_fallback"
    assert current_attrs["load_forecast"]["fallback_applied"] is True

    assert next_actions.value_fn(coordinator) == ("Climate: Preconditioning: Heat to 21 C | EV: Start EV charging")
    action_attrs = next_actions.attrs_fn(coordinator)
    assert [action["action_id"] for action in action_attrs["actions"]] == ["climate-1", "ev-1"]
    assert action_attrs["actions"][0]["determination"]["accepted_decision"]["score"] == 0.9
    assert action_attrs["actions"][0]["determination"]["load_forecast"]["expected_kw"] == 2.0
    assert action_attrs["actions"][1]["determination"]["load_forecast"]["expected_kw"] == 1.5
    assert action_attrs["actions"][0]["desired_state"]["Target temperature C"] == 21
    assert action_attrs["policy_order"] == ["cost", "comfort", "ev_readiness"]
    assert action_attrs["weather_forecast"]["cache_age_seconds"] == 901
    assert action_attrs["ai_explanation"] == {
        "configured": False,
        "available": False,
        "availability_reason": "ai_task_entity_not_configured",
        "result": None,
    }
    assert "plan_confidence" not in action_attrs
    assert "confidence" not in action_attrs["actions"][0]


def test_operational_summaries_hide_disabled_controls() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    actions = [
        PlanAction(
            action_id="climate-1",
            plan_id="plan-1",
            execute_not_before=now,
            execute_not_after=now + timedelta(minutes=5),
            asset=ActionAsset.DAIKIN,
            kind=ActionKind.SET_HVAC,
            desired_state={"hvac_mode": "heat"},
            hard_constraints=[],
            reason_codes=[],
            expected_cost_delta=0.0,
            confidence=1.0,
        ),
        PlanAction(
            action_id="enphase-1",
            plan_id="plan-1",
            execute_not_before=now + timedelta(minutes=5),
            execute_not_after=now + timedelta(minutes=10),
            asset=ActionAsset.ENPHASE,
            kind=ActionKind.SET_PROFILE,
            desired_state={"profile": "self_consumption"},
            hard_constraints=[],
            reason_codes=[],
            expected_cost_delta=0.0,
            confidence=1.0,
        ),
    ]
    coordinator = _coordinator(
        _plan(
            actions=actions,
            device_plans={
                "climate": {
                    "current_state_label": "Heat (19 C)",
                    "next_planned_state_label": "Heat to 21 C",
                },
                "enphase": {
                    "current_state_label": "Self-Consumption",
                    "next_planned_state_label": "AI Optimisation",
                },
            },
        ),
        options={
            "climate_control_enabled": True,
            "enphase_control_enabled": False,
        },
        entry_data={
            "daikin_climate_entity": "climate.home",
            "enphase_profile_entity": "select.enphase_profile",
        },
    )
    current = next(item for item in SENSORS if item.key == "current_state")
    next_actions = next(item for item in SENSORS if item.key == "next_actions")

    assert current.value_fn(coordinator) == "Climate: Heat (19 C)"
    assert [item["asset"] for item in current.attrs_fn(coordinator)["controlled_assets"]] == ["Climate"]
    assert next_actions.value_fn(coordinator) == "Climate: Heat to 21 C"
    next_attrs = next_actions.attrs_fn(coordinator)
    assert next_attrs["action_count"] == 1
    assert [action["action_id"] for action in next_attrs["actions"]] == ["climate-1"]


def test_data_quality_reports_input_issues_good_data_and_missing_coverage() -> None:
    issue_plan = _plan(input_issues=["weather_entity_unavailable"])
    issue_attrs = presentation_module.decision_data_quality_attrs(_coordinator(issue_plan))
    assert issue_attrs["status"] == "Input issue"
    assert issue_attrs["summary"] == "1 input issue is limiting this plan."

    issue_plan.input_issues.append("pv_forecast_entity_stale")
    assert presentation_module.decision_data_quality_attrs(_coordinator(issue_plan))["summary"] == (
        "2 input issues are limiting this plan."
    )

    good_plan = _plan()
    good_plan.confidence = 1.0
    good_attrs = presentation_module.decision_data_quality_attrs(_coordinator(good_plan))
    assert good_attrs["status"] == "Good"
    assert good_attrs["summary"] == "No material input-quality limitation affected this plan."
    assert presentation_module.coverage_summary({}, 24) is None


def test_confidence_helper_edge_cases_are_readable() -> None:
    assert presentation_module.confidence_health_score(InputHealth.DEGRADED) == 0.65
    assert presentation_module.forecast_source_confidence(_coordinator(None)) is None

    plan = _plan()
    plan.confidence = 0.8
    assert presentation_module.forecast_source_confidence(_coordinator(plan)) == 0.8
    assert (
        presentation_module.latest_forecast_snapshot(_coordinator(plan, store_data={"forecast_snapshots": "bad"})) == {}
    )
    assert (
        presentation_module.confidence_sources(
            _coordinator(
                plan,
                store_data={"forecast_snapshots": [{"plan_id": "plan-1", "confidence": {"sources": "bad"}}]},
            )
        )
        == []
    )
    assert (
        presentation_module.forecast_coverage_sources(
            _coordinator(plan, store_data={"forecast_snapshots": [{"plan_id": "plan-1", "forecast_coverage": "bad"}]})
        )
        == []
    )
    assert "stitched" in presentation_module.confidence_source_reason({"source": "forecast_series_stitched"})
    assert (
        presentation_module.confidence_source_reason({"source": "forecast_series_leading_fill"})
        == "Confidence source was not classified."
    )
    assert "shorter" in presentation_module.confidence_source_reason({"source": "forecast_series_partial"})
    assert "fails closed" in presentation_module.confidence_source_reason({"source": "point_value_only"})
    assert presentation_module.confidence_source_reason({"source": "invalid_state"}) == (
        "The entity state could not be converted into usable forecast data."
    )
    assert presentation_module.confidence_source_reason({"source": "other"}) == "Confidence source was not classified."

    assert presentation_module.confidence_improvement_actions(
        0.4,
        1.0,
        0.4,
        [{"input": "Load", "entity_id": "sensor.load", "source": "Invalid State", "confidence": 0.4}],
        {},
    ) == ["Fix Load (sensor.load) so it has a numeric usable state."]
    assert presentation_module.confidence_improvement_actions(
        0.5,
        1.0,
        0.5,
        [{"input": "PV", "entity_id": "sensor.pv", "source": "Forecast Series", "confidence": 0.5}],
        {},
    ) == ["Improve PV (sensor.pv) source confidence or data quality."]
    assert presentation_module.confidence_improvement_actions(
        0.65,
        0.65,
        1.0,
        [],
        {"pv": {"issues": ["pv_forecast_entity_unavailable", "pv_forecast_entity_stale"]}},
    ) == ["Resolve pv input issue(s): pv_forecast_entity_unavailable, pv_forecast_entity_stale."]
    assert presentation_module.confidence_improvement_actions(0.8, 1.0, None, [], {}) == [
        "Use forecast-capable entities with confidence metadata for price, PV, load, and weather inputs."
    ]
    assert presentation_module.confidence_improvement_actions(1.0, 1.0, 1.0, [], {}) == [
        "Confidence is already at 100%; no action is needed."
    ]


def test_sensor_platform_setup_groups_planner_sensors(monkeypatch: object) -> None:
    coordinator = _coordinator(_plan())
    entry = SimpleNamespace(entry_id="test_entry", runtime_data=coordinator)
    added: list[object] = []

    asyncio.run(sensor_module.async_setup_entry(SimpleNamespace(), entry, added.extend))

    assert len(added) == len(SENSORS)


def test_next_actions_entity_attributes_stay_below_recorder_budget() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    huge_text = "decision evidence ⚡ " * 2_000
    actions = [
        PlanAction(
            action_id=f"action-{index}-{huge_text}",
            plan_id="plan-1",
            execute_not_before=now + timedelta(minutes=index * 5),
            execute_not_after=now + timedelta(minutes=(index + 1) * 5),
            asset=ActionAsset.EV,
            kind=ActionKind.EV_START,
            desired_state={"detail": huge_text},
            hard_constraints=[huge_text] * 8,
            reason_codes=[huge_text] * 8,
            expected_cost_delta=0.1,
            confidence=0.9,
        )
        for index in range(12)
    ]
    plan = _plan(actions=actions)
    plan.decision_audit = {
        "summary": huge_text,
        "policy_order": [huge_text] * 12,
        "accepted": [
            {"action_id": action.action_id, **{f"evidence-{field}": huge_text for field in range(20)}}
            for action in actions
        ],
    }
    description = next(item for item in SENSORS if item.key == "next_actions")

    attrs = PlannerSensor(_coordinator(plan, options={"ev_control_enabled": True}), description).extra_state_attributes
    encoded_size = len(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    assert encoded_size <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
    assert attrs["attributes_truncated"] is True
    assert attrs["action_count"] == 12


def test_existing_status_surfaces_expose_builtin_load_evidence() -> None:
    plan = _plan()
    coordinator = _coordinator(
        plan,
        store_data={
            "forecast_snapshots": [
                {
                    "plan_id": plan.plan_id,
                    "built_in_load_forecast": {
                        "source": "built_in_recorder_history",
                        "source_entity_id": "sensor.whole_home_power",
                        "status": "ready",
                        "trained_at": "2026-06-27T00:00:00+00:00",
                        "last_attempt_at": "2026-06-27T00:00:00+00:00",
                        "last_attempt_source_entity_id": "sensor.whole_home_power",
                        "last_training_status": "failed",
                        "last_training_quality_failures": ["forecast_accuracy_below_persistence_gate"],
                        "last_training_validation": {"mae_kw": 2.0},
                        "unusable_since": "2026-06-20T00:00:00+00:00",
                        "first_expected_kw": 1.2,
                        "first_upper_kw": 1.5,
                    },
                }
            ]
        },
    )

    attrs = sensor_module._controlled_state_attrs(coordinator)

    assert attrs["load_forecast"]["status"] == "ready"
    assert attrs["load_forecast"]["first_expected_kw"] == 1.2
    assert attrs["load_forecast"]["last_attempt_at"] == "2026-06-27T00:00:00+00:00"
    assert attrs["load_forecast"]["source_entity_id"] == "sensor.whole_home_power"
    assert attrs["load_forecast"]["last_training_status"] == "failed"
    assert attrs["load_forecast"]["last_training_validation"] == {"mae_kw": 2.0}
    assert presentation_module.action_load_forecast_attrs(coordinator, "missing") == {}
    assert (
        presentation_module.action_load_forecast_attrs(
            _coordinator(plan, store_data={"forecast_snapshots": []}),
            "missing",
        )
        == {}
    )
    assert (
        presentation_module.action_load_forecast_attrs(
            _coordinator(
                plan,
                store_data={"forecast_snapshots": [{"plan_id": plan.plan_id, "action_load_forecasts": []}]},
            ),
            "missing",
        )
        == {}
    )
    assert (
        presentation_module.confidence_source_reason({"source": "built_in_recorder_history"})
        == "A local deterministic forecast learned from measured household load in Home Assistant Recorder."
    )


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
    )

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
    assert presentation_module.plain_action(profile_action)["decision"] == "Switch Enphase profile to Self-Consumption."
    assert "requires_haeo_plan" not in presentation_module.plain_action(profile_action)
    assert presentation_module.action_sentence(restore_action) == "Restore Enphase to AI Optimisation."
    assert presentation_module.action_sentence(climate_action) == "Set climate to Off."
    climate_action.kind = ActionKind.RELEASE_HVAC
    assert (
        presentation_module.action_sentence(climate_action) == "Release climate control to the configured automations."
    )
    assert presentation_module.action_sentence(start_action) == "Start EV charging"
    assert presentation_module.plain_state_details(
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
        "Start": "Sat 27 Jun, 00:00",
        "Bad Time": "not-a-time",
    }
    daylight_details = presentation_module.plain_state_details(
        {
            "daylight_lowest_cost": {
                "enabled": True,
                "applicable": True,
                "forecast_complete": False,
                "selected": False,
                "window_start_utc": now.isoformat(),
                "window_end_utc": (now + timedelta(hours=8)).isoformat(),
                "reason": "ev_daylight_forecast_incomplete",
                "unavailable_detail": None,
            },
            "allocation_source_now": "ready_by_fallback",
            "allocated_slots": [
                {"allocation_source": "daylight"},
                {"allocation_source": "ready_by_fallback"},
            ],
        }
    )
    assert daylight_details["Daylight lowest-cost charging"] == {
        "Enabled": True,
        "Applicable": True,
        "Forecast Complete": False,
        "Selected": False,
        "Sunrise": "Sat 27 Jun, 00:00",
        "Sunset": "Sat 27 Jun, 08:00",
        "Status": (
            "The complete remaining sunrise-to-sunset forecast is not available, so ready-by scheduling is used."
        ),
    }
    assert daylight_details["Current allocation source"] == "Ready-by fallback"
    assert daylight_details["Charging allocation sources"] == [
        "Daylight preference",
        "Ready-by fallback",
    ]
    assert (
        presentation_module.reason_summary("away_hvac_policy") == "Nobody is home, so climate control can be reduced."
    )
    assert presentation_module.reason_summary(123) == ""
    assert sensor_module._charge_state_label_from_raw("unknown") is None
    assert (
        sensor_module._charge_timeline_state_label({"state": "charging", "target_soc_percent": 80}) == "Charging to 80%"
    )
    assert sensor_module._charge_timeline_state_label({"state": "idle"}) == "Not Charging"
    assert presentation_module.display_state("") == "Unknown"
    assert presentation_module.display_state("ev_soc_ai_hvac") == "EV SOC AI HVAC"
    assert presentation_module.bounded_json({"a": {"b": {"c": {"d": {"e": 1}}}}}) == {
        "a": {"b": {"c": {"d": "<truncated>"}}}
    }
    assert presentation_module.bounded_json(list(range(13)))[-1] == {"truncated_count": 1}


def test_planned_action_windows_show_explicit_home_assistant_local_date(monkeypatch: object) -> None:
    monkeypatch.setattr(
        presentation_module.dt_util,
        "as_local",
        lambda value: value.astimezone(ZoneInfo("Australia/Melbourne")),
    )
    start = datetime(2026, 8, 8, 5, 30, tzinfo=UTC)
    action = PlanAction(
        action_id="climate-precondition",
        plan_id="plan-1",
        execute_not_before=start,
        execute_not_after=start + timedelta(minutes=30),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"phase": "preconditioning", "hvac_mode": "heat", "target_temperature": 24},
        hard_constraints=[],
        reason_codes=["hvac_thermal_shift_before_expensive_period"],
        expected_cost_delta=0.2,
        confidence=0.7,
    )

    assert presentation_module.action_window(action) == "Sat 8 Aug, 15:30-16:00"
    assert presentation_module.plain_state_details({"execute_not_before": start})["Start"] == "Sat 8 Aug, 15:30"
    action.execute_not_before = datetime(2026, 8, 8, 13, 50, tzinfo=UTC)
    action.execute_not_after = datetime(2026, 8, 8, 14, 10, tzinfo=UTC)
    assert presentation_module.action_window(action) == "Sat 8 Aug, 23:50-Sun 9 Aug, 00:10"
    action.execute_not_before = None
    assert presentation_module.action_window(action) == "Next planning window"
    assert presentation_module.date_time_label("not-a-time") == "not-a-time"
    assert presentation_module.local_datetime(datetime(2026, 8, 8, 15, 30)) == datetime(2026, 8, 8, 15, 30)


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


def _coordinator(
    plan: EnergyPlan | None,
    *,
    store_data: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
    entry_data: dict[str, object] | None = None,
    hass: object | None = None,
) -> SimpleNamespace:
    stored = dict(store_data or {})
    configured_options = dict(options or {})
    configured_entry_data = dict(entry_data or {})
    production = stored.get("production")
    if isinstance(production, dict) and production.get("dry_run_ready_cycles", 0) >= 3:
        production = dict(production)
        production.setdefault(
            "dry_run_evidence_fingerprint",
            sensor_module.production_evidence_fingerprint(configured_entry_data, configured_options),
        )
        stored["production"] = production
    active_control = (
        configured_options.get("planner_enabled") is True
        and configured_options.get("dry_run") is False
        and isinstance(stored.get("production"), dict)
        and stored["production"].get("armed") is True
    )
    return SimpleNamespace(
        data=plan,
        store=SimpleNamespace(data=stored),
        options=configured_options,
        entry_data=configured_entry_data,
        entry=SimpleNamespace(entry_id="test_entry"),
        hass=hass,
        active_control=active_control,
        effective_control=active_control,
        automatic_control_requested=(
            configured_options.get("planner_enabled") is True and configured_options.get("dry_run") is False
        ),
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


def test_next_actions_ai_explanation_exposes_latest_accepted_response() -> None:
    coordinator = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.extended_openai_ai_task", "pv_forecast_entity": "sensor.pv"},
        store_data={
            "ai_recommendations": [
                {
                    "created_at": "2026-06-27T00:00:00+00:00",
                    "plan_id": "plan-1",
                    "plan_fingerprint": _material_plan_fingerprint(_plan()),
                    "status": "accepted",
                    "service_called": "ai_task.generate_data",
                    "ai_task_entity": "ai_task.extended_openai_ai_task",
                    "rejected_reason": None,
                    "accepted": {
                        "outcome": "action_required",
                        "summary": "The PV input needs attention.",
                        "affected_item": "pv_forecast_entity",
                        "problem": "Only a current point is available.",
                        "next_step": "Map a timestamped PV forecast.",
                        "expected_benefit": "Solar-aware planning can use the full horizon.",
                        "verification": "Check forecast coverage in Next actions.",
                    },
                }
            ]
        },
    )

    attrs = sensor_module._ai_advice_attrs(coordinator)

    assert attrs["available"] is True
    assert attrs["ai_task_entity"] == "ai_task.extended_openai_ai_task"
    assert attrs["outcome"] == "action_required"
    assert attrs["summary"] == "The PV input needs attention."
    assert attrs["recommended_action"] == {
        "affected_item": {
            "key": "pv_forecast_entity",
            "name": "PV forecast input",
            "configured_value": "sensor.pv",
        },
        "problem": "Only a current point is available.",
        "next_step": "Map a timestamped PV forecast.",
        "expected_benefit": "Solar-aware planning can use the full horizon.",
        "verification": "Check forecast coverage in Next actions.",
    }


def test_next_actions_ai_explanation_uses_effective_entity_and_service_availability() -> None:
    states = {"ai_task.provider": SimpleNamespace(state="ready")}
    services_available = False
    hass = SimpleNamespace(
        states=SimpleNamespace(get=states.get),
        services=SimpleNamespace(
            has_service=lambda domain, service: services_available
            and (domain, service) == ("ai_task", "generate_data")
        ),
    )
    coordinator = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.provider"},
        hass=hass,
    )

    assert sensor_module._ai_advice_attrs(coordinator)["availability_reason"] == "ai_service_unavailable"

    services_available = True
    assert sensor_module._ai_advice_attrs(coordinator)["available"] is True

    states.clear()
    assert sensor_module._ai_advice_attrs(coordinator)["availability_reason"] == "ai_task_entity_unavailable"


def test_next_actions_ai_explanation_reuses_accepted_response_for_equivalent_new_plan() -> None:
    previous = _plan()
    current = replace(previous, plan_id="plan-2")
    coordinator = _coordinator(
        current,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={
            "ai_recommendations": [
                {
                    "created_at": "2026-06-27T00:00:00+00:00",
                    "plan_id": previous.plan_id,
                    "plan_fingerprint": _material_plan_fingerprint(previous),
                    "status": "accepted",
                    "accepted": {"outcome": "no_action_needed", "summary": "Still applicable."},
                }
            ]
        },
    )

    attrs = sensor_module._ai_advice_attrs(coordinator)

    assert attrs["plan_id"] == "plan-1"
    assert "current_plan_id" not in attrs
    assert attrs["reused_for_current_plan"] is True
    assert attrs["summary"] == "Still applicable."


def test_next_actions_ai_explanation_keeps_rejection_visible_for_equivalent_new_plan() -> None:
    previous = _plan()
    current = replace(previous, plan_id="plan-2")
    coordinator = _coordinator(
        current,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={
            "ai_recommendations": [
                {
                    "plan_id": previous.plan_id,
                    "plan_fingerprint": _material_plan_fingerprint(previous),
                    "status": "rejected",
                    "accepted": {},
                    "rejected_reason": "ai_response_not_actionable",
                    "rejected_detail": {
                        "reason": "ai_response_not_actionable",
                        "message": "The provider did not return a complete actionable result.",
                    },
                }
            ]
        },
    )

    attrs = sensor_module._ai_advice_attrs(coordinator)

    assert attrs["reused_for_current_plan"] is True
    assert attrs["rejected_reason"] == "ai_response_not_actionable"


def test_next_actions_ai_explanation_reuses_latest_legacy_nested_fingerprint() -> None:
    previous = _plan()
    current = replace(previous, plan_id="plan-2")
    coordinator = _coordinator(
        current,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={
            "ai_recommendations": [
                {
                    "plan_id": previous.plan_id,
                    "status": "accepted",
                    "rejected_detail": {"plan_fingerprint": _material_plan_fingerprint(previous)},
                    "accepted": {"outcome": "no_action_needed", "summary": "Legacy result remains applicable."},
                }
            ]
        },
    )

    assert sensor_module._ai_advice_attrs(coordinator)["summary"] == "Legacy result remains applicable."


def test_next_actions_ai_explanation_hides_cached_and_pending_results_without_provider() -> None:
    previous = _plan()
    current = replace(previous, plan_id="plan-2")
    fingerprint = _material_plan_fingerprint(current)
    coordinator = _coordinator(
        current,
        store_data={
            "ai_recommendations": [
                {
                    "plan_id": previous.plan_id,
                    "plan_fingerprint": fingerprint,
                    "status": "accepted",
                    "accepted": {"outcome": "no_action_needed", "summary": "Should be hidden."},
                }
            ]
        },
    )
    coordinator._ai_advice_pending_fingerprint = fingerprint
    coordinator._ai_advice_pending_reason = "ai_rate_limited"

    assert sensor_module._ai_advice_attrs(coordinator) == {
        "configured": False,
        "available": False,
        "availability_reason": "ai_task_entity_not_configured",
        "result": None,
    }


def test_next_actions_ai_explanation_does_not_reuse_older_matching_response() -> None:
    older = _plan()
    latest = _plan(preview=[{"import_price": 0.4}])
    current = replace(older, plan_id="plan-3")
    current_fingerprint = _material_plan_fingerprint(current)
    coordinator = _coordinator(
        current,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={
            "ai_recommendations": [
                {
                    "plan_id": older.plan_id,
                    "plan_fingerprint": _material_plan_fingerprint(older),
                    "status": "accepted",
                    "accepted": {"outcome": "no_action_needed", "summary": "Old A result."},
                },
                {
                    "plan_id": latest.plan_id,
                    "plan_fingerprint": _material_plan_fingerprint(latest),
                    "status": "accepted",
                    "accepted": {"outcome": "no_action_needed", "summary": "Latest B result."},
                },
            ]
        },
    )
    coordinator._ai_advice_pending_fingerprint = current_fingerprint
    coordinator._ai_advice_pending_reason = "request_in_flight"

    assert sensor_module._ai_advice_attrs(coordinator)["result"] is None


def test_next_actions_ai_explanation_reports_pending_for_current_plan() -> None:
    plan = _plan()
    coordinator = _coordinator(plan, entry_data={"ai_task_entity": "ai_task.provider"})
    coordinator._ai_advice_pending_fingerprint = _material_plan_fingerprint(plan)
    coordinator._ai_advice_pending_reason = "ai_rate_limited"

    assert sensor_module._ai_advice_attrs(coordinator) == {
        "configured": True,
        "available": True,
        "availability_reason": None,
        "result": None,
        "pending_reason": "ai_rate_limited",
    }

    coordinator._ai_advice_pending_fingerprint = "stale"


def test_next_actions_ai_explanation_handles_enabled_without_response_and_non_dict_payloads() -> None:
    no_response = _coordinator(_plan(), entry_data={"ai_task_entity": "ai_task.provider"})
    assert sensor_module._ai_advice_attrs(no_response)["result"] is None


    coordinator = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={
            "ai_recommendations": [
                {
                    "plan_id": "plan-1",
                    "plan_fingerprint": _material_plan_fingerprint(_plan()),
                    "status": None,
                    "accepted": "invalid",
                    "rejected_detail": "invalid",
                    "rejected_reason": None,
                }
            ]
        },
    )
    attrs = sensor_module._ai_advice_attrs(coordinator)

    assert attrs["outcome"] is None
    assert attrs["rejected_detail"] == {}

    malformed = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={"ai_recommendations": ["bad"]},
    )
    malformed.store.data["ai_recommendations"] = "bad"
    assert sensor_module._ai_advice_attrs(malformed)["result"] is None


def test_next_actions_ai_explanation_exposes_rejection_detail() -> None:
    coordinator = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.extended_openai_ai_task"},
        store_data={
            "ai_recommendations": [
                {
                    "created_at": "2026-06-27T00:00:00+00:00",
                    "plan_id": "plan-1",
                    "plan_fingerprint": _material_plan_fingerprint(_plan()),
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

    attrs = sensor_module._ai_advice_attrs(coordinator)

    assert attrs["service_called"] == "ai_task.generate_data"
    assert attrs["ai_task_entity"] == "ai_task.extended_openai_ai_task"
    assert attrs["rejected_reason"] == "ai_response_forbidden_fields"
    assert attrs["rejected_detail"]["message"] == "The AI response included forbidden fields."
    assert attrs["rejected_detail"]["fields"] == ["hard_constraint_changes"]


def test_next_actions_ai_explanation_hides_legacy_history_without_plan_fingerprint() -> None:
    coordinator = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.provider"},
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

    attrs = sensor_module._ai_advice_attrs(coordinator)

    assert attrs == {
        "configured": True,
        "available": True,
        "availability_reason": None,
        "result": None,
    }


def test_next_actions_ai_explanation_hides_stale_or_unsafe_plan_advice() -> None:
    plan = _plan()
    recommendation = {
        "plan_id": plan.plan_id,
        "plan_fingerprint": _material_plan_fingerprint(plan),
        "status": "accepted",
        "accepted": {"outcome": "no_action_needed", "summary": "stale"},
    }
    changed = _plan(preview=[{"import_price": 0.4}])
    changed_coordinator = _coordinator(
        changed,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={"ai_recommendations": [recommendation]},
    )
    assert sensor_module._ai_advice_attrs(changed_coordinator)["result"] is None
    unsafe = _plan()
    unsafe.health = InputHealth.UNSAFE
    unsafe.status = "unsafe"
    unsafe_coordinator = _coordinator(
        unsafe,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={"ai_recommendations": [recommendation]},
    )

    assert sensor_module._ai_advice_attrs(unsafe_coordinator)["result"] is None


def test_data_quality_explains_the_limiting_source_without_percentages() -> None:
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
                    "forecast_coverage": [
                        {
                            "config_key": "pv_forecast_entity",
                            "entity_id": "sensor.pv",
                            "classification": "healthy",
                            "first_timestamp": "2026-07-12T00:00:00+00:00",
                            "last_timestamp": "2026-07-12T11:55:00+00:00",
                            "covered_hours": 12.0,
                            "continuous_hours": 12.0,
                            "longest_continuous_hours": 12.0,
                            "leading_missing_slots": 0,
                            "trailing_missing_slots": 144,
                            "internal_missing_slots": 0,
                            "leading_gap_filled_slots": 0,
                            "leading_gap_filled_hours": 0.0,
                            "ignored_extra": "bounded",
                        },
                        "bad",
                    ],
                }
            ]
        },
    )

    attrs = sensor_module._next_actions_attrs(coordinator)["data_quality"]

    assert attrs["status"] == "Fallback data"
    assert attrs["summary"] == "PV forecast uses the weakest data source in this plan."
    assert attrs["limiting_inputs"] == [
        {
            "input": "PV forecast",
            "entity_id": "sensor.pv",
            "source": "Point Value Repeated",
            "reason": "Only a current point value was found, so it is repeated across the planning horizon with "
            "a conservative fallback weight.",
            "coverage": "12 of 24 planning hours available",
        }
    ]
    assert "overall_confidence" not in attrs
    assert "calculation" not in attrs
    assert "source_confidence" not in attrs
    assert attrs["improvement_actions"] == [
        "Replace PV forecast (sensor.pv) with an entity that exposes forecast data for the planning horizon, "
        "or add source confidence metadata."
    ]
    assert attrs["input_issues"] == []


def test_live_state_entities_preserve_charge_and_timeline_fallbacks() -> None:
    plan = _plan(device_plans={"ev": {"timeline": [{"state": "charging", "charge_kw": 7.2}]}})
    coordinator = _coordinator(plan, options={"ev_control_enabled": True})
    entity = PlannerSensor(coordinator, next(item for item in SENSORS if item.key == "mode"))
    assert entity.native_value == "review"
    assert sensor_module._asset_current_state(plan, ActionAsset.EV) == "Charging (7.2 kW)"
    assert sensor_module._asset_next_state(plan, ActionAsset.EV) == "Idle"
    assert sensor_module._ev_current_charge_state(coordinator) == "Charging (7.2 kW)"
    assert sensor_module._ev_current_charge_state(_coordinator(None)) == "Unknown"
    assert sensor_module._charge_timeline_state_label({"state": "charging"}) == "Charging"
    assert sensor_module._charge_timeline_state_label({"state": "unavailable"}) == "Unavailable"
    assert sensor_module._timeline_state_label({"state": "charging", "target_soc_percent": 85}) == "Charging to 85%"
    assert sensor_module._timeline_state_label({"state": "idle", "profile": "Self-Consumption"}) == (
        "Idle: Self-Consumption"
    )
    for raw, expected in [("on", "Charging"), ("fully_charged", "Fully Charged"), ("paused", "Paused")]:
        assert sensor_module._charge_state_label_from_raw(raw) == expected
    malformed = _coordinator(plan, entry_data={"ai_task_entity": "ai_task.provider"},
                             store_data={"ai_recommendations": ["invalid"]})
    assert sensor_module._next_actions_attrs(malformed)["ai_explanation"]["result"] is None


def test_shared_data_quality_handles_unsafe_and_missing_plan() -> None:
    assert presentation_module.decision_data_quality_attrs(_coordinator(None)) == {}
    plan = _plan()
    plan.health = InputHealth.UNSAFE
    plan.confidence = 0
    attrs = sensor_module._next_actions_attrs(_coordinator(plan))["data_quality"]
    assert attrs["status"] == "Unsafe inputs"
    assert presentation_module.display_state("   ") == "Unknown"
