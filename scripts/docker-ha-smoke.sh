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
mkdir -p "$TMP_DIR/custom_components/fake_planner_test"
cat > "$TMP_DIR/custom_components/fake_planner_test/manifest.json" <<'JSON'
{
  "domain": "fake_planner_test",
  "name": "Fake Planner Test Helpers",
  "version": "0.1.0",
  "documentation": "https://example.invalid/fake-planner-test",
  "integration_type": "hub",
  "iot_class": "local_push"
}
JSON
cat > "$TMP_DIR/custom_components/fake_planner_test/__init__.py" <<'PY'
"""Test helpers for Docker smoke tests."""

from __future__ import annotations

from datetime import timedelta
import json
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util

DOMAIN = "fake_planner_test"
PLATFORMS: list[Platform] = []


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register smoke-test helper services."""

    async def force_ev_calibration_due(call: ServiceCall) -> None:
        """Mark HA Energy Planner EV calibration due for smoke validation."""
        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            model = dict(store.data.get("ev_charge_calibration", {}))
            model["trained_at"] = "1970-01-01T00:00:00+00:00"
            model.pop("last_attempt_at", None)
            await store.async_save_ev_charge_calibration(model)

    async def seed_due_forecast_snapshot(call: ServiceCall) -> None:
        """Seed one due forecast snapshot for smoke calibration validation."""
        valid_at = dt_util.utcnow()
        issued_at = valid_at - timedelta(minutes=5)
        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            await store.async_add_forecast_snapshot(
                {
                    "created_at": valid_at.isoformat(),
                    "plan_id": "docker_smoke_calibration",
                    "forecast_training_slots": [
                        {
                            "valid_at": valid_at.isoformat(),
                            "pv_forecast_kw_issued_at": issued_at.isoformat(),
                            "pv_forecast_kw": 1.0,
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

    async def seed_production_evidence(call: ServiceCall) -> None:
        """Bind smoke review evidence to the integration's current production contract."""
        from custom_components.ha_energy_planner.entry_data import combined_entry_data
        from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

        for entry in hass.config_entries.async_entries("ha_energy_planner"):
            coordinator = getattr(entry, "runtime_data", None)
            store = getattr(coordinator, "store", None)
            if store is None:
                continue
            production = dict(store.data.get("production", {}))
            production["dry_run_ready_cycles"] = 3
            production["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
                combined_entry_data(coordinator.entry),
                coordinator.options,
            )
            await store.async_save_production(production)

    async def assert_unsafe_arm_rejected(call: ServiceCall) -> None:
        """Verify an unsafe manual arm fails without emitting an expected HA error log."""
        from homeassistant.exceptions import HomeAssistantError

        coordinators = [
            getattr(entry, "runtime_data", None)
            for entry in hass.config_entries.async_entries("ha_energy_planner")
        ]
        coordinators = [coordinator for coordinator in coordinators if coordinator is not None]
        if not coordinators:
            raise RuntimeError("Energy planner coordinator was not loaded")
        for coordinator in coordinators:
            try:
                await coordinator.async_operator_arm_production_control("docker_smoke_reject_unsafe_arm")
            except HomeAssistantError:
                continue
            raise RuntimeError("Unsafe production arm unexpectedly succeeded")

    async def capture_persistent_notification(call: ServiceCall) -> None:
        """Capture persistent notification calls for smoke validation."""
        notification_id = str(call.data.get("notification_id", ""))
        restore_notification_id = "ha_energy_planner_restore_safe_state_01KW3HATESTENERGYPLANNER000"
        if notification_id:
            current = hass.states.get("input_text.planner_restore_notification_seen")
            if (
                getattr(current, "state", None) == restore_notification_id
                and notification_id != restore_notification_id
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
        """Return a bounded no-action explanation for smoke validation."""
        return {
            "response": json.dumps(
                {
                    "outcome": "no_action_needed",
                    "summary": "The smoke plan has no material user action.",
                }
            )
        }

    hass.services.async_register(
        DOMAIN,
        "ai_advice",
        ai_advice,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        "ai_task",
        "generate_data",
        ai_advice,
        supports_response=SupportsResponse.ONLY,
    )
    hass.states.async_set("ai_task.smoke_advisor", "ready")
    hass.services.async_register(DOMAIN, "force_ev_calibration_due", force_ev_calibration_due)
    hass.services.async_register(DOMAIN, "seed_due_forecast_snapshot", seed_due_forecast_snapshot)
    hass.services.async_register(DOMAIN, "seed_thermal_model_sample", seed_thermal_model_sample)
    hass.services.async_register(DOMAIN, "seed_enphase_command_rate_limit", seed_enphase_command_rate_limit)
    hass.services.async_register(DOMAIN, "seed_production_evidence", seed_production_evidence)
    hass.services.async_register(DOMAIN, "assert_unsafe_arm_rejected", assert_unsafe_arm_rejected)
    hass.services.async_register("persistent_notification", "create", capture_persistent_notification)
    return True
PY

cat > "$TMP_DIR/configuration.yaml" <<'YAML'
default_config:

fake_planner_test:

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
    unit_of_measurement: kW
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
  fake_climate_zone:
    name: Fake climate zone
  ev_connected:
    name: EV connected
    initial: true
  ev_charging:
    name: EV charging feedback
    initial: false
  ev_smart_charging_start:
    name: EV charger start
    initial: false
  ev_smart_charging_stop:
    name: EV charger stop
    initial: true

timer:
  climate_scheduler_guard:
    name: Climate scheduler guard
    duration: "00:00:30"

input_datetime:
  ev_ready_by:
    name: EV ready by
    has_date: false
    has_time: true
    initial: "06:00"

input_text:
  planner_current_state_seen:
    name: Planner current state seen
    initial: unknown
  planner_next_actions_seen:
    name: Planner next actions seen
    initial: unknown
  planner_armed_seen:
    name: Planner armed seen
    initial: unknown
  planner_calendar_seen:
    name: Planner calendar seen
    initial: unknown
  planner_automatic_control_seen:
    name: Planner automatic control seen
    initial: unknown
  planner_climate_control_off_seen:
    name: Planner climate control off seen
    initial: unknown
  planner_ev_control_off_seen:
    name: Planner EV control off seen
    initial: unknown
  planner_enphase_control_off_seen:
    name: Planner Enphase control off seen
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
  manual_override_release_seen:
    name: Manual override release seen
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

template:
  - sensor:
      - name: Smoke import price forecast
        state: "{{ states('input_number.import_price') }}"
        attributes:
          unitOfMeasurement: "c/kWh"
          confidence: "{{ 0.94 }}"
          detailedForecast: "{{ ([{'perKwh': ((states('input_number.import_price') | float) * 100) | round(3)}, {'perKwh': 11}, {'perKwh': 12}, {'perKwh': 13}, {'perKwh': 14}, {'perKwh': -5}, {'perKwh': 60}, {'perKwh': 61}, {'perKwh': 62}, {'perKwh': 63}, {'perKwh': 64}, {'perKwh': 65}] * 24) }}"
      - name: Smoke export price forecast
        state: "{{ states('input_number.export_price') }}"
        attributes:
          unitOfMeasurement: "c/kWh"
          confidence: "{{ 0.93 }}"
          detailedForecast: "{{ ([{'perKwh': ((states('input_number.export_price') | float) * 100) | round(3)}, {'perKwh': 9}, {'perKwh': 10}, {'perKwh': 11}, {'perKwh': 12}, {'perKwh': 13}, {'perKwh': 14}, {'perKwh': 15}, {'perKwh': 16}, {'perKwh': 17}, {'perKwh': 18}, {'perKwh': 19}] * 24) }}"
      - name: Smoke PV forecast series
        state: "{{ states('input_number.pv_forecast') }}"
        attributes:
          unitOfMeasurement: "W"
          confidence: "{{ 0.92 }}"
          detailedForecast: "{{ ([{'prediction': {'watts': 2500}}, {'prediction': {'watts': 3000}}, {'prediction': {'watts': 3500}}, {'prediction': {'watts': 4000}}, {'prediction': {'watts': 4500}}, {'prediction': {'watts': 5000}}, {'prediction': {'watts': 4500}}, {'prediction': {'watts': 4000}}, {'prediction': {'watts': 3500}}, {'prediction': {'watts': 3000}}, {'prediction': {'watts': 2500}}, {'prediction': {'watts': 2000}}] * 24) }}"
      - name: Smoke weather forecast
        state: "sunny"
        attributes:
          nativeTemperature: "{{ 21 }}"
          temperatureUnit: "C"
          confidence: "{{ 0.90 }}"
          detailedForecast: "{{ ([{'nativeTemperature': 19.0}, {'nativeTemperature': 20.0}, {'nativeTemperature': 21.0}, {'nativeTemperature': 22.0}, {'nativeTemperature': 23.0}, {'nativeTemperature': 24.0}] * 48) }}"

automation:
  - alias: Fake EV charger start feedback
    id: fake_ev_charger_start_feedback
    mode: restart
    triggers:
      - trigger: state
        entity_id: input_boolean.ev_smart_charging_start
        to: "on"
    actions:
      - action: input_boolean.turn_on
        data:
          entity_id:
            - input_boolean.ev_smart_charging_stop
            - input_boolean.ev_charging
  - alias: Fake EV charger stop feedback
    id: fake_ev_charger_stop_feedback
    mode: restart
    triggers:
      - trigger: state
        entity_id: input_boolean.ev_smart_charging_stop
        to: "off"
    actions:
      - action: input_boolean.turn_off
        data:
          entity_id:
            - input_boolean.ev_smart_charging_start
            - input_boolean.ev_charging
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
      # Explicit arming must not turn a persisted request into apparent command
      # authority before the current plan and reviewed evidence are healthy.
      - action: fake_planner_test.assert_unsafe_arm_rejected
      - condition: state
        entity_id: binary_sensor.energy_planner_armed
        state: "off"
      - action: ha_energy_planner.replan
      - wait_template: >-
          {{ state_attr('sensor.energy_planner_next_actions', 'health') == 'Healthy' }}
        timeout: "00:00:30"
        continue_on_timeout: false
      - action: fake_planner_test.seed_production_evidence
      - action: ha_energy_planner.arm_production_control
        data:
          reason: docker_smoke_reviewed_contract
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
          value: 21
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.10
      - delay: "00:00:02"
      - action: ha_energy_planner.replan
      # Restore only after the coordinated HVAC action and its ownership
      # snapshot have reached the observable actuator state. A fixed delay can
      # race a serialized startup refresh on slower Docker hosts.
      - wait_template: >-
          {{ is_state('automation.fake_climate_conflict', 'off')
             and is_state('input_boolean.fake_climate_zone', 'on') }}
        timeout: "00:00:30"
        continue_on_timeout: false
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_hvac_precondition_restore
      - delay: "00:00:02"
      - action: input_number.set_value
        data:
          entity_id: input_number.fake_indoor_temperature
          value: 21
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
          entity_id: input_number.import_price
          value: 0.10
      - delay: "00:00:02"
      - action: ha_energy_planner.replan
      - delay: "00:00:07"
      - action: input_boolean.turn_on
        data:
          entity_id: input_boolean.climate_manual_override
      - delay: "00:00:03"
      - action: input_text.set_value
        target:
          entity_id: input_text.manual_override_release_seen
        data:
          value: >-
            {{ states('automation.fake_climate_conflict') }}|{{ states('input_boolean.fake_climate_zone') }}
      - action: input_boolean.turn_off
        data:
          entity_id: input_boolean.climate_manual_override
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
      - action: fake_planner_test.force_ev_calibration_due
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
      - action: fake_planner_test.seed_due_forecast_snapshot
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
      - action: input_boolean.turn_on
        data:
          entity_id: input_boolean.climate_change_from_scheduler
      - action: climate.set_hvac_mode
        data:
          entity_id: climate.fake_daikin
          hvac_mode: heat
      - action: input_boolean.turn_off
        data:
          entity_id: input_boolean.climate_change_from_scheduler
      - action: fake_planner_test.seed_thermal_model_sample
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: input_select.select_option
        data:
          entity_id: input_select.enphase_profile
          option: Self-Consumption
      - delay: "00:00:01"
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_ev_baseline_reset
      - delay: "00:00:02"
      - action: input_boolean.turn_off
        data:
          entity_id:
            - input_boolean.ev_smart_charging_start
            - input_boolean.ev_charging
      - action: input_boolean.turn_on
        data:
          entity_id: input_boolean.ev_smart_charging_stop
      - action: ha_energy_planner.resume_control
        data:
          reason: docker_smoke_ev_restore_setup
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
          value: 0.25
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.60
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
          value: 0.25
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.60
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: ha_energy_planner.restore_safe_state
        data:
          reason: docker_smoke_second_arbitrage_restore
      - delay: "00:00:03"
      - action: input_number.set_value
        data:
          entity_id: input_number.import_price
          value: 0.25
      - action: input_number.set_value
        data:
          entity_id: input_number.export_price
          value: 0.60
      - action: fake_planner_test.seed_enphase_command_rate_limit
      - action: ha_energy_planner.replan
      - delay: "00:00:10"
      - action: button.press
        data:
          entity_id: button.energy_planner_restore_safe_state
      - delay: "00:00:02"
      - action: switch.turn_off
        data:
          entity_id: switch.energy_planner_automatic_control
      - delay: "00:00:02"
      - action: switch.turn_off
        target:
          entity_id:
            - switch.energy_planner_climate_control
            - switch.energy_planner_ev_control
            - switch.energy_planner_enphase_control
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_climate_control_off_seen
          value: "{{ states('switch.energy_planner_climate_control') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_ev_control_off_seen
          value: "{{ states('switch.energy_planner_ev_control') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_enphase_control_off_seen
          value: "{{ states('switch.energy_planner_enphase_control') }}"
      - action: switch.turn_on
        target:
          entity_id:
            - switch.energy_planner_climate_control
            - switch.energy_planner_ev_control
            - switch.energy_planner_enphase_control
      - action: ha_energy_planner.resume_control
        data:
          reason: docker_smoke_automatic_control
      - action: ha_energy_planner.replan
      - delay: "00:00:05"
      - action: fake_planner_test.seed_production_evidence
      - action: switch.turn_on
        data:
          entity_id: switch.energy_planner_automatic_control
      - delay: "00:00:02"
      - action: button.press
        data:
          entity_id: button.energy_planner_explain
      - delay: "00:00:05"
      - action: ha_energy_planner.replan
      - delay: "00:00:05"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_current_state_seen
          value: "{{ states('sensor.energy_planner_current_state') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_next_actions_seen
          value: "{{ states('sensor.energy_planner_next_actions') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_armed_seen
          value: "{{ states('binary_sensor.energy_planner_armed') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_calendar_seen
          value: "{{ states('calendar.energy_planner_plan') }}"
      - action: input_text.set_value
        data:
          entity_id: input_text.planner_automatic_control_seen
          value: "{{ states('switch.energy_planner_automatic_control') }}"
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
          "price_freshness_minutes": 30,
          "forecast_freshness_minutes": 120,
          "material_change_threshold_percent": 5.0,
          "enphase_minimum_savings": 0.25,
          "command_rate_limit_seconds": 1,
          "max_daily_ev_actions": 50,
          "max_daily_climate_actions": 50,
          "max_daily_enphase_actions": 50,
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
              "amber_import_price_entity": "sensor.smoke_import_price_forecast",
              "amber_export_price_entity": "sensor.smoke_export_price_forecast",
              "pv_forecast_entity": "sensor.smoke_pv_forecast_series",
              "household_load_entity": "input_number.baseline_load",
              "pv_observed_entity": "input_number.pv_forecast",
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
              "climate_zone_entities": ["input_boolean.fake_climate_zone"],
              "climate_change_from_scheduler_entity": "input_boolean.climate_change_from_scheduler",
              "climate_scheduler_guard_timer_entity": "timer.climate_scheduler_guard",
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
              "ai_task_entity": "ai_task.smoke_advisor"
            },
            "subentry_id": "haep_ai",
            "subentry_type": "ai",
            "title": "AI",
            "unique_id": null
          },
          {
            "data": {
              "ev_soc_entity": "input_number.ev_soc",
              "ev_charging_entity": "input_boolean.ev_charging",
              "ev_connected_entity": "input_boolean.ev_connected",
              "ev_smart_charging_target_soc_entity": "input_number.ev_target_soc",
              "ev_charger_start_entity": "input_boolean.ev_smart_charging_start",
              "ev_charger_stop_entity": "input_boolean.ev_smart_charging_stop"
            },
            "subentry_id": "haep_ev",
            "subentry_type": "ev",
            "title": "EV",
            "unique_id": null
          }
        ],
        "title": "HA Energy Planner",
        "unique_id": "ha_energy_planner",
        "version": 2
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
      "armed": false,
      "armed_at": "2026-06-27T00:00:00+00:00",
      "armed_reason": "docker_smoke",
      "acknowledged_at": "2026-06-27T00:00:00+00:00",
      "dry_run_ready_cycles": 0
    }
  }
}
JSON

