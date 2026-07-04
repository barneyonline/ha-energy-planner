# Energy Planner - Home Assistant Custom Integration

<!-- Badges -->
[![Release](https://img.shields.io/github/v/release/barneyonline/ha-energy-planner?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-energy-planner/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-energy-planner)](LICENSE)

[![CI](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/ci.yml?branch=main&label=ci)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/barneyonline/ha-energy-planner/graph/badge.svg)](https://codecov.io/gh/barneyonline/ha-energy-planner)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/hassfest.yml)
[![Codespell](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/codespell.yml?branch=main&label=codespell)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/codespell.yml)

[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-energy-planner%2Fmain%2Fcustom_components%2Fha_energy_planner%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)
[![Install](https://img.shields.io/badge/install-manual-blue)](#installation)

[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/issues)
![Development Status](https://img.shields.io/badge/development-active-success?style=flat-square)

Local-first Home Assistant integration for planning and safely coordinating household energy decisions across tariffs, solar forecasts, battery state, EV charging, climate comfort, Enphase profiles, HAEO optimization, and optional local AI advice.

## Supported device categories

- Energy system overview, planning health, forecast confidence, cost estimate, and execution audit entities
- Energy inputs for import/export tariffs, PV forecasts, baseline load forecasts, weather, and battery state of charge
- EV charging plan, current/next charging state, ready-by planning, charge energy estimates, and start/stop controls
- Climate plan, current/next climate state, comfort targets, HVAC power modeling, manual override handling, and presence-aware control
- Presence as a separate device, including multi-person occupancy inputs
- Enphase profile monitoring and planned current/next battery profile state
- HAEO optimization service integration with deterministic fallback behavior
- Optional local AI advisory device for structured advice, accepted/rejected status, and rejection reasons
- System safety controls, including dry-run, active control, production arming, pause/resume, preflight, and safe-state restore

## Key features

- Guided setup with no required inputs on initial install; add Energy, Climate, Presence, Enphase, AI, and EV inputs separately from the integration page
- Device structure aligned with Home Assistant hub-style integrations: each planning area appears as its own device with relevant sub-entities
- Deterministic planner that evaluates price, solar, load, battery reserve, EV readiness, comfort, carbon, and configured priority order
- HAEO service support for optimization, with bounded fallback planning when HAEO is unavailable or returns an unhealthy result
- Enphase profile scenario mapping for restore, battery self-consumption, and battery charging behavior
- EV planning with connected state, SOC, start/stop entities, daily trip-history replay, and estimated charging kWh
- Climate planning with current state, next planned state, comfort windows, HVAC power estimation, thermal model replay, and manual override blocking
- 24-hour plan visibility for Climate, Enphase, and EV devices through plan sensors and timeline attributes
- Forecast confidence breakdown across required inputs so stale, missing, or invalid data is visible
- Optional AI advice through supported Home Assistant AI task or conversation entities, rate-limited and treated as advisory only
- AI advice rejection reasons, compact summaries, and no permission for AI output to call services or bypass hard constraints
- Execution audit and support bundle services for production review without reading Home Assistant storage files directly
- Home Assistant diagnostics, system health, repair/preflight evidence, entity translations, and icons for all exposed entities
- Dockerized validation gate covering compile checks, pytest with 100% coverage, fixture replay, live-schema validation, Home Assistant `check_config`, and an optional Home Assistant smoke test

## Installation

Energy Planner is currently a manual custom integration install. It is intentionally not packaged for HACS at this stage.

1. Copy `custom_components/ha_energy_planner` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & services -> Add integration**.
4. Search for **Energy Planner**.
5. Add the integration. Initial setup does not require any mapped entities.
6. Open the integration page and add the planning areas you want to use:
   - **Energy** for tariffs, solar/load forecasts, battery SOC, weather, and HAEO.
   - **Presence** for person entities used by occupancy-aware planning.
   - **Climate** for Daikin climate and HVAC power inputs.
   - **Enphase** for profile monitoring and profile scenario mapping.
   - **AI** for optional local advisory service selection.
   - **EV** for vehicle SOC, connected state, and charge start/stop controls.

## Compatibility

- Integration domain: `ha_energy_planner`
- Integration display name: `Energy Planner`
- Current manifest version: `0.1.27`
- Integration type: `hub`
- IoT class: `local_polling`
- Claimed Home Assistant quality scale: `platinum`
- Python: `3.11+`
- Home Assistant setup: custom integration installed under `custom_components`

Energy Planner does not authenticate directly with vendor cloud APIs. It reads existing Home Assistant entities and calls configured Home Assistant services. Vendor-specific behavior depends on the entities and services exposed by the integrations already installed in your Home Assistant instance.

## Recommended companion integrations

Energy Planner can be useful with different source integrations, but the current implementation is built around these common inputs:

- Amber Electric price sensors for import/export tariffs
- Solcast or HAFO-style PV forecast sensors
- Baseline load forecast sensors
- Bureau of Meteorology or another weather provider
- HAEO for response-capable optimization through `haeo.optimize`
- Enphase profile entity/control exposed through an Enphase integration
- EV Smart Charging or equivalent start/stop controls
- BMW/vehicle entities or equivalent EV connected/SOC sensors
- Daikin climate and HVAC power entities
- Extended OpenAI Conversation or another supported local AI task/conversation provider for optional advice

## Safety model

Energy Planner is built around conservative production controls:

- The **Planner enabled** switch starts off.
- The **Dry run** switch starts on.
- Active control requires mapped inputs, healthy preflight status, production arming, and dry-run review.
- The executor revalidates hard constraints immediately before every device service call.
- Device commands are blocked when inputs are stale, missing, unavailable, unsafe, or outside configured policy.
- AI advice is optional, rate-limited, redacted, and advisory only.
- Restore-safe-state support is available through both a service and button entity.

Run preflight before enabling active control:

```yaml
service: ha_energy_planner.run_preflight
```

Only proceed when the response shows the integration is ready, required entities and services are available, and production control has been intentionally armed.

## Services

Energy Planner registers these Home Assistant services:

- `ha_energy_planner.replan`: request an immediate planner refresh.
- `ha_energy_planner.run_preflight`: check active-mode readiness without issuing device commands.
- `ha_energy_planner.export_diagnostics`: return redacted diagnostic state.
- `ha_energy_planner.export_support_bundle`: return preflight plus redacted diagnostics for production review.
- `ha_energy_planner.restore_safe_state`: restore planner-owned EV, Enphase, and HVAC state where supported.
- `ha_energy_planner.arm_production_control`: acknowledge production readiness and allow active device commands when other checks pass.
- `ha_energy_planner.disarm_production_control`: block active device commands until production control is armed again.
- `ha_energy_planner.pause_control`: temporarily pause planner-owned active control for all devices or a device class.
- `ha_energy_planner.resume_control`: clear the active-control pause.
- `ha_energy_planner.set_ev_ready_by`: set a runtime EV ready-by override.
- `ha_energy_planner.set_manual_hvac_override`: block planner HVAC control for a bounded manual override window.

## Production setup checklist

1. Install Energy Planner and add it from **Devices & services**.
2. Add planning areas from the integration page.
3. Map the required source entities and services for the planning areas you want to use.
4. Leave active control disabled and dry-run enabled.
5. Run `ha_energy_planner.run_preflight`.
6. Fix missing, unavailable, stale, or invalid inputs.
7. Run several dry-run cycles and review plan, confidence, cost, timeline, next-state, AI advice, and execution audit entities.
8. Export a support bundle with `ha_energy_planner.export_support_bundle`.
9. Arm production control only after the dry-run plan matches your expectations.
10. Keep dry-run enabled for the first production-readiness review, then disable dry-run only when you are ready for real service calls.

## Rollback and manual recovery

If active control behaves unexpectedly:

1. Call `ha_energy_planner.restore_safe_state` or press the **Restore safe state** button.
2. Turn off the **Planner enabled** switch.
3. Turn on the **Dry run** switch.
4. Confirm EV charging, Enphase profile, and climate automation state manually in Home Assistant.
5. Review `ha_energy_planner.run_preflight`, `ha_energy_planner.export_support_bundle`, and the execution audit entity.
6. Leave active control disabled until the cause is understood.

## Development and validation

Run the full local validation gate:

```bash
scripts/docker-validate.sh
```

This runs:

- Python compile checks
- Shell syntax checks
- Quality-scale evidence validation
- Pytest inside the Home Assistant Docker image with 100% coverage
- Replay fixtures
- Live-schema fixture validation
- Real-history fixture validation
- Home Assistant `check_config`
- Docker smoke test against a real Home Assistant container, unless `HEP_SKIP_HA_SMOKE=1` is set

Routine GitHub CI skips the smoke test to keep normal push and pull-request feedback lighter. The heavier Home Assistant smoke test runs from the **Home Assistant Smoke** workflow on a weekly schedule or when started manually.

Run only the smoke test locally:

```bash
scripts/docker-ha-smoke.sh
```

Start a local Home Assistant Core container with the integration mounted:

```bash
docker compose up
```

Then open <http://localhost:8124> and add **Energy Planner** from **Devices & services**. The local container uses `docker/homeassistant/config` as its config directory.

## Replay and real-evidence validation

Sanitized replay fixtures can be checked with:

```bash
scripts/replay-fixture.py tests/fixtures/replay/*.json
```

Export and validate a real production evidence bundle:

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

Good output means the real live-schema, HAEO value-evidence, and real-history profiles pass:

- `ha-energy-planner-v1-real`
- `ha-energy-planner-haeo-value-v1-real`
- `ha-energy-planner-history-v1-real`

If fixtures were exported separately, validate them without calling Home Assistant:

```bash
scripts/export-real-validation-bundle.sh --validate-only
```

## Documentation

- Requirement evidence: [docs/requirements-audit.md](docs/requirements-audit.md)
- Quality evidence: [quality_scale.yaml](quality_scale.yaml)
- Issue tracker: [GitHub Issues](https://github.com/barneyonline/ha-energy-planner/issues)
