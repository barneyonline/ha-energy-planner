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
    PLANNER_SUBENTRY_SCHEMAS,
    POLICY_STEP_AI_SAFETY,
    POLICY_STEP_CLIMATE,
    POLICY_STEP_DATA_HEALTH,
    POLICY_STEP_ENPHASE,
    POLICY_STEP_EV_BATTERY_GRID,
    POLICY_STEP_PRIORITIES,
    POLICY_STEP_SCHEDULE,
    STEP_USER_DATA_SCHEMA,
    AISubentryFlow,
    ClimateSubentryFlow,
    ConfigFlow,
    EnergySubentryFlow,
    EnphaseSubentryFlow,
    EVSubentryFlow,
    OptionsFlow,
    _enphase_profile_options,
    _entity_values,
    _form_suggested_values,
    _normalize_ai_config,
    _normalize_options_input,
    _options_schema,
    _ready_by_valid,
    _validate_config,
    _validate_options,
)
from custom_components.ha_energy_planner.const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_TASK_ENTITY,
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BASELINE_LOAD_OBSERVED,
    CONF_BATTERY_SOC,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_DEFAULT_READY_BY,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_EARLIEST_START,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HAEO_OPTIMIZE_SERVICE,
    CONF_PERSON_ENTITIES,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
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
    async_consolidate_subentries,
    grouped_subentry_data,
)


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

    def async_add_subentry(self, entry: Any, subentry: Any) -> bool:
        self.added.append(subentry)
        return True

    def async_remove_subentry(self, entry: Any, subentry_id: str) -> bool:
        self.removed.append(subentry_id)
        return True

    def async_update_subentry(self, entry: Any, subentry: Any, **changes: Any) -> bool:
        self.updated.append((subentry, changes))
        return True


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
            "select.enphase_profile",
            "ai_task.extended_openai_ai_task",
        },
        {
            ("haeo", "optimize"),
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


def test_validate_config_accepts_available_entities_and_services() -> None:
    assert _validate_config(_valid_hass(), _valid_input()) == {}


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


def test_initial_config_schema_requires_no_inputs() -> None:
    assert STEP_USER_DATA_SCHEMA.schema == {}


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
    load_observed_filter = schema_fields[CONF_BASELINE_LOAD_OBSERVED].serialize()["selector"]["entity"]["filter"][0]
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
    assert load_observed_filter["unit_of_measurement"] == ["W", "kW", "MW"]
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


def test_haeo_service_is_not_user_configurable_in_energy_group() -> None:
    energy_schema = PLANNER_SUBENTRY_SCHEMAS["energy"]
    energy_fields = {getattr(key, "schema", key) for key in energy_schema.schema}

    assert CONF_HAEO_OPTIMIZE_SERVICE not in energy_fields


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


def test_enphase_flow_uses_profile_entity_options_for_profile_roles() -> None:
    entry = SimpleNamespace(subentries={})
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=entry)
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)
    flow.async_create_entry = Mock(return_value={"type": "create_entry"})

    profile_step = asyncio.run(flow.async_step_user({CONF_ENPHASE_PROFILE: "select.enphase_profile"}))

    assert profile_step["type"] == "form"
    assert profile_step["step_id"] == "profiles"
    profile_fields = {
        getattr(key, "schema", key): selector for key, selector in profile_step["data_schema"].schema.items()
    }
    restore_selector = profile_fields[CONF_ENPHASE_AI_PROFILE].serialize()["selector"]["select"]
    assert restore_selector["options"][:3] == ["Self-Consumption", "AI Optimisation", "Full Backup"]

    result = asyncio.run(
        flow.async_step_profiles(
            {
                CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
                CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
                CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
            }
        )
    )

    assert result == {"type": "create_entry"}
    flow.async_create_entry.assert_called_once_with(
        title="Enphase",
        data={
            CONF_ENPHASE_PROFILE: "select.enphase_profile",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
            CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
            CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
        },
    )


