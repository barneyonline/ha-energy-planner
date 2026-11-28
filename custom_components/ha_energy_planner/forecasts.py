"""Forecast helpers for Energy Planner."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from math import copysign, isfinite, isnan
from operator import itemgetter
from sys import float_info
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
_MAX_FORECAST_CONTAINER_DEPTH = 4
# The deepest supported container can hold a row with one mapping of nested
# fields. Its scalar fields are eight levels below the root attributes.
_MAX_TARIFF_FIELD_DEPTH = _MAX_FORECAST_CONTAINER_DEPTH + 4
_TIME_KEYS = ("valid_at", "datetime", "start_time", "period_start", "from", "time", "date", "nem_time")
FORECAST_ROW_CONTAINER_KEYS = ("value", "values", "forecast", "data", "prediction")
FORECAST_HEALTHY_HOURS = 12.0
FORECAST_DEGRADED_HOURS = 8.0
MAX_FORECAST_SOURCE_ROWS = 2048
_MAX_TARIFF_CACHE_FIELDS = MAX_FORECAST_SOURCE_ROWS * 16
_MAX_TARIFF_CACHE_TEXT_LENGTH = 256
_MAX_TARIFF_CACHE_INT_BITS = 1024
_FORECAST_CADENCE_KEYS = ("forecast_interval_minutes", "interval_minutes", "resolution_minutes")
_SCIENTIFIC_NUMBER = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))[eE][+-]?\d+")
_MIN_FORECAST_INTERVAL_MINUTES = timedelta.resolution.total_seconds() / 60
TARIFF_VALUE_KEYS = ("import_price", "general_price", "export_price", "feed_in_price", "per_kwh", "price", "value")
TARIFF_END_KEYS = ("end_time", "period_end", "valid_until", "to")
_TARIFF_NESTED_KEYS = frozenset({
    *_FORECAST_CONTAINER_KEYS, *FORECAST_ROW_CONTAINER_KEYS, *_TIME_KEYS, *TARIFF_END_KEYS, *TARIFF_VALUE_KEYS,
    "duration", "unit", "units", "unit_of_measurement",
})
_TARIFF_SOURCE_ATTRIBUTE_KEYS = frozenset({
    *_FORECAST_CONTAINER_KEYS,
    *_FORECAST_CADENCE_KEYS,
    *TARIFF_VALUE_KEYS,
    "unit_of_measurement", "unit", "temperature_unit",
    "confidence", "confidence_percent", "forecast_confidence", "forecast_confidence_percent",
    "issued_at", "last_updated", "forecast_issued_at", "generated_at", "generated_time", "forecast_generated_at",
})
_LATEST_FORECAST_END = datetime.max.replace(tzinfo=UTC)
_MAX_FORECAST_INTERVAL_MINUTES = (_LATEST_FORECAST_END - datetime.min.replace(tzinfo=UTC)).total_seconds() / 60


@dataclass(slots=True)
class ForecastSource:
    """Validated source rows independent of the requested planning grid."""

    declared: tuple[list[tuple[datetime, datetime, float]], int] | None
    points: list[tuple[datetime, float | None]]
    ordered: list[float | None]
    cadence: timedelta
    ordered_interval: float


@dataclass(slots=True)
class _TariffRow:
    values: dict[str, Any]
    durations: tuple[Any, ...]
    timestamp: datetime | None
    timestamp_invalid: bool
    timestamp_has_offset: bool
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True, slots=True)
class _UnreadableTariffValue:
    """Represent a malformed value without passing a provider object to a worker."""

    source_type: type[Any]
    source_id: int


@dataclass(slots=True)
class TariffSnapshotBudget:
    """Bound the work needed to retain or fingerprint one tariff source."""

    remaining: int = _MAX_TARIFF_CACHE_FIELDS

    def consume(self, value: Any) -> bool:
        """Reserve room for one field, including its scalar size."""
        self.remaining -= 1
        value_type = type(value)
        return (
            self.remaining >= 0
            and (value_type is not str or len(value) <= _MAX_TARIFF_CACHE_TEXT_LENGTH)
            and (value_type is not int or value.bit_length() <= _MAX_TARIFF_CACHE_INT_BITS)
        )


class TariffForecast:
    """Extract tariff rows and resolve timestamps before selecting a price column."""

    def __init__(
        self, raw_attributes: dict[str, Any], *, container_value_keys: tuple[str, ...] | None = None,
    ) -> None:
        self.attributes = _with_canonical_keys(raw_attributes)
        self.explicit_intervals = _representable_cadences(raw_attributes)
        self.rows = _prepare_tariff_rows(_container_forecast_items(
            self.attributes, ("value",) if container_value_keys is None else container_value_keys,
            tariff=container_value_keys is None or _prefer_native_tariffs(container_value_keys),
        ))

    def source(
        self, interval_minutes: int, value_keys: tuple[str, ...], *, boundaries: bool = False,
    ) -> ForecastSource:
        rows = self.rows or _prepare_tariff_rows(_keyed_forecast_items(self.attributes, value_keys))
        default_unit = _forecast_default_unit(self.attributes, unit_key_only=boundaries)
        if rows and (declared := _declared_tariff_intervals(
            rows, value_keys, default_unit, explicit_intervals=self.explicit_intervals,
        )) is not None:
            return ForecastSource(declared, [], [], timedelta(), float(interval_minutes))
        parsed: list[tuple[datetime | None, float | None] | None] = [
            _parse_item(item, value_keys=value_keys, value_kind="price", default_unit=default_unit) for item in rows
        ]
        if None in parsed:
            known_times = {point[0] for point in parsed if point is not None and point[0] is not None}
            if known_times:
                # A reported time with no usable price ends the preceding
                # bucket. A valid duplicate still supplies that time's price.
                for index, (row, point) in enumerate(zip(rows, parsed, strict=True)):
                    if (point is None and isinstance(row, _TariffRow)
                            and row.timestamp is not None and row.timestamp not in known_times):
                        parsed[index] = (row.timestamp, None)
        return _assemble_forecast_source(parsed, self.explicit_intervals, interval_minutes)

    def boundary_source(self, interval_minutes: int) -> ForecastSource:
        return self.source(interval_minutes, TARIFF_VALUE_KEYS, boundaries=True)


def tariff_source_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Select tariff inputs without rewriting aliases or truncating source rows."""
    return {
        key: value for key, value in attributes.items()
        if key in _TARIFF_SOURCE_ATTRIBUTE_KEYS or _canonical_key(key) in _TARIFF_SOURCE_ATTRIBUTE_KEYS
    }


