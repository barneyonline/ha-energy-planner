# Climate decision policy

Energy Planner can compare a learned prediction of normal climate operation with
preheating or precooling. The comparison includes HVAC electrical energy, tariff
prices, lost export revenue, solar, the selected EV schedule and supported battery
behaviour. A tariff difference alone is not reported as a monetary saving.

## Policies and activation

- **Automatic** (default): retain legacy behaviour while learning and collect passive history;
  activate economic decisions only after validation passes. Intentional observation
  periods require a validated mode, a complete baseline prediction and an eligible
  economic comparison. Only distinct eligible opportunities advance the configured
  cadence (default every tenth), with at most one observation per local day.
- **Observe**: compare without commands from the economic engine. Legacy control
  remains available before the economic engine has ever activated.
- **Legacy**: use the existing tariff-window policy.

Automatic activation does not enable Automatic control, change Armed, or bypass
manual overrides, confidence, minimum-cycle or rollback checks. After economic
activation, degraded evidence returns control to normal climate automations.
Select Legacy explicitly to restore the old policy.

### Direction permission and fallback evidence

**Preconditioning mode** is separate from the Automatic/Observe/Legacy decision
policy. Its default, **Automatic**, permits either direction. **Cooling only**
and **Heating only** restrict acquisition by both tariff and economic planning,
revalidation of an incumbent economic schedule and execution of an older queued
action. An incompatible owned cycle releases through the existing restoration
transaction. A safe stop or restoration to the original user/automation baseline
is not a new preconditioning acquisition and remains allowed. These restrictions
do not change the room goals or equipment offsets and do not disable external
Home Assistant climate automations.

For mapped rooms, tariff fallback never treats the current equipment mode as
evidence of future demand. New heating acquisition, whether the head is running
or idle, requires every mapped participating room to need heat: a current reading below
its lower comfort boundary, or a recent qualified passive trend predicting a
crossing of that boundary by the proposed peak. A single colder bedroom does not
justify raising the other comfortable rooms to their upper boundary. Conflicting
needs leave normal comfort controls in charge; no acquisition is preferable to an
unsupported whole-unit intervention. Cooling is supported by a room above its
upper boundary or a qualified trend predicting that crossing. Cooling only also
explicitly permits preparation toward the lower room goal while the head is idle.

Direction inference uses only source/mode/regime-compatible passive evidence that
already qualifies for temperature learning. In addition, it requires 12 accepted
windows covering at least 60 minutes within the preceding two hours, with the
latest at most 30 minutes old. It takes the median of those recent windows; an
overnight median, stale observations, old whole-degree steps or cold outdoor air
alone cannot establish daytime heating need. Disabling temperature learning
disables this predictive path. Actual room-boundary demand remains available.
Outdoor temperature differences never supply a synthetic indoor drift or coast
prediction for mapped rooms; missing per-room evidence stays unknown. Weather still participates in validated
economic simulation; its savings/recovery/readiness requirements are unchanged.
Installations without mapped room inputs retain weather/temperature direction
inference, without a current-equipment-mode fallback. Explicit direction
restrictions still apply. Humidity,
presence or comfort-only zone mappings do not count as room temperature inputs:
Daikin-based heating remains eligible through execution. An explicitly mapped
temperature sensor that loses its reading still blocks new acquisition.

Preconditioning diagnostics expose `direction`: the permission, chosen mode,
reason, observed equipment mode, evaluated peak time and per-room measured
temperatures, bounds, usable recent passive rates
and projected peak temperatures. Safety blockers such as low confidence, manual
HVAC overrides, away policy and missing comfort inputs take precedence over a
no-demand explanation, including during independent-zone fallback. A missing
projection remains unknown. These
linear direction checks do not establish predicted finishing temperatures,
economic readiness, measured energy or savings. Queued fallback heating and
cooling acquisitions recheck current room demand immediately before execution.
A confirmed continuation of the same owned lifecycle retains its completion and
comfort rules; it does not reacquire merely because a tariff refresh occurred.

See the [26 October all-input assessment](hvac-demand-assessment-2026-10-26.md)
for the mode-priority defect, independent recommendation and replay evidence.

### 24 October 2026 decision assessment

The deployed 1.4.6 fallback selected heat to 24°C at noon before a 16:00 peak.
Main was 22.18°C and Bedrooms 21.84°C within the 21–24°C band. Between 09:00 and
12:00, 15-minute sampled room history showed robust trends of about −0.05°C/hour
for each room. Extending that rate to 16:00 would leave them about 21.98°C and
21.63°C. This is an illustrative extrapolation, not a validated counterfactual:
the exact hourly forecast at decision time was not retained, daytime conditions
can change, and HVAC electrical power was estimated rather than measured. The
economic model had no qualified validation windows. A tariff spread and colder
outside air did not establish that heating comfortable rooms to 24°C was useful.
Observed heating coincided with Main reaching 24°C at 13:15 and 24.62°C at 13:41.

