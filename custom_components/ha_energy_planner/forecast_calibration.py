"""Compact forecast error calibration helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from math import isfinite
from typing import Any

FORECAST_CALIBRATION_FIELDS = ("pv_forecast_kw", "baseline_load_forecast_kw")
MIN_CALIBRATION_SAMPLES = 12
MIN_FACTOR = 0.70
MAX_FACTOR = 1.30


def apply_forecast_calibration(
    values: Iterable[float | None],
    model: Mapping[str, Any] | None,
    field: str,
) -> list[float | None]:
    """Return values adjusted by a proven bounded forecast calibration factor."""
    calibration = dict((model or {}).get(field, {}))
    if not calibration.get("enabled"):
        return list(values)
    try:
        factor_value = float(calibration.get("factor", 1.0))
    except (TypeError, ValueError):
        return list(values)
    if not isfinite(factor_value):
        return list(values)
    factor = _bounded_factor(factor_value)
    adjusted: list[float | None] = []
    for value in values:
        number = _finite_float_or_none(value)
        adjusted.append(None if number is None else round(max(number * factor, 0.0), 4))
    return adjusted


def update_forecast_calibration(
    model: Mapping[str, Any] | None,
    snapshots: Iterable[Mapping[str, Any]],
    actuals: Mapping[str, float | None],
    *,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """Update compact calibration statistics from due forecast snapshots."""
    updated = {key: dict(value) if isinstance(value, Mapping) else value for key, value in dict(model or {}).items()}
    if not any(_finite_float_or_none(actuals.get(field)) is not None for field in FORECAST_CALIBRATION_FIELDS):
        return updated, False
    changed = False
    seen = set(str(item) for item in updated.get("_seen_sample_ids", []))

    for snapshot in snapshots:
        plan_id = str(snapshot.get("plan_id", "unknown"))
        for slot in snapshot.get("forecast_training_slots", []):
            if not isinstance(slot, Mapping):
                continue
            valid_at = _parse_datetime_or_none(slot.get("valid_at"))
            if valid_at is None or valid_at > now:
                continue
            for field in FORECAST_CALIBRATION_FIELDS:
                actual = actuals.get(field)
                forecast = slot.get(field)
                sample_id = f"{plan_id}:{slot.get('valid_at')}:{field}"
                if sample_id in seen:
                    continue
                if not _sample_valid(forecast, actual):
                    continue
                seen.add(sample_id)
                _add_sample(updated, field, float(forecast), float(actual))
                changed = True

    if changed:
        updated["_seen_sample_ids"] = sorted(seen)[-200:]
    return updated, changed


def _add_sample(model: dict[str, Any], field: str, forecast: float, actual: float) -> None:
    calibration = dict(model.get(field, {}))
    count = int(calibration.get("sample_count", 0))
    ratio_sum = float(calibration.get("ratio_sum", 0.0)) + (actual / forecast)
    factor = _bounded_factor(ratio_sum / (count + 1))
    raw_error_sum = float(calibration.get("raw_abs_pct_error_sum", 0.0)) + _absolute_pct_error(forecast, actual)
    calibrated_error_sum = float(calibration.get("calibrated_abs_pct_error_sum", 0.0)) + _absolute_pct_error(
        forecast * factor,
        actual,
    )
    sample_count = count + 1
    calibration.update(
        {
            "sample_count": sample_count,
            "ratio_sum": round(ratio_sum, 6),
            "factor": factor,
            "raw_abs_pct_error_sum": round(raw_error_sum, 6),
            "calibrated_abs_pct_error_sum": round(calibrated_error_sum, 6),
            "enabled": sample_count >= MIN_CALIBRATION_SAMPLES and calibrated_error_sum < raw_error_sum,
        }
    )
    model[field] = calibration


def _sample_valid(forecast: Any, actual: Any) -> bool:
    try:
        forecast_float = float(forecast)
        actual_float = float(actual)
    except (TypeError, ValueError):
        return False
    return isfinite(forecast_float) and isfinite(actual_float) and forecast_float > 0 and actual_float >= 0


def _absolute_pct_error(forecast: float, actual: float) -> float:
    denominator = max(abs(actual), 0.001)
    return abs(actual - forecast) / denominator


def _bounded_factor(value: float) -> float:
    if not isfinite(value):
        return 1.0
    return round(min(max(value, MIN_FACTOR), MAX_FACTOR), 4)


def _finite_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