def capture_tariff_source_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Detach all tariff rows while omitting unused provider metadata."""
    return {key: _capture_tariff_value(value, 1) for key, value in tariff_source_attributes(attributes).items()}


def tariff_mapping_items(attributes: Mapping[str, Any], *, nested: bool) -> Iterator[tuple[str, Any]]:
    """Keep tariff row fields and time-map keys in their original order."""
    if not nested:
        yield from attributes.items()
        return
    for key, value in attributes.items():
        if key in _TARIFF_NESTED_KEYS or _canonical_key(key) in _TARIFF_NESTED_KEYS or _parse_datetime_or_none(key):
            yield key, value


def _tariff_value_type(value: Any) -> type[Any]:
    """Use standard container types while retaining exact scalar types."""
    if isinstance(value, dict):
        return dict
    if isinstance(value, list):
        return list
    if isinstance(value, tuple):
        return tuple
    return type(value)


def _capture_tariff_value(value: Any, depth: int) -> Any:
    # At the maximum field depth, a container's type is enough to represent
    # malformed scalar evidence. None of its children can supply a valid field.
    value_type = type(value)
    if value is None or value_type in (str, bool, int, float):
        return value
    if value_type not in (dict, list, tuple, datetime):
        value_type = _tariff_value_type(value)
    if depth == _MAX_TARIFF_FIELD_DEPTH and value_type in (dict, list, tuple):
        return value_type()
    if value_type is dict:
        return {
            key: _capture_tariff_value(item, depth + 1) for key, item in tariff_mapping_items(value, nested=True)
        }
    if value_type is list:
        return [_capture_tariff_value(item, depth + 1) for item in value]
    if value_type is tuple:
        return tuple(_capture_tariff_value(item, depth + 1) for item in value)
    try:
        captured = deepcopy(value)
        if _tariff_values_match(value, captured, None, depth):
            return captured
    except Exception:  # A provider's copy or equality hooks can reject a detached value.
        pass
    # An unreadable field remains a gap and can still be matched to the
    # source object during the refresh's stale-input check.
    return _UnreadableTariffValue(type(value), id(value))


def cacheable_tariff_snapshot(attributes: Mapping[str, Any]) -> dict[str, Any] | None:
    """Detach a bounded tariff payload, or skip caching unsupported values."""
    try:
        return _copy_tariff_mapping(attributes, 1, TariffSnapshotBudget())
    except ValueError:
        return None


def _copy_tariff_mapping(attributes: Mapping[str, Any], depth: int, budget: TariffSnapshotBudget) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key, value in tariff_mapping_items(attributes, nested=depth > 1):
        if len(snapshot) == MAX_FORECAST_SOURCE_ROWS:
            raise ValueError("Tariff snapshot mapping exceeds the cache limit")
        copied_key = _copy_tariff_value(key, depth, budget)
        if copied_key in snapshot:
            raise ValueError("Tariff snapshot contains equivalent timestamp keys")
        snapshot[copied_key] = _copy_tariff_value(value, depth, budget)
    return snapshot


def _copy_tariff_value(value: Any, depth: int, budget: TariffSnapshotBudget) -> Any:
    if depth > _MAX_TARIFF_FIELD_DEPTH or not budget.consume(value):
        raise ValueError("Tariff payload exceeds the cache budget")
    value_type = type(value)
    if value_type is dict:
        return _copy_tariff_mapping(value, depth + 1, budget)
    if value_type in (list, tuple) and len(value) > MAX_FORECAST_SOURCE_ROWS:
        raise ValueError("Tariff snapshot sequence exceeds the cache limit")
    if value_type is list:
        return [_copy_tariff_value(item, depth + 1, budget) for item in value]
    if value_type is tuple:
        return tuple(_copy_tariff_value(item, depth + 1, budget) for item in value)
    if value_type is datetime:
        # Capture its current offset independently of the source timezone object.
        offset = value.utcoffset()
        return value.replace(tzinfo=None if offset is None else timezone(offset))
    if value is None or value_type in (str, bool, int, float):
        return value
    raise ValueError("Tariff payload contains an unsupported cache value")


def tariff_snapshot_matches(
    attributes: Mapping[str, Any], snapshot: dict[str, Any], *, acyclic_snapshot: bool = False,
    strict: bool = False,
) -> bool:
    """Compare a detached tariff payload without conflating types or alias order."""
    # Bounded cache snapshots cannot contain cycles. Comparing against one
    # needs no tracking set even when the current input contains a cycle.
    return _tariff_mappings_match(attributes, snapshot, None if acyclic_snapshot else set(), 1, strict=strict)


def _tariff_mappings_match(
    current: Mapping[str, Any], captured: Mapping[str, Any], compared: set[tuple[int, int]] | None, depth: int,
    *, strict: bool,
) -> bool:
    for field, captured_field in zip_longest(
        tariff_mapping_items(current, nested=depth > 1), tariff_mapping_items(captured, nested=depth > 1),
    ):
        if field is None or captured_field is None:
            return False
        key, value = field
        captured_key, captured_value = captured_field
        if not (_tariff_values_match(key, captured_key, compared, depth, strict=strict)
                and _tariff_values_match(value, captured_value, compared, depth, strict=strict)):
            return False
    return True


def _tariff_values_match(
    current: Any, captured: Any, compared: set[tuple[int, int]] | None, depth: int,
    *, strict: bool = False,
) -> bool:
    if current is captured:
        return True
    if isinstance(captured, _UnreadableTariffValue):
        return type(current) is captured.source_type and id(current) == captured.source_id
    if type(current) is not type(captured) and (
        strict or _tariff_value_type(current) is not _tariff_value_type(captured)
    ):
        return False
    if not isinstance(current, (dict, list, tuple)):
        try:
            if isinstance(current, datetime):
                # A fall-back hour can compare equal despite having a different UTC offset.
                return bool(current.isoformat() == captured.isoformat())
            if strict and type(current) is float:
                if current == captured == 0.0:
                    return copysign(1.0, current) == copysign(1.0, captured)
                # JSON distinguishes signed zeros but encodes all NaNs alike.
                if isnan(current):
                    return isnan(captured)
            return bool(current == captured)
        except Exception:  # Provider equality and offset hooks can reject malformed values.
            return False
    if depth >= _MAX_TARIFF_FIELD_DEPTH:
        return True
    if compared is not None:
        pair = (id(current), id(captured))
        if pair in compared:
            return True
        compared.add(pair)
    if isinstance(current, dict):
        return _tariff_mappings_match(current, captured, compared, depth + 1, strict=strict)
    if len(current) != len(captured):
        return False
    for value, item in zip(current, captured, strict=True):
        if not _tariff_values_match(value, item, compared, depth + 1, strict=strict):
            return False
    return True


def _forecast_default_unit(attributes: dict[str, Any], *, unit_key_only: bool = False) -> str:
    return str(attributes.get("unit_of_measurement", "" if unit_key_only else
                              attributes.get("unit", attributes.get("temperature_unit", ""))))


def _representable_cadences(attributes: dict[str, Any]) -> tuple[float, ...]:
    return tuple(
        float(minutes) for minutes in _declared_interval_minutes(attributes)
        if minutes is not None and _MIN_FORECAST_INTERVAL_MINUTES <= minutes < _MAX_FORECAST_INTERVAL_MINUTES
    )


def parse_forecast_source(
    state: Any, interval_minutes: int, value_keys: tuple[str, ...], value_kind: str,
) -> ForecastSource:
    """Normalize once before sampling on one or more grids."""
    raw_attributes = getattr(state, "attributes", {}) or {}
    if value_kind == "price":
        return TariffForecast(raw_attributes, container_value_keys=value_keys).source(interval_minutes, value_keys)
    attributes = _with_canonical_keys(raw_attributes)
    raw_items = _forecast_items(attributes, value_keys)
    default_unit = _forecast_default_unit(attributes)
    if value_kind == "power":
        raw_items = _energy_items_as_average_power(raw_items, value_keys, default_unit)
    parsed = [_parse_item(item, value_keys=value_keys, value_kind=value_kind, default_unit=default_unit)
              for item in raw_items]
    return _assemble_forecast_source(parsed, _representable_cadences(raw_attributes), interval_minutes)


def _assemble_forecast_source(
    parsed: Sequence[tuple[datetime | None, float | None] | None],
    explicit_intervals: tuple[float, ...],
    interval_minutes: int,
) -> ForecastSource:
    points = sorted(
        [(at, item[1]) for item in parsed if item is not None and (at := item[0]) is not None],
        key=itemgetter(0),
    )
    cadence = timedelta()
    if points:
        explicit_interval = _forecast_cadence_before_datetime_end(explicit_intervals, points[-1][0])
        explicit_cadence = timedelta(minutes=explicit_interval) if explicit_interval is not None else None
        cadence = _conservative_cadence(
            points, explicit_cadence or timedelta(minutes=interval_minutes), maximum=explicit_cadence,
        )
    ordered = [item[1] if item is not None else None for item in parsed] if not points and any(parsed) else []
    return ForecastSource(None, points, ordered, cadence, next(iter(explicit_intervals), float(interval_minutes)))


def parse_tariff_source(state: Any, interval_minutes: int) -> ForecastSource:
    """Normalize source intervals using the planning grid's price key precedence."""
    return TariffForecast(getattr(state, "attributes", {}) or {}).boundary_source(interval_minutes)


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
) -> list[dict[str, Any]]:
    """Expose sanitized source boundaries used to identify tariff lifecycles."""
    return tariff_intervals_from_source(parse_tariff_source(state, interval_minutes), details=details,
                                        slot_starts=slot_starts)


