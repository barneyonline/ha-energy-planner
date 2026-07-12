"""Tests for coordinator helper behavior."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.ha_energy_planner import coordinator as coordinator_module
from custom_components.ha_energy_planner.ai_advisor import AIAdviceResult
from custom_components.ha_energy_planner.const import (
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_DAIKIN_CLIMATE,
    CONF_DEFAULT_READY_BY,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SOC,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from custom_components.ha_energy_planner.coordinator import (
    EnergyPlannerCoordinator,
    _bool_state_value,
    _configured_entity_ids,
    _decision_input_fingerprint,
    _float_state_value,
    _is_manual_hvac_change,
    _is_material_state_change,
    _is_planner_owned_control_feedback,
    _latest_ai_plan_fingerprint,
    _latest_ai_service_call_at,
    _material_plan_fingerprint,
    _overrides_from_store,
    _parse_datetime_or_none,
    _seconds_until_next_interval_boundary,
    _snapshot_actions,
    _split_entity_values,
)
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
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
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint


def test_configured_entity_ids_excludes_services_and_splits_lists() -> None:
    entity_ids = _configured_entity_ids(
        {
            "haeo_optimize_service": "haeo.optimize",
            "enphase_profile_control_service": "select.select_option",
            "amber_import_price_entity": "sensor.import_price",
            "climate_automation_entities": "automation.heat, automation.cool",
            "person_entities": "person.james,person.cath",
            "ai_advisor_service": "ai_task.generate_data",
            "ai_task_entity": "ai_task.local",
            "daikin_power_entity": "sensor.daikin_power",
            "ev_smart_charging_entity": "switch.ev_control",
            "empty_entity": "",
        }
    )
    assert entity_ids == [
        "person.cath",
        "person.james",
        "sensor.import_price",
    ]


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str
    attributes: dict[str, object] = field(default_factory=dict)


class FakeStates:
    """Minimal state registry."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.states = FakeStates(values or {})
        self.services = SimpleNamespace(calls=[], async_call=self._async_call_service)
        self.created_tasks: list[object] = []

    def async_create_task(self, task: object) -> None:
        close = getattr(task, "close", None)
        if callable(close):
            close()
        self.created_tasks.append(task)

    async def async_add_executor_job(self, func: object, *args: object) -> object:
        return func(*args)

    async def _async_call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, object],
        *,
        blocking: bool,
    ) -> None:
        self.services.calls.append((domain, service, data, blocking))


class FakeStore:
    """Minimal planner store."""

    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = data or {}
        self.saved_plans: list[EnergyPlan] = []
        self.discovery: list[dict[str, object]] = []
        self.trip_history: list[dict[str, object]] = []
        self.forecast_calibrations: list[dict[str, object]] = []
        self.thermal_models: list[dict[str, object]] = []
        self.haeo_runs: list[dict[str, object]] = []
        self.ai_recommendations: list[dict[str, object]] = []
        self.forecast_snapshots: list[dict[str, object]] = []
        self.dry_run_comparisons: list[dict[str, object]] = []
        self.production_saves: list[dict[str, object]] = []
        self.control_pause_saves: list[dict[str, object]] = []

    async def async_save_plan(self, plan: EnergyPlan) -> None:
        self.saved_plans.append(plan)

    async def async_save_overrides(self, overrides: list[object]) -> None:
        self.data["overrides"] = overrides

    async def async_save_ownership(self, ownership: dict[str, object]) -> None:
        self.data["ownership"] = ownership

    async def async_save_discovery(self, discovery: dict[str, object]) -> None:
        self.discovery.append(discovery)

    async def async_save_trip_history(self, trip_history: dict[str, object]) -> None:
        self.trip_history.append(trip_history)
        self.data["trip_history"] = trip_history

    async def async_save_forecast_calibration(self, calibration: dict[str, object]) -> None:
        self.forecast_calibrations.append(calibration)
        self.data["forecast_calibration"] = calibration

    async def async_save_thermal_model(self, thermal_model: dict[str, object]) -> None:
        self.thermal_models.append(thermal_model)
        self.data["thermal_model"] = thermal_model

    async def async_add_haeo_run(self, run: dict[str, object]) -> None:
        self.haeo_runs.append(run)

    async def async_add_ai_recommendation(self, recommendation: dict[str, object]) -> None:
        self.ai_recommendations.append(recommendation)

    async def async_add_forecast_snapshot(self, snapshot: dict[str, object]) -> None:
        self.forecast_snapshots.append(snapshot)

    async def async_add_dry_run_comparison(self, comparison: dict[str, object]) -> None:
        self.dry_run_comparisons.append(comparison)
        self.data["dry_run_comparisons"] = [comparison]

    async def async_save_production(self, production: dict[str, object]) -> None:
        self.production_saves.append(production)
        self.data["production"] = production

    async def async_save_control_pause(self, pause: dict[str, object]) -> None:
        self.control_pause_saves.append(pause)
        self.data["control_pause"] = pause

    @asynccontextmanager
    async def async_delay_save(self) -> object:
        self.delay_entered = True
        yield
        self.delay_exited = True


class FakeExecutor:
    """Minimal executor."""

    def __init__(self) -> None:
        self.options = {}
        self.entry_data = {}
        self.evaluated: list[tuple[EnergyPlan, object]] = []
        self.restored: list[str] = []

    async def async_evaluate(self, plan: EnergyPlan, context: object) -> None:
        self.evaluated.append((plan, context))

    async def async_restore_safe_state(self, reason: str) -> None:
        self.restored.append(reason)

    async def async_notify_plan_fallback(self, plan: EnergyPlan, violations: list[str]) -> None:
        self.fallback = (plan, violations)
        self.fallback_options = dict(self.options)


@dataclass(slots=True)
class FakeEntry:
    """Minimal config entry."""

    data: dict[str, str]
    options: dict[str, object] = field(default_factory=dict)


def test_coordinator_records_refresh_duration_in_memory() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()
    expected = _plan("refresh-duration")

    async def update_locked() -> EnergyPlan:
        coordinator._pending_refresh_trigger = "newer_request"
        return expected

    coordinator._pending_refresh_trigger = "state_change"
    coordinator._async_update_data_locked = update_locked

    result = asyncio.run(coordinator._async_update_data())

    assert result is expected
    assert coordinator.last_refresh_metadata["succeeded"] is True
    assert coordinator.last_refresh_metadata["duration_ms"] >= 0
    assert coordinator.last_refresh_metadata["completed_at"].tzinfo is not None
    assert coordinator.last_refresh_metadata["trigger"] == "state_change"
    assert coordinator.refresh_metrics["trigger_counts"] == {"state_change": 1}
    assert coordinator.refresh_metrics["succeeded"] == 1


def test_coordinator_records_failed_refresh() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()

    async def update_locked() -> EnergyPlan:
        raise RuntimeError("failed refresh")

    coordinator._async_update_data_locked = update_locked
    try:
        asyncio.run(coordinator._async_update_data())
    except RuntimeError:
        pass

    assert coordinator.refresh_metrics["failed"] == 1
    assert coordinator.last_refresh_metadata["succeeded"] is False


class FakeEvent:
    """Minimal state changed event."""

    def __init__(
        self,
        entity_id: str,
        old: str,
        new: str,
        *,
        old_attributes: dict[str, object] | None = None,
        new_attributes: dict[str, object] | None = None,
    ) -> None:
        self.data = {
            "entity_id": entity_id,
            "old_state": FakeState(old, old_attributes or {}),
            "new_state": FakeState(new, new_attributes or {}),
        }


def test_manual_hvac_change_detected_without_guard() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert _is_manual_hvac_change(
        FakeHass(),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
        {"ownership": {}},
        FakeEvent("climate.daikin", "heat", "off"),
        now,
    )


def test_manual_hvac_change_ignored_when_scheduler_guard_on() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert not _is_manual_hvac_change(
        FakeHass({"input_boolean.scheduler": "on"}),
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler",
        },
        {"ownership": {}},
        FakeEvent("climate.daikin", "heat", "off"),
        now,
    )


