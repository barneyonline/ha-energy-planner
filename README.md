# Energy Planner - Home Assistant Custom Integration

Local-first energy planning for Home Assistant, coordinating tariffs, solar, household load, batteries, EV charging, climate comfort and Enphase controls.

Energy Planner reads existing Home Assistant entities and calls their integration actions. It does not connect directly to vendor clouds or replace the integrations that supply device data and controls.

## Supported control areas

- EV charging through a stateful charger switch, with vehicle profiles for cars sharing one charger.
- Climate preconditioning through a main thermostat, configured zones and automations.
- Enphase operating profiles and independent Export Limit control, where supported by the upstream integration.

## Key features

- Tariff-aware EV scheduling with ready-by, target SOC, solar, battery-reserve and grid constraints.
- Climate preconditioning with room comfort protection, learned heating/cooling rates and guarded restoration.
- A read-only **Climate control active** binary sensor for coordinating other HVAC automations; see [automation guards](docs/climate-decisions.md#coordinating-other-automations).
- Recorder-trained household-load forecasts and fail-closed checks for missing or stale inputs.
- Current state, next actions, a plan calendar with confirmed history and redacted diagnostics.
- Review mode, per-area controls and guarded Automatic control, with Armed showing actual command authority.
- Optional on-demand AI Task explanations that remain advisory.

**Current load forecast** keeps the committed plan's value and model evidence visible while the next refresh saves its replacement. The new plan and its forecast appear together, avoiding brief unknown-state pulses in Recorder. Missing or invalid forecast evidence remains explicit; refresh failures and stale-plan safety checks retain their normal behavior.

EV charging waits when the vehicle reports `SUSPENDED_EV` / `SuspendedEV`, including after a planner stop or restart. This does not prove the SOC target was reached. Cached charging reports from before a recorded refusal do not reset the wait. See [vehicle suspension and retry rules](docs/ev-scheduling.md#vehicle-suspension-and-waiting).

Tariff plans retain the provider's UTC boundaries across refreshes, including partial current intervals. Climate calendar entries show planned preconditioning and coast phases; command deadlines appear in their details. Finishing temperatures remain unknown until the relevant heating or cooling rate is ready. HVAC timing and room-feedback edits preserve unrelated controls; changing actuators or global safety settings retains guarded restoration.

Missed-preconditioning diagnostics reconcile a changed tariff tail with confirmed execution of the same preparation episode. Matching requires the same configuration, mode, target, peak and preparation deadline. Confirmed evidence retains its original execution time and window across overlapping replacements whose regenerated starts differ; a replacement plan alone does not prove success. Older history or coasting ownership without this correlation evidence retains strict lifecycle matching.

Independent room completion is available for explicitly paired switch zones with fresh room sensors, confirmed actuator states and installer-verified minimum-airflow combinations. A completed room pauses while remaining rooms prepare; reopening requires a 0.3°C temperature margin and ten minutes paused. The shared head stops with an airflow path retained when preparation completes. Unsupported installations keep the existing shared-unit handoff. See [zone topology and airflow requirements](docs/climate-decisions.md#independent-zone-completion) before enabling this optional control.

Independent room control preserves the economic degradation guard: away-off ownership cannot start a new fallback cycle after economic control has activated. Existing tariff cycles can finish; explicit Legacy policy keeps its fallback behavior.

Unfinished room targets retain their paired actuator baselines through deferred recovery and reloads. Recovery under a manually controlled head waits for compatible room bounds before reopening a zone.

EV daylight and continuous charging, PV calibration lead times and climate observation comparisons also use actual interval boundaries.
Daylight preference never authorises a higher-cost charging candidate when a cheaper schedule meets the same readiness requirements. After safety, readiness and dwell checks, estimated cost takes precedence; daylight breaks economic ties. Forecast solar is valued at its export opportunity cost rather than treated as free energy.
Preliminary cost estimates preserve daylight options until physical capacity and readiness-buffer checks establish whether cheaper overnight charging can actually meet the same requirements.
Combined daylight and overnight charging explanations describe the capacity, readiness and cost checks without claiming that validation leaves the estimated cost unchanged.
Forecast confidence measures covered time, and confirmed climate activity keeps its identity when calendar labels change.
Coverage thresholds retain exact elapsed durations when tariff timestamps differ by seconds.

## Quick install (HACS)

1. Add `https://github.com/barneyonline/ha-energy-planner` to **HACS → Custom repositories** as an **Integration**.
2. Download Energy Planner and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Energy Planner**.
4. Open **Configure → Planner settings** to map inputs. Keep **Automatic control** off while reviewing plans and the safety check.

[Open in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=barneyonline&repository=ha-energy-planner&category=integration). Manual installation and removal: see [Installation](https://github.com/barneyonline/ha-energy-planner/wiki/Installation).

## Compatibility

- Home Assistant **2026.10.0** or newer, using Python **3.14+**.
- Normal energy planning requires tariff and external PV forecasts, measured household consumption with Recorder history, and battery SOC. Each control area needs its mapped controls and feedback.
- Export-only operation has narrower input requirements; see [Enphase Export Limit](https://github.com/barneyonline/ha-energy-planner/wiki/Enphase-Export-Limit).
- Export Limit accepts Amber's declared 30-minute intervals with first-second starts as contiguous half-hour windows. Other malformed intervals, gaps and overlaps still block control.
- The load model needs at least three qualifying history days and must pass validation before forecast-dependent commands are allowed.
- No third-party Python dependencies or separate vendor credentials are required. A configured AI provider may run remotely.

## Documentation

See the [Wiki](https://github.com/barneyonline/ha-energy-planner/wiki) for setup, configuration, actions, upgrades, troubleshooting and limitations.

- [Setup and configuration](https://github.com/barneyonline/ha-energy-planner/wiki/Hub-and-Input-Setup)
- [Safety and production](https://github.com/barneyonline/ha-energy-planner/wiki/Safety-and-Production)
- [Troubleshooting](https://github.com/barneyonline/ha-energy-planner/wiki/Troubleshooting) and [known limitations](https://github.com/barneyonline/ha-energy-planner/wiki/Known-Limitations)
- [Releases](https://github.com/barneyonline/ha-energy-planner/releases), [changelog](CHANGELOG.md) and [issues](https://github.com/barneyonline/ha-energy-planner/issues)
- [Development and validation](https://github.com/barneyonline/ha-energy-planner/wiki/Development), [requirements evidence](docs/requirements-audit.md) and [quality-scale evidence](quality_scale.yaml)

The Platinum quality-scale claim is a repository self-assessment. This custom integration is not reviewed, security audited, maintained or supported by the Home Assistant project.
