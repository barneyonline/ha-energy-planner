"""Tests for EV Smart Charging adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import (
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
)
from custom_components.ha_energy_planner.ev_adapter import EVSmartChargingAdapter
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str


class FakeStates:
    """Minimal HA states registry."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeServices:
    """Minimal HA service bus."""

    def __init__(self, states: FakeStates) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        entity_id = data["entity_id"]
        if service in {"turn_on", "press"}:
            self.states.values[entity_id] = "on"
        elif service == "turn_off":
            self.states.values[entity_id] = "off"
        elif "value" in data:
            self.states.values[entity_id] = str(data["value"])
        elif "time" in data:
            self.states.values[entity_id] = str(data["time"])

    def has_service(self, domain: str, service: str) -> bool:
        return True


class PreflightServices(FakeServices):
    """Service bus with selective service availability."""

    def has_service(self, domain: str, service: str) -> bool:
        return not (domain == "input_datetime" and service == "set_datetime")


class FailingServices(FakeServices):
    """Service bus that raises for helper writes."""

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        raise RuntimeError("service unavailable")


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str]) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states)


class FailingHass(FakeHass):
    """HA object with failing services."""

    def __init__(self, values: dict[str, str]) -> None:
        self.states = FakeStates(values)
        self.services = FailingServices(self.states)


class PreflightHass(FakeHass):
    """HA object with a missing ready-by helper service."""

    def __init__(self, values: dict[str, str]) -> None:
        self.states = FakeStates(values)
        self.services = PreflightServices(self.states)


def _action(kind: ActionKind, desired_state: dict[str, Any] | None = None) -> PlanAction:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return PlanAction(
        action_id=kind,
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=kind,
        desired_state=desired_state or {},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )


def test_ev_schedule_sets_helpers_then_starts() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "connected_not_charging",
            "switch.ev_start": "off",
            "switch.ev_stop": "on",
            "input_number.ev_target_soc": "50",
            "input_text.ev_ready_by": "06:00",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "switch.ev_stop",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "input_number.ev_target_soc",
            CONF_EV_SMART_CHARGING_READY_BY: "input_text.ev_ready_by",
        },
    )
    result = asyncio.run(
        adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80, "ready_by": "07:00"}))
    )
    assert result.applied is True
    assert hass.services.calls == [
        ("input_number", "set_value", {"entity_id": "input_number.ev_target_soc", "value": 80}),
        ("input_text", "set_value", {"entity_id": "input_text.ev_ready_by", "value": "07:00"}),
        ("switch", "turn_on", {"entity_id": "switch.ev_start"}),
    ]


def test_ev_schedule_skips_helper_writes_when_values_already_match() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "input_number.ev_target_soc": "80.0",
            "input_datetime.ev_ready_by": "07:00:00",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "input_number.ev_target_soc",
            CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by",
        },
    )

    result = asyncio.run(
        adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80, "ready_by": "07:00"}))
    )

    assert result.applied is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.ev_start"}),
    ]


def test_ev_schedule_skips_every_command_when_helpers_and_start_state_match() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "on",
            "input_number.ev_target_soc": "80.01",
            "input_text.ev_ready_by": "07:00",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "input_number.ev_target_soc",
            CONF_EV_SMART_CHARGING_READY_BY: "input_text.ev_ready_by",
        },
    )

    result = asyncio.run(
        adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80, "ready_by": "07:00"}))
    )

    assert result.applied is True
    assert result.reason == "already_in_desired_state"
    assert hass.services.calls == []


def test_ev_start_presses_unknown_button_control() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "on", "button.ev_start": "unknown"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is True
    assert result.reason == "button_press_called"
    assert hass.services.calls == [
        ("button", "press", {"entity_id": "button.ev_start"}),
    ]


def test_ev_stop_presses_dedicated_unknown_button_control() -> None:
    hass = FakeHass({"button.ev_stop": "unknown"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_SMART_CHARGING_STOP: "button.ev_stop",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_STOP)))

    assert result.applied is True
    assert result.reason == "button_press_called"
    assert hass.services.calls == [
        ("button", "press", {"entity_id": "button.ev_stop"}),
    ]


def test_ev_stop_does_not_press_legacy_single_button_control() -> None:
    hass = FakeHass({"button.ev_control": "unknown"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            "ev_smart_charging_entity": "button.ev_control",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_STOP)))

    assert result.applied is False
    assert result.reason == "ev_control_unavailable"
    assert hass.services.calls == []


def test_ev_start_fails_when_disconnected() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "off", "switch.ev_start": "off"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
        },
    )
    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))
    assert result.applied is False
    assert result.reason == "ev_not_connected"
    assert hass.services.calls == []


def test_ev_schedule_fails_closed_without_target_helper() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "on", "switch.ev_start": "off"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
        },
    )
    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80})))
    assert result.applied is False
    assert result.reason == "ev_target_soc_helper_not_configured"
    assert hass.services.calls == []


def test_ev_schedule_fails_closed_when_helper_service_fails() -> None:
    hass = FailingHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "input_number.ev_target_soc": "50",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "input_number.ev_target_soc",
        },
    )
    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80})))
    assert result.applied is False
    assert result.reason == "ev_target_soc_helper_unsupported"


