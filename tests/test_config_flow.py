"""Tests for config-flow validation helpers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import voluptuous as vol

from custom_components.ha_energy_planner.config_flow import (
    INPUT_STEP_AI,
    INPUT_STEP_CLIMATE,
    INPUT_STEP_ENERGY,
    INPUT_STEP_ENPHASE,
    INPUT_STEP_EV,
    INPUT_STEP_PRESENCE,
    PLANNER_SUBENTRY_SCHEMAS,
    POLICY_STEP_AI_SAFETY,
    POLICY_STEP_CLIMATE,
    POLICY_STEP_DATA_HEALTH,
    POLICY_STEP_ENPHASE,
    POLICY_STEP_EV_BATTERY_GRID,
    POLICY_STEP_PRIORITIES,
    POLICY_STEP_SCHEDULE,
    STEP_USER_DATA_SCHEMA,
    SUBENTRY_EV,
    ConfigFlow,
    OptionsFlow,
    _entity_values,
    _form_suggested_values,
    _normalize_ai_config,
    _normalize_options_input,
    _options_schema,
    _ready_by_valid,
    _validate_config,
    _validate_options,
    _validate_subentry_config,
)
from custom_components.ha_energy_planner.const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_ENABLED,
    CONF_AI_TASK_ENTITY,
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BATTERY_SOC,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_EARLIEST_START,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    CONF_EV_LOW_PRICE_THRESHOLD,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
    CONF_INSTANCE_NAME,
    CONF_PERSON_ENTITIES,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PRIORITY_WEIGHTS,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_SECONDARY,
    CONF_PV_OBSERVED,
    CONF_WEATHER,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.entry_data import combined_entry_data
from custom_components.ha_energy_planner.subentry_migration import (
    async_migrate_subentries_to_entry_data,
    grouped_subentry_data,
)

CONF_BASELINE_LOAD_FORECAST = CONF_HOUSEHOLD_LOAD


@dataclass(slots=True)
class FakeState:
    """Minimal state."""

    state: str = "on"
    attributes: dict[str, Any] | None = None


class FakeStates:
    """Minimal state registry."""

    def __init__(self, entity_ids: set[str], attributes: dict[str, dict[str, Any]] | None = None) -> None:
        self.entity_ids = entity_ids
        self.attributes = attributes or {}

    def get(self, entity_id: str) -> FakeState | None:
        return FakeState(attributes=self.attributes.get(entity_id, {})) if entity_id in self.entity_ids else None


class FakeServices:
    """Minimal service registry."""

    def __init__(self, services: set[tuple[str, str]]) -> None:
        self.services = services

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.services


class FakeHass:
    """Minimal HA object."""

    def __init__(
        self,
        entity_ids: set[str],
        services: set[tuple[str, str]],
        attributes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.states = FakeStates(entity_ids, attributes)
        self.services = FakeServices(services)


class FakeConfigEntries:
    """Minimal config entry manager for migration tests."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.removed: list[str] = []
        self.updated: list[tuple[Any, dict[str, Any]]] = []
        self.entry_updates: list[tuple[Any, dict[str, Any]]] = []

    def async_add_subentry(self, entry: Any, subentry: Any) -> bool:
        self.added.append(subentry)
        return True

    def async_remove_subentry(self, entry: Any, subentry_id: str) -> bool:
        self.removed.append(subentry_id)
        return True

    def async_update_subentry(self, entry: Any, subentry: Any, **changes: Any) -> bool:
        self.updated.append((subentry, changes))
        return True

    def async_update_entry(self, entry: Any, **changes: Any) -> None:
        self.entry_updates.append((entry, changes))


def _valid_input(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
        CONF_DAIKIN_CLIMATE: "climate.daikin",
        CONF_CLIMATE_TARGET_LOW: "input_number.climate_low",
        CONF_CLIMATE_TARGET_HIGH: "input_number.climate_high",
        CONF_PERSON_ENTITIES: "person.james,person.cath",
    }
    data.update(overrides or {})
    return data


def _valid_hass() -> FakeHass:
    return FakeHass(
        {
            "sensor.import_price",
            "sensor.export_price",
            "sensor.pv_forecast",
            "sensor.baseline_load",
            "sensor.battery_soc",
            "climate.daikin",
            "input_number.climate_low",
            "input_number.climate_high",
            "person.james",
            "person.cath",
            "automation.heat",
            "automation.cool",
            "switch.living_zone",
            "input_boolean.study_zone",
            "input_boolean.hvac_override",
            "input_boolean.scheduler_change",
            "timer.scheduler_guard",
            "select.enphase_profile",
            "switch.shared_charger",
            "ai_task.extended_openai_ai_task",
        },
        {
            ("select", "select_option"),
            ("conversation", "process"),
            ("ai_task", "generate_data"),
        },
        {
            "select.enphase_profile": {
                "options": ["Self-Consumption", "AI Optimisation", "Full Backup"],
            },
        },
    )


