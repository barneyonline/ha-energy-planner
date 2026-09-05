"""Durable Enphase ownership transaction, independent of execution policy."""

from __future__ import annotations

from datetime import datetime

from .enphase_adapter import EnphaseCommandResult, EnphaseProfileAdapter
from .models import ActionKind, PlanAction
from .ownership import EnphaseOwnership, OwnershipStore


class EnphaseControlTransaction:
    """Retain the first baseline across accepted, interrupted and repeated commands."""

    def __init__(self, store: OwnershipStore, adapter: EnphaseProfileAdapter, now: datetime) -> None:
        self.store = store
        self.adapter = adapter
        self.now = now
        self.previous = EnphaseOwnership.from_mapping(store.data.get("ownership", {}))

    async def _async_prepare(self, profile: str) -> None:
        ownership = dict(self.store.data.get("ownership", {}))
        EnphaseOwnership(self.previous.profile or profile, self.now).apply_to(ownership)
        await self.store.async_save_ownership(ownership)
        await self.store.async_flush()

    async def async_execute(self, action: PlanAction) -> EnphaseCommandResult:
        """Persist before dispatch; cancellation deliberately leaves recovery evidence."""
        self.adapter.before_dispatch = self._async_prepare
        try:
            result = await self.adapter.async_execute(action)
        finally:
            self.adapter.before_dispatch = None
        ownership = dict(self.store.data.get("ownership", {}))
        if result.applied:
            if action.kind == ActionKind.RESTORE_AI:
                EnphaseOwnership().apply_to(ownership)
            elif result.changed_profile_at:
                EnphaseOwnership(
                    self.previous.profile or result.saved_profile,
                    self.now,
                ).apply_to(ownership)
        elif result.rollback_succeeded is True:
            self.previous.apply_to(ownership)
        elif result.command_sent and result.saved_profile is not None:
            EnphaseOwnership(self.previous.profile or result.saved_profile, self.now).apply_to(ownership)
        await self.store.async_save_ownership(ownership)
        return result
