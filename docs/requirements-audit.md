# Requirements Audit

Status as of 2026-08-02.

## Covered

- Custom integration scaffold, config flow, options flow, entities, services,
  diagnostics, and versioned Home Assistant `Store` persistence are present.
  The manifest classifies Energy Planner as a service integration so configured
  entries remain visible on the main Devices & services integration page rather
  than being routed to the Helpers-only experience.
  Diagnostics expose redacted entity/service mapping, input-health metadata,
  plan metadata, latest HAEO run status, bounded recent outcomes, and compact
  Store summaries rather than relying on unbounded raw Store inspection.
  Store load normalizes known schema fields so missing or malformed older data
  falls back to safe list/dict defaults while preserving unknown metadata, and
  malformed persisted execution timestamps are ignored instead of raising
  through safety-gate evaluation.
- The takeover-active binary sensor is tied to persisted planner ownership
  state rather than merely reporting candidate actions in the current plan.
- Planner enable, dry-run, AI-enable, replan, and restore-safe-state entity
  controls have direct regression coverage for option updates and coordinator
  calls. Runtime option updates request replanning without reloading the config
  entry. Docker smoke coverage exercises the planner-enabled, AI-enabled, and
  dry-run switch entities plus the replan and restore-safe-state button entities
  through Home Assistant Core service calls.
- Config flow validation checks mapped entity IDs, expected domains, compatible
  units where exposed, entity availability, and configured service availability
  without issuing commands. The user must provide mapped people/entities rather
  than receiving environment-specific person defaults in production Python.
  Options flow validation enforces coherent device constraints and supported
  unique priority-weight tokens before configuration values reach the planner.
  Settings backed by native entities are centrally excluded from the Options
  schema, keeping configuration and day-to-day control on distinct surfaces
  without deleting existing stored values during upgrade.
- The planner builds a 24-hour, five-minute decision context and keeps compact
  plan, HAEO, forecast, bounded action, AI, ownership, override, and outcome
  records.
- Published plan-status, next-action, and dry-run comparison sensor attributes
  are compact, JSON-friendly, and bounded so enum/datetime values are
  serialized and nested audit evidence cannot exceed Recorder's state-attribute
  limit. Full bounded dry-run evidence remains available through diagnostics.
- Forecast confidence is calculated from source confidence metadata where
  exposed, uses a conservative lower confidence for point-sensor forecast
  fallback, and caps the published plan/action confidence without weakening
  fail-closed health checks.
- Weather forecast attributes and current weather state are normalized into
  outdoor temperature values for the decision context, with canonical attribute
  matching and Fahrenheit-to-Celsius conversion for common weather schemas.
- HAEO integration uses configured Home Assistant services only, with baseline
  and flexible-load second-pass calls and no private HAEO imports. Parsed
  second-pass grid/battery evidence is applied before final hard-constraint
  validation and persisted with the HAEO run metadata. Service failures,
  including legacy no-`return_response` fallback failures, are reported as
  failed HAEO solves instead of raising through planner refresh.
- HAEO response parsing accepts flat slot lists, timestamp-keyed schedules,
  nested grid/battery evidence, and camelCase live-export keys with W-to-kW
  normalization; parsed battery charge/discharge evidence is covered through
  the Enphase arbitrage planner path. Non-finite and out-of-horizon timestamped
  HAEO evidence is ignored before it can influence grid-limit validation or
  arbitrage-value calculations.
- Flexible-load second-pass readiness clears inherited baseline grid fields
  before checking response coverage. Accepted import/export pairs carry per-slot
  provenance so constraints and cost estimates do not add projected EV/HVAC
  demand twice; uncovered slots retain their baseline evidence instead.
- Safety defaults are fail-closed: execution disabled, dry-run enabled, stale
  required inputs unsafe, non-finite numeric inputs rejected, and due actions
  revalidated before execution.