def tariff_intervals_from_source(
    source: ForecastSource, *, details: dict[str, Any] | None = None, slot_starts: list[datetime] | None = None,
) -> list[dict[str, Any]]:
    """Expose boundaries from a source already normalized for tariff intervals."""
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
    priced_points = {(at, price) for at, price in source.points if price is not None}
    intervals = [(at, at + min(source.cadence, _LATEST_FORECAST_END - at), price)
                 for at, price in sorted(priced_points)]
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
) -> list[float | None] | None:
    """Return a slot-aligned numeric forecast series from common HA attributes.

    Supports two common integration shapes:
    - list attributes such as ``forecast`` or ``forecasts`` with timestamped dicts
    - list attributes with ordered numeric values and no timestamps

    Set ``require_timestamped`` for freshness evidence: ordered values have no
    source interval and must not be treated as proof of current coverage.
    """
    source = parse_forecast_source(state, interval_minutes, value_keys, value_kind)
    return forecast_series_from_source(
        source, issued_at=issued_at, horizon_hours=horizon_hours, interval_minutes=interval_minutes,
        require_timestamped=require_timestamped, slot_starts=slot_starts,
    )


def forecast_series_from_source(
    source: ForecastSource, *, issued_at: datetime, horizon_hours: int, interval_minutes: int,
    require_timestamped: bool = False, slot_starts: list[datetime] | None = None,
) -> list[float | None] | None:
    """Sample a parsed forecast on an implicit or explicit planning grid."""
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
            slot_count = (horizon_hours * 60) // interval_minutes
            return _align_ordered_values(source.ordered, slot_count, interval_minutes, source_interval)
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


