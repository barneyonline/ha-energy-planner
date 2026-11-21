"""Exercise HA's repeated calendar reads and bounded Next actions presentation.

Run with ``pytest -q -s tests/test_entity_publication.py`` for synthetic timings.
Assertions concern deterministic work and behavior, never wall-clock speed.
"""

from __future__ import annotations

import asyncio
import cProfile
import json
import pstats
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from unittest.mock import patch

from test_calendar import _action, _coordinator, _plan, _running_coordinator
from test_sensor import _coordinator as sensor_coordinator

from custom_components.ha_energy_planner import calendar as calendar_module
from custom_components.ha_energy_planner import entity as entity_module
from custom_components.ha_energy_planner.sensor import SENSORS, PlannerSensor

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def publication_fixture(history_count=5000):
    """Retain many discrete confirmations and large nested action evidence."""
    action = _action("next", NOW, NOW + timedelta(minutes=5))
    action.desired_state["evidence"] = {f"source_{i}": "⚡" * 20000 for i in range(16)}
    plan = _plan([action])
    plan.created_at = NOW
    coordinator = _coordinator(plan)
    coordinator.entry_data = {}
    history = []
    for index in range(history_count):
        start = NOW - timedelta(minutes=history_count-index+1)
        history.append({"uid": f"confirmed-{index}", "start": start.isoformat(),
                        "end": (start + timedelta(seconds=1)).isoformat(),
                        "confirmed_start": start.isoformat(), "confirmed_action": "true",
                        "summary": "Synthetic confirmed activity", "description": "Evidence " * 100,
                        "location": "EV"})
    coordinator.store.data["calendar_history"] = {"history": history, "pending": []}
    sensor_owner = sensor_coordinator(
        plan, options=coordinator.options, entry_data={"ev_charger_entity": "switch.synthetic_charger"},
    )
    return coordinator, sensor_owner


def _publication(calendar):
    """HA reads event for state, attributes, and scheduling its next alarm."""
    assert calendar.state == "on"
    assert calendar.state_attributes["message"] == "EV: Start EV charging"
    assert calendar.event.uid == "next"


