#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "$ROOT_DIR/.ha-smoke.XXXXXX")"
LOG_FILE="$TMP_DIR/home-assistant.log"

cleanup() {
  if [[ "${KEEP_HA_SMOKE:-0}" == "1" ]]; then
    echo "Preserving Home Assistant smoke config at $TMP_DIR"
  else
    chmod -R u+rwX "$TMP_DIR" 2>/dev/null || true
    if ! rm -rf "$TMP_DIR" 2>/dev/null; then
      docker run --rm \
        -v "$TMP_DIR:/cleanup" \
        --entrypoint /bin/sh \
        ghcr.io/home-assistant/home-assistant:stable \
        -c 'find /cleanup -mindepth 1 -exec rm -rf {} +' >/dev/null 2>&1 || true
      rm -rf "$TMP_DIR" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT

mkdir -p "$TMP_DIR/custom_components" "$TMP_DIR/.storage"
cp -R "$ROOT_DIR/custom_components/ha_energy_planner" "$TMP_DIR/custom_components/"
mkdir -p "$TMP_DIR/custom_components/fake_haeo"
cat > "$TMP_DIR/custom_components/fake_haeo/manifest.json" <<'JSON'
{
  "domain": "fake_haeo",
  "name": "Fake HAEO",
  "version": "0.1.0",
  "documentation": "https://example.invalid/fake-haeo",
  "integration_type": "hub",
  "iot_class": "local_push"
}
JSON
cat > "$TMP_DIR/custom_components/fake_haeo/__init__.py" <<'PY'
"""Fake HAEO service-response integration for Docker smoke tests."""

from __future__ import annotations

from datetime import timedelta
import json

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util

DOMAIN = "fake_haeo"
PLATFORMS: list[Platform] = []


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register a response-capable fake HAEO optimize service."""

    async def force_trip_import_due(call: ServiceCall) -> None:
        """Mark HA Energy Planner trip Recorder import due for smoke validation."""
        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            history = dict(store.data.get("trip_history", {}))
            history["recorder_imported_at"] = "1970-01-01T00:00:00+00:00"
            await store.async_save_trip_history(history)

    async def seed_due_forecast_snapshot(call: ServiceCall) -> None:
        """Seed one due forecast snapshot for smoke calibration validation."""
        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            await store.async_add_forecast_snapshot(
                {
                    "created_at": "1970-01-01T00:00:00+00:00",
                    "plan_id": "docker_smoke_calibration",
                    "forecast_training_slots": [
                        {
                            "valid_at": "1970-01-01T00:00:00+00:00",
                            "pv_forecast_kw": 1.0,
                            "baseline_load_forecast_kw": 1.0,
                        }
                    ],
                }
            )

    async def seed_thermal_model_sample(call: ServiceCall) -> None:
        """Seed a recent prior HVAC sample for smoke thermal-model validation."""
        sampled_at = dt_util.utcnow() - timedelta(minutes=5)
        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            model = dict(store.data.get("thermal_model", {}))
            model["last_sample"] = {
                "sampled_at": sampled_at.isoformat(),
                "hvac_mode": "heat",
                "indoor_temperature_c": 20.0,
                "outdoor_temperature_c": 12.0,
                "hvac_power_kw": 1.8,
            }
            await store.async_save_thermal_model(model)

    async def seed_enphase_command_rate_limit(call: ServiceCall) -> None:
        """Seed a future Enphase command cooldown for smoke execution-gate validation."""
        attempted_at = dt_util.utcnow() + timedelta(minutes=5)
        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            await store.async_save_command_rate_limits({"enphase:set_profile": attempted_at.isoformat()})

    async def capture_persistent_notification(call: ServiceCall) -> None:
        """Capture persistent notification calls for smoke validation."""
        notification_id = str(call.data.get("notification_id", ""))
        if notification_id:
            current = hass.states.get("input_text.planner_restore_notification_seen")
            if (
                getattr(current, "state", None) == "ha_energy_planner_restore_safe_state"
                and notification_id != "ha_energy_planner_restore_safe_state"
            ):
                return
            hass.states.async_set(
                "input_text.planner_restore_notification_seen",
                notification_id,
                {
                    "title": call.data.get("title", ""),
                    "message": call.data.get("message", ""),
                },
            )

    async def ai_advice(call: ServiceCall) -> dict:
        """Return bounded local-AI-style JSON advice for smoke validation."""
        return {
            "response": json.dumps(
                {
                    "alerts": ["Smoke advisor observed bounded plan metadata"],
                    "suggested_precondition_lead_minutes": 45,
                    "suggested_forecast_buffer_percent": 12,
                    "suggested_takeover_savings_threshold": 0.33,
                    "reasoning_summary": "Smoke advisor response stayed within the allowed contract.",
                    "confidence": 0.77,
                }
            )
        }

    async def optimize(call: ServiceCall) -> dict:
        export_state = hass.states.get("input_number.export_price")
        import_state = hass.states.get("input_number.import_price")
        try:
            export_price = float(export_state.state) if export_state else 0.0
        except (TypeError, ValueError):
            export_price = 0.0
        try:
            import_price = float(import_state.state) if import_state else 0.0
        except (TypeError, ValueError):
            import_price = 0.0

        if export_price >= 0.5:
            slots = [
                {
                    "gridImportW": 0,
                    "gridExportW": 6000,
                    "batteryChargeW": 0,
                    "batteryDischargeW": 6000,
                    "batterySocPercent": 58,
                }
                for _index in range(12)
            ]
        else:
            slots = [
                {
                    "gridImportW": 1200 if import_price >= 0 else 0,
                    "gridExportW": 0,
                    "batteryChargeW": 500,
                    "batteryDischargeW": 0,
                    "batterySocPercent": 56,
                }
                for _index in range(12)
            ]

        return {
            "ok": True,
            "phase": call.data.get("phase"),
            "plan_id": call.data.get("plan_id"),
            "result": {"slots": slots},
        }

    hass.services.async_register(
        DOMAIN,
        "optimize",
        optimize,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "ai_advice",
        ai_advice,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, "force_trip_import_due", force_trip_import_due)
    hass.services.async_register(DOMAIN, "seed_due_forecast_snapshot", seed_due_forecast_snapshot)
    hass.services.async_register(DOMAIN, "seed_thermal_model_sample", seed_thermal_model_sample)
    hass.services.async_register(DOMAIN, "seed_enphase_command_rate_limit", seed_enphase_command_rate_limit)
    hass.services.async_register("persistent_notification", "create", capture_persistent_notification)
    return True
PY

cat > "$TMP_DIR/configuration.yaml" <<'YAML'
default_config:

fake_haeo:

input_number:
  import_price:
    name: Import price
    min: -5
    max: 5
    step: 0.001
    initial: 0.25
  export_price:
    name: Export price
    min: -5
    max: 5
    step: 0.001
    initial: 0.08
  pv_forecast:
    name: PV forecast
    min: 0
    max: 20
    step: 0.1
    initial: 2.5
  baseline_load:
    name: Baseline load
    min: 0
    max: 20
    step: 0.1
    initial: 1.2
  battery_soc:
    name: Battery SOC
    min: 0
    max: 100
    step: 1
    initial: 55
  ev_soc:
    name: EV SOC
    min: 0
    max: 100
    step: 1
    initial: 40
  ev_target_soc:
    name: EV target SOC
    min: 0
    max: 100
    step: 1
    initial: 50
  diagnostics_response_seen:
    name: Diagnostics response seen
    min: 0
    max: 1
    step: 1
    initial: 0
  climate_target_low:
    name: Climate target low
    min: 5
    max: 35
    step: 0.5
    initial: 18
  climate_target_high:
    name: Climate target high
    min: 5
    max: 35
    step: 0.5
    initial: 24
  fake_indoor_temperature:
    name: Fake indoor temperature
    min: 5
    max: 35
    step: 0.5
    initial: 21
  daikin_power:
    name: Daikin power
    min: 0
    max: 10
    step: 0.1
    initial: 1.7

input_boolean:
  climate_manual_override:
    name: Climate manual override
  climate_change_from_scheduler:
    name: Climate change from scheduler
  fake_heater:
    name: Fake heater
  ev_connected:
    name: EV connected
    initial: true
  ev_smart_charging_start:
    name: EV Smart Charging start
    initial: false
  ev_smart_charging_stop:
    name: EV Smart Charging stop
    initial: true

input_datetime:
  ev_ready_by:
    name: EV ready by
    has_date: false
    has_time: true
    initial: "06:00"

input_text:
  planner_plan_status_seen:
    name: Planner plan status seen
    initial: unknown
  planner_data_healthy_seen:
    name: Planner data healthy seen
    initial: unknown
  planner_takeover_active_seen:
    name: Planner takeover active seen
    initial: unknown
  planner_dry_run_seen:
    name: Planner dry run seen
    initial: unknown
  planner_enabled_seen:
    name: Planner enabled seen
    initial: unknown
  planner_ai_enabled_seen:
    name: Planner AI enabled seen
    initial: unknown
  planner_replan_button_seen:
    name: Planner replan button seen
    initial: unknown
  planner_restore_notification_seen:
    name: Planner restore notification seen
    initial: unknown
  diagnostics_data_token_seen:
    name: Diagnostics data token seen
    initial: unknown
  diagnostics_data_address_seen:
    name: Diagnostics data address seen
    initial: unknown
  diagnostics_option_token_seen:
    name: Diagnostics option token seen
    initial: unknown

input_select:
  fake_person:
    name: Fake person
    options:
      - home
      - not_home
    initial: home
  enphase_profile:
    name: Enphase profile
    options:
      - AI Optimisation
      - Self-Consumption
      - Full Backup
    initial: AI Optimisation

climate:
  - platform: generic_thermostat
    name: Fake Daikin
    heater: input_boolean.fake_heater
    target_sensor: input_number.fake_indoor_temperature
    min_temp: 5
    max_temp: 35
    target_temp: 21

script:
  haeo_optimize:
    sequence:
      - delay: "00:00:00"

template:
  - sensor:
      - name: Smoke import price forecast
        state: "{{ states('input_number.import_price') }}"
        attributes:
          unitOfMeasurement: "c/kWh"
          confidence: "{{ 0.94 }}"
          detailedForecast: "{{ [{'perKwh': ((states('input_number.import_price') | float) * 100) | round(3)}, {'perKwh': 31}, {'perKwh': 32}, {'perKwh': 33}, {'perKwh': 34}, {'perKwh': 35}, {'perKwh': 36}, {'perKwh': 37}, {'perKwh': 38}, {'perKwh': 39}, {'perKwh': 40}, {'perKwh': 41}] }}"
      - name: Smoke export price forecast
        state: "{{ states('input_number.export_price') }}"
        attributes:
          unitOfMeasurement: "c/kWh"
          confidence: "{{ 0.93 }}"
          detailedForecast: "{{ [{'perKwh': ((states('input_number.export_price') | float) * 100) | round(3)}, {'perKwh': 9}, {'perKwh': 10}, {'perKwh': 11}, {'perKwh': 12}, {'perKwh': 13}, {'perKwh': 14}, {'perKwh': 15}, {'perKwh': 16}, {'perKwh': 17}, {'perKwh': 18}, {'perKwh': 19}] }}"
      - name: Smoke PV forecast series
        state: "{{ states('input_number.pv_forecast') }}"
        attributes:
          unitOfMeasurement: "W"
          confidence: "{{ 0.92 }}"
          detailedForecast: "{{ [{'prediction': {'watts': 2500}}, {'prediction': {'watts': 3000}}, {'prediction': {'watts': 3500}}, {'prediction': {'watts': 4000}}, {'prediction': {'watts': 4500}}, {'prediction': {'watts': 5000}}, {'prediction': {'watts': 4500}}, {'prediction': {'watts': 4000}}, {'prediction': {'watts': 3500}}, {'prediction': {'watts': 3000}}, {'prediction': {'watts': 2500}}, {'prediction': {'watts': 2000}}] }}"
      - name: Smoke baseline load forecast series
        state: "{{ states('input_number.baseline_load') }}"
        attributes:
          unitOfMeasurement: "W"
          confidence: "{{ 0.91 }}"
          detailedForecast: "{{ [{'watts': 1200}, {'watts': 1400}, {'watts': 1600}, {'watts': 1800}, {'watts': 2000}, {'watts': 2200}, {'watts': 2000}, {'watts': 1800}, {'watts': 1600}, {'watts': 1400}, {'watts': 1200}, {'watts': 1000}] }}"
      - name: Smoke weather forecast
        state: "sunny"
        attributes:
          nativeTemperature: "{{ 21 }}"
          temperatureUnit: "C"
          confidence: "{{ 0.90 }}"
          detailedForecast: "{{ [{'nativeTemperature': 19.0}, {'nativeTemperature': 20.0}, {'nativeTemperature': 21.0}, {'nativeTemperature': 22.0}, {'nativeTemperature': 23.0}, {'nativeTemperature': 24.0}] }}"

automation:
  - alias: Fake climate conflict
    id: fake_climate_conflict
    mode: single
    triggers:
      - trigger: homeassistant
        event: start
    actions:
      - delay: "00:00:00"
  - alias: HA Energy Planner service smoke
    id: ha_energy_planner_service_smoke
    mode: single
    triggers:
      - trigger: homeassistant
        event: start
    actions:
      - delay: "00:00:08"
      - action: input_boolean.turn_on
        data:
          entity_id: input_boolean.climate_change_from_scheduler
      - action: climate.set_hvac_mode
        data:
          entity_id: climate.fake_daikin
          hvac_mode: heat
      - delay: "00:00:01"
      - action: input_boolean.turn_off
        data:
          entity_id: input_boolean.climate_change_from_scheduler
      - action: automation.turn_on
        data:
          entity_id: automation.fake_climate_conflict
      - action: input_number.set_value
        data:
          entity_id: input_number.fake_indoor_temperature
          value: 17.5
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.10
      - delay: "00:00:02"
      - action: ha_energy_planner.replan
      - delay: "00:00:07"
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_hvac_precondition_restore
      - delay: "00:00:02"
      - action: input_number.set_value
        data:
          entity_id: input_number.fake_indoor_temperature
          value: 21
      - action: automation.turn_on
        data:
          entity_id: automation.fake_climate_conflict
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.60
      - delay: "00:00:02"
      - action: ha_energy_planner.replan
      - delay: "00:00:07"
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_hvac_suppression_restore
      - delay: "00:00:02"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.25
      - action: input_select.select_option
        data:
          entity_id: input_select.fake_person
          option: not_home
      - action: ha_energy_planner.replan
      - delay: "00:00:20"
      - action: ha_energy_planner.set_manual_hvac_override
        data:
          duration_minutes: 10
          reason: docker_smoke_manual_override
      - delay: "00:00:01"
      - action: input_select.select_option
        data:
          entity_id: input_select.fake_person
          option: home
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: AI Optimisation
      - action: input_number.set_value
        data:
          entity_id: input_number.ev_soc
          value: 35
      - action: ha_energy_planner.set_ev_ready_by
        data:
          ready_by: "23:45:00"
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: -0.05
      - action: input_number.set_value
        data:
          entity_id: input_number.ev_soc
          value: 35
      - delay: "00:00:02"
      - action: ha_energy_planner.replan
      - delay: "00:00:08"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.25
      - action: input_number.set_value
        data:
          entity_id: input_number.ev_soc
          value: 80
      - action: input_boolean.turn_off
        data:
          entity_id: input_boolean.ev_connected
      - delay: "00:00:01"
      - action: input_number.set_value
        data:
          entity_id: input_number.ev_soc
          value: 72
      - delay: "00:00:01"
      - action: input_boolean.turn_on
        data:
          entity_id: input_boolean.ev_connected
      - delay: "00:00:05"
      - action: fake_haeo.force_trip_import_due
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_number.set_value
        data:
          entity_id: input_number.pv_forecast
          value: 2.0
      - action: input_number.set_value
        data:
          entity_id: input_number.baseline_load
          value: 2.0
      - action: fake_haeo.seed_due_forecast_snapshot
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_number.set_value
        data:
          entity_id: input_number.fake_indoor_temperature
          value: 20.5
      - action: input_number.set_value
        data:
          entity_id: input_number.daikin_power
          value: 1.7
      - action: fake_haeo.seed_thermal_model_sample
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: Self-Consumption
      - delay: "00:00:01"
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_restore
      - delay: "00:00:03"
      - action: input_number.set_value
        data:
          entity_id: input_number.ev_soc
          value: 80
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.25
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.08
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: Self-Consumption
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_number.set_value
        data:
          entity_id: input_number.ev_soc
          value: 80
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.05
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.60
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: AI Optimisation
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.25
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.08
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_final_restore
      - delay: "00:00:03"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.05
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.60
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: AI Optimisation
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_second_arbitrage_restore
      - delay: "00:00:03"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.05
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.60
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: AI Optimisation
      - action: fake_haeo.seed_enphase_command_rate_limit
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: button.press
        data:
          entity_id: button.system_restore_safe_state
      - delay: "00:00:02"
      - action: switch.turn_on
        data:
          entity_id: switch.system_dry_run
      - delay: "00:00:02"
      - action: switch.turn_off
        data:
          entity_id: switch.system_enabled
      - delay: "00:00:02"
      - action: switch.turn_on
        data:
          entity_id: switch.system_enabled
      - delay: "00:00:02"
      - action: switch.turn_on
        data:
          entity_id: switch.ai_ai_enabled
      - delay: "00:00:02"
      - action: button.press
        data:
          entity_id: button.system_replan
      - delay: "00:00:05"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_plan_status_seen
          value: "{{ states('sensor.system_plan_status') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_data_healthy_seen
          value: "{{ states('binary_sensor.system_data_health') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_takeover_active_seen
          value: "{{ states('binary_sensor.system_takeover_active') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_dry_run_seen
          value: "{{ states('switch.system_dry_run') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_enabled_seen
          value: "{{ states('switch.system_enabled') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_ai_enabled_seen
          value: "{{ states('switch.ai_ai_enabled') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_replan_button_seen
          value: "{{ states('button.system_replan') }}"
      - action: ha_energy_planner.export_diagnostics
        response_variable: hep_diagnostics
      - action: input_text.set_value
        data:
          entity_id: input_text.diagnostics_data_token_seen
          value: "{{ hep_diagnostics.entry.data.api_token }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.diagnostics_data_address_seen
          value: "{{ hep_diagnostics.entry.data.home_address }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.diagnostics_option_token_seen
          value: "{{ hep_diagnostics.entry.options.access_token }}"
      - action: input_number.set_value
        data:
          entity_id: input_number.diagnostics_response_seen
          value: "{{ 1 if hep_diagnostics.plan is defined else 0 }}"

logger:
  default: warning
  logs:
    custom_components.ha_energy_planner: debug
YAML

cat > "$TMP_DIR/.storage/core.config_entries" <<'JSON'
{
  "version": 1,
  "minor_version": 5,
  "key": "core.config_entries",
  "data": {
    "entries": [
      {
        "created_at": "2026-06-27T00:00:00+00:00",
        "data": {
          "api_token": "docker-smoke-secret-token",
          "home_address": "1 Secret Smoke Street"
        },
        "disabled_by": null,
        "discovery_keys": {},
        "domain": "ha_energy_planner",
        "entry_id": "01KW3HATESTENERGYPLANNER000",
        "minor_version": 1,
        "modified_at": "2026-06-27T00:00:00+00:00",
        "options": {
          "planning_horizon_hours": 24,
          "planning_interval_minutes": 5,
          "default_ready_by": "07:00",
          "battery_min_soc_percent": 10.0,
          "ev_min_soc_percent": 40.0,
          "ev_max_soc_percent": 90.0,
          "ev_fallback_target_soc_percent": 80.0,
          "ev_charge_rate_kw": 7.0,
          "ev_soc_per_kwh": 5.0,
          "grid_import_limit_kw": 10.0,
          "grid_export_limit_kw": 10.0,
          "occupied_temperature_tolerance_percent": 10.0,
          "hvac_suppression_min_price_delta": 0.20,
          "hvac_precondition_lead_minutes": 30,
          "hvac_precondition_min_price_delta": 0.20,
          "hvac_min_cycle_minutes": 20,
          "manual_hvac_override_minutes": 120,
          "enphase_profile_min_hold_minutes": 30,
          "planner_enabled": true,
          "dry_run": false,
          "ev_control_enabled": true,
          "climate_control_enabled": true,
          "enphase_control_enabled": true,
          "ai_enabled": false,
          "price_freshness_minutes": 30,
          "forecast_freshness_minutes": 120,
          "material_change_threshold_percent": 5.0,
          "enphase_minimum_savings": 0.25,
          "command_rate_limit_seconds": 1,
          "priority_weights": "cost,comfort,ev_readiness,battery_reserve,solar_self_consumption,carbon",
          "access_token": "docker-smoke-option-token"
        },
        "pref_disable_new_entities": false,
        "pref_disable_polling": false,
        "source": "user",
        "subentries": [
          {
            "data": {},
            "subentry_id": "haep_system",
            "subentry_type": "system",
            "title": "System",
            "unique_id": null
          },
          {
            "data": {
              "haeo_optimize_service": "fake_haeo.optimize",
              "amber_import_price_entity": "sensor.smoke_import_price_forecast",
              "amber_export_price_entity": "sensor.smoke_export_price_forecast",
              "pv_forecast_entity": "sensor.smoke_pv_forecast_series",
              "baseline_load_forecast_entity": "sensor.smoke_baseline_load_forecast_series",
              "battery_soc_entity": "input_number.battery_soc"
            },
            "subentry_id": "haep_energy",
            "subentry_type": "energy",
            "title": "Energy",
            "unique_id": null
          },
          {
            "data": {
              "person_entities": "input_select.fake_person"
            },
            "subentry_id": "haep_presence",
            "subentry_type": "presence",
            "title": "Presence",
            "unique_id": null
          },
          {
            "data": {
              "daikin_climate_entity": "climate.fake_daikin",
              "daikin_power_entity": "input_number.daikin_power",
              "climate_automation_entities": "automation.fake_climate_conflict",
              "climate_change_from_scheduler_entity": "input_boolean.climate_change_from_scheduler",
              "climate_manual_override_entity": "input_boolean.climate_manual_override",
              "climate_target_low_entity": "input_number.climate_target_low",
              "climate_target_high_entity": "input_number.climate_target_high",
              "weather_entity": "sensor.smoke_weather_forecast"
            },
            "subentry_id": "haep_climate",
            "subentry_type": "climate",
            "title": "Climate",
            "unique_id": null
          },
          {
            "data": {
              "enphase_profile_entity": "input_select.enphase_profile",
              "enphase_profile_control_service": "input_select.select_option",
              "enphase_ai_profile": "AI Optimisation",
              "enphase_self_consumption_profile": "Self-Consumption",
              "enphase_full_backup_profile": "Full Backup"
            },
            "subentry_id": "haep_enphase",
            "subentry_type": "enphase",
            "title": "Enphase",
            "unique_id": null
          },
          {
            "data": {
              "ai_advisor_service": "fake_haeo.ai_advice"
            },
            "subentry_id": "haep_ai",
            "subentry_type": "ai",
            "title": "AI",
            "unique_id": null
          },
          {
            "data": {
              "ev_soc_entity": "input_number.ev_soc",
              "ev_charging_entity": "",
              "ev_connected_entity": "input_boolean.ev_connected",
              "ev_smart_charging_entity": "input_boolean.ev_smart_charging_start",
              "ev_smart_charging_start_entity": "input_boolean.ev_smart_charging_start",
              "ev_smart_charging_stop_entity": "input_boolean.ev_smart_charging_stop",
              "ev_smart_charging_target_soc_entity": "input_number.ev_target_soc",
              "ev_smart_charging_ready_by_entity": "input_datetime.ev_ready_by"
            },
            "subentry_id": "haep_ev",
            "subentry_type": "ev",
            "title": "EV",
            "unique_id": null
          }
        ],
        "title": "HA Energy Planner",
        "unique_id": "ha_energy_planner",
        "version": 1
      }
    ]
  }
}
JSON

cat > "$TMP_DIR/.storage/ha_energy_planner_state" <<'JSON'
{
  "version": 1,
  "minor_version": 1,
  "key": "ha_energy_planner_state",
  "data": {
    "ownership": {
      "climate_automations": {
        "automation.fake_climate_conflict": "on"
      }
    },
    "production": {
      "armed": true,
      "armed_at": "2026-06-27T00:00:00+00:00",
      "armed_reason": "docker_smoke",
      "acknowledged_at": "2026-06-27T00:00:00+00:00",
      "dry_run_ready_cycles": 3
    }
  }
}
JSON

set +e
docker run --rm \
  -v "$TMP_DIR:/config" \
  --entrypoint timeout \
  ghcr.io/home-assistant/home-assistant:stable \
  240s python3 -m homeassistant --config /config >"$LOG_FILE" 2>&1
STATUS=$?
set -e

if [[ "$STATUS" != "0" && "$STATUS" != "124" ]]; then
  cat "$LOG_FILE"
  exit "$STATUS"
fi

if ! grep -q "Finished fetching ha_energy_planner data.*success: True" "$LOG_FILE"; then
  cat "$LOG_FILE"
  echo "ha_energy_planner did not complete an initial coordinator refresh" >&2
  exit 1
fi

if grep -E "(ERROR|CRITICAL).*ha_energy_planner|custom_components\\.ha_energy_planner.*(Traceback|Exception)" "$LOG_FILE"; then
  cat "$LOG_FILE"
  exit 1
fi

python3 - <<'PY' "$TMP_DIR"
from __future__ import annotations

import json
import sys
from pathlib import Path

config_dir = Path(sys.argv[1])
storage = config_dir / ".storage"


def load_storage(name: str) -> dict:
    path = storage / name
    if not path.exists():
        raise SystemExit(f"Missing expected Home Assistant storage file: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


entity_registry = load_storage("core.entity_registry")
entities = {
    entry["entity_id"]
    for entry in entity_registry["data"]["entities"]
    if entry.get("platform") == "ha_energy_planner"
}
expected_entities = {
    "sensor.system_next_action",
    "sensor.system_plan_status",
    "sensor.energy_estimated_daily_cost",
    "sensor.energy_forecast_confidence",
    "binary_sensor.system_data_health",
    "binary_sensor.system_takeover_active",
    "switch.system_enabled",
    "switch.system_dry_run",
    "switch.ai_ai_enabled",
    "button.system_replan",
    "button.system_restore_safe_state",
}
missing = expected_entities - entities
if missing:
    raise SystemExit(f"Missing HA Energy Planner entities: {sorted(missing)}")

device_registry = load_storage("core.device_registry")
device_identifiers = {
    tuple(identifier)
    for entry in device_registry["data"]["devices"]
    for identifier in entry.get("identifiers", [])
    if identifier and identifier[0] == "ha_energy_planner"
}
expected_device_identifiers = {
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000_system"),
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000_energy"),
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000_climate"),
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000_enphase"),
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000_ai"),
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000_ev"),
}
missing_devices = expected_device_identifiers - device_identifiers
if missing_devices:
    raise SystemExit(f"Missing HA Energy Planner device registry entries: {sorted(missing_devices)}")

planner_store = load_storage("ha_energy_planner_state")
store_data = planner_store["data"]
active_plan = store_data.get("active_plan")
if not active_plan or active_plan.get("horizon_hours") != 24 or active_plan.get("interval_minutes") != 5:
    raise SystemExit("Planner Store did not persist a valid active 24h/5m plan")
if active_plan.get("mode") not in {"DISABLED", "DRY_RUN", "ACTIVE_HEALTHY", "ACTIVE_DEGRADED"}:
    raise SystemExit(f"Unexpected persisted planner mode: {active_plan.get('mode')}")
if active_plan.get("mode") != "DRY_RUN":
    raise SystemExit(f"Dry-run switch entity did not produce a DRY_RUN active plan: {active_plan.get('mode')}")
if not store_data.get("haeo_runs"):
    raise SystemExit("Planner Store did not persist HAEO run metadata")
haeo_evidence_counts = [
    run.get("baseline", {}).get("evidence_counts", {})
    for run in store_data.get("haeo_runs", [])
    if isinstance(run.get("baseline", {}).get("evidence_counts", {}), dict)
]
haeo_second_pass_evidence_counts = [
    (run.get("second_pass") or {}).get("evidence_counts", {})
    for run in store_data.get("haeo_runs", [])
    if isinstance((run.get("second_pass") or {}).get("evidence_counts", {}), dict)
]
if not any(
    counts.get("haeo_grid_import_forecast_kw", 0) >= 4
    and counts.get("haeo_battery_charge_forecast_kw", 0) >= 4
    and counts.get("haeo_battery_soc_forecast_percent", 0) >= 4
    for counts in haeo_evidence_counts
):
    raise SystemExit(f"Planner Store did not persist parsed HAEO grid-charge evidence counts: {haeo_evidence_counts}")
if not any(
    counts.get("haeo_grid_export_forecast_kw", 0) >= 4
    and counts.get("haeo_battery_discharge_forecast_kw", 0) >= 4
    for counts in haeo_evidence_counts
):
    raise SystemExit(f"Planner Store did not persist parsed HAEO export/discharge evidence counts: {haeo_evidence_counts}")
if not any(
    counts.get("haeo_grid_import_forecast_kw", 0) >= 4
    and counts.get("haeo_grid_export_forecast_kw", 0) >= 4
    and counts.get("haeo_battery_charge_forecast_kw", 0) >= 4
    and counts.get("haeo_battery_discharge_forecast_kw", 0) >= 4
    for counts in haeo_second_pass_evidence_counts
):
    raise SystemExit(
        "Planner Store did not persist parsed second-pass HAEO evidence counts: "
        f"{haeo_second_pass_evidence_counts}"
    )
if "discovery" not in store_data:
    raise SystemExit("Planner Store did not persist discovery data")
ai_discovery = store_data.get("discovery", {}).get("ai", {})
if not ai_discovery.get("supported") or ai_discovery.get("details", {}).get("service") != "fake_haeo.ai_advice":
    raise SystemExit(f"Planner discovery did not record the local AI advisor service as supported: {ai_discovery}")
ai_recommendations = store_data.get("ai_recommendations", [])
if not any(
    recommendation.get("status") == "accepted"
    and recommendation.get("service_called") == "fake_haeo.ai_advice"
    and recommendation.get("accepted", {}).get("suggested_precondition_lead_minutes") == 45
    and recommendation.get("accepted", {}).get("suggested_forecast_buffer_percent") == 12
    and recommendation.get("accepted", {}).get("suggested_takeover_savings_threshold") == 0.33
    and recommendation.get("accepted", {}).get("confidence") == 0.77
    for recommendation in ai_recommendations
):
    raise SystemExit(f"Planner Store did not persist accepted bounded local AI advice: {ai_recommendations}")
snapshots = store_data.get("forecast_snapshots", [])
if not snapshots:
    raise SystemExit("Planner Store did not persist forecast snapshots")
latest_snapshot = snapshots[-1]
if not any(
    (snapshot.get("ai") or {}).get("status") == "accepted"
    and (snapshot.get("ai") or {}).get("accepted_fields") == [
        "alerts",
        "confidence",
        "reasoning_summary",
        "suggested_forecast_buffer_percent",
        "suggested_precondition_lead_minutes",
        "suggested_takeover_savings_threshold",
    ]
    and (snapshot.get("ai") or {}).get("service_called") == "fake_haeo.ai_advice"
    for snapshot in snapshots
):
    raise SystemExit("Forecast snapshots did not persist bounded local AI advice metadata")
snapshot_actions = [
    action
    for snapshot in snapshots
    for action in snapshot.get("actions", [])
    if isinstance(action, dict)
]
if not any(
    str(action.get("action_id", "")).endswith("-ev-minimum-soc")
    and action.get("kind") == "ev_schedule"
    and action.get("desired_state", {}).get("ready_by") == "23:45"
    and action.get("desired_state", {}).get("target_soc_percent", 0) >= 80
    and action.get("desired_state", {}).get("allocated_slots")
    for action in snapshot_actions
):
    raise SystemExit("Forecast snapshots did not persist the active EV schedule action with runtime ready-by metadata")
if not any(
    str(action.get("action_id", "")).endswith("-ev-minimum-soc")
    and action.get("kind") == "ev_schedule"
    and any(
        isinstance(slot, dict) and float(slot.get("import_price", 0.0)) < 0
        for slot in action.get("desired_state", {}).get("allocated_slots", [])
    )
    for action in snapshot_actions
):
    raise SystemExit("Forecast snapshots did not persist an active EV schedule allocated to a negative import-price slot")
if not any(
    str(action.get("action_id", "")).endswith("-enphase-arbitrage-profile")
    and action.get("kind") == "set_profile"
    and action.get("desired_state", {}).get("profile") == "Full Backup"
    and action.get("desired_state", {}).get("arbitrage_source") in {
        "haeo_battery_arbitrage_value",
        "haeo_export_value",
    }
    and float(action.get("expected_cost_delta") or 0) >= 0.25
    for action in snapshot_actions
):
    raise SystemExit("Forecast snapshots did not persist an Enphase arbitrage action backed by HAEO value evidence")
if not latest_snapshot.get("forecast_training_slots"):
    raise SystemExit("Forecast snapshot did not include compact forecast training slots")
forecast_training_slots = latest_snapshot.get("forecast_training_slots", [])
pv_training = [slot.get("pv_forecast_kw") for slot in forecast_training_slots[:4]]
if pv_training != [2.5, 3.0, 3.5, 4.0]:
    raise SystemExit(f"Forecast snapshot did not use HA template PV forecast attributes: {pv_training}")
baseline_training = [slot.get("baseline_load_forecast_kw") for slot in forecast_training_slots[:4]]
if baseline_training != [1.2, 1.4, 1.6, 1.8]:
    raise SystemExit(f"Forecast snapshot did not use HA template load forecast attributes: {baseline_training}")
if not any(
    len(snapshot.get("preview", [])) >= 4
    and [slot.get("import_price") for slot in snapshot["preview"][:4]][1:] == [0.31, 0.32, 0.33]
    and [slot.get("export_price") for slot in snapshot["preview"][:4]][1:] == [0.09, 0.10, 0.11]
    for snapshot in snapshots
):
    raise SystemExit("Forecast preview did not use HA template Amber price forecast attributes")
weather_preview = [slot.get("outdoor_temperature_forecast_c") for slot in latest_snapshot.get("preview", [])[:4]]
if weather_preview != [19.0, 20.0, 21.0, 22.0]:
    raise SystemExit(f"Forecast preview did not use HA template weather forecast attributes: {weather_preview}")
if "forecast_calibration" not in latest_snapshot:
    raise SystemExit("Forecast snapshot did not include calibration metadata")
if "thermal_model" not in latest_snapshot:
    raise SystemExit("Forecast snapshot did not include thermal model metadata")
if latest_snapshot.get("trip_history", {}).get("recorder_import_reason") not in {
    "recorder_ev_entities_not_configured",
    "recorder_import_recent",
    "recorder_imported",
    "recorder_no_new_trips",
} and not str(latest_snapshot.get("trip_history", {}).get("recorder_import_reason", "")).startswith("recorder_import_unavailable:"):
    raise SystemExit(f"Unexpected Recorder import reason metadata: {latest_snapshot.get('trip_history')}")
if not any(
    snapshot.get("trip_history", {}).get("recorder_import_reason") == "recorder_imported"
    and snapshot.get("trip_history", {}).get("record_count", 0) >= 1
    for snapshot in snapshots
):
    raise SystemExit("Smoke run did not import a compact EV trip from Home Assistant Recorder")
trip_records = store_data.get("trip_history", {}).get("records", [])
if not any(
    record.get("source") == "recorder"
    and record.get("start_soc_percent") == 80.0
    and record.get("end_soc_percent") == 72.0
    for record in trip_records
    if isinstance(record, dict)
):
    raise SystemExit(f"Recorder import did not persist the expected EV trip record: {trip_records}")
if "forecast_calibration" not in store_data:
    raise SystemExit("Planner Store did not initialize forecast calibration state")
forecast_calibration = store_data.get("forecast_calibration", {})
seen_sample_ids = forecast_calibration.get("_seen_sample_ids", [])
if not any("docker_smoke_calibration" in str(sample_id) for sample_id in seen_sample_ids):
    raise SystemExit(f"Forecast calibration did not consume the smoke due snapshot: {seen_sample_ids}")
for field in ("pv_forecast_kw", "baseline_load_forecast_kw"):
    if forecast_calibration.get(field, {}).get("sample_count", 0) < 1:
        raise SystemExit(f"Forecast calibration did not store samples for {field}: {forecast_calibration}")
if "thermal_model" not in store_data:
    raise SystemExit("Planner Store did not initialize thermal model state")
thermal_model = store_data.get("thermal_model", {})
active_model = thermal_model.get("active_hvac_load_kw", {})
if active_model.get("sample_count", 0) < 1:
    raise SystemExit(f"Thermal model did not record an active HVAC power sample: {thermal_model}")
try:
    active_average = float(active_model.get("average"))
except (TypeError, ValueError):
    active_average = 0.0
if active_average < 1.7:
    raise SystemExit(f"Thermal model active HVAC load average was not sourced from HA power state: {thermal_model}")
overrides = store_data.get("overrides", [])
if not any(item.get("reason") == "docker_smoke_manual_override" for item in overrides):
    raise SystemExit("set_manual_hvac_override service did not persist the smoke override")
outcomes = store_data.get("outcomes", [])
restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state" and "docker_smoke_restore" in item.get("reason", "")
]
if not restore_outcomes:
    raise SystemExit("restore_safe_state service did not persist the smoke outcome")
if not any(
    "ev_saved_state_restored" in item.get("reason", "")
    and item.get("post_state", {}).get("ev_smart_charging_start_entity") == "off"
    for item in restore_outcomes
):
    raise SystemExit("restore_safe_state did not restore the EV Smart Charging helper to its pre-takeover state")
if not any("hvac_automation_state_restored" in item.get("reason", "") for item in restore_outcomes):
    raise SystemExit("restore_safe_state did not restore the mapped climate automation state")
if not any(
    (
        "enphase_profile_applied" in item.get("reason", "")
        or "already_in_desired_profile" in item.get("reason", "")
    )
    and item.get("post_state", {}).get("enphase_profile_entity") == "AI Optimisation"
    for item in restore_outcomes
):
    raise SystemExit("restore_safe_state did not leave the mapped Enphase profile at AI Optimisation")
if store_data.get("ownership"):
    raise SystemExit(f"restore_safe_state did not clear planner ownership: {store_data.get('ownership')}")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-hvac-expensive-period-suppression")
    and item.get("reason") == "hvac_automations_suppressed"
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "off"
    for item in outcomes
):
    raise SystemExit("Active-mode HVAC expensive-period suppression did not disable the mapped automation")
suppression_restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state" and "docker_smoke_hvac_suppression_restore" in item.get("reason", "")
]
if not any(
    "hvac_automation_state_restored" in item.get("reason", "")
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "on"
    for item in suppression_restore_outcomes
):
    raise SystemExit("HVAC suppression restore did not re-enable the mapped automation")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-hvac-precondition-before-expensive-period")
    and item.get("reason") == "hvac_action_applied"
    and item.get("post_state", {}).get("daikin_climate_entity") == "heat"
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "off"
    for item in outcomes
):
    raise SystemExit("Active-mode HVAC preconditioning did not control the climate entity and suppress automation")
precondition_restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state" and "docker_smoke_hvac_precondition_restore" in item.get("reason", "")
]
if not any(
    "hvac_automation_state_restored" in item.get("reason", "")
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "on"
    for item in precondition_restore_outcomes
):
    raise SystemExit("HVAC precondition restore did not re-enable the mapped automation")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-hvac-away-off")
    and item.get("reason") == "hvac_action_applied"
    and item.get("post_state", {}).get("daikin_climate_entity") == "off"
    for item in outcomes
):
    raise SystemExit("Active-mode HVAC away-off action was not applied in the smoke run")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-ev-minimum-soc")
    and item.get("reason") in {"input_boolean_turn_on_called", "already_in_desired_state"}
    for item in outcomes
):
    raise SystemExit("Active-mode EV Smart Charging action was not applied in the smoke run")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-ev-minimum-soc")
    and item.get("post_state", {}).get("ev_smart_charging_ready_by_entity") == "23:45:00"
    for item in outcomes
):
    raise SystemExit("set_ev_ready_by service did not apply the normalized runtime ready-by value to EV Smart Charging")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-enphase-arbitrage-profile")
    and item.get("reason") == "enphase_profile_applied"
    and item.get("post_state", {}).get("enphase_profile_entity") == "Full Backup"
    for item in outcomes
):
    raise SystemExit("Active-mode Enphase arbitrage profile action was not applied in the smoke run")
enphase_arbitrage_outcome_indexes = [
    index
    for index, item in enumerate(outcomes)
    if item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-enphase-arbitrage-profile")
    and item.get("reason") == "enphase_profile_applied"
    and item.get("post_state", {}).get("enphase_profile_entity") == "Full Backup"
]
if len(enphase_arbitrage_outcome_indexes) < 2:
    raise SystemExit(
        "Active-mode Enphase arbitrage was not applied across two price-control cycles: "
        f"{enphase_arbitrage_outcome_indexes}"
    )
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-enphase-restore-ai")
    and item.get("reason") == "enphase_profile_applied"
    and item.get("post_state", {}).get("enphase_profile_entity") == "AI Optimisation"
    for item in outcomes
):
    raise SystemExit("Active-mode Enphase restore-AI action was not applied in the smoke run")
final_restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state" and "docker_smoke_final_restore" in item.get("reason", "")
]
if not final_restore_outcomes:
    raise SystemExit("Final restore_safe_state service did not persist the smoke outcome")
if not any(
    "enphase_profile_applied" in item.get("reason", "")
    and item.get("post_state", {}).get("enphase_profile_entity") == "AI Optimisation"
    for item in final_restore_outcomes
):
    raise SystemExit("Final restore_safe_state did not restore Enphase profile after active arbitrage smoke action")
second_arbitrage_restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state" and "docker_smoke_second_arbitrage_restore" in item.get("reason", "")
]
if not any(
    "enphase_profile_applied" in item.get("reason", "")
    and item.get("post_state", {}).get("enphase_profile_entity") == "AI Optimisation"
    for item in second_arbitrage_restore_outcomes
):
    raise SystemExit("Second active-mode Enphase arbitrage cycle was not restored to AI Optimisation")
enphase_restore_ai_index = next(
    (
        index
        for index, item in enumerate(outcomes)
        if item.get("result") == "applied"
        and str(item.get("action_id", "")).endswith("-enphase-restore-ai")
        and item.get("reason") == "enphase_profile_applied"
    ),
    None,
)
final_restore_index = next(
    (
        index
        for index, item in enumerate(outcomes)
        if item.get("action_id") == "restore_safe_state" and "docker_smoke_final_restore" in item.get("reason", "")
    ),
    None,
)
second_restore_index = next(
    (
        index
        for index, item in enumerate(outcomes)
        if item.get("action_id") == "restore_safe_state"
        and "docker_smoke_second_arbitrage_restore" in item.get("reason", "")
    ),
    None,
)
cooldown_index = next(
    (
        index
        for index, item in enumerate(outcomes)
        if item.get("result") == "rejected"
        and str(item.get("action_id", "")).endswith("-enphase-arbitrage-profile")
        and item.get("reason") == "device_command_rate_limited"
    ),
    None,
)
first_arbitrage_index = enphase_arbitrage_outcome_indexes[0]
second_arbitrage_index = enphase_arbitrage_outcome_indexes[1]
if not all(
    index is not None
    for index in (
        enphase_restore_ai_index,
        final_restore_index,
        second_restore_index,
        cooldown_index,
    )
):
    raise SystemExit("Smoke run did not persist all Enphase multi-cycle outcome markers")
if not (
    enphase_restore_ai_index
    < first_arbitrage_index
    < final_restore_index
    < second_arbitrage_index
    < second_restore_index
    < cooldown_index
):
    raise SystemExit(
        "Enphase multi-cycle outcomes were not persisted in the expected price-control order: "
        f"restore={enphase_restore_ai_index}, first={first_arbitrage_index}, final_restore={final_restore_index}, "
        f"second={second_arbitrage_index}, second_restore={second_restore_index}, cooldown={cooldown_index}"
    )
if not any(
    item.get("result") == "rejected"
    and str(item.get("action_id", "")).endswith("-enphase-arbitrage-profile")
    and item.get("reason") == "device_command_rate_limited"
    for item in outcomes
):
    raise SystemExit("Active-mode Enphase command cooldown did not reject a repeated arbitrage action")
if not any(
    item.get("action_id") == "restore_safe_state"
    and "button_pressed" in item.get("reason", "")
    for item in outcomes
):
    raise SystemExit("Restore-safe-state button entity did not persist a button_pressed restore outcome")

restore_state = load_storage("core.restore_state")


def restored_entity_state(entity_id: str) -> str | None:
    return next(
        (
            item.get("state", {}).get("state")
            for item in restore_state["data"]
            if item.get("state", {}).get("entity_id") == entity_id
        ),
        None,
    )


expected_helper_states = {
    "input_text.planner_plan_status_seen": "Current",
    "input_text.planner_data_healthy_seen": "off",
    "input_text.planner_takeover_active_seen": "off",
    "input_text.planner_dry_run_seen": "on",
    "input_text.planner_enabled_seen": "on",
    "input_text.planner_ai_enabled_seen": "on",
    "input_text.planner_restore_notification_seen": "ha_energy_planner_restore_safe_state",
    "input_text.diagnostics_data_token_seen": "**REDACTED**",
    "input_text.diagnostics_data_address_seen": "**REDACTED**",
    "input_text.diagnostics_option_token_seen": "**REDACTED**",
}
for entity_id, expected_state in expected_helper_states.items():
    actual_state = restored_entity_state(entity_id)
    if actual_state != expected_state:
        raise SystemExit(
            f"Unexpected captured HA Energy Planner entity state for {entity_id}: "
            f"{actual_state!r} != {expected_state!r}"
        )
replan_button_state = restored_entity_state("input_text.planner_replan_button_seen")
if replan_button_state in {None, "unknown", "unavailable"}:
    raise SystemExit(f"Replan button entity was not pressed through Home Assistant Core: {replan_button_state!r}")
diagnostics_response_state = next(
    (
        item.get("state", {}).get("state")
        for item in restore_state["data"]
        if item.get("state", {}).get("entity_id") == "input_number.diagnostics_response_seen"
    ),
    None,
)
if diagnostics_response_state != "1.0":
    raise SystemExit("export_diagnostics service response was not observed by the smoke automation")

print("HA Energy Planner entity/service storage assertions passed")
PY

echo "HA Energy Planner Docker smoke test passed"
