# Energy Planner

Home Assistant custom integration for the `ha-energy-planner` specification.

The current implementation is fail-closed by default: planner execution starts
disabled, dry-run starts enabled, and device control only occurs after the user
maps the required Home Assistant entities/services and explicitly enables
active mode.

## Docker validation

Run the full local validation gate:

```bash
scripts/docker-validate.sh
```

This runs Python compile checks, the full pytest suite inside the Home
Assistant Docker image, replay fixtures, live-schema fixture validation,
Home Assistant `check_config`, and the Docker smoke test.

Run only the automated Docker smoke test:

```bash
scripts/docker-ha-smoke.sh
```

Start a disposable Home Assistant Core container with the integration mounted:

```bash
docker compose up
```

Then open <http://localhost:8124> and add **Energy Planner** from Devices &
services. The test container uses `docker/homeassistant/config` as its config
directory.

## Safety defaults

- `switch.ha_energy_planner_enabled` defaults off.
- `switch.ha_energy_planner_dry_run` defaults on.
- New config entries are created with active control disabled and dry-run
  enabled. Active device control requires an explicit options/switch change by
  the operator after preflight review.
- The executor revalidates constraints and discovered capabilities before every
  device service call.
- `ha_energy_planner.run_preflight` checks entity/service readiness, capability
  discovery, Recorder availability, first-run safety mode, and recent execution
  audit outcomes without issuing device commands.
- `ha_energy_planner.restore_safe_state` restores planner-owned EV,
  Enphase, and climate automation state where supported, clears ownership, and
  creates a persistent notification.

## Real Home Assistant hookup checklist

1. Install the integration files under
   `custom_components/ha_energy_planner/`.
2. Restart Home Assistant and add **Energy Planner** from Devices & services.
3. Map the required entities and services:
   - HAEO optimize service.
   - Amber import and export price sensors.
   - PV/HAFO forecast sensor.
   - Baseline load forecast sensor.
   - Battery SOC sensor.
   - Daikin climate entity and climate target helpers.
   - Occupancy/person entities.
   - EV Smart Charging start/stop controls and EV SOC/connected entities, if EV
     control is enabled.
   - Enphase profile entity/control service and profile names, if Enphase
     profile control is enabled.
4. Leave `planner_enabled` off and `dry_run` on.
5. Call `ha_energy_planner.run_preflight` and confirm:
   - `active_control_ready` is `true`.
   - `entities.missing` and `entities.unavailable` are empty.
   - `services.missing` and `services.unavailable` are empty.
   - `mode.safe_first_run_mode` is `true`.
6. Run a dry-run replan with `ha_energy_planner.replan`.
7. Review `ha_energy_planner.export_diagnostics`, the entity states, and the
   `audit.recent_outcomes` returned by `ha_energy_planner.run_preflight`.
8. Export real validation evidence:

```bash
HOME_ASSISTANT_URL=http://homeassistant.local:8123 \
HOME_ASSISTANT_TOKEN=... \
HEP_AMBER_IMPORT_ENTITY=sensor.amber_express_home_general_price \
HEP_AMBER_EXPORT_ENTITY=sensor.amber_express_home_feed_in_price \
HEP_PV_FORECAST_ENTITY=sensor.pv_forecast \
HEP_BASELINE_LOAD_ENTITY=sensor.baseline_load_forecast \
HEP_WEATHER_ENTITY=weather.home \
HEP_HAEO_SERVICE=haeo.optimize \
HEP_EV_CONNECTED_ENTITY=binary_sensor.ev_connected \
HEP_EV_SOC_ENTITY=sensor.ev_soc \
HEP_THERMAL_INDOOR_ENTITY=climate.daikin \
HEP_THERMAL_INDOOR_ATTRIBUTE=current_temperature \
HEP_DAIKIN_POWER_ENTITY=sensor.daikin_power \
scripts/export-real-validation-bundle.sh
```

Good output means all three profiles pass:
`ha-energy-planner-v1-real`, `ha-energy-planner-haeo-value-v1-real`, and
`ha-energy-planner-history-v1-real`.
9. Only after several dry-run cycles look correct, turn on
   `switch.ha_energy_planner_enabled`. Keep `switch.ha_energy_planner_dry_run`
   on for the first active-readiness review, then turn dry-run off only when
   you are ready for real device service calls.