def test_calendar_publication_work_is_bounded_by_selected_events(monkeypatch):
    monkeypatch.setattr(calendar_module.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(calendar_module.dt_util, "now", lambda: NOW)
    owner, _ = publication_fixture()
    saved = deepcopy(owner.store.data)
    calendar = calendar_module.EnergyPlannerCalendar(owner)
    profiler = cProfile.Profile()
    with patch.object(calendar_module, "CalendarEvent", wraps=calendar_module.CalendarEvent) as events:
        profiler.enable()
        started = perf_counter()
        _publication(calendar)
        elapsed_ms = (perf_counter()-started)*1000
        profiler.disable()
    print({"calendar_ms_profiled": round(elapsed_ms, 3), "event_constructions": events.call_count})
    pstats.Stats(profiler).sort_stats("cumtime").print_stats(8)
    # One live action + one selected output per read, independent of history size.
    assert events.call_count == 6
    assert owner.store.data == saved
    started = perf_counter()
    _publication(calendar)
    print({"calendar_ms_unprofiled": round((perf_counter()-started)*1000, 3)})

    # A narrow history request builds just its matching event, while historical
    # queries remain bounded by the persisted confirmation ceiling.
    start = NOW - timedelta(minutes=100)
    with patch.object(calendar_module, "CalendarEvent", wraps=calendar_module.CalendarEvent) as events:
        matching = asyncio.run(calendar.async_get_events(owner.hass, start, start + timedelta(seconds=1)))
    assert len(matching) == 1
    assert events.call_count == 2
    all_history = asyncio.run(calendar.async_get_events(owner.hass, NOW - timedelta(days=30), NOW))
    assert len(all_history) == 1000
    assert owner.store.data == saved


def test_next_actions_nested_publication_compacts_without_mutation(monkeypatch):
    monkeypatch.setattr(calendar_module.dt_util, "utcnow", lambda: NOW)
    _, owner = publication_fixture(0)
    saved = deepcopy(owner.data)
    sensor = PlannerSensor(owner, next(d for d in SENSORS if d.key == "next_actions"))
    started = perf_counter()
    readings = [(sensor.native_value, sensor.extra_state_attributes) for _ in range(3)]
    elapsed_ms = (perf_counter()-started)*1000
    assert readings[0] == readings[1] == readings[2]
    attrs = readings[0][1]
    size = len(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")).encode())
    print({"next_actions_three_reads_ms": round(elapsed_ms, 3), "attribute_bytes": size})
    assert size <= entity_module.RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
    assert attrs["attributes_truncated"] is True
    assert attrs["actions"][0]["action_id"] == "next"
    assert owner.data == saved
    owner.data.actions[0].desired_state["charge_kw"] = 3
    assert sensor.extra_state_attributes != attrs


def test_calendar_rollover_replans_options_and_expiry_are_fresh(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(calendar_module.dt_util, "utcnow", lambda: clock[0])
    first = _action("first", NOW, NOW + timedelta(minutes=5))
    second = replace(first, action_id="second", execute_not_before=NOW + timedelta(minutes=10),
                     execute_not_after=NOW + timedelta(minutes=15))
    owner = _coordinator(_plan([second, first]))
    calendar = calendar_module.EnergyPlannerCalendar(owner)
    assert calendar.event.uid == "first"
    clock[0] += timedelta(minutes=5)
    assert calendar.event.uid == "second"
    owner.options["ev_control_enabled"] = False
    assert calendar.event is None
    owner.options["ev_control_enabled"] = True
    assert calendar.event.uid == "second"
    owner.data = _plan([replace(second, action_id="new-plan")])
    assert calendar.event.uid == "new-plan"
    clock[0] = NOW + timedelta(minutes=15)
    assert calendar.event is None
    owner.data = None
    assert calendar.event is None


def test_calendar_feedback_ownership_and_clock_changes_without_replan(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(calendar_module.dt_util, "utcnow", lambda: clock[0])
    owner, action, state, actual_start = _running_coordinator(NOW, climate=True)
    calendar = calendar_module.EnergyPlannerCalendar(owner)
    assert calendar.event.start == actual_start
    assert "confirmed" in calendar.event.summary
    state.state = "off"
    assert calendar.event.start == NOW
    assert "planned" in calendar.event.summary
    state.state = "cool"
    owner.store.data["ownership"]["hvac_control"]["required_evidence_lost"] = True
    assert calendar.event.start == NOW
    owner.store.data["ownership"]["hvac_control"]["required_evidence_lost"] = False
    assert calendar.event.start == actual_start
    # The head can be off when this phase expires; no stale confirmed event may
    # remain selected without a coordinator replan or an options update.
    state.state = "off"
    clock[0] = action.execute_not_after
    assert calendar.event is None
    clock[0] = NOW - timedelta(hours=1)
    assert calendar.event.start == NOW


def test_calendar_selection_preserves_reconciliation_and_offset_order(monkeypatch):
    monkeypatch.setattr(calendar_module.dt_util, "utcnow", lambda: NOW)
    owner, _ = publication_fixture(3)
    # An archived UID must continue to suppress a provisional current record.
    archived = owner.store.data["calendar_history"]["history"][-1]
    owner.data.actions[0].action_id = archived["uid"]
    calendar = calendar_module.EnergyPlannerCalendar(owner)
    assert calendar.event is None
    # Selecting before CalendarEvent construction must compare instants, rather
    # than sorting the ISO strings of records with different UTC offsets.
    owner.data = None
    owner.store.data["calendar_history"]["history"] += [
        {**archived, "uid": "later", "start": "2026-11-06T01:00:00+00:00",
         "confirmed_start": "2026-11-06T01:00:00+00:00", "end": "2026-11-06T02:00:00+00:00"},
        {**archived, "uid": "earlier", "start": "2026-11-06T11:30:00+11:00",
         "confirmed_start": "2026-11-06T11:30:00+11:00", "end": "2026-11-06T12:00:00+11:00"},
    ]
    assert calendar.event.uid == "earlier"
    assert [e.uid for e in calendar_module._events_with_history(owner)][-2:] == ["earlier", "later"]
