"""Compact conservative HVAC thermal model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from statistics import median
from typing import Any

THERMAL_MODEL_VERSION = 2
MIN_THERMAL_SAMPLES = 12
MIN_HVAC_LOAD_KW = 0.2
MAX_HVAC_LOAD_KW = 10.0
ACTIVE_POWER_THRESHOLD_KW = 0.1
MIN_SAMPLE_INTERVAL_MINUTES = 5
MAX_SAMPLE_INTERVAL_HOURS = 2
MIN_TEMPERATURE_DELTA_C = 0.05
MAX_ACTIVE_RATE_C_PER_HOUR = 6.0
MAX_PASSIVE_RATE_C_PER_HOUR = 3.0
MAX_ROLLING_SAMPLES = 96
_INACTIVE_HVAC_MODES = {"off", "idle"}
_ACTIVE_HVAC_MODES = {"heat", "cool"}


def update_thermal_model(
    model: Mapping[str, Any] | None,
    previous_sample: Mapping[str, Any] | None,
    current_sample: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Update compact HVAC thermal model statistics from adjacent samples."""
    updated, _migrated = _migrate_model(model, current_sample)
    previous = dict(previous_sample or {})
    current = dict(current_sample or {})
    if not previous and isinstance(updated.get("last_sample"), Mapping):
        previous = dict(updated["last_sample"])
    updated["last_sample"] = current
    # Never train across a migration boundary: the legacy anchor may be one of
    # the high-frequency/noisy samples that contaminated the old statistics.
    if _migrated or not previous:
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
    if hours < MIN_SAMPLE_INTERVAL_MINUTES / 60 or hours > MAX_SAMPLE_INTERVAL_HOURS:
        return updated, True

    current_power = _float_or_none(current.get("hvac_power_kw"))
    previous_mode = _hvac_mode(previous.get("hvac_mode"))
    current_mode = _hvac_mode(current.get("hvac_mode"))
    if previous_power is None or current_power is None or not previous_mode or not current_mode:
        return updated, True
    modes_stable = previous_mode == current_mode
    previous_active = previous_power is not None and previous_power >= ACTIVE_POWER_THRESHOLD_KW
    current_active = current_power is not None and current_power >= ACTIVE_POWER_THRESHOLD_KW
    temperature_delta = current_temp - previous_temp

    # Samples spanning an HVAC start, stop, or mode transition conflate two
    # operating regimes and are deliberately retained only as the next anchor.
    if not modes_stable or previous_active != current_active:
        return updated, True

    if previous_active and current_active and current_mode in _ACTIVE_HVAC_MODES:
        if abs(temperature_delta) >= MIN_TEMPERATURE_DELTA_C:
            active_rate = temperature_delta / hours
            if 0 < active_rate <= MAX_ACTIVE_RATE_C_PER_HOUR and current_mode != "cool":
                if MIN_HVAC_LOAD_KW <= previous_power <= MAX_HVAC_LOAD_KW:
                    _add_rolling_stat(updated, "active_hvac_load_kw", previous_power)
                _add_rolling_stat(updated, "active_heat_rate_c_per_hour", active_rate)
            elif -MAX_ACTIVE_RATE_C_PER_HOUR <= active_rate < 0 and current_mode != "heat":
                if MIN_HVAC_LOAD_KW <= previous_power <= MAX_HVAC_LOAD_KW:
                    _add_rolling_stat(updated, "active_hvac_load_kw", previous_power)
                _add_rolling_stat(updated, "active_cool_rate_c_per_hour", abs(active_rate))
    elif (
        not previous_active
        and not current_active
        and previous_mode in _INACTIVE_HVAC_MODES
        and current_mode in _INACTIVE_HVAC_MODES
        and abs(temperature_delta) >= MIN_TEMPERATURE_DELTA_C
    ):
        drift_per_hour = temperature_delta / hours
        if abs(drift_per_hour) <= MAX_PASSIVE_RATE_C_PER_HOUR:
            _add_rolling_stat(updated, "passive_indoor_drift_c_per_hour", drift_per_hour)
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


def thermal_active_temperature_rate_c_per_hour(
    model: Mapping[str, Any] | None,
    mode: str,
    fallback_c_per_hour: float | None = None,
) -> float | None:
    """Return learned active heating/cooling rate when the model is mature enough."""
    key = "active_heat_rate_c_per_hour" if mode == "heat" else "active_cool_rate_c_per_hour"
    bucket = dict((model or {}).get(key, {}))
    if not (model or {}).get("enabled"):
        return fallback_c_per_hour
    if _int_or_zero(bucket.get("sample_count")) < 3:
        return fallback_c_per_hour
    value = _float_or_none(bucket.get("average"))
    if value is None:
        return fallback_c_per_hour
    return round(_clamp(value, 0.05, 10.0), 4)


def thermal_model_summary(model: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return compact model metadata for diagnostics."""
    active = dict((model or {}).get("active_hvac_load_kw", {}))
    heat_rate = dict((model or {}).get("active_heat_rate_c_per_hour", {}))
    cool_rate = dict((model or {}).get("active_cool_rate_c_per_hour", {}))
    drift = dict((model or {}).get("passive_indoor_drift_c_per_hour", {}))
    return {
        "model_version": _int_or_zero((model or {}).get("model_version")),
        "enabled": bool((model or {}).get("enabled", False)),
        "active_sample_count": _int_or_zero(active.get("sample_count")),
        "active_hvac_load_kw": _float_or_none(active.get("average")),
        "active_heat_rate_c_per_hour": _float_or_none(heat_rate.get("average")),
        "active_heat_rate_sample_count": _int_or_zero(heat_rate.get("sample_count")),
        "active_cool_rate_c_per_hour": _float_or_none(cool_rate.get("average")),
        "active_cool_rate_sample_count": _int_or_zero(cool_rate.get("sample_count")),
        "passive_sample_count": _int_or_zero(drift.get("sample_count")),
        "passive_indoor_drift_c_per_hour": _float_or_none(drift.get("average")),
    }


def _add_rolling_stat(model: dict[str, Any], key: str, value: float) -> None:
    """Add a value to a bounded window and expose its robust median."""
    bucket = dict(model.get(key, {}))
    values = (
        [_float_or_none(item) for item in bucket.get("values", [])]
        if isinstance(bucket.get("values"), list)
        else []
    )
    retained = [item for item in values if item is not None]
    retained.append(round(value, 6))
    retained = retained[-MAX_ROLLING_SAMPLES:]
    total_sample_count = _int_or_zero(bucket.get("total_sample_count", bucket.get("sample_count"))) + 1
    bucket.update(
        {
            "sample_count": len(retained),
            "total_sample_count": total_sample_count,
            "values": retained,
            "average": round(median(retained), 6),
        }
    )
    model[key] = bucket


def _migrate_model(
    model: Mapping[str, Any] | None,
    current_sample: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return a current model, resetting unsafe unversioned statistics."""
    source = dict(model or {})
    if source.get("model_version") == THERMAL_MODEL_VERSION:
        return {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in source.items()
        }, False
    reset: dict[str, Any] = {"model_version": THERMAL_MODEL_VERSION}
    if source:
        reset["migration"] = {
            "reset_reason": "legacy_unbounded_statistics",
            "reset_at": current_sample.get("sampled_at"),
        }
    return reset, bool(source)


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


def _hvac_mode(value: Any) -> str:
    """Return a normalized HVAC mode without inventing missing state."""
    return str(value or "").strip().lower()


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