def test_manual_hvac_change_preserved_during_planner_grace_when_not_expected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert _is_manual_hvac_change(
        FakeHass(),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
        {"ownership": {"planner_hvac_action_expires_at": (now + timedelta(minutes=1)).isoformat()}},
        FakeEvent("climate.daikin", "heat", "off"),
        now,
    )


def test_material_state_change_uses_configured_percent_threshold() -> None:
    assert not _is_material_state_change(
        FakeEvent("sensor.price", "100", "104"), {"material_change_threshold_percent": 5}
    )
    assert _is_material_state_change(FakeEvent("sensor.price", "100", "105"), {"material_change_threshold_percent": 5})
    assert _is_material_state_change(
        FakeEvent("person.james", "home", "not_home"), {"material_change_threshold_percent": 5}
    )
    assert not _is_material_state_change(
        FakeEvent("sensor.price", "on", "on"), {"material_change_threshold_percent": 5}
    )


def test_material_state_change_treats_non_finite_numbers_as_material() -> None:
    options = {"material_change_threshold_percent": 5}

    assert _is_material_state_change(FakeEvent("sensor.price", "1.0", "nan"), options)
    assert _is_material_state_change(FakeEvent("sensor.price", "inf", "1.0"), options)


def test_material_state_change_detects_only_planner_input_attribute_updates() -> None:
    options = {"material_change_threshold_percent": 5}
    old_forecast = [{"valid_at": "2026-06-27T10:00:00+00:00", "value": 1.0}]
    new_forecast = [{"valid_at": "2026-06-27T10:00:00+00:00", "value": 2.0}]

    assert _is_material_state_change(
        FakeEvent(
            "sensor.pv_forecast",
            "1.0",
            "1.0",
            old_attributes={"forecast": old_forecast, "friendly_name": "PV old"},
            new_attributes={"forecast": new_forecast, "friendly_name": "PV new"},
        ),
        options,
    )
    assert not _is_material_state_change(
        FakeEvent(
            "sensor.pv_forecast",
            "1.0",
            "1.0",
            old_attributes={"forecast": old_forecast, "friendly_name": "PV old"},
            new_attributes={"forecast": old_forecast, "friendly_name": "PV new"},
        ),
        options,
    )


def test_material_attribute_change_overrides_subthreshold_numeric_state_change() -> None:
    assert _is_material_state_change(
        FakeEvent(
            "weather.home",
            "20.0",
            "20.1",
            old_attributes={"temperature": 20.0},
            new_attributes={"temperature": 21.0},
        ),
        {"material_change_threshold_percent": 5},
    )


def test_material_state_change_canonicalizes_camel_case_forecast_attributes() -> None:
    changes = (
        ("pvEstimate", [1.0, 2.0], [2.0, 3.0]),
        ("baselineLoadForecastKw", [0.5, 0.6], [0.7, 0.8]),
        ("forecastConfidence", 0.8, 0.9),
        ("unitOfMeasurement", "W", "kW"),
        ("forecastIntervalMinutes", 30, 60),
        ("intervalMinutes", 30, 15),
        ("resolutionMinutes", 30, 15),
        ("detailedForecast", [{"value": 1.0}], [{"value": 2.0}]),
    )

    for key, old_value, new_value in changes:
        assert _is_material_state_change(
            FakeEvent(
                "sensor.forecast",
                "unchanged",
                "unchanged",
                old_attributes={key: old_value},
                new_attributes={key: new_value},
            ),
            {"material_change_threshold_percent": 5},
        ), key


def test_overrides_restored_only_when_active() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    overrides = _overrides_from_store(
        {
            "overrides": [
                {
                    "kind": "manual_hvac",
                    "source": "service",
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    "reason": "active",
                },
                {
                    "kind": "manual_hvac",
                    "source": "service",
                    "expires_at": (now - timedelta(minutes=5)).isoformat(),
                    "reason": "expired",
                },
            ]
        },
        now,
    )
    assert len(overrides) == 1
    assert overrides[0].reason == "active"


def test_seconds_until_next_interval_boundary() -> None:
    assert (
        _seconds_until_next_interval_boundary(
            datetime(2026, 6, 27, 10, 3, 30, tzinfo=UTC),
            5,
        )
        == 90.0
    )
    assert (
        _seconds_until_next_interval_boundary(
            datetime(2026, 6, 27, 10, 5, 0, tzinfo=UTC),
            5,
        )
        == 300.0
    )


def test_start_listeners_schedules_configured_boundary_refresh_without_entities(monkeypatch: object) -> None:
    calls: list[float] = []

    def fake_async_call_later(hass: object, delay: float, action: object) -> object:
        calls.append(delay)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_async_call_later,
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = FakeEntry({}, {CONF_PLANNING_INTERVAL_MINUTES: 15})
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []

    coordinator.async_start_listeners()

    assert len(calls) == 1
    assert 0 < calls[0] <= 900
    assert coordinator._unsub_listeners == []


def test_coordinator_init_sets_runtime_state_without_real_data_update_coordinator(
    monkeypatch: object, caplog: object
) -> None:
    caplog.set_level(logging.INFO, logger=coordinator_module.__name__)

    def fake_data_update_init(
        self: object, hass: object, *, logger: object, name: str, update_interval: object
    ) -> None:
        self.hass = hass
        self.data = None

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.DataUpdateCoordinator.__init__",
        fake_data_update_init,
    )
    store = FakeStore(
        {
            "overrides": [
                {
                    "kind": "manual_hvac",
                    "source": "store",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "reason": "active",
                }
            ]
        }
    )
    entry = FakeEntry(
        {"amber_import_price_entity": "sensor.import"},
        {CONF_DEFAULT_READY_BY: "06:30", "ai_enabled": True},
    )

    coordinator = EnergyPlannerCoordinator(FakeHass(), entry, store)

    assert coordinator.entry is entry
    assert coordinator.store is store
    assert coordinator.ready_by == "06:30"
    assert coordinator.executor.entry_data == {"amber_import_price_entity": "sensor.import"}
    assert coordinator.executor.notification_grace_until is not None
    assert coordinator.executor.notification_grace_until > datetime.now(UTC)
    assert coordinator.planner_enabled is False
    assert coordinator.dry_run is True
    assert len(coordinator.overrides) == 1
    assert "provider may log bounded prompts" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)

    coordinator.entry.options["planner_enabled"] = "true"
    coordinator.entry.options["dry_run"] = "false"
    assert coordinator.planner_enabled is False
    assert coordinator.dry_run is True


def test_coordinator_builds_configured_haeo_adapter_and_capability_metadata() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator._haeo_adapter = None

    adapter = coordinator._get_haeo_adapter(
        {"haeo_optimize_service": "custom.optimize", "haeo_config_entry_id": "entry-1"}
    )

    assert adapter.optimize_service == "custom.optimize"
    assert adapter.haeo_config_entry_id == "entry-1"
    assert coordinator_module._haeo_capability_metadata(adapter) == adapter.capabilities.as_dict()


def test_async_update_data_uses_lock_and_delay_save() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()

    async def fake_locked() -> EnergyPlan:
        return _plan("locked")

    coordinator._async_update_data_locked = fake_locked

    result = asyncio.run(coordinator._async_update_data())

    assert result.plan_id == "locked"
    assert coordinator.store.delay_entered is True
    assert coordinator.store.delay_exited is True


