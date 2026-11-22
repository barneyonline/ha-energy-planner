"""Identity-bound export dispatch reservations and observed-confirmation timing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypeGuard

from .const import CONF_COMMAND_RATE_LIMIT_SECONDS
from .export_limit_policy import EXPORT_ASSET

MIN_CHANGE_SECONDS = 300
MAX_SAVED_CHANGE_SECONDS = 86400
PRUNED_TIMING_KEY = f"{EXPORT_ASSET}_pruned_timing"


def control_key(identity: dict[str, str]) -> str:
    """Entity aliases share the same upstream control budget."""
    return EXPORT_ASSET + ":" + json.dumps(
        [identity.get(key, "") for key in ("config_entry_id", "unique_id", "device_id")],
        separators=(",", ":"),
    )


def same_control(left: dict[str, str], right: dict[str, str]) -> bool:
    return control_key(left) == control_key(right)


def valid_stamp(value: Any, now: datetime) -> TypeGuard[int | float]:
    return type(value) in (int, float) and 0 <= value <= now.timestamp()


def interval(options: dict[str, Any]) -> float:
    return max(MIN_CHANGE_SECONDS, float(options.get(CONF_COMMAND_RATE_LIMIT_SECONDS, 0) or 0))


def _valid_interval(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and MIN_CHANGE_SECONDS <= value <= MAX_SAVED_CHANGE_SECONDS


def _valid_record(record: Any, now: datetime) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(record, dict) and record.get("version") == 1
        and valid_stamp(record.get("checked_at"), now)
        and not any(record.get(key) is not None and not valid_stamp(record[key], now)
                    for key in ("attempted_at", "confirmed_at", "external_settled_at"))
        and _valid_interval(record.get("interval_seconds"))
        and all(key in record for key in ("attempted_at", "confirmed_at", "confirmation_request"))
    )


def _pruned_timing(limits: dict[str, Any]) -> tuple[float, float]:
    """Read the latest prune time and the longest reservation it may have retired."""
    summary = limits.get(PRUNED_TIMING_KEY)
    if isinstance(summary, dict):
        at, seconds = summary.get("checked_at"), summary.get("interval_seconds")
        if valid_stamp(at, datetime.max.replace(tzinfo=UTC)) and _valid_interval(seconds):
            return at, seconds
    return 0, MIN_CHANGE_SECONDS


def normalized_record(
    limits: dict[str, Any], identity: dict[str, str], ownership: dict[str, Any], now: datetime,
    *, configured_interval: float,
) -> dict[str, Any]:
    """Migrate conservatively once; repair bad/future clocks with a bounded wait."""
    key = control_key(identity)
    record = limits.get(key)
    if _valid_record(record, now):
        return {**record}

    pruned_at, pruned_interval = _pruned_timing(limits)
    corrected_since_prune = now.timestamp() < pruned_at
    if key not in limits and not corrected_since_prune:
        attempted: float | None = None
        if EXPORT_ASSET in limits or ownership.get("baseline") or ownership.get("pending"):
            # Old ownership has no trustworthy observed-confirmation time.
            attempted = now.timestamp()
        return {"version": 1, "checked_at": now.timestamp(), "attempted_at": attempted,
                "confirmed_at": None, "confirmation_request": None,
                "interval_seconds": (MIN_CHANGE_SECONDS if attempted is None else
                                     max(MIN_CHANGE_SECONDS, min(configured_interval, MAX_SAVED_CHANGE_SECONDS)))}

    saved = record.get("interval_seconds") if isinstance(record, dict) else None
    # A control may be missing or have been recreated while the clock was
    # ahead. In either case its pruned reservation can still apply.
    return {"version": 1, "checked_at": now.timestamp(), "attempted_at": now.timestamp(),
            "confirmed_at": None, "confirmation_request": None,
            "interval_seconds": max(
                saved if _valid_interval(saved) else MIN_CHANGE_SECONDS,
                pruned_interval if corrected_since_prune else MIN_CHANGE_SECONDS,
                min(configured_interval, MAX_SAVED_CHANGE_SECONDS),
            ),
            "clock_recovered_at": now.isoformat()}


def prune_inactive_records(limits: dict[str, Any], protected: set[str], now: datetime) -> None:
    """Forget unmapped identities only once every possible saved wait has elapsed."""
    cutoff = now.timestamp() - MAX_SAVED_CHANGE_SECONDS
    retired_interval = None
    for key, record in list(limits.items()):
        if key in protected or not key.startswith(EXPORT_ASSET + ":"):
            continue
        # The latest evidence is stamped at reservation, confirmation, or
        # settlement. Retain recent identities across aliases and remaps.
        if _valid_record(record, now) and max(
            record[name] for name in ("checked_at", "attempted_at", "confirmed_at", "external_settled_at")
            if record.get(name) is not None
        ) <= cutoff:
            retired_interval = max(retired_interval or MIN_CHANGE_SECONDS, record["interval_seconds"])
            del limits[key]
    if retired_interval is not None:
        pruned_at, pruned_interval = _pruned_timing(limits)
        limits[PRUNED_TIMING_KEY] = {
            "checked_at": max(pruned_at, now.timestamp()),
            "interval_seconds": max(pruned_interval, retired_interval),
        }


def next_eligible(record: dict[str, Any], options: dict[str, Any]) -> datetime | None:
    deadlines = []
    if record.get("attempted_at") is not None:
        deadlines.append(record["attempted_at"] + max(interval(options), record["interval_seconds"]))
    for key in ("confirmed_at", "external_settled_at"):
        if record.get(key) is not None:
            deadlines.append(record[key] + MIN_CHANGE_SECONDS)
    return datetime.fromtimestamp(max(deadlines), UTC) if deadlines else None


def cooldown_status(
    store_data: dict[str, Any], identity: dict[str, str] | None, options: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """Expose a deadline without attributing reads or external writes as changes."""
    if identity is None:
        return {}
    record = normalized_record(store_data.get("command_rate_limits", {}), identity,
                               store_data.get("ownership", {}).get(EXPORT_ASSET, {}), now,
                               configured_interval=interval(options))
    deadline = next_eligible(record, options)
    return {"minimum_change_interval_seconds": MIN_CHANGE_SECONDS,
            "next_eligible_change_at": deadline.isoformat() if deadline else None,
            "last_dispatch_reserved_at": record.get("attempted_at"),
            "last_change_observed_at": record.get("confirmed_at"),
            "last_external_settlement_observed_at": record.get("external_settled_at"),
            "deferred": deadline is not None and now < deadline,
            "cooldown_reason": "export_limit_change_rate_limited" if deadline and now < deadline else None}
