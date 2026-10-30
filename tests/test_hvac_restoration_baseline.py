"""Mode-dependent Daikin restoration from a minimal sanitized incident replay."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_hvac_adapter import FakeHass, FakeState, _action

from custom_components.ha_energy_planner import hvac_adapter as adapter_module
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter, HVACCommandResult
from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction

HEAD = "climate.head"
PAIRS = {"switch.main": "climate.main", "switch.bed": "climate.bed"}
ZONES = list(PAIRS.values())
AUTOMATIONS = {f"automation.schedule_{number}": "on" for number in range(4)}
FIXTURE = json.loads((Path(__file__).parent / "fixtures/hvac/off_mode_restoration_2026_10_29.json").read_text())


def entry_data(supported=True):
    result = {"daikin_climate_entity": HEAD, "climate_zone_entities": [*PAIRS, *ZONES],
              "climate_automation_entities": list(AUTOMATIONS)}
    if supported:
        result["hvac_zone_control"] = {"pairs": PAIRS, "safe_open_sets": [list(PAIRS), ["switch.main"], ["switch.bed"]]}
    return result


class RememberedDaikin(FakeHass):
    """Separate mode memories, hidden off targets, delayed feedback and head-relative bounds."""

    def __init__(self, mode="off"):
        super().__init__(dict(AUTOMATIONS))
        self.mode = mode
        self.remembered = FIXTURE["remembered_mode"] if mode == "off" else mode
        self.head_targets = dict(FIXTURE["head_targets"])
        self.zone_targets = deepcopy(FIXTURE["zone_targets"])
        self.power = dict(zip(PAIRS, FIXTURE["original_power"], strict=True))
        self.delay = 0.002
        self.mode_delay = 0.025  # scaled 30.4s feedback, beyond the initial 30s readback window
        self.clamp_zone = False
        self.fail_zone = False
        self.timeout_zone = False
        self.publish()
        original = self.services.async_call

        async def call(domain, service, data, blocking=False, context=None):
            entity = data["entity_id"]
            if domain != "climate" and entity not in PAIRS:
                return await original(domain, service, data, blocking, context)
            self.services.calls.append((domain, service, dict(data)))
            self.services.contexts.append(context)

            def feedback():
                if entity in PAIRS:
                    self.power[entity] = "on" if service == "turn_on" else "off"
                    if service == "turn_on" and self.mode == "off":
                        self.mode = self.remembered
                elif entity == HEAD:
                    if service == "turn_off":
                        self.mode = "off"
                    elif service in {"turn_on", "set_hvac_mode"}:
                        self.mode = data.get("hvac_mode", self.remembered)
                        if self.mode != "off":
                            self.remembered = self.mode
                    elif service == "set_temperature":
                        self.head_targets[self.remembered] = data["temperature"]
                elif service == "set_temperature":
                    target = data["temperature"]
                    head_target = self.head_targets[self.remembered]
                    if self.clamp_zone:
                        target = head_target
                    else:
                        assert head_target - 2 <= target <= head_target + 2
                    self.zone_targets[self.remembered][ZONES.index(entity)] = target
                self.publish()

            if entity in ZONES and self.fail_zone:
                raise OSError("sanitized device failure")
            delay = self.mode_delay if entity == HEAD and service == "set_hvac_mode" else self.delay
            asyncio.get_running_loop().call_later(delay, feedback)
            if entity in ZONES and self.timeout_zone:
                raise TimeoutError

        self.services.async_call = call

    def publish(self):
        target = self.head_targets[self.remembered]
        self.states.values[HEAD] = FakeState(self.mode, {"temperature": target, "min_temp": 7, "max_temp": 35,
                                                       "hvac_modes": ["off", "heat", "cool"]})
        for index, (actuator, zone) in enumerate(PAIRS.items()):
            self.states.values[actuator] = self.power[actuator]
            visible = self.mode != "off" and self.power[actuator] == "on"
            self.states.values[zone] = FakeState(self.mode if visible else "off", {
                "temperature": self.zone_targets[self.remembered][index] if visible else None,
                "min_temp": target - 2, "max_temp": target + 2,
            })


@pytest.fixture(autouse=True)
def short_deadlines(monkeypatch):
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.015)
    monkeypatch.setattr(adapter_module, "_STATE_RECONCILIATION_TIMEOUT_SECONDS", 0.06)
    monkeypatch.setattr(adapter_module, "_STATE_CONFIRMATION_POLL_SECONDS", 0.001)


def desired(mode="cool"):
    return {"hvac_mode": mode, "target_temperature": 17 if mode == "cool" else 21,
            "zone_targets": dict(zip(ZONES, FIXTURE["planner_zone_targets"], strict=True)),
            "enable_zones": True, "configured_zones_only": True, "suppress_automations": True}


@pytest.mark.parametrize("supported", [False, True])
def test_hidden_off_baseline_rejects_before_wake_or_automation_changes(supported):
    hass = RememberedDaikin()
    adapter = DaikinHVACAdapter(hass, entry_data(supported))
    result = asyncio.run(adapter.async_execute(_action(desired())))
    assert not result.applied and result.rollback_succeeded and not result.command_sent
    assert result.reason == "climate_zone_target_unavailable"
    assert not hass.services.calls
    assert hass.mode == "off" and hass.head_targets == FIXTURE["head_targets"]
    assert hass.zone_targets == FIXTURE["zone_targets"]


@pytest.mark.parametrize("supported", [False, True])
def test_visible_other_mode_is_not_a_reversible_cooling_baseline(supported):
    hass = RememberedDaikin("heat")
    hass.power = dict.fromkeys(PAIRS, "on")
    hass.publish()
    result = asyncio.run(DaikinHVACAdapter(hass, entry_data(supported)).async_execute(_action(desired())))
    assert result.reason == "climate_zone_baseline_mode_mismatch"
    assert result.rollback_succeeded and not hass.services.calls and hass.mode == "heat"


def test_exact_mode_sequence_cannot_fill_original_baseline_gaps_after_reload():
    """The 24C value enters old snapshots on the refresh following mode selection."""
    async def replay():
        hass = RememberedDaikin()
        data = entry_data()
        first = DaikinHVACAdapter(hass, data)
        automations, zones = first.takeover_snapshot()
        main = first.main_takeover_snapshot()
        assert zones == dict(zip(PAIRS, FIXTURE["original_power"], strict=True))
        original = {"climate_automations": automations, "hvac_control": {"main_state": main, "zone_states": zones}}
        tx = HVACOwnershipTransaction(original, datetime.now(UTC))

        async def send(entity, service, **values):
            await hass.services.async_call(entity.split(".")[0], service, {"entity_id": entity, **values})
            await asyncio.sleep(hass.mode_delay + 0.002 if service == "set_hvac_mode" else 0.004)

        await send("switch.bed", "turn_on")
        assert hass.mode == "heat" and [hass.states.get(zone).attributes["temperature"] for zone in ZONES] == [20, 20]
        await send(HEAD, "set_hvac_mode", hvac_mode="cool")
        assert hass.head_targets["cool"] == 23
        assert [hass.states.get(zone).attributes["temperature"] for zone in ZONES] == [24, 24]
        for target in (23, 17):
            if target == 17:
                await send(HEAD, "set_temperature", temperature=target)
            adapter = DaikinHVACAdapter(hass, data)
            adapter.set_zone_control_context({}, original["hvac_control"])
            snapshot = adapter.takeover_snapshot()[1]
            assert not set(snapshot) & set(ZONES)
            # Defense at the durable ownership boundary also rejects a stale
            # adapter/caller offering the newly exposed numeric targets.
            exposed = {zone: {"target_temperature": 24} for zone in ZONES}
            prepared = tx.prepare(desired(), {}, exposed, adapter.main_takeover_snapshot())
            assert prepared["hvac_control"]["zone_states"] == zones
            for applied in (True, False):
                result = HVACCommandResult(applied, "test", {}, {}, {}, rollback_succeeded=False,
                                           saved_zone_states=exposed)
                completed = tx.complete(result, prepared, desired(), main_state_superseded=False,
                                        superseded_zone_entity_ids=set())
                assert completed["hvac_control"]["zone_states"] == zones
        await send(ZONES[0], "set_temperature", temperature=17)
        await send(ZONES[1], "set_temperature", temperature=19)
        await send(HEAD, "set_hvac_mode", hvac_mode="heat")
        assert [hass.states.get(zone).attributes["temperature"] for zone in ZONES] == [20, 20]
        await send(HEAD, "turn_off")
        await send("switch.bed", "turn_off")
        assert hass.mode == "off" and hass.head_targets["heat"] == 19
        assert all(hass.states.get(zone).attributes["temperature"] is None for zone in ZONES)
    asyncio.run(replay())


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("uncertain", [False, True])
def test_same_mode_visible_baseline_restores_exact_targets_after_delayed_feedback(supported, uncertain):
    async def run():
        hass = RememberedDaikin("cool")
        hass.power = dict.fromkeys(PAIRS, "on")
        hass.publish()
        adapter = DaikinHVACAdapter(hass, entry_data(supported))
        automations, zones = adapter.takeover_snapshot()
        main = adapter.main_takeover_snapshot()
        hass.timeout_zone = uncertain
        result = await adapter.async_execute(_action(desired()))
        assert result.applied
        assert hass.head_targets["cool"] == 17 and hass.zone_targets["cool"] == [17, 19]
        # A fresh adapter after reload receives the immutable original records.
        control = {"main_state": main, "zone_states": zones}
        adapter = DaikinHVACAdapter(hass, entry_data(supported))
        adapter.set_zone_control_context({}, control)
        refresh_snapshot = adapter.takeover_snapshot()[1]
        assert [refresh_snapshot[zone]["target_temperature"] for zone in ZONES] == [17, 19]
        tx = HVACOwnershipTransaction({"hvac_control": control}, datetime.now(UTC))
        prepared = tx.prepare(desired(), {}, refresh_snapshot, adapter.main_takeover_snapshot())
        assert prepared["hvac_control"]["zone_states"] == zones
        assert prepared["hvac_control"]["main_state"] == main
        result = await adapter.async_restore(automations, zones, main)
        assert result.applied and result.rollback_succeeded
        assert not result.saved_main_state and not result.saved_zone_states
        assert hass.mode == "cool" and hass.head_targets["cool"] == 23 and hass.zone_targets["cool"] == [24, 24]
        assert all(hass.states.get(entity).state == "on" for entity in AUTOMATIONS)
    asyncio.run(run())


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("bounds", ["incompatible", "unavailable", "missing", "malformed"])
def test_legacy_pending_record_is_preserved_without_waking_head(supported, bounds):
    hass = RememberedDaikin()
    zones = {zone: {"target_temperature": 24} for zone in ZONES}
    zones.update(dict(zip(PAIRS, FIXTURE["original_power"], strict=True)))
    main = deepcopy(FIXTURE["legacy_main"])
    for zone in ZONES:
        attrs = hass.states.get(zone).attributes
        if bounds == "unavailable":
            attrs.update(min_temp=None, max_temp=None)
        elif bounds == "missing":
            attrs.clear()
        elif bounds == "malformed":
            attrs.update(min_temp="bad", max_temp=21)
    for _ in range(3):
        adapter = DaikinHVACAdapter(hass, entry_data(supported))
        result = asyncio.run(adapter.async_restore({}, zones, main))
        assert not result.applied and result.rollback_succeeded is False
        expected_zones = zones if supported else {zone: zones[zone] for zone in ZONES}
        assert result.saved_zone_states == expected_zones
        assert {key: result.saved_main_state[key] for key in FIXTURE["legacy_main"]} == FIXTURE["legacy_main"]
        assert result.reason == ("hvac_zone_restore_operator_required" if bounds == "incompatible"
                                 else "hvac_zone_restore_pending")
        assert bool(result.saved_main_state.get("zone_recovery_blocker")) is (bounds == "incompatible")
        assert not hass.services.calls and hass.mode == "off"
        main = result.saved_main_state


def test_asynchronous_vendor_clamp_cannot_claim_restoration():
    async def run():
        hass = RememberedDaikin("cool")
        hass.power = dict.fromkeys(PAIRS, "on")
        hass.publish()
        adapter = DaikinHVACAdapter(hass, entry_data())
        _, zones = adapter.takeover_snapshot()
        main = adapter.main_takeover_snapshot()
        assert (await adapter.async_execute(_action(desired()))).applied
        hass.clamp_zone = True
        result = await adapter.async_restore({}, zones, main)
        assert not result.applied and result.saved_zone_states
        assert hass.head_targets["cool"] == 23 and hass.zone_targets["cool"] == [23, 23]
        assert result.saved_main_state["target_temperature"] == 23
    asyncio.run(run())


def test_explicit_user_supersession_resolves_legacy_record_without_replaying_stale_targets():
    hass = RememberedDaikin()
    adapter = DaikinHVACAdapter(hass, entry_data())
    zones = {zone: {"target_temperature": 24} for zone in ZONES}
    zones.update(dict(zip(PAIRS, FIXTURE["original_power"], strict=True)))
    main = deepcopy(FIXTURE["legacy_main"])
    persisted = []
    adapter.set_zone_manual_override_check(lambda: set(zones))

    async def persist(main_superseded, entity_ids):
        persisted.append((main_superseded, entity_ids))

    adapter.set_manual_supersession_persistence_callback(persist)
    result = asyncio.run(adapter.async_restore({}, zones, main))
    assert result.applied and not result.saved_zone_states and not result.saved_main_state
    assert persisted and not hass.services.calls and hass.mode == "off"


def test_operator_handoff_retires_only_explicitly_superseded_pairs_durably():
    from test_executor import FakeStore

    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        hass = RememberedDaikin()
        store = FakeStore()
        zones = {zone: {"target_temperature": 24} for zone in ZONES}
        zones.update(dict(zip(PAIRS, FIXTURE["original_power"], strict=True)))
        store.data["ownership"] = {"hvac_control": {
            "main_state": deepcopy(FIXTURE["legacy_main"]), "zone_states": zones,
            "required_evidence_lost": "hvac_release_failed",
        }}
        executor = Executor(store, hass=hass, entry_data=entry_data())
        first = await executor.async_release_hvac_control("climate_zone_changed", preserve_zone_entity_id=ZONES[0])
        assert str(first.result) == "failed"
        remaining = store.data["ownership"]["hvac_control"]
        assert remaining["zone_states"] == {ZONES[1]: {"target_temperature": 24}, "switch.bed": "off"}
        assert remaining["main_state"]["zone_recovery_blocker"] == "saved_zone_targets_incompatible_with_off_baseline"
        assert not hass.services.calls
        final = await executor.async_release_hvac_control("climate_zone_changed", preserve_zone_entity_id=ZONES[1])
        assert str(final.result) == "restored"
        assert "hvac_control" not in store.data["ownership"]
        assert not hass.services.calls and hass.mode == "off" and store.flush_count >= 2
    asyncio.run(run())


@pytest.mark.parametrize("zone_mode", ["heat", "off", "unavailable"])
def test_a_new_visible_zone_target_requires_matching_mode_provenance(zone_mode):
    hass = RememberedDaikin("cool")
    hass.power = dict.fromkeys(PAIRS, "on")
    hass.publish()
    hass.states.values[ZONES[0]].state = zone_mode
    result = asyncio.run(DaikinHVACAdapter(hass, entry_data()).async_execute(_action(desired())))
    assert not result.applied and not hass.services.calls
    assert result.reason == ("climate_zone_unavailable" if zone_mode == "unavailable"
                             else "climate_zone_baseline_mode_mismatch")


def test_known_same_mode_baselines_survive_a_planner_stop_and_new_adapter():
    async def run():
        hass = RememberedDaikin("cool")
        hass.power = dict.fromkeys(PAIRS, "on")
        hass.publish()
        adapter = DaikinHVACAdapter(hass, entry_data())
        _, zones = adapter.takeover_snapshot()
        main = adapter.main_takeover_snapshot()
        hass.mode = "off"
        hass.publish()
        adapter = DaikinHVACAdapter(hass, entry_data())
        adapter.set_zone_control_context({}, {"main_state": main, "zone_states": zones})
        records = []
        adapter.set_command_callback(records.append)
        assert (await adapter.async_execute(_action(desired()))).applied
        result = await adapter.async_restore({}, zones, main)
        assert result.applied and hass.mode == "cool"
        assert hass.zone_targets["cool"] == [24, 24]
        assert not any(record.get("stage") == "zone_bounds_pending" for record in records)
    asyncio.run(run())


def test_first_legacy_release_restores_off_and_schedules_then_stops_mode_probing():
    async def run():
        hass = RememberedDaikin("cool")
        hass.power = dict.fromkeys(PAIRS, "on")
        hass.head_targets["cool"] = 17
        hass.zone_targets["cool"] = [17, 19]
        hass.publish()
        hass.states.values.update(dict.fromkeys(AUTOMATIONS, "off"))
        adapter = DaikinHVACAdapter(hass, entry_data())
        zones = {zone: {"target_temperature": 24} for zone in ZONES}
        zones.update(dict(zip(PAIRS, FIXTURE["original_power"], strict=True)))
        main = deepcopy(FIXTURE["legacy_main"])
        main.pop("zone_recovery_attempted")
        result = await adapter.async_restore(AUTOMATIONS, zones, main)
        assert not result.applied and result.reason == "hvac_zone_restore_operator_required"
        assert hass.mode == "off" and hass.head_targets["heat"] == 19
        assert all(hass.states.get(entity).state == "on" for entity in AUTOMATIONS)
        assert result.saved_zone_states == zones
        assert result.saved_main_state["zone_recovery_attempted"]
        assert not any(payload["entity_id"] in ZONES for _, _, payload in hass.services.calls)
        count = len(hass.services.calls)
        result = await DaikinHVACAdapter(hass, entry_data()).async_restore(
            {}, result.saved_zone_states, result.saved_main_state,
        )
        assert result.reason == "hvac_zone_restore_operator_required" and len(hass.services.calls) == count
    asyncio.run(run())


def test_user_zone_supersession_during_target_restoration_keeps_the_users_target():
    async def run():
        hass = RememberedDaikin("cool")
        hass.power = dict.fromkeys(PAIRS, "on")
        hass.publish()
        adapter = DaikinHVACAdapter(hass, entry_data())
        _, zones = adapter.takeover_snapshot()
        main = adapter.main_takeover_snapshot()
        assert (await adapter.async_execute(_action(desired()))).applied
        changed = set()
        persisted = []
        adapter.set_zone_manual_override_check(lambda: changed)

        async def save(main_superseded, entity_ids):
            persisted.append((main_superseded, set(entity_ids)))

        adapter.set_manual_supersession_persistence_callback(save)
        original = hass.services.async_call

        def supersede():
            changed.update({ZONES[1], "switch.bed"})
            hass.zone_targets["cool"][1] = 22
            hass.publish()

        async def dispatch(domain, service, payload, **kwargs):
            await original(domain, service, payload, **kwargs)
            if payload["entity_id"] == HEAD and service == "set_temperature":
                asyncio.get_running_loop().call_later(hass.delay, supersede)

        hass.services.async_call = dispatch
        result = await adapter.async_restore({}, zones, main)
        assert result.applied and hass.zone_targets["cool"] == [24, 22]
        assert persisted and persisted[-1][1] == changed
    asyncio.run(run())


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("failed_zone", ZONES)
def test_failed_owned_refresh_restores_pre_command_targets_without_losing_original_baselines(supported, failed_zone):
    async def run():
        hass = RememberedDaikin("cool")
        hass.power = dict.fromkeys(PAIRS, "on")
        hass.publish()
        adapter = DaikinHVACAdapter(hass, entry_data(supported))
        _, original_zones = adapter.takeover_snapshot()
        original_main = adapter.main_takeover_snapshot()
        assert (await adapter.async_execute(_action(desired()))).applied
        previous = {"hvac_control": {
            "main_state": original_main, "zone_states": original_zones, "main_state_committed": True,
        }}
        adapter = DaikinHVACAdapter(hass, entry_data(supported))
        adapter.set_zone_control_context({}, previous["hvac_control"])
        attempt_zones = adapter.takeover_snapshot()[1]
        next_desired = desired()
        next_desired["zone_targets"] = dict.fromkeys(ZONES, 18)
        tx = HVACOwnershipTransaction(previous, datetime.now(UTC))
        prepared = tx.prepare(next_desired, {}, attempt_zones, adapter.main_takeover_snapshot())
        assert prepared["hvac_control"]["zone_states"] == original_zones
        original_call = hass.services.async_call

        async def reject_selected_zone(domain, service, payload, **kwargs):
            if payload["entity_id"] == failed_zone and service == "set_temperature" and payload["temperature"] == 18:
                raise OSError("sanitized refresh failure")
            await original_call(domain, service, payload, **kwargs)

        hass.services.async_call = reject_selected_zone
        result = await adapter.async_execute(_action(next_desired))
        assert not result.applied and result.rollback_succeeded
        assert result.reason == "hvac_control_service_failed"
        assert not result.saved_zone_states and not result.saved_main_state
        assert hass.head_targets["cool"] == 17 and hass.zone_targets["cool"] == [17, 19]
        completed = tx.complete(result, prepared, next_desired, main_state_superseded=False,
                                superseded_zone_entity_ids=set())
        assert completed == previous
        final = await DaikinHVACAdapter(hass, entry_data(supported)).async_restore({}, original_zones, original_main)
        assert final.applied
        assert hass.head_targets["cool"] == 23 and hass.zone_targets["cool"] == [24, 24]
    asyncio.run(run())
