"""Shared charger identity, guest sessions, swap races and per-vehicle calibration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import State

from custom_components.ha_energy_planner.config_flow import ConfigFlow, OptionsFlow
from custom_components.ha_energy_planner.const import (
    CONF_DEFAULT_READY_BY,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_EV_SOC_PER_KWH,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator, _configured_entity_ids
from custom_components.ha_energy_planner.entry_data import combined_entry_data
from custom_components.ha_energy_planner.ev_adapter import EVSmartChargingAdapter
from custom_components.ha_energy_planner.executor import Executor
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint
from custom_components.ha_energy_planner.sensor import ActiveVehicleSensor
from custom_components.ha_energy_planner.storage import PlannerStore
from custom_components.ha_energy_planner.subentry_migration import async_migrate_subentries_to_entry_data
from custom_components.ha_energy_planner.training import HistoryTraining, training_request
from custom_components.ha_energy_planner.vehicles import (
    AUTO,
    HOME,
    MANUAL,
    PORT,
    VEHICLE,
    VEHICLES,
    VehicleCalibration,
    VehicleSession,
    connection,
    home_state,
    identify,
    valid_soc,
    vehicle_entity_ids,
)


def profile(name: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        PORT: f"sensor.{name.lower()}_port",
        HOME: f"device_tracker.{name.lower()}",
        CONF_EV_SOC: f"sensor.{name.lower()}_soc",
        CONF_EV_SMART_CHARGING_TARGET_SOC: f"sensor.{name.lower()}_target",
        CONF_DEFAULT_READY_BY: "07:00",
        CONF_EV_CHARGE_RATE_KW: 7,
        CONF_EV_SOC_PER_KWH: 2,
        **changes,
    }


def setup() -> tuple[Any, dict[str, Any]]:
    values = {
        "binary_sensor.plug": "on",
        "binary_sensor.charging": "off",
        "switch.charger": "off",
        "sensor.a_port": "CONNECTED",
        "device_tracker.a": "home",
        "sensor.a_soc": "30",
        "sensor.a_target": "80",
        "sensor.b_port": "DISCONNECTED",
        "device_tracker.b": "home",
        "sensor.b_soc": "65",
        "sensor.b_target": "90",
    }
    states = SimpleNamespace(values=values)
    states.get = lambda entity: State(entity, values[entity]) if entity in values else None
    hass = SimpleNamespace(states=states, data={}, services=SimpleNamespace(async_call=AsyncMock()))
    data = {
        CONF_EV_CONNECTED: "binary_sensor.plug",
        CONF_EV_CHARGING: "binary_sensor.charging",
        CONF_EV_CHARGER: "switch.charger",
        VEHICLES: [profile("A"), profile("B")],
    }
    return hass, data


@pytest.mark.parametrize(("value", "expected"), [("CONNECTED", True), ("off", False), ("unavailable", None)])
def test_connection_normalization(value: str, expected: bool | None) -> None:
    assert connection(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"), [("Home", True), ("not_home", False), ("Office", False), ("unknown", None), ("on", True)]
)
def test_home_normalization(value: str, expected: bool | None) -> None:
    assert home_state(value) is expected


@pytest.mark.parametrize(
    ("updates", "selection", "vehicle", "reason"),
    [
        ({}, AUTO, "A", "detected_automatically"),
        ({"binary_sensor.plug": "off"}, AUTO, None, "unplugged"),
        ({"binary_sensor.plug": "unknown"}, AUTO, None, "charger_connection_unknown"),
        ({}, MANUAL, None, "manual"),
        ({}, "B", "B", "selected_manually"),
        ({}, "removed", None, "vehicle_removed"),
        ({"sensor.b_port": "CONNECTED"}, AUTO, None, "multiple_vehicles_connected"),
        ({"sensor.b_port": "CONNECTED", "device_tracker.b": "not_home"}, AUTO, "A", "detected_automatically"),
        ({"sensor.b_port": "unknown"}, AUTO, None, "vehicle_evidence_unknown"),
        ({"sensor.a_port": "DISCONNECTED"}, AUTO, None, "waiting_for_vehicle"),
        ({"device_tracker.a": "unknown"}, AUTO, None, "vehicle_evidence_unknown"),
    ],
)
def test_identification(updates: dict, selection: str, vehicle: str | None, reason: str) -> None:
    hass, data = setup()
    hass.states.values.update(updates)
    chosen, actual = identify(hass, data, selection)
    assert (chosen["id"] if chosen else None, actual) == (vehicle, reason)


@pytest.mark.parametrize("value", ["unavailable", "nan", "inf", "-1", "101"])
def test_invalid_target_never_uses_fallback(value: str) -> None:
    hass, data = setup()
    hass.states.values["sensor.a_target"] = value
    session = VehicleSession()
    session.update(hass, data)
    assert not session.allowed
    assert session.reason == "vehicle_soc_or_target_unavailable"
    assert CONF_EV_SOC not in session.resolve(data)
    assert not valid_soc(hass, None)


def test_auto_swap_and_manual_guest_reset_with_reload() -> None:
    hass, data = setup()
    session = VehicleSession()
    assert session.update(hass, data)
    assert not session.update(hass, data)
    assert session.resolve(data)[CONF_EV_SOC] == "sensor.a_soc"
    first_generation = session.generation
    session.selection = MANUAL
    session.update(hass, data)
    restored = VehicleSession(session.snapshot())
    restored.update(hass, data)
    assert restored.selection == MANUAL and not restored.allowed
    hass.states.values["binary_sensor.plug"] = "off"
    restored.update(hass, data)
    assert restored.selection == AUTO
    # Charger can update before old car disconnect telemetry arrives.
    hass.states.values.update({"binary_sensor.plug": "on", "sensor.b_port": "CONNECTED"})
    restored.update(hass, data)
    assert restored.reason == "multiple_vehicles_connected"
    hass.states.values["sensor.a_port"] = "DISCONNECTED"
    restored.update(hass, data)
    assert restored.profile["id"] == "B"
    assert restored.resolve(data)[CONF_EV_SMART_CHARGING_TARGET_SOC] == "sensor.b_target"
    assert restored.generation > first_generation
    assert restored.options(DEFAULT_OPTIONS)[CONF_DEFAULT_READY_BY] == "07:00"
    assert VehicleSession().options(DEFAULT_OPTIONS) == DEFAULT_OPTIONS


def test_manual_can_be_selected_before_guest_plugs_in() -> None:
    hass, data = setup()
    hass.states.values["binary_sensor.plug"] = "off"
    session = VehicleSession()
    session.selection = MANUAL
    session.update(hass, data)
    assert session.selection == MANUAL
    hass.states.values["binary_sensor.plug"] = "on"
    session.update(hass, data)
    assert session.selection == MANUAL
    hass.states.values["binary_sensor.plug"] = "off"
    session.update(hass, data)
    assert session.selection == AUTO


def make_coordinator(hass: Any, data: dict) -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = hass
    coordinator.entry = SimpleNamespace(data=data, options={}, subentries={}, entry_id="entry", title="House")
    coordinator.vehicle_session = VehicleSession()
    coordinator.vehicle_calibration = VehicleCalibration()
    coordinator._vehicle_ownership_key = None
    coordinator._refresh_generation = 0
    coordinator._ev_auto_start_compensation_generation = 0
    coordinator._ev_auto_start_retry_cancel = None
    coordinator._command_lock = asyncio.Lock()
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator.store = PlannerStore.__new__(PlannerStore)
    coordinator.store.data = {}
    coordinator.store._saved_generation = 0
    coordinator.store._mutation_generation = 0
    coordinator.store._async_save = AsyncMock()
    coordinator.executor = Executor(coordinator.store, hass=hass, entry_data=data, options=DEFAULT_OPTIONS)
    coordinator.executor.ev_command_guard = coordinator._ev_command_guard
    coordinator.executor.ev_restore_guard = coordinator._ev_restore_allowed
    coordinator.async_update_listeners = Mock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator._mark_forced_refresh = Mock()
    return coordinator


def test_command_token_cannot_cross_swap_or_target_change() -> None:
    hass, data = setup()
    coordinator = make_coordinator(hass, data)
    guard = coordinator._ev_command_guard()
    assert guard()
    hass.states.values["sensor.a_target"] = "20"
    assert not guard()
    fresh_guard = coordinator._ev_command_guard()
    assert fresh_guard()
    hass.states.values["binary_sensor.plug"] = "off"
    assert not fresh_guard()
    hass.states.values.update(
        {"binary_sensor.plug": "on", "sensor.a_port": "DISCONNECTED", "sensor.b_port": "CONNECTED"}
    )
    assert not fresh_guard()
    assert coordinator.entry_data[CONF_EV_SOC] == "sensor.b_soc"
    assert coordinator.planner_options[CONF_EV_CHARGE_RATE_KW] == 7


def test_manual_releases_ownership_and_invalidates_inflight_commands() -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.store.data["ownership"] = {
            "ev_smart_charging_state": {"switch.charger": "off"},
            "enphase_profile": "AI",
        }
        guard = coordinator._ev_command_guard()
        await coordinator.async_select_vehicle(MANUAL)
        assert not guard()
        assert coordinator.store.data["ownership"] == {"enphase_profile": "AI"}
        assert coordinator.store.data["ev_vehicle_session"]["selection"] == MANUAL
        assert not hass.services.async_call.called
        await coordinator._async_reconcile_vehicle_ownership()
        result = await coordinator.executor.async_manual_ev_charging(True, None)
        assert not result.applied and result.reason == "ev_vehicle_policy_withheld"
        with pytest.raises(ValueError, match="Unknown vehicle"):
            await coordinator.async_select_vehicle("missing")

    asyncio.run(run())


def test_adapter_cannot_stop_or_write_helpers_after_identity_is_lost() -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        adapter = EVSmartChargingAdapter(
            hass,
            data,
            command_guard=coordinator._ev_command_guard(),
            confirmation_timeout_seconds=0,
            confirmation_retries=0,
        )
        hass.states.values["sensor.b_port"] = "CONNECTED"
        result = await adapter._async_call_control("switch.charger", turn_on=False, force=True)
        assert result.reason == "ev_vehicle_session_changed"
        assert not await adapter._async_set_entity_value("number.target", 80)
        assert not hass.services.async_call.called

    asyncio.run(run())


def test_all_vehicles_listened_to_and_production_identity_stays_stable() -> None:
    hass, data = setup()
    coordinator = make_coordinator(hass, data)
    assert vehicle_entity_ids(data) <= set(_configured_entity_ids(coordinator.entry_data))
    expected = production_evidence_fingerprint(data, coordinator.options)
    assert production_evidence_fingerprint(coordinator.entry_data, coordinator.planner_options) == expected
    coordinator.vehicle_session.selection = "B"
    assert production_evidence_fingerprint(coordinator.entry_data, coordinator.planner_options) == expected


def test_profile_data_migrates_to_entry_without_changing_identity() -> None:
    hass, data = setup()
    subentry = SimpleNamespace(data=profile("A"), subentry_id="a", subentry_type=VEHICLE, title="A")
    entry = SimpleNamespace(data={CONF_EV_SOC: "sensor.old", "ev_vehicle_mode": True}, subentries={"a": subentry})
    combined = combined_entry_data(entry)
    assert CONF_EV_SOC not in combined
    assert combined[VEHICLES][0]["id"] == "a"
    hass.config_entries = SimpleNamespace(
        async_update_entry=lambda entry, **kwargs: setattr(entry, "data", kwargs["data"]),
        async_remove_subentry=lambda entry, subentry_id: entry.subentries.pop(subentry_id),
    )
    assert async_migrate_subentries_to_entry_data(hass, entry)
    assert entry.subentries == {}
    assert combined_entry_data(entry) == combined
    assert not async_migrate_subentries_to_entry_data(hass, entry)
    # A retry after data persistence but before subentry removal must not duplicate profiles.
    entry.subentries = {"a": subentry}
    assert combined_entry_data(entry) == combined
    assert len(entry.data[VEHICLES]) == 1
    assert async_migrate_subentries_to_entry_data(hass, entry)
    assert len(entry.data[VEHICLES]) == 1
    entry.data[VEHICLES] = []
    assert combined_entry_data(entry)[VEHICLES] == []


def test_vehicle_calibration_keeps_models_separate_and_drops_uncertain_intervals() -> None:
    hass, data = setup()
    session = VehicleSession()
    learner = VehicleCalibration()
    now = datetime(2026, 9, 12, tzinfo=UTC)
    session.update(hass, data)
    assert learner.observe(hass, session, data, {}, now) is None
    hass.states.values["binary_sensor.charging"] = "on"
    assert learner.observe(hass, session, data, {}, now) is None
    assert learner.observe(hass, session, data, {}, now + timedelta(minutes=10)) is None
    hass.states.values.update({"binary_sensor.charging": "off", "sensor.a_soc": "44"})
    model = learner.observe(hass, session, data, {}, now + timedelta(hours=1))
    assert model["status"] == "ready" and model["soc_per_kwh"] == 1.8
    assert model["soc_entity_id"] == "sensor.a_soc"
    hass.states.values["binary_sensor.charging"] = "on"
    learner.observe(hass, session, data, model, now + timedelta(hours=2))
    hass.states.values.update({"binary_sensor.charging": "off", "sensor.a_soc": "58"})
    model2 = learner.observe(hass, session, data, model, now + timedelta(hours=3))
    assert model2["sample_count"] == 2
    hass.states.values["binary_sensor.charging"] = "on"
    learner.observe(hass, session, data, model2, now + timedelta(hours=4))
    session.selection = MANUAL
    session.update(hass, data)
    assert learner.observe(hass, session, data, model2, now + timedelta(hours=5)) is None
    assert learner.pending is None
    session.selection = "B"
    session.update(hass, data)
    learner.observe(hass, session, data, model2, now + timedelta(hours=6))
    hass.states.values.update({"binary_sensor.charging": "off", "sensor.b_soc": "72"})
    model_b = learner.observe(hass, session, data, model2, now + timedelta(hours=7))
    assert model_b["soc_entity_id"] == "sensor.b_soc" and model_b["sample_count"] == 1


def test_profiles_skip_unattributed_recorder_training(monkeypatch: Any) -> None:
    async def run() -> None:
        hass, data = setup()
        session = VehicleSession()
        session.update(hass, data)
        data = session.resolve(data)
        store = {"ev_vehicle_calibrations": {"A": {"soc_per_kwh": 1.8}}}
        request = training_request(data, DEFAULT_OPTIONS, store, "UTC")
        assert request.ev_model == {"soc_per_kwh": 1.8}
        monkeypatch.setattr(
            "custom_components.ha_energy_planner.training.async_update_builtin_load_forecast",
            AsyncMock(return_value=({}, False, "load_ready")),
        )
        recorder = AsyncMock()
        monkeypatch.setattr("custom_components.ha_energy_planner.training.async_update_ev_charge_calibration", recorder)
        result = await HistoryTraining(hass, "entry", AsyncMock())._train(request)
        assert result.ev_model == request.ev_model and not result.ev_changed
        assert not recorder.called

    asyncio.run(run())


def test_selection_and_active_vehicle_entities(monkeypatch: Any) -> None:
    async def run() -> None:
        from custom_components.ha_energy_planner import select as select_module
        from custom_components.ha_energy_planner import sensor as sensor_module

        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.entry.runtime_data = coordinator
        coordinator._update_vehicle_session()
        monkeypatch.setattr(
            "custom_components.ha_energy_planner.entity.CoordinatorEntity.__init__",
            lambda self, coordinator: setattr(self, "coordinator", coordinator),
        )
        added = []
        await select_module.async_setup_entry(hass, coordinator.entry, added.extend)
        selector = added[0]
        assert selector.options == [AUTO, "A", "B", MANUAL]
        assert selector.current_option == AUTO
        assert selector.extra_state_attributes["active_vehicle"] == "A"
        await selector.async_select_option("B")
        assert selector.current_option == "B"
        active = ActiveVehicleSensor(coordinator, "active_ev_vehicle")
        assert active.native_value == "B"
        assert active.extra_state_attributes["vehicle_inputs_ready"]
        await selector.async_select_option(MANUAL)
        assert active.native_value is None
        monkeypatch.setattr(sensor_module, "SENSORS", [])
        await sensor_module.async_setup_entry(hass, coordinator.entry, added.extend)
        assert isinstance(added[-1], ActiveVehicleSensor)
        coordinator.entry.data = {}
        await select_module.async_setup_entry(hass, coordinator.entry, added.extend)

    asyncio.run(run())


def test_vehicle_flow_add_edit_and_validation(monkeypatch: Any) -> None:
    async def run() -> None:
        hass, data = setup()
        entry = SimpleNamespace(data={CONF_EV_CONNECTED: "binary_sensor.plug"}, options={}, subentries={})
        hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(side_effect=lambda entry, **kwargs: setattr(entry, "data", kwargs["data"])),
        )
        flow = OptionsFlow(entry)
        flow.hass = hass
        monkeypatch.setattr(flow, "async_show_form", lambda **kwargs: kwargs)
        monkeypatch.setattr(flow, "async_create_entry", lambda **kwargs: kwargs)
        assert ConfigFlow.async_get_supported_subentry_types(entry) == {}
        assert (await flow.async_step_init())["type"] == "menu"
        assert (await flow.async_step_edit_vehicle())["reason"] == "no_vehicles"
        assert (await flow.async_step_add_vehicle())["step_id"] == "add_vehicle"
        user_input = {k: v for k, v in profile("A").items() if k != "id"}
        result = await flow.async_step_add_vehicle(user_input)
        assert result["data"] == entry.options
        assert entry.data["ev_vehicle_mode"]
        existing = entry.data[VEHICLES][0]
        errors = (await flow.async_step_add_vehicle(user_input))["errors"]
        assert errors["name"] == "vehicle_name_in_use" and errors[PORT] == "vehicle_entity_in_use"
        assert (await flow.async_step_edit_vehicle())["step_id"] == "edit_vehicle"
        assert (await flow.async_step_edit_vehicle({"vehicle_id": existing["id"]}))["step_id"] == "vehicle"
        assert (await flow.async_step_vehicle(user_input))["data"] == entry.options
        assert entry.data[VEHICLES][0]["id"] == existing["id"]
        invalid = {
            **user_input, "name": "", PORT: "sensor.missing", HOME: "switch.charger", CONF_DEFAULT_READY_BY: "never",
        }
        entry.data.pop(CONF_EV_CONNECTED)
        errors = (await flow.async_step_vehicle(invalid))["errors"]
        assert errors["base"] == "vehicle_requires_charger_connection"
        assert errors[HOME] == "entity_not_found"
        entry.data[CONF_EV_CONNECTED] = "binary_sensor.plug"
        for rate in [0, 51, float("nan")]:
            errors = (await flow.async_step_vehicle({**user_input, CONF_EV_CHARGE_RATE_KW: rate}))["errors"]
            assert errors["base"] == "invalid_vehicle_settings"
        assert (await flow.async_step_remove_vehicle())["step_id"] == "remove_vehicle"
        assert (await flow.async_step_remove_vehicle({"vehicle_id": "missing"}))["reason"] == "vehicle_not_found"
        await flow.async_step_remove_vehicle({"vehicle_id": existing["id"]})
        assert entry.data[VEHICLES] == [] and entry.data["ev_vehicle_mode"]
        assert (await flow.async_step_vehicle(user_input))["reason"] == "vehicle_not_found"

    asyncio.run(run())


def test_vehicle_runtime_refresh_swap_and_restart(tmp_path: Any, monkeypatch: Any) -> None:
    """Exercise real HA states/storage through coordinator context and execution."""
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from custom_components.ha_energy_planner.const import DOMAIN
    from custom_components.ha_energy_planner.models import ActionAsset

    async def run() -> None:
        fake, data = setup()
        hass = HomeAssistant(str(tmp_path))
        for entity, value in fake.states.values.items():
            hass.states.async_set(entity, value)
        entry = ConfigEntry(
            domain=DOMAIN,
            title="Shared charger",
            data=data,
            options={**DEFAULT_OPTIONS, "ai_enabled": False},
            source="user",
            unique_id=None,
            version=5,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        store = PlannerStore(hass, entry.entry_id)
        coordinator = EnergyPlannerCoordinator(hass, entry, store)
        coordinator._schedule_debounced_refresh = Mock()
        try:
            first = await coordinator._async_update_data_locked(defer_execution=True)
            assert coordinator._last_decision_context.ev_vehicle_id == "A"
            assert coordinator._last_decision_context.current_ev_soc_percent == 30
            assert coordinator._last_decision_context.ev_target_soc_percent == 80
            assert coordinator._last_decision_context.ev_ready_by == "07:00"
            # Observe one complete attributed interval and persist a model.
            now = datetime.now(UTC)
            coordinator.vehicle_calibration.pending = ("A", now - timedelta(hours=1), 16.0)
            await coordinator._async_update_data_locked(defer_execution=True)
            assert store.data["ev_vehicle_calibrations"]["A"]["soc_per_kwh"] == 1.8
            coordinator.vehicle_session.selection = MANUAL
            manual_plan = await coordinator._async_update_data_locked(defer_execution=True)
            assert coordinator._last_decision_context.ev_policy_allowed is False
            assert all(a.asset != ActionAsset.EV for a in manual_plan.actions)
            # Manual mode survives loading the same real storage in a new runtime.
            fresh = PlannerStore(hass, entry.entry_id)
            await fresh.async_load()
            restored = VehicleSession(fresh.data["ev_vehicle_session"])
            restored.update(hass, data)
            assert restored.selection == MANUAL and not restored.allowed
            assert fresh.data["ev_vehicle_calibrations"]["A"]["soc_per_kwh"] == 1.8
            # A stale first-car plan must be rejected before dispatch.
            generation = coordinator._refresh_generation
            hass.states.async_set("binary_sensor.plug", "off")
            await coordinator._async_execute_plan_if_current(
                generation, first, coordinator._last_decision_context, coordinator.options
            )
            hass.states.async_set("sensor.a_port", "DISCONNECTED")
            hass.states.async_set("sensor.b_port", "CONNECTED")
            hass.states.async_set("binary_sensor.plug", "on")
            await coordinator._async_update_data_locked(defer_execution=True)
            assert coordinator._last_decision_context.ev_vehicle_id == "B"
            assert coordinator._last_decision_context.current_ev_soc_percent == 65
            assert coordinator._last_decision_context.ev_target_soc_percent == 90
            assert coordinator._training_request().ev_model == {}
            # Target loss withdraws EV policy, including the owned safety-stop path.
            hass.states.async_set("sensor.b_target", "unavailable")
            plan = await coordinator._async_update_data_locked(defer_execution=True)
            assert coordinator._last_decision_context.ev_policy_allowed is False
            await coordinator.executor.async_evaluate(plan, coordinator._last_decision_context)
            assert not any(a.asset == ActionAsset.EV for a in plan.actions)
        finally:
            coordinator.history_training.stop()
            if coordinator.history_training.task:
                await coordinator.history_training.task
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("previous", ["on", "unavailable", "unknown"])
def test_queued_unplug_resets_manual_even_after_replug(monkeypatch: Any, previous: str) -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.vehicle_session.selection = MANUAL
        coordinator._update_vehicle_session()
        callbacks = []
        tasks = []
        monkeypatch.setattr(
            "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
            lambda hass, ids, cb: callbacks.append(cb) or (lambda: None),
        )
        coordinator._schedule_next_boundary_refresh = Mock()
        coordinator._start_load_forecast_source_listener = Mock()
        coordinator._wake_startup_auto_recovery = Mock()
        coordinator._schedule_debounced_refresh = Mock()
        coordinator._unsub_listeners = []
        coordinator._async_create_listener_task = lambda coro: tasks.append(asyncio.create_task(coro))
        coordinator.async_start_listeners()
        callbacks[0](
            SimpleNamespace(
                data={
                    "entity_id": "binary_sensor.plug",
                    "old_state": State("binary_sensor.plug", previous),
                    "new_state": State("binary_sensor.plug", "off"),
                }
            )
        )
        await asyncio.gather(*tasks)
        assert coordinator.vehicle_session.selection == AUTO
        assert coordinator.store.data["ev_vehicle_session"]["selection"] == AUTO
        assert coordinator._schedule_debounced_refresh.called

    asyncio.run(run())


def test_ready_by_updates_selected_profile_only() -> None:
    from custom_components.ha_energy_planner import _async_update_listener, _entry_topology_signature

    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.entry.subentries = {}
        coordinator.entry.runtime_data = coordinator
        coordinator.entry_topology_signature = _entry_topology_signature(coordinator.entry)
        coordinator.async_handle_options_update = AsyncMock()
        coordinator.async_prepare_configuration_reload = AsyncMock()
        await coordinator._async_reconcile_vehicle_ownership()
        ownership_key = coordinator._vehicle_ownership_key
        guard = coordinator._ev_command_guard()
        fingerprint = production_evidence_fingerprint(coordinator.entry_data, coordinator.planner_options)

        def update_entry(entry: Any, *, data: dict) -> None:
            entry.data = data

        hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(side_effect=update_entry), async_reload=AsyncMock(),
        )
        await coordinator.async_set_ready_by("06:15")
        await _async_update_listener(hass, coordinator.entry)
        assert coordinator.entry.data[VEHICLES][0][CONF_DEFAULT_READY_BY] == "06:15"
        assert coordinator.entry.data[VEHICLES][1][CONF_DEFAULT_READY_BY] == "07:00"
        assert coordinator.planner_options[CONF_DEFAULT_READY_BY] == "06:15"
        assert coordinator.async_request_refresh.call_count == 1
        assert not coordinator.async_prepare_configuration_reload.called
        assert not hass.config_entries.async_reload.called
        assert coordinator._vehicle_ownership_key == ownership_key
        assert not guard()
        assert production_evidence_fingerprint(coordinator.entry_data, coordinator.planner_options) == fingerprint
        # Editing the profile through HA must take the same runtime update path.
        coordinator.entry.data[VEHICLES][0][CONF_DEFAULT_READY_BY] = "06:30"
        await _async_update_listener(hass, coordinator.entry)
        assert coordinator.async_request_refresh.call_count == 2
        assert not hass.config_entries.async_reload.called
        coordinator.vehicle_session.selection = MANUAL
        with pytest.raises(ValueError, match="tracked vehicle"):
            await coordinator.async_set_ready_by("06:00")
        # Physical mappings still require the original reload handoff.
        coordinator.entry.data[VEHICLES][0][PORT] = "sensor.replacement_port"
        await _async_update_listener(hass, coordinator.entry)
        assert coordinator.async_prepare_configuration_reload.call_count == 1
        assert hass.config_entries.async_reload.call_count == 1

    asyncio.run(run())


def test_charger_retry_and_rollback_stop_at_a_vehicle_boundary() -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)

        async def actuate(*args: Any, **kwargs: Any) -> None:
            # First start is sent for A. New evidence becomes ambiguous while
            # waiting for charging confirmation; no retry or rollback may stop B.
            hass.states.values["sensor.b_port"] = "CONNECTED"

        hass.services.async_call.side_effect = actuate
        adapter = EVSmartChargingAdapter(
            hass,
            data,
            command_guard=coordinator._ev_command_guard(),
            confirmation_timeout_seconds=0,
            confirmation_retries=2,
        )
        result = await adapter.async_set_charging(True)
        assert not result.applied
        assert hass.services.async_call.call_count == 1
        assert hass.services.async_call.call_args.args[1] == "turn_on"

    asyncio.run(run())


def test_vehicle_settings_hide_legacy_fields_and_require_charger_feedback() -> None:
    from custom_components.ha_energy_planner.config_flow import (
        SUBENTRY_EV,
        OptionsFlow,
        _validate_subentry_config,
    )

    hass, data = setup()
    entry = SimpleNamespace(data=data, options={}, subentries={}, entry_id="entry")
    schema = OptionsFlow(entry)._settings_schema()
    for section in schema.schema.values():
        fields = section.schema.schema
        names = {str(getattr(marker, "schema", marker)) for marker in fields}
        assert CONF_EV_SOC not in names
        assert CONF_EV_SMART_CHARGING_TARGET_SOC not in names
        assert CONF_DEFAULT_READY_BY not in names
    errors = _validate_subentry_config(hass, entry, {}, subentry_type=SUBENTRY_EV)
    assert CONF_EV_CONNECTED in errors and CONF_EV_CHARGING in errors
    data[VEHICLES][0][CONF_EV_CHARGE_RATE_KW] = 11
    session = VehicleSession()
    session.update(hass, data)
    assert session.options(DEFAULT_OPTIONS)[CONF_EV_CHARGE_RATE_KW] == 7


def test_connection_outage_preserves_manual_until_confirmed_unplug() -> None:
    hass, data = setup()
    session = VehicleSession({"selection": MANUAL, "was_plugged": True})
    hass.states.values["binary_sensor.plug"] = "unavailable"
    session.update(hass, data)
    assert session.selection == MANUAL and session.was_plugged and not session.allowed
    hass.states.values["binary_sensor.plug"] = "off"
    session.update(hass, data)
    assert session.selection == AUTO and not session.was_plugged


def test_shared_charger_configuration_accepts_feedback_without_legacy_car_sensors() -> None:
    from custom_components.ha_energy_planner.config_flow import SUBENTRY_EV, _validate_subentry_config

    hass, data = setup()
    entry = SimpleNamespace(data=data, options={}, subentries={}, entry_id="entry")
    assert not _validate_subentry_config(
        hass,
        entry,
        {
            CONF_EV_CONNECTED: data[CONF_EV_CONNECTED],
            CONF_EV_CHARGING: data[CONF_EV_CHARGING],
        },
        subentry_type=SUBENTRY_EV,
    )


def test_old_connected_car_is_not_reselected_before_new_car_telemetry_arrives() -> None:
    hass, data = setup()
    session = VehicleSession()
    session.update(hass, data)
    assert session.profile["id"] == "A"
    hass.states.values["binary_sensor.plug"] = "off"
    session.update(hass, data)
    hass.states.values["binary_sensor.plug"] = "on"
    session.update(hass, data)
    # A's stale CONNECTED and B's stale DISCONNECTED do not identify A.
    assert not session.allowed and session.reason == "waiting_for_vehicle_disconnect"
    restarted = VehicleSession(session.snapshot())
    restarted.update(hass, data)
    assert not restarted.allowed
    hass.states.values["sensor.a_port"] = "DISCONNECTED"
    restarted.update(hass, data)
    hass.states.values["sensor.b_port"] = "CONNECTED"
    restarted.update(hass, data)
    assert restarted.profile["id"] == "B"
    assert restarted.allowed


def test_same_car_replug_waits_for_a_port_disconnect_or_explicit_identity() -> None:
    hass, data = setup()
    session = VehicleSession()
    session.update(hass, data)
    hass.states.values["binary_sensor.plug"] = "off"
    session.update(hass, data)
    hass.states.values["binary_sensor.plug"] = "on"
    session.update(hass, data)
    assert not session.allowed
    session.selection = "A"
    session.update(hass, data)
    assert session.allowed
    session.selection = AUTO
    hass.states.values["sensor.a_port"] = "DISCONNECTED"
    session.update(hass, data)
    hass.states.values["sensor.a_port"] = "CONNECTED"
    session.update(hass, data)
    assert session.allowed


def test_startup_and_swap_cannot_restore_previous_vehicle_ownership() -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.store.data["ownership"] = {"ev_smart_charging_state": {"switch.charger": "on"}}
        assert not coordinator._ev_restore_allowed()
        await coordinator.executor.async_restore_safe_state("startup")
        assert all(call.args[0] == "persistent_notification" for call in hass.services.async_call.call_args_list)
        await coordinator._async_reconcile_vehicle_ownership()
        assert coordinator._ev_restore_allowed()
        hass.states.values.update({"sensor.a_port": "DISCONNECTED", "sensor.b_port": "CONNECTED"})
        assert not coordinator._ev_restore_allowed()
        await coordinator.executor.async_restore_safe_state("swap")
        assert all(call.args[0] == "persistent_notification" for call in hass.services.async_call.call_args_list)
        coordinator.entry.data = {}
        assert coordinator._ev_restore_allowed()

    asyncio.run(run())


def test_bmw_cardata_swap_fixture() -> None:
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / "fixtures/vehicles/bmw_cardata_swap.json").read_text())
    hass, data = setup()
    session = VehicleSession()
    for step in fixture["steps"]:
        hass.states.values.update(step["states"])
        session.update(hass, data)
        assert (session.profile["id"] if session.profile else None) == step["vehicle"]
        assert session.reason == step["reason"]


@pytest.mark.parametrize(
    "charging,connected,expected", [(True, True, 7), (None, True, 7), (False, True, 0), (True, False, 0)],
)
def test_unmanaged_charger_load_remains_in_household_plan(
    charging: bool | None, connected: bool, expected: float,
) -> None:
    from datetime import timedelta

    from custom_components.ha_energy_planner.constraints import _projected_grid_flows_kw
    from custom_components.ha_energy_planner.models import ActionAsset, DecisionContext, DecisionSlot, InputHealth
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    now = datetime(2026, 9, 12, tzinfo=UTC)
    context = DecisionContext(
        created_at=now, plan_id="guest", input_health=InputHealth.HEALTHY,
        current_battery_soc_percent=None, current_ev_soc_percent=None, occupancy_state="unknown",
        ev_policy_allowed=False, ev_charging=charging, ev_connected=connected,
        slots=[DecisionSlot(valid_at=now + timedelta(minutes=i * 5), import_price=0.2, export_price=0.05,
                            pv_forecast_kw=0, baseline_load_forecast_kw=2) for i in range(2)],
    )
    plan = DryRunPlanner({**DEFAULT_OPTIONS, "planner_enabled": True}).create_plan(context)
    assert all(slot.projected_ev_load_kw == expected for slot in context.slots)
    assert _projected_grid_flows_kw(context.slots[0])[0] == 2 + expected
    assert plan.estimated_daily_cost == round((2 + expected) * 0.2 / 6, 4)
    assert all(action.asset != ActionAsset.EV for action in plan.actions)


@pytest.mark.parametrize("rapid_replug", [False, True])
def test_unplug_releases_capacity_without_charger_commands(rapid_replug: bool) -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.executor.entry_id = "entry"
        await coordinator._async_reconcile_vehicle_ownership()
        reservations = coordinator.executor._ev_grid_reservations()
        reservations["entry"] = {"load_kw": 7, "limit_kw": 10}
        hass.states.values["binary_sensor.plug"] = "off"
        coordinator._update_vehicle_session()
        if rapid_replug:
            hass.states.values.update(
                {"binary_sensor.plug": "on", "sensor.a_port": "DISCONNECTED", "sensor.b_port": "CONNECTED"}
            )
        await coordinator._async_reconcile_vehicle_ownership()
        assert "entry" not in reservations
        assert coordinator.store.data["ev_grid_reservation"]["active"] is False
        assert not hass.services.async_call.called
        # Manual selection alone cannot prove an active guest load has stopped.
        reservations["entry"] = {"load_kw": 7, "limit_kw": 10}
        hass.states.values["binary_sensor.plug"] = "on"
        await coordinator.async_select_vehicle(MANUAL)
        assert reservations["entry"]["external_baseline"] is True

    asyncio.run(run())


def test_new_charger_can_be_configured_before_first_vehicle() -> None:
    from custom_components.ha_energy_planner.config_flow import SUBENTRY_EV, _validate_subentry_config

    hass, data = setup()
    entry = SimpleNamespace(data={}, options={}, subentries={}, entry_id="entry")
    charger = {key: data[key] for key in (CONF_EV_CONNECTED, CONF_EV_CHARGING, CONF_EV_CHARGER)}
    assert not _validate_subentry_config(hass, entry, charger, subentry_type=SUBENTRY_EV)
    # An incomplete legacy vehicle mapping still requires its target sensor.
    errors = _validate_subentry_config(hass, entry, {**charger, CONF_EV_SOC: "sensor.a_soc"}, subentry_type=SUBENTRY_EV)
    assert CONF_EV_SMART_CHARGING_TARGET_SOC in errors


def test_percent_suffixed_soc_calibrates_without_refresh_error() -> None:
    hass, data = setup()
    session = VehicleSession()
    learner = VehicleCalibration()
    now = datetime(2026, 9, 12, tzinfo=UTC)
    hass.states.values.update({"sensor.a_soc": "30%", "binary_sensor.charging": "on"})
    session.update(hass, data)
    assert session.allowed
    assert learner.observe(hass, session, data, {}, now) is None
    hass.states.values.update({"sensor.a_soc": "44 %", "binary_sensor.charging": "off"})
    session.update(hass, data)
    model = learner.observe(hass, session, data, {}, now + timedelta(hours=1))
    assert model["status"] == "ready"
    assert model["sample_count"] == 1


def test_charger_first_options_then_add_vehicle_flow(monkeypatch: Any) -> None:
    from custom_components.ha_energy_planner.config_flow import INPUT_STEP_EV, OptionsFlow

    async def run() -> None:
        hass, data = setup()
        entry = SimpleNamespace(data={}, options={}, subentries={}, entry_id="entry")

        def update_entry(entry: Any, *, data: dict) -> None:
            entry.data = data

        hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(side_effect=update_entry), async_entries=lambda *a: [],
        )
        options_flow = OptionsFlow(entry)
        options_flow.hass = hass
        monkeypatch.setattr(options_flow, "async_create_entry", lambda **kwargs: kwargs)
        monkeypatch.setattr(options_flow, "async_show_form", lambda **kwargs: kwargs)
        charger = {key: data[key] for key in (CONF_EV_CONNECTED, CONF_EV_CHARGING, CONF_EV_CHARGER)}
        result = await options_flow.async_step_settings({INPUT_STEP_EV: charger})
        assert "errors" not in result
        assert entry.data == charger
        vehicle_flow = OptionsFlow(entry)
        vehicle_flow.hass = hass
        monkeypatch.setattr(vehicle_flow, "async_create_entry", lambda **kwargs: kwargs)
        monkeypatch.setattr(vehicle_flow, "async_show_form", lambda **kwargs: kwargs)
        result = await vehicle_flow.async_step_add_vehicle({k: v for k, v in profile("A").items() if k != "id"})
        assert entry.data[VEHICLES][0]["name"] == "A"
        assert entry.data["ev_vehicle_mode"] is True
        assert not hass.services.async_call.called

    asyncio.run(run())


def test_vehicle_swap_clears_manual_override_before_context_is_built() -> None:
    from custom_components.ha_energy_planner.models import Override

    hass, data = setup()
    coordinator = make_coordinator(hass, data)
    coordinator._update_vehicle_session()
    coordinator.overrides = [
        Override(kind="manual_ev_charging", source="test", expires_at=None, reason="manual_start"),
        Override(kind="manual_hvac", source="test", expires_at=None, reason="manual"),
    ]
    hass.states.values.update({"sensor.a_port": "DISCONNECTED", "sensor.b_port": "CONNECTED"})
    # entry_data is resolved before DecisionContext.active_overrides is built.
    assert coordinator.entry_data["ev_vehicle_id"] == "B"
    assert [override.kind for override in coordinator.overrides] == ["manual_hvac"]


@pytest.mark.parametrize("interruption", ["off", "unavailable"])
def test_queued_charging_interruption_drops_calibration_interval(monkeypatch: Any, interruption: str) -> None:
    async def run() -> None:
        hass, data = setup()
        hass.states.values["binary_sensor.charging"] = "on"
        coordinator = make_coordinator(hass, data)
        coordinator._update_vehicle_session()
        await coordinator.store.async_save_vehicle_session(coordinator.vehicle_session.snapshot())
        now = datetime(2026, 9, 12, tzinfo=UTC)
        coordinator.vehicle_calibration.observe(hass, coordinator.vehicle_session, data, {}, now)
        callbacks = []
        monkeypatch.setattr(
            "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
            lambda hass, ids, cb: callbacks.append(cb) or (lambda: None),
        )
        coordinator._schedule_next_boundary_refresh = Mock()
        coordinator._start_load_forecast_source_listener = Mock()
        coordinator._wake_startup_auto_recovery = Mock()
        coordinator._schedule_debounced_refresh = Mock()
        coordinator._unsub_listeners = []
        coordinator.async_start_listeners()
        # HA has already received the resume; replay both queued events without
        # a planner refresh between them.
        for old, new in [("on", interruption), (interruption, "on")]:
            callbacks[0](SimpleNamespace(data={
                "entity_id": "binary_sensor.charging",
                "old_state": State("binary_sensor.charging", old),
                "new_state": State("binary_sensor.charging", new),
            }))
        assert coordinator.vehicle_calibration.pending is None
        coordinator.vehicle_calibration.observe(hass, coordinator.vehicle_session, data, {}, now + timedelta(hours=1))
        assert coordinator.vehicle_calibration.pending[1] == now + timedelta(hours=1)

    asyncio.run(run())


def test_port_disconnect_evidence_survives_restart_before_replan(monkeypatch: Any) -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator._update_vehicle_session()
        hass.states.values["binary_sensor.plug"] = "off"
        coordinator._update_vehicle_session()
        await coordinator.store.async_save_vehicle_session(coordinator.vehicle_session.snapshot())
        assert coordinator.store.data["ev_vehicle_session"]["blocked_vehicle_ids"] == ["A"]
        callbacks, tasks = [], []
        monkeypatch.setattr(
            "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
            lambda hass, ids, cb: callbacks.append(cb) or (lambda: None),
        )
        coordinator._schedule_next_boundary_refresh = Mock()
        coordinator._start_load_forecast_source_listener = Mock()
        coordinator._wake_startup_auto_recovery = Mock()
        coordinator._schedule_debounced_refresh = Mock()
        coordinator._unsub_listeners = []
        coordinator._async_create_listener_task = lambda coro: tasks.append(asyncio.create_task(coro))
        coordinator.async_start_listeners()
        hass.states.values["sensor.a_port"] = "DISCONNECTED"
        callbacks[0](SimpleNamespace(data={
            "entity_id": "sensor.a_port",
            "old_state": State("sensor.a_port", "CONNECTED"),
            "new_state": State("sensor.a_port", "DISCONNECTED"),
        }))
        await asyncio.gather(*tasks)
        assert coordinator.store.data["ev_vehicle_session"]["blocked_vehicle_ids"] == []
        # The same car plugs back in after reload, before the next planner tick.
        restored = VehicleSession(coordinator.store.data["ev_vehicle_session"])
        hass.states.values.update({"binary_sensor.plug": "on", "sensor.a_port": "CONNECTED"})
        restored.update(hass, data)
        assert restored.allowed and restored.profile["id"] == "A"

    asyncio.run(run())


def test_queued_session_save_preserves_a_newer_manual_selection() -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator._update_vehicle_session()
        pending_save = coordinator._async_save_vehicle_session()
        await coordinator.async_select_vehicle(MANUAL)
        await pending_save
        assert coordinator.store.data["ev_vehicle_session"]["selection"] == MANUAL

    asyncio.run(run())


@pytest.mark.parametrize("same_vehicle", [False, True])
def test_unplug_during_ownership_write_is_not_consumed_by_older_release(same_vehicle: bool) -> None:
    async def run() -> None:
        hass, data = setup()
        coordinator = make_coordinator(hass, data)
        coordinator.executor.entry_id = "entry"
        coordinator._update_vehicle_session()
        reservations = coordinator.executor._ev_grid_reservations()
        reservations["entry"] = {"load_kw": 7, "limit_kw": 10}
        entered, resume = asyncio.Event(), asyncio.Event()
        original_save = coordinator.store.async_save_ownership

        async def delayed_save(ownership: dict) -> None:
            entered.set()
            await resume.wait()
            await original_save(ownership)

        coordinator.store.async_save_ownership = delayed_save
        release = asyncio.create_task(coordinator._async_reconcile_vehicle_ownership())
        await entered.wait()
        hass.states.values.update({"binary_sensor.plug": "off", "sensor.a_port": "DISCONNECTED"})
        coordinator._update_vehicle_session()
        hass.states.values.update({
            "binary_sensor.plug": "on",
            "sensor.a_port": "CONNECTED" if same_vehicle else "DISCONNECTED",
            "sensor.b_port": "DISCONNECTED" if same_vehicle else "CONNECTED",
        })
        coordinator._update_vehicle_session()
        resume.set()
        await release
        await coordinator._async_reconcile_vehicle_ownership()
        assert "entry" not in reservations
        assert coordinator.store.data["ev_grid_reservation"]["active"] is False
        assert not hass.services.async_call.called

    asyncio.run(run())
