# EV charging decisions

The EV planner evaluates physical charging schedules against the vehicle's mapped
SOC target and next local ready-by time. It extends requested input coverage through
ready-by, up to 48 hours. Missing forecast values remain missing; they are never
filled with an invented future tariff or solar prediction.

## Scheduling and readiness

Charging remains continuous when the existing continuous setting is enabled.
The charging-strategy selector can explicitly select Continuous, Split, or Adaptive;
existing entries resolve their previous boolean to the corresponding strategy. Adaptive allows
pauses when they are economical and the readiness deadline remains attainable.

The default readiness buffer is 30 minutes. It is a soft objective: the planner
first tries to meet the actual deadline, then the buffer, then compares costs and
preferences. It reports expected/conservative completion, remaining margin, and
the latest start among schedules it actually validated. That last value is not a
proof that no later feasible start exists.

The deterministic search is bounded to 2,000 candidate evaluations and runs off
the Home Assistant event loop. It retains a separately generated earliest-feasible
candidate. It does not claim a globally cheapest solution. Diagnostics distinguish
valid schedules, provable capacity/price shortfall, incomplete forecast coverage,
and search that did not find a feasible schedule.

Available EV power is calculated from the grid-import limit, conservative solar
and household-load forecasts, and committed HVAC demand. A start/stop charger must
fit at its full configured rate. Battery discharge is never assumed to create safe
headroom. Existing cross-entry reservations remain the final execution safeguard;
this feature does not jointly schedule multiple EVs.

### Daylight preference and peak prices

Daylight is an economic tie-breaker, not an instruction to maximise charging
before sunset regardless of price. A complete daylight candidate is compared
with the ordinary ready-by candidate using delivered energy and effective cost,
including partial intervals and solar export opportunity cost. Cost comparisons
use the same four-decimal currency precision as reported optimisation costs.
A daylight candidate cannot replace better target coverage or a cheaper candidate
with the same coverage. Continuous schedules still need one contiguous window;
split candidates can combine daylight and ready-by capacity without duplicates.

The physical optimiser revalidates every candidate against capacity, the actual
ready-by deadline, the configured readiness buffer, price limits, action limits
and minimum dwell. Within those requirements it ranks estimated cost before
daylight energy. A costlier daylight slot remains eligible if required for
readiness, but a 40 c/kWh evening slot does not displace a 9 c/kWh morning slot
merely because it is still daylight. Explicit active Continuous or opportunistic
charge-now intent keeps its existing precedence. Carbon preferences and economic
schedule-retention thresholds remain configured objectives; daylight does not
bypass them. A retained cheaper night schedule is valid with daylight enabled.

Decision evidence reports both seed candidate costs, whether daylight was
selected, and whether a better ready-by schedule or subsequent physical
revalidation displaced it. Candidate comparisons are modelled estimates; they
are not measured savings or proof of successful charging. Enabling daylight does
not require actual solar surplus; surplus already lowers effective cost when
forecast evidence supports it.

## Charging observations

Optional measured charging power accepts W or kW. Optional cumulative energy
accepts Wh or kWh. Observations older than ten minutes are not used for learning.
Measured energy takes precedence over integrated power. The existing configured
charging-power calibration remains available when measured history is insufficient.

When a sensor provides `sampled_at_utc`, its actual sample time is authoritative:
cached reports cannot refresh it, and malformed, missing or naive values in
that explicit field fail closed. Sensors without that field retain the normal
Home Assistant report-time fallback. Confirming a power reduction also requires
the sample to follow the command. Fresh measured power above the configured rate
raises the retained grid reservation; low or unavailable telemetry alone cannot
shrink that reservation.

Observed capacity survives a concurrent start rejected before dispatch. Unissued
requested increases do not become physical evidence, and a confirmed reduction
retires the previous peak. A failed capacity save still requests a safety refresh
to retry durability checks before new commands.
Command confirmations are reconciled before asynchronous bookkeeping so later
power readings remain authoritative, including if telemetry then becomes unavailable.
Listeners consume each power event's sample rather than a later state-machine
value. The confirmation sample timestamp retires older queued readings, so they
cannot reinstate a peak after a proven reduction.

Compact statistics are retained for aggregate performance and SOC bands below 60%,
60–80%, 80–90%, and 90–100%. A band requires three sessions and 60 observed minutes.
The conservative efficiency estimate includes a 10% margin. Sensor or capability
changes invalidate incompatible learning without clearing unresolved session spend.
Raw location and trip history are not recorded.

## Battery and cost estimates

When battery inputs and an observed supported self-consumption or backup profile
are available, the planner compares baseline and EV schedules using chronological
battery energy, reserve, rate limits, and conversion losses. Solar used in the EV
can therefore have a cost beyond the lost export payment: it may replace energy
that would have avoided a later household import.

Terminal usable energy is valued using the median nonnegative import price in the
last three known forecast hours, adjusted for discharge efficiency. No value is
invented for forecasts outside the available horizon. Missing or opaque battery
behaviour is identified in diagnostics; opaque profiles use conservative self-consumption
and backup scenarios where those models are supported. Existing Enphase profile authority and
recovery rules remain unchanged. All reported savings are modelled estimates.

