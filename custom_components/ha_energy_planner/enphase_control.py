"""Durable Enphase ownership transaction, independent of execution policy."""

from __future__ import annotations

from collections.abc import Callable
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

    async def async_execute(
        self,
        action: PlanAction,
        *,
        dispatch_guard: Callable[[], str | None] | None = None,
    ) -> EnphaseCommandResult:
        """Persist before dispatch; cancellation deliberately leaves recovery evidence."""
        rejected_before_dispatch = False

        async def prepare(profile: str) -> str | None:
            nonlocal rejected_before_dispatch
            ownership = dict(self.store.data.get("ownership", {}))
            EnphaseOwnership(self.previous.profile or profile, self.now).apply_to(ownership)
            await self.store.async_save_ownership(ownership)
            await self.store.async_flush()
            reason = dispatch_guard() if dispatch_guard is not None else None
            rejected_before_dispatch = reason is not None
            return reason

        self.adapter.before_dispatch = prepare
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
        elif not result.command_sent or result.rollback_succeeded is True:
            self.previous.apply_to(ownership)
        elif result.command_sent and result.saved_profile is not None:
            EnphaseOwnership(self.previous.profile or result.saved_profile, self.now).apply_to(ownership)
        await self.store.async_save_ownership(ownership)
        if rejected_before_dispatch:
            await self.store.async_flush()
        return result
