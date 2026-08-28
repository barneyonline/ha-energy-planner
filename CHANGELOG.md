# Changelog

## Unreleased

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- None

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.9.15 - 2026-08-25

### 🚧 Breaking changes

- None

### ✨ New features

- Added a configurable 0–30 minute household-load outage grace, defaulting to
  10 minutes. A current, quality-approved built-in model can conservatively
  bridge only a known `unknown`/`unavailable` transition inside that window.

### 🐛 Bug fixes

- Home Assistant startup no longer raises a safe-state restore failure when the
  Enphase profile entity is still loading and Energy Planner has no recorded
  Enphase ownership to restore. Genuine owned-profile restore failures remain
  actionable and retain their recovery evidence.
- The main climate rollback target is now checked during discovery, planning,
  preflight, and immediately before execution in every configuration; configured
  zone targets are additionally checked when temperature synchronisation is
  enabled. Missing targets hard-suppress new HVAC takeover while keeping release
  eligible and notify once without blocking independent EV control.
- Weather planning now retrieves Home Assistant's hourly
  `weather.get_forecasts` response, normalises naive timestamps in the Home
  Assistant timezone, and uses a freshness-bounded cache before legacy attribute
  or point fallbacks with their actual source and coverage reported.
- Continuous household-load outage timing now survives unavailable-state
  changes, non-numeric interludes, reloads, and restarts, so the grace cannot be
  restarted without a valid numeric recovery. Missing or non-numeric evidence
  invalidates fallback for the remainder of that continuous outage.
- Sub-five-minute coordinator refreshes no longer replace the HVAC thermal
  learner's eligible anchor, allowing stable five-minute samples to mature.
- Explain availability now requires both the configured AI task entity and the
  `ai_task.generate_data` action. Expired pauses report inactive while retaining
  their historical reason, assets, and expiry.

### 🔧 Improvements

- The Docker Home Assistant smoke test now waits on coordinator work and exits
  when its scenario completes instead of running until the fixed timeout.
- Pull-request CI now selects pytest, quality-scale, and validation jobs from
  the changed paths while retaining the full non-smoke CI set for pushes and
  manual Tests runs.

### 🔄 Other changes

- None

## 0.9.14 - 2026-08-24

### 🚧 Breaking changes

- **Precondition configured zones only** is now **Synchronise configured zone
  temperatures**. Daikin preconditioning always changes the main thermostat
  target; when synchronisation is enabled, it then applies the same target to
  configured zone thermostats. The former main-setpoint-preserving behavior is
  no longer supported.
- Removed the legacy **Minimum EV SOC** and **Maximum EV SOC** settings. EV
  charging now uses the live value of the mapped **Vehicle target SOC entity**
  directly; config-entry migration removes the obsolete stored options.

### ✨ New features

- None

### 🐛 Bug fixes

- Daikin HVAC control now confirms the main target before setting subordinate
  zone temperatures, allowing the device to refresh zone temperature bounds
  derived from the main setpoint. Target changes fail closed before takeover
  when a main or synchronized zone target cannot be captured for rollback.
- Disabling configured-zone temperature synchronisation now leaves climate-zone
  targets unchanged, and a failed synchronized takeover restores the captured
  main mode and target before reporting a successful rollback. When takeover
  starts from off, rollback also restores the active mode Daikin remembered for
  its next turn-on before switching it off again. Unresolved main state is
  retained for a later release retry, while a subsequent manual change to the
  main thermostat durably supersedes that snapshot before release and is left
  untouched across restarts. An originally-off thermostat is always commanded
  back off even if an earlier mode or target restoration step fails, and no new
  HVAC acquisition can clear an unresolved main-state snapshot before recovery.
  Main-target feedback published during a multi-call transaction is ignored
  only when it matches the pending planner or rollback target, so a different
  user setpoint still creates a manual override even while the scheduler guard
  is active. An off-to-active intermediate mode is accepted only during the
  adapter's explicit turn-on phase. The in-flight transaction, release, or
  safe-state recovery then stops restoring the main thermostat, durably
  preserves the user's mode and target, and restores only subordinate zones
  and automations. Unexpected mode transitions follow the same path. Pending
  zone feedback is also matched to the exact commanded or restored target;
  a different user zone target is durably removed from rollback ownership and
  remains untouched while the main thermostat, other zones, and automations
  return to their captured states. Manual supersession is rechecked after
  persistence and confirmation awaits and before automation rollback calls.
- Automatic control now retains a restart-resumable recovery handoff when setup
  or reload encounters a temporary pause that blocks every enabled control area.
  Once the pause clears, healthy validation can restore **Armed / Running**
  without requiring an operator toggle.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.9.13 - 2026-08-23

### 🚧 Breaking changes

- EV planning now uses the mapped vehicle target-SOC entity as its authoritative
  target. The obsolete fallback target option and `set_ev_target_soc` service
  are removed by config-entry migration. A configured legacy EV must map its
  vehicle target before that migration can complete.

### ✨ New features

- Climate settings now include **Precondition configured zones only**. When
  enabled, tariff preconditioning keeps main Daikin power and mode control but
  applies temperature targets only to configured zone climate entities.
