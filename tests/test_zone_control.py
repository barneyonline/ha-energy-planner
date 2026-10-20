"""Independent room preparation, airflow proof and durable actuator transactions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import Context
from homeassistant.helpers import entity_registry
from test_constraints import _plan
from test_executor import FakeStore, _arm_store
from test_hvac_adapter import FakeHass, FakeState, _action
from test_zone_temperature import NOW, context

from custom_components.ha_energy_planner import hvac_adapter as adapter_module
from custom_components.ha_energy_planner.climate_inputs import read_climate_inputs, validate_climate_config
from custom_components.ha_energy_planner.climate_runtime import economic_actions
from custom_components.ha_energy_planner.const import CONF_HVAC_POLICY, DEFAULT_OPTIONS
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.coordinator import (
    _matches_climate_transaction,
    _matches_pending_coupled_zone_hvac_feedback,
    _pending_zone_hvac_manual_change_entity_id,
    _unambiguous_coupled_zone_entity_ids_match,
)
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter, HVACCommandResult
from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction
from custom_components.ha_energy_planner.models import OccupancyState
from custom_components.ha_energy_planner.ownership import OwnershipState
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.thermal_learning import sample, update
from custom_components.ha_energy_planner.zone_control import desired_fields, evaluate, live_rejection, topology
from custom_components.ha_energy_planner.zone_temperature import comfort_breached

MAIN, BED = "switch.main", "switch.bed"
MAIN_CLIMATE, BED_CLIMATE = "climate.living_thermostat", "climate.sleeping_thermostat"
PAIRS = {MAIN: MAIN_CLIMATE, BED: BED_CLIMATE}


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(entity_registry, "async_get", lambda hass: SimpleNamespace(
        async_get=lambda entity: hass.zone_registry_entries.get(entity)))


def data():
    return {
        "daikin_climate_entity": "climate.head", "climate_zone_entities": [*PAIRS, *PAIRS.values()],
        "climate_target_low_entity": "input_number.low", "climate_target_high_entity": "input_number.high",
        "hvac_zone_control": {"pairs": PAIRS, "safe_open_sets": [[MAIN, BED], [MAIN], [BED]]},
        "hvac_zone_mappings": {MAIN: {"temperature": "sensor.living"}, BED: {"temperature": "sensor.bed"}},
    }


def room_context(mode="cool", main=23.73, bed=20.96):
    result = context()
    result.climate_zone_entities = data()["climate_zone_entities"]
    result.occupied_temperature_low_c, result.occupied_temperature_high_c = 21, 24
    result.current_hvac_mode = mode
    result.climate_inputs = {
        "zone_topology": topology(data()), "power_source_type": "estimated",
        "zones": {key: {"temperature": value, "temperature_entity": sensor,
                        "temperature_source": {"fresh": True, "status": "ready", "updated_at": NOW.isoformat()}}
                  for key, value, sensor in [(MAIN, main, "sensor.living"), (BED, bed, "sensor.bed")]},
    }
    return result


def ownership(c, mode="cool"):
    return {"mode": mode, "phase": "preconditioning", "period_start": NOW + timedelta(hours=3),
            "period_end": NOW + timedelta(hours=5), "baseline_price": 0.1,
            "precondition_target": 21 if mode == "cool" else 24,
            "main_state_committed": True}


def device(c):
    hass = FakeHass({
        "climate.head": FakeState(c.current_hvac_mode, {"temperature": 24, "min_temp": 16, "max_temp": 32}),
        MAIN: "on", BED: "on", MAIN_CLIMATE: FakeState(c.current_hvac_mode, {"temperature": 23}),
        BED_CLIMATE: FakeState(c.current_hvac_mode, {"temperature": 23}),
        "input_number.low": FakeState("21"), "input_number.high": FakeState("24"),
    })
    for room in c.climate_inputs["zones"].values():
        hass.states.values[room["temperature_entity"]] = SimpleNamespace(
            state=str(room["temperature"]), attributes={"unit_of_measurement": "°C"}, last_updated=c.created_at,
        )
    original_get = hass.states.get
    hass.states.get = lambda entity: (hass.states.values[entity] if isinstance(
        hass.states.values.get(entity), SimpleNamespace) else original_get(entity))
    hass.zone_registry_entries = {entity: SimpleNamespace(config_entry_id="daikin", device_id="controller",
                                                         platform="daikin")
                                  for entity in ["climate.head", *PAIRS, *PAIRS.values()]}
    return hass


@pytest.mark.parametrize(("mode", "main", "bed", "expected"), [
    ("cool", 23.73, 20.96, {MAIN: "on", BED: "off"}),
    ("cool", 20.96, 23.73, {MAIN: "off", BED: "on"}),
    ("heat", 21, 24.1, {MAIN: "on", BED: "off"}),
    ("heat", 24.1, 21, {MAIN: "off", BED: "on"}),
])
def test_today_reverse_and_heating_independent_completion(mode, main, bed, expected):
    c = room_context(mode, main, bed)
    result = evaluate(c, mode, 24 if mode == "heat" else 21)
    assert result["power"] == expected
    assert not comfort_breached(c, mode, coasting=False)
    # The original shared-unit policy still protects unsupported installations.
    c.climate_inputs.pop("zone_topology")
    assert comfort_breached(c, mode, coasting=False)


def test_distinct_room_targets_aliases_and_shared_sensor_do_not_merge_actuators():
    c = room_context(bed=22)
    c.climate_inputs["zones"][BED].update(low=22.5, high=25)
    result = evaluate(c, "cool", 21)
    assert result["targets"] == {MAIN_CLIMATE: 21, BED_CLIMATE: 22.5}
    c.climate_inputs["zones"][MAIN_CLIMATE] = deepcopy(c.climate_inputs["zones"][MAIN])
    assert evaluate(c, "cool", 21)["supported"]
    c.climate_inputs["zones"][MAIN_CLIMATE]["low"] = 20
    assert evaluate(c, "cool", 21)["reason"] == "zone_mapping_missing_or_ambiguous"
    del c.climate_inputs["zones"][MAIN_CLIMATE]
    c.climate_inputs["zones"][BED]["temperature_entity"] = "sensor.living"
    # Even sharing a source never turns two explicitly independent dampers into one.
    assert len(evaluate(c, "cool", 21)["power"]) == 2


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_pause_hysteresis_and_dwell_survive_serialization(mode):
    c = room_context(mode, main=22, bed=24.1 if mode == "heat" else 20.9)
    target = 24 if mode == "heat" else 21
    first = evaluate(c, mode, target)
    c.hvac_control = {"zone_intent": deepcopy(first)}
    c.climate_inputs["zones"][BED]["temperature"] = 23.6 if mode == "heat" else 21.4
    c.created_at = NOW + timedelta(minutes=9, seconds=59)
    assert evaluate(c, mode, target)["zones"][BED]["restart_blocker"] == "pause_dwell"
    c.created_at = NOW + timedelta(minutes=10)
    c.climate_inputs["zones"][BED]["temperature"] = 23.8 if mode == "heat" else 21.2
    assert evaluate(c, mode, target)["zones"][BED]["restart_blocker"] == "hysteresis"
    c.climate_inputs["zones"][BED]["temperature"] = 23.6 if mode == "heat" else 21.4
    assert evaluate(c, mode, target)["power"][BED] == "on"
    c.hvac_control["zone_intent"]["zones"][BED]["paused_at"] = "bad"
    assert evaluate(c, mode, target)["reason"] == "zone_pause_time_invalid"


def test_all_complete_coast_keeps_airflow_and_original_intent():
    c = room_context(main=20.9)
    c.hvac_control = ownership(c)
    result = evaluate(c, "cool", 21)
    assert result["all_complete"] and all(row["status"] == "completed" for row in result["zones"].values())
    desired = desired_fields(c, "cool", 21, "pre_peak_coast")
    assert desired["zone_stop"] and desired["hvac_mode"] == "off"
    assert not desired["enable_zones"] and "zone_power" not in desired
    actions = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    assert actions[0].desired_state["phase"] == "pre_peak_coast"
    assert actions[0].desired_state["zone_stop"]


def test_active_and_initial_satisfied_room_fallback_and_economic_guard():
    c = room_context()
    c.hvac_control = ownership(c)
    actions = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    assert actions[0].desired_state["zone_power"] == {MAIN: "on", BED: "off"}
    assert actions[0].desired_state["phase"] == "preconditioning"
    c.hvac_control = {}
    assert HVACPlanningPolicy(DEFAULT_OPTIONS, {})._next_hvac_period(c) is not None
    assert economic_actions(c, DEFAULT_OPTIONS, actions) == actions
    assert c.climate_decision["reason"] == "zone_subset_economic_model_unvalidated"
    assert "expected_saving" not in c.climate_decision
    c.hvac_control = {"economic_policy_version": 1}
    assert economic_actions(c, DEFAULT_OPTIONS, actions)[0].kind == "release_hvac"
    c.hvac_control = {}
    c.climate_engine = {"ever_active": True}
    assert economic_actions(c, DEFAULT_OPTIONS, actions) == [
        action for action in actions if action.kind == "release_hvac"
    ]


@pytest.mark.parametrize("change", ["sensor", "stale", "mapping", "bounds", "topology", "airflow", "mode"])
def test_missing_invalid_or_unsafe_inputs_fail_closed(change):
    c = room_context()
    if change == "sensor":
        c.climate_inputs["zones"][BED]["temperature"] = None
    elif change == "stale":
        c.climate_inputs["zones"][BED]["temperature_source"]["fresh"] = False
    elif change == "mapping":
        c.climate_inputs["zones"].pop(BED)
    elif change == "bounds":
        c.climate_inputs["zones"][BED]["low"] = 25
    elif change == "topology":
        c.hvac_control = {"zone_intent": {"pairs": {"switch.other": "climate.other"}}}
    elif change == "mode":
        c.current_hvac_mode = "dry"
    else:
        c.climate_inputs["zone_topology"]["safe_open_sets"] = [[MAIN, BED]]
    assert not evaluate(c, c.current_hvac_mode, 21)["supported"]
    assert "zone_control_blocked" in desired_fields(c, c.current_hvac_mode, 21, "preconditioning")


@pytest.mark.parametrize("bad", [
    [], {"pairs": {}}, {"pairs": [], "safe_open_sets": []},
    {"pairs": {MAIN: BED_CLIMATE, BED: BED_CLIMATE}, "safe_open_sets": [[MAIN, BED]]},
    {"pairs": PAIRS, "safe_open_sets": [[]]}, {"pairs": PAIRS, "safe_open_sets": [[MAIN]]},
    {"pairs": PAIRS, "safe_open_sets": [[MAIN, BED, "switch.unknown"]]},
    {"pairs": PAIRS, "safe_open_sets": [[MAIN, BED], [False]]},
    {"pairs": PAIRS, "safe_open_sets": [[MAIN, BED], [MAIN, MAIN]]},
])
def test_topology_validation_rejects_ambiguous_or_unproven_config(bad):
    config = {**data(), "hvac_zone_control": bad}
    assert not topology(config)["supported"]
    if bad:
        assert validate_climate_config(config)["hvac_zone_control"] == "invalid_climate_configuration"


def test_topology_requires_explicit_pairs_and_complete_configured_zone_coverage():
    config = data()
    config["climate_zone_entities"] = ",".join(config["climate_zone_entities"])
    assert topology(config)["supported"]
    config["climate_zone_entities"] += ",switch.manual_exclusion"
    assert not topology(config)["supported"]
    config.pop("hvac_zone_control")
    assert topology(config)["reason"] == "independent_airflow_not_configured"


def test_live_freshness_intent_supersession_and_stop():
    c = room_context()
    hass = device(c)
    desired = {"mode": "cool", "precondition_target": 21, **desired_fields(c, "cool", 21, "preconditioning")}
    assert live_rejection(hass, data(), DEFAULT_OPTIONS, desired, {}, NOW) is None
    hass.states.values["sensor.bed"].state = "23"
    assert live_rejection(hass, data(), DEFAULT_OPTIONS, desired, {}, NOW) == "zone_intent_superseded"
    assert live_rejection(hass, data(), DEFAULT_OPTIONS, desired, {}, NOW + timedelta(hours=3))
    assert live_rejection(hass, data(), DEFAULT_OPTIONS, {"zone_control_blocked": "unsafe"}, {}, NOW) == "unsafe"
    assert live_rejection(hass, data(), DEFAULT_OPTIONS, {**desired, "zone_stop": True}, {}, NOW) is None
    assert live_rejection(hass, data(), DEFAULT_OPTIONS, {}, {}, NOW) is None


def test_adapter_closes_cold_room_keeps_it_closed_and_restores_originals(monkeypatch):
    c = room_context()
    hass = device(c)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    automations, zones = adapter.takeover_snapshot()
    main = adapter.main_takeover_snapshot()
    desired = {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    result = asyncio.run(adapter.async_execute(_action(desired)))
    assert result.applied
    assert hass.states.get(BED).state == "off" and hass.states.get(MAIN).state == "on"
    assert hass.states.get(MAIN_CLIMATE).attributes["temperature"] == 21
    assert hass.states.get(BED_CLIMATE).attributes["temperature"] == 23
    hass.services.calls.clear()
    previous = {"zone_states": zones, "zone_intent": desired["zone_intent"]}
    second = DaikinHVACAdapter(hass, data())
    second.set_zone_control_context(DEFAULT_OPTIONS, previous)
    assert asyncio.run(second.async_execute(_action(desired))).applied
    assert not hass.services.calls  # Refresh never reopens a paused room.
    restored = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore(automations, zones, main))
    assert restored.applied
    assert hass.states.get(BED).state == "on"
    assert hass.states.get(MAIN_CLIMATE).attributes["temperature"] == 23
    assert hass.states.get("climate.head").attributes["temperature"] == 24


def test_adapter_preserves_airflow_open_before_close_and_all_complete_stop(monkeypatch):
    c = room_context(main=20.9, bed=23)
    hass = device(c)
    hass.states.values[BED] = "off"
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    desired = {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    assert asyncio.run(adapter.async_execute(_action(desired))).applied
    calls = [(service, payload["entity_id"]) for _, service, payload in hass.services.calls]
    assert calls.index(("turn_on", BED)) < calls.index(("turn_off", MAIN))
    hass.services.calls.clear()
    c.climate_inputs["zones"][BED]["temperature"] = 20.9
    stop = {"mode": "cool", "precondition_target": 21, **desired_fields(c, "cool", 21, "pre_peak_coast")}
    assert asyncio.run(adapter.async_execute(_action(stop))).applied
    assert hass.states.get("climate.head").state == "off"
    assert hass.states.get(BED).state == "on"
    assert all(payload["entity_id"] not in PAIRS for _, _, payload in hass.services.calls)


def test_failed_or_uncertain_closure_retains_restoration_and_bounded_readback(monkeypatch):
    c = room_context()
    hass = device(c)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    hass.services.noop_entities.add(BED)
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    desired = {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    persisted = []
    async def persist(record):
        persisted.append(deepcopy(record))
    adapter.set_confirmation_persistence_callback(persist)
    result = asyncio.run(adapter.async_execute(_action(desired)))
    assert not result.applied and result.rollback_succeeded is False
    assert result.reason == "zone_subset_confirmation_failed"
    assert result.saved_zone_states[BED] == "on"
    assert any(record and record["entity_id"] == BED for record in persisted)
    assert sum(payload["entity_id"] == BED for _, _, payload in hass.services.calls) == 1


def test_original_baselines_and_durable_intent_survive_subset_reopen_and_coast():
    c = room_context()
    original = {"hvac_control": {**ownership(c), "main_state": {"hvac_mode": "cool", "target_temperature": 24},
                                 "zone_states": {MAIN: "on", BED: "off", MAIN_CLIMATE: {"target_temperature": 23}}},
                "climate_automations": {"automation.climate": "on"}}
    current = deepcopy(original)
    for minute, phase in [(0, "preconditioning"), (11, "preconditioning"), (15, "pre_peak_coast")]:
        c.created_at = NOW + timedelta(minutes=minute)
        c.hvac_control = current["hvac_control"]
        desired = {"phase": phase, **desired_fields(c, "cool", 21, phase)}
        transaction = HVACOwnershipTransaction(current, c.created_at)
        prepared = transaction.prepare(desired, {"automation.climate": "off"}, {MAIN: "off", BED: "on"},
                                       {"hvac_mode": "cool", "target_temperature": 21})
        assert prepared["hvac_control"]["zone_intent"] == desired["zone_intent"]
        current = transaction.complete(HVACCommandResult(True, "applied", {}, {}, {}), prepared, desired,
                                       main_state_superseded=False, superseded_zone_entity_ids=set())
        assert current["hvac_control"]["main_state"] == original["hvac_control"]["main_state"]
        assert current["hvac_control"]["zone_states"] == original["hvac_control"]["zone_states"]
        assert current["climate_automations"] == original["climate_automations"]


def test_alias_off_null_is_command_feedback_but_explicit_user_change_wins():
    entry = data()
    assert _unambiguous_coupled_zone_entity_ids_match(entry, BED, BED_CLIMATE)
    assert not _unambiguous_coupled_zone_entity_ids_match(entry, BED, MAIN_CLIMATE)
    event = SimpleNamespace(data={"entity_id": BED_CLIMATE, "old_state": FakeState("cool", {"temperature": 23}),
                                  "new_state": FakeState("off", {"temperature": None})}, context=Context())
    pending = {"hvac_mode": "cool", "enable_zones": True, "configured_zones_only": True,
               "zone_power": {MAIN: "on", BED: "off"},
               "coupled_zone_feedback_expected": {"actuator_entity_id": BED, "state": "off", "context_id": "own"}}
    assert _matches_pending_coupled_zone_hvac_feedback(entry, pending, event)
    assert _pending_zone_hvac_manual_change_entity_id(entry, event, pending) is None
    command = {"entity_id": BED, "service": "turn_off", "context_id": "own", "issued_at": NOW,
               "deadline": NOW + timedelta(seconds=180), "requested": {}}
    assert _matches_climate_transaction(entry, [command], event, NOW)
    event.context = Context(user_id="person")
    assert not _matches_climate_transaction(entry, [command], event, NOW)
    event.context = Context()
    event.data.update(entity_id=BED, old_state=FakeState("on"), new_state=FakeState("off"))
    assert _pending_zone_hvac_manual_change_entity_id(entry, event, pending) is None
    event.data["new_state"] = FakeState("on")
    event.data["old_state"] = FakeState("off")
    assert _pending_zone_hvac_manual_change_entity_id(entry, event, pending) == BED


def test_learning_breaks_at_subset_transition_and_does_not_reuse_full_topology_rate():
    c = room_context()
    hass = device(c)
    first = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    model = update({}, first)
    hass.states.values[BED] = "off"
    c.created_at += timedelta(minutes=5)
    second = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    assert first["sources"][MAIN]["identity"] != second["sources"][MAIN]["identity"]
    model = update(model, second)
    assert not model["sources"][MAIN]["buckets"]
    read = read_climate_inputs(hass, data(), DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {})
    assert read["zone_topology"]["supported"]


@pytest.mark.parametrize("missing", ["head", "entry_id", "device_id", "actuator", "other_controller"])
def test_registry_rejects_missing_or_cross_controller_topology(missing):
    c = room_context()
    hass = device(c)
    if missing == "head":
        hass.zone_registry_entries.pop("climate.head")
    elif missing == "entry_id":
        hass.zone_registry_entries["climate.head"].config_entry_id = None
    elif missing == "device_id":
        hass.zone_registry_entries["climate.head"].device_id = None
    elif missing == "actuator":
        hass.zone_registry_entries.pop(BED)
    else:
        hass.zone_registry_entries[BED].device_id = "other"
    read = read_climate_inputs(hass, data(), DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {})
    assert read["zone_topology"]["reason"] == "zone_controller_registry_unproven"
    assert not read["zone_topology"]["supported"]


def test_constraint_allows_owned_occupied_coast_stop_and_rejects_unowned_suppression():
    c = room_context(main=20.9)
    c.hvac_control = ownership(c)
    c.hvac_control["lifecycle_id"] = "owned"
    action = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0]
    owned = OwnershipState(planner_takeover_started_at=NOW - timedelta(minutes=5),
                           hvac_control_phase="preconditioning")
    validator = ConstraintValidator(DEFAULT_OPTIONS)
    violations = validator.validate_action(c, _plan(NOW, action), action, now=NOW, ownership=owned)
    assert "hvac_comfort_not_valid_for_suppression" not in violations
    unowned = validator.validate_action(c, _plan(NOW, action), action, now=NOW)
    assert "hvac_comfort_not_valid_for_suppression" in unowned
    # An active warm room is the feedback source, despite a cold head sensor.
    c.climate_inputs["zones"][MAIN]["temperature"] = 23.73
    action = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0]
    assert "hvac_comfort_not_valid_for_suppression" not in validator.validate_action(
        c, _plan(NOW, action), action, now=NOW, ownership=owned)


@pytest.mark.parametrize("response", ["delayed", "timeout", "failure"])
def test_slow_uncertain_and_failed_closure_never_resends(monkeypatch, response):
    c = room_context()
    hass = device(c)
    original_call = hass.services.async_call
    tasks = []
    async def call(domain, service, payload, **kwargs):
        if payload["entity_id"] == BED and service == "turn_off":
            if response == "failure":
                raise RuntimeError("failed device write")
            async def settle():
                await asyncio.sleep(.003)
                await original_call(domain, service, payload, **kwargs)
                # Real paired thermostats may lose both mode and target on off.
                hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})
            tasks.append(asyncio.create_task(settle()))
            if response == "timeout":
                raise TimeoutError
            return
        await original_call(domain, service, payload, **kwargs)
    hass.services.async_call = call
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .03)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", .001)
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    desired = {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    async def execute():
        result = await adapter.async_execute(_action(desired))
        await asyncio.gather(*tasks)
        return result
    result = asyncio.run(execute())
    assert result.applied is (response != "failure")
    assert sum(service == "turn_off" and payload["entity_id"] == BED
               for _, service, payload in hass.services.calls) <= 1


def test_reload_reconciles_uncertain_switch_then_restores_originals(monkeypatch):
    c = room_context()
    hass = device(c)
    hass.states.values[BED] = "off"
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    original = DaikinHVACAdapter(hass, data())
    _, zones = original.takeover_snapshot()
    zones[BED] = "on"
    record = {"domain": "switch", "entity_id": BED, "service": "turn_off", "requested": {},
              "deadline": (NOW + timedelta(seconds=180)).isoformat(), "context_id": "before-reload"}
    pending = []
    async def persist(value):
        pending.append(value)
    original.set_confirmation_persistence_callback(persist, record)
    result = asyncio.run(original.async_restore({}, zones, {"hvac_mode": "cool", "target_temperature": 24}))
    assert result.applied and hass.states.get(BED).state == "on"
    assert pending[0] is None


def test_paused_alias_reopen_uses_original_target_snapshot(monkeypatch):
    c = room_context(bed=21.4)
    c.created_at = NOW + timedelta(minutes=11)
    previous_context = room_context()
    previous = {"zone_intent": evaluate(previous_context, "cool", 21),
                "zone_states": {BED_CLIMATE: {"target_temperature": 23}}}
    c.hvac_control = previous
    hass = device(c)
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})
    original_call = hass.services.async_call
    async def call(domain, service, payload, **kwargs):
        await original_call(domain, service, payload, **kwargs)
        if payload["entity_id"] == BED and service == "turn_on":
            hass.states.values[BED_CLIMATE] = FakeState("cool", {"temperature": 23})
    hass.services.async_call = call
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: c.created_at)
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, previous)
    assert BED_CLIMATE not in adapter.deferred_zone_entities
    assert adapter.takeover_snapshot()[1][BED_CLIMATE] == {"target_temperature": 23}
    desired = {"mode": "cool", "hvac_mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    assert asyncio.run(adapter.async_execute(_action(desired))).applied
    assert hass.states.get(BED_CLIMATE).attributes["temperature"] == 21


def test_executor_persists_before_subset_write_and_recovers_failed_close(monkeypatch):
    c = room_context()
    c.hvac_control = ownership(c)
    hass = device(c)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    store = FakeStore()
    executor = Executor(store, hass=hass, entry_data=data(), options={**DEFAULT_OPTIONS, "dry_run": False,
                                                                   "climate_control_enabled": True})
    _arm_store(store, executor)
    action = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0]
    original_call = hass.services.async_call
    failed_closes = []
    async def call(domain, service, payload, **kwargs):
        if payload["entity_id"] == BED and service == "turn_off":
            control = store.data["ownership"]["hvac_control"]
            assert control["zone_intent"]["power"][BED] == "off"
            assert control["zone_states"][BED] == "on"
            assert store.flush_count > 0
            failed_closes.append(payload)
            return
        await original_call(domain, service, payload, **kwargs)
    hass.services.async_call = call
    # Plan evaluation without a context still executes all production gates;
    # constraint behavior is checked independently above.
    asyncio.run(executor.async_evaluate(_plan(NOW, action)))
    assert len(failed_closes) == 1, store.data.get("outcomes")
    assert hass.states.get("climate.head").attributes["temperature"] == 24
    assert not store.data["ownership"].get("hvac_control")


def test_manual_zone_alias_supersession_preserves_both_original_controls():
    from custom_components.ha_energy_planner.zone_control import superseded_entities
    assert superseded_entities(data(), {BED_CLIMATE}) == {BED, BED_CLIMATE}
    assert superseded_entities({}, {BED_CLIMATE}) == {BED_CLIMATE}
    store = FakeStore()
    store.data["ownership"] = {"hvac_control": {"zone_states": {
        MAIN: "on", BED: "off", BED_CLIMATE: {"target_temperature": 23},
    }}}
    executor = Executor(store, entry_data=data())
    executor.pending_hvac_desired_state = {}
    assert executor.mark_pending_hvac_zone_manual_override(BED_CLIMATE)
    assert set(executor.pending_hvac_desired_state["manual_zone_entity_ids"]) == {BED, BED_CLIMATE}
    asyncio.run(executor._async_persist_provisional_hvac_zone_supersession({BED_CLIMATE}))
    assert store.data["ownership"]["hvac_control"]["zone_states"] == {MAIN: "on"}


def test_invalid_pause_version_bounds_and_unsupported_target_checks():
    from custom_components.ha_energy_planner.constraints import _hvac_suppression_is_directionally_safe
    from custom_components.ha_energy_planner.zone_temperature import target_reached
    c = room_context()
    c.occupied_temperature_low_c = None
    assert evaluate(c, "cool", 21)["reason"] == "zone_bounds_unavailable"
    assert not comfort_breached(c, "cool", coasting=False)
    c = room_context()
    c.climate_inputs["zones"][MAIN]["temperature"] = None
    assert not _hvac_suppression_is_directionally_safe(c, "cool", 0)
    assert not target_reached(c, "cool", 21)
    c = room_context()
    intent = evaluate(c, "cool", 21)
    c.hvac_control["zone_intent"] = {**intent, "version": 99}
    assert evaluate(c, "cool", 21)["reason"] == "zone_intent_version_unknown"
    c.hvac_control["zone_intent"] = intent
    intent["zones"][BED]["paused_at"] = (NOW + timedelta(minutes=1)).isoformat()
    assert evaluate(c, "cool", 21)["reason"] == "zone_pause_time_invalid"
    c.climate_inputs = {}
    c.hvac_control = {}
    assert desired_fields(c, "cool", 21, "preconditioning") == {}


def test_adapter_rejects_stale_intent_and_loss_of_last_airflow_path(monkeypatch):
    c = room_context()
    hass = device(c)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    adapter = DaikinHVACAdapter(hass, data())
    blocked = {"zone_control_blocked": "minimum_airflow_unproven"}
    result = asyncio.run(adapter.async_execute(_action(blocked)))
    assert not result.applied and result.reason == "minimum_airflow_unproven"
    assert not hass.services.calls
    adapter._zone_desired = blocked
    with pytest.raises(adapter_module._HVACStateConfirmationError, match="fresh room"):
        asyncio.run(adapter._async_command(hass, "switch", "turn_off", {"entity_id": BED}))
    adapter._zone_desired = {}
    hass.states.values[MAIN] = "off"
    with pytest.raises(adapter_module._HVACStateConfirmationError, match="airflow"):
        asyncio.run(adapter._async_command(hass, "switch", "turn_off", {"entity_id": BED}))
    assert not hass.services.calls


@pytest.mark.parametrize("failure", ["stop", "open", "preopen", "exception", "unsafe_baseline"])
def test_failed_restoration_retains_all_baselines_and_never_guesses_airflow(monkeypatch, failure):
    c = room_context()
    hass = device(c)
    adapter = DaikinHVACAdapter(hass, data())
    zones = {MAIN: "on", BED: "on"}
    if failure == "stop":
        async def stop(*args):
            return False
        adapter._async_restore_main_state_preserving_manual = stop
    elif failure in {"open", "preopen"}:
        if failure == "preopen":
            hass.states.values["climate.head"] = FakeState("off", {"temperature": 24})
        async def open_zones(*args):
            return False, zones
        adapter._async_restore_zone_states = open_zones
    elif failure == "exception":
        hass.states.values["climate.head"] = FakeState("off", {"temperature": 24})
        async def open_zones(*args):
            raise RuntimeError("persistence failed")
        adapter._async_restore_zone_states = open_zones
    else:
        zones = {MAIN: "off", BED: "off"}
    result = asyncio.run(adapter._async_restore_main_and_zones(
        "climate.head", {"hvac_mode": "cool", "target_temperature": 24}, zones))
    assert result == (False, False, zones)
    if failure in {"unsafe_baseline", "preopen"}:
        assert hass.states.get("climate.head").state == "off"


@pytest.mark.parametrize("failed_stop", [False, True])
def test_restoration_confirms_head_off_before_closing_original_last_path(monkeypatch, failed_stop):
    c = room_context()
    hass = device(c)
    original_call = hass.services.async_call
    async def call(domain, service, payload, **kwargs):
        observed = hass.states.get(payload["entity_id"])
        await original_call(domain, service, payload, **kwargs)
        if payload["entity_id"] == "climate.head" and service == "turn_off":
            hass.states.values["climate.head"] = FakeState("off", dict(observed.attributes))
    hass.services.async_call = call
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .02)
    adapter = DaikinHVACAdapter(hass, data())
    main = {"hvac_mode": "off", "target_temperature": 24, "rollback_active_hvac_mode": "cool",
            "rollback_hvac_mode_changed": True}
    zones = {MAIN: "off", BED: "off", MAIN_CLIMATE: {"target_temperature": 23},
             BED_CLIMATE: {"target_temperature": 23}}
    original = adapter._async_restore_main_state_preserving_manual
    calls = []
    async def restore(*args):
        calls.append(args)
        return False if failed_stop and len(calls) == 2 else await original(*args)
    adapter._async_restore_main_state_preserving_manual = restore
    result = asyncio.run(adapter.async_restore({}, zones, main))
    assert result.applied is not failed_stop
    assert hass.states.get("climate.head").state == "off"
    if failed_stop:
        assert result.saved_zone_states == {MAIN: "off", BED: "off"}
    else:
        assert all(hass.states.get(entity).state == "off" for entity in PAIRS)
        last_close = max(i for i, (_, service, payload) in enumerate(hass.services.calls)
                         if service == "turn_off" and payload["entity_id"] in PAIRS)
        assert any(i < last_close and service == "turn_off" and payload["entity_id"] == "climate.head"
                   for i, (_, service, payload) in enumerate(hass.services.calls))


@pytest.mark.parametrize("failure", ["service", "power_readback"])
def test_subset_main_failure_hands_back_without_reopening_cold_room(monkeypatch, failure):
    c = room_context()
    hass = device(c)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    original_call = hass.services.async_call
    async def call(domain, service, payload, **kwargs):
        if payload["entity_id"] == "climate.head" and service == "set_temperature":
            if failure == "service":
                raise RuntimeError("head rejected")
            hass.states.values[BED] = "on"
        await original_call(domain, service, payload, **kwargs)
    hass.services.async_call = call
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    desired = {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    result = asyncio.run(adapter.async_execute(_action(desired)))
    assert not result.applied and result.rollback_succeeded is False
    assert result.reason == ("zone_subset_service_failed" if failure == "service"
                             else "zone_subset_confirmation_failed")
    assert not any(service == "turn_on" and payload["entity_id"] == BED
                   for _, service, payload in hass.services.calls)


def test_confirmed_closure_starts_dwell_after_slow_command_and_keeps_it_on_refresh():
    c = room_context()
    desired = {"phase": "preconditioning", **desired_fields(c, "cool", 21, "preconditioning")}
    before = {"hvac_control": {"main_state": {"hvac_mode": "cool", "target_temperature": 24}}}
    transaction = HVACOwnershipTransaction(before, NOW)
    prepared = transaction.prepare(desired, {}, {}, {})
    confirmed = NOW + timedelta(minutes=3)
    saved = transaction.complete(HVACCommandResult(True, "applied", {}, {}, {}), prepared, desired,
                                 main_state_superseded=False, superseded_zone_entity_ids=set(), confirmed_at=confirmed)
    assert saved["hvac_control"]["zone_intent"]["zones"][BED]["paused_at"] == confirmed.isoformat()
    c.created_at = NOW + timedelta(minutes=11)
    c.hvac_control = saved["hvac_control"]
    c.climate_inputs["zones"][BED]["temperature"] = 21.4
    assert evaluate(c, "cool", 21)["zones"][BED]["restart_blocker"] == "pause_dwell"


def test_manual_zone_remains_untouched_during_remaining_restoration():
    c = room_context()
    hass = device(c)
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": 26})
    adapter = DaikinHVACAdapter(hass, data())
    result = asyncio.run(adapter.async_restore({}, {MAIN: "on", MAIN_CLIMATE: {"target_temperature": 23}},
                                               {"hvac_mode": "cool", "target_temperature": 24}))
    assert result.applied
    assert hass.states.get(BED).state == "off"
    assert hass.states.get(BED_CLIMATE).attributes["temperature"] == 26
    assert all(payload.get("entity_id") not in {BED, BED_CLIMATE} for _, _, payload in hass.services.calls)


def test_topology_never_pairs_the_head_as_a_room_and_rejects_malformed_zone_lists():
    config = data()
    config["daikin_climate_entity"] = MAIN_CLIMATE
    assert topology(config)["reason"] == "invalid_zone_topology"
    for configured in (None, [MAIN, {}]):
        config = {**data(), "climate_zone_entities": configured}
        assert topology(config)["reason"] == "invalid_zone_topology"


def test_every_intermediate_airflow_combination_is_verified_while_head_demands():
    c = room_context()
    hass = device(c)
    config = data()
    config["hvac_zone_control"] = {"pairs": {**PAIRS, "switch.third": "climate.third"},
                                  "safe_open_sets": [[MAIN, BED, "switch.third"], [MAIN]]}
    config["climate_zone_entities"] += ["switch.third", "climate.third"]
    hass.states.values[BED] = "off"
    hass.states.values["switch.third"] = "off"
    adapter = DaikinHVACAdapter(hass, config)
    with pytest.raises(adapter_module._HVACStateConfirmationError, match="airflow"):
        asyncio.run(adapter._async_command(hass, "switch", "turn_on", {"entity_id": BED}))
    assert not hass.services.calls
    # Opening paths with the head confirmed off cannot starve a compressor.
    hass.states.values["climate.head"] = FakeState("off", {"temperature": 24})
    asyncio.run(adapter._async_command(hass, "switch", "turn_on", {"entity_id": BED}))
    assert hass.states.get(BED).state == "on"


def test_late_sensor_change_after_durable_intent_retires_unsent_close(monkeypatch):
    c = room_context()
    hass = device(c)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_zone_control_context(DEFAULT_OPTIONS, {})
    persisted = []
    async def persist(record):
        persisted.append(record)
        if record and record["entity_id"] == BED:
            hass.states.values["sensor.bed"].state = "23"
    adapter.set_confirmation_persistence_callback(persist)
    desired = {"hvac_mode": "cool", "mode": "cool", "precondition_target": 21, "target_temperature": 21,
               "enable_zones": True, **desired_fields(c, "cool", 21, "preconditioning")}
    result = asyncio.run(adapter.async_execute(_action(desired)))
    assert not result.applied and result.reason == "zone_subset_confirmation_failed"
    assert persisted[-1] is None
    assert not any(payload["entity_id"] == BED for _, _, payload in hass.services.calls)


def test_serialized_transition_time_preserves_qualified_source_identity():
    c = room_context()
    hass = device(c)
    c.hvac_control["zone_transition_at"] = NOW
    before = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    c.hvac_control["zone_transition_at"] = NOW.isoformat()
    after = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    assert before["sources"][MAIN]["identity"] == after["sources"][MAIN]["identity"]


def test_explicit_alias_off_retires_stale_head_activation_confirmation():
    c = room_context()
    hass = device(c)
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 21})
    hass.states.values[BED_CLIMATE] = FakeState("cool", {"temperature": 21})
    command = {"entity_id": "climate.head", "service": "set_temperature", "context_id": "target",
               "issued_at": NOW, "confirmed_at": NOW, "deadline": NOW + timedelta(seconds=180),
               "requested": {"temperature": 21}, "requested_hvac_mode": "cool", "transaction_id": "old",
               "coupled_zones": {BED_CLIMATE: {"mode": "cool", "target_temperature": 23}},
               "mode_commands": [
                   {"entity_id": "climate.head", "service": "turn_on", "context_id": "on",
                    "issued_at": NOW, "confirmed_at": NOW, "transaction_id": "old"},
                   {"entity_id": BED, "service": "turn_off", "context_id": "off",
                    "issued_at": NOW + timedelta(seconds=10), "transaction_id": "new"},
               ]}
    event = SimpleNamespace(data={"entity_id": BED_CLIMATE, "old_state": FakeState("off", {"temperature": None}),
                                  "new_state": hass.states.get(BED_CLIMATE)}, context=Context())
    assert not _matches_climate_transaction(data(), [command], event, NOW + timedelta(seconds=130), hass)


def test_lost_room_evidence_cannot_block_original_head_shutdown():
    c = room_context()
    hass = device(c)
    adapter = DaikinHVACAdapter(hass, data())
    adapter._zone_desired = {"zone_control_blocked": "zone_feedback_unavailable"}
    result = asyncio.run(adapter.async_restore(saved_main_state={"hvac_mode": "off"}))
    assert result.applied and hass.states.get("climate.head").state == "off"


@pytest.mark.parametrize("saved_switch", [True, False])
def test_manual_alias_release_durably_retires_pair_before_any_write(saved_switch):
    c = room_context()
    hass = device(c)
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 21})
    hass.states.values[BED_CLIMATE] = FakeState("cool", {"temperature": 26})

    class DurableStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.flushed = []

        async def async_flush(self):
            await super().async_flush()
            self.flushed.append(deepcopy(self.data["ownership"]))

    store = DurableStore()
    store.data["ownership"] = {"hvac_control": {
        "main_state": {"hvac_mode": "cool", "target_temperature": 24},
        "zone_states": {MAIN: "on", **({BED: "off"} if saved_switch else {})},
        "command_confirmation": {"entity_id": BED},
    }}
    original_call = hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        durable = store.flushed[0]["hvac_control"]
        assert BED not in durable["zone_states"]
        assert BED_CLIMATE not in durable["zone_states"]
        assert "command_confirmation" not in durable
        await original_call(domain, service, payload, **kwargs)

    hass.services.async_call = call
    executor = Executor(store, hass=hass, entry_data=data())
    asyncio.run(executor.async_release_hvac_control("manual", preserve_zone_entity_id=BED_CLIMATE))
    assert hass.states.get("climate.head").attributes["temperature"] == 24
    assert hass.states.get(BED_CLIMATE).attributes["temperature"] == 26
    assert all(payload.get("entity_id") not in {BED, BED_CLIMATE} for _, _, payload in hass.services.calls)


def test_failed_hidden_target_retains_paired_power_for_fresh_adapter_retry(monkeypatch):
    c = room_context()
    hass = device(c)
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 21})
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    original_call = hass.services.async_call
    failures = []

    async def call(domain, service, payload, **kwargs):
        if payload["entity_id"] == BED_CLIMATE and service == "set_temperature" and not failures:
            failures.append(payload)
            raise RuntimeError("temporary target rejection")
        await original_call(domain, service, payload, **kwargs)
        if payload["entity_id"] == BED:
            if service == "turn_on":
                hass.states.values[BED_CLIMATE] = FakeState("cool", {"temperature": 21})
            elif service == "turn_off":
                hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})

    hass.services.async_call = call
    zones = {MAIN: "on", BED: "off", MAIN_CLIMATE: {"target_temperature": 23},
             BED_CLIMATE: {"target_temperature": 23}}
    main = {"hvac_mode": "cool", "target_temperature": 24}
    first = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore({}, zones, main))
    assert not first.applied
    assert first.saved_zone_states == {BED_CLIMATE: {"target_temperature": 23}, BED: "off"}
    assert hass.states.get(BED).state == "off"
    second = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore(
        {}, first.saved_zone_states, first.saved_main_state))
    assert second.applied and second.saved_zone_states == {}
    assert hass.states.get(BED).state == "off"
    assert hass.states.get("climate.head").attributes["temperature"] == 24
    writes = [(service, payload["entity_id"]) for _, service, payload in hass.services.calls]
    assert writes.index(("set_temperature", "climate.head")) < writes.index(("turn_on", BED))
    assert not any(entity == "climate.head" and service in {"turn_off", "turn_on", "set_hvac_mode"}
                   for service, entity in writes)


def test_already_restored_running_head_does_not_cycle_or_write():
    c = room_context()
    hass = device(c)
    adapter = DaikinHVACAdapter(hass, data())
    _, zones = adapter.takeover_snapshot()
    main = adapter.main_takeover_snapshot()
    result = asyncio.run(adapter.async_restore({}, zones, main))
    assert result.applied
    assert not hass.services.calls


@pytest.mark.parametrize("failed_stop", [False, True])
def test_unsafe_current_airflow_requires_confirmed_stop_before_recovery(failed_stop):
    c = room_context()
    hass = device(c)
    hass.states.values[MAIN] = "off"
    hass.states.values[BED] = "off"
    adapter = DaikinHVACAdapter(hass, data())
    if failed_stop:
        async def reject_stop(*args):
            return False
        adapter._async_restore_main_state_preserving_manual = reject_stop
    zones = {MAIN: "on", BED: "on"}
    main_ok, zones_ok, unresolved = asyncio.run(adapter._async_restore_main_and_zones(
        "climate.head", {"hvac_mode": "cool", "target_temperature": 24}, zones))
    if failed_stop:
        assert not main_ok and not zones_ok and unresolved == zones
        assert not hass.services.calls
    else:
        assert main_ok and zones_ok and not unresolved
        writes = [(service, payload["entity_id"]) for _, service, payload in hass.services.calls]
        assert writes.index(("turn_off", "climate.head")) < writes.index(("turn_on", MAIN))
        assert writes.index(("turn_on", BED)) < writes.index(("turn_on", "climate.head"))


@pytest.mark.parametrize("policy", ["automatic", "observe"])
@pytest.mark.parametrize("owned", [True, False])
def test_subset_economic_guard_preserves_release_and_treats_hold_as_unowned(policy, owned):
    c = room_context()
    c.hvac_control = ownership(c)
    actions = HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    assert any(action.kind == "set_hvac" for action in actions)
    assert any(action.kind == "release_hvac" for action in actions)
    c.climate_engine = {"ever_active": True}
    if not owned:
        c.hvac_control = {"released_until": NOW - timedelta(minutes=1)}
    result = economic_actions(c, {**DEFAULT_OPTIONS, CONF_HVAC_POLICY: policy}, actions)
    assert result == (actions if owned else [action for action in actions if action.kind == "release_hvac"])
    c.climate_engine = {}
    assert economic_actions(c, {**DEFAULT_OPTIONS, CONF_HVAC_POLICY: policy}, actions) == actions


@pytest.mark.parametrize("policy", ["automatic", "observe", "legacy"])
@pytest.mark.parametrize("ever_active", [False, True])
def test_away_off_ownership_cannot_authorize_new_subset_fallback_after_activation(policy, ever_active):
    c = room_context()
    c.occupancy_state = OccupancyState.AWAY
    c.hvac_control = {"phase": "away_off", "started_at": NOW - timedelta(minutes=30)}
    c.climate_engine = {"ever_active": ever_active, "status": "degraded" if ever_active else "learning"}
    c.climate_inputs["power_source_type"] = "measured"
    options = {**DEFAULT_OPTIONS, CONF_HVAC_POLICY: policy, "hvac_precondition_while_away": True}
    actions = HVACPlanningPolicy(options, {})._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    assert any(action.desired_state.get("phase") == "preconditioning" for action in actions)
    releases = [action for action in actions if action.kind == "release_hvac"]
    assert releases
    assert economic_actions(c, options, actions) == (
        releases if ever_active and policy != "legacy" else actions
    )


def test_queued_preconfirmation_intent_preserves_confirmed_pause_timestamp():
    c = room_context()
    desired = {"phase": "preconditioning", **desired_fields(c, "cool", 21, "preconditioning")}
    confirmed = NOW + timedelta(minutes=3)
    previous = deepcopy(desired["zone_intent"])
    previous["zones"][BED]["paused_at"] = confirmed.isoformat()
    previous["zones"][BED]["restart_not_before"] = (confirmed + timedelta(minutes=10)).isoformat()
    transaction = HVACOwnershipTransaction({"hvac_control": {"zone_intent": previous}},
                                           NOW + timedelta(minutes=4))
    prepared = transaction.prepare(desired, {}, {}, {})
    assert prepared["hvac_control"]["zone_intent"]["zones"][BED]["paused_at"] == confirmed.isoformat()
    assert desired["zone_intent"]["zones"][BED]["paused_at"] == NOW.isoformat()
    saved = transaction.complete(HVACCommandResult(True, "applied", {}, {}, {}), prepared, desired,
                                 main_state_superseded=False, superseded_zone_entity_ids=set(),
                                 confirmed_at=NOW + timedelta(minutes=4))
    assert saved["hvac_control"]["zone_intent"]["zones"][BED]["paused_at"] == confirmed.isoformat()
    c.hvac_control = saved["hvac_control"]
    c.climate_inputs["zones"][BED]["temperature"] = 21.4
    c.created_at = NOW + timedelta(minutes=11)
    assert evaluate(c, "cool", 21)["zones"][BED]["restart_blocker"] == "pause_dwell"
    c.created_at = NOW + timedelta(minutes=13)
    assert evaluate(c, "cool", 21)["power"][BED] == "on"


@pytest.mark.parametrize("missing", [None, "unknown", "unavailable"])
@pytest.mark.parametrize("when", ["before", "persist"])
@pytest.mark.parametrize(("domain", "service", "payload"), [
    ("climate", "set_temperature", {"entity_id": "climate.head", "temperature": 21}),
    ("climate", "turn_on", {"entity_id": "climate.head"}),
    ("climate", "set_hvac_mode", {"entity_id": "climate.head", "hvac_mode": "cool"}),
    ("switch", "turn_off", {"entity_id": BED}),
])
def test_unknown_damper_is_never_counted_as_closed_airflow(missing, when, domain, service, payload):
    hass = device(room_context())
    config = data()
    config["hvac_zone_control"] = {"pairs": {**PAIRS, "switch.third": "climate.third"},
                                  "safe_open_sets": [[MAIN, BED, "switch.third"], [MAIN]]}
    config["climate_zone_entities"] += ["switch.third", "climate.third"]
    hass.states.values[BED] = missing if when == "before" else "off"
    hass.states.values["switch.third"] = "off"
    adapter = DaikinHVACAdapter(hass, config)
    persisted = []

    async def persist(record):
        persisted.append(record)
        if when == "persist" and record:
            hass.states.values[BED] = missing

    adapter.set_confirmation_persistence_callback(persist)
    # Main alone is safe, but Main plus the unobserved Bedroom is unverified.
    with pytest.raises(adapter_module._HVACStateConfirmationError, match="power feedback"):
        asyncio.run(adapter._async_command(hass, domain, service, payload))
    assert not hass.services.calls
    if when == "persist":
        assert persisted[-1] is None


@pytest.mark.parametrize("owned", [False, True])
def test_restoration_stops_head_for_unknown_zone_power_and_preserves_exclusions(owned):
    hass = device(room_context())
    hass.states.values[BED] = "unavailable"
    zones = {MAIN: "on", **({BED: "off"} if owned else {})}
    result = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore(
        {}, zones, {"hvac_mode": "cool", "target_temperature": 24}))
    writes = [(service, payload["entity_id"]) for _, service, payload in hass.services.calls]
    assert writes[0] == ("turn_off", "climate.head")
    assert not result.applied and result.saved_zone_states == zones
    assert hass.states.get("climate.head").state == "off"
    assert all(entity != BED for _, entity in writes)
    if owned:
        # Recovery waits for available feedback before opening the actuator.
        hass.states.values[BED] = "off"
        result = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore(
            {}, result.saved_zone_states, result.saved_main_state))
        assert result.applied
        writes = [(service, payload["entity_id"]) for _, service, payload in hass.services.calls]
        assert writes.index(("turn_on", BED)) < writes.index(("turn_on", "climate.head"))
        assert hass.states.get(BED).state == "off"
        assert hass.states.get("climate.head").state == "cool"


@pytest.mark.parametrize(("manual", "when"), [
    ("main", "before"), ("sibling", "before"),
    ("main", "persist"), ("sibling", "persist"), ("exact", "persist"),
    ("main", "readback"), ("sibling", "readback"),
])
def test_independent_switch_recovery_preserves_override_scope_during_persist_and_readback(monkeypatch, manual, when):
    hass = device(room_context())
    hass.states.values[MAIN] = "off"
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 26})
    hass.states.values[BED_CLIMATE] = FakeState("cool", {"temperature": 26})
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .02)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", .001)
    flags = {"main": False, "zones": set()}

    def override():
        flags["main"] = manual == "main"
        flags["zones"] = ({BED, BED_CLIMATE} if manual == "sibling"
                          else {MAIN, MAIN_CLIMATE} if manual == "exact" else set())

    if when == "before":
        override()
    adapter = DaikinHVACAdapter(hass, data())
    adapter.set_manual_override_check(lambda: flags["main"])
    adapter.set_zone_manual_override_check(lambda: flags["zones"])
    persisted = []

    async def persist(record):
        persisted.append(record)
        if when == "persist" and record and record["entity_id"] == MAIN:
            override()

    adapter.set_confirmation_persistence_callback(persist)
    original_call = hass.services.async_call
    tasks = []

    async def call(domain, service, payload, **kwargs):
        if when == "readback" and payload["entity_id"] == MAIN:
            override()

            async def settle():
                await asyncio.sleep(.003)
                await original_call(domain, service, payload, **kwargs)

            tasks.append(asyncio.create_task(settle()))
        else:
            await original_call(domain, service, payload, **kwargs)

    hass.services.async_call = call

    async def restore():
        result = await adapter.async_restore(saved_zone_states={MAIN: "on"})
        await asyncio.gather(*tasks)
        return result

    result = asyncio.run(restore())
    assert result.applied and not result.saved_zone_states
    assert hass.states.get("climate.head").attributes["temperature"] == 26
    assert hass.states.get(BED_CLIMATE).attributes["temperature"] == 26
    if manual == "exact":
        assert hass.states.get(MAIN).state == "off"
        assert not hass.services.calls and persisted[-1] is None
    else:
        assert hass.states.get(MAIN).state == "on"
        assert [(service, payload["entity_id"]) for _, service, payload in hass.services.calls] == [("turn_on", MAIN)]


@pytest.mark.parametrize("power", ["off", "on", "unavailable"])
@pytest.mark.parametrize("retain_main", [True, False])
def test_deferred_off_head_retry_retains_paired_power_until_hidden_target_recovers(monkeypatch, power, retain_main):
    hass = device(room_context())
    hass.states.values["climate.head"] = FakeState("off", {"temperature": 24})
    hass.states.values[BED] = power
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", .001)
    original_call = hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        await original_call(domain, service, payload, **kwargs)
        if payload["entity_id"] == BED:
            hass.states.values[BED_CLIMATE] = FakeState(
                "cool" if service == "turn_on" else "off",
                {"temperature": 21 if service == "turn_on" else None})
        if payload["entity_id"] == "climate.head" and service == "turn_off":
            hass.states.values["climate.head"] = FakeState("off", {"temperature": 24})

    hass.services.async_call = call
    main = {"hvac_mode": "off", "target_temperature": 24, "rollback_active_hvac_mode": "cool",
            "rollback_hvac_mode_changed": True, "zone_recovery_attempted": True, "recovery_version": 1}
    zones = {BED_CLIMATE: {"target_temperature": 23}, BED: "off"}
    for _ in range(2):
        pending = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore({}, zones, main))
        assert not pending.applied and pending.saved_zone_states == zones
        assert pending.saved_main_state == main
        assert hass.states.get("climate.head").state == "off"
        assert all(payload["entity_id"] == BED for _, _, payload in hass.services.calls)
    assert len(hass.services.calls) == (1 if power == "on" else 0)
    hass.services.calls.clear()
    # A normal-operation wake makes recovery actionable; it must still own
    # the power needed to expose the closed thermostat, even after reloads.
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 24})
    hass.states.values[BED] = "off"
    final = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore(
        {}, pending.saved_zone_states, pending.saved_main_state if retain_main else {}))
    assert final.applied and not final.saved_zone_states and not final.saved_main_state
    writes = [(service, payload) for _, service, payload in hass.services.calls]
    assert any(service == "turn_on" and payload["entity_id"] == BED for service, payload in writes)
    assert any(service == "set_temperature" and payload == {"entity_id": BED_CLIMATE, "temperature": 23}
               for service, payload in writes)
    assert hass.states.get("climate.head").state == ("off" if retain_main else "cool")
    assert hass.states.get(BED).state == "off"
    if not retain_main:
        assert hass.states.get("climate.head").attributes["temperature"] == 24
        assert all(payload["entity_id"] != "climate.head" for _, payload in writes)


@pytest.mark.parametrize("mode", ["off", "cool"])
def test_deferred_target_without_paired_power_baseline_does_not_acquire_the_actuator(mode):
    hass = device(room_context())
    hass.states.values["climate.head"] = FakeState(mode, {"temperature": 24})
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None})
    main = {}
    zones = {BED_CLIMATE: {"target_temperature": 23}}
    pending = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore({}, zones, main))
    assert not pending.applied and pending.saved_zone_states == zones
    assert pending.saved_main_state == main
    assert not hass.services.calls


@pytest.mark.parametrize("bounds", [
    {"min_temp": 19, "max_temp": 23}, {"min_temp": 25, "max_temp": 19},
    {"min_temp": None, "max_temp": 23}, {"min_temp": "bad", "max_temp": 23},
])
def test_user_head_recovery_waits_for_compatible_bounds_without_zone_or_guard_churn(bounds):
    hass = device(room_context())
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 21})
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None, **bounds})
    config = {**data(), "climate_change_from_scheduler_entity": "input_boolean.guard",
              "climate_scheduler_guard_timer_entity": "timer.guard"}
    hass.states.values["input_boolean.guard"] = "off"
    hass.states.values["timer.guard"] = "idle"
    zones = {BED: "off", BED_CLIMATE: {"target_temperature": 26}}
    for _ in range(2):
        result = asyncio.run(DaikinHVACAdapter(hass, config).async_restore({}, zones, {}))
        assert not result.applied and result.saved_zone_states == zones and not result.saved_main_state
    assert not hass.services.calls
    assert hass.states.get(BED).state == "off"
    assert hass.states.get("climate.head").attributes["temperature"] == 21
    # A later user setting publishes compatible bounds. Recovery may now
    # expose the saved target without changing that new head setting.
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 24})
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None, "min_temp": 22, "max_temp": 26})
    original_call = hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        await original_call(domain, service, payload, **kwargs)
        if payload["entity_id"] == BED:
            hass.states.values[BED_CLIMATE] = FakeState(
                "cool" if service == "turn_on" else "off",
                {"temperature": 24 if service == "turn_on" else None, "min_temp": 22, "max_temp": 26})

    hass.services.async_call = call
    result = asyncio.run(DaikinHVACAdapter(hass, config).async_restore({}, result.saved_zone_states, {}))
    assert result.applied and not result.saved_zone_states
    assert hass.states.get(BED).state == "off"
    assert hass.states.get("climate.head").attributes["temperature"] == 24
    assert all(payload["entity_id"] != "climate.head" for _, _, payload in hass.services.calls)
    assert any(service == "set_temperature" and payload == {"entity_id": BED_CLIMATE, "temperature": 26}
               for _, service, payload in hass.services.calls)


def test_actionable_sibling_does_not_open_a_pair_with_incompatible_saved_target(monkeypatch):
    hass = device(room_context())
    hass.states.values["climate.head"] = FakeState("cool", {"temperature": 21})
    hass.states.values[MAIN_CLIMATE] = FakeState("cool", {"temperature": 21, "min_temp": 19, "max_temp": 23})
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": None, "min_temp": 19, "max_temp": 23})
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", .005)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", .001)
    zones = {MAIN: "on", MAIN_CLIMATE: {"target_temperature": 23},
             BED: "off", BED_CLIMATE: {"target_temperature": 26}}
    result = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore({}, zones, {}))
    assert not result.applied and result.saved_zone_states == {BED: "off", BED_CLIMATE: zones[BED_CLIMATE]}
    assert hass.services.calls == [("climate", "set_temperature", {"entity_id": MAIN_CLIMATE, "temperature": 23})]
    hass.services.calls.clear()
    result = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore({}, result.saved_zone_states, {}))
    assert not result.applied and not hass.services.calls
    assert hass.states.get(BED).state == "off"
    assert hass.states.get("climate.head").attributes["temperature"] == 21


def test_already_restored_closed_target_does_not_reopen_under_a_user_head():
    hass = device(room_context())
    hass.states.values[BED] = "off"
    hass.states.values[BED_CLIMATE] = FakeState("off", {"temperature": 23, "min_temp": 19, "max_temp": 23})
    zones = {BED: "off", BED_CLIMATE: {"target_temperature": 23}}
    result = asyncio.run(DaikinHVACAdapter(hass, data()).async_restore({}, zones, {}))
    assert result.applied and not result.saved_zone_states
    assert not hass.services.calls
