# Requirements Audit

Status as of 2026-09-05.

The integration self-assesses at Platinum against the current Home Assistant
quality-scale catalog. All integration modules are checked with strict mypy against the
pinned Home Assistant type surface, including typed `ConfigEntry.runtime_data`
use throughout; the Docker and pull-request gates enforce that result.

## Covered

- Main thermostat shutdown feedback is scoped through service dispatch and confirmation. Configured zone climates may turn off and lose target attributes without triggering manual override; user attribution, unrelated ancestry, unexpected targets/modes, auxiliary changes, unrelated zones and events after the command remain excluded. Adapter regressions exercise successful and failed restores (`tests/test_hvac_adapter.py`).

- EV and climate action limits remain configurable (defaults 10 and 12 respectively, with a 60-minute manual HVAC override; existing values preserved). The optimizer distinguishes physically feasible candidates rejected by the rolling action allowance from capacity/price shortfalls, and notifications expose the limit and remaining allowance. Regression tests cover exhausted/one-action allowances, recovery after raising the cap, physical shortfalls, and actionable notifications (`tests/test_ev_optimization.py`, `tests/test_executor.py`). Safety-stop accounting remains conservative.

- Active legacy HVAC cycles resample fresh timestamped import prices on their persisted tariff grid, avoiding false cancellation when refresh times move. Genuine price changes and source gaps still reject the cycle (`tests/test_inputs.py`). Successful acquisition retains the original main baseline separately from unresolved recovery; subsequent coast commands and a new executor preserve that baseline. Release restores main/zone target dependencies before damper closure or main shutdown, including dynamic Daikin bounds and mandatory off cleanup after persistence failures or cancellation (`tests/test_executor.py`, `tests/test_hvac_adapter.py`). Climate diagnostics distinguish model learning, legacy rejection, execution, and pending restoration with readable bounded blocker/target attributes and legacy non-mapping ownership compatibility (`tests/test_diagnostics.py`, `tests/test_planner.py`, `tests/test_sensor.py`).

- Preconditioning acquisition survives the next planning cycle when heating from at or below the lower comfort boundary or cooling from at or above the upper boundary. Opposite-boundary handoffs remain active, and scheduled coasting boundaries override stale persisted phases. Home/away, mirrored heating/cooling, manual override, missing evidence, and unsafe-input regressions are covered in `tests/test_planner.py`.

- Startup input warnings have a bounded ten-minute grace without bypassing input safety. Persistent outages warn after the deadline and preserve their original duration; new or repeated outages warn immediately even while other inputs are starting (`tests/test_coordinator.py`).
- Zone restoration retains incompatible targets without repeated device or scheduler-guard commands, releases other eligible states, and retries the original target after recovery, and accepts an already-observed baseline despite changed command bounds (`tests/test_hvac_adapter.py`). Plan health exposes unresolved durable HVAC restoration as degraded independently of input quality and handles legacy non-mapping HVAC ownership (`tests/test_sensor.py`).

- Actuator recovery metadata uses atomic Home Assistant Store writes with an
  explicit successful-write acknowledgement. Home Assistant's logged write
  failures cannot acknowledge the pending generation or permit new device
  dispatch; shutdown-deferred writes must be flushed before acknowledgement. `tests/test_control_runtime.py` injects real
  Store write failures before EV, HVAC and Enphase commands, verifies retries,
  and recovers interrupted commands using newly constructed HA/Store/executor
  instances and persisted ownership/reservations. The compatibility matrix runs
  these contracts on the minimum, pinned and stable Home Assistant versions.
  Supported compatibility images and the full-suite coverage job use combined
  execution. Test failures and interpreter crashes fail the gate
  (`tests/scripts/test_docker_compatibility.py`).
- Operator disarm revokes command authority and restores owned HVAC before
  flushing the resulting state. Real Store tests cover disk failures and
  write failures during shutdown after takeover, including partial restoration failure,
  retained durable recovery evidence, listener updates, and successful retries.
  Acquisition continues to require its explicit durable flush.
- Continuous EV windows minimize total energy cost, including fractional final
  slots and solar opportunity cost; configured carbon preferences and current
  session anchoring remain enforced. `tests/test_ev.py` checks an independent
  exhaustive cost oracle, and `tests/test_executor.py` overlaps EV transactions
  across persistence, cancellation, failed dispatch and stop confirmation.
- Observation sequences in `tests/fixtures/decision_replay/` regenerate plans
  and execute them across successive refresh times with real HA services and
  storage. They verify charging allocations, exact commands, manual stops,
  stale-input safe stops, resumed planning, and both autumn ready-by occurrences.
  These supplement the existing constraint-only supplied-plan replay fixtures.
- Custom integration scaffold, config flow, options flow, entities, services,
  diagnostics, and versioned Home Assistant `Store` persistence are present.
  The manifest classifies Energy Planner as a service integration so configured
  entries remain visible on the main Devices & services integration page rather
  than being routed to the Helpers-only experience.
  Diagnostics expose redacted entity/service mapping, input-health metadata,
  plan metadata, bounded recent outcomes, and compact
  Store summaries rather than relying on unbounded raw Store inspection.
  Store load normalizes known schema fields so missing or malformed older data
  falls back to safe list/dict defaults while preserving unknown metadata, and
  malformed persisted execution timestamps are ignored instead of raising
  through safety-gate evaluation.
- The primary user-facing status surface is limited to **Armed**, **Current
  state**, **Next actions**, and the read-only **Plan** calendar. Diagnostic
  **Decision summary**, **Plan health**, **Current load forecast**, **Planning
  duration**, and **Load forecast coverage score** sensors provide deeper
  bounded evidence without cluttering that primary surface. Armed reports
  effective command authority: a persisted arm request remains visible in attributes but
  stale or incomplete reviewed production evidence keeps the entity off with a
  stable blocking reason. Manual arming re-runs preflight and cannot cancel safe
  recovery or grant apparent authority while that evidence is invalid. Current
  state publishes actual configured entity snapshots and ownership only for
  enabled control areas; Next actions mirrors that enabled-area summary and
  excludes disabled-area actions from its bounded decision evidence and action
  count. The Plan calendar expands allocated EV slots into contiguous charging
  windows with explicit start and stop times instead of showing the planner's
  short recheck interval, groups event evidence into readable bulleted sections,
  renders embedded timestamps in Home Assistant's local timezone, and omits
  actions for device-control areas whose selector is off.
- The Energy Planner service contains a flat device list: one planner device with
  all planner entities, plus one device per tracked vehicle. Vehicle devices
  represent saved profiles; telemetry entities remain owned by their source
  integrations. Setup migrates vehicle subentries into entry data with stable
  profile IDs and removes stale vehicle devices after profile removal. Repeated
  migration and setup retain device IDs and user names
  (`tests/test_device_registry_runtime.py`, `tests/test_vehicles.py`).
- **Configure** offers Planner settings and Add/Edit/Remove vehicle actions.
  Planner settings retains the central form with collapsible input and policy
  sections. No config-subentry flows are exposed. Existing legacy input mappings
  and vehicle profiles migrate to main-entry data during setup. Ready-by-only
  updates still replan without a configuration reload or control handoff. Options
  saves also migrate pending subentries when the integration is disabled, so
  the first later setup cannot undo profile edits or resurrect removed profiles
  (`tests/test_vehicle_options_runtime.py`).
- Automatic control is the sole master intent switch. It remains on while
  startup safety temporarily disarms production command authority; Armed and
  the stable active/recovery/review Mode sensor expose actual lifecycle state. Separate Climate control,
  EV control, and Enphase control switches select the participating areas and
  appear with Automatic control in the device's Controls section. A
  device-off transition while armed restores only that asset and fails without
  changing the selector if restoration is incomplete; device-on transitions pass
  preflight before execution while unaffected controls remain armed. The former planner, dry-run, and legacy
  `*_control_enabled` switch entities remain removed. Runtime option updates
  request replanning without reloading the config entry, and Docker smoke
  coverage exercises switches and buttons through Home Assistant Core service
  calls.
- Config flow validation checks mapped entity IDs, expected domains, compatible
  units where exposed, entity availability, and configured service availability
  without issuing commands. The user must provide mapped people/entities rather
  than receiving environment-specific person defaults in production Python.
  Central settings validation enforces coherent device constraints and supported
  unique priority-weight tokens before configuration values reach the planner.
  Opportunistic charging and opt-in lowest-cost daylight charging are configured centrally;
  ready-by belongs to each tracked vehicle when profiles are enabled. Legacy EV
  mappings require SOC, charging feedback, and the authoritative target SOC entity.
  Shared-charger setup accepts connection and charging feedback before the first
  vehicle profile is added. Retired number, time, and switch entities, the duplicate
  keep-charger-on switch, the fixed-duration pause buttons, the manual EV
  start/stop buttons, and the connected helper are removed from the entity
  registry during setup. Keep charger on remains editable in EV settings and
  arbitrary pauses remain available through the pause service.