def test_enphase_reconfigure_opens_profile_role_selection_when_entity_exists() -> None:
    existing = SimpleNamespace(
        subentry_type="enphase",
        data={
            CONF_ENPHASE_PROFILE: "select.enphase_profile",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
            CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
            CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
        },
    )
    entry = SimpleNamespace(subentries={"enphase": existing})
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=entry)
    flow._get_reconfigure_subentry = Mock(return_value=existing)

    result = asyncio.run(flow.async_step_reconfigure())

    assert result["type"] == "form"
    assert result["step_id"] == "profiles"
    profile_fields = {getattr(key, "schema", key): selector for key, selector in result["data_schema"].schema.items()}
    assert set(profile_fields) == {
        CONF_ENPHASE_AI_PROFILE,
        CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
        CONF_ENPHASE_FULL_BACKUP_PROFILE,
    }
    restore_selector = profile_fields[CONF_ENPHASE_AI_PROFILE].serialize()["selector"]["select"]
    assert restore_selector["options"][:3] == ["Self-Consumption", "AI Optimisation", "Full Backup"]


def test_enphase_reconfigure_without_existing_profile_opens_entity_form() -> None:
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(flow.async_step_reconfigure())

    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_enphase_profile_entity_form_prefills_current_selection() -> None:
    existing = SimpleNamespace(
        subentry_type="enphase",
        data={CONF_ENPHASE_PROFILE: "select.enphase_profile"},
    )
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={"enphase": existing}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(flow._async_step_profile_entity(None))

    assert result["type"] == "form"
    assert result["step_id"] == "user"


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


def test_ev_target_soc_and_ready_by_are_native_not_external_helpers() -> None:
    ev_schema = PLANNER_SUBENTRY_SCHEMAS["ev"]
    schema_fields = {getattr(key, "schema", key): selector for key, selector in ev_schema.schema.items()}

    assert CONF_EV_SMART_CHARGING_TARGET_SOC not in schema_fields
    assert CONF_EV_SMART_CHARGING_READY_BY not in schema_fields


def test_validate_config_accepts_input_button_ev_controls() -> None:
    hass = FakeHass(
        {"input_button.ev_start", "input_button.ev_stop"},
        {("haeo", "optimize"), ("select", "select_option")},
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
        {("haeo", "optimize"), ("select", "select_option")},
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


def test_subentry_fields_have_readable_translation_labels() -> None:
    strings = _strings()

    for subentry_type, schema in PLANNER_SUBENTRY_SCHEMAS.items():
        subentry_strings = strings["config_subentries"][subentry_type]
        labels = subentry_strings["step"]["user"]["data"]
        descriptions = subentry_strings["step"]["user"]["data_description"]
        schema_keys = {str(getattr(key, "schema", key)) for key in schema.schema}

        assert subentry_strings["flow_title"]
        assert subentry_strings["entry_type"]
        assert subentry_strings["initiate_flow"]["user"]
        assert subentry_strings["initiate_flow"]["reconfigure"]
        assert schema_keys <= labels.keys()
        assert schema_keys <= descriptions.keys()
        for key in schema_keys:
            assert labels[key] != key
            assert "_" not in labels[key]


def test_english_locale_files_include_subentry_button_labels() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"
    expected_subentry_types = set(PLANNER_SUBENTRY_SCHEMAS)

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        subentries = translations["config_subentries"]

        assert expected_subentry_types <= subentries.keys()
        for subentry_type in expected_subentry_types:
            initiate_flow = subentries[subentry_type]["initiate_flow"]
            assert initiate_flow["user"], f"{translations_path.name} missing {subentry_type} user label"
            assert initiate_flow["reconfigure"], f"{translations_path.name} missing {subentry_type} reconfigure label"


def test_english_locale_files_translate_reconfigure_success() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))

        assert translations["config"]["abort"]["reconfigure_successful"] == "Reconfigure Successful"
        assert translations["config_subentries"]["ai"]["abort"]["reconfigure_successful"] == "Reconfigure Successful"


