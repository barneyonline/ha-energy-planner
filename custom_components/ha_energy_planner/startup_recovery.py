"""Startup recovery boundary; coordinator retains mutable state and lock authority."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HOUSEHOLD_LOAD,
    DOMAIN,
)
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

if TYPE_CHECKING:
    from .coordinator import EnergyPlannerCoordinator

_LOGGER = logging.getLogger(__name__)

STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS = 10 * 60

STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS = 30

STARTUP_AUTO_RECOVERY_REQUIRED_RUNS = 3

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

    async def _async_start_recovery(_hass: HomeAssistant) -> None:
        self._startup_auto_recovery_start_unsub = None
        if not self._startup_auto_recovery_authorized:
            return
        store_data = getattr(getattr(self, "store", None), "data", {})
        production = parse_production_state(
            store_data.get("production") if isinstance(store_data, dict) else None
        )
        if production.armed:
            self._startup_auto_recovery_deadline = monotonic() + STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
            self.executor.notification_grace_until = dt_util.utcnow() + timedelta(
                seconds=STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
            )
            await self._async_update_startup_auto_recovery(
                "grace",
                successful_runs=0,
                required_runs=1,
                reason="startup_grace_in_progress",
                started=True,
            )
        else:
            self._startup_auto_recovery_deadline = None
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason="startup_safe_recovery_resumed",
            )
        self._startup_auto_recovery_task = self.entry.async_create_background_task(
            self.hass,
            self._async_run_startup_auto_recovery(),
            f"{DOMAIN} startup automatic-control recovery",
        )

    self._startup_auto_recovery_start_unsub = async_at_started(
        self.hass,
        _async_start_recovery,
    )


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
) -> None:
    """Cancel startup recovery without treating persisted progress as authorization."""
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
    if hasattr(self, "executor"):
        self.executor.notification_grace_until = None
    start_unsub = getattr(self, "_startup_auto_recovery_start_unsub", None)
    if start_unsub is not None:
        start_unsub()
    self._startup_auto_recovery_start_unsub = None
    task = getattr(self, "_startup_auto_recovery_task", None)
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    self._startup_auto_recovery_task = None
    if not isinstance(store_data, dict):
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
    if not recovery_active or not isinstance(recovery, dict):
        return
    if preserve_control and reason in {"home_assistant_shutdown", "configuration_reload"}:
        # Persist the current grace/recovery state verbatim so the next
        # process can distinguish an armed restart from disarmed recovery.
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
            self._startup_auto_recovery_authorized = True
            self._startup_auto_recovery_task = self.entry.async_create_background_task(
                self.hass,
                self._async_run_startup_auto_recovery(),
                f"{DOMAIN} startup automatic-control recovery",
            )


async def _async_complete_startup_grace(self: EnergyPlannerCoordinator) -> tuple[bool, str]:
    """Wait for the full grace period and evaluate one fresh committed plan."""
    deadline = self._startup_auto_recovery_deadline
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        deadline = monotonic() + STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
        self._startup_auto_recovery_deadline = deadline
    remaining = max(deadline - monotonic(), 0.0)
    if remaining:
        await asyncio.sleep(remaining)
    if not self._startup_auto_recovery_authorized:
        return False, "startup_recovery_cancelled"
    # Keep ordinary transient fallback notifications suppressed while the
    # awaited deadline refresh is in flight. The result below either clears
    # this suppression silently or replaces it with one recovery warning.
    self.executor.notification_grace_until = datetime.max.replace(tzinfo=UTC)
    validation_ok, reason = await self._async_run_startup_auto_recovery_validation()
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
    await self._async_update_startup_auto_recovery(
        "waiting_for_safe",
        successful_runs=0,
        reason=restore_reason or reason,
    )
    await self.executor.async_notify_startup_recovery_unsafe(restore_reason or reason)
    self.executor.notification_grace_until = None


async def _async_retry_startup_safe_recovery(self: EnergyPlannerCoordinator) -> None:
    """Retry indefinitely until three fresh healthy plans can reactivate control."""
    successful_runs = 0
    while self._startup_auto_recovery_authorized and self.automatic_control_requested:
        await asyncio.sleep(STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS)
        if not self._startup_auto_recovery_authorized or not self.automatic_control_requested:
            return
        await self._async_update_startup_auto_recovery(
            "validating",
            successful_runs=successful_runs,
            reason="validation_in_progress",
        )
        validation_ok, reason = await self._async_run_startup_auto_recovery_validation()
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
            if successful_runs >= STARTUP_AUTO_RECOVERY_REQUIRED_RUNS:
                recovered, reason = await self._async_reactivate_after_startup_recovery(
                    successful_runs
                )
                if recovered:
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
    report = build_preflight_report(self.hass, self)
    final_ready, reason = _startup_auto_recovery_validation_ready(report, self.entry_data)
    if not final_ready or not report.get("safe_to_activate_now"):
        return False, reason if not final_ready else "final_preflight_failed"

    await self.async_arm_production_control("startup_auto_recovered")
    try:
        await self._async_compensate_ev_auto_start(
            require_unowned=False,
            refresh=False,
        )
        self._mark_forced_refresh("startup_auto_recovery_activation")
        await self.async_refresh()
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
        "safe": plan.health in {InputHealth.HEALTHY, InputHealth.DEGRADED}
        and plan.status == "current",
        "violations": list(violations),
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
    if entry_data.get(CONF_HOUSEHOLD_LOAD) and not bool(dict(report.get("recorder", {})).get("available")):
        return False, "recorder_unavailable"
    return True, "startup_dependencies_ready"


def _startup_auto_recovery_validation_ready(
    report: dict[str, Any],
    entry_data: dict[str, Any],
) -> tuple[bool, str]:
    """Return whether a committed recovery plan passes all non-production gates."""
    ready, reason = _startup_auto_recovery_prerequisites(report, entry_data)
    if not ready:
        return ready, reason
    if not bool(dict(report.get("current_plan", {})).get("safe")):
        return False, "current_plan_unsafe"
    return True, "validation_succeeded"


def _action_outcome_failed(outcome: Any) -> bool:
    """Return whether a restore outcome explicitly reports failure."""
    result = getattr(outcome, "result", None)
    return str(getattr(result, "value", result)).lower() == "failed"


def _startup_auto_recovery_successful_runs(value: Any) -> int:
    """Return a bounded fail-closed recovery progress counter."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, STARTUP_AUTO_RECOVERY_REQUIRED_RUNS)
