"""Tests for forecast normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.forecasts import (
    _energy_items_as_average_power,
    _energy_to_kwh,
    _items_from_value,
    _parse_datetime_or_none,
    constant_forecast,
    forecast_series_from_state,
    latest_forecast_valid_at_from_state,
    normalize_scalar_value,
)


@dataclass(slots=True)
class FakeState:
    """Minimal state with attributes."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def test_forecast_series_parses_timestamp_keyed_watts_map() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "watts": {
                "2026-06-27T00:00:00+00:00": 500,
                "2026-06-27T00:15:00+00:00": 1500,
            }
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("pv_forecast_kw", "pv_estimate", "estimate", "power", "watts", "value"),
        value_kind="power",
    )

    assert series == [0.5, 1.5, 1.5, 1.5]


def test_forecast_series_parses_nested_prediction_items() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "forecast": {
                "predictions": [
                    {
                        "start_time": "2026-06-27T00:00:00+00:00",
                        "values": {"load_kw": 1.2},
                    },
                    {
                        "start_time": "2026-06-27T00:30:00+00:00",
                        "values": {"load_kw": 2.4},
                    },
                ]
            }
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("baseline_load_forecast_kw", "load_kw", "load", "power", "watts", "value"),
        value_kind="power",
    )

    assert series == [1.2, 1.2, 2.4, 2.4]


def test_forecast_series_uses_state_unit_for_timestamp_maps() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "unit_of_measurement": "W",
            "data": {
                "2026-06-27T00:00:00+00:00": 1000,
                "2026-06-27T00:15:00+00:00": 2000,
            },
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("baseline_load_forecast_kw", "load_kw", "load", "power", "watts", "value"),
        value_kind="power",
    )

    assert series == [1.0, 2.0, 2.0, 2.0]


def test_forecast_series_converts_megawatt_power_units_to_kw() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "unit_of_measurement": "MW",
            "forecast": [
                {"period_start": "2026-06-27T00:00:00+00:00", "power": 0.001},
                {"period_start": "2026-06-27T00:15:00+00:00", "power": 0.002},
            ],
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("baseline_load_forecast_kw", "load_kw", "load", "power", "watts", "value"),
        value_kind="power",
    )

    assert series == [1.0, 2.0, 2.0, 2.0]


def test_forecast_series_converts_solcast_energy_buckets_to_average_kw() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "3.0",
        {
            "unit_of_measurement": "kWh",
            "detailedForecast": [
                {"period_start": "2026-06-27T00:00:00+00:00", "pv_estimate": 0.5},
                {"period_start": "2026-06-27T00:30:00+00:00", "pv_estimate": 1.0},
            ],
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("pv_forecast_kw", "pv_estimate", "estimate", "power", "watts", "value"),
        value_kind="power",
    )

    assert series == [1.0, 1.0, 2.0, 2.0]


def test_latest_forecast_valid_at_reads_solcast_detailed_forecast() -> None:
    state = FakeState(
        "3.0",
        {
            "unit_of_measurement": "kWh",
            "detailedForecast": [
                {"period_start": "2026-06-27T00:00:00+10:00", "pv_estimate": 0.5},
                {"period_start": "2026-06-27T23:30:00+10:00", "pv_estimate": 0.0},
            ],
        },
    )

    assert latest_forecast_valid_at_from_state(
        state,
        value_keys=("pv_forecast_kw", "pv_estimate", "estimate", "power", "watts", "value"),
    ) == datetime(2026, 6, 27, 13, 30, tzinfo=UTC)


def test_forecast_series_converts_solcast_wh_energy_buckets_to_average_kw() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "unit_of_measurement": "Wh",
            "detailedForecast": [
                {"period_start": "2026-06-27T00:00:00+00:00", "pv_estimate": 500},
                {"period_start": "2026-06-27T00:30:00+00:00", "pv_estimate": 1000},
            ],
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("pv_forecast_kw", "pv_estimate", "estimate", "power", "watts", "value"),
        value_kind="power",
    )

    assert series == [1.0, 1.0, 2.0, 2.0]


def test_forecast_series_converts_cent_price_units_to_dollars() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "unit_of_measurement": "c/kWh",
            "forecast": [
                {"period_start": "2026-06-27T00:00:00+00:00", "price": 12},
                {"period_start": "2026-06-27T00:15:00+00:00", "price": 34},
            ],
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("price", "value"),
        value_kind="price",
    )

    assert series == [0.12, 0.34, 0.34, 0.34]


def test_forecast_series_parses_camel_case_live_export_keys() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "unitOfMeasurement": "c/kWh",
            "detailedForecast": {
                "data": [
                    {"periodStart": "2026-06-27T00:00:00+00:00", "perKwh": 12},
                    {"periodStart": "2026-06-27T00:30:00+00:00", "perKwh": 34},
                ]
            },
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("import_price", "general_price", "per_kwh", "price", "value"),
        value_kind="price",
    )

    assert series == [0.12, 0.12, 0.34, 0.34]