def test_english_locale_files_explain_solcast_pv_forecast_sensor() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        description = translations["config_subentries"]["energy"]["step"]["user"]["data_description"][CONF_PV_FORECAST]

        assert "Forecast Today" in description
        assert "Peak Forecast Today" in description
        assert "detailedForecast" in description


def test_english_locale_files_explain_bom_hourly_weather_forecast() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        description = translations["config_subentries"]["climate"]["step"]["user"]["data_description"][CONF_WEATHER]

        assert "Bureau of Meteorology" in description
        assert "Hourly" in description
        assert "temperature forecast" in description


def test_english_locale_files_label_ev_charge_rate_as_kw() -> None:
    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"

    for translations_path in (integration_dir / "translations").glob("en*.json"):
        translations = json.loads(translations_path.read_text(encoding="utf-8"))
        label = translations["options"]["step"]["ev_battery_grid"]["data"][CONF_EV_CHARGE_RATE_KW]

        assert label == "EV charge rate (kW)"


def test_options_flow_fields_have_readable_translation_labels() -> None:
    strings = _strings()
    labels: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for step in strings["options"]["step"].values():
        labels.update(step.get("data", {}))
        descriptions.update(step.get("data_description", {}))
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
        "Shows persistent notifications when required inputs are unsafe, a grid limit is exceeded, or HAEO falls "
        "back. Turning this off dismisses those notifications without weakening safety checks."
    )

    integration_dir = Path(__file__).parents[1] / "custom_components" / "ha_energy_planner"
    for path in (
        integration_dir / "strings.json",
        integration_dir / "translations" / "en.json",
        integration_dir / "translations" / "en-AU.json",
        integration_dir / "translations" / "en-GB.json",
    ):
        strings = json.loads(path.read_text(encoding="utf-8"))
        section = strings["options"]["step"][POLICY_STEP_AI_SAFETY]
        assert section["data"][CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED] == expected_heading
        assert section["data_description"][CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED] == expected_description


def test_options_flow_init_shows_policy_section_menu() -> None:
    flow = OptionsFlow(SimpleNamespace(options={}))

    result = asyncio.run(flow.async_step_init())

    assert result["type"] == "menu"
    assert tuple(result["menu_options"]) == (
        POLICY_STEP_SCHEDULE,
        POLICY_STEP_EV_BATTERY_GRID,
        POLICY_STEP_CLIMATE,
        POLICY_STEP_ENPHASE,
        POLICY_STEP_AI_SAFETY,
        POLICY_STEP_DATA_HEALTH,
        POLICY_STEP_PRIORITIES,
    )


def test_config_flow_user_step_creates_entry_after_confirmation() -> None:
    flow = ConfigFlow.__new__(ConfigFlow)
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow.async_create_entry = Mock(return_value={"type": "create_entry"})
    flow.async_show_form = Mock(return_value={"type": "form"})

    form = asyncio.run(flow.async_step_user())
    created = asyncio.run(flow.async_step_user({}))

    assert form == {"type": "form"}
    assert created == {"type": "create_entry"}
    flow.async_set_unique_id.assert_awaited_once_with("ha_energy_planner")
    flow._abort_if_unique_id_configured.assert_called_once_with()


def test_config_flow_reports_options_and_subentry_flow_types() -> None:
    options_flow = ConfigFlow.async_get_options_flow(SimpleNamespace(options={}))
    supported = ConfigFlow.async_get_supported_subentry_types(SimpleNamespace())

    assert isinstance(options_flow, OptionsFlow)
    assert supported["energy"] is EnergySubentryFlow
    assert supported["climate"] is ClimateSubentryFlow
    assert supported["ev"] is EVSubentryFlow


