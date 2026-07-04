"""Tests for Enphase profile adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import (
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
)
from custom_components.ha_energy_planner.enphase_adapter import EnphaseProfileAdapter, _profile_control_service
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str


class FakeStates:
    """Minimal HA state registry."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeServices:
    """Minimal HA service bus."""

    def __init__(self, states: FakeStates, *, confirm_change: bool = True, fail: bool = False) -> None:
        self.states = states
        self.confirm_change = confirm_change
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        if self.fail:
            raise RuntimeError("service failed")
        if self.confirm_change and "option" in data:
            self.states.values[data["entity_id"]] = str(data["option"])


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str], *, confirm_change: bool = True, fail: bool = False) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states, confirm_change=confirm_change, fail=fail)


def _action(kind: ActionKind, desired_state: dict[str, Any] | None = None) -> PlanAction:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return PlanAction(
        action_id=kind,
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.ENPHASE,
        kind=kind,
        desired_state=desired_state or {},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=1.0,
        confidence=1.0,
        requires_haeo_plan_id=None,
    )


def _entry_data() -> dict[str, str]:
    return {
        CONF_ENPHASE_PROFILE: "select.enphase_profile",
        CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
    }


def test_enphase_profile_change_requires_observed_confirmation() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"})
    adapter = EnphaseProfileAdapter(hass, _entry_data())
    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))
    assert result.applied is True
    assert result.reason == "enphase_profile_applied"
    assert result.saved_profile == "AI Optimisation"
    assert result.changed_profile_at is True
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_profile_change_honors_legacy_configured_service() -> None:
    hass = FakeHass({"input_select.enphase_profile": "AI Optimisation"})
    adapter = EnphaseProfileAdapter(
        hass,
        {
            CONF_ENPHASE_PROFILE: "input_select.enphase_profile",
            CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "input_select.select_option",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is True
    assert hass.services.calls == [
        ("input_select", "select_option", {"entity_id": "input_select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_profile_change_infers_input_select_service() -> None:
    hass = FakeHass({"input_select.enphase_profile": "AI Optimisation"})
    adapter = EnphaseProfileAdapter(
        hass,
        {
            CONF_ENPHASE_PROFILE: "input_select.enphase_profile",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is True
    assert hass.services.calls == [
        ("input_select", "select_option", {"entity_id": "input_select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_profile_change_fails_when_not_confirmed() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"}, confirm_change=False)
    adapter = EnphaseProfileAdapter(hass, _entry_data())
    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))
    assert result.applied is False
    assert result.reason == "enphase_profile_not_confirmed"
    assert result.post_state[CONF_ENPHASE_PROFILE] == "AI Optimisation"


def test_enphase_profile_change_fails_closed_when_service_fails() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"}, fail=True)
    adapter = EnphaseProfileAdapter(hass, _entry_data())

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is False
    assert result.reason == "enphase_profile_service_failed"
    assert result.post_state[CONF_ENPHASE_PROFILE] == "AI Optimisation"
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_restore_ai_uses_configured_ai_profile() -> None:
    hass = FakeHass({"select.enphase_profile": "Full Backup"})
    adapter = EnphaseProfileAdapter(hass, _entry_data())
    result = asyncio.run(adapter.async_execute(_action(ActionKind.RESTORE_AI)))
    assert result.applied is True
    assert result.reason == "enphase_profile_applied"
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "AI Optimisation"}),
    ]


def test_enphase_rejects_missing_profile_and_unsupported_action() -> None:
    adapter = EnphaseProfileAdapter(FakeHass({"select.enphase_profile": "AI Optimisation"}), _entry_data())
    missing = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE)))
    unsupported = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert missing.reason == "enphase_profile_missing"
    assert unsupported.reason == "unsupported_enphase_action"


def test_enphase_restore_requires_configured_ai_profile() -> None:
    adapter = EnphaseProfileAdapter(
        FakeHass({"select.enphase_profile": "Full Backup"}),
        {CONF_ENPHASE_PROFILE: "select.enphase_profile"},
    )

    result = asyncio.run(adapter.async_restore_ai())

    assert result.applied is False
    assert result.reason == "enphase_ai_profile_not_configured"


def test_enphase_profile_requires_available_entity_and_control_service() -> None:
    unavailable = asyncio.run(
        EnphaseProfileAdapter(
            FakeHass({"select.enphase_profile": "unavailable"}),
            _entry_data(),
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )
    missing_control = asyncio.run(
        EnphaseProfileAdapter(
            FakeHass({"sensor.enphase_profile": "AI Optimisation"}),
            {CONF_ENPHASE_PROFILE: "sensor.enphase_profile", CONF_ENPHASE_AI_PROFILE: "AI Optimisation"},
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )
    invalid_control = asyncio.run(
        EnphaseProfileAdapter(
            FakeHass({"select.enphase_profile": "AI Optimisation"}),
            {
                CONF_ENPHASE_PROFILE: "select.enphase_profile",
                CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
                CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "select_option",
            },
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )

    assert unavailable.reason == "enphase_profile_entity_unavailable"
    assert missing_control.reason == "enphase_profile_control_not_configured"
    assert invalid_control.reason == "enphase_profile_control_invalid"


def test_enphase_profile_change_skips_when_already_selected_and_helper_fallbacks() -> None:
    adapter = EnphaseProfileAdapter(FakeHass({"select.enphase_profile": "AI Optimisation"}), _entry_data())

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "AI Optimisation"})))

    assert result.applied is True
    assert result.reason == "already_in_desired_profile"
    assert result.changed_profile_at is False
    assert _profile_control_service({}, None) is None
    assert _profile_control_service({}, "sensor.enphase") is None
