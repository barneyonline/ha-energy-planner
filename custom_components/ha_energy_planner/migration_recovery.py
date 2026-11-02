"""Shared recovery for legacy configurations missing a vehicle target."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import CONF_EV_SMART_CHARGING_TARGET_SOC, DOMAIN
from .type_defs import EnergyPlannerConfigEntry
from .vehicles import VEHICLE

_MIGRATION_ISSUE_PREFIX = "legacy_vehicle_target_"


def migration_issue_id(entry_id: str) -> str:
    """Keep a single issue throughout target selection and recovery."""
    return f"{_MIGRATION_ISSUE_PREFIX}{entry_id}"


def async_refresh_migration_issues(hass: HomeAssistant) -> None:
    """Reconcile existing notices after startup, including legacy ownerless issues."""
    from homeassistant.helpers import issue_registry as ir

    if ir.DATA_REGISTRY not in hass.data:
        return
    issue_ids = [
        issue_id for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith(_MIGRATION_ISSUE_PREFIX)
    ]
    pending = {
        migration_issue_id(entry.entry_id): entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.version < 4
    }
    # Both APIs land in HA 2026.11. Keep the advertised 2026.10 minimum usable.
    refresh = getattr(ir, "async_refresh_issues", None)
    # Select exact IDs without an owner filter so pre-2026.11 records are included.
    with refresh(hass, DOMAIN, issue_ids=issue_ids) if refresh is not None else nullcontext():
        for issue_id in issue_ids:
            if (entry := pending.get(issue_id)) is not None:
                # A saved target may still need a migration retry. Recreate the
                # existing issue in place to adopt ownership and retain dismissal.
                async_create_migration_issue(hass, entry)
            elif refresh is None:
                ir.async_delete_issue(hass, DOMAIN, issue_id)


def async_clear_migration_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove obsolete recovery instructions without initializing a registry."""
    from homeassistant.helpers import issue_registry as ir

    if ir.DATA_REGISTRY in hass.data:
        ir.async_delete_issue(hass, DOMAIN, migration_issue_id(entry_id))


def async_create_migration_issue(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Create or update the entry's actionable migration issue."""
    from homeassistant.helpers import issue_registry as ir

    ownership: dict[str, Any] = {}
    if getattr(ir, "async_refresh_issues", None) is not None:
        ownership["config_entry_id"] = entry.entry_id
    ir.async_create_issue(
        hass, DOMAIN, migration_issue_id(entry.entry_id),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="legacy_vehicle_target",
        translation_placeholders={"title": entry.title},
        data={"entry_id": entry.entry_id},
        **ownership,
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