- EV SOC gained per kWh is now calibrated automatically from completed charging
  sessions in Home Assistant Recorder. The configured estimate remains a
  conservative bootstrap value until at least one hour of clean history is
  available, and learned rates include a 10% readiness margin. A learned model
  is used only with the same charging sensor, SOC sensor, and configured charger
  power that produced it.

### 🐛 Bug fixes

- Continuous EV charging now remains committed after charging is observed, so
  tariff forecast revisions cannot split one continuous schedule into repeated
  short start/stop bursts. Configured maximum import prices remain authoritative.
- Stop-only EV plans remain constraint-valid when the current SOC is already
  above the authoritative vehicle target.

### 🔧 Improvements

- Planner refreshes no longer wait for slow device-service feedback. Device
  commands are serialized separately, newer plans replace queued stale work,
  and unload waits for any in-flight command to reach a safe persistence
  boundary.
- Recorder imports and retained planning evidence now use bounded, compacted
  history. EV history is queried in adaptive chunks, forecast and dry-run
  records are bucketed, and Home Assistant Store serialization runs off the
  event loop to reduce planner memory, database, and event-loop load.

### 🔄 Other changes

- None

## 0.9.12 - 2026-08-16

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Zone thermostat target changes now participate in manual-override detection
  without misclassifying changes protected by the configured scheduler guard.

### 🔧 Improvements

- Climate Zones can now include subordinate `climate` entities. Planner HVAC
  actions apply and confirm the main thermostat target on every configured zone
  thermostat while retaining switch/helper takeover and restoration behavior.

### 🔄 Other changes

- None

## 0.9.11 - 2026-08-15

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Manual safety-gate arming now requires the current preflight and reviewed
  production evidence to pass. The Armed entity reports effective command
  authority and exposes stale reviewed evidence as
  `production_evidence_contract_changed` instead of appearing armed while the
  executor rejects every command. Missing or unrecognized plan-health values
  also fail closed during preflight.
- Climate comfort handoff no longer calls the release adapter when Energy
  Planner owns no HVAC state. Release/no-op audit rows no longer consume the
  daily climate command allowance, so repeated replans cannot exhaust the cap
  before a real preconditioning start.
- A missed preconditioning start now uses the remaining contiguous lower-price
  window instead of abandoning the whole lifecycle. Catch-up does not cross
  tariff gaps or continue heating/cooling after the applicable comfort target
  is already reached, and peak-period load is projected from the temperature
  the shortened run can actually achieve.
- Degraded issues and scoped pauses are isolated by control area. An EV-only
  fault or pause no longer prevents an otherwise eligible climate action, while
  unsafe shared inputs and per-device capability/confidence checks still fail
  closed. Scoped pauses retain unaffected authority across restart and status
  reporting, and occupancy availability is scoped to climate control.
- Direct HVAC takeover may recover toward comfort from below the heating range
  or above the cooling range. Opposite-direction commands and targets outside
  the configured comfort bounds remain blocked.

### 🔧 Improvements

- **Current state** and **Next actions** now omit Climate, EV, or Enphase areas
  when their corresponding device-control switch is off. Disabled-area actions
  are also excluded from the Next actions attributes and action count.
- Plan calendar descriptions now group action evidence into short bulleted
  sections and render embedded schedule and forecast timestamps in Home
  Assistant's local timezone instead of exposing raw UTC ISO values.

### 🔄 Other changes

- None

## 0.9.10 - 2026-08-15

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Startup automatic-control recovery now runs as Home Assistant background work,
  preventing its bounded dependency wait from delaying bootstrap or producing a
  setup-timeout warning.
- Persistent notifications raised during startup are now deferred until Home
  Assistant reaches `RUNNING`. Recovered conditions cancel their queued alerts,
  and repeated alerts for the same condition are collapsed to the latest one.
- Startup safety validation now awaits a fresh non-debounced coordinator
  refresh. Unsafe startup recovery retries every 30 seconds, resets progress on
  any unhealthy check, and re-arms after three consecutive healthy checks.
  Whole-system shutdown preserves active ownership and EV capacity reservations;
  integration reload and removal continue to restore them.
- Explicit production-gate arming or disarming now cancels the superseded
  startup recovery and dismisses its stale warning. A failed
  configuration-reload handoff resumes disarmed safety recovery on the
  still-loaded coordinator.

### 🔧 Improvements

- Previously active installations now resume **Automatic control** and
  **Armed** immediately after restart. The ten-minute startup safety grace
  begins only when Home Assistant reaches `RUNNING`; ordinary runtime safety
  gates continue to apply throughout it.
- **Automatic control** now represents retained operator intent, while
  **Armed** remains the authoritative command-permission state. If the startup
  deadline is unsafe, Energy Planner disarms and restores its devices while
  leaving Automatic control on for automatic recovery.

### 🔄 Other changes

- None

## 0.9.9 - 2026-08-15

### 🚧 Breaking changes

- None

### ✨ New features

