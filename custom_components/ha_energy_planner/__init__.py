"""Energy Planner custom integration."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .const import (
    ATTR_ASSET,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION_MINUTES,
    ATTR_READY_BY,
    ATTR_REASON,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BASELINE_LOAD_OBSERVED,
    CONF_DEFAULT_READY_BY,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC_PER_KWH,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_HOUSEHOLD_LOAD,
    CONF_HOUSEHOLD_LOAD_OUTAGE_GRACE_MINUTES,
    CONF_INSTANCE_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    EV_RESERVATION_EXTERNAL_BASELINE,
    EV_RESERVATION_EXTERNAL_POWER_OPTIONS,
    EV_RESERVATION_EXTERNAL_TOPOLOGY,
    EV_RESERVATION_HANDOFF_PENDING,
    EV_RESERVATION_RETAIN_WHEN_UNLOADED,
    EV_SIGNAL_CLOCK_HANDOFFS,
    INTEGRATION_NAME,
    LEGACY_INTEGRATION_NAME,
    PLATFORMS,
    SERVICE_ARM_PRODUCTION_CONTROL,
    SERVICE_DISARM_PRODUCTION_CONTROL,
    SERVICE_EXPORT_DIAGNOSTICS,
    SERVICE_EXPORT_SUPPORT_BUNDLE,
    SERVICE_PAUSE_CONTROL,
    SERVICE_REPLAN,
    SERVICE_RESTORE_SAFE_STATE,
    SERVICE_RESUME_CONTROL,
    SERVICE_RUN_PREFLIGHT,
    SERVICE_SET_EV_READY_BY,
    SERVICE_SET_MANUAL_HVAC_OVERRIDE,
)
from .entry_data import combined_entry_data, remove_retired_config_keys
from .ev_policy import power_limit_options
from .ev_runtime import (
    EV_CAP_FEEDBACK_CLOCK_INVALID,
    EV_RETIRED_CAP_KW,
    MAX_EV_CAP_HISTORY,
    check_ev_cap_feedback_clock,
    invalidate_ev_cap_feedback_clock,
    timestamp,
)
from .migration_recovery import (
    async_clear_migration_issue,
    async_create_migration_issue,
    async_refresh_migration_issues,
)
from .type_defs import EnergyPlannerConfigEntry
from .vehicles import VEHICLE, VEHICLES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .coordinator import EnergyPlannerCoordinator
    from .models import ActionOutcome

def _config_schema(config: dict[str, Any]) -> dict[str, Any]:
    """Declare UI-only setup while keeping standalone planner tools importable."""
    from homeassistant.helpers import config_validation as cv

    return cv.config_entry_only_config_schema(DOMAIN)(config)


CONFIG_SCHEMA = _config_schema

_LOGGER = logging.getLogger(__name__)

_REASON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_DUPLICATE_ENTITY_ID_MIGRATIONS = {"switch.ai_ai_enabled": "switch.ai_enabled"}
_LEGACY_DEFAULT_EV_SOC_PER_KWH = 5.0


async def async_migrate_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> bool:
    """Migrate measured-load configuration without trusting forecast entities."""
    version = getattr(entry, "version", 1)
    if version > 5:
        return False
    data = dict(entry.data)
    if not data.get(CONF_HOUSEHOLD_LOAD) and data.get(CONF_BASELINE_LOAD_OBSERVED):
        data[CONF_HOUSEHOLD_LOAD] = data[CONF_BASELINE_LOAD_OBSERVED]
    data.pop(CONF_BASELINE_LOAD_FORECAST, None)
    data.pop(CONF_BASELINE_LOAD_OBSERVED, None)
    options = dict(entry.options)
    try:
        uses_legacy_ev_rate = (
            float(options.get(CONF_EV_SOC_PER_KWH, _LEGACY_DEFAULT_EV_SOC_PER_KWH))
            == _LEGACY_DEFAULT_EV_SOC_PER_KWH
        )
    except (TypeError, ValueError):
        uses_legacy_ev_rate = False
    if version < 3 and uses_legacy_ev_rate:
        options[CONF_EV_SOC_PER_KWH] = DEFAULT_OPTIONS[CONF_EV_SOC_PER_KWH]
    if version < 4:
        if _legacy_ev_configuration_requires_target(entry):
            from homeassistant.exceptions import ConfigEntryError

            async_create_migration_issue(hass, entry)
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="legacy_vehicle_target")
        options.pop(CONF_EV_FALLBACK_TARGET_SOC_PERCENT, None)
    if version < 5:
        options.pop(CONF_EV_MAX_SOC_PERCENT, None)
        options.pop(CONF_EV_MIN_SOC_PERCENT, None)
    if getattr(entry, "minor_version", 1) < 2:
        from .ev_policy import finite

        grace = finite(options.get(CONF_HOUSEHOLD_LOAD_OUTAGE_GRACE_MINUTES))
        if grace is not None and 0 < grace < 360:
            options[CONF_HOUSEHOLD_LOAD_OUTAGE_GRACE_MINUTES] = 360
    hass.config_entries.async_update_entry(entry, data=data, options=options, version=5, minor_version=2)
    async_clear_migration_issue(hass, entry.entry_id)
    return True


def _legacy_ev_configuration_requires_target(entry: EnergyPlannerConfigEntry) -> bool:
    """Return whether a configured legacy EV is missing its vehicle target entity."""
    sections = [dict(getattr(entry, "data", {}))]
    sections.extend(
        dict(getattr(subentry, "data", {}))
        for subentry in getattr(entry, "subentries", {}).values()
    )
    target_configured = any(section.get(CONF_EV_SMART_CHARGING_TARGET_SOC) for section in sections)
    ev_configured = any(
        value
        for section in sections
        for key, value in section.items()
        if str(key).startswith("ev_") and key != CONF_EV_SMART_CHARGING_TARGET_SOC
    )
    return ev_configured and not target_configured


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level services."""
    import probatio as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
    from homeassistant.helpers import config_validation as cv

    from .coordinator import EnergyPlannerCoordinator
    from .diagnostics import async_get_config_entry_diagnostics
    from .preflight import build_preflight_report

    async_refresh_migration_issues(hass)
    await _async_rehydrate_all_ev_grid_reservations(hass)

    async def _require_coordinator(call: ServiceCall) -> EnergyPlannerCoordinator:
        """Return the loaded coordinator or raise a translated service error."""
        loaded = [
            (str(getattr(entry, "entry_id", getattr(coordinator.entry, "entry_id", ""))), coordinator)
            for entry in hass.config_entries.async_entries(DOMAIN)
            if isinstance((coordinator := getattr(entry, "runtime_data", None)), EnergyPlannerCoordinator)
        ]
        requested_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if requested_entry_id:
            for entry_id, coordinator in loaded:
                if entry_id == requested_entry_id:
                    return coordinator
            raise ServiceValidationError(
                "The selected Energy Planner configuration is not loaded.",
                translation_domain=DOMAIN,
                translation_key="config_entry_not_found",
            )
        if not loaded:
            raise ServiceValidationError(
                "No loaded Energy Planner configuration is available.",
                translation_domain=DOMAIN,
                translation_key="no_config_entry",
            )
        if len(loaded) > 1:
            raise ServiceValidationError(
                "Multiple Energy Planner configurations are loaded; select config_entry_id.",
                translation_domain=DOMAIN,
                translation_key="config_entry_required",
            )
        return loaded[0][1]

    def _config_entry_field() -> dict[Any, Any]:
        return {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}

    async def handle_replan(call: ServiceCall) -> None:
        coordinator = await _require_coordinator(call)
        await coordinator.async_request_replan()

    async def handle_resume_climate(call: ServiceCall) -> dict[str, Any]:
        coordinator = await _require_coordinator(call)
        return await coordinator.async_resume_climate_planning()

    async def handle_restore(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "manual_service_call"))
        coordinator = await _require_coordinator(call)
        outcome = await coordinator.async_restore_safe_state(reason)
        if outcome.result.value == "failed":
            raise HomeAssistantError(
                f"Energy Planner could not fully restore safe state: {outcome.reason}",
                translation_domain=DOMAIN,
                translation_key="restore_safe_state_failed",
                translation_placeholders={"reason": outcome.reason},
            )

    async def handle_ready_by(call: ServiceCall) -> None:
        ready_by = str(call.data[ATTR_READY_BY])
        coordinator = await _require_coordinator(call)
        await coordinator.async_set_ready_by(ready_by)

    async def handle_charge_now(call: ServiceCall) -> dict[str, Any]:
        coordinator = await _require_coordinator(call)
        result = await coordinator.async_charge_now(int(call.data[ATTR_DURATION_MINUTES]))
        if getattr(result, "reason", None) == "ev_vehicle_suspended" and (
            not result.command_sent or result.safe_state_confirmed is True
        ):
            return {"status": "waiting_for_vehicle", "reason": result.reason}
        if not result.applied:
            raise HomeAssistantError(
                f"Charge now is blocked: {result.reason}. Capacity and charger safety checks remain active.",
                translation_domain=DOMAIN, translation_key="ev_charge_now_blocked",
                translation_placeholders={"reason": result.reason},
            )
        return {"status": "charging", "duration_minutes": call.data[ATTR_DURATION_MINUTES]}

    async def handle_cancel_charge_now(call: ServiceCall) -> None:
        coordinator = await _require_coordinator(call)
        result = await coordinator.async_cancel_charge_now()
        if not result.applied:
            raise HomeAssistantError(f"Charging could not be confirmed stopped: {result.reason}")

    async def handle_manual_override(call: ServiceCall) -> None:
        duration = int(call.data[ATTR_DURATION_MINUTES])
        reason = str(call.data.get(ATTR_REASON, "manual_service_call"))
        coordinator = await _require_coordinator(call)
        outcome = await coordinator.async_set_manual_hvac_override(duration, reason)
        if outcome is not None and outcome.result.value == "failed":
            raise HomeAssistantError(
                f"The manual HVAC override was set, but climate control could not be fully released: {outcome.reason}",
                translation_domain=DOMAIN,
                translation_key="restore_safe_state_failed",
                translation_placeholders={"reason": outcome.reason},
            )

    async def handle_export_diagnostics(call: ServiceCall) -> dict[str, Any]:
        coordinator = await _require_coordinator(call)
        return await async_get_config_entry_diagnostics(hass, coordinator.entry)

    async def handle_run_preflight(call: ServiceCall) -> dict[str, Any]:
        coordinator = await _require_coordinator(call)
        return build_preflight_report(hass, coordinator, require_export_write_gate=True)

    async def handle_export_support_bundle(call: ServiceCall) -> dict[str, Any]:
        coordinator = await _require_coordinator(call)
        return {
            "preflight": build_preflight_report(hass, coordinator, require_export_write_gate=True),
            "diagnostics": await async_get_config_entry_diagnostics(hass, coordinator.entry),
        }

    async def handle_arm_production(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_acknowledged"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_operator_arm_production_control(reason)

    async def handle_disarm_production(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_operator_disarm_production_control(reason)

    async def handle_pause_control(call: ServiceCall) -> None:
        duration = int(call.data[ATTR_DURATION_MINUTES])
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        asset = str(call.data.get(ATTR_ASSET, "all"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_pause_control(duration, reason, asset)

    async def handle_resume_control(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        coordinator = await _require_coordinator(call)
        asset = str(call.data.get(ATTR_ASSET, "all"))
        if asset == "all":
            await coordinator.async_resume_control(reason)
        else:
            await coordinator.async_resume_control(reason, asset=asset)

    hass.services.async_register(
        DOMAIN,
        SERVICE_REPLAN,
        handle_replan,
        schema=vol.Schema(_config_entry_field()),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_SAFE_STATE,
        handle_restore,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Optional(ATTR_REASON, default="manual_service_call"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EV_READY_BY,
        handle_ready_by,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Required(ATTR_READY_BY): vol.All(cv.string, _validate_ready_by_time),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, "charge_now", handle_charge_now,
        schema=vol.Schema({**_config_entry_field(),
                           vol.Optional(ATTR_DURATION_MINUTES, default=60):
                           vol.All(vol.Coerce(int), vol.Range(min=1, max=240))}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "cancel_charge_now", handle_cancel_charge_now,
        schema=vol.Schema(_config_entry_field()),
    )
    hass.services.async_register(
        DOMAIN, "resume_climate_planning", handle_resume_climate,
        schema=vol.Schema(_config_entry_field()), supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MANUAL_HVAC_OVERRIDE,
        handle_manual_override,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Required(ATTR_DURATION_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Optional(ATTR_REASON, default="manual_service_call"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_DIAGNOSTICS,
        handle_export_diagnostics,
        schema=vol.Schema(_config_entry_field()),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_PREFLIGHT,
        handle_run_preflight,
        schema=vol.Schema(_config_entry_field()),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_SUPPORT_BUNDLE,
        handle_export_support_bundle,
        schema=vol.Schema(_config_entry_field()),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ARM_PRODUCTION_CONTROL,
        handle_arm_production,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Optional(ATTR_REASON, default="user_acknowledged"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DISARM_PRODUCTION_CONTROL,
        handle_disarm_production,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Optional(ATTR_REASON, default="user_requested"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PAUSE_CONTROL,
        handle_pause_control,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Required(ATTR_DURATION_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=1, max=10080)),
                vol.Optional(ATTR_ASSET, default="all"): vol.In(
                    ["all", "ev", "daikin", "enphase", "enphase_export_limit"]),
                vol.Optional(ATTR_REASON, default="user_requested"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_CONTROL,
        handle_resume_control,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Optional(ATTR_ASSET, default="all"): vol.In(
                    ["all", "ev", "daikin", "enphase", "enphase_export_limit"]),
                vol.Optional(ATTR_REASON, default="user_requested"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> bool:
    """Set up Energy Planner from a config entry."""
    from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

    from .coordinator import EnergyPlannerCoordinator, InitialPlanSuperseded
    from .storage import PlannerStore
    from .subentry_migration import async_migrate_subentries_to_entry_data

    if not entry.options:
        hass.config_entries.async_update_entry(entry, options=DEFAULT_OPTIONS)
    if getattr(entry, "title", None) == LEGACY_INTEGRATION_NAME:
        hass.config_entries.async_update_entry(entry, title=INTEGRATION_NAME)
    async_migrate_subentries_to_entry_data(hass, entry)
    domain_entries = hass.config_entries.async_entries(DOMAIN)
    legacy_store_entry_id = _legacy_store_owner_entry_id(domain_entries)
    store = PlannerStore(
        hass,
        entry.entry_id,
        legacy_fallback=entry.entry_id == legacy_store_entry_id,
    )
    await store.async_load()
    ownership = dict(store.data.get("ownership", {}))
    topology = ownership.get("ev_smart_charging_control_topology")
    if isinstance(topology, dict):
        migrated_topology = remove_retired_config_keys(topology)
        if migrated_topology != topology:
            await store.async_save_ownership({**ownership, "ev_smart_charging_control_topology": migrated_topology})
    retained_handoff = _rehydrate_ev_grid_reservation(hass, entry, getattr(store, "data", {}))
    coordinator = EnergyPlannerCoordinator(hass, entry, store)
    coordinator.entry_topology_signature = _entry_topology_signature(entry)
    entry.runtime_data = coordinator
    forwarding_started = False
    try:
        with ExitStack() as initial_feedback:
            external_ev = store.data.get("ev_grid_reservation", {})
            mapped_external_ev = (
                external_ev.get(EV_RESERVATION_EXTERNAL_BASELINE) is True
                and isinstance(external_ev.get(EV_RESERVATION_EXTERNAL_TOPOLOGY), dict)
            )
            initial_load_increased = False
            if mapped_external_ev or "ev_power_limit_entity" in ownership.get("ev_smart_charging_state", {}):
                initial_load_increased = coordinator.observe_initial_ev_feedback(initial_feedback)
            if retained_handoff:
                try:
                    async with coordinator._command_lock:
                        await coordinator.executor.async_persist_ev_grid_reservation()
                except (OSError, HomeAssistantError) as error:
                    raise ConfigEntryNotReady("Could not save the retained EV reservation") from error
                retained_handoff = False
            elif mapped_external_ev and initial_load_increased:
                # Other entries can reserve shared headroom while the first refresh waits.
                async with coordinator._command_lock:
                    await coordinator.executor.async_persist_ev_grid_reservation()
            await coordinator.async_retry_initial_hvac_handoff()
            await coordinator.async_reconcile_production_evidence_contract()
            await coordinator.async_config_entry_first_refresh()
            coordinator.async_start_listeners()
        coordinator.async_start_startup_auto_recovery()
        forwarding_started = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _async_sync_planner_device(hass, entry)
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        entry.async_on_unload(coordinator.async_shutdown)
    except (Exception, asyncio.CancelledError) as error:
        # Keep cleanup alive if Home Assistant cancels setup while platforms or
        # recovery are starting. Do not cancel a transaction with external effects.
        coordinator._tearing_down = True
        preserve_control = not forwarding_started and (
            retained_handoff or isinstance(error.__cause__, InitialPlanSuperseded)
        )
        _retain_ev_signal_clocks_for_reload(coordinator)
        cleanup = asyncio.create_task(
            _async_cleanup_failed_setup(hass, entry, coordinator, forwarding_started, preserve_control=preserve_control)
        )
        from .task_lifecycle import async_finish_runtime_cleanup

        await async_finish_runtime_cleanup(cleanup)
        raise
    async_clear_migration_issue(hass, entry.entry_id)
    return True


def _retain_ev_signal_clocks_for_reload(coordinator: EnergyPlannerCoordinator) -> None:
    retain = getattr(getattr(coordinator, "executor", None), "retain_ev_signal_clock_handoff", None)
    if callable(retain):
        try:
            retain()
        except (RuntimeError, ValueError):
            _LOGGER.exception("Could not observe retained EV feedback during runtime handoff")


async def _async_cleanup_failed_setup(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    coordinator: EnergyPlannerCoordinator,
    forwarding_started: bool,
    *,
    preserve_control: bool,
) -> None:
    """Attempt every cleanup step without masking the original setup failure."""
    from .notifications import cancel_deferred_notifications_for_entry

    steps: list[Callable[[], Awaitable[object]]] = []
    if not preserve_control:
        steps.append(lambda: coordinator.async_cancel_startup_auto_recovery("setup_entry_failed"))
    steps.extend([
        coordinator.async_shutdown,
        coordinator.async_wait_for_plan_execution,
        coordinator.async_wait_for_refresh_shutdown,
    ])
    # A superseded first plan grants no command authority and setup has not
    # started recovery. Preserve the existing handoff for Core's next attempt.
    if not preserve_control:
        steps.extend([
            lambda: coordinator.async_disarm_production_control("setup_entry_failed"),
            lambda: coordinator.async_restore_safe_state("setup_entry_failed", refresh=False),
        ])
    if forwarding_started:
        steps.append(lambda: hass.config_entries.async_unload_platforms(entry, PLATFORMS))
    try:
        for step in steps:
            try:
                await step()
            except (Exception, asyncio.CancelledError):
                _LOGGER.exception("Energy Planner setup cleanup step failed")
    finally:
        # A draining device transaction may defer a notification after shutdown
        # first removed callbacks. Release these final references as well.
        cancel_deferred_notifications_for_entry(hass, entry.entry_id)
        _retain_ev_signal_clocks_for_reload(coordinator)
        if getattr(coordinator, "_ev_capacity_flush_needed", False):
            coordinator.executor.retain_ev_grid_handoff()
        object.__delattr__(entry, "runtime_data")


async def async_unload_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> bool:
    """Unload a config entry."""
    from homeassistant.core import CoreState
    from homeassistant.exceptions import HomeAssistantError

    from .task_lifecycle import async_finish_runtime_cleanup

    coordinator = entry.runtime_data
    whole_system_shutdown = getattr(hass, "state", CoreState.running) in {
        CoreState.stopping,
        CoreState.final_write,
        CoreState.stopped,
    }
    configuration_reload_handoff = bool(
        getattr(coordinator, "_configuration_reload_handoff", False)
    )
    preserve_automatic_state = bool(
        configuration_reload_handoff
        or (
            whole_system_shutdown
            and getattr(coordinator, "automatic_control_requested", False)
        )
    )
    # Stop new listener/timer work before waiting for any in-flight planner
    # execution. The coordinator's teardown marker also prevents an already
    # queued refresh from committing a new device command after restoration.
    unload_completed = False
    listeners_stopped = False
    disarm_started = False
    recovery_was_authorized = getattr(coordinator, "_startup_auto_recovery_authorized", False)
    observations = ExitStack()

    async def finish_unload() -> None:
        nonlocal unload_completed
        for _ in range(3):
            if not getattr(coordinator, "_ev_capacity_flush_needed", False):
                break
            flush = asyncio.create_task(coordinator.async_wait_for_refresh_shutdown())
            try:
                # The platforms have unloaded. Complete the Core lifecycle even if its caller cancels.
                await async_finish_runtime_cleanup(flush)
            except (OSError, HomeAssistantError) as error:
                _LOGGER.warning("Could not save retained EV feedback at entry handoff (%s)", type(error).__name__)
        observations.close()
        if configuration_reload_handoff:
            _retain_ev_signal_clocks_for_reload(coordinator)
        if getattr(coordinator, "_ev_capacity_flush_needed", False):
            coordinator.executor.retain_ev_grid_handoff()
        object.__delattr__(entry, "runtime_data")
        unload_completed = True

    async def unload_platforms() -> bool:
        platforms = asyncio.create_task(hass.config_entries.async_unload_platforms(entry, PLATFORMS))
        await async_finish_runtime_cleanup(platforms)
        if not platforms.result():
            return False
        await finish_unload()
        return True

    try:
        if preserve_automatic_state:
            await coordinator.async_cancel_startup_auto_recovery(
                "configuration_reload" if configuration_reload_handoff else "home_assistant_shutdown",
                preserve_control=True,
            )
        else:
            await coordinator.async_cancel_startup_auto_recovery("entry_unload", preserve_control=True)
        observe_ev_unload = getattr(getattr(coordinator, "executor", None), "observe_ev_unload", None)
        if callable(observe_ev_unload):
            observe_ev_unload(observations, lambda increased: coordinator._schedule_ev_capacity_feedback(
                invalidate=increased, refresh=increased, retain_during_shutdown=True,
            ), preserve_control=preserve_automatic_state)
        # Core invokes the irreversible coordinator shutdown only after a successful unload.
        coordinator._begin_shutdown()
        listeners_stopped = True
        await coordinator.async_wait_for_plan_execution()
        await coordinator.async_wait_for_refresh_shutdown()
        if preserve_automatic_state:
            handoff_pending = configuration_reload_handoff and getattr(coordinator, "hvac_handoff_unresolved", False)
            return False if handoff_pending else await unload_platforms()
        disarm_started = True
        terminal_cleanup = asyncio.create_task(_async_disarm_and_restore_for_unload(coordinator))
        if await async_finish_runtime_cleanup(terminal_cleanup):
            raise asyncio.CancelledError
        restore_outcome = terminal_cleanup.result()
        store_data = getattr(getattr(coordinator, "store", None), "data", {})
        remaining_ownership = store_data.get("ownership") if isinstance(store_data, Mapping) else None
        ev_reservation = store_data.get("ev_grid_reservation") if isinstance(store_data, Mapping) else None
        unresolved_restore = bool(remaining_ownership) or bool(
            isinstance(ev_reservation, Mapping) and ev_reservation.get("active") is True
        )
        if (
            getattr(getattr(restore_outcome, "result", None), "value", None) in {"failed", "pending"}
            and unresolved_restore
        ):
            await coordinator.async_disarm_production_control("entry_unload_restore_failed")
            return False
        unload_ok = await unload_platforms()
        if not unload_ok:
            await coordinator.async_disarm_production_control("entry_platform_unload_failed")
        return unload_ok
    finally:
        observations.close()
        if not unload_completed and entry.runtime_data is coordinator and not whole_system_shutdown:
            if configuration_reload_handoff:
                coordinator._configuration_reload_handoff = False
            if listeners_stopped:
                coordinator.async_start_listeners()
            if configuration_reload_handoff or (recovery_was_authorized and not disarm_started):
                cleanup = asyncio.create_task(_async_resume_recovery_after_failed_unload(entry, coordinator))
                if await async_finish_runtime_cleanup(cleanup):
                    raise asyncio.CancelledError


async def _async_disarm_and_restore_for_unload(coordinator: EnergyPlannerCoordinator) -> ActionOutcome:
    """Complete safe-state restoration after accepting an ordinary unload."""
    try:
        await coordinator.async_cancel_startup_auto_recovery("entry_unload", disarm_reason="entry_unload")
    finally:
        outcome = await coordinator.async_restore_safe_state("entry_unload", refresh=False)
    return outcome


async def _async_resume_recovery_after_failed_unload(
    entry: EnergyPlannerConfigEntry, coordinator: EnergyPlannerCoordinator,
) -> None:
    """Recheck recovery evidence before resuming an interrupted runtime."""
    operator_disarm = getattr(coordinator, "_operator_disarm_request", None)
    failure: Exception | None
    for _ in range(2):
        try:
            await coordinator.async_reconcile_production_evidence_contract()
        except Exception as error:
            failure = error
        else:
            failure = None
        if (
            entry.runtime_data is not coordinator or getattr(coordinator, "_tearing_down", False)
            or operator_disarm is not getattr(coordinator, "_operator_disarm_request", None)
        ):
            return
        if failure is None:
            coordinator.async_start_startup_auto_recovery()
            return
    _LOGGER.warning("Could not resume Energy Planner recovery after failed unload (%s)", type(failure).__name__)
    await coordinator._async_fail_startup_recovery()


async def async_remove_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove resolved per-entry history; retain uncertain recovery evidence."""
    from .notifications import async_dismiss_entry_notifications
    from .storage import PlannerStore

    async_clear_migration_issue(hass, entry.entry_id)
    handoff = hass.data.get(DOMAIN, {}).get(EV_SIGNAL_CLOCK_HANDOFFS, {}).pop(entry.entry_id, None)
    if handoff is not None:
        handoff.close()
    if not await PlannerStore(hass, entry.entry_id).async_remove_if_safe():
        _LOGGER.warning("Energy Planner retained recovery storage for removed entry %s", entry.entry_id)
    else:
        await async_dismiss_entry_notifications(hass, entry.entry_id)


async def _async_rehydrate_all_ev_grid_reservations(hass: HomeAssistant) -> None:
    """Restore conservative EV reservations before config entries can execute."""
    from .storage import PlannerStore

    entries = hass.config_entries.async_entries(DOMAIN)
    legacy_store_entry_id = _legacy_store_owner_entry_id(entries)
    for entry in entries:
        if getattr(entry, "runtime_data", None) is not None:
            continue
        store = PlannerStore(
            hass,
            entry.entry_id,
            legacy_fallback=entry.entry_id == legacy_store_entry_id,
        )
        await store.async_load()
        _rehydrate_ev_grid_reservation(hass, entry, store.data)


def _legacy_store_owner_entry_id(entries: list[Any]) -> str | None:
    """Return the first actual legacy entry eligible for global-store import."""
    for entry in entries:
        if not bool(getattr(entry, "data", {}).get(CONF_INSTANCE_NAME)):
            return str(getattr(entry, "entry_id", "")) or None
    return None


def _rehydrate_ev_grid_reservation(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    store_data: dict[str, Any],
) -> bool:
    """Restore persisted load and report an unsaved handoff for this entry."""
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return False
    restored_at = datetime.now(UTC)
    domain_data = hass_data.get(DOMAIN)
    if isinstance(domain_data, dict):
        reservations = domain_data.get("ev_grid_reservations")
        if isinstance(reservations, dict) and isinstance(live := reservations.get(entry.entry_id), dict):
            _, clock_changed = check_ev_cap_feedback_clock(live, restored_at)
            if clock_changed:
                live[EV_RESERVATION_HANDOFF_PENDING] = True
            return live.get(EV_RESERVATION_HANDOFF_PENDING) is True
    ownership = store_data.get("ownership")
    persisted_reservation = store_data.get("ev_grid_reservation")
    if (
        isinstance(persisted_reservation, dict)
        and persisted_reservation.get("active") is False
    ):
        return False
    has_owned_ev_state = isinstance(ownership, dict) and bool(
        ownership.get("ev_smart_charging_state")
    )
    has_persisted_reservation = isinstance(persisted_reservation, dict) and (
        persisted_reservation.get("active") is True or bool(persisted_reservation)
    )
    if not has_owned_ev_state and not has_persisted_reservation:
        return False
    domain_data = hass_data.setdefault(DOMAIN, {})
    if not isinstance(domain_data, dict):
        return False
    reservations = domain_data.setdefault("ev_grid_reservations", {})
    if not isinstance(reservations, dict):
        return False
    options = {**DEFAULT_OPTIONS, **dict(getattr(entry, "options", {}))}
    persisted_load_kw = (
        _non_negative_finite_float(persisted_reservation.get("load_kw"))
        if isinstance(persisted_reservation, dict)
        else 0.0
    )
    reservation = {
        "load_kw": max(
            _non_negative_finite_float(options[CONF_EV_CHARGE_RATE_KW]),
            persisted_load_kw,
        ),
        "limit_kw": _non_negative_finite_float(options[CONF_GRID_IMPORT_LIMIT_KW]),
        "reserved_at": restored_at.isoformat(),
        EV_RESERVATION_RETAIN_WHEN_UNLOADED: True,
    }
    if (
        isinstance(persisted_reservation, dict)
        and persisted_reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is True
    ):
        reservation[EV_RESERVATION_EXTERNAL_BASELINE] = True
        topology = persisted_reservation.get(EV_RESERVATION_EXTERNAL_TOPOLOGY)
        if isinstance(topology, dict):
            reservation[EV_RESERVATION_EXTERNAL_TOPOLOGY] = dict(topology)
            power_options = persisted_reservation.get(EV_RESERVATION_EXTERNAL_POWER_OPTIONS)
            if isinstance(power_options, dict):
                reservation[EV_RESERVATION_EXTERNAL_POWER_OPTIONS] = power_limit_options(power_options)
            observed_load_kw = _non_negative_finite_float(persisted_reservation.get("observed_load_kw"))
            observed_limit_kw = _non_negative_finite_float(persisted_reservation.get("observed_limit_kw"))
            reservation.update(observed_load_kw=observed_load_kw, observed_limit_kw=observed_limit_kw)
            # A replacement charger's configured rate is not evidence about
            # the external charger that produced this reservation.
            external_load_kw = max(persisted_load_kw, observed_load_kw, observed_limit_kw)
            reservation["load_kw"] = external_load_kw or reservation["load_kw"]
    if isinstance(persisted_reservation, dict):
        _restore_ev_cap_dispute(reservation, persisted_reservation, restored_at)
    current = reservations.setdefault(entry.entry_id, reservation)
    return current.get(EV_RESERVATION_HANDOFF_PENDING) is True


def _restore_ev_cap_dispute(reservation: dict[str, Any], persisted: dict[str, Any], restored_at: datetime) -> None:
    """Recover the last reversible cap proof without replaying a pending command."""
    if (settled_cap := _non_negative_finite_float(persisted.get("confirmed_cap_kw"))) > 0:
        reservation["confirmed_cap_kw"] = settled_cap
    if (activity_at := timestamp(persisted.get("observed_activity_at"))) is not None:
        reservation["observed_activity_at"] = activity_at.isoformat()
    last_report = timestamp(persisted.get("observed_load_at"))
    report_kw = _non_negative_finite_float(persisted.get("observed_report_kw"))
    if last_report is not None and (
        report_kw or _non_negative_finite_float(persisted.get("observed_load_kw"))
        or _non_negative_finite_float(persisted.get("load_kw"))
    ):
        reservation["observed_load_at"] = last_report.isoformat()
        if report_kw:
            reservation["observed_report_kw"] = report_kw
    reduction = persisted.get("cap_reduction")
    proof_in_future = False
    if isinstance(reduction, dict):
        decoded = _decode_ev_cap_proof(reduction, timestamp(persisted.get("confirmed_power_at")))
        if decoded is None:
            reservation["load_kw"] = max(
                reservation["load_kw"], _non_negative_finite_float(reduction.get("prior_load_kw")),
                _non_negative_finite_float(reduction.get("retained_load_kw")),
            )
        else:
            history = reduction.get("history")
            if isinstance(history, list):
                decoded["history"] = [
                    proof for item in history[:MAX_EV_CAP_HISTORY]
                    if (proof := _decode_ev_cap_proof(item)) is not None
                ]
            # Keep historical evidence for disputes without reinstating a confirmed limit on startup.
            reservation["cap_reduction"] = decoded
            proof_in_future = any(
                proof.get(EV_CAP_FEEDBACK_CLOCK_INVALID) is not True
                and (confirmed_at := timestamp(proof.get("confirmed_at"))) is not None and confirmed_at > restored_at
                for proof in (decoded, *decoded.get("history", ()))
            )
    pending = persisted.get("pending_power_limit")
    command_at = timestamp(pending.get("at")) if isinstance(pending, dict) else None
    retired_cap = _non_negative_finite_float(persisted.get(EV_RETIRED_CAP_KW))
    if isinstance(pending, dict):
        retired_cap = _non_negative_finite_float(pending.get("physical_power_kw")) or retired_cap
    if retired_cap:
        reservation[EV_RETIRED_CAP_KW] = retired_cap
    if proof_in_future or (command_at is not None and command_at > restored_at):
        if isinstance(pending, dict):
            reservation["pending_power_limit"] = dict(pending)
        invalidate_ev_cap_feedback_clock(reservation)
        reservation[EV_RESERVATION_HANDOFF_PENDING] = True


def _decode_ev_cap_proof(raw: Any, fallback_time: datetime | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cap = _non_negative_finite_float(raw.get("cap_kw"))
    confirmed_at = timestamp(raw.get("confirmed_at")) or fallback_time
    pending = raw.get("pending_limit")
    if confirmed_at is None or not cap or not isinstance(pending, dict):
        return None
    prior_confirmation = timestamp(raw.get("prior_confirmation_at"))
    decoded = {
        "prior_load_kw": _non_negative_finite_float(raw.get("prior_load_kw")),
        "retained_load_kw": _non_negative_finite_float(raw.get("retained_load_kw")),
        "cap_kw": cap,
        "confirmed_at": confirmed_at.isoformat(),
        "prior_confirmation_at": prior_confirmation.isoformat() if prior_confirmation is not None else None,
        "prior_confirmed_cap_kw": _non_negative_finite_float(raw.get("prior_confirmed_cap_kw")) or None,
        "pending_limit": dict(pending),
        "disputed_load_kw": _non_negative_finite_float(raw.get("disputed_load_kw")),
    }
    if raw.get(EV_CAP_FEEDBACK_CLOCK_INVALID) is True:
        decoded[EV_CAP_FEEDBACK_CLOCK_INVALID] = True
    return decoded


def _non_negative_finite_float(value: Any) -> float:
    """Return a finite non-negative float for lifecycle reservation recovery."""
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return numeric if numeric >= 0 and numeric < float("inf") else 0.0


async def _async_update_listener(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Handle options and subentry updates."""
    while (coordinator := getattr(entry, "runtime_data", None)) is not None:
        # Data and options writes can queue separate callbacks before either
        # completes its reload. Only one may prepare and reload this runtime.
        async with coordinator.entry_update_lock:
            if getattr(entry, "runtime_data", None) is not coordinator:
                # The preceding callback replaced the coordinator. Recheck
                # the current topology instead of unloading its recovery task
                # with a handoff prepared on the discarded coordinator.
                continue
            topology_signature = _entry_topology_signature(entry)
            previous_topology_signature = getattr(coordinator, "entry_topology_signature", None)
            if previous_topology_signature is not None and topology_signature != previous_topology_signature:
                prepare_reload = getattr(coordinator, "async_prepare_configuration_reload", None)
                if callable(prepare_reload):
                    await prepare_reload()
                await hass.config_entries.async_reload(entry.entry_id)
                return
            handle_zone_mapping_update = getattr(coordinator, "async_handle_zone_mapping_update", None)
            if callable(handle_zone_mapping_update):
                await handle_zone_mapping_update()
            handle_options_update = getattr(coordinator, "async_handle_options_update", None)
            if callable(handle_options_update):
                await handle_options_update()
                handle_vehicle_update = getattr(coordinator, "async_handle_vehicle_settings_update", None)
                if callable(handle_vehicle_update):
                    await handle_vehicle_update()
                return
            request_replan = getattr(coordinator, "async_request_replan", None)
            if callable(request_replan):
                await request_replan()
            return


def _entry_topology_signature(entry: EnergyPlannerConfigEntry) -> tuple[Any, ...]:
    """Return stable config data that requires rebuilding platforms and listeners."""
    subentries = getattr(entry, "subentries", {})
    return (
        _freeze_config_value({
            key: [{k: v for k, v in p.items() if k != CONF_DEFAULT_READY_BY} for p in value]
            if key == VEHICLES else value
            for key, value in getattr(entry, "data", {}).items() if key != "hvac_zone_mappings"
        }),
        tuple(
            sorted(
                (
                    str(getattr(subentry, "subentry_id", subentry_id)),
                    str(getattr(subentry, "subentry_type", "")),
                    _freeze_config_value({
                        key: value for key, value in getattr(subentry, "data", {}).items()
                        if key != "hvac_zone_mappings" and (
                            getattr(subentry, "subentry_type", "") != VEHICLE or key != CONF_DEFAULT_READY_BY
                        )
                    }),
                )
                for subentry_id, subentry in subentries.items()
            )
        ),
    )


def _freeze_config_value(value: Any) -> Any:
    """Convert config-entry values into a deterministic comparable shape."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze_config_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_config_value(item) for item in value))
    return value


def _async_sync_planner_device(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Keep planner and vehicle devices flat, preserving planner entity ownership."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from .entity import planner_device_identifier
    from .entity_registry_migration import async_migrate_entity_registry

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    _async_migrate_duplicate_entity_ids(ent_reg)
    async_migrate_entity_registry(hass, entry)
    device = dev_reg.async_get_or_create(
        entry_type=None,
        config_entry_id=entry.entry_id,
        identifiers={planner_device_identifier(entry.entry_id)},
        manufacturer=INTEGRATION_NAME,
        model=INTEGRATION_NAME,
        name=str(getattr(entry, "title", "") or INTEGRATION_NAME),
    )

    for entity in list(ent_reg.entities.values()):
        if entity.platform != DOMAIN or getattr(entity, "config_entry_id", None) != entry.entry_id:
            continue
        if entity.device_id != device.id or getattr(entity, "config_subentry_id", None) is not None:
            ent_reg.async_update_entity(
                entity.entity_id,
                device_id=device.id,
                config_subentry_id=None,
            )

    vehicle_identifiers = set()
    vehicle_prefix = f"{entry.entry_id}_vehicle_"
    for profile in combined_entry_data(entry).get(VEHICLES, []):
        identifier = (DOMAIN, f"{vehicle_prefix}{profile['id']}")
        vehicle_identifiers.add(identifier)
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={identifier},
            name=profile["name"],
            manufacturer=INTEGRATION_NAME,
            model="Vehicle",
        )
    retired_identifiers = {
        (DOMAIN, f"{entry.entry_id}_{suffix}")
        for suffix in ("system", "energy", "climate", "presence", "enphase", "ai", "ev", "controls")
    }
    # Scope cleanup to this entry so another planner's devices cannot be removed.
    for old_device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        stale_vehicle = any(domain == DOMAIN and identifier.startswith(vehicle_prefix)
                            for domain, identifier in old_device.identifiers) and not (
            old_device.identifiers & vehicle_identifiers
        )
        if stale_vehicle or (old_device.id != device.id and old_device.identifiers & retired_identifiers):
            dev_reg.async_remove_device(old_device.id)


def _async_migrate_duplicate_entity_ids(ent_reg: Any) -> None:
    """Rename entity IDs generated from duplicated device/entity labels."""
    for old_entity_id, new_entity_id in _DUPLICATE_ENTITY_ID_MIGRATIONS.items():
        entity = ent_reg.entities.get(old_entity_id)
        if entity is None or getattr(entity, "platform", None) != DOMAIN:
            continue
        if ent_reg.entities.get(new_entity_id) is not None:
            continue
        ent_reg.async_update_entity(old_entity_id, new_entity_id=new_entity_id)




def _validate_ready_by_time(value: Any) -> str:
    """Validate and normalize a local ready-by time string."""
    import probatio as vol

    parts = str(value).strip().split(":")
    if len(parts) not in {2, 3}:
        raise vol.Invalid("ready_by must be a valid local time in HH:MM format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as err:
        raise vol.Invalid("ready_by must be a valid local time in HH:MM format") from err
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise vol.Invalid("ready_by must be a valid local time in HH:MM format")
    return f"{hour:02d}:{minute:02d}"


def _validate_reason_code(value: Any) -> str:
    """Validate and normalize a redacted audit reason code."""
    import probatio as vol

    reason = str(value).strip()
    if not _REASON_CODE_PATTERN.fullmatch(reason):
        raise vol.Invalid("reason must be a compact redacted reason code")
    return reason
