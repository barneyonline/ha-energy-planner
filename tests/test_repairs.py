"""Migration repairs exercise real HA entry state transitions and restart storage."""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_energy_planner import async_migrate_entry, async_remove_entry, async_setup
from custom_components.ha_energy_planner.config_flow import ConfigFlow
from custom_components.ha_energy_planner.const import CONF_EV_SMART_CHARGING_TARGET_SOC as TARGET
from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.migration_recovery import (
    async_clear_migration_issue,
    async_create_migration_issue,
    async_refresh_migration_issues,
    async_save_vehicle_target,
    migration_issue_id,
)
from custom_components.ha_energy_planner.repairs import VehicleTargetRepairFlow, async_create_fix_flow

ROOT = Path(__file__).resolve().parents[1]


def make_entry():
    return ConfigEntry(
        domain=DOMAIN, title="Repair fixture", data={"ev_soc_entity": "sensor.car_soc"},
        options={"planner_enabled": False}, source="user", unique_id=None, version=3, minor_version=1,
        discovery_keys=MappingProxyType({}), subentries_data=[],
    )


async def prepare(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    loader.async_setup(hass)
    frame.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    hass.auth = await auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
    hass.states.async_set("sensor.target", "80", {"unit_of_measurement": "%"})
    return hass


def flow_for(hass, entry):
    flow = VehicleTargetRepairFlow(entry.entry_id)
    flow.hass = hass
    return flow


@pytest.mark.parametrize("use_reconfigure", [False, True])
def test_real_failed_setup_repair_and_restart(tmp_path, use_reconfigure):
    """Recover immediately on supported Core and preserve state through restart."""
    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        hass.config_entries._async_schedule_save()
        original_data, original_options = dict(entry.data), dict(entry.options)
        fixture = json.loads((ROOT / "tests/fixtures/upgrade/0.9.18.json").read_text())
        storage = tmp_path / ".storage"
        storage.mkdir(exist_ok=True)
        key = f"{DOMAIN}_state_{entry.entry_id}"
        (storage / key).write_text(json.dumps({**fixture["store"], "key": key}))
        try:
            assert not await hass.config_entries.async_setup(entry.entry_id)
            assert entry.state is ConfigEntryState.MIGRATION_ERROR
            assert entry.version == 3 and dict(entry.data) == original_data and dict(entry.options) == original_options
            issue_id = migration_issue_id(entry.entry_id)
            registry = ir.async_get(hass)
            assert registry.async_get_issue(DOMAIN, issue_id).is_fixable
            async_create_migration_issue(hass, entry)
            assert len([key for key in registry.issues if key[0] == DOMAIN]) == 1
            flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
            flow.hass = hass
            assert (await flow.async_step_init())["type"] == "form"
            assert (await flow.async_step_target({TARGET: "sensor.missing"}))["errors"][TARGET] == "entity_not_found"
            assert entry.version == 3 and dict(entry.data) == original_data
            with patch.object(ConfigEntries, "async_schedule_reload") as reload:
                if use_reconfigure:
                    reconfigure = ConfigFlow()
                    reconfigure.hass = hass
                    reconfigure.context = {"source": "reconfigure", "entry_id": entry.entry_id}
                    result = await reconfigure.async_step_reconfigure({TARGET: "sensor.target"})
                    assert result["reason"] == "repair_required"
                    result = await flow.async_step_target({TARGET: "sensor.target"})
                else:
                    result = await flow.async_step_target({TARGET: "sensor.target"})
                reload.assert_not_called()
            assert result["type"] == "create_entry"
            assert entry.state is ConfigEntryState.LOADED
            assert entry.version == 5
            assert entry.runtime_data.store.data["ownership"] == fixture["store"]["data"]["ownership"]
            assert entry.runtime_data.overrides[0].reason == "operator_requested"
            assert registry.async_get_issue(DOMAIN, issue_id) is None
            assert entry.data[TARGET] == "sensor.target"
            assert dict(entry.options) == original_options
            entry_id = entry.entry_id
        finally:
            await hass.async_stop(force=True)
        restarted = await prepare(tmp_path)
        try:
            restored = restarted.config_entries.async_get_entry(entry_id)
            assert restored is not None
            assert await restarted.config_entries.async_setup(entry_id)
            assert restored.state is ConfigEntryState.LOADED and restored.version == 5
            assert restored.runtime_data.store.data["ownership"] == fixture["store"]["data"]["ownership"]
            assert restored.runtime_data.overrides[0].reason == "operator_requested"
            assert restored.data[TARGET] == "sensor.target"
            assert restored.data["ev_soc_entity"] == original_data["ev_soc_entity"]
            assert dict(restored.options) == original_options
            assert ir.async_get(restarted).async_get_issue(DOMAIN, issue_id) is None
        finally:
            await restarted.async_stop(force=True)
    asyncio.run(run())


def test_shared_target_validation_and_legacy_subentries(tmp_path):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            for target in (None, "", "  ", 4):
                assert async_save_vehicle_target(hass, entry, {TARGET: target}) == {
                    TARGET: "ev_planning_sensor_required",
                }
            assert async_save_vehicle_target(hass, entry, {TARGET: "sensor.missing"}) == {TARGET: "entity_not_found"}
            for kind in ("ev", "vehicle", "system"):
                subentry = ConfigSubentry(data=MappingProxyType({TARGET: "sensor.old"}),
                                         subentry_type=kind, title=kind, unique_id=None)
                hass.config_entries.async_add_subentry(entry, subentry)
            assert async_save_vehicle_target(hass, entry, {TARGET: "sensor.target", "unrelated": "discard"}) == {}
            assert "unrelated" not in entry.data
            assert all(s.data[TARGET] == ("sensor.old" if s.subentry_type == "vehicle" else "sensor.target")
                       for s in entry.subentries.values())
            assert entry.version == 3 and entry.options["planner_enabled"] is False
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("scenario", [
    "removed", "foreign", "recovered", "disabled", "not_ready", "failed", "blocked", "loaded",
])
def test_repair_stale_entries_and_retry_outcomes(tmp_path, scenario):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        flow = flow_for(hass, entry)
        try:
            async_create_migration_issue(hass, entry)
            if scenario == "removed":
                hass.config_entries._entries.pop(entry.entry_id)
            elif scenario == "foreign":
                object.__setattr__(entry, "domain", "other")
            elif scenario == "recovered":
                hass.config_entries.async_update_entry(entry, version=5)
            elif scenario == "disabled":
                from homeassistant.config_entries import ConfigEntryDisabler
                object.__setattr__(entry, "disabled_by", ConfigEntryDisabler.USER)
            elif scenario != "not_ready":
                entry._async_set_state(hass, ConfigEntryState.MIGRATION_ERROR, None)

            async def retry(entry_id):
                assert entry_id == entry.entry_id
                if scenario == "failed":
                    raise HomeAssistantError("retry blocked")
                if scenario == "loaded":
                    entry._async_set_state(hass, ConfigEntryState.LOADED, None)

            with patch.object(
                ConfigEntries, "async_retry_migration", new=AsyncMock(side_effect=retry),
            ) as retry_mock:
                result = await flow.async_step_target({TARGET: "sensor.target"})
                if scenario in {"removed", "foreign", "recovered", "disabled"}:
                    reason = {"removed": "entry_removed", "foreign": "entry_removed",
                              "recovered": "already_repaired", "disabled": "entry_disabled"}[scenario]
                    assert result["reason"] == reason
                    assert TARGET not in entry.data
                    retry_mock.assert_not_called()
                elif scenario == "loaded":
                    assert result["type"] == "create_entry"
                else:
                    expected = "retry_not_ready" if scenario == "not_ready" else "retry_failed"
                    assert result["errors"]["base"] == expected
                    assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)) is not None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_issue_routing_and_cleanup(tmp_path):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            async_clear_migration_issue(hass, entry.entry_id)
            for data in (None, {}, {"entry_id": 4}, {"entry_id": entry.entry_id}):
                assert await async_create_fix_flow(hass, "unknown", data) is None
            async_create_migration_issue(hass, entry)
            with patch(
                "custom_components.ha_energy_planner.storage.PlannerStore.async_remove_if_safe", return_value=True,
            ):
                await async_remove_entry(hass, entry)
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)) is None
            async_create_migration_issue(hass, entry)
            async_save_vehicle_target(hass, entry, {TARGET: "sensor.target"})
            assert await async_migrate_entry(hass, entry)
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)) is None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_reconfigure_stale_form_and_loaded_entry(tmp_path):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        flow = ConfigFlow()
        flow.hass = hass
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        try:
            await flow.async_step_reconfigure()
            hass.config_entries.async_update_entry(entry, version=5)
            assert (await flow.async_step_reconfigure({TARGET: "sensor.target"}))["reason"] == "already_repaired"
            assert TARGET not in entry.data
            fresh = ConfigFlow()
            fresh.hass = hass
            fresh.context = flow.context
            with patch.object(ConfigEntries, "async_schedule_reload") as reload:
                result = await fresh.async_step_reconfigure({TARGET: "sensor.target"})
                assert result["reason"] == "reconfigure_successful"
                reload.assert_called_once_with(entry.entry_id)
            from homeassistant.config_entries import ConfigEntryDisabler
            object.__setattr__(entry, "disabled_by", ConfigEntryDisabler.USER)
            assert (await fresh.async_step_reconfigure())["reason"] == "entry_disabled"
            hass.config_entries._entries.pop(entry.entry_id)
            assert (await fresh.async_step_reconfigure())["reason"] == "entry_removed"
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_migration_raises_translated_error_for_missing_target(tmp_path):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        original = (dict(entry.data), dict(entry.options), entry.version)
        try:
            with pytest.raises(ConfigEntryError) as raised:
                await async_migrate_entry(hass, entry)
            assert raised.value.translation_key == "legacy_vehicle_target"
            assert (dict(entry.data), dict(entry.options), entry.version) == original
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)).is_fixable
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_repairs_manager_recovers_migration_and_clears_issue(tmp_path):
    """Use the real frontend flow manager, including init payload and abort cleanup."""
    from homeassistant.setup import async_setup_component

    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            assert not await hass.config_entries.async_setup(entry.entry_id)
            assert await async_setup_component(hass, "repairs", {})
            manager = hass.data["repairs"]["flow_manager"]
            issue_id = migration_issue_id(entry.entry_id)
            form = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
            assert form["step_id"] == "target" and not form["errors"]
            result = await manager.async_configure(form["flow_id"], {TARGET: "sensor.target"})
            assert result["type"] == "create_entry"
            assert entry.state is ConfigEntryState.LOADED and entry.version == 5
            assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("surface", ["repair", "reconfigure"])
