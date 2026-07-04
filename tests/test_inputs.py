"""Tests for Home Assistant input normalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BATTERY_SOC,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_EV_CONNECTED,
    CONF_EV_SOC,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_PERSON_ENTITIES,
    CONF_PV_FORECAST,
    CONF_WEATHER,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.inputs import (
    InputManager,
    _attribute_value,
    _combined_confidence,
    _finite_float_or_none,
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

    assert context.input_health == InputHealth.HEALTHY
    assert [slot.import_price for slot in context.slots] == [0.10, 0.20, 0.30, 0.40]
    assert [slot.export_price for slot in context.slots] == [0.01, 0.02, 0.03, 0.04]
    assert [slot.pv_forecast_kw for slot in context.slots] == [0.5, 1.0, 1.0, 1.0]
    assert [slot.baseline_load_forecast_kw for slot in context.slots] == [1.2, 1.2, 1.2, 1.2]
    assert context.current_enphase_profile == "AI Optimisation"
    assert context.enphase_ai_profile == "AI Optimisation"
    assert context.enphase_self_consumption_profile == "Self-Consumption"
    assert context.enphase_full_backup_profile == "Full Backup"
    assert context.current_hvac_mode == "heat"
    assert context.current_hvac_temperature_c == 21.5
    assert context.current_hvac_power_kw == 1.7
    assert context.current_outdoor_temperature_c == 13.2
    assert [slot.outdoor_temperature_forecast_c for slot in context.slots] == [14.0, 16.0, 16.0, 16.0]
    assert manager.thermal_sample(context)["hvac_power_kw"] == 1.7


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
            "sensor.battery": FakeState("55"),
            "sensor.daikin_power": FakeState("1700", {"unitOfMeasurement": "W"}),
            "person.james": FakeState("home"),
        }
    )

    manager = InputManager(hass, entry_data, options)
    context = manager.build_context()

    assert context.current_hvac_power_kw == 1.7
    assert manager.thermal_sample(context)["hvac_power_kw"] == 1.7
    assert manager.current_forecast_observations() == {
        "pv_forecast_kw": 2.0,
        "baseline_load_forecast_kw": 1.5,
    }


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
    assert [slot.outdoor_temperature_forecast_c for slot in context.slots] == [25.0, 25.0, 25.0, 25.0]


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
    assert [slot.outdoor_temperature_forecast_c for slot in context.slots] == [25.0, 25.0, 25.0, 25.0]


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
        "pv_forecast_kw": {"enabled": True, "factor": 1.2, "sample_count": 12},
        "baseline_load_forecast_kw": {"enabled": True, "factor": 0.8, "sample_count": 12},
    }
    manager = InputManager(hass, entry_data, options, forecast_calibration=calibration)

    context = manager.build_context()

    assert [slot.pv_forecast_kw for slot in context.slots] == [1.2, 2.4, 2.4, 2.4]
    assert [slot.baseline_load_forecast_kw for slot in context.slots] == [1.6, 2.4, 2.4, 2.4]
    assert [slot["pv_forecast_kw"] for slot in manager.forecast_training_slots] == [1.0, 2.0, 2.0, 2.0]


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

    context = InputManager(hass, entry_data, options).build_context()

    assert context.input_health == InputHealth.HEALTHY
    assert context.forecast_confidence == 0.62


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
    assert manager._optional_weather_temperatures("weather_key", datetime(2026, 6, 27, tzinfo=UTC), 1, 15)[2] == "weather_key_unavailable"
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
    assert manager._required_series(CONF_PV_FORECAST, ("value",), "power", now, 1, 15)[1] == "pv_forecast_entity_unavailable"
    assert manager._required_series(CONF_AMBER_IMPORT_PRICE, ("value",), "price", now, 1, 15)[1] == "amber_import_price_entity_non_numeric"
    assert "amber_import_price_entity_stale" in manager._freshness_issues(now)
    assert "pv_forecast_entity_stale" in manager._freshness_issues(now)


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