python3 - "$TMP_DIR" <<'PY'
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

path = Path(sys.argv[1]) / ".storage" / "ha_energy_planner_state"
payload = json.loads(path.read_text())
trained_at = datetime.now(UTC).isoformat()
expected = [1.2] * 96
upper = [1.5] * 96
payload["data"]["built_in_load_forecast"] = {
    "model_version": 3,
    "contract_version": 4,
    "status": "ready",
    "quality_ready": True,
    "quality_failures": [],
    "source_entity_id": "input_number.baseline_load",
    "trained_at": trained_at,
    "last_attempt_at": trained_at,
    "last_attempt_source_entity_id": "input_number.baseline_load",
    "last_attempt_timezone": "UTC",
    "timezone": "UTC",
    "safety_gates_bypassed": False,
    "history_started_on": "2026-06-01",
    "history_ended_on": "2026-06-27",
    "history_days": 26,
    "training_days": 26,
    "complete_days": 26,
    "fully_observed_days": 26,
    "minimum_training_day_coverage": 0.8,
    "history_coverage": 1.0,
    "validation": {
        "origin_count": 2,
        "sample_count": 192,
        "mae_kw": 0.1,
        "rmse_kw": 0.12,
        "persistence_mae_kw": 0.2,
        "positive_residual_p90_kw": 0.2,
        "calibration_buffer_kw": 0.3,
        "upper_coverage": 0.95,
    },
    "cleaning": {"ev_intervals_excluded": True, "hvac_power_subtracted": True},
    "profiles": {
        "weekday": {"expected": expected, "upper": upper},
        "weekend": {"expected": expected, "upper": upper},
    },
}
path.write_text(json.dumps(payload, indent=2) + "\n")
PY

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
    "sensor.energy_planner_mode",
    "sensor.energy_planner_current_state",
    "sensor.energy_planner_next_actions",
    "binary_sensor.energy_planner_armed",
    "calendar.energy_planner_plan",
    "switch.energy_planner_automatic_control",
    "switch.energy_planner_climate_control",
    "switch.energy_planner_ev_control",
    "switch.energy_planner_enphase_control",
    "button.energy_planner_explain",
    "button.energy_planner_restore_safe_state",
}
missing = expected_entities - entities
if missing:
    raise SystemExit(f"Missing HA Energy Planner entities: {sorted(missing)}")
