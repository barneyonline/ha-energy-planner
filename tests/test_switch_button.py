"""Tests for switch and button entity behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_energy_planner import button as button_module
from custom_components.ha_energy_planner import switch as switch_module
from custom_components.ha_energy_planner.button import BUTTONS, PlannerButton
from custom_components.ha_energy_planner.const import (
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
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
        self.pause_calls: list[tuple[int, str, str]] = []
        self.resume_calls: list[str] = []
        self.ev_keep_on_calls: list[bool] = []
        self.ai_advice_requests = 0
        self.entry_data: dict[str, object] = {}
        self.active_control_calls: list[bool] = []
        self.device_control_calls: list[tuple[str, bool]] = []
        self.active_control_value = False
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

    async def async_disarm_production_control(self, reason: str) -> None:
        self.disarm_calls.append(reason)

    async def async_pause_control(self, duration_minutes: int, reason: str, asset: str) -> None:
        self.pause_calls.append((duration_minutes, reason, asset))

    async def async_resume_control(self, reason: str) -> None:
        self.resume_calls.append(reason)

    async def async_set_ev_keep_charger_on(self, enabled: bool) -> None:
        self.ev_keep_on_calls.append(enabled)
        options = self.options
        options[CONF_EV_KEEP_CHARGER_ON] = enabled
        self.hass.config_entries.async_update_entry(self.entry, options=options)


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


def test_switches_expose_one_master_and_three_device_controls() -> None:
    keys = {description.key for description in SWITCHES}

    assert {"active_control", "climate_control", "ev_control", "enphase_control"} <= keys
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

    asyncio.run(PlannerSwitch.async_turn_on(switch))

    assert coordinator.device_control_calls == [(option_key, True)]
    assert coordinator.entry.options[option_key] is True
    assert switch.write_count == 1


def test_non_activation_switches_still_use_their_own_options() -> None:
    coordinator = FakeCoordinator({CONF_EV_LOW_PRICE_CHARGING_ENABLED: False})
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(
            description for description in SWITCHES if description.key == "ev_opportunistic_charging"
        ),
        write_count=0,
    )
    switch._async_set_option = lambda value: PlannerSwitch._async_set_option(switch, value)
    switch.async_write_ha_state = lambda: setattr(switch, "write_count", switch.write_count + 1)

    assert PlannerSwitch.is_on.fget(switch) is False
    asyncio.run(PlannerSwitch.async_turn_on(switch))
    asyncio.run(PlannerSwitch.async_turn_off(switch))

    assert coordinator.entry.options[CONF_EV_LOW_PRICE_CHARGING_ENABLED] is False
    assert coordinator.replan_count == 2
    assert switch.write_count == 2


def test_switch_setup_and_constructor(monkeypatch: object) -> None:
    coordinator = FakeCoordinator({CONF_DRY_RUN: True})
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list[object] = []
    removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str:
            return f"switch.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.extend(entities)

    monkeypatch.setattr(switch_module, "async_add_planner_entities", fake_add_planner_entities)
    monkeypatch.setattr(switch_module.er, "async_get", lambda hass: FakeRegistry())

    asyncio.run(switch_module.async_setup_entry(SimpleNamespace(), entry, None))
    switch = PlannerSwitch(
        coordinator,
        next(description for description in SWITCHES if description.key == "active_control"),
    )

    assert len(added) == len(SWITCHES)
    assert switch.is_on is False
    assert removed == [
        "switch.entry-1_enabled",
        "switch.entry-1_dry_run",
        "switch.entry-1_ai_enabled",
        "switch.entry-1_ev_control_enabled",
        "switch.entry-1_climate_control_enabled",
        "switch.entry-1_enphase_control_enabled",
        "switch.entry-1_ev_connected_helper",
    ]


def test_opportunistic_charging_switch_persists_and_replans() -> None:
    coordinator = FakeCoordinator({CONF_EV_LOW_PRICE_CHARGING_ENABLED: False})
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(
            description
            for description in SWITCHES
            if description.option_key == CONF_EV_LOW_PRICE_CHARGING_ENABLED
        ),
        write_count=0,
    )
    switch._async_set_option = lambda value: PlannerSwitch._async_set_option(switch, value)
    switch.async_write_ha_state = lambda: setattr(switch, "write_count", switch.write_count + 1)

    asyncio.run(PlannerSwitch.async_turn_on(switch))

    assert coordinator.entry.options[CONF_EV_LOW_PRICE_CHARGING_ENABLED] is True
    assert coordinator.replan_count == 1
    assert switch.write_count == 1


def test_keep_on_switch_uses_coordinator_validation_path() -> None:
    coordinator = FakeCoordinator({CONF_EV_KEEP_CHARGER_ON: False})
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(
            description
            for description in SWITCHES
            if description.option_key == CONF_EV_KEEP_CHARGER_ON
        ),
        write_count=0,
    )
    switch._async_set_option = lambda value: PlannerSwitch._async_set_option(switch, value)
    switch.async_write_ha_state = lambda: setattr(
        switch,
        "write_count",
        switch.write_count + 1,
    )

    asyncio.run(PlannerSwitch.async_turn_on(switch))

    assert coordinator.ev_keep_on_calls == [True]
    assert coordinator.entry.options[CONF_EV_KEEP_CHARGER_ON] is True
    assert switch.write_count == 1


def test_button_setup_and_constructor(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list[object] = []
    removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str:
            return f"button.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.extend(entities)

    monkeypatch.setattr(button_module, "async_add_planner_entities", fake_add_planner_entities)
    monkeypatch.setattr(button_module.er, "async_get", lambda hass: FakeRegistry())

    asyncio.run(button_module.async_setup_entry(None, entry, None))
    button = PlannerButton(coordinator, next(description for description in BUTTONS if description.key == "replan"))

    assert len(added) == len(BUTTONS)
    assert button.unique_id == "entry-1_replan"
    assert removed == ["button.entry-1_ev_start_charging", "button.entry-1_ev_stop_charging"]


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
    assert description.available_fn(coordinator) is True
    entity = PlannerButton(coordinator, description)
    assert entity.available is True

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
    assert descriptions["request_ai_advice"].icon == "mdi:comment-question-outline"


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


def test_preflight_notification_message_reports_success() -> None:
    assert (
        button_module._preflight_notification_message(
            {"ok": True, "active_control_ready": True, "checks": [{"check": "recorder_available", "ok": True}]}
        )
        == "Active control is ready.\n\nAll preflight checks passed."
    )


def test_successful_preflight_dismisses_old_alert_without_notifying(monkeypatch: object) -> None:
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
            "dismiss",
            {"notification_id": "ha_energy_planner_preflight_entry-1"},
            False,
        )
    ]


def test_production_control_buttons_call_coordinator() -> None:
    coordinator = FakeCoordinator()

    for key in (
        "arm_production_control",
        "disarm_production_control",
        "pause_control_1h",
        "pause_control_4h",
        "resume_control",
    ):
        button = SimpleNamespace(
            coordinator=coordinator,
            entity_description=next(description for description in BUTTONS if description.key == key),
        )
        asyncio.run(PlannerButton.async_press(button))

    assert coordinator.arm_calls == ["button_pressed"]
    assert coordinator.disarm_calls == ["button_pressed"]
    assert coordinator.pause_calls == [
        (60, "button_pressed", "all"),
        (240, "button_pressed", "all"),
    ]
    assert coordinator.resume_calls == ["button_pressed"]
