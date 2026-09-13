"""Physical, economic and failure invariants for bounded EV planning."""

from datetime import UTC, datetime, timedelta
from itertools import product
from time import perf_counter
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.ev import EVChargeSchedule
from custom_components.ha_energy_planner.ev_optimization import _battery_cost, _deliver, optimise_ev
from custom_components.ha_energy_planner.ev_policy import finite, power_capability, strategy
from custom_components.ha_energy_planner.ev_telemetry import measured, update_ev_telemetry
from custom_components.ha_energy_planner.models import DecisionContext, DecisionSlot, InputHealth, OccupancyState

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def context(prices=(0.2, 0.1, 0.3, 0.4), *, load=1, pv=0):
    return DecisionContext(
        NOW,
        "test",
        [DecisionSlot(NOW + timedelta(minutes=5 * i), p, 0.05, pv, load) for i, p in enumerate(prices)],
        None,
        40,
        OccupancyState.UNKNOWN,
        InputHealth.HEALTHY,
        ev_connected=True,
        ev_charging=False,
        ev_target_soc_percent=45,
    )


def solve(ctx, **options):
    settings = {
        **DEFAULT_OPTIONS,
        "ev_charge_rate_kw": 6,
        "ev_soc_per_kwh": 10,
        "ev_readiness_buffer_minutes": 0,
        **options,
    }
    target = ctx.ev_target_soc_percent
    return optimise_ev(
        ctx,
        settings,
        target=target,
        ready_by=NOW + timedelta(minutes=5 * len(ctx.slots)),
        earliest_start=NOW,
        charge_rate_kw=6,
        soc_per_kwh=10,
        standard=EVChargeSchedule([], target, 40, target - 40, True, "test"),
    )


def number(unit="kW", current=6, low=1, high=6, step=1):
    return SimpleNamespace(
        entity_id="number.charger",
        state=str(current),
        attributes={"unit_of_measurement": unit, "min": low, "max": high, "step": step},
    )


def test_capacity_is_part_of_allocation():
    ctx = context((0.01, 0.2, 0.3))
    ctx.slots[0].baseline_load_forecast_upper_kw = 9
    schedule, evidence = solve(ctx)
    assert schedule.allocations[0].valid_at == NOW + timedelta(minutes=5)
    assert evidence["capacity_excluded_slots"] == 1
    assert evidence["search_status"] == "valid_schedule"


def test_partial_current_and_deadline_do_not_create_energy():
    ctx = context((0.01, 0.2))
    ctx.created_at = NOW + timedelta(minutes=3)
    schedule, _ = solve(ctx)
    assert len(schedule.allocations) == 2
    assert schedule.allocations[0].added_soc_percent == pytest.approx(2)
    assert sum(a.added_soc_percent for a in schedule.allocations) == pytest.approx(5)


def test_buffer_is_soft_but_earlier_completion_wins():
    ctx = context((0.4, 0.3, 0.2, 0.01))
    schedule, evidence = solve(ctx, ev_readiness_buffer_minutes=15)
    assert schedule.allocations[0].valid_at == NOW
    assert evidence["readiness_margin_minutes"] == pytest.approx(15)
    schedule, evidence = solve(ctx, ev_readiness_buffer_minutes=60)
    assert not schedule.infeasible
    assert evidence["search_status"] == "valid_schedule"


def test_continuous_matches_exhaustive_single_slot_oracle():
    for prices in product((-0.1, 0.1, 1), repeat=4):
        schedule, evidence = solve(context(prices))
        assert schedule.allocations[0].valid_at == NOW + timedelta(minutes=5 * prices.index(min(prices)))
        assert evidence["modelled_incremental_cost"] == pytest.approx(min(prices) * 0.5)


def test_variable_power_uses_supported_steps_and_capacity():
    ctx = context((0.1, 0.2, 0.3), load=7)
    ctx.ev_evidence["power_capability"] = power_capability(number(), {"ev_limit_min": 1, "ev_limit_max": 6})
    ctx.ev_evidence["power_limit_mapped"] = True
    schedule, evidence = solve(ctx)
    assert not schedule.infeasible
    assert max(evidence["physical_power_by_time"].values()) <= 3
    ctx.ev_evidence["power_capability"] = None
    schedule, evidence = solve(ctx)
    assert schedule.infeasible
    assert not schedule.allocations