def test_options_flow_all_policy_section_steps_show_forms() -> None:
    flow = OptionsFlow(SimpleNamespace(options={}))

    for method_name, step_id in [
        ("async_step_ev_battery_grid", POLICY_STEP_EV_BATTERY_GRID),
        ("async_step_climate", POLICY_STEP_CLIMATE),
        ("async_step_enphase", POLICY_STEP_ENPHASE),
        ("async_step_ai_safety", POLICY_STEP_AI_SAFETY),
        ("async_step_data_health", POLICY_STEP_DATA_HEALTH),
        ("async_step_priorities", POLICY_STEP_PRIORITIES),
    ]:
        result = asyncio.run(getattr(flow, method_name)())
        assert result["type"] == "form"
        assert result["step_id"] == step_id


def test_options_flow_section_update_preserves_other_options() -> None:
    updates: list[dict[str, Any]] = []
    flow = OptionsFlow(
        SimpleNamespace(
            options={
                CONF_EV_MIN_SOC_PERCENT: 55,
                CONF_PRIORITY_WEIGHTS: "comfort,cost,ev_readiness,battery_reserve,solar_self_consumption,carbon",
            }
        )
    )
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda entry, **kwargs: updates.append(kwargs),
        )
    )

    result = asyncio.run(
        flow.async_step_schedule(
            {
                CONF_PLANNING_HORIZON_HOURS: 36,
                CONF_PLANNING_INTERVAL_MINUTES: 10,
                CONF_DEFAULT_READY_BY: "08:30",
            }
        )
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert updates == [{"options": flow._options}]
    assert flow._options[CONF_PLANNING_HORIZON_HOURS] == 36
    assert flow._options[CONF_EV_MIN_SOC_PERCENT] == 55
    assert flow._options[CONF_PRIORITY_WEIGHTS] == (
        "comfort,cost,ev_readiness,battery_reserve,solar_self_consumption,carbon"
    )


def test_options_flow_uses_saved_values_after_returning_to_menu() -> None:
    flow = OptionsFlow(SimpleNamespace(options={CONF_EV_MIN_SOC_PERCENT: 55}))

    asyncio.run(
        flow.async_step_schedule(
            {
                CONF_PLANNING_HORIZON_HOURS: 36,
                CONF_PLANNING_INTERVAL_MINUTES: 10,
                CONF_DEFAULT_READY_BY: "08:30",
            }
        )
    )

    result = asyncio.run(flow.async_step_schedule())
    schema_fields = {str(getattr(key, "schema", key)): key for key in result["data_schema"].schema}

    assert result["type"] == "form"
    assert schema_fields[CONF_PLANNING_HORIZON_HOURS].default() == 36


def test_options_flow_section_validation_returns_form_errors() -> None:
    flow = OptionsFlow(SimpleNamespace(options={}))

    result = asyncio.run(
        flow.async_step_ev_battery_grid(
            {
                CONF_EV_MIN_SOC_PERCENT: 95,
                CONF_EV_MAX_SOC_PERCENT: 80,
                CONF_EV_FALLBACK_TARGET_SOC_PERCENT: 90,
            }
        )
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "ev_min_above_max"


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
        data={CONF_HAEO_OPTIMIZE_SERVICE: "haeo.optimize", CONF_AMBER_IMPORT_PRICE: "sensor.old_import"},
        subentries={
            "prices": SimpleNamespace(
                data={
                    CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
                    CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
                },
            ),
            "forecasts": SimpleNamespace(
                data={
                    CONF_PV_FORECAST: "sensor.pv_forecast",
                    CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
                    CONF_BATTERY_SOC: "sensor.battery_soc",
                },
            ),
        },
    )

    assert combined_entry_data(entry) == {
        CONF_HAEO_OPTIMIZE_SERVICE: "haeo.optimize",
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
    }


def test_subentry_user_step_updates_existing_group_instead_of_creating_duplicate() -> None:
    existing = SimpleNamespace(
        subentry_type="energy",
        data={
            CONF_AMBER_IMPORT_PRICE: "sensor.old_import",
            CONF_AMBER_EXPORT_PRICE: "sensor.old_export",
            CONF_PV_FORECAST: "sensor.old_pv_forecast",
            CONF_BASELINE_LOAD_FORECAST: "sensor.old_baseline_load",
            CONF_BATTERY_SOC: "sensor.old_battery_soc",
        },
    )
    entry = SimpleNamespace(subentries={"energy": existing})
    flow = EnergySubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=entry)
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)
    flow.async_update_and_abort = Mock(return_value={"type": "abort", "reason": "reconfigure_successful"})
    user_input = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
    }

    result = asyncio.run(flow.async_step_user(user_input))

    assert result == {"type": "abort", "reason": "reconfigure_successful"}
    flow.async_update_and_abort.assert_called_once_with(
        entry,
        existing,
        title="Energy",
        data=user_input,
    )


