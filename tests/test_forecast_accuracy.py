"""Tests for rolling, time-aligned forecast accuracy evidence."""

from __future__ import annotations

import pytest

from custom_components.ha_energy_planner.forecast_accuracy import (
    accuracy_threshold_errors,
    summarize_forecast_accuracy,
)

BUCKETS = [
    {"name": "near", "min_hours": 0, "max_hours": 4},
    {"name": "day", "min_hours": 4, "max_hours": 12},
]


def test_accuracy_summary_reports_mae_rmse_and_persistence_baseline() -> None:
    samples = [
        {
            "issued_at": "2026-06-01T00:00:00+00:00",
            "valid_at": "2026-06-01T01:00:00+00:00",
            "lead_hours": 1,
            "forecast": 2.0,
            "actual": 1.0,
            "baseline": 3.0,
        },
        {
            "issued_at": "2026-06-02T00:00:00+00:00",
            "valid_at": "2026-06-02T06:00:00+00:00",
            "lead_hours": 6,
            "forecast": 4.0,
            "actual": 4.0,
            "baseline": 2.0,
        },
    ]

    summary = summarize_forecast_accuracy(samples, BUCKETS)

    assert summary["origin_count"] == 2
    assert summary["overall"] == {
        "sample_count": 2,
        "forecast_mae": 0.5,
        "forecast_rmse": 0.707107,
        "baseline_mae": 2.0,
        "baseline_rmse": 2.0,
    }
    assert summary["horizon_buckets"]["near"]["sample_count"] == 1
    assert summary["horizon_buckets"]["day"]["sample_count"] == 1


def test_accuracy_summary_rejects_misaligned_lead_time_and_empty_samples() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize_forecast_accuracy([], BUCKETS)
    with pytest.raises(ValueError, match="not aligned"):
        summarize_forecast_accuracy(
            [
                {
                    "issued_at": "2026-06-01T00:00:00+00:00",
                    "valid_at": "2026-06-01T02:00:00+00:00",
                    "lead_hours": 1,
                    "forecast": 1,
                    "actual": 1,
                    "baseline": 1,
                }
            ],
            BUCKETS,
        )
    with pytest.raises(ValueError, match="precedes"):
        summarize_forecast_accuracy(
            [
                {
                    "issued_at": "2026-06-01T02:00:00+00:00",
                    "valid_at": "2026-06-01T01:00:00+00:00",
                    "lead_hours": -1,
                    "forecast": 1,
                    "actual": 1,
                    "baseline": 1,
                }
            ],
            BUCKETS,
        )
    with pytest.raises(ValueError, match="invalid forecast horizon bucket"):
        summarize_forecast_accuracy(
            [
                {
                    "issued_at": "2026-06-01T00:00:00+00:00",
                    "valid_at": "2026-06-01T01:00:00+00:00",
                    "lead_hours": 1,
                    "forecast": 1,
                    "actual": 1,
                    "baseline": 1,
                }
            ],
            [{"name": "bad", "min_hours": 2, "max_hours": 1}],
        )
    with pytest.raises(ValueError, match="non-finite"):
        summarize_forecast_accuracy(
            [
                {
                    "issued_at": "2026-06-01T00:00:00+00:00",
                    "valid_at": "2026-06-01T01:00:00+00:00",
                    "lead_hours": 1,
                    "forecast": "nan",
                    "actual": 1,
                    "baseline": 1,
                }
            ],
            BUCKETS,
        )


def test_accuracy_thresholds_require_each_bucket_to_beat_baseline() -> None:
    summary = {
        "origin_count": 1,
        "horizon_buckets": {
            "near": {"sample_count": 1, "forecast_mae": 2.0, "baseline_mae": 1.0},
        },
    }

    errors = accuracy_threshold_errors(
        summary,
        {
            "min_origins": 2,
            "min_samples_per_bucket": 2,
            "required_buckets": ["near", "day"],
            "max_baseline_mae_ratio": 1.0,
            "max_mae": 1.5,
        },
    )

    assert errors == [
        "origin_count below 2",
        "horizon bucket near has fewer than 2 samples",
        "horizon bucket near forecast MAE 2.0000 exceeds baseline allowance 1.0000",
        "horizon bucket near forecast MAE 2.0000 exceeds 1.5000",
        "missing horizon bucket day",
    ]