def _forecast_cadence_before_datetime_end(aliases: tuple[float, ...], latest_start: datetime) -> float | None:
    """Choose the first cadence whose endpoint fits every timestamped row."""
    remaining = _LATEST_FORECAST_END - latest_start
    return next((minutes for minutes in aliases if timedelta(minutes=minutes) <= remaining), None)


def _declared_tariff_intervals(
    items: list[Any], value_keys: tuple[str, ...], default_unit: str, *, explicit_intervals: tuple[float, ...],
) -> tuple[list[tuple[datetime, datetime, float]], int] | None:
    """Honor Amber interval metadata; reject broken native evidence without fallback.

    Timestamp-only and other provider schemas retain their existing parser. Once
    an Amber per_kwh source declares interval metadata, every row must validate.
    Real gaps remain gaps and explicit ends cannot be extended by inferred cadence.
    """
    if not any(isinstance(item, _TariffRow) and _has_declared_tariff_bounds(item.values) for item in items):
        return None
    intervals: list[tuple[datetime, datetime, float]] = []
    normalized = 0
    for item in items:
        if not isinstance(item, _TariffRow):
            return [], 0
        start, end = item.start, item.end
        duration = item.values.get("duration")
        per_kwh: Any = item.values.get("per_kwh")
        if type(per_kwh) not in (int, float) or not -float_info.max <= per_kwh <= float_info.max:
            return [], 0
        parsed = _parse_item(item, value_keys=value_keys, value_kind="price", default_unit=default_unit)
        if (
            start is None or end is None or not item.timestamp_has_offset
            or type(duration) not in (int, float) or duration not in (5, 15, 30)
            or any(type(declared) not in (int, float) or declared != duration for declared in item.durations)
            or parsed is None or parsed[0] != start
        ):
            return [], 0
        canonical_start = _amber_half_hour_start(start, end, item.values)
        if end - canonical_start != timedelta(minutes=duration):
            return [], 0
        normalized += canonical_start != start
        intervals.append((canonical_start, end, parsed[1]))
    intervals.sort(key=itemgetter(0))
    maximum_minutes = _forecast_cadence_before_datetime_end(explicit_intervals, intervals[-1][0])
    if maximum_minutes is not None and any(
        (end - start).total_seconds() / 60 > maximum_minutes for start, end, _ in intervals
    ):
        return [], 0
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