def test_start_listeners_handles_manual_ev_and_material_changes(monkeypatch: object) -> None:
    callbacks: list[object] = []
    scheduled: list[float] = []

    def fake_track(hass: object, entity_ids: list[str], callback: object) -> object:
        callbacks.append(callback)
        return lambda: scheduled.append(-1)

    def fake_call_later(hass: object, delay: float, action: object) -> object:
        scheduled.append(delay)
        return lambda: scheduled.append(-2)

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
        fake_track,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_call_later,
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass({"input_boolean.scheduler": "off"})
    coordinator.entry = FakeEntry(
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SOC: "sensor.ev_soc",
            "amber_import_price_entity": "sensor.price",
        },
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
    )
    coordinator.store = FakeStore(
        {
            "ownership": {},
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": datetime.now(UTC),
                    "desired_state": {"hvac_mode": "heat"},
                }
            ],
        }
    )
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []
    coordinator._refresh_generation = 0

    coordinator.async_start_listeners()
    callback = callbacks[0]
    callback(FakeEvent("climate.daikin", "off", "heat"))
    assert coordinator.hass.created_tasks == []
    coordinator.store.data["execution_audit"] = []
    callback(FakeEvent("climate.daikin", "off", "heat"))
    callback(FakeEvent("sensor.ev_soc", "50", "51"))
    callback(FakeEvent("sensor.price", "100", "110"))

    assert len(coordinator.hass.created_tasks) == 2
    assert coordinator._refresh_generation == 1
    assert coordinator._debounce_cancel is not None
    assert len(scheduled) >= 2


def test_obsolete_planner_result_does_not_save_or_execute() -> None:
    previous = _plan("previous")
    stale = _plan("stale")
    coordinator = _coordinator_for_commit(previous, current_generation=2)

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            1,
            stale,
            object(),
            {"planner_enabled": True},
        )
    )

    assert result is previous
    assert coordinator.store.saved_plans == []
    assert coordinator.executor.evaluated == []


def test_obsolete_planner_result_schedules_refresh_when_hass_present() -> None:
    previous = _plan("previous")
    stale = _plan("stale")
    coordinator = _coordinator_for_commit(previous, current_generation=2)
    coordinator.hass = FakeHass()

    result = asyncio.run(coordinator._async_commit_plan_if_current(1, stale, object(), {"planner_enabled": True}))

    assert result is previous
    assert len(coordinator.hass.created_tasks) == 1


def test_current_planner_result_saves_and_executes() -> None:
    plan = _plan("current")
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            plan,
            context,
            {"planner_enabled": True},
        )
    )

    assert result is plan
    assert coordinator.store.saved_plans == [plan]
    assert coordinator.executor.evaluated == [(plan, context)]
    assert coordinator.executor.options == {"planner_enabled": True}


def test_planner_options_include_runtime_ready_by_override() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry({}, {CONF_DEFAULT_READY_BY: "07:00"})
    coordinator.ready_by = "08:30"

    assert coordinator.options[CONF_DEFAULT_READY_BY] == "07:00"
    assert coordinator.planner_options[CONF_DEFAULT_READY_BY] == "08:30"
    assert coordinator.entry.options[CONF_DEFAULT_READY_BY] == "07:00"


def test_ai_advice_is_rate_limited_to_five_minutes() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore(
        {
            "ai_recommendations": [
                {
                    "created_at": (now - timedelta(seconds=120)).isoformat(),
                    "status": "accepted",
                    "service_called": "ai_task.generate_data",
                }
            ]
        }
    )
    context = SimpleNamespace(created_at=now)

    result, should_store = asyncio.run(coordinator._async_get_throttled_ai_advice(context, _plan("plan-1"), {}, {}))

    assert should_store is False
    assert result.status == "skipped"
    assert result.rejected_reason == "ai_rate_limited"
    assert result.service_called is None
    assert result.rejected_detail["retry_after_seconds"] == 180


def test_ai_advice_runs_after_rate_limit_window(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    calls = 0

    class FakeAIAdvisor:
        def __init__(self, hass: object, entry_data: dict[str, object], options: dict[str, object]) -> None:
            pass

        async def async_get_advice(self, context: object, plan: EnergyPlan) -> AIAdviceResult:
            nonlocal calls
            calls += 1
            return AIAdviceResult(
                status="accepted",
                accepted={"confidence": 0.8},
                rejected_reason=None,
                rejected_detail={},
                service_called="ai_task.generate_data",
            )

    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.LocalAIAdvisor", FakeAIAdvisor)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.store = FakeStore(
        {
            "ai_recommendations": [
                {
                    "created_at": (now - timedelta(seconds=301)).isoformat(),
                    "status": "accepted",
                    "service_called": "ai_task.generate_data",
                }
            ]
        }
    )
    context = SimpleNamespace(created_at=now)

    result, should_store = asyncio.run(coordinator._async_get_throttled_ai_advice(context, _plan("plan-1"), {}, {}))

    assert calls == 1
    assert should_store is True
    assert result.status == "accepted"


def test_ai_advice_skips_unsafe_or_zero_confidence_plan() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore({"ai_recommendations": []})
    plan = _plan("unsafe")
    plan.health = InputHealth.UNSAFE
    plan.confidence = 0

    result, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=plan.created_at), plan, {}, {})
    )

    assert should_store is False
    assert result.rejected_reason == "ai_skipped_unsafe_plan"

    plan.health = InputHealth.HEALTHY
    plan.status = "unsafe"
    result, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=plan.created_at), plan, {}, {})
    )
    assert should_store is False
    assert result.rejected_reason == "ai_skipped_unsafe_plan"


def test_background_ai_is_non_blocking_single_flight_and_persists() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        updates: list[str] = []
        calls = 0

        class TaskHass:
            @staticmethod
            def async_create_task(coro: object) -> asyncio.Task[None]:
                return asyncio.create_task(coro)

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = TaskHass()
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._ai_advice_task = None
        coordinator._ai_advice_fingerprint = None
        coordinator._last_phase_durations = {}
        coordinator._planner_lock = asyncio.Lock()
        coordinator.async_update_listeners = lambda: updates.append("updated")
        plan = _plan("background-ai")
        context = SimpleNamespace(created_at=plan.created_at)

        async def delayed_advice(
            built_context: object,
            built_plan: EnergyPlan,
            entry_data: dict[str, object],
            options: dict[str, object],
        ) -> tuple[AIAdviceResult, bool]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return AIAdviceResult("accepted", {"confidence": 0.8}, None, "ai_task.generate_data"), True

        coordinator._async_get_throttled_ai_advice = delayed_advice
        coordinator._schedule_ai_advice(context, plan, {}, {"ai_enabled": True})
        task = coordinator._ai_advice_task
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=1)
        assert coordinator.store.ai_recommendations == []

        coordinator._schedule_ai_advice(context, plan, {}, {"ai_enabled": True})
        assert coordinator._ai_advice_task is task
        assert calls == 1

        release.set()
        await task
        assert coordinator.store.ai_recommendations[0]["status"] == "accepted"
        assert updates == ["updated"]
        assert "ai_background_ms" in coordinator._last_phase_durations
        assert coordinator._ai_advice_task is None

    asyncio.run(scenario())


def test_background_ai_replaces_stale_flight_and_shutdown_cancels() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class TaskHass:
            @staticmethod
            def async_create_task(coro: object) -> asyncio.Task[None]:
                return asyncio.create_task(coro)

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = TaskHass()
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._ai_advice_task = None
        coordinator._ai_advice_fingerprint = None
        coordinator._last_phase_durations = {}
        coordinator._planner_lock = asyncio.Lock()
        coordinator._debounce_cancel = None
        coordinator._boundary_cancel = None
        coordinator._unsub_listeners = []
        coordinator.async_update_listeners = lambda: None
        context = SimpleNamespace(created_at=_plan("first").created_at)

        async def blocked_advice(*args: object) -> tuple[AIAdviceResult, bool]:
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        coordinator._async_get_throttled_ai_advice = blocked_advice
        first = _plan("first")
        coordinator._schedule_ai_advice(context, first, {}, {"ai_enabled": True})
        first_task = coordinator._ai_advice_task
        assert first_task is not None
        await asyncio.wait_for(started.wait(), timeout=1)

        second = _plan("second")
        second.preview = [{"import_price": 0.42}]
        started.clear()
        coordinator._schedule_ai_advice(context, second, {}, {"ai_enabled": True})
        second_task = coordinator._ai_advice_task
        assert second_task is not None and second_task is not first_task
        await asyncio.wait_for(started.wait(), timeout=1)
        assert first_task.cancelled()

        coordinator.async_shutdown()
        try:
            await second_task
        except asyncio.CancelledError:
            pass
        assert second_task.cancelled()
        assert coordinator._ai_advice_task is None

    asyncio.run(scenario())