retired_entities = {
    "button.energy_planner_start_charging",
    "button.energy_planner_stop_charging",
    "number.energy_planner_target_soc",
    "switch.energy_planner_ev_connected_helper",
    "switch.energy_planner_opportunistic_charging",
    "number.energy_planner_opportunistic_charging_price_threshold",
}
unexpected_retired = retired_entities & entities
if unexpected_retired:
    raise SystemExit(f"Retired HA Energy Planner entities still exist: {sorted(unexpected_retired)}")

device_registry = load_storage("core.device_registry")
device_identifiers = {
    tuple(identifier)
    for entry in device_registry["data"]["devices"]
    for identifier in entry.get("identifiers", [])
    if identifier and identifier[0] == "ha_energy_planner"
}
expected_device_identifiers = {
    ("ha_energy_planner", "01KW3HATESTENERGYPLANNER000"),
}
if device_identifiers != expected_device_identifiers:
    raise SystemExit(
        "Energy Planner did not expose exactly one device: "
        f"expected={sorted(expected_device_identifiers)} actual={sorted(device_identifiers)}"
    )

config_entries = load_storage("core.config_entries")["data"]["entries"]
planner_entry = next(entry for entry in config_entries if entry.get("domain") == "ha_energy_planner")
if planner_entry.get("subentries"):
    raise SystemExit(f"Legacy Add device subentries were not removed: {planner_entry['subentries']}")
