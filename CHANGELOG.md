# Changelog

## Unreleased

### Changed

- The AI-provider privacy notice is logged at informational level instead of surfacing as a Home Assistant warning.
- The plan-fallback notification toggle now uses the same heading-and-description style as the surrounding safety controls.

## 0.5.1 - 2026-07-12

### Added

- An **AI and safety** option to disable and dismiss recurring unsafe-input, grid-limit, and HAEO fallback notifications without weakening fail-closed behavior.

## 0.5.0 - 2026-07-12

### Added

- Optional second PV forecast input with timestamp-safe Today/Tomorrow stitching.
- Per-input forecast coverage diagnostics and bounded conservative baseline-load leading-gap fill.
- Refresh-trigger, phase-timing, retention, HAEO-evidence, and usable-horizon diagnostics.
- Versioned thermal learning, production evidence contracts, fresh-plan activation checks, and shared fail-closed pause parsing.

### Changed

- The recommended default planning horizon is now 12 hours. Continuous forecast coverage is healthy at 12 hours, degraded from 8 to under 12 hours, and unsafe below 8 hours.
- Required point-only inputs no longer masquerade as full forecasts; secondary PV stitching requires timezone-aware timestamps and does not calibrate slots without primary-source provenance.
- Replanning uses an explicit decision-input allowlist, one-minute non-manual floor, coalescing, and stable input fingerprints.
- AI advice runs after plan commit as a cancellable single-flight task and is published only for the current safe plan.
- Thermal learning uses explicit HVAC mode/power evidence, minimum sample spacing, plausible-rate gates, and bounded robust medians.
- Forecast calibration and retained planner evidence use bounded, migration-safe, time-aware storage.

### Fixed

- Dry-run actions are recorded as skipped instead of rejected, while repeated dry-run evidence is coalesced without hiding real command attempts.
- HAEO is ready only when response-capable services return continuous import and export evidence across enough solve slots.
- Planner-owned device feedback is suppressed only when a successful command matches the observed state.
- Stale AI results, stale plans, active pauses, changed control contracts, and missing or malformed production state now fail closed.
- Corrupt thermal, calibration, retention, pause, boolean, and evidence-counter state is reset, filtered, or blocked safely.

### Upgrade Notes

- Existing configured planning horizons are preserved; review horizons above 12 hours against the actual Amber coverage available at your site.
- Configure the optional secondary PV entity only when it exposes timezone-aware timestamps. Solcast tomorrow data can then extend the today forecast safely.
- Production evidence is tied to the mapped control surfaces and decision policy. Relevant configuration changes require new healthy dry-run evidence before active commands resume.
- Legacy thermal and forecast-calibration statistics are migrated or reset before they can influence planning.
- AI provider integrations may log prompts independently; review provider logging settings before enabling advisory features.

### Validation

- Dockerized pytest: `647 passed`
- Coverage: `100%` across `7,314` statements
- Replay, live-schema, real-history, quality-scale, Home Assistant `check_config`, and Docker smoke validation

## 0.4.0 - 2026-07-12

### Added

- Optional grid carbon-intensity forecasts with carbon-aware EV slot allocation and action scoring.
- Conservative PV lower bounds and load upper bounds learned independently per forecast lead time.
- Refresh, HAEO latency/cache/capability, calibration, uncertainty, and cost-horizon telemetry.

### Changed

- EV ready-by deadlines now use the Home Assistant timezone, handle DST gaps/rollovers, and preserve an absolute UTC deadline.
- HVAC lookahead and preconditioning windows now use elapsed time instead of assuming five-minute slots.
- Forecast training retains dense near-term evidence and sparse samples across the full configured horizon.
- HAEO calls detect response/flexible-load capabilities, skip unsupported second passes, cache equivalent short-lived solves, and fail closed on ambiguous native config entries.
- Production preflight now requires only configured and enabled control areas, allowing safe partial installations.
- Monetary forecasts use Home Assistant's configured currency and expose the actual priced horizon.

### Fixed

- Carbon priority no longer contributes an unconditional zero score.
- Solar-flexibility and battery-safety decisions now use conservative learned forecast bounds while cost estimates retain expected values.