The later 15:50 snapshot was different: Main was 22.43°C and Bedrooms 20.32°C.
Some bedroom warming was reasonable comfort control; that snapshot did not prove
that both rooms should be preheated to 24°C. The new fallback leaves that conflict
with normal controls. The 14:41 cooling snapshot (Main 23.70°C, Bedrooms 20.12°C)
also leaves conflicting needs with normal controls for a new Automatic acquisition;
already-running cooling is not evidence for a fresh tariff takeover. Explicit
Cooling only remains an operator-selected preparation policy. `tests/test_hvac_direction.py`
replays these sanitized inputs with explicitly synthetic forecasts and tariffs.
Neither these tests nor the historical observations prove a full uninterrupted
cooling outcome or a monetary saving.

Readiness is separate for heating and cooling. It requires 14 days of usable
normal-operation history, 20 complete chronological validation windows, five
active episodes, temperature MAE no greater than 0.5°C, temperature 90th-percentile
error no greater than 1°C, energy error no greater than 20%, state accuracy of at
least 90% and active recall of at least 80%. Two consecutive successful daily
validations are required. Missing live evidence removes authority immediately;
two failed daily validations or 14 days without an actual qualifying validation
window revoke it. Restoring a missing live input requires two new successful daily
validations before reactivation. Each mapped room must also pass its own
chronological validation; whole-home accuracy cannot qualify an inaccurate room.
These engineering thresholds are not guarantees of future performance.

## Zone feedback and learned timing

Configure → **Zone temperatures** selects one temperature sensor for each configured
zone, preserving other room mappings. Existing average helpers can represent a
shared zone. Selecting a sensor does not enable the zone. Celsius and Fahrenheit
readings use the existing freshness policy. Missing or stale explicitly mapped
readings block acquisition and safely release shared-unit ownership; they never
silently fall back to the Daikin temperature. Unmapped installations retain their
existing main-unit feedback. Explicit room comfort helpers must also be available
and ordered correctly; a failed helper cannot inherit household bounds. Invalid
explicit mappings block shared-unit control.

Cooling follows the warmest mapped room and heating the coldest. All mapped rooms
must reach their applicable target before early coast. Opposite comfort limits
remain protected before acquisition and during preparation, and coast hands back
at the comfort boundary. Qualified zero-duration preparation does not start a cycle.

Temperature learning is enabled by default, independently of economic-model
readiness. Separate source-specific heating, cooling and passive trends use
non-overlapping windows of 5–30 minutes, accumulating coarse readings rather than
turning each whole-degree step into a five-minute rate. Flat and adverse trends
are retained. Transitions, sensor failures, interventions and uncertain commands
break the observation interval. Rates use distinct sensor-report timestamps; an old
reading from before the observed operating regime cannot become a recent change.
Each mode needs 12 accepted windows covering at
least 60 minutes from the last 14 days. Individual dated windows and measured-load
evidence expire, so one new observation cannot revive old readiness.
Sensor or topology changes reset incompatible statistics. Static source identity is separate
from transient zone power and transition timestamps. Normal zone closures/reopens
and reloads preserve compatible history. Each dated window is tagged with its stable
physical airflow/equipment regime (including head and active zone setpoints); only
matching-regime windows qualify the current source/mode. Future preparation selects
structured evidence for the exact proposed sensor, controller, topology, open subset,
mode and head/active-zone equipment targets, using the same zone-control intent as
the proposed command. It can use qualified retained windows while the head is off,
without starting equipment to establish compatibility. Economic candidates use their
own proposed target; temperature qualification does not authorize an economic model
or measured-energy claim. Opaque legacy windows remain retained and usable for
current-regime reporting, but cannot qualify a future regime until 12 structured
matching windows covering 60 minutes have accumulated. Unknown topology, blocked
airflow intent, missing metadata and incompatible targets use configured fallback. A transition timestamp
breaks a cross-transition interval, including a close/reopen between samples, but
never creates a new storage identity. Each source/mode retains at most 96 windows
across regimes, not an unbounded list of regime models. Measured-load evidence
also selects the matching regime. Missing matching evidence uses fallback timing;
no readiness threshold is lowered. Old transient-key evidence cannot be attributed
safely to the new regime keys and requalifies once on upgrade. Changing one room's
sensor does not erase other rooms' evidence; shared topology/controller changes
invalidate incompatible shared airflow evidence.

**Base preconditioning lead** accepts **0–360 minutes** and defaults to **90 minutes**.
Existing saved values are preserved. The base is the fallback duration when the
relevant heating/cooling rates are not ready or temperature learning is disabled.
Qualified rates can extend preparation up to **Maximum learned preconditioning lead**
(default **180 minutes**, configurable up to **360 minutes**). The maximum must be
at least the base. Raising only the maximum does not lengthen the fallback.
Zero base lead disables preconditioning.