A retained feasible schedule is changed economically only when savings meet both
the default absolute threshold of 0.50 tariff-currency units and the 5% relative
threshold. Adaptive mode also uses a default 15-minute dwell. Safety, price limits,
and readiness take precedence over economic stability.

## Price policy and session budget

Existing installations retain hard price ceilings. New installations default to
Departure priority, but above-normal-price charging requires an explicitly entered
emergency ceiling and positive extra-spending budget. Configurations with no price
ceiling retain their existing eligibility.

The planning definition of extra spending is grid energy multiplied by the amount
that the import price exceeds the normal ceiling. Execution conservatively budgets
commanded energy at the emergency ceiling, without assuming solar credit. Fresh, aligned
energy-meter intervals at an unchanged observed tariff settle against measured
EV energy and that tariff; missing, reset, or ambiguous intervals retain the
commanded upper bound. Premium
commands have timed stop deadlines and include confirmation/stop latency in their
budget allowance. An uncertain stop retains ownership, capacity, and spending
exposure. A confirmed disconnect resets the session budget; an unknown connection
state or restart does not.

Device/network failure can prevent a requested stop, so monetary limits describe
command authorisation and conservative accounting, not a guarantee that a faulty
charger will cease consuming energy.

## Optional variable power

Map a Home Assistant `number` entity with A, W, or kW units and a measured charger
power sensor. Enter allowed minimum and maximum setpoints. Current controls also
require conservative phase voltage and a phase count of one or three. The maximum
configured rate remains an upper bound. Commands intersect configured and entity
limits and round down to an advertised step. Below the minimum nonzero setpoint,
the planner stops charging.

Starts/increases reserve load and persist recovery metadata before setting the
limit and starting. Failed or uncertain limit confirmation cannot fall through to
an unrestricted start. Reductions release capacity only after the lower limit and
fresh measured load are confirmed. Original limits are restored only after a
confirmed stop. External limit changes are treated as manual conflicts.

The Decision summary sensor's `ev_optimization` attribute contains the selected
strategy, completion/margin evidence, capacity exclusions, cost assumptions,
spending information, search status, and retained-schedule comparison. These are
deterministic inputs to explanations; AI cannot authorise commands.

Measured energy settlement uses sensor report timestamps and retains commanded
spending for the interval after the latest report. Repeated counter values do not
refund unobserved consumption. Calibration compares delivery with the commanded
setpoint; changing the performance schema invalidates learned estimates while
preserving session spending. Delayed power feedback can confirm a pending
reduction on a subsequent update. A confirmed stop with a failed original-limit
restore retains recovery ownership until restoration succeeds.

Current-slot requirements apply to retained schedules as well as generated
candidates: an eligible active Continuous session or low-price charge-now request
cannot be displaced by a cheaper future window. Valid zero-delivery intervals
count toward learned session duration. Diagnostics report the configured or
Recorder fallback until measured statistics meet the model's observation threshold.

Expiry of a charging lease requires a confirmed stop, including when the saved
charger baseline was on. Confirmed stops end spending accrual even if restoring
the original number limit remains pending. Economic schedule retention compares
feasible cost choices; daylight cannot force replacement with a more expensive
schedule.

Confirmed stops also close the preceding telemetry sample's billing exposure,
so a later refresh cannot debit it again. A retained capacity reservation does
not accrue session spending while charging is observed off.

With shared-charger vehicle profiles, number commands use the same captured
vehicle-session guard as start/stop commands. Measured calibration identity
includes the resolved vehicle. A confirmed unplug resets session spending and
discards incomplete measured intervals; uncertain handoff preserves spending.

## Bounded operation during household-consumption outages

Known outages use the quality-approved load forecast for the configured grace (default
30 minutes), with an additional 1 kW upper-load uncertainty allowance. Existing charging
can continue at no more than the current setpoint; automatic new starts wait for recovery.
Normal import-limit and shared-reservation checks still apply. Variable-power commands
use confirmed current-limit handling; if safe reduction cannot be established, pause.
Forecast-only authority expires at an execution deadline and is never renewed by replans.

`ha_energy_planner.charge_now` authorizes 1–240 minutes (default 60) of charging outside
normal economic windows/price ceilings. It does not bypass capacity, connected-vehicle,
SOC, recovery or charger checks. Its expiry and the earlier outage deadline are enforced
by the actuator timer. `ha_energy_planner.cancel_charge_now` stops and applies the normal
one-hour manual-stop hold. Vehicle swaps/unplug continue to invalidate session overrides.

The adapter also checks the charging lease before every start/retry and while
waiting for confirmation. A dispatched start that reaches its deadline requires
a safe stop; delayed successful feedback cannot renew authority. An unconfirmed
stop retains recovery ownership and capacity for the existing retry policy.

