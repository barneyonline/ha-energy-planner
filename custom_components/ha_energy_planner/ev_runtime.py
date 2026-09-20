"""Pure EV execution evidence, spending settlement, and allocation deadlines."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .action_limits import action_budget
from .ev_policy import finite


def timestamp(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return result if result.tzinfo is not None else None


def audit_evidence(audit: list[Any], options: dict[str, Any], now: datetime) -> dict[str, Any]:
    rows = [
        row
        for row in audit
        if isinstance(row, dict)
        and row.get("asset") == "ev"
        and row.get("result") in {"applied", "failed", "restored"}
        and (at := timestamp(row.get("attempted_at"))) is not None
        and now - timedelta(hours=24) <= at <= now
    ]
    limit = int(options.get("max_daily_ev_actions", 10))
    changed = [row for row in rows if row.get("reason") != "already_in_desired_state"]
    latest = max((at for row in changed if (at := timestamp(row.get("attempted_at"))) is not None), default=None)
    return {
        "remaining_actions": (
            action_budget(audit, {"max_daily_ev_actions": limit}, now, "ev")["remaining"] if limit > 0 else 10000
        ),
        "last_transition_at": latest.isoformat() if latest else None,
    }


def settle_spending(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Settle a durable command exposure using its conservative grid upper bound."""
    result = dict(record)
    exposure = result.get("command_exposure")
    if not isinstance(exposure, dict):
        return result
    start = timestamp(exposure.get("at"))
    if start is not None and start <= now:
        cost_rate = max(finite(exposure.get("cost_per_hour")) or 0, 0)
        result["emergency_spend"] = (
            max(finite(result.get("emergency_spend")) or 0, 0) + (now - start).total_seconds() / 3600 * cost_rate
        )
        result["command_exposure"] = {**exposure, "at": now.isoformat()}
    return result


def allocation_deadline(
    action: Any, options: dict[str, Any], record: dict[str, Any], now: datetime, price: float | None
) -> tuple[datetime | None, float, str | None]:
    """A premium command gets a bounded lease; never infer authorisation from missing prices."""
    evidence = action.desired_state.get("optimization", {})
    completion = timestamp(evidence.get("conservative_completion"))
    end = action.execute_not_after
    deadline = completion if completion is not None and now < completion < end else None
    if not options.get("ev_price_limit_enabled"):
        return deadline, 0.0, None
    ceiling = float(options.get("ev_max_import_price", 1))
    if record.get("budget_uncertain") and price is not None and price > ceiling:
        return None, 0.0, "ev_emergency_spend_uncertain"
    if price is None:
        return None, 0.0, "ev_execution_price_unavailable"
    if price <= ceiling:
        return deadline, 0.0, None
    if options.get("ev_price_policy") != "departure_priority":
        return None, 0.0, "ev_execution_price_ceiling"
    emergency = finite(options.get("ev_emergency_price"))
    budget = max((finite(options.get("ev_emergency_budget")) or 0) - (finite(record.get("emergency_spend")) or 0), 0)
    if emergency is None or price > emergency or budget <= 0:
        return None, 0.0, "ev_emergency_budget_or_ceiling"
    power = max(
        finite(action.desired_state.get("projected_load_kw_now")) or 0, finite(options.get("ev_charge_rate_kw")) or 0, 0
    )
    # Reserve for the configured emergency ceiling, including confirmation and
    # stop dispatch latency. Missing solar telemetry never grants budget credit.
    cost_rate = power * max(emergency - ceiling, 0)
    if cost_rate <= 0:
        return None, 0.0, "ev_emergency_budget_or_ceiling"
    latency = 30 + float(options.get("ev_confirmation_timeout_seconds", 30)) * (
        int(options.get("ev_confirmation_retries", 1)) + 1
    )
    seconds = budget / cost_rate * 3600 - latency
    if seconds <= 0:
        return None, 0.0, "ev_emergency_budget_insufficient_for_confirmation"
    deadline = min(deadline or end, end, now + timedelta(seconds=seconds))
    return deadline, cost_rate, None


def price_stop_required(options: dict[str, Any], record: dict[str, Any], price: float | None) -> bool:
    """Revoke existing cost authority independently of the next start action."""
    if not options.get("ev_price_limit_enabled"):
        return False
    if price is None:
        return True
    if price <= float(options.get("ev_max_import_price", 1)):
        return False
    if options.get("ev_price_policy") != "departure_priority":
        return True
    return bool(
        record.get("budget_uncertain")
        or price > float(options.get("ev_emergency_price", 0))
        or float(record.get("emergency_spend", 0)) >= float(options.get("ev_emergency_budget", 0))
    )
