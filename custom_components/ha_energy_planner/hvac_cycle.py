"""HVAC mode timing independent of actuator ownership and tariff lifecycles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def instant(value: Any) -> datetime | None:
    """Read a persisted, timezone-aware instant conservatively."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.astimezone(UTC) if isinstance(value, datetime) and value.tzinfo is not None else None


def mode_intent(record: dict[str, Any]) -> bool:
    """Exclude targets, zones, and commands that cannot change the head mode."""
    previous = record.get("previous_mode")
    service = record.get("service")
    if record.get("uncertainty_compensation") and service in {"turn_on", "turn_off", "set_hvac_mode"}:
        return True
    if service == "turn_on":
        return previous not in {"auto", "cool", "dry", "fan_only", "heat", "heat_cool"}
    if service == "turn_off":
        return previous != "off"
    return service == "set_hvac_mode" and record.get("requested", {}).get("hvac_mode") != previous


def mode_feedback_matches(record: dict[str, Any], mode: str | None) -> bool:
    """Require the mode requested by this exact command, including recalled on mode."""
    service = record.get("service")
    if service == "turn_off":
        return mode == "off"
    if service == "turn_on":
        return mode not in {None, "off", "unknown", "unavailable"}
    return service == "set_hvac_mode" and mode == record.get("requested", {}).get("hvac_mode")


def cycle_active(cycle: dict[str, Any], now: datetime, minutes: int,
                 desired_mode: str | None, current_mode: str | None) -> bool:
    """Block automatic starts/direction changes; safe stops remain immediate."""
    if minutes <= 0 or desired_mode in {None, "off"}:
        return False
    dwell = timedelta(minutes=minutes)
    pending = cycle.get("pending")
    if isinstance(pending, dict):
        # A readback deadline is not proof that the device stopped by then.
        # Fresh feedback must settle uncertainty before a rest clock can expire.
        return True
    if desired_mode == current_mode:
        return False
    changed_at = instant(cycle.get("changed_at"))
    return changed_at is not None and now < changed_at + dwell


def record_cycle(cycle: dict[str, Any], record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Retain one head mode intent and its confirmed feedback boundary."""
    if not mode_intent(record):
        return cycle
    updated = dict(cycle)
    if record["stage"] == "intent":
        updated["pending"] = {key: record[key] for key in ("context_id", "issued_at", "deadline")}
        updated["pending"].update(service=record["service"], previous_mode=record.get("previous_mode"),
                                  requested=record.get("requested", {}),
                                  uncertainty_compensation=bool(record.get("uncertainty_compensation")))
        updated["entity_id"] = record["entity_id"]
    elif record["stage"] == "not_dispatched":
        if updated.get("pending", {}).get("context_id") == record["context_id"]:
            updated.pop("pending")
    elif record["stage"] == "confirmed":
        if updated.get("confirmed_context_id") == record["context_id"]:
            return cycle
        mode = record.get("observed", {}).get("hvac_mode")
        if mode in {None, "unknown", "unavailable"} or not mode_feedback_matches(record, mode):
            return cycle
        pending = updated.get("pending", {})
        if pending and pending.get("context_id") != record["context_id"]:
            return cycle
        updated.pop("pending", None)
        # Matching mode intent proves either a transition or explicit safety
        # compensation; ordinary same-mode commands were excluded above.
        issued = instant(record.get("issued_at")) or now
        feedback = instant(record.get("feedback_changed_at"))
        changed = feedback if feedback is not None and issued <= feedback <= now else now
        previous = instant(updated.get("changed_at"))
        updated.update(entity_id=record["entity_id"], mode=mode,
                       changed_at=max(changed, previous or changed).isoformat(),
                       confirmed_context_id=record["context_id"])
    return updated


def reconcile_cycle(cycle: dict[str, Any], now: datetime, mode: str | None,
                    changed_at: Any, reported_at: Any) -> dict[str, Any]:
    """Settle a surviving intent only from fresh, available head feedback."""
    pending = cycle.get("pending")
    if not isinstance(pending, dict) or mode not in {"off", "auto", "cool", "dry", "fan_only", "heat", "heat_cool"}:
        return cycle
    issued = instant(pending.get("issued_at"))
    reported = instant(reported_at)
    if issued is None or reported is None or not issued <= reported <= now:
        return cycle
    # An unchanged compensating baseline is not proof that the newer write
    # was sent. Its adapter must confirm dispatch/readback, including on reload.
    if pending.get("uncertainty_compensation") or not mode_feedback_matches(pending, mode):
        return cycle
    return record_cycle(cycle, {
        **pending, "entity_id": cycle.get("entity_id"), "stage": "confirmed",
        "observed": {"hvac_mode": mode}, "feedback_changed_at": changed_at,
    }, now)
