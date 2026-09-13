# Climate decision policy

Energy Planner can compare a learned prediction of normal climate operation with
preheating or precooling. The comparison includes HVAC electrical energy, tariff
prices, lost export revenue, solar, the selected EV schedule and supported battery
behaviour. A tariff difference alone is not reported as a monetary saving.

## Policies and activation

- **Automatic** (default): retain legacy behaviour while learning, with occasional
  observation periods; activate economic decisions only after validation passes.
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
