"""Daikin HVAC execution adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
_ROLLBACK_ACTIVE_HVAC_MODE = "rollback_active_hvac_mode"
_ROLLBACK_HVAC_MODE_CHANGED = "rollback_hvac_mode_changed"
_ACTIVE_HVAC_MODES = frozenset({"auto", "cool", "dry", "fan_only", "heat", "heat_cool"})


class _HVACStateConfirmationError(RuntimeError):
    """Raised when command ordering cannot be confirmed safely."""


class _HVACManualOverrideError(RuntimeError):
    """Raised when a user supersedes an in-flight HVAC command."""


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
    saved_main_state: dict[str, Any] = field(default_factory=dict)
    command_sent: bool = False


class DaikinHVACAdapter:
    """Control Daikin through Home Assistant climate services."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data
        self._async_persist_main_state: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._manual_override_requested: Callable[[], bool] | None = None
        self._async_persist_manual_supersession: Callable[[], Awaitable[None]] | None = None
        self._manual_supersession_persisted = False
        self._manual_zone_overrides_requested: Callable[[], set[str]] | None = None
        self._async_persist_zone_supersession: Callable[[set[str]], Awaitable[None]] | None = None
        self._async_persist_supersessions: Callable[[bool, set[str]], Awaitable[None]] | None = None
        self._persisted_zone_supersessions: set[str] = set()
        self._set_turn_on_feedback_expected: Callable[[bool], None] | None = None
        self._set_pending_main_restore: Callable[[dict[str, Any]], None] | None = None
        self._set_pending_zone_restore: Callable[[dict[str, Any]], None] | None = None

    def set_main_state_persistence_callback(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Set the crash-boundary persistence hook for evolving main rollback state."""
        self._async_persist_main_state = callback

    def set_manual_override_check(self, callback: Callable[[], bool]) -> None:
        """Set the synchronous check for a superseding user command."""
        self._manual_override_requested = callback

    def set_manual_override_persistence_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Set the durable boundary for a superseding user command."""
        self._async_persist_manual_supersession = callback

    def set_zone_manual_override_check(
        self,
        callback: Callable[[], set[str]],
    ) -> None:
        """Set the synchronous check for superseding zone commands."""
        self._manual_zone_overrides_requested = callback

    def set_zone_manual_override_persistence_callback(
        self,
        callback: Callable[[set[str]], Awaitable[None]],
    ) -> None:
        """Set the durable boundary for superseding zone commands."""
        self._async_persist_zone_supersession = callback

    def set_manual_supersession_persistence_callback(
        self,
        callback: Callable[[bool, set[str]], Awaitable[None]],
    ) -> None:
        """Set the atomic durable boundary for main and zone supersession."""
        self._async_persist_supersessions = callback

    def set_turn_on_feedback_callback(
        self,
        callback: Callable[[bool], None],
    ) -> None:
        """Expose the bounded phase that can publish a turn-on mode."""
        self._set_turn_on_feedback_expected = callback

    def set_pending_main_restore_callback(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Expose the main rollback snapshot expected during restoration."""
        self._set_pending_main_restore = callback

    def set_pending_zone_restore_callback(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Expose zone rollback snapshots expected during restoration."""
        self._set_pending_zone_restore = callback

    def _manual_zone_override_entity_ids(self) -> set[str]:
        """Return configured zones superseded during this transaction."""
        if self._manual_zone_overrides_requested is None:
            return set()
        return {
            str(entity_id)
            for entity_id in self._manual_zone_overrides_requested()
            if str(entity_id)
        }

    def _raise_if_manual_override_requested(
        self,
        *,
        include_zones: bool = True,
    ) -> None:
        """Stop the transaction before another planner-owned mutation."""
        main_superseded = (
            self._manual_override_requested is not None
            and self._manual_override_requested()
        )
        if main_superseded or (
            include_zones and self._manual_zone_override_entity_ids()
        ):
            raise _HVACManualOverrideError

    async def _async_persist_override_supersessions(
        self,
        main_superseded: bool,
        zone_entity_ids: set[str],
    ) -> None:
        """Persist newly observed main and zone supersession atomically."""
        persist_main = main_superseded and not self._manual_supersession_persisted
        persist_zones = zone_entity_ids - self._persisted_zone_supersessions
        if not persist_main and not persist_zones:
            return
        if self._async_persist_supersessions is not None:
            await self._async_persist_supersessions(
                persist_main,
                persist_zones,
            )
        else:
            if persist_main and self._async_persist_manual_supersession is not None:
                await self._async_persist_manual_supersession()
            if persist_zones and self._async_persist_zone_supersession is not None:
                await self._async_persist_zone_supersession(persist_zones)
        self._manual_supersession_persisted = (
            self._manual_supersession_persisted or persist_main
        )
        self._persisted_zone_supersessions.update(persist_zones)

    async def _async_persist_requested_manual_supersessions(self) -> None:
        """Persist main and zone supersession at an actuator boundary."""
        while True:
            main_superseded = (
                self._manual_override_requested is not None
                and self._manual_override_requested()
            )
            zone_entity_ids = self._manual_zone_override_entity_ids()
            await self._async_persist_override_supersessions(
                main_superseded,
                zone_entity_ids,
            )
            main_is_pending = (
                self._manual_override_requested is not None
                and self._manual_override_requested()
                and not self._manual_supersession_persisted
            )
            zones_are_pending = not self._manual_zone_override_entity_ids().issubset(
                self._persisted_zone_supersessions
            )
            if not main_is_pending and not zones_are_pending:
                return

    def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
        """Return automation and zone state that must survive a takeover crash."""
        return self._enabled_automation_states(), self._zone_states()

    def main_takeover_snapshot(self) -> dict[str, Any]:
        """Return main climate state that must survive a takeover crash."""
        climate_state = self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE))
        return {} if climate_state is None else _climate_state_snapshot(climate_state)

    async def async_execute(self, action: PlanAction) -> HVACCommandResult:
        """Execute a supported HVAC action."""
        self._manual_supersession_persisted = False
        self._persisted_zone_supersessions.clear()
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
        synchronize_zone_temperatures = action.desired_state.get("configured_zones_only") is True
        controlled_zone_entities = [
            entity_id
            for entity_id in self._zone_entities()
            if entity_id.split(".", 1)[0] != "climate"
            or synchronize_zone_temperatures
        ]
        saved_zone_states = {
            entity_id: state
            for entity_id, state in captured_zone_states.items()
            if action.desired_state.get("enable_zones")
            and entity_id in controlled_zone_entities
        }
        if action.kind != ActionKind.SET_HVAC:
            return HVACCommandResult(False, "unsupported_hvac_action", pre_state, self._snapshot(), {}, True)
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        climate_state = self._state(climate_entity)
        if climate_entity is None or climate_state is None:
            return HVACCommandResult(False, "daikin_climate_unavailable", pre_state, self._snapshot(), {}, True)
        saved_main_state = _climate_state_snapshot(climate_state)
        if not action.desired_state:
            return HVACCommandResult(False, "hvac_desired_state_empty", pre_state, self._snapshot(), {}, True)
        if (
            _temperature_desired_state(action.desired_state)
            and not _has_restorable_temperature_target(saved_main_state)
        ):
            # A main-target mutation cannot be transactional unless the target
            # it is about to replace can be restored after any later failure.
            return HVACCommandResult(
                False,
                "main_climate_target_unavailable",
                pre_state,
                self._snapshot(),
                {},
                True,
            )
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
            self._state(entity_id) is None for entity_id in controlled_zone_entities
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
        if (
            action.desired_state.get("enable_zones")
            and synchronize_zone_temperatures
            and _temperature_desired_state(action.desired_state)
            and any(
                not _has_restorable_temperature_target(
                    dict(saved_zone_states.get(entity_id, {}))
                )
                for entity_id in self._zone_climate_entities()
            )
        ):
            # Every subordinate target that will be replaced needs a usable
            # rollback snapshot. An empty climate snapshot otherwise makes both
            # apply and confirmation no-ops while leaving the planner target set.
            return HVACCommandResult(
                False,
                "climate_zone_target_unavailable",
                pre_state,
                self._snapshot(),
                {},
                True,
            )
        if not await self._async_arm_scheduler_guard():
            await self._async_persist_requested_manual_supersessions()
            superseded_zones = self._manual_zone_override_entity_ids()
            return HVACCommandResult(
                False,
                "climate_scheduler_guard_failed",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                False,
                {
                    entity_id: state
                    for entity_id, state in saved_zone_states.items()
                    if entity_id not in superseded_zones
                },
            )
        manual_override_result = await self._async_manual_override_result_if_requested(
            pre_state,
            {},
            {},
            main_entity=climate_entity,
            saved_main_state=saved_main_state,
        )
        if manual_override_result is not None:
            return manual_override_result
        if (
            action.desired_state.get("suppress_automations")
            and action.desired_state.get("hvac_mode") is None
            and action.desired_state.get("target_temperature") is None
            and not action.desired_state.get("enable_zones")
        ):
            disabled, changed_states = await self._async_disable_automations(saved_automation_states)
            manual_override_result = await self._async_manual_override_result_if_requested(
                pre_state,
                changed_states,
                {},
                main_entity=climate_entity,
                saved_main_state=saved_main_state,
            )
            if manual_override_result is not None:
                return manual_override_result
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
        manual_override_result = await self._async_manual_override_result_if_requested(
            pre_state,
            changed_automations,
            {},
            main_entity=climate_entity,
            saved_main_state=saved_main_state,
        )
        if manual_override_result is not None:
            return manual_override_result
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
        manual_override_result = await self._async_manual_override_result_if_requested(
            pre_state,
            changed_automations,
            changed_zones,
            main_entity=climate_entity,
            saved_main_state=saved_main_state,
        )
        if manual_override_result is not None:
            return manual_override_result
        if not zones_enabled:
            rollback_succeeded, unresolved_states, unresolved_zones, unresolved_main_state = (
                await self._async_rollback_takeover(
                    changed_automations,
                    changed_zones,
                    main_entity=climate_entity,
                    saved_main_state=saved_main_state,
                )
            )
            return HVACCommandResult(
                False,
                "hvac_zone_service_failed" if rollback_succeeded else "hvac_acquisition_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
                unresolved_main_state,
            )
        command_sent = bool(
            saved_automation_states
            or any(entity_id.split(".", 1)[0] != "climate" for entity_id in changed_zones)
        )

        try:
            command_sent = (
                await self._async_apply_hvac_state(
                    climate_entity,
                    action.desired_state,
                    takeover_main_state=saved_main_state,
                )
                or command_sent
            )
            self._raise_if_manual_override_requested()
            state_confirmed = await self._async_confirm_complete_hvac_state(
                climate_entity,
                action.desired_state,
            )
            self._raise_if_manual_override_requested()
        except _HVACManualOverrideError:
            return await self._async_manual_override_result(
                pre_state,
                changed_automations,
                changed_zones,
                main_entity=climate_entity,
                saved_main_state=saved_main_state,
            )
        except _HVACStateConfirmationError:
            state_confirmed = False
        except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
            rollback_succeeded, unresolved_states, unresolved_zones, unresolved_main_state = (
                await self._async_rollback_takeover(
                    changed_automations,
                    changed_zones,
                    main_entity=climate_entity,
                    saved_main_state=saved_main_state,
                )
            )
            return HVACCommandResult(
                False,
                "hvac_control_service_failed" if rollback_succeeded else "hvac_acquisition_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
                unresolved_main_state,
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
                    self._raise_if_manual_override_requested()
                    command_sent = (
                        await self._async_apply_hvac_state(
                            climate_entity,
                            action.desired_state,
                            force=True,
                            takeover_main_state=saved_main_state,
                        )
                        or command_sent
                    )
                    self._raise_if_manual_override_requested()
                    state_confirmed = await self._async_confirm_complete_hvac_state(
                        climate_entity,
                        action.desired_state,
                    )
                    self._raise_if_manual_override_requested()
                except _HVACManualOverrideError:
                    return await self._async_manual_override_result(
                        pre_state,
                        changed_automations,
                        changed_zones,
                        main_entity=climate_entity,
                        saved_main_state=saved_main_state,
                    )
                except _HVACStateConfirmationError:
                    confirmation_reason = "hvac_state_confirmation_failed"
                except Exception:  # noqa: BLE001 - retry remains inside the same rollback boundary.
                    confirmation_reason = "hvac_control_service_failed"
        if not state_confirmed:
            rollback_succeeded, unresolved_states, unresolved_zones, unresolved_main_state = (
                await self._async_rollback_takeover(
                    changed_automations,
                    changed_zones,
                    main_entity=climate_entity,
                    saved_main_state=saved_main_state,
                )
            )
            return HVACCommandResult(
                False,
                confirmation_reason if rollback_succeeded else "hvac_acquisition_rollback_failed",
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                unresolved_zones,
                unresolved_main_state,
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
        saved_main_state: dict[str, Any] | None = None,
    ) -> HVACCommandResult:
        """Release HVAC ownership by restoring captured actuator state."""
        self._manual_supersession_persisted = False
        self._persisted_zone_supersessions.clear()
        pre_state = self._snapshot()
        states = dict(saved_automation_states or {})
        zones = dict(saved_zone_states or {})
        main_state = dict(saved_main_state or {})
        if (
            states or zones or main_state or self._automation_entities()
        ) and not await self._async_arm_scheduler_guard():
            await self._async_persist_requested_manual_supersessions()
            main_superseded = (
                self._manual_override_requested is not None
                and self._manual_override_requested()
            )
            superseded_zones = self._manual_zone_override_entity_ids()
            return HVACCommandResult(
                False,
                "climate_scheduler_guard_failed",
                pre_state,
                self._snapshot(),
                states,
                False,
                {
                    entity_id: state
                    for entity_id, state in zones.items()
                    if entity_id not in superseded_zones
                },
                {} if main_superseded else main_state,
            )
        main_restored = True
        unresolved_main_state: dict[str, Any] = {}
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        await self._async_persist_requested_manual_supersessions()
        if main_state:
            main_restored = bool(climate_entity) and await self._async_restore_main_state_preserving_manual(
                str(climate_entity),
                main_state,
            )
            if not main_restored:
                unresolved_main_state = main_state
        await self._async_persist_requested_manual_supersessions()
        zones_restored, unresolved_zones = await self._async_restore_zone_states(zones)
        await self._async_persist_requested_manual_supersessions()
        restored, unresolved_states = await self._async_enable_automation_entities(states)
        await self._async_persist_requested_manual_supersessions()
        reason = "no_hvac_automation_state_saved"
        if not main_restored or not restored or not zones_restored:
            reason = "hvac_release_failed"
        elif states or zones or main_state or self._automation_entities():
            reason = "hvac_control_released"
        return HVACCommandResult(
            applied=(bool(states or zones or main_state or self._automation_entities()))
            and main_restored
            and restored
            and zones_restored,
            reason=reason,
            pre_state=pre_state,
            post_state=self._snapshot(),
            saved_automation_states=unresolved_states,
            rollback_succeeded=main_restored and restored and zones_restored,
            saved_zone_states=unresolved_zones,
            saved_main_state=unresolved_main_state,
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
                await self._async_persist_requested_manual_supersessions()
                unresolved[entity_id] = "on"
                continue
            confirmed = await self._async_confirm_state(entity_id, "on")
            await self._async_persist_requested_manual_supersessions()
            if not confirmed:
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
            try:
                self._raise_if_manual_override_requested()
            except _HVACManualOverrideError:
                return False, changed
            if not await self._async_confirm_state(entity_id, "on"):
                return False, changed
            try:
                self._raise_if_manual_override_requested()
            except _HVACManualOverrideError:
                return False, changed
        return True, changed

    async def _async_restore_zone_states(self, states: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Restore captured zone states."""
        unresolved: dict[str, Any] = {}
        for entity_id, state in states.items():
            if await self._async_zone_restore_is_superseded(entity_id):
                continue
            confirmed = False
            if entity_id.split(".", 1)[0] == "climate":
                if not isinstance(state, dict) or not _has_restorable_temperature_target(state):
                    # Empty or malformed persisted target evidence cannot prove
                    # that a previously changed zone was restored.
                    unresolved[entity_id] = state
                    continue
                try:
                    await self._async_apply_hvac_state(
                        entity_id,
                        state,
                        respect_manual_override=False,
                    )
                    confirmed = await self._async_confirm_hvac_state(
                        entity_id,
                        state,
                    )
                except Exception:  # noqa: BLE001 - retain target snapshot for a later release retry.
                    pass
            else:
                observed = self._state(entity_id)
                if observed is not None and observed.state == state:
                    continue
                service = SERVICE_TURN_ON if state == "on" else SERVICE_TURN_OFF
                try:
                    await self.hass.services.async_call(
                        entity_id.split(".", 1)[0],
                        service,
                        {ATTR_ENTITY_ID: entity_id},
                        blocking=True,
                    )
                    confirmed = await self._async_confirm_state(
                        entity_id,
                        state,
                    )
                except Exception:  # noqa: BLE001 - retain state for a later release retry.
                    pass
            if await self._async_zone_restore_is_superseded(entity_id):
                continue
            if not confirmed:
                unresolved[entity_id] = state
        return not unresolved, unresolved

    async def _async_zone_restore_is_superseded(self, entity_id: str) -> bool:
        """Persist and identify a zone whose user state must be preserved."""
        await self._async_persist_requested_manual_supersessions()
        if entity_id not in self._manual_zone_override_entity_ids():
            return False
        return True

    async def _async_rollback_takeover(
        self,
        saved_automation_states: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
        restore_main: bool = True,
    ) -> tuple[bool, dict[str, str], dict[str, Any], dict[str, Any]]:
        """Release every actuator acquired before a climate command failed."""
        if restore_main and self._set_pending_main_restore is not None:
            self._set_pending_main_restore(dict(saved_main_state))
        if self._set_pending_zone_restore is not None:
            self._set_pending_zone_restore(dict(changed_zones))
        await self._async_persist_requested_manual_supersessions()
        main_restored = (
            await self._async_restore_main_state_preserving_manual(
                main_entity,
                saved_main_state,
            )
            if restore_main
            else True
        )
        await self._async_persist_requested_manual_supersessions()
        zones_restored, unresolved_zones = await self._async_restore_zone_states(changed_zones)
        await self._async_persist_requested_manual_supersessions()
        automations_restored, unresolved_states = await self._async_enable_automation_entities(
            saved_automation_states
        )
        await self._async_persist_requested_manual_supersessions()
        return (
            main_restored and zones_restored and automations_restored,
            unresolved_states,
            unresolved_zones,
            {} if main_restored or not restore_main else saved_main_state,
        )

    async def _async_manual_override_result(
        self,
        pre_state: dict[str, Any],
        changed_automations: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
    ) -> HVACCommandResult:
        """Roll back while preserving every user-superseded climate entity."""
        main_superseded = (
            self._manual_override_requested is not None
            and self._manual_override_requested()
        )
        superseded_zones = self._manual_zone_override_entity_ids()
        await self._async_persist_override_supersessions(
            main_superseded,
            superseded_zones,
        )
        rollback_zones = {
            entity_id: state
            for entity_id, state in changed_zones.items()
            if entity_id not in superseded_zones
        }
        rollback_succeeded, unresolved_states, unresolved_zones, unresolved_main = (
            await self._async_rollback_takeover(
                changed_automations,
                rollback_zones,
                main_entity=main_entity,
                saved_main_state=saved_main_state,
                restore_main=not main_superseded,
            )
        )
        return HVACCommandResult(
            False,
            (
                "manual_hvac_override_detected"
                if rollback_succeeded
                else "hvac_acquisition_rollback_failed"
            ),
            pre_state,
            self._snapshot(),
            unresolved_states,
            rollback_succeeded,
            unresolved_zones,
            unresolved_main,
        )

    async def _async_manual_override_result_if_requested(
        self,
        pre_state: dict[str, Any],
        changed_automations: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
    ) -> HVACCommandResult | None:
        """Return a subordinate-only rollback when a user superseded the command."""
        try:
            self._raise_if_manual_override_requested()
        except _HVACManualOverrideError:
            return await self._async_manual_override_result(
                pre_state,
                changed_automations,
                changed_zones,
                main_entity=main_entity,
                saved_main_state=saved_main_state,
            )
        return None

    async def _async_restore_main_state_preserving_manual(
        self,
        entity_id: str,
        saved_state: dict[str, Any],
    ) -> bool:
        """Restore main state unless a user supersedes the pending snapshot."""
        try:
            return await self._async_restore_main_state(entity_id, saved_state)
        except _HVACManualOverrideError:
            await self._async_persist_requested_manual_supersessions()
            return True

    async def _async_restore_main_state(
        self,
        entity_id: str,
        saved_state: dict[str, Any],
    ) -> bool:
        """Restore and confirm the complete main state changed during takeover."""
        desired_state = _main_hvac_desired_state(saved_state)
        if not desired_state:
            return False
        if desired_state.get("hvac_mode") == "off":
            restore_succeeded = True
            if saved_state.get(_ROLLBACK_HVAC_MODE_CHANGED) is True:
                active_mode = saved_state.get(_ROLLBACK_ACTIVE_HVAC_MODE)
                active_mode_restored = (
                    isinstance(active_mode, str)
                    and active_mode in _ACTIVE_HVAC_MODES
                )
                restore_succeeded = active_mode_restored
                if active_mode_restored:
                    # Restore the mode Daikin will remember for its next
                    # turn-on before returning the thermostat to off.
                    try:
                        await self._async_apply_hvac_state(
                            entity_id,
                            {"hvac_mode": active_mode},
                            respect_zone_manual_override=False,
                        )
                    except _HVACManualOverrideError:
                        raise
                    except Exception:  # noqa: BLE001 - continue to the mandatory off cleanup.
                        restore_succeeded = False
            # Restore the target while the unit still accepts temperature
            # commands, but never let a target failure skip the off cleanup.
            target_state = _temperature_desired_state(desired_state)
            if target_state:
                try:
                    await self._async_apply_hvac_state(
                        entity_id,
                        target_state,
                        respect_zone_manual_override=False,
                    )
                except _HVACManualOverrideError:
                    raise
                except Exception:  # noqa: BLE001 - continue to the mandatory off cleanup.
                    restore_succeeded = False
            try:
                await self._async_apply_hvac_state(
                    entity_id,
                    {"hvac_mode": "off"},
                    respect_zone_manual_override=False,
                )
            except _HVACManualOverrideError:
                raise
            except Exception:  # noqa: BLE001 - retain ownership even if the service changed state.
                restore_succeeded = False
            main_state_restored = await self._async_confirm_hvac_state(
                entity_id,
                desired_state,
            )
            self._raise_if_manual_override_requested(include_zones=False)
            return restore_succeeded and main_state_restored
        try:
            await self._async_apply_hvac_state(
                entity_id,
                desired_state,
                respect_zone_manual_override=False,
            )
        except _HVACManualOverrideError:
            raise
        except Exception:  # noqa: BLE001 - rollback must report unresolved main state.
            return False
        main_state_restored = await self._async_confirm_hvac_state(
            entity_id,
            desired_state,
        )
        self._raise_if_manual_override_requested(include_zones=False)
        return main_state_restored

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
        if not (
            desired_state.get("enable_zones")
            and desired_state.get("configured_zones_only") is True
        ):
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
        takeover_main_state: dict[str, Any] | None = None,
        respect_manual_override: bool = True,
        respect_zone_manual_override: bool = True,
    ) -> bool:
        """Apply thermostat mode before its target and report whether a command was sent."""
        if respect_manual_override:
            self._raise_if_manual_override_requested(
                include_zones=respect_zone_manual_override,
            )
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
                if respect_manual_override:
                    self._raise_if_manual_override_requested(
                        include_zones=respect_zone_manual_override,
                    )
            return command_sent

        if desired_mode:
            if observed.state == "off":
                command_sent = True
                if self._set_turn_on_feedback_expected is not None:
                    self._set_turn_on_feedback_expected(True)
                try:
                    await self.hass.services.async_call(
                        "climate",
                        SERVICE_TURN_ON,
                        {ATTR_ENTITY_ID: entity_id},
                        blocking=True,
                    )
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                    if not await self._async_confirm_hvac_on(entity_id):
                        raise _HVACStateConfirmationError(
                            "climate turn-on was not confirmed"
                        )
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                    observed = self._state(entity_id) or observed
                finally:
                    if self._set_turn_on_feedback_expected is not None:
                        self._set_turn_on_feedback_expected(False)
            if force or not _mode_matches(observed, desired_mode):
                if takeover_main_state is not None and takeover_main_state.get("hvac_mode") == "off":
                    rollback_state = dict(takeover_main_state)
                    mode_was_already_changed = (
                        rollback_state.get(_ROLLBACK_HVAC_MODE_CHANGED) is True
                    )
                    rollback_state[_ROLLBACK_HVAC_MODE_CHANGED] = True
                    if (
                        not mode_was_already_changed
                        and observed.state in _ACTIVE_HVAC_MODES
                    ):
                        rollback_state[_ROLLBACK_ACTIVE_HVAC_MODE] = observed.state
                    if self._async_persist_main_state is not None:
                        # The enriched snapshot must be durable before the
                        # following call overwrites Daikin's remembered mode.
                        await self._async_persist_main_state(rollback_state)
                        if respect_manual_override:
                            self._raise_if_manual_override_requested(
                                include_zones=respect_zone_manual_override,
                            )
                    takeover_main_state.update(rollback_state)
                command_sent = True
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {ATTR_ENTITY_ID: entity_id, "hvac_mode": desired_mode},
                    blocking=True,
                )
                if respect_manual_override:
                    self._raise_if_manual_override_requested(
                        include_zones=respect_zone_manual_override,
                    )
                if not await self._async_confirm_hvac_state(
                    entity_id,
                    {"hvac_mode": desired_mode},
                ):
                    raise _HVACStateConfirmationError("climate HVAC mode was not confirmed")
                if respect_manual_override:
                    self._raise_if_manual_override_requested(
                        include_zones=respect_zone_manual_override,
                    )

        observed = self._state(entity_id) or observed
        if desired_temperature is not None and (force or not _temperature_matches(observed, desired_temperature)):
            command_sent = True
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "temperature": desired_temperature},
                blocking=True,
            )
            if respect_manual_override:
                self._raise_if_manual_override_requested(
                    include_zones=respect_zone_manual_override,
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
            if respect_manual_override:
                self._raise_if_manual_override_requested(
                    include_zones=respect_zone_manual_override,
                )
        if (
            desired_state.get("enable_zones")
            and desired_state.get("configured_zones_only") is True
        ):
            # Daikin zone temperature bounds can depend on the main thermostat
            # target. Confirm the main target before asking subordinate climate
            # entities to accept the same setpoint.
            if not await self._async_confirm_hvac_state(entity_id, main_desired_state):
                raise _HVACStateConfirmationError(
                    "main climate state was not confirmed before zone targets"
                )
            zone_target = _temperature_desired_state(desired_state)
            for zone_entity in self._zone_climate_entities():
                command_sent = (
                    await self._async_apply_hvac_state(
                        zone_entity,
                        zone_target,
                        force=force,
                        respect_manual_override=respect_manual_override,
                        respect_zone_manual_override=respect_zone_manual_override,
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
            try:
                self._raise_if_manual_override_requested()
            except _HVACManualOverrideError:
                return False, changed_states
            if not await self._async_confirm_state(automation_id, "off"):
                return False, changed_states
            try:
                self._raise_if_manual_override_requested()
            except _HVACManualOverrideError:
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


def _has_restorable_temperature_target(saved_state: dict[str, Any]) -> bool:
    """Return whether a snapshot can restore one complete target shape."""
    return saved_state.get("target_temperature") is not None or (
        saved_state.get("target_temp_low") is not None
        and saved_state.get("target_temp_high") is not None
    )


def _main_hvac_desired_state(desired_state: dict[str, Any]) -> dict[str, Any]:
    """Return command fields intended for the main thermostat."""
    return {
        key: desired_state[key]
        for key in ("hvac_mode", "target_temperature", "target_temp_low", "target_temp_high")
        if desired_state.get(key) is not None
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


def _climate_state_snapshot(state: State) -> dict[str, Any]:
    """Capture the main thermostat mode and target for transactional rollback."""
    return {"hvac_mode": state.state, **_climate_target_snapshot(state)}


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
