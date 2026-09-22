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

## Charging observations

Optional measured charging power accepts W or kW. Optional cumulative energy
accepts Wh or kWh. Observations older than ten minutes are not used for learning.
Measured energy takes precedence over integrated power. The existing configured
charging-power calibration remains available when measured history is insufficient.

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
the original number limit remains pending. Economic schedule retention cannot
override the selected daylight preference.

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
