"""Shared fail-closed safety-state parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


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
