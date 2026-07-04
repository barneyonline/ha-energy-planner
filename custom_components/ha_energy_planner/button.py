"""Button platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .type_defs import EnergyPlannerConfigEntry


@dataclass(frozen=True, kw_only=True)
class PlannerButtonDescription(ButtonEntityDescription):
    """Button description."""

    press_fn: Callable[[EnergyPlannerCoordinator], Awaitable[None]]


async def _replan(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_request_replan()


async def _restore(coordinator: EnergyPlannerCoordinator) -> None:
    await coordinator.async_restore_safe_state("button_pressed")


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
