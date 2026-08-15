"""Shared fail-closed safety-state parsing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

DRY_RUN_READY_CYCLES_REQUIRED = 3
_MAX_REASONABLE_LEGACY_READY_CYCLES = 10_000
_CONTROL_AREA_ASSETS = {"ev": "ev", "hvac": "daikin", "enphase": "enphase"}


@dataclass(frozen=True, slots=True)
class ProductionSafetyState:
    """Defensively parsed production-gate state."""

    raw: dict[str, Any]
    armed: bool
    dry_run_ready_cycles: int
    dry_run_evidence_fingerprint: str | None


def strict_bool(value: Any, *, default: bool = False) -> bool:
    """Accept only actual booleans, falling back safely for corrupt values."""
    return value if isinstance(value, bool) else default


def parse_production_state(value: Any) -> ProductionSafetyState:
    """Return bounded fail-closed production state from persisted data."""
    raw = dict(value) if isinstance(value, Mapping) else {}
    cycles_value = raw.get("dry_run_ready_cycles", 0)
    cycles = 0
    if (
        isinstance(cycles_value, int)
        and not isinstance(cycles_value, bool)
        and 0 <= cycles_value <= _MAX_REASONABLE_LEGACY_READY_CYCLES
    ):
        cycles = min(cycles_value, DRY_RUN_READY_CYCLES_REQUIRED)
    fingerprint_value = raw.get("dry_run_evidence_fingerprint")
    fingerprint = fingerprint_value if isinstance(fingerprint_value, str) and fingerprint_value else None
    return ProductionSafetyState(
        raw=raw,
        armed=raw.get("armed") is True,
        dry_run_ready_cycles=cycles,
        dry_run_evidence_fingerprint=fingerprint,
    )


def control_pause_reason(value: Any, now: datetime, *, asset: str | None = None) -> str | None:
    """Return a pause rejection reason, treating malformed active state as paused."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return "planner_paused"
    if not value:
        return None
    active = value.get("active")
    if active is False or str(active).lower() in {"false", "off", "0"}:
        return None
    legacy_pause = active is None and any(key in value for key in ("until", "assets", "reason"))
    if active is None:
        if not legacy_pause:
            return None
    elif active is not True:
        return "planner_paused"
    until = _datetime_or_none(value.get("until"))
    if value.get("until") is not None and until is None:
        return "planner_paused"
    if until is not None and _as_utc(now) >= until:
        return None
    assets = value.get("assets")
    if asset is None or assets is None:
        return "planner_paused"
    if isinstance(assets, str):
        asset_values = {assets}
    elif isinstance(assets, list) and all(isinstance(item, str) for item in assets):
        asset_values = set(assets)
    else:
        return "planner_paused"
    if "all" in asset_values:
        return "planner_paused"
    return f"{asset}_control_paused" if asset in asset_values else None


def partition_control_areas_by_pause(
    value: Any,
    now: datetime,
    areas: list[str],
) -> tuple[list[str], list[str]]:
    """Return available and paused control areas using runtime asset semantics."""
    raw_reason = control_pause_reason(value, now)
    configured_assets = value.get("assets") if isinstance(value, dict) else None
    if raw_reason is not None and configured_assets is not None:
        asset_values = (
            {configured_assets}
            if isinstance(configured_assets, str)
            else set(configured_assets)
            if isinstance(configured_assets, list) and all(isinstance(item, str) for item in configured_assets)
            else set()
        )
        known_assets = {"all", *_CONTROL_AREA_ASSETS.values()}
        if not asset_values or not asset_values <= known_assets:
            return [], list(areas)
    available: list[str] = []
    paused: list[str] = []
    for area in areas:
        asset = _CONTROL_AREA_ASSETS.get(area)
        if asset is None or control_pause_reason(value, now, asset=asset) is not None:
            paused.append(area)
        else:
            available.append(area)
    return available, paused


def _datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
