"""Time-aligned, out-of-sample forecast calibration helpers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from statistics import median
from typing import Any

FORECAST_CALIBRATION_FIELDS = ("pv_forecast_kw", "baseline_load_forecast_kw")
MIN_CALIBRATION_SAMPLES = 48
MIN_HOLDOUT_SAMPLES = 12
MIN_SAMPLE_SPAN = timedelta(hours=6)
MAX_OBSERVATION_SKEW = timedelta(seconds=90)
MAX_STORED_SAMPLES = 768
MAX_LEAD_BUCKETS_PER_TIMESTAMP = 4
MIN_FACTOR = 0.70
MAX_FACTOR = 1.30
MIN_UNCERTAINTY_FACTOR = 0.50
MAX_UNCERTAINTY_FACTOR = 1.50
MIN_HOLDOUT_IMPROVEMENT = 0.02


def apply_forecast_calibration(
    values: Iterable[float | None],
    model: Mapping[str, Any] | None,
    field: str,
    *,
    interval_minutes: int = 5,
    lead_offset_minutes: float = 0.0,
    uncertainty_mode: str | None = None,
) -> list[float | None]:
    """Adjust values with a proven expected or conservative lead-time factor."""
    calibration = dict((model or {}).get(field, {}))
    if calibration.get("model_version") != 3:
        return list(values)
    buckets = calibration.get("buckets")
    if not isinstance(buckets, Mapping):
        return list(values)
    adjusted: list[float | None] = []
    for index, value in enumerate(values):
        number = _finite_float_or_none(value)
        if number is None:
            adjusted.append(None)
            continue
        bucket = buckets.get(str(max(int((lead_offset_minutes + index * interval_minutes) // 30), 0)))
        calibration_enabled = (
            bucket.get("uncertainty_enabled", bucket.get("enabled"))
            if uncertainty_mode in {"lower", "upper"}
            else bucket.get("enabled")
        ) if isinstance(bucket, Mapping) else False
        if not isinstance(bucket, Mapping) or not calibration_enabled:
            adjusted.append(number)
            continue
        factor_key = {
            "lower": "lower_factor",
            "upper": "upper_factor",
        }.get(uncertainty_mode, "factor")
        try:
            factor_value = float(bucket.get(factor_key, bucket.get("factor", 1.0)))
        except (TypeError, ValueError):
            adjusted.append(number)
            continue
        factor = (
            _bounded_uncertainty_factor(factor_value)
            if uncertainty_mode in {"lower", "upper"}
            else _bounded_factor(factor_value)
        ) if isfinite(factor_value) else 1.0
        adjusted.append(round(max(number * factor, 0.0), 4))
    return adjusted


def update_forecast_calibration(
    model: Mapping[str, Any] | None,
    snapshots: Iterable[Mapping[str, Any]],
    actuals: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """Update calibration using only forecasts paired to timestamped observations.

    A bare current value is deliberately not accepted: it cannot safely represent
    every overdue forecast slot.  Callers may provide one ``{value, observed_at}``
    mapping, a list of those mappings, or a Recorder-style timestamp/value map.
    """
    updated = {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in dict(model or {}).items()
    }
    observations = {
        field: _observations_for_field(actuals.get(field), now)
        for field in FORECAST_CALIBRATION_FIELDS
    }
    if not any(observations.values()):
        return updated, False

    changed = False
    for field in FORECAST_CALIBRATION_FIELDS:
        field_observations = observations[field]
        if not field_observations:
            continue
        calibration = dict(updated.get(field, {})) if isinstance(updated.get(field), Mapping) else {}
        samples = _stored_samples(calibration.get("samples", []), field)
        seen = {str(sample["sample_id"]) for sample in samples}
        field_changed = False

        for snapshot in snapshots:
            slots = snapshot.get("forecast_training_slots", [])
            if not isinstance(slots, list):
                continue
            for slot in slots:
                if not isinstance(slot, Mapping):
                    continue
                valid_at = _parse_datetime_or_none(slot.get("valid_at"))
                issued_at = _parse_datetime_or_none(slot.get(f"{field}_issued_at", slot.get("issued_at")))
                if valid_at is None or issued_at is None or valid_at > _as_utc(now) or issued_at > valid_at:
                    continue
                observation = _nearest_observation(field_observations, valid_at)
                if observation is None or not _sample_valid(slot.get(field), observation[1]):
                    continue
                lead_bucket = max(int((valid_at - issued_at).total_seconds() // 1800), 0)
                sample_id = f"{field}:{valid_at.isoformat()}:{lead_bucket}"
                if sample_id in seen:
                    continue
                samples.append(
                    {
                        "sample_id": sample_id,
                        "valid_at": valid_at.isoformat(),
                        "lead_bucket": lead_bucket,
                        "forecast": round(float(slot[field]), 6),
                        "actual": round(observation[1], 6),
                    }
                )
                seen.add(sample_id)
                changed = True
                field_changed = True

        if field_changed:
            updated[field] = _rebuild_model(_trim_samples(samples))

    return updated, changed


def _trim_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound storage without allowing lead buckets to evict distinct observations."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in sorted(samples, key=lambda item: (item["valid_at"], item["lead_bucket"])):
        grouped[str(sample["valid_at"])].append(sample)
    retained: list[dict[str, Any]] = []
    max_timestamps = MAX_STORED_SAMPLES // MAX_LEAD_BUCKETS_PER_TIMESTAMP
    for valid_at in sorted(grouped)[-max_timestamps:]:
        group = grouped[valid_at]
        if len(group) <= MAX_LEAD_BUCKETS_PER_TIMESTAMP:
            retained.extend(group)
            continue
        last = len(group) - 1
        retained.extend(
            group[round(index * last / (MAX_LEAD_BUCKETS_PER_TIMESTAMP - 1))]
            for index in range(MAX_LEAD_BUCKETS_PER_TIMESTAMP)
        )
    return retained


def _rebuild_model(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Build and validate an independent robust factor for each lead bucket."""
    grouped_by_bucket: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped_by_bucket[int(sample["lead_bucket"])].append(sample)
    buckets = {
        str(lead_bucket): _rebuild_bucket(bucket_samples)
        for lead_bucket, bucket_samples in sorted(grouped_by_bucket.items())
    }
    enabled_buckets = [bucket for bucket in buckets.values() if bucket["enabled"]]
    uncertainty_buckets = [bucket for bucket in buckets.values() if bucket["uncertainty_enabled"]]
    return {
        "model_version": 3,
        "sample_count": len({str(sample["valid_at"]) for sample in samples}),
        "raw_sample_count": len(samples),
        "factor": enabled_buckets[0]["factor"] if enabled_buckets else 1.0,
        "enabled": bool(enabled_buckets),
        "uncertainty_enabled": bool(uncertainty_buckets),
        "buckets": buckets,
        "samples": samples,
    }


