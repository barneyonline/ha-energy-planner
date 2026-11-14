"""Forecast helpers for Energy Planner."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from operator import itemgetter
from typing import Any

from .models import ForecastPoint

_FORECAST_CONTAINER_KEYS = (
    "forecast",
    "forecasts",
    "data",
    "values",
    "detailedForecast",
    "detailed_forecast",
    "predictions",
)
_TIME_KEYS = ("valid_at", "datetime", "start_time", "period_start", "from", "time", "date", "nem_time")
FORECAST_HEALTHY_HOURS = 12.0
FORECAST_DEGRADED_HOURS = 8.0
MAX_FORECAST_SOURCE_ROWS = 2048
_TARIFF_VALUE_KEYS = ("import_price", "general_price", "export_price", "feed_in_price", "per_kwh", "price", "value")


@dataclass(slots=True)
class ForecastSource:
    """Validated source rows independent of the requested planning grid."""

    declared: tuple[list[tuple[datetime, datetime, float]], int] | None
    points: list[tuple[datetime, float]]
    ordered: list[float | None]
    cadence: timedelta
    ordered_interval: float


def parse_forecast_source(
    state: Any, interval_minutes: int, value_keys: tuple[str, ...], value_kind: str, *,
    unit_key_only: bool = False,
) -> ForecastSource:
    """Normalize once before sampling on one or more grids."""
    attributes = _with_canonical_keys(getattr(state, "attributes", {}) or {})
    raw_items = _forecast_items(attributes, value_keys)
    default_unit = str(attributes.get("unit_of_measurement", "" if unit_key_only else
                                     attributes.get("unit", attributes.get("temperature_unit", ""))))
    explicit_interval = _explicit_interval_minutes(attributes)
    declared = (_declared_tariff_intervals(raw_items, value_keys, default_unit, maximum_minutes=explicit_interval)
                if value_kind == "price" and raw_items else None)
    if declared is not None:
        return ForecastSource(declared, [], [], timedelta(), float(interval_minutes))
    if value_kind == "power":
        raw_items = _energy_items_as_average_power(raw_items, value_keys, default_unit)
    parsed = [_parse_item(item, value_keys=value_keys, value_kind=value_kind, default_unit=default_unit)
              for item in raw_items]
    points = sorted(
        [(_as_aware_utc(item[0]), item[1]) for item in parsed if item is not None and item[0] is not None],
        key=itemgetter(0),
    )
    explicit_cadence = timedelta(minutes=explicit_interval) if explicit_interval is not None else None
    cadence = _conservative_cadence(
        points, explicit_cadence or timedelta(minutes=interval_minutes), maximum=explicit_cadence,
    )
    ordered = [item[1] if item is not None else None for item in parsed] if not points and any(parsed) else []
    return ForecastSource(None, points, ordered, cadence, explicit_interval or float(interval_minutes))


def parse_tariff_source(state: Any, interval_minutes: int) -> ForecastSource:
    """Normalize source intervals using the planning grid's price key precedence."""
    return parse_forecast_source(state, interval_minutes, _TARIFF_VALUE_KEYS, "price", unit_key_only=True)


def constant_forecast(
    *,
    issued_at: datetime,
    source: str,
    value: float,
    unit: str,
    horizon_hours: int,
    interval_minutes: int,
    confidence: float | None = None,
) -> list[ForecastPoint]:
    """Create a simple constant forecast series from a point sensor."""
    fresh_until = issued_at + timedelta(minutes=interval_minutes)
    return [
        ForecastPoint(
            issued_at=issued_at,
            valid_at=issued_at + timedelta(minutes=offset),
            source=source,
            value=value,
            unit=unit,
            confidence=confidence,
            fresh_until=fresh_until,
        )
        for offset in range(0, horizon_hours * 60, interval_minutes)
    ]


def tariff_source_intervals(
    state: Any, interval_minutes: int, *, details: dict[str, Any] | None = None,
    slot_starts: list[datetime] | None = None,
    source: ForecastSource | None = None,
) -> list[dict[str, Any]]:
    """Expose sanitized source boundaries used to identify tariff lifecycles."""
    source = source if source is not None else parse_tariff_source(state, interval_minutes)
    declared = source.declared
    if declared is not None:
        intervals, normalized = declared
        if details is not None:
            details.update(source_validity="declared_intervals" if intervals else "invalid_intervals",
                           normalized_intervals=normalized)
        return [{"start": start.isoformat(), "end": end.isoformat(), "price": price}
                for start, end, price in _tariff_window(intervals, slot_starts)]
    if details is not None:
        details.update(source_validity="inferred_intervals", normalized_intervals=0)
    intervals = [(at, at + source.cadence, price) for at, price in sorted(set(source.points))]
    return [{"start": start.isoformat(), "end": end.isoformat(), "price": price}
            for start, end, price in _tariff_window(intervals, slot_starts)]


