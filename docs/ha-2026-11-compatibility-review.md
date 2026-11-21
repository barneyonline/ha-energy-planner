# Home Assistant 2026.11.0 compatibility review

The new minimum lets Energy Planner use Home Assistant's conditional form markers
directly for EV price settings. This source review compares the release and
contributor notes with the Core `2026.11.0` tag and Energy Planner source.
[Core release tag](https://github.com/home-assistant/core/releases/tag/2026.11.0) · [Release notes](https://www.home-assistant.io/blog/2026/11/04/release-202611/) · [Contributor changes](https://www.home-assistant.io/blog/2026/11/04/release-202611/#backward-incompatible-changes)

The config-entry ownership and refresh changes for migration repairs belong to
the existing draft PR, which depends on this minimum-version bump. [Repair ownership and refresh PR #164](https://github.com/barneyonline/ha-energy-planner/pull/164).

## Minimum-version changes

- `hacs.json` and the pinned support policy declare `2026.11.0`. The runtime
  matrix covers that exact minimum and floating stable. Home Assistant 2026.11
  moves distribution to the built-in Marketplace and migrates installed HACS
  repositories and update entities at startup. The tagged Marketplace installer
  reads `hacs.json` and checks the selected release's `homeassistant` minimum.
  A fresh install can use the catalog or add the public repository from a link
  after connecting GitHub. [Support policy](stable-release.md) · [Marketplace installation and migration](https://www.home-assistant.io/integrations/marketplace/#coming-from-hacs) · [Tagged minimum check](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/marketplace/repositories/base.py#L1849-L1889)
- The EV options use `data_entry_flow.Required(..., visible=...)` directly.
  The maximum import price and price policy follow the price cap toggle, the
  low-price threshold follows its own toggle, and the emergency price and budget
  require both the price cap and departure priority. Core 2026.11.0 enforces
  visibility on the backend and omits hidden values from submitted data. The
  planner retains their saved settings. Tests exercise schema serialization and
  Core options-flow submission on minimum and stable.
  [Planner config flow](../custom_components/ha_energy_planner/config_flow.py) · [Conditional EV runtime tests](../tests/test_conditional_ev_options_runtime.py) · [Tagged flow API](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/data_entry_flow.py) · [Contributor notes](https://contributors.home-assistant.io/blog/2026/10/21/form-fields-visible/)
- The exact pytest filters for upstream HTTP warnings still apply. At the new floor
  Core still subclasses `aiohttp.web.Application` and sets a string application
  key for `hass`. The warning notes attribute both to the supported Core
  implementation. [Warning policy](validation-warnings.md) · [Tagged HTTP server](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/http/server.py)

## Follow-up opportunities

The follow-up changes add climate unit labels and in-place indicator renames.
The operator guides now cover [Action history](stable-release.md#example-setup),
[ABRP SoC](ev-scheduling.md#charging-observations), and
[Enphase radio diagnostics](export-limit-timing.md).

1. **Show the climate temperature unit in Current state.** The diagnostic
   snapshot publishes `temperature_unit` with the reported readings and targets.
   The summary uses the live reading when the head reports a non-Celsius unit.
   A separate control
   review should cover Celsius and Fahrenheit readback, configured room targets,
   command conversion and restoration of persisted baselines. Core publishes
   current and target temperatures in the Home Assistant system unit. In the
   tagged source it converts the min/max capability values for display and
   publishes `target_temp_step` straight from the entity, so verify step handling
   independently. The planner already converts mapped room sensors to Celsius
   while parts of the adapter compare raw climate state and service values. This
   warrants tests before changing live control. [Current state sensor](../custom_components/ha_energy_planner/sensor.py) · [Climate inputs](../custom_components/ha_energy_planner/climate_inputs.py) · [HVAC adapter](../custom_components/ha_energy_planner/hvac_adapter.py) · [Tagged climate entity](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/climate/entity.py)
2. **Use Core's logs and Action history for troubleshooting.** An operator can
   open the integration's filtered logs during a reproduction and use Settings
   → Tools → Action history to correlate service calls, times, targets and
   context IDs. The history reads Recorder `call_service` events, which Core
   emits before invoking the service handler. Keep the planner calendar and
   device readback as the evidence for confirmed actions. [Release walkthrough](https://www.home-assistant.io/blog/2026/11/04/release-202611/#why-did-that-happen) · [Tagged service dispatch](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/core.py) · [Tagged action history query](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/logbook/websocket_api.py) · [Planner calendar](../custom_components/ha_energy_planner/calendar_history.py)
3. **Evaluate Daikin demand control on known hardware.** The new
   `daikin.set_demand_control` action can limit supported units to 40–100% of
   nominal power in manual mode. Automatic mode lets the unit manage the limit.
   Multiple indoor units sharing an outdoor unit can produce the least
   restrictive effective setting. An opt-in experiment could measure demand and
   comfort under price or grid constraints. Planner control would need proof of
   support, a readable original setting, confirmation and safe restoration, and
   measurement of actual electrical response. [Tagged action schema](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/daikin/services.yaml) · [Daikin action documentation](https://www.home-assistant.io/actions/daikin.set_demand_control/) · [HVAC ownership and restoration](climate-decisions.md)

   The 2026.11.0 action checks device support, writes the settings and refreshes
   the coordinator. It exposes neither a response nor a public read action for
   the enabled state, mode and limit. The bundled driver has a cached read
   method. Adding Planner control requires an upstream readback surface or
   another verified source for the original setting and subsequent confirmation.
   [Tagged action implementation](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/daikin/services.py)
4. **Document A Better Routeplanner as an optional vehicle SoC source.** Core
   adds a cloud-push integration with per-vehicle state of charge and an enum
   charging sensor. SoC can be assessed through the existing per-vehicle
   mapping. The integration may show the latest cached vehicle telemetry between
   updates, and states that distinguish AC and DC charging need separate
   normalization and evidence. Use charger-side connection and fresh delivery
   feedback for control. [Core integration change](https://github.com/home-assistant/core/pull/174058) · [ABRP telemetry documentation](https://www.home-assistant.io/integrations/abrp/#data-updates) · [EV mapping](ev-scheduling.md) · [Feedback normalization](../custom_components/ha_energy_planner/ev.py)
5. **Use supported Envoy signal sensors when troubleshooting Enphase.** Core
   adds optional Enpower and Encharge radio communication-level sensors. Where
   the equipment supplies them, they can help distinguish a weak radio link
   from a planner-side input problem. Keep them as diagnostic context, since
   export control depends on gateway identity and verified setting readback.
   [Core changelog](https://www.home-assistant.io/changelogs/core-2026.11/#enphase-envoy) · [Envoy diagnostic sensors](https://www.home-assistant.io/integrations/enphase_envoy/#enpower-sensor-entities) · [Planner export control](../custom_components/ha_energy_planner/enphase_export_limit.py)
6. **Opt the climate-control indicator into in-place entity ID changes.** Core
   2026.11 keeps remove-and-readd behavior for a custom entity class that defines
   `async_added_to_hass` without opting in. `PlannerBinarySensor` adds a store
   listener that calls `self.async_write_ha_state()` and unregisters on removal.
   The store keys the subscription by callback, independently of `entity_id`.
   The `async_entity_id_changed()` override opts in without rebinding the listener.
   The real registry test checks ownership updates under the new ID and cleanup
   on unload. Core plans to
   remove the fallback in 2027.11. [Entity lifecycle guidance](https://contributors.home-assistant.io/blog/2026/10/12/new-entity-lifecycle-hooks/#backwards-compatibility) · [Planner binary sensor](../custom_components/ha_energy_planner/binary_sensor.py) · [Store listener](../custom_components/ha_energy_planner/storage.py)

## Compatibility to preserve

| Upstream change | Energy Planner impact |
| --- | --- |
| Explicit `step_id` for forms, menus, progress and external steps | Existing forms and menus provide the step ID, including the repair form. The warning for custom integrations becomes an error in 2027.5.0. [Config flow](../custom_components/ha_energy_planner/config_flow.py) · [Core guidance](https://contributors.home-assistant.io/blog/2026/10/23/flow-results-require-step-id/) |
| Config entry lifecycle callbacks return `None` in Core integrations | Boolean results remain supported in custom integrations without deprecation. The planner uses the unload result to preserve failure and recovery behavior. A lifecycle refactor would need specific translated failure paths and tests. [Planner lifecycle](../custom_components/ha_energy_planner/__init__.py) · [Core guidance](https://contributors.home-assistant.io/blog/2026/10/23/config-entry-methods-return-none/) |
| `DeviceInfo` dataclass, Marketplace categories and child devices | The planner constructs `DeviceInfo` with keyword arguments, registers devices with named fields and already declares `energy`. Keep stable device identifiers and the existing planner/vehicle device ownership topology. [Entity base](../custom_components/ha_energy_planner/entity.py) · [Manifest](../custom_components/ha_energy_planner/manifest.json) · [DeviceInfo guidance](https://contributors.home-assistant.io/blog/2026/10/13/device-info-class/) · [Categories](https://contributors.home-assistant.io/blog/2026/10/20/integration-categories/) · [Child devices](https://contributors.home-assistant.io/blog/2026/10/22/child-devices/) |
| Entity naming | Existing IDs survive Core's naming migration. Current state uses `friendly_name` for display, and in 2026.11.0 `State.name` still reads that attribute. Consider registry-backed names and device context before changing what the diagnostic `name` field means. [Naming guidance](https://www.home-assistant.io/blog/2026/11/03/naming-things-in-home-assistant/) · [Tagged `State.name`](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/core.py) |
| New units selector and `Undefined` sentinel | The units selector helps users choose a sensor unit. Planner setup selects existing entities and validates their reported units. `Undefined` is a 2026.12 change and is absent at 2026.11.0. The `probatio.UNDEFINED` sentinel used in schema tests is independent. [Units selector](https://contributors.home-assistant.io/blog/2026/10/09/uom-selector/) · [Sentinel announcement](https://contributors.home-assistant.io/blog/2026/11/01/undefined-helper/) · [Tagged typing helpers](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/helpers/typing.py) |
| Power-to-energy helpers, statistics correction and target exclusions | Dashboard-generated energy helpers support Energy dashboard setup. The planner's gross-load input still depends on a power series, and its Recorder import uses raw state changes independently of statistics corrections. Its actuator actions use explicit, configured targets. Keep these source and control boundaries. [Energy setup](https://www.home-assistant.io/blog/2026/11/04/release-202611/#from-power-to-energy-in-one-step) · [Statistics correction](https://www.home-assistant.io/blog/2026/11/04/release-202611/#remove-bad-readings-from-your-history) · [Target exclusions](https://www.home-assistant.io/blog/2026/11/04/release-202611/#exclude-items-from-a-target) · [Recorder import](../custom_components/ha_energy_planner/recorder_import.py) · [Control adapters](../custom_components/ha_energy_planner/adapter_helpers.py) |

A Core version floor governs available APIs. Persisted planner data, config
entries and subentries, incoming forecast shapes, and the capabilities of mapped
devices have their own lifecycles. Keep the existing migrations and fail-closed
runtime checks when dropping the 2026.10-only form path.
[Subentry migration](../custom_components/ha_energy_planner/subentry_migration.py) · [Forecast inputs](../custom_components/ha_energy_planner/inputs.py) · [Preflight](../custom_components/ha_energy_planner/preflight.py)
