"""Deterministic Recorder-backed household load forecasting."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite, sqrt
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODEL_VERSION = 1
FORECAST_CONTRACT_VERSION = 2
HISTORY_LOOKBACK = timedelta(days=28)
TRAIN_INTERVAL = timedelta(hours=6)
HEALTHY_MODEL_AGE = timedelta(hours=24)
STALE_MODEL_AGE = timedelta(hours=72)
BUCKET_MINUTES = 15
BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES
MIN_COMPLETE_DAYS = 3
MIN_DAY_COVERAGE = 0.80
MIN_BUCKET_SAMPLES = 3
MIN_VALIDATION_TRAINING_DAYS = 1
HOLDOUT_ORIGINS = 2
MIN_HOLDOUT_SAMPLES = 144
MAX_BASELINE_MAE_RATIO = 1.10
MIN_UPPER_COVERAGE = 0.90
MAX_INTERPOLATION_BUCKETS = 2
RECENT_CORRECTION_MIN = 0.75
RECENT_CORRECTION_MAX = 1.25
RECENT_CORRECTION_HOURS = 2.0

_TRUE_STATES = {
    "on",
    "true",
    "1",
    "charging",
    "finishing",
}
_FALSE_STATES = {
    "off",
    "false",
    "0",
    "idle",
    "available",
    "preparing",
    "suspended_ev",
    "suspended_evse",
    "fully_charged",
    "connected_not_charging",
}
_UNKNOWN_STATES = {"unknown", "unavailable", "none", ""}


@dataclass(frozen=True, slots=True)
class LoadForecastResult:
    """Forecast values plus bounded health evidence."""

    expected_kw: list[float | None]
    upper_kw: list[float | None]
    status: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _HistoryEvent:
    at: datetime
    value: float | bool | None


@dataclass(frozen=True, slots=True)
class _HistoricalDay:
    local_date: date
    day_type: str
    values: dict[int, float]


def build_load_forecast_model(
    load_states: list[Any],
    *,
    now: datetime,
    timezone: str,
    source_entity_id: str,
    load_unit: str,
    ev_charging_states: list[Any] | None = None,
    hvac_power_states: list[Any] | None = None,
    hvac_power_unit: str = "",
) -> dict[str, Any]:
    """Build a compact deterministic model from Recorder state changes."""
    now_utc = _as_utc(now)
    start = now_utc - HISTORY_LOOKBACK
    bucket_values, effective_start, ev_excluded, hvac_subtracted = clean_load_history_buckets(
        load_states,
        load_unit=load_unit,
        ev_charging_states=ev_charging_states,
        hvac_power_states=hvac_power_states,
        hvac_power_unit=hvac_power_unit,
        start=start,
        end=now_utc,
        timezone=timezone,
    )
    return build_load_forecast_model_from_buckets(
        bucket_values,
        now=now_utc,
        timezone=timezone,
        source_entity_id=source_entity_id,
        history_start=effective_start or start,
        ev_intervals_excluded=ev_excluded,
        hvac_power_subtracted=hvac_subtracted,
    )


def clean_load_history_buckets(
    load_states: list[Any],
    *,
    load_unit: str,
    start: datetime,
    end: datetime,
    timezone: str,
    ev_charging_states: list[Any] | None = None,
    hvac_power_states: list[Any] | None = None,
    hvac_power_unit: str = "",
) -> tuple[list[tuple[datetime, float]], datetime | None, bool, bool]:
    """Normalize and clean one bounded Recorder history chunk."""
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    load_events = _power_events(load_states, load_unit)
    ev_events = _bool_events(ev_charging_states or [])
    hvac_events = _power_events(hvac_power_states or [], hvac_power_unit)
    if not load_events:
        return [], None, bool(ev_events), bool(hvac_events)
    effective_start = max(start_utc, load_events[0].at)
    return (
        _time_weighted_buckets(
            load_events,
            ev_events,
            hvac_events,
            start=effective_start,
            end=end_utc,
            zone=_timezone(timezone),
        ),
        effective_start,
        bool(ev_events),
        bool(hvac_events),
    )


def build_load_forecast_model_from_buckets(
    bucket_values: list[tuple[datetime, float]],
    *,
    now: datetime,
    timezone: str,
    source_entity_id: str,
    history_start: datetime,
    ev_intervals_excluded: bool = False,
    hvac_power_subtracted: bool = False,
) -> dict[str, Any]:
    """Build a compact model from already-cleaned 15-minute history buckets."""
    now_utc = _as_utc(now)
    zone = _timezone(timezone)
    effective_start = _as_utc(history_start)
    all_days, eligible_day_count = _historical_days(
        bucket_values,
        start=effective_start,
        end=now_utc,
        zone=zone,
    )
    complete_days = [day for day in all_days if len(day.values) == BUCKETS_PER_DAY]
    history_coverage = (
        sum(len(day.values) for day in all_days) / (eligible_day_count * BUCKETS_PER_DAY)
        if eligible_day_count
        else 0.0
    )
    validation = _rolling_validation(complete_days, comparison_days=all_days)
    uncertainty_buffer = float(validation.get("positive_residual_p90_kw") or 0.0)
    profiles = {
        day_type: _profile_for_day_type(complete_days, day_type, uncertainty_buffer)
        for day_type in ("weekday", "weekend")
    }
    quality_failures = _quality_failures(
        complete_days=len(complete_days),
        history_coverage=history_coverage,
        validation=validation,
        profiles=profiles,
    )
    insufficient_history = any(
        failure in {"insufficient_complete_days", "insufficient_history_coverage", "insufficient_holdout_samples"}
        for failure in quality_failures
    )
    status = "ready" if not quality_failures else "learning" if insufficient_history else "failed"
    first_day = min((day.local_date for day in all_days), default=None)
    last_day = max((day.local_date for day in all_days), default=None)
    return {
        "model_version": MODEL_VERSION,
        "contract_version": FORECAST_CONTRACT_VERSION,
        "status": status,
        "quality_ready": status == "ready",
        "quality_failures": quality_failures,
        "source_entity_id": source_entity_id,
        "trained_at": now_utc.isoformat(),
        "last_attempt_at": now_utc.isoformat(),
        "last_attempt_source_entity_id": source_entity_id,
        "last_attempt_timezone": str(zone),
        "timezone": str(zone),
        "history_started_on": first_day.isoformat() if first_day else None,
        "history_ended_on": last_day.isoformat() if last_day else None,
        "history_days": len(all_days),
        "complete_days": len(complete_days),
        "history_coverage": round(history_coverage, 6),
        "uncertainty_buffer_kw": round(uncertainty_buffer, 6),
        "cleaning": {
            "ev_intervals_excluded": ev_intervals_excluded,
            "hvac_power_subtracted": hvac_power_subtracted,
        },
        "validation": validation,
        "profiles": profiles,
    }


def load_forecast_from_model(
    model: dict[str, Any] | None,
    *,
    now: datetime,
    timezone: str,
    horizon_hours: int,
    interval_minutes: int,
    source_entity_id: str,
    current_load_kw: float | None = None,
    current_ev_charging: bool | None = None,
) -> LoadForecastResult:
    """Return planning-interval expected and conservative load forecasts."""
    slot_count = max(int(horizon_hours * 60 / interval_minutes), 0)
    empty = [None] * slot_count
    validated = _validated_model(model, source_entity_id, timezone)
    if validated is None:
        return LoadForecastResult(empty, empty.copy(), "failed", _model_details(model, "failed", 0.0))
    status, age_hours = load_forecast_model_status(validated, now=now)
    profiles = validated["profiles"]
    zone = _timezone(timezone)
    now_utc = _as_utc(now)
    correction = _recent_correction(
        profiles,
        now_utc,
        zone,
        current_load_kw=current_load_kw,
        current_ev_charging=current_ev_charging,
    )
    expected: list[float | None] = []
    upper: list[float | None] = []
    for index in range(slot_count):
        target = now_utc + timedelta(minutes=index * interval_minutes)
        expected_value = _profile_interval_average(
            profiles,
            target,
            interval_minutes=interval_minutes,
            zone=zone,
            profile_key="expected",
        )
        upper_value = _profile_interval_average(
            profiles,
            target,
            interval_minutes=interval_minutes,
            zone=zone,
            profile_key="upper",
        )
        if expected_value is None or upper_value is None:
            expected.append(None)
            upper.append(None)
            continue
        elapsed_hours = index * interval_minutes / 60
        correction_weight = max(1.0 - elapsed_hours / RECENT_CORRECTION_HOURS, 0.0)
        factor = 1.0 + (correction - 1.0) * correction_weight
        adjusted_expected = max(expected_value * factor, 0.0)
        adjusted_upper = max(upper_value, upper_value * factor, adjusted_expected)
        expected.append(round(adjusted_expected, 4))
        upper.append(round(adjusted_upper, 4))
    coverage = sum(value is not None for value in expected) / slot_count if slot_count else 0.0
    if coverage < 1.0 and status in {"ready", "degraded"}:
        status = "failed"
    details = _model_details(validated, status, age_hours)
    details.update(
        {
            "forecast_coverage": round(coverage, 6),
            "recent_correction_factor": round(correction, 4),
            "first_expected_kw": next((value for value in expected if value is not None), None),
            "first_upper_kw": next((value for value in upper if value is not None), None),
        }
    )
    return LoadForecastResult(expected, upper, status, details)


def load_forecast_model_status(model: dict[str, Any], *, now: datetime) -> tuple[str, float]:
    """Return categorical model health and age in hours."""
    trained_at = _parse_datetime(model.get("trained_at"))
    if trained_at is None:
        return "failed", 0.0
    now_utc = _as_utc(now)
    if trained_at > now_utc + timedelta(minutes=5):
        return "failed", 0.0
    age = max(now_utc - trained_at, timedelta())
    age_hours = age.total_seconds() / 3600
    stored_status = str(model.get("status", "failed"))
    if stored_status != "ready" or model.get("quality_ready") is not True:
        unusable_since = _parse_datetime(model.get("unusable_since"))
        if stored_status == "learning" and unusable_since is not None and now_utc - unusable_since > STALE_MODEL_AGE:
            return "failed", age_hours
        return stored_status if stored_status in {"learning", "failed"} else "failed", age_hours
    if age <= HEALTHY_MODEL_AGE:
        return "ready", age_hours
    if age <= STALE_MODEL_AGE:
        return "degraded", age_hours
    return "stale", age_hours


def training_due(
    model: dict[str, Any] | None,
    *,
    now: datetime,
    source_entity_id: str,
    timezone: str | None = None,
) -> bool:
    """Return whether a new Recorder training pass is due."""
    if not isinstance(model, dict):
        return True
    now_utc = _as_utc(now)
    if (
        model.get("model_version") != MODEL_VERSION
        or model.get("contract_version") != FORECAST_CONTRACT_VERSION
        or model.get("source_entity_id") != source_entity_id
        or (timezone is not None and model.get("timezone") != timezone)
    ):
        return True
    attempted_source = model.get("last_attempt_source_entity_id")
    attempted_timezone = model.get("last_attempt_timezone", model.get("timezone"))
    last_attempt = _parse_datetime(model.get("last_attempt_at"))
    attempt_matches = attempted_source == source_entity_id and (timezone is None or attempted_timezone == timezone)
    if attempt_matches and last_attempt is not None:
        return last_attempt > now_utc + timedelta(minutes=5) or now_utc >= last_attempt + TRAIN_INTERVAL
    trained_at = _parse_datetime(model.get("trained_at"))
    return trained_at is None or trained_at > now_utc + timedelta(minutes=5) or now_utc >= trained_at + TRAIN_INTERVAL


def normalize_power_kw(value: Any, unit: str) -> float | None:
    """Normalize a finite non-negative power value into kW."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number) or number < 0:
        return None
    normalized = str(unit).strip().lower().replace(" ", "")
    if normalized in {"w", "watt", "watts"}:
        number /= 1000
    elif normalized in {"mw", "megawatt", "megawatts"}:
        number *= 1000
    elif normalized not in {"kw", "kilowatt", "kilowatts"}:
        return None
    return number


