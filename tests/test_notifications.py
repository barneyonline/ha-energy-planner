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
    notifications_module.cancel_deferred_persistent_notification(hass, "alert")


def test_cancelling_one_deferred_notification_keeps_shared_start_listener(monkeypatch: Any) -> None:
    hass = SimpleNamespace(state=CoreState.starting, data={})
    start_callbacks: list[Any] = []
    listener_cancelled: list[bool] = []
    monkeypatch.setattr(
        notifications_module,
        "async_at_started",
        lambda hass_arg, callback: (
            start_callbacks.append(callback) or (lambda: listener_cancelled.append(True))
        ),
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
