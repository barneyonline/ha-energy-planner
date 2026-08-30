"""Tests for Energy Planner sensor entities."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import CoreState

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
from custom_components.ha_energy_planner.sensor import LEGACY_SENSOR_DESCRIPTIONS, SENSORS, PlannerSensor


def test_sensors_expose_safe_empty_values_without_plan() -> None:
    coordinator = _coordinator(None)
    values = {description.key: description.value_fn(coordinator) for description in SENSORS}
    attrs = {description.key: description.attrs_fn(coordinator) for description in SENSORS}

    assert values == {
        "mode": "review",
        "current_state": "No controls configured",
        "next_actions": "Unknown",
        "load_forecast_coverage_score": None,
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

    coordinator.data = _plan()
    assert next(item for item in SENSORS if item.key == "next_actions").value_fn(coordinator) == (
        "No controls configured"
    )

    enabled_without_actuator = _coordinator(
        _plan(), options={"climate_control_enabled": True}
    )
    assert next(item for item in SENSORS if item.key == "current_state").value_fn(
        enabled_without_actuator
    ) == "No controls configured"


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
    description = next(
        item for item in SENSORS if item.key == "load_forecast_coverage_score"
    )

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
    description = next(
        item for item in SENSORS if item.key == "load_forecast_coverage_score"
    )

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


def test_retired_sensor_helpers_remain_safe_for_diagnostics_without_a_plan() -> None:
    """Keep non-entity diagnostic formatters safe while their registry IDs retire."""
    coordinator = _coordinator(None)

    assert sensor_module._plan_status_attrs(coordinator) == {}
    assert sensor_module._asset_plan_state(None, ActionAsset.EV) == "Unknown"
    assert sensor_module._decision_audit_state(None) == "Unknown"
    assert sensor_module._decision_audit_attrs(None) == {}
    assert sensor_module._rejected_actions_state(None) == "Unknown"
    assert sensor_module._rejected_actions_attrs(None) == {}
    assert sensor_module._upcoming_timeline_state(None) == "Unknown"
    assert sensor_module._upcoming_timeline_attrs(None) == {}
    assert sensor_module._device_decision_state(None, ActionAsset.EV) == "Unknown"
    assert sensor_module._device_decision_attrs(None, ActionAsset.EV) == {}
    assert sensor_module._asset_plan_attrs(None, ActionAsset.EV) == {}
    assert sensor_module._asset_current_state(None, ActionAsset.EV) == "Unknown"
    assert sensor_module._asset_next_state(None, ActionAsset.EV) == "Unknown"
    assert sensor_module._asset_state_attrs(None, ActionAsset.EV, "current") == {}
    assert sensor_module._ev_next_charge_state(None) == "Unknown"
    assert sensor_module._ev_charge_state_attrs(coordinator, "current") == {
        "configured_charging_entity": None,
        "live_state": None,
    }
    assert sensor_module._presence_state(None) == "Unknown"
    assert sensor_module._confidence_breakdown_state(coordinator) == "Unknown"
    assert sensor_module._confidence_breakdown_attrs(coordinator) == {}
    assert sensor_module._execution_audit_state(coordinator) == "No Activity"
    assert sensor_module._dry_run_comparison_state(coordinator) == "No Dry Run"
    assert sensor_module._support_bundle_state(coordinator) == "No Plan"


def test_plan_status_ignores_retired_optimizer_history() -> None:
    coordinator = _coordinator(_plan(), store_data={"haeo_runs": [{"status": "failed"}]})

    assert "haeo" not in sensor_module._plan_status_attrs(coordinator)


def test_consolidated_ownership_covers_enphase_and_disabled_control_reason() -> None:
    assert sensor_module._asset_owned(
        {"ownership": {"enphase_profile_changed_at": "2026-08-08T00:00:00+00:00"}},
        ActionAsset.ENPHASE,
    ) is True
    coordinator = _coordinator(
        _plan(),
        options={
            "ev_control_enabled": False,
            "climate_control_enabled": True,
            "enphase_control_enabled": True,
        },
    )
    assert "ev_control_disabled" in sensor_module._control_block_attrs(coordinator)["reasons"]


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

    assert next_actions.value_fn(coordinator) == (
        "Climate: Preconditioning: Heat to 21 C | EV: Start EV charging"
    )
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
    assert [item["asset"] for item in current.attrs_fn(coordinator)["controlled_assets"]] == [
        "Climate"
    ]
    assert next_actions.value_fn(coordinator) == "Climate: Heat to 21 C"
    next_attrs = next_actions.attrs_fn(coordinator)
    assert next_attrs["action_count"] == 1
    assert [action["action_id"] for action in next_attrs["actions"]] == ["climate-1"]


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
                "until": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
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

    confidence = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "confidence_breakdown")
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")
    block = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "control_block_reason")
    audit = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "execution_audit")
    comparison = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "dry_run_comparison")
    support = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "support_bundle_summary")

    assert confidence.value_fn(coordinator) == "Unsafe inputs"
    assert confidence.attrs_fn(coordinator)["status"] == "Unsafe inputs"
    assert confidence.attrs_fn(coordinator)["input_issues"][0] == {
        "code": "pv_forecast_entity_unavailable",
        "description": "PV Forecast Entity Unavailable",
    }
    assert production.value_fn(coordinator) == "Armed"
    assert production.attrs_fn(coordinator)["ready_to_arm"] is True
    assert production.attrs_fn(coordinator)["dry_run_evidence_complete"] is True
    assert block.value_fn(coordinator) == "PV Forecast Entity Unavailable"
    assert block.attrs_fn(coordinator)["reasons"] == [
        "pv_forecast_entity_unavailable",
        "ev_soc_entity_unavailable",
        "weather_entity_unavailable",
    ]
    assert block.attrs_fn(coordinator)["available_control_areas"] == ["hvac", "enphase"]
    assert block.attrs_fn(coordinator)["paused_control_areas"] == ["ev"]
    assert audit.value_fn(coordinator) == "Rejected"
    assert audit.attrs_fn(coordinator)["outcome_count"] == 2
    assert comparison.value_fn(coordinator) == "2 Planned"
    assert comparison.attrs_fn(coordinator)["latest"]["plan_id"] == "plan-1"
    assert support.value_fn(coordinator) == "Needs Review"
    assert support.attrs_fn(coordinator)["latest_ai"] == {
        "configured": False,
        "available": False,
        "availability_reason": "ai_task_entity_not_configured",
        "result": None,
    }


def test_dry_run_comparison_attributes_stay_below_recorder_limit() -> None:
    huge_text = "x" * 20_000
    comparisons = [
        {
            "created_at": f"2026-07-{day:02d}T00:00:00+00:00",
            "plan_id": f"plan-{day}-{huge_text}",
            "planned_action_count": day,
            "estimated_daily_cost": 1.23,
            "recent_outcome_count": 5,
            "recent_outcomes": [{"pre_state": huge_text, "post_state": huge_text}] * 5,
            "next_action": {
                "action_id": huge_text,
                "asset": huge_text,
                "kind": huge_text,
                "execute_not_before": huge_text,
                "execute_not_after": huge_text,
                "desired_state": {huge_text: huge_text},
                "hard_constraints": [huge_text] * 8,
                "reason_codes": [huge_text] * 8,
            },
        }
        for day in range(1, 7)
    ]
    coordinator = _coordinator(_plan(), store_data={"dry_run_comparisons": comparisons})
    comparison = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "dry_run_comparison")

    attrs = comparison.attrs_fn(coordinator)

    assert len(json.dumps(attrs, separators=(",", ":")).encode()) < 16_384
    assert len(attrs["recent"]) == 5
    assert len(attrs["latest"]["plan_id"]) == 256
    assert len(attrs["latest"]["next_action"]["action_id"]) == 256
    assert "recent_outcomes" not in attrs["latest"]
    assert "next_action" not in attrs["recent"][-1]


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

    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "decision_audit").value_fn(coordinator)
        == "1 Accepted"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "rejected_actions").value_fn(coordinator)
        == "1 Rejected"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "upcoming_timeline").value_fn(coordinator)
        == "1 Upcoming"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_decision").value_fn(coordinator)
        == "Accepted"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "enphase_decision").value_fn(coordinator)
        == "Rejected"
    )
    ev_attrs = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_decision").attrs_fn(coordinator)
    timeline_attrs = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "upcoming_timeline").attrs_fn(
        coordinator
    )
    assert ev_attrs["summary"] == "EV action was selected because the EV needs charge before its ready-by time."
    assert timeline_attrs["rows"][0]["estimated_kwh"] == 3.5

    empty_plan = _plan()
    empty_coordinator = _coordinator(empty_plan)
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "decision_audit").attrs_fn(empty_coordinator)[
            "accepted"
        ]
        == []
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "rejected_actions").attrs_fn(empty_coordinator)[
            "rejected"
        ]
        == []
    )
    assert (
        sensor_module._device_decision_summary(ActionAsset.DAIKIN, None, {"device": "Climate"})
        == "Climate action was considered but not selected."
    )
    assert (
        sensor_module._device_decision_summary(ActionAsset.DAIKIN, None, None)
        == "Climate was not considered in this planning run."
    )


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
    confidence = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "confidence_breakdown")

    attrs = confidence.attrs_fn(coordinator)

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


def test_data_quality_reports_input_issues_good_data_and_missing_coverage() -> None:
    issue_plan = _plan(input_issues=["weather_entity_unavailable"])
    issue_attrs = sensor_module._decision_data_quality_attrs(_coordinator(issue_plan))
    assert issue_attrs["status"] == "Input issue"
    assert issue_attrs["summary"] == "1 input issue is limiting this plan."

    issue_plan.input_issues.append("pv_forecast_entity_stale")
    assert sensor_module._decision_data_quality_attrs(_coordinator(issue_plan))["summary"] == (
        "2 input issues are limiting this plan."
    )

    good_plan = _plan()
    good_plan.confidence = 1.0
    good_attrs = sensor_module._decision_data_quality_attrs(_coordinator(good_plan))
    assert good_attrs["status"] == "Good"
    assert good_attrs["summary"] == "No material input-quality limitation affected this plan."
    assert sensor_module._coverage_summary({}, 24) is None


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
    assert (
        sensor_module._forecast_coverage_sources(
            _coordinator(plan, store_data={"forecast_snapshots": [{"plan_id": "plan-1", "forecast_coverage": "bad"}]})
        )
        == []
    )
    assert "stitched" in sensor_module._confidence_source_reason({"source": "forecast_series_stitched"})
    assert (
        sensor_module._confidence_source_reason({"source": "forecast_series_leading_fill"})
        == "Confidence source was not classified."
    )
    assert "shorter" in sensor_module._confidence_source_reason({"source": "forecast_series_partial"})
    assert "fails closed" in sensor_module._confidence_source_reason({"source": "point_value_only"})
    assert sensor_module._confidence_limiting_factor(0.0, 0.0, 0.0) == "unsafe_inputs"
    assert sensor_module._confidence_limiting_factor(0.65, 0.65, None) == "input_health"
    assert sensor_module._confidence_limiting_factor(1.0, 1.0, None) == "unknown"
    assert sensor_module._confidence_limiting_factor(0.65, 0.65, 0.65) == "input_health_and_forecast_sources"
    assert sensor_module._confidence_limiting_factor(0.65, 0.65, 0.9) == "input_health"
    assert sensor_module._confidence_limiting_factor(0.7, 1.0, 0.7) == "forecast_sources"
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
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")
    audit = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "execution_audit")
    comparison = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "dry_run_comparison")
    support = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "support_bundle_summary")

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
    assert sensor_module._pause_active("corrupt") is True


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
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")

    assert production.value_fn(coordinator) == "Evidence Complete"
    assert production.attrs_fn(coordinator)["required_control_areas"] == ["ev"]

    coordinator.store.data["production"]["armed"] = True
    assert production.value_fn(coordinator) == "Armed"


def test_production_readiness_keeps_unpaused_control_area_available() -> None:
    coordinator = _coordinator(
        _plan(),
        options={
            "ev_control_enabled": True,
            "climate_control_enabled": True,
            "planner_enabled": True,
            "dry_run": False,
        },
        entry_data={
            "ev_smart_charging_start_entity": "button.ev_start",
            "daikin_climate_entity": "climate.home",
        },
        store_data={
            "production": {"armed": True, "dry_run_ready_cycles": 3},
            "control_pause": {"active": True, "assets": ["ev"]},
        },
    )
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")

    attrs = production.attrs_fn(coordinator)

    assert production.value_fn(coordinator) == "Armed"
    assert attrs["control_paused"] is False
    assert attrs["available_control_areas"] == ["hvac"]
    assert attrs["paused_control_areas"] == ["ev"]

    coordinator.store.data["control_pause"] = {"active": True, "assets": ["all"]}
    block = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "control_block_reason")
    assert production.value_fn(coordinator) == "Armed - Blocked"
    assert block.attrs_fn(coordinator)["reason"] == "planner_paused"


def test_production_readiness_exposes_startup_auto_recovery_progress() -> None:
    coordinator = _coordinator(
        _plan(),
        options={"ev_control_enabled": True, "planner_enabled": True, "dry_run": False},
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
        store_data={
            "production": {
                "startup_auto_recovery": {
                    "status": "validating",
                    "successful_runs": 2,
                    "required_runs": 3,
                    "started_at": "2026-08-14T12:00:00+00:00",
                    "deadline": "2026-08-14T12:10:00+00:00",
                    "last_reason": "validation_succeeded",
                }
            }
        },
    )
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")

    attrs = production.attrs_fn(coordinator)

    assert attrs["startup_auto_recovery_status"] == "validating"
    assert attrs["startup_auto_recovery_successful_runs"] == 2
    assert attrs["startup_auto_recovery_required_runs"] == 3
    assert attrs["startup_auto_recovery_grace_started_at"] == "2026-08-14T12:00:00+00:00"
    assert attrs["startup_auto_recovery_last_reason"] == "validation_succeeded"
    assert attrs["automatic_control_requested"] is True
    assert attrs["automatic_control_running"] is False


def test_production_readiness_blocks_armed_mismatched_contract() -> None:
    coordinator = _coordinator(
        _plan(),
        options={"ev_control_enabled": True},
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
        store_data={"production": {"armed": True, "dry_run_ready_cycles": 3}},
    )
    coordinator.entry_data["ev_smart_charging_start_entity"] = "button.ev_replaced"
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")

    assert production.value_fn(coordinator) == "Armed - Blocked"
    assert production.attrs_fn(coordinator)["dry_run_evidence_fingerprint_matches"] is False
    assert production.attrs_fn(coordinator)["dry_run_evidence_complete"] is False
    block = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "control_block_reason")
    assert "production_evidence_contract_changed" in block.attrs_fn(coordinator)["reasons"]


def test_production_sensors_fail_closed_for_missing_and_malformed_state() -> None:
    coordinator = _coordinator(
        _plan(),
        options={"ev_control_enabled": True},
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
    )
    production = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "production_readiness")
    block = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "control_block_reason")

    for value in (
        None,
        "corrupt",
        {"armed": "true", "dry_run_ready_cycles": "3"},
        {"armed": 1, "dry_run_ready_cycles": 10_001},
    ):
        if value is None:
            coordinator.store.data.pop("production", None)
        else:
            coordinator.store.data["production"] = value
        assert production.value_fn(coordinator) == "Not Ready"
        assert production.attrs_fn(coordinator)["dry_run_ready_cycles"] == 0
        assert block.attrs_fn(coordinator)["armed"] is False
        assert block.attrs_fn(coordinator)["reason"] == "production_gate_not_armed"

    coordinator.store.data["production"] = {
        "armed": True,
        "dry_run_ready_cycles": "3",
        "dry_run_evidence_fingerprint": sensor_module.production_evidence_fingerprint(
            coordinator.entry_data, coordinator.options
        ),
    }
    assert production.value_fn(coordinator) == "Armed - Blocked"
    assert block.attrs_fn(coordinator)["reason"] == "production_dry_run_evidence_incomplete"


def test_sensor_platform_setup_groups_planner_sensors(monkeypatch: object) -> None:
    coordinator = _coordinator(_plan())
    entry = SimpleNamespace(entry_id="test_entry", runtime_data=coordinator)
    added: list[tuple[object, object, object]] = []
    removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str:
            return f"sensor.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.append((entry_arg, add_entities, list(entities)))

    monkeypatch.setattr(sensor_module, "async_add_planner_entities", fake_add_planner_entities)
    monkeypatch.setattr(sensor_module.er, "async_get", lambda hass: FakeRegistry())

    asyncio.run(sensor_module.async_setup_entry(SimpleNamespace(), entry, "add_entities"))

    assert added[0][0] is entry
    assert added[0][1] == "add_entities"
    assert len(added[0][2]) == len(SENSORS)
    assert len(removed) == len(LEGACY_SENSOR_DESCRIPTIONS)


def test_planner_sensor_delegates_value_and_attributes() -> None:
    coordinator = _coordinator(_plan())
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "plan_status")
    sensor = PlannerSensor(coordinator, description)

    assert sensor.native_value == "Current"
    assert sensor.native_unit_of_measurement is None
    assert sensor.extra_state_attributes["plan_id"] == "plan-1"


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

    attrs = PlannerSensor(
        _coordinator(plan, options={"ev_control_enabled": True}), description
    ).extra_state_attributes
    encoded_size = len(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    assert encoded_size <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
    assert attrs["attributes_truncated"] is True
    assert attrs["action_count"] == 12


def test_estimated_cost_sensor_uses_home_assistant_currency_and_horizon() -> None:
    plan = _plan()
    plan.estimated_cost_horizon_hours = 6.5
    coordinator = _coordinator(plan, hass=SimpleNamespace(config=SimpleNamespace(currency="NZD")))
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "estimated_daily_cost")
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "forecast_confidence")

    attrs = description.attrs_fn(coordinator)

    assert attrs["calibration_enabled"] is True
    assert attrs["fields"]["pv_forecast_kw"]["enabled_lead_buckets"] == 1
    assert attrs["fields"]["pv_forecast_kw"]["lead_buckets"]["0"]["lower_factor"] == 0.7


def test_forecast_calibration_attributes_reject_malformed_store_shapes() -> None:
    assert sensor_module._forecast_calibration_attrs(
        _coordinator(_plan(), store_data={"forecast_calibration": "invalid"})
    ) == {"calibration_enabled": False, "fields": {}}
    assert sensor_module._forecast_calibration_attrs(
        _coordinator(
            _plan(),
            store_data={"forecast_calibration": {"pv_forecast_kw": "invalid"}},
        )
    ) == {"calibration_enabled": False, "fields": {}}

    attrs = sensor_module._forecast_calibration_attrs(
        _coordinator(
            _plan(),
            store_data={
                "forecast_calibration": {
                    "pv_forecast_kw": {"sample_count": 1, "buckets": "invalid"},
                }
            },
        )
    )
    assert attrs == {
        "calibration_enabled": False,
        "fields": {
            "pv_forecast_kw": {
                "sample_count": 1,
                "enabled_lead_buckets": 0,
                "uncertainty_enabled_lead_buckets": 0,
                "lead_buckets": {},
            }
        },
    }


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
    assert sensor_module._action_load_forecast_attrs(coordinator, "missing") == {}
    assert sensor_module._action_load_forecast_attrs(
        _coordinator(plan, store_data={"forecast_snapshots": []}),
        "missing",
    ) == {}
    assert sensor_module._action_load_forecast_attrs(
        _coordinator(
            plan,
            store_data={
                "forecast_snapshots": [
                    {"plan_id": plan.plan_id, "action_load_forecasts": []}
                ]
            },
        ),
        "missing",
    ) == {}
    assert (
        sensor_module._confidence_source_reason({"source": "built_in_recorder_history"})
        == "A local deterministic forecast learned from measured household load in Home Assistant Recorder."
    )


def test_plan_status_attributes_are_json_friendly_and_bounded() -> None:
    plan = _plan(
        preview=[{"slot": index} for index in range(20)],
        input_issues=[f"issue_{index}" for index in range(30)],
    )
    coordinator = _coordinator(plan)
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "plan_status")

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
    )
    plan = _plan(actions=[action])
    coordinator = _coordinator(plan)
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "next_action")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Schedule EV charging"
    assert attrs == {
        "action": "Schedule EV charging",
        "decision": "Schedule EV charging to 80%.",
        "when": "Sat 27 Jun, 00:00-00:05",
        "why": "Charging was placed in the cheapest slots before the ready-by time.",
        "constraints": ["EV Bounds"],
        "desired_state": {"Target SOC percent": 80},
        "estimated_value": -0.25,
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
    )
    ev_action = PlanAction(
        action_id="ev-1",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"target_soc_percent": 80, "allocated_slots": [{"slot": index} for index in range(20)]},
        hard_constraints=["ready_by"],
        reason_codes=["least_cost_slots_before_ready_by"],
        expected_cost_delta=None,
        confidence=0.8,
    )
    plan = _plan(actions=[climate_action, ev_action])
    coordinator = _coordinator(
        plan,
        store_data={"ev_charge_calibration": {"status": "ready", "soc_per_kwh": 1.8}},
    )

    climate = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_plan")
    ev = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_charging_plan")

    assert climate.value_fn(coordinator) == "Change climate state"
    climate_attrs = climate.attrs_fn(coordinator)
    assert climate_attrs["planned_actions"][0]["decision"] == "Set climate to Cool at 22 C."
    assert climate_attrs["planned_actions"][0]["why"] == "Preconditioning before a more expensive electricity period."
    assert climate_attrs["planned_actions"][0]["desired_state"]["Target temperature C"] == 22
    assert ev.value_fn(coordinator) == "Schedule EV charging"
    ev_attrs = ev.attrs_fn(coordinator)
    assert ev_attrs["charge_calibration"] == {"status": "ready", "soc_per_kwh": 1.8}
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
    ev = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_charging_plan")
    climate = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_plan")
    enphase = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "enphase_plan")

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
    climate = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_plan")

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
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_current_state").value_fn(coordinator)
        == "Heat (21.5 C)"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_next_state").value_fn(coordinator)
        == "Off"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "enphase_current_state").value_fn(coordinator)
        == "Idle"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "enphase_next_state").value_fn(coordinator)
        == "Charge Battery (2.5 kW)"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_current_state").value_fn(coordinator)
        == "Charging to 80%"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_next_state").value_fn(coordinator) == "Idle"
    )


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

    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_current_state").value_fn(coordinator)
        == "Unknown"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "enphase_current_state").value_fn(coordinator)
        == "Unknown"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "enphase_next_state").value_fn(coordinator)
        == "Consume Battery (1.25 kW)"
    )
    assert (
        next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_next_state").value_fn(coordinator) == "Idle"
    )


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
    current = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_current_state")
    next_state = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "climate_next_state")

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
    current_charge = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_current_charge_state")
    next_charge = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_next_charge_state")

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
    current = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_current_charge_state")
    next_charge = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_next_charge_state")

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


def test_ev_plan_attributes_exclude_raw_charge_calibration_samples() -> None:
    coordinator = _coordinator(
        _plan(),
        store_data={
            "ev_charge_calibration": {
                "status": "ready",
                "soc_per_kwh": 1.8,
                "samples": [{"soc_gain_percent": 14}],
            }
        },
    )
    ev = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ev_charging_plan")

    assert ev.attrs_fn(coordinator)["charge_calibration"] == {"status": "ready", "soc_per_kwh": 1.8}


def test_presence_sensor_exposes_inferred_occupancy_context() -> None:
    coordinator = _coordinator(
        _plan(preview=[{"start": "2026-06-27T00:00:00+00:00", "occupied": "away"}]),
        entry_data={"person_entities": ["person.james", "person.cath"]},
    )
    presence = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "presence_state")

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
    presence = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "presence_state")

    assert presence.value_fn(coordinator) == "Unknown"
    assert presence.attrs_fn(coordinator)["person_entities"] == ["person.james"]


def test_ai_advice_sensor_exposes_latest_accepted_response() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "Action Required"
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


def test_ai_advice_sensor_uses_effective_entity_and_service_availability() -> None:
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
    description = next(
        item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice"
    )

    assert description.value_fn(coordinator) == "Unavailable"
    assert description.attrs_fn(coordinator)["availability_reason"] == "ai_service_unavailable"

    services_available = True
    assert description.value_fn(coordinator) == "No response"
    assert description.attrs_fn(coordinator)["available"] is True

    states.clear()
    assert description.value_fn(coordinator) == "Unavailable"
    assert description.attrs_fn(coordinator)["availability_reason"] == "ai_task_entity_unavailable"


def test_ai_advice_sensor_reuses_accepted_response_for_equivalent_new_plan() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "No Action Needed"
    assert attrs["plan_id"] == "plan-1"
    assert "current_plan_id" not in attrs
    assert attrs["reused_for_current_plan"] is True
    assert attrs["summary"] == "Still applicable."


def test_ai_advice_sensor_keeps_rejection_visible_for_equivalent_new_plan() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "No actionable result"
    assert attrs["reused_for_current_plan"] is True
    assert attrs["rejected_reason"] == "ai_response_not_actionable"


def test_ai_advice_sensor_reuses_latest_legacy_nested_fingerprint() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    assert description.value_fn(coordinator) == "No Action Needed"
    assert description.attrs_fn(coordinator)["summary"] == "Legacy result remains applicable."


def test_ai_advice_sensor_hides_cached_and_pending_results_without_provider() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    assert description.value_fn(coordinator) == "Not configured"
    assert description.attrs_fn(coordinator) == {
        "configured": False,
        "available": False,
        "availability_reason": "ai_task_entity_not_configured",
        "result": None,
    }


def test_ai_advice_sensor_does_not_reuse_older_matching_response() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    assert description.value_fn(coordinator) == "Pending"
    assert description.attrs_fn(coordinator)["result"] is None


def test_ai_advice_sensor_reports_pending_for_current_plan() -> None:
    plan = _plan()
    coordinator = _coordinator(plan, entry_data={"ai_task_entity": "ai_task.provider"})
    coordinator._ai_advice_pending_fingerprint = _material_plan_fingerprint(plan)
    coordinator._ai_advice_pending_reason = "ai_rate_limited"
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    assert description.value_fn(coordinator) == "Pending"
    assert description.attrs_fn(coordinator) == {
        "configured": True,
        "available": True,
        "availability_reason": None,
        "result": None,
        "pending_reason": "ai_rate_limited",
    }

    coordinator._ai_advice_pending_fingerprint = "stale"
    assert description.value_fn(coordinator) == "No response"


def test_ai_advice_sensor_handles_enabled_without_response_and_non_dict_payloads() -> None:
    no_response = _coordinator(_plan(), entry_data={"ai_task_entity": "ai_task.provider"})
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    assert description.value_fn(no_response) == "No response"

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
    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "No actionable result"
    assert attrs["outcome"] is None
    assert attrs["rejected_detail"] == {}

    malformed = _coordinator(
        _plan(),
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={"ai_recommendations": ["bad"]},
    )
    assert description.value_fn(malformed) == "No response"
    malformed.store.data["ai_recommendations"] = "bad"
    assert description.attrs_fn(malformed)["result"] is None


def test_ai_advice_sensor_exposes_rejection_detail() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "No actionable result"
    assert attrs["service_called"] == "ai_task.generate_data"
    assert attrs["ai_task_entity"] == "ai_task.extended_openai_ai_task"
    assert attrs["rejected_reason"] == "ai_response_forbidden_fields"
    assert attrs["rejected_detail"]["message"] == "The AI response included forbidden fields."
    assert attrs["rejected_detail"]["fields"] == ["hard_constraint_changes"]


def test_ai_advice_sensor_hides_legacy_history_without_plan_fingerprint() -> None:
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
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")

    attrs = description.attrs_fn(coordinator)

    assert description.value_fn(coordinator) == "No response"
    assert attrs == {
        "configured": True,
        "available": True,
        "availability_reason": None,
        "result": None,
    }


def test_ai_advice_sensor_hides_stale_or_unsafe_plan_advice() -> None:
    plan = _plan()
    recommendation = {
        "plan_id": plan.plan_id,
        "plan_fingerprint": _material_plan_fingerprint(plan),
        "status": "accepted",
        "accepted": {"outcome": "no_action_needed", "summary": "stale"},
    }
    description = next(item for item in LEGACY_SENSOR_DESCRIPTIONS if item.key == "ai_advice")
    changed = _plan(preview=[{"import_price": 0.4}])
    changed_coordinator = _coordinator(
        changed,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={"ai_recommendations": [recommendation]},
    )
    unsafe = _plan()
    unsafe.health = InputHealth.UNSAFE
    unsafe.status = "unsafe"
    unsafe_coordinator = _coordinator(
        unsafe,
        entry_data={"ai_task_entity": "ai_task.provider"},
        store_data={"ai_recommendations": [recommendation]},
    )

    assert description.value_fn(changed_coordinator) == "No response"
    assert description.attrs_fn(unsafe_coordinator)["result"] is None


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
    assert "requires_haeo_plan" not in sensor_module._plain_action(profile_action)
    assert sensor_module._action_sentence(restore_action) == "Restore Enphase to AI Optimisation."
    assert sensor_module._action_sentence(climate_action) == "Set climate to Off."
    climate_action.kind = ActionKind.RELEASE_HVAC
    assert sensor_module._action_sentence(climate_action) == "Release climate control to the configured automations."
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
        "Start": "Sat 27 Jun, 00:00",
        "Bad Time": "not-a-time",
    }
    daylight_details = sensor_module._plain_state_details(
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
            "The complete remaining sunrise-to-sunset forecast is not available, "
            "so ready-by scheduling is used."
        ),
    }
    assert daylight_details["Current allocation source"] == "Ready-by fallback"
    assert daylight_details["Charging allocation sources"] == [
        "Daylight preference",
        "Ready-by fallback",
    ]
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


def test_planned_action_windows_show_explicit_home_assistant_local_date(monkeypatch: object) -> None:
    monkeypatch.setattr(
        sensor_module.dt_util,
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

    assert sensor_module._action_window(action) == "Sat 8 Aug, 15:30-16:00"
    assert sensor_module._time_label(start.isoformat()) == "15:30"
    assert sensor_module._plain_state_details({"execute_not_before": start})["Start"] == "Sat 8 Aug, 15:30"
    action.execute_not_before = datetime(2026, 8, 8, 13, 50, tzinfo=UTC)
    action.execute_not_after = datetime(2026, 8, 8, 14, 10, tzinfo=UTC)
    assert sensor_module._action_window(action) == "Sat 8 Aug, 23:50-Sun 9 Aug, 00:10"
    action.execute_not_before = None
    assert sensor_module._action_window(action) == "Next planning window"
    assert sensor_module._date_time_label("not-a-time") == "not-a-time"
    assert sensor_module._local_datetime(datetime(2026, 8, 8, 15, 30)) == datetime(2026, 8, 8, 15, 30)


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
            configured_options.get("planner_enabled") is True
            and configured_options.get("dry_run") is False
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
