"""Tests for executor capability gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import CoreState

from custom_components.ha_energy_planner import executor as executor_module
from custom_components.ha_energy_planner import notifications as notifications_module
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_IMPORT_PRICE,
    CONF_BYPASS_SAFETY_GATES,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_COMMAND_RATE_LIMIT_SECONDS,
    CONF_DAIKIN_CLIMATE,
    CONF_DEFAULT_READY_BY,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CHARGING,
    CONF_EV_CONFIRMATION_RETRIES,
    CONF_EV_CONFIRMATION_TIMEOUT_SECONDS,
    CONF_EV_CONNECTED,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    CONF_MAX_DAILY_CLIMATE_ACTIONS,
    CONF_MAX_DAILY_ENPHASE_ACTIONS,
    CONF_MAX_DAILY_EV_ACTIONS,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PV_FORECAST,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.executor import (
    Executor,
    _actionable_input_issues,
    _clean_reason_codes,
    _daily_action_cap_reason,
    _device_control_disabled_reason,
    _ev_command_entity_for_action,
    _ev_safety_stop_attempt_limit_reached,
    _ev_safety_stop_backoff_active,
    _ev_safety_stop_block_active,
    _ev_safety_stop_failure_pause,
    _pause_rejection_reason,
    _plan_fallback_message,
    _profile_control_service_for_target,
    _restore_notification_message,
    _service_target_for_action,
)
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
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
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint


class _HVACAdapterDouble(DaikinHVACAdapter):
    """Fake device behavior with the real adapter's persistence-hook contract."""

    def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
        return {}, {}

    def main_takeover_snapshot(self) -> dict[str, Any]:
        return {}


def test_only_actionable_load_forecast_failures_request_notifications() -> None:
    assert _actionable_input_issues(["household_load_entity_forecast_learning"]) == []
    assert _actionable_input_issues(["household_load_entity_forecast_degraded"]) == []
    assert _actionable_input_issues(["household_load_entity_forecast_failed"]) == [
        "household_load_entity_forecast_failed"
    ]
    assert _actionable_input_issues(["household_load_entity_forecast_stale"]) == [
        "household_load_entity_forecast_stale"
    ]


def test_failed_ev_safety_stop_retries_are_bounded_over_a_rolling_day() -> None:
    now = datetime.now(UTC)
    failed_stop = {
        "asset": "ev",
        "kind": "ev_stop",
        "result": "failed",
        "attempted_at": now,
        "desired_state": {"ev_safety_stop": True},
    }

    assert _ev_safety_stop_attempt_limit_reached([failed_stop, failed_stop], now) is False
    assert (
        _ev_safety_stop_attempt_limit_reached(
            [failed_stop, failed_stop],
            now,
            additional_failures=1,
        )
        is True
    )
    assert _ev_safety_stop_attempt_limit_reached([failed_stop] * 3, now) is True
    assert (
        _ev_safety_stop_attempt_limit_reached(
            [{**failed_stop, "attempted_at": now - timedelta(days=2)}] * 3,
            now,
        )
        is False
    )
    assert _ev_safety_stop_attempt_limit_reached("invalid", now) is False
    assert _ev_safety_stop_attempt_limit_reached("invalid", now, additional_failures=3) is True
    assert _ev_safety_stop_attempt_limit_reached(
        [{**failed_stop, "attempted_at": now.replace(tzinfo=None).isoformat()}] * 3,
        now,
    )
    assert (
        _ev_safety_stop_attempt_limit_reached(
            [{**failed_stop, "attempted_at": "invalid"}] * 3,
            now,
        )
        is False
    )
    assert _ev_safety_stop_attempt_limit_reached(["invalid", {"asset": "climate"}], now) is False
    assert _ev_safety_stop_failure_pause(None) is False
    assert _ev_safety_stop_failure_pause({"reason": "ev_stop_not_confirmed"}) is False
    assert _ev_safety_stop_failure_pause({"reason": "ev_stop_not_confirmed", "safety_stop_failure": True}) is True


def test_failed_ev_safety_stop_backoff_survives_unrelated_pause_overwrite() -> None:
    now = datetime.now(UTC)
    failed_stop = {
        "asset": "ev",
        "kind": "ev_stop",
        "result": "failed",
        "attempted_at": now - timedelta(minutes=5),
        "desired_state": {"ev_safety_stop": True},
    }
    store = FakeStore()
    store.data["execution_audit"] = [failed_stop]
    store.data["ownership"] = {"ev_smart_charging_state": {"switch.ev": "on"}}
    store.data["control_pause"] = {
        "active": True,
        "assets": ["climate"],
        "until": now + timedelta(minutes=2),
        "reason": "hvac_state_confirmation_failed",
    }
    executor = Executor(store, options={CONF_EV_CONTROL_ENABLED: True})
    action = SimpleNamespace(
        asset=ActionAsset.EV,
        kind=ActionKind.EV_STOP,
        desired_state={"ev_safety_stop": True},
    )

    assert _ev_safety_stop_backoff_active([failed_stop], now) is True
    assert executor._control_rejection_reason(action, now) == "ev_control_paused"
    context = SimpleNamespace(
        active_overrides=[],
        ev_connected=False,
        input_health=InputHealth.HEALTHY,
        slots=[],
    )
    assert executor._owned_ev_safety_stop(SimpleNamespace(created_at=now), context) is None
    assert (
        _ev_safety_stop_backoff_active(
            [{**failed_stop, "attempted_at": now - timedelta(minutes=11)}],
            now,
        )
        is False
    )
    assert _ev_safety_stop_backoff_active("invalid", now) is False
    assert (
        _ev_safety_stop_backoff_active(
            ["invalid", {**failed_stop, "attempted_at": "invalid"}],
            now,
        )
        is False
    )


def test_persisted_ev_safety_stop_block_survives_audit_rotation() -> None:
    now = datetime.now(UTC)
    limits = {"ev_safety_stop_blocked_until": now + timedelta(hours=23)}

    assert _ev_safety_stop_block_active(limits, now) is True
    assert (
        _ev_safety_stop_block_active(
            {"ev_safety_stop_blocked_until": now - timedelta(seconds=1)},
            now,
        )
        is False
    )
    assert (
        _ev_safety_stop_block_active(
            {"ev_safety_stop_blocked_until": "invalid"},
            now,
        )
        is False
    )
    assert _ev_safety_stop_block_active(None, now) is False


def test_owned_ev_safety_stop_is_rejected_after_retry_limit() -> None:
    now = datetime.now(UTC)
    store = FakeStore()
    store.data["execution_audit"] = [
        {
            "asset": "ev",
            "kind": "ev_stop",
            "result": "failed",
            "attempted_at": now,
            "desired_state": {"ev_safety_stop": True},
        }
    ] * 3
    executor = Executor(store, options={CONF_EV_CONTROL_ENABLED: True})
    action = SimpleNamespace(
        asset=ActionAsset.EV,
        kind=ActionKind.EV_STOP,
        desired_state={"ev_safety_stop": True},
    )

    assert executor._control_rejection_reason(action, now) == "ev_safety_stop_retry_limit_reached"
    assert executor._owned_ev_safety_stop(SimpleNamespace(created_at=now), None) is None


def test_failed_start_pause_does_not_block_owned_ev_safety_stop() -> None:
    now = datetime.now(UTC)
    store = FakeStore()
    store.data["control_pause"] = {
        "active": True,
        "assets": ["ev"],
        "until": now + timedelta(minutes=10),
        "reason": "ev_charging_confirmation_timeout",
    }
    executor = Executor(store, options={CONF_EV_CONTROL_ENABLED: True})
    action = SimpleNamespace(
        asset=ActionAsset.EV,
        kind=ActionKind.EV_STOP,
        desired_state={"ev_safety_stop": True},
    )

    assert executor._control_rejection_reason(action, now) is None

    store.data["control_pause"]["safety_stop_failure"] = True
    assert executor._control_rejection_reason(action, now) == "ev_control_paused"


