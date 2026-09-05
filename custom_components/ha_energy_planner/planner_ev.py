"""Pure EV daylight allocation and local ready-by boundary policy."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ev import EVChargeSchedule, allocate_least_cost_charging
from .models import (
    DecisionContext,
)


def _daylight_preferred_ev_schedule(
    context: DecisionContext,
    *,
    standard_schedule: EVChargeSchedule,
    enabled: bool,
    current_soc_percent: float,
    target_soc_percent: float,
    ready_by: datetime,
    earliest_start: datetime,
    charge_rate_kw: float,
    soc_per_kwh: float,
    interval_minutes: int,
    carbon_weight: float,
    continuous: bool,
    continue_current: bool,
    force_current: bool,
    max_import_price: float | None,
) -> tuple[EVChargeSchedule, dict[str, Any]]:
    """Prefer a fully known daylight window without weakening ready-by behavior."""
    evidence: dict[str, Any] = {
        "applicable": False,
        "forecast_complete": False,
        "selected": False,
        "window_start_utc": None,
        "window_end_utc": None,
        "reason": "ev_daylight_lowest_cost_disabled",
    }
    if not enabled:
        return standard_schedule, evidence
    if continue_current:
        evidence["reason"] = "ev_daylight_deferred_active_session"
        return standard_schedule, evidence
    if force_current:
        evidence["reason"] = "ev_daylight_deferred_opportunistic_charge"
        return standard_schedule, evidence
    if current_soc_percent >= target_soc_percent:
        evidence["reason"] = "already_at_target"
        return standard_schedule, evidence

    window = next(
        (
            item
            for item in sorted(context.daylight_windows, key=lambda item: item.start)
            if item.end > context.created_at and item.end <= ready_by
        ),
        None,
    )
    if window is None:
        evidence["reason"] = "ev_daylight_window_not_before_ready_by"
        return standard_schedule, evidence

    evidence.update(
        {
            "applicable": True,
            "window_start_utc": window.start.isoformat(),
            "window_end_utc": window.end.isoformat(),
        }
    )
    interval = timedelta(minutes=interval_minutes)
    remaining_start = max(context.created_at, window.start)
    daylight_forecast_slots = sorted(
        (slot for slot in context.slots if remaining_start <= slot.valid_at < window.end),
        key=lambda slot: slot.valid_at,
    )
    complete = bool(
        daylight_forecast_slots
        and daylight_forecast_slots[0].valid_at < remaining_start + interval
        and daylight_forecast_slots[-1].valid_at + interval >= window.end
        and all(
            right.valid_at - left.valid_at == interval
            for left, right in zip(
                daylight_forecast_slots,
                daylight_forecast_slots[1:],
                strict=False,
            )
        )
        and all(_daylight_cost_inputs_complete(slot) for slot in daylight_forecast_slots)
    )
    evidence["forecast_complete"] = complete
    if not complete:
        evidence["reason"] = "ev_daylight_forecast_incomplete"
        return standard_schedule, evidence

    daylight_slots = [slot for slot in daylight_forecast_slots if slot.valid_at + interval <= window.end]
    daylight_schedule = allocate_least_cost_charging(
        daylight_slots,
        current_soc_percent=current_soc_percent,
        target_soc_percent=target_soc_percent,
        ready_by=window.end,
        charge_rate_kw=charge_rate_kw,
        soc_per_kwh=soc_per_kwh,
        interval_minutes=interval_minutes,
        carbon_weight=carbon_weight,
        continuous=continuous,
        max_import_price=max_import_price,
        allocation_source="daylight",
    )
    if not daylight_schedule.allocations:
        evidence["reason"] = "ev_daylight_no_eligible_charge"
        return standard_schedule, evidence
    if not daylight_schedule.infeasible:
        evidence.update({"selected": True, "reason": "ev_daylight_lowest_cost_selected"})
        return (
            EVChargeSchedule(
                allocations=daylight_schedule.allocations,
                target_soc_percent=daylight_schedule.target_soc_percent,
                scheduled_soc_percent=daylight_schedule.scheduled_soc_percent,
                required_charge_percent=daylight_schedule.required_charge_percent,
                infeasible=False,
                reason="daylight_lowest_effective_cost_slots",
            ),
            evidence,
        )
    if continuous:
        evidence["reason"] = "ev_daylight_continuous_capacity_insufficient"
        return standard_schedule, evidence

    daylight_times = {slot.valid_at for slot in daylight_slots}
    fallback_schedule = allocate_least_cost_charging(
        [slot for slot in context.slots if slot.valid_at not in daylight_times],
        current_soc_percent=daylight_schedule.scheduled_soc_percent,
        target_soc_percent=target_soc_percent,
        ready_by=ready_by,
        charge_rate_kw=charge_rate_kw,
        soc_per_kwh=soc_per_kwh,
        interval_minutes=interval_minutes,
        carbon_weight=carbon_weight,
        earliest_start=earliest_start,
        continuous=False,
        max_import_price=max_import_price,
        allocation_source="ready_by_fallback",
    )
    evidence.update(
        {
            "selected": True,
            "reason": "ev_daylight_lowest_cost_with_ready_by_fallback",
        }
    )
    return (
        EVChargeSchedule(
            allocations=[*daylight_schedule.allocations, *fallback_schedule.allocations],
            target_soc_percent=target_soc_percent,
            scheduled_soc_percent=fallback_schedule.scheduled_soc_percent,
            required_charge_percent=daylight_schedule.required_charge_percent,
            infeasible=fallback_schedule.infeasible,
            reason="daylight_lowest_effective_cost_with_ready_by_fallback",
        ),
        evidence,
    )


def _daylight_cost_inputs_complete(slot: Any) -> bool:
    """Return whether a slot can be compared using complete effective-cost evidence."""
    values = (
        slot.import_price,
        slot.export_price,
        slot.pv_forecast_kw,
        slot.baseline_load_forecast_kw,
    )
    return all(value is not None and not isinstance(value, bool) and isfinite(float(value)) for value in values)


def _next_ready_by(created_at: Any, ready_by: str, local_timezone: str | None = None) -> Any:
    """Return the next local ready-by instant normalized to UTC."""
    try:
        hour_text, minute_text = ready_by.split(":", 1)
        ready_time = time(hour=int(hour_text), minute=int(minute_text[:2]))
    except (TypeError, ValueError):
        ready_time = time(hour=7, minute=0)
    try:
        timezone = ZoneInfo(local_timezone or "UTC")
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    created_at_utc = created_at.astimezone(UTC)
    local_date = created_at_utc.astimezone(timezone).date()
    day_offset = 0
    while True:
        candidates = _valid_local_instants(local_date + timedelta(days=day_offset), ready_time, timezone)
        future = [candidate for candidate in candidates if candidate > created_at_utc]
        if future:
            return min(future)
        day_offset += 1


def _ev_earliest_start(
    created_at: datetime,
    ready_by: datetime,
    configured_start: str,
    local_timezone: str | None,
) -> datetime:
    """Return the active charging window's earliest UTC instant."""
    if configured_start.strip().lower() == "none":
        return created_at.astimezone(UTC)
    try:
        hour_text, minute_text = configured_start.split(":", 1)
        start_time = time(hour=int(hour_text), minute=int(minute_text[:2]))
    except (TypeError, ValueError):
        return created_at.astimezone(UTC)
    try:
        timezone = ZoneInfo(local_timezone or "UTC")
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    ready_date = ready_by.astimezone(timezone).date()
    candidates = [
        instant
        for candidate_date in (ready_date - timedelta(days=1), ready_date)
        for instant in _valid_local_instants(candidate_date, start_time, timezone)
        if instant <= ready_by
    ]
    return max(created_at.astimezone(UTC), max(candidates))


def _valid_local_instants(local_date: date, local_time: time, timezone: ZoneInfo) -> list[datetime]:
    """Resolve a wall time, advancing through a DST gap when necessary."""
    requested = datetime.combine(local_date, local_time)
    minute_offset = 0
    while True:
        wall = requested + timedelta(minutes=minute_offset)
        candidates: set[datetime] = set()
        for fold in (0, 1):
            aware = wall.replace(tzinfo=timezone, fold=fold)
            instant = aware.astimezone(UTC)
            if instant.astimezone(timezone).replace(tzinfo=None) == wall:
                candidates.add(instant)
        if candidates:
            return sorted(candidates)
        minute_offset += 1
