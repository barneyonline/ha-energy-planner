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

Tariff plans retain the provider's UTC boundaries across refreshes, including partial current intervals. Climate calendar entries show planned preconditioning and coast phases; command deadlines appear in their details. Finishing temperatures remain unknown until the relevant heating or cooling rate is ready. HVAC timing and room-feedback edits preserve unrelated controls; changing actuators or global safety settings retains guarded restoration.

Missed-preconditioning diagnostics reconcile a changed tariff tail with confirmed execution of the same preparation episode. Matching requires the same configuration, mode, target, peak and preparation deadline. Confirmed evidence retains its original execution time and window across overlapping replacements whose regenerated starts differ; a replacement plan alone does not prove success. Older history or coasting ownership without this correlation evidence retains strict lifecycle matching.

EV daylight and continuous charging, PV calibration lead times and climate observation comparisons also use actual interval boundaries.
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
