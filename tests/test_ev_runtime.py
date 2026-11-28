"""EV power command ordering, leases and durable spending recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.ev_adapter import EVChargerAdapter
from custom_components.ha_energy_planner.ev_runtime import (
    EV_CAP_FEEDBACK_CLOCK_INVALID,
    EV_RETIRED_CAP_KW,
    MAX_NATIVE_STOP_SAMPLES,
    EVImplicitSignalClock,
    NativeEVStopObservations,
    allocation_deadline,
    audit_evidence,
    check_ev_cap_feedback_clock,
    settle_spending,
    timestamp,
)
from custom_components.ha_energy_planner.ev_telemetry import (
    stop_signal_reported_at,
    unverified_positive_power,
    updated_ev_signal_clock,
)
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def test_cap_clock_can_retire_a_future_command_without_a_known_setting():
    pending = {"at": (NOW + timedelta(seconds=1)).isoformat(), "physical_power_kw": "unknown"}
    reservation = {"load_kw": 8, "pending_power_limit": pending}
    assert check_ev_cap_feedback_clock(reservation, NOW) == (None, True)
    assert reservation == {"load_kw": 8} and EV_RETIRED_CAP_KW not in reservation
    assert pending[EV_CAP_FEEDBACK_CLOCK_INVALID] is True


@pytest.mark.parametrize(("value", "unit", "sampled_at", "receipt", "expected"), [
    (8000, "W", "malformed", NOW, (8, NOW)),
    (8, "kW", (NOW + timedelta(seconds=1)).isoformat(), NOW, (8, NOW)),
    (8, "kW", (NOW - timedelta(seconds=601)).isoformat(), NOW, None),
    (8, "kW", "malformed", NOW - timedelta(seconds=601), None),
    (8, "kW", "malformed", NOW + timedelta(seconds=1), None),
    (8, "kW", "malformed", None, None),
    (0, "kW", "malformed", NOW, None),
    (8, "A", "malformed", NOW, None),
])
def test_unverified_ev_meter_uses_recent_positive_power_only(value, unit, sampled_at, receipt, expected):
    state = SimpleNamespace(
        state=value, last_updated=receipt, last_reported=receipt,
        attributes={"unit_of_measurement": unit, "sampled_at_utc": sampled_at},
    )
    assert unverified_positive_power(state, NOW) == expected


def test_native_stop_signal_requires_an_observation_timestamp():
    unstamped = SimpleNamespace(attributes={}, last_updated=None)
    assert stop_signal_reported_at(unstamped, NOW) is None


def test_native_stop_signal_ignores_a_clock_marker_from_an_earlier_raw_state():
    state = SimpleNamespace(last_changed=NOW, last_updated=NOW, attributes={})
    invalid = EVImplicitSignalClock(NOW - timedelta(seconds=1), None)
    assert stop_signal_reported_at(state, NOW, implicit_clock=invalid) == NOW


def test_native_stop_signal_keeps_rejections_bounded_under_a_burst_of_invalid_samples():
    def report(sample, receipt):
        return SimpleNamespace(last_changed=NOW, last_updated=receipt, last_reported=receipt,
                               attributes={"sampled_at_utc": sample.isoformat()})

    clock = None
    for offset in range(1, MAX_NATIVE_STOP_SAMPLES + 3):
        _, clock = updated_ev_signal_clock(report(NOW + timedelta(seconds=offset), NOW), None, clock, None)
    assert len(clock.invalid_explicit_samples) == MAX_NATIVE_STOP_SAMPLES
    assert clock.omitted_explicit_samples == (NOW + timedelta(seconds=1), NOW + timedelta(seconds=2))
    receipt = NOW + timedelta(seconds=300)
    replay = report(NOW + timedelta(seconds=1), receipt)
    assert stop_signal_reported_at(replay, receipt, implicit_clock=clock) is None
    fresh = report(receipt, receipt)
    assert stop_signal_reported_at(fresh, receipt, implicit_clock=clock) == receipt
    corrected = report(NOW, receipt)
    assert stop_signal_reported_at(corrected, receipt, implicit_clock=clock) == NOW


def test_native_stop_observation_history_deduplicates_and_bounds_samples():
    observations = NativeEVStopObservations()
    for offset in reversed(range(MAX_NATIVE_STOP_SAMPLES + 1)):
        sample = NOW + timedelta(seconds=offset)
        observations.note_draw(sample, 0)
        observations.note_draw(sample, 0)
    assert len(observations.stop_samples) == MAX_NATIVE_STOP_SAMPLES
    assert observations.first_stop_after(NOW) == NOW + timedelta(seconds=1)
    observed_late = NOW + timedelta(seconds=2, milliseconds=500)
    observations.note_signal(observed_late, active=True)
    observations.note_draw(observed_late, 0)
    assert observations.activity_at == observed_late
    assert observations.first_stop_after(observed_late) == NOW + timedelta(seconds=3)


def test_native_stop_history_uses_later_proof_when_activity_falls_inside_an_evicted_interval():
    observations = NativeEVStopObservations()
    for offset in range(400):
        observations.note_draw(NOW + timedelta(seconds=offset), 0)
    activity_at = NOW + timedelta(seconds=127, milliseconds=500)
    observations.note_signal(activity_at, active=True)
    assert len(observations.stop_samples) == MAX_NATIVE_STOP_SAMPLES
    assert observations.first_stop_after(activity_at) == NOW + timedelta(seconds=272)


def test_native_stop_receipts_keep_initial_and_later_proof_in_a_bounded_history():
    observations = NativeEVStopObservations()
    entity_id = "sensor.power"
    for offset in range(400):
        receipt = NOW + timedelta(seconds=offset)
        observations.note_stop_receipt(entity_id, receipt, receipt)
        observations.note_stop_receipt(entity_id, receipt, receipt + timedelta(milliseconds=500))
    assert len(observations.stop_receipts[entity_id]) == MAX_NATIVE_STOP_SAMPLES
    assert observations.stop_received_by(entity_id, NOW) == NOW
    assert observations.stop_received_by(entity_id, NOW + timedelta(seconds=397)) == NOW + timedelta(seconds=397)


def test_native_stop_receipt_cutoff_does_not_cross_an_evicted_contradiction():
    observations = NativeEVStopObservations()
    entity_id = "switch.charger"
    for offset in range(1, 129):
        receipt = NOW + timedelta(milliseconds=offset)
        observations.note_stop_receipt(entity_id, receipt, receipt)
    contradiction = NOW + timedelta(milliseconds=490)
    observations.note_stop_receipt(entity_id, None, contradiction)
    cutoff = NOW + timedelta(milliseconds=500)
    for offset in range(1, 129):
        receipt = cutoff + timedelta(milliseconds=offset)
        observations.note_stop_receipt(entity_id, receipt, receipt)
    earlier = NOW + timedelta(milliseconds=128)
    latest = cutoff + timedelta(milliseconds=128)
    assert len(observations.stop_receipts[entity_id]) == MAX_NATIVE_STOP_SAMPLES
    assert observations.stop_received_by(entity_id, earlier) == earlier
    assert observations.stop_received_by(entity_id, cutoff) is None
    assert observations.stop_received_by(entity_id, latest) == latest


def action(**desired):
    return PlanAction(
        "ev",
        "plan",
        NOW,
        NOW + timedelta(minutes=5),
        ActionAsset.EV,
        ActionKind.EV_SCHEDULE,
        {"charging_required_now": True, "projected_load_kw_now": 6, **desired},
        [],
        [],
        0,
        1,
    )


def test_timestamp_and_audit_remaining_actions():
    assert timestamp("invalid") is None
    assert timestamp(datetime(2026, 1, 1)) is None
    assert timestamp(NOW.isoformat()) == NOW
    rows = [
        {"asset": "ev", "result": "applied", "attempted_at": NOW.isoformat()},
        None,
        {"asset": "ev", "result": "failed", "attempted_at": (NOW - timedelta(days=2)).isoformat()},
    ]
    evidence = audit_evidence(rows, {"max_daily_ev_actions": 4}, NOW)
    assert evidence["remaining_actions"] == 3
    assert evidence["last_transition_at"] == NOW.isoformat()
    assert audit_evidence([], {"max_daily_ev_actions": 0}, NOW)["remaining_actions"] == 10000


def test_spending_is_settled_idempotently_and_retained_after_expiry():
    record = {"emergency_spend": 0.1, "command_exposure": {"at": NOW.isoformat(), "cost_per_hour": 3}}
    settled = settle_spending(record, NOW + timedelta(minutes=5))
    assert settled["emergency_spend"] == pytest.approx(0.35)
    assert settle_spending(settled, NOW + timedelta(minutes=5)) == settled
    assert record["emergency_spend"] == 0.1
    assert settle_spending({"command_exposure": "bad"}, NOW) == {"command_exposure": "bad"}
    assert settle_spending({"command_exposure": {"at": "bad"}}, NOW) == {"command_exposure": {"at": "bad"}}


@pytest.mark.parametrize(
    "changes,price,reason",
    [
        ({}, None, "ev_execution_price_unavailable"),
        ({}, 0.4, "ev_execution_price_ceiling"),
        (
            {"ev_price_policy": "departure_priority", "ev_emergency_price": 0.5, "ev_emergency_budget": 0},
            0.4,
            "ev_emergency_budget_or_ceiling",
        ),
        (
            {"ev_price_policy": "departure_priority", "ev_emergency_price": 0.5, "ev_emergency_budget": 0.01},
            0.4,
            "ev_emergency_budget_insufficient_for_confirmation",
        ),
        (
            {"ev_price_policy": "departure_priority", "ev_emergency_price": 0.5, "ev_emergency_budget": 1},
            0.6,
            "ev_emergency_budget_or_ceiling",
        ),
    ],
)
def test_price_and_budget_rejections(changes, price, reason):
    options = {**DEFAULT_OPTIONS, "ev_price_limit_enabled": True, "ev_max_import_price": 0.2, **changes}
    assert allocation_deadline(action(), options, {}, NOW, price)[2] == reason


def test_premium_lease_reserves_stop_latency_and_partial_slot():
    options = {
        **DEFAULT_OPTIONS,
        "ev_charge_rate_kw": 6,
        "ev_price_limit_enabled": True,
        "ev_max_import_price": 0.2,
        "ev_price_policy": "departure_priority",
        "ev_emergency_price": 0.5,
        "ev_emergency_budget": 0.15,
    }
    deadline, rate, reason = allocation_deadline(action(), options, {}, NOW, 0.4)
    assert reason is None
    assert rate == pytest.approx(1.8)
    assert deadline == NOW + timedelta(seconds=210)
    partial = action(optimization={"conservative_completion": (NOW + timedelta(minutes=1)).isoformat()})
    assert allocation_deadline(partial, DEFAULT_OPTIONS, {}, NOW, 0.1)[0] == NOW + timedelta(minutes=1)
    assert allocation_deadline(action(), options, {}, NOW, 0.1) == (None, 0, None)
    options["ev_charge_rate_kw"] = 0
    assert (
        allocation_deadline(action(projected_load_kw_now=0), options, {}, NOW, 0.4)[2]
        == "ev_emergency_budget_or_ceiling"
    )


class Charger:
    def __init__(self, *, fail=False, confirm=True):
        self.fail, self.confirm = fail, confirm
        self.calls = []
        self.values = {}
        self.put("number.limit", "6", {"unit_of_measurement": "kW", "min": 1, "max": 6, "step": 1})
        self.put("switch.charger", "off")
        self.put("sensor.power", "0", {"unit_of_measurement": "kW"})
        self.states = SimpleNamespace(get=self.values.get)
        self.services = SimpleNamespace(async_call=self.call, has_service=lambda *_: True)

    def put(self, entity_id, state, attrs=None):
        from homeassistant.util import dt as dt_util

        self.values[entity_id] = SimpleNamespace(
            entity_id=entity_id, state=str(state), attributes=attrs or {}, last_updated=dt_util.utcnow()
        )

    async def call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data))
        if domain == "number":
            if self.fail:
                raise RuntimeError("charger rejected limit")
            if self.confirm:
                self.put(data["entity_id"], data["value"], self.values[data["entity_id"]].attributes)
        else:
            self.put("switch.charger", "on" if service == "turn_on" else "off")
            self.put(
                "sensor.power",
                self.values["number.limit"].state if service == "turn_on" else 0,
                {"unit_of_measurement": "kW"},
            )


def adapter(charger):
    return EVChargerAdapter(
        charger,
        {
            "ev_charger_entity": "switch.charger",
            "ev_charging_entity": "switch.charger",
            "ev_power_limit_entity": "number.limit",
            "ev_power_entity": "sensor.power",
        },
        power_options={"ev_limit_min": 1, "ev_limit_max": 6},
        confirmation_timeout_seconds=0,
        confirmation_retries=0,
    )


def limited(**changes):
    return action(
        power_limit={"entity_id": "number.limit", "unit": "kW", "value": 3, "physical_power_kw": 3, **changes}
    )


def test_limit_is_confirmed_before_start_and_restore_stops_first():
    async def run():
        charger = Charger()
        device = adapter(charger)
        result = await device.async_execute(limited())
        assert result.applied
        assert [call[1] for call in charger.calls] == ["set_value", "turn_on"]
        assert result.pre_state["ev_power_limit_entity"] == "6"
        assert result.post_state["confirmed_power_kw"] == 3
        charger.calls.clear()
        restored = await device.async_restore({"ev_power_limit_entity": "6"})
        assert restored.applied
        assert [call[1] for call in charger.calls] == ["turn_off", "set_value"]
        assert charger.values["switch.charger"].state == "off"

    asyncio.run(run())


@pytest.mark.parametrize("fail,confirm", [(True, True), (False, False)])
def test_unconfirmed_limit_never_starts_charger(fail, confirm):
    async def run():
        charger = Charger(fail=fail, confirm=confirm)
        result = await adapter(charger).async_execute(limited())
        assert not result.applied
        assert result.reason == "ev_power_limit_unconfirmed"
        assert not any(call[1] == "turn_on" for call in charger.calls)

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_id": "number.other"},
        {"unit": "A"},
        {"value": 3.1},
        {"value": None},
        {"physical_power_kw": 0},
        {"physical_power_kw": 3.1},
    ],
)
def test_invalid_number_requests_fail_closed(changes):
    async def run():
        charger = Charger()
        result = await adapter(charger).async_execute(limited(**changes))
        assert not result.applied
        assert not any(call[1] in {"set_value", "turn_on"} for call in charger.calls)

    asyncio.run(run())


def test_noop_limit_and_expired_lease():
    async def run():
        charger = Charger()
        device = adapter(charger)
        assert await device._async_set_power_limit(limited(value=6, physical_power_kw=6).desired_state["power_limit"])
        assert charger.calls == []
        expired = action(charge_lease_until=(NOW - timedelta(days=365)).isoformat())
        result = await device.async_execute(expired)
        assert not result.applied and result.reason == "ev_allocation_expired"
        assert not await device._async_write_number("number.missing", 3)
        assert not await device._async_write_number("number.limit", 7)
        charger.values["number.limit"].attributes.pop("min")
        assert not await device._async_write_number("number.limit", 3)

    asyncio.run(run())


def test_planned_stop_restores_original_limit_and_retains_failure():
    async def run():
        charger = Charger()
        device = adapter(charger)
        await device.async_execute(limited())
        result = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        assert result.applied
        await device.async_execute(limited())
        charger.fail = True
        result = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        assert not result.applied and result.safe_state_confirmed is True
        assert charger.values["switch.charger"].state == "off"
        result = await device.async_restore({"ev_power_limit_entity": "6"})
        assert not result.applied

    asyncio.run(run())


@pytest.mark.parametrize("source", ["switch", "meter"])
def test_planned_stop_retains_reservation_when_charging_resumes_during_limit_write(source):
    async def run():
        charger = Charger()
        device = adapter(charger)
        await device.async_execute(limited())

        async def resume_during_write(domain, service, data, **kwargs):
            await charger.call(domain, service, data, **kwargs)
            if domain == "number":
                if source == "switch":
                    charger.put("switch.charger", "on")
                charger.put("sensor.power", 6, {"unit_of_measurement": "kW"})

        charger.services.async_call = resume_during_write
        result = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        assert not result.applied and result.safe_state_confirmed is False
        assert result.command_sent and result.reason == "ev_stop_not_confirmed"

    asyncio.run(run())


def test_planned_native_restore_requires_stop_evidence_newer_than_draw():
    from homeassistant.util import dt as dt_util

    async def run():
        charger = Charger()
        device = adapter(charger)
        device.entry_data.update(ev_charging_entity="sensor.charging", ev_connected_entity="binary_sensor.connected")
        charger.put("binary_sensor.connected", "on")
        charger.put("number.limit", 3, charger.values["number.limit"].attributes)
        charger.put("sensor.power", 5, {"unit_of_measurement": "kW"})
        stale_source_time = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
        charger.put("sensor.charging", "idle", {"sampled_at_utc": stale_source_time})

        async def acknowledge_without_stopping(domain, service, data, **kwargs):
            if domain == "switch":
                charger.calls.append((domain, service, data))
            else:
                await charger.call(domain, service, data, **kwargs)

        charger.services.async_call = acknowledge_without_stopping
        stop = action(charging_required_now=False, restore_power_limit="6")
        result = await device.async_execute(stop)
        assert result.reason == "ev_stop_not_confirmed"
        assert charger.calls == [("switch", "turn_off", {"entity_id": "switch.charger"})]

        charger.put("binary_sensor.connected", "off", {"sampled_at_utc": dt_util.utcnow().isoformat()})
        result = await device.async_execute(stop)
        assert result.applied and result.safe_state_confirmed
        assert charger.calls[-1] == ("number", "set_value", {"entity_id": "number.limit", "value": 6})

    asyncio.run(run())


@pytest.mark.parametrize("future_stop_clock", [False, True])
def test_native_restore_forces_stop_when_meter_contradicts_switch(future_stop_clock):
    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    async def run():
        charger = Charger()
        device = adapter(charger)
        if future_stop_clock:
            future = dt_util.utcnow() + timedelta(minutes=1)
            charger.values["switch.charger"] = State("switch.charger", "off", last_updated=future)
        charger.put("number.limit", 3, charger.values["number.limit"].attributes)
        charger.put("sensor.power", 5, {"unit_of_measurement": "kW"})
        assert not device.native_restore_stop_is_current()
        result = await device.async_restore({"ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"})
        assert result.applied and result.safe_state_confirmed
        assert charger.calls == [
            ("switch", "turn_off", {"entity_id": "switch.charger"}),
            ("number", "set_value", {"entity_id": "number.limit", "value": 6}),
        ]

    asyncio.run(run())


@pytest.mark.parametrize(("entrypoint", "mapped_charging", "service_error", "zero_reported"), [
    ("recovery", True, False, True), ("scheduled", True, False, True), ("scheduled", False, False, True),
    ("recovery", False, True, True), ("recovery", True, True, False),
])
def test_native_stop_waits_for_delayed_zero_before_raising_cap(
    entrypoint, mapped_charging, service_error, zero_reported
):
    async def run():
        charger = Charger()
        device = adapter(charger)
        device.confirmation_timeout_seconds = 0.5 if zero_reported else 0
        device.confirmation_retries = 1
        device.confirmation_poll_seconds = 0.01
        if mapped_charging:
            device.entry_data["ev_charging_entity"] = "sensor.charging"
            charger.put("sensor.charging", "idle")
        else:
            device.entry_data.pop("ev_charging_entity")
        power_attributes = {"unit_of_measurement": "kW"}
        charger.put("number.limit", 3, charger.values["number.limit"].attributes)
        charger.put("sensor.power", 5, power_attributes)

        async def acknowledge_before_power_updates(domain, service, data, **kwargs):
            if domain == "switch":
                charger.calls.append((domain, service, data))
                if zero_reported:
                    asyncio.get_running_loop().call_soon(charger.put, "sensor.power", 0, power_attributes)
                if service_error:
                    raise RuntimeError("Stop service reply lost")
            else:
                assert charger.values["sensor.power"].state == "0"
                await charger.call(domain, service, data, **kwargs)

        charger.services.async_call = acknowledge_before_power_updates
        if entrypoint == "recovery":
            result = await device.async_restore({"ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"})
        else:
            result = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        stop = ("switch", "turn_off", {"entity_id": "switch.charger"})
        if zero_reported:
            assert result.applied and result.safe_state_confirmed
            assert charger.calls == [stop, ("number", "set_value", {"entity_id": "number.limit", "value": 6})]
        else:
            assert not result.applied and result.safe_state_confirmed is False
            assert result.reason == "ev_stop_not_confirmed" and charger.calls == [stop, stop]

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["recovery", "scheduled"])
@pytest.mark.parametrize("lost_service_reply", [False, True])
def test_native_stop_awaits_ordered_zero_while_meter_is_unavailable(entrypoint, lost_service_reply):
    async def run():
        charger = Charger()
        device = adapter(charger)
        device.confirmation_timeout_seconds = 0 if lost_service_reply else 0.5
        device.confirmation_retries = int(lost_service_reply)
        device.confirmation_poll_seconds = 0.01
        power_attributes = {"unit_of_measurement": "kW"}
        charger.put("sensor.charging", "idle")
        charger.put("number.limit", 3, charger.values["number.limit"].attributes)
        charger.put("sensor.power", 5, power_attributes)
        draw_at = charger.values["sensor.power"].last_updated
        device.retained_draw_at = lambda: draw_at
        if not lost_service_reply:
            charger.put("sensor.power", "unavailable", power_attributes)

        async def lose_stop_reply(domain, service, data, **kwargs):
            if domain == "switch":
                charger.calls.append((domain, service, data))
                charger.put("sensor.power", "unavailable", power_attributes)
                raise RuntimeError("Stop service reply lost")
            assert charger.values["sensor.power"].state == "0"
            await charger.call(domain, service, data, **kwargs)

        charger.services.async_call = lose_stop_reply
        call_control = device._async_call_control

        async def deliver_readback_after_cached_off(*args, **kwargs):
            result = await call_control(*args, **kwargs)
            if result.reason == "already_in_desired_state":
                device.confirmation_timeout_seconds = 0.5
                asyncio.get_running_loop().call_soon(charger.put, "sensor.power", 0, power_attributes)
            return result

        device._async_call_control = deliver_readback_after_cached_off
        if entrypoint == "recovery":
            result = await device.async_restore({"ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"})
        else:
            result = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        assert result.applied and result.safe_state_confirmed
        stop_calls = [("switch", "turn_off", {"entity_id": "switch.charger"})] if lost_service_reply else []
        assert charger.calls == [*stop_calls, ("number", "set_value", {"entity_id": "number.limit", "value": 6})]

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["recovery", "scheduled"])
@pytest.mark.parametrize("feedback", [
    "fresh", "missing", "last_poll", "late", "late_explicit", "last_poll_control", "late_control",
    "last_poll_repeat", "last_poll_repeat_control",
])
def test_native_stop_waits_when_cached_zero_predates_draw(monkeypatch, entrypoint, feedback):
    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    from custom_components.ha_energy_planner import ev_adapter as ev_adapter_module

    async def run():
        charger = Charger()
        device = adapter(charger)
        device.confirmation_timeout_seconds = 0 if feedback == "missing" else 0.5
        device.confirmation_poll_seconds = 0.01
        now = dt_util.utcnow()
        cached_at = now - timedelta(seconds=10)
        draw_at = now - timedelta(seconds=5)
        device.retained_draw_at = lambda: draw_at
        power_attributes = {"unit_of_measurement": "kW"}
        charger.put("number.limit", 3, charger.values["number.limit"].attributes)
        charger.values["switch.charger"] = State(
            "switch.charger", "off", last_changed=cached_at, last_updated=cached_at,
        )
        charger.values["sensor.power"] = State(
            "sensor.power", "0", {**power_attributes, "sampled_at_utc": cached_at.isoformat()}, last_updated=cached_at,
        )
        if feedback == "fresh":
            asyncio.get_running_loop().call_soon(charger.put, "sensor.power", 0, power_attributes)
        elif feedback != "missing":
            clock = 0.0

            async def wake_after_deadline(_delay):
                nonlocal clock
                clock = 0.75
                receipt = now + timedelta(seconds=0.45 if feedback.startswith("last_poll") else 0.6)
                if "repeat" in feedback:
                    entity_id = "switch.charger" if feedback.endswith("_control") else "sensor.power"
                    sample_at = now + timedelta(seconds=0.4) if entity_id == "switch.charger" else receipt
                    device.stop_observations.note_stop_receipt(entity_id, sample_at, receipt)
                    assert device.stop_observations.stop_received_by(entity_id, now) is None
                    receipt = now + timedelta(seconds=0.6)
                    late_sample = sample_at if entity_id == "switch.charger" else receipt
                    device.stop_observations.note_stop_receipt(entity_id, late_sample, receipt)
                if feedback.endswith("_control"):
                    source = now + timedelta(seconds=0.4)
                    charger.values["switch.charger"] = State(
                        "switch.charger", "off", {"sampled_at_utc": source.isoformat()}, last_updated=receipt,
                        last_reported=receipt,
                    )
                    return
                attributes = power_attributes
                if feedback == "late_explicit":
                    attributes = {**attributes, "sampled_at_utc": (now + timedelta(seconds=0.4)).isoformat()}
                charger.values["sensor.power"] = State(
                    "sensor.power", "0", attributes, last_updated=receipt, last_reported=receipt,
                )

            fake_loop = SimpleNamespace(time=lambda: clock)
            monkeypatch.setattr(ev_adapter_module, "dt_util", SimpleNamespace(
                utcnow=lambda: now + timedelta(seconds=clock),
            ))
            monkeypatch.setattr(ev_adapter_module, "asyncio", SimpleNamespace(
                get_running_loop=lambda: fake_loop, sleep=wake_after_deadline,
            ))
        if entrypoint == "recovery":
            result = await device.async_restore({"ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"})
        else:
            result = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        accepted = feedback == "fresh" or feedback.startswith("last_poll")
        assert result.applied is accepted
        assert result.safe_state_confirmed is accepted
        assert [call[1] for call in charger.calls] == (["set_value"] if accepted else [])

    asyncio.run(run())


@pytest.mark.parametrize("stopped_by", ["charging", "connection"])
@pytest.mark.parametrize("in_window", [True, False])
@pytest.mark.parametrize("wake", ["split", "both", "overrun_timely", "overrun_late"])
def test_native_stop_readback_extends_only_when_charging_stop_was_timely(monkeypatch, stopped_by, in_window, wake):
    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    from custom_components.ha_energy_planner import ev_adapter as ev_adapter_module

    async def run():
        charger = Charger()
        device = adapter(charger)
        device.entry_data.update(ev_charging_entity="binary_sensor.charging", ev_connected_entity="binary_sensor.plug")
        device.confirmation_timeout_seconds = 0.5
        device.confirmation_poll_seconds = 0.5
        start = dt_util.utcnow()
        clock = 0.0
        ids = {"charging": "binary_sensor.charging", "connection": "binary_sensor.plug"}
        for entity_id in (*ids.values(), "switch.charger"):
            charger.values[entity_id] = State(entity_id, "on", last_changed=start, last_updated=start)

        def stopped(entity_id, delay):
            received = start + timedelta(seconds=delay)
            charger.values[entity_id] = State(
                entity_id, "off", last_changed=received, last_updated=received, last_reported=received,
            )

        async def report(_delay):
            nonlocal clock
            if clock:
                clock = 0.76
                stopped("switch.charger", 0.7)
            else:
                stopped(ids[stopped_by], 0.49 if in_window else 0.51)
                if wake != "split":
                    stopped("switch.charger", 1.05 if wake == "overrun_late" else 0.51)
                clock = 1.23 if wake.startswith("overrun") else 0.52

        monkeypatch.setattr(ev_adapter_module, "dt_util", SimpleNamespace(
            utcnow=lambda: start + timedelta(seconds=clock),
        ))
        monkeypatch.setattr(ev_adapter_module, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock), sleep=report,
        ))
        outcome = await device._async_confirm_stop("switch.charger", ids["charging"], extend_for_control=True,
                                                  ordered_stop=True)
        assert outcome == ("confirmed" if in_window and wake != "overrun_late" else "timeout")
        assert clock == (0.76 if wake == "split" and in_window else 1.23 if wake.startswith("overrun") else 0.52)

    asyncio.run(run())


def test_native_stop_confirmation_accepts_disconnected_vehicle_override():
    async def run():
        charger = Charger()
        charger.put("binary_sensor.charging", "on")
        device = adapter(charger)
        device.entry_data["ev_charging_entity"] = "binary_sensor.charging"
        device.connected_override = False
        result = await device._async_confirm_stop("switch.charger", "binary_sensor.charging",
                                                 extend_for_control=True, ordered_stop=True)
        assert result == "confirmed" and not charger.calls

    asyncio.run(run())


@pytest.mark.parametrize("bad_source", ["stale", "future", "malformed"])
def test_native_stop_readback_does_not_extend_for_invalid_charging_timestamp(monkeypatch, bad_source):
    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    from custom_components.ha_energy_planner import ev_adapter as ev_adapter_module

    async def run():
        charger = Charger()
        device = adapter(charger)
        device.entry_data["ev_charging_entity"] = "binary_sensor.charging"
        device.confirmation_timeout_seconds = 0.5
        device.confirmation_poll_seconds = 0.25
        charging_id = "binary_sensor.charging"
        control_id = "switch.charger"
        start = dt_util.utcnow()
        elapsed = 0.0
        clock = start
        for entity_id in (control_id, charging_id):
            charger.values[entity_id] = State(entity_id, "on", last_changed=start, last_updated=start)

        async def publish_report(delay):
            nonlocal clock, elapsed
            elapsed += delay
            clock = start + timedelta(seconds=elapsed)
            if elapsed == 0.25:
                stamps = {"stale": (start - timedelta(seconds=601)).isoformat(),
                          "future": (start + timedelta(seconds=5)).isoformat(), "malformed": "unknown"}
                charger.values[charging_id] = State(
                    charging_id, "off", {"sampled_at_utc": stamps[bad_source]}, last_changed=clock, last_updated=clock,
                )
            if elapsed > 0.5:
                for entity_id in (control_id, charging_id):
                    charger.values[entity_id] = State(entity_id, "off", last_changed=clock, last_updated=clock)

        monkeypatch.setattr(ev_adapter_module, "dt_util", SimpleNamespace(utcnow=lambda: clock))
        monkeypatch.setattr(ev_adapter_module, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: elapsed), sleep=publish_report,
        ))
        outcome = await device._async_confirm_stop(control_id, charging_id, extend_for_control=True, ordered_stop=True)
        assert outcome == "timeout" and elapsed == 0.5

    asyncio.run(run())


@pytest.mark.parametrize("feedback", ["stale", "zero", "draw_before_cap_restore", "draw_during_cap_restore"])
def test_expired_guarded_start_requires_ordered_stop_before_cap_rollback(monkeypatch, feedback):
    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    from custom_components.ha_energy_planner import ev_adapter as ev_adapter_module

    async def run():
        charger = Charger()
        device = adapter(charger)
        device.start_guard = lambda: True
        if feedback == "zero":
            device.entry_data.pop("ev_charging_entity")
        else:
            device.entry_data["ev_charging_entity"] = "sensor.charging"
            charger.put("sensor.charging", "idle")
        now = dt_util.utcnow()
        lease = now + timedelta(seconds=1)
        clock = [now]
        monkeypatch.setattr(ev_adapter_module, "dt_util", SimpleNamespace(utcnow=lambda: clock[0]))
        device.confirmation_timeout_seconds = 0 if feedback == "stale" else 0.5
        device.confirmation_poll_seconds = 0.01

        def set_power(value):
            charger.values["sensor.power"] = State(
                "sensor.power", str(value), {"unit_of_measurement": "kW"},
                last_updated=clock[0], last_reported=clock[0],
            )

        async def dispatch(domain, service, data, **kwargs):
            if domain == "switch":
                assert service == "turn_off"
                charger.calls.append((domain, service, data))
                if feedback != "stale":
                    clock[0] += timedelta(milliseconds=1)
                    asyncio.get_running_loop().call_soon(set_power, 0)
                return
            await charger.call(domain, service, data, **kwargs)
            if data["value"] == 3:
                clock[0] = lease
                set_power(3)
            else:
                assert charger.values["sensor.power"].state == "0"
                if feedback == "draw_during_cap_restore":
                    clock[0] += timedelta(milliseconds=1)
                    set_power(5)

        charger.services.async_call = dispatch
        if feedback == "draw_before_cap_restore":
            confirm_stop = device._async_confirm_stop

            async def replace_stop_evidence(*args, **kwargs):
                confirmed = await confirm_stop(*args, **kwargs)
                assert confirmed == "confirmed"
                clock[0] += timedelta(milliseconds=1)
                set_power(5)
                return confirmed

            monkeypatch.setattr(device, "_async_confirm_stop", replace_stop_evidence)
        planned = limited()
        planned.desired_state["charge_lease_until"] = lease.isoformat()
        result = await device.async_execute(planned)
        assert not result.applied and result.command_sent and result.reason == "ev_allocation_expired"
        assert result.safe_state_confirmed is (feedback == "zero")
        assert result.rollback_succeeded is (feedback == "zero")
        restore_calls = ["set_value"] if feedback in {"zero", "draw_during_cap_restore"} else []
        assert [call[1] for call in charger.calls] == [
            "set_value", "turn_off", *restore_calls,
        ]

    asyncio.run(run())


def test_native_stop_rechecks_meter_freshness_before_raising_cap(monkeypatch):
    from homeassistant.core import State

    from custom_components.ha_energy_planner import ev_adapter as ev_adapter_module

    async def run():
        charger = Charger()
        device = adapter(charger)
        charger.put("number.limit", 3, charger.values["number.limit"].attributes)
        near_expiry = NOW + timedelta(minutes=10)
        times = iter((near_expiry, near_expiry + timedelta(seconds=1)))
        monkeypatch.setattr(ev_adapter_module, "dt_util", SimpleNamespace(utcnow=lambda: next(times)))
        earlier = NOW - timedelta(seconds=2)
        charger.values["switch.charger"] = State("switch.charger", "off", last_changed=earlier, last_updated=earlier)
        charger.values["sensor.power"] = State(
            "sensor.power", "0", {"unit_of_measurement": "kW", "sampled_at_utc": NOW.isoformat()}, last_updated=NOW,
        )
        device.retained_draw_at = lambda: NOW - timedelta(seconds=1)
        result = await device.async_restore({"ev_power_limit_entity": "6", "ev_power_limit_unit": "kW"})
        assert not result.applied and result.safe_state_confirmed is False
        assert result.reason == "ev_stop_not_confirmed" and not charger.calls
        assert next(times, None) is None

    asyncio.run(run())


@pytest.mark.parametrize("draw_during_rollback", [False, True])
@pytest.mark.parametrize("owned_lifecycle_cap", [False, True])
def test_superseded_start_rechecks_stop_after_increasing_cap_rollback(draw_during_rollback, owned_lifecycle_cap):
    async def run():
        charger = Charger()
        device = adapter(charger)
        device.start_guard = lambda: not charger.calls
        requested = 7 if owned_lifecycle_cap else 3
        restored = 9 if owned_lifecycle_cap else 6
        if owned_lifecycle_cap:
            charger.put("number.limit", 8, {**charger.values["number.limit"].attributes, "max": 9})
            device.power_options["ev_limit_max"] = 9
            device.stopped_power_limit_baseline = {"ev_power_limit_entity": "9", "ev_power_limit_unit": "kW"}

        async def dispatch(domain, service, data, **kwargs):
            if domain == "switch":
                assert service == "turn_off"
                charger.calls.append((domain, service, data))
                return
            await charger.call(domain, service, data, **kwargs)
            if data["value"] == restored and draw_during_rollback:
                charger.put("sensor.power", 5, {"unit_of_measurement": "kW"})

        charger.services.async_call = dispatch
        result = await device.async_execute(limited(value=requested, physical_power_kw=requested))
        assert not result.applied and result.command_sent and result.reason == "ev_grid_projection_stale"
        assert result.safe_state_confirmed is not draw_during_rollback
        assert result.rollback_succeeded is not draw_during_rollback
        assert not result.power_limit_superseded
        assert [call[1] for call in charger.calls] == [
            "set_value", "set_value", *(["turn_off"] if draw_during_rollback else []),
        ]
        assert [call[2]["value"] for call in charger.calls if call[0] == "number"] == [requested, restored]

    asyncio.run(run())


@pytest.mark.parametrize(("source", "retained"), [
    ("meter", True), ("charging", True), ("charging", False), ("control", False),
])
def test_native_stop_rejects_replayed_source_timestamp_from_the_future(monkeypatch, source, retained):
    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    async def run():
        charger = Charger()
        device = adapter(charger)
        now = dt_util.utcnow()
        retained_draw = now - timedelta(seconds=30)
        earlier_stop = retained_draw - timedelta(seconds=5)
        device.retained_draw_at = lambda: retained_draw if retained else None
        device.entry_data["ev_charging_entity"] = "sensor.charging"
        for entity_id, value in (("switch.charger", "off"), ("sensor.charging", "idle")):
            charger.values[entity_id] = State(entity_id, value, last_updated=earlier_stop, last_changed=earlier_stop)
        entity_id, value = {
            "meter": ("sensor.power", "0"), "charging": ("sensor.charging", "idle"),
            "control": ("switch.charger", "off"),
        }[source]
        attributes = {"unit_of_measurement": "kW"} if source == "meter" else {}
        report_time = now - timedelta(seconds=10)
        received = report_time - timedelta(seconds=10)
        replay = State(
            entity_id, value, {**attributes, "sampled_at_utc": report_time.isoformat()},
            last_changed=earlier_stop, last_updated=received,
        )
        replay.last_reported = now
        charger.values[entity_id] = replay
        if source != "meter":
            charger.put("sensor.power", "unavailable")
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        assert not device.native_restore_stop_is_current()
        charger.values[entity_id] = State(
            entity_id, value, {**attributes, "sampled_at_utc": now.isoformat()},
            last_changed=earlier_stop, last_updated=now,
        )
        assert device.native_restore_stop_is_current()

    asyncio.run(run())


def test_limit_restore_never_increases_power_after_unconfirmed_stop():
    from unittest.mock import AsyncMock

    from custom_components.ha_energy_planner.ev_adapter import EVCommandResult

    async def run():
        charger = Charger()
        device = adapter(charger)
        device._async_stop = AsyncMock(
            return_value=EVCommandResult(False, "stop_failed", {}, {}, safe_state_confirmed=False)
        )
        result = await device.async_restore({"ev_power_limit_entity": "6"})
        assert not result.applied
        planned = await device.async_execute(action(charging_required_now=False, restore_power_limit="6"))
        assert not planned.applied and planned.reason == "stop_failed"
        assert charger.calls == []
        charger.confirm = False
        device.confirmation_timeout_seconds = 0.003
        device.confirmation_poll_seconds = 0.001
        assert not await device._async_write_number("number.limit", 3)

    asyncio.run(run())


def test_default_policy_upgrade_preserves_production_fingerprint():
    from custom_components.ha_energy_planner.ev_policy import EV_DEFAULTS
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    old = {key: value for key, value in DEFAULT_OPTIONS.items() if key not in EV_DEFAULTS}
    assert production_evidence_fingerprint({}, old) == production_evidence_fingerprint({}, DEFAULT_OPTIONS)
    changed = {**DEFAULT_OPTIONS, "ev_emergency_budget": 5}
    assert production_evidence_fingerprint({}, changed) != production_evidence_fingerprint({}, DEFAULT_OPTIONS)


def test_price_authority_is_revoked_independently_of_new_commands():
    from custom_components.ha_energy_planner.ev_runtime import price_stop_required

    opts = {**DEFAULT_OPTIONS, "ev_price_limit_enabled": True, "ev_max_import_price": 0.2}
    assert not price_stop_required(DEFAULT_OPTIONS, {}, None)
    assert price_stop_required(opts, {}, None)
    assert not price_stop_required(opts, {}, 0.1)
    assert price_stop_required(opts, {}, 0.3)
    opts.update(ev_price_policy="departure_priority", ev_emergency_price=0.5, ev_emergency_budget=1)
    assert not price_stop_required(opts, {"emergency_spend": 0.9}, 0.3)
    assert price_stop_required(opts, {"emergency_spend": 1}, 0.3)
    assert price_stop_required(opts, {}, 0.6)
    assert price_stop_required(opts, {"budget_uncertain": True}, 0.3)
    assert (
        allocation_deadline(action(), opts, {"budget_uncertain": True}, NOW, 0.3)[2] == "ev_emergency_spend_uncertain"
    )


def test_restore_stops_before_rejecting_replaced_limit_unit():
    charger = Charger()
    charger.put("switch.charger", "on")
    result = asyncio.run(adapter(charger).async_restore({"ev_power_limit_entity": 16, "ev_power_limit_unit": "A"}))
    assert result.reason == "ev_power_limit_unit_changed"
    assert charger.values["switch.charger"].state == "off"
    assert all(domain != "number" for domain, _, _ in charger.calls)


def test_number_confirmation_does_not_release_capacity_with_high_measured_load():
    charger = Charger()

    async def run():
        original = charger.call

        async def high_load(domain, service, data, **kwargs):
            await original(domain, service, data, **kwargs)
            charger.put("sensor.power", 6, {"unit_of_measurement": "kW"})

        charger.services.async_call = high_load
        result = await adapter(charger).async_execute(
            action(power_limit={"entity_id": "number.limit", "value": 3, "unit": "kW", "physical_power_kw": 3})
        )
        assert result.applied
        assert "confirmed_power_kw" not in result.post_state

    asyncio.run(run())


def test_number_writes_obey_vehicle_session_guard_before_and_after_service():
    async def run():
        charger = Charger()
        guarded = adapter(charger)
        guarded.command_guard = lambda: False
        assert not await guarded._async_write_number("number.limit", 3)
        assert not charger.calls
        checks = iter((True, False))
        guarded.command_guard = lambda: next(checks)
        assert not await guarded._async_write_number("number.limit", 3)
        assert len(charger.calls) == 1
    asyncio.run(run())
