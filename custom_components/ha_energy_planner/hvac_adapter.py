"""Daikin HVAC execution adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, State

from .const import CONF_CLIMATE_AUTOMATIONS, CONF_DAIKIN_CLIMATE, STATE_UNKNOWN_VALUES
from .models import ActionKind, PlanAction


@dataclass(slots=True)
class HVACCommandResult:
    """Result of an HVAC adapter action."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    saved_automation_states: dict[str, str]


class DaikinHVACAdapter:
    """Control Daikin through Home Assistant climate services."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data

    async def async_execute(self, action: PlanAction) -> HVACCommandResult:
        """Execute a supported HVAC action."""
        pre_state = self._snapshot()
        saved_automation_states = self._automation_states()
        if action.kind != ActionKind.SET_HVAC:
            return HVACCommandResult(False, "unsupported_hvac_action", pre_state, self._snapshot(), {})
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        climate_state = self._state(climate_entity)
        if climate_entity is None or climate_state is None:
            return HVACCommandResult(False, "daikin_climate_unavailable", pre_state, self._snapshot(), {})
        if not action.desired_state:
            return HVACCommandResult(False, "hvac_desired_state_empty", pre_state, self._snapshot(), {})
        if action.desired_state.get("suppress_automations"):
            if not any(state == "on" for state in saved_automation_states.values()):
                return HVACCommandResult(True, "already_in_desired_hvac_state", pre_state, self._snapshot(), {})
            if not await self._async_disable_automations(saved_automation_states):
                return HVACCommandResult(False, "hvac_automation_service_failed", pre_state, self._snapshot(), saved_automation_states)
            return HVACCommandResult(True, "hvac_automations_suppressed", pre_state, self._snapshot(), saved_automation_states)
        if _already_in_desired_state(climate_state, action.desired_state):
            return HVACCommandResult(True, "already_in_desired_hvac_state", pre_state, self._snapshot(), {})

        if not await self._async_disable_automations(saved_automation_states):
            return HVACCommandResult(False, "hvac_automation_service_failed", pre_state, self._snapshot(), saved_automation_states)
        desired_mode = action.desired_state.get("hvac_mode")
        desired_temperature = action.desired_state.get("target_temperature")
        target_low = action.desired_state.get("target_temp_low")
        target_high = action.desired_state.get("target_temp_high")

        try:
            if desired_mode == "off":
                await self.hass.services.async_call("climate", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: climate_entity}, blocking=True)
            elif desired_mode:
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {ATTR_ENTITY_ID: climate_entity, "hvac_mode": desired_mode},
                    blocking=True,
                )
            if desired_temperature is not None:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {ATTR_ENTITY_ID: climate_entity, "temperature": desired_temperature},
                    blocking=True,
                )
            elif target_low is not None and target_high is not None:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {ATTR_ENTITY_ID: climate_entity, "target_temp_low": target_low, "target_temp_high": target_high},
                    blocking=True,
                )
        except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
            return HVACCommandResult(False, "hvac_control_service_failed", pre_state, self._snapshot(), saved_automation_states)
        return HVACCommandResult(True, "hvac_action_applied", pre_state, self._snapshot(), saved_automation_states)

    async def async_restore(self, saved_automation_states: dict[str, str] | None = None) -> HVACCommandResult:
        """Restore saved climate automation states."""
        pre_state = self._snapshot()
        states = dict(saved_automation_states or {})
        failed = False
        for automation_id, state in states.items():
            try:
                if state == "on":
                    await self.hass.services.async_call("automation", SERVICE_TURN_ON, {ATTR_ENTITY_ID: automation_id}, blocking=True)
                elif state == "off":
                    await self.hass.services.async_call("automation", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: automation_id}, blocking=True)
            except Exception:  # noqa: BLE001 - restore must continue and report failure.
                failed = True
        reason = "no_hvac_automation_state_saved"
        if failed:
            reason = "hvac_automation_restore_failed"
        elif states:
            reason = "hvac_automation_state_restored"
        return HVACCommandResult(
            applied=bool(states) and not failed,
            reason=reason,
            pre_state=pre_state,
            post_state=self._snapshot(),
            saved_automation_states=states,
        )

    async def _async_disable_automations(self, states: dict[str, str]) -> bool:
        for automation_id, state in states.items():
            if state == "on":
                try:
                    await self.hass.services.async_call(
                        "automation",
                        SERVICE_TURN_OFF,
                        {ATTR_ENTITY_ID: automation_id},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                    return False
        return True

    def _automation_states(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for entity_id in self._automation_entities():
            state = self._state(entity_id)
            if state is not None:
                states[entity_id] = state.state
        return states

    def _automation_entities(self) -> list[str]:
        configured = self.entry_data.get(CONF_CLIMATE_AUTOMATIONS, "")
        if isinstance(configured, str):
            return [entity_id.strip() for entity_id in configured.split(",") if entity_id.strip()]
        if isinstance(configured, list):
            return [str(entity_id) for entity_id in configured if str(entity_id).strip()]
        return []

    def _snapshot(self) -> dict[str, Any]:
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        snapshot: dict[str, Any] = {}
        if climate_entity:
            snapshot[CONF_DAIKIN_CLIMATE] = self._state_value(climate_entity)
        for automation_id in self._automation_entities():
            snapshot[automation_id] = self._state_value(automation_id)
        return snapshot

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


def _already_in_desired_state(state: State, desired_state: dict[str, Any]) -> bool:
    desired_mode = desired_state.get("hvac_mode")
    if desired_mode is not None and str(desired_mode) != state.state:
        return False
    attributes = getattr(state, "attributes", {}) or {}
    desired_temperature = desired_state.get("target_temperature")
    if desired_temperature is not None and not _float_equal(attributes.get("temperature"), desired_temperature):
        return False
    target_low = desired_state.get("target_temp_low")
    if target_low is not None and not _float_equal(attributes.get("target_temp_low"), target_low):
        return False
    target_high = desired_state.get("target_temp_high")
    if target_high is not None and not _float_equal(attributes.get("target_temp_high"), target_high):
        return False
    return True


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 0.05
    except (TypeError, ValueError):
        return False
