"""Direct EV charger execution adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.util import dt as dt_util

from .adapter_helpers import async_call_device_service, available_state, service_entity_available
from .const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
)
from .ev import ev_charging_state, ev_charging_state_for_stop, ev_charging_state_proves_active, ev_vehicle_suspended
from .ev_policy import PowerCapability, finite, native_limit_value_matches, power_capability
from .ev_runtime import NativeEVStopObservations, timestamp
from .ev_telemetry import current_sample_reported_at, measured, stop_signal_reported_at
from .models import ActionKind, PlanAction
from .vehicles import connection


@dataclass(slots=True)
class EVCommandResult:
    """Result of a direct EV charger action."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    command_sent: bool = False
    rollback_succeeded: bool | None = None
    safe_state_confirmed: bool | None = None
    power_limit_written: bool = False
    power_limit_superseded: bool = False
    stopped_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NativeEVStopEvidence:
    """Ordered evidence that the owned native charger is stopped."""

    confirmed_at: datetime
    activity_at: datetime | None

    def settlement_time(self, fallback: datetime) -> datetime:
        """Use the stop's source time whenever activity supplies an ordering boundary."""
        return self.confirmed_at if self.activity_at is not None else fallback


_CONTROL_KEYS = {
    CONF_EV_CHARGER,
    CONF_EV_SMART_CHARGING,
}


