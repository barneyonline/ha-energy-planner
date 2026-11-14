"""Shared rolling control-attempt accounting."""

from datetime import UTC, datetime, timedelta
from typing import Any

EXPORT_ACTION_CAP_REACHED = "export_limit_daily_action_cap_reached"


def counted_attempt(row: Any) -> bool:
    """Count applied/uncertain attempts, excluding no-ops and climate releases."""
    return (
        isinstance(row, dict)
        and row.get("asset") in {"ev", "daikin", "enphase", "enphase_export_limit"}
        and (
            (row.get("command_sent") is not False
             and (row.get("result") == "pending"
                  or str(row.get("reason", "")).startswith("export_limit_service_rejected")))
            if row.get("asset") == "enphase_export_limit"
            else (row.get("result") in {"applied", "failed", "restored"}
                  or row.get("asset") == "ev" and row.get("reason") == "ev_vehicle_suspended"
                  and row.get("command_sent") is True)
        )
        and row.get("reason") not in {"already_in_desired_state", "already_in_desired_hvac_state"}
        # Older EV safety-stop retries recorded missing local targets as failed
        # commands even though the adapter never dispatched a service call.
        and not (row.get("asset") == "ev" and row.get("reason") == "ev_control_unavailable"
                 and row.get("command_sent") is not True)
        and (row.get("asset") != "daikin" or row.get("kind") in {None, "set_hvac"})
    )


def attempt_time(row: dict[str, Any]) -> datetime | None:
    """Read legacy naive timestamps as UTC."""
    try:
        value = datetime.fromisoformat(row["attempted_at"])
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    except (KeyError, TypeError, ValueError):
        return None


def _future_export_attempt(row: Any, now: datetime) -> bool:
    """Distinguish a future dispatch from an audit-only outcome."""
    return (
        counted_attempt(row) and row["asset"] == "enphase_export_limit"
        and (at := attempt_time(row)) is not None and at > now
    )


def has_future_export_attempt(rows: list[Any], now: datetime) -> bool:
    """Check the shared ledger independently of the mapped control."""
    return any(_future_export_attempt(row, now) for row in rows)


def reanchor_export_attempts(rows: list[Any], now: datetime) -> list[Any]:
    """Move only future export dispatches to the clock correction time."""
    # A future export attempt is evidence of a dispatch during clock skew.
    # Persist its accounting time on repair so later reads cannot extend it.
    return [
        {**row, "attempted_at": now.isoformat()}
        if _future_export_attempt(row, now)
        else row
        for row in rows
    ]


def recent_attempts(rows: Any, now: datetime) -> list[dict[str, Any]]:
    """Retain compact timestamps independently of presentation audit rotation."""
    return [
        {**{key: row.get(key) for key in ("attempted_at", "asset", "kind", "result", "reason")},
         **({"command_sent": row["command_sent"]} if isinstance(row.get("command_sent"), bool) else {})}
        for row in reanchor_export_attempts(rows, now) if counted_attempt(row)
        and (at := attempt_time(row)) is not None and now - timedelta(hours=24) < at <= now
    ] if isinstance(rows, list) else []


def action_budget(rows: Any, options: dict[str, Any], now: datetime, asset: str) -> dict[str, Any]:
    """Describe allowance and its next expiry without granting command authority."""
    key = {"ev": "ev", "daikin": "climate", "enphase": "enphase", "enphase_export_limit": "export_limit"}[asset]
    limit = int(options.get(f"max_daily_{key}_actions", 0) or 0)
    times = sorted(at for row in recent_attempts(rows, now) if row["asset"] == asset
                   and (at := attempt_time(row)) is not None)
    next_at = times[0] + timedelta(hours=24) if times else None
    available_at = times[len(times) - limit] + timedelta(hours=24) if limit > 0 and len(times) >= limit else None
    return {
        "limit": limit, "used": len(times), "remaining": max(limit - len(times), 0) if limit else None,
        "next_action_expires_at": next_at.isoformat() if next_at else None,
        "allowance_available_at": available_at.isoformat() if available_at else None,
    }


def budget_history(store: dict[str, Any]) -> list[Any]:
    """Use legacy audit only before a dedicated attempt ledger exists."""
    rows = store.get("action_attempts")
    if not isinstance(rows, list):
        rows = store.get("execution_audit")
    return rows if isinstance(rows, list) else []
