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


@pytest.mark.parametrize("mode,temperature,target,coast", [("heat", 23, 23, 19), ("cool", 19, 19, 23)])
def test_live_early_target_enters_coast_and_does_not_reheat(mode, temperature, target, coast):
    ctx = context()
    ctx.current_hvac_temperature_c = temperature
    ctx.hvac_control = {
        "phase": "preconditioning",
        "mode": mode,
        "main_state_committed": True,
        "period_start": NOW + timedelta(minutes=15),
        "period_end": NOW + timedelta(hours=1),
        "precondition_end": NOW + timedelta(minutes=15),
        "baseline_price": 0.1,
        "precondition_target": target,
        "coast_target": coast,
    }
    policy = HVACPlanningPolicy({**OPTIONS, "planning_interval_minutes": 5}, {})
    actions = policy._hvac_lifecycle_actions(ctx, ctx.created_at, ctx.created_at + timedelta(minutes=5))
    assert actions[0].desired_state["phase"] == "pre_peak_coast"
    assert actions[0].desired_state["target_temperature"] == coast
    assert actions[0].desired_state["precondition_end"] == NOW
    ctx.hvac_control.update(actions[0].desired_state)
    ctx.created_at += timedelta(minutes=5)
    ctx.current_hvac_temperature_c = 21
    assert (
        policy._hvac_lifecycle_actions(ctx, ctx.created_at, ctx.created_at + timedelta(minutes=5))[0].desired_state[
            "phase"
        ]
        == "pre_peak_coast"
    )


def test_learning_observation_does_not_withhold_legacy_cycle():
    ctx = context()
    ctx.climate_inputs = {"identity": "test", "power_source_type": "estimated"}
    ctx.climate_engine = {
        "status": "learning",
        "opportunities": 9,
        "observation_until": (NOW + timedelta(hours=1)).isoformat(),
    }
    legacy = [action()]
    assert economic_actions(ctx, OPTIONS, legacy) == legacy
    assert ctx.climate_engine["opportunities"] == 9
    assert not ctx.climate_decision["observing"]


def test_source_tariff_identity_survives_refresh_grid_drift_and_configuration_changes():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planner_hvac import _legacy_lifecycle_id

    tariff = SimpleNamespace(
        attributes={
            "unit_of_measurement": "AUD/kWh",
            "forecast": [{"valid_at": (NOW + timedelta(minutes=i * 30)).isoformat(), "price": i} for i in range(6)],
        }
    )
    intervals = tariff_source_intervals(tariff, 5)
    assert len(intervals) == 6
    assert tariff_source_intervals(SimpleNamespace(attributes={}), 5) == []
    ctx = context()
    ctx.climate_inputs.update(identity="config", tariff_intervals=intervals)
    start, end = NOW + timedelta(minutes=30), NOW + timedelta(hours=1)
    cycle = _legacy_lifecycle_id(ctx, OPTIONS, start, end, "heat")
    assert (
        _legacy_lifecycle_id(ctx, OPTIONS, start + timedelta(minutes=14), end + timedelta(minutes=14), "heat") == cycle
    )
    assert _legacy_lifecycle_id(ctx, OPTIONS, start, end, "cool") != cycle
    ctx.climate_inputs["identity"] = "changed"
    assert _legacy_lifecycle_id(ctx, OPTIONS, start, end, "heat") != cycle
    planned = plan([action()])
    planned.actions[0].desired_state["lifecycle_id"] = cycle
    history = record_plan({}, to_jsonable(planned))
    planned.created_at += timedelta(minutes=1)
    planned.actions[0].desired_state["period_start"] += timedelta(seconds=45)
    history = record_plan(history, to_jsonable(planned))
    assert "last_missed" not in history
    control = {
        **planned.actions[0].desired_state,
        "phase": "pre_peak_coast",
        "main_state_committed": True,
        "preconditioning_confirmed": True,
    }
    history = record_plan(history, to_jsonable(plan([])), control)
    assert "last_missed" not in history