- Added bounded startup recovery for installations that were actively controlling
  devices before a production-evidence contract transition. Energy Planner waits
  up to ten minutes for required Home Assistant entities and services, retries
  safe-state restoration, and reactivates automatic control only after three
  consecutive non-commanding healthy plans five seconds apart. Recovery never
  overrides an operator stop, a configuration change, or a later runtime outage.

### 🐛 Bug fixes

- Recorder-facing sensor and binary-sensor attributes now use a shared 12 KiB
  byte budget with deterministic compaction, preventing oversized
  `next_actions` decision evidence from exceeding Home Assistant's 16 KiB
  state-attribute limit. Calendar event metadata is byte-bounded as well.
- The Plan calendar now omits actions and EV charging windows for device-control
  areas whose Climate control, EV control, or Enphase control switch is off.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.9.8 - 2026-08-13

### 🚧 Breaking changes

- None

### ✨ New features

- Added an opt-in **Precondition while away** Climate policy. It permits only
  the tariff preconditioning/coast lifecycle while occupancy is known to be
  away; ordinary HVAC-on commands remain blocked and comfort target bounds are
  still enforced immediately before execution. Moving from planner-owned
  away-off state into preconditioning also respects the configured HVAC
  minimum cycle/rest period.

### 🐛 Bug fixes

- When away preconditioning is enabled but its tariff window starts later, the
  planner now turns unowned HVAC off immediately and keeps the future
  preconditioning lifecycle. Previously the future action could suppress the
  immediate away-off action and leave HVAC running until the window began.
  Candidate selection now accounts for the rest period created by away-off,
  and existing away-off ownership cannot be released merely because the
  temperature reaches a comfort boundary while no tariff window qualifies.
- Forecast confidence is now evaluated by source and subsystem. Optional
  carbon or weather confidence no longer caps the required-source plan score or
  unrelated device actions, while climate actions still enforce weather,
  tariff, and load confidence independently and explain the limiting score. An
  active climate lifecycle is explicitly released if its relevant confidence
  later falls below threshold.
- Away HVAC-on commands must carry a complete, internally marked tariff
  lifecycle before the execution constraint can apply the away-preconditioning
  opt-in; a phase label alone is insufficient. Minimum-cycle continuation also
  requires the action to match the persisted lifecycle mode and timestamps.
  Malformed legacy away-off start evidence falls back to the valid takeover
  timestamp instead of indefinitely restarting the rest window.
- External Climate Manual Override helper changes now use the configured
  manual override duration instead of creating a permanent block. Legacy
  non-expiring helper overrides are bounded on startup and the helper is
  cleared when the timeout expires.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.9.7 - 2026-08-12

### 🚧 Breaking changes

- None

### ✨ New features

- Added a dedicated **Mode** enum sensor with stable `review` and `active`
  states, exposing the operational mode previously available only as an
  attribute of **Armed**.

### 🐛 Bug fixes

- **Explain** no longer misclassifies a normal absence of a worthwhile and
  thermally feasible climate-preconditioning window as a Climate settings
  fault. Climate recommendations now require specific input evidence.
- The **Load forecast coverage score** sensor now publishes the latest evaluated
  retraining score instead of appearing frozen on a retained model's older
  score. Its attributes distinguish the latest attempt from the active model.
- Device control selectors now turn off unconditionally before attempting a
  single serialized best-effort restore. An unavailable or unconfirmable device
  can retain recovery evidence and create a notification, but it can no longer
  force its control selector back on or permit new start/schedule commands after disable.
  Interrupted EV restores remain eligible only for bounded safe-stop recovery.
- A disconnected EV with inactive charging feedback and a confirmed-off
  persistent charger control is now accepted as safely stopped. Automatic
  safety-stop failures use ten-minute backoff and a maximum of three attempts
  per rolling day instead of repeatedly pressing a momentary Stop control.
  Dedicated persisted retry timestamps prevent unrelated asset pauses or audit
  rotation from erasing those limits.
- Climate takeover now explicitly cancels in-flight climate automation actions,
  confirms thermostat turn-on and mode before sending target temperature, and
  reasserts the full target once when an already-running schedule overwrites
  preconditioning.
- Renamed the guard setting to **Climate Scheduler Guard Timer** and added an
  explanatory field description consistent with the other Climate settings.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.9.6 - 2026-08-12

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Added a paired climate scheduler guard timer mapping. When the external
  schedule-versus-manual classifier helpers are configured, Energy Planner now
  starts and confirms the timer and scheduler-change boolean before any HVAC
  takeover, climate command, zone command, rollback, or release. Missing or
  unavailable guard state blocks the actuator path so planner activity cannot
  be misclassified as a manual override.

### 🔧 Improvements

- Retired the **Pause control 1 hour** and **Pause control 4 hours** buttons.
  The flexible `ha_energy_planner.pause_control` service remains available for
  automations.
- Retired the duplicate **Keep charger on** switch while preserving **Keep
  charger enabled after target SOC** in EV settings and retaining its stored
  value during upgrade.

### 🔄 Other changes

- None

## 0.9.5 - 2026-08-11

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Removed the obsolete Ready by, Opportunistic charging, and Opportunistic
  charging price threshold entities during setup now that those controls live
  in EV settings. Existing option values are preserved.
