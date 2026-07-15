"""EV trip-history and target calculation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any

MAX_STORED_TRIPS = 120


@dataclass(slots=True)
class EVTripRecord:
    """A compact EV trip record."""

    started_at: datetime
    ended_at: datetime
    start_soc_percent: float
    end_soc_percent: float

    @property
    def consumed_soc_percent(self) -> float:
        """Return consumed SOC for this trip."""
        return max(self.start_soc_percent - self.end_soc_percent, 0.0)


@dataclass(slots=True)
class EVTripSummary:
    """Summarized trip-history demand."""

    observed_days: int
    max_daily_soc_percent: float
    average_daily_soc_percent: float
    history_sufficient: bool


@dataclass(slots=True)
class EVTarget:
    """Calculated EV charging target."""

    current_soc_percent: float | None
    target_soc_percent: float
    required_charge_percent: float
    max_attainable_soc_percent: float
    infeasible: bool
    reason: str


@dataclass(slots=True)
class EVChargeAllocation:
    """Allocated EV charging slot."""

    valid_at: datetime
    charge_kw: float
    added_soc_percent: float
    import_price: float
    effective_price: float | None = None
    solar_surplus_used_kw: float = 0.0
    grid_import_used_kw: float = 0.0
    carbon_intensity_g_per_kwh: float | None = None
    estimated_carbon_g: float | None = None


@dataclass(slots=True)
class EVChargeSchedule:
    """Least-cost EV charging schedule."""

    allocations: list[EVChargeAllocation]
    target_soc_percent: float
    scheduled_soc_percent: float
    required_charge_percent: float
    infeasible: bool
    reason: str


def summarize_trip_history(
    trips: list[EVTripRecord],
    *,
    minimum_history_days: int = 3,
) -> EVTripSummary:
    """Summarize trips into conservative daily SOC consumption."""
    daily: dict[str, float] = {}
    for trip in trips:
        day = trip.started_at.date().isoformat()
        daily[day] = daily.get(day, 0.0) + trip.consumed_soc_percent
    observed = len(daily)
    values = list(daily.values())
    max_daily = max(values, default=0.0)
    average_daily = sum(values) / observed if observed else 0.0
    return EVTripSummary(
        observed_days=observed,
        max_daily_soc_percent=round(max_daily, 3),
        average_daily_soc_percent=round(average_daily, 3),
        history_sufficient=observed >= minimum_history_days,
    )


def summarize_stored_trip_history(
    history: dict[str, Any] | None,
    *,
    minimum_history_days: int = 3,
) -> EVTripSummary:
    """Summarize persisted compact trip history."""
    return summarize_trip_history(
        trip_records_from_store(history or {}),
        minimum_history_days=minimum_history_days,
    )


def trip_records_from_store(history: dict[str, Any]) -> list[EVTripRecord]:
    """Parse stored trip records."""
    records: list[EVTripRecord] = []
    for item in history.get("records", []):
        if not isinstance(item, dict):
            continue
        try:
            records.append(
                EVTripRecord(
                    started_at=_parse_datetime(item["started_at"]),
                    ended_at=_parse_datetime(item["ended_at"]),
                    start_soc_percent=float(item["start_soc_percent"]),
                    end_soc_percent=float(item["end_soc_percent"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records


def update_trip_history_from_values(
    history: dict[str, Any] | None,
    *,
    connected: bool | None,
    soc_percent: float | None,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """Update compact trip history from current EV connection/SOC state."""
    updated = {
        "active_trip": dict((history or {}).get("active_trip") or {}),
        "records": list((history or {}).get("records") or []),
    }
    if connected is None or soc_percent is None:
        return updated, False

    active_trip = dict(updated.get("active_trip") or {})
    if connected is False:
        if active_trip:
            return updated, False
        updated["active_trip"] = {
            "started_at": now.isoformat(),
            "start_soc_percent": round(float(soc_percent), 3),
        }
        return updated, True

    if not active_trip:
        return updated, False

    start_soc = _float_or_none(active_trip.get("start_soc_percent"))
    started_at = active_trip.get("started_at")
    updated["active_trip"] = {}
    if start_soc is None or started_at is None or start_soc <= float(soc_percent):
        return updated, True

    records = list(updated.get("records") or [])
    records.append(
        {
            "started_at": str(started_at),
            "ended_at": now.isoformat(),
            "start_soc_percent": round(start_soc, 3),
            "end_soc_percent": round(float(soc_percent), 3),
        }
    )
    updated["records"] = records[-MAX_STORED_TRIPS:]
    return updated, True


def import_trip_history_from_state_sequences(
    history: dict[str, Any] | None,
    *,
    connected_states: list[Any],
    soc_states: list[Any],
    imported_at: datetime,
) -> tuple[dict[str, Any], bool]:
    """Import compact trip records from Recorder state sequences."""
    updated = {
        "active_trip": dict((history or {}).get("active_trip") or {}),
        "records": list((history or {}).get("records") or []),
        "recorder_imported_at": (history or {}).get("recorder_imported_at"),
    }
    events = _trip_history_events(connected_states, soc_states)
    if not events:
        return updated, False

    records = list(updated.get("records") or [])
    known_keys = {_record_key(record) for record in records if isinstance(record, dict)}
    active_start: datetime | None = None
    active_start_soc: float | None = None
    last_soc: float | None = None
    changed = False

    for event_time, kind, value in events:
        if kind == "soc":
            last_soc = value
            continue
        connected = bool(value)
        if not connected:
            if active_start is None and last_soc is not None:
                active_start = event_time
                active_start_soc = last_soc
            continue
        if active_start is None or active_start_soc is None or last_soc is None:
            active_start = None
            active_start_soc = None
            continue
        if active_start_soc > last_soc:
            record = {
                "started_at": active_start.isoformat(),
                "ended_at": event_time.isoformat(),
                "start_soc_percent": round(active_start_soc, 3),
                "end_soc_percent": round(last_soc, 3),
                "source": "recorder",
            }
            key = _record_key(record)
            if key not in known_keys:
                records.append(record)
                known_keys.add(key)
                changed = True
        active_start = None
        active_start_soc = None

    records = sorted(
        [record for record in records if isinstance(record, dict)],
        key=lambda record: str(record.get("started_at", "")),
    )
    updated["records"] = records[-MAX_STORED_TRIPS:]
    updated["recorder_imported_at"] = imported_at.isoformat()
    return updated, changed or updated["recorder_imported_at"] != (history or {}).get("recorder_imported_at")


def calculate_ev_target(
    *,
    current_soc_percent: float | None,
    summary: EVTripSummary,
    ev_min_soc_percent: float,
    ev_max_soc_percent: float,
    fallback_target_soc_percent: float,
    available_charge_hours: float,
    charge_rate_percent_per_hour: float,
) -> EVTarget:
    """Calculate a conservative ready-by target and feasibility."""
    if ev_min_soc_percent > ev_max_soc_percent:
        raise ValueError("ev_min_soc_percent must be <= ev_max_soc_percent")
    if summary.history_sufficient:
        desired = ev_min_soc_percent + summary.max_daily_soc_percent
        reason = "history_max_daily_consumption"
    else:
        desired = fallback_target_soc_percent
        reason = "fallback_until_history_sufficient"

    target = _clamp(desired, ev_min_soc_percent, ev_max_soc_percent)
    current = current_soc_percent
    starting_soc = current if current is not None else 0.0
    max_attainable = min(
        ev_max_soc_percent,
        starting_soc + max(available_charge_hours, 0.0) * max(charge_rate_percent_per_hour, 0.0),
    )
    feasible_target = min(target, max_attainable)
    infeasible = feasible_target < target
    return EVTarget(
        current_soc_percent=current_soc_percent,
        target_soc_percent=round(feasible_target, 3),
        required_charge_percent=round(max(feasible_target - starting_soc, 0.0), 3),
        max_attainable_soc_percent=round(max_attainable, 3),
        infeasible=infeasible,
        reason="infeasible_before_ready_by" if infeasible else reason,
    )


def allocate_least_cost_charging(
    slots: list[Any],
    *,
    current_soc_percent: float,
    target_soc_percent: float,
    ready_by: datetime,
    charge_rate_kw: float,
    soc_per_kwh: float,
    interval_minutes: int,
    carbon_weight: float = 0.0,
    earliest_start: datetime | None = None,
    continuous: bool = False,
    force_current: bool = False,
    max_import_price: float | None = None,
) -> EVChargeSchedule:
    """Allocate EV charging to cheapest feasible slots before ready-by.

    Slot ranking is solar-aware: surplus PV is valued at the foregone feed-in
    price, and any remaining charge power is valued at the grid import price.
    """
    required = max(target_soc_percent - current_soc_percent, 0.0)
    if required == 0:
        return EVChargeSchedule([], target_soc_percent, current_soc_percent, 0.0, False, "already_at_target")

    soc_per_slot = max(charge_rate_kw, 0.0) * (interval_minutes / 60.0) * max(soc_per_kwh, 0.0)
    if soc_per_slot <= 0:
        return EVChargeSchedule([], target_soc_percent, current_soc_percent, required, True, "ev_charge_rate_invalid")

    feasible_slots = [
        slot
        for slot in slots
        if slot.valid_at < ready_by
        and (earliest_start is None or slot.valid_at >= earliest_start)
        and slot.import_price is not None
    ]
    current_slot = min(feasible_slots, key=lambda slot: slot.valid_at, default=None)
    price_eligible = [
        slot
        for slot in feasible_slots
        if max_import_price is None
        or float(slot.import_price) <= max_import_price
        or (force_current and slot is current_slot)
    ]
    ranked = _rank_charging_slots(price_eligible, charge_rate_kw, carbon_weight)
    if continuous:
        ordered = _best_continuous_slots(
            price_eligible,
            ranked,
            required_slots=ceil(required / soc_per_slot),
            interval_minutes=interval_minutes,
            force_current=force_current,
        )
    elif force_current and current_slot in ranked:
        ordered = [current_slot, *(slot for slot in ranked if slot is not current_slot)]
    else:
        ordered = ranked
    remaining = required
    allocations: list[EVChargeAllocation] = []
    for slot in ordered:
        if remaining <= 0:
            break
        added_soc = min(soc_per_slot, remaining)
        charge_fraction = added_soc / soc_per_slot
        charge_kw = round(charge_rate_kw * charge_fraction, 6)
        effective_price, solar_kw, grid_kw = _charge_cost_components(slot, charge_kw)
        carbon_intensity = _float_or_none(getattr(slot, "carbon_intensity_g_per_kwh", None))
        estimated_carbon = (
            round(grid_kw * (interval_minutes / 60.0) * carbon_intensity, 3) if carbon_intensity is not None else None
        )
        allocations.append(
            EVChargeAllocation(
                valid_at=slot.valid_at,
                charge_kw=charge_kw,
                added_soc_percent=round(added_soc, 6),
                import_price=float(slot.import_price),
                effective_price=effective_price,
                solar_surplus_used_kw=solar_kw,
                grid_import_used_kw=grid_kw,
                carbon_intensity_g_per_kwh=carbon_intensity,
                estimated_carbon_g=estimated_carbon,
            )
        )
        remaining -= added_soc

    scheduled = target_soc_percent - max(remaining, 0.0)
    infeasible = remaining > 0.000001
    used_solar_surplus = any(allocation.solar_surplus_used_kw > 0 for allocation in allocations)
    return EVChargeSchedule(
        allocations=allocations,
        target_soc_percent=round(target_soc_percent, 3),
        scheduled_soc_percent=round(scheduled, 3),
        required_charge_percent=round(required, 3),
        infeasible=infeasible,
        reason="infeasible_before_ready_by_or_price_limit"
        if infeasible and max_import_price is not None
        else "infeasible_before_ready_by"
        if infeasible
        else "continuous_charging_window_before_ready_by"
        if continuous
        else "least_cost_solar_aware_slots_before_ready_by"
        if used_solar_surplus
        else "least_cost_slots_before_ready_by",
    )


def _best_continuous_slots(
    feasible_slots: list[Any],
    ranked_slots: list[Any],
    *,
    required_slots: int,
    interval_minutes: int,
    force_current: bool,
) -> list[Any]:
    """Return the lowest-ranked contiguous window in chronological order."""
    chronological = sorted(feasible_slots, key=lambda slot: slot.valid_at)
    if required_slots <= 0 or not chronological:
        return []
    rank = {slot.valid_at: index for index, slot in enumerate(ranked_slots)}
    windows: list[list[Any]] = []
    for index in range(len(chronological)):
        window = chronological[index : index + required_slots]
        if len(window) != required_slots:
            continue
        if all(
            (right.valid_at - left.valid_at).total_seconds() == interval_minutes * 60
            for left, right in zip(window, window[1:], strict=False)
        ):
            windows.append(window)
    if force_current:
        windows = [window for window in windows if window[0] is chronological[0]]
    if not windows:
        return chronological[:required_slots]
    return min(windows, key=lambda window: (sum(rank[slot.valid_at] for slot in window), window[0].valid_at))


def _rank_charging_slots(slots: list[Any], charge_kw: float, carbon_weight: float) -> list[Any]:
    """Rank slots by normalized effective cost and grid carbon intensity."""
    weighted_carbon = min(max(float(carbon_weight), 0.0), 1.0)
    rows = []
    for slot in slots:
        cost = _effective_charge_price(slot, charge_kw)
        _effective, _solar_kw, grid_kw = _charge_cost_components(slot, charge_kw)
        intensity = _float_or_none(getattr(slot, "carbon_intensity_g_per_kwh", None))
        carbon = intensity * (grid_kw / charge_kw) if intensity is not None and charge_kw > 0 else None
        rows.append((slot, cost, carbon))
    available_carbon = [carbon for _slot, _cost, carbon in rows if carbon is not None]
    if not available_carbon or weighted_carbon == 0:
        return [row[0] for row in sorted(rows, key=lambda row: (row[1], row[0].valid_at))]
    costs = [cost for _slot, cost, _carbon in rows]
    min_cost, max_cost = min(costs), max(costs)
    min_carbon, max_carbon = min(available_carbon), max(available_carbon)

    def score(row: tuple[Any, float, float | None]) -> tuple[float, float, datetime]:
        slot, cost, carbon = row
        normalized_cost = _normalize_range(cost, min_cost, max_cost)
        normalized_carbon = _normalize_range(
            max_carbon if carbon is None else carbon,
            min_carbon,
            max_carbon,
        )
        return (
            (1.0 - weighted_carbon) * normalized_cost + weighted_carbon * normalized_carbon,
            cost,
            slot.valid_at,
        )

    return [row[0] for row in sorted(rows, key=score)]


def _normalize_range(value: float, minimum: float, maximum: float) -> float:
    if maximum <= minimum:
        return 0.0
    return (value - minimum) / (maximum - minimum)


def _effective_charge_price(slot: Any, charge_kw: float) -> float:
    """Return the effective unit price for EV charging in one slot."""
    effective_price, _solar_kw, _grid_kw = _charge_cost_components(slot, charge_kw)
    if effective_price is not None:
        return effective_price
    return float(slot.import_price)


def _charge_cost_components(slot: Any, charge_kw: float) -> tuple[float | None, float, float]:
    """Return effective price plus solar/grid split for one charging slot."""
    if charge_kw <= 0:
        return None, 0.0, 0.0
    import_price = _float_or_none(getattr(slot, "import_price", None))
    if import_price is None:
        return None, 0.0, round(charge_kw, 6)
    surplus_kw = _solar_surplus_kw(slot)
    solar_kw = min(charge_kw, surplus_kw)
    grid_kw = max(charge_kw - solar_kw, 0.0)
    export_price = _float_or_none(getattr(slot, "export_price", None)) or 0.0
    effective_price = ((solar_kw * export_price) + (grid_kw * import_price)) / charge_kw
    return round(effective_price, 6), round(solar_kw, 6), round(grid_kw, 6)


def _solar_surplus_kw(slot: Any) -> float:
    """Return forecast PV surplus available for flexible EV charging."""
    pv_value = getattr(slot, "pv_forecast_lower_kw", None)
    load_value = getattr(slot, "baseline_load_forecast_upper_kw", None)
    pv_kw = _float_or_none(getattr(slot, "pv_forecast_kw", None) if pv_value is None else pv_value)
    load_kw = _float_or_none(getattr(slot, "baseline_load_forecast_kw", None) if load_value is None else load_value)
    if pv_kw is None or load_kw is None:
        return 0.0
    existing_flexible_load_kw = (_float_or_none(getattr(slot, "projected_hvac_load_kw", None)) or 0.0) + (
        _float_or_none(getattr(slot, "projected_ev_load_kw", None)) or 0.0
    )
    return round(max(pv_kw - load_kw - existing_flexible_load_kw, 0.0), 6)


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
        if "," in value and "." not in value:
            value = value.replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trip_history_events(connected_states: list[Any], soc_states: list[Any]) -> list[tuple[datetime, str, Any]]:
    events: list[tuple[datetime, str, Any]] = []
    for state in soc_states:
        timestamp = _state_timestamp(state)
        soc = _float_or_none(getattr(state, "state", None))
        if timestamp is not None and soc is not None:
            events.append((timestamp, "soc", soc))
    for state in connected_states:
        timestamp = _state_timestamp(state)
        connected = _connected_bool(getattr(state, "state", None))
        if timestamp is not None and connected is not None:
            events.append((timestamp, "connected", connected))
    return sorted(events, key=lambda event: (event[0], 0 if event[1] == "soc" else 1))


def _state_timestamp(state: Any) -> datetime | None:
    for attr in ("last_changed", "last_updated"):
        value = getattr(state, attr, None)
        if isinstance(value, datetime):
            return value
    return None


def _connected_bool(value: Any) -> bool | None:
    normalized = str(value).lower().strip().replace(" ", "_").replace("-", "_")
    if normalized in {
        "on",
        "true",
        "1",
        "connected",
        "charging",
        "home",
        "yes",
        "plugged_in",
        "plugged",
        "vehicle_connected",
        "charger_connected",
        "connected_not_charging",
        "plugged_in_not_charging",
        "fully_charged",
        "charge_complete",
        "charging_complete",
        "ready",
        "present",
    }:
        return True
    if normalized in {
        "off",
        "false",
        "0",
        "disconnected",
        "not_connected",
        "not_home",
        "idle",
        "no",
        "unplugged",
        "plugged_out",
        "vehicle_disconnected",
        "charger_disconnected",
        "vehicle_not_connected",
        "charger_not_connected",
        "not_plugged",
        "not_plugged_in",
        "away",
    }:
        return False
    return None


def _record_key(record: dict[str, Any]) -> tuple[str, str, float | None, float | None]:
    return (
        str(record.get("started_at", "")),
        str(record.get("ended_at", "")),
        _float_or_none(record.get("start_soc_percent")),
        _float_or_none(record.get("end_soc_percent")),
    )
