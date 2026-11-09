"""Refresh lifecycle timing and command contention regressions."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from test_coordinator import FakeEntry, FakeHass, FakeStore, _plan

from custom_components.ha_energy_planner import coordinator as coordinator_module
from custom_components.ha_energy_planner import refresh_timing as timing_module
from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator


def test_refresh_plans_while_unrelated_command_owns_lock() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.entry = FakeEntry({"enphase_export_limit_entity": "select.export"})
        coordinator.hass = FakeHass()
        coordinator.hass.async_create_task = asyncio.create_task
        coordinator.store = FakeStore()
        coordinator.data = _plan("old")
        coordinator._command_lock = asyncio.Lock()
        coordinator._planner_lock = asyncio.Lock()
        coordinator._refresh_generation = 0
        coordinator._async_update_data_locked = AsyncMock(return_value=_plan("responsive"))
        await coordinator._command_lock.acquire()
        refresh = asyncio.create_task(coordinator._async_update_data())
        try:
            # Yield deterministically: a blocked command must not block planning.
            for _ in range(10):
                await asyncio.sleep(0)
            assert refresh.done(), "refresh is coupled to an unrelated command transaction"
            assert (await refresh).plan_id == "responsive"
        finally:
            coordinator._tearing_down = True
            coordinator._command_lock.release()
            await refresh
            task = getattr(coordinator, "_export_reconcile_task", None)
            if task is not None:
                await task

    asyncio.run(scenario())


def coordinator_for_timing():
    from test_coordinator import _coordinator_for_runtime_services

    coordinator = _coordinator_for_runtime_services()
    coordinator.hass.async_create_task = asyncio.create_task
    coordinator._async_update_data_locked = AsyncMock(return_value=_plan("timed"))
    coordinator._schedule_debounced_refresh = Mock()
    coordinator.async_update_listeners = Mock()
    return coordinator


def test_weather_is_prepared_before_planner_lock() -> None:
    async def scenario() -> None:
        coordinator = coordinator_for_timing()
        coordinator.entry.data["weather_entity"] = "weather.home"
        prepared = (coordinator_module._weather_input_identity(
            coordinator.entry_data, coordinator.planner_options), False, {}, {})
        observed = []

        async def prepare():
            observed.append("weather")
            assert not coordinator._planner_lock.locked()
            return prepared

        async def update(*, defer_execution=False):
            observed.append("planner")
            assert coordinator._planner_lock.locked()
            assert coordinator._prepared_weather_for_refresh is prepared
            return _plan("weather-before-lock")

        coordinator._async_prepare_weather_forecast = prepare
        coordinator._async_update_data_locked = update
        assert (await coordinator._async_update_data()).plan_id == "weather-before-lock"
        assert observed == ["weather", "planner"]
        assert not hasattr(coordinator, "_prepared_weather_for_refresh")

    asyncio.run(scenario())


@pytest.mark.parametrize("race", ["entity", "interval", "freshness", "force", "added"])
def test_weather_configuration_races_retry_without_holding_planner_lock(race) -> None:
    async def scenario() -> None:
        coordinator = coordinator_for_timing()
        if race != "added":
            coordinator.entry.data["weather_entity"] = "weather.home"
        calls = []

        async def locked_refresh(**kwargs):
            assert coordinator._planner_lock.locked()
            await coordinator._async_weather_for_refresh(
                coordinator.entry_data, coordinator.planner_options,
                now=datetime(2026, 11, 9, tzinfo=UTC),
                force=bool(getattr(coordinator, "_force_next_refresh", False)),
            )
            return _plan("timed")

        coordinator._async_update_data_locked = AsyncMock(side_effect=locked_refresh)

        async def weather(data, options, **kwargs):
            assert not coordinator._planner_lock.locked()
            calls.append((data.get("weather_entity"), kwargs["force"]))
            if len(calls) == 1:
                if race == "entity":
                    coordinator.entry.data["weather_entity"] = "weather.changed"
                elif race == "interval":
                    coordinator.entry.options["planning_interval_minutes"] = 15
                elif race == "freshness":
                    coordinator.entry.options["forecast_freshness_minutes"] = 30
                elif race == "force":
                    coordinator._force_next_refresh = True
            return {}, {}

        if race == "added":
            # The weather selector may be added while awaiting the planner lock.
            await coordinator._planner_lock.acquire()
        coordinator._async_weather_forecast = weather
        refresh = asyncio.create_task(coordinator._async_update_data())
        if race == "added":
            await asyncio.sleep(0)
            coordinator.entry.data["weather_entity"] = "weather.home"
            coordinator._planner_lock.release()
        assert (await refresh).plan_id == "timed"
        assert len(calls) == (1 if race == "added" else 2)
        assert calls[-1] == ("weather.changed" if race == "entity" else "weather.home", race == "force")
        coordinator._async_update_data_locked.assert_awaited_once()

    asyncio.run(scenario())


def test_weather_preparation_and_direct_refresh_fallback() -> None:
    async def scenario() -> None:
        coordinator = coordinator_for_timing()
        coordinator.entry.data["weather_entity"] = "weather.home"
        expected = ({"forecast": [{"datetime": "2026-11-07T00:00:00+00:00"}]}, {"fetch_status": "fetched"})
        coordinator._async_weather_forecast = AsyncMock(return_value=expected)
        prepared = await coordinator._async_prepare_weather_forecast()
        assert prepared[2:] == expected
        coordinator._prepared_weather_for_refresh = prepared
        now = datetime(2026, 11, 7, tzinfo=UTC)
        assert await coordinator._async_weather_for_refresh(
            coordinator.entry_data, coordinator.planner_options, now=now, force=False,
        ) == expected
        assert await coordinator._async_weather_for_refresh(
            coordinator.entry_data, coordinator.planner_options, now=now, force=True,
        ) == expected
        coordinator.entry.data["weather_entity"] = "weather.changed"
        assert await coordinator._async_weather_for_refresh(
            coordinator.entry_data, coordinator.planner_options, now=now, force=False,
        ) == expected
        del coordinator._prepared_weather_for_refresh
        assert await coordinator._async_weather_for_refresh({}, {}, now=now, force=True) == expected
        assert coordinator._async_weather_forecast.await_count == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_path", [
    "success", "failure", "cancel", "flush_failure", "flush_cancel", "fingerprint", "teardown",
])
def test_timing_partitions_every_exit_and_flush(monkeypatch, exit_path):
    async def scenario():
        clock = [10.0]
        monkeypatch.setattr(timing_module, "perf_counter", lambda: clock[0])
        monkeypatch.setattr(coordinator_module, "perf_counter", lambda: clock[0])
        coordinator = coordinator_for_timing()
        coordinator._last_phase_durations = {"another_refresh_ms": 9999}
        coordinator._pending_refresh_trigger = "state_change"
        coordinator._refresh_request_timing = ("state_change", 9.5, asyncio.current_task())

        @asynccontextmanager
        async def delayed_save():
            try:
                yield
            finally:
                clock[0] += 0.7
                if exit_path == "flush_failure":
                    raise OSError("atomic flush failed")
                if exit_path == "flush_cancel":
                    raise asyncio.CancelledError

        coordinator.store.async_delay_save = delayed_save

        async def update(*, defer_execution=False):
            clock[0] += 0.1
            if exit_path == "fingerprint":
                timing_module.current_timing().aliases["fingerprint_ms"] = 100
                return _plan("retained")
            timing_module.stage("planner_ms")
            clock[0] += 0.2
            if exit_path == "failure":
                raise ValueError("planner failed")
            if exit_path == "cancel":
                raise asyncio.CancelledError
            timing_module.stage("post_plan_ms")
            clock[0] += 0.3
            coordinator._deferred_plan_execution = (0, _plan("new"), object(), {})
            return _plan("new")

        coordinator._async_update_data_locked = update
        coordinator._schedule_plan_execution = Mock()
        if exit_path == "teardown":
            coordinator._tearing_down = True
            coordinator.data = _plan("old")
        if exit_path in {"failure", "cancel", "flush_failure", "flush_cancel"}:
            with pytest.raises((ValueError, OSError, asyncio.CancelledError)):
                await coordinator._async_update_data()
            coordinator._schedule_plan_execution.assert_not_called()
            assert coordinator._deferred_plan_execution is None
        else:
            await coordinator._async_update_data()
        latest = coordinator.last_refresh_metadata
        assert latest["succeeded"] == (exit_path not in {"failure", "cancel", "flush_failure", "flush_cancel"})
        phases = latest["exclusive_phases_ms"]
        assert "another_refresh_ms" not in latest["phases"]
        assert all(value >= 0 for value in phases.values())
        assert sum(phases.values()) == pytest.approx(latest["duration_ms"], abs=.003)
        assert latest["request_age_ms"] == 500
        assert timing_module.CURRENT_REFRESH.get() is None
        if exit_path != "teardown":
            assert phases["storage_flush_ms"] == 700
        if exit_path == "success":
            assert latest["duration_ms"] == 1300
            assert phases["planner_ms"] == 200
            assert phases["post_plan_ms"] == 300
        if exit_path == "fingerprint":
            assert latest["phases"]["fingerprint_ms"] == 100

    asyncio.run(scenario())


def test_planner_lock_wait_cancellation_does_not_copy_active_refresh(monkeypatch):
    async def scenario():
        clock = [0.0]
        monkeypatch.setattr(timing_module, "perf_counter", lambda: clock[0])
        coordinator = coordinator_for_timing()
        entered, release = asyncio.Event(), asyncio.Event()

        async def update(*, defer_execution=False):
            timing_module.stage("inputs_ms")
            entered.set()
            await release.wait()
            return _plan("first")

        coordinator._async_update_data_locked = update
        first = asyncio.create_task(coordinator._async_update_data())
        await entered.wait()
        second = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(0)
        clock[0] = 2
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        second_snapshot = coordinator.last_refresh_metadata
        assert second_snapshot["exclusive_phases_ms"]["planner_lock_wait_ms"] == 2000
        assert "inputs_ms" not in second_snapshot["exclusive_phases_ms"]
        release.set()
        await first
        assert coordinator.last_refresh_metadata["succeeded"] is True
        assert coordinator.last_refresh_metadata["exclusive_phases_ms"]["inputs_ms"] == 2000
        assert second_snapshot["succeeded"] is False

    asyncio.run(scenario())


def test_child_task_cannot_write_parent_timing():
    async def scenario():
        timing = timing_module.RefreshTiming()
        token = timing_module.CURRENT_REFRESH.set(timing)
        try:
            async def child():
                assert timing_module.current_timing() is None
                timing_module.stage("wrong_task_ms")
            await asyncio.create_task(child())
            assert timing_module.current_timing() is timing
            assert "wrong_task_ms" not in timing.finish()["phases"]
        finally:
            timing_module.CURRENT_REFRESH.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize("result", ["success", "failure", "cancel", "shutdown", "mapping_removed"])
@pytest.mark.parametrize("order", ["executor_first", "reconciliation_first"])
def test_deferred_reconciliation_coalesces_and_gates_execution(monkeypatch, result, order):
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator.entry.data["enphase_export_limit_entity"] = "select.old"
        reconcile = AsyncMock()
        monkeypatch.setattr(coordinator_module.ExportLimitControl, "reconcile", reconcile)
        coordinator._async_execute_plan_if_current = AsyncMock()
        await coordinator._command_lock.acquire()
        async def queue_execution():
            coordinator._pending_plan_execution = (0, _plan("pending"), object(), {})
            task = asyncio.create_task(coordinator._async_drain_plan_execution())
            await asyncio.sleep(0)
            return task

        executor = await queue_execution() if order == "executor_first" else None
        for _ in range(20):
            await coordinator._async_update_data()
        worker = coordinator._export_reconcile_task
        await asyncio.sleep(0)
        assert not worker.done()
        assert coordinator._export_reconcile_pending is True
        assert coordinator._async_update_data_locked.await_count == 20
        assert coordinator.refresh_metrics["completed"] == 20
        # An execution waiter admitted before reconciliation must also fail closed.
        if executor is None:
            executor = await queue_execution()
        if result == "mapping_removed":
            coordinator.entry.data.clear()
        else:
            coordinator.entry.data["enphase_export_limit_entity"] = "select.new"
        if result == "failure":
            reconcile.side_effect = OSError("disk failure")
        elif result == "cancel":
            worker.cancel()
        elif result == "shutdown":
            coordinator._begin_shutdown()
        coordinator._command_lock.release()
        if result == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await worker
        else:
            await worker
        await executor
        coordinator._async_execute_plan_if_current.assert_not_awaited()
        assert coordinator._export_reconcile_task is None
        if result in {"failure", "cancel"}:
            assert coordinator._export_reconcile_pending is True
            coordinator._schedule_debounced_refresh.assert_not_called()
            reconcile.side_effect = None
            await coordinator._async_update_data()
            assert coordinator._export_reconcile_pending is False
        elif result == "shutdown":
            reconcile.assert_not_awaited()
            coordinator._schedule_debounced_refresh.assert_not_called()
        else:
            assert coordinator._export_reconcile_pending is False
            assert coordinator._refresh_generation == 1
            coordinator._schedule_debounced_refresh.assert_called_once_with(
                "export_reconciled", debounce_seconds=0, force=True)
            assert reconcile.await_count == (0 if result == "mapping_removed" else 1)
        assert coordinator._last_export_reconciliation["duration_ms"] >= 0
        if result == "success":
            # Exactly one current plan can execute after reconciliation and
            # fresh planning; stale/pending-era work was dropped above.
            coordinator._pending_plan_execution = (coordinator._refresh_generation, _plan("fresh"), object(), {})
            await coordinator._async_drain_plan_execution()
            coordinator._async_execute_plan_if_current.assert_awaited_once()

    asyncio.run(scenario())


def test_inline_reconciliation_wait_and_work_have_separate_elapsed_timings(monkeypatch):
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator.entry.data["enphase_export_limit_entity"] = "select.export"
        clock = [0.0]
        monkeypatch.setattr(timing_module, "perf_counter", lambda: clock[0])
        entered = asyncio.Event()

        async def reconcile(_self, now):
            clock[0] += 3
            entered.set()

        monkeypatch.setattr(coordinator_module.ExportLimitControl, "reconcile", reconcile)
        await coordinator._command_lock.acquire()
        timing = timing_module.RefreshTiming()
        task = asyncio.create_task(coordinator._async_reconcile_export_limit(timing))
        await asyncio.sleep(0)
        clock[0] = 80
        coordinator._command_lock.release()
        await task
        snapshot = timing.finish()
        assert entered.is_set()
        assert snapshot["exclusive_phases_ms"]["command_lock_wait_ms"] == 80000
        assert snapshot["exclusive_phases_ms"]["export_reconciliation_ms"] == 3000
        assert snapshot["duration_ms"] == 83000

    asyncio.run(scenario())


def test_mapping_removed_after_failed_reconciliation_clears_runtime_gate():
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator._export_reconcile_pending = True
        await coordinator._async_update_data()
        assert coordinator._export_reconcile_pending is False
        assert coordinator._refresh_generation == 1
    asyncio.run(scenario())


def test_shutdown_drains_deferred_reconciliation(monkeypatch):
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator.entry.data["enphase_export_limit_entity"] = "select.export"
        reconcile = AsyncMock()
        monkeypatch.setattr(coordinator_module.ExportLimitControl, "reconcile", reconcile)
        await coordinator._command_lock.acquire()
        await coordinator._async_update_data()
        await asyncio.sleep(0)
        coordinator._begin_shutdown()
        drain = asyncio.create_task(coordinator.async_wait_for_plan_execution())
        await asyncio.sleep(0)
        assert not drain.done()
        coordinator._command_lock.release()
        await asyncio.wait_for(drain, 1)
        reconcile.assert_not_awaited()
        assert coordinator._export_reconcile_task is None
    asyncio.run(scenario())


def test_pending_status_is_visible_before_new_plan_commits():
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status
    from custom_components.ha_energy_planner.sensor import _planning_duration_attrs

    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator.data = _plan("previous")
        await coordinator._async_update_data()
        coordinator._export_reconcile_pending = True
        status = export_limit_status(coordinator)
        assert status["ready"] is False
        assert status["reason"] == "export_reconciliation_pending"
        attrs = _planning_duration_attrs(coordinator)
        assert attrs["exclusive_phases_ms"] == coordinator.last_refresh_metadata["exclusive_phases_ms"]
        assert attrs["export_reconciliation_pending"] is True
    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_real_pipeline_distinguishes_planner_executor_queue_work_and_delivery(monkeypatch, fail):
    async def scenario():
        coordinator = coordinator_for_timing()
        del coordinator._async_update_data_locked
        coordinator._schedule_plan_execution = Mock()
        clock = [0.0]
        monkeypatch.setattr(timing_module, "perf_counter", lambda: clock[0])
        monkeypatch.setattr(coordinator_module, "perf_counter", lambda: clock[0])

        class Planner:
            def __init__(self, *args, **kwargs):
                pass

            def create_plan(self, context):
                clock[0] += 3
                if fail:
                    raise RuntimeError("worker failed")
                return _plan("worker")

        async def queued_executor(func):
            clock[0] += 2
            try:
                return func()
            finally:
                clock[0] += 1

        monkeypatch.setattr(coordinator_module, "DryRunPlanner", Planner)
        coordinator.hass.async_add_executor_job = queued_executor
        if fail:
            with pytest.raises(RuntimeError, match="worker failed"):
                await coordinator._async_update_data()
        else:
            await coordinator._async_update_data()
        latest = coordinator.last_refresh_metadata
        assert latest["duration_ms"] == 6000
        assert latest["phases"]["planner_queue_ms"] == 2000
        assert latest["phases"]["planner_work_ms"] == 3000
        assert latest["exclusive_phases_ms"]["planner_ms"] == 6000
        assert sum(latest["exclusive_phases_ms"].values()) == 6000

    asyncio.run(scenario())


def test_inline_reconciliation_failure_reports_only_this_refresh(monkeypatch):
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator.entry.data["enphase_export_limit_entity"] = "select.export"
        coordinator._last_phase_durations = {"old_planner_ms": 10000}
        clock = [0.0]
        monkeypatch.setattr(timing_module, "perf_counter", lambda: clock[0])

        async def fail(_self, now):
            clock[0] += 4
            raise OSError("reconciliation persistence failed")

        monkeypatch.setattr(coordinator_module.ExportLimitControl, "reconcile", fail)
        with pytest.raises(OSError):
            await coordinator._async_update_data()
        latest = coordinator.last_refresh_metadata
        assert latest["succeeded"] is False
        assert latest["exclusive_phases_ms"]["export_reconciliation_ms"] == 4000
        assert "old_planner_ms" not in latest["phases"]
        assert "inputs_ms" not in latest["phases"]
        assert coordinator._export_reconcile_pending is True
        assert timing_module.CURRENT_REFRESH.get() is None
    asyncio.run(scenario())


def test_request_age_tracks_first_coalesced_trigger(monkeypatch):
    async def scenario():
        coordinator = coordinator_for_timing()
        del coordinator._schedule_debounced_refresh
        clock = [0.0]
        monkeypatch.setattr(timing_module, "perf_counter", lambda: clock[0])
        monkeypatch.setattr(coordinator_module, "perf_counter", lambda: clock[0])
        callbacks = []
        monkeypatch.setattr(coordinator_module, "async_call_later", lambda hass, delay, cb: (
            callbacks.append(cb) or (lambda: None)))
        coordinator._schedule_debounced_refresh("first")
        clock[0] = 1
        coordinator._schedule_debounced_refresh("latest")
        clock[0] = 2
        coordinator.async_request_refresh = coordinator._async_update_data
        callbacks[-1](None)
        await asyncio.gather(*coordinator._listener_tasks)
        assert coordinator.last_refresh_metadata["request_age_ms"] == 2000
        assert coordinator.last_refresh_metadata["trigger"] == "latest"
        assert coordinator.refresh_metrics["coalesced"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown_during_admission", [False, True])
def test_refresh_does_not_join_existing_waiter_on_just_released_command_lock(monkeypatch, shutdown_during_admission):
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator.entry.data["enphase_export_limit_entity"] = "select.export"
        monkeypatch.setattr(coordinator_module.ExportLimitControl, "reconcile", AsyncMock())
        release_waiter = asyncio.Event()
        await coordinator._command_lock.acquire()

        async def older_command():
            async with coordinator._command_lock:
                assert coordinator._export_reconcile_pending is True
                if shutdown_during_admission:
                    coordinator._begin_shutdown()
                await release_waiter.wait()

        older = asyncio.create_task(older_command())
        await asyncio.sleep(0)
        coordinator._command_lock.release()
        # The lock is unlocked but has an admitted waiter which must run first.
        # Start refresh before that waiter resumes to exercise fair acquisition.
        assert not coordinator._command_lock.locked()
        refresh = asyncio.create_task(coordinator._async_update_data(), eager_start=True)
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            assert refresh.done(), "refresh queued behind an already admitted command"
            await refresh
            if shutdown_during_admission:
                assert getattr(coordinator, "_export_reconcile_task", None) is None
        finally:
            coordinator._tearing_down = True
            release_waiter.set()
            await older
            await refresh
            worker = getattr(coordinator, "_export_reconcile_task", None)
            if worker is not None:
                await worker
    asyncio.run(scenario())


def test_cancelled_refresh_snapshot_cannot_change_when_executor_finishes_later(monkeypatch):
    async def scenario():
        coordinator = coordinator_for_timing()
        del coordinator._async_update_data_locked
        admitted, run_worker = asyncio.Event(), asyncio.Event()
        worker = None

        async def queued_executor(func):
            nonlocal worker
            async def job():
                await run_worker.wait()
                return func()
            worker = asyncio.create_task(job())
            admitted.set()
            # A real executor future may keep running after refresh cancellation.
            return await asyncio.shield(worker)

        coordinator.hass.async_add_executor_job = queued_executor
        task = asyncio.create_task(coordinator._async_update_data())
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        snapshot = dict(coordinator.last_refresh_metadata)
        phases = dict(snapshot["phases"])
        assert "planner_queue_ms" not in phases
        assert "planner_work_ms" not in phases
        run_worker.set()
        await worker
        assert coordinator.last_refresh_metadata == snapshot
        assert coordinator.last_refresh_metadata["phases"] == phases
        assert coordinator.last_refresh_metadata["succeeded"] is False
    asyncio.run(scenario())


def test_request_owner_without_running_loop_is_unknown():
    assert timing_module.request_owner() is None


def test_request_age_does_not_borrow_a_different_tasks_request():
    async def scenario():
        coordinator = coordinator_for_timing()
        coordinator._pending_refresh_trigger = "state_change"
        coordinator._refresh_request_timing = ("state_change", 0.0, asyncio.current_task())
        await asyncio.create_task(coordinator._async_update_data())
        assert coordinator.last_refresh_metadata["request_age_ms"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("already_scheduled", [False, True])
def test_deferred_reconciliation_counts_only_the_dispatched_followup(monkeypatch, already_scheduled):
    async def scenario():
        coordinator = coordinator_for_timing()
        del coordinator._schedule_debounced_refresh
        callbacks = []
        monkeypatch.setattr(coordinator_module, "async_call_later", lambda hass, delay, cb: (
            callbacks.append(cb) or (lambda: None)))
        if already_scheduled:
            coordinator._schedule_debounced_refresh("state_change")
        generation = coordinator._refresh_generation
        coordinator._async_reconcile_export_limit = AsyncMock()
        coordinator._export_reconcile_pending = True
        await coordinator._async_deferred_export_reconciliation()
        assert coordinator._refresh_generation > generation
        assert coordinator._export_reconcile_pending is False
        callbacks[-1](None)
        await asyncio.gather(*coordinator._listener_tasks)
        assert coordinator.refresh_requested == 1
        assert coordinator.refresh_metrics["requested"] == 1
        assert coordinator.refresh_metrics.get("coalesced", 0) == int(already_scheduled)
    asyncio.run(scenario())


@pytest.mark.parametrize("blocker,expected_reason", [
    ("unconfirmed", "export_limit_unconfirmed"),
    ("rejected", "export_limit_service_rejected:controls_disabled"),
    ("request_pending", "export_limit_pending"),
    ("restore_pending", "export_limit_restoration_pending"),
    ("control_pause", "enphase_export_limit_control_paused"),
    ("global_pause", "planner_paused"),
    ("unrelated_pause", "export_reconciliation_pending"),
    ("none", "export_reconciliation_pending"),
])
def test_reconciliation_wait_preserves_current_export_blockers(blocker, expected_reason):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    coordinator = coordinator_for_timing()
    coordinator.data = _plan("previous")
    coordinator.data.device_plans["enphase_export_limit"] = {"ready": True, "summary": "An old planned state"}
    coordinator.entry.options["enphase_export_limit_control_enabled"] = True
    coordinator._export_reconcile_pending = True
    ownership = {}
    if blocker == "unconfirmed":
        ownership["pause"] = "export_limit_unconfirmed"
    elif blocker == "rejected":
        ownership["pause"] = "export_limit_service_rejected:controls_disabled"
    elif blocker == "request_pending":
        ownership["pending"] = {"watts": 0}
    elif blocker == "restore_pending":
        ownership["restoring"] = True
    coordinator.store.data["ownership"] = {"enphase_export_limit": ownership}
    if blocker in {"control_pause", "global_pause", "unrelated_pause"}:
        coordinator.store.data["control_pause"] = {
            "active": True,
            "assets": ["all" if blocker == "global_pause" else
                       "daikin" if blocker == "unrelated_pause" else "enphase_export_limit"],
        }
    status = export_limit_status(coordinator)
    assert status["reason"] == expected_reason
    assert status["ready"] is False
    assert status["reconciliation_pending"] is True
    assert status["summary"] != "An old planned state"
    if blocker not in {"none", "unrelated_pause"}:
        assert "serialized control reconciliation" not in status["summary"]


@pytest.mark.parametrize("blocker", ["none", "restoring", "paused"])
def test_reconciliation_wait_preserves_export_cooldown(blocker, monkeypatch):
    from custom_components.ha_energy_planner import plan_presentation as presentation

    coordinator = coordinator_for_timing()
    coordinator.data = _plan("cooldown")
    coordinator._export_reconcile_pending = True
    ownership = ({"restoring": True} if blocker == "restoring" else
                 {"pause": "export_limit_unconfirmed"} if blocker == "paused" else {})
    coordinator.store.data["ownership"] = {"enphase_export_limit": ownership}
    deadline = "2026-11-05T00:05:00+00:00"
    monkeypatch.setattr(presentation, "cooldown_status", lambda *args: {
        "deferred": True, "cooldown_reason": "export_limit_change_rate_limited",
        "next_eligible_change_at": deadline,
    })
    status = presentation.export_limit_status(coordinator)
    assert status["reconciliation_pending"] is True
    assert status["ready"] is False and status["deferred"] is True
    assert status["next_eligible_change_at"] == deadline
    if blocker == "paused":
        assert status["reason"] == "export_limit_unconfirmed"
    else:
        assert status["reason"] == "export_limit_change_rate_limited"
        assert deadline in status["summary"]
