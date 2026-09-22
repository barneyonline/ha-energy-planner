"""Shared recovery for legacy configurations missing a vehicle target."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import CONF_EV_SMART_CHARGING_TARGET_SOC, DOMAIN
from .type_defs import EnergyPlannerConfigEntry
from .vehicles import VEHICLE


def migration_issue_id(entry_id: str) -> str:
    """Keep a single issue throughout target selection and recovery."""
    return f"legacy_vehicle_target_{entry_id}"


def supports_migration_retry(hass: HomeAssistant) -> bool:
    """The public retry API is absent from the supported HA 2026.9 baseline."""
    return callable(getattr(hass.config_entries, "async_retry_migration", None))


def async_clear_migration_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove obsolete recovery instructions without initializing a registry."""
    from homeassistant.helpers import issue_registry as ir

    if ir.DATA_REGISTRY in hass.data:
        ir.async_delete_issue(hass, DOMAIN, migration_issue_id(entry_id))


def async_create_migration_issue(
    hass: HomeAssistant, entry: EnergyPlannerConfigEntry, *, restart_required: bool = False,
) -> None:
    """Create or update the entry's actionable migration issue."""
    from homeassistant.helpers import issue_registry as ir

    ir.async_create_issue(
        hass, DOMAIN, migration_issue_id(entry.entry_id),
        is_fixable=not restart_required,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="migration_restart_required" if restart_required else "legacy_vehicle_target",
        translation_placeholders={"title": entry.title},
        data={"entry_id": entry.entry_id},
    )


def async_save_vehicle_target(
    hass: HomeAssistant, entry: EnergyPlannerConfigEntry, user_input: dict[str, Any],
) -> dict[str, str]:
    """Validate and save one target without losing settings or legacy mappings."""
    # Import lazily so the config flow can share this helper without an import cycle.
    from .config_flow import _validate_config

    target = user_input.get(CONF_EV_SMART_CHARGING_TARGET_SOC)
    if not isinstance(target, str) or not target.strip():
        return {CONF_EV_SMART_CHARGING_TARGET_SOC: "ev_planning_sensor_required"}
    data = {CONF_EV_SMART_CHARGING_TARGET_SOC: target}
    errors = _validate_config(hass, data)
    if errors:
        return errors
    for subentry in entry.subentries.values():
        if subentry.subentry_type != VEHICLE and CONF_EV_SMART_CHARGING_TARGET_SOC in subentry.data:
            hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, **data})
    hass.config_entries.async_update_entry(entry, data={**entry.data, **data})
    return {}
