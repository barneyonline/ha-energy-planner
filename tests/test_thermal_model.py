"""Tests for compact HVAC thermal model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.thermal_model import (
    thermal_active_temperature_rate_c_per_hour,
    thermal_hvac_load_kw,
    thermal_model_summary,
    update_thermal_model,
)


def test_thermal_model_enables_after_enough_active_power_samples() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    model: dict[str, object] = {}

    for index in range(12):
        previous = {
            "sampled_at": (now + timedelta(minutes=5 * index)).isoformat(),
            "indoor_temperature_c": 20.0,
            "hvac_power_kw": 1.8,
        }
        current = {
            "sampled_at": (now + timedelta(minutes=5 * (index + 1))).isoformat(),
            "indoor_temperature_c": 20.1,
            "hvac_power_kw": 1.7,
        }
        model, changed = update_thermal_model(model, previous, current)
        assert changed is True

    summary = thermal_model_summary(model)

    assert summary["enabled"] is True
    assert summary["active_sample_count"] == 12
    assert summary["active_heat_rate_c_per_hour"] == 1.2
    assert summary["active_heat_rate_sample_count"] == 12
    assert thermal_hvac_load_kw(model, 1.0) == 1.8


def test_thermal_model_tracks_active_cooling_rate() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    model: dict[str, object] = {}

    for index in range(12):
        model, changed = update_thermal_model(
            model,
            {
                "sampled_at": (now + timedelta(minutes=5 * index)).isoformat(),
                "indoor_temperature_c": 25.0,
                "hvac_power_kw": 1.6,
            },
            {
                "sampled_at": (now + timedelta(minutes=5 * (index + 1))).isoformat(),
                "indoor_temperature_c": 24.8,
                "hvac_power_kw": 1.6,
            },
        )
        assert changed is True

    summary = thermal_model_summary(model)

    assert summary["enabled"] is True
    assert summary["active_cool_rate_c_per_hour"] == 2.4
    assert summary["active_cool_rate_sample_count"] == 12


def test_thermal_model_tracks_passive_temperature_drift_without_hvac_power() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    model, changed = update_thermal_model(
        {},
        {
            "sampled_at": now.isoformat(),
            "indoor_temperature_c": 20.0,
            "hvac_power_kw": 0.0,
        },
        {
            "sampled_at": (now + timedelta(minutes=30)).isoformat(),
            "indoor_temperature_c": 21.0,
            "hvac_power_kw": 0.0,
        },
    )

    assert changed is True
    assert thermal_model_summary(model)["passive_indoor_drift_c_per_hour"] == 2.0
    assert thermal_hvac_load_kw(model, 1.0) == 1.0


def test_thermal_model_aligns_naive_and_aware_sample_timestamps() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    model, changed = update_thermal_model(
        {},
        {
            "sampled_at": "2026-06-27T00:00:00",
            "indoor_temperature_c": 20.0,
            "hvac_power_kw": 1.8,
        },
        {
            "sampled_at": now.replace(hour=0, minute=30).isoformat(),
            "indoor_temperature_c": 20.5,
            "hvac_power_kw": 1.7,
        },
    )

    assert changed is True
    assert thermal_model_summary(model)["active_sample_count"] == 1
    assert thermal_model_summary(model)["active_hvac_load_kw"] == 1.8


def test_thermal_model_accepts_comma_decimal_sample_values() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    model, changed = update_thermal_model(
        {},
        {
            "sampled_at": now.isoformat(),
            "indoor_temperature_c": "20,0",
            "hvac_power_kw": "1,8",
        },
        {
            "sampled_at": (now + timedelta(minutes=30)).isoformat(),
            "indoor_temperature_c": "20,4",
            "hvac_power_kw": "1,7",
        },
    )

    assert changed is True
    assert thermal_model_summary(model)["active_sample_count"] == 1
    assert thermal_model_summary(model)["active_hvac_load_kw"] == 1.8


def test_thermal_model_ignores_non_finite_sample_values() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    model, changed = update_thermal_model(
        {},
        {
            "sampled_at": now.isoformat(),
            "indoor_temperature_c": 20.0,
            "hvac_power_kw": "nan",
        },
        {
            "sampled_at": (now + timedelta(minutes=30)).isoformat(),
            "indoor_temperature_c": "inf",
            "hvac_power_kw": 1.8,
        },
    )

    assert changed is True
    assert thermal_model_summary(model)["active_sample_count"] == 0
    assert thermal_model_summary(model)["passive_sample_count"] == 0
    assert thermal_hvac_load_kw(model, 1.0) == 1.0


def test_thermal_model_ignores_malformed_persisted_numbers() -> None:
    model = {
        "enabled": True,
        "active_hvac_load_kw": {
            "sample_count": "bad",
            "sum": "nan",
            "average": "nan",
        },
        "passive_indoor_drift_c_per_hour": {
            "sample_count": -4,
            "average": "inf",
        },
    }

    summary = thermal_model_summary(model)

    assert summary["active_sample_count"] == 0
    assert summary["active_hvac_load_kw"] is None
    assert summary["passive_sample_count"] == 0
    assert summary["passive_indoor_drift_c_per_hour"] is None
    assert thermal_hvac_load_kw(model, 1.0) == 1.0
    assert (
        thermal_active_temperature_rate_c_per_hour(
            {"enabled": True, "active_heat_rate_c_per_hour": {"sample_count": 3, "average": "nan"}},
            "heat",
            0.75,
        )
        == 0.75
    )
