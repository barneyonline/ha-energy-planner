"""Recovery writes must be acknowledged by the actual Home Assistant Store."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError
from test_enphase_adapter import _action, _entry_data

from custom_components.ha_energy_planner import async_remove_entry
from custom_components.ha_energy_planner.enphase_adapter import EnphaseProfileAdapter
from custom_components.ha_energy_planner.enphase_control import EnphaseControlTransaction
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.models import ActionKind
from custom_components.ha_energy_planner.storage import PlannerStore


@pytest.mark.parametrize("failure", [WriteError, SerializationError])
def test_real_write_failure_blocks_dispatch_and_retries(tmp_path: Path, failure) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        await store.async_save_ownership({"enphase_profile": "AI Optimisation"})
        original = Path(store._store.path).read_bytes()
        calls = []
        hass.states.async_set("select.enphase_profile", "AI Optimisation")
        async def select(call):
            calls.append(call)
            hass.states.async_set(call.data["entity_id"], call.data["option"])
        hass.services.async_register("select", "select_option", select)
        async def fail(self, data):
            raise failure("synthetic write failure")
        try:
            with patch.object(Store, "_async_write_data", fail):
                with pytest.raises(HomeAssistantError):
                    await EnphaseControlTransaction(
                        store, EnphaseProfileAdapter(hass, _entry_data()), datetime.now(UTC)
                    ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
            assert calls == []
            assert store._is_dirty
            assert Path(store._store.path).read_bytes() == original
            await store.async_flush()
            assert not store._is_dirty
            restarted = PlannerStore(hass, "runtime")
            await restarted.async_load()
            assert restarted.data["ownership"] == store.data["ownership"]
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_real_store_flushes_during_stopping_and_rejects_read_only(tmp_path: Path) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        try:
            hass.set_state(CoreState.stopping)
            await store.async_save_ownership({"enphase_profile": "Full Backup"})
            assert json.loads(Path(store._store.path).read_text())["data"]["ownership"] == store.data["ownership"]
            store._store.make_read_only()
            with pytest.raises(HomeAssistantError):
                await store.async_save_ownership({"enphase_profile": "AI Optimisation"})
            assert store._is_dirty
        finally:
            hass.set_state(CoreState.running)
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_existing_enphase_restore_can_dispatch_when_storage_is_unwritable(tmp_path: Path) -> None:
    """The acquisition persistence guard must not gate mandatory compensation."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        await store.async_save_ownership({"enphase_profile": "AI Optimisation"})
        hass.states.async_set("select.enphase_profile", "Full Backup")
        async def select(call):
            hass.states.async_set(call.data["entity_id"], call.data["option"])
        hass.services.async_register("select", "select_option", select)
        async def fail(self, data):
            raise WriteError("synthetic write failure")
        try:
            with patch.object(Store, "_async_write_data", fail):
                with pytest.raises(HomeAssistantError):
                    await store.async_save_production({"armed": False})
                with pytest.raises(HomeAssistantError):
                    await Executor(store, hass=hass, entry_data=_entry_data()).async_restore_safe_state("test")
                assert hass.states.get("select.enphase_profile").state == "AI Optimisation"
                assert store._is_dirty
                restarted = PlannerStore(hass, "runtime")
                await restarted.async_load()
                assert restarted.data["ownership"]["enphase_profile"] == "AI Optimisation"
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_cancelled_write_drains_before_next_generation(tmp_path: Path) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        reached, release = asyncio.Event(), asyncio.Event()
        original = Store._async_write_data
        async def hold(self, data):
            reached.set()
            await release.wait()
            await original(self, data)
        try:
            with patch.object(Store, "_async_write_data", hold):
                first = asyncio.create_task(store.async_save_ownership({"enphase_profile": "original"}))
                await reached.wait()
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                second = asyncio.create_task(store.async_save_production({"armed": False}))
                await asyncio.sleep(0)
                assert not first.done() and not second.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await first
                await second
            assert not store._is_dirty
            persisted = json.loads(Path(store._store.path).read_text())["data"]
            assert persisted["ownership"] == {"enphase_profile": "original"}
            assert persisted["production"] == {"armed": False}
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("ownership,reservation,removed", [
    ({}, {}, True), ({}, {"active": False}, True),
    ({"enphase_profile": "original"}, {}, False),
    ([], {}, False), ({}, [], False), ({}, {"active": True}, False),
    ({}, {"load_kw": 7}, False),
])
def test_entry_removal_preserves_uncertain_evidence(tmp_path: Path, ownership, reservation, removed) -> None:
    from types import SimpleNamespace
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        try:
            await store._store.async_save({"ownership": ownership, "ev_grid_reservation": reservation})
            await async_remove_entry(hass, SimpleNamespace(entry_id="runtime"))
            assert Path(store._store.path).exists() is not removed
            empty = PlannerStore(hass, "absent")
            assert await empty.async_remove_if_safe()
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())