def test_future_configuration_is_not_reported_as_repaired(tmp_path, surface):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        hass.config_entries.async_update_entry(entry, version=6)
        entry._async_set_state(hass, ConfigEntryState.MIGRATION_ERROR, None)
        original = dict(entry.data)
        try:
            assert not await async_migrate_entry(hass, entry)
            if surface == "repair":
                result = await flow_for(hass, entry).async_step_target({TARGET: "sensor.target"})
            else:
                flow = ConfigFlow()
                flow.hass = hass
                flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
                result = await flow.async_step_reconfigure({TARGET: "sensor.target"})
            assert result["reason"] == "unsupported_version"
            assert entry.version == 6 and dict(entry.data) == original
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)) is None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("surface", ["repair", "reconfigure"])
def test_repair_cannot_overwrite_target_during_migration(tmp_path, surface):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            async_create_migration_issue(hass, entry)
            flow = flow_for(hass, entry)
            assert (await flow.async_step_init())["type"] == "form"
            # Another recovery has saved its target and started setup while this form remains open.
            hass.states.async_set("sensor.other_target", "70", {"unit_of_measurement": "%"})
            hass.config_entries.async_update_entry(entry, data={**entry.data, TARGET: "sensor.other_target"})
            entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
            retry = AsyncMock()
            with patch.object(ConfigEntries, "async_retry_migration", new=retry):
                if surface == "repair":
                    result = await flow.async_step_target({TARGET: "sensor.target"})
                else:
                    reconfigure = ConfigFlow()
                    reconfigure.hass = hass
                    reconfigure.context = {"source": "reconfigure", "entry_id": entry.entry_id}
                    result = await reconfigure.async_step_reconfigure({TARGET: "sensor.target"})
            assert result["errors"]["base"] == "retry_not_ready"
            assert entry.data[TARGET] == "sensor.other_target"
            retry.assert_not_called()
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)).is_fixable
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("legacy_api", [False, True])
def test_startup_refresh_preserves_ignored_repairs_and_removes_stale_issues(tmp_path, legacy_api):
    """Reconcile real registry records, including ownerless records from older Core."""
    async def run():
        hass = await prepare(tmp_path)
        pending, retrying, migrated, removed, disabled = [make_entry() for _ in range(5)]
        for entry in (pending, retrying, migrated, removed, disabled):
            hass.config_entries._entries[entry.entry_id] = entry
        registry = ir.async_get(hass)
        try:
            # Simulate issues created by the previous integration, without ownership.
            for entry in (pending, retrying, migrated, removed, disabled):
                ir.async_create_issue(
                    hass, DOMAIN, migration_issue_id(entry.entry_id), is_fixable=True,
                    severity=ir.IssueSeverity.ERROR, translation_key="legacy_vehicle_target",
                )
                registry.async_ignore(DOMAIN, migration_issue_id(entry.entry_id), True)
            original = registry.async_get_issue(DOMAIN, migration_issue_id(pending.entry_id))
            hass.config_entries.async_update_entry(retrying, data={**retrying.data, TARGET: "sensor.target"})
            hass.config_entries.async_update_entry(migrated, version=5)
            hass.config_entries._entries.pop(removed.entry_id)
            from homeassistant.config_entries import ConfigEntryDisabler
            object.__setattr__(disabled, "disabled_by", ConfigEntryDisabler.USER)
            for domain, issue_id in ((DOMAIN, "unrelated"), ("other", migration_issue_id("foreign"))):
                ir.async_create_issue(
                    hass, domain, issue_id, is_fixable=False,
                    severity=ir.IssueSeverity.WARNING, translation_key="unrelated",
                )
            with patch.object(ir, "async_refresh_issues", None, create=True) if legacy_api else nullcontext():
                assert await async_setup(hass, {})
                assert await async_setup(hass, {})
            for entry in (pending, retrying, disabled):
                issue = registry.async_get_issue(DOMAIN, migration_issue_id(entry.entry_id))
                assert issue is not None and issue.dismissed_version == original.dismissed_version
                assert issue.data == {"entry_id": entry.entry_id}
                if not legacy_api and hasattr(ir, "async_refresh_issues"):
                    assert issue.config_entry_id == entry.entry_id
            assert registry.async_get_issue(DOMAIN, migration_issue_id(pending.entry_id)).created == original.created
            for entry in (migrated, removed):
                assert registry.async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)) is None
            assert registry.async_get_issue(DOMAIN, "unrelated") is not None
            assert registry.async_get_issue("other", migration_issue_id("foreign")) is not None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_new_registry_contract_scopes_refresh_and_sets_owner_on_every_creation(tmp_path):
    """Exercise optional API selection even on the advertised minimum Core."""
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            async_create_migration_issue(hass, entry)
            issue_id = migration_issue_id(entry.entry_id)
            stale_id = migration_issue_id("removed")
            ir.async_create_issue(
                hass, DOMAIN, stale_id, is_fixable=True,
                severity=ir.IssueSeverity.ERROR, translation_key="legacy_vehicle_target",
            )
            with (
                patch.object(ir, "async_refresh_issues", return_value=nullcontext(), create=True) as refresh,
                patch.object(ir, "async_create_issue") as create,
            ):
                async_refresh_migration_issues(hass)
                async_create_migration_issue(hass, entry)
                refresh.assert_called_once_with(hass, DOMAIN, issue_ids=[issue_id, stale_id])
                assert ir.async_get(hass).async_get_issue(DOMAIN, stale_id) is not None
                assert create.call_count == 2
                assert all(call.kwargs["config_entry_id"] == entry.entry_id for call in create.call_args_list)
                create.side_effect = HomeAssistantError("registry failed")
                with pytest.raises(HomeAssistantError, match="registry failed"):
                    async_create_migration_issue(hass, entry)
                assert create.call_count == 3
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.skipif(not hasattr(ir, "async_refresh_issues"), reason="Requires HA 2026.11 issue registry")
def test_native_refresh_cancels_cleanup_on_error(tmp_path):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            async_create_migration_issue(hass, entry)
            ir.async_create_issue(
                hass, DOMAIN, migration_issue_id("removed"), is_fixable=True,
                severity=ir.IssueSeverity.ERROR, translation_key="legacy_vehicle_target",
            )
            with patch.object(ir, "async_create_issue", side_effect=HomeAssistantError("registry failed")):
                with pytest.raises(HomeAssistantError, match="registry failed"):
                    async_refresh_migration_issues(hass)
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id("removed")) is not None
            assert ir.async_get(hass).async_get_issue(DOMAIN, migration_issue_id(entry.entry_id)) is not None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.skipif(not hasattr(ir, "async_refresh_issues"), reason="Requires HA 2026.11 issue registry")