def _tariff_window(
    intervals: list[tuple[datetime, datetime, float]], starts: list[datetime] | None,
) -> list[tuple[datetime, datetime, float]]:
    """Bound published boundaries after validating the complete source evidence."""
    if starts and len(intervals) > 576:
        window_start = min(_as_aware_utc(start) for start in starts)
        window_end = max(_as_aware_utc(start) for start in starts)
        intervals = [row for row in intervals if row[1] > window_start and row[0] <= window_end]
    return intervals[:576]


def forecast_series_from_state(
    state: Any,
    *,
    issued_at: datetime,
    horizon_hours: int,
    interval_minutes: int,
    value_keys: tuple[str, ...],
    value_kind: str,
    require_timestamped: bool = False,
    slot_starts: list[datetime] | None = None,
    source: ForecastSource | None = None,
) -> list[float | None] | None:
    """Return a slot-aligned numeric forecast series from common HA attributes.

    Supports two common integration shapes:
    - list attributes such as ``forecast`` or ``forecasts`` with timestamped dicts
    - list attributes with ordered numeric values and no timestamps

    Set ``require_timestamped`` for freshness evidence: ordered values have no
    source interval and must not be treated as proof of current coverage.
    """
    source = source if source is not None else parse_forecast_source(state, interval_minutes, value_keys, value_kind)
    if source.declared is None and not source.points and not source.ordered:
        return None
    issued_at = _as_aware_utc(issued_at)
    starts = slot_starts if slot_starts is not None else [
        issued_at + timedelta(minutes=offset) for offset in range(0, horizon_hours * 60, interval_minutes)
    ]
    if source.declared is not None:
        intervals, _normalized = source.declared
        return [_value_for_tariff_slot(at, intervals) for at in starts]
    if not source.points:
        if require_timestamped:
            return None
        # Ordered payloads have no temporal metadata. Treat each source position as
        # exactly one planner interval and retain invalid positions as gaps. This is
        # deliberately conservative: inventing a longer cadence would silently
        # extend a short forecast across the rest of the planning horizon.
        source_interval = source.ordered_interval
        if slot_starts is None:
            return _align_ordered_values(source.ordered, len(starts), interval_minutes, source_interval)
        return [
            source.ordered[index] if 0 <= (index := int((at - issued_at).total_seconds() / 60 / source_interval))
            < len(source.ordered) else None for at in starts
        ]
    return [_value_for_slot(at, source.points, final_cadence=source.cadence) for at in starts]


def _amber_half_hour_start(start: datetime, end: datetime | None, item: dict[str, Any]) -> datetime:
    """Translate only Amber's documented, duration-backed first-second convention."""
    # public-api/swagger.json: duration=30, startTime=02:00:01Z, endTime=02:30:00Z.
    # This is schema evidence, not permission to round arbitrary timestamps.
    if (
        end is not None
        and type(item.get("duration")) in (int, float)
        and item["duration"] == 30
        and (end.minute % 30, end.second, end.microsecond) == (0, 0, 0)
        and end - start == timedelta(minutes=30, seconds=-1)
    ):
        return start - timedelta(seconds=1)
    return start


