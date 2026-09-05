"""Upgrade, reload and restart contracts using real HA entry/platform machinery."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_energy_planner import async_migrate_entry
from custom_components.ha_energy_planner.config_flow import ConfigFlow
from custom_components.ha_energy_planner.const import CONF_EV_SMART_CHARGING_TARGET_SOC, DOMAIN

ROOT = Path(__file__).resolve().parents[1]


def make_entry(data, *, version=5, options=None):
    return ConfigEntry(
        domain=DOMAIN, title="Upgrade fixture", data=data, options=options or {},
        source="user", unique_id=None, version=version, minor_version=1,
        discovery_keys=MappingProxyType({}), subentries_data=[],
    )


def test_previous_release_setup_reload_restart_preserves_recovery(tmp_path: Path) -> None:
    fixture = json.loads((ROOT / "tests/fixtures/upgrade/0.9.18.json").read_text())
    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")
    entry = make_entry(fixture["entry"]["data"], options=fixture["entry"]["options"])
    key = f"{DOMAIN}_state_{entry.entry_id}"
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / key).write_text(json.dumps({**fixture["store"], "key": key}))

    async def lifetime(restart=False):
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        hass.auth = await auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
        if restart:
            current = hass.config_entries.async_get_entry(entry.entry_id)
            assert current is not None
        else:
            current = entry
            hass.config_entries._entries[current.entry_id] = current
            hass.config_entries._async_schedule_save()
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=current.entry_id, identifiers={(DOMAIN, f"{current.entry_id}_system")}
            )
            er.async_get(hass).async_get_or_create(
                "sensor", DOMAIN, f"{current.entry_id}_mode", config_entry=current,
                device_id=device.id, suggested_object_id="my_existing_mode",
            )
        try:
            assert await hass.config_entries.async_setup(current.entry_id)
            assert current.state is ConfigEntryState.LOADED
            coordinator = current.runtime_data
            assert coordinator.store.data["ownership"] == fixture["store"]["data"]["ownership"]
            assert coordinator.store.data["ev_grid_reservation"]["active"] is True
            assert coordinator.overrides[0].reason == "operator_requested"
            assert "outcomes" not in coordinator.store.data
            entities = er.async_get(hass)
            mode_id = entities.async_get_entity_id("sensor", DOMAIN, f"{current.entry_id}_mode")
            assert mode_id == "sensor.my_existing_mode"
            assert hass.states.get(mode_id) is not None
            # Configuration reload handoff preserves unresolved ownership;
            # no actual actuator is mapped or commanded by this fixture.
            coordinator._configuration_reload_handoff = True
            assert await hass.config_entries.async_reload(current.entry_id)
            assert current.runtime_data is not coordinator
            assert current.runtime_data.store.data["ownership"] == fixture["store"]["data"]["ownership"]
            assert entities.async_get_entity_id("sensor", DOMAIN, f"{current.entry_id}_mode") == mode_id
        finally:
            await hass.async_stop(force=True)
    asyncio.run(lifetime())
    asyncio.run(lifetime(restart=True))


def test_legacy_target_reconfigure_retries_migration_without_replacing_entry(tmp_path: Path) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        entry = make_entry({"ev_soc_entity": "sensor.car_soc"}, version=3)
        hass.config_entries._entries[entry.entry_id] = entry
        flow = ConfigFlow()
        flow.hass = hass
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        try:
            assert not await async_migrate_entry(hass, entry)
            assert (await flow.async_step_reconfigure())["type"] == "form"
            invalid = await flow.async_step_reconfigure({CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.missing"})
            assert invalid["errors"]
            hass.states.async_set("sensor.vehicle_target", "80", {"unit_of_measurement": "%"})
            with patch.object(ConfigEntries, "async_schedule_reload") as reload:
                result = await flow.async_step_reconfigure({CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.vehicle_target"})
                assert result["reason"] == "reconfigure_successful"
                reload.assert_called_once_with(entry.entry_id)
            assert await async_migrate_entry(hass, entry)
            assert entry.version == 5
            assert entry.data["ev_soc_entity"] == "sensor.car_soc"
            assert entry.data[CONF_EV_SMART_CHARGING_TARGET_SOC] == "sensor.vehicle_target"
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_loaded_target_repair_updates_legacy_subentry_and_uses_update_listener(tmp_path: Path) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        entry = make_entry({"instance_name": "Keep my settings"})
        hass.config_entries._entries[entry.entry_id] = entry
        for values in (
            {CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.old_target"},
            {"household_load_entity": "sensor.load"},
        ):
            hass.config_entries.async_add_subentry(entry, ConfigSubentry(
                data=MappingProxyType(values), subentry_type="ev", title="Legacy section", unique_id=None,
            ))
        notifications = []
        async def listener(hass, current):
            notifications.append(current.entry_id)
        entry.add_update_listener(listener)
        flow = ConfigFlow()
        flow.hass = hass
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        hass.states.async_set("sensor.new_target", "90", {"unit_of_measurement": "%"})
        try:
            with patch.object(ConfigEntries, "async_schedule_reload") as reload:
                result = await flow.async_step_reconfigure({CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.new_target"})
                assert result["reason"] == "reconfigure_successful"
                reload.assert_not_called()
            await hass.async_block_till_done()
            assert notifications
            assert entry.data["instance_name"] == "Keep my settings"
            from custom_components.ha_energy_planner.subentry_migration import async_migrate_subentries_to_entry_data
            async_migrate_subentries_to_entry_data(hass, entry)
            assert entry.data[CONF_EV_SMART_CHARGING_TARGET_SOC] == "sensor.new_target"
            assert entry.data["household_load_entity"] == "sensor.load"
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())
