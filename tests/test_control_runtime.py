"""Compose real HA storage/services with actuator transactions and fresh runtimes."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import Context, CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import storage as ha_storage
from homeassistant.util import dt as dt_util

from custom_components.ha_energy_planner import _rehydrate_ev_grid_reservation, adapter_helpers
from custom_components.ha_energy_planner.config_flow import INPUT_STEP_CLIMATE, OptionsFlow
from custom_components.ha_energy_planner.const import (
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_CONNECTED,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator
from custom_components.ha_energy_planner.ev_adapter import EVChargerAdapter, EVCommandResult
from custom_components.ha_energy_planner.executor import Executor
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


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_resume_refreshes_during_home_assistant_debounce_cooldown(tmp_path, monkeypatch, refresh_fails):
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
            if refresh_fails and refreshes == 2:
                raise TimeoutError("planner input timeout")
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
            if refresh_fails:
                with pytest.raises(HomeAssistantError, match="Could not refresh climate planning"):
                    await coordinator.async_resume_climate_planning()
                assert not coordinator.last_update_success
            else:
                result = await coordinator.async_resume_climate_planning()
                assert result["reason"] == "device_control_paused"
            assert refreshes == 2
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_charge_now_requires_fresh_evidence_during_debounce_cooldown(tmp_path, monkeypatch, refresh_fails):
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

        async def refresh():
            nonlocal refreshes
            refreshes += 1
            if refresh_fails and refreshes == 2:
                raise TimeoutError("planner input timeout")
            coordinator._last_decision_context = context if refreshes == 1 else None
            return plan

        start = AsyncMock(return_value=SimpleNamespace(applied=False, reason="ev_grid_projection_unavailable"))
        monkeypatch.setattr(coordinator, "_async_update_data", refresh)
        monkeypatch.setattr(coordinator.executor, "async_manual_ev_charging", start)
        try:
            await coordinator.async_request_refresh()
            assert coordinator._last_decision_context is context
            if refresh_fails:
                with pytest.raises(HomeAssistantError, match="Could not refresh charging evidence"):
                    await coordinator.async_charge_now()
                start.assert_not_awaited()
                assert coordinator._last_decision_context is context
            else:
                result = await coordinator.async_charge_now()
                assert not result.applied
                assert start.call_args.args == (True, None)
            assert refreshes == (2 if refresh_fails else 3)
            assert coordinator.overrides == []
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
                preserve_main_state=main, preserve_zone_entity_id=None if main else entity,
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