- Configurable grid import/export kW limits are represented as options and
  validated as hard constraints against HAEO grid-flow evidence when available,
  otherwise against normalized PV/load plus projected EV/HVAC flexible load.
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
  acceptance is tracked separately from proven-safe stop confirmation. Neither
  disconnected feedback nor the state of a separate stop command helper can
  release safety ownership; meaningful charging feedback, the persistent EV
  charger control, or rollback must prove the safe state. A compensating stop
  that does prove the safe state is recorded as a successful recovery and
  clears ownership and household capacity. Manual compensation follows the same
  success contract and creates the normal stop override, while unconfirmed owned
  stops retain ownership and capacity for recovery.
- Manual EV commands, scheduled execution, and explicit restoration are
  serialized by the coordinator. Regular planner-owned schedule stops use the
  same proven-safe release contract as synthetic safety stops, and unowned stop
  commands cannot create a restorable takeover baseline.
- EV target SOC can be sourced from an external vehicle sensor, with the native
  number retained as the fallback. Unavailable, nonnumeric, and out-of-range
  external values use that fallback without degrading active control while
  remaining visible as advisory input evidence. Preconditioning keep-on is
  suppressed while this fallback is active because the vehicle-enforced target
  cannot be verified. When no connected-state entity
  is mapped, a native EV-device helper switch provides connected state and
  drives the same compact trip-history path.
- The optional preconditioning policy keeps the charger control enabled after
  target SOC is reached while preserving manual-stop and execution safety-gate
  precedence. Only the actual after-target preconditioning action selects the
  control-state confirmation path, that path requires the persistent direct
  charger control rather than an optional start command, and the current slot
  reserves the full configured charger rate for grid and battery evaluation.
  EV reconfiguration, preflight, and the integration-created keep-on switch
  reject keep-on without that persistent switch/input-boolean control.
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
  explicitly forced current slot is the bounded exception: minimum-SOC recovery
  and enabled below-threshold opportunistic charging may claim the current slot
  before the configured earliest start, while any remaining continuous window
  stays within the configured hours.
- Multiple EVs are supported as separate named config entries. Entry-scoped
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
  enabled manual-override helper creates an authoritative indefinite override;
  helper feedback from integration-created timed overrides is guarded.
- Persistent notifications are emitted for restore-safe-state, infeasible EV
  ready-by schedules, unsafe required inputs, grid-limit fallback, and HAEO
  fallback classes, using stable notification IDs and compact redacted reason
  codes. Native HAEO response and flexible-projection capability gaps remain
  diagnostic evidence without producing an actionable fallback notification.
  Identical fallback conditions notify once and are updated only when their
  content changes. The three plan-fallback notification classes can be
  disabled as a group; doing so dismisses their stable IDs without changing plan
  health or fail-closed execution. User-provided service reason fields are
  validated as compact reason codes before they can be persisted or shown in
  notifications.
- Discovery records non-commanding capability evidence for HAEO, EV, Daikin,
  Enphase, and the local AI service before active control is allowed.
- Local AI advice is disabled by default, minimized, JSON-only, whitelisted,
  and advisory. Unsupported response fields are rejected, so it cannot call
  services or change hard constraints. The integration warns that provider
  integrations may independently log bounded prompts. Docker smoke coverage
  exercises a response-capable local AI advisor service through Home Assistant
  Core and verifies accepted bounded advice in Store recommendations.
- Replay fixtures cover stale inputs, battery floor rejection, EV infeasible
  ready-by evidence, negative-price EV scheduling, HVAC occupancy/manual
  override rules, and Enphase holds.
