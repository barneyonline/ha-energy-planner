"""Button platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .models import OutcomeResult
from .preflight import build_preflight_report
from .type_defs import EnergyPlannerConfigEntry

_PREFLIGHT_NOTIFICATION_ID = "ha_energy_planner_preflight"


@dataclass(frozen=True, kw_only=True)
class PlannerButtonDescription(ButtonEntityDescription):
    """Button description."""

    press_fn: Callable[[EnergyPlannerCoordinator], Awaitable[None]]


async def _replan(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_request_replan()


async def _restore(coordinator: EnergyPlannerCoordinator) -> None:
    outcome = await coordinator.async_restore_safe_state("button_pressed")
    if outcome.result == OutcomeResult.FAILED:
        raise HomeAssistantError(
            f"Energy Planner could not fully restore safe state: {outcome.reason}",
            translation_domain=DOMAIN,
            translation_key="restore_safe_state_failed",
            translation_placeholders={"reason": outcome.reason},
        )


async def _run_preflight(coordinator: EnergyPlannerCoordinator) -> None:
    report = build_preflight_report(coordinator.hass, coordinator)
    await coordinator.hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "Energy Planner preflight passed" if report.get("ok") else "Energy Planner preflight failed",
            "message": _preflight_notification_message(report),
            "notification_id": _PREFLIGHT_NOTIFICATION_ID,
        },
        blocking=False,
    )


async def _arm(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_arm_production_control("button_pressed")


async def _disarm(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_disarm_production_control("button_pressed")


async def _pause_one_hour(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_pause_control(60, "button_pressed", "all")


async def _pause_four_hours(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_pause_control(240, "button_pressed", "all")


async def _resume(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_resume_control("button_pressed")


async def _start_ev_charging(coordinator: EnergyPlannerCoordinator) -> None:
    await _manual_ev_charging(coordinator, True)


async def _stop_ev_charging(coordinator: EnergyPlannerCoordinator) -> None:
    await _manual_ev_charging(coordinator, False)


async def _manual_ev_charging(coordinator: EnergyPlannerCoordinator, enabled: bool) -> None:
    """Apply a manual EV command and surface adapter rejection to the user."""
    result = await coordinator.async_manual_ev_charging(enabled)
    if not result.applied:
        raise HomeAssistantError(
            f"Energy Planner could not change EV charging: {result.reason}",
            translation_domain=DOMAIN,
            translation_key="manual_ev_control_failed",
            translation_placeholders={"reason": result.reason},
        )


BUTTONS: tuple[PlannerButtonDescription, ...] = (
    PlannerButtonDescription(
        key="replan",
        translation_key="replan",
        icon="mdi:refresh",
        entity_category=EntityCategory.CONFIG,
        press_fn=_replan,
    ),
    PlannerButtonDescription(
        key="restore_safe_state",
        translation_key="restore_safe_state",
        icon="mdi:backup-restore",
        entity_category=EntityCategory.CONFIG,
        press_fn=_restore,
    ),
    PlannerButtonDescription(
        key="run_preflight",
        translation_key="run_preflight",
        icon="mdi:clipboard-check-outline",
        entity_category=EntityCategory.CONFIG,
        press_fn=_run_preflight,
    ),
    PlannerButtonDescription(
        key="arm_production_control",
        translation_key="arm_production_control",
        icon="mdi:shield-check",
        entity_category=EntityCategory.CONFIG,
        press_fn=_arm,
    ),
    PlannerButtonDescription(
        key="disarm_production_control",
        translation_key="disarm_production_control",
        icon="mdi:shield-off",
        entity_category=EntityCategory.CONFIG,
        press_fn=_disarm,
    ),
    PlannerButtonDescription(
        key="pause_control_1h",
        translation_key="pause_control_1h",
        icon="mdi:pause-circle-outline",
        entity_category=EntityCategory.CONFIG,
        press_fn=_pause_one_hour,
    ),
    PlannerButtonDescription(
        key="pause_control_4h",
        translation_key="pause_control_4h",
        icon="mdi:pause-octagon-outline",
        entity_category=EntityCategory.CONFIG,
        press_fn=_pause_four_hours,
    ),
    PlannerButtonDescription(
        key="resume_control",
        translation_key="resume_control",
        icon="mdi:play-circle-outline",
        entity_category=EntityCategory.CONFIG,
        press_fn=_resume,
    ),
    PlannerButtonDescription(
        key="ev_start_charging",
        translation_key="ev_start_charging",
        icon="mdi:ev-station",
        entity_category=EntityCategory.CONFIG,
        press_fn=_start_ev_charging,
    ),
    PlannerButtonDescription(
        key="ev_stop_charging",
        translation_key="ev_stop_charging",
        icon="mdi:ev-station-off",
        entity_category=EntityCategory.CONFIG,
        press_fn=_stop_ev_charging,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(
        entry, async_add_entities, (PlannerButton(coordinator, description) for description in BUTTONS)
    )


class PlannerButton(EnergyPlannerEntity, ButtonEntity):
    """Planner button."""

    entity_description: PlannerButtonDescription

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        description: PlannerButtonDescription,
    ) -> None:
        """Initialize button."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Handle button press."""
        await self.entity_description.press_fn(self.coordinator)


def _preflight_notification_message(report: dict[str, Any]) -> str:
    """Return a concise persistent-notification message for a preflight report."""
    status = "Active control is ready." if report.get("active_control_ready") else "Active control is not ready."
    failing_checks = [check for check in report.get("checks", []) if not check.get("ok")]
    if not failing_checks:
        check_summary = "All preflight checks passed."
    else:
        check_summary = "Failing checks:\n" + "\n".join(
            f"- {_preflight_check_name(check)} ({'blocking' if check.get('blocking') else 'advisory'}): "
            f"{check.get('message', 'No detail available.')}"
            for check in failing_checks[:8]
        )
    return f"{status}\n\n{check_summary}"


def _preflight_check_name(check: dict[str, Any]) -> str:
    """Return a readable preflight check name."""
    return str(check.get("check", "unknown_check")).replace("_", " ").capitalize()
