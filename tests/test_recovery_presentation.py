"""Recovery explanations distinguish displayed readings from accepted samples."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.const import (
    CONF_HOUSEHOLD_LOAD,
    CONF_LOAD_RECOVERY_MAX_AGE_MINUTES,
)
from custom_components.ha_energy_planner.entity import recorder_safe_attributes
from custom_components.ha_energy_planner.recovery_presentation import recovery_details

NOW = datetime(2026, 9, 26, 0, 40, tzinfo=UTC)
SOURCE = "sensor.consumption"


def coordinator():
    state = SimpleNamespace(
        state="526", attributes={"unit_of_measurement": "W", "sampled_at_utc": NOW.isoformat()},
        last_reported=NOW,
    )
    return SimpleNamespace(
        store=SimpleNamespace(data={"load_source_outage": {
            "entity_id": SOURCE, "started_at": (NOW - timedelta(minutes=20)).isoformat(),
            "recovery_first_sample": NOW.isoformat(), "recovery_stage": "stabilizing",
        }}),
        entry_data={CONF_HOUSEHOLD_LOAD: SOURCE}, options={},
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: state)),
        automatic_control_requested=True, effective_control=False,
        data=SimpleNamespace(input_issues=["household_load_entity_unavailable"]),
    )


@pytest.mark.parametrize(("changes", "reason"), [
    ({"state": "unavailable"}, "sample_unavailable"),
    ({"state": "bad"}, "sample_unavailable"),
    ({"sample": "bad"}, "sample_timestamp_missing"),
    ({"sample": NOW + timedelta(seconds=1)}, "sample_in_future"),
    ({"sample": NOW - timedelta(minutes=16)}, "sample_too_old"),
    ({"sample": NOW - timedelta(minutes=5), "started": NOW}, "sample_before_outage"),
    ({}, "waiting_for_next_sample"),
    ({"first": None}, "waiting_for_next_sample"),
    ({"first": NOW + timedelta(minutes=1)}, "waiting_for_next_sample"),
    ({"first": NOW - timedelta(minutes=31)}, "waiting_for_next_sample"),
    ({"first": NOW - timedelta(minutes=5)}, "waiting_for_plan"),
])
def test_exact_consumption_blocker(changes, reason):
    c = coordinator()
    state = c.hass.states.get(SOURCE)
    state.state = changes.get("state", state.state)
    state.attributes["sampled_at_utc"] = changes.get("sample", NOW)
    outage = c.store.data["load_source_outage"]
    outage["recovery_first_sample"] = changes.get("first", NOW)
    outage["started_at"] = changes.get("started", NOW - timedelta(minutes=20))
    details = recovery_details(c, NOW)
    assert details["reason"] == reason
    assert details["load_recovery_pending"]
    assert details["plan_issues"] == ["household_load_entity_unavailable"]
    assert details["sample_max_age_seconds"] == 900
    assert details["required_sample_advance_seconds"] == 60
    assert details["sample_pair_window_seconds"] == 1800
    assert len(details["summary"]) < 255
    assert recorder_safe_attributes(details)["evaluated_at"] == NOW.isoformat()


def test_delayed_cloud_samples_remain_explainable_until_healthy_plan_commits():
    c = coordinator()
    state = c.hass.states.get(SOURCE)
    state.attributes["sampled_at_utc"] = NOW - timedelta(minutes=6)
    c.store.data["load_source_outage"]["recovery_first_sample"] = NOW - timedelta(minutes=11)
    details = recovery_details(c, NOW)
    assert details["reason"] == "waiting_for_plan"
    assert details["sample_age_seconds"] == 360
    assert details["first_sample_age_seconds"] == 660
    assert details["sample_advance_seconds"] == 300
    c.options[CONF_LOAD_RECOVERY_MAX_AGE_MINUTES] = 5
    assert recovery_details(c, NOW)["reason"] == "sample_too_old"
    c.options.clear()
    c.store.data["load_source_outage"] = {}
    c._load_recovery_pending = True
    assert recovery_details(c, NOW)["reason"] == "waiting_for_plan"
    c._load_recovery_pending = False
    c.store.data["production"] = {"startup_auto_recovery": {
        "status": "recovered", "successful_runs": 1, "required_runs": 1, "completed_at": NOW,
    }}
    c.effective_control = True
    details = recovery_details(c, NOW)
    assert details["reason"] == "recovered"
    assert not details["automatic_retry"]
    assert not details["load_recovery_pending"]
    assert details["recovered_at"] == NOW
    c.effective_control = False
    assert recovery_details(c, NOW)["reason"] == "inactive"


@pytest.mark.parametrize("status", [
    "waiting", "waiting_for_home_assistant", "grace", "validating", "restoring", "waiting_for_safe",
])
def test_startup_progress_and_cancellation(status):
    c = coordinator()
    c.store.data["load_source_outage"] = None
    c.store.data["production"] = {"startup_auto_recovery": {
        "status": status, "successful_runs": 0, "required_runs": 3,
        "last_reason": "validation_plan_unsafe", "deadline": NOW - timedelta(minutes=10),
    }}
    details = recovery_details(c, NOW)
    assert details["reason"] == status
    assert details["automatic_retry"]
    assert details["retry_interval_seconds"] == (
        30 if status in {"waiting_for_safe", "validating", "restoring"} else None
    )
    assert details["successful_checks"] == 0
    assert details["required_checks"] == 3
    assert details["startup_last_reason"] == "validation_plan_unsafe"
    c.automatic_control_requested = False
    details = recovery_details(c, NOW)
    assert not details["automatic_retry"]
    assert details["retry_interval_seconds"] is None
    assert details["reason"] == "inactive"


def test_missing_source_and_entity_timestamp_fallback():
    c = coordinator()
    state = c.hass.states.get(SOURCE)
    state.attributes.pop("sampled_at_utc")
    details = recovery_details(c, NOW)
    assert details["sampled_at"] == NOW
    assert details["sample_timestamp_source"] == "entity_report_time"
    c.hass.states.get = lambda _: None
    assert recovery_details(c, NOW)["reason"] == "sample_unavailable"
    c.entry_data.clear()
    c.data = None
    c.store.data["production"] = {"startup_auto_recovery": "invalid"}
    details = recovery_details(c, NOW)
    assert details["reason"] == "inactive"
    assert details["plan_issues"] == []
    assert details["sampled_at"] is None
    assert details["source_entity_id"] is None


@pytest.mark.parametrize("status", ["waiting", "waiting_for_home_assistant", "grace"])
def test_startup_waits_do_not_claim_thirty_second_validation(status):
    c = coordinator()
    c.store.data["load_source_outage"] = {}
    c.store.data["production"] = {"startup_auto_recovery": {
        "status": status, "deadline": NOW + timedelta(minutes=10),
    }}
    details = recovery_details(c, NOW)
    assert details["automatic_retry"]
    assert details["retry_interval_seconds"] is None


def test_accepted_samples_do_not_become_a_new_blocker_while_waiting_for_plan():
    from custom_components.ha_energy_planner.coordinator import _updated_load_source_outage

    c = coordinator()
    state = c.hass.states.get(SOURCE)
    state.attributes["sampled_at_utc"] = NOW - timedelta(minutes=6)
    c.store.data["load_source_outage"]["recovery_first_sample"] = NOW - timedelta(minutes=11)
    # The real acceptance path consumes the pair before the plan can commit.
    c.store.data["load_source_outage"] = _updated_load_source_outage(
        c.hass, c.entry_data, c.store.data["load_source_outage"], now=NOW,
    )
    assert not c.store.data["load_source_outage"]
    c._load_recovery_pending = True
    c.data.input_issues = ["amber_import_price_entity_unavailable"]
    details = recovery_details(c, NOW + timedelta(minutes=10))
    assert details["sample_age_seconds"] == 960
    assert details["reason"] == "waiting_for_plan"
    assert details["plan_issues"] == ["amber_import_price_entity_unavailable"]
