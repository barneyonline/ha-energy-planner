"""Recovery alerts follow confirmed device outcomes and current plan evidence."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import CoreState
from test_executor import FakeHass, FakeStore, _arm_store, _context

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.ev_adapter import EVCommandResult
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    EnergyPlan,
    InputHealth,
    OutcomeResult,
    PlanAction,
    PlannerMode,
)


def _plan(*, infeasible: bool = False) -> EnergyPlan:
    now = datetime.now(UTC)
    return EnergyPlan(
        plan_id="plan", created_at=now, horizon_hours=12, interval_minutes=15,
        status="current", health=InputHealth.HEALTHY, mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test", confidence=1.0, estimated_daily_cost=None,
        actions=[PlanAction(
            "stop", "plan", now - timedelta(minutes=1), now + timedelta(minutes=15),
            ActionAsset.EV, ActionKind.EV_SCHEDULE,
            {"charging_required_now": False, "infeasible": infeasible, "target_soc_percent": 80, "ready_by": "08:00"},
            [], [], None, 1.0,
        )], preview=[],
    )


def _alerts(hass: FakeHass, service: str) -> list[str]:
    return [data["notification_id"] for domain, name, data in hass.services.calls
            if domain == "persistent_notification" and name == service]


def _executor() -> Executor:
    executor = Executor(
        FakeStore(), hass=FakeHass({"switch.ev": "off", "binary_sensor.charging": "off"}),
        entry_data={"ev_charger_entity": "switch.ev", "ev_charging_entity": "binary_sensor.charging"},
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "ev_control_enabled": True,
                 "dry_run": False, "ev_confirmation_timeout_seconds": 0},
        entry_id="garage",
    )
    _arm_store(executor.store, executor)
    return executor


@pytest.mark.parametrize("manual", [False, True])
def test_confirmed_stop_clears_legacy_scoped_warning_without_another_restore(manual: bool) -> None:
    executor = _executor()
    executor.store.data["ownership"] = {"ev_smart_charging_state": {"switch.ev": "off"}}

    async def run() -> None:
        # This legacy alert predates persisted notification-failure tracking.
        if manual:
            result = await executor.async_manual_ev_charging(False, None)
            assert result.applied
        else:
            plan = _plan()
            await executor.async_evaluate(plan, _context(plan.created_at))
        assert "ha_energy_planner_restore_safe_state_ev_garage" in _alerts(executor.hass, "dismiss")
        assert not _alerts(executor.hass, "create")

    asyncio.run(run())


@pytest.mark.parametrize("manual", [False, True])
def test_safe_stop_with_failed_power_limit_restoration_retains_warning(manual: bool, monkeypatch: Any) -> None:
    executor = _executor()
    executor.store.data["ownership"] = {
        "ev_smart_charging_state": {"switch.ev": "off", "ev_power_limit_entity": 16},
    }
    restore_failed = EVCommandResult(
        False, "ev_power_limit_restore_failed", {}, {}, command_sent=True, safe_state_confirmed=True,
    )
    execute_failed = EVCommandResult(
        False, "ev_power_limit_restore_failed", {}, {}, command_sent=True, safe_state_confirmed=False,
    )

    async def fail_restore(*args: Any, **kwargs: Any) -> EVCommandResult:
        return restore_failed

    async def fail_execute(*args: Any, **kwargs: Any) -> EVCommandResult:
        return execute_failed

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.ev_adapter.EVSmartChargingAdapter.async_restore", fail_restore,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.ev_adapter.EVSmartChargingAdapter.async_execute", fail_execute,
    )

    async def run() -> None:
        await executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.FAILED, reason=restore_failed.reason), scope="ev",
        )
        executor.hass.services.calls.clear()
        if manual:
            await executor.async_manual_ev_charging(False, None)
        else:
            plan = _plan()
            await executor.async_evaluate(plan, _context(plan.created_at))
        assert "ha_energy_planner_restore_safe_state_ev_garage" not in _alerts(executor.hass, "dismiss")
        assert executor.store.data["restore_notification_failures"] == {"ev": ["ev"]}
        assert executor.store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == 16

    asyncio.run(run())


def test_partial_recovery_survives_executor_reload_and_updates_global_warning() -> None:
    executor = _executor()

    async def run() -> None:
        await executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.FAILED, reason="ev_failed:hvac_failed"),
            unresolved_assets={"ev", "daikin"},
        )
        reloaded = Executor(executor.store, hass=executor.hass, entry_id="garage")
        executor.hass.services.calls.clear()
        await reloaded.async_resolve_restore_notifications("ev")
        assert "ha_energy_planner_restore_safe_state_garage" not in _alerts(executor.hass, "dismiss")
        message = next(data["message"] for _, service, data in executor.hass.services.calls if service == "create")
        assert "daikin" in message and "ev_failed" not in message
        assert executor.store.data["restore_notification_failures"] == {"all": ["daikin"]}
        await reloaded.async_release_hvac_control("recovered")
        assert "ha_energy_planner_restore_safe_state_garage" in _alerts(executor.hass, "dismiss")
        assert executor.store.data["restore_notification_failures"] == {}

    asyncio.run(run())


def test_pending_restore_keeps_warning_and_guarded_ev_restore_cannot_clear_it() -> None:
    executor = _executor()

    async def run() -> None:
        await executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.FAILED, reason="export_limit_unconfirmed"),
            scope="enphase_export_limit",
        )
        executor.hass.services.calls.clear()
        await executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.PENDING, reason="export_limit_pending"),
            scope="enphase_export_limit", restored_assets=set(),
        )
        assert not _alerts(executor.hass, "dismiss")
        executor.ev_restore_guard = lambda: False
        await executor.async_restore_safe_state("vehicle_session_changed")
        assert "ha_energy_planner_restore_safe_state_ev_garage" not in _alerts(executor.hass, "dismiss")

    asyncio.run(run())


def test_full_restore_clears_every_scoped_alert_and_allows_recurrence() -> None:
    executor = _executor()

    async def run() -> None:
        for scope in [None, "ev", "daikin", "enphase", "enphase_export_limit"]:
            await executor._async_notify_restore(
                SimpleNamespace(result=OutcomeResult.FAILED, reason="test"), scope=scope,
            )
        executor.hass.services.calls.clear()
        outcome = await executor.async_restore_safe_state("recovered")
        assert outcome.result == OutcomeResult.RESTORED
        assert len(_alerts(executor.hass, "dismiss")) == 5
        assert executor.store.data["restore_notification_failures"] == {}
        await executor._async_notify_restore(SimpleNamespace(result=OutcomeResult.FAILED, reason="test"), scope="ev")
        assert _alerts(executor.hass, "create") == ["ha_energy_planner_restore_safe_state_ev_garage"]

    asyncio.run(run())


def test_current_plan_updates_infeasibility_warning_before_any_execution() -> None:
    executor = _executor()
    plan = _plan(infeasible=True)

    async def run() -> None:
        await executor.async_notify_plan_fallback(plan, [])
        assert "ha_energy_planner_ev_infeasible_garage" in _alerts(executor.hass, "create")
        executor.hass.services.calls.clear()
        plan.actions[0].desired_state["optimization"] = {
            "search_status": "ev_daily_action_cap_reached", "action_limit": 10,
        }
        await executor.async_notify_plan_fallback(plan, [])
        created = [data for _, service, data in executor.hass.services.calls if service == "create"]
        assert created[0]["title"].endswith("EV action limit reached")
        plan.actions[0].desired_state["infeasible"] = False
        await executor.async_notify_plan_fallback(plan, [])
        assert "ha_energy_planner_ev_infeasible_garage" in _alerts(executor.hass, "dismiss")

    asyncio.run(run())


@pytest.mark.parametrize("policy", ["notifications_disabled", "ev_disabled", "dry_run", "disabled", "startup_grace"])
def test_notification_policy_cannot_recreate_obsolete_ev_feasibility_warning(policy: str) -> None:
    executor = _executor()
    plan = _plan(infeasible=True)
    if policy == "notifications_disabled":
        executor.options["plan_fallback_notifications_enabled"] = False
    elif policy == "ev_disabled":
        executor.options["ev_control_enabled"] = False
    elif policy in {"dry_run", "disabled"}:
        plan.mode = PlannerMode.DRY_RUN if policy == "dry_run" else PlannerMode.DISABLED
    else:
        executor.notification_grace_until = datetime.now(UTC) + timedelta(minutes=10)

    async def run() -> None:
        await executor.async_notify_plan_fallback(plan, [])
        if policy not in {"dry_run", "disabled"}:
            await executor._async_notify_ev_infeasible(plan.actions[0])
        assert not _alerts(executor.hass, "create")
        assert "ha_energy_planner_ev_infeasible_garage" in _alerts(executor.hass, "dismiss")
        if policy != "startup_grace":
            assert "ha_energy_planner_ev_interrupted_garage" in _alerts(executor.hass, "dismiss")

    asyncio.run(run())


def test_confirmed_stop_cancels_deferred_restore_warning(monkeypatch: Any) -> None:
    executor = _executor()
    executor.hass.state = CoreState.starting
    executor.hass.data = {}
    started = []
    monkeypatch.setattr("custom_components.ha_energy_planner.notifications.async_at_started",
                        lambda hass, callback: started.append(callback) or (lambda: None))

    async def run() -> None:
        await executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.FAILED, reason="ev_failed"), scope="ev",
        )
        await executor.async_manual_ev_charging(False, None)
        executor.hass.state = CoreState.running
        await started[0](executor.hass)
        assert not _alerts(executor.hass, "create")

    asyncio.run(run())


@pytest.mark.parametrize("required", [None, 0, 5])
def test_target_completion_clears_only_obsolete_interruption_warning(required: float | None) -> None:
    executor = _executor()
    plan = _plan()
    plan.actions[0].desired_state["required_charge_percent"] = required
    asyncio.run(executor.async_notify_plan_fallback(plan, []))
    assert ("ha_energy_planner_ev_interrupted_garage" in _alerts(executor.hass, "dismiss")) is (required == 0)


def test_confirmed_unplug_retires_session_alerts_without_claiming_device_restoration() -> None:
    executor = _executor()
    asyncio.run(executor.async_release_ev_policy(unplugged=True))
    assert "ha_energy_planner_ev_interrupted_garage" in _alerts(executor.hass, "dismiss")
    assert "ha_energy_planner_ev_infeasible_garage" in _alerts(executor.hass, "dismiss")
    assert "ha_energy_planner_restore_safe_state_ev_garage" not in _alerts(executor.hass, "dismiss")
