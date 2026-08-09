"""Tests for the read-only plan calendar."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.ha_energy_planner import calendar as calendar_module
from custom_components.ha_energy_planner.calendar import EnergyPlannerCalendar
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
    assert "Load forecast: ready; expected 1.2 kW; conservative 1.5 kW." in (entity.event.description or "")

    events = asyncio.run(
        entity.async_get_events(
            coordinator.hass,
            now,
            now + timedelta(hours=2),
        )
    )

    assert [event.uid for event in events] == ["ev-start", "climate-precondition"]
    assert "Desired state:" in (events[1].description or "")


def test_calendar_handles_empty_plan_and_requested_ranges() -> None:
    now = datetime.now(UTC)
    coordinator = _coordinator(None)
    entity = EnergyPlannerCalendar(coordinator)

    assert entity.event is None
    assert asyncio.run(entity.async_get_events(coordinator.hass, now, now + timedelta(hours=1))) == []
    assert calendar_module._compact_mapping("plain") == "plain"


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
        requires_haeo_plan_id=None,
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
        store=SimpleNamespace(data={}),
        entry=SimpleNamespace(entry_id="entry-1"),
        hass=SimpleNamespace(),
    )
