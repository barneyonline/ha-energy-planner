"""Identity-bound export dispatch reservations and observed-confirmation timing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from .const import CONF_COMMAND_RATE_LIMIT_SECONDS
from .export_limit_policy import EXPORT_ASSET

MIN_CHANGE_SECONDS = 300


def control_key(identity: dict[str, str]) -> str:
    """Entity aliases share the same upstream control budget."""
    return EXPORT_ASSET + ":" + json.dumps(
        [identity.get(key, "") for key in ("config_entry_id", "unique_id", "device_id")],
        separators=(",", ":"),
    )


def same_control(left: dict[str, str], right: dict[str, str]) -> bool:
    return control_key(left) == control_key(right)


def valid_stamp(value: Any, now: datetime) -> bool:
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= now.timestamp()


def interval(options: dict[str, Any]) -> float:
    return max(MIN_CHANGE_SECONDS, float(options.get(CONF_COMMAND_RATE_LIMIT_SECONDS, 0) or 0))


def normalized_record(
    limits: dict[str, Any], identity: dict[str, str], ownership: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """Migrate conservatively once; repair bad/future clocks with a bounded wait."""
    key = control_key(identity)
    record = limits.get(key)
    if key not in limits:
        attempted: float | None = None
        if EXPORT_ASSET in limits or ownership.get("baseline") or ownership.get("pending"):
            # Old ownership has no trustworthy observed-confirmation time.
            attempted = now.timestamp()
        return {"version": 1, "checked_at": now.timestamp(), "attempted_at": attempted,
                "confirmed_at": None, "confirmation_request": None, "interval_seconds": MIN_CHANGE_SECONDS}
    saved_interval: Any = record.get("interval_seconds") if isinstance(record, dict) else None
    interval_valid = (type(saved_interval) in (int, float) and isfinite(saved_interval)
                      and MIN_CHANGE_SECONDS <= saved_interval <= 86400)
    if (
        not isinstance(record, dict) or record.get("version") != 1
        or not valid_stamp(record.get("checked_at"), now)
        or any(record.get(key) is not None and not valid_stamp(record[key], now)
               for key in ("attempted_at", "confirmed_at", "external_settled_at"))
        or not interval_valid
        or any(key not in record for key in ("attempted_at", "confirmed_at", "confirmation_request"))
    ):
        return {"version": 1, "checked_at": now.timestamp(), "attempted_at": now.timestamp(),
                "confirmed_at": None, "confirmation_request": None,
                "interval_seconds": saved_interval if interval_valid else MIN_CHANGE_SECONDS,
                "clock_recovered_at": now.isoformat()}
    return {**record, "checked_at": now.timestamp()}


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
                               store_data.get("ownership", {}).get(EXPORT_ASSET, {}), now)
    deadline = next_eligible(record, options)
    return {"minimum_change_interval_seconds": MIN_CHANGE_SECONDS,
            "next_eligible_change_at": deadline.isoformat() if deadline else None,
            "last_dispatch_reserved_at": record.get("attempted_at"),
            "last_change_observed_at": record.get("confirmed_at"),
            "last_external_settlement_observed_at": record.get("external_settled_at"),
            "deferred": deadline is not None and now < deadline,
            "cooldown_reason": "export_limit_change_rate_limited" if deadline and now < deadline else None}
