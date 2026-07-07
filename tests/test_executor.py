"""Tests for executor capability gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from custom_components.ha_energy_planner import executor as executor_module
from custom_components.ha_energy_planner.const import (
    CONF_COMMAND_RATE_LIMIT_SECONDS,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_MAX_DAILY_CLIMATE_ACTIONS,
    CONF_MAX_DAILY_ENPHASE_ACTIONS,
    CONF_MAX_DAILY_EV_ACTIONS,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.executor import (
    Executor,
    _clean_reason_codes,
    _daily_action_cap_reason,
    _device_control_disabled_reason,
    _pause_rejection_reason,
    _plan_fallback_message,
    _profile_control_service_for_target,
    _restore_notification_message,
    _service_target_for_action,
)
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    HAEOStatus,
    InputHealth,
    OccupancyState,
    PlanAction,
    PlannerMode,
)


class FakeStore:
    """Minimal planner store."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"ownership": {}, "outcomes": []}

    async def async_add_outcome(self, outcome: Any) -> None:
        self.data["outcomes"].append(outcome)

    async def async_save_ownership(self, ownership: dict[str, Any]) -> None:
        self.data["ownership"] = ownership

    async def async_save_command_rate_limits(self, limits: dict[str, Any]) -> None:
        self.data["command_rate_limits"] = limits

    async def async_save_control_pause(self, pause: dict[str, Any]) -> None:
        self.data["control_pause"] = pause

    async def async_clear_ownership(self) -> None:
        self.data["ownership"] = {}


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str


