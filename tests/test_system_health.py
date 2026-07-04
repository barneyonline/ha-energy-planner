"""Tests for Energy Planner system health."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.models import InputHealth, PlannerMode
from custom_components.ha_energy_planner.system_health import system_health_info


class FakeConfigEntries:
    """Minimal config entry manager."""

    def __init__(self, entries: list[object]) -> None:
        self.entries = entries

    def async_entries(self, domain: str) -> list[object]:
        assert domain == DOMAIN
        return self.entries


def test_system_health_reports_loaded_planner_state() -> None:
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            health=InputHealth.HEALTHY,
            status="current",
            mode=PlannerMode.DRY_RUN,
        ),
        options={"planner_enabled": False, "dry_run": True},
        store=SimpleNamespace(
            data={
                "haeo_runs": [{"baseline": {"status": "ready"}}],
                "ai_recommendations": [{"status": "accepted"}],
            }
        ),
    )
    entry = SimpleNamespace(
        runtime_data=coordinator,
        subentries={"energy": object(), "climate": object()},
    )
    hass = SimpleNamespace(config_entries=FakeConfigEntries([entry]))

    info = asyncio.run(system_health_info(hass))

    assert info == {
        "configured_entries": 1,
        "loaded_entries": 1,
        "planner_enabled": False,
        "dry_run": True,
        "data_healthy": True,
        "plan_status": "current",
        "plan_mode": "DRY_RUN",
        "plan_health": "healthy",
        "configured_input_groups": 2,
        "latest_haeo_status": "ready",
        "latest_ai_status": "accepted",
    }


def test_system_health_handles_unloaded_entries() -> None:
    hass = SimpleNamespace(config_entries=FakeConfigEntries([SimpleNamespace(runtime_data=None)]))

    info = asyncio.run(system_health_info(hass))

    assert info == {"configured_entries": 1, "loaded_entries": 0}