- Renamed **Explain or troubleshoot** to **Explain** and made every button press
  publish a Home Assistant notification, including not-ready and rate-limit
  rejections, immediate pending feedback, and the accepted, rejected, or failed
  result.
- Moved **Climate control**, **EV control**, and **Enphase control** from the
  Configuration grouping into the device's Controls section alongside
  **Automatic control**.
- **Run safety check** now always publishes its result as a Home Assistant
  notification, including an explicit success response when every check passes.
- Replaced the misleading short **Schedule EV charging** calendar entry with
  complete contiguous **EV: Charging window** events derived from allocated
  slots. Each event now shows its actual start, stop, power, and estimated
  energy in Home Assistant's local timezone; no event is created when no
  charging was allocated, and malformed slots cannot break the calendar.
- Prevented momentary EV start/stop oscillation when a charger service times out
  after accepting a command. Energy Planner now waits for delayed charging
  feedback before deciding whether rollback is necessary, so a confirmed late
  start is not immediately followed by Stop charging.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.9.4 - 2026-08-11

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Household-load conservative bounds now calibrate correlated 15-minute
  residuals as day-level blocks using finite-sample upper quantiles. This keeps
  the existing 90% safety gate while preventing a small number of historical
  days from producing an under-sized uncertainty buffer. The model and forecast
  contracts are version 3 and 4 respectively, so existing models retrain and
  production review evidence is intentionally renewed after upgrade.
- Added a diagnostic Load forecast coverage score sensor with the current
  percentage, required threshold, model status, and combined bypass state.

### 🔧 Improvements

- Consolidated the settings form from thirteen menus into six task-oriented
  sections. EV charging now contains its entity mappings, Ready by,
  Opportunistic charging, opportunistic price threshold, Keep charger on, and
  charging policy in one place; Climate, Enphase, Energy, Planning, and
  Safety/troubleshooting settings are similarly grouped.
- Added a default-off **Bypass safety gates** option. When explicitly enabled,
  it waives the household-load 90% coverage gate, production preflight checks,
  and dry-run evidence checks so Automatic control can arm. Device selection,
  command execution, service errors, and feedback confirmation remain visible
  and enforced.

### 🔄 Other changes

- None

## 0.9.3 - 2026-08-11

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Upgrades now persistently remove every retired optimizer config alias and
  stored run/action metadata, including entries that have no legacy
  subentries, and dismiss fallback notifications created by older releases.
- Removed the retired optimizer fields from runtime planning models, replay
  contracts, newly serialized actions, and the normative architecture spec.
- Forecast models with recurring clock-time gaps no longer report ready unless
  every production bucket can be resolved within the bounded interpolation
  policy.
- A mapped household-load entity that has not yet been restored during Home
  Assistant startup no longer records a failed training attempt or starts the
  six-hour retry backoff. Energy Planner remains fail-closed and retries on the
  next refresh after the source appears.

### 🔧 Improvements

- Household-load training now accepts local days with at least 80% valid
  cleaned 15-minute buckets. Bounded historical negative readings, excluded EV
  charging, and brief source outages no longer discard an otherwise usable
  day. The model and forecast contracts are version 2 and 3 respectively, so
  existing models retrain and production review evidence is intentionally
  renewed after upgrade.
- Removed HAEO and HAFO support, including optimizer configuration, service
  calls, response parsing, planner dependencies, telemetry, fixtures, and
  validation tooling. Legacy optimizer settings are discarded during migration.
- Preflight readiness continues to require only the individual EV, Climate,
  and Enphase control surfaces that are enabled; configured surfaces may remain
  off without blocking the enabled areas.

### 🔄 Other changes

- None

## 0.9.2 - 2026-08-09

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- None

### 🔧 Improvements

- Reduced the built-in household-load readiness minimum from seven to three
  complete days while retaining two leakage-free validation origins, the
  persistence comparison, and conservative-bound coverage gate. Production
  profiles still require three clean observations per clock bucket. The load
  forecast contract is version 2, so upgrading retrains the model and requires
  fresh review evidence before automatic control can be re-armed.
- Made the existing three-observed-day EV trip-history minimum explicit. Until
  that history is available, EV planning continues to use the configured
  fallback Target SOC.

### 🔄 Other changes

- None

## 0.9.1 - 2026-08-09

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Household-load conservative bounds now protect HAEO-free grid-import checks,
  15-minute profiles are time-weighted into every configured planning interval,
  and live HVAC subtraction normalizes W/kW/MW before clamping at zero. Calendar
  and Next actions evidence is aligned to each action time, and only fully
  observed days satisfy the seven-complete-day readiness gate.
- Conservative load/PV uncertainty now remains part of the grid-import safety
  check when optional HAEO grid evidence is present. Recorder training queries
  adaptive UTC-aligned chunks with a per-entity cardinality limit, exposes retained
  failed-training evidence and source identity, and notifies for model-quality
  failure only after the model has remained unusable for 72 hours.
