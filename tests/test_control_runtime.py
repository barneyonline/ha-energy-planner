"""Compose real HA storage/services with actuator transactions and fresh runtimes."""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, CoreState, HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers import storage as ha_storage
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError

from custom_components.ha_energy_planner import (
    PLATFORMS,
    _rehydrate_ev_grid_reservation,
    adapter_helpers,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
    ev_adapter,
    startup_recovery,
)
from custom_components.ha_energy_planner.config_flow import INPUT_STEP_CLIMATE, OptionsFlow
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_IMPORT_PRICE,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    DEFAULT_OPTIONS,
    DOMAIN,
    EV_RESERVATION_EXTERNAL_BASELINE,
    EV_SIGNAL_CLOCK_HANDOFFS,
)
from custom_components.ha_energy_planner.coordinator import (
    EnergyPlannerCoordinator,
    InitialPlanSuperseded,
    _HvacOutageFeedback,
    _HvacOutages,
    _PlanExecution,
)
from custom_components.ha_energy_planner.discovery import CapabilityDiscovery
from custom_components.ha_energy_planner.enphase_export_limit import ExportLimitControl
from custom_components.ha_energy_planner.entry_data import combined_entry_data
from custom_components.ha_energy_planner.ev_adapter import EVChargerAdapter, EVCommandResult
from custom_components.ha_energy_planner.ev_control import ev_grid_headroom_reason
from custom_components.ha_energy_planner.ev_runtime import (
    EV_RETIRED_CAP_KW,
    MAX_EV_CAP_HISTORY,
    MAX_NATIVE_STOP_SAMPLES,
    EVImplicitSignalClock,
)
from custom_components.ha_energy_planner.executor import (
    Executor,
    _record_ev_cap_confirmation,
    _retain_ev_cap_readback,
)
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


def _freeze_entry_clock(monkeypatch, current):
    class EntryDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            at = current()
            return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)

    monkeypatch.setattr("custom_components.ha_energy_planner.datetime", EntryDateTime)


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


@pytest.mark.parametrize("cap_feedback", [
    "restored", "restore_failed", "uncertain", "manual", "remapped", "save_draw", "binary",
])
def test_expired_ev_lease_reconciles_native_cap_after_confirmed_stop(tmp_path, monkeypatch, cap_feedback):
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 7, "step": 1}
        hass.states.async_set("number.limit", 5, attributes)
        initial_charging = "off" if cap_feedback in {"manual", "binary"} else "on"
        hass.states.async_set("switch.charger", initial_charging)
        hass.states.async_set("binary_sensor.charging", initial_charging)
        hass.states.async_set("sensor.ev_power", 5 if initial_charging == "on" else 0, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data[CONF_EV_CHARGING] = "binary_sensor.charging"
        if cap_feedback != "binary":
            executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_limit_min=1, ev_limit_max=7)
        _arm(store, executor)
        baseline = ({CONF_EV_CHARGER: "off"} if cap_feedback == "binary" else
                    {"ev_power_limit_entity": "4", "ev_power_limit_unit": "kW"})
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
        )
        if cap_feedback != "binary":
            plan.next_action.desired_state["power_limit"] = {
                "entity_id": "number.limit", "value": 6, "unit": "kW", "physical_power_kw": 6,
            }
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
            if cap_feedback != "binary":
                hass.states.async_set("number.limit", 3, attributes, context=Context(user_id="driver"))

        async def stop_charger(call: ServiceCall) -> None:
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("switch.charger", "off", context=call.context)
            hass.states.async_set("binary_sensor.charging", "off", context=call.context)
            hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"}, context=call.context)

        hass.services.async_register("number", "set_value", set_limit)
        hass.services.async_register("switch", "turn_on", start_charger)
        hass.services.async_register("switch", "turn_off", stop_charger)
        if cap_feedback == "save_draw":
            save = store._store.async_save

            async def observe_draw_during_release(payload):
                if "ownership" in payload.get("_pending_ev_release", {}):
                    hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                    await asyncio.sleep(0)
                    hass.states.async_set("sensor.ev_power", "unavailable")
                await save(payload)

            monkeypatch.setattr(store._store, "async_save", observe_draw_during_release)
        try:
            await executor.async_evaluate(plan, context, plan_is_current=lambda: True)

            assert store.data["execution_audit"][-1]["reason"] == "ev_allocation_expired"
            assert hass.states.get("binary_sensor.charging").state == "off"
            latest = PlannerStore(hass, "runtime")
            await latest.async_load()
            if cap_feedback == "save_draw":
                assert executor._ev_grid_reservations()["runtime"]["load_kw"] >= 8
                assert latest.data["ev_grid_reservation"]["active"]
                assert latest.data["ownership"] == ownership
                assert "command_exposure" in latest.data["ev_telemetry"]
                assert lease_changes == [deadline]
                assert [service for _, service, _ in calls] == ["set_value", "turn_off", "set_value"]
                return
            assert "runtime" not in executor._ev_grid_reservations()
            assert lease_changes == [None]
            assert latest.data["ev_grid_reservation"] == {"active": False}
            assert "command_exposure" not in latest.data["ev_telemetry"]
            if cap_feedback in {"restored", "restore_failed", "remapped"}:
                assert [service for _, service, _ in calls] == ["set_value", "turn_off", "set_value"]
            elif cap_feedback == "uncertain":
                assert [service for _, service, _ in calls] == ["set_value", "turn_off"]
            elif cap_feedback == "binary":
                assert [service for _, service, _ in calls] == ["turn_on", "turn_off"]
            else:
                assert [service for _, service, _ in calls] == ["set_value", "turn_on", "turn_off"]
            unresolved = cap_feedback in {"restore_failed", "uncertain", "remapped"}
            assert latest.data["ownership"] == (ownership if unresolved else {})
            expected_cap = {
                "restored": 4, "restore_failed": 6, "uncertain": 5, "manual": 3, "remapped": 5, "binary": 5,
            }
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


@pytest.mark.parametrize("restore_feedback", ["restored", "wrong_unit", "lost_meter", "write_refused", "cap_changed"])
def test_number_control_transaction_reserves_then_confirms_reduction_and_restores(
    tmp_path, monkeypatch, restore_feedback,
) -> None:
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
            setting = call.data["value"]
            if setting == 6 and restore_feedback == "write_refused":
                raise HomeAssistantError("Test limiter refused restoration")
            unit = "A" if setting == 6 and restore_feedback == "wrong_unit" else "kW"
            hass.states.async_set("number.limit", setting, {**attrs, "unit_of_measurement": unit})
            hass.states.async_set("sensor.ev_power", 3 if setting == 3 else 0, {"unit_of_measurement": "kW"})

        if restore_feedback in {"lost_meter", "cap_changed"}:
            backend_save = store._store.async_save

            async def interrupted_save(data):
                proposal = data.get("_pending_ev_release", {})
                if "ownership" in proposal:
                    if restore_feedback == "lost_meter":
                        hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                        await asyncio.sleep(0)
                        hass.states.async_set("sensor.ev_power", "unavailable")
                    else:
                        hass.states.async_set("number.limit", 3, attrs)
                await backend_save(data)

            monkeypatch.setattr(store._store, "async_save", interrupted_save)

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
            deadline_updates = []
            if restore_feedback != "restored":
                executor.ev_allocation_deadline_callback = deadline_updates.append
            await executor.async_evaluate(plan, context)
            assert calls[-2][1:] == ("turn_off", {"entity_id": "switch.charger"})
            assert calls[-1][1] == "set_value"
            expected_cap = "3" if restore_feedback in {"write_refused", "cap_changed"} else "6"
            assert float(hass.states.get("number.limit").state) == float(expected_cap)
            if restore_feedback == "restored":
                assert not store.data["ev_grid_reservation"]["active"]
                assert not store.data["ownership"]
            else:
                assert store.data["ev_grid_reservation"]["active"]
                assert store.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "6"
                if restore_feedback == "lost_meter":
                    assert store.data["ev_grid_reservation"]["load_kw"] >= 8
                    assert "command_exposure" in store.data["ev_telemetry"]
                else:
                    assert "command_exposure" not in store.data["ev_telemetry"]
            assert deadline_updates == ([] if restore_feedback in {"restored", "lost_meter"} else [None])
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
        if intervention == "previous_cap_confirmation":
            executor.options["ev_charge_rate_kw"] = 6
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
            expected_reservation = dict(original_reservation)
            if intervention == "previous_cap_confirmation":
                confirmed_at = hass.states.get("sensor.ev_power").last_reported.isoformat()
                pending_limit = expected_reservation.pop("pending_power_limit")
                expected_reservation.pop("observed_limit_kw", None)
                expected_reservation.update(
                    load_kw=6, observed_load_kw=6, observed_report_kw=6,
                    observed_load_at=confirmed_at, confirmed_power_at=confirmed_at,
                    confirmed_power_limit=True, confirmed_cap_kw=6,
                    cap_reduction={"prior_load_kw": 6, "retained_load_kw": 6, "cap_kw": 6, "confirmed_at": confirmed_at,
                                   "prior_confirmation_at": None, "prior_confirmed_cap_kw": None,
                                   "pending_limit": pending_limit},
                )
            assert reservations["runtime"] == expected_reservation
            expected_other_load = {"previous_cap_confirmation": 3, "pause_during_cap": 2}.get(intervention, 4)
            assert reservations[other_entry_id]["load_kw"] == expected_other_load
            reloaded = PlannerStore(hass, "runtime")
            await reloaded.async_load()
            assert reloaded.data["ownership"] == original_ownership
            assert reloaded.data["ev_grid_reservation"] == {**expected_reservation, "active": True}
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
            if intervention == "previous_cap_confirmation":
                hass.states.async_set("sensor.ev_power", 6.5, {**power_attrs, "sampled_at_utc": confirmed_at})
                assert executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                assert reservations["runtime"]["load_kw"] == 6.5
                assert reservations["runtime"]["pending_power_limit"] == pending_limit
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


def test_native_cap_confirmation_does_not_publish_headroom_from_a_replayed_sample(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        clock = start = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
        hass.states.async_set("number.limit", 6, attrs)
        hass.states.async_set("sensor.ev_power", 6, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_limit_min=1, ev_limit_max=6, ev_charge_rate_kw=6)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": executor.entry_data,
        })
        reservations = executor._ev_grid_reservations()
        reservations[executor.entry_id] = {"load_kw": 6, "limit_kw": 10, "reserved_at": start.isoformat()}
        rejected = (start + timedelta(seconds=2)).isoformat()

        async def limit(call):
            nonlocal clock
            hass.states.async_set("number.limit", call.data["value"], attrs, timestamp=clock.timestamp())
            power_attrs = {"unit_of_measurement": "kW", "sampled_at_utc": rejected}
            hass.states.async_set("sensor.ev_power", 3, power_attrs, timestamp=clock.timestamp())
            clock += timedelta(seconds=3)
            hass.states.async_set("sensor.ev_power", 3, {**power_attrs, "name": "Meter"}, timestamp=clock.timestamp())

        hass.services.async_register("number", "set_value", limit)
        plan, context = _command("ev")
        action = plan.actions[0]
        action.desired_state.update(projected_load_kw_now=3, power_limit={
            "entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3,
        })
        context.slots[0].projected_ev_load_kw = 3
        try:
            reason, previous = executor._reserve_ev_grid_capacity(action, context, clock)
            assert reason is None
            adapter = EVChargerAdapter(hass, executor.entry_data, power_options=executor.options,
                                       confirmation_timeout_seconds=0, confirmation_retries=0)
            with ExitStack() as observations:
                executor._observe_native_ev_adapter(observations, adapter, executor.entry_data)
                result = await adapter.async_execute(action)
            assert result.applied and "confirmed_power_kw" not in result.post_state
            executor._reconcile_ev_grid_reservation(action, result, previous)
            assert reservations[executor.entry_id]["load_kw"] == 6
            assert reservations[executor.entry_id]["pending_power_limit"]["value"] == 3
            clock += timedelta(seconds=1)
            fresh = {"unit_of_measurement": "kW", "sampled_at_utc": clock.isoformat()}
            hass.states.async_set("sensor.ev_power", 3, fresh, timestamp=clock.timestamp())
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
            assert reservations[executor.entry_id]["load_kw"] == 3
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("feedback", [
    None, "cached", "out_of_order", "same_timestamp", "fresh", "higher_after_cap", "completed_during_save",
    "clock_invalidated", "distinct_while_pending",
])
def test_delayed_power_confirmation_releases_only_the_confirmed_reduction(tmp_path, feedback):
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
            if feedback == "cached":
                hass.states.async_set("sensor.ev_power", 3, {
                    "unit_of_measurement": "kW", "sampled_at_utc": old_sample,
                })
            elif feedback in {"out_of_order", "same_timestamp", "fresh"}:
                low_sample = dt_util.utcnow()
                await asyncio.sleep(0)
                peak_sample = dt_util.utcnow()
                hass.states.async_set("sensor.ev_power", 6, {
                    "unit_of_measurement": "kW", "sampled_at_utc": peak_sample.isoformat(),
                })
                assert not executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                sample = {"out_of_order": low_sample, "same_timestamp": peak_sample}.get(feedback, dt_util.utcnow())
                hass.states.async_set("sensor.ev_power", 3, {
                    "unit_of_measurement": "kW", "sampled_at_utc": sample.isoformat(),
                })
                if feedback == "fresh":
                    assert not executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

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
            if feedback in {"out_of_order", "same_timestamp", "fresh"}:
                assert result.post_state["confirmed_power_kw"] == 3
            executor._reconcile_ev_grid_reservation(plan.actions[0], result, previous)
            await executor.async_persist_ev_grid_reservation()
            if feedback == "fresh":
                assert store.data["ev_grid_reservation"]["load_kw"] == 3
                assert "pending_power_limit" not in store.data["ev_grid_reservation"]
                return
            assert store.data["ev_grid_reservation"]["load_kw"] == 6
            assert store.data["ev_grid_reservation"]["pending_power_limit"]["value"] == 3
            executor.sync_ev_grid_reservation()
            assert executor._ev_grid_reservations()[executor.entry_id]["load_kw"] == 6
            original_boundary = store.data["ev_grid_reservation"]["pending_power_limit"]["at"]
            reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
            assert reason is None
            completed = feedback in {"completed_during_save", "clock_invalidated"}
            if completed:
                hass.states.async_set("sensor.ev_power", 3, {"unit_of_measurement": "kW"})
                executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                assert "pending_power_limit" not in executor._ev_grid_reservations()[executor.entry_id]
            repeat = await adapter.async_execute(plan.actions[0])
            executor._reconcile_ev_grid_reservation(plan.actions[0], repeat, previous)
            reservation = executor._ev_grid_reservations()[executor.entry_id]
            if completed:
                assert not repeat.power_limit_written
                assert "pending_power_limit" not in reservation
                reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
                assert reason is None and previous is not None
                stable_repeat = await adapter.async_execute(plan.actions[0])
                assert stable_repeat.applied and not stable_repeat.power_limit_written
                executor._reconcile_ev_grid_reservation(plan.actions[0], stable_repeat, previous)
                reservation = executor._ev_grid_reservations()[executor.entry_id]
                hass.states.async_set("sensor.ev_power", 2.1, {"unit_of_measurement": "kW"})
                assert not executor.sync_ev_grid_reservation()
                assert reservation["load_kw"] == 3
                if feedback == "clock_invalidated":
                    reservation["confirmed_power_at"] = (dt_util.utcnow() + timedelta(seconds=20)).isoformat()
                hass.states.async_set("sensor.ev_power", 5, {"unit_of_measurement": "kW"})
                assert executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                retained = reservation["load_kw"]
                reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
                assert reason is None and previous is not None
                repeat_after_confirmation = await adapter.async_execute(plan.actions[0])
                assert repeat_after_confirmation.applied and not repeat_after_confirmation.power_limit_written
                executor._reconcile_ev_grid_reservation(plan.actions[0], repeat_after_confirmation, previous)
                reservation = executor._ev_grid_reservations()[executor.entry_id]
                assert "pending_power_limit" not in reservation
                hass.states.async_set("sensor.ev_power", 3, {"unit_of_measurement": "kW"})
                assert not executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                assert reservation["load_kw"] == retained
                assert "pending_power_limit" not in reservation

            if completed or feedback == "distinct_while_pending":
                # A different setting reached outside Planner can still earn
                # fresh physical confirmation without a duplicate write.
                retained = reservation["load_kw"]
                hass.states.async_set("number.limit", 2, attrs)
                plan.actions[0].desired_state.update(projected_load_kw_now=2, power_limit={
                    "entity_id": "number.limit", "value": 2, "unit": "kW", "physical_power_kw": 2,
                })
                context.slots[0].projected_ev_load_kw = 2
                reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
                assert reason is None and previous is not None
                distinct_cap = await adapter.async_execute(plan.actions[0])
                assert distinct_cap.applied and not distinct_cap.power_limit_written
                executor._reconcile_ev_grid_reservation(plan.actions[0], distinct_cap, previous)
                reservation = executor._ev_grid_reservations()[executor.entry_id]
                assert reservation["load_kw"] == retained
                assert reservation["pending_power_limit"]["physical_power_kw"] == 2
                hass.states.async_set("sensor.ev_power", 2, {"unit_of_measurement": "kW"})
                executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                assert reservation["load_kw"] == reservation["confirmed_cap_kw"] == 2
                assert "pending_power_limit" not in reservation
                return
            assert reservation["pending_power_limit"]["at"] == original_boundary
            now = dt_util.utcnow()
            reservation["pending_power_limit"]["at"] = (now - timedelta(seconds=3)).isoformat()
            peak_at = now
            hass.states.async_set("sensor.ev_power", 6, {
                "unit_of_measurement": "kW", "sampled_at_utc": peak_at.isoformat(),
            })
            assert not executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
            reason, renewed_previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, dt_util.utcnow())
            assert reason is None and renewed_previous is not None
            reservation = executor._ev_grid_reservations()[executor.entry_id]
            assert reservation["observed_load_at"] == peak_at.isoformat()
            for sample in (peak_at - timedelta(seconds=1), peak_at):
                hass.states.async_set("sensor.ev_power", 3, {
                    "unit_of_measurement": "kW", "sampled_at_utc": sample.isoformat(),
                })
                assert not executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                assert reservation["load_kw"] == 6
                assert "pending_power_limit" in reservation
            hass.states.async_set("sensor.ev_power", 3, {
                "unit_of_measurement": "kW", "sampled_at_utc": dt_util.utcnow().isoformat(),
            })
            executor.sync_ev_grid_reservation()
            if feedback == "higher_after_cap":
                executor.options["ev_charge_rate_kw"] = 5
                hass.states.async_set("sensor.ev_power", 4, {"unit_of_measurement": "kW"})
                assert executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
            executor._restore_ev_grid_reservation(renewed_previous)
            await executor.async_persist_ev_grid_reservation()
            saved = store.data["ev_grid_reservation"]
            assert isinstance(saved.get("confirmed_power_at"), str)
            assert "pending_power_limit" not in saved
            if feedback == "higher_after_cap":
                assert saved["load_kw"] == 5 and saved["observed_load_kw"] == 4
            else:
                assert saved["load_kw"] == 3 and saved["observed_load_kw"] == 3
                plan.actions[0].desired_state["projected_load_kw_now"] = 7
                context.slots[0].projected_ev_load_kw = 7
                reason, before_large_start = executor._reserve_ev_grid_capacity(
                    plan.actions[0], context, dt_util.utcnow(),
                )
                assert reason is None and before_large_start is not None
                hass.states.async_set("sensor.ev_power", 6, {
                    "unit_of_measurement": "kW", "sampled_at_utc": peak_at.isoformat(),
                })
                assert not executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                executor._restore_ev_grid_reservation(before_large_start)
                assert executor._ev_grid_reservations()[executor.entry_id]["load_kw"] == 3
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


