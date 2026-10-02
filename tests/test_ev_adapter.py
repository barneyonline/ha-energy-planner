"""Tests for EV Smart Charging adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.ha_energy_planner import ev_adapter as ev_adapter_module
from custom_components.ha_energy_planner.const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
)
from custom_components.ha_energy_planner.ev_adapter import EVSmartChargingAdapter
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str
    attributes: dict[str, Any] | None = None


class FakeStates:
    """Minimal HA states registry."""

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

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        entity_id = data["entity_id"]
        if service in {"turn_on", "press"}:
            self.states.values[entity_id] = "on"
        elif service == "turn_off":
            self.states.values[entity_id] = "off"
        elif "option" in data:
            self.states.values[entity_id] = str(data["option"])
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

    def __init__(self, values: dict[str, str | FakeState]) -> None:
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


class ConfirmingServices(FakeServices):
    """Service bus that also updates charger feedback."""

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        await super().async_call(domain, service, data, blocking)
        if service in {"turn_on", "press"}:
            self.states.values["binary_sensor.ev_charging"] = "on"
        elif service == "turn_off":
            self.states.values["binary_sensor.ev_charging"] = "off"


class ConfirmingHass(FakeHass):
    """HA object whose charging feedback follows control calls."""

    def __init__(self, values: dict[str, str]) -> None:
        self.states = FakeStates(values)
        self.services = ConfirmingServices(self.states)


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
            CONF_EV_CHARGER: "switch.ev_start",

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


def test_set_ready_by_updates_helper_without_starting_charging() -> None:
    hass = FakeHass({"input_datetime.ev_ready_by": "06:00:00", "switch.ev_start": "off"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by",
            CONF_EV_CHARGER: "switch.ev_start",
        },
    )

    result = asyncio.run(adapter.async_set_ready_by("23:45"))

    assert result.applied is True
    assert result.reason == "ev_ready_by_helper_updated"
    assert result.pre_state[CONF_EV_SMART_CHARGING_READY_BY] == "06:00:00"
    assert result.post_state[CONF_EV_SMART_CHARGING_READY_BY] == "23:45"
    assert hass.states.values["switch.ev_start"] == "off"
    assert hass.services.calls == [
        ("input_datetime", "set_datetime", {"entity_id": "input_datetime.ev_ready_by", "time": "23:45"})
    ]


def test_set_ready_by_reports_missing_helper() -> None:
    adapter = EVSmartChargingAdapter(FakeHass({}), {})

    result = asyncio.run(adapter.async_set_ready_by("23:45"))

    assert result.applied is False
    assert result.reason == "ev_ready_by_helper_not_configured"


def test_set_ready_by_reports_unsupported_helper_domain() -> None:
    adapter = EVSmartChargingAdapter(
        FakeHass({"sensor.ready_by": "06:00"}),
        {CONF_EV_SMART_CHARGING_READY_BY: "sensor.ready_by"},
    )

    result = asyncio.run(adapter.async_set_ready_by("23:45"))

    assert result.applied is False
    assert result.reason == "ev_ready_by_helper_unsupported"


def test_set_ready_by_reports_helper_service_failure() -> None:
    adapter = EVSmartChargingAdapter(
        FailingHass({"input_datetime.ready_by": "06:00:00"}),
        {CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ready_by"},
    )

    result = asyncio.run(adapter.async_set_ready_by("23:45"))

    assert result.applied is False
    assert result.reason == "ev_ready_by_helper_unsupported"


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
            CONF_EV_CHARGER: "switch.ev_start",
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


def test_ev_schedule_sets_select_target_soc_and_ready_by_helpers() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "select.ev_target_soc": FakeState("70%", {"options": ["70%", "80%", "90%"]}),
            "input_select.ev_ready_by": FakeState("06:00", {"options": ["06:00", "07:00:00", "08:00"]}),
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "select.ev_target_soc",
            CONF_EV_SMART_CHARGING_READY_BY: "input_select.ev_ready_by",
        },
    )

    result = asyncio.run(
        adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80, "ready_by": "07:00"}))
    )

    assert result.applied is True
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.ev_target_soc", "option": "80%"}),
        ("input_select", "select_option", {"entity_id": "input_select.ev_ready_by", "option": "07:00:00"}),
        ("switch", "turn_on", {"entity_id": "switch.ev_start"}),
    ]


def test_ev_schedule_accepts_matching_read_only_target_soc_sensor() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "sensor.ev_target_soc": FakeState(
                "80",
                {
                    "state_class": "measurement",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "friendly_name": "JCW Aceman E Battery EV Target state of charge",
                },
            ),
            "input_text.ev_ready_by": "07:00",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.ev_target_soc",
            CONF_EV_SMART_CHARGING_READY_BY: "input_text.ev_ready_by",
        },
    )

    result = asyncio.run(
        adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80, "ready_by": "07:00"}))
    )

    assert result.applied is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.ev_start"}),
    ]


def test_ev_schedule_rejects_select_target_soc_without_matching_option() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
            "select.ev_target_soc": FakeState("70%", {"options": ["70%", "90%"]}),
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "select.ev_target_soc",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80})))

    assert result.applied is False
    assert result.reason == "ev_target_soc_helper_unsupported"
    assert hass.services.calls == []


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
            CONF_EV_CHARGER: "switch.ev_start",
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
            CONF_EV_CHARGER: "switch.ev_start",
        },
    )
    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))
    assert result.applied is False
    assert result.reason == "ev_not_connected"
    assert hass.services.calls == []


def test_ev_start_fails_when_configured_connection_state_is_unavailable() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "unavailable",
            "switch.ev_start": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_start",
        },
        connected_override=True,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_connected_state_unavailable"
    assert hass.services.calls == []
    assert adapter._state(None) is None


def test_ev_start_and_stop_require_confirmed_charging_feedback() -> None:
    hass = ConfirmingHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "off",
            "switch.ev_control": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
    )

    started = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))
    stopped = asyncio.run(adapter.async_execute(_action(ActionKind.EV_STOP)))

    assert started.applied is True
    assert started.reason == "ev_charging_confirmed"
    assert stopped.applied is True
    assert stopped.reason == "ev_charging_stopped_confirmed"














def test_ev_stop_disables_switch_when_vehicle_has_suspended_charging() -> None:
    hass = FakeHass(
        {
            "sensor.ev_connector_status": "SUSPENDED_EV",
            "switch.ev_charger": "on",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CHARGING: "sensor.ev_connector_status",
            CONF_EV_CHARGER: "switch.ev_charger",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_set_charging(False))

    assert result.applied is True
    assert result.reason == "ev_charging_stopped_confirmed"
    assert result.command_sent is True
    assert result.safe_state_confirmed is True
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.ev_charger"})]


def test_ev_stop_confirms_stateful_control_without_charging_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("an existing on control should be checked without waiting")

    monkeypatch.setattr(ev_adapter_module.asyncio, "sleep", unexpected_sleep)
    hass = FakeHass({"switch.ev_charger": "on"})
    adapter = EVSmartChargingAdapter(
        hass,
        {CONF_EV_CHARGER: "switch.ev_charger"},
    )

    result = asyncio.run(adapter.async_set_charging(False))

    assert result.applied is True
    assert result.safe_state_confirmed is True
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.ev_charger"})]






def test_ev_confirmation_failure_without_a_new_command_does_not_claim_ownership() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "off",
            "switch.ev_control": "on",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
        confirmation_retries=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.command_sent is False
    assert result.rollback_succeeded is None
    assert hass.services.calls == []


def test_ev_retry_service_failure_still_restores_first_command() -> None:
    class RetryFailServices(FakeServices):
        turn_on_calls = 0

        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            if service == "turn_on":
                self.turn_on_calls += 1
                if self.turn_on_calls == 2:
                    raise RuntimeError("retry failed")
                self.calls.append((domain, service, data))
                return
            await super().async_call(domain, service, data, blocking)

    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "off",
            "switch.ev_control": "off",
        }
    )
    hass.services = RetryFailServices(hass.states)
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
        confirmation_retries=1,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_control_service_failed"
    assert result.rollback_succeeded is True
    assert hass.services.calls[-1] == ("switch", "turn_off", {"entity_id": "switch.ev_control"})


def test_ev_switch_confirmation_timeout_is_not_downgraded_to_no_change() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "off",
            "switch.ev_control": "off",
            "input_boolean.ev_stop": "on",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
        confirmation_retries=1,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_charging_confirmation_timeout"
    assert result.command_sent is True
    assert result.rollback_succeeded is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.ev_control"}),
        ("switch", "turn_off", {"entity_id": "switch.ev_control"}),
    ]


def test_ev_unconfirmed_compensation_retains_rollback_failure_evidence() -> None:
    class StuckControlServices(FakeServices):
        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            self.calls.append((domain, service, data))
            if service == "turn_on":
                self.states.values[data["entity_id"]] = "on"

    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "off",
            "switch.ev_control": "off",
        }
    )
    hass.services = StuckControlServices(hass.states)
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
        confirmation_retries=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.command_sent is True
    assert result.rollback_succeeded is False
    assert hass.states.values["switch.ev_control"] == "on"


def test_ev_keep_on_confirms_stateful_control_without_requiring_active_charging() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "sensor.ev_charging": "fully_charged",
            "switch.ev_control": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START, {"keep_charger_on": True})))

    assert result.applied is True
    assert result.reason == "ev_charger_enabled_for_preconditioning"
    assert hass.services.calls == [("switch", "turn_on", {"entity_id": "switch.ev_control"})]


def test_ev_keep_on_rejects_momentary_start_control() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "button.ev_start": "unknown",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "button.ev_start",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START, {"keep_charger_on": True})))

    assert result.applied is False
    assert result.reason == "ev_keep_on_requires_stateful_control"
    assert hass.services.calls == []


def test_ev_keep_on_prefers_persistent_charger_over_optional_start_button() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "button.ev_start": "unknown",
            "switch.ev_control": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",

            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START, {"keep_charger_on": True})))

    assert result.applied is True
    assert result.reason == "ev_charger_enabled_for_preconditioning"
    assert hass.services.calls == [("switch", "turn_on", {"entity_id": "switch.ev_control"})]


def test_ev_keep_on_rejects_separate_start_switch_without_persistent_control() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_start": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            "ev_charger_start_entity": "switch.ev_start",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START, {"keep_charger_on": True})))

    assert result.applied is False
    assert result.reason == "ev_keep_on_requires_stateful_control"
    assert hass.services.calls == []


def test_ev_confirmation_accepts_matching_feedback_when_control_is_already_set() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "on",
            "switch.ev_control": "on",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is True
    assert result.reason == "already_in_desired_state"
    assert hass.services.calls == []


def test_ev_confirmation_rejects_service_failure_and_unknown_feedback() -> None:
    failed = asyncio.run(
        EVSmartChargingAdapter(
            FailingHass(
                {
                    "binary_sensor.ev_connected": "on",
                    "binary_sensor.ev_charging": "off",
                    "switch.ev_control": "off",
                }
            ),
            {
                CONF_EV_CONNECTED: "binary_sensor.ev_connected",
                CONF_EV_CHARGING: "binary_sensor.ev_charging",
                CONF_EV_CHARGER: "switch.ev_control",
            },
            confirmation_timeout_seconds=0,
        ).async_execute(_action(ActionKind.EV_START))
    )
    unknown_hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "sensor.ev_charging": "warming_up",
            "switch.ev_start": "off",
        }
    )
    unknown = asyncio.run(
        EVSmartChargingAdapter(
            unknown_hass,
            {
                CONF_EV_CONNECTED: "binary_sensor.ev_connected",
                CONF_EV_CHARGING: "sensor.ev_charging",
                CONF_EV_CHARGER: "switch.ev_start",
            },
            confirmation_timeout_seconds=0.002,
            confirmation_poll_seconds=0.001,
        ).async_execute(_action(ActionKind.EV_START))
    )

    assert failed.applied is False
    assert failed.reason == "ev_control_service_failed"
    assert unknown.applied is False
    assert unknown.reason == "ev_charging_confirmation_timeout"


def test_ev_confirmation_fails_when_feedback_is_unavailable() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "binary_sensor.ev_charging": "unavailable",
            "switch.ev_start": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_start",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_charging_confirmation_unavailable"


def test_ev_schedule_fails_closed_without_target_helper() -> None:
    hass = FakeHass({"binary_sensor.ev_connected": "on", "switch.ev_start": "off"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_start",
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
            CONF_EV_CHARGER: "switch.ev_start",
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
            CONF_EV_CHARGER: "switch.ev_start",
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


def test_ev_restore_uses_saved_persistent_switch_state() -> None:
    hass = FakeHass({"switch.ev_control": "on"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CHARGER: "switch.ev_control",
        },
    )
    result = asyncio.run(adapter.async_restore({CONF_EV_CHARGER: "off"}))
    assert result.applied is True
    assert result.reason == "ev_saved_state_restored"
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.ev_control"}),
    ]




def test_ev_restore_deduplicates_new_and_legacy_aliases_for_same_control() -> None:
    hass = FakeHass({"switch.ev_control": "on"})
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CHARGER: "switch.ev_control",
            "ev_smart_charging_entity": "switch.ev_control",
        },
    )

    result = asyncio.run(
        adapter.async_restore(
            {
                CONF_EV_CHARGER: "off",
                "ev_smart_charging_entity": "off",
            }
        )
    )

    assert result.applied is True
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.ev_control"})]


def test_ev_execute_rejects_unsupported_action_kind() -> None:
    hass = FakeHass({})
    adapter = EVSmartChargingAdapter(hass, {})

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_HVAC)))

    assert result.applied is False
    assert result.reason == "unsupported_ev_action"


def test_ev_restore_without_saved_state_stops_as_fallback() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "off",
            "switch.ev_stop": "on",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_stop",
        },
    )

    result = asyncio.run(adapter.async_restore())

    assert result.applied is True
    assert result.reason == "ev_charging_stopped_confirmed"
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.ev_stop"})]


def test_disconnected_ev_with_off_persistent_control_is_already_safely_stopped() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "off",
            "sensor.ev_connector": "AVAILABLE",
            "switch.ev_charger": "off",
            "button.ev_start": "unknown",
            "button.ev_stop": "unknown",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "sensor.ev_connector",
            CONF_EV_CHARGER: "switch.ev_charger",
        },
        confirmation_timeout_seconds=0,
        confirmation_retries=0,
    )

    result = asyncio.run(adapter.async_set_charging(False))

    assert result.applied is True
    assert result.reason == "already_in_desired_state"
    assert result.safe_state_confirmed is True
    assert result.command_sent is False
    assert hass.services.calls == []


def test_inactive_connected_ev_with_off_persistent_control_is_already_safe() -> None:
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "sensor.ev_connector": "SUSPENDED_EV",
            "switch.ev_charger": "off",
        }
    )
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "sensor.ev_connector",
            CONF_EV_CHARGER: "switch.ev_charger",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_set_charging(False))

    assert result.safe_state_confirmed is True
    assert hass.services.calls == []












def test_ev_restore_reports_unrestorable_saved_state() -> None:
    hass = FakeHass({"sensor.ev": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "sensor.ev"})

    result = asyncio.run(adapter.async_restore({CONF_EV_CHARGER: "on"}))

    assert result.applied is False
    assert result.reason == "ev_control_domain_unsupported"
    assert hass.services.calls == []


def test_ev_restore_invalid_persistent_control_falls_back_to_safe_stop() -> None:
    hass = FakeHass({"sensor.ev": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "sensor.ev"})

    result = asyncio.run(adapter.async_restore({CONF_EV_CHARGER: "on"}))

    assert result.applied is False
    assert result.reason == "ev_control_domain_unsupported"
    assert result.rollback_succeeded is False
    assert hass.services.calls == []


def test_ev_native_connected_helper_blocks_manual_start_when_disconnected() -> None:
    hass = FakeHass({"switch.ev_start": "off"})
    adapter = EVSmartChargingAdapter(
        hass,
        {CONF_EV_CHARGER: "switch.ev_start"},
        connected_override=False,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_not_connected"
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
            CONF_EV_CHARGER: "switch.ev_start",
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
            CONF_EV_CHARGER: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.target",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"target_soc_percent": 80})))

    assert result.applied is False
    assert result.reason == "ev_target_soc_helper_unsupported"


def test_ev_controls_fail_closed_for_unavailable_and_unsupported_domains() -> None:
    missing = asyncio.run(
        EVSmartChargingAdapter(FakeHass({}), {CONF_EV_CHARGER: "switch.ev_start"}).async_execute(
            _action(ActionKind.EV_START)
        )
    )
    unsupported = asyncio.run(
        EVSmartChargingAdapter(
            FakeHass({"sensor.ev_start": "off"}), {CONF_EV_CHARGER: "sensor.ev_start"}
        ).async_execute(_action(ActionKind.EV_START))
    )

    assert missing.reason == "ev_control_unavailable"
    assert unsupported.reason == "ev_control_domain_unsupported"


def test_ev_control_service_errors_fail_closed() -> None:
    button = asyncio.run(
        EVSmartChargingAdapter(
            FailingHass({"button.ev_start": "off"}), {CONF_EV_CHARGER: "button.ev_start"}
        ).async_execute(_action(ActionKind.EV_START))
    )
    switch = asyncio.run(
        EVSmartChargingAdapter(
            FailingHass({"switch.ev_start": "off"}), {CONF_EV_CHARGER: "switch.ev_start"}
        ).async_execute(_action(ActionKind.EV_START))
    )

    assert button.reason == "ev_control_domain_unsupported"
    assert button.command_sent is False
    assert button.rollback_succeeded is None
    assert switch.reason == "ev_control_service_failed"
    assert switch.command_sent is True
    assert switch.rollback_succeeded is False


def test_ev_start_service_error_waits_for_delayed_feedback_before_rollback() -> None:
    class AcceptedThenRaisedServices(FakeServices):
        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            self.calls.append((domain, service, data))

            async def delayed_feedback() -> None:
                await asyncio.sleep(0.002)
                self.states.values["sensor.ev_connector_status"] = "CHARGING"

            asyncio.create_task(delayed_feedback())
            raise RuntimeError("provider timed out after accepting command")

    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "sensor.ev_connector_status": "SUSPENDED_EVSE",
            "switch.ev_start": "off",
            "button.ev_stop": "unknown",
        }
    )
    hass.services = AcceptedThenRaisedServices(hass.states)
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "sensor.ev_connector_status",
            CONF_EV_CHARGER: "switch.ev_start",
        },
        confirmation_timeout_seconds=0.02,
        confirmation_poll_seconds=0.001,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is True
    assert result.reason == "ev_charging_confirmed_after_service_error"
    assert result.command_sent is True
    assert result.rollback_succeeded is None
    assert hass.services.calls == [("switch", "turn_on", {"entity_id": "switch.ev_start"})]




def test_ev_stateful_stop_service_error_confirms_control_and_feedback() -> None:
    class AcceptedThenRaisedServices(FakeServices):
        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            await super().async_call(domain, service, data, blocking)
            self.states.values["binary_sensor.ev_charging"] = "off"
            raise RuntimeError("provider timed out after accepting stop")

    hass = FakeHass(
        {
            "binary_sensor.ev_charging": "on",
            "switch.ev_charger": "on",
        }
    )
    hass.services = AcceptedThenRaisedServices(hass.states)
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_CHARGER: "switch.ev_charger",
        },
        confirmation_timeout_seconds=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_STOP)))

    assert result.applied is True
    assert result.reason == "ev_charging_stopped_confirmed_after_service_error"
    assert result.safe_state_confirmed is True
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.ev_charger"})]


def test_ev_control_service_exception_restores_a_mutated_switch() -> None:
    class ApplyThenRaiseServices(FakeServices):
        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            await super().async_call(domain, service, data, blocking)
            if len(self.calls) == 1:
                raise RuntimeError("service failed after starting charger")

    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "switch.ev_control": "off",
        }
    )
    hass.services = ApplyThenRaiseServices(hass.states)
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_control",
        },
        confirmation_timeout_seconds=0,
        confirmation_retries=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert result.applied is False
    assert result.reason == "ev_control_service_failed"
    assert result.command_sent is True
    assert result.rollback_succeeded is True
    assert result.safe_state_confirmed is True
    assert hass.states.values["switch.ev_control"] == "off"
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.ev_control"}),
        ("switch", "turn_off", {"entity_id": "switch.ev_control"}),
    ]


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
            CONF_EV_CHARGER: "switch.ev_start",
            CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by",
        },
    )
    time_adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGER: "switch.ev_start",
            CONF_EV_SMART_CHARGING_READY_BY: "time.ev_ready_by",
        },
    )

    assert asyncio.run(datetime_adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"ready_by": "07:00"}))).applied
    hass.states.values["switch.ev_start"] = "off"
    assert asyncio.run(time_adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"ready_by": "08:00"}))).applied

    assert (
        "input_datetime",
        "set_datetime",
        {"entity_id": "input_datetime.ev_ready_by", "time": "07:00"},
    ) in hass.services.calls
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
                "select.no_match": FakeState("hello", {"options": ["hello", "world"]}),
                "unknown.entity": "on",
            }
        ),
        {},
    )

    assert adapter._entity_value_matches("input_number.bad", 10) is False
    assert adapter._entity_value_matches("input_datetime.bad", "07:00") is False
    assert adapter._entity_value_matches("time.bad", "07:00") is False
    assert adapter._entity_value_matches("input_text.note", "hello") is True
    assert adapter._entity_value_matches("select.unsupported", "hello") is True
    assert adapter._entity_value_matches("unknown.entity", "on") is False
    assert adapter._select_option_for_value("select.unsupported", "hello") == "hello"
    assert adapter._select_option_for_value("select.missing", "hello") is None
    assert adapter._select_option_for_value("select.no_match", "missing") is None
    assert adapter._can_set_entity_value(None) is False


def test_native_schedule_and_manual_commands_control_charger_directly() -> None:
    hass = FakeHass({"switch.charger": "off"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "switch.charger"})

    start = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"charging_required_now": True})))
    stop = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"charging_required_now": False})))
    manual_start = asyncio.run(adapter.async_set_charging(True))
    manual_stop = asyncio.run(adapter.async_set_charging(False))

    assert start.applied is True
    assert stop.applied is True
    assert manual_start.applied is True
    assert manual_stop.applied is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.charger"}),
        ("switch", "turn_off", {"entity_id": "switch.charger"}),
        ("switch", "turn_on", {"entity_id": "switch.charger"}),
        ("switch", "turn_off", {"entity_id": "switch.charger"}),
    ]


def test_ev_dispatch_timeout_accepts_observed_feedback_without_retry(monkeypatch: Any) -> None:
    from custom_components.ha_energy_planner import adapter_helpers

    monkeypatch.setattr(adapter_helpers, "DEVICE_SERVICE_TIMEOUT_SECONDS", 0.005)
    hass = FakeHass(
        {
            "binary_sensor.ev_connected": "on",
            "sensor.feedback": "SUSPENDED_EVSE",
            "switch.ev": "off",
            "button.stop": "unknown",
        }
    )
    calls: list[str] = []

    async def accepted(domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        calls.append(data["entity_id"])
        hass.states.values["sensor.feedback"] = "CHARGING"
        await asyncio.Event().wait()

    hass.services.async_call = accepted
    adapter = EVSmartChargingAdapter(
        hass,
        {
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_CHARGING: "sensor.feedback",
            CONF_EV_CHARGER: "switch.ev",
        },
        confirmation_timeout_seconds=0.02,
        confirmation_poll_seconds=0.001,
    )
    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))
    assert result.applied and result.command_sent
    assert result.reason == "ev_charging_confirmed_after_service_error"
    assert calls == ["switch.ev"]


@pytest.mark.parametrize("domain", ["button", "input_button"])
def test_ev_buttons_never_dispatch_or_claim_command_sent(domain: str) -> None:
    entity_id = f"{domain}.start"
    hass = FakeHass({entity_id: "unavailable"})
    adapter = EVSmartChargingAdapter(hass, {})
    result = asyncio.run(adapter._async_call_control(entity_id, turn_on=True))
    assert not result.applied
    assert not result.command_sent
    assert result.reason == "ev_control_unavailable"
    assert hass.services.calls == []
    hass.states.values[entity_id] = "unknown"
    recovered = asyncio.run(adapter._async_call_control(entity_id, turn_on=True))
    assert not recovered.applied
    assert hass.services.calls == []


@pytest.mark.parametrize("saved", [{CONF_EV_CHARGER: "unknown"}, {"ev_charger_start_entity": "off"}])
def test_ev_restore_invalid_snapshot_safely_stops_the_configured_switch(saved) -> None:
    hass = FakeHass({"switch.charger": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "switch.charger"})
    result = asyncio.run(adapter.async_restore(saved))
    assert result.applied and result.safe_state_confirmed
    assert result.reason == "ev_saved_state_safe_stop"
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.charger"})]


def test_ev_restore_snapshot_cannot_touch_conflicting_legacy_alias() -> None:
    hass = FakeHass({"switch.current": "on", "switch.legacy": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "switch.current",
                                          "ev_smart_charging_entity": "switch.legacy"})
    result = asyncio.run(adapter.async_restore({"ev_smart_charging_entity": "off"}))
    assert result.applied and result.safe_state_confirmed
    assert hass.states.values["switch.legacy"] == "on"
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.current"})]


def test_ev_recovery_rejects_non_stateful_snapshot_targets() -> None:
    hass = FakeHass({"sensor.invalid": "on"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "sensor.invalid"})
    assert asyncio.run(adapter._async_restore_control_snapshot({CONF_EV_CHARGER: "off"})) == (False, False)
    assert asyncio.run(adapter._async_control_proves_safe("sensor.invalid")) is False
    assert asyncio.run(adapter._async_control_proves_safe(None)) is False
    assert not hass.services.calls


def test_ev_failed_stop_retains_uncertainty_if_mapping_disappears_during_dispatch() -> None:
    hass = FakeHass({"switch.charger": "on"})
    data = {CONF_EV_CHARGER: "switch.charger"}
    async def lost_control(domain, service, service_data, blocking=False):
        data.clear()
        raise RuntimeError("configuration changed during dispatch")
    hass.services.async_call = lost_control
    adapter = EVSmartChargingAdapter(hass, data, confirmation_timeout_seconds=0)
    result = asyncio.run(adapter.async_set_charging(False))
    assert not result.applied and result.command_sent
    assert result.rollback_succeeded is False
    assert hass.states.values["switch.charger"] == "on"


def test_ev_legacy_ready_by_only_schedule_still_uses_the_charger_switch() -> None:
    hass = FakeHass({"switch.charger": "off", "input_datetime.ready_by": "06:00:00"})
    adapter = EVSmartChargingAdapter(hass, {CONF_EV_CHARGER: "switch.charger",
        CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ready_by"})
    result = asyncio.run(adapter.async_execute(_action(ActionKind.EV_SCHEDULE, {"ready_by": "07:00"})))
    assert result.applied
    assert hass.services.calls[-1] == ("switch", "turn_on", {"entity_id": "switch.charger"})
    assert asyncio.run(adapter._async_set_entity_value("sensor.unsupported", 80)) is False
