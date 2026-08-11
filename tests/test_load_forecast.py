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
    _finite_sample_upper_quantile,
    _HistoricalDay,
    _leave_one_out_buffer,
    _parse_datetime,
    _percentile,
    _power_events,
    _profile_interval_average,
    _profile_is_forecastable,
    _profile_value,
    _quality_failures,
    _rolling_validation,
    _time_weighted_buckets,
    _timezone,
    build_load_forecast_model,
    clean_load_history_buckets,
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
    unit_transition = _power_events(
        [
            type(
                "State",
                (),
                {
                    "state": "1000",
                    "last_updated": datetime(2026, 6, 27, tzinfo=UTC),
                    "attributes": {"unit_of_measurement": "W"},
                },
            )(),
            type(
                "State",
                (),
                {
                    "state": "1",
                    "last_updated": datetime(2026, 6, 27, 0, 15, tzinfo=UTC),
                    "attributes": {"unit_of_measurement": "kW"},
                },
            )(),
        ],
        "MW",
    )
    assert [event.value for event in unit_transition] == [1.0, 1.0]
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert clean_load_history_buckets(
        [],
        load_unit="kW",
        start=now - timedelta(hours=1),
        end=now,
        timezone="UTC",
    ) == ([], None, False, False)
    assert _time_weighted_buckets(
        [],
        [],
        [],
        start=now - timedelta(hours=1),
        end=now,
        zone=ZoneInfo("UTC"),
    ) == []


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
    assert model["complete_days"] >= 3
    assert model["validation"]["origin_count"] == 2
    assert model["validation"]["sample_count"] >= 144
    assert model["validation"]["upper_coverage"] >= 0.9
    assert model["validation"]["calibration_buffer_kw"] >= 0
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


