"""Tests for shared fail-closed safety state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.safety import (
    control_pause_reason,
    control_pause_status,
    parse_production_state,
    partition_control_areas_by_pause,
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


def test_control_area_pause_partition_preserves_unpaused_assets_and_fails_closed() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    areas = ["ev", "hvac", "enphase"]

    assert partition_control_areas_by_pause(
        {"active": True, "assets": ["ev"]},
        now,
        areas,
    ) == (["hvac", "enphase"], ["ev"])
    assert partition_control_areas_by_pause("corrupt", now, areas) == ([], areas)
    assert partition_control_areas_by_pause(
        {"active": True, "assets": ["unknown"]},
        now,
        areas,
    ) == ([], areas)
    assert partition_control_areas_by_pause({}, now, ["unknown"]) == ([], ["unknown"])


def test_control_pause_status_reports_expiry_without_losing_audit_fields() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    expired = control_pause_status(
        {
            "active": True,
            "until": (now - timedelta(minutes=1)).isoformat(),
            "reason": "climate_zone_target_unavailable",
            "assets": ["daikin"],
        },
        now,
    )

    assert expired["active"] is False
    assert expired["expired"] is True
    assert expired["reason"] == "climate_zone_target_unavailable"
    assert expired["assets"] == ["daikin"]
    assert control_pause_status("corrupt", now)["active"] is True
    malformed = control_pause_status(
        {"active": True, "until": "invalid", "assets": ["ev"]}, now
    )
    assert malformed["active"] is True
    assert malformed["malformed"] is True
