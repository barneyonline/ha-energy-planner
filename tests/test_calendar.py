"""Tests for the read-only plan calendar."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
    assert "Why:" in (entity.event.description or "")
    assert "Confidence:" not in (entity.event.description or "")
    assert "Data quality:" in (entity.event.description or "")
    assert "Constraints:" in (entity.event.description or "")
    assert "Load forecast: ready; expected 1.4 kW; conservative 1.8 kW" in (entity.event.description or "")

    events = asyncio.run(
        entity.async_get_events(
            coordinator.hass,
            now,
            now + timedelta(hours=2),
        )
    )

    assert [event.uid for event in events] == ["ev-start", "climate-precondition"]
    assert "Desired state:" in (events[1].description or "")
    assert "expected 2.2 kW; conservative 2.7 kW" in (events[1].description or "")


def test_calendar_handles_empty_plan_and_requested_ranges() -> None:
    now = datetime.now(UTC)
    coordinator = _coordinator(None)
    entity = EnergyPlannerCalendar(coordinator)

    assert entity.event is None
    assert asyncio.run(entity.async_get_events(coordinator.hass, now, now + timedelta(hours=1))) == []
    assert calendar_module._compact_mapping("plain") == "plain"


def test_calendar_expands_ev_schedule_into_complete_charging_windows() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    action = replace(
        _action("ev-schedule", now, now + timedelta(minutes=5)),
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "target_soc_percent": 80,
            "ready_by": "07:00",
            "allocated_slots": [
                {"valid_at": (now + timedelta(minutes=30)).isoformat(), "charge_kw": 7},
                {"valid_at": (now + timedelta(minutes=35)).isoformat(), "charge_kw": 6},
                {"valid_at": (now + timedelta(minutes=50)).isoformat(), "charge_kw": 7},
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
    assert "Charging power: 6-7 kW" in (events[0].description or "")
    assert "Estimated energy: 1.08 kWh" in (events[0].description or "")
    assert "Target SOC: 80%" in (events[0].description or "")
    assert "Ready by: 07:00" in (events[0].description or "")
    assert "Charging power: 7 kW" in (events[1].description or "")


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

    assert "expected unknown kW; conservative unknown kW" in description


def test_calendar_platform_adds_one_system_calendar(monkeypatch: object) -> None:
    coordinator = _coordinator(_plan([]))
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[object] = []

    monkeypatch.setattr(
        calendar_module,
        "async_add_planner_entities",
        lambda entry_arg, add_entities, entities: added.extend(entities),
    )

    asyncio.run(calendar_module.async_setup_entry(coordinator.hass, entry, None))

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