if planner_entry.get("data", {}).get("ai_task_entity") != "ai_task.smoke_advisor":
    raise SystemExit("Legacy subentry mappings were not migrated into central settings")

planner_store = load_storage("ha_energy_planner_state_01KW3HATESTENERGYPLANNER000")
store_data = planner_store["data"]
active_plan = store_data.get("active_plan")
if not active_plan or active_plan.get("horizon_hours") != 24 or active_plan.get("interval_minutes") != 5:
    raise SystemExit("Planner Store did not persist a valid active 24h/5m plan")
if active_plan.get("mode") not in {"DISABLED", "DRY_RUN", "ACTIVE_HEALTHY", "ACTIVE_DEGRADED"}:
    raise SystemExit(f"Unexpected persisted planner mode: {active_plan.get('mode')}")
if active_plan.get("mode") not in {"ACTIVE_HEALTHY", "ACTIVE_DEGRADED"}:
    raise SystemExit(f"Automatic control did not produce an active plan: {active_plan.get('mode')}")
if "discovery" not in store_data:
    raise SystemExit("Planner Store did not persist discovery data")
ai_discovery = store_data.get("discovery", {}).get("ai", {})
if not ai_discovery.get("supported") or ai_discovery.get("details", {}).get("service") != "ai_task.generate_data":
    raise SystemExit(f"Planner discovery did not record the local AI task service as supported: {ai_discovery}")
