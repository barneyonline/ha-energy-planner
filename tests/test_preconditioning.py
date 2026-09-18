"""Preconditioning decision and durable missed-window regression evidence."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner import storage
from custom_components.ha_energy_planner.climate_runtime import economic_actions
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    ActionOutcome,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    OutcomeResult,
    Override,
    PlanAction,
    PlannerMode,
    to_jsonable,
)
from custom_components.ha_energy_planner.planner import DryRunPlanner
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.preconditioning import (
    current_status,
    planning_status,
    record_outcome,
    record_plan,
)

NOW = datetime(2026, 9, 18, 3, tzinfo=UTC)
OPTIONS = {**DEFAULT_OPTIONS, "planner_enabled": True, "dry_run": False, "climate_control_enabled": True}


def context():
    return DecisionContext(
        NOW,
        "plan-1",
        [
            DecisionSlot(
                NOW + timedelta(minutes=5 * i), 0.1 if i < 3 else 0.8, 0.05, 0, 1, outdoor_temperature_forecast_c=5
            )
            for i in range(12)
        ],
        None,
        None,
        OccupancyState.OCCUPIED,
        InputHealth.HEALTHY,
        current_hvac_mode="heat",
        current_hvac_temperature_c=21,
        current_outdoor_temperature_c=5,
        occupied_temperature_low_c=19,
        occupied_temperature_high_c=23,
    )


def action():
    return PlanAction(
        "action-1",
        "plan-1",
        NOW,
        NOW + timedelta(minutes=5),
        ActionAsset.DAIKIN,
        ActionKind.SET_HVAC,
        {
            "phase": "preconditioning",
            "mode": "heat",
            "period_start": NOW + timedelta(minutes=15),
            "period_end": NOW + timedelta(hours=1),
            "precondition_end": NOW + timedelta(minutes=15),
        },
        [],
        [],
        None,
        1.0,
    )


def plan(actions=None):
    return EnergyPlan(
        "plan-1",
        NOW,
        24,
        5,
        "current",
        InputHealth.HEALTHY,
        PlannerMode.ACTIVE_HEALTHY,
        "test",
        1.0,
        None,
        [action()] if actions is None else actions,
        [],
        device_plans={"climate": {"preconditioning": {"status": "scheduled", "summary": "Scheduled"}}},
    )


def outcome(result=OutcomeResult.REJECTED, **kwargs):
    values = dict(
        action_id="action-1",
        attempted_at=NOW,
        result=result,
        reason="production_gate_not_armed",
        pre_state={},
        post_state={},
        plan_id="plan-1",
        asset="daikin",
        kind="set_hvac",
        desired_state=action().desired_state,
    )
    values.update(kwargs)
    return ActionOutcome(**values)


@pytest.mark.parametrize(
    ("occupancy", "override", "expected"),
    [
        (OccupancyState.OCCUPIED, True, "manual_hvac_override"),
        (OccupancyState.AWAY, False, "occupancy_away"),
        (OccupancyState.UNKNOWN, False, "occupancy_unknown"),
    ],
)
def test_economic_blocker_is_explained_and_clears(occupancy, override, expected):
    ctx = context()
    ctx.climate_inputs = {"identity": "test"}
    ctx.climate_engine = {"status": "active", "ever_active": True}
    ctx.occupancy_state = occupancy
    ctx.active_overrides = [Override("manual_hvac", "test", NOW + timedelta(minutes=5), "manual")] if override else []
    assert economic_actions(ctx, OPTIONS, []) == []
    assert ctx.climate_decision["reason"] == expected
    assert ctx.climate_decision["preconditioning_status"] == "blocked"
    assert "No candidate" not in ctx.climate_decision["summary"]
    ctx.occupancy_state = OccupancyState.OCCUPIED
    ctx.active_overrides = []
    economic_actions(ctx, OPTIONS, [])
    assert ctx.climate_decision["reason"] == "no_candidate"


def test_observation_policy_and_release_hold_are_explicit():
    ctx = context()
    ctx.climate_inputs = {"identity": "test"}
    ctx.climate_engine = {"status": "ready_observing"}
    economic_actions(ctx, {**OPTIONS, "hvac_decision_policy": "observe"}, [])
    assert ctx.climate_decision["reason"] == "observation_only"
    assert ctx.climate_decision["preconditioning_status"] == "observation"
    ctx.hvac_control = {"released_until": NOW + timedelta(minutes=5)}
    assert economic_actions(ctx, OPTIONS, []) == []
    assert ctx.climate_decision["reason"] == "release_hold"
    ctx.created_at += timedelta(minutes=5)
    economic_actions(ctx, OPTIONS, [])
    assert ctx.climate_decision["reason"] != "release_hold"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("manual", "manual_hvac_override"),
        ("away", "occupancy_away"),
        ("unknown", "occupancy_unknown"),
        ("temperature", "comfort_inputs_missing"),
        ("confidence", "insufficient_confidence"),
        ("gap", "forecast_gap"),
    ],
)
def test_legacy_status_identifies_blocker(change, expected):
    ctx = context()
    if change == "manual":
        ctx.active_overrides = [Override("manual_hvac", "test", None, "manual")]
    elif change == "away":
        ctx.occupancy_state = OccupancyState.AWAY
    elif change == "unknown":
        ctx.occupancy_state = OccupancyState.UNKNOWN
    elif change == "temperature":
        ctx.current_hvac_temperature_c = None
    elif change == "confidence":
        ctx.forecast_confidence = 0
    else:
        ctx.climate_legacy_decision = {"reason": "forecast_gap"}
    result = planning_status(ctx, [], PlannerMode.ACTIVE_HEALTHY, "Reason", OPTIONS)
    assert result["status"] == "blocked"
    assert result["reason"] == expected


def test_planned_action_is_not_reported_as_running_and_global_mode_is_visible():
    ctx = context()
    assert planning_status(ctx, [], PlannerMode.ACTIVE_HEALTHY, "No opportunity", OPTIONS)["status"] == "no_opportunity"
    result = planning_status(ctx, [action()], PlannerMode.ACTIVE_HEALTHY, "", OPTIONS)
    assert result["status"] == "scheduled"
    assert result["next_start"] == NOW.isoformat()
    result = planning_status(ctx, [action()], PlannerMode.DRY_RUN, "", OPTIONS)
    assert result["reason"] == "planner_not_active"
    ctx.climate_decision = {"summary": "Learning", "preconditioning_status": "learning", "reason": "model_learning"}
    assert planning_status(ctx, [], PlannerMode.ACTIVE_HEALTHY, "", OPTIONS)["status"] == "learning"
    ctx.climate_decision["legacy_fallback"] = True
    assert (
        planning_status(ctx, [], PlannerMode.ACTIVE_HEALTHY, "No legacy opportunity", OPTIONS)["summary"]
        == "No legacy opportunity"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("flat", "price_difference"),
        ("gap", "forecast_gap"),
        ("short", "forecast_too_short"),
        ("lead", "lead_time_disabled"),
        ("temperature", "comfort_inputs_missing"),
        ("rest", "minimum_cycle"),
    ],
)
def test_legacy_candidate_evidence_and_recovery(change, reason):
    ctx = context()
    policy = HVACPlanningPolicy(OPTIONS, {})
    kwargs = {}
    if change == "flat":
        for slot in ctx.slots:
            slot.import_price = 0.1
    elif change == "gap":
        for slot in ctx.slots:
            slot.import_price = None
    elif change == "short":
        ctx.slots = ctx.slots[:1]
    elif change == "lead":
        policy = HVACPlanningPolicy({**OPTIONS, "hvac_precondition_lead_minutes": 0}, {})
    elif change == "temperature":
        ctx.current_hvac_temperature_c = None
    else:
        kwargs["earliest_start"] = NOW + timedelta(hours=3)
    assert policy._next_hvac_period(ctx, **kwargs) is None
    decision = ctx.climate_legacy_decision
    assert decision["reason"] == reason
    assert decision["rejected"][reason]["count"] > 0
    if change == "flat":
        assert decision["rejected"][reason]["evidence"]["actual_delta"] == 0
        assert decision["rejected"][reason]["evidence"]["required_delta"] > 0
    recovered = context()
    result = DryRunPlanner(OPTIONS).create_plan(recovered)
    assert result.device_plans["climate"]["preconditioning"]["status"] == "scheduled"


def test_missed_window_survives_store_reload_and_successful_retry_is_not_missed(monkeypatch):
    class Disk:
        saved = None

        def __init__(self, *args, **kwargs):
            pass

        async def async_load(self):
            return deepcopy(self.saved)

        async def async_save(self, data):
            Disk.saved = deepcopy(data)

    monkeypatch.setattr(storage, "Store", Disk)

    async def scenario():
        first = storage.PlannerStore(object())
        await first.async_save_plan(plan())
        await first.async_add_outcome(outcome())
        reloaded = storage.PlannerStore(object())
        await reloaded.async_load()
        assert current_status(reloaded.data, plan())["reason"] == "production_gate_not_armed"
        later = plan([])
        later.plan_id = "plan-2"
        later.created_at = NOW + timedelta(minutes=20)
        await reloaded.async_save_plan(later)
        missed = current_status(reloaded.data, later)["last_missed_opportunity"]
        assert missed["plan_id"] == "plan-1"
        assert missed["last_outcome"]["reason"] == "production_gate_not_armed"
        await reloaded.async_save_plan(later)
        assert reloaded.data["preconditioning_history"]["last_missed"] == missed
        # A different, successful window must not replace the retained missed window.
        next_plan = plan()
        next_plan.actions[0].desired_state["period_end"] += timedelta(hours=1)
        await reloaded.async_save_plan(next_plan)
        await reloaded.async_add_outcome(
            outcome(OutcomeResult.APPLIED, desired_state=next_plan.actions[0].desired_state)
        )
        await reloaded.async_save_plan(later)
        assert reloaded.data["preconditioning_history"]["last_missed"] == missed
        assert "pending" not in reloaded.data["preconditioning_history"]

    asyncio.run(scenario())


def test_pending_window_correlates_regenerated_ids_and_expiration():
    serialized = to_jsonable(plan())
    history = record_plan({}, serialized)
    new = deepcopy(serialized)
    new["plan_id"] = "new"
    new["actions"][0]["action_id"] = "new-action"
    new["actions"][0]["plan_id"] = "new"
    assert record_plan(history, new) == history
    applied = to_jsonable(outcome(OutcomeResult.APPLIED, plan_id="new", action_id="new-action"))
    success = record_outcome(history, applied)
    assert success["pending"]["fulfilled"]
    assert not history["pending"]["fulfilled"]  # immutable previous storage generation
    expired = deepcopy(new)
    expired["created_at"] = (NOW + timedelta(hours=2)).isoformat()
    assert record_plan(success, expired) == {}
    missed = record_plan(history, expired)
    assert missed["last_missed"]["reason"] == "window_expired"
    assert "pending" not in missed
    invalid = deepcopy(serialized)
    invalid["actions"][0]["desired_state"]["precondition_end"] = "bad"
    assert record_plan({}, invalid) == {}
    assert record_plan({}, {**serialized, "created_at": "bad"}) == {}
    stale = deepcopy(history)
    stale["pending"]["end"] = "bad"
    assert record_plan(stale, serialized) == history


@pytest.mark.parametrize("change", ["empty", "asset", "kind", "phase", "window", "none"])
def test_unrelated_outcomes_do_not_mark_window_executed(change):
    history = record_plan({}, to_jsonable(plan()))
    entry = to_jsonable(outcome(OutcomeResult.APPLIED))
    if change == "empty":
        history = {}
    elif change == "asset":
        entry["asset"] = "ev"
    elif change == "kind":
        entry["kind"] = "release_hvac"
    elif change == "phase":
        entry["desired_state"]["phase"] = "peak_coast"
    elif change == "none":
        entry["desired_state"] = None
    else:
        entry["desired_state"]["mode"] = "cool"
    assert record_outcome(history, entry) == history


def test_current_status_prioritises_restoration_and_does_not_reuse_old_blocker():
    history = record_outcome(record_plan({}, to_jsonable(plan())), to_jsonable(outcome()))
    store = {"preconditioning_history": history, "ownership": {"hvac_control": {"phase": "preconditioning"}}}
    assert current_status(store, plan())["status"] == "running"
    store["ownership"]["hvac_control"]["required_evidence_lost"] = "zone_unavailable"
    assert current_status(store, plan())["status"] == "restoring"
    store["ownership"]["hvac_control"] = []
    assert current_status(store, plan())["status"] == "blocked"
    other = plan()
    other.plan_id = "new"
    assert current_status(store, other)["status"] == "scheduled"
    assert current_status(store, None)["last_attempt"]["reason"] == "production_gate_not_armed"
    assert current_status({}, None)["last_attempt"] is None
    store["preconditioning_history"] = record_outcome(history, to_jsonable(outcome(OutcomeResult.APPLIED)))
    assert current_status(store, plan())["status"] == "scheduled"
    assert current_status({}, SimpleNamespace(device_plans={}))["last_missed_opportunity"] is None


def test_disabled_control_and_recovered_window_are_not_misrepresented():
    result = planning_status(context(), [action()], PlannerMode.ACTIVE_HEALTHY, "", DEFAULT_OPTIONS)
    assert result["reason"] == "climate_control_disabled"
    assert current_status({"production": {"armed": False}}, plan())["reason"] == "production_gate_not_armed"
    history = record_plan({}, to_jsonable(plan()))
    withdrawn = record_plan(history, to_jsonable(plan([])))
    assert withdrawn["last_missed"]["reason"] == "window_replaced"
    retried = record_plan(withdrawn, to_jsonable(plan()))
    recovered = record_outcome(retried, to_jsonable(outcome(OutcomeResult.APPLIED)))
    assert "last_missed" not in recovered
    assert recovered["pending"]["fulfilled"]
    # Later idempotent skips must not erase confirmation that this window ran.
    skipped = record_outcome(recovered, to_jsonable(outcome(OutcomeResult.SKIPPED)))
    assert skipped["pending"]["fulfilled"]


def test_evaluated_candidate_does_not_claim_command_was_scheduled():
    ctx = context()
    ctx.climate_decision = {"summary": "Positive saving", "preconditioning_status": "scheduled"}
    result = planning_status(ctx, [], PlannerMode.ACTIVE_HEALTHY, "", OPTIONS)
    assert result["reason"] == "schedule_not_selected"
    assert result["status"] == "blocked"


def test_legacy_measurements_survive_sensor_attribute_bounds():
    from custom_components.ha_energy_planner.plan_presentation import bounded_json

    ctx = context()
    for slot in ctx.slots:
        slot.import_price = 0.1
    generated = DryRunPlanner(OPTIONS).create_plan(ctx)
    shown = bounded_json(current_status({}, generated))
    sample = next(item for item in shown["legacy_rejections"] if item["reason"] == "price_difference")
    assert sample["actual_delta"] == 0
    assert isinstance(sample["required_delta"], float)
    assert sample["count"] > 0


def test_confirmed_existing_ownership_and_idempotent_targets_are_not_missed():
    control = {**to_jsonable(action().desired_state), "main_state_committed": True}
    history = record_plan({}, to_jsonable(plan()), control)
    assert history["pending"]["fulfilled"]
    assert record_plan(history, to_jsonable(plan([]))) == {}
    control["mode"] = "cool"
    assert not record_plan({}, to_jsonable(plan()), control)["pending"]["fulfilled"]
    skipped = to_jsonable(outcome(OutcomeResult.SKIPPED, reason="already_in_desired_hvac_state"))
    history = record_outcome(record_plan({}, to_jsonable(plan())), skipped)
    assert history["pending"]["fulfilled"]
    assert current_status({"preconditioning_history": history}, plan())["status"] == "scheduled"
    assert record_plan(history, to_jsonable(plan([]))) == {}
    provisional = {
        "ownership": {"hvac_control": {**control, "main_state": {"temperature": 21}, "main_state_committed": False}}
    }
    assert current_status(provisional, plan())["reason"] == "ownership_unconfirmed"


def test_manual_override_clear_replans_remaining_window_after_restart():
    ctx = context()
    ctx.active_overrides = [Override("manual_hvac", "test", NOW + timedelta(minutes=5), "manual")]
    blocked = DryRunPlanner(OPTIONS).create_plan(ctx)
    assert blocked.device_plans["climate"]["preconditioning"]["reason"] == "manual_hvac_override"
    # A new planner instance and restored inputs after the override expires.
    ctx.created_at += timedelta(minutes=5)
    ctx.slots = ctx.slots[1:]
    resumed = DryRunPlanner(OPTIONS).create_plan(ctx)
    assert resumed.device_plans["climate"]["preconditioning"]["status"] == "scheduled"
    selected = next(a for a in resumed.actions if a.desired_state.get("phase") == "preconditioning")
    assert selected.execute_not_before >= ctx.created_at
    assert selected.desired_state["period_start"] == NOW + timedelta(minutes=15)


def test_retained_window_uses_revised_preconditioning_end():
    serialized = to_jsonable(plan())
    history = record_plan({}, serialized)
    revised = deepcopy(serialized)
    revised["created_at"] = (NOW + timedelta(minutes=16)).isoformat()
    revised["actions"][0]["desired_state"]["precondition_end"] = (NOW + timedelta(minutes=20)).isoformat()
    result = record_plan(history, revised)
    assert "last_missed" not in result
    assert result["pending"]["end"] == (NOW + timedelta(minutes=20)).isoformat()
    assert history["pending"]["end"] == (NOW + timedelta(minutes=15)).isoformat()


@pytest.mark.parametrize("replacement", [False, True])
def test_late_outcome_updates_withdrawn_window_without_touching_replacement(replacement):
    history = record_plan({}, to_jsonable(plan()))
    newer = plan([])
    if replacement:
        newer = plan()
        newer.actions[0].desired_state["period_end"] += timedelta(hours=1)
    history = record_plan(history, to_jsonable(newer))
    rejected = record_outcome(history, to_jsonable(outcome()))
    assert rejected["last_missed"]["last_outcome"]["reason"] == "production_gate_not_armed"
    resolved = record_outcome(rejected, to_jsonable(outcome(OutcomeResult.APPLIED)))
    assert "last_missed" not in resolved
    assert resolved.get("pending") == history.get("pending")


def test_persisted_success_reconciles_existing_pending_record_after_interrupted_audit():
    history = record_plan({}, to_jsonable(plan()))
    control = {**to_jsonable(action().desired_state), "main_state_committed": True}
    # Ownership is saved before async_add_outcome; a restart may occur between them.
    reconciled = record_plan(history, to_jsonable(plan([])), control)
    assert "last_missed" not in reconciled
    withdrawn = record_plan(history, to_jsonable(plan([])))
    assert "last_missed" in withdrawn
    assert record_plan(withdrawn, to_jsonable(plan([])), control) == {}


def test_coasting_ownership_alone_does_not_prove_preconditioning_ran():
    control = {**to_jsonable(action().desired_state), "main_state_committed": True, "phase": "peak_coast"}
    history = record_plan({}, to_jsonable(plan()), control)
    assert not history["pending"]["fulfilled"]


def test_current_status_honours_final_validation_mode_before_first_attempt():
    generated = DryRunPlanner(OPTIONS).create_plan(context())
    assert generated.device_plans["climate"]["preconditioning"]["status"] == "scheduled"
    generated.mode = PlannerMode.ACTIVE_DEGRADED
    generated.input_issues.append("grid_import_limit_exceeded")
    actual = current_status({"production": {"armed": True}}, generated)
    assert actual["status"] == "blocked"
    assert actual["reason"] == "planner_not_active"


def test_final_rollback_gate_does_not_claim_legacy_schedule_selected():
    ctx = context()
    ctx.input_issues = ["main_climate_target_unavailable"]
    generated = DryRunPlanner({**OPTIONS, "minimum_climate_confidence": 0}).create_plan(ctx)
    assert not any(a.desired_state.get("phase") == "preconditioning" for a in generated.actions)
    status = generated.device_plans["climate"]["preconditioning"]
    assert status["status"] == "blocked"
    assert status["reason"] == "rollback_target_unavailable"
    assert "restoration target" in status["summary"].lower()
    assert status["next_start"] is None


def test_store_restart_between_ownership_and_outcome_writes_recovers_confirmation(monkeypatch):
    persisted = {}

    class Disk:
        def __init__(self, *args, **kwargs):
            pass

        async def async_load(self):
            return deepcopy(persisted)

        async def async_save(self, data):
            persisted.clear()
            persisted.update(deepcopy(data))

    monkeypatch.setattr(storage, "Store", Disk)

    async def scenario():
        first = storage.PlannerStore(object())
        await first.async_save_plan(plan())
        original_pending = first.data["preconditioning_history"]["pending"]
        await first.async_save_ownership(
            {
                "hvac_control": {
                    **to_jsonable(action().desired_state),
                    "main_state_committed": True,
                }
            }
        )
        # Execution stops before the separate audit write. Reload only durable evidence.
        reloaded = storage.PlannerStore(object())
        await reloaded.async_load()
        await reloaded.async_save_plan(plan([]))
        assert reloaded.data["preconditioning_history"] == {}
        assert persisted["preconditioning_history"] == {}
        assert not original_pending["fulfilled"]
        # A delayed failed result for a withdrawn window is also retained through reload.
        await reloaded.async_save_ownership({})
        await reloaded.async_save_plan(plan())
        await reloaded.async_save_plan(plan([]))
        await reloaded.async_add_outcome(outcome(OutcomeResult.FAILED, reason="service_timeout"))
        after_result = storage.PlannerStore(object())
        await after_result.async_load()
        assert (
            after_result.data["preconditioning_history"]["last_missed"]["last_outcome"]["reason"] == "service_timeout"
        )
        await after_result.async_add_outcome(outcome(OutcomeResult.APPLIED))
        assert persisted["preconditioning_history"] == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("other_blocker", ["manual", "away"])
def test_simultaneous_legacy_blockers_keep_code_and_summary_consistent(other_blocker):
    ctx = context()
    ctx.forecast_confidence = 0
    if other_blocker == "manual":
        ctx.active_overrides = [Override("manual_hvac", "test", None, "manual")]
    else:
        ctx.occupancy_state = OccupancyState.AWAY
    generated = DryRunPlanner(OPTIONS).create_plan(ctx)
    explanation = generated.device_plans["climate"]["preconditioning"]
    assert explanation["reason"] == "insufficient_confidence"
    assert "confidence is below" in explanation["summary"]
    # Once confidence recovers, both fields must identify the remaining blocker.
    ctx.forecast_confidence = 1
    recovered = DryRunPlanner(OPTIONS).create_plan(ctx).device_plans["climate"]["preconditioning"]
    assert recovered["reason"] == ("manual_hvac_override" if other_blocker == "manual" else "occupancy_away")
    remaining_reason = "manual climate override" if other_blocker == "manual" else "nobody is currently home"
    assert remaining_reason in recovered["summary"]