def test_background_ai_is_cancelled_when_current_plan_becomes_unsafe() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class TaskHass:
            @staticmethod
            def async_create_task(coro: object) -> asyncio.Task[None]:
                return asyncio.create_task(coro)

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = TaskHass()
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._ai_advice_task = None
        coordinator._ai_advice_fingerprint = None
        coordinator._last_phase_durations = {}
        coordinator._planner_lock = asyncio.Lock()

        async def blocked(*args: object) -> tuple[AIAdviceResult, bool]:
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        coordinator._async_get_throttled_ai_advice = blocked
        safe = _plan("safe")
        context = SimpleNamespace(created_at=safe.created_at)
        coordinator._schedule_ai_advice(context, safe, {}, {"ai_enabled": True})
        task = coordinator._ai_advice_task
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=1)

        unsafe = _plan("unsafe")
        unsafe.health = InputHealth.UNSAFE
        unsafe.status = "unsafe"
        coordinator._schedule_ai_advice(context, unsafe, {}, {"ai_enabled": True})
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.cancelled()
        assert coordinator.store.ai_recommendations == []
        assert coordinator._ai_current_plan_safe is False

    asyncio.run(scenario())


def test_background_ai_rechecks_committed_plan_under_planner_lock() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._planner_lock = asyncio.Lock()
        coordinator._last_phase_durations = {}
        coordinator.async_update_listeners = lambda: None
        plan = _plan("safe-race")
        fingerprint = _material_plan_fingerprint(plan)
        coordinator._ai_advice_fingerprint = fingerprint
        coordinator._ai_current_plan_fingerprint = fingerprint
        coordinator._ai_current_plan_safe = True

        async def accepted(*args: object) -> tuple[AIAdviceResult, bool]:
            return AIAdviceResult("accepted", {"confidence": 0.8}, None, "ai_task.generate_data"), True

        coordinator._async_get_throttled_ai_advice = accepted
        async with coordinator._planner_lock:
            task = asyncio.create_task(
                coordinator._async_run_ai_advice(
                    SimpleNamespace(created_at=plan.created_at),
                    plan,
                    {},
                    {"ai_enabled": True},
                    fingerprint,
                )
            )
            await asyncio.sleep(0)
            coordinator._ai_current_plan_safe = False
            coordinator._ai_current_plan_fingerprint = None
        await task

        assert coordinator.store.ai_recommendations == []

    asyncio.run(scenario())


def test_planner_owned_control_feedback_uses_grace_evidence() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    daikin_event = FakeEvent("climate.daikin", "off", "heat", new_attributes={"temperature": 21})
    enphase_event = FakeEvent("select.enphase", "AI Optimisation", "Full Backup")
    entry_data = {
        "daikin_climate_entity": "climate.daikin",
        "enphase_profile_entity": "select.enphase",
    }

    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now - timedelta(seconds=30),
                    "desired_state": {"hvac_mode": "heat", "target_temperature": 21},
                }
            ]
        },
        daikin_event,
        now,
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "enphase",
                    "attempted_at": now - timedelta(seconds=30),
                    "desired_state": {"profile": "Full Backup"},
                }
            ]
        },
        enphase_event,
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "failed",
                    "asset": "enphase",
                    "attempted_at": now - timedelta(seconds=10),
                    "desired_state": {"profile": "Full Backup"},
                }
            ],
            "command_rate_limits": {"enphase:set_profile": now},
        },
        enphase_event,
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        SimpleNamespace(data={"entity_id": "climate.daikin", "new_state": None}),
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now - timedelta(minutes=5),
                    "desired_state": {"hvac_mode": "heat"},
                },
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": "bad",
                },
            ]
        },
        daikin_event,
        now,
    )
    for observed_temperature in (22, "bad"):
        assert not _is_planner_owned_control_feedback(
            entry_data,
            {
                "execution_audit": [
                    {
                        "result": "applied",
                        "asset": "daikin",
                        "attempted_at": now,
                        "desired_state": {"hvac_mode": "heat", "target_temperature": 21},
                    }
                ]
            },
            FakeEvent(
                "climate.daikin",
                "off",
                "heat",
                new_attributes={"temperature": observed_temperature},
            ),
            now,
        )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now - timedelta(seconds=10),
                    "desired_state": {"hvac_mode": "cool"},
                }
            ]
        },
        daikin_event,
        now,
    )


def test_background_ai_cached_skip_and_failure_are_bounded() -> None:
    async def scenario() -> None:
        class TaskHass:
            @staticmethod
            def async_create_task(coro: object) -> asyncio.Task[None]:
                return asyncio.create_task(coro)

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = TaskHass()
        coordinator._ai_advice_task = None
        coordinator._ai_advice_fingerprint = None
        coordinator._last_phase_durations = {}
        coordinator._planner_lock = asyncio.Lock()
        coordinator.async_update_listeners = lambda: None
        plan = _plan("cached")
        fingerprint = _material_plan_fingerprint(plan)
        coordinator.store = FakeStore(
            {"ai_recommendations": [{"status": "accepted", "rejected_detail": {"plan_fingerprint": fingerprint}}]}
        )
        context = SimpleNamespace(created_at=plan.created_at)

        coordinator._schedule_ai_advice(context, plan, {}, {"ai_enabled": True})
        assert coordinator._ai_advice_task is None

        async def skipped(*args: object) -> tuple[AIAdviceResult, bool]:
            return AIAdviceResult("skipped", {}, "rate_limited", None), False

        coordinator._async_get_throttled_ai_advice = skipped
        coordinator._ai_advice_fingerprint = fingerprint
        await coordinator._async_run_ai_advice(context, plan, {}, {"ai_enabled": True}, fingerprint)
        assert coordinator.store.ai_recommendations == []

        async def failed(*args: object) -> tuple[AIAdviceResult, bool]:
            raise RuntimeError("provider failed")

        coordinator._async_get_throttled_ai_advice = failed
        await coordinator._async_run_ai_advice(context, plan, {}, {"ai_enabled": True}, fingerprint)
        assert coordinator.store.ai_recommendations == []

    asyncio.run(scenario())


def test_material_ai_fingerprint_changes_with_forecast_preview_and_cost() -> None:
    first = _plan("generated-1")
    first.preview = [{"import_price": 0.1, "pv_forecast_kw": 1.0}]
    first.estimated_daily_cost = 2.5
    second = _plan("generated-2")
    second.preview = [{"import_price": 0.2, "pv_forecast_kw": 1.0}]
    second.estimated_daily_cost = 2.5

    assert _material_plan_fingerprint(first) != _material_plan_fingerprint(second)
    second.preview = [{"valid_at": "2026-06-27T00:05:00+00:00", "import_price": 0.1, "pv_forecast_kw": 1.0}]
    first.preview = [{"valid_at": "2026-06-27T00:00:00+00:00", "import_price": 0.1, "pv_forecast_kw": 1.0}]
    assert _material_plan_fingerprint(first) == _material_plan_fingerprint(second)


def test_ai_advice_reuses_unchanged_material_plan() -> None:
    plan = _plan("new-generated-id")
    fingerprint = _material_plan_fingerprint(plan)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore(
        {"ai_recommendations": [{"status": "accepted", "rejected_detail": {"plan_fingerprint": fingerprint}}]}
    )

    result, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=plan.created_at), plan, {}, {})
    )

    assert should_store is False
    assert result.rejected_reason == "ai_plan_unchanged"


def test_ai_fingerprint_lookup_and_decision_fingerprint_edges() -> None:
    assert _latest_ai_plan_fingerprint("bad") is None
    assert _latest_ai_plan_fingerprint([{"ignored": True}, "bad"]) is None
    assert _latest_ai_plan_fingerprint(["bad", {"status": "accepted", "plan_fingerprint": "top-level"}]) == "top-level"
    assert (
        _latest_ai_plan_fingerprint([{"status": "accepted", "rejected_detail": {"plan_fingerprint": "nested"}}])
        == "nested"
    )
    hass = FakeHass({"sensor.price": "0.25"})
    present = _decision_input_fingerprint(
        hass,
        {"amber_import_price_entity": "sensor.price"},
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
        [],
        now=datetime(2026, 6, 27, tzinfo=UTC),
    )
    missing = _decision_input_fingerprint(
        FakeHass(),
        {"amber_import_price_entity": "sensor.price"},
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
        [],
        now=datetime(2026, 6, 27, tzinfo=UTC),
    )
    assert present != missing


