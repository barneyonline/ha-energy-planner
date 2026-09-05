"""Persistent storage helpers for Energy Planner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from .const import STORE_KEY, STORE_VERSION
from .durable_storage import DurableStore as Store
from .models import ActionOutcome, EnergyPlan, Override, to_jsonable

_LIST_FIELDS = {
    "ai_recommendations",
    "execution_audit",
    "forecast_snapshots",
    "dry_run_comparisons",
    "overrides",
}

_DICT_FIELDS = {
    "ai_last_attempt",
    "command_rate_limits",
    "discovery",
    "ev_grid_reservation",
    "ev_charge_calibration",
    "forecast_calibration",
    "built_in_load_forecast",
    "load_source_outage",
    "ownership",
    "control_pause",
    "production",
    "thermal_model",
}

_LEGACY_MIGRATION_MARKER = "_entry_store_migrated_to"
_FORECAST_SNAPSHOT_BUCKET_MINUTES = 30
_FORECAST_SNAPSHOT_HARD_CAP = 128
_DRY_RUN_COMPARISON_BUCKET_MINUTES = 30
_DRY_RUN_COMPARISON_HARD_CAP = 384


class PlannerStore:
    """Versioned Store wrapper."""

    def __init__(self, hass: HomeAssistant, entry_id: str | None = None, *, legacy_fallback: bool = False) -> None:
        """Initialize storage."""
        storage_key = STORE_KEY if entry_id is None else f"{STORE_KEY}_{entry_id}"
        self._entry_id = entry_id
        self._store: Store = Store(
            hass,
            STORE_VERSION,
            storage_key,
            serialize_in_event_loop=False,
        )
        self._legacy_store: Store | None = (
            Store(
                hass,
                STORE_VERSION,
                STORE_KEY,
                serialize_in_event_loop=False,
            )
            if entry_id is not None and legacy_fallback
            else None
        )
        self.data: dict[str, Any] = _default_data()
        self._save_delay_depth = 0
        self._mutation_generation = 0
        self._saved_generation = 0
        self._save_lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load persisted state."""
        loaded = await self._store.async_load()
        if self._legacy_store is not None:
            legacy_loaded = await self._legacy_store.async_load()
            if legacy_loaded and not legacy_loaded.get(_LEGACY_MIGRATION_MARKER):
                if loaded is None:
                    loaded = legacy_loaded
                    await self._store.async_save(legacy_loaded)
                marked_legacy = dict(legacy_loaded)
                marked_legacy[_LEGACY_MIGRATION_MARKER] = self._entry_id
                await self._legacy_store.async_save(marked_legacy)
        if loaded:
            self.data = _normalize_loaded_data(loaded)
            if self.data != loaded:
                await self._store.async_save(self.data)

    async def async_save_plan(self, plan: EnergyPlan) -> None:
        """Persist the compact active plan."""
        self.data["active_plan"] = to_jsonable(plan)
        await self._async_save()

    async def async_remove_if_safe(self) -> bool:
        """Delete this entry's data only when raw recovery evidence is resolved."""
        loaded = await self._store.async_load()
        if loaded is not None:
            ownership = loaded.get("ownership", {})
            reservation = loaded.get("ev_grid_reservation", {})
            if not isinstance(ownership, dict) or ownership:
                return False
            if not isinstance(reservation, dict) or (reservation and reservation.get("active") is not False):
                return False
        await self._store.async_remove()
        return True

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
        await self._async_save()

    async def async_save_overrides(self, overrides: list[Override]) -> None:
        """Persist active overrides."""
        await self._async_set_if_changed("overrides", overrides)

    async def async_add_forecast_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Persist a compact forecast snapshot for replay."""
        snapshots = list(self.data.get("forecast_snapshots", []))
        item = _inherit_forecast_bucket_metadata(
            snapshots,
            to_jsonable(snapshot),
            minutes=_FORECAST_SNAPSHOT_BUCKET_MINUTES,
        )
        snapshots = _upsert_latest_time_bucket(
            snapshots,
            item,
            minutes=_FORECAST_SNAPSHOT_BUCKET_MINUTES,
        )
        # One snapshot per half-hour still provides complete five-minute
        # calibration targets because each snapshot carries the first hour of
        # five-minute training leads. It avoids rewriting hundreds of near-
        # duplicate previews on every planner interval.
        self.data["forecast_snapshots"] = _retain_by_time(
            snapshots,
            hours=48,
            hard_cap=_FORECAST_SNAPSHOT_HARD_CAP,
        )
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
            comparisons = _upsert_latest_time_bucket(
                comparisons,
                item,
                minutes=_DRY_RUN_COMPARISON_BUCKET_MINUTES,
            )
        self.data["dry_run_comparisons"] = _retain_by_time(
            comparisons,
            hours=24 * 7,
            hard_cap=_DRY_RUN_COMPARISON_HARD_CAP,
        )
        await self._async_save()

    async def async_save_forecast_calibration(self, model: dict[str, Any]) -> None:
        """Persist compact forecast calibration statistics."""
        await self._async_set_if_changed("forecast_calibration", model)

    async def async_save_builtin_load_forecast(self, model: dict[str, Any]) -> None:
        """Persist the aggregate Recorder-trained load model."""
        await self._async_set_if_changed("built_in_load_forecast", model)

    async def async_save_load_source_outage(self, outage: dict[str, Any]) -> None:
        """Persist the start of a continuous unusable household-load period."""
        await self._async_set_if_changed("load_source_outage", outage)

    async def async_save_ai_attempt(self, attempt: dict[str, Any]) -> None:
        """Persist sanitized call admission independently of result acceptance."""
        await self._async_set_if_changed("ai_last_attempt", attempt)
        await self.async_flush()

    async def async_add_ai_recommendation(self, recommendation: dict[str, Any]) -> None:
        """Persist compact AI recommendation metadata."""
        recommendations = list(self.data.get("ai_recommendations", []))
        recommendations.append(to_jsonable(recommendation))
        self.data["ai_recommendations"] = recommendations[-50:]
        await self._async_save()

    async def async_attach_ai_to_forecast_snapshot(self, plan_id: str, metadata: dict[str, Any]) -> None:
        """Attach completed background AI metadata to its forecast snapshot."""
        snapshots = list(self.data.get("forecast_snapshots", []))
        for index in range(len(snapshots) - 1, -1, -1):
            snapshot = snapshots[index]
            if not isinstance(snapshot, dict):
                continue
            bucket_plan_ids = snapshot.get("bucket_plan_ids", [])
            if snapshot.get("plan_id") != plan_id and not (
                isinstance(bucket_plan_ids, list) and plan_id in bucket_plan_ids
            ):
                continue
            updated = dict(snapshot)
            updated["ai"] = to_jsonable(metadata)
            updated["ai_plan_id"] = plan_id
            snapshots[index] = updated
            self.data["forecast_snapshots"] = snapshots
            await self._async_save()
            return

    async def async_save_discovery(self, report: dict[str, Any]) -> None:
        """Persist latest non-commanding discovery report."""
        await self._async_set_if_changed("discovery", report)

    async def async_save_ev_charge_calibration(self, model: dict[str, Any]) -> None:
        """Persist the compact Recorder-trained EV charging calibration."""
        await self._async_set_if_changed("ev_charge_calibration", model)

    async def async_save_thermal_model(self, thermal_model: dict[str, Any]) -> None:
        """Persist compact HVAC thermal model state."""
        await self._async_set_if_changed("thermal_model", thermal_model)

    async def async_save_ownership(self, ownership: dict[str, Any]) -> None:
        """Persist planner ownership state."""
        await self._async_set_if_changed("ownership", ownership)

    async def async_save_ev_grid_reservation(self, reservation: dict[str, Any]) -> None:
        """Persist the conservative active EV grid reservation."""
        await self._async_set_if_changed("ev_grid_reservation", reservation)

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
            if self._save_delay_depth == 0 and self._is_dirty:
                await self._async_flush()

    async def _async_save(self) -> None:
        """Mark the current mutation dirty and persist it when not delayed."""
        self._mutation_generation += 1
        if self._save_delay_depth:
            return
        await self._async_flush()

    async def async_flush(self) -> None:
        """Force all observed mutations to durable storage."""
        await self._async_flush(force=True)

    async def _async_flush(self, *, force: bool = False) -> None:
        """Serialize Store writes until every observed mutation is durable."""
        async with self._save_lock:
            while self._is_dirty:
                if self._save_delay_depth and not force:
                    return
                generation = self._mutation_generation
                # Every mutation in this wrapper replaces a top-level value.
                # Capturing the root mapping therefore gives the Store executor
                # an immutable generation while later event-loop mutations build
                # and install new lists/dicts.
                await self._store.async_save(dict(self.data))
                # A writer may have mutated data while this save was in flight.
                # Acknowledge only the generation captured before the await so
                # the loop performs another write for any later mutation.
                self._saved_generation = generation

    @property
    def _is_dirty(self) -> bool:
        """Return whether in-memory state is newer than the last successful save."""
        return self._saved_generation < self._mutation_generation

    async def _async_set_if_changed(self, key: str, value: Any) -> None:
        jsonable = to_jsonable(value)
        if self.data.get(key) == jsonable:
            if self._is_dirty:
                await self._async_flush()
            return
        self.data[key] = jsonable
        await self._async_save()


def _default_data() -> dict[str, Any]:
    return {
        "active_plan": None,
        "execution_audit": [],
        "audit_history_version": 1,
        "ownership": {},
        "overrides": [],
        "forecast_snapshots": [],
        "dry_run_comparisons": [],
        "ev_grid_reservation": {},
        "ev_charge_calibration": {},
        "forecast_calibration": {},
        "built_in_load_forecast": {},
        "load_source_outage": {},
        "discovery": {},
        "command_rate_limits": {},
        "production": {},
        "control_pause": {},
        "thermal_model": {},
        "ai_recommendations": [],
        "ai_last_attempt": {},
    }


def _normalize_loaded_data(loaded: dict[str, Any]) -> dict[str, Any]:
    data = _default_data()
    data.update(loaded)
    data.pop("haeo_runs", None)
    data.pop("trip_history", None)
    data["execution_audit"] = [_audit_entry(record) for record in audit_records(loaded)][-100:]
    data["audit_history_version"] = 1
    data.pop("outcomes", None)
    for key in _LIST_FIELDS:
        if not isinstance(data.get(key), list):
            data[key] = []
    for key in _DICT_FIELDS:
        if not isinstance(data.get(key), dict):
            data[key] = {}
    active_plan = data.get("active_plan")
    if active_plan is not None and not isinstance(active_plan, dict):
        data["active_plan"] = None
    elif isinstance(active_plan, dict):
        actions = active_plan.get("actions")
        if isinstance(actions, list):
            normalized_actions = []
            for action in actions:
                if not isinstance(action, dict):
                    normalized_actions.append(action)
                    continue
                normalized_action = dict(action)
                normalized_action.pop("requires_haeo_plan_id", None)
                normalized_actions.append(normalized_action)
            active_plan = dict(active_plan)
            active_plan["actions"] = normalized_actions
            data["active_plan"] = active_plan
    return data


def audit_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Read canonical history, accepting outcome-only stores during migration."""
    audit = data.get("execution_audit")
    records = [item for item in audit if isinstance(item, dict)] if isinstance(audit, list) else []
    if records:
        return records
    legacy = data.get("outcomes")
    return [item for item in legacy if isinstance(item, dict)] if isinstance(legacy, list) else []


