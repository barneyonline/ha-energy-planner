"""Tests for deferred persistent-notification lifecycle helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from homeassistant.core import CoreState

from custom_components.ha_energy_planner import notifications as notifications_module


def test_deferred_notification_helpers_handle_missing_runtime_data() -> None:
    hass = SimpleNamespace(state=CoreState.starting)

    async def callback() -> None:
        return None

    assert notifications_module.defer_persistent_notification(hass, "alert", callback) is False
    notifications_module.cancel_deferred_persistent_notification(hass, "alert")
    asyncio.run(notifications_module._async_flush_deferred_notifications(hass))

    hass.data = {}
    asyncio.run(notifications_module._async_flush_deferred_notifications(hass))
    notifications_module.cancel_deferred_persistent_notification(hass, "alert")


def test_cancelling_one_deferred_notification_keeps_shared_start_listener(monkeypatch: Any) -> None:
    hass = SimpleNamespace(state=CoreState.starting, data={})
    start_callbacks: list[Any] = []
    listener_cancelled: list[bool] = []
    monkeypatch.setattr(
        notifications_module,
        "async_at_started",
        lambda hass_arg, callback: (start_callbacks.append(callback) or (lambda: listener_cancelled.append(True))),
    )

    assert notifications_module.defer_persistent_notification(
        hass,
        "first",
        lambda: asyncio.sleep(0),
    )
    assert notifications_module.defer_persistent_notification(
        hass,
        "second",
        lambda: asyncio.sleep(0),
    )

    notifications_module.cancel_deferred_persistent_notification(hass, "first")

    assert len(start_callbacks) == 1
    assert listener_cancelled == []


def test_entry_cleanup_cancels_only_owned_callbacks_even_during_flush(monkeypatch: Any) -> None:
    async def run() -> None:
        hass = SimpleNamespace(state=CoreState.starting, data={})
        monkeypatch.setattr(notifications_module, "async_at_started", lambda *args: lambda: None)
        entered = asyncio.Event()
        release = asyncio.Event()
        called: list[str] = []

        async def first() -> None:
            entered.set()
            await release.wait()

        async def later() -> None:
            called.append("later")

        notifications_module.cancel_deferred_notifications_for_entry(SimpleNamespace(), "gone")
        notifications_module.defer_persistent_notification(hass, "first", first, owner_id="retained")
        notifications_module.defer_persistent_notification(hass, "later", later, owner_id="removed")
        flush = asyncio.create_task(notifications_module._async_flush_deferred_notifications(hass))
        await entered.wait()
        notifications_module.cancel_deferred_notifications_for_entry(hass, "removed")
        release.set()
        await flush
        assert called == []
        assert hass.data == {}

    asyncio.run(run())


def test_running_result_supersedes_callback_waiting_behind_another_entry(monkeypatch: Any) -> None:
    async def run() -> None:
        hass = SimpleNamespace(state=CoreState.starting, data={})
        monkeypatch.setattr(notifications_module, "async_at_started", lambda *args: lambda: None)
        entered, release = asyncio.Event(), asyncio.Event()
        seen: list[str] = []

        async def slow() -> None:
            entered.set()
            await release.wait()

        async def obsolete() -> None:
            seen.append("obsolete")

        async def current() -> None:
            seen.append("current")

        notifications_module.defer_persistent_notification(hass, "slow", slow, owner_id="other")
        notifications_module.defer_persistent_notification(hass, "report", obsolete, owner_id="planner")
        hass.state = CoreState.running
        flush = asyncio.create_task(notifications_module._async_flush_deferred_notifications(hass))
        await entered.wait()
        assert not notifications_module.defer_persistent_notification(hass, "report", current, owner_id="planner")
        await current()
        release.set()
        await flush
        assert seen == ["current"]
        assert hass.data == {}

    asyncio.run(run())


def test_entry_removal_dismisses_only_its_notifications_and_pending_callbacks(monkeypatch: Any) -> None:
    async def run() -> None:
        calls: list[Any] = []

        async def dispatch(domain: str, service: str, data: dict[str, str], **kwargs: Any) -> None:
            calls.append((domain, service, data))

        hass = SimpleNamespace(state=CoreState.starting, data={}, services=SimpleNamespace(async_call=dispatch))
        monkeypatch.setattr(notifications_module, "async_at_started", lambda *args: lambda: None)
        notifications_module.defer_persistent_notification(hass, "mine", lambda: asyncio.sleep(0), owner_id="removed")
        notifications_module.defer_persistent_notification(
            hass, "theirs", lambda: asyncio.sleep(0), owner_id="retained",
        )
        await notifications_module.async_dismiss_entry_notifications(hass, "removed")
        assert all(data["notification_id"].endswith("_removed") for _, _, data in calls)
        assert any(data["notification_id"] == "ha_energy_planner_ai_explanation_removed" for _, _, data in calls)
        assert any(data["notification_id"] == "ha_energy_planner_restore_safe_state_ev_removed" for _, _, data in calls)
        pending = hass.data[notifications_module._DEFERRED_NOTIFICATIONS_DATA_KEY]
        assert list(pending.callbacks) == ["theirs"]

    asyncio.run(run())


def test_startup_flush_completes_remaining_callbacks_after_one_raises(monkeypatch: Any) -> None:
    async def run() -> None:
        hass = SimpleNamespace(state=CoreState.starting, data={})
        monkeypatch.setattr(notifications_module, "async_at_started", lambda *args: lambda: None)
        calls: list[str] = []

        async def failed() -> None:
            raise RuntimeError("notification service temporarily unavailable")

        async def recovered() -> None:
            calls.append("created")

        notifications_module.defer_persistent_notification(hass, "failed", failed)
        notifications_module.defer_persistent_notification(hass, "recovered", recovered)
        hass.state = CoreState.running
        await notifications_module._async_flush_deferred_notifications(hass)
        assert calls == ["created"]
        assert hass.data == {}

    asyncio.run(run())
