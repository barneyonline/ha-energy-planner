"""Daikin HVAC execution adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, State

from .const import CONF_CLIMATE_AUTOMATIONS, CONF_CLIMATE_ZONES, CONF_DAIKIN_CLIMATE, STATE_UNKNOWN_VALUES
from .models import ActionKind, PlanAction

_STATE_CONFIRMATION_TIMEOUT_SECONDS = 1.0
_STATE_CONFIRMATION_POLL_SECONDS = 0.05


@dataclass(slots=True)
class HVACCommandResult:
    """Result of an HVAC adapter action."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    saved_automation_states: dict[str, str]
    rollback_succeeded: bool | None = None
    saved_zone_states: dict[str, str] = field(default_factory=dict)


class DaikinHVACAdapter:
    """Control Daikin through Home Assistant climate services."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data

    def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return automation and zone state that must survive a takeover crash."""
        return self._automation_states(), self._zone_states()

    async def async_execute(self, action: PlanAction) -> HVACCommandResult:
        """Execute a supported HVAC action."""
        pre_state = self._snapshot()
        saved_automation_states = self._automation_states()
        captured_zone_states = self._zone_states()
        if action.kind == ActionKind.RELEASE_HVAC:
            return await self.async_restore(saved_automation_states, captured_zone_states)
        saved_zone_states = captured_zone_states if action.desired_state.get("enable_zones") else {}
        if action.kind != ActionKind.SET_HVAC:
            return HVACCommandResult(False, "unsupported_hvac_action", pre_state, self._snapshot(), {}, True)
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        climate_state = self._state(climate_entity)
        if climate_entity is None or climate_state is None:
            return HVACCommandResult(False, "daikin_climate_unavailable", pre_state, self._snapshot(), {}, True)
        if not action.desired_state:
            return HVACCommandResult(False, "hvac_desired_state_empty", pre_state, self._snapshot(), {}, True)
        if set(saved_automation_states) != set(self._automation_entities()):
            return HVACCommandResult(
                False,
                "climate_automation_unavailable",
                pre_state,
                self._snapshot(),
                {},
                True,
                saved_zone_states,
            )
        if action.desired_state.get("enable_zones") and set(saved_zone_states) != set(self._zone_entities()):
            return HVACCommandResult(
                False,
                "climate_zone_unavailable",
                pre_state,
                self._snapshot(),
                saved_automation_states,
                True,
                saved_zone_states,
            )
        if (
            action.desired_state.get("suppress_automations")
            and action.desired_state.get("hvac_mode") is None
            and action.desired_state.get("target_temperature") is None
            and not action.desired_state.get("enable_zones")
        ):
            if not any(state == "on" for state in saved_automation_states.values()):
                return HVACCommandResult(True, "already_in_desired_hvac_state", pre_state, self._snapshot(), {})
            disabled, _changed_states = await self._async_disable_automations(saved_automation_states)
            if not disabled:
                rollback_succeeded, unresolved_states = await self._async_enable_automation_entities(
                    saved_automation_states
                )
                return HVACCommandResult(
                    False,
                    ("hvac_automation_service_failed" if rollback_succeeded else "hvac_automation_rollback_failed"),
                    pre_state,
                    self._snapshot(),
                    unresolved_states,
                    rollback_succeeded,
                )
            return HVACCommandResult(
                True, "hvac_automations_suppressed", pre_state, self._snapshot(), saved_automation_states
            )
        disabled, _changed_states = await self._async_disable_automations(saved_automation_states)
        if not disabled:
            rollback_succeeded, unresolved_states = await self._async_enable_automation_entities(
                saved_automation_states
            )
            return HVACCommandResult(
                False,
                ("hvac_automation_service_failed" if rollback_succeeded else "hvac_automation_rollback_failed"),
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                saved_zone_states,
            )
        zones_enabled, changed_zones = await self._async_enable_zones(saved_zone_states)
        if not zones_enabled:
            rollback_succeeded, unresolved_states, unresolved_zones = await self._async_rollback_takeover(
                saved_automation_states,
                changed_zones,
            )
            return HVACCommandResult(
                False,
                "hvac_zone_service_failed" if rollback_succeeded else "hvac_acquisition_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
            )
        desired_mode = action.desired_state.get("hvac_mode")
        desired_temperature = action.desired_state.get("target_temperature")
        target_low = action.desired_state.get("target_temp_low")
        target_high = action.desired_state.get("target_temp_high")
        mode_matches = _mode_matches(climate_state, desired_mode)
        temperature_matches = _temperature_matches(climate_state, desired_temperature)
        range_matches = _temperature_range_matches(climate_state, target_low, target_high)
        no_climate_change = mode_matches and temperature_matches and range_matches

        try:
            if not mode_matches and desired_mode == "off":
                await self.hass.services.async_call(
                    "climate", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: climate_entity}, blocking=True
                )
            elif desired_mode and desired_mode != "off":
                if climate_state.state == "off":
                    await self.hass.services.async_call(
                        "climate", SERVICE_TURN_ON, {ATTR_ENTITY_ID: climate_entity}, blocking=True
                    )
                if not mode_matches:
                    await self.hass.services.async_call(
                        "climate",
                        "set_hvac_mode",
                        {ATTR_ENTITY_ID: climate_entity, "hvac_mode": desired_mode},
                        blocking=True,
                    )
            if desired_temperature is not None and not temperature_matches:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {ATTR_ENTITY_ID: climate_entity, "temperature": desired_temperature},
                    blocking=True,
                )
            elif target_low is not None and target_high is not None and not range_matches:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {ATTR_ENTITY_ID: climate_entity, "target_temp_low": target_low, "target_temp_high": target_high},
                    blocking=True,
                )
        except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
            rollback_succeeded, unresolved_states, unresolved_zones = await self._async_rollback_takeover(
                saved_automation_states,
                changed_zones,
            )
            return HVACCommandResult(
                False,
                "hvac_control_service_failed" if rollback_succeeded else "hvac_automation_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
            )
        if not await self._async_confirm_hvac_state(climate_entity, action.desired_state):
            rollback_succeeded, unresolved_states, unresolved_zones = await self._async_rollback_takeover(
                saved_automation_states,
                changed_zones,
            )
            return HVACCommandResult(
                False,
                "hvac_state_confirmation_failed" if rollback_succeeded else "hvac_acquisition_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
            )
        changed_automation = any(state == "on" for state in saved_automation_states.values())
        changed_zone = any(state != "on" for state in saved_zone_states.values())
        reason = (
            "already_in_desired_hvac_state"
            if no_climate_change and not changed_automation and not changed_zone
            else "hvac_action_applied"
        )
        return HVACCommandResult(
            True,
            reason,
            pre_state,
            self._snapshot(),
            saved_automation_states,
            saved_zone_states=saved_zone_states,
        )

    async def async_restore(
        self,
        saved_automation_states: dict[str, str] | None = None,
        saved_zone_states: dict[str, str] | None = None,
    ) -> HVACCommandResult:
        """Release HVAC ownership by restoring zones and enabling automations."""
        pre_state = self._snapshot()
        states = dict(saved_automation_states or {})
        zones = dict(saved_zone_states or {})
        zones_restored, unresolved_zones = await self._async_restore_zone_states(zones)
        restored, unresolved_states = await self._async_enable_automation_entities(states)
        reason = "no_hvac_automation_state_saved"
        if not restored or not zones_restored:
            reason = "hvac_release_failed"
        elif states or zones or self._automation_entities():
            reason = "hvac_control_released"
        return HVACCommandResult(
            applied=(bool(states or zones or self._automation_entities())) and restored and zones_restored,
            reason=reason,
            pre_state=pre_state,
            post_state=self._snapshot(),
            saved_automation_states=unresolved_states,
            rollback_succeeded=restored and zones_restored,
            saved_zone_states=unresolved_zones,
        )

    async def _async_enable_automation_entities(
        self,
        saved_states: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, str]]:
        """Enable all configured climate automations."""
        unresolved: dict[str, str] = {}
        entity_ids = list(dict.fromkeys([*self._automation_entities(), *(saved_states or {})]))
        for entity_id in entity_ids:
            state = self._state(entity_id)
            if state is not None and state.state == "on":
                continue
            try:
                await self.hass.services.async_call(
                    "automation", SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
                )
            except Exception:  # noqa: BLE001
                unresolved[entity_id] = "on"
                continue
            if not await self._async_confirm_state(entity_id, "on"):
                unresolved[entity_id] = "on"
        return not unresolved, unresolved

    async def _async_enable_zones(self, states: dict[str, str]) -> tuple[bool, dict[str, str]]:
        """Enable configured zone entities and return those changed."""
        changed: dict[str, str] = {}
        for entity_id, state in states.items():
            if state == "on":
                continue
            changed[entity_id] = state
            try:
                await self.hass.services.async_call(
                    entity_id.split(".", 1)[0], SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
                )
            except Exception:  # noqa: BLE001
                return False, changed
            if not await self._async_confirm_state(entity_id, "on"):
                return False, changed
        return True, changed

    async def _async_restore_zone_states(self, states: dict[str, str]) -> tuple[bool, dict[str, str]]:
        """Restore captured zone states."""
        unresolved: dict[str, str] = {}
        for entity_id, state in states.items():
            observed = self._state(entity_id)
            if observed is not None and observed.state == state:
                continue
            service = SERVICE_TURN_ON if state == "on" else SERVICE_TURN_OFF
            try:
                await self.hass.services.async_call(
                    entity_id.split(".", 1)[0], service, {ATTR_ENTITY_ID: entity_id}, blocking=True
                )
            except Exception:  # noqa: BLE001
                unresolved[entity_id] = state
                continue
            if not await self._async_confirm_state(entity_id, state):
                unresolved[entity_id] = state
        return not unresolved, unresolved

    async def _async_rollback_takeover(
        self,
        saved_automation_states: dict[str, str],
        changed_zones: dict[str, str],
    ) -> tuple[bool, dict[str, str], dict[str, str]]:
        """Release every actuator acquired before a climate command failed."""
        zones_restored, unresolved_zones = await self._async_restore_zone_states(changed_zones)
        automations_restored, unresolved_states = await self._async_enable_automation_entities(
            saved_automation_states
        )
        return zones_restored and automations_restored, unresolved_states, unresolved_zones

    async def _async_confirm_state(self, entity_id: str, expected_state: str) -> bool:
        """Wait briefly for Home Assistant to publish a requested actuator state."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _STATE_CONFIRMATION_TIMEOUT_SECONDS
        while True:
            observed = self._state(entity_id)
            if observed is not None and observed.state == expected_state:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_STATE_CONFIRMATION_POLL_SECONDS, remaining))

    async def _async_confirm_hvac_state(self, entity_id: str, desired_state: dict[str, Any]) -> bool:
        """Wait briefly for Home Assistant to publish the requested thermostat state."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _STATE_CONFIRMATION_TIMEOUT_SECONDS
        while True:
            observed = self._state(entity_id)
            if observed is not None and _already_in_desired_state(observed, desired_state):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_STATE_CONFIRMATION_POLL_SECONDS, remaining))

    async def _async_disable_automations(self, states: dict[str, str]) -> tuple[bool, dict[str, str]]:
        """Disable enabled automations and return the states actually changed."""
        changed_states: dict[str, str] = {}
        for automation_id, state in states.items():
            if state == "on":
                # Treat the boundary as uncertain: a service handler can apply
                # the state change before propagating an exception.
                changed_states[automation_id] = state
                try:
                    await self.hass.services.async_call(
                        "automation",
                        SERVICE_TURN_OFF,
                        {ATTR_ENTITY_ID: automation_id},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                    return False, changed_states
                if not await self._async_confirm_state(automation_id, "off"):
                    return False, changed_states
        return True, changed_states

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

    def _zone_states(self) -> dict[str, str]:
        return {
            entity_id: state.state
            for entity_id in self._zone_entities()
            if (state := self._state(entity_id)) is not None
        }

    def _zone_entities(self) -> list[str]:
        configured = self.entry_data.get(CONF_CLIMATE_ZONES, "")
        if isinstance(configured, str):
            return [entity_id.strip() for entity_id in configured.split(",") if entity_id.strip()]
        if isinstance(configured, list):
            return [str(entity_id).strip() for entity_id in configured if str(entity_id).strip()]
        return []

    def _snapshot(self) -> dict[str, Any]:
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        snapshot: dict[str, Any] = {}
        if climate_entity:
            snapshot[CONF_DAIKIN_CLIMATE] = self._state_value(climate_entity)
        for automation_id in self._automation_entities():
            snapshot[automation_id] = self._state_value(automation_id)
        for zone_id in self._zone_entities():
            snapshot[zone_id] = self._state_value(zone_id)
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
    return (
        _mode_matches(state, desired_state.get("hvac_mode"))
        and _temperature_matches(state, desired_state.get("target_temperature"))
        and _temperature_range_matches(
            state,
            desired_state.get("target_temp_low"),
            desired_state.get("target_temp_high"),
        )
    )


def _mode_matches(state: State, desired_mode: Any) -> bool:
    """Return whether the requested mode is already observed."""
    return desired_mode is None or str(desired_mode) == state.state


def _temperature_matches(state: State, desired_temperature: Any) -> bool:
    """Return whether the requested scalar target is already observed."""
    if desired_temperature is None:
        return True
    attributes = getattr(state, "attributes", {}) or {}
    return _float_equal(attributes.get("temperature"), desired_temperature)


def _temperature_range_matches(state: State, target_low: Any, target_high: Any) -> bool:
    """Return whether every requested range target is already observed."""
    attributes = getattr(state, "attributes", {}) or {}
    return (target_low is None or _float_equal(attributes.get("target_temp_low"), target_low)) and (
        target_high is None or _float_equal(attributes.get("target_temp_high"), target_high)
    )


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 0.05
    except (TypeError, ValueError):
        return False
