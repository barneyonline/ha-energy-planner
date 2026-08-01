# Changelog

## Unreleased

## 0.8.2 - 2026-08-01

### Fixed

- Plan fallback notifications are no longer recreated on every refresh when
  their title, message, and reason codes have not changed. A cleared condition
  can still notify again if it later recurs.
- AI advice remains visible across regenerated plan IDs when the material plan
  is unchanged, reports pending work while a provider call is in flight or
  rate-limited, and retries automatically when the provider-call window opens.
- Cached advice now follows the coordinator's latest-accepted policy, supports
  legacy nested fingerprints, stays hidden when AI advice is disabled, and no
  longer adds regenerated plan IDs to recorder-facing attributes.

### Validation

- Full Docker validation: `846 passed`, `100%` across `8,891` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.8.1 - 2026-07-31

### Fixed

- Energy Planner is classified as a service integration so configured entries
  appear on the main **Settings -> Devices & services -> Integrations** page
  instead of being grouped only with Helpers.

### Validation

- Full Docker validation: `837 passed`, `100%` across `8,768` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.8.0 - 2026-07-26

### Added

- Charging-state confirmation with bounded retries and a fail-closed timeout
  after direct EV start and stop commands.
- Optional external vehicle target-SOC input, a native connected helper, and a
  keep-charger-on policy for vehicle preconditioning after the charge target is
  reached.
- Multiple named Energy Planner entries so each EV has isolated entities,
  history, production arming, execution audit, and persisted planner state.

### Changed

- Integration services accept `config_entry_id`; it is required when more than
  one Energy Planner entry is loaded.
- Initial setup now asks for an instance name and rejects duplicate names.
- The first upgraded entry imports the legacy unscoped store into its new
  entry-scoped store once, marks the legacy source as consumed, and never
  offers that source to newly named entries.
- Failed EV charging confirmation now issues an immediate compensating restore
  or safe-stop command and retains ownership evidence when compensation cannot
  be completed.
- Connected-helper state is treated as runtime evidence, so plugging or
  unplugging does not invalidate an otherwise current production arming record.
- Preconditioning keep-on confirms the stateful charger control rather than
  requiring active charging feedback, and per-entry notifications no longer
  overwrite or dismiss alerts from another EV planner.
- Active EV entries share an atomic household grid-capacity reservation, with
  reservations retained until stop or safe-state restoration is confirmed.
  Observed disconnection now requests that confirmed stop instead of releasing
  capacity while a persistent charger control may remain enabled.

### Fixed

- EV starts now persist their provisional grid claim and original actuator
  topology before crossing the Home Assistant service boundary. After an
  interrupted command, active control confirms a recovery stop before allowing
  another start, including when the EV mapping changed during recovery.
- Existing reservation high-watermarks are checked against tightened import
  limits even when only one EV remains active. Shedding claims held by unloaded
  entries are relinquished so a loaded EV can shed controllable load while the
  unloaded entry's uncertain reservation remains protected.
- Ordinary below-target charging no longer inherits preconditioning-only
  control confirmation when keep-on is enabled.
- Preconditioning reserves the configured charger rate in the current grid and
  battery projection instead of presenting as a zero-load action.
- Safe-state restoration of a momentary start-button takeover issues the
  configured stop command when the saved button state cannot be replayed.
- Manual and scheduled starts now honour the native Connected helper when no
  external vehicle connected-state entity is mapped.
- Manual starts now require a usable grid projection, use the shared multi-EV
  capacity reservation, include their own charger load when absent from that
  projection, and preserve a restorable ownership baseline after successful or
  uncertain commands.
- Uncertain EV reservations survive config-entry unload, while each active
  reservation retains only its owner's configured import limit so departed
  entries cannot leave obsolete limits behind.
- Configured vehicle connection entities now fail closed while unavailable,
  and device grouping updates are isolated to the current config entry.
- Safe-state restore now honours charging-confirmation options and accepts a
  confirmed nested safe-stop compensation as a successful recovery.
- Manual EV starts reject stale or unsafe committed grid evidence instead of
  authorizing a command from an obsolete decision context.
- Persisted EV ownership now rehydrates conservative household reservations
  before entries execute after an unclean Home Assistant restart.
- Runtime option changes synchronize active reservation load and import-limit
  metadata before restore or replanning uses the updated configuration.
- Config-entry unload now stops when safe-state restoration fails, retaining
  the live coordinator for retry and diagnostics instead of abandoning state.
- Active EV reservations can increase but no longer shrink from a model-only
  charge-rate option change or a later start/no-op action while the charger may
  still be drawing power.