def test_early_coast_continuation_bypasses_minimum_cycle_by_lifecycle():
    from custom_components.ha_energy_planner.constraints import _is_same_hvac_lifecycle

    ctx = context()
    ctx.hvac_control = {"phase": "preconditioning", "mode": "heat", "lifecycle_id": "same"}
    transition = action()
    transition.desired_state.update(phase="pre_peak_coast", hvac_mode="heat", lifecycle_id="same")
    ownership = SimpleNamespace(planner_takeover_started_at=NOW, hvac_control_phase="preconditioning")
    assert _is_same_hvac_lifecycle(ctx, transition, ownership)
    transition.desired_state["lifecycle_id"] = "different"
    assert not _is_same_hvac_lifecycle(ctx, transition, ownership)


def test_confirmed_early_transition_retains_all_original_baselines():
    import json
    from pathlib import Path

    from custom_components.ha_energy_planner.constraints import _lifecycle_datetime
    from custom_components.ha_energy_planner.hvac_adapter import HVACCommandResult
    from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction

    evidence = json.loads((Path(__file__).parent / "fixtures/climate/preconditioning_sep_oct_2026.json").read_text())[
        "early_target"
    ]
    now = datetime.fromisoformat(evidence["reached_at"]).astimezone(UTC)
    original = {
        "climate_automations": {"automation.schedule": "on"},
        "hvac_control": {
            "phase": "preconditioning",
            "lifecycle_id": "october-cycle",
            "main_state": {"hvac_mode": "off", "target_temperature": evidence["original_main_target"]},
            "zone_states": {"climate.zone": {"target_temperature": evidence["original_zone_target"]}},
            "main_state_committed": True,
        },
    }
    desired = {
        "phase": "pre_peak_coast",
        "phase_transition_reason": "target_reached",
        "target_temperature": evidence["coast"],
        "precondition_end": now,
    }
    transaction = HVACOwnershipTransaction(deepcopy(original), now)
    provisional = transaction.prepare(
        desired,
        {"automation.schedule": "off"},
        {"climate.zone": {"target_temperature": 24}},
        {"hvac_mode": "heat", "target_temperature": 24},
    )
    confirmed = now + timedelta(seconds=3)
    result = transaction.complete(
        HVACCommandResult(True, "applied", {}, {}, {}),
        provisional,
        desired,
        main_state_superseded=False,
        superseded_zone_entity_ids=set(),
        confirmed_at=confirmed,
    )
    assert result["hvac_control"]["main_state"] == original["hvac_control"]["main_state"]
    assert result["hvac_control"]["zone_states"] == original["hvac_control"]["zone_states"]
    assert result["climate_automations"] == original["climate_automations"]
    assert result["hvac_control"]["precondition_end"] == confirmed
    assert result["hvac_control"]["phase_started_at"] == confirmed
    assert _lifecycle_datetime(datetime(2026, 10, 1)) == datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize("change", ["source", "comfort", "policy"])
def test_owned_legacy_cycle_releases_after_material_configuration_change(change):
    ctx = context()
    ctx.climate_inputs = {"identity": "original"}
    options = dict(OPTIONS)
    selected = next(a for a in DryRunPlanner(options).create_plan(ctx).actions if a.kind == ActionKind.SET_HVAC)
    ctx.hvac_control = dict(selected.desired_state)
    ctx.created_at += timedelta(seconds=20)
    if change == "source":
        ctx.climate_inputs["identity"] = "different-controls"
    elif change == "comfort":
        ctx.occupied_temperature_high_c = 22
    else:
        options["hvac_precondition_lead_minutes"] += 5
    actions = DryRunPlanner(options).create_plan(ctx).actions
    assert actions[0].kind == ActionKind.RELEASE_HVAC
    assert actions[0].desired_state["release_reason"] == "hvac_configuration_changed"


def test_tariff_identity_respects_explicit_intervals_when_forecast_gaps_fill():
    from custom_components.ha_energy_planner.forecasts import tariff_source_intervals
    from custom_components.ha_energy_planner.planner_hvac import _legacy_lifecycle_id

    ctx = context()
    tariff = SimpleNamespace(
        attributes={
            "interval_minutes": 30,
            "forecast": [
                {"valid_at": NOW.isoformat(), "price": 1},
                {"valid_at": (NOW + timedelta(hours=2)).isoformat(), "price": 0.1},
            ],
        }
    )
    ctx.climate_inputs["tariff_intervals"] = tariff_source_intervals(tariff, 5)
    assert ctx.climate_inputs["tariff_intervals"][0]["end"] == (NOW + timedelta(minutes=30)).isoformat()
    cycle = _legacy_lifecycle_id(ctx, OPTIONS, NOW, NOW + timedelta(minutes=30), "heat")
    tariff.attributes["forecast"].insert(1, {"valid_at": (NOW + timedelta(minutes=30)).isoformat(), "price": 0.1})
    ctx.climate_inputs["tariff_intervals"] = tariff_source_intervals(tariff, 5)
    assert _legacy_lifecycle_id(ctx, OPTIONS, NOW, NOW + timedelta(minutes=30), "heat") == cycle


