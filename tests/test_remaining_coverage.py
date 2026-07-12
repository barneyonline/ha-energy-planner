"""Focused tests for remaining defensive branches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.ha_energy_planner import (
    _async_remove_legacy_device,
    _planner_entity_key,
    _validate_ready_by_time,
    _validate_reason_code,
    async_setup,
    async_setup_entry,
)
from custom_components.ha_energy_planner import forecasts as forecasts_module
from custom_components.ha_energy_planner import sensor as sensor_module
from custom_components.ha_energy_planner.ai_advisor import _invalid_response_reason, _parse_response, _preview_summary
from custom_components.ha_energy_planner.config_flow import _validate_config
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS, DOMAIN
from custom_components.ha_energy_planner.constraints import ConstraintValidator, _projected_grid_flows_kw
from custom_components.ha_energy_planner.coordinator import (
    EnergyPlannerCoordinator,
    _is_manual_hvac_change,
    _is_material_state_change,
    _latest_ai_service_call_at,
    _overrides_from_store,
)
from custom_components.ha_energy_planner.coordinator import (
    _bounded_json as coordinator_bounded_json,
)
from custom_components.ha_energy_planner.diagnostics import (
    _latest_haeo_status,
    _recent_items,
    _store_summary,
    _trip_history_summary,
)
from custom_components.ha_energy_planner.discovery import CapabilityEvidence, DiscoveryReport
from custom_components.ha_energy_planner.discovery import _split_entity_values as discovery_split_entities
from custom_components.ha_energy_planner.entity import planner_device_key_for_entity
from custom_components.ha_energy_planner.ev import update_trip_history_from_values
from custom_components.ha_energy_planner.ev_adapter import EVSmartChargingAdapter, _time_parts
from custom_components.ha_energy_planner.executor import Executor, _profile_control_service_for_target
from custom_components.ha_energy_planner.forecasts import _energy_items_as_average_power, _items_from_value, _parse_item
from custom_components.ha_energy_planner.haeo_adapter import apply_haeo_response_to_context
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
from custom_components.ha_energy_planner.inputs import InputManager
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    ConstraintViolation,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    HAEOSolvePhase,
    HAEOSolveResult,
    HAEOStatus,
    InputHealth,
    OccupancyState,
    PlanAction,
    PlannerMode,
)
from custom_components.ha_energy_planner.ownership import EnphaseProfileGuard, OwnershipState
from custom_components.ha_energy_planner.planner import DryRunPlanner
from custom_components.ha_energy_planner.preflight import (
    _audit_report,
    _bounded_join,
    _current_plan_report,
    _entity_report,
    _production_gate_message,
    _service_report,
    _split_entities,
)
from custom_components.ha_energy_planner.preflight import (
    _datetime_or_none as preflight_datetime_or_none,
)
from custom_components.ha_energy_planner.replay import ReplayActionResult, ReplayResult
from custom_components.ha_energy_planner.subentry_migration import (
    SUBENTRY_CLIMATE,
    SUBENTRY_ENERGY,
    SUBENTRY_ENPHASE,
    async_consolidate_subentries,
    grouped_subentry_data,
)
from custom_components.ha_energy_planner.system_health import system_health_info
from custom_components.ha_energy_planner.thermal_model import (
    _aligned_datetimes,
    thermal_hvac_load_kw,
    update_thermal_model,
)
from custom_components.ha_energy_planner.thermal_model import (
    _parse_datetime_or_none as thermal_parse_datetime,
)


@dataclass(slots=True)
class State:
    state: str


class States:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> State | None:
        value = self.values.get(entity_id)
        return None if value is None else State(value)


class Services:
    def __init__(self, available: set[tuple[str, str]] | None = None) -> None:
        self.available = available or set()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.available

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))


def test_remaining_validation_and_small_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        _validate_ready_by_time("bad")
    with pytest.raises(vol.Invalid):
        _validate_ready_by_time("25:00")
    with pytest.raises(vol.Invalid):
        _validate_reason_code("bad reason!")
    assert _validate_ready_by_time("7:05:30") == "07:05"
    assert _validate_reason_code("manual_service_call") == "manual_service_call"
    assert (
        _planner_entity_key("entry", SimpleNamespace(unique_id="", entity_id="sensor.ha_energy_planner_plan_status"))
        == "plan_status"
    )
    assert planner_device_key_for_entity("unknown_entity") == "system"
    assert _time_parts("bad") is None
    assert _time_parts("aa:bb") is None
    assert _time_parts("2026-06-27T07:30:00+10:00") == (7, 30)
    assert EnphaseProfileGuard(min_hold=timedelta(minutes=5), last_changed_at=None).can_change(datetime.now(UTC))

    assert _items_from_value({"data": {"unknown": []}}, ("value",)) == []
    assert (
        _energy_items_as_average_power(
            [{"period_start": "2026-06-27T00:00:00+00:00", "value": 1, "unit": "kWh"}],
            ("value",),
            "kWh",
        )[0]["value"]
        == 1
    )

    slot = DecisionSlot(datetime(2026, 6, 27, tzinfo=UTC), None, 0.05, 1, None)
    assert _projected_grid_flows_kw(slot) == (None, None)


def test_remaining_preflight_helpers() -> None:
    hass = SimpleNamespace(
        states=States(
            {
                "sensor.a": "unavailable",
                "sensor.b": "1",
                "button.ev_start": "unknown",
                "input_button.ev_stop": "unknown",
                "button.ev_unavailable": "unavailable",
            }
        ),
        services=Services({("ok", "service")}),
        config=SimpleNamespace(components={"recorder"}),
        data={},
    )
    entry_data = {
        "amber_import_price_entity": "sensor.a",
        "pv_forecast_entity": "sensor.missing",
        "ev_smart_charging_start_entity": "button.ev_start",
        "ev_smart_charging_stop_entity": "input_button.ev_stop",
        "ev_unavailable_entity": "button.ev_unavailable",
        "person_entities": ["person.a", "bad"],
        "service_key": "badservice",
        "haeo_optimize_service": "ok.service",
    }

    assert _entity_report(hass, entry_data)["unavailable"] == ["button.ev_unavailable", "sensor.a"]
    assert _service_report(hass, {"haeo_optimize_service": "ok.service", "ai_advisor_service": "badservice"})[
        "missing"
    ] == ["badservice"]
    assert _split_entities(["sensor.a", "bad"]) == ["sensor.a"]
    assert _split_entities(123) == []
    assert _audit_report({"execution_audit": ["bad", {"plan_id": "plan-1", "secret": "drop"}]})["recent_outcomes"] == [
        {},
        {"plan_id": "plan-1"},
    ]
    assert _bounded_join(["a", "b", "c", "d", "e", "f"]) == "a, b, c, d, e, 1 more"
    assert (
        _production_gate_message(
            {
                "ready_to_arm": False,
                "dry_run_ready_cycles": 3,
                "device_controls": {"ev": True, "climate": True, "enphase": True},
            }
        )
        == "Production gate is not ready to arm yet."
    )


def test_current_plan_report_defensive_and_status_branches() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    refresh = {"succeeded": True, "completed_at": now}

    assert _current_plan_report(None, now=now)["present"] is False
    malformed_horizon = SimpleNamespace(
        health="healthy",
        status="current",
        confidence=1.0,
        interval_minutes=5,
        horizon_hours=8,
        estimated_cost_horizon_hours="bad",
        input_issues=[],
        created_at=now,
    )
    assert _current_plan_report(
        malformed_horizon, now=now, last_refresh_metadata=refresh
    )["adequate_coverage"] is False
    stale_status = SimpleNamespace(
        **{
            **malformed_horizon.__dict__,
            "status": "stale",
            "estimated_cost_horizon_hours": 8,
        }
    )
    assert _current_plan_report(stale_status, now=now, last_refresh_metadata=refresh)["message"] == (
        "The latest plan is not current."
    )
    zero_confidence = SimpleNamespace(
        **{**malformed_horizon.__dict__, "confidence": 0.0, "estimated_cost_horizon_hours": 8}
    )
    assert _current_plan_report(zero_confidence, now=now, last_refresh_metadata=refresh)["message"] == (
        "Current plan confidence is zero."
    )
    assert preflight_datetime_or_none(123) is None
    assert preflight_datetime_or_none("bad") is None


def test_remaining_thermal_and_input_branches() -> None:
    now = datetime.now(UTC)
    updated, changed = update_thermal_model(
        {},
        {"sampled_at": now.isoformat(), "hvac_mode": "off", "indoor_temperature_c": 20, "hvac_power_kw": 0.0},
        {
            "sampled_at": (now + timedelta(hours=3)).isoformat(),
            "hvac_mode": "off",
            "indoor_temperature_c": 21,
            "hvac_power_kw": 0.0,
        },
    )
    assert changed is True
    assert updated["last_sample"]["indoor_temperature_c"] == 21
    assert (
        thermal_hvac_load_kw({"enabled": True, "active_hvac_load_kw": {"sample_count": 12, "average": "bad"}}, 1.5)
        == 1.5
    )
    assert thermal_parse_datetime(123) is None
    assert thermal_parse_datetime("bad") is None
    assert _aligned_datetimes(None, now) == (None, now)
    assert _aligned_datetimes(now.replace(tzinfo=None), now)[0].tzinfo == UTC
    assert _aligned_datetimes(now, now.replace(tzinfo=None))[1].tzinfo == UTC

    manager = InputManager(
        SimpleNamespace(states=States({"weather.home": "20", "person.a": "home"})),
        {"weather_entity": "weather.home", "person_entities": "person.a"},
        {
            "planning_interval_minutes": 5,
            "planning_horizon_hours": 1,
            "price_freshness_minutes": 30,
            "forecast_freshness_minutes": 60,
        },
    )
    current, series, issue = manager._optional_weather_temperatures("weather_entity", now, 1, 5)
    assert current == 20
    assert series[0] == 20
    assert issue is None
    assert manager._occupancy_state() == OccupancyState.OCCUPIED
    assert (
        InputManager(SimpleNamespace(states=States({})), {}, manager.options)._occupancy_state()
        == OccupancyState.UNKNOWN
    )


def test_remaining_coordinator_and_executor_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    assert _is_material_state_change(SimpleNamespace(data={"old_state": None, "new_state": State("1")}), {}) is True
    assert (
        _is_material_state_change(SimpleNamespace(data={"old_state": State("0"), "new_state": State("1")}), {}) is True
    )
    assert (
        _is_manual_hvac_change(
            SimpleNamespace(states=States({})),
            {"daikin_climate_entity": "climate.daikin"},
            {},
            SimpleNamespace(data={"entity_id": "climate.daikin", "old_state": None, "new_state": State("heat")}),
            now,
        )
        is False
    )
    assert _overrides_from_store({"overrides": ["bad"]}, now) == []

    action = PlanAction(
        "ev",
        "plan",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.EV,
        ActionKind.EV_START,
        {},
        [],
        [],
        None,
        1.0,
        None,
    )
    plan = EnergyPlan(
        "plan", now, 24, 5, "current", InputHealth.HEALTHY, PlannerMode.ACTIVE_HEALTHY, "summary", 1, None, [action], []
    )
    context = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.2, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.UNSAFE,
    )
    store = SimpleNamespace(
        data={"outcomes": []}, async_add_outcome=lambda outcome: _append_async(store.data["outcomes"], outcome)
    )
    asyncio.run(
        Executor(store, options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False}).async_evaluate(
            plan, context
        )
    )
    assert store.data["outcomes"][0].reason == "input_health_not_healthy"
    assert (
        Executor(store, options={"command_rate_limit_seconds": 1})._rate_limit_reason(
            action, now + timedelta(seconds=5)
        )
        is None
    )


def test_remaining_coordinator_haeo_non_ready_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.2, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
    )

    class Store:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {"trip_history": {}, "forecast_snapshots": []}
            self.haeo_runs: list[dict[str, Any]] = []
            self.forecast_snapshots: list[dict[str, Any]] = []

        async def async_save_discovery(self, data: dict[str, Any]) -> None:
            pass

        async def async_add_haeo_run(self, run: dict[str, Any]) -> None:
            self.haeo_runs.append(run)

        async def async_add_forecast_snapshot(self, snapshot: dict[str, Any]) -> None:
            self.forecast_snapshots.append(snapshot)

        async def async_save_plan(self, plan: EnergyPlan) -> None:
            pass

    class HAEO:
        def __init__(self, hass: Any, service: str) -> None:
            pass

        async def async_solve_baseline(self, ctx: DecisionContext) -> HAEOSolveResult:
            return HAEOSolveResult(HAEOSolvePhase.BASELINE, HAEOStatus.READY, "baseline", "plan")

        async def async_solve_with_flexible_load(self, ctx: DecisionContext, projections: list[Any]) -> HAEOSolveResult:
            return HAEOSolveResult(HAEOSolvePhase.FLEXIBLE_LOAD, HAEOStatus.FAILED, "second_failed", "plan")

    class Planner:
        def __init__(self, options: dict[str, Any], thermal_model: dict[str, Any]) -> None:
            pass

        def create_plan(self, ctx: DecisionContext) -> EnergyPlan:
            return EnergyPlan(
                "plan",
                now,
                24,
                5,
                "current",
                InputHealth.HEALTHY,
                PlannerMode.ACTIVE_HEALTHY,
                "summary",
                1,
                None,
                [],
                [],
            )

        def project_flexible_loads(self, ctx: DecisionContext) -> list[str]:
            return ["projection"]

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.CapabilityDiscovery",
        lambda hass, data: SimpleNamespace(inspect=lambda: SimpleNamespace(as_dict=lambda: {})),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_import_ev_trip_history_from_recorder", _trip_import_noop
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.InputManager",
        lambda *a, **k: SimpleNamespace(
            current_forecast_observations=lambda: {},
            build_context=lambda overrides: context,
            thermal_sample=lambda ctx: {},
            forecast_training_slots=[],
            forecast_calibration={},
        ),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_forecast_calibration", lambda *a, **k: ({}, False)
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_thermal_model", lambda *a, **k: ({}, False)
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.HAEOAdapter", HAEO)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.DryRunPlanner", Planner)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.ConstraintValidator",
        lambda options: SimpleNamespace(validate_plan=lambda ctx, plan: []),
    )

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)

    class Hass:
        async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
            return fn(*args)

    coordinator.hass = Hass()
    coordinator.entry = SimpleNamespace(data={}, options={})
    coordinator.store = Store()
    coordinator.executor = SimpleNamespace(
        entry_data={}, options={}, async_notify_plan_fallback=_noop_async, async_evaluate=_noop_async
    )
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0

    result = asyncio.run(coordinator._async_update_data_locked())

    assert "second_failed" in result.input_issues
    assert coordinator.store.haeo_runs[0]["second_pass"]["status"] == HAEOStatus.FAILED

    class FailedBaselineHAEO(HAEO):
        async def async_solve_baseline(self, ctx: DecisionContext) -> HAEOSolveResult:
            return HAEOSolveResult(HAEOSolvePhase.BASELINE, HAEOStatus.FAILED, "baseline_failed", None)

    class NoProjectionPlanner(Planner):
        def project_flexible_loads(self, ctx: DecisionContext) -> list[str]:
            return []

    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.HAEOAdapter", FailedBaselineHAEO)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.DryRunPlanner", NoProjectionPlanner)
    coordinator.store = Store()

    result = asyncio.run(coordinator._async_update_data_locked())

    assert "baseline_failed" in result.input_issues
    assert coordinator.store.haeo_runs[0]["second_pass"] is None


def test_remaining_subentry_migration_branches() -> None:
    subentries = {
        "energy-old": SimpleNamespace(
            subentry_id="energy-old",
            subentry_type=SUBENTRY_ENERGY,
            data={"amber_import_price_entity": "sensor.price", "weather_entity": "weather.home"},
        ),
        "climate-old": SimpleNamespace(
            subentry_id="climate-old",
            subentry_type=SUBENTRY_CLIMATE,
            data={"person_entities": "person.a", "climate_target_low_entity": "input_number.low"},
        ),
        "enphase-old": SimpleNamespace(
            subentry_id="enphase-old",
            subentry_type=SUBENTRY_ENPHASE,
            data={"ai_task_entity": "ai_task.local", "enphase_profile_entity": "select.profile"},
        ),
        "optimizer-old": SimpleNamespace(
            subentry_id="optimizer-old",
            subentry_type="optimizer",
            data={"baseline_load_forecast_entity": "sensor.load"},
        ),
        "ignore": SimpleNamespace(subentry_id="ignore", subentry_type="unknown", data={"x": 1}),
    }
    entry = SimpleNamespace(subentries=subentries)
    grouped = grouped_subentry_data(entry)

    assert grouped["energy"]["amber_import_price_entity"] == "sensor.price"
    assert grouped["climate"]["weather_entity"] == "weather.home"
    assert grouped["presence"]["person_entities"] == "person.a"
    assert grouped["ai"]["ai_task_entity"] == "ai_task.local"

    class ConfigEntries:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.updated: list[Any] = []
            self.removed: list[str] = []

        def async_add_subentry(self, entry: Any, subentry: Any) -> bool:
            self.added.append(subentry)
            entry.subentries[subentry.subentry_id] = subentry
            return True

        def async_update_subentry(self, entry: Any, subentry: Any, *, title: str, data: dict[str, Any]) -> bool:
            self.updated.append((subentry.subentry_type, title, data))
            return True

        def async_remove_subentry(self, entry: Any, subentry_id: str) -> bool:
            self.removed.append(subentry_id)
            return True

    hass = SimpleNamespace(config_entries=ConfigEntries())
    assert async_consolidate_subentries(hass, entry) is True
    assert hass.config_entries.added
    assert "optimizer-old" in hass.config_entries.removed


def test_remaining_system_health_and_registry_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    info = asyncio.run(
        system_health_info(
            SimpleNamespace(services=Services(), config_entries=SimpleNamespace(async_entries=lambda domain: []))
        )
    )
    assert info["configured_entries"] == 0

    entry = SimpleNamespace(subentries={"energy": SimpleNamespace(subentry_type="energy", subentry_id="energy-id")})
    from custom_components.ha_energy_planner.entity import planner_config_subentry_id

    assert planner_config_subentry_id(entry, "missing") is None

    entity = SimpleNamespace(platform=DOMAIN, device_id="legacy", entity_id="sensor.x", unique_id="uid")
    ent_reg = SimpleNamespace(
        entities={"sensor.x": entity},
        async_update_entity=lambda entity_id, **kwargs: setattr(entity, "device_id", kwargs["device_id"]),
    )
    dev = SimpleNamespace(id="legacy")
    removed: list[str] = []
    dev_reg = SimpleNamespace(
        async_get_device=lambda identifiers: dev, async_remove_device=lambda device_id: removed.append(device_id)
    )
    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: ent_reg)
    monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: dev_reg)

    _async_remove_legacy_device(SimpleNamespace(), SimpleNamespace(entry_id="entry"))

    assert entity.device_id is None
    assert removed == ["legacy"]


def test_remaining_setup_services_without_entries() -> None:
    class RegisteredServices(Services):
        def __init__(self) -> None:
            super().__init__()
            self.handlers: dict[str, Any] = {}

        def async_register(self, domain: str, service: str, handler: Any, **kwargs: Any) -> None:
            self.handlers[service] = handler

    services = RegisteredServices()
    hass = SimpleNamespace(
        services=services,
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )

    assert asyncio.run(async_setup(hass, {})) is True
    assert asyncio.run(services.handlers["export_diagnostics"](SimpleNamespace(data={}))) == {
        "error": "no_config_entry"
    }
    assert asyncio.run(services.handlers["run_preflight"](SimpleNamespace(data={}))) == {
        "ok": False,
        "error": "no_config_entry",
    }


def test_remaining_planner_guard_branches() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    planner = DryRunPlanner({**DEFAULT_OPTIONS, "planning_interval_minutes": 5, "hvac_precondition_lead_minutes": 0})
    context = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, None, 0.05, 0, 1), DecisionSlot(now + timedelta(minutes=5), 0.5, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.AWAY,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
    )
    start = now
    end = now + timedelta(minutes=5)

    assert planner._hvac_suppression_action(context, start, end) is None
    assert planner._hvac_preconditioning_action(context, start, end) is None
    context.occupancy_state = OccupancyState.OCCUPIED
    assert planner._hvac_preconditioning_action(context, start, end) is None
    context.current_hvac_temperature_c = 22
    context.occupied_temperature_low_c = 20
    context.occupied_temperature_high_c = 24
    assert planner._hvac_preconditioning_action(context, start, end) is None
    context.slots[0].import_price = 0.1
    assert planner._hvac_preconditioning_action(context, start, end) is None

    planner = DryRunPlanner({**DEFAULT_OPTIONS, "planning_interval_minutes": 5, "hvac_precondition_lead_minutes": 10})
    context.current_hvac_temperature_c = 22
    assert planner._hvac_preconditioning_action(context, start, end) is None


def test_remaining_ai_diagnostics_replay_and_system_health() -> None:
    assert _invalid_response_reason({1: "bad"}) == "ai_response_unsupported_fields"
    assert _parse_response({"response": {"data": '{"confidence": 0.5}'}}) == {"confidence": 0.5}
    assert _parse_response({"response": "bad json"}) is None
    assert _parse_response({"confidence": 0.5}) == {"confidence": 0.5}
    assert _parse_response(123) is None
    assert _preview_summary([]) == {}
    assert _preview_summary(
        [{"valid_at": "a", "import_price": 1, "occupied": "home"}, {"valid_at": "b", "import_price": 3}]
    ) == {
        "samples": 2,
        "start": "a",
        "end": "b",
        "import_price": [1.0, 3.0],
        "occupied": ["home"],
    }

    assert _latest_haeo_status({"forecast_snapshots": [{"haeo": {"status": "ready"}}]}) == {"status": "ready"}
    assert _trip_history_summary("bad") == {}
    assert _recent_items({"items": "bad"}, "items", limit=2) == []
    summary = _store_summary({"trip_history": {"records": "bad"}, "outcomes": "bad"})
    assert summary["outcome_count"] == 0
    assert summary["trip_history"]["record_count"] == 0

    replay = ReplayResult(
        "fixture",
        [ConstraintViolation("plan_bad", "Plan bad")],
        [ReplayActionResult("action", [ConstraintViolation("action_bad", "Action bad")])],
    )
    assert replay.rejected_action_count == 1
    assert replay.to_summary()["actions"][0]["violations"] == ["action_bad"]

    plan = EnergyPlan(
        "plan",
        datetime(2026, 6, 27, tzinfo=UTC),
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "summary",
        1,
        None,
        [],
        [],
    )
    coordinator = SimpleNamespace(
        data=plan,
        store=SimpleNamespace(
            data={"haeo_runs": [{"baseline": {"status": "ready"}}], "ai_recommendations": [{"status": "accepted"}]}
        ),
        options={"planner_enabled": True, "dry_run": False},
    )
    entry = SimpleNamespace(runtime_data=coordinator, subentries={"a": object()})
    info = asyncio.run(
        system_health_info(SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: [entry])))
    )
    assert info["loaded_entries"] == 1
    assert info["latest_haeo_status"] == "ready"
    assert info["latest_ai_status"] == "accepted"


def test_remaining_config_and_adapter_tail_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = SimpleNamespace(states=States({"sensor.value": "1"}), services=Services())
    assert _validate_config(hass, {"amber_import_price_entity": "sensor.value", "haeo_optimize_service": "bad"})[
        "haeo_optimize_service"
    ]

    assert (
        update_trip_history_from_values({"active_trip": {}}, connected=True, soc_percent=80, now=datetime.now(UTC))[1]
        is False
    )
    assert DaikinHVACAdapter(SimpleNamespace(states=States({}), services=Services()), {})._automation_entities() == []


def test_final_exact_remaining_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import voluptuous as vol

    from custom_components.ha_energy_planner import config_flow as config_flow_module
    from custom_components.ha_energy_planner import diagnostics as diagnostics_module
    from custom_components.ha_energy_planner import system_health as system_health_module
    from custom_components.ha_energy_planner.enphase_adapter import EnphaseProfileAdapter

    with pytest.raises(vol.Invalid):
        _validate_ready_by_time("aa:00")

    # Integration service and registry defensive branches.
    fake_coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    fake_coordinator.entry = SimpleNamespace(entry_id="entry")

    async def fake_diagnostics(hass: Any, entry: Any) -> dict[str, Any]:
        return {"entry_id": entry.entry_id}

    monkeypatch.setattr(diagnostics_module, "async_get_config_entry_diagnostics", fake_diagnostics)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [SimpleNamespace(runtime_data=fake_coordinator)]),
        services=SimpleNamespace(
            handlers={},
            async_register=lambda domain, service, handler, **kwargs: hass.services.handlers.setdefault(
                service, handler
            ),
        ),
    )
    asyncio.run(async_setup(hass, {}))
    assert asyncio.run(hass.services.handlers["export_diagnostics"](SimpleNamespace(data={}))) == {"entry_id": "entry"}

    dev_reg = SimpleNamespace(async_get_device=lambda identifiers: None)
    ent_reg = SimpleNamespace(entities={})
    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: ent_reg)
    monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: dev_reg)
    _async_remove_legacy_device(SimpleNamespace(), SimpleNamespace(entry_id="entry"))

    # Forecast defensive branches.
    assert _parse_item("bad", value_keys=("value",), value_kind="price") is None
    monkeypatch.setattr(forecasts_module, "_infer_bucket_hours", lambda timestamps: {timestamps[0]: 0})
    item = {"period_start": "2026-06-27T00:00:00+00:00", "value": 1, "unit": "kWh"}
    assert _energy_items_as_average_power([item], ("value",), "kWh") == [item]

    # Sensor labels and bounded JSON depth.
    assert (
        sensor_module._timeline_state_label({"state": "charging", "profile": "Full Backup"}) == "Charging: Full Backup"
    )
    assert sensor_module._charge_state_label_from_raw("charging") == "Charging"
    assert sensor_module._charge_timeline_state_label({"state": "paused"}) == "Paused"
    assert sensor_module._presence_attrs(
        SimpleNamespace(entry_data={"person_entities": "person.a, person.b"}, data=None)
    ) == {"person_entities": ["person.a", "person.b"]}
    assert sensor_module._display_state("   ") == "Unknown"

    # Discovery/system-health lower helpers.
    assert CapabilityEvidence(True).details == {}
    report = DiscoveryReport(
        CapabilityEvidence(True),
        CapabilityEvidence(True),
        CapabilityEvidence(True),
        CapabilityEvidence(True),
        CapabilityEvidence(True),
    )
    assert report.for_asset("bad") == CapabilityEvidence(False, ["unknown_asset"])
    assert report.as_dict()["haeo"] == {"supported": True, "issues": [], "details": {}}
    assert discovery_split_entities(["sensor.a", " bad "]) == ["sensor.a", "bad"]
    assert discovery_split_entities(123) == []
    registered: list[Any] = []
    system_health_module.async_register(
        SimpleNamespace(), SimpleNamespace(async_register_info=lambda fn: registered.append(fn))
    )
    assert registered == [system_health_info]
    assert system_health_module._latest_status([]) is None
    assert system_health_module._latest_status(["bad"]) is None
    assert system_health_module._latest_status([{"baseline": {"status": "ready"}}]) == "ready"
    assert system_health_module._latest_status([{"baseline": {}}]) is None

    # Adapter and validator tails.
    now = datetime.now(UTC)
    assert (
        DaikinHVACAdapter(
            SimpleNamespace(states=States({}), services=Services()), {"climate_automation_entities": 123}
        )._automation_entities()
        == []
    )
    assert DaikinHVACAdapter(SimpleNamespace(states=States({}), services=Services()), {})._state(None) is None

    class FailingHVACServices(Services):
        async def async_call(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("failed")

    suppress_action = PlanAction(
        "hvac",
        "plan",
        now,
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"suppress_automations": True},
        [],
        [],
        None,
        1,
        None,
    )
    suppress_result = asyncio.run(
        DaikinHVACAdapter(
            SimpleNamespace(
                states=States({"climate.daikin": "heat", "automation.hvac": "on"}), services=FailingHVACServices()
            ),
            {"daikin_climate_entity": "climate.daikin", "climate_automation_entities": "automation.hvac"},
        ).async_execute(suppress_action)
    )
    assert suppress_result.reason == "hvac_automation_service_failed"
    state = SimpleNamespace(state="cool", attributes={"target_temp_low": 20, "target_temp_high": "bad"})
    from custom_components.ha_energy_planner.hvac_adapter import _already_in_desired_state

    assert _already_in_desired_state(state, {"target_temp_high": 24}) is False
    assert EnphaseProfileAdapter(SimpleNamespace(states=States({}), services=Services()), {})._state(None) is None

    # Coordinator/executor final branches.
    action = PlanAction(
        "ev",
        "plan",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.EV,
        ActionKind.EV_START,
        {},
        [],
        [],
        None,
        1.0,
        None,
    )
    store = SimpleNamespace(data={"command_rate_limits": {"ev:ev_start": now.isoformat()}})
    assert (
        Executor(store, options={"command_rate_limit_seconds": 1})._rate_limit_reason(
            action, now + timedelta(seconds=5)
        )
        is None
    )
    expired_store = SimpleNamespace(
        data={"command_rate_limits": {"ev:ev_start": (now - timedelta(seconds=5)).isoformat()}}
    )
    assert Executor(expired_store, options={"command_rate_limit_seconds": 1})._rate_limit_reason(action, now) is None
    from custom_components.ha_energy_planner.executor import _service_target_for_action

    assert (
        _service_target_for_action(
            PlanAction("x", "p", now, now, ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {}, [], [], None, 1, None),
            {"enphase_profile_control_service": "select.select_option"},
        )
        == "select.select_option"
    )
    assert (
        _service_target_for_action(
            PlanAction("x", "p", now, now, ActionAsset.EV, ActionKind.SET_PROFILE, {}, [], [], None, 1, None), {}
        )
        is None
    )
    assert _profile_control_service_for_target({}, "input_select.profile") == "input_select.select_option"
    assert _latest_ai_service_call_at([{"service_called": False}, "bad"]) is None
    assert coordinator_bounded_json({"a": {"b": {"c": {"d": {"e": 1}}}}}) == {"a": {"b": {"c": {"d": "<truncated>"}}}}

    # HAEO response skip branches.
    context = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.2, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
    )
    assert (
        apply_haeo_response_to_context(
            context, {"slots": [{"valid_at": now.isoformat(), "grid_import_kw": 1e308, "unit": "MW"}]}
        )
        == {}
    )
    assert apply_haeo_response_to_context(context, {"slots": [{"valid_at": "bad", "grid_import_kw": "nan"}]}) == {}
    assert apply_haeo_response_to_context(context, {"slots": ["bad"]}) == {}
    assert apply_haeo_response_to_context(context, {"outer": {"inner": {}}}) == {}

    # Input and planner small branches.
    assert InputManager(
        SimpleNamespace(states=States({"binary_sensor.x": "charging"})), {"x": "binary_sensor.x"}, DEFAULT_OPTIONS
    )._optional_bool_state("x") == (True, None)
    assert InputManager(
        SimpleNamespace(states=States({"binary_sensor.x": "unavailable"})), {"x": "binary_sensor.x"}, DEFAULT_OPTIONS
    )._optional_bool_state("x") == (None, "x_unavailable")
    assert InputManager._health_from_issues(["battery_soc_entity_unavailable"]) == InputHealth.UNSAFE
    from custom_components.ha_energy_planner.inputs import _attribute_value

    assert _attribute_value({"Camel Key": "value"}, "camel_key") == "value"
    assert _attribute_value({"Camel Key": "value"}, "camel key") == "value"
    assert _attribute_value({"direct": "value"}, "direct") == "value"
    planner = DryRunPlanner({**DEFAULT_OPTIONS, "planning_interval_minutes": 5, "hvac_precondition_lead_minutes": 10})
    ctx = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.1, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
        current_hvac_temperature_c=17,
        occupied_temperature_low_c=20,
        occupied_temperature_high_c=24,
    )
    assert planner._hvac_preconditioning_action(ctx, now, now + timedelta(minutes=5)) is None
    ctx2 = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.1, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
        current_hvac_temperature_c=22,
        occupied_temperature_low_c=20,
        occupied_temperature_high_c=24,
    )
    assert planner._hvac_suppression_action(ctx2, now, now + timedelta(minutes=5)) is None
    ctx3 = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.1, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
        current_hvac_temperature_c=17,
        occupied_temperature_low_c=20,
        occupied_temperature_high_c=24,
    )
    assert planner._hvac_preconditioning_action(ctx3, now, now + timedelta(minutes=5)) is None
    ctx4 = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.1, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
        current_hvac_temperature_c=22,
        occupied_temperature_low_c=20,
        occupied_temperature_high_c=24,
    )
    assert planner._hvac_preconditioning_action(ctx4, now, now + timedelta(minutes=5)) is None

    # Subentry no-consolidation early return.
    entry = SimpleNamespace(
        subentries={
            "system": SimpleNamespace(subentry_id="system", subentry_type="system", data={}),
            "presence": SimpleNamespace(subentry_id="presence", subentry_type="presence", data={}),
        }
    )
    hass2 = SimpleNamespace(config_entries=SimpleNamespace(async_add_subentry=lambda entry, subentry: False))
    assert async_consolidate_subentries(hass2, entry) is False

    assert (
        EVSmartChargingAdapter(SimpleNamespace(states=States({}), services=Services()), {})._entity_value_matches(
            "sensor.missing", "x"
        )
        is False
    )
    assert EnphaseProfileGuard(min_hold=timedelta(minutes=5), last_changed_at=None).remaining_hold(now) == timedelta(0)
    monkeypatch.setattr(config_flow_module, "_ENTITY_DOMAIN_RULES", {"haeo_optimize_service": {"sensor"}})
    assert _validate_config(
        SimpleNamespace(states=States({}), services=Services()), {"haeo_optimize_service": "missing.service"}
    ) == {"haeo_optimize_service": "service_not_found"}

    class FailingServices:
        def has_service(self, domain: str, service: str) -> bool:
            return True

        async def async_call(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("failed")

    ev_action = PlanAction(
        "ev",
        "plan",
        now,
        now + timedelta(minutes=5),
        ActionAsset.EV,
        ActionKind.EV_SCHEDULE,
        {"ready_by": "07:00"},
        [],
        [],
        None,
        1,
        None,
    )
    ev_result = asyncio.run(
        EVSmartChargingAdapter(
            SimpleNamespace(
                states=States({"time.ready": "06:00", "switch.ev": "off", "binary_sensor.connected": "on"}),
                services=FailingServices(),
            ),
            {
                "ev_connected_entity": "binary_sensor.connected",
                "ev_smart_charging_start_entity": "switch.ev",
                "ev_smart_charging_ready_by_entity": "time.ready",
            },
        ).async_execute(ev_action)
    )
    assert ev_result.reason == "ev_ready_by_helper_unsupported"

    unsupported_enphase = PlanAction(
        "bad", "plan", now, now, ActionAsset.ENPHASE, ActionKind.EV_START, {}, [], [], None, 1, None
    )
    assert ConstraintValidator(DEFAULT_OPTIONS)._evaluate_enphase_action(unsupported_enphase, now, None) == []
    invalid_context = DecisionContext(
        now,
        "plan",
        [DecisionSlot(now, 0.2, 0.05, 0, 1)],
        50,
        50,
        OccupancyState.OCCUPIED,
        HAEOStatus.READY,
        InputHealth.HEALTHY,
    )
    hvac_action = PlanAction(
        "hvac",
        "plan",
        now,
        now,
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"suppress_automations": True},
        [],
        [],
        None,
        1,
        None,
    )
    assert (
        ConstraintValidator(DEFAULT_OPTIONS)
        ._evaluate_hvac_action(invalid_context, hvac_action, now, OwnershipState())[0]
        .code
        == "hvac_comfort_not_valid_for_suppression"
    )


def test_setup_entry_adds_default_options_for_empty_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def __init__(self, hass: Any) -> None:
            pass

        async def async_load(self) -> None:
            pass

    class Coordinator:
        def __init__(self, hass: Any, entry: Any, store: Any) -> None:
            self.entry = entry

        async def async_config_entry_first_refresh(self) -> None:
            pass

        def async_start_listeners(self) -> None:
            pass

        def async_shutdown(self) -> None:
            pass

        async def async_restore_safe_state(self, reason: str, *, refresh: bool = True) -> None:
            pass

    monkeypatch.setattr("custom_components.ha_energy_planner.storage.PlannerStore", Store)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.EnergyPlannerCoordinator", Coordinator)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.subentry_migration.async_consolidate_subentries", lambda hass, entry: False
    )
    monkeypatch.setattr("custom_components.ha_energy_planner._async_remove_legacy_device", lambda hass, entry: None)
    monkeypatch.setattr("custom_components.ha_energy_planner._async_sync_planner_devices", lambda hass, entry: None)
    updates: list[dict[str, Any]] = []
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda entry, **kwargs: updates.append(kwargs),
            async_forward_entry_setups=lambda entry, platforms: _noop_true(),
        )
    )
    entry = SimpleNamespace(
        options={},
        title="Energy Planner",
        data={},
        subentries={},
        runtime_data=None,
        add_update_listener=lambda listener: lambda: None,
        async_on_unload=lambda cb: None,
    )

    assert asyncio.run(async_setup_entry(hass, entry)) is True
    assert updates and updates[0]["options"] == DEFAULT_OPTIONS


async def _noop_true() -> bool:
    return True


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    return None


async def _trip_import_noop(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], bool, str]:
    return {}, False, "not_due"


async def _append_async(target: list[Any], item: Any) -> None:
    target.append(item)
