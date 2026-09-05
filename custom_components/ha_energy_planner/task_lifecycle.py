"""Task lifecycle boundary; coordinator retains mutable state and lock authority."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback

from .notifications import cancel_deferred_notifications_for_entry

if TYPE_CHECKING:
    from .coordinator import EnergyPlannerCoordinator

_LOGGER = logging.getLogger(__name__)

@callback
def _async_create_listener_task(self: EnergyPlannerCoordinator, coroutine: Coroutine[Any, Any, Any]) -> None:
    """Own listener jobs until they reach a safe teardown boundary."""
    if getattr(self, "_tearing_down", False):
        coroutine.close()
        return
    task = self.hass.async_create_task(coroutine)
    self._listener_tasks.add(task)
    task.add_done_callback(self._listener_tasks.discard)


def _begin_shutdown(self: EnergyPlannerCoordinator) -> None:
    """Stop new coordinator work while preserving an in-flight command."""
    # A refresh task may already be queued even after its timer/listener is
    # cancelled. Suppress its eventual commit until setup is resumed.
    self._tearing_down = True
    if "history_training" in self.__dict__:
        self.history_training.stop()
    cancel_deferred_notifications_for_entry(
        self.hass, getattr(getattr(self, "entry", None), "entry_id", None)
    )
    self._clear_ev_auto_start_compensation()
    if self._debounce_cancel is not None:
        self._debounce_cancel()
        self._debounce_cancel = None
    if self._boundary_cancel is not None:
        self._boundary_cancel()
        self._boundary_cancel = None
    ai_task = getattr(self, "_ai_advice_task", None)
    if ai_task is not None and not ai_task.done():
        ai_task.cancel()
    self._ai_advice_task = None
    # Device execution may already have changed external state and still be
    # confirming, rolling back, or persisting ownership. Let that single
    # transaction reach its safe boundary; lifecycle cleanup explicitly
    # awaits it after this method has prevented any newer work from running.
    self._pending_plan_execution = None
    self._deferred_plan_execution = None
    recovery_task = getattr(self, "_startup_auto_recovery_task", None)
    if recovery_task is not None and not recovery_task.done():
        recovery_task.cancel()
    self._startup_auto_recovery_task = None
    recovery_start_unsub = getattr(self, "_startup_auto_recovery_start_unsub", None)
    if recovery_start_unsub is not None:
        recovery_start_unsub()
    self._startup_auto_recovery_start_unsub = None
    self._startup_auto_recovery_authorized = False
    self._startup_auto_recovery_deadline = None
    if hasattr(self, "executor"):
        self.executor.notification_grace_until = None
    while self._unsub_listeners:
        self._unsub_listeners.pop()()


async def async_wait_for_plan_execution(self: EnergyPlannerCoordinator) -> None:
    """Drain execution and entry-owned jobs without cancelling device transactions."""
    tasks = set(self._listener_tasks)
    execution_task = getattr(self, "_plan_execution_task", None)
    if execution_task is not None:
        tasks.add(execution_task)
    if not tasks:
        return
    results = await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            _LOGGER.error("Unexpected failure while awaiting entry task shutdown", exc_info=result)


async def async_wait_for_refresh_shutdown(self: EnergyPlannerCoordinator) -> None:
    """Wait until any refresh already inside the planner lock has finished."""
    async with self._planner_lock:
        return
