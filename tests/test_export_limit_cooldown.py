"""Five-minute export budget across feedback latency, recovery and aliases."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_export_limit import DATA, ENTITY, NOW, OPTIONS, action, context, limit_state, price_state
from test_export_limit import setup as export_setup

from custom_components.ha_energy_planner import enphase_export_limit as runtime
from custom_components.ha_energy_planner.const import CONF_COMMAND_RATE_LIMIT_SECONDS, CONF_DRY_RUN
from custom_components.ha_energy_planner.export_limit_cooldown import control_key, cooldown_status
from custom_components.ha_energy_planner.export_limit_policy import EXPORT_ASSET, build_actions
from custom_components.ha_energy_planner.models import OutcomeResult


@pytest.fixture
def setup(monkeypatch):
    return export_setup.__wrapped__(monkeypatch)


def move(s, seconds):
    """Advance absolute observation time and publish a new read of the same state."""
    s.clock["now"] = NOW + timedelta(seconds=seconds)
    s.states[ENTITY].attributes["last_successful_readback"] = s.clock["now"].timestamp()
    s.states["sensor.export_price"].last_updated = s.clock["now"]
    return s.clock["now"]


def latest_action(s, negative):
    s.states["sensor.export_price"] = price_state((-1 if negative else 1, -1 if negative else 1))
    s.states["sensor.export_price"].last_updated = s.clock["now"]
    return build_actions(context(runtime.evidence(s.hass, DATA, OPTIONS, s.clock["now"])))[0]


@pytest.mark.parametrize("general,eligible", [(0, 300), (1, 300), (600, 600)])
def test_attempt_confirmation_and_restore_boundaries(setup, general, eligible):
    async def run():
        s = setup
        s.control.options = {CONF_COMMAND_RATE_LIMIT_SECONDS: general}
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        baseline = deepcopy(s.control.state["baseline"])
        # Resume, reload and even lowering options cannot shorten a reservation.
        await s.control.resume()
        s.store.data = json.loads(json.dumps(s.store.data))
        s.control = runtime.ExportLimitControl(s.hass, s.store, DATA)
        for t in (1, 100, 299, eligible - 1):
            result = await s.control.restore(move(s, t))
            assert result == (OutcomeResult.PENDING, "export_limit_change_rate_limited", False)
            assert s.control.state["baseline"] == baseline
            assert not s.control.state.get("pending")
            assert len(s.calls) == 1
        assert (await s.control.restore(move(s, eligible)))[0] == OutcomeResult.RESTORED
        assert len(s.calls) == 2 and not s.control.state
        # Clearing ownership after restore does not reset the device budget.
        assert (await s.control.execute(latest_action(s, True), s.clock["now"]))[2] is False
        assert s.control.cooldown(s.clock["now"])["next_eligible_change_at"] == (
            s.clock["now"] + timedelta(seconds=300)).isoformat()
    asyncio.run(run())


@pytest.mark.parametrize("latency", [0, 152.5, 239.8, 254.6, 720])
def test_late_confirmation_starts_full_observed_wait_once(setup, latency):
    async def run():
        s = setup
        requested = action(s)
        await s.control.execute(requested, NOW)
        if latency >= 600:
            assert await s.control.reconcile(move(s, 600)) == "export_limit_unconfirmed"
        observed = move(s, latency + 10)
        # The matching API read completed ten seconds before Planner saw it.
        s.states[ENTITY] = limit_state(0, readback=(NOW + timedelta(seconds=latency)).timestamp())
        await s.control.reconcile(observed)
        await s.control.resume()
        first_deadline = s.control.cooldown(observed)["next_eligible_change_at"]
        assert first_deadline == (observed + timedelta(seconds=300)).isoformat()
        for delta in (1, 60, 299):
            now = move(s, latency + 10 + delta)
            assert await s.control.reconcile(now) is None
            assert s.control.cooldown(now)["next_eligible_change_at"] == first_deadline
            assert (await s.control.execute(requested, now))[0] == OutcomeResult.SKIPPED
            assert (await s.control.execute(latest_action(s, False), now))[2] is False
        s.mode["value"] = "confirm"
        assert (await s.control.execute(latest_action(s, False), move(s, latency + 310)))[2]
        applied = [x for x in s.store.data["execution_audit"] if x["result"] == "applied"]
        assert len(applied) == 2
        assert applied[-1]["attempted_at"] >= first_deadline
        assert len(s.calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["pending", "timeout", "cancel", "reject"])
def test_uncertain_or_failed_reservations_survive_resume_and_restart(setup, mode):
    async def run():
        s = setup
        s.mode["value"] = mode
        requested = action(s)
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await s.control.execute(requested, NOW)
        else:
            await s.control.execute(requested, NOW)
        s.store.data = json.loads(json.dumps(s.store.data))
        clone = runtime.ExportLimitControl(s.hass, s.store, DATA)
        await clone.resume()
        for t in (1, 299):
            assert not (await clone.execute(requested, move(s, t)))[2]
        assert len(s.calls) == 1
        s.mode["value"] = "confirm"
        sent = (await clone.execute(requested, move(s, 300)))[2]
        assert sent is (mode == "reject")
        if mode != "reject":
            assert clone.state["pending"]
            assert not (await clone.execute(requested, move(s, 900)))[2]
            assert clone.state["pause"] == "export_limit_unconfirmed"
    asyncio.run(run())


def test_latest_tariff_replacement_and_noop_cancellation_through_executor(setup):
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.planner import DryRunPlanner
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        s = setup
        opts = {**OPTIONS, CONF_DRY_RUN: False, CONF_COMMAND_RATE_LIMIT_SECONDS: 1}
        s.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(DATA, opts)
        executor = Executor(s.store, hass=s.hass, entry_data=DATA, options=opts)
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        old_action = latest_action(s, False)
        for t, negative in ((1, False), (60, True), (100, False), (299, True), (300, True), (301, False)):
            now = move(s, t)
            latest_action(s, negative) if not negative else None
            if negative:
                s.states["sensor.export_price"] = price_state((-1, -1))
            ctx = context(runtime.evidence(s.hass, DATA, opts, now))
            ctx.created_at = now
            plan = DryRunPlanner(opts).create_plan(ctx)
            await executor.async_evaluate(plan, ctx)
            assert len(s.calls) == (2 if t == 301 else 1)
        # A stale queued action cannot run against revised tariff evidence.
        s.states["sensor.export_price"] = price_state((-1, -1))
        assert executor._export_limit_rejection_reason(old_action, s.clock["now"]) == "export_tariff_changed"
        assert s.control.state["baseline"]["watts"] is None
        assert [x[1] for x in s.calls] == ["set_export_limit", "disable_export_limit"]
        assert all(x["result"] not in {"applied", "restored"} for x in s.store.data["execution_audit"]
                   if x["reason"] == "export_limit_change_rate_limited")
    asyncio.run(run())


def test_alias_identity_and_return_to_original_device_keep_budget(setup):
    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        alias = "select.renamed_export_limit"
        s.registry[alias] = SimpleNamespace(**{**vars(s.registry[ENTITY]), "domain": "select"})
        s.states[alias] = SimpleNamespace(state="enable_limit", attributes={})
        mapping = {**DATA, "enphase_export_limit_entity": alias}
        clone = runtime.ExportLimitControl(s.hass, s.store, mapping)
        assert (await clone.restore(move(s, 299)))[1] == "export_limit_change_rate_limited"
        assert clone.state["identity"]["entity_id"] == alias
        s.registry[alias].device_id = "replacement"
        s.registry[ENTITY].device_id = "replacement"
        assert await clone.reconcile(s.clock["now"]) == "export_limit_target_changed"
        await clone.resume()
        s.registry[alias].device_id = "gateway-a"
        s.registry[ENTITY].device_id = "gateway-a"
        assert (await clone.restore(s.clock["now"]))[2] is False
        assert len(s.calls) == 1
        assert len([k for k in s.store.data["command_rate_limits"] if k.startswith(EXPORT_ASSET + ":")]) == 2
    asyncio.run(run())


@pytest.mark.parametrize("bad", [None, {}, "bad", {"version": 9}, {"version": 1, "checked_at": float("nan")},
                                    {"version": 1, "checked_at": NOW.timestamp() + 86400},
                                    {"version": 1, "checked_at": NOW.timestamp(), "interval_seconds": 300},
                                    {"version": 1, "checked_at": NOW.timestamp(), "interval_seconds": "bad"},
                                    {"version": 1, "checked_at": NOW.timestamp(), "interval_seconds": float("inf")},
                                    {"version": 1, "checked_at": NOW.timestamp(), "interval_seconds": 299}])
def test_corrupt_or_future_record_waits_once_then_recovers(setup, bad):
    async def run():
        s = setup
        key = control_key(runtime.target_identity(s.hass, ENTITY))
        s.store.data["command_rate_limits"] = {key: bad}
        requested = action(s)
        assert not (await s.control.execute(requested, NOW))[2]
        assert not (await s.control.execute(requested, move(s, 299)))[2]
        assert (await s.control.execute(requested, move(s, 300)))[2]
        assert len(s.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("legacy", [None, "bad", NOW.isoformat(), (NOW + timedelta(days=365)).isoformat()])
def test_legacy_attempt_migration_is_bounded_and_durable(setup, legacy):
    async def run():
        s = setup
        s.store.data["command_rate_limits"] = {EXPORT_ASSET: legacy}
        await s.control.reconcile(NOW)
        requested = action(s)
        assert not (await s.control.execute(requested, move(s, 299)))[2]
        assert (await s.control.execute(requested, move(s, 300)))[2]
    asyncio.run(run())


@pytest.mark.parametrize("general", [1, 600])
def test_backwards_wall_clock_reserves_new_bounded_interval(setup, general):
    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        await s.control.reconcile(move(s, 100))
        key = control_key(runtime.target_identity(s.hass, ENTITY))
        s.store.data["command_rate_limits"][key]["interval_seconds"] = max(general, 300)
        backwards = move(s, -3600)
        assert not (await s.control.restore(backwards))[2]
        assert s.control.cooldown(backwards)["next_eligible_change_at"] == (
            backwards + timedelta(seconds=max(general, 300))).isoformat()
        assert not (await s.control.restore(move(s, -3301)))[2]
        assert (await s.control.restore(move(s, -3600 + max(general, 300))))[2]
        assert len(s.calls) == 2
    asyncio.run(run())


def test_reads_noops_and_unavailable_identity_do_not_reserve_attempts(setup):
    async def run():
        s = setup
        assert cooldown_status(s.store.data, None, {}, NOW) == {}
        unmapped = runtime.ExportLimitControl(s.hass, s.store, {})
        assert await unmapped.prepare_cooldown(NOW) is None
        assert unmapped.cooldown(NOW) == {}
        s.states[ENTITY] = limit_state(0)
        requested = deepcopy(action_for_zero(s))
        for t in (0, 60, 120):
            assert (await s.control.execute(requested, move(s, t)))[0] == OutcomeResult.SKIPPED
            assert s.control.cooldown(s.clock["now"])["next_eligible_change_at"] is None
        s.registry.clear()
        assert await s.control.prepare_cooldown(s.clock["now"]) is None
        assert not s.calls
    asyncio.run(run())


def action_for_zero(s):
    """Construct zero-export intent while feedback already matches it."""
    s.states[ENTITY] = limit_state()
    requested = action(s)
    s.states[ENTITY] = limit_state(0)
    return requested


def test_new_matching_feedback_before_dispatch_is_unsent_and_unattributed(setup):
    async def run():
        s = setup
        original = s.store.async_flush

        async def changed():
            await original()
            s.states[ENTITY] = limit_state(0)

        s.store.async_flush = changed
        result = await s.control.execute(action(s), NOW)
        assert result == (OutcomeResult.REJECTED, "external_export_limit_changed", False)
        assert not s.calls
        assert not s.store.data["execution_audit"]
        assert not s.control.state.get("baseline")
        assert s.control.cooldown(NOW)["next_eligible_change_at"] is None
        assert s.control.cooldown(NOW)["last_change_observed_at"] is None
    asyncio.run(run())


@pytest.mark.parametrize("failure,reason", [
    ("readback_stale", "export_limit_readback_stale"),
    ("readback_future", "export_limit_readback_stale"),
    ("tariff_stale", "export_tariff_stale"),
    ("unsupported", "export_limit_unsupported"),
    ("service", "export_limit_services_unavailable"),
    ("target", "export_limit_target_invalid"),
])
def test_deadline_never_overrides_latest_evidence_blockers(setup, failure, reason):
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.planner import DryRunPlanner
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        s = setup
        opts = {**OPTIONS, CONF_DRY_RUN: False}
        s.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(DATA, opts)
        executor = Executor(s.store, hass=s.hass, entry_data=DATA, options=opts)
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        requested = latest_action(s, False)
        now = move(s, 300)
        if failure == "readback_stale":
            s.states[ENTITY].attributes["last_successful_readback"] = now.timestamp() - 601
        elif failure == "readback_future":
            s.states[ENTITY].attributes["last_successful_readback"] = now.timestamp() + 1
        elif failure == "tariff_stale":
            s.states["sensor.export_price"].last_updated = now - timedelta(minutes=31)
        elif failure == "unsupported":
            s.states[ENTITY].state = "unsupported"
        elif failure == "service":
            s.hass.services.has_service = lambda *args: False
        elif failure == "target":
            s.registry[ENTITY].platform = "other"
        assert executor._export_limit_rejection_reason(requested, now) == reason
        ctx = context(runtime.evidence(s.hass, DATA, opts, now))
        ctx.created_at = now
        await executor.async_evaluate(DryRunPlanner(opts).create_plan(ctx), ctx)
        assert len(s.calls) == 1
    asyncio.run(run())


def test_cooldown_wakeup_diagnostics_and_calendar_show_deferred_plan(setup, monkeypatch):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as coordinator_module
    from custom_components.ha_energy_planner.calendar import _calendar_events
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status
    from custom_components.ha_energy_planner.planner import DryRunPlanner
    from custom_components.ha_energy_planner.sensor import _next_actions_attrs

    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        latest_action(s, False)
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=s.hass)
        owner.store = s.store
        owner._export_boundary_cancel = None
        owner._listener_tasks = set()
        s.hass.async_create_task = asyncio.create_task
        owner.data = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(s.hass, DATA, OPTIONS, NOW)))
        timing = s.control.cooldown(NOW)
        callbacks, cancelled = [], []
        monkeypatch.setattr(coordinator_module, "async_call_later", lambda hass, delay, cb: (
            callbacks.append((delay, cb)) or (lambda: cancelled.append(True))))
        owner._schedule_export_boundary({**owner.data.device_plans[EXPORT_ASSET], **timing})
        assert callbacks[-1][0] == 300
        # Restoration still receives a wakeup with disabled policy and no tariff blocks.
        owner._schedule_export_boundary(timing)
        assert callbacks[-1][0] == 300 and cancelled == [True]
        status = export_limit_status(owner)
        assert not status["ready"] and status["reason"] == "export_limit_change_rate_limited"
        assert status["last_change_observed_at"] == NOW.timestamp()
        attrs = _next_actions_attrs(owner)["export_limit"]
        assert attrs["next_eligible_change_at"] == timing["next_eligible_change_at"]
        events = _calendar_events(owner)
        assert events and all("deferred" in event.summary for event in events)
        assert all("Next eligible setting change:" in event.description for event in events)
        assert all("export_limit_change_rate_limited" in event.description for event in events)
        callbacks[-1][1](NOW + timedelta(seconds=300))
        assert owner._force_next_refresh
        assert owner._pending_refresh_trigger == "export_tariff_boundary"
        owner._schedule_export_boundary({**timing, "next_eligible_change_at": NOW.isoformat()})
        assert callbacks[-1][0] == 0
        owner._schedule_export_boundary({})
        assert owner._export_boundary_cancel is None
    asyncio.run(run())


@pytest.mark.parametrize("stamp", ["invalid", None, float("nan"), NOW.timestamp() + 86400])
def test_invalid_pending_request_time_requires_fresh_match_before_new_budget(setup, stamp):
    async def run():
        s = setup
        await s.control.execute(action(s), NOW)
        s.store.data["ownership"][EXPORT_ASSET]["pending"]["sent_at"] = stamp
        now = move(s, 1)
        assert await s.control.reconcile(now) == "export_limit_pending"
        assert s.control.state["pending"]["request_clock_recovered"]
        assert len(s.calls) == 1
        s.states[ENTITY] = limit_state(0, readback=now.timestamp())
        assert await s.control.reconcile(now) is None
        assert s.control.cooldown(now)["next_eligible_change_at"] == (now + timedelta(seconds=300)).isoformat()
        assert not (await s.control.restore(move(s, 300)))[2]
        assert len(s.calls) == 1
    asyncio.run(run())


def test_real_store_flushes_reservation_before_service_and_retains_after_reload(setup, monkeypatch):
    from test_storage import FakeStore

    from custom_components.ha_energy_planner import storage as storage_module

    async def run():
        s = setup
        monkeypatch.setattr(storage_module, "Store", FakeStore)
        monkeypatch.setattr(FakeStore, "loaded_by_key", None)
        monkeypatch.setattr(FakeStore, "saved", None)
        store = storage_module.PlannerStore(s.hass, "export-entry")
        control = runtime.ExportLimitControl(s.hass, store, DATA)
        original_call = s.hass.services.async_call
        s.store.flushes = 1
        key = control_key(runtime.target_identity(s.hass, ENTITY))

        async def call(*args, **kwargs):
            saved = FakeStore.saved
            assert saved["ownership"][EXPORT_ASSET]["pending"]
            assert saved["command_rate_limits"][key]["attempted_at"] == NOW.timestamp()
            await original_call(*args, **kwargs)

        s.hass.services.async_call = call
        # A coordinator may batch ordinary saves; dispatch must force them out.
        async with store.async_delay_save():
            await control.execute(action(s), NOW)
        s.states[ENTITY] = limit_state(0)
        await control.reconcile(NOW)
        monkeypatch.setattr(FakeStore, "loaded", json.loads(json.dumps(FakeStore.saved)))
        reloaded = storage_module.PlannerStore(s.hass, "export-entry")
        await reloaded.async_load()
        clone = runtime.ExportLimitControl(s.hass, reloaded, DATA)
        assert not (await clone.restore(move(s, 299)))[2]
        assert reloaded.data["command_rate_limits"][key]["confirmed_at"] == NOW.timestamp()
        assert len(s.calls) == 1
    asyncio.run(run())


def test_slow_durable_dispatch_rejects_pre_dispatch_readback(setup):
    async def run():
        s = setup
        requested = action(s)
        original = s.store.async_flush

        async def delayed():
            await original()
            move(s, 240)

        async def call(*args, **kwargs):
            s.calls.append(args)
            s.states[ENTITY] = limit_state(0, readback=(NOW + timedelta(seconds=100)).timestamp())

        s.store.async_flush = delayed
        s.hass.services.async_call = call
        assert (await s.control.execute(requested, NOW))[2]
        assert s.control.state["pending"]["confirmation_not_before"] == (NOW + timedelta(seconds=240)).timestamp()
        assert not s.control.cooldown(s.clock["now"])["last_change_observed_at"]
        assert await s.control.reconcile(s.clock["now"]) == "export_limit_pending"
        s.store.async_flush = original
        await s.control.reconcile(move(s, 250))
        assert s.control.cooldown(s.clock["now"])["next_eligible_change_at"] == (
            NOW + timedelta(seconds=550)).isoformat()
        assert len(s.calls) == 1
    asyncio.run(run())


def test_crash_with_reserved_request_requires_new_read_after_restart(setup):
    async def run():
        s = setup
        await s.control.execute(action(s), NOW)
        # Simulate the last durable generation before crossing the service.
        s.store.data["ownership"][EXPORT_ASSET]["pending"]["confirmation_not_before"] = None
        s.store.data = json.loads(json.dumps(s.store.data))
        clone = runtime.ExportLimitControl(s.hass, s.store, DATA)
        now = move(s, 100)
        s.states[ENTITY] = limit_state(0, readback=NOW.timestamp())
        assert await clone.reconcile(now) == "export_limit_pending"
        assert not clone.cooldown(now)["last_change_observed_at"]
        now = move(s, 101)
        assert await clone.reconcile(now) is None
        assert clone.cooldown(now)["next_eligible_change_at"] == (now + timedelta(seconds=300)).isoformat()
        assert not (await clone.restore(move(s, 400)))[2]
        assert len(s.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("restore", [False, True])
def test_stricter_interval_edit_during_flush_blocks_dispatch(setup, restore):
    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        requested = latest_action(s, False)
        move(s, 300)
        original = s.store.async_flush

        async def edited():
            await original()
            s.control.options[CONF_COMMAND_RATE_LIMIT_SECONDS] = 600

        s.store.async_flush = edited
        result = (await s.control.restore(s.clock["now"]) if restore
                  else await s.control.execute(requested, s.clock["now"]))
        assert result[1:] == ("export_limit_change_rate_limited", False)
        assert len(s.calls) == 1
        assert not s.control.state.get("pending")
        assert s.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
    asyncio.run(run())


def test_late_external_settlement_cannot_shorten_uncertain_request_wait(setup):
    async def run():
        s = setup
        await s.control.execute(action(s), NOW)
        interrupted = deepcopy(s.control.state)
        now = move(s, 254.6)
        s.states[ENTITY] = limit_state(0, slew=77, readback=now.timestamp())
        assert await s.control.reconcile(now) is None
        assert not s.control.state.get("pending") and not s.control.state.get("baseline")
        assert s.store.data["execution_audit"][-1]["result"] == "rejected"
        # This is an external settlement, not Planner-confirmed physical control.
        assert s.control.cooldown(now)["last_change_observed_at"] is None
        assert s.control.cooldown(now)["last_external_settlement_observed_at"] == now.timestamp()
        await s.control.save(interrupted)
        assert await s.control.reconcile(move(s, 255)) is None
        requested = latest_action(s, False)
        s.mode["value"] = "confirm"
        assert not (await s.control.execute(requested, move(s, 300)))[2]
        assert not (await s.control.execute(requested, move(s, 554.599)))[2]
        assert len(s.calls) == 1
        assert (await s.control.execute(requested, move(s, 554.6)))[2]
        assert len(s.calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("elapsed", [300, 301])
@pytest.mark.parametrize("with_blocks", [False, True])
@pytest.mark.parametrize("still_needed", [False, True])
def test_expiry_during_refresh_replans_once_using_latest_intent(setup, monkeypatch, elapsed, with_blocks, still_needed):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as coordinator_module

    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=s.hass)
        owner.store = s.store
        owner._listener_tasks = set()
        s.hass.async_create_task = asyncio.create_task
        cancelled, scheduled, refreshes = [], [], []
        owner._export_boundary_cancel = lambda: cancelled.append(True)
        move(s, 299)
        captured = {**runtime.evidence(s.hass, DATA, OPTIONS, s.clock["now"]),
                    **s.control.cooldown(s.clock["now"])}
        if not with_blocks:
            captured["blocks"] = []
        assert captured["deferred"]
        # Input collection/storage can cross eligibility after evidence was captured.
        now = move(s, elapsed)
        monkeypatch.setattr(coordinator_module, "async_call_later", lambda hass, delay, cb: (
            scheduled.append((delay, cb)) or (lambda: None)))

        async def refresh():
            refreshes.append(True)
            s.states["sensor.export_price"] = price_state((1, 1) if still_needed else (-1, -1))
            s.states["sensor.export_price"].last_updated = now
            fresh = runtime.evidence(s.hass, DATA, OPTIONS, now)
            for desired in build_actions(context(fresh)):
                await s.control.execute(desired, now)
            fresh.update(s.control.cooldown(now))
            if not with_blocks:
                fresh["blocks"] = []
            owner._schedule_export_boundary(fresh)

        owner.async_request_refresh = refresh
        owner._schedule_export_boundary(captured)
        assert cancelled == [True]
        assert scheduled and scheduled[-1][0] == 0
        scheduled[-1][1](now)
        await asyncio.sleep(0)
        assert refreshes == [True]
        assert len(s.calls) == (2 if still_needed else 1)
        assert sum(delay == 0 for delay, _ in scheduled) == 1
        if still_needed:
            assert scheduled[-1][0] == 300
            assert s.calls[-1][1] == "disable_export_limit"
        elif with_blocks:
            assert scheduled[-1][0] > 0
        else:
            assert owner._export_boundary_cancel is None
    asyncio.run(run())


@pytest.mark.parametrize("baseline", [None, 3000])
@pytest.mark.parametrize("confirmed_before_crash", [False, True])
@pytest.mark.parametrize("rollback", [200, -3600])
def test_clock_repair_retains_restoration_audit_identity(setup, baseline, confirmed_before_crash, rollback):
    async def run():
        s = setup
        s.states[ENTITY] = limit_state(baseline)
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        s.mode["value"] = "pending"
        await s.control.restore(move(s, 300))
        interrupted = deepcopy(s.control.state)
        request = interrupted["pending"]
        expected_id = f"{request['action_id']}-{request['sent_at']}"
        if confirmed_before_crash:
            now = move(s, 310)
            s.states[ENTITY] = limit_state(baseline, readback=now.timestamp())
            await s.control.reconcile(now)
        # Model the durable generation left by a crash before ownership clears.
        persisted = []
        save = s.store.async_save_ownership

        async def capture(ownership):
            export = ownership.get(EXPORT_ASSET, {})
            if export.get("pending"):
                persisted.append(deepcopy(export))
            await save(ownership)

        s.store.async_save_ownership = capture
        replay = interrupted
        original_history = (deepcopy(s.store.data["calendar_history"]["history"])
                            if confirmed_before_crash else None)
        for elapsed in (rollback, rollback - 1):
            await s.control.save(replay)
            now = move(s, elapsed)
            s.states[ENTITY] = limit_state(baseline, readback=now.timestamp())
            assert await s.control.reconcile(now) is None
            history = s.store.data["calendar_history"]["history"]
            assert len(history) == 2 and len(s.calls) == 2
            restored = [row for row in history if "Restored" in row["summary"]]
            assert len(restored) == 1 and restored[0]["uid"] == expected_id
            if original_history is not None:
                assert history == original_history
            original_history = deepcopy(history)
            # A further rollback replays the newly repaired durable request.
            replay = deepcopy(persisted[-1])
            assert replay["pending"]["audit_action_id"] == expected_id
            assert s.control.cooldown(now)["next_eligible_change_at"] == (
                now + timedelta(seconds=300)).isoformat()
    asyncio.run(run())


@pytest.mark.parametrize("confirmed_watts", [None, 0, 3000])
@pytest.mark.parametrize("price", [-0.1, 0.2])
@pytest.mark.parametrize("pv_kw", [2.0, 5.0])
def test_deferred_preview_and_cost_use_confirmed_export_limit(setup, confirmed_watts, price, pv_kw):
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    async def run():
        s = setup
        s.states[ENTITY] = limit_state(confirmed_watts if confirmed_watts != 0 else None)
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        if confirmed_watts != 0:
            assert (await s.control.restore(move(s, 300)))[0] == OutcomeResult.RESTORED
        now = s.clock["now"]
        s.states["sensor.export_price"] = price_state((price, price))
        s.states["sensor.export_price"].last_updated = now
        evidence = runtime.evidence(s.hass, DATA, OPTIONS, now)
        evidence.update(ownership=s.control.state, **s.control.cooldown(now))
        assert evidence["deferred"] and evidence["feedback"]["watts"] == confirmed_watts
        evidence.update(ready=False, reason=evidence["cooldown_reason"])
        ctx = context(evidence)
        ctx.created_at = now
        ctx.slots[0].valid_at = now
        ctx.slots[0].pv_forecast_kw = pv_kw
        ctx.slots[0].export_price = price
        calls_before = deepcopy(s.calls)
        history_before = deepcopy(s.store.data["execution_audit"])
        plan = DryRunPlanner(OPTIONS).create_plan(ctx)
        expected_export = pv_kw - 1.0
        if confirmed_watts is not None:
            expected_export = min(expected_export, confirmed_watts / 1000)
        assert plan.preview[0]["projected_grid_export_kw"] == expected_export
        assert plan.preview[0]["assumed_zero_export"] is (confirmed_watts == 0)
        assert plan.preview[0]["pv_forecast_kw"] == pv_kw
        assert plan.estimated_daily_cost == round(-expected_export * 5 / 60 * price, 4)
        assert not plan.actions
        assert s.calls == calls_before and s.store.data["execution_audit"] == history_before
    asyncio.run(run())


@pytest.mark.parametrize("price,future_limit", [(-0.1, 0), (0.2, None)])
@pytest.mark.parametrize("restoring", [False, True])
def test_confirmed_hold_ends_at_eligible_time_without_bypassing_restoration(setup, price, future_limit, restoring):
    from custom_components.ha_energy_planner.export_limit_policy import assumed_export_limit_watts

    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        s.states["sensor.export_price"] = price_state((price, price))
        current = runtime.evidence(s.hass, DATA, OPTIONS, NOW)
        current.update(ownership={**s.control.state, "restoring": restoring}, **s.control.cooldown(NOW))
        current.update(ready=False, reason="export_limit_change_rate_limited")
        ctx = context(current)
        assert assumed_export_limit_watts(ctx, NOW + timedelta(seconds=299)) == 0
        assert assumed_export_limit_watts(ctx, NOW + timedelta(seconds=300)) == (
            None if restoring else future_limit)
        assert assumed_export_limit_watts(ctx, NOW + timedelta(hours=2)) is None
        assert len(s.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("blocker", ["upstream_pending", "uncertain_write", "stale", "invalid_deadline"])
def test_deferred_projection_does_not_invent_a_confirmed_hold(setup, blocker):
    from custom_components.ha_energy_planner.export_limit_policy import assumed_export_limit_watts, curtailment_preview

    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        if blocker == "uncertain_write":
            now = move(s, 300)
            desired = latest_action(s, False)
            s.mode["value"] = "timeout"
            assert (await s.control.execute(desired, now))[0] == OutcomeResult.PENDING
            assert s.control.state["pending"] and not s.states[ENTITY].attributes["pending"]
        else:
            now = move(s, 1)
            if blocker == "upstream_pending":
                s.states[ENTITY] = limit_state(0, pending=True, readback=now.timestamp())
            elif blocker == "stale":
                s.states[ENTITY].attributes["last_successful_readback"] = now.timestamp() - 601
        current = runtime.evidence(s.hass, DATA, OPTIONS, now)
        current.update(ownership=s.control.state, **s.control.cooldown(now))
        current.update(ready=False)
        if blocker == "invalid_deadline":
            current["next_eligible_change_at"] = "invalid"
        ctx = context(current)
        assert assumed_export_limit_watts(ctx, now) is None
        assert curtailment_preview(ctx, ctx.slots[0])["assumed_zero_export"] is False
    asyncio.run(run())


def test_future_projection_does_not_treat_service_failure_as_timing_only(setup):
    from custom_components.ha_energy_planner.export_limit_policy import assumed_export_limit_watts

    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        s.hass.services.has_service = lambda *args: False
        current = runtime.evidence(s.hass, DATA, OPTIONS, NOW)
        current.update(ownership=s.control.state, **s.control.cooldown(NOW))
        assert current["reason"] == "export_limit_services_unavailable" and current["feedback_ready"]
        ctx = context(current)
        assert assumed_export_limit_watts(ctx, NOW) == 0
        assert assumed_export_limit_watts(ctx, NOW + timedelta(seconds=300)) is None
        assert len(s.calls) == 1
    asyncio.run(run())


def test_export_hold_preserves_on_site_solar_and_import_cost(setup):
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    async def run():
        s = setup
        s.mode["value"] = "confirm"
        await s.control.execute(action(s), NOW)
        current = runtime.evidence(s.hass, DATA, OPTIONS, NOW)
        current.update(ownership=s.control.state, **s.control.cooldown(NOW))
        current.update(ready=False, reason=current["cooldown_reason"])
        ctx = context(current)
        ctx.slots[0].baseline_load_forecast_kw = 6.0
        plan = DryRunPlanner(OPTIONS).create_plan(ctx)
        assert plan.preview[0]["pv_forecast_kw"] == 5.0
        assert plan.preview[0]["projected_grid_export_kw"] == 0.0
        assert plan.estimated_daily_cost == round(1.0 * 5 / 60 * 0.2, 4)
        assert len(s.calls) == 1 and not plan.actions
    asyncio.run(run())


def test_unpriced_surplus_does_not_credit_exports_or_erase_known_import_cost():
    from custom_components.ha_energy_planner.const import CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    ctx = context({})
    ctx.slots[0].export_price = None
    imported = deepcopy(ctx.slots[0])
    imported.valid_at = NOW + timedelta(minutes=5)
    imported.pv_forecast_kw = 0.0
    ctx.slots.append(imported)
    plan = DryRunPlanner({**OPTIONS, CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED: False}).create_plan(ctx)
    assert plan.estimated_daily_cost == round(1.0 * 5 / 60 * 0.2, 4)
    assert all("projected_grid_export_kw" not in row for row in plan.preview)
