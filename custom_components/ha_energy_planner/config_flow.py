"""Config flow for Energy Planner."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import SectionConfig, section
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
    CONF_BYPASS_SAFETY_GATES,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_CLIMATE_ZONES,
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
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CHARGING,
    CONF_EV_CONFIRMATION_RETRIES,
    CONF_EV_CONFIRMATION_TIMEOUT_SECONDS,
    CONF_EV_CONNECTED,
    CONF_EV_CONTINUOUS_CHARGING,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_EARLIEST_START,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    CONF_EV_LOW_PRICE_THRESHOLD,
    CONF_EV_MAX_IMPORT_PRICE,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_PRICE_LIMIT_ENABLED,
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
    CONF_HOUSEHOLD_LOAD,
    CONF_HVAC_MIN_CYCLE_MINUTES,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
    CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA,
    CONF_INSTANCE_NAME,
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
from .entry_data import combined_entry_data

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


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INSTANCE_NAME, default=INTEGRATION_NAME): TextSelector(TextSelectorConfig()),
    }
)

ENERGY_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AMBER_IMPORT_PRICE): _entity_selector(entity_filter=_sensor_filter(_PRICE_SENSOR_UNITS)),
        vol.Required(CONF_AMBER_EXPORT_PRICE): _entity_selector(entity_filter=_sensor_filter(_PRICE_SENSOR_UNITS)),
        vol.Required(CONF_PV_FORECAST): _entity_selector(entity_filter=_sensor_filter(_FORECAST_SENSOR_UNITS)),
        vol.Optional(CONF_PV_FORECAST_SECONDARY): _entity_selector(
            entity_filter=_sensor_filter(_FORECAST_SENSOR_UNITS)
        ),
        vol.Required(CONF_HOUSEHOLD_LOAD): _entity_selector(entity_filter=_sensor_filter(_POWER_SENSOR_UNITS)),
        vol.Optional(CONF_CARBON_INTENSITY_FORECAST): _entity_selector(
            entity_filter=_sensor_filter(_CARBON_INTENSITY_SENSOR_UNITS)
        ),
        vol.Optional(CONF_PV_OBSERVED): _entity_selector(entity_filter=_sensor_filter(_POWER_SENSOR_UNITS)),
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
        vol.Optional(CONF_CLIMATE_ZONES): _entity_selector(["switch", "input_boolean"], multiple=True),
        vol.Optional(CONF_CLIMATE_CHANGE_FROM_SCHEDULER): _entity_selector("input_boolean"),
        vol.Optional(CONF_CLIMATE_SCHEDULER_GUARD_TIMER): _entity_selector("timer"),
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
        vol.Optional(CONF_EV_SMART_CHARGING_TARGET_SOC): _entity_selector(entity_filter=_EV_TARGET_SOC_FILTER),
        vol.Optional(CONF_EV_CHARGER): _entity_selector(["switch", "input_boolean"]),
        vol.Optional(CONF_EV_CHARGER_START): _entity_selector(["switch", "button", "input_boolean", "input_button"]),
        vol.Optional(CONF_EV_CHARGER_STOP): _entity_selector(["switch", "button", "input_boolean", "input_button"]),
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

INPUT_STEP_ENERGY = "energy_inputs"
INPUT_STEP_CLIMATE = "climate_inputs"
INPUT_STEP_PRESENCE = "presence_inputs"
INPUT_STEP_ENPHASE = "enphase_inputs"
INPUT_STEP_AI = "ai_inputs"
INPUT_STEP_EV = "ev_inputs"

_INPUT_SECTION_TYPES = {
    INPUT_STEP_ENERGY: SUBENTRY_ENERGY,
    INPUT_STEP_CLIMATE: SUBENTRY_CLIMATE,
    INPUT_STEP_PRESENCE: SUBENTRY_PRESENCE,
    INPUT_STEP_ENPHASE: SUBENTRY_ENPHASE,
    INPUT_STEP_AI: SUBENTRY_AI,
    INPUT_STEP_EV: SUBENTRY_EV,
}

_HOUSEHOLD_ACTUATOR_KEYS = (
    CONF_DAIKIN_CLIMATE,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_ZONES,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_ENPHASE_PROFILE,
)
_EV_ACTUATOR_KEYS = (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
)
_ACTUATOR_KEYS = (*_HOUSEHOLD_ACTUATOR_KEYS, *_EV_ACTUATOR_KEYS)
_SUBENTRY_ACTUATOR_KEYS = {
    SUBENTRY_CLIMATE: frozenset(
        {
            CONF_DAIKIN_CLIMATE,
            CONF_CLIMATE_AUTOMATIONS,
            CONF_CLIMATE_ZONES,
            CONF_CLIMATE_MANUAL_OVERRIDE,
        }
    ),
    SUBENTRY_ENPHASE: frozenset({CONF_ENPHASE_PROFILE}),
    SUBENTRY_EV: frozenset(_EV_ACTUATOR_KEYS),
}

_MULTI_ENTITY_KEYS = {CONF_CLIMATE_AUTOMATIONS, CONF_CLIMATE_ZONES, CONF_PERSON_ENTITIES}

POLICY_STEP_SCHEDULE = "schedule"
POLICY_STEP_EV_BATTERY_GRID = "ev_battery_grid"
POLICY_STEP_CLIMATE = "climate"
POLICY_STEP_ENPHASE = "enphase"
POLICY_STEP_AI_SAFETY = "ai_safety"
POLICY_STEP_DATA_HEALTH = "data_health"
POLICY_STEP_PRIORITIES = "priorities"

_POLICY_SECTION_OPTIONS = (
    POLICY_STEP_SCHEDULE,
    POLICY_STEP_EV_BATTERY_GRID,
    POLICY_STEP_CLIMATE,
    POLICY_STEP_ENPHASE,
    POLICY_STEP_AI_SAFETY,
    POLICY_STEP_DATA_HEALTH,
    POLICY_STEP_PRIORITIES,
)

# Options configure planner/device structure and operating constraints. Settings
# with native entities are intentionally omitted so day-to-day control has one
# Home Assistant surface that can also be automated.
_ENTITY_MANAGED_OPTION_FIELDS = frozenset(
    {
        CONF_PLANNER_ENABLED,
        CONF_DRY_RUN,
        CONF_EV_CONTROL_ENABLED,
        CONF_CLIMATE_CONTROL_ENABLED,
        CONF_ENPHASE_CONTROL_ENABLED,
    }
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
        CONF_EV_CONTINUOUS_CHARGING,
        CONF_EV_EARLIEST_START,
        CONF_EV_PRICE_LIMIT_ENABLED,
        CONF_EV_MAX_IMPORT_PRICE,
        CONF_EV_LOW_PRICE_CHARGING_ENABLED,
        CONF_EV_LOW_PRICE_THRESHOLD,
        CONF_EV_KEEP_CHARGER_ON,
        CONF_EV_CONFIRMATION_TIMEOUT_SECONDS,
        CONF_EV_CONFIRMATION_RETRIES,
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
        CONF_BYPASS_SAFETY_GATES,
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

_POLICY_SECTION_FIELDS = {
    step_id: tuple(field for field in fields if field not in _ENTITY_MANAGED_OPTION_FIELDS)
    for step_id, fields in _POLICY_SECTION_FIELDS.items()
}

_POLICY_ALL_FIELDS = tuple(field for step_id in _POLICY_SECTION_OPTIONS for field in _POLICY_SECTION_FIELDS[step_id])

_SETTINGS_SECTION_INPUT_TYPES = {
    INPUT_STEP_ENERGY: (SUBENTRY_ENERGY,),
    INPUT_STEP_CLIMATE: (SUBENTRY_CLIMATE, SUBENTRY_PRESENCE),
    INPUT_STEP_ENPHASE: (SUBENTRY_ENPHASE,),
    INPUT_STEP_AI: (SUBENTRY_AI,),
    INPUT_STEP_EV: (SUBENTRY_EV,),
    POLICY_STEP_PRIORITIES: (),
}

_SETTINGS_SECTION_OPTION_FIELDS = {
    INPUT_STEP_ENERGY: (
        CONF_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_USABLE_CAPACITY_KWH,
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
        CONF_BATTERY_MAX_CHARGE_KW,
        CONF_BATTERY_MAX_DISCHARGE_KW,
        CONF_GRID_IMPORT_LIMIT_KW,
        CONF_GRID_EXPORT_LIMIT_KW,
        *tuple(
            field
            for field in _POLICY_SECTION_FIELDS[POLICY_STEP_DATA_HEALTH]
            if field != CONF_BYPASS_SAFETY_GATES
        ),
    ),
    INPUT_STEP_CLIMATE: _POLICY_SECTION_FIELDS[POLICY_STEP_CLIMATE],
    INPUT_STEP_ENPHASE: _POLICY_SECTION_FIELDS[POLICY_STEP_ENPHASE],
    INPUT_STEP_AI: (
        CONF_BYPASS_SAFETY_GATES,
        *_POLICY_SECTION_FIELDS[POLICY_STEP_AI_SAFETY],
    ),
    INPUT_STEP_EV: (
        CONF_DEFAULT_READY_BY,
        CONF_EV_MIN_SOC_PERCENT,
        CONF_EV_MAX_SOC_PERCENT,
        CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
        CONF_EV_CHARGE_RATE_KW,
        CONF_EV_SOC_PER_KWH,
        CONF_EV_CONTINUOUS_CHARGING,
        CONF_EV_EARLIEST_START,
        CONF_EV_PRICE_LIMIT_ENABLED,
        CONF_EV_MAX_IMPORT_PRICE,
        CONF_EV_LOW_PRICE_CHARGING_ENABLED,
        CONF_EV_LOW_PRICE_THRESHOLD,
        CONF_EV_KEEP_CHARGER_ON,
        CONF_EV_CONFIRMATION_TIMEOUT_SECONDS,
        CONF_EV_CONFIRMATION_RETRIES,
    ),
    POLICY_STEP_PRIORITIES: (
        CONF_PLANNING_HORIZON_HOURS,
        CONF_PLANNING_INTERVAL_MINUTES,
        *_POLICY_SECTION_FIELDS[POLICY_STEP_PRIORITIES],
    ),
}

_SETTINGS_SECTION_ORDER = tuple(_SETTINGS_SECTION_INPUT_TYPES)


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
        CONF_EV_CONTINUOUS_CHARGING: BooleanSelector(),
        CONF_EV_EARLIEST_START: TextSelector(TextSelectorConfig()),
        CONF_EV_PRICE_LIMIT_ENABLED: BooleanSelector(),
        CONF_EV_MAX_IMPORT_PRICE: NumberSelector(
            NumberSelectorConfig(min=-10, max=10, step=0.01, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_LOW_PRICE_CHARGING_ENABLED: BooleanSelector(),
        CONF_EV_LOW_PRICE_THRESHOLD: NumberSelector(
            NumberSelectorConfig(min=-10, max=10, step=0.01, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_KEEP_CHARGER_ON: BooleanSelector(),
        CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: NumberSelector(
            NumberSelectorConfig(min=0, max=120, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_EV_CONFIRMATION_RETRIES: NumberSelector(
            NumberSelectorConfig(min=0, max=3, step=1, mode=NumberSelectorMode.BOX)
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
        CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED: BooleanSelector(),
        CONF_EV_CONTROL_ENABLED: BooleanSelector(),
        CONF_CLIMATE_CONTROL_ENABLED: BooleanSelector(),
        CONF_ENPHASE_CONTROL_ENABLED: BooleanSelector(),
        CONF_AI_TIMEOUT_SECONDS: NumberSelector(
            NumberSelectorConfig(min=1, max=120, step=1, mode=NumberSelectorMode.BOX)
        ),
        CONF_BYPASS_SAFETY_GATES: BooleanSelector(),
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

    VERSION = 2

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            title = str(user_input.get(CONF_INSTANCE_NAME, INTEGRATION_NAME)).strip()
            if not title:
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={CONF_INSTANCE_NAME: "instance_name_required"},
                )
            current_entries = self._async_current_entries() if getattr(self, "hass", None) is not None else []
            if any(entry.title == title for entry in current_entries):
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={CONF_INSTANCE_NAME: "instance_name_in_use"},
                )
            return self.async_create_entry(
                title=title,
                data={CONF_INSTANCE_NAME: title},
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

class OptionsFlow(config_entries.OptionsFlow):
    """Handle central Energy Planner settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._data = dict(getattr(config_entry, "data", {}))
        self._options = dict(config_entry.options)

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show every input and policy group on one sectioned settings page."""
        errors: dict[str, str] = {}
        if user_input is not None:
            updated_data = dict(self._data)
            updated_options = {**DEFAULT_OPTIONS, **self._options}
            for step_id in _SETTINGS_SECTION_ORDER:
                if step_id not in user_input:
                    continue
                submitted = dict(user_input[step_id])
                for input_type in _SETTINGS_SECTION_INPUT_TYPES[step_id]:
                    data_schema = PLANNER_SUBENTRY_SCHEMAS[input_type]
                    section_fields = _schema_field_names(data_schema)
                    if input_type == SUBENTRY_AI:
                        section_fields.add(CONF_AI_ADVISOR_SERVICE)
                    submitted_data = {
                        key: value for key, value in submitted.items() if key in section_fields
                    }
                    if input_type == SUBENTRY_AI:
                        submitted_data = _normalize_ai_config(submitted_data)
                    entity_fields = section_fields - {CONF_AI_ADVISOR_SERVICE}
                    mappings_changed = any(
                        submitted_data.get(key) != self._data.get(key)
                        for key in entity_fields
                    )
                    section_errors = (
                        _validate_subentry_config(
                            self.hass,
                            self._config_entry,
                            submitted_data,
                            subentry_type=input_type,
                        )
                        if mappings_changed
                        else {}
                    )
                    if section_errors:
                        errors.setdefault("base", next(iter(section_errors.values())))
                        continue
                    updated_data = {
                        key: value
                        for key, value in updated_data.items()
                        if key not in section_fields
                    }
                    updated_data.update(submitted_data)
                updated_options.update(
                    {
                        key: value
                        for key, value in submitted.items()
                        if key in _SETTINGS_SECTION_OPTION_FIELDS[step_id]
                    }
                )

            duplicate_errors = _duplicate_household_actuator_errors(
                self.hass,
                self._config_entry,
                updated_data,
            )
            if duplicate_errors:
                errors.setdefault("base", next(iter(duplicate_errors.values())))

            option_errors = _validate_options(updated_options)
            if not _ev_keep_on_control_compatible(updated_data, updated_options):
                option_errors[CONF_EV_KEEP_CHARGER_ON] = "ev_keep_on_requires_persistent_control"
            if option_errors:
                errors.setdefault("base", next(iter(option_errors.values())))

            if not errors:
                self._async_save_entry_data(updated_data)
                self._options = _normalize_options_input(updated_options)
                return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="init",
            data_schema=self._settings_schema(),
            errors=errors,
            last_step=True,
        )

    def _settings_schema(self) -> vol.Schema:
        """Return one form with collapsible sections for all settings."""
        schema: dict[Any, Any] = {}
        options = {**DEFAULT_OPTIONS, **self._options}
        for step_id in _SETTINGS_SECTION_ORDER:
            input_types = _SETTINGS_SECTION_INPUT_TYPES[step_id]
            nested_fields: dict[Any, Any] = {}
            schema_fields: set[str] = set()
            for input_type in input_types:
                data_schema = PLANNER_SUBENTRY_SCHEMAS[input_type]
                nested_fields.update(_optional_settings_fields(data_schema))
                schema_fields.update(_schema_field_names(data_schema))
            option_fields = _SETTINGS_SECTION_OPTION_FIELDS[step_id]
            nested_fields.update(_options_section_schema(options, option_fields).schema)
            current = {
                key: value
                for key, value in self._data.items()
                if key in schema_fields
            }
            nested = self.add_suggested_values_to_schema(
                vol.Schema(nested_fields),
                _form_suggested_values(current),
            )
            schema[vol.Required(step_id)] = section(nested, SectionConfig(collapsed=True))
        return vol.Schema(schema)

    def _async_save_entry_data(self, data: dict[str, Any]) -> None:
        """Persist central input settings with the completed options form."""
        self._data = {
            key: value
            for key, value in data.items()
            if key not in {CONF_BASELINE_LOAD_FORECAST, CONF_BASELINE_LOAD_OBSERVED}
        }
        hass = getattr(self, "hass", None)
        config_entries_manager = getattr(hass, "config_entries", None)
        async_update_entry = getattr(config_entries_manager, "async_update_entry", None)
        if callable(async_update_entry):
            async_update_entry(self._config_entry, data=self._data)


def _form_suggested_values(data: dict[str, Any]) -> dict[str, Any]:
    """Return values shaped for config forms."""
    values = dict(data)
    for key in _MULTI_ENTITY_KEYS:
        if key in values:
            values[key] = _entity_values(values[key])
    return values


def _optional_settings_fields(schema: vol.Schema) -> dict[Any, Any]:
    """Return input mappings as removable fields inside combined settings sections."""
    fields: dict[Any, Any] = {}
    for marker, validator in schema.schema.items():
        if isinstance(marker, vol.Required):
            marker = vol.Optional(marker.schema)
        fields[marker] = validator
    return fields


def _schema_field_names(schema: vol.Schema) -> set[str]:
    """Return field names declared by a voluptuous form schema."""
    return {str(getattr(field, "schema", field)) for field in schema.schema}


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
    earliest_start = str(user_input[CONF_EV_EARLIEST_START])
    if earliest_start.lower() != "none" and not _ready_by_valid(earliest_start):
        errors[CONF_EV_EARLIEST_START] = "invalid_ready_by"
    priority_values = _priority_values_from_form(user_input)
    if not _priority_weights_valid(priority_values):
        errors["base"] = "invalid_priority_weights"
    return errors


def _validate_config(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    """Validate configured entities without calling device services."""
    errors: dict[str, str] = {}
    scheduler_guard = bool(user_input.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER))
    scheduler_timer = bool(user_input.get(CONF_CLIMATE_SCHEDULER_GUARD_TIMER))
    if scheduler_guard != scheduler_timer:
        missing_key = (
            CONF_CLIMATE_SCHEDULER_GUARD_TIMER
            if scheduler_guard
            else CONF_CLIMATE_CHANGE_FROM_SCHEDULER
        )
        errors[missing_key] = "climate_scheduler_guard_pair_required"
    for observed_key, forecast_key in ((CONF_PV_OBSERVED, CONF_PV_FORECAST),):
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
    for key in (CONF_AI_ADVISOR_SERVICE,):
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


def _validate_subentry_config(
    hass: HomeAssistant,
    entry: ConfigEntry,
    user_input: dict[str, Any],
    *,
    subentry_type: str | None = None,
) -> dict[str, str]:
    """Validate one subentry, including cross-entry actuator ownership."""
    errors = _validate_config(hass, user_input)
    duplicate_errors = _duplicate_household_actuator_errors(
        hass,
        entry,
        user_input,
        subentry_type=subentry_type,
    )
    for key, error in duplicate_errors.items():
        errors.setdefault(key, error)
    if subentry_type == SUBENTRY_EV and not _ev_keep_on_control_compatible(
        user_input,
        {**DEFAULT_OPTIONS, **dict(getattr(entry, "options", {}))},
    ):
        errors.setdefault("base", "ev_keep_on_requires_persistent_control")
    return errors


def _duplicate_household_actuator_errors(
    hass: HomeAssistant,
    current_entry: ConfigEntry,
    user_input: dict[str, Any],
    *,
    subentry_type: str | None = None,
) -> dict[str, str]:
    """Reject actuators assigned to more than one planner control."""
    requested = {
        key: set(_entity_values(user_input.get(key)))
        for key in _ACTUATOR_KEYS
        if user_input.get(key)
    }
    errors: dict[str, str] = {}
    for key, requested_entities in requested.items():
        other_requested_entities = {
            entity_id
            for other_key, entity_ids in requested.items()
            if other_key != key
            for entity_id in entity_ids
        }
        if requested_entities.intersection(other_requested_entities):
            errors[key] = "household_actuator_in_use"

    config_entries = getattr(hass, "config_entries", None)
    async_entries = getattr(config_entries, "async_entries", None)
    if not callable(async_entries):
        return errors
    current_entry_id = str(getattr(current_entry, "entry_id", ""))
    current_subentry_keys = _SUBENTRY_ACTUATOR_KEYS.get(
        subentry_type,
        frozenset(key for key in _ACTUATOR_KEYS if key in user_input),
    )
    for other_entry in async_entries(DOMAIN):
        is_current_entry = other_entry is current_entry or (
            current_entry_id
            and str(getattr(other_entry, "entry_id", "")) == current_entry_id
        )
        other_data = combined_entry_data(other_entry)
        other_actuator_entities = {
            entity_id
            for other_key in _ACTUATOR_KEYS
            if not (is_current_entry and other_key in current_subentry_keys)
            for entity_id in _entity_values(other_data.get(other_key))
        }
        for key, requested_entities in requested.items():
            if requested_entities.intersection(other_actuator_entities):
                errors[key] = "household_actuator_in_use"
    return errors


def _ev_keep_on_control_compatible(
    entry_data: dict[str, Any],
    options: dict[str, Any],
) -> bool:
    """Return whether keep-on has an authoritative persistent charger control."""
    if options.get(CONF_EV_KEEP_CHARGER_ON) is not True:
        return True
    entity_id = entry_data.get(CONF_EV_CHARGER) or entry_data.get(CONF_EV_SMART_CHARGING)
    return bool(
        entity_id
        and str(entity_id).split(".", 1)[0] in {"switch", "input_boolean"}
    )


_ENTITY_DOMAIN_RULES = {
    CONF_AMBER_IMPORT_PRICE: {"sensor"},
    CONF_AMBER_EXPORT_PRICE: {"sensor"},
    CONF_PV_FORECAST: {"sensor"},
    CONF_PV_FORECAST_SECONDARY: {"sensor"},
    CONF_HOUSEHOLD_LOAD: {"sensor"},
    CONF_CARBON_INTENSITY_FORECAST: {"sensor"},
    CONF_PV_OBSERVED: {"sensor"},
    CONF_BATTERY_SOC: {"sensor"},
    CONF_ENPHASE_PROFILE: {"select", "input_select"},
    CONF_DAIKIN_CLIMATE: {"climate"},
    CONF_DAIKIN_POWER: {"sensor"},
    CONF_CLIMATE_AUTOMATIONS: {"automation"},
    CONF_CLIMATE_ZONES: {"switch", "input_boolean"},
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER: {"input_boolean"},
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER: {"timer"},
    CONF_CLIMATE_MANUAL_OVERRIDE: {"input_boolean"},
    CONF_CLIMATE_TARGET_LOW: {"input_number"},
    CONF_CLIMATE_TARGET_HIGH: {"input_number"},
    CONF_PERSON_ENTITIES: {"person"},
    CONF_EV_SOC: {"sensor"},
    CONF_EV_CHARGING: {"binary_sensor", "sensor", "switch"},
    CONF_EV_CONNECTED: {"binary_sensor", "sensor"},
    CONF_EV_CHARGER: {"switch", "input_boolean"},
    CONF_EV_CHARGER_START: {"switch", "button", "input_boolean", "input_button"},
    CONF_EV_CHARGER_STOP: {"switch", "button", "input_boolean", "input_button"},
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
    CONF_HOUSEHOLD_LOAD: _POWER_UNITS,
    CONF_CARBON_INTENSITY_FORECAST: _CARBON_INTENSITY_UNITS,
    CONF_PV_OBSERVED: _POWER_UNITS,
    CONF_BATTERY_SOC: _PERCENT_UNITS,
    CONF_DAIKIN_POWER: _POWER_UNITS,
    CONF_EV_SOC: _PERCENT_UNITS,
}