def test_subentry_user_step_prefills_existing_group_when_opened() -> None:
    existing = SimpleNamespace(
        subentry_type="energy",
        data={
            CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
            CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
            CONF_PV_FORECAST: "sensor.pv_forecast",
            CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
            CONF_BATTERY_SOC: "sensor.battery_soc",
        },
    )
    flow = EnergySubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={"energy": existing}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(flow.async_step_user())

    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_subentry_user_step_returns_errors_for_invalid_input() -> None:
    flow = EnergySubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(
        flow.async_step_user(
            {
                CONF_AMBER_IMPORT_PRICE: "input_number.import_price",
                CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
                CONF_PV_FORECAST: "sensor.pv_forecast",
                CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
                CONF_BATTERY_SOC: "sensor.battery_soc",
            }
        )
    )

    assert result["type"] == "form"
    assert result["errors"][CONF_AMBER_IMPORT_PRICE] == "invalid_entity_domain"


def test_subentry_reconfigure_step_updates_active_subentry() -> None:
    existing = SimpleNamespace(
        subentry_type="energy",
        data={
            CONF_AMBER_IMPORT_PRICE: "sensor.old_import",
            CONF_AMBER_EXPORT_PRICE: "sensor.old_export",
            CONF_PV_FORECAST: "sensor.old_pv_forecast",
            CONF_BASELINE_LOAD_FORECAST: "sensor.old_baseline_load",
            CONF_BATTERY_SOC: "sensor.old_battery_soc",
        },
    )
    entry = SimpleNamespace(subentries={"energy": existing})
    flow = EnergySubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=entry)
    flow._get_reconfigure_subentry = Mock(return_value=existing)
    flow.async_update_and_abort = Mock(return_value={"type": "abort", "reason": "reconfigure_successful"})
    user_input = {
        CONF_AMBER_IMPORT_PRICE: "sensor.import_price",
        CONF_AMBER_EXPORT_PRICE: "sensor.export_price",
        CONF_PV_FORECAST: "sensor.pv_forecast",
        CONF_BASELINE_LOAD_FORECAST: "sensor.baseline_load",
        CONF_BATTERY_SOC: "sensor.battery_soc",
    }

    result = asyncio.run(flow.async_step_reconfigure(user_input))

    assert result == {"type": "abort", "reason": "reconfigure_successful"}
    flow.async_update_and_abort.assert_called_once_with(
        entry,
        existing,
        title="Energy",
        data=user_input,
    )


def test_enphase_user_step_with_existing_profile_opens_profiles() -> None:
    existing = SimpleNamespace(
        subentry_type="enphase",
        data={CONF_ENPHASE_PROFILE: "select.enphase_profile"},
    )
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={"enphase": existing}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(flow.async_step_user())

    assert result["type"] == "form"
    assert result["step_id"] == "profiles"


