"""Advice completion and cancellation cannot publish stale plan explanations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_coordinator import _coordinator_for_runtime_services, _plan

from custom_components.ha_energy_planner import advice_runtime, notifications, startup_recovery
from custom_components.ha_energy_planner.ai_advisor import AIAdviceResult
from custom_components.ha_energy_planner.const import CONF_AI_TASK_ENTITY


def test_ai_metadata_with_nonstring_clock_or_fingerprint_is_not_reused():
    assert (
        advice_runtime._latest_ai_plan_fingerprint(
            [{"status": "accepted", "rejected_detail": {"plan_fingerprint": 42}}]
        )
        is None
    )
    assert advice_runtime._latest_ai_service_call_at([{"service_called": True, "created_at": 42}]) is None


@pytest.mark.parametrize("outcome", ["accepted", "stale", "stale_under_lock", "failed", "cancelled"])
def test_background_advice_completion_does_not_emit_button_notifications(outcome):
    async def run():
        owner = _coordinator_for_runtime_services()
        owner._last_phase_durations = {}
        owner._ai_current_plan_safe = True
        owner._ai_current_plan_fingerprint = "fp"
        owner._ai_advice_fingerprint = "other" if outcome == "stale" else "fp"
        owner._async_notify_ai_advice = AsyncMock()
        owner.async_update_listeners = lambda: None

        async def provider(*args, **kwargs):
            if outcome == "failed":
                raise RuntimeError("synthetic provider failure")
            if outcome == "cancelled":
                raise asyncio.CancelledError
            return AIAdviceResult("accepted", {"summary": "current"}, None, "ai_task.generate_data"), True

        owner._async_get_throttled_ai_advice = provider
        if outcome == "stale_under_lock":
            await owner._planner_lock.acquire()
        task = asyncio.create_task(
            owner._async_run_ai_advice(
                SimpleNamespace(created_at=datetime.now(UTC)),
                _plan("ai"),
                {},
                {},
                "fp",
            )
        )
        owner._ai_advice_task = task
        if outcome == "stale_under_lock":
            await asyncio.sleep(0)
            owner._ai_current_plan_safe = False
            owner._planner_lock.release()
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        owner._async_notify_ai_advice.assert_not_awaited()
        assert owner._ai_advice_task is None
        assert len(owner.store.ai_recommendations) == (1 if outcome == "accepted" else 0)

    asyncio.run(run())


def test_expired_ai_rate_limit_allows_explicit_request():
    async def run():
        owner = _coordinator_for_runtime_services(entry_data={CONF_AI_TASK_ENTITY: "ai_task.local"})
        owner.data = _plan("ai")
        owner.store.data["ai_last_attempt"] = {"created_at": datetime.now(UTC) - timedelta(hours=1)}
        owner._set_ai_advice_pending = lambda *args: None
        owner._tearing_down = False

        # Stop at the notification boundary to avoid creating a provider task.
        async def notify(*args):
            owner._tearing_down = True

        owner._async_notify_ai_advice = notify
        from dataclasses import dataclass

        @dataclass
        class Request:
            created_at: datetime

        owner._last_decision_context = Request(datetime.now(UTC))
        await owner.async_request_ai_advice()
        assert owner.hass.created_tasks == []
        assert owner._ai_current_plan_safe

    asyncio.run(run())


def test_cancel_last_deferred_notification_after_start_listener_was_cleared():
    hass = SimpleNamespace(data={})
    pending = notifications._DeferredNotifications()
    pending.callbacks["last"] = lambda: None
    hass.data[notifications._DEFERRED_NOTIFICATIONS_DATA_KEY] = pending
    notifications.cancel_deferred_persistent_notification(hass, "last")
    assert hass.data == {}


def test_cancel_recovery_before_executor_initialization():
    owner = SimpleNamespace(store=SimpleNamespace(data=None))
    asyncio.run(startup_recovery.async_cancel_startup_auto_recovery(owner, "setup_entry_failed"))
    assert owner._startup_auto_recovery_authorized is False
    assert owner._startup_auto_recovery_task is None


def test_safe_recovery_retry_stops_when_not_authorized():
    owner = SimpleNamespace(_startup_auto_recovery_authorized=False)
    asyncio.run(startup_recovery._async_retry_startup_safe_recovery(owner))


def test_recovery_error_after_control_disabled_does_not_restore_or_rearm():
    owner = SimpleNamespace(
        automatic_control_requested=False,
        store=SimpleNamespace(data={}),
        _async_retry_startup_safe_recovery=AsyncMock(side_effect=RuntimeError("disabled")),
    )
    asyncio.run(startup_recovery._async_run_startup_auto_recovery(owner))
    owner._async_retry_startup_safe_recovery.assert_awaited_once()


def test_recovery_blocker_skips_nonblocking_and_successful_checks():
    report = {
        "production": {"dry_run_evidence_complete": True},
        "checks": [{"blocking": False, "ok": False}, {"blocking": True, "ok": True}],
        "current_plan": {"message": "waiting for inputs"},
    }
    assert startup_recovery._active_control_not_ready_reason(report) == "waiting for inputs"
