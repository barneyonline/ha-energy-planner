"""Enphase profile execution adapter."""

from __future__ import annotations

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


class EnphaseProfileAdapter:
    """Change Enphase profile through configured Home Assistant service mapping."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data

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
        pre_state = self._snapshot()
        current_profile = pre_state.get(CONF_ENPHASE_PROFILE)
        ai_profile = self.entry_data.get(CONF_ENPHASE_AI_PROFILE)
        if not ai_profile:
            return self._result(False, "enphase_ai_profile_not_configured", pre_state, current_profile, False)
        return await self._async_set_profile(str(ai_profile), pre_state, current_profile)

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
            return self._result(False, "enphase_profile_service_failed", pre_state, current_profile, False)
        post_state = self._snapshot()
        observed_profile = post_state.get(CONF_ENPHASE_PROFILE)
        if observed_profile != desired_profile:
            return EnphaseCommandResult(
                applied=False,
                reason="enphase_profile_not_confirmed",
                pre_state=pre_state,
                post_state=post_state,
                saved_profile=current_profile,
                changed_profile_at=False,
            )
        return EnphaseCommandResult(
            applied=True,
            reason="enphase_profile_applied",
            pre_state=pre_state,
            post_state=post_state,
            saved_profile=current_profile,
            changed_profile_at=True,
        )

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