def test_emergency_only_when_normal_prices_cannot_meet_target():
    ctx = context((0.5, 0.1))
    schedule, evidence = solve(
        ctx,
        ev_price_limit_enabled=True,
        ev_max_import_price=0.2,
        ev_price_policy="departure_priority",
        ev_emergency_price=0.6,
        ev_emergency_budget=0.5,
    )
    assert schedule.allocations[0].import_price == 0.1
    assert evidence["planned_emergency_extra"] == 0
    ctx.ev_target_soc_percent = 50
    schedule, evidence = solve(
        ctx,
        ev_price_limit_enabled=True,
        ev_max_import_price=0.2,
        ev_price_policy="departure_priority",
        ev_emergency_price=0.6,
        ev_emergency_budget=0.15,
    )
    assert not schedule.infeasible
    assert evidence["planned_emergency_extra"] == pytest.approx(0.15)
    ctx.ev_evidence["emergency_spend"] = 0.15
    schedule, evidence = solve(
        ctx,
        ev_price_limit_enabled=True,
        ev_max_import_price=0.2,
        ev_price_policy="departure_priority",
        ev_emergency_price=0.6,
        ev_emergency_budget=0.15,
    )
    assert schedule.infeasible
    assert evidence["emergency_budget_remaining"] == 0


def test_retained_plan_requires_meaningful_savings():
    ctx = context((0.2, 0.1, 0.3))
    ctx.ev_evidence["retained_schedule"] = [{"valid_at": NOW.isoformat(), "physical_power_kw": 6}]
    schedule, evidence = solve(ctx)
    assert schedule.allocations[0].valid_at == NOW
    assert evidence["schedule_change_reason"] == "saving_below_schedule_change_threshold"
    ctx.slots[0].baseline_load_forecast_upper_kw = 9
    schedule, _ = solve(ctx)
    assert schedule.allocations[0].valid_at != NOW


def test_battery_reserve_and_opportunity_cost():
    ctx = context((0.1, 1, 1, 1), pv=0)
    ctx.slots[0].pv_forecast_kw = 7
    ctx.current_battery_soc_percent = 10
    ctx.current_enphase_profile = ctx.enphase_self_consumption_profile = "self"
    baseline, terminal, reason = _battery_cost(ctx, DEFAULT_OPTIONS, {}, timedelta(minutes=5))
    candidate, _, _ = _battery_cost(ctx, DEFAULT_OPTIONS, {0: 0.5}, timedelta(minutes=5))
    assert reason == "observed_profile_simulation"
    assert terminal > 0
    assert candidate > baseline
    ctx.current_enphase_profile = "opaque ai"
    assert _battery_cost(ctx, DEFAULT_OPTIONS, {}, timedelta(minutes=5))[2] == "battery_profile_or_model_unavailable"
    ctx.current_enphase_profile = ctx.enphase_full_backup_profile = "backup"
    assert _battery_cost(ctx, DEFAULT_OPTIONS, {}, timedelta(minutes=5))[2] == "observed_profile_simulation"
    ctx.slots[-1].import_price = None
    assert _battery_cost(ctx, DEFAULT_OPTIONS, {}, timedelta(minutes=5))[2] == "battery_forecast_incomplete"


def test_band_rates_integrate_across_boundaries():
    ctx = context()
    ctx.ev_evidence["performance"] = {
        "aggregate": {"minutes": 60, "soc_per_kwh": 10},
        "bands": {"80": {"sessions": 3, "minutes": 60, "soc_per_kwh": 5}},
    }
    assert _deliver(ctx, 79, 1, 10, expected=True) == pytest.approx(84.5)
    assert _deliver(ctx, 99, 10, 10) == 100
    ctx.ev_evidence["performance"]["bands"]["80"]["sessions"] = 2
    assert _deliver(ctx, 80, 1, 10, expected=True) == 90


def test_forecast_gap_distinguished_from_capacity_shortfall():
    ctx = context((0.1, None))
    ctx.ev_target_soc_percent = 90
    schedule, evidence = solve(ctx)
    assert schedule.infeasible
    assert evidence["search_status"] == "forecast_coverage_insufficient"
    ctx.slots[1].import_price = 0.1
    schedule, evidence = solve(ctx)
    assert evidence["search_status"] == "capacity_or_price_shortfall"


