"""Tests for forecast normalization helpers."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_energy_planner import forecasts as forecasts_module
from custom_components.ha_energy_planner.forecasts import (
    MAX_FORECAST_SOURCE_ROWS,
    _energy_items_as_average_power,
    _energy_to_kwh,
    _items_from_value,
    _parse_datetime_or_none,
    cacheable_tariff_snapshot,
    capture_tariff_source_attributes,
    constant_forecast,
    forecast_coverage_details,
    forecast_coverage_ratio,
    forecast_series_from_state,
    forecast_timestamp_status_from_state,
    latest_forecast_valid_at_from_state,
    normalize_scalar_value,
    tariff_snapshot_matches,
)


@dataclass(slots=True)
class FakeState:
    """Minimal state with attributes."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def test_tariff_snapshot_detects_changed_offset_and_container_size():
    first = datetime(2026, 4, 5, 2, 30, tzinfo=ZoneInfo("Australia/Melbourne"))
    rows = [first, (0.25,)]
    rows.append(rows)
    attributes = {"forecast": rows}
    snapshot = deepcopy(attributes)

    assert tariff_snapshot_matches(attributes, snapshot)

    rows[0] = first.replace(fold=1)
    assert rows[0] == first
    assert not tariff_snapshot_matches(attributes, snapshot)

    rows[0] = first
    rows.pop()
    assert not tariff_snapshot_matches(attributes, snapshot)


def test_tariff_snapshot_treats_uncomparable_provider_values_as_changed():
    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    invalid = {"import_price": {now: {"diagnostic": "first", "price": Decimal("sNaN")}}}
    recovered = {"import_price": {now: {"diagnostic": "second", "price": Decimal("0.25")}}}

    # HA can detect the attribute change from provider metadata that Planner ignores.
    assert recovered != invalid
    assert not tariff_snapshot_matches(recovered, invalid)
    assert not tariff_snapshot_matches(invalid, recovered)