- Executable live-schema fixtures cover representative Amber price, PV/HAFO,
  baseline-load, weather, timestamp-keyed nested HAEO, and list-based nested
  HAEO response payload shapes through `scripts/validate-live-schema-fixture.py`,
  so sanitized real Home Assistant exports can be validated outside pytest.
  `scripts/export-real-live-schema.sh` wraps the required real export set, and
  `scripts/export-live-schema-fixture.py` can export individual Home Assistant
  state/service payloads into that fixture format using operator-supplied URL
  and token values, with built-in and operator-specified key redaction plus an
  optional pre-write parser validation gate. The validator also has a
  `ha-energy-planner-v1-real` profile that reports missing required real-export
  fixture names, mismatched fixture kind/value-kind metadata, and missing
  exported source entity/service metadata before full live-schema completion is
  claimed. The stricter `ha-energy-planner-haeo-value-v1-real` profile requires
  the real HAEO response fixture to include parsed grid import/export and
  battery charge/discharge evidence before Enphase value-evidence validation is
  accepted.
- Executable real-history fixtures cover Recorder-style MINI trip replay,
  Daikin thermal-model replay, and rolling-origin PV/load forecast accuracy
  through `scripts/validate-real-history-fixture.py`. Forecast evidence is
  matched by issue/valid time, reports MAE and RMSE for near/day/long lead-time
  buckets, and must outperform a no-lookahead persistence baseline.
  `scripts/export-real-history-fixtures.sh` wraps sanitized Home Assistant
  history export for the required `real_mini_trip_history`,
  `real_daikin_thermal_history`, `real_pv_forecast_accuracy`, and
  `real_load_forecast_accuracy` fixtures, and the
  `ha-energy-planner-history-v1-real` profile verifies exported source entity
  metadata before real-history completion is claimed.
- Forecast parsing retains uncovered horizon slots as missing values and never
  extrapolates the final bucket. Per-input evidence reports first/last
  timestamps, total and continuous coverage, and leading/internal/trailing
  gaps. Continuous coverage is healthy at 12 hours, degraded from 8 to under
  12 hours, and unsafe below 8 hours; thresholds are capped by deliberately
  shorter configured horizons. Degraded inputs remain action-ineligible under
  the planner's existing healthy-input action gate.
- A second optional PV entity supports timestamp-safe Solcast Today/Tomorrow
  stitching across midnight and daylight-saving changes. Secondary series must
  expose timezone-aware timestamps; untimestamped and naive timestamps are
  diagnosed and rejected, and secondary slots are excluded from calibration
  until per-slot issue-time provenance is retained. Required Amber, PV, and
  load point values cannot satisfy forecast coverage. Short baseline-load
  leading gaps are filled from the current numeric state for at most one hour,
  with explicit diagnostic evidence and reduced confidence; long or internal
  gaps remain missing and fail closed.
- `scripts/export-real-validation-bundle.sh` runs both real export wrappers and
  enforces all real validation profiles in one command so full real-system
  evidence cannot accidentally skip live-schema, HAEO value, or Recorder
  history validation. Its `--validate-only` mode checks already exported
  `real_*.json` fixtures without calling Home Assistant again.
- Docker Home Assistant validation is available through
  `scripts/docker-validate.sh`, `scripts/docker-ha-smoke.sh`, and
  `docker compose`. The full validation gate runs compile and Ruff checks, Dockerized
  pytest, replay fixtures, live-schema validation, real-history validation, Home Assistant
  `check_config`, and the smoke test in one repeatable sequence. The smoke test
  now verifies coordinator refresh, entity
  registry entries, published plan-status/data-health/takeover/dry-run entity
  states, device registry registration, persisted active plan, HAEO run
  metadata including parsed camelCase grid-charge and export/discharge evidence
  counts from a response-capable Home Assistant service, discovery storage,
  forecast snapshot training slots sourced from Home Assistant template-sensor
  forecast attributes using canonical live-style key variants and W-to-kW
  normalization, Amber cent/kWh forecast attributes reflected in compact plan
  previews, weather camelCase forecast attributes reflected in compact plan previews,
  forecast calibration state updated from time-aligned, dedicated observed-power
  entities, HVAC thermal-model state updated from Home Assistant climate and
  power entity samples, Recorder import metadata, and a compact EV trip
  imported from Home Assistant Recorder state history. It also verifies
  bounded forecast-snapshot action metadata for the active EV schedule with
  runtime ready-by override, an active EV schedule allocated to a negative
  import-price slot, and an Enphase arbitrage action backed by parsed HAEO
  value evidence. It also verifies
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
  replan and restore button execution through Home Assistant Core, restore-safe-state
  persistent notification service emission through Home Assistant Core,
  enabled/dry-run/AI-enabled switch execution through Home Assistant Core,
  accepted bounded local-AI advice from a response-capable Home Assistant
  service, and verifies the
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
  PV, and baseline-load entities, including cent-to-dollar price and W/kW/MW
  power normalization, plus weather forecast temperature attributes, with
  point-sensor fallback. Optional point-sensor power inputs used for forecast
  calibration and the HVAC thermal model use the same W/kW/MW normalization.
  HAEO evidence applies the same W/kW/MW normalization and canonical key
  matching before grid-limit or arbitrage calculations. Representative
  integration-specific Amber, PV/HAFO, weather, and HAEO schemas are covered by
  executable live-schema fixtures and the real-export validator profiles.