- Manual EV starts now expire decision evidence with the planning interval;
  manual device failures are persisted, placed into EV control backoff, and
  subsequent manual starts honour both that pause and command cooldown.
- Planner-owned EV power now receives one prioritized, audited safety-stop
  attempt per plan when the vehicle disconnects, EV control is disabled,
  required inputs become unhealthy, or a hard grid-import violation degrades
  an otherwise healthy plan. Reservations and ownership remain held when that
  stop cannot be confirmed.
- Failed config-entry unloads disarm production control without scheduling a
  replan that could issue new commands during teardown recovery.
- Preconditioning keep-on now uses only the persistent charger control, even
  when an optional separate start command is also mapped; options, EV
  reconfiguration, and preflight reject incompatible mappings before execution.
- Config flows reject native or legacy EV charger controls, Daikin,
  climate-automation, and Enphase actuators already controlled by another
  Energy Planner entry.
- Failed confirmation after a momentary start always uses the configured safe
  stop instead of treating restoration of an unrelated already-enabled control
  as a successful rollback.
- Safe-state restoration now persists the control that initiated EV ownership,
  so a successful momentary takeover cannot later skip its configured stop.
- Active EV reservation high-watermarks are persisted across unclean restarts;
  confirmed releases are persisted explicitly and do not rehydrate from stale
  ownership metadata.
- Scheduled EV stops now bypass failure pauses, command cooldowns, and daily
  action caps, matching manual-stop recovery behavior.
- Legacy helper-based EV schedules are consistently treated as charging starts
  for safety gates, household reservations, audit targets, and restoration.
- Separate start switches and input booleans now use the configured safe-stop
  control during restoration and failed-confirmation compensation instead of
  treating the command helper's prior state as proof that charging stopped.
- Separate stop switches now issue a stop command even when their helper state
  already appears off, and successful stops neutralize latched switch-based
  start commands before ownership and grid reservations are released.
- Legacy helper-based charging starts now participate in external-control
  conflict detection instead of overriding a recent third-party stop; conflict
  evidence is now bound to the prior command's direction and entity so the
  planner's own stop cannot block the next charging start.
- EV execution audits record the control actually commanded; conflict detection
  observes available charging feedback for momentary starts, falls back to a
  stateful charger control when feedback is unavailable, and uses the persistent
  control for preconditioning keep-on actions. Normal fully charged and
  disconnected states no longer trigger false external-override conflicts.
- Unavailable or invalid external vehicle targets now use the native target as
  a non-blocking advisory fallback. Preconditioning keep-on cannot energise the
  charger while that fallback is active or when the authoritative target
  exceeds configured SOC bounds.
- Coordinated execution now abandons remaining device commands when an input
  change or teardown makes the source plan obsolete during an earlier command.
- Restoring an already-active charger baseline clears planner stop ownership
  while retaining its load in shared multi-EV household capacity accounting.
- EV safety stops now ignore unrelated input/plan constraints and unavailable
  start controls. An unhealthy refresh with planner-owned EV power synthesizes
  a stop, retries through normal failure backoff, and clears ownership only
  after the stop succeeds.
- Safety-stop command acceptance is now distinct from proven-safe confirmation.
  Disconnected feedback and separate stop-helper state retain EV ownership and
  household capacity until the persistent charger control, meaningful charging
  feedback, or rollback proves the charger safe. A compensating stop that does
  prove the safe state is recorded as successful and releases ownership.
- Confirmed manual EV stops now clear the same persisted charger ownership as
  scheduled safety stops, avoiding a redundant restore after charging is safe.
- Owned automated and manual stops now use the persisted actuator topology, so
  a failed reconfiguration reload cannot stop the replacement charger and then
  discard recovery ownership for the original charger.
- Reservation-only restore failures now retain their provisional actuator
  topology, so every later retry continues targeting the charger that may still
  be active rather than a reconfigured replacement.
- Manual stops now treat a proven-safe compensating stop as successful, while
  an accepted but unconfirmed owned stop fails closed and retains ownership.
- EV ownership now persists the actuator topology that created it, so config
  entry reload after EV reconfiguration restores the original charger instead
  of resolving ownership through the replacement mapping.
- Existing multi-EV reservations that no longer fit the strictest household
  import limit now use one atomic shedding claim so a single planner-owned entry
  takes the confirmed safety-stop path; concurrent evaluations no longer stop
  every EV before the remaining capacity is re-evaluated.