Full recovery needs two advancing samples at least 60 seconds apart. Retain the first
sample for up to 30 minutes; require only the newest to meet the configurable 1–30 minute
sample-age limit (default 15). Source timestamps take precedence over HA report times.
A single fresh sample stable for 90 seconds permits bounded degraded operation when the
original outage budget and model are eligible. Existing eligible model continuations
retain their original allowance during stabilization; new explicit starts wait for that
stability check. Stopped EVs do not gain new automatic start authority. All limits and
price ceilings remain enforced, and repeated samples cannot grant full recovery.
Flapping resets observation but preserves outage age and power ceilings. Budget expiry
pauses charging even if a single sample remains numeric. Unknown outage age fails closed.

Committed full sample recovery wakes startup recovery immediately. One fresh safety
validation, safe-state restoration, final preflight and activation verification are still
required. This never re-enables disabled device controls. Recovery stages and sample-age
limits are exposed alongside the existing remaining-budget and uncertainty attributes.

EV start and stop commands share one mapped charger switch (or `input_boolean`
helper). Separate command buttons are retired. Upgrades retain an existing
charger switch and remove old start/stop mappings; entries with only separate
controls require a stateful control before EV operation can resume. Saved
command identity remains recovery evidence, and a confirmed switch-off is
required to release charging owned through an old command endpoint.


## Vehicle suspension and waiting

`SUSPENDED_EV` and `SuspendedEV` mean the vehicle is declining or pausing offered
energy. `SUSPENDED_EVSE` / `SuspendedEVSE` mean the charger is withholding energy
and do not by themselves block a permitted start. These meanings follow the
[Open Charge Alliance status definitions](https://openchargealliance.org/wp-content/uploads/2025/05/Using-OCPP-with-CHAdeMO.pdf).
Neither vehicle suspension nor zero power proves the battery is full. For example,
79% SOC against an 80% target remains unmet; missing SOC remains uncertain.
The charging-status sensor shows the vehicle wait and exposes `soc_target_met`
separately. Future schedule completion and savings remain modelled estimates.

Automatic starts wait during vehicle refusal, including Adaptive, Continuous,
Split, saved legacy schedules, degraded-load fallback and execution of an older
plan. No charging or power-limit command is issued just to test a current refusal.
If refusal arrives during start confirmation, including after a service error,
the adapter stops retrying and makes one bounded safe-stop compensation. Permission
must be confirmed off; inactive vehicle feedback alone never proves that. A
confirmed compensation is expected waiting, not successful charging or a failed
charger. Unconfirmed compensation retains ownership, grid capacity, conservative
billing exposure and the existing safety-stop recovery/backoff rules. Refusal
observed during a failed power-limit write is also retained before compensation
changes feedback; the power-limit failure remains recorded as a failure.

The wait is persisted for the vehicle/session and mapped charger topology. A
planner stop may change feedback to `SUSPENDED_EVSE`; that does not erase the wait.
A verified disconnection/new plug session, changed vehicle/control mapping,
changed known target, or positive charging feedback allows reconsideration.
Current vehicle refusal takes precedence over a target change or contradictory
connection sample. Changing power tuning alone does not reset the wait. A new
SOC sample below target never resets it by itself. `Finishing` is a stopped
transaction state, so it does not count as positive charging evidence for a reset.
Verified unplug evidence survives repeated contradictory refusal reports; a new
session with current refusal remains blocked on its own evidence boundary.

An explicit **Charge now** / manual start may reset the remembered wait for one
attempt when current feedback permits charging. It cannot override live refusal
or unknown/faulted feedback during a remembered refusal. Charge now returns
`waiting_for_vehicle` for an expected refusal, creates no running override, and
leaves the SOC target unchanged. Rejected or unconfirmed manual starts retain the
remembered wait; a blocked attempt does not invent a fresh vehicle refusal or
replace its original evidence. Only confirmed active charging clears that wait. When a refusal timestamp is known,
the positive charging report must have a valid source/report timestamp at or after
that refusal; an older cached report or an invalid source timestamp cannot reset it.
Automatic replans do not repeat a refused manual attempt. Resolve the vehicle-side pause, change the target if appropriate, or
unplug/replug before trying again. Real issued commands remain counted in the
action allowance; no-command waiting does not consume it or create failed history.

**Keep charger On** retains its existing stateful permission behavior after the
measured SOC reaches target. It can enable preconditioning permission while the
vehicle declines traction-battery charging, without reporting active charging.
It cannot bypass tariff, capacity, safety, expiry or power-limit restoration rules.

These behaviors are verified with deterministic tests. Live acceptance still
requires observing the actual vehicle status, charger switch, meter and SOC across
a refusal, confirmed stop, restart and new plug session. A passing repository gate
does not establish physical charger behavior or measured energy savings.


Refusal-evidence storage errors cannot abort a safety stop. The wait is retained
in memory and its durable write is retried on a later cycle; persistence across
restart cannot be guaranteed while storage is unavailable. Verified tracked-vehicle
unplug reconciliation clears an older wait even when no disconnected telemetry
sample was collected. Its captured unplug timestamp protects refusal received in
a newer session while reconciliation was queued or awaiting storage.
