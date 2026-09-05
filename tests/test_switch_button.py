"""Tests for switch and button entity behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_energy_planner import button as button_module
from custom_components.ha_energy_planner import notifications as notifications_module
from custom_components.ha_energy_planner import switch as switch_module
from custom_components.ha_energy_planner.button import BUTTONS, PlannerButton
from custom_components.ha_energy_planner.const import (
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
    CONF_PLANNER_ENABLED,
)
from custom_components.ha_energy_planner.models import OutcomeResult
from custom_components.ha_energy_planner.switch import SWITCHES, PlannerSwitch


class FakeConfigEntries:
    """Capture option updates."""

    def __init__(self) -> None:
        self.updated: list[tuple[object, dict[str, object]]] = []

    def async_update_entry(self, entry: object, *, options: dict[str, object]) -> None:
        self.updated.append((entry, options))
        entry.options = options


class FakeServices:
    """Capture service calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, object],
        *,
        blocking: bool = False,
    ) -> None:
        self.calls.append((domain, service, service_data, blocking))


class FakeCoordinator:
    """Minimal coordinator for entity methods."""

    def __init__(self, options: dict[str, object] | None = None) -> None:
        self.entry = SimpleNamespace(entry_id="entry-1", title="Garage EV", options=options or {})
        self.hass = SimpleNamespace(config_entries=FakeConfigEntries(), services=FakeServices())
        self.replan_count = 0
        self.restore_calls: list[tuple[str, bool]] = []
        self.arm_calls: list[str] = []
        self.disarm_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.ai_advice_requests = 0
        self.entry_data: dict[str, object] = {}
        self.active_control_calls: list[bool] = []
        self.device_control_calls: list[tuple[str, bool]] = []
        self.active_control_value = False
        self.automatic_control_requested = False
        self.last_update_success = True
        self.restore_result = SimpleNamespace(result=OutcomeResult.RESTORED, reason="restored")
        self.last_control_mode = (
            bool(self.options[CONF_PLANNER_ENABLED]),
            bool(self.options[CONF_DRY_RUN]),
        )

    @property
    def options(self) -> dict[str, object]:
        return {
            CONF_PLANNER_ENABLED: False,
            CONF_DRY_RUN: True,
            **dict(self.entry.options),
        }

    @property
    def active_control(self) -> bool:
        return self.active_control_value

    async def async_set_active_control(self, enabled: bool) -> None:
        self.active_control_calls.append(enabled)
        self.automatic_control_requested = enabled
        self.active_control_value = enabled

    async def async_set_device_control(self, option_key: str, enabled: bool) -> None:
        self.device_control_calls.append((option_key, enabled))
        options = self.options
        options[option_key] = enabled
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    async def async_request_replan(self) -> None:
        self.replan_count += 1

    async def async_request_ai_advice(self) -> None:
        self.ai_advice_requests += 1

    async def async_handle_options_update(self) -> None:
        previous_enabled, previous_dry_run = self.last_control_mode
        current_mode = (
            bool(self.options[CONF_PLANNER_ENABLED]),
            bool(self.options[CONF_DRY_RUN]),
        )
        self.last_control_mode = current_mode
        if previous_enabled and not current_mode[0]:
            await self.async_restore_safe_state("planner_disabled", refresh=False)
        elif not previous_dry_run and current_mode[1]:
            await self.async_restore_safe_state("dry_run_enabled", refresh=False)
        await self.async_request_replan()

    async def async_restore_safe_state(self, reason: str, *, refresh: bool = True) -> Any:
        self.restore_calls.append((reason, refresh))
        return self.restore_result

    async def async_arm_production_control(self, reason: str) -> None:
        self.arm_calls.append(reason)

    async def async_operator_arm_production_control(self, reason: str) -> None:
        self.arm_calls.append(reason)

    async def async_disarm_production_control(self, reason: str) -> None:
        self.disarm_calls.append(reason)

    async def async_operator_disarm_production_control(self, reason: str) -> None:
        self.disarm_calls.append(reason)

    async def async_resume_control(self, reason: str) -> None:
        self.resume_calls.append(reason)


