# Setup, upgrades and stable contracts

Home Assistant 2026.6.0 remains the minimum. The pinned validation baseline is
2026.9.0; CI also exercises 2026.8.2 and floating stable. HACS owns the minimum
declaration and `tool.energy-planner.support` in pyproject owns tested versions.
Weekly runtime checks detect upstream changes even without a repository push.

## Example setup

Create an entry named **Family EV**, then open Configure. The initial dialog only
asks for a name; it does not map planning inputs.

| Area | Example mapping | What to verify |
|---|---|---|
| Energy | Amber import/export forecast sensors | Forecast timestamps, units and horizon are current. |
| Solar | Solcast Today and Tomorrow sensors | External PV forecast covers the planned slots. |
| Load | `sensor.house_consumption_power` | Gross consumption in W/kW/MW, recorded for qualifying training days; not net grid power. |
| Battery | `sensor.battery_soc` | SOC reflects the managed household battery. |
| EV | Vehicle SOC, charging, connected and target-SOC entities | Target SOC is authoritative and uses percent. Feedback confirms commands. |
| Charger | `switch.ev_charger` or separate start/stop entities | Service behavior and feedback match the selected controls. |

These names are examples. Select entities actually supplied by your integrations.
Keep Automatic control off, enable the device areas you intend to evaluate,
review Current state and Next actions, and run the safety check. Configure
ready-by and charging preferences in the EV settings. Only enable Automatic
control once the evidence and mapped device behavior are understood.

Current state, Next actions, Plan health and the safety-check result are the
troubleshooting entry points. Production readiness is no longer a standalone
sensor. A learning model may need more than three calendar days if history is
incomplete or holdout validation does not pass.

## Services

Use these examples in Developer tools → Actions after replacing the entry ID.
Omit config_entry_id only when exactly one planner is loaded. Export and
preflight actions return response data; an automation/script can capture it
with `response_variable`.

```yaml
action: ha_energy_planner.run_preflight
data:
  config_entry_id: YOUR_PLANNER_ENTRY_ID
response_variable: planner_preflight
```

```yaml
action: ha_energy_planner.pause_control
data:
  config_entry_id: YOUR_PLANNER_ENTRY_ID
  asset: ev
  duration_minutes: 60
  reason: user_requested
```

```yaml
action: ha_energy_planner.set_ev_ready_by
data:
  config_entry_id: YOUR_PLANNER_ENTRY_ID
  ready_by: "07:30"
```

```yaml
action: ha_energy_planner.restore_safe_state
data:
  config_entry_id: YOUR_PLANNER_ENTRY_ID
  reason: manual_service_call
```

`resume_control` ends pauses. `set_manual_hvac_override` accepts a bounded
duration and reason. `export_support_bundle` returns preflight and redacted
diagnostics. All fields and limits are described in `services.yaml`. Reason
values are compact audit codes, not free-form personal information.

## Upgrade and recovery

1. Back up Home Assistant, including config entries, entity/device registries and
   integration storage. Preserve the integration files/version.
2. Prefer upgrading from 0.9.18 or newer. A synthetic 0.9.18 Store/configuration
   fixture exercises real HA setup, reload and restart. Older migration helpers
   remain supported, but every historical release is not a runtime baseline.
3. For pre-0.9.13 EV configurations that fail migration because no vehicle target
   SOC is mapped, open the entry menu → Reconfigure. Select a valid target-SOC
   entity. This repairs the original entry and retries migration. Do not
   delete/recreate it or edit `.storage` to bypass the check. Configure remains
   the normal entry point for other inputs and options.
4. Confirm original entity IDs, control intent, overrides and device feedback.
   Recovery may keep Armed off while retaining Automatic control.
5. Review the first plan and run the safety check before enabling new control.

The Store envelope remains version 1; audit normalization removes the duplicate
`outcomes` list in favor of `execution_audit`. A file-only downgrade is not a
supported rollback procedure. Restore the complete matching pre-upgrade backup
and integration version while control is disabled, then verify device state.
A backup cannot undo a command already accepted by a device.

If storage fails, correct disk capacity/permissions and retry. Failed generations
remain dirty; new acquisitions cannot rely on a failed save. Restoration commands
remain available, and persistence failures require device feedback verification
even if the service already changed state.

## Removal and retained data

Turn off Automatic control, restore safe state, and verify feedback before
deletion. Entry deletion removes its model/audit storage only when saved ownership
and reservations are resolved. Uncertain or malformed evidence is retained with
a log message. Other entries and the shared legacy import archive are not deleted.
Retained orphan storage may be removed manually only after a complete backup and
confirmation that referenced devices no longer require restoration. Do not delete
it merely to clear an error.

## Stable interface policy

- Within 1.x, live entity unique IDs and service names are compatibility contracts.
  Display labels and translations may improve without changing IDs.
- Existing valid service fields/results remain compatible within a major version.
  New optional fields can be added. Removing behavior requires release notes,
  a replacement path and at least one minor-release deprecation period before
  the next major version.
- Migrations preserve intent, user values and recovery evidence. Safety-sensitive
  omissions must be repaired rather than guessed.
- Mode states are `review`, `recovery` and `active`; Automatic control expresses
  intent and Armed expresses command authority. Automate these surfaces rather
  than parsing generated English summaries.
- Diagnostic attributes, timings, model internals and Store keys are support
  evidence, not a stable automation API. Calendar text and summaries may change.
- Raise the HA minimum only with an announced compatibility release, tested
  baseline and published upgrade instructions.

Optional AI Task providers may run remotely. Selected planning context goes to
the configured provider; its privacy, availability and cost policies apply.
The deterministic planner remains local and does not require AI.