- The integration-created Keep charger on switch now rejects start/stop-only
  mappings instead of bypassing options-flow validation.
- Planner-owned disconnect and recovery stops now run even when automation is
  disabled, dry-run is enabled, or the source plan has aged past its normal
  action window. A connected manual start still honours its one-hour override,
  and expired or malformed bounded overrides are pruned during live refreshes.
- Legacy global storage is migrated and rehydrated for the first actual legacy
  entry before any named multi-EV entry can execute, independent of config-entry
  ordering. Legacy single-button mappings are also rejected by capability
  discovery and manual-start gating because they cannot provide the required
  stop path.
- Manual EV commands, scheduled execution, explicit restoration, and config-entry
  teardown are now serialized. Queued refreshes cannot issue a new command during
  unload, and every planner-owned scheduled stop requires proven-safe completion
  before releasing ownership or household capacity.
- Failed multi-EV shedding claims rotate to another loaded charger while retaining
  the uncertain reservation, and continuous-charging mode returns an explicitly
  infeasible contiguous partial window instead of silently scheduling gaps.
- HVAC automation suppression and Enphase profile changes now use transactional
  compensation. Failed rollbacks retain only the unresolved baseline ownership so
  restore-safe-state can retry it after service or confirmation failures.
- Flexible HAEO responses must provide their own continuous grid evidence and now
  carry per-slot provenance, preventing both inherited-baseline acceptance and
  double-counting projected EV/HVAC load in limits and cost estimates.
- Store writes use serialized mutation generations, so transient or concurrent
  save failures remain retryable. Recorder import timestamps also survive live EV
  events and successful empty imports, avoiding repeated 30-day history scans.
- External ready-by and secondary-PV changes now trigger replanning, arbitrary
  configured planning intervals share one epoch cadence, and expired manual HVAC
  helpers are cleared with retryable ownership evidence.
- Advisory AI configuration no longer blocks production preflight, non-finite or
  boolean AI numerics are rejected, Enphase discovery honours custom control
  services, takeover diagnostics include reservation-only recovery, and system
  health aggregates multiple entries deterministically.
- Release metadata validation now agrees with multi-entry support and the
  manifest and package versions are aligned at 0.8.0.

### Validation

- Full Docker validation: `823 passed`, `100%` across `8,708` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.7.0 - 2026-07-17

### Changed

- Config-entry and subentry mapping changes now rebuild runtime listeners and
  entities, while failed platform unloads leave the coordinator operational.
- The manifest and quality evidence now describe Energy Planner as a
  single-entry calculated helper with a defensible Gold quality target.
- Explicit services and control buttons now raise translated Home Assistant
  errors when no runtime is loaded or a requested device action fails.

### Fixed

- Safe-state restoration retains ownership for each asset until its restore is
  confirmed, and entering disabled or dry-run mode restores planner takeovers.
- Manual HVAC detection now includes control-attribute changes without treating
  the planner's own in-flight setpoint changes as manual overrides.

### Validation

- Full Docker validation: `683 passed`, `100%` across `7,672` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.6.1 - 2026-07-17

### Fixed

- Dry-run comparison sensor attributes now publish compact recorder-safe
  summaries instead of repeating nested execution evidence that could exceed
  Home Assistant's 16,384-byte state-attribute limit.

### Validation

- Full Docker validation: `663 passed`, `100%` across `7,548` statements, plus
  replay, schema, history, Home Assistant configuration, and end-to-end smoke
  checks.

## 0.6.0 - 2026-07-15

### Added

- Native EV smart charging, removing the requirement to install the separate EV Smart Charging integration.
- Direct charger switch or separate start/stop control, native Target SOC and Ready by entities, and manual Start charging/Stop charging buttons.
- Continuous least-cost charging windows by default, optional interval scheduling, earliest-start limits, maximum import-price filtering, low-price immediate charging, and minimum-SOC immediate recovery.
- A persistent `ha_energy_planner.set_ev_target_soc` service alongside the now-persistent ready-by service.

### Changed

- EV schedule actions now decide whether the charger must be on in the current planning interval; they no longer delegate schedule execution to an external integration.
- No-op charger decisions are audited as skipped and do not consume daily command caps or command cooldowns.
- New EV configurations expose direct charger controls only. Existing `ev_smart_charging_*` controls and helper-backed entries remain readable for migration compatibility.

### Validation

- Full Docker validation: `662 passed`, `100%` across `7,536` statements, plus replay, schema, history,
  Home Assistant configuration, and end-to-end smoke checks.

## 0.5.2 - 2026-07-12

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
