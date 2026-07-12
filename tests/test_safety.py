"""Tests for shared fail-closed safety state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.safety import (
    control_pause_reason,
    parse_production_state,
    strict_bool,
)


def test_production_state_parser_is_strict_bounded_and_fail_closed() -> None:
    assert parse_production_state(None).armed is False
    assert parse_production_state("corrupt").raw == {}
    assert parse_production_state({"armed": "true", "dry_run_ready_cycles": "3"}).armed is False
    assert parse_production_state({"armed": 1, "dry_run_ready_cycles": True}).dry_run_ready_cycles == 0
    assert parse_production_state({"dry_run_ready_cycles": -1}).dry_run_ready_cycles == 0
    assert parse_production_state({"dry_run_ready_cycles": 10_001}).dry_run_ready_cycles == 0
    assert parse_production_state({"dry_run_ready_cycles": 42}).dry_run_ready_cycles == 3
    assert parse_production_state(
        {"dry_run_evidence_fingerprint": 123}
    ).dry_run_evidence_fingerprint is None
    assert strict_bool("true") is False
    assert strict_bool("false", default=True) is True
    assert strict_bool(True) is True


def test_control_pause_parser_handles_current_and_legacy_shapes() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)

    assert control_pause_reason(None, now) is None
    assert control_pause_reason("corrupt", now) == "planner_paused"
    assert control_pause_reason({}, now) is None
    assert control_pause_reason({"unrelated": True}, now) is None
    assert control_pause_reason({"active": False, "until": "bad"}, now) is None
    assert control_pause_reason({"active": True, "until": now - timedelta(seconds=1)}, now) is None
    assert control_pause_reason({"active": True}, now) == "planner_paused"
    assert control_pause_reason({"active": "garbage"}, now) == "planner_paused"
    assert control_pause_reason({"active": True, "until": "bad"}, now) == "planner_paused"
    assert control_pause_reason({"reason": "legacy"}, now) == "planner_paused"
    assert control_pause_reason(
        {"until": (now + timedelta(minutes=5)).replace(tzinfo=None), "assets": ["ev"]},
        now,
        asset="ev",
    ) == "ev_control_paused"
    assert control_pause_reason(
        {"active": True, "until": now + timedelta(minutes=5), "assets": ["ev"]},
        now,
        asset="enphase",
    ) is None
    assert control_pause_reason(
        {"active": True, "until": now + timedelta(minutes=5), "assets": {"bad": True}},
        now,
        asset="ev",
    ) == "planner_paused"
