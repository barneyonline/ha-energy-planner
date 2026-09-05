"""Checked persistence for recovery evidence, including during Core shutdown.

HA Store logs write failures instead of propagating them. This small adapter
observes the actual write hook, then turns a missing acknowledgement into an
error. The private hooks are covered against every supported runtime in CI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError

from .const import DOMAIN


class DurableStore(Store[dict[str, Any]]):
    """A save returns only after an acknowledged, atomic disk write."""

    def __init__(
        self, hass: HomeAssistant, version: int, key: str, *, serialize_in_event_loop: bool = False
    ) -> None:
        super().__init__(
            hass, version, key, atomic_writes=True,
            serialize_in_event_loop=serialize_in_event_loop,
        )
        self._confirmation_lock = asyncio.Lock()
        self._write_succeeded = False

    async def async_save(self, data: dict[str, Any]) -> None:
        """Drain even a cancelled write before allowing another generation."""
        job = asyncio.create_task(self._async_save_checked(data))
        cancelled = False
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                cancelled = True
        job.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _async_save_checked(self, data: dict[str, Any]) -> None:
        async with self._confirmation_lock:
            self._write_succeeded = False
            await super().async_save(data)
            if not self._write_succeeded:
                # During Core stopping, Store queues its write for final_write.
                # Recovery evidence needs acknowledgement before dispatch now.
                write_pending: Callable[[], Awaitable[None]] = self._async_handle_write_data
                await write_pending()
            if not self._write_succeeded:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="storage_write_failed"
                )

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        try:
            await super()._async_write_data(data)
        except (WriteError, SerializationError):
            self._write_succeeded = False
            raise
        self._write_succeeded = True
