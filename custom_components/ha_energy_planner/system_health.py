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
    loaded_entries = [entry for entry in entries if getattr(entry, "runtime_data", None) is not None]
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
            "last_refresh_duration_ms": (getattr(coordinator, "last_refresh_metadata", None) or {}).get(
                "duration_ms"
            ),
            "latest_haeo_duration_ms": _latest_haeo_metric(store_data.get("haeo_runs"), "duration_ms"),
            "latest_haeo_cache_hit": _latest_haeo_metric(store_data.get("haeo_runs"), "cache_hit"),
            "latest_ai_status": _latest_status(store_data.get("ai_recommendations")),
        }
    )
    refresh_metrics = getattr(coordinator, "refresh_metrics", None)
    if refresh_metrics is not None:
        info["refresh_metrics"] = refresh_metrics
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


def _latest_value(value: Any, key: str) -> Any:
    """Return a compact value from the latest stored dictionary."""
    if not isinstance(value, list) or not value or not isinstance(value[-1], dict):
        return None
    return value[-1].get(key)


def _latest_haeo_metric(value: Any, key: str) -> Any:
    """Return a combined metric from the latest HAEO baseline/second pass."""
    if not isinstance(value, list) or not value or not isinstance(value[-1], dict):
        return None
    phases = [value[-1].get(name) for name in ("baseline", "second_pass")]
    phase_values = [phase.get(key) for phase in phases if isinstance(phase, dict) and phase.get(key) is not None]
    if not phase_values:
        return value[-1].get(key)
    if key == "duration_ms":
        return round(sum(float(item) for item in phase_values), 3)
    if key == "cache_hit":
        return any(bool(item) for item in phase_values)
    return phase_values[-1]
