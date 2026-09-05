"""Device migration contracts against the installed Home Assistant registry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_energy_planner import _async_sync_planner_device
from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.entity import planner_device_identifier


def test_real_registry_migrates_only_this_entries_retired_devices(tmp_path: Path) -> None:
    """Exercise supported registry APIs, including the minimum supported HA."""
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        entries = [
            ConfigEntry(
                domain=DOMAIN, title=title, data={}, options={}, source="user",
                unique_id=None, version=5, minor_version=1,
                discovery_keys=MappingProxyType({}), subentries_data=[],
            )
            for title in ("House Planner", "Other Planner")
        ]
        entry, other_entry = entries
        for config_entry in entries:
            # Register real entries without starting the integration itself.
            hass.config_entries._entries[config_entry.entry_id] = config_entry
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        try:
            retired = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{entry.entry_id}_system")},
            )
            unrelated = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{entry.entry_id}_custom")},
            )
            other = devices.async_get_or_create(
                config_entry_id=other_entry.entry_id,
                identifiers={(DOMAIN, f"{entry.entry_id}_ai")},
            )
            live = entities.async_get_or_create(
                "sensor", DOMAIN, f"{entry.entry_id}_mode",
                config_entry=entry, device_id=retired.id,
            )
            _async_sync_planner_device(hass, entry)
            planner_devices = [
                device for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
                if planner_device_identifier(entry.entry_id) in device.identifiers
            ]
            assert len(planner_devices) == 1
            planner = planner_devices[0]
            assert planner.name == "House Planner"
            assert entities.async_get(live.entity_id).device_id == planner.id
            assert devices.async_get(retired.id) is None
            assert devices.async_get(unrelated.id) is not None
            assert devices.async_get(other.id) is not None
            _async_sync_planner_device(hass, entry)
            assert devices.async_get(planner.id) is not None
            assert len(devices.devices) == 3
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())