def _power_events(states: list[Any], unit: str) -> list[_HistoryEvent]:
    events = []
    for state in states:
        at = _state_time(state)
        if at is None:
            continue
        raw = getattr(state, "state", None)
        attributes = getattr(state, "attributes", {}) or {}
        state_unit = str(attributes.get("unit_of_measurement") or attributes.get("unit") or unit)
        value = None if str(raw).strip().lower() in _UNKNOWN_STATES else normalize_power_kw(raw, state_unit)
        events.append(_HistoryEvent(at, value))
    return _deduplicate_events(events)


def _bool_events(states: list[Any]) -> list[_HistoryEvent]:
    events = []
    for state in states:
        at = _state_time(state)
        if at is None:
            continue
        raw = str(getattr(state, "state", "")).strip().lower()
        value = True if raw in _TRUE_STATES else False if raw in _FALSE_STATES else None
        events.append(_HistoryEvent(at, value))
    return _deduplicate_events(events)


def _deduplicate_events(events: list[_HistoryEvent]) -> list[_HistoryEvent]:
    by_time = {event.at: event for event in events}
    return [by_time[at] for at in sorted(by_time)]


def _state_time(state: Any) -> datetime | None:
    value = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    return _as_utc(value) if isinstance(value, datetime) else None


def _time_weighted_buckets(
    load_events: list[_HistoryEvent],
    ev_events: list[_HistoryEvent],
    hvac_events: list[_HistoryEvent],
    *,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
) -> list[tuple[datetime, float]]:
    if not load_events:
        return []
    bucket_start = _floor_bucket(start)
    result: list[tuple[datetime, float]] = []
    event_sets = (load_events, ev_events, hvac_events)
    event_times = [[event.at for event in events] for events in event_sets]
    while bucket_start < end:
        bucket_end = min(bucket_start + timedelta(minutes=BUCKET_MINUTES), end)
        boundaries = {bucket_start, bucket_end}
        for times in event_times:
            left = bisect_right(times, bucket_start)
            right = bisect_right(times, bucket_end - timedelta(microseconds=1))
            boundaries.update(times[left:right])
        ordered = sorted(boundaries)
        weighted = 0.0
        valid_seconds = 0.0
        for segment_start, segment_end in zip(ordered, ordered[1:], strict=False):
            seconds = (segment_end - segment_start).total_seconds()
            load = _event_value(load_events, event_times[0], segment_start)
            ev_charging = _event_value(ev_events, event_times[1], segment_start)
            hvac = _event_value(hvac_events, event_times[2], segment_start)
            if (
                not isinstance(load, int | float)
                or ev_charging is True
                or (ev_events and ev_charging is None)
                or (hvac_events and not isinstance(hvac, int | float))
            ):
                continue
            cleaned = max(float(load) - (float(hvac) if isinstance(hvac, int | float) else 0.0), 0.0)
            weighted += cleaned * seconds
            valid_seconds += seconds
        bucket_seconds = (bucket_end - bucket_start).total_seconds()
        if bucket_seconds > 0 and valid_seconds / bucket_seconds >= MIN_DAY_COVERAGE:
            result.append((bucket_start.astimezone(zone), weighted / valid_seconds))
        bucket_start += timedelta(minutes=BUCKET_MINUTES)
    return result