def test_tariff_snapshot_treats_invalid_provider_offset_as_changed():
    class BrokenOffset(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta:
            raise ValueError("Invalid provider UTC offset")

    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    invalid = {"forecast": [{"diagnostic": "old", "start_time": now.replace(tzinfo=BrokenOffset())}]}
    recovered = {"forecast": [{"diagnostic": "new", "start_time": now}]}

    assert invalid != recovered
    assert not tariff_snapshot_matches(recovered, invalid)
    assert not tariff_snapshot_matches(invalid, recovered)


def test_tariff_capture_preserves_nested_time_maps_and_price_aliases():
    class DebugMetadata(NamedTuple):
        value: str

    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    leaf = {"perKwh": 0.25, "startTime": now, "debug": DebugMetadata("unused")}
    time_map = {now.isoformat(): {"value": leaf, "values": ("invalid alternate payload",), "debug": ("nested",)}}
    attributes = time_map
    for _ in range(5):
        attributes = {"forecast": attributes}

    def price_series(payload):
        return forecast_series_from_state(
            FakeState("0.25", payload), issued_at=now, horizon_hours=1, interval_minutes=5,
            value_keys=("per_kwh",), value_kind="price",
        )

    snapshot = capture_tariff_source_attributes(attributes)
    assert tariff_snapshot_matches(attributes, snapshot)
    series = price_series(snapshot)
    assert series[0] == 0.25
    leaf["debug"] = ("changed",)
    assert tariff_snapshot_matches(attributes, snapshot)
    leaf["perKwh"] = 0.50
    assert not tariff_snapshot_matches(attributes, snapshot)
    leaf["perKwh"] = {"value": 0.50}
    invalid_snapshot = capture_tariff_source_attributes(attributes)
    assert price_series(attributes) is None
    assert price_series(invalid_snapshot) is None
    assert tariff_snapshot_matches(attributes, invalid_snapshot)


def test_tariff_cache_preserves_timestamp_keys_without_copying_unrelated_row_metadata():
    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    row = {"Export Price": -0.25, "debug": MappingProxyType({"first": True})}
    attributes = {"forecast": {now: row}}

    snapshot = cacheable_tariff_snapshot(attributes)

    assert snapshot == {"forecast": {now: {"Export Price": -0.25}}}
    row["debug"] = MappingProxyType({"second": True})
    assert tariff_snapshot_matches(attributes, snapshot, acyclic_snapshot=True)
    row["Export Price"] = 0.50
    assert not tariff_snapshot_matches(attributes, snapshot, acyclic_snapshot=True)


@pytest.mark.parametrize("container", [list, tuple])
def test_tariff_cache_skips_oversized_sequences(container):
    assert cacheable_tariff_snapshot({"forecast": container([0] * MAX_FORECAST_SOURCE_ROWS)}) is not None
    assert cacheable_tariff_snapshot({"forecast": container([0] * (MAX_FORECAST_SOURCE_ROWS + 1))}) is None


def test_tariff_cache_bounds_nested_fields_and_scalar_payloads():
    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    too_many_keys = {
        (now + timedelta(minutes=index)).isoformat(): 0
        for index in range(MAX_FORECAST_SOURCE_ROWS + 1)
    }
    assert cacheable_tariff_snapshot({"forecast": too_many_keys}) is None

    individually_bounded = [[0] * MAX_FORECAST_SOURCE_ROWS] * 16
    assert cacheable_tariff_snapshot({"forecast": individually_bounded}) is None
    assert cacheable_tariff_snapshot({"forecast": [{"price": "0" * 1_000}]}) is None
    assert cacheable_tariff_snapshot({"forecast": [{"price": 10**400}]}) is None


@pytest.mark.parametrize("timestamped", [False, True])
def test_oversized_forecast_price_keeps_its_gap(timestamped):
    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    rows = [10**400, 0.25]
    state = FakeState("0.15", {"forecast": [
        {"valid_at": now + timedelta(minutes=index * 5), "price": price}
        for index, price in enumerate(rows)
    ] if timestamped else rows})

    series = forecast_series_from_state(
        state, issued_at=now, horizon_hours=1, interval_minutes=5, value_keys=("price",), value_kind="price",
    )

    assert series is not None
    assert series[:2] == [None, 0.25]


@pytest.mark.parametrize("middle", ["invalid", "absent", "valid_duplicate"])
@pytest.mark.parametrize("explicit_cadence", [False, True])
def test_inferred_tariff_boundaries_and_series_preserve_reported_price_gaps(middle, explicit_cadence):
    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    half_hour = timedelta(minutes=30)
    middle_at = now + half_hour
    following_at = middle_at + half_hour
    rows = [
        {"valid_at": now, "price": 0.25},
        {"valid_at": following_at, "price": 0.50},
    ]
    if middle != "absent":
        if middle == "valid_duplicate":
            rows.insert(1, {"valid_at": middle_at, "price": 0.30})
        rows.insert(-1, {"valid_at": middle_at, "price": 10**400})
    attributes = {"forecast": rows}
    if explicit_cadence:
        attributes["forecast_interval_minutes"] = 60
    state = FakeState("0.15", attributes)

    series = forecast_series_from_state(
        state, issued_at=now, horizon_hours=2, interval_minutes=30, value_keys=("price",), value_kind="price",
    )
    boundaries = forecasts_module.tariff_source_intervals(state, 30)

    assert series is not None
    assert series == {
        "invalid": [0.25, None, 0.50, None],
        "absent": [0.25, 0.25, 0.50, 0.50],
        "valid_duplicate": [0.25, 0.30, 0.50, None],
    }[middle]
    assert boundaries[0]["end"] == (following_at if middle == "absent" else middle_at).isoformat()
    assert [row["price"] for row in boundaries] == (
        [0.25, 0.30, 0.50] if middle == "valid_duplicate" else [0.25, 0.50]
    )


@pytest.mark.parametrize("kind", ["value", "cadence", "energy", "naive"])
def test_oversized_forecasts_preserve_full_source_semantics(kind) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    origin = now - timedelta(minutes=30 * (MAX_FORECAST_SOURCE_ROWS + 1))
    rows = [
        {"valid_at": (origin + timedelta(minutes=30 * i)).isoformat(), "value": 1.0}
        for i in range(MAX_FORECAST_SOURCE_ROWS + 4)
    ]
    if kind == "value":
        rows.append({"valid_at": (now + timedelta(minutes=4)).isoformat(), "value": "unavailable"})
    elif kind == "cadence":
        rows.append({"valid_at": (origin + timedelta(minutes=5)).isoformat(), "value": 1.0})
    elif kind == "energy":
        rows.append({"valid_at": (now + timedelta(minutes=4)).isoformat(), "value": "unavailable"})
    else:
        for row in rows:
            row["valid_at"] = row["valid_at"].replace("+00:00", "")
    state = FakeState("0", {"forecast": list(reversed(rows)),
                            "unit_of_measurement": "kWh" if kind == "energy" else "kW"})
    kwargs = dict(issued_at=now + timedelta(minutes=5), horizon_hours=1, interval_minutes=5,
                  value_keys=("value",), value_kind="power")
    actual = forecast_series_from_state(state, **kwargs)
    if kind == "cadence":
        assert actual == [None] * 5 + [1.0] + [None] * 5 + [1.0]
    elif kind == "energy":
        # The invalid value still ends the preceding four-minute energy bucket.
        assert actual == [15.0] * 5 + [2.0] * 7
    else:
        assert actual == [1.0] * 12


def test_dense_forecast_does_not_truncate_values_inside_requested_horizon() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    state = FakeState("0", {"forecast": [
        {"valid_at": (now + timedelta(seconds=i)).isoformat(), "value": float(i)}
        for i in range(3601)
    ]})
    assert forecast_series_from_state(state, issued_at=now, horizon_hours=1, interval_minutes=5,
        value_keys=("value",), value_kind="power") == [float(i * 300) for i in range(12)]


@pytest.mark.parametrize("invalid", ["start", "overlap", "non_dict", "naive", "naive_priority"])
def test_large_declared_tariffs_validate_all_source_evidence(invalid) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    rows = [
        {"start_time": (now + timedelta(minutes=30 * i)).isoformat(),
         "end_time": (now + timedelta(minutes=30 * (i + 1))).isoformat(),
         "duration": 30, "per_kwh": 0.3}
        for i in range(-MAX_FORECAST_SOURCE_ROWS, 3)
    ]
    if invalid == "start":
        rows.append({**rows[0], "start_time": "broken"})
    elif invalid == "overlap":
        rows.insert(0, dict(rows[0]))
    elif invalid == "naive":
        rows.append({**rows[0], "start_time": rows[0]["start_time"].replace("+00:00", "")})
    elif invalid == "naive_priority":
        rows[-1]["valid_at"] = rows[-1]["start_time"].replace("+00:00", "")
    else:
        rows.append(0.1)
    state = FakeState("0", {"detailedForecast": rows})
    details = {}
    assert forecasts_module.tariff_source_intervals(
        state, 5, details=details, slot_starts=[now, now + timedelta(hours=1)]
    ) == []
    assert details["source_validity"] == "invalid_intervals"
    assert forecast_series_from_state(state, issued_at=now, horizon_hours=1, interval_minutes=5,
        value_keys=("per_kwh",), value_kind="price") == [None] * 12


@pytest.mark.parametrize("declared", [False, True])
def test_published_tariff_window_keeps_current_rows_after_validation(declared) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    rows = [
        {"start_time": (now + timedelta(minutes=30 * i)).isoformat(),
         "end_time": (now + timedelta(minutes=30 * (i + 1))).isoformat(),
         "duration": 30, "per_kwh" if declared else "price": 0.3}
        for i in range(-MAX_FORECAST_SOURCE_ROWS, 3)
    ]
    state = FakeState("0", {"detailedForecast": list(reversed(rows))})
    result = forecasts_module.tariff_source_intervals(
        state, 5, slot_starts=[now, now + timedelta(hours=1)]
    )
    assert len(result) == 3
    assert result[0]["start"] == now.isoformat()
    assert result[-1]["start"] == (now + timedelta(hours=1)).isoformat()
    assert forecasts_module.tariff_source_intervals(state, 5, slot_starts=[]) != []


@pytest.mark.parametrize("container", ["rows", "time_map"])
def test_shared_tariff_rows_preserve_boundary_and_price_precedence(container):
    from custom_components.ha_energy_planner.forecasts import (
        TariffForecast,
        forecast_series_from_source,
        tariff_intervals_from_source,
    )

    now = datetime(2026, 6, 27, tzinfo=UTC)
    if container == "rows":
        forecasts = [{"start_time": now.isoformat(), "import_price": 45, "export_price": -20, "per_kwh": 10}]
        boundary_price, export_price = 45, -0.2
    else:
        forecasts = {now.isoformat(): 45}
        boundary_price, export_price = 45, 0.45
    tariff = TariffForecast({"forecasts": forecasts, "unit": "c/kWh"})
    intervals = tariff_intervals_from_source(tariff.boundary_source(15))
    series = forecast_series_from_source(
        tariff.source(15, ("export_price", "feed_in_price", "per_kwh", "price", "value")),
        issued_at=now, horizon_hours=1, interval_minutes=15, slot_starts=[now],
    )
    assert intervals[0]["price"] == boundary_price
    assert series == [export_price]


def test_inferred_tariff_window_caps_unique_boundaries():
    start = datetime(2026, 6, 27, 8, 1, tzinfo=UTC)
    starts = [start + timedelta(minutes=7 * index) for index in range(3)]
    rows = [{"start_time": at.isoformat(), "price": 0.2} for at in starts]
    state = FakeState("0.2", {"forecast": [rows[0]] * 576 + rows[1:]})

    intervals = forecasts_module.tariff_source_intervals(state, 5, slot_starts=starts)

    assert [row["start"] for row in intervals] == [at.isoformat() for at in starts]


def test_tariff_intervals_select_current_window_after_large_history_prefix() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    origin = now - timedelta(minutes=15 * (MAX_FORECAST_SOURCE_ROWS + 10))
    rows = [
        {"start_time": (origin + timedelta(minutes=15 * index)).isoformat(), "price": index / 100}
        for index in range(MAX_FORECAST_SOURCE_ROWS + 20)
    ]
    state = FakeState("0", {"forecast": rows, "forecast_interval_minutes": 15})
    intervals = forecasts_module.tariff_source_intervals(
        state, 15, slot_starts=[now, now + timedelta(hours=1)]
    )
    assert any(
        datetime.fromisoformat(interval["start"]) <= now < datetime.fromisoformat(interval["end"])
        for interval in intervals
    )


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

    assert series == [0.5, 1.5, None, None]


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

    assert series == [1.0, 2.0, None, None]


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

    assert series == [1.0, 2.0, None, None]


def test_forecast_series_preserves_solcast_power_under_daily_energy_sensor() -> None:
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

    assert series == [0.5, 0.5, 1.0, 1.0]


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


def test_latest_forecast_valid_at_skips_values_without_timestamps_and_normalizes_naive_time() -> None:
    state = FakeState(
        "0",
        {
            "forecast": [
                1.0,
                {"pv_estimate": 2.0},
                {"period_start": "2026-06-27T10:00:00", "pv_estimate": 3.0},
            ]
        },
    )

    assert latest_forecast_valid_at_from_state(
        state,
        value_keys=("pv_forecast_kw", "pv_estimate", "estimate", "power", "watts", "value"),
    ) == datetime(2026, 6, 27, 10, 0, tzinfo=UTC)


def test_forecast_timestamp_status_distinguishes_aware_naive_and_ordered_payloads() -> None:
    keys = ("power", "value")

    assert (
        forecast_timestamp_status_from_state(
            FakeState("0", {"forecast": [{"period_start": "2026-06-27T00:00:00+10:00", "power": 1.0}]}),
            value_keys=keys,
        )
        == "aware_timestamps"
    )
    assert (
        forecast_timestamp_status_from_state(
            FakeState("0", {"forecast": [{"period_start": "2026-06-27T00:00:00", "power": 1.0}]}),
            value_keys=keys,
        )
        == "naive_timestamps"
    )
    assert (
        forecast_timestamp_status_from_state(
            FakeState("0", {"forecast": [1.0, 2.0]}),
            value_keys=keys,
        )
        == "untimestamped"
    )
    assert (
        forecast_timestamp_status_from_state(
            FakeState("0", {"forecast": [{"period_start": "bad", "power": 1.0}]}),
            value_keys=keys,
        )
        == "untimestamped"
    )


def test_forecast_series_honours_explicit_interval_energy_units() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "unit_of_measurement": "Wh",
            "detailedForecast": [
                {"period_start": "2026-06-27T00:00:00+00:00", "pv_estimate": 500, "unit": "Wh"},
                {"period_start": "2026-06-27T00:30:00+00:00", "pv_estimate": 1000, "units": "Wh"},
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

    assert series == [0.12, 0.34, None, None]


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

    assert series == [1.0, None, None, None]
    assert forecast_coverage_ratio(series) == 0.25
    assert forecast_coverage_ratio(None) == 0.0


def test_forecast_coverage_details_classifies_continuous_coverage() -> None:
    starts_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)

    assert (
        forecast_coverage_details([1.0] * 24, starts_at=starts_at, interval_minutes=30)["classification"] == "healthy"
    )
    degraded = forecast_coverage_details(
        [1.0] * 16 + [None] * 8,
        starts_at=starts_at,
        interval_minutes=30,
    )
    assert degraded["classification"] == "degraded"
    assert degraded["covered_hours"] == 8
    assert degraded["continuous_hours"] == 8
    assert degraded["first_timestamp"] == "2026-06-27T00:00:00+00:00"
    assert degraded["last_timestamp"] == "2026-06-27T07:30:00+00:00"
    assert degraded["leading_missing_slots"] == 0
    assert degraded["trailing_missing_slots"] == 8


@pytest.mark.parametrize('covered_minutes,classification', [(480, 'degraded'), (720, 'healthy')])
@pytest.mark.parametrize('short_by_seconds', [0, 1])
@pytest.mark.parametrize('split_seconds', [0, 1])
def test_forecast_coverage_thresholds_use_exact_elapsed_duration(
    covered_minutes, classification, short_by_seconds, split_seconds,
):
    start = datetime(2026, 10, 19, tzinfo=UTC)
    starts = [start + timedelta(minutes=m) for m in range(0, 24 * 60, 5)]
    starts = sorted(set(starts) | {at + timedelta(seconds=split_seconds) for at in starts})
    count = sum(at < start + timedelta(minutes=covered_minutes) for at in starts)
    starts[count] -= timedelta(seconds=short_by_seconds)
    details = forecast_coverage_details([1] * count + [None] * (len(starts) - count),
        starts_at=start, interval_minutes=5, slot_starts=starts, horizon_end=start + timedelta(hours=24))
    expected = classification if not short_by_seconds else 'unsafe' if covered_minutes == 480 else 'degraded'
    assert details['classification'] == expected
    assert details['continuous_hours'] == round((covered_minutes * 60 - short_by_seconds) / 3600, 4)


def test_forecast_coverage_details_fails_closed_on_leading_and_internal_gaps() -> None:
    starts_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    leading = forecast_coverage_details(
        [None] + [1.0] * 24,
        starts_at=starts_at,
        interval_minutes=30,
    )
    internal = forecast_coverage_details(
        [1.0] * 15 + [None] + [1.0] * 24,
        starts_at=starts_at,
        interval_minutes=30,
    )

    assert leading["classification"] == "unsafe"
    assert leading["continuous_hours"] == 0
    assert leading["longest_continuous_hours"] == 12
    assert internal["classification"] == "unsafe"
    assert internal["continuous_hours"] == 7.5
    assert internal["internal_missing_slots"] == 1


def test_forecast_coverage_details_caps_thresholds_for_short_configured_horizon() -> None:
    starts_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    complete = forecast_coverage_details([1.0] * 4, starts_at=starts_at, interval_minutes=15)
    partial = forecast_coverage_details([1.0, 1.0, None, None], starts_at=starts_at, interval_minutes=15)

    assert complete["classification"] == "healthy"
    assert complete["healthy_threshold_hours"] == 1
    assert partial["classification"] == "unsafe"
    assert partial['coverage_ratio'] == .5
    assert forecast_coverage_details(None, starts_at=starts_at, interval_minutes=15)['coverage_ratio'] == 0


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
    ) == [1.0, None, None, None]


