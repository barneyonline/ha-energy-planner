"""Tests for Daikin HVAC adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import (
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
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
        self.fail_services: set[tuple[str, str]] = set()
        self.noop_entities: set[str] = set()

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        if (domain, service) in self.fail_services:
            raise RuntimeError("service failed")
        entity_id = data["entity_id"]
        if entity_id in self.noop_entities:
            return
        if service == "turn_on":
            self.states.values[entity_id] = "on"
        elif service == "turn_off":
            self.states.values[entity_id] = "off"
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
    assert adapter.takeover_snapshot() == ({"automation.climate": "on"}, {})
    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "heat", "target_temperature": 20})))
    assert result.applied is True
    assert result.saved_automation_states == {"automation.climate": "on"}
    assert hass.services.calls == [
        ("automation", "turn_off", {"entity_id": "automation.climate"}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 20}),
    ]


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
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 19})
    ]
    assert mode_result.applied is True
    assert mode_hass.services.calls == [
        ("climate", "set_hvac_mode", {"entity_id": "climate.daikin", "hvac_mode": "cool"})
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
    assert hass.services.calls == [("automation", "turn_off", {"entity_id": "automation.climate"})]


def test_hvac_action_fails_when_automation_does_not_confirm_disabled(monkeypatch: object) -> None:
    hass = FakeHass({"climate.daikin": "heat", "automation.climate": "on"})
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
        ("automation", "turn_off", {"entity_id": "automation.climate"}),
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

    async def delayed_mode_change(
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
            data["hvac_mode"],
        )

    hass.services.async_call = delayed_mode_change

    result = asyncio.run(adapter.async_execute(_action({"hvac_mode": "cool"})))

    assert result.applied is True
    assert result.reason == "hvac_action_applied"


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
    assert hass.states.values["automation.climate"] == "on"


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
    assert result.reason == "hvac_automation_rollback_failed"
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
    assert hass.states.values["automation.climate"] == "on"


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


def test_hvac_zone_state_mismatch_fails_acquisition_and_release_closed() -> None:
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


def test_hvac_automation_state_mismatch_is_retained_for_release_retry() -> None:
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


def test_hvac_action_rejects_unsupported_kind_and_empty_desired_state() -> None:
    hass = FakeHass({"climate.daikin": "heat"})
    adapter = DaikinHVACAdapter(hass, {CONF_DAIKIN_CLIMATE: "climate.daikin"})
    unsupported = _action({"hvac_mode": "heat"})
    unsupported.kind = ActionKind.EV_START

    unsupported_result = asyncio.run(adapter.async_execute(unsupported))
    empty_result = asyncio.run(adapter.async_execute(_action({})))

    assert unsupported_result.reason == "unsupported_hvac_action"
    assert empty_result.reason == "hvac_desired_state_empty"


def test_hvac_release_enables_configured_automation_even_if_saved_off() -> None:
    hass = FakeHass({"automation.climate": "on"})
    adapter = DaikinHVACAdapter(hass, {CONF_CLIMATE_AUTOMATIONS: "automation.climate"})

    restored = asyncio.run(adapter.async_restore({"automation.climate": "off"}))
    empty = asyncio.run(adapter.async_restore({}))

    assert restored.applied is True
    assert restored.reason == "hvac_control_released"
    assert empty.applied is True
    assert empty.reason == "hvac_control_released"
    assert ("automation", "turn_on", {"entity_id": "automation.climate"}) not in hass.services.calls


def test_hvac_takeover_turns_on_climate_and_zones_then_release_restores_zones() -> None:
    hass = FakeHass(
        {
            "climate.daikin": "off",
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
    assert hass.services.calls[:5] == [
        ("automation", "turn_off", {"entity_id": "automation.climate"}),
        ("switch", "turn_on", {"entity_id": "switch.living"}),
        ("climate", "turn_on", {"entity_id": "climate.daikin"}),
        ("climate", "set_hvac_mode", {"entity_id": "climate.daikin", "hvac_mode": "heat"}),
        ("climate", "set_temperature", {"entity_id": "climate.daikin", "temperature": 23}),
    ]
    assert released.reason == "hvac_control_released"
    assert hass.states.values["switch.living"] == "off"
    assert hass.states.values["automation.climate"] == "on"


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