- The planner builds a 24-hour, five-minute decision context and keeps compact
  plan, forecast, bounded action, AI, ownership, override, and outcome
  records.
- Published current-state and next-actions attributes are compact, JSON-friendly,
  and bounded so enum/datetime values are serialized and nested action evidence
  cannot exceed Recorder's state-attribute limit. Full bounded dry-run,
  forecast-health, and execution evidence remains available through diagnostics.
- Forecast safety weighting is calculated from source metadata where exposed
  and uses a conservative lower weight for point-sensor fallback. The weight
  remains internal to fail-closed control gates; user-facing attributes instead
  identify the limiting source, affected entities, temporal coverage, and
  corrective action so a fixed fallback weight is not presented as a measured
  probability.
- Weather uses the official hourly `weather.get_forecasts` response action,
  normalizes naive timestamps in Home Assistant's timezone before UTC alignment,
  and caches successful responses for the shorter of 15 minutes and the planning
  interval. Manual replans force a fetch. A failed fetch reuses cache only within
  forecast freshness, then tries legacy attributes and the current point value.
  Diagnostics expose fetch/cache status, age, source, coverage, and failure;
  canonical matching and Fahrenheit-to-Celsius conversion remain supported.
- Safety defaults are fail-closed: execution disabled, dry-run enabled, stale
  required inputs unsafe, non-finite numeric inputs rejected, and due actions
  revalidated before execution.
- Configurable grid import/export kW limits are represented as options and
  validated as hard constraints against normalized PV/load plus projected
  EV/HVAC flexible load.
- EV momentary command endpoints treat a service exception as an ambiguous
  outcome when charging feedback is available. The adapter waits through the
  configured confirmation window and accepts delayed matching feedback before
  attempting rollback, preventing an accepted Start command from being followed
  immediately by Stop solely because the provider response timed out.
- Native EV charger, Daikin HVAC, and Enphase profile adapters execute through
  mapped Home Assistant entities/services and support restore where configured.
- Native EV execution optionally confirms mapped charging-state feedback after
  start and stop commands. Confirmation is bounded by configurable timeout and
  retry limits; unavailable feedback and exhausted retries trigger an immediate
  compensating restore or safe-stop, preserve ownership if compensation fails,
  and are covered by adapter and executor tests. Manual commands persist the
  same outcome evidence and engage the EV control pause after device or
  confirmation failures. Subsequent starts honour that pause and the command
  cooldown, while manual and scheduled stops bypass failure backoff, command
  cooldowns, and daily caps for recovery. Safe-state ownership records the
  actual commanded entity and its complete EV actuator topology so momentary
  takeovers cannot be mistaken for a restorable unrelated persistent control.
  Restore, automated safety-stop, and manual-stop paths use that persisted
  topology after EV mapping changes rather than clearing ownership through a
  replacement actuator. Command
  acceptance is tracked separately from proven-safe stop confirmation. A
  separate stop command helper cannot release safety ownership by itself;
  meaningful inactive charging feedback together with a confirmed-off
  persistent charger control, a stateful control, or rollback must prove the
  safe state. Failed automatic safety stops back off for ten minutes and stop
  after three attempts per rolling day. Dedicated command metadata preserves
  the backoff and retry block independently of shared pause updates and bounded
  audit retention. A compensating stop
  that does prove the safe state is recorded as a successful recovery and
  clears ownership and household capacity. Manual compensation follows the same
  success contract and creates the normal stop override, while unconfirmed owned
  stops retain ownership and capacity for recovery.
- Manual EV commands, scheduled execution, and explicit restoration are
  serialized by the coordinator. Regular planner-owned schedule stops use the
  same proven-safe release contract as synthetic safety stops, and unowned stop
  commands cannot create a restorable takeover baseline.
- Mapped EV charging feedback is an immediate coordinator input. While the
  master control is armed and EV control is enabled, a newly observed charging
  transition without a bounded in-flight planner-start expectation is stopped
  through the normal confirmed and audited EV command path. The same
  compensation runs when either control is enabled while charging is already
  active, covering chargers that default to immediate charging on plug-in. A
  failed or unconfirmed compensation remains pending and retries every 30
  seconds, including through temporarily unknown or unavailable charging
  feedback, until inactive charging feedback is observed or control is disabled.
  Planner and manual starts establish that expectation immediately before the
  service boundary and retain it only when a start command may have taken effect,
  so their own feedback cannot be mistaken for an external auto-start while
  rejected or no-op starts cannot mask a later plug-in event.
- EV target SOC comes only from the required mapped vehicle sensor. Missing,
  unavailable, nonnumeric, or out-of-range target evidence blocks EV planning
  instead of substituting a planner-derived target. The live vehicle value is
  used directly and must remain within the physical 0% to 100% range. Stop-only
  schedules remain valid when current SOC already exceeds that target. The
  connected-state entity remains optional because charging feedback independently
  confirms commanded power delivery.
- The optional preconditioning policy keeps the charger control enabled after
  target SOC is reached while preserving manual-stop and execution safety-gate
  precedence. Only the actual after-target preconditioning action selects the
  control-state confirmation path, that path requires the persistent direct
  charger control rather than an optional start command, and the current slot
  reserves the full configured charger rate for grid and battery evaluation.
  EV settings validation and preflight reject keep-on without that persistent
  switch/input-boolean control.
  Keep-on also requires the authoritative target to be available and remain
  within configured SOC policy bounds, preventing an external vehicle target
  from bypassing the hard planner maximum.
- EV safety stops use direction-specific validation: unrelated unhealthy plan
  inputs and unavailable start controls cannot block an available stop path.
  When unhealthy inputs, an observed disconnect, disabled EV control, or a hard
  grid-import violation leave EV power planner-owned or reserved, execution
  prioritizes one audited safety-stop attempt for that plan, retains its
  reservation after an uncertain outcome, and clears ownership only after
  success. Existing single- and cross-entry reservations are also re-evaluated
  against the strictest current household limit. A shared atomic shedding claim
  selects one loaded over-limit reservation for the confirmed safety-stop path,
  while other entries retain their reservations until that claim releases. An
  unloaded claimant relinquishes the claim without releasing its uncertain
  capacity, allowing a loaded EV to shed controllable load instead of either
  leaving a running charger behind a rejected continuation action or stopping
  every EV during concurrent evaluations.
- A failed loaded shedding claimant also releases only the atomic claim while
  retaining its uncertain capacity, allowing another loaded charger to attempt
  a confirmed stop. Continuous-charging allocation never returns a fragmented
  fallback; a contiguous partial window is explicitly marked infeasible. An
  explicitly forced current slot is the bounded exception: enabled below-threshold
  opportunistic charging may claim the current slot before the configured earliest
  start, while any remaining continuous window
  stays within the configured hours. Once charging feedback confirms an active
  continuous session, replanning may retain its current pre-window slot so
  forecast repricing cannot fragment it; the configured maximum import price
  remains authoritative. Daylight preference is evaluated from deterministic sunrise/sunset windows
  calculated from Home Assistant's configured location. It requires complete
  effective-cost evidence through sunset; continuous schedules fall back as a
  whole when daylight capacity is insufficient, while split schedules allocate
  daylight first and de-duplicate ready-by fallback slots. Completed Recorder
  charging sessions of at least 30 minutes calibrate effective SOC gained per
  kWh from SOC gain, configured charger power, and active duration. At least 60
  minutes and 3% gain are required; the learned rate carries a 10% conservative
  margin and the configured 2% estimate is used only while history is insufficient.
- Multiple chargers are supported as separate named config entries. Entry-scoped
  storage isolates plans, history, production state, pauses, and audit records;
  services require `config_entry_id` when multiple runtimes are loaded. Each
  planner remains independent for cost optimization, while active EV commands
  share an atomic in-memory household grid-capacity reservation. Reservations
  are held through uncertain stop/rollback outcomes and released only after a
  confirmed stop or safe-state restoration. Observed disconnection triggers a
  confirmed stop instead of optimistically releasing capacity, preventing both
  reconnection races and two entries spending the same projected grid headroom.
  Manual starts use the latest committed decision projection, include their own
  charger load when it is not already represented, fail closed when that
  projection is unavailable, older than the planning interval, or unsafe, and
  participate in the same reservation lifecycle.
  Successful and uncertain manual starts preserve the original charger state
  for later safe-state restoration. Unavailable configured connection evidence
  blocks a start, and uncertain reservations remain protected across config-entry
  unload. The active reservation high-watermark is persisted independently of
  ownership and rehydrated before entry execution after restart; explicit
  releases are persisted as inactive. The provisional reservation and actuator
  topology are committed before the start service call, and reservation-only
  recovery requires a confirmed stop on that topology before another start.
  Each reservation stores its owner's
  configured import limit. Runtime option updates apply import-limit changes and
  reservation increases immediately, while an active reservation cannot shrink
  during later start/no-op actions or across an unclean restart and remains
  conservative until a confirmed stop. Cross-entry config validation prevents native or legacy EV
  charger controls, the same Daikin climate control, climate automation,
  climate zone, writable manual-override helper, or Enphase profile actuator
  from having two planners.
