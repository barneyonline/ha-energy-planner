<p align="center">
  <img src=".github/assets/icon.svg" alt="Energy Planner icon" width="96" height="96">
</p>

# Energy Planner - Home Assistant Custom Integration

> [!IMPORTANT]
> Energy Planner is in active development and is not ready for production use. Do not rely on it for real device control, billing decisions, or unattended energy automation yet.

<!-- Badges -->
[![Release](https://img.shields.io/github/v/release/barneyonline/ha-energy-planner?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-energy-planner/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-energy-planner)](LICENSE)

[![CI](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/ci.yml?branch=main&label=ci)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/barneyonline/ha-energy-planner/graph/badge.svg)](https://codecov.io/gh/barneyonline/ha-energy-planner)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/hassfest.yml)
[![Codespell](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/codespell.yml?branch=main&label=codespell)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/codespell.yml)

[![Quality target](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-energy-planner%2Fmain%2Fcustom_components%2Fha_energy_planner%2Fmanifest.json&query=%24.quality_scale&label=quality%20target&cacheSeconds=3600)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
[![Install](https://img.shields.io/badge/install-manual-blue)](#installation)

[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/issues)
![Development Status](https://img.shields.io/badge/development-active-success?style=flat-square)

Local-first Home Assistant integration for planning and safely coordinating household energy decisions across tariffs, solar forecasts, battery state, EV charging, climate comfort, Enphase profiles, HAEO optimization, and optional local AI advice.

## Supported device categories

- Energy system overview, planning health, forecast confidence, cost estimate, and execution audit entities
- Energy inputs for import/export tariffs, PV forecasts, baseline load forecasts, optional grid carbon-intensity forecasts, optional measured PV/load power for forecast validation, weather, and battery state of charge
- Native EV smart charging with native or external target-SOC input, ready-by controls, direct charger start/stop with charging-state confirmation, a connected helper, minimum-SOC recovery, preconditioning keep-on policy, price limits, low-price charging, continuous or interval-based schedules, and charge energy estimates
- Climate plan, current/next climate state, comfort targets, HVAC power modeling, manual override handling, and presence-aware control
- Presence as a separate device, including multi-person occupancy inputs
- Enphase profile monitoring and planned current/next battery profile state
- HAEO optimization service integration with deterministic fallback behavior
- Optional local AI advisory device for structured advice, accepted/rejected status, and rejection reasons
- System safety controls, including dry-run, active control, production arming, pause/resume, preflight, and safe-state restore

## Key features

- Guided setup with no required inputs on initial install; add Energy, Climate, Presence, Enphase, AI, and EV inputs separately from the integration page
- Forecast calibration is opt-in through separate observed PV and household-load power sensors; validated lead-time models learn expected values plus conservative PV/load uncertainty bounds, and forecast entities are never treated as ground truth
- Device structure aligned with Home Assistant hub-style integrations: each planning area appears as its own device with relevant sub-entities
- Deterministic planner that evaluates price, solar, load, battery reserve, EV readiness, comfort, carbon, and configured priority order
- Marginal-value scoring across devices so forecast surplus, battery capacity, EV readiness, and climate comfort are compared against the same constrained energy budget
- Battery-aware decisions using configured usable capacity, reserve floor, round-trip efficiency, maximum charge power, and maximum discharge power
- HAEO service support with capability detection, short-lived equivalent-input caching, solve/refresh latency telemetry, per-slot baseline/flexible evidence provenance, and bounded fallback planning when HAEO is unavailable, unhealthy, or unable to return planning evidence
- Enphase profile scenario mapping for restore, battery self-consumption, and battery charging behavior
- EV planning with connected state or a native connected helper, SOC, confirmed start/stop commands, daily trip-history replay, timezone/DST-safe ready-by deadlines, estimated charging kWh, and cost/solar/carbon-aware scheduling
- Multiple EVs through separate named planner entries with isolated entities, history, production controls, execution audit, and persisted state
- Climate planning with current state, next planned state, comfort windows, HVAC power estimation, thermal model replay, comfort coasting, and manual override blocking
- Configurable plan visibility for Climate, Enphase, and EV devices through plan sensors and timeline attributes; the recommended default horizon is 12 hours
- Forecast confidence breakdown across required inputs so stale, missing, invalid, or low-confidence subsystem data is visible
- Coverage-aware forecast health and diagnostics: at least 12 continuous hours is healthy, 8 to under 12 is degraded, and under 8 is unsafe; uncovered slots remain missing instead of repeating the final value
- Decision audit, rejected action, upcoming timeline, and per-device decision sensors explaining what was selected and why alternatives were skipped
- Optional AI advice through supported Home Assistant AI Task entities, scheduled after plan commit, skipped for unsafe/zero-confidence plans, single-flight cached by a bounded material plan signature, rate-limited, and treated as advisory only
- AI advice rejection reasons, compact summaries, and no permission for AI output to call services or bypass hard constraints
- Execution audit and support bundle services for production review without reading Home Assistant storage files directly; repeated identical skipped dry-run outcomes are coalesced with occurrence counts, while applied/failed safety events remain distinct
- Explicit decision-input replan listeners, observation-only sampling for high-frequency power sensors, a one-minute non-manual refresh floor, epoch-aligned scheduling for every supported interval, unchanged-input short-circuiting, and refresh trigger/phase/counter telemetry
- Home Assistant diagnostics, system health, modular repair/preflight evidence for partial installations, native currency/device-class semantics, entity translations, and icons for all exposed entities
- Dockerized validation gate covering compile and Ruff checks, pytest with 100% coverage, fixture replay, live-schema validation, Home Assistant `check_config`, and an optional Home Assistant smoke test

## Installation

Energy Planner is currently installed as a custom integration. It is not in the default HACS catalog, but you can add this repository to HACS manually.

### HACS custom repository

1. Open Home Assistant.
2. Go to **HACS -> Integrations**.
3. Open the three-dot menu and select **Custom repositories**.
4. Add `https://github.com/barneyonline/ha-energy-planner` as an **Integration** repository.
5. Search for **Energy Planner** in HACS.
6. Select **Download**.
7. Restart Home Assistant.
8. Go to **Settings -> Devices & services -> Add integration**.
9. Search for **Energy Planner**.
10. Add the integration. Initial setup does not require any mapped entities.
11. Open the integration page and add the planning areas you want to use:
   - **Energy** for tariffs, solar/load forecasts, battery SOC, weather, and HAEO.
   - **Presence** for person entities used by occupancy-aware planning.
   - **Climate** for Daikin climate and HVAC power inputs.
   - **Enphase** for profile monitoring and profile scenario mapping.
   - **AI** for optional local advisory service selection.
   - **EV** for vehicle SOC, connected state, and charge start/stop controls.

### Manual file copy

1. Copy `custom_components/ha_energy_planner` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & services -> Add integration**.
4. Search for **Energy Planner**.
5. Add the integration. Initial setup does not require any mapped entities.
6. Open the integration page and add the planning areas you want to use.

## Compatibility

- Integration domain: `ha_energy_planner`
- Integration display name: `Energy Planner`
- Current manifest version: `0.8.1`
- Minimum Home Assistant version: `2026.6.0`
- Integration type: `service`
- IoT class: `calculated`
- Project quality-scale target: `gold` (custom integrations remain in Home Assistant's Custom tier)
- Python: `3.14+`
- Home Assistant setup: custom integration installed under `custom_components`

Energy Planner does not authenticate directly with vendor cloud APIs. It reads existing Home Assistant entities and calls configured Home Assistant services. Vendor-specific behavior depends on the entities and services exposed by the integrations already installed in your Home Assistant instance.

## Recommended companion integrations

Energy Planner can be useful with different source integrations, but the current implementation is built around these common inputs:

- Amber Electric price sensors for import/export tariffs from the Home Assistant core [Amber Electric integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/amberelectric) or [hass-energy/amber-express](https://github.com/hass-energy/amber-express).
- Solar production forecast sensors from [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar), [hass-energy/hafo](https://github.com/hass-energy/hafo), or another forecast integration that exposes Home Assistant sensor data.
- Baseline load forecast sensors from [hass-energy/hafo](https://github.com/hass-energy/hafo) or another forecast source that exposes household load forecast data.
- Weather inputs from [bremor/bureau_of_meteorology](https://github.com/bremor/bureau_of_meteorology) or another Home Assistant weather provider.
- HAEO optimization through [hass-energy/haeo](https://github.com/hass-energy/haeo), using the response-capable `haeo.optimize` service.
- Enphase profile monitoring and control through [barneyonline/ha-enphase-energy](https://github.com/barneyonline/ha-enphase-energy) or another integration that exposes the system profile as a selectable entity.
- A charger or vehicle integration that exposes a writable charging switch, or separate start/stop buttons. Energy Planner performs the smart-charging optimization itself.
- BMW/vehicle connected-state and SOC entities from [kvanbiesen/bmw-cardata-ha](https://github.com/kvanbiesen/bmw-cardata-ha) or equivalent vehicle integrations.
- Daikin climate and HVAC power entities from Home Assistant climate/sensor integrations.
- Optional AI advice from an AI Task provider such as [jekalmin/extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation), when it exposes an `ai_task` entity.

For Amber-backed planning, start with the 12-hour default. Energy Planner reports each source's first and last timestamps, covered and continuous hours, and leading, internal, and trailing gaps. A degraded 8-to-under-12-hour window remains visible but does not produce eligible device actions under the existing healthy-input action gate.

Required Amber, PV, and baseline-load inputs must expose forecast series; a numeric current value is retained only for the current slot and cannot satisfy forecast coverage.

## Native EV smart charging

Energy Planner no longer requires the separate EV Smart Charging integration. Add the **EV** planning area and map the vehicle SOC and charging-state entities, plus either one charger switch or separate start/stop controls. Map a vehicle connected-state entity when one is available; otherwise use the EV device's native **Connected** helper switch. Energy Planner exposes its own **Target SOC**, **Ready by**, **Start charging**, and **Stop charging** entities.

The planner chooses cost-, solar-, carbon-, battery-, and grid-aware charging windows inside the configured earliest-start and ready-by window. Continuous charging is the default to reduce charger cycling; when filtering leaves no full contiguous window, the planner exposes a contiguous partial schedule as infeasible instead of silently splitting charging across gaps. Continuous mode can be disabled for interval-level optimization. A configured maximum import price can exclude expensive intervals, low-price charging can claim the current interval, and an EV below the minimum SOC is charged immediately. An optional external target-SOC sensor overrides the native Target SOC entity, which is useful when the vehicle integration owns the driver's current charge target. If that entity is unavailable or invalid, the native target is used and the source problem remains visible as a non-blocking advisory. **Keep charger on for preconditioning** keeps the charge control enabled after the target is reached while the vehicle remains connected; manual stop and the normal safety gates still take precedence. When a configured external target is unavailable and the native fallback is active, keep-on remains off because the vehicle-enforced limit cannot be verified.

When a charging-state entity is mapped, start and stop commands are considered successful only after that feedback reaches the requested state. The confirmation timeout and retry count are configurable. Unavailable feedback or an exhausted timeout fails closed, immediately restores the prior writable control state (or issues a safe-stop for momentary controls), records the failure, and engages the normal temporary control pause for starts. A disconnected or unplugged feedback state does not by itself prove that a momentary stop disabled automatic charging. A separate stop switch or input boolean is also only a command endpoint: its own state is not proof that charging is disabled. Safety ownership and capacity remain held unless meaningful charging feedback, the persistent EV charger control, or rollback confirms the safe state. A compensating stop that proves this safe state completes the recovery and releases ownership rather than retaining a failed action. Safe-state ownership records both the control that was actually commanded and the actuator mapping used at that time, so restore, automated safety-stop, and manual-stop paths continue to command the original charger after a later EV reconfiguration rather than clearing ownership from the replacement mapping. If compensation cannot be completed, the original state remains in planner ownership for restore-safe-state handling. Without a mapped charging-state entity, the adapter retains command-accepted behavior for ordinary charger commands, while safety reservations still require a confirmed persistent control or rollback before release.

Preconditioning keep-on requires the mapped persistent **EV charger control** to be a switch or input boolean. The options flow, EV reconfiguration, preflight, and the integration's Keep charger on switch reject configurations that expose only separate start/stop commands. Optional separate start buttons and start switches are never treated as proof that the charger remains enabled. The persistent control is confirmed even when charging feedback reports `fully_charged` or `connected_not_charging`. Keep-on remains disabled when the authoritative external target is unavailable or outside the configured minimum/maximum SOC policy, so vehicle-owned targeting cannot bypass the planner's hard maximum. The current slot conservatively reserves the configured full EV charge rate, so preconditioning cannot bypass grid-import or battery planning constraints.

These EV policy additions change the production-control evidence contract. After upgrading, run fresh dry-run cycles, review preflight, and arm production control again before expecting active commands.

Manual start/stop buttons create a one-hour override so the immediate replan does not undo the requested state. Manual commands, scheduled execution, and explicit restoration share one coordinator lock, so delayed confirmation cannot let an older scheduled command overwrite a completed manual stop. If inputs change while one coordinated command is waiting for confirmation, the remaining commands from that obsolete plan are abandoned and replanned. Manual starts use the latest committed household projection and the same cross-entry grid-capacity reservation as scheduled starts; they fail closed unless that projection was committed within the configured planning interval and remains safe. Manual command outcomes are persisted, and device/confirmation failures engage an EV pause and command cooldown that subsequent starts honour. Manual and scheduled stops bypass failure pauses, command cooldowns, daily action caps, unrelated plan constraints, and unavailable start controls so they can retry making the charger safe; every planner-owned stop releases capacity and clears ownership only after stop or rollback is confirmed. A manual stop whose initial confirmation fails but whose compensating stop proves the charger safe is treated as successful and receives the normal one-hour manual-stop override; an unconfirmed owned stop fails and retains ownership for recovery. Restoring an already-active external charger baseline clears planner stop ownership but retains its load in shared household capacity accounting. If unhealthy inputs, an observed disconnect, disabled EV control, or a hard grid-import violation leave planner-owned EV power active, execution prioritizes one audited safety-stop attempt for that plan and releases ownership only after success. Other active scheduled commands remain behind the normal dry-run, production-arming, health, confidence, grid-limit, rate-limit, and restore-safe-state gates.

Existing entries that used the old `ev_smart_charging_*` control keys continue to work during migration. Reconfigure the EV planning area to store the new direct-charger fields. Legacy ready-by helpers remain readable for compatibility, and the external target-SOC input is available to both upgraded and new entries.

### Multiple EVs

Create one named Energy Planner integration entry per EV. Each entry has its own EV mapping, native helpers, plan, trip history, production arming, control pause, audit, and storage namespace. Shared read-only household tariff, solar, load, and battery inputs must currently be mapped into each entry. Actuators are single-owner: the same EV persistent/start/stop control, Daikin climate entity, climate automation, or Enphase profile control cannot be assigned to more than one planner entry. EV control validation compares native and legacy fields as one set, so the same entity cannot be hidden under different start/stop mappings. When more than one entry is loaded, integration service calls must include `config_entry_id`; the Home Assistant service UI provides an entry selector.

Each entry plans independently, but active execution uses one shared in-memory household reservation gate. Scheduled and manual start or keep-on actions require a usable household projection and atomically reserve their projected EV load against the strictest configured grid-import limit among active reservations. A reservation remains held until a stop or safe-state restoration is confirmed. Observed disconnection requests a stop and does not release headroom until the charger control is proven safe, preventing automatic charging on reconnection from racing ahead of the next plan. Before issuing a start, the reservation and original actuator topology are persisted; after an interrupted command, active control must confirm a recovery stop on that original topology before a new start can run, and manual starts are rejected until that recovery completes. The reservation high-watermark is rehydrated before entries execute after restart, so an unclean restart cannot forget a previously higher active charge rate; an explicit confirmed release is persisted as inactive. Runtime option changes can increase an active reserved load immediately, but neither the option update nor a later start/no-op action can reduce it while the charger may still draw the earlier load. Import-limit policy updates apply immediately to single- and multi-EV reservations. When combined reservations no longer fit the strictest household limit, one shared atomic shedding claim selects a single loaded planner entry for the confirmed safety-stop path. Other entries keep their reservations while that claim is active, preventing concurrent evaluations from stopping every EV. A failed claimant retains its uncertain reservation but releases the claim so another loaded, controllable EV can attempt shedding; unloaded claimants are handled the same way. The claim clears when the selected reservation is released so the remaining capacity can be evaluated again. This also prevents concurrent entries from consuming the same projected headroom. Energy Planner still does not perform joint cost optimization across vehicles, so charging priority remains first-safe-action-wins when the combined plans do not fit.

For Solcast, configure **Forecast Today** as the primary PV forecast and optionally **Forecast Tomorrow** as the second PV forecast. Secondary values must have timezone-aware timestamps and are stitched in absolute time, including across midnight and daylight-saving transitions, with the primary source taking precedence where values overlap. Until per-slot provenance is retained, secondary PV slots are deliberately excluded from forecast calibration. A baseline-load forecast may conservatively fill up to one hour of missing leading slots from its current numeric state; this is reported explicitly and reduces source confidence.

## Safety model

Energy Planner is built around conservative production controls:

- The **Planner enabled** switch starts off.
- The **Dry run** switch starts on.
- Active control requires mapped inputs for each enabled control area, healthy modular preflight status, production arming, and dry-run review. Unconfigured or disabled device areas do not block a partial installation.
- Preflight reports historical `dry_run_evidence_complete` separately from `safe_to_activate_now`. Dry-run evidence is bound to the current control-area/entity/policy fingerprint but intentionally survives switching from dry-run to active mode, changing advisory-only AI settings, and changing the per-run EV ready-by time. Current activation additionally requires a recent successfully refreshed healthy plan, non-zero confidence, at least eight usable priced hours (or the whole configured horizon when shorter), and no active control pause. Execution independently rechecks the evidence fingerprint, bounded evidence count, and strict armed state and fails closed after a contract change or corrupt/missing production state.
- The executor revalidates hard constraints immediately before every device service call.
- Device commands are blocked when inputs are stale, missing, unavailable, unsafe, or outside configured policy.
- Unsafe-input, grid-limit, and HAEO fallback notifications are created once per distinct condition and updated only when their content changes. They can be turned off with **Show plan fallback notifications** under **AI and safety**. Turning the option off dismisses existing fallback notifications; it does not change plan health, device eligibility, or fail-closed execution.
- Device control is paused temporarily when a command fails or a recent planner-owned EV/Enphase state appears to have been changed externally.
- AI advice is optional, rate-limited, minimized, and advisory only. Provider integrations receive a bounded prompt and may log that prompt independently; review the provider's logging configuration and set its logger to warning or stricter when privacy matters.
- Preflight and restore-safe-state support are available through both services and button entities.

Run preflight before enabling active control from the integration **Run preflight** button or service:

```yaml
service: ha_energy_planner.run_preflight
```

Only proceed when `safe_to_activate_now` is true, required entities and services are available, and production control has been intentionally armed. Historical dry-run evidence alone is not a statement that current forecasts are safe.

## Services

Energy Planner registers these Home Assistant services:

All services accept an optional `config_entry_id`. It may be omitted with one loaded planner and is required when multiple planner entries are loaded.

- `ha_energy_planner.replan`: request an immediate planner refresh.
- `ha_energy_planner.run_preflight`: check active-mode readiness without issuing device commands. The same check is also exposed as the **Run preflight** button entity.
- `ha_energy_planner.export_diagnostics`: return redacted diagnostic state.
- `ha_energy_planner.export_support_bundle`: return preflight plus redacted diagnostics for production review.
- `ha_energy_planner.restore_safe_state`: restore planner-owned EV, Enphase, and HVAC state where supported.
- `ha_energy_planner.arm_production_control`: acknowledge production readiness and allow active device commands when other checks pass.
- `ha_energy_planner.disarm_production_control`: block active device commands until production control is armed again.
- `ha_energy_planner.pause_control`: temporarily pause planner-owned active control for all devices or a device class.
- `ha_energy_planner.resume_control`: clear the active-control pause.
- `ha_energy_planner.set_ev_ready_by`: set and persist the native EV ready-by time.
- `ha_energy_planner.set_ev_target_soc`: set and persist the native EV target SOC.
- `ha_energy_planner.set_manual_hvac_override`: block planner HVAC control for a bounded manual override window.

## Production setup checklist

1. Install Energy Planner and add it from **Devices & services**.
2. Add planning areas from the integration page.
3. Map the required source entities and services for the planning areas you want to use.
4. Review the **EV, battery, and grid** policy settings, especially EV target/minimum/maximum SOC, charge rate, earliest start, continuous charging, charging confirmation timeout/retries, preconditioning keep-on behavior, price controls, usable home-battery capacity, efficiency, and max charge/discharge power.
5. Review the **Data health** confidence thresholds for tariff, solar, load, climate, EV, and Enphase decisions.
6. Leave active control disabled and dry-run enabled.
7. Press the **Run preflight** button or run `ha_energy_planner.run_preflight`.
8. Fix missing, unavailable, stale, invalid, or low-confidence inputs.
9. Run several dry-run cycles and review plan, confidence, decision audit, rejected actions, upcoming timeline, next-state, AI advice, and execution audit entities.
10. Export a support bundle with `ha_energy_planner.export_support_bundle`.
11. Arm production control only after the dry-run plan matches your expectations.
12. Keep dry-run enabled for the first production-readiness review, then disable dry-run only when you are ready for real service calls.

The dry-run comparison entity publishes compact latest/recent summaries so its
state attributes remain recorder-safe. Detailed bounded comparison and outcome
evidence remains available in the exported support bundle.

## Rollback and manual recovery

If active control behaves unexpectedly:

1. Call `ha_energy_planner.restore_safe_state` or press the **Restore safe state** button.
2. Turn off the **Planner enabled** switch.
3. Turn on the **Dry run** switch.
4. Confirm EV charging, Enphase profile, and climate automation state manually in Home Assistant. Energy Planner suppresses already-queued plan commits during teardown, refuses config-entry unload when safe-state restoration fails, disarms production control, and leaves the runtime available for retry and diagnostics without scheduling new device actions.
5. Review `ha_energy_planner.run_preflight`, `ha_energy_planner.export_support_bundle`, and the execution audit entity.
6. Leave active control disabled until the cause is understood.

## Development and validation

Run the full local validation gate:

```bash
scripts/docker-validate.sh
```

This runs:

- Python compile checks
- Ruff lint and import-order checks
- Shell syntax checks
- Quality-scale evidence validation
- Pytest inside the Home Assistant Docker image with 100% coverage
- Replay fixtures
- Live-schema fixture validation
- Real-history fixture validation
- Rolling-origin PV/load forecast accuracy gates with MAE/RMSE by lead-time bucket and persistence-baseline comparison
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
HEP_PV_ACTUAL_ENTITY=sensor.pv_power \
HEP_LOAD_FORECAST_ENTITY=sensor.baseline_load_forecast \
HEP_LOAD_ACTUAL_ENTITY=sensor.household_load_power \
HEP_WEATHER_ENTITY=weather.home \
HEP_HAEO_SERVICE=haeo.optimize \
HEP_EV_CONNECTED_ENTITY=binary_sensor.ev_connected \
HEP_EV_SOC_ENTITY=sensor.ev_soc \
HEP_THERMAL_INDOOR_ENTITY=climate.daikin \
HEP_THERMAL_INDOOR_ATTRIBUTE=current_temperature \
HEP_DAIKIN_POWER_ENTITY=sensor.daikin_power \
scripts/export-real-validation-bundle.sh
```

Good output means the real live-schema, HAEO value-evidence, and real-history profiles pass. The history profile includes rolling, time-aligned PV and load accuracy evidence; each configured horizon bucket must meet its MAE limit and beat a persistence baseline:

- `ha-energy-planner-v1-real`
- `ha-energy-planner-haeo-value-v1-real`
- `ha-energy-planner-history-v1-real`

If fixtures were exported separately, validate them without calling Home Assistant:

```bash
scripts/export-real-validation-bundle.sh --validate-only
```

## Documentation

- Release notes: [CHANGELOG.md](CHANGELOG.md)
- Requirement evidence: [docs/requirements-audit.md](docs/requirements-audit.md)
- Quality evidence: [quality_scale.yaml](quality_scale.yaml)
- Issue tracker: [GitHub Issues](https://github.com/barneyonline/ha-energy-planner/issues)