def test_three_complete_days_can_train_while_short_or_gappy_history_remains_learning() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    three_day = build_load_forecast_model(
        _history(now, days=4),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    short = build_load_forecast_model(
        _history(now, days=3),
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

    assert three_day["complete_days"] == 3
    assert three_day["status"] == "ready"
    assert three_day["validation"]["origin_count"] == 2
    assert three_day["validation"]["sample_count"] == 192
    assert three_day["validation"]["positive_residual_p90_kw"] >= 0
    assert short["complete_days"] == 2
    assert short["status"] == "learning"
    assert "insufficient_training_days" in short["quality_failures"]
    assert gappy["status"] in {"learning", "failed"}


def test_day_block_calibration_preserves_correlated_peak_evidence() -> None:
    baseline = {index: 1.0 for index in range(BUCKETS_PER_DAY)}
    peak = {**baseline, **{index: 4.0 for index in range(12)}}
    days = [
        _HistoricalDay(date(2026, 6, 1), "weekday", peak),
        _HistoricalDay(date(2026, 6, 2), "weekday", baseline),
        _HistoricalDay(date(2026, 6, 3), "weekday", baseline),
        _HistoricalDay(date(2026, 6, 4), "weekday", baseline),
    ]

    assert _percentile([3.0] * 12 + [0.0] * (BUCKETS_PER_DAY * 4 - 12), 0.90) == 0
    assert _leave_one_out_buffer(days, minimum_samples=1) == 3
    assert _finite_sample_upper_quantile([0.0, 1.0, 2.0, 3.0], 0.90) == 3


def test_conservative_coverage_gate_has_explicit_narrow_bypass() -> None:
    validation = {
        "origin_count": 2,
        "sample_count": 144,
        "mae_kw": 1.0,
        "persistence_mae_kw": 1.0,
        "upper_coverage": 0.86,
    }
    profiles = {
        "weekday": {"expected": [1.0] * BUCKETS_PER_DAY, "upper": [2.0] * BUCKETS_PER_DAY},
        "weekend": {"expected": [1.0] * BUCKETS_PER_DAY, "upper": [2.0] * BUCKETS_PER_DAY},
    }

    enforced = _quality_failures(
        training_days=3,
        history_coverage=1.0,
        validation=validation,
        profiles=profiles,
    )
    bypassed = _quality_failures(
        training_days=3,
        history_coverage=1.0,
        validation=validation,
        profiles=profiles,
        bypass_conservative_bound_gate=True,
    )

    assert "conservative_bound_coverage_below_gate" in enforced
    assert "conservative_bound_coverage_below_gate" not in bypassed


def test_bounded_negative_and_ev_gaps_still_qualify_training_days() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    load_states: list[HistoryState] = []
    ev_states: list[HistoryState] = []
    for state in _history(now):
        # Simulate historical signed-source glitches and normal EV exclusions
        # without allowing either bounded gap to invalidate the whole day.
        gap_hour = state.last_updated.day % 12
        load_value = "-1" if state.last_updated.hour == gap_hour and state.last_updated.minute == 0 else state.state
        ev_value = (
            "charging"
            if state.last_updated.hour == (gap_hour + 6) % 24 and state.last_updated.minute == 0
            else "available"
        )
        load_states.append(HistoryState(load_value, state.last_updated))
        ev_states.append(HistoryState(ev_value, state.last_updated))

    model = build_load_forecast_model(
        load_states,
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
        ev_charging_states=ev_states,
    )

    assert model["fully_observed_days"] == 0
    assert model["complete_days"] == model["fully_observed_days"]
    assert model["training_days"] >= 3
    assert model["minimum_training_day_coverage"] == 0.8
    assert model["status"] == "ready"
    assert model["quality_failures"] == []


def test_recurring_training_day_gaps_do_not_produce_an_unusable_ready_model() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    states = [
        HistoryState(
            "unavailable" if 1 <= state.last_updated.hour < 5 else state.state,
            state.last_updated,
        )
        for state in _history(now, days=7)
    ]

    model = build_load_forecast_model(
        states,
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )

    assert model["training_days"] >= 3
    assert model["history_coverage"] >= 0.8
    assert model["status"] == "failed"
    assert "forecast_profile_unavailable" in model["quality_failures"]


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


def test_profile_is_time_weighted_when_resampled_to_a_coarser_interval() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    expected = [0.0] * BUCKETS_PER_DAY
    expected[49] = 10.0
    model["profiles"] = {
        day_type: {"expected": list(expected), "upper": list(expected)}
        for day_type in ("weekday", "weekend")
    }

    forecast = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=30,
        source_entity_id="sensor.house_load",
    )

    assert forecast.expected_kw == [5.0, 0.0]
    assert forecast.upper_kw == [5.0, 0.0]


def test_partially_observed_days_do_not_satisfy_complete_day_gate() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    partial = [
        HistoryState(
            "unavailable" if state.last_updated.hour < 5 else state.state,
            state.last_updated,
        )
        for state in _history(now, days=12)
    ]

    model = build_load_forecast_model(
        partial,
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )

    assert model["history_coverage"] >= 0.79
    assert model["complete_days"] == 0
    assert model["status"] == "learning"
    assert "insufficient_training_days" in model["quality_failures"]


def test_exact_midnight_history_start_retains_first_complete_day() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    states = _history(now, days=28)

    model = build_load_forecast_model(
        states,
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )

    assert model["history_days"] == 28
    assert model["complete_days"] == 28


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


def test_profile_forecastability_rejects_malformed_and_long_runtime_gaps() -> None:
    values: list[float | None] = [1.0] * BUCKETS_PER_DAY
    values[48:51] = [None, None, None]
    profiles = {
        day_type: {"expected": list(values), "upper": list(values)}
        for day_type in ("weekday", "weekend")
    }

    assert _profile_is_forecastable(None) is False
    assert _profile_is_forecastable(profiles["weekday"]) is False
    assert _profile_interval_average(
        profiles,
        datetime(2026, 6, 27, 12, tzinfo=UTC),
        interval_minutes=15,
        zone=ZoneInfo("UTC"),
        profile_key="expected",
    ) is None


def test_unexpected_runtime_profile_failure_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.load_forecast._profile_interval_average",
        lambda *args, **kwargs: None,
    )

    forecast = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=0.25,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
    )

    assert forecast.expected_kw == [None]
    assert forecast.upper_kw == [None]
    assert forecast.status == "failed"


