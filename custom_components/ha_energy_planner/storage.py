"""Persistent storage helpers for Energy Planner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
        entry = _audit_entry(outcome)
        if audit and _deduplicable_outcome(entry) and _same_audit_outcome(audit[-1], entry):
            previous = dict(audit[-1])
            previous["occurrence_count"] = int(previous.get("occurrence_count", 1)) + 1
            previous["last_attempted_at"] = entry["attempted_at"]
            audit[-1] = previous
        else:
            audit.append(entry)
        self.data["execution_audit"] = audit[-100:]
        outcomes = list(self.data.get("outcomes", []))
        serialized = to_jsonable(outcome)
        if outcomes and _deduplicable_outcome(serialized) and _same_audit_outcome(outcomes[-1], serialized):
            previous_outcome = dict(outcomes[-1])
            previous_outcome["occurrence_count"] = int(previous_outcome.get("occurrence_count", 1)) + 1
            previous_outcome["last_attempted_at"] = serialized.get("attempted_at")
            outcomes[-1] = previous_outcome
        else:
            outcomes.append(serialized)
        self.data["outcomes"] = outcomes[-100:]
        await self._async_save()

    async def async_save_overrides(self, overrides: list[Override]) -> None:
        """Persist active overrides."""
        await self._async_set_if_changed("overrides", overrides)

    async def async_add_forecast_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Persist a compact forecast snapshot for replay."""
        snapshots = list(self.data.get("forecast_snapshots", []))
        snapshots.append(to_jsonable(snapshot))
        # Time-based retention preserves day-ahead evidence across refresh
        # storms; the hard cap bounds malformed/atypical records.
        self.data["forecast_snapshots"] = _retain_by_time(snapshots, hours=48, hard_cap=2048)
        await self._async_save()

    async def async_add_dry_run_comparison(self, comparison: dict[str, Any]) -> None:
        """Persist compact dry-run comparison metadata."""
        comparisons = list(self.data.get("dry_run_comparisons", []))
        item = to_jsonable(comparison)
        if comparisons and _same_dry_run_comparison(comparisons[-1], item):
            previous = dict(comparisons[-1])
            previous["occurrence_count"] = int(previous.get("occurrence_count", 1)) + 1
            previous["last_created_at"] = item.get("created_at")
            comparisons[-1] = previous
        else:
            comparisons.append(item)
        self.data["dry_run_comparisons"] = _retain_by_time(comparisons, hours=24 * 7, hard_cap=1024)
        await self._async_save()

    async def async_save_forecast_calibration(self, model: dict[str, Any]) -> None:
        """Persist compact forecast calibration statistics."""
        await self._async_set_if_changed("forecast_calibration", model)

    async def async_add_haeo_run(self, run: dict[str, Any]) -> None:
        """Persist compact HAEO run metadata."""
        runs = list(self.data.get("haeo_runs", []))
        runs.append(to_jsonable(run))
        self.data["haeo_runs"] = _retain_by_time(runs, hours=48, hard_cap=2048)
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
    audit = {
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
    if isinstance(entry.get("desired_state"), dict):
        audit["desired_state"] = _bounded_mapping(entry["desired_state"])
    return audit


def _same_audit_outcome(previous: object, current: object) -> bool:
    """Return whether adjacent audit outcomes carry the same decision."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    # Generated plan/action identifiers and timestamps do not change the
    # material execution decision and must not defeat coalescing.
    keys = (
        "asset",
        "kind",
        "desired_state",
        "result",
        "reason",
        "service_target",
        "pre_state",
        "post_state",
    )
    return all(previous.get(key) == current.get(key) for key in keys)


def _deduplicable_outcome(value: object) -> bool:
    """Return whether repeated outcomes are safe to coalesce."""
    return isinstance(value, dict) and value.get("result") == "skipped"


def _same_dry_run_comparison(previous: object, current: object) -> bool:
    """Return whether adjacent dry-run comparisons are materially identical."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    return _dry_run_signature(previous) == _dry_run_signature(current)


def _dry_run_signature(item: dict[str, Any]) -> dict[str, Any]:
    """Return material dry-run data without generated IDs and timestamps."""
    next_action = item.get("next_action")
    if isinstance(next_action, dict):
        normalized_action: object = {
            key: next_action.get(key)
            for key in (
                "asset",
                "kind",
                "desired_state",
                "hard_constraints",
                "reason_codes",
                "expected_cost_delta",
                "confidence",
                "requires_haeo_plan_id",
            )
        }
    else:
        normalized_action = next_action
    recent_outcomes = item.get("recent_outcomes")
    normalized_outcomes = []
    if isinstance(recent_outcomes, list):
        for outcome in recent_outcomes:
            if not isinstance(outcome, dict):
                continue
            normalized_outcomes.append(
                {
                    key: outcome.get(key)
                    for key in (
                        "asset",
                        "kind",
                        "desired_state",
                        "result",
                        "reason",
                        "service_target",
                        "pre_state",
                        "post_state",
                    )
                }
            )
    return {
        "planned_action_count": item.get("planned_action_count"),
        "next_action": normalized_action,
        "estimated_daily_cost": item.get("estimated_daily_cost"),
        "recent_outcomes": normalized_outcomes,
    }


def _bounded_mapping(value: object) -> dict[str, Any]:
    """Bound stored state maps so audit entries stay compact."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in list(value.items())[:12]}


def _retain_by_time(records: list[Any], *, hours: int, hard_cap: int) -> list[dict[str, Any]]:
    """Retain timestamped records for a duration with a defensive hard cap."""
    records = [item for item in records if isinstance(item, dict)]
    timestamps = [_record_timestamp(item) for item in records]
    valid = [item for item in timestamps if item is not None]
    if not valid:
        return records[-hard_cap:]
    cutoff = max(valid) - timedelta(hours=hours)
    retained = [
        item for item, timestamp in zip(records, timestamps, strict=True) if timestamp is None or timestamp >= cutoff
    ]
    return retained[-hard_cap:]


def _record_timestamp(record: Any) -> datetime | None:
    """Return a normalized record timestamp from supported audit fields."""
    if not isinstance(record, dict):
        return None
    value = record.get("created_at", record.get("attempted_at"))
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
