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
