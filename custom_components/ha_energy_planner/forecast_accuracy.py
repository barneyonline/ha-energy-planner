"""Time-aligned forecast accuracy metrics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from math import isfinite, sqrt
from typing import Any


def summarize_forecast_accuracy(
    samples: Iterable[Mapping[str, Any]],
    buckets: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Calculate forecast and persistence-baseline errors by lead-time bucket."""
    prepared = [_validated_sample(sample) for sample in samples]
    if not prepared:
        raise ValueError("forecast accuracy requires at least one time-aligned sample")
    bucket_summaries: dict[str, dict[str, float | int]] = {}
    for bucket in buckets:
        name = str(bucket["name"])
        minimum = float(bucket.get("min_hours", 0.0))
        maximum = float(bucket["max_hours"])
        if maximum <= minimum:
            raise ValueError(f"invalid forecast horizon bucket {name!r}")
        selected = [sample for sample in prepared if minimum <= sample["lead_hours"] < maximum]
        if selected:
            bucket_summaries[name] = _metrics(selected)
    return {
        "sample_count": len(prepared),
        "origin_count": len({sample["issued_at"] for sample in prepared}),
        "overall": _metrics(prepared),
        "horizon_buckets": bucket_summaries,
    }


def accuracy_threshold_errors(summary: Mapping[str, Any], requirements: Mapping[str, Any]) -> list[str]:
    """Return deterministic validation failures for configured accuracy requirements."""
    errors: list[str] = []
    min_origins = int(requirements.get("min_origins", 1))
    if int(summary.get("origin_count", 0)) < min_origins:
        errors.append(f"origin_count below {min_origins}")
    min_samples = int(requirements.get("min_samples_per_bucket", 1))
    max_baseline_ratio = float(requirements.get("max_baseline_mae_ratio", 1.0))
    max_mae = requirements.get("max_mae")
    buckets = dict(summary.get("horizon_buckets", {}))
    for name in requirements.get("required_buckets", buckets):
        metrics = buckets.get(str(name))
        if metrics is None:
            errors.append(f"missing horizon bucket {name}")
            continue
        if int(metrics["sample_count"]) < min_samples:
            errors.append(f"horizon bucket {name} has fewer than {min_samples} samples")
        baseline_mae = float(metrics["baseline_mae"])
        forecast_mae = float(metrics["forecast_mae"])
        if forecast_mae > baseline_mae * max_baseline_ratio:
            errors.append(
                f"horizon bucket {name} forecast MAE {forecast_mae:.4f} exceeds "
                f"baseline allowance {baseline_mae * max_baseline_ratio:.4f}"
            )
        if max_mae is not None and forecast_mae > float(max_mae):
            errors.append(f"horizon bucket {name} forecast MAE {forecast_mae:.4f} exceeds {float(max_mae):.4f}")
    return errors


def _validated_sample(sample: Mapping[str, Any]) -> dict[str, Any]:
    issued_at = str(sample["issued_at"])
    valid_at = str(sample["valid_at"])
    lead_hours = float(sample["lead_hours"])
    issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    valid = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
    actual_lead_hours = (valid - issued).total_seconds() / 3600
    if actual_lead_hours < 0:
        raise ValueError(f"forecast valid_at precedes issued_at: {issued_at} -> {valid_at}")
    if abs(actual_lead_hours - lead_hours) > 1 / 60:
        raise ValueError(f"forecast lead_hours is not aligned with issued_at and valid_at: {issued_at} -> {valid_at}")
    values = {
        "forecast": float(sample["forecast"]),
        "actual": float(sample["actual"]),
        "baseline": float(sample["baseline"]),
    }
    if not isfinite(lead_hours) or not all(isfinite(value) for value in values.values()):
        raise ValueError(f"forecast accuracy sample contains a non-finite number: {issued_at} -> {valid_at}")
    return {
        "issued_at": issued_at,
        "valid_at": valid_at,
        "lead_hours": lead_hours,
        **values,
    }


def _metrics(samples: list[Mapping[str, Any]]) -> dict[str, float | int]:
    forecast_errors = [float(sample["forecast"]) - float(sample["actual"]) for sample in samples]
    baseline_errors = [float(sample["baseline"]) - float(sample["actual"]) for sample in samples]
    count = len(samples)
    return {
        "sample_count": count,
        "forecast_mae": round(sum(abs(error) for error in forecast_errors) / count, 6),
        "forecast_rmse": round(sqrt(sum(error * error for error in forecast_errors) / count), 6),
        "baseline_mae": round(sum(abs(error) for error in baseline_errors) / count, 6),
        "baseline_rmse": round(sqrt(sum(error * error for error in baseline_errors) / count), 6),
    }
