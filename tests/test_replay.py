"""Tests for sanitized replay fixtures."""

import json
from pathlib import Path

from custom_components.ha_energy_planner.replay import run_replay_file


def test_replay_fixtures_match_expected_outcomes() -> None:
    for fixture_path in sorted(Path("tests/fixtures/replay").glob("*.json")):
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        result = run_replay_file(fixture_path)
        assert [violation.code for violation in result.plan_violations] == fixture.get("expected_plan_violations", [])
        expected_rejected = fixture.get("expected_rejected_action_count", len(result.action_results))
        assert result.rejected_action_count == expected_rejected
        expected_action_violations = fixture.get("expected_action_violations", {})
        actual_action_violations = {
            action.action_id: [violation.code for violation in action.violations] for action in result.action_results
        }
        for action_id, expected_violations in expected_action_violations.items():
            assert actual_action_violations[action_id] == expected_violations