## Execution audit

The integration stores a bounded execution audit in
`.storage/ha_energy_planner_state` under `execution_audit`. Each entry includes
the attempted action, plan ID, result, compact reason code, and configured HA
service/entity target. The `ha_energy_planner.run_preflight` response includes
the latest audit entries so real-world dry-run and active-control behavior can
be reviewed without opening the storage file.

## Rollback and manual recovery

If active control behaves unexpectedly:

1. Call `ha_energy_planner.restore_safe_state` or press
   `button.ha_energy_planner_restore_safe_state`.
2. Turn off `switch.ha_energy_planner_enabled`.
3. Turn on `switch.ha_energy_planner_dry_run`.
4. Confirm EV Smart Charging is back in the desired manual or automation mode.
5. Confirm the Enphase profile is restored to the configured AI profile.
6. Confirm mapped climate automations are back in their prior enabled state.
7. Review `ha_energy_planner.run_preflight` and
   `ha_energy_planner.export_diagnostics` for the last action reason.
8. Leave `switch.ha_energy_planner_enabled` off, or keep
   `switch.ha_energy_planner_dry_run` on, until the audit outcome is reviewed.

## Replay validation

Run sanitized replay fixtures through the shared hard-constraint validator:

```bash
scripts/replay-fixture.py tests/fixtures/replay/*.json
```

The fixture suite includes stale inputs, battery floor violations, HVAC
occupancy rules, manual override rejection, Enphase profile holds, negative
price EV scheduling, and physically infeasible ready-by evidence.

## Live-schema validation

Export all sanitized real evidence and run every real validation profile:

```bash
HOME_ASSISTANT_URL=http://homeassistant.local:8123 \
HOME_ASSISTANT_TOKEN=... \
HEP_AMBER_IMPORT_ENTITY=sensor.amber_express_home_general_price \
HEP_AMBER_EXPORT_ENTITY=sensor.amber_express_home_feed_in_price \
HEP_PV_FORECAST_ENTITY=sensor.pv_forecast \
HEP_BASELINE_LOAD_ENTITY=sensor.baseline_load_forecast \
HEP_WEATHER_ENTITY=weather.burwood_east_hourly \
HEP_HAEO_SERVICE=haeo.optimize \
HEP_EV_CONNECTED_ENTITY=binary_sensor.mini_connected \
HEP_EV_SOC_ENTITY=sensor.mini_soc \
HEP_THERMAL_INDOOR_ENTITY=climate.daikinap02966 \
HEP_THERMAL_INDOOR_ATTRIBUTE=current_temperature \
HEP_DAIKIN_POWER_ENTITY=sensor.daikinap02966_power \
HEP_OUTDOOR_TEMPERATURE_ENTITY=sensor.outdoor_temperature \
scripts/export-real-validation-bundle.sh
```

The bundle runs both lower-level real exporters, then validates all real
profiles: `ha-energy-planner-v1-real`,
`ha-energy-planner-haeo-value-v1-real`, and
`ha-energy-planner-history-v1-real`.
If fixtures were exported or redacted separately, validate them without calling
Home Assistant again:

```bash
scripts/export-real-validation-bundle.sh --validate-only
```

For one-off live-schema exports, use:

```bash
HOME_ASSISTANT_URL=http://homeassistant.local:8123 \
HOME_ASSISTANT_TOKEN=... \
HEP_AMBER_IMPORT_ENTITY=sensor.amber_express_home_general_price \
HEP_AMBER_EXPORT_ENTITY=sensor.amber_express_home_feed_in_price \
HEP_PV_FORECAST_ENTITY=sensor.pv_forecast \
HEP_BASELINE_LOAD_ENTITY=sensor.baseline_load_forecast \
HEP_WEATHER_ENTITY=weather.burwood_east_hourly \
HEP_HAEO_SERVICE=haeo.optimize \
scripts/export-real-live-schema.sh
```

The live-schema wrapper exports all required `real_*` fixtures and runs the
`ha-energy-planner-v1-real` validation profile plus the stricter
`ha-energy-planner-haeo-value-v1-real` profile, which requires the real HAEO
response to include Enphase-relevant grid import/export and battery
charge/discharge evidence. For one-off exports, use the lower-level exporter
directly:

