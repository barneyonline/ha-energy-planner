"""Vehicle options saves before and after an upgrade's first successful setup."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryDisabler, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_energy_planner.const import CONF_EV_CONNECTED, DOMAIN
from custom_components.ha_energy_planner.entry_data import combined_entry_data
from custom_components.ha_energy_planner.subentry_migration import async_migrate_subentries_to_entry_data
from custom_components.ha_energy_planner.vehicles import VEHICLE, VEHICLES


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("action", ["edit", "remove", "add"])
def test_disabled_entry_vehicle_options_survive_setup(tmp_path: Path, legacy: bool, action: str) -> None:
    """Run real options saves; old subentries cannot resurrect removed or edited profiles."""
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        options = {"planning_interval_minutes": 7}
        entry = ConfigEntry(
            domain=DOMAIN, title="Disabled planner", data={CONF_EV_CONNECTED: "binary_sensor.charger"},
            options=options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[], disabled_by=ConfigEntryDisabler.USER,
        )
        hass.config_entries._entries[entry.entry_id] = entry
        vehicles = []
        for name in ("mini", "bmw", "new"):
            values = {
                "name": name, "port_entity": f"binary_sensor.{name}_port", "home_entity": f"device_tracker.{name}",
                "ev_soc_entity": f"sensor.{name}_soc", "ev_smart_charging_target_soc_entity": f"sensor.{name}_target",
                "default_ready_by": "07:00", "ev_charge_rate_kw": 7, "ev_soc_per_kwh": 2,
            }
            for key, value in values.items():
                if key.endswith("entity"):
                    hass.states.async_set(value, "home" if key == "home_entity" else "50")
            vehicles.append(ConfigSubentry(
                data=MappingProxyType(values), subentry_type=VEHICLE, title=name, unique_id=None,
            ))
        for vehicle in vehicles[:2]:
            hass.config_entries.async_add_subentry(entry, vehicle)
        if not legacy:
            async_migrate_subentries_to_entry_data(hass, entry)
        original_ids = [vehicle.subentry_id for vehicle in vehicles[:2]]
        try:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            flow_id = result["flow_id"]
            result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": f"{action}_vehicle"})
            if action == "edit":
                result = await hass.config_entries.options.async_configure(flow_id, {"vehicle_id": original_ids[0]})
                assert result["step_id"] == "vehicle"
                submission = {**vehicles[0].data, "name": "renamed", "default_ready_by": "06:00"}
            elif action == "remove":
                submission = {"vehicle_id": original_ids[0]}
            else:
                submission = dict(vehicles[2].data)
            if action != "remove":
                submission["advanced"] = {"ev_soc_per_kwh": submission.pop("ev_soc_per_kwh")}
            result = await hass.config_entries.options.async_configure(flow_id, submission)
            assert result["type"] == "create_entry"
            assert not entry.subentries
            assert entry.disabled_by is ConfigEntryDisabler.USER
            assert dict(entry.options) == options
            profiles = combined_entry_data(entry)[VEHICLES]
            assert len(profiles) == {"edit": 2, "remove": 1, "add": 3}[action]
            assert profiles[-1 if action != "add" else 1]["id"] == original_ids[1]
            if action == "edit":
                assert profiles[0]["id"] == original_ids[0]
                assert profiles[0]["name"] == "renamed"
                assert profiles[0]["default_ready_by"] == "06:00"
            elif action == "remove":
                assert all(profile["id"] != original_ids[0] for profile in profiles)
            else:
                assert profiles[-1]["name"] == "new" and profiles[-1]["id"] not in original_ids
            # This setup hook must preserve the completed user action.
            assert not async_migrate_subentries_to_entry_data(hass, entry)
            assert combined_entry_data(entry)[VEHICLES] == profiles
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())
