# HVAC restoration with mode-specific zone targets

A saved zone target belongs to the mode and head settings that exposed it.
Switching an off Daikin head into heating can expose different targets from
switching into cooling. A target first seen after planner acquisition does not
establish the original user setting.

Synchronized zone takeover now requires complete numeric baselines for every
zone whose target will change, with an active original head mode matching the
requested mode. It rejects hidden targets with `climate_zone_target_unavailable`
and off/different-mode baselines with `climate_zone_baseline_mode_mismatch`,
before schedule suppression, zone activation or head writes. This deliberately
blocks new synchronized off-to-on and heat-to-cool/cool-to-heat takeovers until
complete mode-aware restoration is supported. Existing same-mode ownership may
continue after a planner stop if its original numeric baselines are retained.
Room goals, equipment offsets, demand gates and cycle/rest protection are unchanged.

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
