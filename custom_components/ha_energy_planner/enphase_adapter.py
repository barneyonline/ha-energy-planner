"""Enphase profile execution adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, State

from .const import (
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    STATE_UNKNOWN_VALUES,
)
from .models import ActionKind, PlanAction


@dataclass(slots=True)
class EnphaseCommandResult:
    """Result of an Enphase profile command."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    saved_profile: str | None
    changed_profile_at: bool
    command_sent: bool = False
    rollback_succeeded: bool | None = None


class EnphaseProfileAdapter:
    """Change Enphase profile through configured Home Assistant service mapping."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        *,
        confirmation_attempts: int = 3,
        confirmation_interval_seconds: float = 0.25,
    ) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data
        self.confirmation_attempts = max(int(confirmation_attempts), 1)
        self.confirmation_interval_seconds = max(float(confirmation_interval_seconds), 0.0)

    async def async_execute(self, action: PlanAction) -> EnphaseCommandResult:
        """Execute an Enphase profile action."""
        pre_state = self._snapshot()
        current_profile = pre_state.get(CONF_ENPHASE_PROFILE)
        if action.kind == ActionKind.SET_PROFILE:
            desired_profile = action.desired_state.get("profile")
            if not desired_profile:
                return self._result(False, "enphase_profile_missing", pre_state, current_profile, False)
            return await self._async_set_profile(str(desired_profile), pre_state, current_profile)
        if action.kind == ActionKind.RESTORE_AI:
            return await self.async_restore_ai()
        return self._result(False, "unsupported_enphase_action", pre_state, current_profile, False)

    async def async_restore_ai(self) -> EnphaseCommandResult:
        """Restore Enphase AI Optimisation profile where configured."""
        ai_profile = self.entry_data.get(CONF_ENPHASE_AI_PROFILE)
        if not ai_profile:
            pre_state = self._snapshot()
            current_profile = pre_state.get(CONF_ENPHASE_PROFILE)
            return self._result(False, "enphase_ai_profile_not_configured", pre_state, current_profile, False)
        return await self.async_restore_profile(str(ai_profile))

    async def async_restore_profile(self, profile: str) -> EnphaseCommandResult:
        """Restore a previously saved Enphase profile."""
        pre_state = self._snapshot()
        current_profile = pre_state.get(CONF_ENPHASE_PROFILE)
        return await self._async_set_profile(profile, pre_state, current_profile)

    async def _async_set_profile(
        self,
        desired_profile: str,
        pre_state: dict[str, Any],
        current_profile: str | None,
    ) -> EnphaseCommandResult:
        profile_entity = self.entry_data.get(CONF_ENPHASE_PROFILE)
        control_service = _profile_control_service(self.entry_data, profile_entity)
        if not profile_entity or self._state(profile_entity) is None:
            return self._result(False, "enphase_profile_entity_unavailable", pre_state, current_profile, False)
        if not control_service:
            return self._result(False, "enphase_profile_control_not_configured", pre_state, current_profile, False)
        if current_profile == desired_profile:
            return self._result(True, "already_in_desired_profile", pre_state, current_profile, False)
        if "." not in str(control_service):
            return self._result(False, "enphase_profile_control_invalid", pre_state, current_profile, False)

        domain, service = str(control_service).split(".", 1)
        service_data = self._service_data(profile_entity, desired_profile)
        try:
            await self.hass.services.async_call(domain, service, service_data, blocking=True)
        except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
            rollback_succeeded = await self._async_rollback_profile(
                profile_entity,
                control_service,
                current_profile,
            )
            return EnphaseCommandResult(
                applied=False,
                reason="enphase_profile_service_failed",
                pre_state=pre_state,
                post_state=self._snapshot(),
                saved_profile=current_profile,
                changed_profile_at=False,
                command_sent=True,
                rollback_succeeded=rollback_succeeded,
            )
        confirmed = await self._async_confirm_profile(profile_entity, desired_profile)
        post_state = self._snapshot()
        if not confirmed:
            rollback_succeeded = await self._async_rollback_profile(
                profile_entity,
                control_service,
                current_profile,
            )
            return EnphaseCommandResult(
                applied=False,
                reason=(
                    "enphase_profile_not_confirmed_rolled_back"
                    if rollback_succeeded
                    else "enphase_profile_not_confirmed_rollback_failed"
                ),
                pre_state=pre_state,
                post_state=self._snapshot(),
                saved_profile=current_profile,
                changed_profile_at=False,
                command_sent=True,
                rollback_succeeded=rollback_succeeded,
            )
        return EnphaseCommandResult(
            applied=True,
            reason="enphase_profile_applied",
            pre_state=pre_state,
            post_state=post_state,
            saved_profile=current_profile,
            changed_profile_at=True,
            command_sent=True,
        )

    async def _async_confirm_profile(self, profile_entity: str, desired_profile: str) -> bool:
        """Wait a bounded time for Home Assistant to expose the requested profile."""
        for attempt in range(self.confirmation_attempts):
            if self._state_value(profile_entity) == desired_profile:
                return True
            if attempt + 1 < self.confirmation_attempts:
                await asyncio.sleep(self.confirmation_interval_seconds)
        return False

    async def _async_rollback_profile(
        self,
        profile_entity: str,
        control_service: str,
        saved_profile: str | None,
    ) -> bool:
        """Compensate an uncertain profile command and confirm the saved profile."""
        if saved_profile is None or "." not in control_service:
            return False
        domain, service = control_service.split(".", 1)
        try:
            await self.hass.services.async_call(
                domain,
                service,
                self._service_data(profile_entity, saved_profile),
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - compensation must fail closed.
            return False
        return await self._async_confirm_profile(profile_entity, saved_profile)

    @staticmethod
    def _service_data(profile_entity: str, desired_profile: str) -> dict[str, Any]:
        """Build service data for common selector domains."""
        return {
            ATTR_ENTITY_ID: profile_entity,
            "option": desired_profile,
        }

    def _snapshot(self) -> dict[str, Any]:
        profile_entity = self.entry_data.get(CONF_ENPHASE_PROFILE)
        if not profile_entity:
            return {}
        return {CONF_ENPHASE_PROFILE: self._state_value(profile_entity)}

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

    @staticmethod
    def _result(
        applied: bool,
        reason: str,
        pre_state: dict[str, Any],
        saved_profile: str | None,
        changed_profile_at: bool,
    ) -> EnphaseCommandResult:
        return EnphaseCommandResult(
            applied=applied,
            reason=reason,
            pre_state=pre_state,
            post_state=pre_state,
            saved_profile=saved_profile,
            changed_profile_at=changed_profile_at,
        )


def _profile_control_service(entry_data: dict[str, Any], profile_entity: str | None) -> str | None:
    """Return the service used to select an Enphase profile."""
    service = entry_data.get(CONF_ENPHASE_PROFILE_CONTROL_SERVICE)
    if service:
        return str(service)
    if not profile_entity or "." not in str(profile_entity):
        return None
    domain = str(profile_entity).split(".", 1)[0]
    if domain in {"select", "input_select"}:
        return f"{domain}.select_option"
    return None