def test_native_ownership_hides_disabled_issues_and_core_removes_only_the_owner(tmp_path):
    """Core ownership works independently of Planner's legacy removal fallback."""
    shutil.copytree(ROOT / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run():
        from homeassistant.config_entries import ConfigEntryDisabler

        hass = await prepare(tmp_path)
        first, second = make_entry(), make_entry()
        for entry in (first, second):
            hass.config_entries._entries[entry.entry_id] = entry
        registry = ir.async_get(hass)
        try:
            assert not await hass.config_entries.async_setup(first.entry_id)
            assert second.state is ConfigEntryState.MIGRATION_ERROR
            first_id, second_id = migration_issue_id(first.entry_id), migration_issue_id(second.entry_id)
            original = registry.async_ignore(DOMAIN, first_id, True)
            assert registry.async_get_issue(DOMAIN, first_id).config_entry_id == first.entry_id
            assert registry.async_get_issue(DOMAIN, second_id).config_entry_id == second.entry_id
            # A failed migration cannot be unloaded by a normal reload. Both
            # that attempt and a public migration retry must preserve dismissal.
            with pytest.raises(HomeAssistantError):
                await hass.config_entries.async_reload(first.entry_id)
            await hass.config_entries.async_retry_migration(first.entry_id)
            assert registry.async_get_issue(DOMAIN, first_id).dismissed_version == original.dismissed_version
            assert registry.async_get_issue(DOMAIN, first_id).created == original.created
            with pytest.raises(HomeAssistantError):
                await hass.config_entries.async_set_disabled_by(first.entry_id, ConfigEntryDisabler.USER)
            issue = registry.async_get_issue(DOMAIN, first_id)
            assert issue is not None and registry.async_is_hidden(issue)
            with pytest.raises(HomeAssistantError):
                await hass.config_entries.async_set_disabled_by(first.entry_id, None)
            issue = registry.async_get_issue(DOMAIN, first_id)
            assert not registry.async_is_hidden(issue) and issue.dismissed_version == original.dismissed_version
            with patch("custom_components.ha_energy_planner.async_remove_entry", new=AsyncMock()):
                result = await hass.config_entries.async_remove(first.entry_id)
            assert result["require_restart"] is False
            assert registry.async_get_issue(DOMAIN, first_id) is None
            assert registry.async_get_issue(DOMAIN, second_id) is not None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_startup_adopts_saved_ownerless_dismissal_and_cleans_stale_restart_records(tmp_path):
    async def run():
        hass = await prepare(tmp_path)
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        hass.config_entries._async_schedule_save()
        issue_id = migration_issue_id(entry.entry_id)
        try:
            for identity in (issue_id, migration_issue_id("removed")):
                ir.async_create_issue(
                    hass, DOMAIN, identity, is_fixable=True,
                    severity=ir.IssueSeverity.ERROR, translation_key="legacy_vehicle_target",
                )
                ir.async_get(hass).async_ignore(DOMAIN, identity, True)
            original = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        finally:
            await hass.async_stop(force=True)
        restarted = await prepare(tmp_path)
        try:
            await ir.async_load(restarted)
            registry = ir.async_get(restarted)
            assert not registry.async_get_issue(DOMAIN, issue_id).active
            assert registry.async_get_issue(DOMAIN, migration_issue_id("removed")) is not None
            assert await async_setup(restarted, {})
            issue = registry.async_get_issue(DOMAIN, issue_id)
            assert issue.active and issue.dismissed_version == original.dismissed_version
            assert issue.created == original.created and issue.data == {"entry_id": entry.entry_id}
            if hasattr(ir, "async_refresh_issues"):
                assert issue.config_entry_id == entry.entry_id
            assert registry.async_get_issue(DOMAIN, migration_issue_id("removed")) is None
        finally:
            await restarted.async_stop(force=True)
    asyncio.run(run())
