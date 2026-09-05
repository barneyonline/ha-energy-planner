"""Real Home Assistant contracts exercised at the minimum and current versions."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.icon import async_get_icons
from homeassistant.util import dt as dt_util

from custom_components.ha_energy_planner import async_setup_entry
from custom_components.ha_energy_planner.ai_advisor import LocalAIAdvisor
from custom_components.ha_energy_planner.const import CONF_AI_TASK_ENTITY, DOMAIN
from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator
from custom_components.ha_energy_planner.models import (
    DecisionContext,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    PlannerMode,
)
from custom_components.ha_energy_planner.storage import PlannerStore


def test_every_integration_module_imports_on_supported_home_assistant() -> None:
    component = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
    for path in sorted(component.glob("*.py")):
        importlib.import_module(f"custom_components.{DOMAIN}.{path.stem}")


def test_home_assistant_loads_custom_entity_icon_resources(tmp_path: Path) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        component = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
        manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
        integration = loader.Integration(hass, f"custom_components.{DOMAIN}", component, manifest)
        hass.data[loader.DATA_INTEGRATIONS] = {DOMAIN: integration}
        try:
            icons = await async_get_icons(hass, "entity", [DOMAIN])
            assert icons[DOMAIN]["calendar"]["plan"]["default"] == "mdi:calendar-clock"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_real_config_entry_awaits_coordinator_unload_callbacks(tmp_path: Path) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        entry = ConfigEntry(
            domain=DOMAIN, title="Runtime contract", data={}, options={},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, PlannerStore(hass, entry.entry_id))
        entry.runtime_data = coordinator
        coordinator.async_start_listeners()
        entry.async_on_unload(coordinator.async_shutdown)
        try:
            # Run HA's actual callback scheduler. A fake that simply calls a
            # coroutine function would never exercise the base shutdown flag.
            await entry._async_process_on_unload(hass)
            assert coordinator._shutdown_requested is True
            assert coordinator._tearing_down is True
            assert coordinator._boundary_cancel is None
            assert coordinator._unsub_listeners == []
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_real_service_registry_provider_typeerror_is_not_retried(tmp_path: Path) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls: list[ServiceCall] = []

        async def provider(call: ServiceCall):
            calls.append(call)
            raise TypeError("provider failed after accepting the request")

        hass.services.async_register("ai_task", "generate_data", provider, supports_response=SupportsResponse.ONLY)
        hass.states.async_set("ai_task.audit_provider", "unknown")
        now = dt_util.utcnow()
        context = DecisionContext(
            created_at=now, plan_id="runtime-test", slots=[], current_battery_soc_percent=None,
            current_ev_soc_percent=None, occupancy_state=OccupancyState.UNKNOWN,
            input_health=InputHealth.DEGRADED,
        )
        plan = EnergyPlan(
            plan_id="runtime-test", created_at=now, horizon_hours=24, interval_minutes=5,
            status="current", health=InputHealth.DEGRADED, mode=PlannerMode.DRY_RUN,
            summary="Runtime contract", confidence=0, estimated_daily_cost=None, actions=[], preview=[],
        )
        try:
            result = await LocalAIAdvisor(
                hass, {CONF_AI_TASK_ENTITY: "ai_task.audit_provider"}, {}
            ).async_get_advice(context, plan)
            assert len(calls) == 1
            assert result.rejected_reason == "ai_service_failed:TypeError"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["first_refresh", "platform_forwarding"])
def test_cancelled_setup_cleans_real_runtime_resources(tmp_path: Path, monkeypatch, stage: str) -> None:
    """Cancel at HA boundaries using a real coordinator, Store and ConfigEntry."""
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        entry = ConfigEntry(
            domain=DOMAIN, title="Runtime cancellation", data={}, options={"planner_enabled": False},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        reached = asyncio.Event()
        never = asyncio.Event()
        coordinators = []

        async def hold_refresh(coordinator) -> None:
            coordinators.append(coordinator)
            reached.set()
            await never.wait()

        async def hold_forwarding(manager, forwarded_entry, platforms) -> None:
            coordinators.append(forwarded_entry.runtime_data)
            reached.set()
            await never.wait()

        if stage == "first_refresh":
            monkeypatch.setattr(EnergyPlannerCoordinator, "async_config_entry_first_refresh", hold_refresh)
        monkeypatch.setattr(ConfigEntries, "async_forward_entry_setups", hold_forwarding)
        setup_task = asyncio.create_task(async_setup_entry(hass, entry))
        reached_task = asyncio.create_task(reached.wait())
        try:
            done, _ = await asyncio.wait(
                [setup_task, reached_task], timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            if setup_task in done:
                await setup_task
            assert reached_task in done
            coordinator = coordinators[0]
            if stage == "platform_forwarding":
                assert coordinator._boundary_cancel is not None
            setup_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(setup_task, timeout=10)
            assert not hasattr(entry, "runtime_data")
            assert coordinator._shutdown_requested
            assert coordinator._tearing_down
            assert coordinator._boundary_cancel is None
            assert coordinator._unsub_listeners == []
            assert coordinator.store.data["ownership"] == {}
        finally:
            reached_task.cancel()
            await asyncio.gather(reached_task, return_exceptions=True)
            if not setup_task.done():
                setup_task.cancel()
                await asyncio.gather(setup_task, return_exceptions=True)
            await hass.async_stop(force=True)

    asyncio.run(run())
