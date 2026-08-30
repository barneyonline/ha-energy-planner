"""EV charging-state, calibration, and scheduling helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, isfinite
from typing import Any

EV_CHARGE_CALIBRATION_MODEL_VERSION = 1
MIN_EV_CHARGE_CALIBRATION_MINUTES = 30
MIN_EV_CHARGE_CALIBRATION_SOC_GAIN = 3.0
MIN_EV_CHARGE_CALIBRATION_TOTAL_MINUTES = 60
EV_CHARGE_CALIBRATION_SAFETY_FACTOR = 0.9
MAX_EV_CHARGE_CALIBRATION_SAMPLES = 60

_EV_ACTIVE_CHARGING_STATES = frozenset(
    {
        "on",
        "true",
        "1",
        "charging",
        "finishing",
    }
)
_EV_INACTIVE_CHARGING_STATES = frozenset(
    {
        "off",
        "false",
        "0",
        "idle",
        "not_charging",
        "connected_not_charging",
        "fully_charged",
        "available",
        "preparing",
        "reserved",
        "unavailable",
        "faulted",
        "plugged",
        "connected",
        "occupied",
        "suspended",
        "suspended_ev",
        "suspended_evse",
        "disconnected",
        "unplugged",
        "not_plugged_in",
    }
)
_EV_SAFE_CONNECTED_INACTIVE_STATES = frozenset(
    {
        "off",
        "false",
        "0",
        "idle",
        "not_charging",
        "connected_not_charging",
        "fully_charged",
        "suspended",
        "suspended_ev",
        "suspended_evse",
    }
)


def ev_charging_state(value: object) -> bool | None:
    """Normalize charger feedback into active or inactive power delivery."""
    normalized = str(value).strip().lower()
    if normalized in _EV_ACTIVE_CHARGING_STATES:
        return True
    if normalized in _EV_INACTIVE_CHARGING_STATES:
        return False
    return None


def ev_charging_state_proves_safe(value: object) -> bool:
    """Return whether feedback proves no charging while connection may remain."""
    return str(value).strip().lower() in _EV_SAFE_CONNECTED_INACTIVE_STATES


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
    allocation_source: str = "ready_by"


@dataclass(slots=True)
class EVChargeSchedule:
    """Least-cost EV charging schedule."""

    allocations: list[EVChargeAllocation]
    target_soc_percent: float
    scheduled_soc_percent: float
    required_charge_percent: float
    infeasible: bool
    reason: str


def build_ev_charge_calibration(
    charging_states: list[Any],
    soc_states: list[Any],
    *,
    charge_rate_kw: float,
    trained_at: datetime,
    charging_entity_id: str,
    soc_entity_id: str,
) -> dict[str, Any]:
    """Build a conservative effective SOC-per-kWh model from charging sessions."""
    samples = _ev_charge_calibration_samples(
        charging_states,
        soc_states,
        charge_rate_kw=charge_rate_kw,
    )
    total_duration_minutes = sum(float(sample["duration_minutes"]) for sample in samples)
    total_energy_kwh = sum(float(sample["estimated_energy_kwh"]) for sample in samples)
    total_soc_gain = sum(float(sample["soc_gain_percent"]) for sample in samples)
    ready = bool(
        total_duration_minutes >= MIN_EV_CHARGE_CALIBRATION_TOTAL_MINUTES
        and total_energy_kwh > 0
        and total_soc_gain >= MIN_EV_CHARGE_CALIBRATION_SOC_GAIN
    )
    raw_soc_per_kwh = total_soc_gain / total_energy_kwh if total_energy_kwh > 0 else None
    learned_soc_per_kwh = (
        round(raw_soc_per_kwh * EV_CHARGE_CALIBRATION_SAFETY_FACTOR, 4)
        if ready and raw_soc_per_kwh is not None
        else None
    )
    return {
        "model_version": EV_CHARGE_CALIBRATION_MODEL_VERSION,
        "status": "ready" if ready else "insufficient_history",
        "trained_at": trained_at.isoformat(),
        "charging_entity_id": charging_entity_id,
        "soc_entity_id": soc_entity_id,
        "charge_rate_kw": round(float(charge_rate_kw), 4),
        "sample_count": len(samples),
        "total_duration_minutes": round(total_duration_minutes, 3),
        "total_soc_gain_percent": round(total_soc_gain, 3),
        "raw_soc_per_kwh": round(raw_soc_per_kwh, 4) if raw_soc_per_kwh is not None else None,
        "soc_per_kwh": learned_soc_per_kwh,
        "safety_factor": EV_CHARGE_CALIBRATION_SAFETY_FACTOR,
        "samples": samples[-MAX_EV_CHARGE_CALIBRATION_SAMPLES:],
    }


def effective_ev_soc_per_kwh(
    calibration: dict[str, Any] | None,
    fallback_soc_per_kwh: float,
    *,
    charging_entity_id: str | None,
    soc_entity_id: str | None,
    charge_rate_kw: float,
) -> tuple[float, str]:
    """Return the learned effective rate or the conservative bootstrap fallback."""
    model = calibration if isinstance(calibration, dict) else {}
    learned = _float_or_none(model.get("soc_per_kwh"))
    if (
        ev_charge_calibration_matches(
            model,
            charging_entity_id=charging_entity_id,
            soc_entity_id=soc_entity_id,
            charge_rate_kw=charge_rate_kw,
        )
        and model.get("status") == "ready"
        and learned is not None
        and learned > 0
    ):
        return learned, "recorder_charging_history"
    fallback = _float_or_none(fallback_soc_per_kwh)
    return (fallback if fallback is not None and fallback > 0 else 2.0), "configured_fallback"


def ev_charge_calibration_matches(
    calibration: dict[str, Any] | None,
    *,
    charging_entity_id: str | None,
    soc_entity_id: str | None,
    charge_rate_kw: float,
) -> bool:
    """Return whether a learned model belongs to the current EV configuration."""
    model = calibration if isinstance(calibration, dict) else {}
    expected_charging_entity = str(charging_entity_id or "").strip()
    expected_soc_entity = str(soc_entity_id or "").strip()
    if not expected_charging_entity or not expected_soc_entity:
        return False
    stored_charge_rate = _float_or_none(model.get("charge_rate_kw"))
    expected_charge_rate = _float_or_none(charge_rate_kw)
    return bool(
        model.get("model_version") == EV_CHARGE_CALIBRATION_MODEL_VERSION
        and model.get("charging_entity_id") == expected_charging_entity
        and model.get("soc_entity_id") == expected_soc_entity
        and stored_charge_rate is not None
        and expected_charge_rate is not None
        and isfinite(stored_charge_rate)
        and isfinite(expected_charge_rate)
        and stored_charge_rate > 0
        and expected_charge_rate > 0
        and abs(stored_charge_rate - expected_charge_rate) < 0.0001
    )


def _ev_charge_calibration_samples(
    charging_states: list[Any],
    soc_states: list[Any],
    *,
    charge_rate_kw: float,
) -> list[dict[str, Any]]:
    if charge_rate_kw <= 0:
        return []
    charging_events = sorted(
        (
            (timestamp, charging)
            for state in charging_states
            if (timestamp := _state_timestamp(state)) is not None
            and (charging := ev_charging_state(getattr(state, "state", None))) is not None
        ),
        key=lambda item: item[0],
    )
    soc_points = sorted(
        (
            (timestamp, soc)
            for state in soc_states
            if (timestamp := _state_timestamp(state)) is not None
            and (soc := _float_or_none(getattr(state, "state", None))) is not None
            and 0 <= soc <= 100
        ),
        key=lambda item: item[0],
    )
    active_start: datetime | None = None
    samples: list[dict[str, Any]] = []
    for timestamp, charging in charging_events:
        if charging:
            active_start = active_start or timestamp
            continue
        if active_start is None or timestamp <= active_start:
            active_start = None
            continue
        duration_minutes = (timestamp - active_start).total_seconds() / 60.0
        start_soc = _soc_at_or_before(soc_points, active_start)
        end_soc = _soc_at_or_before(soc_points, timestamp)
        active_start_value = active_start
        active_start = None
        if (
            duration_minutes < MIN_EV_CHARGE_CALIBRATION_MINUTES
            or duration_minutes > 12 * 60
            or start_soc is None
            or end_soc is None
        ):
            continue
        soc_gain = end_soc - start_soc
        if soc_gain < MIN_EV_CHARGE_CALIBRATION_SOC_GAIN:
            continue
        energy_kwh = charge_rate_kw * duration_minutes / 60.0
        soc_per_kwh = soc_gain / energy_kwh if energy_kwh > 0 else 0.0
        if not 0.1 <= soc_per_kwh <= 10.0:
            continue
        samples.append(
            {
                "started_at": active_start_value.isoformat(),
                "ended_at": timestamp.isoformat(),
                "duration_minutes": round(duration_minutes, 3),
                "start_soc_percent": round(start_soc, 3),
                "end_soc_percent": round(end_soc, 3),
                "soc_gain_percent": round(soc_gain, 3),
                "estimated_energy_kwh": round(energy_kwh, 4),
                "soc_per_kwh": round(soc_per_kwh, 4),
            }
        )
    return samples[-MAX_EV_CHARGE_CALIBRATION_SAMPLES:]


def _soc_at_or_before(
    points: list[tuple[datetime, float]],
    timestamp: datetime,
) -> float | None:
    point = next(((instant, value) for instant, value in reversed(points) if instant <= timestamp), None)
    if point is None or timestamp - point[0] > timedelta(minutes=15):
        return None
    return point[1]


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
    continue_current: bool = False,
    max_import_price: float | None = None,
    allocation_source: str = "ready_by",
) -> EVChargeSchedule:
    """Allocate EV charging to cheapest feasible slots before ready-by.

    Slot ranking is solar-aware: surplus PV is valued at the foregone feed-in
    price, and any remaining charge power is valued at the grid import price.
    A forced current slot may bypass ``earliest_start`` for immediate minimum-
    SOC recovery or opportunistic low-price charging; all later slots continue
    to honour the configured charging window. An observed continuous charging
    session may also retain its current pre-window slot, but it never bypasses
    the maximum import price.
    """
    required = max(target_soc_percent - current_soc_percent, 0.0)
    if required == 0:
        return EVChargeSchedule([], target_soc_percent, current_soc_percent, 0.0, False, "already_at_target")

    soc_per_slot = max(charge_rate_kw, 0.0) * (interval_minutes / 60.0) * max(soc_per_kwh, 0.0)
    if soc_per_slot <= 0:
        return EVChargeSchedule([], target_soc_percent, current_soc_percent, required, True, "ev_charge_rate_invalid")

    candidate_slots = [
        slot
        for slot in slots
        if slot.valid_at < ready_by and slot.import_price is not None
    ]
    current_slot = min(candidate_slots, key=lambda slot: slot.valid_at, default=None)
    regular_slots = [
        slot
        for slot in candidate_slots
        if earliest_start is None or slot.valid_at >= earliest_start
    ]
    current_outside_window = bool(
        (force_current or continue_current)
        and current_slot is not None
        and earliest_start is not None
        and current_slot.valid_at < earliest_start
    )
    feasible_slots = (
        [current_slot, *regular_slots]
        if current_outside_window and current_slot is not None
        else regular_slots
    )
    price_eligible = [
        slot
        for slot in feasible_slots
        if max_import_price is None
        or float(slot.import_price) <= max_import_price
        or (force_current and slot is current_slot)
    ]
    anchor_current = bool(
        force_current
        or (
            continue_current
            and current_slot is not None
            and current_slot in price_eligible
        )
    )
    ranked = _rank_charging_slots(price_eligible, charge_rate_kw, carbon_weight)
    if continuous:
        required_slots = ceil(required / soc_per_slot)
        if current_outside_window and current_slot in price_eligible:
            regular_price_eligible = [slot for slot in price_eligible if slot is not current_slot]
            regular_ranked = [slot for slot in ranked if slot is not current_slot]
            ordered = [
                current_slot,
                *_best_continuous_slots(
                    regular_price_eligible,
                    regular_ranked,
                    required_slots=max(required_slots - 1, 0),
                    interval_minutes=interval_minutes,
                    force_current=False,
                ),
            ]
        else:
            ordered = _best_continuous_slots(
                price_eligible,
                ranked,
                required_slots=required_slots,
                interval_minutes=interval_minutes,
                force_current=anchor_current,
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
                allocation_source=allocation_source,
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
    if windows:
        return min(
            windows,
            key=lambda window: (
                sum(rank[slot.valid_at] for slot in window),
                window[0].valid_at,
            ),
        )

    # Missing/price-ineligible slots can make a full continuous window
    # impossible. Return the best contiguous partial run so allocation is
    # explicitly infeasible instead of silently cycling across gaps.
    runs: list[list[Any]] = []
    for slot in chronological:
        if (
            not runs
            or (slot.valid_at - runs[-1][-1].valid_at).total_seconds()
            != interval_minutes * 60
        ):
            runs.append([slot])
        else:
            runs[-1].append(slot)
    if force_current:
        runs = [run for run in runs if run[0] is chronological[0]]
    candidates = [run[:required_slots] for run in runs if run]
    return min(
        candidates,
        key=lambda run: (
            -len(run),
            sum(rank[slot.valid_at] for slot in run),
            run[0].valid_at,
        ),
        default=[],
    )


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


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
        if "," in value and "." not in value:
            value = value.replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_timestamp(state: Any) -> datetime | None:
    for attr in ("last_changed", "last_updated"):
        value = getattr(state, attr, None)
        if isinstance(value, datetime):
            return value
    return None
