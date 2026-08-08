"""Read-only calendar platform for upcoming Energy Planner actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .models import PlanAction
from .sensor import _action_sentence, _asset_name, _decision_data_quality_attrs, _plain_action
from .type_defs import EnergyPlannerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the planner action calendar."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(entry, async_add_entities, [EnergyPlannerCalendar(coordinator)])


class EnergyPlannerCalendar(EnergyPlannerEntity, CalendarEntity):
    """Calendar of controlled actions in the current committed plan."""

    _attr_translation_key = "plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator, "plan_calendar")

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next controlled action."""
        now = dt_util.utcnow()
        actions = [action for action in _actions(self.coordinator) if action.execute_not_after > now]
        return None if not actions else _calendar_event(actions[0], self.coordinator)

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return controlled actions that overlap the requested range."""
        return [
            _calendar_event(action, self.coordinator)
            for action in _actions(self.coordinator)
            if action.execute_not_after > start_date and action.execute_not_before < end_date
        ]


def _actions(coordinator: EnergyPlannerCoordinator) -> list[PlanAction]:
    """Return current plan actions in calendar order."""
    plan = coordinator.data
    return [] if plan is None else sorted(plan.actions, key=lambda action: action.execute_not_before)


def _calendar_event(action: PlanAction, coordinator: EnergyPlannerCoordinator) -> CalendarEvent:
    """Convert a controlled action to a Home Assistant calendar event."""
    details = _plain_action(action)
    description_lines = [
        _action_sentence(action),
        f"Why: {details.get('why', 'No reason was recorded.')}",
    ]
    data_quality = _decision_data_quality_attrs(coordinator)
    if data_quality.get("status") != "Good":
        description_lines.append(f"Data quality: {data_quality.get('summary', 'Limited input data.')}")
    constraints = details.get("constraints")
    if constraints:
        description_lines.append(f"Constraints: {', '.join(str(item) for item in constraints)}")
    desired_state = details.get("desired_state")
    if desired_state:
        description_lines.append(f"Desired state: {_compact_mapping(desired_state)}")
    return CalendarEvent(
        start=action.execute_not_before,
        end=action.execute_not_after,
        summary=f"{_asset_name(action.asset)}: {details['action']}",
        description="\n".join(description_lines),
        location=_asset_name(action.asset),
        uid=action.action_id,
    )


def _compact_mapping(value: Any) -> str:
    """Return compact user-facing key/value details for a calendar description."""
    if not isinstance(value, dict):
        return str(value)
    return ", ".join(f"{key}: {item}" for key, item in value.items())
