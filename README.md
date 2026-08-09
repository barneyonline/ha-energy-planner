<p align="center">
  <img src=".github/assets/icon.svg" alt="Energy Planner icon" width="96" height="96">
</p>

# Energy Planner

> [!IMPORTANT]
> Energy Planner is in active development. Do not rely on it as the only protection for equipment, billing, comfort, or vehicle readiness.

[![Release](https://img.shields.io/github/v/release/barneyonline/ha-energy-planner?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-energy-planner/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/ci.yml?branch=main&label=ci)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/barneyonline/ha-energy-planner)](LICENSE)

Energy Planner is a local-first Home Assistant custom integration for coordinating energy prices, solar, household load, batteries, EV charging, climate comfort, and Enphase operating profiles.

## What it provides

- One Energy Planner device containing all status, controls, and troubleshooting entities
- One central **Configure** page with sections for Energy, Climate, Presence, Enphase, AI, and EV inputs
- **Armed**, **Current state**, **Next actions**, and **Plan** calendar entities
- A guarded **Automatic control** switch plus individual **Climate control**, **EV control**, and **Enphase control** switches
- A built-in household-load forecast trained from Home Assistant Recorder
- Native EV scheduling and tariff-aware climate preconditioning
- Optional Enphase profile control, HAEO evidence, and on-demand AI troubleshooting
- Notifications only for problems that normally require user action

## Requirements

Energy Planner reads existing Home Assistant entities and calls Home Assistant services. It does not connect directly to vendor clouds.

For full planning, configure:

- Import and export tariff forecast sensors, commonly Amber Electric
- An external PV forecast, commonly Solcast Forecast Today and optionally Forecast Tomorrow
- A measured whole-home instantaneous consumption sensor in W, kW, or MW
- Home Assistant Recorder
- Battery state of charge and the entities for each device you want to control

Weather, carbon intensity, measured PV power, HAEO, and AI are optional. HAFO is not required. An external solar forecast is still required.

Compatibility: Home Assistant 2026.6.0 or newer; current integration version 0.9.1.

## Installation

### HACS custom repository

1. Open **HACS → Integrations**.
2. Select **Custom repositories** from the three-dot menu.
3. Add `https://github.com/barneyonline/ha-energy-planner` as an Integration repository.
4. Download **Energy Planner** and restart Home Assistant.
5. Open **Settings → Devices & services → Add integration** and add **Energy Planner**.
6. Open **Configure** on the integration to map inputs and review policy.

### Manual installation

1. Copy `custom_components/ha_energy_planner` into Home Assistant's `custom_components` directory.
2. Restart Home Assistant.
3. Add **Energy Planner** from **Settings → Devices & services**.

## Initial configuration

1. Map tariff forecasts, external PV forecasts, household load, battery SOC, and the device inputs you intend to use.
2. Confirm Recorder is enabled and retains the household-load sensor.
3. Review EV, battery, grid, climate, and confirmation settings.
4. Enable the individual device-control switches for the devices Energy Planner may manage.
5. Leave **Automatic control** off while reviewing **Current state**, **Next actions**, and the **Plan** calendar.
6. Wait for the built-in load model to become ready and resolve any unsafe inputs.
7. Turn on **Automatic control**. It activates only after preflight and review evidence pass.

If activation is not safe, the switch remains off and ordinary device commands are not issued.

## Household-load forecast

Choose a sensor that reports gross household demand. Do not use:

- Solar production or an energy total in kWh
- A forecast sensor
- Signed net grid flow that becomes negative while exporting
- A cloud-derived value with large transient negative readings

Negative and unavailable readings are treated as missing, not clamped to zero. A grouped sensor can be used, but it represents only its included circuits and may underestimate unmonitored load.

Energy Planner trains from up to 28 days of Recorder history. It removes known EV-charging intervals, subtracts measured HVAC power when available, and builds expected and conservative load profiles.

The model requires:

- Seven complete local-time days
- At least 80% overall valid coverage
- Two holdout origins with at least 144 aligned samples
- Accuracy no more than 10% worse than previous-day persistence
- At least 90% conservative-bound coverage

**History days** means Recorder data exists for those dates. **Complete days** is stricter: every 15-minute bucket must have enough valid cleaned data. EV charging, negative readings, unavailable states, or missing HVAC alignment can therefore produce nine history days but only three complete days.

Training runs at startup, after a source change, and at most every six hours. Replanning cannot create missing history. While the status is `learning`, plans remain visible but forecast-dependent active commands are blocked. Models are `ready` through 24 hours old, `degraded` from 24 to 72 hours, and `stale` after 72 hours.

Changing the load mapping disarms production control and requires fresh review cycles. Routine retraining does not.

## Controls and status

| Entity | Purpose |
| --- | --- |
| **Armed** | Whether Energy Planner may currently issue commands and why |
| **Current state** | Live state of every configured controlled area |
| **Next actions** | Next state for every area, planned actions, and decision evidence |
| **Plan** | Calendar view of upcoming controlled actions |
| **Automatic control** | Runs preflight and enables or disables all planner-owned commands |
| Device control switches | Select whether Climate, EV, or Enphase may participate |
| **Explain or troubleshoot** | Requests one evidence-based AI recommendation on demand |

Mapped EV start and stop controls are planner actuators. Energy Planner does not expose separate manual Start charging or Stop charging buttons. Target SOC is configured centrally or read from an optional external target sensor; it is not exposed as an Energy Planner number entity.

## Planning behavior

EV planning considers target SOC, ready-by time, price, solar, battery reserve, grid limits, connection state, and confirmed charger feedback. It supports a persistent charger switch or separate start/stop controls. Multiple EVs require separate named Energy Planner entries.

Climate planning searches the configured tariff horizon for a lower-cost preconditioning window before an expensive period. It can temporarily take ownership of configured climate automations and zones, then restores them when the period ends, comfort is reached, inputs become unsafe, or a manual override occurs.

Enphase planning can select configured self-consumption, backup, or AI profiles. Only actions that actually consume HAEO evidence depend on HAEO.

## Safety and recovery

- Device commands require healthy inputs, current plans, matching review evidence, production arming, and enabled device controls.
- Hard constraints are checked again immediately before every command.
- Unsafe, missing, stale, or invalid inputs fail closed.
- Learning and ordinary plan changes remain silent; actionable mapping, restoration, EV-readiness, and grid-limit problems can notify once.
- AI advice cannot call services, change settings, or bypass constraints.

If control behaves unexpectedly:

1. Turn off **Automatic control**.
2. Run `ha_energy_planner.restore_safe_state` or press **Restore safe state**.
3. Confirm EV, Enphase, climate automation, and zone states manually.
4. Review **Current state**, **Next actions**, preflight, and the support bundle.
5. Leave automatic control off until the cause is understood.

## Services

Common services include:

- `ha_energy_planner.replan`
- `ha_energy_planner.run_preflight`
- `ha_energy_planner.export_support_bundle`
- `ha_energy_planner.restore_safe_state`
- `ha_energy_planner.pause_control` and `ha_energy_planner.resume_control`
- `ha_energy_planner.set_ev_ready_by` and `ha_energy_planner.set_ev_target_soc`
- `ha_energy_planner.set_manual_hvac_override`

Advanced arm/disarm and diagnostics services are also available in Home Assistant's service UI. With multiple planner entries, provide `config_entry_id`.

## Development

Run the complete validation gate:

```bash
scripts/docker-validate.sh
```

It runs Ruff, the full pytest suite with 100% coverage, replay/schema/history validation, Home Assistant `check_config`, and a Docker smoke test.

Start a local Home Assistant instance with:

```bash
docker compose up
```

Then open <http://localhost:8124>.

## Further documentation

- [Release notes](CHANGELOG.md)
- [Requirements and implementation evidence](docs/requirements-audit.md)
- [Quality-scale evidence](quality_scale.yaml)