For a four-hour fallback, set the base to **240** and the learned maximum to
**240 or higher** in the normal Home Assistant options form. With adequate forecast
coverage and a qualified 16:00 tariff boundary, this permits a 12:00 start. Price,
minimum-cycle, comfort, ownership and restoration checks still apply; room targets
and independent-zone completion are unchanged. More available time does not prove
that a room will reach its target. Finishing temperatures remain unknown until the
relevant directional rates are ready, and estimated energy remains estimated.

Insufficient evidence uses the base lead. When a target needs longer than the
maximum, the fallback policy can use the remaining priced window and report its
predicted shortfall. Price qualification includes the same expanded window used
for candidate selection. Both search grids round within the hard maximum, even
when it is not a whole planning interval. Economic candidates use their own
preparation deadline rather than the far end of the forecast. Fallback coast
feasibility and maintenance-load projections use the first mapped room to exhaust
its own comfort reserve, including its own passive drift.
Economic candidates retain their savings
and recovery checks.
Learning does not manufacture compressor measurements: mode feedback and estimated
watts can describe net room response, but economic energy validation still requires
measured power. Disabling temperature learning retains qualified measured-load
estimates; electrical-load readiness is reported independently. Shared-unit load
evidence combines qualifying measurements from every mapped room, irrespective
of room order or availability. Duplicate timestamps count once, retained
measurements stay at least five minutes apart, and the shared series retains at
most 96 measurements from the last 14 days.
Decision diagnostics label current observed-regime readiness separately from retained
qualified regimes (counts, minutes, rates and structured-window counts). Candidate
timing reports its proposed conditions and compatible qualified rate separately,
with effective lead, learning rejections and finishing-temperature estimates.
Retained qualification alone does not prove compatibility with any particular
future candidate; it remains separate from electrical/economic readiness.

## Cooling equipment compensation

Configure → **Cooling equipment compensation** sets `cooling_equipment_offset_c`
within `hvac_zone_mappings` for each physical zone. Values are −2 to 0°C in 0.5°C
steps, default 0. The UI presents a paired zone once and updates its existing
switch/climate mapping aliases together, preserving sensors and other mappings.
Direct mapping edits must give both aliases the same offset if both specify one.
The form reads an existing offset from either alias, including when the switch has
other mappings but only the thermostat carries the offset. Accepting the displayed
setting preserves it. Calendar preparation titles show the room goal; action
sentences show both the room goal and the shared equipment setpoint.
Nonzero compensation requires the explicit independently controlled topology below.

Room targets and equipment targets have different jobs. With household low/high
comfort helpers at 21/24°C, Main −2 and Bedrooms 0 produce:

| Control | Cooling equipment target | Room completion target |
| --- | --- | --- |
| Main | 19°C | 21°C |
| Bedrooms | 21°C | 21°C |
| Shared head, both rooms active | 19°C | Each mapped room must complete |

The shared head follows the lowest active cooling equipment target so it can still
demand while the compensated room prepares. Every active zone target must fit the
head's ±2°C range. Incompatible combinations block control; offsets are never
silently rounded or clamped. The adapter requires live head mode availability,
finite ordered min/max bounds and a positive target step, confirms the head first,
then waits for each active zone's live range/step before sequential target writes.
It rechecks after persistence yields. Cooling mode availability is required before
activation; the cooling target range and step are checked after that mode is
confirmed, allowing a supported change from heating or an off head's remembered
mode. Unavailable or incompatible cooling limits still block the temperature
write and retain the original restoration baseline. A head whose mode or target feedback cannot
support the exact requested setpoints cannot acquire compensated control.

Room completion, 0.3°C restart hysteresis, ten-minute pause dwell, early coast and
comfort handoff continue to use room sensors and room targets. When Main completes,
its damper pauses; a still-active Bedrooms zone uses head/equipment 21°C. Paused,
manual or deferred hidden-target zones retain the existing command exclusions.
Missing sensor feedback fails closed. Original head/zone targets and powers remain
the restoration baseline across coast, failure, manual overrides and restart.
Heating is unchanged. Legacy and pre-activation Automatic/Observe fallback use the
same command path. The economic engine continues to refuse unvalidated independently
changing subsets; compensation does not qualify an economic model or imply savings.

The observed Daikin head temperature reaching 21°C while the living-area room sensor
remained warmer is consistent with a feedback mismatch, but does not establish
continuous compressor operation. HA `hvac_action` can be inferred from mode when
compressor-frequency feedback is absent. Neither compensation nor more preparation
time guarantees the room will reach 21°C. Installation, configuration, actual room
response, cycling, airflow and measured energy need separate physical verification.

## Optional inputs

Measured HVAC power is optional for setup but required to validate energy savings.
Missing optional measurements disable the associated capability; they do not
invent data. Unknown household-load cleaning provenance prevents optimisation,
because adding HVAC to a forecast that already includes it would double-count it.

An expected-arrival input accepts a date-and-time helper or absolute timestamp
sensor. It does not infer travel or store locations. Arrival preconditioning still
requires **Precondition while away**. Expired or cancelled arrivals invalidate
arrival-specific acquisition.

