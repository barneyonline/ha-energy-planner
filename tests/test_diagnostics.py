"""Tests for diagnostics payload redaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from custom_components.ha_energy_planner.diagnostics import async_get_config_entry_diagnostics
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
            store=FakeStore({"discovery": {"longitude": 145.1, "status": "ok"}}),
        ),
    )

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(None, entry))

    assert diagnostics["entry"]["data"]["api_token"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["latitude"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["safe"] == "value"
    assert diagnostics["entry"]["options"]["password"] == "**REDACTED**"
    assert diagnostics["store"]["discovery"]["longitude"] == "**REDACTED**"
    assert diagnostics["store"]["discovery"]["status"] == "ok"


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
                    "trip_history": {"location_history": ["home", "work"], "records": [{"distance": 1}]},
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
    assert diagnostics["store"]["trip_history"]["location_history"] == "**REDACTED**"
    assert diagnostics["store"]["trip_history"]["record_count"] == 1


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
        requires_haeo_plan_id="plan-1",
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
        input_issues=["haeo_service_called"],
    )
    entry = FakeEntry(
        data={
            "amber_import_price_entity": "sensor.import",
            "haeo_optimize_service": "haeo.optimize",
            "plain_setting": "ignored",
        },
        options={},
        runtime_data=FakeCoordinator(
            data=plan,
            store=FakeStore(
                {
                    "active_plan": {"plan_id": "plan-1"},
                    "haeo_runs": [
                        {"plan_id": "old", "baseline": {"status": "stale"}},
                        {"plan_id": "plan-1", "baseline": {"status": "ready"}},
                    ],
                    "outcomes": [{"action_id": f"old-{index}"} for index in range(12)],
                    "forecast_snapshots": [{}, {}],
                    "ai_recommendations": [{}],
                }
            ),
        ),
    )

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(None, entry))

    assert diagnostics["entity_mapping"] == {
        "amber_import_price_entity": "sensor.import",
        "haeo_optimize_service": "haeo.optimize",
    }
    assert diagnostics["input_health"] == {
        "health": "healthy",
        "confidence": 0.8,
        "issues": ["haeo_service_called"],
    }
    assert diagnostics["plan"]["summary"] == "test summary"
    assert diagnostics["plan"]["estimated_daily_cost"] == 3.25
    assert diagnostics["plan"]["action_count"] == 1
    assert diagnostics["plan"]["next_action"]["action_id"] == "action-1"
    assert diagnostics["haeo"]["plan_id"] == "plan-1"
    assert diagnostics["store"]["active_plan_present"] is True
    assert diagnostics["store"]["haeo_run_count"] == 2
    assert diagnostics["store"]["forecast_snapshot_count"] == 2
    assert diagnostics["store"]["ai_recommendation_count"] == 1
    assert len(diagnostics["recent_outcomes"]) == 10
    assert diagnostics["recent_outcomes"][0]["action_id"] == "old-2"