def _event_value(events: list[_HistoryEvent], times: list[datetime], at: datetime) -> float | bool | None:
    if not events:
        return None
    index = bisect_right(times, at) - 1
    return None if index < 0 else events[index].value


def _historical_days(
    buckets: list[tuple[datetime, float]],
    *,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
) -> tuple[list[_HistoricalDay], int]:
    accumulated: dict[date, dict[int, list[float]]] = {}
    for local, value in buckets:
        local_date = local.date()
        if local_date >= end.astimezone(zone).date():
            continue
        index = local.hour * 4 + local.minute // BUCKET_MINUTES
        accumulated.setdefault(local_date, {}).setdefault(index, []).append(value)
    local_start = start.astimezone(zone)
    first_date = local_start.date()
    if local_start.timetz().replace(tzinfo=None) != time.min:
        first_date += timedelta(days=1)
    last_date = end.astimezone(zone).date() - timedelta(days=1)
    days: list[_HistoricalDay] = []
    eligible = 0
    current = first_date
    while current <= last_date:
        if not _is_dst_transition_day(current, zone):
            eligible += 1
            values = {
                index: sum(samples) / len(samples)
                for index, samples in accumulated.get(current, {}).items()
                if samples
            }
            days.append(_HistoricalDay(current, _day_type(current), values))
        current += timedelta(days=1)
    return days, eligible


