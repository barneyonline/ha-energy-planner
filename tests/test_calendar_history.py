"""Regression coverage for permanent plan calendar history."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ha_energy_planner.calendar_history import (
    calendar_records,
    confirm_calendar_action,
    update_calendar_history,
)

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def record(start=-10, end=10, uid="event", summary="EV: Charge", actual=False):
    item = {
        "uid": uid, "summary": summary, "location": "EV",
        "start": (NOW + timedelta(minutes=start)).isoformat(),
        "end": (NOW + timedelta(minutes=end)).isoformat(),
        "description": "Actual start: confirmed\nCharging in progress." if actual else "Planned EV charging window.",
    }
    if actual:
        item["confirmed_start"] = item["start"]
    return item


def test_history_keeps_elapsed_windows_without_time_or_count_expiry():
    elapsed = record(start=-30, end=-20, actual=True)
    future = record(start=20, end=30, uid="cancelled")
    state = update_calendar_history({}, [elapsed, future], NOW)
    state = update_calendar_history(state, [], NOW + timedelta(days=1000))
    assert len(state["history"]) == 1
    assert state["history"][0]["uid"] == elapsed["uid"]
    assert "End time is estimated" in state["history"][0]["description"]
    assert state["pending"] == []
    state = update_calendar_history(state, [], NOW + timedelta(days=2000))
    assert len(state["history"]) == 1


def test_cancelled_future_window_is_discarded_before_it_starts():
    state = update_calendar_history(None, [record(start=20, end=30)], NOW)
    assert update_calendar_history(state, [], NOW + timedelta(minutes=1)) == {"history": [], "pending": []}


def test_unconfirmed_replan_preserves_current_action_id_and_timing():
    state = update_calendar_history({}, [record()], NOW)
    revised = record(start=-1, end=20, uid="new-plan-id")
    state = update_calendar_history(state, [revised], NOW)
    assert not state["history"]
    assert state["pending"][0]["start"] == revised["start"]
    assert state["pending"][0]["uid"] == "new-plan-id"
    assert state["pending"][0]["end"] == revised["end"]
    assert revised["start"] == record(start=-1)["start"]  # Input is not mutated.


def test_removed_running_window_is_closed_at_replan_time():
    state = update_calendar_history({}, [record(actual=True)], NOW)
    state = update_calendar_history(state, [], NOW)
    archived = state["history"][0]
    assert archived["end"] == NOW.isoformat()
    assert "no confirmed stop time" in archived["description"]
    assert state["pending"] == []


def test_confirmed_start_updates_planned_window_and_restart_remains_separate():
    state = update_calendar_history({}, [record()], NOW)
    confirmed = record(start=-5, end=20, uid="confirmed", actual=True)
    state = update_calendar_history(state, [confirmed], NOW)
    assert state["pending"] == [confirmed]
    restarted = record(start=-1, end=30, uid="restart", actual=True)
    state = update_calendar_history(state, [restarted], NOW)
    assert len(state["history"]) == 1
    assert state["history"][0]["uid"] == "confirmed"
    assert state["pending"] == [restarted]


def test_unavailable_confirmation_keeps_history_without_confirming_new_plan():
    confirmed = record(actual=True)
    state = update_calendar_history({}, [confirmed], NOW)
    state = update_calendar_history(state, [record(start=-1, uid="new")], NOW)
    assert state["history"][0]["start"] == confirmed["start"]
    assert "confirmed_start" not in state["pending"][0]


def test_different_action_does_not_merge_with_running_window():
    state = update_calendar_history({}, [record(actual=True)], NOW)
    state = update_calendar_history(state, [record(summary="EV: Stop")], NOW)
    assert len(state["history"]) == len(state["pending"]) == 1


def test_history_deduplicates_elapsed_window_from_current_plan():
    elapsed = record(start=-30, end=-20, actual=True)
    state = {"pending": [elapsed]}
    state = update_calendar_history(state, [{**elapsed, "uid": "new-plan-id"}], NOW)
    assert len(state["history"]) == 1 and state["history"][0]["uid"] == elapsed["uid"]


@pytest.mark.parametrize("invalid", [
    None, {}, [None], [{"start": 42}],
    [{**record(), "start": "invalid"}],
    [{**record(), "start": "2026-09-30T12:00:00"}],
    [{**record(), "end": record()["start"]}],
    [{**record(), "uid": ""}], [{**record(), "summary": 42}],
])
def test_malformed_saved_events_are_ignored(invalid):
    assert calendar_records(invalid) == []


def test_saved_metadata_is_bounded_and_unknown_fields_are_ignored():
    item = {**record(), "description": "⚡" * 5000, "location": None, "secret": "ignored"}
    result = calendar_records([item])[0]
    assert len(result["description"].encode()) <= 4096
    assert "location" not in result and "secret" not in result


@pytest.mark.parametrize("archived", [False, True])
def test_confirmed_phase_extended_after_forecast_end_has_one_event(archived):
    previous = record(start=-20, end=-1, actual=True)
    saved = {"history" if archived else "pending": [previous]}
    extended = record(start=-20, end=30, actual=True)
    state = update_calendar_history(saved, [extended], NOW)
    assert state == {"history": [], "pending": [extended]}


def test_future_window_moved_to_now_does_not_preserve_future_start():
    saved = {"pending": [record(start=10, end=20)]}
    moved = record(start=-1, end=20, uid="moved")
    state = update_calendar_history(saved, [moved], NOW)
    assert state == {"history": [], "pending": [moved]}


@pytest.mark.parametrize("saved", [{}, {"history": [record(start=-30, end=-20)]}, {"pending": [record()]}])
def test_unconfirmed_elapsed_or_removed_windows_are_never_history(saved):
    state = update_calendar_history(saved, [record(start=-30, end=-20)], NOW)
    assert state == {"history": [], "pending": []}


def test_description_alone_is_not_confirmation():
    item = record(start=-30, end=-20)
    item["description"] = "Actual start: purported start"
    state = update_calendar_history({"history": [item], "pending": [item]}, [item], NOW)
    assert state["history"] == []


@pytest.mark.parametrize("stamp", ["invalid", "2026-09-30T12:00:00", record(start=-1)["start"]])
def test_invalid_or_mismatched_confirmation_is_discarded(stamp):
    item = {**record(), "confirmed_start": stamp, "confirmed_action": "true"}
    result = calendar_records([item])[0]
    assert "confirmed_start" not in result and "confirmed_action" not in result


@pytest.mark.parametrize("result,kind", [
    ("failed", "set_profile"), ("skipped", "set_profile"), ("rejected", "set_profile"),
    ("applied", "ev_start"), ("applied", "ev_schedule"), ("applied", "set_hvac"),
    ("restored", "set_profile"), ("failed", "release_hvac"), ("skipped", "release_hvac"),
])
def test_failed_skipped_and_service_only_activity_does_not_confirm_history(result, kind):
    state = {"pending": [record()]}
    assert confirm_calendar_action(state, {
        "action_id": "event", "kind": kind, "result": result, "attempted_at": NOW.isoformat(),
    }) == state


@pytest.mark.parametrize("kind,result", [
    ("set_profile", "applied"), ("restore_ai", "applied"), ("ev_stop", "applied"),
    ("release_hvac", "restored"),
])
def test_confirmed_discrete_actions_are_retained_without_claiming_running_duration(kind, result):
    item = record()
    state = confirm_calendar_action({"pending": [item]}, {
        "action_id": item["uid"], "kind": kind, "result": result, "attempted_at": NOW.isoformat(),
    })
    event = state["history"][0]
    assert event["confirmed_start"] == NOW.isoformat()
    assert datetime.fromisoformat(event["end"]) - NOW == timedelta(seconds=1)
    assert "not a running duration" in event["description"]
    # The executed action replaces its original planned window in the calendar.
    state = update_calendar_history(state, [item], NOW + timedelta(minutes=1))
    assert len(state["history"]) == 1 and state["pending"] == []


@pytest.mark.parametrize("saved,stamp,action_id", [
    (None, NOW.isoformat(), "event"),
    ({"pending": [record()]}, "invalid", "event"),
    ({"pending": [record()]}, NOW.isoformat(), "unrelated"),
])
def test_discrete_confirmation_requires_valid_timestamp_and_matching_calendar_event(saved, stamp, action_id):
    result = confirm_calendar_action(saved, {
        "result": "applied", "kind": "set_profile", "action_id": action_id, "attempted_at": stamp,
    })
    assert not result.get("history")


def test_success_after_overlapping_discrete_replan_keeps_current_action_identity():
    previous = record(uid="old", summary="Enphase: Set profile")
    revised = record(start=-1, uid="new", summary="Enphase: Set profile")
    state = update_calendar_history({"pending": [previous]}, [revised], NOW)
    state = confirm_calendar_action(state, {
        "result": "applied", "kind": "set_profile", "action_id": "new", "attempted_at": NOW.isoformat(),
    })
    assert len(state["history"]) == 1
    assert state["history"][0]["uid"] == "new"


def test_feedback_preserves_latest_future_actions_and_other_asset_confirmation():
    latest = record(start=10, end=20, uid="latest-plan-profile", summary="Enphase: Set profile")
    climate = {**record(actual=True, uid="climate"), "location": "Climate", "summary": "Climate: Cool"}
    state = {"pending": [latest, climate]}
    # Late feedback renders the previous coordinator plan while the newer plan is already saved.
    older = record(uid="old-profile", summary="Enphase: Set profile")
    state = update_calendar_history(state, [older], NOW, replace_plan=False, location="EV")
    assert state["history"] == []
    assert state["pending"] == [latest, climate]


@pytest.mark.parametrize("asset,kind,expected", [
    ("enphase", "set_profile", "Enphase"), ("enphase", "restore_ai", "Enphase"),
    ("ev", "ev_stop", "EV"), ("daikin", "release_hvac", "Climate"),
])
def test_outcome_survives_plan_replacement_or_expired_presentation_window(asset, kind, expected):
    state = confirm_calendar_action({"pending": [record(uid="new-plan")]}, {
        "result": "restored" if kind == "release_hvac" else "applied", "kind": kind, "asset": asset,
        "action_id": "superseded-plan-action", "plan_id": "superseded-plan", "attempted_at": NOW.isoformat(),
    })
    assert len(state["history"]) == 1
    assert state["history"][0]["uid"] == "superseded-plan-action"
    assert state["history"][0]["location"] == expected
    assert state["pending"][0]["uid"] == "new-plan"


@pytest.mark.parametrize("extra", [
    {"plan_id": "manual", "asset": "enphase"}, {"plan_id": "", "asset": "enphase"},
    {"plan_id": "plan", "asset": None}, {"plan_id": "plan", "asset": "unknown"},
    {"plan_id": "plan", "asset": "enphase", "action_id": ""},
])
def test_outcome_fallback_requires_a_real_planned_device_action(extra):
    state = confirm_calendar_action({}, {
        "result": "applied", "kind": "set_profile", "action_id": "action", "attempted_at": NOW.isoformat(), **extra,
    })
    assert state["history"] == []


def test_stale_execution_snapshot_preserves_confirmation_until_device_feedback():
    confirmed = record(start=-5, end=30, actual=True)
    state = update_calendar_history({"pending": [confirmed]}, [], NOW, replace_plan=False)
    assert state == {"history": [], "pending": [confirmed]}
    stop = NOW + timedelta(minutes=10)
    state = update_calendar_history(state, [], stop, replace_plan=False, location="EV")
    assert state["pending"] == []
    assert state["history"][0]["end"] == stop.isoformat()


def test_positive_execution_snapshot_replaces_only_confirmed_asset():
    previous = record(start=-10, end=20, actual=True)
    climate = {**record(start=-10, end=30, actual=True), "uid": "climate", "location": "Climate"}
    restarted = record(start=-1, end=30, uid="restart", actual=True)
    state = update_calendar_history({"pending": [previous, climate]}, [restarted], NOW, replace_plan=False)
    assert state["pending"] == [restarted, climate]
    assert len(state["history"]) == 1
    assert state["history"][0]["uid"] == previous["uid"]


@pytest.mark.parametrize("recovered_end", [-10, -1])
def test_elapsed_confirmation_coalesces_with_history_and_preserves_later_estimate(recovered_end):
    previous = record(start=-20, end=-5, actual=True)
    recovered = record(start=-20, end=recovered_end, actual=True)
    state = update_calendar_history({"history": [previous]}, [recovered], NOW, replace_plan=False, location="EV")
    assert len(state["history"]) == 1
    assert state["history"][0]["end"] == max(previous["end"], recovered["end"])
    assert state["pending"] == []


def test_old_elapsed_execution_confirmation_preserves_newer_running_phase_extent():
    newer = record(start=-20, end=30, actual=True)
    older = record(start=-20, end=-1, actual=True)
    state = update_calendar_history({"pending": [newer]}, [older], NOW, replace_plan=False)
    assert state == {"history": [], "pending": [newer]}
