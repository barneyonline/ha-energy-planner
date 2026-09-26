"""Availability checks at the shared device-service boundary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.ha_energy_planner.adapter_helpers import async_call_device_service


@pytest.mark.parametrize("domain", [
    "climate", "switch", "select", "number", "input_boolean", "input_number",
    "input_select", "input_datetime", "time", "timer", "button", "input_button",
])
@pytest.mark.parametrize("state", [None, "unavailable"])
def test_device_targets_wait_for_availability_and_retry(domain: str, state: str | None) -> None:
    entity_id = f"{domain}.device"
    states = {} if state is None else {entity_id: SimpleNamespace(state=state)}
    service = AsyncMock()
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get), services=SimpleNamespace(async_call=service))
    context = Context()
    data = {"entity_id": entity_id}

    async def run() -> None:
        with pytest.raises(ServiceValidationError, match="entity_target_unavailable"):
            await async_call_device_service(hass, domain, "test", data, context=context)
        service.assert_not_awaited()
        states[entity_id] = SimpleNamespace(state="on")
        await async_call_device_service(hass, domain, "test", data, context=context)
        service.assert_awaited_once_with(domain, "test", data, blocking=True, context=context)

    asyncio.run(run())


@pytest.mark.parametrize("domain", ["switch", "climate", "timer", "button", "input_button"])
def test_unknown_state_only_allows_never_used_buttons(domain: str) -> None:
    entity_id = f"{domain}.device"
    service = AsyncMock()
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _: SimpleNamespace(state="unknown")),
        services=SimpleNamespace(async_call=service),
    )
    async def run() -> None:
        if domain in {"button", "input_button"}:
            await async_call_device_service(hass, domain, "press", {"entity_id": entity_id})
            service.assert_awaited_once()
        else:
            with pytest.raises(ServiceValidationError):
                await async_call_device_service(hass, domain, "test", {"entity_id": entity_id})
            service.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("targets", [["switch.ready", "switch.late"], "switch.ready, switch.late"])
def test_batch_does_not_partially_dispatch_and_can_retry_after_recovery(targets: list[str] | str) -> None:
    states = {"switch.ready": SimpleNamespace(state="off")}
    service = AsyncMock()
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get), services=SimpleNamespace(async_call=service))
    data = {"entity_id": targets}
    async def run() -> None:
        with pytest.raises(ServiceValidationError):
            await async_call_device_service(hass, "switch", "turn_on", data)
        service.assert_not_awaited()
        states["switch.late"] = SimpleNamespace(state="off")
        await async_call_device_service(hass, "switch", "turn_on", data)
        service.assert_awaited_once_with("switch", "turn_on", data, blocking=True)
    asyncio.run(run())
