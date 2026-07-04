"""System health for Energy Planner."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .models import InputHealth


@callback
def async_register(
    hass: HomeAssistant,
    register: system_health.SystemHealthRegistration,
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Return compact non-sensitive system health information."""
    entries = hass.config_entries.async_entries(DOMAIN)
    loaded_entries = [
        entry
        for entry in entries
        if getattr(entry, "runtime_data", None) is not None
    ]
    info: dict[str, Any] = {
        "configured_entries": len(entries),
        "loaded_entries": len(loaded_entries),
    }
    if not loaded_entries:
        return info

    entry = loaded_entries[0]
    coordinator = entry.runtime_data
    plan = coordinator.data
    store_data = dict(coordinator.store.data)
    info.update(
        {
            "planner_enabled": bool(coordinator.options.get("planner_enabled", False)),
            "dry_run": bool(coordinator.options.get("dry_run", True)),
            "data_healthy": bool(plan and plan.health == InputHealth.HEALTHY),
            "plan_status": None if plan is None else plan.status,
            "plan_mode": None if plan is None else str(plan.mode),
            "plan_health": None if plan is None else str(plan.health),
            "configured_input_groups": len(getattr(entry, "subentries", {})),
            "latest_haeo_status": _latest_status(store_data.get("haeo_runs")),
            "latest_ai_status": _latest_status(store_data.get("ai_recommendations")),
        }
    )
    return info


def _latest_status(value: Any) -> str | None:
    """Return the latest stored status string from a bounded store list."""
    if not isinstance(value, list) or not value:
        return None
    latest = value[-1]
    if not isinstance(latest, dict):
        return None
    status = latest.get("status")
    if status is not None:
        return str(status)
    baseline = latest.get("baseline")
    if isinstance(baseline, dict) and baseline.get("status") is not None:
        return str(baseline["status"])
    return None
