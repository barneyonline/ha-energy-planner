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

> [!IMPORTANT]
> Energy Planner is an unofficial community project in active development. It is not affiliated with, endorsed by, or supported by Home Assistant, Enphase, Amber Electric, Solcast, Daikin, or other vendors.
>
> Automatic control can issue real device commands. Begin in review mode, check the proposed plan and safety evidence, and keep the vendors' own protections and limits enabled.

The Platinum quality-scale label is a repository self-assessment against the current Home Assistant integration quality rules. As a custom integration, Energy Planner is not reviewed, security audited, maintained, or supported by the Home Assistant project. Rule-by-rule evidence is tracked in [`quality_scale.yaml`](quality_scale.yaml).

## Supported Functionality

Energy Planner reads existing Home Assistant entities and calls Home Assistant services. It does not connect directly to vendor clouds or replace the integrations that supply device data and controls.

Planning and control include:

- Tariff-aware EV charging with ready-by, target-SOC, solar, battery-reserve, and grid-limit constraints.
- Climate preconditioning around expensive tariff periods, with presence, comfort, manual-override, ownership, and rollback safeguards.
- Enphase self-consumption, backup, and AI-profile selection where mapped controls support it.
- A Recorder-trained household-load forecast with conservative validation and fail-closed handling of missing or stale data.
- A plan calendar, current state, next actions, input health, forecast confidence, production readiness, and redacted support data.
- Diagnostic sensors for Decision summary, Plan health, Current load forecast, Planning duration, and load-forecast coverage.
- Clear `review`, `recovery`, and `active` mode states so startup recovery is distinguishable from normal review mode.
- Independent switches for climate, EV, and Enphase control, plus a guarded Automatic control switch.
- Optional AI Task explanations that remain advisory and cannot call services or bypass constraints.

Provided Home Assistant actions:

- `ha_energy_planner.replan`: request an immediate planner refresh.
- `ha_energy_planner.run_preflight`: check active-mode readiness without issuing commands.
- `ha_energy_planner.restore_safe_state`: restore planner-owned EV, Enphase, and HVAC state where supported.
- `ha_energy_planner.pause_control` and `ha_energy_planner.resume_control`: pause all control or a selected device class.
- `ha_energy_planner.set_ev_ready_by`: update the EV ready-by time.
- `ha_energy_planner.set_manual_hvac_override`: block planner HVAC control for a bounded period.
- `ha_energy_planner.export_diagnostics` and `ha_energy_planner.export_support_bundle`: return redacted troubleshooting evidence.
- `ha_energy_planner.arm_production_control` and `ha_energy_planner.disarm_production_control`: explicitly manage the advanced production safety gate.

With multiple planner entries, provide `config_entry_id` when calling an action. Create one named Energy Planner entry per EV when managing multiple vehicles.

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

- Home Assistant `2026.6.0` or newer.
- Import and export tariff forecast sensors, commonly supplied by Amber Electric.
- An external PV forecast, commonly Solcast Forecast Today and optionally Forecast Tomorrow.
- A measured whole-home instantaneous consumption sensor in W, kW, or MW.
- Home Assistant Recorder with retained history for the household-load sensor.
- Battery state of charge and the entities or actions required by each device area you want Energy Planner to control.

Weather, carbon intensity, measured PV power, and AI explanations are optional. An external solar forecast remains required. The integration has no third-party Python dependencies and does not require separate vendor credentials.

## Configuration

Initial setup asks for a name and the core planning inputs. After adding the integration, open **Configure** to manage six areas:

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

## Known Limitations

- Tariff and PV forecasts must come from other Home Assistant integrations. Energy Planner does not fetch forecasts directly.
- The load model needs at least three qualifying days of Recorder history and must pass coverage and holdout checks before forecast-dependent commands are allowed.
- Missing, stale, invalid, or unconfirmed inputs fail closed. This can suppress otherwise economical actions.
- Enphase control is limited to the verified profiles exposed by the mapped Home Assistant integration; it does not directly command battery charge or discharge power.
- EV control requires a mapped target-SOC entity and confirmed charger feedback. Multiple EVs require separate Energy Planner entries.
- Climate takeover requires enough mapped state to restore the thermostat, configured zones, and automations safely.
- Optional AI explanations depend on a configured Home Assistant `ai_task` entity and remain advisory only.
- Bypassing safety gates is an advanced, default-off setting that reduces protection and should be used only with an explicit understanding of the risk.

## Troubleshooting

- **Load model stays in learning:** verify Recorder history, the sensor unit, and that the source represents gross household demand rather than solar, energy totals, forecasts, or signed net grid flow.
- **Automatic control is on but Armed is off:** check Production readiness, Current state, Next actions, active pauses, review evidence, and the latest preflight result.
- **No action is planned:** confirm the relevant device-control switch is on and that the required tariff, PV, load, SOC, presence, and device inputs are current.
- **A device command fails or is not confirmed:** turn off Automatic control, run `ha_energy_planner.restore_safe_state`, and verify the mapped services and feedback entities.
- **Control should stop immediately:** turn off the relevant device-control switch or Automatic control. Use `ha_energy_planner.pause_control` for a bounded pause.
- **Multiple entries call the wrong planner:** pass the intended `config_entry_id` to the action.
- **More evidence is needed:** download diagnostics from the integration page or run `ha_energy_planner.export_support_bundle`. Secrets, raw AI content, and unnecessary location history are excluded.

## Removal

1. Turn off **Automatic control** and each device-control switch.
2. Run `ha_energy_planner.restore_safe_state` and confirm EV, Enphase, thermostat, zone, and automation states.
3. Go to **Settings -> Devices & services -> Energy Planner** and delete the entry.
4. If installed through HACS, remove Energy Planner from HACS. For a manual install, delete `custom_components/ha_energy_planner`.
5. Restart Home Assistant if you removed the custom integration files.

Removing Energy Planner stops future plans and commands. It does not remove source integrations, their entities, vendor accounts, or vendor data.

## Useful Links

- [Releases](https://github.com/barneyonline/ha-energy-planner/releases)
- [Issue tracker](https://github.com/barneyonline/ha-energy-planner/issues)
- [Release notes](CHANGELOG.md)
- [Requirements and implementation evidence](docs/requirements-audit.md)
- [Quality-scale evidence](quality_scale.yaml)
- [Home Assistant Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
