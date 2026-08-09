"""Tests for the deterministic built-in household load forecast."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_energy_planner.load_forecast import (
    BUCKETS_PER_DAY,
    FORECAST_CONTRACT_VERSION,
    MODEL_VERSION,
    _bool_events,
    _distance_to_profile_value,
    _HistoricalDay,
    _parse_datetime,
    _percentile,
    _power_events,
    _profile_value,
    _quality_failures,
    _rolling_validation,
    _time_weighted_buckets,
    _timezone,
    build_load_forecast_model,
    load_forecast_from_model,
    load_forecast_model_status,
    normalize_power_kw,
    training_due,
)


@dataclass(slots=True)
class HistoryState:
    """Small Recorder state double."""

    state: str
    last_updated: datetime

    @property
    def last_changed(self) -> datetime:
        return self.last_updated


def _history(
    now: datetime,
    *,
    days: int = 12,
    unit: str = "kW",
    value_fn: Any | None = None,
) -> list[HistoryState]:
    start = now - timedelta(days=days)
    values = []
    cursor = start
    while cursor <= now:
        local_hour = cursor.hour + cursor.minute / 60
        value = value_fn(cursor) if value_fn else 1.0 + 0.4 * (17 <= local_hour < 21)
        stored = value * 1000 if unit == "W" else value
        values.append(HistoryState(str(stored), cursor))
        cursor += timedelta(minutes=15)
    return values


def test_power_normalization_rejects_invalid_values_and_units() -> None:
    assert normalize_power_kw("1500", "W") == 1.5
    assert normalize_power_kw("1.5", "kW") == 1.5
    assert normalize_power_kw("0.0015", "MW") == 1.5
    assert normalize_power_kw("-1", "kW") is None
    assert normalize_power_kw("nan", "kW") is None
    assert normalize_power_kw("1", "kWh") is None
    assert normalize_power_kw("1", "") is None
    assert normalize_power_kw(object(), "kW") is None
    assert _power_events([object()], "kW") == []
    assert _bool_events([object()]) == []


def test_model_trains_ready_and_forecasts_expected_and_upper_load() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now, unit="W"),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="W",
    )

    assert model["model_version"] == MODEL_VERSION
    assert model["contract_version"] == FORECAST_CONTRACT_VERSION
    assert model["status"] == "ready"
    assert model["complete_days"] >= 7
    assert model["validation"]["origin_count"] == 2
    assert model["validation"]["sample_count"] >= 144
    assert model["validation"]["upper_coverage"] >= 0.9
    assert len(model["profiles"]["weekday"]["expected"]) == BUCKETS_PER_DAY

    forecast = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=12,
        interval_minutes=5,
        source_entity_id="sensor.house_load",
        current_load_kw=1.0,
        current_ev_charging=False,
    )

    assert forecast.status == "ready"
    assert len(forecast.expected_kw) == 144
    assert all(value is not None for value in forecast.expected_kw)
    assert all(
        expected is not None and upper is not None and upper >= expected
        for expected, upper in zip(forecast.expected_kw, forecast.upper_kw, strict=True)
    )
    assert forecast.details["source"] == "built_in_recorder_history"
    assert forecast.details["forecast_coverage"] == 1.0


def test_rolling_origin_fixture_proves_quality_contract() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "load_forecast" / "rolling_origin.json").read_text()
    )
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)

    def fixture_value(at: datetime) -> float:
        day_index = (at.date() - (now - timedelta(days=fixture["history_days"])).date()).days
        evening_peak = fixture["evening_peak_kw"] if 17 <= at.hour < 21 else 0.0
        weekend = fixture["weekend_increment_kw"] if at.weekday() >= 5 else 0.0
        alternating = fixture["alternating_day_increment_kw"] if day_index % 2 else 0.0
        return fixture["base_kw"] + evening_peak + weekend + alternating

    model = build_load_forecast_model(
        _history(now, days=fixture["history_days"], value_fn=fixture_value),
        now=now,
        timezone=fixture["timezone"],
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    expected = fixture["expected"]
    validation = model["validation"]

    assert model["status"] == expected["status"]
    assert validation["origin_count"] >= expected["minimum_holdout_origins"]
    assert validation["sample_count"] >= expected["minimum_aligned_samples"]
    assert validation["mae_kw"] <= (
        validation["persistence_mae_kw"] * expected["maximum_mae_ratio_to_persistence"]
    )
    assert validation["upper_coverage"] >= expected["minimum_upper_coverage"]


def test_history_cleaning_excludes_ev_and_subtracts_hvac_power() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    load = _power_events(
        [HistoryState("5", start), HistoryState("4", start + timedelta(minutes=15))],
        "kW",
    )
    ev = _bool_events(
        [HistoryState("charging", start), HistoryState("idle", start + timedelta(minutes=15))]
    )
    hvac = _power_events([HistoryState("1.5", start), HistoryState("1", start + timedelta(minutes=15))], "kW")

    buckets = _time_weighted_buckets(
        load,
        ev,
        hvac,
        start=start,
        end=start + timedelta(minutes=30),
        zone=ZoneInfo("UTC"),
    )

    assert buckets == [(start + timedelta(minutes=15), 3.0)]


def test_history_cleaning_drops_unknown_ev_and_hvac_intervals() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    load = _power_events([HistoryState("3", start)], "kW")
    ev = _bool_events(
        [HistoryState("unknown", start), HistoryState("off", start + timedelta(minutes=15))]
    )
    hvac = _power_events(
        [HistoryState("unavailable", start), HistoryState("1", start + timedelta(minutes=15))],
        "kW",
    )

    buckets = _time_weighted_buckets(
        load,
        ev,
        hvac,
        start=start,
        end=start + timedelta(minutes=30),
        zone=ZoneInfo("UTC"),
    )

    assert buckets == [(start + timedelta(minutes=15), 2.0)]


def test_short_or_gappy_history_remains_learning() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    short = build_load_forecast_model(
        _history(now, days=4),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    gappy_states = []
    for index, state in enumerate(_history(now)):
        if index % 8 == 0:
            gappy_states.extend(
                [
                    state,
                    HistoryState("unavailable", state.last_updated + timedelta(minutes=15)),
                ]
            )
    gappy = build_load_forecast_model(
        gappy_states,
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )

    assert short["status"] == "learning"
    assert "insufficient_complete_days" in short["quality_failures"]
    assert gappy["status"] in {"learning", "failed"}


def test_recent_correction_is_bounded_and_fades_without_reducing_upper_bound() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    forecast = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=3,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
        current_load_kw=100,
        current_ev_charging=False,
    )

    assert forecast.details["recent_correction_factor"] == 1.25
    assert forecast.expected_kw[0] == pytest.approx(1.25)
    assert forecast.expected_kw[-1] == pytest.approx(1.0)
    assert all(
        upper is not None and expected is not None and upper >= expected
        for expected, upper in zip(forecast.expected_kw, forecast.upper_kw, strict=True)
    )

    charging = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
        current_load_kw=100,
        current_ev_charging=True,
    )
    assert charging.details["recent_correction_factor"] == 1.0


def test_profile_interpolation_accepts_only_gaps_up_to_thirty_minutes() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    two_bucket_gap = {**model, "profiles": {key: dict(value) for key, value in model["profiles"].items()}}
    two_bucket_gap["profiles"]["weekend"] = {
        key: list(values) for key, values in model["profiles"]["weekend"].items()
    }
    for values in two_bucket_gap["profiles"]["weekend"].values():
        values[48:50] = [None, None]
    accepted = load_forecast_from_model(
        two_bucket_gap,
        now=now,
        timezone="UTC",
        horizon_hours=0.25,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
    )

    three_bucket_gap = {**model, "profiles": {key: dict(value) for key, value in model["profiles"].items()}}
    three_bucket_gap["profiles"]["weekend"] = {
        key: list(values) for key, values in model["profiles"]["weekend"].items()
    }
    for values in three_bucket_gap["profiles"]["weekend"].values():
        values[48:51] = [None, None, None]
    rejected = load_forecast_from_model(
        three_bucket_gap,
        now=now,
        timezone="UTC",
        horizon_hours=0.25,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
    )

    assert accepted.expected_kw[0] is not None
    assert rejected.expected_kw == [None]
    assert rejected.status == "failed"


def test_model_age_transitions_and_source_or_shape_mismatch_fail_closed() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )

    assert load_forecast_model_status(model, now=now + timedelta(hours=24))[0] == "ready"
    assert load_forecast_model_status(model, now=now + timedelta(hours=25))[0] == "degraded"
    assert load_forecast_model_status(model, now=now + timedelta(hours=73))[0] == "stale"
    assert load_forecast_model_status(model, now=now - timedelta(hours=1))[0] == "failed"
    assert training_due(model, now=now + timedelta(hours=5), source_entity_id="sensor.house_load") is False
    assert training_due(model, now=now + timedelta(hours=6), source_entity_id="sensor.house_load") is True
    assert training_due(model, now=now, source_entity_id="sensor.replacement") is True
    assert training_due(None, now=now, source_entity_id="sensor.house_load") is True
    assert load_forecast_model_status({}, now=now) == ("failed", 0.0)
    attempted = {
        **model,
        "last_attempt_at": (now + timedelta(hours=5)).isoformat(),
        "last_attempt_source_entity_id": "sensor.house_load",
    }
    assert training_due(attempted, now=now + timedelta(hours=6), source_entity_id="sensor.house_load") is False
    long_learning = {
        **model,
        "status": "learning",
        "quality_ready": False,
        "unusable_since": (now - timedelta(hours=73)).isoformat(),
    }
    assert load_forecast_model_status(long_learning, now=now)[0] == "failed"

    wrong_source = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=5,
        source_entity_id="sensor.replacement",
    )
    corrupt = load_forecast_from_model(
        {**model, "profiles": {}},
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=5,
        source_entity_id="sensor.house_load",
    )
    assert wrong_source.status == "failed"
    assert corrupt.status == "failed"
    assert wrong_source.expected_kw == [None] * 12
    corrupt_quality = load_forecast_from_model(
        {**model, "validation": {**model["validation"], "upper_coverage": 0.2}},
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=5,
        source_entity_id="sensor.house_load",
    )
    wrong_timezone = load_forecast_from_model(
        model,
        now=now,
        timezone="Australia/Melbourne",
        horizon_hours=1,
        interval_minutes=5,
        source_entity_id="sensor.house_load",
    )
    assert corrupt_quality.status == "failed"
    assert wrong_timezone.status == "failed"
    assert training_due(
        model,
        now=now,
        source_entity_id="sensor.house_load",
        timezone="Australia/Melbourne",
    )


def test_corrupt_model_variants_and_small_helpers_fail_closed() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    short_profile = {key: dict(value) for key, value in model["profiles"].items()}
    short_profile["weekday"] = {"expected": [1.0], "upper": [1.0]}
    invalid_value = {
        key: {field: list(values) for field, values in value.items()}
        for key, value in model["profiles"].items()
    }
    invalid_value["weekday"]["expected"][0] = "bad"
    inverted_bound = {
        key: {field: list(values) for field, values in value.items()} for key, value in model["profiles"].items()
    }
    inverted_bound["weekday"]["upper"][0] = 0.0
    variants = [
        None,
        {**model, "model_version": 0},
        {**model, "profiles": short_profile},
        {**model, "profiles": invalid_value},
        {**model, "profiles": inverted_bound},
        {**model, "status": "mystery"},
        {**model, "complete_days": "bad"},
    ]

    for variant in variants:
        result = load_forecast_from_model(
            variant,
            now=now,
            timezone="UTC",
            horizon_hours=1,
            interval_minutes=15,
            source_entity_id="sensor.house_load",
        )
        assert result.status == "failed"

    without_attempt = {
        key: value for key, value in model.items() if not key.startswith("last_attempt")
    }
    assert training_due(without_attempt, now=now + timedelta(hours=5), source_entity_id="sensor.house_load") is False
    assert training_due(without_attempt, now=now + timedelta(hours=6), source_entity_id="sensor.house_load") is True
    assert _profile_value("bad", now) is None
    assert _distance_to_profile_value([None] * BUCKETS_PER_DAY, 0, 1) == BUCKETS_PER_DAY
    assert str(_timezone("Not/A_Timezone")) == "UTC"
    assert _parse_datetime(now) == now
    assert _parse_datetime("not-a-date") is None
    assert _percentile([], 0.9) == 0.0


def test_validation_skips_unaligned_origins_and_reports_quality_failures() -> None:
    days = [
        _HistoricalDay(date(2026, 6, day), "weekday", {1: 1.0})
        for day in (1, 2, 3, 4)
    ]
    days[2] = _HistoricalDay(date(2026, 6, 3), "weekday", {0: 1.0})

    validation = _rolling_validation(days)
    gapped_origin = _rolling_validation(
        [
            _HistoricalDay(date(2026, 6, day), "weekday", {0: 1.0})
            for day in (1, 2, 3, 5)
        ]
    )
    failures = _quality_failures(
        complete_days=7,
        history_coverage=1.0,
        validation={
            "origin_count": 2,
            "sample_count": 144,
            "mae_kw": 2.0,
            "persistence_mae_kw": 1.0,
            "upper_coverage": 0.5,
        },
        profiles={"weekday": {"expected": [1.0]}, "weekend": {"expected": [1.0]}},
    )

    assert validation["sample_count"] == 0
    assert gapped_origin["origin_count"] == 0
    assert "forecast_accuracy_below_persistence_gate" in failures
    assert "conservative_bound_coverage_below_gate" in failures


def test_zero_expected_profile_disables_recent_correction() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    zero = [0.0] * BUCKETS_PER_DAY
    zero_model = {
        **model,
        "profiles": {
            "weekday": {"expected": zero, "upper": zero},
            "weekend": {"expected": zero, "upper": zero},
        },
    }

    forecast = load_forecast_from_model(
        zero_model,
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
        current_load_kw=2.0,
    )

    assert forecast.details["recent_correction_factor"] == 1.0


def test_dst_transition_day_is_excluded_but_timezone_forecast_is_continuous() -> None:
    now = datetime(2026, 4, 12, 12, tzinfo=UTC)
    melbourne = ZoneInfo("Australia/Melbourne")

    def local_pattern(at: datetime) -> float:
        local = at.astimezone(melbourne)
        return 1.0 + 0.4 * (17 <= local.hour + local.minute / 60 < 21)

    model = build_load_forecast_model(
        _history(now, days=16, value_fn=local_pattern),
        now=now,
        timezone="Australia/Melbourne",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    forecast = load_forecast_from_model(
        model,
        now=now,
        timezone="Australia/Melbourne",
        horizon_hours=24,
        interval_minutes=5,
        source_entity_id="sensor.house_load",
    )

    assert model["history_days"] < 15
    assert forecast.status == "ready"
    assert len(forecast.expected_kw) == 288
    assert all(value is not None for value in forecast.expected_kw)
