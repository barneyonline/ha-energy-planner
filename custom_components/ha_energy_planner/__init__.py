"""Energy Planner custom integration."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .const import (
    ATTR_ASSET,
    ATTR_DURATION_MINUTES,
    ATTR_READY_BY,
    ATTR_REASON,
    DEFAULT_OPTIONS,
    DOMAIN,
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
from .type_defs import EnergyPlannerConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_REASON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level services."""
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.helpers import config_validation as cv

    from .coordinator import EnergyPlannerCoordinator
    from .diagnostics import async_get_config_entry_diagnostics
    from .preflight import build_preflight_report

    async def _first_coordinator() -> EnergyPlannerCoordinator | None:
        entries = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            coordinator = getattr(entry, "runtime_data", None)
            if isinstance(coordinator, EnergyPlannerCoordinator):
                return coordinator
        return None

    async def handle_replan(call: ServiceCall) -> None:
        if coordinator := await _first_coordinator():
            await coordinator.async_request_replan()

    async def handle_restore(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "manual_service_call"))
        if coordinator := await _first_coordinator():
            await coordinator.async_restore_safe_state(reason)

    async def handle_ready_by(call: ServiceCall) -> None:
        ready_by = str(call.data[ATTR_READY_BY])
        if coordinator := await _first_coordinator():
            await coordinator.async_set_ready_by(ready_by)

    async def handle_manual_override(call: ServiceCall) -> None:
        duration = int(call.data[ATTR_DURATION_MINUTES])
        reason = str(call.data.get(ATTR_REASON, "manual_service_call"))
        if coordinator := await _first_coordinator():
            await coordinator.async_set_manual_hvac_override(duration, reason)

    async def handle_export_diagnostics(call: ServiceCall) -> dict[str, Any]:
        if coordinator := await _first_coordinator():
            return await async_get_config_entry_diagnostics(hass, coordinator.entry)
        return {"error": "no_config_entry"}

    async def handle_run_preflight(call: ServiceCall) -> dict[str, Any]:
        if coordinator := await _first_coordinator():
            return build_preflight_report(hass, coordinator)
        return {"ok": False, "error": "no_config_entry"}

    async def handle_export_support_bundle(call: ServiceCall) -> dict[str, Any]:
        if coordinator := await _first_coordinator():
            return {
                "preflight": build_preflight_report(hass, coordinator),
                "diagnostics": await async_get_config_entry_diagnostics(hass, coordinator.entry),
            }
        return {"error": "no_config_entry"}

    async def handle_arm_production(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_acknowledged"))
        if coordinator := await _first_coordinator():
            await coordinator.async_arm_production_control(reason)

    async def handle_disarm_production(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        if coordinator := await _first_coordinator():
            await coordinator.async_disarm_production_control(reason)

    async def handle_pause_control(call: ServiceCall) -> None:
        duration = int(call.data[ATTR_DURATION_MINUTES])
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        asset = str(call.data.get(ATTR_ASSET, "all"))
        if coordinator := await _first_coordinator():
            await coordinator.async_pause_control(duration, reason, asset)

    async def handle_resume_control(call: ServiceCall) -> None:
        reason = str(call.data.get(ATTR_REASON, "user_requested"))
        if coordinator := await _first_coordinator():
            await coordinator.async_resume_control(reason)

    hass.services.async_register(DOMAIN, SERVICE_REPLAN, handle_replan)
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_SAFE_STATE,
        handle_restore,
        schema=vol.Schema(
            {vol.Optional(ATTR_REASON, default="manual_service_call"): vol.All(cv.string, _validate_reason_code)}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EV_READY_BY,
        handle_ready_by,
        schema=vol.Schema({vol.Required(ATTR_READY_BY): vol.All(cv.string, _validate_ready_by_time)}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MANUAL_HVAC_OVERRIDE,
        handle_manual_override,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DURATION_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Optional(ATTR_REASON, default="manual_service_call"): vol.All(cv.string, _validate_reason_code),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_DIAGNOSTICS,
        handle_export_diagnostics,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_PREFLIGHT,
        handle_run_preflight,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_SUPPORT_BUNDLE,
        handle_export_support_bundle,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ARM_PRODUCTION_CONTROL,
        handle_arm_production,
        schema=vol.Schema(
            {vol.Optional(ATTR_REASON, default="user_acknowledged"): vol.All(cv.string, _validate_reason_code)}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DISARM_PRODUCTION_CONTROL,
        handle_disarm_production,
        schema=vol.Schema(
            {vol.Optional(ATTR_REASON, default="user_requested"): vol.All(cv.string, _validate_reason_code)}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PAUSE_CONTROL,
        handle_pause_control,
        schema=vol.Schema(
            {
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
            {vol.Optional(ATTR_REASON, default="user_requested"): vol.All(cv.string, _validate_reason_code)}
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
    store = PlannerStore(hass)
    await store.async_load()
    coordinator = EnergyPlannerCoordinator(hass, entry, store)
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
    coordinator.async_shutdown()
    await coordinator.async_restore_safe_state("entry_unload", refresh=False)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data = None
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Handle options updates."""
    coordinator = getattr(entry, "runtime_data", None)
    request_replan = getattr(coordinator, "async_request_replan", None)
    if callable(request_replan):
        await request_replan()


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
        if entity.platform != DOMAIN:
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
