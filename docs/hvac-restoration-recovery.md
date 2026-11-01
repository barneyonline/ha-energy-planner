# HVAC restoration with mode-specific zone targets

A saved zone target belongs to the mode and head settings that exposed it.
Switching an off Daikin head into heating can expose different targets from
switching into cooling. A target first seen after planner acquisition does not
establish the original user setting.

Synchronized zone takeover normally requires complete numeric baselines for every
zone whose target will change, with an active original head mode matching the
requested mode. Hidden targets produce `climate_zone_target_unavailable` and
unproven mode transitions produce `climate_zone_baseline_mode_mismatch` before
schedule suppression, zone activation or head writes.

## Opt-in AirBase starts from Off

In Options → Settings → Climate policy, **Allow safe Off starts (AirBase)** is
disabled by default. It permits a new synchronized Off start only for the core
Home Assistant `daikin` integration backed by pydaikin's `DaikinAirBase`, with
explicit same-controller switch/thermostat pairs and installer-verified airflow.
It does not enable any zone, change room sensors or equipment offsets, qualify
an uneconomic plan, establish thermal learning readiness, or make estimated
power into measured energy. Heating-to-cooling and cooling-to-heating takeovers
from an active original mode remain blocked.

Before ownership or any device write, the adapter forces read-only refreshes of
AirBase's `get_control_info`, `get_zone_setting` and `get_model_info` resources.
Manual control monitoring starts before those reads and remains active through
the ownership flush. No requested target is treated as issued-command feedback
during preparation. A head or zone change cancels acquisition without physical
compensation, including when the user's target matches the planned target.
Preparation also rechecks the execution window, current manual-hold helper and
production controls after readback and after ownership persistence. Expiry, a
new hold or disarming cancels the unissued attempt and rolls back only its
provisional ownership; release of any earlier ownership remains separate.
It requires both remembered head targets (`dt1`, `dt2`), both complete zone
arrays (`lztemp_h`, `lztemp_c`), a proven remembered mode, finite mode-specific
head limits, published Home Assistant head bounds/step and exact zone targets
within head ±2°C. Every captured head target must pass the same exact bounds
and step checks used for restoration. Registered zone indices
must cover every physical zone reported by `en_zone`; unused fixed-width array
padding grants no control authority. Each switch and paired thermostat must
prove the same native zone index. Friendly names do not establish identity.
Off/Dry is also supported when the vendor's original `mode=7` and `stemp` agree
with the Off head's numeric target and finite bounds/step. This captures the
original Dry target directly; it does not invent a `dt7` memory or treat a heat,
cool or room-comfort target as Dry intent. Auto and fan-only memories remain
unsupported. Off power, head readback and every zone switch must agree
with the vendor result. Missing fields, unsupported devices, timeouts, aliases
from another controller and incomplete topology keep the previous fail-closed
restriction. No mode is selected to discover hidden values.

The executor flushes the original Off state, exact zone enablement, both mode
memories, controller identity and acquisition topology before the first write.
These vendor-read settings are distinct from room comfort goals. The controller
fingerprint includes the exact registered zone-index bindings and is checked
again at command boundaries, including after persistence
and scheduler-guard waits, so a replacement source cannot inherit permission to
receive an old controller's commands. A refresh's
compensation snapshot is persisted separately from the immutable lifecycle
baseline. Restart, reload and later sensor/topology edits cannot replace the
original settings with targets exposed by planner control. Release uses the
captured controller mapping and checks its registry identity again.
Resuming an owned Off/coast phase also checks the original controller before
reading a fresh attempt snapshot. Every applicable snapshot identity must agree
at dispatch; a replacement's valid readback cannot supersede old ownership.

Release keeps an already safe open subset and checks airflow before any zone
opening that could implicitly wake the head. A saved target alone does not
authorize reopening a closed room. It first restores the acquired mode's head target and zone targets, then
restores the originally remembered head mode/target and confirms Off before
closing originally closed dampers. This may briefly select the remembered mode
as part of restoration. Existing command serialization, confirmation deadlines,
uncertain-command reconciliation, scheduler ownership and manual supersession
remain in force. A zone opening can recall the Off head's mode before the
explicit head command; only the exact freshly read mode/target and its already
open or opening aliases are recognized as that issued activation. Recognition
expires from the original issue time and retires after a later head command.
Dry's null zone targets are scoped to the issued Dry restoration command and
matching live head readback. Explicit user and external-parent contexts still
supersede these expectations. An explicit user change supersedes the affected saved controls. Zone snapshots
also retain their original mode: after a user takes over the head, a cooling
baseline cannot be written into heating memory even if its number fits the
published bounds. The captured controller mapping remains with unresolved zone
ownership after main supersession. Restoring a zone cannot wake a manually
stopped head; incompatible mode-specific targets remain pending for the operator.
A fresh read-only vendor check can clear an already matching inactive zone memory
after manual head takeover without changing the user's mode or waking the unit.
A manually superseded zone also revokes head mode/target restoration writes: a
shared head write can clamp that zone even without a direct thermostat write.
The original Off power is still requested, and unresolved settings remain for
the operator. The check runs again after persistence and scheduler-guard waits.

