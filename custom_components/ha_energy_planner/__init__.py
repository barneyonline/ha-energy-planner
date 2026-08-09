"""Energy Planner custom integration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .const import (
    ATTR_ASSET,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION_MINUTES,
    ATTR_READY_BY,
    ATTR_REASON,
    ATTR_TARGET_SOC,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BASELINE_LOAD_OBSERVED,
    CONF_EV_CHARGE_RATE_KW,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_HOUSEHOLD_LOAD,
    CONF_INSTANCE_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    EV_RESERVATION_EXTERNAL_BASELINE,
    EV_RESERVATION_RETAIN_WHEN_UNLOADED,
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
    SERVICE_SET_EV_TARGET_SOC,
    SERVICE_SET_MANUAL_HVAC_OVERRIDE,
)
from .type_defs import EnergyPlannerConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_REASON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_DUPLICATE_ENTITY_ID_MIGRATIONS = {"switch.ai_ai_enabled": "switch.ai_enabled"}


async def async_migrate_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> bool:
    """Migrate measured-load configuration without trusting forecast entities."""
    if getattr(entry, "version", 1) > 2:
        return False
    data = dict(entry.data)
    if not data.get(CONF_HOUSEHOLD_LOAD) and data.get(CONF_BASELINE_LOAD_OBSERVED):
        data[CONF_HOUSEHOLD_LOAD] = data[CONF_BASELINE_LOAD_OBSERVED]
    data.pop(CONF_BASELINE_LOAD_FORECAST, None)
    data.pop(CONF_BASELINE_LOAD_OBSERVED, None)
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level services."""
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
    from homeassistant.helpers import config_validation as cv

    from .coordinator import EnergyPlannerCoordinator
    from .diagnostics import async_get_config_entry_diagnostics
    from .preflight import build_preflight_report

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

    async def handle_target_soc(call: ServiceCall) -> None:
        target_soc = float(call.data[ATTR_TARGET_SOC])
        coordinator = await _require_coordinator(call)
        await coordinator.async_set_ev_target_soc(target_soc)

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
        return build_preflight_report(hass, coordinator)

    async def handle_export_support_bundle(call: ServiceCall) -> dict[str, Any]:
        coordinator = await _require_coordinator(call)
        return {
            "preflight": build_preflight_report(hass, coordinator),
            "diagnostics": await async_get_config_entry_diagnostics(hass, coordinator.entry),
        }

    async def handle_arm_production(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_acknowledged"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_arm_production_control(reason)

    async def handle_disarm_production(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_disarm_production_control(reason)

    async def handle_pause_control(call: ServiceCall) -> None:
        duration = int(call.data[ATTR_DURATION_MINUTES])
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        asset = str(call.data.get(ATTR_ASSET, "all"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_pause_control(duration, reason, asset)

    async def handle_resume_control(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        coordinator = await _require_coordinator(call)
        await coordinator.async_resume_control(reason)

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
        DOMAIN,
        SERVICE_SET_EV_TARGET_SOC,
        handle_target_soc,
        schema=vol.Schema(
            {
                **_config_entry_field(),
                vol.Required(ATTR_TARGET_SOC): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=0, max=100),
                )
            }
        ),
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
                vol.Optional(ATTR_ASSET, default="all"): vol.In(["all", "ev", "daikin", "enphase"]),
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
                vol.Optional(ATTR_REASON, default="user_requested"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> bool:
    """Set up Energy Planner from a config entry."""
    from .coordinator import EnergyPlannerCoordinator
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
    _rehydrate_ev_grid_reservation(hass, entry, getattr(store, "data", {}))
    coordinator = EnergyPlannerCoordinator(hass, entry, store)
    coordinator.entry_topology_signature = _entry_topology_signature(entry)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        coordinator.async_start_listeners()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _async_sync_planner_device(hass, entry)
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        entry.async_on_unload(coordinator.async_shutdown)
    except Exception:
        coordinator.async_shutdown()
        await coordinator.async_restore_safe_state("setup_entry_failed", refresh=False)
        entry.runtime_data = None
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    # Stop new listener/timer work before waiting for any in-flight planner
    # execution. The coordinator's teardown marker also prevents an already
    # queued refresh from committing a new device command after restoration.
    coordinator.async_shutdown()
    unload_completed = False
    try:
        restore_outcome = await coordinator.async_restore_safe_state("entry_unload", refresh=False)
        store_data = getattr(getattr(coordinator, "store", None), "data", {})
        remaining_ownership = store_data.get("ownership") if isinstance(store_data, Mapping) else None
        ev_reservation = store_data.get("ev_grid_reservation") if isinstance(store_data, Mapping) else None
        unresolved_restore = bool(remaining_ownership) or bool(
            isinstance(ev_reservation, Mapping) and ev_reservation.get("active") is True
        )
        if (
            getattr(getattr(restore_outcome, "result", None), "value", None) == "failed"
            and unresolved_restore
        ):
            await coordinator.async_disarm_production_control("entry_unload_restore_failed")
            return False
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unload_ok:
            entry.runtime_data = None
            unload_completed = True
        else:
            await coordinator.async_disarm_production_control("entry_platform_unload_failed")
        return unload_ok
    finally:
        if not unload_completed and entry.runtime_data is coordinator:
            coordinator.async_start_listeners()


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
) -> None:
    """Reserve conservative headroom for persisted planner-owned EV state."""
    ownership = store_data.get("ownership")
    persisted_reservation = store_data.get("ev_grid_reservation")
    if (
        isinstance(persisted_reservation, dict)
        and persisted_reservation.get("active") is False
    ):
        return
    has_owned_ev_state = isinstance(ownership, dict) and bool(
        ownership.get("ev_smart_charging_state")
    )
    has_persisted_reservation = isinstance(persisted_reservation, dict) and (
        persisted_reservation.get("active") is True or bool(persisted_reservation)
    )
    if not has_owned_ev_state and not has_persisted_reservation:
        return
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return
    domain_data = hass_data.setdefault(DOMAIN, {})
    if not isinstance(domain_data, dict):
        return
    reservations = domain_data.setdefault("ev_grid_reservations", {})
    if not isinstance(reservations, dict):
        return
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
        "reserved_at": datetime.now(UTC).isoformat(),
        EV_RESERVATION_RETAIN_WHEN_UNLOADED: True,
    }
    if (
        isinstance(persisted_reservation, dict)
        and persisted_reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is True
    ):
        reservation[EV_RESERVATION_EXTERNAL_BASELINE] = True
    reservations.setdefault(entry.entry_id, reservation)


def _non_negative_finite_float(value: Any) -> float:
    """Return a finite non-negative float for lifecycle reservation recovery."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric >= 0 and numeric < float("inf") else 0.0


async def _async_update_listener(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Handle options and subentry updates."""
    coordinator = getattr(entry, "runtime_data", None)
    topology_signature = _entry_topology_signature(entry)
    previous_topology_signature = getattr(coordinator, "entry_topology_signature", None)
    if previous_topology_signature is not None and topology_signature != previous_topology_signature:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    handle_options_update = getattr(coordinator, "async_handle_options_update", None)
    if callable(handle_options_update):
        await handle_options_update()
        return
    request_replan = getattr(coordinator, "async_request_replan", None)
    if callable(request_replan):
        await request_replan()


def _entry_topology_signature(entry: EnergyPlannerConfigEntry) -> tuple[Any, ...]:
    """Return stable config data that requires rebuilding platforms and listeners."""
    subentries = getattr(entry, "subentries", {})
    return (
        _freeze_config_value(getattr(entry, "data", {})),
        tuple(
            sorted(
                (
                    str(getattr(subentry, "subentry_id", subentry_id)),
                    str(getattr(subentry, "subentry_type", "")),
                    _freeze_config_value(getattr(subentry, "data", {})),
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
    """Create one planner device, link every entity, and remove old group devices."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from .entity import planner_device_identifier

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    _async_migrate_duplicate_entity_ids(ent_reg)
    device = dev_reg.async_get_or_create(
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

    for old_suffix in ("system", "energy", "climate", "presence", "enphase", "ai", "ev", "controls"):
        old_device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_{old_suffix}")})
        if old_device is not None and old_device.id != device.id:
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
    import voluptuous as vol

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
    import voluptuous as vol

    reason = str(value).strip()
    if not _REASON_CODE_PATTERN.fullmatch(reason):
        raise vol.Invalid("reason must be a compact redacted reason code")
    return reason
