"""Tests for compact forecast calibration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.forecast_calibration import (
    _bounded_factor,
    _finite_float_or_none,
    _parse_datetime_or_none,
    apply_forecast_calibration,
    update_forecast_calibration,
)


def test_update_forecast_calibration_enables_bounded_improving_factor() -> None:
    now = datetime(2026, 6, 27, 1, 0, tzinfo=UTC)
    snapshots = [
        {
            "plan_id": f"plan-{index}",
            "forecast_training_slots": [
                {
                    "valid_at": (now - timedelta(minutes=5)).isoformat(),
                    "pv_forecast_kw": 1.0,
                }
            ],
        }
        for index in range(12)
    ]

    model, changed = update_forecast_calibration(
        {},
        snapshots,
        {"pv_forecast_kw": 2.0},
        now=now,
    )

    assert changed is True
    assert model["pv_forecast_kw"]["sample_count"] == 12
    assert model["pv_forecast_kw"]["factor"] == 1.3
    assert model["pv_forecast_kw"]["enabled"] is True


def test_update_forecast_calibration_deduplicates_samples() -> None:
    now = datetime(2026, 6, 27, 1, 0, tzinfo=UTC)
    snapshot = {
        "plan_id": "plan-1",
        "forecast_training_slots": [
            {
                "valid_at": (now - timedelta(minutes=5)).isoformat(),
                "baseline_load_forecast_kw": 1.0,
            }
        ],
    }

    model, changed = update_forecast_calibration(
        {},
        [snapshot],
        {"baseline_load_forecast_kw": 2.0},
        now=now,
    )
    model, changed_again = update_forecast_calibration(
        model,
        [snapshot],
        {"baseline_load_forecast_kw": 2.0},
        now=now,
    )

    assert changed is True
    assert changed_again is False
    assert model["baseline_load_forecast_kw"]["sample_count"] == 1


def test_apply_forecast_calibration_requires_enabled_model() -> None:
    values = [1.0, None, 2.0]
    disabled = {"pv_forecast_kw": {"enabled": False, "factor": 1.3, "sample_count": 12}}
    enabled = {"pv_forecast_kw": {"enabled": True, "factor": 1.3, "sample_count": 12}}

    assert apply_forecast_calibration(values, disabled, "pv_forecast_kw") == values
    assert apply_forecast_calibration(values, enabled, "pv_forecast_kw") == [1.3, None, 2.6]


def test_forecast_calibration_rejects_non_finite_numbers() -> None:
    now = datetime(2026, 6, 27, 1, 0, tzinfo=UTC)
    snapshot = {
        "plan_id": "plan-1",
        "forecast_training_slots": [
            {
                "valid_at": (now - timedelta(minutes=5)).isoformat(),
                "pv_forecast_kw": "nan",
            }
        ],
    }
    enabled_with_bad_factor = {"pv_forecast_kw": {"enabled": True, "factor": "inf", "sample_count": 12}}

    model, changed = update_forecast_calibration(
        {},
        [snapshot],
        {"pv_forecast_kw": 2.0},
        now=now,
    )

    assert changed is False
    assert model == {}
    assert apply_forecast_calibration([1.0], enabled_with_bad_factor, "pv_forecast_kw") == [1.0]


def test_forecast_calibration_rejects_invalid_factors_and_samples() -> None:
    now = datetime(2026, 6, 27, 1, 0, tzinfo=UTC)
    snapshots = [
        {"plan_id": "plan-1", "forecast_training_slots": ["bad"]},
        {"plan_id": "plan-2", "forecast_training_slots": [{"valid_at": "bad", "pv_forecast_kw": 1.0}]},
        {
            "plan_id": "plan-3",
            "forecast_training_slots": [
                {"valid_at": (now + timedelta(hours=1)).isoformat(), "pv_forecast_kw": 1.0}
            ],
        },
        {
            "plan_id": "plan-4",
            "forecast_training_slots": [
                {"valid_at": (now - timedelta(minutes=5)).isoformat(), "pv_forecast_kw": -1.0}
            ],
        },
    ]

    model, changed = update_forecast_calibration(
        {"pv_forecast_kw": "bad-shape"},
        snapshots,
        {"pv_forecast_kw": 2.0},
        now=now,
    )

    assert changed is False
    assert model == {"pv_forecast_kw": "bad-shape"}
    assert update_forecast_calibration({}, [], {"pv_forecast_kw": None}, now=now) == ({}, False)
    assert apply_forecast_calibration([1.0], {"pv_forecast_kw": {"enabled": True, "factor": "bad"}}, "pv_forecast_kw") == [1.0]
    assert apply_forecast_calibration(["bad", -2.0], {"pv_forecast_kw": {"enabled": True, "factor": 1.2}}, "pv_forecast_kw") == [None, 0.0]
    assert _bounded_factor(float("nan")) == 1.0
    assert _finite_float_or_none("bad") is None
    assert _parse_datetime_or_none(datetime(2026, 6, 27, tzinfo=UTC)) == datetime(2026, 6, 27, tzinfo=UTC)
    assert _parse_datetime_or_none(123) is None
    assert _parse_datetime_or_none("bad") is None