def tariff_tail_plan(end_hour, *, at=NOW - timedelta(minutes=15)):
    """Replay the audit's 03:00 preparation, 05:00 peak and changing tail."""
    ctx = context()
    ctx.created_at = at
    ctx.plan_id = f"tail-{end_hour}-{at.isoformat()}"
    ctx.current_hvac_mode = "cool"
    ctx.current_hvac_temperature_c = 23.98
    ctx.current_outdoor_temperature_c = 32
    ctx.occupied_temperature_low_c = 21
    ctx.occupied_temperature_high_c = 24
    ctx.climate_inputs = {"identity": "fixed-controls-and-rooms"}
    grid_start = at.replace(minute=at.minute // 5 * 5, second=0, microsecond=0)
    instants = [at, *(grid_start + timedelta(minutes=5 * index) for index in range(1, 24 * 12))]
    ctx.slots = [
        DecisionSlot(
            valid_at,
            0.7 if 5 <= valid_at.hour < end_hour else 0.0247 if end_hour == 10 else 0.0055,
            0.05, 0, 1, outdoor_temperature_forecast_c=32,
        )
        for valid_at in instants
    ]
    return DryRunPlanner({**OPTIONS, "hvac_precondition_lead_minutes": 120}).create_plan(ctx)


def preparation_action(generated):
    return next(a for a in generated.actions if a.desired_state.get("phase") == "preconditioning")


@pytest.mark.parametrize("tail", [7, 21])
@pytest.mark.parametrize("confirmation", ["applied", "already_confirmed", "ownership", "coast_ownership"])
def test_tariff_tail_replacement_clears_miss_only_after_confirmation_and_reload(monkeypatch, tail, confirmation):
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
        original = tariff_tail_plan(10)
        replacement = tariff_tail_plan(tail, at=NOW + timedelta(seconds=10))
        before, after = preparation_action(original), preparation_action(replacement)
        assert before.execute_not_before == NOW
        assert after.execute_not_before == replacement.created_at
        assert before.desired_state["precondition_end"] == NOW.replace(hour=5)
        assert after.desired_state["precondition_end"] == NOW.replace(hour=5)
        assert before.desired_state["lifecycle_id"] != after.desired_state["lifecycle_id"]
        assert before.desired_state["configuration_identity"] == after.desired_state["configuration_identity"]
        assert after.desired_state["target_temperature"] == 21
        assert after.desired_state["period_end"] == NOW.replace(hour=tail)
        assert before.desired_state["baseline_price"] != after.desired_state["baseline_price"]
        first = storage.PlannerStore(object())
        await first.async_save_plan(original)
        saved_original = deepcopy(first.data["preconditioning_history"])
        await first.async_save_plan(replacement)
        missed = first.data["preconditioning_history"]["last_missed"]
        assert missed["reason"] == "window_replaced"
        assert not missed["fulfilled"]
        assert not first.data["preconditioning_history"]["pending"]["fulfilled"]
        if confirmation in {"ownership", "coast_ownership"}:
            control = {
                **after.desired_state,
                "main_state_committed": True,
                "preconditioning_confirmed": True,
                "preconditioning_started_at": NOW + timedelta(seconds=10),
                "started_at": NOW + timedelta(seconds=10),
            }
            # Persisted ownership carries lifecycle mode/target, not command fields.
            control.pop("target_temperature")
            control.pop("hvac_mode")
            if confirmation == "coast_ownership":
                control.update(phase="pre_peak_coast", precondition_end=NOW + timedelta(minutes=25))
            await first.async_save_ownership({"hvac_control": control})
        else:
            await first.async_add_outcome(outcome(
                OutcomeResult.APPLIED if confirmation == "applied" else OutcomeResult.SKIPPED,
                reason="hvac_action_applied" if confirmation == "applied" else "already_in_desired_hvac_state",
                desired_state=after.desired_state, attempted_at=NOW + timedelta(seconds=10),
                plan_id=replacement.plan_id, action_id=after.action_id,
            ))
            assert first.data["preconditioning_history"]["pending"]["fulfilled"]
            assert "last_missed" not in first.data["preconditioning_history"]
            assert current_status(first.data, replacement)["last_attempt"]["action_id"] == after.action_id
        # Ownership confirmation must work even if the separate outcome write was lost.
        reloaded = storage.PlannerStore(object())
        await reloaded.async_load()
        later = plan([])
        later.created_at = NOW + timedelta(hours=3)
        await reloaded.async_save_plan(later)
        await reloaded.async_save_plan(later)
        assert reloaded.data["preconditioning_history"] == persisted["preconditioning_history"] == {}
        assert current_status(reloaded.data, later)["last_missed_opportunity"] is None
        assert saved_original["pending"]["fulfilled"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("result,reason", [
    (OutcomeResult.REJECTED, "manual_hvac_override_detected"),
    (OutcomeResult.FAILED, "hvac_confirmation_timeout"),
    (OutcomeResult.SKIPPED, "cancelled"),
])
def test_tail_replacement_failure_and_cancellation_remain_missed(result, reason):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW)
    history = record_plan({}, to_jsonable(original))
    history = record_plan(history, to_jsonable(replacement))
    failed = record_outcome(history, to_jsonable(outcome(
        result, reason=reason, desired_state=preparation_action(replacement).desired_state,
    )))
    assert failed["last_missed"]["last_outcome"]["reason"] == reason
    assert not failed["last_missed"]["fulfilled"]
    assert not failed["pending"]["fulfilled"]
    later = plan([])
    later.created_at = NOW + timedelta(hours=3)
    expired = record_plan(failed, to_jsonable(later))
    assert expired["last_missed"]["reason"] == "window_expired"
    assert expired["last_missed"]["last_outcome"]["reason"] == reason
    assert not expired["last_missed"]["fulfilled"]


@pytest.mark.parametrize("change", [
    "configuration", "mode", "target", "peak", "deadline", "not_legacy", "no_lifecycle", "no_target",
    "bad_peak", "bad_deadline", "deadline_after_peak", "old_record", "nonoverlapping", "before_start",
    "missing_time", "bad_start", "bad_end",
])
def test_cross_lifecycle_confirmation_requires_complete_matching_preparation(change):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW)
    history = record_plan({}, to_jsonable(original))
    desired = to_jsonable(preparation_action(replacement).desired_state)
    at = NOW
    updates = {
        "configuration": {"configuration_identity": "legacy:different-controls"},
        "mode": {"mode": "heat", "hvac_mode": "heat"},
        "target": {"target_temperature": 22},
        "peak": {"period_start": (NOW + timedelta(hours=3)).isoformat()},
        "deadline": {"precondition_end": (NOW + timedelta(hours=1)).isoformat()},
        "not_legacy": {"configuration_identity": "economic:other"},
        "no_lifecycle": {"lifecycle_id": None},
        "no_target": {"target_temperature": None},
        "bad_peak": {"period_start": "bad"},
        "bad_deadline": {"precondition_end": "bad"},
        "deadline_after_peak": {"precondition_end": (NOW + timedelta(hours=3)).isoformat()},
    }
    desired.update(updates.get(change, {}))
    if change == "old_record":
        history["pending"].pop("preparation")
    elif change == "nonoverlapping":
        at += timedelta(hours=2)
    elif change == "before_start":
        at -= timedelta(seconds=1)
    elif change == "missing_time":
        at = None
    elif change in {"bad_start", "bad_end"}:
        history["pending"][change.removeprefix("bad_")] = "bad"
    entry = to_jsonable(outcome(OutcomeResult.APPLIED, desired_state=desired, attempted_at=at))
    assert record_outcome(history, entry) == history
    control = {**desired, "main_state_committed": True, "started_at": at}
    withdrawn = record_plan(history, to_jsonable(plan([])), control)
    assert not withdrawn["last_missed"]["fulfilled"]