- Compact PV and baseline-load forecast calibration is implemented. It records
  due forecasts only when a separately configured measured-power observation is
  timestamp-aligned, deduplicates forecast targets and lead-time buckets, and
  retains a bounded sample window with diverse forecast horizons. Independent
  robust bounded factors are trained per 30-minute lead-time bucket and enabled
  only when that bucket improves a later holdout set spanning enough distinct
  observations and time; near-term evidence cannot alter day-ahead slots.
  Forecast entities are never used as
  actuals, overdue slots are not paired to a current reading, and non-finite
  persisted factors and sample values are ignored.
- Runtime calibration snapshots retain dense near-term targets plus bounded,
  stratified targets through the complete planning horizon. Enabled lead-time
  models expose p10/p90-style bounded factors; conservative flexibility and
  battery calculations use lower PV and upper load while financial estimates
  retain the holdout-validated expected factor.
- Optional grid carbon-intensity series are normalized to gCO2/kWh. Carbon has
  a non-zero action score when the forecast varies, and EV allocation blends
  normalized effective cost with grid emissions according to configured
  priority order while accounting for conservative solar displacement.
- EV next-day demand uses configured fallback until enough local trip history is
  recorded. Future disconnected trips are compactly stored from EV connection
  and SOC state transitions, and older trips are opportunistically imported
  from Home Assistant Recorder when EV SOC and connection entities are mapped.
  Recorder history reads use Recorder's database executor when available and
  fall back to Home Assistant's generic executor only when Recorder is absent.
  The Recorder compactor and current-state paths accept common MINI-like
  connected/disconnected states, connected-not-charging states, and SOC strings
  with percent units or comma decimals. Only compact trip records are kept in
  `Store`. Configured charging feedback also accepts connector-status sensors;
  `SUSPENDED_EV` and `SUSPENDED_EVSE` are normalized as connected but not
  actively charging, so momentary stop controls are not called after a vehicle
  has already suspended power delivery. Disconnection remains insufficient
  evidence for a safe momentary stop.
  Summaries use max daily SOC consumption for the ready-by target when
  sufficient. Persisted Recorder import timestamps tolerate malformed or
  timezone-naive older values without raising through planner refresh. Docker
  smoke coverage now validates one Recorder-imported EV trip from Home
  Assistant state history, and real-history replay fixtures validate broader
  MINI-like state names and SOC formats outside the running HA smoke container.
- Live trip updates preserve Recorder import metadata and successful empty
  imports advance it, so installations without recent trips do not repeat a
  30-day Recorder query on every planner refresh.
