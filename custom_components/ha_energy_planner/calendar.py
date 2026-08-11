"""Read-only calendar platform for upcoming Energy Planner actions."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .models import ActionKind, PlanAction
from .sensor import (
    _action_load_forecast_attrs,
    _action_sentence,
    _asset_name,
    _decision_data_quality_attrs,
    _plain_action,
)
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
        events = [event for event in _calendar_events(self.coordinator) if event.end > now]
        return None if not events else events[0]

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return controlled actions that overlap the requested range."""
        return [
            event for event in _calendar_events(self.coordinator) if event.end > start_date and event.start < end_date
        ]


def _calendar_events(coordinator: EnergyPlannerCoordinator) -> list[CalendarEvent]:
    """Return current plan actions as calendar events in time order."""
    plan = coordinator.data
    if plan is None:
        return []
    events: list[CalendarEvent] = []
    for action in plan.actions:
        if action.kind == ActionKind.EV_SCHEDULE:
            events.extend(_ev_charging_events(action, plan.interval_minutes))
        else:
            events.append(_calendar_event(action, coordinator))
    return sorted(events, key=lambda event: event.start)


def _ev_charging_events(
    action: PlanAction,
    interval_minutes: int,
) -> list[CalendarEvent]:
    """Expand allocated EV slots into complete contiguous charging windows."""
    interval = timedelta(minutes=interval_minutes)
    valid_slots = _valid_ev_charging_slots(action)
    windows: list[dict[str, Any]] = []
    for start, charge_kw in valid_slots:
        end = start + interval
        energy_kwh = charge_kw * interval_minutes / 60
        if windows and start == windows[-1]["end"]:
            windows[-1]["end"] = end
            windows[-1]["energy_kwh"] += energy_kwh
            windows[-1]["charge_rates"].add(charge_kw)
        else:
            windows.append(
                {
                    "start": start,
                    "end": end,
                    "energy_kwh": energy_kwh,
                    "charge_rates": {charge_kw},
                }
            )
    return [_ev_charging_event(action, window, index) for index, window in enumerate(windows, start=1)]


def _valid_ev_charging_slots(action: PlanAction) -> list[tuple[datetime, float]]:
    """Return finite, timezone-aware charging slots without breaking the calendar."""
    valid_slots: list[tuple[datetime, float]] = []
    for item in action.desired_state.get("allocated_slots", []):
        if not isinstance(item, dict) or not item.get("valid_at"):
            continue
        start = dt_util.parse_datetime(str(item["valid_at"]))
        try:
            charge_kw = float(item.get("charge_kw"))
        except (TypeError, ValueError):
            continue
        if start is None or start.tzinfo is None or not isfinite(charge_kw) or charge_kw <= 0:
            continue
        valid_slots.append((start, charge_kw))
    return sorted(valid_slots, key=lambda item: item[0])


def _ev_charging_event(
    action: PlanAction,
    window: dict[str, Any],
    index: int,
) -> CalendarEvent:
    """Render one complete EV charging window."""
    start = window["start"]
    end = window["end"]
    rates = sorted(float(rate) for rate in window["charge_rates"])
    rate_text = f"{rates[0]:g} kW" if len(rates) == 1 else f"{rates[0]:g}-{rates[-1]:g} kW"
    details = _plain_action(action)
    description_lines = [
        "Planned EV charging window.",
        f"Start charging: {_local_datetime_text(start)}",
        f"Stop charging: {_local_datetime_text(end)}",
        f"Charging power: {rate_text}",
        f"Estimated energy: {window['energy_kwh']:.2f} kWh",
    ]
    target = action.desired_state.get("target_soc_percent")
    if target is not None:
        description_lines.append(f"Target SOC: {target}%")
    ready_by = action.desired_state.get("ready_by")
    if ready_by:
        description_lines.append(f"Ready by: {ready_by}")
    description_lines.append(f"Why: {details.get('why', 'No reason was recorded.')}")
    constraints = details.get("constraints")
    if constraints:
        description_lines.append(f"Constraints: {', '.join(str(item) for item in constraints)}")
    return CalendarEvent(
        start=start,
        end=end,
        summary="EV: Charging window",
        description="\n".join(description_lines),
        location="EV",
        uid=f"{action.action_id}-window-{index}",
    )


def _local_datetime_text(value: datetime) -> str:
    """Format a calendar timestamp in Home Assistant's configured timezone."""
    return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M %Z")


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
    load_forecast = _action_load_forecast_attrs(coordinator, action.action_id)
    if load_forecast:
        expected = load_forecast.get("expected_kw")
        conservative = load_forecast.get("conservative_kw")
        description_lines.append(
            "Load forecast: "
            f"{load_forecast.get('status', 'unknown')}; "
            f"expected {expected if expected is not None else 'unknown'} kW; "
            f"conservative {conservative if conservative is not None else 'unknown'} kW "
            f"at {load_forecast.get('valid_at', 'the action time')}."
        )
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