@pytest.mark.parametrize("phase,committed,confirmed", [
    ("preconditioning", False, True), ("peak_coast", True, False), ("preconditioning", False, False),
    ("peak_coast", True, True),
])
def test_cross_lifecycle_uncertain_ownership_never_proves_fulfillment(phase, committed, confirmed):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW)
    history = record_plan({}, to_jsonable(original))
    control = {
        **to_jsonable(preparation_action(replacement).desired_state),
        "main_state_committed": committed, "phase": phase,
        "preconditioning_confirmed": confirmed, "started_at": NOW.isoformat(),
    }
    withdrawn = record_plan(history, to_jsonable(plan([])), control)
    assert not withdrawn["last_missed"]["fulfilled"]


def test_confirmed_replacement_ownership_matches_without_replacement_plan_or_outcome():
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW)
    history = record_plan({}, to_jsonable(original))
    control = {
        **to_jsonable(preparation_action(replacement).desired_state),
        "main_state_committed": True, "started_at": NOW.isoformat(),
    }
    assert record_plan(history, to_jsonable(plan([])), control) == {}
    missed = record_plan(history, to_jsonable(plan([])))
    assert record_plan(missed, to_jsonable(plan([])), control) == {}
    assert record_plan({}, to_jsonable(original), control)["pending"]["fulfilled"]


