"""Regression coverage for bounded confirmed plan calendar history."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ha_energy_planner import calendar_history
from custom_components.ha_energy_planner.calendar_history import (
    CALENDAR_HISTORY_HARD_CAP,
    CALENDAR_HISTORY_RETENTION_DAYS,
    calendar_feedback_recheck_delay,
    calendar_records,
    confirm_calendar_action,
    normalize_calendar_history,
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


def test_calendar_feedback_recheck_delay_includes_window_boundaries():
    history = {"history": [], "pending": [record(start=3), record(end=2)]}
    observations = [record(end=-1), record(end=1), {"start": "invalid"}]
    assert calendar_feedback_recheck_delay(history, observations, NOW) == timedelta(minutes=1)
    assert calendar_feedback_recheck_delay(history, [], NOW) == timedelta(minutes=2)
    assert calendar_feedback_recheck_delay(history, [], NOW + timedelta(minutes=3)) == datetime.resolution


def test_calendar_feedback_recheck_delay_includes_history_expiry_without_timestamp_overflow():
    last_retained_time = NOW - timedelta(days=CALENDAR_HISTORY_RETENTION_DAYS)
    archived = {**record(actual=True), "end": last_retained_time.isoformat()}
    history = {"history": [archived], "pending": []}
    assert calendar_feedback_recheck_delay(history, [], NOW) == datetime.resolution

    latest = datetime.max.replace(tzinfo=UTC)
    history["history"] = [{**record(actual=True), "end": latest.isoformat()}]
    assert calendar_feedback_recheck_delay(history, [], latest) == (
        timedelta(days=CALENDAR_HISTORY_RETENTION_DAYS) + datetime.resolution
    )


@pytest.mark.parametrize("source", ["feedback", "execution", "action"])
def test_calendar_history_reads_saved_pending_once(monkeypatch, source):
    saved = {"pending": [
        record(actual=True),
        record(start=20, end=30, uid="future"),
    ]}
    pending_reads = 0

    def count_saved_pending(value, **kwargs):
        nonlocal pending_reads
        if value is saved["pending"]:
            pending_reads += 1
        return calendar_records(value, **kwargs)

    monkeypatch.setattr(calendar_history, "calendar_records", count_saved_pending)
    if source == "action":
        state = confirm_calendar_action(saved, {
            "kind": "ev_stop", "result": "applied", "action_id": "event", "attempted_at": NOW.isoformat(),
        })
        assert state["history"][0]["confirmed_action"] == "true"
    else:
        state = update_calendar_history(
            saved, [record(actual=True)], NOW, replace_plan=False, location="EV" if source == "feedback" else None,
        )
        assert state["pending"] == saved["pending"]
    assert pending_reads == 1


def test_calendar_feedback_only_compares_confirmed_windows_with_matching_starts(monkeypatch):
    running = record(actual=True)
    history = [
        record(start=-index - 60, end=-index - 59, uid=f"archive-{index}", actual=True)
        for index in range(CALENDAR_HISTORY_HARD_CAP)
    ]
    future = [
        {
            **record(start=index * 5 + 10, end=index * 5 + 15, uid=f"tariff-{index}"),
            "location": "Enphase Export Limit",
        }
        for index in range(288)
    ]
    saved = {"history": list(reversed(history)), "pending": [running, *future]}
    comparisons = []
    original_match = calendar_history._same_running_window

    def count_match(previous, current, now):
        comparisons.append((previous["uid"], current["uid"]))
        return original_match(previous, current, now)

    monkeypatch.setattr(calendar_history, "_same_running_window", count_match)

    assert update_calendar_history(saved, [running], NOW, replace_plan=False, location="EV") == saved
    assert comparisons == [(running["uid"], running["uid"])]


def test_expired_provisional_windows_skip_confirmed_history_comparisons(monkeypatch):
    history = [
        record(start=-index - 60, end=-index - 59, uid=f"archive-{index}", actual=True)
        for index in reversed(range(CALENDAR_HISTORY_HARD_CAP))
    ]
    expired = [
        record(start=-index - 2, end=-index - 1, uid=f"planned-{index}")
        for index in range(288)
    ]
    saved = {"history": history, "pending": expired}
    comparisons = 0
    original_match = calendar_history._same_confirmed_window

    def count_match(previous, current):
        nonlocal comparisons
        comparisons += 1
        return original_match(previous, current)

    monkeypatch.setattr(calendar_history, "_same_confirmed_window", count_match)

    assert update_calendar_history(saved, [], NOW, replace_plan=False) == {"history": history, "pending": []}
    assert comparisons == 0


def test_history_expires_confirmed_windows_after_retention_period():
    elapsed = record(start=-30, end=-20, actual=True)
    future = record(start=20, end=30, uid="cancelled")
    state = update_calendar_history({}, [elapsed, future], NOW)
    assert state["history"][0]["uid"] == elapsed["uid"]
    assert "End time is estimated" in state["history"][0]["description"]
    state = update_calendar_history(
        state, [], NOW + timedelta(days=CALENDAR_HISTORY_RETENTION_DAYS + 1)
    )
    assert state == {"history": [], "pending": []}


def test_history_keeps_only_the_newest_bounded_confirmations():
    history = [
        record(start=-index - 2, end=-index - 1, uid=f"archive-{index}", actual=True)
        for index in reversed(range(CALENDAR_HISTORY_HARD_CAP + 25))
    ]
    state = update_calendar_history({"history": history}, [], NOW)
    assert len(state["history"]) == CALENDAR_HISTORY_HARD_CAP
    assert state["history"][-1]["uid"] == "archive-0"


@pytest.mark.parametrize("newest_first", [False, True])
def test_unsorted_legacy_history_retains_newest_confirmations(newest_first):
    recent = [
        record(start=-index - 2, end=-index - 1, uid=f"recent-{index}", actual=True)
        for index in range(CALENDAR_HISTORY_HARD_CAP)
    ]
    older = record(start=-10_000, end=-9_999, uid="delayed-older", actual=True)
    expired = record(start=-600_000, end=-599_999, uid="delayed-expired", actual=True)
    pending = record(start=10, end=20, uid="pending")
    saved = {"history": [*(recent if newest_first else reversed(recent)), older, expired],
             "pending": [pending]}

    state = normalize_calendar_history(saved, NOW)

    assert [item["uid"] for item in state["history"]] == [
        f"recent-{index}" for index in reversed(range(CALENDAR_HISTORY_HARD_CAP))
    ]
    assert state["pending"] == [pending]
    assert saved["history"][-2:] == [older, expired]


def test_history_cap_counts_valid_confirmations_after_malformed_tail_records():
    history = [
        record(start=-index - 2, end=-index - 1, uid=f"archive-{index}", actual=True)
        for index in reversed(range(CALENDAR_HISTORY_HARD_CAP))
    ]
    unconfirmed = [
        record(start=index, end=index + 1, uid=f"unconfirmed-{index}")
        for index in range(25)
    ]
    state = update_calendar_history(
        {"history": [*history, *unconfirmed, *([None] * 25)]}, [], NOW
    )
    assert len(state["history"]) == CALENDAR_HISTORY_HARD_CAP
    assert state["history"][-1]["uid"] == "archive-0"


def test_cancelled_future_window_is_discarded_before_it_starts():
    state = update_calendar_history(None, [record(start=20, end=30)], NOW)
    assert update_calendar_history(state, [], NOW + timedelta(minutes=1)) == {"history": [], "pending": []}


def test_confirmation_removed_at_its_start_does_not_archive_a_zero_length_window():
    state = update_calendar_history(None, [record(start=0, actual=True)], NOW)

    assert update_calendar_history(state, [], NOW) == {"history": [], "pending": []}


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


def test_large_history_preserves_provisional_id_rejection_and_confirmed_extension():
    history = [
        record(start=-10_000-index*10, end=-9_995-index*10, uid=f"archive-{index}", actual=True)
        for index in range(1000)
    ]
    elapsed = record(start=-30, end=-20, uid="continuing", actual=True)
    saved = {"history": sorted([*history, elapsed], key=lambda item: item["start"]), "pending": []}
    # A reused provisional ID cannot recreate an already confirmed activity.
    provisional = record(start=10, end=20, uid="archive-0")
    future = record(start=20, end=30, uid="future")
    state = update_calendar_history(saved, [provisional, future], NOW)
    assert len(state["history"]) == CALENDAR_HISTORY_HARD_CAP
    assert {item["uid"] for item in state["history"]} == {
        item["uid"] for item in sorted(saved["history"], key=lambda item: item["start"])[-CALENDAR_HISTORY_HARD_CAP:]
    }
    assert state["pending"] == [future]

    # Independent confirmation can extend the same phase beyond an archived
    # estimate without growing beyond the retained history ceiling.
    extended = record(start=-30, end=10, uid="replanned", actual=True)
    state = update_calendar_history(saved, [extended, future], NOW)
    assert len(state["history"]) == CALENDAR_HISTORY_HARD_CAP - 1
    assert "archive-999" not in {item["uid"] for item in state["history"]}
    assert state["pending"] == [extended, future]


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


@pytest.mark.parametrize("kind,label", [
    ("set_export_limit", "Enabled — 0 W"), ("disable_export_limit", "Disabled"),
])
def test_export_confirmation_relabels_saved_history_without_changing_identity_or_duplicating(kind, label):
    previous = {
        **record(start=-10, end=-9, actual=True), "confirmed_action": "true",
        "summary": f"Enphase Export Limit: {label}", "location": "Enphase Export Limit",
    }
    previous["end"] = (datetime.fromisoformat(previous["start"]) + timedelta(seconds=1)).isoformat()
    saved = {"history": [previous]}
    state = update_calendar_history(saved, [], NOW)
    expected = {**previous, "summary": f"Enphase Export Limit: {label} (confirmed)"}
    assert state["history"] == [expected]
    assert saved["history"] == [previous]  # Reading does not mutate saved records.
    assert update_calendar_history(state, [], NOW) == state
    state = confirm_calendar_action(state, {
        "result": "applied", "kind": kind, "asset": "enphase_export_limit",
        "plan_id": "old-plan", "action_id": previous["uid"], "attempted_at": previous["start"],
    })
    assert len(state["history"]) == 1
    assert state["history"][0]["uid"] == previous["uid"]
    assert state["history"][0]["summary"] == expected["summary"]


def test_unconfirmed_export_window_does_not_gain_confirmation_label():
    planned = {**record(), "summary": "Enphase Export Limit: Disabled", "location": "Enphase Export Limit"}
    assert calendar_records([planned])[0]["summary"] == planned["summary"]
    assert update_calendar_history({"pending": [planned]}, [], NOW + timedelta(hours=1))["history"] == []


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


@pytest.mark.parametrize("extra", [
    {"asset": "enphase"}, {"result": "applied"}, {"result": "pending"},
    {"result": "failed"}, {"result": "rejected"}, {"result": "skipped"},
    {"action_id": ""}, {"action_id": None},
    {"pre_state": None}, {"pre_state": {}}, {"post_state": None}, {"post_state": {}},
    *[{"post_state": {"watts": watts}} for watts in (-1, 100001, True, 0.0, "0")],
    {"post_state": {"watts": None, "slew_rate": 77}},
])
def test_export_restoration_requires_confirmed_changed_setting_even_with_matching_plan(extra):
    planned = record(uid="restore-request")
    state = {"pending": [planned]}
    outcome = {
        "result": "restored", "asset": "enphase_export_limit", "kind": "restore_export_limit",
        "plan_id": "manual", "action_id": planned["uid"], "attempted_at": NOW.isoformat(),
        "pre_state": {"watts": None, "slew_rate": 77}, "post_state": {"watts": 0, "slew_rate": 77},
        **extra,
    }
    result = confirm_calendar_action(state, outcome)
    assert not result.get("history")
    assert result["pending"] == [planned]


def test_slew_only_restoration_uses_readback_setting_and_keeps_future_plan_separate():
    planned = record(uid="restore-request", summary="Enphase Export Limit: Disabled (planned)")
    state = confirm_calendar_action({"pending": [planned]}, {
        "result": "restored", "asset": "enphase_export_limit", "kind": "restore_export_limit",
        "plan_id": "manual", "action_id": planned["uid"], "attempted_at": NOW.isoformat(),
        "pre_state": {"watts": 3000, "slew_rate": 100}, "post_state": {"watts": 3000, "slew_rate": 77},
    })
    assert state["history"][0]["summary"] == "Enphase Export Limit: Restored Enabled — 3000 W (confirmed)"
    assert state["pending"] == [planned]


def test_confirmed_climate_release_cannot_keep_its_future_planned_label():
    future = {**record(start=20, end=21, uid='release'), 'location': 'Climate',
              'summary': 'Climate: Release climate control (planned)'}
    saved = {'pending': [future]}
    outcome = {'attempted_at': (NOW + timedelta(minutes=20)).isoformat(), 'asset': 'daikin',
               'kind': 'release_hvac', 'result': 'restored', 'action_id': 'release', 'plan_id': 'plan'}
    confirmed = confirm_calendar_action(saved, outcome)
    assert confirmed['history'][0]['summary'] == 'Climate: Release climate control (confirmed)'
    failed = confirm_calendar_action(saved, {**outcome, 'result': 'failed'})
    assert failed.get('history', []) == []


@pytest.mark.parametrize('archived', [False, True])
def test_climate_phase_label_upgrade_does_not_duplicate_confirmed_activity(archived):
    previous = {**record(start=-30, end=-1 if archived else 5, actual=True), 'location': 'Climate',
                'summary': 'Climate: Change climate state'}
    current = {**previous, 'end': (NOW + timedelta(minutes=60)).isoformat(),
               'summary': 'Climate: Pre-cool to 19 °C (confirmed)'}
    saved = {'history' if archived else 'pending': [previous]}
    updated = update_calendar_history(saved, [current], NOW)
    assert updated == {'history': [], 'pending': [current]}
    expired = update_calendar_history(updated, [], NOW + timedelta(hours=2))
    assert len(expired['history']) == 1
    # A different confirmed phase UID must retain a separate history record.
    separate = update_calendar_history(saved, [{**current, 'uid': 'other-phase'}], NOW)
    assert len(separate['history']) == len(separate['pending']) == 1