def test_maximum_horizon_is_bounded_and_deterministic():
    ctx = context(tuple(0.1 + (i % 24) / 100 for i in range(576)))
    ctx.ev_target_soc_percent = 90
    start = perf_counter()
    first = solve(ctx, ev_charging_strategy="adaptive")
    assert perf_counter() - start < 5
    second = solve(ctx, ev_charging_strategy="adaptive")
    assert first == second
    assert first[1]["candidate_evaluations"] <= 2000
    assert not first[0].infeasible


@pytest.mark.parametrize("value", [None, True, "bad", float("nan"), float("inf")])
def test_finite_rejects_invalid(value):
    assert finite(value) is None


@pytest.mark.parametrize("unit,factor,minimum,maximum", [("A", 0.75, 6, 32), ("W", 0.001, 1000, 6000), ("kW", 1, 1, 6)])
def test_power_unit_conversion(unit, factor, minimum, maximum):
    cap = power_capability(
        number(unit, low=minimum, high=maximum),
        {"ev_limit_min": minimum, "ev_limit_max": maximum, "ev_voltage": 250, "ev_phases": 3},
    )
    assert cap.kw_per_unit == factor
    assert cap.power(minimum * factor - 0.01) == 0
    assert cap.power(maximum * factor + 1) == pytest.approx(maximum * factor)


@pytest.mark.parametrize(
    "state,options",
    [
        (None, {}),
        (number("watts"), {"ev_limit_min": 1, "ev_limit_max": 6}),
        (number(step=0), {"ev_limit_min": 1, "ev_limit_max": 6}),
        (number(), {"ev_limit_min": 7, "ev_limit_max": 6}),
        (number(), {}),
        (number("A"), {"ev_limit_min": 1, "ev_limit_max": 6, "ev_voltage": 240, "ev_phases": 2}),
    ],
)
def test_invalid_capabilities_are_rejected(state, options):
    assert power_capability(state, options) is None


def test_strategy_migration():
    assert strategy({"ev_continuous_charging": False}) == "split"
    assert strategy({}) == "continuous"
    assert strategy({"ev_charging_strategy": "adaptive"}) == "adaptive"


def test_measured_units_and_staleness():
    state = SimpleNamespace(state="7000", attributes={"unit_of_measurement": "W"}, last_updated=NOW)
    assert measured(state, NOW, "power") == 7
    assert measured(state, NOW + timedelta(minutes=11), "power") is None
    assert measured(None, NOW, "power") is None
    state.attributes["unit_of_measurement"] = "Wh"
    assert measured(state, NOW, "energy") == 7
    state.state = "unknown"
    assert measured(state, NOW, "energy") is None


def sample(at=NOW, **values):
    return {
        "identity": [None] * 5 + [6, 1, 6, 250, 1],
        "at": at.isoformat(),
        "soc": 40,
        "connected": True,
        "charging": True,
        "power_kw": 6,
        "energy_kwh": 0,
        "energy_reported_at": at.isoformat(),
        "power_mapped": True,
        "price": 0.5,
        "normal_ceiling": 0.2,
        **values,
    }


def test_session_budget_survives_restart_and_unknown_disconnect():
    record = update_ev_telemetry({}, sample(), reserved_kw=6)
    record = update_ev_telemetry(
        record, sample(NOW + timedelta(minutes=5), connected=None, soc=45, energy_kwh=0.5), reserved_kw=6
    )
    assert record["emergency_spend"] == pytest.approx(0.15)
    changed = sample(NOW + timedelta(minutes=10), connected=None)
    changed["identity"] = ["changed"] * 10
    record = update_ev_telemetry(record, changed)
    assert record["emergency_spend"] >= 0.15
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=15), connected=False, charging=False))
    assert record["emergency_spend"] == 0