ai_recommendations = store_data.get("ai_recommendations", [])
if not any(
    recommendation.get("status") == "accepted"
    and recommendation.get("service_called") == "ai_task.generate_data"
    and recommendation.get("ai_task_entity") == "ai_task.smoke_advisor"
    and recommendation.get("accepted", {}).get("outcome") == "no_action_needed"
    and recommendation.get("accepted", {}).get("summary") == "The smoke plan has no material user action."
    for recommendation in ai_recommendations
):
    raise SystemExit(f"Planner Store did not persist accepted AI explanation: {ai_recommendations}")
snapshots = store_data.get("forecast_snapshots", [])
if not snapshots:
    raise SystemExit("Planner Store did not persist forecast snapshots")
latest_snapshot = snapshots[-1]
if not any(
    (snapshot.get("ai") or {}).get("status") == "accepted"
    and (snapshot.get("ai") or {}).get("accepted_fields") == ["outcome", "summary"]
    and (snapshot.get("ai") or {}).get("service_called") == "ai_task.generate_data"
    and (snapshot.get("ai") or {}).get("ai_task_entity") == "ai_task.smoke_advisor"
    for snapshot in snapshots
):
    raise SystemExit("Forecast snapshots did not persist bounded AI explanation metadata")
snapshot_actions = [
    action
    for snapshot in snapshots
    for action in snapshot.get("actions", [])
    if isinstance(action, dict)
]
if not any(
    str(action.get("action_id", "")).endswith("-ev-native-smart-charge")
    and action.get("kind") == "ev_schedule"
    and action.get("desired_state", {}).get("ready_by")
    and action.get("desired_state", {}).get("target_soc_percent", 0) >= 50
    and action.get("desired_state", {}).get("required_charge_percent", 0) > 0
    and action.get("desired_state", {}).get("allocated_slots")
    for action in snapshot_actions
):
    raise SystemExit(
        "Forecast snapshots did not persist an active EV schedule action with ready-by metadata: "
        f"{snapshot_actions}"
    )