def test_model_age_transitions_and_source_or_shape_mismatch_fail_closed() -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    model = build_load_forecast_model(
        _history(now),
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
    )
    model["last_training_status"] = "failed"
    model["last_training_quality_failures"] = ["forecast_accuracy_below_persistence_gate"]
    model["last_training_validation"] = {"mae_kw": 2.0}
    model["last_attempt_source_entity_id"] = "sensor.house_load"

    visible = load_forecast_from_model(
        model,
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=15,
        source_entity_id="sensor.house_load",
    )
    assert visible.details["source_entity_id"] == "sensor.house_load"
    assert visible.details["last_attempt_source_entity_id"] == "sensor.house_load"
    assert visible.details["last_training_status"] == "failed"
    assert visible.details["last_training_quality_failures"] == ["forecast_accuracy_below_persistence_gate"]
    assert visible.details["last_training_validation"] == {"mae_kw": 2.0}

    assert load_forecast_model_status(model, now=now + timedelta(hours=24))[0] == "ready"
    assert load_forecast_model_status(model, now=now + timedelta(hours=25))[0] == "degraded"
    assert load_forecast_model_status(model, now=now + timedelta(hours=73))[0] == "stale"
    assert load_forecast_model_status(model, now=now - timedelta(hours=1))[0] == "failed"
    assert training_due(model, now=now + timedelta(hours=5), source_entity_id="sensor.house_load") is False
    assert training_due(model, now=now + timedelta(hours=6), source_entity_id="sensor.house_load") is True
    assert training_due(model, now=now, source_entity_id="sensor.replacement") is True
    assert training_due(
        model,
        now=now,
        source_entity_id="sensor.house_load",
        bypass_conservative_bound_gate=True,
    ) is True
    assert training_due(
        {**model, "contract_version": FORECAST_CONTRACT_VERSION - 1},
        now=now,
        source_entity_id="sensor.house_load",
    ) is True
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
    missing_bypass_contract = load_forecast_from_model(
        {key: value for key, value in model.items() if key != "safety_gates_bypassed"},
        now=now,
        timezone="UTC",
        horizon_hours=1,
        interval_minutes=5,
        source_entity_id="sensor.house_load",
    )
    assert wrong_source.status == "failed"
    assert corrupt.status == "failed"
    assert missing_bypass_contract.status == "failed"
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
    boolean_profile = {
        key: {field: list(values) for field, values in value.items()} for key, value in model["profiles"].items()
    }
    boolean_profile["weekday"]["expected"][0] = True
    variants = [
        None,
        {**model, "model_version": 0},
        {**model, "profiles": short_profile},
        {**model, "profiles": invalid_value},
        {**model, "profiles": inverted_bound},
        {**model, "profiles": boolean_profile},
        {**model, "status": "mystery"},
        {**model, "complete_days": "bad"},
        {**model, "complete_days": True},
        {**model, "history_coverage": float("nan")},
        {**model, "history_coverage": 1.1},
        {**model, "validation": {**model["validation"], "mae_kw": None}},
        {**model, "validation": {**model["validation"], "rmse_kw": float("inf")}},
        {**model, "validation": {**model["validation"], "upper_coverage": 1.1}},
        {**model, "validation": {**model["validation"], "sample_count": True}},
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
    long_gap = [1.0] * BUCKETS_PER_DAY
    long_gap[48:51] = [None, None, None]
    assert _profile_value(long_gap, now) is None
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
            for day in (1, 3, 5)
        ]
    )
    failures = _quality_failures(
        training_days=3,
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


def test_validation_uses_partial_previous_days_without_training_on_them() -> None:
    full_values = {index: 1.0 for index in range(BUCKETS_PER_DAY)}
    partial_values = {index: 1.0 for index in range(80)}
    complete = [
        _HistoricalDay(date(2026, 6, day), "weekday", dict(full_values))
        for day in (1, 3, 5)
    ]
    all_days = [
        complete[0],
        _HistoricalDay(date(2026, 6, 2), "weekday", partial_values),
        complete[1],
        _HistoricalDay(date(2026, 6, 4), "weekday", partial_values),
        complete[2],
    ]

    validation = _rolling_validation(complete, comparison_days=all_days)

    assert validation["origin_count"] == 2
    assert validation["sample_count"] == 160
    assert validation["mae_kw"] == 0
    assert validation["persistence_mae_kw"] == 0
    assert validation["upper_coverage"] == 1


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
