"""Weather loss must not abandon confirmed, otherwise-safe HVAC ownership."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_inputs import FakeHass, FakeState
from test_zone_control import isolated_boundary
from test_zone_control import registry as registry

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.inputs import InputManager
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.weather import async_weather_forecast


def test_five_minute_weather_outage_preserves_owned_cooling_and_isolated_room():
    c = isolated_boundary()
    c.created_at -= timedelta(minutes=10)
    c.slots = [s for s in c.slots if s.valid_at >= c.created_at]
    for entity, room in c.climate_inputs["zones"].items():
        room["temperature"] = 18.75 if "bed" in entity or "sleeping" in entity else 21.88
    c.weather_evidence = {"availability": "live"}
    first = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(
        c, c.created_at, c.created_at + timedelta(minutes=5))[0]
    c.hvac_control.update(first.desired_state)
    observed = c.created_at - timedelta(minutes=5)
    forecast = [{"datetime": (observed + timedelta(hours=i)).isoformat(), "temperature": 28}
                for i in range(25)]
    hass = FakeHass({"weather.home": FakeState("unavailable", last_updated=c.created_at)})
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={
        "identity": ("weather.home", 5, 120, "UTC"),
        "entity_id": "weather.home", "fetched_at": observed, "forecast": forecast})
    payload, details = asyncio.run(async_weather_forecast(
        owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS, now=c.created_at, force=False))
    manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    current, values, _ = manager._optional_weather_temperatures("weather_entity", c.created_at, 24, 5)
    assert current is None
    assert values[0] == 28
    c.weather_evidence = manager.weather_forecast_details
    c.forecast_confidence_by_source = {"weather_entity": 0.0}
    actions = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(
        c, c.created_at, c.created_at + timedelta(minutes=5))
    assert actions[0].kind == "set_hvac"
    assert actions[0].desired_state["mode"] == "cool"
    assert actions[0].desired_state["phase"] == "preconditioning"


# These scenarios use real weather normalization and lifecycle generation with fake
# HA services; the immutable fetch origin is deliberately older than the outage.
def episode(mode="cool"):
    from copy import deepcopy
    c = isolated_boundary(mode)
    c.created_at -= timedelta(minutes=10)
    c.hvac_control["precondition_end"] = c.created_at + timedelta(minutes=5)
    c.slots = [s for s in c.slots if s.valid_at >= c.created_at]
    c.weather_evidence = {"availability": "live"}
    first = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(
        c, c.created_at, c.created_at + timedelta(minutes=5))[0]
    c.hvac_control.update(deepcopy(first.desired_state))
    return c


def outage(c, *, age=300, forecast=None, options=None, state="unavailable", cache=True):
    from copy import deepcopy
    options = options or DEFAULT_OPTIONS
    observed = c.created_at - timedelta(seconds=age)
    forecast = forecast if forecast is not None else [
        {"datetime": (observed + timedelta(hours=i)).isoformat(), "temperature": 28}
        for i in range(25)]
    hass = FakeHass({"weather.home": FakeState(state, last_updated=observed)})
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={
        "identity": ("weather.home", options.get("planning_interval_minutes"),
                     options.get("forecast_freshness_minutes"), "UTC"),
        "entity_id": "weather.home", "fetched_at": observed, "forecast": deepcopy(forecast)} if cache else {})
    payload, details = asyncio.run(async_weather_forecast(
        owner, {"weather_entity": "weather.home"}, options, now=c.created_at, force=True))
    manager = InputManager(hass, {"weather_entity": "weather.home"}, options,
                           weather_forecast=payload, weather_forecast_details=details)
    current, values, issue = manager._optional_weather_temperatures("weather_entity", c.created_at, 24, 5)
    c.weather_evidence = manager.weather_forecast_details
    c.forecast_confidence_by_source = {"weather_entity": manager.forecast_confidence_details[-1]["confidence"]}
    for slot, value in zip(c.slots, values, strict=False):
        slot.outdoor_temperature_forecast_c = value
    return owner, current, values, issue


def actions(c, options=None):
    return HVACPlanningPolicy(options or DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(
        c, c.created_at, c.created_at + timedelta(minutes=5))


def test_outage_coast_expiry_restores_schedules_once_without_extra_writes(monkeypatch):
    from copy import deepcopy

    from test_constraints import _plan
    from test_executor import FakeStore, _arm_store
    from test_zone_control import BED, BED_CLIMATE, MAIN, MAIN_CLIMATE, data, device

    from custom_components.ha_energy_planner import hvac_adapter as adapter_module
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .002)
    c = episode()
    start = c.created_at
    hass = device(c)
    hass.states.values[BED] = "off"
    hass.states.values["input_number.low"] = "19"
    hass.states.values["input_number.high"] = "22"
    entry = {**data(), "weather_entity": "weather.home"}
    # The adapter's configuration key is shared with the actual integration.
    from custom_components.ha_energy_planner.const import CONF_CLIMATE_AUTOMATIONS
    schedules = {f"automation.schedule_{i}": "on" for i in range(4)}
    entry[CONF_CLIMATE_AUTOMATIONS] = list(schedules)
    hass.states.values.update({entity: "off" for entity in schedules})
    original = {"planner_takeover_started_at": start - timedelta(minutes=30),
        "hvac_control": {**deepcopy(c.hvac_control),
        "main_state": {"hvac_mode": "cool", "target_temperature": 24},
        "zone_states": {MAIN: "off", BED: "on", MAIN_CLIMATE: {"target_temperature": 23},
                        BED_CLIMATE: {"target_temperature": 23}}}, "climate_automations": schedules}
    store = FakeStore()
    store.data["ownership"] = deepcopy(original)
    options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "climate_control_enabled": True,
               "command_rate_limit_seconds": 0}
    executor = Executor(store, hass=hass, entry_data=entry, options=options)
    _arm_store(store, executor)
    for minutes in (0, 5):
        c.created_at = start + timedelta(minutes=minutes)
        c.hvac_control = deepcopy(store.data["ownership"]["hvac_control"])
        c.current_hvac_mode = hass.states.get("climate.head").state
        c.slots = [s for s in c.slots if s.valid_at >= c.created_at]
        for entity in ("sensor.living", "sensor.bed"):
            hass.states.values[entity].last_updated = c.created_at
        monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: c.created_at)
        owner, _, _, _ = outage(c, age=300 + minutes * 60)
        hass.states.values["weather.home"] = FakeState("unavailable")
        chosen = actions(c)
        assert chosen[0].kind == "set_hvac"
        # Exercise final planner confidence filtering as well as the executor.
        plan = DryRunPlanner(options).create_plan(c)
        assert any(a.kind == "set_hvac" for a in plan.actions)
        assert plan.confidence_breakdown["climate"] == 0
        assert plan.device_plans["climate"]["weather"]["ownership_retained"]
        before = len(hass.services.calls)
        asyncio.run(executor.async_evaluate(_plan(c.created_at, chosen[0]), c))
        outcomes = [(o.result, o.reason) for o in store.data.get("outcomes", [])]
        assert store.data["ownership"]["hvac_control"]["main_state_committed"], outcomes
        assert store.data["outcomes"][-1].result in {"applied", "skipped"}, outcomes
        assert all(hass.states.get(entity).state == "off" for entity in schedules)
        assert hass.states.get(BED).state == "off"
        assert not any(service == "set_hvac_mode" for _, service, _ in hass.services.calls[before:])
        if minutes == 0:
            assert hass.states.get("climate.head").state == "cool"
        else:
            outcomes = [(o.result, o.reason) for o in store.data.get("outcomes", [])]
            assert hass.states.get("climate.head").state == "off", outcomes
            assert store.data["ownership"]["hvac_control"]["phase"] == "pre_peak_coast"
            assert store.data["ownership"]["hvac_control"]["weather_safe_intent"]["phase"] == "pre_peak_coast"
    c.created_at = start + timedelta(minutes=10)
    c.hvac_control = deepcopy(store.data["ownership"]["hvac_control"])
    outage(c, age=900)
    release = actions(c)[0]
    assert release.desired_state["release_reason"] == "hvac_weather_grace_expired"
    asyncio.run(executor.async_evaluate(_plan(c.created_at, release), c))
    assert not store.data["ownership"].get("hvac_control")
    assert all(hass.states.get(entity).state == "on" for entity in schedules)
    issued = list(hass.services.calls)
    asyncio.run(executor.async_release_hvac_control("again"))
    assert [x for x in hass.services.calls if x[0] != "persistent_notification"] == [
        x for x in issued if x[0] != "persistent_notification"]
    assert not owner._weather_forecast_cache.get("persisted")


@pytest.mark.parametrize("mode", ["cool", "heat"])
@pytest.mark.parametrize("failure,expected", [
    ("expiry", "hvac_weather_grace_expired"), ("no_cache", "hvac_weather_unavailable"),
    ("uncommitted", "hvac_weather_ownership_unconfirmed"), ("pending", "hvac_weather_ownership_unconfirmed"),
    ("cycle_pending", "hvac_weather_ownership_unconfirmed"), ("no_intent", "hvac_weather_ownership_unconfirmed"),
    ("head_off", "hvac_weather_cycle_changed"), ("head_reversed", "hvac_weather_cycle_changed"),
    ("sensor", "hvac_required_evidence_lost"), ("stale_room", "hvac_comfort_handoff"),
    ("comfort", "hvac_comfort_handoff"), ("tariff", "hvac_tariff_period_changed"),
    ("manual", "manual_hvac_override"), ("load", "hvac_confidence_below_threshold"),
    ("configured_threshold", "hvac_confidence_below_threshold"),
])
def test_weather_grace_never_masks_real_authority_loss(mode, failure, expected):
    from test_zone_control import MAIN, MAIN_CLIMATE

    from custom_components.ha_energy_planner.models import Override

    c = episode(mode)
    outage(c, age=900 if failure == "expiry" else 300, cache=failure != "no_cache")
    options = dict(DEFAULT_OPTIONS)
    if failure == "uncommitted":
        c.hvac_control.pop("main_state_committed")
    elif failure == "pending":
        c.hvac_control["command_confirmation"] = {"entity_id": "climate.head"}
    elif failure == "cycle_pending":
        c.hvac_cycle["pending"] = {"mode": mode}
    elif failure == "no_intent":
        c.hvac_control.pop("weather_safe_intent")
    elif failure in {"head_off", "head_reversed"}:
        c.current_hvac_mode = "off" if failure == "head_off" else "heat" if mode == "cool" else "cool"
    elif failure == "sensor":
        c.climate_inputs["zones"][MAIN]["temperature"] = None
    elif failure == "stale_room":
        c.climate_inputs["zones"][MAIN]["temperature_source"]["fresh"] = False
    elif failure == "comfort":
        c.hvac_control["phase"] = "peak_coast"
        c.current_hvac_mode = "off"
        for entity in (MAIN, MAIN_CLIMATE):
            c.climate_inputs["zones"][entity]["temperature"] = 22 if mode == "cool" else 19
    elif failure == "tariff":
        c.slots[0].import_price = None
    elif failure == "manual":
        c.active_overrides = [Override(kind="manual_hvac", source="helper", expires_at=None, reason="user")]
    elif failure == "load":
        c.forecast_confidence_by_source["household_load_entity"] = 0
    elif failure == "configured_threshold":
        options["minimum_climate_confidence"] = 100
        c.hvac_control["weather_safe_intent"]["climate_confidence"] = .9
    result = actions(c, options)
    assert result[0].kind == "release_hvac"
    assert result[0].desired_state["release_reason"] == expected
    assert not c.weather_evidence["ownership_retained"]


@pytest.mark.parametrize("kind", ["old", "future", "gap", "too_short", "invalid"])
def test_cached_forecast_must_really_cover_remaining_lifecycle(kind):
    c = episode()
    now = c.created_at
    offsets = {"old": [-48, -47], "future": [1, 2], "gap": [-1, 0, 2, 3], "too_short": [0, 1]}
    forecast = ([{"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 28}
                 for i in offsets.get(kind, [])] if kind != "invalid" else
                [{"datetime": "bad", "temperature": "bad"}])
    outage(c, forecast=forecast)
    result = actions(c)
    assert result[0].kind == "release_hvac"
    assert result[0].desired_state["release_reason"] == "hvac_weather_coverage_lost"


@pytest.mark.parametrize("prior", [False, True])
def test_outage_at_startup_never_acquires_even_with_disabled_confidence_thresholds(prior):
    c = episode()
    c.hvac_control = {}
    outage(c, cache=prior)
    options = {**DEFAULT_OPTIONS, "minimum_climate_confidence": 0,
               "hvac_preconditioning_mode": "cooling_only"}
    assert actions(c, options) == []


def test_recovery_and_flapping_preserve_original_good_time_without_renewing_grace():
    from unittest.mock import AsyncMock
    c = episode()
    owner, _, _, _ = outage(c)
    original = owner._weather_forecast_cache["fetched_at"]
    forecast = owner._weather_forecast_cache["forecast"]
    owner.hass.services = SimpleNamespace(async_call=AsyncMock(return_value={"weather.home": {"forecast": forecast}}))
    for minute, state in [(1, "sunny"), (2, "unavailable"), (4, "sunny"), (5, "unavailable"), (10, "unavailable")]:
        now = c.created_at + timedelta(minutes=minute)
        owner.hass.states.values["weather.home"].state = state
        payload, details = asyncio.run(async_weather_forecast(
            owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS, now=now, force=True))
        assert owner._weather_forecast_cache["fetched_at"] == original
        assert details["last_good_at"] == original.isoformat()
        assert details["availability"] == ("live" if state == "sunny" else "cached" if minute < 10 else "unavailable")
        assert bool(payload) == (minute < 10)
    # A genuinely updated forecast is reassessed as live, with a new evidence origin.
    owner.hass.states.values["weather.home"].state = "sunny"
    forecast[0]["temperature"] = 29
    _payload, details = asyncio.run(async_weather_forecast(
        owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
        now=c.created_at + timedelta(minutes=11), force=True))
    assert details["availability"] == "live"
    assert details["cache_age_seconds"] == 0


@pytest.mark.parametrize("change", ["entity", "interval", "freshness", "timezone", "restart", "reload"])
def test_incompatible_cache_or_new_coordinator_cannot_revive_forecasts(change):
    c = episode()
    owner, _, _, _ = outage(c)
    entry, options = {"weather_entity": "weather.home"}, dict(DEFAULT_OPTIONS)
    if change == "entity":
        entry["weather_entity"] = "weather.other"
    elif change == "interval":
        options["planning_interval_minutes"] = 10
    elif change == "freshness":
        options["forecast_freshness_minutes"] = 60
    elif change == "timezone":
        owner.hass.config.time_zone = "Australia/Melbourne"
    else:
        owner = SimpleNamespace(hass=owner.hass, _weather_forecast_cache={})
    payload, details = asyncio.run(async_weather_forecast(owner, entry, options, now=c.created_at, force=True))
    assert not payload
    assert details["availability"] == "unavailable"
    assert "last_good_at" not in details


@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize("mutation,reason", [
    ("mode", "hvac_weather_intent_changed"), ("identity", "hvac_weather_intent_changed"),
    ("zones", "hvac_weather_intent_changed"), ("phase", "hvac_weather_phase_changed"),
    ("backwards", "hvac_weather_phase_changed"), ("longer", "hvac_weather_window_extended"),
    ("missing_end", "hvac_weather_window_extended"), ("past_end", "hvac_weather_window_ended"),
    ("reversal", "hvac_weather_cycle_changed"), ("restart", "hvac_weather_cycle_changed"),
    ("after_stop", "hvac_weather_cycle_changed"), ("previous_off", "hvac_weather_cycle_changed"),
    ("demand", "hvac_weather_demand_increased"), ("invalid_target", "hvac_weather_demand_increased"),
    ("coast_target", "hvac_weather_demand_increased"), ("added_zone", "hvac_weather_zones_changed"),
    ("reopen", "hvac_weather_zones_changed"), ("zone_demand", "hvac_weather_demand_increased"),
    ("unknown_zone_target", "hvac_weather_demand_increased"),
])
def test_only_last_confirmed_safe_intent_can_continue(mode, mutation, reason):
    from copy import deepcopy

    from test_zone_control import BED

    from custom_components.ha_energy_planner.weather_control import intent_rejection

    c = episode(mode)
    safe = deepcopy(c.hvac_control["weather_safe_intent"])
    desired = {**deepcopy(safe)}
    now = c.created_at
    current = mode
    if mutation == "mode":
        desired["mode"] = "heat" if mode == "cool" else "cool"
    elif mutation == "identity":
        desired["configuration_identity"] = "different"
    elif mutation == "zones":
        desired["controlled_zones"] = []
    elif mutation == "phase":
        desired["phase"] = "direct_control"
    elif mutation == "backwards":
        safe["phase"] = "peak_coast"
    elif mutation == "longer":
        desired["precondition_end"] += timedelta(minutes=1)
    elif mutation == "missing_end":
        desired.pop("period_end")
    elif mutation == "past_end":
        now = safe["period_end"]
    elif mutation == "reversal":
        desired["hvac_mode"] = "cool" if mode == "heat" else "heat"
    elif mutation == "restart":
        current = "off"
    elif mutation == "after_stop":
        now = safe["precondition_end"]
    elif mutation == "previous_off":
        safe["hvac_mode"] = "off"
    elif mutation == "demand":
        desired["target_temperature"] += 1 if mode == "heat" else -1
    elif mutation == "invalid_target":
        desired["target_temperature"] = None
    elif mutation == "coast_target":
        desired["phase"] = "peak_coast"
        desired["target_temperature"] = 30
    elif mutation == "added_zone":
        desired["zone_intent"]["power"]["switch.extra"] = "off"
    elif mutation == "reopen":
        desired["zone_intent"]["power"][BED] = "on"
    elif mutation == "zone_demand":
        entity = next(iter(desired["zone_intent"]["targets"]))
        desired["zone_intent"]["targets"][entity] += 1 if mode == "heat" else -1
    elif mutation == "unknown_zone_target":
        desired["zone_intent"]["targets"]["climate.other"] = 20
    assert intent_rejection(desired, safe, now, current) == reason


@pytest.mark.parametrize("failure", ["expiry", "source", "intent", "reopened", "sensor", "head", "comfort", "manual"])
def test_retained_weather_rechecks_live_evidence_before_any_device_write(monkeypatch, failure):
    from test_hvac_adapter import _action
    from test_zone_control import BED, data, device

    from custom_components.ha_energy_planner import hvac_adapter as adapter_module
    from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter

    c = episode()
    outage(c)
    chosen = actions(c)[0]
    hass = device(c)
    hass.states.values[BED] = "off"
    hass.states.values["input_number.low"] = "19"
    hass.states.values["input_number.high"] = "22"
    entry = {**data(), "weather_entity": "weather.home"}
    now = c.created_at
    if failure == "expiry":
        now += timedelta(minutes=10)
    elif failure == "source":
        entry["weather_entity"] = "weather.changed"
    elif failure == "intent":
        chosen.desired_state["target_temperature"] -= 1
    elif failure == "reopened":
        # Coast isolation is protected at execution even if the saved flag says closed.
        c.hvac_control["phase"] = "pre_peak_coast"
        c.current_hvac_mode = "off"
        hass.states.values["climate.head"].state = "off"
        c.created_at += timedelta(minutes=5)
        outage(c, age=600)
        chosen = actions(c)[0]
        assert chosen.kind == "set_hvac"
        hass.states.values[BED] = "on"
        now = c.created_at
    elif failure == "sensor":
        hass.states.values["sensor.bed"].state = "unavailable"
    elif failure == "head":
        hass.states.values.pop("climate.head")
    elif failure == "manual":
        pass
    else:
        hass.states.values["sensor.living"].state = "18"
        # Remove isolation proof: a real excursion may never be hidden by weather.
        hass.zone_registry_entries = {}
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: now)
    adapter = DaikinHVACAdapter(hass, entry)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, c.hvac_control)
    if failure == "manual":
        adapter.set_manual_override_check(lambda: True)
    result = asyncio.run(adapter.async_execute(_action(chosen.desired_state)))
    assert not result.applied
    assert not hass.services.calls


def test_prefetched_weather_cannot_age_out_while_waiting_for_planner_lock():
    c = episode()
    owner, _, _, _ = outage(c)
    payload, details = asyncio.run(async_weather_forecast(
        owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS, now=c.created_at, force=True))
    manager = InputManager(owner.hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    _, values, _ = manager._optional_weather_temperatures(
        "weather_entity", c.created_at + timedelta(minutes=11), 24, 5)
    assert all(v is None for v in values)
    assert manager.weather_forecast_details["availability"] == "unavailable"
    assert manager.weather_forecast_details["grace_remaining_seconds"] == 0


def test_overlapping_weather_fetches_are_serialized_and_keep_newer_evidence():
    c = episode()
    async def run():
        state = FakeState("sunny", {"temperature_unit": "°C"}, last_updated=c.created_at)
        hass = FakeHass({"weather.home": state})
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0
        async def fetch(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await finish.wait()
            return {"weather.home": {"forecast": [{"datetime": c.created_at.isoformat(), "temperature": calls}]}}
        hass.services = SimpleNamespace(async_call=fetch)
        owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
        first = asyncio.create_task(async_weather_forecast(owner, {"weather_entity": "weather.home"},
                                   DEFAULT_OPTIONS, now=c.created_at, force=True))
        await started.wait()
        second = asyncio.create_task(async_weather_forecast(owner, {"weather_entity": "weather.home"},
                                    DEFAULT_OPTIONS, now=c.created_at + timedelta(seconds=1), force=True))
        await asyncio.sleep(0)
        assert calls == 1
        finish.set()
        a, b = await asyncio.gather(first, second)
        assert a[0]["forecast"][0]["temperature"] == 1
        assert b[0]["forecast"][0]["temperature"] == 2
        assert owner._weather_forecast_cache["fetched_at"] == c.created_at + timedelta(seconds=1)
    asyncio.run(run())


def test_economic_owned_lifecycle_uses_same_bounded_weather_policy():
    from copy import deepcopy

    from test_climate_engine import context, simulation_model

    from custom_components.ha_energy_planner.climate_runtime import economic_actions

    c = context()
    c.slots[0].import_price = 0
    for slot in c.slots[1:]:
        slot.import_price = 5
    c.climate_engine = {"status": "active", "ever_active": True, "model": simulation_model(),
                        "modes": {"heat": {"ready": True}}}
    c.weather_evidence = {"availability": "live"}
    options = {**DEFAULT_OPTIONS, "hvac_minimum_saving": .01}
    first = economic_actions(c, options, [])[0]
    c.hvac_control = deepcopy(first.desired_state)
    c.hvac_control["main_state_committed"] = True
    stop = first.desired_state["precondition_end"]
    steps = int((stop - c.created_at).total_seconds() / 300)
    c.current_hvac_temperature_c = c.climate_decision["temperatures"][steps - 1]
    c.created_at = stop
    c.slots = c.slots[steps:]
    outage(c, forecast=[{"datetime": (c.created_at + timedelta(hours=i)).isoformat(), "temperature": 10}
                        for i in range(25)])
    continued = economic_actions(c, options, [])
    assert continued[0].kind == "set_hvac", continued[0].desired_state
    assert continued[0].desired_state["lifecycle_id"] == first.desired_state["lifecycle_id"]
    assert continued[0].desired_state["precondition_end"] <= first.desired_state["precondition_end"]
    assert continued[0].desired_state["weather_continuation"]


def test_valid_weather_grace_retains_economic_readiness_without_masking_other_issues():
    from copy import deepcopy

    from test_climate_engine import context, ready_model

    from custom_components.ha_energy_planner.climate_runtime import update_readiness

    c = context()
    c.hvac_control = {"mode": "heat", "phase": "preconditioning", "main_state_committed": True,
                      "period_end": c.created_at + timedelta(minutes=20),
                      "weather_safe_intent": {"climate_confidence": 1}}
    c.weather_evidence = {"availability": "cached",
                          "grace_expires_at": (c.created_at + timedelta(minutes=10)).isoformat(),
                          "continuous_until": (c.created_at + timedelta(hours=1)).isoformat()}
    model = ready_model()
    state = {"ever_active": True, "modes": {"heat": {"ready": True, "passes": 2,
        "last_good": c.created_at.isoformat(), "validation_date": c.created_at.date().isoformat()}}}
    c.input_issues = ["advisory_weather_entity_cached_outage"]
    result = update_readiness(deepcopy(state), model, c, DEFAULT_OPTIONS)
    assert result["status"] == "active"
    assert result["modes"]["heat"]["ready"]
    for issue in ("climate_zone_temperature_unavailable", "advisory_weather_entity_incomplete_horizon"):
        c.input_issues.append(issue)
        assert update_readiness(deepcopy(state), model, c, DEFAULT_OPTIONS)["status"] == "degraded"
        c.input_issues.pop()
    c.hvac_control["command_confirmation"] = {"entity_id": "climate.head"}
    assert update_readiness(deepcopy(state), model, c, DEFAULT_OPTIONS)["status"] == "degraded"


def test_weather_grace_advances_existing_refresh_boundary_and_does_not_spin_at_expiry(monkeypatch):
    from custom_components.ha_energy_planner import coordinator as module
    from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator

    now = episode().created_at + timedelta(seconds=15)
    monkeypatch.setattr(module.dt_util, "utcnow", lambda: now)
    scheduled, canceled = [], []
    monkeypatch.setattr(module, "async_call_later", lambda hass, delay, cb:
                        scheduled.append((delay, cb)) or (lambda: canceled.append(True)))
    owner = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    owner.hass = SimpleNamespace()
    owner.entry = SimpleNamespace(options={**DEFAULT_OPTIONS}, data={})
    owner._boundary_cancel = None
    owner.weather_forecast_diagnostics = {"ownership_retained": True,
        "grace_expires_at": (now + timedelta(seconds=10)).isoformat()}
    owner._schedule_next_boundary_refresh()
    assert scheduled[-1][0] == 10
    owner.weather_forecast_diagnostics["grace_expires_at"] = now.isoformat()
    owner._schedule_next_boundary_refresh()
    assert canceled
    assert scheduled[-1][0] == 285


@pytest.mark.parametrize("issue_age", [None, 120, 9000, "naive", "future", "invalid"])
def test_real_fetch_provenance_units_issue_age_and_invalid_responses(issue_age):
    from copy import deepcopy
    from unittest.mock import AsyncMock

    c = episode()
    now = c.created_at
    attributes = {"temperature_unit": "°F"}
    if issue_age is not None:
        attributes["issued_at"] = ("invalid" if issue_age == "invalid" else
                                   now.replace(tzinfo=None).isoformat() if issue_age == "naive" else
                                   (now + timedelta(minutes=1)).isoformat() if issue_age == "future" else
                                   (now - timedelta(seconds=issue_age)).isoformat())
    state = FakeState("sunny", attributes, last_updated=now - timedelta(minutes=1))
    hass = FakeHass({"weather.home": state})
    rows = [{"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 77} for i in range(25)]
    hass.services = SimpleNamespace(async_call=AsyncMock(return_value={"weather.home": {"forecast": rows}}))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                                                         now=now, force=True))
    if issue_age in {9000, "naive", "future", "invalid"}:
        assert not payload
        assert details["fetch_status"] == "failed"
        assert not owner._weather_forecast_cache
        return
    original = deepcopy(owner._weather_forecast_cache)
    live_manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                                weather_forecast=payload, weather_forecast_details=details)
    assert live_manager._optional_weather_temperatures("weather_entity", now, 24, 5)[1][0] == 25
    assert live_manager.weather_forecast_details["availability"] == "live"
    assert details["original_fetched_at"] == now.isoformat()
    assert details["observation_at"] == state.last_updated.isoformat()
    expected_origin = now - timedelta(seconds=issue_age or 0)
    assert details["last_good_at"] == expected_origin.isoformat()
    assert details["provider_coverage_start"] == rows[0]["datetime"]
    assert details["provider_coverage_end"] == rows[-1]["datetime"]
    state.state = "unavailable"
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                                                         now=now + timedelta(minutes=5), force=False))
    manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    current, values, _ = manager._optional_weather_temperatures("weather_entity", now + timedelta(minutes=5), 24, 5)
    assert current is None
    assert values[0] == 25  # Fahrenheit metadata survives the unavailable entity.
    assert manager.forecast_confidence_details[-1]["confidence"] == 0
    state.state = "sunny"
    hass.services.async_call.return_value = {"weather.home": {"forecast": [
        {"datetime": now.isoformat(), "temperature": "broken"}]}}
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                                                         now=now + timedelta(minutes=6), force=True))
    assert details["fetch_status"] == "cached_after_error"
    assert payload["forecast"] == original["forecast"]
    assert {key: owner._weather_forecast_cache[key] for key in original} == original
    assert owner._weather_forecast_cache["outage_started_at"] == (now + timedelta(minutes=5)).isoformat()


@pytest.mark.parametrize("failure", ["authority", "demand", "non_weather_confidence"])
def test_final_planner_filter_rechecks_weather_continuation(failure):
    from custom_components.ha_energy_planner.planner_confidence import _action_meets_confidence_threshold

    c = episode()
    outage(c)
    chosen = actions(c)[0]
    assert _action_meets_confidence_threshold(chosen, c, DEFAULT_OPTIONS)
    if failure == "authority":
        c.hvac_control["command_confirmation"] = {"entity_id": "climate.head"}
    elif failure == "demand":
        chosen.desired_state["target_temperature"] -= 1
    else:
        c.forecast_confidence_by_source["amber_import_price_entity"] = 0
    assert not _action_meets_confidence_threshold(chosen, c, DEFAULT_OPTIONS)


def test_execution_constraints_reject_weather_lease_after_original_expiry():
    from test_constraints import _plan

    from custom_components.ha_energy_planner.constraints import ConstraintValidator

    c = episode()
    outage(c)
    chosen = actions(c)[0]
    reasons = ConstraintValidator(DEFAULT_OPTIONS).validate_action(
        c, _plan(c.created_at, chosen), chosen, now=c.created_at + timedelta(minutes=10))
    assert "hvac_weather_grace_expired" in reasons


def test_confirmed_fresh_action_commits_weather_intent_with_original_baseline():
    from copy import deepcopy

    from custom_components.ha_energy_planner.hvac_adapter import HVACCommandResult
    from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction

    c = episode()
    desired = actions(c)[0].desired_state
    previous = {"hvac_control": {"main_state": {"hvac_mode": "cool", "target_temperature": 24}}}
    transaction = HVACOwnershipTransaction(previous, c.created_at)
    provisional = transaction.prepare(desired, {}, {}, previous["hvac_control"]["main_state"])
    assert "weather_safe_intent" not in provisional["hvac_control"]
    applied = HVACCommandResult(True, "confirmed", {}, {}, {})
    complete = transaction.complete(applied, provisional, desired,
                                    main_state_superseded=False, superseded_zone_entity_ids=set())
    assert complete["hvac_control"]["weather_safe_intent"] == desired["weather_safe_intent"]
    assert complete["hvac_control"]["main_state_committed"]
    assert complete["hvac_control"]["main_state"] == previous["hvac_control"]["main_state"]
    unchanged = deepcopy(complete)
    desired["weather_safe_intent"]["target_temperature"] -= 1
    assert complete == unchanged


def test_weather_bounds_reject_added_demand_and_preserve_earlier_normal_release():
    from copy import deepcopy

    from custom_components.ha_energy_planner.climate_runtime import release_action
    from custom_components.ha_energy_planner.weather_control import bound_weather_actions

    c = episode()
    outage(c)
    desired = deepcopy(actions(c)[0])
    desired.desired_state["target_temperature"] -= 1
    result = bound_weather_actions(c, DEFAULT_OPTIONS, [desired])
    assert result[0].desired_state["release_reason"] == "hvac_weather_demand_increased"
    assert not c.climate_decision["commands_selected"]
    normal = release_action(c, "hvac_expensive_period_ended", timedelta(minutes=5),
                            at=c.created_at + timedelta(minutes=5))
    result = bound_weather_actions(c, DEFAULT_OPTIONS, [normal])
    assert result[0] is normal
    assert result[-1].desired_state["release_reason"] == "hvac_weather_grace_expired"


def test_cache_without_original_time_has_no_grace():
    c = episode()
    owner, _, _, _ = outage(c)
    owner._weather_forecast_cache.pop("fetched_at")
    payload, details = asyncio.run(async_weather_forecast(
        owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS, now=c.created_at, force=True))
    assert not payload
    assert details["availability"] == "unavailable"
    assert "last_good_at" not in details


def test_retained_plan_publishes_diagnostics_and_schedules_expiry_through_real_pipeline(monkeypatch):
    from unittest.mock import Mock

    from test_coordinator import _plan
    from test_refresh_timing import coordinator_for_timing

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        owner = coordinator_for_timing()
        del owner._async_update_data_locked
        owner._schedule_plan_execution = Mock()
        owner._schedule_next_boundary_refresh = Mock()
        evidence = {"availability": "cached", "ownership_retained": True,
                    "retention_reason": "bounded_confirmed_lifecycle"}
        class Planner:
            def __init__(self, *args, **kwargs):
                pass
            def create_plan(self, context):
                context.weather_evidence = dict(evidence)
                return _plan("retained-weather")
        monkeypatch.setattr(module, "DryRunPlanner", Planner)
        await owner._async_update_data()
        assert owner.weather_forecast_diagnostics == evidence
        owner._schedule_next_boundary_refresh.assert_called_once()
    asyncio.run(run())


def test_hourly_service_forecast_never_bridges_sparse_rows_using_entity_cadence():
    c = episode()
    state = FakeState("sunny", {"forecast_interval_minutes": 180})
    manager = InputManager(FakeHass({"weather.home": state}), {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
        weather_forecast={"forecast": [{"datetime": (c.created_at + timedelta(hours=i)).isoformat(), "temperature": 28}
                                        for i in (0, 2)]})
    _, values, _ = manager._optional_weather_temperatures("weather_entity", c.created_at, 24, 5)
    assert values[0] == 28
    assert values[18] is None
    assert manager.weather_forecast_details["continuous_until"] == (c.created_at + timedelta(hours=1)).isoformat()


def test_healthy_identical_polls_leave_grace_for_a_new_brief_outage():
    from unittest.mock import AsyncMock

    c = episode()
    start = c.created_at - timedelta(minutes=20)
    rows = [{"datetime": (start + timedelta(hours=i)).isoformat(), "temperature": 28} for i in range(25)]
    state = FakeState("sunny", {"temperature_unit": "°C"}, last_updated=start)
    hass = FakeHass({"weather.home": state})
    hass.services = SimpleNamespace(async_call=AsyncMock(return_value={"weather.home": {"forecast": rows}}))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    for minutes in (0, 5, 10, 15, 20):
        payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"},
            DEFAULT_OPTIONS, now=start + timedelta(minutes=minutes), force=False))
        assert payload and details["availability"] == "live"
        assert details["last_good_at"] == start.isoformat()
    state.state = "unavailable"
    now = start + timedelta(minutes=25)
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"},
        DEFAULT_OPTIONS, now=now, force=False))
    assert payload["forecast"] == rows
    assert details["availability"] == "cached"
    assert details["cache_age_seconds"] >= 1500
    assert details["last_successful_fetch_at"] == (start + timedelta(minutes=20)).isoformat()
    assert details["grace_expires_at"] == (start + timedelta(minutes=35)).isoformat()
    c.created_at = now
    c.slots = [s for s in c.slots if s.valid_at >= now]
    manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    manager._optional_weather_temperatures("weather_entity", now, 24, 5)
    c.weather_evidence = manager.weather_forecast_details
    c.forecast_confidence_by_source = {"weather_entity": 0}
    assert actions(c)[0].kind == "set_hvac"


def test_service_failure_preserves_live_fresh_legacy_forecasts():
    from unittest.mock import AsyncMock

    c = episode()
    now = c.created_at
    rows = [{"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 28} for i in range(25)]
    state = FakeState("sunny", {"temperature_unit": "°C", "forecast": rows}, last_updated=now)
    hass = FakeHass({"weather.home": state})
    hass.services = SimpleNamespace(async_call=AsyncMock(side_effect=RuntimeError("synthetic outage")))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"},
        DEFAULT_OPTIONS, now=now, force=True))
    manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    _, values, issue = manager._optional_weather_temperatures("weather_entity", now, 24, 5)
    assert values == [28] * 288
    assert issue is None
    assert manager.forecast_confidence_details[-1]["confidence"] == 1
    assert manager.weather_forecast_details["availability"] == "live"
    assert manager.weather_forecast_details["source_type"] == "legacy_entity_attributes"
    assert manager.weather_forecast_details["failure_reason"] == "RuntimeError"


def weather_provenance_cases():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "fixtures/live_schema/weather_outage_provenance.json").read_text())


@pytest.mark.parametrize("case", weather_provenance_cases()["weather_fetch_cases"], ids=lambda case: case["name"])
def test_provider_provenance_fixtures_exercise_actual_weather_fetch(case):
    from datetime import datetime
    from unittest.mock import AsyncMock

    fixture = weather_provenance_cases()
    now = datetime.fromisoformat(fixture["issued_at"])
    attributes = {"temperature_unit": fixture["attributes"]["temperature_unit"], case["attribute"]: case["value"]}
    state = FakeState(fixture["state"], attributes, last_updated=now)
    hass = FakeHass({fixture["source_entity_id"]: state})
    hass.services = SimpleNamespace(async_call=AsyncMock(return_value=fixture["response"]))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": fixture["source_entity_id"]},
        DEFAULT_OPTIONS, now=now, force=True))
    assert details["fetch_status"] == case["expected_fetch_status"]
    if case["expected_fetch_status"] == "fetched":
        assert details["provider_issued_at"] == case["value"]
        manager = InputManager(hass, {"weather_entity": fixture["source_entity_id"]}, DEFAULT_OPTIONS,
                               weather_forecast=payload, weather_forecast_details=details)
        _, values, _ = manager._optional_weather_temperatures("weather_entity", now, 1, 15)
        assert values == fixture["expected"]
        # The same provider metadata is authoritative for the established live
        # attribute path when no successful service response is available.
        state.attributes["forecast"] = fixture["response"][fixture["source_entity_id"]]["forecast"]
        hass.services.async_call.side_effect = RuntimeError("synthetic outage")
        owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
        payload, details = asyncio.run(async_weather_forecast(owner,
            {"weather_entity": fixture["source_entity_id"]}, DEFAULT_OPTIONS, now=now, force=True))
        manager = InputManager(hass, {"weather_entity": fixture["source_entity_id"]}, DEFAULT_OPTIONS,
                               weather_forecast=payload, weather_forecast_details=details)
        assert manager._optional_weather_temperatures("weather_entity", now, 1, 15)[1] == fixture["expected"]
        assert manager.weather_forecast_details["provider_issued_at"] == case["value"]
        assert manager.weather_forecast_details["availability"] == "live"
    else:
        assert not payload
        assert details["availability"] == "unavailable"
        assert details["failure_reason"] == "ValueError:weather_forecast_response_invalid"


def test_live_confirmation_clock_recovers_only_after_sustained_success():
    from unittest.mock import AsyncMock

    c = episode()
    start = c.created_at - timedelta(minutes=20)
    rows = [{"datetime": (start + timedelta(hours=i)).isoformat(), "temperature": 28} for i in range(25)]
    state = FakeState("sunny", last_updated=start)
    hass = FakeHass({"weather.home": state})
    hass.services = SimpleNamespace(async_call=AsyncMock(return_value={"weather.home": {"forecast": rows}}))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    def fetch(minute, status):
        state.state = status
        return asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
            now=start + timedelta(minutes=minute), force=False))
    for minute in (0, 5, 10, 15, 20):
        fetch(minute, "sunny")
    _, first = fetch(21, "unavailable")
    expiry = first["grace_expires_at"]
    for minute, status in ((22, "sunny"), (23, "unavailable"), (24, "sunny"), (25, "unavailable")):
        _, details = fetch(minute, status)
        assert details["grace_expires_at"] == expiry
    assert fetch(35, "unavailable")[0] == {}
    # Single recovery fetches and cache hits cannot reset either evidence age or the outage lease.
    _, recovering = fetch(36, "sunny")
    assert recovering["grace_expires_at"] == expiry
    _, stable = fetch(41, "sunny")
    assert stable["outage_started_at"] is None
    assert stable["last_good_at"] == start.isoformat()
    _, outage_again = fetch(42, "unavailable")
    assert outage_again["grace_expires_at"] == (start + timedelta(minutes=56)).isoformat()
    assert outage_again["cache_age_seconds"] >= 42 * 60


@pytest.mark.parametrize("failure", ["stale", "future", "unavailable", "metadata", "empty", "past", "partial"])
def test_legacy_service_fallback_preserves_actual_freshness_and_coverage(failure):
    from unittest.mock import AsyncMock

    now = episode().created_at
    rows = [{"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 28} for i in range(25)]
    state = FakeState("sunny", {"forecast": rows}, last_updated=now)
    if failure == "stale":
        state.last_updated -= timedelta(minutes=121)
    elif failure == "future":
        state.last_updated += timedelta(minutes=1)
    elif failure == "unavailable":
        state.state = "unavailable"
    elif failure == "metadata":
        state.attributes["issued_at"] = "invalid"
    elif failure == "empty":
        state.attributes["forecast"] = []
    elif failure == "past":
        state.attributes["forecast"] = [{"datetime": (now - timedelta(days=1)).isoformat(), "temperature": 28}]
    else:
        state.attributes["forecast"] = rows[:1]
    hass = FakeHass({"weather.home": state})
    hass.services = SimpleNamespace(async_call=AsyncMock(side_effect=RuntimeError("synthetic outage")))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"},
        DEFAULT_OPTIONS, now=now, force=True))
    manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    _, values, _ = manager._optional_weather_temperatures("weather_entity", now, 24, 5)
    if failure == "partial":
        assert values[0] == 28
        assert values[1] is None
        assert 0 < manager.forecast_confidence_details[-1]["confidence"] < .5
        assert manager.weather_forecast_details["source_type"] == "legacy_entity_attributes_partial"
    else:
        assert all(value is None for value in values)
        assert manager.forecast_confidence_details[-1]["confidence"] == 0
        assert manager.weather_forecast_details["availability"] == "unavailable"


def test_legacy_array_forecast_retains_original_issue_time_and_positions():
    from unittest.mock import AsyncMock

    now = episode().created_at
    issued = now - timedelta(minutes=30)
    values = list(range(300))
    state = FakeState("sunny", {"forecast": values, "forecast_interval_minutes": 5}, last_updated=issued)
    hass = FakeHass({"weather.home": state})
    hass.services = SimpleNamespace(async_call=AsyncMock(side_effect=RuntimeError("synthetic outage")))
    owner = SimpleNamespace(hass=hass, _weather_forecast_cache={})
    payload, details = asyncio.run(async_weather_forecast(owner, {"weather_entity": "weather.home"},
        DEFAULT_OPTIONS, now=now, force=True))
    manager = InputManager(hass, {"weather_entity": "weather.home"}, DEFAULT_OPTIONS,
                           weather_forecast=payload, weather_forecast_details=details)
    _, forecast, _ = manager._optional_weather_temperatures("weather_entity", now, 1, 5)
    assert forecast == values[6:18]
    assert manager.weather_forecast_details["last_good_at"] == issued.isoformat()
    assert manager.weather_forecast_details["cache_age_seconds"] == 1800
    assert manager.weather_forecast_details["provider_issued_at"] is None