if not any(
    str(action.get("action_id", "")).endswith("-ev-native-smart-charge")
    and action.get("kind") == "ev_schedule"
    and any(
        isinstance(slot, dict) and float(slot.get("import_price", 0.0)) < 0
        for slot in action.get("desired_state", {}).get("allocated_slots", [])
    )
    for action in snapshot_actions
):
    raise SystemExit("Forecast snapshots did not persist an active EV schedule allocated to a negative import-price slot")
if not latest_snapshot.get("forecast_training_slots"):
    raise SystemExit("Forecast snapshot did not include compact forecast training slots")
forecast_training_slots = latest_snapshot.get("forecast_training_slots", [])
pv_training = [slot.get("pv_forecast_kw") for slot in forecast_training_slots[:4]]
if pv_training != [2.5, 3.0, 3.5, 4.0]:
    raise SystemExit(f"Forecast snapshot did not use HA template PV forecast attributes: {pv_training}")
if not any(
    [slot.get("baseline_load_forecast_kw") for slot in snapshot.get("forecast_training_slots", [])[:4]]
    == [1.2, 1.2, 1.2, 1.2]
    for snapshot in snapshots
):
    raise SystemExit("Forecast snapshots did not use the built-in Recorder load model")
if not any(
    len(snapshot.get("preview", [])) >= 4
    and [slot.get("import_price") for slot in snapshot["preview"][:4]][1:] == [0.11, 0.12, 0.13]
    and [slot.get("export_price") for slot in snapshot["preview"][:4]][1:] == [0.09, 0.10, 0.11]
    for snapshot in snapshots
):
    raise SystemExit("Forecast preview did not use HA template Amber price forecast attributes")
weather_preview = [slot.get("outdoor_temperature_forecast_c") for slot in latest_snapshot.get("preview", [])[:4]]
if weather_preview != [19.0, 20.0, 21.0, 22.0]:
    raise SystemExit(f"Forecast preview did not use HA template weather forecast attributes: {weather_preview}")
if "forecast_calibration" not in latest_snapshot:
    raise SystemExit("Forecast snapshot did not include calibration metadata")