- HVAC active planning is conservative: away mode off is preserved outside a
  persisted `precondition -> pre_peak_coast -> peak_coast -> release` tariff lifecycle. Every
  valid tariff slot in the configured 1-48 hour horizon is scanned, with the
  current 12-hour default unchanged. Relative pre-window baselines, both price
  deltas persisted at acquisition, contiguous peak boundaries, weather-led heat/cool selection, exact
  high/low comfort targets, least-cost thermally feasible runs, comfort coast,
  conservative maintenance load, and tariff-change fail-safe release are
  implemented. Takeover snapshots configured switch/input-boolean zones,
  disables mapped automations, enables zones, explicitly turns on the climate
  entity, and preserves the original snapshot across peak transitions. Release
  restores zones, enables every automation, retains unresolved ownership for
  retry, and never restores the prior climate mode or setpoint. Comfort-boundary
  release is held through the recorded peak end to prevent reacquisition. A compact
  HVAC thermal model records current indoor temperature, optional Daikin power,
  and optional weather temperature samples. Version 2 requires samples at least
  five minutes apart, ignores sensor deltas below effective precision, excludes
  HVAC start/stop/mode transitions, requires explicit stable heat/cool mode and
  power evidence for active learning plus explicit off/idle evidence for passive
  learning, rejects implausible rates instead of
  clamping them, and derives medians from bounded rolling windows. Legacy
  unbounded statistics are reset before a new anchor is accepted. It also
  tolerates timezone-naive timestamps and comma-decimal strings and ignores
  non-finite values. Planner tests cover replayed cold/heating and
  warm/cooling thermal samples feeding preconditioning projections. Docker
  smoke coverage validates one active HVAC power sample from Home Assistant
  climate/power entities plus occupied preconditioning, expensive-period
  automation suppression, and restoration through Home Assistant services.
  Planner, adapter, coordinator, executor, configuration, discovery, service,
  replay, and diagnostic tests cover full-horizon detection, cold/hot target
  selection, zone takeover/rollback, helper overrides, comfort handoff, peak
  continuation, production/pause-blocked safety release, persisted tariff
  thresholds, and release evidence.
- Enphase execution, verification, hold, minimum-savings gates, and profile
  action generation are implemented. The planner can set a configured
  arbitrage profile when HAEO battery charge/discharge value, HAEO export
  forecast value, or fallback import/export spread exceeds the threshold, and
  restore the configured AI profile when takeover is no longer justified. HAEO
  service responses are parsed into compact grid import/export, battery
  charge/discharge, and battery SOC forecast evidence where available. Direct
  replay or persisted non-finite HAEO evidence is ignored before Enphase value
  calculations so it cannot suppress restore or publish NaN plan values.
- HVAC automation/zone takeover and Enphase profile commands use bounded,
  transactional compensation. Any automation, zone, or saved profile that
  cannot be restored remains in ownership for a later refresh or
  restore-safe-state retry.
- Only an explicit allowlist of decision inputs can request replanning. AI
  result, integration-owned control, climate automation, and high-frequency
  observed power entities cannot create feedback loops; observation-only
  values are sampled on planning boundaries. Material changes are debounced,
  constrained by a one-minute non-manual refresh floor, and coalesced. A stable
  decision-input fingerprint skips HAEO, planning, execution, snapshots, and
  persistence when no material input changed, while explicit manual replans
  always force a fresh computation.
- Coordinator startup schedules recurring wall-clock planning-interval boundary
  refreshes on the same epoch cadence used by decision fingerprints, without
  also registering a fixed `DataUpdateCoordinator` poll, in addition to material-
  change replans. Non-hour-divisor intervals therefore neither drift nor share a
  stale fingerprint bucket. Planner cost previews use the configured planning
  interval rather than assuming a fixed slot duration.
- EV ready-by wall times are resolved in Home Assistant's configured timezone,
  normalized to UTC, and handle next-day rollover, DST folds, and nonexistent
  local times. HVAC suppression and precondition projection windows compare
  timestamps, so their duration is independent of planning interval.
- HAEO integration detects response and flexible-projection capabilities,
  deterministically selects a unique native config entry, skips services that
  cannot return planner evidence and unsupported second passes, and uses a
  bounded 30-second equivalent-input cache. Service-call status is distinct
  from forecast-evidence status; READY requires continuous import and export
  evidence across at least 80% of the requested solve slots. Solve and
  coordinator refresh duration,
  trigger, coalesced/skipped counters, refreshes/hour, phase timing, cache,
  evidence, and capability metadata are available to diagnostics consumers.
