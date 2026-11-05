"""Synthetic full-shape Next actions publication without live Home Assistant."""

from __future__ import annotations

import cProfile
import json
import pstats
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_calendar import _action, _plan
from test_discovery import FakeHass, FakeState
from test_entity_publication import NOW
from test_sensor import _coordinator

from custom_components.ha_energy_planner import enphase_export_limit
from custom_components.ha_energy_planner import entity as entity_module
from custom_components.ha_energy_planner import sensor as sensor_module
from custom_components.ha_energy_planner.entity import RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind
from custom_components.ha_energy_planner.sensor import SENSORS, PlannerSensor


def _full_presentation(monkeypatch, *, ai_status="disabled", evidence_characters=80):
    """Three actions, 48-hour EV allocation, forecasts, zones, weather and AI."""
    evidence = {f"source_{index}": {"status": "ready", "detail": "x" * evidence_characters,
                                 "samples": list(range(144))} for index in range(16)}
    ev = replace(_action("ev", NOW, NOW + timedelta(minutes=15)), kind=ActionKind.EV_SCHEDULE,
                 desired_state={"target_soc_percent": 80, "ready_by": "07:00", "allocated_slots": [
                     {"valid_at": (NOW + timedelta(minutes=15*i)).isoformat(), "charge_kw": 3.6,
                      "allocation_source": "ready_by"} for i in range(192)],
                     "optimization": {"search_status": "valid_schedule", "readiness_margin_minutes": 30,
                                      "physical_power_by_time": {str(i): 7.2 for i in range(192)}}})
    climate = replace(ev, action_id="climate", asset=ActionAsset.DAIKIN, kind=ActionKind.SET_HVAC,
                      desired_state={"hvac_mode": "cool", "target_temperature": 23,
                                     "phase": "preconditioning", "target_evidence": evidence})
    export = replace(ev, action_id="export", asset=ActionAsset.ENPHASE_EXPORT_LIMIT,
                     kind=ActionKind.DISABLE_EXPORT_LIMIT, desired_state={"watts": None})
    plan = _plan([ev, climate, export])
    plan.created_at = NOW
    plan.decision_audit = {"summary": "Synthetic plan", "policy_order": ["safety", "comfort", "cost"],
                           "accepted": [{"action_id": a.action_id, "evidence": evidence} for a in plan.actions]}
    plan.preview = [{"valid_at": (NOW+timedelta(minutes=15*i)).isoformat(), "load_kw": 1.5,
                     "import_price": 0.3} for i in range(192)]
    plan.device_plans["enphase_export_limit"] = {"ready": True, "feedback_ready": True,
        "current_state_label": "Disabled", "next_planned_state_label": "Disabled", "blocks": [
            {"start": (NOW+timedelta(minutes=30*i)).isoformat(),
             "end": (NOW+timedelta(minutes=30*(i+1))).isoformat(), "price": 0.08, "watts": None}
            for i in range(48)]}
    states = {"switch.ev": FakeState("on"), "binary_sensor.charging": FakeState("off"),
              "sensor.soc": FakeState("45"), "climate.home": FakeState("cool", {"temperature": 23}),
              "weather.home": FakeState("sunny"), "ai_task.provider": FakeState("ready"),
              "sensor.export_limit": FakeState("disabled", {"confirmed_watts": None,
                  "last_successful_readback": NOW.timestamp(), "pending": False})}
    states.update({f"climate.zone_{i}": FakeState("cool", {"temperature": 23}) for i in range(8)})
    hass = FakeHass(states, {("ai_task", "generate_data"), ("enphase_ev", "set_export_limit"),
                             ("enphase_ev", "disable_export_limit")})
    monkeypatch.setattr(enphase_export_limit.er, "async_get", lambda _: SimpleNamespace(
        async_get=lambda entity_id: SimpleNamespace(platform="enphase_ev", domain="sensor",
            config_entry_id="synthetic", unique_id="synthetic_export_limit", device_id="synthetic")
        if entity_id == "sensor.export_limit" else None))
    owner = _coordinator(plan, hass=hass, options={"ev_control_enabled": True, "climate_control_enabled": True,
                         "enphase_export_limit_control_enabled": True}, entry_data={
        "ev_charger_entity": "switch.ev", "ev_charging_entity": "binary_sensor.charging",
        "ev_soc_entity": "sensor.soc", "daikin_climate_entity": "climate.home",
        "climate_zone_entities": [f"climate.zone_{i}" for i in range(8)],
        "enphase_export_limit_entity": "sensor.export_limit", "weather_entity": "weather.home"},
        store_data={"forecast_snapshots": [{"plan_id": plan.plan_id,
            "built_in_load_forecast": {"status": "ready", "source": "built_in_recorder_history",
                "validation": evidence, "cleaning": evidence, "first_expected_kw": 1.5, "first_upper_kw": 2.0},
            "action_load_forecasts": [{"action_id": a.action_id, "valid_at": NOW.isoformat(),
                                      "expected_kw": 1.5, "conservative_kw": 2.0} for a in plan.actions]}]})
    owner.weather_forecast_diagnostics = {"fetch_status": "cached", "point_count": 48,
                                         "cache_age_seconds": 15, "validation": evidence}
    if ai_status != "disabled":
        owner.entry_data["ai_task_entity"] = "ai_task.provider"
        fingerprint = sensor_module._material_plan_fingerprint(plan)
        owner._ai_advice_pending_fingerprint = fingerprint
        owner._ai_advice_pending_reason = "request_in_flight"
        if ai_status == "accepted":
            owner.store.data["ai_recommendations"] = [{"plan_id": plan.plan_id,
                "plan_fingerprint": fingerprint, "status": "accepted",
                "accepted": {"outcome": "no_action_needed", "summary": "Synthetic advice"}}]
    return owner


