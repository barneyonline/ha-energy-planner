"""Config flow for Energy Planner."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
    UnknownSubEntry,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from voluptuous import Invalid

from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_ENABLED,
    CONF_AI_TASK_ENTITY,
    CONF_AI_TIMEOUT_SECONDS,
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BASELINE_LOAD_OBSERVED,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC,
    CONF_BATTERY_USABLE_CAPACITY_KWH,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_COMMAND_RATE_LIMIT_SECONDS,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_MIN_SAVINGS,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_EV_SOC_PER_KWH,
    CONF_FORECAST_FRESHNESS_MINUTES,
    CONF_GRID_EXPORT_LIMIT_KW,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_HAEO_OPTIMIZE_SERVICE,
    CONF_HVAC_MIN_CYCLE_MINUTES,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
    CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA,
    CONF_MANUAL_HVAC_OVERRIDE_MINUTES,
    CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT,
    CONF_MAX_DAILY_CLIMATE_ACTIONS,
    CONF_MAX_DAILY_ENPHASE_ACTIONS,
    CONF_MAX_DAILY_EV_ACTIONS,
    CONF_MIN_CLIMATE_CONFIDENCE,
    CONF_MIN_ENPHASE_CONFIDENCE,
    CONF_MIN_EV_CONFIDENCE,
    CONF_MIN_LOAD_CONFIDENCE,
    CONF_MIN_SOLAR_CONFIDENCE,
    CONF_MIN_TARIFF_CONFIDENCE,
    CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT,
    CONF_PERSON_ENTITIES,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PRICE_FRESHNESS_MINUTES,
    CONF_PRIORITY_WEIGHTS,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_SECONDARY,
    CONF_PV_OBSERVED,
    CONF_WEATHER,
    DEFAULT_ENPHASE_AI_PROFILE,
    DEFAULT_ENPHASE_FULL_BACKUP_PROFILE,
    DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE,
    DEFAULT_OPTIONS,
    DOMAIN,
    INTEGRATION_NAME,
)

SUBENTRY_ENERGY = "energy"
SUBENTRY_CLIMATE = "climate"
SUBENTRY_PRESENCE = "presence"
SUBENTRY_ENPHASE = "enphase"
SUBENTRY_AI = "ai"
SUBENTRY_EV = "ev"

_ALLOWED_PRIORITY_WEIGHTS = {
    "cost",
    "comfort",
    "ev_readiness",
    "battery_reserve",
    "solar_self_consumption",
    "carbon",
}
_PRIORITY_OBJECTIVES = (
    "cost",
    "comfort",
    "ev_readiness",
    "battery_reserve",
    "solar_self_consumption",
    "carbon",
)
_PRIORITY_LABELS = {
    "cost": "Cost",
    "comfort": "Comfort",
    "ev_readiness": "EV readiness",
    "battery_reserve": "Battery reserve",
    "solar_self_consumption": "Solar self-consumption",
    "carbon": "Carbon",
}
_PRIORITY_FORM_FIELDS = tuple(f"planning_priority_{index}" for index in range(1, len(_PRIORITY_OBJECTIVES) + 1))

_PRICE_SENSOR_UNITS = ("$/kWh", "AUD/kWh", "A$/kWh", "c/kWh", "¢/kWh", "cent/kWh", "cents/kWh")
_POWER_SENSOR_UNITS = ("W", "kW", "MW")
_FORECAST_SENSOR_UNITS = (*_POWER_SENSOR_UNITS, "Wh", "kWh", "MWh")
_PERCENT_SENSOR_UNITS = ("%", "percent", "percentage")
_CARBON_INTENSITY_SENSOR_UNITS = (
    "gCO2/kWh",
    "gCO₂/kWh",
    "kgCO2/kWh",
    "kgCO₂/kWh",
)
_EV_TARGET_SOC_FILTER = [
    {"domain": ["number", "input_number", "select", "input_select"]},
    {"domain": "sensor", "device_class": "battery", "unit_of_measurement": list(_PERCENT_SENSOR_UNITS)},
    {"domain": "sensor", "unit_of_measurement": list(_PERCENT_SENSOR_UNITS)},
]


def _sensor_filter(units: tuple[str, ...]) -> dict[str, Any]:
    """Return a selector filter for sensors that expose one of the expected units."""
    return {"domain": "sensor", "unit_of_measurement": list(units)}


def _entity_selector(
    domain: str | list[str] | None = None,
    *,
    multiple: bool = False,
    entity_filter: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> EntitySelector:
    config = EntitySelectorConfig(multiple=multiple)
    if entity_filter is not None:
        config["filter"] = entity_filter
    elif domain is not None:
        config["domain"] = domain
    return EntitySelector(config)


STEP_USER_DATA_SCHEMA = vol.Schema({})

ENERGY_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AMBER_IMPORT_PRICE): _entity_selector(entity_filter=_sensor_filter(_PRICE_SENSOR_UNITS)),
        vol.Required(CONF_AMBER_EXPORT_PRICE): _entity_selector(entity_filter=_sensor_filter(_PRICE_SENSOR_UNITS)),
        vol.Required(CONF_PV_FORECAST): _entity_selector(entity_filter=_sensor_filter(_FORECAST_SENSOR_UNITS)),
        vol.Optional(CONF_PV_FORECAST_SECONDARY): _entity_selector(
            entity_filter=_sensor_filter(_FORECAST_SENSOR_UNITS)
        ),
        vol.Required(CONF_BASELINE_LOAD_FORECAST): _entity_selector(entity_filter=_sensor_filter(_POWER_SENSOR_UNITS)),
        vol.Optional(CONF_CARBON_INTENSITY_FORECAST): _entity_selector(
            entity_filter=_sensor_filter(_CARBON_INTENSITY_SENSOR_UNITS)
        ),
        vol.Optional(CONF_PV_OBSERVED): _entity_selector(entity_filter=_sensor_filter(_POWER_SENSOR_UNITS)),
        vol.Optional(CONF_BASELINE_LOAD_OBSERVED): _entity_selector(entity_filter=_sensor_filter(_POWER_SENSOR_UNITS)),
        vol.Required(CONF_BATTERY_SOC): _entity_selector(entity_filter=_sensor_filter(_PERCENT_SENSOR_UNITS)),
    }
)

ENPHASE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENPHASE_PROFILE): _entity_selector(["select", "input_select"]),
        vol.Optional(CONF_ENPHASE_AI_PROFILE, default=DEFAULT_ENPHASE_AI_PROFILE): TextSelector(),
        vol.Optional(
            CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
            default=DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE,
        ): TextSelector(),
        vol.Optional(CONF_ENPHASE_FULL_BACKUP_PROFILE, default=DEFAULT_ENPHASE_FULL_BACKUP_PROFILE): TextSelector(),
    }
)

ENPHASE_ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENPHASE_PROFILE): _entity_selector(["select", "input_select"]),
    }
)

AI_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_AI_TASK_ENTITY): _entity_selector("ai_task"),
    }
)

CLIMATE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DAIKIN_CLIMATE): _entity_selector("climate"),
        vol.Optional(CONF_DAIKIN_POWER): _entity_selector(entity_filter=_sensor_filter(_POWER_SENSOR_UNITS)),
        vol.Optional(CONF_WEATHER): _entity_selector("weather"),
        vol.Optional(CONF_CLIMATE_AUTOMATIONS): _entity_selector("automation", multiple=True),
        vol.Optional(CONF_CLIMATE_CHANGE_FROM_SCHEDULER): _entity_selector("input_boolean"),
        vol.Optional(CONF_CLIMATE_MANUAL_OVERRIDE): _entity_selector("input_boolean"),
        vol.Required(CONF_CLIMATE_TARGET_LOW): _entity_selector("input_number"),
        vol.Required(CONF_CLIMATE_TARGET_HIGH): _entity_selector("input_number"),
    }
)

PRESENCE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PERSON_ENTITIES): _entity_selector("person", multiple=True),
    }
)

EV_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EV_SOC): _entity_selector(entity_filter=_sensor_filter(_PERCENT_SENSOR_UNITS)),
        vol.Optional(CONF_EV_CHARGING): _entity_selector(["binary_sensor", "sensor", "switch"]),
        vol.Optional(CONF_EV_CONNECTED): _entity_selector(["binary_sensor", "sensor"]),
        vol.Optional(CONF_EV_SMART_CHARGING): _entity_selector(["switch", "button", "input_boolean", "input_button"]),
        vol.Optional(CONF_EV_SMART_CHARGING_START): _entity_selector(
            ["switch", "button", "input_boolean", "input_button"]
        ),
        vol.Optional(CONF_EV_SMART_CHARGING_STOP): _entity_selector(
            ["switch", "button", "input_boolean", "input_button"]
        ),
        vol.Optional(CONF_EV_SMART_CHARGING_TARGET_SOC): _entity_selector(entity_filter=_EV_TARGET_SOC_FILTER),
        vol.Optional(CONF_EV_SMART_CHARGING_READY_BY): _entity_selector(
            ["time", "input_datetime", "input_text", "select", "input_select"]
        ),
    }
)

PLANNER_SUBENTRY_SCHEMAS: dict[str, vol.Schema] = {
    SUBENTRY_ENERGY: ENERGY_DATA_SCHEMA,
    SUBENTRY_CLIMATE: CLIMATE_DATA_SCHEMA,
    SUBENTRY_PRESENCE: PRESENCE_DATA_SCHEMA,
    SUBENTRY_ENPHASE: ENPHASE_DATA_SCHEMA,
    SUBENTRY_AI: AI_DATA_SCHEMA,
    SUBENTRY_EV: EV_DATA_SCHEMA,
}

PLANNER_SUBENTRY_TITLES = {
    SUBENTRY_ENERGY: "Energy",
    SUBENTRY_CLIMATE: "Climate",
    SUBENTRY_PRESENCE: "Presence",
    SUBENTRY_ENPHASE: "Enphase",
    SUBENTRY_AI: "AI",
    SUBENTRY_EV: "EV",
}

_MULTI_ENTITY_KEYS = {CONF_CLIMATE_AUTOMATIONS, CONF_PERSON_ENTITIES}

POLICY_STEP_SCHEDULE = "schedule"
POLICY_STEP_EV_BATTERY_GRID = "ev_battery_grid"
POLICY_STEP_CLIMATE = "climate"
POLICY_STEP_ENPHASE = "enphase"
POLICY_STEP_AI_SAFETY = "ai_safety"
POLICY_STEP_DATA_HEALTH = "data_health"
POLICY_STEP_PRIORITIES = "priorities"

_POLICY_MENU_OPTIONS = (
    POLICY_STEP_SCHEDULE,
    POLICY_STEP_EV_BATTERY_GRID,
    POLICY_STEP_CLIMATE,
    POLICY_STEP_ENPHASE,
    POLICY_STEP_AI_SAFETY,
    POLICY_STEP_DATA_HEALTH,
    POLICY_STEP_PRIORITIES,
)

_POLICY_SECTION_FIELDS = {
    POLICY_STEP_SCHEDULE: (
        CONF_PLANNING_HORIZON_HOURS,
        CONF_PLANNING_INTERVAL_MINUTES,
        CONF_DEFAULT_READY_BY,
    ),
    POLICY_STEP_EV_BATTERY_GRID: (
        CONF_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_USABLE_CAPACITY_KWH,
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
        CONF_BATTERY_MAX_CHARGE_KW,
        CONF_BATTERY_MAX_DISCHARGE_KW,
        CONF_EV_MIN_SOC_PERCENT,
        CONF_EV_MAX_SOC_PERCENT,
        CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
        CONF_EV_CHARGE_RATE_KW,
        CONF_EV_SOC_PER_KWH,
        CONF_GRID_IMPORT_LIMIT_KW,
        CONF_GRID_EXPORT_LIMIT_KW,
    ),
    POLICY_STEP_CLIMATE: (
        CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT,
        CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA,
        CONF_HVAC_PRECONDITION_LEAD_MINUTES,
        CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
        CONF_HVAC_MIN_CYCLE_MINUTES,
        CONF_MANUAL_HVAC_OVERRIDE_MINUTES,
    ),
    POLICY_STEP_ENPHASE: (
        CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES,
        CONF_ENPHASE_MIN_SAVINGS,
    ),
    POLICY_STEP_AI_SAFETY: (
        CONF_PLANNER_ENABLED,
        CONF_DRY_RUN,
        CONF_AI_ENABLED,
        CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
        CONF_EV_CONTROL_ENABLED,
        CONF_CLIMATE_CONTROL_ENABLED,
        CONF_ENPHASE_CONTROL_ENABLED,
        CONF_AI_TIMEOUT_SECONDS,
        CONF_COMMAND_RATE_LIMIT_SECONDS,
        CONF_MAX_DAILY_EV_ACTIONS,
        CONF_MAX_DAILY_CLIMATE_ACTIONS,
        CONF_MAX_DAILY_ENPHASE_ACTIONS,
    ),
    POLICY_STEP_DATA_HEALTH: (
        CONF_PRICE_FRESHNESS_MINUTES,
        CONF_FORECAST_FRESHNESS_MINUTES,
        CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT,
        CONF_MIN_TARIFF_CONFIDENCE,
        CONF_MIN_SOLAR_CONFIDENCE,
        CONF_MIN_LOAD_CONFIDENCE,
        CONF_MIN_CLIMATE_CONFIDENCE,
        CONF_MIN_EV_CONFIDENCE,
        CONF_MIN_ENPHASE_CONFIDENCE,
    ),
    POLICY_STEP_PRIORITIES: _PRIORITY_FORM_FIELDS,
}

_POLICY_ALL_FIELDS = tuple(field for step_id in _POLICY_MENU_OPTIONS for field in _POLICY_SECTION_FIELDS[step_id])


def _options_schema(options: dict[str, Any]) -> vol.Schema:
    """Return the complete policy schema used by tests and legacy callers."""
    return _options_section_schema(options, _POLICY_ALL_FIELDS)


def _options_section_schema(options: dict[str, Any], fields: tuple[str, ...]) -> vol.Schema:
    """Return an options schema for a policy section."""
    merged = {**DEFAULT_OPTIONS, **options}
    priority_values = _priority_values_from_options(merged)
    schema: dict[Any, Any] = {}
    for field in fields:
        if field in _PRIORITY_FORM_FIELDS:
            index = _PRIORITY_FORM_FIELDS.index(field)
            schema[vol.Required(field, default=priority_values[index])] = _priority_selector()
            continue
        schema[vol.Required(field, default=merged[field])] = _option_selector(field)
    return vol.Schema(schema)


def _priority_selector() -> SelectSelector:
    """Return the planning objective selector."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[{"value": value, "label": _PRIORITY_LABELS[value]} for value in _PRIORITY_OBJECTIVES],
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=False,
            sort=False,
        )
    )


