"""Acknowledge actual Home Assistant writes before granting command authority."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.storage import Store


class DurableStore(Store[dict[str, Any]]):
    """Keep HA serialization and atomic writes, but fail on an unconfirmed save.

    HA's Store logs write/serialization failures without raising to async_save's
    caller, and can defer writes during shutdown. Neither return path proves
    that actuator recovery metadata reached disk. The write hook acknowledges
    only a completed write; PlannerStore serializes calls to this instance.
    """

    async def async_save(self, data: dict[str, Any]) -> None:
        """Reject failed or deferred writes so the caller retains dirty state."""
        self._write_confirmed = False
        await super().async_save(data)
        if not self._write_confirmed:
            raise OSError("Energy Planner storage write was not confirmed")

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        """Acknowledge only after HA's disk-write executor job succeeds."""
        await super()._async_write_data(data)
        self._write_confirmed = True