def test_ordered_forecast_without_an_explicit_grid_uses_complete_slots() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    state = FakeState("0", {"forecast": [1, 2, 3]})
    assert forecast_series_from_state(
        state, issued_at=now, horizon_hours=1, interval_minutes=25,
        value_keys=("value",), value_kind="power",
    ) == [1.0, 2.0]


def test_ordered_forecast_honors_explicit_minute_cadence_without_extending_coverage() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {"forecast": [1.0, 2.0], "forecast_interval_minutes": 30},
    )

    assert forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=2,
        interval_minutes=15,
        value_keys=("value",),
        value_kind="price",
    ) == [1.0, 1.0, 2.0, 2.0, None, None, None, None]


@pytest.mark.parametrize(("cadence", "fallback_cadence", "expected"), [
    ("invalid", None, [1.0, 2.0, None, None]),
    (1e15, None, [1.0, 2.0, None, None]),
    (1e15, 30, [1.0, 1.0, 2.0, 2.0]),
    ("1e-999999999999999999999", 30, [1.0, 1.0, 2.0, 2.0]),
    ("-1e999999999999999999999", 30, [1.0, 1.0, 2.0, 2.0]),
])
def test_ordered_forecast_uses_first_valid_explicit_cadence(cadence, fallback_cadence, expected) -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {"forecast": [1.0, 2.0], "forecast_interval_minutes": cadence, "interval_minutes": fallback_cadence},
    )

    assert forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("value",),
        value_kind="price",
    ) == expected


