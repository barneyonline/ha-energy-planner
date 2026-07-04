"""Tests for Daikin HVAC adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import CONF_CLIMATE_AUTOMATIONS, CONF_DAIKIN_CLIMATE
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
        self.fail_services: set[tuple[str, str]] = set()

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        if (domain, service) in self.fail_services:
            raise RuntimeError("service failed")
        entity_id = data["entity_id"]
        if service == "turn_on":
            self.states.values[entity_id] = "on"
        elif service == "turn_off":
            self.states.values[entity_id] = "off"
        elif service == "set_hvac_mode":
            self.states.values[entity_id] = str(data["hvac_mode"])
        elif service == "set_temperature" and "temperature" in data:
            self.states.values[f"{entity_id}:temperature"] = str(data["temperature"])


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
        requires_haeo_plan_id=None,
    )


def test_hvac_action_disables_automation_then_controls_climate() -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
    adapter = DaikinHVACAdapter(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    )
    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 20})))
    assert result.applied is True
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate"}),
        ("climate", "set_hvac_mode", {"entity_id": "climate.daikin", "hvac_mode": "heat"}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 20}),
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
    assert result.reason == "hvac_automation_state_restored"
    assert hass.services.calls == [
        ("automation", "turn_on", {"entity_id": "automation.climate"}),
    ]


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
    assert hass.services.calls == []


def test_hvac_action_skips_when_state_already_matches() -> None:
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
    assert result.reason == "already_in_desired_hvac_state"
    assert result.saved_automation_states == {}
    assert hass.services.calls == []


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
        ("automation", "turn_off", {"entity_id": "automation.climate"}),
    ]


def test_hvac_suppression_skips_when_no_automation_enabled() -> None:
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
    assert result.reason == "already_in_desired_hvac_state"
    assert result.saved_automation_states == {}
    assert hass.services.calls == []


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
    assert result.saved_automation_states == {"automation.climate": "on"}
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
    assert hass.states.values["climate.daikin"] == "heat"


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
    assert result.reason == "hvac_automation_restore_failed"
    assert hass.states.values["automation.climate"] == "off"


def test_hvac_action_rejects_unsupported_kind_and_empty_desired_state() -> None:
    hass = FakeHass({"climate.daikin": "heat"})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})
    unsupported = _action({"hvac_mode": "heat"})
    unsupported.kind = ActionKind.EV_START

    unsupported_result = asyncio.run(adapter.async_execute(unsupported))
    empty_result = asyncio.run(adapter.async_execute(_action({})))

    assert unsupported_result.reason == "unsupported_hvac_action"
    assert empty_result.reason == "hvac_desired_state_empty"


def test_hvac_restore_turns_off_saved_automation_and_handles_empty_state() -> None:
    hass = FakeHass({"automation.climate": "on"})
    adapter = DaikinHVACAdapter(hass, {CONF_CLIMATE_AUTOMATIONS: "automation.climate"})

    restored = asyncio.run(adapter.async_restore({"automation.climate": "off"}))
    empty = asyncio.run(adapter.async_restore({}))

    assert restored.applied is True
    assert restored.reason == "hvac_automation_state_restored"
    assert empty.applied is False
    assert empty.reason == "no_hvac_automation_state_saved"
    assert ("automation", "turn_off", {"entity_id": "automation.climate"}) in hass.services.calls


def test_hvac_action_turns_off_climate_and_sets_temperature_range() -> None:
    off_hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
    off_adapter = DaikinHVACAdapter(
        off_hass,
        {CONF_DAIKIN_CLIMATE: "climate.daikin", CONF_CLIMATE_AUTOMATIONS: "automation.climate"},
    )
    range_hass = FakeHass({"climate.daikin": "heat", "automation.climate": "off"})
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