load_forecast = latest_snapshot.get("built_in_load_forecast", {})
if load_forecast.get("status") not in {"ready", "degraded"} or load_forecast.get("source") != "built_in_recorder_history":
    raise SystemExit(f"Forecast snapshot did not include healthy built-in load evidence: {load_forecast}")
if "thermal_model" not in latest_snapshot:
    raise SystemExit("Forecast snapshot did not include thermal model metadata")
ev_calibration = latest_snapshot.get("ev_charge_calibration", {})
if ev_calibration.get("update_reason") not in {
    "ev_charge_calibration_recent",
    "ev_charge_calibration_insufficient_history",
    "ev_charge_calibration_insufficient_history_retained",
    "ev_charge_calibration_ready",
} and not str(ev_calibration.get("update_reason", "")).startswith("ev_charge_calibration_unavailable:"):
    raise SystemExit(f"Unexpected EV calibration metadata: {ev_calibration}")
stored_ev_calibration = store_data.get("ev_charge_calibration", {})
if stored_ev_calibration.get("status") not in {"ready", "insufficient_history"}:
    raise SystemExit(f"Recorder EV calibration was not persisted: {stored_ev_calibration}")
if "forecast_calibration" not in store_data:
    raise SystemExit("Planner Store did not initialize forecast calibration state")
forecast_calibration = store_data.get("forecast_calibration", {})
calibration = forecast_calibration.get("pv_forecast_kw", {})
if calibration.get("model_version") != 3 or calibration.get("sample_count", 0) < 1:
    raise SystemExit(f"Forecast calibration did not store PV samples: {forecast_calibration}")
if not any(sample.get("forecast") == 1.0 and sample.get("actual") == 2.0 for sample in calibration.get("samples", [])):
    raise SystemExit(f"Forecast calibration did not consume the aligned PV smoke sample: {calibration}")
if "baseline_load_forecast_kw" in forecast_calibration:
    raise SystemExit(f"Obsolete external load calibration was retained: {forecast_calibration}")
stored_load_forecast = store_data.get("built_in_load_forecast", {})
if stored_load_forecast.get("source_entity_id") != "input_number.baseline_load":
    raise SystemExit(f"Planner Store did not retain the built-in load model: {stored_load_forecast}")
if stored_load_forecast.get("last_training_status") != "learning":
    raise SystemExit(
        "Real Recorder load training did not complete through the expected learning state: "
        f"{stored_load_forecast}"
    )
training_failures = stored_load_forecast.get("last_training_quality_failures", [])
if any(failure in training_failures for failure in {"recorder_unavailable", "training_error"}):
    raise SystemExit(f"Real Recorder load training failed unexpectedly: {stored_load_forecast}")
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
    "hvac_control_released" in item.get("reason", "")
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "on"
    for item in outcomes
):
    raise SystemExit("HVAC safety release did not restore the mapped climate automation state")
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
    and str(item.get("action_id", "")).endswith("-hvac-preconditioning")
    and item.get("reason") == "hvac_action_applied"
    and item.get("desired_state", {}).get("phase") == "preconditioning"
    and item.get("desired_state", {}).get("hvac_mode") == "heat"
    and item.get("desired_state", {}).get("target_temperature") == 24.0
    and item.get("post_state", {}).get("daikin_climate_entity") == "heat"
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "off"
    and item.get("post_state", {}).get("input_boolean.fake_climate_zone") == "on"
    for item in outcomes
):
    raise SystemExit(
        "Heating lifecycle takeover did not control climate, automation, and zone state: "
        f"{[item for item in outcomes if item.get('asset') == 'daikin']}"
    )
precondition_restore_index = next(
    (
        index
        for index, item in enumerate(outcomes)
        if item.get("action_id") == "restore_safe_state"
        and "docker_smoke_hvac_precondition_restore" in item.get("reason", "")
    ),
    None,
)
if precondition_restore_index is None:
    raise SystemExit("Heating lifecycle restore service did not persist an outcome")
heating_takeover_indices = [
    index
    for index, item in enumerate(outcomes)
    if item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-hvac-preconditioning")
    and item.get("desired_state", {}).get("hvac_mode") == "heat"
    and item.get("desired_state", {}).get("target_temperature") == 24.0
    and item.get("post_state", {}).get("automation.fake_climate_conflict") == "off"
    and item.get("post_state", {}).get("input_boolean.fake_climate_zone") == "on"
]
precondition_restore = outcomes[precondition_restore_index]
if not (
    "hvac_control_released" in precondition_restore.get("reason", "")
    and precondition_restore.get("post_state", {}).get("automation.fake_climate_conflict") == "on"
    and precondition_restore.get("post_state", {}).get("input_boolean.fake_climate_zone") == "off"
):
    raise SystemExit(
        "Heating lifecycle restore did not restore the mapped automation and zone: "
        f"{precondition_restore}"
    )