def _settings_submission(
    flow: OptionsFlow,
    *,
    input_sections: dict[str, dict[str, Any]] | None = None,
    policy_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return a complete central-settings submission using schema defaults."""
    submission: dict[str, dict[str, Any]] = {}
    for marker, validator in flow._settings_schema().schema.items():
        key = str(getattr(marker, "schema", marker))
        current = {
            str(getattr(field, "schema", field)): flow._data[str(getattr(field, "schema", field))]
            for field in validator.schema.schema
            if str(getattr(field, "schema", field)) in flow._data
        }
        submission[key] = validator(current)
    for step_id, values in (input_sections or {}).items():
        target = INPUT_STEP_CLIMATE if step_id == INPUT_STEP_PRESENCE else step_id
        for key in set(submission[target]) & flow._data.keys():
            submission[target].pop(key)
        submission[target].update(values)
    merged_section = {
        POLICY_STEP_SCHEDULE: POLICY_STEP_PRIORITIES,
        POLICY_STEP_EV_BATTERY_GRID: INPUT_STEP_EV,
        POLICY_STEP_CLIMATE: INPUT_STEP_CLIMATE,
        POLICY_STEP_ENPHASE: INPUT_STEP_ENPHASE,
        POLICY_STEP_AI_SAFETY: INPUT_STEP_AI,
        POLICY_STEP_DATA_HEALTH: INPUT_STEP_ENERGY,
        POLICY_STEP_PRIORITIES: POLICY_STEP_PRIORITIES,
    }
    for step_id, values in (policy_overrides or {}).items():
        submission[merged_section[step_id]].update(values)
    return submission


def test_validate_config_accepts_available_entities_and_services() -> None:
    assert _validate_config(_valid_hass(), _valid_input()) == {}


def test_validate_config_requires_complete_scheduler_guard_pair() -> None:
    missing_timer = _validate_config(
        _valid_hass(),
        _valid_input(
            {CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change"}
        ),
    )
    missing_boolean = _validate_config(
        _valid_hass(),
        _valid_input(
            {CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard"}
        ),
    )
    complete = _validate_config(
        _valid_hass(),
        _valid_input(
            {
                CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
                CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard",
            }
        ),
    )

    assert missing_timer[CONF_CLIMATE_SCHEDULER_GUARD_TIMER] == (
        "climate_scheduler_guard_pair_required"
    )
    assert missing_boolean[CONF_CLIMATE_CHANGE_FROM_SCHEDULER] == (
        "climate_scheduler_guard_pair_required"
    )
    assert complete == {}


def test_validate_config_accepts_multi_entity_selector_lists() -> None:
    assert (
        _validate_config(
            _valid_hass(),
            _valid_input(
                {
                    CONF_PERSON_ENTITIES: ["person.james", "person.cath"],
                    CONF_CLIMATE_AUTOMATIONS: ["automation.heat", "automation.cool"],
                }
            ),
        )
        == {}
    )


def test_subentry_validation_rejects_household_actuators_owned_by_another_entry() -> None:
    hass = _valid_hass()
    current_entry = SimpleNamespace(entry_id="entry-current", data={}, subentries={})
    other_entry = SimpleNamespace(
        entry_id="entry-other",
        data={},
        subentries={
            "climate": SimpleNamespace(
                data={
                    CONF_DAIKIN_CLIMATE: "climate.daikin",
                    CONF_CLIMATE_AUTOMATIONS: ["automation.heat"],
                    CONF_CLIMATE_ZONES: ["switch.living_zone"],
                    CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.hvac_override",
                }
            ),
            "enphase": SimpleNamespace(
                data={CONF_ENPHASE_PROFILE: "select.enphase_profile"}
            ),
        },
    )
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [current_entry, other_entry]
    )

    errors = _validate_subentry_config(
        hass,
        current_entry,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: ["automation.heat", "automation.cool"],
            CONF_CLIMATE_ZONES: ["switch.living_zone"],
            CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.hvac_override",
            CONF_ENPHASE_PROFILE: "select.enphase_profile",
            CONF_CLIMATE_TARGET_LOW: "input_number.climate_low",
            CONF_CLIMATE_TARGET_HIGH: "input_number.climate_high",
        },
    )

    assert errors[CONF_DAIKIN_CLIMATE] == "household_actuator_in_use"
    assert errors[CONF_CLIMATE_AUTOMATIONS] == "household_actuator_in_use"
    assert errors[CONF_CLIMATE_ZONES] == "household_actuator_in_use"
    assert errors[CONF_CLIMATE_MANUAL_OVERRIDE] == "household_actuator_in_use"
    assert errors[CONF_ENPHASE_PROFILE] == "household_actuator_in_use"


def test_subentry_validation_allows_current_entry_to_keep_its_actuators() -> None:
    hass = _valid_hass()
    current_entry = SimpleNamespace(
        entry_id="entry-current",
        data={},
        subentries={
            "climate": SimpleNamespace(data={CONF_DAIKIN_CLIMATE: "climate.daikin"})
        },
    )
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [current_entry])

    errors = _validate_subentry_config(
        hass,
        current_entry,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_TARGET_LOW: "input_number.climate_low",
            CONF_CLIMATE_TARGET_HIGH: "input_number.climate_high",
        },
    )

    assert errors == {}


def test_ev_subentry_allows_its_legacy_aliased_actuator_on_reconfigure() -> None:
    hass = _valid_hass()
    current_entry = SimpleNamespace(
        entry_id="entry-current",
        data={},
        options={},
        subentries={
            "ev": SimpleNamespace(
                data={CONF_EV_SMART_CHARGING: "switch.shared_charger"}
            )
        },
    )
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [current_entry])

    errors = _validate_subentry_config(
        hass,
        current_entry,
        {CONF_EV_SMART_CHARGING: "switch.shared_charger"},
        subentry_type=SUBENTRY_EV,
    )

    assert errors == {}


def test_subentry_validation_rejects_ev_controls_owned_under_another_key() -> None:
    hass = _valid_hass()
    current_entry = SimpleNamespace(entry_id="entry-current", data={}, subentries={})
    other_entry = SimpleNamespace(
        entry_id="entry-other",
        data={},
        subentries={
            "ev": SimpleNamespace(
                data={CONF_EV_SMART_CHARGING: "switch.shared_charger"}
            )
        },
    )
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [current_entry, other_entry]
    )

    errors = _validate_subentry_config(
        hass,
        current_entry,
        {CONF_EV_CHARGER_START: "switch.shared_charger"},
        subentry_type=SUBENTRY_EV,
    )

    assert errors[CONF_EV_CHARGER_START] == "household_actuator_in_use"


def test_subentry_validation_rejects_cross_role_actuator_collisions() -> None:
    hass = _valid_hass()
    current_entry = SimpleNamespace(
        entry_id="entry-current",
        data={},
        subentries={
            "ev": SimpleNamespace(data={CONF_EV_CHARGER: "switch.shared_charger"})
        },
    )
    other_entry = SimpleNamespace(
        entry_id="entry-other",
        data={},
        subentries={
            "ev": SimpleNamespace(data={CONF_EV_CHARGER: "switch.living_zone"})
        },
    )
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [current_entry, other_entry]
    )

    errors = _validate_subentry_config(
        hass,
        current_entry,
        {
            CONF_CLIMATE_ZONES: ["switch.shared_charger", "switch.living_zone"],
            CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.hvac_override",
            CONF_EV_SMART_CHARGING: "input_boolean.hvac_override",
        },
    )

    assert errors[CONF_CLIMATE_ZONES] == "household_actuator_in_use"
    assert errors[CONF_CLIMATE_MANUAL_OVERRIDE] == "household_actuator_in_use"
    assert errors[CONF_EV_SMART_CHARGING] == "household_actuator_in_use"


def test_ev_subentry_rejects_keep_on_without_persistent_control() -> None:
    hass = _valid_hass()
    entry = SimpleNamespace(
        entry_id="entry-current",
        data={},
        options={CONF_EV_KEEP_CHARGER_ON: True},
        subentries={},
    )
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    errors = _validate_subentry_config(
        hass,
        entry,
        {},
        subentry_type=SUBENTRY_EV,
    )

    assert errors["base"] == "ev_keep_on_requires_persistent_control"


def test_initial_config_schema_requires_a_planner_name() -> None:
    assert {getattr(key, "schema", key) for key in STEP_USER_DATA_SCHEMA.schema} == {CONF_INSTANCE_NAME}


def test_config_schema_does_not_default_environment_specific_people() -> None:
    presence_schema = PLANNER_SUBENTRY_SCHEMAS["presence"]
    schema_key = next(key for key in presence_schema.schema if getattr(key, "schema", None) == CONF_PERSON_ENTITIES)

    assert getattr(schema_key, "default", None) is vol.UNDEFINED


def test_presence_flow_uses_multi_entity_selector_for_people() -> None:
    presence_schema = PLANNER_SUBENTRY_SCHEMAS["presence"]
    schema_fields = {getattr(key, "schema", key): selector for key, selector in presence_schema.schema.items()}

    assert schema_fields[CONF_PERSON_ENTITIES].serialize()["selector"]["entity"] == {
        "domain": ["person"],
        "multiple": True,
        "reorder": False,
    }


def test_climate_flow_uses_multi_entity_selector_for_automations() -> None:
    climate_schema = PLANNER_SUBENTRY_SCHEMAS["climate"]
    schema_fields = {getattr(key, "schema", key): selector for key, selector in climate_schema.schema.items()}

    assert schema_fields[CONF_CLIMATE_AUTOMATIONS].serialize()["selector"]["entity"] == {
        "domain": ["automation"],
        "multiple": True,
        "reorder": False,
    }
    assert schema_fields[CONF_CLIMATE_ZONES].serialize()["selector"]["entity"] == {
        "domain": ["switch", "input_boolean"],
        "multiple": True,
        "reorder": False,
    }


def test_energy_flow_filters_sensor_selectors_by_expected_units() -> None:
    energy_schema = PLANNER_SUBENTRY_SCHEMAS["energy"]
    schema_fields = {getattr(key, "schema", key): selector for key, selector in energy_schema.schema.items()}

    import_filter = schema_fields[CONF_AMBER_IMPORT_PRICE].serialize()["selector"]["entity"]["filter"][0]
    export_filter = schema_fields[CONF_AMBER_EXPORT_PRICE].serialize()["selector"]["entity"]["filter"][0]
    pv_filter = schema_fields[CONF_PV_FORECAST].serialize()["selector"]["entity"]["filter"][0]
    second_pv_filter = schema_fields[CONF_PV_FORECAST_SECONDARY].serialize()["selector"]["entity"]["filter"][0]
    baseline_filter = schema_fields[CONF_BASELINE_LOAD_FORECAST].serialize()["selector"]["entity"]["filter"][0]
    carbon_filter = schema_fields[CONF_CARBON_INTENSITY_FORECAST].serialize()["selector"]["entity"]["filter"][0]
    pv_observed_filter = schema_fields[CONF_PV_OBSERVED].serialize()["selector"]["entity"]["filter"][0]
    battery_filter = schema_fields[CONF_BATTERY_SOC].serialize()["selector"]["entity"]["filter"][0]

    assert import_filter["domain"] == ["sensor"]
    assert "AUD/kWh" in import_filter["unit_of_measurement"]
    assert "c/kWh" in export_filter["unit_of_measurement"]
    assert {"W", "kW", "kWh"} <= set(pv_filter["unit_of_measurement"])
    assert second_pv_filter == pv_filter
    assert "kW" in baseline_filter["unit_of_measurement"]
    assert "kWh" not in baseline_filter["unit_of_measurement"]
    assert {"gCO2/kWh", "kgCO₂/kWh"} <= set(carbon_filter["unit_of_measurement"])
    assert pv_observed_filter["unit_of_measurement"] == ["W", "kW", "MW"]
    assert battery_filter["unit_of_measurement"] == ["%", "percent", "percentage"]


def test_recommended_default_planning_horizon_is_twelve_hours() -> None:
    schema_fields = {getattr(key, "schema", key): key for key in _options_schema({}).schema}

    assert DEFAULT_OPTIONS[CONF_PLANNING_HORIZON_HOURS] == 12
    assert schema_fields[CONF_PLANNING_HORIZON_HOURS].default() == 12


def test_observed_power_sensor_must_not_be_the_forecast_sensor() -> None:
    hass = FakeHass({"sensor.pv"}, set(), {"sensor.pv": {"unit_of_measurement": "kW"}})

    errors = _validate_config(
        hass,
        {CONF_PV_FORECAST: "sensor.pv", CONF_PV_OBSERVED: "sensor.pv"},
    )

    assert errors[CONF_PV_OBSERVED] == "observation_must_differ_from_forecast"


def test_secondary_pv_forecast_must_be_distinct() -> None:
    hass = FakeHass(
        {"sensor.pv", "sensor.observed"},
        set(),
        {
            "sensor.pv": {"unit_of_measurement": "kW"},
            "sensor.observed": {"unit_of_measurement": "kW"},
        },
    )

    duplicate = _validate_config(
        hass,
        {CONF_PV_FORECAST: "sensor.pv", CONF_PV_FORECAST_SECONDARY: "sensor.pv"},
    )
    observed = _validate_config(
        hass,
        {
            CONF_PV_FORECAST: "sensor.pv",
            CONF_PV_FORECAST_SECONDARY: "sensor.observed",
            CONF_PV_OBSERVED: "sensor.observed",
        },
    )

    assert duplicate[CONF_PV_FORECAST_SECONDARY] == "forecast_sources_must_differ"
    assert observed[CONF_PV_OBSERVED] == "observation_must_differ_from_forecast"


def test_related_power_and_soc_fields_filter_sensor_selectors_by_expected_units() -> None:
    climate_schema = PLANNER_SUBENTRY_SCHEMAS["climate"]
    ev_schema = PLANNER_SUBENTRY_SCHEMAS["ev"]
    climate_fields = {getattr(key, "schema", key): selector for key, selector in climate_schema.schema.items()}
    ev_fields = {getattr(key, "schema", key): selector for key, selector in ev_schema.schema.items()}

    daikin_filter = climate_fields[CONF_DAIKIN_POWER].serialize()["selector"]["entity"]["filter"][0]
    ev_soc_filter = ev_fields[CONF_EV_SOC].serialize()["selector"]["entity"]["filter"][0]

    assert daikin_filter["domain"] == ["sensor"]
    assert daikin_filter["unit_of_measurement"] == ["W", "kW", "MW"]
    assert ev_soc_filter["domain"] == ["sensor"]
    assert ev_soc_filter["unit_of_measurement"] == ["%", "percent", "percentage"]


def test_weather_entity_lives_in_climate_group_not_energy_group() -> None:
    climate_schema = PLANNER_SUBENTRY_SCHEMAS["climate"]
    energy_schema = PLANNER_SUBENTRY_SCHEMAS["energy"]

    climate_fields = {getattr(key, "schema", key) for key in climate_schema.schema}
    energy_fields = {getattr(key, "schema", key) for key in energy_schema.schema}

    assert CONF_WEATHER in climate_fields
    assert CONF_WEATHER not in energy_fields


def test_enphase_profile_defaults_match_planner_roles() -> None:
    enphase_schema = PLANNER_SUBENTRY_SCHEMAS["enphase"]
    fields = {getattr(key, "schema", key) for key in enphase_schema.schema}
    defaults = {
        getattr(key, "schema", key): getattr(key, "default", None)()
        for key in enphase_schema.schema
        if callable(getattr(key, "default", None))
    }

    assert CONF_ENPHASE_PROFILE_CONTROL_SERVICE not in fields
    assert defaults[CONF_ENPHASE_AI_PROFILE] == "AI Optimisation"
    assert defaults[CONF_ENPHASE_SELF_CONSUMPTION_PROFILE] == "Self-Consumption"
    assert defaults[CONF_ENPHASE_FULL_BACKUP_PROFILE] == "Full Backup"


def test_ev_charger_controls_accept_switches_buttons_and_input_buttons() -> None:
    ev_schema = PLANNER_SUBENTRY_SCHEMAS["ev"]
    schema_fields = {getattr(key, "schema", key): selector for key, selector in ev_schema.schema.items()}

    assert schema_fields[CONF_EV_CHARGER].serialize()["selector"]["entity"]["domain"] == [
        "switch",
        "input_boolean",
    ]
    assert schema_fields[CONF_EV_CHARGER_START].serialize()["selector"]["entity"]["domain"] == [
        "switch",
        "button",
        "input_boolean",
        "input_button",
    ]
    assert schema_fields[CONF_EV_CHARGER_STOP].serialize()["selector"]["entity"]["domain"] == [
        "switch",
        "button",
        "input_boolean",
        "input_button",
    ]


def test_ev_target_soc_can_follow_vehicle_and_ready_by_remains_native() -> None:
    ev_schema = PLANNER_SUBENTRY_SCHEMAS["ev"]
    schema_fields = {getattr(key, "schema", key): selector for key, selector in ev_schema.schema.items()}

    assert CONF_EV_SMART_CHARGING_TARGET_SOC in schema_fields
    assert CONF_EV_SMART_CHARGING_READY_BY not in schema_fields


def test_validate_config_accepts_input_button_ev_controls() -> None:
    hass = FakeHass(
        {"input_button.ev_start", "input_button.ev_stop"},
        {("select", "select_option")},
    )

    assert (
        _validate_config(
            hass,
            {
                CONF_EV_SMART_CHARGING_START: "input_button.ev_start",
                CONF_EV_SMART_CHARGING_STOP: "input_button.ev_stop",
            },
        )
        == {}
    )


def test_validate_config_accepts_ev_target_soc_sensor_and_ready_by_select() -> None:
    hass = FakeHass(
        {"sensor.ev_target_soc", "select.ev_ready_by"},
        {("select", "select_option")},
        {
            "sensor.ev_target_soc": {
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "device_class": "battery",
            },
            "select.ev_ready_by": {"options": ["06:00", "07:00", "08:00"]},
        },
    )

    assert (
        _validate_config(
            hass,
            {
                CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.ev_target_soc",
                CONF_EV_SMART_CHARGING_READY_BY: "select.ev_ready_by",
            },
        )
        == {}
    )


def test_form_suggested_values_convert_legacy_comma_lists_for_multi_selectors() -> None:
    assert _form_suggested_values(
        {
            CONF_PERSON_ENTITIES: "person.james, person.cath",
            CONF_CLIMATE_AUTOMATIONS: "automation.heat,automation.cool",
        }
    ) == {
        CONF_PERSON_ENTITIES: ["person.james", "person.cath"],
        CONF_CLIMATE_AUTOMATIONS: ["automation.heat", "automation.cool"],
    }


def test_config_flow_fields_have_readable_translation_labels() -> None:
    strings = _strings()
    labels = strings["config"]["step"]["user"]["data"]
    descriptions = strings["config"]["step"]["user"]["data_description"]
    schema_keys = {str(getattr(key, "schema", key)) for key in STEP_USER_DATA_SCHEMA.schema}

    assert schema_keys <= labels.keys()
    assert schema_keys <= descriptions.keys()
    for key in schema_keys:
        assert labels[key] != key
        assert "_" not in labels[key]


def test_central_input_sections_have_readable_translation_labels() -> None:
    strings = _strings()
    step_by_type = {
        "energy": INPUT_STEP_ENERGY,
        "climate": INPUT_STEP_CLIMATE,
        "presence": INPUT_STEP_CLIMATE,
        "enphase": INPUT_STEP_ENPHASE,
        "ai": INPUT_STEP_AI,
        "ev": INPUT_STEP_EV,
    }

    for input_type, schema in PLANNER_SUBENTRY_SCHEMAS.items():
        section = strings["options"]["step"]["init"]["sections"][step_by_type[input_type]]
        labels = section["data"]
        descriptions = section["data_description"]
        schema_keys = {str(getattr(key, "schema", key)) for key in schema.schema}

        assert section["name"]
        assert section["description"]
        assert schema_keys <= labels.keys()
        assert schema_keys <= descriptions.keys()
        for key in schema_keys:
            assert labels[key] != key
            assert "_" not in labels[key]


def test_english_locale_files_include_central_input_section_labels() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"
    expected_steps = {
        INPUT_STEP_ENERGY,
        INPUT_STEP_CLIMATE,
        INPUT_STEP_ENPHASE,
        INPUT_STEP_AI,
        INPUT_STEP_EV,
        POLICY_STEP_PRIORITIES,
    }

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        assert "config_subentries" not in translations
        assert expected_steps <= translations["options"]["step"]["init"]["sections"].keys()


def test_english_locale_files_translate_reconfigure_success() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))

        assert translations["config"]["abort"]["reconfigure_successful"] == "Reconfigure Successful"


def test_english_locale_files_explain_solcast_pv_forecast_sensor() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        description = translations["options"]["step"]["init"]["sections"][INPUT_STEP_ENERGY][
            "data_description"
        ][CONF_PV_FORECAST]

        assert "Forecast Today" in description
        assert "Peak Forecast Today" in description
        assert "detailedForecast" in description


def test_english_locale_files_explain_bom_hourly_weather_forecast() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        description = translations["options"]["step"]["init"]["sections"][INPUT_STEP_CLIMATE][
            "data_description"
        ][CONF_WEATHER]

        assert "Bureau of Meteorology" in description
        assert "Hourly" in description
        assert "temperature forecast" in description


def test_english_locale_files_label_ev_charge_rate_as_kw() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        label = translations["options"]["step"]["init"]["sections"][INPUT_STEP_EV]["data"][
            CONF_EV_CHARGE_RATE_KW
        ]

        assert label == "EV charge rate (kW)"


def test_options_flow_fields_have_readable_translation_labels() -> None:
    strings = _strings()
    labels: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for section_strings in strings["options"]["step"]["init"]["sections"].values():
        labels.update(section_strings.get("data", {}))
        descriptions.update(section_strings.get("data_description", {}))
    schema_keys = {str(getattr(key, "schema", key)) for key in _options_schema(dict(DEFAULT_OPTIONS)).schema}

    assert schema_keys <= labels.keys()
    assert schema_keys <= descriptions.keys()
    for key in schema_keys:
        assert labels[key] != key
        assert "_" not in labels[key]
        assert descriptions[key]
        assert descriptions[key] != key


def test_plan_fallback_notification_option_copy_matches_toggle_style() -> None:
    expected_heading = "Enable plan fallback notifications"
    expected_description = (
        "Shows persistent notifications only for problems that normally require user action, such as broken "
        "required mappings or a configured grid hard limit. Routine changes and successful recovery do not notify."
    )

    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"
    for path in (
        integration_dir / "strings.json",
        integration_dir / "translations" / "en.json",
        integration_dir / "translations" / "en-AU.json",
        integration_dir / "translations" / "en-GB.json",
    ):
        strings = json.loads(path.read_text(encoding="utf-8"))
        section = strings["options"]["step"]["init"]["sections"][INPUT_STEP_AI]
        assert section["data"][CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED] == expected_heading
        assert section["data_description"][CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED] == expected_description


def test_options_flow_init_shows_one_collapsible_settings_form() -> None:
    flow = OptionsFlow(SimpleNamespace(options={}))

    result = asyncio.run(flow.async_step_init())

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["last_step"] is True
    assert tuple(str(getattr(key, "schema", key)) for key in result["data_schema"].schema) == (
        INPUT_STEP_ENERGY,
        INPUT_STEP_CLIMATE,
        INPUT_STEP_ENPHASE,
        INPUT_STEP_AI,
        INPUT_STEP_EV,
        POLICY_STEP_PRIORITIES,
    )


def test_config_flow_user_step_creates_entry_after_confirmation() -> None:
    flow = ConfigFlow.__new__(ConfigFlow)
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow.async_create_entry = Mock(return_value={"type": "create_entry"})
    flow.async_show_form = Mock(return_value={"type": "form"})

    form = asyncio.run(flow.async_step_user())
    created = asyncio.run(flow.async_step_user({CONF_INSTANCE_NAME: "Commuter EV"}))

    assert form == {"type": "form"}
    assert created == {"type": "create_entry"}
    flow.async_set_unique_id.assert_not_awaited()
    flow._abort_if_unique_id_configured.assert_not_called()
    flow.async_create_entry.assert_called_once_with(
        title="Commuter EV",
        data={CONF_INSTANCE_NAME: "Commuter EV"},
        options=DEFAULT_OPTIONS,
    )


def test_config_flow_rejects_duplicate_planner_name() -> None:
    flow = ConfigFlow.__new__(ConfigFlow)
    flow.hass = SimpleNamespace()
    flow._async_current_entries = Mock(return_value=[SimpleNamespace(title="Commuter EV")])
    flow.async_show_form = Mock(return_value={"type": "form"})

    result = asyncio.run(flow.async_step_user({CONF_INSTANCE_NAME: "Commuter EV"}))

    assert result == {"type": "form"}
    assert flow.async_show_form.call_args.kwargs["errors"] == {CONF_INSTANCE_NAME: "instance_name_in_use"}


def test_config_flow_rejects_blank_planner_name() -> None:
    flow = ConfigFlow.__new__(ConfigFlow)
    flow.async_show_form = Mock(return_value={"type": "form"})

    result = asyncio.run(flow.async_step_user({CONF_INSTANCE_NAME: "   "}))

    assert result == {"type": "form"}
    assert flow.async_show_form.call_args.kwargs["errors"] == {CONF_INSTANCE_NAME: "instance_name_required"}


def test_config_flow_reports_options_without_add_device_subentry_flows() -> None:
    options_flow = ConfigFlow.async_get_options_flow(SimpleNamespace(options={}))

    assert isinstance(options_flow, OptionsFlow)
    assert ConfigFlow.async_get_supported_subentry_types(SimpleNamespace()) == {}


def test_options_flow_consolidates_related_settings_sections() -> None:
    flow = OptionsFlow(SimpleNamespace(options={}))
    schema = flow._settings_schema()

    assert len(schema.schema) == 6
    assert all(validator.options["collapsed"] is True for validator in schema.schema.values())


def test_options_flow_accepts_a_partial_combined_section_submission() -> None:
    flow = OptionsFlow(SimpleNamespace(data={}, options={}))
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace())
    schema = flow._settings_schema()
    planning = next(
        validator
        for marker, validator in schema.schema.items()
        if str(getattr(marker, "schema", marker)) == POLICY_STEP_PRIORITIES
    )

    result = asyncio.run(
        flow.async_step_init({POLICY_STEP_PRIORITIES: planning({})})
    )

    assert result["type"] == "create_entry"


def test_ev_section_combines_mappings_ready_by_and_opportunistic_controls() -> None:
    schema = OptionsFlow(SimpleNamespace(data={}, options={}))._settings_schema()
    ev_section = next(
        validator
        for marker, validator in schema.schema.items()
        if str(getattr(marker, "schema", marker)) == INPUT_STEP_EV
    )
    fields = {
        str(getattr(marker, "schema", marker))
        for marker in ev_section.schema.schema
    }

    assert {
        CONF_EV_SOC,
        CONF_EV_CHARGER,
        CONF_DEFAULT_READY_BY,
        CONF_EV_LOW_PRICE_CHARGING_ENABLED,
        CONF_EV_LOW_PRICE_THRESHOLD,
        CONF_EV_KEEP_CHARGER_ON,
    } <= fields


def test_options_flow_excludes_settings_managed_by_native_entities() -> None:
    schema_keys = {
        str(getattr(key, "schema", key))
        for key in _options_schema(dict(DEFAULT_OPTIONS)).schema
    }

    assert schema_keys.isdisjoint(
        {
            CONF_PLANNER_ENABLED,
            CONF_DRY_RUN,
            CONF_AI_ENABLED,
            CONF_EV_CONTROL_ENABLED,
            CONF_CLIMATE_CONTROL_ENABLED,
            CONF_ENPHASE_CONTROL_ENABLED,
        }
    )

    assert CONF_EV_FALLBACK_TARGET_SOC_PERCENT in schema_keys
    assert CONF_DEFAULT_READY_BY in schema_keys
    assert CONF_EV_LOW_PRICE_CHARGING_ENABLED in schema_keys
    assert CONF_EV_LOW_PRICE_THRESHOLD in schema_keys
    assert CONF_EV_KEEP_CHARGER_ON in schema_keys


def test_options_flow_saves_all_sections_together_and_preserves_options() -> None:
    entry = SimpleNamespace(
        data={CONF_INSTANCE_NAME: "Energy Planner"},
        options={
            CONF_EV_MIN_SOC_PERCENT: 55,
            CONF_PRIORITY_WEIGHTS: "comfort,cost,ev_readiness,battery_reserve,solar_self_consumption,carbon",
        },
    )
    updates: list[dict[str, Any]] = []
    flow = OptionsFlow(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda entry_arg, **kwargs: updates.append(kwargs),
        )
    )
    submission = _settings_submission(
        flow,
        policy_overrides={
            POLICY_STEP_SCHEDULE: {
                CONF_PLANNING_HORIZON_HOURS: 36,
                CONF_PLANNING_INTERVAL_MINUTES: 10,
            }
        },
    )

    result = asyncio.run(flow.async_step_init(submission))

    assert result["type"] == "create_entry"
    assert updates == [
        {
            "data": {
                CONF_INSTANCE_NAME: "Energy Planner",
                CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
                CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
                CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
                CONF_AI_ADVISOR_SERVICE: "",
            }
        }
    ]
    assert result["data"][CONF_PLANNING_HORIZON_HOURS] == 36
    assert result["data"][CONF_EV_MIN_SOC_PERCENT] == 55
    assert result["data"][CONF_PRIORITY_WEIGHTS] == (
        "comfort,cost,ev_readiness,battery_reserve,solar_self_consumption,carbon"
    )


def test_options_flow_sections_prefill_saved_policy_values() -> None:
    flow = OptionsFlow(
        SimpleNamespace(
            data={},
            options={
                CONF_PLANNING_HORIZON_HOURS: 36,
                CONF_PLANNING_INTERVAL_MINUTES: 10,
            },
        )
    )
    schema = flow._settings_schema()
    schedule = next(
        validator
        for marker, validator in schema.schema.items()
        if str(getattr(marker, "schema", marker)) == POLICY_STEP_PRIORITIES
    )
    fields = {
        str(getattr(marker, "schema", marker)): marker
        for marker in schedule.schema.schema
    }

    assert fields[CONF_PLANNING_HORIZON_HOURS].default() == 36
    assert fields[CONF_PLANNING_INTERVAL_MINUTES].default() == 10


def test_options_flow_section_validation_returns_form_errors() -> None:
    flow = OptionsFlow(SimpleNamespace(data={}, options={}))
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace())
    submission = _settings_submission(
        flow,
        policy_overrides={
            POLICY_STEP_EV_BATTERY_GRID: {
                CONF_EV_MIN_SOC_PERCENT: 95,
                CONF_EV_MAX_SOC_PERCENT: 80,
            }
        },
    )

    result = asyncio.run(flow.async_step_init(submission))

    assert result["type"] == "form"
    assert result["errors"]["base"] == "ev_min_above_max"


def test_options_flow_rejects_incomplete_climate_scheduler_guard() -> None:
    entry = SimpleNamespace(entry_id="entry-current", data={}, options={}, subentries={})
    flow = OptionsFlow(entry)
    flow.hass = _valid_hass()
    flow.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    submission = _settings_submission(
        flow,
        input_sections={
            INPUT_STEP_CLIMATE: {
                CONF_DAIKIN_CLIMATE: "climate.daikin",
                CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
                CONF_CLIMATE_TARGET_LOW: "input_number.climate_low",
                CONF_CLIMATE_TARGET_HIGH: "input_number.climate_high",
            }
        },
    )

    result = asyncio.run(flow.async_step_init(submission))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "climate_scheduler_guard_pair_required"}


def test_options_flow_surfaces_entity_managed_setting_errors_at_form_level() -> None:
    flow = OptionsFlow(
        SimpleNamespace(
            data={},
            options={CONF_EV_FALLBACK_TARGET_SOC_PERCENT: 90},
        )
    )
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace())
    submission = _settings_submission(
        flow,
        policy_overrides={
            POLICY_STEP_EV_BATTERY_GRID: {
                CONF_EV_MIN_SOC_PERCENT: 40,
                CONF_EV_MAX_SOC_PERCENT: 80,
            }
        },
    )

    result = asyncio.run(flow.async_step_init(submission))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "ev_fallback_outside_bounds"}


def test_options_flow_rejects_keep_on_without_persistent_control() -> None:
    flow = OptionsFlow(
        SimpleNamespace(
            data={
                CONF_EV_SMART_CHARGING_START: "button.ev_start",
                CONF_EV_SMART_CHARGING_STOP: "button.ev_stop",
            },
            options={},
        )
    )
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace())
    submission = _settings_submission(
        flow,
        policy_overrides={
            POLICY_STEP_EV_BATTERY_GRID: {CONF_EV_KEEP_CHARGER_ON: True}
        },
    )

    result = asyncio.run(flow.async_step_init(submission))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "ev_keep_on_requires_persistent_control"}


def test_options_flow_uses_ordered_priority_dropdowns() -> None:
    schema_fields = {
        str(getattr(key, "schema", key)): selector
        for key, selector in _options_schema(dict(DEFAULT_OPTIONS)).schema.items()
    }

    assert CONF_PRIORITY_WEIGHTS not in schema_fields
    assert all(f"planning_priority_{index}" in schema_fields for index in range(1, 7))
    first_priority = schema_fields["planning_priority_1"].serialize()["selector"]["select"]
    assert first_priority["mode"] == "dropdown"
    assert first_priority["options"][0] == {"value": "cost", "label": "Cost"}
    assert first_priority["options"][1] == {"value": "comfort", "label": "Comfort"}


def test_options_flow_stores_ordered_priority_dropdowns_as_priority_weights() -> None:
    user_input = {
        **DEFAULT_OPTIONS,
        "planning_priority_1": "comfort",
        "planning_priority_2": "cost",
        "planning_priority_3": "ev_readiness",
        "planning_priority_4": "battery_reserve",
        "planning_priority_5": "solar_self_consumption",
        "planning_priority_6": "carbon",
    }

    normalized = _normalize_options_input(user_input)

    assert (
        normalized[CONF_PRIORITY_WEIGHTS] == "comfort,cost,ev_readiness,battery_reserve,solar_self_consumption,carbon"
    )
    assert "planning_priority_1" not in normalized


def test_production_code_does_not_hardcode_inventory_entity_ids() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"
    production_text = "\n".join(path.read_text(encoding="utf-8") for path in integration_dir.glob("*.py"))

    assert "person.james" not in production_text
    assert "person.cath" not in production_text


def test_combined_entry_data_merges_subentries_after_hub_data() -> None:
    entry = SimpleNamespace(
        data={
            "haeo_optimize_service": "haeo.optimize",
            "haeo_config_entry_id": "optimizer-entry",
            CONF_AMBER_IMPORT_PRICE: "sensor.old_import",
        },
        subentries={
            "prices": SimpleNamespace(
                data={
                    CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
                    CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
                },
            ),
            "forecasts": SimpleNamespace(
                data={
                    "haeo_entry_id": "legacy-optimizer-entry",
                    CONF_PV_FORECAST: "sensor.pv_forecast",
                    CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
                    CONF_BATTERY_SOC: "sensor.battery_soc",
                },
            ),
        },
    )

    assert combined_entry_data(entry) == {
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
    }


def test_central_energy_settings_updates_main_entry_data() -> None:
    entry = SimpleNamespace(
        entry_id="entry-current",
        data={
            CONF_INSTANCE_NAME: "Energy Planner",
            CONF_AI_TASK_ENTITY: "ai_task.old",
        },
        options={},
        subentries={},
    )
    updates: list[dict[str, Any]] = []
    flow = OptionsFlow(entry)
    flow.hass = _valid_hass()
    flow.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [entry],
        async_update_entry=lambda entry_arg, **changes: updates.append(changes),
    )
    energy = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
    }

    result = asyncio.run(
        flow.async_step_init(
            _settings_submission(
                flow,
                input_sections={INPUT_STEP_ENERGY: energy},
            )
        )
    )

    assert result["type"] == "create_entry"
    assert updates == [
        {
                "data": {
                    CONF_INSTANCE_NAME: "Energy Planner",
                    CONF_AI_TASK_ENTITY: "ai_task.old",
                    CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                    CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
                    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
                    CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
                    **energy,
                }
        }
    ]


def test_central_input_settings_prefill_and_validate_sections() -> None:
    entry = SimpleNamespace(
        entry_id="entry-current",
        data={CONF_AMBER_IMPORT_PRICE: "sensor.import_price"},
        options={},
        subentries={},
    )
    flow = OptionsFlow(entry)
    flow.hass = _valid_hass()
    flow.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    form = asyncio.run(flow.async_step_init())
    energy_section = next(
        validator
        for marker, validator in form["data_schema"].schema.items()
        if str(getattr(marker, "schema", marker)) == INPUT_STEP_ENERGY
    )
    fields = {
        str(getattr(marker, "schema", marker)): marker
        for marker in energy_section.schema.schema
    }

    invalid = asyncio.run(
        flow.async_step_init(
            _settings_submission(
                flow,
                input_sections={
                    INPUT_STEP_ENERGY: {
                        CONF_AMBER_IMPORT_PRICE: "input_number.import_price",
                        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
                        CONF_PV_FORECAST: "sensor.pv_forecast",
                        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
                        CONF_BATTERY_SOC: "sensor.battery_soc",
                    }
                },
            )
        )
    )

    assert fields[CONF_AMBER_IMPORT_PRICE].description["suggested_value"] == "sensor.import_price"
    assert invalid["errors"]["base"] == "invalid_entity_domain"


def test_central_settings_reject_cross_section_actuator_collisions() -> None:
    entry = SimpleNamespace(entry_id="entry-current", data={}, options={}, subentries={})
    flow = OptionsFlow(entry)
    flow.hass = _valid_hass()
    flow.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    submission = _settings_submission(
        flow,
        input_sections={
            INPUT_STEP_CLIMATE: {
                CONF_DAIKIN_CLIMATE: "climate.daikin",
                CONF_CLIMATE_ZONES: ["switch.shared_charger"],
                CONF_CLIMATE_TARGET_LOW: "input_number.climate_low",
                CONF_CLIMATE_TARGET_HIGH: "input_number.climate_high",
            },
            INPUT_STEP_EV: {CONF_EV_CHARGER: "switch.shared_charger"},
        },
    )

    result = asyncio.run(flow.async_step_init(submission))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "household_actuator_in_use"}


def test_central_ai_settings_normalize_and_clear_task_configuration() -> None:
    entry = SimpleNamespace(
        entry_id="entry-current",
        data={
            CONF_INSTANCE_NAME: "Energy Planner",
            CONF_AI_TASK_ENTITY: "ai_task.old",
            CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
        },
        options={},
        subentries={},
    )
    updates: list[dict[str, Any]] = []
    flow = OptionsFlow(entry)
    flow.hass = _valid_hass()
    flow.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [entry],
        async_update_entry=lambda entry_arg, **changes: updates.append(changes),
    )
    ai_section = next(
        validator
        for marker, validator in flow._settings_schema().schema.items()
        if str(getattr(marker, "schema", marker)) == INPUT_STEP_AI
    )
    ai_task_marker = next(
        marker
        for marker in ai_section.schema.schema
        if str(getattr(marker, "schema", marker)) == CONF_AI_TASK_ENTITY
    )
    assert ai_task_marker.description["suggested_value"] == "ai_task.old"

    result = asyncio.run(
        flow.async_step_init(
            _settings_submission(
                flow,
                input_sections={
                    INPUT_STEP_AI: {
                        CONF_AI_TASK_ENTITY: " ai_task.extended_openai_ai_task "
                    }
                },
            )
        )
    )
    assert result["type"] == "create_entry"
    assert updates[-1]["data"][CONF_AI_TASK_ENTITY] == "ai_task.extended_openai_ai_task"
    assert updates[-1]["data"][CONF_AI_ADVISOR_SERVICE] == "ai_task.generate_data"

    clear_entry = SimpleNamespace(
        entry_id="entry-current",
        data=updates[-1]["data"],
        options=result["data"],
        subentries={},
    )
    clear_flow = OptionsFlow(clear_entry)
    clear_flow.hass = _valid_hass()
    clear_flow.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [clear_entry],
        async_update_entry=lambda entry_arg, **changes: updates.append(changes),
    )
    asyncio.run(
        clear_flow.async_step_init(
            _settings_submission(
                clear_flow,
                input_sections={INPUT_STEP_AI: {}},
            )
        )
    )
    assert CONF_AI_TASK_ENTITY not in updates[-1]["data"]
    assert updates[-1]["data"][CONF_AI_ADVISOR_SERVICE] == ""


def test_consolidated_settings_sections_are_required_collapsible_groups() -> None:
    flow = OptionsFlow(SimpleNamespace(data={}, options={}))
    schema = flow._settings_schema()
    fields = {
        str(getattr(marker, "schema", marker)): marker
        for marker in schema.schema
    }

    for step_id in (
        INPUT_STEP_ENERGY,
        INPUT_STEP_CLIMATE,
        INPUT_STEP_ENPHASE,
        INPUT_STEP_AI,
        INPUT_STEP_EV,
        POLICY_STEP_PRIORITIES,
    ):
        assert isinstance(fields[step_id], vol.Required)
        assert schema.schema[fields[step_id]].options["collapsed"] is True


def test_ai_subentry_normalizes_task_and_ignores_legacy_agent_selection() -> None:
    assert _normalize_ai_config(
        {
            CONF_AI_TASK_ENTITY: " ai_task.extended_openai_ai_task ",
            "ai_agent_id": " conversation.extended_openai_conversation ",
        }
    ) == {
        CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
        CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
    }
    assert _normalize_ai_config({CONF_AI_TASK_ENTITY: " ", "ai_agent_id": " "}) == {
        CONF_AI_ADVISOR_SERVICE: "",
    }


def test_legacy_subentry_data_groups_into_consolidated_buttons() -> None:
    entry = SimpleNamespace(
        subentries={
            "optimizer": SimpleNamespace(subentry_type="optimizer", data={"haeo_optimize_service": "haeo.optimize"}),
            "prices": SimpleNamespace(
                subentry_type="prices",
                data={
                    CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
                    CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
                },
            ),
            "forecasts": SimpleNamespace(
                subentry_type="forecasts",
                data={
                    CONF_PV_FORECAST: "sensor.pv_forecast",
                    CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
                    CONF_BATTERY_SOC: "sensor.battery_soc",
                },
            ),
            "energy": SimpleNamespace(subentry_type="energy", data={CONF_WEATHER: "weather.home"}),
            "climate": SimpleNamespace(
                subentry_type="climate",
                data={CONF_PERSON_ENTITIES: ["person.james", "person.cath"]},
            ),
            "enphase": SimpleNamespace(
                subentry_type="enphase",
                data={
                    "enphase_arbitrage_profile": "Savings",
                    CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "select.select_option",
                    "ai_advisor_service": "ai_task.generate_data",
                },
            ),
            "advisor": SimpleNamespace(
                subentry_type="advisor",
                data={
                    "ai_advisor_service": "ai_task.generate_data",
                    CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
                },
            ),
        },
    )

    grouped = grouped_subentry_data(entry)

    assert grouped["energy"] == {
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
    }
    assert grouped["climate"] == {CONF_WEATHER: "weather.home"}
    assert grouped["presence"] == {CONF_PERSON_ENTITIES: ["person.james", "person.cath"]}
    assert grouped["enphase"] == {
        CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "select.select_option",
        CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
        CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
    }
    assert grouped["ai"] == {
        "ai_advisor_service": "ai_task.generate_data",
        CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
    }


def test_legacy_load_subentry_maps_only_measured_sensor() -> None:
    measured = SimpleNamespace(
        subentries={
            "forecasts": SimpleNamespace(
                subentry_type="forecasts",
                data={
                    "baseline_load_forecast_entity": "sensor.prediction",
                    "baseline_load_observed_entity": "sensor.whole_home_power",
                },
            )
        }
    )
    forecast_only = SimpleNamespace(
        subentries={
            "forecasts": SimpleNamespace(
                subentry_type="forecasts",
                data={"baseline_load_forecast_entity": "sensor.prediction"},
            )
        }
    )

    assert grouped_subentry_data(measured)["energy"][CONF_HOUSEHOLD_LOAD] == "sensor.whole_home_power"
    assert "household_load_entity" not in grouped_subentry_data(forecast_only).get("energy", {})


def test_migration_moves_all_subentry_settings_into_main_entry() -> None:
    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    entry = SimpleNamespace(
        data={CONF_INSTANCE_NAME: "Energy Planner"},
        subentries={
            "energy": SimpleNamespace(
                subentry_id="energy",
                subentry_type="energy",
                data={CONF_WEATHER: "weather.home"},
            ),
        },
    )

    assert async_migrate_subentries_to_entry_data(hass, entry) is True

    assert hass.config_entries.entry_updates == [
        (
            entry,
            {
                "data": {
                    CONF_INSTANCE_NAME: "Energy Planner",
                    CONF_WEATHER: "weather.home",
                }
            },
        )
    ]
    assert hass.config_entries.removed == ["energy"]


def test_migration_removes_retired_config_from_entry_without_subentries() -> None:
    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    entry = SimpleNamespace(
        data={
            CONF_INSTANCE_NAME: "Energy Planner",
            "haeo_optimize_service": "haeo.optimize",
            "haeo_config_entry_id": "optimizer-entry",
            "haeo_entry_id": "legacy-optimizer-entry",
        },
        subentries={},
    )

    assert async_migrate_subentries_to_entry_data(hass, entry) is True
    assert hass.config_entries.entry_updates == [
        (entry, {"data": {CONF_INSTANCE_NAME: "Energy Planner"}})
    ]


def test_validate_config_accepts_compatible_units() -> None:
    hass = FakeHass(
        _valid_hass().states.entity_ids,
        _valid_hass().services.services,
        {
            "sensor.import_price": {"unit_of_measurement": "AUD/kWh"},
            "sensor.export_price": {"unit_of_measurement": "c/kWh"},
            "sensor.pv_forecast": {"unit_of_measurement": "W"},
            "sensor.baseline_load": {"unit_of_measurement": "kW"},
            "sensor.battery_soc": {"unit_of_measurement": "%"},
        },
    )

    assert _validate_config(hass, _valid_input()) == {}


def test_validate_config_accepts_solcast_energy_units_for_pv_forecast() -> None:
    hass = FakeHass(
        _valid_hass().states.entity_ids,
        _valid_hass().services.services,
        {
            "sensor.pv_forecast": {"unit_of_measurement": "kWh", "device_class": "energy"},
        },
    )

    assert _validate_config(hass, _valid_input()) == {}


def test_validate_config_accepts_optional_carbon_intensity_sensor() -> None:
    hass = FakeHass(
        _valid_hass().states.entity_ids | {"sensor.carbon_intensity"},
        _valid_hass().services.services,
        {"sensor.carbon_intensity": {"unit_of_measurement": "gCO₂/kWh"}},
    )

    assert (
        _validate_config(
            hass,
            _valid_input({CONF_CARBON_INTENSITY_FORECAST: "sensor.carbon_intensity"}),
        )
        == {}
    )


def test_validate_config_rejects_invalid_carbon_intensity_unit() -> None:
    hass = FakeHass(
        _valid_hass().states.entity_ids | {"sensor.carbon_intensity"},
        _valid_hass().services.services,
        {"sensor.carbon_intensity": {"unit_of_measurement": "kW"}},
    )

    errors = _validate_config(
        hass,
        _valid_input({CONF_CARBON_INTENSITY_FORECAST: "sensor.carbon_intensity"}),
    )

    assert errors[CONF_CARBON_INTENSITY_FORECAST] == "invalid_unit"


def test_validate_config_rejects_incompatible_sensor_unit() -> None:
    hass = FakeHass(
        _valid_hass().states.entity_ids,
        _valid_hass().services.services,
        {"sensor.battery_soc": {"unit_of_measurement": "kWh"}},
    )

    errors = _validate_config(hass, _valid_input())

    assert errors[CONF_BATTERY_SOC] == "invalid_unit"


def test_validate_config_rejects_wrong_entity_domain() -> None:
    errors = _validate_config(
        _valid_hass(),
        _valid_input({CONF_AMBER_IMPORT_PRICE: "input_number.import_price"}),
    )

    assert errors[CONF_AMBER_IMPORT_PRICE] == "invalid_entity_domain"


def test_validate_config_rejects_empty_ai_service_parts() -> None:
    assert _validate_config(
        _valid_hass(),
        _valid_input({CONF_AI_ADVISOR_SERVICE: ".generate_data"}),
    )[CONF_AI_ADVISOR_SERVICE] == "invalid_service_name"
    assert _validate_config(
        _valid_hass(),
        _valid_input({CONF_AI_ADVISOR_SERVICE: "ai_task."}),
    )[CONF_AI_ADVISOR_SERVICE] == "invalid_service_name"


def test_validate_config_rejects_invalid_or_missing_entities() -> None:
    invalid = _validate_config(_valid_hass(), _valid_input({CONF_AMBER_IMPORT_PRICE: "not an entity"}))
    missing = _validate_config(_valid_hass(), _valid_input({CONF_AMBER_IMPORT_PRICE: "sensor.missing"}))

    assert invalid[CONF_AMBER_IMPORT_PRICE] == "invalid_entity_id"
    assert missing[CONF_AMBER_IMPORT_PRICE] == "entity_not_found"


def test_validate_config_ignores_units_for_fields_without_unit_rules() -> None:
    hass = FakeHass(
        {"person.james"},
        set(),
        {"person.james": {"unit_of_measurement": "kWh"}},
    )

    assert _validate_config(hass, {CONF_PERSON_ENTITIES: "person.james"}) == {}


def test_entity_values_and_ready_by_helpers_handle_edge_cases() -> None:
    assert _entity_values(None) == []
    assert _entity_values("sensor.one, sensor.two") == ["sensor.one", "sensor.two"]
    assert _entity_values(["sensor.one", "", 7]) == ["sensor.one", "7"]
    assert _entity_values(7) == []
    assert _ready_by_valid("07:30:15") is True
    assert _ready_by_valid("07") is False
    assert _ready_by_valid("aa:bb") is False


def test_validate_options_accepts_default_policy_values() -> None:
    assert _validate_options(dict(DEFAULT_OPTIONS)) == {}


def test_default_options_require_intentional_active_mode_enablement() -> None:
    assert DEFAULT_OPTIONS["planner_enabled"] is False
    assert DEFAULT_OPTIONS["dry_run"] is True
    assert DEFAULT_OPTIONS[CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED] is True


def test_validate_options_rejects_ev_fallback_outside_soc_bounds() -> None:
    errors = _validate_options(
        {
            **DEFAULT_OPTIONS,
            CONF_EV_MIN_SOC_PERCENT: 50,
            CONF_EV_MAX_SOC_PERCENT: 80,
            CONF_EV_FALLBACK_TARGET_SOC_PERCENT: 90,
        }
    )

    assert errors[CONF_EV_FALLBACK_TARGET_SOC_PERCENT] == "ev_fallback_outside_bounds"


def test_validate_options_rejects_invalid_default_ready_by() -> None:
    errors = _validate_options({**DEFAULT_OPTIONS, CONF_DEFAULT_READY_BY: "24:90"})

    assert errors[CONF_DEFAULT_READY_BY] == "invalid_ready_by"


def test_validate_options_rejects_invalid_ev_earliest_start() -> None:
    errors = _validate_options({**DEFAULT_OPTIONS, CONF_EV_EARLIEST_START: "25:00"})

    assert errors[CONF_EV_EARLIEST_START] == "invalid_ready_by"


def test_validate_options_rejects_invalid_priority_weights() -> None:
    errors = _validate_options(
        {
            **DEFAULT_OPTIONS,
            "planning_priority_1": "cost",
            "planning_priority_2": "comfort",
            "planning_priority_3": "cost",
            "planning_priority_4": "battery_reserve",
            "planning_priority_5": "solar_self_consumption",
            "planning_priority_6": "carbon",
        }
    )

    assert errors["base"] == "invalid_priority_weights"


def _strings() -> dict[str, Any]:
    path = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner" / "strings.json"
    return json.loads(path.read_text(encoding="utf-8"))
