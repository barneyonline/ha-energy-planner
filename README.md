# Energy Planner - Home Assistant Custom Integration

[![Release](https://img.shields.io/github/v/release/barneyonline/ha-energy-planner?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-energy-planner/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-energy-planner)](LICENSE)

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/ci.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/barneyonline/ha-energy-planner/branch/main/graph/badge.svg)](https://codecov.io/gh/barneyonline/ha-energy-planner)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/hassfest.yml)
[![Self-assessed quality: Platinum](https://img.shields.io/badge/self--assessed%20quality-platinum-blue)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)

[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)
[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/issues)
![Development Status](https://img.shields.io/badge/development-active-success?style=flat-square)

Energy Planner is a local-first Home Assistant custom integration that coordinates tariffs, solar, household load, batteries, EV charging, climate comfort, and Enphase operating profiles in one guarded plan.

The Platinum quality-scale label is a repository self-assessment against the current Home Assistant integration quality rules. As a custom integration, Energy Planner is not reviewed, security audited, maintained, or supported by the Home Assistant project. Rule-by-rule evidence is tracked in [`quality_scale.yaml`](quality_scale.yaml).

## Supported Functionality

Energy Planner reads existing Home Assistant entities and calls Home Assistant services. It does not connect directly to vendor clouds or replace the integrations that supply device data and controls.

The Energy Planner service displays a flat list containing the planner device and each tracked vehicle. Planner entities remain on the planner device; vehicle devices represent saved profiles, while their telemetry remains in the source integration. Existing vehicle sub-entries migrate automatically on reload or when saving vehicle changes before the first reload, preserving profile IDs, settings, and calibration associations. Use **Configure → Planner settings** for inputs and policy, or **Configure → Add vehicle / Edit vehicle / Remove vehicle** to manage profiles.

Planning and control include:

- Tariff-aware EV charging with ready-by, target-SOC, solar, battery-reserve, and grid-limit constraints.
- Climate preconditioning with learned normal-operation comparisons, bounded schedule search, tariff/solar/battery economics, optional room and arrival inputs, and existing comfort, manual-override and rollback safeguards. See [Climate decision policy](docs/climate-decisions.md).
- Enphase self-consumption, backup, and AI-profile selection where mapped controls support it.
- A Recorder-trained household-load forecast with conservative validation and fail-closed handling of missing or stale data.
- A plan calendar, current state, next actions, input health, forecast confidence, production readiness, and redacted support data.
- Diagnostic sensors for Decision summary, Plan health, Current load forecast, Planning duration, and load-forecast coverage.
- Clear `review`, `recovery`, and `active` mode states so startup recovery is distinguishable from normal review mode.
- Independent switches for climate, EV, and Enphase control, plus a guarded Automatic control switch.
- Optional AI Task explanations that remain advisory and cannot call services or bypass constraints.

EV planning includes capacity-aware slots, a soft readiness buffer, optional measured charging performance and number-entity power limits, battery opportunity-cost estimates, and guarded departure-priority spending. See [EV charging decisions](docs/ev-scheduling.md) for configuration, compatibility, and diagnostic evidence.

Continuous EV charging compares the total energy cost of feasible charging
windows, including solar opportunity cost and partial final slots. Configured
carbon preferences, active-session continuity, and ready-by limits still apply.

Before acquiring device control, Energy Planner requires confirmation that
recovery metadata was written to storage. Failed writes block new commands and remain pending for retry.
Shutdown-deferred writes are flushed before command authority is granted.
Disarming still restores planner-owned HVAC zones and automations when storage
is unavailable. A failed save is reported after restoration, and the resulting
state remains pending for retry.

Provided Home Assistant actions:

- `ha_energy_planner.replan`: request an immediate planner refresh.
- `ha_energy_planner.run_preflight`: check active-mode readiness without issuing commands.
- `ha_energy_planner.restore_safe_state`: restore planner-owned EV, Enphase, and HVAC state where supported.
- `ha_energy_planner.pause_control` and `ha_energy_planner.resume_control`: pause all control or a selected device class.
- `ha_energy_planner.set_ev_ready_by`: update the EV ready-by time.
- `ha_energy_planner.set_manual_hvac_override`: block planner HVAC control for a bounded period.
- `ha_energy_planner.export_diagnostics` and `ha_energy_planner.export_support_bundle`: return redacted troubleshooting evidence.
- `ha_energy_planner.arm_production_control` and `ha_energy_planner.disarm_production_control`: explicitly manage the advanced production safety gate.

With multiple planner entries, provide `config_entry_id` when calling an action. Use one planner with vehicle profiles for cars sharing one charger; separate chargers use separate planner entries.

Energy Planner does not provide custom automation triggers or conditions; use
its entity state changes and the standard Home Assistant automation building
blocks when automating planner behavior.

## Installation

### HACS

1. Open HACS.
2. Open the three-dot menu and select **Custom repositories**.
3. Add `https://github.com/barneyonline/ha-energy-planner` as an **Integration** repository.
4. Download Energy Planner.
5. Restart Home Assistant.
6. Go to **Settings -> Devices & services -> Add integration -> Energy Planner**.

[![Open your Home Assistant instance and open the Energy Planner repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=barneyonline&repository=ha-energy-planner&category=integration)

### Manual

1. Copy `custom_components/ha_energy_planner` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & services -> Add integration -> Energy Planner**.

## Requirements

- Home Assistant `2026.6.0` or newer; the pinned release-validation baseline is `2026.9.0`.
- Import and export tariff forecast sensors, commonly supplied by Amber Electric.
- An external PV forecast, commonly Solcast Forecast Today and optionally Forecast Tomorrow.
- A measured whole-home instantaneous consumption sensor in W, kW, or MW.
- Home Assistant Recorder with retained history for the household-load sensor.
- Battery state of charge and the entities or actions required by each device area you want Energy Planner to control.

Weather, carbon intensity, measured PV power, and AI explanations are optional. An external solar forecast remains required. The integration has no third-party Python dependencies and does not require separate vendor credentials.

## Configuration

Initial setup asks for a planner name. Then open **Configure** to map inputs and manage six areas:

- Energy, battery, grid, and data.
- Climate and presence.
- Enphase.
- Safety and troubleshooting.
- EV charging.
- Planning and priorities.

For a safe initial rollout:

1. Map tariff forecasts, PV forecasts, household load, battery SOC, and only the devices you intend to manage.
2. Confirm Recorder retains the selected household-load sensor.
3. Review EV, battery, grid, climate, and confirmation limits.
4. Enable the individual climate, EV, or Enphase control switches you want to evaluate.
5. Leave **Automatic control** off while the load model learns and you review **Current state**, **Next actions**, and the **Plan** calendar.
6. Run the safety check and resolve missing, stale, or unconfirmed inputs.
7. Turn on **Automatic control** only when the plan and mapped device behavior are understood.

**Automatic control** records the operator's request for active control. **Armed** is the actual command-authority gate; unsafe or incomplete evidence keeps it off even when automatic-control intent is retained.

Saving mapped inputs may briefly make planner entities unavailable while their listeners are rebuilt. Concurrent updates from one settings save share that reload and preserve automatic recovery; after the required safety checks pass, Armed returns on. Policy-only changes apply without rebuilding the integration.

When **Automatic control** is armed and **EV control** is enabled, Energy Planner immediately stops charging that a charger starts by itself on plug-in. If the stop cannot be confirmed, it retries every 30 seconds—even while charging feedback is temporarily unavailable—until charging is confirmed inactive or control is disabled. Starts actually issued by Energy Planner or its manual EV controls are ownership-tracked and are not mistaken for plug-in auto-starts; the next plan may start charging again when the current slot calls for it.

## Known Limitations

- Tariff and PV forecasts must come from other Home Assistant integrations. Energy Planner does not fetch forecasts directly.
- The load model needs at least three qualifying days of Recorder history and must pass coverage and holdout checks before forecast-dependent commands are allowed.
- Missing, stale, invalid, or unconfirmed inputs fail closed. This can suppress otherwise economical actions.
- Enphase control is limited to the verified profiles exposed by the mapped Home Assistant integration; it does not directly command battery charge or discharge power.
- EV control requires a mapped target-SOC entity and confirmed charger feedback. Cars sharing one charger use tracked vehicle profiles.
- After the Solcast unit correction, PV calibration restarts from new forecast evidence; older models and training snapshots are discarded. Explicit W/kW/MW forecast units remain supported.
- Climate comfort holds prevent reacquisition until their expiry. An expired hold alone does not block a new preconditioning cycle.
- During preconditioning, heating can continue at or below the lower comfort boundary and cooling at or above the upper boundary. Reaching the opposite boundary hands control back. During pre-peak and peak coasting, either boundary triggers a handoff.
- Climate takeover requires enough mapped state to restore the thermostat, configured zones, and automations safely. Off zones that expose no temperature target are left out of temperature synchronisation for that takeover; the main thermostat, zone switches, and zones with valid targets remain eligible. During a planner turn-on, a deferred zone may recover its target even when Daikin publishes a fresh context: the event must have no user or parent context, match the configured main startup or an unambiguous zone actuator, and restore a finite target. During main-unit turn-on, the remembered mode may differ from the requested mode; zone-switch feedback must still match the requested mode. Deferred zones may then follow the explicit main-unit mode command during its service/confirmation window, with no user or unrelated parent attribution, no auxiliary-setting changes, and only finite recovered targets. Unrelated manual changes remain protected. A later takeover can synchronise a recovered zone.
- Optional AI explanations depend on a configured Home Assistant `ai_task`
  entity and remain advisory only. A newly created AI Task with state `unknown`
  can be used immediately; only a missing or explicitly `unavailable` provider
  blocks Explain. A configured provider may run locally or in a cloud service;
  selected planner context is sent to that provider when Explain is requested.
- Bypassing safety gates is an advanced, default-off setting that reduces protection and should be used only with an explicit understanding of the risk.

## Troubleshooting

- **Load model stays in learning:** verify Recorder history, the sensor unit, and that the source represents gross household demand rather than solar, energy totals, forecasts, or signed net grid flow.
- **Household-load sensor drops out:** inspect the Current load forecast attributes or the diagnostics `load_forecast` section. `fallback_status` shows `active`, `unavailable`, or `not_needed`; `fallback_summary` explains the reason, and `fallback_remaining_seconds` shows the remaining configured grace when the outage start is known. `model_status` on the sensor (`status` in diagnostics) reports model readiness. The default 10-minute grace requires a current, quality-approved model with complete coverage; missing or invalid readings and expired grace still fail closed.
- **Input availability warnings:** warnings identify the issue code and configured entities. During Home Assistant startup, missing-input warnings have a bounded ten-minute grace period; safety gates still apply immediately. The grace applies only to inputs missing on the first refresh. Inputs that remain missing after the grace period warn once; new outages and repeat outages after an input recovers warn immediately, even while other inputs are still starting. Each warned input logs once on loss and once on recovery, including its full observed outage duration. Reason changes are logged at info level and preserve the outage start; recovery requires every issue for that input to clear. Unrecognised issue text is omitted from logs.
- **Solar forecasts near midnight:** configure both today and tomorrow forecasts. Freshness follows the forecast intervals used by planning, including the final interval through its end and validated tomorrow coverage after rollover. Missing or expired coverage still blocks unsafe plans. Untimestamped values remain subject to the entity freshness timeout even when mixed with malformed dated records.
- **Automatic control is on but Armed is off:** check Current state, Next actions, active pauses, and the output of Run safety check or `ha_energy_planner.run_preflight`.
- **No action is planned:** confirm the relevant device-control switch is on and that the required tariff, PV, load, SOC, presence, and device inputs are current.
- **Climate lifecycle:** Active tariff cycles retain their original time grid across refreshes and restarts, but always revalidate against fresh timestamped prices. Successful takeover retains the original main target; release restores that target and dependent zone targets before closing dampers or shutting down the main unit. Real manual changes still supersede the corresponding saved state.
- **Climate learning and decisions:** Economic-model readiness is separate from legacy preconditioning. Decision summary and exported diagnostics expose learning status, normal observation/history counts, validation results, the latest climate execution and release outcomes, and pending restoration. During initial learning, legacy rejection reasons remain visible instead of being replaced by a generic learning message.
- **Pending climate restore:** Plan health reports degraded while a failed climate restore remains in durable ownership, with `pending_hvac_restore` identifying unresolved zone targets, main target, and automations. An off zone without a target, or a saved target outside its current bounds, waits without repeated invalid temperature commands. Other restorable zones and automations are released; the original target remains saved and is retried when compatible state returns. An already-observed saved target confirms recovery even if the current bounds would reject a new command. The planner does not clamp that target or turn a zone on just to restore it. Input health and confidence remain separately visible.
- **A device command fails or is not confirmed:** turn off Automatic control, run `ha_energy_planner.restore_safe_state`, and verify the mapped services and feedback entities.
- **Control should stop immediately:** turn off the relevant device-control switch or Automatic control. Use `ha_energy_planner.pause_control` for a bounded pause.
- **Multiple entries call the wrong planner:** pass the intended `config_entry_id` to the action.
- **Preconditioning did not run:** open **Decision summary → climate → preconditioning** for the current status, blocker, next start, candidate measurements, and retained missed window. See [climate decision explanations](docs/climate-decisions.md#why-preconditioning-did-not-run).
- **More evidence is needed:** download diagnostics from the integration page or run `ha_energy_planner.export_support_bundle`. Secrets, raw AI content, and unnecessary location history are excluded.

Device service dispatch is limited to 30 seconds per call, including restoration.
A timeout can occur after a device accepted the command; the planner retains
ownership evidence until state is reconciled. Weather requests have a ten-second
deadline and use a still-fresh cache when available. Recorder training runs in
the background, publishes only for the current configuration, and backs off
failed EV history imports for 15 minutes. Plans remain subject to the existing
input-health and control safety gates while training is pending.

Storage errors block new device acquisitions until recovery evidence is successfully written. Check disk space and Home Assistant storage permissions if a save fails. Startup recovery waits 30 seconds between unexpected failures. Use the restore action for existing ownership and verify device feedback before resuming control.

## Removal

1. Turn off **Automatic control** and each device-control switch.
2. Run `ha_energy_planner.restore_safe_state` and confirm EV, Enphase, thermostat, zone, and automation states.
3. Go to **Settings -> Devices & services -> Energy Planner** and delete the entry.
4. If installed through HACS, remove Energy Planner from HACS. For a manual install, delete `custom_components/ha_energy_planner`.
5. Restart Home Assistant if you removed the custom integration files.

Deleting an entry removes its retained model and audit storage only when its saved ownership and EV reservation are resolved. Uncertain recovery evidence is retained and logged; resolve device state before deleting the entry. The shared legacy import archive is retained.

Removing Energy Planner stops future plans and commands. It does not remove source integrations, their entities, vendor accounts, or vendor data.

## Useful Links

- [Setup examples, upgrades and stable contracts](docs/stable-release.md)
- [Release acceptance checklist](docs/release-checklist.md)
- [Releases](https://github.com/barneyonline/ha-energy-planner/releases)
- [Issue tracker](https://github.com/barneyonline/ha-energy-planner/issues)
- [Release notes](CHANGELOG.md)
- [Requirements and implementation evidence](docs/requirements-audit.md) — `scripts/docker-validate.sh` runs the full suite and compatibility checks; the 2026.6.0 compatibility modules use separate interpreters and retain all failure exit codes.
- [Architecture review and implementation evidence](docs/architecture-review-2026-09-05.md)
- [Quality-scale evidence](quality_scale.yaml)
- [Home Assistant Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)

### Multiple cars sharing one charger

Configure the shared charger's Plugged In sensor, charging feedback and start/stop controls in **Configure → Planner settings**. Then use **Configure → Add vehicle** for each car. Each profile requires its own charging-port sensor, home-presence entity, SOC sensor and target-SOC sensor, plus a ready-by time. Vehicle charging power is optional; leave it blank to inherit the shared charger power. BMW CarData `CONNECTED` / `DISCONNECTED` and location `home` are supported. A home binary sensor may report `on` / `off`. Target SOC is read only: there is no configured target or target fallback.

The **EV vehicle** selector offers **Auto**, each vehicle name, and **Manual — no tracked charging**. Auto requires the home charger to be plugged in and exactly one vehicle to be connected at home; another vehicle with missing evidence must be ruled out before identification. **Active EV vehicle** shows the result, with the detection reason in its attributes. Manual vehicle selection overrides identity detection but still requires a plugged-in charger and valid SOC/target readings.

When identification is uncertain, target/SOC is unavailable, or Manual is selected, Energy Planner withholds all EV commands and leaves charging untouched. Selecting Manual invalidates pending starts, stops and retries and releases planner ownership without sending a charger command. Charger-native schedules and limits still apply. Manual can be selected before a guest plugs in and resets to Auto on unplug; its selection survives reloads while still plugged in.

Unplugging ends the session. After another car is plugged in, the planner identifies it and builds a new plan with its SOC, target and ready-by time. Delayed telemetry showing both cars connected causes a wait, never a guess. A car that still reports connected from the previous session cannot be selected automatically again until its port reports disconnected; manual vehicle selection can resolve a missed port update. This handles physical swaps automatically; it does not schedule future cable swaps or promise readiness for cars that are not connected.

Manual charging overrides reset before a replacement vehicle’s plan is built. Ready-by times and charging characteristics belong to each vehicle; electricity-price policies remain shared. Charging calibration is learned separately from completed charging intervals observed while that car is identified at home. Interrupted, ambiguous and Manual intervals are discarded, including stop/resume or unavailable feedback between planner refreshes; the configured initial SOC-per-kWh estimate is used until sufficient per-car evidence exists. Energy Planner’s **Shared charger power (kW)** describes the charger capacity and reserves load for Manual or unidentified charging. Each Vehicle’s optional **Vehicle charging power override (kW)** is for cars that charge more slowly; planning uses the lower of the override and shared charger power. Existing configured vehicle power remains an override; clear it to follow future shared charger power changes. SOC, target SOC, ready-by and the initial SOC-per-kWh estimate are configured only on the Vehicle once profiles are enabled. The initial SOC-per-kWh estimate is under **Advanced calibration**, with a default for new vehicles. Shared electricity-price and charging policies remain in Energy Planner’s **Charger and charging policy** section.

Existing single-vehicle configurations continue unchanged until profiles are added. Configure **every** tracked car before enabling Auto control. Once profiles are enabled, old single-car SOC/target mappings are ignored, including after the last profile is removed. Removing profiles never silently reactivates the old mapping. Use **Configure → Edit vehicle** to edit a profile and **Configure → Remove vehicle** to remove it; `set_ev_ready_by` updates the currently identified or manually selected tracked vehicle. Ready-by changes replan without reloading the integration or restoring other devices. A new installation can save charger connection and charging feedback before adding its first vehicle.

Manual and unidentified charging still reserve the configured charging power in household load and cost projections, conservatively throughout the horizon until feedback confirms charging has stopped or the cable is unplugged. No EV commands are issued in these modes. Confirmed unplug events release the previous session’s grid reservation, including rapid swaps between refreshes, during pending storage writes, or immediately after a charger-state outage. Port-disconnect evidence is saved even while no vehicle is selected, so a reload cannot reinstate a cleared stale-connection block.
