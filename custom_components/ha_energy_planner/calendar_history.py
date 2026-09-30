"""Persistent confirmed calendar activity, independent of planning evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def calendar_records(value: Any) -> list[dict[str, str]]:
    """Read only valid, compact calendar records from a saved list."""
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start, end = _timestamp(item.get("start")), _timestamp(item.get("end"))
        if start is None or end is None or end <= start:
            continue
        if not all(isinstance(item.get(key), str) and item[key] for key in ("uid", "summary")):
            continue
        record = {"start": start.isoformat(), "end": end.isoformat()}
        for key, limit in (("uid", 255), ("summary", 255), ("location", 255), ("description", 4096)):
            text = item.get(key)
            if isinstance(text, str):
                record[key] = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        confirmed = _timestamp(item.get("confirmed_start"))
        if confirmed is not None and confirmed == start:
            record["confirmed_start"] = confirmed.isoformat()
        if item.get("confirmed_action") == "true" and confirmed is not None and confirmed == start:
            record["confirmed_action"] = "true"
        records.append(record)
    return records


def update_calendar_history(
    saved: Any, current: list[dict[str, str]], now: datetime,
    *, replace_plan: bool = True, location: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Keep only confirmed activity as history; future plans remain provisional.

    A start must be observed independently of planned timing. Historical window
    ends remain estimates unless a discrete action was confirmed by its adapter.
    No time or count expiry applies to confirmed history.
    """
    saved = saved if isinstance(saved, dict) else {}
    history = [record for record in calendar_records(saved.get("history")) if _confirmed_start(record)]
    current = calendar_records(current)
    if not replace_plan:
        # Observations may come from an older coordinator plan while the latest
        # plan is already persisted. They must never replace its command IDs or
        # future windows. A scoped feedback event can close its device's window;
        # an execution snapshot cannot infer a stop from an absent old-plan window.
        observed = [record for record in current if _confirmed_start(record)
                    and (location is None or record.get("location") == location)]
        if location is None:
            # An old execution can confirm the same phase with a shorter
            # forecast than the newly published plan. Keep its newer extent.
            for index, record in enumerate(observed):
                newer = next((saved_record for saved_record in calendar_records(saved.get("pending"))
                              if _same_confirmed_window(saved_record, record)
                              and datetime.fromisoformat(saved_record["end"]) > datetime.fromisoformat(record["end"])),
                             None)
                if newer is not None:
                    observed[index] = newer
        observed_locations = {record.get("location") for record in observed}
        current = observed + [
            record for record in calendar_records(saved.get("pending"))
            if not _confirmed_start(record) or (
                record.get("location") != location if location is not None
                else record.get("location") not in observed_locations
            )
        ]
    pending = [record for record in current if not any(
        archived["uid"] == record["uid"] and not _confirmed_start(record) for archived in history
    )]
    # A confirmed phase may outlast its forecast end. Keep one entry for that
    # phase when a later plan extends it, even if an earlier end was archived.
    history = [record for record in history if not any(
        _confirmed_start(record) and _confirmed_start(candidate)
        and record["start"] == candidate["start"]
        and _same_running_window(record, candidate, now)
        for candidate in pending
    )]
    for previous in calendar_records(saved.get("pending")):
        start, end = datetime.fromisoformat(previous["start"]), datetime.fromisoformat(previous["end"])
        match = next((record for record in pending if _same_running_window(previous, record, now)), None)
        if match is not None:
            continue
        if end <= now:
            _append_unique(history, previous)
        elif start < now and _confirmed_start(previous):
            previous["end"] = now.isoformat()
            previous["description"] = (
                previous.get("description", "")
                + "\n\nConfirmed activity. End time is estimated; no confirmed stop time was recorded."
            ).encode("utf-8")[:4096].decode("utf-8", errors="ignore")
            _append_unique(history, previous)
        # A replaced window that has not started is not historical activity.
    active = []
    for record in pending:
        if datetime.fromisoformat(record["end"]) <= now:
            _append_unique(history, record)
        else:
            active.append(record)
    return {"history": sorted(history, key=lambda item: item["start"]), "pending": active}