def test_automatic_control_switch_uses_combined_coordinator_path() -> None:
    coordinator = FakeCoordinator()
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in SWITCHES if description.key == "active_control"),
        write_count=0,
    )
    switch.async_write_ha_state = lambda: setattr(switch, "write_count", switch.write_count + 1)

    assert PlannerSwitch.is_on.fget(switch) is False
    asyncio.run(PlannerSwitch.async_turn_on(switch))
    assert PlannerSwitch.is_on.fget(switch) is True
    asyncio.run(PlannerSwitch.async_turn_off(switch))

    assert coordinator.active_control_calls == [True, False]
    assert switch.write_count == 2


def test_automatic_control_switch_retains_requested_intent_while_disarmed() -> None:
    coordinator = FakeCoordinator()
    coordinator.automatic_control_requested = True
    coordinator.active_control_value = False
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in SWITCHES if description.key == "active_control"),
    )

    assert PlannerSwitch.is_on.fget(switch) is True


def test_switches_expose_one_master_and_three_device_controls() -> None:
    descriptions = {description.key: description for description in SWITCHES}
    keys = set(descriptions)

    assert keys == {"active_control", "climate_control", "ev_control", "enphase_control"}
    assert all(
        descriptions[key].entity_category is None
        for key in ("active_control", "climate_control", "ev_control", "enphase_control")
    )
    assert keys.isdisjoint(
        {
            "enabled",
            "dry_run",
            "ai_enabled",
            "ev_control_enabled",
            "climate_control_enabled",
            "enphase_control_enabled",
        }
    )


@pytest.mark.parametrize(
    ("switch_key", "option_key"),
    [
        ("climate_control", CONF_CLIMATE_CONTROL_ENABLED),
        ("ev_control", CONF_EV_CONTROL_ENABLED),
        ("enphase_control", CONF_ENPHASE_CONTROL_ENABLED),
    ],
)
def test_device_control_switches_use_guarded_coordinator_path(switch_key: str, option_key: str) -> None:
    coordinator = FakeCoordinator({option_key: False})
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in SWITCHES if description.key == switch_key),
        write_count=0,
    )
    switch._async_set_option = lambda value: PlannerSwitch._async_set_option(switch, value)
    switch.async_write_ha_state = lambda: setattr(switch, "write_count", switch.write_count + 1)

    assert PlannerSwitch.is_on.fget(switch) is False
    asyncio.run(PlannerSwitch.async_turn_on(switch))
    assert PlannerSwitch.is_on.fget(switch) is True
    asyncio.run(PlannerSwitch.async_turn_off(switch))

    assert coordinator.device_control_calls == [(option_key, True), (option_key, False)]
    assert coordinator.entry.options[option_key] is False
    assert switch.write_count == 2


def test_switch_setup_and_constructor(monkeypatch: object) -> None:
    coordinator = FakeCoordinator({CONF_DRY_RUN: True})
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list[object] = []

    asyncio.run(switch_module.async_setup_entry(SimpleNamespace(), entry, added.extend))
    switch = PlannerSwitch(
        coordinator,
        next(description for description in SWITCHES if description.key == "active_control"),
    )

    assert len(added) == len(SWITCHES)
    assert switch.is_on is False


def test_button_setup_and_constructor(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list[object] = []

    asyncio.run(button_module.async_setup_entry(None, entry, added.extend))
    button = PlannerButton(coordinator, next(description for description in BUTTONS if description.key == "replan"))

    assert len(added) == len(BUTTONS)
    assert button.unique_id == "entry-1_replan"


def test_replan_button_requests_replan() -> None:
    coordinator = FakeCoordinator()
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "replan"),
    )

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.replan_count == 1
    assert coordinator.restore_calls == []


def test_request_ai_advice_button_uses_current_planner_advisory() -> None:
    coordinator = FakeCoordinator()
    description = next(description for description in BUTTONS if description.key == "request_ai_advice")
    button = SimpleNamespace(coordinator=coordinator, entity_description=description)

    assert description.available_fn(coordinator) is False
    coordinator.entry_data[CONF_AI_TASK_ENTITY] = "ai_task.local"
    coordinator.hass.states = SimpleNamespace(
        get=lambda entity_id: SimpleNamespace(state="unknown") if entity_id == "ai_task.local" else None
    )
    assert description.available_fn(coordinator) is True
    entity = PlannerButton(coordinator, description)
    assert entity.available is True

    coordinator.hass.states = SimpleNamespace(
        get=lambda entity_id: SimpleNamespace(state="unavailable") if entity_id == "ai_task.local" else None
    )
    assert description.available_fn(coordinator) is False

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.ai_advice_requests == 1