def test_unchanged_decision_fingerprint_short_circuits_refresh_pipeline() -> None:
    coordinator = _coordinator_for_runtime_services(entry_data={"amber_import_price_entity": "sensor.price"})
    coordinator.data = _plan("existing")
    coordinator._last_decision_fingerprint = _decision_input_fingerprint(
        coordinator.hass,
        coordinator.entry_data,
        coordinator.planner_options,
        coordinator.overrides,
        now=coordinator.data.created_at,
    )
    # Keep the interval bucket stable for this focused short-circuit test.
    original = coordinator_module.dt_util.utcnow
    coordinator_module.dt_util.utcnow = lambda: coordinator.data.created_at
    try:
        result = asyncio.run(coordinator._async_update_data_locked())
    finally:
        coordinator_module.dt_util.utcnow = original

    assert result is coordinator.data
    assert coordinator._refresh_counters["fingerprint_skipped"] == 1


def test_explicit_replan_marks_next_refresh_as_forced() -> None:
    coordinator = _coordinator_for_runtime_services()

    asyncio.run(coordinator.async_request_replan())

    assert coordinator._force_next_refresh is True
    assert coordinator.refresh_requested == 1
    assert coordinator._pending_refresh_trigger == "manual_replan"


def test_latest_ai_service_call_and_state_helpers_cover_edge_cases() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert _latest_ai_service_call_at("bad") is None
    assert _latest_ai_service_call_at([{"created_at": now, "service_called": "ai_task.generate_data"}]) == now
    assert _latest_ai_service_call_at([{"created_at": "bad", "service_called": "ai_task.generate_data"}]) is None
    assert _split_entity_values([" sensor.a ", "bad", "binary_sensor.b"]) == ["sensor.a", "binary_sensor.b"]
    assert _split_entity_values(123) == []
    assert _bool_state_value(FakeHass(), None) is None
    assert _bool_state_value(FakeHass(), "binary_sensor.missing") is None
    assert _bool_state_value(FakeHass({"binary_sensor.yes": "on"}), "binary_sensor.yes") is True
    assert _bool_state_value(FakeHass({"binary_sensor.no": "off"}), "binary_sensor.no") is False
    assert _bool_state_value(FakeHass({"binary_sensor.unknown": "maybe"}), "binary_sensor.unknown") is None
    assert _float_state_value(FakeHass(), None) is None
    assert _float_state_value(FakeHass(), "sensor.missing") is None
    assert _float_state_value(FakeHass({"sensor.bad": "bad"}), "sensor.bad") is None
    assert _float_state_value(FakeHass({"sensor.nan": "nan"}), "sensor.nan") is None
    assert _float_state_value(FakeHass({"sensor.value": "12.5"}), "sensor.value") == 12.5
    assert _parse_datetime_or_none(now) is now
    assert _parse_datetime_or_none(None) is None
    assert _parse_datetime_or_none(123) is None


def test_update_data_locked_records_haeo_ai_snapshot_and_executes(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = SimpleNamespace(
        created_at=now,
        plan_id="plan-refresh",
        slots=[
            DecisionSlot(
                valid_at=now,
                import_price=0.20,
                export_price=0.08,
                pv_forecast_kw=1.0,
                baseline_load_forecast_kw=0.5,
                projected_ev_load_kw=0.0,
            )
        ],
        input_health=InputHealth.HEALTHY,
        haeo_status=HAEOStatus.READY,
        input_issues=[],
        occupancy_state=OccupancyState.OCCUPIED,
    )

    class FakeDiscovery:
        def __init__(self, hass: object, entry_data: dict[str, object]) -> None:
            pass

        def inspect(self) -> SimpleNamespace:
            return SimpleNamespace(as_dict=lambda: {"ok": True})

    class FakeInputManager:
        forecast_training_slots = [{"slot": 1}]

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.forecast_calibration = {}

        def current_forecast_observations(self) -> dict[str, float]:
            return {"pv_forecast_kw": 1.0}

        def thermal_sample(self, built_context: object) -> dict[str, object]:
            assert built_context is context
            return {"sampled_at": now}

        def build_context(self, overrides: list[object]) -> object:
            assert overrides == []
            return context

    class FakeHAEOAdapter:
        def __init__(self, hass: object, service_name: str) -> None:
            self.service_name = service_name
            self.supports_flexible_second_pass = True
            self.second_pass_calls = 0
            self.capabilities = {
                "supports_response": True,
                "supports_flexible_projections": True,
                "supports_flexible_second_pass": True,
                "source": "test",
            }
            self.last_call_metadata: dict[str, object] = {}

        async def async_solve_baseline(self, built_context: object) -> HAEOSolveResult:
            assert built_context is context
            self.last_call_metadata = {
                "duration_ms": 12.5,
                "cache_hit": False,
                "input_fingerprint": "baseline-fingerprint",
                "response_received": True,
                "capabilities": self.capabilities,
            }
            return HAEOSolveResult(
                phase=HAEOSolvePhase.BASELINE,
                status=HAEOStatus.READY,
                reason="haeo_baseline_ready",
                plan_id="plan-refresh",
                service_called="haeo.optimize",
                response={"baseline": True},
            )

        async def async_solve_with_flexible_load(
            self,
            built_context: object,
            projections: list[object],
        ) -> HAEOSolveResult:
            assert built_context is context
            assert projections == ["projection"]
            self.second_pass_calls += 1
            self.last_call_metadata = {
                "duration_ms": 7.25,
                "cache_hit": True,
                "input_fingerprint": "flex-fingerprint",
                "response_received": True,
                "capabilities": self.capabilities,
            }
            return HAEOSolveResult(
                phase=HAEOSolvePhase.FLEXIBLE_LOAD,
                status=HAEOStatus.READY,
                reason="haeo_flexible_ready",
                plan_id="plan-refresh",
                service_called="haeo.optimize",
                response={"flexible": True},
            )

    class FakePlanner:
        initial_projected_loads: list[tuple[float, float]] = []

        def __init__(self, options: dict[str, object], thermal_model: dict[str, object]) -> None:
            assert thermal_model == {"last_sample": {"sampled_at": now}, "enabled": True}

        def create_plan(self, built_context: object) -> EnergyPlan:
            assert built_context is context
            slot = context.slots[0]
            self.initial_projected_loads.append((slot.projected_ev_load_kw, slot.projected_hvac_load_kw))
            slot.projected_ev_load_kw = 7.0
            slot.projected_hvac_load_kw = 1.0
            plan = _plan("plan-refresh")
            plan.created_at = now
            plan.preview = [{"slot": 1}]
            plan.summary = str(getattr(context, "last_haeo_response", None))
            return plan

        def project_flexible_loads(self, built_context: object) -> list[str]:
            assert built_context is context
            return ["projection"]

    class FakeConstraintValidator:
        def __init__(self, options: dict[str, object]) -> None:
            pass

        def validate_plan(self, built_context: object, plan: EnergyPlan) -> list[str]:
            assert built_context is context
            assert plan.plan_id == "plan-refresh"
            return ["input_health_unsafe"]

    async def fake_import_trip_history(
        hass: object,
        entry_data: dict[str, object],
        trip_history: dict[str, object],
        *,
        now: datetime,
    ) -> tuple[dict[str, object], bool, str]:
        return {"records": [{"soc": 80}]}, True, "imported"

    async def fake_ai_advice(
        self: EnergyPlannerCoordinator,
        built_context: object,
        plan: EnergyPlan,
        entry_data: dict[str, object],
        options: dict[str, object],
    ) -> tuple[AIAdviceResult, bool]:
        assert built_context is context
        assert plan.plan_id == "plan-refresh"
        return (
            AIAdviceResult(
                status="accepted",
                accepted={"confidence": 0.8, "reasoning_summary": "Looks good"},
                rejected_reason=None,
                rejected_detail={},
                service_called="ai_task.generate_data",
                ai_task_entity="ai_task.local",
            ),
            True,
        )

    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.CapabilityDiscovery", FakeDiscovery)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_import_ev_trip_history_from_recorder",
        fake_import_trip_history,
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.InputManager", FakeInputManager)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_forecast_calibration",
        lambda model, snapshots, observations, *, now: ({"pv_forecast_kw": {"enabled": True}}, True),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_thermal_model",
        lambda model, previous, sample: ({"last_sample": sample, "enabled": True}, True),
    )

    def fake_apply_haeo_response(built_context: object, response: object) -> dict[str, int]:
        assert built_context is context
        context.last_haeo_response = response
        return {"evidence": len(response or {})}

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.apply_haeo_response_to_context",
        fake_apply_haeo_response,
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.HAEOAdapter", FakeHAEOAdapter)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.DryRunPlanner", FakePlanner)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.ConstraintValidator", FakeConstraintValidator)
    monkeypatch.setattr(EnergyPlannerCoordinator, "_async_get_throttled_ai_advice", fake_ai_advice)

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = FakeEntry({"haeo_optimize_service": "haeo.optimize"}, {"ai_enabled": True})
    coordinator.store = FakeStore({"trip_history": {}, "forecast_snapshots": []})
    coordinator.executor = FakeExecutor()
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0

    result = asyncio.run(coordinator._async_update_data_locked())

    assert result.plan_id == "plan-refresh"
    assert result.summary == "{'flexible': True}"
    assert FakePlanner.initial_projected_loads == [(0.0, 0.0), (0.0, 0.0)]
    assert (context.slots[0].projected_ev_load_kw, context.slots[0].projected_hvac_load_kw) == (7.0, 1.0)
    assert result.status == "unsafe"
    assert result.mode == PlannerMode.ACTIVE_DEGRADED
    assert result.input_issues == ["input_health_unsafe"]
    assert coordinator.store.discovery == [{"ok": True}]
    assert coordinator.store.trip_history == [{"records": [{"soc": 80}]}]
    assert coordinator.store.forecast_calibrations == [{"pv_forecast_kw": {"enabled": True}}]
    assert coordinator.store.thermal_models == [{"last_sample": {"sampled_at": now}, "enabled": True}]
    assert coordinator.store.haeo_runs[0]["flexible_projection_count"] == 1
    assert coordinator.store.haeo_runs[0]["second_pass"]["status"] == HAEOStatus.READY
    assert coordinator.store.haeo_runs[0]["baseline"]["duration_ms"] == 12.5
    assert coordinator.store.haeo_runs[0]["baseline"]["evidence_status"] == "available"
    assert coordinator.store.haeo_runs[0]["second_pass"]["duration_ms"] == 7.25
    assert coordinator.store.haeo_runs[0]["second_pass"]["cache_hit"] is True
    assert coordinator.store.haeo_runs[0]["capabilities"]["supports_flexible_second_pass"] is True
    assert coordinator.store.ai_recommendations == []
    assert coordinator.store.forecast_snapshots[0]["ai"] is None
    assert coordinator.store.forecast_snapshots[0]["trip_history"]["recorder_import_reason"] == "imported"
    assert coordinator.store.saved_plans == [result]
    assert coordinator.executor.evaluated == [(result, context)]

    adapter = coordinator._haeo_adapter
    adapter.supports_flexible_second_pass = False
    adapter.capabilities["supports_flexible_second_pass"] = False
    context.slots[0].projected_ev_load_kw = 0.0
    context.slots[0].projected_hvac_load_kw = 0.0
    coordinator.store = FakeStore({"trip_history": {}, "forecast_snapshots": []})
    coordinator.executor = FakeExecutor()

    asyncio.run(coordinator._async_update_data_locked())

    assert adapter.second_pass_calls == 1
    assert coordinator.store.haeo_runs[0]["second_pass"]["status"] == "skipped"
    assert coordinator.store.haeo_runs[0]["second_pass"]["reason"] == "haeo_flexible_projection_unsupported"
    assert coordinator.store.haeo_runs[0]["second_pass"]["duration_ms"] == 0.0