def test_measured_session_training_and_stall():
    record = update_ev_telemetry({}, sample(), reserved_kw=6)
    for index in range(1, 13):
        record = update_ev_telemetry(
            record, sample(NOW + timedelta(minutes=5 * index), soc=40 + index, energy_kwh=0.5 * index), reserved_kw=6
        )
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=65), charging=False, energy_kwh=6, soc=52))
    aggregate = record["performance"]["aggregate"]
    assert aggregate["minutes"] == 60
    assert aggregate["soc_per_kwh"] == pytest.approx(2)
    assert aggregate["sessions"] == 1
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=70), power_kw=0))
    assert record["delivery_status"] == "stalled"
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=75), power_kw=None, energy_kwh=None))
    assert record["delivery_status"] == "unavailable"


def test_bounded_search_retains_feasible_candidate_when_budget_exhausts(monkeypatch):
    from custom_components.ha_energy_planner import ev_optimization

    monkeypatch.setattr(ev_optimization, "MAX_EVALUATIONS", 2)
    schedule, evidence = solve(context((0.4, 0.1, 0.2, 0.3)))
    assert not schedule.infeasible
    assert schedule.allocations[0].valid_at == NOW
    assert evidence["candidate_evaluations"] == 2


def test_stale_and_off_step_retained_setpoints_are_revalidated():
    for retained_power in (7, 2, 3.2):
        ctx = context()
        ctx.ev_evidence["retained_schedule"] = [{"valid_at": NOW.isoformat(), "physical_power_kw": retained_power}]
        if retained_power == 3.2:
            ctx.ev_evidence["power_capability"] = power_capability(number(), {"ev_limit_min": 1, "ev_limit_max": 6})
        schedule, evidence = solve(ctx)
        assert not schedule.infeasible
        assert evidence["schedule_change_reason"] == "candidate_selected"


def test_adaptive_dwell_retains_safe_current_state_but_not_a_missed_deadline():
    ctx = context((0.1, 0.2, 0.3, 0.4))
    ctx.ev_evidence["last_transition_at"] = NOW.isoformat()
    schedule, evidence = solve(ctx, ev_charging_strategy="adaptive")
    assert schedule.allocations[0].valid_at > NOW
    assert evidence["schedule_change_reason"] == "minimum_dwell_active"
    ctx.ev_target_soc_percent = 60
    schedule, evidence = solve(ctx, ev_charging_strategy="adaptive")
    assert not schedule.infeasible
    assert schedule.allocations[0].valid_at == NOW


def test_gap_coverage_and_expired_battery_slots():
    ctx = context((0.2, 0.1, 0.3, 0.4))
    ctx.slots[1].valid_at += timedelta(minutes=2)
    ctx.ev_target_soc_percent = 90
    assert solve(ctx)[1]["search_status"] == "forecast_coverage_insufficient"
    ctx.current_enphase_profile = ctx.enphase_self_consumption_profile = "self"
    ctx.current_battery_soc_percent = 30
    ctx.created_at += timedelta(minutes=6)
    schedule, evidence = solve(ctx)
    assert evidence["battery_cost_reason"] == "observed_profile_simulation"
    assert all(a.valid_at >= NOW + timedelta(minutes=5) for a in schedule.allocations)


def test_measured_delivery_changes_duration_and_reports_zero_delivery():
    from custom_components.ha_energy_planner.ev_optimization import _charge_interval

    ctx = context()
    ctx.ev_evidence["performance"] = {
        "aggregate": {
            "minutes": 60,
            "soc_per_kwh": 10,
            "delivery_fraction": 0.5,
        }
    }
    soc, energy, hours = _charge_interval(ctx, 40, 45, 6, 1 / 12, 10, expected=True)
    assert soc == 42.5 and energy == 0.25 and hours == pytest.approx(1 / 12)
    ctx.ev_evidence["performance"]["aggregate"]["delivery_fraction"] = 0
    assert _charge_interval(ctx, 40, 45, 6, 1 / 12, 10) == (40, 0, 0)


