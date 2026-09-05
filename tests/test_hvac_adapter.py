"""Tests for Daikin HVAC adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import Context

from custom_components.ha_energy_planner import hvac_adapter as hvac_adapter_module
from custom_components.ha_energy_planner.const import (
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
)
from custom_components.ha_energy_planner.coordinator import (
    _is_planner_owned_control_feedback,
    _pending_zone_hvac_manual_change_entity_id,
)
from custom_components.ha_energy_planner.hvac_adapter import DaikinHVACAdapter
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


class FakeStates:
    """Minimal HA state registry."""

    def __init__(self, values: dict[str, str | FakeState]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        if value is None:
            return None
        if isinstance(value, FakeState):
            return value
        return FakeState(value)


class FakeServices:
    """Minimal HA service bus."""

    def __init__(self, states: FakeStates) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.contexts: list[Any] = []
        self.fail_services: set[tuple[str, str]] = set()
        self.noop_entities: set[str] = set()

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        context: Any = None,
    ) -> None:
        self.calls.append((domain, service, data))
        self.contexts.append(context)
        if (domain, service) in self.fail_services:
            raise RuntimeError("service failed")
        entity_id = data["entity_id"]
        if entity_id in self.noop_entities:
            return
        if service == "turn_on":
            self.states.values[entity_id] = "on"
        elif service == "turn_off":
            self.states.values[entity_id] = "off"
        elif service == "start" and domain == "timer":
            self.states.values[entity_id] = "active"
        elif service == "cancel" and domain == "timer":
            self.states.values[entity_id] = "idle"
        elif service == "set_hvac_mode":
            current = self.states.get(entity_id)
            self.states.values[entity_id] = FakeState(
                str(data["hvac_mode"]),
                {} if current is None else dict(current.attributes),
            )
        elif service == "set_temperature":
            current = self.states.get(entity_id)
            attributes = {} if current is None else dict(current.attributes)
            for key in ("temperature", "target_temp_low", "target_temp_high"):
                if key in data:
                    attributes[key] = data[key]
            self.states.values[entity_id] = FakeState(
                "unknown" if current is None else current.state,
                attributes,
            )


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str | FakeState]) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states)


def _action(desired_state: dict[str, Any]) -> PlanAction:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return PlanAction(
        action_id="hvac",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state=desired_state,
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )


def test_hvac_action_disables_automation_then_controls_climate() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 19}),
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    assert adapter.takeover_snapshot() == ({"automation.climate": "on"}, {})
    assert adapter.main_takeover_snapshot() == {
        "hvac_mode": "heat",
        "target_temperature": 19,
    }
    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 20})))
    assert result.applied is True
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 20}),
    ]


def test_hvac_action_arms_scheduler_classifier_guard_before_actuators() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 19}),
            "automation.climate": "on",
            "input_boolean.scheduler_change": "off",
            "timer.scheduler_guard": "idle",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
            CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 20})))

    assert result.applied is True
    assert hass.services.calls == [
        (
            "timer",
            "start",
            {"entity_id": "timer.scheduler_guard", "duration": "00:00:30"},
        ),
        ("input_boolean", "turn_on", {"entity_id": "input_boolean.scheduler_change"}),
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 20}),
    ]


def test_hvac_action_fails_closed_when_scheduler_guard_is_incomplete() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "input_boolean.scheduler_change": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is False
    assert result.reason == "climate_scheduler_guard_failed"
    assert result.rollback_succeeded is False
    assert hass.services.calls == []


def test_hvac_action_rolls_back_failed_guard_without_touching_actuators() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "input_boolean.scheduler_change": "off",
            "timer.scheduler_guard": "idle",
        }
    )
    hass.services.fail_services.add(("input_boolean", "turn_on"))
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
            CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.reason == "climate_scheduler_guard_failed"
    assert hass.services.calls == [
        (
            "timer",
            "start",
            {"entity_id": "timer.scheduler_guard", "duration": "00:00:30"},
        ),
        ("input_boolean", "turn_on", {"entity_id": "input_boolean.scheduler_change"}),
        ("input_boolean", "turn_off", {"entity_id": "input_boolean.scheduler_change"}),
        ("timer", "cancel", {"entity_id": "timer.scheduler_guard"}),
    ]


def test_hvac_guard_fails_when_timer_does_not_confirm(monkeypatch: Any) -> None:
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0)
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "input_boolean.scheduler_change": "off",
            "timer.scheduler_guard": "idle",
        }
    )
    hass.services.noop_entities.add("timer.scheduler_guard")
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
            CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.reason == "climate_scheduler_guard_failed"
    assert hass.services.calls == [
        (
            "timer",
            "start",
            {"entity_id": "timer.scheduler_guard", "duration": "00:00:30"},
        ),
        ("input_boolean", "turn_off", {"entity_id": "input_boolean.scheduler_change"}),
        ("timer", "cancel", {"entity_id": "timer.scheduler_guard"}),
    ]


def test_hvac_guard_cleanup_service_errors_remain_fail_closed() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "input_boolean.scheduler_change": "off",
            "timer.scheduler_guard": "idle",
        }
    )
    hass.services.fail_services.update(
        {
            ("input_boolean", "turn_on"),
            ("input_boolean", "turn_off"),
            ("timer", "cancel"),
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
            CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.reason == "climate_scheduler_guard_failed"
    assert all(domain not in {"climate", "automation", "switch"} for domain, _, _ in hass.services.calls)


def test_hvac_restore_fails_closed_when_scheduler_guard_is_incomplete() -> None:
    hass = FakeHass(
        {
            "automation.climate": "off",
            "input_boolean.scheduler_change": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
        },
    )

    result = asyncio.run(adapter.async_restore({"automation.climate": "on"}))

    assert result.applied is False
    assert result.reason == "climate_scheduler_guard_failed"
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.services.calls == []


def test_hvac_action_only_calls_services_for_fields_that_differ() -> None:
    target_hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 23}), "automation.climate": "off"})
    target_adapter = DaikinHVACAdapter(
        target_hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    mode_hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 20}), "automation.climate": "off"})
    mode_adapter = DaikinHVACAdapter(
        mode_hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    target_result = asyncio.run(target_adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 19})))
    mode_result = asyncio.run(mode_adapter.async_execute(_action({"hvac_mode": "cool", "target_temperature": 20})))

    assert target_result.applied is True
    assert target_hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 19}),
    ]
    assert mode_result.applied is True
    assert mode_hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
        ("climate", "set_hvac_mode", {"entity_id": "climate.daikin", "hvac_mode": "cool"}),
    ]


def test_hvac_restore_returns_automation_to_saved_state() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    result = asyncio.run(adapter.async_restore({"automation.climate": "on"}))
    assert result.applied is True
    assert result.reason == "hvac_control_released"
    assert hass.services.calls == [
        ("automation", "turn_on", {"entity_id": "automation.climate"}),
    ]


@pytest.mark.parametrize(
    "saved_target",
    [{}, {"target_temp_low": 18}],
)
def test_hvac_restore_retains_unrestorable_climate_zone_snapshot(
    saved_target: dict[str, Any],
) -> None:
    hass = FakeHass({"climate.zone_temperature": FakeState("heat", {"temperature": 23})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_CLIMATE_ZONES: ["climate.zone_temperature"]},
    )

    result = asyncio.run(
        adapter.async_restore(
            saved_zone_states={"climate.zone_temperature": saved_target},
        )
    )

    assert result.applied is False
    assert result.reason == "hvac_release_failed"
    assert result.rollback_succeeded is False
    assert result.saved_zone_states == {
        "climate.zone_temperature": saved_target,
    }
    assert hass.services.calls == []


def test_hvac_action_fails_closed_when_climate_unavailable() -> None:
    hass = FakeHass({"automation.climate": "on"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat"})))
    assert result.applied is False
    assert result.reason == "daikin_climate_unavailable"
    assert result.rollback_succeeded is True
    assert hass.services.calls == []


def test_hvac_action_fails_closed_when_takeover_snapshot_is_incomplete() -> None:
    automation_hass = FakeHass({"climate.daikin": "heat"})
    automation_adapter = DaikinHVACAdapter(
        automation_hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    zone_hass = FakeHass({"climate.daikin": "heat"})
    zone_adapter = DaikinHVACAdapter(
        zone_hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: "switch.zone",
        },
    )

    automation_result = asyncio.run(automation_adapter.async_execute(_action({"hvac_mode": "heat"})))
    zone_result = asyncio.run(zone_adapter.async_execute(_action({"hvac_mode": "heat", "enable_zones": True})))

    assert automation_result.reason == "climate_automation_unavailable"
    assert automation_result.rollback_succeeded is True
    assert automation_hass.services.calls == []
    assert zone_result.reason == "climate_zone_unavailable"
    assert zone_result.rollback_succeeded is True
    assert zone_hass.services.calls == []


def test_hvac_action_still_acquires_automation_when_climate_already_matches() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 20})))
    assert result.applied is True
    assert result.reason == "hvac_action_applied"
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True})
    ]


def test_hvac_action_fails_when_automation_does_not_confirm_disabled(monkeypatch: object) -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 19}),
            "automation.climate": "on",
        }
    )
    hass.services.noop_entities.add("automation.climate")
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    async def state_not_confirmed(entity_id: str, expected_state: str) -> bool:
        return expected_state == "on"

    monkeypatch.setattr(adapter, "_async_confirm_state", state_not_confirmed)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 20})))

    assert result.applied is False
    assert result.reason == "hvac_automation_service_failed"
    assert result.rollback_succeeded is True
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
    ]


def test_hvac_action_rolls_back_when_climate_state_is_not_confirmed(monkeypatch: object) -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("off", {"temperature": 18}),
            "automation.climate": "on",
            "switch.zone": "off",
        }
    )
    hass.services.noop_entities.add("climate.daikin")
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_ZONES: "switch.zone",
        },
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.hvac_adapter._STATE_CONFIRMATION_TIMEOUT_SECONDS",
        0.0,
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "hvac_state_confirmation_failed"
    assert result.rollback_succeeded is True
    assert hass.states.values["automation.climate"] == "on"
    assert hass.states.values["switch.zone"] == "off"
    assert hass.states.get("climate.daikin") == FakeState("off", {"temperature": 18})


def test_hvac_action_waits_for_delayed_climate_state_confirmation() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    original_call = hass.services.async_call

    async def delayed_mode_change(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        if domain != "climate" or service != "set_hvac_mode":
            await original_call(domain, service, data, blocking)
            return
        hass.services.calls.append((domain, service, data))
        asyncio.get_running_loop().call_later(
            0.02,
            hass.states.values.__setitem__,
            data["entity_id"],
            data["hvac_mode"],
        )

    hass.services.async_call = delayed_mode_change

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is True
    assert result.reason == "hvac_action_applied"


def test_hvac_action_reasserts_target_overwritten_by_inflight_schedule(monkeypatch: object) -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 19}),
            "automation.climate": "off",
            "input_boolean.scheduler_change": "off",
            "timer.scheduler_guard": "idle",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
            CONF_CLIMATE_SCHEDULER_GUARD_TIMER: "timer.scheduler_guard",
        },
    )
    original_call = hass.services.async_call
    temperature_calls = 0

    async def overwrite_first_target(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal temperature_calls
        await original_call(domain, service, data, blocking)
        if domain == "climate" and service == "set_temperature":
            temperature_calls += 1
            if temperature_calls == 1:
                hass.states.values[data["entity_id"]] = FakeState("heat", {"temperature": 19})

    hass.services.async_call = overwrite_first_target
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 24})))

    assert result.applied is True
    assert temperature_calls == 2
    assert hass.states.get("climate.daikin") == FakeState("heat", {"temperature": 24})
    assert [call for call in hass.services.calls if call[:2] == ("timer", "start")] == [
        ("timer", "start", {"entity_id": "timer.scheduler_guard", "duration": "00:00:30"}),
        ("timer", "start", {"entity_id": "timer.scheduler_guard", "duration": "00:00:30"}),
    ]


def test_hvac_retry_preserves_originally_revealed_active_mode(monkeypatch: object) -> None:
    hass = FakeHass({"climate.daikin": FakeState("off", {"temperature": 19})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    original_call = hass.services.async_call
    temperature_calls = 0
    persisted_main_states: list[dict[str, Any]] = []

    async def reveal_cool_then_overwrite_first_target(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal temperature_calls
        if domain == "climate" and service == "turn_on":
            hass.services.calls.append((domain, service, data))
            hass.states.values[data["entity_id"]] = FakeState(
                "cool",
                {"temperature": 19},
            )
            return
        await original_call(domain, service, data, blocking)
        if domain == "climate" and service == "set_temperature":
            temperature_calls += 1
            if temperature_calls == 1:
                current = hass.states.get(data["entity_id"])
                hass.states.values[data["entity_id"]] = FakeState(
                    "heat" if current is None else current.state,
                    {"temperature": 19},
                )

    async def persist_main_state(saved_state: dict[str, Any]) -> None:
        persisted_main_states.append(dict(saved_state))

    hass.services.async_call = reveal_cool_then_overwrite_first_target
    adapter.set_main_state_persistence_callback(persist_main_state)
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 24})))

    assert result.applied is True
    assert len(persisted_main_states) == 2
    assert all(state["rollback_active_hvac_mode"] == "cool" for state in persisted_main_states)


def test_hvac_action_audits_command_after_initially_matching_state_drifts() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 24}),
            "automation.climate": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    original_call = hass.services.async_call

    async def finish_inflight_schedule(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        await original_call(domain, service, data, blocking)
        if domain == "automation" and service == "turn_off":
            hass.states.values["climate.daikin"] = FakeState("heat", {"temperature": 19})

    hass.services.async_call = finish_inflight_schedule

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 24})))

    assert result.applied is True
    assert result.reason == "hvac_action_applied"
    assert result.command_sent is True
    assert hass.states.get("climate.daikin") == FakeState("heat", {"temperature": 24})
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 24}),
    ]


def test_hvac_retry_reports_guard_failure(monkeypatch: object) -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 19})})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})
    guard_calls = 0

    async def arm_guard_once() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return guard_calls == 1

    async def confirm_only_original_target(
        entity_id: str,
        desired_state: dict[str, Any],
    ) -> bool:
        return desired_state.get("target_temperature") == 19

    monkeypatch.setattr(adapter, "_async_arm_scheduler_guard", arm_guard_once)
    monkeypatch.setattr(
        adapter,
        "_async_confirm_hvac_state",
        confirm_only_original_target,
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 24})))

    assert result.applied is False
    assert result.reason == "climate_scheduler_guard_failed"


def test_hvac_retry_reports_service_failure(monkeypatch: object) -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 19})})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})
    original_apply = adapter._async_apply_hvac_state

    async def confirm_only_original_target(
        entity_id: str,
        desired_state: dict[str, Any],
    ) -> bool:
        return desired_state.get("target_temperature") == 19

    async def fail_forced_apply(
        entity_id: str,
        desired_state: dict[str, Any],
        *,
        force: bool = False,
        takeover_main_state: dict[str, Any] | None = None,
        respect_manual_override: bool = True,
        respect_zone_manual_override: bool = True,
    ) -> None:
        if force:
            raise RuntimeError("retry failed")
        await original_apply(
            entity_id,
            desired_state,
            takeover_main_state=takeover_main_state,
            respect_manual_override=respect_manual_override,
            respect_zone_manual_override=respect_zone_manual_override,
        )

    monkeypatch.setattr(
        adapter,
        "_async_confirm_hvac_state",
        confirm_only_original_target,
    )
    monkeypatch.setattr(adapter, "_async_apply_hvac_state", fail_forced_apply)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 24})))

    assert result.applied is False
    assert result.reason == "hvac_control_service_failed"


def test_hvac_on_confirmation_waits_and_apply_rejects_missing_state(
    monkeypatch: object,
) -> None:
    hass = FakeHass({"climate.daikin": "off"})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})

    async def exercise() -> None:
        asyncio.get_running_loop().call_later(
            0.01,
            hass.states.values.__setitem__,
            "climate.daikin",
            "heat",
        )
        assert await adapter._async_confirm_hvac_on("climate.daikin") is True
        with pytest.raises(RuntimeError, match="climate state unavailable"):
            await adapter._async_apply_hvac_state("climate.missing", {"hvac_mode": "heat"})

    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.1)
    asyncio.run(exercise())


def test_hvac_apply_does_not_send_mode_before_turn_on_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = FakeHass({"climate.daikin": "off"})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})

    async def never_confirms_on(entity_id: str) -> bool:
        return False

    monkeypatch.setattr(adapter, "_async_confirm_hvac_on", never_confirms_on)

    with pytest.raises(RuntimeError, match="turn-on was not confirmed"):
        asyncio.run(
            adapter._async_apply_hvac_state(
                "climate.daikin",
                {"hvac_mode": "heat", "target_temperature": 24},
            )
        )

    assert hass.services.calls == [("climate", "turn_on", {"entity_id": "climate.daikin"})]


def test_hvac_apply_does_not_send_target_before_mode_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = FakeHass({"climate.daikin": FakeState("cool", {"temperature": 19})})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})

    async def never_confirms_mode(entity_id: str, desired_state: dict[str, Any]) -> bool:
        return False

    monkeypatch.setattr(adapter, "_async_confirm_hvac_state", never_confirms_mode)

    with pytest.raises(RuntimeError, match="HVAC mode was not confirmed"):
        asyncio.run(
            adapter._async_apply_hvac_state(
                "climate.daikin",
                {"hvac_mode": "heat", "target_temperature": 24},
            )
        )

    assert hass.services.calls == [
        (
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.daikin", "hvac_mode": "heat"},
        )
    ]


def test_hvac_away_off_does_not_enable_takeover_zones() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "automation.climate": "on",
            "switch.zone": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_ZONES: ["switch.zone"],
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "off"})))

    assert result.applied is True
    assert result.saved_zone_states == {}
    assert hass.states.values["switch.zone"] == "off"
    assert ("switch", "turn_on", {"entity_id": "switch.zone"}) not in hass.services.calls


def test_hvac_suppression_disables_automations_without_climate_call() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    result = asyncio.run(adapter.async_execute(_action({"suppress_automations": True})))
    assert result.applied is True
    assert result.reason == "hvac_automations_suppressed"
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
    ]


def test_hvac_suppression_stops_actions_when_automation_is_already_off() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    result = asyncio.run(adapter.async_execute(_action({"suppress_automations": True})))
    assert result.applied is True
    assert result.reason == "hvac_automations_suppressed"
    assert result.saved_automation_states == {}
    assert result.command_sent is True
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
    ]


def test_hvac_suppression_honors_manual_main_change_during_disable() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    manual_override = False
    persistence_boundaries: list[int] = []
    original_call = hass.services.async_call

    async def apply_manual_change(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_call(domain, service, data, blocking)
        if domain == "automation" and service == "turn_off":
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True

    async def persist_manual_supersession() -> None:
        persistence_boundaries.append(len(hass.services.calls))

    hass.services.async_call = apply_manual_change
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(adapter.async_execute(_action({"suppress_automations": True})))

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert persistence_boundaries == [1]
    assert hass.states.get("automation.climate").state == "on"
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24


def test_hvac_action_fails_closed_when_automation_service_fails() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
    hass.services.fail_services.add(("automation", "turn_off"))
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is False
    assert result.reason == "hvac_automation_service_failed"
    assert result.saved_automation_states == {}
    assert result.rollback_succeeded is True
    assert hass.states.values["climate.daikin"] == "heat"


def test_hvac_action_fails_closed_when_climate_service_fails() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    hass.services.fail_services.add(("climate", "set_hvac_mode"))
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is False
    assert result.reason == "hvac_control_service_failed"
    assert result.rollback_succeeded is True
    assert hass.states.values["climate.daikin"] == "heat"
    assert hass.states.values["automation.climate"] == "off"


def test_hvac_partial_automation_failure_restores_every_changed_automation() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "automation.first": "on",
            "automation.second": "on",
        }
    )
    original_call = hass.services.async_call

    async def fail_second_disable(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        if service == "turn_off" and data["entity_id"] == "automation.second":
            raise RuntimeError("second automation failed")
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_second_disable
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.first,automation.second",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is False
    assert result.reason == "hvac_automation_service_failed"
    assert result.rollback_succeeded is True
    assert result.saved_automation_states == {}
    assert hass.states.values["automation.first"] == "on"
    assert hass.states.values["automation.second"] == "on"


def test_hvac_suppression_failure_uses_transactional_rollback() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "heat",
            "automation.first": "on",
            "automation.second": "on",
        }
    )
    original_call = hass.services.async_call

    async def fail_second_disable(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        if service == "turn_off" and data["entity_id"] == "automation.second":
            raise RuntimeError("second automation failed")
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_second_disable
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.first,automation.second",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"suppress_automations": True})))

    assert result.applied is False
    assert result.reason == "hvac_automation_service_failed"
    assert result.rollback_succeeded is True
    assert hass.states.values["automation.first"] == "on"


def test_hvac_disable_exception_after_side_effect_restores_failing_automation() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
    original_call = hass.services.async_call

    async def apply_then_fail(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        await original_call(domain, service, data, blocking)
        if domain == "automation" and service == "turn_off":
            raise RuntimeError("handler failed after applying state")

    hass.services.async_call = apply_then_fail
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"suppress_automations": True})))

    assert result.applied is False
    assert result.reason == "hvac_automation_service_failed"
    assert result.rollback_succeeded is True
    assert result.saved_automation_states == {}
    assert hass.states.values["automation.climate"] == "on"


def test_hvac_climate_failure_restores_disabled_automation() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
    hass.services.fail_services.add(("climate", "set_hvac_mode"))
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is False
    assert result.reason == "hvac_control_service_failed"
    assert result.rollback_succeeded is True
    assert hass.states.values["automation.climate"] == "on"


def test_hvac_failed_compensation_retains_unresolved_automation_state() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
    original_call = hass.services.async_call

    async def fail_climate_and_rollback(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        if domain == "climate" or (service == "turn_on" and data["entity_id"] == "automation.climate"):
            raise RuntimeError("service failed")
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_climate_and_rollback
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is False
    assert result.reason == "hvac_acquisition_rollback_failed"
    assert result.rollback_succeeded is False
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.states.values["automation.climate"] == "off"


def test_hvac_restore_reports_service_failure() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    hass.services.fail_services.add(("automation", "turn_on"))
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    result = asyncio.run(adapter.async_restore({"automation.climate": "on"}))

    assert result.applied is False
    assert result.reason == "hvac_release_failed"
    assert hass.states.values["automation.climate"] == "off"


def test_hvac_release_action_routes_through_transactional_restore() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off", "switch.zone": "on"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_ZONES: "switch.zone",
        },
    )
    action = _action({"release_reason": "peak_end"})
    action.kind = ActionKind.RELEASE_HVAC

    result = asyncio.run(adapter.async_execute(action))

    assert result.applied is True
    assert result.reason == "hvac_control_released"
    assert hass.states.values["automation.climate"] == "off"


def test_hvac_zone_acquisition_failure_compensates_automation_and_zone() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on", "switch.zone": "off"})
    hass.services.fail_services.add(("switch", "turn_on"))
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_ZONES: ["switch.zone"],
        },
    )

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "enable_zones": True})))

    assert result.applied is False
    assert result.reason == "hvac_zone_service_failed"
    assert result.rollback_succeeded is True
    assert hass.states.values["automation.climate"] == "on"
    assert hass.states.values["switch.zone"] == "off"


def test_hvac_zone_release_failure_is_retained_for_retry() -> None:
    hass = FakeHass({"climate.daikin": "heat", "switch.zone": "on"})
    hass.services.fail_services.add(("switch", "turn_off"))
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin", CONF_CLIMATE_ZONES: ["switch.zone"]},
    )

    result = asyncio.run(adapter.async_restore({}, {"switch.zone": "off"}))

    assert result.applied is False
    assert result.reason == "hvac_release_failed"
    assert result.saved_zone_states == {"switch.zone": "off"}
    assert adapter._zone_entities() == ["switch.zone"]
    adapter.entry_data[CONF_CLIMATE_ZONES] = object()
    assert adapter._zone_entities() == []


def test_hvac_zone_state_mismatch_fails_acquisition_and_release_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    acquisition_hass = FakeHass(
        {
            "climate.daikin": "heat",
            "automation.climate": "on",
            "switch.zone": "off",
        }
    )
    acquisition_hass.services.noop_entities.add("switch.zone")
    acquisition_adapter = DaikinHVACAdapter(
        acquisition_hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_CLIMATE_ZONES: ["switch.zone"],
        },
    )

    acquisition_result = asyncio.run(
        acquisition_adapter.async_execute(_action({"hvac_mode": "heat", "enable_zones": True}))
    )

    assert acquisition_result.applied is False
    assert acquisition_result.reason == "hvac_zone_service_failed"
    assert acquisition_result.rollback_succeeded is True
    assert acquisition_hass.states.values["switch.zone"] == "off"
    assert acquisition_hass.states.values["automation.climate"] == "on"

    release_hass = FakeHass({"switch.zone": "on"})
    release_hass.services.noop_entities.add("switch.zone")
    release_result = asyncio.run(DaikinHVACAdapter(release_hass, {}).async_restore({}, {"switch.zone": "off"}))

    assert release_result.applied is False
    assert release_result.reason == "hvac_release_failed"
    assert release_result.saved_zone_states == {"switch.zone": "off"}


def test_hvac_automation_state_mismatch_is_retained_for_release_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    hass = FakeHass({"automation.climate": "off"})
    hass.services.noop_entities.add("automation.climate")
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_CLIMATE_AUTOMATIONS: "automation.climate"},
    )

    result = asyncio.run(adapter.async_restore({"automation.climate": "on"}))

    assert result.applied is False
    assert result.reason == "hvac_release_failed"
    assert result.rollback_succeeded is False
    assert result.saved_automation_states == {"automation.climate": "on"}


def test_hvac_release_waits_for_delayed_automation_state_confirmation() -> None:
    hass = FakeHass({"automation.climate": "off"})
    adapter = DaikinHVACAdapter(hass, {CONF_CLIMATE_AUTOMATIONS: "automation.climate"})

    async def delayed_turn_on(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        hass.services.calls.append((domain, service, data))
        asyncio.get_running_loop().call_later(
            0.02,
            hass.states.values.__setitem__,
            data["entity_id"],
            "on",
        )

    hass.services.async_call = delayed_turn_on

    result = asyncio.run(adapter.async_restore({"automation.climate": "on"}))

    assert result.applied is True
    assert result.reason == "hvac_control_released"
    assert result.saved_automation_states == {}


def test_hvac_zone_release_skips_entities_already_at_the_saved_state() -> None:
    hass = FakeHass(
        {
            "switch.already_on": "on",
            "input_boolean.restore_off": "on",
        }
    )
    adapter = DaikinHVACAdapter(hass, {})

    result = asyncio.run(
        adapter.async_restore(
            {},
            {
                "switch.already_on": "on",
                "input_boolean.restore_off": "off",
            },
        )
    )

    assert result.rollback_succeeded is True
    assert hass.services.calls == [("input_boolean", "turn_off", {"entity_id": "input_boolean.restore_off"})]


def test_hvac_release_restores_persisted_main_state() -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 23})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )

    result = asyncio.run(
        adapter.async_restore(
            {},
            {},
            {"hvac_mode": "heat", "target_temperature": 20},
        )
    )

    assert result.applied is True
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert hass.states.get("climate.daikin").attributes["temperature"] == 20


def test_hvac_release_preserves_manual_main_change_during_restore() -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 23})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    manual_override = False
    supersession_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def async_call_with_manual_override(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_async_call(domain, service, data, blocking)
        if domain == "climate" and service == "set_temperature":
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True

    async def persist_manual_supersession() -> None:
        supersession_boundaries.append(len(hass.services.calls))

    hass.services.async_call = async_call_with_manual_override
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(
        adapter.async_restore(
            {},
            {},
            {"hvac_mode": "heat", "target_temperature": 20},
        )
    )

    assert result.applied is True
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert supersession_boundaries == [1]
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24


def test_hvac_release_retains_invalid_main_state() -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 23})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    invalid_main_state = {"unsupported": "state"}

    result = asyncio.run(adapter.async_restore({}, {}, invalid_main_state))

    assert result.applied is False
    assert result.reason == "hvac_release_failed"
    assert result.rollback_succeeded is False
    assert result.saved_main_state == invalid_main_state
    assert hass.services.calls == []


def test_hvac_action_rejects_unsupported_kind_and_empty_desired_state() -> None:
    hass = FakeHass({"climate.daikin": "heat"})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})
    unsupported = _action({"hvac_mode": "heat"})
    unsupported.kind = ActionKind.EV_START

    unsupported_result = asyncio.run(adapter.async_execute(unsupported))
    empty_result = asyncio.run(adapter.async_execute(_action({})))

    assert unsupported_result.reason == "unsupported_hvac_action"
    assert empty_result.reason == "hvac_desired_state_empty"


def test_hvac_release_preserves_automation_that_was_saved_off() -> None:
    hass = FakeHass({"automation.climate": "off"})
    adapter = DaikinHVACAdapter(hass, {CONF_CLIMATE_AUTOMATIONS: "automation.climate"})

    restored = asyncio.run(adapter.async_restore({"automation.climate": "off"}))
    empty = asyncio.run(adapter.async_restore({}))

    assert restored.applied is True
    assert restored.reason == "hvac_control_released"
    assert empty.applied is True
    assert empty.reason == "hvac_control_released"
    assert ("automation", "turn_on", {"entity_id": "automation.climate"}) not in hass.services.calls
    assert hass.states.values["automation.climate"] == "off"


def test_hvac_takeover_turns_on_climate_and_zones_then_release_restores_zones() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("off", {"temperature": 20}),
            "automation.climate": "on",
            "switch.living": "off",
            "input_boolean.study": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: ["automation.climate"],
            CONF_CLIMATE_ZONES: ["switch.living", "input_boolean.study"],
        },
    )
    turn_on_feedback_phases: list[bool] = []
    coupled_zone_feedback_phases: list[tuple[str | None, str | None, str | None]] = []
    adapter.set_turn_on_feedback_callback(turn_on_feedback_phases.append)
    adapter.set_coupled_zone_feedback_callback(
        lambda entity_id, state, context_id: coupled_zone_feedback_phases.append((entity_id, state, context_id))
    )

    acquired = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                }
            )
        )
    )
    released = asyncio.run(adapter.async_restore(acquired.saved_automation_states, acquired.saved_zone_states))

    assert acquired.saved_zone_states == {"switch.living": "off", "input_boolean.study": "on"}
    assert turn_on_feedback_phases == [True, False]
    assert coupled_zone_feedback_phases[0][:2] == ("switch.living", "on")
    assert coupled_zone_feedback_phases[0][2]
    assert coupled_zone_feedback_phases[1] == (None, None, None)
    assert coupled_zone_feedback_phases[2][:2] == ("switch.living", "off")
    assert coupled_zone_feedback_phases[2][2]
    assert coupled_zone_feedback_phases[3] == (None, None, None)
    zone_service_contexts = [
        context
        for call, context in zip(hass.services.calls, hass.services.contexts, strict=True)
        if call[2].get("entity_id") == "switch.living"
    ]
    assert [context.id for context in zone_service_contexts] == [
        coupled_zone_feedback_phases[0][2],
        coupled_zone_feedback_phases[2][2],
    ]
    assert hass.services.calls[:5] == [
        ("automation", "turn_off", {"entity_id": "automation.climate", "stop_actions": True}),
        ("switch", "turn_on", {"entity_id": "switch.living"}),
        ("climate", "turn_on", {"entity_id": "climate.daikin"}),
        ("climate", "set_hvac_mode", {"entity_id": "climate.daikin", "hvac_mode": "heat"}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 23}),
    ]
    assert released.reason == "hvac_control_released"
    assert hass.states.values["switch.living"] == "off"
    assert hass.states.values["automation.climate"] == "on"


def test_hvac_takeover_sets_main_and_zone_climate_targets() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.living_temperature": FakeState("heat", {"temperature": 21}),
            "climate.bedrooms_temperature": FakeState("heat", {"temperature": 22}),
            "switch.living": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: [
                "switch.living",
                "climate.living_temperature",
                "climate.bedrooms_temperature",
            ],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is True
    assert result.saved_zone_states == {
        "switch.living": "off",
        "climate.living_temperature": {"target_temperature": 21},
        "climate.bedrooms_temperature": {"target_temperature": 22},
    }
    assert adapter.takeover_snapshot() == (
        {},
        {
            "switch.living": "on",
            "climate.living_temperature": {"target_temperature": 23},
            "climate.bedrooms_temperature": {"target_temperature": 23},
        },
    )
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.living"}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 23}),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.living_temperature", "temperature": 23},
        ),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.bedrooms_temperature", "temperature": 23},
        ),
    ]

    released = asyncio.run(adapter.async_restore({}, result.saved_zone_states))

    assert released.reason == "hvac_control_released"
    assert hass.states.get("climate.living_temperature").attributes["temperature"] == 21
    assert hass.states.get("climate.bedrooms_temperature").attributes["temperature"] == 22


def test_hvac_disabled_zone_synchronization_leaves_climate_targets_unchanged() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "switch.living": "off",
            "climate.living_temperature": FakeState("heat", {"temperature": 21}),
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["switch.living", "climate.living_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": False,
                }
            )
        )
    )

    assert result.applied is True
    assert result.saved_zone_states == {"switch.living": "off"}
    assert hass.states.get("climate.daikin").attributes["temperature"] == 23
    assert hass.states.get("climate.living_temperature").attributes["temperature"] == 21
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.living"}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 23}),
    ]


def test_coupled_zone_feedback_is_context_scoped_during_takeover_and_release() -> None:
    entry_data = {
        CONF_DAIKIN_CLIMATE: "climate.daikin",
        CONF_CLIMATE_ZONES: ["switch.living", "climate.living_temperature"],
    }
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "switch.living": "off",
            "climate.living_temperature": FakeState(
                "off",
                {"temperature": 20},
            ),
        }
    )
    adapter = DaikinHVACAdapter(hass, entry_data)
    pending: dict[str, Any] = {
        "enable_zones": True,
        "configured_zones_only": True,
        "target_temperature": 23,
    }
    planner_feedback: list[bool] = []
    pending_conflicts: list[str | None] = []
    unrelated_feedback: list[bool] = []

    def set_coupled_feedback(
        actuator_entity_id: str | None,
        state: str | None,
        context_id: str | None,
    ) -> None:
        pending["coupled_zone_feedback_expected"] = (
            None
            if state is None
            else {
                "actuator_entity_id": actuator_entity_id,
                "context_id": context_id,
                "state": state,
            }
        )

    def zone_event(old_mode: str, new_mode: str, context: Context) -> Any:
        return SimpleNamespace(
            context=context,
            data={
                "entity_id": "climate.living_temperature",
                "old_state": FakeState(old_mode, {"temperature": 20}),
                "new_state": FakeState(new_mode, {"temperature": 20}),
            },
        )

    original_async_call = hass.services.async_call

    async def publish_coupled_feedback(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        context: Context | None = None,
    ) -> None:
        if data.get("entity_id") == "switch.living":
            assert context is not None
            old_mode, new_mode = ("off", "heat") if service == "turn_on" else ("heat", "off")
            event = zone_event(old_mode, new_mode, context)
            planner_feedback.append(
                _is_planner_owned_control_feedback(
                    entry_data,
                    {"execution_audit": []},
                    event,
                    datetime.now(UTC),
                    pending_hvac_desired_state=pending,
                )
            )
            pending_conflicts.append(
                _pending_zone_hvac_manual_change_entity_id(
                    entry_data,
                    event,
                    pending,
                )
            )
            unrelated_feedback.append(
                _is_planner_owned_control_feedback(
                    entry_data,
                    {"execution_audit": []},
                    zone_event(
                        old_mode,
                        new_mode,
                        Context(user_id="manual-user"),
                    ),
                    datetime.now(UTC),
                    pending_hvac_desired_state=pending,
                )
            )
            hass.states.values["climate.living_temperature"] = FakeState(
                new_mode,
                {"temperature": 20},
            )
        await original_async_call(domain, service, data, blocking, context)

    hass.services.async_call = publish_coupled_feedback
    adapter.set_coupled_zone_feedback_callback(set_coupled_feedback)

    acquired = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )
    pending["restore_zones"] = acquired.saved_zone_states
    released = asyncio.run(adapter.async_restore({}, acquired.saved_zone_states))

    assert acquired.applied is True
    assert released.applied is True
    assert planner_feedback == [True, True]
    assert pending_conflicts == [None, None]
    assert unrelated_feedback == [False, False]


def test_hvac_pending_manual_override_stops_before_actuator_commands() -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 24})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    adapter.set_manual_override_check(lambda: True)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 21})))

    assert result.applied is False
    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24
    assert hass.services.calls == []


def test_hvac_main_and_zone_supersession_share_one_durable_boundary() -> None:
    adapter = DaikinHVACAdapter(FakeHass({}), {})
    persisted: list[tuple[bool, set[str]]] = []

    async def persist_supersessions(
        main_superseded: bool,
        zone_entity_ids: set[str],
    ) -> None:
        persisted.append((main_superseded, set(zone_entity_ids)))

    adapter.set_manual_override_check(lambda: True)
    adapter.set_zone_manual_override_check(lambda: {"climate.manual_zone"})
    adapter.set_manual_supersession_persistence_callback(persist_supersessions)

    asyncio.run(adapter._async_persist_requested_manual_supersessions())
    asyncio.run(adapter._async_persist_requested_manual_supersessions())

    assert persisted == [(True, {"climate.manual_zone"})]


def test_hvac_manual_override_during_automation_disable_aborts_before_zones() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 24}),
            "automation.climate": "on",
            "automation.second": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: ["automation.climate", "automation.second"],
        },
    )
    manual_override = False
    supersession_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def persist_manual_supersession() -> None:
        supersession_boundaries.append(len(hass.services.calls))

    async def async_call_with_manual_override(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_async_call(domain, service, data, blocking)
        if domain == "automation" and service == "turn_off":
            manual_override = True

    hass.services.async_call = async_call_with_manual_override
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 21})))

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert supersession_boundaries == [1]
    assert hass.services.calls[1] == (
        "automation",
        "turn_on",
        {"entity_id": "automation.climate"},
    )
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24
    assert hass.states.get("automation.climate").state == "on"
    assert hass.states.get("automation.second").state == "on"
    assert all(call[2].get("entity_id") != "automation.second" for call in hass.services.calls)
    assert all(call[0] != "climate" for call in hass.services.calls)


def test_hvac_subordinate_loops_stop_when_confirmation_publishes_manual_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zone_hass = FakeHass(
        {
            "switch.zone_one": "off",
            "switch.zone_two": "off",
        }
    )
    zone_adapter = DaikinHVACAdapter(zone_hass, {})
    zone_manual = False

    async def confirm_zone(entity_id: str, expected_state: str) -> bool:
        nonlocal zone_manual
        zone_manual = True
        return True

    monkeypatch.setattr(zone_adapter, "_async_confirm_state", confirm_zone)
    zone_adapter.set_zone_manual_override_check(lambda: {"switch.zone_one"} if zone_manual else set())

    zones_enabled, changed_zones = asyncio.run(
        zone_adapter._async_enable_zones({"switch.zone_one": "off", "switch.zone_two": "off"})
    )

    assert zones_enabled is False
    assert changed_zones == {"switch.zone_one": "off"}
    assert zone_hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.zone_one"}),
    ]

    automation_hass = FakeHass(
        {
            "automation.one": "on",
            "automation.two": "on",
        }
    )
    automation_adapter = DaikinHVACAdapter(automation_hass, {})
    main_manual = False

    async def confirm_automation(entity_id: str, expected_state: str) -> bool:
        nonlocal main_manual
        main_manual = True
        return True

    monkeypatch.setattr(
        automation_adapter,
        "_async_confirm_state",
        confirm_automation,
    )
    automation_adapter.set_manual_override_check(lambda: main_manual)

    disabled, changed_automations = asyncio.run(
        automation_adapter._async_disable_automations({"automation.one": "on", "automation.two": "on"})
    )

    assert disabled is False
    assert changed_automations == {"automation.one": "on"}
    assert automation_hass.services.calls == [
        (
            "automation",
            "turn_off",
            {"entity_id": "automation.one", "stop_actions": True},
        ),
    ]


def test_hvac_manual_override_during_failed_automation_disable_is_persisted_before_rollback() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: ["automation.climate"],
        },
    )
    manual_override = False
    supersession_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def fail_disable_after_manual_override(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_async_call(domain, service, data, blocking)
        if domain == "automation" and service == "turn_off":
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True
            raise RuntimeError("disable failed after manual change")

    async def persist_manual_supersession() -> None:
        supersession_boundaries.append(len(hass.services.calls))

    hass.services.async_call = fail_disable_after_manual_override
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 21})))

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert supersession_boundaries == [1]
    assert hass.services.calls[1] == (
        "automation",
        "turn_on",
        {"entity_id": "automation.climate"},
    )
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24


def test_hvac_manual_override_during_main_snapshot_flush_stops_mode_command() -> None:
    hass = FakeHass({"climate.daikin": FakeState("off", {"temperature": 20})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    manual_override = False
    persisted_supersessions = 0

    async def persist_main_state(_saved_state: dict[str, Any]) -> None:
        nonlocal manual_override
        hass.states.values["climate.daikin"] = FakeState(
            "cool",
            {"temperature": 24},
        )
        manual_override = True

    async def persist_manual_supersession() -> None:
        nonlocal persisted_supersessions
        persisted_supersessions += 1

    adapter.set_main_state_persistence_callback(persist_main_state)
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 21})))

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert persisted_supersessions == 1
    assert hass.states.get("climate.daikin").state == "cool"
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24
    assert all(call[1] != "set_hvac_mode" for call in hass.services.calls)


def test_hvac_manual_zone_target_is_preserved_while_other_actuators_roll_back() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.manual_zone": FakeState("heat", {"temperature": 22}),
            "climate.other_zone": FakeState("heat", {"temperature": 23}),
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.manual_zone", "climate.other_zone"],
            CONF_CLIMATE_AUTOMATIONS: ["automation.climate"],
        },
    )
    manual_zone_override = False
    persisted_zone_supersessions: list[tuple[set[str], int]] = []
    original_async_call = hass.services.async_call

    async def apply_manual_zone_change(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_zone_override
        await original_async_call(domain, service, data, blocking)
        if (
            domain == "climate"
            and service == "set_temperature"
            and data.get("entity_id") == "climate.manual_zone"
            and data.get("temperature") == 21
        ):
            hass.states.values["climate.manual_zone"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_zone_override = True

    async def persist_zone_supersession(entity_ids: set[str]) -> None:
        persisted_zone_supersessions.append((set(entity_ids), len(hass.services.calls)))

    hass.services.async_call = apply_manual_zone_change
    adapter.set_zone_manual_override_check(lambda: {"climate.manual_zone"} if manual_zone_override else set())
    adapter.set_zone_manual_override_persistence_callback(persist_zone_supersession)

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 21,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert persisted_zone_supersessions == [({"climate.manual_zone"}, 3)]
    assert hass.states.get("climate.daikin").attributes["temperature"] == 20
    assert hass.states.get("climate.manual_zone").attributes["temperature"] == 24
    assert hass.states.get("climate.other_zone").attributes["temperature"] == 23
    assert hass.states.get("automation.climate").state == "on"


def test_hvac_zone_changed_during_restore_is_dropped_before_its_command() -> None:
    hass = FakeHass(
        {
            "climate.first_zone": FakeState("heat", {"temperature": 23}),
            "climate.manual_zone": FakeState("heat", {"temperature": 23}),
        }
    )
    adapter = DaikinHVACAdapter(hass, {})
    manual_zone_override = False
    persisted_zone_supersessions: list[set[str]] = []
    original_async_call = hass.services.async_call

    async def change_second_zone_during_first_restore(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_zone_override
        await original_async_call(domain, service, data, blocking)
        if data.get("entity_id") == "climate.first_zone":
            hass.states.values["climate.manual_zone"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_zone_override = True

    async def persist_zone_supersession(entity_ids: set[str]) -> None:
        persisted_zone_supersessions.append(set(entity_ids))

    hass.services.async_call = change_second_zone_during_first_restore
    adapter.set_zone_manual_override_check(lambda: {"climate.manual_zone"} if manual_zone_override else set())
    adapter.set_zone_manual_override_persistence_callback(persist_zone_supersession)

    result = asyncio.run(
        adapter.async_restore(
            {},
            {
                "climate.first_zone": {"target_temperature": 20},
                "climate.manual_zone": {"target_temperature": 21},
            },
        )
    )

    assert result.rollback_succeeded is True
    assert result.saved_zone_states == {}
    assert persisted_zone_supersessions == [{"climate.manual_zone"}]
    assert hass.states.get("climate.first_zone").attributes["temperature"] == 20
    assert hass.states.get("climate.manual_zone").attributes["temperature"] == 24
    assert all(call[2].get("entity_id") != "climate.manual_zone" for call in hass.services.calls)


def test_hvac_zone_changed_after_restore_command_is_not_retained_for_retry(
    monkeypatch: object,
) -> None:
    hass = FakeHass({"climate.manual_zone": FakeState("heat", {"temperature": 23})})
    adapter = DaikinHVACAdapter(hass, {})
    manual_zone_override = False
    persisted_zone_supersessions: list[set[str]] = []
    original_async_call = hass.services.async_call

    async def supersede_restore_command(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_zone_override
        await original_async_call(domain, service, data, blocking)
        hass.states.values["climate.manual_zone"] = FakeState(
            "heat",
            {"temperature": 24},
        )
        manual_zone_override = True

    async def persist_zone_supersession(entity_ids: set[str]) -> None:
        persisted_zone_supersessions.append(set(entity_ids))

    hass.services.async_call = supersede_restore_command
    adapter.set_zone_manual_override_check(lambda: {"climate.manual_zone"} if manual_zone_override else set())
    adapter.set_zone_manual_override_persistence_callback(persist_zone_supersession)
    monkeypatch.setattr(
        hvac_adapter_module,
        "_STATE_CONFIRMATION_TIMEOUT_SECONDS",
        0,
    )

    result = asyncio.run(
        adapter.async_restore(
            {},
            {"climate.manual_zone": {"target_temperature": 20}},
        )
    )

    assert result.rollback_succeeded is True
    assert result.saved_zone_states == {}
    assert persisted_zone_supersessions == [{"climate.manual_zone"}]
    assert hass.states.get("climate.manual_zone").attributes["temperature"] == 24


def test_hvac_zone_supersession_during_main_restore_is_flushed_before_automation() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 23}),
            "switch.manual_zone": "on",
            "automation.climate": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    manual_zone_override = False
    persistence_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def change_zone_during_main_restore(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_zone_override
        await original_async_call(domain, service, data, blocking)
        if data.get("entity_id") == "climate.daikin":
            hass.states.values["switch.manual_zone"] = "off"
            manual_zone_override = True

    async def persist_zone_supersession(_entity_ids: set[str]) -> None:
        persistence_boundaries.append(len(hass.services.calls))

    hass.services.async_call = change_zone_during_main_restore
    adapter.set_zone_manual_override_check(lambda: {"switch.manual_zone"} if manual_zone_override else set())
    adapter.set_zone_manual_override_persistence_callback(persist_zone_supersession)

    result = asyncio.run(
        adapter.async_restore(
            {"automation.climate": "on"},
            {},
            {"hvac_mode": "heat", "target_temperature": 20},
        )
    )

    assert result.rollback_succeeded is True
    assert persistence_boundaries == [1]
    assert hass.services.calls[1] == (
        "automation",
        "turn_on",
        {"entity_id": "automation.climate"},
    )
    assert hass.states.get("switch.manual_zone").state == "off"


def test_hvac_main_supersession_during_zone_restore_is_flushed_before_automation() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.zone": FakeState("heat", {"temperature": 23}),
            "automation.climate": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    manual_override = False
    persistence_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def change_main_during_zone_restore(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_async_call(domain, service, data, blocking)
        if data.get("entity_id") == "climate.zone":
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True

    async def persist_main_supersession() -> None:
        persistence_boundaries.append(len(hass.services.calls))

    hass.services.async_call = change_main_during_zone_restore
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_main_supersession)

    result = asyncio.run(
        adapter.async_restore(
            {"automation.climate": "on"},
            {"climate.zone": {"target_temperature": 20}},
            {"hvac_mode": "heat", "target_temperature": 20},
        )
    )

    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert persistence_boundaries == [1]
    assert hass.services.calls[1] == (
        "automation",
        "turn_on",
        {"entity_id": "automation.climate"},
    )
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24


def test_hvac_manual_override_during_zone_enable_aborts_before_main() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 24}),
            "switch.zone": "off",
            "switch.second_zone": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["switch.zone", "switch.second_zone"],
        },
    )
    manual_override = False
    supersession_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def persist_manual_supersession() -> None:
        supersession_boundaries.append(len(hass.services.calls))

    async def async_call_with_manual_override(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_async_call(domain, service, data, blocking)
        if data.get("entity_id") == "switch.zone" and service == "turn_on":
            manual_override = True

    hass.services.async_call = async_call_with_manual_override
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 21,
                    "enable_zones": True,
                }
            )
        )
    )

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24
    assert hass.states.get("switch.zone").state == "off"
    assert hass.states.get("switch.second_zone").state == "off"
    assert all(call[2].get("entity_id") != "switch.second_zone" for call in hass.services.calls)
    assert all(call[0] != "climate" for call in hass.services.calls)


def test_hvac_manual_override_before_forced_retry_preserves_user_target(
    monkeypatch: object,
) -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 20})})
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    manual_override = False
    guard_calls = 0

    async def arm_guard() -> bool:
        nonlocal guard_calls, manual_override
        guard_calls += 1
        if guard_calls == 2:
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True
        return True

    async def reject_confirmation(
        entity_id: str,
        desired_state: dict[str, Any],
    ) -> bool:
        return False

    monkeypatch.setattr(adapter, "_async_arm_scheduler_guard", arm_guard)
    monkeypatch.setattr(
        adapter,
        "_async_confirm_complete_hvac_state",
        reject_confirmation,
    )
    adapter.set_manual_override_check(lambda: manual_override)

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 21})))

    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24
    assert hass.services.calls == [
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.daikin", "temperature": 21},
        )
    ]


def test_hvac_manual_override_during_zone_updates_aborts_retry_and_preserves_main() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.zone": FakeState("heat", {"temperature": 22}),
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone"],
            CONF_CLIMATE_AUTOMATIONS: ["automation.climate"],
        },
    )
    manual_override = False
    supersession_boundaries: list[int] = []
    original_async_call = hass.services.async_call

    async def persist_manual_supersession() -> None:
        supersession_boundaries.append(len(hass.services.calls))

    async def async_call_with_manual_override(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_async_call(domain, service, data, blocking)
        if (
            domain == "climate"
            and service == "set_temperature"
            and data.get("entity_id") == "climate.zone"
            and data.get("temperature") == 21
        ):
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True

    hass.services.async_call = async_call_with_manual_override
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 21,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "manual_hvac_override_detected"
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert supersession_boundaries == [3]
    assert hass.services.calls[3] == (
        "climate",
        "set_temperature",
        {"entity_id": "climate.zone", "temperature": 22},
    )
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24
    assert hass.states.get("climate.zone").attributes["temperature"] == 22
    assert hass.states.get("automation.climate").state == "on"
    assert [
        call
        for call in hass.services.calls
        if call[0:2] == ("climate", "set_temperature") and call[2].get("entity_id") == "climate.daikin"
    ] == [
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.daikin", "temperature": 21},
        )
    ]


def test_hvac_zone_climate_restore_retains_unresolved_targets(monkeypatch: object) -> None:
    failed_hass = FakeHass({"climate.failed_zone": FakeState("heat", {"temperature": 23})})
    failed_hass.services.fail_services.add(("climate", "set_temperature"))
    failed = asyncio.run(
        DaikinHVACAdapter(failed_hass, {}).async_restore(
            {},
            {"climate.failed_zone": {"target_temperature": 20}},
        )
    )

    noop_hass = FakeHass({"climate.noop_zone": FakeState("heat", {"temperature": 23})})
    noop_hass.services.noop_entities.add("climate.noop_zone")
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0)
    unconfirmed = asyncio.run(
        DaikinHVACAdapter(noop_hass, {}).async_restore(
            {},
            {"climate.noop_zone": {"target_temperature": 20}},
        )
    )

    assert failed.reason == "hvac_release_failed"
    assert failed.saved_zone_states == {
        "climate.failed_zone": {"target_temperature": 20},
    }
    assert unconfirmed.reason == "hvac_release_failed"
    assert unconfirmed.saved_zone_states == {
        "climate.noop_zone": {"target_temperature": 20},
    }


def test_hvac_zone_climate_snapshot_preserves_temperature_range() -> None:
    hass = FakeHass(
        {
            "climate.zone": FakeState(
                "heat_cool",
                {"target_temp_low": 19, "target_temp_high": 24},
            )
        }
    )

    snapshot = DaikinHVACAdapter(
        hass,
        {CONF_CLIMATE_ZONES: ["climate.zone"]},
    ).takeover_snapshot()

    assert snapshot == (
        {},
        {"climate.zone": {"target_temp_low": 19, "target_temp_high": 24}},
    )


def test_hvac_takeover_targets_main_climate_before_configured_zone_climates() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("off", {"temperature": 20}),
            "climate.living_temperature": FakeState("heat", {"temperature": 21}),
            "switch.living": "off",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["switch.living", "climate.living_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is True
    assert hass.states.get("climate.daikin").attributes["temperature"] == 23
    assert hass.states.get("climate.living_temperature").attributes["temperature"] == 23
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.living"}),
        ("climate", "turn_on", {"entity_id": "climate.daikin"}),
        ("climate", "set_hvac_mode", {"entity_id": "climate.daikin", "hvac_mode": "heat"}),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.daikin", "temperature": 23},
        ),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.living_temperature", "temperature": 23},
        ),
    ]


def test_hvac_zone_only_targeting_fails_closed_without_zone_thermostat() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "switch.living": "off",
            "automation.climate": "on",
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: ["automation.climate"],
            CONF_CLIMATE_ZONES: ["switch.living"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "zone_only_preconditioning_requires_climate_zone"
    assert result.rollback_succeeded is True
    assert hass.services.calls == []


def test_hvac_zone_target_waits_for_confirmed_main_target(monkeypatch: object) -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.zone_temperature": FakeState("heat", {"temperature": 20}),
        }
    )
    hass.services.noop_entities.add("climate.daikin")
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.hvac_adapter._STATE_CONFIRMATION_TIMEOUT_SECONDS",
        0.0,
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "hvac_state_confirmation_failed"
    assert all(call[2]["entity_id"] != "climate.zone_temperature" for call in hass.services.calls)


def test_hvac_zone_climate_target_confirmation_failure_fails_closed(
    monkeypatch: object,
) -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.first_zone": FakeState("heat", {"temperature": 20}),
            "climate.failed_zone": FakeState("heat", {"temperature": 20}),
        }
    )
    hass.services.noop_entities.add("climate.failed_zone")
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.first_zone", "climate.failed_zone"],
        },
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.hvac_adapter._STATE_CONFIRMATION_TIMEOUT_SECONDS",
        0.0,
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "hvac_state_confirmation_failed"
    assert result.rollback_succeeded is True
    first_zone_calls = [call for call in hass.services.calls if call[2]["entity_id"] == "climate.first_zone"]
    assert first_zone_calls == [
        ("climate", "set_temperature", {"entity_id": "climate.first_zone", "temperature": 23}),
        ("climate", "set_temperature", {"entity_id": "climate.first_zone", "temperature": 23}),
        ("climate", "set_temperature", {"entity_id": "climate.first_zone", "temperature": 20}),
    ]
    assert hass.states.get("climate.first_zone").attributes["temperature"] == 20
    assert hass.states.get("climate.daikin").attributes["temperature"] == 20


def test_hvac_zone_failure_restores_main_target_and_off_mode() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("off", {"temperature": 20}),
            "climate.zone_temperature": FakeState("heat", {"temperature": 20}),
        }
    )
    original_call = hass.services.async_call
    remembered_mode = "cool"
    persisted_main_states: list[tuple[dict[str, Any], int]] = []
    pending_main_restores: list[dict[str, Any]] = []
    pending_zone_restores: list[dict[str, Any]] = []

    async def fail_zone_and_preserve_main_attributes(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal remembered_mode
        if domain == "climate" and service == "set_temperature" and data["entity_id"] == "climate.zone_temperature":
            raise RuntimeError("zone target rejected")
        if domain == "climate" and data["entity_id"] == "climate.daikin":
            current = hass.states.get("climate.daikin")
            attributes = {} if current is None else dict(current.attributes)
            if service == "turn_on":
                hass.services.calls.append((domain, service, data))
                hass.states.values["climate.daikin"] = FakeState(
                    remembered_mode,
                    attributes,
                )
                return
            if service == "set_hvac_mode":
                remembered_mode = str(data["hvac_mode"])
        if domain == "climate" and service == "turn_off" and data["entity_id"] == "climate.daikin":
            hass.services.calls.append((domain, service, data))
            current = hass.states.get("climate.daikin")
            hass.states.values["climate.daikin"] = FakeState(
                "off",
                {} if current is None else dict(current.attributes),
            )
            return
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_zone_and_preserve_main_attributes
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )

    async def persist_main_state(saved_state: dict[str, Any]) -> None:
        persisted_main_states.append((dict(saved_state), len(hass.services.calls)))

    adapter.set_main_state_persistence_callback(persist_main_state)
    adapter.set_pending_main_restore_callback(lambda saved_state: pending_main_restores.append(dict(saved_state)))
    adapter.set_pending_zone_restore_callback(lambda saved_states: pending_zone_restores.append(dict(saved_states)))

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    main_state = hass.states.get("climate.daikin")
    assert result.applied is False
    assert result.reason == "hvac_control_service_failed"
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert main_state.state == "off"
    assert main_state.attributes["temperature"] == 20
    assert remembered_mode == "cool"
    assert persisted_main_states == [
        (
            {
                "hvac_mode": "off",
                "target_temperature": 20,
                "rollback_hvac_mode_changed": True,
                "rollback_active_hvac_mode": "cool",
            },
            1,
        )
    ]
    assert pending_main_restores == [persisted_main_states[0][0]]
    assert pending_zone_restores == [{"climate.zone_temperature": {"target_temperature": 20}}]
    assert hass.services.calls[persisted_main_states[0][1]] == (
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.daikin", "hvac_mode": "heat"},
    )


@pytest.mark.parametrize("failed_step", ["active_mode", "target", "turn_off"])
def test_hvac_off_restore_attempts_turn_off_after_partial_failure(
    monkeypatch: object,
    failed_step: str,
) -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 23})})
    original_call = hass.services.async_call

    async def fail_selected_restore_step(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        selected_failure = (
            (failed_step == "active_mode" and service == "set_hvac_mode" and data.get("hvac_mode") == "cool")
            or (failed_step == "target" and service == "set_temperature" and data.get("temperature") == 20)
            or (failed_step == "turn_off" and service == "turn_off")
        )
        if domain == "climate" and selected_failure:
            hass.services.calls.append((domain, service, data))
            raise RuntimeError("restore step failed")
        if domain == "climate" and service == "turn_off":
            hass.services.calls.append((domain, service, data))
            current = hass.states.get(data["entity_id"])
            hass.states.values[data["entity_id"]] = FakeState(
                "off",
                {} if current is None else dict(current.attributes),
            )
            return
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_selected_restore_step
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    monkeypatch.setattr(hvac_adapter_module, "_STATE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    saved_main_state = {
        "hvac_mode": "off",
        "target_temperature": 20,
        "rollback_hvac_mode_changed": True,
        "rollback_active_hvac_mode": "cool",
    }

    result = asyncio.run(adapter.async_restore({}, {}, saved_main_state))

    assert result.applied is False
    assert result.rollback_succeeded is False
    assert result.saved_main_state == saved_main_state
    turn_off_calls = [call for call in hass.services.calls if call[:2] == ("climate", "turn_off")]
    assert turn_off_calls == [("climate", "turn_off", {"entity_id": "climate.daikin"})]
    assert hass.states.get("climate.daikin").state == ("cool" if failed_step == "turn_off" else "off")


@pytest.mark.parametrize(
    ("manual_step", "expected_call_count"),
    [("active_mode", 1), ("target", 2), ("turn_off", 3)],
)
def test_hvac_off_restore_stops_at_manual_main_supersession(
    manual_step: str,
    expected_call_count: int,
) -> None:
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 23})})
    manual_override = False
    supersession_boundaries: list[int] = []
    original_call = hass.services.async_call

    async def supersede_selected_restore_step(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal manual_override
        await original_call(domain, service, data, blocking)
        selected = (
            (manual_step == "active_mode" and service == "set_hvac_mode" and data.get("hvac_mode") == "cool")
            or (manual_step == "target" and service == "set_temperature" and data.get("temperature") == 20)
            or (manual_step == "turn_off" and service == "turn_off")
        )
        if selected:
            hass.states.values["climate.daikin"] = FakeState(
                "heat",
                {"temperature": 24},
            )
            manual_override = True

    async def persist_manual_supersession() -> None:
        supersession_boundaries.append(len(hass.services.calls))

    hass.services.async_call = supersede_selected_restore_step
    adapter = DaikinHVACAdapter(
        hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )
    adapter.set_manual_override_check(lambda: manual_override)
    adapter.set_manual_override_persistence_callback(persist_manual_supersession)
    saved_main_state = {
        "hvac_mode": "off",
        "target_temperature": 20,
        "rollback_hvac_mode_changed": True,
        "rollback_active_hvac_mode": "cool",
    }

    result = asyncio.run(adapter.async_restore({}, {}, saved_main_state))

    assert result.applied is True
    assert result.rollback_succeeded is True
    assert result.saved_main_state == {}
    assert len(hass.services.calls) == expected_call_count
    assert supersession_boundaries == [expected_call_count]
    assert hass.states.get("climate.daikin").state == "heat"
    assert hass.states.get("climate.daikin").attributes["temperature"] == 24


def test_hvac_rollback_retains_main_state_when_last_active_mode_is_unavailable() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("off", {"temperature": 20}),
            "climate.zone_temperature": FakeState("heat", {"temperature": 20}),
        }
    )
    original_call = hass.services.async_call

    async def fail_zone_and_preserve_main_attributes(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        if domain == "climate" and service == "set_temperature" and data["entity_id"] == "climate.zone_temperature":
            raise RuntimeError("zone target rejected")
        if domain == "climate" and service == "turn_off" and data["entity_id"] == "climate.daikin":
            hass.services.calls.append((domain, service, data))
            current = hass.states.get("climate.daikin")
            hass.states.values["climate.daikin"] = FakeState(
                "off",
                {} if current is None else dict(current.attributes),
            )
            return
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_zone_and_preserve_main_attributes
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "hvac_acquisition_rollback_failed"
    assert result.rollback_succeeded is False
    assert result.saved_main_state == {
        "hvac_mode": "off",
        "target_temperature": 20,
        "rollback_hvac_mode_changed": True,
    }
    assert hass.states.get("climate.daikin") == FakeState(
        "off",
        {"temperature": 20},
    )


def test_hvac_target_change_rejects_unrestorable_main_target_when_zone_sync_disabled() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"current_temperature": 20}),
            "climate.zone_temperature": FakeState("heat", {"temperature": 20}),
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": False,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "main_climate_target_unavailable"
    assert result.rollback_succeeded is True
    assert hass.services.calls == []


def test_hvac_zone_sync_rejects_unrestorable_subordinate_target() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.zone_temperature": FakeState(
                "heat",
                {"current_temperature": 20},
            ),
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "climate_zone_target_unavailable"
    assert result.rollback_succeeded is True
    assert hass.services.calls == []


def test_hvac_zone_failure_reports_when_main_target_rollback_fails() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.zone_temperature": FakeState("heat", {"temperature": 20}),
        }
    )
    original_call = hass.services.async_call

    async def fail_zone_and_main_restore(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        if (
            domain == "climate"
            and service == "set_temperature"
            and (data["entity_id"] == "climate.zone_temperature" or data.get("temperature") == 20)
        ):
            raise RuntimeError("target service failed")
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_zone_and_main_restore
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "target_temperature": 23,
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is False
    assert result.reason == "hvac_acquisition_rollback_failed"
    assert result.rollback_succeeded is False
    assert result.saved_main_state == {
        "hvac_mode": "heat",
        "target_temperature": 20,
    }
    assert hass.states.get("climate.daikin").attributes["temperature"] == 23


def test_hvac_zone_climate_without_target_requires_only_availability() -> None:
    hass = FakeHass(
        {
            "climate.daikin": FakeState("heat", {"temperature": 20}),
            "climate.zone_temperature": FakeState("heat", {"temperature": 21}),
        }
    )
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_ZONES: ["climate.zone_temperature"],
        },
    )

    result = asyncio.run(
        adapter.async_execute(
            _action(
                {
                    "hvac_mode": "heat",
                    "enable_zones": True,
                    "configured_zones_only": True,
                }
            )
        )
    )

    assert result.applied is True
    assert result.reason == "already_in_desired_hvac_state"
    assert hass.services.calls == []


def test_hvac_action_turns_off_climate_and_sets_temperature_range() -> None:
    off_hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    off_adapter = DaikinHVACAdapter(
        off_hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin", CONF_CLIMATE_AUTOMATIONS: "automation.climate"},
    )
    range_hass = FakeHass(
        {
            "climate.daikin": FakeState(
                "heat",
                {"target_temp_low": 18, "target_temp_high": 26},
            ),
            "automation.climate": "off",
        }
    )
    range_adapter = DaikinHVACAdapter(
        range_hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin", CONF_CLIMATE_AUTOMATIONS: ["automation.climate"]},
    )

    off_result = asyncio.run(off_adapter.async_execute(_action({"hvac_mode": "off"})))
    range_result = asyncio.run(
        range_adapter.async_execute(_action({"hvac_mode": "cool", "target_temp_low": 20, "target_temp_high": 24}))
    )

    assert off_result.applied is True
    assert ("climate", "turn_off", {"entity_id": "climate.daikin"}) in off_hass.services.calls
    assert range_result.applied is True
    assert (
        "climate",
        "set_temperature",
        {"entity_id": "climate.daikin", "target_temp_low": 20, "target_temp_high": 24},
    ) in range_hass.services.calls


def test_hvac_already_state_checks_temperature_ranges_and_invalid_numbers() -> None:
    adapter = DaikinHVACAdapter(
        FakeHass({"climate.daikin": FakeState("cool", {"target_temp_low": "bad", "target_temp_high": 24})}),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
    )

    result = asyncio.run(
        adapter.async_execute(_action({"hvac_mode": "cool", "target_temp_low": 20, "target_temp_high": 24}))
    )

    assert result.applied is True
    assert result.reason == "hvac_action_applied"


def test_hvac_dispatch_timeout_bounds_failed_compensation(monkeypatch: Any) -> None:
    from custom_components.ha_energy_planner import adapter_helpers

    monkeypatch.setattr(adapter_helpers, "DEVICE_SERVICE_TIMEOUT_SECONDS", 0.005)
    hass = FakeHass({"climate.daikin": FakeState("heat", {"temperature": 19}), "automation.climate": "on"})
    calls: list[str] = []

    async def hung(
        domain: str, service: str, data: dict[str, Any], blocking: bool = False, context: Any = None
    ) -> None:
        calls.append(service)
        if len(calls) == 1:
            hass.states.values[data["entity_id"]] = "off"
        await asyncio.Event().wait()

    hass.services.async_call = hung
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )

    async def run() -> Any:
        async with asyncio.timeout(1):
            return await adapter.async_execute(_action({"hvac_mode": "off", "suppress_automations": True}))

    result = asyncio.run(run())
    assert not result.applied
    assert result.rollback_succeeded is False
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert calls == ["turn_off", "turn_on"]