@pytest.mark.parametrize("confirmation", ["late_outcome", "prior_outcome", "ownership", "prior_ownership"])
def test_original_confirmation_also_fulfills_later_matching_replacement(confirmation):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    applied = to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=preparation_action(original).desired_state,
    ))
    history = record_plan({}, to_jsonable(original))
    if confirmation == "prior_outcome":
        history = record_outcome(history, applied)
    control = {
        **to_jsonable(preparation_action(original).desired_state),
        "main_state_committed": True, "started_at": NOW.isoformat(),
    }
    history = record_plan(
        history, to_jsonable(replacement), control if confirmation == "prior_ownership" else None,
    )
    previous = deepcopy(history)
    if confirmation == "late_outcome":
        confirmed = record_outcome(history, applied)
    elif confirmation == "ownership":
        confirmed = record_plan(history, to_jsonable(replacement), control)
    else:
        confirmed = history
    assert "last_missed" not in confirmed
    assert confirmed["pending"]["fulfilled"]
    if confirmation in {"late_outcome", "prior_outcome"}:
        assert current_status({"preconditioning_history": confirmed}, replacement)["last_attempt"]["attempted_at"]
    later = plan([])
    later.created_at = NOW + timedelta(hours=3)
    assert record_plan(confirmed, to_jsonable(later)) == {}
    assert history == previous  # old storage generations remain immutable


@pytest.mark.parametrize("change", ["configuration", "mode", "target", "peak", "nonoverlapping"])
def test_confirmation_does_not_transfer_to_an_unrelated_replacement(change):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    selected = preparation_action(replacement)
    if change == "configuration":
        selected.desired_state["configuration_identity"] = "legacy:other-controls"
    elif change == "mode":
        selected.desired_state.update(mode="heat", hvac_mode="heat")
    elif change == "target":
        selected.desired_state["target_temperature"] = 22
    elif change == "peak":
        selected.desired_state["period_start"] += timedelta(hours=1)
    else:
        selected.execute_not_before = NOW.replace(hour=5)
    history = record_outcome(record_plan({}, to_jsonable(original)), to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=preparation_action(original).desired_state,
    )))
    revised = record_plan(history, to_jsonable(replacement))
    assert not revised["pending"]["fulfilled"]
    assert "last_outcome" not in revised["pending"]
    assert history["pending"]["fulfilled"]


@pytest.mark.parametrize("attempted_at", [NOW - timedelta(seconds=1), NOW.replace(hour=5), None])
@pytest.mark.parametrize("owned", [False, True])
def test_outcome_outside_both_preparations_does_not_transfer_across_ids(attempted_at, owned):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    history = record_plan(record_plan({}, to_jsonable(original)), to_jsonable(replacement))
    applied = to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=preparation_action(replacement).desired_state,
        attempted_at=attempted_at,
    ))
    revised = record_outcome(history, applied)
    # Preserve existing strict-ID semantics without spreading out-of-window evidence.
    assert revised["pending"]["fulfilled"]
    assert not revised["last_missed"]["fulfilled"]
    control = {
        **to_jsonable(preparation_action(replacement).desired_state), "main_state_committed": True,
        "preconditioning_started_at": to_jsonable(attempted_at),
        "phase_started_at": to_jsonable(attempted_at),
    } if owned else {}
    revised = record_plan(deepcopy(revised), to_jsonable(replacement), control)
    assert not revised["last_missed"]["fulfilled"]
    later = plan([])
    later.created_at = NOW + timedelta(hours=3)
    assert not record_plan(revised, to_jsonable(later), control)["last_missed"]["fulfilled"]


