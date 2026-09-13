"""EV power command ordering, leases and durable spending recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.ev_adapter import EVChargerAdapter
from custom_components.ha_energy_planner.ev_runtime import (
    allocation_deadline,
    audit_evidence,
    settle_spending,
    timestamp,
)
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction

NOW = datetime(2026, 9, 13, tzinfo=UTC)


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
        assert not result.applied and result.safe_state_confirmed is False
        result = await device.async_restore({"ev_power_limit_entity": "6"})
        assert not result.applied

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