def _append_unique(history: list[dict[str, str]], record: dict[str, str]) -> None:
    """Coalesce the same published window even if its plan ID changed."""
    for index, previous in enumerate(history):
        if _same_confirmed_window(previous, record):
            if datetime.fromisoformat(previous["end"]) >= datetime.fromisoformat(record["end"]):
                return
            # Recovered feedback can extend an elapsed estimate for the same
            # phase. Replace its earlier representation rather than duplicate it.
            history.pop(index)
            break
    keys = ("start", "end", "summary", "location")
    if _confirmed_start(record) and not any(
        all(previous.get(key) == record.get(key) for key in keys) for previous in history
    ):
        record = dict(record)
        if "End time is estimated" not in record.get("description", "") and not record.get("confirmed_action"):
            record["description"] = (
                "Confirmed activity. End time is estimated; no confirmed stop time was recorded.\n\n"
                + record.get("description", "")
            ).encode("utf-8")[:4096].decode("utf-8", errors="ignore")
        history.append(record)


def _confirmed_start(record: dict[str, str]) -> bool:
    return record.get("confirmed_start") == record["start"]


def _same_confirmed_window(previous: dict[str, str], current: dict[str, str]) -> bool:
    """Identify one continuous phase independently of its estimated end."""
    return (
        _confirmed_start(previous) and _confirmed_start(current)
        and not previous.get("confirmed_action") and not current.get("confirmed_action")
        and all(previous.get(key) == current.get(key) for key in ("start", "summary", "location"))
    )


def _same_running_window(previous: dict[str, str], current: dict[str, str], now: datetime) -> bool:
    """Match continuous plan windows while keeping real restarts separate."""
    if any(previous.get(key) != current.get(key) for key in ("summary", "location")):
        return False
    if not (_confirmed_start(previous) and _confirmed_start(current)) or previous["start"] != current["start"]:
        return False
    return (
        datetime.fromisoformat(previous["start"]) <= now
        and datetime.fromisoformat(current["start"]) <= now < datetime.fromisoformat(current["end"])
        and datetime.fromisoformat(current["start"]) < datetime.fromisoformat(previous["end"])
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def confirm_calendar_action(saved: Any, outcome: dict[str, Any]) -> dict[str, Any]:
    """Retain discrete actions only after the device adapter confirmed success."""
    state = dict(saved) if isinstance(saved, dict) else {}
    # Charging windows need physical charging feedback, and HVAC phases need
    # committed ownership plus matching live mode, rather than service acceptance.
    confirmed = outcome.get("result") == "applied" or (
        outcome.get("kind") == "release_hvac" and outcome.get("result") == "restored"
    )
    if not confirmed or outcome.get("kind") not in {
        "set_profile", "restore_ai", "ev_stop", "release_hvac",
    }:
        return state
    start = _timestamp(outcome.get("attempted_at"))
    if start is None:
        return state
    history = [record for record in calendar_records(state.get("history")) if _confirmed_start(record)]
    pending = calendar_records(state.get("pending"))
    matches = [record for record in pending if record["uid"] == outcome.get("action_id")]
    if not matches:
        fallback = _outcome_record(outcome, start)
        if fallback is not None:
            matches.append(fallback)
    for record in matches:
        record = {
            **record, "start": start.isoformat(), "end": (start + timedelta(seconds=1)).isoformat(),
            "confirmed_start": start.isoformat(), "confirmed_action": "true",
            "description": (
                "Confirmed device action. This entry marks execution, not a running duration.\n\n"
                + record.get("description", "")
            ).encode("utf-8")[:4096].decode("utf-8", errors="ignore"),
        }
        _append_unique(history, record)
    state["history"] = history
    return state


def _outcome_record(outcome: dict[str, Any], start: datetime) -> dict[str, str] | None:
    """Recover a superseded plan's discrete event from its confirmed outcome."""
    plan_id, action_id = outcome.get("plan_id"), outcome.get("action_id")
    if not isinstance(plan_id, str) or plan_id in {"", "manual"} or not isinstance(action_id, str) or not action_id:
        return None
    labels = {
        ("enphase", "set_profile"): ("Enphase", "Set Enphase profile"),
        ("enphase", "restore_ai"): ("Enphase", "Restore Enphase AI profile"),
        ("ev", "ev_stop"): ("EV", "Stop EV charging"),
        ("daikin", "release_hvac"): ("Climate", "Release climate control"),
    }
    asset, kind = outcome.get("asset"), outcome.get("kind")
    if not isinstance(asset, str) or not isinstance(kind, str) or (asset, kind) not in labels:
        return None
    location, summary = labels[(asset, kind)]
    return calendar_records([{
        "uid": action_id, "summary": f"{location}: {summary}", "location": location,
        "start": start.isoformat(), "end": (start + timedelta(seconds=1)).isoformat(),
    }])[0]
