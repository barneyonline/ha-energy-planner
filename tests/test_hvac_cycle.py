"""Post-release mode timing must survive ownership cleanup and reload."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_constraints import _action, _context, _plan
from test_executor import FakeStore, _arm_store
from test_hvac_adapter import FakeHass, FakeState
from test_planner import _context as planner_context
from test_storage import FakeStore as DiskStore

from custom_components.ha_energy_planner import hvac_adapter as adapter_module
from custom_components.ha_energy_planner import storage as storage_module
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.constraints import ConstraintValidator
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
from custom_components.ha_energy_planner.hvac_cycle import (
    cycle_active,
    instant,
    mode_intent,
    reconcile_cycle,
    record_cycle,
)
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, DecisionSlot
from custom_components.ha_energy_planner.ownership import OwnershipState
from custom_components.ha_energy_planner.planner import DryRunPlanner
from custom_components.ha_energy_planner.storage import PlannerStore

STOP = datetime(2026, 10, 25, 4, 53, 16, 502000, tzinfo=UTC)
HEAD = "climate.daikin"
OPTIONS = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "climate_control_enabled": True,
           "hvac_min_cycle_minutes": 20, "command_rate_limit_seconds": 0}


def stopped():
    return {"entity_id": HEAD, "mode": "off", "changed_at": STOP.isoformat()}


def mode_record(service="turn_off", previous="heat", stage="intent"):
    return {"entity_id": HEAD, "service": service, "previous_mode": previous, "stage": stage,
            "context_id": "stop-1", "issued_at": (STOP - timedelta(seconds=2)).isoformat(),
            "deadline": (STOP + timedelta(minutes=3)).isoformat(), "observed": {"hvac_mode": "off"}}


def wire(executor, adapter, pending):
    executor._configure_pending_hvac_adapter(adapter, pending)


def test_cycle_boundary_and_confirmation_use_feedback_without_refresh_extension():
    record = mode_record()
    pending = record_cycle({}, record, STOP)
    assert pending["pending"]["context_id"] == "stop-1"
    record.update(stage="confirmed", feedback_changed_at=STOP)
    confirmed = record_cycle(pending, record, STOP + timedelta(seconds=30))
    assert instant(confirmed["changed_at"]) == STOP
    assert "pending" not in confirmed
    assert record_cycle(confirmed, record, STOP + timedelta(minutes=5)) == confirmed
    assert cycle_active(confirmed, STOP + timedelta(minutes=19, seconds=59), 20, "heat", "off")
    assert not cycle_active(confirmed, STOP + timedelta(minutes=20), 20, "heat", "off")
    assert not cycle_active(confirmed, STOP, 0, "heat", "off")
    assert not cycle_active(confirmed, STOP, 20, "off", "heat")
    assert not cycle_active(confirmed, STOP, 20, None, "heat")
    assert not cycle_active(confirmed, STOP, 20, "heat", "heat")
    assert cycle_active(confirmed, STOP, 20, "cool", "heat")


@pytest.mark.parametrize("service,previous,requested", [
    ("set_temperature", "heat", {"temperature": 23}),
    ("turn_on", "heat", {}), ("turn_off", "off", {}),
    ("set_hvac_mode", "heat", {"hvac_mode": "heat"}),
])
def test_noop_targets_and_continuations_do_not_restart_cycle(service, previous, requested):
    record = {**mode_record(service, previous), "requested": requested}
    assert not mode_intent(record)
    assert record_cycle(stopped(), record, STOP + timedelta(minutes=2)) == stopped()


def test_uncertain_commands_require_feedback_after_the_reconciliation_lease():
    record = mode_record()
    cycle = record_cycle({}, record, STOP)
    # Even apparent same-mode state is not sufficient proof while unresolved.
    assert cycle_active(cycle, STOP + timedelta(minutes=2), 20, "heat", "heat")
    assert cycle_active(cycle, STOP + timedelta(minutes=22), 20, "heat", "off")
    assert cycle_active(cycle, STOP + timedelta(minutes=23), 20, "heat", "off")
    assert cycle_active({"pending": {}}, STOP, 20, "heat", "off")
    failed = {**record, "stage": "readback_failed"}
    assert record_cycle(cycle, failed, STOP) == cycle
    confirmed = {**record, "stage": "confirmed", "feedback_changed_at": STOP,
                 "observed": {"hvac_mode": "off"}}
    assert record_cycle(cycle, {**confirmed, "context_id": "stale"}, STOP) == cycle
    assert record_cycle(cycle, {**confirmed, "observed": {}}, STOP) == cycle
    assert instant(record_cycle(cycle, confirmed, STOP)["changed_at"]) == STOP
    unchanged = record_cycle(cycle, {**confirmed, "observed": {"hvac_mode": "heat"}}, STOP)
    assert unchanged == cycle
    assert instant("bad") is None
    assert instant(STOP.replace(tzinfo=None)) is None


@pytest.mark.parametrize("economic", [False, True])
def test_constraints_block_new_and_same_lifecycle_restart_but_allow_target_continuation(economic):
    now = STOP + timedelta(seconds=104.971)
    context = _context(now)
    context.current_hvac_mode = "off"
    context.hvac_cycle = stopped()
    desired = {"hvac_mode": "heat", "target_temperature": 24, "lifecycle_id": "new"}
    if economic:
        desired["economic_policy_version"] = 1
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, desired)
    validator = ConstraintValidator(OPTIONS)
    assert "hvac_min_cycle_active" in validator.validate_action(context, _plan(now, action), action, now=now)
    context.hvac_control = {"phase": "preconditioning", "mode": "heat", "lifecycle_id": "new"}
    ownership = OwnershipState(planner_takeover_started_at=STOP, hvac_control_phase="preconditioning")
    assert "hvac_min_cycle_active" in validator.validate_action(
        context, _plan(now, action), action, now=now, ownership=ownership)
    context.current_hvac_mode = "heat"
    assert "hvac_min_cycle_active" not in validator.validate_action(context, _plan(now, action), action, now=now)


@pytest.mark.parametrize("release_reason", ["hvac_tariff_period_changed", "hvac_required_evidence_lost",
                                          "manual_hvac_override"])
def test_real_restoration_clears_ownership_retains_stop_and_blocks_same_plan_and_reload(monkeypatch, release_reason):
    clock = [STOP]
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: clock[0])
    hass = FakeHass({HEAD: FakeState("heat", {"temperature": 24})})
    service_call = hass.services.async_call

    async def retaining_targets(domain, service, data, **kwargs):
        attributes = dict(hass.states.get(HEAD).attributes)
        await service_call(domain, service, data, **kwargs)
        if domain == "climate" and service in {"turn_on", "turn_off"}:
            hass.states.values[HEAD] = FakeState("off" if service == "turn_off" else "heat", attributes)

    monkeypatch.setattr(hass.services, "async_call", retaining_targets)
    store = FakeStore()
    store.data["ownership"] = {"planner_takeover_started_at": STOP - timedelta(minutes=15),
                               "hvac_control": {"main_state": {"hvac_mode": "off", "target_temperature": 20},
                                                "main_state_committed": True, "lifecycle_id": "old"}}
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    _arm_store(store, executor)
    action = _action(STOP, ActionAsset.DAIKIN, ActionKind.SET_HVAC,
                     {"hvac_mode": "heat", "target_temperature": 24, "lifecycle_id": "new"})
    release = replace(action, action_id="release", kind=ActionKind.RELEASE_HVAC,
                      desired_state={"release_reason": release_reason})
    plan = replace(_plan(STOP, action), actions=[release, action])
    asyncio.run(executor.async_evaluate(plan))
    assert hass.states.get(HEAD).state == "off"
    assert store.data["ownership"] == {}
    assert instant(store.data["hvac_cycle"]["changed_at"]) == STOP
    issued = len(hass.services.calls)
    # Reuse the same stale plan after its release has completed.
    asyncio.run(executor.async_evaluate(_plan(STOP, action)))
    assert store.data["outcomes"][-1].reason == "hvac_min_cycle_active"
    assert len(hass.services.calls) == issued
    # Persisted dictionary survives a new executor and the audited 104.971s gap.
    clock[0] = STOP + timedelta(seconds=104.971)
    restored = FakeStore()
    restored.data = deepcopy(store.data)
    restarted = Executor(restored, hass=hass, entry_data=executor.entry_data, options=OPTIONS)
    later = replace(action, execute_not_before=clock[0], execute_not_after=clock[0] + timedelta(minutes=5))
    asyncio.run(restarted.async_evaluate(_plan(clock[0], later)))
    assert restored.data["outcomes"][-1].reason == "hvac_min_cycle_active"
    assert len(hass.services.calls) == issued
    clock[0] = STOP + timedelta(minutes=20)
    assert not restarted._hvac_cycle_active("heat", clock[0])
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(restarted, adapter, {"hvac_mode": "heat"})
    asyncio.run(adapter._async_command(hass, "climate", "turn_on", {"entity_id": HEAD}))
    asyncio.run(adapter._async_confirm_hvac_state(HEAD, {"hvac_mode": "heat"}))
    assert len(hass.services.calls) > issued
    assert instant(restored.data["hvac_cycle"]["changed_at"]) == clock[0]


def test_store_reload_keeps_cycle_separate_from_ownership(monkeypatch):
    monkeypatch.setattr(storage_module, "Store", DiskStore)
    monkeypatch.setattr(DiskStore, "loaded", None)
    monkeypatch.setattr(DiskStore, "loaded_by_key", None)
    store = PlannerStore(object())
    asyncio.run(store.async_save_hvac_cycle(stopped()))
    asyncio.run(store.async_save_ownership({}))
    monkeypatch.setattr(DiskStore, "loaded", deepcopy(DiskStore.saved))
    restarted = PlannerStore(object())
    asyncio.run(restarted.async_load())
    assert restarted.data["ownership"] == {}
    assert restarted.data["hvac_cycle"] == stopped()


def test_service_boundary_rechecks_after_persistence_and_safe_restoration_bypasses_guard(monkeypatch):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    hass = FakeHass({HEAD: FakeState("off", {"temperature": 20})})
    store = FakeStore()
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"hvac_mode": "heat"})
    # A release completed since planning and the earlier executor guard.
    store.data["hvac_cycle"] = stopped()
    with pytest.raises(RuntimeError, match="hvac_min_cycle_active"):
        asyncio.run(adapter._async_command(hass, "climate", "turn_on", {"entity_id": HEAD}))
    assert not hass.services.calls
    # Mandatory restoration of an original active baseline also remains immediate.
    restoring = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, restoring, {"restore_main": {"hvac_mode": "heat"}})
    asyncio.run(restoring._async_command(hass, "climate", "set_hvac_mode",
                                        {"entity_id": HEAD, "hvac_mode": "heat"}))
    assert hass.services.calls[-1][1] == "set_hvac_mode"


def test_failed_service_leaves_durable_uncertainty_and_reconciliation_can_resolve_it(monkeypatch):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    hass = FakeHass({HEAD: FakeState("heat", {"temperature": 20})})
    hass.services.fail_services.add(("climate", "turn_off"))
    store = FakeStore()
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": "off"}})
    with pytest.raises(RuntimeError, match="service failed"):
        asyncio.run(adapter._async_command(hass, "climate", "turn_off", {"entity_id": HEAD}))
    assert "pending" in store.data["hvac_cycle"]
    hass.states.values[HEAD] = FakeState("off", {"temperature": 20})
    reloaded = FakeStore()
    reloaded.data = deepcopy(store.data)
    restarted = Executor(reloaded, hass=hass, entry_data=executor.entry_data, options=OPTIONS)
    recovering = DaikinHVACAdapter(hass, executor.entry_data)
    wire(restarted, recovering, {"restore_main": {"hvac_mode": "off"}})
    asyncio.run(recovering._async_reconcile_recovery())
    assert "pending" not in reloaded.data["hvac_cycle"]
    assert instant(reloaded.data["hvac_cycle"]["changed_at"]) == STOP


@pytest.mark.parametrize("policy", ["legacy", "automatic"])
def test_planning_paths_withhold_new_start_until_rest_expires(policy):
    context = planner_context()
    context.created_at = STOP + timedelta(seconds=104.971)
    context.current_ev_soc_percent = None
    context.current_hvac_mode = "off"
    context.current_hvac_temperature_c = 18.0
    context.occupied_temperature_low_c = 18
    context.occupied_temperature_high_c = 24
    context.slots = [DecisionSlot(valid_at=context.created_at + timedelta(minutes=i), import_price=price,
                                  export_price=0.05, pv_forecast_kw=1, baseline_load_forecast_kw=2)
                     for i, price in [(0, .10), (5, .12), (10, .15), (15, .14), (20, .13), (25, .11), (30, .45)]]
    options = {**OPTIONS, "hvac_precondition_lead_minutes": 30,
               "hvac_precondition_min_price_delta": .20, "hvac_decision_policy": policy}
    baseline = DryRunPlanner(options).create_plan(deepcopy(context))
    assert any(a.asset == ActionAsset.DAIKIN and a.desired_state.get("hvac_mode") == "heat"
               for a in baseline.actions)
    context.hvac_cycle = stopped()
    plan = DryRunPlanner(options).create_plan(context)
    assert not any(a.asset == ActionAsset.DAIKIN and a.desired_state.get("hvac_mode") == "heat"
                   and a.execute_not_before < STOP + timedelta(minutes=20) for a in plan.actions)
    assert context.climate_decision["reason"] == "hvac_min_cycle_active"


def test_active_economic_candidate_cannot_bypass_post_release_rest():
    from test_climate_engine import context, simulation_model

    ctx = context(6)
    # Keep the deterministic economic fixture's time grid, while reproducing
    # the observed off-to-candidate interval in its independent cycle record.
    ctx.current_hvac_mode = "off"
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    ctx.climate_engine = {"status": "active", "ever_active": True, "model": simulation_model(),
                          "modes": {"heat": {"ready": True}}}
    options = {**OPTIONS, "hvac_minimum_saving": .01}
    baseline = DryRunPlanner(options).create_plan(deepcopy(ctx))
    assert any(a.desired_state.get("economic_policy_version") == 1 for a in baseline.actions)
    ctx.hvac_cycle = {**stopped(), "changed_at": (ctx.created_at - timedelta(seconds=104.971)).isoformat()}
    plan = DryRunPlanner(options).create_plan(ctx)
    assert not any(a.asset == ActionAsset.DAIKIN and a.kind != ActionKind.RELEASE_HVAC
                   and a.desired_state.get("hvac_mode") != "off" for a in plan.actions)
    assert ctx.climate_decision["reason"] == "hvac_min_cycle_active"
    assert "scheduled" not in ctx.climate_engine


def test_failed_acquisition_rollback_can_restore_active_baseline_during_rest(monkeypatch):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    hass = FakeHass({HEAD: FakeState("cool", {"temperature": 20})})
    store = FakeStore()
    store.data["hvac_cycle"] = {**stopped(), "mode": "cool"}
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"hvac_mode": "cool"})
    assert asyncio.run(adapter._async_restore_main_state(HEAD, {"hvac_mode": "heat", "target_temperature": 20}))
    assert hass.states.get(HEAD).state == "heat"
    assert store.data["hvac_cycle"]["mode"] == "heat"


def test_dispatch_rechecks_new_boundary_after_guard_await(monkeypatch):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    hass = FakeHass({HEAD: FakeState("off", {"temperature": 20})})
    store = FakeStore()
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"hvac_mode": "heat"})

    async def guard(**kwargs):
        store.data["hvac_cycle"] = stopped()
        return True

    monkeypatch.setattr(adapter, "_async_arm_scheduler_guard", guard)
    with pytest.raises(RuntimeError, match="hvac_min_cycle_active"):
        asyncio.run(adapter._async_command(hass, "climate", "turn_on", {"entity_id": HEAD}))
    assert not hass.services.calls


@pytest.mark.parametrize("restoration", [True, False])
def test_cycle_persistence_failure_cannot_delay_safe_stop_or_baseline_restoration(monkeypatch, restoration):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    hass = FakeHass({HEAD: FakeState("heat", {"temperature": 20})})
    store = FakeStore()
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": "off"}} if restoration else {"hvac_mode": "off"})

    async def disk_failed(cycle):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "async_save_hvac_cycle", disk_failed)
    asyncio.run(adapter._async_command(hass, "climate", "turn_off", {"entity_id": HEAD}))
    asyncio.run(adapter._async_confirm_hvac_state(HEAD, {"hvac_mode": "off"}))
    assert hass.states.get(HEAD).state == "off"
    assert instant(store.data["hvac_cycle"]["changed_at"]) == STOP
    # Fresh automatic acquisition still fails closed if intent cannot be durable.
    hass.states.values[HEAD] = FakeState("off", {"temperature": 20})
    store.data["hvac_cycle"] = {}
    acquiring = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, acquiring, {"hvac_mode": "heat"})
    with pytest.raises(OSError, match="disk unavailable"):
        asyncio.run(acquiring._async_command(hass, "climate", "turn_on", {"entity_id": HEAD}))
    assert all(call[1] != "turn_on" for call in hass.services.calls)


def test_noop_ownership_refresh_cannot_extend_an_expired_confirmed_rest():
    now = STOP + timedelta(minutes=20)
    context = _context(now)
    context.current_hvac_mode = "off"
    context.hvac_cycle = stopped()
    ownership = OwnershipState(planner_takeover_started_at=now - timedelta(minutes=1), hvac_control_phase="away_off")
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC,
                     {"hvac_mode": "heat", "target_temperature": 24})
    violations = ConstraintValidator(OPTIONS).validate_action(
        context, _plan(now, action), action, now=now, ownership=ownership)
    assert "hvac_min_cycle_active" not in violations


@pytest.mark.parametrize("cancelled", [False, True])
def test_proven_unsent_intent_clears_cycle_and_command_uncertainty(monkeypatch, cancelled):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    hass = FakeHass({HEAD: FakeState("off", {"temperature": 20})})
    store = FakeStore()
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"hvac_mode": "heat"})

    async def failed_guard(**kwargs):
        if cancelled:
            raise asyncio.CancelledError
        return False

    monkeypatch.setattr(adapter, "_async_arm_scheduler_guard", failed_guard)
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        asyncio.run(adapter._async_command(hass, "climate", "turn_on", {"entity_id": HEAD}))
    assert not hass.services.calls
    assert "pending" not in store.data["hvac_cycle"]
    assert "command_confirmation" not in store.data["ownership"]["hvac_control"]
    assert not executor._hvac_cycle_active("heat", STOP)


def test_aborted_mandatory_stop_retains_the_earlier_unresolved_start(monkeypatch):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP)
    record = {**mode_record("turn_on", "off"), "context_id": "earlier-start"}
    pending = record_cycle({}, record, STOP)
    hass = FakeHass({HEAD: FakeState("heat", {"temperature": 20})})
    store = FakeStore()
    store.data["hvac_cycle"] = pending
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": "off"}})

    async def failed_guard(**kwargs):
        return False

    monkeypatch.setattr(adapter, "_async_arm_scheduler_guard", failed_guard)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter._async_command(hass, "climate", "turn_off", {"entity_id": HEAD}))
    assert store.data["hvac_cycle"]["pending"] == pending["pending"]
    assert not hass.services.calls


def test_late_stop_feedback_starts_rest_from_the_observed_stop(monkeypatch):
    from types import SimpleNamespace

    late_stop = STOP + timedelta(minutes=22)
    now = STOP + timedelta(minutes=23)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: now)
    cycle = record_cycle({}, mode_record(), STOP)
    hass = FakeHass({})
    hass.states.values[HEAD] = SimpleNamespace(state="off", attributes={"temperature": 20},
                                             last_changed=late_stop, last_reported=now)
    # FakeStates deliberately accepts its typed FakeState only; return native-like feedback directly.
    monkeypatch.setattr(hass.states, "get", lambda entity: hass.states.values.get(entity))
    store = FakeStore()
    store.data["hvac_cycle"] = cycle
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    action = _action(now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {"hvac_mode": "heat", "target_temperature": 24})
    asyncio.run(executor.async_evaluate(_plan(now, action)))
    assert store.data["outcomes"][-1].reason == "hvac_min_cycle_active"
    assert instant(store.data["hvac_cycle"]["changed_at"]) == late_stop
    assert not hass.services.calls
    assert executor._hvac_cycle_active("heat", late_stop + timedelta(minutes=20) - timedelta(microseconds=1))
    assert not executor._hvac_cycle_active("heat", late_stop + timedelta(minutes=20))
    assert store.flush_count > 0


def test_reconciliation_requires_fresh_matching_feedback_even_after_deadline():
    record = {**mode_record("turn_on", "off"), "requested": {}}
    cycle = record_cycle({}, record, STOP)
    now = STOP + timedelta(minutes=24)
    for mode, changed, reported in [(None, STOP, now), ("unknown", STOP, now),
                                    ("off", STOP, None), ("off", STOP, STOP - timedelta(minutes=1)),
                                    ("off", STOP, now + timedelta(seconds=1))]:
        assert reconcile_cycle(cycle, now, mode, changed, reported) == cycle
    assert reconcile_cycle({}, now, "off", STOP, now) == {}
    assert reconcile_cycle({"pending": {}}, now, "off", STOP, now) == {"pending": {}}
    assert reconcile_cycle(cycle, STOP, "off", STOP, STOP) == cycle
    assert reconcile_cycle(cycle, now, "off", STOP - timedelta(minutes=1), now) == cycle
    start = reconcile_cycle(cycle, STOP, "heat", STOP, STOP)
    assert start["mode"] == "heat" and "pending" not in start


@pytest.mark.parametrize("policy", ["legacy", "automatic"])
def test_blocked_fallback_does_not_reserve_hvac_load_or_reduce_ev_capacity(policy):
    ctx = planner_context()
    ctx.created_at = STOP + timedelta(seconds=104.971)
    ctx.ev_ready_by = "05:25"
    ctx.ev_target_soc_percent = 64
    ctx.current_hvac_mode = "off"
    ctx.current_hvac_temperature_c = 18
    ctx.occupied_temperature_low_c = 18
    ctx.occupied_temperature_high_c = 24
    ctx.slots = [DecisionSlot(valid_at=ctx.created_at + timedelta(minutes=i), import_price=price,
                             export_price=.05, pv_forecast_kw=1, baseline_load_forecast_kw=2)
                 for i, price in [(0, .10), (5, .12), (10, .15), (15, .14), (20, .13), (25, .11), (30, .45)]]
    ctx.hvac_cycle = stopped()
    options = {**OPTIONS, "hvac_decision_policy": policy, "hvac_precondition_lead_minutes": 30,
               "hvac_precondition_min_price_delta": .20, "grid_import_limit_kw": 8, "ev_readiness_buffer_minutes": 0}
    reference = deepcopy(ctx)
    reference_options = {**options, "hvac_precondition_lead_minutes": 0}
    reference_plan = DryRunPlanner(reference_options).create_plan(reference)
    plan = DryRunPlanner(options).create_plan(ctx)
    assert not any(a.asset == ActionAsset.DAIKIN and a.kind == ActionKind.SET_HVAC for a in plan.actions)
    assert [slot.projected_hvac_load_kw for slot in ctx.slots] == [0.0] * len(ctx.slots)
    assert [slot.projected_ev_load_kw for slot in ctx.slots] == [slot.projected_ev_load_kw for slot in reference.slots]
    ev = [a.desired_state for a in plan.actions if a.asset == ActionAsset.EV]
    assert ev and any(slot.projected_ev_load_kw > 0 for slot in ctx.slots)
    assert ev == [a.desired_state for a in reference_plan.actions if a.asset == ActionAsset.EV]


def test_missing_head_feedback_cannot_expire_a_persisted_uncertain_command(monkeypatch):
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: STOP + timedelta(hours=1))
    store = FakeStore()
    store.data["hvac_cycle"] = record_cycle({}, mode_record(), STOP)
    cycle = deepcopy(store.data["hvac_cycle"])
    hass = FakeHass({})
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    asyncio.run(executor.async_reconcile_hvac_cycle())
    assert store.data["hvac_cycle"] == cycle
    assert executor._hvac_cycle_active("heat", STOP + timedelta(hours=1))
    assert not hass.services.calls


def test_unchanged_feedback_cannot_retire_a_stop_that_takes_effect_later():
    cycle = record_cycle({}, mode_record(), STOP)
    for minutes in (5, 10, 21):
        now = STOP + timedelta(minutes=minutes)
        assert reconcile_cycle(cycle, now, "heat", STOP - timedelta(minutes=10), now) == cycle
    late_stop = STOP + timedelta(minutes=22)
    now = STOP + timedelta(minutes=25)
    confirmed = reconcile_cycle(cycle, now, "off", late_stop, now)
    assert instant(confirmed["changed_at"]) == late_stop
    assert cycle_active(confirmed, now, 20, "heat", "off")
    assert not cycle_active(confirmed, late_stop + timedelta(minutes=20), 20, "heat", "off")


@pytest.mark.parametrize("baseline,service,requested", [
    ("off", "turn_on", {}), ("heat", "set_hvac_mode", {"hvac_mode": "cool"}),
])
def test_unchanged_baseline_needs_confirmed_compensation_before_uncertainty_retires(
    monkeypatch, baseline, service, requested,
):
    now = STOP + timedelta(minutes=25)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: now)
    hass = FakeHass({HEAD: FakeState(baseline, {"temperature": 20})})
    record = {**mode_record(service, baseline), "requested": requested}
    store = FakeStore()
    store.data["hvac_cycle"] = record_cycle({}, record, STOP)
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": baseline}})
    # A different predicate (the rollback baseline) must not confirm the old command.
    adapter._last_commands[HEAD] = record
    assert asyncio.run(adapter._async_confirm_hvac_state(HEAD, {"hvac_mode": baseline}))
    assert "pending" in store.data["hvac_cycle"]
    assert adapter._unconfirmed_main_mode
    result = asyncio.run(adapter.async_restore({}, {}, {"hvac_mode": baseline}))
    assert result.rollback_succeeded
    assert [call[1] for call in hass.services.calls] == ["turn_off" if baseline == "off" else "set_hvac_mode"]
    assert "pending" not in store.data["hvac_cycle"]
    assert instant(store.data["hvac_cycle"]["changed_at"]) == now
    assert not adapter._unconfirmed_main_mode
    calls = len(hass.services.calls)
    result = asyncio.run(adapter.async_restore({}, {}, {"hvac_mode": baseline}))
    assert result.rollback_succeeded
    assert len(hass.services.calls) == calls
    assert instant(store.data["hvac_cycle"]["changed_at"]) == now


def test_passive_baseline_feedback_cannot_confirm_an_unsent_compensation():
    cycle = record_cycle({}, mode_record("turn_on", "off"), STOP)
    record = {**mode_record("turn_off", "off"), "uncertainty_compensation": True, "context_id": "compensate"}
    compensating = record_cycle(cycle, record, STOP)
    assert reconcile_cycle(compensating, STOP, "off", STOP - timedelta(minutes=5), STOP) == compensating


def test_legacy_pending_mode_recovery_compensates_without_a_cycle_record(monkeypatch):
    now = STOP + timedelta(minutes=25)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: now)
    record = {**mode_record("turn_on", "off"), "domain": "climate", "requested": {}}
    record.pop("previous_mode")  # The earlier release did not persist this field.
    hass = FakeHass({HEAD: FakeState("off", {"temperature": 20})})
    store = FakeStore()
    store.data["ownership"] = {"hvac_control": {"command_confirmation": record}}
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": "off"}})
    assert adapter._unconfirmed_main_mode
    result = asyncio.run(adapter.async_restore({}, {}, {"hvac_mode": "off"}))
    assert result.rollback_succeeded
    assert [call[1] for call in hass.services.calls] == ["turn_off"]
    assert "pending" not in store.data["hvac_cycle"]
    assert instant(store.data["hvac_cycle"]["changed_at"]) == now


def test_aborted_legacy_compensation_preserves_uncertainty_across_another_reload(monkeypatch):
    now = STOP + timedelta(minutes=25)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: now)
    record = {**mode_record("turn_on", "off"), "domain": "climate", "requested": {}}
    hass = FakeHass({HEAD: FakeState("off", {"temperature": 20})})
    store = FakeStore()
    store.data["ownership"] = {"hvac_control": {"command_confirmation": record}}
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": "off"}})

    async def guard_failed(**kwargs):
        return False

    monkeypatch.setattr(adapter, "_async_arm_scheduler_guard", guard_failed)
    assert not asyncio.run(adapter._async_restore_main_state(HEAD, {"hvac_mode": "off"}))
    assert store.data["hvac_cycle"]["pending"]["context_id"] == record["context_id"]
    assert not hass.services.calls
    recovering = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, recovering, {"restore_main": {"hvac_mode": "off"}})
    result = asyncio.run(recovering.async_restore({}, {}, {"hvac_mode": "off"}))
    assert result.rollback_succeeded
    assert [call[1] for call in hass.services.calls] == ["turn_off"]
    assert "pending" not in store.data["hvac_cycle"]


@pytest.mark.parametrize("proof", ["matching", "different_context", "different_entity", "pending"])
def test_independent_confirmation_does_not_resurrect_expired_command_uncertainty(monkeypatch, proof):
    now = STOP + timedelta(minutes=25)
    monkeypatch.setattr(adapter_module.dt_util, "utcnow", lambda: now)
    record = {**mode_record(), "domain": "climate", "requested": {}}
    pending = record_cycle({}, record, STOP)
    confirmed = reconcile_cycle(pending, now, "off", STOP, now)
    assert "pending" not in confirmed
    if proof == "different_context":
        confirmed["confirmed_context_id"] = "other-command"
    elif proof == "different_entity":
        confirmed["entity_id"] = "climate.other"
    elif proof == "pending":
        confirmed["pending"] = {**pending["pending"], "context_id": "newer-unresolved"}
    store = FakeStore()
    store.data["hvac_cycle"] = deepcopy(confirmed)
    store.data["ownership"] = {"hvac_control": {"command_confirmation": record}}
    hass = FakeHass({HEAD: FakeState("off", {"temperature": 20})})
    executor = Executor(store, hass=hass, entry_data={"daikin_climate_entity": HEAD}, options=OPTIONS)
    adapter = DaikinHVACAdapter(hass, executor.entry_data)
    wire(executor, adapter, {"restore_main": {"hvac_mode": "off"}})
    assert adapter._unconfirmed_main_mode is (proof != "matching")
    result = asyncio.run(adapter.async_restore({}, {}, {"hvac_mode": "off"}))
    assert result.rollback_succeeded
    assert len(hass.services.calls) == (0 if proof == "matching" else 1)
    expected_boundary = STOP if proof == "matching" else now
    assert instant(store.data["hvac_cycle"]["changed_at"]) == expected_boundary
    assert executor._hvac_cycle_active("heat", now) is (proof != "matching")
    if proof == "matching":
        # Reusing persisted confirmation after another reload is still a no-op.
        store.data = deepcopy(store.data)
        restarted = Executor(store, hass=hass, entry_data=executor.entry_data, options=OPTIONS)
        retry = DaikinHVACAdapter(hass, executor.entry_data)
        wire(restarted, retry, {"restore_main": {"hvac_mode": "off"}})
        assert asyncio.run(retry.async_restore({}, {}, {"hvac_mode": "off"})).rollback_succeeded
        assert not hass.services.calls
        assert instant(store.data["hvac_cycle"]["changed_at"]) == STOP
