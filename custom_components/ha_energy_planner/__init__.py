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
    CONF_EV_CHARGE_RATE_KW,
    CONF_GRID_IMPORT_LIMIT_KW,
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
_DUPLICATE_ENTITY_ID_MIGRATIONS = {
    "sensor.ai_ai_advice": "sensor.ai_advice",
    "switch.ai_ai_enabled": "switch.ai_enabled",
    "sensor.climate_climate_plan": "sensor.climate_plan",
    "switch.climate_climate_control_enabled": "switch.climate_control_enabled",
    "sensor.enphase_enphase_plan": "sensor.enphase_plan",
    "switch.enphase_enphase_control_enabled": "switch.enphase_control_enabled",
    "sensor.ev_ev_charging_plan": "sensor.ev_charging_plan",
    "switch.ev_ev_control_enabled": "switch.ev_control_enabled",
}


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
    from .subentry_migration import async_consolidate_subentries

    if not entry.options:
        hass.config_entries.async_update_entry(entry, options=DEFAULT_OPTIONS)
    if getattr(entry, "title", None) == LEGACY_INTEGRATION_NAME:
        hass.config_entries.async_update_entry(entry, title=INTEGRATION_NAME)
    async_consolidate_subentries(hass, entry)
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
        _async_remove_legacy_device(hass, entry)
        _async_sync_planner_devices(hass, entry)
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


def _async_remove_legacy_device(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove the old main-entry planner device so entities remain ungrouped."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if device is None:
        return
    for entity in list(ent_reg.entities.values()):
        if entity.platform == DOMAIN and entity.device_id == device.id:
            ent_reg.async_update_entity(entity.entity_id, device_id=None)
    dev_reg.async_remove_device(device.id)


def _async_sync_planner_devices(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Create planner group devices and link existing entities to them."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from .entity import (
        DEVICE_AI,
        DEVICE_CLIMATE,
        DEVICE_ENERGY,
        DEVICE_ENPHASE,
        DEVICE_EV,
        DEVICE_MODELS,
        DEVICE_NAMES,
        DEVICE_PRESENCE,
        DEVICE_SYSTEM,
        OPTIONAL_DEVICE_KEYS,
        planner_device_configured,
        planner_device_identifier,
        planner_device_key_for_entity,
    )

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    _async_migrate_duplicate_entity_ids(ent_reg)
    subentries_by_type = {subentry.subentry_type: subentry for subentry in getattr(entry, "subentries", {}).values()}
    device_subentry_ids = {
        DEVICE_SYSTEM: getattr(subentries_by_type.get(DEVICE_SYSTEM), "subentry_id", None),
        DEVICE_ENERGY: getattr(subentries_by_type.get(DEVICE_ENERGY), "subentry_id", None),
        DEVICE_CLIMATE: getattr(subentries_by_type.get(DEVICE_CLIMATE), "subentry_id", None),
        DEVICE_PRESENCE: getattr(subentries_by_type.get(DEVICE_PRESENCE), "subentry_id", None),
        DEVICE_ENPHASE: getattr(subentries_by_type.get(DEVICE_ENPHASE), "subentry_id", None),
        DEVICE_AI: getattr(subentries_by_type.get(DEVICE_AI), "subentry_id", None),
        DEVICE_EV: getattr(subentries_by_type.get(DEVICE_EV), "subentry_id", None),
    }
    devices = {}
    for device_key in (
        DEVICE_SYSTEM,
        DEVICE_ENERGY,
        DEVICE_CLIMATE,
        DEVICE_PRESENCE,
        DEVICE_ENPHASE,
        DEVICE_AI,
        DEVICE_EV,
    ):
        if device_key in OPTIONAL_DEVICE_KEYS and device_subentry_ids[device_key] is None:
            continue
        devices[device_key] = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=device_subentry_ids[device_key],
            identifiers={planner_device_identifier(entry.entry_id, device_key)},
            manufacturer=INTEGRATION_NAME,
            model=DEVICE_MODELS[device_key],
            name=DEVICE_NAMES[device_key],
        )
        if device_subentry_ids[device_key] is not None:
            dev_reg.async_update_device(
                devices[device_key].id,
                remove_config_entry_id=entry.entry_id,
                remove_config_subentry_id=None,
            )

    for entity in list(ent_reg.entities.values()):
        if entity.platform != DOMAIN or getattr(entity, "config_entry_id", None) != entry.entry_id:
            continue
        entity_key = _planner_entity_key(entry.entry_id, entity)
        device_key = planner_device_key_for_entity(entity_key)
        if not planner_device_configured(entry, device_key):
            continue
        device = devices[device_key]
        config_subentry_id = device_subentry_ids[device_key]
        if entity.device_id != device.id or getattr(entity, "config_subentry_id", None) != config_subentry_id:
            ent_reg.async_update_entity(
                entity.entity_id,
                device_id=device.id,
                config_subentry_id=config_subentry_id,
            )

    old_device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_controls")})
    if old_device is not None:
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


def _planner_entity_key(entry_id: str, entity: Any) -> str:
    """Return the integration entity key from a registry entry."""
    unique_id = str(getattr(entity, "unique_id", "") or "")
    prefix = f"{entry_id}_"
    if unique_id.startswith(prefix):
        return unique_id.removeprefix(prefix)
    return str(getattr(entity, "entity_id", "")).split(".")[-1].removeprefix("ha_energy_planner_")


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
