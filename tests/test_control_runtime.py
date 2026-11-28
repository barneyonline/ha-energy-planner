"""Compose real HA storage/services with actuator transactions and fresh runtimes."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import Context, CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers import storage as ha_storage
from homeassistant.util import dt as dt_util

from custom_components.ha_energy_planner import (
    _rehydrate_ev_grid_reservation,
    adapter_helpers,
    async_setup_entry,
    ev_adapter,
)
from custom_components.ha_energy_planner.config_flow import INPUT_STEP_CLIMATE, OptionsFlow
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_IMPORT_PRICE,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator
from custom_components.ha_energy_planner.discovery import CapabilityDiscovery
from custom_components.ha_energy_planner.entry_data import combined_entry_data
from custom_components.ha_energy_planner.ev_adapter import EVChargerAdapter, EVCommandResult
from custom_components.ha_energy_planner.ev_control import ev_grid_headroom_reason
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
from custom_components.ha_energy_planner.inputs import InputManager
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    OutcomeResult,
    Override,
    PlanAction,
    PlannerMode,
)
from custom_components.ha_energy_planner.planner import DryRunPlanner
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint
from custom_components.ha_energy_planner.storage import PlannerStore


def _devices(hass: HomeAssistant) -> list[tuple[str, str, dict[str, Any]]]:
    """Only devices are simulated; dispatch and published states use real HA."""
    calls = []
    hass.states.async_set("select.profile", "Original profile")
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("binary_sensor.connected", "on")
    hass.states.async_set("climate.home", "heat", {"temperature": 20.0})
    hass.states.async_set("automation.climate", "on")
    hass.states.async_set("switch.zone", "off")

    async def actuate(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))
        entity_id = call.data["entity_id"]
        previous = hass.states.get(entity_id)
        state = previous.state
        attrs = dict(previous.attributes)
        if call.service in {"turn_on", "turn_off"}:
            state = "on" if call.service == "turn_on" else "off"
        elif call.service == "select_option":
            state = call.data["option"]
        elif call.service == "set_hvac_mode":
            state = call.data["hvac_mode"]
        elif call.service == "set_temperature":
            attrs["temperature"] = call.data["temperature"]
        hass.states.async_set(entity_id, state, attrs, context=call.context)

    for domain, services in {
        "switch": ("turn_on", "turn_off"),
        "automation": ("turn_on", "turn_off"),
        "select": ("select_option",),
        "climate": ("turn_on", "turn_off", "set_hvac_mode", "set_temperature"),
    }.items():
        for service in services:
            hass.services.async_register(domain, service, actuate)
    return calls


def _executor(hass: HomeAssistant, store: PlannerStore) -> Executor:
    return Executor(
        store, hass=hass, entry_id="runtime",
        entry_data={
            CONF_ENPHASE_PROFILE: "select.profile", CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
            CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected",
            CONF_DAIKIN_CLIMATE: "climate.home", CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_ZONES: "switch.zone",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True, "dry_run": False,
            "ev_control_enabled": True, "enphase_control_enabled": True, "climate_control_enabled": True,
            "ev_charge_rate_kw": 7.0, "grid_import_limit_kw": 10.0,
        },
    )


def _command(area: str) -> tuple[EnergyPlan, DecisionContext | None]:
    now = dt_util.utcnow()
    asset, kind, desired = {
        "enphase": (ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {"profile": "Self Consumption"}),
        "ev": (ActionAsset.EV, ActionKind.EV_START, {"charging_required_now": True, "projected_load_kw_now": 7.0}),
        "hvac": (ActionAsset.DAIKIN, ActionKind.SET_HVAC,
                 {"hvac_mode": "heat", "target_temperature": 23.0, "enable_zones": True}),
    }[area]
    action = PlanAction(
        "command", "plan", now - timedelta(minutes=1), now + timedelta(minutes=5), asset, kind,
        desired, [], [], None, 1.0,
    )
    plan = EnergyPlan(
        "plan", now, 24, 5, "current", InputHealth.HEALTHY, PlannerMode.ACTIVE_HEALTHY,
        "runtime", 1.0, None, [action], [],
    )
    context = DecisionContext(
        created_at=now, plan_id="plan", slots=[DecisionSlot(now, 0.1, 0.05, 0, 1, projected_ev_load_kw=7)],
        current_battery_soc_percent=50, current_ev_soc_percent=40,
        occupancy_state=OccupancyState.OCCUPIED, input_health=InputHealth.HEALTHY, ev_connected=True,
    )
    return plan, context if area == "ev" else None


def _arm(store: PlannerStore, executor: Executor) -> None:
    store.data["production"] = {
        "armed": True, "dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(executor.entry_data, executor.options),
    }


@pytest.mark.parametrize("hvac_action", ["release", "continuation", None])
@pytest.mark.parametrize("superseded", [False, True])
def test_owned_ev_safety_stop_preserves_hvac_release_for_current_plan(tmp_path, hvac_action, superseded) -> None:
    """Recheck coordinated HVAC intent after a confirmed EV safety stop."""
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "on")
        hass.states.async_set("binary_sensor.connected", "off")
        hass.states.async_set("automation.climate", "off")
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        store.data["ownership"] = {
            "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
            "hvac_control": {"phase": "preconditioning"},
            "climate_automations": {"automation.climate": "on"},
        }
        plan, _ = _command("hvac")
        _, context = _command("ev")
        assert context is not None
        context = replace(context, ev_connected=False)
        if hvac_action == "release":
            plan.actions = [replace(
                plan.actions[0], kind=ActionKind.RELEASE_HVAC,
                desired_state={"release_reason": "comfort",
                               "released_until": (plan.created_at + timedelta(hours=1)).isoformat()},
            )]
        elif hvac_action == "continuation":
            plan.actions[0].desired_state["phase"] = "peak_coast"
        else:
            plan.actions = []
        current = True

        async def stop_charger(call: ServiceCall) -> None:
            nonlocal current
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("switch.charger", "off", context=call.context)
            current = not superseded

        hass.services.async_register("switch", "turn_off", stop_charger)
        try:
            await executor.async_evaluate(plan, context, plan_is_current=lambda: current)
            assert calls[0] == ("switch", "turn_off", {"entity_id": "switch.charger"})
            assert hass.states.get("switch.charger").state == "off"
            assert "ev_smart_charging_state" not in store.data["ownership"]
            if hvac_action is None or superseded:
                assert len(calls) == 1
                assert hass.states.get("automation.climate").state == "off"
                assert "hvac_control" in store.data["ownership"]
                assert "hvac_release_hold_until" not in store.data["ownership"]
            else:
                assert calls[1:] == [("automation", "turn_on", {"entity_id": "automation.climate"})]
                assert "hvac_control" not in store.data["ownership"]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("change", "reason"), [
    ("none", None),
    ("superseded", "plan_superseded_before_dispatch"),
    ("expired", "action_outside_execution_window"),
])
@pytest.mark.parametrize("owned_safety_stop", [False, True])
def test_ev_stop_rechecks_authority_after_refusal_evidence_save(
    tmp_path, monkeypatch, change, reason, owned_safety_stop,
) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "on")
        hass.states.async_set("binary_sensor.charging", "on")
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data[CONF_EV_CHARGING] = "binary_sensor.charging"
        _arm(store, executor)
        store.data["ev_telemetry"] = {"version": 1, "vehicle_suspended": True}
        plan, context = _command("ev")
        desired = {"charging_required_now": False, "charging_reason": "ev_outside_allocated_charging_window"}
        if owned_safety_stop:
            store.data["ownership"] = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}
            desired = {"charging_required_now": False, "charging_reason": "ev_allocation_expired",
                       "ev_safety_stop": True}
        plan.actions = [replace(
            plan.actions[0], kind=ActionKind.EV_STOP if owned_safety_stop else ActionKind.EV_SCHEDULE,
            desired_state=desired,
        )]
        current = True
        clock = plan.created_at
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        saved_refusal = False
        save_telemetry = store.async_save_ev_telemetry

        async def save_then_change_authority(data: dict[str, Any]) -> None:
            nonlocal current, clock, saved_refusal
            await save_telemetry(data)
            if not saved_refusal:
                assert data["vehicle_suspended"] is False
                assert not calls
                current = change != "superseded"
                if change == "expired":
                    clock = plan.actions[0].execute_not_after + timedelta(microseconds=1)
                saved_refusal = True

        async def stop_charger(call: ServiceCall) -> None:
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("switch.charger", "off", context=call.context)
            hass.states.async_set("binary_sensor.charging", "off", context=call.context)

        monkeypatch.setattr(store, "async_save_ev_telemetry", save_then_change_authority)
        hass.services.async_register("switch", "turn_off", stop_charger)
        try:
            await executor.async_evaluate(plan, context, plan_is_current=lambda: current)
            assert saved_refusal
            if reason is not None and not owned_safety_stop:
                assert not calls
                assert hass.states.get("binary_sensor.charging").state == "on"
                assert store.data["execution_audit"][-1]["reason"] == reason
                assert store.data["action_attempts"] == []
            else:
                assert calls == [("switch", "turn_off", {"entity_id": "switch.charger"})]
                assert hass.states.get("binary_sensor.charging").state == "off"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("first_target", ["climate", "automation", "zone", None])
def test_hvac_rechecks_plan_at_first_actuator_dispatch(tmp_path, monkeypatch, first_target) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        if first_target != "automation":
            executor.entry_data.pop(CONF_CLIMATE_AUTOMATIONS)
        if first_target != "zone":
            executor.entry_data.pop(CONF_CLIMATE_ZONES)
        _arm(store, executor)
        plan, context = _command("hvac")
        current = True
        save_ownership = store.async_save_ownership
        scheduler_checks = 0
        arm_scheduler_guard = DaikinHVACAdapter._async_arm_scheduler_guard

        async def save_then_supersede(ownership):
            nonlocal current
            await save_ownership(ownership)
            if first_target == "climate" and ownership.get("hvac_control", {}).get("command_confirmation"):
                assert calls == []
                current = False

        async def arm_then_supersede(adapter, **kwargs):
            nonlocal current, scheduler_checks
            ready = await arm_scheduler_guard(adapter, **kwargs)
            scheduler_checks += 1
            if first_target in {"automation", "zone"} and scheduler_checks == 2:
                await asyncio.sleep(0)
                assert calls == []
                current = False
            return ready

        monkeypatch.setattr(store, "async_save_ownership", save_then_supersede)
        monkeypatch.setattr(DaikinHVACAdapter, "_async_arm_scheduler_guard", arm_then_supersede)
        await executor.async_evaluate(plan, context, plan_is_current=lambda: current)

        if first_target is None:
            assert calls == [("climate", "set_temperature", {"entity_id": "climate.home", "temperature": 23.0})]
            assert store.data["execution_audit"][-1]["result"] == "applied"
            return
        assert not current
        assert calls == []
        fresh_store = PlannerStore(hass, "runtime")
        await fresh_store.async_load()
        assert not fresh_store.data["ownership"]
        assert fresh_store.data["action_attempts"] == []
        assert fresh_store.data["control_pause"] == {}
        assert fresh_store.data["execution_audit"][-1]["reason"] == "plan_superseded_before_dispatch"
        assert fresh_store.data["execution_audit"][-1]["command_sent"] is False

    asyncio.run(run())


@pytest.mark.parametrize(("changed", "reason"), [
    ("expired", "hvac_action_outside_execution_window"),
    ("manual_hold", "manual_hvac_override_active"),
    ("disarmed", "production_gate_not_armed"),
])
@pytest.mark.parametrize("during_command_intent", [False, True])
@pytest.mark.parametrize("owned_baseline", [False, True])
def test_hvac_rechecks_live_authority_after_save(
    tmp_path, monkeypatch, changed, reason, during_command_intent, owned_baseline,
) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.pop(CONF_CLIMATE_AUTOMATIONS)
        executor.entry_data.pop(CONF_CLIMATE_ZONES)
        executor.entry_data[CONF_CLIMATE_MANUAL_OVERRIDE] = "input_boolean.operator_hold"
        hass.states.async_set("input_boolean.operator_hold", "off")
        _arm(store, executor)
        clock = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        plan, context = _command("hvac")
        earlier_ownership = {}
        if owned_baseline:
            hass.states.async_set("automation.climate", "off")
            earlier_ownership = {
                "climate_automations": {"automation.climate": "on"},
                "hvac_control": {
                    "main_state": {"hvac_mode": "heat", "target_temperature": 21.0},
                    "main_state_committed": True,
                },
            }
            await store.async_save_ownership(earlier_ownership)
        save_ownership = store.async_save_ownership
        changed_authority = False

        async def save_then_change_authority(ownership):
            nonlocal clock, changed_authority
            await save_ownership(ownership)
            hvac_control = ownership.get("hvac_control", {})
            if (changed_authority or not hvac_control
                    or bool(hvac_control.get("command_confirmation")) != during_command_intent):
                return
            changed_authority = True
            assert calls == []
            if changed == "expired":
                clock = plan.actions[0].execute_not_after + timedelta(microseconds=1)
            elif changed == "manual_hold":
                hass.states.async_set("input_boolean.operator_hold", "on")
            else:
                await store.async_save_production({**store.data["production"], "armed": False})

        monkeypatch.setattr(store, "async_save_ownership", save_then_change_authority)
        await executor.async_evaluate(plan, context, plan_is_current=lambda: True)

        assert changed_authority
        assert calls == []
        fresh_store = PlannerStore(hass, "runtime")
        await fresh_store.async_load()
        assert fresh_store.data["ownership"] == earlier_ownership
        assert fresh_store.data["action_attempts"] == []
        assert fresh_store.data["control_pause"] == {}
        outcome = fresh_store.data["execution_audit"][-1]
        assert outcome["result"] == "rejected"
        assert outcome["reason"] == reason
        assert outcome["command_sent"] is False

    asyncio.run(run())


@pytest.mark.parametrize("revert_options_at", ["queue", "dispatch", None])
def test_hvac_plan_honors_current_options_when_entry_reverts_before_listener(
    tmp_path, monkeypatch, revert_options_at,
) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        entry = ConfigEntry(
            domain=DOMAIN, title="HVAC option authority", source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
            data={CONF_DAIKIN_CLIMATE: "climate.home"},
            options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False,
                     CONF_CLIMATE_CONTROL_ENABLED: False},
        )
        hass.config_entries._entries[entry.entry_id] = entry
        store = PlannerStore(hass, entry.entry_id)
        owner = EnergyPlannerCoordinator(hass, entry, store)
        last_handled = dict(entry.options)
        enabled = {**last_handled, CONF_CLIMATE_CONTROL_ENABLED: True}
        hass.config_entries.async_update_entry(entry, options=enabled)
        _arm(store, owner.executor)
        plan, context = _command("hvac")
        generation = owner._refresh_generation
        save_ownership = store.async_save_ownership
        refreshed = []
        monkeypatch.setattr(
            owner, "_schedule_debounced_refresh", lambda trigger, **kwargs: refreshed.append((trigger, kwargs)),
        )

        def revert_options() -> None:
            assert not calls
            hass.config_entries.async_update_entry(entry, options=last_handled)
            assert owner._refresh_generation == generation
            assert entry.options == owner._last_handled_options

        async def save_then_revert(ownership):
            await save_ownership(ownership)
            if ownership.get("hvac_control", {}).get("command_confirmation"):
                revert_options()

        if revert_options_at == "dispatch":
            monkeypatch.setattr(store, "async_save_ownership", save_then_revert)
        try:
            async with owner._command_lock:
                owner.data = await owner._async_commit_plan_if_current(
                    generation, plan, context, owner.planner_options, execute=False, entry_options=enabled,
                )
                assert owner.data is plan
                owner._pending_plan_execution = owner._deferred_plan_execution
                drain = asyncio.create_task(owner._async_drain_plan_execution())
                await asyncio.sleep(0)
                assert not drain.done()
                if revert_options_at == "queue":
                    revert_options()
            await drain

            if revert_options_at is None:
                assert calls == [("climate", "set_temperature", {"entity_id": "climate.home", "temperature": 23.0})]
                assert store.data["execution_audit"][-1]["result"] == "applied"
                assert not refreshed
                return

            assert calls == []
            assert refreshed == [("execution_inputs_changed", {"force": True})]
            assert not store.data["ownership"]
            assert not store.data["action_attempts"]
            assert not store.data["control_pause"]
            if revert_options_at == "dispatch":
                assert store.data["execution_audit"][-1]["reason"] == "plan_superseded_before_dispatch"
                assert store.data["execution_audit"][-1]["command_sent"] is False
            else:
                assert not store.data["execution_audit"]
        finally:
            await owner.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["mapping", "tariff", "own_feedback", "polled_feedback", "no_timer", "invalid_limit", "expired",
               "cancelled"],
)
def test_auto_ev_rechecks_plan_after_durable_preparation(tmp_path, monkeypatch, change) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        mapped_limit = change in {"own_feedback", "polled_feedback", "no_timer", "invalid_limit", "cancelled"}
        start_sent, allow_start_to_complete = asyncio.Event(), asyncio.Event()
        hass.states.async_set("switch.replacement_charger", "off")
        hass.states.async_set("sensor.tariff", "0.10", {"forecast": [0.10, 0.20]})
        data = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected",
                CONF_AMBER_IMPORT_PRICE: "sensor.tariff"}
        options = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "ev_control_enabled": True,
                   "ev_charge_rate_kw": 7.0, "grid_import_limit_kw": 10.0}
        if mapped_limit:
            data.update({
                CONF_EV_CHARGING: "binary_sensor.charging", "ev_power_limit_entity": "number.limit",
                "ev_power_entity": "sensor.ev_power",
            })
            options.update(ev_limit_min=1, ev_limit_max=6)
            attributes = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
            hass.states.async_set("binary_sensor.charging", "off")
            hass.states.async_set("number.limit", 5, attributes)
            hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})

            async def set_limit(call):
                calls.append((call.domain, call.service, dict(call.data)))
                feedback_context = None if change == "polled_feedback" else call.context
                hass.states.async_set("number.limit", call.data["value"], attributes, context=feedback_context)
                await asyncio.sleep(0)

            async def start_charging(call):
                calls.append((call.domain, call.service, dict(call.data)))
                hass.states.async_set("switch.charger", "on", context=call.context)
                hass.states.async_set("binary_sensor.charging", "on", context=call.context)
                start_sent.set()
                if change == "cancelled":
                    await allow_start_to_complete.wait()
                await asyncio.sleep(0)

            hass.services.async_register("number", "set_value", set_limit)
            hass.services.async_register("switch", "turn_on", start_charging)
        entry = ConfigEntry(
            domain=DOMAIN, title="Automatic EV dispatch", source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
            data=data, options=options,
        )
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        hass.config_entries._entries[entry.entry_id] = entry
        store = PlannerStore(hass, entry.entry_id)
        owner = EnergyPlannerCoordinator(hass, entry, store)
        entry.runtime_data = owner
        try:
            owner.executor.entry_data = owner.entry_data
            owner.executor.options = owner.planner_options
            _arm(store, owner.executor)
            plan, context = _command("ev")
            assert context is not None
            assert plan.next_action is not None
            prior_deadline = context.created_at + timedelta(hours=1)
            candidate_deadline = context.created_at + timedelta(minutes=2)
            owner._schedule_ev_allocation_deadline(prior_deadline)
            owner.executor.ev_start_feedback_expected_until = prior_deadline
            previous_ownership, previous_reservation = {}, {"active": False}
            if change in {"invalid_limit", "expired"}:
                previous_ownership = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}
                await store.async_save_ownership(previous_ownership)
                reservation = {"load_kw": 3.0, "limit_kw": 10.0}
                owner.executor._ev_grid_reservations()[entry.entry_id] = reservation
                await owner.executor.async_persist_ev_grid_reservation()
                previous_reservation = {**reservation, "active": True}
            if change in {"own_feedback", "polled_feedback", "expired", "cancelled"}:
                plan.next_action.desired_state["optimization"] = {
                    "conservative_completion": candidate_deadline.isoformat(),
                }
            if change == "no_timer":
                owner.executor.ev_allocation_deadline_callback = None
            if change == "expired":
                monkeypatch.setattr(ev_adapter, "dt_util", SimpleNamespace(utcnow=lambda: candidate_deadline))
            exposure = {"at": (context.created_at - timedelta(minutes=5)).isoformat(), "cost_per_hour": 0.2}
            telemetry = {"version": 1, "command_exposure": exposure}
            await store.async_save_ev_telemetry(telemetry)
            manager = InputManager(hass, owner.entry_data, owner.planner_options)
            assert await manager.async_build_context() is not None
            captured_mapping = deepcopy(combined_entry_data(entry))
            tariff = hass.states.get("sensor.tariff")
            assert tariff is not None
            if mapped_limit:
                plan.next_action.desired_state["power_limit"] = {
                    "entity_id": "number.limit", "value": "nan" if change == "invalid_limit" else 6,
                    "unit": "kW", "physical_power_kw": 6,
                }
                owner.async_start_listeners()
            saved_generation = owner._refresh_generation
            refresh_requests = []
            monkeypatch.setattr(
                owner, "_schedule_debounced_refresh", lambda trigger, **_: refresh_requests.append(trigger),
            )
            flush = store.async_flush
            changed = False

            async def flush_then_change():
                nonlocal changed
                await flush()
                if changed:
                    return
                changed = True
                if change == "mapping":
                    hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_EV_CHARGER: "switch.replacement_charger"},
                    )
                elif change == "tariff":
                    tariff.attributes["forecast"][0] = 0.95
                    await store.async_save_ev_telemetry({
                        **store.data["ev_telemetry"], "vehicle_suspended": False,
                    })
                assert owner._refresh_generation == saved_generation
                assert calls == []
                if change in {"own_feedback", "polled_feedback", "no_timer"}:
                    owner._refresh_generation += 1
                    owner._export_replan_generations = (saved_generation, owner._refresh_generation)

            monkeypatch.setattr(store, "async_flush", flush_then_change)

            async def execute():
                async with owner._command_lock:
                    await owner._async_execute_plan_if_current(
                        saved_generation, plan, context, owner.planner_options,
                        mapped_entry_data=captured_mapping, tariffs_current=manager.tariff_inputs_current,
                    )

            if change == "cancelled":
                command = asyncio.create_task(execute())
                try:
                    await asyncio.wait_for(start_sent.wait(), 2)
                    assert owner._ev_allocation_deadline == prior_deadline
                    command.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await command
                    assert owner._ev_allocation_deadline == candidate_deadline
                    assert owner.executor.ev_start_limit_feedback is None
                    assert store.data["ev_grid_reservation"]["active"] is True
                    assert store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "5"
                    assert store.data["ev_telemetry"]["lease_until"] == candidate_deadline.isoformat()
                finally:
                    allow_start_to_complete.set()
                return
            await execute()
            assert changed, store.data["execution_audit"]
            if change in {"own_feedback", "polled_feedback", "no_timer"}:
                assert calls == [
                    ("number", "set_value", {"entity_id": "number.limit", "value": 6}),
                    ("switch", "turn_on", {"entity_id": "switch.charger"}),
                ]
                assert owner._refresh_generation == saved_generation + 1
                assert not refresh_requests
                assert store.data["execution_audit"][-1]["result"] == "applied"
                expected_deadline = prior_deadline if change == "no_timer" else candidate_deadline
                assert owner._ev_allocation_deadline == expected_deadline
                assert owner.executor.ev_start_feedback_expected_until is None
                return
            assert calls == []
            assert refresh_requests == (["execution_inputs_changed"] if change in {"mapping", "tariff"} else [])
            assert owner._ev_allocation_deadline == prior_deadline
            assert owner.executor.ev_start_feedback_expected_until == prior_deadline
            assert owner.executor.ev_start_limit_feedback is None
            reason = {"invalid_limit": "ev_grid_projection_stale", "expired": "ev_allocation_expired"}
            assert store.data["execution_audit"][-1]["reason"] == reason.get(change, "plan_superseded_before_dispatch")
            assert store.data["execution_audit"][-1]["command_sent"] is False
            assert store.data["action_attempts"] == []
            assert not store.data["control_pause"]
            fresh_store = PlannerStore(hass, entry.entry_id)
            await fresh_store.async_load()
            assert fresh_store.data["ownership"] == previous_ownership
            assert fresh_store.data["ev_grid_reservation"] == previous_reservation
            if change == "tariff":
                telemetry["vehicle_suspended"] = False
            assert fresh_store.data["ev_telemetry"] == telemetry
        finally:
            await owner.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("cap_feedback", ["restored", "restore_failed", "uncertain", "manual", "remapped"])
def test_expired_ev_lease_reconciles_native_cap_after_confirmed_stop(tmp_path, monkeypatch, cap_feedback):
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 7, "step": 1}
        hass.states.async_set("number.limit", 5, attributes)
        initial_charging = "off" if cap_feedback == "manual" else "on"
        hass.states.async_set("switch.charger", initial_charging)
        hass.states.async_set("binary_sensor.charging", initial_charging)
        hass.states.async_set("sensor.ev_power", 5 if initial_charging == "on" else 0, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update({
            CONF_EV_CHARGING: "binary_sensor.charging", "ev_power_limit_entity": "number.limit",
            "ev_power_entity": "sensor.ev_power",
        })
        executor.options.update(ev_limit_min=1, ev_limit_max=7)
        _arm(store, executor)
        baseline = {"ev_power_limit_entity": "4", "ev_power_limit_unit": "kW"}
        ownership = {"ev_smart_charging_state": baseline}
        if cap_feedback == "remapped":
            ownership["ev_smart_charging_control_topology"] = {"ev_power_limit_entity": "number.original_limit"}
            hass.states.async_set("number.original_limit", 7, attributes)
        await store.async_save_ownership(ownership)
        executor._ev_grid_reservations()["runtime"] = {"load_kw": 5.0, "limit_kw": 10.0}
        await executor.async_persist_ev_grid_reservation()
        plan, context = _command("ev")
        assert plan.next_action is not None and context is not None
        context.slots[0].projected_ev_load_kw = 6
        await store.async_save_ev_telemetry({
            "version": 1, "command_exposure": {"at": plan.created_at.isoformat(), "cost_per_hour": 0.20},
        })
        deadline = plan.created_at + timedelta(minutes=2)
        plan.next_action.desired_state.update(
            projected_load_kw_now=6,
            optimization={"conservative_completion": deadline.isoformat()},
            power_limit={"entity_id": "number.limit", "value": 6, "unit": "kW", "physical_power_kw": 6},
        )
        lease_changes = []
        executor.ev_allocation_deadline_callback = lease_changes.append
        clock = plan.created_at
        monkeypatch.setattr(ev_adapter, "dt_util", SimpleNamespace(utcnow=lambda: clock))

        async def set_limit(call: ServiceCall) -> None:
            nonlocal clock
            calls.append((call.domain, call.service, dict(call.data)))
            value = call.data["value"]
            if value == 6:
                if cap_feedback != "uncertain":
                    hass.states.async_set("number.limit", value, attributes, context=call.context)
                if cap_feedback != "manual":
                    clock = deadline
            elif cap_feedback == "restore_failed":
                raise HomeAssistantError("Unconfirmed cap restoration")
            else:
                hass.states.async_set("number.limit", value, attributes, context=call.context)

        async def start_charger(call: ServiceCall) -> None:
            nonlocal clock
            calls.append((call.domain, call.service, dict(call.data)))
            clock = deadline
            hass.states.async_set("switch.charger", "on", context=call.context)
            hass.states.async_set("binary_sensor.charging", "on", context=call.context)
            hass.states.async_set("number.limit", 3, attributes, context=Context(user_id="driver"))

        async def stop_charger(call: ServiceCall) -> None:
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("switch.charger", "off", context=call.context)
            hass.states.async_set("binary_sensor.charging", "off", context=call.context)
            hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"}, context=call.context)

        hass.services.async_register("number", "set_value", set_limit)
        hass.services.async_register("switch", "turn_on", start_charger)
        hass.services.async_register("switch", "turn_off", stop_charger)
        try:
            await executor.async_evaluate(plan, context, plan_is_current=lambda: True)

            assert store.data["execution_audit"][-1]["reason"] == "ev_allocation_expired"
            assert hass.states.get("binary_sensor.charging").state == "off"
            assert "runtime" not in executor._ev_grid_reservations()
            assert lease_changes == [None]
            latest = PlannerStore(hass, "runtime")
            await latest.async_load()
            assert latest.data["ev_grid_reservation"] == {"active": False}
            assert "command_exposure" not in latest.data["ev_telemetry"]
            if cap_feedback in {"restored", "restore_failed", "remapped"}:
                assert [service for _, service, _ in calls] == ["set_value", "turn_off", "set_value"]
            elif cap_feedback == "uncertain":
                assert [service for _, service, _ in calls] == ["set_value", "turn_off"]
            else:
                assert [service for _, service, _ in calls] == ["set_value", "turn_on", "turn_off"]
            unresolved = cap_feedback in {"restore_failed", "uncertain", "remapped"}
            assert latest.data["ownership"] == (ownership if unresolved else {})
            expected_cap = {"restored": 4, "restore_failed": 6, "uncertain": 5, "manual": 3, "remapped": 5}
            assert float(hass.states.get("number.limit").state) == expected_cap[cap_feedback]
            if cap_feedback == "remapped":
                assert hass.states.get("number.original_limit").state == "7"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("area", ["ev", "hvac", "enphase"])
def test_real_disk_failure_blocks_device_dispatch_and_remains_retryable(tmp_path, monkeypatch, area) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        _arm(store, executor)
        try:
            def disk_failure(*args, **kwargs):
                raise ha_storage.WriteError("injected write failure")

            with monkeypatch.context() as patch:
                patch.setattr(ha_storage, "write_utf8_file_atomic", disk_failure)
                with pytest.raises(HomeAssistantError):
                    await executor.async_evaluate(*_command(area))
            assert calls == []
            assert store._is_dirty
            await store.async_flush()
            assert not store._is_dirty
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data == store.data
            assert fresh.data["ev_grid_reservation"] if area == "ev" else fresh.data["ownership"]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("already_owned", [False, True])
def test_profile_remap_during_ownership_flush_blocks_unsent_command(tmp_path, monkeypatch, already_owned) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        _arm(store, executor)
        previous = {"unrelated": "preserved"}
        if already_owned:
            previous.update(enphase_profile="Saved baseline", enphase_profile_changed_at="2026-01-01T00:00:00Z")
        await store.async_save_ownership(previous)
        mapped_entry_data = dict(executor.entry_data)
        live_entry_data = dict(mapped_entry_data)
        original_flush = store.async_flush

        async def remap_during_flush() -> None:
            await original_flush()
            if store.data["ownership"] != previous:
                live_entry_data[CONF_ENPHASE_PROFILE] = "select.replacement_profile"

        batch_open, finish_batch = asyncio.Event(), asyncio.Event()

        async def keep_store_batch_open() -> None:
            async with store.async_delay_save():
                batch_open.set()
                await finish_batch.wait()

        monkeypatch.setattr(store, "async_flush", remap_during_flush)
        batch = asyncio.create_task(keep_store_batch_open())
        try:
            await batch_open.wait()
            await executor.async_evaluate(
                *_command("enphase"), plan_is_current=lambda: live_entry_data == mapped_entry_data,
            )

            assert live_entry_data != mapped_entry_data
            assert calls == []
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["ownership"] == previous
            assert not store.data.get("command_rate_limits")
            assert not store.data.get("action_attempts")
            assert not store.data.get("control_pause")
            outcome = store.data["execution_audit"][-1]
            assert outcome["result"] == "rejected"
            assert outcome["reason"] == "plan_superseded_before_dispatch"
            assert outcome["command_sent"] is False
        finally:
            finish_batch.set()
            await batch
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("area", ["ev", "enphase"])
@pytest.mark.parametrize("intervention", ["disarm", "pause", "window_expired"])
def test_first_device_write_rechecks_production_authority_after_save(tmp_path, monkeypatch, area, intervention) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        _arm(store, executor)
        original_ownership = {"unrelated": "preserved"}
        await store.async_save_ownership(original_ownership)
        original_flush = store.async_flush
        pause = {"active": True, "assets": [area], "reason": "operator_pause"}
        intervened = False
        clock = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        plan, context = _command(area)

        async def flush_then_revoke_authority() -> None:
            nonlocal clock, intervened
            await original_flush()
            if intervened or store.data["ownership"] == original_ownership:
                return
            intervened = True
            assert not calls
            if intervention == "disarm":
                await store.async_save_production({**store.data["production"], "armed": False})
            elif intervention == "pause":
                await store.async_save_control_pause(pause)
            else:
                clock = plan.actions[0].execute_not_after + timedelta(microseconds=1)

        monkeypatch.setattr(store, "async_flush", flush_then_revoke_authority)
        try:
            await executor.async_evaluate(plan, context, plan_is_current=lambda: True)
            assert intervened
            assert calls == []
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["ownership"] == original_ownership
            assert not fresh.data["ev_grid_reservation"].get("active")
            assert not fresh.data["command_rate_limits"]
            assert fresh.data["action_attempts"] == []
            assert fresh.data["control_pause"] == (pause if intervention == "pause" else {})
            outcome = fresh.data["execution_audit"][-1]
            assert outcome["result"] == "rejected"
            assert outcome["command_sent"] is False
            assert outcome["reason"] == {
                "disarm": "production_gate_not_armed",
                "pause": f"{area}_control_paused",
                "window_expired": "action_outside_execution_window",
            }[intervention]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "fixture_path",
    sorted((Path(__file__).parent / "fixtures" / "decision_replay").glob("*.json")),
    ids=lambda path: path.stem,
)
def test_observation_sequences_regenerate_plans_and_execute_commands(tmp_path, monkeypatch, fixture_path) -> None:
    """Fixtures contain observations/expectations, never preconstructed plans."""
    fixture = json.loads(fixture_path.read_text())

    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        leases = []
        executor.ev_allocation_deadline_callback = leases.append
        executor.options.update({
            "ev_charge_rate_kw": 6.0, "ev_soc_per_kwh": 10.0, "ev_continuous_charging": True,
            "ev_earliest_start": "None", "ev_low_price_charging_enabled": False,
            "command_rate_limit_seconds": 0, "max_daily_ev_actions": 50,
        })
        executor.options.update(fixture.get("options", {}))
        _arm(store, executor)
        try:
            for index, step in enumerate(fixture["steps"]):
                now = datetime.fromisoformat(step["at"])
                monkeypatch.setattr(dt_util, "utcnow", lambda instant=now: instant)
                context = DecisionContext(
                    created_at=now, plan_id=f"replay-{index}",
                    slots=[DecisionSlot(now + timedelta(minutes=5 * i), price, 0.05, 0, 1)
                           for i, price in enumerate(step["prices"])],
                    current_battery_soc_percent=50, current_ev_soc_percent=step["soc"],
                    occupancy_state=OccupancyState.OCCUPIED,
                    input_health=InputHealth(step.get("health", "healthy")),
                    ev_connected=True, ev_charging=hass.states.get("switch.charger").state == "on",
                    ev_target_soc_percent=fixture["target_soc"], ev_ready_by=fixture["ready_by"],
                    local_timezone=fixture["timezone"],
                    active_overrides=[
                        Override("manual_ev_charging", "service", now + timedelta(minutes=5), "manual_stop")
                    ]
                    if step.get("manual_stop") else [],
                )
                for slot, load in zip(context.slots, step.get("loads", []), strict=False):
                    slot.baseline_load_forecast_upper_kw = load
                plan = DryRunPlanner(executor.options).create_plan(context)
                actions = [action for action in plan.actions if action.asset == ActionAsset.EV]
                allocated = [slot for action in actions for slot in action.desired_state.get("allocated_slots", [])]
                offsets = [int((datetime.fromisoformat(slot["valid_at"]) - now).total_seconds() / 60)
                           for slot in allocated]
                assert offsets == step["offsets"], (fixture["name"], index)
                if "expected_evidence" in step:
                    evidence = actions[0].desired_state["optimization"]
                    for key, value in step["expected_evidence"].items():
                        assert evidence[key] == value
                if "deadline" in step:
                    assert actions[0].desired_state["ready_by_utc"] == step["deadline"]
                    assert all(datetime.fromisoformat(slot["valid_at"]) < datetime.fromisoformat(step["deadline"])
                               for slot in allocated)
                await store.async_save_plan(plan)
                before = len(calls)
                await executor.async_evaluate(plan, context)
                assert [service for _domain, service, _data in calls[before:]] == step["commands"], index
                assert (hass.states.get("switch.charger").state == "on") is step["charging"], index
                if not step["charging"]:
                    assert not store.data["ownership"].get("ev_smart_charging_state"), index
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["active_plan"]["plan_id"] == f"replay-{len(fixture['steps']) - 1}"
            assert fresh.data["execution_audit"]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["disk", "shutdown"])
@pytest.mark.parametrize("restore_fails", [False, True])
def test_operator_disarm_restores_hvac_before_failed_persistence(
    tmp_path, monkeypatch, failure, restore_fails,
) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        entry = ConfigEntry(
            domain=DOMAIN, title="Disarm persistence", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        updates = []
        coordinator.async_add_listener(lambda: updates.append(store.data["production"]["armed"]))
        try:
            await coordinator.executor.async_evaluate(*_command("hvac"))
            assert hass.states.get("automation.climate").state == "off"
            assert hass.states.get("switch.zone").state == "on"
            before = len(calls)
            durable_ownership = store.data["ownership"]
            real_dispatch = adapter_helpers.async_call_device_service

            async def restore(hass, domain, service, data, **kwargs):
                assert store.data["production"]["armed"] is False
                if restore_fails and data["entity_id"] == "automation.climate":
                    raise RuntimeError("injected restoration failure")
                return await real_dispatch(hass, domain, service, data, **kwargs)

            def disk_failure(*args, **kwargs):
                raise ha_storage.WriteError("injected write failure")

            with monkeypatch.context() as patch:
                patch.setattr(
                    "custom_components.ha_energy_planner.hvac_adapter.async_call_device_service", restore,
                )
                patch.setattr(ha_storage, "write_utf8_file_atomic", disk_failure)
                if failure == "shutdown":
                    hass.state = CoreState.stopping
                with pytest.raises(HomeAssistantError):
                    await coordinator.async_operator_disarm_production_control()
            hass.state = CoreState.running
            assert calls[before:] == [
                ("climate", "set_temperature", {"entity_id": "climate.home", "temperature": 20.0}),
                ("switch", "turn_off", {"entity_id": "switch.zone"}),
                *([] if restore_fails else [("automation", "turn_on", {"entity_id": "automation.climate"})]),
            ]
            assert hass.states.get("switch.zone").state == "off"
            assert hass.states.get("automation.climate").state == ("off" if restore_fails else "on")
            assert store.data["production"]["armed"] is False
            assert updates == [False]
            assert store._is_dirty
            assert bool(store.data["ownership"].get("climate_automations")) is restore_fails
            assert store.data["execution_audit"][-1]["result"] == ("failed" if restore_fails else "restored")
            # A failed save must retain the last durable recovery evidence.
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["ownership"] == durable_ownership
            # Once storage recovers, retry the remaining restoration and save.
            await coordinator.async_operator_disarm_production_control()
            assert hass.states.get("automation.climate").state == "on"
            assert not store._is_dirty
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["production"]["armed"] is False
            assert fresh.data["ownership"] == {}
        finally:
            hass.state = CoreState.running
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_serialization_failure_preserves_last_durable_ownership(tmp_path) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        try:
            baseline = {"enphase_profile": "Original profile"}
            await store.async_save_ownership(baseline)
            with pytest.raises(HomeAssistantError):
                await store.async_save_ownership({**baseline, "invalid": object()})
            assert store._is_dirty
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["ownership"] == baseline
            await store.async_save_ownership(baseline)
            assert not store._is_dirty
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_real_store_deferred_shutdown_write_is_flushed_before_acknowledgement(tmp_path) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        try:
            hass.state = CoreState.stopping
            await store.async_save_ownership({"enphase_profile": "Original profile"})
            assert not store._is_dirty
            # Verify actual disk contents before Core final_write can run.
            persisted = json.loads(Path(store._store.path).read_text())
            assert persisted["data"]["ownership"] == {"enphase_profile": "Original profile"}
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["ownership"] == {"enphase_profile": "Original profile"}
        finally:
            hass.state = CoreState.running
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("area", ["ev", "hvac", "enphase"])
def test_interrupted_command_recovers_from_disk_in_a_fresh_runtime(tmp_path: Path, monkeypatch, area) -> None:
    async def run() -> None:
        first_hass = HomeAssistant(str(tmp_path))
        _devices(first_hass)
        store = PlannerStore(first_hass, "runtime")
        executor = _executor(first_hass, store)
        _arm(store, executor)
        target = {"ev": "switch.charger", "hvac": "climate.home", "enphase": "select.profile"}[area]
        async def accepted_then_interrupted(hass, domain, service, data, **kwargs):
            await adapter_helpers.async_call_device_service(hass, domain, service, data, **kwargs)
            if data.get("entity_id") == target:
                raise asyncio.CancelledError

        try:
            module = {"ev": "ev_adapter", "hvac": "hvac_adapter", "enphase": "enphase_adapter"}[area]
            with monkeypatch.context() as patch:
                patch.setattr(
                    f"custom_components.ha_energy_planner.{module}.async_call_device_service",
                    accepted_then_interrupted,
                )
                with pytest.raises(asyncio.CancelledError):
                    await executor.async_evaluate(*_command(area))
            assert store.data["ownership"]
            observed = [(s.entity_id, s.state, dict(s.attributes)) for s in first_hass.states.async_all()]
        finally:
            await first_hass.async_stop(force=True)

        # No prior executor, reservations, Store cache or HA state machine is reused.
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        for entity_id, state, attrs in observed:
            hass.states.async_set(entity_id, state, attrs)
        fresh = PlannerStore(hass, "runtime")
        try:
            await fresh.async_load()
            assert fresh.data["ownership"]
            recovered = _executor(hass, fresh)
            _rehydrate_ev_grid_reservation(
                hass, SimpleNamespace(entry_id="runtime", options=recovered.options), fresh.data
            )
            asset = "daikin" if area == "hvac" else area
            result = await recovered.async_restore_device_control(asset, "restart_recovery")
            assert result.result == OutcomeResult.RESTORED
            assert calls
            assert hass.states.get("select.profile").state == "Original profile"
            assert hass.states.get("switch.charger").state == "off"
            assert hass.states.get("climate.home").attributes["temperature"] == 20.0
            assert hass.states.get("automation.climate").state == "on"
            assert hass.states.get("switch.zone").state == "off"
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            assert reloaded.data["ownership"] == {}
            before = list(calls)
            await recovered.async_restore_device_control(asset, "repeat_recovery")
            assert calls == before
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_number_control_transaction_reserves_then_confirms_reduction_and_restores(tmp_path) -> None:
    """Real HA services and disk state cover the complete number-control transaction."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
        hass.states.async_set("number.limit", 6, attrs)
        hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update({"ev_power_limit_entity": "number.limit", "ev_power_entity": "sensor.ev_power"})
        executor.options.update({"ev_limit_min": 1, "ev_limit_max": 6, "command_rate_limit_seconds": 0})
        _arm(store, executor)

        async def limit(call):
            # The original limit and conservative reservation must be durable
            # before a service is allowed to mutate the charger.
            assert store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "6"
            if call.data["value"] == 3:
                assert store.data["ev_grid_reservation"]["load_kw"] >= 3
            calls.append(("number", "set_value", dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], attrs)
            hass.states.async_set("sensor.ev_power", call.data["value"], {"unit_of_measurement": "kW"})

        hass.services.async_register("number", "set_value", limit)
        plan, context = _command("ev")
        plan.actions[0].desired_state.update({"projected_load_kw_now": 3, "power_limit": {
            "entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3}})
        context.slots[0].projected_ev_load_kw = 3
        try:
            await executor.async_evaluate(plan, context)
            assert [service for _, service, _ in calls] == ["set_value", "turn_on"]
            assert not await executor._async_save_provisional_ev_ownership(plan.actions[0], executor.entry_data)
            assert store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "6"
            assert store.data["ev_grid_reservation"]["load_kw"] == 3
            assert store.data["ev_grid_reservation"]["observed_load_kw"] == 3
            executor.sync_ev_grid_reservation()
            assert store.data["ev_grid_reservation"]["load_kw"] == 3
            assert executor._rate_limit_reason(plan.actions[0], dt_util.utcnow()) == "device_command_rate_limited"
            hass.states.async_set("number.limit", 4, attrs)
            assert executor._observed_conflict_reason(
                plan.actions[0], dt_util.utcnow()) == "external_ev_power_limit_conflict"
            store.data["execution_audit"][-1]["attempted_at"] = (dt_util.utcnow()-timedelta(hours=1)).isoformat()
            assert executor._observed_conflict_reason(
                plan.actions[0], dt_util.utcnow()) == "external_ev_power_limit_conflict"
            hass.states.async_set("number.limit", 3, attrs)
            plan.actions[0].kind = ActionKind.EV_SCHEDULE
            plan.plan_id = plan.actions[0].plan_id = "stop-plan"
            plan.actions[0].action_id = "stop-command"
            plan.actions[0].desired_state = {"charging_required_now": False}
            store.data["ev_telemetry"] = {"version": 1, "command_exposure": {
                "at": dt_util.utcnow().isoformat(), "cost_per_hour": 1}}
            await executor.async_evaluate(plan, context)
            assert calls[-2][1:] == ("turn_off", {"entity_id": "switch.charger"})
            assert calls[-1][1] == "set_value"
            assert hass.states.get("number.limit").state in {"6", "6.0"}
            assert not store.data["ev_grid_reservation"]["active"]
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize(("rollback", "lease_offset", "feedback"), [
    ("cap_only", None, None), ("cap_only", None, "provisional_cap"),
    ("cap_only", None, "measured_draw"), ("cap_only", None, "manual_cap"),
    ("cap_only", None, "remapped_cap"),
    ("stopped", None, None), ("unrestored_cap", None, None),
    ("unconfirmed_stop", 1, None), ("unconfirmed_stop", -1, None),
])
def test_superseded_automatic_ev_cap_rollback(tmp_path, rollback, lease_offset, feedback):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "on")
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1}
        previous_cap = 5 if rollback == "cap_only" else 8
        hass.states.async_set("number.limit", previous_cap, attrs)
        hass.states.async_set("sensor.ev_power", 5, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(
            ev_limit_min=1, ev_limit_max=9, command_rate_limit_seconds=0,
            ev_confirmation_timeout_seconds=0, ev_confirmation_retries=0,
        )
        plan, context = _command("ev")
        assert context is not None
        plan.actions[0].desired_state["power_limit"] = {
            "entity_id": "number.limit", "value": 7, "unit": "kW", "physical_power_kw": 7,
        }
        prior_deadline = context.created_at + timedelta(minutes=2)
        deadlines = [prior_deadline]
        executor.ev_allocation_deadline_callback = deadlines.append
        executor.current_ev_allocation_deadline = lambda: deadlines[-1]
        if lease_offset is not None:
            executor.entry_data[CONF_EV_CHARGING] = "binary_sensor.charging"
            hass.states.async_set("binary_sensor.charging", "on")
            retry_deadline = prior_deadline + timedelta(minutes=lease_offset)
            plan.actions[0].desired_state["optimization"] = {
                "conservative_completion": retry_deadline.isoformat(),
            }
        _arm(store, executor)
        exposure = {"at": context.created_at.isoformat(), "cost_per_hour": 2}
        ownership = {"ev_smart_charging_state": {
            CONF_EV_CHARGER: "off", "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }}
        reservation = {"load_kw": previous_cap, "limit_kw": 10, "reserved_at": context.created_at.isoformat()}
        if rollback == "cap_only":
            reservation.update(confirmed_power_limit=True, confirmed_power_at=reservation["reserved_at"])
        executor._ev_grid_reservations()["runtime"] = reservation
        current = True

        async def write_limit(call):
            nonlocal current
            calls.append((call.domain, call.service, dict(call.data)))
            if rollback == "unrestored_cap" and call.data["value"] == 9:
                return
            hass.states.async_set("number.limit", call.data["value"], attrs, context=call.context)
            if call.data["value"] == 7 and feedback is not None:
                watts = 6000 if feedback == "measured_draw" else 4500
                hass.states.async_set("sensor.ev_power", watts, {"unit_of_measurement": "W"})
                executor.sync_ev_grid_reservation(power_state=hass.states.get("sensor.ev_power"))
            current = False

        def plan_is_current():
            if not current and feedback == "manual_cap" and float(hass.states.get("number.limit").state) == 7:
                hass.states.async_set("number.limit", 6, attrs)
            if not current and feedback == "remapped_cap":
                hass.states.async_set("number.replacement", 9, attrs)
                executor.entry_data = {**executor.entry_data, "ev_power_limit_entity": "number.replacement"}
            return current

        hass.services.async_register("number", "set_value", write_limit)
        try:
            await store.async_save_ownership(ownership)
            await store.async_save_ev_telemetry({
                "version": 1, "command_exposure": exposure, "lease_until": prior_deadline.isoformat(),
            })
            await executor.async_evaluate(plan, context, plan_is_current=plan_is_current)
            services = [service for _, service, _ in calls]
            assert "turn_on" not in services
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            if rollback == "cap_only":
                expected_writes = [7] if feedback == "manual_cap" else [7, previous_cap]
                assert [data["value"] for _, _, data in calls] == expected_writes
                assert services == ["set_value"] * len(calls)
                assert hass.states.get("switch.charger").state == "on"
                assert float(hass.states.get("number.limit").state) == (6 if feedback == "manual_cap" else previous_cap)
                assert deadlines == [prior_deadline]
                assert reloaded.data["ownership"] == ownership
                new_reservation = reloaded.data["ev_grid_reservation"]
                expected_load = 6 if feedback in {"measured_draw", "manual_cap"} else previous_cap
                assert new_reservation["load_kw"] == expected_load
                assert new_reservation["reserved_at"] == reservation["reserved_at"]
                assert new_reservation["confirmed_power_at"] == reservation["confirmed_power_at"]
                if feedback == "measured_draw":
                    assert new_reservation["observed_load_kw"] == 6
                elif feedback == "manual_cap":
                    assert new_reservation["observed_limit_kw"] == 6
                else:
                    assert new_reservation["confirmed_power_limit"] is True
                assert reloaded.data["ev_telemetry"]["command_exposure"] == exposure
                assert reloaded.data["ev_telemetry"]["lease_until"] == prior_deadline.isoformat()
            elif rollback == "unconfirmed_stop":
                assert "turn_off" in services
                assert services.count("set_value") == 1
                assert deadlines == ([prior_deadline] if lease_offset > 0 else [prior_deadline, retry_deadline])
                assert reloaded.data["ownership"] == ownership
                assert reloaded.data["ev_grid_reservation"]["load_kw"] == previous_cap
            else:
                assert services == ["set_value", "turn_off", "set_value"]
                assert hass.states.get("switch.charger").state == "off"
                assert calls[-1][2]["value"] == 9
                assert deadlines == [prior_deadline, None]
                assert reloaded.data["ownership"] == (ownership if rollback == "unrestored_cap" else {})
                assert reloaded.data["ev_grid_reservation"] == {"active": False}
                assert "command_exposure" not in reloaded.data["ev_telemetry"]
            assert reloaded.data["execution_audit"][-1]["result"] == "failed"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("intervention", ["save", "cap", "previous_cap_confirmation", "pause_during_cap", None])
def test_automatic_ev_rechecks_shared_headroom_at_dispatch(tmp_path, monkeypatch, intervention):
    """Headroom and production authority can change while a start prepares."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        power_attrs = {"unit_of_measurement": "kW"}
        cap_attrs = {**power_attrs, "min": 1, "max": 9, "step": 1}
        hass.states.async_set("number.limit", 6, cap_attrs)
        hass.states.async_set("sensor.ev_power", 0, power_attrs)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_limit_min=1, ev_limit_max=9, command_rate_limit_seconds=0)
        _arm(store, executor)
        plan, context = _command("ev")
        assert context is not None
        action = plan.actions[0]
        action.desired_state["power_limit"] = {
            "entity_id": "number.limit", "value": 7, "unit": "kW", "physical_power_kw": 7,
        }
        prior_deadline = context.created_at + timedelta(minutes=2)
        deadlines = [prior_deadline]
        executor.current_ev_allocation_deadline = lambda: deadlines[-1]
        executor.ev_allocation_deadline_callback = deadlines.append
        reservations = executor._ev_grid_reservations()
        assert reservations is not None
        pause = {"active": True, "assets": ["ev"], "reason": "operator_pause"}
        original_reservation = {"load_kw": 6, "limit_kw": 10, "reserved_at": context.created_at.isoformat()}
        if intervention == "previous_cap_confirmation":
            context.slots[0].projected_ev_load_kw = 6
            original_reservation["pending_power_limit"] = {
                "at": context.created_at.isoformat(), "version": 1, "entity_id": "number.limit",
                "unit": "kW", "value": 6, "physical_power_kw": 6,
            }
        reservations["runtime"] = original_reservation
        other_entry_id = "second_ev"
        reservations[other_entry_id] = {
            "load_kw": 2, "limit_kw": 10, "reserved_at": context.created_at.isoformat(),
        }
        other = Executor(
            PlannerStore(hass, other_entry_id), hass=hass, entry_id=other_entry_id,
            entry_data={"ev_power_entity": "sensor.second_ev_power"},
            options={"ev_charge_rate_kw": 2, "grid_import_limit_kw": 10},
        )
        original_ownership = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off", "ev_power_limit_entity": "6"}}
        await store.async_save_ownership(original_ownership)

        def observe_other_load(kw: int = 4) -> None:
            hass.states.async_set("sensor.second_ev_power", kw, power_attrs)
            assert other.sync_ev_grid_reservation()
            assert reservations[other_entry_id]["load_kw"] == kw

        flushed = False
        flush = store.async_flush

        async def flush_then_observe() -> None:
            nonlocal flushed
            await flush()
            if intervention in {"save", "previous_cap_confirmation"} and not flushed:
                assert reservations["runtime"]["load_kw"] == 7
                assert not calls
                flushed = True
                if intervention == "previous_cap_confirmation":
                    observe_other_load(3)
                    hass.states.async_set("sensor.ev_power", 6, power_attrs)
                    executor.sync_ev_grid_reservation()
                    assert reservations["runtime"]["load_kw"] == 6
                else:
                    observe_other_load()

        async def write_limit(call: ServiceCall) -> None:
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], cap_attrs, context=call.context)
            if call.data["value"] == 7:
                if intervention == "cap":
                    observe_other_load()
                elif intervention == "pause_during_cap":
                    await store.async_save_control_pause(pause)

        monkeypatch.setattr(store, "async_flush", flush_then_observe)
        hass.services.async_register("number", "set_value", write_limit)
        try:
            await executor.async_evaluate(plan, context, plan_is_current=lambda: True)
            if intervention is None:
                assert [service for _, service, _ in calls] == ["set_value", "turn_on"], store.data["execution_audit"]
                return
            assert hass.states.get("switch.charger").state == "off"
            assert [data["value"] for _, service, data in calls if service == "set_value"] == (
                [7, 6] if intervention in {"cap", "pause_during_cap"} else []
            )
            assert deadlines == [prior_deadline]
            assert reservations["runtime"] == original_reservation
            expected_other_load = {"previous_cap_confirmation": 3, "pause_during_cap": 2}.get(intervention, 4)
            assert reservations[other_entry_id]["load_kw"] == expected_other_load
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            assert reloaded.data["ownership"] == original_ownership
            assert reloaded.data["ev_grid_reservation"] == {**original_reservation, "active": True}
            outcome = reloaded.data["execution_audit"][-1]
            if intervention == "pause_during_cap":
                assert outcome["reason"] == "ev_control_paused"
                assert outcome["command_sent"] is True
                assert reloaded.data["control_pause"] == pause
            else:
                assert outcome["reason"] == "ev_grid_projection_stale"
            if intervention in {"save", "previous_cap_confirmation"}:
                assert not reloaded.data["action_attempts"]
                assert not reloaded.data["control_pause"]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
def test_automatic_ev_reverts_unsent_claim_when_provisional_flush_is_interrupted(
    tmp_path, monkeypatch, owned, failure,
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        _arm(store, executor)
        plan, context = _command("ev")
        reservations = executor._ev_grid_reservations()
        ownership = {"unrelated": "preserved"}
        previous_spending = {
            "version": 1,
            "emergency_spend": 0.2,
            "command_exposure": {"at": (plan.created_at - timedelta(minutes=1)).isoformat(), "cost_per_hour": 1},
        }
        if owned:
            ownership["ev_smart_charging_state"] = {CONF_EV_CHARGER: "off"}
            reservations["runtime"] = {"load_kw": 3, "limit_kw": 10}
        await store.async_save_ownership(ownership)
        await store.async_save_ev_telemetry(previous_spending)
        flush = store.async_flush
        interrupted = False

        async def lose_acknowledgement():
            nonlocal interrupted
            await flush()
            if not interrupted:
                interrupted = True
                assert not calls
                assert store.data["ev_grid_reservation"]["load_kw"] == 7
                raise failure("provisional write acknowledgement lost")

        monkeypatch.setattr(store, "async_flush", lose_acknowledgement)
        try:
            with pytest.raises(failure, match="provisional write acknowledgement lost"):
                await executor.async_evaluate(plan, context, plan_is_current=lambda: True)
            assert interrupted and not calls
            assert executor._provisional_ev_capacity is None
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            assert reloaded.data["ownership"] == ownership
            assert reloaded.data["ev_telemetry"] == previous_spending
            assert reloaded.data["action_attempts"] == []
            assert reloaded.data["ev_grid_reservation"] == (
                {"active": True, "load_kw": 3, "limit_kw": 10} if owned else {"active": False}
            )
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("interleave", ["notification", "command_ledger"])
def test_new_power_after_confirmation_survives_execution_bookkeeping(tmp_path, monkeypatch, interleave):
    """Post-confirmation device evidence must survive asynchronous bookkeeping."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
        power_attrs = {"unit_of_measurement": "kW"}
        hass.states.async_set("number.limit", 6, attrs)
        hass.states.async_set("sensor.ev_power", 6, power_attrs)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update({"ev_power_limit_entity": "number.limit", "ev_power_entity": "sensor.ev_power"})
        executor.options.update({"ev_limit_min": 1, "ev_limit_max": 6, "command_rate_limit_seconds": 0})
        _arm(store, executor)

        async def limit(call):
            hass.states.async_set("number.limit", call.data["value"], attrs)
            hass.states.async_set("sensor.ev_power", call.data["value"], power_attrs)

        method = ("_async_dismiss_plan_fallback_notification" if interleave == "notification"
                  else "_async_record_command_attempt")
        original = getattr(executor, method)
        observations = []

        async def bookkeeping(*args):
            result = await original(*args)
            if not observations and hass.states.get("switch.charger").state == "on":
                hass.states.async_set("sensor.ev_power", 9, power_attrs)
                executor.sync_ev_grid_reservation()
                observations.append(executor._ev_grid_reservations()["runtime"]["load_kw"])
                hass.states.async_set("sensor.ev_power", "unavailable", power_attrs)
                executor.sync_ev_grid_reservation()
            return result

        monkeypatch.setattr(executor, method, bookkeeping)
        hass.services.async_register("number", "set_value", limit)
        plan, context = _command("ev")
        plan.actions[0].desired_state.update({"projected_load_kw_now": 3, "power_limit": {
            "entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3}})
        context.slots[0].projected_ev_load_kw = 3
        try:
            await executor.async_evaluate(plan, context)
            assert observations == [9]
            reservation = store.data["ev_grid_reservation"]
            assert reservation["load_kw"] == reservation["observed_load_kw"] == 9
            assert not reservation.get("confirmed_power_limit")
            context.slots[0].baseline_load_forecast_kw = 2
            assert executor._ev_reservation_safety_issue(context) == "grid_import_limit_exceeded"
            await store.async_flush()
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            assert reloaded.data["ev_grid_reservation"]["load_kw"] == 9
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "no_timer", "budget", "price"])
def test_premium_commands_require_durable_spending_and_timer(tmp_path, monkeypatch, failure) -> None:
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.options.update({"ev_price_limit_enabled": True, "ev_max_import_price": .2,
                                "ev_price_policy": "departure_priority", "ev_emergency_price": .6,
                                "ev_emergency_budget": .3, "command_rate_limit_seconds": 0})
        leases = []
        if failure != "no_timer":
            executor.ev_allocation_deadline_callback = leases.append
        if failure == "budget":
            executor.options["ev_emergency_budget"] = .001
        _arm(store, executor)
        plan, context = _command("ev")
        context.slots[0].import_price = .7 if failure == "price" else .4
        try:
            await executor.async_evaluate(plan, context)
            if failure:
                assert not calls
                assert store.data["execution_audit"][-1]["result"] == "rejected"
            else:
                assert calls[-1][1] == "turn_on"
                assert leases[-1] is not None
                assert store.data["ev_telemetry"]["command_exposure"]["cost_per_hour"] > 0
                checkpoint = PlannerStore(hass, "runtime")
                await checkpoint.async_load()
                assert checkpoint.data["ev_telemetry"] == store.data["ev_telemetry"]
                later = dt_util.utcnow() + timedelta(minutes=1)
                monkeypatch.setattr(dt_util, "utcnow", lambda: later)
                result = await executor.async_restore_device_control("ev", "test_lease_expired")
                assert result.result == OutcomeResult.RESTORED
                assert leases[-1] is None
                assert "command_exposure" not in store.data["ev_telemetry"]
                assert store.data["ev_telemetry"]["emergency_spend"] > 0
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("premium,number_mapped,timer", [(False, True, True), (True, True, True),
                                                       (True, False, False), (False, False, True)])
def test_manual_ev_preserves_limits_and_durable_cost_authority(tmp_path, premium, number_mapped, timer):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        leases = []
        if timer:
            executor.ev_allocation_deadline_callback = leases.append
        executor.options.update(ev_price_limit_enabled=premium, ev_max_import_price=0.1,
                                ev_price_policy="departure_priority", ev_emergency_price=0.5, ev_emergency_budget=1)
        if number_mapped:
            attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
            hass.states.async_set("number.limit", 6, attrs)
            hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
            executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
            executor.options.update(ev_limit_min=1, ev_limit_max=6)

            async def limit(call):
                hass.states.async_set("number.limit", call.data["value"], attrs)

            hass.services.async_register("number", "set_value", limit)
        _arm(store, executor)
        _, context = _command("ev")
        context.slots[0].import_price = 0.3 if premium else 0.1
        try:
            result = await executor.async_manual_ev_charging(True, context)
            if premium and not timer:
                assert result.reason == "ev_allocation_timer_unavailable"
                assert not calls
            else:
                assert result.applied, result.reason
                assert leases
                if premium:
                    assert store.data["ev_telemetry"]["command_exposure"]["cost_per_hour"] > 0
                stopped = await executor.async_manual_ev_charging(False, context)
                assert stopped.applied, stopped.reason
                assert hass.states.get("switch.charger").state == "off"
        finally:
            await store.async_flush()

    asyncio.run(run())


def test_manual_start_below_minimum_physical_setpoint_is_rejected(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("number.limit", 6, {"unit_of_measurement": "kW", "min": 5, "max": 6, "step": 1})
        hass.states.async_set("sensor.power", 0, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.power")
        executor.options.update(ev_limit_min=5, ev_limit_max=6, ev_charge_rate_kw=4)
        _arm(store, executor)
        _, context = _command("ev")
        try:
            result = await executor.async_manual_ev_charging(True, context)
            assert result.reason == "ev_power_control_unavailable"
            assert not calls
        finally:
            await store.async_flush()

    asyncio.run(run())


def test_confirmed_power_does_not_recreate_a_released_reservation(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        plan, _ = _command("ev")
        executor._reconcile_ev_grid_reservation(plan.actions[0], SimpleNamespace(
            applied=True, post_state={"confirmed_power_kw": 3}, command_sent=True), None)
        assert executor._ev_grid_reservations() == {}

    asyncio.run(run())


@pytest.mark.parametrize("cached_source", [False, True])
def test_delayed_power_confirmation_releases_only_the_confirmed_reduction(tmp_path, cached_source):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
        hass.states.async_set("number.limit", 6, attrs)
        hass.states.async_set("sensor.ev_power", 6, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_limit_min=1, ev_limit_max=6, ev_charge_rate_kw=6, command_rate_limit_seconds=0)
        _arm(store, executor)
        old_sample = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()

        async def limit(call):
            hass.states.async_set("number.limit", call.data["value"], attrs)
            if cached_source:
                hass.states.async_set("sensor.ev_power", 3, {
                    "unit_of_measurement": "kW", "sampled_at_utc": old_sample,
                })

        hass.services.async_register("number", "set_value", limit)
        plan, context = _command("ev")
        plan.actions[0].desired_state.update(projected_load_kw_now=3, power_limit={
            "entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3})
        context.slots[0].projected_ev_load_kw = 3
        executor._ev_grid_reservations()[executor.entry_id] = {"load_kw": 6, "limit_kw": 10}
        try:
            reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
            assert reason is None
            adapter = EVChargerAdapter(hass, executor.entry_data, power_options=executor.options,
                                       confirmation_timeout_seconds=0, confirmation_retries=0)
            result = await adapter.async_execute(plan.actions[0])
            assert result.applied
            executor._reconcile_ev_grid_reservation(plan.actions[0], result, previous)
            await executor.async_persist_ev_grid_reservation()
            assert store.data["ev_grid_reservation"]["load_kw"] == 6
            assert store.data["ev_grid_reservation"]["pending_power_limit"]["value"] == 3
            executor.sync_ev_grid_reservation()
            assert executor._ev_grid_reservations()[executor.entry_id]["load_kw"] == 6
            original_boundary = store.data["ev_grid_reservation"]["pending_power_limit"]["at"]
            reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
            assert reason is None
            repeat = await adapter.async_execute(plan.actions[0])
            executor._reconcile_ev_grid_reservation(plan.actions[0], repeat, previous)
            assert executor._ev_grid_reservations()[executor.entry_id]["pending_power_limit"]["at"] == original_boundary
            hass.states.async_set("sensor.ev_power", 3, {
                "unit_of_measurement": "kW", "sampled_at_utc": dt_util.utcnow().isoformat(),
            })
            executor.sync_ev_grid_reservation()
            await executor.async_persist_ev_grid_reservation()
            assert store.data["ev_grid_reservation"]["load_kw"] == 3
            assert "pending_power_limit" not in store.data["ev_grid_reservation"]
            assert store.data["ev_grid_reservation"]["observed_load_kw"] == 3
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("confirmed_limit", [False, True])
def test_observed_ev_draw_raises_retained_capacity_and_enforces_grid_limit(tmp_path, confirmed_limit):
    """A 7 kW setting cannot conceal an observed 7.36 kW physical load."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("sensor.ev_power", 7360, {"unit_of_measurement": "W"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data["ev_power_entity"] = "sensor.ev_power"
        executor.options["grid_import_limit_kw"] = 14.2
        reservation = {"load_kw": 7.0, "limit_kw": 14.2}
        if confirmed_limit:
            hass.states.async_set("number.limit", 7, {
                "unit_of_measurement": "kW", "min": 1, "max": 7, "step": 1,
            })
            executor.entry_data["ev_power_limit_entity"] = "number.limit"
            executor.options.update(ev_limit_min=1, ev_limit_max=7)
            reservation["confirmed_power_limit"] = True
        executor._ev_grid_reservations()[executor.entry_id] = reservation
        _plan, context = _command("ev")
        assert context is not None
        context.slots[0].baseline_load_forecast_kw = 7
        assert executor._ev_reservation_safety_issue(context) is None
        try:
            executor.sync_ev_grid_reservation()
            assert reservation["load_kw"] == 7.36
            assert not reservation.get("confirmed_power_limit")
            assert executor._ev_reservation_safety_issue(context) == "grid_import_limit_exceeded"
            for reading in (6500, 0, "unavailable"):
                hass.states.async_set("sensor.ev_power", reading, {"unit_of_measurement": "W"})
                executor.sync_ev_grid_reservation()
                assert reservation["load_kw"] == 7.36
            await executor.async_persist_ev_grid_reservation()
            await store.async_flush()
            recovered = PlannerStore(hass, "runtime")
            await recovered.async_load()
            assert recovered.data["ev_grid_reservation"]["load_kw"] == 7.36
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize(
    "previous_kw, requested_kw, observed_kw, expected_kw",
    [(3, 7, None, 3), (3, 7, 6, 6), (7, 7, 7.36, 7.36), (3, 7, 9, 9)],
)
def test_rejected_start_retains_concurrent_physical_evidence(
    tmp_path, previous_kw, requested_kw, observed_kw, expected_kw,
):
    """Rollback must keep actual draw, without reserving an unissued increase."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data["ev_power_entity"] = "sensor.ev_power"
        original = {"load_kw": previous_kw, "limit_kw": 10}
        if previous_kw == 3:
            original["confirmed_power_limit"] = True
        reservations = executor._ev_grid_reservations()
        reservations[executor.entry_id] = original
        plan, context = _command("ev")
        assert context is not None
        action = plan.actions[0]
        action.desired_state["projected_load_kw_now"] = requested_kw
        context.slots[0].projected_ev_load_kw = requested_kw
        try:
            reason, previous = executor._reserve_ev_grid_capacity(action, context, dt_util.utcnow())
            assert reason is None
            assert previous is original
            if observed_kw is not None:
                hass.states.async_set("sensor.ev_power", observed_kw, {"unit_of_measurement": "kW"})
                executor.sync_ev_grid_reservation()
                for reading in (0, "unavailable"):
                    hass.states.async_set("sensor.ev_power", reading, {"unit_of_measurement": "kW"})
                    executor.sync_ev_grid_reservation()
            executor._reconcile_ev_grid_reservation(
                action, EVCommandResult(False, "ev_control_unavailable", {}, {}), previous,
            )
            restored = reservations[executor.entry_id]
            assert restored["load_kw"] == expected_kw
            if expected_kw > previous_kw:
                assert not restored.get("confirmed_power_limit")
            await executor.async_persist_ev_grid_reservation()
            await store.async_flush()
            recovered = PlannerStore(hass, "runtime")
            await recovered.async_load()
            assert recovered.data["ev_grid_reservation"]["load_kw"] == expected_kw
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("confirmation", ["delayed_sample", "adapter_outcome"])
def test_confirmed_reduction_retires_old_observed_capacity(tmp_path, confirmation):
    """A previous physical peak must not reappear after a proven reduction."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("number.limit", 3, {
            "unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1,
        })
        hass.states.async_set("sensor.ev_power", 3, {"unit_of_measurement": "kW"})
        executor = _executor(hass, PlannerStore(hass, "runtime"))
        executor.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        executor.options.update(ev_limit_min=1, ev_limit_max=6)
        reservations = executor._ev_grid_reservations()
        reservation = {"load_kw": 9, "observed_load_kw": 9, "limit_kw": 10}
        reservations[executor.entry_id] = reservation
        plan, context = _command("ev")
        assert context is not None
        action = plan.actions[0]
        try:
            if confirmation == "delayed_sample":
                reservation["pending_power_limit"] = {
                    "version": 1, "at": (dt_util.utcnow() - timedelta(seconds=1)).isoformat(),
                    "entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3,
                }
                executor.sync_ev_grid_reservation()
            else:
                executor._reconcile_ev_grid_reservation(action, EVCommandResult(
                    True, "ev_power_limit_confirmed", {}, {"confirmed_power_kw": 3}, command_sent=True,
                ), None)
            assert reservation["load_kw"] == reservation["observed_load_kw"] == 3
            reason, previous = executor._reserve_ev_grid_capacity(action, context, dt_util.utcnow())
            assert reason is None
            executor._reconcile_ev_grid_reservation(
                action, EVCommandResult(False, "ev_control_unavailable", {}, {}), previous,
            )
            assert reservations[executor.entry_id]["load_kw"] == 3
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("later", ["unavailable", "0", "confirmed_reduction", "renewed_start", "confirmed_stop"])
@pytest.mark.parametrize("feedback", ["power", "limit"])
def test_queued_ev_capacity_event_preserves_peak_but_not_superseded_evidence(tmp_path, monkeypatch, later, feedback):
    """Real HA queues both reports before the capacity listener can run."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        power_attrs = {"unit_of_measurement": "kW"}
        limit_attrs = {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1}
        hass.states.async_set("sensor.ev_power", 7, power_attrs)
        hass.states.async_set("number.limit", 3, limit_attrs)
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        configured.options.update(ev_limit_min=1, ev_limit_max=9)
        entry = ConfigEntry(
            domain=DOMAIN, title="Queued capacity", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        executor = coordinator.executor
        reservations = executor._ev_grid_reservations()
        reservations[entry.entry_id] = {"load_kw": 7, "limit_kw": 10}
        plan, context = _command("ev")
        context.slots[0].baseline_load_forecast_kw = 2
        issues = []

        async def refresh():
            issues.append(executor._ev_reservation_safety_issue(context))

        monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        await hass.async_block_till_done()
        coordinator.async_start_listeners()

        @callback
        def publish(_event):
            # HA queues nested state_changed events until this push callback
            # returns; both listeners then see the later value in hass.states.
            entity_id, attributes = (
                ("number.limit", limit_attrs) if feedback == "limit" else ("sensor.ev_power", power_attrs)
            )
            hass.states.async_set(entity_id, 9, attributes)
            if later == "confirmed_stop":
                executor._release_ev_grid_reservation()
            elif later in {"confirmed_reduction", "renewed_start"}:
                hass.states.async_set("number.limit", 3, limit_attrs)
                hass.states.async_set("sensor.ev_power", 3, power_attrs)
                executor._reconcile_ev_grid_reservation(plan.actions[0], EVCommandResult(
                    True, "ev_power_limit_confirmed", {}, {
                        "confirmed_power_kw": 3,
                        "confirmed_power_at": hass.states.get("sensor.ev_power").last_reported.isoformat(),
                    }, command_sent=True,
                ), None)
                if later == "renewed_start":
                    plan.actions[0].desired_state["projected_load_kw_now"] = 3
                    context.slots[0].projected_ev_load_kw = 3
                    reason, _previous = executor._reserve_ev_grid_capacity(
                        plan.actions[0], context, dt_util.utcnow(),
                    )
                    assert reason is None
            else:
                hass.states.async_set(entity_id, later, attributes)

        unsubscribe = hass.bus.async_listen("ev_provider_push", publish)
        try:
            hass.bus.async_fire("ev_provider_push")
            await hass.async_block_till_done()
            if later == "confirmed_stop":
                assert entry.entry_id not in reservations
                assert issues == []
            elif later == "confirmed_reduction":
                assert reservations[entry.entry_id]["load_kw"] == 3
                assert issues == []
            elif later == "renewed_start":
                # Renewal remains conservative at the configured rate until
                # its own confirmation, but must not revive the retired 9 kW.
                assert reservations[entry.entry_id]["load_kw"] == 7
                assert issues == [None]
            else:
                assert reservations[entry.entry_id]["load_kw"] == 9
                assert issues == ["grid_import_limit_exceeded"]
                await store.async_flush()
                reloaded = PlannerStore(hass, "runtime")
                await reloaded.async_load()
                assert reloaded.data["ev_grid_reservation"]["load_kw"] == 9
        finally:
            unsubscribe()
            await coordinator.async_shutdown()
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["captured", "jitter", "lost_target", "unavailable", "downward"])
def test_delayed_zone_clamp_keeps_real_hvac_ownership_then_honors_user(tmp_path, monkeypatch, scenario):
    """Real HA services and events retain the cycle at the captured 121.4 s delay."""
    from unittest.mock import AsyncMock

    async def run():
        payload = json.loads((Path(__file__).parent / "fixtures/hvac/delayed_zone_clamp.json").read_text())
        if scenario == "downward":
            payload.update(head_target=20, zone_before=24)
        clock = [datetime.fromisoformat(payload["issued_at"])]
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock[0])
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("climate.home", "heat", {"temperature": payload["head_before"]})
        hass.states.async_set("climate.zone", "heat", {"temperature": payload["zone_before"]})
        hass.state = CoreState.running
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.entry_data[CONF_CLIMATE_ZONES] = "climate.zone"
        entry = ConfigEntry(
            domain=DOMAIN, title="Delayed zone feedback", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        manual = AsyncMock()
        monkeypatch.setattr(coordinator, "_async_handle_manual_hvac_change", manual)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)

        async def temperature(call):
            calls.append((call.domain, call.service, dict(call.data)))
            clock[0] = datetime.fromisoformat(payload["confirmed_at"])
            hass.states.async_set(call.data["entity_id"], "heat", {"temperature": call.data["temperature"]},
                                  context=call.context)

        hass.services.async_register("climate", "set_temperature", temperature)
        plan, context = _command("hvac")
        plan.actions[0].desired_state.update(target_temperature=payload["head_target"], phase="preconditioning")
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        try:
            await coordinator.executor.async_evaluate(plan, context)
            await hass.async_block_till_done()
            assert hass.states.get("climate.home").attributes["temperature"] == payload["head_target"]
            baseline = store.data["ownership"]["hvac_control"]["main_state"]
            assert baseline["target_temperature"] == 19
            before = list(calls)
            clock[0] = datetime.fromisoformat(payload["feedback_at"])
            if scenario == "jitter":
                clock[0] += timedelta(seconds=1)
            if scenario in {"lost_target", "unavailable"}:
                hass.states.async_set("climate.zone", "unavailable" if scenario == "unavailable" else "heat",
                                      {"temperature": None})
                await hass.async_block_till_done()
                manual.assert_not_awaited()
                clock[0] += timedelta(seconds=1)
            hass.states.async_set("climate.zone", "heat", {"temperature": payload["zone_after"]})
            await hass.async_block_till_done()
            manual.assert_not_awaited()
            assert not coordinator.overrides
            assert calls == before
            assert store.data["ownership"]["hvac_control"]["main_state"] == baseline
            assert hass.states.get("automation.climate").state == "off"
            hass.states.async_set("climate.zone", "heat", {"temperature": 23}, context=Context(user_id="operator"))
            await hass.async_block_till_done()
            manual.assert_awaited_once_with("climate_zone_changed", preserve_zone_entity_id="climate.zone")
            assert coordinator.executor.hvac_feedback_commands == []
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_manual_stop_keeps_original_limit_after_restore_failure_and_restart(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
        hass.states.async_set("number.limit", 3, attrs)
        hass.states.async_set("switch.charger", "on")
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit")
        _arm(store, executor)
        store.data["ownership"] = {"ev_smart_charging_state": {
            "ev_power_limit_entity": "6", "ev_power_limit_unit": "kW", "ev_power_ownership_version": 1}}
        fail = True

        async def limit(call):
            if fail:
                raise RuntimeError("restoration failed")
            hass.states.async_set("number.limit", call.data["value"], attrs)

        hass.services.async_register("number", "set_value", limit)
        _, context = _command("ev")
        try:
            result = await executor.async_manual_ev_charging(False, context)
            assert not result.applied and result.reason == "ev_power_limit_restore_failed"
            assert hass.states.get("switch.charger").state == "off"
            await store.async_flush()
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "6"
            fail = False
            executor.store = fresh
            result = await executor.async_manual_ev_charging(False, context)
            assert result.applied
            assert hass.states.get("number.limit").state == "6.0"
            assert "ev_smart_charging_state" not in fresh.data["ownership"]
        finally:
            await hass.async_stop()
    asyncio.run(run())


def test_expired_ev_allocation_stops_a_previously_on_baseline(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("switch.charger", "on")
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        store.data["ownership"] = {"ev_smart_charging_state": {CONF_EV_CHARGER: "on"}}
        store.data["ev_telemetry"] = {"version": 1, "command_exposure": {
            "at": (dt_util.utcnow()-timedelta(minutes=5)).isoformat(), "cost_per_hour": 3}}
        try:
            await executor.async_restore_device_control("ev", "ev_allocation_expired")
            assert hass.states.get("switch.charger").state == "off"
            assert "command_exposure" not in store.data["ev_telemetry"]
            assert "ev_smart_charging_state" not in store.data["ownership"]
        finally:
            await hass.async_stop()
    asyncio.run(run())


def test_failed_limit_restoration_does_not_accrue_spending_after_confirmed_stop(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("switch.charger", "on")
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data["ev_power_limit_entity"] = "number.limit"
        store.data["ownership"] = {"ev_smart_charging_state": {
            "ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"}}
        store.data["ev_telemetry"] = {"version": 1, "command_exposure": {
            "at": (dt_util.utcnow()-timedelta(minutes=5)).isoformat(), "cost_per_hour": 3}}

        async def fail_limit(call):
            raise RuntimeError("number offline")

        hass.services.async_register("number", "set_value", fail_limit)
        try:
            await executor.async_restore_device_control("ev", "ev_allocation_expired")
            assert hass.states.get("switch.charger").state == "off"
            assert "command_exposure" not in store.data["ev_telemetry"]
            assert store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "6"
        finally:
            await hass.async_stop()
    asyncio.run(run())


def test_confirmed_stop_is_not_billed_again_on_the_next_telemetry_update(tmp_path):
    from custom_components.ha_energy_planner.ev_telemetry import update_ev_telemetry

    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("switch.charger", "on")
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        start = dt_util.utcnow() - timedelta(minutes=5)
        sample = {"identity": [None]*5+[6, 1, 6, 250, 1], "at": start.isoformat(),
                  "soc": 40, "connected": True, "charging": True, "reserved_kw": 6,
                  "price": 0.4, "normal_ceiling": 0.2}
        store.data["ownership"] = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}
        store.data["ev_telemetry"] = {"version": 1, "identity": sample["identity"], "last_sample": sample,
                                    "command_exposure": {"at": start.isoformat(), "cost_per_hour": 1.2}}
        try:
            await executor.async_restore_device_control("ev", "ev_allocation_expired")
            settled = store.data["ev_telemetry"]["emergency_spend"]
            record = update_ev_telemetry(store.data["ev_telemetry"],
                {**sample, "at": (start+timedelta(minutes=10)).isoformat(), "charging": False}, reserved_kw=6)
            assert record["emergency_spend"] == settled
            record = update_ev_telemetry(record,
                {**sample, "at": (start+timedelta(minutes=15)).isoformat(), "charging": False}, reserved_kw=6)
            assert record["emergency_spend"] == settled
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("unplugged", [False, True])
def test_vehicle_policy_release_closes_old_spending_and_resets_only_on_unplug(tmp_path, unplugged):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        store.data["ev_telemetry"] = {"version": 1, "emergency_spend": 0.5, "budget_uncertain": True,
            "pending": {"aggregate": {"energy": 1}}, "last_sample": {"reserved_kw": 7},
            "command_exposure": {"at": dt_util.utcnow().isoformat(), "cost_per_hour": 1}}
        deadlines = []
        executor.ev_allocation_deadline_callback = deadlines.append
        try:
            await executor.async_release_ev_policy(unplugged=unplugged)
            assert deadlines == [None]
            assert not calls
            telemetry = store.data["ev_telemetry"]
            assert "command_exposure" not in telemetry
            if unplugged:
                assert telemetry["emergency_spend"] == 0
                assert "budget_uncertain" not in telemetry
                assert not telemetry["pending"]
                assert "last_sample" not in telemetry
            else:
                assert telemetry["emergency_spend"] >= 0.5
                assert telemetry["budget_uncertain"]
        finally:
            await hass.async_stop()
    asyncio.run(run())


@pytest.mark.parametrize("climate_limit", [1, 12])
def test_climate_policy_update_and_resume_preserve_real_runtime_safety(tmp_path, monkeypatch, climate_limit):
    """Real HA events, commands and durable ownership across policy saves and resume."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.options["command_rate_limit_seconds"] = 0
        configured.options["max_daily_climate_actions"] = climate_limit
        configured.entry_data["climate_manual_override_entity"] = "input_boolean.manual"
        entry = ConfigEntry(
            domain=DOMAIN, title="Climate lifecycle", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        plan, context = _command("hvac")
        plan.actions[0].desired_state.update({
            "phase": "preconditioning", "precondition_end": (dt_util.utcnow() + timedelta(minutes=5)).isoformat(),
        })
        plan.device_plans["climate"] = {"preconditioning": {"status": "scheduled"}}
        await store.async_save_plan(plan)
        coordinator.async_set_updated_data(plan)
        async def replan():
            coordinator.async_set_updated_data(plan)
            await store.async_save_plan(plan)
            await coordinator.executor.async_evaluate(plan, context)
        monkeypatch.setattr(coordinator, "async_request_replan", replan)
        monkeypatch.setattr(coordinator, "async_refresh", replan)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *a, **kw: None)
        async def helper_off(call):
            hass.states.async_set("input_boolean.manual", "on" if call.service == "turn_on" else "off",
                                  context=call.context)
        hass.services.async_register("input_boolean", "turn_off", helper_off)
        hass.services.async_register("input_boolean", "turn_on", helper_off)
        hass.states.async_set("input_boolean.manual", "off")
        # Seed device states before subscribing, as in an already-running HA instance.
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        try:
            await replan()
            await hass.async_block_till_done()
            assert hass.states.get("climate.home").attributes["temperature"] == 23
            assert len(store.data["action_attempts"]) == 1
            ownership = dict(store.data["ownership"])
            before = len(calls)
            object.__setattr__(entry, "options", MappingProxyType({**entry.options, "max_daily_ev_actions": 20}))
            await coordinator.async_handle_options_update()
            await hass.async_block_till_done()
            assert store.data["production"]["armed"]
            assert store.data["ownership"]["hvac_control"] == ownership["hvac_control"]
            assert store.data["ownership"]["climate_automations"] == ownership["climate_automations"]
            assert all(domain == "automation" and service == "turn_off" for domain, service, _ in calls[before:])
            assert not coordinator.overrides
            assert len(store.data["action_attempts"]) == 1
            await coordinator.async_set_manual_hvac_override(60, "runtime_manual")
            await hass.async_block_till_done()
            assert coordinator.overrides
            result = await coordinator.async_resume_climate_planning()
            await hass.async_block_till_done()
            assert not coordinator.overrides
            assert hass.states.get("input_boolean.manual").state == "off"
            if climate_limit == 1:
                assert result["status"] == "blocked"
                assert result["reason"] == "climate_daily_action_cap_reached"
                assert "1 of 1" in result["summary"]
                assert hass.states.get("climate.home").attributes["temperature"] == 20
                assert len(store.data["action_attempts"]) == 1
            else:
                assert result["status"] == "running"
                assert hass.states.get("climate.home").attributes["temperature"] == 23
                assert len(store.data["action_attempts"]) == 2
            fresh = PlannerStore(hass, "runtime")
            await fresh.async_load()
            assert fresh.data["action_attempts"] == store.data["action_attempts"]
            assert fresh.data["overrides"] == []
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_delayed_main_shutdown_feedback_does_not_leave_runtime_manual_hold(tmp_path, monkeypatch):
    """Publish Daikin's fresh-context zone shutdown through actual HA listeners."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("climate.home", "off", {"temperature": 20})
        hass.states.async_set("climate.room", "cool", {"temperature": 19})
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.entry_data[CONF_CLIMATE_ZONES] = ["climate.room"]
        entry = ConfigEntry(
            domain=DOMAIN, title="Delayed climate feedback", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        async def no_replan():
            pass
        monkeypatch.setattr(coordinator, "async_request_replan", no_replan)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *a, **kw: None)
        async def shutdown(call):
            await asyncio.sleep(0.01)
            hass.states.async_set("climate.room", "off", {"temperature": None})
            await asyncio.sleep(0)
            hass.states.async_set("climate.home", "off", {"temperature": 20}, context=call.context)
        async def startup(call):
            previous = hass.states.get(call.data["entity_id"])
            hass.states.async_set(call.data["entity_id"], "heat", dict(previous.attributes), context=call.context)
        hass.services.async_register("climate", "turn_on", startup)
        hass.services.async_register("climate", "turn_off", shutdown)
        # Seed device states before subscribing, as in an already-running HA instance.
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        try:
            await coordinator.executor.async_evaluate(*_command("hvac"))
            await hass.async_block_till_done()
            assert store.data["ownership"].get("hvac_control"), store.data["execution_audit"]
            async with coordinator._command_lock:
                result = await coordinator.executor.async_restore_device_control("daikin", "runtime_release")
            await hass.async_block_till_done()
            assert result.result == OutcomeResult.RESTORED
            assert hass.states.get("climate.home").state == "off"
            assert hass.states.get("climate.room").attributes["temperature"] is None
            assert not coordinator.overrides
            assert not store.data["ownership"].get("manual_hvac_override_expires_at")
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_superseded_first_plan_asks_home_assistant_to_retry_setup(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        entry = ConfigEntry(
            domain=DOMAIN, title="Superseded first refresh", data={}, options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        coordinator = EnergyPlannerCoordinator(hass, entry, PlannerStore(hass, entry.entry_id))
        add_job = hass.async_add_executor_job

        async def change_inputs(function, *args):
            result = await add_job(function, *args)
            if getattr(function, "__name__", None) == "create_plan":
                coordinator._mark_replan_requested()
            return result

        monkeypatch.setattr(hass, "async_add_executor_job", change_inputs)
        try:
            with pytest.raises(ConfigEntryNotReady, match="before the first plan was ready"):
                await coordinator.async_config_entry_first_refresh()
            assert coordinator.data is None
            assert not coordinator.last_refresh_metadata["succeeded"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_force_replan_waits_for_an_inflight_core_refresh(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = ConfigEntry(
            domain=DOMAIN, title="Force Replan", data={}, options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, PlannerStore(hass, entry.entry_id))
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        plan, _ = _command("ev")

        async def refresh():
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
                await release.wait()
            return plan

        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        try:
            # Prime Core's request cooldown before holding its refresh lock.
            await coordinator.async_request_refresh()
            first = asyncio.create_task(coordinator.async_refresh())
            await asyncio.wait_for(started.wait(), 1)
            replan = asyncio.create_task(coordinator.async_request_replan())
            await asyncio.sleep(0)
            assert not replan.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(first, replan), 1)
            assert calls == 3
        finally:
            release.set()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("refresh_outcome", ["fresh", "failed", "superseded"])
def test_resume_refreshes_during_home_assistant_debounce_cooldown(tmp_path, monkeypatch, refresh_outcome):
    """Resume must publish fresh gate evidence even after a recent refresh."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        entry = ConfigEntry(
            domain=DOMAIN, title="Resume debounce", data={}, options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        plan, _ = _command("hvac")
        refreshes = 0

        async def refresh():
            nonlocal refreshes
            refreshes += 1
            if refresh_outcome == "failed" and refreshes == 2:
                raise TimeoutError("planner input timeout")
            if refresh_outcome == "superseded" and refreshes == 2:
                coordinator.last_refresh_metadata = {"succeeded": False}
                return plan
            store.data["preconditioning_history"] = {"pending": {"last_outcome": {
                "plan_id": plan.plan_id, "result": "rejected",
                "reason": "manual_hvac_override" if refreshes == 1 else "device_control_paused",
            }}}
            return plan

        async def clear():
            return True

        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(coordinator, "_async_clear_expired_manual_hvac_state", clear)
        try:
            await coordinator.async_request_refresh()
            assert refreshes == 1
            if refresh_outcome != "fresh":
                with pytest.raises(HomeAssistantError, match="Could not refresh climate planning"):
                    await coordinator.async_resume_climate_planning()
                assert coordinator.last_update_success is (refresh_outcome == "superseded")
            else:
                result = await coordinator.async_resume_climate_planning()
                assert result["reason"] == "device_control_paused"
            assert refreshes == 2
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("refresh_outcome", ["fresh", "failed", "superseded", "queued", "unpublished", "unload"])
def test_charge_now_requires_fresh_evidence_during_debounce_cooldown(tmp_path, monkeypatch, refresh_outcome):
    """A real HA debouncer must not let stale capacity evidence authorize a start."""
    from unittest.mock import AsyncMock

    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        entry = ConfigEntry(
            domain=DOMAIN, title="Charge now freshness", data={}, options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        plan, context = _command("ev")
        refreshes = 0
        refreshed = asyncio.Event()

        async def refresh():
            nonlocal refreshes
            refreshes += 1
            if refresh_outcome == "failed" and refreshes == 2:
                raise TimeoutError("planner input timeout")
            if refreshes == 1 or refresh_outcome != "superseded":
                coordinator._last_decision_context = context if refreshes == 1 else None
                coordinator._last_decision_generation = coordinator._refresh_generation
            if refreshes == 2:
                refreshed.set()
            return plan

        start = AsyncMock(return_value=SimpleNamespace(applied=False, reason="ev_grid_projection_unavailable"))
        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(coordinator.executor, "async_manual_ev_charging", start)
        try:
            await coordinator.async_request_refresh()
            assert coordinator._last_decision_context is context
            if refresh_outcome in {"queued", "unpublished", "unload"}:
                async with coordinator._command_lock:
                    charge = asyncio.create_task(coordinator.async_charge_now())
                    await asyncio.wait_for(refreshed.wait(), 1)
                    assert not charge.done()
                    if refresh_outcome == "unload":
                        coordinator._begin_shutdown()
                    else:
                        coordinator._mark_replan_requested()
                    if refresh_outcome == "unpublished":
                        coordinator._last_decision_context = SimpleNamespace(plan_id="unpublished")
                        coordinator._last_decision_generation = coordinator._refresh_generation
                with pytest.raises(HomeAssistantError, match="Could not refresh charging evidence"):
                    await charge
                start.assert_not_awaited()
            elif refresh_outcome != "fresh":
                with pytest.raises(HomeAssistantError, match="Could not refresh charging evidence"):
                    await coordinator.async_charge_now()
                start.assert_not_awaited()
                assert coordinator._last_decision_context is context
            else:
                result = await coordinator.async_charge_now()
                assert not result.applied
                assert start.call_args.args == (True, None)
            assert refreshes == (3 if refresh_outcome == "fresh" else 2)
            assert coordinator.overrides == []
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("stop_confirmed", [False, True])
@pytest.mark.parametrize("initial_control", ["off", "on"])
def test_charge_now_start_confirmation_loss_requires_stop_proof(tmp_path, monkeypatch, stop_confirmed, initial_control):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("switch.charger", initial_control)
        hass.states.async_set("binary_sensor.charging", "off")
        entry = ConfigEntry(
            domain=DOMAIN, title="Charge now lost feedback", source="user", unique_id=None,
            data={CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected",
                  CONF_EV_CHARGING: "binary_sensor.charging"},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7, "grid_import_limit_kw": 10,
                     "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 1},
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        plan, context = _command("ev")
        assert context is not None
        context.ev_target_soc_percent = 80

        async def refresh():
            coordinator._last_decision_context = context
            coordinator._last_decision_generation = coordinator._refresh_generation
            return plan

        async def actuate(call):
            calls.append((call.domain, call.service, dict(call.data)))
            started = call.service == "turn_on"
            hass.states.async_set("switch.charger", "on" if started else "off", context=call.context)
            feedback = "unavailable" if started or not stop_confirmed else "off"
            hass.states.async_set("binary_sensor.charging", feedback, context=call.context)

        if initial_control == "on":
            confirm_state = EVChargerAdapter._async_confirm_state

            async def lose_control_before_retry(adapter, entity_id, enabled, **kwargs):
                if enabled and not calls:
                    hass.states.async_set("switch.charger", "off")
                    return "timeout"
                return await confirm_state(adapter, entity_id, enabled, **kwargs)

            monkeypatch.setattr(EVChargerAdapter, "_async_confirm_state", lose_control_before_retry)
        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        hass.services.async_register("switch", "turn_on", actuate)
        hass.services.async_register("switch", "turn_off", actuate)
        try:
            result = await coordinator.async_charge_now()

            assert not result.applied and result.command_sent
            assert result.reason == "ev_charging_confirmation_unavailable"
            assert result.safe_state_confirmed is stop_confirmed
            assert result.rollback_succeeded is stop_confirmed
            assert [service for _, service, _ in calls] == ["turn_on", "turn_off"]
            assert hass.states.get("switch.charger").state == "off"
            assert (coordinator._ev_allocation_deadline is None) is stop_confirmed
            reloaded = PlannerStore(hass, entry.entry_id)
            await reloaded.async_load()
            assert reloaded.data["ev_grid_reservation"]["active"] is not stop_confirmed
            assert bool(reloaded.data["ownership"].get("ev_smart_charging_state")) is not stop_confirmed
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("interruption", "prior_exposure", "power_mapped"), [
    (None, False, False), (None, True, False), ("generation", True, False),
    (None, False, True), ("power_feedback", True, True), ("batched", False, False), ("before_start", False, False),
    ("before_start", False, True), ("generation", True, True), ("owned", True, False),
    ("owned_power_feedback", True, True), ("unload", False, False), ("age", False, False),
    ("confirmed_stop", True, False), ("confirmed_stop", True, True),
    ("confirmed_first_stop", True, True),
    ("confirmed_stop_incompatible_limit", True, True),
    ("uncertain_stop", True, False), ("cancelled", True, False), ("manual_cancelled", False, True),
    ("restore_higher_limit", True, True), ("restore_limit_error", True, True),
    ("paused", False, False), ("shared_capacity", False, False), ("cancelled_unsent", False, False),
    ("soc_reaches_target", False, False), ("target_reaches_soc", False, False),
    ("soc_unavailable", False, False), ("target_unavailable", False, False),
    ("connection_mapping_changed", False, True), ("grid_limit_changed", False, False),
    ("unrelated_option_changed", False, False),
    ("missing_capacity_registry", False, False), ("missing_capacity_reservation", False, False),
    ("manual_cancelled_unsent", False, False),
    ("disconnected", False, True), ("unknown_connection", False, True),
    ("disconnect_before_limit", False, True), ("disconnect_before_retry", False, False),
    ("limit_transient_mismatch", False, True), ("limit_transient_unavailable", False, True),
    ("limit_polled_feedback", False, True), ("limit_linked_feedback", False, True),
    ("limit_competing_user", False, True), ("limit_competing_parent", False, True),
    ("limit_invalid_before_dispatch", True, True),
    ("manual_limit_edit", False, True),
    ("manual_limit_during_save", True, True), ("manual_limit_during_save_then_unavailable", True, True),
    ("queued_manual_limit_during_save", True, True),
])
def test_charge_now_rechecks_context_at_dispatch(tmp_path, monkeypatch, interruption, prior_exposure, power_mapped):
    """Pause durable preparation after the refresh and command-lock checks."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        data = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected"}
        options = {**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7, "grid_import_limit_kw": 10}
        target_changes = {
            "soc_reaches_target": ("sensor.ev_soc", "80"),
            "target_reaches_soc": ("number.ev_target", "79"),
            "soc_unavailable": ("sensor.ev_soc", "unavailable"),
            "target_unavailable": ("number.ev_target", "unknown"),
        }
        target_update = target_changes.get(interruption)
        target_crossing = interruption in {"soc_reaches_target", "target_reaches_soc"}
        if target_update is not None:
            data.update({CONF_EV_SOC: "sensor.ev_soc", CONF_EV_SMART_CHARGING_TARGET_SOC: "number.ev_target"})
            hass.states.async_set("sensor.ev_soc", "79")
            hass.states.async_set("number.ev_target", "80")
        manual_limit_during_save = interruption in {
            "manual_limit_during_save", "manual_limit_during_save_then_unavailable", "queued_manual_limit_during_save",
        }
        start_sent = asyncio.Event()
        start_finished = asyncio.Event()
        cancelled = interruption in {"cancelled", "manual_cancelled"}
        if cancelled or interruption in {
            "confirmed_stop", "confirmed_first_stop", "uncertain_stop", "manual_limit_edit", "disconnect_before_retry",
            "confirmed_stop_incompatible_limit",
        }:
            data[CONF_EV_CHARGING] = "binary_sensor.charging"
            options.update(ev_confirmation_timeout_seconds=0, ev_confirmation_retries=1)
            hass.states.async_set("binary_sensor.charging", "off")

            async def start(call):
                calls.append((call.domain, call.service, dict(call.data)))
                hass.states.async_set("switch.charger", "on", context=call.context)
                if interruption == "manual_limit_edit":
                    hass.states.async_set("number.limit", 4, attrs)
                if interruption == "confirmed_first_stop":
                    hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                if interruption == "confirmed_stop_incompatible_limit":
                    hass.states.async_set("number.limit", requested_limit, {**attrs, "unit_of_measurement": "A"})
                if interruption == "disconnect_before_retry":
                    hass.states.async_set("binary_sensor.connected", "off")
                else:
                    coordinator._mark_replan_requested()
                start_sent.set()
                if cancelled:
                    await start_finished.wait()

            async def refuse_stop(call):
                calls.append((call.domain, call.service, dict(call.data)))
                raise HomeAssistantError("Stop feedback unavailable")

            hass.services.async_register("switch", "turn_on", start)
            if interruption == "uncertain_stop":
                hass.services.async_register("switch", "turn_off", refuse_stop)
        if power_mapped:
            attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
            restoring_higher_limit = interruption == "restore_higher_limit"
            initial_limit, requested_limit = (6, 5) if restoring_higher_limit else (5, 6)
            if manual_limit_during_save:
                initial_limit, requested_limit = 3, 5
            hass.states.async_set("number.limit", initial_limit, attrs)
            hass.states.async_set("sensor.ev_power", 2 if manual_limit_during_save else 0,
                                  {"unit_of_measurement": "kW"})
            data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
            options.update(ev_limit_min=1, ev_limit_max=6)
            if restoring_higher_limit or manual_limit_during_save or interruption == "limit_invalid_before_dispatch":
                options["ev_charge_rate_kw"] = requested_limit

            async def limit(call):
                calls.append((call.domain, call.service, dict(call.data)))
                retry_requires_stop = interruption == "restore_limit_error" and call.data["value"] == initial_limit
                if retry_requires_stop and hass.states.get("switch.charger").state == "on":
                    raise HomeAssistantError("Limit could not be restored while charging")
                feedback_context = call.context
                if call.data["value"] == requested_limit:
                    if interruption == "limit_polled_feedback":
                        feedback_context = None
                    elif interruption == "limit_linked_feedback":
                        feedback_context = Context(parent_id=call.context.id)
                    elif interruption == "limit_competing_user":
                        feedback_context = Context(user_id="operator")
                    elif interruption == "limit_competing_parent":
                        feedback_context = Context(parent_id="another_command")
                hass.states.async_set("number.limit", call.data["value"], attrs, context=feedback_context)
                transient_limit = {"limit_transient_mismatch": 4, "limit_transient_unavailable": "unavailable"}.get(
                    interruption,
                )
                if transient_limit is not None and call.data["value"] == requested_limit:
                    hass.states.async_set("number.limit", transient_limit, attrs, context=call.context)
                    hass.states.async_set("number.limit", requested_limit, attrs, context=call.context)
                if (interruption in {"power_feedback", "owned_power_feedback", "restore_higher_limit",
                                     "restore_limit_error"} and call.data["value"] == requested_limit):
                    hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                if interruption in {"disconnected", "unknown_connection"} and call.data["value"] == requested_limit:
                    connection = "off" if interruption == "disconnected" else "unknown"
                    hass.states.async_set("binary_sensor.connected", connection)
                await asyncio.sleep(0)

            hass.services.async_register("number", "set_value", limit)
        entry = ConfigEntry(
            domain=DOMAIN, title="Charge now dispatch", data=data, options=options,
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        if interruption in {"connection_mapping_changed", "grid_limit_changed", "unrelated_option_changed"}:
            loader.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await hass.config_entries.async_initialize()
            hass.config_entries._entries[entry.entry_id] = entry
            hass.states.async_set("binary_sensor.replacement_connected", "off")
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        plan, context = _command("ev")
        assert context is not None
        context.ev_target_soc_percent = 80
        if target_update is not None:
            context.current_ev_soc_percent = 79
        exposure = {"at": context.created_at.isoformat(), "cost_per_hour": 0.2}
        if prior_exposure:
            await store.async_save_ev_telemetry({"command_exposure": exposure})
        previous_ownership = {}
        previous_deadline = None
        previous_load = 5.0 if interruption == "limit_invalid_before_dispatch" else 3.0
        reservations = coordinator.executor._ev_grid_reservations()
        if interruption == "shared_capacity":
            reservations["second_ev"] = {"load_kw": 1.0, "limit_kw": 10.0}
        charger_on = manual_limit_during_save or interruption in {
            "owned", "owned_power_feedback", "restore_higher_limit", "restore_limit_error",
            "limit_invalid_before_dispatch",
        }
        if charger_on or interruption in {"confirmed_stop", "uncertain_stop", "cancelled",
                                          "confirmed_stop_incompatible_limit"}:
            previous_ownership = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}
            await store.async_save_ownership(previous_ownership)
            reservations[entry.entry_id] = {"load_kw": previous_load, "limit_kw": 10.0}
            if manual_limit_during_save:
                reservations[entry.entry_id].update(confirmed_power_limit=True, observed_load_kw=3.0)
            await coordinator.executor.async_persist_ev_grid_reservation()
            previous_deadline = dt_util.utcnow() + timedelta(
                minutes=120 if interruption == "owned_power_feedback" else 1,
            )
            coordinator._schedule_ev_allocation_deadline(previous_deadline)
            if charger_on:
                hass.states.async_set("switch.charger", "on")
        saving = asyncio.Event()
        resume = asyncio.Event()
        flush = store.async_flush

        async def refresh():
            coordinator._last_decision_context = context
            coordinator._last_decision_generation = coordinator._refresh_generation
            return plan

        async def hold_provisional_flush():
            await flush()
            if not saving.is_set():
                saving.set()
                await resume.wait()

        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(store, "async_flush", hold_provisional_flush)
        if interruption == "before_start":
            execute = EVChargerAdapter.async_execute

            async def invalidate_at_dispatch(adapter, action):
                coordinator._mark_replan_requested()
                return await execute(adapter, action)

            monkeypatch.setattr(EVChargerAdapter, "async_execute", invalidate_at_dispatch)
        if (power_mapped and interruption not in {"manual_cancelled", "disconnect_before_limit"}) or target_crossing:
            coordinator.async_start_listeners()
        if interruption in {"manual_cancelled", "manual_cancelled_unsent"}:
            await coordinator.async_refresh()
            task = asyncio.create_task(coordinator.async_manual_ev_charging(True))
        else:
            task = asyncio.create_task(coordinator.async_charge_now())
        try:
            await asyncio.wait_for(saving.wait(), 2)
            assert calls == []
            assert store.data["ev_grid_reservation"]["active"]
            if interruption in {"generation", "owned", "batched"}:
                coordinator._mark_replan_requested()
            elif interruption == "limit_invalid_before_dispatch":
                generation = coordinator._refresh_generation
                hass.states.async_set("number.limit", initial_limit, {**attrs, "max": initial_limit})
                assert coordinator._refresh_generation == generation
            elif manual_limit_during_save:
                generation = coordinator._refresh_generation
                if interruption == "queued_manual_limit_during_save":
                    @callback
                    def publish(_event):
                        hass.states.async_set("number.limit", 6, attrs)
                        hass.states.async_set("number.limit", "unavailable", attrs)

                    hass.bus.async_listen_once("ev_provider_push", publish)
                    hass.bus.async_fire("ev_provider_push")
                else:
                    hass.states.async_set("number.limit", 6, attrs)
                await asyncio.sleep(0)
                assert reservations[entry.entry_id]["load_kw"] == 6
                assert coordinator._refresh_generation > generation
                if interruption == "manual_limit_during_save_then_unavailable":
                    hass.states.async_set("number.limit", "unavailable", attrs)
                    await asyncio.sleep(0)
            elif interruption == "paused":
                await store.async_save_control_pause({"active": True, "assets": ["ev"]})
            elif interruption == "shared_capacity":
                reservations["second_ev"]["load_kw"] = 8.0
            elif interruption in {"connection_mapping_changed", "grid_limit_changed", "unrelated_option_changed"}:
                generation = coordinator._refresh_generation
                if interruption == "connection_mapping_changed":
                    hass.config_entries.async_update_entry(entry, data={
                        **entry.data, CONF_EV_CONNECTED: "binary_sensor.replacement_connected",
                    })
                else:
                    key, value = ("grid_import_limit_kw", 2) if interruption == "grid_limit_changed" else (
                        "manual_hvac_override_minutes", 45,
                    )
                    hass.config_entries.async_update_entry(entry, options={**entry.options, key: value})
                assert coordinator._refresh_generation == generation
            elif target_update is not None:
                generation = coordinator._refresh_generation
                hass.states.async_set(*target_update)
                await asyncio.sleep(0)
                assert coordinator._refresh_generation == generation
            elif interruption == "disconnect_before_limit":
                hass.states.async_set("binary_sensor.connected", "off")
            elif interruption == "missing_capacity_registry":
                monkeypatch.setitem(hass.data[DOMAIN], "ev_grid_reservations", None)
            elif interruption == "missing_capacity_reservation":
                reservations.pop(entry.entry_id)
            elif interruption in {"cancelled_unsent", "manual_cancelled_unsent"}:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                reloaded = PlannerStore(hass, entry.entry_id)
                await reloaded.async_load()
                assert not reloaded.data["ev_grid_reservation"]["active"]
                assert not reloaded.data["ownership"]
                assert not calls
                return
            elif interruption == "unload":
                coordinator._begin_shutdown()
            elif interruption == "age":
                now = context.created_at + timedelta(hours=1)
                monkeypatch.setattr(dt_util, "utcnow", lambda: now)
            if cancelled:
                resume.set()
                await asyncio.wait_for(start_sent.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert coordinator._ev_allocation_deadline == previous_deadline
                assert store.data["ev_grid_reservation"]["active"]
                if interruption == "manual_cancelled":
                    assert store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "5"
                else:
                    assert store.data["ownership"] == previous_ownership
                return
            if interruption == "batched":
                async with store.async_delay_save():
                    resume.set()
                    result = await task
                    reloaded = PlannerStore(hass, entry.entry_id)
                    await reloaded.async_load()
                    assert not reloaded.data["ev_grid_reservation"]["active"]
                    assert reloaded.data["ownership"] == previous_ownership
            else:
                resume.set()
                result = await task
            if interruption in {None, "unrelated_option_changed", "limit_polled_feedback", "limit_linked_feedback"}:
                assert result.applied and result.command_sent
                assert calls[-1] == ("switch", "turn_on", {"entity_id": "switch.charger"})
                assert coordinator._ev_allocation_deadline == coordinator.overrides[-1].expires_at
                if power_mapped:
                    assert calls[0] == ("number", "set_value", {"entity_id": "number.limit", "value": 6.0})
                if prior_exposure:
                    assert store.data["ev_telemetry"]["command_exposure"]["cost_per_hour"] == 0
            elif interruption in {
                "confirmed_stop", "confirmed_first_stop", "uncertain_stop", "disconnect_before_retry",
            }:
                assert not result.applied and result.command_sent
                assert result.reason == "ev_grid_projection_stale"
                stopped = interruption != "uncertain_stop"
                assert result.safe_state_confirmed is stopped
                assert result.rollback_succeeded is stopped
                assert coordinator._ev_allocation_deadline == (None if stopped else previous_deadline)
                assert store.data["ev_grid_reservation"]["active"] is not stopped
                assert bool(store.data["ownership"]) is not stopped
                assert bool(store.data.get("ev_telemetry", {}).get("command_exposure")) is not stopped
                assert hass.states.get("switch.charger").state == ("off" if stopped else "on")
                assert [service for _, service, _ in calls] == (
                    ["set_value", "turn_on", "turn_off", "set_value"] if power_mapped else ["turn_on", "turn_off"]
                )
            elif interruption == "confirmed_stop_incompatible_limit":
                assert not result.applied and result.command_sent and result.safe_state_confirmed
                assert not result.rollback_succeeded
                assert [service for _, service, _ in calls] == ["set_value", "turn_on", "turn_off"]
                assert hass.states.get("switch.charger").state == "off"
                assert coordinator._ev_allocation_deadline is None
                reloaded = PlannerStore(hass, entry.entry_id)
                await reloaded.async_load()
                assert reloaded.data["ev_grid_reservation"] == {"active": False}
                _rehydrate_ev_grid_reservation(hass, entry, reloaded.data)
                assert entry.entry_id not in reservations
                saved_ev = reloaded.data["ownership"]["ev_smart_charging_state"]
                assert saved_ev["ev_power_limit_entity"] == str(initial_limit)
                assert not reloaded.data["ev_telemetry"].get("command_exposure")
                assert reloaded.data["execution_audit"][-1]["result"] == "failed"
            elif interruption in {"restore_higher_limit", "restore_limit_error"}:
                assert result.reason == "ev_grid_projection_stale"
                assert result.rollback_succeeded and result.safe_state_confirmed
                assert not coordinator.overrides
                assert coordinator._ev_allocation_deadline is None
                assert not store.data["ownership"]
                assert not store.data["ev_grid_reservation"]["active"]
                assert hass.states.get("switch.charger").state == "off"
                assert hass.states.get("number.limit").state == str(float(initial_limit))
                assert [service for _, service, _ in calls] == (
                    ["set_value", "turn_off", "set_value"] if restoring_higher_limit else
                    ["set_value", "set_value", "turn_off", "set_value"]
                )
            elif interruption == "manual_limit_edit":
                assert result.reason == "ev_grid_projection_stale"
                assert result.rollback_succeeded and result.safe_state_confirmed
                assert not store.data["ownership"]
                assert not store.data["ev_grid_reservation"]["active"]
                assert hass.states.get("switch.charger").state == "off"
                assert hass.states.get("number.limit").state == "4"
                assert [service for _, service, _ in calls] == ["set_value", "turn_on", "turn_off"]
            elif interruption in {"power_feedback", "owned_power_feedback", "disconnected", "unknown_connection",
                                  "limit_transient_mismatch", "limit_transient_unavailable", "limit_competing_user",
                                  "limit_competing_parent"}:
                assert not result.applied and result.rollback_succeeded
                assert result.reason == {
                    "disconnected": "ev_not_connected", "unknown_connection": "ev_connected_state_unavailable",
                }.get(interruption, "ev_grid_projection_stale")
                assert calls == [
                    ("number", "set_value", {"entity_id": "number.limit", "value": 6.0}),
                    ("number", "set_value", {"entity_id": "number.limit", "value": 5.0}),
                ]
                assert hass.states.get("switch.charger").state == ("on" if previous_ownership else "off")
                assert coordinator._ev_allocation_deadline == previous_deadline
                assert not coordinator.overrides
                assert store.data["ownership"] == previous_ownership
                measured_draw_retained = interruption == "power_feedback"
                assert store.data["ev_grid_reservation"]["active"] == bool(previous_ownership or measured_draw_retained)
                if previous_ownership:
                    assert store.data["ev_grid_reservation"]["load_kw"] == 8.0
                if measured_draw_retained:
                    assert store.data["ev_grid_reservation"]["load_kw"] == 8.0
                    assert store.data["ev_grid_reservation"]["external_baseline"] is True
                    assert store.data["ev_grid_reservation"]["observed_limit_kw"] == 5.0
                if prior_exposure:
                    assert store.data["ev_telemetry"]["command_exposure"] == exposure
            else:
                assert calls == []
                assert result.reason == (
                    "ev_not_connected" if interruption == "disconnect_before_limit" else "ev_grid_projection_stale"
                )
                assert not result.applied and not result.command_sent
                assert coordinator.overrides == []
                assert coordinator._ev_allocation_deadline == previous_deadline
                assert bool(store.data["control_pause"]) is (interruption == "paused")
                if interruption == "shared_capacity":
                    assert reservations == {"second_ev": {"load_kw": 8.0, "limit_kw": 10.0}}
                assert store.data["execution_audit"][-1]["result"] == "rejected"
                reloaded = PlannerStore(hass, entry.entry_id)
                await reloaded.async_load()
                assert reloaded.data["ev_grid_reservation"]["active"] == bool(previous_ownership)
                if previous_ownership:
                    assert reloaded.data["ev_grid_reservation"]["load_kw"] == (
                        6.0 if manual_limit_during_save else previous_load
                    )
                if manual_limit_during_save:
                    assert reservations[entry.entry_id]["load_kw"] == 6.0
                    assert reloaded.data["ev_grid_reservation"]["observed_load_kw"] == 3.0
                    assert not reloaded.data["ev_grid_reservation"].get("confirmed_power_limit")
                    assert hass.states.get("switch.charger").state == "on"
                if interruption == "limit_invalid_before_dispatch":
                    assert hass.states.get("switch.charger").state == "on"
                assert reloaded.data["ownership"] == previous_ownership
                if prior_exposure:
                    assert reloaded.data["ev_telemetry"]["command_exposure"] == exposure
        finally:
            resume.set()
            start_finished.set()
            if not task.cancelled():
                await task
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("feedback", "retained_kw"), [
    ("measured_draw", 8), ("measured_draw_then_stop", 0), ("measured_draw_stale_stop", 8),
    ("live_enabled_cap", 5),
    ("live_enabled_cap_unchanged_zero", 5),
    ("queued_charging_cap", 8), ("idle_cap", 0), ("disconnected", 8), ("remapped_draw", 8),
    ("remapped_amp_limit", 3.75), ("remapped_after_limit_write", 3.75),
])
def test_rejected_first_charge_now_keeps_only_external_load(tmp_path, monkeypatch, feedback, retained_kw):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        if feedback == "measured_draw_stale_stop":
            hass.states.async_set(
                "switch.charger", "off", timestamp=(dt_util.utcnow() - timedelta(minutes=11)).timestamp(),
            )
        data = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected"}
        remapped = feedback.startswith("remapped_")
        sent_limit = feedback == "remapped_after_limit_write"
        amp_limit = sent_limit or feedback == "remapped_amp_limit"
        attrs = {"unit_of_measurement": "A" if amp_limit else "kW", "min": 1, "max": 9, "step": 1}
        hass.states.async_set("number.limit", 5 if sent_limit else 3, attrs)
        data["ev_power_entity"] = "sensor.ev_power"
        hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
        measured_feedback = feedback in {
            "measured_draw", "measured_draw_then_stop", "measured_draw_stale_stop", "disconnected", "remapped_draw",
        }
        if not measured_feedback:
            data["ev_power_limit_entity"] = "number.limit"

            async def write_limit(call):
                calls.append((call.domain, call.service, dict(call.data)))
                if sent_limit:
                    hass.states.async_set("number.limit", call.data["value"], attrs, context=call.context)
                    if call.data["value"] == 9:
                        remap_executor()
                        coordinator._mark_replan_requested()

            hass.services.async_register("number", "set_value", write_limit)
        if feedback == "queued_charging_cap":
            data[CONF_EV_CHARGING] = "binary_sensor.charging"
            hass.states.async_set("binary_sensor.charging", "off")
        entry = ConfigEntry(
            domain=DOMAIN, title="Charge Now external load", data=data,
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7, "grid_import_limit_kw": 10,
                     "ev_limit_min": 1, "ev_limit_max": 9, "ev_voltage": 250, "ev_phases": 3},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)

        def remap_executor():
            # The planner can install new executor inputs while a command is awaiting I/O.
            hass.states.async_set("switch.replacement_charger", "off")
            hass.states.async_set("sensor.replacement_power", 0, {"unit_of_measurement": "kW"})
            coordinator.executor.entry_data = {
                CONF_EV_CHARGER: "switch.replacement_charger", "ev_power_entity": "sensor.replacement_power",
            }
            coordinator.executor.options = {**coordinator.executor.options, "ev_voltage": 200, "ev_phases": 1}

        if sent_limit:
            hass.states.async_set("switch.charger", "on")
        plan, context = _command("ev")
        assert context is not None
        context.ev_target_soc_percent = 80
        saving, resume = asyncio.Event(), asyncio.Event()
        flush = store.async_flush

        async def refresh():
            coordinator._last_decision_context = context
            coordinator._last_decision_generation = coordinator._refresh_generation
            return plan

        async def hold_first_flush():
            await flush()
            if not saving.is_set():
                saving.set()
                await resume.wait()

        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(store, "async_flush", hold_first_flush)
        coordinator.async_start_listeners()
        task = asyncio.create_task(coordinator.async_charge_now())
        try:
            await asyncio.wait_for(saving.wait(), 2)
            assert store.data["ev_grid_reservation"]["load_kw"] == (6.75 if amp_limit else 7)
            assert not calls
            # A setpoint only contributes when the charger is actually enabled or
            # charging. Physical draw is evidence even without either signal.
            if measured_feedback:
                hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                if feedback == "disconnected":
                    hass.states.async_set("binary_sensor.connected", "off")
                elif feedback in {"measured_draw_then_stop", "measured_draw_stale_stop"}:
                    await asyncio.sleep(0)
                    hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
            elif feedback == "queued_charging_cap":
                hass.states.async_set("binary_sensor.charging", "on")

                @callback
                def publish(_event):
                    hass.states.async_set("number.limit", 8, attrs)
                    hass.states.async_set("number.limit", "unavailable", attrs)

                hass.bus.async_listen_once("ev_provider_push", publish)
                hass.bus.async_fire("ev_provider_push")
            elif not sent_limit:
                if feedback.startswith("live_enabled_cap") or amp_limit:
                    hass.states.async_set("switch.charger", "on")
                hass.states.async_set("number.limit", 5, attrs)
            await asyncio.sleep(0)
            if not sent_limit:
                if remapped:
                    remap_executor()
                coordinator._mark_replan_requested()
            resume.set()
            result = await task
            assert result.reason == "ev_grid_projection_stale"
            assert not result.applied and result.command_sent is sent_limit
            if sent_limit:
                assert result.rollback_succeeded
                assert hass.states.get("switch.charger").state == "on"
            assert not coordinator.overrides and coordinator._ev_allocation_deadline is None
            reloaded = PlannerStore(hass, entry.entry_id)
            await reloaded.async_load()
            assert not reloaded.data["ownership"]
            reservation = reloaded.data["ev_grid_reservation"]
            assert reservation["active"] is bool(retained_kw)
            if retained_kw:
                assert reservation["load_kw"] == retained_kw
                assert reservation["external_baseline"] is True
                assert reservation["retain_when_unloaded"] is True
            assert not coordinator.executor._has_ev_grid_reservation()
            await coordinator.executor.async_restore_device_control("ev", "ev_control_disabled")
            if remapped:
                assert reservation["external_topology"] == data
                if amp_limit:
                    assert reservation["external_power_options"] == {
                        "ev_limit_min": 1, "ev_limit_max": 9, "ev_voltage": 250, "ev_phases": 3,
                    }
                hass.states.async_set("sensor.replacement_power", 0, {"unit_of_measurement": "kW"})
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["load_kw"] == retained_kw
                hass.states.async_set("switch.charger", "off")
                hass.states.async_set("sensor.ev_power", 0, {
                    "unit_of_measurement": "kW", "sampled_at_utc": dt_util.utcnow().isoformat(),
                })
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"] == {"active": False}
            if feedback == "measured_draw":
                initial_report = datetime.fromisoformat(reservation["observed_load_at"])
                context = replace(context)
                saving.clear()
                resume.clear()
                retry = asyncio.create_task(coordinator.async_charge_now())
                try:
                    await asyncio.wait_for(saving.wait(), 2)
                    latest_positive = dt_util.utcnow()
                    hass.states.async_set("sensor.ev_power", 8, {
                        "unit_of_measurement": "kW", "sampled_at_utc": latest_positive.isoformat(),
                    })
                    await asyncio.sleep(0)
                    coordinator._mark_replan_requested()
                finally:
                    resume.set()
                    rejected_retry = await retry
                assert not rejected_retry.applied and not rejected_retry.command_sent
                stale_report = initial_report + (latest_positive - initial_report) / 2
                hass.states.async_set("sensor.ev_power", 0, {
                    "unit_of_measurement": "kW", "sampled_at_utc": stale_report.isoformat(),
                })
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["active"]
            if feedback == "measured_draw_stale_stop":
                # Both power changes arrived before the external reservation was
                # persisted. Core only reports this unchanged switch value.
                assert not coordinator.executor.external_ev_grid_stop_confirmed()
                hass.states.async_set("switch.charger", "off")
                await hass.async_block_till_done()
                released = PlannerStore(hass, entry.entry_id)
                await released.async_load()
                assert released.data["ev_grid_reservation"] == {"active": False}
                assert not released.data["ownership"]
            if feedback == "disconnected":
                # A second 8 kW report is fresh even though Core emits no state
                # change. An older zero sample cannot confirm stop after it.
                initial_report = datetime.fromisoformat(reservation["observed_load_at"])
                hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                latest_positive = hass.states.get("sensor.ev_power").last_reported
                stale_report = initial_report + (latest_positive - initial_report) / 2
                assert initial_report < stale_report < latest_positive
                await hass.async_block_till_done()
                hass.states.async_set("sensor.ev_power", 0, {
                    "unit_of_measurement": "kW", "sampled_at_utc": stale_report.isoformat(),
                })
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["active"]
            if feedback in {"measured_draw", "disconnected"} or feedback.startswith("live_enabled_cap"):
                if feedback.startswith("live_enabled_cap"):
                    # The external 5 kW cap is lower than the configured 7 kW rate.
                    # Stop before Home Assistant receives a same-value zero report.
                    hass.states.async_set("switch.charger", "off")
                    await hass.async_block_till_done()
                    assert store.data["ev_grid_reservation"]["load_kw"] == 5
                stopped_power = {"unit_of_measurement": "kW"}
                if feedback == "live_enabled_cap":
                    stopped_power["sampled_at_utc"] = dt_util.utcnow().isoformat()
                hass.states.async_set("sensor.ev_power", 0, stopped_power)
                await hass.async_block_till_done()
                released = PlannerStore(hass, entry.entry_id)
                await released.async_load()
                assert released.data["ev_grid_reservation"] == {"active": False}
                assert not released.data["ownership"]
                assert hass.states.get("binary_sensor.connected").state == (
                    "off" if feedback == "disconnected" else "on"
                )
            if sent_limit:
                assert [(domain, service, data["value"]) for domain, service, data in calls] == [
                    ("number", "set_value", 9), ("number", "set_value", 5),
                ]
            else:
                assert not calls
        finally:
            resume.set()
            await task
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("last_signal", "mapped_signals", "intervening"), [
    ("control", "all", None), ("charging", "all", None), ("power", "all", "restart"),
    ("power", "all", "teardown"), ("power", "all", "removed"),
    ("control", "control", None), ("charging", "charging", None), ("power", "power", None),
    ("charging", "button", None),
])
def test_confirmed_external_ev_stop_releases_shared_grid_capacity(tmp_path, last_signal, mapped_signals, intervening):
    """Retire observed external load through HA state feedback while still plugged in."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "on")
        hass.states.async_set("binary_sensor.charging", "on")
        power_attributes = {"unit_of_measurement": "kW"}
        hass.states.async_set("sensor.ev_power", 8, power_attributes)
        hass.states.async_set("number.limit", 8, {
            **power_attributes, "min": 1, "max": 9, "step": 1,
        })
        stop_entities = {
            "control": (CONF_EV_CHARGER, "switch.charger", "off", {}),
            "charging": (CONF_EV_CHARGING, "binary_sensor.charging", "off", {}),
            "power": ("ev_power_entity", "sensor.ev_power", 0, power_attributes),
        }
        feedback = {
            signal: config for signal, config in stop_entities.items() if mapped_signals in {signal, "all"}
        }
        if mapped_signals == "button":
            feedback["control"] = (CONF_EV_CHARGER, "button.charger", "unknown", {})
            feedback["charging"] = stop_entities["charging"]
        entry = ConfigEntry(
            domain=DOMAIN, title="External EV stop",
            data={"ev_power_limit_entity": "number.limit",
                  **({CONF_EV_CONNECTED: "binary_sensor.connected"} if mapped_signals == "all" else {}),
                  **{key: entity_id for key, entity_id, _, _ in feedback.values()}},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7, "grid_import_limit_kw": 10,
                     "ev_limit_min": 1, "ev_limit_max": 9},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        reservations = coordinator.executor._ev_grid_reservations()
        now = dt_util.utcnow()
        reservations[entry.entry_id] = {
            "load_kw": 8, "limit_kw": 10, "reserved_at": (now - timedelta(seconds=30)).isoformat(),
        }
        coordinator.executor._retain_external_ev_grid_reservation()
        await coordinator.executor.async_persist_ev_grid_reservation()
        _, context = _command("ev")
        assert context is not None
        coordinator.async_start_listeners()
        try:
            for seconds in (10, 15):
                # A late positive sample must not move the ordering boundary back.
                hass.states.async_set("sensor.ev_power", 8, {
                    **power_attributes, "sampled_at_utc": (now - timedelta(seconds=seconds)).isoformat(),
                })
                await hass.async_block_till_done()
            for signal, (_, entity_id, value, attributes) in feedback.items():
                if signal != last_signal:
                    hass.states.async_set(entity_id, value, attributes)
                    await hass.async_block_till_done()
            assert store.data["ev_grid_reservation"]["active"]
            assert ev_grid_headroom_reason(
                reservations, "second_ev", context, 7, 10,
            ) == "multi_ev_grid_import_limit_exceeded"
            _, entity_id, value, attributes = feedback[last_signal]
            unavailable = "faulted" if last_signal == "charging" else "unavailable"
            hass.states.async_set(entity_id, unavailable, attributes)
            await hass.async_block_till_done()
            assert store.data["ev_grid_reservation"]["active"]
            # HA just received this value, but its source sample was superseded.
            stale_age = (
                timedelta(seconds=12) if last_signal == "power"
                else timedelta(minutes=11) if mapped_signals == "all"
                else timedelta(seconds=35)
            )
            hass.states.async_set(entity_id, value, {
                **attributes, "sampled_at_utc": (now - stale_age).isoformat(),
            })
            await hass.async_block_till_done()
            assert store.data["ev_grid_reservation"]["active"]
            if intervening:
                record = reservations[entry.entry_id]
                reserved_at = record.pop("reserved_at")
                hass.states.async_set(entity_id, value, attributes)
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["active"]
                record["reserved_at"] = reserved_at
                hass.states.async_set(entity_id, value, {**attributes, "sampled_at_utc": "invalid"})
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["active"]
                async with coordinator._command_lock:
                    hass.states.async_set(entity_id, value, attributes)
                    await asyncio.sleep(0)
                    if intervening == "restart":
                        hass.states.async_set(entity_id, 8, attributes)
                    elif intervening == "removed":
                        reservations.pop(entry.entry_id)
                    else:
                        coordinator._tearing_down = True
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["active"]
                reservations[entry.entry_id] = record
                coordinator._tearing_down = False
            hass.states.async_set(entity_id, value, attributes)
            await hass.async_block_till_done()
            reloaded = PlannerStore(hass, entry.entry_id)
            await reloaded.async_load()
            if mapped_signals == "power":
                assert reloaded.data["ev_grid_reservation"]["active"]
                assert ev_grid_headroom_reason(
                    reservations, "second_ev", context, 7, 10,
                ) == "multi_ev_grid_import_limit_exceeded"
            else:
                assert reloaded.data["ev_grid_reservation"] == {"active": False}
                assert ev_grid_headroom_reason(reservations, "second_ev", context, 7, 10) is None
            assert hass.states.get("binary_sensor.connected").state == "on"
            assert not reloaded.data["ownership"]
            assert not calls
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_external_ev_stop_without_an_entry_has_no_authority(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        try:
            assert not Executor(store).external_ev_grid_stop_confirmed()
            assert not Executor(store, hass=hass).external_ev_grid_stop_confirmed()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_releasing_ev_ownership_after_remap_retains_original_capacity_inputs(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "on")
        hass.states.async_set("sensor.orig_power", 0, {"unit_of_measurement": "kW"})
        limit_attrs = {"unit_of_measurement": "A", "min": 1, "max": 16, "step": 1}
        hass.states.async_set("number.orig_limit", 5, limit_attrs)
        store = PlannerStore(hass, "runtime")
        original = _executor(hass, store)
        original.entry_data.update(ev_power_limit_entity="number.orig_limit", ev_power_entity="sensor.orig_power")
        original.options.update(ev_limit_min=1, ev_limit_max=16, ev_voltage=250, ev_phases=3)
        _arm(store, original)
        plan, context = _command("ev")
        assert context is not None
        peer_context = replace(context, slots=[replace(context.slots[0], projected_ev_load_kw=1)])
        try:
            await original.async_evaluate(plan, context)
            assert store.data["ownership"]["ev_smart_charging_state"][CONF_EV_CHARGER] == "on"

            store = PlannerStore(hass, "runtime")
            await store.async_load()
            replacement = _executor(hass, store)
            replacement.entry_data.update({
                CONF_EV_CHARGER: "switch.replacement", CONF_EV_CONNECTED: "binary_sensor.new_connected",
                "ev_power_limit_entity": "number.replacement_limit", "ev_power_entity": "sensor.replacement_power",
            })
            replacement.options.update(ev_limit_min=1, ev_limit_max=8, ev_voltage=200, ev_phases=1)
            await replacement.async_release_ev_policy()
            assert not store.data["ownership"]

            assert replacement.external_ev_watch_inputs() == (
                "switch.charger", None, "sensor.orig_power", "number.orig_limit", "binary_sensor.connected",
            )
            hass.states.async_set("sensor.orig_power", "unavailable", {"unit_of_measurement": "kW"})
            hass.states.async_set("binary_sensor.connected", "off")
            hass.states.async_set("number.orig_limit", 12, limit_attrs)
            assert not replacement.sync_ev_grid_reservation()
            reservations = replacement._ev_grid_reservations()
            assert reservations is not None
            assert ev_grid_headroom_reason(reservations, "second_ev", peer_context, 1, 10) is None
            hass.states.async_set("binary_sensor.connected", "on")
            assert replacement.sync_ev_grid_reservation()
            await replacement.async_persist_ev_grid_reservation()
            assert store.data["ev_grid_reservation"]["load_kw"] == 9
            assert ev_grid_headroom_reason(
                reservations, "second_ev", peer_context, 1, 10,
            ) == "multi_ev_grid_import_limit_exceeded"
            assert not calls
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("case", ["original_kw", "original_amps", "unknown_settings", "corrupt_settings", "legacy"])
def test_reloaded_external_ev_reservation_uses_original_stop_evidence(tmp_path, case):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        legacy = case == "legacy"
        original_amps = case in {"original_amps", "corrupt_settings"}
        attrs = {"unit_of_measurement": "kW"}
        limit_attrs = {**attrs, "min": 1, "max": 16, "step": 1}
        original_limit_attrs = {**limit_attrs, "unit_of_measurement": "A" if original_amps else "kW"}
        hass.states.async_set("switch.old_charger", "off")
        hass.states.async_set("sensor.old_ev_power", "unavailable", attrs)
        hass.states.async_set("number.old_ev_limit", "unavailable", original_limit_attrs)
        hass.states.async_set("number.ev_limit", 7, limit_attrs)
        hass.states.async_set("binary_sensor.old_connected", "on")

        def entry_for(control, connected, power, limit, *, original):
            return ConfigEntry(
                entry_id="external_ev_reconfigure", domain=DOMAIN, title="External EV remap",
                data={CONF_EV_CHARGER: control, CONF_EV_CONNECTED: connected,
                      "ev_power_entity": power, "ev_power_limit_entity": limit},
                options={**DEFAULT_OPTIONS, "grid_import_limit_kw": 10, "ev_limit_min": 1,
                         "ev_charge_rate_kw": 7 if original else 9, "ev_limit_max": 16 if original else 5,
                         "ev_voltage": 250 if original else 200, "ev_phases": 3 if original else 1},
                source="user", unique_id=None, version=5, minor_version=1,
                discovery_keys=MappingProxyType({}), subentries_data=[],
            )

        original = entry_for(
            "switch.old_charger", "binary_sensor.old_connected", "sensor.old_ev_power", "number.old_ev_limit",
            original=True,
        )
        replacement = entry_for(
            "switch.charger", "binary_sensor.connected", "sensor.ev_power", "number.ev_limit", original=False,
        )
        store = PlannerStore(hass, original.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, original, store)
        executor = coordinator.executor
        reservations = executor._ev_grid_reservations()
        reservations[original.entry_id] = {
            "load_kw": 2, "observed_load_kw": 2, "limit_kw": 10, "reserved_at": dt_util.utcnow().isoformat(),
        }
        if case == "unknown_settings":
            replacement_coordinator = EnergyPlannerCoordinator(hass, replacement, store)
            replacement_coordinator.executor._retain_external_ev_grid_reservation(coordinator.entry_data)
        else:
            executor._retain_external_ev_grid_reservation()
        if legacy:
            reservations[original.entry_id].pop("external_topology")
            reservations[original.entry_id].pop("external_power_options")
        await executor.async_persist_ev_grid_reservation()
        await store.async_flush()

        # Drop shared memory and rehydrate the Store in a replacement coordinator.
        reservations.clear()
        new_store = PlannerStore(hass, replacement.entry_id)
        await new_store.async_load()
        decoded = new_store.data
        if case == "corrupt_settings":
            # Inject at the decoded-input boundary because HA's serializer
            # rejects integers outside its supported range.
            reserved = decoded["ev_grid_reservation"]
            invalid_options = {**reserved["external_power_options"], "ev_voltage": 10**400}
            decoded = {**decoded, "ev_grid_reservation": {**reserved, "external_power_options": invalid_options}}
        _rehydrate_ev_grid_reservation(hass, replacement, decoded)
        assert reservations[original.entry_id]["load_kw"] == (9 if legacy else 2)
        coordinator = EnergyPlannerCoordinator(hass, replacement, new_store)
        coordinator.async_start_listeners()
        try:
            hass.states.async_set("switch.charger", "off")
            hass.states.async_set("sensor.ev_power", 0, attrs)
            await hass.async_block_till_done()
            assert new_store.data["ev_grid_reservation"]["active"]
            assert not calls
            await coordinator.executor.async_release_ev_policy()

            _, context = _command("ev")
            assert context is not None
            if not legacy:
                assert ev_grid_headroom_reason(reservations, "second_ev", context, 7, 10) is None
                result = await coordinator.executor.async_manual_ev_charging(True, context)
                assert not result.applied and result.reason == "ev_external_reservation_topology_changed"
                hass.states.async_set("switch.charger", "on")
                await hass.async_block_till_done()
                result = await coordinator.executor.async_manual_ev_charging(False, context)
                assert result.applied
                assert calls == [("switch", "turn_off", {"entity_id": "switch.charger"})]
                calls.clear()
                assert new_store.data["ev_grid_reservation"]["active"]
                await coordinator.executor.async_release_ev_policy(unplugged=True)
                assert new_store.data["ev_grid_reservation"]["active"]

                hass.states.async_set("number.old_ev_limit", 10, original_limit_attrs)
                await hass.async_block_till_done()
                assert reservations[original.entry_id]["load_kw"] == 2
                hass.states.async_set("binary_sensor.old_connected", "off")
                hass.states.async_set("switch.old_charger", "on")
                hass.states.async_set("number.old_ev_limit", 11, original_limit_attrs)
                await hass.async_block_till_done()
                assert reservations[original.entry_id]["load_kw"] == 2
                hass.states.async_set("number.old_ev_limit", "unavailable", original_limit_attrs)
                hass.states.async_set("binary_sensor.old_connected", "on")
                hass.states.async_set("sensor.old_ev_power", 3, attrs)
                await hass.async_block_till_done()
                assert new_store.data["ev_grid_reservation"]["load_kw"] == 3
                hass.states.async_set("sensor.old_ev_power", "unavailable", attrs)
                hass.states.async_set("number.old_ev_limit", 8 if original_amps else 4, original_limit_attrs)
                await hass.async_block_till_done()
                hass.states.async_set("number.ev_limit", 15, limit_attrs)
                await hass.async_block_till_done()
                assert new_store.data["ev_grid_reservation"]["load_kw"] == {
                    "original_kw": 4, "original_amps": 6, "unknown_settings": 3, "corrupt_settings": 3,
                }[case]

            assert ev_grid_headroom_reason(
                reservations, "second_ev", context, 7, 10,
            ) == "multi_ev_grid_import_limit_exceeded"
            hass.states.async_set("switch.old_charger", "off")
            hass.states.async_set("sensor.old_ev_power", 0, attrs)
            await hass.async_block_till_done()
            if legacy:
                assert new_store.data["ev_grid_reservation"]["active"]
            else:
                assert new_store.data["ev_grid_reservation"] == {"active": False}
                assert ev_grid_headroom_reason(reservations, "second_ev", context, 7, 10) is None
            assert not new_store.data["ownership"]
            assert not calls
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("feedback", ["power_at_attach", "cap_at_attach", "unchanged_power_report"])
def test_external_ev_watch_accounts_for_already_published_load(tmp_path, monkeypatch, feedback):
    """Fresh original-charger feedback still raises capacity after a remap or reload."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        now = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        entry = ConfigEntry(
            domain=DOMAIN, title="External EV watch after reload",
            data={CONF_EV_CHARGER: "switch.replacement", "ev_power_entity": "sensor.replacement_power"},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 9, "grid_import_limit_kw": 10},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        previous = PlannerStore(hass, entry.entry_id)
        await previous.async_save_ev_grid_reservation({
            "active": True, "external_baseline": True, "load_kw": 3, "observed_load_kw": 3,
            "limit_kw": 10, "reserved_at": now.isoformat(),
            "external_topology": {
                CONF_EV_CHARGER: "switch.original", "ev_power_entity": "sensor.original_power",
                "ev_power_limit_entity": "number.original_cap",
            },
            "external_power_options": {"ev_limit_min": 1, "ev_limit_max": 10},
        })
        store = PlannerStore(hass, entry.entry_id)
        await store.async_load()
        _rehydrate_ev_grid_reservation(hass, entry, store.data)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        requests = []

        async def request_refresh():
            requests.append(coordinator._pending_refresh_trigger)

        coordinator.async_request_refresh = request_refresh
        _, context = _command("ev")
        assert context is not None
        reservations = coordinator.executor._ev_grid_reservations()
        assert reservations[entry.entry_id]["load_kw"] == 3
        hass.states.async_set("switch.original", "on")
        hass.states.async_set("sensor.replacement_power", 9, {"unit_of_measurement": "kW"})
        power_attributes = {"unit_of_measurement": "kW"}
        if feedback == "cap_at_attach":
            hass.states.async_set("number.original_cap", 7, {**power_attributes, "min": 1, "max": 10, "step": 1})
        else:
            sampled_at = now + timedelta(seconds=1) if feedback == "unchanged_power_report" else now
            power_attributes["sampled_at_utc"] = sampled_at.isoformat()
            hass.states.async_set("sensor.original_power", 7, power_attributes)
        coordinator.async_start_listeners()
        try:
            if feedback == "unchanged_power_report":
                # The timestamp was in the future on attachment. Core now
                # delivers the unchanged State as a report when it is usable.
                assert reservations[entry.entry_id]["load_kw"] == 3
                now += timedelta(seconds=1)
                hass.states.async_set("sensor.original_power", 7, power_attributes)
            assert reservations[entry.entry_id]["load_kw"] == 7
            assert ev_grid_headroom_reason(
                reservations, "another_charger", context, 7, 10,
            ) == "multi_ev_grid_import_limit_exceeded"
            await hass.async_block_till_done()
            restored = PlannerStore(hass, entry.entry_id)
            await restored.async_load()
            assert restored.data["ev_grid_reservation"]["load_kw"] == 7
            assert not restored.data["ownership"]
            assert requests == ["ev_capacity_increased"]
            now += timedelta(seconds=1)
            if feedback != "cap_at_attach":
                hass.states.async_set("sensor.original_power", 7, power_attributes)
                await hass.async_block_till_done()
                assert requests == ["ev_capacity_increased"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("activation", ["switch", "charging", "connected"])
def test_external_ev_watch_adopts_cap_when_original_charger_activates(tmp_path, monkeypatch, activation):
    """Original activity makes a previously idle cap usable after a control remap."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        now = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        entry = ConfigEntry(
            domain=DOMAIN, title="External EV activation after remap",
            data={CONF_EV_CHARGER: "switch.replacement"},
            options={**DEFAULT_OPTIONS, "grid_import_limit_kw": 10},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ev_grid_reservation({
            "active": True, "external_baseline": True, "load_kw": 3, "observed_load_kw": 3,
            "limit_kw": 10, "reserved_at": now.isoformat(),
            "external_topology": {
                CONF_EV_CHARGER: "switch.original", CONF_EV_CHARGING: "binary_sensor.original_charging",
                CONF_EV_CONNECTED: "binary_sensor.original_connected", "ev_power_entity": "sensor.original_power",
                "ev_power_limit_entity": "number.original_cap",
            },
            "external_power_options": {"ev_limit_min": 1, "ev_limit_max": 10},
        })
        _rehydrate_ev_grid_reservation(hass, entry, store.data)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        reservations = coordinator.executor._ev_grid_reservations()
        _, context = _command("ev")
        assert context is not None
        context.slots[0] = replace(context.slots[0], projected_ev_load_kw=6)
        requests, writes = [], []
        save = store._store.async_save

        async def request_refresh():
            requests.append(coordinator._pending_refresh_trigger)

        async def record_save(data):
            writes.append(data["ev_grid_reservation"]["load_kw"])
            await save(data)

        coordinator.async_request_refresh = request_refresh
        monkeypatch.setattr(store._store, "async_save", record_save)
        evidence_ids = {
            "switch": "switch.original",
            "charging": "binary_sensor.original_charging",
            "connected": "binary_sensor.original_connected",
        }
        initial = {
            "switch": "on" if activation == "connected" else "off",
            "charging": "on" if activation == "connected" else "unknown",
            "connected": "off" if activation == "connected" else "on",
        }
        for field, entity_id in evidence_ids.items():
            hass.states.async_set(entity_id, initial[field])
        coordinator.async_start_listeners()
        try:
            now += timedelta(seconds=1)
            hass.states.async_set("number.original_cap", 7, {
                "unit_of_measurement": "kW", "min": 1, "max": 10, "step": 1,
            })
            hass.states.async_set(evidence_ids[activation], initial[activation])
            hass.states.async_set("switch.replacement", "on")
            await hass.async_block_till_done()
            assert reservations[entry.entry_id]["load_kw"] == 3
            assert ev_grid_headroom_reason(reservations, "another_charger", context, 6, 10) is None
            assert not requests and not writes

            now += timedelta(seconds=1)
            hass.states.async_set(evidence_ids[activation], "on")
            await hass.async_block_till_done()
            assert reservations[entry.entry_id]["load_kw"] == 7
            assert ev_grid_headroom_reason(
                reservations, "another_charger", context, 6, 10,
            ) == "multi_ev_grid_import_limit_exceeded"
            assert requests == ["ev_capacity_increased"]
            assert writes == [7]
            now += timedelta(seconds=1)
            hass.states.async_set(evidence_ids[activation], "on")
            await hass.async_block_till_done()
            assert requests == ["ev_capacity_increased"] and writes == [7]
            assert not store.data["ownership"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("initial_power", [3, 7])
def test_entry_accounts_for_external_ev_load_before_first_refresh(tmp_path, monkeypatch, initial_power):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        entry = ConfigEntry(
            domain=DOMAIN, title="External EV entry setup",
            data={CONF_EV_CHARGER: "switch.replacement"},
            options={**DEFAULT_OPTIONS, "grid_import_limit_kw": 10},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        power_id = "sensor.original_power"
        attrs = {"unit_of_measurement": "kW"}
        previous = PlannerStore(hass, entry.entry_id)
        await previous.async_save_ev_grid_reservation({
            "active": True, "external_baseline": True, "load_kw": 3, "observed_load_kw": 3,
            "limit_kw": 10, "reserved_at": dt_util.utcnow().isoformat(),
            "external_topology": {CONF_EV_CHARGER: "switch.original", "ev_power_entity": power_id},
        })
        hass.states.async_set("switch.original", "on")
        hass.states.async_set(power_id, initial_power, attrs)
        entered = asyncio.Event()
        resume = asyncio.Event()

        async def first_refresh(_coordinator):
            entered.set()
            await resume.wait()

        async def skip_platform_setup(_entry, _platforms):
            pass

        async def skip_requested_refresh(_coordinator):
            pass

        monkeypatch.setattr(EnergyPlannerCoordinator, "async_config_entry_first_refresh", first_refresh)
        monkeypatch.setattr(EnergyPlannerCoordinator, "async_request_refresh", skip_requested_refresh)
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", skip_platform_setup)
        monkeypatch.setattr("custom_components.ha_energy_planner._async_sync_planner_device", lambda *_args: None)
        task = asyncio.create_task(async_setup_entry(hass, entry))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            reservations = entry.runtime_data.executor._ev_grid_reservations()
            assert reservations[entry.entry_id]["load_kw"] == initial_power
            persisted = PlannerStore(hass, entry.entry_id)
            await persisted.async_load()
            assert persisted.data["ev_grid_reservation"]["load_kw"] == initial_power
            # A higher reading during the first refresh must also be retained
            # once the saved charger mapping's listeners are attached.
            hass.states.async_set(power_id, 8, attrs)
            resume.set()
            assert await task
            assert reservations[entry.entry_id]["load_kw"] == 8
            await hass.async_block_till_done()
            persisted = PlannerStore(hass, entry.entry_id)
            await persisted.async_load()
            assert persisted.data["ev_grid_reservation"]["load_kw"] == 8
            assert not persisted.data["ownership"]
        finally:
            resume.set()
            await task
            if (coordinator := getattr(entry, "runtime_data", None)) is not None:
                await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("owned", [False, True])
def test_charge_now_releases_only_provisional_capacity_when_store_flush_fails(tmp_path, monkeypatch, owned):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        entry = ConfigEntry(
            domain=DOMAIN, title="Charge now Store failure",
            data={CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected"},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7, "grid_import_limit_kw": 10},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        plan, context = _command("ev")
        assert context is not None
        context.ev_target_soc_percent = 80
        ownership = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}} if owned else {}
        previous_deadline = None
        if owned:
            await store.async_save_ownership(ownership)
            coordinator.executor._ev_grid_reservations()[entry.entry_id] = {"load_kw": 3, "limit_kw": 10}
            await coordinator.executor.async_persist_ev_grid_reservation()
            previous_deadline = dt_util.utcnow() + timedelta(minutes=1)
            coordinator._schedule_ev_allocation_deadline(previous_deadline)

        async def refresh():
            coordinator._last_decision_context = context
            coordinator._last_decision_generation = coordinator._refresh_generation
            return plan

        flush = store.async_flush
        failed = False

        async def lose_acknowledgement():
            nonlocal failed
            await flush()
            if not failed:
                failed = True
                raise OSError("provisional write acknowledgement lost")

        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(store, "async_flush", lose_acknowledgement)
        try:
            with pytest.raises(OSError, match="provisional write acknowledgement lost"):
                await coordinator.async_charge_now()
            assert not calls
            assert coordinator._ev_allocation_deadline == previous_deadline
            reloaded = PlannerStore(hass, entry.entry_id)
            await reloaded.async_load()
            assert reloaded.data["ownership"] == ownership
            assert reloaded.data["ev_grid_reservation"]["active"] is owned
            if owned:
                assert reloaded.data["ev_grid_reservation"]["load_kw"] == 3
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("base", [240, 360])
def test_extended_preconditioning_options_survive_home_assistant_restart(tmp_path, base):
    """Use HA's entry persistence rather than writing integration or Core storage ourselves."""
    async def prepare():
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        return hass

    async def run():
        hass = await prepare()
        entry = ConfigEntry(
            domain=DOMAIN, title="Lead persistence", data={},
            options={"hvac_precondition_lead_minutes": 120, "hvac_precondition_max_lead_minutes": 240,
                     "ev_charge_rate_kw": 5.5},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        hass.config_entries._entries[entry.entry_id] = entry
        try:
            flow = OptionsFlow(entry)
            flow.hass = hass
            climate = next(validator for marker, validator in flow._settings_schema().schema.items()
                           if marker.schema == INPUT_STEP_CLIMATE)
            submitted = climate({"hvac_precondition_lead_minutes": base,
                                 "hvac_precondition_max_lead_minutes": base})
            result = await flow.async_step_settings({INPUT_STEP_CLIMATE: submitted})
            assert result["type"] == "create_entry"
            hass.config_entries.async_update_entry(entry, options=result["data"])
        finally:
            await hass.async_stop(force=True)
        restarted = await prepare()
        try:
            restored = restarted.config_entries.async_get_entry(entry.entry_id)
            assert restored is not None
            assert restored.options["hvac_precondition_lead_minutes"] == base
            assert restored.options["hvac_precondition_max_lead_minutes"] == base
            assert restored.options["ev_charge_rate_kw"] == 5.5
            flow = OptionsFlow(restored)
            climate = next(validator for marker, validator in flow._settings_schema().schema.items()
                           if marker.schema == INPUT_STEP_CLIMATE)
            assert climate({})["hvac_precondition_lead_minutes"] == base
        finally:
            await restarted.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("entity", ["climate.home", "climate.zone", "switch.zone"])
@pytest.mark.parametrize("feedback", ["direct", "intermediate", "user_loss"])
@pytest.mark.parametrize("inherited", [False, True])
def test_real_user_stop_after_outage_aborts_pending_hvac(tmp_path, monkeypatch, entity, feedback, inherited):
    """Real HA contexts distinguish a user stop from source recovery."""
    from unittest.mock import AsyncMock

    from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter, _HVACManualOverrideError

    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        mode = "on" if entity.startswith("switch.") else "heat"
        attrs = {} if entity.startswith("switch.") else {"temperature": 21}
        baseline_context = Context(user_id="earlier-operator")
        user_context = baseline_context if inherited else Context(user_id="operator")
        hass.states.async_set(entity, mode, attrs, context=baseline_context)
        hass.state = CoreState.running
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.entry_data[CONF_CLIMATE_ZONES] = ["climate.zone", "switch.zone"]
        entry = ConfigEntry(
            domain=DOMAIN, title="Outage user control", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        manual = AsyncMock()
        monkeypatch.setattr(coordinator, "_async_handle_manual_hvac_change", manual)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        pending = {"hvac_mode": "heat", "target_temperature": 21, "enable_zones": True}
        coordinator.executor.pending_hvac_desired_state = pending
        adapter = DaikinHVACAdapter(hass, coordinator.entry_data)
        coordinator.executor._configure_pending_hvac_adapter(adapter, pending)
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        try:
            hass.states.async_set(
                entity, "unknown" if feedback == "intermediate" else "unavailable", attrs,
                context=user_context if feedback == "user_loss" else Context(),
            )
            await hass.async_block_till_done()
            adapter._raise_if_manual_override_requested()
            manual.assert_not_awaited()
            if feedback == "intermediate":
                hass.states.async_set(entity, "unavailable", attrs, context=user_context)
                await hass.async_block_till_done()
                adapter._raise_if_manual_override_requested()
                manual.assert_not_awaited()
            hass.states.async_set(entity, "off", attrs, context=user_context)
            await hass.async_block_till_done()
            if inherited:
                adapter._raise_if_manual_override_requested()
                manual.assert_not_awaited()
                assert store.data["climate_audit"][-1]["stage"] == "availability_feedback"
                assert calls == []
                return
            with pytest.raises(_HVACManualOverrideError):
                adapter._raise_if_manual_override_requested()
            main = entity == "climate.home"
            manual.assert_awaited_once_with(
                "daikin_state_changed" if main else "climate_zone_changed",
                **({"preserve_main_state": True} if main else {"preserve_zone_entity_id": entity}),
            )
            assert store.data["climate_audit"][-1]["stage"] == "manual_recovery"
            assert calls == []
        finally:
            coordinator.executor.pending_hvac_desired_state = None
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("intervention", ["none", "zone_target", "zone_mode", "head_target", "automation"])
def test_owned_release_recalls_disabled_zone_without_manual_override(tmp_path, monkeypatch, intervention):
    """Replay mode recall through real HA events, adapter dispatch and executor release."""
    from copy import deepcopy
    from unittest.mock import AsyncMock

    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        hass.states.async_set("climate.home", "cool", {"temperature": 17, "min_temp": 7, "max_temp": 35})
        hass.states.async_set("climate.bedrooms", "off", {"temperature": 19})
        hass.states.async_set("climate.main_zone", "cool", {"temperature": 17})
        hass.states.async_set("switch.zone", "on")
        hass.states.async_set("automation.climate", "off")
        hass.state = CoreState.running
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.entry_data[CONF_CLIMATE_ZONES] = ["climate.bedrooms", "climate.main_zone", "switch.zone"]
        entry = ConfigEntry(
            domain=DOMAIN, title="Disabled zone restoration feedback", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        baseline = {"phase": "restoration", "lifecycle_id": "owned-release",
                    "main_state": {"hvac_mode": "off", "target_temperature": 19,
                                   "rollback_active_hvac_mode": "heat", "rollback_hvac_mode_changed": True},
                    "zone_states": {"switch.zone": "off", "climate.main_zone": {"target_temperature": 17}}}
        await store.async_save_ownership({"hvac_control": deepcopy(baseline),
                                          "climate_automations": {"automation.climate": "on"}})
        manual = AsyncMock()
        monkeypatch.setattr(coordinator, "_async_handle_manual_hvac_change", manual)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        captured = []

        async def actuate(call):
            entity = call.data["entity_id"]
            calls.append((call.domain, call.service, dict(call.data)))
            old = hass.states.get(entity)
            attrs = dict(old.attributes)
            mode = old.state
            if call.service == "set_hvac_mode":
                mode, attrs = call.data["hvac_mode"], {"temperature": 19}
            elif call.service == "set_temperature":
                attrs["temperature"] = call.data["temperature"]
            elif call.service == "turn_off":
                mode = "off"
            elif call.service == "turn_on":
                mode = "on"
            hass.states.async_set(entity, mode, attrs, context=call.context)
            if entity == "climate.home" and call.service == "set_hvac_mode":
                # Deliver head first, then a separate unattributed vendor poll.
                await hass.async_block_till_done()
                hass.states.async_set("climate.main_zone", "heat", {"temperature": 19})
                hass.states.async_set("climate.bedrooms", "off", {"temperature": 20})
                await hass.async_block_till_done()
                manual.assert_not_awaited()
                pending = coordinator.executor.pending_hvac_desired_state
                command = next(item for item in pending["feedback_commands"] if item["context_id"] == call.context.id)
                captured.append(deepcopy(command))
                assert command["restoration"] is True
                assert command["observed"] == {"hvac_mode": "cool", "target_temperature": 17}
                assert command["coupled_zones"]["climate.bedrooms"]["target_temperature"] == 19
                control = store.data["ownership"]["hvac_control"]
                assert control["main_state"] == baseline["main_state"]
                assert control["zone_states"] == baseline["zone_states"]
                if intervention != "none":
                    entity = "climate.home" if intervention == "head_target" else "climate.bedrooms"
                    hass.states.async_set(entity, "cool" if intervention == "zone_mode" else
                                          "heat" if intervention == "head_target" else "off",
                                          {"temperature": 21}, context=Context(
                                              parent_id="external-automation" if intervention == "automation" else None,
                                              user_id=None if intervention == "automation" else "operator"))
                    await hass.async_block_till_done()
            if entity == "climate.home" and call.service == "turn_off":
                hass.states.async_set("climate.bedrooms", "off", {"temperature": None})
                hass.states.async_set("climate.main_zone", "off", {"temperature": None})

        for domain, services in {"climate": ("set_hvac_mode", "set_temperature", "turn_off"),
                                 "switch": ("turn_off",), "automation": ("turn_on",)}.items():
            for service in services:
                hass.services.async_register(domain, service, actuate)
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        try:
            outcome = await coordinator.executor.async_release_hvac_control(
                "hvac_expensive_period_ended", plan_id="scheduled-release")
            await hass.async_block_till_done()
            assert captured, (outcome, calls, store.data.get("climate_audit"))
            assert not coordinator.overrides
            assert coordinator.executor.pending_hvac_desired_state is None
            if intervention == "none":
                manual.assert_not_awaited()
                assert outcome.result == OutcomeResult.RESTORED
                assert "hvac_control" not in store.data["ownership"]
                assert hass.states.get("climate.home").state == "off"
                assert hass.states.get("switch.zone").state == "off"
                assert hass.states.get("automation.climate").state == "on"
                assert not any(row["stage"] == "unmatched_feedback" for row in store.data["climate_audit"])
                # A later safe off request must still pass the executor gate.
                plan, _ = _command("hvac")
                plan.actions[0].desired_state = {"hvac_mode": "off"}
                await coordinator.executor.async_evaluate(plan)
                assert store.data["execution_audit"][-1]["reason"] != "manual_hvac_override_active"
            else:
                main = intervention == "head_target"
                manual.assert_awaited_once_with(
                    "daikin_state_changed" if main else "climate_zone_changed",
                    **({"preserve_main_state": True} if main else {"preserve_zone_entity_id": "climate.bedrooms"}),
                )
                assert not coordinator.executor.hvac_feedback_commands or all(
                    item["context_id"] != captured[0]["context_id"]
                    for item in coordinator.executor.hvac_feedback_commands)
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("entity", ["climate.home", "climate.bedrooms"])
def test_off_start_owned_mapping_keeps_manual_feedback_after_reconfiguration(tmp_path, monkeypatch, entity):
    """Old equipment stays observed until its captured restoration is released."""
    from unittest.mock import AsyncMock

    from custom_components.ha_energy_planner.daikin_memory import CONTROL_KEYS, MEMORY_KEY

    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("climate.home", "cool", {"temperature": 17})
        hass.states.async_set("climate.bedrooms", "cool", {"temperature": 19})
        hass.states.async_set("switch.zone", "on")
        hass.states.async_set("switch.other", "off")
        hass.states.async_set("climate.other", "off", {"temperature": None})
        hass.states.async_set("climate.replacement", "off", {"temperature": 25})
        hass.state = CoreState.running
        store = PlannerStore(hass, "owned-mapping")
        configured = _executor(hass, store)
        old_data = {
            **configured.entry_data,
            CONF_DAIKIN_CLIMATE: "climate.home",
            CONF_CLIMATE_ZONES: ["switch.zone", "switch.other", "climate.bedrooms", "climate.other"],
            "hvac_zone_control": {
                "pairs": {"switch.zone": "climate.bedrooms", "switch.other": "climate.other"},
                "safe_open_sets": [["switch.zone", "switch.other"], ["switch.zone"]],
            },
        }
        new_data = {**configured.entry_data, CONF_DAIKIN_CLIMATE: "climate.replacement", CONF_CLIMATE_ZONES: []}
        entry = ConfigEntry(
            domain=DOMAIN,
            title="Owned Off memory",
            data=new_data,
            options=configured.options,
            source="user",
            unique_id=None,
            version=5,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        main = {
            "hvac_mode": "off",
            "target_temperature": 19,
            MEMORY_KEY: {"version": 1, "control_data": {key: old_data.get(key) for key in CONTROL_KEYS}},
        }
        await store.async_save_ownership(
            {
                "hvac_control": {
                    "main_state": main,
                    "zone_states": {"switch.zone": "on", "climate.bedrooms": {"target_temperature": 24}},
                }
            }
        )
        manual = AsyncMock()
        monkeypatch.setattr(coordinator, "_async_handle_manual_hvac_change", manual)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        coordinator.executor.pending_hvac_desired_state = {
            "restore_main": main,
            "restore_zones": {"switch.zone": "on", "climate.bedrooms": {"target_temperature": 24}},
        }
        try:
            hass.states.async_set(entity, "cool", {"temperature": 22}, context=Context(user_id="operator"))
            await hass.async_block_till_done()
            main_changed = entity == "climate.home"
            if main_changed:
                manual.assert_awaited_once_with("daikin_state_changed", preserve_main_state=True)
            else:
                manual.assert_awaited_once_with("climate_zone_changed", preserve_zone_entity_id=entity)
            if not main_changed:
                assert set(coordinator.executor.pending_hvac_desired_state["manual_zone_entity_ids"]) == {
                    "switch.zone",
                    "climate.bedrooms",
                }
        finally:
            coordinator.executor.pending_hvac_desired_state = None
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("intervention", [
    "none", "head", "zone", "release_head", "release_zone", "read_head", "read_zone", "flush_head", "flush_zone",
    "read_match", "flush_match",
    "read_helper", "flush_helper", "read_expired", "flush_expired", "read_disarm", "flush_disarm",
])
def test_off_dry_acquisition_and_release_with_real_feedback(tmp_path, monkeypatch, intervention):
    """Full HA event path: a damper wakes Dry, then cooling and Dry null aliases."""
    from copy import deepcopy
    from unittest.mock import AsyncMock
    from urllib.parse import unquote

    from test_constraints import _plan
    from test_hvac_adapter import _action
    from test_zone_control import BED_CLIMATE, MAIN, MAIN_CLIMATE, PAIRS, data, room_context

    from custom_components.ha_energy_planner import daikin_memory, zone_control
    from custom_components.ha_energy_planner.zone_control import desired_fields

    monkeypatch.setattr("custom_components.ha_energy_planner.hvac_adapter._STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("custom_components.ha_energy_planner.hvac_adapter._STATE_RECONCILIATION_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("custom_components.ha_energy_planner.hvac_adapter._STATE_CONFIRMATION_POLL_SECONDS", 0.001)

    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        source = json.loads(
            (Path(__file__).parent / "fixtures/hvac/airbase_off_dry_memory_2026_10_31.json").read_text()
        )
        control, model, zones = (deepcopy(source[key]) for key in ("control", "model", "zones"))
        zones = {key: unquote(value).split(";") for key, value in zones.items()}

        class Values:
            def values_for_resource(self, resource):
                return (
                    control
                    if resource.endswith("get_control_info")
                    else model
                    if resource.endswith("get_model_info")
                    else {key: ";".join(value) for key, value in zones.items()}
                )

        async def intervene(phase):
            nonlocal injected
            if not injected and intervention in {f"{phase}_head", f"{phase}_match"}:
                injected = True
                target = 21 if intervention.endswith("match") else 20
                control["stemp"] = str(target)
                head = hass.states.get("climate.head")
                hass.states.async_set(
                    "climate.head", "off", {**head.attributes, "temperature": target},
                    context=Context(user_id="operator"),
                )
            elif not injected and intervention == f"{phase}_zone":
                injected = True
                zones["zone_onoff"][0] = "0"
                hass.states.async_set(MAIN, "off", context=Context(user_id="operator"))
            elif not injected and intervention == f"{phase}_helper":
                injected = True
                hass.states.async_set("input_boolean.operator_hold", "on", context=Context(user_id="operator"))
            elif not injected and intervention == f"{phase}_expired":
                injected = True
                monkeypatch.setattr(dt_util, "utcnow", lambda: c.created_at + timedelta(seconds=2))
            elif not injected and intervention == f"{phase}_disarm":
                injected = True
                store.data["production"]["armed"] = False
            await hass.async_block_till_done()

        async def update(*args, **kwargs):
            await intervene("read")

        vendor = SimpleNamespace(values=Values(), update_status=update, mac="controller")
        config = {**data(), CONF_CLIMATE_AUTOMATIONS: "automation.climate",
                  CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.operator_hold"}
        hass.states.async_set("input_boolean.operator_hold", "off")
        monkeypatch.setattr(
            daikin_memory, "provider",
            lambda *args, **kwargs: (vendor, {MAIN_CLIMATE: 0, BED_CLIMATE: 1}, "core-daikin"),
        )
        monkeypatch.setattr(daikin_memory, "read_topology", lambda *args: zone_control.topology(config))
        monkeypatch.setattr(zone_control, "read_topology", lambda *args: zone_control.topology(config))
        registration = SimpleNamespace(platform="daikin", config_entry_id="core-daikin", device_id="controller")
        registered = dict.fromkeys(["climate.head", *PAIRS.values()], registration)
        monkeypatch.setattr(entity_registry, "async_get", lambda _hass: SimpleNamespace(async_get=registered.get))

        def publish():
            mode = {"1": "heat", "2": "cool", "7": "dry"}[control["mode"]] if control["pow"] == "1" else "off"
            target = float(control["stemp"])
            hass.states.async_set(
                "climate.head",
                mode,
                {
                    "temperature": target,
                    "min_temp": 16,
                    "max_temp": 32,
                    "target_temp_step": 1,
                    "hvac_modes": ["off", "heat", "cool", "dry"],
                },
            )
            for index, (actuator, entity) in enumerate(PAIRS.items()):
                power = zones["zone_onoff"][index]
                hass.states.async_set(actuator, "on" if power == "1" else "off")
                value = (
                    float(zones["lztemp_h" if mode == "heat" else "lztemp_c"][index])
                    if mode in {"heat", "cool"}
                    else None
                )
                hass.states.async_set(
                    entity,
                    mode if power == "1" else "off",
                    {
                        "zone_id": index, "temperature": value, "min_temp": target - 2,
                        "max_temp": target + 2, "target_temp_step": 1,
                    },
                )

        publish()
        for entity in ("sensor.living", "sensor.bed"):
            hass.states.async_set(entity, 25, {"unit_of_measurement": "°C"})
        hass.states.async_set("input_number.low", 21)
        hass.states.async_set("input_number.high", 24)
        hass.state = CoreState.running
        store = PlannerStore(hass, "off-dry-runtime")
        entry = ConfigEntry(
            domain=DOMAIN,
            title="Off Dry readback",
            data=config,
            options={**DEFAULT_OPTIONS, "dry_run": False, "climate_control_enabled": True, "hvac_safe_off_start": True},
            source="user",
            unique_id=None,
            version=5,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        manual = AsyncMock()
        monkeypatch.setattr(coordinator, "_async_handle_manual_hvac_change", manual)
        monkeypatch.setattr(coordinator, "_async_handle_manual_override_helper", AsyncMock())
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        injected = False
        actuations = []
        original_flush = store.async_flush

        async def flush():
            await intervene("flush")
            await original_flush()

        monkeypatch.setattr(store, "async_flush", flush)

        async def actuate(call):
            nonlocal injected
            actuations.append((call.domain, call.service, dict(call.data)))
            entity = call.data["entity_id"]
            if call.domain == "automation":
                hass.states.async_set(entity, "off" if call.service == "turn_off" else "on", context=call.context)
                return
            if entity in PAIRS:
                index = list(PAIRS).index(entity)
                zones["zone_onoff"][index] = "1" if call.service == "turn_on" else "0"
                if call.service == "turn_on":
                    control["pow"] = "1"
            elif entity == "climate.head":
                if call.service == "turn_off":
                    control["pow"] = "0"
                elif call.service == "turn_on":
                    control["pow"] = "1"
                elif call.service == "set_hvac_mode":
                    control["mode"] = {"heat": "1", "cool": "2", "dry": "7"}[call.data["hvac_mode"]]
                    control["pow"] = "1"
                    if control["mode"] in {"1", "2"}:
                        control["stemp"] = control["dt" + control["mode"]]
                elif call.service == "set_temperature":
                    control["stemp"] = str(call.data["temperature"])
                    if control["mode"] in {"1", "2"}:
                        control["dt" + control["mode"]] = control["stemp"]
                        key = "lztemp_h" if control["mode"] == "1" else "lztemp_c"
                        center = float(control["stemp"])
                        for index in range(2):
                            zones[key][index] = str(min(center + 2, max(center - 2, float(zones[key][index]))))
            elif entity in PAIRS.values() and call.service == "set_temperature":
                index = list(PAIRS.values()).index(entity)
                zones["lztemp_h" if control["mode"] == "1" else "lztemp_c"][index] = str(call.data["temperature"])
            publish()
            await hass.async_block_till_done()
            if not injected and intervention == "head" and control["mode"] == "7" and control["pow"] == "1":
                injected = True
                control["stemp"] = "20"
                hass.states.async_set("climate.head", "dry", {"temperature": 20}, context=Context(user_id="operator"))
                await hass.async_block_till_done()
            elif (not injected and intervention == "release_head" and call.service == "set_hvac_mode"
                  and control["mode"] == "7"):
                injected = True
                control["pow"] = "0"
                head = hass.states.get("climate.head")
                hass.states.async_set("climate.head", "off", dict(head.attributes), context=Context(user_id="operator"))
                await hass.async_block_till_done()
            elif not injected and intervention == "zone" and call.service == "set_hvac_mode" and control["mode"] == "2":
                injected = True
                zones["lztemp_c"][0] = "18"
                hass.states.async_set(MAIN_CLIMATE, "cool", {"temperature": 18}, context=Context(user_id="operator"))
                await hass.async_block_till_done()

        for domain, services in {
            "climate": ("turn_on", "turn_off", "set_hvac_mode", "set_temperature"),
            "switch": ("turn_on", "turn_off"),
            "automation": ("turn_on", "turn_off"),
        }.items():
            for service in services:
                hass.services.async_register(domain, service, actuate)
        await hass.async_block_till_done()
        coordinator.async_start_listeners()
        try:
            c = room_context(main=25, bed=25)
            c.created_at = dt_util.utcnow()
            desired = {
                "mode": "cool",
                "hvac_mode": "cool",
                "precondition_target": 21,
                "target_temperature": 21,
                "enable_zones": True,
                **desired_fields(c, "cool", 21, "preconditioning"),
            }
            action = _action(desired)
            action.execute_not_before = c.created_at
            action.execute_not_after = c.created_at + (
                timedelta(seconds=1) if intervention.endswith("expired") else timedelta(minutes=5)
            )
            await coordinator.executor.async_evaluate(_plan(c.created_at, action))
            if intervention.startswith(("read_", "flush_")):
                assert injected and control["pow"] == "0" and control["mode"] == "7"
                target = 21 if intervention.endswith("match") else 20 if intervention.endswith("head") else 19
                assert float(control["stemp"]) == target
                if intervention.endswith("zone"):
                    assert hass.states.get(MAIN).state == "off"
                if intervention.endswith("helper"):
                    assert hass.states.get("input_boolean.operator_hold").state == "on"
                assert not actuations
                assert not store.data.get("ownership", {}).get("hvac_control")
                assert coordinator.executor.pending_hvac_desired_state is None
                reason = (
                    "manual_hvac_override_active" if intervention.endswith("helper") else
                    "hvac_action_outside_execution_window" if intervention.endswith("expired") else
                    "production_gate_not_armed" if intervention.endswith("disarm") else
                    "manual_hvac_override_detected"
                )
                assert store.data["action_attempts"] == []
                outcome = store.data["execution_audit"][-1]
                assert outcome["reason"] == reason
                assert outcome["result"] == "rejected"
                assert outcome["command_sent"] is False
            elif intervention in {"none", "release_head", "release_zone"}:
                assert hass.states.get("climate.head").state == "cool", (
                    [(row.get("stage"), row.get("entity_id"), row.get("expected"), row.get("observed"))
                     for row in store.data.get("climate_audit", []) if row.get("stage") == "unmatched_feedback"],
                    manual.await_args_list,
                )
                if intervention == "release_zone":
                    # A valid manual target under cool/21 would be clamped by
                    # the old cool/17 head restoration, despite excluding its
                    # direct thermostat writes from release ownership.
                    zones["lztemp_c"][0] = "20"
                    hass.states.async_set(
                        MAIN_CLIMATE, "cool", {"temperature": 20}, context=Context(user_id="operator"),
                    )
                    await hass.async_block_till_done()
                    # The manual callback is mocked so this test can drive the
                    # AirBase adapter release itself. Commit its accepted scope.
                    await coordinator._async_persist_runtime_hvac_supersessions()
                result = await coordinator.executor.async_release_hvac_control(
                    "period_ended", preserve_zone_entity_id=MAIN_CLIMATE if intervention == "release_zone" else None,
                )
                if intervention == "release_head":
                    assert injected and control["pow"] == "0" and float(control["stemp"]) == 17
                    await coordinator._async_persist_runtime_hvac_supersessions()
                    confirmed = await coordinator.executor.async_release_hvac_control("confirm_owned_zones")
                    assert confirmed.result is OutcomeResult.RESTORED
                    assert control["pow"] == "0" and float(control["stemp"]) == 17
                elif intervention == "release_zone":
                    assert result.result is OutcomeResult.FAILED
                    assert float(zones["lztemp_c"][0]) == 20
                    assert control["pow"] == "0" and float(control["stemp"]) == 21
                    assert MAIN_CLIMATE not in store.data["ownership"]["hvac_control"]["zone_states"]
                    assert store.data["ownership"]["hvac_control"]["main_state"]["off_start_restore_attempted"]
                else:
                    assert result.result is OutcomeResult.RESTORED, (result, store.data.get("climate_audit"))
                    assert control["pow"] == "0" and control["mode"] == "7" and float(control["stemp"]) == 19
                    manual.assert_not_awaited()
            elif intervention == "head":
                assert injected and float(control["stemp"]) == 20
                assert control["mode"] == "7"
                assert manual.await_count >= 1
            else:
                assert injected and float(zones["lztemp_c"][0]) == 18
                assert manual.await_count >= 1
        finally:
            coordinator.executor.pending_hvac_desired_state = None
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_oversized_zone_feedback_preserves_automation_recovery_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        zone = "climate.zone"
        hass.states.async_set(zone, "heat", {"zone_id": 0, "temperature": 22})
        hass.state = CoreState.running
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        entry = ConfigEntry(
            domain=DOMAIN, title="HVAC feedback",
            data={**configured.entry_data, CONF_CLIMATE_ZONES: [zone]},
            options={**configured.options, CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY: True},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        save_ownership = store.async_save_ownership
        injected = False

        async def inject_feedback(ownership: dict[str, Any]) -> None:
            nonlocal injected
            await save_ownership(ownership)
            confirmation = ownership.get("hvac_control", {}).get("command_confirmation", {})
            if not injected and confirmation.get("entity_id") == zone:
                injected = True
                hass.states.async_set(zone, "heat", {"zone_id": 0, "temperature": 10**400})
                await asyncio.sleep(0)

        monkeypatch.setattr(store, "async_save_ownership", inject_feedback)
        coordinator.async_start_listeners()
        try:
            plan, context = _command("hvac")
            plan.actions[0].desired_state.update(target_temperature=20, configured_zones_only=True)
            async with coordinator._command_lock:
                await coordinator.executor.async_evaluate(plan, context)
            await hass.async_block_till_done()
            assert injected
            assert [(domain, service) for domain, service, _ in calls] == [
                ("automation", "turn_off"), ("automation", "turn_on"),
            ]
            hvac = CapabilityDiscovery(hass, coordinator.entry_data, coordinator.options).inspect().hvac
            assert hvac.issues == ["climate_zone_target_unavailable"]
            await store.async_flush()
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            event = next(item for item in reloaded.data["climate_audit"] if item["stage"] == "unmatched_feedback")
            assert event["expected"]["temperature"] == 22
            assert event["observed"]["temperature"] is None
            assert {"hvac_control", "climate_automations"}.isdisjoint(reloaded.data["ownership"])
            assert f"required_evidence_missing issue=climate_zone_target_unavailable entities={zone}" in caplog.text
            assert "OverflowError" not in caplog.text
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("later", ["unavailable", "0", "confirmed_reduction", "renewed_start", "confirmed_stop"])
def test_queued_power_event_preserves_peak_but_not_superseded_evidence(tmp_path, monkeypatch, later):
    """Real HA queues both reports before the capacity listener can run."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        power_attrs = {"unit_of_measurement": "kW"}
        hass.states.async_set("sensor.ev_power", 7, power_attrs)
        hass.states.async_set("number.limit", 3, {
            "unit_of_measurement": "kW", "min": 1, "max": 7, "step": 1,
        })
        store = PlannerStore(hass, "runtime")
        configured = _executor(hass, store)
        configured.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        configured.options.update(ev_limit_min=1, ev_limit_max=7)
        entry = ConfigEntry(
            domain=DOMAIN, title="Queued capacity", data=configured.entry_data,
            options=configured.options, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        executor = coordinator.executor
        reservations = executor._ev_grid_reservations()
        reservations[entry.entry_id] = {"load_kw": 7, "limit_kw": 10}
        plan, context = _command("ev")
        context.slots[0].baseline_load_forecast_kw = 2
        issues = []

        async def refresh():
            issues.append(executor._ev_reservation_safety_issue(context))

        monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **kwargs: None)
        await hass.async_block_till_done()
        coordinator.async_start_listeners()

        @callback
        def publish(_event):
            # HA queues nested state_changed events until this push callback
            # returns; both listeners then see the later value in hass.states.
            hass.states.async_set("sensor.ev_power", 9, power_attrs)
            if later == "confirmed_stop":
                executor._release_ev_grid_reservation()
            elif later in {"confirmed_reduction", "renewed_start"}:
                hass.states.async_set("sensor.ev_power", 3, power_attrs)
                executor._reconcile_ev_grid_reservation(plan.actions[0], EVCommandResult(
                    True, "ev_power_limit_confirmed", {}, {
                        "confirmed_power_kw": 3,
                        "confirmed_power_at": hass.states.get("sensor.ev_power").last_reported.isoformat(),
                    }, command_sent=True,
                ), None)
                if later == "renewed_start":
                    plan.actions[0].desired_state["projected_load_kw_now"] = 3
                    context.slots[0].projected_ev_load_kw = 3
                    reason, _previous = executor._reserve_ev_grid_capacity(
                        plan.actions[0], context, dt_util.utcnow(),
                    )
                    assert reason is None
            else:
                hass.states.async_set("sensor.ev_power", later, power_attrs)

        unsubscribe = hass.bus.async_listen("ev_provider_push", publish)
        try:
            hass.bus.async_fire("ev_provider_push")
            await hass.async_block_till_done()
            if later == "confirmed_stop":
                assert entry.entry_id not in reservations
                assert issues == []
            elif later == "confirmed_reduction":
                assert reservations[entry.entry_id]["load_kw"] == 3
                assert issues == []
            elif later == "renewed_start":
                # Renewal remains conservative at the configured rate until
                # its own confirmation, but must not revive the retired 9 kW.
                assert reservations[entry.entry_id]["load_kw"] == 7
                assert issues == [None]
            else:
                assert reservations[entry.entry_id]["load_kw"] == 9
                assert issues == ["grid_import_limit_exceeded"]
                await store.async_flush()
                reloaded = PlannerStore(hass, "runtime")
                await reloaded.async_load()
                assert reloaded.data["ev_grid_reservation"]["load_kw"] == 9
        finally:
            unsubscribe()
            await coordinator.async_shutdown()
            await hass.async_stop()
    asyncio.run(run())
