"""Persistent storage helpers for Energy Planner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORE_KEY, STORE_VERSION
from .models import ActionOutcome, EnergyPlan, Override, to_jsonable

_LIST_FIELDS = {
    "ai_recommendations",
    "execution_audit",
    "forecast_snapshots",
    "dry_run_comparisons",
    "haeo_runs",
    "outcomes",
    "overrides",
}

_DICT_FIELDS = {
    "command_rate_limits",
    "discovery",
    "forecast_calibration",
    "ownership",
    "control_pause",
    "production",
    "thermal_model",
    "trip_history",
}


class PlannerStore:
    """Versioned Store wrapper."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize storage."""
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self.data: dict[str, Any] = _default_data()
        self._save_delay_depth = 0
        self._save_pending = False

    async def async_load(self) -> None:
        """Load persisted state."""
        loaded = await self._store.async_load()
        if loaded:
            self.data = _normalize_loaded_data(loaded)

    async def async_save_plan(self, plan: EnergyPlan) -> None:
        """Persist the compact active plan."""
        self.data["active_plan"] = to_jsonable(plan)
        await self._async_save()

    async def async_add_outcome(self, outcome: ActionOutcome) -> None:
        """Append an execution outcome."""
        audit = list(self.data.get("execution_audit", []))
        audit.append(_audit_entry(outcome))
        self.data["execution_audit"] = audit[-100:]
        outcomes = list(self.data.get("outcomes", []))
        outcomes.append(to_jsonable(outcome))
        self.data["outcomes"] = outcomes[-100:]
        await self._async_save()

    async def async_save_overrides(self, overrides: list[Override]) -> None:
        """Persist active overrides."""
        await self._async_set_if_changed("overrides", overrides)

    async def async_add_forecast_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Persist a compact forecast snapshot for replay."""
        snapshots = list(self.data.get("forecast_snapshots", []))
        snapshots.append(to_jsonable(snapshot))
        self.data["forecast_snapshots"] = snapshots[-96:]
        await self._async_save()

    async def async_add_dry_run_comparison(self, comparison: dict[str, Any]) -> None:
        """Persist compact dry-run comparison metadata."""
        comparisons = list(self.data.get("dry_run_comparisons", []))
        comparisons.append(to_jsonable(comparison))
        self.data["dry_run_comparisons"] = comparisons[-96:]
        await self._async_save()

    async def async_save_forecast_calibration(self, model: dict[str, Any]) -> None:
        """Persist compact forecast calibration statistics."""
        await self._async_set_if_changed("forecast_calibration", model)

    async def async_add_haeo_run(self, run: dict[str, Any]) -> None:
        """Persist compact HAEO run metadata."""
        runs = list(self.data.get("haeo_runs", []))
        runs.append(to_jsonable(run))
        self.data["haeo_runs"] = runs[-100:]
        await self._async_save()

    async def async_add_ai_recommendation(self, recommendation: dict[str, Any]) -> None:
        """Persist compact AI recommendation metadata."""
        recommendations = list(self.data.get("ai_recommendations", []))
        recommendations.append(to_jsonable(recommendation))
        self.data["ai_recommendations"] = recommendations[-50:]
        await self._async_save()

    async def async_save_discovery(self, report: dict[str, Any]) -> None:
        """Persist latest non-commanding discovery report."""
        await self._async_set_if_changed("discovery", report)

    async def async_save_trip_history(self, trip_history: dict[str, Any]) -> None:
        """Persist compact EV trip history."""
        await self._async_set_if_changed("trip_history", trip_history)

    async def async_save_thermal_model(self, thermal_model: dict[str, Any]) -> None:
        """Persist compact HVAC thermal model state."""
        await self._async_set_if_changed("thermal_model", thermal_model)

    async def async_save_ownership(self, ownership: dict[str, Any]) -> None:
        """Persist planner ownership state."""
        await self._async_set_if_changed("ownership", ownership)

    async def async_save_command_rate_limits(self, limits: dict[str, Any]) -> None:
        """Persist command rate-limit timestamps."""
        await self._async_set_if_changed("command_rate_limits", limits)

    async def async_save_production(self, production: dict[str, Any]) -> None:
        """Persist production arming state."""
        await self._async_set_if_changed("production", production)

    async def async_save_control_pause(self, pause: dict[str, Any]) -> None:
        """Persist active control pause state."""
        await self._async_set_if_changed("control_pause", pause)

    async def async_clear_ownership(self) -> None:
        """Clear planner-owned state for dry-run restore."""
        await self._async_set_if_changed("ownership", {})

    @asynccontextmanager
    async def async_delay_save(self) -> Any:
        """Coalesce multiple Store writes into one disk write."""
        self._save_delay_depth += 1
        try:
            yield
        finally:
            self._save_delay_depth -= 1
            if self._save_delay_depth == 0 and self._save_pending:
                self._save_pending = False
                await self._store.async_save(self.data)

    async def _async_save(self) -> None:
        if self._save_delay_depth:
            self._save_pending = True
            return
        await self._store.async_save(self.data)

    async def _async_set_if_changed(self, key: str, value: Any) -> None:
        jsonable = to_jsonable(value)
        if self.data.get(key) == jsonable:
            return
        self.data[key] = jsonable
        await self._async_save()


def _default_data() -> dict[str, Any]:
    return {
        "active_plan": None,
        "execution_audit": [],
        "outcomes": [],
        "ownership": {},
        "overrides": [],
        "forecast_snapshots": [],
        "dry_run_comparisons": [],
        "forecast_calibration": {},
        "haeo_runs": [],
        "discovery": {},
        "command_rate_limits": {},
        "production": {},
        "control_pause": {},
        "trip_history": {},
        "thermal_model": {},
        "ai_recommendations": [],
    }


def _normalize_loaded_data(loaded: dict[str, Any]) -> dict[str, Any]:
    data = _default_data()
    data.update(loaded)
    for key in _LIST_FIELDS:
        if not isinstance(data.get(key), list):
            data[key] = []
    for key in _DICT_FIELDS:
        if not isinstance(data.get(key), dict):
            data[key] = {}
    active_plan = data.get("active_plan")
    if active_plan is not None and not isinstance(active_plan, dict):
        data["active_plan"] = None
    return data


def _audit_entry(outcome: ActionOutcome) -> dict[str, Any]:
    """Return a compact, redacted execution audit entry."""
    entry = to_jsonable(outcome)
    return {
        "attempted_at": entry.get("attempted_at"),
        "plan_id": entry.get("plan_id"),
        "action_id": entry.get("action_id"),
        "asset": entry.get("asset"),
        "kind": entry.get("kind"),
        "result": entry.get("result"),
        "reason": entry.get("reason"),
        "service_target": entry.get("service_target"),
        "pre_state": _bounded_mapping(entry.get("pre_state")),
        "post_state": _bounded_mapping(entry.get("post_state")),
    }


def _bounded_mapping(value: object) -> dict[str, Any]:
    """Bound stored state maps so audit entries stay compact."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in list(value.items())[:12]}