class EVChargerAdapter:
    """Execute planner decisions through configured charger controls."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        *,
        confirmation_timeout_seconds: float = 30.0,
        confirmation_retries: int = 1,
        confirmation_poll_seconds: float = 1.0,
        connected_override: bool | None = None,
        command_guard: Callable[[], bool] | None = None,
        start_guard: Callable[[], bool] | None = None,
        power_limit_context: Context | None = None,
        stopped_power_limit_baseline: dict[str, Any] | None = None,
        power_options: dict[str, Any] | None = None,
        retained_draw_at: Callable[[], datetime | None] | None = None,
    ) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data
        self.confirmation_timeout_seconds = max(float(confirmation_timeout_seconds), 0.0)
        self.confirmation_retries = max(int(confirmation_retries), 0)
        self.confirmation_poll_seconds = max(float(confirmation_poll_seconds), 0.01)
        self.connected_override = connected_override
        self.command_guard = command_guard or (lambda: True)
        self.start_guard = start_guard
        self.power_limit_context = power_limit_context
        self.stopped_power_limit_baseline = stopped_power_limit_baseline
        self.power_options = dict(power_options or {})
        self.retained_draw_at = retained_draw_at
        self.stop_observations = NativeEVStopObservations()

    async def async_execute(self, action: PlanAction) -> EVCommandResult:
        """Execute a supported EV action through Home Assistant services."""
        pre_state = self._snapshot()
        command_started_at = dt_util.utcnow()
        if (action.kind in {ActionKind.EV_START, ActionKind.EV_SCHEDULE}
                and action.desired_state.get("charging_required_now", True)
                and not action.desired_state.get("keep_charger_on") and self._vehicle_suspended()):
            return EVCommandResult(False, "ev_vehicle_suspended", pre_state, self._snapshot())
        lease_deadline = (timestamp(action.desired_state.get("charge_lease_until"))
                          if action.kind in {ActionKind.EV_START, ActionKind.EV_SCHEDULE}
                          and action.desired_state.get("charging_required_now", True) else None)
        if lease_deadline is not None and command_started_at >= lease_deadline:
            return EVCommandResult(False, "ev_allocation_expired", pre_state, self._snapshot())
        limit = action.desired_state.get("power_limit")
        limit_applied = False
        limit_sent = False
        if limit and bool(action.desired_state.get("charging_required_now", True)):
            if self.start_guard is not None:
                connection_rejection = self._start_connection_rejection()
                if connection_rejection or not self.start_guard():
                    return EVCommandResult(
                        False, connection_rejection or "ev_grid_projection_stale", pre_state, self._snapshot(),
                    )
            limit_applied, limit_sent = await self._async_set_power_limit(limit, valid_until=lease_deadline)
            if lease_deadline is not None and dt_util.utcnow() >= lease_deadline:
                result = await self._async_confirmation_failure(
                    reason="ev_allocation_expired", initial_pre_state=pre_state,
                    command_sent=limit_sent, requested_enabled=True,
                    command_entity_id=self._start_entity() or "", force_safe_stop=limit_sent,
                )
                if self.start_guard is not None and result.safe_state_confirmed is True:
                    return await self._async_reconcile_stopped_power_limit(
                        result, pre_state, limit, limit_confirmed=limit_applied,
                    )
                return result
            if not limit_applied:
                if not limit_sent and self.start_guard is not None:
                    return EVCommandResult(False, "ev_grid_projection_stale", pre_state, self._snapshot())
                refusal = self._vehicle_refusal_snapshot()
                stopped = await self._async_stop(require_stop_feedback=self._guarded_metered_cap())
                return EVCommandResult(False, "ev_power_limit_unconfirmed", pre_state, {**self._snapshot(), **refusal},
                                       command_sent=True, safe_state_confirmed=_safe_state_confirmed(stopped))
        if action.kind == ActionKind.EV_START:
            result = await self._async_start(action)
        elif action.kind == ActionKind.EV_STOP:
            result = await self._async_stop(
                require_stop_feedback=action.desired_state.get("restore_power_limit") is not None,
            )
        elif action.kind == ActionKind.EV_SCHEDULE:
            result = await self._async_schedule(action)
        else:
            return EVCommandResult(False, "unsupported_ev_action", pre_state, self._snapshot())

        if (
            result.applied and self.start_guard is not None and limit
            and action.kind in {ActionKind.EV_START, ActionKind.EV_SCHEDULE}
            and action.desired_state.get("charging_required_now", True)
            and not self._power_limit_is_confirmed(limit)
        ):
            # The cap can change during dispatch or while charging confirms.
            result = await self._async_confirmation_failure(
                reason="ev_grid_projection_stale", initial_pre_state=pre_state,
                command_sent=result.command_sent, requested_enabled=True, command_entity_id=self._start_entity() or "",
                force_safe_stop=result.command_sent,
                stop_feedback_required=bool(self.entry_data.get(CONF_EV_CHARGING)),
            )
        expired_after_cap_write = (
            self.start_guard is not None
            and result.reason == "ev_allocation_expired"
            and result.safe_state_confirmed is None
        )
        if (
            limit_sent and not result.command_sent
            and (result.reason == "ev_vehicle_suspended" or expired_after_cap_write)
        ):
            result = await self._async_confirmation_failure(
                reason=result.reason, initial_pre_state=pre_state, command_sent=True,
                requested_enabled=True, command_entity_id=self._start_entity() or "", force_safe_stop=True,
            )
        lifecycle_reconciled = False
        if limit_sent and result.safe_state_confirmed is not True and (
            result.reason == "ev_grid_projection_stale" or (
                self.start_guard is not None and not result.command_sent
                and result.reason in {"ev_not_connected", "ev_connected_state_unavailable"}
            )
        ):
            assert limit is not None
            previous_limit = finite(pre_state.get("ev_power_limit_entity"))
            limit_state = self._state(self.entry_data.get("ev_power_limit_entity"))
            current_limit = finite(getattr(limit_state, "state", None))
            ordered_stop = self._guarded_metered_cap()
            stop_is_current = self.native_restore_stop_is_current if ordered_stop else self._existing_state_proves_safe
            non_increasing = (
                previous_limit is not None and current_limit is not None and previous_limit <= current_limit
            )
            can_restore_without_stop = (
                not result.command_sent and previous_limit is not None
                and (
                    stop_is_current() or self._power_limit_was_superseded(limit_state, limit)
                    or (non_increasing and self._power_limit_is_confirmed(limit))
                )
            )
            if can_restore_without_stop:
                if ordered_stop and not non_increasing and self._power_limit_is_confirmed(limit):
                    # With a confirmed stop, restore the original lifecycle cap directly.
                    result = await self._async_reconcile_stopped_power_limit(
                        replace(result, safe_state_confirmed=True), pre_state, limit, limit_confirmed=True,
                    )
                    lifecycle_reconciled = True
                else:
                    rollback = await self._async_restore_power_limit(
                        pre_state, safe_state_confirmed=result.safe_state_confirmed, expected_limit=limit,
                    )
                    result = replace(result, rollback_succeeded=rollback.applied)
            if result.rollback_succeeded is not True and not (lifecycle_reconciled and result.safe_state_confirmed):
                stopped = await self._async_stop(require_stop_feedback=ordered_stop)
                result = replace(result, rollback_succeeded=False, safe_state_confirmed=_safe_state_confirmed(stopped))
        if (not lifecycle_reconciled and limit_applied and self.start_guard is not None
                and not result.applied and result.safe_state_confirmed is True):
            assert limit is not None
            result = await self._async_reconcile_stopped_power_limit(result, pre_state, limit, limit_confirmed=True)

        restore_limit = action.desired_state.get("restore_power_limit")
        if restore_limit is not None and not action.desired_state.get("charging_required_now", True):
            if not _safe_state_confirmed(result):
                return replace(result, applied=False, reason=_unconfirmed_stop_reason(result))
            saved_state = {
                "ev_power_limit_entity": restore_limit,
                "ev_power_limit_unit": action.desired_state.get("restore_power_limit_unit"),
            }
            restored = await self._async_restore_stopped_native_limit(result, saved_state, pre_state)
            result = replace(restored, reason=result.reason) if restored.applied else restored
        post_state = self._snapshot()
        if "vehicle_refusal_feedback" in result.post_state:
            post_state["vehicle_refusal_feedback"] = result.post_state["vehicle_refusal_feedback"]
            post_state["vehicle_refusal_at"] = result.post_state.get("vehicle_refusal_at")
        if limit and result.applied:
            post_state["pending_power_limit"] = {
                "version": 1, "at": command_started_at.isoformat(),
                "entity_id": limit["entity_id"], "value": limit["value"],
                "unit": limit["unit"], "physical_power_kw": limit["physical_power_kw"],
            }
            power_id = self.entry_data.get("ev_power_entity")
            power_state = self.hass.states.get(power_id) if power_id else None
            now = dt_util.utcnow()
            observed = measured(power_state, now, "power")
            reported = self._meter_reported_at(power_state, now)
            if observed is not None and reported is not None and reported >= command_started_at:
                if observed <= float(limit["physical_power_kw"]) + .01:
                    post_state["confirmed_power_kw"] = float(limit["physical_power_kw"])
                    post_state["confirmed_power_at"] = reported.isoformat()
        return EVCommandResult(
            result.applied,
            "ev_power_limit_confirmed" if result.applied and limit_sent else result.reason,
            pre_state,
            post_state,
            command_sent=result.command_sent or limit_sent,
            rollback_succeeded=result.rollback_succeeded,
            safe_state_confirmed=result.safe_state_confirmed,
            power_limit_written=limit_sent,
            power_limit_superseded=result.power_limit_superseded,
            stopped_at=result.stopped_at,
        )

    async def async_restore(
        self,
        saved_state: dict[str, Any] | None = None,
        *,
        command_entity_id: str | None = None,
    ) -> EVCommandResult:
        """Restore charger controls to saved state, or stop as a safe fallback."""
        pre_state = self._snapshot()
        if saved_state and "ev_power_limit_entity" in saved_state:
            stopped = await self._async_stop(require_stop_feedback=True)
            if not _safe_state_confirmed(stopped):
                return replace(stopped, applied=False, reason=_unconfirmed_stop_reason(stopped))
            return await self._async_restore_stopped_native_limit(stopped, saved_state, pre_state)
        if saved_state:
            restore_entity_id = command_entity_id or self._start_entity()
            if restore_entity_id and self._start_command_requires_safe_stop(restore_entity_id):
                safe_stop = await self._async_stop()
                safely_stopped = _safe_state_confirmed(safe_stop)
                return EVCommandResult(
                    safely_stopped,
                    ("ev_saved_state_safe_stop" if safely_stopped else _unconfirmed_stop_reason(safe_stop)),
                    pre_state,
                    self._snapshot(),
                    command_sent=safe_stop.command_sent,
                    rollback_succeeded=safely_stopped,
                    safe_state_confirmed=safely_stopped,
                )
            attempted, restored = await self._async_restore_control_snapshot(
                saved_state,
                entity_ids={restore_entity_id} if restore_entity_id else None,
            )
            if not attempted:
                safe_stop = await self._async_stop()
                safely_stopped = _safe_state_confirmed(safe_stop)
                return EVCommandResult(
                    safely_stopped,
                    ("ev_saved_state_safe_stop" if safely_stopped else _unconfirmed_stop_reason(safe_stop)),
                    pre_state,
                    self._snapshot(),
                    command_sent=safe_stop.command_sent,
                    rollback_succeeded=safely_stopped,
                    safe_state_confirmed=safely_stopped,
                )
            return EVCommandResult(
                restored,
                "ev_saved_state_restored" if restored else "ev_saved_state_not_restorable",
                pre_state,
                self._snapshot(),
                command_sent=attempted,
                rollback_succeeded=restored if attempted else None,
                safe_state_confirmed=restored,
            )
        result = await self._async_stop()
        safely_stopped = _safe_state_confirmed(result)
        return EVCommandResult(
            safely_stopped,
            (
                "ev_safe_stop_restored"
                if safely_stopped and not result.applied
                else result.reason
                if safely_stopped
                else _unconfirmed_stop_reason(result)
            ),
            pre_state,
            self._snapshot(),
            command_sent=result.command_sent,
            rollback_succeeded=safely_stopped,
            safe_state_confirmed=safely_stopped,
        )

    async def async_set_ready_by(self, ready_by: str) -> EVCommandResult:
        """Update a legacy external ready-by helper during the compatibility window."""
        pre_state = self._snapshot()
        ready_by_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY)
        if not ready_by_entity:
            return EVCommandResult(False, "ev_ready_by_helper_not_configured", pre_state, self._snapshot())
        if not self._entity_value_matches(ready_by_entity, ready_by) and not self._can_set_entity_value(
            ready_by_entity
        ):
            return EVCommandResult(False, "ev_ready_by_helper_unsupported", pre_state, self._snapshot())
        if not await self._async_set_entity_value(ready_by_entity, ready_by):
            return EVCommandResult(False, "ev_ready_by_helper_unsupported", pre_state, self._snapshot())
        return EVCommandResult(True, "ev_ready_by_helper_updated", pre_state, self._snapshot())

    async def async_set_charging(self, enabled: bool) -> EVCommandResult:
        """Start or stop charging for a manual Energy Planner command."""
        return await self._async_start(None) if enabled else await self._async_stop()

    async def _async_start(self, action: PlanAction | None) -> EVCommandResult:
        deadline = timestamp(action.desired_state.get("charge_lease_until")) if action is not None else None
        power_limit = action.desired_state.get("power_limit") if action is not None else None
        if deadline is not None:
            if dt_util.utcnow() >= deadline:
                return EVCommandResult(False, "ev_allocation_expired", self._snapshot(), self._snapshot())
        if connection_rejection := self._start_connection_rejection():
            return EVCommandResult(False, connection_rejection, self._snapshot(), self._snapshot())
        keep_charger_on = bool(action and action.desired_state.get("keep_charger_on"))
        if keep_charger_on:
            keep_on_entity = self._keep_on_entity()
            if not keep_on_entity or keep_on_entity.split(".", 1)[0] not in {
                "switch",
                "input_boolean",
            }:
                return EVCommandResult(
                    False,
                    "ev_keep_on_requires_stateful_control",
                    self._snapshot(),
                    self._snapshot(),
                )
            return await self._async_control_with_confirmation(
                keep_on_entity,
                enabled=True,
                confirmation_entity=keep_on_entity,
                confirmation_reason="ev_charger_enabled_for_preconditioning",
                valid_until=deadline,
                power_limit=power_limit,
            )
        start_entity = self._start_entity()
        if self._vehicle_suspended():
            return EVCommandResult(False, "ev_vehicle_suspended", self._snapshot(), self._snapshot())
        if not start_entity:
            return EVCommandResult(False, "ev_start_control_not_configured", self._snapshot(), self._snapshot())
        return await self._async_control_with_confirmation(
            start_entity, enabled=True, valid_until=deadline, power_limit=power_limit,
        )

    def _start_connection_rejection(self) -> str | None:
        """Check the native helper before energizing the charger."""
        connected_entity = self.entry_data.get(CONF_EV_CONNECTED)
        if connected_entity:
            connected = self._state(connected_entity)
            if connected is None:
                return "ev_connected_state_unavailable"
            return None if _truthy_state(connected) else "ev_not_connected"
        return "ev_not_connected" if self.connected_override is False else None

    async def _async_stop(self, *, require_stop_feedback: bool = False) -> EVCommandResult:
        stop_entity = self._stop_entity()
        if not stop_entity:
            return EVCommandResult(False, "ev_stop_control_not_configured", self._snapshot(), self._snapshot())
        return await self._async_control_with_confirmation(
            stop_entity, enabled=False, require_stop_feedback=require_stop_feedback,
        )

    async def _async_control_with_confirmation(
        self,
        entity_id: str,
        *,
        enabled: bool,
        confirmation_entity: str | None = None,
        confirmation_reason: str | None = None,
        valid_until: datetime | None = None,
        power_limit: dict[str, Any] | None = None,
        require_stop_feedback: bool = False,
    ) -> EVCommandResult:
        """Issue a command and confirm the mapped charging feedback state."""
        charging_entity = confirmation_entity or self.entry_data.get(CONF_EV_CHARGING)
        if require_stop_feedback and charging_entity is None:
            charging_entity = entity_id
        initial_pre_state = self._snapshot()
        command_sent = False
        control_adopted = False
        stop_feedback_required = not enabled and bool(charging_entity) and (
            require_stop_feedback or self.start_guard is not None
        )
        for _attempt in range(self.confirmation_retries + 1):
            if enabled and self.start_guard is not None and (
                self._start_connection_rejection() or not self.start_guard()
                or (power_limit and not self._power_limit_is_confirmed(power_limit))
            ):
                # Charging can first appear during the compensating stop.
                return await self._async_confirmation_failure(
                    reason="ev_grid_projection_stale", initial_pre_state=initial_pre_state,
                    command_sent=command_sent, requested_enabled=True, command_entity_id=entity_id,
                    force_safe_stop=command_sent,
                    stop_feedback_required=bool(self.entry_data.get(CONF_EV_CHARGING)),
                )
            if enabled and confirmation_entity is None and self._vehicle_suspended():
                return await self._async_confirmation_failure(
                    reason="ev_vehicle_suspended", initial_pre_state=initial_pre_state,
                    command_sent=command_sent, requested_enabled=True, command_entity_id=entity_id,
                    force_safe_stop=control_adopted,
                )
            if valid_until is not None and dt_util.utcnow() >= valid_until:
                return await self._async_confirmation_failure(
                    reason="ev_allocation_expired", initial_pre_state=initial_pre_state,
                    command_sent=command_sent, requested_enabled=enabled, command_entity_id=entity_id,
                    force_safe_stop=control_adopted,
                )
            force_stop = False
            if not enabled:
                safely_stopped = (
                    self.native_restore_stop_is_current()
                    if require_stop_feedback else self._existing_state_proves_safe()
                )
                if safely_stopped:
                    return EVCommandResult(
                        True,
                        "ev_charging_stopped_confirmed" if command_sent else "already_in_desired_state",
                        initial_pre_state,
                        self._snapshot(),
                        command_sent=command_sent,
                        safe_state_confirmed=True,
                    )
                # Active feedback can arrive ahead of the switch update.
                force_stop = self._charging_feedback_is_active() or (
                    require_stop_feedback and self._has_current_meter_draw()
                )
                stop_feedback_required = stop_feedback_required or force_stop
            result = await self._async_call_control(entity_id, turn_on=enabled, force=force_stop)
            command_sent = (
                command_sent or result.command_sent or (result.applied and result.reason != "already_in_desired_state")
            )
            if require_stop_feedback and result.reason == "already_in_desired_state":
                if (not command_sent and self.stop_observations.activity_at is None
                        and self._latest_meter_draw_at() is None):
                    break
                # An in-flight stop or unproven cached feedback can still be followed by fresh readback.
            control_adopted = control_adopted or result.applied or command_sent
            # Dispatch can consume the remaining lease. Its coordinator timer
            # waits for the command lock, so this adapter must compensate now.
            if valid_until is not None and dt_util.utcnow() >= valid_until:
                return await self._async_confirmation_failure(
                    reason="ev_allocation_expired", initial_pre_state=initial_pre_state,
                    command_sent=command_sent, requested_enabled=enabled, command_entity_id=entity_id,
                    force_safe_stop=control_adopted,
                )
            if not result.applied and result.reason != "already_in_desired_state":
                if not command_sent:
                    return result
                if charging_entity:
                    confirmation = await self._async_confirm_state(
                        charging_entity,
                        enabled,
                        control_state=confirmation_entity is not None,
                        valid_until=valid_until,
                        stop_control=entity_id if stop_feedback_required else None,
                        ordered_stop=require_stop_feedback,
                    )
                    if confirmation == "expired":
                        return await self._async_confirmation_failure(
                            reason="ev_allocation_expired", initial_pre_state=initial_pre_state,
                            command_sent=command_sent, requested_enabled=enabled, command_entity_id=entity_id,
                            force_safe_stop=control_adopted,
                        )
                    if confirmation == "vehicle_suspended":
                        return await self._async_confirmation_failure(
                            reason="ev_vehicle_suspended", initial_pre_state=initial_pre_state,
                            command_sent=command_sent, requested_enabled=enabled, command_entity_id=entity_id,
                            force_safe_stop=True,
                        )
                    if confirmation == "confirmed":
                        safe_state_confirmed = None
                        if not enabled:
                            safe_state_confirmed = stop_feedback_required or await self._async_control_proves_safe(
                                entity_id
                            )
                        return EVCommandResult(
                            True,
                            confirmation_reason
                            or (
                                "ev_charging_confirmed_after_service_error"
                                if enabled
                                else "ev_charging_stopped_confirmed_after_service_error"
                            ),
                            initial_pre_state,
                            self._snapshot(),
                            command_sent=True,
                            safe_state_confirmed=safe_state_confirmed,
                        )
                if require_stop_feedback:
                    continue
                return await self._async_confirmation_failure(
                    reason=result.reason,
                    initial_pre_state=initial_pre_state,
                    command_sent=True,
                    requested_enabled=enabled,
                    command_entity_id=entity_id,
                    stop_feedback_required=stop_feedback_required,
                )
            if not charging_entity:
                safe_state_confirmed = None
                if not enabled:
                    safe_state_confirmed = await self._async_control_proves_safe(entity_id)
                return EVCommandResult(
                    result.applied,
                    result.reason,
                    initial_pre_state,
                    self._snapshot(),
                    command_sent=command_sent,
                    safe_state_confirmed=safe_state_confirmed,
                )
            confirmation = await self._async_confirm_state(
                charging_entity,
                enabled,
                control_state=confirmation_entity is not None,
                valid_until=valid_until,
                stop_control=entity_id if stop_feedback_required else None,
                ordered_stop=require_stop_feedback,
            )
            if confirmation == "expired":
                return await self._async_confirmation_failure(
                    reason="ev_allocation_expired", initial_pre_state=initial_pre_state,
                    command_sent=command_sent, requested_enabled=enabled, command_entity_id=entity_id,
                    force_safe_stop=control_adopted,
                )
            if confirmation == "confirmed":
                safe_state_confirmed = None
                if not enabled:
                    safe_state_confirmed = stop_feedback_required or await self._async_control_proves_safe(entity_id)
                return EVCommandResult(
                    True,
                    (
                        result.reason
                        if not command_sent
                        else confirmation_reason
                        or ("ev_charging_confirmed" if enabled else "ev_charging_stopped_confirmed")
                    ),
                    initial_pre_state,
                    self._snapshot(),
                    command_sent=command_sent,
                    safe_state_confirmed=safe_state_confirmed,
                )
            if require_stop_feedback:
                if not command_sent:
                    break
                continue
            if confirmation == "unavailable":
                return await self._async_confirmation_failure(
                    reason="ev_charging_confirmation_unavailable",
                    initial_pre_state=initial_pre_state,
                    command_sent=command_sent,
                    requested_enabled=enabled,
                    command_entity_id=entity_id,
                    stop_feedback_required=stop_feedback_required,
                )
            if confirmation == "vehicle_suspended":
                return await self._async_confirmation_failure(
                    reason="ev_vehicle_suspended", initial_pre_state=initial_pre_state,
                    command_sent=command_sent, requested_enabled=enabled, command_entity_id=entity_id,
                    force_safe_stop=True,
                )
        if require_stop_feedback:
            return EVCommandResult(False, "ev_stop_not_confirmed", initial_pre_state, self._snapshot(),
                                   command_sent=command_sent, safe_state_confirmed=False)
        return await self._async_confirmation_failure(
            reason="ev_charging_confirmation_timeout",
            initial_pre_state=initial_pre_state,
            command_sent=command_sent,
            requested_enabled=enabled,
            command_entity_id=entity_id,
            stop_feedback_required=stop_feedback_required,
        )

    async def _async_control_proves_safe(self, entity_id: str | None) -> bool:
        """Return whether a stateful charger control is confirmed off."""
        if not entity_id or entity_id.split(".", 1)[0] not in {
            "switch",
            "input_boolean",
        }:
            return False
        return (
            await self._async_confirm_state(
                entity_id,
                False,
                control_state=True,
            )
            == "confirmed"
        )

    def _existing_state_proves_safe(self) -> bool:
        """Check the off control and inactive or disconnected charging feedback."""
        return self._control_is_off(self._stop_entity()) and self._charging_has_stopped(
            self._state(self.entry_data.get(CONF_EV_CHARGING)),
        )

    def _guarded_metered_cap(self) -> bool:
        """Require metered stop proof when compensating a guarded native-cap start."""
        return self.start_guard is not None and bool(
            self.entry_data.get("ev_power_limit_entity") and self.entry_data.get("ev_power_entity")
        )

    async def _async_restore_stopped_native_limit(
        self, stopped: EVCommandResult, saved_state: dict[str, Any], pre_state: dict[str, Any],
    ) -> EVCommandResult:
        stopped = self.revalidate_native_restore(stopped)
        if not _safe_state_confirmed(stopped):
            return stopped
        limit_result = await self._async_restore_power_limit(
            saved_state, safe_state_confirmed=True, pre_state=pre_state,
        )
        return self.revalidate_native_restore(replace(
            limit_result, command_sent=stopped.command_sent or limit_result.command_sent,
            stopped_at=stopped.stopped_at,
        ), saved_state)

    def revalidate_native_restore(
        self, result: EVCommandResult, saved_state: dict[str, Any] | None = None,
    ) -> EVCommandResult:
        """Require current stop evidence after an awaited native-cap or Store write."""
        evidence = self.native_restore_stop_evidence()
        if evidence is None:
            return replace(result, applied=False, reason="ev_stop_not_confirmed", post_state=self._snapshot(),
                           rollback_succeeded=False, safe_state_confirmed=False, stopped_at=None)
        stopped_at = evidence.settlement_time(result.stopped_at or dt_util.utcnow())
        result = replace(result, stopped_at=stopped_at)
        if result.applied and saved_state is not None and not self.native_restore_limit_is_current(saved_state):
            return replace(result, applied=False, reason="ev_power_limit_restore_failed", post_state=self._snapshot(),
                           rollback_succeeded=False)
        return result

    def native_restore_stop_is_current(self) -> bool:
        """Require stop evidence without a newer contradictory metered draw."""
        return self.native_restore_stop_evidence() is not None

    def _latest_meter_draw_at(self) -> datetime | None:
        observed = self.stop_observations.draw_at
        retained = self.retained_draw_at() if self.retained_draw_at is not None else None
        if observed is None:
            return retained
        return max(observed, retained or observed)

    def _native_stop_signal_at(self, state: State, now: datetime, received_by: datetime | None) -> datetime | None:
        if received_by is not None and state.entity_id in self.stop_observations.stop_receipts:
            return self.stop_observations.stop_received_by(state.entity_id, received_by)
        return stop_signal_reported_at(
            state, now, received_by=received_by,
            implicit_clock=self.stop_observations.implicit_signal_clocks.get(state.entity_id),
        )

    def native_restore_stop_evidence(self, *, received_by: datetime | None = None) -> NativeEVStopEvidence | None:
        """Return the current stop proof and the feedback that can supersede earlier proof."""
        now = dt_util.utcnow()
        control = self._off_control_state(self._stop_entity())
        if control is None or (control_at := self._native_stop_signal_at(control, now, received_by)) is None:
            return None
        charging_entity = self.entry_data.get(CONF_EV_CHARGING)
        charging = self._state(charging_entity)
        connected = self._state(self.entry_data.get(CONF_EV_CONNECTED))
        charging_at = (
            self._native_stop_signal_at(charging, now, received_by)
            if charging is not None and _charging_state_matches(charging, False) is True else None
        )
        disconnected_at = (
            self._native_stop_signal_at(connected, now, received_by)
            if connected is not None and connection(connected.state) is False else None
        )
        if charging_entity and charging_at is None and disconnected_at is None and self.connected_override is not False:
            return None
        power = self._state(self.entry_data.get("ev_power_entity"))
        report_at = self._meter_reported_at(power, now)
        draw = measured(power, now, "power") if report_at is not None else None
        activity_at = self.stop_observations.active_at
        if (latest_draw := self._latest_meter_draw_at()) is not None:
            activity_at = max(activity_at or latest_draw, latest_draw)
        if draw and report_at is not None:
            activity_at = max(activity_at or report_at, report_at)
        if activity_at is None:
            return NativeEVStopEvidence(control_at, None)

        zero_at = report_at if draw == 0 else None
        if received_by is not None and zero_at is not None and power is not None:
            receipt = (power.last_updated if "sampled_at_utc" in power.attributes
                       else getattr(power, "last_reported", power.last_updated))
            if receipt > received_by:
                zero_at = self.stop_observations.stop_received_by(power.entity_id, received_by)
        current_stop_at = min((
            at for at in (control_at, charging_at, disconnected_at, zero_at)
            if at is not None and at > activity_at
        ), default=None)
        if current_stop_at is None:
            return None
        retained_stop_at = self.stop_observations.first_stop_after(activity_at)
        return NativeEVStopEvidence(min(current_stop_at, retained_stop_at or current_stop_at), activity_at)

    def _has_current_meter_draw(self) -> bool:
        power = self._state(self.entry_data.get("ev_power_entity"))
        now = dt_util.utcnow()
        return self._meter_reported_at(power, now) is not None and bool(measured(power, now, "power"))

    def _meter_reported_at(self, power: State | None, now: datetime) -> datetime | None:
        if power is None:
            return None
        return current_sample_reported_at(
            power, now, signal_clock=self.stop_observations.implicit_signal_clocks.get(power.entity_id),
        )

    def native_restore_limit_is_current(self, saved_state: dict[str, Any]) -> bool:
        """Check that the restored cap still matches the approved value."""
        state = self._state(self.entry_data.get("ev_power_limit_entity"))
        value = finite(saved_state.get("ev_power_limit_entity"))
        unit = saved_state.get("ev_power_limit_unit")
        return (
            state is not None and value is not None
            and (unit is None or unit == state.attributes.get("unit_of_measurement"))
            and _number_value_in_bounds(state, value) and native_limit_value_matches(state, value)
        )

    def _control_is_off(self, entity_id: str | None) -> bool:
        return self._off_control_state(entity_id) is not None

    def _off_control_state(self, entity_id: str | None) -> State | None:
        if not entity_id or entity_id.split(".", 1)[0] not in {"switch", "input_boolean"}:
            return None
        state = self._state(entity_id)
        return state if state is not None and _control_state_matches(state, False) else None

    def _charging_has_stopped(self, charging_state: State | None, *, received_by: datetime | None = None) -> bool:
        connected_state = self._state(self.entry_data.get(CONF_EV_CONNECTED))
        if self.connected_override is False:
            return True
        now = dt_util.utcnow() if received_by is not None else None
        if connected_state is not None and connection(connected_state.state) is False:
            if now is None or self._native_stop_signal_at(connected_state, now, received_by) is not None:
                return True
        return (charging_state is not None and _charging_state_matches(charging_state, False) is True
                and (now is None or self._native_stop_signal_at(charging_state, now, received_by) is not None))

    def _charging_feedback_is_active(self) -> bool:
        state = self._state(self.entry_data.get(CONF_EV_CHARGING))
        return state is not None and ev_charging_state_proves_active(state.state)

    async def _async_confirm_state(
        self, entity_id: str, enabled: bool, *, control_state: bool = False, valid_until: datetime | None = None,
        stop_control: str | None = None, ordered_stop: bool = False,
    ) -> str:
        """Wait for charging feedback or a stateful control to match the request."""
        if stop_control is not None:
            return await self._async_confirm_stop(stop_control, entity_id, extend_for_control=True,
                                                  ordered_stop=ordered_stop)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.confirmation_timeout_seconds
        while True:
            remaining_lease = (valid_until - dt_util.utcnow()).total_seconds() if valid_until is not None else None
            if remaining_lease is not None and remaining_lease <= 0:
                return "expired"
            state = self._state(entity_id)
            if state is None:
                return "unavailable"
            if enabled and not control_state and ev_vehicle_suspended(state.state):
                return "vehicle_suspended"
            matches = (
                _control_state_matches(state, enabled) if control_state else _charging_state_matches(state, enabled)
            )
            if matches is True:
                return "confirmed"
            if loop.time() >= deadline:
                return "timeout"
            await asyncio.sleep(min(self.confirmation_poll_seconds, max(deadline - loop.time(), 0.0),
                                    remaining_lease if remaining_lease is not None else self.confirmation_poll_seconds))

    async def _async_confirm_stop(
        self, control_entity: str, charging_entity: str | None, *, extend_for_control: bool, ordered_stop: bool = False,
    ) -> str:
        """Require both stop signals together and allow their updates to arrive separately."""
        loop = asyncio.get_running_loop()
        timeout = self.confirmation_timeout_seconds
        deadline = loop.time() + timeout
        receipt_window = timedelta(seconds=timeout)
        receipt_deadline = dt_util.utcnow() + receipt_window if ordered_stop else None
        received_by: datetime | None = None
        waiting_for_control = False
        while True:
            charging_state = self._state(charging_entity)
            charging_stopped = self._charging_has_stopped(charging_state)
            if charging_stopped:
                control = self._off_control_state(control_entity)
                if control is not None and (not ordered_stop or self.native_restore_stop_is_current()):
                    if received_by is None or self.native_restore_stop_evidence(received_by=received_by):
                        return "confirmed"
                if extend_for_control and not waiting_for_control and (
                    control is None or (received_by is not None
                                        and self._native_stop_signal_at(control, dt_util.utcnow(), received_by) is None)
                ) and (not ordered_stop or self._charging_has_stopped(
                    charging_state, received_by=received_by or dt_util.utcnow(),
                )):
                    waiting_for_control = True
                    if received_by is not None:
                        # A delayed wake must not give the switch an unbounded extra window.
                        deadline += timeout
                        receipt_deadline = received_by + receipt_window
                        received_by = receipt_deadline if loop.time() >= deadline else None
                        continue
                    # Preserve the switch's readback window once charging stops.
                    deadline = loop.time() + timeout
                    if ordered_stop:
                        receipt_deadline = dt_util.utcnow() + receipt_window
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "unavailable" if charging_state is None and not charging_stopped else "timeout"
            await asyncio.sleep(min(self.confirmation_poll_seconds, remaining))
            if ordered_stop and loop.time() >= deadline:
                # A delayed wake can still confirm evidence received before the cutoff.
                received_by = receipt_deadline

    async def _async_confirmation_failure(
        self,
        *,
        reason: str,
        initial_pre_state: dict[str, Any],
        command_sent: bool,
        requested_enabled: bool,
        command_entity_id: str,
        force_safe_stop: bool = False,
        stop_feedback_required: bool = False,
    ) -> EVCommandResult:
        """Compensate a command whose requested device state was not confirmed."""
        if not command_sent and not force_safe_stop:
            return EVCommandResult(False, reason, initial_pre_state, self._snapshot())
        refusal = self._vehicle_refusal_snapshot()
        force_safe_stop = force_safe_stop or bool(refusal)
        guarded_start = requested_enabled and self.start_guard is not None
        attempted = False
        restored = False
        command_requires_safe_stop = self._start_command_requires_safe_stop(command_entity_id)
        if (requested_enabled and not guarded_start and not force_safe_stop and reason != "ev_allocation_expired"
                and not command_requires_safe_stop):
            attempted, restored = await self._async_restore_control_snapshot(
                initial_pre_state,
                entity_ids={command_entity_id},
            )
        if not attempted:
            # A bounded start can have reached the charger while charging readback
            # was unavailable. Turning the switch off alone cannot clear its load.
            attempted, restored = await self._async_issue_safe_stop(
                check_active_feedback=not requested_enabled,
                require_stopped_feedback=stop_feedback_required or (
                    guarded_start and bool(self.entry_data.get(CONF_EV_CHARGING))
                ),
                ordered_stop=requested_enabled and self._guarded_metered_cap(),
            )
        post_state = {**self._snapshot(), **refusal}
        return EVCommandResult(
            False,
            reason,
            initial_pre_state,
            post_state,
            command_sent=command_sent or attempted,
            rollback_succeeded=restored if attempted else False,
            safe_state_confirmed=restored if attempted else False,
        )

    async def _async_restore_control_snapshot(
        self,
        saved_state: dict[str, Any],
        *,
        entity_ids: set[str] | None = None,
    ) -> tuple[bool, bool]:
        """Restore only writable charger controls from a prior adapter snapshot."""
        attempted = False
        restored = True
        seen_entities: set[str] = set()
        for key, state in saved_state.items():
            if key not in _CONTROL_KEYS or state not in {"on", "off"}:
                continue
            entity_id = self._configured_entity(key)
            if not entity_id or entity_id in seen_entities:
                continue
            if entity_ids is not None and entity_id not in entity_ids:
                continue
            seen_entities.add(entity_id)
            if entity_id.split(".", 1)[0] not in {"switch", "input_boolean"}:
                continue
            attempted = True
            result = await self._async_call_control(
                entity_id,
                turn_on=state == "on",
                force=True,
            )
            confirmed = result.applied and (
                await self._async_confirm_state(
                    entity_id,
                    state == "on",
                    control_state=True,
                )
                == "confirmed"
            )
            restored = restored and confirmed
        return attempted, attempted and restored

    async def _async_issue_safe_stop(
        self, *, check_active_feedback: bool, require_stopped_feedback: bool, ordered_stop: bool,
    ) -> tuple[bool, bool]:
        """Confirm the charger switch is off after an uncertain command."""
        stop_entity = self._stop_entity()
        if not stop_entity:
            return False, False
        # Retain positive feedback even if the charging entity drops out during dispatch.
        require_stopped_feedback = require_stopped_feedback or (
            check_active_feedback and self._charging_feedback_is_active()
        )
        result = await self._async_call_control(stop_entity, turn_on=False, force=True)
        if not (result.command_sent or result.applied):
            return False, False
        if ordered_stop or require_stopped_feedback or (check_active_feedback and self._charging_feedback_is_active()):
            charging_entity = self.entry_data.get(CONF_EV_CHARGING)
            confirmation = await self._async_confirm_stop(
                stop_entity, charging_entity or (stop_entity if ordered_stop else None),
                extend_for_control=False, ordered_stop=ordered_stop,
            )
            return True, confirmation == "confirmed"
        return True, result.applied and await self._async_control_proves_safe(stop_entity)

    async def _async_write_number(
        self, entity_id: str, value: float, *, valid_until: datetime | None = None,
        context: Context | None = None,
    ) -> bool | None:
        """Return None before dispatch, or whether a sent write was confirmed."""
        if not self.command_guard():
            return None
        if valid_until is not None and dt_util.utcnow() >= valid_until:
            return None
        state = self._state(entity_id)
        if state is None or not entity_id.startswith("number."):
            return None
        if not _number_value_in_bounds(state, value):
            return None
        try:
            await async_call_device_service(
                self.hass, "number", "set_value", {"entity_id": entity_id, "value": value}, context=context,
            )
            deadline = asyncio.get_running_loop().time() + self.confirmation_timeout_seconds
            while True:
                if not self.command_guard():
                    return False
                remaining_lease = (valid_until-dt_util.utcnow()).total_seconds() if valid_until is not None else None
                if remaining_lease is not None and remaining_lease <= 0:
                    return False
                state = self._state(entity_id)
                if native_limit_value_matches(state, value):
                    return True
                if asyncio.get_running_loop().time() >= deadline:
                    return False
                await asyncio.sleep(min(self.confirmation_poll_seconds,
                                        remaining_lease if remaining_lease is not None
                                        else self.confirmation_poll_seconds))
        except Exception:  # Device errors leave ownership and load reserved.
            return False

    async def _async_restore_power_limit(
        self, saved_state: dict[str, Any], *, safe_state_confirmed: bool | None,
        pre_state: dict[str, Any] | None = None,
        expected_limit: dict[str, Any] | None = None,
    ) -> EVCommandResult:
        if pre_state is None:
            pre_state = self._snapshot()
        entity_id = self.entry_data.get("ev_power_limit_entity")
        state = self._state(entity_id)
        if expected_limit is not None and not self._power_limit_is_confirmed(expected_limit):
            superseded = self._power_limit_was_superseded(state, expected_limit)
            reason = "ev_power_limit_superseded" if superseded else "ev_power_limit_restore_failed"
            return EVCommandResult(superseded, reason, pre_state, self._snapshot(),
                                   safe_state_confirmed=safe_state_confirmed)
        value = finite(saved_state["ev_power_limit_entity"])
        unit = saved_state.get("ev_power_limit_unit")
        if unit is not None and state is not None and unit != state.attributes.get("unit_of_measurement"):
            return EVCommandResult(False, "ev_power_limit_unit_changed", pre_state, self._snapshot(),
                                   safe_state_confirmed=safe_state_confirmed)
        if (
            expected_limit is None and state is not None and isinstance(entity_id, str)
            and entity_id.startswith("number.") and value is not None and _number_value_in_bounds(state, value)
            and native_limit_value_matches(state, value)
        ):
            return EVCommandResult(True, "ev_power_limit_restored", pre_state, self._snapshot(),
                                   safe_state_confirmed=safe_state_confirmed)
        write_result = None
        if entity_id and value is not None and (
            expected_limit is None or self._native_limit_accepts(state, value, unit)
        ):
            write_result = await self._async_write_number(entity_id, value)
        restored = write_result is True
        if restored and expected_limit is not None:
            # A matching number in changed units or bounds cannot prove rollback.
            assert value is not None
            restored = self._native_limit_accepts(self._state(entity_id), value, unit)
        return EVCommandResult(restored, "ev_power_limit_restored" if restored else "ev_power_limit_restore_failed",
                               pre_state, self._snapshot(), command_sent=write_result is not None,
                               safe_state_confirmed=safe_state_confirmed)

    async def _async_reconcile_stopped_power_limit(
        self, result: EVCommandResult, pre_state: dict[str, Any], limit: dict[str, Any], *, limit_confirmed: bool,
    ) -> EVCommandResult:
        """Release native-cap ownership only after a verified restore or supersession."""
        ordered_stop = self._guarded_metered_cap()
        if ordered_stop:
            verified = self.revalidate_native_restore(result)
            if not _safe_state_confirmed(verified):
                return replace(verified, reason=result.reason)
            result = verified
        if not (limit_confirmed or self._power_limit_is_confirmed(limit)):
            # The stop proves grid capacity is free. A delayed cap write may still arrive.
            return replace(result, rollback_succeeded=False)
        baseline = self.stopped_power_limit_baseline or pre_state
        rollback = await self._async_restore_power_limit(baseline, safe_state_confirmed=True, expected_limit=limit)
        verified = self.revalidate_native_restore(result) if ordered_stop else result
        stopped = _safe_state_confirmed(verified)
        return replace(
            result, safe_state_confirmed=stopped, stopped_at=verified.stopped_at,
            rollback_succeeded=stopped and rollback.applied and self.stopped_power_limit_baseline != {},
            post_state={**result.post_state, **rollback.post_state},
            power_limit_superseded=rollback.reason == "ev_power_limit_superseded",
        )

    @staticmethod
    def _native_limit_accepts(state: State | None, value: float, unit: str | None) -> bool:
        """Check the physical control's unit, bounds and increment for a setpoint."""
        if state is None or state.attributes.get("unit_of_measurement") != unit:
            return False
        minimum, maximum, step = (finite(state.attributes.get(key)) for key in ("min", "max", "step"))
        if minimum is None or maximum is None or step is None or step <= 0 or not minimum <= value <= maximum:
            return False
        remainder = (value - minimum) % step
        return min(remainder, step - remainder) < 1e-6

    def _power_limit_was_superseded(self, state: State | None, expected_limit: dict[str, Any]) -> bool:
        """Recognize a different valid setting in the same units as Planner's write."""
        if state is None or state.entity_id != expected_limit.get("entity_id"):
            return False
        current_value, expected_value = finite(state.state), finite(expected_limit.get("value"))
        return (
            current_value is not None and expected_value is not None and abs(current_value - expected_value) >= 1e-6
            and self._native_limit_accepts(state, current_value, expected_limit.get("unit"))
        )

    def _validated_power_limit(self, requested: dict[str, Any]) -> tuple[PowerCapability, float] | None:
        entity_id = self.entry_data.get("ev_power_limit_entity")
        if not isinstance(entity_id, str) or entity_id != requested.get("entity_id"):
            return None
        capability = power_capability(self._state(entity_id), self.power_options)
        power, value = finite(requested.get("physical_power_kw")), finite(requested.get("value"))
        if capability is None or power is None or value is None or requested.get("unit") != capability.unit:
            return None
        valid = capability.setpoint(power)
        if valid is None or abs(valid-value) > 1e-6 or abs(value*capability.kw_per_unit-power) > 1e-6:
            return None
        return capability, value

    def _power_limit_is_confirmed(self, requested: dict[str, Any]) -> bool:
        validated = self._validated_power_limit(requested)
        if validated is None:
            return False
        capability, value = validated
        return abs(capability.current - value) < 1e-6

    async def _async_set_power_limit(
        self, requested: dict[str, Any], *, valid_until: datetime | None = None,
    ) -> tuple[bool, bool]:
        """Return whether the cap was confirmed and whether a write was sent."""
        validated = self._validated_power_limit(requested)
        if validated is None:
            return False, False
        capability, value = validated
        if abs(capability.current-value) < 1e-6:
            return True, False
        confirmed = await self._async_write_number(
            capability.entity_id, value, valid_until=valid_until, context=self.power_limit_context,
        )
        return confirmed is True, confirmed is not None

    async def _async_schedule(self, action: PlanAction) -> EVCommandResult:
        if "charging_required_now" not in action.desired_state:
            return await self._async_legacy_schedule(action)
        if bool(action.desired_state.get("charging_required_now")):
            return await self._async_start(action)
        return await self._async_stop(require_stop_feedback=action.desired_state.get("restore_power_limit") is not None)

    async def _async_legacy_schedule(self, action: PlanAction) -> EVCommandResult:
        """Honor saved helper-based entries while users migrate to native controls."""
        target_soc = action.desired_state.get("target_soc_percent")
        ready_by = action.desired_state.get("ready_by")
        target_entity_value = self.entry_data.get(CONF_EV_SMART_CHARGING_TARGET_SOC)
        target_entity = target_entity_value if isinstance(target_entity_value, str) and target_entity_value else None
        ready_by_entity_value = self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY)
        ready_by_entity = (
            ready_by_entity_value if isinstance(ready_by_entity_value, str) and ready_by_entity_value else None
        )
        if target_soc is not None:
            if not target_entity:
                return EVCommandResult(False, "ev_target_soc_helper_not_configured", self._snapshot(), self._snapshot())
            if not self._entity_value_matches(target_entity, target_soc) and not self._can_set_entity_value(
                target_entity
            ):
                return EVCommandResult(False, "ev_target_soc_helper_unsupported", self._snapshot(), self._snapshot())
        if ready_by is not None:
            if not ready_by_entity:
                return EVCommandResult(False, "ev_ready_by_helper_not_configured", self._snapshot(), self._snapshot())
            if not self._entity_value_matches(ready_by_entity, ready_by) and not self._can_set_entity_value(
                ready_by_entity
            ):
                return EVCommandResult(False, "ev_ready_by_helper_unsupported", self._snapshot(), self._snapshot())
        if target_soc is not None:
            assert target_entity is not None
            if not await self._async_set_entity_value(target_entity, target_soc):
                return EVCommandResult(
                    False,
                    "ev_target_soc_helper_unsupported",
                    self._snapshot(),
                    self._snapshot(),
                )
        if ready_by is not None:
            assert ready_by_entity is not None
            if not await self._async_set_entity_value(ready_by_entity, ready_by):
                return EVCommandResult(
                    False,
                    "ev_ready_by_helper_unsupported",
                    self._snapshot(),
                    self._snapshot(),
                )
        return await self._async_start(action)

    def _start_entity(self) -> str | None:
        return self._keep_on_entity()

    def _start_command_requires_safe_stop(self, entity_id: str) -> bool:
        """Old command endpoints are recovery evidence, never replay targets."""
        return entity_id != self._keep_on_entity() or entity_id.split(".", 1)[0] not in {"switch", "input_boolean"}

    def _keep_on_entity(self) -> str | None:
        """Return the single stateful charger control."""
        return self.entry_data.get(CONF_EV_CHARGER) or self.entry_data.get(CONF_EV_SMART_CHARGING)

    def _stop_entity(self) -> str | None:
        return self._keep_on_entity()

    def _configured_entity(self, key: str) -> str | None:
        aliases = {
            CONF_EV_SMART_CHARGING: CONF_EV_CHARGER,
        }
        return self.entry_data.get(key) or self.entry_data.get(aliases.get(key, ""))

    async def _async_call_control(
        self,
        entity_id: str,
        *,
        turn_on: bool,
        force: bool = False,
    ) -> EVCommandResult:
        if not self.command_guard():
            return EVCommandResult(False, "ev_vehicle_session_changed", self._snapshot(), self._snapshot())
        if not service_entity_available(self.hass, entity_id):
            return EVCommandResult(False, "ev_control_unavailable", self._snapshot(), self._snapshot())
        domain = entity_id.split(".", 1)[0]
        state = self._state(entity_id)
        if state is None:
            return EVCommandResult(False, "ev_control_unavailable", self._snapshot(), self._snapshot())
        if domain in {"switch", "input_boolean"}:
            service = SERVICE_TURN_ON if turn_on else SERVICE_TURN_OFF
            if not force and ((turn_on and _truthy_state(state)) or (not turn_on and not _truthy_state(state))):
                return EVCommandResult(True, "already_in_desired_state", self._snapshot(), self._snapshot())
            try:
                await async_call_device_service(self.hass, domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)
            except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                return EVCommandResult(
                    False,
                    "ev_control_service_failed",
                    self._snapshot(),
                    self._snapshot(),
                    command_sent=True,
                )
            return EVCommandResult(True, f"{domain}_{service}_called", self._snapshot(), self._snapshot())
        return EVCommandResult(False, "ev_control_domain_unsupported", self._snapshot(), self._snapshot())

    async def _async_set_entity_value(self, entity_id: str, value: Any) -> bool:
        if not self.command_guard():
            return False
        domain = entity_id.split(".", 1)[0]
        if self._entity_value_matches(entity_id, value):
            return True
        with suppress(Exception):
            if domain in {"number", "input_number"}:
                await async_call_device_service(
                    self.hass, domain, "set_value", {ATTR_ENTITY_ID: entity_id, "value": value}, blocking=True
                )
                return True
            if domain == "input_datetime":
                await async_call_device_service(
                    self.hass, domain, "set_datetime", {ATTR_ENTITY_ID: entity_id, "time": str(value)}, blocking=True
                )
                return True
            if domain == "input_text":
                await async_call_device_service(
                    self.hass, domain, "set_value", {ATTR_ENTITY_ID: entity_id, "value": str(value)}, blocking=True
                )
                return True
            if domain == "time":
                await async_call_device_service(
                    self.hass, domain, "set_value", {ATTR_ENTITY_ID: entity_id, "value": str(value)}, blocking=True
                )
                return True
            if domain in {"select", "input_select"}:
                option = self._select_option_for_value(entity_id, value)
                if option is None:
                    return False
                await async_call_device_service(
                    self.hass, domain, "select_option", {ATTR_ENTITY_ID: entity_id, "option": option}, blocking=True
                )
                return True
        return False

    def _entity_value_matches(self, entity_id: str, value: Any) -> bool:
        state = self._state(entity_id)
        if state is None:
            return False
        domain = entity_id.split(".", 1)[0]
        if domain in {"number", "input_number"}:
            return _float_equal(state.state, value)
        if domain in {"input_datetime", "time"}:
            return _time_value_matches(state.state, value)
        if domain == "input_text":
            return str(state.state) == str(value)
        if domain == "sensor":
            return _float_equal(state.state, value)
        if domain in {"select", "input_select"}:
            return (
                str(state.state) == str(value)
                or _float_equal(state.state, value)
                or _time_value_matches(state.state, value)
            )
        return False

    def _can_set_entity_value(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        domain = entity_id.split(".", 1)[0]
        service = None
        if domain in {"number", "input_number", "input_text", "time"}:
            service = "set_value"
        elif domain == "input_datetime":
            service = "set_datetime"
        elif domain in {"select", "input_select"}:
            service = "select_option"
        if service is None:
            return False
        has_service = getattr(self.hass.services, "has_service", None)
        return not callable(has_service) or has_service(domain, service)

    def _select_option_for_value(self, entity_id: str, value: Any) -> str | None:
        """Return the option matching a target SOC or ready-by value."""
        state = self._state(entity_id)
        if state is None:
            return None
        attributes = getattr(state, "attributes", {}) or {}
        options = attributes.get("options")
        candidates = [str(option) for option in options] if isinstance(options, list) else []
        if str(state.state) not in candidates:
            candidates.append(str(state.state))
        for option in candidates:
            if str(option) == str(value) or _float_equal(option, value) or _time_value_matches(option, value):
                return option
        return None

    def _snapshot(self) -> dict[str, Any]:
        entity_ids = {
            key: entity_id
            for key, entity_id in self.entry_data.items()
            if key
            in {
                "ev_power_limit_entity",
                CONF_EV_CHARGING,
                CONF_EV_CONNECTED,
                CONF_EV_CHARGER,
                CONF_EV_SMART_CHARGING,
                CONF_EV_SMART_CHARGING_TARGET_SOC,
                CONF_EV_SMART_CHARGING_READY_BY,
            }
            and entity_id
        }
        snapshot: dict[str, Any] = {key: self._state_value(entity_id) for key, entity_id in entity_ids.items()}
        limit_id = self.entry_data.get("ev_power_limit_entity")
        limit_state = self._state(limit_id) if limit_id else None
        if limit_state is not None:
            snapshot["ev_power_limit_unit"] = limit_state.attributes.get("unit_of_measurement")
            snapshot["ev_power_ownership_version"] = 1
        return snapshot

    def _state(self, entity_id: str | None) -> State | None:
        return available_state(self.hass, entity_id)

    def _vehicle_refusal_snapshot(self) -> dict[str, Any]:
        """Capture refusal before a compensating stop can replace its feedback."""
        feedback = getattr(self._state(self.entry_data.get(CONF_EV_CHARGING)), "state", None)
        if ev_vehicle_suspended(feedback):
            return {"vehicle_refusal_feedback": feedback, "vehicle_refusal_at": dt_util.utcnow().isoformat()}
        return {}

    def _vehicle_suspended(self) -> bool:
        return ev_vehicle_suspended(getattr(self._state(self.entry_data.get(CONF_EV_CHARGING)), "state", None))

    def _state_value(self, entity_id: str) -> str | None:
        state = self._state(entity_id)
        return None if state is None else state.state


def _number_value_in_bounds(state: State, value: float) -> bool:
    minimum, maximum = finite(state.attributes.get("min")), finite(state.attributes.get("max"))
    return minimum is not None and maximum is not None and minimum <= value <= maximum


def _truthy_state(state: State) -> bool:
    """Return whether a Home Assistant state means enabled/active/connected."""
    return str(state.state).lower() in {
        "on",
        "true",
        "connected",
        "charging",
        "home",
        "yes",
        "1",
        "plugged_in",
        "connected_not_charging",
        "fully_charged",
    }


def _charging_state_matches(state: State, enabled: bool) -> bool | None:
    """Return whether a charging-feedback state confirms the requested state."""
    charging = ev_charging_state(state.state) if enabled else ev_charging_state_for_stop(state.state)
    return None if charging is None else charging is enabled


def _control_state_matches(state: State, enabled: bool) -> bool:
    """Return whether a writable control confirms enabled or disabled state."""
    return _truthy_state(state) is enabled


def _safe_state_confirmed(result: EVCommandResult) -> bool:
    """Return whether a stop or rollback has established a safe state."""
    return result.safe_state_confirmed is True or result.rollback_succeeded is True


def _unconfirmed_stop_reason(result: EVCommandResult) -> str:
    """Return an explicit reason when a stop command was not proven safe."""
    return "ev_stop_not_confirmed" if result.applied else result.reason


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(str(left).strip().removesuffix("%")) - float(str(right).strip().removesuffix("%"))) < 0.05
    except (TypeError, ValueError):
        return False


def _time_value_matches(left: Any, right: Any) -> bool:
    left_parts = _time_parts(left)
    right_parts = _time_parts(right)
    return left_parts is not None and left_parts == right_parts


def _time_parts(value: Any) -> tuple[int, int] | None:
    text = str(value).strip()
    if "T" in text:
        text = text.rsplit("T", 1)[-1]
    text = text.split("+", 1)[0].split("-", 1)[0]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None