if not any(index > precondition_restore_index for index in heating_takeover_indices):
    raise SystemExit("Second heating lifecycle takeover did not reacquire automation and zone ownership")
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
    and str(item.get("action_id", "")).endswith("-ev-native-smart-charge")
    and item.get("reason")
    in {"ev_charging_confirmed", "ev_charging_stopped_confirmed"}
    for item in outcomes
):
    raise SystemExit("Active-mode native EV charger action was not applied in the smoke run")
if not any(
    item.get("result") == "applied"
    and str(item.get("action_id", "")).endswith("-ev-native-smart-charge")
    and item.get("desired_state", {}).get("ready_by") == "23:45"
    for item in outcomes
):
    if not any(
        item.get("result") == "rejected"
        and str(item.get("action_id", "")).endswith("-ev-native-smart-charge")
        and item.get("reason") == "external_ev_charging_conflict"
        for item in outcomes
    ):
        raise SystemExit(
            "set_ev_ready_by service did not produce a follow-up EV schedule or safe conflict rejection"
        )
enphase_arbitrage_outcomes = [
    item
    for item in outcomes
    if str(item.get("action_id", "")).endswith("-enphase-arbitrage-profile")
]
if enphase_arbitrage_outcomes and not any(
    item.get("result") == "applied"
    and item.get("reason") == "enphase_profile_applied"
    and item.get("post_state", {}).get("enphase_profile_entity") == "Full Backup"
    for item in enphase_arbitrage_outcomes
) and not any(
    item.get("result") == "rejected"
    and item.get("reason") in {"external_enphase_profile_conflict", "device_command_rate_limited"}
    for item in enphase_arbitrage_outcomes
):
    raise SystemExit(
        "Active-mode Enphase arbitrage was neither applied nor safely blocked by conflict/cooldown"
    )
final_restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state" and "docker_smoke_final_restore" in item.get("reason", "")
]
if not final_restore_outcomes:
    raise SystemExit("Final restore_safe_state service did not persist the smoke outcome")
second_arbitrage_restore_outcomes = [
    item
    for item in outcomes
    if item.get("action_id") == "restore_safe_state"
    and "docker_smoke_second_arbitrage_restore" in item.get("reason", "")
]
if not second_arbitrage_restore_outcomes:
    raise SystemExit("Second Enphase restore_safe_state service did not persist the smoke outcome")
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
    "input_text.planner_armed_seen": "on",
    "input_text.planner_automatic_control_seen": "on",
    "input_text.planner_climate_control_off_seen": "off",
    "input_text.planner_ev_control_off_seen": "off",
    "input_text.planner_enphase_control_off_seen": "off",
    "input_text.diagnostics_data_token_seen": "**REDACTED**",
    "input_text.diagnostics_data_address_seen": "**REDACTED**",
    "input_text.diagnostics_option_token_seen": "**REDACTED**",
    "input_text.manual_override_release_seen": "on|off",
}
for entity_id, expected_state in expected_helper_states.items():
    actual_state = restored_entity_state(entity_id)
    if actual_state != expected_state:
        raise SystemExit(
            f"Unexpected captured HA Energy Planner entity state for {entity_id}: "
            f"{actual_state!r} != {expected_state!r}"
        )
restore_notification_state = restored_entity_state("input_text.planner_restore_notification_seen")
if restore_notification_state == "ha_energy_planner_restore_safe_state_01KW3HATESTENERGYPLANNER000":
    raise SystemExit("A successful safe-state restore unexpectedly created a persistent notification")
for entity_id in (
    "input_text.planner_current_state_seen",
    "input_text.planner_next_actions_seen",
    "input_text.planner_calendar_seen",
):
    captured_state = restored_entity_state(entity_id)
    if captured_state in {None, "unknown", "unavailable"}:
        raise SystemExit(f"Consolidated status entity was unavailable: {entity_id}={captured_state!r}")
next_actions_state = restored_entity_state("input_text.planner_next_actions_seen") or ""
if not all(asset in next_actions_state for asset in ("Climate:", "EV:", "Enphase:")):
    raise SystemExit(f"Next actions did not summarize every controlled area: {next_actions_state!r}")
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
