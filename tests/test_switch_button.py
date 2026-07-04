"""Tests for switch and button entity behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner import button as button_module
from custom_components.ha_energy_planner import switch as switch_module
from custom_components.ha_energy_planner.button import BUTTONS, PlannerButton
from custom_components.ha_energy_planner.const import CONF_AI_ENABLED, CONF_DRY_RUN, CONF_PLANNER_ENABLED
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
        self.entry = SimpleNamespace(entry_id="entry-1", options=options or {})
        self.hass = SimpleNamespace(config_entries=FakeConfigEntries(), services=FakeServices())
        self.replan_count = 0
        self.restore_calls: list[str] = []
        self.arm_calls: list[str] = []
        self.disarm_calls: list[str] = []
        self.pause_calls: list[tuple[int, str, str]] = []
        self.resume_calls: list[str] = []

    @property
    def options(self) -> dict[str, object]:
        return {
            CONF_PLANNER_ENABLED: False,
            CONF_DRY_RUN: True,
            CONF_AI_ENABLED: False,
            **dict(self.entry.options),
        }

    async def async_request_replan(self) -> None:
        self.replan_count += 1

    async def async_restore_safe_state(self, reason: str) -> None:
        self.restore_calls.append(reason)

    async def async_arm_production_control(self, reason: str) -> None:
        self.arm_calls.append(reason)

    async def async_disarm_production_control(self, reason: str) -> None:
        self.disarm_calls.append(reason)

    async def async_pause_control(self, duration_minutes: int, reason: str, asset: str) -> None:
        self.pause_calls.append((duration_minutes, reason, asset))

    async def async_resume_control(self, reason: str) -> None:
        self.resume_calls.append(reason)


def test_switch_updates_config_entry_option_and_replans() -> None:
    coordinator = FakeCoordinator({CONF_DRY_RUN: True})
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in SWITCHES if description.option_key == CONF_DRY_RUN),
        write_count=0,
    )
    switch._async_set_option = lambda value: PlannerSwitch._async_set_option(switch, value)
    switch.async_write_ha_state = lambda: setattr(switch, "write_count", switch.write_count + 1)

    assert PlannerSwitch.is_on.fget(switch) is True

    asyncio.run(PlannerSwitch.async_turn_off(switch))

    assert coordinator.entry.options[CONF_DRY_RUN] is False
    assert coordinator.hass.config_entries.updated[-1][1][CONF_DRY_RUN] is False
    assert coordinator.replan_count == 1
    assert switch.write_count == 1
    assert PlannerSwitch.is_on.fget(switch) is False


def test_switch_setup_and_constructor(monkeypatch: object) -> None:
    coordinator = FakeCoordinator({CONF_DRY_RUN: True})
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[object] = []

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.extend(entities)

    monkeypatch.setattr(switch_module, "async_add_planner_entities", fake_add_planner_entities)

    asyncio.run(switch_module.async_setup_entry(None, entry, None))
    switch = PlannerSwitch(coordinator, next(description for description in SWITCHES if description.key == "dry_run"))

    assert len(added) == len(SWITCHES)
    assert switch.is_on is True


def test_switch_preserves_other_options_when_updated() -> None:
    coordinator = FakeCoordinator({CONF_DRY_RUN: True, CONF_AI_ENABLED: True})
    switch = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(
            description for description in SWITCHES if description.option_key == CONF_PLANNER_ENABLED
        ),
    )
    switch._async_set_option = lambda value: PlannerSwitch._async_set_option(switch, value)
    switch.async_write_ha_state = lambda: None

    asyncio.run(PlannerSwitch.async_turn_on(switch))

    assert coordinator.entry.options[CONF_PLANNER_ENABLED] is True
    assert coordinator.entry.options[CONF_DRY_RUN] is True
    assert coordinator.entry.options[CONF_AI_ENABLED] is True


def test_button_setup_and_constructor(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[object] = []

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.extend(entities)

    monkeypatch.setattr(button_module, "async_add_planner_entities", fake_add_planner_entities)

    asyncio.run(button_module.async_setup_entry(None, entry, None))
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


def test_restore_button_restores_safe_state() -> None:
    coordinator = FakeCoordinator()
    button = SimpleNamespace(
        coordinator=coordinator,
        entity_description=next(description for description in BUTTONS if description.key == "restore_safe_state"),
    )

    asyncio.run(PlannerButton.async_press(button))

    assert coordinator.restore_calls == ["button_pressed"]
    assert coordinator.replan_count == 0


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
                "title": "Energy Planner preflight failed",
                "message": (
                    "Active control is not ready.\n\n"
                    "Failing checks:\n"
                    "- Configured entities available (blocking): Configured entities are missing."
                ),
                "notification_id": "ha_energy_planner_preflight",
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
