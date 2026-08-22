"""Tests for persistent Store normalization."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

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
    created_keys: list[str] = []
    created_kwargs: list[dict[str, Any]] = []
    loaded_by_key: dict[str, dict[str, Any] | None] | None = None
    saved_by_key: dict[str, dict[str, Any]] = {}

    def __init__(self, hass: object, version: int, key: str, **kwargs: Any) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.kwargs = kwargs
        FakeStore.created_keys.append(key)
        FakeStore.created_kwargs.append(kwargs)

    async def async_load(self) -> dict[str, Any] | None:
        if self.loaded_by_key is not None:
            return self.loaded_by_key.get(self.key)
        return self.loaded

    async def async_save(self, data: dict[str, Any]) -> None:
        FakeStore.saved = data
        FakeStore.saved_by_key[self.key] = data
        FakeStore.save_count += 1


def test_store_namespaces_state_per_config_entry_and_supports_legacy_fallback(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.created_keys = []
    FakeStore.created_kwargs = []

    PlannerStore(object(), "vehicle-one", legacy_fallback=True)
    PlannerStore(object(), "vehicle-two")

    assert FakeStore.created_keys == [
        "ha_energy_planner_state_vehicle-one",
        "ha_energy_planner_state",
        "ha_energy_planner_state_vehicle-two",
    ]
    assert FakeStore.created_kwargs == [
        {"serialize_in_event_loop": False},
        {"serialize_in_event_loop": False},
        {"serialize_in_event_loop": False},
    ]


def test_store_persists_ev_grid_reservation_high_watermark(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.saved = None
    store = PlannerStore(object(), "vehicle-one")

    asyncio.run(
        store.async_save_ev_grid_reservation(
            {
                "active": True,
                "load_kw": 11.0,
                "limit_kw": 10.0,
                "reserved_at": "2026-07-26T00:00:00+00:00",
            }
        )
    )

    assert store.data["ev_grid_reservation"]["load_kw"] == 11.0


def test_store_imports_legacy_state_into_first_entry_namespace(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    monkeypatch.setattr(
        FakeStore,
        "loaded_by_key",
        {
            "ha_energy_planner_state_vehicle-one": None,
            "ha_energy_planner_state": {"ownership": {"ev": {"switch.ev": "off"}}},
        },
    )
    FakeStore.saved = None
    FakeStore.saved_by_key = {}
    store = PlannerStore(object(), "vehicle-one", legacy_fallback=True)

    asyncio.run(store.async_load())

    assert store.data["ownership"] == {"ev": {"switch.ev": "off"}}
    assert FakeStore.saved_by_key["ha_energy_planner_state_vehicle-one"]["ownership"] == {
        "ev": {"switch.ev": "off"}
    }
    assert FakeStore.saved_by_key["ha_energy_planner_state"]["_entry_store_migrated_to"] == "vehicle-one"


def test_store_does_not_reimport_marked_legacy_state(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    monkeypatch.setattr(
        FakeStore,
        "loaded_by_key",
        {
            "ha_energy_planner_state_vehicle-one": None,
            "ha_energy_planner_state": {
                "ownership": {"ev": {"switch.ev": "off"}},
                "_entry_store_migrated_to": "vehicle-one",
            },
        },
    )
    FakeStore.saved_by_key = {}
    store = PlannerStore(object(), "vehicle-one", legacy_fallback=True)

    asyncio.run(store.async_load())

    assert store.data["ownership"] == {}
    assert FakeStore.saved_by_key == {}


def test_store_load_fills_missing_schema_defaults(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = {"ownership": {"enphase_profile": "AI Optimisation"}}

    store = PlannerStore(object())
    asyncio.run(store.async_load())

    assert store.data["ownership"] == {"enphase_profile": "AI Optimisation"}
    assert store.data["outcomes"] == []
    assert store.data["forecast_snapshots"] == []
    assert store.data["command_rate_limits"] == {}
    assert store.data["built_in_load_forecast"] == {}
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
        "built_in_load_forecast": [],
        "future_metadata": {"kept": True},
    }

    store = PlannerStore(object())
    asyncio.run(store.async_load())

    assert store.data["active_plan"] is None
    assert store.data["outcomes"] == []
    assert store.data["forecast_snapshots"] == []
    assert store.data["ownership"] == {}
    assert store.data["trip_history"] == {}
    assert store.data["built_in_load_forecast"] == {}
    assert store.data["future_metadata"] == {"kept": True}


def test_store_load_persists_retired_optimizer_cleanup(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = {
        "haeo_runs": [{"status": "failed"}],
        "active_plan": {
            "plan_id": "plan-1",
            "actions": [
                "malformed-action",
                {
                    "action_id": "action-1",
                    "requires_haeo_plan_id": "optimizer-plan",
                }
            ],
        },
    }
    FakeStore.saved = None

    store = PlannerStore(object())
    asyncio.run(store.async_load())

    assert "haeo_runs" not in store.data
    assert store.data["active_plan"]["actions"] == ["malformed-action", {"action_id": "action-1"}]
    assert FakeStore.saved == store.data


def test_store_persists_compact_builtin_load_model_only_when_changed(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())
    model = {
        "model_version": 1,
        "source_entity_id": "sensor.house_load",
        "profiles": {"weekday": {"expected": [1.0]}},
    }

    asyncio.run(store.async_save_builtin_load_forecast(model))
    asyncio.run(store.async_save_builtin_load_forecast(model))

    assert store.data["built_in_load_forecast"] == model
    assert FakeStore.save_count == 1


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


def test_store_forced_flush_persists_inside_delay_context(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    async def mutate_store() -> None:
        async with store.async_delay_save():
            await store.async_save_ownership({"ev": "provisional"})
            assert FakeStore.save_count == 0

            await store.async_flush()

            assert FakeStore.save_count == 1
            assert FakeStore.saved is not None
            assert FakeStore.saved["ownership"] == {"ev": "provisional"}
            await store.async_save_trip_history({"records": [{"soc": 50}]})
            assert FakeStore.save_count == 1

    asyncio.run(mutate_store())

    assert FakeStore.save_count == 2
    assert FakeStore.saved is not None
    assert FakeStore.saved["trip_history"] == {"records": [{"soc": 50}]}


def test_store_skips_unchanged_setter_writes(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    FakeStore.loaded = None
    FakeStore.saved = None
    FakeStore.save_count = 0
    store = PlannerStore(object())

    asyncio.run(store.async_save_ownership({}))

    assert FakeStore.saved is None
    assert FakeStore.save_count == 0


def test_store_retries_unchanged_value_after_transient_save_failure(monkeypatch: object) -> None:
    class FailsOnceStore(FakeStore):
        attempts = 0

        async def async_save(self, data: dict[str, Any]) -> None:
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise OSError("temporary storage failure")
            await super().async_save(data)

    monkeypatch.setattr(storage_module, "Store", FailsOnceStore)
    FailsOnceStore.attempts = 0
    FakeStore.save_count = 0
    store = PlannerStore(object())

    with pytest.raises(OSError, match="temporary storage failure"):
        asyncio.run(store.async_save_ownership({"ev": "owned"}))
    asyncio.run(store.async_save_ownership({"ev": "owned"}))

    assert FailsOnceStore.attempts == 2
    assert FakeStore.save_count == 1
    assert FakeStore.saved == store.data


def test_store_delayed_save_keeps_dirty_marker_after_failure(monkeypatch: object) -> None:
    class FailsOnceStore(FakeStore):
        attempts = 0

        async def async_save(self, data: dict[str, Any]) -> None:
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise OSError("temporary storage failure")
            await super().async_save(data)

    monkeypatch.setattr(storage_module, "Store", FailsOnceStore)
    FailsOnceStore.attempts = 0
    FakeStore.save_count = 0
    store = PlannerStore(object())

    async def fail_delayed_save() -> None:
        async with store.async_delay_save():
            await store.async_save_ownership({"ev": "owned"})

    with pytest.raises(OSError, match="temporary storage failure"):
        asyncio.run(fail_delayed_save())
    asyncio.run(store.async_save_ownership({"ev": "owned"}))

    assert FailsOnceStore.attempts == 2
    assert FakeStore.save_count == 1


def test_store_flushes_mutation_that_arrives_during_inflight_save(monkeypatch: object) -> None:
    class BarrierStore(FakeStore):
        started: asyncio.Event
        release: asyncio.Event
        snapshots: list[dict[str, Any]]

        async def async_save(self, data: dict[str, Any]) -> None:
            if not self.snapshots:
                self.started.set()
                await self.release.wait()
            # Home Assistant may serialize this mapping in an executor after
            # the event loop has accepted another mutation.
            self.snapshots.append(deepcopy(data))

    monkeypatch.setattr(storage_module, "Store", BarrierStore)

    async def save_concurrently() -> list[dict[str, Any]]:
        BarrierStore.started = asyncio.Event()
        BarrierStore.release = asyncio.Event()
        BarrierStore.snapshots = []
        store = PlannerStore(object())
        first = asyncio.create_task(store.async_save_ownership({"ev": "owned"}))
        await BarrierStore.started.wait()
        second = asyncio.create_task(store.async_save_trip_history({"records": [{"soc": 50}]}))
        await asyncio.sleep(0)
        BarrierStore.release.set()
        await asyncio.gather(first, second)
        return BarrierStore.snapshots

    snapshots = asyncio.run(save_concurrently())

    assert len(snapshots) == 2
    assert snapshots[0]["trip_history"] == {}
    assert snapshots[-1]["ownership"] == {"ev": "owned"}
    assert snapshots[-1]["trip_history"] == {"records": [{"soc": 50}]}


def test_store_concurrent_failure_retains_later_generation_for_retry(monkeypatch: object) -> None:
    class BarrierFailsSecondStore(FakeStore):
        started: asyncio.Event
        release: asyncio.Event
        attempts: int
        snapshots: list[dict[str, Any]]

        async def async_save(self, data: dict[str, Any]) -> None:
            type(self).attempts += 1
            attempt = type(self).attempts
            snapshot = deepcopy(data)
            if attempt == 1:
                self.started.set()
                await self.release.wait()
            if attempt == 2:
                raise OSError("temporary concurrent failure")
            self.snapshots.append(snapshot)

    monkeypatch.setattr(storage_module, "Store", BarrierFailsSecondStore)

    async def save_concurrently() -> tuple[list[object], list[dict[str, Any]], int]:
        BarrierFailsSecondStore.started = asyncio.Event()
        BarrierFailsSecondStore.release = asyncio.Event()
        BarrierFailsSecondStore.attempts = 0
        BarrierFailsSecondStore.snapshots = []
        store = PlannerStore(object())
        first = asyncio.create_task(store.async_save_ownership({"ev": "owned"}))
        await BarrierFailsSecondStore.started.wait()
        second = asyncio.create_task(
            store.async_save_trip_history({"records": [{"soc": 50}]})
        )
        await asyncio.sleep(0)
        BarrierFailsSecondStore.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        return results, BarrierFailsSecondStore.snapshots, BarrierFailsSecondStore.attempts

    results, snapshots, attempts = asyncio.run(save_concurrently())

    assert sum(isinstance(result, OSError) for result in results) == 1
    assert attempts == 3
    assert snapshots[-1]["ownership"] == {"ev": "owned"}
    assert snapshots[-1]["trip_history"] == {"records": [{"soc": 50}]}


def test_store_defers_inflight_flush_while_delay_context_is_active(
    monkeypatch: object,
) -> None:
    class BarrierStore(FakeStore):
        started: asyncio.Event
        release_first: asyncio.Event
        delayed_mutation_ready: asyncio.Event
        release_delay: asyncio.Event
        snapshots: list[dict[str, Any]]

        async def async_save(self, data: dict[str, Any]) -> None:
            snapshot = deepcopy(data)
            if not self.snapshots:
                self.started.set()
                await self.release_first.wait()
            self.snapshots.append(snapshot)

    monkeypatch.setattr(storage_module, "Store", BarrierStore)

    async def save_with_overlapping_delay() -> list[dict[str, Any]]:
        BarrierStore.started = asyncio.Event()
        BarrierStore.release_first = asyncio.Event()
        BarrierStore.delayed_mutation_ready = asyncio.Event()
        BarrierStore.release_delay = asyncio.Event()
        BarrierStore.snapshots = []
        store = PlannerStore(object())

        first = asyncio.create_task(store.async_save_ownership({"ev": "owned"}))
        await BarrierStore.started.wait()

        async def delayed_writer() -> None:
            async with store.async_delay_save():
                await store.async_save_trip_history({"records": [{"soc": 50}]})
                BarrierStore.delayed_mutation_ready.set()
                await BarrierStore.release_delay.wait()

        second = asyncio.create_task(delayed_writer())
        await BarrierStore.delayed_mutation_ready.wait()
        BarrierStore.release_first.set()
        await first
        BarrierStore.release_delay.set()
        await second
        return BarrierStore.snapshots

    snapshots = asyncio.run(save_with_overlapping_delay())

    assert len(snapshots) == 2
    assert snapshots[0]["trip_history"] == {}
    assert snapshots[1]["trip_history"] == {"records": [{"soc": 50}]}


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
        await store.async_add_ai_recommendation({"plan_id": "plan-1"})
        await store.async_save_thermal_model({"last_sample": {"temperature": 20}})
        await store.async_clear_ownership()

    asyncio.run(persist_records())

    assert FakeStore.save_count == 6
    assert store.data["active_plan"]["plan_id"] == "plan-1"
    assert store.data["overrides"][0]["reason"] == "testing"
    assert store.data["forecast_snapshots"] == [{"plan_id": "plan-1"}]
    assert store.data["forecast_calibration"] == {"pv_forecast_kw": {"factor": 1.1}}
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


def test_forecast_snapshot_retention_is_defensively_bounded(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    store.data["forecast_snapshots"] = [{"index": index} for index in range(384)]

    asyncio.run(store.async_add_forecast_snapshot({"index": 384}))

    assert len(store.data["forecast_snapshots"]) == 128
    assert store.data["forecast_snapshots"][0] == {"index": 257}
    assert store.data["forecast_snapshots"][-1] == {"index": 384}


def test_forecast_snapshots_keep_one_latest_record_per_half_hour(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    start = datetime(2026, 6, 27, tzinfo=UTC)

    async def add_snapshots() -> None:
        for index in range(24):
            created_at = start + timedelta(minutes=5 * index)
            await store.async_add_forecast_snapshot(
                {"created_at": created_at, "plan_id": f"plan-{index}"}
            )

    asyncio.run(add_snapshots())

    assert [item["plan_id"] for item in store.data["forecast_snapshots"]] == [
        "plan-5",
        "plan-11",
        "plan-17",
        "plan-23",
    ]


def test_background_ai_metadata_attaches_to_matching_forecast_snapshot(monkeypatch: object) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    store.data["forecast_snapshots"] = [
        {"plan_id": "current", "ai": None},
        {"plan_id": "newer", "ai": None},
        "malformed",
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
            "ai_plan_id": "current",
        },
        {"plan_id": "newer", "ai": None},
        "malformed",
    ]


def test_bucketed_snapshot_preserves_delayed_ai_metadata_and_plan_provenance(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    start = datetime(2026, 6, 27, tzinfo=UTC)

    async def replace_then_attach() -> None:
        await store.async_add_forecast_snapshot(
            {"created_at": start, "plan_id": "plan-0", "ai": None}
        )
        await store.async_add_forecast_snapshot(
            {"created_at": start + timedelta(minutes=5), "plan_id": "plan-1", "ai": None}
        )
        await store.async_attach_ai_to_forecast_snapshot(
            "plan-0",
            {"status": "accepted"},
        )
        await store.async_add_forecast_snapshot(
            {"created_at": start + timedelta(minutes=10), "plan_id": "plan-2", "ai": None}
        )

    asyncio.run(replace_then_attach())

    assert store.data["forecast_snapshots"] == [
        {
            "created_at": "2026-06-27T00:10:00+00:00",
            "plan_id": "plan-2",
            "ai": {"status": "accepted"},
            "bucket_plan_ids": ["plan-0", "plan-1"],
            "ai_plan_id": "plan-0",
        }
    ]


def test_bucketed_snapshot_retains_distinct_action_evidence_without_duplicates(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    start = datetime(2026, 6, 27, tzinfo=UTC)
    active = {
        "action_id": "plan-0-ev-native-smart-charge",
        "asset": "ev",
        "kind": "ev_schedule",
        "desired_state": {"charging_required_now": True, "allocated_slots": [{"price": -0.1}]},
        "reason_codes": ["negative_price"],
    }
    refreshed_active = {**active, "action_id": "plan-1-ev-native-smart-charge"}
    inactive = {
        **active,
        "action_id": "plan-2-ev-native-smart-charge",
        "desired_state": {"charging_required_now": False, "allocated_slots": []},
        "reason_codes": ["already_at_target"],
    }

    async def add_snapshots() -> None:
        for index, actions in enumerate(([active, "malformed"], [refreshed_active], [inactive])):
            await store.async_add_forecast_snapshot(
                {
                    "created_at": start + timedelta(minutes=5 * index),
                    "plan_id": f"plan-{index}",
                    "actions": actions,
                }
            )

    asyncio.run(add_snapshots())

    actions = store.data["forecast_snapshots"][0]["actions"]
    assert [action["action_id"] for action in actions] == [
        "plan-1-ev-native-smart-charge",
        "plan-2-ev-native-smart-charge",
    ]


def test_bucketed_snapshot_prioritizes_negative_price_ev_action_at_cap(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    start = datetime(2026, 6, 27, tzinfo=UTC)

    async def add_snapshots() -> None:
        for index in range(13):
            negative_price = index == 0
            action = {
                "action_id": f"plan-{index}-ev-native-smart-charge",
                "asset": "ev",
                "kind": "ev_schedule",
                "desired_state": (
                    "malformed"
                    if index == 1
                    else {"variant": index}
                    if index == 2
                    else {
                        "charging_required_now": negative_price,
                        "variant": index,
                        "allocated_slots": (
                            [{"import_price": -0.05, "starts_at": "negative"}]
                            if negative_price
                            else []
                        ),
                    }
                ),
            }
            await store.async_add_forecast_snapshot(
                {
                    "created_at": start + timedelta(minutes=index),
                    "plan_id": f"plan-{index}",
                    "actions": [action],
                }
            )

    asyncio.run(add_snapshots())

    actions = store.data["forecast_snapshots"][0]["actions"]
    assert len(actions) == 12
    assert any(
        isinstance(action.get("desired_state"), dict)
        and action["desired_state"].get("allocated_slots")
        and action["desired_state"]["allocated_slots"][0]["import_price"] < 0
        for action in actions
    )


def test_bucketed_snapshot_preserves_successful_recorder_import_summary(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(storage_module, "Store", FakeStore)
    store = PlannerStore(object())
    start = datetime(2026, 6, 27, tzinfo=UTC)

    async def add_snapshots() -> None:
        await store.async_add_forecast_snapshot(
            {
                "created_at": start,
                "plan_id": "imported",
                "trip_history": {
                    "recorder_import_reason": "recorder_imported",
                    "record_count": 1,
                },
            }
        )
        await store.async_add_forecast_snapshot(
            {
                "created_at": start + timedelta(minutes=5),
                "plan_id": "recent",
                "trip_history": {
                    "recorder_import_reason": "recorder_import_recent",
                    "record_count": 1,
                },
            }
        )

    asyncio.run(add_snapshots())

    assert store.data["forecast_snapshots"][0]["trip_history"] == {
        "recorder_import_reason": "recorder_imported",
        "record_count": 1,
        "latest_recorder_import_reason": "recorder_import_recent",
    }
    assert storage_module._merge_forecast_bucket_trip_history(
        {"recorder_import_reason": "recorder_imported", "record_count": 1},
        None,
    ) == {"recorder_import_reason": "recorder_imported", "record_count": 1}
    assert storage_module._merge_forecast_bucket_trip_history(
        {"recorder_import_reason": "recorder_import_recent", "record_count": "bad"},
        {"recorder_import_reason": "recorder_no_new_trips", "record_count": False},
    ) == {"recorder_import_reason": "recorder_no_new_trips", "record_count": 0}


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
    store.data["dry_run_comparisons"] = [
        {"created_at": (now - timedelta(days=8)).isoformat(), "planned_action_count": 1},
        {"created_at": (now - timedelta(days=1)).isoformat(), "planned_action_count": 2},
    ]
    asyncio.run(store.async_add_dry_run_comparison({"created_at": now, "planned_action_count": 3, "next_action": None}))

    assert len(store.data["forecast_snapshots"]) == 128
    assert all(item["plan_id"] != "expired" for item in store.data["forecast_snapshots"])
    assert all(isinstance(item, dict) for item in store.data["forecast_snapshots"])
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
