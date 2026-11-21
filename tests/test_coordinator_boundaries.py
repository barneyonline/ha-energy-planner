"""Optional runtime callbacks and stale transactions retain their safety gates."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_coordinator import (
    FakeEvent,
    FakeHass,
    _coordinated_action,
    _coordinator_for_commit,
    _coordinator_for_runtime_services,
    _plan,
)
from test_lifecycle import FakeConfigEntries, FakeCoordinator, FakeEntry, FakeStore
from test_lifecycle import FakeHass as LifecycleHass

from custom_components.ha_energy_planner import _async_update_listener, async_unload_entry
from custom_components.ha_energy_planner import coordinator as module
from custom_components.ha_energy_planner.const import (
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_EV_CHARGING,
    CONF_EV_CONTROL_ENABLED,
    CONF_HOUSEHOLD_LOAD,
)
from custom_components.ha_energy_planner.ev_adapter import EVCommandResult
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind


def test_configuration_reload_in_review_mode_has_no_restore_or_rearm():
    owner = _coordinator_for_runtime_services(options={"planner_enabled": False})
    asyncio.run(owner.async_prepare_configuration_reload())
    assert owner._configuration_reload_handoff is False
    assert owner.executor.restored == []


def test_native_ev_option_updates_without_config_entry_writer():
    async def run():
        owner = _coordinator_for_runtime_services()
        owner.hass.config_entries = SimpleNamespace()
        owner.async_handle_options_update = AsyncMock()
        await owner.async_set_ev_low_price_threshold(0.1)
        await owner.async_set_ev_keep_charger_on(False)
        assert owner.async_handle_options_update.await_count == 2

    asyncio.run(run())


def test_failed_manual_ev_command_does_not_replace_override():
    async def run():
        owner = _coordinator_for_runtime_services()
        original = list(owner.overrides)
        owner.executor.async_manual_ev_charging = AsyncMock(return_value=EVCommandResult(False, "unavailable", {}, {}))
        result = await owner.async_manual_ev_charging(True)
        assert not result.applied
        assert owner.overrides == original

    asyncio.run(run())


def test_manual_hvac_override_without_release_callback_retains_durable_override():
    owner = _coordinator_for_runtime_services(options={CONF_CLIMATE_CONTROL_ENABLED: True})
    owner.executor.async_release_hvac_control = None
    asyncio.run(owner.async_set_manual_hvac_override(30, "manual_test"))
    assert any(override.kind == "manual_hvac" for override in owner.overrides)


def test_expired_helper_with_no_cleanup_override_needs_no_override_write():
    owner = _coordinator_for_runtime_services()
    owner._async_clear_expired_manual_hvac_state = AsyncMock(return_value=True)
    asyncio.run(owner._async_reconcile_expired_manual_hvac_state(True))
    owner._async_clear_expired_manual_hvac_state.assert_awaited_once()
    assert owner.overrides == []


def test_expiry_removed_during_helper_cleanup_does_not_overwrite_ownership():
    owner = _coordinator_for_runtime_services()
    owner.store.data["ownership"] = {"other_device": True}
    owner.store.async_save_ownership = AsyncMock()
    assert asyncio.run(owner._async_clear_expired_manual_hvac_state())
    owner.store.async_save_ownership.assert_not_awaited()
    assert owner.store.data["ownership"] == {"other_device": True}


def test_ev_auto_start_compensation_can_finish_without_refresh():
    owner = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGING: "sensor.charging"},
        options={"planner_enabled": True, "dry_run": False, CONF_EV_CONTROL_ENABLED: True},
        hass=FakeHass({"sensor.charging": "charging"}),
        store_data={"production": {"armed": True}},
    )
    owner.async_update_listeners = lambda: None
    result = asyncio.run(owner._async_compensate_ev_auto_start(require_unowned=False, refresh=False))
    assert result.applied
    assert owner.refresh_requested == 0


def test_export_reconciliation_without_mapping_does_not_acquire_command_lock():
    owner = _coordinator_for_runtime_services()
    owner._command_lock = None
    asyncio.run(owner._async_reconcile_export_limit())


@pytest.mark.parametrize("subscription", ["missing", "removed"])
def test_load_source_callback_handles_a_subscription_already_removed(monkeypatch, subscription):
    owner = _coordinator_for_runtime_services(entry_data={CONF_HOUSEHOLD_LOAD: "sensor.load"})
    owner._unsub_listeners = []
    owner._load_forecast_training_attempted = False
    scheduled = []
    callbacks = []
    unsubscribed = []

    def track(hass, entities, callback):
        callbacks.append(callback)
        return None if subscription == "missing" else lambda: unsubscribed.append(True)

    monkeypatch.setattr(module, "async_track_state_change_event", track)
    owner._schedule_debounced_refresh = lambda *args, **kwargs: scheduled.append(args)
    owner._start_load_forecast_source_listener(owner.entry_data)
    owner._unsub_listeners.clear()
    callbacks[0](FakeEvent("sensor.load", "unavailable", "1.2"))
    assert len(scheduled) == 1
    assert unsubscribed == ([] if subscription == "missing" else [True])


def test_validation_for_another_plan_does_not_mark_it_committed():
    owner = _coordinator_for_commit(None, current_generation=1)
    owner._startup_auto_recovery_validation_active = True
    owner._last_startup_auto_recovery_validation = {"plan_id": "old", "committed": False}
    plan = _plan("current")
    assert asyncio.run(owner._async_commit_plan_if_current(1, plan, object(), {})) is plan
    assert owner._last_startup_auto_recovery_validation["committed"] is False
    assert owner.executor.evaluated == []


def test_plan_invalidated_during_calendar_render_without_hass_is_discarded(monkeypatch):
    from custom_components.ha_energy_planner import calendar

    owner = _coordinator_for_commit(_plan("previous"), current_generation=1)

    def render(*args):
        owner._refresh_generation += 1
        return []

    monkeypatch.setattr(calendar, "calendar_event_records", render)
    result = asyncio.run(owner._async_commit_plan_if_current(1, _plan("stale"), object(), {}))
    assert result is owner.data
    assert owner.store.saved_plans == []


def test_next_action_stops_after_generation_changes_without_hass():
    owner = _coordinator_for_commit(None, current_generation=1)
    plan = _plan("current")
    plan.actions = [
        _coordinated_action(plan, name, asset, kind)
        for name, asset, kind in [
            ("first", ActionAsset.EV, ActionKind.EV_START),
            ("second", ActionAsset.ENPHASE, ActionKind.SET_PROFILE),
        ]
    ]

    async def invalidate(*args):
        owner._refresh_generation += 1

    owner.executor.async_evaluate = AsyncMock(side_effect=invalidate)
    asyncio.run(owner._async_commit_plan_if_current(1, plan, object(), {"planner_enabled": True}))
    owner.executor.async_evaluate.assert_awaited_once()


@pytest.mark.parametrize("topology_changed", [False, True])
def test_legacy_update_listener_without_optional_callbacks(topology_changed):
    entry = FakeEntry()
    owner = SimpleNamespace(entry_update_lock=asyncio.Lock(), entry_topology_signature=() if topology_changed else None)
    entry.runtime_data = owner
    hass = LifecycleHass(FakeConfigEntries())
    asyncio.run(_async_update_listener(hass, entry))
    assert hass.config_entries.reloads == ([entry.entry_id] if topology_changed else [])


def test_failed_platform_unload_during_shutdown_keeps_runtime():
    hass = LifecycleHass(FakeConfigEntries(unload_ok=False))
    from homeassistant.core import CoreState

    hass.state = CoreState.stopping
    entry = FakeEntry()
    owner = FakeCoordinator(hass, entry, FakeStore(hass))
    owner.automatic_control_requested = True
    entry.runtime_data = owner
    assert asyncio.run(async_unload_entry(hass, entry)) is False
    assert entry.runtime_data is owner