def _audit_entry(outcome: ActionOutcome | dict[str, Any]) -> dict[str, Any]:
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
    if "occurrence_count" in entry:
        count = entry["occurrence_count"]
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            audit["occurrence_count"] = count
    if isinstance(entry.get("last_attempted_at"), str):
        audit["last_attempted_at"] = entry["last_attempted_at"]
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


def _upsert_latest_time_bucket(
    records: list[Any],
    item: dict[str, Any],
    *,
    minutes: int,
) -> list[Any]:
    """Keep only the latest record in a fixed UTC time bucket."""
    if not records:
        return [item]
    previous_timestamp = _record_timestamp(records[-1])
    current_timestamp = _record_timestamp(item)
    if previous_timestamp is None or current_timestamp is None:
        return [*records, item]
    bucket_seconds = max(int(minutes), 1) * 60
    if int(previous_timestamp.timestamp()) // bucket_seconds != int(current_timestamp.timestamp()) // bucket_seconds:
        return [*records, item]
    return [*records[:-1], item]


def _inherit_forecast_bucket_metadata(
    records: list[Any],
    item: dict[str, Any],
    *,
    minutes: int,
) -> dict[str, Any]:
    """Carry bounded plan provenance and completed AI data across bucket replacement."""
    if not records or not isinstance(records[-1], dict):
        return item
    previous = records[-1]
    previous_timestamp = _record_timestamp(previous)
    current_timestamp = _record_timestamp(item)
    if previous_timestamp is None or current_timestamp is None:
        return item
    bucket_seconds = max(int(minutes), 1) * 60
    if int(previous_timestamp.timestamp()) // bucket_seconds != int(current_timestamp.timestamp()) // bucket_seconds:
        return item

    updated = dict(item)
    current_plan_id = str(item.get("plan_id") or "")
    previous_bucket_plan_ids = previous.get("bucket_plan_ids", [])
    bucket_plan_ids = (
        [
            str(plan_id)
            for plan_id in previous_bucket_plan_ids
            if str(plan_id) and str(plan_id) != current_plan_id
        ]
        if isinstance(previous_bucket_plan_ids, list)
        else []
    )
    previous_plan_id = str(previous.get("plan_id") or "")
    if previous_plan_id and previous_plan_id != current_plan_id:
        bucket_plan_ids.append(previous_plan_id)
    if bucket_plan_ids:
        updated["bucket_plan_ids"] = list(dict.fromkeys(bucket_plan_ids))[-12:]

    bucket_actions = _merge_forecast_bucket_actions(
        previous.get("actions"),
        updated.get("actions"),
    )
    if bucket_actions:
        updated["actions"] = bucket_actions

    bucket_trip_history = _merge_forecast_bucket_trip_history(
        previous.get("trip_history"),
        updated.get("trip_history"),
    )
    if bucket_trip_history:
        updated["trip_history"] = bucket_trip_history

    previous_ai = previous.get("ai")
    if not updated.get("ai") and isinstance(previous_ai, dict) and previous_ai:
        updated["ai"] = previous_ai
        updated["ai_plan_id"] = str(previous.get("ai_plan_id") or previous_plan_id)
    return updated


