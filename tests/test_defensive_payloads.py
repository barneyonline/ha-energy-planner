"""Malformed and incomplete payloads retain conservative public behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_calendar import _action, _coordinator, _plan

from custom_components.ha_energy_planner import (
    ai_advisor,
    config_flow,
    forecasts,
    inputs,
    plan_presentation,
    planner_confidence,
    planner_presentation,
    recorder_import,
    sensor,
    storage,
)
from custom_components.ha_energy_planner.const import CONF_EV_CHARGING, CONF_EV_KEEP_CHARGER_ON
from custom_components.ha_energy_planner.coordinator import _bounded_json, _hvac_control_from_ownership
from custom_components.ha_energy_planner.entity import recorder_safe_identifier
from custom_components.ha_energy_planner.forecast_accuracy import summarize_forecast_accuracy
from custom_components.ha_energy_planner.ownership import OwnershipState
from custom_components.ha_energy_planner.preflight import _apply_ev_keep_on_preflight
from custom_components.ha_energy_planner.thermal_model import THERMAL_MODEL_VERSION, update_thermal_model

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def test_forecast_metadata_skips_invalid_earlier_keys_and_out_of_order_points():
    assert (
        forecasts._explicit_interval_minutes({"forecast_interval_minutes": 0, "interval_minutes": float("nan")}) is None
    )
    points = [
        {"valid_at": "invalid", "period_start": (NOW + timedelta(hours=1)).isoformat(), "value": 1},
        {"valid_at": NOW.isoformat(), "value": 2},
    ]
    state = SimpleNamespace(attributes={"forecast": points})
    assert forecasts.latest_forecast_valid_at_from_state(state, value_keys=("value",)) == NOW + timedelta(hours=1)
    converted = forecasts._energy_items_as_average_power(points, ("value",), "kWh")
    assert len(converted) == 2
    assert all(item["value"] > 0 for item in converted)


def test_ai_incomplete_wrappers_and_optional_preview_metadata():
    assert ai_advisor._parse_response({"response": {"unknown": "ignored"}}) is None
    assert ai_advisor._preview_summary([{}]) == {"samples": 1}
    assert ai_advisor._extract_response_text({"speech": {"plain": {"speech": 42}, "speech": []}}) is None
    for reason in ("ai_response_forbidden_fields", "unrecognized_reason"):
        detail = ai_advisor._invalid_response_detail({}, reason)
        assert detail["reason"] == reason
        assert "fields" not in detail


def test_selector_and_priority_inputs_without_optional_values():
    selector = config_flow._entity_selector()
    assert "domain" not in selector.config
    assert config_flow._priority_values_from_string("invalid, cost, cost") == ["cost"]


def test_numeric_input_helpers_preserve_missing_data():
    manager = inputs.InputManager(
        SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(state="invalid"))),
        {CONF_EV_CHARGING: "sensor.charging"},
        {},
    )
    assert manager._optional_bool_state(CONF_EV_CHARGING) == (None, f"{CONF_EV_CHARGING}_unsupported_state")
    manager._record_forecast_confidence(0.4)
    assert manager._forecast_confidence_scores == [0.4]
    assert manager.forecast_confidence_details == []
    assert inputs._leading_present_slots([1.0, 2.0]) == 2
    assert inputs._percent_float_or_none(45) == 45
    state = SimpleNamespace(state="1", last_updated=NOW, attributes={})
    assert inputs._forecast_source_status(state, now=NOW, freshness_minutes=5, value_keys=("value",)) == "usable"
    assert _bounded_json([1, 2]) == [1, 2]
    assert _hvac_control_from_ownership(
        {"hvac_control": {"phase": "away_off"}, "planner_takeover_started_at": "bad"}
    ) == {"phase": "away_off"}


def test_timeline_optional_action_fields_and_missing_forecast_confidence():
    action = _action("minimal", NOW, NOW + timedelta(minutes=5))
    action.desired_state = {}
    slot = SimpleNamespace(projected_ev_load_kw=2.0)
    assert planner_presentation._ev_timeline_entry(slot, [action], [action]) == {
        "state": "charging",
        "charge_kw": 2.0,
        "reason_codes": action.reason_codes[:4],
    }
    assert planner_presentation._planned_enphase_profile([action]) is None
    assert planner_presentation._enphase_next_state_label({"state": "charging"}) == "Charging"
    assert len(planner_confidence._confidence_checks("unknown")) == 1
    owner = _coordinator(_plan([]))
    owner.store.data["forecast_snapshots"] = [
        {"plan_id": owner.data.plan_id, "confidence": {"forecast_source_confidence": "invalid"}}
    ]
    assert plan_presentation.forecast_source_confidence(owner) == owner.data.confidence
    owner.data = None
    assert (
        sensor._planning_duration_value(SimpleNamespace(refresh_metrics={}, last_refresh_metadata={"duration_ms": 12}))
        == 12
    )
    owner.entry_data = {CONF_EV_CHARGING: "sensor.charging"}
    owner.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(state="unknown")))
    assert sensor._ev_current_charge_state(owner) == "Unknown"


def test_identifier_budget_can_be_shorter_than_a_multibyte_character():
    identifier = recorder_safe_identifier("⚡" * 20, max_bytes=16)
    assert len(identifier.encode()) <= 16
    assert identifier.startswith("...-")


def test_keep_on_preflight_does_not_duplicate_existing_issue():
    issue = "ev_keep_on_requires_stateful_control"
    discovery = {"ev": {"issues": [issue]}}
    _apply_ev_keep_on_preflight(discovery, {}, {CONF_EV_KEEP_CHARGER_ON: True})
    assert discovery["ev"]["issues"] == [issue]
    assert discovery["ev"]["supported"] is False


def test_takeover_keeps_original_automation_baseline_and_unexpired_hold():
    ownership = OwnershipState()
    ownership.begin_hvac_takeover({"automation.a": "on"}, NOW)
    ownership.begin_hvac_takeover({"automation.a": "off"}, NOW + timedelta(minutes=1))
    ownership.set_manual_hvac_override(NOW, timedelta(hours=1))
    ownership.clear_expired_overrides(NOW)
    assert ownership.manual_hvac_override_active(NOW)
    assert ownership.restore_hvac() == {"automation.a": "on"}


def test_forecast_accuracy_omits_empty_horizon_bucket():
    summary = summarize_forecast_accuracy(
        [
            {
                "issued_at": NOW.isoformat(),
                "valid_at": (NOW + timedelta(hours=1)).isoformat(),
                "lead_hours": 1,
                "forecast": 1,
                "actual": 1,
                "baseline": 1,
            },
        ],
        [{"name": "later", "min_hours": 2, "max_hours": 4}],
    )
    assert summary["horizon_buckets"] == {}
    assert summary["overall"]["sample_count"] == 1


@pytest.mark.parametrize(
    ("mode", "power", "delta", "expected_stat"),
    [
        ("heat", 0.15, 0.5, "active_heat_rate_c_per_hour"),
        ("cool", 11, -0.5, "active_cool_rate_c_per_hour"),
        ("off", 0, 4, None),
    ],
)
def test_thermal_rates_do_not_learn_out_of_bounds_power_or_passive_drift(mode, power, delta, expected_stat):
    previous = {"sampled_at": NOW.isoformat(), "hvac_mode": mode, "hvac_power_kw": power, "indoor_temperature_c": 20}
    current = {**previous, "sampled_at": (NOW + timedelta(hours=1)).isoformat(), "indoor_temperature_c": 20 + delta}
    model, changed = update_thermal_model({"model_version": THERMAL_MODEL_VERSION}, previous, current)
    assert changed
    assert "active_hvac_load_kw" not in model
    assert "passive_indoor_drift_c_per_hour" not in model
    if expected_stat:
        assert model[expected_stat]["sample_count"] == 1


def test_recorder_drops_rows_without_timestamps_and_keeps_no_future_soc():
    missing = SimpleNamespace(state="on")
    charging = SimpleNamespace(state="on", last_changed=NOW)
    soc = SimpleNamespace(state="50", last_changed=NOW + timedelta(minutes=1))
    assert recorder_import._deduplicate_states([missing, charging]) == [charging]
    actual_charging, actual_soc = recorder_import._compact_ev_history_chunk([missing, charging], [missing, soc])
    assert actual_charging == [charging]
    assert actual_soc == [soc]


def test_storage_accepts_missing_action_list_and_same_plan_bucket():
    assert storage._normalize_loaded_data({"active_plan": {"actions": None}})["active_plan"]["actions"] is None
    item = {"created_at": NOW.isoformat(), "plan_id": "same"}
    updated = storage._inherit_forecast_bucket_metadata([item], item, minutes=5)
    assert updated == item


def test_ai_snapshot_without_matching_plan_is_unchanged():
    store = storage.PlannerStore.__new__(storage.PlannerStore)
    store.data = {"forecast_snapshots": [{"plan_id": "another"}]}
    asyncio.run(store.async_attach_ai_to_forecast_snapshot("missing", {"status": "accepted"}))
    assert store.data == {"forecast_snapshots": [{"plan_id": "another"}]}


def test_recorder_executor_falls_back_when_loaded_component_has_no_executor(monkeypatch):
    monkeypatch.setattr(recorder_import, "import_module", lambda _: SimpleNamespace(get_instance=lambda _: None))
    fallback = object()
    assert recorder_import._recorder_executor(SimpleNamespace(async_add_executor_job=fallback)) is fallback


def test_calendar_missing_daylight_times_and_forecast_time_are_omitted(monkeypatch):
    from custom_components.ha_energy_planner import calendar

    action = _action("minimal", NOW, NOW + timedelta(minutes=5))
    action.desired_state["daylight_lowest_cost"] = {"selected": True}
    owner = _coordinator(_plan([action]))
    monkeypatch.setattr(calendar, "action_load_forecast_attrs", lambda *args: {"expected_kw": 1, "conservative_kw": 2})
    event = calendar._calendar_event(action, owner)
    assert "Expected load: 1.00 kW" in event.description
    assert "Forecast time:" not in event.description
    window = {
        "start": NOW,
        "end": NOW + timedelta(minutes=5),
        "charge_rates": [1],
        "energy_kwh": 1 / 12,
        "allocation_source": "daylight",
    }
    event = calendar._ev_charging_event(action, window, 1, {})
    assert "Daylight preference" in event.description
    assert "Sunrise:" not in event.description
    assert "Sunset:" not in event.description


def test_daylight_window_outside_requested_horizon_is_not_used(monkeypatch):
    monkeypatch.setattr(
        inputs, "get_astral_event_date", lambda hass, event, day: NOW + timedelta(hours=6 if event == "sunrise" else 18)
    )
    assert (
        inputs._daylight_windows(
            SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), NOW, NOW + timedelta(hours=1)
        )
        == []
    )


def test_existing_entry_storage_wins_over_unmarked_legacy_storage():
    from unittest.mock import AsyncMock

    store = storage.PlannerStore.__new__(storage.PlannerStore)
    current = storage._default_data()
    current["ready_by"] = "08:00"
    legacy = {"ready_by": "07:00"}
    store._store = SimpleNamespace(async_load=AsyncMock(return_value=current), async_save=AsyncMock())
    store._legacy_store = SimpleNamespace(async_load=AsyncMock(return_value=legacy), async_save=AsyncMock())
    store._entry_id = "entry"
    asyncio.run(store.async_load())
    assert store.data["ready_by"] == "08:00"
    store._legacy_store.async_save.assert_awaited_once()
    assert store._legacy_store.async_save.call_args.args[0][storage._LEGACY_MIGRATION_MARKER] == "entry"


def test_training_exception_keeps_current_ready_model_usable(monkeypatch):
    from test_recorder_import import CurrentState, ForecastHass

    from custom_components.ha_energy_planner.const import CONF_HOUSEHOLD_LOAD
    from custom_components.ha_energy_planner.load_forecast import FORECAST_CONTRACT_VERSION, MODEL_VERSION

    model = {
        "model_version": MODEL_VERSION,
        "contract_version": FORECAST_CONTRACT_VERSION,
        "quality_ready": True,
        "source_entity_id": "sensor.load",
        "timezone": "UTC",
        "profiles": {},
        "safety_gates_bypassed": False,
    }

    def fail(*args, **kwargs):
        raise recorder_import.LoadForecastRecorderError("unavailable")

    monkeypatch.setattr(recorder_import, "_build_household_load_forecast_model", fail)
    updated, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.load"},
            model,
            now=NOW,
            timezone="UTC",
        )
    )
    assert changed
    assert updated["quality_ready"] is True
    assert "unusable_since" not in updated
    assert updated["last_training_status"] == "failed"
    assert reason.startswith("load_forecast_recorder_unavailable")
