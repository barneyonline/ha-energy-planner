"""Read-only calendar platform for upcoming Energy Planner actions."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
)
from .coordinator import EnergyPlannerCoordinator
from .entity import (
    EnergyPlannerEntity,
    async_add_planner_entities,
    recorder_safe_identifier,
    recorder_safe_text,
)
from .models import ActionAsset, ActionKind, PlanAction
from .safety import strict_bool
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
        if not _calendar_control_enabled(coordinator, action.asset):
            continue
        if action.kind == ActionKind.EV_SCHEDULE:
            events.extend(_ev_charging_events(action, plan.interval_minutes))
        else:
            events.append(_calendar_event(action, coordinator))
    return sorted(events, key=lambda event: event.start)


def _calendar_control_enabled(
    coordinator: EnergyPlannerCoordinator,
    asset: ActionAsset,
) -> bool:
    """Return whether the asset is selected for device control."""
    option_by_asset = {
        ActionAsset.EV: CONF_EV_CONTROL_ENABLED,
        ActionAsset.DAIKIN: CONF_CLIMATE_CONTROL_ENABLED,
        ActionAsset.ENPHASE: CONF_ENPHASE_CONTROL_ENABLED,
    }
    return strict_bool(coordinator.options.get(option_by_asset[asset]), default=False)


def _ev_charging_events(
    action: PlanAction,
    interval_minutes: int,
) -> list[CalendarEvent]:
    """Expand allocated EV slots into complete contiguous charging windows."""
    interval = timedelta(minutes=interval_minutes)
    valid_slots = _valid_ev_charging_slots(action)
    windows: list[dict[str, Any]] = []
    for start, charge_kw, allocation_source in valid_slots:
        end = start + interval
        energy_kwh = charge_kw * interval_minutes / 60
        if (
            windows
            and start == windows[-1]["end"]
            and allocation_source == windows[-1]["allocation_source"]
        ):
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
                    "allocation_source": allocation_source,
                }
            )
    return [_ev_charging_event(action, window, index) for index, window in enumerate(windows, start=1)]


def _valid_ev_charging_slots(action: PlanAction) -> list[tuple[datetime, float, str]]:
    """Return finite, timezone-aware charging slots without breaking the calendar."""
    valid_slots: list[tuple[datetime, float, str]] = []
    for item in action.desired_state.get("allocated_slots", []):
        if not isinstance(item, dict) or not item.get("valid_at"):
            continue
        start = dt_util.parse_datetime(str(item["valid_at"]))
        charge_kw_value = item.get("charge_kw")
        if not isinstance(charge_kw_value, str | int | float):
            continue
        try:
            charge_kw = float(charge_kw_value)
        except (TypeError, ValueError):
            continue
        if start is None or start.tzinfo is None or not isfinite(charge_kw) or charge_kw <= 0:
            continue
        allocation_source = str(item.get("allocation_source") or "ready_by")
        valid_slots.append((start, charge_kw, allocation_source))
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
    description_lines = ["Planned EV charging window."]
    _append_description_section(
        description_lines,
        "Schedule",
        [
            f"Start charging: {_local_datetime_text(start)}",
            f"Stop charging: {_local_datetime_text(end)}",
        ],
    )
    charging_details = [
        f"Power: {rate_text}",
        f"Estimated energy: {window['energy_kwh']:.2f} kWh",
    ]
    target = action.desired_state.get("target_soc_percent")
    if target is not None:
        charging_details.append(f"Target SOC: {target}%")
    ready_by = action.desired_state.get("ready_by")
    if ready_by:
        charging_details.append(f"Ready by: {ready_by}")
    daylight = action.desired_state.get("daylight_lowest_cost")
    allocation_source = str(window.get("allocation_source") or "ready_by")
    if (
        isinstance(daylight, dict)
        and daylight.get("selected")
        and allocation_source == "daylight"
    ):
        charging_details.append("Policy: Lowest effective-cost daylight charging")
        charging_details.append("Allocation: Daylight preference")
        for key, label in (
            ("window_start_utc", "Sunrise"),
            ("window_end_utc", "Sunset"),
        ):
            instant = dt_util.parse_datetime(str(daylight.get(key, "")))
            if instant is not None:
                charging_details.append(f"{label}: {_local_datetime_text(instant)}")
    elif (
        isinstance(daylight, dict)
        and daylight.get("selected")
        and allocation_source == "ready_by_fallback"
    ):
        charging_details.append("Policy: Ready-by fallback after daylight preference")
        charging_details.append("Allocation: Ready-by fallback")
    _append_description_section(description_lines, "Charging", charging_details)
    _append_description_section(
        description_lines,
        "Why",
        [details.get("why", "No reason was recorded.")],
    )
    constraints = details.get("constraints")
    if constraints:
        _append_description_section(description_lines, "Constraints", constraints)
    return CalendarEvent(
        start=start,
        end=end,
        summary=recorder_safe_text("EV: Charging window", max_bytes=255),
        description=recorder_safe_text("\n".join(description_lines), max_bytes=4_096),
        location=recorder_safe_text("EV", max_bytes=255),
        uid=recorder_safe_identifier(f"{action.action_id}-window-{index}", max_bytes=255),
    )


def _local_datetime_text(value: datetime) -> str:
    """Format a calendar timestamp in Home Assistant's configured timezone."""
    local = dt_util.as_local(value)
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local:%a} {local.day} {local:%b %Y}, {clock} {local:%Z}"


