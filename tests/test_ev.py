"""Tests for EV trip history and target calculations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.ev import (
    EVTripRecord,
    _best_continuous_slots,
    _charge_cost_components,
    _connected_bool,
    _effective_charge_price,
    _float_or_none,
    _solar_surplus_kw,
    _state_timestamp,
    allocate_least_cost_charging,
    calculate_ev_target,
    import_trip_history_from_state_sequences,
    summarize_stored_trip_history,
    summarize_trip_history,
    trip_records_from_store,
    update_trip_history_from_values,
)
from custom_components.ha_energy_planner.models import DecisionSlot


def test_ev_summary_uses_max_daily_consumption() -> None:
    base = datetime(2026, 6, 24, tzinfo=UTC)
    summary = summarize_trip_history(
        [
            EVTripRecord(base, base + timedelta(minutes=20), 80, 72),
            EVTripRecord(base + timedelta(hours=2), base + timedelta(hours=3), 72, 65),
            EVTripRecord(base + timedelta(days=1), base + timedelta(days=1, hours=1), 80, 70),
            EVTripRecord(base + timedelta(days=2), base + timedelta(days=2, hours=1), 90, 82),
        ]
    )
    assert summary.history_sufficient is True
    assert summary.max_daily_soc_percent == 15
    assert summary.average_daily_soc_percent == 11


def test_ev_target_clamps_and_marks_infeasible() -> None:
    summary = summarize_trip_history([], minimum_history_days=3)
    target = calculate_ev_target(
        current_soc_percent=40,
        summary=summary,
        ev_min_soc_percent=30,
        ev_max_soc_percent=90,
        fallback_target_soc_percent=85,
        available_charge_hours=2,
        charge_rate_percent_per_hour=10,
    )
    assert target.target_soc_percent == 60
    assert target.required_charge_percent == 20
    assert target.infeasible is True
    assert target.reason == "infeasible_before_ready_by"


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
    assert _best_continuous_slots([], [], required_slots=0, interval_minutes=5, force_current=False) == []


def test_update_trip_history_records_completed_disconnected_trip() -> None:
    started = datetime(2026, 6, 26, 22, 0, tzinfo=UTC)
    ended = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    history, changed = update_trip_history_from_values(
        {},
        connected=False,
        soc_percent=82,
        now=started,
    )
    assert changed is True
    assert history["active_trip"]["start_soc_percent"] == 82

    history, changed = update_trip_history_from_values(
        history,
        connected=True,
        soc_percent=74,
        now=ended,
    )

    assert changed is True
    assert history["active_trip"] == {}
    assert history["records"] == [
        {
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "start_soc_percent": 82.0,
            "end_soc_percent": 74.0,
        }
    ]
    summary = summarize_stored_trip_history(history, minimum_history_days=1)
    assert summary.history_sufficient is True
    assert summary.max_daily_soc_percent == 8


def test_trip_history_helpers_ignore_invalid_records_and_noop_updates() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert trip_records_from_store({"records": ["bad", {"started_at": "bad"}]}) == []

    history, changed = update_trip_history_from_values({}, connected=None, soc_percent=80, now=now)
    assert changed is False
    assert history == {"active_trip": {}, "records": []}

    active = {"active_trip": {"started_at": now.isoformat(), "start_soc_percent": 80}, "records": []}
    assert update_trip_history_from_values(active, connected=False, soc_percent=79, now=now)[1] is False
    updated, changed = update_trip_history_from_values(active, connected=True, soc_percent=80, now=now)
    assert changed is True
    assert updated["records"] == []


def test_ev_target_and_schedule_invalid_edge_cases() -> None:
    summary = summarize_trip_history([])
    try:
        calculate_ev_target(
            current_soc_percent=40,
            summary=summary,
            ev_min_soc_percent=90,
            ev_max_soc_percent=80,
            fallback_target_soc_percent=85,
            available_charge_hours=1,
            charge_rate_percent_per_hour=10,
        )
    except ValueError as err:
        assert "ev_min_soc_percent" in str(err)
    else:
        raise AssertionError("Expected invalid EV SOC bounds to fail")

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
    assert _connected_bool("maybe") is None


class RecorderState:
    """Minimal Recorder state."""

    def __init__(self, state: str, timestamp: datetime) -> None:
        self.state = state
        self.last_changed = timestamp
        self.last_updated = timestamp


def test_import_trip_history_from_state_sequences_records_and_dedupes_trips() -> None:
    base = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)
    connected_states = [
        RecorderState("on", base),
        RecorderState("off", base + timedelta(hours=1)),
        RecorderState("on", base + timedelta(hours=3)),
    ]
    soc_states = [
        RecorderState("80", base),
        RecorderState("78", base + timedelta(hours=1)),
        RecorderState("70", base + timedelta(hours=3)),
    ]

    history, changed = import_trip_history_from_state_sequences(
        {},
        connected_states=connected_states,
        soc_states=soc_states,
        imported_at=base + timedelta(hours=4),
    )
    history, changed_again = import_trip_history_from_state_sequences(
        history,
        connected_states=connected_states,
        soc_states=soc_states,
        imported_at=base + timedelta(hours=5),
    )

    assert changed is True
    assert changed_again is True
    assert len(history["records"]) == 1
    assert history["records"][0]["source"] == "recorder"
    assert history["records"][0]["start_soc_percent"] == 78.0
    assert history["records"][0]["end_soc_percent"] == 70.0


def test_import_trip_history_accepts_mini_like_states_and_percent_soc_strings() -> None:
    base = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)
    connected_states = [
        RecorderState("plugged_in", base),
        RecorderState("unplugged", base + timedelta(hours=1)),
        RecorderState("plugged_in", base + timedelta(hours=3)),
        RecorderState("vehicle_disconnected", base + timedelta(days=1, hours=1)),
        RecorderState("vehicle_connected", base + timedelta(days=1, hours=2)),
        RecorderState("away", base + timedelta(days=2, hours=1)),
        RecorderState("home", base + timedelta(days=2, hours=2)),
        RecorderState("not_plugged_in", base + timedelta(days=3, hours=1)),
        RecorderState("connected_not_charging", base + timedelta(days=3, hours=2)),
    ]
    soc_states = [
        RecorderState("90 %", base),
        RecorderState("88%", base + timedelta(hours=1)),
        RecorderState("80 %", base + timedelta(hours=3)),
        RecorderState("79%", base + timedelta(days=1, hours=1)),
        RecorderState("74 %", base + timedelta(days=1, hours=2)),
        RecorderState("74%", base + timedelta(days=2, hours=1)),
        RecorderState("68 %", base + timedelta(days=2, hours=2)),
        RecorderState("67,5 %", base + timedelta(days=3, hours=1)),
        RecorderState("61,25 %", base + timedelta(days=3, hours=2)),
    ]

    history, changed = import_trip_history_from_state_sequences(
        {},
        connected_states=connected_states,
        soc_states=soc_states,
        imported_at=base + timedelta(days=3),
    )

    assert changed is True
    assert [record["start_soc_percent"] for record in history["records"]] == [88.0, 79.0, 74.0, 67.5]
    assert [record["end_soc_percent"] for record in history["records"]] == [80.0, 74.0, 68.0, 61.25]
    summary = summarize_stored_trip_history(history)
    assert summary.history_sufficient is True
    assert summary.observed_days == 4
    assert summary.max_daily_soc_percent == 8
    assert summary.average_daily_soc_percent == 6.312
