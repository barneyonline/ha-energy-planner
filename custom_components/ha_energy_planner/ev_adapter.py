"""Direct EV charger execution adapter."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, State

from .const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    STATE_UNKNOWN_VALUES,
)
from .models import ActionKind, PlanAction


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


_CONTROL_KEYS = {
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
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
    ) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data
        self.confirmation_timeout_seconds = max(float(confirmation_timeout_seconds), 0.0)
        self.confirmation_retries = max(int(confirmation_retries), 0)
        self.confirmation_poll_seconds = max(float(confirmation_poll_seconds), 0.01)
        self.connected_override = connected_override

    async def async_execute(self, action: PlanAction) -> EVCommandResult:
        """Execute a supported EV action through Home Assistant services."""
        pre_state = self._snapshot()
        if action.kind == ActionKind.EV_START:
            result = await self._async_start(action)
        elif action.kind == ActionKind.EV_STOP:
            result = await self._async_stop()
        elif action.kind == ActionKind.EV_SCHEDULE:
            result = await self._async_schedule(action)
        else:
            return EVCommandResult(False, "unsupported_ev_action", pre_state, self._snapshot())

        post_state = self._snapshot()
        return EVCommandResult(
            result.applied,
            result.reason,
            pre_state,
            post_state,
            command_sent=result.command_sent,
            rollback_succeeded=result.rollback_succeeded,
            safe_state_confirmed=result.safe_state_confirmed,
        )

    async def async_restore(
        self,
        saved_state: dict[str, Any] | None = None,
        *,
        command_entity_id: str | None = None,
    ) -> EVCommandResult:
        """Restore charger controls to saved state, or stop as a safe fallback."""
        pre_state = self._snapshot()
        if saved_state:
            restore_entity_id = command_entity_id or self._start_entity()
            if restore_entity_id and self._start_command_requires_safe_stop(
                restore_entity_id
            ):
                safe_stop = await self._async_stop()
                safely_stopped = _safe_state_confirmed(safe_stop)
                return EVCommandResult(
                    safely_stopped,
                    (
                        "ev_saved_state_safe_stop"
                        if safely_stopped
                        else _unconfirmed_stop_reason(safe_stop)
                    ),
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
                    (
                        "ev_saved_state_safe_stop"
                        if safely_stopped
                        else _unconfirmed_stop_reason(safe_stop)
                    ),
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
        connected_entity = self.entry_data.get(CONF_EV_CONNECTED)
        if connected_entity:
            connected = self._state(connected_entity)
            if connected is None:
                return EVCommandResult(
                    False,
                    "ev_connected_state_unavailable",
                    self._snapshot(),
                    self._snapshot(),
                )
            if not _truthy_state(connected):
                return EVCommandResult(False, "ev_not_connected", self._snapshot(), self._snapshot())
        elif self.connected_override is False:
            return EVCommandResult(False, "ev_not_connected", self._snapshot(), self._snapshot())
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
                press_button=False,
                confirmation_entity=keep_on_entity,
                confirmation_reason="ev_charger_enabled_for_preconditioning",
            )
        start_entity = self._start_entity()
        if not start_entity:
            return EVCommandResult(False, "ev_start_control_not_configured", self._snapshot(), self._snapshot())
        return await self._async_control_with_confirmation(start_entity, enabled=True)

    async def _async_stop(self) -> EVCommandResult:
        stop_entity = self._stop_entity(separate_only=True)
        if stop_entity:
            result = await self._async_control_with_confirmation(
                stop_entity,
                enabled=False,
                press_button=True,
                force=True,
                # A separate stop entity is a command endpoint, not an
                # authoritative persistent charger-state control. Its neutral
                # state cannot prove that charging will remain disabled.
                safe_control_entity=None,
            )
            return await self._async_finalize_stop(result, stop_entity)
        stop_entity = self._stop_entity()
        if not stop_entity:
            return EVCommandResult(False, "ev_stop_control_not_configured", self._snapshot(), self._snapshot())
        result = await self._async_control_with_confirmation(
            stop_entity,
            enabled=False,
            press_button=False,
            safe_control_entity=stop_entity,
        )
        return await self._async_finalize_stop(result, stop_entity)

    async def _async_control_with_confirmation(
        self,
        entity_id: str,
        *,
        enabled: bool,
        press_button: bool = True,
        confirmation_entity: str | None = None,
        confirmation_reason: str | None = None,
        force: bool = False,
        safe_control_entity: str | None = None,
    ) -> EVCommandResult:
        """Issue a command and confirm the mapped charging feedback state."""
        charging_entity = confirmation_entity or self.entry_data.get(CONF_EV_CHARGING)
        initial_pre_state = self._snapshot()
        command_sent = False
        for _attempt in range(self.confirmation_retries + 1):
            result = await self._async_call_control(
                entity_id,
                turn_on=enabled,
                press_button=press_button,
                force=force,
            )
            command_sent = command_sent or result.command_sent or (
                result.applied and result.reason != "already_in_desired_state"
            )
            if not result.applied and result.reason != "already_in_desired_state":
                if command_sent:
                    return await self._async_confirmation_failure(
                        reason=result.reason,
                        initial_pre_state=initial_pre_state,
                        command_sent=True,
                        requested_enabled=enabled,
                        command_entity_id=entity_id,
                    )
                return result
            if not charging_entity:
                safe_state_confirmed = None
                if not enabled:
                    safe_state_confirmed = await self._async_control_proves_safe(
                        safe_control_entity
                    )
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
            )
            if confirmation == "confirmed":
                safe_state_confirmed = None
                if not enabled:
                    if safe_control_entity:
                        safe_state_confirmed = await self._async_control_proves_safe(
                            safe_control_entity
                        )
                    else:
                        safe_state_confirmed = self._charging_feedback_proves_safe(
                            charging_entity
                        )
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
            if confirmation == "unavailable":
                return await self._async_confirmation_failure(
                    reason="ev_charging_confirmation_unavailable",
                    initial_pre_state=initial_pre_state,
                    command_sent=command_sent,
                    requested_enabled=enabled,
                    command_entity_id=entity_id,
                )
        return await self._async_confirmation_failure(
            reason="ev_charging_confirmation_timeout",
            initial_pre_state=initial_pre_state,
            command_sent=command_sent,
            requested_enabled=enabled,
            command_entity_id=entity_id,
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

    def _charging_feedback_proves_safe(self, entity_id: str) -> bool:
        """Return whether charging feedback proves more than disconnection."""
        state = self._state(entity_id)
        if state is None:
            return False
        return str(state.state).strip().lower() in {
            "off",
            "false",
            "0",
            "idle",
            "not_charging",
            "connected_not_charging",
            "fully_charged",
        }

    async def _async_confirm_state(self, entity_id: str, enabled: bool, *, control_state: bool = False) -> str:
        """Wait for charging feedback or a stateful control to match the request."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.confirmation_timeout_seconds
        while True:
            state = self._state(entity_id)
            if state is None:
                return "unavailable"
            matches = (
                _control_state_matches(state, enabled)
                if control_state
                else _charging_state_matches(state, enabled)
            )
            if matches is True:
                return "confirmed"
            if loop.time() >= deadline:
                return "timeout"
            await asyncio.sleep(min(self.confirmation_poll_seconds, max(deadline - loop.time(), 0.0)))

    async def _async_confirmation_failure(
        self,
        *,
        reason: str,
        initial_pre_state: dict[str, Any],
        command_sent: bool,
        requested_enabled: bool,
        command_entity_id: str,
    ) -> EVCommandResult:
        """Compensate a command whose requested device state was not confirmed."""
        if not command_sent:
            return EVCommandResult(False, reason, initial_pre_state, self._snapshot())
        attempted = False
        restored = False
        command_requires_safe_stop = self._start_command_requires_safe_stop(
            command_entity_id
        )
        if requested_enabled and not command_requires_safe_stop:
            attempted, restored = await self._async_restore_control_snapshot(
                initial_pre_state,
                entity_ids={command_entity_id},
            )
        if not attempted:
            attempted, restored = await self._async_issue_safe_stop()
        return EVCommandResult(
            False,
            reason,
            initial_pre_state,
            self._snapshot(),
            command_sent=True,
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
                press_button=False,
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

    async def _async_issue_safe_stop(self) -> tuple[bool, bool]:
        """Issue one unconfirmed stop command when a momentary start cannot be rolled back."""
        stop_entity = self._stop_entity(separate_only=True)
        separate_stop = bool(stop_entity)
        press_button = separate_stop
        if not stop_entity:
            stop_entity = self._stop_entity()
            press_button = False
        if not stop_entity:
            return False, False
        result = await self._async_call_control(
            stop_entity,
            turn_on=False,
            press_button=press_button,
            force=separate_stop or not press_button,
        )
        if not result.applied:
            return True, False
        confirmation_entity = self.entry_data.get(CONF_EV_CHARGING) if press_button else stop_entity
        if not confirmation_entity:
            stopped = await self._async_control_proves_safe(
                None if press_button else stop_entity
            )
        else:
            confirmed = (
                await self._async_confirm_state(
                    confirmation_entity,
                    False,
                    control_state=not press_button,
                )
                == "confirmed"
            )
            stopped = confirmed and (
                not press_button
                or self._charging_feedback_proves_safe(confirmation_entity)
            )
        if not stopped:
            return True, False
        reset_attempted, reset_succeeded = await self._async_reset_start_command(
            stopped_entity=stop_entity
        )
        return True, not reset_attempted or reset_succeeded

    async def _async_schedule(self, action: PlanAction) -> EVCommandResult:
        if "charging_required_now" not in action.desired_state:
            return await self._async_legacy_schedule(action)
        if bool(action.desired_state.get("charging_required_now")):
            return await self._async_start(action)
        return await self._async_stop()

    async def _async_legacy_schedule(self, action: PlanAction) -> EVCommandResult:
        """Honor saved helper-based entries while users migrate to native controls."""
        target_soc = action.desired_state.get("target_soc_percent")
        ready_by = action.desired_state.get("ready_by")
        target_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_TARGET_SOC)
        ready_by_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY)
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
        if target_soc is not None and not await self._async_set_entity_value(target_entity, target_soc):
            return EVCommandResult(False, "ev_target_soc_helper_unsupported", self._snapshot(), self._snapshot())
        if ready_by is not None and not await self._async_set_entity_value(ready_by_entity, ready_by):
            return EVCommandResult(False, "ev_ready_by_helper_unsupported", self._snapshot(), self._snapshot())
        return await self._async_start(action)

    def _start_entity(self) -> str | None:
        return (
            self.entry_data.get(CONF_EV_CHARGER_START)
            or self.entry_data.get(CONF_EV_CHARGER)
            or self.entry_data.get(CONF_EV_SMART_CHARGING_START)
            or self.entry_data.get(CONF_EV_SMART_CHARGING)
        )

    def _start_command_requires_safe_stop(self, entity_id: str) -> bool:
        """Return whether a start control cannot prove a restored safe state."""
        if entity_id.split(".", 1)[0] in {"button", "input_button"}:
            return True
        return entity_id in {
            self.entry_data.get(CONF_EV_CHARGER_START),
            self.entry_data.get(CONF_EV_SMART_CHARGING_START),
        }

    async def _async_finalize_stop(
        self,
        result: EVCommandResult,
        stop_entity: str,
    ) -> EVCommandResult:
        """Neutralize a separate start command after charging is stopped."""
        if not result.applied:
            return result
        reset_attempted, reset_succeeded = await self._async_reset_start_command(
            stopped_entity=stop_entity
        )
        if not reset_attempted:
            return result
        return EVCommandResult(
            reset_succeeded,
            (
                "ev_charging_stopped_and_start_reset"
                if reset_succeeded
                else "ev_start_command_reset_failed"
            ),
            result.pre_state,
            self._snapshot(),
            command_sent=True,
            rollback_succeeded=(
                result.rollback_succeeded if reset_succeeded else False
            ),
            safe_state_confirmed=(
                result.safe_state_confirmed is True and reset_succeeded
            ),
        )

    async def _async_reset_start_command(
        self,
        *,
        stopped_entity: str,
    ) -> tuple[bool, bool]:
        """Reset a switch-based separate start command to its neutral state."""
        start_entity = self.entry_data.get(CONF_EV_CHARGER_START) or self.entry_data.get(
            CONF_EV_SMART_CHARGING_START
        )
        if (
            not start_entity
            or start_entity == stopped_entity
            or start_entity.split(".", 1)[0] not in {"switch", "input_boolean"}
        ):
            return False, True
        start_state = self._state(start_entity)
        if start_state is not None and not _truthy_state(start_state):
            return False, True
        result = await self._async_call_control(
            start_entity,
            turn_on=False,
            press_button=False,
            force=True,
        )
        if not result.applied:
            return True, False
        confirmed = await self._async_confirm_state(
            start_entity,
            False,
            control_state=True,
        )
        return True, confirmed == "confirmed"

    def _keep_on_entity(self) -> str | None:
        """Return the persistent charger-enable control used for preconditioning."""
        return self.entry_data.get(CONF_EV_CHARGER) or self.entry_data.get(
            CONF_EV_SMART_CHARGING
        )

    def _stop_entity(self, *, separate_only: bool = False) -> str | None:
        separate = self.entry_data.get(CONF_EV_CHARGER_STOP) or self.entry_data.get(CONF_EV_SMART_CHARGING_STOP)
        if separate or separate_only:
            return separate
        return self.entry_data.get(CONF_EV_CHARGER) or self.entry_data.get(CONF_EV_SMART_CHARGING)

    def _configured_entity(self, key: str) -> str | None:
        aliases = {
            CONF_EV_SMART_CHARGING: CONF_EV_CHARGER,
            CONF_EV_SMART_CHARGING_START: CONF_EV_CHARGER_START,
            CONF_EV_SMART_CHARGING_STOP: CONF_EV_CHARGER_STOP,
        }
        return self.entry_data.get(key) or self.entry_data.get(aliases.get(key, ""))

    async def _async_call_control(
        self,
        entity_id: str,
        *,
        turn_on: bool,
        press_button: bool = True,
        force: bool = False,
    ) -> EVCommandResult:
        raw_state = self.hass.states.get(entity_id)
        if raw_state is None:
            return EVCommandResult(False, "ev_control_unavailable", self._snapshot(), self._snapshot())
        domain = entity_id.split(".", 1)[0]
        if domain in {"button", "input_button"} and (turn_on or press_button):
            try:
                await self.hass.services.async_call(domain, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True)
            except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                return EVCommandResult(
                    False,
                    "ev_control_service_failed",
                    self._snapshot(),
                    self._snapshot(),
                    command_sent=True,
                )
            return EVCommandResult(True, f"{domain}_press_called", self._snapshot(), self._snapshot())

        state = self._state(entity_id)
        if state is None:
            return EVCommandResult(False, "ev_control_unavailable", self._snapshot(), self._snapshot())
        if domain in {"switch", "input_boolean"}:
            service = SERVICE_TURN_ON if turn_on else SERVICE_TURN_OFF
            if not force and ((turn_on and _truthy_state(state)) or (not turn_on and not _truthy_state(state))):
                return EVCommandResult(True, "already_in_desired_state", self._snapshot(), self._snapshot())
            try:
                await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)
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
        domain = entity_id.split(".", 1)[0]
        if self._entity_value_matches(entity_id, value):
            return True
        with suppress(Exception):
            if domain in {"number", "input_number"}:
                await self.hass.services.async_call(
                    domain, "set_value", {ATTR_ENTITY_ID: entity_id, "value": value}, blocking=True
                )
                return True
            if domain == "input_datetime":
                await self.hass.services.async_call(
                    domain, "set_datetime", {ATTR_ENTITY_ID: entity_id, "time": str(value)}, blocking=True
                )
                return True
            if domain == "input_text":
                await self.hass.services.async_call(
                    domain, "set_value", {ATTR_ENTITY_ID: entity_id, "value": str(value)}, blocking=True
                )
                return True
            if domain == "time":
                await self.hass.services.async_call(
                    domain, "set_value", {ATTR_ENTITY_ID: entity_id, "value": str(value)}, blocking=True
                )
                return True
            if domain in {"select", "input_select"}:
                option = self._select_option_for_value(entity_id, value)
                if option is None:
                    return False
                await self.hass.services.async_call(
                    domain, "select_option", {ATTR_ENTITY_ID: entity_id, "option": option}, blocking=True
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
                CONF_EV_CHARGING,
                CONF_EV_CONNECTED,
                CONF_EV_CHARGER,
                CONF_EV_CHARGER_START,
                CONF_EV_CHARGER_STOP,
                CONF_EV_SMART_CHARGING,
                CONF_EV_SMART_CHARGING_START,
                CONF_EV_SMART_CHARGING_STOP,
                CONF_EV_SMART_CHARGING_TARGET_SOC,
                CONF_EV_SMART_CHARGING_READY_BY,
            }
            and entity_id
        }
        return {key: self._state_value(entity_id) for key, entity_id in entity_ids.items()}

    def _state(self, entity_id: str | None) -> State | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in STATE_UNKNOWN_VALUES:
            return None
        return state

    def _state_value(self, entity_id: str) -> str | None:
        state = self._state(entity_id)
        return None if state is None else state.state


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
    value = str(state.state).strip().lower()
    if value in {"on", "true", "1", "charging"}:
        return enabled
    if value in {
        "off",
        "false",
        "0",
        "idle",
        "not_charging",
        "connected_not_charging",
        "fully_charged",
        "disconnected",
        "unplugged",
        "not_plugged_in",
    }:
        return not enabled
    return None


def _control_state_matches(state: State, enabled: bool) -> bool:
    """Return whether a writable control confirms enabled or disabled state."""
    return _truthy_state(state) is enabled


def _safe_state_confirmed(result: EVCommandResult) -> bool:
    """Return whether a stop or rollback has established a safe state."""
    return result.safe_state_confirmed is True or result.rollback_succeeded is True


def _unconfirmed_stop_reason(result: EVCommandResult) -> str:
    """Return an explicit reason when a stop command was not proven safe."""
    return "ev_stop_not_confirmed" if result.applied else result.reason


# Kept as an import alias for custom code and one-release test compatibility.
EVSmartChargingAdapter = EVChargerAdapter


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