@pytest.mark.parametrize("later_result", [OutcomeResult.FAILED, OutcomeResult.APPLIED])
def test_bounded_confirmation_survives_later_out_of_window_attempt(later_result):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    desired = preparation_action(original).desired_state
    history = record_outcome(record_plan({}, to_jsonable(original)), to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=desired,
    )))
    history = record_outcome(history, to_jsonable(outcome(
        later_result, desired_state=desired, attempted_at=NOW.replace(hour=5),
    )))
    history = record_plan(deepcopy(history), to_jsonable(replacement))
    assert history["pending"]["fulfilled"]
    assert history["pending"]["confirmation"]["attempted_at"] == NOW.isoformat()
    assert history["pending"]["last_outcome"]["attempted_at"] == NOW.replace(hour=5).isoformat()
    later = plan([])
    later.created_at = NOW + timedelta(hours=3)
    assert record_plan(deepcopy(history), to_jsonable(later)) == {}


def test_same_lifecycle_revision_revalidates_saved_confirmation_bounds():
    original = tariff_tail_plan(10)
    history = record_outcome(record_plan({}, to_jsonable(original)), to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=preparation_action(original).desired_state,
    )))
    previous = deepcopy(history)
    revised = deepcopy(original)
    deadline = NOW.replace(hour=5) - timedelta(minutes=20)
    preparation_action(revised).desired_state["precondition_end"] = deadline
    updated = record_plan(history, to_jsonable(revised))
    # Refresh revalidates the actual attempt time against the revised bounds.
    updated = record_plan(updated, to_jsonable(revised))
    assert updated["pending"]["confirmation"]["end"] == deadline.isoformat()
    replacement = tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    preparation_action(replacement).desired_state["precondition_end"] = deadline
    assert record_plan(updated, to_jsonable(replacement))["pending"]["fulfilled"]
    assert history == previous


def test_late_outcome_with_revised_deadline_does_not_copy_mismatched_proof():
    original = tariff_tail_plan(10)
    preparation_action(original).desired_state["precondition_end"] -= timedelta(minutes=20)
    replacement = tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    preparation_action(replacement).desired_state["precondition_end"] -= timedelta(minutes=20)
    history = record_plan(record_plan({}, to_jsonable(original)), to_jsonable(replacement))
    restored_tail = tariff_tail_plan(10, at=NOW + timedelta(seconds=15))
    history = record_plan(history, to_jsonable(restored_tail))
    history = record_outcome(history, to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=preparation_action(original).desired_state,
        attempted_at=NOW + timedelta(seconds=20),
    )))
    assert history["pending"]["fulfilled"]
    assert "last_missed" not in history
    assert "confirmation" not in history["pending"]


@pytest.mark.parametrize("minutes", [10, 30])
def test_same_lifecycle_deadline_revision_updates_replacement_descriptor(minutes):
    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    history = record_plan({}, to_jsonable(original))
    original_descriptor = deepcopy(history["pending"]["preparation"])
    revised = deepcopy(original)
    deadline = NOW.replace(hour=5) - timedelta(minutes=minutes)
    preparation_action(revised).desired_state["precondition_end"] = deadline
    preparation_action(replacement).desired_state["precondition_end"] = deadline
    history = record_plan(history, to_jsonable(revised))
    assert history["pending"]["preparation"]["deadline"] == deadline.isoformat()
    assert history["pending"]["preparation"] != original_descriptor
    history = record_plan(history, to_jsonable(replacement))
    confirmed = record_outcome(history, to_jsonable(outcome(
        OutcomeResult.APPLIED, desired_state=preparation_action(replacement).desired_state,
        attempted_at=replacement.created_at,
    )))
    assert "last_missed" not in confirmed
    assert confirmed["pending"]["fulfilled"]
    # Older persisted records must not be hydrated from a newer plan.
    legacy = record_plan({}, to_jsonable(original))
    legacy["pending"].pop("preparation")
    assert "preparation" not in record_plan(legacy, to_jsonable(revised))["pending"]


