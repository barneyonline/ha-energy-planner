"""Shared Home Assistant adapter boundaries with explicit command deadlines."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError

from .const import CONF_ENPHASE_PROFILE_CONTROL_SERVICE, STATE_UNKNOWN_VALUES

DEVICE_SERVICE_TIMEOUT_SECONDS = 30.0


class DeviceTargetUnavailable(ServiceValidationError):
    """A target was rejected locally before any service was dispatched."""


async def async_call_device_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    service_data: dict[str, Any],
    *,
    blocking: bool = True,
    context: Context | None = None,
) -> None:
    """Bound dispatch; timeout is an uncertain command, not proof of rejection.

    Callers own device-specific confirmation and compensation. Caller
    cancellation propagates unchanged, leaving their durable ownership intact.
    The same deadline applies independently to compensating commands.
    DeviceTargetUnavailable proves dispatch was rejected locally; callers must
    not count it as an uncertain or attempted command.
    """
    entity_ids = service_data.get(ATTR_ENTITY_ID, [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_id.strip() for entity_id in entity_ids.split(",")]
    if any(not service_entity_available(hass, entity_id) for entity_id in entity_ids):
        raise DeviceTargetUnavailable("entity_target_unavailable")
    async with asyncio.timeout(DEVICE_SERVICE_TIMEOUT_SECONDS):
        if context is None:
            await hass.services.async_call(domain, service, service_data, blocking=blocking)
        else:
            await hass.services.async_call(domain, service, service_data, blocking=blocking, context=context)


def service_entity_available(hass: HomeAssistant, entity_id: str, *, allow_unknown: bool = False) -> bool:
    """Check a service target without rejecting buttons that were never pressed."""
    state = hass.states.get(entity_id)
    if state is None or state.state in {STATE_UNAVAILABLE, None}:
        return False
    return allow_unknown or state.state != STATE_UNKNOWN or entity_id.split(".", 1)[0] in {"button", "input_button"}


def available_state(hass: HomeAssistant, entity_id: str | None) -> State | None:
    """Read an adapter state, excluding missing and unavailable evidence."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in STATE_UNKNOWN_VALUES:
        return None
    return state


def profile_control_service(entry_data: dict[str, Any], profile_entity: str | None) -> str | None:
    """Resolve the same profile service for execution and audit targets."""
    service = entry_data.get(CONF_ENPHASE_PROFILE_CONTROL_SERVICE)
    if service:
        return str(service)
    if not profile_entity or "." not in str(profile_entity):
        return None
    domain = str(profile_entity).split(".", 1)[0]
    if domain in {"select", "input_select"}:
        return f"{domain}.select_option"
    return None


def zone_temperature_sync_deferred(state: State | None) -> bool:
    """Leave an off zone with no exposed target untouched for this takeover."""
    return (
        state is not None
        and state.state == "off"
        and all(state.attributes.get(key) is None for key in ("temperature", "target_temp_low", "target_temp_high"))
    )