- Built-in models aged 24–72 hours remain visibly degraded and reduce load
  confidence, but no longer trip the integration-wide unsafe-input command gate;
  learning, stale, and failed models continue to block active commands.
- Recorder power states are normalized using their historical W/kW/MW unit
  attributes, preventing source unit changes from reinterpreting older readings.
- A household-load mapping or forecast-contract change now restores safe state
  and explicitly disarms production control before new dry-run review cycles,
  so the Armed entity cannot remain misleadingly on while commands are blocked.
- Core real-evidence export and Docker smoke validation no longer require an
  external household-load forecast or a configured HAEO service.
- Deterministic EV and Enphase fallback actions are no longer incorrectly
  marked as HAEO-dependent when current HAEO grid evidence was not used. Stale
  HAEO arbitrage evidence is ignored, while genuinely HAEO-derived actions keep
  the existing fail-closed execution gate.
- **Explain or troubleshoot** now requests Home Assistant AI Task structured
  output and keeps rejected results visible across materially equivalent plan
  refreshes instead of silently returning an empty result.

### 🔧 Improvements

- Replaced the required external baseline-load forecast with a built-in,
  deterministic household-load forecast trained from up to 28 days of Home
  Assistant Recorder history. Energy setup now requires a measured whole-home
  power sensor and keeps the external PV forecast. The model exposes expected
  and conservative load evidence through the existing status and calendar
  surfaces, handles weekday/weekend patterns and DST, and fails closed for
  active commands while learning, stale, or failed. HAFO is no longer required;
  HAEO remains optional. Existing measured-load mappings migrate automatically,
  while forecast-only legacy entries require the user to select a real sensor.
- Added individual **Climate control**, **EV control**, and **Enphase control**
  switches. **Automatic control** remains the master arm switch and now respects
  the selected device switches instead of enabling every configured area.
  Turning one device control off restores only that device while the other
  selected areas remain armed. A failed restore leaves the switch on and reports
  an actionable error; enabling an area while armed must pass preflight first.
  The evidence fingerprint now covers configured mappings and policy rather than
  runtime device selection. Existing installations must collect three new review
  plans and re-arm Automatic control once after upgrading.
- Removed the manual EV start/stop buttons, Target SOC number entity, and EV
  connected helper. The fallback Target SOC is now configured in the central
  **EV, battery and grid** section, while live connection state comes only from
  an optionally mapped vehicle or charger entity.
- **Next actions** now summarizes the next state of every configured controlled
  area in its state value, matching the combined **Current state** presentation.

### 🔄 Other changes

- None

## 0.9.0 - 2026-08-08

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Climate planning now continues evaluating forecast price rises when an
  unowned HVAC is at or beyond a comfort boundary. This allows preconditioning
  to be scheduled before the expensive period while preserving the immediate
  safety release when Energy Planner already owns climate control.
- Planned action windows now include an explicit date and use Home Assistant's
  configured local timezone, so tomorrow's preconditioning cannot appear to be
  a current or UTC-timed action.

### 🔧 Improvements

- Replaced repetitive user-facing confidence percentages with categorical data
  quality evidence. **Next actions** and calendar events now identify the weakest
  forecast source, affected entities, available coverage, and corrective action;
  the conservative numeric weight remains internal to safety gates.
- Consolidated the integration into one Energy Planner device containing every
  entity. Removed the Add device/config-subentry UI and moved Energy, Climate,
  Presence, Enphase, AI, and EV mappings into separate sections of the central
  **Configure** page. Existing mappings are migrated into the main config entry.
- Replaced generic AI advice with an on-demand **Explain or troubleshoot**
  button. Automatic background calls and the AI Enabled switch are removed. AI
  output is accepted only when it either says no action is needed or supplies a
  complete user action tied to current planner evidence: the affected entity or
  setting, problem, exact next step, expected benefit, and verification.
- Reduced persistent notifications to problems that normally require user
  action. Routine plan/configuration changes, successful safe-state restores,
  successful preflight checks, and safe HAEO fallback no longer notify. Repeated
  infeasible-EV and restore-failure alerts are deduplicated with stable IDs.
- Replaced the fragmented plan, health, forecast, cost, takeover, per-device
  state, and audit sensors with **Armed**, **Current state**, and **Next actions**.
  Existing registry entries for the retired status entities are removed during
  setup; this is intentionally a clean break without compatibility aliases.
- Added a read-only **Plan** calendar containing the upcoming controlled actions.
  Calendar descriptions and **Next actions** attributes explain the accepted
  decision, reasons, constraints, desired state, data quality, priority order,
  and constrained energy budget used to determine each action.
- Added one **Automatic control** switch that enables configured control areas,
  runs preflight, arms production control, and enters active mode as one guarded
  operation. Turning it off restores safe state and returns to review-only mode.
- Removed the old planner, dry-run, EV-control, climate-control,
  Enphase-control, and AI Enabled switches. **Automatic control** is now the only
  activation switch; obsolete registry entries are removed during setup.


- The combined activation refuses to bypass the existing dry-run evidence,
  current-plan, availability, capability, or pause checks. An early activation
  attempt safely starts review planning and reports healthy-plan progress.

### 🔄 Other changes