- Legacy global storage is eligible only for the first upgraded unnamed entry
  and is marked consumed after the entry-scoped store exists. Stable persistent
  notification IDs are suffixed with the config-entry ID so one planner cannot
  overwrite or dismiss another planner's alert.
- Manual Daikin or planner-owned zone changes create a temporary override,
  persist across restart, and release only HVAC ownership. An externally
  enabled manual-override helper uses the same configured timeout; legacy
  indefinite helper state is bounded during startup, and helper feedback from
  integration-created timed overrides is guarded.
- Automatic notifications are limited to conditions that normally require user
  action: broken required mappings/capabilities, failed safe-state restoration,
  infeasible EV readiness, and configured grid hard-limit conflicts. Explicitly
  pressing **Run safety check** or **Explain** always returns a notification,
  including successful safety checks. Successful restore operations, routine
  changes, and stale data remain silent. Stable IDs and content signatures
  deduplicate repeated alerts; the actionable plan-alert group can be disabled
  without changing plan health or fail-closed execution. User-provided reason
  fields are compacted and redacted before they can be shown. Persistent
  notification creation is queued until Home Assistant reaches its running
  state, after every integration has had a chance to load. Each stable ID keeps
  only its latest queued alert, and recovery dismissals cancel that alert before
  startup completes.
- Discovery records non-commanding capability evidence for EV, Daikin,
  Enphase, and the local AI service before active control is allowed.
- AI explanation and troubleshooting is on-demand, minimized, structured, and
  whitelisted. Automatic background calls and the AI Enabled entity are removed.
  The primary **Explain** button accepts only **No action
  needed** or one complete action anchored to a current planner issue/rejection
  target, including the affected configured entity or setting, problem, exact
  next step, expected benefit, and verification. Generic tuning suggestions are
  rejected. Expected no-action decisions, including the absence of a tariff
  window that is both worthwhile and thermally feasible for climate shifting,
  cannot be promoted to settings faults without specific input evidence. AI
  cannot call services, change settings, or bypass hard constraints. The result
  and pending state are published in **Next actions**
  attributes instead of a separate status entity. Home Assistant's AI Task
  structured-output schema is supplied to the provider. Pressing the button
  immediately publishes pending feedback, then replaces it with the accepted,
  rejected, or failed result; rejected results also remain visible across
  equivalent plan refreshes. The integration warns that provider
  integrations may independently log bounded prompts. Docker smoke coverage
  exercises a response-capable local AI advisor service through Home Assistant
  Core and verifies accepted bounded advice in Store recommendations.
- Replay fixtures cover stale inputs, battery floor rejection, EV infeasible
  ready-by evidence, negative-price EV scheduling, HVAC occupancy/manual
  override rules, bounded helper overrides, opt-in away preconditioning, and
  Enphase holds.
- Executable live-schema fixtures cover representative Amber price, external
  PV, and optional weather payload shapes through `scripts/validate-live-schema-fixture.py`,
  so sanitized real Home Assistant exports can be validated outside pytest.
  `scripts/export-real-live-schema.sh` wraps the required real export set, and
  `scripts/export-live-schema-fixture.py` can export individual Home Assistant
  state/service payloads into that fixture format using operator-supplied URL
  and token values, with built-in and operator-specified key redaction plus an
  optional pre-write parser validation gate. The validator also has a
  `ha-energy-planner-v1-real` profile that reports missing required real-export
  fixture names, mismatched fixture kind/value-kind metadata, and missing
  exported source entity metadata before full live-schema completion is
  claimed.
- Executable real-history fixtures cover Recorder-style EV charge calibration,
  Daikin thermal-model replay, and rolling-origin external-PV forecast accuracy
  through `scripts/validate-real-history-fixture.py`. Forecast evidence is
  matched by issue/valid time, reports MAE and RMSE for near/day/long lead-time
  buckets, and must outperform a no-lookahead persistence baseline.
  `scripts/export-real-history-fixtures.sh` wraps sanitized Home Assistant
  history export for the required `real_ev_charge_calibration`,
  `real_daikin_thermal_history` and `real_pv_forecast_accuracy` fixtures, and the
  `ha-energy-planner-history-v1-real` profile verifies exported source entity
  metadata before real-history completion is claimed.
- Forecast parsing retains uncovered horizon slots as missing values and never
  extrapolates the final bucket. Per-input evidence reports first/last
  timestamps, total and continuous coverage, and leading/internal/trailing
  gaps. Continuous coverage is healthy at 12 hours, degraded from 8 to under
  12 hours, and unsafe below 8 hours; thresholds are capped by deliberately
  shorter configured horizons. Degraded inputs remain action-ineligible under
  the planner's existing healthy-input action gate.
- Solcast `pv_estimate`, `pv_estimate10`, and `pv_estimate90` interval fields retain their kW semantics independently of the daily sensor's kWh unit. Explicit interval units and inherited power units take precedence, and generic energy buckets still convert by duration. Calibration model version 4 and versioned training snapshots prevent pre-correction samples from biasing corrected forecasts; unversioned snapshots cannot repopulate the reset model. `tests/test_forecast_calibration.py` covers upgrade invalidation and fresh retraining. `tests/test_forecasts.py` exercises a sanitised live-shaped fixture in `tests/fixtures/solcast_daily_power.json`.
- A second optional PV entity supports timestamp-safe Solcast Today/Tomorrow
  stitching across midnight and daylight-saving changes. Secondary series must
  expose timezone-aware timestamps; untimestamped and naive timestamps are
  diagnosed and rejected, and secondary slots are excluded from calibration
  until per-slot issue-time provenance is retained. Required Amber and PV point
  values cannot satisfy forecast coverage. Household load uses the built-in
  Recorder model and never accepts a legacy forecast entity as measured input.
- `scripts/export-real-validation-bundle.sh` runs both real export wrappers and
  enforces the dependency-free core live-schema and Recorder-history profiles.
  Its `--validate-only` mode checks already exported
  `real_*.json` fixtures without calling Home Assistant again.
- Docker Home Assistant validation is available through
  `scripts/docker-validate.sh`, `scripts/docker-ha-smoke.sh`, and
  `docker compose`. The full validation gate runs compile and Ruff checks, Dockerized
  pytest, replay fixtures, live-schema validation, real-history validation, Home Assistant
  `check_config`, and the smoke test in one repeatable sequence. The smoke harness
  coalesces queued coordinator refreshes, waits for background execution, and
  stops Home Assistant on an explicit completion marker instead of consuming its
  full failure timeout. Pull-request CI classifies changed paths with
  `scripts/select_ci_checks.py`, runs only the affected expensive jobs, and
  fails safe to the full CI set for an unclassified trigger path.
  Behavioral changes also run real runtime contracts and smoke tests on the HACS
  minimum/pinned HA 2026.9.0 and current stable. Documentation-only
  changes retain scoped quality checks. `scripts/docker-validate.sh` remains the
  complete local gate including minimum/pinned runtime contracts and package smoke.
  Coverage instruments branches, enforces exactly 100% statement coverage with
  `scripts/check_coverage.py`, and rejects per-module branch regressions against the reviewed baseline. The smoke test
  now verifies coordinator refresh, entity
  registry entries, the Armed/Mode/Current state/Next actions/Plan calendar surface,
  Automatic control and its three device selectors, device registry registration, persisted active plan, discovery storage,
  forecast snapshot training slots sourced from Home Assistant PV template
  attributes and the persisted built-in household-load model, Amber cent/kWh forecast attributes reflected in compact plan
  previews, weather camelCase forecast attributes reflected in compact plan previews,
  PV forecast calibration updated from a time-aligned observed-power entity,
  built-in load health/evidence stored without raw history, HVAC thermal-model state updated from Home Assistant climate and
  power entity samples, Recorder import metadata, and compact EV charge-rate
  calibration state. It also verifies
  bounded forecast-snapshot action metadata for the active EV schedule with
  runtime ready-by override, an active EV schedule allocated to a negative
  import-price slot, and an Enphase arbitrage action backed by deterministic
  forecast evidence. It also verifies
  real HA service invocation for manual HVAC override plus restore-safe-state,
  active-mode occupied HVAC preconditioning before an expensive period with
  automation suppression and restoration, an active-mode occupied
  expensive-period HVAC automation suppression and restoration path, and an
  active-mode HVAC away-off execution path against a Home Assistant
  `generic_thermostat`. It also runs the `set_ev_ready_by`
  service through Home Assistant Core and verifies the normalized runtime value
  updates Energy Planner's native ready-by setting during active scheduling, a
  direct active-mode EV charger execution path against local Home Assistant
  controls, an active-mode Enphase arbitrage profile takeover against a local
  `input_select`, an active-mode Enphase restore-to-AI profile action when
  arbitrage value drops below threshold, active-mode Enphase command-cooldown
  rejection for a repeated arbitrage opportunity, final safe-state restoration,
  replan and restore execution through Home Assistant Core, confirmation that a
  successful restore does not emit a persistent notification,
  migration away from the enabled/dry-run/AI-enabled switches, an on-demand
  no-action AI explanation from a response-capable Home Assistant service, and
  verifies the
  `export_diagnostics` response payload plus token/address redaction through
  an HA automation `response_variable`.
  Restore-safe-state validation includes live EV helper restoration, mapped HVAC
  automation restoration, and mapped Enphase profile restoration to the
  configured AI profile. Active-mode price/control coverage also asserts an
  ordered multi-cycle Enphase sequence: low-value restore to AI, high-value
  arbitrage takeover, restore, a second high-value takeover, second restore,
  and command-cooldown rejection for a repeated arbitrage opportunity.
