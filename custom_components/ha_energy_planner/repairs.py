"""Repair legacy vehicle targets using the supported migration lifecycle."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .config_flow import _EV_TARGET_SOC_FILTER, ConfigFlow, _entity_selector
from .const import CONF_EV_SMART_CHARGING_TARGET_SOC, DOMAIN
from .migration_recovery import (
    async_clear_migration_issue,
    async_create_migration_issue,
    async_save_vehicle_target,
    migration_issue_id,
)


class VehicleTargetRepairFlow(RepairsFlow):
    """Select the missing target, then recover or explain the required restart."""

    def __init__(self, entry_id: str) -> None:
        """Remember only the entry identity; fetch fresh state on every step."""
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Open the target form; init data contains the issue ID, not form input."""
        return await self.async_step_target()

    async def async_step_target(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Repair a target without replacing the config entry."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None or entry.domain != DOMAIN:
            async_clear_migration_issue(self.hass, self.entry_id)
            return self.async_abort(reason="entry_removed")
        if entry.version > ConfigFlow.VERSION:
            return self.async_abort(reason="unsupported_version")
        if entry.version >= 4:
            async_clear_migration_issue(self.hass, entry.entry_id)
            return self.async_abort(reason="already_repaired")
        if entry.disabled_by:
            return self.async_abort(reason="entry_disabled")
        errors: dict[str, str] = {}
        if user_input is not None:
            if entry.state is not ConfigEntryState.MIGRATION_ERROR:
                errors["base"] = "retry_not_ready"
            else:
                errors = async_save_vehicle_target(self.hass, entry, user_input)
            if not errors:
                retry = getattr(self.hass.config_entries, "async_retry_migration", None)
                if not callable(retry):
                    async_create_migration_issue(self.hass, entry, restart_required=True)
                    return self.async_abort(reason="restart_required")
                try:
                    await retry(entry.entry_id)
                except HomeAssistantError:
                    errors["base"] = "retry_failed"
                else:
                    if entry.state is ConfigEntryState.LOADED:
                        async_clear_migration_issue(self.hass, entry.entry_id)
                        return self.async_create_entry(data={})
                    errors["base"] = "retry_failed"
        return self.async_show_form(
            step_id="target",
            data_schema=vol.Schema({
                vol.Required(CONF_EV_SMART_CHARGING_TARGET_SOC): _entity_selector(entity_filter=_EV_TARGET_SOC_FILTER),
            }),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None,
) -> RepairsFlow | None:
    """Route only this integration's entry-specific target issues."""
    entry_id = (data or {}).get("entry_id")
    if isinstance(entry_id, str) and issue_id == migration_issue_id(entry_id):
        return VehicleTargetRepairFlow(entry_id)
    return None
