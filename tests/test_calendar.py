"""Tests for the read-only plan calendar."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.ha_energy_planner import calendar as calendar_module
from custom_components.ha_energy_planner.calendar import EnergyPlannerCalendar
from custom_components.ha_energy_planner.const import (
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
)
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    EnergyPlan,
    InputHealth,
    PlanAction,
    PlannerMode,
)


def test_calendar_exposes_current_and_upcoming_actions() -> None:
    now = datetime.now(UTC)
    first = _action("ev-start", now + timedelta(minutes=10), now + timedelta(minutes=15))
    second = replace(
        first,
        action_id="climate-precondition",
        execute_not_before=now + timedelta(hours=1),
        execute_not_after=now + timedelta(hours=1, minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"hvac_mode": "heat", "target_temperature": 21},
        reason_codes=["precondition_before_peak"],
    )
    coordinator = _coordinator(_plan([second, first]))
    coordinator.store.data["forecast_snapshots"] = [
        {
            "plan_id": "plan-1",
            "built_in_load_forecast": {
                "source": "built_in_recorder_history",
                "status": "ready",
                "first_expected_kw": 1.2,
                "first_upper_kw": 1.5,
            },
            "action_load_forecasts": [
                {
                    "action_id": "ev-start",
                    "valid_at": (now + timedelta(minutes=10)).isoformat(),
                    "expected_kw": 1.4,
                    "conservative_kw": 1.8,
                },
                {
                    "action_id": "climate-precondition",
                    "valid_at": (now + timedelta(hours=1)).isoformat(),
                    "expected_kw": 2.2,
                    "conservative_kw": 2.7,
                },
            ],
        }
    ]
    entity = EnergyPlannerCalendar(coordinator)

    assert entity.event is not None
    assert entity.event.uid == "ev-start"
    assert entity.event.summary == "EV: Start EV charging"
    assert "Why\n• " in (entity.event.description or "")
    assert "Confidence:" not in (entity.event.description or "")
    assert "Data quality\n• " in (entity.event.description or "")
    assert "Constraints\n• " in (entity.event.description or "")
    assert ("Load forecast\n• Status: Ready\n• Expected load: 1.40 kW\n• Conservative load: 1.80 kW") in (
        entity.event.description or ""
    )

    events = asyncio.run(
        entity.async_get_events(
            coordinator.hass,
            now,
            now + timedelta(hours=2),
        )
    )

    assert [event.uid for event in events] == ["ev-start", "climate-precondition"]
    assert "Planned state\n" in (events[1].description or "")
    assert "• Expected load: 2.20 kW\n• Conservative load: 2.70 kW" in (events[1].description or "")


def test_calendar_handles_empty_plan_and_requested_ranges() -> None:
    now = datetime.now(UTC)
    coordinator = _coordinator(None)
    entity = EnergyPlannerCalendar(coordinator)

    assert entity.event is None
    assert asyncio.run(entity.async_get_events(coordinator.hass, now, now + timedelta(hours=1))) == []


def test_calendar_expands_ev_schedule_into_complete_charging_windows() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    action = replace(
        _action("ev-schedule", now, now + timedelta(minutes=5)),
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "target_soc_percent": 80,
            "ready_by": "07:00",
            "daylight_lowest_cost": {
                "selected": True,
                "window_start_utc": (now + timedelta(minutes=20)).isoformat(),
                "window_end_utc": (now + timedelta(hours=8)).isoformat(),
            },
            "allocated_slots": [
                {
                    "valid_at": (now + timedelta(minutes=30)).isoformat(),
                    "charge_kw": 7,
                    "allocation_source": "daylight",
                },
                {
                    "valid_at": (now + timedelta(minutes=35)).isoformat(),
                    "charge_kw": 6,
                    "allocation_source": "daylight",
                },
                {
                    "valid_at": (now + timedelta(minutes=50)).isoformat(),
                    "charge_kw": 7,
                    "allocation_source": "daylight",
                },
            ],
        },
    )
    coordinator = _coordinator(_plan([action]))
    entity = EnergyPlannerCalendar(coordinator)

    events = asyncio.run(
        entity.async_get_events(
            coordinator.hass,
            now,
            now + timedelta(hours=2),
        )
    )

    assert [event.summary for event in events] == ["EV: Charging window", "EV: Charging window"]
    assert [(event.start, event.end) for event in events] == [
        (now + timedelta(minutes=30), now + timedelta(minutes=40)),
        (now + timedelta(minutes=50), now + timedelta(minutes=55)),
    ]
    assert entity.event == events[0]
    assert events[0].uid == "ev-schedule-window-1"
    assert "Start charging:" in (events[0].description or "")
    assert "Stop charging:" in (events[0].description or "")
    assert "+00:00" not in (events[0].description or "")
    assert "Schedule\n• Start charging:" in (events[0].description or "")
    assert "Charging\n• Power: 6-7 kW" in (events[0].description or "")
    assert "Estimated energy: 1.08 kWh" in (events[0].description or "")
    assert "Target SOC: 80%" in (events[0].description or "")
    assert "Ready by: 07:00" in (events[0].description or "")
    assert "Policy: Lowest effective-cost daylight charging" in (events[0].description or "")
    assert "Sunrise:" in (events[0].description or "")
    assert "Sunset:" in (events[0].description or "")
    assert "Power: 7 kW" in (events[1].description or "")


def test_calendar_separates_daylight_and_ready_by_fallback_windows() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    action = replace(
        _action("ev-mixed-schedule", now, now + timedelta(minutes=5)),
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "daylight_lowest_cost": {
                "selected": True,
                "window_start_utc": now.isoformat(),
                "window_end_utc": (now + timedelta(minutes=5)).isoformat(),
            },
            "allocated_slots": [
                {
                    "valid_at": now.isoformat(),
                    "charge_kw": 7,
                    "allocation_source": "daylight",
                },
                {
                    "valid_at": (now + timedelta(minutes=5)).isoformat(),
                    "charge_kw": 7,
                    "allocation_source": "ready_by_fallback",
                },
            ],
        },
    )

    events = calendar_module._calendar_events(_coordinator(_plan([action])))

    assert len(events) == 2
    assert "Allocation: Daylight preference" in (events[0].description or "")
    assert "Sunset:" in (events[0].description or "")
    assert "Allocation: Ready-by fallback" in (events[1].description or "")
    assert "Sunrise:" not in (events[1].description or "")


def test_calendar_omits_ev_schedule_without_allocated_charging() -> None:
    now = datetime.now(UTC)
    action = replace(
        _action("ev-idle", now, now + timedelta(minutes=5)),
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"charging_required_now": False, "allocated_slots": []},
    )
    coordinator = _coordinator(_plan([action]))

    assert EnergyPlannerCalendar(coordinator).event is None


def test_calendar_omits_actions_for_disabled_device_controls() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    ev_action = _action("ev-start", now, now + timedelta(minutes=5))
    climate_action = replace(
        ev_action,
        action_id="climate-precondition",
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
    )
    enphase_action = replace(
        ev_action,
        action_id="enphase-profile",
        asset=ActionAsset.ENPHASE,
        kind=ActionKind.SET_PROFILE,
    )
    coordinator = _coordinator(_plan([ev_action, climate_action, enphase_action]))
    coordinator.options.update(
        {
            CONF_EV_CONTROL_ENABLED: False,
            CONF_CLIMATE_CONTROL_ENABLED: True,
            CONF_ENPHASE_CONTROL_ENABLED: False,
        }
    )

    events = calendar_module._calendar_events(coordinator)

    assert [event.uid for event in events] == ["climate-precondition"]


def test_calendar_omits_ev_charging_windows_when_ev_control_is_disabled() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    action = replace(
        _action("ev-schedule", now, now + timedelta(minutes=5)),
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "allocated_slots": [
                {"valid_at": (now + timedelta(minutes=30)).isoformat(), "charge_kw": 7},
            ],
        },
    )
    coordinator = _coordinator(_plan([action]))
    coordinator.options[CONF_EV_CONTROL_ENABLED] = False

    assert calendar_module._calendar_events(coordinator) == []


def test_calendar_ignores_malformed_ev_slots() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    action = replace(
        _action("ev-invalid-slots", now, now + timedelta(minutes=5)),
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "allocated_slots": [
                None,
                {"valid_at": "not-a-date", "charge_kw": 7},
                {"valid_at": now.replace(tzinfo=None).isoformat(), "charge_kw": 7},
                {"valid_at": now.isoformat(), "charge_kw": "invalid"},
                {"valid_at": now.isoformat(), "charge_kw": {"unexpected": 7}},
                {"valid_at": now.isoformat(), "charge_kw": "nan"},
                {"valid_at": now.isoformat(), "charge_kw": 0},
                {"valid_at": now.isoformat(), "charge_kw": 7},
            ]
        },
    )

    events = calendar_module._calendar_events(_coordinator(_plan([action])))

    assert len(events) == 1
    assert events[0].start == now


def test_calendar_renders_missing_action_load_values_as_unknown() -> None:
    now = datetime.now(UTC)
    action = _action("ev-start", now + timedelta(minutes=10), now + timedelta(minutes=15))
    coordinator = _coordinator(_plan([action]))
    coordinator.store.data["forecast_snapshots"] = [
        {
            "plan_id": "plan-1",
            "built_in_load_forecast": {"status": "learning"},
            "action_load_forecasts": [
                {
                    "action_id": action.action_id,
                    "valid_at": action.execute_not_before.isoformat(),
                    "expected_kw": None,
                    "conservative_kw": None,
                }
            ],
        }
    ]

    description = EnergyPlannerCalendar(coordinator).event.description or ""

    assert "Expected load: Unknown" in description
    assert "Conservative load: Unknown" in description


def test_calendar_description_uses_readable_sections_and_local_time(monkeypatch: object) -> None:
    local_zone = ZoneInfo("Australia/Melbourne")
    monkeypatch.setattr(
        calendar_module.dt_util,
        "as_local",
        lambda value: value.astimezone(local_zone),
    )
    period_start = datetime(2026, 8, 15, 11, 38, tzinfo=UTC)
    action = replace(
        _action("climate-precondition", period_start, period_start + timedelta(minutes=15)),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={
            "hvac_mode": "heat",
            "target_temperature": 24.0,
            "period_start": period_start.isoformat(),
            "period_end": (period_start + timedelta(hours=1)).isoformat(),
            "enable_zones": True,
            "controlled_zones": ["switch.bedrooms", "switch.main"],
            "reason": "internal_duplicate_reason",
        },
    )

    description = calendar_module._calendar_events(_coordinator(_plan([action])))[0].description or ""

    assert (
        "Schedule\n• Period Start: Sat 15 Aug 2026, 9:38 PM AEST\n• Period End: Sat 15 Aug 2026, 10:38 PM AEST"
    ) in description
    assert (
        "Planned state\n"
        "• Climate mode: Heat\n"
        "• Target temperature: 24 °C\n"
        "• Enable Zones: Yes\n"
        "• Controlled Zones: switch.bedrooms, switch.main"
    ) in description
    assert "Internal Duplicate Reason" not in description
    assert "+00:00" not in description
    assert "2026-08-15T" not in description


def test_calendar_detail_formatting_handles_units_and_nested_values() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)

    assert calendar_module._calendar_detail("Projected HVAC load kW", 1.5) == ("Projected HVAC load: 1.5 kW")
    assert calendar_module._calendar_detail("Target SOC percent", 80) == "Target SOC: 80%"
    assert calendar_module._calendar_value_text(False) == "No"
    assert calendar_module._calendar_value_text({"enabled": True, "at": now}) == (
        f"enabled: Yes; at: {calendar_module._local_datetime_text(now)}"
    )
    assert calendar_module._calendar_datetime(now.replace(tzinfo=None)) is None


def test_calendar_event_metadata_is_bounded_for_recorder() -> None:
    now = datetime.now(UTC)
    huge_text = "calendar evidence ⚡ " * 2_000
    action = replace(
        _action(huge_text, now + timedelta(minutes=10), now + timedelta(minutes=15)),
        desired_state={"detail": huge_text},
        hard_constraints=[huge_text],
        reason_codes=[huge_text],
    )

    event = EnergyPlannerCalendar(_coordinator(_plan([action]))).event

    assert event is not None
    assert len((event.summary or "").encode("utf-8")) <= 255
    assert len((event.description or "").encode("utf-8")) <= 4_096
    assert len((event.location or "").encode("utf-8")) <= 255
    assert len((event.uid or "").encode("utf-8")) <= 255


def test_calendar_platform_adds_one_system_calendar(monkeypatch: object) -> None:
    coordinator = _coordinator(_plan([]))
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[object] = []

    asyncio.run(calendar_module.async_setup_entry(coordinator.hass, entry, added.extend))

    assert len(added) == 1
    assert isinstance(added[0], EnergyPlannerCalendar)
    assert added[0].translation_key == "plan"


def _action(action_id: str, start: datetime, end: datetime) -> PlanAction:
    return PlanAction(
        action_id=action_id,
        plan_id="plan-1",
        execute_not_before=start,
        execute_not_after=end,
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={"charge_kw": 7},
        hard_constraints=["grid_import_limit"],
        reason_codes=["ev_soc_below_target"],
        expected_cost_delta=0.25,
        confidence=0.9,
    )


def _plan(actions: list[PlanAction]) -> EnergyPlan:
    return EnergyPlan(
        plan_id="plan-1",
        created_at=datetime.now(UTC),
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=0.9,
        estimated_daily_cost=2.0,
        actions=actions,
        preview=[],
    )


def _coordinator(plan: EnergyPlan | None) -> SimpleNamespace:
    return SimpleNamespace(
        data=plan,
        options={
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: True,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
        store=SimpleNamespace(data={}),
        entry=SimpleNamespace(entry_id="entry-1"),
        hass=SimpleNamespace(),
    )