class FakeStore:
    """Minimal planner store."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"ownership": {}, "outcomes": []}
        self.flush_count = 0

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

    async def async_flush(self) -> None:
        self.flush_count += 1


def _arm_store(store: FakeStore, executor: Executor) -> None:
    """Install a complete production contract for active execution tests."""
    store.data["production"] = {
        "armed": True,
        "dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(executor.entry_data, executor.options),
    }


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str
    attributes: dict[str, Any] | None = None


class FakeStates:
    """Minimal state registry."""

    def __init__(self, values: dict[str, str | FakeState] | None = None) -> None:
        self.values = values or {}

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        if value is None:
            return None
        if isinstance(value, FakeState):
            return value
        attributes = {"temperature": 21.0} if entity_id.startswith("climate.") else None
        return FakeState(value, attributes)


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

    def __init__(self, values: dict[str, str | FakeState] | None = None) -> None:
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
        input_health=InputHealth.HEALTHY,
    )


def test_executor_loads_hvac_takeover_timestamp_from_store() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    store = FakeStore()
    store.data["ownership"]["planner_takeover_started_at"] = now.isoformat()
    store.data["ownership"]["hvac_control"] = {"phase": "away_off"}

    ownership = Executor(store)._ownership_from_store()

    assert ownership.planner_takeover_started_at == now
    assert ownership.hvac_control_phase == "away_off"


def test_executor_ignores_malformed_ownership_timestamps_from_store() -> None:
    store = FakeStore()
    store.data["ownership"] = {
        "enphase_profile_changed_at": "not-a-date",
        "planner_takeover_started_at": "also-not-a-date",
        "manual_hvac_override_expires_at": "still-not-a-date",
        "hvac_control": "not-a-mapping",
    }

    ownership = Executor(store)._ownership_from_store()

    assert ownership.enphase_profile_changed_at is None
    assert ownership.planner_takeover_started_at is None
    assert ownership.manual_hvac_override_expires_at is None
    assert ownership.hvac_control_phase is None


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
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
        },
    )
    _arm_store(store, executor)
    asyncio.run(executor.async_evaluate(plan, _context(now)))
    assert store.data["outcomes"][0].result == "rejected"
    assert store.data["outcomes"][0].reason == "ev_start_control_unavailable,ev_stop_control_not_configured"
    assert hass.services.calls == []


def test_executor_keep_on_ignores_unavailable_separate_start_control() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev-keep-on",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={"keep_charger_on": True},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
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
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_control": "off",
            "button.ev_start": "unavailable",
        }
    )
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_control",
            CONF_EV_CHARGER_START: "button.ev_start",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
        },
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert hass.services.calls == [("switch", "turn_on", {"entity_id": "switch.ev_control"})]
    assert store.data["outcomes"][0].result == "applied"
    assert store.data["outcomes"][0].reason == ("ev_charger_enabled_for_preconditioning")


def test_executor_keep_on_rejects_invalid_persistent_controls() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev-keep-on",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={"keep_charger_on": True},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
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
    cases = [
        (
            {
                CONF_EV_CONNECTED: "binary_sensor.ev_connected",
                CONF_EV_CHARGER_START: "input_boolean.ev_start",
                CONF_EV_CHARGER_STOP: "input_boolean.ev_stop",
            },
            {
                "binary_sensor.ev_connected": "on",
                "input_boolean.ev_start": "off",
                "input_boolean.ev_stop": "on",
            },
            "ev_keep_on_requires_stateful_control",
        ),
        (
            {
                CONF_EV_CONNECTED: "binary_sensor.ev_connected",
                CONF_EV_CHARGER: "switch.missing_control",
                CONF_EV_CHARGER_START: "input_boolean.ev_start",
                CONF_EV_CHARGER_STOP: "input_boolean.ev_stop",
            },
            {
                "binary_sensor.ev_connected": "on",
                "input_boolean.ev_start": "off",
                "input_boolean.ev_stop": "on",
            },
            "ev_keep_on_control_unavailable",
        ),
    ]

    for entry_data, states, expected_reason in cases:
        store = FakeStore()
        hass = FakeHass(states)
        executor = Executor(
            store,
            hass=hass,
            entry_data=entry_data,
            options={
                **DEFAULT_OPTIONS,
                "planner_enabled": True,
                "dry_run": False,
                CONF_EV_CONTROL_ENABLED: True,
            },
        )
        _arm_store(store, executor)

        asyncio.run(executor.async_evaluate(plan, _context(now)))

        assert hass.services.calls == []
        assert store.data["outcomes"][0].result == "rejected"
        assert store.data["outcomes"][0].reason == expected_reason


def test_successful_restore_safe_state_dismisses_old_alert_without_notifying() -> None:
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(
        store,
        hass=hass,
        options={CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED: False},
    )

    outcome = asyncio.run(executor.async_restore_safe_state("test_restore_reason"))

    assert outcome.result == "restored"
    assert store.data["ownership"] == {}
    assert hass.services.calls == [
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_restore_safe_state"},
        )
    ]


def test_failed_restore_notification_is_actionable_and_deduplicated() -> None:
    hass = FakeHass()
    executor = Executor(FakeStore(), hass=hass)
    outcome = SimpleNamespace(result=OutcomeResult.FAILED, reason="hvac_restore_failed")

    asyncio.run(executor._async_notify_restore(outcome))
    asyncio.run(executor._async_notify_restore(outcome))

    assert hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner safe-state restore failed",
                "message": (
                    "Some planner-owned controls could not be restored. Check the mapped devices and retry. "
                    "Reason: hvac_restore_failed."
                ),
                "notification_id": "ha_energy_planner_restore_safe_state",
            },
        )
    ]


def test_failed_restore_notification_waits_for_home_assistant_startup(monkeypatch: object) -> None:
    hass = FakeHass()
    hass.data = {}
    hass.state = CoreState.starting
    executor = Executor(FakeStore(), hass=hass)
    outcome = SimpleNamespace(result=OutcomeResult.FAILED, reason="enphase_profile_entity_unavailable")
    start_callbacks: list[Any] = []
    monkeypatch.setattr(
        notifications_module,
        "async_at_started",
        lambda hass_arg, callback: start_callbacks.append(callback) or (lambda: None),
    )

    asyncio.run(executor._async_notify_restore(outcome))

    assert hass.services.calls == []
    assert len(start_callbacks) == 1
    assert executor._plan_fallback_notification_signatures == {}

    hass.state = CoreState.running
    asyncio.run(start_callbacks[0](hass))

    assert len(hass.services.calls) == 1
    assert hass.services.calls[0][0:2] == ("persistent_notification", "create")
    assert executor._plan_fallback_notification_signatures


def test_recovered_restore_cancels_deferred_startup_notification(monkeypatch: object) -> None:
    hass = FakeHass()
    hass.data = {}
    hass.state = CoreState.starting
    executor = Executor(FakeStore(), hass=hass)
    start_callbacks: list[Any] = []
    listener_cancelled: list[bool] = []
    monkeypatch.setattr(
        notifications_module,
        "async_at_started",
        lambda hass_arg, callback: (start_callbacks.append(callback) or (lambda: listener_cancelled.append(True))),
    )

    asyncio.run(
        executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.FAILED, reason="enphase_profile_entity_unavailable")
        )
    )
    asyncio.run(
        executor._async_notify_restore(SimpleNamespace(result=OutcomeResult.RESTORED, reason="enphase_profile_applied"))
    )

    assert listener_cancelled == [True]
    hass.state = CoreState.running
    asyncio.run(start_callbacks[0](hass))
    assert all(call[1] != "create" for call in hass.services.calls)


def test_restore_safe_state_attempts_configured_enphase_without_ownership() -> None:
    store = FakeStore()
    hass = FakeHass({"select.enphase": "Self Consumption"})
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_ENPHASE_PROFILE: "select.enphase",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    outcome = asyncio.run(executor.async_restore_safe_state("manual"))

    assert outcome.result == "restored"
    assert outcome.reason == "manual:enphase_profile_applied"
    assert outcome.post_state["ownership"] == "cleared"
    assert hass.states.values["select.enphase"] == "AI Optimisation"
    assert hass.services.calls[0] == (
        "select",
        "select_option",
        {"entity_id": "select.enphase", "option": "AI Optimisation"},
    )


@pytest.mark.parametrize(
    "restore_reason",
    ["production_evidence_contract_changed", "startup_control_paused"],
)
def test_restore_safe_state_ignores_unavailable_unowned_enphase_fallback(
    restore_reason: str,
) -> None:
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_ENPHASE_PROFILE: "select.enphase",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    outcome = asyncio.run(executor.async_restore_safe_state(restore_reason))

    assert outcome.result == OutcomeResult.RESTORED
    assert outcome.reason == f"{restore_reason}:enphase_profile_entity_unavailable"
    assert store.data["ownership"] == {}
    assert all(call[1] != "create" for call in hass.services.calls)


def test_restore_safe_state_fails_for_unavailable_owned_enphase_profile() -> None:
    store = FakeStore()
    store.data["ownership"] = {"enphase_profile": "Self Consumption"}
    hass = FakeHass()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_ENPHASE_PROFILE: "select.enphase",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    outcome = asyncio.run(executor.async_restore_safe_state("startup"))

    assert outcome.result == OutcomeResult.FAILED
    assert outcome.reason == "startup:enphase_profile_entity_unavailable"
    assert store.data["ownership"] == {"enphase_profile": "Self Consumption"}
    assert any(call[1] == "create" for call in hass.services.calls)


def test_manual_restore_fails_for_unowned_enphase_fallback_failure(
    monkeypatch: object,
) -> None:
    class FailedEnphaseAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore_ai(self) -> object:
            return SimpleNamespace(
                applied=False,
                reason="enphase_profile_service_failed",
                pre_state={},
                post_state={},
            )

    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", FailedEnphaseAdapter)
    hass = FakeHass()

    outcome = asyncio.run(Executor(FakeStore(), hass=hass).async_restore_safe_state("manual_service_call"))

    assert outcome.result == OutcomeResult.FAILED
    assert outcome.reason == "manual_service_call:enphase_profile_service_failed"
    assert any(call[1] == "create" for call in hass.services.calls)


def test_unowned_enphase_fallback_exception_remains_a_restore_failure(
    monkeypatch: object,
) -> None:
    class RaisingEnphaseAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore_ai(self) -> object:
            raise RuntimeError("service failure")

    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", RaisingEnphaseAdapter)

    outcome = asyncio.run(Executor(FakeStore(), hass=FakeHass()).async_restore_safe_state("manual_service_call"))

    assert outcome.result == OutcomeResult.FAILED
    assert outcome.reason == "manual_service_call:enphase_restore_exception"


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
                "notification_id": "ha_energy_planner_ev_infeasible",
            },
        )
    ]
    assert store.data["outcomes"][0].result == "rejected"
    asyncio.run(executor._async_notify_ev_infeasible(action))
    assert len(hass.services.calls) == 1
    plan.actions = []
    asyncio.run(executor.async_notify_plan_fallback(plan, []))
    assert hass.services.calls[-1] == (
        "persistent_notification",
        "dismiss",
        {"notification_id": "ha_energy_planner_ev_infeasible"},
    )


def test_notification_ids_and_titles_are_isolated_per_config_entry() -> None:
    garage = Executor(FakeStore(), entry_id="entry-garage", entry_title="Garage EV")
    driveway = Executor(FakeStore(), entry_id="entry-driveway", entry_title="Driveway EV")

    garage_ids = set(garage._plan_fallback_notification_ids())
    driveway_ids = set(driveway._plan_fallback_notification_ids())

    assert garage_ids.isdisjoint(driveway_ids)
    assert garage._notification_id("ha_energy_planner_restore_safe_state") == (
        "ha_energy_planner_restore_safe_state_entry-garage"
    )
    assert driveway._notification_title("plan unsafe") == "Driveway EV: plan unsafe"


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
        input_issues=["daikin_climate_unavailable", "grid_import_limit_exceeded"],
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
            "dismiss",
            {"notification_id": "ha_energy_planner_hvac_capability"},
        ),
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner configuration needs attention",
                "message": (
                    "Automatic control is blocked because required configuration or mapped entities "
                    "need attention. Reason codes: daikin_climate_unavailable."
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
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_ev_infeasible"},
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
        input_issues=["input_health_unsafe", "daikin_climate_unavailable"],
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
            {"notification_id": "ha_energy_planner_ev_infeasible"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_hvac_capability"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_haeo_fallback"},
        ),
    ]


def test_hvac_capability_notification_is_deduplicated_and_recovers() -> None:
    now = datetime.now(UTC)
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.DEGRADED,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="test",
        confidence=0.8,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
        input_issues=["main_climate_target_unavailable"],
    )
    hass = FakeHass({"climate.daikin": FakeState("off", {})})
    executor = Executor(
        FakeStore(),
        hass=hass,
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options={CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY: True},
    )

    asyncio.run(executor.async_notify_plan_fallback(plan, []))
    asyncio.run(executor.async_notify_plan_fallback(plan, []))

    creates = [
        call
        for call in hass.services.calls
        if call[:2] == ("persistent_notification", "create")
        and call[2]["notification_id"] == "ha_energy_planner_hvac_capability"
    ]
    assert len(creates) == 1

    plan.input_issues = []
    asyncio.run(executor.async_notify_plan_fallback(plan, []))
    assert (
        "persistent_notification",
        "dismiss",
        {"notification_id": "ha_energy_planner_hvac_capability"},
    ) in hass.services.calls


def test_plan_fallback_notifications_can_be_disabled() -> None:
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
        input_issues=["input_health_unsafe", "daikin_climate_unavailable"],
    )
    store = FakeStore()
    hass = FakeHass()
    executor = Executor(
        store,
        hass=hass,
        options={CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED: False},
    )

    asyncio.run(
        executor.async_notify_plan_fallback(
            plan,
            ["input_health_unsafe", "grid_import_limit_exceeded"],
        )
    )

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
            {"notification_id": "ha_energy_planner_ev_infeasible"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_hvac_capability"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_haeo_fallback"},
        ),
    ]


def test_plan_fallback_notification_ignores_self_recovering_stale_input() -> None:
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
        input_issues=["amber_import_price_stale"],
    )
    hass = FakeHass()

    asyncio.run(Executor(FakeStore(), hass=hass).async_notify_plan_fallback(plan, ["input_health_unsafe"]))

    assert all(call[1] == "dismiss" for call in hass.services.calls)


def test_actionable_notification_is_not_recreated_until_condition_changes() -> None:
    class RecoveringServices(FakeServices):
        def __init__(self, states: FakeStates) -> None:
            super().__init__(states)
            self.available = False
            self.fail_next_call = False

        def has_service(self, domain: str, service: str) -> bool:
            return self.available

        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            if self.fail_next_call and service == "create":
                self.fail_next_call = False
                raise RuntimeError("temporary notification failure")
            await super().async_call(domain, service, data, blocking)

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
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
        input_issues=["daikin_climate_unavailable"],
    )
    hass = FakeHass()
    hass.services = RecoveringServices(hass.states)
    executor = Executor(FakeStore(), hass=hass)

    # An unavailable service and a transient call failure must not mark the
    # notification as delivered; the next refresh should retry it.
    violations = ["input_health_unsafe"]
    asyncio.run(executor.async_notify_plan_fallback(plan, violations))
    hass.services.available = True
    hass.services.fail_next_call = True
    asyncio.run(executor.async_notify_plan_fallback(plan, violations))
    asyncio.run(executor.async_notify_plan_fallback(plan, violations))
    asyncio.run(executor.async_notify_plan_fallback(plan, violations))

    create_calls = [
        call
        for call in hass.services.calls
        if call[0:2] == ("persistent_notification", "create")
        and call[2]["notification_id"] == "ha_energy_planner_plan_unsafe"
    ]
    assert len(create_calls) == 1

    plan.input_issues = []
    asyncio.run(executor.async_notify_plan_fallback(plan, violations))
    plan.input_issues = ["daikin_climate_unavailable"]
    asyncio.run(executor.async_notify_plan_fallback(plan, violations))

    create_calls = [
        call
        for call in hass.services.calls
        if call[0:2] == ("persistent_notification", "create")
        and call[2]["notification_id"] == "ha_energy_planner_plan_unsafe"
    ]
    assert len(create_calls) == 2


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
        input_issues=["input_health_unsafe", "daikin_climate_unavailable"],
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
            {"notification_id": "ha_energy_planner_ev_infeasible"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_hvac_capability"},
        ),
        (
            "persistent_notification",
            "dismiss",
            {"notification_id": "ha_energy_planner_haeo_fallback"},
        ),
    ]


def test_startup_recovery_notification_is_deduplicated_and_dismissed() -> None:
    hass = FakeHass()
    executor = Executor(
        FakeStore(),
        hass=hass,
        entry_id="entry-1",
        entry_title="Garage EV",
    )

    asyncio.run(executor.async_notify_startup_recovery_unsafe("entity_unavailable"))
    asyncio.run(executor.async_notify_startup_recovery_unsafe("entity_unavailable"))
    asyncio.run(executor.async_dismiss_startup_recovery_notification())

    create_calls = [call for call in hass.services.calls if call[1] == "create"]
    assert len(create_calls) == 1
    assert create_calls[0][2]["notification_id"] == ("ha_energy_planner_startup_recovery_entry-1")
    assert "retry automatically every 30 seconds" in create_calls[0][2]["message"]
    assert hass.services.calls[-1] == (
        "persistent_notification",
        "dismiss",
        {"notification_id": "ha_energy_planner_startup_recovery_entry-1"},
    )


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
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "input_boolean.ev_start": "off",
            "input_boolean.ev_stop": "on",
        }
    )
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
        },
    )
    _arm_store(store, executor)

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
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "input_boolean.ev_start": "off",
            "input_boolean.ev_stop": "on",
        }
    )
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_COMMAND_RATE_LIMIT_SECONDS: 3600,
        },
    )
    _arm_store(store, executor)

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
    )
    store = FakeStore()
    store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "ev",
            "kind": "ev_start",
            "result": "applied",
            "service_target": "input_boolean.ev_start",
            "desired_state": {"charging_required_now": True},
            "post_state": {"input_boolean.ev_start": "on"},
        }
    ]
    executor = Executor(
        store,
        hass=FakeHass({"input_boolean.ev_start": "off"}),
        entry_data={CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start"},
    )

    assert executor._observed_conflict_reason(action, now) == "external_ev_charging_conflict"
    action.kind = ActionKind.EV_SCHEDULE
    assert executor._observed_conflict_reason(action, now) == "external_ev_charging_conflict"
    store.data["execution_audit"][-1].update(
        {
            "kind": "ev_schedule",
            "desired_state": {},
        }
    )
    assert executor._observed_conflict_reason(action, now) == "external_ev_charging_conflict"
    store.data["execution_audit"][-1].update(
        {
            "kind": "ev_stop",
            "service_target": "input_boolean.ev_stop",
            "desired_state": {"charging_required_now": False},
        }
    )
    assert executor._observed_conflict_reason(action, now) is None
    store.data["execution_audit"][-1].update(
        {
            "kind": "ev_start",
            "service_target": "input_boolean.other_ev_start",
            "desired_state": {"charging_required_now": True},
        }
    )
    assert executor._observed_conflict_reason(action, now) is None
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


def test_executor_ev_conflict_uses_actual_command_and_feedback_entities() -> None:
    now = datetime.now(UTC)
    keep_on_action = PlanAction(
        action_id="keep-on",
        plan_id="plan-1",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={"keep_charger_on": True},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    keep_on_entry_data = {
        CONF_EV_CHARGER: "switch.ev_control",
        CONF_EV_CHARGER_START: "button.ev_start",
        CONF_EV_CHARGING: "binary_sensor.ev_charging",
    }
    keep_on_store = FakeStore()
    keep_on_executor = Executor(
        keep_on_store,
        hass=FakeHass(
            {
                "switch.ev_control": "off",
                "button.ev_start": "unknown",
                "binary_sensor.ev_charging": "fully_charged",
            }
        ),
        entry_data=keep_on_entry_data,
    )
    keep_on_outcome = keep_on_executor._action_outcome(
        keep_on_action,
        now,
        result=OutcomeResult.APPLIED,
        reason="ev_charger_enabled_for_preconditioning",
        pre_state={},
        post_state={},
        plan_id="plan-1",
    )
    keep_on_store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "ev",
            "kind": "ev_start",
            "result": "applied",
            "service_target": keep_on_outcome.service_target,
            "desired_state": {"keep_charger_on": True},
            "post_state": {},
        }
    ]

    assert keep_on_outcome.service_target == "switch.ev_control"
    assert keep_on_executor._observed_conflict_reason(keep_on_action, now) == "external_ev_charging_conflict"

    button_action = PlanAction(
        action_id="button-start",
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
    )
    button_store = FakeStore()
    button_store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "ev",
            "kind": "ev_start",
            "result": "applied",
            "service_target": "button.ev_start",
            "desired_state": {},
            "post_state": {},
        }
    ]
    button_hass = FakeHass(
        {
            "button.ev_start": "unknown",
            "binary_sensor.ev_charging": "off",
        }
    )
    button_executor = Executor(
        button_store,
        hass=button_hass,
        entry_data={
            CONF_EV_CHARGER_START: "button.ev_start",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
        },
    )

    for stopped_state in ("off", "connected_not_charging"):
        button_hass.states.values["binary_sensor.ev_charging"] = stopped_state
        assert button_executor._observed_conflict_reason(button_action, now) == "external_ev_charging_conflict"
    for expected_state in (
        "charging",
        "fully_charged",
        "disconnected",
        "unplugged",
        "not_plugged_in",
        "unavailable",
    ):
        button_hass.states.values["binary_sensor.ev_charging"] = expected_state
        assert button_executor._observed_conflict_reason(button_action, now) is None
    button_hass.states.values.pop("binary_sensor.ev_charging")
    assert button_executor._observed_conflict_reason(button_action, now) is None

    stateful_store = FakeStore()
    stateful_store.data["execution_audit"] = [
        {
            "attempted_at": now.isoformat(),
            "asset": "ev",
            "kind": "ev_start",
            "result": "applied",
            "service_target": "switch.ev_control",
            "desired_state": {},
            "post_state": {},
        }
    ]
    stateful_hass = FakeHass(
        {
            "switch.ev_control": "off",
            "binary_sensor.ev_charging": "unavailable",
        }
    )
    stateful_executor = Executor(
        stateful_store,
        hass=stateful_hass,
        entry_data={
            CONF_EV_CHARGER: "switch.ev_control",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
        },
    )

    assert stateful_executor._observed_conflict_reason(button_action, now) == "external_ev_charging_conflict"
    stateful_hass.states.values.pop("binary_sensor.ev_charging")
    assert stateful_executor._observed_conflict_reason(button_action, now) == "external_ev_charging_conflict"
    stateful_hass.states.values["switch.ev_control"] = "unavailable"
    assert stateful_executor._observed_conflict_reason(button_action, now) is None


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
    executor.hass.states.values.pop("input_select.enphase_profile")
    assert executor._observed_conflict_reason(action, now) is None


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
            "kind": "ev_start",
            "result": "applied",
            "service_target": "input_boolean.ev_start",
            "desired_state": {},
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
            return SimpleNamespace(
                applied=False,
                reason="ev_failed",
                pre_state={"ev_smart_charging_start_entity": "off"},
                post_state={"ev_smart_charging_start_entity": "on"},
                command_sent=True,
                rollback_succeeded=False,
            )

    class FailedHVACAdapter(_HVACAdapterDouble):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_execute(self, action: Any) -> Any:
            return SimpleNamespace(
                applied=False,
                reason="hvac_failed",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state={},
                rollback_succeeded=None,
                command_sent=False,
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
                command_sent=True,
                rollback_succeeded=None,
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
        control_option = {
            ActionAsset.EV: CONF_EV_CONTROL_ENABLED,
            ActionAsset.DAIKIN: CONF_CLIMATE_CONTROL_ENABLED,
            ActionAsset.ENPHASE: CONF_ENPHASE_CONTROL_ENABLED,
        }[action.asset]
        executor = Executor(
            store,
            hass=FakeHass(states),
            entry_data=entry_data,
            options={
                **DEFAULT_OPTIONS,
                "planner_enabled": True,
                "dry_run": False,
                control_option: True,
            },
        )
        _arm_store(store, executor)
        asyncio.run(executor.async_evaluate(plan, _context(now)))
        assert store.data["control_pause"]["reason"] == reason
        assert store.data["outcomes"][0].result == "failed"
        if action.asset == ActionAsset.EV:
            assert store.data["ownership"]["ev_smart_charging_state"] == {"ev_smart_charging_start_entity": "off"}


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
    )

    assert _pause_rejection_reason({}, action, now) is None
    assert _pause_rejection_reason({"until": "bad"}, action, now) == "planner_paused"
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
        _pause_rejection_reason({"until": (now + timedelta(minutes=1)).isoformat(), "assets": 123}, action, now)
        == "planner_paused"
    )

    assert _device_control_disabled_reason(ActionAsset.EV, {}) == "ev_control_disabled"
    assert _device_control_disabled_reason(ActionAsset.DAIKIN, {}) == "climate_control_disabled"
    assert _device_control_disabled_reason(ActionAsset.ENPHASE, {}) == "enphase_control_disabled"
    assert _device_control_disabled_reason(ActionAsset.EV, {CONF_EV_CONTROL_ENABLED: "true"}) == ("ev_control_disabled")
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
    climate_audit = [
        {
            "asset": "daikin",
            "kind": "release_hvac",
            "attempted_at": now.isoformat(),
            "result": "restored",
        },
        {
            "asset": "daikin",
            "kind": "set_hvac",
            "attempted_at": now.isoformat(),
            "result": "applied",
        },
    ]
    assert (
        _daily_action_cap_reason(
            ActionAsset.DAIKIN,
            {CONF_MAX_DAILY_CLIMATE_ACTIONS: 2},
            climate_audit,
            now,
        )
        is None
    )
    assert (
        _daily_action_cap_reason(
            ActionAsset.DAIKIN,
            {CONF_MAX_DAILY_CLIMATE_ACTIONS: 1},
            climate_audit,
            now,
        )
        == "climate_daily_action_cap_reached"
    )
    assert _daily_action_cap_reason(ActionAsset.ENPHASE, {CONF_MAX_DAILY_ENPHASE_ACTIONS: 1}, audit, now) is None

    store = FakeStore()
    executor = Executor(store)
    assert executor._control_rejection_reason(action, now) == "production_gate_not_armed"
    store.data["control_pause"] = {"until": (now + timedelta(minutes=5)).isoformat(), "assets": ["all"]}
    assert executor._control_rejection_reason(action, now) == "planner_paused"
    store.data["control_pause"] = {}
    store.data["production"] = {}
    assert executor._control_rejection_reason(action, now) == "production_gate_not_armed"
    store.data["production"] = {"armed": True}
    assert executor._control_rejection_reason(action, now) == "production_evidence_contract_changed"
    executor.options = {
        CONF_BYPASS_SAFETY_GATES: True,
        CONF_EV_CONTROL_ENABLED: True,
    }
    assert executor._control_rejection_reason(action, now) is None
    executor.options = {}
    store.data["production"] = {"armed": "true"}
    assert executor._control_rejection_reason(action, now) == "production_gate_not_armed"
    store.data["production"] = {"armed": True}
    store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint({}, {})
    assert executor._control_rejection_reason(action, now) == "production_dry_run_evidence_incomplete"
    store.data["production"]["dry_run_ready_cycles"] = 3
    assert executor._control_rejection_reason(action, now) == "ev_control_disabled"
    executor.options = {CONF_EV_CONTROL_ENABLED: True}
    store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint({}, executor.options)
    assert executor._control_rejection_reason(action, now) is None
    executor.options = {CONF_EV_CONTROL_ENABLED: True, CONF_MAX_DAILY_EV_ACTIONS: 3}
    store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint({}, executor.options)
    store.data["execution_audit"] = audit
    assert executor._control_rejection_reason(action, now) == "ev_daily_action_cap_reached"


def test_executor_blocks_armed_control_when_entity_or_policy_contract_changes() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    entry_data = {CONF_EV_SMART_CHARGING_START: "button.ev_start"}
    options = {**DEFAULT_OPTIONS, CONF_EV_CONTROL_ENABLED: True}
    store = FakeStore()
    store.data["production"] = {
        "armed": True,
        "dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(entry_data, options),
    }
    executor = Executor(store, entry_data=entry_data, options=options)

    assert executor._control_rejection_reason(action, now) is None
    executor.options = {**options, CONF_DEFAULT_READY_BY: "23:45"}
    assert executor._control_rejection_reason(action, now) is None
    executor.entry_data = {CONF_EV_SMART_CHARGING_START: "button.ev_replaced"}
    assert executor._control_rejection_reason(action, now) == "production_evidence_contract_changed"
    executor.entry_data = entry_data
    executor.options = {**options, "command_rate_limit_seconds": 999}
    assert executor._control_rejection_reason(action, now) == "production_evidence_contract_changed"


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
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "input_boolean.ev_start": "off",
            "input_boolean.ev_stop": "on",
        }
    )
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_COMMAND_RATE_LIMIT_SECONDS: 3600,
        },
    )
    _arm_store(store, executor)

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
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
    )
    _arm_store(store, executor)

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
        {
            "phase": "preconditioning",
            "period_start": now + timedelta(minutes=30),
            "period_end": now + timedelta(hours=2),
            "baseline_price": 0.10,
            "mode": "heat",
            "precondition_target": 23.0,
            "coast_target": 19.0,
        },
        [],
        [],
        None,
        1.0,
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


def test_executor_reports_dry_run_as_skipped_before_plan_violations() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        "restore",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.ENPHASE,
        ActionKind.RESTORE_AI,
        {"profile": "AI Optimisation"},
        [],
        [],
        None,
        1.0,
    )
    plan = EnergyPlan(
        "plan-1", now, 24, 5, "unsafe", InputHealth.UNSAFE, PlannerMode.DRY_RUN, "test", 0.0, None, [action], []
    )
    context = _context(now)
    context.input_health = InputHealth.UNSAFE
    context.slots[0].baseline_load_forecast_kw = 50.0
    store = FakeStore()
    executor = Executor(store, options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": True})

    asyncio.run(executor.async_evaluate(plan, context))

    assert len(store.data["outcomes"]) == 1
    assert store.data["outcomes"][0].result == OutcomeResult.SKIPPED
    assert store.data["outcomes"][0].reason == "dry_run"


def test_executor_applies_daikin_action_and_records_takeover(monkeypatch: object) -> None:
    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            self.persist_main_state: Any = None
            self.manual_override_check: Any = None
            self.persist_manual_supersession: Any = None
            self.zone_manual_override_check: Any = None
            self.persist_zone_supersession: Any = None
            self.persist_supersessions: Any = None
            self.set_turn_on_feedback: Any = None
            self.set_coupled_zone_feedback: Any = None
            self.set_pending_main_restore: Any = None
            self.set_pending_zone_restore: Any = None

        def set_main_state_persistence_callback(self, callback: Any) -> None:
            self.persist_main_state = callback

        def set_manual_override_check(self, callback: Any) -> None:
            self.manual_override_check = callback

        def set_manual_override_persistence_callback(self, callback: Any) -> None:
            self.persist_manual_supersession = callback

        def set_zone_manual_override_check(self, callback: Any) -> None:
            self.zone_manual_override_check = callback

        def set_zone_manual_override_persistence_callback(self, callback: Any) -> None:
            self.persist_zone_supersession = callback

        def set_manual_supersession_persistence_callback(self, callback: Any) -> None:
            self.persist_supersessions = callback

        def set_turn_on_feedback_callback(self, callback: Any) -> None:
            self.set_turn_on_feedback = callback

        def set_coupled_zone_feedback_callback(self, callback: Any) -> None:
            self.set_coupled_zone_feedback = callback

        def set_pending_main_restore_callback(self, callback: Any) -> None:
            self.set_pending_main_restore = callback

        def set_pending_zone_restore_callback(self, callback: Any) -> None:
            self.set_pending_zone_restore = callback

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
            return {"automation.hvac": "on"}, {
                "switch.zone": "off",
                "climate.zone_temperature": {"target_temperature": 21},
            }

        def main_takeover_snapshot(self) -> dict[str, Any]:
            return {"hvac_mode": "off", "target_temperature": 20}

        async def async_execute(self, action: PlanAction) -> object:
            assert self.manual_override_check() is False
            assert callable(self.persist_manual_supersession)
            assert self.zone_manual_override_check() == set()
            assert callable(self.persist_zone_supersession)
            assert callable(self.persist_supersessions)
            self.set_turn_on_feedback(True)
            assert executor.pending_hvac_desired_state["turn_on_feedback_expected"] is True
            self.set_turn_on_feedback(False)
            self.set_coupled_zone_feedback(
                "switch.zone",
                "on",
                "planner-context",
            )
            assert executor.pending_hvac_desired_state["coupled_zone_feedback_expected"] == {
                "actuator_entity_id": "switch.zone",
                "context_id": "planner-context",
                "state": "on",
            }
            self.set_coupled_zone_feedback(None, None, None)
            self.set_pending_main_restore({"hvac_mode": "off", "target_temperature": 20})
            assert executor.pending_hvac_desired_state["restore_main"] == {
                "hvac_mode": "off",
                "target_temperature": 20,
            }
            self.set_pending_zone_restore({"switch.zone": "off"})
            assert executor.pending_hvac_desired_state["restore_zones"] == {"switch.zone": "off"}
            assert store.data["ownership"]["climate_automations"] == {"automation.hvac": "on"}
            assert store.data["ownership"]["hvac_control"]["zone_states"] == {"switch.zone": "off"}
            assert store.data["ownership"]["hvac_control"]["main_state"] == {
                "hvac_mode": "off",
                "target_temperature": 20,
            }
            assert store.flush_count >= 1
            await self.persist_main_state(
                {
                    "hvac_mode": "off",
                    "target_temperature": 20,
                    "rollback_hvac_mode_changed": True,
                    "rollback_active_hvac_mode": "cool",
                }
            )
            assert store.data["ownership"]["hvac_control"]["main_state"] == {
                "hvac_mode": "off",
                "target_temperature": 20,
                "rollback_hvac_mode_changed": True,
                "rollback_active_hvac_mode": "cool",
            }
            assert store.flush_count >= 2
            return type(
                "Result",
                (),
                {
                    "applied": True,
                    "reason": "hvac_set",
                    "pre_state": {"climate.daikin": "off"},
                    "post_state": {"climate.daikin": "heat"},
                    "saved_automation_states": {"automation.hvac": "on"},
                    "saved_zone_states": {"switch.zone": "off"},
                    "saved_main_state": {},
                    "rollback_succeeded": None,
                    "command_sent": False,
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
        {
            "phase": "preconditioning",
            "period_start": now + timedelta(minutes=30),
            "period_end": now + timedelta(hours=2),
            "baseline_price": 0.2,
            "mode": "heat",
            "precondition_target": 23.0,
            "coast_target": 19.0,
            "projected_precondition_end_temperature": 21.0,
            "enable_zones": True,
        },
        [],
        [],
        None,
        1.0,
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
        store,
        hass=FakeHass({"climate.daikin": "off"}),
        entry_data={"daikin_climate_entity": "climate.daikin"},
        options={CONF_CLIMATE_CONTROL_ENABLED: True},
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["outcomes"][0].result == "applied"
    assert store.data["ownership"]["climate_automations"] == {"automation.hvac": "on"}
    assert store.data["ownership"]["hvac_control"]["phase"] == "preconditioning"
    assert store.data["ownership"]["hvac_control"]["coast_target"] == 19.0
    assert store.data["ownership"]["hvac_control"]["projected_precondition_end_temperature"] == 21.0
    assert store.data["ownership"]["hvac_control"]["zone_states"] == {"switch.zone": "off"}
    assert "main_state" not in store.data["ownership"]["hvac_control"]
    assert "planner_hvac_action_expires_at" in store.data["ownership"]
    assert store.flush_count == 2

    takeover_started_at = store.data["ownership"]["planner_takeover_started_at"]
    peak_action = replace(
        action,
        action_id="hvac-peak",
        desired_state={**action.desired_state, "phase": "peak_coast", "coast_target": 19.0},
    )
    asyncio.run(executor.async_evaluate(replace(plan, actions=[peak_action])))

    assert store.data["ownership"]["planner_takeover_started_at"] == takeover_started_at
    assert store.flush_count == 4


@pytest.mark.parametrize("synchronize_zone_temperatures", [False, True])
def test_executor_rechecks_climate_rollback_capability_before_mutation(
    monkeypatch: object,
    synchronize_zone_temperatures: bool,
) -> None:
    class UnexpectedAdapter(_HVACAdapterDouble):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("adapter must not be constructed")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", UnexpectedAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac-race",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"hvac_mode": "heat", "target_temperature": 21},
        [],
        [],
        None,
        1.0,
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
    hass = FakeHass({"climate.daikin": FakeState("off", {})})
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options={
            CONF_CLIMATE_CONTROL_ENABLED: True,
            CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY: synchronize_zone_temperatures,
        },
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["outcomes"][-1].result == OutcomeResult.REJECTED
    assert store.data["outcomes"][-1].reason == "main_climate_target_unavailable"
    assert store.data["ownership"] == {}
    assert store.data.get("control_pause", {}) == {}
    assert hass.services.calls == []


def test_executor_marks_only_an_active_hvac_transaction_as_manual() -> None:
    executor = Executor(FakeStore())

    assert executor.mark_pending_hvac_manual_override() is False
    assert executor.mark_pending_hvac_zone_manual_override("switch.zone") is False
    assert executor.mark_pending_hvac_zone_manual_override("") is False
    executor.pending_hvac_desired_state = {"target_temperature": 21}

    assert executor.mark_pending_hvac_manual_override() is True
    assert executor.mark_pending_hvac_zone_manual_override("switch.zone") is True
    assert executor.mark_pending_hvac_zone_manual_override("switch.zone") is True
    assert executor.pending_hvac_desired_state == {
        "target_temperature": 21,
        "manual_override_detected": True,
        "manual_zone_entity_ids": ["switch.zone"],
    }


def test_executor_flushes_manual_main_supersession_without_losing_subordinates() -> None:
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "zone_states": {"switch.zone": "off"},
        },
    }
    executor = Executor(store)

    asyncio.run(executor._async_persist_provisional_hvac_manual_supersession())

    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {"zone_states": {"switch.zone": "off"}},
    }
    assert store.flush_count == 1


def test_executor_flushes_manual_zone_supersession_without_losing_other_evidence() -> None:
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "zone_states": {
                "climate.manual_zone": {"target_temperature": 22},
                "switch.other_zone": "off",
            },
        },
    }
    executor = Executor(store)

    asyncio.run(executor._async_persist_provisional_hvac_zone_supersession({"climate.manual_zone"}))

    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "zone_states": {"switch.other_zone": "off"},
        },
    }
    assert store.flush_count == 1


def test_executor_marks_away_off_without_taking_zone_ownership(
    monkeypatch: object,
) -> None:
    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, str]]:
            return {"automation.hvac": "on"}, {"switch.zone": "off"}

        async def async_execute(self, action: PlanAction) -> object:
            if action.desired_state == {"hvac_mode": "off"}:
                saved_zone_states: dict[str, str] = {}
                post_state = {
                    "climate.daikin": "off",
                    "switch.zone": "off",
                }
            else:
                # The away-off phase already created an empty zone ownership
                # map. A later phase must merge this newly acquired baseline
                # before the adapter can mutate the zone.
                assert store.data["ownership"]["hvac_control"]["zone_states"] == {"switch.zone": "off"}
                saved_zone_states = {"switch.zone": "off"}
                post_state = {
                    "climate.daikin": "heat",
                    "switch.zone": "on",
                }
            return SimpleNamespace(
                applied=True,
                reason="hvac_action_applied",
                pre_state={"climate.daikin": "heat", "switch.zone": "off"},
                post_state=post_state,
                saved_automation_states={"automation.hvac": "on"},
                saved_zone_states=saved_zone_states,
                saved_main_state={},
                rollback_succeeded=None,
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac-away",
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
        store,
        hass=FakeHass({"climate.daikin": "heat"}),
        entry_data={"daikin_climate_entity": "climate.daikin"},
        options={CONF_CLIMATE_CONTROL_ENABLED: True},
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan))

    away_control = store.data["ownership"]["hvac_control"]
    assert away_control["zone_states"] == {}
    assert away_control["phase"] == "away_off"
    assert isinstance(away_control["started_at"], datetime)

    precondition_action = replace(
        action,
        action_id="hvac-precondition",
        desired_state={
            "hvac_mode": "heat",
            "target_temperature": 23,
            "enable_zones": True,
        },
    )
    asyncio.run(
        executor.async_evaluate(
            replace(plan, actions=[precondition_action]),
        )
    )

    assert store.data["ownership"]["hvac_control"]["zone_states"] == {
        "switch.zone": "off",
    }


@pytest.mark.parametrize(
    ("command_sent", "expected_result"),
    [(False, OutcomeResult.SKIPPED), (True, OutcomeResult.APPLIED)],
)
def test_persisted_preconditioning_accounts_for_commands_after_limits(
    monkeypatch: object,
    command_sent: bool,
    expected_result: OutcomeResult,
) -> None:
    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, str]]:
            return {"automation.hvac": "off"}, {"switch.zone": "on"}

        async def async_execute(self, action: PlanAction) -> object:
            return SimpleNamespace(
                applied=True,
                reason="already_in_desired_hvac_state",
                pre_state={"climate.daikin": "heat"},
                post_state={"climate.daikin": "heat"},
                saved_automation_states={"automation.hvac": "off"},
                saved_zone_states={"switch.zone": "on"},
                command_sent=command_sent,
                saved_main_state={},
                rollback_succeeded=None,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    now = datetime.now(UTC)
    period_start = now + timedelta(minutes=30)
    period_end = now + timedelta(hours=2)
    precondition_end = period_start
    action = PlanAction(
        "hvac-preconditioning",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "phase": "preconditioning",
            "mode": "heat",
            "hvac_mode": "heat",
            "period_start": period_start,
            "period_end": period_end,
            "precondition_end": precondition_end,
            "target_temperature": 23.0,
            "precondition_min_price_delta": 0.20,
            "suppression_min_price_delta": 0.15,
            "enable_zones": True,
            "suppress_automations": True,
        },
        [],
        [],
        None,
        1.0,
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
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_CLIMATE_CONTROL_ENABLED: True,
        CONF_COMMAND_RATE_LIMIT_SECONDS: 3600,
        CONF_MAX_DAILY_CLIMATE_ACTIONS: 1,
    }
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "phase": "preconditioning",
            "mode": "heat",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "precondition_end": precondition_end.isoformat(),
            "zone_states": {"switch.zone": "off"},
        },
        "planner_takeover_started_at": now - timedelta(minutes=1),
    }
    store.data["command_rate_limits"] = {"daikin:set_hvac": now}
    store.data["execution_audit"] = [
        {
            "asset": "daikin",
            "attempted_at": now,
            "result": "applied",
        }
    ]
    executor = Executor(
        store,
        hass=FakeHass({"climate.daikin": "heat"}),
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options=options,
    )
    _arm_store(store, executor)
    context = _context(now)
    context.current_hvac_temperature_c = 21
    context.occupied_temperature_low_c = 19
    context.occupied_temperature_high_c = 23
    context.hvac_control = {
        "phase": "preconditioning",
        "mode": "heat",
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "precondition_end": precondition_end.isoformat(),
    }

    asyncio.run(executor.async_evaluate(plan, context))

    assert store.data["outcomes"][-1].result == expected_result
    assert store.data["outcomes"][-1].reason == "already_in_desired_hvac_state"
    assert store.data["ownership"]["hvac_control"]["precondition_min_price_delta"] == 0.20
    assert store.data["ownership"]["hvac_control"]["suppression_min_price_delta"] == 0.15


def test_executor_clears_provisional_hvac_ownership_after_successful_rollback(
    monkeypatch: object,
) -> None:
    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, str]]:
            return {"automation.hvac": "on"}, {"switch.zone": "off"}

        async def async_execute(self, action: PlanAction) -> object:
            assert "hvac_control" in store.data["ownership"]
            return SimpleNamespace(
                applied=False,
                reason="hvac_control_service_failed",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                rollback_succeeded=True,
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"phase": "preconditioning", "mode": "heat", "target_temperature": 23.0},
        [],
        [],
        None,
        1.0,
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
    store.data["ownership"] = {"enphase_profile": "AI Optimisation"}
    executor = Executor(
        store,
        hass=FakeHass({"climate.daikin": "off"}),
        entry_data={"daikin_climate_entity": "climate.daikin"},
        options={CONF_CLIMATE_CONTROL_ENABLED: True},
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["ownership"] == {"enphase_profile": "AI Optimisation"}
    assert store.data["outcomes"][0].reason == "hvac_control_service_failed"


def test_hvac_acquisition_exception_persists_inflight_manual_supersession(
    monkeypatch: object,
) -> None:
    class RaisingDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
            return {}, {
                "climate.bedrooms": {"target_temperature": 21},
                "switch.living": "off",
            }

        def main_takeover_snapshot(self) -> dict[str, Any]:
            return {"hvac_mode": "off", "target_temperature": 20}

        async def async_execute(self, action: PlanAction) -> object:
            assert executor.mark_pending_hvac_manual_override() is True
            assert executor.mark_pending_hvac_zone_manual_override("climate.bedrooms") is True
            raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", RaisingDaikinAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "phase": "preconditioning",
            "hvac_mode": "heat",
            "target_temperature": 23.0,
            "enable_zones": True,
            "configured_zones_only": True,
        },
        [],
        [],
        None,
        1.0,
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
        store,
        hass=FakeHass({"climate.daikin": "off"}),
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options={CONF_CLIMATE_CONTROL_ENABLED: True},
    )
    _arm_store(store, executor)

    with pytest.raises(RuntimeError, match="unexpected adapter failure"):
        asyncio.run(executor.async_evaluate(plan))

    hvac_control = store.data["ownership"]["hvac_control"]
    assert "main_state" not in hvac_control
    assert hvac_control["zone_states"] == {"switch.living": "off"}
    assert store.flush_count == 2


@pytest.mark.parametrize("rollback_succeeded", [True, False])
def test_executor_does_not_reintroduce_inherited_manual_state_after_rollback(
    monkeypatch: object,
    rollback_succeeded: bool,
) -> None:
    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            self.persist_zone_supersession: Any = None

        def set_zone_manual_override_persistence_callback(
            self,
            callback: Any,
        ) -> None:
            self.persist_zone_supersession = callback

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
            return {}, {}

        async def async_execute(self, action: PlanAction) -> object:
            assert executor.mark_pending_hvac_manual_override() is True
            assert executor.mark_pending_hvac_zone_manual_override("climate.manual_zone") is True
            await self.persist_zone_supersession({"climate.manual_zone"})
            return SimpleNamespace(
                applied=False,
                reason="manual_hvac_override_detected",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={
                    "climate.manual_zone": {"target_temperature": 20},
                },
                rollback_succeeded=rollback_succeeded,
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "phase": "preconditioning",
            "target_temperature": 23.0,
            "enable_zones": True,
            "configured_zones_only": True,
        },
        [],
        [],
        None,
        1.0,
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
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "phase": "preconditioning",
            "main_state": {},
            "zone_states": {
                "climate.manual_zone": {"target_temperature": 20},
                "switch.other_zone": "off",
            },
        },
    }
    executor = Executor(
        store,
        hass=FakeHass({"climate.daikin": "heat"}),
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options={CONF_CLIMATE_CONTROL_ENABLED: True},
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["ownership"]["hvac_control"]["zone_states"] == {"switch.other_zone": "off"}
    assert "main_state" not in store.data["ownership"]["hvac_control"]
    if not rollback_succeeded:
        assert store.data["ownership"]["hvac_control"]["required_evidence_lost"] == "hvac_acquisition_rollback_failed"
    assert store.data["outcomes"][0].reason == "manual_hvac_override_detected"


def test_hvac_specific_release_restores_zones_without_touching_other_assets(monkeypatch: object) -> None:
    restored: list[tuple[dict[str, str], dict[str, str]]] = []

    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(
            self,
            automations: dict[str, str],
            zones: dict[str, str],
        ) -> object:
            restored.append((dict(automations), dict(zones)))
            return SimpleNamespace(
                applied=True,
                rollback_succeeded=True,
                reason="hvac_control_released",
                pre_state={"switch.zone": "on"},
                post_state={"switch.zone": "off"},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {"zone_states": {"switch.zone": "off"}, "phase": "peak_coast"},
        "ev_smart_charging_state": {"switch.ev": "off"},
        "enphase_profile": "AI Optimisation",
        "manual_hvac_override_expires_at": None,
    }

    outcome = asyncio.run(Executor(store, hass=FakeHass()).async_release_hvac_control("manual"))

    assert outcome.result == OutcomeResult.RESTORED
    assert restored == [({"automation.hvac": "on"}, {"switch.zone": "off"})]
    assert store.data["ownership"] == {
        "ev_smart_charging_state": {"switch.ev": "off"},
        "enphase_profile": "AI Optimisation",
        "manual_hvac_override_expires_at": None,
    }


def test_hvac_specific_release_restores_persisted_main_state(monkeypatch: object) -> None:
    restored: list[tuple[dict[str, str], dict[str, Any], dict[str, Any]]] = []

    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(
            self,
            automations: dict[str, str],
            zones: dict[str, Any],
            main_state: dict[str, Any],
        ) -> object:
            restored.append((dict(automations), dict(zones), dict(main_state)))
            return SimpleNamespace(
                applied=True,
                rollback_succeeded=True,
                reason="hvac_control_released",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {"switch.zone": "off"},
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "required_evidence_lost": "hvac_acquisition_rollback_failed",
        },
    }

    outcome = asyncio.run(Executor(store, hass=FakeHass()).async_release_hvac_control("retry"))

    assert outcome.result == OutcomeResult.RESTORED
    assert restored == [
        (
            {"automation.hvac": "on"},
            {"switch.zone": "off"},
            {"hvac_mode": "off", "target_temperature": 20},
        )
    ]
    assert store.data["ownership"] == {}


def test_hvac_specific_release_retains_unresolved_main_state(monkeypatch: object) -> None:
    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(
            self,
            automations: dict[str, str],
            zones: dict[str, Any],
            main_state: dict[str, Any],
        ) -> object:
            return SimpleNamespace(
                applied=False,
                rollback_succeeded=False,
                reason="hvac_release_failed",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state=main_state,
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    saved_main_state = {"hvac_mode": "off", "target_temperature": 20}
    store = FakeStore()
    store.data["ownership"] = {
        "hvac_control": {
            "zone_states": {},
            "main_state": saved_main_state,
        },
    }

    outcome = asyncio.run(Executor(store, hass=FakeHass()).async_release_hvac_control("retry"))

    assert outcome.result == OutcomeResult.FAILED
    assert store.data["ownership"]["hvac_control"] == {
        "zone_states": {},
        "main_state": saved_main_state,
        "required_evidence_lost": "hvac_release_failed",
    }


def test_manual_hvac_release_preserves_changed_zone_target(monkeypatch: object) -> None:
    restored: list[dict[str, Any]] = []

    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(
            self,
            automations: dict[str, str],
            zones: dict[str, Any],
        ) -> object:
            assert "climate.bedrooms" not in store.data["ownership"]["hvac_control"]["zone_states"]
            assert store.flush_count == 1
            restored.append(dict(zones))
            return SimpleNamespace(
                applied=True,
                rollback_succeeded=True,
                reason="hvac_control_released",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {
                "climate.bedrooms": {"target_temperature": 21},
                "switch.living": "off",
            },
            "phase": "peak_coast",
        },
    }

    outcome = asyncio.run(
        Executor(store, hass=FakeHass()).async_release_hvac_control(
            "climate_zone_changed",
            preserve_zone_entity_id="climate.bedrooms",
        )
    )

    assert outcome.result == OutcomeResult.RESTORED
    assert restored == [{"switch.living": "off"}]
    assert store.data["ownership"] == {}

    class FailingDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, *args: object) -> object:
            assert "climate.bedrooms" not in failed_store.data["ownership"]["hvac_control"]["zone_states"]
            assert failed_store.flush_count == 1
            raise RuntimeError("restore failed")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FailingDaikinAdapter)
    failed_store = FakeStore()
    failed_store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {"climate.bedrooms": {"target_temperature": 21}},
            "phase": "peak_coast",
        },
    }

    failed = asyncio.run(
        Executor(failed_store, hass=FakeHass()).async_release_hvac_control(
            "climate_zone_changed",
            preserve_zone_entity_id="climate.bedrooms",
        )
    )

    assert failed.result == OutcomeResult.FAILED
    assert failed_store.data["ownership"]["hvac_control"]["zone_states"] == {}


def test_manual_hvac_release_preserves_changed_main_state(monkeypatch: object) -> None:
    restored: list[tuple[dict[str, str], dict[str, Any]]] = []

    class FakeDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(
            self,
            automations: dict[str, str],
            zones: dict[str, Any],
        ) -> object:
            assert "main_state" not in store.data["ownership"]["hvac_control"]
            assert store.flush_count == 1
            restored.append((dict(automations), dict(zones)))
            return SimpleNamespace(
                applied=True,
                rollback_succeeded=True,
                reason="hvac_control_released",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FakeDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {"switch.zone": "off"},
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "required_evidence_lost": "hvac_acquisition_rollback_failed",
        },
    }

    outcome = asyncio.run(
        Executor(store, hass=FakeHass()).async_release_hvac_control(
            "daikin_state_changed",
            preserve_main_state=True,
        )
    )

    assert outcome.result == OutcomeResult.RESTORED
    assert restored == [({"automation.hvac": "on"}, {"switch.zone": "off"})]
    assert store.data["ownership"] == {}


def test_hvac_release_exception_does_not_reintroduce_inflight_manual_changes(
    monkeypatch: object,
) -> None:
    class RaisingDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, *args: object) -> object:
            assert executor.mark_pending_hvac_manual_override() is True
            assert executor.mark_pending_hvac_zone_manual_override("climate.bedrooms") is True
            raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", RaisingDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {
                "climate.bedrooms": {"target_temperature": 21},
                "switch.living": "off",
            },
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "phase": "peak_coast",
        },
    }
    executor = Executor(store, hass=FakeHass())

    outcome = asyncio.run(executor.async_release_hvac_control("retry"))

    assert outcome.result == OutcomeResult.FAILED
    assert store.flush_count == 1
    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {"switch.living": "off"},
            "phase": "peak_coast",
            "required_evidence_lost": "hvac_release_failed",
        },
    }


def test_planned_hvac_release_bypasses_normal_execution_gates() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac-release",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.RELEASE_HVAC,
        {"release_reason": "comfort", "released_until": now + timedelta(hours=1)},
        [],
        [],
        0.0,
        1.0,
    )
    competing_action = PlanAction(
        "ev-start",
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
    )
    plan = EnergyPlan(
        "plan-1",
        now,
        24,
        5,
        "unsafe",
        InputHealth.UNSAFE,
        PlannerMode.DRY_RUN,
        "test",
        0.0,
        None,
        [competing_action, action],
        [],
    )
    executor = Executor(FakeStore())
    releases: list[tuple[str, str, object]] = []

    async def release(reason: str, *, plan_id: str, action: object) -> object:
        releases.append((reason, plan_id, action))
        return SimpleNamespace()

    executor.async_release_hvac_control = release

    consumed_action = asyncio.run(executor.async_evaluate(plan))

    assert releases == [("comfort", "plan-1", action)]
    assert consumed_action is action


def test_blocked_owned_hvac_continuation_releases_before_competing_action() -> None:
    now = datetime.now(UTC)
    competing_action = PlanAction(
        "ev-start",
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
    )
    continuation = PlanAction(
        "hvac-peak",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"phase": "peak_coast", "hvac_mode": "heat", "target_temperature": 19.0},
        [],
        [],
        None,
        1.0,
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
        [competing_action, continuation],
        [],
    )
    store = FakeStore()
    store.data["ownership"] = {"hvac_control": {"phase": "preconditioning"}}
    executor = Executor(
        store,
        options={**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False},
    )
    releases: list[tuple[str, str]] = []

    async def release(reason: str, *, plan_id: str) -> object:
        releases.append((reason, plan_id))
        return SimpleNamespace()

    executor.async_release_hvac_control = release

    consumed_action = asyncio.run(executor.async_evaluate(plan))

    assert releases == [("hvac_continuation_blocked_production_gate_not_armed", "plan-1")]
    assert consumed_action is continuation
    assert store.data["outcomes"] == []


def test_hvac_release_handles_no_hass_exception_partial_retry_and_hold(monkeypatch: object) -> None:
    empty = FakeStore()
    empty_release = asyncio.run(Executor(empty).async_release_hvac_control("empty"))
    assert empty_release.result == OutcomeResult.SKIPPED
    assert empty_release.reason == "already_released_hvac_control"
    assert (
        _daily_action_cap_reason(
            ActionAsset.DAIKIN,
            {CONF_MAX_DAILY_CLIMATE_ACTIONS: 1},
            [
                {
                    "asset": empty_release.asset,
                    "kind": empty_release.kind,
                    "attempted_at": empty_release.attempted_at,
                    "result": empty_release.result,
                }
            ],
            empty_release.attempted_at,
        )
        is None
    )

    class RaisingAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, states: dict[str, str]) -> object:
            raise RuntimeError("restore failed")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", RaisingAdapter)
    unowned_store = FakeStore()
    unowned = asyncio.run(Executor(unowned_store, hass=FakeHass()).async_release_hvac_control("no_ownership"))
    assert unowned.result == OutcomeResult.SKIPPED
    assert unowned.reason == "already_released_hvac_control"

    no_hass_store = FakeStore()
    no_hass_store.data["ownership"] = {"climate_automations": {"automation.hvac": "on"}}
    no_hass = asyncio.run(Executor(no_hass_store).async_release_hvac_control("no_hass"))
    assert no_hass.result == OutcomeResult.FAILED
    assert no_hass_store.data["ownership"]["climate_automations"] == {"automation.hvac": "on"}

    failed_store = FakeStore()
    failed_store.data["ownership"] = {"climate_automations": {"automation.hvac": "on"}}
    failed = asyncio.run(Executor(failed_store, hass=FakeHass()).async_release_hvac_control("manual"))
    assert failed.result == OutcomeResult.FAILED
    assert failed_store.data["ownership"]["climate_automations"] == {"automation.hvac": "on"}
    assert failed_store.data["ownership"]["hvac_control"]["required_evidence_lost"] == "hvac_release_failed"

    class PartialAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, automations: dict[str, str], zones: dict[str, str]) -> object:
            return SimpleNamespace(
                applied=False,
                rollback_succeeded=False,
                reason="hvac_release_failed",
                pre_state={},
                post_state={},
                saved_automation_states={"automation.hvac": "on"},
                saved_zone_states={"switch.zone": "off"},
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", PartialAdapter)
    partial_store = FakeStore()
    partial_store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {"zone_states": {"switch.zone": "off"}},
    }
    failed_hold_until = datetime.now(UTC) + timedelta(hours=1)
    partial_action = SimpleNamespace(
        action_id="partial-release",
        desired_state={"released_until": failed_hold_until},
    )
    partial = asyncio.run(
        Executor(partial_store, hass=FakeHass()).async_release_hvac_control(
            "comfort",
            action=partial_action,
        )
    )
    assert partial.result == OutcomeResult.FAILED
    assert partial_store.data["ownership"]["hvac_control"]["zone_states"] == {"switch.zone": "off"}
    assert partial_store.data["ownership"]["hvac_control"]["required_evidence_lost"] == "hvac_release_failed"
    assert partial_store.data["ownership"]["hvac_release_hold_until"] == failed_hold_until

    class SuccessfulAdapter(PartialAdapter):
        async def async_restore(self, automations: dict[str, str], zones: dict[str, str]) -> object:
            return SimpleNamespace(
                applied=True,
                rollback_succeeded=True,
                reason="hvac_control_released",
                pre_state={},
                post_state={},
                saved_automation_states={},
                saved_zone_states={},
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", SuccessfulAdapter)
    retried = asyncio.run(Executor(partial_store, hass=FakeHass()).async_release_hvac_control("retry"))
    assert retried.result == OutcomeResult.RESTORED
    assert partial_store.data["ownership"] == {"hvac_release_hold_until": failed_hold_until}

    held_store = FakeStore()
    held_store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {"zone_states": {"switch.zone": "off"}},
    }
    hold_until = datetime.now(UTC) + timedelta(hours=1)
    release_action = SimpleNamespace(
        action_id="release",
        desired_state={"released_until": hold_until},
    )
    held = asyncio.run(
        Executor(held_store, hass=FakeHass()).async_release_hvac_control("comfort", action=release_action)
    )
    assert held.result == OutcomeResult.RESTORED
    assert held_store.data["ownership"] == {"hvac_release_hold_until": hold_until}


def test_executor_retains_failed_hvac_rollback_for_later_restore(monkeypatch: object) -> None:
    restored_states: list[tuple[dict[str, str], dict[str, str], dict[str, Any]]] = []

    class TransactionalDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, str]]:
            return (
                {
                    "automation.hvac": "on",
                    "automation.already_restored": "on",
                },
                {
                    "switch.zone": "off",
                    "switch.already_restored": "off",
                },
            )

        def main_takeover_snapshot(self) -> dict[str, Any]:
            return {"hvac_mode": "heat", "target_temperature": 20}

        async def async_execute(self, action: PlanAction) -> object:
            return SimpleNamespace(
                applied=False,
                reason="hvac_acquisition_rollback_failed",
                pre_state={"automation.hvac": "on"},
                post_state={"automation.hvac": "off"},
                saved_automation_states={"automation.hvac": "on"},
                saved_zone_states={"switch.zone": "off"},
                saved_main_state={"hvac_mode": "heat", "target_temperature": 20},
                rollback_succeeded=False,
                command_sent=False,
            )

        async def async_restore(
            self,
            states: dict[str, str],
            zones: dict[str, str],
            main_state: dict[str, Any],
        ) -> object:
            restored_states.append((dict(states), dict(zones), dict(main_state)))
            return SimpleNamespace(
                applied=True,
                rollback_succeeded=True,
                reason="hvac_automation_state_restored",
                pre_state={"automation.hvac": "off"},
                post_state={"automation.hvac": "on"},
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", TransactionalDaikinAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac-rollback",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"hvac_mode": "cool"},
        [],
        [],
        None,
        1.0,
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
        store,
        hass=FakeHass({"climate.daikin": "heat"}),
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options={**DEFAULT_OPTIONS, CONF_CLIMATE_CONTROL_ENABLED: True, "planner_enabled": True, "dry_run": False},
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["ownership"]["climate_automations"] == {"automation.hvac": "on"}
    assert store.data["ownership"]["hvac_control"] == {
        "zone_states": {"switch.zone": "off"},
        "main_state": {"hvac_mode": "heat", "target_temperature": 20},
        "required_evidence_lost": "hvac_acquisition_rollback_failed",
    }
    outcome = asyncio.run(executor.async_restore_safe_state("retry"))
    assert outcome.result == OutcomeResult.RESTORED
    assert restored_states == [
        (
            {"automation.hvac": "on"},
            {"switch.zone": "off"},
            {"hvac_mode": "heat", "target_temperature": 20},
        )
    ]
    assert store.data["ownership"] == {}


def test_executor_recovers_unresolved_main_before_new_acquisition(
    monkeypatch: object,
) -> None:
    restored_main_states: list[dict[str, Any]] = []

    class RecoveryOnlyDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_execute(self, action: PlanAction) -> object:
            raise AssertionError("new acquisition must wait for main recovery")

        async def async_restore(
            self,
            states: dict[str, str],
            zones: dict[str, Any],
            main_state: dict[str, Any],
        ) -> object:
            restored_main_states.append(dict(main_state))
            return SimpleNamespace(
                applied=False,
                rollback_succeeded=False,
                reason="hvac_release_failed",
                pre_state={"climate.daikin": "heat"},
                post_state={"climate.daikin": "heat"},
                saved_automation_states=states,
                saved_zone_states=zones,
                saved_main_state=main_state,
                command_sent=False,
            )

    monkeypatch.setattr(
        executor_module,
        "DaikinHVACAdapter",
        RecoveryOnlyDaikinAdapter,
    )
    now = datetime.now(UTC)
    action = PlanAction(
        "hvac-new-acquisition",
        "plan-1",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {"hvac_mode": "cool"},
        [],
        [],
        None,
        1.0,
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
    saved_main_state = {"hvac_mode": "off", "target_temperature": 20}
    store = FakeStore()
    store.data["ownership"] = {
        "hvac_control": {
            "zone_states": {},
            "main_state": saved_main_state,
            "required_evidence_lost": "hvac_acquisition_rollback_failed",
        },
    }
    executor = Executor(
        store,
        hass=FakeHass({"climate.daikin": "heat"}),
        entry_data={CONF_DAIKIN_CLIMATE: "climate.daikin"},
        options={
            **DEFAULT_OPTIONS,
            CONF_CLIMATE_CONTROL_ENABLED: True,
            "planner_enabled": True,
            "dry_run": False,
        },
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert restored_main_states == [saved_main_state]
    assert store.data["ownership"]["hvac_control"] == {
        "zone_states": {},
        "main_state": saved_main_state,
        "required_evidence_lost": "hvac_release_failed",
    }
    assert store.data["outcomes"][0].result == OutcomeResult.FAILED
    assert store.data["outcomes"][0].reason == "hvac_release_failed"


def test_executor_safe_restore_retains_only_unresolved_hvac_actuators(
    monkeypatch: object,
) -> None:
    class PartialDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(
            self,
            states: dict[str, str],
            zones: dict[str, str],
            main_state: dict[str, Any],
        ) -> object:
            assert states == {
                "automation.restored": "on",
                "automation.failed": "on",
            }
            assert zones == {
                "switch.restored": "off",
                "switch.failed": "off",
            }
            assert main_state == {"hvac_mode": "heat", "target_temperature": 20}
            return SimpleNamespace(
                applied=False,
                rollback_succeeded=False,
                reason="hvac_release_failed",
                pre_state={},
                post_state={},
                saved_automation_states={"automation.failed": "on"},
                saved_zone_states={"switch.failed": "off"},
                saved_main_state={"hvac_mode": "heat", "target_temperature": 20},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", PartialDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {
            "automation.restored": "on",
            "automation.failed": "on",
        },
        "hvac_control": {
            "phase": "peak_coast",
            "zone_states": {
                "switch.restored": "off",
                "switch.failed": "off",
            },
            "main_state": {"hvac_mode": "heat", "target_temperature": 20},
        },
        "planner_takeover_started_at": "2026-01-01T00:00:00+00:00",
        "planner_hvac_action_expires_at": "2026-01-01T00:02:00+00:00",
    }
    executor = Executor(store, hass=FakeHass())

    outcome = asyncio.run(executor.async_restore_safe_state("retry"))

    assert outcome.result == OutcomeResult.FAILED
    assert store.data["ownership"] == {
        "climate_automations": {"automation.failed": "on"},
        "hvac_control": {
            "phase": "peak_coast",
            "zone_states": {"switch.failed": "off"},
            "main_state": {"hvac_mode": "heat", "target_temperature": 20},
            "required_evidence_lost": "hvac_release_failed",
        },
        "planner_takeover_started_at": "2026-01-01T00:00:00+00:00",
        "planner_hvac_action_expires_at": "2026-01-01T00:02:00+00:00",
    }


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
                    "command_sent": True,
                    "rollback_succeeded": None,
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
        store,
        hass=FakeHass({"select.enphase": "AI Optimisation"}),
        entry_data={CONF_ENPHASE_PROFILE: "select.enphase"},
        options={CONF_ENPHASE_CONTROL_ENABLED: True},
    )
    executor.entry_data[CONF_ENPHASE_AI_PROFILE] = "AI Optimisation"
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan))

    assert store.data["outcomes"][0].result == "applied"
    assert store.data["ownership"]["enphase_profile"] == "AI Optimisation"
    assert "enphase_profile_changed_at" in store.data["ownership"]


def test_executor_retains_uncertain_enphase_command_until_safe_restore(monkeypatch: object) -> None:
    restored_profiles: list[str] = []

    class UncertainEnphaseAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_execute(self, action: PlanAction) -> object:
            return SimpleNamespace(
                applied=False,
                reason="enphase_profile_not_confirmed_rollback_failed",
                pre_state={CONF_ENPHASE_PROFILE: "AI Optimisation"},
                post_state={CONF_ENPHASE_PROFILE: "unknown"},
                saved_profile="AI Optimisation",
                changed_profile_at=False,
                command_sent=True,
                rollback_succeeded=False,
            )

        async def async_restore_profile(self, profile: str) -> object:
            restored_profiles.append(profile)
            return SimpleNamespace(
                applied=True,
                reason="enphase_profile_applied",
                pre_state={CONF_ENPHASE_PROFILE: "unknown"},
                post_state={CONF_ENPHASE_PROFILE: "AI Optimisation"},
            )

    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", UncertainEnphaseAdapter)
    now = datetime.now(UTC)
    action = PlanAction(
        "enphase-uncertain",
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
        store,
        hass=FakeHass({"select.enphase": "AI Optimisation"}),
        entry_data={
            CONF_ENPHASE_PROFILE: "select.enphase",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
        options={**DEFAULT_OPTIONS, CONF_ENPHASE_CONTROL_ENABLED: True, "planner_enabled": True, "dry_run": False},
    )
    _arm_store(store, executor)

    asyncio.run(executor.async_evaluate(plan, _context(now)))

    assert store.data["ownership"]["enphase_profile"] == "AI Optimisation"
    assert store.data["outcomes"][-1].result == OutcomeResult.FAILED
    outcome = asyncio.run(executor.async_restore_safe_state("retry"))
    assert outcome.result == OutcomeResult.RESTORED
    assert restored_profiles == ["AI Optimisation"]
    assert store.data["ownership"] == {}


def test_executor_restore_safe_state_reports_failed_restore(monkeypatch: object) -> None:
    ev_adapter_kwargs: list[dict[str, Any]] = []

    class FakeEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any], **kwargs: Any) -> None:
            ev_adapter_kwargs.append(kwargs)

        async def async_restore(self, state: dict[str, Any]) -> object:
            return type(
                "Result",
                (),
                {"applied": False, "reason": "ev_restore_failed", "pre_state": {"ev": "on"}, "post_state": {}},
            )()

    class FakeDaikinAdapter(_HVACAdapterDouble):
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
        "planner_takeover_started_at": "2026-01-01T00:00:00+00:00",
        "enphase_profile": "AI Optimisation",
        "enphase_profile_changed_at": "2026-01-01T00:00:00+00:00",
    }
    executor = Executor(
        store,
        hass=FakeHass(),
        options={
            CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 17,
            CONF_EV_CONFIRMATION_RETRIES: 4,
        },
    )

    outcome = asyncio.run(executor.async_restore_safe_state("manual"))

    assert outcome.result == "failed"
    assert outcome.reason == "manual:ev_restore_failed:hvac_restored:enphase_profile_unavailable"
    assert store.data["ownership"] == {
        "ev_smart_charging_state": {"switch.ev": "on"},
        "enphase_profile": "AI Optimisation",
        "enphase_profile_changed_at": "2026-01-01T00:00:00+00:00",
    }
    assert outcome.post_state["ownership"] == "partially_cleared"
    assert outcome.post_state["remaining_ownership"] == [
        "enphase_profile",
        "enphase_profile_changed_at",
        "ev_smart_charging_state",
    ]
    assert ev_adapter_kwargs == [
        {
            "confirmation_timeout_seconds": 17.0,
            "confirmation_retries": 4,
        }
    ]


def test_executor_restore_device_control_restores_only_selected_asset(monkeypatch: object) -> None:
    restored: list[dict[str, Any]] = []

    class FakeEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any], **kwargs: Any) -> None:
            pass

        async def async_restore(self, state: dict[str, Any] | None = None) -> object:
            restored.append(dict(state or {}))
            return SimpleNamespace(
                applied=True,
                reason="ev_restored",
                pre_state={"switch.ev": "on"},
                post_state={"switch.ev": "off"},
            )

    class UnexpectedAdapter(_HVACAdapterDouble):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("An unrelated device adapter was constructed")

    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", FakeEVAdapter)
    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", UnexpectedAdapter)
    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", UnexpectedAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {"switch.ev": "on"},
        "climate_automations": {"automation.hvac": "off"},
        "hvac_control": {"phase": "preconditioning"},
        "enphase_profile": "AI Optimisation",
        "enphase_profile_changed_at": "2026-01-01T00:00:00+00:00",
    }
    hass = FakeHass()
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                }
            }
        }
    }
    executor = Executor(store, hass=hass, entry_id="ev-a")

    outcome = asyncio.run(executor.async_restore_device_control("ev", "ev_control_disabled"))

    assert outcome.result == OutcomeResult.RESTORED
    assert restored == [{}]
    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "off"},
        "hvac_control": {"phase": "preconditioning"},
        "enphase_profile": "AI Optimisation",
        "enphase_profile_changed_at": "2026-01-01T00:00:00+00:00",
    }
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert hass.services.calls[-1] == (
        "persistent_notification",
        "dismiss",
        {"notification_id": "ha_energy_planner_restore_safe_state_ev_ev-a"},
    )


def test_executor_restore_device_control_rejects_unknown_asset() -> None:
    executor = Executor(FakeStore(), hass=FakeHass())

    with pytest.raises(ValueError, match="Unsupported device control asset"):
        asyncio.run(executor.async_restore_device_control("unknown", "test"))


def test_executor_restore_safe_state_retains_ownership_without_hass() -> None:
    store = FakeStore()
    ownership = {
        "ev_smart_charging_state": {"switch.ev": "on"},
        "climate_automations": {"automation.hvac": "on"},
    }
    store.data["ownership"] = ownership

    outcome = asyncio.run(Executor(store).async_restore_safe_state("shutdown"))

    assert outcome.result == "failed"
    assert outcome.reason == "shutdown:home_assistant_unavailable"
    assert outcome.post_state == {
        "ownership": "retained",
        "remaining_ownership": ["climate_automations", "ev_smart_charging_state"],
    }
    assert store.data["ownership"] == ownership


def test_executor_restore_confirms_reservation_only_ev_stop() -> None:
    now = datetime.now(UTC)
    hass = FakeHass({"switch.ev_charger": "on"})
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_EV_CHARGER: "switch.ev_charger"},
        options={CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0},
        entry_id="ev-a",
    )

    outcome = asyncio.run(executor.async_restore_safe_state("entry_unload"))

    assert outcome.result == OutcomeResult.RESTORED
    assert hass.services.calls[0] == (
        "switch",
        "turn_off",
        {"entity_id": "switch.ev_charger"},
    )
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["ev_grid_reservation"] == {"active": False}


def test_executor_restore_retains_unconfirmed_reservation_only_ev_load() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.old_charging": "disconnected",
            "button.old_stop": "unknown",
            "switch.new_charger": "on",
        }
    )
    reservation = {
        "load_kw": 7.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
    }
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {"ev-a": reservation},
        }
    }
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_command_entity_id": "button.old_start",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGING: "binary_sensor.old_charging",
            CONF_EV_CHARGER_STOP: "button.old_stop",
        },
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGER: "switch.new_charger",
        },
        options={CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0},
        entry_id="ev-a",
    )

    outcome = asyncio.run(executor.async_restore_safe_state("entry_unload"))

    assert outcome.result == OutcomeResult.FAILED
    assert outcome.reason == ("entry_unload:ev_stop_not_confirmed:enphase_ai_profile_not_configured")
    assert hass.services.calls[0] == (
        "button",
        "press",
        {"entity_id": "button.old_stop"},
    )
    assert all(call[2].get("entity_id") != "switch.new_charger" for call in hass.services.calls)
    retained = hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]
    assert retained["retain_when_unloaded"] is True
    assert store.data["ev_grid_reservation"]["active"] is True
    assert store.data["ownership"] == {
        "ev_smart_charging_command_entity_id": "button.old_start",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGING: "binary_sensor.old_charging",
            CONF_EV_CHARGER_STOP: "button.old_stop",
        },
    }


def test_executor_restore_uses_owned_ev_control_topology_after_reconfigure() -> None:
    hass = FakeHass(
        {
            "binary_sensor.old_charging": "off",
            "button.old_start": "unknown",
            "input_boolean.old_stop": "on",
            "button.new_start": "unknown",
            "input_boolean.new_stop": "on",
        }
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {
            CONF_EV_CHARGER_START: "unknown",
            CONF_EV_CHARGER_STOP: "on",
        },
        "ev_smart_charging_command_entity_id": "button.old_start",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGING: "binary_sensor.old_charging",
            CONF_EV_CHARGER_START: "button.old_start",
            CONF_EV_CHARGER_STOP: "input_boolean.old_stop",
        },
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGER_START: "button.new_start",
            CONF_EV_CHARGER_STOP: "input_boolean.new_stop",
        },
    )

    outcome = asyncio.run(executor.async_restore_safe_state("entry_unload"))

    assert outcome.result == OutcomeResult.RESTORED
    assert store.data["ownership"] == {}
    assert hass.services.calls[0] == (
        "input_boolean",
        "turn_off",
        {"entity_id": "input_boolean.old_stop"},
    )
    assert all(call[2].get("entity_id") != "input_boolean.new_stop" for call in hass.services.calls)
    assert hass.states.values["input_boolean.new_stop"] == "on"


def test_executor_restore_safe_state_continues_after_asset_exception(monkeypatch: object) -> None:
    class RaisingEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any], **kwargs: Any) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            raise RuntimeError("service failure")

    class SuccessfulDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            return SimpleNamespace(
                applied=True,
                reason="hvac_restored",
                pre_state={"automation.hvac": "off"},
                post_state={"automation.hvac": "on"},
            )

    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", RaisingEVAdapter)
    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", SuccessfulDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {"switch.ev": "on"},
        "climate_automations": {"automation.hvac": "on"},
    }

    outcome = asyncio.run(Executor(store, hass=FakeHass()).async_restore_safe_state("manual"))

    assert outcome.result == "failed"
    assert outcome.reason == "manual:ev_restore_exception:hvac_restored:enphase_ai_profile_not_configured"
    assert outcome.post_state["automation.hvac"] == "on"
    assert outcome.post_state["ownership"] == "partially_cleared"
    assert store.data["ownership"] == {"ev_smart_charging_state": {"switch.ev": "on"}}


def test_executor_restore_clears_ev_and_retains_failed_hvac_and_enphase(monkeypatch: object) -> None:
    class SuccessfulEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any], **kwargs: Any) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            return SimpleNamespace(applied=True, reason="ev_restored", pre_state={}, post_state={})

    class FailedDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            return SimpleNamespace(applied=False, reason="hvac_unavailable", pre_state={}, post_state={})

    class RaisingEnphaseAdapter:
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore_ai(self) -> object:
            raise RuntimeError("service failure")

    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", SuccessfulEVAdapter)
    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FailedDaikinAdapter)
    monkeypatch.setattr(executor_module, "EnphaseProfileAdapter", RaisingEnphaseAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {"switch.ev": "on"},
        "climate_automations": {"automation.hvac": "on"},
        "enphase_profile": "AI Optimisation",
    }

    outcome = asyncio.run(Executor(store, hass=FakeHass()).async_restore_safe_state("manual"))

    assert outcome.result == "failed"
    assert outcome.reason == "manual:ev_restored:hvac_unavailable:enphase_restore_exception"
    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "on"},
        "enphase_profile": "AI Optimisation",
        "hvac_control": {
            "zone_states": {},
            "required_evidence_lost": "hvac_release_failed",
        },
    }


def test_executor_restore_retains_hvac_after_adapter_exception(monkeypatch: object) -> None:
    class RaisingDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, state: dict[str, Any]) -> object:
            raise RuntimeError("service failure")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", RaisingDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {"climate_automations": {"automation.hvac": "on"}}

    outcome = asyncio.run(Executor(store, hass=FakeHass()).async_restore_safe_state("manual"))

    assert outcome.result == "failed"
    assert "hvac_restore_exception" in outcome.reason
    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {},
            "required_evidence_lost": "hvac_release_failed",
        },
    }


def test_safe_state_exception_does_not_reintroduce_inflight_manual_hvac_changes(
    monkeypatch: object,
) -> None:
    class RaisingDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, *args: object) -> object:
            assert executor.mark_pending_hvac_manual_override() is True
            assert executor.mark_pending_hvac_zone_manual_override("climate.bedrooms") is True
            raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", RaisingDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {
                "climate.bedrooms": {"target_temperature": 21},
                "switch.living": "off",
            },
            "main_state": {"hvac_mode": "off", "target_temperature": 20},
            "phase": "peak_coast",
        },
    }
    executor = Executor(store, hass=FakeHass())

    outcome = asyncio.run(executor.async_restore_safe_state("manual"))

    assert outcome.result == OutcomeResult.FAILED
    assert "hvac_restore_exception" in outcome.reason
    assert store.flush_count == 1
    assert store.data["ownership"] == {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {"switch.living": "off"},
            "phase": "peak_coast",
            "required_evidence_lost": "hvac_release_failed",
        },
    }


def test_safe_state_failed_result_filters_inflight_manual_zone(
    monkeypatch: object,
) -> None:
    class FailedDaikinAdapter(_HVACAdapterDouble):
        def __init__(self, hass: object, entry_data: dict[str, Any]) -> None:
            pass

        async def async_restore(self, *args: object) -> object:
            assert executor.mark_pending_hvac_zone_manual_override("climate.bedrooms") is True
            return SimpleNamespace(
                applied=False,
                rollback_succeeded=False,
                reason="hvac_release_failed",
                pre_state={},
                post_state={},
                saved_automation_states={"automation.hvac": "on"},
                saved_zone_states={
                    "climate.bedrooms": {"target_temperature": 21},
                    "switch.living": "off",
                },
                saved_main_state={},
                command_sent=False,
            )

    monkeypatch.setattr(executor_module, "DaikinHVACAdapter", FailedDaikinAdapter)
    store = FakeStore()
    store.data["ownership"] = {
        "climate_automations": {"automation.hvac": "on"},
        "hvac_control": {
            "zone_states": {
                "climate.bedrooms": {"target_temperature": 21},
                "switch.living": "off",
            },
            "phase": "peak_coast",
        },
    }
    executor = Executor(store, hass=FakeHass())

    outcome = asyncio.run(executor.async_restore_safe_state("manual"))

    assert outcome.result == OutcomeResult.FAILED
    assert store.data["ownership"]["hvac_control"]["zone_states"] == {
        "switch.living": "off",
    }


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
    ev_stop = PlanAction("ev-stop", "plan-1", now, now, ActionAsset.EV, ActionKind.EV_STOP, {}, [], [], None, 1.0)
    hvac = PlanAction("hvac", "plan-1", now, now, ActionAsset.DAIKIN, ActionKind.SET_HVAC, {}, [], [], None, 1.0)
    enphase = PlanAction(
        "enphase", "plan-1", now, now, ActionAsset.ENPHASE, ActionKind.SET_PROFILE, {}, [], [], None, 1.0
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


@pytest.mark.parametrize("interruption", ["none", "failure", "cancel"])
@pytest.mark.parametrize("barrier_phase", ["persistence", "dispatch"])
def test_concurrent_ev_starts_hold_capacity_across_persistence_and_dispatch(interruption, barrier_phase) -> None:
    async def run() -> None:
        persisted = asyncio.Event()
        release_save = asyncio.Event()

        class BarrierStore(FakeStore):
            async def async_flush(self) -> None:
                if self.flush_count == 0 and barrier_phase == "persistence":
                    persisted.set()
                    await release_save.wait()
                await super().async_flush()

        now = datetime.now(UTC)
        hass = FakeHass({"switch.a": "off", "switch.b": "off", "binary_sensor.connected": "on"})
        hass.data = {}
        hass.config_entries = SimpleNamespace(async_entries=lambda domain: [
            SimpleNamespace(entry_id=name, runtime_data=object()) for name in ("a", "b")
        ])
        options = {
            CONF_EV_CHARGE_RATE_KW: 7.0, CONF_GRID_IMPORT_LIMIT_KW: 10.0,
            CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0, CONF_EV_CONFIRMATION_RETRIES: 0,
        }
        first = Executor(BarrierStore(), hass=hass, entry_id="a", options=options,
                         entry_data={CONF_EV_CHARGER: "switch.a", CONF_EV_CONNECTED: "binary_sensor.connected"})
        second = Executor(FakeStore(), hass=hass, entry_id="b", options=options,
                          entry_data={CONF_EV_CHARGER: "switch.b", CONF_EV_CONNECTED: "binary_sensor.connected"})
        context = _context(now)
        context.ev_connected = True
        context.slots[0].projected_ev_load_kw = 7.0
        original = hass.services.async_call

        async def accept_or_interrupt(domain, service, data, **kwargs):
            await original(domain, service, data, **kwargs)
            if data.get("entity_id") == "switch.a" and service == "turn_on":
                if barrier_phase == "dispatch":
                    persisted.set()
                    await release_save.wait()
                if interruption == "failure":
                    raise RuntimeError("accepted but response failed")
                if interruption == "cancel":
                    raise asyncio.CancelledError

        hass.services.async_call = accept_or_interrupt
        task = asyncio.create_task(first.async_manual_ev_charging(True, context))
        try:
            await asyncio.wait_for(persisted.wait(), timeout=2)
            assert hass.states.values["switch.a"] == ("off" if barrier_phase == "persistence" else "on")
            rejected = await asyncio.wait_for(second.async_manual_ev_charging(True, context), timeout=2)
            assert rejected.reason == "multi_ev_grid_import_limit_exceeded"
            assert hass.states.values["switch.b"] == "off"
            reservations = hass.data["ha_energy_planner"]["ev_grid_reservations"]
            assert set(reservations) == {"a"}
            assert sum(item["load_kw"] for item in reservations.values()) + 1 <= 10
            release_save.set()
            if interruption == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert set(reservations) == {"a"}
                assert first.store.data["ownership"]
            else:
                result = await task
                assert result.applied is (interruption == "none")
                if interruption == "failure":
                    assert result.rollback_succeeded is True
                    assert reservations == {}
            hass.services.async_call = original
            # Confirmed stop releases both successful and uncertain accepted starts.
            stopped = await first.async_manual_ev_charging(False, context)
            assert stopped.applied
            assert reservations == {}
            admitted = await second.async_manual_ev_charging(True, context)
            assert admitted.applied
            assert set(reservations) == {"b"}
            assert hass.states.values["switch.a"] == "off"
            assert hass.states.values["switch.b"] == "on"
        finally:
            release_save.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("stop_confirmed", [True, False])
def test_concurrent_ev_start_cannot_use_capacity_until_stop_is_confirmed(stop_confirmed) -> None:
    async def run() -> None:
        stopping = asyncio.Event()
        release_stop = asyncio.Event()
        hass = FakeHass({"switch.a": "off", "switch.b": "off", "binary_sensor.connected": "on"})
        hass.data = {}
        hass.config_entries = SimpleNamespace(async_entries=lambda domain: [
            SimpleNamespace(entry_id=name, runtime_data=object()) for name in ("a", "b")
        ])
        options = {
            CONF_EV_CHARGE_RATE_KW: 7.0, CONF_GRID_IMPORT_LIMIT_KW: 10.0,
            CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0, CONF_EV_CONFIRMATION_RETRIES: 0,
        }
        first, second = [
            Executor(FakeStore(), hass=hass, entry_id=name, options=options,
                     entry_data={CONF_EV_CHARGER: f"switch.{name}", CONF_EV_CONNECTED: "binary_sensor.connected"})
            for name in ("a", "b")
        ]
        context = _context(datetime.now(UTC))
        context.ev_connected = True
        context.slots[0].projected_ev_load_kw = 7.0
        assert (await first.async_manual_ev_charging(True, context)).applied
        original = hass.services.async_call

        async def delayed_stop(domain, service, data, **kwargs):
            if data.get("entity_id") == "switch.a" and service == "turn_off":
                stopping.set()
                await release_stop.wait()
                if not stop_confirmed:
                    return
            await original(domain, service, data, **kwargs)

        hass.services.async_call = delayed_stop
        task = asyncio.create_task(first.async_manual_ev_charging(False, context))
        try:
            await asyncio.wait_for(stopping.wait(), timeout=2)
            waiting = await second.async_manual_ev_charging(True, context)
            assert waiting.reason == "multi_ev_grid_import_limit_exceeded"
            release_stop.set()
            stopped = await task
            assert stopped.applied is stop_confirmed
            result = await second.async_manual_ev_charging(True, context)
            assert result.applied is stop_confirmed
            reservations = hass.data["ha_energy_planner"]["ev_grid_reservations"]
            assert set(reservations) == ({"b"} if stop_confirmed else {"a"})
            assert not (hass.states.values["switch.a"] == hass.states.values["switch.b"] == "on")
        finally:
            release_stop.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_multiple_ev_entries_share_atomic_grid_capacity_reservations() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_a_connected": "on",
            "binary_sensor.ev_b_connected": "on",
            "input_boolean.ev_a_charger": "off",
            "input_boolean.ev_b_charger": "off",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id="ev-a", runtime_data=object()),
            SimpleNamespace(entry_id="ev-b", runtime_data=object()),
        ]
    )
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
        CONF_EV_CHARGE_RATE_KW: 7.0,
        CONF_GRID_IMPORT_LIMIT_KW: 10.0,
    }
    store_a = FakeStore()
    store_b = FakeStore()
    executor_a = Executor(
        store_a,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_a_connected",
            CONF_EV_CHARGER: "input_boolean.ev_a_charger",
        },
        options=options,
        entry_id="ev-a",
    )
    executor_b = Executor(
        store_b,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_b_connected",
            CONF_EV_CHARGER: "input_boolean.ev_b_charger",
        },
        options=options,
        entry_id="ev-b",
    )
    _arm_store(store_a, executor_a)
    _arm_store(store_b, executor_b)

    def plan_for(entry_id: str, kind: ActionKind, charging: bool) -> EnergyPlan:
        action = PlanAction(
            action_id=f"{entry_id}-{kind}",
            plan_id=f"plan-{entry_id}-{kind}",
            execute_not_before=now - timedelta(minutes=1),
            execute_not_after=now + timedelta(minutes=1),
            asset=ActionAsset.EV,
            kind=kind,
            desired_state={
                "charging_required_now": charging,
                "projected_load_kw_now": 7.0 if charging else 0.0,
            },
            hard_constraints=[],
            reason_codes=[],
            expected_cost_delta=None,
            confidence=1.0,
        )
        return EnergyPlan(
            plan_id=action.plan_id,
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

    context_a = _context(now)
    context_a.ev_connected = True
    context_a.slots[0].projected_ev_load_kw = 7.0
    context_b = _context(now)
    context_b.ev_connected = True
    context_b.slots[0].projected_ev_load_kw = 7.0

    asyncio.run(executor_a.async_evaluate(plan_for("ev-a", ActionKind.EV_START, True), context_a))
    asyncio.run(executor_b.async_evaluate(plan_for("ev-b", ActionKind.EV_START, True), context_b))

    assert store_a.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store_b.data["outcomes"][-1].result == OutcomeResult.REJECTED
    assert store_b.data["outcomes"][-1].reason == "multi_ev_grid_import_limit_exceeded"
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-a"}

    asyncio.run(executor_a.async_evaluate(plan_for("ev-a", ActionKind.EV_STOP, False), context_a))
    asyncio.run(executor_b.async_evaluate(plan_for("ev-b", ActionKind.EV_START, True), context_b))

    assert store_a.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store_b.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-b"}

    disconnected = _context(now)
    disconnected.ev_connected = False
    empty_plan = plan_for("ev-b", ActionKind.EV_STOP, False)
    empty_plan.actions = []
    asyncio.run(executor_b.async_evaluate(empty_plan, disconnected))

    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store_b.data["ownership"] == {}
    assert store_b.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store_b.data["outcomes"][-1].desired_state["charging_reason"] == "ev_disconnected_safety_stop"


def test_manual_ev_commands_share_capacity_and_release_it_on_stop() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_a_connected": "on",
            "binary_sensor.ev_b_connected": "on",
            "input_boolean.ev_a_charger": "off",
            "input_boolean.ev_b_charger": "off",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id="ev-a", runtime_data=object()),
            SimpleNamespace(entry_id="ev-b", runtime_data=object()),
        ]
    )
    options = {
        CONF_EV_CHARGE_RATE_KW: 7.0,
        CONF_GRID_IMPORT_LIMIT_KW: 10.0,
    }
    store_a = FakeStore()
    store_b = FakeStore()
    executor_a = Executor(
        store_a,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_a_connected",
            CONF_EV_CHARGER: "input_boolean.ev_a_charger",
        },
        options=options,
        entry_id="ev-a",
    )
    executor_b = Executor(
        store_b,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_b_connected",
            CONF_EV_CHARGER: "input_boolean.ev_b_charger",
        },
        options=options,
        entry_id="ev-b",
    )
    context_a = _context(now)
    context_b = _context(now)

    started_a = asyncio.run(executor_a.async_manual_ev_charging(True, context_a))
    blocked_b = asyncio.run(executor_b.async_manual_ev_charging(True, context_b))
    assert store_a.data["ownership"]["ev_smart_charging_command_entity_id"] == ("input_boolean.ev_a_charger")

    stopped_a = asyncio.run(executor_a.async_manual_ev_charging(False, None))
    started_b = asyncio.run(executor_b.async_manual_ev_charging(True, context_b))

    assert started_a.applied is True
    assert blocked_b.applied is False
    assert blocked_b.reason == "multi_ev_grid_import_limit_exceeded"
    assert stopped_a.applied is True
    assert store_a.data["ownership"] == {}
    assert started_b.applied is True
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-b"}


def test_manual_ev_start_is_safety_stopped_while_planner_is_disabled_and_dry_run() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "input_boolean.ev_charger": "off",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id="ev-a", runtime_data=object()),
            SimpleNamespace(entry_id="ev-b", runtime_data=object()),
        ]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "input_boolean.ev_charger",
        },
        options={
            **DEFAULT_OPTIONS,
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    started = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))
    assert started.applied is True
    assert hass.states.values["input_boolean.ev_charger"] == "on"

    inactive_context = _context(now)
    inactive_context.ev_connected = True
    inactive_context.active_overrides = [
        Override(
            kind="manual_ev_charging",
            source="button",
            expires_at=now + timedelta(hours=1),
            reason="manual_start",
        )
    ]
    inactive_plan = EnergyPlan(
        plan_id="inactive-disconnect",
        created_at=now - timedelta(minutes=10),
        horizon_hours=24,
        interval_minutes=5,
        status="disabled",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DISABLED,
        summary="Planner disabled and dry-run enabled",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )

    asyncio.run(executor.async_evaluate(inactive_plan, inactive_context))

    assert hass.states.values["input_boolean.ev_charger"] == "on"
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-a"}

    hass.states.values["binary_sensor.ev_connected"] = "off"
    inactive_context.ev_connected = False
    asyncio.run(executor.async_evaluate(inactive_plan, inactive_context))

    assert hass.states.values["input_boolean.ev_charger"] == "off"
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["ownership"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].desired_state["charging_reason"] == "ev_disconnected_safety_stop"


def test_manual_ev_stop_uses_owned_topology_after_reconfigure() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "switch.old_charger": "on",
            "switch.new_charger": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
        "ev_smart_charging_command_entity_id": "switch.old_charger",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGER: "switch.old_charger",
        },
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_EV_CHARGER: "switch.new_charger"},
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    result = asyncio.run(executor.async_manual_ev_charging(False, None))

    assert result.applied is True
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.old_charger"})]
    assert hass.states.values["switch.old_charger"] == "off"
    assert hass.states.values["switch.new_charger"] == "on"
    assert store.data["ownership"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].service_target == "switch.old_charger"
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["outcomes"][-1].service_target == "switch.old_charger"


def test_ev_auto_start_compensation_uses_audited_stop_path() -> None:
    hass = FakeHass({"input_boolean.ev_charger": "on"})
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGER: "input_boolean.ev_charger",
            CONF_EV_CHARGING: "input_boolean.ev_charger",
        },
    )

    result = asyncio.run(executor.async_compensate_ev_auto_start(None))

    assert result.applied is True
    assert hass.states.values["input_boolean.ev_charger"] == "off"
    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.ev_charger"})]
    outcome = store.data["outcomes"][-1]
    assert outcome.action_id == "ev_auto_start_compensation"
    assert outcome.kind == "ev_stop"
    assert outcome.desired_state["charging_reason"] == "ev_auto_start_compensation"


def test_compensated_manual_ev_stop_is_successful() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "unavailable",
            "switch.ev_charger": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
        "ev_smart_charging_command_entity_id": "switch.ev_charger",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGER: "switch.ev_charger",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
        },
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGER: "switch.ev_charger",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
        },
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
            CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0,
            CONF_EV_CONFIRMATION_RETRIES: 0,
        },
        entry_id="ev-a",
    )

    result = asyncio.run(executor.async_manual_ev_charging(False, None))

    assert result.applied is True
    assert result.reason == "ev_safe_stop_compensated"
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.ev_charger"}),
        ("switch", "turn_off", {"entity_id": "switch.ev_charger"}),
    ]
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert "control_pause" not in store.data
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].reason == "ev_safe_stop_compensated"


def test_unconfirmed_owned_manual_ev_stop_fails_closed() -> None:
    now = datetime.now(UTC)
    reservation = {
        "load_kw": 7.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
    }
    hass = FakeHass(
        {
            "input_boolean.ev_start": "off",
            "input_boolean.ev_stop": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {"ev-a": reservation},
        }
    }
    ownership = {
        "ev_smart_charging_state": {CONF_EV_CHARGER_START: "off"},
        "ev_smart_charging_command_entity_id": "input_boolean.ev_start",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGER_START: "input_boolean.ev_start",
            CONF_EV_CHARGER_STOP: "input_boolean.ev_stop",
        },
    }
    store = FakeStore()
    store.data["ownership"] = ownership
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGER_START: "input_boolean.ev_start",
            CONF_EV_CHARGER_STOP: "input_boolean.ev_stop",
        },
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    result = asyncio.run(executor.async_manual_ev_charging(False, None))

    assert result.applied is False
    assert result.reason == "ev_stop_not_confirmed"
    assert store.data["ownership"] == ownership
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"] == {
        **reservation,
        "retain_when_unloaded": True,
    }
    assert store.data["control_pause"]["reason"] == "ev_stop_not_confirmed"
    assert store.data["outcomes"][-1].result == OutcomeResult.FAILED


def test_manual_ev_control_fails_closed_without_home_assistant() -> None:
    store = FakeStore()
    executor = Executor(store)

    result = asyncio.run(executor.async_manual_ev_charging(False, None))

    assert result.applied is False
    assert result.reason == "home_assistant_unavailable"
    assert store.data["outcomes"][-1].result == OutcomeResult.REJECTED


def test_manual_ev_start_rejects_missing_grid_projection_context() -> None:
    hass = FakeHass()
    hass.data = {}
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    result = asyncio.run(executor.async_manual_ev_charging(True, None))
    unavailable_context = _context(datetime.now(UTC))
    unavailable_context.slots[0].baseline_load_forecast_kw = None
    unavailable_context.slots[0].pv_forecast_kw = None
    unavailable_result = asyncio.run(executor.async_manual_ev_charging(True, unavailable_context))
    stale_context = _context(datetime.now(UTC) - timedelta(hours=3))
    stale_result = asyncio.run(executor.async_manual_ev_charging(True, stale_context))
    expired_plan_context = _context(datetime.now(UTC) - timedelta(minutes=6))
    expired_plan_result = asyncio.run(executor.async_manual_ev_charging(True, expired_plan_context))
    future_context = _context(datetime.now(UTC) + timedelta(minutes=1))
    future_result = asyncio.run(executor.async_manual_ev_charging(True, future_context))
    unsafe_context = _context(datetime.now(UTC))
    unsafe_context.input_health = InputHealth.UNSAFE
    unsafe_result = asyncio.run(executor.async_manual_ev_charging(True, unsafe_context))
    naive_context = _context(datetime.now(UTC))
    naive_context.created_at = datetime.now()
    naive_result = asyncio.run(executor.async_manual_ev_charging(True, naive_context))

    assert result.applied is False
    assert result.reason == "ev_grid_projection_unavailable"
    assert unavailable_result.applied is False
    assert unavailable_result.reason == "ev_grid_projection_unavailable"
    assert stale_result.reason == "ev_grid_projection_stale"
    assert expired_plan_result.reason == "ev_grid_projection_stale"
    assert future_result.reason == "ev_grid_projection_stale"
    assert unsafe_result.reason == "ev_grid_projection_unsafe"
    assert naive_result.reason == "ev_grid_projection_stale"
    assert hass.services.calls == []
    assert all(outcome.result == OutcomeResult.REJECTED for outcome in executor.store.data["outcomes"])


def test_manual_ev_start_rejects_control_without_safe_stop_path() -> None:
    now = datetime.now(UTC)
    hass = FakeHass({"button.ev_control": "unknown"})
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_EV_SMART_CHARGING: "button.ev_control"},
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    result = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))

    assert result.applied is False
    assert result.reason == "ev_stop_control_unsupported"
    assert executor.ev_start_feedback_expected_until is None
    assert hass.services.calls == []
    assert (
        hass.data.get("ha_energy_planner", {}).get(
            "ev_grid_reservations",
            {},
        )
        == {}
    )
    assert store.data["ownership"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.REJECTED


def test_successful_manual_ev_start_is_owned_and_restorable() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "input_boolean.ev_charger": "off",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "input_boolean.ev_charger",
        },
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    started = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))

    assert started.applied is True
    assert executor.ev_start_feedback_expected_until is not None
    assert store.data["ownership"]["ev_smart_charging_state"][CONF_EV_CHARGER] == "off"
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-a"}

    restored = asyncio.run(executor.async_restore_safe_state("manual"))

    assert restored.result == OutcomeResult.RESTORED
    assert hass.states.values["input_boolean.ev_charger"] == "off"
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}


def test_manual_ev_start_adopts_and_restores_already_active_charger() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_charger": "on",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_charger",
        },
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    started = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))

    assert started.applied is True
    assert started.reason == "already_in_desired_state"
    assert started.command_sent is False
    assert executor.ev_start_feedback_expected_until is None
    assert store.data["ownership"]["ev_smart_charging_state"] == {
        CONF_EV_CHARGER: "on",
        CONF_EV_CONNECTED: "on",
    }
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-a"}
    assert hass.services.calls == []

    restored = asyncio.run(executor.async_restore_safe_state("entry_unload"))

    assert restored.result == OutcomeResult.RESTORED
    assert hass.states.values["switch.ev_charger"] == "on"
    assert all(call[1] != "turn_off" for call in hass.services.calls)
    assert store.data["ownership"] == {}
    reservation = hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]
    assert reservation["external_baseline"] is True
    assert reservation["retain_when_unloaded"] is True
    assert store.data["ev_grid_reservation"]["active"] is True
    assert store.data["ev_grid_reservation"]["external_baseline"] is True
    assert executor._has_ev_grid_reservation() is False

    other_executor = Executor(
        FakeStore(),
        hass=hass,
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-b",
    )
    reason, _previous = other_executor._reserve_ev_grid_capacity(
        SimpleNamespace(
            kind=ActionKind.EV_START,
            desired_state={"projected_load_kw_now": 7.0},
        ),
        _context(now),
        now,
    )

    assert reason == "multi_ev_grid_import_limit_exceeded"


def test_external_ev_reservation_helper_handles_missing_runtime_state() -> None:
    Executor(FakeStore())._retain_external_ev_grid_reservation()

    hass = FakeHass()
    hass.data = {}
    executor = Executor(FakeStore(), hass=hass, entry_id="ev-a")

    executor._retain_external_ev_grid_reservation()

    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}


def test_manual_ev_uncertain_failure_retains_reservation_and_ownership(
    monkeypatch: object,
) -> None:
    result = SimpleNamespace(
        applied=False,
        reason="ev_charging_confirmation_timeout",
        pre_state={CONF_EV_SMART_CHARGING_START: "off"},
        post_state={CONF_EV_SMART_CHARGING_START: "on"},
        command_sent=True,
        rollback_succeeded=False,
    )

    class UncertainAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_set_charging(self, enabled: bool) -> Any:
            assert enabled is True
            assert store.data["ev_grid_reservation"]["active"] is True
            assert store.data["ownership"][
                "ev_smart_charging_command_entity_id"
            ] == executor_module._ev_command_entity_for_action(
                SimpleNamespace(
                    asset=ActionAsset.EV,
                    kind=ActionKind.EV_START,
                    desired_state={"charging_required_now": True},
                ),
                executor.entry_data,
            )
            return result

    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", UncertainAdapter)
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "button.ev_start": "unknown",
            "button.ev_stop": "unknown",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "button.ev_stop",
        },
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    actual = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))

    assert actual is result
    assert store.data["ownership"]["ev_smart_charging_state"] == result.pre_state
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-a"}
    reservation = hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]
    assert reservation["retain_when_unloaded"] is True
    assert store.data["control_pause"]["reason"] == "ev_charging_confirmation_timeout"
    assert executor.ev_start_feedback_expected_until is not None
    assert isinstance(store.data["command_rate_limits"]["ev:ev_start"], datetime)
    assert store.data["outcomes"][-1].result == OutcomeResult.FAILED
    assert store.data["outcomes"][-1].action_id == "manual_ev_start"

    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])
    executor._discard_stale_ev_grid_reservations(hass.data["ha_energy_planner"]["ev_grid_reservations"])

    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-a"}


def test_manual_ev_rolled_back_start_clears_provisional_recovery_state(
    monkeypatch: object,
) -> None:
    class RolledBackAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_set_charging(self, enabled: bool) -> Any:
            assert enabled is True
            return SimpleNamespace(
                applied=False,
                reason="ev_charging_confirmation_timeout",
                pre_state={CONF_EV_CHARGER: "off"},
                post_state={CONF_EV_CHARGER: "off"},
                command_sent=True,
                rollback_succeeded=True,
                safe_state_confirmed=True,
            )

    monkeypatch.setattr(
        executor_module,
        "EVSmartChargingAdapter",
        RolledBackAdapter,
    )
    now = datetime.now(UTC)
    hass = FakeHass({"switch.ev_charger": "off"})
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_EV_CHARGER: "switch.ev_charger"},
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    result = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))

    assert result.rollback_succeeded is True
    assert executor.ev_start_feedback_expected_until is None
    assert store.data["ownership"] == {}
    assert store.data["ev_grid_reservation"] == {"active": False}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}


def test_ev_grid_reservation_counts_load_missing_from_context_projection() -> None:
    now = datetime.now(UTC)
    hass = FakeHass()
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 8.0
    action = SimpleNamespace(
        kind=ActionKind.EV_START,
        desired_state={"projected_load_kw_now": 7.0},
    )

    reason, previous = executor._reserve_ev_grid_capacity(action, context, now)

    assert reason == "multi_ev_grid_import_limit_exceeded"
    assert previous is None
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}


def test_ev_grid_reservations_keep_only_each_entrys_configured_limit() -> None:
    now = datetime.now(UTC)
    hass = FakeHass()
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id=entry_id, runtime_data=object()) for entry_id in ("ev-a", "ev-b", "ev-c", "ev-d")
        ]
    )

    def executor(entry_id: str, limit_kw: float) -> Executor:
        return Executor(
            FakeStore(),
            hass=hass,
            options={
                CONF_EV_CHARGE_RATE_KW: 2.0,
                CONF_GRID_IMPORT_LIMIT_KW: limit_kw,
            },
            entry_id=entry_id,
        )

    def reserve(target: Executor, load_kw: float) -> str | None:
        context = _context(now)
        context.slots[0].baseline_load_forecast_kw = 0.0
        context.slots[0].projected_ev_load_kw = load_kw
        action = SimpleNamespace(
            kind=ActionKind.EV_START,
            desired_state={"projected_load_kw_now": load_kw},
        )
        reason, _previous = target._reserve_ev_grid_capacity(action, context, now)
        return reason

    executor_a = executor("ev-a", 10.0)
    executor_b = executor("ev-b", 8.0)
    executor_c = executor("ev-c", 12.0)
    executor_d = executor("ev-d", 12.0)

    assert reserve(executor_a, 2.0) is None
    assert reserve(executor_b, 2.0) is None
    assert reserve(executor_c, 2.0) is None
    reservations = hass.data["ha_energy_planner"]["ev_grid_reservations"]
    assert reservations["ev-c"]["limit_kw"] == 12.0

    executor_b._release_ev_grid_reservation()

    assert reserve(executor_d, 5.0) is None
    assert set(reservations) == {"ev-a", "ev-c", "ev-d"}


def test_active_ev_reservation_synchronizes_changed_options() -> None:
    now = datetime.now(UTC)
    reservation = {
        "load_kw": 7.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
        "retain_when_unloaded": True,
    }
    hass = FakeHass()
    hass.data = {"ha_energy_planner": {"ev_grid_reservations": {"ev-a": reservation}}}
    store = FakeStore()
    executor = Executor(
        store,
        hass=hass,
        options={
            CONF_EV_CHARGE_RATE_KW: 11.0,
            CONF_GRID_IMPORT_LIMIT_KW: 8.0,
        },
        entry_id="ev-a",
    )

    executor.sync_ev_grid_reservation()
    asyncio.run(executor.async_persist_ev_grid_reservation())

    assert reservation == {
        "load_kw": 11.0,
        "limit_kw": 8.0,
        "reserved_at": now.isoformat(),
        "retain_when_unloaded": True,
    }
    assert store.data["ev_grid_reservation"]["active"] is True
    assert store.data["ev_grid_reservation"]["load_kw"] == 11.0

    executor._release_ev_grid_reservation()
    asyncio.run(executor.async_persist_ev_grid_reservation())

    assert store.data["ev_grid_reservation"] == {"active": False}


def test_ev_grid_reservation_uses_store_persistence_api() -> None:
    class PersistingStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.saved_reservation: dict[str, Any] | None = None

        async def async_save_ev_grid_reservation(
            self,
            reservation: dict[str, Any],
        ) -> None:
            self.saved_reservation = reservation

    store = PersistingStore()
    executor = Executor(store)

    asyncio.run(executor.async_persist_ev_grid_reservation())

    assert store.saved_reservation == {"active": False}


def test_ev_keep_on_command_target_uses_persistent_control() -> None:
    action = SimpleNamespace(desired_state={"keep_charger_on": True})

    assert (
        _ev_command_entity_for_action(
            action,
            {"ev_charger_entity": "switch.ev_control"},
        )
        == "switch.ev_control"
    )


def test_active_ev_reservation_does_not_shrink_from_an_options_change() -> None:
    now = datetime.now(UTC)
    reservation = {
        "load_kw": 11.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
    }
    hass = FakeHass()
    hass.data = {"ha_energy_planner": {"ev_grid_reservations": {"ev-a": reservation}}}
    executor = Executor(
        FakeStore(),
        hass=hass,
        options={
            CONF_EV_CHARGE_RATE_KW: 3.0,
            CONF_GRID_IMPORT_LIMIT_KW: 8.0,
            CONF_PLANNING_INTERVAL_MINUTES: 5,
        },
        entry_id="ev-a",
    )

    executor.sync_ev_grid_reservation()

    assert reservation["load_kw"] == 11.0
    assert reservation["limit_kw"] == 8.0

    context = _context(now)
    context.slots[0].pv_forecast_kw = 20.0
    context.slots[0].baseline_load_forecast_kw = 0.0
    context.slots[0].projected_ev_load_kw = 3.0
    action = SimpleNamespace(
        kind=ActionKind.EV_START,
        desired_state={"projected_load_kw_now": 3.0},
    )

    reason, previous = executor._reserve_ev_grid_capacity(action, context, now)

    assert reason is None
    assert previous is reservation
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]["load_kw"] == 11.0


def test_manual_ev_start_honors_backoff_while_stop_remains_available() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "input_boolean.ev_start": "off",
            "input_boolean.ev_stop": "on",
        }
    )
    hass.data = {}
    store = FakeStore()
    store.data["control_pause"] = {
        "active": True,
        "assets": ["ev"],
        "until": now + timedelta(minutes=10),
        "reason": "ev_charging_confirmation_timeout",
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "input_boolean.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options={
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )

    start = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))
    store.data["control_pause"] = {}
    store.data["command_rate_limits"] = {"ev:ev_start": now}
    executor.options[CONF_COMMAND_RATE_LIMIT_SECONDS] = 60
    rate_limited_start = asyncio.run(executor.async_manual_ev_charging(True, _context(now)))
    stop = asyncio.run(executor.async_manual_ev_charging(False, None))

    assert start.applied is False
    assert start.reason == "ev_control_paused"
    assert store.data["outcomes"][0].result == OutcomeResult.REJECTED
    assert rate_limited_start.applied is False
    assert rate_limited_start.reason == "device_command_rate_limited"
    assert store.data["outcomes"][1].result == OutcomeResult.REJECTED
    assert stop.applied is True
    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.ev_stop"})]


def test_scheduled_ev_stop_bypasses_failure_pause_rate_limit_and_daily_cap() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "button.ev_start": "unavailable",
            "input_boolean.ev_stop": "on",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
        CONF_COMMAND_RATE_LIMIT_SECONDS: 3600,
        CONF_MAX_DAILY_EV_ACTIONS: 1,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    store.data["control_pause"] = {
        "active": True,
        "assets": ["ev"],
        "until": now + timedelta(minutes=10),
        "reason": "ev_charging_confirmation_timeout",
    }
    store.data["command_rate_limits"] = {"ev:ev_stop": now}
    store.data["execution_audit"] = [{"asset": "ev", "result": "applied", "attempted_at": now}]
    action = PlanAction(
        action_id="ev-stop",
        plan_id="plan-stop",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_STOP,
        desired_state={"charging_required_now": False},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan = EnergyPlan(
        plan_id="plan-stop",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="stop",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = True
    context.input_health = InputHealth.UNSAFE

    asyncio.run(executor.async_evaluate(plan, context))

    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.ev_stop"})]
    assert store.data["ownership"] == {}


def test_regular_planner_owned_stop_clears_ownership_and_cannot_restore_on() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_charger": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {CONF_EV_CHARGER: "on"},
        "ev_smart_charging_command_entity_id": "switch.ev_charger",
        "ev_smart_charging_control_topology": {CONF_EV_CHARGER: "switch.ev_charger"},
    }
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_charger",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    action = PlanAction(
        action_id="regular-owned-stop",
        plan_id="regular-owned-stop-plan",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"charging_required_now": False},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan = EnergyPlan(
        plan_id="regular-owned-stop-plan",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="regular stop",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = True

    asyncio.run(executor.async_evaluate(plan, context))
    calls_after_stop = list(hass.services.calls)
    asyncio.run(executor.async_restore_safe_state("entry_unload"))

    assert calls_after_stop == [("switch", "turn_off", {"entity_id": "switch.ev_charger"})]
    assert not any(
        domain == "switch" and service == "turn_on" and data.get("entity_id") == "switch.ev_charger"
        for domain, service, data in hass.services.calls
    )
    assert hass.states.values["switch.ev_charger"] == "off"
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["outcomes"][0].result == OutcomeResult.APPLIED


def test_regular_planner_owned_stop_requires_confirmed_safe_state(
    monkeypatch: object,
) -> None:
    class SupportedDiscovery:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def inspect(self) -> SupportedDiscovery:
            return self

        def for_asset(self, asset: Any) -> Any:
            return SimpleNamespace(supported=True, issues=[])

    class UnconfirmedStopAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_execute(self, action: Any) -> Any:
            return SimpleNamespace(
                applied=True,
                reason="ev_stop_command_accepted",
                pre_state={CONF_EV_CHARGER: "on"},
                post_state={CONF_EV_CHARGER: "off"},
                command_sent=True,
                rollback_succeeded=False,
                safe_state_confirmed=False,
            )

    monkeypatch.setattr(executor_module, "CapabilityDiscovery", SupportedDiscovery)
    monkeypatch.setattr(
        executor_module,
        "EVSmartChargingAdapter",
        UnconfirmedStopAdapter,
    )
    now = datetime.now(UTC)
    reservation = {
        "load_kw": 7.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
    }
    hass = FakeHass()
    hass.data = {"ha_energy_planner": {"ev_grid_reservations": {"ev-a": reservation}}}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    ownership = {
        "ev_smart_charging_state": {CONF_EV_CHARGER: "on"},
        "ev_smart_charging_command_entity_id": "switch.ev_charger",
    }
    store.data["ownership"] = ownership
    executor = Executor(
        store,
        hass=hass,
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
        },
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    action = PlanAction(
        "regular-unconfirmed-stop",
        "regular-unconfirmed-plan",
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        ActionAsset.EV,
        ActionKind.EV_STOP,
        {"charging_required_now": False},
        [],
        [],
        None,
        1.0,
    )
    plan = EnergyPlan(
        "regular-unconfirmed-plan",
        now,
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "regular unconfirmed stop",
        1.0,
        None,
        [action],
        [],
    )
    context = _context(now)
    context.ev_connected = True

    asyncio.run(executor.async_evaluate(plan, context))

    assert store.data["outcomes"][-1].result == OutcomeResult.FAILED
    assert store.data["outcomes"][-1].reason == "ev_stop_not_confirmed"
    assert store.data["ownership"] == ownership
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]["retain_when_unloaded"] is True


def test_unhealthy_plan_stops_and_releases_planner_owned_ev_power() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "off",
            "button.ev_start": "unavailable",
            "input_boolean.ev_stop": "on",
        }
    )
    hass.data = {}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {
            CONF_EV_SMART_CHARGING_START: "off",
        },
        "ev_smart_charging_command_entity_id": "button.ev_start",
    }
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    plan = EnergyPlan(
        plan_id="unsafe-plan",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="unsafe",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="unsafe",
        confidence=0.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    context = _context(now)
    context.input_health = InputHealth.UNSAFE

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.ev_stop"})]
    assert store.data["ownership"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].desired_state["input_health_safety_stop"] is True

    asyncio.run(executor.async_evaluate(plan, context))

    assert len(hass.services.calls) == 1
    assert len(store.data["outcomes"]) == 1


def test_unrelated_degraded_inputs_do_not_stop_planner_owned_ev_power() -> None:
    now = datetime.now(UTC)
    store = FakeStore()
    store.data["ownership"] = {"ev_smart_charging_state": {"switch.ev": "on"}}
    executor = Executor(store)
    context = _context(now)
    context.input_health = InputHealth.DEGRADED
    context.input_issues = ["occupancy_unknown", "daikin_climate_unavailable"]
    plan = SimpleNamespace(
        plan_id="degraded-plan",
        created_at=now,
        interval_minutes=5,
        input_issues=[],
    )

    assert executor._owned_ev_safety_stop(plan, context) is None

    context.input_issues = ["ev_soc_unavailable"]
    safety_stop = executor._owned_ev_safety_stop(plan, context)
    assert safety_stop is not None
    assert safety_stop.desired_state["input_health_safety_stop"] is True


@pytest.mark.parametrize(
    ("source", "issue"),
    [
        (
            CONF_AMBER_IMPORT_PRICE,
            "amber_import_price_entity_forecast_coverage_degraded",
        ),
        (CONF_PV_FORECAST, "pv_forecast_entity_forecast_coverage_degraded"),
    ],
)
def test_ev_relevant_low_confidence_stops_planner_owned_ev_power(
    source: str,
    issue: str,
) -> None:
    now = datetime.now(UTC)
    store = FakeStore()
    store.data["ownership"] = {"ev_smart_charging_state": {"switch.ev": "on"}}
    executor = Executor(store, options=dict(DEFAULT_OPTIONS))
    context = _context(now)
    context.input_health = InputHealth.DEGRADED
    context.input_issues = [issue]
    context.forecast_confidence_by_source = {source: 0.4}
    plan = SimpleNamespace(
        plan_id="degraded-plan",
        created_at=now,
        interval_minutes=5,
        input_issues=[],
    )

    safety_stop = executor._owned_ev_safety_stop(plan, context)

    assert safety_stop is not None
    assert safety_stop.desired_state["charging_reason"] == "ev_input_health_safety_stop"
    assert safety_stop.desired_state["input_health_safety_stop"] is True


def test_grid_degraded_plan_replaces_owned_ev_start_with_safety_stop() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "off",
            "button.ev_start": "unknown",
            "input_boolean.ev_stop": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {
            CONF_EV_SMART_CHARGING_START: "unknown",
        },
        "ev_smart_charging_command_entity_id": "button.ev_start",
    }
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    action = PlanAction(
        action_id="ev-start",
        plan_id="grid-degraded-plan",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={
            "charging_required_now": True,
            "projected_load_kw_now": 7.0,
        },
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan = EnergyPlan(
        plan_id="grid-degraded-plan",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="grid limit exceeded",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
        input_issues=["grid_import_limit_exceeded"],
    )
    context = _context(now)
    context.ev_connected = True

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.ev_stop"})]
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].desired_state["charging_reason"] == "ev_grid_import_limit_exceeded_safety_stop"


def test_disabled_ev_control_safely_reconciles_interrupted_restore() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "off",
            "button.ev_start": "unknown",
            "input_boolean.ev_stop": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {
            CONF_EV_SMART_CHARGING_START: "unknown",
        },
        "ev_smart_charging_command_entity_id": "button.ev_start",
    }
    enabled_options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "input_boolean.ev_stop",
        },
        options=enabled_options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    executor.options = {**enabled_options, CONF_EV_CONTROL_ENABLED: False}
    action = PlanAction(
        action_id="ev-start",
        plan_id="ev-control-disabled-plan",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={
            "charging_required_now": True,
            "projected_load_kw_now": 7.0,
        },
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan = EnergyPlan(
        plan_id="ev-control-disabled-plan",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="EV control disabled",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = True

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.ev_stop"})]
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].desired_state["charging_reason"] == "ev_control_disabled_safety_stop"


def test_disconnected_ev_retains_capacity_when_safety_stop_fails(
    monkeypatch: object,
) -> None:
    now = datetime.now(UTC)

    class SupportedDiscovery:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def inspect(self) -> SupportedDiscovery:
            return self

        def for_asset(self, asset: Any) -> Any:
            return SimpleNamespace(supported=True, issues=[])

    class FailedStopAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_execute(self, action: Any) -> Any:
            return SimpleNamespace(
                applied=False,
                reason="ev_stop_failed",
                pre_state={"input_boolean.ev_stop": "on"},
                post_state={"input_boolean.ev_stop": "on"},
                command_sent=True,
                rollback_succeeded=False,
            )

    monkeypatch.setattr(executor_module, "CapabilityDiscovery", SupportedDiscovery)
    monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", FailedStopAdapter)
    reservation = {
        "load_kw": 7.0,
        "limit_kw": 9.0,
        "reserved_at": now.isoformat(),
    }
    hass = FakeHass()
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": reservation,
                "ev-b": {
                    "load_kw": 3.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                },
            },
            "ev_grid_shedding_entry_id": "ev-a",
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id=entry_id, runtime_data=object()) for entry_id in ("ev-a", "ev-b")
        ]
    )
    store = FakeStore()
    ownership = {
        "ev_smart_charging_state": {
            CONF_EV_SMART_CHARGING_START: "off",
        },
        "ev_smart_charging_command_entity_id": "button.ev_start",
    }
    store.data["ownership"] = ownership
    store.data["execution_audit"] = [
        {
            "asset": "ev",
            "kind": "ev_stop",
            "result": "failed",
            "attempted_at": now - timedelta(minutes=20 - index),
            "desired_state": {"ev_safety_stop": True},
        }
        for index in range(2)
    ]
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    executor = Executor(
        store,
        hass=hass,
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    plan = EnergyPlan(
        plan_id="disconnected-stop-failed",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="disconnected",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = False

    asyncio.run(executor.async_evaluate(plan, context))

    retained = hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]
    assert retained["load_kw"] == 7.0
    assert retained["retain_when_unloaded"] is True
    assert store.data["ownership"] == ownership
    assert store.data["outcomes"][-1].result == OutcomeResult.FAILED
    pause_until = store.data["control_pause"]["until"]
    assert store.data["control_pause"]["safety_stop_failure"] is True
    assert pause_until - datetime.now(UTC) > timedelta(hours=23)
    retry_limits = store.data["command_rate_limits"]
    assert isinstance(retry_limits["ev_safety_stop_failed_at"], datetime)
    assert retry_limits["ev_safety_stop_blocked_until"] - datetime.now(UTC) > timedelta(hours=23)
    assert "ev_grid_shedding_entry_id" not in hass.data["ha_energy_planner"]

    outcome_count = len(store.data["outcomes"])
    retry_plan = replace(plan, plan_id="disconnected-stop-paused")
    asyncio.run(executor.async_evaluate(retry_plan, context))
    assert len(store.data["outcomes"]) == outcome_count

    executor_b = Executor(FakeStore(), hass=hass, entry_id="ev-b")
    context_b = _context(now)
    context_b.slots[0].baseline_load_forecast_kw = 0.0
    context_b.slots[0].pv_forecast_kw = 0.0
    context_b.slots[0].projected_ev_load_kw = 3.0
    assert executor_b._ev_reservation_safety_issue(context_b) == "multi_ev_grid_import_limit_exceeded"
    assert hass.data["ha_energy_planner"]["ev_grid_shedding_entry_id"] == "ev-b"


def test_disconnected_ev_retains_capacity_when_button_stop_is_unconfirmed() -> None:
    now = datetime.now(UTC)
    reservation = {
        "load_kw": 7.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
    }
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "disconnected",
            "button.ev_start": "unknown",
            "button.ev_stop": "unknown",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {"ev-a": reservation},
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    ownership = {
        "ev_smart_charging_state": {
            CONF_EV_CHARGER_START: "unknown",
        },
        "ev_smart_charging_command_entity_id": "button.ev_start",
    }
    store.data["ownership"] = ownership
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
        CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER_START: "button.ev_start",
            CONF_EV_CHARGER_STOP: "button.ev_stop",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    plan = EnergyPlan(
        plan_id="disconnected-button-stop",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="disconnected",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = False

    asyncio.run(executor.async_evaluate(plan, context))

    retained = hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"]
    assert retained["retain_when_unloaded"] is True
    assert store.data["ownership"] == ownership
    assert store.data["outcomes"][-1].result == OutcomeResult.FAILED
    assert store.data["outcomes"][-1].reason == "ev_stop_not_confirmed"


def test_compensated_owned_safety_stop_clears_ownership_and_capacity() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "unavailable",
            "switch.ev_charger": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                }
            }
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
        "ev_smart_charging_command_entity_id": "switch.ev_charger",
    }
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
        CONF_EV_CONFIRMATION_TIMEOUT_SECONDS: 0,
        CONF_EV_CONFIRMATION_RETRIES: 0,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGER: "switch.ev_charger",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    plan = EnergyPlan(
        plan_id="compensated-safety-stop",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="unsafe",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="unsafe",
        confidence=0.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    context = _context(now)
    context.input_health = InputHealth.UNSAFE

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.ev_charger"}),
        ("switch", "turn_off", {"entity_id": "switch.ev_charger"}),
    ]
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert "control_pause" not in store.data
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].reason == "ev_safe_stop_compensated"


def test_owned_safety_stop_uses_persisted_topology_after_reconfigure() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "switch.old_charger": "on",
            "switch.new_charger": "on",
        }
    )
    hass.data = {"ha_energy_planner": {"ev_grid_reservations": {}}}
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {CONF_EV_CHARGER: "off"},
        "ev_smart_charging_command_entity_id": "switch.old_charger",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGER: "switch.old_charger",
        },
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_EV_CHARGER: "switch.new_charger"},
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
        },
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    plan = EnergyPlan(
        plan_id="old-topology-safety-stop",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="unsafe",
        health=InputHealth.UNSAFE,
        mode=PlannerMode.ACTIVE_DEGRADED,
        summary="unsafe",
        confidence=0.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    context = _context(now)
    context.input_health = InputHealth.UNSAFE

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.old_charger"})]
    assert hass.states.values["switch.old_charger"] == "off"
    assert hass.states.values["switch.new_charger"] == "on"
    assert store.data["ownership"] == {}


def test_recovered_reservation_stops_provisional_topology_before_new_start() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "switch.old_charger": "on",
            "switch.new_charger": "off",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                },
            },
        },
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    store = FakeStore()
    # This is the durable state possible after a service command is accepted
    # but before its pre-state snapshot can be committed as full ownership.
    store.data["ownership"] = {
        "ev_smart_charging_command_entity_id": "switch.old_charger",
        "ev_smart_charging_control_topology": {
            CONF_EV_CHARGER: "switch.old_charger",
        },
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={CONF_EV_CHARGER: "switch.new_charger"},
        options={
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_EV_CHARGE_RATE_KW: 7.0,
            CONF_GRID_IMPORT_LIMIT_KW: 10.0,
        },
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    action = PlanAction(
        action_id="new-topology-start",
        plan_id="recovered-reservation",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={
            "charging_required_now": True,
            "projected_load_kw_now": 7.0,
        },
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan = EnergyPlan(
        plan_id="recovered-reservation",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="recover provisional EV command",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = True
    context.slots[0].projected_ev_load_kw = 7.0

    manual_start = asyncio.run(executor.async_manual_ev_charging(True, context))

    assert manual_start.applied is False
    assert manual_start.reason == "ev_recovery_stop_required"
    assert hass.services.calls == []

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.old_charger"})]
    assert hass.states.values["switch.old_charger"] == "off"
    assert hass.states.values["switch.new_charger"] == "off"
    assert store.data["ownership"] == {}
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"] == {}
    assert store.data["outcomes"][-1].desired_state["charging_reason"] == "ev_recovered_reservation_safety_stop"
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert store.data["outcomes"][-1].service_target == "switch.old_charger"


def test_existing_multi_ev_limit_conflict_sheds_owned_reservation() -> None:
    now = datetime.now(UTC)
    hass = FakeHass(
        {
            "binary_sensor.ev_a_charging": "off",
            "button.ev_a_start": "unknown",
            "input_boolean.ev_a_stop": "on",
        }
    )
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 9.0,
                    "reserved_at": (now - timedelta(minutes=2)).isoformat(),
                },
                "ev-b": {
                    "load_kw": 3.0,
                    "limit_kw": 10.0,
                    "reserved_at": (now - timedelta(minutes=1)).isoformat(),
                },
            },
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id=entry_id, runtime_data=object()) for entry_id in ("ev-a", "ev-b")
        ]
    )
    store = FakeStore()
    store.data["ownership"] = {
        "ev_smart_charging_state": {
            CONF_EV_CHARGER_START: "unknown",
        },
        "ev_smart_charging_command_entity_id": "button.ev_a_start",
    }
    options = {
        **DEFAULT_OPTIONS,
        "planner_enabled": True,
        "dry_run": False,
        CONF_EV_CONTROL_ENABLED: True,
        CONF_EV_CHARGE_RATE_KW: 7.0,
        CONF_GRID_IMPORT_LIMIT_KW: 9.0,
    }
    executor = Executor(
        store,
        hass=hass,
        entry_data={
            CONF_EV_CHARGING: "binary_sensor.ev_a_charging",
            CONF_EV_CHARGER_START: "button.ev_a_start",
            CONF_EV_CHARGER_STOP: "input_boolean.ev_a_stop",
        },
        options=options,
        entry_id="ev-a",
    )
    _arm_store(store, executor)
    action = PlanAction(
        action_id="ev-a-continue",
        plan_id="multi-ev-over-limit",
        execute_not_before=now - timedelta(minutes=1),
        execute_not_after=now + timedelta(minutes=1),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={
            "charging_required_now": True,
            "projected_load_kw_now": 7.0,
        },
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan = EnergyPlan(
        plan_id="multi-ev-over-limit",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="shared limit changed",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[action],
        preview=[],
    )
    context = _context(now)
    context.ev_connected = True
    context.slots[0].baseline_load_forecast_kw = 0.0
    context.slots[0].pv_forecast_kw = 0.0
    context.slots[0].projected_ev_load_kw = 7.0

    asyncio.run(executor.async_evaluate(plan, context))

    assert hass.services.calls == [
        (
            "input_boolean",
            "turn_off",
            {"entity_id": "input_boolean.ev_a_stop"},
        )
    ]
    assert set(hass.data["ha_energy_planner"]["ev_grid_reservations"]) == {"ev-b"}
    assert store.data["ownership"] == {}
    assert store.data["outcomes"][-1].result == OutcomeResult.APPLIED
    assert (
        store.data["outcomes"][-1].desired_state["charging_reason"]
        == "ev_multi_ev_grid_import_limit_exceeded_safety_stop"
    )
    assert "ev_grid_shedding_entry_id" not in hass.data["ha_energy_planner"]


def test_multi_ev_limit_conflict_claims_only_one_shedding_entry() -> None:
    now = datetime.now(UTC)
    hass = FakeHass()
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 9.0,
                    "reserved_at": now.isoformat(),
                },
                "ev-b": {
                    "load_kw": 3.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                },
            },
            "ev_grid_shedding_entry_id": "missing-entry",
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id=entry_id, runtime_data=object()) for entry_id in ("ev-a", "ev-b")
        ]
    )
    executor_a = Executor(FakeStore(), hass=hass, entry_id="ev-a")
    executor_b = Executor(FakeStore(), hass=hass, entry_id="ev-b")
    context_a = _context(now)
    context_a.slots[0].baseline_load_forecast_kw = 0.0
    context_a.slots[0].pv_forecast_kw = 0.0
    context_a.slots[0].projected_ev_load_kw = 7.0
    context_b = _context(now)
    context_b.slots[0].baseline_load_forecast_kw = 0.0
    context_b.slots[0].pv_forecast_kw = 0.0
    context_b.slots[0].projected_ev_load_kw = 3.0

    assert executor_a._ev_grid_shedding_claim() is None
    assert executor_a._ev_reservation_safety_issue(context_a) == "multi_ev_grid_import_limit_exceeded"
    assert executor_b._ev_reservation_safety_issue(context_b) is None
    assert hass.data["ha_energy_planner"]["ev_grid_shedding_entry_id"] == "ev-a"

    executor_a._release_ev_grid_reservation()

    assert "ev_grid_shedding_entry_id" not in hass.data["ha_energy_planner"]
    assert executor_b._ev_reservation_safety_issue(context_b) is None


def test_loaded_ev_can_replace_unloaded_retained_shedding_claim() -> None:
    now = datetime.now(UTC)
    hass = FakeHass()
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 7.0,
                    "limit_kw": 9.0,
                    "reserved_at": now.isoformat(),
                    "retain_when_unloaded": True,
                },
                "ev-b": {
                    "load_kw": 3.0,
                    "limit_kw": 10.0,
                    "reserved_at": now.isoformat(),
                },
            },
            "ev_grid_shedding_entry_id": "ev-a",
        },
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id="ev-a", runtime_data=None),
            SimpleNamespace(entry_id="ev-b", runtime_data=object()),
        ]
    )
    executor = Executor(FakeStore(), hass=hass, entry_id="ev-b")
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 0.0
    context.slots[0].pv_forecast_kw = 0.0
    context.slots[0].projected_ev_load_kw = 3.0

    assert executor._ev_grid_shedding_claim() is None
    assert executor._ev_reservation_safety_issue(context) == "multi_ev_grid_import_limit_exceeded"
    assert hass.data["ha_energy_planner"]["ev_grid_shedding_entry_id"] == "ev-b"


def test_single_ev_reservation_detects_tightened_import_limit() -> None:
    now = datetime.now(UTC)
    hass = FakeHass()
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": {
                    "load_kw": 11.0,
                    "limit_kw": 8.0,
                    "reserved_at": now.isoformat(),
                },
            },
        },
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
    )
    executor = Executor(FakeStore(), hass=hass, entry_id="ev-a")
    context = _context(now)
    context.slots[0].baseline_load_forecast_kw = 0.0
    context.slots[0].pv_forecast_kw = 0.0
    # The current plan reflects a reduced 3 kW option, but the running charger
    # still owns the persisted 11 kW high-watermark until a confirmed stop.
    context.slots[0].projected_ev_load_kw = 3.0

    assert executor._ev_reservation_safety_issue(context) == "grid_import_limit_exceeded"
    assert hass.data["ha_energy_planner"]["ev_grid_shedding_entry_id"] == "ev-a"


def test_ev_reservation_safety_issue_ignores_incomplete_or_safe_evidence() -> None:
    now = datetime.now(UTC)
    context = _context(now)
    executor = Executor(FakeStore(), entry_id="ev-a")

    assert executor._ev_reservation_safety_issue(context) is None

    hass = FakeHass()
    hass.data = {"ha_energy_planner": {"ev_grid_reservations": {}}}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id=entry_id, runtime_data=object()) for entry_id in ("ev-a", "ev-b")
        ]
    )
    executor.hass = hass

    assert executor._ev_reservation_safety_issue(context) is None

    hass.data["ha_energy_planner"]["ev_grid_reservations"] = {
        "ev-a": {
            "load_kw": 2.0,
            "limit_kw": 10.0,
            "reserved_at": now.isoformat(),
        },
        "ev-b": {
            "load_kw": 1.0,
            "limit_kw": 10.0,
            "reserved_at": now.isoformat(),
        },
    }
    context.slots[0].baseline_load_forecast_kw = None
    context.slots[0].pv_forecast_kw = None

    assert executor._ev_reservation_safety_issue(context) is None

    context.slots[0].baseline_load_forecast_kw = 1.0
    context.slots[0].pv_forecast_kw = 0.0
    context.slots[0].projected_ev_load_kw = 2.0

    assert executor._ev_reservation_safety_issue(context) is None


def test_ev_grid_reservation_reconciles_failed_commands(monkeypatch: object) -> None:
    now = datetime.now(UTC)

    class SupportedDiscovery:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def inspect(self) -> SupportedDiscovery:
            return self

        def for_asset(self, asset: Any) -> Any:
            return SimpleNamespace(supported=True, issues=[])

    monkeypatch.setattr(executor_module, "CapabilityDiscovery", SupportedDiscovery)

    def execute(
        *,
        kind: ActionKind,
        command_sent: bool,
        rollback_succeeded: bool | None,
        previous: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        result = SimpleNamespace(
            applied=False,
            reason="ev_failed",
            pre_state={},
            post_state={},
            command_sent=command_sent,
            rollback_succeeded=rollback_succeeded,
        )

        class FailedAdapter:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def async_execute(self, action: Any) -> Any:
                if action.kind != ActionKind.EV_STOP:
                    assert store.flush_count == 1
                    assert store.data["ev_grid_reservation"]["active"] is True
                    assert store.data["ownership"].get(
                        "ev_smart_charging_control_topology"
                    ) == executor_module._ev_control_topology(executor.entry_data)
                return result

        monkeypatch.setattr(executor_module, "EVSmartChargingAdapter", FailedAdapter)
        hass = FakeHass()
        reservations = {"ev-a": previous} if previous is not None else {}
        hass.data = {"ha_energy_planner": {"ev_grid_reservations": reservations}}
        hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [SimpleNamespace(entry_id="ev-a", runtime_data=object())]
        )
        store = FakeStore()
        executor = Executor(
            store,
            hass=hass,
            options={
                **DEFAULT_OPTIONS,
                "planner_enabled": True,
                "dry_run": False,
                CONF_EV_CONTROL_ENABLED: True,
                CONF_EV_CHARGE_RATE_KW: 7.0,
                CONF_GRID_IMPORT_LIMIT_KW: 10.0,
            },
            entry_id="ev-a",
        )
        _arm_store(store, executor)
        action = PlanAction(
            "ev",
            "plan",
            now - timedelta(minutes=1),
            now + timedelta(minutes=1),
            ActionAsset.EV,
            kind,
            {"charging_required_now": kind != ActionKind.EV_STOP},
            [],
            [],
            None,
            1.0,
        )
        plan = EnergyPlan(
            "plan",
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
        context = _context(now)
        context.ev_connected = True
        context.slots[0].projected_ev_load_kw = 7.0

        asyncio.run(executor.async_evaluate(plan, context))

        return reservations

    prior = {"load_kw": 6.0, "limit_kw": 10.0, "reserved_at": now.isoformat()}
    assert (
        execute(
            kind=ActionKind.EV_START,
            command_sent=True,
            rollback_succeeded=True,
            previous=None,
        )
        == {}
    )
    assert (
        execute(
            kind=ActionKind.EV_START,
            command_sent=False,
            rollback_succeeded=None,
            previous=None,
        )
        == {}
    )
    assert execute(
        kind=ActionKind.EV_START,
        command_sent=False,
        rollback_succeeded=None,
        previous=prior,
    ) == {"ev-a": prior}
    assert execute(
        kind=ActionKind.EV_STOP,
        command_sent=False,
        rollback_succeeded=False,
        previous=prior,
    ) == {"ev-a": prior}


def test_ev_grid_reservation_defensive_branches() -> None:
    now = datetime.now(UTC)
    action = PlanAction(
        "ev",
        "plan",
        now,
        now,
        ActionAsset.EV,
        ActionKind.EV_SCHEDULE,
        {"charging_required_now": True},
        [],
        [],
        None,
        1.0,
    )
    previous = {"load_kw": 6.0, "limit_kw": 10.0, "reserved_at": now.isoformat()}
    other = {"load_kw": 3.0, "limit_kw": 10.0, "reserved_at": now.isoformat()}
    retained = {
        "load_kw": 2.0,
        "limit_kw": 10.0,
        "reserved_at": now.isoformat(),
        "retain_when_unloaded": True,
    }
    hass = FakeHass()
    hass.data = {
        "ha_energy_planner": {
            "ev_grid_reservations": {
                "ev-a": previous,
                "ev-b": other,
                "retained": retained,
                "stale": other,
            }
        }
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id="ev-a", runtime_data=object()),
            SimpleNamespace(entry_id="ev-b", runtime_data=object()),
        ]
    )
    executor = Executor(
        FakeStore(),
        hass=hass,
        options={CONF_EV_CHARGE_RATE_KW: 7.0, CONF_GRID_IMPORT_LIMIT_KW: 10.0},
        entry_id="ev-a",
    )
    unavailable = _context(now)
    unavailable.slots[0].baseline_load_forecast_kw = None
    unavailable.slots[0].pv_forecast_kw = None

    missing_reason, missing_previous = executor._reserve_ev_grid_capacity(
        action,
        None,
        now,
    )
    reason, restored = executor._reserve_ev_grid_capacity(action, unavailable, now)

    assert missing_reason == "ev_grid_projection_unavailable"
    assert missing_previous == previous
    assert reason == "multi_ev_grid_projection_unavailable"
    assert restored == previous
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"] == previous
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"]["retained"] == retained
    assert "stale" not in hass.data["ha_energy_planner"]["ev_grid_reservations"]

    constrained = _context(now)
    constrained.slots[0].projected_ev_load_kw = 7.0
    reason, restored = executor._reserve_ev_grid_capacity(action, constrained, now)

    assert reason == "multi_ev_grid_import_limit_exceeded"
    assert restored == previous

    no_entries_hass = FakeHass()
    no_entries_hass.data = {}
    no_entries_executor = Executor(FakeStore(), hass=no_entries_hass, entry_id="ev-a")
    no_entries_executor._discard_stale_ev_grid_reservations({})
    no_entries_executor._retain_ev_grid_reservation_when_unloaded()
    no_entries_executor.sync_ev_grid_reservation()
    Executor(FakeStore(), hass=no_entries_hass).sync_ev_grid_reservation()
    bad_domain_hass = FakeHass()
    bad_domain_hass.data = {"ha_energy_planner": []}
    bad_domain_executor = Executor(FakeStore(), hass=bad_domain_hass)
    assert bad_domain_executor._ev_grid_reservations() is None
    assert bad_domain_executor._ev_grid_shedding_claim() is None

    assert executor_module._ev_action_wants_power(action) is True
    action.desired_state["charging_required_now"] = False
    assert executor_module._ev_action_wants_power(action) is False
    legacy_controls = {
        CONF_EV_SMART_CHARGING_START: "switch.ev_start",
        CONF_EV_SMART_CHARGING_STOP: "switch.ev_stop",
    }
    assert _service_target_for_action(action, legacy_controls) == "switch.ev_stop"
    action.desired_state.pop("charging_required_now")
    assert executor_module._ev_action_wants_power(action) is True
    assert _service_target_for_action(action, legacy_controls) == "switch.ev_start"
    assert _ev_command_entity_for_action(action, legacy_controls) == "switch.ev_start"
    action.kind = ActionKind.SET_HVAC
    assert executor_module._ev_action_wants_power(action) is False
    assert executor_module._positive_float("bad") == 0.0
    assert executor_module._positive_float(float("nan")) == 0.0

    action.kind = ActionKind.EV_START
    action.desired_state["charging_required_now"] = True
    executor._reconcile_ev_grid_reservation(
        action,
        SimpleNamespace(
            applied=False,
            command_sent=False,
            rollback_succeeded=None,
        ),
        previous,
    )
    assert hass.data["ha_energy_planner"]["ev_grid_reservations"]["ev-a"] == previous

    executor.store.data["ownership"] = []
    assert executor._owned_ev_control_topology() is None
    executor.store.data["ownership"] = {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}
    assert (
        asyncio.run(
            executor._async_save_provisional_ev_ownership(
                action,
                {CONF_EV_CHARGER: "switch.ev"},
            )
        )
        is False
    )
    assert executor.ev_start_feedback_expected_until is None
    executor._expect_ev_start_feedback()
    assert executor.ev_start_feedback_expected_until is not None
    asyncio.run(executor._async_clear_provisional_ev_ownership())
    assert executor.ev_start_feedback_expected_until is None
    assert executor.store.data["ownership"] == {"ev_smart_charging_state": {CONF_EV_CHARGER: "off"}}


def test_enphase_interrupted_command_is_restored_by_targeted_disable() -> None:
    async def run() -> None:
        now = datetime.now(UTC)
        hass = FakeHass({"select.enphase": "Custom Baseline"})
        store = FakeStore()
        executor = Executor(
            store,
            hass=hass,
            entry_data={CONF_ENPHASE_PROFILE: "select.enphase", CONF_ENPHASE_AI_PROFILE: "AI Optimisation"},
            options={CONF_ENPHASE_CONTROL_ENABLED: True},
        )
        _arm_store(store, executor)
        action = PlanAction(
            "profile",
            "plan",
            now - timedelta(minutes=1),
            now + timedelta(minutes=1),
            ActionAsset.ENPHASE,
            ActionKind.SET_PROFILE,
            {"profile": "Self Consumption"},
            [],
            [],
            1.0,
            1.0,
        )
        plan = EnergyPlan(
            "plan",
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
        original = hass.services.async_call

        async def accepted_then_cancelled(
            domain: str, service: str, data: dict[str, Any], blocking: bool = False
        ) -> None:
            assert store.flush_count == 1
            await original(domain, service, data, blocking)
            raise asyncio.CancelledError

        hass.services.async_call = accepted_then_cancelled
        with pytest.raises(asyncio.CancelledError):
            await executor.async_evaluate(plan)
        assert hass.states.values["select.enphase"] == "Self Consumption"
        assert store.data["ownership"]["enphase_profile"] == "Custom Baseline"
        hass.services.async_call = original
        restored = await executor.async_restore_device_control("enphase", "enphase_control_disabled")
        assert restored.result == OutcomeResult.RESTORED
        assert hass.states.values["select.enphase"] == "Custom Baseline"
        assert "enphase_profile" not in store.data["ownership"]

        # An observed no-op does not acquire ownership or consume command limits.
        action.desired_state["profile"] = "Custom Baseline"
        await executor.async_evaluate(plan)
        assert store.data["outcomes"][-1].result == OutcomeResult.SKIPPED
        assert "enphase_profile" not in store.data["ownership"]
        assert store.data.get("command_rate_limits", {}) == {}

    asyncio.run(run())