def test_variable_search_preserves_physics_across_diverse_cost_and_solar_shapes():
    import random

    randomizer = random.Random(17)
    for _ in range(80):
        count = randomizer.choice((6, 12, 24))
        ctx = context(tuple(randomizer.choice((-0.1, 0.05, 0.2, 0.8)) for _ in range(count)))
        ctx.ev_target_soc_percent = randomizer.choice((43, 45, 48, 52, 60))
        for slot in ctx.slots:
            slot.pv_forecast_kw = randomizer.choice((0, 2, 4, 8))
            slot.baseline_load_forecast_upper_kw = randomizer.choice((1, 3, 5, 8))
            slot.carbon_intensity_g_per_kwh = randomizer.choice((10, 100, 500))
        ctx.ev_evidence["power_capability"] = power_capability(number(), {"ev_limit_min": 1, "ev_limit_max": 6})
        schedule, evidence = solve(ctx, ev_charging_strategy="adaptive", max_daily_ev_actions=0)
        assert evidence["candidate_evaluations"] <= 2000
        assert sum(a.added_soc_percent for a in schedule.allocations) <= ctx.ev_target_soc_percent - 40 + 1e-6
        for allocation in schedule.allocations:
            slot = next(s for s in ctx.slots if s.valid_at == allocation.valid_at)
            physical = evidence["physical_power_by_time"][slot.valid_at.isoformat()]
            assert physical <= 10 - slot.baseline_load_forecast_upper_kw + slot.pv_forecast_kw + 1e-6
            assert physical == int(physical)
            assert allocation.charge_kw <= physical + 1e-6


def test_power_fallback_and_energy_counter_reset_do_not_invent_learning():
    record = update_ev_telemetry({}, sample(energy_kwh=None), reserved_kw=6)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=5), soc=41, energy_kwh=None), reserved_kw=6)
    assert record["pending"]["aggregate"]["energy"] == pytest.approx(0.5)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=6), charging=False), reserved_kw=6)
    assert record["performance"]["aggregate"]["soc_per_kwh"] == pytest.approx(2)
    record = update_ev_telemetry({}, sample(energy_kwh=10), reserved_kw=6)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=5), soc=41, energy_kwh=0), reserved_kw=6)
    assert not record.get("pending")
    record["pending"] = {"aggregate": {"energy": 0.1, "gain": 0, "minutes": 1}}
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=6), charging=False))
    assert not record.get("performance", {}).get("aggregate")


def test_unknown_battery_profile_uses_conservative_supported_scenarios():
    ctx = context()
    ctx.current_enphase_profile = "opaque AI"
    ctx.enphase_self_consumption_profile = "self"
    ctx.enphase_full_backup_profile = "backup"
    ctx.current_battery_soc_percent = 70
    schedule, evidence = solve(ctx)
    assert not schedule.infeasible
    assert evidence["battery_cost_reason"] == "uncertain_profile_conservative_scenarios"


def test_corrupt_spend_and_incompatible_learning_never_grant_new_budget():
    for record in (
        {"version": 99, "emergency_spend": 2},
        {"version": 1, "emergency_spend": "bad", "performance": [], "pending": [], "last_sample": []},
        {"version": 1, "budget_uncertain": True},
    ):
        updated = update_ev_telemetry(record, sample())
        assert updated["budget_uncertain"]
    ctx = context((0.4,))
    ctx.ev_evidence["budget_uncertain"] = True
    assert solve(
        ctx,
        ev_price_limit_enabled=True,
        ev_max_import_price=0.2,
        ev_price_policy="departure_priority",
        ev_emergency_price=1,
        ev_emergency_budget=5,
    )[0].infeasible


def test_active_stall_does_not_credit_configured_power_in_current_slot():
    ctx = context((0.1, 0.2, 0.3))
    ctx.ev_charging = True
    ctx.ev_evidence["delivery_status"] = "stalled"
    schedule, evidence = solve(ctx)
    assert schedule.allocations[0].added_soc_percent == 0
    assert evidence["conservative_completion"] == (NOW + timedelta(minutes=10)).isoformat()


def test_planner_emits_safe_physical_limit_for_active_manual_continuation():
    from custom_components.ha_energy_planner.planner import DryRunPlanner

    ctx = context()
    ctx.ev_evidence["power_capability"] = power_capability(number(), {"ev_limit_min": 1, "ev_limit_max": 6})
    planner = DryRunPlanner({**DEFAULT_OPTIONS, "ev_charge_rate_kw": 6})
    evidence = {"physical_power_by_time": {}}
    assert planner._ev_power_command(ctx, evidence, True)["power_limit"]["value"] == 6
    evidence["physical_power_by_time"][NOW.isoformat()] = 3
    assert planner._ev_power_command(ctx, evidence, True)["power_limit"]["physical_power_kw"] == 3
    evidence["physical_power_by_time"][NOW.isoformat()] = 0
    assert planner._ev_power_command(ctx, evidence, True) == {}


