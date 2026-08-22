"""Daikin HVAC execution adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, State

from .const import (
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    STATE_UNKNOWN_VALUES,
)
from .models import ActionKind, PlanAction

_STATE_CONFIRMATION_TIMEOUT_SECONDS = 5.0
_STATE_CONFIRMATION_POLL_SECONDS = 0.05
_SCHEDULER_GUARD_DURATION = "00:00:30"


class _HVACStateConfirmationError(RuntimeError):
    """Raised when command ordering cannot be confirmed safely."""


@dataclass(slots=True)
class HVACCommandResult:
    """Result of an HVAC adapter action."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    saved_automation_states: dict[str, str]
    rollback_succeeded: bool | None = None
    saved_zone_states: dict[str, Any] = field(default_factory=dict)
    command_sent: bool = False


class DaikinHVACAdapter:
    """Control Daikin through Home Assistant climate services."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data

    def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
        """Return automation and zone state that must survive a takeover crash."""
        return self._enabled_automation_states(), self._zone_states()

    async def async_execute(self, action: PlanAction) -> HVACCommandResult:
        """Execute a supported HVAC action."""
        pre_state = self._snapshot()
        saved_automation_states = self._automation_states()
        restorable_automation_states = {
            entity_id: state
            for entity_id, state in saved_automation_states.items()
            if state == "on"
        }
        captured_zone_states = self._zone_states()
        if action.kind == ActionKind.RELEASE_HVAC:
            return await self.async_restore(restorable_automation_states, captured_zone_states)
        saved_zone_states = captured_zone_states if action.desired_state.get("enable_zones") else {}
        if action.kind != ActionKind.SET_HVAC:
            return HVACCommandResult(False, "unsupported_hvac_action", pre_state, self._snapshot(), {}, True)
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        climate_state = self._state(climate_entity)
        if climate_entity is None or climate_state is None:
            return HVACCommandResult(False, "daikin_climate_unavailable", pre_state, self._snapshot(), {}, True)
        if not action.desired_state:
            return HVACCommandResult(False, "hvac_desired_state_empty", pre_state, self._snapshot(), {}, True)
        if action.desired_state.get("configured_zones_only") and not self._zone_climate_entities():
            return HVACCommandResult(
                False,
                "zone_only_preconditioning_requires_climate_zone",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                True,
                saved_zone_states,
            )
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
        if action.desired_state.get("enable_zones") and any(
            self._state(entity_id) is None for entity_id in self._zone_entities()
        ):
            return HVACCommandResult(
                False,
                "climate_zone_unavailable",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                True,
                saved_zone_states,
            )
        if not await self._async_arm_scheduler_guard():
            return HVACCommandResult(
                False,
                "climate_scheduler_guard_failed",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                False,
                saved_zone_states,
            )
        if (
            action.desired_state.get("suppress_automations")
            and action.desired_state.get("hvac_mode") is None
            and action.desired_state.get("target_temperature") is None
            and not action.desired_state.get("enable_zones")
        ):
            disabled, changed_states = await self._async_disable_automations(saved_automation_states)
            if not disabled:
                rollback_succeeded, unresolved_states = await self._async_enable_automation_entities(
                    changed_states
                )
                return HVACCommandResult(
                    False,
                    ("hvac_automation_service_failed" if rollback_succeeded else "hvac_automation_rollback_failed"),
                    pre_state,
                    self._snapshot(),
                    unresolved_states,
                    rollback_succeeded,
                )
            command_sent = bool(saved_automation_states)
            return HVACCommandResult(
                True,
                "hvac_automations_suppressed" if command_sent else "already_in_desired_hvac_state",
                pre_state,
                self._snapshot(),
                changed_states,
                command_sent=command_sent,
            )
        disabled, changed_automations = await self._async_disable_automations(saved_automation_states)
        if not disabled:
            rollback_succeeded, unresolved_states = await self._async_enable_automation_entities(
                changed_automations
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
                changed_automations,
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
        command_sent = bool(
            saved_automation_states
            or any(entity_id.split(".", 1)[0] != "climate" for entity_id in changed_zones)
        )

        try:
            command_sent = (
                await self._async_apply_hvac_state(climate_entity, action.desired_state)
                or command_sent
            )
        except _HVACStateConfirmationError:
            state_confirmed = False
        except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
            rollback_succeeded, unresolved_states, unresolved_zones = await self._async_rollback_takeover(
                changed_automations,
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
        else:
            state_confirmed = await self._async_confirm_complete_hvac_state(
                climate_entity,
                action.desired_state,
            )
        confirmation_reason = "hvac_state_confirmation_failed"
        if not state_confirmed:
            # A schedule action can already be running when its automation is
            # disabled. Re-arm the classifier guard and reassert the complete
            # desired state once after that action has settled.
            if not await self._async_arm_scheduler_guard():
                confirmation_reason = "climate_scheduler_guard_failed"
            else:
                try:
                    command_sent = (
                        await self._async_apply_hvac_state(
                            climate_entity,
                            action.desired_state,
                            force=True,
                        )
                        or command_sent
                    )
                except _HVACStateConfirmationError:
                    confirmation_reason = "hvac_state_confirmation_failed"
                except Exception:  # noqa: BLE001 - retry remains inside the same rollback boundary.
                    confirmation_reason = "hvac_control_service_failed"
                else:
                    state_confirmed = await self._async_confirm_complete_hvac_state(
                        climate_entity,
                        action.desired_state,
                    )
        if not state_confirmed:
            rollback_succeeded, unresolved_states, unresolved_zones = await self._async_rollback_takeover(
                changed_automations,
                changed_zones,
            )
            return HVACCommandResult(
                False,
                confirmation_reason if rollback_succeeded else "hvac_acquisition_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
            )
        reason = (
            "already_in_desired_hvac_state"
            if not command_sent
            else "hvac_action_applied"
        )
        return HVACCommandResult(
            True,
            reason,
            pre_state,
            self._snapshot(),
            changed_automations,
            saved_zone_states=saved_zone_states,
            command_sent=command_sent,
        )

    async def async_restore(
        self,
        saved_automation_states: dict[str, str] | None = None,
        saved_zone_states: dict[str, Any] | None = None,
    ) -> HVACCommandResult:
        """Release HVAC ownership by restoring zones and enabling automations."""
        pre_state = self._snapshot()
        states = dict(saved_automation_states or {})
        zones = dict(saved_zone_states or {})
        if (states or zones or self._automation_entities()) and not await self._async_arm_scheduler_guard():
            return HVACCommandResult(
                False,
                "climate_scheduler_guard_failed",
                pre_state,
                self._snapshot(),
                states,
                False,
                zones,
            )
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

    async def _async_arm_scheduler_guard(self) -> bool:
        """Arm and confirm the classifier guard before planner-owned mutations."""
        guard_entity = self.entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER)
        timer_entity = self.entry_data.get(CONF_CLIMATE_SCHEDULER_GUARD_TIMER)
        if not guard_entity and not timer_entity:
            return True
        if not guard_entity or not timer_entity:
            return False

        timer_was_active = self._state_value(timer_entity) == "active"
        guard_was_on = self._state_value(guard_entity) == "on"
        try:
            await self.hass.services.async_call(
                "timer",
                "start",
                {
                    ATTR_ENTITY_ID: timer_entity,
                    "duration": _SCHEDULER_GUARD_DURATION,
                },
                blocking=True,
            )
            if not await self._async_confirm_state(timer_entity, "active"):
                await self._async_restore_failed_guard(
                    guard_entity,
                    timer_entity,
                    guard_was_on=guard_was_on,
                    timer_was_active=timer_was_active,
                )
                return False
            await self.hass.services.async_call(
                "input_boolean",
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: guard_entity},
                blocking=True,
            )
            if await self._async_confirm_state(guard_entity, "on"):
                return True
        except Exception:  # noqa: BLE001 - a missing guard must prevent actuator commands.
            pass
        await self._async_restore_failed_guard(
            guard_entity,
            timer_entity,
            guard_was_on=guard_was_on,
            timer_was_active=timer_was_active,
        )
        return False

    async def _async_restore_failed_guard(
        self,
        guard_entity: str,
        timer_entity: str,
        *,
        guard_was_on: bool,
        timer_was_active: bool,
    ) -> None:
        """Best-effort rollback of guard state changed by a failed arm."""
        if not guard_was_on:
            try:
                await self.hass.services.async_call(
                    "input_boolean",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: guard_entity},
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - original guard state is best-effort cleanup.
                pass
        if not timer_was_active:
            try:
                await self.hass.services.async_call(
                    "timer",
                    "cancel",
                    {ATTR_ENTITY_ID: timer_entity},
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - original guard state is best-effort cleanup.
                pass

    async def _async_enable_automation_entities(
        self,
        saved_states: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, str]]:
        """Enable climate automations that were active before takeover."""
        unresolved: dict[str, str] = {}
        entity_ids = [
            entity_id
            for entity_id, state in (saved_states or {}).items()
            if state == "on"
        ]
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

    async def _async_enable_zones(self, states: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Enable configured zone entities and return those changed."""
        changed: dict[str, Any] = {}
        for entity_id, state in states.items():
            if entity_id.split(".", 1)[0] == "climate":
                # Target snapshots are restored if any later command in the
                # takeover transaction fails.
                changed[entity_id] = state
                continue
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

    async def _async_restore_zone_states(self, states: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Restore captured zone states."""
        unresolved: dict[str, Any] = {}
        for entity_id, state in states.items():
            if entity_id.split(".", 1)[0] == "climate" and isinstance(state, dict):
                try:
                    await self._async_apply_hvac_state(entity_id, state)
                except Exception:  # noqa: BLE001 - retain target snapshot for a later release retry.
                    unresolved[entity_id] = state
                    continue
                if not await self._async_confirm_hvac_state(entity_id, state):
                    unresolved[entity_id] = state
                continue
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
        changed_zones: dict[str, Any],
    ) -> tuple[bool, dict[str, str], dict[str, Any]]:
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

    async def _async_confirm_complete_hvac_state(
        self,
        entity_id: str,
        desired_state: dict[str, Any],
    ) -> bool:
        """Confirm the main thermostat and every configured zone target."""
        if not await self._async_confirm_hvac_state(
            entity_id,
            _main_hvac_desired_state(desired_state),
        ):
            return False
        if not desired_state.get("enable_zones"):
            return True
        zone_target = _temperature_desired_state(desired_state)
        if not zone_target:
            return True
        for zone_entity in self._zone_climate_entities():
            if not await self._async_confirm_hvac_state(zone_entity, zone_target):
                return False
        return True

    async def _async_confirm_hvac_on(self, entity_id: str) -> bool:
        """Wait for the thermostat to leave its off state."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _STATE_CONFIRMATION_TIMEOUT_SECONDS
        while True:
            observed = self._state(entity_id)
            if observed is not None and observed.state != "off":
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_STATE_CONFIRMATION_POLL_SECONDS, remaining))

    async def _async_apply_hvac_state(
        self,
        entity_id: str,
        desired_state: dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        """Apply thermostat mode before its target and report whether a command was sent."""
        main_desired_state = _main_hvac_desired_state(desired_state)
        desired_mode = main_desired_state.get("hvac_mode")
        desired_temperature = main_desired_state.get("target_temperature")
        target_low = main_desired_state.get("target_temp_low")
        target_high = main_desired_state.get("target_temp_high")
        observed = self._state(entity_id)
        if observed is None:
            raise RuntimeError("climate state unavailable")
        command_sent = False

        if desired_mode == "off":
            if force or observed.state != "off":
                command_sent = True
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                )
            return command_sent

        if desired_mode:
            if observed.state == "off":
                command_sent = True
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                )
                if not await self._async_confirm_hvac_on(entity_id):
                    raise _HVACStateConfirmationError("climate turn-on was not confirmed")
                observed = self._state(entity_id) or observed
            if force or not _mode_matches(observed, desired_mode):
                command_sent = True
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {ATTR_ENTITY_ID: entity_id, "hvac_mode": desired_mode},
                    blocking=True,
                )
                if not await self._async_confirm_hvac_state(
                    entity_id,
                    {"hvac_mode": desired_mode},
                ):
                    raise _HVACStateConfirmationError("climate HVAC mode was not confirmed")

        observed = self._state(entity_id) or observed
        if desired_temperature is not None and (force or not _temperature_matches(observed, desired_temperature)):
            command_sent = True
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "temperature": desired_temperature},
                blocking=True,
            )
        elif (
            target_low is not None
            and target_high is not None
            and (force or not _temperature_range_matches(observed, target_low, target_high))
        ):
            command_sent = True
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "target_temp_low": target_low, "target_temp_high": target_high},
                blocking=True,
            )
        if desired_state.get("enable_zones"):
            zone_target = _temperature_desired_state(desired_state)
            for zone_entity in self._zone_climate_entities():
                command_sent = (
                    await self._async_apply_hvac_state(
                        zone_entity,
                        zone_target,
                        force=force,
                    )
                    or command_sent
                )
        return command_sent

    async def _async_disable_automations(self, states: dict[str, str]) -> tuple[bool, dict[str, str]]:
        """Stop configured automations and return the states actually changed."""
        changed_states: dict[str, str] = {}
        for automation_id, state in states.items():
            if state == "on":
                # Treat the boundary as uncertain: a service handler can apply
                # the state change before propagating an exception.
                changed_states[automation_id] = state
            try:
                # Home Assistant can report an automation as off while an
                # action sequence started earlier is still running.
                await self.hass.services.async_call(
                    "automation",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: automation_id, "stop_actions": True},
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

    def _enabled_automation_states(self) -> dict[str, str]:
        """Return configured automations that must be re-enabled on release."""
        return {
            entity_id: state
            for entity_id, state in self._automation_states().items()
            if state == "on"
        }

    def _automation_entities(self) -> list[str]:
        configured = self.entry_data.get(CONF_CLIMATE_AUTOMATIONS, "")
        if isinstance(configured, str):
            return [entity_id.strip() for entity_id in configured.split(",") if entity_id.strip()]
        if isinstance(configured, list):
            return [str(entity_id) for entity_id in configured if str(entity_id).strip()]
        return []

    def _zone_states(self) -> dict[str, Any]:
        states: dict[str, Any] = {}
        for entity_id in self._zone_entities():
            state = self._state(entity_id)
            if state is None:
                continue
            if entity_id.split(".", 1)[0] == "climate":
                states[entity_id] = _climate_target_snapshot(state)
            else:
                states[entity_id] = state.state
        return states

    def _zone_climate_entities(self) -> list[str]:
        """Return subordinate zone thermostats that receive target setpoints."""
        return [
            entity_id
            for entity_id in self._zone_entities()
            if entity_id.split(".", 1)[0] == "climate"
        ]

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
        for guard_id in (
            self.entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER),
            self.entry_data.get(CONF_CLIMATE_SCHEDULER_GUARD_TIMER),
        ):
            if guard_id:
                snapshot[guard_id] = self._state_value(guard_id)
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


def _temperature_desired_state(desired_state: dict[str, Any]) -> dict[str, Any]:
    """Return only target-temperature fields supported by subordinate zones."""
    return {
        key: desired_state[key]
        for key in ("target_temperature", "target_temp_low", "target_temp_high")
        if desired_state.get(key) is not None
    }


def _main_hvac_desired_state(desired_state: dict[str, Any]) -> dict[str, Any]:
    """Return command fields intended for the main thermostat."""
    if not desired_state.get("configured_zones_only"):
        return desired_state
    return {
        key: value
        for key, value in desired_state.items()
        if key not in {"target_temperature", "target_temp_low", "target_temp_high"}
    }


def _climate_target_snapshot(state: State) -> dict[str, Any]:
    """Capture the target fields needed to restore a subordinate thermostat."""
    attributes = getattr(state, "attributes", {}) or {}
    target_low = attributes.get("target_temp_low")
    target_high = attributes.get("target_temp_high")
    if target_low is not None and target_high is not None:
        return {
            "target_temp_low": target_low,
            "target_temp_high": target_high,
        }
    temperature = attributes.get("temperature")
    return {"target_temperature": temperature} if temperature is not None else {}


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