def test_enphase_profiles_step_without_profile_returns_entity_form() -> None:
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(flow.async_step_profiles())

    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_enphase_profiles_step_updates_existing_subentry() -> None:
    existing = SimpleNamespace(
        subentry_type="enphase",
        data={CONF_ENPHASE_PROFILE: "select.enphase_profile"},
    )
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={"enphase": existing}))
    flow._get_reconfigure_subentry = Mock(return_value=existing)
    flow.async_update_and_abort = Mock(return_value={"type": "abort"})

    result = asyncio.run(
        flow.async_step_profiles(
            {
                CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
                CONF_ENPHASE_SELF_CONSUMPTION_PROFILE: "Self-Consumption",
                CONF_ENPHASE_FULL_BACKUP_PROFILE: "Full Backup",
            }
        )
    )

    assert result == {"type": "abort"}
    flow.async_update_and_abort.assert_called_once()


def test_enphase_profile_entity_step_returns_validation_errors() -> None:
    flow = EnphaseSubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=SimpleNamespace(subentries={}))
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)

    result = asyncio.run(flow.async_step_user({CONF_ENPHASE_PROFILE: "sensor.not_select"}))

    assert result["type"] == "form"
    assert result["errors"][CONF_ENPHASE_PROFILE] == "invalid_entity_domain"


def test_ai_subentry_stores_selected_ai_task_entity_with_generate_data_service() -> None:
    entry = SimpleNamespace(subentries={})
    flow = AISubentryFlow()
    flow.hass = _valid_hass()
    flow._get_entry = Mock(return_value=entry)
    flow._get_reconfigure_subentry = Mock(side_effect=ValueError)
    flow.async_create_entry = Mock(return_value={"type": "create_entry"})

    result = asyncio.run(flow.async_step_user({CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task"}))

    assert result == {"type": "create_entry"}
    flow.async_create_entry.assert_called_once_with(
        title="AI",
        data={
            CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
        },
    )


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
            "optimizer": SimpleNamespace(subentry_type="optimizer", data={CONF_HAEO_OPTIMIZE_SERVICE: "haeo.optimize"}),
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
        CONF_HAEO_OPTIMIZE_SERVICE: "haeo.optimize",
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


def test_migration_removes_source_group_when_all_fields_moved() -> None:
    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    entry = SimpleNamespace(
        subentries={
            "energy": SimpleNamespace(
                subentry_id="energy",
                subentry_type="energy",
                data={CONF_WEATHER: "weather.home"},
            ),
        },
    )

    assert async_consolidate_subentries(hass, entry) is True

    assert [subentry.subentry_type for subentry in hass.config_entries.added] == ["system", "climate"]
    assert hass.config_entries.removed == ["energy"]


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


def test_validate_config_rejects_missing_service() -> None:
    errors = _validate_config(
        FakeHass(_valid_hass().states.entity_ids, {("select", "select_option")}),
        _valid_input({CONF_HAEO_OPTIMIZE_SERVICE: "haeo.optimize"}),
    )

    assert errors[CONF_HAEO_OPTIMIZE_SERVICE] == "service_not_found"


def test_validate_config_rejects_invalid_service_name() -> None:
    errors = _validate_config(
        _valid_hass(),
        _valid_input({CONF_HAEO_OPTIMIZE_SERVICE: "haeo"}),
    )

    assert errors[CONF_HAEO_OPTIMIZE_SERVICE] == "invalid_service_name"


def test_enphase_profile_options_are_empty_when_entity_missing() -> None:
    assert _enphase_profile_options(FakeHass(set(), set()), "select.missing") == []


def test_validate_config_rejects_empty_service_domain_or_service() -> None:
    assert (
        _validate_config(_valid_hass(), _valid_input({CONF_HAEO_OPTIMIZE_SERVICE: ".optimize"}))[
            CONF_HAEO_OPTIMIZE_SERVICE
        ]
        == "invalid_service_name"
    )
    assert (
        _validate_config(_valid_hass(), _valid_input({CONF_HAEO_OPTIMIZE_SERVICE: "haeo."}))[CONF_HAEO_OPTIMIZE_SERVICE]
        == "invalid_service_name"
    )


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