def _is_dst_transition_day(local_date: date, zone: ZoneInfo) -> bool:
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return end - start != timedelta(hours=24)


def _profile_for_day_type(
    days: list[_HistoricalDay],
    day_type: str,
    uncertainty_buffer: float,
    *,
    minimum_samples: int = MIN_BUCKET_SAMPLES,
) -> dict[str, list[float | None]]:
    expected: list[float | None] = []
    upper: list[float | None] = []
    for index in range(BUCKETS_PER_DAY):
        global_values = [day.values[index] for day in days if index in day.values]
        class_values = [day.values[index] for day in days if day.day_type == day_type and index in day.values]
        if len(global_values) < minimum_samples:
            expected.append(None)
            upper.append(None)
            continue
        global_expected = median(global_values)
        class_expected = median(class_values) if class_values else global_expected
        weight = len(class_values) / (len(class_values) + 3)
        value = global_expected * (1 - weight) + class_expected * weight
        global_upper = _percentile(global_values, 0.90)
        class_upper = _percentile(class_values, 0.90) if class_values else global_upper
        empirical_upper = global_upper * (1 - weight) + class_upper * weight
        expected.append(round(value, 6))
        upper.append(round(max(empirical_upper, value + uncertainty_buffer), 6))
    return {"expected": expected, "upper": upper}