def test_restore_button_restores_safe_state() -> None:
    coordinator = FakeCoordinator()
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "restore_safe_state"),
    )

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.restore_calls == [("button_pressed", True)]
    assert coordinator.replan_count == 0


def test_removed_manual_ev_buttons_are_not_exposed() -> None:
    descriptions = {description.key: description for description in BUTTONS}

    assert "ev_start_charging" not in descriptions
    assert "ev_stop_charging" not in descriptions
    assert "pause_control_1h" not in descriptions
    assert "pause_control_4h" not in descriptions
    assert descriptions["request_ai_advice"].icon is None


def test_restore_button_raises_translated_error_when_restore_is_incomplete() -> None:
    coordinator = FakeCoordinator()
    coordinator.restore_result = SimpleNamespace(result=OutcomeResult.FAILED, reason="hvac_restore_failed")
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "restore_safe_state"),
    )

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(PlannerButton.async_press(button))

    assert error.value.translation_domain == "ha_energy_planner"
    assert error.value.translation_key == "restore_safe_state_failed"


def test_preflight_button_creates_notification(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "run_preflight"),
    )

    monkeypatch.setattr(
        button_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {
            "ok": False,
            "active_control_ready": False,
            "checks": [
                {
                    "check": "configured_entities_available",
                    "ok": False,
                    "blocking": True,
                    "message": "Configured entities are missing.",
                }
            ],
        },
    )

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Garage EV: preflight failed",
                "message": (
                    "Active control is not ready.\n\n"
                    "Failing checks:\n"
                    "- Configured entities available (blocking): Configured entities are missing."
                ),
                "notification_id": "ha_energy_planner_preflight_entry-1",
            },
            False,
        )
    ]


def test_preflight_notification_is_deferred_during_startup(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    coordinator.hass.data = {}
    coordinator.hass.state = CoreState.starting
    report_ok = False
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "run_preflight"),
    )

    def preflight_report(hass: object, coordinator_arg: object) -> dict[str, object]:
        return {"ok": report_ok, "active_control_ready": report_ok, "checks": []}

    monkeypatch.setattr(
        button_module,
        "build_preflight_report",
        preflight_report,
    )
    start_callbacks: list[Any] = []
    monkeypatch.setattr(
        notifications_module,
        "async_at_started",
        lambda hass_arg, callback: start_callbacks.append(callback) or (lambda: None),
    )

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.hass.services.calls == []
    report_ok = True
    coordinator.hass.state = CoreState.running
    asyncio.run(start_callbacks[0](coordinator.hass))
    assert coordinator.hass.services.calls[0][2]["title"] == "Garage EV: preflight passed"


def test_preflight_notification_message_reports_success() -> None:
    assert (
        button_module._preflight_notification_message(
            {"ok": True, "active_control_ready": True, "checks": [{"check": "recorder_available", "ok": True}]}
        )
        == "Active control is ready.\n\nAll preflight checks passed."
    )


def test_successful_preflight_returns_passed_notification(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "run_preflight"),
    )
    monkeypatch.setattr(
        button_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {"ok": True, "active_control_ready": True, "checks": []},
    )

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Garage EV: preflight passed",
                "message": "Active control is ready.\n\nAll preflight checks passed.",
                "notification_id": "ha_energy_planner_preflight_entry-1",
            },
            False,
        )
    ]


def test_production_control_buttons_call_coordinator() -> None:
    coordinator = FakeCoordinator()

    for key in (
        "arm_production_control",
        "disarm_production_control",
        "resume_control",
    ):
        button = SimpleNamespace(
            coordinator=coordinator,
            entity_description=next(description for description in BUTTONS if description.key == key),
        )
        asyncio.run(PlannerButton.async_press(button))

    assert coordinator.arm_calls == ["button_pressed"]
    assert coordinator.disarm_calls == ["button_pressed"]
    assert coordinator.resume_calls == ["button_pressed"]