def test_update_data_locked_does_not_record_successful_haeo_baseline_as_issue(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = SimpleNamespace(
        created_at=now,
        plan_id="plan-no-flex",
        slots=[
            DecisionSlot(
                valid_at=now,
                import_price=0.20,
                export_price=0.08,
                pv_forecast_kw=1.0,
                baseline_load_forecast_kw=0.5,
            )
        ],
        input_health=InputHealth.HEALTHY,
        haeo_status=HAEOStatus.READY,
        input_issues=[],
        occupancy_state=OccupancyState.OCCUPIED,
    )

    class FakeDiscovery:
        def __init__(self, hass: object, entry_data: dict[str, object]) -> None:
            pass

        def inspect(self) -> SimpleNamespace:
            return SimpleNamespace(as_dict=lambda: {"ok": True})

    class FakeInputManager:
        forecast_training_slots: list[dict[str, object]] = []
        forecast_confidence_details: list[dict[str, object]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.forecast_calibration = {}

        def current_forecast_observations(self) -> dict[str, float]:
            return {}

        def thermal_sample(self, built_context: object) -> dict[str, object]:
            assert built_context is context
            return {}

        def build_context(self, overrides: list[object]) -> object:
            assert overrides == []
            return context

    class FakeHAEOAdapter:
        def __init__(self, hass: object, service_name: str) -> None:
            pass

        async def async_solve_baseline(self, built_context: object) -> HAEOSolveResult:
            assert built_context is context
            return HAEOSolveResult(
                phase=HAEOSolvePhase.BASELINE,
                status=HAEOStatus.READY,
                reason="haeo_service_called",
                plan_id="plan-no-flex",
                service_called="haeo.optimize",
                response={},
            )

        async def async_solve_with_flexible_load(
            self,
            built_context: object,
            projections: list[object],
        ) -> HAEOSolveResult:
            raise AssertionError("second HAEO pass should not run without projections")

    class FakePlanner:
        def __init__(self, options: dict[str, object], thermal_model: dict[str, object]) -> None:
            pass

        def create_plan(self, built_context: object) -> EnergyPlan:
            assert built_context is context
            plan = _plan("plan-no-flex")
            plan.created_at = now
            return plan

        def project_flexible_loads(self, built_context: object) -> list[object]:
            assert built_context is context
            return []

    class FakeConstraintValidator:
        def __init__(self, options: dict[str, object]) -> None:
            pass

        def validate_plan(self, built_context: object, plan: EnergyPlan) -> list[str]:
            assert built_context is context
            assert plan.plan_id == "plan-no-flex"
            return []

    async def fake_import_trip_history(
        hass: object,
        entry_data: dict[str, object],
        trip_history: dict[str, object],
        *,
        now: datetime,
    ) -> tuple[dict[str, object], bool, str]:
        return trip_history, False, "unchanged"

    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.CapabilityDiscovery", FakeDiscovery)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_import_ev_trip_history_from_recorder",
        fake_import_trip_history,
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.InputManager", FakeInputManager)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_forecast_calibration",
        lambda model, snapshots, observations, *, now: (model, False),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_thermal_model",
        lambda model, previous, sample: (model, False),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.apply_haeo_response_to_context",
        lambda built_context, response: {},
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.HAEOAdapter", FakeHAEOAdapter)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.DryRunPlanner", FakePlanner)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.ConstraintValidator", FakeConstraintValidator)

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = FakeEntry(
        {"haeo_optimize_service": "haeo.optimize"},
        {
            "ai_enabled": False,
            CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED: False,
        },
    )
    coordinator.store = FakeStore({"trip_history": {}, "forecast_snapshots": []})
    coordinator.executor = FakeExecutor()
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0

    result = asyncio.run(coordinator._async_update_data_locked())

    assert result.input_issues == []
    assert coordinator.store.haeo_runs[0]["flexible_projection_count"] == 0
    assert coordinator.store.haeo_runs[0]["second_pass"] is None
    assert coordinator.executor.fallback == (result, [])
    assert coordinator.executor.fallback_options[CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED] is False


def test_update_data_locked_records_dry_run_comparison(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = SimpleNamespace(
        created_at=now,
        plan_id="plan-dry",
        slots=[DecisionSlot(now, 0.2, 0.05, 0, 1)],
        input_health=InputHealth.HEALTHY,
        haeo_status=HAEOStatus.READY,
        input_issues=[],
        occupancy_state=OccupancyState.OCCUPIED,
    )

    class FakeHAEOAdapter:
        def __init__(self, hass: object, service_name: str) -> None:
            pass

        async def async_solve_baseline(self, built_context: object) -> HAEOSolveResult:
            return HAEOSolveResult(HAEOSolvePhase.BASELINE, HAEOStatus.READY, "baseline", "plan-dry")

        async def async_solve_with_flexible_load(
            self,
            built_context: object,
            projections: list[object],
        ) -> HAEOSolveResult:
            return HAEOSolveResult(HAEOSolvePhase.FLEXIBLE_LOAD, HAEOStatus.READY, "flexible", "plan-dry")

    class FakePlanner:
        def __init__(self, options: dict[str, object], thermal_model: dict[str, object]) -> None:
            pass

        def create_plan(self, built_context: object) -> EnergyPlan:
            plan = _plan("plan-dry")
            plan.created_at = now
            plan.mode = PlannerMode.DRY_RUN
            return plan

        def project_flexible_loads(self, built_context: object) -> list[str]:
            return []

    async def fake_import_trip_history(
        hass: object,
        data: dict[str, object],
        history: dict[str, object],
        *,
        now: datetime,
    ) -> tuple[dict[str, object], bool, str]:
        return history, False, "unchanged"

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.CapabilityDiscovery",
        lambda hass, data: SimpleNamespace(inspect=lambda: SimpleNamespace(as_dict=lambda: {})),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.InputManager",
        lambda *args, **kwargs: SimpleNamespace(
            current_forecast_observations=lambda: {},
            build_context=lambda overrides: context,
            thermal_sample=lambda built_context: {},
            forecast_training_slots=[],
            forecast_calibration={},
        ),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_import_ev_trip_history_from_recorder",
        fake_import_trip_history,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_forecast_calibration",
        lambda *args, **kwargs: ({}, False),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_thermal_model",
        lambda *args, **kwargs: ({}, False),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.apply_haeo_response_to_context",
        lambda built_context, response: {},
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.HAEOAdapter", FakeHAEOAdapter)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.DryRunPlanner", FakePlanner)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.ConstraintValidator",
        lambda options: SimpleNamespace(validate_plan=lambda built_context, plan: []),
    )

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = FakeEntry({}, {"ai_enabled": False})
    coordinator.store = FakeStore({"trip_history": {}, "forecast_snapshots": []})
    coordinator.executor = FakeExecutor()
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0

    result = asyncio.run(coordinator._async_update_data_locked())

    assert result.mode == PlannerMode.DRY_RUN
    assert coordinator.store.dry_run_comparisons[0]["plan_id"] == "plan-dry"