def _rebuild_bucket(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Train on earlier timestamps and score this lead bucket on a later holdout."""
    by_valid_at: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_valid_at[str(sample["valid_at"])].append(sample)
    timestamp_groups = sorted(by_valid_at.items())
    unique_count = len(timestamp_groups)
    holdout_count = max(MIN_HOLDOUT_SAMPLES, unique_count // 4)
    split = max(unique_count - holdout_count, 0)
    training = timestamp_groups[:split]
    holdout = timestamp_groups[split:]

    training_ratios = [
        median(float(item["actual"]) / float(item["forecast"]) for item in group)
        for _valid_at, group in training
    ]
    factor = _bounded_factor(median(training_ratios)) if training_ratios else 1.0
    lower_factor = _bounded_uncertainty_factor(_percentile(training_ratios, 0.10)) if training_ratios else 1.0
    upper_factor = _bounded_uncertainty_factor(_percentile(training_ratios, 0.90)) if training_ratios else 1.0
    lower_factor = min(lower_factor, factor)
    upper_factor = max(upper_factor, factor)
    raw_errors = [_group_absolute_pct_error(group, 1.0) for _valid_at, group in holdout]
    calibrated_errors = [_group_absolute_pct_error(group, factor) for _valid_at, group in holdout]
    raw_error = sum(raw_errors)
    calibrated_error = sum(calibrated_errors)
    first_at = _parse_datetime_or_none(timestamp_groups[0][0]) if timestamp_groups else None
    last_at = _parse_datetime_or_none(timestamp_groups[-1][0]) if timestamp_groups else None
    sufficient_span = bool(first_at and last_at and last_at - first_at >= MIN_SAMPLE_SPAN)
    enabled = (
        unique_count >= MIN_CALIBRATION_SAMPLES
        and len(holdout) >= MIN_HOLDOUT_SAMPLES
        and sufficient_span
        and raw_error > 0
        and calibrated_error <= raw_error * (1.0 - MIN_HOLDOUT_IMPROVEMENT)
    )
    uncertainty_enabled = (
        unique_count >= MIN_CALIBRATION_SAMPLES
        and len(training) >= MIN_CALIBRATION_SAMPLES - holdout_count
        and sufficient_span
    )
    return {
        "sample_count": unique_count,
        "training_sample_count": len(training),
        "holdout_sample_count": len(holdout),
        "factor": factor,
        "lower_factor": lower_factor,
        "upper_factor": upper_factor,
        "raw_abs_pct_error_sum": round(raw_error, 6),
        "calibrated_abs_pct_error_sum": round(calibrated_error, 6),
        "enabled": enabled,
        "uncertainty_enabled": uncertainty_enabled,
    }


def _group_absolute_pct_error(group: list[dict[str, Any]], factor: float) -> float:
    forecasts = [float(item["forecast"]) for item in group]
    actual = median(float(item["actual"]) for item in group)
    forecast = median(forecasts) * factor
    return _absolute_pct_error(forecast, actual)


def _stored_samples(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    valid: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        valid_at = _parse_datetime_or_none(item.get("valid_at"))
        if valid_at is None or not _sample_valid(item.get("forecast"), item.get("actual")):
            continue
        try:
            lead_bucket = max(int(item.get("lead_bucket", 0)), 0)
        except (TypeError, ValueError):
            continue
        valid.append(
            {
                "sample_id": str(item.get("sample_id") or f"{field}:{valid_at.isoformat()}:{lead_bucket}"),
                "valid_at": valid_at.isoformat(),
                "lead_bucket": lead_bucket,
                "forecast": float(item["forecast"]),
                "actual": float(item["actual"]),
            }
        )
    return valid


def _observations_for_field(value: Any, now: datetime) -> list[tuple[datetime, float]]:
    candidates: list[Any]
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, Mapping) and "value" not in value and "observed_at" not in value:
        candidates = [{"observed_at": timestamp, "value": observed} for timestamp, observed in value.items()]
    else:
        candidates = [value]
    observations: list[tuple[datetime, float]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        observed_at = _parse_datetime_or_none(candidate.get("observed_at"))
        observed = _finite_float_or_none(candidate.get("value"))
        if observed_at is None or observed is None or observed < 0 or observed_at > _as_utc(now):
            continue
        observations.append((observed_at, observed))
    return observations


def _nearest_observation(
    observations: list[tuple[datetime, float]], valid_at: datetime
) -> tuple[datetime, float] | None:
    if not observations:
        return None
    nearest = min(observations, key=lambda item: abs(item[0] - valid_at))
    return nearest if abs(nearest[0] - valid_at) <= MAX_OBSERVATION_SKEW else None


def _sample_valid(forecast: Any, actual: Any) -> bool:
    forecast_float = _finite_float_or_none(forecast)
    actual_float = _finite_float_or_none(actual)
    return forecast_float is not None and actual_float is not None and forecast_float > 0 and actual_float >= 0


def _absolute_pct_error(forecast: float, actual: float) -> float:
    denominator = max(abs(actual), 0.1)
    return abs(actual - forecast) / denominator


def _bounded_factor(value: float) -> float:
    if not isfinite(value):
        return 1.0
    return round(min(max(value, MIN_FACTOR), MAX_FACTOR), 4)


def _bounded_uncertainty_factor(value: float) -> float:
    if not isfinite(value):
        return 1.0
    return round(min(max(value, MIN_UNCERTAINTY_FACTOR), MAX_UNCERTAINTY_FACTOR), 4)


def _percentile(values: list[float], quantile: float) -> float:
    """Return a deterministic linearly interpolated percentile."""
    if not values:
        return 1.0
    ordered = sorted(values)
    position = min(max(quantile, 0.0), 1.0) * (len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def _finite_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