@pytest.mark.parametrize("timestamped", [False, True])
@pytest.mark.parametrize("tiny", [5e-324, 1e-9])
def test_forecast_cadence_skips_values_below_datetime_resolution(timestamped, tiny):
    issued_at = datetime(2026, 6, 27, tzinfo=UTC)
    state = FakeState("0", {
        "forecast_interval_minutes": 1e15,
        "interval_minutes": tiny,
        "resolution_minutes": 30,
        "forecast": [{"valid_at": issued_at.isoformat(), "value": 1.0}] if timestamped else [1.0],
    })
    assert forecast_series_from_state(
        state, issued_at=issued_at, horizon_hours=1, interval_minutes=15,
        value_keys=("value",), value_kind="power",
        slot_starts=[issued_at + timedelta(minutes=step) for step in (0, 15, 30, 45)],
    ) == [1.0, 1.0, None, None]


@pytest.mark.parametrize("canonical", [1e15, 30])
def test_forecast_cadence_preserves_valid_spellings_of_the_same_field(canonical):
    issued_at = datetime(2026, 6, 27, tzinfo=UTC)
    state = FakeState("0", {
        "forecastIntervalMinutes": 30 if canonical == 1e15 else 15,
        "forecast_interval_minutes": canonical,
        "interval_minutes": 45,
        "forecast": [{"valid_at": issued_at.isoformat(), "value": 1.0}],
    })
    assert forecast_series_from_state(
        state, issued_at=issued_at, horizon_hours=1, interval_minutes=15,
        value_keys=("value",), value_kind="power",
    ) == [1.0, 1.0, None, None]