- The full Docker validation gate passes with `931` tests and `100%` coverage,
  plus Ruff, quality-scale, replay, schema, history, Home Assistant configuration,
  and Docker smoke checks.

## 0.8.5 - 2026-08-08

### 🚧 Breaking changes

- None

### ✨ New features

- Climate tariff control now follows a persisted precondition, peak-coast, and
  release lifecycle across the configured planning horizon. Heating can
  precondition to the high comfort target and cooling to the low target before
  an expensive period, then coast at the opposite comfort boundary.
- Configured climate zones are enabled during takeover and restored to their
  original states when control is released. An off climate entity is explicitly
  turned on before its operating mode and target are applied.
- Climate plan, timeline, audit, and diagnostics output now expose tariff-period
  boundaries, lifecycle phase, selected mode and targets, controlled zones, and
  thermal-feasibility evidence.

### 🐛 Bug fixes

- None

### 🔧 Improvements

- The manual-override helper is authoritative in both directions. Activating it
  immediately releases HVAC ownership, restores zones, and enables configured
  climate automations without affecting EV or Enphase ownership.
- Comfort-boundary breaches, missing lifecycle evidence, tariff changes, and
  peak completion release climate control safely and prevent repeated takeover
  during the same expensive period.
- Safety releases remain available after production disarm, dry-run enablement,
  degraded input health, climate-control disablement, or action-cap exhaustion.

### 🔄 Other changes

- The full Docker validation gate passes with `921` tests and `100%` coverage,
  plus Ruff, quality-scale, replay, schema, history, Home Assistant configuration,
  and Docker smoke checks.

## 0.8.4 - 2026-08-02

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Native HAEO response and flexible-projection capability gaps no longer raise
  recurring fallback notifications when the current deterministic plan remains
  healthy. The capability evidence remains available in plan diagnostics, and
  genuine HAEO service or solve failures still notify.

### 🔧 Improvements

- None

### 🔄 Other changes

- Ruff, quality-scale validation, Home Assistant configuration validation,
  replay and schema checks, and `859` tests at `100%` coverage passed.
- The Docker smoke test remains blocked by its pre-existing HVAC suppression
  restore assertion, which is unrelated to the notification filtering change.

## 0.8.3 - 2026-08-02

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- EV connector feedback now treats `SUSPENDED_EV` and `SUSPENDED_EVSE` as a
  connected session with no active power delivery. Momentary stop controls are
  not called when this feedback already proves charging is suspended, avoiding
  charger API rejections after the vehicle reaches its target SOC.
- The EV Stop charging button now uses a supported Material Design icon instead
  of rendering with a blank icon in Home Assistant.

### 🔧 Improvements

- None

### 🔄 Other changes

- Full Docker validation: `858 passed`, `100%` across `8,962` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.8.2 - 2026-08-01

### 🚧 Breaking changes

- None

### ✨ New features

- Added native EV-device entities for enabling opportunistic charging and
  setting its import-price threshold. Both controls persist their values and
  request an immediate replan.

### 🐛 Bug fixes

- Plan fallback notifications are no longer recreated on every refresh when
  their title, message, and reason codes have not changed. A cleared condition
  can still notify again if it later recurs.
- AI advice remains visible across regenerated plan IDs when the material plan
  is unchanged, reports pending work while a provider call is in flight or
  rate-limited, and retries automatically when the provider-call window opens.
- Cached advice now follows the coordinator's latest-accepted policy, supports
  legacy nested fingerprints, stays hidden when AI advice is disabled, and no
  longer adds regenerated plan IDs to recorder-facing attributes.

### 🔧 Improvements

- Reserved the Options UI for configuration and operating constraints. Settings
  backed by native Home Assistant entities are now omitted from Options while
  retaining their persisted values for upgrade compatibility.
- Enabled opportunistic EV charging before the configured earliest start when
  low-price charging is enabled and the current import price is at or below its
  threshold. Only the current interval bypasses the charging hours; later
  allocations remain inside the configured window and existing safety gates
  continue to apply.

### 🔄 Other changes

- Full Docker validation: `855 passed`, `100%` across `8,942` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.8.1 - 2026-07-31

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Energy Planner is classified as a service integration so configured entries
  appear on the main **Settings -> Devices & services -> Integrations** page
  instead of being grouped only with Helpers.

### 🔧 Improvements

- None

### 🔄 Other changes

- Full Docker validation: `837 passed`, `100%` across `8,768` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.8.0 - 2026-07-26

### 🚧 Breaking changes

- None

### ✨ New features

- Charging-state confirmation with bounded retries and a fail-closed timeout
  after direct EV start and stop commands.
- Optional external vehicle target-SOC input, a native connected helper, and a
  keep-charger-on policy for vehicle preconditioning after the charge target is
  reached.
- Multiple named Energy Planner entries so each EV has isolated entities,
  history, production arming, execution audit, and persisted planner state.

### 🐛 Bug fixes

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

### 🔧 Improvements

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

### 🔄 Other changes

