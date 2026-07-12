"""Constants for Energy Planner."""

from __future__ import annotations

DOMAIN = "ha_energy_planner"
INTEGRATION_NAME = "Energy Planner"
LEGACY_INTEGRATION_NAME = f"HA {INTEGRATION_NAME}"

PLATFORMS = ["sensor", "binary_sensor", "switch", "button"]

CONF_HAEO_OPTIMIZE_SERVICE = "haeo_optimize_service"
CONF_AMBER_IMPORT_PRICE = "amber_import_price_entity"
CONF_AMBER_EXPORT_PRICE = "amber_export_price_entity"
CONF_PV_FORECAST = "pv_forecast_entity"
CONF_PV_FORECAST_SECONDARY = "pv_forecast_secondary_entity"
CONF_BASELINE_LOAD_FORECAST = "baseline_load_forecast_entity"
CONF_CARBON_INTENSITY_FORECAST = "carbon_intensity_forecast_entity"
CONF_PV_OBSERVED = "pv_observed_entity"
CONF_BASELINE_LOAD_OBSERVED = "baseline_load_observed_entity"
CONF_BATTERY_SOC = "battery_soc_entity"
CONF_ENPHASE_PROFILE = "enphase_profile_entity"
CONF_ENPHASE_PROFILE_CONTROL_SERVICE = "enphase_profile_control_service"
CONF_ENPHASE_AI_PROFILE = "enphase_ai_profile"
CONF_ENPHASE_ARBITRAGE_PROFILE = "enphase_arbitrage_profile"
CONF_ENPHASE_SELF_CONSUMPTION_PROFILE = "enphase_self_consumption_profile"
CONF_ENPHASE_FULL_BACKUP_PROFILE = "enphase_full_backup_profile"
CONF_DAIKIN_CLIMATE = "daikin_climate_entity"
CONF_DAIKIN_POWER = "daikin_power_entity"
CONF_CLIMATE_AUTOMATIONS = "climate_automation_entities"
CONF_CLIMATE_CHANGE_FROM_SCHEDULER = "climate_change_from_scheduler_entity"
CONF_CLIMATE_MANUAL_OVERRIDE = "climate_manual_override_entity"
CONF_CLIMATE_TARGET_LOW = "climate_target_low_entity"
CONF_CLIMATE_TARGET_HIGH = "climate_target_high_entity"
CONF_PERSON_ENTITIES = "person_entities"
CONF_EV_SOC = "ev_soc_entity"
CONF_EV_CHARGING = "ev_charging_entity"
CONF_EV_CONNECTED = "ev_connected_entity"
CONF_EV_SMART_CHARGING = "ev_smart_charging_entity"
CONF_EV_SMART_CHARGING_START = "ev_smart_charging_start_entity"
CONF_EV_SMART_CHARGING_STOP = "ev_smart_charging_stop_entity"
CONF_EV_SMART_CHARGING_TARGET_SOC = "ev_smart_charging_target_soc_entity"
CONF_EV_SMART_CHARGING_READY_BY = "ev_smart_charging_ready_by_entity"
CONF_AI_ADVISOR_SERVICE = "ai_advisor_service"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_WEATHER = "weather_entity"

DEFAULT_HAEO_OPTIMIZE_SERVICE = "haeo.optimize"
DEFAULT_ENPHASE_AI_PROFILE = "AI Optimisation"
DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE = "Self-Consumption"
DEFAULT_ENPHASE_FULL_BACKUP_PROFILE = "Full Backup"

CONF_PLANNING_HORIZON_HOURS = "planning_horizon_hours"
CONF_PLANNING_INTERVAL_MINUTES = "planning_interval_minutes"
CONF_DEFAULT_READY_BY = "default_ready_by"
CONF_BATTERY_MIN_SOC_PERCENT = "battery_min_soc_percent"
CONF_BATTERY_USABLE_CAPACITY_KWH = "battery_usable_capacity_kwh"
CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT = "battery_round_trip_efficiency_percent"
CONF_BATTERY_MAX_CHARGE_KW = "battery_max_charge_kw"
CONF_BATTERY_MAX_DISCHARGE_KW = "battery_max_discharge_kw"
CONF_EV_MIN_SOC_PERCENT = "ev_min_soc_percent"
CONF_EV_MAX_SOC_PERCENT = "ev_max_soc_percent"
CONF_EV_FALLBACK_TARGET_SOC_PERCENT = "ev_fallback_target_soc_percent"
CONF_EV_CHARGE_RATE_KW = "ev_charge_rate_kw"
CONF_EV_SOC_PER_KWH = "ev_soc_per_kwh"
CONF_GRID_IMPORT_LIMIT_KW = "grid_import_limit_kw"
CONF_GRID_EXPORT_LIMIT_KW = "grid_export_limit_kw"
CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT = "occupied_temperature_tolerance_percent"
CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA = "hvac_suppression_min_price_delta"
CONF_HVAC_PRECONDITION_LEAD_MINUTES = "hvac_precondition_lead_minutes"
CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA = "hvac_precondition_min_price_delta"
CONF_HVAC_MIN_CYCLE_MINUTES = "hvac_min_cycle_minutes"
CONF_MANUAL_HVAC_OVERRIDE_MINUTES = "manual_hvac_override_minutes"
CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES = "enphase_profile_min_hold_minutes"
CONF_PLANNER_ENABLED = "planner_enabled"
CONF_DRY_RUN = "dry_run"
CONF_AI_ENABLED = "ai_enabled"
CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED = "plan_fallback_notifications_enabled"
CONF_EV_CONTROL_ENABLED = "ev_control_enabled"
CONF_CLIMATE_CONTROL_ENABLED = "climate_control_enabled"
CONF_ENPHASE_CONTROL_ENABLED = "enphase_control_enabled"
CONF_AI_TIMEOUT_SECONDS = "ai_timeout_seconds"
CONF_PRICE_FRESHNESS_MINUTES = "price_freshness_minutes"
CONF_FORECAST_FRESHNESS_MINUTES = "forecast_freshness_minutes"
CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT = "material_change_threshold_percent"
CONF_ENPHASE_MIN_SAVINGS = "enphase_minimum_savings"
CONF_COMMAND_RATE_LIMIT_SECONDS = "command_rate_limit_seconds"
CONF_MAX_DAILY_EV_ACTIONS = "max_daily_ev_actions"
CONF_MAX_DAILY_CLIMATE_ACTIONS = "max_daily_climate_actions"
CONF_MAX_DAILY_ENPHASE_ACTIONS = "max_daily_enphase_actions"
CONF_PRIORITY_WEIGHTS = "priority_weights"
CONF_MIN_TARIFF_CONFIDENCE = "minimum_tariff_confidence"
CONF_MIN_SOLAR_CONFIDENCE = "minimum_solar_confidence"
CONF_MIN_LOAD_CONFIDENCE = "minimum_load_confidence"
CONF_MIN_CLIMATE_CONFIDENCE = "minimum_climate_confidence"
CONF_MIN_EV_CONFIDENCE = "minimum_ev_confidence"
CONF_MIN_ENPHASE_CONFIDENCE = "minimum_enphase_confidence"