def test_empty_forecast_with_oversized_cadence_has_no_coverage() -> None:
    assert forecast_series_from_state(
        FakeState("0", {"forecast": [], "interval_minutes": 1e15}),
        issued_at=datetime(2026, 6, 27, tzinfo=UTC),
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("value",),
        value_kind="price",
    ) is None


def test_timestamped_forecast_does_not_fill_irregular_internal_gaps() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "forecast": [
                {"valid_at": "2026-06-27T00:00:00+00:00", "value": 1.0},
                {"valid_at": "2026-06-27T00:30:00+00:00", "value": 2.0},
                {"valid_at": "2026-06-27T02:00:00+00:00", "value": 3.0},
            ]
        },
    )

    assert forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=3,
        interval_minutes=30,
        value_keys=("value",),
        value_kind="price",
    ) == [1.0, 2.0, None, None, 3.0, None]


def test_timestamped_forecast_honors_explicit_cadence_for_single_point() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {
            "forecast_interval_minutes": 60,
            "forecast": [{"valid_at": "2026-06-27T00:00:00+00:00", "value": 1.0}],
        },
    )

    assert forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=2,
        interval_minutes=15,
        value_keys=("value",),
        value_kind="price",
    ) == [1.0, 1.0, 1.0, 1.0, None, None, None, None]


def test_timestamped_forecast_preserves_missing_slots_before_first_point() -> None:
    issued_at = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    state = FakeState(
        "0",
        {"forecast": [{"valid_at": "2026-06-27T00:30:00+00:00", "value": 1.0}]},
    )

    assert forecast_series_from_state(
        state,
        issued_at=issued_at,
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("value",),
        value_kind="price",
    ) == [None, None, 1.0, None]


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
    assert normalize_scalar_value(0.42, value_kind="carbon_intensity", unit="kgCO₂/kWh") == 420
    assert normalize_scalar_value(420, value_kind="carbon_intensity", unit="gCO2/kWh") == 420
    assert normalize_scalar_value(7, value_kind="other") == 7


def test_energy_buckets_without_timestamps_keep_hourly_fallback() -> None:
    """A provider without timestamps still converts its energy units consistently."""
    assert _energy_items_as_average_power(
        [{"energy": 500, "unit": "Wh"}, {"energy": 2, "unit": "kWh"}],
        ("energy",),
        "kWh",
    ) == [{"energy": 0.5, "unit": "kW"}, {"energy": 2.0, "unit": "kW"}]