def test_forecast_series_parses_weather_temperature_and_converts_fahrenheit() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "sunny",
        {
            "temperature_unit": "°F",
            "forecast": [
                {"datetime": "2026-06-27T00:00:00+00:00", "temperature": 68},
                {"datetime": "2026-06-27T00:30:00+00:00", "temperature": 77},
            ],
        },
    )

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("outdoor_temperature_forecast_c", "temperature", "native_temperature", "temp", "value"),
        value_kind="temperature",
    )

    assert series == [20.0, 20.0, 25.0, 25.0]


def test_forecast_series_rejects_non_finite_values() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState("0", {"forecast": [1.0, "nan", "inf", "-inf", 2.0]})

    series = forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("price", "value"),
        value_kind="price",
    )

    assert series == [1.0, 2.0]


def test_constant_forecast_builds_interval_points() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)

    points = constant_forecast(
        issued_at=issued_at,
        source="sensor.point",
        value=1.5,
        unit="kW",
        horizon_hours=1,
        interval_minutes=30,
        confidence=0.8,
    )

    assert [point.valid_at for point in points] == [issued_at, issued_at + timedelta(minutes=30)]
    assert {point.fresh_until for point in points} == {issued_at + timedelta(minutes=30)}
    assert points[0].source == "sensor.point"
    assert points[0].confidence == 0.8


def test_forecast_series_returns_none_for_missing_or_unusable_items() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)

    assert (
        forecast_series_from_state(
            FakeState("0", {}),
            issued_at=issued_at,
            horizon_hours=1,
            interval_minutes=15,
            value_keys=("value",),
            value_kind="price",
        )
        is None
    )
    assert (
        forecast_series_from_state(
            FakeState("0", {"forecast": [{"time": 1, "other": 2}, object(), {"value": "bad"}, {"value": "nan"}]}),
            issued_at=issued_at,
            horizon_hours=1,
            interval_minutes=15,
            value_keys=("value",),
            value_kind="price",
        )
        is None
    )


def test_forecast_series_treats_bad_times_as_ordered_values() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "forecast": [
                {"time": "not-a-date", "value": 1.0},
            ]
        },
    )

    assert forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("value",),
        value_kind="price",
    ) == [1.0]


def test_forecast_helpers_cover_nested_and_rejected_shapes() -> None:
    assert _items_from_value(None, ("value",)) == []
    assert _items_from_value("bad", ("value",)) == []
    assert _items_from_value({"data": {"values": [{"value": 1}]}}, ("value",)) == [{"value": 1}]
    assert _items_from_value({"2026-06-27T00:00:00+00:00": {"value": 1}}, ("value",)) == [
        {"value": 1, "valid_at": "2026-06-27T00:00:00+00:00"}
    ]
    assert _parse_datetime_or_none(datetime(2026, 6, 27, tzinfo=UTC)) == datetime(2026, 6, 27, tzinfo=UTC)
    assert _parse_datetime_or_none(123) is None
    assert _parse_datetime_or_none("bad") is None


def test_forecast_energy_conversion_keeps_unconvertible_buckets() -> None:
    converted = _energy_items_as_average_power(
        [
            1.0,
            {"period_start": "2026-06-27T00:00:00+00:00", "energy": 1.0, "unit": "kWh"},
            {"period_start": "2026-06-27T00:00:00+00:00", "energy": 1.0, "unit": "kWh"},
            {"period_start": "2026-06-27T01:00:00+00:00", "unit": "kWh"},
            {"period_start": "2026-06-27T02:00:00+00:00", "energy": "bad", "unit": "kWh"},
            {"period_start": "2026-06-27T03:00:00+00:00", "energy": "nan", "unit": "kWh"},
            {"period_start": "2026-06-27T04:00:00+00:00", "energy": 500, "unit": "Wh"},
            {"period_start": "2026-06-27T05:00:00+00:00", "energy": 0.001, "unit": "MWh"},
        ],
        ("energy",),
        "kWh",
    )

    assert converted[0] == 1.0
    assert converted[1]["energy"] == 1.0
    assert converted[3] == {"period_start": "2026-06-27T01:00:00+00:00", "unit": "kWh"}
    assert converted[4]["energy"] == "bad"
    assert converted[5]["energy"] == "nan"
    assert converted[6]["energy"] == 0.5
    assert converted[7]["energy"] == 1.0
    assert _energy_to_kwh(2, "MWh") == 2000


def test_scalar_normalization_passthroughs_and_energy_units() -> None:
    assert normalize_scalar_value(68, value_kind="temperature", unit="F") == 20.0
    assert normalize_scalar_value(25, value_kind="temperature", unit="C") == 25
    assert normalize_scalar_value(12, value_kind="price", unit="c/kWh") == 0.12
    assert normalize_scalar_value(2, value_kind="power", value_key="energy", unit="kWh") == 2
    assert normalize_scalar_value(7, value_kind="other") == 7