def _declared_interval_minutes(attributes: dict[str, Any]) -> Iterator[int | float | Decimal | None]:
    """Yield positive cadences, with None for numeric strings beyond Decimal's range."""
    aliases: dict[str, list[Any]] = {name: [] for name in _FORECAST_CADENCE_KEYS}
    for key, raw_value in attributes.items():
        canonical = _canonical_key(key)
        if key != canonical and canonical in aliases:
            aliases[canonical].append(raw_value)
    for name, alternate_values in aliases.items():
        for raw_value in (attributes.get(name), *alternate_values):
            try:
                value = Decimal(raw_value) if isinstance(raw_value, str) else raw_value
            except InvalidOperation:
                # A syntactically valid positive declaration still disagrees
                # with 30 minutes even when its exponent is unrepresentable.
                match = _SCIENTIFIC_NUMBER.fullmatch(raw_value.strip().replace("_", ""))
                if match is not None and Decimal(match[1]) > 0:
                    yield None
                continue
            if isinstance(value, (int, float, Decimal)) and not (isinstance(value, Decimal) and value.is_nan()):
                if value > 0:
                    yield value


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
        valid_at = _first_forecast_time(_flatten_item(item))
        if valid_at is None:
            continue
        valid_at = _utc_forecast_time(valid_at)
        if valid_at is not None and (latest is None or valid_at > latest):
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
        valid_at = _first_forecast_time(_flatten_item(item))
        if valid_at is None:
            continue
        if valid_at.tzinfo is None or valid_at.utcoffset() is None:
            return "naive_timestamps"
        found |= _utc_forecast_time(valid_at) is not None
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
    return _forecast_items_from_canonical(_with_canonical_keys(attributes), value_keys)