def _rolling_validation(
    days: list[_HistoricalDay],
    *,
    comparison_days: list[_HistoricalDay] | None = None,
) -> dict[str, Any]:
    errors: list[float] = []
    squared_errors: list[float] = []
    baseline_errors: list[float] = []
    positive_residuals: list[float] = []
    upper_hits = 0
    samples = 0
    origins = 0
    by_date = {
        day.local_date: day
        for day in (comparison_days if comparison_days is not None else days)
    }
    for holdout in days[-HOLDOUT_ORIGINS:]:
        prior = [day for day in days if day.local_date < holdout.local_date]
        if len(prior) < MIN_VALIDATION_TRAINING_DAYS:
            continue
        previous = by_date.get(holdout.local_date - timedelta(days=1))
        if previous is None:
            continue
        buffer = _leave_one_out_buffer(prior, minimum_samples=MIN_VALIDATION_TRAINING_DAYS)
        profile = _profile_for_day_type(
            prior,
            holdout.day_type,
            buffer,
            minimum_samples=MIN_VALIDATION_TRAINING_DAYS,
        )
        origin_samples = 0
        for index, actual in holdout.values.items():
            expected = profile["expected"][index]
            upper = profile["upper"][index]
            baseline = previous.values.get(index)
            if expected is None or upper is None or baseline is None:
                continue
            error = abs(actual - expected)
            errors.append(error)
            positive_residuals.append(max(actual - expected, 0.0))
            squared_errors.append(error**2)
            baseline_errors.append(abs(actual - baseline))
            upper_hits += int(actual <= upper)
            samples += 1
            origin_samples += 1
        if origin_samples:
            origins += 1
    mae = sum(errors) / samples if samples else None
    rmse = sqrt(sum(squared_errors) / samples) if samples else None
    baseline_mae = sum(baseline_errors) / samples if samples else None
    return {
        "origin_count": origins,
        "sample_count": samples,
        "mae_kw": round(mae, 6) if mae is not None else None,
        "rmse_kw": round(rmse, 6) if rmse is not None else None,
        "persistence_mae_kw": round(baseline_mae, 6) if baseline_mae is not None else None,
        "upper_coverage": round(upper_hits / samples, 6) if samples else None,
        "positive_residual_p90_kw": round(_percentile(positive_residuals, 0.90), 6),
    }


def _leave_one_out_buffer(
    days: list[_HistoricalDay],
    *,
    minimum_samples: int = MIN_BUCKET_SAMPLES,
) -> float:
    residuals: list[float] = []
    for target in days:
        training = [day for day in days if day.local_date != target.local_date]
        if len(training) < minimum_samples:
            continue
        profile = _profile_for_day_type(
            training,
            target.day_type,
            0.0,
            minimum_samples=minimum_samples,
        )
        for index, actual in target.values.items():
            expected = profile["expected"][index]
            if expected is not None:
                residuals.append(max(actual - expected, 0.0))
    return _percentile(residuals, 0.90) if residuals else 0.0


