"""Tests for diagnostics payload redaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from custom_components.ha_energy_planner.diagnostics import _load_forecast_summary, async_get_config_entry_diagnostics
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    EnergyPlan,
    InputHealth,
    PlanAction,
    PlannerMode,
)


@dataclass(slots=True)
class FakeStore:
    """Minimal store wrapper."""

    data: dict[str, Any]


@dataclass(slots=True)
class FakeCoordinator:
    """Minimal coordinator shape used by diagnostics."""

    data: EnergyPlan
    store: FakeStore
    last_refresh_metadata: dict[str, Any] | None = None
    refresh_metrics: dict[str, Any] | None = None
    automatic_control_requested: bool = False
    active_control: bool = False
    effective_control: bool = False


@dataclass(slots=True)
class FakeEntry:
    """Minimal config entry shape used by diagnostics."""

    data: dict[str, Any]
    options: dict[str, Any]
    runtime_data: FakeCoordinator


def test_diagnostics_redacts_sensitive_keys() -> None:
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=datetime(2026, 6, 27, tzinfo=UTC),
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    entry = FakeEntry(
        data={"api_token": "secret-value", "latitude": -37.8, "safe": "value"},
        options={"password": "hidden", "dry_run": True},
        runtime_data=FakeCoordinator(
            data=plan,
            store=FakeStore(
                {
                    "discovery": {"longitude": 145.1, "status": "ok"},
                    "haeo_runs": [{"baseline": {"status": "stale"}}],
                    "production": {
                        "startup_auto_recovery": {
                            "status": "validating",
                            "successful_runs": 2,
                        }
                    },
                }
            ),
        ),
    )

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(None, entry))

    assert diagnostics["entry"]["data"]["api_token"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["latitude"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["safe"] == "value"
    assert diagnostics["entry"]["options"]["password"] == "**REDACTED**"
    assert diagnostics["store"]["discovery"]["longitude"] == "**REDACTED**"
    assert diagnostics["store"]["discovery"]["status"] == "ok"
    assert diagnostics["startup_auto_recovery"] == {"status": "validating", "successful_runs": 2}
    assert diagnostics["automatic_control"] == {"requested": False, "running": False}


def test_diagnostics_redacts_prompts_addresses_and_raw_model_payloads() -> None:
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=datetime(2026, 6, 27, tzinfo=UTC),
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
    entry = FakeEntry(
        data={"home_address": "1 Secret St", "safe": "value"},
        options={"access_token": "token-value"},
        runtime_data=FakeCoordinator(
            data=plan,
            store=FakeStore(
                {
                    "discovery": {"raw_prompt": "full prompt"},
                    "outcomes": [{"raw_response": {"text": "model output"}}],
                    "ev_charge_calibration": {
                        "status": "ready",
                        "soc_per_kwh": 1.8,
                        "samples": [{"soc_gain_percent": 14}],
                    },
                }
            ),
        ),
    )

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(None, entry))

    assert diagnostics["entry"]["data"]["home_address"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["safe"] == "value"
    assert diagnostics["entry"]["options"]["access_token"] == "**REDACTED**"
    assert diagnostics["store"]["discovery"]["raw_prompt"] == "**REDACTED**"
    assert diagnostics["recent_outcomes"][0]["raw_response"] == "**REDACTED**"
    assert diagnostics["store"]["ev_charge_calibration"]["status"] == "ready"
    assert diagnostics["store"]["ev_charge_calibration"]["soc_per_kwh"] == 1.8
    assert "samples" not in diagnostics["store"]["ev_charge_calibration"]


def test_diagnostics_exposes_compact_operational_metadata() -> None:
    action = PlanAction(
        action_id="action-1",
        plan_id="plan-1",
        execute_not_before=datetime(2026, 6, 27, tzinfo=UTC),
        execute_not_after=datetime(2026, 6, 27, 0, 5, tzinfo=UTC),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"target_soc_percent": 80},
        hard_constraints=["ev_min_soc"],
        reason_codes=["ev_soc_below_target"],
        expected_cost_delta=None,
        confidence=0.8,
    )
    plan = EnergyPlan(
        plan_id="plan-1",
        created_at=datetime(2026, 6, 27, tzinfo=UTC),
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="test summary",
        confidence=0.8,
        estimated_daily_cost=3.25,
        actions=[action],
        preview=[],
        input_issues=["ev_soc_missing"],
    )
    entry = FakeEntry(
        data={
            "amber_import_price_entity": "sensor.import",
            "haeo_optimize_service": "retired.value",
            "plain_setting": "ignored",
        },
        options={},
        runtime_data=FakeCoordinator(
            data=plan,
            store=FakeStore(
                {
                    "active_plan": {"plan_id": "plan-1"},
                    "outcomes": [{"action_id": f"old-{index}"} for index in range(12)],
                    "forecast_snapshots": [{}, {}],
                    "ai_recommendations": [{}],
                    "built_in_load_forecast": {
                        "status": "ready",
                        "source_entity_id": "sensor.house_load",
                        "profiles": {"weekday": [1.0]},
                    },
                }
            ),
            last_refresh_metadata={"duration_ms": 25.0},
            refresh_metrics={
                "refreshes_per_hour": 12.0,
                "trigger_counts": {"boundary": 10, "state": 2},
                "skipped_count": 3,
                "coalesced_count": 4,
            },
            automatic_control_requested=True,
            active_control=True,
            effective_control=False,
        ),
    )

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(None, entry))

    assert diagnostics["entity_mapping"] == {
        "amber_import_price_entity": "sensor.import",
    }
    assert diagnostics["input_health"] == {
        "health": "healthy",
        "confidence": 0.8,
        "issues": ["ev_soc_missing"],
    }
    assert diagnostics["plan"]["summary"] == "test summary"
    assert diagnostics["plan"]["estimated_daily_cost"] == 3.25
    assert diagnostics["plan"]["action_count"] == 1
    assert diagnostics["plan"]["next_action"]["action_id"] == "action-1"
    assert diagnostics["plan"]["next_action"]["desired_state"] == {"target_soc_percent": 80}
    assert diagnostics["refresh_performance"] == {
        "refreshes_per_hour": 12.0,
        "trigger_counts": {"boundary": 10, "state": 2},
        "skipped_count": 3,
        "coalesced_count": 4,
        "latest": {"duration_ms": 25.0},
    }
    assert diagnostics["store"]["active_plan_present"] is True
    assert diagnostics["store"]["forecast_snapshot_count"] == 2
    assert diagnostics["store"]["ai_recommendation_count"] == 1
    assert diagnostics["store"]["built_in_load_forecast"] == {
        "status": "ready",
        "source_entity_id": "sensor.house_load",
    }
    assert diagnostics["automatic_control"] == {"requested": True, "running": False}
    assert len(diagnostics["recent_outcomes"]) == 10
    assert diagnostics["recent_outcomes"][0]["action_id"] == "old-2"
    assert _load_forecast_summary([]) == {}
