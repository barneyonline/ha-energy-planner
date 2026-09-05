"""Persistent-notification lifecycle helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import CoreState
from homeassistant.helpers.start import async_at_started

from .const import DOMAIN

_DEFERRED_NOTIFICATIONS_DATA_KEY = f"{DOMAIN}_deferred_persistent_notifications"


@dataclass(slots=True)
class _DeferredNotifications:
    """Pending notification callbacks for one Home Assistant instance."""

    callbacks: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    cancel_start_listener: Callable[[], None] | None = None


def persistent_notifications_ready(hass: Any) -> bool:
    """Return whether Home Assistant has completed integration startup."""
    return getattr(hass, "state", CoreState.running) is CoreState.running


def defer_persistent_notification(
    hass: Any,
    notification_id: str,
    create_callback: Callable[[], Awaitable[None]],
    *,
    owner_id: str | None = None,
) -> bool:
    """Defer one keyed notification until Home Assistant has started."""
    if persistent_notifications_ready(hass):
        return False
    data = getattr(hass, "data", None)
    if not isinstance(data, MutableMapping):
        return False
    pending = data.get(_DEFERRED_NOTIFICATIONS_DATA_KEY)
    if not isinstance(pending, _DeferredNotifications):
        pending = _DeferredNotifications()
        data[_DEFERRED_NOTIFICATIONS_DATA_KEY] = pending
    pending.callbacks[notification_id] = create_callback
    if owner_id is not None:
        pending.owners[notification_id] = owner_id
    if pending.cancel_start_listener is None:
        pending.cancel_start_listener = async_at_started(hass, _async_flush_deferred_notifications)
    return True


def cancel_deferred_persistent_notification(hass: Any, notification_id: str) -> None:
    """Cancel a notification that has not yet been created."""
    data = getattr(hass, "data", None)
    if not isinstance(data, MutableMapping):
        return
    pending = data.get(_DEFERRED_NOTIFICATIONS_DATA_KEY)
    if not isinstance(pending, _DeferredNotifications):
        return
    pending.callbacks.pop(notification_id, None)
    pending.owners.pop(notification_id, None)
    if pending.callbacks:
        return
    if pending.cancel_start_listener is not None:
        pending.cancel_start_listener()
    data.pop(_DEFERRED_NOTIFICATIONS_DATA_KEY, None)


def cancel_deferred_notifications_for_entry(hass: Any, entry_id: str | None) -> None:
    """Release startup callbacks owned by an unloading entry."""
    data = getattr(hass, "data", None)
    pending = data.get(_DEFERRED_NOTIFICATIONS_DATA_KEY) if isinstance(data, MutableMapping) else None
    if not isinstance(pending, _DeferredNotifications):
        return
    for notification_id, owner_id in tuple(pending.owners.items()):
        if owner_id == entry_id:
            cancel_deferred_persistent_notification(hass, notification_id)


async def _async_flush_deferred_notifications(hass: Any) -> None:
    """Create the latest version of every notification deferred at startup."""
    data = getattr(hass, "data", None)
    if not isinstance(data, MutableMapping):
        return
    pending = data.get(_DEFERRED_NOTIFICATIONS_DATA_KEY)
    if not isinstance(pending, _DeferredNotifications):
        return
    for notification_id in tuple(pending.callbacks):
        create_callback = pending.callbacks.pop(notification_id, None)
        pending.owners.pop(notification_id, None)
        if create_callback is not None:
            with suppress(Exception):
                await create_callback()
    if data.get(_DEFERRED_NOTIFICATIONS_DATA_KEY) is pending:
        data.pop(_DEFERRED_NOTIFICATIONS_DATA_KEY, None)
