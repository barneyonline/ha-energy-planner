"""An automation guard follows ownership, including slow commands and recovery."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner import storage as storage_module
from custom_components.ha_energy_planner.binary_sensor import BINARY_SENSORS, PlannerBinarySensor
from custom_components.ha_energy_planner.storage import PlannerStore

DESCRIPTION = next(item for item in BINARY_SENSORS if item.key == "climate_control_active")


def entity_for(store, entry_id="entry-one", description=DESCRIPTION):
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id=entry_id), store=store,
        async_add_listener=lambda *args: lambda: None,
        # Authority and device power must not erase unresolved ownership.
        active_control=False, dry_run=True, options={"climate_control_enabled": False},
    )
    return PlannerBinarySensor(coordinator, description)


@pytest.mark.parametrize("ownership,expected", [
    ({}, False),
    ({"manual_hvac_override_expires_at": "2099-01-01T00:00:00+00:00"}, False),
    ({"hvac_release_hold_until": "2099-01-01T00:00:00+00:00"}, False),
    ({"enphase_export_limit": {"baseline": {"watts": None}}, "ev_smart_charging_state": {"state": "on"}}, False),
    ({"hvac_control": {"phase": "preconditioning", "main_state_committed": False}}, True),
    ({"hvac_control": {"phase": "preconditioning", "main_state_committed": True}}, True),
    ({"hvac_control": {"phase": "pre_peak_coast"}}, True),
    ({"hvac_control": {"phase": "peak_coast"}}, True),
    ({"hvac_control": {"phase": "away_off"}}, True),
    ({"hvac_control": {"required_evidence_lost": "hvac_release_failed"}}, True),
    ({"hvac_control": {"command_confirmation": {"status": "readback_pending"}}}, True),
    ({"climate_automations": {"automation.climate": "on"}}, True),
    ({"planner_takeover_started_at": "2020-01-01T00:00:00+00:00"}, True),
    ({"planner_hvac_action_expires_at": "2020-01-01T00:00:00+00:00"}, True),
    ({"hvac_control": "unknown_legacy_record"}, True),
    (None, True),
    ([], True),
])
def test_ownership_signal_is_independent_of_power_and_command_authority(ownership, expected):
    entity = entity_for(SimpleNamespace(data={"ownership": ownership}))
    assert entity.is_on is expected
    assert entity.unique_id == "entry-one_climate_control_active"
    assert entity.entity_description.entity_registry_enabled_default
    assert set(entity.extra_state_attributes) == {"phase", "restoration_pending", "command_pending"}


class MemoryDisk:
    """Only disk I/O is replaced; ownership publication uses the real Store wrapper."""

    def __init__(self, *args, **kwargs):
        self.saved = None

    async def async_save(self, data):
        self.saved = deepcopy(data)

    async def async_load(self):
        return self.saved


def test_sensor_updates_before_disk_or_commands_and_through_failed_restore(monkeypatch):
    async def run():
        monkeypatch.setattr(storage_module, "Store", MemoryDisk)
        store = PlannerStore(object(), "entry-one")
        other = entity_for(PlannerStore(object(), "entry-two"), entry_id="entry-two")
        entity = entity_for(store)
        armed = entity_for(store, description=BINARY_SENSORS[0])
        writes, removers = [], []
        monkeypatch.setattr(entity, "async_write_ha_state", lambda: writes.append(
            (entity.is_on, entity.extra_state_attributes)))
        monkeypatch.setattr(entity, "async_on_remove", removers.append)
        await entity.async_added_to_hass()
        await armed.async_added_to_hass()
        assert len(store._ownership_listeners) == 1  # Armed keeps its coordinator-only lifecycle.
        assert other.unique_id == "entry-two_climate_control_active"

        entered, release = asyncio.Event(), asyncio.Event()
        original_save = store._store.async_save

        async def slow_disk(data):
            entered.set()
            await release.wait()
            await original_save(data)

        monkeypatch.setattr(store._store, "async_save", slow_disk)
        acquire = asyncio.create_task(store.async_save_ownership({"hvac_control": {"phase": "preconditioning"}}))
        await entered.wait()
        assert writes[-1][0] is True  # Observable even before acquisition durability finishes.
        assert not acquire.done()
        assert other.is_on is False  # Another entry cannot claim this unit's ownership.
        release.set()
        await acquire
        # This is the executor's service boundary, after acquisition persistence.
        assert entity.is_on is True
        assert writes[-1][1]["phase"] == "preconditioning"
        await store.async_save_ownership({"hvac_control": {"phase": "peak_coast"}})
        assert writes[-1][0] is True
        await store.async_save_ownership({"hvac_control": {"required_evidence_lost": "hvac_release_failed"}})
        assert writes[-1][1]["restoration_pending"] is True
        await store.async_clear_ownership()
        assert writes[-1][0] is False
        count = len(writes)
        await store.async_save_ownership({})
        await store.async_save_production({"armed": True})
        assert len(writes) == count

        for remove in removers:
            remove()
        await store.async_save_ownership({"hvac_control": {"phase": "preconditioning"}})
        assert len(writes) == count

    asyncio.run(run())


def test_persisted_unresolved_ownership_is_on_after_restart(monkeypatch):
    async def run():
        monkeypatch.setattr(storage_module, "Store", MemoryDisk)
        store = PlannerStore(object(), "entry-one")
        await store.async_save_ownership({"hvac_control": {
            "required_evidence_lost": "hvac_acquisition_rollback_failed",
            "command_confirmation": {"status": "uncertain"},
        }})
        restarted = PlannerStore(object(), "entry-one")
        restarted._store.saved = store._store.saved
        await restarted.async_load()
        entity = entity_for(restarted)
        assert entity.is_on is True
        assert entity.extra_state_attributes["restoration_pending"] is True
        assert entity.extra_state_attributes["command_pending"] is True
        await restarted.async_save_ownership({"manual_hvac_override_expires_at": "2099-01-01T00:00:00+00:00"})
        assert entity.is_on is False

    asyncio.run(run())


def test_broken_observer_cannot_block_durable_ownership_or_other_observers(monkeypatch, caplog):
    monkeypatch.setattr(storage_module, "Store", MemoryDisk)
    store = PlannerStore(object(), "entry-one")
    notifications = []

    def broken():
        raise RuntimeError("synthetic observer failure")

    remove = store.async_add_ownership_listener(broken)
    store.async_add_ownership_listener(lambda: notifications.append(True))
    asyncio.run(store.async_save_ownership({"hvac_control": {"phase": "preconditioning"}}))
    assert store._store.saved["ownership"] == store.data["ownership"]
    assert notifications == [True]
    assert "Error publishing planner ownership state" in caplog.text
    remove()
    remove()
    asyncio.run(store.async_clear_ownership())
    assert notifications == [True, True]
