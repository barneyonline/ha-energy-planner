"""Rolling allowance boundaries and conservative legacy accounting."""
from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.action_limits import (
    action_budget,
    attempt_time,
    budget_history,
    counted_attempt,
    recent_attempts,
)

NOW = datetime(2026, 9, 20, tzinfo=UTC)


def test_budgets_exclude_noops_and_releases_and_expire_individually():
    rows = [dict(asset="daikin", kind="set_hvac", result="applied", reason="hvac_action_applied",
                 attempted_at=(NOW - timedelta(hours=i)).isoformat()) for i in (1, 2, 3)]
    rows += [{**rows[0], "result": "skipped"}, {**rows[0], "kind": "release_hvac"},
             {**rows[0], "reason": "already_in_desired_hvac_state"},
             {**rows[0], "attempted_at": (NOW - timedelta(hours=24)).isoformat()},
             {**rows[0], "attempted_at": (NOW + timedelta(seconds=1)).isoformat()}, None]
    budget = action_budget(rows, {"max_daily_climate_actions": 2}, NOW, "daikin")
    assert budget["used"] == 3 and budget["remaining"] == 0
    assert budget["next_action_expires_at"] == (NOW + timedelta(hours=21)).isoformat()
    assert budget["allowance_available_at"] == (NOW + timedelta(hours=22)).isoformat()
    assert action_budget(
        recent_attempts(rows, NOW), {"max_daily_climate_actions": 2}, NOW + timedelta(hours=22), "daikin",
    )["remaining"] == 1
    assert action_budget(rows, {}, NOW, "daikin")["remaining"] is None
    assert action_budget([], {}, NOW, "ev")["next_action_expires_at"] is None
    assert recent_attempts(None, NOW) == []
    assert attempt_time({"attempted_at": "2026-09-20T00:00:00"}) == NOW
    for value in [{}, {"attempted_at": "bad"}, {"attempted_at": None}]:
        assert attempt_time(value) is None
    assert counted_attempt({"asset": "ev", "result": "failed"})
    assert budget_history({"execution_audit": rows}) is rows
    assert budget_history({"action_attempts": [], "execution_audit": rows}) == []


def test_invalid_audit_and_other_assets():
    assert budget_history({"execution_audit": None}) == []
    assert recent_attempts([{"asset": "ev", "result": "failed"}], NOW) == []
    rows = [{"asset": "enphase", "result": "failed", "attempted_at": NOW.isoformat()}]
    assert action_budget(rows, {"max_daily_enphase_actions": 2}, NOW, "enphase")["remaining"] == 1
    assert action_budget(rows, {}, NOW, "ev")["used"] == 0