def test_snapshot_actions_are_bounded_and_auditable() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = PlanAction(
        action_id="plan-1-ev-minimum-soc",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "target_soc_percent": 80,
            "ready_by": "07:00",
            "allocated_slots": [
                {
                    "valid_at": (now + timedelta(minutes=5 * index)).isoformat(),
                    "charge_kw": 7.0,
                }
                for index in range(20)
            ],
        },
        hard_constraints=["ev_min_soc", "ready_by"],
        reason_codes=["ev_soc_below_target", "fallback_target"],
        expected_cost_delta=None,
        confidence=0.93,
        requires_haeo_plan_id="plan-1",
    )
    plan = _plan("plan-1")
    plan.actions = [action]

    snapshot = _snapshot_actions(plan)

    assert snapshot == [
        {
            "action_id": "plan-1-ev-minimum-soc",
            "asset": "ev",
            "kind": "ev_schedule",
            "execute_not_before": "2026-06-27T00:00:00+00:00",
            "execute_not_after": "2026-06-27T00:05:00+00:00",
            "desired_state": {
                "target_soc_percent": 80,
                "ready_by": "07:00",
                "allocated_slots": [
                    {
                        "valid_at": "2026-06-27T00:00:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:05:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:10:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:15:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:20:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:25:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:30:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:35:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:40:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:45:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:50:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:55:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {"truncated_count": 8},
                ],
            },
            "hard_constraints": ["ev_min_soc", "ready_by"],
            "reason_codes": ["ev_soc_below_target", "fallback_target"],
            "expected_cost_delta": None,
            "confidence": 0.93,
            "requires_haeo_plan_id": "plan-1",
        }
    ]


def test_restore_safe_state_refreshes_by_default() -> None:
    coordinator = _coordinator_for_restore()

    asyncio.run(coordinator.async_restore_safe_state("manual_service_call"))

    assert coordinator.executor.restored == ["manual_service_call"]
    assert coordinator.refresh_requested == 1
    assert coordinator._refresh_generation == 1


def test_restore_safe_state_can_skip_refresh_for_teardown() -> None:
    coordinator = _coordinator_for_restore()

    asyncio.run(coordinator.async_restore_safe_state("entry_unload", refresh=False))

    assert coordinator.executor.restored == ["entry_unload"]
    assert coordinator.refresh_requested == 0
    assert coordinator._refresh_generation == 0


def test_request_replan_and_ready_by_mark_generation_and_refresh() -> None:
    coordinator = _coordinator_for_runtime_services()

    asyncio.run(coordinator.async_request_replan())
    asyncio.run(coordinator.async_set_ready_by("09:15"))

    assert coordinator.ready_by == "09:15"
    assert coordinator.refresh_requested == 2
    assert coordinator._refresh_generation == 2
    assert coordinator._pending_refresh_trigger == "ready_by_changed"


def test_set_ready_by_updates_configured_ev_helper(monkeypatch: object) -> None:
    calls: list[tuple[object, dict[str, object], str]] = []

    class FakeEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, object]) -> None:
            self.hass = hass
            self.entry_data = entry_data

        async def async_set_ready_by(self, ready_by: str) -> None:
            calls.append((self.hass, self.entry_data, ready_by))

    monkeypatch.setattr(coordinator_module, "EVSmartChargingAdapter", FakeEVAdapter)
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by"}
    )

    asyncio.run(coordinator.async_set_ready_by("23:45"))

    assert coordinator.ready_by == "23:45"
    assert coordinator.refresh_requested == 1
    assert calls == [(coordinator.hass, coordinator.entry_data, "23:45")]


def test_manual_hvac_override_replaces_existing_override_and_turns_on_helper() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )
    coordinator.overrides = [
        SimpleNamespace(kind="manual_hvac", reason="old"),
        SimpleNamespace(kind="other", reason="kept"),
    ]

    asyncio.run(coordinator.async_set_manual_hvac_override(15, "user_change"))

    assert [override.kind for override in coordinator.overrides] == ["other", "manual_hvac"]
    assert coordinator.overrides[-1].reason == "user_change"
    assert coordinator.store.data["overrides"] == coordinator.overrides
    assert "manual_hvac_override_expires_at" in coordinator.store.data["ownership"]
    assert coordinator.hass.services.calls == [
        (
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.manual_override"},
            True,
        )
    ]
    assert coordinator.refresh_requested == 1
    assert coordinator._refresh_generation == 1


def test_manual_hvac_change_handler_uses_configured_duration() -> None:
    coordinator = _coordinator_for_runtime_services(options={"manual_hvac_override_minutes": 45})

    asyncio.run(coordinator._async_handle_manual_hvac_change("daikin_state_changed"))

    assert coordinator.overrides[-1].reason == "daikin_state_changed"
    assert coordinator.refresh_requested == 1


