<p align="center">
  <img src=".github/assets/icon.svg" alt="Energy Planner icon" width="96" height="96">
</p>

# Energy Planner

> [!IMPORTANT]
> Energy Planner is in active development. Do not rely on it as the only protection for equipment, billing, comfort, or vehicle readiness.

[![Release](https://img.shields.io/github/v/release/barneyonline/ha-energy-planner?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-energy-planner/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-energy-planner/ci.yml?branch=main&label=ci)](https://github.com/barneyonline/ha-energy-planner/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/barneyonline/ha-energy-planner)](LICENSE)

Energy Planner is a local-first Home Assistant custom integration for coordinating energy prices, solar, household load, batteries, EV charging, climate comfort, and Enphase operating profiles.

## What it provides

- One Energy Planner device containing all status, controls, and troubleshooting entities
- One central **Configure** page with sections for Energy, Climate, Presence, Enphase, AI, and EV inputs
- **Armed**, **Mode**, **Current state**, **Next actions**, and a **Plan** calendar
  that shows actions only for enabled device-control areas
- A guarded **Automatic control** switch plus individual **Climate control**, **EV control**, and **Enphase control** switches
- A built-in household-load forecast trained from Home Assistant Recorder
- Native EV scheduling and tariff-aware climate preconditioning
- Optional Enphase profile control and on-demand AI troubleshooting
- Notifications only for problems that normally require user action

## Requirements

Energy Planner reads existing Home Assistant entities and calls Home Assistant services. It does not connect directly to vendor clouds.

For full planning, configure:

- Import and export tariff forecast sensors, commonly Amber Electric
- An external PV forecast, commonly Solcast Forecast Today and optionally Forecast Tomorrow
- A measured whole-home instantaneous consumption sensor in W, kW, or MW
- Home Assistant Recorder
- Battery state of charge and the entities for each device you want to control

Weather, carbon intensity, measured PV power, and AI are optional. An external solar forecast is still required.

Compatibility: Home Assistant 2026.6.0 or newer; current integration version 0.9.14.

## Installation

### HACS custom repository

1. Open **HACS → Integrations**.
2. Select **Custom repositories** from the three-dot menu.
3. Add `https://github.com/barneyonline/ha-energy-planner` as an Integration repository.
4. Download **Energy Planner** and restart Home Assistant.
5. Open **Settings → Devices & services → Add integration** and add **Energy Planner**.
6. Open **Configure** on the integration to map inputs and review policy.

### Manual installation

1. Copy `custom_components/ha_energy_planner` into Home Assistant's `custom_components` directory.
2. Restart Home Assistant.
3. Add **Energy Planner** from **Settings → Devices & services**.

## Initial configuration

1. Map tariff forecasts, external PV forecasts, household load, battery SOC, and the device inputs you intend to use.
2. Confirm Recorder is enabled and retains the household-load sensor.
3. Review EV, battery, grid, climate, and confirmation settings.
4. Enable the individual device-control switches for the devices Energy Planner may manage.
5. Leave **Automatic control** off while reviewing **Current state**, **Next actions**, and the **Plan** calendar.
6. Wait for the built-in load model to become ready and resolve any unsafe inputs.
7. Turn on **Automatic control**. It activates only after preflight and review evidence pass.

If activation is not safe, the switch remains off and ordinary device commands are not issued.

## Household-load forecast

Choose a sensor that reports gross household demand. Do not use:

- Solar production or an energy total in kWh
- A forecast sensor
- Signed net grid flow that becomes negative while exporting
- A cloud-derived value with large transient negative readings

Negative and unavailable readings are treated as missing, not clamped to zero. A grouped sensor can be used, but it represents only its included circuits and may underestimate unmonitored load.

Energy Planner trains from up to 28 days of Recorder history. It removes known EV-charging intervals, subtracts measured HVAC power when available, and builds expected and conservative load profiles. Conservative-bound calibration scores each day as one block so correlated 15-minute readings do not overstate the amount of independent safety evidence.

If the mapped load entity has not yet appeared during Home Assistant startup,
Energy Planner remains fail-closed and retries when the source becomes
available; the transient absence does not consume the six-hour training retry
interval.

The model requires:

- Three qualifying local-time training days with at least 80% valid buckets each
- At least 80% overall valid coverage
- Two holdout origins with at least 144 aligned samples
- Accuracy no more than 10% worse than previous-day persistence
- At least 90% conservative-bound coverage

**History days** means Recorder data exists for those dates. **Training days** may contain bounded gaps: EV charging, historical negative readings, unavailable states, or missing HVAC alignment can remove up to 20% of a day's buckets without discarding the entire day. The model still requires rolling holdout validation and three observations for every production-profile clock bucket.

The diagnostic **Load forecast coverage score** sensor shows the latest evaluated holdout coverage percentage and required 90% threshold. If a retraining attempt fails the threshold and the prior safe model is retained, the sensor shows the new attempted score while its attributes identify the active model score and evaluation time. The default-off **Bypass safety gates** setting waives this threshold, production preflight, and dry-run evidence requirements when an operator explicitly accepts reduced protection. It does not hide service failures or device feedback results.

Energy Planner keeps all sensor, binary-sensor, and calendar metadata below Home Assistant Recorder's state-attribute limit. Exceptionally large decision evidence is compacted and marked with `attributes_truncated: true`; full operational evidence remains available through the support bundle instead of being written into every Recorder state row.

Training runs at startup, after a source change, and at most every six hours. Replanning cannot create missing history. While the status is `learning`, plans remain visible but forecast-dependent active commands are blocked. Models are `ready` through 24 hours old, `degraded` from 24 to 72 hours, and `stale` after 72 hours.

Changing the load mapping disarms production control and requires fresh review cycles. Routine retraining does not.

## Controls and status

| Entity | Purpose |
| --- | --- |
| **Armed** | Whether Energy Planner may currently issue commands and why; stale or incomplete reviewed evidence keeps it off even if an earlier arm request was persisted |
| **Mode** | `review` while observing and `active` when automatic control is fully active |
| **Current state** | Live state of every configured area whose device-control switch is on |
| **Next actions** | Next state, planned actions, and decision evidence for enabled control areas |
| **Load forecast coverage score** | Current conservative-bound score, required threshold, and safety-bypass state |
| **Plan** | Calendar view of upcoming controlled actions, with readable evidence sections, local timestamps, and complete EV charging windows |
| **Automatic control** | Retains the operator's request for automatic control, including while startup safety is temporarily disarmed |
| Device control switches | Select whether Climate, EV, or Enphase may participate; grouped under Controls |
| **Explain** | Requests one evidence-based AI explanation and returns it as a Home Assistant notification |

The advanced **Run safety check** button always returns a Home Assistant notification, including when every check passes.

Energy Planner queues persistent notifications until Home Assistant has completed startup and every integration has had a chance to load. A queued alert is cancelled if the condition recovers first; otherwise, the latest alert for each condition is shown after startup.

**Explain** treats an uneconomic or thermally unsuitable climate-preconditioning window as a normal no-action plan result. It recommends changing Climate settings only when the current plan contains a specific comfort, presence, or climate-control input fault.

Turning off a device control selector always takes effect immediately. The disabled area disappears from **Current state** and **Next actions**, including the latter's action attributes and count. Energy Planner then makes one serialized best-effort safe-state restore; if confirmation is unavailable, it retains diagnostic recovery evidence and notifies without turning the selector back on or permitting new start/schedule commands. Unresolved EV ownership remains eligible for bounded safe-stop recovery after interruption or restart, with ten-minute backoff and a maximum of three failed attempts per rolling day. Dedicated EV retry timestamps keep those limits intact if another control area later updates the shared pause state or execution-audit rows rotate out.

Settings are grouped into six areas: Energy/battery/grid/data, Climate and presence, Enphase, Safety and troubleshooting, EV charging, and Planning and priorities. EV charging contains its mapped entities plus Ready by, Opportunistic charging, the opportunistic price threshold, Keep charger on, and charging policy. Ready by, both opportunistic-charging values, and Keep charger on are settings-only; obsolete native entities for those values are removed on upgrade. The one- and four-hour pause buttons are also retired; automations can still call `ha_energy_planner.pause_control` with any supported duration.

Mapped EV start and stop controls are planner actuators. Energy Planner does not expose separate manual Start charging or Stop charging buttons. The mapped vehicle target-SOC entity is required and authoritative; Energy Planner does not calculate or expose a separate target.

## Planning behavior

EV planning considers the vehicle target SOC, ready-by time, price, solar, battery reserve, grid limits, connection state, and confirmed charger feedback. Home Assistant Recorder history automatically calibrates the effective percentage gained per kWh from completed charging sessions using the configured charger power. Sessions must last at least 30 minutes, and at least 60 minutes of clean history is required before the learned rate replaces the conservative **Bootstrap EV SOC gained per kWh** setting. A learned rate is reused only while the charging sensor, SOC sensor, and configured charger power still match; configuration changes use the bootstrap rate until retraining succeeds. A 10% margin is applied to the observed rate so planning starts early rather than risking a missed ready-by time. Once a continuous charging window starts and charging is confirmed, later tariff revisions keep that session running until its planned target unless a configured hard price limit or safety gate requires a stop. The integration supports a persistent charger switch or separate start/stop controls. Multiple EVs require separate named Energy Planner entries.

Climate planning searches the configured tariff horizon for a lower-cost preconditioning window before an expensive period. Preconditioning requires somebody home by default. **Precondition while away** can explicitly permit only a complete planner-generated tariff preconditioning/coast lifecycle when occupancy is known to be away; it does not permit unrelated HVAC-on commands, unknown occupancy, targets outside the configured comfort bounds, or restarting planner-owned HVAC before the configured minimum cycle/rest period has elapsed. Only an action matching the persisted lifecycle mode and timestamps qualifies as a continuation that can bypass a redundant rest check. If an away preconditioning window begins in the future, unowned HVAC is turned off immediately and remains off until that window begins. If a refresh or temporary block misses the preferred start, the planner can use the remaining contiguous lower-price slots rather than discarding the entire opportunity. It never crosses a tariff-data gap or continues heating/cooling past the applicable comfort target. The planner can temporarily take ownership of configured climate automations and zones, then restores zone switches/helpers and automations when the period ends, comfort is reached, relevant confidence falls below threshold, inputs become unsafe, or a manual override occurs. The main Daikin thermostat always receives and confirms the planner target. When **Synchronise configured zone temperatures** is enabled, configured zone climate entities then receive and confirm the same target; this main-first order allows Daikin to update zone temperature bounds derived from the main setpoint before a zone command is validated. When it is disabled, zone climate targets remain unchanged while configured zone switches/helpers are still enabled and restored. Synchronisation requires at least one `climate` entity under Climate Zones and fails closed before takeover if any main or zone target that would be replaced cannot be captured for rollback. A failed takeover restores the captured main mode, main target, and zone targets before reporting rollback success. If immediate main restoration fails, the captured state remains owned and is retried during release or safe-state recovery; a later manual main-thermostat change supersedes the snapshot and is never overwritten by release. It does not call the release adapter when no HVAC ownership exists, and releases do not consume the daily climate command cap. Takeover stops already-running configured automation actions even when an automation is already off, while release re-enables only automations that were active before takeover. It confirms the complete commanded main and zone state and reasserts it once if a concurrent schedule overwrites the first command. If an external schedule-versus-manual classifier is used, configure both its Scheduler Change `input_boolean` and **Climate Scheduler Guard Timer** under Climate and presence. Energy Planner starts and confirms both for a 30-second settle window before any planner-owned HVAC mutation; an incomplete or unavailable guard fails closed instead of allowing the action to be classified as manual. External Manual Override helper changes use the configured manual override duration and are automatically cleared when it expires.

For an originally-off main thermostat, acquisition rollback restores the active
mode Daikin remembered for its next turn-on before switching it off again. That
mode is persisted before planner mode selection; if it cannot be recovered,
the integration still attempts and confirms the original off state and retains
main ownership for a later retry. New HVAC acquisition is blocked until that
snapshot is successfully recovered. A manual main-thermostat change
durably removes the stale rollback snapshot before any release actuator call,
so restart recovery cannot replay it over the user's setting. During an active
multi-call climate transaction, only target feedback matching the pending
planner or rollback target is treated as planner-owned. An off-to-active mode
transition is accepted only while the adapter is explicitly waiting for its
turn-on call. A different user setpoint or unexpected mode triggers manual
override even while the scheduler guard is active. An acquisition rollback,
release, or safe-state recovery then stops restoring the main thermostat,
leaves the user's main state untouched, and restores only subordinate zones
and automations before release completes. Pending zone-climate feedback must
likewise match the exact planner or rollback target. A different user zone
target is durably removed from ownership before rollback continues, remains
untouched during the current transaction and later recovery, and does not stop
the main thermostat, other zones, or automations from returning to their
captured states.

Enphase planning can select configured self-consumption, backup, or AI profiles using local tariff, PV, household-load, battery, and policy evidence.

## Safety and recovery

- Device commands require explicitly healthy or degraded shared inputs, an eligible asset-specific confidence/capability result, a current plan, matching review evidence, production arming, and the relevant device control. Unknown health classifications fail closed. A degraded or paused device area does not stop an independent ready area, including after restart; occupancy readiness applies only to climate control.
- Hard constraints are checked again immediately before every command.
- Unsafe, missing, stale, or invalid inputs fail closed.
- Confidence gates use only the forecast sources relevant to each device. The
  plan-wide score uses required tariff, PV, and household-load sources;
  optional weather and carbon scores remain visible without suppressing
  unrelated actions.
- Learning and ordinary plan changes remain silent; actionable mapping, restoration, EV-readiness, and grid-limit problems can notify once.
- AI advice cannot call services, change settings, or bypass constraints.
- An installation that was both requested-active and armed before restart
  returns to **Armed / Running** immediately. Its fresh ten-minute startup grace
  begins when Home Assistant reaches `RUNNING`, not when the config entry loads.
  Ordinary input-health, constraint, capability, conflict, feedback, cooldown,
  and rate-limit gates continue to protect every command during the grace.
- If a temporary pause blocks every enabled control area during setup or reload,
  Energy Planner disarms and restores safe state while retaining automatic-control
  intent and a restart-resumable recovery handoff. After the pause expires or is
  resumed, three consecutive healthy checks safely restore **Armed / Running**.
- At the deadline Energy Planner awaits a fresh, non-debounced plan and complete
  preflight. A healthy result remains armed silently. An unsafe, unavailable,
  failed, or uncommitted result disarms before one best-effort safe-state
  restore and creates one notification. **Automatic control remains on** because
  it represents retained intent; **Armed** is the actual command-authority gate,
  and **Mode** reads `review` while safety has disarmed commands.
- Disarmed startup recovery retries every 30 seconds indefinitely. Three
  consecutive healthy awaited checks revalidate the production contract,
  reconcile safe state, re-arm, and verify a fresh active plan. Any unhealthy
  check resets the sequence. Success is silent and dismisses the prior warning.
  A production-evidence contract change discovered during startup also enters
  this fail-closed recovery path when the installation was previously armed and
  automatic control is still requested. Only fail-safe stop, release, or restore
  commands may run until the new contract passes all three checks and the final
  active-plan verification; new start, schedule, or takeover commands remain
  blocked.
  Operator disable, explicit safety-gate arm or disarm, pause, or a
  configuration change remains immediately authoritative. Terminal operator
  cancellation dismisses the superseded recovery warning. Whole-Home-Assistant
  shutdown preserves active ownership and EV reservations; runtime reload,
  removal, and setup failure still restore.
  Progress is visible on **Production readiness**, **Armed**, and diagnostics.
  Recovery runs as Home Assistant background work and does not delay startup.

If control behaves unexpectedly:

1. Turn off **Automatic control**.
2. Run `ha_energy_planner.restore_safe_state` or press **Restore safe state**.
3. Confirm EV, Enphase, climate automation, and zone states manually.
4. Review **Current state**, **Next actions**, preflight, and the support bundle.
5. Leave automatic control off until the cause is understood.

## Services

Common services include:

- `ha_energy_planner.replan`
- `ha_energy_planner.run_preflight`
- `ha_energy_planner.export_support_bundle`
- `ha_energy_planner.restore_safe_state`
- `ha_energy_planner.pause_control` and `ha_energy_planner.resume_control`
- `ha_energy_planner.set_ev_ready_by`
- `ha_energy_planner.set_manual_hvac_override`

Advanced arm/disarm and diagnostics services are also available in Home Assistant's service UI. With multiple planner entries, provide `config_entry_id`.

## Development

Run the complete validation gate:

```bash
scripts/docker-validate.sh
```

It runs Ruff, the full pytest suite with 100% coverage, replay/schema/history validation, Home Assistant `check_config`, and a Docker smoke test.

For the Home Assistant integration smoke path alone, run:

```bash
scripts/docker-ha-smoke.sh
```

The smoke harness coalesces queued coordinator refreshes, waits for background
device execution and AI advice, and stops Home Assistant as soon as the scenario
is complete. Its 240-second limit is a failure deadline, not a fixed runtime.

Pull requests classify their changed files before starting the expensive test
jobs. Integration changes run the full pytest, quality-scale, and validation
set; test-only and tooling changes run their affected subsets; documentation
changes avoid the full pytest and Home Assistant validation jobs. Pushes and
manual Tests runs continue to execute the complete non-smoke CI job set. Run
`scripts/docker-validate.sh` for the complete gate, including the smoke test.

Start a local Home Assistant instance with:

```bash
docker compose up
```

Then open <http://localhost:8124>.

## Further documentation

- [Release notes](CHANGELOG.md)
- [Requirements and implementation evidence](docs/requirements-audit.md)
- [Quality-scale evidence](quality_scale.yaml)
