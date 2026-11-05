"""Device migration contracts against the installed Home Assistant registry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_energy_planner import _async_sync_planner_device
from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.entity import planner_device_identifier
from custom_components.ha_energy_planner.subentry_migration import async_migrate_subentries_to_entry_data
from custom_components.ha_energy_planner.vehicles import VEHICLE, VEHICLES


@pytest.mark.parametrize("existing_planner", [False, True])
def test_real_registry_migrates_only_this_entries_retired_devices(
    tmp_path: Path, existing_planner: bool, caplog: pytest.LogCaptureFixture,
) -> None:
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
            original = None
            if existing_planner:
                original = devices.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={planner_device_identifier(entry.entry_id)},
                )
                assert original.entry_type is None
                devices.async_update_device(original.id, name_by_user="My planner")
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
            assert planner.entry_type is None
            if original is not None:
                assert planner.id == original.id
                assert planner.name_by_user == "My planner"
            assert entities.async_get(live.entity_id).device_id == planner.id
            assert devices.async_get(retired.id) is None
            assert devices.async_get(unrelated.id) is not None
            assert devices.async_get(other.id) is not None
            _async_sync_planner_device(hass, entry)
            assert devices.async_get(planner.id).entry_type is None
            assert len(devices.devices) == 3
            profile = ConfigSubentry(
                data=MappingProxyType({"default_ready_by": "07:00"}),
                subentry_type=VEHICLE, title="MINI Aceman", unique_id=None,
            )
            hass.config_entries.async_add_subentry(entry, profile)
            assert async_migrate_subentries_to_entry_data(hass, entry)
            assert not entry.subentries
            assert entry.data[VEHICLES][0]["id"] == profile.subentry_id
            _async_sync_planner_device(hass, entry)
            identifier = (DOMAIN, f"{entry.entry_id}_vehicle_{profile.subentry_id}")
            vehicle = next(
                d for d in dr.async_entries_for_config_entry(devices, entry.entry_id) if identifier in d.identifiers
            )
            assert vehicle.name == "MINI Aceman"
            assert vehicle.entry_type is None and vehicle.via_device_id is None
            assert vehicle.config_entry_id == entry.entry_id
            assert vehicle.config_subentry_id is None
            assert devices.async_get(planner.id).config_entry_id == entry.entry_id
            assert devices.async_get(planner.id).config_subentry_id is None
            devices.async_update_device(vehicle.id, name_by_user="My MINI")
            hass.config_entries.async_update_entry(entry, data={**entry.data, VEHICLES: [
                {**entry.data[VEHICLES][0], "name": "MINI renamed"},
            ]})
            _async_sync_planner_device(hass, entry)
            assert devices.async_get(vehicle.id).name == "MINI renamed"
            assert devices.async_get(vehicle.id).name_by_user == "My MINI"
            assert entities.async_get(live.entity_id).device_id == planner.id
            # Another entry's profile device is never pruned.
            foreign = devices.async_get_or_create(
                config_entry_id=other_entry.entry_id, identifiers={(DOMAIN, f"{other_entry.entry_id}_vehicle_other")},
            )
            hass.config_entries.async_update_entry(entry, data={**entry.data, VEHICLES: []})
            _async_sync_planner_device(hass, entry)
            assert devices.async_get(vehicle.id) is None
            assert devices.async_get(foreign.id) is not None
            assert devices.async_get(planner.id) is not None
            assert devices.async_get(unrelated.id) is not None
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())
    assert not [
        record for record in caplog.records
        if "deprecat" in record.getMessage().lower()
        and any(name in record.getMessage() for name in ("config_entries", "config_entries_subentries"))
    ]