A mode-aware restoration gets one durable automatic attempt. Failure or
interruption retains the original evidence and requests shutdown; subsequent
retries inspect readback and can confirm an already completed restoration, but
cannot oscillate between modes to discover or repair unknown settings.
`hvac_zone_restore_operator_required` and `off_start_restore_operator_required`
identify incomplete recovery; a changed registry owner reports
`off_start_provider_identity_changed` and prevents writes to the replacement.
Use the bounded operator handoff below if readback cannot confirm completion.
No saved target is silently clamped, invented or discarded. Before reporting
success, a fresh read-only vendor snapshot must confirm the original remembered
mode, every owned heating/cooling memory and zone power after shutdown. If a Dry
write unexpectedly changes an inactive mode memory, the baseline stays pending
for the operator; visible Off/head feedback alone cannot clear it.

Protocol evidence: HA's [2026.10.4 Daikin climate implementation](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/daikin/climate.py)
selects the zone array by active mode and hides numeric targets while Off.
The pinned `pydaikin==2.19.1` implementation provides fresh read-only resource
updates and AirBase's separate mode memories; see the [AirBase source](https://github.com/fredrike/pydaikin/blob/master/pydaikin/daikin_airbase.py),
[native zone switch identities](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/daikin/switch.py)
and [control readback/recall implementation](https://github.com/fredrike/pydaikin/blob/master/pydaikin/daikin_brp069.py).
This is local fake/replay validation, not physical-device acceptance. No operator
baseline fallback is inferred for devices that cannot provide these memories.
A sanitized read-only AirBase sample with two physical zones, eight encoded
array slots, Off/Dry `stemp` and absent `dt7` is replayed in
`tests/fixtures/hvac/airbase_off_dry_memory_2026_10_31.json`. It establishes payload
shape and available readback, not physical rollback acceptance.

Within an owned lifecycle, a previously missing zone baseline stays missing
across refresh and reload. An existing numeric baseline stays original even if
feedback changes. Releasing and acquiring a fresh lifecycle is the boundary for
capturing a new baseline; an in-progress lifecycle cannot adopt newly exposed
mode memories as original targets. A failed refresh uses its immediately
preceding head and zone settings for compensation. Those per-attempt snapshots
remain separate from the immutable lifecycle settings used for final release,
so rollback cannot pair a current 17°C head with original 24°C zone targets.

## Existing incompatible pending records

The sanitized 29 October sequence was off/head 19°C with hidden zone targets,
then remembered heat/head 19°C/zones 20°C, then remembered cool/head 23°C/zones
24°C. A later refresh captured 24°C after acquisition. Release returned the head
to heat/19°C and then off, whose published zone bounds were 17–21°C. The old
record cannot establish that 24°C is the original heating or off-state intent.
The minimal synthetic replay is `tests/fixtures/hvac/off_mode_restoration_2026_10_29.json`.

After a confirmed shutdown, saved targets outside finite published zone bounds
produce `hvac_zone_restore_operator_required`. Plan health's
`pending_hvac_restore.recovery_blocker` reports
`saved_zone_targets_incompatible_with_off_baseline`. The original head, target
and paired-power evidence remains pending. Automatic retries only inspect
feedback; they do not probe other modes by waking the head, clamp saved targets,
or declare restoration complete. Missing/unavailable bounds remain ordinary
pending feedback, without inferring operator intent. Input health can remain
healthy while plan health is degraded; Climate control active stays on while
ownership remains unresolved.

## Bounded operator handoff

Use this only when you have decided the desired final settings. A manual hold
alone does not discard restoration evidence, and `resume_climate_control` cannot
resolve an incompatible target.

1. Export diagnostics and keep the pending head/zone/schedule settings. Set a
   manual HVAC override long enough to complete the work. The override service
   can report failed restoration while retaining the hold and pending evidence.
2. In Home Assistant, explicitly set the desired head mode/temperature and each
   affected physical zone's power/target, using installer-approved airflow
   combinations. If targets are hidden while off, selecting an active mode is
   an operator decision and may run heating/cooling. Choose the intended final
   values; 24°C from the old record is not established original intent.
3. Wait for actual device readback after each change. Observed manual head and
   zone changes supersede only their corresponding saved baselines. Declared
   switch/thermostat pairs supersede together. A no-op service call or a helper
   toggle does not prove a changed zone setting. Return the head and dampers to
   the intended final power state and confirm readback.
4. Run `ha_energy_planner.restore_safe_state` once. Verify pending restoration
   clears and schedules show their intended states before clearing the manual
   hold. Clearing through user supersession means the operator replaced the old
   intent; it does not prove the old 24°C targets were restored.

If any affected baseline remains, or commands/feedback are uncertain, stop with
the hold and evidence retained. Do not cycle modes repeatedly or edit stored
ownership to force completion. This patch provides no automatic repair of an
ambiguous legacy baseline and has no live-device acceptance evidence.