def _forecast_items_from_canonical(attributes: dict[str, Any], value_keys: tuple[str, ...]) -> list[Any]:
    return _container_forecast_items(
        attributes, value_keys, tariff=_prefer_native_tariffs(value_keys),
    ) or _keyed_forecast_items(attributes, value_keys)


def _prefer_native_tariffs(value_keys: tuple[str, ...]) -> bool:
    return "per_kwh" in value_keys or any("price" in key for key in value_keys)


def _container_forecast_items(
    attributes: dict[str, Any], value_keys: tuple[str, ...], *, tariff: bool,
) -> list[Any]:
    containers: tuple[str, ...] = _FORECAST_CONTAINER_KEYS
    if tariff:
        # Provider-native tariff blocks take precedence over a convenience
        # forecast that may already have been resampled on a rolling grid.
        containers = ("detailedForecast", "detailed_forecast", *containers)
    for key in containers:
        value = attributes.get(key)
        items = _items_from_value(value, value_keys)
        if items:
            return items
    return []


def _keyed_forecast_items(attributes: dict[str, Any], value_keys: tuple[str, ...]) -> list[Any]:
    for key in value_keys:
        items = _items_from_value(attributes.get(key), value_keys, map_value_key=key)
        if items:
            return items
    return []


def _has_declared_tariff_bounds(item: dict[str, Any]) -> bool:
    return "per_kwh" in item and ("duration" in item or "end_time" in item)


