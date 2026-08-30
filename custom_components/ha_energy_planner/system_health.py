"""System health for Energy Planner."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .entry_data import combined_entry_data
from .models import InputHealth
from .type_defs import EnergyPlannerConfigEntry

_INPUT_SECTION_PREFIXES = (
    ("amber_", "pv_", "baseline_load_", "battery_soc_", "carbon_intensity_"),
    ("daikin_", "weather_", "climate_"),
    ("person_",),
    ("enphase_",),
    ("ai_",),
    ("ev_",),
)


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

    ordered_entries = sorted(
        loaded_entries,
        key=lambda item: str(getattr(item, "entry_id", "")),
    )
    summaries = [
        (str(getattr(entry, "entry_id", "")) or f"entry_{index + 1}", _entry_health_summary(entry))
        for index, entry in enumerate(ordered_entries)
    ]
    if len(summaries) == 1:
        _, summary = summaries[0]
        info.update(summary)
        coordinator = ordered_entries[0].runtime_data
        refresh_metrics = getattr(coordinator, "refresh_metrics", None)
        if refresh_metrics is not None:
            info["refresh_metrics"] = refresh_metrics
        return info

    worst_entry_id, worst_summary = max(
        summaries,
        key=lambda item: (_health_severity(item[1].get("plan_health")), item[0]),
    )
    visible = summaries[:8]
    if worst_entry_id not in {entry_id for entry_id, _summary in visible}:
        visible[-1] = (worst_entry_id, worst_summary)
    info.update(
        {
            "planner_enabled": any(bool(summary["planner_enabled"]) for _, summary in summaries),
            "dry_run": all(bool(summary["dry_run"]) for _, summary in summaries),
            "data_healthy": all(bool(summary["data_healthy"]) for _, summary in summaries),
            "plan_status": worst_summary["plan_status"],
            "plan_mode": worst_summary["plan_mode"],
            "plan_health": worst_summary["plan_health"],
            "worst_entry_id": worst_entry_id,
            "unhealthy_entry_count": sum(
                not bool(summary["data_healthy"]) for _, summary in summaries
            ),
            "entries": {entry_id: summary for entry_id, summary in visible},
            "entries_truncated": max(len(summaries) - len(visible), 0),
        }
    )
    return info


def _entry_health_summary(entry: EnergyPlannerConfigEntry) -> dict[str, Any]:
    """Return deterministic non-sensitive health for one loaded planner entry."""
    coordinator = entry.runtime_data
    plan = coordinator.data
    store_data = dict(coordinator.store.data)
    refresh_metrics = getattr(coordinator, "refresh_metrics", None)
    refresh_metrics = dict(refresh_metrics) if isinstance(refresh_metrics, dict) else {}
    entry_data = combined_entry_data(entry)
    return {
        "planner_enabled": bool(coordinator.options.get("planner_enabled", False)),
        "dry_run": bool(coordinator.options.get("dry_run", True)),
        "data_healthy": bool(plan and plan.health == InputHealth.HEALTHY),
        "plan_status": None if plan is None else plan.status,
        "plan_mode": None if plan is None else str(plan.mode),
        "plan_health": None if plan is None else str(plan.health),
        "configured_input_sections": sum(
            any(value and key.startswith(prefixes) for key, value in entry_data.items())
            for prefixes in _INPUT_SECTION_PREFIXES
        ),
        "last_refresh_duration_ms": (getattr(coordinator, "last_refresh_metadata", None) or {}).get(
            "duration_ms"
        ),
        "refreshes_per_hour": refresh_metrics.get(
            "refreshes_per_hour", refresh_metrics.get("refreshes_last_hour")
        ),
        "refresh_trigger_counts": refresh_metrics.get("trigger_counts", {}),
        "last_refresh_trigger": refresh_metrics.get("last_trigger"),
        "skipped_refresh_count": refresh_metrics.get(
            "skipped_count", refresh_metrics.get("fingerprint_skipped", 0)
        ),
        "coalesced_refresh_count": refresh_metrics.get(
            "coalesced_count", refresh_metrics.get("coalesced", 0)
        ),
        "refresh_phase_durations_ms": refresh_metrics.get("phase_durations_ms", {}),
        "usable_optimization_horizon_hours": (
            None if plan is None else getattr(plan, "estimated_cost_horizon_hours", None)
        ),
        "latest_ai_status": _latest_status(store_data.get("ai_recommendations")),
    }


def _health_severity(value: Any) -> int:
    """Return a stable ordering where missing health is the worst state."""
    return {"healthy": 0, "degraded": 1, "unsafe": 2}.get(str(value), 3)


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
