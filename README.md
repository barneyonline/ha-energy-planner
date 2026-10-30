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

Tariff plans retain the provider's UTC boundaries across refreshes, including partial current intervals. Climate calendar entries show planned preconditioning and coast phases; command deadlines appear in their details. Base preconditioning lead accepts 0–360 minutes (default 90); a four-hour fallback uses base 240 and a learned maximum of at least 240. Raising the learned maximum alone does not extend the fallback. Zero base lead disables preconditioning. Finishing temperatures remain unknown until the relevant heating or cooling rate is ready. HVAC timing and room-feedback edits preserve unrelated controls; changing actuators or global safety settings retains guarded restoration.

**Preconditioning mode** separately permits Automatic, Cooling only or Heating only.
Cooling only keeps the configured room goal and equipment compensation while
preventing planner heating acquisition in both planning paths and at execution.
Automatic fallback with mapped room sensors requires actual room-boundary demand
or recent qualified passive room trends, regardless of the current equipment mode.
Cold weather alone cannot justify heating comfortable rooms. Outdoor temperatures
do not supply a fabricated indoor drift rate or coast prediction. Conflicting room needs remain with normal comfort controls. Original
state restoration and safe stops are still allowed. Zones with only humidity,
presence or comfort mappings retain Daikin temperature feedback. Diagnostics prioritise safety
blockers over no-demand explanations. See [climate decisions](docs/climate-decisions.md) and the
[26 October decision assessment](docs/hvac-demand-assessment-2026-10-26.md).

Future preconditioning windows stay pending when a tariff tail changes. Rescheduling updates the pending window; cancellation removes it. Neither is reported as a missed execution before the episode starts. Refresh also clears older replacement reports demonstrably recorded before their saved episode start, without claiming that preparation ran. Saved attempts at or after the start, or with unknown timing, remain preserved even when a planning snapshot predates them. Matching provisional ownership retains history until its command outcome arrives and remains unconfirmed throughout that wait. Attempt history survives rollback and reload before that outcome is saved.

Missed-preconditioning diagnostics reconcile a changed tariff tail with confirmed execution of the same preparation episode. Matching requires the same configuration, mode, target, peak and preparation deadline. Confirmed evidence retains its original execution time and window across overlapping replacements whose regenerated starts differ; a replacement plan alone does not prove success. Older history or coasting ownership without this correlation evidence retains strict lifecycle matching.

Independent room completion is available for explicitly paired switch zones with fresh room sensors, confirmed actuator states and installer-verified minimum-airflow combinations. A completed room pauses while remaining rooms prepare; reopening requires a 0.3°C temperature margin and ten minutes paused. The shared head stops with an airflow path retained when preparation completes. Unsupported installations keep the existing shared-unit handoff. See [zone topology and airflow requirements](docs/climate-decisions.md#independent-zone-completion) before enabling this optional control.

Configure → **Cooling equipment compensation** optionally offsets each physical zone's cooling equipment setpoint by −2 to 0°C, default 0. For a 21°C room goal, Main −2 commands Main equipment and the shared head to 19°C while Bedrooms 0 remains at 21°C. Room completion still uses 21°C; heating and household comfort helpers are unchanged. Paired aliases update together. Independent airflow proof and fresh room feedback remain required, and incompatible live mode, range or step feedback blocks control. This compensation is a configurable trial, not a guarantee of room temperature or savings. See [equipment compensation](docs/climate-decisions.md#cooling-equipment-compensation). Calendar preparation titles retain the room goal; action descriptions state the shared equipment target separately. Cooling target limits are checked after the requested mode is confirmed.

The configured HVAC minimum cycle/rest interval survives completed restoration and reload. A confirmed planner stop starts a fresh rest period before automatic restart or direction changes, including a new tariff lifecycle. Planning and the service boundary enforce it; target-only and zone-only continuations do not restart it. Safe stops, original-state restoration and manual handoff remain immediate. Unconfirmed mode commands block automatic starts until fresh feedback matches the requested mode or restoration confirms an explicit compensating mode command. An unchanged baseline or readback deadline alone cannot retire an uncertain write. Confirmed compensation reserves a conservative rest even when its baseline was already displayed. A persisted confirmation for the exact head command retires its older readback lease without repeating compensation or extending rest. Commands cancelled before dispatch do not create a rest period. Blocked fallback candidates release their projected HVAC load before EV scheduling.

Missing, unknown or unavailable head and zone feedback triggers a health refresh without starting or extending manual override. Availability recovery alone also leaves overrides unchanged, even when recovered settings differ across the gap. Diagnostics record these events as `availability_feedback`. Control still requires available feedback; uncertain commands and original restoration settings remain subject to reconciliation and retry checks. Observed control changes after recovery retain manual protection. During a gap, an attributed change between concrete control attribute values or an explicit manual-override helper/service request remains authoritative. A recovery with a fresh user context and a mode, zone-power or concrete setting change from the last observed valid state also takes priority, including during pending commands. Fresh user attribution can survive failed or intermediate unavailable samples before the actual control readback arrives. This records `manual_recovery`; inherited contexts and recovery without a known prior state cannot establish user intent.

During HVAC release, a disabled Daikin zone can stay off while recalling a different numeric target after the planner restores the head’s heating/cooling mode. The planner recognizes this only within 120 seconds of the issued restoration command, with captured off-zone settings and matching current head/zone feedback. Explicit user or automation contexts, conflicting changes and later commands remain authoritative, including when a later command fails. Recognition never changes saved restoration targets or renews on refresh; it does not establish physical control or energy savings.

Thermal learning retains bounded evidence through normal zone cycles and reloads. Transitions break observation windows; each airflow/equipment regime uses its own qualifying evidence. Future preparation can select retained evidence for the exact proposed sensor, controller, topology, open subset and equipment targets while the head is idle. Unknown or incompatible candidate evidence uses the configured lead; older opaque windows remain retained but need new structured observations before qualifying a future regime. Sensor changes invalidate the affected source; shared controller/topology changes invalidate the affected shared evidence.

For a matching partial-zone candidate, only rooms whose dampers will open need qualified active rates. Closed rooms contribute no active preparation time; their finishing temperatures remain unknown, separately from active-rate readiness. Unknown or blocked airflow intent still uses fallback timing.

Independent room control preserves the economic degradation guard: away-off ownership cannot start a new fallback cycle after economic control has activated. Existing tariff cycles can finish; explicit Legacy policy keeps its fallback behavior.

Unfinished room targets retain their paired actuator baselines through deferred recovery and reloads. Recovery under a manually controlled head waits for compatible room bounds before reopening a zone. Synchronized zone takeover now requires visible original targets and a matching active original head mode; hidden off-state targets and unproven mode changes are rejected before device writes. Later refreshes cannot adopt newly exposed mode memories as original settings. Incompatible targets after confirmed shutdown retain ownership and expose an operator recovery blocker without waking the head. See [HVAC restoration recovery](docs/hvac-restoration-recovery.md) for the conservative acquisition limitation and a bounded manual handoff for legacy pending records.

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

Energy Planner is configured through the UI; a `ha_energy_planner:` entry in `configuration.yaml` is unsupported.

On Home Assistant 2026.11 or newer, EV maximum import price and low-price charging threshold appear only when their respective toggles are enabled. Disabling a toggle retains its saved threshold for later use. Older supported versions continue to show both thresholds.

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
