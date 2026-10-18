"""Stable UTC forecast boundaries and remaining, rather than nominal, durations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .models import DecisionSlot


def planning_starts(
    now: datetime, horizon_hours: int, interval_minutes: int, tariffs: list[dict[str, Any]],
) -> list[datetime]:
    """Keep the current partial interval and every native future price boundary."""
    now = now.astimezone(UTC)
    end = now + timedelta(hours=horizon_hours)
    interval = timedelta(minutes=interval_minutes)
    native = [datetime.fromisoformat(row[key]).astimezone(UTC)
              for row in tariffs for key in ("start", "end")]
    if not native:
        return [now + timedelta(minutes=offset) for offset in range(0, horizon_hours * 60, interval_minutes)]
    origin = min(native)
    first = origin + ((now - origin) // interval + 1) * interval
    starts = {now, *(at for at in native if now < at < end)}
    while first < end:
        starts.add(first)
        first += interval
    return sorted(starts)


def slot_end(slot: DecisionSlot, interval_minutes: float) -> datetime:
    """Honor explicit split/tail boundaries; compatibility slots remain uniform."""
    return getattr(slot, "end_at", None) or slot.valid_at + timedelta(minutes=interval_minutes)


def slot_hours(slot: DecisionSlot, interval_minutes: float) -> float:
    """Return only this interval's available operating time."""
    return max((slot_end(slot, interval_minutes) - slot.valid_at).total_seconds() / 3600, 0)
