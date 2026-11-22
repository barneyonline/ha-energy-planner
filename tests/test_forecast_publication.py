"""Committed forecast presentation across refresh and storage interleavings."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from test_coordinator import FakeEntry, FakeHass, FakeState, _coordinator_for_runtime_services, _plan

from custom_components.ha_energy_planner import coordinator as coordinator_module
from custom_components.ha_energy_planner import storage as storage_module
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_IMPORT_PRICE,
    CONF_DEFAULT_READY_BY,
    CONF_GRID_IMPORT_LIMIT_KW,
)
from custom_components.ha_energy_planner.coordinator import _PlanExecution
from custom_components.ha_energy_planner.inputs import InputManager
from custom_components.ha_energy_planner.models import (
    DecisionContext,
    DecisionSlot,
    InputHealth,
    OccupancyState,
    PlannerMode,
)
from custom_components.ha_energy_planner.plan_presentation import latest_forecast_snapshot, planning_status
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint
from custom_components.ha_energy_planner.sensor import SENSORS, PlannerSensor
from custom_components.ha_energy_planner.storage import PlannerStore


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("outcome", [
    "committed", "obsolete", "failed", "conflict", "tariff_changed", "tariff_type_changed", "mapping_changed",
    "options_changed",
])
def test_forecast_stays_aligned_during_refresh_persistence_and_listeners(
    monkeypatch: pytest.MonkeyPatch, connected: bool, outcome: str,
) -> None:
    """Read real entity properties while a real bucket replacement is saving."""
    tariff_change = outcome in {"tariff_changed", "tariff_type_changed"}

    async def scenario() -> None:
        previous = _plan("committed-A")
        now = previous.created_at + timedelta(minutes=1)
        replacement = _plan("replacement-B")
        replacement.created_at = now
        replacement.mode = PlannerMode.DRY_RUN
        context = DecisionContext(
            created_at=now, plan_id=replacement.plan_id,
            slots=[DecisionSlot(now, 0.2, 0.05, 0, 1)],
            current_battery_soc_percent=50, current_ev_soc_percent=60,
            occupancy_state=OccupancyState.OCCUPIED, input_health=InputHealth.HEALTHY,
            ev_connected=connected, ev_charging=False,
        )
        model = {"status": "ready", "first_expected_kw": 0.2229,
                 "first_upper_kw": 0.4, "source_entity_id": "sensor.house"}
        old_snapshot = {
            "created_at": previous.created_at.isoformat(), "plan_id": previous.plan_id,
            "built_in_load_forecast": {**model, "first_expected_kw": 0.2227},
            "confidence": {"forecast_source_confidence": 0.9},
        }
        saving = asyncio.Event()
        release = asyncio.Event()
        saved = []

        class DiskStore:
            def __init__(self, *args, **kwargs):
                pass

            async def async_save(self, data):
                if not saving.is_set() and (proposal := data.get("_pending_plan_publication")):
                    assert proposal["forecast_snapshots"][-1]["plan_id"] == replacement.plan_id
                    assert data["forecast_snapshots"][-1]["plan_id"] == previous.plan_id
                    assert data["discovery"] == {"fixture": True}
                    saving.set()
                    await release.wait()
                    if outcome == "failed":
                        raise OSError("forecast persistence failed")
                saved.append(deepcopy(data))

        monkeypatch.setattr(storage_module, "Store", DiskStore)
        store = PlannerStore(object())
        store.data["forecast_snapshots"] = [old_snapshot]
        store.data["active_plan"] = {"plan_id": previous.plan_id, "actions": []}
        tariff_id = "sensor.import_tariff"
        entry_data = {"ev_connected_entity": "binary_sensor.plug"}
        if tariff_change:
            entry_data[CONF_AMBER_IMPORT_PRICE] = tariff_id
        coordinator = _coordinator_for_runtime_services(
            entry_data=entry_data,
            hass=FakeHass({"binary_sensor.plug": "on" if connected else "off"}),
        )
        coordinator.ready_by = "06:15"
        if outcome == "mapping_changed":
            class EntryWithSubentries(FakeEntry):
                subentries: dict[str, SimpleNamespace]

            coordinator.entry = EntryWithSubentries(entry_data)
            coordinator.entry.subentries = {}
        # Exercise HA's real result publication and refresh-failure handling.
        coordinator.hass.is_stopping = False
        DataUpdateCoordinator.__init__(
            coordinator, coordinator.hass, logging.getLogger(__name__),
            config_entry=None, name="forecast publication",
            request_refresh_debouncer=SimpleNamespace(function=None, async_cancel=lambda: None),
        )
        coordinator.store = store
        coordinator.data = previous
        store.calendar_plan = previous
        store.data["production"] = {
            "dry_run_ready_cycles": 2,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                coordinator.entry_data, coordinator.options,
            ),
        }
        startup_validation = outcome == "failed"
        coordinator._startup_auto_recovery_validation_active = startup_validation
        coordinator.hass.async_create_task = asyncio.create_task
        coordinator._load_forecast_training_attempted = False
        coordinator._schedule_plan_execution = Mock()
        obsolete_advice = coordinator._ai_advice_task = Mock()
        obsolete_advice.done.return_value = False
        coordinator.__dict__["history_training"] = SimpleNamespace(
            request=lambda request: None, last_result=None,
        )
        monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)
        monkeypatch.setattr(coordinator_module, "CapabilityDiscovery", lambda *args: SimpleNamespace(
            inspect=lambda **kwargs: SimpleNamespace(as_dict=lambda: {"fixture": True}),
            hvac_evidence_in_dry=lambda: None,
        ))
        rows = [{"start_time": now.isoformat(), "end_time": (now + timedelta(minutes=30)).isoformat(),
                 "per_kwh": 1 if outcome == "tariff_type_changed" else 0.2}]
        class FixtureInputs(InputManager):
            async def async_build_context(self, overrides):
                assert await super().async_build_context(overrides) is not None
                self.load_forecast_details = model
                return context

        if tariff_change:
            coordinator.hass.states.values[tariff_id] = FakeState("0.2", {"forecasts": rows}, now)
            monkeypatch.setattr(coordinator_module, "InputManager", FixtureInputs)
        else:
            monkeypatch.setattr(coordinator_module, "InputManager", lambda *args, **kwargs: SimpleNamespace(
                current_forecast_observations=lambda: {}, async_build_context=AsyncMock(return_value=context),
                retained_hvac_tariff_slots=lambda context: None, forecast_training_slots=[],
                load_forecast_details=model,
            ))
        planner = Mock(return_value=SimpleNamespace(create_plan=lambda context: replacement))
        monkeypatch.setattr(coordinator_module, "DryRunPlanner", planner)
        monkeypatch.setattr(coordinator_module, "ConstraintValidator", lambda options: SimpleNamespace(
            validate_plan=lambda context, plan: [],
        ))
        description = next(item for item in SENSORS if item.key == "current_load_forecast")
        entity = PlannerSensor(coordinator, description)
        readings = []
        plan_pairs = []

        def listener():
            readings.append((entity.native_value, entity.extra_state_attributes))
            plan_pairs.append((coordinator.data.plan_id, store.data["active_plan"]["plan_id"]))

        coordinator._listeners = {("forecast", None): (listener, None)}
        coordinator.async_update_listeners()
        before = readings[-1]
        refresh = asyncio.create_task(coordinator._async_refresh())
        concurrent_save = None
        try:
            await asyncio.wait_for(saving.wait(), 5)
            assert store.data["forecast_snapshots"][-1]["plan_id"] == previous.plan_id
            assert len(store.data["forecast_snapshots"]) == 1
            assert coordinator.data is previous
            assert store.calendar_plan is previous
            assert store.data["production"]["dry_run_ready_cycles"] == 2
            coordinator._schedule_plan_execution.assert_not_called()
            during = readings[-1]
            # The real interval-boundary callback publishes clock-dependent
            # health independently of the still-running refresh.
            callbacks = []
            monkeypatch.setattr(coordinator_module, "async_call_later", lambda hass, delay, cb:
                                callbacks.append(cb) or (lambda: None))
            coordinator._schedule_debounced_refresh = lambda *args, **kwargs: None
            coordinator._schedule_next_boundary_refresh()
            callbacks[-1](now)
            # Background execution also publishes Store-backed outcomes in its
            # finally block, outside the refresh lock.
            coordinator._pending_plan_execution = _PlanExecution(0, previous, context, {})
            coordinator._async_execute_plan_if_current = AsyncMock()
            await coordinator._async_drain_plan_execution()
            assert readings[-2:] == [during, during]
            assert during[1]["plan_id"] == previous.plan_id
            assert before[0] == 0.2227
            assert before[1]["plan_id"] == previous.plan_id
            assert before[1]["model_status"] == "ready"
            if outcome == "obsolete":
                coordinator._mark_replan_requested(force=True)
            if tariff_change:
                rows[0]["per_kwh"] = True if outcome == "tariff_type_changed" else 0.6
            if outcome == "mapping_changed":
                generation = coordinator._refresh_generation
                coordinator.entry.subentries = {
                    "ev": SimpleNamespace(data={"ev_connected_entity": "binary_sensor.new"}),
                }
                assert coordinator._refresh_generation == generation
            if outcome == "options_changed":
                generation = coordinator._refresh_generation
                coordinator.entry.options = {**coordinator.entry.options, CONF_GRID_IMPORT_LIMIT_KW: 2.5}
                assert coordinator._refresh_generation == generation
            if outcome == "conflict":
                current = {**store.data["production"], "armed_reason": "new_operator_request"}
                concurrent_save = asyncio.create_task(store.async_save_production(current))
                await asyncio.sleep(0)
                assert store.data["production"] == current
        finally:
            release.set()
            await refresh
            if concurrent_save is not None:
                await concurrent_save
            await asyncio.gather(*coordinator._listener_tasks)
        result = coordinator.data
        assert all(published == stored for published, stored in plan_pairs)
        assert obsolete_advice.cancel.call_count == int(outcome == "committed")
        if outcome == "failed":
            assert coordinator.last_update_success is False
            assert isinstance(coordinator.last_exception, OSError)
            assert entity.available is False
            assert result is previous
            assert store.data["production"]["dry_run_ready_cycles"] == 2
            assert store.calendar_plan is previous
            coordinator._schedule_plan_execution.assert_not_called()
            if startup_validation:
                assert not coordinator._last_startup_auto_recovery_validation["committed"]
            return
        if tariff_change or outcome in {"obsolete", "conflict", "mapping_changed", "options_changed"}:
            assert result is previous
            assert latest_forecast_snapshot(coordinator) == old_snapshot
            assert store.data["active_plan"]["plan_id"] == previous.plan_id
            assert coordinator.executor.evaluated == []
            assert store.data["production"]["dry_run_ready_cycles"] == 2
            assert saved[-1]["active_plan"]["plan_id"] == previous.plan_id
            if outcome == "conflict":
                assert saved[-1]["production"]["armed_reason"] == "new_operator_request"
            else:
                assert len(saved) == 1
            coordinator._schedule_plan_execution.assert_not_called()
            return
        # HA publishes the returned plan and notifies entities synchronously.
        assert planner.call_args.args[0][CONF_DEFAULT_READY_BY] == "06:15"
        assert coordinator.options[CONF_DEFAULT_READY_BY] != coordinator.ready_by
        assert readings[-1][0] == 0.2229
        assert readings[-1][1]["plan_id"] == replacement.plan_id
        assert readings[-1][1]["valid_at"] == now.isoformat()
        assert readings[-1][1]["conservative_forecast_kw"] == 0.4
        assert saved[-1]["active_plan"]["plan_id"] == replacement.plan_id
        assert saved[-1]["production"]["dry_run_ready_cycles"] == 3
        assert len(saved) == 2
        assert len(saved[-1]["forecast_snapshots"]) == 1
        # A subsequent successful refresh with genuinely absent model evidence
        # must publish unknown, replacing the retained copy with only plan B.
        prior_id = replacement.plan_id
        now += timedelta(minutes=1)
        replacement = _plan("missing-evidence-C")
        replacement.created_at = context.created_at = now
        context.plan_id = replacement.plan_id
        model.clear()
        coordinator._force_next_refresh = True
        await coordinator._async_refresh()
        await asyncio.gather(*coordinator._listener_tasks)
        assert coordinator.data is replacement
        assert entity.available is True
        assert readings[-1] == (None, {})
        assert coordinator._retained_forecast_snapshot["plan_id"] == prior_id
        assert len(store.data["forecast_snapshots"]) == 1

    asyncio.run(scenario())


def test_refresh_rechecks_options_after_weather_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator_for_runtime_services()
    previous = coordinator.data = _plan("committed")
    coordinator._schedule_debounced_refresh = Mock()
    fingerprint = Mock()
    monkeypatch.setattr(coordinator_module, "_decision_input_fingerprint", fingerprint)

    async def weather(*args, **kwargs):
        # Core has installed the new configuration while its listener is still queued.
        coordinator.entry.options = {CONF_GRID_IMPORT_LIMIT_KW: 2.5}
        assert coordinator._refresh_generation == 0
        return {}, {}

    monkeypatch.setattr(coordinator, "_async_weather_for_refresh", weather)
    assert asyncio.run(coordinator._async_update_data()) is previous
    assert coordinator._force_next_refresh
    fingerprint.assert_not_called()
    coordinator._schedule_debounced_refresh.assert_called_once_with(
        "refresh_superseded", mode=coordinator_module._RefreshMode.IMMEDIATE, force=True,
    )


@pytest.mark.parametrize("case", ["absent", "missing_model", "invalid_number", "stale_model",
                                  "unsafe", "zero", "no_plan", "corrupt_history", "retained"])
def test_forecast_retention_requires_current_plan_evidence(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """Never borrow another plan/configuration or resurrect unusable load data."""
    coordinator = _coordinator_for_runtime_services()
    coordinator.data = _plan("current-config-plan")
    good_model = {"status": "ready", "first_expected_kw": 1.2, "first_upper_kw": 1.8}
    coordinator._retained_forecast_snapshot = {
        "plan_id": "obsolete-config-plan", "built_in_load_forecast": good_model,
    }
    snapshot = {"plan_id": coordinator.data.plan_id, "built_in_load_forecast": dict(good_model)}
    if case == "missing_model":
        snapshot["built_in_load_forecast"] = None
    elif case == "invalid_number":
        snapshot["built_in_load_forecast"]["first_expected_kw"] = float("nan")
    elif case == "stale_model":
        snapshot["built_in_load_forecast"] = {"status": "stale", "update_reason": "load_forecast_stale"}
    elif case == "unsafe":
        coordinator.data.health = InputHealth.UNSAFE
        coordinator.data.status = "unsafe"
        snapshot["built_in_load_forecast"] = {"status": "unavailable", "quality_failures": ["coverage"]}
    elif case == "zero":
        snapshot["built_in_load_forecast"]["first_expected_kw"] = 0
    elif case == "no_plan":
        coordinator.data = None
        coordinator._retained_forecast_snapshot = snapshot
    elif case == "retained":
        coordinator._retained_forecast_snapshot = snapshot
    # A newer snapshot with another configuration must never fill a gap.
    future = {"plan_id": "future-config-plan", "built_in_load_forecast": good_model}
    coordinator.store.data["forecast_snapshots"] = (
        [future] if case in {"absent", "retained"} else [snapshot, future]
    )
    if case == "corrupt_history":
        coordinator._retained_forecast_snapshot = snapshot
        coordinator.store.data["forecast_snapshots"] = "bad"
    entity = PlannerSensor(coordinator, next(item for item in SENSORS if item.key == "current_load_forecast"))
    expected = 0 if case == "zero" else 1.2 if case == "retained" else None
    assert entity.native_value == expected
    if case in {"absent", "missing_model", "no_plan", "corrupt_history"}:
        assert entity.extra_state_attributes == {}
    else:
        assert entity.extra_state_attributes["plan_id"] == "current-config-plan"
    # A clock-dependent health report still expires while the exact committed
    # snapshot remains available; retaining evidence grants no control lease.
    if case == "retained":
        expired_at = coordinator.data.created_at + timedelta(hours=1)
        monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: expired_at)
        coordinator.last_refresh_metadata = {
            "completed_at": coordinator.data.created_at, "succeeded": True,
        }
        assert planning_status(coordinator)["stalled"] is True
