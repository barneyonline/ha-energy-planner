# Amber tariff forecast coverage

Energy Planner prefers native tariff intervals over a convenience forecast.
For Amber's `per_kwh`, `start_time`, `end_time` and `duration` schema (including
camelCase equivalents), declared ends determine coverage. Every row must have
finite numeric prices, aware timestamps and a consistent 5-, 15- or 30-minute
duration. Missing metadata, inconsistent times and overlapping intervals reject
that native series; the convenience forecast and scalar price cannot replace it.

Amber's [published API schema](https://github.com/amberelectric/public-api/blob/main/swagger.json)
defines duration in minutes and illustrates a 30-minute interval from
`02:00:01Z` to `02:30:00Z`. Energy Planner represents this exact convention as
`[02:00:00Z, 02:30:00Z)`: normalization requires duration 30, an end aligned to a
UTC half-hour and an elapsed span of exactly 29 minutes 59 seconds. This shares
the existing Export Limit policy's convention. Other timestamp shapes are not
rounded. Exact native intervals, including high-resolution prices, keep their
boundaries. Negative prices retain their sign and transition at the same instant
in the planning grid, price series and HVAC tariff lifecycle evidence.

The [Amber Express sensor implementation](https://github.com/hass-energy/amber-express/blob/main/custom_components/amber_express/sensor.py)
publishes both native data and a convenience forecast with minute-formatted
timestamps. They are two representations of the same source, not independent
confirmation. Energy Planner uses the native price precision and metadata.

## Safety and recovery

Real leading, internal and tail gaps remain missing. Explicit ends prevent
cadence inference from carrying an old price into a gap or beyond the final
interval. An unsafe leading/internal gap cannot borrow later covered hours;
the existing healthy/degraded horizon thresholds and source freshness checks
remain in force. Current point prices never provide future coverage.

If fresh data advertises a known start within two seconds of the collected
snapshot while that instant is uncovered, one refresh is scheduled when all
eligible import/export starts have arrived. This grants no control authority
before coverage is valid. Later starts can update the shared pending timer
within its original two-second window; a moving provider cannot postpone it
indefinitely. Actual retry requests cannot repeat within 30 seconds, measured
using elapsed monotonic time. The retry bypasses the ordinary request debounce
while retaining Home Assistant's refresh lock. If context building crosses the
deadline, the eligible snapshot schedules one immediate retry.
Recovery cancels pending work without consuming a retry attempt, so an imminent
gap can schedule a replacement; unload/reload invalidates retired callbacks.
Invalid or stale sources and longer gaps await ordinary source/interval refreshes.
Planner ownership, manual overrides, HVAC cycle limits and the 300-second Export
Limit setting cooldown continue to apply to every refreshed plan.

The existing bounded `forecast_coverage` diagnostics include:

- `source_validity`: declared, inferred, or invalid interval evidence.
- `source_freshness`: fresh or stale tariff state.
- `normalized_intervals`: count translated using the exact Amber convention.
- `leading_missing_slots`, `internal_missing_slots`, `trailing_missing_slots`,
  plus covered and continuous hours.
- `next_retry_at`: the imminent validity boundary, or null when none is eligible.

These fields contain counts, classifications and timestamps, with no raw payloads.
`next_retry_at` describes eligibility from that snapshot; the shared timer also
enforces the retry frequency bound.

## Regression evidence and limits

`tests/fixtures/tariffs/amber_boundary_2026_11_04.json` contains sanitized import
and export rows with generic entity IDs, prices, units and 27 native intervals.
The retained November 4 capture reproduces the old parser's unsafe classification
at `08:00:00.200Z`, then healthy classification at `08:00:01Z` and `08:00:10Z`,
using the same sufficient payload. The corrected parser provides 12 continuous
hours at all three instants. Deterministic regressions also cover invalid
metadata, real gaps, stale data, exact price changes, UTC/local DST transitions,
grid/lifecycle agreement, different import/export validity times, context builds
that cross a boundary, bounded retries and teardown.

The earlier audit reported 48 incomplete-horizon episodes, totalling about
38 minutes 17 seconds, mostly near half-hour boundaries. Historical native
payloads were not retained. That history establishes recurrence, but cannot
prove this mechanism caused every episode. No live Home Assistant connection or
device action is needed for these tests; replay is not a deployed-outcome claim.

Validation: focused forecast/input/coordinator/export regressions followed by
`scripts/docker-validate.sh`, without weakening safety fixtures or the full gate.