- Full Docker validation: `823 passed`, `100%` across `8,708` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.7.0 - 2026-07-17

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Safe-state restoration retains ownership for each asset until its restore is
  confirmed, and entering disabled or dry-run mode restores planner takeovers.
- Manual HVAC detection now includes control-attribute changes without treating
  the planner's own in-flight setpoint changes as manual overrides.

### 🔧 Improvements

- Config-entry and subentry mapping changes now rebuild runtime listeners and
  entities, while failed platform unloads leave the coordinator operational.
- The manifest and quality evidence now describe Energy Planner as a
  single-entry calculated helper with a defensible Gold quality target.
- Explicit services and control buttons now raise translated Home Assistant
  errors when no runtime is loaded or a requested device action fails.

### 🔄 Other changes

- Full Docker validation: `683 passed`, `100%` across `7,672` statements, plus
  Ruff, replay, schema, history, Home Assistant configuration, and end-to-end
  smoke checks.

## 0.6.1 - 2026-07-17

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Dry-run comparison sensor attributes now publish compact recorder-safe
  summaries instead of repeating nested execution evidence that could exceed
  Home Assistant's 16,384-byte state-attribute limit.

### 🔧 Improvements

- None

### 🔄 Other changes

- Full Docker validation: `663 passed`, `100%` across `7,548` statements, plus
  replay, schema, history, Home Assistant configuration, and end-to-end smoke
  checks.

## 0.6.0 - 2026-07-15

### 🚧 Breaking changes

- None

### ✨ New features

- Native EV smart charging, removing the requirement to install the separate EV Smart Charging integration.
- Direct charger switch or separate start/stop control, native Target SOC and Ready by entities, and manual Start charging/Stop charging buttons.
- Continuous least-cost charging windows by default, optional interval scheduling, earliest-start limits, maximum import-price filtering, low-price immediate charging, and minimum-SOC immediate recovery.
- A persistent `ha_energy_planner.set_ev_target_soc` service alongside the now-persistent ready-by service.

### 🐛 Bug fixes

- None

### 🔧 Improvements

- EV schedule actions now decide whether the charger must be on in the current planning interval; they no longer delegate schedule execution to an external integration.
- No-op charger decisions are audited as skipped and do not consume daily command caps or command cooldowns.
- New EV configurations expose direct charger controls only. Existing `ev_smart_charging_*` controls and helper-backed entries remain readable for migration compatibility.

### 🔄 Other changes

- Full Docker validation: `662 passed`, `100%` across `7,536` statements, plus replay, schema, history,
  Home Assistant configuration, and end-to-end smoke checks.

## 0.5.2 - 2026-07-12

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- None

### 🔧 Improvements

- The AI-provider privacy notice is logged at informational level instead of surfacing as a Home Assistant warning.
- The plan-fallback notification toggle now uses the same heading-and-description style as the surrounding safety controls.

### 🔄 Other changes

- None

## 0.5.1 - 2026-07-12

### 🚧 Breaking changes

- None

### ✨ New features

- An **AI and safety** option to disable and dismiss recurring unsafe-input, grid-limit, and HAEO fallback notifications without weakening fail-closed behavior.

### 🐛 Bug fixes

- None

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.5.0 - 2026-07-12

### 🚧 Breaking changes

- Existing configured planning horizons are preserved; review horizons above 12 hours against the actual Amber coverage available at your site.
- Configure the optional secondary PV entity only when it exposes timezone-aware timestamps. Solcast tomorrow data can then extend the today forecast safely.
- Production evidence is tied to the mapped control surfaces and decision policy. Relevant configuration changes require new healthy dry-run evidence before active commands resume.
- Legacy thermal and forecast-calibration statistics are migrated or reset before they can influence planning.
- AI provider integrations may log prompts independently; review provider logging settings before enabling advisory features.

### ✨ New features

- Optional second PV forecast input with timestamp-safe Today/Tomorrow stitching.
- Per-input forecast coverage diagnostics and bounded conservative baseline-load leading-gap fill.
- Refresh-trigger, phase-timing, retention, HAEO-evidence, and usable-horizon diagnostics.
- Versioned thermal learning, production evidence contracts, fresh-plan activation checks, and shared fail-closed pause parsing.

### 🐛 Bug fixes

- Dry-run actions are recorded as skipped instead of rejected, while repeated dry-run evidence is coalesced without hiding real command attempts.
- HAEO is ready only when response-capable services return continuous import and export evidence across enough solve slots.
- Planner-owned device feedback is suppressed only when a successful command matches the observed state.
- Stale AI results, stale plans, active pauses, changed control contracts, and missing or malformed production state now fail closed.
- Corrupt thermal, calibration, retention, pause, boolean, and evidence-counter state is reset, filtered, or blocked safely.

### 🔧 Improvements

- The recommended default planning horizon is now 12 hours. Continuous forecast coverage is healthy at 12 hours, degraded from 8 to under 12 hours, and unsafe below 8 hours.
- Required point-only inputs no longer masquerade as full forecasts; secondary PV stitching requires timezone-aware timestamps and does not calibrate slots without primary-source provenance.
- Replanning uses an explicit decision-input allowlist, one-minute non-manual floor, coalescing, and stable input fingerprints.
- AI advice runs after plan commit as a cancellable single-flight task and is published only for the current safe plan.
- Thermal learning uses explicit HVAC mode/power evidence, minimum sample spacing, plausible-rate gates, and bounded robust medians.
- Forecast calibration and retained planner evidence use bounded, migration-safe, time-aware storage.