def _declared_tariff_intervals(
    raw_items: list[Any], value_keys: tuple[str, ...], default_unit: str, *, maximum_minutes: float | None,
) -> tuple[list[tuple[datetime, datetime, float]], int] | None:
    """Honor Amber interval metadata; reject broken native evidence without fallback.

    Timestamp-only and other provider schemas retain their existing parser. Once
    an Amber per_kwh source declares interval metadata, every row must validate.
    Real gaps remain gaps and explicit ends cannot be extended by inferred cadence.
    """
    items = [_flatten_item(item) if isinstance(item, dict) else {} for item in raw_items]
    if not any("per_kwh" in item and ("duration" in item or "end_time" in item) for item in items):
        return None
    intervals: list[tuple[datetime, datetime, float]] = []
    normalized = 0
    for item in items:
        start = _parse_datetime_or_none(item.get("start_time"))
        end = _parse_datetime_or_none(item.get("end_time"))
        duration = item.get("duration")
        parsed = _parse_item(item, value_keys=value_keys, value_kind="price", default_unit=default_unit)
        if (
            start is None or end is None or start.utcoffset() is None or end.utcoffset() is None
            or type(duration) not in (int, float) or duration not in (5, 15, 30)
            or type(item.get("per_kwh")) not in (int, float) or parsed is None or parsed[0] != start
        ):
            return [], 0
        start, end = _as_aware_utc(start), _as_aware_utc(end)
        canonical_start = _amber_half_hour_start(start, end, item)
        if (end - canonical_start != timedelta(minutes=duration)
                or (maximum_minutes is not None and duration > maximum_minutes)):
            return [], 0
        normalized += canonical_start != start
        intervals.append((canonical_start, end, parsed[1]))
    intervals.sort(key=itemgetter(0))
    if any(right[0] < left[1] for left, right in zip(intervals, intervals[1:], strict=False)):
        return [], 0
    return intervals, normalized


def _value_for_tariff_slot(at: datetime, intervals: list[tuple[datetime, datetime, float]]) -> float | None:
    """Sample half-open declared intervals without bridging their gaps or tail."""
    position = bisect_right(intervals, at, key=itemgetter(0)) - 1
    if position < 0 or at >= intervals[position][1]:
        return None
    return intervals[position][2]


def forecast_coverage_ratio(series: list[float | None] | None) -> float:
    """Return the fraction of requested forecast slots that contain values."""
    if not series:
        return 0.0
    return sum(value is not None for value in series) / len(series)


def forecast_coverage_details(
    series: list[float | None] | None,
    *,
    starts_at: datetime,
    interval_minutes: int,
    slot_starts: list[datetime] | None = None,
    horizon_end: datetime | None = None,
) -> dict[str, Any]:
    """Return bounded temporal coverage evidence for one aligned forecast.

    Classification uses continuous coverage from the first planner slot. This
    deliberately treats leading or internal gaps as a safety boundary rather
    than allowing later values to make an immediately unusable forecast look
    healthy. Thresholds are capped by a shorter configured horizon so existing
    installations that intentionally plan for less than eight hours continue to
    require complete coverage of that horizon.
    """
    values = list(series or [])
    slot_count = len(values)
    present = [index for index, value in enumerate(values) if value is not None]
    first_index = present[0] if present else None
    last_index = present[-1] if present else None
    leading_missing_slots = first_index if first_index is not None else slot_count
    trailing_missing_slots = slot_count - last_index - 1 if last_index is not None else slot_count
    continuous_slots = 0
    for value in values:
        if value is None:
            break
        continuous_slots += 1
    starts = slot_starts if slot_starts is not None else [
        starts_at + timedelta(minutes=i * interval_minutes) for i in range(slot_count)
    ]
    durations = [
        ((starts[i + 1] if i + 1 < slot_count else horizon_end or at + timedelta(minutes=interval_minutes))
         - at) for i, at in enumerate(starts)
    ]
    longest_duration = current_duration = timedelta()
    for i, value in enumerate(values):
        current_duration = current_duration + durations[i] if value is not None else timedelta()
        longest_duration = max(longest_duration, current_duration)
    # Sum timedeltas at datetime precision before converting to hours. A
    # one-second tariff split must not turn exactly eight hours into < 8.
    requested_hours = sum(durations, timedelta()).total_seconds() / 3600
    covered_hours = sum((durations[i] for i in present), timedelta()).total_seconds() / 3600
    continuous_hours = sum(durations[:continuous_slots], timedelta()).total_seconds() / 3600
    healthy_threshold = min(FORECAST_HEALTHY_HOURS, requested_hours)
    degraded_threshold = min(FORECAST_DEGRADED_HOURS, requested_hours)
    if slot_count and continuous_hours >= healthy_threshold:
        classification = "healthy"
    elif slot_count and continuous_hours >= degraded_threshold:
        classification = "degraded"
    else:
        classification = "unsafe"

    starts_at = _as_aware_utc(starts_at)
    return {
        "classification": classification,
        "first_timestamp": None
        if first_index is None
        else starts[first_index].isoformat(),
        "last_timestamp": None
        if last_index is None
        else starts[last_index].isoformat(),
        "covered_hours": round(covered_hours, 4),
        "coverage_ratio": covered_hours / requested_hours if requested_hours > 0 else 0.0,
        "continuous_hours": round(continuous_hours, 4),
        "longest_continuous_hours": round(longest_duration.total_seconds() / 3600, 4),
        "leading_missing_slots": leading_missing_slots,
        "trailing_missing_slots": trailing_missing_slots,
        "internal_missing_slots": max(slot_count - len(present) - leading_missing_slots - trailing_missing_slots, 0),
        "requested_hours": round(requested_hours, 4),
        "healthy_threshold_hours": round(healthy_threshold, 4),
        "degraded_threshold_hours": round(degraded_threshold, 4),
    }


