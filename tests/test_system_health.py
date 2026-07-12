"""Tests for Energy Planner system health."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.models import InputHealth, PlannerMode
from custom_components.ha_energy_planner.system_health import (
    _latest_haeo_metric,
    _latest_value,
    system_health_info,
)


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
            estimated_cost_horizon_hours=12.0,
        ),
        options={"planner_enabled": False, "dry_run": True},
        last_refresh_metadata={"duration_ms": 15.0},
        refresh_metrics={
            "refreshes_last_hour": 12,
            "last_trigger": "boundary",
            "fingerprint_skipped": 3,
            "coalesced": 4,
            "phase_durations_ms": {"inputs": 4.5},
        },
        store=SimpleNamespace(
            data={
                "haeo_runs": [
                    {
                        "baseline": {"status": "ready", "duration_ms": 10.0, "cache_hit": False},
                        "second_pass": {"duration_ms": 2.5, "cache_hit": True},
                    }
                ],
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
        "last_refresh_duration_ms": 15.0,
        "refreshes_per_hour": 12,
        "refresh_trigger_counts": {},
        "last_refresh_trigger": "boundary",
        "skipped_refresh_count": 3,
        "coalesced_refresh_count": 4,
        "refresh_phase_durations_ms": {"inputs": 4.5},
        "usable_optimization_horizon_hours": 12.0,
        "refresh_metrics": {
            "refreshes_last_hour": 12,
            "last_trigger": "boundary",
            "fingerprint_skipped": 3,
            "coalesced": 4,
            "phase_durations_ms": {"inputs": 4.5},
        },
        "latest_haeo_duration_ms": 12.5,
        "latest_haeo_cache_hit": True,
        "latest_ai_status": "accepted",
    }


def test_system_health_handles_unloaded_entries() -> None:
    hass = SimpleNamespace(config_entries=FakeConfigEntries([SimpleNamespace(runtime_data=None)]))

    info = asyncio.run(system_health_info(hass))

    assert info == {"configured_entries": 1, "loaded_entries": 0}


def test_latest_value_rejects_malformed_history() -> None:
    assert _latest_value(None, "duration_ms") is None
    assert _latest_value([], "duration_ms") is None
    assert _latest_value(["invalid"], "duration_ms") is None
    assert _latest_value([{"duration_ms": 4.0}], "duration_ms") == 4.0
    assert _latest_haeo_metric(None, "duration_ms") is None
    assert _latest_haeo_metric([{"duration_ms": 3.0}], "duration_ms") == 3.0
    assert _latest_haeo_metric([{"baseline": {"status": "ready"}}], "duration_ms") is None
    assert _latest_haeo_metric([{"baseline": {"status": "ready"}}], "status") == "ready"