def test_indexed_forecast_alignment_preserves_duplicates_and_expired_gaps() -> None:
    from custom_components.ha_energy_planner.forecasts import _value_for_slot

    start = datetime(2026, 1, 1, tzinfo=UTC)
    cadence = timedelta(minutes=5)
    points = [(start + index * cadence, float(index)) for index in range(10000)]
    points.insert(9999, (points[9999][0], -1.0))
    assert _value_for_slot(start - cadence, points, final_cadence=cadence) is None
    assert _value_for_slot(points[-1][0], points, final_cadence=cadence) == 9999.0
    assert _value_for_slot(points[-1][0] + cadence, points, final_cadence=cadence) is None
    assert _value_for_slot(start, [], final_cadence=cadence) is None
    sparse = [points[0], points[-1]]
    assert _value_for_slot(start + cadence, sparse, final_cadence=cadence) is None


@pytest.mark.parametrize("value_key", ["pv_estimate", "pv_estimate10", "pv_estimate90"])
def test_solcast_live_shape_does_not_double_forecast(value_key: str) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "solcast_daily_power.json").read_text())
    series = forecast_series_from_state(
        FakeState(fixture["state"], fixture["attributes"]),
        issued_at=datetime(2026, 9, 11, 2, 30, tzinfo=UTC),
        horizon_hours=1,
        interval_minutes=15,
        value_keys=(value_key,),
        value_kind="power",
    )
    assert series == [5.6506, 5.6506, 5.4, 5.4]
    assert max(series) < 10


def test_generic_energy_forecast_still_converts_inherited_kwh() -> None:
    state = FakeState(
        "0",
        {
            "unit_of_measurement": "kWh",
            "forecast": [
                {"period_start": "2026-09-11T00:00:00Z", "energy": 2},
                {"period_start": "2026-09-11T00:30:00Z", "energy": 3},
            ],
        },
    )
    assert forecast_series_from_state(
        state,
        issued_at=datetime(2026, 9, 11, tzinfo=UTC),
        horizon_hours=1,
        interval_minutes=15,
        value_keys=("energy",),
        value_kind="power",
    ) == [4, 4, 6, 6]


@pytest.mark.parametrize("unit,value", [(" W ", 1500), ("watts", 1500), ("kW", 1.5), ("MW", 0.0015)])
@pytest.mark.parametrize("key", ["pv_estimate", "pv_estimate10", "pv_estimate90"])
def test_pv_estimate_preserves_inherited_power_units(unit: str, value: float, key: str) -> None:
    now = datetime(2026, 9, 11, tzinfo=UTC)
    state = FakeState("0", {
        "unit_of_measurement": unit,
        "forecast": [{"period_start": now.isoformat(), key: value}],
    })
    assert forecast_series_from_state(
        state, issued_at=now, horizon_hours=1, interval_minutes=30,
        value_keys=(key,), value_kind="power",
    ) == [1.5, None]


@pytest.mark.parametrize("native_key", ["per_kwh", "feed_in_price", "export_price"])
def test_native_tariff_detail_overrides_a_resampled_convenience_forecast(native_key):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planning_grid import planning_starts

    now = datetime(2026, 10, 19, 9, 3, tzinfo=UTC)
    native = [{'start_time': now.replace(minute=0).isoformat(), native_key: .1},
              {'start_time': now.replace(minute=30).isoformat(), native_key: .5}]
    source = FakeState('.1', {'forecast': [{'valid_at': now.isoformat(), 'price': .9}],
                               'detailedForecast': native})
    intervals = tariff_source_intervals(source, 15)
    starts = planning_starts(now, 1, 15, intervals)
    assert starts[1] == now.replace(minute=15)
    assert starts[2] == now.replace(minute=30)
    values = forecast_series_from_state(source, issued_at=now, horizon_hours=1,
        interval_minutes=15, value_keys=(native_key, 'price'), value_kind='price', slot_starts=starts)
    assert values == [.1, .1, .5, .5, None]


def test_grid_retains_native_forecast_expiry_between_regular_boundaries():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planning_grid import planning_starts

    origin = datetime(2026, 10, 19, 9, tzinfo=UTC)
    now = origin + timedelta(minutes=3)
    source = FakeState('.1', {'forecast': [{'start_time': origin.isoformat(), 'per_kwh': .1},
        {'start_time': (origin + timedelta(minutes=20)).isoformat(), 'per_kwh': .5}]})
    intervals = tariff_source_intervals(source, 15)
    starts = planning_starts(now, 1, 15, intervals)
    expiry = origin + timedelta(minutes=40)
    assert expiry in starts
    values = forecast_series_from_state(source, issued_at=now, horizon_hours=1, interval_minutes=15,
        value_keys=('per_kwh',), value_kind='price', slot_starts=starts)
    assert values[starts.index(expiry)] is None
    assert values[starts.index(expiry) - 1] == .5


def test_tariff_grid_uses_utc_across_the_repeated_daylight_saving_hour():
    from zoneinfo import ZoneInfo

    from custom_components.ha_energy_planner.planning_grid import planning_starts

    now = datetime(2026, 4, 4, 15, 53, tzinfo=UTC)
    boundary = now.replace(hour=16, minute=0)
    starts = planning_starts(now, 1, 15, [{'start': boundary.isoformat(),
                                         'end': (boundary + timedelta(minutes=30)).isoformat()}])
    zone = ZoneInfo('Australia/Melbourne')
    assert now.astimezone(zone).strftime('%H:%M %z') == '02:53 +1100'
    assert starts[1] == boundary
    assert starts[1].astimezone(zone).strftime('%H:%M %z') == '02:00 +1000'
    assert starts == sorted(set(starts))


