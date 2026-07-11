"""Tests for Home Assistant input normalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BASELINE_LOAD_OBSERVED,
    CONF_BATTERY_SOC,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_PERSON_ENTITIES,
    CONF_PV_FORECAST,
    CONF_PV_OBSERVED,
    CONF_WEATHER,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.inputs import (
    InputManager,
    _attribute_value,
    _combined_confidence,
    _finite_float_or_none,
    _forecast_source_issued_at,
    _percent_float_or_none,
    _ready_by_time_or_none,
    _series_value,
    _state_confidence,
)
from custom_components.ha_energy_planner.models import HAEOStatus, InputHealth, OccupancyState


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))


class FakeStates:
    """Minimal HA state registry."""

    def __init__(self, values: dict[str, FakeState]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        return self.values.get(entity_id)


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, FakeState]) -> None:
        self.states = FakeStates(values)


def test_input_manager_uses_forecast_attributes_for_slot_values() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_CLIMATE_TARGET_LOW: "input_number.low",
        CONF_CLIMATE_TARGET_HIGH: "input_number.high",
        CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
        CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
        CONF_ENPHASE_PROFILE: "select.enphase_profile",
        CONF_DAIKIN_CLIMATE: "climate.daikin",
        CONF_DAIKIN_POWER: "sensor.daikin_power",
        CONF_WEATHER: "weather.home",
        CONF_PERSON_ENTITIES: "person.james,person.cath",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState(
                "0.99",
                {"forecast": [{"price": 0.10}, {"price": 0.20}, {"price": 0.30}, {"price": 0.40}]},
            ),
            "sensor.export": FakeState("0.05", {"forecast": [0.01, 0.02, 0.03, 0.04]}),
            "sensor.pv": FakeState(
                "0",
                {"forecast": [{"watts": 500, "unit": "W"}, {"watts": 1000, "unit": "W"}]},
            ),
            "sensor.load": FakeState("1.2"),
            "sensor.battery": FakeState("55"),
            "input_number.low": FakeState("18"),
            "input_number.high": FakeState("24"),
            "select.enphase_profile": FakeState("AI Optimisation"),
            "climate.daikin": FakeState("heat", {"currentTemperature": 21.5}),
            "sensor.daikin_power": FakeState("1.7"),
            "weather.home": FakeState(
                "sunny",
                {
                    "temperature": 13.2,
                    "forecast": [
                        {"temperature": 14.0},
                        {"temperature": 16.0},
                    ],
                },
            ),
            "person.james": FakeState("home"),
            "person.cath": FakeState("not_home"),
        }
    )

    manager = InputManager(hass, entry_data, options)
    context = manager.build_context()

    assert [slot.import_price for slot in context.slots] == [0.10, 0.20, 0.30, 0.40]
    assert [slot.export_price for slot in context.slots] == [0.01, 0.02, 0.03, 0.04]
    assert [slot.pv_forecast_kw for slot in context.slots] == [0.5, 1.0, None, None]
    assert context.input_health == InputHealth.UNSAFE
    assert "pv_forecast_entity_incomplete_horizon" in context.input_issues
    assert context.forecast_confidence == 0.5
    assert [slot.baseline_load_forecast_kw for slot in context.slots] == [1.2, 1.2, 1.2, 1.2]
    assert context.current_enphase_profile == "AI Optimisation"
    assert context.enphase_ai_profile == "AI Optimisation"
    assert context.enphase_self_consumption_profile == "Self-Consumption"
    assert context.enphase_full_backup_profile == "Full Backup"
    assert context.current_hvac_mode == "heat"
    assert context.current_hvac_temperature_c == 21.5
    assert context.current_hvac_power_kw == 1.7
    assert context.current_outdoor_temperature_c == 13.2
    assert [slot.outdoor_temperature_forecast_c for slot in context.slots] == [14.0, 16.0, None, None]
    assert manager.thermal_sample(context)["hvac_power_kw"] == 1.7


def test_input_manager_reads_ev_target_sensor_and_ready_by_select() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_EV_SOC: "sensor.ev_soc",
        CONF_EV_CONNECTED: "binary_sensor.ev_connected",
        CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.ev_target",
        CONF_EV_SMART_CHARGING_READY_BY: "select.ev_ready_by",
        CONF_PERSON_ENTITIES: ["person.james"],
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "sensor.ev_soc": FakeState("72"),
            "binary_sensor.ev_connected": FakeState("on"),
            "sensor.ev_target": FakeState("80", {"device_class": "battery", "unit_of_measurement": "%"}),
            "select.ev_ready_by": FakeState("08:00", {"options": ["07:00", "08:00"]}),
            "person.james": FakeState("home"),
        }
    )

    context = InputManager(hass, entry_data, options).build_context()

    assert context.current_ev_soc_percent == 72
    assert context.ev_connected is True
    assert context.ev_target_soc_percent == 80
    assert context.ev_ready_by == "08:00"
    assert context.input_health == InputHealth.HEALTHY


def test_input_manager_ev_helper_state_edge_cases() -> None:
    manager = InputManager(
        FakeHass(
            {
                "sensor.unavailable": FakeState("unavailable"),
                "sensor.bad_soc": FakeState("bad"),
                "sensor.high_soc": FakeState("101"),
                "select.unavailable": FakeState("unavailable"),
                "select.none": FakeState("None"),
                "select.bad_time": FakeState("25:99"),
            }
        ),
        {},
        DEFAULT_OPTIONS,
    )

    assert manager._optional_soc_state(CONF_EV_SMART_CHARGING_TARGET_SOC) == (None, None)
    manager.entry_data[CONF_EV_SMART_CHARGING_TARGET_SOC] = "sensor.unavailable"
    assert manager._optional_soc_state(CONF_EV_SMART_CHARGING_TARGET_SOC) == (
        None,
        "ev_smart_charging_target_soc_entity_unavailable",
    )
    manager.entry_data[CONF_EV_SMART_CHARGING_TARGET_SOC] = "sensor.bad_soc"
    assert manager._optional_soc_state(CONF_EV_SMART_CHARGING_TARGET_SOC) == (
        None,
        "ev_smart_charging_target_soc_entity_non_numeric",
    )
    manager.entry_data[CONF_EV_SMART_CHARGING_TARGET_SOC] = "sensor.high_soc"
    assert manager._optional_soc_state(CONF_EV_SMART_CHARGING_TARGET_SOC) == (
        None,
        "ev_smart_charging_target_soc_entity_out_of_range",
    )

    assert manager._optional_ready_by_state(CONF_EV_SMART_CHARGING_READY_BY) == (None, None)
    manager.entry_data[CONF_EV_SMART_CHARGING_READY_BY] = "select.unavailable"
    assert manager._optional_ready_by_state(CONF_EV_SMART_CHARGING_READY_BY) == (
        None,
        "ev_smart_charging_ready_by_entity_unavailable",
    )
    manager.entry_data[CONF_EV_SMART_CHARGING_READY_BY] = "select.none"
    assert manager._optional_ready_by_state(CONF_EV_SMART_CHARGING_READY_BY) == (None, None)
    manager.entry_data[CONF_EV_SMART_CHARGING_READY_BY] = "select.bad_time"
    assert manager._optional_ready_by_state(CONF_EV_SMART_CHARGING_READY_BY) == (
        None,
        "ev_smart_charging_ready_by_entity_invalid_time",
    )


def test_ev_helper_value_parsers_handle_supported_formats() -> None:
    assert _percent_float_or_none("80%") == 80
    assert _percent_float_or_none("80,5%") == 80.5
    assert _ready_by_time_or_none("") is None
    assert _ready_by_time_or_none("2026-07-05T08:15:00+10:00") == "08:15"
    assert _ready_by_time_or_none("2026-07-05Tbad") is None
    assert _ready_by_time_or_none("7:05") == "07:05"
    assert _ready_by_time_or_none("07:05:30") == "07:05"
    assert _ready_by_time_or_none("not a time") is None


def test_input_manager_adds_trip_history_summary_to_context() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "person.james": FakeState("home"),
        }
    )
    trip_history = {
        "records": [
            {
                "started_at": "2026-06-24T08:00:00+00:00",
                "ended_at": "2026-06-24T09:00:00+00:00",
                "start_soc_percent": 80,
                "end_soc_percent": 70,
            },
            {
                "started_at": "2026-06-25T08:00:00+00:00",
                "ended_at": "2026-06-25T09:00:00+00:00",
                "start_soc_percent": 80,
                "end_soc_percent": 68,
            },
            {
                "started_at": "2026-06-26T08:00:00+00:00",
                "ended_at": "2026-06-26T09:00:00+00:00",
                "start_soc_percent": 80,
                "end_soc_percent": 74,
            },
        ]
    }

    context = InputManager(hass, entry_data, options, trip_history=trip_history).build_context()

    assert context.ev_trip_observed_days == 3
    assert context.ev_trip_max_daily_soc_percent == 12
    assert context.ev_trip_history_sufficient is True


def test_input_manager_converts_cent_price_point_sensors_to_dollars() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("12", {"unit_of_measurement": "c/kWh"}),
            "sensor.export": FakeState("5", {"unit": "c/kWh"}),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "person.james": FakeState("home"),
        }
    )

    context = InputManager(hass, entry_data, options).build_context()

    assert [slot.import_price for slot in context.slots] == [0.12, 0.12, 0.12, 0.12]
    assert [slot.export_price for slot in context.slots] == [0.05, 0.05, 0.05, 0.05]


def test_input_manager_normalizes_optional_power_point_sensors() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_PV_OBSERVED: "sensor.pv_observed",
        CONF_BASELINE_LOAD_OBSERVED: "sensor.load_observed",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_DAIKIN_POWER: "sensor.daikin_power",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("0.002", {"unitOfMeasurement": "MW"}),
            "sensor.load": FakeState("1500", {"unitOfMeasurement": "W"}),
            "sensor.pv_observed": FakeState("0.002", {"unitOfMeasurement": "MW"}),
            "sensor.load_observed": FakeState("1500", {"unitOfMeasurement": "W"}),
            "sensor.battery": FakeState("55"),
            "sensor.daikin_power": FakeState("1700", {"unitOfMeasurement": "W"}),
            "person.james": FakeState("home"),
        }
    )

    manager = InputManager(hass, entry_data, options)
    context = manager.build_context()

    assert context.current_hvac_power_kw == 1.7
    assert manager.thermal_sample(context)["hvac_power_kw"] == 1.7
    observations = manager.current_forecast_observations()
    assert observations["pv_forecast_kw"]["value"] == 2.0  # type: ignore[index]
    assert observations["baseline_load_forecast_kw"]["value"] == 1.5  # type: ignore[index]


def test_input_manager_accepts_mini_like_ev_connected_state() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_EV_CONNECTED: "sensor.ev_connection",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "sensor.ev_connection": FakeState("connected_not_charging"),
            "person.james": FakeState("home"),
        }
    )

    context = InputManager(hass, entry_data, options).build_context()

    assert context.ev_connected is True
    assert context.input_health == InputHealth.HEALTHY


def test_input_manager_converts_weather_current_temperature_from_fahrenheit() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_WEATHER: "weather.home",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "weather.home": FakeState(
                "sunny",
                {
                    "temperature": 68,
                    "temperature_unit": "F",
                    "forecast": [{"temperature": 77}],
                },
            ),
            "person.james": FakeState("home"),
        }
    )

    context = InputManager(hass, entry_data, options).build_context()

    assert context.current_outdoor_temperature_c == 20.0
    assert [slot.outdoor_temperature_forecast_c for slot in context.slots] == [25.0, None, None, None]
    assert context.input_health == InputHealth.DEGRADED


def test_input_manager_parses_camel_case_current_weather_temperature() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_WEATHER: "weather.home",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "weather.home": FakeState(
                "sunny",
                {
                    "nativeTemperature": 68,
                    "temperatureUnit": "F",
                    "forecast": [{"nativeTemperature": 77}],
                },
            ),
            "person.james": FakeState("home"),
        }
    )

    context = InputManager(hass, entry_data, options).build_context()

    assert context.current_outdoor_temperature_c == 20.0
    assert [slot.outdoor_temperature_forecast_c for slot in context.slots] == [25.0, None, None, None]
    assert context.input_health == InputHealth.DEGRADED


def test_input_manager_applies_enabled_forecast_calibration_to_planning_slots() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0", {"forecast": [1.0, 2.0]}),
            "sensor.load": FakeState("2.0", {"forecast": [2.0, 3.0]}),
            "sensor.battery": FakeState("55"),
            "person.james": FakeState("home"),
        }
    )
    calibration = {
        "pv_forecast_kw": {
            "model_version": 3,
            "buckets": {"0": {"enabled": True, "factor": 1.2}, "1": {"enabled": True, "factor": 1.2}},
        },
        "baseline_load_forecast_kw": {
            "model_version": 3,
            "buckets": {"0": {"enabled": True, "factor": 0.8}, "1": {"enabled": True, "factor": 0.8}},
        },
    }
    manager = InputManager(hass, entry_data, options, forecast_calibration=calibration)

    context = manager.build_context()

    assert [slot.pv_forecast_kw for slot in context.slots] == [1.2, 2.4, None, None]
    assert [slot.baseline_load_forecast_kw for slot in context.slots] == [1.6, 2.4, None, None]
    assert context.input_health == InputHealth.UNSAFE
    assert all(slot["pv_forecast_kw_issued_at"] <= slot["valid_at"] for slot in manager.forecast_training_slots)
    assert all(
        slot["baseline_load_forecast_kw_issued_at"] <= slot["valid_at"]
        for slot in manager.forecast_training_slots
    )


def test_forecast_observations_use_dedicated_measured_entities_with_timestamps() -> None:
    observed_at = datetime(2026, 6, 27, 1, 2, 3, tzinfo=UTC)
    entry_data = {
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load_forecast",
        CONF_PV_OBSERVED: "sensor.pv_power",
        CONF_BASELINE_LOAD_OBSERVED: "sensor.house_power",
    }
    hass = FakeHass(
        {
            "sensor.pv_forecast": FakeState("99", last_updated=observed_at),
            "sensor.load_forecast": FakeState("88", last_updated=observed_at),
            "sensor.pv_power": FakeState("1200", {"unit_of_measurement": "W"}, observed_at),
            "sensor.house_power": FakeState("2.5", {"unit_of_measurement": "kW"}, observed_at),
        }
    )

    observations = InputManager(hass, entry_data, DEFAULT_OPTIONS).current_forecast_observations()

    assert observations == {
        "pv_forecast_kw": {"value": 1.2, "observed_at": observed_at},
        "baseline_load_forecast_kw": {"value": 2.5, "observed_at": observed_at},
    }


def test_forecast_observations_do_not_fall_back_to_forecast_entities() -> None:
    hass = FakeHass(
        {
            "sensor.pv_forecast": FakeState("99"),
            "sensor.load_forecast": FakeState("88"),
        }
    )
    entry_data = {
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load_forecast",
    }

    assert InputManager(hass, entry_data, DEFAULT_OPTIONS).current_forecast_observations() == {
        "pv_forecast_kw": None,
        "baseline_load_forecast_kw": None,
    }


def test_training_slots_keep_per_source_issue_times_across_refreshes(monkeypatch: Any) -> None:
    first_now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    second_now = first_now + timedelta(minutes=5)
    pv_issued = first_now - timedelta(hours=2)
    load_issued = first_now - timedelta(hours=3)
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 5}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
    }
    values = [1.0] * 12
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.2", {"forecast": [0.2] * 12}, first_now),
            "sensor.export": FakeState("0.05", {"forecast": [0.05] * 12}, first_now),
            "sensor.pv": FakeState("1", {"forecast": values}, pv_issued),
            "sensor.load": FakeState(
                "1",
                {"forecast": values, "forecastGeneratedAt": load_issued.isoformat()},
                first_now,
            ),
            "sensor.battery": FakeState("50", last_updated=first_now),
        }
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.inputs.dt_util.utcnow", lambda: first_now)
    first = InputManager(hass, entry_data, options)
    first.build_context()
    monkeypatch.setattr("custom_components.ha_energy_planner.inputs.dt_util.utcnow", lambda: second_now)
    second = InputManager(hass, entry_data, options)
    second.build_context()

    first_common = first.forecast_training_slots[1]
    second_common = second.forecast_training_slots[0]
    assert first_common["valid_at"] == second_common["valid_at"]
    assert first_common["pv_forecast_kw_issued_at"] == second_common["pv_forecast_kw_issued_at"] == pv_issued
    assert (
        first_common["baseline_load_forecast_kw_issued_at"]
        == second_common["baseline_load_forecast_kw_issued_at"]
        == load_issued
    )


def test_forecast_source_issue_time_accepts_datetime_and_naive_fallback() -> None:
    fallback = datetime(2026, 6, 27, 12, 0)
    issued = datetime(2026, 6, 27, 9, 0, tzinfo=UTC)

    assert _forecast_source_issued_at(FakeState("1", {"issued_at": issued}), fallback) == issued
    assert _forecast_source_issued_at(FakeState("1", last_updated=fallback), fallback) == fallback.replace(tzinfo=UTC)


def test_input_manager_combines_forecast_confidence_metadata() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_WEATHER: "weather.home",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20", {"forecast": [0.20, 0.21, 0.22, 0.23], "confidence": 0.91}),
            "sensor.export": FakeState("0.05", {"forecast": [0.05, 0.06, 0.07, 0.08], "confidence_percent": 84}),
            "sensor.pv": FakeState("1.0", {"forecast": [1.0, 2.0, 3.0, 4.0], "forecast_confidence": 0.62}),
            "sensor.load": FakeState(
                "2.0",
                {"forecast": [2.0, 2.1, 2.2, 2.3], "forecast_confidence_percent": 77},
            ),
            "sensor.battery": FakeState("55"),
            "weather.home": FakeState("sunny", {"temperature": 13.2, "forecast": [13.2, 14.0], "confidence": 0.88}),
            "person.james": FakeState("home"),
        }
    )

    manager = InputManager(hass, entry_data, options)
    context = manager.build_context()

    assert context.input_health == InputHealth.DEGRADED
    assert context.forecast_confidence == 0.44
    assert manager.forecast_confidence_details == [
        {
            "config_key": CONF_AMBER_IMPORT_PRICE,
            "entity_id": "sensor.import",
            "source": "forecast_series",
            "confidence": 0.91,
        },
        {
            "config_key": CONF_AMBER_EXPORT_PRICE,
            "entity_id": "sensor.export",
            "source": "forecast_series",
            "confidence": 0.84,
        },
        {
            "config_key": CONF_PV_FORECAST,
            "entity_id": "sensor.pv",
            "source": "forecast_series",
            "confidence": 0.62,
        },
        {
            "config_key": CONF_BASELINE_LOAD_FORECAST,
            "entity_id": "sensor.load",
            "source": "forecast_series",
            "confidence": 0.77,
        },
        {
            "config_key": CONF_WEATHER,
            "entity_id": "weather.home",
            "source": "forecast_series_partial",
            "confidence": 0.44,
        },
    ]


def test_input_manager_marks_required_non_finite_numeric_state_unsafe() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    entry_data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import",
        CONF_AMBER_EXPORT_PRICE: "sensor.export",
        CONF_PV_FORECAST: "sensor.pv",
        CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        CONF_BATTERY_SOC: "sensor.battery",
        CONF_PERSON_ENTITIES: "person.james",
    }
    hass = FakeHass(
        {
            "sensor.import": FakeState("0.20"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("nan"),
            "person.james": FakeState("home"),
        }
    )

    context = InputManager(hass, entry_data, options).build_context()

    assert context.current_battery_soc_percent is None
    assert context.input_health == InputHealth.UNSAFE
    assert context.haeo_status == HAEOStatus.STALE
    assert "battery_soc_entity_non_numeric" in context.input_issues


def test_input_manager_reports_missing_unavailable_and_unknown_occupancy() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    hass = FakeHass(
        {
            "sensor.import": FakeState("unknown"),
            "sensor.export": FakeState("0.05"),
            "sensor.pv": FakeState("1.0"),
            "sensor.load": FakeState("2.0"),
            "sensor.battery": FakeState("55"),
            "person.james": FakeState("unavailable"),
        }
    )

    context = InputManager(
        hass,
        {
            CONF_AMBER_IMPORT_PRICE: "sensor.import",
            CONF_AMBER_EXPORT_PRICE: "sensor.export",
            CONF_PV_FORECAST: "sensor.pv",
            CONF_BASELINE_LOAD_FORECAST: "sensor.load",
            CONF_BATTERY_SOC: "sensor.battery",
            CONF_PERSON_ENTITIES: "person.james",
        },
        options,
    ).build_context()

    assert context.input_health == InputHealth.UNSAFE
    assert context.occupancy_state == OccupancyState.UNKNOWN
    assert "amber_import_price_entity_unavailable" in context.input_issues
    assert "occupancy_unknown" in context.input_issues


def test_input_manager_optional_state_edge_cases() -> None:
    options = {**DEFAULT_OPTIONS, "planning_horizon_hours": 1, "planning_interval_minutes": 15}
    manager = InputManager(
        FakeHass(
            {
                "sensor.unavailable": FakeState("unavailable"),
                "sensor.bad": FakeState("bad"),
                "sensor.power": FakeState("1000", {"unitOfMeasurement": "W"}),
                "binary_sensor.no": FakeState("off"),
                "binary_sensor.unsupported": FakeState("standby"),
                "select.unavailable": FakeState("unknown"),
                "climate.unavailable": FakeState("unavailable"),
                "weather.unavailable": FakeState("unknown"),
                "weather.bad": FakeState("cloudy"),
                "person.away": FakeState("not_home"),
            }
        ),
        {
            "sensor_key": "sensor.unavailable",
            "bad_sensor_key": "sensor.bad",
            CONF_DAIKIN_POWER: "sensor.power",
            "bool_key": "binary_sensor.no",
            "bad_bool_key": "binary_sensor.unsupported",
            "string_key": "select.unavailable",
            "climate_key": "climate.unavailable",
            "weather_key": "weather.unavailable",
            "bad_weather_key": "weather.bad",
            CONF_PERSON_ENTITIES: ["person.away"],
        },
        options,
    )

    assert manager._optional_numeric_state("missing") == (None, None)
    assert manager._optional_numeric_state("sensor_key") == (None, "sensor_key_unavailable")
    assert manager._optional_numeric_state("bad_sensor_key") == (None, "bad_sensor_key_non_numeric")
    assert manager._optional_numeric_state(CONF_DAIKIN_POWER) == (1.0, None)
    assert manager._optional_bool_state("bool_key") == (False, None)
    assert manager._optional_bool_state("bad_bool_key") == (None, "bad_bool_key_unsupported_state")
    assert manager._optional_string_state("missing") == (None, None)
    assert manager._optional_string_state("string_key") == (None, "string_key_unavailable")
    assert manager._optional_climate_state("missing") == (None, None, None)
    assert manager._optional_climate_state("climate_key") == (None, None, "climate_key_unavailable")
    assert (
        manager._optional_weather_temperatures("weather_key", datetime(2026, 6, 27, tzinfo=UTC), 1, 15)[2]
        == "weather_key_unavailable"
    )
    bad_weather = manager._optional_weather_temperatures("bad_weather_key", datetime(2026, 6, 27, tzinfo=UTC), 1, 15)
    assert bad_weather[0] is None
    assert bad_weather[2] == "bad_weather_key_non_numeric_temperature"
    assert manager._occupancy_state() == OccupancyState.AWAY
    assert manager._list_from_config("missing") == []


def test_input_manager_required_series_and_freshness_edge_cases() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    options = {
        **DEFAULT_OPTIONS,
        "planning_horizon_hours": 1,
        "planning_interval_minutes": 15,
        "price_freshness_minutes": 30,
        "forecast_freshness_minutes": 60,
    }
    old_price = now - timedelta(hours=2)
    manager = InputManager(
        FakeHass(
            {
                "sensor.import": FakeState("bad", last_updated=old_price),
                "sensor.export": FakeState("0.05", last_updated=old_price),
                "sensor.pv": FakeState("unknown", last_updated=old_price),
                "sensor.load": FakeState("2.0", last_updated=old_price),
                "sensor.battery": FakeState("unavailable"),
            }
        ),
        {
            CONF_AMBER_IMPORT_PRICE: "sensor.import",
            CONF_AMBER_EXPORT_PRICE: "sensor.export",
            CONF_PV_FORECAST: "sensor.pv",
            CONF_BASELINE_LOAD_FORECAST: "sensor.load",
            CONF_BATTERY_SOC: "sensor.battery",
        },
        options,
    )

    assert manager._numeric_state("missing") == (None, "missing_not_configured")
    assert manager._numeric_state(CONF_BATTERY_SOC) == (None, "battery_soc_entity_unavailable")
    assert manager._required_series("missing", ("value",), "price", now, 1, 15)[1] == "missing_not_configured"
    assert (
        manager._required_series(CONF_PV_FORECAST, ("value",), "power", now, 1, 15)[1]
        == "pv_forecast_entity_unavailable"
    )
    assert (
        manager._required_series(CONF_AMBER_IMPORT_PRICE, ("value",), "price", now, 1, 15)[1]
        == "amber_import_price_entity_non_numeric"
    )
    assert "amber_import_price_entity_stale" in manager._freshness_issues(now)
    assert "pv_forecast_entity_stale" in manager._freshness_issues(now)


def test_input_manager_does_not_mark_timestamped_future_forecast_stale() -> None:
    now = datetime(2026, 6, 27, 9, 0, tzinfo=UTC)
    options = {
        **DEFAULT_OPTIONS,
        "forecast_freshness_minutes": 60,
    }
    old_update = now - timedelta(hours=4)
    manager = InputManager(
        FakeHass(
            {
                "sensor.pv": FakeState(
                    "3.0",
                    {
                        "unit_of_measurement": "kWh",
                        "detailedForecast": [
                            {"period_start": "2026-06-27T00:00:00+00:00", "pv_estimate": 0.5},
                            {"period_start": "2026-06-27T23:30:00+00:00", "pv_estimate": 0.0},
                        ],
                    },
                    last_updated=old_update,
                ),
                "sensor.load": FakeState("2.0", last_updated=old_update),
            }
        ),
        {
            CONF_PV_FORECAST: "sensor.pv",
            CONF_BASELINE_LOAD_FORECAST: "sensor.load",
        },
        options,
    )

    issues = manager._freshness_issues(now)

    assert "pv_forecast_entity_stale" not in issues
    assert "baseline_load_forecast_entity_stale" in issues


def test_input_manager_state_cache_and_small_helpers() -> None:
    hass = FakeHass({"sensor.value": FakeState("1", {"camelCase": 2})})
    manager = InputManager(hass, {}, DEFAULT_OPTIONS)

    first = manager._state("sensor.value")
    hass.states.values["sensor.value"] = FakeState("2")
    second = manager._state("sensor.value")

    assert first is second
    assert _series_value([], 0) is None
    assert _series_value([1.0], 3) == 1.0
    assert _finite_float_or_none("bad") is None
    assert _finite_float_or_none("nan") is None
    assert _attribute_value({}, "missing") is None
    assert _attribute_value({"camelCase": 2}, "camel_case") == 2
    assert _state_confidence(FakeState("0", {"confidence": "bad"}), default=0.5) == 0.5
    assert _state_confidence(FakeState("0", {"confidence_percent": 150}), default=0.5) == 1.0
    assert _combined_confidence([]) == 1.0
    assert InputManager._health_from_issues(["weather_entity_unavailable"]) == InputHealth.DEGRADED
    assert InputManager._health_from_issues([]) == InputHealth.HEALTHY
