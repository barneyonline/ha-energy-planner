"""Mode-specific AirBase memories and reversible Off acquisition."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from homeassistant.helpers import entity_registry
from test_hvac_adapter import FakeState, _action
from test_zone_control import BED, BED_CLIMATE, MAIN, MAIN_CLIMATE, PAIRS, data, device, room_context
from test_zone_temperature import NOW

from custom_components.ha_energy_planner import hvac_adapter as adapter_module
from custom_components.ha_energy_planner.daikin_memory import MEMORY_KEY, OFF_START_OPTION, provider, read_memory
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction
from custom_components.ha_energy_planner.zone_control import desired_fields


class Values:
    def __init__(self):
        self.control = {"pow": "0", "mode": "1", "stemp": "19", "dt1": "19", "dt2": "23"}
        self.model = {"heat_l": "16", "heat_h": "32", "cool_l": "16", "cool_h": "32", "en_zone": "2"}
        self.zones = {"zone_onoff": "1;0", "lztemp_h": "20;20", "lztemp_c": "24;24"}

    def values_for_resource(self, resource):
        return (
            self.control
            if resource.endswith("get_control_info")
            else self.model
            if resource.endswith("get_model_info")
            else self.zones
        )


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", 0.001)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: NOW)
    c = room_context(main=25, bed=25)
    c.current_hvac_mode = "off"
    hass = device(c)
    values = Values()
    reads = []

    async def update(resources, **kwargs):
        reads.append((resources, kwargs))

    cls = type("DaikinAirBase", (), {"__module__": "pydaikin.daikin_airbase"})
    airbase = cls()
    airbase.mac, airbase.values, airbase.update_status = "controller", values, update
    coordinator = SimpleNamespace(device=airbase, last_update_success=True)
    entry = SimpleNamespace(runtime_data=coordinator)
    hass.config_entries = SimpleNamespace(async_get_entry=lambda key: entry)
    for index, entity in enumerate(PAIRS.values()):
        hass.zone_registry_entries[entity].unique_id = f"controller-zone{index}-temperature"
    for index, entity in enumerate(PAIRS):
        hass.zone_registry_entries[entity].unique_id = f"controller-zone{index}"
    monkeypatch.setattr(
        entity_registry,
        "async_get",
        lambda _: SimpleNamespace(async_get=lambda entity: hass.zone_registry_entries.get(entity)),
    )
    hass.states.values["climate.head"] = FakeState(
        "off",
        {
            "temperature": 19,
            "min_temp": 16,
            "max_temp": 32,
            "target_temp_step": 1,
            "hvac_modes": ["off", "heat", "cool"],
        },
    )
    hass.states.values[MAIN], hass.states.values[BED] = "on", "off"
    for entity in PAIRS.values():
        hass.states.values[entity] = FakeState("off", {"temperature": None, "min_temp": 17, "max_temp": 21})
    modes = {
        "heat": {"head": 19, "zones": {MAIN_CLIMATE: 20, BED_CLIMATE: 20}},
        "cool": {"head": 23, "zones": {MAIN_CLIMATE: 24, BED_CLIMATE: 24}},
    }
    selected = ["heat"]
    original = hass.services.async_call

    def publish():
        head = hass.states.get("climate.head")
        mode = selected[0]
        head.attributes.update(
            temperature=modes[mode]["head"],
            min_temp=16,
            max_temp=32,
            target_temp_step=1,
            hvac_modes=["off", "heat", "cool", "dry"],
        )
        hass.states.values["climate.head"] = head
        for actuator, entity in PAIRS.items():
            enabled = head.state != "off" and hass.states.get(actuator).state == "on"
            hass.states.values[entity] = FakeState(
                mode if enabled else "off",
                {
                    "temperature": modes[mode]["zones"].get(entity) if head.state != "off" else None,
                    "min_temp": modes[mode]["head"] - 2,
                    "max_temp": modes[mode]["head"] + 2,
                    "target_temp_step": 1,
                },
            )
        values.control.update(
            pow="0" if head.state == "off" else "1",
            mode={"heat": "1", "cool": "2", "dry": "7"}[mode],
            stemp=str(modes[mode]["head"]),
            dt1=str(modes["heat"]["head"]),
            dt2=str(modes["cool"]["head"]),
        )
        tails = {key: unquote(value).split(";")[len(PAIRS) :] for key, value in values.zones.items()}
        values.zones.update(
            zone_onoff=";".join(
                [*("1" if hass.states.get(a).state == "on" else "0" for a in PAIRS), *tails["zone_onoff"]]
            ),
            lztemp_h=";".join([*(str(modes["heat"]["zones"][e]) for e in PAIRS.values()), *tails["lztemp_h"]]),
            lztemp_c=";".join([*(str(modes["cool"]["zones"][e]) for e in PAIRS.values()), *tails["lztemp_c"]]),
        )

    async def service(domain, name, payload, **kwargs):
        entity = payload["entity_id"]
        if name == "set_temperature":
            current = hass.states.get(entity)
            assert current.attributes["min_temp"] <= payload["temperature"] <= current.attributes["max_temp"]
        await original(domain, name, payload, **kwargs)
        if entity == "climate.head":
            if name == "turn_on":
                hass.states.values[entity] = FakeState(selected[0])
            elif name == "set_hvac_mode":
                if payload["hvac_mode"] == "dry":
                    modes["dry"]["head"] = hass.states.get("climate.head").attributes["temperature"]
                selected[0] = payload["hvac_mode"]
            elif name == "set_temperature":
                modes[selected[0]]["head"] = payload["temperature"]
        elif entity in PAIRS and name == "turn_on" and hass.states.get("climate.head").state == "off":
            hass.states.values["climate.head"].state = selected[0]
        elif entity in PAIRS.values() and name == "set_temperature":
            modes[selected[0]]["zones"][entity] = payload["temperature"]
        publish()

    hass.services.async_call = service
    publish()
    desired = {
        "mode": "cool",
        "hvac_mode": "cool",
        "precondition_target": 21,
        "target_temperature": 21,
        "enable_zones": True,
        **desired_fields(c, "cool", 21, "preconditioning"),
    }
    config = data()
    adapter = DaikinHVACAdapter(hass, config)
    adapter.set_zone_control_context({OFF_START_OPTION: True}, {})
    return SimpleNamespace(
        hass=hass,
        c=c,
        data=config,
        adapter=adapter,
        desired=desired,
        values=values,
        modes=modes,
        reads=reads,
        entry=entry,
        publish=publish,
        selected=selected,
    )


def test_read_only_complete_mode_memory_with_hidden_off_targets(system):
    s = system
    snapshot = asyncio.run(read_memory(s.hass, s.data, "cool"))
    assert snapshot["selected_mode"] == "heat"
    assert snapshot["modes"]["cool"]["zones"][MAIN_CLIMATE] == 24
    assert s.reads and not s.hass.services.calls
    assert s.hass.states.get("climate.head").state == "off"


@pytest.mark.parametrize(
    "damage",
    [
        "missing_heat",
        "missing_cool",
        "nonfinite",
        "range",
        "bad_power",
        "short",
        "auto",
        "powered",
        "mismatch",
        "wrong_power",
        "head_missing",
        "error",
    ],
)
def test_incomplete_reads_fail_closed_without_waking(system, damage):
    s = system
    if damage == "missing_heat":
        s.values.zones.pop("lztemp_h")
    elif damage == "missing_cool":
        s.values.control.pop("dt2")
    elif damage == "nonfinite":
        s.values.control["dt2"] = "nan"
    elif damage == "range":
        s.values.zones["lztemp_c"] = "24;30"
    elif damage == "bad_power":
        s.values.zones["zone_onoff"] = "1;?"
    elif damage == "short":
        s.values.zones["lztemp_c"] = "24"
    elif damage == "auto":
        s.values.control["mode"] = "3"
    elif damage == "powered":
        s.values.control["pow"] = "1"
    elif damage == "mismatch":
        s.values.control["stemp"] = "23"
    elif damage == "wrong_power":
        s.values.zones["zone_onoff"] = "0;0"
    elif damage == "head_missing":
        del s.hass.states.values["climate.head"]
    elif damage == "error":

        async def fail(*args, **kwargs):
            raise TimeoutError

        s.entry.runtime_data.device.update_status = fail
    assert asyncio.run(read_memory(s.hass, s.data, "cool")) is None
    assert not s.hass.services.calls


@pytest.mark.parametrize(
    "damage",
    ["topology", "head", "platform", "entry", "stale", "type", "prefix", "suffix", "index", "duplicate", "switch"],
)
def test_provider_rejects_unproven_registry_and_source(system, damage):
    s = system
    rows = s.hass.zone_registry_entries
    if damage == "topology":
        s.data["hvac_zone_control"] = {}
    elif damage == "head":
        rows.pop("climate.head")
    elif damage == "platform":
        for row in rows.values():
            row.platform = "other"
    elif damage == "entry":
        s.entry.runtime_data = None
    elif damage == "stale":
        s.entry.runtime_data.last_update_success = False
    elif damage == "type":
        s.entry.runtime_data.device = SimpleNamespace()
    elif damage == "prefix":
        rows[MAIN_CLIMATE].unique_id = "other-zone0-temperature"
    elif damage == "suffix":
        rows[MAIN_CLIMATE].unique_id = "controller-zone0-switch"
    elif damage == "index":
        rows[MAIN_CLIMATE].unique_id = "controller-zonebad-temperature"
    elif damage == "duplicate":
        rows[BED_CLIMATE].unique_id = rows[MAIN_CLIMATE].unique_id
        rows[BED].unique_id = rows[MAIN].unique_id
    elif damage == "switch":
        rows[BED].unique_id = rows[MAIN].unique_id
    assert provider(s.hass, s.data) is None
    assert asyncio.run(read_memory(s.hass, s.data, "cool")) is None
    assert not s.reads


def test_mode_specific_acquisition_and_release_freeze_original_baseline(system):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        automations, zones = s.adapter.takeover_snapshot()
        main = s.adapter.main_takeover_snapshot()
        assert zones[MAIN_CLIMATE] == {"target_temperature": 24, "baseline_hvac_mode": "cool"}
        assert main["target_temperature"] == 19
        original = deepcopy(s.modes)
        prepared = HVACOwnershipTransaction({}, s.c.created_at).prepare(s.desired, automations, zones, main)
        # The complete baseline is JSON serializable before any physical write.
        reloaded = json.loads(json.dumps(prepared, default=str))
        result = await s.adapter.async_execute(_action(s.desired))
        assert result.applied, result.reason
        assert s.modes["cool"]["head"] == 21 and s.modes["cool"]["zones"][MAIN_CLIMATE] == 21
        assert reloaded["hvac_control"]["main_state"][MEMORY_KEY]["modes"]["cool"]["target_temperature"] == 23
        restore = DaikinHVACAdapter(s.hass, s.data)
        result = await restore.async_restore(
            {}, reloaded["hvac_control"]["zone_states"], reloaded["hvac_control"]["main_state"]
        )
        assert result.applied, result.reason
        assert s.modes == original
        assert s.hass.states.get("climate.head").state == "off"
        assert s.hass.states.get(MAIN).state == "on" and s.hass.states.get(BED).state == "off"

    asyncio.run(run())


def test_option_disabled_and_legacy_missing_baseline_never_read_or_wake(system):
    s = system

    async def run():
        s.adapter.set_zone_control_context({}, {})
        await s.adapter.async_prepare_off_start(_action(s.desired))
        result = await s.adapter.async_execute(_action(s.desired))
        assert result.reason == "climate_zone_target_unavailable"
        s.adapter.set_zone_control_context({OFF_START_OPTION: True}, {"main_state": {"hvac_mode": "off"}})
        await s.adapter.async_prepare_off_start(_action(s.desired))
        assert not s.reads and not s.hass.services.calls

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["open", "mode", "target", "zone", "shutdown", "identity"])
def test_failed_restoration_shuts_down_and_never_oscillates_on_retry(system, monkeypatch, failure):
    s = system
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.002)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", 0.001)

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        zones, main = s.adapter.takeover_snapshot()[1], s.adapter.main_takeover_snapshot()
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        restore = DaikinHVACAdapter(s.hass, s.data)
        persisted = []

        async def persist(value):
            persisted.append(deepcopy(value))

        restore.set_main_state_persistence_callback(persist)
        original = s.hass.services.async_call

        async def service(domain, name, payload, **kwargs):
            entity = payload["entity_id"]
            reject = (
                (failure == "open" and entity == MAIN and name == "turn_on")
                or (failure == "mode" and name == "set_hvac_mode")
                or (failure == "target" and entity == "climate.head" and name == "set_temperature")
                or (failure == "zone" and entity == MAIN_CLIMATE and name == "set_temperature")
                or (failure == "shutdown" and entity == "climate.head" and name == "turn_off")
            )
            if reject:
                raise OSError("synthetic rejected restoration")
            return await original(domain, name, payload, **kwargs)

        s.hass.services.async_call = service
        if failure == "open":
            s.hass.states.values[MAIN] = "off"
            s.hass.states.values[BED] = "off"
        if failure == "identity":
            main[MEMORY_KEY]["entry_id"] = "replaced_controller"
        result = await restore.async_restore({}, zones, main)
        assert not result.applied
        assert result.saved_main_state[MEMORY_KEY] == main[MEMORY_KEY]
        assert result.reason == "hvac_zone_restore_operator_required"
        if failure != "shutdown" and failure != "identity":
            assert s.hass.states.get("climate.head").state == "off"
        # A restart keeps the attempted flag. Subsequent retries may shut down,
        # but cannot reselect modes or replace the immutable temperature intent.
        s.hass.services.async_call = original
        s.hass.services.calls.clear()
        retry = await DaikinHVACAdapter(s.hass, s.data).async_restore(
            {},
            result.saved_zone_states,
            json.loads(json.dumps(result.saved_main_state)),
        )
        assert not retry.applied
        assert all(name not in {"turn_on", "set_hvac_mode", "set_temperature"} for _, name, _ in s.hass.services.calls)
        if failure != "identity":
            assert persisted[0]["off_start_restore_attempted"]

    asyncio.run(run())


@pytest.mark.parametrize("supersession", ["callback", "durable", "dispatch"])
def test_restoration_manual_zone_supersession_preserves_user_target(system, supersession):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        zones, main = s.adapter.takeover_snapshot()[1], s.adapter.main_takeover_snapshot()
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        s.modes["cool"]["zones"][BED_CLIMATE] = 20
        s.publish()
        original = s.hass.services.async_call

        async def clamp(domain, service, payload, **kwargs):
            await original(domain, service, payload, **kwargs)
            if payload["entity_id"] == "climate.head" and service == "set_temperature":
                mode = s.selected[0]
                center = s.modes[mode]["head"]
                for entity, value in s.modes[mode]["zones"].items():
                    s.modes[mode]["zones"][entity] = min(center + 2, max(center - 2, value))
                s.publish()

        s.hass.services.async_call = clamp
        s.hass.services.calls.clear()
        restore = DaikinHVACAdapter(s.hass, s.data)
        manual = set() if supersession == "dispatch" else {BED, BED_CLIMATE}
        if supersession == "durable":
            zones.pop(BED)
            zones.pop(BED_CLIMATE)
        else:
            restore.set_zone_manual_override_check(lambda: manual)

        async def supersede(record):
            if record and record["entity_id"] == "climate.head" and record["service"] == "set_temperature":
                manual.update({BED, BED_CLIMATE})

        if supersession == "dispatch":
            restore.set_confirmation_persistence_callback(supersede)
        result = await restore.async_restore({}, zones, main)
        assert not result.applied
        assert s.modes["cool"]["zones"][BED_CLIMATE] == 20
        assert s.hass.states.get("climate.head").state == "off"
        assert result.saved_main_state[MEMORY_KEY] == main[MEMORY_KEY]
        assert result.saved_zone_states[MAIN_CLIMATE]["target_temperature"] == 24
        assert all(service == "turn_off" for _, service, _ in s.hass.services.calls)

    asyncio.run(run())


def test_failed_attempt_can_finish_from_read_only_confirmed_memory(system):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        zones, main = s.adapter.takeover_snapshot()[1], s.adapter.main_takeover_snapshot()
        main["off_start_restore_attempted"] = True
        result = await s.adapter.async_restore({}, zones, main)
        assert result.applied
        assert not s.hass.services.calls

    asyncio.run(run())


def test_refresh_compensation_is_separate_from_lifecycle_and_survives_reload(system):
    from test_executor import FakeStore

    from custom_components.ha_energy_planner.executor import Executor

    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main = s.adapter.main_takeover_snapshot()
        zones = s.adapter.takeover_snapshot()[1]
        original = {"hvac_control": {"main_state": deepcopy(main), "zone_states": deepcopy(zones)}}
        attempt = deepcopy(main)
        attempt[MEMORY_KEY]["modes"]["cool"]["target_temperature"] = 17
        attempt[MEMORY_KEY]["modes"]["cool"]["zones"] = {MAIN_CLIMATE: 17, BED_CLIMATE: 19}
        tx = HVACOwnershipTransaction(original, s.c.created_at)
        current = tx.prepare(s.desired, {}, zones, attempt)
        assert current["hvac_control"]["main_state"] == main
        assert current["hvac_control"]["attempt_snapshot"]["main_state"] == attempt
        store = FakeStore()
        store.data["ownership"] = current
        executor = Executor(store, hass=s.hass, entry_data=s.data)
        attempt["off_start_restore_attempted"] = True
        await executor._async_persist_provisional_hvac_main_state(attempt)
        saved = store.data["ownership"]["hvac_control"]
        assert "off_start_restore_attempted" not in saved["main_state"]
        assert saved["attempt_snapshot"]["main_state"]["off_start_restore_attempted"]
        assert json.loads(json.dumps(saved["attempt_snapshot"]))["main_state"] == attempt
        from custom_components.ha_energy_planner.hvac_adapter import HVACCommandResult

        failed = tx.complete(
            HVACCommandResult(
                False,
                "hvac_acquisition_rollback_failed",
                {},
                {},
                {},
                False,
                zones,
                attempt,
            ),
            store.data["ownership"],
            s.desired,
            main_state_superseded=False,
            superseded_zone_entity_ids=set(),
        )
        assert failed["hvac_control"]["main_state"] == main
        assert failed["hvac_control"]["attempt_snapshot"]["main_state"] == attempt
        assert failed["hvac_control"]["off_start_control_data"] == main[MEMORY_KEY]["control_data"]
        # The equivalent initial acquisition progress belongs to the original
        # snapshot; its one-attempt limit also survives the provisional flush.
        initial = {**deepcopy(main), "off_start_restore_attempted": True}
        await executor._async_persist_provisional_hvac_main_state(initial)
        assert store.data["ownership"]["hvac_control"]["main_state"]["off_start_restore_attempted"]

    asyncio.run(run())


@pytest.mark.parametrize("route", ["release", "safe_state"])
def test_executor_flushes_mode_memory_before_writes_and_restores_owned_mapping(system, route):
    from test_constraints import _plan
    from test_executor import FakeStore, _arm_store

    from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
    from custom_components.ha_energy_planner.executor import Executor

    s = system
    store = FakeStore()
    executor = Executor(
        store,
        hass=s.hass,
        entry_data=s.data,
        options={**DEFAULT_OPTIONS, OFF_START_OPTION: True, "dry_run": False, "climate_control_enabled": True},
    )
    _arm_store(store, executor)
    action = _action(s.desired)
    action.execute_not_before = s.c.created_at
    action.execute_not_after = s.c.created_at.replace(year=s.c.created_at.year + 1)
    action.plan_id = "plan-1"
    original_call = s.hass.services.async_call

    async def call(domain, service, payload, **kwargs):
        assert store.flush_count > 0
        memory = store.data["ownership"]["hvac_control"]["main_state"][MEMORY_KEY]
        assert memory["modes"]["cool"]["target_temperature"] == 23
        await original_call(domain, service, payload, **kwargs)

    s.hass.services.async_call = call

    async def run():
        await executor.async_evaluate(_plan(s.c.created_at, action))
        assert s.hass.states.get("climate.head").state == "cool", store.data["outcomes"]
        # A later sensor/head/topology edit cannot redirect old ownership to
        # newly selected equipment or substitute comfort values for its memory.
        executor.entry_data = {
            **s.data,
            "daikin_climate_entity": "climate.replacement",
            "climate_zone_entities": [],
            "hvac_zone_control": {},
        }
        s.hass.states.values["climate.replacement"] = FakeState("heat", {"temperature": 28})
        result = (
            await executor.async_release_hvac_control("mapping_changed")
            if route == "release"
            else await executor.async_restore_device_control("daikin", "mapping_changed")
        )
        assert result.result.value == "restored", result
        assert s.hass.states.get("climate.head").state == "off"
        assert s.hass.states.get("climate.replacement").state == "heat"
        assert s.modes["cool"]["head"] == 23

    asyncio.run(run())


@pytest.mark.parametrize("response", ["delayed", "timeout"])
def test_asynchronous_mode_activation_and_readback_do_not_resend(system, monkeypatch, response):
    s = system
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", 0.001)
    original = s.hass.services.async_call
    tasks = []

    async def service(domain, name, payload, **kwargs):
        if name == "set_hvac_mode":

            async def settle():
                await asyncio.sleep(0.003)
                await original(domain, name, payload, **kwargs)

            tasks.append(asyncio.create_task(settle()))
            if response == "timeout":
                raise TimeoutError
            return
        await original(domain, name, payload, **kwargs)

    s.hass.services.async_call = service

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        result = await s.adapter.async_execute(_action(s.desired))
        await asyncio.gather(*tasks)
        assert result.applied, result.reason
        assert sum(name == "set_hvac_mode" for _, name, _ in s.hass.services.calls) == 1

    asyncio.run(run())


def test_equipment_offset_uses_fresh_mode_bounds_and_restores_mode_memories(system):
    s = system
    s.data["hvac_zone_mappings"][MAIN]["cooling_equipment_offset_c"] = -2
    s.c.climate_inputs["zones"][MAIN]["cooling_equipment_offset_c"] = -2
    s.c.occupied_temperature_low_c, s.c.occupied_temperature_high_c = 19, 22
    s.hass.states.values["input_number.low"] = FakeState("19")
    s.hass.states.values["input_number.high"] = FakeState("22")
    s.adapter.entry_data = s.data
    desired = {**s.desired, "precondition_target": 19, **desired_fields(s.c, "cool", 19, "preconditioning")}

    async def run():
        await s.adapter.async_prepare_off_start(_action(desired))
        zones, main = s.adapter.takeover_snapshot()[1], s.adapter.main_takeover_snapshot()
        result = await s.adapter.async_execute(_action(desired))
        assert result.applied, result.reason
        assert s.modes["cool"] == {"head": 17, "zones": {MAIN_CLIMATE: 17, BED_CLIMATE: 19}}
        result = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)
        assert result.applied, result.reason
        assert s.modes["cool"] == {"head": 23, "zones": {MAIN_CLIMATE: 24, BED_CLIMATE: 24}}

    asyncio.run(run())


def test_capture_capability_edge_cases_and_owned_data(system):
    from custom_components.ha_energy_planner.daikin_memory import CONTROL_KEYS, owned_control_data

    s = system
    assert asyncio.run(read_memory(s.hass, s.data, "dry")) is None
    for raw in (
        {},
        {"main_state": None},
        {"main_state": {MEMORY_KEY: {"version": 3}}},
        {"main_state": {MEMORY_KEY: {"version": 1, "control_data": []}}},
        {"main_state": {MEMORY_KEY: {"version": 1, "control_data": {}}}},
    ):
        assert owned_control_data(s.data, raw) == s.data

    async def run():
        memory = await read_memory(s.hass, s.data, "cool")
        control = {"main_state": {MEMORY_KEY: memory}}
        assert set(memory["control_data"]) == set(CONTROL_KEYS)
        assert (
            owned_control_data({"daikin_climate_entity": "climate.other"}, control)["daikin_climate_entity"]
            == "climate.head"
        )
        s.values.zones["zone_onoff"] = "1;0;0"
        assert await read_memory(s.hass, s.data, "cool") is None
        s.values.zones["zone_onoff"] = "1;0"
        s.values.control["dt1"] = "18"
        assert await read_memory(s.hass, s.data, "cool") is None

    asyncio.run(run())


def test_exact_mode_bounds_and_step_are_rechecked_at_dispatch(system, monkeypatch):
    s = system
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.002)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", 0.001)

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        assert not s.adapter._memory_target_available("climate.missing", 19)
        assert not s.adapter._memory_target_available(MAIN_CLIMATE, 30)
        s.hass.states.get(MAIN_CLIMATE).attributes["target_temp_step"] = None
        assert not s.adapter._memory_target_available(MAIN_CLIMATE, 19)
        with pytest.raises(adapter_module._HVACStateConfirmationError, match="bounds"):
            await s.adapter._async_apply_hvac_state("climate.head", {"target_temperature": 19.5})
        with pytest.raises(adapter_module._HVACStateConfirmationError, match="bounds"):
            s.adapter._assert_memory_target("set_temperature", {"entity_id": MAIN_CLIMATE, "temperature": 30})
        assert not s.hass.services.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid",
    ["low", "high", "step", "zero_step", "heat_half", "cool_half", "range", "dry_missing", "dry_half", "dry_range"],
)
def test_capture_rejects_head_memories_that_cannot_pass_restore_bounds(system, invalid):
    s = system
    head = s.hass.states.get("climate.head")
    if invalid in {"low", "high", "step"}:
        head.attributes[{"low": "min_temp", "high": "max_temp", "step": "target_temp_step"}[invalid]] = None
    elif invalid == "zero_step":
        head.attributes["target_temp_step"] = 0
    elif invalid == "heat_half":
        s.values.control.update(dt1="19.5", stemp="19.5")
        head.attributes["temperature"] = 19.5
    elif invalid == "cool_half":
        s.values.control["dt2"] = "23.5"
    elif invalid == "range":
        head.attributes["max_temp"] = 22
    else:
        target = {"dry_missing": None, "dry_half": 19.5, "dry_range": 33}[invalid]
        s.values.control.update(mode="7", stemp=target)
        head.attributes["temperature"] = target

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        assert MEMORY_KEY not in s.adapter.main_takeover_snapshot()
        result = await s.adapter.async_execute(_action(s.desired))
        assert not result.applied and not s.hass.services.calls

    asyncio.run(run())


def test_cancelled_acquisition_keeps_unattempted_complete_baseline_for_restart(system):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        s.adapter.set_abort_main_state(main)
        original = s.adapter._async_execute

        async def cancel(action):
            # Simulate cancellation after accepted activation and target writes.
            result = await original(action)
            assert result.applied
            raise asyncio.CancelledError

        s.adapter._async_execute = cancel
        with pytest.raises(asyncio.CancelledError):
            await s.adapter.async_execute(_action(s.desired))
        assert s.hass.states.get("climate.head").state == "off"
        assert "off_start_restore_attempted" not in main
        result = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)
        assert result.applied, (result, s.hass.services.calls, s.modes)
        assert s.modes["cool"]["zones"][MAIN_CLIMATE] == 24

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["off", "heat", "cool"])
def test_manual_head_supersession_never_writes_another_modes_zone_memory(system, monkeypatch, mode):
    from custom_components.ha_energy_planner.daikin_memory import owned_control_data

    s = system
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.002)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", 0.001)

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        # Main baseline is superseded, but original mode provenance and mapping
        # remain with the still-owned zones, even after selecting new equipment.
        control = {"off_start_control_data": main[MEMORY_KEY]["control_data"], "zone_states": zones}
        s.hass.states.values["climate.head"].state = mode
        if mode == "off":
            # Original enablement is On, but the operator has stopped the head
            # and closed its path. Recovery must retain the power baseline
            # without recalling heating by opening that switch.
            s.hass.states.values[MAIN] = "off"
        if mode == "heat":
            s.hass.states.values["climate.head"].attributes["temperature"] = 23
            for entity in PAIRS.values():
                s.hass.states.values[entity] = FakeState(
                    "heat", {"temperature": 24, "min_temp": 21, "max_temp": 25, "target_temp_step": 1}
                )
        if mode == "cool":
            s.hass.states.values["climate.head"].attributes["temperature"] = 23
            for entity in PAIRS.values():
                s.hass.states.values[entity] = FakeState(
                    "cool", {"temperature": 24, "min_temp": 21, "max_temp": 25, "target_temp_step": 1}
                )
        # The explicit paired alias remains tied to its original controller.
        restored_data = owned_control_data({"daikin_climate_entity": "climate.other"}, control)
        restore = DaikinHVACAdapter(s.hass, restored_data)
        restore.set_zone_control_context({}, control)
        result = await restore.async_restore({}, zones)
        assert not any(name in {"set_hvac_mode", "set_temperature"} for _, name, _ in s.hass.services.calls)
        if mode == "off":
            assert not result.applied
            assert not s.hass.services.calls
            assert s.hass.states.get("climate.head").state == "off"
        elif mode == "heat":
            assert not result.applied
            assert result.saved_zone_states[MAIN_CLIMATE]["baseline_hvac_mode"] == "cool"
        else:
            assert result.applied

    asyncio.run(run())


def test_heating_off_start_restores_original_heating_and_preserves_cooling(system):
    s = system
    for room in s.c.climate_inputs["zones"].values():
        room["temperature"] = 18
        s.hass.states.values[room["temperature_entity"]].state = "18"
    desired = {
        "mode": "heat",
        "hvac_mode": "heat",
        "precondition_target": 24,
        "target_temperature": 24,
        "enable_zones": True,
        **desired_fields(s.c, "heat", 24, "preconditioning"),
    }
    original = deepcopy(s.modes)

    async def run():
        await s.adapter.async_prepare_off_start(_action(desired))
        zones, main = s.adapter.takeover_snapshot()[1], s.adapter.main_takeover_snapshot()
        assert (await s.adapter.async_execute(_action(desired))).applied
        assert s.modes["heat"]["head"] == 24
        assert (await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)).applied
        assert s.modes == original
        assert s.hass.states.get("climate.head").state == "off"

    asyncio.run(run())


@pytest.mark.parametrize("replacement", [False, True])
def test_failed_scheduler_guard_still_shuts_down_proven_off_owner(system, replacement):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        zones, main = s.adapter.takeover_snapshot()[1], s.adapter.main_takeover_snapshot()
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        if replacement:
            main[MEMORY_KEY]["entry_id"] = "replaced_controller"
        restore = DaikinHVACAdapter(s.hass, s.data)

        async def no_guard(**kwargs):
            return False

        restore._async_arm_scheduler_guard = no_guard
        s.hass.services.calls.clear()
        result = await restore.async_restore({}, zones, main)
        assert not result.applied
        assert result.saved_main_state[MEMORY_KEY] == main[MEMORY_KEY]
        assert s.hass.states.get("climate.head").state == ("cool" if replacement else "off")
        assert all(name == "turn_off" for _, name, _ in s.hass.services.calls)

    asyncio.run(run())


@pytest.mark.parametrize("corrupt_inactive_memory", [False, True])
def test_live_airbase_padding_and_off_dry_replay(system, corrupt_inactive_memory):
    """Actual read-only protocol shape: two zones, eight slots and no dt7."""
    s = system
    fixture = json.loads((Path(__file__).parent / "fixtures/hvac/airbase_off_dry_memory_2026_10_31.json").read_text())
    s.values.control, s.values.model, s.values.zones = (deepcopy(fixture[key]) for key in ("control", "model", "zones"))
    for mode, key, zone_key in (("heat", "dt1", "lztemp_h"), ("cool", "dt2", "lztemp_c")):
        s.modes[mode] = {
            "head": float(s.values.control[key]),
            "zones": {
                entity: float(unquote(s.values.zones[zone_key]).split(";")[index])
                for index, entity in enumerate(PAIRS.values())
            },
        }
    s.modes["dry"] = {"head": float(s.values.control["stemp"]), "zones": {}}
    s.selected[0] = "dry"
    for index, actuator in enumerate(PAIRS):
        s.hass.states.values[actuator] = (
            "on" if unquote(s.values.zones["zone_onoff"]).split(";")[index] == "1" else "off"
        )
    original = deepcopy(s.modes)

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        assert main[MEMORY_KEY]["selected_mode"] == "dry"
        assert main[MEMORY_KEY]["modes"]["dry"]["target_source"] == "original_off_dry_stemp"
        assert len(zones) == 4 and not s.hass.services.calls
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        service = s.hass.services.async_call

        async def corrupt(domain, name, payload, **kwargs):
            await service(domain, name, payload, **kwargs)
            if (
                corrupt_inactive_memory
                and payload["entity_id"] == "climate.head"
                and name == "set_temperature"
                and s.selected[0] == "dry"
            ):
                s.modes["cool"]["head"] = original["cool"]["head"] + 1
                s.publish()

        s.hass.services.async_call = corrupt
        result = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, json.loads(json.dumps(main)))
        assert result.applied is not corrupt_inactive_memory
        assert s.hass.states.get("climate.head").state == "off"
        if corrupt_inactive_memory:
            assert (
                result.saved_main_state[MEMORY_KEY]["modes"]["cool"]["target_temperature"] == original["cool"]["head"]
            )
            assert result.saved_zone_states == zones
            s.hass.services.calls.clear()
            retry = await DaikinHVACAdapter(s.hass, s.data).async_restore(
                {}, result.saved_zone_states, result.saved_main_state
            )
            assert not retry.applied and not s.hass.services.calls
        else:
            assert s.modes == original
            assert len(s.values.zones["lztemp_c"].split(";")) == 8

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["count", "dry_bounds", "identity"])
def test_mode_memory_requires_physical_count_dry_bounds_and_stable_identity(system, invalid):
    s = system
    if invalid == "count":
        s.values.model["en_zone"] = "9"
    elif invalid == "dry_bounds":
        s.values.control["mode"] = "7"
        s.hass.states.get("climate.head").attributes.pop("target_temp_step")
    else:

        async def change_owner(*args, **kwargs):
            s.entry.runtime_data = None

        s.entry.runtime_data.device.update_status = change_owner
    assert asyncio.run(read_memory(s.hass, s.data, "cool")) is None
    assert not s.hass.services.calls


@pytest.mark.parametrize("invalid", ["missing_source", "unsupported_mode", "missing_head", "timeout"])
def test_activation_read_is_not_an_invented_restoration_baseline(system, invalid):
    from custom_components.ha_energy_planner.daikin_memory import read_activation

    s = system
    if invalid == "missing_source":
        s.entry.runtime_data = None
    elif invalid == "unsupported_mode":
        s.values.control["mode"] = "3"
    elif invalid == "missing_head":
        del s.hass.states.values["climate.head"]
    else:

        async def timeout(*args, **kwargs):
            raise TimeoutError

        s.entry.runtime_data.device.update_status = timeout
    assert asyncio.run(read_activation(s.hass, s.data)) is None
    assert not s.hass.services.calls


@pytest.mark.parametrize("change", ["mac", "zone_count", "zone_indices"])
def test_physical_source_change_after_snapshot_retires_unsent_command(system, change):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        baseline = s.adapter.main_takeover_snapshot()

        async def persist(record):
            if record:
                if change == "mac":
                    s.entry.runtime_data.device.mac = "different-controller"
                elif change == "zone_count":
                    s.values.model["en_zone"] = "3"
                else:
                    rows = s.hass.zone_registry_entries
                    for first, second in ((MAIN, BED), (MAIN_CLIMATE, BED_CLIMATE)):
                        rows[first].unique_id, rows[second].unique_id = rows[second].unique_id, rows[first].unique_id

        s.adapter.set_confirmation_persistence_callback(persist)
        with pytest.raises(adapter_module._HVACStateConfirmationError, match="identity"):
            await s.adapter._async_command(s.hass, "switch", "turn_on", {"entity_id": BED})
        assert not s.hass.services.calls
        assert s.adapter.main_takeover_snapshot()[MEMORY_KEY] == baseline[MEMORY_KEY]

    asyncio.run(run())


@pytest.mark.parametrize("bad", ["user", "parent", "target", "expired", "superseded", "other_mode", "auxiliary"])
def test_off_start_activation_feedback_is_exact_and_bounded(system, bad):
    from datetime import timedelta

    from homeassistant.core import Context

    from custom_components.ha_energy_planner.coordinator import _matches_climate_transaction

    s = system
    s.hass.states.values["climate.head"] = FakeState("dry", {"temperature": 19})
    command = {
        "entity_id": BED,
        "service": "turn_on",
        "main_mode": "off",
        "context_id": "issued",
        "issued_at": NOW,
        "deadline": NOW + timedelta(minutes=3),
        "off_start_head_activation": {
            "entity_id": "climate.head",
            "hvac_mode": "dry",
            "target_temperature": 19,
            "zone_climates": list(PAIRS.values()),
        },
    }
    old, new = FakeState("off", {"temperature": 19}), FakeState("dry", {"temperature": 19})
    event = SimpleNamespace(data={"entity_id": "climate.head", "old_state": old, "new_state": new}, context=Context())
    assert _matches_climate_transaction(s.data, [command], event, NOW, s.hass)
    commands, now = [command], NOW
    if bad == "user":
        event.context = Context(user_id="operator")
    elif bad == "parent":
        event.context = Context(parent_id="external")
    elif bad == "target":
        new.attributes["temperature"] = 20
    elif bad == "expired":
        now += timedelta(minutes=2)
    elif bad == "superseded":
        commands.append(
            {
                "entity_id": "climate.head",
                "issued_at": NOW,
                "deadline": command["deadline"],
                "context_id": "newer",
                "service": "set_hvac_mode",
                "requested": {"hvac_mode": "cool"},
            }
        )
    elif bad == "other_mode":
        new.state = "heat"
    elif bad == "auxiliary":
        new.attributes["fan_mode"] = "high"
    assert not _matches_climate_transaction(s.data, commands, event, now, s.hass)


def test_failed_off_capability_prepare_keeps_legacy_rejection_without_writes(system):
    s = system
    s.values.control.pop("dt2")

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        assert MEMORY_KEY not in s.adapter.main_takeover_snapshot()
        result = await s.adapter.async_execute(_action(s.desired))
        assert result.reason == "climate_zone_target_unavailable" and not s.hass.services.calls

    asyncio.run(run())


def test_unproven_current_wake_mode_stops_restoration_without_probing(system):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        s.values.control["mode"] = "3"
        result = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)
        assert not result.applied and not s.hass.services.calls
        assert result.saved_main_state[MEMORY_KEY] == main[MEMORY_KEY]
        assert result.saved_zone_states == zones

    asyncio.run(run())


@pytest.mark.parametrize("route", ["release", "safe_state"])
def test_durable_restore_attempt_survives_outer_exception(system, monkeypatch, route):
    from test_executor import FakeStore

    from custom_components.ha_energy_planner import executor as executor_module
    from custom_components.ha_energy_planner.executor import Executor

    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        store = FakeStore()
        store.data["ownership"] = {
            "hvac_control": {
                "main_state": main,
                "zone_states": zones,
                "off_start_control_data": main[MEMORY_KEY]["control_data"],
            }
        }
        executor = Executor(store, hass=s.hass, entry_data=s.data)

        class Interrupted(DaikinHVACAdapter):
            async def async_restore(self, *args):
                assert self._async_persist_main_state is not None
                await self._async_persist_main_state({**deepcopy(main), "off_start_restore_attempted": True})
                raise RuntimeError("synthetic interrupted restoration")

        monkeypatch.setattr(executor_module, "DaikinHVACAdapter", Interrupted)
        if route == "release":
            await executor.async_release_hvac_control("interrupted")
        else:
            await executor.async_restore_device_control("daikin", "interrupted")
        retained = store.data["ownership"]["hvac_control"]
        assert retained["main_state"][MEMORY_KEY] == main[MEMORY_KEY]
        assert retained["main_state"]["off_start_restore_attempted"]
        monkeypatch.setattr(executor_module, "DaikinHVACAdapter", DaikinHVACAdapter)
        s.hass.services.calls.clear()
        await executor.async_release_hvac_control("retry")
        assert all(name == "turn_off" for _, name, _ in s.hass.services.calls)

    asyncio.run(run())


@pytest.mark.parametrize("main_complete", [False, True])
def test_restoration_does_not_reopen_originally_closed_room_or_cycle_completed_room(system, main_complete):
    s = system
    stopped = MAIN if main_complete else BED
    s.c.climate_inputs["zones"][stopped]["temperature"] = 20
    sensor = s.c.climate_inputs["zones"][stopped]["temperature_entity"]
    s.hass.states.values[sensor].state = "20"
    desired = {**s.desired, **desired_fields(s.c, "cool", 21, "preconditioning")}

    async def run():
        await s.adapter.async_prepare_off_start(_action(desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        assert (await s.adapter.async_execute(_action(desired))).applied
        s.hass.services.calls.clear()
        result = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)
        assert result.applied, result
        # Bedrooms was originally Off. A captured target cannot activate it.
        assert not any(entity["entity_id"] == BED and name == "turn_on" for _, name, entity in s.hass.services.calls)
        if main_complete:
            head_modes = [
                entity.get("hvac_mode") for _, name, entity in s.hass.services.calls if name == "set_hvac_mode"
            ]
            assert head_modes == ["heat"]  # No extra Off -> cooling -> Off cycle.
        assert s.hass.states.get("climate.head").state == "off"

    asyncio.run(run())


def test_off_start_requires_safe_airflow_on_the_first_implicit_wake(system):
    s = system
    s.data["hvac_zone_control"]["safe_open_sets"] = [[MAIN, BED]]
    s.hass.states.values[MAIN] = "off"
    s.values.zones["zone_onoff"] = "0;0"

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        assert s.adapter.main_takeover_snapshot()[MEMORY_KEY]
        result = await s.adapter.async_execute(_action(s.desired))
        assert not result.applied and not s.hass.services.calls
        assert s.hass.states.get("climate.head").state == "off"

    asyncio.run(run())


def test_manual_head_release_confirms_owned_inactive_zone_memory_without_wake(system):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        memory = s.adapter.main_takeover_snapshot()[MEMORY_KEY]
        zones = s.adapter.takeover_snapshot()[1]
        restore = DaikinHVACAdapter(s.hass, s.data)
        restore.set_zone_control_context(
            {},
            {
                "off_start_control_data": memory["control_data"],
                "off_start_identity": {key: memory[key] for key in ("entry_id", "controller_fingerprint")},
            },
        )
        result = await restore.async_restore({}, zones)
        assert result.applied and not s.hass.services.calls

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["power", "path", "shutdown"])
def test_restoration_retains_evidence_when_safe_airflow_cannot_be_established(system, missing):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        s.hass.services.calls.clear()
        if missing == "power":
            s.hass.states.values[MAIN] = "unavailable"
        else:
            s.hass.states.values[MAIN] = "off"
            s.hass.states.values[BED] = "off"
        if missing == "path":
            s.hass.states.values["climate.head"].state = "off"
            s.publish()
            zones = {}
        if missing == "shutdown":
            s.hass.services.fail_services.add(("climate", "turn_off"))
        result = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)
        assert not result.applied
        assert result.saved_main_state[MEMORY_KEY] == main[MEMORY_KEY]
        assert all(name == "turn_off" for _, name, _ in s.hass.services.calls)

    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failed_native_preparation_retires_the_early_manual_monitor(system, monkeypatch, failure):
    from test_constraints import _plan
    from test_executor import FakeStore, _arm_store

    from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
    from custom_components.ha_energy_planner.executor import Executor

    s = system
    store = FakeStore()
    executor = Executor(
        store,
        hass=s.hass,
        entry_data=s.data,
        options={**DEFAULT_OPTIONS, OFF_START_OPTION: True, "dry_run": False, "climate_control_enabled": True},
    )
    _arm_store(store, executor)
    action = _action(s.desired)
    action.execute_not_before = s.c.created_at
    action.execute_not_after = s.c.created_at.replace(year=s.c.created_at.year + 1)
    action.plan_id = "plan-1"

    async def interrupt(adapter, action):
        assert executor.pending_hvac_desired_state == {}
        raise failure("synthetic preparation interruption")

    monkeypatch.setattr(DaikinHVACAdapter, "async_prepare_off_start", interrupt)

    async def run():
        with pytest.raises(failure):
            await executor.async_evaluate(_plan(s.c.created_at, action))
        assert executor.pending_hvac_desired_state is None
        assert not s.hass.services.calls
        assert not store.data.get("ownership", {}).get("hvac_control")

    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["unchanged", "before_read", "during_read"])
def test_owned_coast_cannot_acquire_a_replacement_controller(system, replacement):
    s = system

    def replace_controller():
        s.entry.runtime_data.device.mac = "replacement"
        for index, (actuator, climate) in enumerate(PAIRS.items()):
            s.hass.zone_registry_entries[actuator].unique_id = f"replacement-zone{index}"
            s.hass.zone_registry_entries[climate].unique_id = f"replacement-zone{index}-temperature"

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        main, zones = s.adapter.main_takeover_snapshot(), s.adapter.takeover_snapshot()[1]
        control = {"main_state": deepcopy(main), "zone_states": zones, "main_state_committed": True}
        assert (await s.adapter.async_execute(_action(s.desired))).applied
        await s.adapter._async_apply_hvac_state("climate.head", {"hvac_mode": "off"})
        s.hass.services.calls.clear()
        s.reads.clear()
        if replacement == "before_read":
            replace_controller()
        elif replacement == "during_read":
            original_read = s.entry.runtime_data.device.update_status

            async def update(*args, **kwargs):
                await original_read(*args, **kwargs)
                replace_controller()

            s.entry.runtime_data.device.update_status = update
        resume = DaikinHVACAdapter(s.hass, s.data)
        resume.set_zone_control_context({OFF_START_OPTION: True}, control)
        await resume.async_prepare_off_start(_action(s.desired))
        result = await resume.async_execute(_action(s.desired))
        assert control["main_state"] == main
        if replacement == "unchanged":
            assert result.applied and s.hass.states.get("climate.head").state == "cool"
        else:
            assert not result.applied and not s.hass.services.calls
            assert s.hass.states.get("climate.head").state == "off"
            if replacement == "before_read":
                assert not s.reads
            restored = await DaikinHVACAdapter(s.hass, s.data).async_restore({}, zones, main)
            assert not restored.applied and not s.hass.services.calls
            assert restored.saved_main_state[MEMORY_KEY] == main[MEMORY_KEY]

    asyncio.run(run())


@pytest.mark.parametrize("incompatible", ["lifecycle", "attempt", "retained_identity"])
def test_dispatch_requires_all_snapshot_authorities_to_match(system, incompatible):
    s = system

    async def run():
        await s.adapter.async_prepare_off_start(_action(s.desired))
        original = s.adapter.main_takeover_snapshot()
        control = {
            "main_state": deepcopy(original),
            "off_start_identity": {key: original[MEMORY_KEY][key] for key in ("entry_id", "controller_fingerprint")},
        }
        s.adapter.set_zone_control_context({OFF_START_OPTION: True}, control)
        if incompatible == "lifecycle":
            control["main_state"][MEMORY_KEY]["entry_id"] = "old-controller"
        elif incompatible == "attempt":
            s.adapter._off_start_memory["entry_id"] = "other-controller"
        else:
            control["off_start_identity"]["entry_id"] = "retired-controller"
        with pytest.raises(adapter_module._HVACStateConfirmationError, match="identity"):
            await s.adapter._async_command(s.hass, "switch", "turn_on", {"entity_id": BED})
        assert not s.hass.services.calls

    asyncio.run(run())