def _explicit_interval_minutes(attributes: dict[str, Any]) -> float | None:
    """Return a valid explicitly declared forecast cadence in minutes."""
    for key in ("forecast_interval_minutes", "interval_minutes", "resolution_minutes"):
        raw_value = attributes.get(key)
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if isfinite(value) and value > 0:
            return value
    return None


def _align_ordered_values(
    values: list[float | None],
    slot_count: int,
    planner_interval_minutes: int,
    source_interval_minutes: float,
) -> list[float | None]:
    """Align ordered buckets without extending beyond their declared coverage."""
    series: list[float | None] = []
    for index in range(slot_count):
        elapsed = index * planner_interval_minutes
        source_index = int(elapsed // source_interval_minutes)
        if source_index >= len(values):
            series.append(None)
            continue
        series.append(values[source_index])
    return series


def latest_forecast_valid_at_from_state(
    state: Any,
    *,
    value_keys: tuple[str, ...],
) -> datetime | None:
    """Return the latest timestamp found in a forecast payload."""
    attributes = _with_canonical_keys(getattr(state, "attributes", {}) or {})
    raw_items = _forecast_items(attributes, value_keys)
    latest: datetime | None = None
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        flattened = _flatten_item(item)
        valid_at = None
        for key in _TIME_KEYS:
            if key not in flattened:
                continue
            valid_at = _parse_datetime_or_none(flattened[key])
            if valid_at is not None:
                break
        if valid_at is None:
            continue
        valid_at = _as_aware_utc(valid_at)
        if latest is None or valid_at > latest:
            latest = valid_at
    return latest


def forecast_timestamp_status_from_state(
    state: Any,
    *,
    value_keys: tuple[str, ...],
) -> str:
    """Return whether forecast timestamps are present and timezone-aware.

    A secondary series cannot be placed safely without absolute timestamps.
    Naive timestamps are rejected rather than assuming UTC or the Home
    Assistant timezone, which could shift values across midnight or DST.
    """
    attributes = _with_canonical_keys(getattr(state, "attributes", {}) or {})
    raw_items = _forecast_items(attributes, value_keys)
    found = False
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        flattened = _flatten_item(item)
        for key in _TIME_KEYS:
            if key not in flattened:
                continue
            valid_at = _parse_datetime_or_none(flattened[key])
            if valid_at is None:
                continue
            found = True
            if valid_at.tzinfo is None or valid_at.utcoffset() is None:
                return "naive_timestamps"
            break
    return "aware_timestamps" if found else "untimestamped"


def normalize_scalar_value(value: float, *, value_kind: str, value_key: str = "", unit: str = "") -> float:
    """Normalize a scalar forecast/input value into planner units."""
    if value_kind == "temperature":
        return _normalize_temperature_value(value, unit)
    if value_kind == "price":
        return _normalize_price_value(value, unit)
    if value_kind == "power":
        return _normalize_power_value(value, value_key=value_key, unit=unit)
    if value_kind == "carbon_intensity":
        return _normalize_carbon_intensity_value(value, unit)
    return value


def _forecast_items(attributes: dict[str, Any], value_keys: tuple[str, ...]) -> list[Any]:
    attributes = _with_canonical_keys(attributes)
    containers: tuple[str, ...] = _FORECAST_CONTAINER_KEYS
    if "per_kwh" in value_keys or any("price" in key for key in value_keys):
        # Provider-native tariff blocks take precedence over a convenience
        # forecast that may already have been resampled on a rolling grid.
        containers = ("detailedForecast", "detailed_forecast", *containers)
    for key in containers:
        value = attributes.get(key)
        items = _items_from_value(value, value_keys)
        if items:
            return items
    for key in value_keys:
        items = _items_from_value(attributes.get(key), value_keys, map_value_key=key)
        if items:
            return items
    return []


def _items_from_value(
    value: Any,
    value_keys: tuple[str, ...],
    depth: int = 0,
    map_value_key: str | None = None,
) -> list[Any]:
    if value is None or depth > 4:
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    value = _with_canonical_keys(value)

    mapped_items = _items_from_time_map(value, value_keys, map_value_key)
    if mapped_items:
        return mapped_items

    for key in _FORECAST_CONTAINER_KEYS:
        items = _items_from_value(value.get(key), value_keys, depth + 1)
        if items:
            return items
    return []


def _items_from_time_map(value: dict[str, Any], value_keys: tuple[str, ...], map_value_key: str | None) -> list[Any]:
    items: list[dict[str, Any]] = []
    value_key = map_value_key or value_keys[0]
    for key, raw_value in value.items():
        valid_at = _parse_datetime_or_none(key)
        if valid_at is None:
            continue
        if isinstance(raw_value, dict):
            item = dict(raw_value)
            item.setdefault("valid_at", key)
        else:
            item = {"valid_at": key, value_key: raw_value}
        items.append(item)
    return items


def _parse_item(
    item: Any,
    *,
    value_keys: tuple[str, ...],
    value_kind: str,
    default_unit: str = "",
) -> tuple[datetime | None, float] | None:
    if isinstance(item, (int, float, str)):
        try:
            value = float(item)
        except (TypeError, ValueError):
            return None
        if not isfinite(value):
            return None
        return None, normalize_scalar_value(value, value_kind=value_kind, unit=default_unit)
    if not isinstance(item, dict):
        return None

    item = _flatten_item(item)
    value_key = next((key for key in value_keys if key in item), None)
    if value_key is None:
        return None
    try:
        value = float(item[value_key])
    except (TypeError, ValueError):
        return None
    if not isfinite(value):
        return None
    unit = _forecast_item_unit(item, value_key, default_unit)
    value = normalize_scalar_value(value, value_kind=value_kind, value_key=value_key, unit=unit)

    valid_at = None
    for key in _TIME_KEYS:
        if key not in item:
            continue
        valid_at = _parse_datetime_or_none(item[key])
        if valid_at is not None:
            break
    return valid_at, value


def _forecast_item_unit(item: dict[str, Any], value_key: str | None, default_unit: str) -> str:
    """Use Solcast power-field units independently of the daily energy sensor."""
    # Solcast's daily sensor is kWh, but its interval estimates are average kW.
    # Explicit item units still take precedence for other forecast producers.
    field_unit = (
        "kW"
        if value_key in {"pv_estimate", "pv_estimate10", "pv_estimate90"}
        and _normalize_unit(default_unit) in _ENERGY_UNITS
        else default_unit
    )
    return str(item.get("unit", item.get("units", item.get("unit_of_measurement", field_unit))))


def _energy_items_as_average_power(
    raw_items: list[Any],
    value_keys: tuple[str, ...],
    default_unit: str,
) -> list[Any]:
    """Convert energy forecast buckets into average kW power values."""
    prepared: list[tuple[Any, dict[str, Any] | None, datetime | None]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            prepared.append((item, None, None))
            continue
        flattened = _flatten_item(item)
        valid_at = None
        for key in _TIME_KEYS:
            if key not in flattened:
                continue
            valid_at = _parse_datetime_or_none(flattened[key])
            if valid_at is not None:
                break
        prepared.append((item, flattened, valid_at))

    timestamps = [valid_at for _item, _flattened, valid_at in prepared if valid_at is not None]
    inferred_hours = _infer_bucket_hours(timestamps)
    converted: list[Any] = []
    for item, prepared_flattened, valid_at in prepared:
        if prepared_flattened is None:
            converted.append(item)
            continue
        value_key = next((key for key in value_keys if key in prepared_flattened), None)
        unit = _forecast_item_unit(prepared_flattened, value_key, default_unit)
        if value_key is None or _normalize_unit(unit) not in _ENERGY_UNITS:
            converted.append(item)
            continue
        bucket_hours = inferred_hours.get(valid_at, 1.0)
        if bucket_hours <= 0:
            converted.append(item)
            continue
        try:
            energy = float(prepared_flattened[value_key])
        except (TypeError, ValueError):
            converted.append(item)
            continue
        if not isfinite(energy):
            converted.append(item)
            continue
        converted_item = dict(item)
        converted_item[value_key] = _energy_to_kwh(energy, unit) / bucket_hours
        converted_item["unit"] = "kW"
        converted.append(converted_item)
    return converted


def _infer_bucket_hours(timestamps: list[datetime]) -> dict[datetime | None, float]:
    """Infer forecast bucket durations from adjacent timestamps."""
    if not timestamps:
        return {None: 1.0}
    ordered = sorted(timestamps)
    durations: dict[datetime | None, float] = {}
    previous_duration = 1.0
    for index, valid_at in enumerate(ordered):
        if index + 1 < len(ordered):
            duration = (ordered[index + 1] - valid_at).total_seconds() / 3600
            if duration > 0:
                previous_duration = duration
        durations[valid_at] = previous_duration
    durations[None] = previous_duration
    return durations


def _energy_to_kwh(value: float, unit: str) -> float:
    unit_lower = _normalize_unit(unit)
    if unit_lower in {"wh", "watt-hour", "watthour", "watt-hours", "watthours"}:
        return value / 1000
    if unit_lower in {"mwh", "megawatt-hour", "megawatthour", "megawatt-hours", "megawatthours"}:
        return value * 1000
    return value


def _flatten_item(item: dict[str, Any]) -> dict[str, Any]:
    flattened = _with_canonical_keys(item)
    for key in ("value", "values", "forecast", "data", "prediction"):
        nested = flattened.get(key)
        if isinstance(nested, dict):
            for nested_key, nested_value in _with_canonical_keys(nested).items():
                flattened.setdefault(nested_key, nested_value)
    return flattened


def _with_canonical_keys(value: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(value)
    for key, item_value in value.items():
        canonical.setdefault(_canonical_key(key), item_value)
    return canonical


def _canonical_key(value: Any) -> str:
    raw = str(value)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    separated = re.sub(r"[^0-9A-Za-z]+", "_", separated)
    return separated.strip("_").lower()


def _normalize_price_value(value: float, unit: str) -> float:
    unit_lower = _normalize_unit(unit)
    if unit_lower in {"c/kwh", "¢/kwh", "cent/kwh", "cents/kwh"}:
        return value / 100
    return value


def _normalize_power_value(value: float, *, value_key: str, unit: str) -> float:
    unit_lower = _normalize_unit(unit)
    if unit_lower in {"mw", "megawatt", "megawatts"}:
        return value * 1000
    if unit_lower in {"w", "watt", "watts"} or "watt" in value_key.lower():
        return value / 1000
    if unit_lower in _ENERGY_UNITS:
        return _energy_to_kwh(value, unit)
    return value


def _normalize_temperature_value(value: float, unit: str) -> float:
    unit_lower = _normalize_unit(unit)
    if unit_lower in {"f", "°f", "fahrenheit"}:
        return round((value - 32) * 5 / 9, 4)
    return value


def _normalize_carbon_intensity_value(value: float, unit: str) -> float:
    """Normalize grid carbon intensity to grams CO2 equivalent per kWh."""
    unit_lower = _normalize_unit(unit).replace("₂", "2")
    if unit_lower.startswith("kgco2/"):
        return value * 1000
    return value


def _normalize_unit(unit: str) -> str:
    return unit.strip().lower().replace(" ", "")


_ENERGY_UNITS = {
    "wh",
    "watt-hour",
    "watthour",
    "watt-hours",
    "watthours",
    "kwh",
    "kilowatt-hour",
    "kilowatthour",
    "kilowatt-hours",
    "kilowatthours",
    "mwh",
    "megawatt-hour",
    "megawatthour",
    "megawatt-hours",
    "megawatthours",
}


def _value_for_slot(
    slot_time: datetime,
    timestamped: list[tuple[datetime, float]],
    *,
    final_cadence: timedelta,
) -> float | None:
    """Return the bucket value only while the source forecast has coverage."""
    # Right insertion preserves the source's last value for duplicate times.
    # Search the sorted series directly, avoiding a scan per destination slot.
    position = bisect_right(timestamped, slot_time, key=itemgetter(0)) - 1
    if position < 0:
        return None
    selected_at, selected = timestamped[position]
    if slot_time >= selected_at + final_cadence:
        return None
    return selected


def _conservative_cadence(
    timestamped: list[tuple[datetime, float]],
    default: timedelta,
    *,
    maximum: timedelta | None = None,
) -> timedelta:
    """Infer bucket duration without extending beyond a declared or observed cadence."""
    gaps = [right[0] - left[0] for left, right in zip(timestamped, timestamped[1:], strict=False) if right[0] > left[0]]
    cadence = min(gaps) if gaps else default
    return min(cadence, maximum) if maximum is not None else cadence


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