Room measurements are an object keyed by an existing configured zone entity:

```yaml
climate.living:
  temperature: sensor.living_temperature
  humidity: sensor.living_humidity
  presence: binary_sensor.living_occupied
  low: input_number.living_comfort_low
  high: input_number.living_comfort_high
  maximum_humidity: 60
```

Temperature, humidity, presence and comfort helpers are optional. Humidity ceilings
require usable observations and a validated humidity model. Shared ducted systems
use total measured HVAC power once, not once per room. Existing actuator
capabilities and restoration rules still determine what can be commanded.

Solar irradiance uses W/m². A forecast sensor may provide `issued_at` and a
`forecast` list of `valid_at` timestamps and `irradiance` values, also in W/m².
There is no extrapolation beyond supplied forecast coverage.

Equipment efficiency curves accept `heat` and/or `cool` lists:

```yaml
heat:
  - temperature: 0
    cop: 2.5
  - temperature: 10
    cop: 3.5
```

Temperatures must increase and COP values must be positive. Curves are used only
within their range and after calibration against electrical measurements; the
integration does not claim to measure COP.

## Decisions and observations

Candidates must preserve configured comfort and account for recovery within the
forecast horizon. They must finish within 0.25°C of the baseline temperature and
must not obtain apparent savings by leaving the battery depleted. The minimum
expected saving defaults to 0.25 in the tariff currency, with positive savings
also required under the conservative scenario. The existing preconditioning lead
time bounds run duration. Device-supported temperature increments are respected.

Every tenth distinct eligible opportunity is normally left to the existing
climate controls, at most once per day. Repeated refreshes do not increment the
opportunity counter. Manual interventions and missing measurements invalidate
observation evidence. Predictions are stored before the observation starts, and
energy comparisons integrate the actual forecast interval, including 15- and
30-minute planning slots. The cadence is configurable.

The climate plan's `economics` attributes show readiness blockers, selected
schedule, baseline and candidate costs, estimated savings and rejection reasons.
Savings are estimates against a counterfactual baseline; only actual temperature
and energy consumption are measured. Small forecast changes do not replace a
revalidated incumbent schedule. Safety and manual control take precedence.

Observation history is bounded to 60 days and comparison history to 100 entries.
Source or comfort-policy changes invalidate affected learning. Historical data
without trustworthy ownership provenance cannot establish normal behaviour.

Candidate generation is bounded before simulation. It covers each start in the
configured lead window and each supported target, then evenly samples duration
and release combinations across the remaining horizon. All generated candidates
receive full comfort, recovery and economic checks. The selected schedule is the
best eligible generated candidate, not a guaranteed global optimum across every
possible combination. If the mandatory start/target coverage cannot fit the
500,000 simulated-slot work budget, `search_work_limit` leaves normal controls in
charge. This avoids making the standard 12-hour horizon permanently ineligible.

Normal-operation neighbour lookup uses a 0.25°C input grid in both chronological
validation and planning. Lookup caching uses exactly those normalized inputs, so
candidate order cannot alter a prediction. Thermal trajectories retain their
continuous temperature values. Solar exposure is interpolated only between
timestamped forecast points, without extrapolation.

Retained schedules are simulated at their persisted phase timestamps, including
phase changes inside a new planning slot. Room trajectories must recover no later
than their own baseline, meet their applicable arrival deadlines and finish near
their baseline temperature. Both higher and lower demand scenarios are evaluated
because negative tariffs can reverse which energy outcome costs more.

Changing the planning horizon, interval or occupancy mapping resets learning
readiness. Switching to Legacy first restores any owned economic lifecycle.
Observation-only comparisons cannot remove legacy release actions. A changed
arrival invalidates a scheduled candidate; arrival after an early release remains
valid if it is inside the overall forecast horizon. Date-only helpers are ignored.

Cent-denominated tariff inputs are normalized to whole currency before computing
costs and savings. Diagnostics use the tariff's explicit currency or Home
Assistant's configured currency; they do not label whole-currency savings as cents.

Preconditioning and coast targets are aligned to the device's supported increment
and minimum/maximum temperature, while staying within configured comfort limits.
Coast predictions include thermostat demand when temperature reaches the selected
coast target; coasting does not imply that the compressor must remain off.

A failed restoration or an unrecognized owned economic lifecycle version forces
another safe release attempt before any new climate command can be selected.


## Why preconditioning did not run

Open **Decision summary → climate → preconditioning**. The same evidence is
included in downloaded diagnostics. `status` distinguishes `scheduled`, `running`,
`learning`, `observation`, `blocked`, `no_opportunity`, and `restoring`.
`summary` gives the explanation, `reason` is its stable code, `next_step` explains
what to check. When legacy planning has several blockers, confidence takes
precedence over manual and occupancy blockers in both the reason code and explanation.
`evaluated_at` and `next_start` show when it was assessed and
when the next selected preconditioning command is due. A scheduled command still
passes execution gates; it does not prove the heater or cooler ran. Running means
planner ownership, including coasting, rather than measured compressor activity.

