"""Equipment compensation and persistent source/regime learning contracts."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.helpers import entity_registry
from test_calendar import _coordinator as calendar_coordinator
from test_calendar import _plan as calendar_plan
from test_constraints import _plan
from test_hvac_adapter import FakeState, _action
from test_storage import FakeStore
from test_zone_control import (
    BED,
    BED_CLIMATE,
    MAIN,
    MAIN_CLIMATE,
    PAIRS,
    data,
    device,
    ownership,
    room_context,
)
from test_zone_temperature import NOW

from custom_components.ha_energy_planner import calendar as calendar_module
from custom_components.ha_energy_planner import hvac_adapter as adapter_module
from custom_components.ha_energy_planner import storage as storage_module
from custom_components.ha_energy_planner.climate_inputs import read_climate_inputs, validate_climate_config
from custom_components.ha_energy_planner.climate_runtime import economic_actions
from custom_components.ha_energy_planner.config_flow import OptionsFlow
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
from custom_components.ha_energy_planner.plan_presentation import plain_action
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.preconditioning import _preparation
from custom_components.ha_energy_planner.storage import PlannerStore
from custom_components.ha_energy_planner.thermal_learning import rate, sample, summary, update
from custom_components.ha_energy_planner.zone_control import desired_fields, evaluate, live_rejection
from custom_components.ha_energy_planner.zone_temperature import target_reached


@pytest.fixture(autouse=True)
def local_device_registry(monkeypatch):
    monkeypatch.setattr(entity_registry, "async_get", lambda hass: SimpleNamespace(
        async_get=lambda entity: hass.zone_registry_entries.get(entity)))
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .002)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", .001)


def compensated(main=23, bed=23):
    c = room_context(main=main, bed=bed)
    c.climate_inputs["zones"][MAIN]["cooling_equipment_offset_c"] = -2
    config = data()
    config["hvac_zone_mappings"][MAIN]["cooling_equipment_offset_c"] = -2
    return c, config


def compensated_device(c):
    hass = device(c)
    for entity in ["climate.head", *PAIRS.values()]:
        state = hass.states.get(entity)
        state.attributes.update(min_temp=16, max_temp=32, target_temp_step=.5, hvac_modes=["off", "cool", "heat"])
        hass.states.values[entity] = state
    original = hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        entity = payload["entity_id"]
        before = hass.states.get(entity)
        attributes = dict(before.attributes) if before is not None else {}
        await original(domain, service, payload, **kwargs)
        if domain == "climate" and service in {"turn_on", "turn_off"}:
            # Daikin retains capabilities and recalls an active mode on wake.
            hass.states.values[entity] = FakeState(c.current_hvac_mode if service == "turn_on" else "off", attributes)
        if payload["entity_id"] == "climate.head" and service == "set_temperature":
            target = payload["temperature"]
            for entity in PAIRS.values():
                state = hass.states.get(entity)
                state.attributes.update(min_temp=target - 2, max_temp=target + 2)
                hass.states.values[entity] = state

    hass.services.async_call = call
    return hass


def desired(c):
    return {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21,
            "target_temperature": 21, "enable_zones": True,
            **desired_fields(c, "cool", 21, "preconditioning")}


@pytest.mark.parametrize("policy", ["legacy", "automatic", "observe"])
@pytest.mark.parametrize("initial_mode", ["cool", "off"])
def test_equipment_and_room_targets_stay_separate_through_both_policies(policy, initial_mode):
    c, config = compensated()
    c.hvac_control = ownership(c)
    options = {**DEFAULT_OPTIONS, "hvac_policy": policy}
    fallback = HVACPlanningPolicy(options, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    actions = economic_actions(c, options, fallback)
    command = next(action for action in actions if action.kind == "set_hvac")
    assert command.desired_state["target_temperature"] == 19
    assert command.desired_state["precondition_target"] == 21
    assert command.desired_state["zone_targets"] == {MAIN_CLIMATE: 19, BED_CLIMATE: 21}
    intent = command.desired_state["zone_intent"]
    assert intent["zones"][MAIN]["target"] == intent["zones"][BED]["target"] == 21
    assert not target_reached(c, "cool", 21)
    hass = compensated_device(c)
    hass.states.values["climate.head"].state = initial_mode
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(options, c.hvac_control)
    baseline = adapter.main_takeover_snapshot()
    automations, zones = adapter.takeover_snapshot()
    result = asyncio.run(adapter.async_execute(command))
    if initial_mode == "off":
        assert not result.applied and result.reason == "climate_zone_baseline_mode_mismatch"
        assert not hass.services.calls
        return
    assert result.applied
    assert hass.states.get("climate.head").attributes["temperature"] == 19
    assert hass.states.get(MAIN_CLIMATE).attributes["temperature"] == 19
    assert hass.states.get(BED_CLIMATE).attributes["temperature"] == 21
    writes = [(service, payload["entity_id"]) for _, service, payload in hass.services.calls]
    assert writes.index(("set_temperature", "climate.head")) < writes.index(("set_temperature", MAIN_CLIMATE))
    # Recovery in a fresh adapter still uses the original 24/23/23 baseline.
    restored = asyncio.run(DaikinHVACAdapter(hass, config).async_restore(automations, zones, baseline))
    assert restored.applied
    assert hass.states.get("climate.head").attributes["temperature"] == 24
    assert hass.states.get("climate.head").state == initial_mode
    assert all(hass.states.get(entity).attributes["temperature"] == 23 for entity in PAIRS.values())
    assert c.occupied_temperature_low_c == 21 and c.occupied_temperature_high_c == 24


def test_compensated_aliases_pause_room_completion_and_restart_from_serialized_intent():
    c, _ = compensated(bed=20.9)
    c.climate_inputs["zones"][MAIN_CLIMATE] = deepcopy(c.climate_inputs["zones"][MAIN])
    first = desired(c)
    assert first["zone_power"] == {MAIN: "on", BED: "off"}
    assert first["target_temperature"] == 19
    c.hvac_control = {"zone_intent": json.loads(json.dumps(first["zone_intent"]))}
    c.created_at += timedelta(minutes=10)
    c.climate_inputs["zones"][BED]["temperature"] = 21.4
    assert desired(c)["zone_power"][BED] == "on"
    c.climate_inputs["zones"][MAIN]["temperature"] = 20.9
    c.climate_inputs["zones"][MAIN_CLIMATE]["temperature"] = 20.9
    resumed = desired(c)
    assert resumed["zone_power"] == {MAIN: "off", BED: "on"}
    assert resumed["target_temperature"] == 21
    c.climate_inputs["zones"][BED]["temperature"] = 20.9
    assert desired(c)["zone_stop"] and target_reached(c, "cool", 21)
    assert evaluate(c, "heat", 24)["targets"] == {MAIN_CLIMATE: 24, BED_CLIMATE: 24}
    c.climate_inputs["zones"][MAIN_CLIMATE]["cooling_equipment_offset_c"] = 0
    assert evaluate(c, "cool", 21)["reason"] == "zone_equipment_offset_invalid"


def test_compensated_paused_hidden_target_is_not_written_or_reopened():
    c, config = compensated(bed=20.9)
    hass = compensated_device(c)
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {"zone_states": {BED_CLIMATE: {"target_temperature": 23}}})
    assert asyncio.run(adapter.async_execute(_action(desired(c)))).applied
    assert hass.states.get(MAIN_CLIMATE).attributes["temperature"] == 19
    assert hass.states.get(BED).state == "off"
    assert all(payload["entity_id"] != BED_CLIMATE for _, _, payload in hass.services.calls)


@pytest.mark.parametrize("change", ["bounds", "step", "mode", "missing", "head_range"])
def test_incompatible_live_capabilities_block_compensated_writes(change):
    c, config = compensated()
    hass = compensated_device(c)
    head = hass.states.get("climate.head")
    if change == "bounds":
        head.attributes["min_temp"] = 20
    elif change == "step":
        head.attributes["target_temp_step"] = 2
    elif change == "mode":
        head.attributes["hvac_modes"] = ["heat", "off"]
    elif change == "missing":
        head.attributes.pop("target_temp_step")
    else:
        c.climate_inputs["zones"][BED].update(low=23, high=25, temperature=24)
        hass.states.values["sensor.bed"].state = "24"
        config["hvac_zone_mappings"][BED]["low"] = "input_number.bed_low"
        hass.states.values["input_number.bed_low"] = FakeState("23")
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    result = asyncio.run(adapter.async_execute(_action(desired(c))))
    assert not result.applied
    assert all(not (service == "set_temperature" and payload.get("temperature") == 19)
               for _, service, payload in hass.services.calls)


@pytest.mark.parametrize("change", ["bounds", "manual", "sensor", "head_intent"])
def test_compensated_persist_boundary_rechecks_capabilities_and_user_authority(change):
    c, config = compensated()
    hass = compensated_device(c)
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    overridden = []
    adapter.set_manual_override_check(lambda: bool(overridden))

    async def persist(record):
        if record and record["entity_id"] == MAIN_CLIMATE:
            if change == "bounds":
                hass.states.values[MAIN_CLIMATE].attributes["max_temp"] = 18
            elif change == "manual":
                overridden.append(True)
            else:
                hass.states.values["sensor.living"].state = "20"

    adapter.set_confirmation_persistence_callback(persist)
    command = desired(c)
    if change == "head_intent":
        command["target_temperature"] = 21
        assert live_rejection(hass, config, DEFAULT_OPTIONS, command, {}, NOW) == "zone_equipment_target_superseded"
    result = asyncio.run(adapter.async_execute(_action(command)))
    assert not result.applied
    assert all(not (service == "set_temperature" and payload == {"entity_id": MAIN_CLIMATE, "temperature": 19})
               for _, service, payload in hass.services.calls)


@pytest.mark.parametrize("bad", [True, None, "bad", float("inf"), -2.5, .5, -.25])
def test_offset_configuration_is_bounded_and_does_not_infer_safe_airflow(bad):
    _, config = compensated()
    config["hvac_zone_mappings"][MAIN]["cooling_equipment_offset_c"] = bad
    assert validate_climate_config(config)


def test_offset_options_update_both_aliases_preserving_sources_and_household_helpers():
    _, config = compensated()
    config["hvac_zone_mappings"][MAIN_CLIMATE] = {"temperature": "sensor.living", "humidity": "sensor.rh"}
    config["hvac_zone_mappings"][MAIN].pop("cooling_equipment_offset_c")
    entry = SimpleNamespace(data=config, options={})
    updates = []
    flow = OptionsFlow(entry)
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_update_entry=lambda entry, **kwargs: updates.append(kwargs)))
    assert asyncio.run(flow.async_step_zone_equipment())["step_id"] == "zone_equipment"
    assert asyncio.run(flow.async_step_zone_equipment({MAIN: -2, BED: 0}))["type"] == "create_entry"
    saved = updates[-1]["data"]
    assert saved["hvac_zone_mappings"][MAIN_CLIMATE] == {
        "temperature": "sensor.living", "humidity": "sensor.rh", "cooling_equipment_offset_c": -2}
    assert saved["hvac_zone_mappings"][MAIN]["cooling_equipment_offset_c"] == -2
    assert saved["climate_target_low_entity"] == config["climate_target_low_entity"]
    assert asyncio.run(flow.async_step_zone_equipment({MAIN: -3}))["errors"]
    config["hvac_zone_mappings"][MAIN]["cooling_equipment_offset_c"] = -2
    config["hvac_zone_mappings"][MAIN_CLIMATE]["cooling_equipment_offset_c"] = 0
    assert validate_climate_config(config)
    config.pop("hvac_zone_control")
    assert validate_climate_config(config)
    flow._data = {}
    assert asyncio.run(flow.async_step_zone_equipment())["reason"] == "no_climate_zones"


def observe(c, hass, minutes, *, decrement=.1):
    c.created_at = NOW + timedelta(minutes=minutes)
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 26 - (minutes % (24 * 60)) / 5 * decrement
        state = hass.states.values[room["temperature_entity"]]
        state.state = str(room["temperature"])
        state.last_updated = c.created_at
    return sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")


def trained():
    c = room_context(main=26, bed=26)
    hass = device(c)
    model = {}
    for i in range(13):
        model = update(model, observe(c, hass, i * 5))
    assert rate(model, MAIN, "cool", c.created_at) == pytest.approx(1.2)
    return c, hass, model


def test_repeated_bedroom_cycles_keep_history_select_regime_and_reject_transition_windows():
    c, hass, model = trained()
    identity = model["sources"][MAIN]["identity"]
    original = deepcopy(model["sources"][MAIN]["buckets"])
    hass.states.values[BED] = "off"
    c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=65)
    model = update(model, observe(c, hass, 65))
    assert model["sources"][MAIN]["identity"] == identity
    assert model["sources"][MAIN]["buckets"] == original
    assert model["sources"][MAIN]["last_rejection"] == "operating_transition"
    assert rate(model, MAIN, "cool", c.created_at) is None
    evidence = summary(model, c.created_at)["sources"][MAIN]
    assert evidence["retained_windows"]["cool"] == 12
    assert evidence["modes"]["cool"]["count"] == 0
    for i in range(14, 27):
        model = update(model, observe(c, hass, i * 5, decrement=.2))
    assert rate(model, MAIN, "cool", c.created_at) == pytest.approx(2.4)
    assert summary(model, c.created_at)["sources"][MAIN]["modes"]["cool"]["count"] == 12
    for i, power in enumerate(["on", "off", "on", "off", "on"], 27):
        hass.states.values[BED] = power
        c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=i * 5)
        model = update(model, observe(c, hass, i * 5))
        assert rate(model, MAIN, "cool", c.created_at) == pytest.approx(1.2 if power == "on" else 2.4)
        assert model["sources"][MAIN]["identity"] == identity
        assert model["sources"][MAIN]["buckets"]["cool"]["count"] == 24
    # A close/reopen between samples leaves the same power but still breaks the interval.
    model = update(model, observe(c, hass, 160))
    count = model["sources"][MAIN]["buckets"]["cool"]["count"]
    c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=162)
    model = update(model, observe(c, hass, 165))
    assert model["sources"][MAIN]["last_rejection"] == "operating_transition"
    assert model["sources"][MAIN]["buckets"]["cool"]["count"] == count


def test_source_models_save_load_and_invalidate_only_changed_sensor(monkeypatch):
    c, hass, model = trained()
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded_by_key = None
    FakeStore.loaded = None
    store = PlannerStore(object(), "thermal-regimes")
    asyncio.run(store.async_save_thermal_model(model))
    # Simulate the JSON serialization boundary, then load through the real storage API.
    FakeStore.loaded = json.loads(json.dumps(FakeStore.saved))
    reloaded = PlannerStore(object(), "thermal-regimes")
    asyncio.run(reloaded.async_load())
    restored = reloaded.data["thermal_model"]
    assert restored == model
    assert rate(restored, MAIN, "cool", c.created_at) == pytest.approx(1.2)
    hass.states.values[BED] = "off"
    c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=65)
    restored = update(restored, observe(c, hass, 65))
    assert rate(restored, MAIN, "cool", c.created_at) is None
    hass.states.values[BED] = "on"
    c.hvac_control["zone_transition_at"] = (NOW + timedelta(minutes=70)).isoformat()
    restored = update(restored, observe(c, hass, 70))
    assert rate(restored, MAIN, "cool", c.created_at) == pytest.approx(1.2)
    before = deepcopy(restored["sources"][BED]["buckets"])
    c.climate_inputs["zones"][MAIN]["temperature_entity"] = "sensor.replacement"
    hass.states.values["sensor.replacement"] = SimpleNamespace(state="24", attributes={}, last_updated=c.created_at)
    restored = update(restored, observe(c, hass, 75))
    assert restored["sources"][MAIN]["buckets"] == {}
    assert restored["sources"][BED]["buckets"]["cool"]["windows"][:12] == before["cool"]["windows"]
    assert rate(restored, BED, "cool", c.created_at) == pytest.approx(1.2)
    assert rate(restored, BED, "cool", NOW + timedelta(days=15)) is None
    # A real controller topology change invalidates the shared airflow evidence.
    c.climate_inputs["zone_topology"]["safe_open_sets"] = [[MAIN, BED]]
    restored = update(restored, observe(c, hass, 80))
    assert restored["sources"][BED]["buckets"] == {}
    FakeStore.loaded = None


def test_compensated_readback_keeps_mapping_offsets_without_changing_comfort():
    c, config = compensated()
    hass = compensated_device(c)
    result = read_climate_inputs(hass, config, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {})
    assert result["configuration_valid"]
    assert result["zones"][MAIN]["cooling_equipment_offset_c"] == -2
    assert "cooling_equipment_offset_c" not in result["zones"][BED]


@pytest.mark.parametrize("change", [None, "head", "goal", "power", "phase", "missing_goal", "intent", "stop"])
def test_room_comfort_constraint_verifies_compensation_without_weakening_bounds(change):
    c, _ = compensated()
    c.hvac_control = ownership(c)
    action = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0]
    if change == "head":
        action.desired_state["target_temperature"] = 18
    elif change == "goal":
        action.desired_state["precondition_target"] = 19
    elif change == "power":
        action.desired_state["zone_power"][BED] = "off"
    elif change == "phase":
        action.desired_state["phase"] = "peak_coast"
    elif change == "missing_goal":
        action.desired_state.pop("precondition_target")
    elif change == "intent":
        action.desired_state.pop("zone_intent")
    elif change == "stop":
        action.desired_state["zone_stop"] = True
    options = {**DEFAULT_OPTIONS, "occupied_temperature_tolerance_percent": 0,
               "planner_enabled": True, "dry_run": False}
    from custom_components.ha_energy_planner.ownership import OwnershipState
    owned = OwnershipState(planner_takeover_started_at=NOW-timedelta(minutes=30),
                           hvac_control_phase='preconditioning')
    result = ConstraintValidator(options).validate_action(c, _plan(NOW, action), action, now=NOW, ownership=owned)
    if change:
        expected = "hvac_target_outside_comfort_bounds" if change == "goal" else "hvac_equipment_compensation_invalid"
        assert expected in result
    else:
        assert not result
        assert _preparation(action.desired_state)["target"] == 21


@pytest.mark.parametrize("already_matches", [False, True])
def test_active_zone_waits_for_compatible_live_bounds_even_when_target_matches(already_matches):
    c, config = compensated()
    hass = compensated_device(c)
    original = hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        await original(domain, service, payload, **kwargs)
        if payload["entity_id"] == "climate.head" and service == "set_temperature" and payload["temperature"] == 19:
            state = hass.states.get(MAIN_CLIMATE)
            state.attributes.update(min_temp=20, max_temp=21)
            if already_matches:
                state.attributes["temperature"] = 19
            hass.states.values[MAIN_CLIMATE] = state

    hass.services.async_call = call
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    result = asyncio.run(adapter.async_execute(_action(desired(c))))
    assert not result.applied and result.saved_main_state["target_temperature"] == 24
    assert all(not (service == "set_temperature" and payload == {"entity_id": MAIN_CLIMATE, "temperature": 19})
               for _, service, payload in hass.services.calls)
    # The executor releases failed subset acquisition through the durable baseline.
    restored = asyncio.run(DaikinHVACAdapter(hass, config).async_restore(
        result.saved_automation_states, result.saved_zone_states, result.saved_main_state))
    assert restored.applied
    assert hass.states.get("climate.head").attributes["temperature"] == 24


def test_measured_electrical_evidence_selects_regime_and_cannot_revive_expired_windows():
    c = room_context(main=26, bed=26)
    hass = device(c)
    c.climate_inputs["power_source_type"] = "measured"
    model = {}
    for i in range(13):
        model = update(model, observe(c, hass, i * 5))
    assert summary(model, c.created_at)["electrical_load_model_ready"]
    assert rate(model, MAIN, "heat", c.created_at) is None
    hass.states.values[BED] = "off"
    c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=65)
    model = update(model, observe(c, hass, 65))
    assert not summary(model, c.created_at)["electrical_load_model_ready"]
    hass.states.values[BED] = "on"
    c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=70)
    model = update(model, observe(c, hass, 70))
    assert summary(model, c.created_at)["electrical_load_model_ready"]
    for i in range(3):
        model = update(model, observe(c, hass, 15 * 24 * 60 + i * 5))
    assert rate(model, MAIN, "cool", c.created_at) is None
    assert not summary(model, c.created_at)["electrical_load_model_ready"]
    assert model["sources"][MAIN]["buckets"]["cool"]["count"] == 1


def test_many_airflow_regimes_share_the_existing_96_window_bound():
    c = room_context(main=26, bed=26)
    hass = device(c)
    model = {}
    for i in range(150):
        power = "on" if (i // 20) % 2 == 0 else "off"
        if hass.states.get(BED).state != power:
            hass.states.values[BED] = power
            c.hvac_control["zone_transition_at"] = NOW + timedelta(minutes=i * 5)
        model = update(model, observe(c, hass, i * 5))
    rows = model["sources"][MAIN]["buckets"]["cool"]["windows"]
    assert len(rows) == 96
    assert len({row["regime"] for row in rows}) == 2
    assert all(row["minutes"] >= 5 for row in rows)


def test_live_intent_rejects_an_incompatible_shared_head_range_from_an_old_producer():
    c, config = compensated(bed=24)
    c.climate_inputs["zones"][BED].update(low=23, high=25)
    config["hvac_zone_mappings"][BED]["low"] = "input_number.bed_low"
    config["hvac_zone_mappings"][BED]["high"] = "input_number.bed_high"
    hass = compensated_device(c)
    hass.states.values["input_number.bed_low"] = FakeState("23")
    hass.states.values["input_number.bed_high"] = FakeState("25")
    old_intent = {"mode": "cool", "precondition_target": 21, "target_temperature": 19,
                  "equipment_compensation": True, "zone_intent": evaluate(c, "cool", 21)}
    assert live_rejection(hass, config, DEFAULT_OPTIONS, old_intent, {}, NOW) == "zone_equipment_range_incompatible"


@pytest.mark.parametrize("target", [24, 24.0, "24.0"])
def test_equivalent_equipment_target_encodings_keep_the_same_stable_regime(target):
    c = room_context()
    hass = device(c)
    first = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    state = hass.states.get("climate.head")
    state.attributes["temperature"] = target
    hass.states.values["climate.head"] = state
    c.hvac_control["zone_transition_at"] = NOW + timedelta(seconds=1)
    after = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    assert first["sources"][MAIN]["identity"] == after["sources"][MAIN]["identity"]
    assert first["sources"][MAIN]["regime"] == after["sources"][MAIN]["regime"]
    assert first["sources"][MAIN]["transition_at"] != after["sources"][MAIN]["transition_at"]


@pytest.mark.parametrize("change", ["missing", "stop", "mode"])
def test_compensated_command_without_current_zone_intent_is_rejected_before_io(change):
    c, config = compensated()
    hass = compensated_device(c)
    command = desired(c)
    if change == "missing":
        command.pop("zone_intent")
    elif change == "stop":
        command["zone_stop"] = True
    else:
        command["mode"] = "heat"
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    result = asyncio.run(adapter.async_execute(_action(command)))
    assert not result.applied and result.reason == "zone_equipment_intent_invalid"
    assert not hass.services.calls


@pytest.mark.parametrize("stored_alias", [MAIN, MAIN_CLIMATE])
def test_equipment_form_preserves_effective_offset_from_either_paired_alias(stored_alias):
    config = data()
    config["hvac_zone_mappings"][MAIN_CLIMATE] = {"temperature": "sensor.living"}
    config["hvac_zone_mappings"][stored_alias]["cooling_equipment_offset_c"] = -2
    assert not validate_climate_config(config)
    updates = []
    flow = OptionsFlow(SimpleNamespace(data=config, options={}))
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_update_entry=lambda entry, **kwargs: updates.append(kwargs)))
    form = asyncio.run(flow.async_step_zone_equipment())
    marker = next(key for key in form["data_schema"].schema if key.schema == MAIN)
    shown = marker.description["suggested_value"]
    assert shown == -2
    # Accepting the displayed setting while editing another zone must preserve it.
    assert asyncio.run(flow.async_step_zone_equipment({MAIN: shown, BED: 0}))["type"] == "create_entry"
    saved = updates[-1]["data"]["hvac_zone_mappings"]
    assert saved[MAIN]["cooling_equipment_offset_c"] == saved[MAIN_CLIMATE]["cooling_equipment_offset_c"] == -2


def test_compensated_calendar_and_action_describe_room_goal_separately_from_equipment():
    c, _ = compensated()
    c.hvac_control = ownership(c)
    action = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0]
    event = calendar_module._calendar_event(action, calendar_coordinator(calendar_plan([action])))
    assert event.summary == "Climate: Pre-cool to 21 °C (planned)"
    description = "Cool with a room preparation target of 21 C and shared equipment at 19 C."
    assert plain_action(action)["decision"] == description
    assert event.description.startswith(description)
    assert action.desired_state["target_temperature"] == 19


@pytest.mark.parametrize("initial_mode", ["heat", "off"])
@pytest.mark.parametrize("cooling_target_valid", [True, False])
def test_compensation_rejects_unproven_mode_restoration_before_selecting_cooling(initial_mode, cooling_target_valid):
    c, config = compensated()
    c.current_hvac_mode = "heat"
    # Explicit cooling permission can request a mode change; weather alone must not reverse a running head.
    cooling = HVACPlanningPolicy({**DEFAULT_OPTIONS, "hvac_preconditioning_mode": "cooling_only"}, {})
    assert cooling._next_hvac_period(c)["mode"] == "cool"
    hass = compensated_device(c)
    head = hass.states.get("climate.head")
    head.state = initial_mode
    head.attributes.update(min_temp=22, max_temp=30, target_temp_step=2)
    for climate in PAIRS.values():
        hass.states.values[climate].state = initial_mode
    original = hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        await original(domain, service, payload, **kwargs)
        if payload["entity_id"] == "climate.head" and service in {"turn_on", "set_hvac_mode", "turn_off"}:
            head = hass.states.get("climate.head")
            if head.state == "cool":
                head.attributes.update(min_temp=16 if cooling_target_valid else 20, max_temp=32, target_temp_step=.5)
            else:
                head.attributes.update(min_temp=22, max_temp=30, target_temp_step=2)
            for climate in PAIRS.values():
                hass.states.values[climate].state = head.state

    hass.services.async_call = call
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    result = asyncio.run(adapter.async_execute(_action(desired(c))))
    assert not result.applied and result.reason == "climate_zone_baseline_mode_mismatch"
    assert result.rollback_succeeded and not hass.services.calls
    assert hass.states.get("climate.head").state == initial_mode
    assert hass.states.get("climate.head").attributes["temperature"] == 24