def test_block_move_escapes_a_local_minimum_without_exceeding_action_limit():
    ctx = context((5, 5, 5, 9, 3, 3, 3, 9, 0, 9, 0, 9))
    ctx.ev_target_soc_percent = 55
    schedule, evidence = solve(ctx, ev_charging_strategy="split", max_daily_ev_actions=2)
    assert [a.valid_at for a in schedule.allocations] == [NOW + timedelta(minutes=5 * i) for i in (4, 5, 6)]
    assert evidence["planned_transitions"] == 2


def test_positive_but_insufficient_emergency_budget_cannot_buy_a_full_slot():
    schedule, evidence = solve(
        context((0.4,)),
        ev_price_limit_enabled=True,
        ev_max_import_price=0.2,
        ev_price_policy="departure_priority",
        ev_emergency_price=0.5,
        ev_emergency_budget=0.01,
    )
    assert schedule.infeasible
    assert evidence["planned_emergency_extra"] <= 0.01


def test_adaptive_future_pauses_prefer_dwell_without_sacrificing_readiness():
    ctx = context((0.01, 1, 0.01, 1, 0.01, 1, 1, 1))
    ctx.ev_target_soc_percent = 55
    schedule, evidence = solve(ctx, ev_charging_strategy="adaptive", max_daily_ev_actions=0)
    assert evidence["readiness_dwell_exceptions"] == 0
    assert not schedule.infeasible


@pytest.mark.parametrize("variable", [False, True])
def test_full_horizon_battery_economics_stays_within_execution_budget(variable):
    ctx = context(tuple(0.1 + (i % 24) / 100 for i in range(576)), pv=2)
    ctx.ev_target_soc_percent = 90
    ctx.current_battery_soc_percent = 60
    ctx.current_enphase_profile = ctx.enphase_self_consumption_profile = "self"
    if variable:
        ctx.ev_evidence["power_capability"] = power_capability(number(), {"ev_limit_min": 1, "ev_limit_max": 6})
    started = perf_counter()
    schedule, evidence = solve(ctx, ev_charging_strategy="adaptive", max_daily_ev_actions=0)
    assert perf_counter() - started < 5
    assert evidence["candidate_evaluations"] <= 2000
    assert not schedule.infeasible


def test_cross_band_measurements_only_update_aggregate():
    record = update_ev_telemetry({}, sample(soc=59, energy_kwh=0), reserved_kw=6)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=5), soc=61, energy_kwh=0.5), reserved_kw=6)
    assert set(record["pending"]) == {"aggregate"}


def test_duplicate_retained_candidate_still_enforces_both_savings_thresholds():
    ctx = context((0.4, 0.1, 0.2))
    ctx.ev_evidence["retained_schedule"] = [{"valid_at": NOW.isoformat(), "physical_power_kw": 6}]
    schedule, evidence = solve(ctx)
    assert schedule.allocations[0].valid_at == NOW
    assert evidence["schedule_change_reason"] == "saving_below_schedule_change_threshold"
    assert evidence["retained_schedule_saving"] == pytest.approx(0.15)
    assert solve(ctx, ev_schedule_min_saving=0.1)[0].allocations[0].valid_at == NOW + timedelta(minutes=5)


def test_adaptive_active_charger_has_incumbent_even_without_stored_schedule():
    ctx = context((0.4, 0.1, 0.2))
    ctx.ev_charging = True
    schedule, evidence = solve(ctx, ev_charging_strategy="adaptive")
    assert schedule.allocations[0].valid_at == NOW
    assert evidence["schedule_change_reason"] == "saving_below_schedule_change_threshold"