- Dry-run actions are recorded as intentionally skipped with the stable
  `dry_run` reason. Plan-wide violations remain on plan health instead of being
  copied to unrelated action rejections, including neutral Enphase restores,
  and materially identical audit/comparison records are coalesced with first/
  last occurrence evidence. AI advice is skipped for unsafe or zero-confidence
  plans and reused only while a bounded action, forecast preview, issue, and
  cost signature remains unchanged. Provider work runs as a cancellable
  single-flight task after plan commit, so advisory latency cannot hold the
  coordinator refresh lock. Final publication is serialized with plan commits,
  and sensors expose advice only when its material fingerprint matches the
  current safe plan. Accepted advice remains visible across regenerated plan IDs
  with an equivalent material signature. Changed plans show bounded pending
  metadata while provider work is in flight or rate-limited, and a single
  delayed retry runs when the provider-call window opens.
- Forecast snapshots, dry-run comparisons, and HAEO run evidence use time-based
  retention with defensive hard caps, preserving day-ahead training evidence
  across manual refresh bursts without unbounded storage growth.
- Store persistence serializes concurrent writers and tracks mutation/saved
  generations. Transient failures remain dirty and retryable, including writes
  arriving during an in-flight or delayed save.
- Forecast calibration explicitly drops legacy models and rebuilds current
  model fields from bounded timestamped evidence when persisted raw or unique
  counters are inconsistent or implausibly large. Bounded processed-observation
  observation-plus-lead identities prevent duplicate training without dropping
  older observations or newly available lead buckets that arrive out of order.
- Preflight discovery blocks only configured and enabled control areas. AI
  provider configuration remains advisory, custom Enphase control services are
  discovered consistently with execution, and keep-on requires an available
  persistent switch/input-boolean. Partial
  EV, Climate, Enphase, or explicit HAEO installations can arm independently;
  dry-run-only installations keep discovery advisory and cannot claim active
  production readiness without an enabled controllable area.
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
  remain paused rather than failing open.
  `active_control_ready` still requires the independent production arm.
- Planner refreshes are serialized behind a coordinator lock, and stale planner
  results are discarded before they can overwrite the active plan or execute
  device actions when a newer replan request has arrived.
- Non-response integration services queue coordinator work in the Home
  Assistant task loop so service calls return quickly; only the explicit
  `export_diagnostics` response service awaits and returns a payload. Service
  reason inputs are bounded and restricted to compact audit codes.
- The `set_ev_ready_by` service validates local time input, normalizes accepted
  values to `HH:MM`, persists the native setting, and queues planner work. The
  `set_ev_target_soc` service validates and persists a percentage target. Native
  time/number entities expose the same controls on the EV device. The EV device
  also exposes a native opportunistic-charging switch and import-price threshold
  number; both persist the corresponding planner option and request an immediate
  replan.
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
  field redaction, entity mapping, latest HAEO status, plan metadata, and
  bounded recent outcomes.
- Diagnostics and system health expose rolling refresh metrics when supplied by
  the coordinator, including refreshes per hour, last trigger,
  skipped/coalesced counts, phase durations, and the usable optimization
  horizon, while remaining compatible with older coordinators.
- Multi-entry system health is deterministic, reports aggregate worst-case
  health, and includes bounded per-entry summaries. The takeover diagnostic also
  reflects reservation-only and provisional EV recovery state.
- Estimated-cost telemetry reports the usable priced horizon and uses Home
  Assistant's configured currency with the monetary sensor device class.
- Home Assistant validation is covered by Docker smoke coverage, Home Assistant
  `check_config`, unit tests, replay fixtures, live-schema fixtures, and
  real-history replay fixtures. The optional
  `scripts/export-real-validation-bundle.sh` command remains available for
  later validation against an operator's actual Home Assistant instance, but
  real-instance execution is not required for the current covered status.
