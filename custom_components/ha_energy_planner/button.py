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
from .discovery import CapabilityDiscovery
from .entity import EnergyPlannerEntity
from .models import OutcomeResult
from .notifications import defer_persistent_notification
from .preflight import build_preflight_report
from .type_defs import EnergyPlannerConfigEntry

_PREFLIGHT_NOTIFICATION_ID = "ha_energy_planner_preflight"


# Coordinator locks serialize commands; allow stop controls to dispatch immediately.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PlannerButtonDescription(ButtonEntityDescription):
    """Button description."""

    press_fn: Callable[[EnergyPlannerCoordinator], Awaitable[None]]
    available_fn: Callable[[EnergyPlannerCoordinator], bool] = lambda coordinator: True


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


async def _request_ai_advice(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_request_ai_advice()


def _ai_advice_available(coordinator: EnergyPlannerCoordinator) -> bool:
    return (
        CapabilityDiscovery(
            coordinator.hass,
            coordinator.entry_data,
            coordinator.options,
        )
        .inspect()
        .ai.supported
    )


async def _run_preflight(coordinator: EnergyPlannerCoordinator) -> None:
    entry = coordinator.entry
    title = getattr(entry, "title", None) or "Energy Planner"
    entry_id = getattr(entry, "entry_id", None)
    notification_id = f"{_PREFLIGHT_NOTIFICATION_ID}_{entry_id}" if entry_id else _PREFLIGHT_NOTIFICATION_ID
    if defer_persistent_notification(
        coordinator.hass,
        notification_id,
        lambda: _run_preflight(coordinator),
        owner_id=entry_id,
    ):
        return
    report = build_preflight_report(coordinator.hass, coordinator)
    await _async_publish_preflight_notification(
        coordinator,
        report,
        title=title,
        notification_id=notification_id,
    )


async def _async_publish_preflight_notification(
    coordinator: EnergyPlannerCoordinator,
    report: dict[str, Any],
    *,
    title: str,
    notification_id: str,
) -> None:
    """Publish one preflight result after Home Assistant startup."""
    await coordinator.hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": f"{title}: preflight passed" if report.get("ok") else f"{title}: preflight failed",
            "message": _preflight_notification_message(report),
            "notification_id": notification_id,
        },
        blocking=False,
    )


async def _arm(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_operator_arm_production_control("button_pressed")


async def _disarm(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_operator_disarm_production_control("button_pressed")


async def _resume(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_resume_control("button_pressed")


BUTTONS: tuple[PlannerButtonDescription, ...] = (
    PlannerButtonDescription(
        key="replan",
        translation_key="replan",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_replan,
    ),
    PlannerButtonDescription(
        key="restore_safe_state",
        translation_key="restore_safe_state",
        entity_category=EntityCategory.CONFIG,
        press_fn=_restore,
    ),
    PlannerButtonDescription(
        key="request_ai_advice",
        translation_key="request_ai_advice",
        press_fn=_request_ai_advice,
        available_fn=_ai_advice_available,
    ),
    PlannerButtonDescription(
        key="run_preflight",
        translation_key="run_preflight",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_run_preflight,
    ),
    PlannerButtonDescription(
        key="arm_production_control",
        translation_key="arm_production_control",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_arm,
    ),
    PlannerButtonDescription(
        key="disarm_production_control",
        translation_key="disarm_production_control",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_disarm,
    ),
    PlannerButtonDescription(
        key="resume_control",
        translation_key="resume_control",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_resume,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_entities(PlannerButton(coordinator, description) for description in BUTTONS)


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

    @property
    def available(self) -> bool:
        """Return whether this button can currently be used."""
        return super().available and self.entity_description.available_fn(self.coordinator)


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