@pytest.mark.parametrize("scenario", [
    "refresh", "shutdown", "shutdown_retry", "cancel_unload", "resume_after_save",
    "confirmed_reduction", "confirmed_reduction_report",
])
def test_ev_capacity_is_durable_during_refresh_and_unload(tmp_path, monkeypatch, scenario):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entity = "sensor.ev_power"
        attributes = {"unit_of_measurement": "W"}
        entry_data = {"ev_power_entity": entity}
        options = {**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7}
        confirmed_reduction = scenario in {"confirmed_reduction", "confirmed_reduction_report"}
        if confirmed_reduction:
            entry_data["ev_power_limit_entity"] = "number.limit"
            options.update(ev_limit_min=1, ev_limit_max=7)
            hass.states.async_set("number.limit", "7", {
                "unit_of_measurement": "kW", "min": 1, "max": 7, "step": 1,
            })
        entry = ConfigEntry(
            domain=DOMAIN, title="EV capacity feedback", data=entry_data,
            options=options, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        object.__setattr__(entry, "runtime_data", coordinator)
        coordinator.data, _ = _command("ev")
        reservation = {"load_kw": 7}
        coordinator.executor._ev_grid_reservations()[entry.entry_id] = reservation
        refresh_started, save_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        latest_persisted = asyncio.Event()
        failed = False
        blocking_save = scenario != "refresh"
        shutting_down = scenario in {"shutdown", "shutdown_retry", "resume_after_save"}
        latest_load = 7 if confirmed_reduction else 7.2
        unload = None

        async def blocked_planning(**_kwargs):
            refresh_started.set()
            await release.wait()
            return coordinator.data

        backend_save = store._store.async_save

        async def save(data):
            nonlocal failed
            load = data["ev_grid_reservation"].get("load_kw")
            if blocking_save and load == pytest.approx(7.1):
                save_started.set()
                await release.wait()
            if scenario == "shutdown_retry" and not failed and load == pytest.approx(7.2):
                failed = True
                raise OSError("simulated reservation write failure during shutdown")
            await backend_save(data)
            if load == pytest.approx(latest_load):
                assert coordinator._command_lock.locked()
                latest_persisted.set()

        monkeypatch.setattr(coordinator, "_async_update_data_locked", blocked_planning)
        monkeypatch.setattr(store._store, "async_save", save)
        hass.states.async_set(entity, "7000", attributes)
        coordinator.async_start_listeners()
        try:
            hass.states.async_set(entity, "7100", attributes)
            started = save_started if blocking_save else refresh_started
            await asyncio.wait_for(started.wait(), 5)
            if scenario == "refresh":
                assert coordinator._planner_lock.locked()
                assert store._save_delay_depth == 1

            generation = coordinator._refresh_generation
            if scenario == "cancel_unload":
                draining = asyncio.Event()
                wait_for_execution = coordinator.async_wait_for_plan_execution
                deadline = dt_util.utcnow() + timedelta(minutes=15)
                coordinator._schedule_ev_allocation_deadline(deadline)
                original_timer = coordinator._ev_allocation_cancel

                async def wait_for_drain():
                    draining.set()
                    await wait_for_execution()

                monkeypatch.setattr(coordinator, "async_wait_for_plan_execution", wait_for_drain)
                unload = asyncio.create_task(async_unload_entry(hass, entry))
                await asyncio.wait_for(draining.wait(), 5)
                assert coordinator._ev_allocation_cancel is None
                hass.states.async_set(entity, "7200", attributes)
                await asyncio.sleep(0)
                assert coordinator._refresh_generation == generation
                unload.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await unload
                assert not coordinator._tearing_down
                assert coordinator._refresh_generation == generation + 1
                assert coordinator._ev_allocation_deadline == deadline
                assert coordinator._ev_allocation_cancel not in (None, original_timer)
                assert len(coordinator._listener_tasks) == 1
                release.set()
            elif confirmed_reduction:
                if scenario == "confirmed_reduction_report":
                    hass.states.async_set(entity, "7000", attributes)
                    await asyncio.sleep(0)
                    assert reservation["load_kw"] == pytest.approx(7.1)
                reservation["pending_power_limit"] = {
                    "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 7,
                    "physical_power_kw": 7, "at": dt_util.utcnow().isoformat(),
                }
                hass.states.async_set(entity, "7000", attributes)
                await asyncio.sleep(0)
                assert coordinator._refresh_generation == generation
                assert reservation["confirmed_power_limit"] is True
                release.set()
            else:
                hass.states.async_set(entity, "7200", attributes)
                await asyncio.sleep(0)
                assert coordinator._refresh_generation == generation + 1

            if shutting_down:
                coordinator._begin_shutdown()
                release.set()
                await coordinator.async_wait_for_plan_execution()
                if scenario == "shutdown_retry":
                    assert failed and not latest_persisted.is_set()
                    assert coordinator._ev_capacity_flush_needed
                await coordinator.async_wait_for_refresh_shutdown()
            await asyncio.wait_for(latest_persisted.wait(), 5)
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            saved_reservation = saved.data["ev_grid_reservation"]
            assert saved_reservation["load_kw"] == pytest.approx(latest_load)
            if confirmed_reduction:
                assert saved_reservation["confirmed_power_limit"] is True
                assert saved_reservation["confirmed_power_at"] == reservation["confirmed_power_at"]
                assert "pending_power_limit" not in saved_reservation
            if shutting_down:
                assert not refresh_started.is_set()
                if scenario == "resume_after_save":
                    coordinator.async_start_listeners()
                    await asyncio.wait_for(refresh_started.wait(), 5)
            elif scenario == "refresh":
                assert coordinator._planner_lock.locked()
        finally:
            coordinator._begin_shutdown()
            release.set()
            if unload is not None and not unload.done():
                unload.cancel()
                await asyncio.gather(unload, return_exceptions=True)
            await coordinator.async_wait_for_plan_execution()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("abort_phase", [
    "core_debounce", "command_lock", "stale_inputs", "stale_after_evaluation", "startup_validation",
])
def test_ev_capacity_refresh_survives_aborted_unload(tmp_path, monkeypatch, abort_phase):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entity = "sensor.ev_power"
        attributes = {"unit_of_measurement": "W"}
        entry = ConfigEntry(
            domain=DOMAIN, title="EV capacity debounce", data={"ev_power_entity": entity},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 7}, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        coordinator = EnergyPlannerCoordinator(hass, entry, PlannerStore(hass, entry.entry_id))
        coordinator.data, context = _command("ev")
        coordinator.executor._ev_grid_reservations()[entry.entry_id] = {"load_kw": 7}
        planned = []
        executed = []
        inputs_match = True

        async def plan(**_kwargs):
            planned.append(coordinator.executor._ev_grid_reservations()[entry.entry_id]["load_kw"])
            if (abort_phase != "core_debounce" and len(planned) > 1
                    and not coordinator._startup_auto_recovery_validation_active):
                coordinator._deferred_plan_execution = _PlanExecution(
                    coordinator._refresh_generation, coordinator.data, context, coordinator.options,
                )
            return coordinator.data

        async def execute(plan, _context, **_kwargs):
            nonlocal inputs_match
            executed.append(planned[-1])
            if abort_phase == "stale_after_evaluation" and len(executed) == 1:
                inputs_match = False
            return plan.next_action

        async def finish_core_cooldown():
            debouncer = coordinator._debounced_refresh
            if debouncer._timer_task is not None:
                debouncer._timer_task.cancel()
                debouncer._on_debounce()
            await hass.async_block_till_done()

        monkeypatch.setattr(coordinator, "_async_update_data_locked", plan)
        monkeypatch.setattr(coordinator.executor, "async_evaluate", execute)
        monkeypatch.setattr(coordinator, "_plan_input_snapshot_matches", lambda *_args, **_kwargs: inputs_match)
        hass.states.async_set(entity, "7000", attributes)
        coordinator.async_start_listeners()
        try:
            await coordinator.async_request_refresh()
            hass.states.async_set(entity, "7200", attributes)
            await hass.async_block_till_done()
            assert planned == [7]
            assert not coordinator._listener_tasks

            if abort_phase == "command_lock":
                async with coordinator._command_lock:
                    await finish_core_cooldown()
                    assert planned == pytest.approx([7, 7.2])
                    assert coordinator._plan_execution_task is not None
                    coordinator._begin_shutdown()
                await coordinator.async_wait_for_plan_execution()
                assert executed == []
            elif abort_phase in {"stale_inputs", "stale_after_evaluation"}:
                inputs_match = abort_phase == "stale_after_evaluation"
                await finish_core_cooldown()
                await coordinator.async_wait_for_plan_execution()
                assert planned == pytest.approx([7, 7.2])
                assert coordinator._debounce_cancel is not None
                assert len(executed) == int(abort_phase == "stale_after_evaluation")
                coordinator._begin_shutdown()
                inputs_match = True
            elif abort_phase == "startup_validation":
                requested = []
                with monkeypatch.context() as during_validation:
                    during_validation.setattr(coordinator, "_queue_ev_capacity_refresh", requested.append)
                    await coordinator._async_run_startup_auto_recovery_validation()
                assert planned == pytest.approx([7, 7.2]) and executed == []
                assert requested == [coordinator._ev_capacity_work]
                coordinator._begin_shutdown()
            else:
                coordinator._begin_shutdown()
                await finish_core_cooldown()
                assert planned == [7]

            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            await finish_core_cooldown()
            await coordinator.async_wait_for_plan_execution()
            expected_plans = [7, 7.2] if abort_phase == "core_debounce" else [7, 7.2, 7.2]
            assert planned == pytest.approx(expected_plans)
            expected_executions = {"core_debounce": 0, "stale_after_evaluation": 2}.get(abort_phase, 1)
            assert executed == pytest.approx([7.2] * expected_executions)

            # A later unrelated execution needs no extra EV feedback work.
            if abort_phase == "command_lock":
                async with coordinator._command_lock:
                    coordinator._schedule_plan_execution(_PlanExecution(
                        coordinator._refresh_generation, coordinator.data, context, coordinator.options,
                    ))
                    await asyncio.sleep(0)
                    coordinator._begin_shutdown()
                await coordinator.async_wait_for_plan_execution()
            else:
                coordinator._begin_shutdown()
            coordinator.async_start_listeners()
            await finish_core_cooldown()
            assert planned == pytest.approx(expected_plans)
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("case", [
    "ordinary_drain", "deferred_startup_save", "persistent_reconcile_failure", "cancel_during_reconcile",
    "cancel_after_terminal_disarm", "deferred_startup_disarm", "deferred_disarmed_failure",
    "owned_restore_after_retry", "orphan_reservation_restore_after_retry",
])
def test_aborted_unload_recovers_only_with_durable_startup_evidence(tmp_path, monkeypatch, case):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        entry = ConfigEntry(
            domain=DOMAIN, title="Startup recovery during unload", data={},
            options={**DEFAULT_OPTIONS, "dry_run": False, "planner_enabled": True}, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        object.__setattr__(entry, "runtime_data", coordinator)
        persistent_failure = case == "persistent_reconcile_failure"
        deferred_failure = case in {"deferred_startup_disarm", "deferred_disarmed_failure"}
        restore_after_retry = case in {"owned_restore_after_retry", "orphan_reservation_restore_after_retry"}
        terminal_cancel = case == "cancel_after_terminal_disarm"
        armed_recovery = persistent_failure or restore_after_retry or case == "deferred_startup_disarm"
        recovery_status = "grace" if armed_recovery else "waiting_for_safe"
        if recovery_status == "grace":
            _arm(store, coordinator.executor)
        else:
            store.data["production"]["armed"] = False
        store.data["production"]["startup_auto_recovery"] = {"status": recovery_status}
        if restore_after_retry:
            store.data["production"]["dry_run_evidence_fingerprint"] = "obsolete"
        if case == "owned_restore_after_retry":
            store.data["ownership"]["ev_smart_charging_state"] = {"state": "on"}
        elif case == "orphan_reservation_restore_after_retry":
            store.data["ev_grid_reservation"] = {"active": True, "load_kw": 7}
        elif terminal_cancel:
            store.data["ownership"]["enphase_profile"] = "Original profile"
        coordinator._startup_auto_recovery_authorized = True
        coordinator._configuration_reload_handoff = case == "deferred_startup_save"
        backend_save = store._store.async_save
        recovery_started = asyncio.Event()
        reconcile_writing, finish_reconcile = asyncio.Event(), asyncio.Event()
        disarm_written = asyncio.Event()
        failed_writes = []
        restored = []
        unload_task = None

        async def rejected_drain():
            if case != "cancel_after_terminal_disarm":
                raise OSError("reservation drain rejected")

        async def save(data):
            production = data["production"]
            status = production["startup_auto_recovery"]["status"]
            if case == "cancel_during_reconcile" and status == "waiting_for_home_assistant":
                reconcile_writing.set()
                await finish_reconcile.wait()
            if (persistent_failure and production["armed"]) or (
                (case == "deferred_startup_save" or restore_after_retry)
                and status == "waiting_for_safe" and not failed_writes
            ) or (
                deferred_failure and status == recovery_status
            ) or (
                deferred_failure and status == "failed" and "failed" not in failed_writes
            ):
                failed_writes.append(status)
                raise OSError("simulated recovery evidence write failure")
            await backend_save(data)
            if deferred_failure and status == "failed":
                disarm_written.set()
            if case == "cancel_after_terminal_disarm" and not reconcile_writing.is_set():
                assert production["armed"] is False and status == "cancelled"
                reconcile_writing.set()
                await finish_reconcile.wait()

        async def supervise():
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["production"]["startup_auto_recovery"]["status"] == "waiting_for_safe"
            recovery_started.set()

        async def restore_owned_ev(reason, **_kwargs):
            assert not store.data["production"]["armed"]
            await store.async_save_ownership({})
            await store.async_save_ev_grid_reservation({"active": False})
            restored.append(reason)

        monkeypatch.setattr(coordinator, "async_wait_for_refresh_shutdown", rejected_drain)
        monkeypatch.setattr(coordinator, "_async_run_startup_auto_recovery", supervise)
        if restore_after_retry or terminal_cancel:
            monkeypatch.setattr(coordinator, "async_restore_safe_state", restore_owned_ev)
        if deferred_failure:
            await backend_save(dict(store.data))
        monkeypatch.setattr(store._store, "async_save", save)
        coordinator.async_start_listeners()
        try:
            if case == "deferred_startup_save":
                with store.defer_save():
                    with pytest.raises(OSError, match="reservation drain rejected"):
                        await async_unload_entry(hass, entry)
                    await asyncio.wait_for(recovery_started.wait(), 5)
                assert failed_writes == ["waiting_for_safe"]
            elif deferred_failure:
                with store.defer_save():
                    with pytest.raises(OSError, match="reservation drain rejected"):
                        await async_unload_entry(hass, entry)
                    await asyncio.wait_for(disarm_written.wait(), 5)
                    saved = PlannerStore(hass, entry.entry_id)
                    await saved.async_load()
                    assert saved.data["production"]["armed"] is False
                    assert saved.data["production"]["startup_auto_recovery"]["status"] == "failed"
                assert failed_writes == [recovery_status, recovery_status, "failed"]
                assert not coordinator._startup_auto_recovery_authorized and not recovery_started.is_set()
            elif case == "cancel_during_reconcile":
                unload_task = asyncio.create_task(async_unload_entry(hass, entry))
                await asyncio.wait_for(reconcile_writing.wait(), 5)
                unload_task.cancel()
                await asyncio.sleep(0)
                assert not unload_task.done()
                finish_reconcile.set()
                with pytest.raises(asyncio.CancelledError):
                    await unload_task
                await asyncio.wait_for(recovery_started.wait(), 5)
            elif case == "cancel_after_terminal_disarm":
                unload_task = asyncio.create_task(async_unload_entry(hass, entry))
                await asyncio.wait_for(reconcile_writing.wait(), 5)
                unload_task.cancel()
                await asyncio.sleep(0)
                assert not unload_task.done()
                finish_reconcile.set()
                with pytest.raises(asyncio.CancelledError):
                    await unload_task
                saved = PlannerStore(hass, entry.entry_id)
                await saved.async_load()
                assert saved.data["production"]["armed"] is False
                assert saved.data["production"]["startup_auto_recovery"]["status"] == "cancelled"
                assert restored == ["entry_unload"] and not saved.data["ownership"]
                assert not coordinator._startup_auto_recovery_authorized
                assert not recovery_started.is_set()
            else:
                with pytest.raises(OSError, match="reservation drain rejected"):
                    await async_unload_entry(hass, entry)
                if persistent_failure:
                    assert failed_writes == ["waiting_for_home_assistant"] * 2
                    assert store.data["production"]["armed"] is False
                    assert coordinator._startup_auto_recovery_task is None
                    assert not coordinator._startup_auto_recovery_authorized and not recovery_started.is_set()
                    assert not coordinator._export_retry_startup_blocked
                    assert coordinator.executor.notification_grace_until is None
                else:
                    await asyncio.wait_for(recovery_started.wait(), 5)
            assert entry.runtime_data is coordinator
            assert not coordinator._tearing_down
            if restore_after_retry:
                assert restored == ["production_evidence_contract_changed"]
                assert failed_writes == ["waiting_for_safe"]
        finally:
            finish_reconcile.set()
            if unload_task is not None and not unload_task.done():
                await asyncio.gather(unload_task, return_exceptions=True)
            monkeypatch.undo()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("draws", [(3, 5), (5, 3)])
@pytest.mark.parametrize("intervening_draws", [(), (5,), (5, 3), (3, 5)])
def test_equal_timestamp_conflicts_preserve_reservation_in_either_order(tmp_path, draws, intervening_draws):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        try:
            store = PlannerStore(hass, "runtime")
            executor = _executor(hass, store)
            executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
            executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9)
            hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
            sampled_at = dt_util.utcnow() - timedelta(seconds=2)
            pending = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
                "at": (sampled_at - timedelta(seconds=1)).isoformat(),
            }
            executor._ev_grid_reservations()[executor.entry_id] = {"load_kw": 9, "pending_power_limit": pending}
            samples = [(draws[0], sampled_at)]
            samples.extend((draw, sampled_at + timedelta(seconds=1)) for draw in intervening_draws)
            samples.append((draws[1], sampled_at))
            for draw, source_time in samples:
                hass.states.async_set("sensor.ev_power", draw, {
                    "unit_of_measurement": "kW", "sampled_at_utc": source_time.isoformat(),
                })
                executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
                await executor.async_persist_ev_grid_reservation()
            saved = PlannerStore(hass, "runtime")
            await saved.async_load()
            assert saved.data["ev_grid_reservation"]["load_kw"] == 9
            assert saved.data["ev_grid_reservation"]["pending_power_limit"] == pending

            hass.states.async_set("sensor.ev_power", 3, {
                "unit_of_measurement": "kW", "sampled_at_utc": dt_util.utcnow().isoformat(),
            })
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
            assert executor._ev_grid_reservations()[executor.entry_id]["load_kw"] == 3
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("restart", "prior_cap"), [
    ("none", 3), ("external", 3), ("owned", 3), ("remapped", 3), ("missing_report", 3), ("malformed", 3),
    ("none", 7), ("external", 7), ("oversized", 7),
])
def test_ev_adapter_confirmation_preserves_prior_peak_from_same_report(tmp_path, restart, prior_cap):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9)
        power_attrs = {"unit_of_measurement": "kW"}
        cap_attrs = {**power_attrs, "min": 1, "max": 9, "step": 1}
        hass.states.async_set("number.limit", 4 if prior_cap == 3 else prior_cap, cap_attrs)
        hass.states.async_set("sensor.ev_power", 9, power_attrs)
        cap = {"entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3}
        earlier = {
            **cap, "value": prior_cap, "physical_power_kw": prior_cap,
            "version": 1, "at": (dt_util.utcnow() - timedelta(seconds=1)).isoformat(),
        }
        reservations = executor._ev_grid_reservations()
        reservations["runtime"] = {"load_kw": 9, "pending_power_limit": earlier}
        if restart == "owned":
            reservations["runtime"].update(
                confirmed_power_at=(dt_util.utcnow() - timedelta(seconds=2)).isoformat(), confirmed_cap_kw=9,
            )
        plan, context = _command("ev")
        action = plan.actions[0]
        action.desired_state.update(projected_load_kw_now=3, power_limit=cap)
        context.slots[0].projected_ev_load_kw = 3

        async def write_cap(call):
            if prior_cap == 3:
                hass.states.async_set("number.limit", call.data["value"], cap_attrs)
            hass.states.async_set("sensor.ev_power", 3, {**power_attrs, "sampled_at_utc": dt_util.utcnow().isoformat()})
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
            assert reservations["runtime"]["cap_reduction"]["prior_load_kw"] == 9
            if prior_cap != 3:
                hass.states.async_set("number.limit", call.data["value"], cap_attrs)

        hass.services.async_register("number", "set_value", write_cap)
        try:
            reason, previous = executor._reserve_ev_grid_capacity(action, context, dt_util.utcnow())
            assert reason is None and previous is not None
            adapter = EVChargerAdapter(hass, executor.entry_data, power_options=executor.options,
                                       confirmation_timeout_seconds=0, confirmation_retries=0)
            result = await adapter.async_execute(action)
            assert result.applied and result.power_limit_written
            reported = result.post_state["confirmed_power_at"]
            executor._reconcile_ev_grid_reservation(action, result, previous)
            if restart != "none":
                if restart == "external":
                    executor._retain_external_ev_grid_reservation()
                elif restart == "remapped":
                    await store.async_save_ownership({
                        "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
                        "ev_smart_charging_control_topology": {
                            "ev_power_entity": "sensor.ev_power", "ev_power_limit_entity": "number.limit",
                        },
                    })
                    executor.entry_data["ev_power_entity"] = "sensor.replacement"
                await executor.async_persist_ev_grid_reservation()
                saved = PlannerStore(hass, "runtime")
                await saved.async_load()
                serialized = saved.data["ev_grid_reservation"]
                if restart == "missing_report":
                    serialized.pop("observed_load_at")
                    serialized["cap_reduction"]["history"] = [None]
                elif restart == "malformed":
                    serialized["cap_reduction"].pop("pending_limit")
                elif restart == "oversized":
                    serialized["cap_reduction"]["history"][0]["retained_load_kw"] = 10**400
                reservations.clear()
                entry = SimpleNamespace(entry_id="runtime", options=executor.options)
                _rehydrate_ev_grid_reservation(hass, entry, saved.data)
                assert reservations["runtime"]["load_kw"] == {"external": 3, "malformed": 9}.get(restart, 5)
                assert "pending_power_limit" not in reservations["runtime"]
                if restart == "remapped":
                    hass.states.async_set("sensor.replacement", 5, {**power_attrs, "sampled_at_utc": reported})
                    assert not executor.note_ev_grid_power_report(hass.states.get("sensor.replacement"))
                    assert "pending_power_limit" not in reservations["runtime"]
            hass.states.async_set("sensor.ev_power", prior_cap + 1, {**power_attrs, "sampled_at_utc": reported})
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))
            assert reservations["runtime"]["load_kw"] == 9
            if restart == "malformed":
                assert "pending_power_limit" not in reservations["runtime"]
            else:
                assert reservations["runtime"]["pending_power_limit"] == result.post_state["pending_power_limit"]
            if restart == "remapped":
                hass.states.async_set("sensor.replacement", 2, power_attrs)
                replacement = hass.states.get("sensor.replacement")
                assert not executor.note_ev_grid_power_report(replacement)
                assert not executor.sync_ev_grid_reservation(power_state=replacement)
                assert reservations["runtime"]["load_kw"] == 9
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("older_first", [False, True])
@pytest.mark.parametrize("boundary", ["runtime", "restart", "rollback_after_first", "rollback_after_both"])
def test_two_disputed_ev_caps_restore_the_original_peak(tmp_path, older_first, boundary):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9, grid_import_limit_kw=20)
        reservations = executor._ev_grid_reservations()
        reservations["runtime"] = {"load_kw": 9}
        older = dt_util.utcnow() - timedelta(seconds=2)
        newer = older + timedelta(seconds=1)
        evidence_changed = []

        def report(draw, at):
            hass.states.async_set("sensor.ev_power", draw, {
                "unit_of_measurement": "kW", "sampled_at_utc": at.isoformat(),
            })
            executor.note_ev_grid_power_report(
                hass.states.get("sensor.ev_power"), on_evidence_changed=lambda: evidence_changed.append(at),
            )

        try:
            for cap, at in ((7, older), (3, newer)):
                hass.states.async_set("number.limit", cap, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
                reservations["runtime"]["pending_power_limit"] = {
                    "version": 1, "entity_id": "number.limit", "unit": "kW", "value": cap, "physical_power_kw": cap,
                    "at": (at - timedelta(milliseconds=1)).isoformat(),
                }
                report(cap, at)
                assert reservations["runtime"]["load_kw"] == cap
            samples = [(8, older), (5, newer)]
            if not older_first:
                samples.reverse()
            previous = None
            if boundary.startswith("rollback"):
                plan, context = _command("ev")
                plan.actions[0].desired_state["projected_load_kw_now"] = 12
                context.slots[0].projected_ev_load_kw = 12
                reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, context.created_at)
                assert reason is None and previous is not None
            report(*samples[0])
            expected_after_first = 3 if older_first else 7
            assert reservations["runtime"]["load_kw"] == (12 if previous else expected_after_first)
            if boundary == "restart":
                await executor.async_persist_ev_grid_reservation()
                saved = PlannerStore(hass, "runtime")
                await saved.async_load()
                reservations.clear()
                _rehydrate_ev_grid_reservation(
                    hass, SimpleNamespace(entry_id="runtime", options=executor.options), saved.data,
                )
            elif boundary == "rollback_after_first":
                await executor._async_rollback_ev_start(previous, {})
                assert reservations["runtime"]["load_kw"] == expected_after_first
            report(*samples[1])
            if boundary == "rollback_after_both":
                await executor._async_rollback_ev_start(previous, {})
            assert reservations["runtime"]["load_kw"] == 9
            assert evidence_changed == [older, newer, samples[0][1], samples[1][1]]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("trailing_cap", [False, True])
