<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/readme-banner-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/readme-banner-light.svg">
  <img src="docs/images/readme-banner-light.svg" alt="Energy Planner — Plan locally. Review clearly. Control with confidence." width="1280">
</picture>

<p align="center">
  <a href="https://github.com/barneyonline/ha-energy-planner/releases"><img src="https://img.shields.io/github/v/release/barneyonline/ha-energy-planner?display_name=tag&amp;sort=semver" alt="Latest release"></a>
  <a href="https://codecov.io/gh/barneyonline/ha-energy-planner"><img src="https://codecov.io/gh/barneyonline/ha-energy-planner/branch/main/graph/badge.svg" alt="Codecov coverage"></a>
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=barneyonline&amp;repository=ha-energy-planner&amp;category=integration"><img src="https://img.shields.io/badge/HACS-Open_repository-41BDF5.svg" alt="Open Energy Planner in HACS"></a>
</p>

<p align="center">Coordinate EV charging, climate comfort and Enphase controls with your home's energy plan.</p>

<p align="center">
  <a href="#get-started">Install</a> ·
  <a href="#key-features">Features</a> ·
  <a href="#at-a-glance">Screenshots</a> ·
  <a href="https://github.com/barneyonline/ha-energy-planner/wiki">Documentation</a>
</p>

Local-first planning through your existing Home Assistant entities and actions.

## Key features

- **EV charging:** Tariff-aware schedules, ready-by targets and vehicle profiles for a shared charger.
- **Climate comfort:** Preconditioning with room feedback, learned thermal rates and guarded restoration.
- **Enphase controls:** Operating profiles and independent Export Limit control where supported.
- **Load forecasting:** Recorder-trained household demand alongside solar, tariffs and battery reserves.
- **Review and control:** Current state, next actions, a plan calendar and per-area Automatic control.
- **Diagnostics and explanations:** Redacted diagnostics and optional, on-demand AI advice.

## At a glance

Choose which areas the planner manages, then review its state and next actions.

<!-- Inline images wrap on narrow screens. Keep their natural proportions. -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/planner-controls-dark.jpg">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/planner-controls-light.jpg">
    <img src="docs/images/planner-controls-light.jpg" alt="Planner controls: Automatic control, climate, Enphase Export Limit, Enphase Profile, EV control, vehicle and Explain" width="320" align="top">
  </picture>
  &nbsp;
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/planner-sensors-dark.jpg">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/planner-sensors-light.jpg">
    <img src="docs/images/planner-sensors-light.jpg" alt="Current state and next actions: active vehicle, Armed, climate control active, charging status, mode, plan and recovery" width="320" align="top">
  </picture>
</p>

*Examples from a configured installation; live settings and readings are not first-run defaults.*

<details>
<summary>More screenshots · diagnostics, plan calendar and history</summary>

### Forecast and plan health

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/planner-diagnostics-dark.jpg">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/planner-diagnostics-light.jpg">
    <img src="docs/images/planner-diagnostics-light.jpg" alt="Planner diagnostics: current load forecast, decision summary, forecast coverage, plan health and planning duration" width="320">
  </picture>
</p>

### Plan calendar

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/plan-calendar-dark.jpg">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/plan-calendar-light.jpg">
  <img src="docs/images/plan-calendar-light.jpg" alt="Plan calendar showing confirmed Export Limit history alongside upcoming EV charging and Export Limit windows" width="757">
</picture>

The calendar distinguishes upcoming plan windows from retained confirmed activity. See [Plan Calendar](https://github.com/barneyonline/ha-energy-planner/wiki/Plan-Calendar).

</details>

<a id="quick-install-hacs"></a>

## Get started

1. [Open Energy Planner in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=barneyonline&repository=ha-energy-planner&category=integration), add the repository if prompted, and download it.
2. Restart Home Assistant, then add **Energy Planner** under **Settings → Devices & services → Add integration**.
3. Map your inputs in **Configure → Planner settings**. Keep **Automatic control** off while reviewing plans and the safety check.

Enter your Home Assistant URL if prompted by the HACS link. [Manual installation and removal](https://github.com/barneyonline/ha-energy-planner/wiki/Installation).

Requires **Home Assistant 2026.10.0+** (Python **3.14+**). Normal planning needs tariff and solar forecasts, household consumption with Recorder history, and battery SOC. [Export-only operation](https://github.com/barneyonline/ha-energy-planner/wiki/Enphase-Export-Limit) has narrower requirements.

**Automatic control** records your request to operate; **Armed** reports actual command authority.

## Documentation

[Setup](https://github.com/barneyonline/ha-energy-planner/wiki/Hub-and-Input-Setup) · [Safety and production](https://github.com/barneyonline/ha-energy-planner/wiki/Safety-and-Production) · [Troubleshooting](https://github.com/barneyonline/ha-energy-planner/wiki/Troubleshooting) · [Known limitations](https://github.com/barneyonline/ha-energy-planner/wiki/Known-Limitations)

The [planning and control guide](docs/planning-and-control.md) preserves the detailed operating notes, compatibility requirements and safety limits.

<details>
<summary>Planning and control details</summary>

<a id="supported-control-areas"></a>
<a id="compatibility"></a>

- [Supported control areas](docs/planning-and-control.md#supported-control-areas) and [compatibility](docs/planning-and-control.md#compatibility)
- [EV scheduling, suspension, daylight preference and cost checks](docs/ev-scheduling.md)
- [Climate demand, thermal learning, independent zones and equipment compensation](docs/climate-decisions.md)
- [HVAC restoration and recovery](docs/hvac-restoration-recovery.md)
- [Export Limit timing and confirmed readback](docs/export-limit-timing.md)
- [Load forecasting, tariffs, solar and battery reserves](https://github.com/barneyonline/ha-energy-planner/wiki/Energy-Planning)
- [Calendar timing, planned windows and confirmed history](https://github.com/barneyonline/ha-energy-planner/wiki/Plan-Calendar)

Configured through the Home Assistant UI. No separate vendor credentials or third-party Python dependencies are required. AI explanations are advisory; selected planner context goes to your configured provider only when requested.

</details>

[Releases](https://github.com/barneyonline/ha-energy-planner/releases) · [Changelog](CHANGELOG.md) · [Issues](https://github.com/barneyonline/ha-energy-planner/issues) · [Development](https://github.com/barneyonline/ha-energy-planner/wiki/Development) · [Requirements evidence](docs/requirements-audit.md) · [Quality-scale evidence](quality_scale.yaml) · [Validation warnings](docs/validation-warnings.md)

The Platinum quality-scale claim is a repository self-assessment. This custom integration is not reviewed, security audited, maintained or supported by the Home Assistant project.

---

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/tests.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/hassfest.yml)
[![Validation](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/validate.yaml?branch=main&label=validation)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/validate.yaml)
[![Quality scale: Platinum self-assessment](https://img.shields.io/badge/quality_scale-Platinum-00a9ce)](quality_scale.yaml)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/stargazers)
[![Open issues](https://img.shields.io/github/issues/barneyonline/ha-energy-planner)](https://github.com/barneyonline/ha-energy-planner/issues)
[![License](https://img.shields.io/github/license/barneyonline/ha-energy-planner)](LICENSE)
