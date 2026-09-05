# Energy Planner specification

This is the current product contract. The [historical design](docs/archive/specification-pre-1.0.md) is retained for provenance and does not describe current configuration. [Requirements evidence](docs/requirements-audit.md) maps detailed policies to code and tests.

## Scope and configuration

Energy Planner composes existing Home Assistant tariff, solar, household load, battery, EV and climate inputs into a deterministic plan. It issues commands through mapped HA services; it has no direct vendor cloud client. Multiple EVs use separate named entries, shared grid reservations and exclusive actuator ownership.

Create a named entry, then use Configure to map inputs and policy. Planning evidence includes import/export tariff forecasts, an external PV forecast, gross instantaneous household consumption retained in Recorder, and battery SOC. Device controls need feedback and restoration inputs. Weather, measured PV, carbon intensity and AI explanation are optional.

EV planning uses the mapped vehicle target SOC, charging and connected state, and a direct charger switch or start/stop controls. Ready-by, earliest-start, opportunistic charging and daylight policy are settings. There are no writable Target SOC or Ready by entities. Legacy EV Smart Charging keys remain readable for migration compatibility.

## Planning and authority

The pure planner works with normalized snapshots; the executor rechecks current evidence before dispatch. Automatic control is operator intent; Armed is actual command authority. Review and Recovery do not grant permission to acquire control. Missing, stale or uncertain evidence fails closed. The default-off safety bypass retains its explicitly documented constraints and cannot be enabled by an AI response or restored plan.

EV scheduling respects ready-by, target SOC, battery reserve and grid limits. Opt-in daylight preference requires complete slots within sunrise and sunset and does not outrank ready-by or manual controls. Unsolicited charging is compensated when automatic EV authority is active; expected planner feedback is not treated as manual interference.

HVAC commands respect comfort, occupancy and bounded manual overrides. Rollback preserves user supersession and restores only still-owned thermostat, zone and automation state. Enphase control uses verified mapped profiles, preserving the original profile across repeated acquisitions.

## Failure and persistence

New acquisitions require an acknowledged disk write of recovery evidence. Storage failures remain dirty and retryable. Write cancellation drains before another generation proceeds. Service timeout is uncertain acceptance; ownership cannot be discarded until feedback proves a safe result. Device dispatch has a 30-second deadline per call. Confirmation and compensation have their own bounded phases, so an entire transaction can take longer.

Reload stops admission and drains in-flight transactions. Startup may retain operator intent while recovery keeps authority disarmed. Unresolved ownership and reservations survive reload and deletion cleanup. Loaded plans are not permission to replay device commands.

## Learning and AI

Recorder training runs outside the event loop with bounded history, single-flight work, source/lifetime checks and retry backoff. Readiness depends on qualifying days, coverage and holdout quality, not only elapsed time.

AI explanations receive selected planner context through a configured AI Task provider, which may be local or remote. Responses are bounded, validated and advisory; they cannot execute services or change policy. Provider failure cannot grant authority. Raw prompts and raw responses are not persisted.

## Stable interface and acceptance

[Stable contracts](docs/stable-release.md) document services, entities, examples and upgrade policy. The [release checklist](docs/release-checklist.md) requires the full Docker gate, package installation checks, upgrade/restart evidence and representative operating cycles. HACS declares the minimum HA version; pyproject declares tested versions and tools, consumed through scripts/support_policy.py.