- Forecast normalization parses common forecast/list attributes, nested
  prediction wrappers, timestamp-keyed forecast maps, canonical camelCase key
  variants, item-level units, and state-level units for Amber import/export,
  external PV entities and measured household load, including cent-to-dollar
  price and W/kW/MW power normalization, plus weather forecast temperature attributes, with
  point-sensor fallback. Optional point-sensor power inputs used for forecast
  calibration and the HVAC thermal model use the same W/kW/MW normalization.
  Representative integration-specific Amber, external PV, and weather schemas are covered by
  executable live-schema fixtures and the real-export validator profiles.
- Compact external-PV forecast calibration is implemented. It records due PV
  forecasts only when a separately configured measured-power observation is
  timestamp-aligned, deduplicates forecast targets and lead-time buckets, and
  retains a bounded sample window with diverse forecast horizons. Independent
  robust bounded factors are trained per 30-minute lead-time bucket and enabled
  only when that bucket improves a later holdout set spanning enough distinct
  observations and time; near-term evidence cannot alter day-ahead slots.
  Forecast entities are never used as
  actuals, overdue slots are not paired to a current reading, and non-finite
  persisted factors and sample values are ignored. Obsolete external-load
  calibration is discarded.
- Built-in household-load forecasting is implemented in
  `custom_components/ha_energy_planner/load_forecast.py` and trained through
  `recorder_import.py`. Up to 28 days of Recorder state changes are converted
  into time-weighted 15-minute local buckets. Known EV-charging intervals are
  excluded, aligned HVAC power is subtracted and clamped at zero, and unknown
  cleaning intervals are not admitted as clean observations. Robust clock-time
  medians are blended with weekday/weekend evidence using `n / (n + 3)`.
  Expected and conservative profiles require three observations per bucket;
  only gaps of at most 30 minutes are interpolated. A bounded recent-load
  correction fades to neutral over two hours, and UTC planning slots are mapped
  through timezone-aware local timestamps for DST folds and gaps.
- A configurable `household_load_outage_grace_minutes` (default 10, range
  0–30) permits only a known `unknown`/`unavailable` transition to use a ready,
  quality-approved, current, complete model that still matches the mapped
  entity and timezone. The degraded path omits current-load correction, uses
  the conservative upper series, caps load confidence at 0.65, and records
  outage age, grace, model age, and correction state. Missing/non-numeric
  entities, model mismatch, stale or incomplete models, and elapsed grace fail
  closed. A persisted continuous-outage timestamp survives sentinel changes,
  non-numeric interludes, reloads, and restarts. Missing or non-numeric evidence
  makes the continuous outage ineligible for fallback until numeric recovery.
  The option participates in production-evidence fingerprinting.
- Load-fallback evidence includes `fallback_status`, `fallback_reason`, a plain
  explanation, and remaining grace seconds in Current load forecast, the
  existing plan presentation, and exported diagnostics. Tests distinguish
  bridging, expired/disabled grace, missing/non-numeric inputs, invalid outage
  timing, ineligible continuous outages, and unready/incomplete models without
  weakening the 10-minute default or model-quality gate.
- PV freshness uses the planning parser's final-interval coverage and the same
  timestamp validation used to admit tomorrow's forecast. Regression cases
  cover 23:30, the exclusive midnight endpoint, valid tomorrow coverage, absent
  or expired tomorrow data, naive timestamps, invalid values, and leading gaps.
  Existing continuous-horizon checks continue to reject incomplete plans.
  Freshness interval checks require a parsed timestamped value; regression
  tests reject old numeric arrays mixed with invalid dated records in both
  primary and secondary sources, while fresh ordered sources remain supported.
- Availability logging resolves known codes to bounded configured entity IDs,
  reports per-input recovery duration, and suppresses unchanged outages.
  Stable input identities group multiple reasons and discovery aliases,
  preserving the first-loss time through stale/unavailable/non-numeric
  transitions and partial issue resolution. Tests verify independent input
  recovery and genuine entity remapping.
  Arbitrary issue text and invalid entity values never enter logs. Evidence:
  `availability.py`, `coordinator.py`, `tests/test_availability.py`, and the
  coordinator transition/recovery tests.
- Conservative-bound calibration treats each local day as one dependent block:
  it computes a finite-sample 90% positive-residual score per day and applies a
  conservative 95% finite-sample upper quantile across those day scores. This
  prevents correlated 15-minute samples from overstating independent evidence
  while preserving the separate 90% leakage-free holdout coverage gate.
- The default-off `bypass_safety_gates` option explicitly waives that coverage
  failure, all production preflight checks, and dry-run evidence/fingerprint
  checks. Automatic control still must be selected and armed, and runtime
  service errors and feedback confirmation remain observable. The
  `load_forecast_coverage_score` diagnostic sensor exposes the latest evaluated
  percentage (including a failed retraining score when the prior safe model is
  retained), its evaluation time, the active-model score,
  required threshold, model status, and whether the combined bypass is active.
  The complementary `current_load_forecast` diagnostic sensor exposes the
  expected load used for the current interval, its conservative upper bound,
  horizon coverage, correction state, model age, and source/fallback health.
- Readiness requires three training days with at least 80% valid buckets per
  day, 80% valid historical coverage, two
  leakage-free holdout origins with at least 144 aligned samples, MAE no more
  than 10% worse than previous-day persistence, and at least 90% conservative
  coverage. Bounded gaps from excluded EV charging, historical invalid power,
  or brief source outages no longer discard an otherwise usable day.
  Validation-only profiles may use the one or two earlier training days needed
  to score those first two origins, while production profiles still
  require three clean observations per clock bucket. Preceding days below the
  per-day coverage gate may provide aligned previous-day persistence samples
  but are never admitted to the production profile. Ready models are healthy
  through 24 hours, degraded
  through 72 hours, and stale afterward. Learning and initial quality-gate
  failures remain silent; missing mapping, unavailable Recorder after a model
  becomes unsafe, history exceeding the bounded query limit, and a model that
  remains unusable for 72 hours are actionable. Training occurs at startup,
  after source changes, and no more than every six hours, with failed-attempt
  backoff. Recorder reads
  use adaptive UTC-aligned chunks with a per-entity state limit, beginning at
  seven days and narrowing to one day when necessary, and compact each chunk
  before continuing. Only aggregate profiles, validation metrics, source
  and contract identity, and timestamps are persisted.
- A mapped load entity that has not yet been restored during Home Assistant
  startup leaves the persisted model and training cadence unchanged. The next
  coordinator refresh after the source appears can therefore train immediately
  while planning remains fail-closed during the transient absence.
- EV charging calibration retrains from the latest 30 days of Recorder history
  no more than daily. Failed or insufficient retraining retains the last ready
  model, while entity or configured charger-power changes force retraining.
- Config-entry version 2 migrates only the legacy measured-load mapping to
  `household_load_entity`; a legacy forecast-only mapping is removed and active
  control remains fail-closed until a real measured sensor is selected. The
  production evidence fingerprint includes the measured mapping and built-in
  forecast contract version, but excludes routine model values and retraining
  timestamps. A mismatch restores safe state and explicitly disarms production
  control. When startup finds the mismatch on a previously armed installation
  whose automatic-control intent is still active, it persists a restart-safe
  handoff and requires three fresh non-commanding validation plans plus final
  active-plan verification before re-arming; otherwise new dry-run review cycles
  remain required. Explicit operator arming also
  requires current preflight and matching evidence rather than merely setting
  the persisted armed flag. Relevant evidence is exposed through
  the existing Current state, Next actions, calendar, diagnostics, and support
  surfaces without adding an entity. Tests cover normalization, cleaning,
  quality gates, correction,
  true interval aggregation, DST, age/source/corruption recovery, migration,
  Recorder failure and cardinality bounds, persistence, delayed notifications,
  conservative grid-limit use, action-time evidence, and deterministic
  operation with the built-in aggregate model.
- Runtime calibration snapshots retain dense near-term targets plus bounded,
  stratified targets through the complete planning horizon. Enabled lead-time
  models expose p10/p90-style bounded factors; conservative flexibility and
  battery calculations use lower PV and upper load while financial estimates
  retain the holdout-validated expected factor.
