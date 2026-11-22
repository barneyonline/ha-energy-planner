"""Upgrade, reload and restart contracts using real HA entry/platform machinery."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest
from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.helpers.storage import Store

from custom_components.ha_energy_planner import async_migrate_entry
from custom_components.ha_energy_planner.config_flow import ConfigFlow
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_IMPORT_PRICE,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint
from custom_components.ha_energy_planner.storage import PlannerStore

ROOT = Path(__file__).resolve().parents[1]


def make_entry(data, *, version=5, options=None):
    return ConfigEntry(
        domain=DOMAIN, title="Upgrade fixture", data=data, options=options or {},
        source="user", unique_id=None, version=version, minor_version=1,
        discovery_keys=MappingProxyType({}), subentries_data=[],
    )


@pytest.mark.parametrize("with_vehicle", [False, True])
def test_previous_release_setup_reload_restart_preserves_recovery(tmp_path: Path, with_vehicle: bool) -> None:
    fixture = json.loads((ROOT / "tests/fixtures/upgrade/0.9.18.json").read_text())
    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")
    entry = make_entry(fixture["entry"]["data"], options=fixture["entry"]["options"])
    vehicle = ConfigSubentry(
        data=MappingProxyType({"default_ready_by": "07:00"}),
        subentry_type="vehicle", title="MINI Aceman", unique_id=None,
    )
    if with_vehicle:
        fixture["store"]["data"]["ev_vehicle_calibrations"] = {
            vehicle.subentry_id: {"status": "collecting", "sample_count": 1},
        }
    device_ids = set()
    key = f"{DOMAIN}_state_{entry.entry_id}"
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / key).write_text(json.dumps({**fixture["store"], "key": key}))

    async def lifetime(restart=False):
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
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
            if with_vehicle:
                hass.config_entries.async_add_subentry(current, vehicle)
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
            assert not current.subentries
            if with_vehicle:
                assert current.data["ev_vehicles"][0]["id"] == vehicle.subentry_id
                assert current.runtime_data.store.data["ev_vehicle_calibrations"] == (
                    fixture["store"]["data"]["ev_vehicle_calibrations"]
                )
                own_devices = dr.async_entries_for_config_entry(dr.async_get(hass), current.entry_id)
                assert len(own_devices) == 2
                assert {d.name for d in own_devices} == {"Upgrade fixture", "MINI Aceman"}
                assert all(d.config_entry_id == current.entry_id and d.config_subentry_id is None for d in own_devices)
                if restart:
                    assert {d.id for d in own_devices} == device_ids
                device_ids.update(d.id for d in own_devices)
                menu = await hass.config_entries.options.async_init(current.entry_id)
                assert menu["type"] == "menu"
                assert menu["menu_options"] == [
                    "settings", "zone_temperatures", "zone_equipment", "add_vehicle", "edit_vehicle", "remove_vehicle",
                ]
                selection = await hass.config_entries.options.async_configure(
                    menu["flow_id"], {"next_step_id": "edit_vehicle"},
                )
                assert selection["step_id"] == "edit_vehicle"
                form = await hass.config_entries.options.async_configure(
                    menu["flow_id"], {"vehicle_id": vehicle.subentry_id},
                )
                assert form["step_id"] == "vehicle"
                hass.config_entries.options.async_abort(menu["flow_id"])
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


def test_separate_data_and_options_writes_preserve_reload_recovery(tmp_path: Path) -> None:
    """Exercise real HA callbacks, reload locking, platforms and persisted recovery."""
    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        hass.auth = await auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
        entry = make_entry({}, options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False})
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            assert await hass.config_entries.async_setup(entry.entry_id)
            coordinator = entry.runtime_data
            await coordinator.async_arm_production_control("test_existing_operator_authority")
            preparing = asyncio.Event()
            finish_preparing = asyncio.Event()
            prepare = type(coordinator).async_prepare_configuration_reload

            async def gated_prepare(runtime):
                preparing.set()
                await finish_preparing.wait()
                await prepare(runtime)

            with (
                patch.object(type(coordinator), "async_prepare_configuration_reload", gated_prepare),
                patch.object(ConfigEntries, "async_reload", wraps=hass.config_entries.async_reload) as reload,
            ):
                # The central options form writes data before HA saves its options.
                hass.config_entries.async_update_entry(entry, data={"household_load_entity": "sensor.new_load"})
                hass.config_entries.async_update_entry(entry, options={**entry.options, "ev_keep_charger_on": True})
                await asyncio.wait_for(preparing.wait(), 10)
                await asyncio.sleep(0)
                finish_preparing.set()
                await asyncio.wait_for(hass.async_block_till_done(), 10)
                assert reload.call_count == 1

            replacement = entry.runtime_data
            assert replacement is not coordinator
            assert entry.state is ConfigEntryState.LOADED
            assert replacement.automatic_control_requested
            assert not replacement.active_control
            assert replacement._startup_auto_recovery_authorized
            production = replacement.store.data["production"]
            assert production["disarmed_reason"] == "configuration_changed"
            assert production["startup_auto_recovery"]["status"] == "waiting_for_home_assistant"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("armed", [True, False])
def test_first_plan_supersession_preserves_control_for_core_retry(tmp_path: Path, armed: bool) -> None:
    """A tariff revision during the first Store write leaves startup intent intact."""
    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        hass.auth = await auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
        price_entity = "sensor.import_price"
        entry = make_entry(
            {CONF_AMBER_IMPORT_PRICE: price_entity},
            options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
        )
        hass.config_entries._entries[entry.entry_id] = entry
        hass.states.async_set(price_entity, "0.20", {"unit_of_measurement": "AUD/kWh"})
        previous = PlannerStore(hass, entry.entry_id)
        ownership = {"enphase_profile": "AI Optimisation"}
        reservation = {"active": True, "load_kw": 7.2}
        await previous.async_save_ownership(ownership)
        await previous.async_save_ev_grid_reservation(reservation)
        await previous.async_save_production({
            "armed": armed,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(dict(entry.data), dict(entry.options)),
            "startup_auto_recovery": {"status": "waiting_for_safe"},
        })

        save = Store.async_save
        candidate_writes = 0

        async def change_tariff_during_save(storage, payload):
            nonlocal candidate_writes
            await save(storage, payload)
            if storage.key == f"{DOMAIN}_state_{entry.entry_id}" and "_pending_plan_publication" in payload:
                candidate_writes += 1
                if candidate_writes == 1:
                    hass.states.async_set(price_entity, "0.31", {"unit_of_measurement": "AUD/kWh"})

        try:
            with patch.object(Store, "async_save", change_tariff_during_save):
                assert not await hass.config_entries.async_setup(entry.entry_id)
                assert entry.state is ConfigEntryState.SETUP_RETRY
                assert not hasattr(entry, "runtime_data")
                assert candidate_writes == 1
                await previous.async_load()
                assert not previous.data.get("active_plan")
                assert previous.data["ownership"] == ownership
                assert previous.data["ev_grid_reservation"] == reservation
                assert previous.data["production"]["armed"] is armed
                assert previous.data["production"]["startup_auto_recovery"]["status"] == "waiting_for_home_assistant"

                # Let Core deliver the registered automatic retry at startup.
                hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
                await asyncio.wait_for(hass.async_block_till_done(wait_background_tasks=True), 10)
                assert candidate_writes >= 2
                assert entry.state is ConfigEntryState.LOADED
                assert entry.runtime_data.data is not None
                assert entry.runtime_data.active_control is armed
                assert entry.runtime_data._startup_auto_recovery_authorized
                assert entry.runtime_data.store.data["ownership"] == ownership
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_legacy_target_reconfigure_directs_recovery_without_replacing_entry(tmp_path: Path) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        entry = make_entry({"ev_soc_entity": "sensor.car_soc"}, version=3)
        hass.config_entries._entries[entry.entry_id] = entry
        flow = ConfigFlow()
        flow.hass = hass
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        try:
            from homeassistant.exceptions import ConfigEntryError

            with pytest.raises(ConfigEntryError):
                await async_migrate_entry(hass, entry)
            entry._async_set_state(hass, ConfigEntryState.MIGRATION_ERROR, None)
            assert (await flow.async_step_reconfigure())["type"] == "form"
            invalid = await flow.async_step_reconfigure({CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.missing"})
            assert invalid["errors"]
            hass.states.async_set("sensor.vehicle_target", "80", {"unit_of_measurement": "%"})
            with patch.object(ConfigEntries, "async_schedule_reload") as reload:
                result = await flow.async_step_reconfigure({CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.vehicle_target"})
                assert result["reason"] == "repair_required"
                reload.assert_not_called()
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
        frame.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await asyncio.wait_for(er.async_load(hass, load_empty=True), timeout=10)
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