def test_metered_spending_settles_aligned_energy_at_observed_price_without_solar_credit():
    record = update_ev_telemetry({}, sample(energy_kwh=0, price=0.4, normal_ceiling=0.2), reserved_kw=6)
    record["command_exposure"] = {"at": NOW.isoformat(), "cost_per_hour": 3}
    record["emergency_spend"] = 0.1
    updated = update_ev_telemetry(
        record, sample(NOW + timedelta(minutes=5), soc=41, energy_kwh=0.5, price=0.4, normal_ceiling=0.2), reserved_kw=6
    )
    assert updated["emergency_spend"] == pytest.approx(0.2)
    assert updated["spending_source"] == "measured_energy_conservative_grid"
    record["last_sample"]["energy_kwh"] = 1
    conservative = update_ev_telemetry(
        record, sample(NOW + timedelta(minutes=5), soc=41, energy_kwh=0.5, price=0.4, normal_ceiling=0.2), reserved_kw=6
    )
    assert conservative["emergency_spend"] == pytest.approx(0.35)


def test_mature_band_in_simulation_and_unavailable_opaque_battery_evidence():
    ctx = context()
    ctx.ev_evidence["performance"] = {"bands": {"0": {"sessions": 3, "minutes": 60, "soc_per_kwh": 10}}}
    assert not solve(ctx)[0].infeasible
    ctx.current_enphase_profile = "opaque"
    ctx.enphase_self_consumption_profile = "self"
    assert solve(ctx)[1]["battery_cost_reason"] == "battery_profile_or_model_unavailable"
    ctx.ev_target_soc_percent = 90
    ctx.ev_evidence["last_transition_at"] = (NOW - timedelta(hours=1)).isoformat()
    assert solve(ctx, ev_charging_strategy="adaptive")[0].infeasible


def test_energy_only_feedback_detects_stalls():
    record = update_ev_telemetry({}, sample(power_kw=None, power_mapped=False, energy_mapped=True), reserved_kw=6)
    record = update_ev_telemetry(
        record, sample(NOW + timedelta(minutes=5), power_kw=None, power_mapped=False, energy_mapped=True), reserved_kw=6
    )
    assert record["delivery_status"] == "stalled"
    record = update_ev_telemetry(
        record,
        sample(NOW + timedelta(minutes=6), power_kw=None, energy_kwh=None, power_mapped=False, energy_mapped=True),
        reserved_kw=6,
    )
    assert record["delivery_status"] == "unavailable"


def test_review_normal_price_block_prevents_unnecessary_emergency_spend():
    ctx = context((0.01, 1, 0.01, 1, 0.2, 0.2, 0.2, 1, 0.01, 1, 0.01, 1))
    ctx.ev_target_soc_percent = 55
    schedule, evidence = solve(ctx, ev_charging_strategy="split", max_daily_ev_actions=2,
                               ev_price_limit_enabled=True, ev_max_import_price=0.3,
                               ev_price_policy="departure_priority", ev_emergency_price=2, ev_emergency_budget=5,
                               ev_readiness_buffer_minutes=45)
    assert not schedule.infeasible
    assert evidence["planned_emergency_extra"] == 0
    assert [a.valid_at for a in schedule.allocations] == [NOW + timedelta(minutes=5*i) for i in (4, 5, 6)]


def test_review_old_meter_report_never_refunds_commanded_spending():
    record = update_ev_telemetry({}, sample(energy_kwh=0, energy_reported_at=NOW.isoformat(),
                                           price=0.4, normal_ceiling=0.2), reserved_kw=6)
    record["command_exposure"] = {"at": NOW.isoformat(), "cost_per_hour": 3}
    updated = update_ev_telemetry(record, sample(NOW + timedelta(minutes=5), energy_kwh=0,
                                               energy_reported_at=NOW.isoformat(),
                                               price=0.4, normal_ceiling=0.2), reserved_kw=6)
    assert updated["emergency_spend"] == pytest.approx(0.25)


def test_review_reduced_setpoint_is_not_learned_as_charger_underperformance():
    record = update_ev_telemetry({}, sample(commanded_kw=3, power_kw=3), reserved_kw=3)
    for index in range(1, 13):
        record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=index*5), commanded_kw=3,
                                                   power_kw=3, energy_kwh=index*0.25, soc=40+index), reserved_kw=3)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=65), charging=False), reserved_kw=3)
    assert record["performance"]["aggregate"]["delivery_fraction"] == 1