- Optional grid carbon-intensity series are normalized to gCO2/kWh. Carbon has
  a non-zero action score when the forecast varies, and EV allocation blends
  normalized effective cost with grid emissions according to configured
  priority order while accounting for conservative solar displacement.
- EV energy demand uses the live vehicle target and a persisted compact
  charge-rate calibration. Recorder history reads use Recorder's database
  executor when available and fall back to Home Assistant's generic executor
  only when Recorder is absent. The 30-day calibration import is split into
  adaptive chunks beginning at seven days; each entity query has an explicit
  50,000-state limit, dense chunks narrow to one hour, and the proven sub-day
  span is reused for later chunks. Only charging transitions plus the SOC values
  required around them cross back to the event loop, and a 20,000-row compacted
  history cap fails closed with a stable reason instead of allowing query or
  memory growth. Calibration accepts common charger and connector-status states
  plus SOC strings with percent units or comma decimals; only bounded session
  samples and aggregate model values are kept in `Store`.
  Learned rates are accepted only when the stored charging entity, SOC entity,
  and configured charger power match the current EV configuration; otherwise
  planning uses the conservative bootstrap rate while retraining.
  Configured charging feedback also accepts connector-status sensors;
  `SUSPENDED_EV` and `SUSPENDED_EVSE` are normalized as connected but not
  actively charging, so momentary stop controls are not called after a vehicle
  has already suspended power delivery. Disconnection remains insufficient
  evidence for a safe momentary stop.
  Persisted Recorder calibration timestamps tolerate malformed or timezone-naive
  older values without raising through planner refresh. Docker smoke coverage
  validates the compact calibration lifecycle, and real-history replay fixtures
  validate charging-state and SOC formats outside the running HA smoke container.
- HVAC active planning is conservative: away mode off is preserved outside a
  persisted `precondition -> pre_peak_coast -> peak_coast -> release` tariff lifecycle. Every
  valid tariff slot in the configured 1-48 hour horizon is scanned, with the
  current 12-hour default unchanged. Relative pre-window baselines, both price
  deltas persisted at acquisition, contiguous peak boundaries, weather-led heat/cool selection, exact
  high/low comfort targets, least-cost thermally feasible runs, comfort coast,
  conservative maintenance load, and tariff-change fail-safe release are
  implemented. A missed preferred start can use the remaining contiguous
  lower-price slots before the peak; catch-up cannot cross a tariff gap or run
  beyond the applicable comfort target. Takeover snapshots configured switch/input-boolean zones,
  disables mapped automations, enables those zones, explicitly turns on the main climate
  entity, applies and confirms its target on the main thermostat first, and, when configured-zone
  synchronisation is enabled, then applies and confirms the same target on configured zone climate
  entities. Disabled synchronisation leaves zone climate targets unchanged while retaining
  switch/helper takeover. Target mutations require complete restorable main and synchronized-zone
  snapshots before takeover. The default-off option rejects configurations without a zone climate
  target, and failed acquisition restores the captured main mode and target before reporting
  rollback success. Options-aware discovery always validates the finite main
  rollback target and additionally validates configured-zone targets when
  synchronisation is enabled. Off zones with entirely absent target attributes are
  deferred for that takeover, including provisional ownership persistence, retries,
  confirmation, and rollback; the planner never invents a rollback temperature.
  Main control, switch/helper takeover, and other zones remain eligible.
  `zone_targets_deferred` exposes this condition, and the next takeover reevaluates
  recovered zones. Active, unavailable, or malformed-target zones retain safety checks.
  Tests in `tests/test_discovery.py` and `tests/test_hvac_adapter.py` cover recovery,
  frozen exclusions, failed-command rollback, and target recovery during main-unit or zone-switch activation. Fresh-context recovery is accepted only for deferred configured zones during the explicit main turn-on phase or an unambiguous zone-switch call, with no user/parent attribution and finite restored targets. Main-unit turn-on may restore the remembered mode before the requested mode is applied; zone-switch feedback must still match the requested mode. Deferred zones may then follow the explicit main-unit mode command during its service/confirmation window, with no user or unrelated parent attribution, no auxiliary-setting changes, and only finite recovered targets. Regression coverage reproduces off/null-target to heat/restored-target feedback without aborting the adapter transaction, including a cooling request that first restores remembered heating; attributed changes, unrelated zones, inactive modes, active-zone target changes, auxiliary settings, invalid targets, and feedback outside startup retain manual-override handling. It publishes affected entity IDs in Current state
  and Next actions, hard-suppresses new HVAC takeover candidates while keeping
  releases eligible, and creates one recovery-aware notification.
  Execution repeats the check immediately before adapter construction so the
  race path cannot acquire ownership, start the scheduler guard, mutate a
  device, or create a failure pause; adapter checks remain the final boundary.
  For an originally-off main thermostat, the active mode revealed by turn-on is
  persisted before planner mode selection, restored before returning to off, and retained as
  unresolved ownership if it cannot be recovered. Earlier mode or target restoration failures do
  not skip the independently attempted and confirmed off cleanup. Unresolved main state is
  persisted for release and safe-state retries and blocks new acquisition until recovered, but a
  manual main-thermostat change durably supersedes that snapshot
  before release actuators run and is preserved across restart recovery. Pending
  transaction feedback suppresses only matching scalar/range targets or expected
  intermediate mode transitions; off-to-active feedback is expected only during
  the adapter's explicit turn-on phase. A different manual target or unexpected
  mode bypasses the scheduler guard, synchronously aborts acquisition rollback,
  release, or safe-state main restoration, preserves the user's main state, and
  restores subordinate ownership. Pending zone feedback is matched to the exact
  action or rollback target. A configured switch/helper's coupled climate entity
  may publish only the corresponding off-to-active or active-to-off transition
  with the actuator call's Home Assistant context while that call and
  confirmation are explicitly phased. A context-free sibling refresh requires
  an unambiguous actuator/climate entity-ID pair, except for the bounded deferred-zone main-startup recovery described above; unrelated zone, user-context,
  target, and auxiliary-control changes still supersede the transaction. A different user
  target synchronously supersedes and durably removes only that zone's baseline
  before the remaining rollback actuators run. Await-to-actuator boundaries recheck supersession after main
  snapshot persistence, mode confirmation, and automation disable. It preserves
  the original snapshot across peak transitions. Release
  restores zones, re-enables only automations that were active before takeover,
  retains unresolved ownership for
  retry. Ordinary lifecycle release never restores the prior climate mode or
  setpoint; unresolved acquisition recovery restores its persisted main snapshot
  unless a manual main change supersedes it. An ownership-free
  release is a no-op, and only actual `set_hvac` attempts consume the daily
  climate command allowance. Comfort-boundary
  release of planner-owned control is held through the recorded peak end to
  prevent reacquisition. An unowned comfort-boundary state still scans future
  tariff evidence so a pre-peak takeover is not lost while existing automations
  remain responsible. Planned action attributes use explicit Home Assistant-local
  dates and times. A compact
  HVAC thermal model records current indoor temperature, optional Daikin power,
  and optional weather temperature samples. Version 2 requires samples at least
  five minutes apart, ignores sensor deltas below effective precision, excludes
  HVAC start/stop/mode transitions, requires explicit stable heat/cool mode and
  power evidence for active learning plus explicit off/idle evidence for passive
  learning, rejects implausible rates instead of
  clamping them, and derives medians from bounded rolling windows. Legacy
  unbounded statistics are reset before a new anchor is accepted. It also
  tolerates timezone-naive timestamps and comma-decimal strings and ignores
  non-finite values. Refreshes inside the five-minute minimum preserve the last
  eligible anchor; stable observations therefore mature during refresh storms.
  Mode/power transitions and invalid or over-two-hour gaps advance or reset the
  anchor without changing the version-2 schema or persisted statistics. Planner
  tests cover replayed cold/heating and
  warm/cooling thermal samples feeding preconditioning projections. Docker
  smoke coverage validates one active HVAC power sample from Home Assistant
  climate/power entities plus occupied preconditioning, expensive-period
  automation suppression, and restoration through Home Assistant services.
  Planner, adapter, coordinator, executor, configuration, discovery, service,
  replay, and diagnostic tests cover full-horizon detection, cold/hot target
  selection, catch-up starts, zone takeover/rollback, helper overrides, owned comfort handoff, peak
  continuation, production/pause-blocked safety release, persisted tariff
  thresholds, and release evidence.
  Installations with an external schedule-versus-manual classifier can map its
  scheduler-change boolean and timer as a required pair. The adapter starts and
  confirms both before takeover, zone/climate commands, compensation, or
  release; incomplete, unavailable, or unconfirmed guard helpers fail closed so
  planner commands cannot engage the authoritative manual-override path.