def test_record_ev_trip_event_saves_when_values_change() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SOC: "sensor.ev_soc",
        },
        hass=FakeHass({"binary_sensor.ev_connected": "on", "sensor.ev_soc": "72"}),
        store_data={
            "trip_history": {
                "active_trip": {
                    "started_at": "2026-06-27T00:00:00+00:00",
                    "start_soc_percent": 80,
                }
            }
        },
    )

    asyncio.run(coordinator._async_record_ev_trip_event())

    assert "records" in coordinator.store.data["trip_history"]


def test_production_control_runtime_methods_update_store_and_refresh() -> None:
    coordinator = _coordinator_for_runtime_services(store_data={"production": {"dry_run_ready_cycles": 3}})

    asyncio.run(coordinator.async_arm_production_control("operator_ack"))
    asyncio.run(coordinator.async_disarm_production_control("operator_stop"))
    asyncio.run(coordinator.async_pause_control(30, "maintenance", "ev"))
    asyncio.run(coordinator.async_resume_control("maintenance_done"))

    assert coordinator.store.production_saves[0]["armed"] is True
    assert coordinator.store.production_saves[0]["armed_reason"] == "operator_ack"
    assert coordinator.store.production_saves[1]["armed"] is False
    assert coordinator.store.production_saves[1]["disarmed_reason"] == "operator_stop"
    assert coordinator.store.control_pause_saves[0]["assets"] == ["ev"]
    assert coordinator.store.control_pause_saves[0]["reason"] == "maintenance"
    assert coordinator.store.control_pause_saves[1]["active"] is False
    assert coordinator.store.control_pause_saves[1]["reason"] == "maintenance_done"
    assert coordinator.refresh_requested == 2
    assert coordinator._refresh_generation == 2
    assert coordinator._pending_refresh_trigger == "control_resumed"


def test_production_pause_fallback_persistence_handles_lightweight_stores() -> None:
    coordinator = _coordinator_for_runtime_services()
    coordinator.store = type("Store", (), {"data": {}})()

    asyncio.run(coordinator.async_arm_production_control("ack"))
    asyncio.run(coordinator.async_pause_control(10, "pause", "invalid"))
    asyncio.run(coordinator._async_record_dry_run_comparison(_plan("plan-1")))

    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.store.data["control_pause"]["assets"] == ["all"]
    assert coordinator.store.data["dry_run_comparisons"][0]["plan_id"] == "plan-1"


def test_production_evidence_and_dry_run_comparison_are_recorded() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    coordinator = _coordinator_for_runtime_services(
        store_data={"execution_audit": [{"result": "applied", "action_id": "previous"}]}
    )
    dry_run = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=1.23,
        actions=[action],
        preview=[],
    )
    unsafe = _plan("unsafe")
    unsafe.health = InputHealth.UNSAFE

    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    asyncio.run(coordinator._async_record_dry_run_comparison(dry_run))
    asyncio.run(coordinator._async_update_production_evidence(unsafe, []))

    assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1
    assert coordinator.store.data["production"]["last_blocking_reason"] == "input_health_unsafe"
    comparison = coordinator.store.dry_run_comparisons[0]
    assert comparison["planned_action_count"] == 1
    assert comparison["next_action"]["action_id"] == "ev"
    assert comparison["recent_outcome_count"] == 1


def test_production_evidence_resets_when_control_contract_changes() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
        options={"ev_control_enabled": True},
    )
    dry_run = _plan("dry-run")
    dry_run.mode = PlannerMode.DRY_RUN

    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    first_fingerprint = coordinator.store.data["production"]["dry_run_evidence_fingerprint"]
    coordinator.entry.data["enphase_profile_entity"] = "select.enphase"
    coordinator.entry.options["enphase_control_enabled"] = True
    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))

    assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1
    assert coordinator.store.data["production"]["dry_run_evidence_fingerprint"] != first_fingerprint


def test_production_evidence_rejects_malformed_counters_and_saturates() -> None:
    dry_run = _plan("dry-run")
    dry_run.mode = PlannerMode.DRY_RUN

    for corrupt in ("3", True, 3.0, -1, 10_001):
        coordinator = _coordinator_for_runtime_services(
            store_data={"production": {"dry_run_ready_cycles": corrupt}}
        )
        asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
        assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1

    coordinator = _coordinator_for_runtime_services()
    for _index in range(5):
        asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 3


def test_runtime_ready_by_does_not_change_production_evidence_contract() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_DEFAULT_READY_BY: "07:00", "ev_control_enabled": True},
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
    )
    dry_run = _plan("dry-run")
    dry_run.mode = PlannerMode.DRY_RUN

    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    fingerprint = coordinator.store.data["production"]["dry_run_evidence_fingerprint"]
    coordinator.ready_by = "23:45"

    assert production_evidence_fingerprint(coordinator.entry_data, coordinator.planner_options) == fingerprint


def test_shutdown_cancels_pending_callbacks_and_listeners() -> None:
    calls: list[str] = []
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._debounce_cancel = lambda: calls.append("debounce")
    coordinator._boundary_cancel = lambda: calls.append("boundary")
    coordinator._unsub_listeners = [
        lambda: calls.append("listener_1"),
        lambda: calls.append("listener_2"),
    ]

    coordinator.async_shutdown()

    assert calls == ["debounce", "boundary", "listener_2", "listener_1"]
    assert coordinator._debounce_cancel is None
    assert coordinator._boundary_cancel is None
    assert coordinator._unsub_listeners == []


def test_debounced_and_boundary_refresh_callbacks_schedule_refresh(monkeypatch: object) -> None:
    scheduled: list[tuple[float, object]] = []

    def fake_async_call_later(hass: object, delay: float, action: object) -> object:
        scheduled.append((delay, action))
        return lambda: scheduled.append((-1, "cancelled"))

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_async_call_later,
    )
    coordinator = _coordinator_for_runtime_services()
    coordinator._debounce_cancel = lambda: scheduled.append((-2, "old_debounce_cancelled"))
    coordinator._boundary_cancel = lambda: scheduled.append((-3, "old_boundary_cancelled"))

    coordinator._schedule_debounced_refresh()
    debounce_callback = scheduled[-1][1]
    debounce_callback(None)
    coordinator._schedule_next_boundary_refresh()
    boundary_callback = scheduled[-1][1]
    boundary_callback(None)
    # Boundary scheduling bypasses the 20-second state debounce but still uses
    # the coalescing/minimum-interval callback.
    scheduled[-2][1](None)

    assert coordinator._refresh_generation == 2
    assert len(coordinator.hass.created_tasks) == 2
    assert scheduled[0] == (-2, "old_debounce_cancelled")
    assert scheduled[2] == (-3, "old_boundary_cancelled")


def _coordinator_for_commit(previous: EnergyPlan | None, *, current_generation: int) -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore()
    coordinator.executor = FakeExecutor()
    coordinator.entry = FakeEntry({"battery_soc_entity": "sensor.battery"})
    coordinator.data = previous
    coordinator._refresh_generation = current_generation
    return coordinator


def _coordinator_for_restore() -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.executor = FakeExecutor()
    coordinator._refresh_generation = 0
    coordinator.refresh_requested = 0

    async def request_refresh() -> None:
        coordinator.refresh_requested += 1

    coordinator.async_request_refresh = request_refresh
    return coordinator


def _coordinator_for_runtime_services(
    *,
    entry_data: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
    hass: FakeHass | None = None,
    store_data: dict[str, object] | None = None,
) -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = hass or FakeHass()
    coordinator.entry = FakeEntry(entry_data or {}, options or {})
    coordinator.store = FakeStore(store_data or {})
    coordinator.executor = FakeExecutor()
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0
    coordinator._listeners = {}
    coordinator.refresh_requested = 0
    coordinator._debounce_cancel = None
    coordinator._boundary_cancel = None
    coordinator._unsub_listeners = []

    async def request_refresh() -> None:
        coordinator.refresh_requested += 1

    coordinator.async_request_refresh = request_refresh
    return coordinator


def _plan(plan_id: str) -> EnergyPlan:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return EnergyPlan(
        plan_id=plan_id,
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