## 0.3.0 - 2026-07-12

### Added

- Optional measured PV and household-load power inputs for time-aligned forecast calibration.
- Independent 30-minute lead-time calibration models with robust median fitting and later holdout validation.
- Rolling-origin PV/load forecast accuracy validation with MAE and RMSE by near, day, and long horizon.
- Persistence-baseline gates for exported real forecast evidence.

### Changed

- Successful flexible-load HAEO results now regenerate the final plan instead of only updating stored evidence.
- Forecast confidence now accounts for actual horizon coverage.
- Required forecasts with missing or internally gapped coverage fail closed instead of repeating the last value.
- Estimated daily cost now uses HAEO grid flows where complete and battery charge/discharge evidence otherwise.
- Forecast attribute changes, including canonical camelCase variants, trigger replanning without reacting to unrelated metadata churn.

### Fixed

- Prevented forecast calibration from treating forecast entity states as measured ground truth.
- Prevented overdue forecasts from being paired with one current observation after downtime.
- Prevented correlated refresh snapshots and near-term bias from leaking calibration into unvalidated day-ahead slots.
- Prevented partial HAEO grid-flow evidence from suppressing fallback cost calculation.
- Prevented timestamp gaps inside a forecast from being silently forward-filled.

### Upgrade Notes

- To enable forecast calibration, configure separate **Observed PV power** and **Observed baseline load power** sensors in the Energy subentry. Do not select the forecast sensors themselves.
- Existing pre-0.3 calibration state is ignored until the new timestamped per-lead model has enough holdout-validated evidence.
- Required forecast sources should cover the complete planning horizon; incomplete horizons now mark planning inputs unsafe.

### Validation

- Dockerized pytest: `519 passed`
- Coverage: `100%` across `6,188` statements
- Replay, live-schema, rolling forecast-accuracy, Home Assistant `check_config`, quality-scale, and Docker smoke validation

## 0.2.1 - 2026-07-07

### Fixed

- Removed duplicated device names from AI, EV, Climate, and Enphase switch labels.
- Added a one-time entity registry cleanup for duplicated entity IDs generated by earlier labels.

## 0.2.0 - 2026-07-06

### Added

- Marginal-value planning evidence across EV, climate, Enphase, solar surplus, battery reserve, and tariff value.
- Weighted device priority scoring based on the configured planning priority order.
- Battery modelling options for usable capacity, round-trip efficiency, maximum charge power, and maximum discharge power.
- Capacity- and efficiency-aware Enphase profile decisions.
- Solar-aware EV charging allocation using effective cost across surplus solar and grid import.
- Climate thermal-shift planning with comfort coasting, active heat/cool learning, and estimated preconditioning windows.
- Subsystem confidence reporting for tariff, solar, load, climate, EV, and Enphase planning.
- Per-subsystem confidence thresholds that can block low-confidence device decisions.
- Decision audit, rejected actions, upcoming timeline, and per-device Decision sensors.
- Action backoff when a device command fails.
- Conflict detection when recent planner-owned EV or Enphase state appears to have been changed externally.

### Changed

- Enphase decisions no longer rely on simple tariff spread alone. Battery and solar value now need enough usable capacity and configured savings value.
- EV charging plans prefer lower effective-cost windows and include solar/grid split details.
- Plan attributes use more plain-English summaries for accepted and rejected decisions.
- The production safety model now records clearer reasons when control is paused by failures or conflicts.

### Upgrade Notes

- Review the new **EV, battery, and grid** policy options after upgrade:
  - usable battery capacity
  - battery round-trip efficiency
  - maximum battery charge power
  - maximum battery discharge power
- Review the new **Data health** confidence thresholds. The defaults are intentionally conservative.
- Check the new Decision, Decision audit, Rejected actions, and Upcoming timeline entities before arming production control.
- Run preflight and allow several healthy dry-run cycles before enabling active device control.

### Validation

- `ruff check custom_components/ha_energy_planner tests`
- Dockerized pytest and coverage: `492 passed`, `100%` coverage
- Translation JSON validation