def _calendar_event(action: PlanAction, coordinator: EnergyPlannerCoordinator) -> CalendarEvent:
    """Convert a controlled action to a Home Assistant calendar event."""
    details = _plain_action(action)
    description_lines = [_action_sentence(action)]
    _append_description_section(
        description_lines,
        "Why",
        [details.get("why", "No reason was recorded.")],
    )
    data_quality = _decision_data_quality_attrs(coordinator)
    if data_quality.get("status") != "Good":
        _append_description_section(
            description_lines,
            "Data quality",
            [data_quality.get("summary", "Limited input data.")],
        )
    constraints = details.get("constraints")
    if constraints:
        _append_description_section(description_lines, "Constraints", constraints)
    desired_state = details.get("desired_state")
    if isinstance(desired_state, dict):
        schedule: list[str] = []
        planned_state: list[str] = []
        for key, value in desired_state.items():
            if str(key).casefold().endswith("reason"):
                continue
            detail = _calendar_detail(str(key), value)
            if _calendar_datetime(value) is not None:
                schedule.append(detail)
            else:
                planned_state.append(detail)
        _append_description_section(description_lines, "Schedule", schedule)
        _append_description_section(description_lines, "Planned state", planned_state)
    load_forecast = _action_load_forecast_attrs(coordinator, action.action_id)
    if load_forecast:
        expected = load_forecast.get("expected_kw")
        conservative = load_forecast.get("conservative_kw")
        forecast_details = [
            f"Status: {_display_value(load_forecast.get('status', 'unknown'))}",
            f"Expected load: {_power_text(expected)}",
            f"Conservative load: {_power_text(conservative)}",
        ]
        valid_at = _calendar_datetime(load_forecast.get("valid_at"))
        if valid_at is not None:
            forecast_details.append(f"Forecast time: {_local_datetime_text(valid_at)}")
        _append_description_section(
            description_lines,
            "Load forecast",
            forecast_details,
        )
    return CalendarEvent(
        start=action.execute_not_before,
        end=action.execute_not_after,
        summary=recorder_safe_text(f"{_asset_name(action.asset)}: {details['action']}", max_bytes=255),
        description=recorder_safe_text("\n".join(description_lines), max_bytes=4_096),
        location=recorder_safe_text(_asset_name(action.asset), max_bytes=255),
        uid=recorder_safe_identifier(action.action_id, max_bytes=255),
    )


def _append_description_section(lines: list[str], heading: str, items: list[Any]) -> None:
    """Append a spaced, bulleted calendar-description section."""
    visible_items = [str(item) for item in items if item not in (None, "")]
    if not visible_items:
        return
    lines.extend(["", heading, *(f"• {item}" for item in visible_items)])


def _calendar_detail(label: str, value: Any) -> str:
    """Return one readable planned-state detail with units and local time."""
    rendered_label = label
    rendered_value = _calendar_value_text(value)
    lower_label = label.casefold()
    if lower_label.endswith(" c") and isinstance(value, int | float):
        rendered_label = label[:-2]
        rendered_value = f"{value:g} °C"
    elif lower_label.endswith(" kw") and isinstance(value, int | float):
        rendered_label = label[:-3]
        rendered_value = f"{value:g} kW"
    elif lower_label.endswith(" percent") and isinstance(value, int | float):
        rendered_label = label[:-8]
        rendered_value = f"{value:g}%"
    return f"{rendered_label}: {rendered_value}"


def _calendar_value_text(value: Any) -> str:
    """Render a calendar detail without raw datetimes or Python containers."""
    parsed = _calendar_datetime(value)
    if parsed is not None:
        return _local_datetime_text(parsed)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(_calendar_value_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_calendar_value_text(item)}" for key, item in value.items())
    return str(value)


def _calendar_datetime(value: Any) -> datetime | None:
    """Parse one timezone-aware calendar timestamp."""
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
    else:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _power_text(value: Any) -> str:
    """Return a compact forecast-power label."""
    return "Unknown" if not isinstance(value, int | float) else f"{value:.2f} kW"


def _display_value(value: Any) -> str:
    """Return a short title-cased calendar value."""
    return str(value).replace("_", " ").title()
