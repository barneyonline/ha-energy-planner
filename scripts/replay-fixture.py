#!/usr/bin/env python3
"""Run one or more sanitized HA Energy Planner replay fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.ha_energy_planner.replay import run_replay_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    for fixture in args.fixtures:
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        result = run_replay_file(fixture)
        print(json.dumps(result.to_summary(), indent=2, sort_keys=True))
        expected_plan_violations = fixture_data.get("expected_plan_violations", [])
        expected_rejected = fixture_data.get("expected_rejected_action_count")
        if expected_rejected is None:
            expected_rejected = len(result.action_results)
        if [violation.code for violation in result.plan_violations] != expected_plan_violations:
            failed = True
        if result.rejected_action_count != expected_rejected:
            failed = True
        expected_action_violations = fixture_data.get("expected_action_violations", {})
        actual_action_violations = {
            action.action_id: [violation.code for violation in action.violations] for action in result.action_results
        }
        for action_id, expected_violations in expected_action_violations.items():
            if actual_action_violations.get(action_id) != expected_violations:
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