def _absolute_tariff_time(value: Any) -> datetime | None:
    parsed = _parse_datetime_or_none(value)
    if parsed is None or parsed.utcoffset() is None:
        return None
    return _utc_forecast_time(parsed)


def _tariff_row_durations(row: dict[str, Any]) -> Iterator[Any]:
    """Read each duration declaration before spelling and nesting are flattened."""
    row_mappings = (row, *(value for key, value in row.items()
                           if _canonical_key(key) in FORECAST_ROW_CONTAINER_KEYS))
    for fields in row_mappings:
        if isinstance(fields, dict):
            for name, duration in fields.items():
                if _canonical_key(name) == "duration":
                    yield duration


def _prepare_tariff_rows(raw_items: list[Any]) -> list[Any]:
    rows = []
    for item in raw_items:
        if not isinstance(item, dict):
            rows.append(item)
            continue
        fields = _flatten_item(item)
        source_time = _first_forecast_time(fields)
        timestamp = _utc_forecast_time(source_time) if source_time is not None else None
        start = _absolute_tariff_time(fields.get("start_time"))
        end = _absolute_tariff_time(fields.get("end_time"))
        rows.append(_TariffRow(
            fields, tuple(_tariff_row_durations(item)), timestamp, source_time is not None and timestamp is None,
            source_time is not None and source_time.utcoffset() is not None, start, end,
        ))
    return rows


def _items_from_value(
    value: Any,
    value_keys: tuple[str, ...],
    depth: int = 0,
    map_value_key: str | None = None,
) -> list[Any]:
    if value is None or depth > _MAX_FORECAST_CONTAINER_DEPTH:
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
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(value):
            return None
        return None, normalize_scalar_value(value, value_kind=value_kind, unit=default_unit)
    if isinstance(item, _TariffRow):
        if item.timestamp_invalid:
            return None
        valid_at, item = item.timestamp, item.values
    elif isinstance(item, dict):
        item = _flatten_item(item)
        valid_at = _first_forecast_time(item)
        if valid_at is not None:
            valid_at = _utc_forecast_time(valid_at)
            if valid_at is None:
                return None
    else:
        return None

    value_key = next((key for key in value_keys if key in item), None)
    if value_key is None:
        return None
    try:
        value = float(item[value_key])
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(value):
        return None
    unit = _forecast_item_unit(item, value_key, default_unit)
    value = normalize_scalar_value(value, value_kind=value_kind, value_key=value_key, unit=unit)

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
        valid_at = _first_forecast_time(flattened)
        prepared.append((item, flattened, _utc_forecast_time(valid_at) if valid_at is not None else None))

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
    for key in FORECAST_ROW_CONTAINER_KEYS:
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
    """Normalize the spellings accepted by forecast providers."""
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
    timestamped: list[tuple[datetime, float | None]],
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
    if slot_time - selected_at >= final_cadence:
        return None
    return selected


def _conservative_cadence(
    timestamped: list[tuple[datetime, float | None]],
    default: timedelta,
    *,
    maximum: timedelta | None = None,
) -> timedelta:
    """Infer bucket duration without extending beyond a declared or observed cadence."""
    gaps = [right[0] - left[0] for left, right in zip(timestamped, timestamped[1:], strict=False) if right[0] > left[0]]
    cadence = min(gaps) if gaps else default
    return min(cadence, maximum) if maximum is not None else cadence


def _first_forecast_time(item: dict[str, Any]) -> datetime | None:
    """Return the first parseable timestamp in source key order."""
    for key in _TIME_KEYS:
        if key in item and (valid_at := _parse_datetime_or_none(item[key])) is not None:
            return valid_at
    return None


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


def _utc_forecast_time(value: datetime) -> datetime | None:
    """Reject source timestamps whose timezone offsets cross datetime's range."""
    try:
        return _as_aware_utc(value)
    except OverflowError:
        return None
