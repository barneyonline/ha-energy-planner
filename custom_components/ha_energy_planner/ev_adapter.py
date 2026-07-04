"""EV Smart Charging execution adapter."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, State

from .const import (
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
    """Result of an EV Smart Charging adapter action."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]


class EVSmartChargingAdapter:
    """Execute EV actions through configured EV Smart Charging controls only."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data

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
        return EVCommandResult(result.applied, result.reason, pre_state, post_state)

    async def async_restore(self, saved_state: dict[str, Any] | None = None) -> EVCommandResult:
        """Restore EV Smart Charging to saved state, or stop as a safe fallback."""
        pre_state = self._snapshot()
        if saved_state:
            applied = False
            for key, state in saved_state.items():
                entity_id = self.entry_data.get(key)
                if entity_id and state in {"on", "off"} and entity_id.split(".", 1)[0] in {"switch", "input_boolean"}:
                    await self._async_call_control(entity_id, turn_on=state == "on")
                    applied = True
            return EVCommandResult(
                applied,
                "ev_saved_state_restored" if applied else "ev_saved_state_not_restorable",
                pre_state,
                self._snapshot(),
            )
        result = await self._async_stop()
        return EVCommandResult(result.applied, result.reason, pre_state, self._snapshot())

    async def _async_start(self, action: PlanAction) -> EVCommandResult:
        connected = self._state(self.entry_data.get(CONF_EV_CONNECTED))
        if connected is not None and not _truthy_state(connected):
            return EVCommandResult(False, "ev_not_connected", self._snapshot(), self._snapshot())
        start_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_START) or self.entry_data.get(CONF_EV_SMART_CHARGING)
        if not start_entity:
            return EVCommandResult(False, "ev_start_control_not_configured", self._snapshot(), self._snapshot())
        return await self._async_call_control(start_entity, turn_on=True)

    async def _async_stop(self) -> EVCommandResult:
        stop_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_STOP)
        if stop_entity:
            return await self._async_call_control(stop_entity, turn_on=False, press_button=True)
        stop_entity = self.entry_data.get(CONF_EV_SMART_CHARGING)
        if not stop_entity:
            return EVCommandResult(False, "ev_stop_control_not_configured", self._snapshot(), self._snapshot())
        return await self._async_call_control(stop_entity, turn_on=False, press_button=False)

    async def _async_schedule(self, action: PlanAction) -> EVCommandResult:
        target_soc = action.desired_state.get("target_soc_percent")
        ready_by = action.desired_state.get("ready_by")
        target_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_TARGET_SOC)
        ready_by_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY)

        if target_soc is not None:
            if not target_entity:
                return EVCommandResult(False, "ev_target_soc_helper_not_configured", self._snapshot(), self._snapshot())
            if not self._can_set_entity_value(target_entity):
                return EVCommandResult(False, "ev_target_soc_helper_unsupported", self._snapshot(), self._snapshot())
        if ready_by is not None:
            if not ready_by_entity:
                return EVCommandResult(False, "ev_ready_by_helper_not_configured", self._snapshot(), self._snapshot())
            if not self._can_set_entity_value(ready_by_entity):
                return EVCommandResult(False, "ev_ready_by_helper_unsupported", self._snapshot(), self._snapshot())
        if target_soc is not None and not await self._async_set_entity_value(target_entity, target_soc):
            return EVCommandResult(False, "ev_target_soc_helper_unsupported", self._snapshot(), self._snapshot())
        if ready_by is not None and not await self._async_set_entity_value(ready_by_entity, ready_by):
            return EVCommandResult(False, "ev_ready_by_helper_unsupported", self._snapshot(), self._snapshot())
        return await self._async_start(action)

    async def _async_call_control(self, entity_id: str, *, turn_on: bool, press_button: bool = True) -> EVCommandResult:
        raw_state = self.hass.states.get(entity_id)
        if raw_state is None:
            return EVCommandResult(False, "ev_control_unavailable", self._snapshot(), self._snapshot())
        domain = entity_id.split(".", 1)[0]
        if domain in {"button", "input_button"} and (turn_on or press_button):
            try:
                await self.hass.services.async_call(domain, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True)
            except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                return EVCommandResult(False, "ev_control_service_failed", self._snapshot(), self._snapshot())
            return EVCommandResult(True, f"{domain}_press_called", self._snapshot(), self._snapshot())

        state = self._state(entity_id)
        if state is None:
            return EVCommandResult(False, "ev_control_unavailable", self._snapshot(), self._snapshot())
        if domain in {"switch", "input_boolean"}:
            service = SERVICE_TURN_ON if turn_on else SERVICE_TURN_OFF
            if (turn_on and _truthy_state(state)) or (not turn_on and not _truthy_state(state)):
                return EVCommandResult(True, "already_in_desired_state", self._snapshot(), self._snapshot())
            try:
                await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)
            except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                return EVCommandResult(False, "ev_control_service_failed", self._snapshot(), self._snapshot())
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
        if service is None:
            return False
        has_service = getattr(self.hass.services, "has_service", None)
        return not callable(has_service) or has_service(domain, service)

    def _snapshot(self) -> dict[str, Any]:
        entity_ids = {
            key: entity_id
            for key, entity_id in self.entry_data.items()
            if key
            in {
                CONF_EV_CHARGING,
                CONF_EV_CONNECTED,
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


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 0.05
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
