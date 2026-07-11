"""Tests for time-aligned forecast calibration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.forecast_calibration import (
    MIN_CALIBRATION_SAMPLES,
    _as_utc,
    _bounded_factor,
    _finite_float_or_none,
    _nearest_observation,
    _observations_for_field,
    _parse_datetime_or_none,
    _stored_samples,
    apply_forecast_calibration,
    update_forecast_calibration,
)


def _evidence(
    *,
    now: datetime,
    count: int = MIN_CALIBRATION_SAMPLES,
    forecast: float = 1.0,
    actual: float = 1.2,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    start = now - timedelta(minutes=10 * (count - 1))
    snapshots: list[dict[str, object]] = []
    observations: dict[str, float] = {}
    for index in range(count):
        valid_at = start + timedelta(minutes=10 * index)
        snapshots.append(
            {
                "plan_id": f"plan-{index}",
                "forecast_training_slots": [
                    {
                        "issued_at": valid_at - timedelta(hours=1),
                        "valid_at": valid_at,
                        "pv_forecast_kw": forecast,
                    }
                ],
            }
        )
        observations[valid_at.isoformat()] = actual
    return snapshots, {"pv_forecast_kw": observations}


def test_update_forecast_calibration_enables_bounded_out_of_sample_factor() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    snapshots, actuals = _evidence(now=now, actual=2.0)

    model, changed = update_forecast_calibration({}, snapshots, actuals, now=now)

    calibration = model["pv_forecast_kw"]
    assert changed is True
    assert calibration["sample_count"] == MIN_CALIBRATION_SAMPLES
    assert calibration["factor"] == 1.3
    assert calibration["buckets"]["2"]["training_sample_count"] == 36
    assert calibration["buckets"]["2"]["holdout_sample_count"] == 12
    assert calibration["enabled"] is True


def test_update_requires_timestamped_observation_and_rejects_overdue_slots() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    old = now - timedelta(hours=1)
    snapshots = [
        {
            "forecast_training_slots": [
                {"issued_at": old - timedelta(hours=1), "valid_at": old, "pv_forecast_kw": 1.0}
            ]
        }
    ]

    assert update_forecast_calibration({}, snapshots, {"pv_forecast_kw": 2.0}, now=now) == ({}, False)
    assert update_forecast_calibration(
        {},
        snapshots,
        {"pv_forecast_kw": {"value": 2.0, "observed_at": now}},
        now=now,
    ) == ({}, False)


def test_update_matches_small_timestamp_skew_and_deduplicates_lead_bucket() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    snapshots = [
        {
            "forecast_training_slots": [
                {
                    "issued_at": now - timedelta(minutes=65),
                    "valid_at": now - timedelta(minutes=5),
                    "baseline_load_forecast_kw": 1.0,
                }
            ]
        }
    ] * 2
    actual = {
        "baseline_load_forecast_kw": {
            "value": 2.0,
            "observed_at": now - timedelta(minutes=4, seconds=15),
        }
    }

    model, changed = update_forecast_calibration({}, snapshots, actual, now=now)
    model, changed_again = update_forecast_calibration(model, snapshots, actual, now=now)

    assert changed is True
    assert changed_again is False
    assert model["baseline_load_forecast_kw"]["sample_count"] == 1
    assert model["baseline_load_forecast_kw"]["raw_sample_count"] == 1


def test_field_issue_time_prevents_refresh_origins_creating_lead_samples() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    valid_at = now - timedelta(minutes=1)
    source_issued = valid_at - timedelta(hours=1)
    snapshots = [
        {
            "forecast_training_slots": [
                {
                    "issued_at": refresh_issued,
                    "pv_forecast_kw_issued_at": source_issued,
                    "valid_at": valid_at,
                    "pv_forecast_kw": 1.0,
                }
            ]
        }
        for refresh_issued in (valid_at - timedelta(minutes=5), valid_at - timedelta(hours=8))
    ]

    model, changed = update_forecast_calibration(
        {}, snapshots, {"pv_forecast_kw": {valid_at.isoformat(): 1.2}}, now=now
    )

    assert changed is True
    assert model["pv_forecast_kw"]["raw_sample_count"] == 1
    assert set(model["pv_forecast_kw"]["buckets"]) == {"2"}


def test_correlated_lead_buckets_do_not_count_as_distinct_observations() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    valid_at = now - timedelta(minutes=1)
    slots = [
        {
            "issued_at": valid_at - timedelta(minutes=30 * lead),
            "valid_at": valid_at,
            "pv_forecast_kw": 1.0,
        }
        for lead in range(1, 20)
    ]

    model, changed = update_forecast_calibration(
        {},
        [{"forecast_training_slots": slots}],
        {"pv_forecast_kw": {valid_at.isoformat(): 1.2}},
        now=now,
    )

    assert changed is True
    assert model["pv_forecast_kw"]["sample_count"] == 1
    assert model["pv_forecast_kw"]["raw_sample_count"] == 4
    assert set(model["pv_forecast_kw"]["buckets"]) == {"1", "7", "13", "19"}
    assert model["pv_forecast_kw"]["enabled"] is False


def test_calibration_requires_a_real_time_span_and_improving_holdout() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    snapshots, actuals = _evidence(now=now, count=MIN_CALIBRATION_SAMPLES, actual=1.2)
    # Compress all evidence into less than the required six-hour span.
    for index, snapshot in enumerate(snapshots):
        valid_at = now - timedelta(minutes=MIN_CALIBRATION_SAMPLES - index)
        slot = snapshot["forecast_training_slots"][0]  # type: ignore[index]
        slot["valid_at"] = valid_at
        slot["issued_at"] = valid_at - timedelta(hours=1)
    actuals = {
        "pv_forecast_kw": {
            (now - timedelta(minutes=MIN_CALIBRATION_SAMPLES - index)).isoformat(): 1.2
            for index in range(MIN_CALIBRATION_SAMPLES)
        }
    }

    model, _changed = update_forecast_calibration({}, snapshots, actuals, now=now)

    assert model["pv_forecast_kw"]["enabled"] is False


def test_apply_forecast_calibration_requires_enabled_finite_model() -> None:
    values = [1.0, None, 2.0]
    disabled = {"pv_forecast_kw": {"model_version": 3, "buckets": {"0": {"enabled": False, "factor": 1.3}}}}
    enabled = {"pv_forecast_kw": {"model_version": 3, "buckets": {"0": {"enabled": True, "factor": 1.3}}}}
    invalid = {"pv_forecast_kw": {"model_version": 3, "buckets": {"0": {"enabled": True, "factor": "inf"}}}}
    legacy = {"pv_forecast_kw": {"model_version": 2, "enabled": True, "factor": 1.3}}
    bad_buckets = {"pv_forecast_kw": {"model_version": 3, "buckets": []}}

    assert apply_forecast_calibration(values, disabled, "pv_forecast_kw") == values
    assert apply_forecast_calibration(values, enabled, "pv_forecast_kw") == [1.3, None, 2.6]
    assert apply_forecast_calibration([1.0], invalid, "pv_forecast_kw") == [1.0]
    assert apply_forecast_calibration([1.0], legacy, "pv_forecast_kw") == [1.0]
    assert apply_forecast_calibration([1.0], bad_buckets, "pv_forecast_kw") == [1.0]
    assert apply_forecast_calibration(
        [1.0],
        {"pv_forecast_kw": {"model_version": 3, "buckets": {"0": {"enabled": True, "factor": object()}}}},
        "pv_forecast_kw",
    ) == [1.0]
    assert apply_forecast_calibration(
        ["bad", -2.0],
        {"pv_forecast_kw": {"model_version": 3, "buckets": {"0": {"enabled": True, "factor": 1.2}}}},
        "pv_forecast_kw",
    ) == [None, 0.0]


def test_near_term_factor_does_not_change_day_ahead_slots() -> None:
    model = {
        "pv_forecast_kw": {
            "model_version": 3,
            "buckets": {"0": {"enabled": True, "factor": 1.2}},
        }
    }

    assert apply_forecast_calibration(
        [1.0, 1.0, 1.0],
        model,
        "pv_forecast_kw",
        interval_minutes=30,
    ) == [1.2, 1.0, 1.0]


def test_lead_buckets_train_and_apply_independent_factors() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    start = now - timedelta(minutes=10 * (MIN_CALIBRATION_SAMPLES - 1))
    snapshots: list[dict[str, object]] = []
    observations: dict[str, float] = {}
    for index in range(MIN_CALIBRATION_SAMPLES):
        valid_at = start + timedelta(minutes=10 * index)
        snapshots.append(
            {
                "forecast_training_slots": [
                    {
                        "pv_forecast_kw_issued_at": valid_at - timedelta(minutes=5),
                        "valid_at": valid_at,
                        "pv_forecast_kw": 1 / 1.2,
                    },
                    {
                        "pv_forecast_kw_issued_at": valid_at - timedelta(minutes=65),
                        "valid_at": valid_at,
                        "pv_forecast_kw": 1 / 0.8,
                    },
                ]
            }
        )
        observations[valid_at.isoformat()] = 1.0

    model, _changed = update_forecast_calibration(
        {}, snapshots, {"pv_forecast_kw": observations}, now=now
    )
    calibration = model["pv_forecast_kw"]

    assert calibration["buckets"]["0"]["factor"] == 1.2
    assert calibration["buckets"]["2"]["factor"] == 0.8
    assert apply_forecast_calibration([1.0], model, "pv_forecast_kw") == [1.2]
    assert apply_forecast_calibration([1.0], model, "pv_forecast_kw", lead_offset_minutes=65) == [0.8]


def test_forecast_calibration_rejects_malformed_evidence() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    snapshots = [
        {"forecast_training_slots": "bad"},
        {"forecast_training_slots": ["bad"]},
        {"forecast_training_slots": [{"issued_at": now, "valid_at": "bad", "pv_forecast_kw": 1.0}]},
        {
            "forecast_training_slots": [
                {"issued_at": now + timedelta(hours=1), "valid_at": now, "pv_forecast_kw": 1.0}
            ]
        },
    ]

    model, changed = update_forecast_calibration(
        {"pv_forecast_kw": "bad-shape"},
        snapshots,
        {"pv_forecast_kw": {"value": 2.0, "observed_at": now}},
        now=now,
    )

    assert changed is False
    assert model == {"pv_forecast_kw": "bad-shape"}
    assert _bounded_factor(float("nan")) == 1.0
    assert _finite_float_or_none("bad") is None
    assert _parse_datetime_or_none(datetime(2026, 6, 27, tzinfo=UTC)) == datetime(2026, 6, 27, tzinfo=UTC)
    assert _parse_datetime_or_none(123) is None
    assert _parse_datetime_or_none("bad") is None


def test_calibration_storage_and_observation_helpers_reject_bad_shapes() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)

    assert _stored_samples("bad", "pv_forecast_kw") == []
    assert _stored_samples(
        [
            "bad",
            {},
            {
                "valid_at": now.isoformat(),
                "forecast": 1.0,
                "actual": 1.0,
                "lead_bucket": "bad",
            },
        ],
        "pv_forecast_kw",
    ) == []
    assert _observations_for_field(
        [
            "bad",
            {"value": -1, "observed_at": now},
            {"value": 1, "observed_at": now + timedelta(seconds=1)},
            {"value": 1.5, "observed_at": now},
        ],
        now,
    ) == [(now, 1.5)]
    assert _nearest_observation([], now) is None
    assert _as_utc(datetime(2026, 6, 27, 12, 0)) == now