@pytest.mark.parametrize("direction", [0, 1], ids=["export", "import"])
@pytest.mark.parametrize("seconds", [-0.8, 0, 9])
def test_captured_amber_boundary_has_continuous_native_coverage(direction, seconds):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planning_grid import planning_starts

    rows = json.loads((Path(__file__).parent / "fixtures/tariffs/amber_boundary_2026_11_04.json").read_text())
    row = rows[direction]
    state = FakeState(row["state"], row["attributes"])
    now = datetime(2026, 11, 4, 8, 0, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    intervals = tariff_source_intervals(state, 15)
    starts = planning_starts(now, 12, 15, intervals)
    series = forecast_series_from_state(
        state, issued_at=now, horizon_hours=12, interval_minutes=15,
        value_keys=("per_kwh", "price", "value"), value_kind="price", slot_starts=starts,
    )
    coverage = forecast_coverage_details(
        series, starts_at=now, interval_minutes=15, slot_starts=starts, horizon_end=now + timedelta(hours=12),
    )
    assert coverage["classification"] == "healthy"
    assert coverage["continuous_hours"] == coverage["covered_hours"] == 12
    assert intervals[0]["start"] == "2026-11-04T08:00:00+00:00"
    assert intervals[0]["end"] == "2026-11-04T08:30:00+00:00"
    assert all(at.second == 0 for at in starts[1:])
    assert series[0] == row["attributes"]["detailedForecast"][0]["per_kwh"]
    assert series[starts.index(datetime(2026, 11, 4, 8, 30, tzinfo=UTC))] == (
        row["attributes"]["detailedForecast"][1]["per_kwh"]
    )


def _amber_state():
    rows = json.loads((Path(__file__).parent / "fixtures/tariffs/amber_boundary_2026_11_04.json").read_text())
    return FakeState(rows[0]["state"], rows[0]["attributes"])


@pytest.mark.parametrize("changes", [
    {"duration": None}, {"duration": True}, {"duration": "30"}, {"duration": float("nan")},
    {"duration": -30}, {"duration": 15}, {"end_time": "bad"},
    {"Duration": 5}, {"values": {"duration": 5}}, {"Values": {"Duration": 5}},
    {"values": {"duration": "30"}},
    {"end_time": "2026-11-04T08:29:59+00:00"},
    {"end_time": "2026-11-04T08:30:01+00:00"},
    {"end_time": "2026-11-04T08:30:00"}, {"start_time": "2026-11-04T08:00:01"},
    {"start_time": "bad"}, {"start_time": "2026-11-04T08:00:02+00:00"},
    {"valid_at": "2026-11-04T08:00:00+00:00"}, {"per_kwh": True}, {"per_kwh": float("inf")},
])
def test_amber_inconsistent_metadata_cannot_fall_back_to_convenience_or_scalar(changes):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    state = _amber_state()
    state.attributes["detailedForecast"][0].update(changes)
    details = {}
    assert tariff_source_intervals(state, 15, details=details) == []
    assert details == {"source_validity": "invalid_intervals", "normalized_intervals": 0}
    series = forecast_series_from_state(
        state, issued_at=datetime(2026, 11, 4, 8, tzinfo=UTC), horizon_hours=12,
        interval_minutes=15, value_keys=("per_kwh", "price", "value"), value_kind="price",
    )
    assert series == [None] * 48


def test_amber_agreeing_duration_declarations_keep_native_intervals():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    state = _amber_state()
    expected = tariff_source_intervals(state, 15)
    state.attributes["detailedForecast"][0].update(Duration=30.0, values={"duration": 30})
    assert tariff_source_intervals(state, 15) == expected


@pytest.mark.parametrize("missing", ["start_time", "end_time", "duration", "per_kwh"])
def test_amber_partial_metadata_is_rejected(missing):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    state = _amber_state()
    del state.attributes["detailedForecast"][1][missing]
    assert tariff_source_intervals(state, 15) == []


@pytest.mark.parametrize("kind", ["leading", "internal", "tail"])
def test_amber_real_gaps_and_insufficient_coverage_remain_unsafe(kind):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planning_grid import planning_starts

    state = _amber_state()
    items = state.attributes["detailedForecast"]
    if kind == "leading":
        items.pop(0)
    elif kind == "internal":
        items.pop(1)
    else:
        del items[14:]
    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    starts = planning_starts(now, 12, 15, tariff_source_intervals(state, 15))
    series = forecast_series_from_state(state, issued_at=now, horizon_hours=12, interval_minutes=15,
        value_keys=("per_kwh",), value_kind="price", slot_starts=starts)
    details = forecast_coverage_details(series, starts_at=now, interval_minutes=15,
        slot_starts=starts, horizon_end=now + timedelta(hours=12))
    assert details["classification"] == "unsafe"
    key = {"leading": "leading", "internal": "internal", "tail": "trailing"}[kind]
    assert details[f"{key}_missing_slots"] > 0


def test_amber_overlap_and_mixed_non_dict_rows_fail_closed():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    state = _amber_state()
    state.attributes["detailedForecast"][1] = state.attributes["detailedForecast"][0]
    assert tariff_source_intervals(state, 15) == []
    state.attributes["detailedForecast"][1] = 0.1
    assert tariff_source_intervals(state, 15) == []


@pytest.mark.parametrize("duration", [5, 15, 30])
def test_declared_exact_high_resolution_boundaries_and_negative_price_changes(duration):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planning_grid import planning_starts

    origin = datetime(2026, 11, 4, 8, 0, 2, 100000, tzinfo=UTC)
    prices = [-0.4, 0.3, -0.2]
    items = [{"startTime": (origin + timedelta(minutes=i * duration)).isoformat(),
              "endTime": (origin + timedelta(minutes=(i + 1) * duration)).isoformat(),
              "duration": duration, "perKwh": price} for i, price in enumerate(prices)]
    state = FakeState("99", {"detailedForecast": items})
    intervals = tariff_source_intervals(state, 15)
    assert datetime.fromisoformat(intervals[0]["start"]) == origin
    assert datetime.fromisoformat(intervals[-1]["end"]) == origin + timedelta(minutes=3 * duration)
    instants = [origin - timedelta(microseconds=1), origin,
                origin + timedelta(minutes=duration) - timedelta(microseconds=1),
                origin + timedelta(minutes=duration), origin + timedelta(minutes=2 * duration),
                origin + timedelta(minutes=3 * duration)]
    assert forecast_series_from_state(state, issued_at=origin, horizon_hours=2, interval_minutes=15,
        value_keys=("per_kwh",), value_kind="price", slot_starts=instants) == [None, -0.4, -0.4, 0.3, -0.2, None]
    starts = planning_starts(origin - timedelta(seconds=2), 2, 15, intervals)
    assert all(datetime.fromisoformat(row["start"]) in starts for row in intervals)


@pytest.mark.parametrize("origin", [
    datetime(2026, 4, 4, 15, 30, tzinfo=UTC),  # Melbourne's repeated hour.
    datetime(2026, 10, 3, 15, 30, tzinfo=UTC),  # Melbourne's skipped hour.
])
def test_amber_local_dst_metadata_preserves_utc_price_instants(origin):
    from zoneinfo import ZoneInfo

    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    zone = ZoneInfo("Australia/Melbourne")
    items = [{"start_time": (origin + timedelta(minutes=30 * i, seconds=1)).astimezone(zone).isoformat(),
              "end_time": (origin + timedelta(minutes=30 * (i + 1))).astimezone(zone).isoformat(),
              "duration": 30, "per_kwh": price} for i, price in enumerate([-0.1, 0.2, -0.3])]
    state = FakeState("99", {"detailedForecast": items})
    intervals = tariff_source_intervals(state, 15)
    assert [datetime.fromisoformat(row["start"]) for row in intervals] == [
        origin + timedelta(minutes=30 * i) for i in range(3)]
    assert forecast_series_from_state(state, issued_at=origin, horizon_hours=1, interval_minutes=15,
        value_keys=("per_kwh",), value_kind="price") == [-0.1, -0.1, 0.2, 0.2]


def test_other_provider_one_second_timestamps_are_not_rounded():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    origin = datetime(2026, 11, 4, 8, tzinfo=UTC)
    state = FakeState("-9", {"forecast": [
        {"start_time": (origin + timedelta(minutes=i * 30, seconds=1)).isoformat(),
         "end_time": (origin + timedelta(minutes=(i + 1) * 30)).isoformat(),
         "duration": 30, "price": price} for i, price in enumerate([0.1, -0.2])
    ]})
    assert tariff_source_intervals(state, 15)[0]["start"] == (origin + timedelta(seconds=1)).isoformat()
    assert forecast_series_from_state(state, issued_at=origin, horizon_hours=1, interval_minutes=15,
        value_keys=("price",), value_kind="price") == [None, 0.1, 0.1, -0.2]


@pytest.mark.parametrize("gap", [2, 30])
def test_declared_tariff_ends_expose_short_internal_gaps(gap):
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planning_grid import planning_starts

    origin = datetime(2026, 11, 4, 8, tzinfo=UTC)
    state = FakeState("9", {"detailedForecast": [
        {"start_time": origin.isoformat(), "end_time": (origin + timedelta(minutes=30)).isoformat(),
         "duration": 30, "per_kwh": -0.1},
        {"start_time": (origin + timedelta(minutes=30, seconds=gap)).isoformat(),
         "end_time": (origin + timedelta(minutes=60, seconds=gap)).isoformat(), "duration": 30, "per_kwh": 0.2},
    ]})
    starts = planning_starts(origin, 1, 15, tariff_source_intervals(state, 15))
    series = forecast_series_from_state(state, issued_at=origin, horizon_hours=1, interval_minutes=15,
        value_keys=("per_kwh",), value_kind="price", slot_starts=starts)
    assert series[starts.index(origin + timedelta(minutes=30))] is None
    assert series[starts.index(origin + timedelta(minutes=30, seconds=gap))] == 0.2
    detail = forecast_coverage_details(series, starts_at=origin, interval_minutes=15,
        slot_starts=starts, horizon_end=origin + timedelta(hours=1))
    assert detail["classification"] == "unsafe"
    assert detail["internal_missing_slots"] == 1


def test_declared_amber_duration_cannot_exceed_explicit_source_cadence():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals

    state = _amber_state()
    state.attributes["forecast_interval_minutes"] = 5
    assert tariff_source_intervals(state, 15) == []
    assert forecast_series_from_state(state, issued_at=datetime(2026, 11, 4, 8, tzinfo=UTC),
        horizon_hours=1, interval_minutes=15, value_keys=("per_kwh",), value_kind="price") == [None] * 4