def _quality_failures(
    *,
    complete_days: int,
    history_coverage: float,
    validation: dict[str, Any],
    profiles: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if complete_days < MIN_COMPLETE_DAYS:
        failures.append("insufficient_complete_days")
    if history_coverage < MIN_DAY_COVERAGE:
        failures.append("insufficient_history_coverage")
    if validation.get("origin_count", 0) < HOLDOUT_ORIGINS or validation.get("sample_count", 0) < MIN_HOLDOUT_SAMPLES:
        failures.append("insufficient_holdout_samples")
    mae = validation.get("mae_kw")
    baseline_mae = validation.get("persistence_mae_kw")
    if isinstance(mae, int | float) and isinstance(baseline_mae, int | float):
        if mae > float(baseline_mae) * MAX_BASELINE_MAE_RATIO:
            failures.append("forecast_accuracy_below_persistence_gate")
    coverage = validation.get("upper_coverage")
    if isinstance(coverage, int | float) and coverage < MIN_UPPER_COVERAGE:
        failures.append("conservative_bound_coverage_below_gate")
    if any(all(value is None for value in profile.get("expected", [])) for profile in profiles.values()):
        failures.append("forecast_profile_unavailable")
    return failures


def _validated_model(
    model: dict[str, Any] | None,
    source_entity_id: str,
    timezone: str,
) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    if model.get("model_version") != MODEL_VERSION or model.get("contract_version") != FORECAST_CONTRACT_VERSION:
        return None
    if (
        model.get("source_entity_id") != source_entity_id
        or model.get("timezone") != str(_timezone(timezone))
        or not isinstance(model.get("profiles"), dict)
    ):
        return None
    for day_type in ("weekday", "weekend"):
        profile = model["profiles"].get(day_type)
        if not isinstance(profile, dict):
            return None
        if not all(
            isinstance(profile.get(key), list) and len(profile[key]) == BUCKETS_PER_DAY
            for key in ("expected", "upper")
        ):
            return None
        expected = profile["expected"]
        upper = profile["upper"]
        if any(not _valid_profile_number(value) for value in (*expected, *upper)):
            return None
        if any(
            isinstance(expected_value, int | float)
            and isinstance(upper_value, int | float)
            and upper_value < expected_value
            for expected_value, upper_value in zip(expected, upper, strict=True)
        ):
            return None
    status = model.get("status")
    if status not in {"learning", "ready", "failed"}:
        return None
    complete_days = model.get("complete_days")
    history_coverage = model.get("history_coverage")
    validation = model.get("validation")
    if status == "ready" and (
        model.get("quality_ready") is not True
        or model.get("quality_failures") not in ([], None)
        or not isinstance(complete_days, int)
        or isinstance(complete_days, bool)
        or not _valid_fraction(history_coverage)
        or not isinstance(validation, dict)
        or not _valid_validation_metrics(validation)
        or _quality_failures(
            complete_days=(
                complete_days
                if isinstance(complete_days, int) and not isinstance(complete_days, bool)
                else 0
            ),
            history_coverage=float(history_coverage) if _valid_fraction(history_coverage) else 0.0,
            validation=validation if isinstance(validation, dict) else {},
            profiles=model["profiles"],
        )
    ):
        return None
    return model


def _valid_profile_number(value: Any) -> bool:
    return value is None or _valid_non_negative_number(value)


def _valid_validation_metrics(validation: dict[str, Any]) -> bool:
    """Return whether persisted ready-model validation evidence is complete."""
    return (
        isinstance(validation.get("origin_count"), int)
        and not isinstance(validation.get("origin_count"), bool)
        and validation["origin_count"] >= 0
        and isinstance(validation.get("sample_count"), int)
        and not isinstance(validation.get("sample_count"), bool)
        and validation["sample_count"] >= 0
        and all(
            _valid_non_negative_number(validation.get(key))
            for key in ("mae_kw", "rmse_kw", "persistence_mae_kw")
        )
        and _valid_fraction(validation.get("upper_coverage"))
    )


def _valid_non_negative_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value) and value >= 0


def _valid_fraction(value: Any) -> bool:
    return _valid_non_negative_number(value) and float(value) <= 1.0


def _recent_correction(
    profiles: dict[str, Any],
    now: datetime,
    zone: ZoneInfo,
    *,
    current_load_kw: float | None,
    current_ev_charging: bool | None,
) -> float:
    if current_load_kw is None or not isfinite(current_load_kw) or current_load_kw < 0 or current_ev_charging is True:
        return 1.0
    local = now.astimezone(zone)
    profile = profiles.get(_day_type(local.date()), {})
    expected = _profile_value(profile.get("expected"), local)
    if expected is None or expected <= 0:
        return 1.0
    return min(max(current_load_kw / expected, RECENT_CORRECTION_MIN), RECENT_CORRECTION_MAX)