def test_ev_draw_between_cap_proofs_is_available_if_later_proof_is_disputed(tmp_path, late, trailing_cap):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=12)
        reservation = {"load_kw": 9}
        executor._ev_grid_reservations()["runtime"] = reservation
        first = dt_util.utcnow() - timedelta(seconds=3)
        peak = first + timedelta(seconds=1)
        last = peak + timedelta(seconds=1)
        trailing = last + timedelta(milliseconds=500)

        def report(draw, at):
            hass.states.async_set("sensor.ev_power", draw, {
                "unit_of_measurement": "kW", "sampled_at_utc": at.isoformat(),
            })
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

        def confirm(cap, at):
            hass.states.async_set("number.limit", cap, {"unit_of_measurement": "kW", "min": 1, "max": 12, "step": 1})
            reservation["pending_power_limit"] = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": cap, "physical_power_kw": cap,
                "at": (at - timedelta(milliseconds=1)).isoformat(),
            }
            report(cap, at)
            assert reservation["load_kw"] == cap

        try:
            confirm(7, first)
            if not late:
                report(10, peak)
            confirm(3, last)
            if trailing_cap:
                confirm(2, trailing)
            if late:
                report(10, peak)
                assert reservation["load_kw"] == (2 if trailing_cap else 3)
            if trailing_cap:
                report(4, trailing)
            report(5, last)
            assert reservation["load_kw"] == 10
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("remap_phase", ["before_attach", "live", "live_rebound", "reattach"])
def test_owned_original_ev_meter_remains_watched_until_ownership_is_released(tmp_path, remap_phase):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        original = "sensor.ev_original"
        replacement = "sensor.ev_replacement"
        entry = ConfigEntry(
            domain=DOMAIN, title="Keep watching the owned EV meter",
            data={CONF_EV_CHARGER: "switch.charger",
                  "ev_power_entity": replacement if remap_phase == "before_attach" else original,
                  "ev_power_limit_entity": "number.limit"},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 5, "ev_limit_min": 1, "ev_limit_max": 12},
            source="user", unique_id=None, version=5, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        caller = PlannerStore(hass, entry.entry_id)
        await caller.async_save_ownership({
            "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
            "ev_smart_charging_control_topology": {
                CONF_EV_CHARGER: "switch.charger", "ev_power_entity": original, "ev_power_limit_entity": "number.limit",
            },
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, caller)
        reservations = coordinator.executor._ev_grid_reservations()
        pending = {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (dt_util.utcnow() - timedelta(seconds=2)).isoformat(),
        }
        reservations[entry.entry_id] = {"load_kw": 9, "pending_power_limit": pending}
        cap_attrs = {"unit_of_measurement": "kW", "min": 1, "max": 12, "step": 1}
        hass.states.async_set("number.limit", 3, cap_attrs)
        stamp = dt_util.utcnow().isoformat()
        power_attrs = {"unit_of_measurement": "kW", "sampled_at_utc": stamp}
        hass.states.async_set(original, 3, power_attrs)
        try:
            coordinator.executor.note_ev_grid_power_report(hass.states.get(original))
            await coordinator.executor.async_persist_ev_grid_reservation()
            reservations.clear()
            _rehydrate_ev_grid_reservation(hass, entry, caller.data)
            coordinator.async_start_listeners()
            if remap_phase != "before_attach":
                object.__setattr__(entry, "data", MappingProxyType({**entry.data, "ev_power_entity": replacement}))
                if remap_phase == "reattach":
                    assert coordinator.executor.entry_data["ev_power_entity"] == original
                    coordinator._begin_shutdown()
                    coordinator.async_start_listeners()
                elif remap_phase == "live_rebound":
                    coordinator.executor.entry_data = coordinator.entry_data
                    await coordinator.executor.async_persist_ev_grid_reservation()
            assert reservations[entry.entry_id]["load_kw"] == 5
            hass.states.async_set(original, 6, power_attrs)
            hass.states.async_set(original, "unavailable")
            await hass.async_block_till_done()
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["ev_grid_reservation"]["load_kw"] == 9
            await caller.async_save_ownership({})
            hass.states.async_set(original, 11, {"unit_of_measurement": "kW"})
            hass.states.async_set(original, 11, {"unit_of_measurement": "kW"})
            await hass.async_block_till_done()
            assert reservations[entry.entry_id]["load_kw"] == 9
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_ev_cap_history_keeps_a_conservative_floor_when_trimmed():
    now = dt_util.utcnow()
    reservation = {"load_kw": 20}
    for index in range(MAX_EV_CAP_HISTORY + 3):
        cap = 19 - index
        reported = now + timedelta(seconds=index)
        reservation["pending_power_limit"] = {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": cap, "physical_power_kw": cap,
            "at": reported.isoformat(),
        }
        _record_ev_cap_confirmation(reservation, cap, reported)
    proof = reservation["cap_reduction"]
    assert len(proof["history"]) == MAX_EV_CAP_HISTORY
    for entry in [proof, *proof["history"]]:
        reported = datetime.fromisoformat(entry["confirmed_at"])
        assert _retain_ev_cap_readback(reservation, entry["cap_kw"] + .5, reported)
    assert reservation["load_kw"] == 20
    assert "cap_reduction" not in reservation


def test_ev_cap_correction_revokes_every_confirmation_from_one_sample():
    sampled_at = dt_util.utcnow()
    reservation = {"load_kw": 9}
    for cap in (7, 5, 3):
        reservation["pending_power_limit"] = {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": cap, "physical_power_kw": cap,
            "at": sampled_at.isoformat(),
        }
        _record_ev_cap_confirmation(reservation, cap, sampled_at)
    assert len(reservation["cap_reduction"]["history"]) == 2

    assert _retain_ev_cap_readback(reservation, 8, sampled_at)
    assert reservation["load_kw"] == 9
    assert "cap_reduction" not in reservation


@pytest.mark.parametrize("earlier_confirmation", [False, True])
@pytest.mark.parametrize(("reconfirmed", "command_sent"), [(False, False), (True, False), (False, True)])
def test_ev_start_rollback_keeps_disputed_reduction_evidence(tmp_path, earlier_confirmation, reconfirmed, command_sent):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        try:
            store = PlannerStore(hass, "runtime")
            executor = _executor(hass, store)
            executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
            executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9, grid_import_limit_kw=20)
            hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
            sampled_at = dt_util.utcnow() - timedelta(seconds=2)
            command_at = sampled_at - timedelta(seconds=1)
            pending = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
                "at": command_at.isoformat(),
            }
            reservation = {"load_kw": 9, "pending_power_limit": pending}
            older_proof = (command_at - timedelta(seconds=1)).isoformat() if earlier_confirmation else None
            if older_proof is not None:
                reservation["confirmed_power_at"] = older_proof
            reservations = executor._ev_grid_reservations()
            reservations["runtime"] = reservation

            def report(draw, source_time):
                hass.states.async_set("sensor.ev_power", draw, {
                    "unit_of_measurement": "kW", "sampled_at_utc": source_time.isoformat(),
                })
                executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

            report(3, sampled_at)
            assert reservations["runtime"]["load_kw"] == 3
            plan, context = _command("ev")
            action = plan.actions[0]
            proposed_load = 12 if reconfirmed else 3
            action.desired_state.update(projected_load_kw_now=proposed_load, power_limit={**pending})
            context.slots[0].projected_ev_load_kw = proposed_load
            reason, previous = executor._reserve_ev_grid_capacity(action, context, context.created_at)
            assert reason is None and previous["confirmed_power_at"] == sampled_at.isoformat()
            report(5, sampled_at)
            latest = sampled_at + timedelta(seconds=1)
            assert reservations["runtime"]["load_kw"] == max(proposed_load, 9)
            report(3 if reconfirmed else 4, latest)

            await executor._async_rollback_ev_start(previous, {}, command_sent=command_sent)
            if reconfirmed:
                assert reservations["runtime"]["load_kw"] == 3
                report(4, latest)
                await executor.async_persist_ev_grid_reservation()
            saved = PlannerStore(hass, "runtime")
            await saved.async_load()
            retained = saved.data["ev_grid_reservation"]
            assert retained["load_kw"] == 9 and retained["pending_power_limit"] == pending
            assert retained.get("confirmed_power_at") == older_proof
            assert retained["observed_load_at"] == latest.isoformat() and retained["observed_report_kw"] == 4
            assert "cap_reduction" not in retained
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_ordinary_unload_disarms_an_entry_without_pending_startup_recovery(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        entry = ConfigEntry(
            domain=DOMAIN, title="Ordinary unload", data={}, options=DEFAULT_OPTIONS, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        object.__setattr__(entry, "runtime_data", coordinator)
        _arm(store, coordinator.executor)

        async def unload_platforms(_entry, _platforms):
            return True

        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)
        coordinator.async_start_listeners()
        try:
            assert await async_unload_entry(hass, entry)
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["production"]["armed"] is False
            assert "startup_auto_recovery" not in saved.data["production"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("fault", [
    "terminal_save_succeeds", "terminal_save_fails", "ev_bookkeeping_fails_once", "terminal_during_shutdown",
    "restart", "restart_unavailable", "restart_available_during_restore",
    "ownership_ack_once", "ownership_ack_on_unload",
])
def test_terminal_startup_failure_restores_owned_enphase_profile(tmp_path, monkeypatch, fault):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        available_during_restore = fault == "restart_available_during_restore"
        waiting_for_entity = available_during_restore or fault == "restart_unavailable"
        combined_ownership = fault in {"terminal_save_fails", "ev_bookkeeping_fails_once"}
        shutting_down = fault == "terminal_during_shutdown"
        hass.state = CoreState.starting if waiting_for_entity else CoreState.running
        calls = _devices(hass)
        hass.states.async_set("select.profile", "unavailable" if waiting_for_entity else "Self Consumption")
        devices = {CONF_ENPHASE_PROFILE: "select.profile"}
        if combined_ownership:
            devices[CONF_EV_CHARGER] = "switch.charger"
            hass.states.async_set("switch.charger", "on")
        entry = ConfigEntry(
            domain=DOMAIN, title="Restore profile after startup failure",
            data=devices,
            options={**DEFAULT_OPTIONS, "dry_run": False, "planner_enabled": True,
                     "enphase_control_enabled": True, "ev_control_enabled": combined_ownership},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        restarting = waiting_for_entity or fault == "restart"
        if restarting:
            store.data["production"] = {"armed": False, "startup_auto_recovery": {
                "status": "failed", "last_reason": "startup_recovery_save_failed",
            }}
        else:
            _arm(store, coordinator.executor)
            coordinator._startup_auto_recovery_authorized = True
        ownership = {"enphase_profile": "Original profile"}
        if combined_ownership:
            ownership["ev_smart_charging_state"] = {CONF_EV_CHARGER: "off"}
            store.data["ev_telemetry"] = {"command_exposure": {"reserved_kw": 7}}
        await store.async_save_ownership(ownership)
        backend_save = store._store.async_save
        adapter_attempted, finish_attempt = asyncio.Event(), asyncio.Event()
        restore_device = coordinator.executor.async_restore_safe_state
        restore_attempts = 0
        failed_ev_write = False
        ownership_write_failures = {"ownership_ack_once": 1, "ownership_ack_on_unload": 3}.get(fault, 0)
        failed_ownership_writes = 0

        async def save(data):
            nonlocal failed_ev_write, failed_ownership_writes
            status = data["production"].get("startup_auto_recovery", {}).get("status")
            if status == "grace" or fault == "terminal_save_fails":
                raise OSError("simulated startup Store failure")
            if shutting_down and status == "failed":
                coordinator._tearing_down = True
            if (fault == "ev_bookkeeping_fails_once" and not failed_ev_write
                    and "command_exposure" not in data["ev_telemetry"]):
                failed_ev_write = True
                raise OSError("simulated EV bookkeeping failure")
            if status == "failed" and not data["ownership"] and failed_ownership_writes < ownership_write_failures:
                failed_ownership_writes += 1
                raise OSError("simulated ownership acknowledgement failure")
            await backend_save(data)

        async def restore_then_wait(*args, **kwargs):
            nonlocal restore_attempts
            restore_attempts += 1
            result = await restore_device(*args, **kwargs)
            if restore_attempts == 2:
                adapter_attempted.set()
                await finish_attempt.wait()
            return result

        monkeypatch.setattr(store._store, "async_save", save)
        if available_during_restore:
            monkeypatch.setattr(coordinator.executor, "async_restore_safe_state", restore_then_wait)
        try:
            if restarting:
                assert await coordinator.async_reconcile_production_evidence_contract()
                if waiting_for_entity:
                    assert store.data["ownership"]["enphase_profile"] == "Original profile"
                    coordinator.async_start_listeners()
                    hass.state = CoreState.running
                    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
                    if available_during_restore:
                        await asyncio.wait_for(adapter_attempted.wait(), 5)
                    else:
                        await hass.async_block_till_done()
                    assert calls == []
                    hass.states.async_set("select.profile", "Self Consumption")
                    finish_attempt.set()
                    await hass.async_block_till_done()
            else:
                coordinator.async_start_startup_auto_recovery()
                task = coordinator._startup_auto_recovery_task
                if task is not None:
                    await asyncio.wait_for(task, 5)
            if fault == "ownership_ack_on_unload":
                await hass.async_block_till_done()
                assert coordinator._failed_startup_restore_flush_needed
                stale = PlannerStore(hass, entry.entry_id)
                await stale.async_load()
                assert stale.data["ownership"] == ownership
                coordinator._begin_shutdown()
                await coordinator.async_wait_for_refresh_shutdown()
            assert failed_ownership_writes == ownership_write_failures
            assert not coordinator._failed_startup_restore_flush_needed or fault == "terminal_save_fails"
            assert not coordinator._startup_auto_recovery_authorized
            assert coordinator._startup_auto_recovery_task is None
            expected = [("select", "select_option", {"entity_id": "select.profile", "option": "Original profile"})]
            if combined_ownership:
                expected.insert(0, ("switch", "turn_off", {"entity_id": "switch.charger"}))
                assert hass.states.get("switch.charger").state == "off"
            assert calls == ([] if shutting_down else expected)
            expected_profile = "Self Consumption" if shutting_down else "Original profile"
            assert hass.states.get("select.profile").state == expected_profile
            if fault != "terminal_save_fails":
                saved = PlannerStore(hass, entry.entry_id)
                await saved.async_load()
                assert saved.data["production"]["armed"] is False
                assert saved.data["production"]["startup_auto_recovery"]["status"] == "failed"
                assert bool(saved.data["ownership"]) is shutting_down
        finally:
            finish_attempt.set()
            monkeypatch.undo()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("restore_target", ["topology", "command_entity", "reservation_only", "legacy_empty_state"])
def test_terminal_startup_restore_watches_charger_used_by_restoration(tmp_path, restore_target):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "unavailable")
        hass.states.async_set("switch.replacement", "off")
        reservation_only = restore_target in {"reservation_only", "legacy_empty_state"}
        mapped_charger = "switch.charger" if reservation_only else "switch.replacement"
        entry = ConfigEntry(
            domain=DOMAIN, title="Restore original charger after startup failure",
            data={CONF_EV_CHARGER: mapped_charger},
            options={**DEFAULT_OPTIONS, "ev_control_enabled": True}, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        store.data["production"] = {"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }}
        if reservation_only:
            await store.async_save_ev_grid_reservation({"active": True, "load_kw": 7, "limit_kw": 10})
            if restore_target == "legacy_empty_state":
                await store.async_save_ownership({"ev_smart_charging_state": []})
                store = PlannerStore(hass, entry.entry_id)
                await store.async_load()
            _rehydrate_ev_grid_reservation(hass, entry, store.data)
        else:
            ownership = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}
            ownership["ev_smart_charging_control_topology"] = {CONF_EV_CHARGER: "switch.charger"}
            if restore_target == "command_entity":
                ownership["ev_smart_charging_command_entity_id"] = "switch.charger"
            await store.async_save_ownership(ownership)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        try:
            assert await coordinator.async_reconcile_production_evidence_contract()
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            assert not calls
            assert store.data["ownership"] or store.data["ev_grid_reservation"]["active"]
            hass.states.async_set("switch.charger", "on")
            await hass.async_block_till_done()
            assert calls == [("switch", "turn_off", {"entity_id": "switch.charger"})]
            assert not store.data["ownership"]
            assert store.data["ev_grid_reservation"] == {"active": False}
            assert coordinator._failed_startup_restore_watch_cancel is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("direct_shutdown", [False, True])
def test_shutdown_drains_dispatched_terminal_startup_restore(tmp_path, monkeypatch, direct_shutdown):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        hass.state = CoreState.running
        _devices(hass)
        hass.states.async_set("select.profile", "Self Consumption")
        entry = ConfigEntry(
            domain=DOMAIN, title="Drain terminal startup restore", data={CONF_ENPHASE_PROFILE: "select.profile"},
            options={**DEFAULT_OPTIONS, "dry_run": False, "planner_enabled": True, "enphase_control_enabled": True},
            source="user", unique_id=None, version=5, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        object.__setattr__(entry, "runtime_data", coordinator)
        _arm(store, coordinator.executor)
        coordinator._startup_auto_recovery_authorized = True
        await store.async_save_ownership({"enphase_profile": "Original profile"})
        backend_save = store._store.async_save
        dispatched, finish_service, platforms_unloaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
        unload = None

        async def save(data):
            if data["production"].get("startup_auto_recovery", {}).get("status") == "grace":
                raise OSError("simulated startup Store failure")
            await backend_save(data)

        async def restore_profile(call):
            if not direct_shutdown:
                hass.states.async_set(call.data["entity_id"], call.data["option"], context=call.context)
            dispatched.set()
            await finish_service.wait()
            if direct_shutdown:
                raise HomeAssistantError("Simulated restore failure")

        async def unload_platforms(_entry, _platforms):
            assert not store.data["ownership"]
            platforms_unloaded.set()
            return True

        monkeypatch.setattr(store._store, "async_save", save)
        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)
        hass.services.async_register("select", "select_option", restore_profile)
        try:
            coordinator.async_start_startup_auto_recovery()
            await asyncio.wait_for(dispatched.wait(), 5)
            if direct_shutdown:
                startup = coordinator._startup_auto_recovery_task
                assert startup is not None
                await coordinator.async_shutdown()
                finish_service.set()
                await asyncio.wait_for(asyncio.gather(startup, return_exceptions=True), 5)
                assert coordinator._failed_startup_restore_watch_cancel is None
                assert not coordinator._unsub_listeners
                assert store.data["ownership"] == {"enphase_profile": "Original profile"}
                return
            hass.state = CoreState.stopping
            unload = asyncio.create_task(async_unload_entry(hass, entry))
            await asyncio.sleep(0)
            assert not unload.done() and not platforms_unloaded.is_set()
            assert coordinator._command_lock.locked()
            finish_service.set()
            assert await asyncio.wait_for(unload, 5)
            assert platforms_unloaded.is_set()
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["production"]["startup_auto_recovery"]["status"] == "failed"
            assert not saved.data["ownership"]
            assert saved.data["execution_audit"][-1]["result"] == "restored"
        finally:
            finish_service.set()
            if unload is not None and not unload.done():
                await asyncio.gather(unload, return_exceptions=True)
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["unit_of_measurement", "max"])
def test_terminal_ev_restore_retries_when_native_limit_readiness_arrives(tmp_path, missing):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 10}
        hass.states.async_set("number.limit", 3, {key: value for key, value in attributes.items() if key != missing})
        hass.states.async_set("sensor.household", "unavailable")
        entry = ConfigEntry(
            domain=DOMAIN, title="Restore charger cap after unit feedback",
            data={CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit",
                  CONF_HOUSEHOLD_LOAD: "sensor.household"},
            options=DEFAULT_OPTIONS, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_production({"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }})
        await store.async_save_ownership({"ev_smart_charging_state": {
            CONF_EV_CHARGER: "off", "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        coordinator = EnergyPlannerCoordinator(hass, entry, store)

        async def set_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], attributes)

        hass.services.async_register("number", "set_value", set_limit)
        try:
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            assert not calls and store.data["ownership"]
            before = list(store.data["execution_audit"])
            hass.states.async_set("sensor.household", 1500, {"unit_of_measurement": "W"})
            await hass.async_block_till_done()
            assert not calls and store.data["execution_audit"] == before
            hass.states.async_set("number.limit", 3, attributes)
            await hass.async_block_till_done()
            assert calls == [("number", "set_value", {"entity_id": "number.limit", "value": 9})], (
                [item["reason"] for item in store.data["execution_audit"]],
            )
            assert not store.data["ownership"]
            assert coordinator._failed_startup_restore_watch_cancel is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_terminal_partial_release_ignores_feedback_from_the_released_asset(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        hass.states.async_set("select.profile", "unavailable")
        hass.states.async_set("number.limit", "unavailable")
        entry = ConfigEntry(
            domain=DOMAIN, title="Ignore readiness of a released asset", data={
                CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit",
                CONF_ENPHASE_PROFILE: "select.profile",
            }, options=DEFAULT_OPTIONS, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_production({"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }})
        ev_baseline = {CONF_EV_CHARGER: "off", "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"}
        await store.async_save_ownership({
            "ev_smart_charging_state": ev_baseline, "enphase_profile": "Original profile",
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        try:
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            hass.states.async_set("select.profile", "Self Consumption")
            await hass.async_block_till_done()
            assert calls == [("select", "select_option", {"entity_id": "select.profile", "option": "Original profile"})]
            assert store.data["ownership"] == {"ev_smart_charging_state": ev_baseline}
            before = list(store.data["execution_audit"])
            for _ in range(3):
                hass.states.async_set("select.profile", "unavailable")
                hass.states.async_set("select.profile", "Self Consumption")
                await hass.async_block_till_done()
            assert store.data["execution_audit"] == before
            assert coordinator._failed_startup_restore_watch_cancel is not None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("feedback", "capacity_saved"), [
    ("idle", False), ("finishing", True), ("unplugged", True), ("switch_off", True), ("charging_resumed", True),
    ("shared_feedback", True), ("valid_clock", True), ("implicit_clock", True),
])
def test_terminal_ev_restore_retries_when_stop_feedback_arrives(tmp_path, monkeypatch, feedback, capacity_saved):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        connected = "binary_sensor.connected"
        charging = "sensor.charging"
        control = "switch.charger"
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 10}
        pending_switch = feedback in {"switch_off", "charging_resumed"}
        if feedback in {"valid_clock", "implicit_clock"}:
            future = dt_util.utcnow() + timedelta(minutes=1)
            hass.states.async_set(charging, "idle", {"sampled_at_utc": future.isoformat()})
        else:
            hass.states.async_set(charging, "idle" if pending_switch else "charging")
        hass.states.async_set(connected, "plugged_in" if feedback == "unplugged" else "connected")
        hass.states.async_set("number.limit", 3, attributes)
        hass.states.async_set("binary_sensor.replacement", "on")
        entry = ConfigEntry(
            domain=DOMAIN, title="Restore cap after delayed charger stop",
            data={CONF_EV_CHARGER: control, CONF_EV_CHARGING: charging,
                  CONF_EV_CONNECTED: "binary_sensor.replacement", "ev_power_limit_entity": "number.limit"},
            options={**DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 0},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_production({"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }})
        await store.async_save_ownership({
            "ev_smart_charging_state": {
                CONF_EV_CHARGER: "off", "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
            },
            "ev_smart_charging_control_topology": {
                CONF_EV_CHARGER: control, CONF_EV_CHARGING: charging, "ev_power_limit_entity": "number.limit",
            },
            "ev_smart_charging_capacity_inputs": {
                CONF_EV_CONNECTED: charging if feedback == "shared_feedback" else connected,
            } if capacity_saved else None,
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        reservations = coordinator.executor._ev_grid_reservations()
        reservations[entry.entry_id] = {"load_kw": 3}

        async def delayed_stop(_call):
            """Leave feedback unchanged until the test publishes it."""

        async def set_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], attributes)

        confirm_state = EVChargerAdapter._async_confirm_state
        simulate_resumption = feedback == "charging_resumed"

        async def confirm_while_charging_resumes(adapter, entity_id, enabled, **kwargs):
            nonlocal simulate_resumption
            if entity_id == charging and simulate_resumption:
                simulate_resumption = False
                if not kwargs.get("stop_control"):
                    confirmed = await confirm_state(adapter, entity_id, enabled, **kwargs)
                    hass.states.async_set(charging, "charging")
                    hass.states.async_set(control, "off")
                    return confirmed
                hass.states.async_set(charging, "charging")
                hass.states.async_set(control, "off")
            return await confirm_state(adapter, entity_id, enabled, **kwargs)

        hass.services.async_register("number", "set_value", set_limit)
        if pending_switch:
            hass.states.async_set(control, "on")
            hass.services.async_register("switch", "turn_off", delayed_stop)
        if simulate_resumption:
            monkeypatch.setattr(EVChargerAdapter, "_async_confirm_state", confirm_while_charging_resumes)
        try:
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            assert store.data["ownership"] and hass.states.get("number.limit").state == "3", (
                calls, hass.states.get(control), store.data["execution_audit"],
            )
            assert reservations[entry.entry_id]["load_kw"] == 3
            assert coordinator._failed_startup_restore_watch_cancel is not None
            if feedback == "unplugged":
                before = list(calls)
                hass.states.async_set(connected, "connected_not_charging")
                await hass.async_block_till_done()
                hass.states.async_set(connected, "connected")
                hass.states.async_set("binary_sensor.replacement", "unplugged")
                await hass.async_block_till_done()
                assert calls == before and store.data["ownership"] and hass.states.get("number.limit").state == "3"
            entity_id, state = {
                "idle": (charging, "idle"), "finishing": (charging, "finishing"),
                "unplugged": (connected, "unplugged"), "switch_off": (control, "off"),
                "charging_resumed": (charging, "idle"),
                "shared_feedback": (charging, "finishing"),
                "valid_clock": (charging, "idle"),
                "implicit_clock": (charging, "idle"),
            }[feedback]
            proof = {"sampled_at_utc": dt_util.utcnow().isoformat()} if feedback == "valid_clock" else None
            hass.states.async_set(entity_id, state, proof)
            await hass.async_block_till_done()
            assert calls[-1:] == [("number", "set_value", {"entity_id": "number.limit", "value": 9})]
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert not saved.data["ownership"]
            assert entry.entry_id not in reservations
            assert coordinator._failed_startup_restore_watch_cancel is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("readback_order", [
    "delayed", "native_write", "release_save", "active_baseline", "meter_only", "meter_lost", "stale_stop",
    "future_stop", "implicit_transition", "cap_changed", "unit_changed", "exposure_conflict", "cap_then_draw",
])
def test_terminal_ev_restore_reconciles_delayed_cap_readback_without_another_write(
    tmp_path, monkeypatch, readback_order,
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 10}
        starts_restored = readback_order in {"cap_changed", "unit_changed", "exposure_conflict", "cap_then_draw"}
        hass.states.async_set("number.limit", 9 if starts_restored else 3, attributes)
        hass.states.async_set("sensor.charging", "idle")
        hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
        entry = ConfigEntry(
            domain=DOMAIN, title="Confirm delayed cap restoration", data={
                CONF_EV_CHARGER: "switch.charger", CONF_EV_CHARGING: "sensor.charging",
                CONF_EV_CONNECTED: "binary_sensor.connected",
                "ev_power_limit_entity": "number.limit", "ev_power_entity": "sensor.ev_power",
            }, options={
                **DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 0,
            }, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_production({"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }})
        await store.async_save_ownership({"ev_smart_charging_state": {
            CONF_EV_CHARGER: "on" if readback_order == "active_baseline" else "off",
            "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        coordinator = EnergyPlannerCoordinator(hass, entry, store)

        async def no_refresh():
            pass

        monkeypatch.setattr(coordinator, "async_request_refresh", no_refresh)
        reservations = coordinator.executor._ev_grid_reservations()
        reservations[entry.entry_id] = {"load_kw": 7}

        def restart_charging():
            hass.states.async_set("switch.charger", "on")
            hass.states.async_set("sensor.charging", "charging")
            hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})

        async def delayed_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            if readback_order == "native_write":
                restart_charging()
                await asyncio.sleep(0)
            if readback_order != "delayed":
                hass.states.async_set("number.limit", 9, attributes)

        def number_calls():
            return [call for call in calls if call[0] == "number"]

        deadline = dt_util.utcnow() + timedelta(minutes=5)
        exposure = {"at": (deadline - timedelta(minutes=6)).isoformat(), "cost_per_hour": 3, "reserved_kw": 7}
        await store.async_save_ev_telemetry({"command_exposure": exposure, "last_sample": {"reserved_kw": 7}})
        coordinator._schedule_ev_allocation_deadline(deadline)
        if readback_order not in {"delayed", "native_write"}:
            save = store._store.async_save
            interrupted = False
            exposure_changes = 0

            async def save_after_restart(data):
                nonlocal interrupted, exposure_changes
                proposal = data.get("_pending_ev_release")
                if proposal and readback_order == "exposure_conflict" and exposure_changes < 2:
                    exposure_changes += 1
                    with store.defer_save():
                        await store.async_save_ev_telemetry({
                            "command_exposure": exposure,
                            "last_sample": {"reserved_kw": 7, "power_kw": exposure_changes},
                        })
                if proposal and not interrupted:
                    interrupted = True
                    assert proposal.get("ev_grid_reservation") == (
                        None if readback_order == "active_baseline" else {"active": False}
                    )
                    assert proposal["ownership"] == {} and "command_exposure" not in proposal["ev_telemetry"]
                    assert data["ownership"] and data["ev_grid_reservation"]["active"]
                    assert data["ev_telemetry"]["command_exposure"] == exposure
                    if readback_order in {"cap_changed", "cap_then_draw"}:
                        hass.states.async_set("number.limit", 3, attributes)
                    elif readback_order == "unit_changed":
                        hass.states.async_set("number.limit", 9, {**attributes, "unit_of_measurement": "A"})
                    elif readback_order in {
                        "meter_only", "meter_lost", "stale_stop", "future_stop", "implicit_transition",
                    }:
                        hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                        await asyncio.sleep(0)
                        if readback_order == "meter_lost":
                            hass.states.async_set("sensor.ev_power", "unavailable")
                        elif readback_order in {"stale_stop", "future_stop"}:
                            offset = 1 if readback_order == "future_stop" else -1
                            source_stamp = dt_util.utcnow() + timedelta(minutes=offset)
                            hass.states.async_set(
                                "sensor.charging", "idle", {"sampled_at_utc": source_stamp.isoformat()},
                            )
                    elif readback_order != "exposure_conflict":
                        restart_charging()
                    await asyncio.sleep(0)
                elif proposal and readback_order == "cap_then_draw" and "ownership" not in proposal:
                    hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                    await asyncio.sleep(0)
                await save(data)

            monkeypatch.setattr(store._store, "async_save", save_after_restart)

        hass.services.async_register("number", "set_value", delayed_limit)
        try:
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            expected = [] if starts_restored else [("number", "set_value", {"entity_id": "number.limit", "value": 9})]
            assert number_calls() == expected and store.data["ownership"]
            new_draw = (not starts_restored and readback_order != "delayed") or readback_order == "cap_then_draw"
            assert reservations[entry.entry_id]["load_kw"] >= (8 if new_draw else 7)
            if readback_order in {"delayed", "cap_changed", "unit_changed"}:
                assert "command_exposure" not in store.data["ev_telemetry"]
                assert coordinator._ev_allocation_deadline is None
            else:
                assert store.data["ev_telemetry"]["command_exposure"] == exposure
                assert coordinator._ev_allocation_deadline == deadline
            if new_draw:
                saved = PlannerStore(hass, entry.entry_id)
                await saved.async_load()
                assert saved.data["ev_grid_reservation"]["load_kw"] >= 8 and saved.data["ownership"]
                if readback_order == "meter_only":
                    stale = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
                    hass.states.async_set("sensor.charging", "idle", {"status_detail": "connected"})
                    hass.states.async_set("sensor.ev_power", 0)
                    hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW", "sampled_at_utc": stale})
                    await hass.async_block_till_done()
                    assert store.data["ownership"] and reservations[entry.entry_id]["load_kw"] >= 8
                if readback_order in {"stale_stop", "future_stop"}:
                    hass.states.async_set("sensor.charging", "idle", {"sampled_at_utc": dt_util.utcnow().isoformat()})
                elif readback_order == "implicit_transition":
                    hass.states.async_set("sensor.charging", "finishing")
                else:
                    if readback_order == "cap_then_draw":
                        hass.states.async_set("number.limit", 9, attributes)
                    hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
                    hass.states.async_set("sensor.charging", "idle")
                    hass.states.async_set("switch.charger", "off")
            else:
                if readback_order == "delayed":
                    hass.states.async_set("number.limit", 4, attributes)
                    await hass.async_block_till_done()
                    assert calls == expected and store.data["ownership"]
                final_attributes = {**attributes, "max": 11} if readback_order == "exposure_conflict" else attributes
                hass.states.async_set("number.limit", 9, final_attributes)
            await hass.async_block_till_done()
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert number_calls() == expected and not saved.data["ownership"]
            assert "command_exposure" not in saved.data["ev_telemetry"]
            assert saved.data["ev_telemetry"]["emergency_spend"] > 0
            assert coordinator._ev_allocation_deadline is None
            if readback_order == "active_baseline":
                assert reservations[entry.entry_id][EV_RESERVATION_EXTERNAL_BASELINE] is True
                assert saved.data["ev_grid_reservation"][EV_RESERVATION_EXTERNAL_BASELINE] is True
            else:
                assert entry.entry_id not in reservations
            assert coordinator._failed_startup_restore_watch_cancel is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("source", [
    "during_shutdown", "shutdown_drain", "inactive_shutdown_drain", "interrupted_drain", "cancelled_drain",
    "reload_interrupted", "reload_platforms", "reload_save_failure", "reload_save_cancelled", "reload_later_draw",
    "reload_store_outage", "reload_cancel_platforms", "shutdown_platforms", "binary_handoff", "cap_handoff",
    "cap_late_draw_handoff", "cap_zero_handoff", "external_activation", "regular_meter", "direct_resume",
    "retained_meter", "canonical_shutdown", "rehydrated", "during_manual", "canonical_manual", "manual_rehydrated",
    "before_stage_manual", "active_shutdown_drain",
])
def test_native_release_retains_latest_draw_during_unload_and_manual_stop(tmp_path, monkeypatch, source):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        hass.config_entries = ConfigEntries(hass, {})
        calls = _devices(hass)
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 12, "step": 1}
        cap_proof_handoff = source in {"cap_late_draw_handoff", "cap_zero_handoff"}
        hass.states.async_set("number.limit", 3, attributes)
        initial_power = "unavailable" if cap_proof_handoff or source == "external_activation" else 0
        hass.states.async_set("sensor.ev_power", initial_power, {"unit_of_measurement": "kW"})
        power_entity, limit_entity = "sensor.ev_power", "number.limit"
        if source == "retained_meter":
            power_entity, limit_entity = "sensor.replacement_power", "number.replacement_limit"
            hass.states.async_set(power_entity, 0, {"unit_of_measurement": "kW"})
            hass.states.async_set(limit_entity, 4, attributes)
        options = {**DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0}
        if source == "shutdown_platforms":
            options.update(planner_enabled=True, dry_run=False)
        if source in {"cap_handoff", "external_activation"} or cap_proof_handoff:
            options.update(ev_limit_min=1, ev_limit_max=12)
        entry = ConfigEntry(
            domain=DOMAIN, title="Retain late draw during unload", data={
                CONF_EV_CHARGER: "switch.charger", "ev_power_entity": power_entity,
                **({} if source == "binary_handoff" else {"ev_power_limit_entity": limit_entity}),
            }, options=options, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        ownership = {"ev_smart_charging_state": {
            CONF_EV_CHARGER: "off", "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }}
        if source == "binary_handoff":
            ownership["ev_smart_charging_state"] = {CONF_EV_CHARGER: "off"}
        if source == "external_activation":
            ownership = {}
        if source == "retained_meter":
            ownership["ev_smart_charging_control_topology"] = {
                CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit",
                "ev_power_entity": "sensor.ev_power",
            }
        await store.async_save_ownership(ownership)
        rehydrated = source in {"rehydrated", "manual_rehydrated"}
        if rehydrated:
            hass.states.async_set("sensor.ev_power", "unavailable")
            await store.async_save_ev_grid_reservation({
                "active": True, "load_kw": 11, "observed_load_at": dt_util.utcnow().isoformat(),
            })
            store = PlannerStore(hass, entry.entry_id)
            await store.async_load()
            _rehydrate_ev_grid_reservation(hass, entry, store.data)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        object.__setattr__(entry, "runtime_data", coordinator)
        hass.config_entries._entries[entry.entry_id] = entry
        reservations = coordinator.executor._ev_grid_reservations()
        if source == "external_activation":
            reservations[entry.entry_id] = {
                "load_kw": 3, "reserved_at": dt_util.utcnow().isoformat(), EV_RESERVATION_EXTERNAL_BASELINE: True,
                "external_topology": {**entry.data, CONF_EV_CONNECTED: "binary_sensor.connected"},
                "external_power_options": {"ev_limit_min": 1, "ev_limit_max": 12},
            }
            await coordinator.executor.async_persist_ev_grid_reservation()
        elif source in {"inactive_shutdown_drain", "regular_meter", "retained_meter", "shutdown_platforms"}:
            await store.async_save_ev_grid_reservation({"active": False})
        elif not rehydrated:
            reservations[entry.entry_id] = {"load_kw": 7}
            if source != "before_stage_manual":
                await store.async_save_ev_grid_reservation({"active": True, "load_kw": 7})
        cap_confirmed_at = dt_util.utcnow() - timedelta(seconds=2)
        if cap_proof_handoff:
            cap_reservation = reservations[entry.entry_id]
            cap_reservation["pending_power_limit"] = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
                "at": (cap_confirmed_at - timedelta(seconds=1)).isoformat(),
            }
            if source == "cap_late_draw_handoff":
                _record_ev_cap_confirmation(cap_reservation, 3, cap_confirmed_at)
                cap_reservation.update(observed_load_at=cap_confirmed_at.isoformat(), observed_report_kw=3)
            await coordinator.executor.async_persist_ev_grid_reservation()
        save = store._store.async_save
        assert coordinator.executor._ev_unload_draw is None
        reload_platforms = source in {
            "reload_platforms", "reload_save_failure", "reload_save_cancelled", "reload_later_draw",
            "reload_store_outage", "reload_cancel_platforms", "binary_handoff", "cap_handoff", "cap_late_draw_handoff",
            "cap_zero_handoff", "external_activation",
        }
        preserve_platforms = reload_platforms or source == "shutdown_platforms"
        platforms_unloaded = write_interrupted = False
        outage_attempts = 0
        unload_task = None

        async def set_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], attributes)

        async def publish_draw(power=11, sampled_at=None):
            power_attrs = {"unit_of_measurement": "kW"}
            if sampled_at is not None:
                power_attrs["sampled_at_utc"] = sampled_at.isoformat()
            hass.states.async_set("sensor.ev_power", power, power_attrs)
            await asyncio.sleep(0)
            hass.states.async_set("sensor.ev_power", "unavailable")

        async def interleave(data):
            nonlocal outage_attempts, write_interrupted
            if (source in {"during_shutdown", "during_manual"} and data.get("_pending_ev_release")) or (
                source in {"canonical_shutdown", "canonical_manual"}
                and data.get("ev_grid_reservation") == {"active": False}
            ):
                if source in {"during_shutdown", "canonical_shutdown"}:
                    assert coordinator._tearing_down and not coordinator._unsub_listeners
                assert store.data["ownership"] and entry.entry_id in reservations
                await publish_draw()
            if source == "before_stage_manual" and not write_interrupted and data["ev_grid_reservation"].get("active"):
                write_interrupted = True
                await publish_draw()
            if platforms_unloaded and not write_interrupted:
                if source == "reload_save_failure":
                    write_interrupted = True
                    raise OSError("Simulated final reservation write failure")
                if source == "reload_save_cancelled":
                    write_interrupted = True
                    unload_task.cancel()
                if source == "reload_later_draw":
                    write_interrupted = True
                    await publish_draw(13)
                if source == "reload_store_outage" and outage_attempts < 3:
                    outage_attempts += 1
                    raise OSError("Simulated unavailable Store")
            await save(data)

        drain_observation = source in {
            "shutdown_drain", "inactive_shutdown_drain", "interrupted_drain", "cancelled_drain", "reload_interrupted",
            "active_shutdown_drain",
        }
        active_at = None
        if drain_observation:
            wait_for_refresh = coordinator.async_wait_for_refresh_shutdown

            async def during_drain():
                nonlocal active_at
                assert coordinator._tearing_down and not coordinator._unsub_listeners
                if source == "active_shutdown_drain":
                    active_at = dt_util.utcnow().isoformat()
                    hass.states.async_set("switch.charger", "on", {"sampled_at_utc": active_at})
                    await asyncio.sleep(0)
                    hass.states.async_set("switch.charger", "unavailable")
                await publish_draw()
                if source in {"interrupted_drain", "reload_interrupted"}:
                    raise OSError("Simulated unload drain failure")
                if source == "cancelled_drain":
                    raise asyncio.CancelledError
                await wait_for_refresh()

            monkeypatch.setattr(coordinator, "async_wait_for_refresh_shutdown", during_drain)

        async def unload_platforms(*_args):
            nonlocal platforms_unloaded
            if preserve_platforms:
                if source == "cap_handoff":
                    hass.states.async_remove("sensor.ev_power")
                    hass.states.async_set("number.limit", 11, attributes)
                    await asyncio.sleep(0)
                    hass.states.async_set("number.limit", "unavailable")
                    hass.states.async_remove("number.limit")
                elif source == "cap_late_draw_handoff":
                    await publish_draw(sampled_at=cap_confirmed_at - timedelta(seconds=1))
                elif source == "cap_zero_handoff":
                    await publish_draw(0)
                elif source == "external_activation":
                    hass.states.async_set("number.limit", 11, attributes)
                    await asyncio.sleep(0)
                    assert reservations[entry.entry_id]["load_kw"] == 3
                    hass.states.async_set("switch.charger", "on")
                    await asyncio.sleep(0)
                    hass.states.async_set("switch.charger", "unavailable")
                    hass.states.async_set("number.limit", "unavailable")
                else:
                    await publish_draw()
                platforms_unloaded = True
                return True
            pytest.fail("Uncertain charging must retain the entry's recovery ownership")

        async def refresh():
            pass

        monkeypatch.setattr(store._store, "async_save", interleave)
        if source == "reload_cancel_platforms":
            unloaded_platforms = []

            async def forward_platform_unload(_entry, platform):
                unloaded_platforms.append(platform)
                if len(unloaded_platforms) == len(PLATFORMS):
                    await publish_draw()
                    asyncio.get_running_loop().call_soon(unload_task.cancel)
                return True

            monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", forward_platform_unload)
        else:
            monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)
        monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
        hass.services.async_register("number", "set_value", set_limit)
        try:
            coordinator.async_start_listeners()
            if reload_platforms or source == "reload_interrupted":
                coordinator._configuration_reload_handoff = True
            if source == "shutdown_platforms":
                hass.state = CoreState.stopping
                await store.async_save_production({"armed": True})
                assert coordinator.automatic_control_requested
            if source in {"regular_meter", "retained_meter", "direct_resume"}:
                if source == "direct_resume":
                    coordinator._begin_shutdown()
                    hass.states.async_set("sensor.ev_power", 11, {"unit_of_measurement": "kW"})
                    coordinator.async_start_listeners()
                    await hass.async_block_till_done()
                    hass.states.async_set("sensor.ev_power", "unavailable")
                else:
                    await publish_draw()
                await hass.async_block_till_done()
                retry = await coordinator.async_cancel_charge_now()
                assert not retry.applied and retry.reason == "ev_stop_not_confirmed"
            elif source in {"during_manual", "canonical_manual", "manual_rehydrated", "before_stage_manual"}:
                result = await coordinator.async_cancel_charge_now()
                assert not result.applied and result.reason == "ev_stop_not_confirmed"
            elif source in {"interrupted_drain", "cancelled_drain", "reload_interrupted"}:
                expected_error = asyncio.CancelledError if source == "cancelled_drain" else OSError
                with pytest.raises(expected_error):
                    await async_unload_entry(hass, entry)
                assert not coordinator._tearing_down
                await hass.async_block_till_done()
            elif preserve_platforms:
                if source in {"reload_save_cancelled", "reload_cancel_platforms"}:
                    # The fake integration loader does not register metadata for Core's deprecation reporter.
                    monkeypatch.setattr("homeassistant.config_entries.report_usage", lambda *_args, **_kwargs: None)

                    async def get_component():
                        return SimpleNamespace(async_unload_entry=async_unload_entry)

                    async def unload_through_core():
                        entry._async_set_state(hass, ConfigEntryState.LOADED, None)
                        integration = SimpleNamespace(domain=DOMAIN, async_get_component=get_component)
                        async with entry.setup_lock:
                            return await entry.async_unload(hass, integration=integration)

                    unload_task = asyncio.create_task(unload_through_core())
                    assert await unload_task
                    assert entry.state is ConfigEntryState.NOT_LOADED
                    if source == "reload_save_cancelled":
                        assert write_interrupted
                    else:
                        assert set(unloaded_platforms) == set(PLATFORMS)
                else:
                    assert await async_unload_entry(hass, entry)
                assert not hasattr(entry, "runtime_data")
                if source == "reload_save_failure":
                    assert write_interrupted
                other_executor = _executor(hass, PlannerStore(hass, "other"))
                other_executor.retain_ev_grid_handoff()
                assert "runtime" not in reservations
                other_executor._discard_stale_ev_grid_reservations(reservations)
                assert entry.entry_id in reservations
                if source == "reload_store_outage":
                    assert outage_attempts == 3
                    replacement = PlannerStore(hass, entry.entry_id)
                    await replacement.async_load()
                    assert replacement.data["ev_grid_reservation"]["load_kw"] == 7
                    assert _rehydrate_ev_grid_reservation(hass, entry, replacement.data)
                    replacement_coordinator = EnergyPlannerCoordinator(hass, entry, replacement)
                    await replacement_coordinator.executor.async_persist_ev_grid_reservation()
                    assert not _rehydrate_ev_grid_reservation(hass, entry, replacement.data)
            else:
                assert not await async_unload_entry(hass, entry)
            assert coordinator.executor._ev_unload_draw is None
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert bool(saved.data["ownership"]) is (source != "external_activation")
            expected_load = 3 if cap_proof_handoff else 13 if source == "reload_later_draw" else 11
            assert saved.data["ev_grid_reservation"]["load_kw"] == expected_load
            if source in {"cap_handoff", "external_activation"}:
                assert saved.data["ev_grid_reservation"]["observed_limit_kw"] == 11
            elif source == "cap_late_draw_handoff":
                assert saved.data["ev_grid_reservation"]["cap_reduction"]["prior_load_kw"] == 11
            elif source == "cap_zero_handoff":
                assert saved.data["ev_grid_reservation"]["confirmed_power_limit"] is True
            else:
                assert saved.data["ev_grid_reservation"]["observed_load_at"]
            if active_at is not None:
                assert saved.data["ev_grid_reservation"]["observed_activity_at"] == active_at
            expected = [("number", "set_value", {"entity_id": "number.limit", "value": 9})]
            no_writes = rehydrated or drain_observation or preserve_platforms or source in {
                "regular_meter", "retained_meter", "direct_resume",
            }
            assert calls == ([] if no_writes else expected)
            if source == "inactive_shutdown_drain":
                retry = await coordinator.async_cancel_charge_now()
                assert not retry.applied and retry.reason == "ev_stop_not_confirmed" and not calls
            if source == "manual_rehydrated":
                hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
                result = await coordinator.async_cancel_charge_now()
                assert result.applied and calls == expected
                await saved.async_load()
                assert saved.data["ev_grid_reservation"] == {"active": False} and not saved.data["ownership"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_below_reservation_draw_persists_without_a_replan(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        _devices(hass)
        entry = ConfigEntry(
            domain=DOMAIN, title="Persist native stop evidence", data={
                CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit",
                "ev_power_entity": "sensor.ev_power",
            }, options=DEFAULT_OPTIONS, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({"ev_smart_charging_state": {
            "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        coordinator.executor._ev_grid_reservations()[entry.entry_id] = {"load_kw": 7}
        persisted = []
        save = store.async_save_ev_grid_reservation

        async def capture(reservation):
            persisted.append(reservation)
            await save(reservation)

        async def unexpected_refresh():
            pytest.fail("Ordering evidence alone does not require a capacity replan")

        monkeypatch.setattr(store, "async_save_ev_grid_reservation", capture)
        monkeypatch.setattr(coordinator, "async_request_refresh", unexpected_refresh)
        try:
            coordinator.async_start_listeners()
            async with coordinator._command_lock:
                for power in range(6000, 6012):
                    hass.states.async_set("sensor.ev_power", power, {"unit_of_measurement": "W"})
                await asyncio.sleep(0)
                assert not persisted
            await hass.async_block_till_done()
            hass.states.async_set("sensor.ev_power", "unavailable")
            await hass.async_block_till_done()
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert len(persisted) == 1 and saved.data["ev_grid_reservation"]["load_kw"] == 7
            assert saved.data["ev_grid_reservation"]["observed_load_at"]
            assert saved.data["ev_grid_reservation"]["observed_report_kw"] == pytest.approx(6.011)
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("during", [
    "cap_write", "cap_write_controls", "reservation", "equal_reservation", "proposal", "promotion",
    "promotion_controls", "promotion_poll", "promotion_controls_repeat", "promotion_meter_repeat",
    "promotion_meter_control_refresh", "promotion_outage", "promotion_controls_previous_draw",
    "cap_write_controls_previous_draw", "promotion_meter_delayed", "promotion_controls_delayed",
    "promotion_late_activation", "promotion_nested_meter", "promotion_meter_outage",
    "promotion_late_activation_before_initial", "promotion_nested_meter_overflow", "promotion_late_activation_overflow",
])
def test_native_release_prices_through_new_stop_after_intervening_draw(tmp_path, monkeypatch, during):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        control_feedback = "_controls" in during
        outage = during == "promotion_outage"
        no_new_activity = during == "promotion_poll" or outage
        retained_draw = during.endswith("_previous_draw")
        delayed_source = during.endswith("_delayed")
        activation_before_initial = during in {
            "promotion_late_activation_before_initial", "promotion_late_activation_overflow",
        }
        phase_to_interrupt = (
            "promotion" if during.startswith("promotion") else "cap_write" if during.startswith("cap_write") else during
        )
        clock = dt_util.utcnow()
        first_stop = clock
        renewed_stop = first_stop + timedelta(seconds=61)
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        limit_attributes = {"unit_of_measurement": "kW", "min": 1, "max": 10}
        hass.states.async_set("number.limit", 3 if phase_to_interrupt == "cap_write" else 9, limit_attributes)
        power_attributes = {"unit_of_measurement": "kW"}
        no_meter = (control_feedback and not retained_draw) or during == "promotion_poll"
        initial_feedback_at = (first_stop - timedelta(seconds=5)) if activation_before_initial else first_stop
        hass.states.async_set("sensor.ev_power", "unavailable" if no_meter else 0,
                              power_attributes, timestamp=initial_feedback_at.timestamp())
        if activation_before_initial:
            hass.states.async_set("switch.charger", "off", {"sampled_at_utc": initial_feedback_at.isoformat()},
                                  timestamp=clock.timestamp())
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        await store.async_save_ownership({"ev_smart_charging_state": {
            "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        await store.async_save_ev_telemetry({"command_exposure": {
            "at": (first_stop - timedelta(minutes=1)).isoformat(), "cost_per_hour": 60,
        }})
        await store.async_save_command_rate_limits({"ev_safety_stop_failed_at": first_stop.isoformat()})
        reservation = {"load_kw": 7}
        if retained_draw:
            reservation.update(observed_load_at=(first_stop - timedelta(seconds=5)).isoformat(), observed_report_kw=7)
        executor._ev_grid_reservations()["runtime"] = reservation
        save = store._store.async_save
        intervened = False
        proposals = 0

        async def renew_stop():
            nonlocal clock, intervened
            intervened = True
            if during == "promotion_late_activation" or activation_before_initial:
                offsets = (range(MAX_NATIVE_STOP_SAMPLES + 2) if during == "promotion_late_activation_overflow"
                           else (0, 100, 200))
                for tenths in offsets:
                    clock = renewed_stop + timedelta(milliseconds=100 * tenths)
                    attrs = {**power_attributes, "sampled_at_utc": clock.isoformat()}
                    hass.states.async_set("sensor.ev_power", 0, attrs, timestamp=clock.timestamp())
                    await asyncio.sleep(0)
                clock += timedelta(seconds=5)
                activation_stamp = (first_stop + timedelta(seconds=-1 if activation_before_initial else 1)).isoformat()
                hass.states.async_set("switch.charger", "on", {"sampled_at_utc": activation_stamp},
                                      timestamp=clock.timestamp())
                await asyncio.sleep(0)
                clock += timedelta(seconds=5)
                hass.states.async_set("switch.charger", "off", timestamp=clock.timestamp())
                return
            clock = first_stop if during == "equal_reservation" else first_stop + timedelta(seconds=1)
            if control_feedback:
                hass.states.async_set("switch.charger", "on", timestamp=clock.timestamp())
            elif outage:
                hass.states.async_set("switch.charger", "unavailable", timestamp=clock.timestamp())
            elif not no_new_activity:
                hass.states.async_set("sensor.ev_power", 8, power_attributes, timestamp=clock.timestamp())
            await asyncio.sleep(0)
            if during in {"promotion_nested_meter", "promotion_nested_meter_overflow"}:
                @callback
                def report_queued_zeros(_event):
                    nonlocal clock
                    offsets = (range(MAX_NATIVE_STOP_SAMPLES + 1) if during == "promotion_nested_meter_overflow"
                               else (0, 50, 100))
                    for tenths in offsets:
                        clock = renewed_stop + timedelta(milliseconds=100 * tenths)
                        hass.states.async_set("sensor.ev_power", 0, power_attributes, timestamp=clock.timestamp())

                hass.bus.async_listen_once("test_queued_meter_reports", report_queued_zeros)
                hass.bus.async_fire("test_queued_meter_reports")
                await asyncio.sleep(0)
                return
            clock = renewed_stop + timedelta(seconds=10 if delayed_source else 0)
            if control_feedback or no_new_activity:
                hass.states.async_set("switch.charger", "off", {"sampled_at_utc": renewed_stop.isoformat()},
                                      timestamp=clock.timestamp())
            else:
                zero_attrs = ({**power_attributes, "sampled_at_utc": renewed_stop.isoformat()}
                              if delayed_source else power_attributes)
                hass.states.async_set("sensor.ev_power", 0, zero_attrs, timestamp=clock.timestamp())
            if during == "promotion_meter_outage":
                await asyncio.sleep(0)
                clock += timedelta(seconds=10)
                hass.states.async_set("sensor.ev_power", "unavailable", timestamp=clock.timestamp())
                clock += timedelta(seconds=20)
                hass.states.async_set("switch.charger", "off", {"sampled_at_utc": clock.isoformat()},
                                      timestamp=clock.timestamp())
            if delayed_source or during in {
                "promotion_controls_repeat", "promotion_meter_repeat", "promotion_meter_control_refresh",
            }:
                await asyncio.sleep(0)
                clock += timedelta(seconds=30)
                if during == "promotion_meter_repeat":
                    hass.states.async_set("sensor.ev_power", 0, power_attributes, timestamp=clock.timestamp())
                elif during == "promotion_meter_delayed":
                    repeated_zero_attrs = {**power_attributes, "sampled_at_utc": clock.isoformat()}
                    hass.states.async_set("sensor.ev_power", 0, repeated_zero_attrs, timestamp=clock.timestamp())
                else:
                    if during == "promotion_meter_control_refresh":
                        hass.states.async_set("switch.charger", "unavailable", timestamp=clock.timestamp())
                        clock += timedelta(seconds=1)
                    hass.states.async_set("switch.charger", "off", {"sampled_at_utc": clock.isoformat()},
                                          timestamp=clock.timestamp())
            clock += timedelta(minutes=1)

        async def resume_then_stop(payload):
            nonlocal proposals
            phase = "reservation"
            if payload.get("_pending_ev_release"):
                proposals += 1
                phase = "proposal"
            elif payload.get("_pending_ev_release_recovery"):
                phase = "promotion"
            if not intervened and (
                phase == phase_to_interrupt or (phase == "reservation" and during == "equal_reservation")
            ):
                await renew_stop()
            await save(payload)

        async def set_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            assert phase_to_interrupt == "cap_write"
            await renew_stop()
            hass.states.async_set("number.limit", call.data["value"], limit_attributes)

        monkeypatch.setattr(store._store, "async_save", resume_then_stop)
        hass.services.async_register("number", "set_value", set_limit)
        try:
            result = await executor.async_restore_device_control("ev", "native_release")
            assert result.result == OutcomeResult.RESTORED and intervened
            assert [call[1] for call in calls] == (["set_value"] if phase_to_interrupt == "cap_write" else [])
            assert proposals == (2 if phase_to_interrupt in {"proposal", "promotion"} and not no_new_activity else 1)
            latest = PlannerStore(hass, "runtime")
            await latest.async_load()
            assert not latest.data["ownership"] and latest.data["ev_grid_reservation"] == {"active": False}
            assert "command_exposure" not in latest.data["ev_telemetry"]
            assert latest.data["ev_telemetry"]["emergency_spend"] == pytest.approx(1 if no_new_activity else 121 / 60)
            expected_stop = first_stop if no_new_activity else renewed_stop
            assert latest.data["command_rate_limits"]["ev_safe_stop_confirmed_at"] == expected_stop.isoformat()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("feedback", ["meter", "switch"])
def test_initial_native_stop_settles_at_source_time(tmp_path, monkeypatch, feedback):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        clock = started = dt_util.utcnow()
        stopped_at = started + timedelta(seconds=10)
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        hass.states.async_set("switch.charger", "on", timestamp=started.timestamp())
        hass.states.async_set("sensor.ev_power", 7, {"unit_of_measurement": "kW"}, timestamp=started.timestamp())
        hass.states.async_set("number.limit", 9, {"unit_of_measurement": "kW", "min": 1, "max": 10})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        await store.async_save_ownership({"ev_smart_charging_state": {
            "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        await store.async_save_ev_telemetry({"command_exposure": {
            "at": (started - timedelta(minutes=1)).isoformat(), "cost_per_hour": 60,
        }})
        await store.async_save_command_rate_limits({"ev_safety_stop_failed_at": started.isoformat()})
        executor._ev_grid_reservations()["runtime"] = {"load_kw": 7}

        async def stop(call):
            nonlocal clock
            calls.append((call.domain, call.service, dict(call.data)))
            clock = started + timedelta(seconds=50)
            stamp = {"sampled_at_utc": stopped_at.isoformat()}
            hass.states.async_set("switch.charger", "off", stamp if feedback == "switch" else {},
                                  timestamp=clock.timestamp())
            if feedback == "meter":
                hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW", **stamp},
                                      timestamp=clock.timestamp())
            else:
                hass.states.async_set("sensor.ev_power", "unavailable", timestamp=clock.timestamp())

        hass.services.async_register("switch", "turn_off", stop)
        try:
            result = await executor.async_restore_device_control("ev", "native_release")
            assert result.result == OutcomeResult.RESTORED
            assert [call[1] for call in calls] == ["turn_off"]
            latest = PlannerStore(hass, "runtime")
            await latest.async_load()
            assert latest.data["ev_telemetry"]["emergency_spend"] == pytest.approx(70 / 60)
            assert latest.data["command_rate_limits"]["ev_safe_stop_confirmed_at"] == stopped_at.isoformat()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("during", ["initial", "promotion"])
def test_native_release_rejects_old_stop_after_newer_explicit_active_report(tmp_path, monkeypatch, during):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        clock = started = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        hass.states.async_set("number.limit", 3 if during == "initial" else 9,
                              {"unit_of_measurement": "kW", "min": 1, "max": 10})
        hass.states.async_set("sensor.ev_power", "unavailable")
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_confirmation_timeout_seconds=0, ev_confirmation_retries=0)
        await store.async_save_ownership({"ev_smart_charging_state": {
            "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        await store.async_save_ev_telemetry({"command_exposure": {"at": started.isoformat(), "cost_per_hour": 1}})
        executor._ev_grid_reservations()["runtime"] = {"load_kw": 7}
        save = store._store.async_save

        def report(state, sample, received):
            nonlocal clock
            clock = started + timedelta(seconds=received)
            sampled_at = (started + timedelta(seconds=sample)).isoformat()
            hass.states.async_set("switch.charger", state, {"sampled_at_utc": sampled_at}, timestamp=clock.timestamp())

        async def interleave(payload):
            if payload.get("_pending_ev_release_recovery"):
                assert during == "promotion"
                for state, sample, received in (("on", 1, 1), ("on", 3, 3), ("off", 2, 4)):
                    report(state, sample, received)
                    await asyncio.sleep(0)
            await save(payload)

        async def publish_old_stop(call):
            calls.append((call.domain, call.service, dict(call.data)))
            report("off", 2, 4)

        if during == "initial":
            report("on", 3, 3)
            hass.services.async_register("switch", "turn_off", publish_old_stop)
        monkeypatch.setattr(store._store, "async_save", interleave)
        try:
            result = await executor.async_restore_device_control("ev", "native_release")
            assert result.result == OutcomeResult.FAILED and "ev_stop_not_confirmed" in result.reason
            latest = PlannerStore(hass, "runtime")
            await latest.async_load()
            assert latest.data["ownership"] and latest.data["ev_grid_reservation"]["active"]
            assert latest.data["ev_grid_reservation"]["observed_activity_at"] == (
                started + timedelta(seconds=3)
            ).isoformat()
            assert "command_exposure" in latest.data["ev_telemetry"]
            retry = await executor.async_restore_device_control("ev", "native_release")
            assert retry.result == OutcomeResult.FAILED and "ev_stop_not_confirmed" in retry.reason
            executor._ev_grid_reservations().clear()
            entry = SimpleNamespace(entry_id="runtime", options=executor.options)
            _rehydrate_ev_grid_reservation(hass, entry, latest.data)
            restarted = Executor(latest, hass=hass, entry_id="runtime", entry_data=executor.entry_data,
                                 options=executor.options)
            retry_after_restart = await restarted.async_restore_device_control("ev", "native_release")
            assert retry_after_restart.result == OutcomeResult.FAILED
            assert "ev_stop_not_confirmed" in retry_after_restart.reason
            assert [call[1] for call in calls] == (["turn_off"] if during == "initial" else [])
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("remapped", [False, True])
def test_owned_native_activity_between_restores_survives_store_reload(tmp_path, monkeypatch, remapped):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        clock = started = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 10})
        hass.states.async_set("sensor.ev_power", "unavailable")
        original = {CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit",
                    "ev_power_entity": "sensor.ev_power"}
        new = {**original, CONF_EV_CHARGER: "switch.replacement"} if remapped else original
        entry = ConfigEntry(
            domain=DOMAIN, title="Native EV activity across restores", data=new, source="user", unique_id=None,
            options={**DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 0},
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": original,
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        reservations = coordinator.executor._ev_grid_reservations()

        def report(state, sampled, received):
            nonlocal clock
            clock = started + timedelta(seconds=received)
            sample_at = started + timedelta(seconds=sampled)
            hass.states.async_set("switch.charger", state, {"sampled_at_utc": sample_at.isoformat()},
                                  timestamp=clock.timestamp())

        async def refresh():
            pass

        monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *_args, **_kwargs: None)
        try:
            report("on", 2, 2)
            coordinator.async_start_listeners()
            report("off", 1, 3)
            await hass.async_block_till_done()
            first = await coordinator.executor.async_restore_device_control("ev", "native_release")
            assert first.result == OutcomeResult.FAILED
            report("on", 5, 5)
            report("on", 6, 6)
            hass.states.async_remove("switch.charger")
            report("off", 4, 7)
            await hass.async_block_till_done()
            latest = PlannerStore(hass, entry.entry_id)
            await latest.async_load()
            assert latest.data["ev_grid_reservation"]["observed_activity_at"] == (
                started + timedelta(seconds=6)
            ).isoformat()
            assert latest.data["ownership"] and latest.data["ev_grid_reservation"]["active"]
            reservations.clear()
            _rehydrate_ev_grid_reservation(hass, entry, latest.data)
            restarted = EnergyPlannerCoordinator(hass, entry, latest)
            retry = await restarted.executor.async_restore_device_control("ev", "native_release")
            assert retry.result == OutcomeResult.FAILED and "ev_stop_not_confirmed" in retry.reason
            assert hass.states.get("number.limit").state == "3" and not calls
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["on", "unavailable", "removed", "invalid_clock_change", "valid_clock_change"])
def test_native_stop_uses_only_eligible_receipts_before_two_confirmation_cutoffs(
    tmp_path, monkeypatch, interruption,
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        start = dt_util.utcnow()
        clock = start
        elapsed = 0.0
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        topology = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CHARGING: "binary_sensor.charging",
                    "ev_power_limit_entity": "number.limit"}
        store = PlannerStore(hass, "runtime")
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": topology,
        })
        executor = _executor(hass, store)
        charger = EVChargerAdapter(hass, topology, confirmation_timeout_seconds=0.5, confirmation_retries=0)

        def report(entity_id, state, elapsed):
            nonlocal clock
            clock = start + timedelta(seconds=elapsed)
            hass.states.async_set(entity_id, state, {"sampled_at_utc": clock.isoformat()}, timestamp=clock.timestamp())

        report("binary_sensor.charging", "on", -0.9)
        report("switch.charger", "off", -0.8)
        clock = start

        async def delayed_wake(_delay):
            nonlocal clock, elapsed
            if interruption.endswith("_clock_change"):
                report("switch.charger", "on", 0.2)
                clock = start + timedelta(seconds=0.45)
                sample = "invalid" if interruption == "invalid_clock_change" else clock.isoformat()
                hass.states.async_set("binary_sensor.charging", "off", {"sampled_at_utc": sample},
                                      timestamp=clock.timestamp())
                report("switch.charger", "off", 0.75)
                clock = start + timedelta(seconds=1.05)
                hass.states.async_set("binary_sensor.charging", "off", timestamp=clock.timestamp())
                clock = start + timedelta(seconds=1.1)
                hass.states.async_set("binary_sensor.charging", "off", {"name": "Charger"},
                                      timestamp=clock.timestamp())
            else:
                if interruption == "removed":
                    hass.states.async_remove("switch.charger")
                else:
                    report("switch.charger", interruption, 0.45)
                report("binary_sensor.charging", "off", 0.49)
                report("switch.charger", "off", 1.1)
            elapsed = 1.2
            clock = start + timedelta(seconds=elapsed)

        monkeypatch.setattr(ev_adapter, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: elapsed), sleep=delayed_wake,
        ))
        try:
            with ExitStack() as observations:
                executor._observe_native_ev_adapter(observations, charger, topology)
                outcome = await charger._async_confirm_stop("switch.charger", "binary_sensor.charging",
                                                            extend_for_control=True, ordered_stop=True)
                expected = "confirmed" if interruption == "valid_clock_change" else "timeout"
                assert outcome == expected and not calls
                assert charger.native_restore_stop_is_current()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("source", "unverified"), [
    ("switch.charger", False), ("binary_sensor.charging", False), ("sensor.ev_power", False),
    ("sensor.ev_power", True), ("number.limit", False),
])
def test_setup_retains_original_native_activity_during_first_refresh(tmp_path, monkeypatch, source, unverified):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        calls = _devices(hass)
        start = clock = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        topology = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CHARGING: "binary_sensor.charging",
                    "ev_power_entity": "sensor.ev_power", "ev_power_limit_entity": "number.limit"}
        entry = ConfigEntry(
            domain=DOMAIN, title="Owned EV feedback during setup", data={**topology, CONF_EV_CHARGER: "switch.new"},
            options={**DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 0,
                     "ev_limit_min": 1, "ev_limit_max": 12},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": topology,
        })
        await store.async_save_ev_grid_reservation({"active": True, "load_kw": 8})

        def report(entity, state, sampled, received):
            nonlocal clock
            clock = start + timedelta(seconds=received)
            attrs = {"sampled_at_utc": (start + timedelta(seconds=sampled)).isoformat()}
            if entity == "sensor.ev_power":
                attrs["unit_of_measurement"] = "kW"
            hass.states.async_set(entity, state, attrs, timestamp=clock.timestamp())

        initial_cap = source == "number.limit"
        report("switch.charger", "on" if initial_cap else "off", 0, 0)
        report("binary_sensor.charging", "on" if initial_cap else "off", 0, 0)
        if source != "sensor.ev_power":
            hass.states.async_set("sensor.ev_power", "unavailable")
        hass.states.async_set("number.limit", 11 if initial_cap else 3, {
            "unit_of_measurement": "kW", "min": 1, "max": 12, "step": 1,
        })
        entering_refresh = asyncio.Event()
        finish_refresh = asyncio.Event()

        async def first_refresh(_coordinator):
            entering_refresh.set()
            await finish_refresh.wait()

        async def skip_platform_setup(_entry, _platforms):
            pass

        monkeypatch.setattr(EnergyPlannerCoordinator, "async_config_entry_first_refresh", first_refresh)
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", skip_platform_setup)
        monkeypatch.setattr("custom_components.ha_energy_planner._async_sync_planner_device", lambda *_args: None)
        setup = asyncio.create_task(async_setup_entry(hass, entry))
        try:
            await asyncio.wait_for(entering_refresh.wait(), 2)
            metered = source == "sensor.ev_power"
            if initial_cap:
                reservation = entry.runtime_data.executor._ev_grid_reservations()[entry.entry_id]
                assert reservation["load_kw"] == 11 and not calls
            else:
                if metered and not unverified:
                    report(source, 5, 2, 2)
                    await hass.async_block_till_done()
                    initial = PlannerStore(hass, entry.entry_id)
                    await initial.async_load()
                    assert initial.data["ev_grid_reservation"]["observed_report_kw"] == 5
                    report(source, 7, 2, 2.5)
                else:
                    report(source, 7 if metered else "on", 60 if unverified else 2, 2)
                report(source, 0 if metered else "off", 1, 3)
            finish_refresh.set()
            assert await setup
            await hass.async_block_till_done()
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            if initial_cap:
                assert saved.data["ev_grid_reservation"]["load_kw"] == 11 and not calls
            else:
                evidence_key = "observed_load_at" if metered and not unverified else "observed_activity_at"
                assert saved.data["ev_grid_reservation"][evidence_key] == (start + timedelta(seconds=2)).isoformat()
                if metered and not unverified:
                    assert saved.data["ev_grid_reservation"]["observed_report_kw"] == 7
                result = await entry.runtime_data.executor.async_restore_device_control("ev", "native_release")
                assert result.result == OutcomeResult.FAILED and "ev_stop_not_confirmed" in result.reason
                assert hass.states.get("number.limit").state == "3" and not calls
        finally:
            finish_refresh.set()
            await setup
            if (coordinator := getattr(entry, "runtime_data", None)) is not None:
                await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_native_implicit_clock_change_records_same_state_charging_as_activity(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        start = clock = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 10, "step": 1}
        hass.states.async_set("number.limit", 3, attrs)
        topology = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CHARGING: "binary_sensor.charging",
                    CONF_EV_CONNECTED: "binary_sensor.connected", "ev_power_limit_entity": "number.limit"}
        hass.states.async_set("switch.charger", "off", timestamp=start.timestamp())
        hass.states.async_set("binary_sensor.connected", "off", timestamp=start.timestamp())
        saved = {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"}
        store = PlannerStore(hass, "runtime")
        await store.async_save_ownership({
            "ev_smart_charging_state": saved, "ev_smart_charging_control_topology": topology,
        })
        executor = _executor(hass, store)
        charger = EVChargerAdapter(hass, topology, confirmation_timeout_seconds=0, confirmation_retries=0)

        async def set_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], attrs)

        hass.services.async_register("number", "set_value", set_limit)
        try:
            with ExitStack() as observations:
                executor._observe_native_ev_adapter(observations, charger, topology)
                clock += timedelta(seconds=1)
                hass.states.async_set("binary_sensor.charging", "on", {"sampled_at_utc": "invalid"},
                                      timestamp=clock.timestamp())
                clock += timedelta(seconds=1)
                hass.states.async_set("binary_sensor.charging", "on", timestamp=clock.timestamp())
                result = await charger.async_restore(saved)
                assert not result.applied and result.reason == "ev_stop_not_confirmed"
                assert charger.stop_observations.active_at == clock
                assert hass.states.get("number.limit").state == "3"
                assert calls == [("switch", "turn_off", {"entity_id": "switch.charger"})]
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("handoff", [
    "setup_retry", "reload", "unobserved_report", "replayed_report_metadata", "clock_rewind", "raw_clock_rewind",
    "unobserved_rewind_update", "missing_report", "unobserved_missing_report", "recovered_raw_change",
    "unobserved_newer_raw_report", "unobserved_implicit_transition", "backwards_heartbeat", "raw_backwards_heartbeat",
    "explicit_backwards_heartbeat", "regular_backwards_heartbeat",
    "gap_backwards_heartbeat", "gap_explicit_backwards_heartbeat", "gap_updated_stop", "recycled_explicit_timestamp",
    "recycled_explicit_force_update", "recycled_explicit_after_removal", "recycled_meter_timestamp",
    "entry_removed_before_retry", "failed_setup_retry", "failed_setup_drain",
    "gap_charging_activity", "gap_meter_draw", "gap_cap_and_signal", "gap_charging_new_claim",
    "gap_future_meter_draw", "unload_future_meter_draw",
])
def test_native_signal_clock_is_reused_for_the_same_report_after_runtime_replacement(tmp_path, monkeypatch, handoff):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        calls = _devices(hass)
        start = clock = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        topology = {CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected",
                    "ev_power_limit_entity": "number.limit"}
        gap_evidence = handoff in {
            "gap_charging_activity", "gap_meter_draw", "gap_cap_and_signal", "gap_charging_new_claim",
            "gap_future_meter_draw", "unload_future_meter_draw",
        }
        if gap_evidence or handoff == "recycled_meter_timestamp":
            topology["ev_power_entity"] = "sensor.ev_power"
        if gap_evidence:
            topology[CONF_EV_CHARGING] = "binary_sensor.charging"
        options = {**DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 0}
        if gap_evidence:
            options.update(ev_limit_min=1, ev_limit_max=12)
        entry = ConfigEntry(
            domain=DOMAIN, title="Native stop clock across runtime replacement", data=topology,
            options=options,
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        hass.config_entries._entries[entry.entry_id] = entry
        hass.states.async_set("switch.charger", "off", timestamp=start.timestamp())
        hass.states.async_set("binary_sensor.connected", "off", {"sampled_at_utc": "invalid"},
                              timestamp=start.timestamp())
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 12, "step": 1}
        hass.states.async_set("number.limit", 3 if gap_evidence or handoff == "failed_setup_drain" else 9, attrs)
        if gap_evidence:
            hass.states.async_set("binary_sensor.charging", "off", timestamp=start.timestamp())

        async def set_limit(call):
            calls.append((call.domain, call.service, dict(call.data)))
            hass.states.async_set("number.limit", call.data["value"], attrs)

        hass.services.async_register("number", "set_value", set_limit)
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": topology,
        })
        reservation = {"active": True, "load_kw": 7, "observed_activity_at": (start + timedelta(seconds=1)).isoformat()}
        if handoff == "gap_charging_new_claim":
            reservation = {"active": False}
        await store.async_save_ev_grid_reservation(reservation)
        setup_failure = handoff in {"failed_setup_retry", "failed_setup_drain"}
        retry = handoff == "setup_retry" or setup_failure
        connected = "binary_sensor.connected"

        def report_implicit_stop():
            nonlocal clock
            clock = start + timedelta(seconds=2)
            hass.states.async_set(connected, "off", timestamp=clock.timestamp())

        def rewind_unchanged_report():
            nonlocal clock
            state = hass.states.get(connected)
            clock = start
            hass.states.async_set(connected, state.state, state.attributes, timestamp=clock.timestamp())
            assert hass.states.get(connected) is state and state.last_reported == clock
            clock = start + timedelta(seconds=3)
            hass.states.async_set(connected, state.state, state.attributes, timestamp=clock.timestamp())
            assert hass.states.get(connected) is state

        def record_handoff_feedback():
            nonlocal clock
            clock = start + timedelta(seconds=4)
            if "meter_draw" in handoff:
                meter = {"unit_of_measurement": "kW"}
                if "future" in handoff:
                    meter["sampled_at_utc"] = (clock + timedelta(milliseconds=500)).isoformat()
                hass.states.async_set("sensor.ev_power", 8, meter, timestamp=clock.timestamp())
                hass.states.async_set("sensor.ev_power", 8, meter, timestamp=clock.timestamp())
            else:
                hass.states.async_set("binary_sensor.charging", "on", timestamp=clock.timestamp())
                hass.states.async_set("binary_sensor.charging", "on", {
                    "sampled_at_utc": (clock - timedelta(seconds=1)).isoformat(),
                }, timestamp=clock.timestamp())
            if handoff == "gap_cap_and_signal":
                hass.states.async_set("number.limit", 10, attrs, timestamp=clock.timestamp())
            clock = start + timedelta(seconds=5)
            stale = {"sampled_at_utc": (start + timedelta(seconds=2)).isoformat()}
            hass.states.async_set("binary_sensor.charging", "off", stale, timestamp=clock.timestamp())
            hass.states.async_set("sensor.ev_power", "unavailable", timestamp=clock.timestamp())

        async def first_refresh(_coordinator):
            nonlocal retry
            if retry:
                retry = False
                if setup_failure:
                    raise ConfigEntryNotReady("Initial input is temporarily unavailable")
                report_implicit_stop()
                raise ConfigEntryNotReady("Earlier plan superseded") from InitialPlanSuperseded()

        async def unload_platforms(_entry, _platforms):
            nonlocal clock
            assert not entry.runtime_data._unsub_listeners
            if handoff == "regular_backwards_heartbeat" or handoff.startswith("recycled_"):
                return True
            raw_rewind = handoff in {"raw_clock_rewind", "unobserved_rewind_update", "recovered_raw_change"}
            if raw_rewind or handoff in {
                "unobserved_newer_raw_report", "unobserved_implicit_transition", "raw_backwards_heartbeat",
                "explicit_backwards_heartbeat", "gap_explicit_backwards_heartbeat", "gap_updated_stop",
            }:
                hass.states.async_set(connected, "on", {"sampled_at_utc": "invalid"}, timestamp=clock.timestamp())
            if handoff in {
                "unobserved_implicit_transition", "explicit_backwards_heartbeat", "gap_explicit_backwards_heartbeat",
                "gap_updated_stop",
            }:
                clock = start + timedelta(seconds=2)
                source = clock.isoformat() if "explicit_backwards" in handoff else "invalid"
                hass.states.async_set(connected, "off", {"sampled_at_utc": source}, timestamp=clock.timestamp())
            else:
                report_implicit_stop()
            if handoff.endswith("_heartbeat") and not handoff.startswith("gap_"):
                rewind_unchanged_report()
            if raw_rewind or handoff == "clock_rewind":
                clock = start
                hass.states.async_set(connected, "off", {"name": "Charger"}, timestamp=clock.timestamp())
                clock = start + timedelta(seconds=3)
                adapter = EVChargerAdapter(hass, topology)
                adapter.stop_observations.active_at = start + timedelta(seconds=1)
                adapter.stop_observations.implicit_signal_clocks[connected] = EVImplicitSignalClock(
                    hass.states.get(connected).last_changed, start + timedelta(seconds=2),
                )
                assert adapter.native_restore_stop_evidence() is None
                if handoff != "unobserved_rewind_update":
                    hass.states.async_set(connected, "off", {"name": "Charger after clock change"},
                                          timestamp=clock.timestamp())
                assert entry.runtime_data.executor._native_ev_signal_clocks[connected].started_at is None
            if handoff == "unload_future_meter_draw":
                record_handoff_feedback()
            return True

        async def setup_platforms(_entry, _platforms):
            pass

        if handoff == "failed_setup_drain":
            wait_for_shutdown = EnergyPlannerCoordinator.async_wait_for_refresh_shutdown

            async def drain(coordinator):
                nonlocal clock
                assert not coordinator._unsub_listeners
                sampled = (start + timedelta(seconds=5)).isoformat()
                for receipt in (3, 7):
                    clock = start + timedelta(seconds=receipt)
                    attrs = {"sampled_at_utc": sampled, "name": str(receipt)}
                    hass.states.async_set(connected, "off", attrs, timestamp=clock.timestamp())
                await wait_for_shutdown(coordinator)

            monkeypatch.setattr(EnergyPlannerCoordinator, "async_wait_for_refresh_shutdown", drain)

        monkeypatch.setattr(EnergyPlannerCoordinator, "async_config_entry_first_refresh", first_refresh)
        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", setup_platforms)
        monkeypatch.setattr("custom_components.ha_energy_planner._async_sync_planner_device", lambda *_args: None)
        try:
            if handoff == "setup_retry" or setup_failure:
                with pytest.raises(ConfigEntryNotReady):
                    await async_setup_entry(hass, entry)
            else:
                assert await async_setup_entry(hass, entry)
                if handoff == "regular_backwards_heartbeat":
                    report_implicit_stop()
                    rewind_unchanged_report()
                elif handoff.startswith("recycled_explicit"):
                    for receipt, sample in ((2, 5), (3, 6), (7, 5)):
                        clock = start + timedelta(seconds=receipt)
                        sampled = start + timedelta(seconds=sample)
                        attrs = {"sampled_at_utc": sampled.isoformat(), "name": str(receipt)}
                        if receipt == 7 and handoff == "recycled_explicit_after_removal":
                            hass.states.async_remove(connected)
                        hass.states.async_set(connected, "off", attrs, timestamp=clock.timestamp(),
                                              force_update=receipt == 7 and handoff == "recycled_explicit_force_update")
                elif handoff == "recycled_meter_timestamp":
                    sampled = (start + timedelta(seconds=5)).isoformat()
                    for receipt in (2, 7):
                        clock = start + timedelta(seconds=receipt)
                        attrs = {"sampled_at_utc": sampled, "unit_of_measurement": "kW", "name": str(receipt)}
                        hass.states.async_set("sensor.ev_power", 0, attrs, timestamp=clock.timestamp())
                entry.runtime_data._configuration_reload_handoff = True
                assert await async_unload_entry(hass, entry)
            assert not hasattr(entry, "runtime_data")
            retained_handoffs = hass.data[DOMAIN][EV_SIGNAL_CLOCK_HANDOFFS]
            retained_handoff = retained_handoffs[entry.entry_id]
            assert retained_handoff.subscriptions
            if handoff == "entry_removed_before_retry":
                await async_remove_entry(hass, entry)
                assert not retained_handoff.subscriptions and entry.entry_id not in retained_handoffs
                return
            if handoff in {"gap_backwards_heartbeat", "gap_explicit_backwards_heartbeat"}:
                rewind_unchanged_report()
            elif handoff == "gap_updated_stop":
                clock = start + timedelta(seconds=3)
                hass.states.async_set(connected, "off", timestamp=clock.timestamp())
            elif handoff == "failed_setup_retry":
                sampled = (start + timedelta(seconds=5)).isoformat()
                for receipt in (3, 7):
                    clock = start + timedelta(seconds=receipt)
                    attrs = {"sampled_at_utc": sampled, "name": str(receipt)}
                    hass.states.async_set(connected, "off", attrs, timestamp=clock.timestamp())
            if gap_evidence and handoff != "unload_future_meter_draw":
                record_handoff_feedback()
            unobserved_report = handoff in {
                "unobserved_report", "replayed_report_metadata", "unobserved_rewind_update",
                "unobserved_newer_raw_report", "unobserved_implicit_transition",
            }
            if unobserved_report:
                retained_handoff.close()
                old_report = hass.states.get("binary_sensor.connected")
                clock = start + timedelta(seconds=3)
                replayed_metadata = handoff == "replayed_report_metadata"
                hass.states.async_set("binary_sensor.connected", "off", {"name": "Charger after listeners detached"},
                                      timestamp=(old_report.last_updated if replayed_metadata else clock).timestamp(),
                                      context=old_report.context if replayed_metadata else None)
                assert hass.states.get("binary_sensor.connected") is not old_report
            elif handoff in {"missing_report", "unobserved_missing_report"}:
                if handoff == "unobserved_missing_report":
                    retained_handoff.close()
                hass.states.async_remove("binary_sensor.connected")
            elif handoff == "recovered_raw_change":
                hass.states.async_set("binary_sensor.connected", "on", timestamp=clock.timestamp())
                clock += timedelta(seconds=1)
                hass.states.async_set("binary_sensor.connected", "off", timestamp=clock.timestamp())
            assert await async_setup_entry(hass, entry)
            assert not retained_handoff.subscriptions
            result = await entry.runtime_data.executor.async_restore_device_control("ev", "native_release")
            stopped = handoff in {"setup_retry", "reload", "recovered_raw_change", "gap_updated_stop"}
            expected = OutcomeResult.RESTORED if stopped else OutcomeResult.FAILED
            assert result.result == expected and not calls
            latest = PlannerStore(hass, entry.entry_id)
            await latest.async_load()
            assert bool(latest.data["ownership"]) is (expected == OutcomeResult.FAILED)
            if gap_evidence:
                key = "observed_load_at" if handoff == "gap_meter_draw" else "observed_activity_at"
                assert latest.data["ev_grid_reservation"][key] == (start + timedelta(seconds=4)).isoformat()
                if "meter_draw" in handoff or handoff == "gap_cap_and_signal":
                    assert latest.data["ev_grid_reservation"]["load_kw"] == (8 if "meter_draw" in handoff else 10)
                if "future_meter_draw" in handoff:
                    assert "observed_load_at" not in latest.data["ev_grid_reservation"]
            if handoff.startswith("recycled_explicit"):
                clock = start + timedelta(seconds=8)
                hass.states.async_set(connected, "off", {"sampled_at_utc": clock.isoformat()},
                                      timestamp=clock.timestamp())
                retried = await entry.runtime_data.executor.async_restore_device_control("ev", "native_release")
                assert retried.result == OutcomeResult.RESTORED and not calls
        finally:
            if (coordinator := getattr(entry, "runtime_data", None)) is not None:
                await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_native_signal_clock_retention_follows_original_topology_across_acquisitions(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        now = dt_util.utcnow()

        def reports(entity_id):
            old = State(entity_id, "off", {"sampled_at_utc": "invalid"}, last_changed=now, last_updated=now)
            new = State(entity_id, "off", last_changed=now, last_updated=now + timedelta(seconds=1))
            return new, old

        try:
            for original in ("switch.first", "switch.second"):
                await store.async_save_ownership({
                    "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
                    "ev_smart_charging_control_topology": {
                        CONF_EV_CHARGER: original, CONF_EV_CONNECTED: "binary_sensor.connected",
                    },
                })
                assert executor.note_native_ev_signal_report(original, *reports(original)) is None
            assert executor.note_native_ev_signal_report("switch.first", *reports("switch.first")) is None
            retired_report, _explicit = reports("switch.first")
            executor.note_native_ev_signal_heartbeat(retired_report, now)
            assert set(executor._native_ev_signal_clocks) == {"switch.second"}
            connected = "binary_sensor.connected"
            current, explicit = reports(connected)
            assert executor.note_native_ev_signal_report(connected, current, explicit) is None
            assert executor.note_native_ev_signal_report(connected, explicit, current) is None
            assert executor._native_ev_signal_clocks[connected].started_at is None
            assert executor.note_native_ev_signal_report(connected, None, explicit) is None
            assert set(executor._native_ev_signal_clocks) == {"switch.second"}
            current, explicit = reports("switch.second")
            assert executor.note_native_ev_signal_report("switch.second", None, current) is None
            assert not executor._native_ev_signal_clocks
            assert executor.note_native_ev_signal_report("switch.second", current, explicit) is None
            await store.async_save_ownership({})
            assert not _executor(hass, store)._native_ev_signal_clocks

            with ExitStack() as observations:
                retained = executor._observe_native_ev_stop(observations, {CONF_EV_CHARGER: "switch.unowned"})
                hass.states.async_set("switch.unowned", "off", {"sampled_at_utc": "invalid"})
                hass.states.async_set("switch.unowned", "off")
                assert "switch.unowned" in retained.implicit_signal_clocks
                hass.states.async_set("switch.unowned", "on")
                assert not executor._ev_grid_reservations()
            assert not _executor(hass, store)._native_ev_signal_clocks
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("clock_state", ["normal", "rewound", "rewound_zero", "rewound_limit"])
@pytest.mark.parametrize("handoff", [False, True])
def test_handoff_keeps_late_draw_as_fallback_without_reopening_confirmed_capacity(
    tmp_path, monkeypatch, clock_state, handoff,
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        start = now = dt_util.utcnow()
        clock_rewound = clock_state != "normal"
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.options.update(ev_limit_min=1, ev_limit_max=9)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": {
                "ev_power_entity": "sensor.ev_power", "ev_power_limit_entity": "number.limit",
            },
        })
        reservation = {"load_kw": 7, "pending_power_limit": {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (start - timedelta(seconds=3)).isoformat(),
        }}
        confirmed_at = start + timedelta(seconds=20) if clock_rewound else start
        _record_ev_cap_confirmation(reservation, 3, confirmed_at)
        executor._ev_grid_reservations()["runtime"] = reservation
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
        power_id = "sensor.ev_power"
        zero_attrs = {"unit_of_measurement": "kW", "sampled_at_utc": confirmed_at.isoformat()}
        if clock_state in {"rewound_zero", "rewound_limit"}:
            now = confirmed_at
        if clock_state == "rewound_zero":
            hass.states.async_set(power_id, 0, zero_attrs, timestamp=now.timestamp())
        observer = None
        if handoff:
            executor.retain_ev_signal_clock_handoff()
            observer = hass.data[DOMAIN][EV_SIGNAL_CLOCK_HANDOFFS]["runtime"]
        try:
            if clock_state == "rewound_zero":
                now = start
                before = hass.states.get(power_id)
                hass.states.async_set(power_id, 0, zero_attrs, timestamp=now.timestamp())
                assert hass.states.get(power_id) is before
                if not handoff:
                    evidence = []
                    executor.note_ev_grid_power_report(
                        before, received_at=now, on_evidence_changed=lambda: evidence.append(True),
                    )
                    assert evidence == [True]
                assert reservation["load_kw"] == 3 and reservation["cap_reduction"]["feedback_clock_invalid"] is True
                now = confirmed_at
            elif clock_state == "rewound_limit":
                now = start
                cap = hass.states.get("number.limit")
                hass.states.async_set(cap.entity_id, 2, cap.attributes, timestamp=now.timestamp())
                if not handoff:
                    executor.sync_ev_grid_reservation(limit_state=hass.states.get(cap.entity_id))
                assert reservation["load_kw"] == 3 and reservation["cap_reduction"]["feedback_clock_invalid"] is True
                now = confirmed_at
            for draw in (7, 8):
                now = confirmed_at + timedelta(seconds=1) if clock_rewound and draw == 8 else now + timedelta(seconds=1)
                hass.states.async_set(power_id, draw, {
                    "unit_of_measurement": "kW", "sampled_at_utc": (start - timedelta(seconds=1)).isoformat(),
                }, timestamp=now.timestamp())
                if not handoff:
                    executor.note_ev_grid_power_report(hass.states.get(power_id))
                assert reservation["load_kw"] == (draw if clock_rewound else 3)
            assert reservation["cap_reduction"]["retained_load_kw"] == 8
            await executor.async_persist_ev_grid_reservation()
            saved = PlannerStore(hass, "runtime")
            await saved.async_load()
            assert saved.data["ev_grid_reservation"]["cap_reduction"]["retained_load_kw"] == 8
        finally:
            if observer is not None:
                observer.close()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("clock_state", ["rewound", "caught_up", "disputed"])
def test_future_restored_ev_cap_proof_cannot_suppress_current_draw(tmp_path, monkeypatch, handoff, clock_state):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        now = start = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        _freeze_entry_clock(monkeypatch, lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": {
                "ev_power_entity": "sensor.ev_power", "ev_power_limit_entity": "number.limit",
            },
        })
        previous_epoch = now + timedelta(seconds=20)
        reservation = {"load_kw": 7}
        for cap, confirmed_at in ((4, now + timedelta(seconds=10)), (3, previous_epoch)):
            reservation["pending_power_limit"] = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": cap, "physical_power_kw": cap,
                "at": (confirmed_at - timedelta(seconds=1)).isoformat(),
            }
            _record_ev_cap_confirmation(reservation, cap, confirmed_at)
        await store.async_save_ev_grid_reservation({"active": True, **reservation})
        restored = PlannerStore(hass, "runtime")
        await restored.async_load()
        entry = SimpleNamespace(entry_id="runtime", options=executor.options)
        _rehydrate_ev_grid_reservation(hass, entry, restored.data)
        reservations = executor._ev_grid_reservations()
        current = reservations["runtime"]
        assert current["load_kw"] == 5
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
        observer = None
        try:
            if clock_state != "rewound":
                now = previous_epoch + timedelta(seconds=1)
            if handoff:
                executor.retain_ev_signal_clock_handoff()
                observer = hass.data[DOMAIN][EV_SIGNAL_CLOCK_HANDOFFS]["runtime"]

            def report(draw, sampled_at):
                hass.states.async_set("sensor.ev_power", draw, {
                    "unit_of_measurement": "kW", "sampled_at_utc": sampled_at.isoformat(),
                }, timestamp=now.timestamp())
                if not handoff:
                    executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

            if clock_state == "disputed":
                report(6, previous_epoch)
                assert current["load_kw"] == 6
                now += timedelta(seconds=1)
            report(8, start)
            assert current["load_kw"] == 8
            expected_latest = previous_epoch if clock_state == "disputed" else start
            assert current["observed_load_at"] == expected_latest.isoformat()
            assert current["cap_reduction"]["feedback_clock_invalid"] is True
            expected_load = 8
            if clock_state == "disputed":
                assert "pending_power_limit" not in current
                report(3, previous_epoch + timedelta(seconds=1))
                assert current["load_kw"] == 8
            if clock_state == "rewound" and not handoff:
                fresh_cap = start + timedelta(seconds=2)
                peak_at = start + timedelta(seconds=1)
                hass.states.async_set("number.limit", 2, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
                current["pending_power_limit"] = {
                    "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 2, "physical_power_kw": 2,
                    "at": peak_at.isoformat(),
                }
                now = fresh_cap
                report(2, fresh_cap)
                assert current["load_kw"] == 2 and current["cap_reduction"]["prior_confirmation_at"] is None
                now += timedelta(seconds=1)
                report(9, peak_at)
                assert current["load_kw"] == 2
                now += timedelta(seconds=1)
                report(4, fresh_cap)
                expected_load = 9
                assert current["load_kw"] == expected_load
            await executor.async_persist_ev_grid_reservation()
            await restored.async_load()
            assert restored.data["ev_grid_reservation"]["load_kw"] == expected_load
            reservations.clear()
            _rehydrate_ev_grid_reservation(hass, entry, restored.data)
            assert reservations["runtime"]["cap_reduction"]["feedback_clock_invalid"] is True
        finally:
            if observer is not None:
                observer.close()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("already_hydrated", [False, True])
def test_initial_setup_persists_invalid_cap_clock_without_meter_feedback(tmp_path, monkeypatch, already_hydrated):
    async def run():
        start = now = dt_util.utcnow()
        _freeze_entry_clock(monkeypatch, lambda: now)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        topology = {"ev_power_entity": "sensor.ev_power", "ev_power_limit_entity": "number.limit"}
        entry = ConfigEntry(
            domain=DOMAIN, title="Cap clock recovery", data=topology,
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 5},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": topology,
        })
        original = start + timedelta(seconds=20)
        reservation = {"active": True, "load_kw": 8, "pending_power_limit": {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (original - timedelta(seconds=1)).isoformat(),
        }}
        _record_ev_cap_confirmation(reservation, 3, original)
        if not already_hydrated:
            reservation.pop("confirmed_cap_kw")
        await store.async_save_ev_grid_reservation(reservation)
        if already_hydrated:
            now = original + timedelta(seconds=1)
            assert not _rehydrate_ev_grid_reservation(hass, entry, store.data)
            now = start

        async def first_refresh(_coordinator):
            persisted = PlannerStore(hass, entry.entry_id)
            await persisted.async_load()
            assert persisted.data["ev_grid_reservation"]["cap_reduction"]["feedback_clock_invalid"] is True

        async def skip_platform_setup(_entry, _platforms):
            pass

        monkeypatch.setattr(EnergyPlannerCoordinator, "async_config_entry_first_refresh", first_refresh)
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", skip_platform_setup)
        monkeypatch.setattr("custom_components.ha_energy_planner._async_sync_planner_device", lambda *_args: None)
        try:
            assert await async_setup_entry(hass, entry)
            await entry.runtime_data.async_shutdown()
        finally:
            await hass.async_stop(force=True)

        now = original + timedelta(seconds=1)
        restarted = HomeAssistant(str(tmp_path))
        try:
            stored = PlannerStore(restarted, entry.entry_id)
            await stored.async_load()
            assert not _rehydrate_ev_grid_reservation(restarted, entry, stored.data)
            executor = EnergyPlannerCoordinator(restarted, entry, stored).executor
            restarted.states.async_set("sensor.ev_power", 8, {
                "unit_of_measurement": "kW", "sampled_at_utc": start.isoformat(),
            }, timestamp=now.timestamp())
            assert executor.note_ev_grid_power_report(restarted.states.get("sensor.ev_power"))
            assert executor._ev_grid_reservations()[entry.entry_id]["load_kw"] == 8
        finally:
            await restarted.async_stop(force=True)

    asyncio.run(run())


def test_ev_clock_rollback_retires_a_reopened_pending_cap(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        start = now = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        executor.options.update(ev_limit_min=1, ev_limit_max=9)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1}
        hass.states.async_set("number.limit", 3, attrs)
        reservation = {"load_kw": 8}
        for cap, offset in ((4, 10), (3, 20)):
            confirmed_at = start + timedelta(seconds=offset)
            reservation["pending_power_limit"] = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": cap, "physical_power_kw": cap,
                "at": (confirmed_at - timedelta(seconds=1)).isoformat(),
            }
            _record_ev_cap_confirmation(reservation, cap, confirmed_at)
        executor._ev_grid_reservations()["runtime"] = reservation

        def report(drawn_kw, received, sampled):
            nonlocal now
            now = start + timedelta(seconds=received)
            hass.states.async_set("sensor.ev_power", drawn_kw, {
                "unit_of_measurement": "kW", "sampled_at_utc": (start + timedelta(seconds=sampled)).isoformat(),
            }, timestamp=now.timestamp())
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

        try:
            report(8, 21, 20)
            assert reservation["load_kw"] == 8 and reservation["pending_power_limit"]["value"] == 3
            report(0, 1, 1)
            assert "pending_power_limit" not in reservation
            assert reservation[EV_RETIRED_CAP_KW] == 3
            report(0, 22, 22)
            assert reservation["load_kw"] == 8
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("scenario", [
    "rejected_start", "cold_pending", "cold_past_pending", "cold_pending_with_write", "cold_dispute",
])
def test_ev_pending_clock_retirement_survives_acquisition_or_startup(tmp_path, monkeypatch, scenario):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        start = dt_util.utcnow()
        past_pending = scenario in {"cold_past_pending", "cold_pending_with_write"}
        if past_pending:
            start -= timedelta(seconds=20)
        now = start + timedelta(seconds=10)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9, grid_import_limit_kw=20)
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
        pending = {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (start + timedelta(seconds=9)).isoformat(),
        }
        reservations = executor._ev_grid_reservations()
        reservation = {"load_kw": 8, "limit_kw": 20, "reserved_at": start.isoformat()}
        if scenario == "cold_dispute":
            earlier = start + timedelta(seconds=5)
            reservation["pending_power_limit"] = {
                **pending, "value": 4, "physical_power_kw": 4, "at": earlier.isoformat(),
            }
            _record_ev_cap_confirmation(reservation, 4, earlier)
        reservation["pending_power_limit"] = dict(pending)
        if scenario == "cold_dispute":
            _record_ev_cap_confirmation(reservation, 3, now)
        reservations["runtime"] = reservation

        def report(draw):
            hass.states.async_set("sensor.ev_power", draw, {
                "unit_of_measurement": "kW", "sampled_at_utc": now.isoformat(),
            }, timestamp=now.timestamp())
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

        try:
            report(8)
            plan, context = _command("ev")
            action = plan.actions[0]
            action.desired_state.update(projected_load_kw_now=7, power_limit=dict(pending))
            context.slots[0].projected_ev_load_kw = 7
            if scenario == "rejected_start":
                reason, previous = executor._reserve_ev_grid_capacity(action, context, now)
                assert reason is None and previous["pending_power_limit"] == pending
                now = start + timedelta(seconds=1)
                report(0)
                assert "pending_power_limit" not in reservations["runtime"]
                await executor._async_rollback_ev_start(previous, {}, command_sent=False)
            else:
                await executor.async_persist_ev_grid_reservation()
                restored = PlannerStore(hass, "runtime")
                await restored.async_load()
                reservations.clear()
                restart_at = start + timedelta(seconds=20 if past_pending else 1)
                _freeze_entry_clock(monkeypatch, lambda: restart_at)
                entry = SimpleNamespace(entry_id="runtime", options=executor.options)
                assert _rehydrate_ev_grid_reservation(hass, entry, restored.data) is (not past_pending)

            assert reservations["runtime"][EV_RETIRED_CAP_KW] == 3
            assert "pending_power_limit" not in reservations["runtime"]
            now = start + timedelta(seconds=21 if past_pending else 11)
            fresh_pending = None
            if scenario == "cold_pending_with_write":
                fresh_pending = {**pending, "value": 2, "physical_power_kw": 2, "at": now.isoformat()}
                written = EVCommandResult(True, "ev_started", {}, {"pending_power_limit": fresh_pending},
                                          power_limit_written=True)
                executor._reconcile_ev_grid_reservation(action, written, None)
            if scenario != "rejected_start":
                unchanged = EVCommandResult(
                    True, "ev_already_charging", {}, {"pending_power_limit": {**pending, "at": now.isoformat()}},
                )
                executor._reconcile_ev_grid_reservation(action, unchanged, None)
            report(0)
            assert reservations["runtime"]["load_kw"] == 8
            assert reservations["runtime"].get("pending_power_limit") == fresh_pending
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("rewind_during_acquisition", [False, True])
def test_ev_rejected_acquisition_keeps_only_current_cap_timing(tmp_path, monkeypatch, rewind_during_acquisition):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        start = now = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9, grid_import_limit_kw=20)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1}
        hass.states.async_set("number.limit", 3, attrs)
        pending = {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (start + timedelta(seconds=19)).isoformat(),
        }
        reservation = {"load_kw": 8, "limit_kw": 20, "pending_power_limit": pending}
        _record_ev_cap_confirmation(reservation, 3, start + timedelta(seconds=20))
        reservations = executor._ev_grid_reservations()
        reservations["runtime"] = reservation

        def report(draw):
            hass.states.async_set("sensor.ev_power", draw, {
                "unit_of_measurement": "kW", "sampled_at_utc": now.isoformat(),
            }, timestamp=now.timestamp())
            executor.note_ev_grid_power_report(hass.states.get("sensor.ev_power"))

        try:
            if rewind_during_acquisition:
                now = start + timedelta(seconds=21)
                plan, context = _command("ev")
                reason, previous = executor._reserve_ev_grid_capacity(plan.actions[0], context, now)
                assert reason is None and "confirmed_power_at" in previous
                now = start
                report(0)
                assert previous["cap_reduction"]["feedback_clock_invalid"] is True
                await executor._async_rollback_ev_start(previous, {}, command_sent=False)
                assert "confirmed_power_at" not in reservations["runtime"]
                assert reservations["runtime"]["confirmed_cap_kw"] == 3
                return
            report(8)
            assert reservation["cap_reduction"]["feedback_clock_invalid"] is True
            hass.states.async_set("number.limit", 2, attrs)
            fresh = {**pending, "value": 2, "physical_power_kw": 2, "at": now.isoformat()}
            plan, context = _command("ev")
            action = plan.actions[0]
            action.desired_state.update(projected_load_kw_now=7, power_limit=fresh)
            context.slots[0].projected_ev_load_kw = 7
            written = EVCommandResult(True, "ev_started", {}, {"pending_power_limit": fresh},
                                      power_limit_written=True)
            executor._reconcile_ev_grid_reservation(action, written, None)
            reason, previous = executor._reserve_ev_grid_capacity(action, context, now)
            assert reason is None and previous["pending_power_limit"] == fresh
            await executor._async_rollback_ev_start(previous, {}, command_sent=False)
            assert reservations["runtime"]["pending_power_limit"] == fresh
            now += timedelta(seconds=1)
            report(2)
            assert reservations["runtime"]["load_kw"] == 2
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("immediate_confirmation", [False, True])
def test_ev_cap_result_from_future_clock_cannot_grant_headroom(tmp_path, monkeypatch, immediate_confirmation):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        start = dt_util.utcnow()
        written_at = now = start + timedelta(seconds=10)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_entity="sensor.ev_power", ev_power_limit_entity="number.limit")
        executor.options.update(ev_charge_rate_kw=5, ev_limit_min=1, ev_limit_max=9, grid_import_limit_kw=20)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1}
        hass.states.async_set("switch.charger", "on", timestamp=now.timestamp())
        hass.states.async_set("number.limit", 6, attrs, timestamp=now.timestamp())
        power_id = "sensor.ev_power"

        def publish(draw):
            hass.states.async_set(power_id, draw, {
                "unit_of_measurement": "kW", "sampled_at_utc": now.isoformat(),
            }, timestamp=now.timestamp())

        async def set_limit(call):
            nonlocal now
            hass.states.async_set("number.limit", call.data["value"], attrs, timestamp=now.timestamp())
            now = start
            publish(8)
            executor.note_ev_grid_power_report(hass.states.get(power_id))

        hass.services.async_register("number", "set_value", set_limit)
        reservations = executor._ev_grid_reservations()
        reservations["runtime"] = {"load_kw": 8, "limit_kw": 20}
        plan, context = _command("ev")
        action = plan.actions[0]
        control = {"entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3}
        action.desired_state.update(projected_load_kw_now=3, power_limit=control)
        context.slots[0].projected_ev_load_kw = 3
        reason, previous = executor._reserve_ev_grid_capacity(action, context, now)
        assert reason is None
        try:
            if immediate_confirmation:
                hass.states.async_set("number.limit", 3, attrs, timestamp=now.timestamp())
                now = start
                result = EVCommandResult(True, "ev_started", {}, {
                    "confirmed_power_kw": 3, "confirmed_power_at": written_at.isoformat(),
                }, power_limit_written=True)
            else:
                adapter = EVChargerAdapter(hass, executor.entry_data, power_options=executor.options,
                                           confirmation_timeout_seconds=0, confirmation_retries=0)
                result = await adapter.async_execute(action)
                assert result.applied and result.power_limit_written
                assert result.post_state["pending_power_limit"]["at"] == written_at.isoformat()

            executor._reconcile_ev_grid_reservation(action, result, previous)
            reservation = reservations["runtime"]
            assert "pending_power_limit" not in reservation and reservation[EV_RETIRED_CAP_KW] == 3
            now = written_at + timedelta(seconds=1)
            publish(0)
            executor.note_ev_grid_power_report(hass.states.get(power_id))
            assert reservation["load_kw"] == 8
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("scoped", [False, True])
def test_native_control_heartbeat_retires_future_cap_clock_without_meter(tmp_path, monkeypatch, scoped):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        start = dt_util.utcnow()
        now = start + timedelta(seconds=20)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        topology = {CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit"}
        entry = ConfigEntry(
            domain=DOMAIN, title="Native cap clock without meter", data=topology,
            options={**DEFAULT_OPTIONS, "ev_limit_min": 1, "ev_limit_max": 9},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": topology,
        })
        owner = EnergyPlannerCoordinator(hass, entry, store)
        reservations = owner.executor._ev_grid_reservations()
        reservations[entry.entry_id] = reservation = {"load_kw": 8, "pending_power_limit": {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (now - timedelta(seconds=1)).isoformat(),
        }}
        _record_ev_cap_confirmation(reservation, 3, now)
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
        hass.states.async_set("switch.charger", "off", timestamp=now.timestamp())
        before = hass.states.get("switch.charger")
        feedback = []
        try:
            with ExitStack() as observers:
                if scoped:
                    owner.executor.observe_ev_setup(observers, feedback.append)
                else:
                    owner.async_start_listeners()
                now = start
                hass.states.async_set("switch.charger", "off", timestamp=now.timestamp())
                assert hass.states.get("switch.charger") is before
                assert reservation["cap_reduction"]["feedback_clock_invalid"] is True
                if scoped:
                    assert feedback == [False]
                else:
                    await hass.async_block_till_done()
                    saved = PlannerStore(hass, entry.entry_id)
                    await saved.async_load()
                    assert saved.data["ev_grid_reservation"]["cap_reduction"]["feedback_clock_invalid"] is True
        finally:
            await owner.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_handoff_observer_replaces_and_closes_obsolete_bindings(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)

        async def own(limit):
            await store.async_save_ownership({
                "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
                "ev_smart_charging_control_topology": {"ev_power_limit_entity": limit},
            })

        await own("number.first")
        domain = executor._ev_grid_domain_data()
        try:
            hass.data[DOMAIN] = None
            executor.retain_ev_signal_clock_handoff()
            hass.data[DOMAIN] = domain
            domain["ev_grid_reservations"] = None
            executor.retain_ev_signal_clock_handoff()
            assert EV_SIGNAL_CLOCK_HANDOFFS not in domain
            domain.pop("ev_grid_reservations")

            executor.retain_ev_signal_clock_handoff()
            handoffs = domain[EV_SIGNAL_CLOCK_HANDOFFS]
            original = handoffs["runtime"]
            assert original.subscriptions
            await own("number.second")
            executor.retain_ev_signal_clock_handoff()
            replacement = handoffs["runtime"]
            assert replacement is not original and replacement.subscriptions and not original.subscriptions

            await store.async_save_ownership({})
            executor.retain_ev_signal_clock_handoff()
            assert not replacement.subscriptions and not handoffs
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("binding", ["regular_control", "retained_limit_only"])
def test_native_ev_watch_subscribes_only_to_relevant_report_and_change_sources(tmp_path, monkeypatch, binding):
    from custom_components.ha_energy_planner import coordinator as coordinator_module

    async def run():
        hass = HomeAssistant(str(tmp_path))
        control = "switch.charger"
        original_limit = "number.original_limit"
        original = {"ev_power_limit_entity": original_limit}
        if binding == "regular_control":
            original.update({CONF_EV_CHARGER: control, CONF_EV_CHARGING: control})
        configured = original if binding == "regular_control" else {"ev_power_limit_entity": "number.replacement"}
        entry = ConfigEntry(
            domain=DOMAIN, title="Native feedback listener coverage", data=configured, options={**DEFAULT_OPTIONS},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": original,
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        changes, reports = [], []
        register_changes = coordinator_module.async_track_state_change_event
        register_reports = coordinator_module.async_track_state_report_event

        def track_changes(hass, sources, handler):
            changes.append(tuple(sources))
            return register_changes(hass, sources, handler)

        def track_reports(hass, sources, handler):
            reports.append(tuple(sources))
            return register_reports(hass, sources, handler)

        monkeypatch.setattr(coordinator_module, "async_track_state_change_event", track_changes)
        monkeypatch.setattr(coordinator_module, "async_track_state_report_event", track_reports)
        try:
            coordinator.async_start_listeners()
            if binding == "regular_control":
                assert reports == [(control,)] and changes == [tuple(sorted((original_limit, control)))]
            else:
                assert not reports and (original_limit,) in changes
                coordinator.executor.retain_ev_signal_clock_handoff()
                handoff = hass.data[DOMAIN][EV_SIGNAL_CLOCK_HANDOFFS][entry.entry_id]
                try:
                    assert len(handoff.subscriptions) == 1
                finally:
                    handoff.close()
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("feedback", [
    "meter_draw", "activity_heartbeat", "activity_meter", "activity_control",
    "implicit_control", "implicit_remapped_connection",
])
def test_terminal_ev_restore_retries_advanced_stop_feedback(tmp_path, monkeypatch, feedback):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        attributes = {"unit_of_measurement": "kW", "min": 1, "max": 10}
        power_attributes = {"unit_of_measurement": "kW"}
        hass.states.async_set("number.limit", 9, attributes)
        initial_at = dt_util.utcnow()
        activity_at = initial_at + timedelta(seconds=1)
        clock = activity_at
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        control_source = "binary_sensor.connected" if feedback == "implicit_remapped_connection" else "switch.charger"
        hass.states.async_set("switch.charger", "off", {"sampled_at_utc": initial_at.isoformat()},
                              timestamp=initial_at.timestamp())
        if feedback.startswith("implicit"):
            hass.states.async_set(control_source, "off", {"sampled_at_utc": "invalid"},
                                  timestamp=initial_at.timestamp())
        if feedback == "activity_meter":
            power_attributes["sampled_at_utc"] = initial_at.isoformat()
        hass.states.async_set("sensor.ev_power", 0, power_attributes, timestamp=initial_at.timestamp())
        original = {CONF_EV_CHARGER: "switch.charger", "ev_power_limit_entity": "number.limit",
                    "ev_power_entity": "sensor.ev_power"}
        configured = original
        if feedback == "implicit_remapped_connection":
            original = {**original, CONF_EV_CONNECTED: control_source}
            configured = {**original, CONF_EV_CONNECTED: "binary_sensor.new"}
        entry = ConfigEntry(
            domain=DOMAIN, title="Reconcile fresh stop evidence", data=configured,
            options={**DEFAULT_OPTIONS, "ev_confirmation_timeout_seconds": 0},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_production({"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }})
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": original,
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        reservation = {"load_kw": 8}
        if feedback == "meter_draw":
            reservation.update(observed_load_at=activity_at.isoformat(), observed_report_kw=8)
        else:
            reservation["observed_activity_at"] = activity_at.isoformat()
        coordinator.executor._ev_grid_reservations()[entry.entry_id] = reservation
        try:
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            assert store.data["ownership"] and store.data["ev_grid_reservation"]["active"]
            clock += timedelta(seconds=1)
            if feedback.startswith("implicit"):
                hass.states.async_set(control_source, "off", timestamp=clock.timestamp())
                clock += timedelta(seconds=1)
                hass.states.async_set(control_source, "off", {"name": "Charger"}, timestamp=clock.timestamp())
            elif feedback == "activity_control":
                hass.states.async_set("switch.charger", "off", {"sampled_at_utc": clock.isoformat()},
                                      timestamp=clock.timestamp())
            else:
                old_state = hass.states.get("sensor.ev_power")
                if feedback == "activity_meter":
                    power_attributes = {**power_attributes, "sampled_at_utc": clock.isoformat()}
                hass.states.async_set("sensor.ev_power", 0, power_attributes, timestamp=clock.timestamp())
                if feedback != "activity_meter":
                    assert hass.states.get("sensor.ev_power") is old_state
            await hass.async_block_till_done()
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert not calls and not saved.data["ownership"]
            assert saved.data["ev_grid_reservation"] == {"active": False}
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("feedback", ["explicit_source", "positive_draw", "already_confirmed"])
def test_terminal_power_heartbeat_ignores_existing_or_nonstop_evidence(feedback):
    now = dt_util.utcnow()
    draw_at = now - timedelta(seconds=2)
    attributes = {"unit_of_measurement": "kW"}
    if feedback == "explicit_source":
        attributes["sampled_at_utc"] = now.isoformat()
    state = State("sensor.ev_power", "1" if feedback == "positive_draw" else "0", attributes, last_updated=now)
    old_report = now - timedelta(seconds=1 if feedback == "already_confirmed" else 3)
    retries = []
    coordinator = SimpleNamespace(
        _failed_startup_restore_watch_cancel=lambda: None,
        executor=SimpleNamespace(ev_grid_activity_at=lambda: draw_at),
        store=SimpleNamespace(data={"ev_grid_reservation": {"active": True}}),
        _queue_failed_startup_restore=lambda: retries.append(state),
    )
    startup_recovery._observe_failed_startup_power_report(
        coordinator, SimpleNamespace(data={"new_state": state, "old_last_reported": old_report, "last_reported": now}),
    )
    assert not retries


@pytest.mark.parametrize("write", ["active", "proposal", "external", "retained", "rejected_proposal"])
def test_native_stop_store_failure_keeps_recovery_evidence(tmp_path, monkeypatch, write):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("number.limit", 9, {"unit_of_measurement": "kW", "min": 1, "max": 10})
        hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor._ev_grid_reservations()["runtime"] = {"load_kw": 7}
        await store.async_save_ownership({"ev_smart_charging_state": {
            CONF_EV_CHARGER: "on" if write == "external" else "off",
            "ev_power_limit_entity": "9", "ev_power_limit_unit": "kW",
        }})
        await store.async_save_command_rate_limits({"ev_safety_stop_failed_at": dt_util.utcnow().isoformat()})
        exposure = {"at": (dt_util.utcnow() - timedelta(minutes=1)).isoformat(), "cost_per_hour": 3}
        await store.async_save_ev_telemetry({"command_exposure": exposure})
        save = store._store.async_save
        failed = False

        async def interrupted_save(data):
            nonlocal failed
            proposal = data.get("_pending_ev_release")
            reservation = data["ev_grid_reservation"]
            if write in {"retained", "rejected_proposal"} and proposal:
                hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
                await asyncio.sleep(0)
            stages = {
                "active": reservation.get("active") is True and not proposal,
                "proposal": bool(proposal),
                "rejected_proposal": bool(proposal),
                "external": reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is True,
                "retained": reservation.get("load_kw", 0) >= 8 and not proposal,
            }
            if stages[write] and not failed:
                failed = True
                raise OSError("simulated native release save failure")
            await save(data)

        monkeypatch.setattr(store._store, "async_save", interrupted_save)
        try:
            with pytest.raises(OSError, match="simulated native release save failure"):
                await executor.async_restore_device_control("ev", "button_pressed")
            saved = PlannerStore(hass, "runtime")
            await saved.async_load()
            assert saved.data["ev_grid_reservation"]["active"]
            if write == "external":
                assert not saved.data["ownership"]
                assert saved.data["ev_grid_reservation"][EV_RESERVATION_EXTERNAL_BASELINE] is True
                assert "ev_safe_stop_confirmed_at" in saved.data["command_rate_limits"]
            else:
                assert saved.data["ownership"] and saved.data["ev_telemetry"]["command_exposure"] == exposure
                if write in {"retained", "rejected_proposal"}:
                    assert saved.data["ev_grid_reservation"]["load_kw"] == 8
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["manual", "recovery"])
@pytest.mark.parametrize("edit", ["before_stage", "during_stage"])
def test_native_stop_preserves_other_ownership_edits(tmp_path, monkeypatch, entrypoint, edit):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        hass.states.async_set("number.limit", 9, {"unit_of_measurement": "kW", "min": 1, "max": 10})
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data["ev_power_limit_entity"] = "number.limit"
        executor._ev_grid_reservations()["runtime"] = {"load_kw": 7}
        owned = {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"}
        await store.async_save_ownership({
            "ev_smart_charging_state": owned,
            "manual_hvac_override_expires_at": (dt_util.utcnow() + timedelta(minutes=15)).isoformat(),
        })
        await executor.async_persist_ev_grid_reservation()
        awaiting_store = asyncio.Event()
        publish = store.async_publish_ev_release

        async def publish_after_wait(stage, is_current):
            awaiting_store.set()
            return await publish(stage, is_current)

        monkeypatch.setattr(store, "async_publish_ev_release", publish_after_wait)
        operation = (
            executor.async_manual_ev_charging(False, None) if entrypoint == "manual"
            else executor.async_restore_device_control("ev", "startup_recovery_save_failed")
        )
        task = None
        try:
            if edit == "before_stage":
                async with store._save_lock:
                    task = asyncio.create_task(operation)
                    await asyncio.wait_for(awaiting_store.wait(), 5)
                    with store.defer_save():
                        await store.async_save_ownership({"ev_smart_charging_state": owned})
                result = await task
            else:
                save = store._store.async_save
                changed = False

                async def concurrent_ownership_change(data):
                    nonlocal changed
                    if data.get("_pending_ev_release") and not changed:
                        changed = True
                        with store.defer_save():
                            await store.async_save_ownership({"ev_smart_charging_state": owned})
                    await save(data)

                monkeypatch.setattr(store._store, "async_save", concurrent_ownership_change)
                result = await operation
                assert changed
            assert result.applied if entrypoint == "manual" else result.result == OutcomeResult.RESTORED
            saved = PlannerStore(hass, "runtime")
            await saved.async_load()
            assert not saved.data["ownership"] and saved.data["ev_grid_reservation"] == {"active": False}
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("already_tearing_down", [False, True])
def test_shutdown_persists_terminal_disarm_without_owned_devices(tmp_path, monkeypatch, already_tearing_down):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        hass.state = CoreState.running
        entry = ConfigEntry(
            domain=DOMAIN, title="Durable startup disarm", data={},
            options={**DEFAULT_OPTIONS, "dry_run": False, "planner_enabled": True}, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        object.__setattr__(entry, "runtime_data", coordinator)
        _arm(store, coordinator.executor)
        backend_save = store._store.async_save
        await backend_save(deepcopy(store.data))
        online = False

        async def save(data):
            if not online:
                if already_tearing_down:
                    coordinator._tearing_down = True
                raise OSError("Simulated Store unavailability")
            await backend_save(data)

        async def unload_platforms(_entry, _platforms):
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["production"]["armed"] is False
            assert saved.data["production"]["startup_auto_recovery"]["status"] == "failed"
            assert not saved.data["ownership"]
            return True

        monkeypatch.setattr(store._store, "async_save", save)
        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)
        try:
            await coordinator._async_fail_startup_recovery()
            await hass.async_block_till_done()
            assert coordinator._failed_startup_restore_flush_needed
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["production"]["armed"] is True
            online = True
            hass.state = CoreState.stopping
            assert await async_unload_entry(hass, entry)
            assert not coordinator._failed_startup_restore_flush_needed
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_operator_disarm_supersedes_queued_terminal_startup_cleanup(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = ConfigEntry(
            domain=DOMAIN, title="Supersede terminal startup write", data={}, options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        updates = []
        save_production = store.async_save_production

        async def save(data):
            updates.append(data["startup_auto_recovery"]["status"])
            await save_production(data)

        monkeypatch.setattr(store, "async_save_production", save)
        try:
            failure = asyncio.create_task(coordinator._async_fail_startup_recovery())
            disarm = asyncio.create_task(coordinator.async_operator_disarm_production_control())
            await asyncio.gather(failure, disarm)
            assert updates == ["cancelled"]
            assert store.data["production"]["armed"] is False
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("remapped", [False, True])
@pytest.mark.parametrize(("cold", "intent_kind"), [
    (False, "none"), (False, "fresh"), (True, "none"), (True, "fresh"), (True, "inherited"),
    (True, "earlier_sentinel"), (True, "fresh_after_sentinels"), (True, "before_watch"),
])
def test_terminal_restore_observes_manual_zone_off_before_retrying(tmp_path, monkeypatch, remapped, cold, intent_kind):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        hass.states.async_set("climate.home", "heat")
        intent = Context(user_id="a" * 32) if intent_kind != "none" else None
        inherited = intent if intent_kind == "inherited" else None
        hass.states.async_set("switch.zone", "unavailable" if cold else "on", context=inherited)
        zone = "switch.new_zone" if remapped else "switch.zone"
        entry = ConfigEntry(
            domain=DOMAIN, title="Preserve manual off during retained recovery",
            data={CONF_DAIKIN_CLIMATE: "climate.home", CONF_CLIMATE_ZONES: zone},
            options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        ownership = {"hvac_control": {"zone_states": {"switch.zone": "on"}}}
        await store.async_save_ownership(ownership)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)

        async def no_refresh():
            pass

        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(coordinator, "async_request_refresh", no_refresh)
        try:
            coordinator.async_start_listeners()
            if not cold:
                hass.states.async_set("switch.zone", "unavailable")
                await hass.async_block_till_done()
            if intent_kind in {"earlier_sentinel", "fresh_after_sentinels"}:
                prior_intent = intent if intent_kind == "earlier_sentinel" else Context(user_id="b" * 32)
                hass.states.async_set("switch.zone", "unknown", context=prior_intent)
                hass.states.async_set("switch.zone", "unavailable", context=Context())
                await hass.async_block_till_done()
            await store.async_save_production({"armed": False, "startup_auto_recovery": {
                "status": "failed", "last_reason": "startup_recovery_save_failed",
            }})
            if intent_kind == "before_watch":
                hass.states.async_set("switch.zone", "off", context=intent)
                await hass.async_block_till_done()
                assert "switch.zone" not in store.data["ownership"].get("hvac_control", {}).get("zone_states", {})
            startup_recovery._watch_failed_startup_restore(coordinator)
            await hass.async_block_till_done()
            if intent_kind != "before_watch":
                assert coordinator._failed_startup_restore_watch_cancel is not None
                assert not calls and store.data["ownership"]["hvac_control"]["zone_states"] == {"switch.zone": "on"}
                hass.states.async_set("switch.zone", "off", context=intent)
                await hass.async_block_till_done()
            attributed = intent_kind in {"fresh", "fresh_after_sentinels", "before_watch"}
            expected_calls = [] if attributed else [("switch", "turn_on", {"entity_id": "switch.zone"})]
            assert calls == expected_calls
            assert hass.states.get("switch.zone").state == ("off" if attributed else "on")
            assert "switch.zone" not in store.data["ownership"].get("hvac_control", {}).get("zone_states", {})
            assert coordinator._failed_startup_restore_watch_cancel is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("saved_target", "superseded"), [
    (None, False), ({"target_temperature": 22}, False), ({"target_temperature": 24}, True),
    ({"hvac_mode": "cool"}, True),
])
def test_first_observed_climate_report_supersedes_conflicting_saved_controls(saved_target, superseded):
    coordinator = object.__new__(EnergyPlannerCoordinator)
    coordinator._failed_startup_restore_watch_cancel = lambda: None
    coordinator.store = SimpleNamespace(data={
        "production": {"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }},
        "ownership": {"hvac_control": {"zone_states": {"climate.zone": saved_target}}},
    })
    event = SimpleNamespace(data={
        "old_state": None,
        "new_state": State("climate.zone", "heat", {"temperature": 22}, context=Context(user_id="a" * 32)),
    })
    assert coordinator._manual_edit_before_first_hvac_observation(
        event, "climate.zone", _HvacOutageFeedback(None, set()), _HvacOutages(),
    ) is superseded


@pytest.mark.parametrize(("device", "before_listeners"), [("zone", True), ("main", True), ("main", False)])
@pytest.mark.parametrize("attributed", [False, True])
def test_terminal_restore_tracks_first_hvac_edit_during_ev_restore(tmp_path, device, before_listeners, attributed):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        hass.states.async_set("switch.charger", "on")
        target = "climate.home" if device == "main" else "switch.old_zone"
        edited, baseline = ("heat", "off") if device == "main" else ("off", "on")
        hass.states.async_set(target, "unavailable")
        entry = ConfigEntry(
            domain=DOMAIN, title="Keep operator intent during initial device restoration",
            data={
                CONF_DAIKIN_CLIMATE: "climate.home", CONF_CLIMATE_ZONES: "switch.new_zone",
                CONF_EV_CHARGER: "switch.charger",
            },
            options=DEFAULT_OPTIONS, source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        hvac_control = {"main_state": {"hvac_mode": baseline}} if device == "main" else {"zone_states": {
            target: baseline,
        }}
        await store.async_save_ownership({
            "hvac_control": hvac_control, "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
        })
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        dispatched, finish = asyncio.Event(), asyncio.Event()
        reconcile = None

        async def stop_ev(call):
            calls.append((call.domain, call.service, dict(call.data)))
            dispatched.set()
            await finish.wait()
            hass.states.async_set("switch.charger", "off", context=call.context)

        hass.services.async_register("switch", "turn_off", stop_ev)
        try:
            if not before_listeners:
                coordinator.async_start_listeners()
            await store.async_save_production({"armed": False, "startup_auto_recovery": {
                "status": "failed", "last_reason": "startup_recovery_save_failed",
            }})
            reconcile = asyncio.create_task(coordinator.async_reconcile_production_evidence_contract())
            await asyncio.wait_for(dispatched.wait(), 5)
            context = Context(user_id="a" * 32) if attributed else None
            hass.states.async_set(target, edited, context=context)
            await asyncio.sleep(0)
            if attributed:
                assert coordinator.hvac_handoff_unresolved
            finish.set()
            assert await asyncio.wait_for(reconcile, 5)
            await hass.async_block_till_done()
            assert calls[0] == ("switch", "turn_off", {"entity_id": "switch.charger"})
            assert len(calls) == (1 if attributed else 2)
            assert hass.states.get(target).state == (edited if attributed else baseline)
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            saved_hvac = saved.data["ownership"].get("hvac_control", {})
            assert not saved_hvac.get("main_state") and not saved_hvac.get("zone_states")
            assert bool(saved.data["overrides"]) is attributed
        finally:
            finish.set()
            if reconcile is not None and not reconcile.done():
                await asyncio.gather(reconcile, return_exceptions=True)
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("mapped_target", ["number.export_limit", "select.export_limit", None])
def test_durable_export_readback_retires_terminal_restore_watcher(tmp_path, monkeypatch, mapped_target):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.starting
        entry = ConfigEntry(
            domain=DOMAIN, title="Retire completed startup restoration",
            data={CONF_ENPHASE_EXPORT_LIMIT_ENTITY: mapped_target}, options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        await store.async_save_production({"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }})
        ownership = {"enphase_export_limit": {"baseline": {"watts": 5000}, "restoring": True}}
        if mapped_target is None:
            ownership["enphase_profile"] = "Original profile"
        await store.async_save_ownership(ownership)

        async def confirm_export_restore(control, _now):
            await control.save({})
            return OutcomeResult.RESTORED, "export_limit_restored", False

        monkeypatch.setattr(ExportLimitControl, "restore", confirm_export_restore)
        readback = "sensor.export_readback" if mapped_target == "select.export_limit" else None
        monkeypatch.setattr(
            "custom_components.ha_energy_planner.coordinator.export_limit_feedback_entity_id", lambda *_: readback,
        )
        monkeypatch.setattr(startup_recovery, "feedback_entity_id", lambda *_: readback)
        try:
            coordinator.async_start_listeners()
            assert coordinator._failed_startup_restore_watch_cancel is not None
            if mapped_target is None:
                assert not coordinator._failed_startup_restore_device_scopes
                assert store.data["ownership"] == ownership
                return
            if readback:
                assert readback in coordinator._failed_startup_restore_device_scopes
            hass.state = CoreState.running
            if readback:
                hass.states.async_set(readback, "5000")
                await hass.async_block_till_done()
            else:
                async with coordinator._command_lock:
                    await coordinator._async_reconcile_export_limit_locked()
            assert coordinator._failed_startup_restore_watch_cancel is None
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert not saved.data["ownership"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_released_zone_feedback_retires_extra_listener(tmp_path, monkeypatch):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        entry = ConfigEntry(
            domain=DOMAIN, title="Release old zone listener",
            data={CONF_DAIKIN_CLIMATE: "climate.home", CONF_CLIMATE_ZONES: "switch.new_zone"},
            options=DEFAULT_OPTIONS,
            source="user", unique_id=None, version=5, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        await store.async_save_ownership({"hvac_control": {"zone_states": {"switch.zone": "off"}}})
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        scheduled = []
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *args, **_kwargs: scheduled.append(args))
        try:
            coordinator.async_start_listeners()
            await coordinator.async_restore_safe_state("button_pressed", refresh=False)
            assert not store.data["ownership"]
            hass.states.async_set("switch.zone", "on", context=Context(user_id="a" * 32))
            hass.states.async_set("switch.zone", "off", context=Context(user_id="a" * 32))
            await hass.async_block_till_done()
            assert not calls and not scheduled and not coordinator.overrides
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_terminal_hvac_restore_retries_when_target_step_appears():
    entity = "climate.head"
    attributes = {"temperature": 22, "min_temp": 16, "max_temp": 30}
    before = State(entity, "off", attributes)
    current = before
    adapter = DaikinHVACAdapter(SimpleNamespace(states=SimpleNamespace(get=lambda _entity: current)), {})
    retries = []
    coordinator = SimpleNamespace(
        _failed_startup_restore_watch_cancel=lambda: None,
        _failed_startup_restore_hvac_entities={entity},
        _queue_failed_startup_restore=lambda: retries.append(adapter._memory_target_available(entity, 24)),
        store=SimpleNamespace(data={"ownership": {"hvac_control": {"zone_states": {entity: "off"}}}}),
    )
    assert not adapter._memory_target_available(entity, 24)
    current = State(entity, "off", {**attributes, "current_temperature": 23})
    startup_recovery._observe_failed_startup_availability(
        coordinator, SimpleNamespace(data={"old_state": before, "new_state": current}),
    )
    assert not retries
    before = current
    current = State(entity, "off", {**before.attributes, "target_temp_step": 0.5})
    startup_recovery._observe_failed_startup_availability(
        coordinator, SimpleNamespace(data={"old_state": before, "new_state": current}),
    )
    assert retries == [True]


@pytest.mark.parametrize("ready_target", [
    "automation", "scheduler_timer", "scheduler_timer_paused", "scheduler_timer_missing_end",
    "saved_zone", "zone_target", "zone_bounds", "zone_assumed", "manual_restore", "shutdown",
])
def test_terminal_startup_retries_pending_hvac_restoration_on_availability(tmp_path, ready_target):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        calls = _devices(hass)
        data = {
            CONF_DAIKIN_CLIMATE: "climate.home", CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_ENPHASE_PROFILE: "select.profile", CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        }
        ownership = {"climate_automations": {"automation.climate": "on"}}
        if ready_target.startswith("scheduler_timer"):
            data.update({CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_guard",
                         CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard"})
            hass.states.async_set("input_boolean.scheduler_guard", "on")
            timer_state = {"scheduler_timer_paused": "paused", "scheduler_timer_missing_end": "active"}
            hass.states.async_set("timer.scheduler_guard", timer_state.get(ready_target, "unavailable"))
            hass.states.async_set("automation.climate", "off")
        elif ready_target == "saved_zone":
            data["hvac_zone_mappings"] = {"zone": {"actuator": "switch.new_zone"}}
            ownership = {"hvac_control": {"zone_states": {"switch.old_zone": "off"}}}
            hass.states.async_set("switch.old_zone", "unavailable")
            hass.states.async_set("switch.new_zone", "on")
            hass.services.async_remove("switch", "turn_off")
        elif ready_target.startswith("zone_"):
            ownership = {"hvac_control": {"zone_states": {"climate.old_zone": {"target_temperature": 24}}}}
            if ready_target == "zone_bounds":
                hass.states.async_set("climate.old_zone", "heat", {"temperature": 22, "max_temp": 23})
            elif ready_target == "zone_assumed":
                hass.states.async_set("climate.old_zone", "off", {"temperature": 24, "assumed_state": True})
            else:
                hass.states.async_set("climate.old_zone", "off")
        elif ready_target == "manual_restore":
            hass.states.async_set("automation.climate", "off")
            hass.services.async_remove("automation", "turn_on")
        else:
            hass.states.async_set("automation.climate", "unavailable")
        entry = ConfigEntry(
            domain=DOMAIN, title="Restore held HVAC controls", data=data,
            options={**DEFAULT_OPTIONS, "enphase_control_enabled": ready_target != "manual_restore"},
            source="user", unique_id=None, version=5, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        store.data["production"] = {"armed": False, "startup_auto_recovery": {
            "status": "failed", "last_reason": "startup_recovery_save_failed",
        }}
        await store.async_save_ownership(ownership)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        try:
            assert await coordinator.async_reconcile_production_evidence_contract()
            coordinator.async_start_listeners()
            await hass.async_block_till_done()
            assert not calls and store.data["ownership"]
            if ready_target == "shutdown":
                retained = deepcopy(store.data["ownership"])
                assert coordinator._failed_startup_restore_watch_cancel is not None
                await coordinator.async_shutdown()
                assert coordinator._failed_startup_restore_watch_cancel is None
                hass.states.async_set("automation.climate", "off")
                await hass.async_block_till_done()
                assert not calls and store.data["ownership"] == retained
                return
            if ready_target.startswith("scheduler_timer"):
                deadline = dt_util.utcnow() + timedelta(hours=1)
                hass.states.async_set("timer.scheduler_guard", "active", {"finishes_at": deadline.isoformat()})
            elif ready_target == "saved_zone":
                audit_size = len(store.data["execution_audit"])
                assert hass.states.async_remove("switch.old_zone")
                await hass.async_block_till_done()
                assert len(store.data["execution_audit"]) == audit_size
                hass.states.async_set("switch.old_zone", "off")
            elif ready_target.startswith("zone_"):
                observed = hass.states.get("climate.old_zone")
                audit_size = len(store.data["execution_audit"])
                hass.states.async_set("climate.old_zone", observed.state, {
                    **observed.attributes, "current_temperature": 22,
                })
                await hass.async_block_till_done()
                assert len(store.data["execution_audit"]) == audit_size
                hass.states.async_set("climate.old_zone", observed.state, {
                    "temperature": 22 if ready_target == "zone_bounds" else 24, "min_temp": 16, "max_temp": 30,
                })
            elif ready_target == "manual_restore":
                async def restore(call):
                    calls.append((call.domain, call.service, dict(call.data)))
                    hass.states.async_set(call.data["entity_id"], "on", context=call.context)

                hass.services.async_register("automation", "turn_on", restore)
                await coordinator.async_restore_safe_state("button_pressed", refresh=False)
                # A Core-start callback can already be queued when its watch is retired.
                coordinator._queue_failed_startup_restore()
            else:
                hass.states.async_set("automation.climate", "off")
            await hass.async_block_till_done()
            if ready_target == "zone_bounds":
                expected_calls = [("climate", "set_temperature", {"entity_id": "climate.old_zone", "temperature": 24})]
            elif ready_target == "saved_zone" or ready_target.startswith("zone_"):
                expected_calls = []
            else:
                expected_calls = [("automation", "turn_on", {"entity_id": "automation.climate"})]
            assert calls == expected_calls
            assert hass.states.get("select.profile").state == "Original profile"
            assert not store.data["ownership"]
            assert coordinator._failed_startup_restore_watch_cancel is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("blocked_status", ["grace", "failed", "waiting_for_home_assistant", "waiting_for_safe"])
def test_operator_disarm_wins_over_concurrent_recovery_writes(tmp_path, monkeypatch, blocked_status):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.state = CoreState.running
        entry = ConfigEntry(
            domain=DOMAIN, title="Disarm during startup", data={},
            options={**DEFAULT_OPTIONS, "dry_run": False, "planner_enabled": True}, source="user", unique_id=None,
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        _arm(store, coordinator.executor)
        reconcile_on_abort = blocked_status in {"waiting_for_home_assistant", "waiting_for_safe"}
        if blocked_status == "waiting_for_safe":
            store.data["production"]["dry_run_evidence_fingerprint"] = "obsolete"
        coordinator._startup_auto_recovery_authorized = True
        writing_progress, fail_progress = asyncio.Event(), asyncio.Event()
        write = store._store._async_write_data
        disarm_task = None
        unload_task = None
        writes_at_blocked_status = 0
        restorations = []

        async def reject_unload_drain():
            raise OSError("reservation drain rejected")

        async def record_restore(reason, **_kwargs):
            restorations.append(reason)

        async def write_data(data):
            nonlocal writes_at_blocked_status
            recovery = data["data"]["production"]["startup_auto_recovery"]
            if recovery["status"] == blocked_status:
                writes_at_blocked_status += 1
                if blocked_status == "waiting_for_home_assistant" and writes_at_blocked_status == 1:
                    raise WriteError("simulated first recovery write failure")
                writing_progress.set()
                await fail_progress.wait()
                if blocked_status != "waiting_for_safe":
                    raise WriteError("simulated atomic file write failure")
            if recovery["status"] == "grace":
                raise WriteError("simulated atomic file write failure")
            await write(data)

        monkeypatch.setattr(store._store, "_async_write_data", write_data)
        monkeypatch.setattr(coordinator, "async_restore_safe_state", record_restore)
        if reconcile_on_abort:
            object.__setattr__(entry, "runtime_data", coordinator)
            monkeypatch.setattr(coordinator, "async_wait_for_refresh_shutdown", reject_unload_drain)
            coordinator.async_start_listeners()
            unload_task = asyncio.create_task(async_unload_entry(hass, entry))
        else:
            coordinator.async_start_startup_auto_recovery()
        try:
            await asyncio.wait_for(writing_progress.wait(), 5)
            disarm_task = asyncio.create_task(coordinator.async_operator_disarm_production_control("button_pressed"))
            await asyncio.sleep(0)
            assert store.data["production"]["startup_auto_recovery"]["last_reason"] == "operator_disarmed"
            fail_progress.set()
            await asyncio.wait_for(disarm_task, 5)
            if unload_task is not None:
                with pytest.raises(OSError, match="reservation drain rejected"):
                    await unload_task
            saved = PlannerStore(hass, entry.entry_id)
            await saved.async_load()
            assert saved.data["production"]["armed"] is False
            assert saved.data["production"]["disarmed_reason"] == "button_pressed"
            recovery = saved.data["production"]["startup_auto_recovery"]
            assert recovery["status"] == "cancelled" and recovery["last_reason"] == "operator_disarmed"
            assert not await coordinator.async_reconcile_production_evidence_contract()
            assert not coordinator._startup_auto_recovery_authorized
            assert restorations == []
        finally:
            fail_progress.set()
            if disarm_task is not None and not disarm_task.done():
                await asyncio.gather(disarm_task, return_exceptions=True)
            if unload_task is not None:
                await asyncio.gather(unload_task, return_exceptions=True)
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


@pytest.mark.parametrize("interruption", [
    "pending_save", "canonical_save", "save_error", "save_error_twice", "shutdown",
])
def test_charge_now_retains_native_recovery_when_draw_supersedes_compensation(tmp_path, monkeypatch, interruption):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        attrs = {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1}
        hass.states.async_set("binary_sensor.charging", "off")
        hass.states.async_set("number.limit", 6, attrs)
        hass.states.async_set("sensor.ev_power", 0, {"unit_of_measurement": "kW"})
        entry = ConfigEntry(
            domain=DOMAIN, title="Charge now compensation", source="user", unique_id=None,
            data={CONF_EV_CHARGER: "switch.charger", CONF_EV_CONNECTED: "binary_sensor.connected",
                  CONF_EV_CHARGING: "binary_sensor.charging", "ev_power_limit_entity": "number.limit",
                  "ev_power_entity": "sensor.ev_power"},
            options={**DEFAULT_OPTIONS, "ev_charge_rate_kw": 5, "ev_limit_min": 1, "ev_limit_max": 6,
                     "grid_import_limit_kw": 10, "ev_confirmation_timeout_seconds": 0, "ev_confirmation_retries": 1},
            version=5, minor_version=1, discovery_keys=MappingProxyType({}), subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        plan, context = _command("ev")
        assert context is not None
        context.ev_target_soc_percent = 80
        await store.async_save_ev_telemetry({"command_exposure": {
            "at": context.created_at.isoformat(), "cost_per_hour": 1,
        }})

        async def refresh():
            coordinator._last_decision_context = context
            coordinator._last_decision_generation = coordinator._refresh_generation
            return plan

        async def publish_draw_then_hide_meter():
            hass.states.async_set("sensor.ev_power", 8, {"unit_of_measurement": "kW"})
            await asyncio.sleep(0)
            hass.states.async_set("sensor.ev_power", "unavailable")

        async def actuate(call):
            calls.append((call.domain, call.service, dict(call.data)))
            if call.service == "set_value":
                hass.states.async_set("number.limit", call.data["value"], attrs, context=call.context)
            elif call.service == "turn_on":
                if interruption == "shutdown":
                    assert coordinator._unsub_listeners
                    coordinator._begin_shutdown()
                    assert not coordinator._unsub_listeners
                else:
                    coordinator._mark_replan_requested()
            elif interruption == "shutdown":
                await publish_draw_then_hide_meter()

        if interruption != "shutdown":
            save = store._store.async_save
            save_error = interruption in {"save_error", "save_error_twice"}
            release_failed = recovery_save_failed = False

            async def interrupt_release(payload):
                nonlocal release_failed, recovery_save_failed
                if interruption == "canonical_save":
                    interleave = payload.get("ev_grid_reservation") == {"active": False}
                else:
                    interleave = "ownership" in payload.get("_pending_ev_release", {})
                if interleave:
                    if save_error:
                        coordinator._begin_shutdown()
                        await publish_draw_then_hide_meter()
                        release_failed = True
                        raise OSError("Simulated EV release Store failure")
                    await publish_draw_then_hide_meter()
                if release_failed and interruption == "save_error_twice" and not recovery_save_failed:
                    recovery_save_failed = True
                    raise OSError("Simulated retained reservation Store failure")
                await save(payload)

            monkeypatch.setattr(store._store, "async_save", interrupt_release)
        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(coordinator, "_schedule_debounced_refresh", lambda *_args, **_kwargs: None)
        hass.services.async_register("number", "set_value", actuate)
        hass.services.async_register("switch", "turn_on", actuate)
        hass.services.async_register("switch", "turn_off", actuate)
        try:
            if interruption in {"shutdown", "save_error", "save_error_twice"}:
                coordinator.async_start_listeners()
            if interruption in {"save_error", "save_error_twice"}:
                with pytest.raises(OSError, match="Simulated EV release Store failure"):
                    await coordinator.async_charge_now()
                if interruption == "save_error_twice":
                    assert recovery_save_failed and store._is_dirty
                    await store.async_flush()
            else:
                result = await coordinator.async_charge_now()
                assert not result.applied and result.command_sent and result.safe_state_confirmed is False
                assert not result.rollback_succeeded
            assert [service for _, service, _ in calls] == [
                "set_value", "turn_on", "turn_off", *([] if interruption == "shutdown" else ["set_value"]),
            ]
            assert coordinator._ev_allocation_deadline is not None
            latest = PlannerStore(hass, entry.entry_id)
            await latest.async_load()
            assert latest.data["ev_grid_reservation"]["active"]
            assert latest.data["ev_grid_reservation"]["load_kw"] >= 8
            assert latest.data["ev_grid_reservation"]["observed_load_at"]
            assert latest.data["ownership"]["ev_smart_charging_state"]["ev_power_limit_entity"] == "6"
            assert latest.data["ev_telemetry"]["command_exposure"]["cost_per_hour"] == 1
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
    ("unverified_draw_old_zero", 8), ("unverified_draw_new_zero", 0),
    ("live_enabled_cap", 5),
    ("live_enabled_cap_unchanged_zero", 5),
    ("queued_charging_cap", 8), ("idle_cap", 0), ("disconnected", 8), ("remapped_draw", 8),
    ("remapped_amp_limit", 3.75), ("remapped_after_limit_write", 3.75),
])
def test_rejected_first_charge_now_keeps_only_external_load(tmp_path, monkeypatch, feedback, retained_kw):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        unverified = feedback.startswith("unverified_draw")
        start = clock = dt_util.utcnow()
        if unverified:
            monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
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
            if unverified:
                clock = start + timedelta(seconds=2)
                hass.states.async_set("sensor.ev_power", 8, {
                    "unit_of_measurement": "kW", "sampled_at_utc": (start + timedelta(seconds=60)).isoformat(),
                }, timestamp=clock.timestamp())
                clock = start + timedelta(seconds=3)
                zero_source = start + timedelta(seconds=1) if feedback == "unverified_draw_old_zero" else clock
                hass.states.async_set("sensor.ev_power", 0, {
                    "unit_of_measurement": "kW", "sampled_at_utc": zero_source.isoformat(),
                }, timestamp=clock.timestamp())
            elif measured_feedback:
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
    ("power", "all", "teardown"), ("power", "all", "removed"), ("power", "all", "unload_drain"),
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
                    if intervening == "unload_drain":
                        hass.states.async_set(entity_id, 9, attributes)
                    hass.states.async_set(entity_id, value, attributes)
                    await asyncio.sleep(0)
                    if intervening == "restart":
                        hass.states.async_set(entity_id, 8, attributes)
                    elif intervening == "removed":
                        reservations.pop(entry.entry_id)
                    elif intervening == "unload_drain":
                        coordinator._begin_shutdown()
                    else:
                        coordinator._tearing_down = True
                if intervening == "unload_drain":
                    with store.defer_save():
                        await coordinator.async_wait_for_plan_execution()
                        assert store.data["ev_grid_reservation"]["active"]
                        await coordinator.async_wait_for_refresh_shutdown()
                else:
                    await hass.async_block_till_done()
                    assert store.data["ev_grid_reservation"]["active"]
                    reservations[entry.entry_id] = record
                    coordinator._tearing_down = False
            if intervening != "unload_drain":
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
            async with coordinator._command_lock:
                hass.states.async_set("sensor.ev_power", 1, attrs)
                hass.states.async_set("switch.old_charger", "off")
                hass.states.async_set("sensor.old_ev_power", 0, attrs)
                if not legacy:
                    assert coordinator.executor.external_ev_grid_stop_confirmed()
                # The replacement can emit unchanged telemetry before the original release runs.
                hass.states.async_set("sensor.ev_power", 1, attrs)
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
            assert requests == ["ev_capacity_feedback"]
            now += timedelta(seconds=1)
            if feedback != "cap_at_attach":
                hass.states.async_set("sensor.original_power", 7, power_attributes)
                await hass.async_block_till_done()
                assert requests == ["ev_capacity_feedback"]
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("replay_kind", [
    "same_explicit", "changed_explicit", "backwards_implicit", "backwards_implicit_attributes",
])
def test_invalid_meter_clock_does_not_release_capacity_on_replayed_report(
    tmp_path, monkeypatch, external, replay_kind,
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        _devices(hass)
        now = dt_util.utcnow()
        prior = (now - timedelta(seconds=1)).isoformat()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_limit_min=1, ev_limit_max=9)
        reservation = {"load_kw": 7, "reserved_at": prior}
        if external:
            reservation.update({EV_RESERVATION_EXTERNAL_BASELINE: True, "external_topology": {
                CONF_EV_CHARGER: "switch.charger", "ev_power_entity": "sensor.ev_power",
            }})
        else:
            reservation["pending_power_limit"] = {
                "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
                "at": prior,
            }
            await store.async_save_ownership({
                "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
                "ev_smart_charging_control_topology": executor.entry_data,
            })
        reservations = executor._ev_grid_reservations()
        reservations["runtime"] = reservation
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
        attributes = {"unit_of_measurement": "kW"}
        backwards = replay_kind.startswith("backwards_implicit")
        if not backwards:
            attributes["sampled_at_utc"] = (now + timedelta(seconds=2)).isoformat()
        value = 0 if external else 3
        try:
            first_receipt = now + timedelta(seconds=5) if backwards else now
            hass.states.async_set("sensor.ev_power", value, attributes, timestamp=first_receipt.timestamp())
            state = hass.states.get("sensor.ev_power")
            assert not executor.note_ev_grid_power_report(state)
            now += timedelta(seconds=3)
            new_attributes = replay_kind in {"changed_explicit", "backwards_implicit_attributes"}
            if new_attributes:
                attributes = {**attributes, "name": "Meter"}
            hass.states.async_set("sensor.ev_power", value, attributes, timestamp=now.timestamp())
            replay = hass.states.get("sensor.ev_power")
            assert (replay is state) is not new_attributes
            if backwards:
                assert replay.last_changed == first_receipt
            hass.states.async_set("switch.charger", "off", timestamp=now.timestamp())
            if external:
                assert not executor.release_stopped_external_ev_grid_capacity()
            else:
                executor.sync_ev_grid_reservation(power_state=replay)
                assert "pending_power_limit" in reservation
            assert reservations["runtime"]["load_kw"] == 7

            now += timedelta(seconds=3)
            hass.states.async_set("sensor.ev_power", value, {
                "unit_of_measurement": "kW", "sampled_at_utc": now.isoformat(),
            }, timestamp=now.timestamp())
            if external:
                assert executor.release_stopped_external_ev_grid_capacity()
            else:
                executor.sync_ev_grid_reservation()
                assert "pending_power_limit" not in reservation
                assert reservation["load_kw"] == 3
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("invalid_clock", ["malformed_source", "backwards_implicit", "future_limit_without_history"])
def test_unverified_positive_ev_power_requires_a_later_sample_to_reduce_capacity(tmp_path, monkeypatch, invalid_clock):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        start = now = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        executor.entry_data.update(ev_power_limit_entity="number.limit", ev_power_entity="sensor.ev_power")
        executor.options.update(ev_limit_min=1, ev_limit_max=9)
        await store.async_save_ownership({
            "ev_smart_charging_state": {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"},
            "ev_smart_charging_control_topology": executor.entry_data,
        })
        pending = {
            "version": 1, "entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3,
            "at": (start - timedelta(seconds=1)).isoformat(),
        }
        reservation = {"load_kw": 6, "pending_power_limit": pending}
        if invalid_clock == "future_limit_without_history":
            reservation["confirmed_power_at"] = (start + timedelta(seconds=20)).isoformat()
            reservation["confirmed_cap_kw"] = 5
        executor._ev_grid_reservations()["runtime"] = reservation
        hass.states.async_set("number.limit", 3, {"unit_of_measurement": "kW", "min": 1, "max": 9, "step": 1})
        attrs = {"unit_of_measurement": "kW"}
        power_id = "sensor.ev_power"
        try:
            if invalid_clock == "backwards_implicit":
                hass.states.async_set(power_id, 8, attrs, timestamp=(start + timedelta(seconds=5)).timestamp())
                previous = hass.states.get(power_id)
                hass.states.async_set(power_id, 8, attrs, timestamp=now.timestamp())
                assert hass.states.get(power_id) is previous
            else:
                hass.states.async_set(power_id, 8, {**attrs, "sampled_at_utc": "invalid"}, timestamp=now.timestamp())
            assert executor.note_ev_grid_power_report(hass.states.get(power_id))
            assert reservation["load_kw"] == 8 and reservation["observed_activity_at"] == start.isoformat()
            assert "confirmed_power_at" not in reservation
            assert "observed_load_at" not in reservation
            if invalid_clock == "future_limit_without_history":
                assert "pending_power_limit" not in reservation
                assert reservation[EV_RETIRED_CAP_KW] == 3
                reservation["pending_power_limit"] = pending

            now += timedelta(seconds=1)
            hass.states.async_set(
                power_id, 3, {**attrs, "sampled_at_utc": start.isoformat()}, timestamp=now.timestamp(),
            )
            executor.note_ev_grid_power_report(hass.states.get(power_id))
            assert reservation["load_kw"] == 8 and reservation["pending_power_limit"] == pending

            now += timedelta(seconds=1)
            hass.states.async_set(power_id, 3, {**attrs, "sampled_at_utc": now.isoformat()}, timestamp=now.timestamp())
            executor.note_ev_grid_power_report(hass.states.get(power_id))
            assert reservation["load_kw"] == 3 and "pending_power_limit" not in reservation
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("entity_id", ["switch.charger", "binary_sensor.charging"])
def test_external_ev_stop_signals_require_a_fresh_source_sample_after_rejection(tmp_path, monkeypatch, entity_id):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = _devices(hass)
        clock = start = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
        hass.states.async_set("binary_sensor.charging", "off", timestamp=start.timestamp())
        store = PlannerStore(hass, "runtime")
        executor = _executor(hass, store)
        reservations = executor._ev_grid_reservations()
        reservations["runtime"] = {
            "load_kw": 7, "reserved_at": start.isoformat(), EV_RESERVATION_EXTERNAL_BASELINE: True,
            "external_topology": {CONF_EV_CHARGER: "switch.charger", CONF_EV_CHARGING: "binary_sensor.charging"},
        }
        rejected = (start + timedelta(seconds=2)).isoformat()
        try:
            hass.states.async_set("binary_sensor.connected", "on", timestamp=clock.timestamp())
            wrong = hass.states.get("binary_sensor.connected")
            assert not executor.note_external_ev_signal_clock(wrong.entity_id, wrong, None)
            assert not executor.note_external_ev_signal_clock(entity_id, None, None)
            hass.states.async_set(entity_id, "on", {"sampled_at_utc": rejected}, timestamp=clock.timestamp())
            assert not executor.note_external_ev_signal_clock(entity_id, hass.states.get(entity_id), None, clock)
            hass.states.async_set(entity_id, "off", {"sampled_at_utc": rejected}, timestamp=clock.timestamp())
            assert not executor.release_stopped_external_ev_grid_capacity()
            clock += timedelta(seconds=3)
            hass.states.async_set(entity_id, "off", {"sampled_at_utc": rejected, "name": "Charger"},
                                  timestamp=clock.timestamp())
            assert not executor.release_stopped_external_ev_grid_capacity()
            clock += timedelta(seconds=1)
            hass.states.async_set(entity_id, "off", {"sampled_at_utc": clock.isoformat()}, timestamp=clock.timestamp())
            assert executor.release_stopped_external_ev_grid_capacity()
            assert not reservations and not calls
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("activation", "handoff"), [
    ("switch", False), ("charging", False), ("connected", False), ("meter", False), ("meter_report", False),
    ("switch_report", False),
    ("switch", True), ("charging", True), ("connected", True), ("cap", True), ("meter", True), ("retired", True),
])
def test_external_ev_watch_adopts_cap_when_original_charger_activates(tmp_path, monkeypatch, activation, handoff):
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
            "switch": "on" if activation in ("connected", "cap") else "off",
            "charging": "on" if activation in ("connected", "cap") else "unknown",
            "connected": "off" if activation == "connected" else "on",
        }
        for field, entity_id in evidence_ids.items():
            hass.states.async_set(entity_id, initial[field])
        if activation == "switch_report":
            source = now + timedelta(seconds=1)
            signal_attrs = {"sampled_at_utc": source.isoformat()}
            hass.states.async_set(evidence_ids["switch"], "on", signal_attrs, timestamp=source.timestamp())
            hass.states.async_set(evidence_ids["charging"], "off", timestamp=source.timestamp())
        power_id = "sensor.original_power"
        power_attrs = {"unit_of_measurement": "kW"}
        if activation == "meter_report":
            hass.states.async_set(power_id, 2, power_attrs, timestamp=(now - timedelta(minutes=11)).timestamp())
        retained = None
        if handoff:
            coordinator.executor.retain_ev_signal_clock_handoff()
            retained = hass.data[DOMAIN][EV_SIGNAL_CLOCK_HANDOFFS][entry.entry_id]
        else:
            coordinator.async_start_listeners()
        cap_id = "number.original_cap"
        cap_attrs = {"unit_of_measurement": "kW", "min": 1, "max": 10, "step": 1}
        try:
            if activation == "switch_report":
                now = source + timedelta(seconds=1)
                initial_signal = hass.states.get(evidence_ids["switch"])
                hass.states.async_set(evidence_ids["switch"], "on", signal_attrs, timestamp=now.timestamp())
                assert hass.states.get(evidence_ids["switch"]) is initial_signal
                await hass.async_block_till_done()
                assert store.data["ev_grid_reservation"]["observed_activity_at"] == source.isoformat()
                assert writes == [3] and not requests
                return
            if activation == "retired":
                now += timedelta(seconds=1)
                hass.states.async_set(evidence_ids["charging"], "off", timestamp=now.timestamp())
                hass.states.async_set(power_id, 0, power_attrs, timestamp=now.timestamp())
                assert coordinator.executor.release_stopped_external_ev_grid_capacity()
                now += timedelta(seconds=1)
                hass.states.async_set(evidence_ids["switch"], "on", timestamp=now.timestamp())
                hass.states.async_set(power_id, 8, power_attrs, timestamp=now.timestamp())
                assert entry.entry_id not in reservations
                return
            now += timedelta(seconds=1)
            hass.states.async_set(cap_id, 3 if activation == "cap" else 7, cap_attrs)
            if activation in evidence_ids:
                hass.states.async_set(evidence_ids[activation], initial[activation])
            hass.states.async_set("switch.replacement", "on")
            await hass.async_block_till_done()
            assert reservations[entry.entry_id]["load_kw"] == 3
            assert ev_grid_headroom_reason(reservations, "another_charger", context, 6, 10) is None
            assert not requests and not writes

            now += timedelta(seconds=1)
            if activation == "cap":
                hass.states.async_set(cap_id, 7, cap_attrs)
            elif activation in ("meter", "meter_report"):
                previous = hass.states.get(power_id)
                hass.states.async_set(power_id, 2, power_attrs, timestamp=now.timestamp())
                if activation == "meter_report":
                    assert hass.states.get(power_id) is previous
            else:
                hass.states.async_set(evidence_ids[activation], "on")
            await hass.async_block_till_done()
            assert reservations[entry.entry_id]["load_kw"] == 7
            assert ev_grid_headroom_reason(
                reservations, "another_charger", context, 6, 10,
            ) == "multi_ev_grid_import_limit_exceeded"
            if handoff:
                assert not requests and not writes
                hass.states.async_set(cap_id, "unavailable")
                previous = PlannerStore(hass, entry.entry_id)
                await previous.async_load()
                assert _rehydrate_ev_grid_reservation(hass, entry, previous.data)
                replacement = EnergyPlannerCoordinator(hass, entry, previous)
                await replacement.executor.async_persist_ev_grid_reservation()
                assert previous.data["ev_grid_reservation"]["load_kw"] == 7
                assert not previous.data["ownership"]
                return
            assert requests == ["ev_capacity_feedback"]
            assert writes == [7]
            now += timedelta(seconds=1)
            if activation in evidence_ids:
                hass.states.async_set(evidence_ids[activation], "on")
            await hass.async_block_till_done()
            assert requests == ["ev_capacity_feedback"] and writes == [7]
            assert not store.data["ownership"]
        finally:
            if retained is not None:
                retained.close()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize(("initial_power", "feedback"), [
    (3, "transient_draw"), (7, "transient_draw"), (3, "failed_drain"), (7, "replayed_meter"), (7, "replayed_switch"),
    (7, "resumed_during_setup"), (7, "resumed_after_setup"),
    (7, "unverified_below_reserve"), (7, "unverified_after_setup"),
])
def test_entry_accounts_for_external_ev_load_before_first_refresh(tmp_path, monkeypatch, initial_power, feedback):
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
        control_id = "switch.original"
        charging_id = "binary_sensor.original_charging"
        now = dt_util.utcnow()
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        attrs = {"unit_of_measurement": "kW"}
        previous = PlannerStore(hass, entry.entry_id)
        await previous.async_save_ev_grid_reservation({
            "active": True, "external_baseline": True, "load_kw": 3, "observed_load_kw": 3,
            "limit_kw": 10, "reserved_at": dt_util.utcnow().isoformat(),
            "external_topology": {
                CONF_EV_CHARGER: control_id, CONF_EV_CHARGING: charging_id, "ev_power_entity": power_id,
            },
        })
        hass.states.async_set(control_id, "on", timestamp=now.timestamp())
        hass.states.async_set(charging_id, "off", timestamp=now.timestamp())
        hass.states.async_set(power_id, initial_power, attrs, timestamp=now.timestamp())
        entered = asyncio.Event()
        resume = asyncio.Event()
        reject_write = feedback == "failed_drain"
        save_reservation = PlannerStore.async_save_ev_grid_reservation

        async def fail_writes_during_setup(store, reservation):
            if reject_write and reservation.get("load_kw") == 8:
                raise OSError("simulated reservation Store failure")
            await save_reservation(store, reservation)

        async def first_refresh(_coordinator):
            entered.set()
            await resume.wait()
            if reject_write:
                raise ConfigEntryNotReady("Superseded first refresh") from InitialPlanSuperseded()
            if feedback == "failed_drain":
                stored = PlannerStore(hass, entry.entry_id)
                await stored.async_load()
                assert stored.data["ev_grid_reservation"]["load_kw"] == 8

        async def skip_platform_setup(_entry, _platforms):
            pass

        async def skip_requested_refresh(_coordinator):
            pass

        monkeypatch.setattr(EnergyPlannerCoordinator, "async_config_entry_first_refresh", first_refresh)
        monkeypatch.setattr(EnergyPlannerCoordinator, "async_request_refresh", skip_requested_refresh)
        if reject_write:
            monkeypatch.setattr(PlannerStore, "async_save_ev_grid_reservation", fail_writes_during_setup)
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

            def set_state(entity_id, value, attributes=None):
                hass.states.async_set(entity_id, value, attributes, timestamp=now.timestamp())

            def resume_then_stop():
                nonlocal now
                now += timedelta(seconds=1)
                set_state(power_id, 0, attrs)
                now += timedelta(seconds=1)
                set_state(charging_id, "on")
                now += timedelta(seconds=1)
                set_state(control_id, "off")
                set_state(charging_id, "off")

            def unverified_draw_then_stale_stop():
                nonlocal now
                now += timedelta(seconds=1)
                receipt = now
                future = now + timedelta(seconds=1)
                if feedback == "unverified_after_setup":
                    hass.states.async_set(power_id, 6, attrs, timestamp=future.timestamp())
                    set_state(power_id, 6, attrs)
                else:
                    set_state(power_id, 6, {**attrs, "sampled_at_utc": future.isoformat()})
                set_state(power_id, "unavailable")
                now += timedelta(milliseconds=500)
                set_state(control_id, "off")
                set_state(power_id, 0, {**attrs, "sampled_at_utc": (now - timedelta(seconds=1)).isoformat()})
                return receipt.isoformat()

            unverified_receipt = None
            if feedback in {"transient_draw", "failed_drain"}:
                now += timedelta(seconds=1)
                set_state(power_id, 8, attrs)
                set_state(power_id, "unavailable")
            elif feedback == "resumed_during_setup":
                resume_then_stop()
            elif feedback == "unverified_below_reserve":
                unverified_receipt = unverified_draw_then_stale_stop()
            elif feedback.startswith("replayed"):
                now += timedelta(seconds=1)
                sample = (now + timedelta(seconds=1)).isoformat()
                entity_id = power_id if feedback == "replayed_meter" else control_id
                value = 0 if entity_id == power_id else "off"
                attributes = {**(attrs if entity_id == power_id else {}), "sampled_at_utc": sample}
                set_state(entity_id, value, attributes)
                now += timedelta(seconds=2)
                set_state(entity_id, value, {**attributes, "name": "replayed"})
                if entity_id == power_id:
                    set_state(control_id, "off")
                else:
                    set_state(power_id, 0, attrs)
            resume.set()
            if reject_write:
                with pytest.raises(ConfigEntryNotReady, match="Superseded first refresh"):
                    await task
                assert not hasattr(entry, "runtime_data")
                await persisted.async_load()
                assert persisted.data["ev_grid_reservation"]["load_kw"] == initial_power
                reject_write = False
                task = asyncio.create_task(async_setup_entry(hass, entry))
            assert await task
            if feedback == "resumed_after_setup":
                resume_then_stop()
            elif feedback == "unverified_after_setup":
                unverified_receipt = unverified_draw_then_stale_stop()
            expected = 8 if feedback in {"transient_draw", "failed_drain"} else initial_power
            await hass.async_block_till_done()
            assert reservations[entry.entry_id]["load_kw"] == expected
            persisted = PlannerStore(hass, entry.entry_id)
            await persisted.async_load()
            assert persisted.data["ev_grid_reservation"]["load_kw"] == expected
            if unverified_receipt is not None:
                assert persisted.data["ev_grid_reservation"]["observed_activity_at"] == unverified_receipt
            assert not persisted.data["ownership"]
            if feedback not in {"transient_draw", "failed_drain"}:
                now += timedelta(seconds=1)
                set_state(control_id, "off", {"sampled_at_utc": now.isoformat()})
                set_state(power_id, 0, {**attrs, "sampled_at_utc": now.isoformat()})
                await hass.async_block_till_done()
                assert entry.entry_id not in reservations
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
