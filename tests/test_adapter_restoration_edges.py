"""Restoration paths keep their saved baseline and optional adapter contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_executor import FakeStore
from test_hvac_adapter import FakeHass, FakeState

from custom_components.ha_energy_planner.const import CONF_DAIKIN_CLIMATE
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter


@pytest.mark.parametrize(
    ("initial", "desired"),
    [
        ("heat", {"hvac_mode": "off"}),
        ("off", {"hvac_mode": "cool"}),
        ("heat", {"target_temp_low": 18, "target_temp_high": 24}),
    ],
)
def test_restoration_commands_can_complete_without_manual_override_checks(initial, desired):
    async def run():
        hass = FakeHass({"climate.main": FakeState(initial, {"temperature": 20})})
        adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.main"})
        adapter._async_persist_main_state = AsyncMock()
        snapshot = {"hvac_mode": "off"}
        sent = await adapter._async_apply_hvac_state(
            "climate.main",
            desired,
            respect_manual_override=False,
            takeover_main_state=snapshot,
        )
        assert sent
        observed = hass.states.get("climate.main")
        if desired.get("hvac_mode"):
            assert observed.state == desired["hvac_mode"]
        else:
            assert observed.attributes["target_temp_low"] == 18
            assert observed.attributes["target_temp_high"] == 24
        if initial == "off":
            adapter._async_persist_main_state.assert_awaited()
            assert snapshot["rollback_hvac_mode_changed"] is True

    asyncio.run(run())


def test_releasing_unowned_hvac_without_mapped_automations_needs_no_commands():
    adapter = DaikinHVACAdapter(FakeHass({}), {})
    result = asyncio.run(adapter.async_restore({}))
    assert not result.applied
    assert result.reason == "no_hvac_automation_state_saved"


def test_executor_without_runtime_cannot_reinstate_shared_grid_claim():
    executor = Executor(FakeStore())
    executor._restore_ev_grid_reservation({"load_kw": 3})
    executor._set_ev_grid_shedding_claim("entry")
    assert executor._ev_grid_domain_data() is None
    executor.store = SimpleNamespace(data={})
    asyncio.run(executor._async_flush_provisional_state())


def test_attempt_order_does_not_replace_newer_action_clock_with_older_one():
    from custom_components.ha_energy_planner.executor import _latest_ev_safety_stop_failure_at

    now = datetime(2026, 11, 6, tzinfo=UTC)
    assert (
        _latest_ev_safety_stop_failure_at(
            [
                {
                    "asset": "ev",
                    "kind": "ev_stop",
                    "result": "failed",
                    "desired_state": {"ev_safety_stop": True},
                    "attempted_at": now.isoformat(),
                },
                {
                    "asset": "ev",
                    "kind": "ev_stop",
                    "result": "failed",
                    "desired_state": {"ev_safety_stop": True},
                    "attempted_at": (now - timedelta(minutes=1)).isoformat(),
                },
            ]
        )
        == now
    )


def test_unowned_restore_without_runtime_has_nothing_to_dispatch():
    executor = Executor(FakeStore())
    result = asyncio.run(executor.async_restore_safe_state("manual"))
    assert result.result == "restored"


def test_deferred_notification_failure_does_not_cache_success():
    executor = Executor(FakeStore())
    asyncio.run(executor._async_create_deferred_notification(title="title", message="message", notification_id="id"))
    assert executor._plan_fallback_notification_signatures == {}


def test_climate_target_notification_without_runtime_is_best_effort():
    from test_coordinator import _plan

    from custom_components.ha_energy_planner.models import PlannerMode

    plan = _plan("missing-target")
    plan.mode = PlannerMode.ACTIVE_HEALTHY
    plan.input_issues = ["main_climate_target_unavailable"]
    executor = Executor(FakeStore())
    asyncio.run(executor.async_notify_plan_fallback(plan, []))
    assert executor._plan_fallback_notification_signatures == {}


def test_provisional_ev_identity_is_retryable_without_another_storage_write():
    from test_calendar import _action

    store = FakeStore()
    executor = Executor(store)
    action = _action("ev", datetime.now(UTC), datetime.now(UTC))

    async def run():
        assert await executor._async_save_provisional_ev_ownership(action, {"ev_charger_entity": "switch.ev"})
        saved = dict(store.data["ownership"])
        store.async_save_ownership = AsyncMock()
        assert await executor._async_save_provisional_ev_ownership(action, {"ev_charger_entity": "switch.ev"})
        store.async_save_ownership.assert_not_awaited()
        assert store.data["ownership"] == saved

    asyncio.run(run())


def test_shared_shedding_claim_without_entry_registry_retains_existing_claim():
    from custom_components.ha_energy_planner.const import DOMAIN

    executor = Executor(FakeStore(), hass=SimpleNamespace(data={DOMAIN: {"ev_grid_reservations": {"entry": {}}}}))
    executor._set_ev_grid_shedding_claim("entry")
    assert executor._ev_grid_shedding_claim() == "entry"


def test_zone_only_supersession_keeps_main_rollback_baseline():
    from custom_components.ha_energy_planner.hvac_adapter import HVACCommandResult
    from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction

    previous = {"hvac_control": {"main_state": {"hvac_mode": "heat"}, "zone_states": {"switch.zone": "off"}}}
    transaction = HVACOwnershipTransaction(previous, datetime.now(UTC))
    result = transaction.complete(
        HVACCommandResult(False, "failed", {}, {}, {}, True),
        previous,
        {},
        main_state_superseded=False,
        superseded_zone_entity_ids={"switch.zone"},
    )
    assert result["hvac_control"]["main_state"] == {"hvac_mode": "heat"}
    assert result["hvac_control"]["zone_states"] == {}


def test_missing_grid_projection_does_not_create_first_reservation():
    from test_calendar import _action
    from test_planner import _context

    from custom_components.ha_energy_planner.ev_control import reserve_ev_grid_capacity

    context = _context()
    context.slots[0].baseline_load_forecast_kw = None
    action = _action("ev", context.created_at, context.created_at)
    reservations = {}
    reason, previous = reserve_ev_grid_capacity(reservations, "entry", action, context, context.created_at, {})
    assert reason == "ev_grid_projection_unavailable"
    assert previous is None
    assert reservations == {}


def test_suspended_vehicle_without_current_slot_remains_stopped():
    from test_planner import _context

    from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    context = _context()
    context.slots = []
    context.ev_evidence["vehicle_suspended"] = True
    plan = DryRunPlanner({**DEFAULT_OPTIONS, "planner_enabled": True, "ev_control_enabled": True}).create_plan(context)
    ev = next(action for action in plan.actions if str(action.asset) == "ev")
    assert ev.desired_state["charging_required_now"] is False
    assert ev.desired_state["charging_reason"] == "ev_vehicle_suspended"


def test_matching_enphase_profile_is_not_an_external_conflict():
    from test_calendar import _action
    from test_executor import FakeHass as ExecutorHass

    from custom_components.ha_energy_planner.models import ActionAsset, ActionKind

    now = datetime.now(UTC)
    action = _action("profile", now, now + timedelta(minutes=5))
    action.asset, action.kind = ActionAsset.ENPHASE, ActionKind.SET_PROFILE
    action.desired_state = {"profile": "Self Consumption"}
    store = FakeStore()
    store.data["execution_audit"] = [
        {
            "asset": "enphase",
            "kind": "set_profile",
            "result": "applied",
            "attempted_at": now.isoformat(),
            "post_state": {"current_profile": "Self Consumption"},
        }
    ]
    executor = Executor(
        store,
        hass=ExecutorHass({"select.profile": "Self Consumption"}),
        entry_data={"enphase_profile_entity": "select.profile"},
    )
    assert executor._observed_conflict_reason(action, now) is None


def test_owned_ev_safety_stop_does_not_discard_other_device_action(monkeypatch):
    from test_calendar import _action, _plan

    from custom_components.ha_energy_planner.models import ActionAsset, ActionKind

    now = datetime.now(UTC)
    action = _action("profile", now, now + timedelta(minutes=5))
    action.asset, action.kind = ActionAsset.ENPHASE, ActionKind.SET_PROFILE
    stop = _action("stop", now, now + timedelta(minutes=5))
    stop.kind = ActionKind.EV_STOP
    executor = Executor(FakeStore())
    monkeypatch.setattr(executor, "_owned_ev_safety_stop", lambda *args: stop)
    # The nested evaluation shares the same attempted-plan marker and terminates
    # through the ordinary disabled-mode gate rather than recursively stopping.
    asyncio.run(executor.async_evaluate(_plan([action])))
    assert executor._ev_safety_stop_attempted_plan_id == "plan-1"
    assert [outcome.asset for outcome in executor.store.data["outcomes"]] == [ActionAsset.EV, ActionAsset.ENPHASE]