def _merge_forecast_bucket_actions(previous: Any, current: Any) -> list[dict[str, Any]]:
    """Retain bounded, materially distinct action evidence within one forecast bucket."""
    actions: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    previous_actions = previous if isinstance(previous, list) else []
    current_actions = current if isinstance(current, list) else []
    for raw_action in [*previous_actions, *current_actions]:
        if not isinstance(raw_action, dict):
            continue
        action = dict(raw_action)
        signature = {
            key: action.get(key)
            for key in (
                "asset",
                "kind",
                "desired_state",
                "hard_constraints",
                "reason_codes",
                "expected_cost_delta",
                "confidence",
            )
        }
        try:
            existing_index = signatures.index(signature)
        except ValueError:
            signatures.append(signature)
            actions.append(action)
        else:
            actions[existing_index] = action
    if len(actions) <= 12:
        return actions
    selected_indexes = set(
        sorted(
            range(len(actions)),
            key=lambda index: (_forecast_action_retention_priority(actions[index]), index),
        )[-12:]
    )
    return [action for index, action in enumerate(actions) if index in selected_indexes]


def _forecast_action_retention_priority(action: dict[str, Any]) -> int:
    """Prioritize scarce or active EV allocation evidence during bucket compaction."""
    desired_state = action.get("desired_state")
    if not isinstance(desired_state, dict):
        return 0
    allocated_slots = desired_state.get("allocated_slots")
    if not isinstance(allocated_slots, list):
        return 0
    if any(
        isinstance(slot, dict)
        and isinstance(slot.get("import_price"), int | float)
        and not isinstance(slot.get("import_price"), bool)
        and slot["import_price"] < 0
        for slot in allocated_slots
    ):
        return 2
    return 1 if desired_state.get("charging_required_now") and allocated_slots else 0


def _merge_forecast_bucket_trip_history(previous: Any, current: Any) -> dict[str, Any]:
    """Preserve one successful Recorder import summary across later bucket refreshes."""
    previous_summary = dict(previous) if isinstance(previous, dict) else {}
    current_summary = dict(current) if isinstance(current, dict) else {}
    if not previous_summary:
        return current_summary
    if not current_summary:
        return previous_summary
    previous_imported = previous_summary.get("recorder_import_reason") == "recorder_imported"
    current_imported = current_summary.get("recorder_import_reason") == "recorder_imported"
    if previous_imported and not current_imported:
        merged = previous_summary
        merged["latest_recorder_import_reason"] = current_summary.get("recorder_import_reason")
    else:
        merged = current_summary
    record_counts = [
        value
        for value in (previous_summary.get("record_count"), current_summary.get("record_count"))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    merged["record_count"] = max(record_counts, default=0)
    return merged


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
