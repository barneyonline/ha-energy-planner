"""Tests for persistent Store normalization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner import storage as storage_module
from custom_components.ha_energy_planner.models import (
    ActionOutcome,
    EnergyPlan,
    InputHealth,
    OutcomeResult,
    Override,
    PlannerMode,
)
from custom_components.ha_energy_planner.storage import (
    PlannerStore,
    _audit_entry,
    _dry_run_signature,
    _record_timestamp,
    _same_audit_outcome,
    _same_dry_run_comparison,
)


class FakeStore:
    """Minimal Home Assistant Store replacement."""

    loaded: dict[str, Any] | None = None
    saved: dict[str, Any] | None = None
    save_count: int = 0

    def __init__(self, hass: object, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self) -> dict[str, Any] | None:
        return self.loaded

    async def async_save(self, data: dict[str, Any]) -> None:
        FakeStore.saved = data
        FakeStore.save_count += 1


def test_store_load_fills_missing_schema_defaults(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = {"ownership": {"enphase_profile": "AI Optimisation"}}

    store = PlannerStore(object())
    asyncio.run(store.async_load())

    assert store.data["ownership"] == {"enphase_profile": "AI Optimisation"}
    assert store.data["outcomes"] == []
    assert store.data["forecast_snapshots"] == []
    assert store.data["command_rate_limits"] == {}
    assert store.data["active_plan"] is None
    assert store.data["execution_audit"] == []


def test_store_load_repairs_malformed_known_fields_and_preserves_unknown(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = {
        "active_plan": "not-a-plan",
        "outcomes": None,
        "forecast_snapshots": {"bad": "shape"},
        "ownership": None,
        "trip_history": [],
        "future_metadata": {"kept": True},
    }

    store = PlannerStore(object())
    asyncio.run(store.async_load())

    assert store.data["active_plan"] is None
    assert store.data["outcomes"] == []
    assert store.data["forecast_snapshots"] == []
    assert store.data["ownership"] == {}
    assert store.data["trip_history"] == {}
    assert store.data["future_metadata"] == {"kept": True}


def test_store_add_outcome_updates_bounded_execution_audit(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    asyncio.run(
        store.async_add_outcome(
            ActionOutcome(
                action_id="ev-start",
                attempted_at=datetime(2026, 6, 27, tzinfo=UTC),
                result=OutcomeResult.APPLIED,
                reason="input_boolean_turn_on_called",
                pre_state={"input_boolean.ev_start": "off"},
                post_state={"input_boolean.ev_start": "on"},
                plan_id="plan-1",
                asset="ev",
                kind="ev_start",
                service_target="input_boolean.ev_start",
            )
        )
    )

    assert FakeStore.saved is not None
    assert FakeStore.saved["execution_audit"] == [
        {
            "attempted_at": "2026-06-27T00:00:00+00:00",
            "plan_id": "plan-1",
            "action_id": "ev-start",
            "asset": "ev",
            "kind": "ev_start",
            "result": "applied",
            "reason": "input_boolean_turn_on_called",
            "service_target": "input_boolean.ev_start",
            "pre_state": {"input_boolean.ev_start": "off"},
            "post_state": {"input_boolean.ev_start": "on"},
        }
    ]
    assert FakeStore.save_count == 1


def test_store_delay_save_batches_multiple_mutations(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    async def mutate_store() -> None:
        async with store.async_delay_save():
            await store.async_save_ownership({"enphase_profile": "AI Optimisation"})
            await store.async_save_trip_history({"records": [{"soc": 50}]})
            await store.async_save_discovery({"ok": True})

    asyncio.run(mutate_store())

    assert FakeStore.save_count == 1
    assert FakeStore.saved is not None
    assert FakeStore.saved["ownership"] == {"enphase_profile": "AI Optimisation"}
    assert FakeStore.saved["trip_history"] == {"records": [{"soc": 50}]}
    assert FakeStore.saved["discovery"] == {"ok": True}


def test_store_skips_unchanged_setter_writes(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    asyncio.run(store.async_save_ownership({}))

    assert FakeStore.saved is None
    assert FakeStore.save_count == 0


def test_store_persists_command_rate_limits(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    asyncio.run(store.async_save_command_rate_limits({"ev:ev_start": "2026-06-27T00:00:00+00:00"}))

    assert FakeStore.save_count == 1
    assert FakeStore.saved is not None
    assert FakeStore.saved["command_rate_limits"] == {"ev:ev_start": "2026-06-27T00:00:00+00:00"}


def test_store_persists_production_pause_and_dry_run_comparison(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    async def persist() -> None:
        await store.async_add_dry_run_comparison({"plan_id": "plan-1", "created_at": datetime(2026, 6, 27, tzinfo=UTC)})
        await store.async_save_production({"armed": True, "armed_at": datetime(2026, 6, 27, tzinfo=UTC)})
        await store.async_save_control_pause({"active": True, "until": datetime(2026, 6, 27, tzinfo=UTC)})

    asyncio.run(persist())

    assert FakeStore.save_count == 3
    assert FakeStore.saved is not None
    assert store.data["dry_run_comparisons"] == [{"plan_id": "plan-1", "created_at": "2026-06-27T00:00:00+00:00"}]
    assert FakeStore.saved["production"] == {"armed": True, "armed_at": "2026-06-27T00:00:00+00:00"}
    assert FakeStore.saved["control_pause"] == {"active": True, "until": "2026-06-27T00:00:00+00:00"}


def test_store_persists_plan_and_list_backed_records(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())
    now = datetime(2026, 6, 27, tzinfo=UTC)

    async def persist_records() -> None:
        await store.async_save_plan(
            EnergyPlan(
                plan_id="plan-1",
                created_at=now,
                horizon_hours=24,
                interval_minutes=5,
                status="current",
                health=InputHealth.HEALTHY,
                mode=PlannerMode.DRY_RUN,
                summary="summary",
                confidence=1.0,
                estimated_daily_cost=1.23,
                actions=[],
                preview=[],
            )
        )
        await store.async_save_overrides(
            [Override(kind="manual_hvac", source="test", expires_at=now + timedelta(minutes=5), reason="testing")]
        )
        await store.async_add_forecast_snapshot({"plan_id": "plan-1"})
        await store.async_save_forecast_calibration({"pv_forecast_kw": {"factor": 1.1}})
        await store.async_add_haeo_run({"plan_id": "plan-1"})
        await store.async_add_ai_recommendation({"plan_id": "plan-1"})
        await store.async_save_thermal_model({"last_sample": {"temperature": 20}})
        await store.async_clear_ownership()

    asyncio.run(persist_records())

    assert FakeStore.save_count == 7
    assert store.data["active_plan"]["plan_id"] == "plan-1"
    assert store.data["overrides"][0]["reason"] == "testing"
    assert store.data["forecast_snapshots"] == [{"plan_id": "plan-1"}]
    assert store.data["forecast_calibration"] == {"pv_forecast_kw": {"factor": 1.1}}
    assert store.data["haeo_runs"] == [{"plan_id": "plan-1"}]
    assert store.data["ai_recommendations"] == [{"plan_id": "plan-1"}]
    assert store.data["thermal_model"] == {"last_sample": {"temperature": 20}}


def test_store_delay_save_without_mutations_does_not_write(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    async def no_mutation() -> None:
        async with store.async_delay_save():
            pass

    asyncio.run(no_mutation())

    assert FakeStore.save_count == 0


def test_forecast_snapshot_retention_covers_day_ahead_training_at_five_minutes(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    store.data["forecast_snapshots"] = [{"index": index} for index in range(384)]

    asyncio.run(store.async_add_forecast_snapshot({"index": 384}))

    assert len(store.data["forecast_snapshots"]) == 385
    assert store.data["forecast_snapshots"][0] == {"index": 0}
    assert store.data["forecast_snapshots"][-1] == {"index": 384}


def test_background_ai_metadata_attaches_to_matching_forecast_snapshot(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    store.data["forecast_snapshots"] = [
        {"plan_id": "current", "ai": None},
        {"plan_id": "newer", "ai": None},
    ]

    asyncio.run(
        store.async_attach_ai_to_forecast_snapshot(
            "current",
            {"status": "accepted", "accepted_fields": ["confidence"]},
        )
    )

    assert store.data["forecast_snapshots"] == [
        {
            "plan_id": "current",
            "ai": {"status": "accepted", "accepted_fields": ["confidence"]},
        },
        {"plan_id": "newer", "ai": None},
    ]


def test_store_audit_entry_bounds_mapping_values(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())
    pre_state = {f"key-{index}": index for index in range(20)}

    asyncio.run(
        store.async_add_outcome(
            ActionOutcome(
                action_id="ev-start",
                attempted_at=datetime(2026, 6, 27, tzinfo=UTC),
                result=OutcomeResult.APPLIED,
                reason="ok",
                pre_state=pre_state,
                post_state=[],
                plan_id="plan-1",
            )
        )
    )

    audit = store.data["execution_audit"][0]
    assert len(audit["pre_state"]) == 12
    assert audit["post_state"] == {}


def test_store_coalesces_materially_identical_dry_run_outcomes(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    first_at = datetime(2026, 6, 27, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=1)

    async def add_outcomes() -> None:
        for action_id, plan_id, attempted_at in (
            ("generated-action-1", "generated-plan-1", first_at),
            ("generated-action-2", "generated-plan-2", second_at),
        ):
            await store.async_add_outcome(
                ActionOutcome(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    result=OutcomeResult.SKIPPED,
                    reason="dry_run",
                    pre_state={},
                    post_state={},
                    plan_id=plan_id,
                    asset="enphase",
                    kind="restore_ai",
                )
            )

    asyncio.run(add_outcomes())

    assert len(store.data["execution_audit"]) == 1
    assert store.data["execution_audit"][0]["occurrence_count"] == 2
    assert store.data["execution_audit"][0]["last_attempted_at"] == second_at.isoformat()
    assert len(store.data["outcomes"]) == 1
    assert store.data["outcomes"][0]["occurrence_count"] == 2
    assert store.data["outcomes"][0]["last_attempted_at"] == second_at.isoformat()


def test_store_does_not_coalesce_applied_outcomes(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    now = datetime(2026, 6, 27, tzinfo=UTC)
    outcome = ActionOutcome("action", now, OutcomeResult.APPLIED, "ok", {}, {}, "plan", asset="ev", kind="ev_start")

    asyncio.run(store.async_add_outcome(outcome))
    asyncio.run(store.async_add_outcome(outcome))

    assert len(store.data["execution_audit"]) == 2
    assert len(store.data["outcomes"]) == 2


def test_time_based_retention_preserves_recent_evidence_across_bursts(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    now = datetime(2026, 6, 30, tzinfo=UTC)
    store.data["forecast_snapshots"] = [
        "malformed",
        {"created_at": (now - timedelta(hours=49)).isoformat(), "plan_id": "expired"},
        *[{"created_at": (now - timedelta(hours=24)).isoformat(), "plan_id": f"burst-{index}"} for index in range(500)],
    ]

    asyncio.run(store.async_add_forecast_snapshot({"created_at": now, "plan_id": "latest"}))
    store.data["haeo_runs"] = [
        {"created_at": (now - timedelta(hours=49)).isoformat(), "plan_id": "expired"},
        {"created_at": (now - timedelta(hours=1)).isoformat(), "plan_id": "recent"},
    ]
    asyncio.run(store.async_add_haeo_run({"created_at": now, "plan_id": "latest"}))
    store.data["dry_run_comparisons"] = [
        {"created_at": (now - timedelta(days=8)).isoformat(), "planned_action_count": 1},
        {"created_at": (now - timedelta(days=1)).isoformat(), "planned_action_count": 2},
    ]
    asyncio.run(store.async_add_dry_run_comparison({"created_at": now, "planned_action_count": 3, "next_action": None}))

    assert len(store.data["forecast_snapshots"]) == 501
    assert all(item["plan_id"] != "expired" for item in store.data["forecast_snapshots"])
    assert all(isinstance(item, dict) for item in store.data["forecast_snapshots"])
    assert [item["plan_id"] for item in store.data["haeo_runs"]] == ["recent", "latest"]
    assert [item["planned_action_count"] for item in store.data["dry_run_comparisons"]] == [2, 3]


def test_store_coalesces_dry_run_comparisons_ignoring_generated_metadata(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    base_action = {
        "asset": "ev",
        "kind": "ev_start",
        "desired_state": {"enabled": True},
        "reason_codes": ["cheap_price"],
    }

    async def add_comparisons() -> None:
        await store.async_add_dry_run_comparison(
            {
                "created_at": "first",
                "plan_id": "plan-1",
                "planned_action_count": 1,
                "next_action": {**base_action, "action_id": "a-1"},
            }
        )
        await store.async_add_dry_run_comparison(
            {
                "created_at": "second",
                "plan_id": "plan-2",
                "planned_action_count": 1,
                "next_action": {**base_action, "action_id": "a-2"},
            }
        )

    asyncio.run(add_comparisons())

    assert len(store.data["dry_run_comparisons"]) == 1
    assert store.data["dry_run_comparisons"][0]["occurrence_count"] == 2


def test_audit_dedup_helpers_handle_malformed_and_sparse_records() -> None:
    assert _same_audit_outcome("bad", {}) is False
    assert _same_dry_run_comparison({}, "bad") is False
    assert _dry_run_signature(
        {"next_action": "unknown", "recent_outcomes": ["bad", {"asset": "ev", "result": "skipped"}]}
    ) == {
        "planned_action_count": None,
        "next_action": "unknown",
        "estimated_daily_cost": None,
        "recent_outcomes": [
            {
                "asset": "ev",
                "kind": None,
                "desired_state": None,
                "result": "skipped",
                "reason": None,
                "service_target": None,
                "pre_state": None,
                "post_state": None,
            }
        ],
    }
    outcome = ActionOutcome(
        action_id="ev",
        attempted_at=datetime(2026, 6, 27, tzinfo=UTC),
        result=OutcomeResult.SKIPPED,
        reason="dry_run",
        pre_state={},
        post_state={},
        plan_id="plan",
        desired_state={"target_soc_percent": 80},
    )
    assert _audit_entry(outcome)["desired_state"] == {"target_soc_percent": 80}
    naive = datetime(2026, 6, 27)
    assert _record_timestamp({"created_at": naive}) == naive.replace(tzinfo=UTC)
    assert _record_timestamp({"created_at": "bad"}) is None
    assert _record_timestamp("bad") is None
