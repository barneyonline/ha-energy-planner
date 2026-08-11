"""Button platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
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
    return bool(str(coordinator.entry_data.get("ai_task_entity", "") or "").strip())


async def _run_preflight(coordinator: EnergyPlannerCoordinator) -> None:
    report = build_preflight_report(coordinator.hass, coordinator)
    entry = coordinator.entry
    title = getattr(entry, "title", None) or "Energy Planner"
    entry_id = getattr(entry, "entry_id", None)
    notification_id = f"{_PREFLIGHT_NOTIFICATION_ID}_{entry_id}" if entry_id else _PREFLIGHT_NOTIFICATION_ID
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
    await coordinator.async_arm_production_control("button_pressed")


async def _disarm(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_disarm_production_control("button_pressed")


async def _resume(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_resume_control("button_pressed")


BUTTONS: tuple[PlannerButtonDescription, ...] = (
    PlannerButtonDescription(
        key="replan",
        translation_key="replan",
        icon="mdi:refresh",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
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
        key="request_ai_advice",
        translation_key="request_ai_advice",
        icon="mdi:comment-question-outline",
        press_fn=_request_ai_advice,
        available_fn=_ai_advice_available,
    ),
    PlannerButtonDescription(
        key="run_preflight",
        translation_key="run_preflight",
        icon="mdi:clipboard-check-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_run_preflight,
    ),
    PlannerButtonDescription(
        key="arm_production_control",
        translation_key="arm_production_control",
        icon="mdi:shield-check",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_arm,
    ),
    PlannerButtonDescription(
        key="disarm_production_control",
        translation_key="disarm_production_control",
        icon="mdi:shield-off",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_disarm,
    ),
    PlannerButtonDescription(
        key="resume_control",
        translation_key="resume_control",
        icon="mdi:play-circle-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=_resume,
    ),
)

_RETIRED_BUTTON_KEYS = (
    "ev_start_charging",
    "ev_stop_charging",
    "pause_control_1h",
    "pause_control_4h",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    _remove_retired_buttons(hass, entry)
    async_add_planner_entities(
        entry, async_add_entities, (PlannerButton(coordinator, description) for description in BUTTONS)
    )


def _remove_retired_buttons(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove retired command buttons from the entity registry."""
    registry = er.async_get(hass)
    for key in _RETIRED_BUTTON_KEYS:
        entity_id = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id is not None:
            registry.async_remove(entity_id)


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
