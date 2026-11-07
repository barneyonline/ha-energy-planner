# Entity state publication performance

This change selects reconciled calendar records before constructing returned
Home Assistant `CalendarEvent` objects. State publication selects the current or
next record; range queries select records overlapping the requested range.
Current-plan windows are still expanded into events before reconciliation and
filtering, so those objects are additional work. Confirmed history within the
365-day/1,000-record retention ceiling participates in validation and reconciliation
before selection, including archived UID suppression and running-window matching.
The ceiling also applies when loading legacy Store data. Descriptions,
confirmation rules and control execution are unchanged. There is no presentation cache: options, plan, feedback, ownership and
clock changes are read each time.

## Reproduction and measurements

Measurement base: `50fc72b7fbcd8b37aa91e149df39cc3e18547f3d`, including PR #167
(refresh timing/lock contention) and PR #169 (development tooling). These entity
property reads are separate from coordinator refresh timing.

Run in the pinned HA 2026.10.0 / Python 3.14 test environment:

```sh
scripts/docker-pytest-fast.sh -s tests/test_entity_publication.py
```

The fixture uses 5,000 synthetic confirmed historical actions, one current action
and nested Unicode evidence. It contains no household history. The calendar test
executes HA's actual `state`, `state_attributes`, and `event` getters, matching
its state/attributes/alarm-selection sequence. cProfile locates the expensive
calls. No assertion depends on elapsed time.

| Same synthetic publication sequence | Main | Local change |
| --- | ---: | ---: |
| HA calendar event constructions | 15,006 | 6 |
| Profiled duration | 877.145 ms | 156.943 ms |
| Calendar record normalization calls | 9 | 9 |

These are individual local measurements, including profiler/mock overhead, not a
latency distribution or performance guarantee. Other profiled runs ranged from
733 to 877 ms before and 157 to 186 ms after. One unprofiled fixed sequence took
51.683 ms. The baseline fails the deterministic `event_constructions == 6`
assertion; the fixed version passes. A single-event historical range constructs
only two objects (the current plan event and the matching historical event),
and a full historical query returned all 5,000 confirmations at that measurement
base. Current retention bounds that query to the newest 1,000 confirmations within
365 days; these original timings are not measurements of the retention change.

The same fixture exercises the actual Next actions sensor value and attributes
three times. It took 5.171 ms total, retained 9,711 UTF-8 JSON bytes, marked
`attributes_truncated`, and preserved source data. This initial scalar fixture
did not establish a slow path. A subsequent fuller fixture below identified repeated conversion and AI-matching work. The existing
Recorder byte budget and full diagnostic sources are retained.

Regressions cover repeated reads, exact end-time rollover, expiry, replans,
options, physical feedback and ownership changes without replanning, a backward
clock change, historical range queries, archived UID suppression, offset-aware
ordering, nested attribute compaction and source nonmutation.

## Fuller Next actions profiling

`tests/test_next_actions_publication.py` adds three actions (EV scheduling,
climate preparation and Export Limit), 192 EV allocation slots, 48 export tariff
blocks, eight zones, nested load-model validation and cleaning evidence, weather
diagnostics and disabled/pending/accepted AI cases. The fixture executes actual
capability discovery with deterministic state, service and registry fakes.

The initial implementation walks attributes once with `to_jsonable`, then again
to normalize them. A single normalization walk now converts Store types as
needed. Datetime, enum, dataclass, tuple/set and fallback representations remain
compatible. Compaction profile selection and resulting attributes match the
prior conversion pipeline exactly; source data remains untouched. AI advice
matching now computes the safe current plan's fingerprint once for the current
read and shares it with pending matching. No fingerprint or presentation data
is cached across reads; provider and plan safety checks remain on every read.

| Three complete reads, profiled | Before | After |
| --- | ---: | ---: |
| AI disabled duration | 40.448 ms | 35.955 ms |
| AI pending duration | 59.393 ms | 44.555 ms |
| AI accepted duration | 55.486 ms | 45.192 ms |
| Redundant whole-attribute conversion walks | 3 | 0 |
| Plan hashes when advice is pending | 6 | 3 |

Total Python function calls fell from 234,486 to 172,992 with AI disabled and
from 361,449 to 236,538 with AI pending. These are individual synthetic runs,
including profiler/mock overhead. Assertions constrain operation counts and
exact payload compatibility, never elapsed time. Both the conversion and hash
bounds fail against the prior implementation. Final JSON sizes remain exactly
9,037 / 9,045 / 9,200 bytes respectively, including the same truncation marker.
These improvements still do not reproduce or explain a 0.706-second live write.

Additional regressions change an action in place without a new plan ID, update
pending advice, weather-cache age, live capability evidence, options, ownership
and provider configuration without a coordinator replan. They prevent stale
presentation while preserving the current Recorder byte budget and safety gates.

## Limits and live evidence

Read-only evidence collected on 6 November 2026 identified HA 2026.11.0 warnings
for the calendar (0.645 seconds) and Next actions (0.706 seconds). Installed
version 1.4.10 does not identify its source commit. The grouped log does not
independently map each warning to each first/latest timestamp. Refresh-duration
Recorder samples measure coordinator work, not these entity writes.

This synthetic reproduction demonstrates avoidable calendar work; it does not
establish the cause of either live warning or prove live improvement. Calendar
history reconciliation still scales with retained history, and a deliberately
wide historical query still constructs every matching event. Current-plan
expansion still constructs each planned window before filtering: a synthetic
fragmented EV schedule with 576 slots constructs 288 plan-window objects and one
returned event for a single `event` read. Moving or caching that work would
require further measured evidence and careful invalidation;
this patch avoids that broader change. Host contention and other event-loop
work remain possible contributors to the live warnings. The live Next actions
warning remains unexplained by these fixtures; a bounded shape/count/byte-size
fixture from the warning period would be needed to distinguish a large
intermediate payload from host contention. No live instrumentation or deployment
is part of this change.

## Validation

- `scripts/docker-validate.sh`: passed on the pinned HA 2026.10.0 baseline,
  including lint, strict mypy, quality-scale evidence, full tests, coverage,
  replay/schema/history fixtures, HA configuration, runtime compatibility and
  package smoke. Full suite: 5,281 passed, 5 skipped, 36 subtests passed;
  20,638/20,638 statements covered (100%), branch regression gate passed.
- Focused tests on HA 2026.11.0: 226 passed. Files:
  `tests/test_entity_publication.py`, `tests/test_next_actions_publication.py`,
  `tests/test_calendar.py`, `tests/test_calendar_history.py`, `tests/test_sensor.py`,
  `tests/test_entity.py`, `tests/test_binary_sensor.py`.
- `git apply --check` on a clean snapshot of the base: passed.
- Review-time comparisons against the unchanged base matched all 1,500
  mixed-type/deep/compacted normalization cases, 1,200 calendar current-event and
  range pairs, 20 physical-feedback event selections, and 24 AI matching
  provider/plan/safety variants. The fragmented current-plan probe above limits
  the construction claim to the reconciled-record stage.

The focused runs used isolated Docker containers with pinned test dependencies,
`python3 -m pytest -q -s tests/test_entity_publication.py` for the reproduction,
and `python3 -m pytest -q` with the seven files above for compatibility.
Local development and validation performed no release, deployment, live API call
or production instrumentation.