def _option_selector(field: str) -> Any:
    """Return the selector for a policy option."""
    selectors: dict[str, Any] = {
        CONF_PLANNING_HORIZON_HOURS: NumberSelector(
            NumberSelectorConfig(min=1, max=48, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_PLANNING_INTERVAL_MINUTES: NumberSelector(
            NumberSelectorConfig(min=5, max=60, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_DEFAULT_READY_BY: TextSelector(TextSelectorConfig()),
        CONF_BATTERY_MIN_SOC_PERCENT: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_BATTERY_USABLE_CAPACITY_KWH: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: NumberSelector(
            NumberSelectorConfig(min=1, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_BATTERY_MAX_CHARGE_KW: NumberSelector(
            NumberSelectorConfig(min=0, max=50, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_BATTERY_MAX_DISCHARGE_KW: NumberSelector(
            NumberSelectorConfig(min=0, max=50, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_MIN_SOC_PERCENT: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_MAX_SOC_PERCENT: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_FALLBACK_TARGET_SOC_PERCENT: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_CHARGE_RATE_KW: NumberSelector(
            NumberSelectorConfig(min=0.1, max=50, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_SOC_PER_KWH: NumberSelector(
            NumberSelectorConfig(min=0.1, max=50, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_GRID_IMPORT_LIMIT_KW: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_GRID_EXPORT_LIMIT_KW: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=0.1, mode=NumberSelectorMode.BOX)
        ),
        CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT: NumberSelector(
            NumberSelectorConfig(min=0, max=50, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA: NumberSelector(
            NumberSelectorConfig(min=0, max=5, step=0.01, mode=NumberSelectorMode.BOX)
        ),
        CONF_HVAC_PRECONDITION_LEAD_MINUTES: NumberSelector(
            NumberSelectorConfig(min=0, max=120, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA: NumberSelector(
            NumberSelectorConfig(min=0, max=5, step=0.01, mode=NumberSelectorMode.BOX)
        ),
        CONF_HVAC_MIN_CYCLE_MINUTES: NumberSelector(
            NumberSelectorConfig(min=0, max=240, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_MANUAL_HVAC_OVERRIDE_MINUTES: NumberSelector(
            NumberSelectorConfig(min=1, max=1440, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES: NumberSelector(
            NumberSelectorConfig(min=1, max=240, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_PLANNER_ENABLED: BooleanSelector(),
        CONF_DRY_RUN: BooleanSelector(),
        CONF_AI_ENABLED: BooleanSelector(),
        CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED: BooleanSelector(),
        CONF_EV_CONTROL_ENABLED: BooleanSelector(),
        CONF_CLIMATE_CONTROL_ENABLED: BooleanSelector(),
        CONF_ENPHASE_CONTROL_ENABLED: BooleanSelector(),
        CONF_AI_TIMEOUT_SECONDS: NumberSelector(
            NumberSelectorConfig(min=1, max=120, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_PRICE_FRESHNESS_MINUTES: NumberSelector(
            NumberSelectorConfig(min=1, max=240, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_FORECAST_FRESHNESS_MINUTES: NumberSelector(
            NumberSelectorConfig(min=1, max=1440, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MIN_TARIFF_CONFIDENCE: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MIN_SOLAR_CONFIDENCE: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MIN_LOAD_CONFIDENCE: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MIN_CLIMATE_CONFIDENCE: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MIN_EV_CONFIDENCE: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MIN_ENPHASE_CONFIDENCE: NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_ENPHASE_MIN_SAVINGS: NumberSelector(
            NumberSelectorConfig(min=0, max=10, step=0.01, mode=NumberSelectorMode.BOX)
        ),
        CONF_COMMAND_RATE_LIMIT_SECONDS: NumberSelector(
            NumberSelectorConfig(min=0, max=3600, step=5, mode=NumberSelectorMode.BOX)
        ),
        CONF_MAX_DAILY_EV_ACTIONS: NumberSelector(
            NumberSelectorConfig(min=0, max=48, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MAX_DAILY_CLIMATE_ACTIONS: NumberSelector(
            NumberSelectorConfig(min=0, max=48, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_MAX_DAILY_ENPHASE_ACTIONS: NumberSelector(
            NumberSelectorConfig(min=0, max=48, step=1, mode=NumberSelectorMode.BOX)
        ),
    }
    return selectors[field]


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Energy Planner."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=INTEGRATION_NAME,
                data={},
                options=DEFAULT_OPTIONS,
            )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors={},
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlow:
        """Return options flow."""
        return OptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return planner input subentry flows supported by this integration."""
        return {
            SUBENTRY_ENERGY: EnergySubentryFlow,
            SUBENTRY_CLIMATE: ClimateSubentryFlow,
            SUBENTRY_PRESENCE: PresenceSubentryFlow,
            SUBENTRY_ENPHASE: EnphaseSubentryFlow,
            SUBENTRY_AI: AISubentryFlow,
            SUBENTRY_EV: EVSubentryFlow,
        }


class PlannerSubentryFlow(ConfigSubentryFlow):
    """Base flow for a single planner input group."""

    subentry_type: str
    data_schema: vol.Schema
    title: str

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle creating or configuring an input group."""
        return await self._async_step_configure(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle editing an input group."""
        return await self._async_step_configure(user_input)

    async def _async_step_configure(self, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        """Show and validate the group form."""
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_active_subentry() or self._existing_subentry(entry)
        if user_input is not None:
            errors = _validate_config(self.hass, user_input)
            if not errors:
                if subentry is not None:
                    return self.async_update_and_abort(entry, subentry, title=self.title, data=user_input)
                return self.async_create_entry(title=self.title, data=user_input)

        schema = self.data_schema
        if user_input is None and subentry is not None:
            schema = self.add_suggested_values_to_schema(schema, _form_suggested_values(dict(subentry.data)))
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _existing_subentry(self, entry: ConfigEntry) -> ConfigSubentry | None:
        """Return an existing subentry for this single-instance group."""
        for subentry in entry.subentries.values():
            if subentry.subentry_type == self.subentry_type:
                return subentry
        return None

    def _get_active_subentry(self) -> ConfigSubentry | None:
        """Return the subentry being reconfigured, if any."""
        try:
            return self._get_reconfigure_subentry()
        except (ValueError, UnknownSubEntry):
            return None


class EnergySubentryFlow(PlannerSubentryFlow):
    """Configure price, forecast, and battery inputs."""

    subentry_type = SUBENTRY_ENERGY
    data_schema = ENERGY_DATA_SCHEMA
    title = PLANNER_SUBENTRY_TITLES[SUBENTRY_ENERGY]


class ClimateSubentryFlow(PlannerSubentryFlow):
    """Configure climate inputs."""

    subentry_type = SUBENTRY_CLIMATE
    data_schema = CLIMATE_DATA_SCHEMA
    title = PLANNER_SUBENTRY_TITLES[SUBENTRY_CLIMATE]


class PresenceSubentryFlow(PlannerSubentryFlow):
    """Configure presence inputs."""

    subentry_type = SUBENTRY_PRESENCE
    data_schema = PRESENCE_DATA_SCHEMA
    title = PLANNER_SUBENTRY_TITLES[SUBENTRY_PRESENCE]


class EnphaseSubentryFlow(PlannerSubentryFlow):
    """Configure Enphase inputs."""

    subentry_type = SUBENTRY_ENPHASE
    data_schema = ENPHASE_DATA_SCHEMA
    title = PLANNER_SUBENTRY_TITLES[SUBENTRY_ENPHASE]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle choosing the Enphase system profile entity."""
        if user_input is None and self._has_existing_profile_entity():
            return await self.async_step_profiles()
        return await self._async_step_profile_entity(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle editing the Enphase system profile entity."""
        if user_input is None and self._has_existing_profile_entity():
            return await self.async_step_profiles()
        return await self._async_step_profile_entity(user_input)

    async def async_step_profiles(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle choosing profile names from the selected profile entity."""
        return await self._async_step_profiles(user_input)

    async def _async_step_profile_entity(self, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        """Show and validate the Enphase profile entity form."""
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_active_subentry() or self._existing_subentry(entry)
        current = dict(getattr(subentry, "data", {}) or {})
        if user_input is not None:
            errors = _validate_config(self.hass, user_input)
            if not errors:
                self._enphase_pending_data = {
                    **current,
                    CONF_ENPHASE_PROFILE: user_input[CONF_ENPHASE_PROFILE],
                }
                return await self.async_step_profiles()

        schema = ENPHASE_ENTITY_SCHEMA
        if user_input is None and current:
            schema = self.add_suggested_values_to_schema(schema, _form_suggested_values(current))
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_step_profiles(self, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        """Show and validate profile role selections."""
        entry = self._get_entry()
        subentry = self._get_active_subentry() or self._existing_subentry(entry)
        current = dict(getattr(subentry, "data", {}) or {})
        base = dict(getattr(self, "_enphase_pending_data", None) or current)
        if not base.get(CONF_ENPHASE_PROFILE):
            return await self._async_step_profile_entity(None)

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**base, **user_input}
            errors = _validate_config(self.hass, data)
            if not errors:
                if subentry is not None:
                    return self.async_update_and_abort(entry, subentry, title=self.title, data=data)
                return self.async_create_entry(title=self.title, data=data)

        suggested = {**current, **base}
        schema = self.add_suggested_values_to_schema(
            _enphase_profiles_schema(self.hass, suggested),
            _form_suggested_values(suggested),
        )
        return self.async_show_form(step_id="profiles", data_schema=schema, errors=errors)

    def _has_existing_profile_entity(self) -> bool:
        """Return whether the current Enphase subentry already has a profile entity."""
        entry = self._get_entry()
        subentry = self._get_active_subentry() or self._existing_subentry(entry)
        return bool(subentry and dict(getattr(subentry, "data", {}) or {}).get(CONF_ENPHASE_PROFILE))


class AISubentryFlow(PlannerSubentryFlow):
    """Configure local AI advisor inputs."""

    subentry_type = SUBENTRY_AI
    data_schema = AI_DATA_SCHEMA
    title = PLANNER_SUBENTRY_TITLES[SUBENTRY_AI]

    async def _async_step_configure(self, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        """Show and validate the AI agent form."""
        if user_input is not None:
            user_input = _normalize_ai_config(user_input)
        return await super()._async_step_configure(user_input)


class EVSubentryFlow(PlannerSubentryFlow):
    """Configure EV inputs."""

    subentry_type = SUBENTRY_EV
    data_schema = EV_DATA_SCHEMA
    title = PLANNER_SUBENTRY_TITLES[SUBENTRY_EV]


class OptionsFlow(config_entries.OptionsFlow):
    """Handle options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._options = dict(config_entry.options)

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the policy section menu."""
        return self.async_show_menu(step_id="init", menu_options=_POLICY_MENU_OPTIONS)

    async def async_step_schedule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage scheduling policy."""
        return await self._async_step_policy_section(POLICY_STEP_SCHEDULE, user_input)

    async def async_step_ev_battery_grid(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage EV, battery, and grid policy."""
        return await self._async_step_policy_section(POLICY_STEP_EV_BATTERY_GRID, user_input)

    async def async_step_climate(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage climate policy."""
        return await self._async_step_policy_section(POLICY_STEP_CLIMATE, user_input)

    async def async_step_enphase(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage Enphase policy."""
        return await self._async_step_policy_section(POLICY_STEP_ENPHASE, user_input)

    async def async_step_ai_safety(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage AI and safety policy."""
        return await self._async_step_policy_section(POLICY_STEP_AI_SAFETY, user_input)

    async def async_step_data_health(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage data health policy."""
        return await self._async_step_policy_section(POLICY_STEP_DATA_HEALTH, user_input)

    async def async_step_priorities(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage planning priority policy."""
        return await self._async_step_policy_section(POLICY_STEP_PRIORITIES, user_input)

    async def _async_step_policy_section(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> config_entries.ConfigFlowResult:
        """Manage one policy section."""
        errors: dict[str, str] = {}
        options = {**DEFAULT_OPTIONS, **self._options}
        if user_input is not None:
            updated = {**options, **user_input}
            errors = _validate_options(updated)
            if not errors:
                self._async_save_options(_normalize_options_input(updated))
                return await self.async_step_init()
        return self.async_show_form(
            step_id=step_id,
            data_schema=_options_section_schema(options, _POLICY_SECTION_FIELDS[step_id]),
            errors=errors,
        )

    def _async_save_options(self, options: dict[str, Any]) -> None:
        """Persist options without ending the policy menu flow."""
        self._options = dict(options)
        hass = getattr(self, "hass", None)
        config_entries_manager = getattr(hass, "config_entries", None)
        async_update_entry = getattr(config_entries_manager, "async_update_entry", None)
        if callable(async_update_entry):
            async_update_entry(self._config_entry, options=self._options)


def _form_suggested_values(data: dict[str, Any]) -> dict[str, Any]:
    """Return values shaped for config forms."""
    values = dict(data)
    for key in _MULTI_ENTITY_KEYS:
        if key in values:
            values[key] = _entity_values(values[key])
    return values


def _enphase_profiles_schema(hass: HomeAssistant, data: dict[str, Any]) -> vol.Schema:
    """Return a profile-role schema using options from the selected profile entity."""
    profile_options = _enphase_profile_options(hass, str(data.get(CONF_ENPHASE_PROFILE, "") or ""))
    return vol.Schema(
        {
            vol.Required(
                CONF_ENPHASE_AI_PROFILE,
                default=data.get(CONF_ENPHASE_AI_PROFILE, DEFAULT_ENPHASE_AI_PROFILE),
            ): _profile_select_selector(profile_options, data.get(CONF_ENPHASE_AI_PROFILE), DEFAULT_ENPHASE_AI_PROFILE),
            vol.Required(
                CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
                default=data.get(CONF_ENPHASE_SELF_CONSUMPTION_PROFILE, DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE),
            ): _profile_select_selector(
                profile_options,
                data.get(CONF_ENPHASE_SELF_CONSUMPTION_PROFILE),
                DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE,
            ),
            vol.Required(
                CONF_ENPHASE_FULL_BACKUP_PROFILE,
                default=data.get(CONF_ENPHASE_FULL_BACKUP_PROFILE, DEFAULT_ENPHASE_FULL_BACKUP_PROFILE),
            ): _profile_select_selector(
                profile_options,
                data.get(CONF_ENPHASE_FULL_BACKUP_PROFILE),
                DEFAULT_ENPHASE_FULL_BACKUP_PROFILE,
            ),
        }
    )


def _profile_select_selector(options: list[str], configured: Any, default: str) -> SelectSelector:
    """Return a profile selector with entity-provided options plus current values."""
    choices = _dedupe_text_values([*options, configured, default])
    return SelectSelector(
        SelectSelectorConfig(
            options=choices,
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,
            sort=False,
        )
    )


def _enphase_profile_options(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return profile names advertised by a select/input_select entity."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return []
    attributes = getattr(state, "attributes", {}) or {}
    options = attributes.get("options")
    values = list(options) if isinstance(options, list) else []
    values.append(getattr(state, "state", None))
    return _dedupe_text_values(values)


def _dedupe_text_values(values: list[Any]) -> list[str]:
    """Return non-empty strings with order preserved."""
    choices: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        choices.append(text)
    return choices


def _normalize_ai_config(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return stored AI config from the selected AI Task entity."""
    data = dict(user_input)
    task_entity = str(data.get(CONF_AI_TASK_ENTITY, "") or "").strip()
    data.pop("ai_agent_id", None)
    if task_entity:
        data[CONF_AI_TASK_ENTITY] = task_entity
        data[CONF_AI_ADVISOR_SERVICE] = "ai_task.generate_data"
    else:
        data.pop(CONF_AI_TASK_ENTITY, None)
        data[CONF_AI_ADVISOR_SERVICE] = ""
    return data


def _normalize_options_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return persisted options from the policy form."""
    data = dict(user_input)
    data[CONF_PRIORITY_WEIGHTS] = ",".join(_priority_values_from_form(data))
    for field in _PRIORITY_FORM_FIELDS:
        data.pop(field, None)
    return data


def _validate_options(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate policy options that selectors alone cannot prove."""
    errors: dict[str, str] = {}
    ev_min = float(user_input[CONF_EV_MIN_SOC_PERCENT])
    ev_max = float(user_input[CONF_EV_MAX_SOC_PERCENT])
    ev_fallback = float(user_input[CONF_EV_FALLBACK_TARGET_SOC_PERCENT])
    if ev_min > ev_max:
        errors["base"] = "ev_min_above_max"
    elif not ev_min <= ev_fallback <= ev_max:
        errors[CONF_EV_FALLBACK_TARGET_SOC_PERCENT] = "ev_fallback_outside_bounds"
    if not _ready_by_valid(str(user_input[CONF_DEFAULT_READY_BY])):
        errors[CONF_DEFAULT_READY_BY] = "invalid_ready_by"
    priority_values = _priority_values_from_form(user_input)
    if not _priority_weights_valid(priority_values):
        errors["base"] = "invalid_priority_weights"
    return errors


def _validate_config(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    """Validate configured entities without calling device services."""
    errors: dict[str, str] = {}
    for observed_key, forecast_key in (
        (CONF_PV_OBSERVED, CONF_PV_FORECAST),
        (CONF_BASELINE_LOAD_OBSERVED, CONF_BASELINE_LOAD_FORECAST),
    ):
        if user_input.get(observed_key) and user_input.get(observed_key) == user_input.get(forecast_key):
            errors[observed_key] = "observation_must_differ_from_forecast"
    if user_input.get(CONF_PV_FORECAST_SECONDARY) and user_input.get(CONF_PV_FORECAST_SECONDARY) == user_input.get(
        CONF_PV_FORECAST
    ):
        errors[CONF_PV_FORECAST_SECONDARY] = "forecast_sources_must_differ"
    if user_input.get(CONF_PV_OBSERVED) and user_input.get(CONF_PV_OBSERVED) == user_input.get(
        CONF_PV_FORECAST_SECONDARY
    ):
        errors[CONF_PV_OBSERVED] = "observation_must_differ_from_forecast"
    for key in (CONF_HAEO_OPTIMIZE_SERVICE, CONF_AI_ADVISOR_SERVICE):
        value = user_input.get(key)
        if not value:
            continue
        service_error = _validate_service(hass, str(value))
        if service_error:
            errors[key] = service_error
    for key, expected_domains in _ENTITY_DOMAIN_RULES.items():
        if key in errors:
            continue
        for entity_id in _entity_values(user_input.get(key)):
            entity_error = _validate_entity(hass, entity_id, expected_domains)
            if entity_error:
                errors[key] = entity_error
                break
            unit_error = _validate_entity_unit(hass, entity_id, key)
            if unit_error:
                errors[key] = unit_error
                break
    return errors


_ENTITY_DOMAIN_RULES = {
    CONF_AMBER_IMPORT_PRICE: {"sensor"},
    CONF_AMBER_EXPORT_PRICE: {"sensor"},
    CONF_PV_FORECAST: {"sensor"},
    CONF_PV_FORECAST_SECONDARY: {"sensor"},
    CONF_BASELINE_LOAD_FORECAST: {"sensor"},
    CONF_CARBON_INTENSITY_FORECAST: {"sensor"},
    CONF_PV_OBSERVED: {"sensor"},
    CONF_BASELINE_LOAD_OBSERVED: {"sensor"},
    CONF_BATTERY_SOC: {"sensor"},
    CONF_ENPHASE_PROFILE: {"select", "input_select"},
    CONF_DAIKIN_CLIMATE: {"climate"},
    CONF_DAIKIN_POWER: {"sensor"},
    CONF_CLIMATE_AUTOMATIONS: {"automation"},
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER: {"input_boolean"},
    CONF_CLIMATE_MANUAL_OVERRIDE: {"input_boolean"},
    CONF_CLIMATE_TARGET_LOW: {"input_number"},
    CONF_CLIMATE_TARGET_HIGH: {"input_number"},
    CONF_PERSON_ENTITIES: {"person"},
    CONF_EV_SOC: {"sensor"},
    CONF_EV_CHARGING: {"binary_sensor", "sensor", "switch"},
    CONF_EV_CONNECTED: {"binary_sensor", "sensor"},
    CONF_EV_SMART_CHARGING: {"switch", "button", "input_boolean", "input_button"},
    CONF_EV_SMART_CHARGING_START: {"switch", "button", "input_boolean", "input_button"},
    CONF_EV_SMART_CHARGING_STOP: {"switch", "button", "input_boolean", "input_button"},
    CONF_EV_SMART_CHARGING_TARGET_SOC: {"number", "input_number", "sensor", "select", "input_select"},
    CONF_EV_SMART_CHARGING_READY_BY: {"time", "input_datetime", "input_text", "select", "input_select"},
    CONF_AI_TASK_ENTITY: {"ai_task"},
    CONF_WEATHER: {"weather"},
}


def _validate_service(hass: HomeAssistant, service_name: str) -> str | None:
    if "." not in service_name:
        return "invalid_service_name"
    domain, service = service_name.split(".", 1)
    if not domain or not service:
        return "invalid_service_name"
    has_service = getattr(hass.services, "has_service", None)
    if callable(has_service) and not has_service(domain, service):
        return "service_not_found"
    return None


def _validate_entity(hass: HomeAssistant, entity_id: str, expected_domains: set[str]) -> str | None:
    try:
        cv.entity_id(entity_id)
    except Invalid:
        return "invalid_entity_id"
    domain = entity_id.split(".", 1)[0]
    if domain not in expected_domains:
        return "invalid_entity_domain"
    if hass.states.get(entity_id) is None:
        return "entity_not_found"
    return None


def _validate_entity_unit(hass: HomeAssistant, entity_id: str, config_key: str) -> str | None:
    expected_units = _ENTITY_UNIT_RULES.get(config_key)
    if not expected_units:
        return None
    state = hass.states.get(entity_id)
    attributes = getattr(state, "attributes", {}) or {}
    unit = attributes.get("unit_of_measurement") or attributes.get("unit")
    if unit is None:
        return None
    if _normalize_unit(str(unit)) not in expected_units:
        return "invalid_unit"
    return None


def _normalize_unit(unit: str) -> str:
    return unit.strip().lower().replace(" ", "").replace("₂", "2").replace("aud", "$").replace("a$", "$")


def _entity_values(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _ready_by_valid(value: str) -> bool:
    parts = value.strip().split(":")
    if len(parts) not in {2, 3}:
        return False
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def _priority_values_from_options(options: dict[str, Any]) -> list[str]:
    """Return a complete priority order from persisted options."""
    values = _priority_values_from_string(
        str(options.get(CONF_PRIORITY_WEIGHTS, DEFAULT_OPTIONS[CONF_PRIORITY_WEIGHTS]))
    )
    return [*values, *[value for value in _PRIORITY_OBJECTIVES if value not in values]]


def _priority_values_from_form(user_input: dict[str, Any]) -> list[str]:
    """Return priority values from the options form or legacy stored input."""
    if all(field in user_input for field in _PRIORITY_FORM_FIELDS):
        return [str(user_input[field]).strip() for field in _PRIORITY_FORM_FIELDS]
    return _priority_values_from_string(str(user_input.get(CONF_PRIORITY_WEIGHTS, "")))


def _priority_values_from_string(value: str) -> list[str]:
    """Return unique supported priorities from a comma-separated value."""
    values: list[str] = []
    for item in value.split(","):
        text = item.strip()
        if text in _ALLOWED_PRIORITY_WEIGHTS and text not in values:
            values.append(text)
    return values


def _priority_weights_valid(values: list[str]) -> bool:
    return (
        len(values) == len(_PRIORITY_OBJECTIVES)
        and len(values) == len(set(values))
        and all(item in _ALLOWED_PRIORITY_WEIGHTS for item in values)
    )


_PRICE_UNITS = {"$/kwh", "c/kwh", "¢/kwh", "cent/kwh", "cents/kwh"}
_POWER_UNITS = {"w", "kw", "mw", "watt", "watts", "kilowatt", "kilowatts"}
_ENERGY_UNITS = {
    "wh",
    "kwh",
    "mwh",
    "watt-hour",
    "watthour",
    "watt-hours",
    "watthours",
    "kilowatt-hour",
    "kilowatthour",
    "kilowatt-hours",
    "kilowatthours",
    "megawatt-hour",
    "megawatthour",
    "megawatt-hours",
    "megawatthours",
}
_PERCENT_UNITS = {"%", "percent", "percentage"}
_CARBON_INTENSITY_UNITS = {"gco2/kwh", "kgco2/kwh"}

_ENTITY_UNIT_RULES = {
    CONF_AMBER_IMPORT_PRICE: _PRICE_UNITS,
    CONF_AMBER_EXPORT_PRICE: _PRICE_UNITS,
    CONF_PV_FORECAST: _POWER_UNITS | _ENERGY_UNITS,
    CONF_PV_FORECAST_SECONDARY: _POWER_UNITS | _ENERGY_UNITS,
    CONF_BASELINE_LOAD_FORECAST: _POWER_UNITS,
    CONF_CARBON_INTENSITY_FORECAST: _CARBON_INTENSITY_UNITS,
    CONF_PV_OBSERVED: _POWER_UNITS,
    CONF_BASELINE_LOAD_OBSERVED: _POWER_UNITS,
    CONF_BATTERY_SOC: _PERCENT_UNITS,
    CONF_DAIKIN_POWER: _POWER_UNITS,
    CONF_EV_SOC: _PERCENT_UNITS,
}