Manual overrides, unknown occupancy, away policy, observation-only policy,
scheduled observation periods, disabled Climate control, review mode, and an
unarmed controller and final plan validation failures are distinguished from an uneconomic opportunity.
Missing thermostat or zone restoration targets explicitly block takeover. Economic
model readiness and validation progress remain alongside this status in the
climate attributes. Automatic policy can still use legacy tariff control while
its economic model is learning; learning alone does not prove climate is blocked.

For legacy tariff planning, `legacy_rejections` contains bounded counts and one
sample per rejection cause. Samples include actual and required price differences,
preparation minutes, coast hours, missing price timestamps, comfort temperatures,
and minimum-cycle or release-hold evidence. These describe candidates considered
in the current refresh, not independent faults or proof that every candidate
failed for the same reason. A selected catch-up window can coexist with rejection
samples for earlier, infeasible candidates. Thresholds and safety rules are unchanged.

`last_attempt` links a scheduled window to its execution result and reason.
Only a matching current-plan rejection changes the current status to blocked;
older attempts remain historical evidence. `last_missed_opportunity` retains the
most recent planned window that expired or was withdrawn without confirmed
applied preconditioning or an already-satisfied target, including original plan/action IDs, timestamps,
and its last execution result when available. Missing execution evidence is not
labelled a device failure. Committed ownership of the preconditioning phase also
prevents a false missed record after upgrade or a restart between ownership and
audit writes. Coasting ownership alone does not prove preconditioning ran.
A later successful retry of that same window clears
its missed record. Late outcomes still update a withdrawn window after a newer
plan is committed; they do not overwrite the newer pending window. Successful
other windows do not erase an earlier missed one.

The pending window and last missed window survive restart and normal audit
rotation. Only these two records are retained; this is not a complete event log.
The next committed plan records expiry or withdrawal. A window that was never
selected has no missed-window record: use the current reason and candidate
rejections instead. Historical records from before this feature cannot be
reconstructed. Pending restoration takes priority in the current status until
ownership recovery is resolved.

## Independent zone completion

This optional control supports two to eight independently switchable zones on one
registered controller. Configure `hvac_zone_control` in Climate settings only
after verifying the physical actuator/thermostat pairs and minimum airflow with
the manufacturer or installer. Home Assistant's entity registry must put the head,
switches and thermostats on the same device, integration platform and config
entry. Different device registrations, climate-only zones, missing controls,
ambiguous room mappings and incomplete configured-zone coverage are unsupported.
Names, common sensor mappings and zone floor area do not prove independence or
safe airflow. No settings or hardware capabilities are inferred.

For example, after the installer has confirmed that either zone alone supports
the unit's required airflow:

```json
{
  "pairs": {
    "switch.main": "climate.main_temperature",
    "switch.bedrooms": "climate.bedrooms_temperature"
  },
  "safe_open_sets": [
    ["switch.main", "switch.bedrooms"],
    ["switch.main"],
    ["switch.bedrooms"]
  ]
}
```

Each listed set is an **exact** permitted open combination. The full set must be
listed. Intermediate combinations during sequential writes also need verification
while the head is active. Omit unverified combinations; a requested subset without an exact match
hands control back safely. Do not copy the single-zone entries above as an airflow
assessment for your installation. All paired entities must be explicitly included
in Climate zones. Removing a zone from that list is an exclusion and invalidates
this complete topology declaration. Normal manual changes still supersede planner
control and initiate restoration; they are never treated as an invitation to
reopen an excluded zone.

Map a fresh temperature sensor to each physical zone's switch or thermostat in
Room measurements. If both aliases are mapped, their temperature sensor and
comfort bounds must agree. Two different switches sharing a sensor remain two
different physical zones. Room-specific low/high helpers take precedence over
household bounds. Independent preparation explicitly synchronizes the active
paired thermostats, with original targets captured before writes. A previously
off thermostat exposing no target retains the existing deferred-target rules.
There is no substitution from the head's temperature when room feedback is lost.

Cooling pauses a zone at or below its applicable target; heating pauses it at or
above target. Other rooms continue only when their remaining open combination is
verified safe. A paused zone can reopen **ten minutes after confirmed closure** and at least
**0.3°C** beyond its target in the direction requiring preparation. Safety-driven
closure is immediate; the dwell never forces a completed room to continue
conditioning. Confirmations and sequential writes keep the existing service
deadlines, action budgets and compressor start/rest guards. Each actual write
rechecks fresh room feedback. Failed or uncertain closures cause shared-unit
restoration, with ownership retained while any recovery remains unresolved.
Airflow-dependent writes require known on/off feedback from every paired damper;
an unavailable actuator is never counted as closed. Recovery stops demand and
waits for available feedback before restoring a safe original combination.
Queued refreshes retain the confirmed closure time, so they cannot shorten the
restart dwell using an older planning timestamp.

The last airflow path is never closed for completion. When all zones finish, the
head is confirmed off for coast while paths remain as they were. With verified
current airflow, restoration confirms the original head settings in place before
reopening paused controls and restoring their dependent targets. An already-off
head gets open paths before any active mode is restored. Original off baselines
are confirmed before closing their last path. Restoration confirms the original
head/zone power and automation baselines without cycling an already-correct head.
Original baselines are never replaced by a subset, pause, reopen or coast state.
An unresolved target retains its paired power baseline across retries and reloads.
Deferred retries keep that dependency even when the actuator already matches its
original power. They leave an already-off head off; when normal operation makes
recovery actionable, the saved pair can reopen, restore its target and return to
its original power. Recovery never acquires a paired actuator without a saved
power baseline.
If a manual head change supersedes its saved baseline while owned zones remain
pending, an already-active head permits recovery of those explicitly saved pairs.
That recovery sends no head commands and retains the user's mode and target.
Only unfinished pairs with saved targets inside their published bounds are
opened. Incompatible or invalid bounds keep that pair pending without switch
cycling or guard renewal, even while an eligible sibling makes progress. A target
already confirmed at its baseline does not require reopening its closed actuator.
Restoration that cannot establish a safe original open combination remains
pending. Manual supersession durably retires both paired snapshots before any
remaining restoration writes.
A manual change to the head or another zone leaves unrelated switch recovery
available, including during intent persistence and delayed readback. A manual
change to that exact zone still prevents its restoration.

Plan diagnostics `climate.economics.zone_control` and ownership
`hvac_control.zone_intent` expose active/paused/completed zones, mapped source
temperature and freshness, target, hysteresis, pause time, restart blocker and
topology limitations. Pending command/restoration evidence remains in the
existing ownership and climate audit. A room paused within the hysteresis margin
is still reported as paused; completion is not proof of a measured energy saving.

The current economic model is qualified for a fixed zone set. Independent subset
control therefore uses the existing eligible fallback lifecycle and refuses new
economic candidates or hands an owned economic cycle back. It does not inherit
old savings predictions or bypass the existing ever-active degradation guard.
Observe still permits fallback lifecycle releases. A release hold or away-off
ownership cannot authorize a new fallback after economic control has activated;
only an existing tariff preparation/coast lifecycle may continue. Explicit Legacy
policy retains its configured fallback behavior.
Thermal observations reset their source regime when physical zone power changes;
starts, stops and uncertain commands cannot train across that transition.
Finishing-temperature estimates require qualified structured observations compatible
with the explicitly proposed open-zone regime, even before physical acquisition. Configured fallback timing remains explicitly uncertain.

Repository tests cannot establish real damper movement, sufficient airflow,
upstream mode/target coupling or compressor behavior. Live acceptance requires
installer airflow evidence plus observed heating and cooling trials, independent
zone closures, bounded reopening, all-complete shutdown and exact restoration.
This feature does not change live settings during development.

## Completing a tariff cycle and restoration

Reaching the preconditioning target enters coast immediately. Before the original
peak starts the phase is `pre_peak_coast`; during it the phase is `peak_coast`.
Heating reduces demand to the lower comfort target; cooling mirrors this at the
upper target. A refresh or small temperature drift cannot restart preconditioning
within the same lifecycle. Minimum-cycle rules do not block this demand reduction.
The configured schedules remain suppressed with `stop_actions: true` until release.
Coasting lowers thermostat demand and does not promise zero compressor activity.
Heating hands control back at the lower comfort boundary; cooling at the upper
boundary. Temperatures outside the comfort band hand back control. Required evidence loss, safety failure and genuine manual intervention
also release control. Economic cycles revalidate the actual early coast timestamp,
current conditions and remaining savings before retaining ownership.

Original main mode, remembered active mode, target, zone targets, dampers and
individual automation states remain the restoration baseline. Restore the main
mode/target, wait for compatible zone bounds, restore and confirm each zone,
restore dampers, then return the original main power state. Service acceptance
never proves restoration. Shutdown cleanup runs even when a dependent zone fails
or an interrupted acquisition loses its scheduler guard. A genuine manual main
change still supersedes that cleanup.
Unresolved zones retain their main recovery context; retries restore unfinished
settings only. Deferred targets do not repeatedly toggle an already-off head or
arm guards. A legacy zone-only record cannot energize equipment: it waits until
normal operation exposes compatible bounds. Unknown recovery versions stay pending.
When authoritative main and zone targets already match the saved settings, recovery
confirms completion without waking the head. If the main still needs restoration,
zone snapshots remain pending until dependent changes and readback are complete.

Each issued command records its context, requested control values and deadline.
Fixture-supported remembered-mode, dynamic-bound and shutdown feedback can match
the command during confirmation and a bounded settling period of at most two
minutes. User attribution, incompatible targets and auxiliary setting changes
retain manual priority. A scheduler guard alone does not authorize arbitrary
changes. A head target command permits only a required clamp of a zone target
outside the new bounds; a compatible zone must retain its own target. Declared
tariff intervals also bound lifecycle identity, so filling a forecast gap does
not replace the same tariff opportunity. The retained 30 September 24°C-to-22°C
change remains an unexplained
conflict; neither its source nor the exact 1 October service exception is inferred.
Manual supersession retires the prior transaction's expectations immediately,
including during settling after command execution has finished. Explicit manual
override requests also retire prior expectations before releasing owned controls.
Historical outcomes remain diagnostic evidence and cannot revive retired
expectations in the live state listener.

## Coordinating other automations

The enabled-by-default **Climate control active** binary sensor normally has the
entity ID `binary_sensor.energy_planner_climate_control_active`. Use the entity
belonging to the planner entry that controls your unit; Home Assistant may rename
it if that ID is already taken. It is read-only and reports ownership independently
of Armed, Automatic control and whether the compressor is running.

It turns **On** when acquisition is recorded, before the first HVAC command, and
stays on through preconditioning, target-based coast, peak coast, away-off control,
uncertain commands and unresolved restoration. Persisted ownership keeps it on
after reload. It turns **Off** once climate ownership is released; a manual
override timer by itself does not count as planner ownership. Attributes expose
`phase`, `command_pending` and `restoration_pending` when that context is available.

For an ordinary HVAC automation, require this sensor to be explicitly **Off**
immediately before changing the main unit or its zones. Unknown or unavailable
must not authorize those writes. Check again after any delay. In a departure
automation that also controls lights or locks, guard only the HVAC branch:

```yaml
actions:
  - choose:
      - conditions:
          - condition: state
            entity_id: binary_sensor.energy_planner_climate_control_active
            state: "off"
        sequence:
          - action: climate.turn_off
            target:
              entity_id: climate.your_unit
  # Other departure actions can follow here.
```

The sensor does not edit existing automations and is a coordination signal rather
than an atomic lock. Keep deliberate manual changes and emergency shutdowns able
to take priority. If normal occupancy or comfort policy should resume after
release, an On-to-Off trigger can re-evaluate that policy using current conditions
and respecting manual overrides.

## Power provenance and validation diagnostics

**HVAC power source type** accepts `auto` (default), `measured` or `estimated`.
Auto leaves sources unknown unless physical provenance is established; watts and
`state_class: measurement` alone do not establish a meter. Known synthetic
sources such as fixed Powercalc remain estimated even when declared measured,
and diagnostics report the configuration conflict. An explicit measured declaration
can qualify an otherwise unknown physical source. No sensor mapping or meter
purchase is required for legacy preconditioning.

Provenance is attached to every new observation and model. Old observations
without provenance remain unknown. Source or provenance changes invalidate the
corresponding economic readiness, predictions and training authority while preserving actuator
ownership and unrelated history. Estimated/unknown power cannot establish energy
accuracy, active-power validation or measured savings. Validation thresholds and
chronological training remain unchanged.

Heating and cooling diagnostics show actual values beside required thresholds,
consecutive daily passes, never-qualified versus expired evidence, and bounded
counts of rejected candidate window starts: insufficient preceding history,
sampling gaps, intervention/washout, insufficient neighbours, unsupported thermal
conditions and invalid power provenance. Counts describe attempts, not manufactured
independent validation windows. Manual or incomplete observation comparisons remain
invalid and cannot create validation success or an additional observation hold.

The versioned climate audit is independent of the general execution audit: it
retains at most 100 records for 30 days across restart. Identical retries merge
first/last timestamps and occurrence counts. It contains lifecycle/transaction IDs,
service stages, expected and observed controls, sanitized exception categories and
override attribution; credentials, vendor response bodies and location history are
excluded. Decision summary and diagnostic exports include the lifecycle, transition
reason, heating/cooling/coasting explanation, unresolved settings and latest failure.

An accepted thermostat command can publish feedback after its five-second readback
timeout. Its exact command expectation remains valid within the original
two-minute settling window, including during a mode retry. This preserves planner
ownership and the original restoration baseline when delayed feedback matches.
Readback timeouts do not extend the deadline or suppress explicit user changes, different
targets, unrelated automation contexts, superseding commands or cancellation.

A confirmed main temperature command also permits its exact automatic zone bounds
clamp for 150 seconds after the first confirmation, capped at three minutes after
issuance. For example, a 24 C main target can cause a delayed 20 C zone to clamp to
22 C. A replay of the observed 121.4-second delay preserves the preconditioning
cycle and its restoration baseline. The extra 30 seconds accommodates polling jitter.
The allowance requires current head mode/target readback and matching current zone
controls; newer measurement-only updates do not invalidate it. Temporary missing
zone targets or unavailable states preserve the bounded evidence and may recover
to the exact clamp calculated from a finite command-time target or the owned zone
baseline. No target is invented when both are missing. Combined or split mode and
target feedback also requires a confirmed head/zone activation from the same
transaction. Either update order is supported, including an exact clamp while the
captured old mode is still reported. The related activation's parent context remains
valid for this bounded settling period; unrelated activations and contexts do not.
Later related off, conflicting mode or cross-transaction commands retire older
activation proof, including through zone-switch aliases. Bounded issue-order history
preserves intervening off commands if a later on is unconfirmed or fails; dispatch
errors cannot revive the retired proof. A later confirmed activation can provide
new proof within its own transaction, without extending the target deadline.
A temperature command alone cannot activate an off zone. Explicit
users, unrelated automation contexts, auxiliary changes and conflicting targets
retain manual priority, including attribution on newer current states.
Repeated confirmations, unrelated helper commands and no-op refreshes cannot renew
the allowance, and late confirmations cannot revive an expired expectation.

Recovery retries restore available unfinished dampers and automations independently
when zone targets remain hidden after confirmed main shutdown. Unavailable controls
retain ownership without restarting the main unit or repeatedly arming the guard.
Confirmed shutdown during acquisition rollback retains the same completed-step
marker. Ownership merges recovery progress only for the matching original baseline.
Outcome diagnostics preserve their existing fields and sanitized actuator readbacks
even after unrelated execution history rotates. Changing the power declaration
retires affected energy evidence while retaining unrelated observation history.
Learning-era opportunity counters are excluded when the first qualified observation
cadence begins; subsequent refreshes retain the qualified counter.

## Deployment acceptance

Prepare and run `scripts/docker-validate.sh` without skip flags before release.
Publishing and live deployment are separate actions. Before deployment, back up
the installed integration and relevant planner configuration/storage. Confirm the
loaded version and saved/runtime state, then observe a complete naturally eligible
heating cycle through coasting and restoration. Cooling acceptance uses deterministic
replay until a natural cooling opportunity occurs. On failure, retain transaction
evidence, restore through the supported ownership path and roll back if required;
do not clear ownership or override records to make the UI appear healthy.


## Stable timing, configuration edits and calendar evidence

Timestamped tariffs retain their native UTC boundaries, including source coverage expiry. Native detailed tariff blocks take precedence over already-resampled convenience forecasts. A refresh at 09:03, 09:08 or 09:14 does not move an unchanged 16:00 peak or its 14:30 preconditioning start. The current and final intervals can be shorter than the configured cadence; energy and thermal calculations use their actual durations. Extending EV coverage replaces the former partial tail with a newly averaged complete interval. UTC boundaries remain unambiguous through daylight-saving changes. Untimestamped forecast arrays retain their relative-time interpretation and cannot establish native tariff boundaries.

HVAC lead, learning and price-policy changes replan the affected lifecycle without restoring EV, profile or Export Limit ownership. Zone temperature/comfort mappings restore affected HVAC ownership, update input subscriptions and invalidate affected learned models through source identity, while unrelated controls continue. Physical actuator mappings and global safety changes retain full guarded recovery. Original restoration baselines, uncertain commands, manual overrides and shared power constraints remain authoritative.

The calendar shows preconditioning until its selected completion boundary and coast until the tariff period ends, rather than treating a command eligibility deadline as the operating end. Future phases are labeled planned; independent live feedback and committed ownership establish confirmed history. Release events are discrete actions. An occupancy-return handoff has its own reason instead of claiming an expensive tariff period ended.

Until active temperature rates qualify, diagnostics identify the configured fallback duration and report finishing temperatures as unknown. A target setting is an instruction, not a prediction. Passive drift readiness alone does not establish heating/cooling readiness or measured energy savings. Pending Export Limit requests explicitly say gateway confirmation is outstanding, independently of healthy planning inputs.

Regression evidence: tests/test_inputs.py (native grid and complete horizon), tests/test_planner.py (stable lifecycle, tariff times and integrated partial-slot energy), tests/test_calendar.py (phase duration, command detail and point releases), tests/test_coordinator.py (policy/mapping isolation), and the full scripts/docker-validate.sh gate. These tests do not establish deployed or physical-device performance.

Climate observation predictions persist each baseline interval's end, so measured comparisons integrate partial and split intervals without double-counting or dropping coverage. Older predictions retain their original fixed-cadence interpretation. PV calibration selects lead buckets by elapsed time, and sparse training snapshots still cover the full horizon. Daylight EV coverage checks interval adjacency; continuous charging credits only the actual capacity of a retained pre-window slot.

Forecast confidence weights coverage by actual duration rather than interval count, including point-only inputs. Load coverage diagnostics use the same elapsed durations. Calendar label upgrades retain a confirmed climate phase's existing identity (UID, confirmed start and location), while separate phase UIDs and discrete actions remain distinct.

Coverage classification sums elapsed durations before converting to hours. Exact eight- and twelve-hour windows qualify at their existing thresholds even when import/export tariff boundaries are a second apart; a window ending one second short still fails that threshold.
