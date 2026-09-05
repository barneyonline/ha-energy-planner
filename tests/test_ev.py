"""Tests for EV history, calibration, and charging allocation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from custom_components.ha_energy_planner.ev import (
    _best_continuous_slots,
    _charge_cost_components,
    _effective_charge_price,
    _float_or_none,
    _solar_surplus_kw,
    _state_timestamp,
    allocate_least_cost_charging,
    build_ev_charge_calibration,
    effective_ev_soc_per_kwh,
    ev_charging_state,
    ev_charging_state_proves_safe,
)
from custom_components.ha_energy_planner.models import DecisionSlot


@pytest.mark.parametrize("required_soc", [5.0, 7.5, 10.0, 12.5])
@pytest.mark.parametrize("pv", [(0, 0, 0, 0), (0, 4, 8, 2), (8, 8, 8, 8)])
def test_continuous_schedule_matches_exhaustive_energy_cost_oracle(required_soc, pv) -> None:
    """An independent money calculation catches rank sums and partial-slot errors."""
    now = datetime(2026, 9, 5, tzinfo=UTC)
    for prices in product((-0.10, 0.10, 1.00), repeat=4):
        slots = [
            DecisionSlot(now + timedelta(minutes=5 * index), price, 0.05, pv[index], 1.0)
            for index, price in enumerate(prices)
        ]
        schedule = allocate_least_cost_charging(
            slots, current_soc_percent=40, target_soc_percent=40 + required_soc,
            ready_by=now + timedelta(minutes=20), charge_rate_kw=6, soc_per_kwh=10,
            interval_minutes=5, continuous=True,
        )
        count = int((required_soc + 4.999999) // 5)

        def actual_cost(start, count=count, prices=prices):
            remaining_kwh = required_soc / 10
            cost = 0.0
            for index in range(start, start + count):
                energy = min(0.5, remaining_kwh)
                solar_energy = min(energy, max(pv[index] - 1, 0) / 12)
                cost += solar_energy * 0.05 + (energy - solar_energy) * prices[index]
                remaining_kwh -= energy
            return round(cost, 9)

        cheapest_start = min(range(5 - count), key=lambda start: (actual_cost(start), start))
        selected = [int((item.valid_at - now).total_seconds() / 300) for item in schedule.allocations]
        assert selected == list(range(cheapest_start, cheapest_start + count)), (prices, pv, required_soc)
        assert schedule.infeasible is False
        assert sum(item.added_soc_percent for item in schedule.allocations) == pytest.approx(required_soc)


def test_continuous_window_compares_price_magnitudes_not_rank_sums() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    slots = [
        DecisionSlot(now + timedelta(minutes=5 * index), price, 0.05, 0, 1)
        for index, price in enumerate((0.01, 1.00, 0.40, 0.41))
    ]
    schedule = allocate_least_cost_charging(
        slots, current_soc_percent=40, target_soc_percent=50,
        ready_by=now + timedelta(minutes=20), charge_rate_kw=6, soc_per_kwh=10,
        interval_minutes=5, continuous=True,
    )
    assert [item.valid_at for item in schedule.allocations] == [slots[2].valid_at, slots[3].valid_at]
    assert sum(item.charge_kw / 12 * item.import_price for item in schedule.allocations) == pytest.approx(0.405)


@pytest.mark.parametrize("carbon_weight,expected_start", [(0, 0), (0.8, 2), (1, 2)])
def test_continuous_window_preserves_carbon_preference(carbon_weight, expected_start) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    slots = [
        DecisionSlot(
            now + timedelta(minutes=5 * index), price, 0.05, 0, 1,
            carbon_intensity_g_per_kwh=carbon,
        )
        for index, (price, carbon) in enumerate(((0.1, 800), (0.1, None), (0.3, 50), (0.3, 50)))
    ]
    schedule = allocate_least_cost_charging(
        slots, current_soc_percent=40, target_soc_percent=50,
        ready_by=now + timedelta(minutes=20), charge_rate_kw=6, soc_per_kwh=10,
        interval_minutes=5, continuous=True, carbon_weight=carbon_weight,
    )
    assert schedule.allocations[0].valid_at == slots[expected_start].valid_at


def test_ev_charging_state_distinguishes_suspended_power_from_connection() -> None:
    assert ev_charging_state("Charging") is True
    assert ev_charging_state("FINISHING") is True
    assert ev_charging_state("SUSPENDED_EV") is False
    assert ev_charging_state(" suspended_evse ") is False
    assert ev_charging_state("AVAILABLE") is False
    assert ev_charging_state("warming_up") is None
    assert ev_charging_state_proves_safe("SUSPENDED_EV") is True
    assert ev_charging_state_proves_safe("disconnected") is False
    assert ev_charging_state_proves_safe("AVAILABLE") is False


def test_allocate_least_cost_charging_uses_cheapest_slots_before_ready_by() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [
        DecisionSlot(now + timedelta(minutes=0), 0.50, 0.05, 0, 1),
        DecisionSlot(now + timedelta(minutes=5), 0.10, 0.05, 0, 1),
        DecisionSlot(now + timedelta(minutes=10), 0.20, 0.05, 0, 1),
        DecisionSlot(now + timedelta(minutes=15), 0.01, 0.05, 0, 1),
    ]
    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=50,
        ready_by=now + timedelta(minutes=20),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
    )
    assert schedule.infeasible is False
    assert [allocation.valid_at for allocation in schedule.allocations] == [
        now + timedelta(minutes=15),
        now + timedelta(minutes=5),
    ]
    assert [allocation.charge_kw for allocation in schedule.allocations] == [6, 6]


def test_continuous_charging_keeps_active_session_after_repricing() -> None:
    now = datetime(2026, 8, 17, 18, 35, tzinfo=UTC)
    slots = [
        DecisionSlot(now + timedelta(minutes=offset), price, 0.05, 0, 1)
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.10)]
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=64,
        target_soc_percent=74,
        ready_by=now + timedelta(minutes=15),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        continuous=True,
        continue_current=True,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [
        now,
        now + timedelta(minutes=5),
    ]
    assert schedule.infeasible is False


def test_continuous_session_does_not_bypass_price_limit() -> None:
    now = datetime(2026, 8, 17, 18, 35, tzinfo=UTC)
    slots = [
        DecisionSlot(now + timedelta(minutes=offset), price, 0.05, 0, 1)
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.10)]
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=64,
        target_soc_percent=74,
        ready_by=now + timedelta(minutes=15),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        continuous=True,
        continue_current=True,
        max_import_price=0.20,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [
        now + timedelta(minutes=5),
        now + timedelta(minutes=10),
    ]


def test_continuous_pre_window_session_stays_anchored_after_repricing() -> None:
    now = datetime(2026, 8, 17, 18, 35, tzinfo=UTC)
    earliest_start = now + timedelta(minutes=5)
    slots = [
        DecisionSlot(now + timedelta(minutes=offset), price, 0.05, 0, 1)
        for offset, price in [(0, 0.15), (5, 0.10), (10, 0.10)]
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=64,
        target_soc_percent=74,
        ready_by=now + timedelta(minutes=15),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        earliest_start=earliest_start,
        continuous=True,
        continue_current=True,
        max_import_price=0.20,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [
        now,
        earliest_start,
    ]


def test_continuous_pre_window_session_still_honors_price_limit() -> None:
    now = datetime(2026, 8, 17, 18, 35, tzinfo=UTC)
    earliest_start = now + timedelta(minutes=5)
    slots = [
        DecisionSlot(now + timedelta(minutes=offset), price, 0.05, 0, 1)
        for offset, price in [(0, 0.50), (5, 0.10), (10, 0.10)]
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=64,
        target_soc_percent=74,
        ready_by=now + timedelta(minutes=15),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        earliest_start=earliest_start,
        continuous=True,
        continue_current=True,
        max_import_price=0.20,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [
        earliest_start,
        now + timedelta(minutes=10),
    ]


def test_allocate_least_cost_charging_prefers_solar_surplus_effective_cost() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [
        DecisionSlot(now, 0.10, 0.05, 0.0, 2.0),
        DecisionSlot(now + timedelta(minutes=5), 0.30, 0.02, 8.0, 2.0),
        DecisionSlot(now + timedelta(minutes=10), 0.12, 0.05, 0.0, 2.0),
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=45,
        ready_by=now + timedelta(minutes=15),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
    )

    assert schedule.reason == "least_cost_solar_aware_slots_before_ready_by"
    assert schedule.allocations[0].valid_at == now + timedelta(minutes=5)
    assert schedule.allocations[0].import_price == 0.30
    assert schedule.allocations[0].effective_price == 0.02
    assert schedule.allocations[0].solar_surplus_used_kw == 6
    assert schedule.allocations[0].grid_import_used_kw == 0


def test_allocate_charging_honors_carbon_priority_and_reports_emissions() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [
        DecisionSlot(now, 0.10, 0.05, 0.0, 2.0, carbon_intensity_g_per_kwh=900),
        DecisionSlot(
            now + timedelta(minutes=30),
            0.20,
            0.05,
            0.0,
            2.0,
            carbon_intensity_g_per_kwh=100,
        ),
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=45,
        ready_by=now + timedelta(hours=1),
        charge_rate_kw=5,
        soc_per_kwh=2,
        interval_minutes=30,
        carbon_weight=0.8,
    )

    assert schedule.allocations[0].valid_at == now + timedelta(minutes=30)
    assert schedule.allocations[0].carbon_intensity_g_per_kwh == 100
    assert schedule.allocations[0].estimated_carbon_g == 250


def test_solar_surplus_uses_conservative_forecast_bounds() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slot = DecisionSlot(
        now,
        0.20,
        0.05,
        8.0,
        2.0,
        pv_forecast_lower_kw=4.0,
        baseline_load_forecast_upper_kw=3.0,
    )

    assert _solar_surplus_kw(slot) == 1.0


def test_ev_solar_aware_cost_helpers_cover_fallbacks() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    zero_charge_slot = DecisionSlot(now, 0.25, 0.05, 6.0, 2.0)
    missing_import_slot = DecisionSlot(now, None, 0.05, 6.0, 2.0)
    missing_forecast_slot = DecisionSlot(now, 0.25, 0.05, None, 2.0)
    flexible_load_slot = DecisionSlot(now, 0.25, None, 8.0, 2.0, projected_hvac_load_kw=1.5)

    assert _charge_cost_components(zero_charge_slot, 0) == (None, 0.0, 0.0)
    assert _charge_cost_components(missing_import_slot, 6) == (None, 0.0, 6)
    assert _effective_charge_price(zero_charge_slot, 0) == 0.25
    assert _solar_surplus_kw(missing_forecast_slot) == 0.0
    assert _solar_surplus_kw(flexible_load_slot) == 4.5
    assert _charge_cost_components(flexible_load_slot, 6) == (0.0625, 4.5, 1.5)


def test_allocate_least_cost_charging_marks_infeasible() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [DecisionSlot(now, 0.10, 0.05, 0, 1)]
    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=70,
        ready_by=now + timedelta(minutes=5),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
    )
    assert schedule.infeasible is True
    assert schedule.scheduled_soc_percent == 45
    assert schedule.reason == "infeasible_before_ready_by"


def test_allocate_native_charging_honors_force_current_and_price_limit() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [
        DecisionSlot(now + timedelta(minutes=offset), price, 0.05, 0, 1)
        for offset, price in [(0, 0.8), (5, 0.1), (10, 0.2)]
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=50,
        ready_by=now + timedelta(minutes=15),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        force_current=True,
        max_import_price=0.15,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [now, now + timedelta(minutes=5)]
    assert schedule.infeasible is False
    assert _best_continuous_slots(
        [], required_slots=0, interval_minutes=5, force_current=False,
        charge_rate_kw=6, required_soc_percent=0, soc_per_slot=5, carbon_weight=0,
    ) == []


def test_forced_current_slot_bypasses_earliest_start_without_moving_future_window() -> None:
    now = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    earliest_start = now + timedelta(hours=1)
    slots = [
        DecisionSlot(now, -0.05, 0.05, 0, 1),
        DecisionSlot(earliest_start, 0.20, 0.05, 0, 1),
        DecisionSlot(earliest_start + timedelta(minutes=5), 0.10, 0.05, 0, 1),
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=55,
        ready_by=earliest_start + timedelta(minutes=10),
        earliest_start=earliest_start,
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        continuous=True,
        force_current=True,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [
        now,
        earliest_start,
        earliest_start + timedelta(minutes=5),
    ]
    assert schedule.infeasible is False


def test_current_slot_still_honors_earliest_start_without_override() -> None:
    now = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    earliest_start = now + timedelta(hours=1)
    slots = [
        DecisionSlot(now, -0.05, 0.05, 0, 1),
        DecisionSlot(earliest_start, 0.20, 0.05, 0, 1),
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=45,
        ready_by=earliest_start + timedelta(minutes=5),
        earliest_start=earliest_start,
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
    )

    assert [allocation.valid_at for allocation in schedule.allocations] == [earliest_start]


def test_continuous_charging_uses_only_contiguous_partial_windows() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [
        DecisionSlot(now + timedelta(minutes=offset), price, 0.05, 0, 1)
        for offset, price in [(0, 0.01), (10, 0.20), (15, 0.30)]
    ]

    schedule = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=55,
        ready_by=now + timedelta(minutes=20),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        continuous=True,
    )
    forced = allocate_least_cost_charging(
        slots,
        current_soc_percent=40,
        target_soc_percent=55,
        ready_by=now + timedelta(minutes=20),
        charge_rate_kw=6,
        soc_per_kwh=10,
        interval_minutes=5,
        continuous=True,
        force_current=True,
    )

    assert [item.valid_at for item in schedule.allocations] == [
        now + timedelta(minutes=10),
        now + timedelta(minutes=15),
    ]
    assert schedule.scheduled_soc_percent == 50
    assert schedule.infeasible is True
    assert [item.valid_at for item in forced.allocations] == [now]
    assert forced.scheduled_soc_percent == 45
    assert forced.infeasible is True


def test_ev_target_and_schedule_invalid_edge_cases() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    slots = [DecisionSlot(now, 0.10, 0.05, 0, 1)]
    assert (
        allocate_least_cost_charging(
            slots,
            current_soc_percent=80,
            target_soc_percent=80,
            ready_by=now + timedelta(hours=1),
            charge_rate_kw=6,
            soc_per_kwh=10,
            interval_minutes=5,
        ).reason
        == "already_at_target"
    )
    assert (
        allocate_least_cost_charging(
            slots,
            current_soc_percent=40,
            target_soc_percent=80,
            ready_by=now + timedelta(hours=1),
            charge_rate_kw=0,
            soc_per_kwh=10,
            interval_minutes=5,
        ).reason
        == "ev_charge_rate_invalid"
    )


def test_ev_low_level_parsers_cover_missing_and_unknown_values() -> None:
    class EmptyState:
        state = "maybe"

    class UpdatedOnly:
        state = "on"
        last_updated = datetime(2026, 6, 27, tzinfo=UTC)

    assert _float_or_none("67,5 %") == 67.5
    assert _float_or_none("bad") is None
    assert _state_timestamp(EmptyState()) is None
    assert _state_timestamp(UpdatedOnly()) == datetime(2026, 6, 27, tzinfo=UTC)


class RecorderState:
    """Minimal Recorder state."""

    def __init__(self, state: str, timestamp: datetime) -> None:
        self.state = state
        self.last_changed = timestamp
        self.last_updated = timestamp


def test_charge_calibration_learns_conservative_soc_per_kwh_from_completed_session() -> None:
    base = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)

    calibration = build_ev_charge_calibration(
        [RecorderState("charging", base), RecorderState("idle", base + timedelta(hours=1))],
        [RecorderState("50", base), RecorderState("64", base + timedelta(hours=1))],
        charge_rate_kw=7,
        trained_at=base + timedelta(hours=2),
        charging_entity_id="sensor.charger_status",
        soc_entity_id="sensor.vehicle_soc",
    )

    assert calibration["status"] == "ready"
    assert calibration["sample_count"] == 1
    assert calibration["raw_soc_per_kwh"] == 2.0
    assert calibration["soc_per_kwh"] == 1.8
    assert effective_ev_soc_per_kwh(
        calibration,
        2.0,
        charging_entity_id="sensor.charger_status",
        soc_entity_id="sensor.vehicle_soc",
        charge_rate_kw=7,
    ) == (1.8, "recorder_charging_history")
    assert effective_ev_soc_per_kwh(
        calibration,
        2.0,
        charging_entity_id="sensor.replacement_charger",
        soc_entity_id="sensor.vehicle_soc",
        charge_rate_kw=7,
    ) == (2.0, "configured_fallback")
    assert effective_ev_soc_per_kwh(
        calibration,
        2.0,
        charging_entity_id="sensor.charger_status",
        soc_entity_id="sensor.replacement_vehicle_soc",
        charge_rate_kw=7,
    ) == (2.0, "configured_fallback")
    assert effective_ev_soc_per_kwh(
        calibration,
        2.0,
        charging_entity_id="sensor.charger_status",
        soc_entity_id="sensor.vehicle_soc",
        charge_rate_kw=11,
    ) == (2.0, "configured_fallback")


def test_charge_calibration_rejects_short_or_stale_soc_sessions() -> None:
    base = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)

    calibration = build_ev_charge_calibration(
        [
            RecorderState("on", base),
            RecorderState("off", base + timedelta(minutes=20)),
            RecorderState("on", base + timedelta(hours=2)),
            RecorderState("off", base + timedelta(hours=3)),
        ],
        [RecorderState("50", base), RecorderState("60", base + timedelta(minutes=20))],
        charge_rate_kw=7,
        trained_at=base + timedelta(hours=4),
        charging_entity_id="sensor.charger_status",
        soc_entity_id="sensor.vehicle_soc",
    )

    assert calibration["status"] == "insufficient_history"
    assert calibration["sample_count"] == 0
    assert effective_ev_soc_per_kwh(
        calibration,
        2.2,
        charging_entity_id="sensor.charger_status",
        soc_entity_id="sensor.vehicle_soc",
        charge_rate_kw=7,
    ) == (2.2, "configured_fallback")


def test_charge_calibration_rejects_invalid_power_noise_and_implausible_gain() -> None:
    base = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)
    assert build_ev_charge_calibration(
        [],
        [],
        charge_rate_kw=0,
        trained_at=base,
        charging_entity_id="sensor.charger",
        soc_entity_id="sensor.soc",
    )["sample_count"] == 0

    calibration = build_ev_charge_calibration(
        [
            RecorderState("idle", base),
            RecorderState("charging", base + timedelta(hours=1)),
            RecorderState("idle", base + timedelta(hours=1)),
            RecorderState("charging", base + timedelta(hours=2)),
            RecorderState("idle", base + timedelta(hours=3)),
            RecorderState("charging", base + timedelta(hours=4)),
            RecorderState("idle", base + timedelta(hours=5)),
        ],
        [
            RecorderState("50", base + timedelta(hours=2)),
            RecorderState("51", base + timedelta(hours=3)),
            RecorderState("0", base + timedelta(hours=4)),
            RecorderState("80", base + timedelta(hours=5)),
        ],
        charge_rate_kw=7,
        trained_at=base + timedelta(hours=6),
        charging_entity_id="sensor.charger",
        soc_entity_id="sensor.soc",
    )

    assert calibration["status"] == "insufficient_history"
    assert calibration["sample_count"] == 0


def test_indexed_soc_lookup_preserves_duplicate_and_freshness_boundaries() -> None:
    from custom_components.ha_energy_planner.ev import _soc_at_or_before

    now = datetime(2026, 6, 27, tzinfo=UTC)
    points = [(now, 10.0), (now, 20.0), (now + timedelta(minutes=30), 30.0)]
    assert _soc_at_or_before(points, now) == 20.0
    assert _soc_at_or_before(points, now + timedelta(minutes=15)) == 20.0
    assert _soc_at_or_before(points, now + timedelta(minutes=15, seconds=1)) is None
    assert _soc_at_or_before(points, now - timedelta(seconds=1)) is None
    assert _soc_at_or_before([], now) is None