@pytest.mark.parametrize("coast", [False, True])
def test_persisted_away_off_takeover_time_does_not_hide_preparation_confirmation(coast):
    from custom_components.ha_energy_planner.hvac_adapter import HVACCommandResult
    from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction

    original, replacement = tariff_tail_plan(10), tariff_tail_plan(21, at=NOW + timedelta(seconds=10))
    history = record_plan(record_plan({}, to_jsonable(original)), to_jsonable(replacement))
    takeover = NOW - timedelta(hours=1)
    previous = {"hvac_control": {
        "phase": "away_off", "started_at": takeover,
        "main_state": {"hvac_mode": "off", "target_temperature": 24},
    }}
    desired = preparation_action(replacement).desired_state
    transaction = HVACOwnershipTransaction(previous, replacement.created_at)
    provisional = transaction.prepare(desired, {}, {}, {})
    completed = transaction.complete(
        HVACCommandResult(True, "hvac_action_applied", {}, {}, {}), provisional, desired,
        main_state_superseded=False, superseded_zone_entity_ids=set(),
    )
    control = completed["hvac_control"]
    assert control["started_at"] == takeover
    assert control["phase_started_at"] == replacement.created_at
    assert control["preconditioning_started_at"] == replacement.created_at
    if coast:
        control.update(phase="peak_coast", phase_started_at=NOW.replace(hour=5), precondition_end=NOW.replace(hour=5))
    later = plan([])
    later.created_at = NOW + timedelta(hours=3)
    # Round-trip the ownership data as it is persisted, without any outcome.
    assert record_plan(history, to_jsonable(later), to_jsonable(control)) == {}
    if not coast:
        pending_original = record_plan({}, to_jsonable(original))
        assert record_plan(pending_original, to_jsonable(later), to_jsonable(control)) == {}


def test_confirmed_preparation_start_survives_continuation_and_resets_on_new_lifecycle():
    from custom_components.ha_energy_planner.hvac_adapter import HVACCommandResult
    from custom_components.ha_energy_planner.hvac_control import HVACOwnershipTransaction

    desired = preparation_action(tariff_tail_plan(10)).desired_state
    ownership = {}
    first = None
    for minute in range(3):
        now = NOW + timedelta(minutes=minute)
        if minute == 2:
            desired = {**desired, "lifecycle_id": "replacement"}
        transaction = HVACOwnershipTransaction(deepcopy(ownership), now)
        provisional = transaction.prepare(desired, {}, {}, {"hvac_mode": "off", "target_temperature": 24})
        ownership = transaction.complete(
            HVACCommandResult(True, "hvac_action_applied", {}, {}, {}), provisional, desired,
            main_state_superseded=False, superseded_zone_entity_ids=set(),
        )
        assert ownership["hvac_control"]["preconditioning_started_at"] == (now if minute == 2 else NOW)
        if minute == 0:
            first = deepcopy(ownership)
    assert first["hvac_control"]["preconditioning_started_at"] == NOW


@pytest.mark.parametrize("confirmation", ["outcome", "ownership", "none", "before_episode"])
def test_repeated_replacements_retain_the_original_preparation_window(confirmation):
    original = tariff_tail_plan(10)
    history = record_plan({}, to_jsonable(original))
    for seconds, tail in ((10, 21), (20, 7), (30, 13)):
        replacement = tariff_tail_plan(tail, at=NOW + timedelta(seconds=seconds))
        history = record_plan(history, to_jsonable(replacement))
        assert not history["pending"]["fulfilled"]
        assert history["pending"]["start"] == replacement.created_at.isoformat()
        assert history["pending"]["episode_start"] == NOW.isoformat()
    previous = deepcopy(history)
    at = NOW + timedelta(seconds=-1 if confirmation == "before_episode" else 1)
    if confirmation in {"outcome", "before_episode"}:
        history = record_outcome(history, to_jsonable(outcome(
            OutcomeResult.APPLIED, desired_state=preparation_action(original).desired_state, attempted_at=at,
        )))
    elif confirmation == "ownership":
        control = {
            **to_jsonable(preparation_action(original).desired_state),
            "main_state_committed": True, "preconditioning_started_at": at.isoformat(),
        }
        history = record_plan(history, to_jsonable(replacement), control)
    expected = confirmation in {"outcome", "ownership"}
    assert history["pending"]["fulfilled"] is expected
    assert ("last_missed" not in history) is expected
    assert not previous["pending"]["fulfilled"]