- Enphase execution, verification, hold, minimum-savings gates, and profile
  action generation are implemented. The planner can set a configured
  arbitrage profile when deterministic forecast solar-export value exceeds the
  threshold and restore the configured AI profile when takeover is no longer
  justified.
- HVAC automation/zone takeover and Enphase profile commands use bounded,
  transactional compensation. Any automation, zone, or saved profile that
  cannot be restored remains in ownership for a later refresh or
  restore-safe-state retry.
- Only an explicit allowlist of decision inputs can request replanning. AI
  result, integration-owned control, climate automation, and high-frequency
  observed power entities cannot create feedback loops; observation-only
  values are sampled on planning boundaries. Material changes are debounced,
  constrained by a one-minute non-manual refresh floor, and coalesced. A stable
  decision-input fingerprint skips planning, execution, snapshots, and
  persistence when no material input changed, while explicit manual replans
  always force a fresh computation.
- Coordinator startup schedules recurring wall-clock planning-interval boundary
  refreshes on the same epoch cadence used by decision fingerprints, without
  also registering a fixed `DataUpdateCoordinator` poll, in addition to material-
  change replans. Non-hour-divisor intervals therefore neither drift nor share a
  stale fingerprint bucket. Planner cost previews use the configured planning
  interval rather than assuming a fixed slot duration.
- A previously requested-active and armed installation preserves production
  arming, device ownership, and EV reservation on startup. Home Assistant's
  `async_at_started` boundary begins a fresh ten-minute grace at Core `RUNNING`;
  no startup-only gate suppresses ordinary planning or execution, while all
  normal runtime safety gates remain authoritative. The deadline uses one
  awaited `async_refresh()` and a complete preflight. A healthy result remains
  armed silently. An unsafe or failed result disarms before best-effort restore,
  retains automatic intent, shows **Recovery** in Mode after Home Assistant has
  started, notifies once, and persists `waiting_for_safe`.
  Recovery then runs non-debounced checks every 30 seconds indefinitely and
  requires three consecutive healthy committed plans before safe-state
  reconciliation, evidence refresh, re-arm, active refresh, and final readiness
  verification. Whole-system shutdown preserves control state; runtime unload,
  operator disable, explicit safety-gate arm or disarm, pause, and configuration
  changes retain precedence. A temporary all-control pause encountered during
  setup or reload disarms and restores safe state but persists a restart-resumable
  recovery handoff, so recovery waits for the pause to clear before re-arming.
  Terminal operator cancellation dismisses the
  superseded recovery warning. A failed configuration-reload platform unload
  resumes the disarmed recovery lifecycle on the still-loaded coordinator. A
  restarted disarmed recovery resumes with its counter reset.
  Configuration callbacks serialize preparation and reload per coordinator,
  then recheck the replacement runtime and latest topology. Separate data and
  options writes from one settings save cannot queue a second unload that
  cancels recovery. A genuine mapping change arriving during reload is still
  applied with a fresh handoff. Real Home Assistant entry/platform regression
  coverage in `tests/test_upgrade_runtime.py` verifies that recovery survives
  separate data/options notifications; `tests/test_lifecycle.py` also covers
  concurrent callbacks, later topology changes, and callbacks after unload.
  A production-evidence mismatch found while reconciling a previously armed
  startup follows the same disarmed recovery lifecycle, so migrations cannot
  leave automatic-control intent stranded without a background recovery task.
  Recovery is registered as Home Assistant background work so its grace and
  validation waits cannot hold bootstrap open.
- EV ready-by wall times are resolved in Home Assistant's configured timezone,
  normalized to UTC, and handle next-day rollover, DST folds, and nonexistent
  local times. HVAC suppression and precondition projection windows compare
  timestamps, so their duration is independent of planning interval.
- Planner contexts retain per-source forecast confidence. The plan-wide
  production score is capped by required tariff, PV, and household-load
  sources, while tariff, solar, load, climate, EV, and Enphase action gates use
  only their relevant source and device evidence. A low optional carbon source
  therefore cannot suppress climate control, but low weather confidence still
  blocks climate preconditioning at its configured threshold. Degraded input
  issues, capability failures, and scoped pauses are isolated to their control
  area, including startup reconciliation and command-authority reporting;
  occupancy availability is scoped to climate. Unsafe or unrecognized shared
  planning health remains a global fail-closed boundary.
  Heating below the comfort range and cooling above it are directionally safe
  takeover operations when their targets remain within configured bounds. When an opted-in
  away preconditioning period begins later, the plan includes both an immediate
  away-off command and a future lifecycle when the latter remains feasible
  after the resulting minimum rest period. If the lifecycle is already due, it
  can start directly without first creating an away-off rest period. Existing away-off ownership is
  retained when no candidate qualifies. Away HVAC-on execution additionally
  requires complete timestamped, numeric, reason-coded lifecycle evidence, and
  minimum-cycle continuation is limited to the same persisted mode and period
  timestamps. Malformed legacy away-off start evidence uses a valid takeover
  timestamp as its conservative fallback. An active lifecycle releases
  ownership when its relevant confidence falls below threshold.
- Every integration-owned sensor and binary-sensor attribute payload passes
  through a shared UTF-8 byte budget below Recorder's hard state-attribute
  limit. Oversized nested evidence is compacted deterministically while
  retaining the entity's top-level attribute contract and publishing an
  `attributes_truncated` marker. Calendar summaries, descriptions, locations,
  and UIDs have explicit byte bounds; adversarial Unicode and nested-payload
  tests cover each dynamic entity metadata path.
- Dry-run actions are recorded as intentionally skipped with the stable
  `dry_run` reason. Plan-wide violations remain on plan health instead of being
  copied to unrelated action rejections, including neutral Enphase restores,
  and materially identical audit/comparison records are coalesced with first/
  last occurrence evidence. AI explanation is refused for unsafe or
  zero-confidence plans and reused only while a bounded action, forecast
  preview, issue, and cost signature remains unchanged. Provider work runs as a
  cancellable single-flight task only after a button press, so provider latency
  cannot hold the coordinator refresh lock. Plan commits never initiate or
  retry provider work; they only cancel an in-flight result when its evidence
  becomes obsolete. Final publication is serialized with plan commits, and
  sensors expose the result only when its material fingerprint matches the
  current safe plan. Accepted advice remains visible across regenerated plan IDs
  with an equivalent material signature. Changed plans show bounded pending
  metadata while provider work is in flight or rate-limited, and a single
  delayed retry runs when the provider-call window opens.
- Forecast snapshots and dry-run comparisons use 30-minute UTC buckets,
  time-based retention, and defensive hard caps. A forecast snapshot still
  carries twelve five-minute near-term targets, so the calibration learner
  retains dense target coverage while scanning and serializing at most 128
  snapshots instead of every refresh in a two-day window. Each bucket keeps a
  bounded list of superseded plan IDs and AI provenance so delayed explanation
  metadata remains attachable, plus up to twelve materially distinct action
  variants for operational replay, prioritizing negative-price and active EV
  allocations at the cap, and one successful Recorder-import summary without
  retaining duplicate forecast and training payloads.
- Store persistence serializes concurrent writers and tracks mutation/saved
  generations. Transient failures remain dirty and retryable, including writes
  arriving during an in-flight or delayed save. Home Assistant Store JSON
  serialization runs in its executor, using a captured copy-on-write root so
  large retained histories do not block the event loop.
- Forecast calibration explicitly drops legacy models and rebuilds current
  model fields from bounded timestamped evidence when persisted raw or unique
  counters are inconsistent or implausibly large. Bounded processed-observation
  observation-plus-lead identities prevent duplicate training without dropping
  older observations or newly available lead buckets that arrive out of order.
- Preflight discovery blocks only configured and enabled EV, Climate, and
  Enphase control areas. AI
  provider configuration remains advisory, custom Enphase control services are
  discovered consistently with execution, and keep-on requires an available
  persistent switch/input-boolean. Partial
  EV, Climate, or Enphase installations can arm independently;
  dry-run-only installations keep discovery advisory and cannot claim active
  production readiness without an enabled controllable area. AI availability
  requires both the configured `ai_task` entity and registered
  `ai_task.generate_data`. A newly registered AI Task's initial `unknown` state
  is requestable, while a missing entity or explicit `unavailable` state remains
  blocked; sensor and preflight evidence expose configured, effective
  availability, and a stable reason while Explain remains advisory.
- Preflight distinguishes historical dry-run evidence from current activation
  safety. `safe_to_activate_now` additionally requires a current healthy,
  non-zero-confidence plan, a recent successful coordinator refresh, at least
  eight usable priced hours (or the full configured horizon when shorter), and
  no active control pause. Historical evidence is invalidated when the required
  control areas, mapped entities/services, or decision/control policies change,
  while runtime planner/dry-run mode, per-run EV ready-by changes, and advisory
  AI settings are excluded so an intentional dry-run-to-active transition
  retains valid evidence. The executor and readiness sensor independently fail
  closed on a mismatch, missing state, non-boolean armed value, or malformed or
  unreasonable evidence counter. Pause
  parsing is shared and timezone-aware; malformed active and legacy pause states
  remain paused rather than failing open. Expired records report inactive and
  expired at the report timestamp while retaining reason, assets, and expiry.
  `active_control_ready` still requires the independent production arm.
