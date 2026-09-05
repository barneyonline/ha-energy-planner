"""Compose real HA storage/services with actuator transactions and fresh runtimes."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.helpers import storage as ha_storage
from homeassistant.util import dt as dt_util

from custom_components.ha_energy_planner import _rehydrate_ev_grid_reservation, adapter_helpers
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
                with pytest.raises(OSError, match="write was not confirmed"):
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
        executor.options.update({
            "ev_charge_rate_kw": 6.0, "ev_soc_per_kwh": 10.0, "ev_continuous_charging": True,
            "ev_earliest_start": "None", "ev_low_price_charging_enabled": False,
            "command_rate_limit_seconds": 0, "max_daily_ev_actions": 50,
        })
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
                plan = DryRunPlanner(executor.options).create_plan(context)
                actions = [action for action in plan.actions if action.asset == ActionAsset.EV]
                allocated = [slot for action in actions for slot in action.desired_state.get("allocated_slots", [])]
                offsets = [int((datetime.fromisoformat(slot["valid_at"]) - now).total_seconds() / 60)
                           for slot in allocated]
                assert offsets == step["offsets"], (fixture["name"], index)
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
                if failure == "disk":
                    patch.setattr(ha_storage, "write_utf8_file_atomic", disk_failure)
                else:
                    hass.state = CoreState.stopping
                with pytest.raises(OSError, match="write was not confirmed"):
                    await coordinator.async_operator_disarm_production_control()
            hass.state = CoreState.running
            assert calls[before:] == [
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
            with pytest.raises(OSError, match="write was not confirmed"):
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


def test_real_store_deferred_shutdown_write_is_not_acknowledged(tmp_path) -> None:
    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        store = PlannerStore(hass, "runtime")
        try:
            hass.state = CoreState.stopping
            with pytest.raises(OSError, match="write was not confirmed"):
                await store.async_save_ownership({"enphase_profile": "Original profile"})
            assert store._is_dirty
            # A later explicit retry must still write the same pending value.
            hass.state = CoreState.running
            await store.async_save_ownership({"enphase_profile": "Original profile"})
            assert not store._is_dirty
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