def _profile_value(values: Any, local: datetime) -> float | None:
    if not isinstance(values, list) or len(values) != BUCKETS_PER_DAY:
        return None
    position = (local.hour * 60 + local.minute) / BUCKET_MINUTES
    lower = int(position) % BUCKETS_PER_DAY
    fraction = position - int(position)
    lower_value = _nearest_profile_value(values, lower)
    upper_value = _nearest_profile_value(values, (lower + 1) % BUCKETS_PER_DAY)
    if lower_value is None or upper_value is None:
        return lower_value if fraction == 0 else None
    return float(lower_value) * (1 - fraction) + float(upper_value) * fraction


def _profile_interval_average(
    profiles: dict[str, Any],
    start: datetime,
    *,
    interval_minutes: int,
    zone: ZoneInfo,
    profile_key: str,
) -> float | None:
    """Return the time-weighted profile average across one UTC planning slot."""
    total = 0.0
    for minute in range(interval_minutes):
        midpoint = start + timedelta(minutes=minute, seconds=30)
        local = midpoint.astimezone(zone)
        values = profiles[_day_type(local.date())][profile_key]
        index = local.hour * 4 + local.minute // BUCKET_MINUTES
        value = _nearest_profile_value(values, index)
        if value is None:
            return None
        total += value
    return total / interval_minutes


def _nearest_profile_value(values: list[Any], index: int) -> float | None:
    value = values[index]
    if isinstance(value, int | float) and isfinite(value):
        return float(value)
    before_distance = _distance_to_profile_value(values, index, -1)
    after_distance = _distance_to_profile_value(values, index, 1)
    missing_run = before_distance + after_distance - 1
    if missing_run > MAX_INTERPOLATION_BUCKETS:
        return None
    before = float(values[(index - before_distance) % BUCKETS_PER_DAY])
    after = float(values[(index + after_distance) % BUCKETS_PER_DAY])
    fraction = before_distance / (before_distance + after_distance)
    return before * (1 - fraction) + after * fraction


def _distance_to_profile_value(values: list[Any], index: int, direction: int) -> int:
    """Return the cyclic distance to the next finite profile value."""
    for distance in range(1, BUCKETS_PER_DAY + 1):
        candidate = values[(index + direction * distance) % BUCKETS_PER_DAY]
        if isinstance(candidate, int | float) and isfinite(candidate):
            return distance
    return BUCKETS_PER_DAY


def _model_details(model: dict[str, Any] | None, status: str, age_hours: float) -> dict[str, Any]:
    source = model if isinstance(model, dict) else {}
    return {
        "source": "built_in_recorder_history",
        "source_entity_id": source.get("source_entity_id"),
        "status": status,
        "model_version": source.get("model_version"),
        "contract_version": source.get("contract_version"),
        "trained_at": source.get("trained_at"),
        "last_attempt_at": source.get("last_attempt_at"),
        "last_attempt_source_entity_id": source.get("last_attempt_source_entity_id"),
        "last_training_status": source.get("last_training_status"),
        "last_training_quality_failures": list(source.get("last_training_quality_failures", []))[:8]
        if isinstance(source.get("last_training_quality_failures", []), list)
        else [],
        "last_training_validation": dict(source.get("last_training_validation", {}))
        if isinstance(source.get("last_training_validation"), dict)
        else {},
        "unusable_since": source.get("unusable_since"),
        "model_age_hours": round(age_hours, 3),
        "history_started_on": source.get("history_started_on"),
        "history_ended_on": source.get("history_ended_on"),
        "history_days": source.get("history_days", 0),
        "complete_days": source.get("complete_days", 0),
        "history_coverage": source.get("history_coverage", 0.0),
        "quality_failures": list(source.get("quality_failures", []))[:8]
        if isinstance(source.get("quality_failures", []), list)
        else [],
        "validation": dict(source.get("validation", {})) if isinstance(source.get("validation"), dict) else {},
        "cleaning": dict(source.get("cleaning", {})) if isinstance(source.get("cleaning"), dict) else {},
    }


def _floor_bucket(value: datetime) -> datetime:
    value = _as_utc(value)
    minute = value.minute - value.minute % BUCKET_MINUTES
    return value.replace(minute=minute, second=0, microsecond=0)


def _day_type(value: date) -> str:
    return "weekend" if value.weekday() >= 5 else "weekday"


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction
