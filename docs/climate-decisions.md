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