```bash
HOME_ASSISTANT_URL=http://homeassistant.local:8123 \
HOME_ASSISTANT_TOKEN=... \
scripts/export-live-schema-fixture.py \
  --out tests/fixtures/live_schema/real_amber_import.json \
  --validate \
  --redact-key serial \
  forecast-state \
  --name real_amber_import \
  --entity-id sensor.amber_express_home_general_price \
  --value-kind price \
  --value-keys import_price,general_price,per_kwh,price,value

HOME_ASSISTANT_URL=http://homeassistant.local:8123 \
HOME_ASSISTANT_TOKEN=... \
scripts/export-live-schema-fixture.py \
  --out tests/fixtures/live_schema/real_haeo_response.json \
  --validate \
  --redact-key serial \
  haeo-response \
  --name real_haeo_response \
  --service haeo.optimize \
  --service-data-json '{"source":"schema_export"}'

scripts/validate-live-schema-fixture.py tests/fixtures/live_schema/*.json

scripts/validate-live-schema-fixture.py \
  --profile ha-energy-planner-v1-real \
  tests/fixtures/live_schema/real_*.json

scripts/validate-live-schema-fixture.py \
  --profile ha-energy-planner-haeo-value-v1-real \
  tests/fixtures/live_schema/real_*.json
```

The exporter redacts sensitive keys and does not store Home Assistant tokens.
With `--validate`, it checks the sanitized payload with the same parser used by
`scripts/validate-live-schema-fixture.py` before writing the fixture. The
`ha-energy-planner-v1-real` profile verifies that the required real Amber
import/export, PV/HAFO, baseline-load, weather, and HAEO response fixture names
are present with the expected fixture type, value kind, and exported source
entity/service metadata. Use repeated `--redact-key` arguments for
site-specific identifiers that should not be committed in fixtures.
Only run the `haeo-response` subcommand against response-capable planning
services that are safe to call for validation. Set `HEP_HAEO_SERVICE_DATA_JSON`
when the real HAEO service needs a specific non-commanding scenario to return
both grid-charge and discharge/export evidence.

## Real history replay validation

Export sanitized Home Assistant Recorder history for MINI trip import and Daikin
thermal-model replay:

```bash
HOME_ASSISTANT_URL=http://homeassistant.local:8123 \
HOME_ASSISTANT_TOKEN=... \
HEP_EV_CONNECTED_ENTITY=binary_sensor.mini_connected \
HEP_EV_SOC_ENTITY=sensor.mini_soc \
HEP_THERMAL_INDOOR_ENTITY=climate.daikinap02966 \
HEP_THERMAL_INDOOR_ATTRIBUTE=current_temperature \
HEP_DAIKIN_POWER_ENTITY=sensor.daikinap02966_power \
HEP_OUTDOOR_TEMPERATURE_ENTITY=sensor.outdoor_temperature \
scripts/export-real-history-fixtures.sh
```

The wrapper writes `real_mini_trip_history.json` and
`real_daikin_thermal_history.json`, validates each replay, and runs the
`ha-energy-planner-history-v1-real` coverage profile. The exporter stores only
sanitized state, timestamps, selected attributes, and source entity metadata; it
does not store Home Assistant tokens. Use `HEP_HISTORY_START`/`HEP_HISTORY_END`
or `HEP_HISTORY_DAYS` to control the Recorder window, and `HEP_REDACT_KEYS` for
site-specific identifiers. For one-off checks:

```bash
scripts/validate-real-history-fixture.py tests/fixtures/history/*.json

scripts/validate-real-history-fixture.py \
  --profile ha-energy-planner-history-v1-real \
  tests/fixtures/history/real_*.json
```

## HAEO integration

The integration calls only the configured Home Assistant service, defaulting to
`haeo.optimize`. If the service is unavailable, planning degrades and records a
compact HAEO run reason instead of importing HAEO internals or controlling
devices on stale optimizer state.

## Local AI advisor

AI advice is disabled by default. When enabled, the integration calls only the
configured local Home Assistant service, sends a compact redacted summary, and
accepts JSON fields from the whitelisted soft-policy schema. AI output is stored
as advice metadata and cannot call services or change hard constraints.