### 🔄 Other changes

- Dockerized pytest: `647 passed`
- Coverage: `100%` across `7,314` statements
- Replay, live-schema, real-history, quality-scale, Home Assistant `check_config`, and Docker smoke validation

## 0.4.0 - 2026-07-12

### 🚧 Breaking changes

- None

### ✨ New features

- Optional grid carbon-intensity forecasts with carbon-aware EV slot allocation and action scoring.
- Conservative PV lower bounds and load upper bounds learned independently per forecast lead time.
- Refresh, HAEO latency/cache/capability, calibration, uncertainty, and cost-horizon telemetry.

### 🐛 Bug fixes

- Carbon priority no longer contributes an unconditional zero score.
- Solar-flexibility and battery-safety decisions now use conservative learned forecast bounds while cost estimates retain expected values.

### 🔧 Improvements

- EV ready-by deadlines now use the Home Assistant timezone, handle DST gaps/rollovers, and preserve an absolute UTC deadline.
- HVAC lookahead and preconditioning windows now use elapsed time instead of assuming five-minute slots.
- Forecast training retains dense near-term evidence and sparse samples across the full configured horizon.
- HAEO calls detect response/flexible-load capabilities, skip unsupported second passes, cache equivalent short-lived solves, and fail closed on ambiguous native config entries.
- Production preflight now requires only configured and enabled control areas, allowing safe partial installations.
- Monetary forecasts use Home Assistant's configured currency and expose the actual priced horizon.

### 🔄 Other changes

- None

## 0.3.0 - 2026-07-12

### 🚧 Breaking changes

- To enable forecast calibration, configure separate **Observed PV power** and **Observed baseline load power** sensors in the Energy subentry. Do not select the forecast sensors themselves.
- Existing pre-0.3 calibration state is ignored until the new timestamped per-lead model has enough holdout-validated evidence.
- Required forecast sources should cover the complete planning horizon; incomplete horizons now mark planning inputs unsafe.

### ✨ New features

- Optional measured PV and household-load power inputs for time-aligned forecast calibration.
- Independent 30-minute lead-time calibration models with robust median fitting and later holdout validation.
- Rolling-origin PV/load forecast accuracy validation with MAE and RMSE by near, day, and long horizon.
- Persistence-baseline gates for exported real forecast evidence.

### 🐛 Bug fixes

- Prevented forecast calibration from treating forecast entity states as measured ground truth.
- Prevented overdue forecasts from being paired with one current observation after downtime.
- Prevented correlated refresh snapshots and near-term bias from leaking calibration into unvalidated day-ahead slots.
- Prevented partial HAEO grid-flow evidence from suppressing fallback cost calculation.
- Prevented timestamp gaps inside a forecast from being silently forward-filled.

### 🔧 Improvements

- Successful flexible-load HAEO results now regenerate the final plan instead of only updating stored evidence.
- Forecast confidence now accounts for actual horizon coverage.
- Required forecasts with missing or internally gapped coverage fail closed instead of repeating the last value.
- Estimated daily cost now uses HAEO grid flows where complete and battery charge/discharge evidence otherwise.
- Forecast attribute changes, including canonical camelCase variants, trigger replanning without reacting to unrelated metadata churn.

### 🔄 Other changes

- Dockerized pytest: `519 passed`
- Coverage: `100%` across `6,188` statements
- Replay, live-schema, rolling forecast-accuracy, Home Assistant `check_config`, quality-scale, and Docker smoke validation

## 0.2.1 - 2026-07-07

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Removed duplicated device names from AI, EV, Climate, and Enphase switch labels.
- Added a one-time entity registry cleanup for duplicated entity IDs generated by earlier labels.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## 0.2.0 - 2026-07-06

### 🚧 Breaking changes

- Review the new **EV, battery, and grid** policy options after upgrade:
  - usable battery capacity
  - battery round-trip efficiency
  - maximum battery charge power
  - maximum battery discharge power
- Review the new **Data health** confidence thresholds. The defaults are intentionally conservative.
- Check the new Decision, Decision audit, Rejected actions, and Upcoming timeline entities before arming production control.
- Run preflight and allow several healthy dry-run cycles before enabling active device control.

### ✨ New features

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

### 🐛 Bug fixes

- None

### 🔧 Improvements

- Enphase decisions no longer rely on simple tariff spread alone. Battery and solar value now need enough usable capacity and configured savings value.
- EV charging plans prefer lower effective-cost windows and include solar/grid split details.
- Plan attributes use more plain-English summaries for accepted and rejected decisions.
- The production safety model now records clearer reasons when control is paused by failures or conflicts.

### 🔄 Other changes

- `ruff check custom_components/ha_energy_planner tests`
- Dockerized pytest and coverage: `492 passed`, `100%` coverage
- Translation JSON validation
