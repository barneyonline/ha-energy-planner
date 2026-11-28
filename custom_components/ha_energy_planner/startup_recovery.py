"""Startup recovery boundary; coordinator retains mutable state and lock authority."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, async_noop, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_report_event
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .climate_inputs import hvac_feedback_is_observed
from .const import (
    CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
    CONF_ENPHASE_PROFILE,
    CONF_HOUSEHOLD_LOAD,
    DOMAIN,
    EV_RESERVATION_EXTERNAL_BASELINE,
    HVAC_DRY_ZONE_TARGETS_ABSENT,
    STARTUP_WARMUP_SECONDS,
    STATE_UNKNOWN_VALUES,
)
from .enphase_export_limit import feedback_entity_id
from .ev import ev_charging_state_for_stop
from .ev_policy import native_limit_value_matches
from .ev_runtime import EVRestoreFeedback
from .ev_telemetry import current_sample_reported_at, measured
from .export_limit_policy import EXPORT_ASSET, area_safe
from .models import (
    EnergyPlan,
    InputHealth,
)
from .preflight import (
    build_preflight_report,
    production_evidence_fingerprint,
)
from .safety import (
    DRY_RUN_READY_CYCLES_REQUIRED,
    parse_production_state,
)
from .task_lifecycle import async_finish_runtime_cleanup
from .vehicles import connection

if TYPE_CHECKING:
    from homeassistant.core import State

    from .coordinator import EnergyPlannerCoordinator

_LOGGER = logging.getLogger(__name__)

_RESTORE_CLIMATE_ATTRIBUTES = (
    "temperature", "target_temp_low", "target_temp_high", "min_temp", "max_temp", "target_temp_step",
)
_RESTORE_EV_LIMIT_ATTRIBUTES = ("unit_of_measurement", "min", "max")

STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS = STARTUP_WARMUP_SECONDS

STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS = 30

STARTUP_AUTO_RECOVERY_REQUIRED_RUNS = 1

STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES = frozenset(
    {
        "waiting",
        "waiting_for_home_assistant",
        "grace",
        "waiting_for_safe",
        "restoring",
        "validating",
    }
)


def async_start_startup_auto_recovery(self: EnergyPlannerCoordinator) -> None:
    """Start recovery only after Home Assistant has fully started."""
    if not getattr(self, "_startup_auto_recovery_authorized", False):
        return
    task = getattr(self, "_startup_auto_recovery_task", None)
    if task is not None and not task.done():
        return
    if getattr(self, "_startup_auto_recovery_start_unsub", None) is not None:
        return

    unsubscribe = async_noop

    def cancel_start() -> None:
        unsubscribe()

    async def _async_start_recovery(_hass: HomeAssistant) -> None:
        # Core may already have queued this job when its subscription is retired.
        if self._startup_auto_recovery_start_unsub is not cancel_start:
            return
        self._startup_auto_recovery_start_unsub = None
        if not self._startup_auto_recovery_authorized:
            return
        # Unload must be able to drain startup even while its first save is in flight.
        starting_task = asyncio.current_task()
        self._startup_auto_recovery_task = starting_task
        try:
            if await _async_prepare_startup_recovery(self) and self._startup_auto_recovery_authorized:
                self._startup_auto_recovery_task = self.entry.async_create_background_task(
                    self.hass,
                    self._async_run_startup_auto_recovery(),
                    f"{DOMAIN} startup automatic-control recovery",
                )
        finally:
            if self._startup_auto_recovery_task is starting_task:
                self._startup_auto_recovery_task = None

    self._startup_auto_recovery_start_unsub = cancel_start
    unsubscribe = async_at_started(self.hass, _async_start_recovery)


async def _async_prepare_startup_recovery(self: EnergyPlannerCoordinator) -> bool:
    """Make initial recovery progress durable before starting supervision."""
    failure: Exception | None
    for _ in range(2):
        store_data = getattr(getattr(self, "store", None), "data", {})
        production = parse_production_state(
            store_data.get("production") if isinstance(store_data, dict) else None
        )
        try:
            if production.armed:
                self._startup_auto_recovery_deadline = monotonic() + STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
                self.executor.notification_grace_until = dt_util.utcnow() + timedelta(
                    seconds=STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
                )
                await self._async_update_startup_auto_recovery(
                    "grace", successful_runs=0, required_runs=1, reason="startup_grace_in_progress", started=True,
                )
            else:
                self._startup_auto_recovery_deadline = None
                await self._async_update_startup_auto_recovery(
                    "waiting_for_safe", successful_runs=0, reason="startup_safe_recovery_resumed",
                )
            await self.store.async_flush()
        except Exception as error:
            failure = error
        else:
            failure = None
        if not self._startup_auto_recovery_authorized:
            return False
        if failure is None:
            if not production.armed:
                self._export_retry_startup_blocked = False
                self._schedule_export_retry()
            return True
    _LOGGER.warning("Could not save Energy Planner startup recovery (%s)", type(failure).__name__)
    await self._async_fail_startup_recovery()
    return False


async def _async_fail_startup_recovery(self: EnergyPlannerCoordinator) -> None:
    """Revoke unsupervised authority and release temporary startup guards."""
    self._startup_auto_recovery_authorized = False
    self._startup_auto_recovery_deadline = None
    self._export_retry_startup_blocked = False
    self.executor.notification_grace_until = None
    operator_disarm = getattr(self, "_operator_disarm_request", None)
    cleanup = asyncio.create_task(_async_finish_failed_startup_recovery(self, operator_disarm))
    self._listener_tasks.add(cleanup)
    cleanup.add_done_callback(self._listener_tasks.discard)
    if await async_finish_runtime_cleanup(cleanup):
        raise asyncio.CancelledError


async def _async_finish_failed_startup_recovery(
    self: EnergyPlannerCoordinator, operator_disarm: object | None,
) -> None:
    """Finish a terminal failure and any dispatched restoration during unload."""
    for attempt in range(2):
        if operator_disarm is not getattr(self, "_operator_disarm_request", None):
            return
        armed = parse_production_state(self.store.data.get("production")).armed
        try:
            await self._async_update_startup_auto_recovery(
                "failed", successful_runs=0, reason="startup_recovery_save_failed", completed=True,
                disarm_reason="startup_recovery_save_failed" if armed else None,
            )
            await self.store.async_flush()
        except Exception as error:
            self._failed_startup_restore_flush_needed = True
            if operator_disarm is not getattr(self, "_operator_disarm_request", None):
                return
            if attempt:
                _LOGGER.warning("Could not save Energy Planner recovery failure (%s)", type(error).__name__)
        else:
            self._failed_startup_restore_flush_needed = False
            break
    if operator_disarm is getattr(self, "_operator_disarm_request", None) and not getattr(self, "_tearing_down", False):
        await self._async_restore_failed_startup()
        _watch_failed_startup_restore(self)


async def _async_restore_failed_startup(self: EnergyPlannerCoordinator) -> None:
    """Release retained device authority after a terminal startup save failure."""
    async with self._command_lock:
        if not _failed_startup_needs_restore(self) or getattr(self, "_tearing_down", False):
            return
        if _failed_startup_has_restore_targets(self):
            try:
                await self.executor.async_restore_safe_state("startup_recovery_save_failed")
            except Exception as error:
                _LOGGER.warning("Could not restore Energy Planner devices after startup failed (%s)",
                                type(error).__name__)
        await _async_flush_failed_startup_restore(self)


def _failed_startup_needs_restore(self: EnergyPlannerCoordinator) -> bool:
    return getattr(self, "_failed_startup_restore_flush_needed", False) or _failed_startup_has_restore_targets(self)


async def _async_flush_failed_startup_restore(self: EnergyPlannerCoordinator) -> None:
    """Keep retry ownership until all completed restore changes are durable."""
    self._failed_startup_restore_flush_needed = True
    try:
        await self.store.async_flush()
    except Exception as error:
        _LOGGER.warning("Could not save Energy Planner's startup restoration (%s)", type(error).__name__)
    else:
        self._failed_startup_restore_flush_needed = False


def _failed_startup_has_restore_targets(self: EnergyPlannerCoordinator) -> bool:
    data = getattr(getattr(self, "store", None), "data", {})
    production = parse_production_state(data.get("production"))
    recovery = production.raw.get("startup_auto_recovery")
    if (production.armed or not isinstance(recovery, dict) or recovery.get("status") != "failed"
            or recovery.get("last_reason") != "startup_recovery_save_failed"):
        return False
    ownership = data.get("ownership", {})
    if any(ownership.get(key) for key in (
        "ev_smart_charging_state", "hvac_control", "climate_automations", "enphase_profile",
        "enphase_profile_changed_at",
    )) or ownership.get(EXPORT_ASSET, {}).get("baseline"):
        return True
    reservation = data.get("ev_grid_reservation", {})
    return bool(
        reservation.get("active") is True and reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is not True
    )


def _queue_failed_startup_restore(self: EnergyPlannerCoordinator) -> None:
    if getattr(self, "_tearing_down", False) or not _failed_startup_needs_restore(self):
        _retire_failed_startup_restore_watch(self)
        return
    if getattr(self, "_failed_startup_restore_running", False):
        self._failed_startup_restore_needed = True
        return

    async def restore() -> None:
        try:
            while self._failed_startup_restore_needed and not self._tearing_down:
                self._failed_startup_restore_needed = False
                await self._async_restore_failed_startup()
        finally:
            self._failed_startup_restore_running = False
            _retire_failed_startup_restore_watch(self)

    self._failed_startup_restore_needed = True
    self._failed_startup_restore_running = True
    self._async_create_listener_task(restore())


def _retire_failed_startup_restore_watch(self: EnergyPlannerCoordinator) -> None:
    unsubscribe = getattr(self, "_failed_startup_restore_watch_cancel", None)
    if unsubscribe is not None and not _failed_startup_needs_restore(self):
        unsubscribe()
        if refresh_inputs := getattr(self, "_refresh_input_subscription", None):
            refresh_inputs()


def _watch_failed_startup_restore(self: EnergyPlannerCoordinator) -> None:
    if (getattr(self, "_tearing_down", False) or not _failed_startup_needs_restore(self)
            or getattr(self, "_failed_startup_restore_watch_cancel", None)):
        return
    from .coordinator import _state_listener_targets

    if refresh_inputs := getattr(self, "_refresh_input_subscription", None):
        refresh_inputs()

    @callback
    def after_start(_hass: HomeAssistant) -> None:
        self._queue_failed_startup_restore()

    @callback
    def retained_target_changed(event: Any) -> None:
        _observe_failed_startup_availability(self, event)

    entry_data = self.entry_data
    ownership = self.store.data.get("ownership", {})
    control = ownership.get("hvac_control", {})
    watched, _retained_zones = _state_listener_targets(entry_data, control)
    monitored = set(watched)
    hvac_entities = self.executor.hvac_restore_entity_ids()
    ev_feedback = self.executor.ev_restore_feedback()
    device_scopes = {entity: {"ev"} for entity in ev_feedback.entity_ids}
    if ownership.get("enphase_profile") or ownership.get("enphase_profile_changed_at"):
        profile = entry_data.get(CONF_ENPHASE_PROFILE)
        if isinstance(profile, str) and profile:
            device_scopes.setdefault(profile, set()).add("enphase")
    if ownership.get(EXPORT_ASSET, {}).get("baseline"):
        export_target = entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)
        if isinstance(export_target, str) and export_target:
            device_scopes.setdefault(export_target, set()).add(EXPORT_ASSET)
            if (export_target.startswith("select.")
                    and (export_feedback := feedback_entity_id(self.hass, export_target))):
                monitored.add(export_feedback)
                device_scopes.setdefault(export_feedback, set()).add(EXPORT_ASSET)
    restore_entities = (*device_scopes, *hvac_entities)
    retained = tuple(dict.fromkeys(entity for entity in restore_entities if entity not in monitored))
    subscriptions: list[Callable[[], None]] = []

    def cancel() -> None:
        self._failed_startup_restore_watch_cancel = None
        self._failed_startup_restore_hvac_entities = frozenset()
        self._failed_startup_restore_device_scopes = {}
        self._failed_startup_restore_ev_feedback = EVRestoreFeedback()
        if cancel in self._unsub_listeners:
            self._unsub_listeners.remove(cancel)
        while subscriptions:
            subscriptions.pop()()

    self._failed_startup_restore_watch_cancel = cancel
    self._failed_startup_restore_hvac_entities = frozenset(hvac_entities)
    self._failed_startup_restore_device_scopes = device_scopes
    self._failed_startup_restore_ev_feedback = ev_feedback
    self._unsub_listeners.append(cancel)
    if retained:
        subscriptions.append(async_track_state_change_event(self.hass, retained, retained_target_changed))
    if ev_feedback.power_entity_id:
        @callback
        def power_reported(event: Any) -> None:
            _observe_failed_startup_power_report(self, event)

        subscriptions.append(async_track_state_report_event(self.hass, [ev_feedback.power_entity_id], power_reported))
    subscriptions.append(async_at_started(self.hass, after_start))


def _observe_failed_startup_availability(self: EnergyPlannerCoordinator, event: Any) -> None:
    if getattr(self, "_failed_startup_restore_watch_cancel", None) is None:
        return
    old, new = event.data.get("old_state"), event.data.get("new_state")
    if new is None or new.state in STATE_UNKNOWN_VALUES:
        return
    if new.entity_id in self._failed_startup_restore_hvac_entities:
        if _hvac_restore_feedback_advanced(old, new) and _retained_restore_scope(self, "daikin"):
            self._queue_failed_startup_restore()
    elif (scopes := self._failed_startup_restore_device_scopes.get(new.entity_id)) and (
        old is None or old.state in STATE_UNKNOWN_VALUES
        or _ev_restore_feedback_advanced(self, self._failed_startup_restore_ev_feedback, old, new)
    ) and any(_retained_restore_scope(self, scope) for scope in scopes):
        self._queue_failed_startup_restore()


def _retained_restore_scope(self: EnergyPlannerCoordinator, scope: str) -> bool:
    ownership = self.store.data.get("ownership", {})
    if scope == "ev":
        reservation = self.store.data.get("ev_grid_reservation", {})
        return bool(ownership.get("ev_smart_charging_state") or (
            reservation.get("active") is True and reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is not True
        ))
    if scope == "daikin":
        return bool(ownership.get("hvac_control") or ownership.get("climate_automations"))
    if scope == "enphase":
        return bool(ownership.get("enphase_profile") or ownership.get("enphase_profile_changed_at"))
    return bool(ownership.get(EXPORT_ASSET, {}).get("baseline"))


def _observe_failed_startup_power_report(self: EnergyPlannerCoordinator, event: Any) -> None:
    state = event.data["new_state"]
    if self._failed_startup_restore_watch_cancel is None or "sampled_at_utc" in state.attributes:
        return
    now = dt_util.utcnow()
    if measured(state, now, "power") != 0 or (activity_at := self.executor.ev_grid_activity_at()) is None:
        return
    reported = current_sample_reported_at(state, now, received_at=event.data["last_reported"])
    if (reported is not None and event.data["old_last_reported"] <= activity_at < reported
            and _retained_restore_scope(self, "ev")):
        self._queue_failed_startup_restore()


def _ev_restore_feedback_advanced(
    self: EnergyPlannerCoordinator, feedback: EVRestoreFeedback, old: State, new: State,
) -> bool:
    if new.entity_id == feedback.native_limit_entity_id:
        if (feedback.native_limit_unit is not None
                and new.attributes.get("unit_of_measurement") != feedback.native_limit_unit):
            return False
        return (
            feedback.native_limit_value is not None
            and native_limit_value_matches(new, feedback.native_limit_value)
            and not native_limit_value_matches(old, feedback.native_limit_value)
        ) or any(
            new.attributes.get(key) is not None and old.attributes.get(key) != new.attributes[key]
            for key in _RESTORE_EV_LIMIT_ATTRIBUTES
        )
    if new.entity_id == feedback.power_entity_id:
        now = dt_util.utcnow()
        new_at = current_sample_reported_at(new, now)
        if new_at is None or measured(new, now, "power") != 0:
            return False
        activity_at = self.executor.ev_grid_activity_at()
        if activity_at is not None and new_at <= activity_at:
            return False
        old_at = current_sample_reported_at(old, now)
        return old_at is None or measured(old, now, "power") != 0 or (activity_at is not None and old_at <= activity_at)
    if new.entity_id == feedback.connection_entity_id and connection(new.state) is False:
        return connection(old.state) is not False or _stop_source_advanced(self, old, new)
    return (
        (new.entity_id == feedback.charging_entity_id or new.entity_id in feedback.control_entity_ids)
        and ev_charging_state_for_stop(new.state) is False
        and (ev_charging_state_for_stop(old.state) is not False or _stop_source_advanced(self, old, new))
    )


def _stop_source_advanced(self: EnergyPlannerCoordinator, old: State, new: State) -> bool:
    now = dt_util.utcnow()
    new_at = self.executor.native_ev_signal_reported_at(new, now)
    if new_at is None:
        return False
    old_at = self.executor.native_ev_signal_reported_at(old, now)
    activity_at = self.executor.ev_grid_activity_at()
    if activity_at is None:
        return old_at is None
    return new_at > activity_at and (old_at is None or old_at <= activity_at)


def _hvac_restore_feedback_advanced(old: State | None, new: State) -> bool:
    if not hvac_feedback_is_observed(new):
        return False
    if new.domain == "timer":
        return _scheduler_timer_usable(new) and not _scheduler_timer_usable(old)
    if not hvac_feedback_is_observed(old):
        return True
    return new.domain == "climate" and (
        old.state != new.state
        or any(old.attributes.get(key) != new.attributes.get(key) for key in _RESTORE_CLIMATE_ATTRIBUTES)
    )


def _scheduler_timer_usable(state: State | None) -> bool:
    if not hvac_feedback_is_observed(state):
        return False
    if state.state == "active":
        end = dt_util.parse_datetime(str(state.attributes.get("finishes_at", "")))
        return end is not None and end.tzinfo is not None
    return state.state == "idle"


@callback
def _wake_startup_auto_recovery(self: EnergyPlannerCoordinator) -> None:
    """Wake a recovery task waiting for startup dependencies."""
    event = getattr(self, "_startup_auto_recovery_wakeup", None)
    if event is not None:
        event.set()


async def async_cancel_startup_auto_recovery(
    self: EnergyPlannerCoordinator,
    reason: str,
    *,
    preserve_control: bool = False,
    restore_owned_state: bool = True,
    disarm_reason: str | None = None,
) -> None:
    """Cancel startup recovery without treating persisted progress as authorization."""
    operator_disarm = getattr(self, "_operator_disarm_request", None)
    store_data = getattr(getattr(self, "store", None), "data", None)
    recovery = (
        parse_production_state(store_data.get("production")).raw.get("startup_auto_recovery")
        if isinstance(store_data, dict)
        else None
    )
    recovery_active = bool(
        getattr(self, "_startup_auto_recovery_authorized", False)
        or (isinstance(recovery, dict) and recovery.get("status") in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES)
    )
    self._startup_auto_recovery_authorized = False
    self._startup_auto_recovery_deadline = None
    self._export_retry_startup_blocked = False
    if hasattr(self, "executor"):
        self.executor.notification_grace_until = None
    start_unsub = getattr(self, "_startup_auto_recovery_start_unsub", None)
    if start_unsub is not None:
        start_unsub()
    self._startup_auto_recovery_start_unsub = None
    task = getattr(self, "_startup_auto_recovery_task", None)
    if task is None or task is asyncio.current_task() or task.done():
        task = None
    elif not task.cancelling():
        task.cancel()
    if disarm_reason is not None:
        try:
            # Revoke production authority and save the cancellation before
            # waiting for the startup task's cleanup.
            if recovery_active or reason != "entry_unload":
                await self._async_update_startup_auto_recovery(
                    "cancelled", successful_runs=_startup_auto_recovery_successful_runs(
                        recovery.get("successful_runs") if isinstance(recovery, dict) else None,
                    ), reason=reason, completed=True, disarm_reason=disarm_reason,
                )
            else:
                await self.async_disarm_production_control(disarm_reason)
        finally:
            if task is not None:
                results: list[None | BaseException] = await asyncio.gather(task, return_exceptions=True)
                result = results[0]
                if isinstance(result, Exception):
                    _LOGGER.error("Startup recovery task failed during operator disarm", exc_info=result)
            self._startup_auto_recovery_task = None
        await self.executor.async_dismiss_startup_recovery_notification()
        return
    if task is None:
        self._startup_auto_recovery_task = None
    else:
        # Drain the retiring task before rollback can resume startup recovery.
        # Its expected cancellation is independent of cancellation of this caller.
        retired = asyncio.gather(task, return_exceptions=True)
        cancelled = False
        while not retired.done():
            try:
                await asyncio.shield(retired)
            except asyncio.CancelledError:
                cancelled = True
        if self._startup_auto_recovery_task is task:
            self._startup_auto_recovery_task = None
        if cancelled:
            raise asyncio.CancelledError
        error = retired.result()[0]
        if isinstance(error, Exception):
            raise error
    if not isinstance(store_data, dict):
        return
    if operator_disarm is not getattr(self, "_operator_disarm_request", None):
        return
    production = parse_production_state(store_data.get("production"))
    cancel_requires_restore = bool(
        recovery_active
        and production.armed
        and restore_owned_state
        and not preserve_control
        and reason not in {"automatic_control_disabled", "entry_unload", "setup_entry_failed"}
    )
    if cancel_requires_restore:
        await self.async_disarm_production_control(f"startup_auto_recovery_cancelled:{reason}")
        try:
            await self.async_restore_safe_state(
                f"startup_auto_recovery_cancelled:{reason}",
                refresh=False,
            )
        except Exception:  # noqa: BLE001 - the production gate is already disarmed.
            _LOGGER.exception("Could not restore safe state after startup recovery cancellation")
    recovery = parse_production_state(store_data.get("production")).raw.get("startup_auto_recovery")
    if operator_disarm is not getattr(self, "_operator_disarm_request", None):
        return
    if not recovery_active or not isinstance(recovery, dict):
        if not preserve_control:
            await self.executor.async_dismiss_startup_recovery_notification()
        return
    if preserve_control and reason in {
        "home_assistant_shutdown", "configuration_reload", "entry_unload", "operator_armed",
    }:
        # Persist the current grace/recovery state verbatim so the next
        # process or a rejected operator replacement can resume it.
        return
    await self._async_update_startup_auto_recovery(
        "interrupted" if preserve_control else "cancelled",
        successful_runs=_startup_auto_recovery_successful_runs(recovery.get("successful_runs")),
        reason=reason,
        completed=True,
    )
    await self.executor.async_dismiss_startup_recovery_notification()


async def _async_run_startup_auto_recovery(self: EnergyPlannerCoordinator) -> None:
    """Apply the armed startup grace, then recover indefinitely if unsafe."""
    try:
        production = parse_production_state(self.store.data.get("production"))
        if production.armed:
            grace_safe, reason = await self._async_complete_startup_grace()
            if grace_safe or not self._startup_auto_recovery_authorized:
                return
            await self._async_enter_startup_safe_recovery(reason)
        await self._async_retry_startup_safe_recovery()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - startup recovery is strictly fail closed.
        task = asyncio.current_task()
        assert task is not None
        if task.cancelling():
            raise
        _LOGGER.exception("Unexpected startup automatic-control recovery failure")
        if self.automatic_control_requested:
            try:
                await self._async_enter_startup_safe_recovery("unexpected_recovery_error")
            except Exception:  # noqa: BLE001 - keep the production gate fail closed.
                _LOGGER.exception("Could not persist the startup recovery failure state")
                try:
                    await self.async_disarm_production_control("unexpected_recovery_error")
                except Exception:  # noqa: BLE001 - persistence may itself be unavailable.
                    _LOGGER.exception("Could not persist startup recovery disarming")
            # A persistent storage failure must not spin up an immediate retry
            # loop. Recheck operator intent after the cancellable delay.
            await asyncio.sleep(STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS)
            if getattr(self, "_tearing_down", False) or not self.automatic_control_requested:
                return
            self._startup_auto_recovery_authorized = True
            self._startup_auto_recovery_task = self.entry.async_create_background_task(
                self.hass,
                self._async_run_startup_auto_recovery(),
                f"{DOMAIN} startup automatic-control recovery",
            )


async def _wait_for_load_recovery(self: EnergyPlannerCoordinator, delay: float) -> None:
    """A committed full load recovery wakes validation without granting authority."""
    event = getattr(self, "_load_recovery_ready", None)
    if event is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=delay)
    except TimeoutError:
        pass
    event.clear()


async def _async_complete_startup_grace(self: EnergyPlannerCoordinator) -> tuple[bool, str]:
    """Wait for grace or confirmed load recovery, then validate a fresh plan."""
    deadline = self._startup_auto_recovery_deadline
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        deadline = monotonic() + STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
        self._startup_auto_recovery_deadline = deadline
    remaining = max(deadline - monotonic(), 0.0)
    if remaining:
        await _wait_for_load_recovery(self, remaining)
    if not self._startup_auto_recovery_authorized:
        return False, "startup_recovery_cancelled"
    # Keep ordinary transient fallback notifications suppressed while the
    # awaited deadline refresh is in flight. The result below either clears
    # this suppression silently or replaces it with one recovery warning.
    self.executor.notification_grace_until = datetime.max.replace(tzinfo=UTC)
    validation_ok, reason = await self._async_run_startup_auto_recovery_validation()
    if not self._startup_auto_recovery_authorized:
        return False, "startup_recovery_cancelled"
    if not validation_ok:
        return False, reason
    self._startup_auto_recovery_authorized = False
    await self._async_update_startup_auto_recovery(
        "recovered",
        successful_runs=1,
        required_runs=1,
        reason="startup_grace_completed_healthy",
        completed=True,
    )
    self.executor.notification_grace_until = None
    self._export_retry_startup_blocked = False
    self._schedule_export_retry()
    await self.executor.async_dismiss_startup_recovery_notification()
    return True, "startup_grace_completed_healthy"


async def _async_enter_startup_safe_recovery(self: EnergyPlannerCoordinator, reason: str) -> None:
    """Disarm an unsafe startup while preserving automatic-control intent."""
    self._startup_auto_recovery_authorized = True
    self._startup_auto_recovery_deadline = None
    # Persist a restart-resumable transition before the first operation
    # that changes command authority. If shutdown cancels the subsequent
    # restore, the next process must still recognize this as disarmed
    # recovery rather than a previously operator-disarmed installation.
    await self._async_update_startup_auto_recovery(
        "restoring",
        successful_runs=0,
        reason=reason,
    )
    await self.async_disarm_production_control("startup_grace_unsafe")
    restore_reason = reason
    try:
        restore = await self.async_restore_safe_state("startup_grace_unsafe", refresh=False)
        if _action_outcome_failed(restore):
            restore_reason = str(getattr(restore, "reason", "safe_state_restore_failed"))
    except Exception:  # noqa: BLE001 - the production gate is already disarmed.
        _LOGGER.exception("Could not restore safe state after unsafe startup grace")
        restore_reason = "safe_state_restore_failed"
    if not self._startup_auto_recovery_authorized:
        return
    await self._async_update_startup_auto_recovery(
        "waiting_for_safe",
        successful_runs=0,
        reason=restore_reason or reason,
    )
    await self.executor.async_notify_startup_recovery_unsafe(restore_reason or reason)
    self.executor.notification_grace_until = None
    self._export_retry_startup_blocked = False
    self._schedule_export_retry()


async def _async_retry_startup_safe_recovery(self: EnergyPlannerCoordinator) -> None:
    """Retry until one complete fresh safety validation can reactivate control."""
    successful_runs = 0
    while self._startup_auto_recovery_authorized and self.automatic_control_requested:
        await _wait_for_load_recovery(self, STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS)
        if not self._startup_auto_recovery_authorized or not self.automatic_control_requested:
            return
        await self._async_update_startup_auto_recovery(
            "validating",
            successful_runs=successful_runs,
            reason="validation_in_progress",
        )
        validation_ok, reason = await self._async_run_startup_auto_recovery_validation()
        if not self._startup_auto_recovery_authorized:
            return
        if not validation_ok:
            successful_runs = 0
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason=reason,
            )
            await self.executor.async_notify_startup_recovery_unsafe(reason)
        else:
            successful_runs += 1
            await self._async_update_startup_auto_recovery(
                "validating",
                successful_runs=successful_runs,
                reason="validation_succeeded",
            )
            recovered, reason = await self._async_reactivate_after_startup_recovery(
                successful_runs
            )
            if recovered or not self._startup_auto_recovery_authorized:
                return
            successful_runs = 0
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason=reason,
            )
            await self.executor.async_notify_startup_recovery_unsafe(reason)


async def _async_reactivate_after_startup_recovery(
    self: EnergyPlannerCoordinator,
    successful_runs: int,
) -> tuple[bool, str]:
    """Restore, arm, and verify a recovered startup installation."""
    await self._async_update_startup_auto_recovery(
        "restoring",
        successful_runs=successful_runs,
        reason="healthy_validation_sequence_complete",
    )
    try:
        restore = await self.async_restore_safe_state("startup_auto_recovery", refresh=False)
    except Exception:  # noqa: BLE001 - recovery remains disarmed and retries.
        _LOGGER.exception("Could not restore safe state before startup reactivation")
        return False, "safe_state_restore_failed"
    if _action_outcome_failed(restore):
        return False, str(getattr(restore, "reason", "safe_state_restore_failed"))
    if not self._startup_auto_recovery_authorized:
        return False, "startup_recovery_cancelled"

    production = parse_production_state(self.store.data.get("production")).raw
    production.update(
        {
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                self.entry_data,
                self.options,
            ),
            "dry_run_ready_cycles": DRY_RUN_READY_CYCLES_REQUIRED,
            "last_dry_run_ready_at": dt_util.utcnow(),
        }
    )
    await self._async_save_production(production)
    if not self._startup_auto_recovery_authorized:
        return False, "startup_recovery_cancelled"
    report = build_preflight_report(self.hass, self)
    final_ready, reason = _startup_auto_recovery_validation_ready(report, self.entry_data)
    if not final_ready or not report.get("safe_to_activate_now"):
        return False, reason if not final_ready else "final_preflight_failed"

    # Cancellation can revoke recovery while production is provisionally
    # armed. Keep export writes closed until validation or a later operator arm.
    await self.async_arm_production_control("startup_auto_recovered", provisional=True)
    try:
        await self._async_compensate_ev_auto_start(
            require_unowned=False,
            refresh=False,
        )
        await self._async_refresh_with_export_preflight("startup_auto_recovery_activation")
    except Exception:  # noqa: BLE001 - a failed activation refresh must fail closed.
        _LOGGER.exception("Startup automatic-control activation refresh failed")
        await self.async_disarm_production_control("startup_auto_recovery_replan_failed")
        try:
            await self.async_restore_safe_state(
                "startup_auto_recovery_replan_failed",
                refresh=False,
            )
        except Exception:  # noqa: BLE001 - the production gate remains disarmed.
            _LOGGER.exception("Could not restore safe state after failed recovery refresh")
        return False, "active_replan_failed"

    if not self._startup_auto_recovery_authorized:
        return False, "startup_recovery_cancelled"

    final_report = build_preflight_report(self.hass, self)
    final_ready, reason = _startup_auto_recovery_validation_ready(final_report, self.entry_data)
    if not final_ready or not final_report.get("active_control_ready"):
        await self.async_disarm_production_control("startup_auto_recovery_replan_unsafe")
        try:
            await self.async_restore_safe_state(
                "startup_auto_recovery_replan_unsafe",
                refresh=False,
            )
        except Exception:  # noqa: BLE001 - the production gate remains disarmed.
            _LOGGER.exception("Could not restore safe state after unsafe recovery replan")
        return False, reason if not final_ready else "active_replan_unsafe"

    self._startup_auto_recovery_authorized = False
    await self._async_update_startup_auto_recovery(
        "recovered",
        successful_runs=successful_runs,
        reason="automatic_control_reactivated",
        completed=True,
    )
    self._export_startup_activation_pending = False
    self._schedule_export_retry()
    if self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True:
        self._schedule_debounced_refresh("startup_export_activation", debounce_seconds=0, force=True)
    await self.executor.async_dismiss_startup_recovery_notification()
    return True, "automatic_control_reactivated"


async def _async_run_startup_auto_recovery_validation(self: EnergyPlannerCoordinator) -> tuple[bool, str]:
    """Run one forced refresh while suppressing every device action."""
    self._startup_auto_recovery_validation_active = True
    self._last_startup_auto_recovery_validation = None
    try:
        self._mark_forced_refresh("startup_auto_recovery_validation")
        # This check is a safety boundary: it must await a newly committed
        # plan instead of returning after the coordinator debounce accepts
        # a refresh request.
        await self.async_refresh()
    except Exception:  # noqa: BLE001 - one failed validation resets the sequence.
        _LOGGER.exception("Startup automatic-control validation refresh failed")
        return False, "validation_refresh_failed"
    finally:
        self._startup_auto_recovery_validation_active = False
        self._resume_ev_capacity_after_validation()
    validation = self._last_startup_auto_recovery_validation
    if not isinstance(validation, dict) or validation.get("committed") is not True:
        return False, "validation_plan_not_committed"
    if validation.get("safe") is not True or validation.get("violations"):
        return False, "validation_plan_unsafe"
    report = build_preflight_report(self.hass, self)
    ready, reason = _startup_auto_recovery_validation_ready(report, self.entry_data)
    return (True, "validation_succeeded") if ready else (False, reason)


def _record_startup_auto_recovery_validation_candidate(
    self: EnergyPlannerCoordinator,
    plan: EnergyPlan,
    violations: list[str],
) -> None:
    """Capture one validation candidate before the generation-safe commit."""
    if not getattr(self, "_startup_auto_recovery_validation_active", False):
        return
    self._last_startup_auto_recovery_validation = {
        "plan_id": plan.plan_id,
        "healthy": plan.health == InputHealth.HEALTHY and plan.status != "unsafe",
        "safe": (plan.health in {InputHealth.HEALTHY, InputHealth.DEGRADED}
        and plan.status == "current") or area_safe(plan),
        "violations": [] if area_safe(plan) else list(violations),
        "committed": False,
    }


async def _async_update_startup_auto_recovery(
    self: EnergyPlannerCoordinator,
    status: str,
    *,
    successful_runs: int,
    reason: str,
    required_runs: int = STARTUP_AUTO_RECOVERY_REQUIRED_RUNS,
    started: bool = False,
    completed: bool = False,
    disarm_reason: str | None = None,
) -> None:
    """Persist compact recovery progress for entities and diagnostics."""
    production = parse_production_state(self.store.data.get("production")).raw
    current = production.get("startup_auto_recovery")
    recovery = dict(current) if isinstance(current, dict) else {}
    now = dt_util.utcnow()
    recovery.update(
        {
            "status": status,
            "successful_runs": _startup_auto_recovery_successful_runs(successful_runs),
            "required_runs": required_runs,
            "last_reason": str(reason)[:160],
            "updated_at": now,
        }
    )
    if status == "waiting_for_home_assistant":
        recovery.pop("started_at", None)
        recovery.pop("deadline", None)
        recovery.pop("completed_at", None)
    if started:
        recovery.update(
            {
                "started_at": now,
                "deadline": now + timedelta(seconds=STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS),
            }
        )
        recovery.pop("completed_at", None)
    if completed:
        self._startup_auto_recovery_authorized = False
        self._startup_auto_recovery_deadline = None
        recovery["completed_at"] = now
    if status == "recovered":
        production.pop("startup_activation_pending", None)
    if disarm_reason is not None:
        await self.async_disarm_production_control(disarm_reason, recovery_state=recovery)
    else:
        production["startup_auto_recovery"] = recovery
        await self._async_save_production(production)
        self.async_update_listeners()


def _active_control_not_ready_reason(report: dict[str, Any]) -> str:
    """Return one actionable reason that the combined activation was rejected."""
    production = dict(report.get("production", {}))
    ready_cycles = parse_production_state(production).dry_run_ready_cycles
    if not production.get("dry_run_evidence_complete"):
        return (
            f"review mode has recorded {ready_cycles}/{DRY_RUN_READY_CYCLES_REQUIRED} healthy plans; "
            "wait for the readiness sensor, review the plan, then turn Automatic control on again"
        )
    for check in report.get("checks", []):
        if check.get("blocking") and not check.get("ok"):
            return str(check.get("message") or "a safety check failed")
    return str(report.get("current_plan", {}).get("message") or "a safety check failed")


def _startup_auto_recovery_prerequisites(
    report: dict[str, Any],
    entry_data: dict[str, Any],
) -> tuple[bool, str]:
    """Return whether startup dependencies are ready without trusting bypasses."""
    control_areas = dict(report.get("control_areas", {}))
    required = list(control_areas.get("required", []))
    if not required:
        return False, "no_required_control_areas"
    if not control_areas.get("ready"):
        return False, "no_ready_control_area"
    if not control_areas.get("available"):
        return False, "control_paused"
    if not control_areas.get("confidence_eligible"):
        return False, "no_confidence_eligible_control_area"
    if (entry_data.get(CONF_HOUSEHOLD_LOAD)
            and not bool(dict(report.get("recorder", {})).get("available"))
            and not bool(dict(report.get("current_plan", {})).get("export_limit_safe"))):
        return False, "recorder_unavailable"
    return True, "startup_dependencies_ready"


def _startup_auto_recovery_validation_ready(
    report: dict[str, Any],
    entry_data: dict[str, Any],
) -> tuple[bool, str]:
    """Return whether a committed recovery plan passes all non-production gates."""
    ready, reason = _startup_auto_recovery_prerequisites(report, entry_data)
    if not ready:
        if reason == "no_ready_control_area" and _startup_waiting_for_dry_mode(report, entry_data):
            return False, HVAC_DRY_ZONE_TARGETS_ABSENT
        return ready, reason
    if not bool(dict(report.get("current_plan", {})).get("safe")):
        return False, "current_plan_unsafe"
    return True, "validation_succeeded"


def _startup_waiting_for_dry_mode(report: dict[str, Any], entry_data: dict[str, Any]) -> bool:
    """Distinguish a temporary HVAC-only startup block from faulty prerequisites."""
    entities = report.get("entities", {})
    return (
        report.get("control_areas", {}).get("required") == ["hvac"]
        and report.get("discovery", {}).get("hvac", {}).get("issues") == [HVAC_DRY_ZONE_TARGETS_ABSENT]
        and bool(report.get("current_plan", {}).get("safe"))
        and not (entities.get("missing") or entities.get("unavailable"))
        and (not entry_data.get(CONF_HOUSEHOLD_LOAD) or bool(report.get("recorder", {}).get("available")))
    )


def _action_outcome_failed(outcome: Any) -> bool:
    """Return whether a restore outcome explicitly reports failure."""
    result = getattr(outcome, "result", None)
    return str(getattr(result, "value", result)).lower() == "failed"


def _startup_auto_recovery_successful_runs(value: Any) -> int:
    """Return a bounded fail-closed recovery progress counter."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, STARTUP_AUTO_RECOVERY_REQUIRED_RUNS)