- Planner refreshes are serialized behind a coordinator lock, and stale planner
  results are discarded before they can overwrite the active plan. Current
  plans are committed while persistence is delayed, then device execution runs
  through a separate command lock after the refresh and Store scopes exit.
  Slow service-feedback confirmation therefore cannot block input collection or
  planning; queued plans coalesce to the newest generation, and execution checks
  staleness again between coordinated device actions. Each execution attempt
  notifies Store-backed entities, unexpected per-plan failures do not strand a
  newer queued safety plan, and unload clears queued work before awaiting the
  current device transaction's confirmation, rollback, and ownership-persistence
  boundary. Teardown also drains any refresh already inside the planner lock;
  queued refreshes become no-ops, and ownership cleanup re-reads state after
  helper-service waits so concurrent device ownership cannot be erased.
- Non-response integration services queue coordinator work in the Home
  Assistant task loop so service calls return quickly; only the explicit
  `export_diagnostics` response service awaits and returns a payload. Service
  reason inputs are bounded and restricted to compact audit codes.
- The `set_ev_ready_by` service validates local time input, normalizes accepted
  values to `HH:MM`, persists the central EV setting, and queues planner work.
  Ready by, opportunistic charging, and its import-price threshold are exposed
  only in EV settings; setup removes their obsolete duplicate entities without
  changing the stored option values.
- Climate, EV, and Enphase each expose a translated device control switch on the
  same Energy Planner device. Automatic control remains the guarded master arm;
  disabling a device restores only that asset, failed restoration leaves the
  selector unchanged, and enabling an area while armed passes preflight without
  disarming unaffected controls.
- All integration services accept an optional `config_entry_id`. A single
  loaded runtime remains backward compatible, while multiple runtimes reject
  ambiguous calls and route explicitly targeted calls to the selected entry.
- EV, Enphase, and Daikin adapters avoid duplicate commands where current
  observable state already matches the requested state. Native EV no-op
  decisions are skipped without consuming command caps.
- EV, Enphase, and Daikin adapters fail closed on Home Assistant service-layer
  errors and return auditable command results instead of raising through the
  planner task.
- Device execution is rate-limited per asset/action kind through a configurable
  command cooldown, while failsafe restore remains exempt so recovery is not
  blocked.
- Config-entry unload and setup-failure paths restore planner ownership without
  scheduling fresh planner work during teardown or failed setup. Listener/timer
  shutdown and an explicit teardown marker also suppress already-queued plan
  commits until a failed unload resumes the coordinator. A failed
  unload restore refuses the unload, disarms production control, and keeps the
  coordinator operational for retry and diagnostics without a fresh replan.
- `export_diagnostics` returns the same redacted compact config-entry
  diagnostics payload exposed through Home Assistant diagnostics, with tests for
  token, coordinate, address, raw prompt, raw model response, location-history
  field redaction, entity mapping, plan metadata, and
  bounded recent outcomes.
- Diagnostics and system health expose rolling refresh metrics when supplied by
  the coordinator, including refreshes per hour, last trigger,
  skipped/coalesced counts, phase durations, and the usable optimization
  horizon, while remaining compatible with older coordinators.
- Multi-entry system health is deterministic, reports aggregate worst-case
  health, and includes bounded per-entry summaries. Ownership diagnostics also
  reflect reservation-only and provisional EV recovery state.
- Home Assistant validation is covered by Docker smoke coverage, Home Assistant
  `check_config`, unit tests, replay fixtures, live-schema fixtures, and
  real-history replay fixtures. The optional
  `scripts/export-real-validation-bundle.sh` command remains available for
  later validation against an operator's actual Home Assistant instance, but
  real-instance execution is not required for the current covered status.


## Stable release preparation (September 2026)

- Recovery persistence observes the actual Home Assistant Store write hook. Write
  and serialization failures remain dirty and block new acquisitions; cancellation
  drains the write and shutdown flushes deferred data before acknowledging it.
  Evidence: `durable_storage.py`, `tests/test_storage_runtime.py`.
- Real HA upgrade/reload/restart tests use a synthetic 0.9.18 Store/config fixture,
  preserve the live Mode entity ID, manual override and unresolved ownership and
  reservation. Missing legacy vehicle targets create a fixable Repairs issue. Repairs and Reconfigure share target validation and preserve legacy mappings. HA 2026.9 saves the correction with explicit restart instructions; newer HA retries migration through the public API. Repair and Reconfigure reject changes during migration and configurations from newer integration versions. Real failed-setup, repair, and restart evidence is in `tests/test_repairs.py`.
  Evidence: `tests/test_upgrade_runtime.py`, `tests/fixtures/upgrade/0.9.18.json`.
- Entry deletion removes resolved per-entry storage and retains unresolved evidence.
- Support policy and tooling versions come from HACS/pyproject through
  `scripts/support_policy.py`. Weekly compatibility jobs pull their runtime image.
- Deterministic component ZIP/checksum creation runs before publication; runtime
  smoke installs the unpacked artifact. Exact image/tool metadata is retained.
- Current configuration, public contracts, upgrade/rollback and data-retention
  policy are in `docs/stable-release.md`; the old specification is archived.
- `docs/release-checklist.md` and the observation validator require real operating
  evidence before a stable 1.x release. That observation is pending; synthetic
  validation does not claim household acceptance.
  `tests/scripts/test_release_tools.py` verifies that every scenario rejects
  missing, blank, and non-string evidence references.

### Expired climate comfort holds

- `planner_hvac.py` resolves a persisted hold-only `released_until` before
  ownership-dependent comfort and override checks. A future hold prevents
  reacquisition, and an expired hold permits normal preconditioning immediately,
  including at or beyond either comfort boundary. Additional ownership and failed
  restoration metadata retain the existing recovery path.
- `tests/test_planner.py::test_hold_only_state_expires_before_comfort_handoff`
  covers serialized timestamps before, at, and after expiry on both boundaries.
  `test_expired_hold_keeps_unresolved_hvac_ownership_recovery` verifies that
  unresolved actuator ownership is still restored before any new takeover.


### Shared charger vehicle profiles

- Planner settings expose shared charger power and charging policies; Vehicle settings own SOC, target SOC, ready-by, effective vehicle power and bootstrap efficiency. Vehicle power is an optional override capped at shared charger power; omitted overrides follow shared power changes. Existing overrides are preserved. Initial efficiency is in a collapsed Advanced calibration section and is stored flat for compatibility. The displayed hub schema also defines permitted writes. Profile-owned fields are excluded from hub form writes as well as display, preserving hidden legacy values even after the last profile is removed (`tests/test_vehicles.py`).

- `vehicles.py`, `config_flow.py`, and `entry_data.py` provide repeatable vehicle profiles without flattening car telemetry into charger settings. `select.py` and `sensor.py` expose selection, resolved identity, and detection reasons. Required target SOC comes from a sensor only; ready-by and charging characteristics are per vehicle.
- `coordinator.py` resolves home/port/charger evidence, listens to all vehicle entities, persists Manual selection across reloads and resets it on unplug. EV charging overrides are removed synchronously at a session boundary before the replacement vehicle’s planning context is built. Previously connected vehicle IDs are excluded from automatic reuse until port-disconnect evidence is observed, including across restart. Listener saves also persist port-only evidence changes, and queued unplug boundaries following unknown feedback reset the session. Queued saves read the current session to preserve newer selections. Ambiguous or unavailable evidence withholds EV commands while permitting unrelated household planning.
- `executor.py` releases old session ownership without commanding the charger. `ev_adapter.py` checks the captured vehicle/session token at each service boundary, including retries, helper writes and restoration. A previous vehicle's pending command cannot cross a detected session boundary.
- `VehicleCalibration` learns separately from completed, continuously attributed local charging intervals. `training.py` excludes shared-charger Recorder history from vehicle calibration. Queued charging stop/resume and unavailable events discard pending learning even without an intervening refresh. Incomplete learning is discarded on restart or interrupted identity; completed per-car models persist.
- Runtime profile deadlines are excluded from topology and production-evidence fingerprints; updates invalidate pending plans and replan without disarming household control. Charger-first setup accepts shared feedback without legacy vehicle mappings. Unmanaged charging retains conservative load projections, and confirmed unplug events release stale reservations even across rapid replug. Monotonic unplug generations ensure an older pending ownership write cannot consume a newer unplug boundary, including same-car reconnection. Percent-suffixed SOC uses the same normalization in session validation and calibration.
- `tests/test_vehicles.py` exercises these regression paths plus BMW-style connection/location states, away charging, unknown/ambiguous identity, target loss, guest mode, unplug reset, reload, physical swaps, stale command tokens, storage isolation, profile configuration and UI entities. Full validation remains `scripts/docker-validate.sh`.
## Economic climate planning