def test_ev_schedule_preflights_helpers_before_writing_values() -> None:
    hass = PreflightHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "input_number.ev_target_soc": "50",
            "input_datetime.ev_ready_by": "06:00:00",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "input_number.ev_target_soc",
            CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by",
        },
    )

    result = asyncio.run(
        adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80, "ready_by": "07:00"}))
    )

    assert result.applied is False
    assert result.reason == "ev_ready_by_helper_unsupported"
    assert hass.states.values["input_number.ev_target_soc"] == "50"
    assert hass.services.calls == []


def test_ev_restore_uses_saved_switch_state() -> None:
    hass = FakeHass({"switch.ev_start": "on"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
        },
    )
    result = asyncio.run(adapter.async_restore({CONF_EV_SMART_CHARGING_START: "off"}))
    assert result.applied is True
    assert result.reason == "ev_saved_state_restored"
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.ev_start"}),
    ]


def test_ev_execute_rejects_unsupported_action_kind() -> None:
    hass = FakeHass({})
    adapter = EVSmartChargingAdapter(hass, {})

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_HVAC)))

    assert result.applied is False
    assert result.reason == "unsupported_ev_action"


def test_ev_restore_without_saved_state_stops_as_fallback() -> None:
    hass = FakeHass({"switch.ev_stop": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_SMART_CHARGING_STOP: "switch.ev_stop"})

    result = asyncio.run(adapter.async_restore())

    assert result.applied is True
    assert result.reason == "switch_turn_off_called"
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.ev_stop"})]


def test_ev_restore_reports_unrestorable_saved_state() -> None:
    hass = FakeHass({"sensor.ev": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_SMART_CHARGING_START: "sensor.ev"})

    result = asyncio.run(adapter.async_restore({CONF_EV_SMART_CHARGING_START: "on"}))

    assert result.applied is False
    assert result.reason == "ev_saved_state_not_restorable"
    assert hass.services.calls == []


def test_ev_start_requires_configured_start_control() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CONNECTED: "binary_sensor.ev_connected"})

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_start_control_not_configured"


def test_ev_stop_requires_configured_stop_control() -> None:
    hass = FakeHass({})
    adapter = EVSmartChargingAdapter(hass, {})

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_STOP)))

    assert result.applied is False
    assert result.reason == "ev_stop_control_not_configured"


def test_ev_schedule_requires_ready_by_helper() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "on", "switch.ev_start": "off"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"ready_by": "07:00"})))

    assert result.applied is False
    assert result.reason == "ev_ready_by_helper_not_configured"


def test_ev_schedule_rejects_unsupported_target_helper_domain() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "on", "switch.ev_start": "off", "sensor.target": "50"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.target",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80})))

    assert result.applied is False
    assert result.reason == "ev_target_soc_helper_unsupported"


def test_ev_controls_fail_closed_for_unavailable_and_unsupported_domains() -> None:
    missing = asyncio.run(
        EVSmartChargingAdapter(FakeHass({}), {CONF_EV_SMART_CHARGING_START: "switch.ev_start"}).async_execute(
            _action(ActionKind.EV_START)
        )
    )
    unsupported = asyncio.run(
        EVSmartChargingAdapter(FakeHass({"sensor.ev_start": "off"}), {CONF_EV_SMART_CHARGING_START: "sensor.ev_start"}).async_execute(
            _action(ActionKind.EV_START)
        )
    )

    assert missing.reason == "ev_control_unavailable"
    assert unsupported.reason == "ev_control_domain_unsupported"


def test_ev_control_service_errors_fail_closed() -> None:
    button = asyncio.run(
        EVSmartChargingAdapter(FailingHass({"button.ev_start": "off"}), {CONF_EV_SMART_CHARGING_START: "button.ev_start"}).async_execute(
            _action(ActionKind.EV_START)
        )
    )
    switch = asyncio.run(
        EVSmartChargingAdapter(FailingHass({"switch.ev_start": "off"}), {CONF_EV_SMART_CHARGING_START: "switch.ev_start"}).async_execute(
            _action(ActionKind.EV_START)
        )
    )

    assert button.reason == "ev_control_service_failed"
    assert switch.reason == "ev_control_service_failed"


def test_ev_schedule_writes_all_supported_helper_domains() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "input_datetime.ev_ready_by": "06:00:00",
            "time.ev_ready_by": "06:00",
        }
    )
    datetime_adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by",
        },
    )
    time_adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_READY_BY: "time.ev_ready_by",
        },
    )

    assert asyncio.run(datetime_adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"ready_by": "07:00"}))).applied
    hass.states.values["switch.ev_start"] = "off"
    assert asyncio.run(time_adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"ready_by": "08:00"}))).applied

    assert ("input_datetime", "set_datetime", {"entity_id": "input_datetime.ev_ready_by", "time": "07:00"}) in hass.services.calls
    assert ("time", "set_value", {"entity_id": "time.ev_ready_by", "value": "08:00"}) in hass.services.calls


def test_ev_value_match_helpers_handle_invalid_values() -> None:
    adapter = EVSmartChargingAdapter(
        FakeHass(
            {
                "input_number.bad": "not-number",
                "input_datetime.bad": "not-time",
                "time.bad": "also-bad",
                "input_text.note": "hello",
                "select.unsupported": "hello",
            }
        ),
        {},
    )

    assert adapter._entity_value_matches("input_number.bad", 10) is False
    assert adapter._entity_value_matches("input_datetime.bad", "07:00") is False
    assert adapter._entity_value_matches("time.bad", "07:00") is False
    assert adapter._entity_value_matches("input_text.note", "hello") is True
    assert adapter._entity_value_matches("select.unsupported", "hello") is False
    assert adapter._can_set_entity_value(None) is False