class FakeStates:
    """Minimal state registry."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeServices:
    """Minimal service registry."""

    def __init__(self, states: FakeStates) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return True

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        entity_id = data.get("entity_id")
        if entity_id and service == "turn_on":
            self.states.values[entity_id] = "on"
        elif entity_id and service == "turn_off":
            self.states.values[entity_id] = "off"
        elif entity_id and service == "select_option" and "option" in data:
            self.states.values[entity_id] = str(data["option"])


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states)


def _context(now: datetime) -> DecisionContext:
    return DecisionContext(
        created_at=now,
        plan_id="plan-1",
        slots=[DecisionSlot(now, 0.1, 0.05, 0, 1)],
        current_battery_soc_percent=50,
        current_ev_soc_percent=40,
        occupancy_state=OccupancyState.OCCUPIED,
        haeo_status=HAEOStatus.READY,
        input_health=InputHealth.HEALTHY,
    )


def test_executor_loads_hvac_takeover_timestamp_from_store() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    store = FakeStore()
    store.data["ownership"]["planner_takeover_started_at"] = now.isoformat()

    ownership = Executor(store)._ownership_from_store()

    assert ownership.planner_takeover_started_at == now


def test_executor_ignores_malformed_ownership_timestamps_from_store() -> None:
    store = FakeStore()
    store.data["ownership"] = {
        "enphase_profile_changed_at": "not-a-date",
        "planner_takeover_started_at": "also-not-a-date",
        "manual_hvac_override_expires_at": "still-not-a-date",
    }

    ownership = Executor(store)._ownership_from_store()

    assert ownership.enphase_profile_changed_at is None
    assert ownership.planner_takeover_started_at is None
    assert ownership.manual_hvac_override_expires_at is None


def test_executor_rejects_ev_action_when_discovery_fails() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(
        store,
        hass=hass,
        entry_data={"ev_smart_charging_start_entity": "switch.ev_start"},
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
    )
    asyncio.run(executor.async_evaluate(plan, _context(now)))
    assert store.data["outcomes"][0].result == "rejected"
    assert store.data["outcomes"][0].reason == "ev_start_control_unavailable,ev_stop_control_not_configured"
    assert hass.services.calls == []


def test_restore_safe_state_creates_persistent_notification() -> None:
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(store, hass=hass)

    outcome = asyncio.run(executor.async_restore_safe_state("test_restore_reason"))

    assert outcome.result == "restored"
    assert store.data["ownership"] == {}
    assert hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner restored safe state",
                "message": (
                    "Planner-owned EV, Enphase, and Daikin controls were restored where supported. "
                    "Reason: test_restore_reason:enphase_ai_profile_not_configured."
                ),
                "notification_id": "ha_energy_planner_restore_safe_state",
            },
        )
    ]


def test_infeasible_ev_schedule_creates_persistent_notification_before_rejection() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"target_soc_percent": 65, "ready_by": "07:00", "infeasible": True},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(
        store,
        hass=hass,
        entry_data={"ev_smart_charging_start_entity": "switch.ev_start"},
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner EV target infeasible",
                "message": (
                    "The EV cannot reach the requested ready-by target with the current schedule. "
                    "Planned target: 65%. Ready by: 07:00."
                ),
                "notification_id": "ha_energy_planner_ev_infeasible_plan-1",
            },
        )
    ]
    assert store.data["outcomes"][0].result == "rejected"


def test_plan_fallback_notification_reports_unsafe_and_grid_limit_classes() -> None:
    now = datetime.now(UTC)
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="unsafe",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="test",
        confidence=0.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
        input_issues=["input_health_unsafe", "grid_import_limit_exceeded"],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(store, hass=hass)

    asyncio.run(
        executor.async_notify_plan_fallback(
            plan,
            ["input_health_unsafe", "grid_import_limit_exceeded"],
        )
    )

    assert hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner plan unsafe",
                "message": (
                    "Required inputs are stale, missing, or invalid. Device control remains blocked. "
                    "Plan status: unsafe. Mode: ACTIVE_DEGRADED. "
                    "Reason codes: input_health_unsafe, grid_import_limit_exceeded."
                ),
                "notification_id": "ha_energy_planner_plan_unsafe",
            },
        ),
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner grid limit fallback",
                "message": (
                    "The current plan would exceed a configured grid import/export hard limit. "
                    "Plan status: unsafe. Mode: ACTIVE_DEGRADED. "
                    "Reason codes: grid_import_limit_exceeded."
                ),
                "notification_id": "ha_energy_planner_grid_limit_fallback",
            },
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_haeo_fallback"},
        ),
    ]


def test_plan_fallback_notification_dismisses_during_startup_grace() -> None:
    now = datetime.now(UTC)
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="unsafe",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="test",
        confidence=0.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
        input_issues=["input_health_unsafe", "haeo_service_failed:ServiceValidationError"],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(store, hass=hass, notification_grace_until=now + timedelta(minutes=5))

    asyncio.run(executor.async_notify_plan_fallback(plan, ["input_health_unsafe"]))

    assert hass.services.calls == [
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_plan_unsafe"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_grid_limit_fallback"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_haeo_fallback"},
        ),
    ]


def test_plan_fallback_notification_reports_haeo_issue_without_plan_violation() -> None:
    now = datetime.now(UTC)
    plan = EnergyPlan(
        plan_id="plan-1",
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
        input_issues=["haeo_service_unavailable"],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(store, hass=hass)

    asyncio.run(executor.async_notify_plan_fallback(plan, []))

    assert hass.services.calls == [
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_plan_unsafe"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_grid_limit_fallback"},
        ),
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner HAEO fallback",
                "message": (
                    "HAEO did not return a healthy optimization result. "
                    "The deterministic fallback remains constrained. "
                    "Plan status: current. Mode: ACTIVE_HEALTHY. "
                    "Reason codes: haeo_service_unavailable."
                ),
                "notification_id": "ha_energy_planner_haeo_fallback",
            },
        ),
    ]


def test_plan_fallback_notification_ignores_successful_haeo_call_reason() -> None:
    now = datetime.now(UTC)
    plan = EnergyPlan(
        plan_id="plan-1",
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
        input_issues=["haeo_service_called"],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(store, hass=hass)

    asyncio.run(executor.async_notify_plan_fallback(plan, []))

    assert hass.services.calls[-1] == (
        "persistent_notification",
        "dismiss",
        {"notification_id": "ha_energy_planner_haeo_fallback"},
    )


def test_plan_fallback_notifications_are_dismissed_when_planner_disabled() -> None:
    now = datetime.now(UTC)
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="unsafe",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.DISABLED,
        summary="test",
        confidence=0.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
        input_issues=["input_health_unsafe", "haeo_service_failed:ServiceValidationError"],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(store, hass=hass)

    asyncio.run(executor.async_notify_plan_fallback(plan, ["input_health_unsafe"]))

    assert hass.services.calls == [
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_plan_unsafe"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_grid_limit_fallback"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_haeo_fallback"},
        ),
    ]


def test_executor_preserves_first_ev_pre_takeover_state() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    hass = FakeHass({"input_boolean.ev_start": "off", "input_boolean.ev_stop": "on"})
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))
    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["ownership"]["ev_smart_charging_state"][CONF_EV_SMART_CHARGING_START] == "off"
    assert hass.states.values["input_boolean.ev_start"] == "on"


def test_executor_rate_limits_repeated_device_command() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    store.data["command_rate_limits"] = {"ev:ev_start": now.isoformat()}
    hass = FakeHass({"input_boolean.ev_start": "off", "input_boolean.ev_stop": "on"})
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_COMMAND_RATE_LIMIT_SECONDS: 3600,
        },
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["outcomes"][0].result == "rejected"
    assert store.data["outcomes"][0].reason == "device_command_rate_limited"
    assert hass.services.calls == []
    assert hass.states.values["input_boolean.ev_start"] == "off"


def test_executor_detects_recent_ev_external_conflict_and_pauses_control() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    store = FakeStore()
    store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "ev",
            "result": "applied",
            "post_state": {"input_boolean.ev_start": "on"},
        }
    ]
    executor = Executor(
        store,
        hass=FakeHass({"input_boolean.ev_start": "off"}),
        entry_data={CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start"},
    )

    assert executor._observed_conflict_reason(action, now) == "external_ev_charging_conflict"
    asyncio.run(
        executor._async_pause_asset_control(
            ActionAsset.EV,
            now,
            "external_ev_charging_conflict",
            timedelta(minutes=2),
        )
    )
    assert store.data["control_pause"]["assets"] == ["ev"]
    assert store.data["control_pause"]["reason"] == "external_ev_charging_conflict"


def test_executor_detects_recent_enphase_external_conflict() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="enphase",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.ENPHASE,
        kind=ActionKind.SET_PROFILE,
        desired_state={"profile": "Self-Consumption"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=1.0,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    store = FakeStore()
    store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "enphase",
            "result": "applied",
            "post_state": {"profile": "Self-Consumption"},
        }
    ]
    executor = Executor(
        store,
        hass=FakeHass({"input_select.enphase_profile": "AI Optimisation"}),
        entry_data={
            CONF_ENPHASE_PROFILE: "input_select.enphase_profile",
            CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "input_select.select_option",
        },
    )

    assert executor._observed_conflict_reason(action, now) == "external_enphase_profile_conflict"


def test_executor_conflict_helpers_cover_defensive_branches() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now,
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    store_without_pause_method = SimpleNamespace(data={})
    asyncio.run(
        Executor(store_without_pause_method)._async_pause_asset_control(
            ActionAsset.EV,
            now,
            "pause_reason",
            timedelta(minutes=1),
        )
    )
    assert store_without_pause_method.data["control_pause"]["reason"] == "pause_reason"

    assert executor_module._entity_id_from_service_target(None) is None
    assert executor_module._latest_applied_audit_for_asset("bad", ActionAsset.EV, now) is None
    assert (
        executor_module._latest_applied_audit_for_asset(
            [
                "bad",
                {"asset": "climate", "result": "applied", "attempted_at": now.isoformat()},
                {"asset": "ev", "result": "rejected", "attempted_at": now.isoformat()},
                {"asset": "ev", "result": "applied", "attempted_at": "not-a-date"},
                {
                    "asset": "ev",
                    "result": "applied",
                    "attempted_at": (now - timedelta(minutes=10)).isoformat(),
                },
            ],
            ActionAsset.EV,
            now,
        )
        is None
    )

    no_target = Executor(FakeStore(), hass=FakeHass({"input_boolean.ev_start": "off"}), entry_data={})
    no_target.store.data["execution_audit"] = [
        {"attempted_at": now.isoformat(), "asset": "ev", "result": "applied", "post_state": {}}
    ]
    assert no_target._observed_conflict_reason(action, now) is None
    no_state = Executor(
        FakeStore(),
        hass=FakeHass({}),
        entry_data={CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start"},
    )
    no_state.store.data["execution_audit"] = [
        {"attempted_at": now.isoformat(), "asset": "ev", "result": "applied", "post_state": {}}
    ]
    assert no_state._observed_conflict_reason(action, now) is None
    no_conflict = Executor(
        FakeStore(),
        hass=FakeHass({"input_boolean.ev_start": "on"}),
        entry_data={CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start"},
    )
    no_conflict.store.data["execution_audit"] = [
        {"attempted_at": now.isoformat(), "asset": "ev", "result": "applied", "post_state": {}}
    ]
    assert no_conflict._observed_conflict_reason(action, now) is None


def test_executor_rejects_and_pauses_on_observed_conflict() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        "plan-1",
        now,
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "test",
        1.0,
        None,
        [action],
        [],
    )
    store = FakeStore()
    store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "ev",
            "result": "applied",
            "post_state": {"input_boolean.ev_start": "on"},
        }
    ]
    hass = FakeHass({"input_boolean.ev_start": "off", "input_boolean.ev_stop": "on"})
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["outcomes"][0].result == "rejected"
    assert store.data["outcomes"][0].reason == "external_ev_charging_conflict"
    assert store.data["control_pause"]["assets"] == ["ev"]


def test_executor_pauses_failed_adapter_results(monkeypatch: Any) -> None:
    now = datetime.now(UTC)

    class FailedEVAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_execute(self, action: Any) -> Any:
            return SimpleNamespace(applied=False, reason="ev_failed", pre_state={}, post_state={})

    class FailedHVACAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_execute(self, action: Any) -> Any:
            return SimpleNamespace(
                applied=False,
                reason="hvac_failed",
                pre_state={},
                post_state={},
                saved_automation_states={},
            )

    class FailedEnphaseAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_execute(self, action: Any) -> Any:
            return SimpleNamespace(
                applied=False,
                reason="enphase_failed",
                pre_state={},
                post_state={},
                saved_profile=None,
                changed_profile_at=False,
            )

    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", FailedEVAdapter)
    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FailedHVACAdapter)
    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", FailedEnphaseAdapter)

    class SupportedDiscovery:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def inspect(self) -> SupportedDiscovery:
            return self

        def for_asset(self, asset: Any) -> Any:
            return SimpleNamespace(supported=True, issues=[])

    monkeypatch.setattr(executor_module, "CapabilityDiscovery", SupportedDiscovery)

    cases = [
        (
            PlanAction(
                "ev",
                "plan-1",
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
            ),
            {
                "ev_smart_charging_start_entity": "input_boolean.ev_start",
                "ev_smart_charging_stop_entity": "input_boolean.ev_stop",
            },
            {"input_boolean.ev_start": "off", "input_boolean.ev_stop": "on"},
            "ev_failed",
        ),
        (
            PlanAction(
                "climate",
                "plan-1",
                now - timedelta(minutes=1),
                now + timedelta(minutes=1),
                ActionAsset.DAIKIN,
                ActionKind.SET_HVAC,
                {"hvac_mode": "off"},
                [],
                [],
                None,
                1.0,
                None,
            ),
            {"daikin_climate_entity": "climate.daikin"},
            {"climate.daikin": "heat"},
            "hvac_failed",
        ),
        (
            PlanAction(
                "enphase",
                "plan-1",
                now - timedelta(minutes=1),
                now + timedelta(minutes=1),
                ActionAsset.ENPHASE,
                ActionKind.SET_PROFILE,
                {"profile": "Self-Consumption"},
                [],
                [],
                1.0,
                1.0,
                None,
            ),
            {
                "enphase_profile_entity": "input_select.enphase",
                "enphase_profile_control_service": "input_select.select_option",
            },
            {"input_select.enphase": "AI Optimisation"},
            "enphase_failed",
        ),
    ]
    for action, entry_data, states, reason in cases:
        store = FakeStore()
        plan = EnergyPlan(
            "plan-1",
            now,
            24,
            5,
            "current",
            InputHealth.HEALTHY,
            PlannerMode.ACTIVE_HEALTHY,
            "test",
            1.0,
            None,
            [action],
            [],
        )
        asyncio.run(Executor(store, hass=FakeHass(states), entry_data=entry_data).async_evaluate(plan, _context(now)))
        assert store.data["control_pause"]["reason"] == reason
        assert store.data["outcomes"][0].result == "failed"


def test_executor_rejects_active_command_when_production_gate_not_armed() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    store.data["production"] = {}
    executor = Executor(
        store,
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["outcomes"][0].result == "rejected"
    assert store.data["outcomes"][0].reason == "production_gate_not_armed"


def test_executor_control_gate_helpers_cover_pause_controls_and_daily_caps() -> None:
    now = datetime.now(UTC)
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

    assert _pause_rejection_reason({}, action, now) is None
    assert _pause_rejection_reason({"until": "bad"}, action, now) is None
    assert _pause_rejection_reason({"until": (now - timedelta(minutes=1)).isoformat()}, action, now) is None
    assert _pause_rejection_reason({"until": (now + timedelta(minutes=1)).isoformat()}, action, now) == "planner_paused"
    assert (
        _pause_rejection_reason({"until": (now + timedelta(minutes=1)).isoformat(), "assets": "ev"}, action, now)
        == "ev_control_paused"
    )
    assert (
        _pause_rejection_reason({"until": (now + timedelta(minutes=1)).isoformat(), "assets": ["daikin"]}, action, now)
        is None
    )
    assert (
        _pause_rejection_reason({"until": (now + timedelta(minutes=1)).isoformat(), "assets": 123}, action, now) is None
    )

    assert _device_control_disabled_reason(ActionAsset.EV, {}) == "ev_control_disabled"
    assert _device_control_disabled_reason(ActionAsset.DAIKIN, {}) == "climate_control_disabled"
    assert _device_control_disabled_reason(ActionAsset.ENPHASE, {}) == "enphase_control_disabled"
    assert _device_control_disabled_reason(ActionAsset.EV, {CONF_EV_CONTROL_ENABLED: True}) is None

    audit = [
        "bad",
        {"asset": "daikin", "attempted_at": now.isoformat(), "result": "applied"},
        {"asset": "ev", "attempted_at": "bad", "result": "applied"},
        {"asset": "ev", "attempted_at": (now - timedelta(days=2)).isoformat(), "result": "applied"},
        {"asset": "ev", "attempted_at": now.isoformat(), "result": "rejected"},
        {"asset": "ev", "attempted_at": now.isoformat(), "result": "applied"},
        {"asset": "ev", "attempted_at": now.isoformat(), "result": "failed"},
        {"asset": "ev", "attempted_at": now.isoformat(), "result": "restored"},
    ]
    assert _daily_action_cap_reason(ActionAsset.EV, {CONF_MAX_DAILY_EV_ACTIONS: 0}, audit, now) is None
    assert _daily_action_cap_reason(ActionAsset.EV, {CONF_MAX_DAILY_EV_ACTIONS: 1}, "bad", now) is None
    assert _daily_action_cap_reason(ActionAsset.EV, {CONF_MAX_DAILY_EV_ACTIONS: 4}, audit, now) is None
    assert (
        _daily_action_cap_reason(ActionAsset.EV, {CONF_MAX_DAILY_EV_ACTIONS: 3}, audit, now)
        == "ev_daily_action_cap_reached"
    )
    assert (
        _daily_action_cap_reason(ActionAsset.DAIKIN, {CONF_MAX_DAILY_CLIMATE_ACTIONS: 1}, audit, now)
        == "climate_daily_action_cap_reached"
    )
    assert _daily_action_cap_reason(ActionAsset.ENPHASE, {CONF_MAX_DAILY_ENPHASE_ACTIONS: 1}, audit, now) is None

    store = FakeStore()
    executor = Executor(store)
    assert executor._control_rejection_reason(action, now) is None
    store.data["control_pause"] = {"until": (now + timedelta(minutes=5)).isoformat(), "assets": ["all"]}
    assert executor._control_rejection_reason(action, now) == "ev_control_paused"
    store.data["control_pause"] = {}
    store.data["production"] = {}
    assert executor._control_rejection_reason(action, now) == "production_gate_not_armed"
    store.data["production"] = {"armed": True}
    assert executor._control_rejection_reason(action, now) == "ev_control_disabled"
    executor.options = {CONF_EV_CONTROL_ENABLED: True}
    assert executor._control_rejection_reason(action, now) is None
    executor.options = {CONF_EV_CONTROL_ENABLED: True, CONF_MAX_DAILY_EV_ACTIONS: 3}
    store.data["execution_audit"] = audit
    assert executor._control_rejection_reason(action, now) == "ev_daily_action_cap_reached"


def test_executor_ignores_malformed_command_rate_limit_timestamp() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    store.data["command_rate_limits"] = {"ev:ev_start": "not-a-date"}
    hass = FakeHass({"input_boolean.ev_start": "off", "input_boolean.ev_stop": "on"})
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_COMMAND_RATE_LIMIT_SECONDS: 3600,
        },
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["outcomes"][0].result == "applied"
    assert store.data["outcomes"][0].reason == "input_boolean_turn_on_called"
    assert store.data["outcomes"][0].asset == "ev"
    assert store.data["outcomes"][0].kind == "ev_start"
    assert store.data["outcomes"][0].service_target == "input_boolean.ev_start"
    assert hass.states.values["input_boolean.ev_start"] == "on"


def test_executor_restore_ai_releases_enphase_ownership() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="enphase-restore",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.ENPHASE,
        kind=ActionKind.RESTORE_AI,
        desired_state={"profile": "AI Optimisation"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=0.0,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    store = FakeStore()
    store.data["ownership"] = {
        "enphase_profile": "AI Optimisation",
        "enphase_profile_changed_at": (now - timedelta(hours=1)).isoformat(),
        "ev_smart_charging_state": {CONF_EV_SMART_CHARGING_START: "off"},
    }
    hass = FakeHass({"input_select.enphase_profile": "Savings"})
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_ENPHASE_PROFILE: "input_select.enphase_profile",
            CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "input_select.select_option",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
    )

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert hass.states.values["input_select.enphase_profile"] == "AI Optimisation"
    assert "enphase_profile" not in store.data["ownership"]
    assert "enphase_profile_changed_at" not in store.data["ownership"]
    assert store.data["ownership"]["ev_smart_charging_state"] == {
        CONF_EV_SMART_CHARGING_START: "off",
    }
    assert store.data["outcomes"][0].result == "applied"


def test_executor_returns_without_outcome_for_no_or_not_due_action() -> None:
    now = datetime.now(UTC)
    store = FakeStore()
    executor = Executor(store)
    empty_plan = EnergyPlan(
        "plan-1", now, 24, 5, "current", InputHealth.HEALTHY, PlannerMode.ACTIVE_HEALTHY, "test", 1.0, None, [], []
    )
    future_action = PlanAction(
        action_id="future",
        plan_id="plan-1",
        execute_not_before=now + timedelta(hours=1),
        execute_not_after=now + timedelta(hours=2),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )
    future_plan = EnergyPlan(
        "plan-1",
        now,
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "test",
        1.0,
        None,
        [future_action],
        [],
    )

    asyncio.run(executor.async_evaluate(empty_plan))
    asyncio.run(executor.async_evaluate(future_plan))

    assert store.data["outcomes"] == []


def test_executor_records_mode_rejections_without_hass() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        "ev",
        "plan-1",
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
    for mode, expected_result, expected_reason in [
        (PlannerMode.DRY_RUN, "skipped", "dry_run"),
        (PlannerMode.DISABLED, "rejected", "planner_disabled"),
        (PlannerMode.ACTIVE_DEGRADED, "rejected", "input_health_degraded"),
    ]:
        store = FakeStore()
        plan = EnergyPlan("plan-1", now, 24, 5, "current", InputHealth.HEALTHY, mode, "test", 1.0, None, [action], [])
        asyncio.run(Executor(store).async_evaluate(plan))
        assert store.data["outcomes"][0].result == expected_result
        assert store.data["outcomes"][0].reason == expected_reason


def test_executor_applies_daikin_action_and_records_takeover(monkeypatch: object) -> None:
    class FakeDaikinAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_execute(self, action: PlanAction) -> object:
            return type(
                "Result",
                (),
                {
                    "applied": True,
                    "reason": "hvac_set",
                    "pre_state": {"climate.daikin": "off"},
                    "post_state": {"climate.daikin": "heat"},
                    "saved_automation_states": {"automation.hvac": "on"},
                },
            )()

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {},
        [],
        [],
        None,
        1.0,
        None,
    )
    plan = EnergyPlan(
        "plan-1",
        now,
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "test",
        1.0,
        None,
        [action],
        [],
    )
    store = FakeStore()
    executor = Executor(
        store, hass=FakeHass({"climate.daikin": "off"}), entry_data={"daikin_climate_entity": "climate.daikin"}
    )

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["outcomes"][0].result == "applied"
    assert store.data["ownership"]["climate_automations"] == {"automation.hvac": "on"}
    assert "planner_hvac_action_expires_at" in store.data["ownership"]


def test_executor_applies_enphase_profile_and_saves_original(monkeypatch: object) -> None:
    class FakeEnphaseAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_execute(self, action: PlanAction) -> object:
            return type(
                "Result",
                (),
                {
                    "applied": True,
                    "reason": "profile_set",
                    "pre_state": {"select.enphase": "AI Optimisation"},
                    "post_state": {"select.enphase": "Self-Consumption"},
                    "saved_profile": "AI Optimisation",
                    "changed_profile_at": True,
                },
            )()

    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", FakeEnphaseAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "enphase",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.ENPHASE,
        ActionKind.SET_PROFILE,
        {},
        [],
        [],
        None,
        1.0,
        None,
    )
    plan = EnergyPlan(
        "plan-1",
        now,
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "test",
        1.0,
        None,
        [action],
        [],
    )
    store = FakeStore()
    executor = Executor(
        store, hass=FakeHass({"select.enphase": "AI Optimisation"}), entry_data={CONF_ENPHASE_PROFILE: "select.enphase"}
    )
    executor.entry_data[CONF_ENPHASE_AI_PROFILE] = "AI Optimisation"

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["outcomes"][0].result == "applied"
    assert store.data["ownership"]["enphase_profile"] == "AI Optimisation"
    assert "enphase_profile_changed_at" in store.data["ownership"]


def test_executor_restore_safe_state_reports_failed_restore(monkeypatch: object) -> None:
    class FakeEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            return type(
                "Result",
                (),
                {"applied": False, "reason": "ev_restore_failed", "pre_state": {"ev": "on"}, "post_state": {}},
            )()

    class FakeDaikinAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            return type(
                "Result",
                (),
                {"applied": True, "reason": "hvac_restored", "pre_state": {}, "post_state": {"hvac": "on"}},
            )()

    class FakeEnphaseAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore_ai(self) -> object:
            return type(
                "Result",
                (),
                {"applied": False, "reason": "enphase_profile_unavailable", "pre_state": {}, "post_state": {}},
            )()

    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", FakeEVAdapter)
    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", FakeEnphaseAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {"switch.ev": "on"},
        "climate_automations": {"automation.hvac": "off"},
    }
    executor = Executor(store, hass=FakeHass())

    outcome = asyncio.run(executor.async_restore_safe_state("manual"))

    assert outcome.result == "failed"
    assert outcome.reason == "manual:ev_restore_failed:hvac_restored:enphase_profile_unavailable"
    assert store.data["ownership"] == {}


def test_executor_notification_helpers_skip_when_unavailable() -> None:
    class UnavailableServices(FakeServices):
        def has_service(self, domain: str, service: str) -> bool:
            return False

    hass = FakeHass()
    hass.services = UnavailableServices(hass.states)
    executor = Executor(FakeStore(), hass=hass)

    asyncio.run(executor._async_create_notification(title="Title", message="Message", notification_id="id"))
    asyncio.run(executor._async_dismiss_notification("id"))
    asyncio.run(
        Executor(FakeStore())._async_create_notification(title="Title", message="Message", notification_id="id")
    )
    asyncio.run(Executor(FakeStore())._async_dismiss_notification("id"))

    assert hass.services.calls == []


def test_executor_message_and_service_target_helpers_cover_edge_cases() -> None:
    now = datetime.now(UTC)
    ev_stop = PlanAction("ev-stop", "plan-1", now, now, ActionAsset.EV, ActionKind.EV_STOP, {}, [], [], None, 1.0, None)
    hvac = PlanAction("hvac", "plan-1", now, now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {}, [], [], None, 1.0, None)
    enphase = PlanAction(
        "enphase", "plan-1", now, now, ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {}, [], [], None, 1.0, None
    )

    assert _service_target_for_action(ev_stop, {CONF_EV_SMART_CHARGING_STOP: "switch.ev_stop"}) == "switch.ev_stop"
    assert _service_target_for_action(hvac, {"daikin_climate_entity": "climate.daikin"}) == "climate.daikin"
    assert (
        _service_target_for_action(enphase, {CONF_ENPHASE_PROFILE: "select.enphase"})
        == "select.select_option:select.enphase"
    )
    assert _profile_control_service_for_target({}, "sensor.enphase") is None
    assert _profile_control_service_for_target({}, None) is None
    assert f"{('x' * 497)}..." in _restore_notification_message("x" * 600)
    assert _clean_reason_codes(["", "  multi\n space  ", "x" * 100]) == ["multi space", ("x" * 77) + "..."]
    assert "not specified" in _plan_fallback_message(
        EnergyPlan(
            "plan-1", now, 24, 5, "current", InputHealth.HEALTHY, PlannerMode.ACTIVE_HEALTHY, "test", 1.0, None, [], []
        ),
        "Summary.",
        [],
    )
