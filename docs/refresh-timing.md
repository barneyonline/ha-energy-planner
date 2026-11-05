# Refresh timing diagnostics

The Planning duration sensor continues to report elapsed wall time from entry to
exit of the coordinator refresh call. Its total includes failures, cancellation,
lock waits and the final delayed-save flush. A failed flush is a failed refresh.
These durations do not measure CPU time, physical actuation or energy savings.

Diagnostics under `refresh_performance.latest` and the sensor attributes expose:

| Field | Meaning |
| --- | --- |
| `duration_ms` | Entire refresh call, with the existing sensor value unchanged in meaning. |
| `request_age_ms` | Age of an attributable request, including debounce/coalescing before refresh entry; `null` when requesting-task attribution is unavailable. Outside the total. |
| `exclusive_phases_ms` | Nonoverlapping elapsed phases, whose sum equals the total within rounding. |
| `phase_durations_ms` / `latest.phases` | Existing phase interface plus new detail and compatibility aliases; do not sum this mapping. |
| `export_reconciliation_pending` | Runtime gate while command authority/reconciliation is outstanding or failed. |
| `export_reconciliation` | Last deferred reconciliation's total, phases and success flag, separately from refresh time. |

Exclusive phases are `command_lock_wait_ms`, `export_reconciliation_ms`,
`planner_lock_wait_ms`, `inputs_ms`, `planner_ms`, `post_plan_ms`,
`storage_flush_ms`, `post_refresh_ms` and `unattributed_ms` (residual setup and
accounting work). Only reached phases appear. `planner_ms` includes executor
queueing and result delivery. Additional `planner_queue_ms` and `planner_work_ms`
are details inside that phase when the worker completes, not additional elapsed
phases. If cancelled while a worker is still running, its later completion cannot
modify that refresh's snapshot. Queue/work details are then absent.

The compatibility aliases `fingerprint_ms` and
`persistence_and_execution_ms` describe work inside the exclusive phases. Device
execution already runs separately after refresh; the legacy alias name does not
imply that physical commands finish within the refresh. Post-plan accounting now
also covers climate-engine storage and post-commit work omitted by older phases.
The final storage-flush phase is measured on exceptional exits too. Timings retain
one snapshot per refresh/deferred reconciliation and the existing bounded counters;
no raw input payloads or durable timing history are added.

When the command lock is busy, one entry-owned reconciliation job queues behind
it. Immediate admission respects older lock waiters, including the gap after
release before a waiter resumes. A zero-deadline timeout bounds only lock
acquisition to the current event-loop turn; reconciliation and device transactions
are never put inside that timeout. Refreshes continue input collection and planning, with export readiness marked
`export_reconciliation_pending`. Pending refreshes cannot use the fingerprint
shortcut. Device execution checks the gate under the command lock; it cannot
execute a plan built from unconfirmed ownership. Successful reconciliation
invalidates those plans and requests a coalesced fresh refresh. Failures keep the
gate closed and retry on the next normal refresh. Durable pending, uncertain,
manual-change, baseline and restoration checks remain in the export controller.
The follow-up increments the requested-refresh counter only when dispatched,
including when it coalesces with an already scheduled refresh. Export status
retains current durable pause, confirmation and restoration reasons ahead of the
transient queue reason; `reconciliation_pending` reports the queue independently.
The export cooldown deadline remains visible while reconciliation is queued, and
restoration still waits for the existing observed-confirmation cooldown.
Configuration and feedback are read again after command acquisition. Shutdown
stops admission and drains the job before final restoration, preserving in-flight
command durability. No busy polling or increased refresh interval is used.

Uncontended reconciliation still occurs before planning, including any necessary
restoration and durable writes. Slow reconciliation or storage can therefore still
lengthen that refresh, now with explicit accounting. This change targets coupling
to an already busy unrelated command; it does not promise a maximum refresh time.

The controlled contention test fails on base `92b4694`: planning cannot finish
until an unrelated command releases the lock. With this change planning finishes
while the same blocker remains held, and repeated triggers create one queued
reconciliation job. Controlled-clock tests separately account for an 80,000 ms
command wait and 3,000 ms reconciliation without real sleeping.

The sanitized 4 November investigation supplied 1,205 observations from deployed
1.4.10: median 159.574 ms, p95 196.702 ms, maximum 81,360.690 ms. The old phases
explained only 3,625.809 ms of the maximum. Neither that evidence nor these
synthetic tests identify its cause or establish a live fix. No Home Assistant
access, credentials, deployment or physical device tests were used.
