"""Compact conservative HVAC thermal model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import Any

MIN_THERMAL_SAMPLES = 12
MIN_HVAC_LOAD_KW = 0.2
MAX_HVAC_LOAD_KW = 10.0
ACTIVE_POWER_THRESHOLD_KW = 0.1


def update_thermal_model(
    model: Mapping[str, Any] | None,
    previous_sample: Mapping[str, Any] | None,
    current_sample: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Update compact HVAC thermal model statistics from adjacent samples."""
    updated = {key: dict(value) if isinstance(value, Mapping) else value for key, value in dict(model or {}).items()}
    previous = dict(previous_sample or {})
    current = dict(current_sample or {})
    updated["last_sample"] = current
    if not previous:
        return updated, True

    previous_time = _parse_datetime_or_none(previous.get("sampled_at"))
    current_time = _parse_datetime_or_none(current.get("sampled_at"))
    previous_time, current_time = _aligned_datetimes(previous_time, current_time)
    previous_temp = _float_or_none(previous.get("indoor_temperature_c"))
    current_temp = _float_or_none(current.get("indoor_temperature_c"))
    previous_power = _float_or_none(previous.get("hvac_power_kw"))
    if (
        previous_time is None
        or current_time is None
        or current_time <= previous_time
        or previous_temp is None
        or current_temp is None
    ):
        return updated, True

    hours = (current_time - previous_time).total_seconds() / 3600
    if hours <= 0 or hours > 2:
        return updated, True

    if previous_power is not None and previous_power >= ACTIVE_POWER_THRESHOLD_KW:
        _add_average(updated, "active_hvac_load_kw", _clamp(previous_power, MIN_HVAC_LOAD_KW, MAX_HVAC_LOAD_KW))
    else:
        drift_per_hour = (current_temp - previous_temp) / hours
        _add_average(updated, "passive_indoor_drift_c_per_hour", _clamp(drift_per_hour, -5.0, 5.0))
    _refresh_enabled(updated)
    return updated, True


def thermal_hvac_load_kw(model: Mapping[str, Any] | None, fallback_kw: float) -> float:
    """Return learned active HVAC load when the model is mature enough."""
    active = dict((model or {}).get("active_hvac_load_kw", {}))
    if not (model or {}).get("enabled"):
        return fallback_kw
    if _int_or_zero(active.get("sample_count")) < MIN_THERMAL_SAMPLES:
        return fallback_kw
    value = _float_or_none(active.get("average"))
    if value is None:
        return fallback_kw
    return round(_clamp(value, MIN_HVAC_LOAD_KW, MAX_HVAC_LOAD_KW), 4)


def thermal_model_summary(model: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return compact model metadata for diagnostics."""
    active = dict((model or {}).get("active_hvac_load_kw", {}))
    drift = dict((model or {}).get("passive_indoor_drift_c_per_hour", {}))
    return {
        "enabled": bool((model or {}).get("enabled", False)),
        "active_sample_count": _int_or_zero(active.get("sample_count")),
        "active_hvac_load_kw": _float_or_none(active.get("average")),
        "passive_sample_count": _int_or_zero(drift.get("sample_count")),
        "passive_indoor_drift_c_per_hour": _float_or_none(drift.get("average")),
    }


def _add_average(model: dict[str, Any], key: str, value: float) -> None:
    bucket = dict(model.get(key, {}))
    count = _int_or_zero(bucket.get("sample_count"))
    total = (_float_or_none(bucket.get("sum")) or 0.0) + value
    sample_count = count + 1
    bucket.update(
        {
            "sample_count": sample_count,
            "sum": round(total, 6),
            "average": round(total / sample_count, 6),
        }
    )
    model[key] = bucket


def _refresh_enabled(model: dict[str, Any]) -> None:
    active = dict(model.get("active_hvac_load_kw", {}))
    model["enabled"] = _int_or_zero(active.get("sample_count")) >= MIN_THERMAL_SAMPLES


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _aligned_datetimes(left: datetime | None, right: datetime | None) -> tuple[datetime | None, datetime | None]:
    if left is None or right is None:
        return left, right
    if left.tzinfo is None and right.tzinfo is not None:
        return left.replace(tzinfo=right.tzinfo), right
    if left.tzinfo is not None and right.tzinfo is None:
        return left, right.replace(tzinfo=left.tzinfo)
    return left, right


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip()
        if "," in value and "." not in value:
            value = value.replace(",", ".")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _int_or_zero(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)
