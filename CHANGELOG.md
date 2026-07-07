# Changelog

## 0.2.0 - 2026-07-06

### Added

- Marginal-value planning evidence across EV, climate, Enphase, solar surplus, battery reserve, and tariff value.
- Weighted device priority scoring based on the configured planning priority order.
- Battery modelling options for usable capacity, round-trip efficiency, maximum charge power, and maximum discharge power.
- Capacity- and efficiency-aware Enphase profile decisions.
- Solar-aware EV charging allocation using effective cost across surplus solar and grid import.
- Climate thermal-shift planning with comfort coasting, active heat/cool learning, and estimated preconditioning windows.
- Subsystem confidence reporting for tariff, solar, load, climate, EV, and Enphase planning.
- Per-subsystem confidence thresholds that can block low-confidence device decisions.
- Decision audit, rejected actions, upcoming timeline, and per-device Decision sensors.
- Action backoff when a device command fails.
- Conflict detection when recent planner-owned EV or Enphase state appears to have been changed externally.

### Changed

- Enphase decisions no longer rely on simple tariff spread alone. Battery and solar value now need enough usable capacity and configured savings value.
- EV charging plans prefer lower effective-cost windows and include solar/grid split details.
- Plan attributes use more plain-English summaries for accepted and rejected decisions.
- The production safety model now records clearer reasons when control is paused by failures or conflicts.

### Upgrade Notes

- Review the new **EV, battery, and grid** policy options after upgrade:
  - usable battery capacity
  - battery round-trip efficiency
  - maximum battery charge power
  - maximum battery discharge power
- Review the new **Data health** confidence thresholds. The defaults are intentionally conservative.
- Check the new Decision, Decision audit, Rejected actions, and Upcoming timeline entities before arming production control.
- Run preflight and allow several healthy dry-run cycles before enabling active device control.

### Validation

- `ruff check custom_components/ha_energy_planner tests`
- Dockerized pytest and coverage: `492 passed`, `100%` coverage
- Translation JSON validation