@pytest.mark.parametrize("ai_status", ["disabled", "pending", "accepted"])
def test_full_next_actions_publication_profile(monkeypatch, ai_status):
    monkeypatch.setattr(sensor_module.dt_util, "utcnow", lambda: NOW)
    owner = _full_presentation(monkeypatch, ai_status=ai_status)
    saved = deepcopy(owner.data), deepcopy(owner.store.data)
    sensor = PlannerSensor(owner, next(d for d in SENSORS if d.key == "next_actions"))
    raw = sensor.entity_description.attrs_fn(owner)
    # Preserve the original conversion/normalization result, including the
    # selected compaction profile, rather than just accepting smaller output.
    expected = entity_module.recorder_safe_attributes(
        entity_module._normalize_attribute_value(entity_module.to_jsonable(raw)),
    )
    profiler = cProfile.Profile()
    with patch.object(entity_module, "to_jsonable", wraps=entity_module.to_jsonable) as conversions, \
         patch.object(sensor_module, "_material_plan_fingerprint",
                      wraps=sensor_module._material_plan_fingerprint) as fingerprints:
        profiler.enable()
        started = perf_counter()
        readings = [(sensor.native_value, sensor.extra_state_attributes) for _ in range(3)]
        elapsed = (perf_counter()-started)*1000
        profiler.disable()
    assert readings[0] == readings[1] == readings[2]
    value, attrs = readings[0]
    assert attrs == expected
    assert value.startswith("Climate:")
    assert attrs["action_count"] == 3
    size = len(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
    assert (owner.data, owner.store.data) == saved
    print({"ai": ai_status, "three_reads_profiled_ms": round(elapsed, 3),
           "fingerprints": fingerprints.call_count, "whole_payload_conversions": conversions.call_count,
           "attribute_bytes": size})
    pstats.Stats(profiler).sort_stats("cumtime").print_stats(10)
    # Native JSON containers need no second conversion walk. Pending AI must
    # hash the current plan once per read, just like accepted advice.
    assert conversions.call_count == 0
    assert fingerprints.call_count == (0 if ai_status == "disabled" else 3)


def test_next_actions_updates_without_cached_plan_presentation(monkeypatch):
    monkeypatch.setattr(sensor_module.dt_util, "utcnow", lambda: NOW)
    owner = _full_presentation(monkeypatch, ai_status="pending")
    sensor = PlannerSensor(owner, next(d for d in SENSORS if d.key == "next_actions"))

    assert "pending_reason" in sensor.extra_state_attributes["ai_explanation"]
    # In-place plan changes must invalidate advice immediately, even without a
    # new plan ID or a coordinator publication.
    owner.data.actions[0].desired_state["target_soc_percent"] = 90
    assert "pending_reason" not in sensor.extra_state_attributes["ai_explanation"]
    owner._ai_advice_pending_fingerprint = sensor_module._material_plan_fingerprint(owner.data)
    assert "pending_reason" in sensor.extra_state_attributes["ai_explanation"]
    owner.weather_forecast_diagnostics["cache_age_seconds"] = 120
    raw = sensor.entity_description.attrs_fn(owner)
    assert raw["weather_forecast"]["cache_age_seconds"] == 120
    owner.hass.states.values["climate.home"].attributes["temperature"] = None
    capability = sensor.entity_description.attrs_fn(owner)["climate_capability"]
    assert "main_climate_target_unavailable" in capability["issues"]
    owner.options["ev_control_enabled"] = False
    assert sensor.extra_state_attributes["action_count"] == 2
    owner.store.data["ownership"] = {"enphase_export_limit": {"pending": {"watts": 0}}}
    assert sensor.entity_description.attrs_fn(owner)["export_limit"]["reason"] == "export_limit_pending"
    owner.entry_data.pop("ai_task_entity")
    assert sensor.extra_state_attributes["ai_explanation"]["configured"] is False


@pytest.mark.parametrize("unsafe_state", ["missing", "health", "status"])
def test_ai_advice_does_not_hash_or_reuse_an_unsafe_plan(monkeypatch, unsafe_state):
    monkeypatch.setattr(sensor_module.dt_util, "utcnow", lambda: NOW)
    owner = _full_presentation(monkeypatch, ai_status="accepted")
    if unsafe_state == "missing":
        owner.data = None
    elif unsafe_state == "health":
        owner.data.health = sensor_module.InputHealth.UNSAFE
    else:
        owner.data.status = "unsafe"
    with patch.object(sensor_module, "_material_plan_fingerprint",
                      side_effect=AssertionError("Unsafe plans must not be matched to advice")):
        attrs = sensor_module._ai_advice_attrs(owner)
    assert attrs["result"] is None
    assert "pending_reason" not in attrs
    assert "summary" not in attrs