def test_metered_spending_retains_the_unobserved_tail_and_staggered_learning():
    record = update_ev_telemetry({}, sample(energy_kwh=0, commanded_kw=6,
        energy_reported_at=(NOW - timedelta(minutes=1)).isoformat(), price=0.4, normal_ceiling=0.2), reserved_kw=6)
    record["command_exposure"] = {"at": NOW.isoformat(), "cost_per_hour": 3}
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=5), energy_kwh=0.2, soc=42,
        energy_reported_at=(NOW + timedelta(minutes=4)).isoformat(), price=0.4, normal_ceiling=0.2), reserved_kw=6)
    assert record["emergency_spend"] == pytest.approx(0.09)
    assert record["pending"]["aggregate"]["energy"] == pytest.approx(0.2)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=10), energy_kwh=0.2, soc=42,
        energy_reported_at=(NOW + timedelta(minutes=4)).isoformat(), price=0.4, normal_ceiling=0.2), reserved_kw=6)
    assert record["emergency_spend"] == pytest.approx(0.34)


def test_retained_window_cannot_interrupt_an_active_continuous_session():
    ctx = context((0.4, 0.1, 0.2))
    ctx.ev_charging = True
    ctx.ev_evidence["retained_schedule"] = [
        {"valid_at": ctx.slots[1].valid_at.isoformat(), "physical_power_kw": 6}]
    schedule, _ = solve(ctx, ev_charging_strategy="continuous")
    assert schedule.allocations[0].valid_at == NOW


@pytest.mark.parametrize("selected_strategy", ["split", "adaptive"])
def test_low_price_force_current_applies_to_every_candidate(selected_strategy):
    ctx = context((0.2, 0.1, 0.3))
    schedule, _ = optimise_ev(
        ctx, {**DEFAULT_OPTIONS, "ev_charging_strategy": selected_strategy, "ev_readiness_buffer_minutes": 0},
        target=45, ready_by=NOW + timedelta(minutes=15), earliest_start=NOW,
        charge_rate_kw=6, soc_per_kwh=10, standard=EVChargeSchedule([], 45, 40, 5, True, "test"),
        force_current=True,
    )
    assert schedule.allocations[0].valid_at == NOW


def test_measured_stall_minutes_reduce_learned_session_delivery():
    record = update_ev_telemetry({}, sample(power_kw=0), reserved_kw=6)
    for index in range(1, 13):
        delivered = max(index - 6, 0) * 0.5
        record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=index*5),
            power_kw=0 if index <= 6 else 6, energy_kwh=delivered, soc=40+delivered*5), reserved_kw=6)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=65), charging=False), reserved_kw=6)
    aggregate = record["performance"]["aggregate"]
    assert aggregate["minutes"] == pytest.approx(60)
    assert aggregate["delivery_fraction"] == pytest.approx(0.5)


@pytest.mark.parametrize("minutes, expected", [(5, "configured_or_recorder_calibration"), (60, "measured")])
def test_charging_model_source_requires_sufficient_observations(minutes, expected):
    ctx = context()
    ctx.ev_evidence["performance"] = {"aggregate": {"minutes": minutes, "soc_per_kwh": 8,
                                                    "delivery_fraction": 1}}
    assert solve(ctx)[1]["charging_model_source"] == expected


def test_zero_energy_with_soc_jump_is_not_a_valid_stall_interval():
    record = update_ev_telemetry({}, sample(power_kw=0), reserved_kw=6)
    record = update_ev_telemetry(record, sample(NOW + timedelta(minutes=5), power_kw=0, soc=45), reserved_kw=6)
    assert not record["pending"]


def test_retained_night_window_cannot_override_selected_daylight_preference():
    from custom_components.ha_energy_planner.models import DaylightWindow

    ctx = context((0.01, 0.4, 0.4))
    ctx.daylight_windows = [DaylightWindow(NOW + timedelta(minutes=5), NOW + timedelta(minutes=10))]
    ctx.ev_evidence["retained_schedule"] = [
        {"valid_at": NOW.isoformat(), "physical_power_kw": 6}]
    schedule, evidence = solve(ctx, ev_charging_strategy="split", ev_daylight_lowest_cost_charging_enabled=True)
    assert schedule.allocations[0].valid_at == NOW + timedelta(minutes=5)
    assert evidence["schedule_change_reason"] != "saving_below_schedule_change_threshold"