The automatic climate policy collects ownership-tagged observations, validates a
learned normal-operation baseline chronologically, and compares preconditioning
with normal operation using the same EV allocation and site-energy model.
Readiness never arms production control. Manual intervention, uncertain inputs,
unsupported battery dispatch and failed restoration remain blocking conditions.

Implementation evidence: `climate_inputs.py`, `climate_learning.py`,
`climate_economics.py`, `climate_optimizer.py`, `climate_runtime.py` and the existing
planner/executor transaction paths. Regression evidence is in
`tests/test_climate_engine.py`; the complete Docker gate remains mandatory.
See [Climate decision policy](climate-decisions.md) for settings, model limitations
and the distinction between observed energy and estimated avoided cost.

Climate review regressions additionally cover policy-switch restoration,
arrival deadlines inside forecast slots, conservative terminal battery state,
EV/grid capacity, per-room chronological validation, observation contamination,
actual validation-window expiry and requalification, and interval-aligned energy
comparisons. A bounded candidate set receives complete validation, with explicit failure when
mandatory start/target coverage exceeds its budget; solar interpolation never extrapolates beyond forecast coverage.

Further climate review coverage exercises exact persisted phase boundaries within
new slots, candidate-order independence, paired demand uncertainty under negative
tariffs, per-room arrival/recovery, zone activation prediction, observation-only
legacy restoration, arrival beyond release, learned normal target selection and
successful candidate selection on a covered 12-hour forecast. Normal-operation
lookup uses the same 0.25°C grid in validation and runtime simulation.

### EV scheduling extension

- `ev_optimization.py` evaluates physical charging, partial slots, capacity, soft readiness margins, battery opportunity cost, and bounded search; `tests/test_ev_optimization.py` includes an exhaustive small oracle and a 48-hour runtime bound.
- `ev_policy.py`, `ev_telemetry.py`, and `ev_runtime.py` define optional number capabilities, measured performance, conservative session spending, and command leases. `tests/test_ev_runtime.py` verifies ordering and failure paths; synthetic schema/history fixtures exercise the same parsers.
- Existing multi-EV reservations remain authoritative; this change does not implement joint multi-EV scheduling. See `docs/ev-scheduling.md` for policy defaults and modelled-cost limitations.

- `ev_capacity_readiness_buffer.json` replays capacity exclusion and buffer preservation through generated plans and real Home Assistant services. Calendar regressions distinguish physical limits, partial-slot energy, and actual completion times.
- The full Docker gate includes optional number controls, feedback, persisted recovery metadata reload, packaged-install smoke validation, prior-release upgrade recovery, and unchanged cross-entry reservation tests. The 576-slot benchmarks include fixed and variable power with chronological battery economics and enforce the five-second limit.

- Candidate-constraint regressions cover active Continuous sessions with retained future windows and low-price charge-now triggers in Split/Adaptive modes. Calibration regressions include measured stalls and immature-model diagnostics (`tests/test_ev_optimization.py`).

- Runtime recovery regressions verify that lease expiry stops a previously-on baseline and that a confirmed stop ends spending despite failed number restoration (`tests/test_control_runtime.py`). A retained-night-window regression preserves selected daylight preference (`tests/test_ev_optimization.py`).

- `test_confirmed_stop_is_not_billed_again_on_the_next_telemetry_update` exercises lease expiry followed by repeated telemetry updates with a retained reservation, preserving the settled spending total.

- Rebase integration regressions cover vehicle-session guards before and after number writes and policy-release spending/timer cleanup. Confirmed unplug resets the budget and incomplete measurements; uncertain handoff retains spending (`tests/test_ev_runtime.py`, `tests/test_control_runtime.py`).


## Preconditioning explanations and missed-window evidence

- Planning records manual/occupancy and observation-policy blockers at the decision point and retains legacy candidate rejection counts with measured thresholds (`climate_runtime.py`, `planner_hvac.py`). Candidate evaluation is distinguished from command selection, including final battery-profile rejection.
- `preconditioning.py` joins current planning evidence with ownership and matching execution outcomes. Decision summary and diagnostics expose current status, next start, next step, legacy measurements, and the most recent missed window without changing control authority or safety limits.
- `PlannerStore` persists the pending and last missed windows with plan/outcome writes, preserving immutable save generations. Windows correlate across regenerated plan IDs; applied retries prevent false missed records and clear a recovered same-window record.
- `tests/test_preconditioning.py` covers blocker recovery, observation policy, hold expiry, forecast/price/lead/rest evidence, planned versus actual control, store reload, repeated refreshes, failed and successful attempts, expired/withdrawn windows, restored ownership, stale-outcome isolation, and attribute bounds. Existing climate/executor/adapter recovery regressions remain part of the full Docker gate.

- Review regressions reproduce concurrent replan/late-outcome correlation, restart between committed ownership and audit writes, coasting-only false positives, post-planning validation downgrade, and the final restoration-target gate. Storage-level tests verify durable readback and immutable prior generations.

- Simultaneous-blocker regressions verify that legacy confidence, manual override, and occupancy reason codes stay aligned with their explanations and move to the remaining blocker when confidence recovers.

### Climate recovery and action allowance lifecycle

- `action_limits.py` and the persistent `action_attempts` ledger account for real/uncertain attempts independently of audit rotation; no-op climate suppression is excluded. Boundary, legacy migration and restart evidence: `tests/test_action_limits.py`, `tests/test_storage.py`, `tests/test_control_runtime.py`.
- Policy-only options updates preserve active ownership and armed state; the existing recovery path remains for safety-sensitive changes. Evidence: `tests/test_coordinator.py` and real Home Assistant service/event/storage lifecycle tests in `tests/test_control_runtime.py`.
- The Resume climate planning service and button clear manual helper/internal holds under execution/planner locks, replan, and expose remaining gates. Evidence: `tests/test_services.py`, `tests/test_switch_button.py`, `tests/test_coordinator.py`, `tests/test_control_runtime.py`.
- Climate diagnostics expose allowance usage and the next usable allowance after a cap rejection. Evidence: `tests/test_diagnostics.py`.

- Climate confirmation yields to queued HA state listeners while command attribution remains active; the compatibility suite exercises policy updates, real manual recovery and delayed main shutdown feedback on each supported HA release.

### Confirmed calendar starts and uncertain EV delivery

- `calendar.py` combines current plans with confirmed charging-state timestamps and committed climate phase ownership. `hvac_control.py` persists phase starts. `tests/test_calendar.py` covers replans, fresh wrappers, future windows, stop/restart and missing evidence; `tests/test_executor.py` covers climate phase transitions.
- `ev_optimization.py` retains conservative readiness while accounting for the possible cost/carbon/battery/emergency-budget exposure of continued commands under stalled/unavailable delivery. `ev_telemetry.py` accepts positive measured power over an unchanged cumulative meter for delivery classification. Reproduction and regression evidence: `tests/test_ev_optimization.py`.

### Review regressions for recovery and calendar changes

- `tests/test_executor.py` exercises the retry gate with both compact and full audit records, and climate phase transitions through the real prepare/complete ownership sequence.
- `tests/test_coordinator.py` verifies that Resume waits for deferred execution evidence before reporting blockers. `tests/test_control_runtime.py` uses the real Home Assistant refresh debounce to verify an immediate fresh result during cooldown and an explicit service error when refreshing fails.
- `tests/test_calendar.py` covers confirmed off-state coasting and legacy non-mapping ownership. `tests/test_diagnostics.py` verifies that expired action-cap evidence does not present an exhausted allowance or a nonexistent expiry.

## EV consumption-outage resilience and explicit charging

- Known load outages have bounded model authority, a conservative upper-load margin,
  no automatic starts and no setpoint increases. Shared reservations remain authoritative;
  unsafe/uncertain reductions stop safely. Evidence: `ev_resilience.py`, `inputs.py`,
  `planner.py`, `executor.py`, `ev_runtime.py`; regressions in `test_ev_resilience.py`,
  `test_inputs.py`, `test_planner.py`, `test_executor.py`.
- Charge now is an explicit expiring economic override with connection, target and
  capacity gates, confirmed command handling and an execution timer. Evidence:
  `coordinator.py`, `button.py`, `__init__.py`, `services.yaml`; service/coordinator tests.
- Persisted outage age survives flapping; two fresh samples establish stable recovery.
  Status and deduplicated interruption notifications explain degraded/blocked charging.
  Evidence: `ev_resilience.py`, `sensor.py`, `plan_presentation.py`, coordinator tests.

- Charge now bypasses refresh debounce and rejects failed refreshes before issuing commands;
  real Home Assistant coverage: `test_charge_now_requires_fresh_evidence_during_debounce_cooldown`
  in `tests/test_control_runtime.py`. Confirmed manual/automatic resumes clear interruption
  alerts and reset recurrence deduplication; executor tests cover both paths.