DEFAULT_OPTIONS = {
    CONF_PLANNING_HORIZON_HOURS: 12,
    CONF_PLANNING_INTERVAL_MINUTES: 5,
    CONF_DEFAULT_READY_BY: "07:00",
    CONF_BATTERY_MIN_SOC_PERCENT: 10.0,
    CONF_BATTERY_USABLE_CAPACITY_KWH: 10.0,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: 90.0,
    CONF_BATTERY_MAX_CHARGE_KW: 5.0,
    CONF_BATTERY_MAX_DISCHARGE_KW: 5.0,
    CONF_EV_MIN_SOC_PERCENT: 40.0,
    CONF_EV_MAX_SOC_PERCENT: 90.0,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT: 80.0,
    CONF_EV_CHARGE_RATE_KW: 7.0,
    CONF_EV_SOC_PER_KWH: 5.0,
    CONF_GRID_IMPORT_LIMIT_KW: 10.0,
    CONF_GRID_EXPORT_LIMIT_KW: 10.0,
    CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT: 10.0,
    CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA: 0.20,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES: 30,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA: 0.20,
    CONF_HVAC_MIN_CYCLE_MINUTES: 20,
    CONF_MANUAL_HVAC_OVERRIDE_MINUTES: 120,
    CONF_ENPHASE_PROFILE_MIN_HOLD_MINUTES: 30,
    CONF_PLANNER_ENABLED: False,
    CONF_DRY_RUN: True,
    CONF_AI_ENABLED: False,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED: True,
    CONF_EV_CONTROL_ENABLED: False,
    CONF_CLIMATE_CONTROL_ENABLED: False,
    CONF_ENPHASE_CONTROL_ENABLED: False,
    CONF_AI_TIMEOUT_SECONDS: 20,
    CONF_PRICE_FRESHNESS_MINUTES: 30,
    CONF_FORECAST_FRESHNESS_MINUTES: 120,
    CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT: 5.0,
    CONF_ENPHASE_MIN_SAVINGS: 0.25,
    CONF_COMMAND_RATE_LIMIT_SECONDS: 60,
    CONF_MAX_DAILY_EV_ACTIONS: 4,
    CONF_MAX_DAILY_CLIMATE_ACTIONS: 8,
    CONF_MAX_DAILY_ENPHASE_ACTIONS: 6,
    CONF_PRIORITY_WEIGHTS: "cost,comfort,ev_readiness,battery_reserve,solar_self_consumption,carbon",
    CONF_MIN_TARIFF_CONFIDENCE: 50.0,
    CONF_MIN_SOLAR_CONFIDENCE: 50.0,
    CONF_MIN_LOAD_CONFIDENCE: 50.0,
    CONF_MIN_CLIMATE_CONFIDENCE: 50.0,
    CONF_MIN_EV_CONFIDENCE: 50.0,
    CONF_MIN_ENPHASE_CONFIDENCE: 50.0,
}

DEBOUNCE_SECONDS = 20
MIN_NON_MANUAL_REFRESH_INTERVAL_SECONDS = 60
AI_ADVICE_MIN_INTERVAL_SECONDS = 300

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}_state"

SERVICE_REPLAN = "replan"
SERVICE_RESTORE_SAFE_STATE = "restore_safe_state"
SERVICE_SET_EV_READY_BY = "set_ev_ready_by"
SERVICE_SET_MANUAL_HVAC_OVERRIDE = "set_manual_hvac_override"
SERVICE_EXPORT_DIAGNOSTICS = "export_diagnostics"
SERVICE_EXPORT_SUPPORT_BUNDLE = "export_support_bundle"
SERVICE_RUN_PREFLIGHT = "run_preflight"
SERVICE_ARM_PRODUCTION_CONTROL = "arm_production_control"
SERVICE_DISARM_PRODUCTION_CONTROL = "disarm_production_control"
SERVICE_PAUSE_CONTROL = "pause_control"
SERVICE_RESUME_CONTROL = "resume_control"

ATTR_REASON = "reason"
ATTR_READY_BY = "ready_by"
ATTR_DURATION_MINUTES = "duration_minutes"
ATTR_ASSET = "asset"

STATE_UNKNOWN_VALUES = {"unknown", "unavailable", None}
