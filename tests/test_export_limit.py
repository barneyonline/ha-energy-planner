"""Independent export tariff policy and gateway-confirmed ownership regressions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientResponseError, ServerDisconnectedError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ha_energy_planner import enphase_export_limit as runtime
from custom_components.ha_energy_planner.action_limits import action_budget, budget_history, reanchor_export_attempts
from custom_components.ha_energy_planner.calendar_history import confirm_calendar_action
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_BYPASS_SAFETY_GATES,
    CONF_COMMAND_RATE_LIMIT_SECONDS,
    CONF_DRY_RUN,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
    CONF_ENPHASE_PROFILE,
    CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS,
    CONF_PLANNER_ENABLED,
    CONF_PRICE_FRESHNESS_MINUTES,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.coordinator import _PlanExecution
from custom_components.ha_energy_planner.export_limit_policy import (
    EXPORT_ASSET,
    area_safe,
    assumed_zero_export,
    aware_time,
    block_time,
    build_actions,
    tariff_blocks,
)
from custom_components.ha_energy_planner.models import (
    ActionKind,
    DecisionContext,
    DecisionSlot,
    InputHealth,
    OccupancyState,
    OutcomeResult,
    to_jsonable,
)
from custom_components.ha_energy_planner.planner import DryRunPlanner

NOW = datetime(2026, 10, 1, 0, 10, tzinfo=UTC)
GATEWAY = "a" * 64
ENTITY = "sensor.enphase_export_limit"
DATA = {CONF_ENPHASE_EXPORT_LIMIT_ENTITY: ENTITY, CONF_AMBER_EXPORT_PRICE: "sensor.export_price"}
OPTIONS = {**DEFAULT_OPTIONS, CONF_PLANNER_ENABLED: True, CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED: True}


def price_state(prices=(-0.1, 0.0, 0.2), *, start=None, explicit=True, unit="$/kWh"):
    start = start or NOW.replace(minute=0)
    return SimpleNamespace(
        state="-0.1",
        last_updated=NOW,
        attributes={
            "unit_of_measurement": unit,
            "forecasts": [
                dict(
                    start_time=(start + timedelta(minutes=i * 30)).isoformat(),
                    per_kwh=value,
                    **({"end_time": (start + timedelta(minutes=(i + 1) * 30)).isoformat()} if explicit else {}),
                )
                for i, value in enumerate(prices)
            ],
        },
    )


def amber_price_state(prices=(-0.1, 0.0, 0.2), *, start=None):
    """Sanitized Amber detailedForecast shape with inclusive first-second starts."""
    state = price_state(prices, start=start)
    items = state.attributes.pop("forecasts")
    for item in items:
        item["duration"] = 30
        item["start_time"] = (datetime.fromisoformat(item["start_time"]) + timedelta(seconds=1)).isoformat()
    state.attributes["detailedForecast"] = items
    return state


def limit_state(watts=None, *, pending=False, slew=100.0, readback=None):
    return SimpleNamespace(
        state="pending" if pending else "disabled" if watts is None else "zero_export" if watts == 0 else "limited",
        last_updated=NOW,
        attributes={
            "confirmed_watts": watts,
            "slew_rate": slew,
            "pending": pending,
            "requested_watts": 0 if pending else None,
            "requested_action": "set" if pending else None,
            "pending_requested_at": NOW.isoformat() if pending else None,
            "request_status": "pending" if pending else "confirmed",
            "last_successful_readback": readback if readback is not None else NOW.timestamp(),
            "gateway_identity": GATEWAY,
            "gateway_identity_verified": True,
        },
    )


class Store:
    def __init__(self):
        self.data = {"ownership": {}, "production": {"armed": True, "dry_run_ready_cycles": 3}, "execution_audit": []}
        self.flushes = 0
        self.fail = False

    async def async_save_restore_notification_failures(self, failures: dict[str, Any]) -> None:
        self.data["restore_notification_failures"] = failures

    async def async_save_ownership(self, value):
        self.data["ownership"] = deepcopy(value)

    async def async_save_control_pause(self, value):
        self.data["control_pause"] = deepcopy(value)

    async def async_save_production(self, value):
        self.data["production"] = to_jsonable(value)

    async def async_flush(self):
        self.flushes += 1
        if self.fail:
            raise OSError("disk failed")

    @asynccontextmanager
    async def async_delay_save(self):
        yield

    def defer_save(self):
        return nullcontext()

    async def async_flush_deferred(self):
        pass

    async def async_add_outcome(self, value):
        self.data["execution_audit"].append(to_jsonable(value))
        self.data["calendar_history"] = confirm_calendar_action(
            self.data.get("calendar_history"), to_jsonable(value),
        )

    async def async_save_command_rate_limits(self, value):
        self.data["command_rate_limits"] = value

    async def async_reanchor_export_attempts(self, now):
        self.data["action_attempts"] = reanchor_export_attempts(budget_history(self.data), now)


@pytest.fixture
def setup(monkeypatch):
    states = {ENTITY: limit_state(), "sensor.export_price": price_state()}
    registry = {
        ENTITY: SimpleNamespace(
            platform="enphase_ev",
            domain="sensor",
            config_entry_id="site-a",
            unique_id="enphase_site-a_export_limit",
            device_id="gateway-a",
        )
    }
    monkeypatch.setattr(runtime.er, "async_get", lambda hass: SimpleNamespace(
        async_get=registry.get,
        async_get_entity_id=lambda domain, platform, unique_id: next((
            entity for entity, entry in registry.items()
            if (entry.domain, entry.platform, entry.unique_id) == (domain, platform, unique_id)
        ), None),
    ))
    calls = []
    mode = {"value": "pending"}
    store = Store()

    async def call(domain, service, data, **kwargs):
        assert store.flushes > 0
        if domain == "enphase_ev" and "expected_gateway" in data:
            assert data["expected_gateway"] == states[ENTITY].attributes["gateway_identity"]
            data = {key: value for key, value in data.items() if key != "expected_gateway"}
        calls.append((domain, service, data))
        if domain != "enphase_ev":
            return
        if mode["value"] == "reject":
            raise ServiceValidationError("rejected")
        if mode["value"] == "timeout":
            raise TimeoutError
        if mode["value"] == "cancel":
            raise asyncio.CancelledError
        watts = data.get("limit_watts")
        states[ENTITY] = limit_state(
            watts if mode["value"] == "confirm" else states[ENTITY].attributes["confirmed_watts"],
            pending=mode["value"] == "pending",
            slew=data.get("slew_rate", states[ENTITY].attributes["slew_rate"]),
            readback=clock["now"].timestamp(),
        )
        if mode["value"] == "pending":
            states[ENTITY].attributes.update(
                requested_watts=watts, requested_action="disable" if watts is None else "set"
            )

    hass = SimpleNamespace(
        states=SimpleNamespace(get=states.get),
        services=SimpleNamespace(has_service=lambda domain, service: True, async_call=call),
        data={},
        config=SimpleNamespace(time_zone="UTC"),
    )

    clock = {"now": NOW}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr("homeassistant.util.dt.utcnow", lambda: clock["now"])
    return SimpleNamespace(
        hass=hass,
        store=store,
        states=states,
        registry=registry,
        calls=calls,
        mode=mode,
        clock=clock,
        control=runtime.ExportLimitControl(hass, store, DATA),
    )


def advance_clock(setup, seconds=300):
    """Publish fresh feedback after the mandatory export change interval."""
    setup.clock["now"] += timedelta(seconds=seconds)
    for state in setup.states.values():
        if "last_successful_readback" in state.attributes:
            state.attributes["last_successful_readback"] = setup.clock["now"].timestamp()
    setup.states["sensor.export_price"].last_updated = setup.clock["now"]
    return setup.clock["now"]


def context(evidence, now=NOW):
    return DecisionContext(
        now,
        "export-plan",
        [DecisionSlot(now, 0.2, -0.1, 5.0, 1.0)],
        None,
        None,
        OccupancyState.UNKNOWN,
        InputHealth.UNSAFE,
        export_limit=evidence,
    )


def action(setup):
    return build_actions(context(runtime.evidence(setup.hass, DATA, OPTIONS, setup.clock["now"])))[0]


@pytest.mark.parametrize(
    "prices,expected", [((-0.1, -0.2), [0, 0]), ((0.0, 0.1), [None, None]), ((-0.0, -0.1), [None, 0])]
)
@pytest.mark.parametrize("explicit", [False, True])
def test_half_hour_sign_and_units(prices, expected, explicit):
    blocks, issue = tariff_blocks(price_state(prices, explicit=explicit, unit="c/kWh"), NOW, OPTIONS)
    assert issue is None
    assert [b["watts"] for b in blocks] == expected
    assert blocks[0]["price"] == prices[0] / 100
    assert block_time(blocks[0]["end"]) == NOW.replace(minute=30)


@pytest.mark.parametrize("offset,expected", [(0, 0), (1, 0), (1799, 0), (1800, None), (1801, None)])
@pytest.mark.parametrize("camel_case", [False, True])
def test_amber_half_hour_boundary_sign_and_native_precedence(offset, expected, camel_case):
    start = NOW.replace(minute=0)
    state = amber_price_state()
    if camel_case:
        for item in state.attributes["detailedForecast"]:
            item["startTime"] = item.pop("start_time")
            item["endTime"] = item.pop("end_time")
            item["perKwh"] = item.pop("per_kwh")
    # Convenience forecasts must not override the native prices.
    state.attributes["forecast"] = price_state((0.2, -0.1, -0.1)).attributes["forecasts"]
    now = start + timedelta(seconds=offset)
    state.last_updated = now
    blocks, issue = tariff_blocks(state, now, OPTIONS)
    assert issue is None
    assert blocks[0]["watts"] == expected
    assert block_time(blocks[0]["start"]) == start + timedelta(minutes=30 if offset >= 1800 else 0)
    assert block_time(blocks[0]["end"]) == block_time(blocks[1]["start"])
    assert all(block_time(block["end"]) - block_time(block["start"]) == timedelta(minutes=30)
               for block in blocks)


@pytest.mark.parametrize("native_index", [0, 1, 2])
def test_amber_and_exact_half_hours_can_be_mixed(native_index):
    state = price_state()
    state.attributes["forecasts"][native_index] = amber_price_state().attributes["detailedForecast"][native_index]
    blocks, issue = tariff_blocks(state, NOW, OPTIONS)
    assert issue is None
    assert [block["watts"] for block in blocks] == [0, None]


def test_amber_float_duration_and_input_are_preserved():
    state = amber_price_state()
    state.attributes["detailedForecast"][0]["duration"] = 30.0
    original = deepcopy(state.attributes)
    assert tariff_blocks(state, NOW, OPTIONS)[1] is None
    assert state.attributes == original


@pytest.mark.parametrize("duration", [None, 5, 15, "30", True, float("nan")])
def test_amber_short_interval_requires_explicit_numeric_half_hour_duration(duration):
    state = amber_price_state()
    state.attributes["detailedForecast"][0]["duration"] = duration
    assert tariff_blocks(state, NOW, OPTIONS) == ([], "export_tariff_overlap_or_duration")


@pytest.mark.parametrize("duration", [15, 30])
def test_amber_declared_duration_matches_exact_half_hour(duration):
    state = price_state()
    rows = state.attributes.pop("forecasts")
    rows[0]["duration"] = duration
    state.attributes.update(detailedForecast=rows, forecast_interval_minutes=30, interval_minutes=30)
    blocks, issue = tariff_blocks(state, NOW, OPTIONS)
    if duration == 15:
        assert (blocks, issue) == ([], "export_tariff_overlap_or_duration")
    else:
        assert issue is None
        assert blocks[0]["watts"] == 0


@pytest.mark.parametrize("start_delta,end_delta", [(1, 0), (-2, 0), (0, -1), (60, 60), (0.000001, 0)])
def test_amber_normalization_rejects_other_timestamp_offsets(start_delta, end_delta):
    state = amber_price_state()
    item = state.attributes["detailedForecast"][0]
    item["start_time"] = (datetime.fromisoformat(item["start_time"]) + timedelta(seconds=start_delta)).isoformat()
    item["end_time"] = (datetime.fromisoformat(item["end_time"]) + timedelta(seconds=end_delta)).isoformat()
    assert tariff_blocks(state, NOW, OPTIONS) == ([], "export_tariff_overlap_or_duration")


@pytest.mark.parametrize("overlap", [False, True])
def test_amber_normalization_preserves_real_gap_and_overlap_rejection(overlap):
    state = amber_price_state()
    items = state.attributes["detailedForecast"]
    if overlap:
        items[1] = deepcopy(items[0])
    else:
        items.pop(1)
    assert tariff_blocks(state, NOW, OPTIONS) == (
        [], "export_tariff_overlap_or_duration" if overlap else "export_tariff_gap",
    )


def test_amber_normalization_handles_dst_in_absolute_time():
    local = datetime(2026, 10, 4, 1, 30, tzinfo=ZoneInfo("Australia/Melbourne"))
    state = amber_price_state(start=local.astimezone(UTC))
    state.last_updated = local
    blocks, issue = tariff_blocks(state, local, OPTIONS)
    assert issue is None
    assert block_time(blocks[1]["start"]).astimezone(local.tzinfo).hour == 3
    assert block_time(blocks[1]["start"]) - block_time(blocks[0]["start"]) == timedelta(minutes=30)


def test_amber_export_execution_confirms_sign_changes_without_repeating_commands(setup):
    async def run():
        setup.states["sensor.export_price"] = amber_price_state()
        setup.mode["value"] = "confirm"
        planned = action(setup)
        assert (await setup.control.execute(planned, NOW))[0] == OutcomeResult.PENDING
        assert await setup.control.reconcile(NOW) is None
        assert [call[1] for call in setup.calls] == ["set_export_limit"]
        remaining = build_actions(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))
        assert [action.kind for action in remaining] == [ActionKind.DISABLE_EXPORT_LIMIT]
        assert remaining[0].execute_not_before == NOW.replace(minute=30)
        assert (await setup.control.execute(planned, NOW))[0] == OutcomeResult.SKIPPED
        assert len(setup.calls) == 1
        boundary = NOW.replace(minute=30)
        setup.clock["now"] = boundary
        setup.states[ENTITY] = limit_state(0, readback=boundary.timestamp())
        setup.states["sensor.export_price"].last_updated = boundary
        assert (await setup.control.execute(remaining[0], boundary))[0] == OutcomeResult.PENDING
        assert await setup.control.reconcile(boundary) is None
        assert [call[1] for call in setup.calls] == ["set_export_limit", "disable_export_limit"]
        assert (await setup.control.execute(remaining[0], boundary))[0] == OutcomeResult.SKIPPED
        assert len(setup.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda s: setattr(s, "state", "unavailable"), "export_tariff_unavailable"),
        (lambda s: setattr(s, "last_updated", NOW - timedelta(minutes=31)), "export_tariff_stale"),
        (lambda s: s.attributes.update(confidence=0.1), "export_tariff_confidence_low"),
        (lambda s: s.attributes.update(forecasts=[-1]), "export_tariff_invalid"),
        (lambda s: s.attributes["forecasts"][0].update(start_time="2026-10-01T00:00:00"), "export_tariff_invalid"),
        (lambda s: s.attributes["forecasts"][0].update(per_kwh=float("nan")), "export_tariff_invalid"),
        (lambda s: s.attributes["forecasts"][0].update(per_kwh=10**1000), "export_tariff_invalid"),
        (lambda s: s.attributes["forecasts"][0].update(end_time="bad"), "export_tariff_invalid"),
        (
            lambda s: s.attributes["forecasts"][0].update(end_time="2026-10-01T00:20:00+00:00"),
            "export_tariff_overlap_or_duration",
        ),
        (
            lambda s: s.attributes["forecasts"][1].update(
                start_time="2026-10-01T00:15:00+00:00", end_time="2026-10-01T00:45:00+00:00"
            ),
            "export_tariff_overlap_or_duration",
        ),
        (lambda s: s.attributes.update(forecasts=[]), "export_tariff_block_missing"),
        (
            lambda s: s.attributes.update(forecasts=[{"start_time": "2026-10-01T00:00:00+00:00", "per_kwh": -1}]),
            "export_tariff_ambiguous_duration",
        ),
    ],
)
def test_invalid_tariff_holds(mutation, reason):
    state = price_state()
    mutation(state)
    assert tariff_blocks(state, NOW, OPTIONS) == ([], reason)
    assert tariff_blocks(None, NOW, OPTIONS)[1] == "export_tariff_unavailable"


@pytest.mark.parametrize("cached", [False, True])
def test_ineligible_export_tariff_skips_full_row_validation(monkeypatch, cached):
    from custom_components.ha_energy_planner import export_limit_policy as policy

    tariff = price_state((-0.1,) * 512)
    tariff.attributes["forecasts"][-1]["per_kwh"] = "invalid"
    tariff.last_updated = NOW - timedelta(minutes=31)
    tariff.attributes["confidence"] = 0.1
    parse = Mock(wraps=policy._parse_blocks)
    monkeypatch.setattr(policy, "_parse_blocks", parse)
    blocks = policy.ExportTariffCache().blocks if cached else tariff_blocks

    assert blocks(tariff, NOW, OPTIONS) == ([], "export_tariff_stale")
    tariff.last_updated = NOW
    assert blocks(tariff, NOW, OPTIONS) == ([], "export_tariff_confidence_low")
    parse.assert_not_called()

    tariff.attributes["confidence"] = 1.0
    assert blocks(tariff, NOW, OPTIONS) == ([], "export_tariff_invalid")
    parse.assert_called_once()


def test_explicit_cadence_gaps_and_dst():
    s = price_state((-1,), explicit=False)
    s.attributes["forecast_interval_minutes"] = 30
    assert tariff_blocks(s, NOW, OPTIONS)[1] is None
    s = price_state((-1, 1))
    s.attributes["forecasts"].pop(1)
    assert len(tariff_blocks(s, NOW, OPTIONS)[0]) == 1
    assert aware_time("naive") is None
    local = datetime(2026, 10, 4, 1, 50, tzinfo=ZoneInfo("Australia/Melbourne")).astimezone(UTC)
    s = price_state((-1, 1), start=local.replace(minute=30))
    s.last_updated = local
    assert tariff_blocks(s, local, OPTIONS)[1] is None


@pytest.mark.parametrize("path", sorted(
    path for path in (Path(__file__).parent / "fixtures" / "live_schema").glob("export_tariff_*.json")
    if path.name.startswith(("export_tariff_cadence_", "export_tariff_conflicting_"))
    or "_out_of_range" in path.name
), ids=lambda path: path.stem)
def test_export_tariff_rejects_invalid_live_schema(path):
    fixture = json.loads(path.read_text())
    now = datetime.fromisoformat(fixture["now"])
    state = SimpleNamespace(**fixture["state"])
    state.last_updated = datetime.fromisoformat(state.last_updated)
    assert tariff_blocks(state, now, OPTIONS) == (fixture["expected"], fixture["expected_issue"])


@pytest.mark.parametrize("ignored", ["invalid", "sNaN", None, 0, "-1e999999999999999999999"])
def test_export_continues_to_accept_agreeing_cadence_aliases(ignored):
    state = price_state((-0.1,), explicit=False)
    state.attributes.update(forecast_interval_minutes="30", forecastIntervalMinutes=30.0, interval_minutes=ignored)
    blocks, issue = tariff_blocks(state, NOW, OPTIONS)
    assert issue is None
    assert blocks[0]["watts"] == 0


def test_preview_transitions_and_price_only_planning(setup):
    ev = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
    ctx = context(ev)
    actions = build_actions(ctx)
    assert [a.kind for a in actions] == [ActionKind.SET_EXPORT_LIMIT, ActionKind.DISABLE_EXPORT_LIMIT]
    assert actions[1].execute_not_before == NOW.replace(minute=30)
    assert assumed_zero_export(ctx, NOW)
    assert not assumed_zero_export(ctx, NOW.replace(minute=30))
    plan = DryRunPlanner(OPTIONS).create_plan(ctx)
    assert area_safe(plan) and plan.health == InputHealth.UNSAFE
    assert plan.mode == "DRY_RUN" and plan.estimated_daily_cost == 0
    assert plan.preview[0]["pv_forecast_kw"] == 5
    assert plan.device_plans[EXPORT_ASSET]["timeline"][0]["state"] == "Enabled — 0 W"
    assert DryRunPlanner({**OPTIONS, CONF_DRY_RUN: False}).create_plan(ctx).mode == "ACTIVE_DEGRADED"
    setup.states[ENTITY] = limit_state(0)
    assert len(build_actions(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))) == 1
    setup.states["sensor.export_price"] = price_state((-1, -1))
    assert not build_actions(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))
    assert build_actions(context({})) == []
    assert DryRunPlanner({**OPTIONS, CONF_PLANNER_ENABLED: False}).create_plan(ctx).actions == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"last_successful_readback": None}, "export_limit_entity_unavailable"),
        ({"last_successful_readback": "invalid"}, "export_limit_readback_invalid"),
        ({"last_successful_readback": 10**400}, "export_limit_readback_invalid"),
        ({"last_successful_readback": NOW.timestamp() - 601}, "export_limit_readback_stale"),
        ({"slew_rate": 0}, "export_limit_unsupported"),
        ({"slew_rate": 10**400}, "export_limit_unsupported"),
        ({"pending": "false"}, "export_limit_unsupported"),
        ({"confirmed_watts": True}, "export_limit_unsupported"),
    ],
)
def test_sensor_contract(setup, change, reason):
    setup.states[ENTITY].attributes.update(change)
    assert runtime.feedback(setup.hass, ENTITY, NOW)[1] == reason
    assert not runtime.evidence(setup.hass, DATA, OPTIONS, NOW)["ready"]


def test_sensor_identity_services_and_pending(setup):
    assert runtime.target_identity(setup.hass, ENTITY)["config_entry_id"] == "site-a"
    setup.registry[ENTITY].platform = "other"
    assert runtime.feedback(setup.hass, ENTITY, NOW)[1] == "export_limit_target_invalid"
    setup.registry[ENTITY].platform = "enphase_ev"
    setup.states[ENTITY] = limit_state(0, pending=True)
    assert runtime.evidence(setup.hass, DATA, OPTIONS, NOW)["reason"] == "export_limit_pending"
    setup.states[ENTITY] = limit_state()
    setup.hass.services.has_service = lambda *args: False
    assert runtime.evidence(setup.hass, DATA, OPTIONS, NOW)["reason"] == "export_limit_services_unavailable"
    setup.states.pop(ENTITY)
    assert runtime.feedback(setup.hass, ENTITY, NOW)[1] == "export_limit_entity_unavailable"


@pytest.mark.parametrize("identity_fields", [
    {"gateway_identity_verified": False},
    {"gateway_identity_verified": True},
    {"gateway_identity": "short", "gateway_identity_verified": True},
    {"gateway_identity": "g" * 64, "gateway_identity_verified": True},
    {"gateway_identity": GATEWAY},
])
def test_explicit_invalid_gateway_metadata_blocks_an_unowned_control(setup, identity_fields):
    async def run():
        requested = action(setup)
        attributes = setup.states[ENTITY].attributes
        attributes.pop("gateway_identity")
        attributes.pop("gateway_identity_verified")
        attributes.update(identity_fields)
        evidence = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
        assert evidence["reason"] == runtime.GATEWAY_UNVERIFIED
        assert not evidence["feedback_ready"]
        assert not build_actions(context(evidence))
        assert (await setup.control.execute(requested, NOW))[1:] == (runtime.GATEWAY_UNVERIFIED, False)
        assert not setup.control.state
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("invalid_endpoint", ["implicit_start", "declared_end"])
def test_export_tariff_outside_utc_range_does_not_abort_input_collection(setup, invalid_endpoint):
    import json
    from pathlib import Path

    from custom_components.ha_energy_planner.const import CONF_AMBER_IMPORT_PRICE, CONF_PV_FORECAST
    from custom_components.ha_energy_planner.inputs import InputManager

    fixture_path = Path(__file__).parent / "fixtures/live_schema/export_tariff_start_utc_out_of_range.json"
    fixture = json.loads(fixture_path.read_text())
    state = SimpleNamespace(**fixture["state"])
    state.last_updated = datetime.fromisoformat(state.last_updated)
    if invalid_endpoint == "declared_end":
        row = state.attributes["forecasts"][0]
        row.update(start_time=NOW.replace(minute=0).isoformat(), end_time=row["start_time"], duration=30)
    setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = state
    setup.states["sensor.import"] = price_state()
    setup.states["sensor.pv"] = SimpleNamespace(state="1", attributes={"forecast": [1.0] * 48}, last_updated=NOW)
    data = {**DATA, CONF_AMBER_IMPORT_PRICE: "sensor.import", CONF_PV_FORECAST: "sensor.pv"}

    built = InputManager(setup.hass, data, OPTIONS).build_context()

    assert built.export_limit["reason"] == fixture["expected_issue"]
    assert not build_actions(built)
    assert built.slots[0].pv_forecast_kw == 1.0
    assert not setup.calls


@pytest.fixture
def select_setup(setup):
    """The control has no readback attributes and a differently named sensor."""
    select = "select.gateway_export_control"
    sensor = "sensor.renamed_gateway_feedback"
    setup.states[sensor] = setup.states.pop(ENTITY)
    setup.registry[sensor] = setup.registry.pop(ENTITY)
    setup.states[select] = SimpleNamespace(state="disable_limit", attributes={"default_limit_watts": 5000})
    setup.registry[select] = SimpleNamespace(**{**vars(setup.registry[sensor]), "domain": "select"})
    # Keep the existing fake service updating the readback at its new name.
    setup.states[ENTITY] = setup.states[sensor]
    original_call = setup.hass.services.async_call

    async def call(*args, **kwargs):
        await original_call(*args, **kwargs)
        setup.states[sensor] = setup.states[ENTITY]

    setup.hass.services.async_call = call
    setup.select, setup.sensor = select, sensor
    setup.mapping = {**DATA, CONF_ENPHASE_EXPORT_LIMIT_ENTITY: select}
    setup.control = runtime.ExportLimitControl(setup.hass, setup.store, setup.mapping)
    return setup


@pytest.mark.parametrize("failure", [None, "missing", "entry", "device", "stale", "unavailable", "sensor_unavailable"])
def test_select_resolves_only_its_matching_readback(select_setup, failure):
    setup = select_setup
    from custom_components.ha_energy_planner.config_flow import ENPHASE_DATA_SCHEMA, _validate_config

    assert ENPHASE_DATA_SCHEMA({CONF_ENPHASE_EXPORT_LIMIT_ENTITY: setup.select})
    assert _validate_config(setup.hass, setup.mapping) == {}
    assert runtime.feedback_entity_id(setup.hass, setup.select) == setup.sensor
    if failure == "missing":
        setup.registry.pop(setup.sensor)
    elif failure == "entry":
        setup.registry[setup.sensor].config_entry_id = "other-site"
    elif failure == "device":
        setup.registry[setup.sensor].device_id = "other-gateway"
    elif failure == "stale":
        setup.states[setup.sensor].attributes["last_successful_readback"] = NOW.timestamp() - 601
    elif failure == "unavailable":
        setup.states[setup.select].state = "unavailable"
    elif failure == "sensor_unavailable":
        setup.states[setup.sensor] = SimpleNamespace(state="unavailable", attributes={})
    observed, issue = runtime.feedback(setup.hass, setup.select, NOW)
    assert (issue is None) is (failure is None)
    assert runtime.evidence(setup.hass, setup.mapping, OPTIONS, NOW)["ready"] is (failure is None)
    if failure is None:
        assert observed["watts"] is None
        assert observed["identity"]["entity_id"] == setup.select
    if failure == "sensor_unavailable":
        assert issue == "export_limit_entity_unavailable"
        assert observed["identity"]["entity_id"] == setup.select
    assert runtime.feedback_entity_id(setup.hass, "select.missing") is None


@pytest.mark.parametrize("mapped_profile", [False, True])
def test_select_export_only_execution_never_changes_profile(select_setup, mapped_profile):
    from custom_components.ha_energy_planner.const import (
        CONF_ENPHASE_AI_PROFILE,
        CONF_ENPHASE_CONTROL_ENABLED,
        CONF_ENPHASE_PROFILE,
    )
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    setup = select_setup

    async def run():
        mapping = dict(setup.mapping)
        if mapped_profile:
            mapping[CONF_ENPHASE_PROFILE] = "select.system_profile"
            mapping[CONF_ENPHASE_AI_PROFILE] = "AI Optimisation"
            setup.states["select.system_profile"] = SimpleNamespace(state="Full Backup", attributes={})
        opts = {**OPTIONS, CONF_DRY_RUN: False, CONF_ENPHASE_CONTROL_ENABLED: False}
        setup.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(mapping, opts)
        executor = Executor(setup.store, hass=setup.hass, entry_data=mapping, options=opts)
        ctx = context(runtime.evidence(setup.hass, mapping, opts, NOW))
        plan = DryRunPlanner(opts).create_plan(ctx)
        plan.actions = plan.actions[:1]
        await executor.async_evaluate(plan, ctx)
        assert setup.calls == [("enphase_ev", "set_export_limit", {"entity_id": setup.select, "limit_watts": 0})]
        assert setup.control.state["pending"]["watts"] == 0
        # The unchanged Select option is not confirmation, nor is acceptance.
        assert setup.control.state["baseline"]["watts"] is None
        setup.states[setup.sensor] = limit_state(0)
        assert await setup.control.reconcile(NOW) is None
        assert not setup.control.state.get("pending")
        setup.mode["value"] = "confirm"
        assert (await setup.control.restore(advance_clock(setup)))[0] == OutcomeResult.RESTORED
        assert setup.calls[-1] == ("enphase_ev", "disable_export_limit", {
            "entity_id": setup.select, "slew_rate": 100.0,
        })
        for reason in ("configuration_changed", "startup_auto_recovery", "entry_unload"):
            restored = await executor.async_restore_safe_state(reason)
            assert restored.result == OutcomeResult.RESTORED
            assert "enphase_profile" not in restored.reason
        assert all(
            (domain == "enphase_ev" and service in {"set_export_limit", "disable_export_limit"})
            or (domain == "persistent_notification" and service == "dismiss")
            for domain, service, _ in setup.calls
        )
        if mapped_profile:
            assert setup.states["select.system_profile"].state == "Full Backup"

    asyncio.run(run())


def test_select_readback_attributes_trigger_refresh(select_setup, monkeypatch):
    from test_coordinator import FakeEvent, FakeHass, _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module

    setup = select_setup
    callbacks, watched, requests = [], [], []
    monkeypatch.setattr(module, "async_track_state_change_event", lambda hass, ids, callback: (
        watched.extend(ids), callbacks.append(callback), lambda: None
    )[-1])
    monkeypatch.setattr(module, "async_call_later", lambda *args: lambda: None)
    owner = _coordinator_for_runtime_services(entry_data=setup.mapping, options=OPTIONS, hass=FakeHass())
    owner.hass.bus = SimpleNamespace(async_listen=lambda *args: lambda: None)
    owner._schedule_debounced_refresh = lambda *args, **kwargs: requests.append((args, kwargs))
    owner.async_start_listeners()
    assert setup.sensor in watched and setup.select in watched
    callbacks[0](FakeEvent(setup.sensor, "disabled", "disabled"))
    assert requests[-1][0] == ("export_limit_feedback",)
    assert requests[-1][1]["force"]


@pytest.mark.parametrize("change", ["rename", "late_registration", "remove", "shared_input"])
def test_select_readback_listener_follows_registry_changes(select_setup, monkeypatch, change):
    from test_coordinator import FakeEvent, FakeHass, _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module

    setup = select_setup
    registered_sensor = setup.registry[setup.sensor]
    if change in {"late_registration", "shared_input"}:
        setup.registry.pop(setup.sensor)
    if change == "shared_input":
        setup.mapping["pv_forecast_secondary_entity"] = setup.sensor
    listeners, registry_callbacks, requests = {}, [], []

    def track(hass, ids, callback):
        for entity in ids:
            listeners[entity] = callback

        def remove():
            for entity in ids:
                listeners.pop(entity, None)

        return remove

    monkeypatch.setattr(module, "async_track_state_change_event", track)
    monkeypatch.setattr(module, "async_call_later", lambda *args: lambda: None)
    hass = FakeHass()
    hass.bus = SimpleNamespace(async_listen=lambda event, cb: (
        registry_callbacks.append((event, cb)) or (lambda: registry_callbacks.clear())))
    owner = _coordinator_for_runtime_services(entry_data=setup.mapping, options=OPTIONS, hass=hass)
    owner._schedule_debounced_refresh = lambda *args, **kwargs: requests.append((args, kwargs))
    owner.async_start_listeners()
    assert registry_callbacks, "Select readback must track registry additions and renames"
    event_name, registry_changed = registry_callbacks[0]
    assert event_name == "entity_registry_updated"
    # Unrelated changes should neither recreate listeners nor trigger a plan.
    registry_changed(SimpleNamespace(data={"entity_id": "sensor.unrelated", "action": "update"}))
    assert requests == []
    old_sensor = setup.sensor
    if change in {"rename", "remove"}:
        setup.registry.pop(old_sensor)
    if change == "rename":
        setup.sensor = "sensor.new_feedback_name"
    if change == "remove":
        registry_changed(SimpleNamespace(data={"entity_id": old_sensor, "action": "remove"}))
        assert old_sensor not in listeners
        assert requests[-1][1]["force"]
        # Recreating the matching sensor restores immediate readback observation.
    setup.registry[setup.sensor] = registered_sensor
    registry_changed(SimpleNamespace(data={"entity_id": setup.sensor, "action": "create"}))
    assert setup.sensor in listeners
    if change == "rename":
        assert old_sensor not in listeners
    listeners[setup.sensor](FakeEvent(setup.sensor, "disabled", "disabled"))
    assert requests[-1] == (("export_limit_feedback",), {"debounce_seconds": 0, "force": True})
    # Shutdown removes both subscriptions and a queued registry event stays inert.
    owner._begin_shutdown()
    assert not listeners and not registry_callbacks
    registry_changed(SimpleNamespace(data={"entity_id": setup.sensor, "action": "remove"}))
    assert not listeners


@pytest.mark.parametrize("existing_select", [False, True])
@pytest.mark.parametrize(
    "other_target", ["same", "different_entry", "different_unique", "unregistered", "unconfigured"])
def test_export_limit_aliases_cannot_be_shared_by_planners(select_setup, existing_select, other_target):
    from custom_components.ha_energy_planner.config_flow import SUBENTRY_ENPHASE, _validate_subentry_config

    setup = select_setup
    existing_entity, requested_entity = (
        (setup.select, setup.sensor) if existing_select else (setup.sensor, setup.select)
    )
    if other_target == "different_entry":
        setup.registry[existing_entity].config_entry_id = "site-b"
    elif other_target == "different_unique":
        setup.registry[existing_entity].unique_id = "enphase_site-b_export_limit"
    elif other_target == "unregistered":
        setup.registry.pop(existing_entity)
    current_entry = SimpleNamespace(entry_id="planner-a", data={}, options=OPTIONS, subentries={})
    other_entry = SimpleNamespace(entry_id="planner-b", options=OPTIONS, subentries={}, data={
        CONF_ENPHASE_EXPORT_LIMIT_ENTITY: existing_entity,
    })
    if other_target == "unconfigured":
        other_entry.data.clear()
    setup.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [current_entry, other_entry])
    errors = _validate_subentry_config(setup.hass, current_entry, {
        CONF_ENPHASE_EXPORT_LIMIT_ENTITY: requested_entity,
    }, subentry_type=SUBENTRY_ENPHASE)
    assert errors == ({CONF_ENPHASE_EXPORT_LIMIT_ENTITY: "household_actuator_in_use"} if other_target == "same" else {})
    # Editing a planner's own alias must remain possible.
    current_entry.data = other_entry.data
    setup.hass.config_entries.async_entries = lambda domain: [current_entry]
    assert _validate_subentry_config(setup.hass, current_entry, {
        CONF_ENPHASE_EXPORT_LIMIT_ENTITY: requested_entity,
    }, subentry_type=SUBENTRY_ENPHASE) == {}


@pytest.mark.parametrize("baseline", [None, 0, 3000])
def test_exact_baseline_restoration(setup, baseline):
    async def run():
        setup.states[ENTITY] = limit_state(baseline, slew=77)
        a = action(setup)
        if baseline == 0:
            a.kind = ActionKind.DISABLE_EXPORT_LIMIT
            a.execute_not_before = NOW
            a.desired_state["watts"] = None
        result = await setup.control.execute(a, NOW)
        assert result == (OutcomeResult.PENDING, "export_limit_pending", True)
        assert setup.store.data["ownership"][EXPORT_ASSET]["baseline"] == {"watts": baseline, "slew_rate": 77}
        assert setup.calls[0][2]["entity_id"] == ENTITY
        assert "slew_rate" not in setup.calls[0][2]
        assert len(setup.calls) == 1
        assert (await setup.control.execute(a, NOW))[2] is False
        setup.states[ENTITY] = limit_state(a.desired_state["watts"], slew=77, readback=NOW.timestamp())
        assert await setup.control.reconcile(NOW + timedelta(seconds=1)) is None
        setup.mode["value"] = "confirm"
        assert (await setup.control.restore(advance_clock(setup, 301)))[2] is True
        assert setup.calls[-1][2]["slew_rate"] == 77
        # Readback must confirm the original slew as well as its enabled/watts state.
        assert not setup.control.state
        assert setup.store.data["execution_audit"][-1]["result"] == "restored"

    asyncio.run(run())


def test_manual_change_reconciles_automatically_with_new_baseline(setup):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY] = limit_state(5000)
        assert await setup.control.reconcile(NOW) is None
        assert "baseline" not in setup.control.state
        assert (await setup.control.restore(NOW))[2] is False
        assert len(setup.calls) == 1
        assert "pause" not in setup.control.state
        assert setup.control.state["expected"]["watts"] == 5000
        await setup.control.execute(action(setup), advance_clock(setup))
        assert setup.control.state["baseline"]["watts"] == 5000
        assert len(setup.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["pending", "timeout", "cancel", "reject"])
def test_uncertain_and_rejected_requests_never_repeat(setup, mode):
    async def run():
        setup.mode["value"] = mode
        a = action(setup)
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await setup.control.execute(a, NOW)
        else:
            await setup.control.execute(a, NOW)
        clone = runtime.ExportLimitControl(setup.hass, setup.store, DATA)
        await clone.execute(a, NOW + timedelta(seconds=2))
        assert len(setup.calls) == 1
        await clone.resume()
        if mode != "reject":
            assert clone.state["pending"]
            setup.clock["now"] = NOW + timedelta(minutes=10)
            assert await clone.reconcile(setup.clock["now"]) == "export_limit_unconfirmed"
            assert await clone.reconcile(setup.clock["now"]) == "export_limit_unconfirmed"
            # A fresh late readback resolves the command, retaining the explicit pause.
            setup.clock["now"] = NOW + timedelta(minutes=11)
            setup.states[ENTITY] = limit_state(0, readback=setup.clock["now"].timestamp())
            assert await clone.reconcile(setup.clock["now"]) == "export_limit_unconfirmed"
            await clone.resume()
            assert "pause" not in clone.state

    asyncio.run(run())


def test_persistence_failure_and_target_replacement(setup):
    async def run():
        a = action(setup)
        setup.store.fail = True
        with pytest.raises(OSError):
            await setup.control.execute(a, NOW)
        assert not setup.calls
        assert not setup.control.state
        setup.store.fail = False
        await setup.control.execute(a, NOW)
        setup.registry[ENTITY].device_id = "replacement"
        assert await setup.control.reconcile(NOW) == "export_limit_target_changed"
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_noop_restoration_and_unavailable_target(setup):
    async def run():
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.SKIPPED
        a = action(setup)
        setup.states[ENTITY] = limit_state(0)
        assert (await setup.control.execute(a, NOW))[0] == OutcomeResult.SKIPPED
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        await setup.control.execute(a, NOW)
        setup.states.pop(ENTITY)
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.PENDING
        assert setup.control.state["restoring"]
        await setup.control.resume()
        assert setup.control.state["restoring"]

    asyncio.run(run())


def test_pending_confirmation_not_counted_twice(setup):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        budget = action_budget(setup.store.data["execution_audit"], OPTIONS, NOW, EXPORT_ASSET)
        assert budget["used"] == 1 and budget["limit"] == 0

    asyncio.run(run())


def test_readback_timeout_without_fresh_feedback_and_restore_retry(setup):
    async def run():
        await setup.control.execute(action(setup), NOW)
        setup.clock["now"] = NOW + timedelta(minutes=11)
        assert await setup.control.reconcile(setup.clock["now"]) == "export_limit_unconfirmed"
        setup.states[ENTITY] = limit_state(0, readback=setup.clock["now"].timestamp())
        await setup.control.reconcile(setup.clock["now"])
        await setup.control.resume()
        setup.mode["value"] = "reject"
        assert (await setup.control.restore(advance_clock(setup)))[0] == OutcomeResult.FAILED
        assert setup.control.state["restoring"]
        await setup.control.resume()
        setup.mode["value"] = "confirm"
        await setup.control.restore(advance_clock(setup))
        assert not setup.control.state

    asyncio.run(run())


def test_restore_already_baseline_and_unsupported_owned_state(setup):
    async def run():
        observed, _ = runtime.feedback(setup.hass, ENTITY, NOW)
        await setup.control.save({"identity": observed["identity"], "gateway_identity": GATEWAY,
                                  "baseline": {"watts": None, "slew_rate": 100}})
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.RESTORED
        await setup.control.save({"identity": observed["identity"], "expected": {"watts": None, "slew_rate": 100}})
        setup.states[ENTITY].state = "unsupported"
        assert await setup.control.reconcile(NOW) == "export_limit_unsupported"
        assert setup.control.state["pause"] == "export_limit_unsupported"

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "disabled", "unarmed", "stale", "revised", "missing_hass", "cooldown"])
def test_executor_independent_dispatch_and_gates(setup, failure):
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False}
        ctx = context(runtime.evidence(setup.hass, DATA, opts, NOW))
        plan = DryRunPlanner(opts).create_plan(ctx)
        plan.actions = plan.actions[:1]
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=opts)
        setup.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(DATA, opts)
        if failure == "disabled":
            executor.options[CONF_PLANNER_ENABLED] = False
        elif failure == "unarmed":
            setup.store.data["production"]["armed"] = False
        elif failure == "stale":
            setup.states["sensor.export_price"].last_updated = NOW - timedelta(hours=1)
        elif failure == "revised":
            setup.states["sensor.export_price"] = price_state((0, 0, 1))
        elif failure == "missing_hass":
            executor.hass = None
        elif failure == "cooldown":
            setup.store.data["command_rate_limits"] = {EXPORT_ASSET: NOW.isoformat()}
        await executor.async_evaluate(plan, ctx)
        assert bool(setup.calls) is (failure is None)
        assert setup.store.data["execution_audit"][-1]["asset"] == EXPORT_ASSET
        assert setup.store.data["execution_audit"][-1]["service_target"] == ENTITY
        if failure is None:
            assert setup.store.data["command_rate_limits"][EXPORT_ASSET] == NOW
        else:
            assert setup.store.data["execution_audit"][-1]["result"] == "rejected"

    asyncio.run(run())


@pytest.mark.parametrize("mapping_kind", ["sensor", "select"])
def test_export_only_preflight_discovery_and_dry_run_evidence(setup, request, mapping_kind):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.binary_sensor import _armed_attrs
    from custom_components.ha_energy_planner.discovery import CapabilityDiscovery
    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    if mapping_kind == "select":
        setup = request.getfixturevalue("select_setup")
    mapping = setup.mapping if mapping_kind == "select" else DATA

    async def run():
        plan = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(setup.hass, mapping, OPTIONS, NOW)))
        coordinator = _coordinator_for_runtime_services(entry_data=mapping, options=OPTIONS, hass=setup.hass)
        coordinator.data = plan
        coordinator.last_refresh_metadata = {"succeeded": True, "completed_at": NOW}
        report = CapabilityDiscovery(setup.hass, mapping, OPTIONS).inspect()
        assert report.for_asset(EXPORT_ASSET).supported
        for i in range(3):
            await coordinator._async_update_production_evidence(plan, ["input_health_unsafe"])
            assert coordinator.store.data["production"]["dry_run_ready_cycles"] == i + 1
        coordinator.store.data["production"]["dry_run_evidence_fingerprint"] = (
            production_evidence_fingerprint(mapping, OPTIONS))
        report = build_preflight_report(setup.hass, coordinator)
        assert report["safe_to_activate_now"], report
        assert report["current_plan"]["export_limit_safe"]
        assert not report["current_plan"]["legacy_safe"]
        assert report["control_areas"]["confidence_eligible"] == [EXPORT_ASSET]
        coordinator._export_reconcile_pending = True
        report = build_preflight_report(setup.hass, coordinator)
        assert not report["safe_to_activate_now"]
        assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == "export_reconciliation_pending"
        armed = _armed_attrs(coordinator)
        assert EXPORT_ASSET not in armed["ready_control_areas"]
        assert EXPORT_ASSET not in armed["confidence_eligible_control_areas"]
        assert not armed["current_plan_safe"]

        async def settle(trigger):
            assert trigger == "automatic_control_preflight"
            coordinator._export_reconcile_pending = False

        coordinator._async_refresh_with_export_preflight = AsyncMock(side_effect=settle)
        assert (await coordinator._async_operator_control_preflight())["safe_to_activate_now"]
        coordinator._async_refresh_with_export_preflight.assert_awaited_once()
        setup.hass.services.has_service = lambda *args: False
        assert not CapabilityDiscovery(setup.hass, mapping, OPTIONS).inspect().for_asset(EXPORT_ASSET).supported

    asyncio.run(run())


@pytest.mark.parametrize("change", ["tariff", "generation", "pending", "superseded_refresh"])
def test_operator_preflight_revalidates_published_export_readiness(setup, change):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.binary_sensor import _armed_attrs
    from custom_components.ha_energy_planner.preflight import build_preflight_report

    async def run():
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.data = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
        for _ in range(3):
            await owner._async_update_production_evidence(owner.data, ["input_health_unsafe"])
        assert build_preflight_report(setup.hass, owner)["safe_to_activate_now"]
        if change == "pending":
            setup.states[ENTITY].attributes["pending"] = True
        else:
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].attributes.update(
                forecast_interval_minutes=15, interval_minutes=30,
            )
            if change in {"generation", "superseded_refresh"}:
                owner._mark_replan_requested()
            if change == "superseded_refresh":
                owner.last_refresh_metadata.update(succeeded=False, generation=owner._refresh_generation)
        report = build_preflight_report(setup.hass, owner)
        armed = _armed_attrs(owner)
        assert not report["safe_to_activate_now"]
        assert EXPORT_ASSET not in report["control_areas"]["ready"]
        assert EXPORT_ASSET not in armed["ready_control_areas"]
        assert not armed["current_plan_safe"]

        async def refresh(_trigger):
            live = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
            owner.data = DryRunPlanner(OPTIONS).create_plan(context(live))
            owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW,
                                           "generation": owner._refresh_generation}

        owner._async_refresh_with_export_preflight = AsyncMock(side_effect=refresh)
        assert not (await owner._async_operator_control_preflight())["safe_to_activate_now"]
        owner._async_refresh_with_export_preflight.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("blocked_by", ["generation", "activation", "tariff", "gateway", "control", "missing_identity"])
def test_blocked_export_evidence_keeps_other_control_ready(setup, blocked_by):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.binary_sensor import _armed_attrs
    from custom_components.ha_energy_planner.discovery import CapabilityDiscovery
    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    profile = "select.enphase_profile"
    setup.states[profile] = SimpleNamespace(state="self_consumption", attributes={})
    mapping = {**DATA, CONF_ENPHASE_PROFILE: profile, CONF_ENPHASE_AI_PROFILE: "ai_optimization"}
    options = {**OPTIONS, CONF_DRY_RUN: False, CONF_ENPHASE_CONTROL_ENABLED: True}
    owner = _coordinator_for_runtime_services(entry_data=mapping, options=options, hass=setup.hass)
    owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, mapping, options, NOW)))
    owner.data.health = InputHealth.HEALTHY
    owner.data.status = "current"
    owner.data.confidence = 1
    owner.data.confidence_breakdown = {"enphase": 1, "solar": 1, "tariff": 1}
    owner.data.estimated_cost_horizon_hours = 24
    owner.data.input_issues = []
    owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
    owner.store.data["production"] = {
        "armed": True, "dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(mapping, options),
    }
    assert build_preflight_report(setup.hass, owner)["control_areas"]["confidence_eligible"] == [
        "enphase", EXPORT_ASSET,
    ]

    if blocked_by == "activation":
        owner.store.data["production"]["export_operator_arm_revalidate"] = "completed"
        reason = "export_activation_pending"
    elif blocked_by == "tariff":
        setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].attributes["forecasts"][0]["per_kwh"] = 0.2
        reason = "export_tariff_changed"
    elif blocked_by == "gateway":
        setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
        reason = "current_plan_refresh_unconfirmed"
    elif blocked_by == "control":
        setup.registry[ENTITY].device_id = "gateway-b"
        reason = "current_plan_refresh_unconfirmed"
    elif blocked_by == "missing_identity":
        owner.data.control_area_health[EXPORT_ASSET]["feedback"].pop("identity")
        reason = "current_plan_refresh_unconfirmed"
    else:
        owner._mark_replan_requested()
        reason = "current_plan_refresh_unconfirmed"
    report = build_preflight_report(setup.hass, owner, require_export_write_gate=True)
    assert report["active_control_ready"] and report["current_plan"]["legacy_safe"]
    assert not report["current_plan"]["export_limit_safe"]
    assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == reason
    with patch.object(
        CapabilityDiscovery, "inspect", autospec=True, side_effect=CapabilityDiscovery.inspect,
    ) as inspect:
        armed = _armed_attrs(owner)
    inspect.assert_called_once()
    assert armed["armed"] and armed["confidence_eligible_control_areas"] == ["enphase"]


@pytest.mark.parametrize("bypassed", [False, True])
def test_export_only_reports_inactive_until_startup_preflight_opens_write_gate(setup, bypassed):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.binary_sensor import _armed_attrs
    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    options = {**OPTIONS, CONF_DRY_RUN: False, CONF_BYPASS_SAFETY_GATES: bypassed}
    owner = _coordinator_for_runtime_services(entry_data=DATA, options=options, hass=setup.hass)
    owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, DATA, options, NOW)))
    owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
    owner.store.data["production"] = {
        "armed": True, "dry_run_ready_cycles": 3, "export_operator_arm_revalidate": "completed",
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(DATA, options),
    }

    assert build_preflight_report(setup.hass, owner)["safe_to_activate_now"]
    report = build_preflight_report(setup.hass, owner, require_export_write_gate=True)
    assert not report["active_control_ready"]
    assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == "export_activation_pending"
    assert not owner.effective_control
    assert not _armed_attrs(owner)["armed"]

    owner._admit_export_after_restart(owner.data)
    assert owner.effective_control
    assert not setup.calls


def test_preflight_reuses_validated_tariff_and_rechecks_live_evidence(setup, monkeypatch):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import export_limit_policy as policy
    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
    owner.data = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))
    owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
    owner.store.data["production"] = {
        "armed": True, "dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(DATA, OPTIONS),
    }
    owner._preflight_tariff_cache = policy.ExportTariffCache()
    parse = Mock(wraps=policy._parse_blocks)
    monkeypatch.setattr(policy, "_parse_blocks", parse)

    for _ in range(10):
        assert build_preflight_report(setup.hass, owner)["safe_to_activate_now"]
    assert parse.call_count == 1

    setup.clock["now"] = NOW + timedelta(minutes=1)
    report = build_preflight_report(setup.hass, owner, options_override={"price_freshness_minutes": 0.1})
    assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == "export_tariff_stale"
    assert parse.call_count == 1
    setup.clock["now"] = NOW

    tariff = setup.states[DATA[CONF_AMBER_EXPORT_PRICE]]
    tariff.attributes["forecasts"][0]["per_kwh"] = 0.2
    report = build_preflight_report(setup.hass, owner)
    assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == "export_tariff_changed"
    assert parse.call_count == 2

    tariff.state = "unavailable"
    report = build_preflight_report(setup.hass, owner)
    assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == "export_tariff_unavailable"
    assert parse.call_count == 2

    tariff.state = "0.2"
    build_preflight_report(setup.hass, owner)
    assert parse.call_count == 3


def test_preflight_tariff_cache_ignores_unrelated_source_metadata(monkeypatch):
    from custom_components.ha_energy_planner import export_limit_policy as policy

    tariff = price_state()
    cache = policy.ExportTariffCache()
    parse = Mock(wraps=policy._parse_blocks)
    monkeypatch.setattr(policy, "_parse_blocks", parse)
    expected = cache.blocks(tariff, NOW, OPTIONS)
    assert expected[1] is None

    cyclic = []
    cyclic.append(cyclic)
    for metadata in ({"counter": 1}, {"counter": 2}, object(), cyclic):
        tariff.attributes["provider_diagnostics"] = metadata
        assert cache.blocks(tariff, NOW, OPTIONS) == expected
    assert parse.call_count == 1


def test_preflight_validates_complete_oversized_tariff_without_retaining_it(monkeypatch):
    from custom_components.ha_energy_planner import export_limit_policy as policy
    from custom_components.ha_energy_planner.forecasts import MAX_FORECAST_SOURCE_ROWS

    cache = policy.ExportTariffCache()
    normal = price_state()
    expected = cache.blocks(normal, NOW, OPTIONS)
    assert expected[1] is None
    large = price_state(prices=(-0.1, 0.0, 0.2) + (0.2,) * MAX_FORECAST_SOURCE_ROWS)
    parse = Mock(wraps=policy._parse_blocks)
    monkeypatch.setattr(policy, "_parse_blocks", parse)

    for _ in range(2):
        assert cache.blocks(large, NOW, OPTIONS) == expected
    assert parse.call_count == 2
    assert cache._snapshot is None
    assert cache._source is None

    large.attributes["forecasts"][-1]["end_time"] = "invalid"
    assert cache.blocks(large, NOW, OPTIONS) == ([], "export_tariff_invalid")
    assert cache.blocks(normal, NOW, OPTIONS) == expected
    assert cache.blocks(normal, NOW, OPTIONS) == expected
    assert parse.call_count == 4


def test_preflight_tariff_cache_distinguishes_types_and_handles_uncacheable_attributes(monkeypatch):
    from custom_components.ha_energy_planner import export_limit_policy as policy

    tariff = price_state()
    tariff.attributes["confidence"] = 1
    row = tariff.attributes["forecasts"][0]
    row["start_time"] = datetime.fromisoformat(row["start_time"])
    cache = policy.ExportTariffCache()
    baseline = cache.blocks(tariff, NOW, OPTIONS)
    assert baseline[1] is None
    assert baseline == policy.tariff_blocks(tariff, NOW, OPTIONS)

    tariff.attributes["confidence"] = True
    assert cache.blocks(tariff, NOW, OPTIONS) == ([], "export_tariff_confidence_low")
    tariff.attributes["confidence"] = 1
    assert cache.blocks(tariff, NOW, OPTIONS) == baseline

    forecasts = tariff.attributes["forecasts"]
    tariff.attributes["forecasts"] = tuple(forecasts)
    assert cache.blocks(tariff, NOW, OPTIONS) == ([], "export_tariff_block_missing")
    tariff.attributes["forecasts"] = forecasts

    parse = Mock(wraps=policy._parse_blocks)
    monkeypatch.setattr(policy, "_parse_blocks", parse)
    cyclic = []
    cyclic.append(cyclic)
    deeply_nested = [None]
    for _ in range(8):
        deeply_nested = [deeply_nested]
    repeated = datetime(2026, 4, 5, 2, 30, tzinfo=ZoneInfo("Australia/Melbourne"))
    fixed_offset = datetime.fromisoformat(repeated.isoformat())
    assert repeated != fixed_offset
    for unsupported_metadata in (object(), cyclic, deeply_nested, {repeated: 1, fixed_offset: 2}):
        row["value"] = unsupported_metadata
        assert cache.blocks(tariff, NOW, OPTIONS) == baseline
        assert cache.blocks(tariff, NOW, OPTIONS) == baseline
    assert parse.call_count == 8


def test_preflight_tariff_cache_rechecks_reordered_aliases_and_repeated_local_times(monkeypatch):
    from custom_components.ha_energy_planner import export_limit_policy as policy

    repeated = datetime(2026, 4, 5, 2, 30, tzinfo=ZoneInfo("Australia/Melbourne"))
    start = repeated.astimezone(UTC)
    now = start + timedelta(minutes=5)
    tariff = price_state(prices=(-0.1,), start=start, explicit=False)
    tariff.last_updated = now
    tariff.attributes["interval_minutes"] = 30
    row = tariff.attributes["forecasts"][0]
    row["start_time"] = repeated
    row.update({"ExportPrice": -0.1, "Export Price": 0.1})
    cache = policy.ExportTariffCache()
    parse = Mock(wraps=policy._parse_blocks)
    monkeypatch.setattr(policy, "_parse_blocks", parse)

    assert cache.blocks(tariff, now, OPTIONS)[0][0]["watts"] == 0
    assert cache.blocks(tariff, now, OPTIONS)[0][0]["watts"] == 0
    assert parse.call_count == 1

    row["ExportPrice"] = row.pop("ExportPrice")
    assert cache.blocks(tariff, now, OPTIONS)[0][0]["watts"] is None
    assert parse.call_count == 2

    row["start_time"] = repeated.replace(fold=1)
    assert row["start_time"] == repeated
    assert cache.blocks(tariff, now, OPTIONS) == ([], "export_tariff_block_missing")
    assert parse.call_count == 3

    tariff.attributes["issued_at"] = datetime(2026, 4, 5, 2, 30)
    assert cache.blocks(tariff, now, OPTIONS) == ([], "export_tariff_stale")
    assert parse.call_count == 3


@pytest.mark.parametrize("during_save", [False, True])
def test_coordinator_expires_provider_tariff_on_same_state(setup, monkeypatch, during_save):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import storage as storage_module

    async def run():
        state = setup.states[DATA[CONF_AMBER_EXPORT_PRICE]]
        state.attributes["issuedAt"] = (NOW - timedelta(seconds=59)).isoformat()
        owner = _coordinator_for_runtime_services(
            entry_data=DATA, options={**OPTIONS, CONF_PRICE_FRESHNESS_MINUTES: 1}, hass=setup.hass,
        )
        setup.hass.async_add_executor_job = asyncio.to_thread
        setup.hass.async_create_task = asyncio.create_task
        owner._admit_export_reconciliation = Mock(return_value=None)
        owner._schedule_export_boundary = Mock()
        owner._schedule_plan_execution = Mock()
        owner._schedule_debounced_refresh = Mock()
        owner.history_training = SimpleNamespace(request=Mock(), last_result=None)
        expire_on_save = False
        saved = {}

        async def save(payload):
            saved.clear()
            saved.update(deepcopy(payload))
            if expire_on_save and "_pending_plan_publication" in payload:
                await asyncio.sleep(0)
                setup.clock["now"] += timedelta(seconds=2)

        monkeypatch.setattr(storage_module, "Store", lambda *_args, **_kwargs: SimpleNamespace(async_save=save))
        owner.store = storage_module.PlannerStore(setup.hass, owner.entry.entry_id)
        owner.data = previous = await owner._async_update_data()
        assert previous.control_area_health[EXPORT_ASSET]["ready"]
        assert owner._last_decision_context.export_limit["blocks"]
        await owner.async_wait_for_plan_execution()
        assert saved["active_plan"]["plan_id"] == previous.plan_id

        if during_save:
            owner._force_next_refresh = True
            expire_on_save = True
            assert await owner._async_update_data() is previous
            expire_on_save = False
            assert saved["active_plan"]["plan_id"] == previous.plan_id
            assert owner.store.data["active_plan"]["plan_id"] == previous.plan_id
        else:
            setup.clock["now"] += timedelta(seconds=1)
            assert await owner._async_update_data() is previous
            setup.clock["now"] += timedelta(seconds=1)

        assert owner.hass.states.get(DATA[CONF_AMBER_EXPORT_PRICE]) is state
        owner.data = await owner._async_update_data()
        assert owner.data is not previous
        assert owner.data.control_area_health[EXPORT_ASSET]["reason"] == "export_tariff_stale"
        assert f"{CONF_AMBER_EXPORT_PRICE}_stale" not in owner.data.input_issues
        await owner.async_wait_for_plan_execution()

    asyncio.run(run())


@pytest.mark.parametrize("interruption", [
    "startup_cleanup", "startup_recovery", "selector_removed", "provisional_save", "final_save", "final_save_restart",
    "verification_lost", "tariff_freshness_expired", "tariff_rows_changed",
    "completed_save_verification_lost", "completed_save_gateway_replaced", "completed_save_new_readback",
    "completed_save_preflight_error", "cancel", "save_error", "disarm",
    "disarm_completed_save",
    "disarm_during_rollback", "disarm_startup_cleanup", "disarm_notification", "save_failed_after_disarm", "newer_arm",
])
def test_operator_arm_revalidates_export_after_awaits(setup, interruption, monkeypatch):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as coordinator_module
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        active = interruption in {"final_save_restart", "startup_recovery"}
        options = {**OPTIONS, CONF_DRY_RUN: not active}
        if interruption == "tariff_freshness_expired":
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].attributes["issued_at"] = (
                NOW - timedelta(minutes=30) + timedelta(seconds=1)
            ).isoformat()
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=options, hass=setup.hass)
        owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, DATA, options, NOW)))
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
        owner.store.data["production"] = {
            "armed": False, "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(DATA, options),
        }
        owner._async_refresh_with_export_preflight = AsyncMock()
        owner._async_compensate_ev_auto_start = AsyncMock()
        owner.async_handle_options_update = AsyncMock()
        if interruption == "startup_recovery":
            owner._startup_auto_recovery_authorized = True
            owner.store.data["production"]["startup_auto_recovery"] = {"status": "waiting_for_safe"}
            owner.async_start_startup_auto_recovery = Mock()
        setup.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, options: setattr(entry, "options", options),
        )
        entered, release = asyncio.Event(), asyncio.Event()
        save = owner._async_save_production
        cancel = owner.async_cancel_startup_auto_recovery

        if interruption == "completed_save_preflight_error":
            build_report = coordinator_module.build_preflight_report

            def fail_live_report(*args, **kwargs):
                if owner.store.data["production"].get("export_operator_arm_revalidate") == "completed":
                    raise RuntimeError("live preflight failed")
                return build_report(*args, **kwargs)

            monkeypatch.setattr(coordinator_module, "build_preflight_report", fail_live_report)

        async def wait_for_disarm():
            entered.set()
            await release.wait()

        async def save_then_interrupt(production):
            await save(production)
            if not production.get("armed"):
                if interruption == "disarm_during_rollback" and not entered.is_set():
                    entered.set()
                    await release.wait()
                return
            assert owner._export_write_blocked()
            if interruption == "provisional_save" and production.get("startup_activation_pending"):
                setup.states[ENTITY].attributes["pending"] = True
            if not production.get("startup_activation_pending"):
                if interruption == "final_save_restart":
                    restarted = _coordinator_for_runtime_services(
                        entry_data=DATA, options=options, hass=setup.hass, store_data=deepcopy(owner.store.data),
                    )
                    assert restarted._export_write_blocked()
                    assert await restarted.async_reconcile_production_evidence_contract()
                    assert not restarted.active_control
                    assert restarted._startup_auto_recovery_authorized
                    assert restarted._export_retry_startup_blocked
                    owner._mark_replan_requested()
                if interruption in {"final_save", "disarm_during_rollback"}:
                    owner._mark_replan_requested()
                elif interruption == "verification_lost":
                    assert production["export_operator_arm_revalidate"] is True
                    setup.states[ENTITY].attributes["gateway_identity_verified"] = False
                elif interruption == "tariff_freshness_expired":
                    setup.clock["now"] = NOW + timedelta(seconds=2)
                elif interruption == "tariff_rows_changed":
                    setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].attributes["forecasts"][0]["per_kwh"] = 0.2
                elif (interruption == "completed_save_verification_lost"
                      and production.get("export_operator_arm_revalidate") == "completed"):
                    setup.states[ENTITY].attributes["gateway_identity_verified"] = False
                elif (interruption == "completed_save_gateway_replaced"
                      and production.get("export_operator_arm_revalidate") == "completed"):
                    setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
                elif (interruption == "completed_save_new_readback"
                      and production.get("export_operator_arm_revalidate") == "completed"):
                    setup.clock["now"] = NOW + timedelta(seconds=1)
                    setup.states[ENTITY].attributes["last_successful_readback"] = setup.clock["now"].timestamp()
                elif interruption == "save_error":
                    raise OSError("production save failed")
                elif (
                    not entered.is_set()
                    and (
                        interruption in {"cancel", "disarm", "save_failed_after_disarm", "newer_arm"}
                        or (interruption == "disarm_completed_save"
                            and production.get("export_operator_arm_revalidate") == "completed")
                    )
                ):
                    entered.set()
                    await release.wait()
                    if interruption == "save_failed_after_disarm":
                        raise OSError("production save failed after disarm")

        async def cancel_startup(*args, **kwargs):
            if interruption == "startup_cleanup":
                await asyncio.sleep(0)
                owner._mark_replan_requested()
            elif interruption == "startup_recovery":
                await cancel(*args, **kwargs)
                assert not owner._startup_auto_recovery_authorized
                setup.states[ENTITY].attributes["gateway_identity_verified"] = False
            elif interruption == "selector_removed":
                owner.entry.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED] = False
            elif interruption == "disarm_startup_cleanup":
                await wait_for_disarm()

        owner._async_save_production = save_then_interrupt
        owner.async_cancel_startup_auto_recovery = cancel_startup
        if interruption == "disarm_notification":
            owner.executor.async_dismiss_startup_recovery_notification = wait_for_disarm
        if interruption in {
            "startup_cleanup", "startup_recovery", "selector_removed", "final_save_restart",
            "disarm_startup_cleanup", "disarm_notification",
        }:
            operation = owner.async_operator_arm_production_control()
        else:
            operation = owner.async_set_active_control(True)
        if interruption in {
            "cancel", "disarm", "disarm_completed_save", "disarm_during_rollback", "save_failed_after_disarm",
            "newer_arm", "disarm_startup_cleanup", "disarm_notification",
        }:
            task = asyncio.create_task(operation)
            await asyncio.wait_for(entered.wait(), 1)
            if interruption == "disarm_completed_save":
                assert owner.store.data["production"]["export_operator_arm_revalidate"] == "completed"
            if interruption == "newer_arm":
                await owner.async_set_active_control(True)
                release.set()
                await task
                assert owner.active_control
                assert not owner._export_write_blocked()
                owner._async_compensate_ev_auto_start.assert_awaited_once()
                return
            if interruption != "cancel":
                await owner.async_disarm_production_control("later_stop")
                release.set()
                if interruption in {"disarm", "disarm_completed_save", "disarm_startup_cleanup", "disarm_notification"}:
                    await task
                else:
                    error = OSError if interruption == "save_failed_after_disarm" else HomeAssistantError
                    with pytest.raises(error):
                        await task
                assert owner.store.data["production"]["disarmed_reason"] == "later_stop"
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        elif interruption in {"completed_save_verification_lost", "completed_save_gateway_replaced"}:
            await operation
            assert owner.store.data["production"]["export_operator_arm_revalidate"] == "completed"
            assert owner._export_write_blocked()
            assert not owner.effective_control
            restarted = _coordinator_for_runtime_services(
                entry_data=DATA, options=owner.options, hass=setup.hass, store_data=deepcopy(owner.store.data),
            )
            await restarted.async_reconcile_production_evidence_contract()
            assert restarted._export_write_blocked()
            assert not restarted.effective_control
            if interruption == "completed_save_verification_lost":
                setup.states[ENTITY].attributes["gateway_identity_verified"] = True
            else:
                owner._admit_export_after_restart(owner.data)
                assert owner._export_write_blocked()
                live = runtime.evidence(setup.hass, DATA, options, NOW)
                owner.data = DryRunPlanner(options).create_plan(context(live))
            owner._admit_export_after_restart(owner.data)
            assert not owner._export_write_blocked()
            assert not setup.calls
            return
        elif interruption == "completed_save_new_readback":
            await operation
            assert owner.effective_control
            assert not owner._export_write_blocked()
            assert not setup.calls
            return
        elif interruption == "completed_save_preflight_error":
            with pytest.raises(RuntimeError, match="live preflight failed"):
                await operation
            assert owner.store.data["production"]["export_operator_arm_revalidate"] == "completed"
            assert owner.dry_run and owner._export_write_blocked()
            assert not setup.calls
            return
        else:
            error = OSError if interruption == "save_error" else HomeAssistantError
            with pytest.raises(error):
                await operation
        assert not owner.store.data["production"]["armed"]
        assert not owner.store.data["production"].get("startup_activation_pending")
        assert not owner.store.data["production"].get("export_operator_arm_revalidate")
        assert owner.dry_run is (not active)
        if interruption == "startup_recovery":
            assert owner._startup_auto_recovery_authorized
            assert owner.store.data["production"]["startup_auto_recovery"] == {"status": "waiting_for_safe"}
            owner.async_start_startup_auto_recovery.assert_called_once_with()
        owner._async_compensate_ev_auto_start.assert_not_called()
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["cancel", "disarm", "cleanup_failure"])
def test_operator_arm_drains_startup_recovery_without_consuming_cancellation(setup, interruption):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        options = {**OPTIONS, CONF_DRY_RUN: False}
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=options, hass=setup.hass)
        owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, DATA, options, NOW)))
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
        owner.store.data["production"] = {
            "armed": False, "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(DATA, options),
            "startup_auto_recovery": {"status": "waiting_for_safe"},
        }
        owner._startup_auto_recovery_authorized = True
        owner.async_start_startup_auto_recovery = Mock()
        owner._async_refresh_with_export_preflight = AsyncMock()
        owner._async_compensate_ev_auto_start = AsyncMock()
        running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def startup():
            try:
                running.set()
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                if interruption == "cleanup_failure":
                    raise RuntimeError("startup cleanup failed")

        recovery = owner._startup_auto_recovery_task = asyncio.create_task(startup())
        await asyncio.wait_for(running.wait(), 1)
        arm = asyncio.create_task(owner.async_operator_arm_production_control())
        await asyncio.wait_for(cleaning.wait(), 1)
        disarm = None
        if interruption != "cleanup_failure":
            arm.cancel()
            await asyncio.sleep(0)
            try:
                assert not recovery.done()
                assert not owner.store.data["production"]["armed"]
                if interruption == "disarm":
                    disarm = asyncio.create_task(owner.async_operator_disarm_production_control("later_disarm"))
                    await asyncio.sleep(0)
                    assert not recovery.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await arm
        else:
            release.set()
            with pytest.raises(RuntimeError, match="startup cleanup failed"):
                await arm

        assert recovery.done()
        production = owner.store.data["production"]
        assert not production["armed"]
        if disarm is None:
            assert production["startup_auto_recovery"] == {"status": "waiting_for_safe"}
            assert owner._startup_auto_recovery_authorized
            owner.async_start_startup_auto_recovery.assert_called_once_with()
        else:
            await disarm
            assert owner.store.data["production"]["disarmed_reason"] == "later_disarm"
            assert not owner._startup_auto_recovery_authorized
            owner.async_start_startup_auto_recovery.assert_not_called()
        owner._async_compensate_ev_auto_start.assert_not_awaited()
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("previously_armed", [False, True])
@pytest.mark.parametrize("successor", ["rejected", "cancelled", "save_failed", "disarmed"])
def test_superseded_export_arm_restores_only_its_provisional_authorization(setup, previously_armed, successor):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        options = {**OPTIONS, CONF_DRY_RUN: True}
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=options, hass=setup.hass)
        owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, DATA, options, NOW)))
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
        production = {
            "armed": previously_armed, "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(DATA, options),
        }
        if previously_armed:
            production["armed_reason"] = "prior_authorization"
        owner.store.data["production"] = production
        owner.async_cancel_startup_auto_recovery = AsyncMock()
        owner._async_compensate_ev_auto_start = AsyncMock()
        first_saved, release_first, next_refresh = asyncio.Event(), asyncio.Event(), asyncio.Event()
        save = owner._async_save_production

        async def save_with_interruption(state):
            await save(state)
            if state.get("armed_reason") == "first" and state.get("startup_activation_pending"):
                first_saved.set()
                await release_first.wait()
            if state.get("armed_reason") == "successor" and state.get("startup_activation_pending"):
                raise OSError("successor save failed")

        async def refresh_for_arm(_trigger):
            if first_saved.is_set() and successor in {"cancelled", "disarmed"}:
                next_refresh.set()
                await asyncio.Future()

        owner._async_save_production = save_with_interruption
        owner._async_refresh_with_export_preflight = refresh_for_arm
        first = asyncio.create_task(owner.async_operator_arm_production_control("first"))
        await asyncio.wait_for(first_saved.wait(), 1)
        assert owner._export_write_blocked()
        owner.store.data["production"]["last_dry_run_ready_at"] = NOW.isoformat()

        if successor == "rejected":
            setup.states[ENTITY].attributes["pending"] = True
            with pytest.raises(HomeAssistantError):
                await owner.async_operator_arm_production_control("successor")
        elif successor == "save_failed":
            with pytest.raises(OSError, match="successor save failed"):
                await owner.async_operator_arm_production_control("successor")
        else:
            second = asyncio.create_task(owner.async_operator_arm_production_control("successor"))
            await asyncio.wait_for(next_refresh.wait(), 1)
            if successor == "disarmed":
                await owner.async_disarm_production_control("later_disarm")
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second

        release_first.set()
        await first
        actual = owner.store.data["production"]
        assert actual["armed"] is (previously_armed and successor != "disarmed")
        if successor == "disarmed":
            assert actual["disarmed_reason"] == "later_disarm"
        else:
            assert actual.get("armed_reason") == production.get("armed_reason")
        assert actual["last_dry_run_ready_at"] == NOW.isoformat()
        assert not actual.get("startup_activation_pending")
        assert not actual.get("export_operator_arm_revalidate")
        assert not owner._export_write_blocked()
        owner._async_compensate_ev_auto_start.assert_not_called()
        assert not setup.calls

    asyncio.run(run())


def test_refresh_retries_a_failed_operator_arm_rollback(setup):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        options = {**OPTIONS, CONF_DRY_RUN: False}
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=options, hass=setup.hass)
        owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, DATA, options, NOW)))
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
        owner.store.data["production"] = {
            "armed": True, "armed_reason": "previous_arm", "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(DATA, options),
        }
        owner._async_refresh_with_export_preflight = AsyncMock()
        owner._schedule_debounced_refresh = Mock()
        owner._async_update_data_locked = AsyncMock(return_value=owner.data)
        owner._admit_export_reconciliation = Mock(return_value=None)
        save = owner._async_save_production
        rollback_attempts = 0

        async def fail_first_rollback(production):
            nonlocal rollback_attempts
            await save(production)
            if production.get("startup_activation_pending"):
                setup.states[ENTITY].attributes["pending"] = True
            elif production.get("armed_reason") == "previous_arm":
                rollback_attempts += 1
                if rollback_attempts == 1:
                    raise OSError("rollback save failed")

        owner._async_save_production = fail_first_rollback
        with pytest.raises(OSError, match="rollback save failed"):
            await owner.async_operator_arm_production_control()
        assert owner._export_write_blocked()
        assert owner._export_operator_arm.rollback_pending
        assert owner._schedule_debounced_refresh.call_args.args[0] == "operator_arm_rollback"
        setup.states[ENTITY].attributes["pending"] = False
        await owner._async_update_data()
        assert rollback_attempts == 2
        assert owner.store.data["production"]["armed_reason"] == "previous_arm"
        assert owner.active_control
        assert not owner._export_write_blocked()
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize(("dry_run", "pending_during_arm"), [(False, False), (True, False), (False, True)])
def test_completed_operator_arm_preserves_authorization_across_restart(setup, dry_run, pending_during_arm):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    async def run():
        profile = "select.enphase_profile"
        setup.states[profile] = SimpleNamespace(state="self_consumption", attributes={})
        mapping = {**DATA, CONF_ENPHASE_PROFILE: profile, CONF_ENPHASE_AI_PROFILE: "ai_optimization"}
        options = {**OPTIONS, CONF_DRY_RUN: dry_run, CONF_ENPHASE_CONTROL_ENABLED: True}
        owner = _coordinator_for_runtime_services(entry_data=mapping, options=options, hass=setup.hass)
        plan = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, mapping, options, NOW)))
        plan.health, plan.status, plan.confidence = InputHealth.HEALTHY, "current", 1
        plan.confidence_breakdown = {"enphase": 1, "solar": 1, "tariff": 1}
        plan.estimated_cost_horizon_hours = 24
        plan.input_issues = []
        owner.data = plan
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW, "generation": 0}
        owner.store.data["production"] = {
            "armed": False, "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(mapping, options),
        }
        owner._async_refresh_with_export_preflight = AsyncMock()
        owner._async_compensate_ev_auto_start = AsyncMock()
        save_production = owner._async_save_production

        async def save_with_pending_export(production):
            await save_production(production)
            if production.get("export_operator_arm_revalidate") == "completed":
                setup.states[ENTITY].attributes["pending"] = True

        if pending_during_arm:
            owner._startup_auto_recovery_authorized = True
            owner.store.data["production"]["startup_auto_recovery"] = {"status": "waiting_for_safe"}
            owner._async_save_production = save_with_pending_export
        await owner.async_operator_arm_production_control("previously_confirmed")
        assert owner._export_write_blocked() is pending_during_arm
        assert owner.store.data["production"]["export_operator_arm_revalidate"] == "completed"
        if pending_during_arm:
            assert owner.store.data["production"]["startup_auto_recovery"]["last_reason"] == "operator_armed"
            assert owner.active_control
            report = build_preflight_report(setup.hass, owner)
            assert report["active_control_ready"]
            assert report["control_areas"]["confidence_eligible"] == ["enphase"]
            setup.states[ENTITY].attributes["pending"] = False
            fresh_plan = deepcopy(plan)
            owner.data = fresh_plan
            owner._pending_plan_execution = _PlanExecution(
                owner._refresh_generation, fresh_plan, object(), owner.planner_options,
            )
            owner.executor.async_evaluate = AsyncMock()
            await owner._async_drain_plan_execution()
            assert not owner._export_write_blocked()
        restarted = _coordinator_for_runtime_services(
            entry_data=mapping, options=options, hass=setup.hass, store_data=deepcopy(owner.store.data),
        )
        assert not restarted._export_activation_pending()
        assert await restarted.async_reconcile_production_evidence_contract() is not dry_run
        assert restarted.store.data["production"]["armed"]
        assert restarted.store.data["production"]["armed_reason"] == "previously_confirmed"
        assert restarted.active_control is not dry_run
        assert restarted._export_write_blocked()
        restarted._admit_export_after_restart(plan)
        assert restarted._export_write_blocked()

        restarted.data = plan
        restarted.last_refresh_metadata = dict(owner.last_refresh_metadata)
        setup.hass.is_running = False
        restarted._admit_export_after_restart(plan)
        assert restarted._export_write_blocked()

        setup.hass.is_running = True
        setup.states[ENTITY].attributes["pending"] = True
        report = build_preflight_report(setup.hass, restarted)
        assert report["active_control_ready"]
        assert report["control_areas"]["confidence_eligible"] == ["enphase"]
        restarted._admit_export_after_restart(plan)
        assert restarted._export_write_blocked()

        setup.states[ENTITY].attributes["pending"] = False
        if not dry_run:
            restarted._schedule_debounced_refresh = Mock()
            restarted._pending_plan_execution = _PlanExecution(
                restarted._refresh_generation, plan, object(), restarted.planner_options,
                tariffs_current=lambda: False,
            )
            await restarted._async_drain_plan_execution()
            assert restarted._export_write_blocked()
            restarted._schedule_debounced_refresh.assert_called_once_with("execution_inputs_changed", force=True)
        restarted._pending_plan_execution = _PlanExecution(
            restarted._refresh_generation, plan, object(), restarted.planner_options,
        )
        restarted.executor.async_evaluate = AsyncMock()
        await restarted._async_drain_plan_execution()
        assert restarted._export_write_blocked() is dry_run
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("healthy", [False, True])
@pytest.mark.parametrize("owned", [False, True])
def test_production_evidence_rejects_gateway_replacement_published_after_plan_creation(setup, healthy, owned):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        coordinator = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        plan = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))
        if healthy:
            plan.health = InputHealth.HEALTHY
            plan.status = "current"
        violations = [] if healthy else ["input_health_unsafe"]
        if owned:
            coordinator.store.data["ownership"] = {EXPORT_ASSET: {"gateway_identity": GATEWAY}}
        await coordinator._async_update_production_evidence(plan, violations)
        assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1

        advance_clock(setup, 1)
        setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
        await coordinator._async_update_production_evidence(plan, violations)
        assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("scenario", [
    "settled", "in_flight", "changed_feedback", "retry_past_grace",
    "retry_past_grace_repeat", "retry_past_grace_tariff_change", "retry_past_grace_external",
])
def test_export_only_startup_validation_and_activation_wait_for_reconciliation(setup, monkeypatch, scenario, request):

    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module
    from custom_components.ha_energy_planner import startup_recovery

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False, "ai_enabled": False}
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=opts, hass=setup.hass)
        owner.store.data["production"] = {"armed": False}
        owner._startup_auto_recovery_authorized = True
        owner._startup_auto_recovery_validation_active = False
        setup.hass.async_create_task = asyncio.create_task
        setup.hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro)
        setup.hass.async_add_executor_job = lambda func, *args: asyncio.to_thread(func, *args)
        owner._schedule_export_boundary = Mock()
        owner._schedule_debounced_refresh = Mock()
        owner._schedule_plan_execution = Mock()
        checks = []
        first_flush = asyncio.Event()
        release_first_flush = asyncio.Event()

        async def flush():
            # Force the reconciliation task to yield before the plan can use it.
            validating, active = owner._startup_auto_recovery_validation_active, owner.active_control
            if validating or active:
                assert not owner._planner_lock.locked()
            checks.append((validating, active))
            if scenario == "in_flight" and len(checks) == 1:
                first_flush.set()
                await release_first_flush.wait()
            elif scenario == "changed_feedback":
                setup.states[ENTITY] = limit_state(0)
            await asyncio.sleep(0)

        owner.store.async_flush = flush

        async def refresh():
            owner.data = await owner._async_update_data()

        owner.async_refresh = refresh
        monkeypatch.setattr(module, "InputManager", lambda *args, **kwargs: SimpleNamespace(
            current_forecast_observations=lambda: {},
            async_build_context=AsyncMock(side_effect=lambda overrides: context(
                runtime.evidence(setup.hass, DATA, opts, setup.clock["now"]), setup.clock["now"],
            )),
            retained_hvac_tariff_slots=lambda ctx: None, thermal_sample=lambda built: {},
            forecast_training_slots=[], forecast_calibration={}, load_forecast_details={},
        ))
        if scenario.startswith("retry_past_grace"):
            fixture = request.getfixturevalue("retry_owner")
            opts[CONF_COMMAND_RATE_LIMIT_SECONDS] = 3600
            owner.entry.options[CONF_COMMAND_RATE_LIMIT_SECONDS] = 3600
            writer = fixture.create(opts)
            setup.states[ENTITY] = limit_state(0)
            await setup.control.save({
                "identity": runtime.target_identity(setup.hass, ENTITY),
                "gateway_identity": GATEWAY,
                "gateway_identity_verified": True,
                "baseline": {"watts": None, "slew_rate": 100.0},
                "expected": {"watts": 0, "slew_rate": 100.0},
            })
            await setup.control.prepare_cooldown(NOW - timedelta(hours=1))

            def positive_tariff(now):
                tariff = price_state((0.1, 0.1), start=now.replace(minute=0))
                tariff.last_updated = now
                setup.states["sensor.export_price"] = tariff

            positive_tariff(NOW)
            await writer.executor.async_execute_export_limit(action(setup), NOW)
            owner.store.data = deepcopy(setup.store.data)
            owner._startup_auto_recovery_authorized = True
            owner._schedule_export_retry()
            wakeup = fixture.timers[-1]
            assert wakeup.delay == 3600
            positive_tariff(advance_clock(setup, 1800))
            ready, reason = await owner._async_run_startup_auto_recovery_validation()
            assert not ready
            await startup_recovery._async_enter_startup_safe_recovery(owner, reason)
            assert not owner.active_control
            for _ in range(2):
                owner._startup_auto_recovery_authorized = False
                assert await owner.async_reconcile_production_evidence_contract()
            # Model the fully-started callback for disarmed recovery.
            owner._export_retry_startup_blocked = False
            now = advance_clock(setup, 1800)
            positive_tariff(now)
            wakeup.wake(now)
            await asyncio.gather(*owner._listener_tasks)
            assert not owner.active_control
            assert len(setup.calls) == 1
            retry = owner.store.data["ownership"][EXPORT_ASSET]["retry"]
            assert retry["attempts"] == 1 and retry["state"] == "ready"
            if scenario == "retry_past_grace_tariff_change":
                tariff = price_state((-0.1, -0.1), start=now.replace(minute=0))
                tariff.last_updated = now
                setup.states["sensor.export_price"] = tariff
            if scenario == "retry_past_grace_external":
                setup.states[ENTITY] = limit_state(3500, readback=now.timestamp())
            assert await owner._async_run_startup_auto_recovery_validation() == (True, "validation_succeeded")
            assert await owner._async_reactivate_after_startup_recovery(1) == (True, "automatic_control_reactivated")
            assert owner.active_control
            setup.store.data = owner.store.data
            if scenario == "retry_past_grace_tariff_change":
                assert "retry" not in setup.control.state
                assert len(setup.calls) == 1
                return
            if scenario in {"retry_past_grace_repeat", "retry_past_grace_external"}:
                owner.executor = writer.executor
                await owner._async_retry_export_limit()
                saved = setup.control.state["retry"]
                assert saved["attempts"] == 1 and saved["state"] == "scheduled"
                assert saved["next_attempt_at"] == (now + timedelta(minutes=5)).isoformat()
                if scenario == "retry_past_grace_external":
                    assert setup.control.state["baseline"]["watts"] == 3500
                owner._schedule_export_retry()
                wakeup = fixture.timers[-1]
                assert wakeup.delay == 3600
                now = advance_clock(setup, 3600)
                positive_tariff(now)
                setup.mode["value"] = "confirm"
                wakeup.wake(now)
                await asyncio.gather(*owner._listener_tasks)
                assert len(setup.calls) == 3 and "retry" not in setup.control.state
                return
            setup.mode["value"] = "confirm"
            owner.executor = writer.executor
            await owner._async_retry_export_limit()
            assert setup.calls[-1][1] == "disable_export_limit" and len(setup.calls) == 2
            return
        if scenario == "in_flight":
            await owner._async_update_data()
            await asyncio.wait_for(first_flush.wait(), 1)
            owner._schedule_plan_execution.reset_mock()
        validation = asyncio.create_task(owner._async_run_startup_auto_recovery_validation())
        await asyncio.sleep(0)
        release_first_flush.set()
        result = await validation
        owner._schedule_plan_execution.assert_not_called()
        if scenario == "changed_feedback":
            assert result[0] is False and not owner.active_control
            assert owner._export_reconcile_pending
            owner._schedule_debounced_refresh.assert_not_called()
            return
        assert result == (True, "validation_succeeded")
        assert await owner._async_reactivate_after_startup_recovery(3) == (True, "automatic_control_reactivated")
        assert checks == ([(False, False)] if scenario == "in_flight" else []) + [(True, False), (False, True)]
        assert owner.data.control_area_health[EXPORT_ASSET]["ready"]
        assert owner.active_control
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("scenario", [
    "pending", "completed", "completed_with_pause", "competing_refresh", "competing_provisional", "weather_update",
    "planner_lock_update", "operator_arm", "arm_then_disarm", "enable_then_disable", "unavailable",
])
def test_operator_control_transition_checks_reconciled_export_plan(setup, monkeypatch, scenario):
    from unittest.mock import Mock

    from homeassistant.exceptions import HomeAssistantError
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module
    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    async def run():
        arm_only = scenario in {"operator_arm", "arm_then_disarm"}
        stop_requested = scenario in {"arm_then_disarm", "enable_then_disable"}
        opts = {**OPTIONS, CONF_PLANNER_ENABLED: arm_only, CONF_DRY_RUN: True, "ai_enabled": False}
        mapping = {**DATA, "weather_entity": "weather.home"} if scenario == "weather_update" else DATA
        owner = _coordinator_for_runtime_services(entry_data=mapping, options=opts, hass=setup.hass)
        owner.store.data["production"] = {
            "armed": False, "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(mapping, opts),
        }
        setup.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, options: setattr(entry, "options", options),
        )
        setup.hass.async_create_task = asyncio.create_task
        setup.hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro)
        setup.hass.async_add_executor_job = lambda func, *args: asyncio.to_thread(func, *args)
        owner._schedule_export_boundary = Mock()
        owner._schedule_debounced_refresh = Mock()
        owner._schedule_plan_execution = Mock()
        release, checked = asyncio.Event(), asyncio.Event()
        stop_saved = asyncio.Event()
        start_competing_refresh = asyncio.Event()
        preflight_reasons = []
        if scenario == "unavailable":
            setup.states[ENTITY].state = "unavailable"
        if scenario == "completed_with_pause":
            owner.store.data["ownership"] = {EXPORT_ASSET: {
                "identity": runtime.target_identity(setup.hass, ENTITY), "pause": "external_export_limit_conflict",
            }}

        save_rate_limits = owner.store.async_save_command_rate_limits
        save_production = owner.store.async_save_production

        async def wait_then_save_rate_limits(value):
            await release.wait()
            await save_rate_limits(value)

        async def record_stop(production):
            await save_production(production)
            if production.get("disarmed_reason") and not production.get("armed"):
                stop_saved.set()

        async def competing_refresh():
            await start_competing_refresh.wait()
            owner.data = await owner._async_update_data()

        async def weather(*args, **kwargs):
            if scenario == "weather_update" and owner._pending_refresh_trigger == "automatic_control_preflight":
                setup.states[ENTITY] = limit_state(1500)
                await asyncio.sleep(0)
            return {}, {}

        competitor = asyncio.create_task(competing_refresh()) if scenario.startswith("competing_") else None

        async def refresh():
            if owner._pending_refresh_trigger == "automatic_control_preflight":
                preflight_reasons.append(owner.data.control_area_health[EXPORT_ASSET]["reason"])
                checked.set()
                if competitor is not None:
                    start_competing_refresh.set()
                    await competitor
                    if scenario == "competing_provisional":
                        release.set()
                if scenario == "planner_lock_update":
                    await owner._planner_lock.acquire()

                    def change_and_unlock():
                        setup.states[ENTITY] = limit_state(1500)
                        owner._planner_lock.release()

                    asyncio.get_running_loop().call_soon(change_and_unlock)
            owner.data = await owner._async_update_data()
            if (scenario.startswith("completed") or scenario in {"competing_refresh", "planner_lock_update"}) and (
                worker := owner._export_reconcile_task
            ) is not None:
                release.set()
                await worker

        def preflight(hass, coordinator, **kwargs):
            preflight_reasons.append(coordinator.data.control_area_health[EXPORT_ASSET]["reason"])
            return build_preflight_report(hass, coordinator, **kwargs)

        owner.store.async_save_command_rate_limits = wait_then_save_rate_limits
        owner.store.async_save_production = record_stop
        owner.async_refresh = owner.async_request_refresh = refresh
        owner._async_weather_forecast = weather
        monkeypatch.setattr(module, "build_preflight_report", preflight)
        monkeypatch.setattr(module, "InputManager", lambda hass, data, options, **kwargs: SimpleNamespace(
            current_forecast_observations=lambda: {},
            async_build_context=AsyncMock(
                side_effect=lambda overrides: context(runtime.evidence(hass, data, options, NOW)),
            ),
            retained_hvac_tariff_slots=lambda ctx: None, thermal_sample=lambda built: {},
            forecast_training_slots=[], forecast_calibration={}, load_forecast_details={},
        ))
        if arm_only:
            owner._mark_forced_refresh("state_change")
            await refresh()
            enable = asyncio.create_task(owner.async_operator_arm_production_control())
        else:
            enable = asyncio.create_task(owner.async_set_active_control(True))
        await asyncio.wait_for(checked.wait(), 1)
        stop = None
        if stop_requested:
            stop = asyncio.create_task(
                owner.async_operator_disarm_production_control() if arm_only else owner.async_set_active_control(False)
            )
            await asyncio.wait_for(stop_saved.wait(), 1)
        if scenario != "competing_provisional":
            release.set()
        try:
            if scenario == "unavailable":
                with pytest.raises(HomeAssistantError, match="Automatic control is not ready"):
                    await enable
            else:
                await enable
            if stop is not None:
                await stop
        finally:
            if (worker := owner._export_reconcile_task) is not None:
                await worker
        assert preflight_reasons[:2] == [
            "external_export_limit_conflict" if scenario == "completed_with_pause" else "export_reconciliation_pending",
            "export_limit_entity_unavailable" if scenario == "unavailable" else None,
        ]
        armed = not stop_requested and scenario != "unavailable"
        assert preflight_reasons[2:] == ([None] * (4 + arm_only) if armed else [])
        assert owner.store.data["production"]["armed"] is armed
        assert owner.active_control is (armed and not arm_only)
        if stop_requested:
            assert owner.dry_run
            assert all(not saved.get("armed") for saved in owner.store.production_saves)
        assert not setup.calls

    asyncio.run(run())


def test_boundary_timer_and_shutdown_rebuild_fresh_plan(setup, monkeypatch):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module
    from custom_components.ha_energy_planner import task_lifecycle

    async def run():
        coordinator = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS)
        coordinator._export_boundary_cancel = None
        coordinator._listener_tasks = set()
        callbacks, cancelled = [], []
        timer_clock = [100.0]
        monkeypatch.setattr(module, "monotonic", lambda: timer_clock[0])
        monkeypatch.setattr(module, "async_call_later", lambda hass, delay, cb: (
            callbacks.append((delay, cb)) or (lambda: cancelled.append(True))))
        evidence = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
        coordinator._schedule_export_boundary(evidence)
        assert callbacks[-1][0] == 20 * 60
        for seconds in range(1, 11):
            setup.clock["now"] = NOW + timedelta(seconds=seconds)
            timer_clock[0] = 100.0 + seconds
            coordinator._schedule_export_boundary(evidence)
        assert len(callbacks) == 1
        assert not cancelled

        # A real wall-clock correction changes the delay for the same UTC deadline.
        setup.clock["now"] += timedelta(seconds=30)
        coordinator._schedule_export_boundary(evidence)
        assert cancelled == [True]
        assert callbacks[-1][0] == 20 * 60 - 40
        callbacks[0][1](NOW)
        assert len(callbacks) == 2
        callbacks[-1][1](NOW)
        assert coordinator._force_next_refresh
        assert callbacks[-1][0] == 0
        callbacks[-1][1](NOW)
        assert coordinator._pending_refresh_trigger == "export_tariff_boundary"
        coordinator._schedule_export_boundary({})
        coordinator._schedule_export_boundary(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
        task_lifecycle._begin_shutdown(coordinator)
        assert coordinator._export_boundary_cancel is None
        callbacks[-1][1](NOW)
        assert coordinator._debounce_cancel is None

    asyncio.run(run())


@pytest.mark.parametrize("seconds_to_deadline", [-1, 5])
def test_cooldown_wakeup_queues_refresh_while_core_is_busy(setup, monkeypatch, tmp_path, seconds_to_deadline):
    import logging

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.debounce import Debouncer
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        owner = _coordinator_for_runtime_services(entry_data=dict(DATA), options=OPTIONS, hass=setup.hass)
        owner._export_boundary_cancel = None
        setup.hass.async_create_task = asyncio.create_task
        refresh = AsyncMock()
        refresh_lock = asyncio.Lock()

        async def serialized_refresh():
            async with refresh_lock:
                await refresh()

        owner.async_refresh = serialized_refresh
        owner._debounced_refresh = Debouncer(
            HomeAssistant(str(tmp_path)), logging.getLogger(__name__),
            cooldown=10, immediate=True, function=serialized_refresh,
        )
        owner.async_request_refresh = DataUpdateCoordinator.async_request_refresh.__get__(owner)
        timers = []
        monkeypatch.setattr(module, "async_call_later", lambda _hass, delay, cb: (
            timers.append((delay, cb)) or (lambda: None)
        ))
        try:
            await owner.async_request_refresh()
            refresh.assert_awaited_once()
            deadline = NOW + timedelta(seconds=seconds_to_deadline)
            owner._schedule_export_boundary({
                "deferred": True, "next_eligible_change_at": deadline.isoformat(),
            })
            assert timers[-1][0] == max(seconds_to_deadline, 0)
            now = advance_clock(setup, max(seconds_to_deadline, 0))
            async with refresh_lock:
                timers[-1][1](now)
                assert timers[-1][0] == 0
                timers[-1][1](now)
                (task,) = owner._listener_tasks
                await asyncio.sleep(0)
                assert not task.done()
                refresh.assert_awaited_once()
            await task
            assert refresh.await_count == 2
            assert not owner._debounced_refresh._execute_at_end_of_timer
        finally:
            owner._debounced_refresh.async_shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("export_first", [False, True])
@pytest.mark.parametrize("interval_under_floor", [False, True])
def test_coincident_export_and_planning_boundaries_share_one_core_request(
    setup, monkeypatch, tmp_path, export_first, interval_under_floor,
):
    import logging

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.debounce import Debouncer
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from test_coordinator import _coordinator_for_runtime_services, _plan

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        owner = _coordinator_for_runtime_services(
            entry_data=dict(DATA), options={**OPTIONS, CONF_DRY_RUN: False}, hass=setup.hass,
        )
        owner._export_boundary_cancel = None
        setup.hass.async_create_task = asyncio.create_task
        owner._async_update_data_locked = AsyncMock(return_value=_plan("export"))
        schedule = owner._schedule_debounced_refresh
        owner._schedule_debounced_refresh = Mock()
        await owner._async_update_data()
        await owner._export_reconcile_task
        assert owner._settled_export_reconciliation is not None
        owner._schedule_debounced_refresh = schedule
        owner._async_update_data_locked.reset_mock()
        owner.async_update_listeners = Mock()

        core = HomeAssistant(str(tmp_path))
        owner._debounced_refresh = Debouncer(
            core, logging.getLogger(__name__), cooldown=10, immediate=True, function=owner._async_update_data,
        )
        owner.async_refresh = owner._async_update_data
        owner.async_request_refresh = DataUpdateCoordinator.async_request_refresh.__get__(owner)
        clock = 100.0
        timers = {}

        def schedule_timer(hass, delay, callback):
            token = object()
            timers[token] = (clock + delay, callback)
            return lambda: timers.pop(token, None)

        monkeypatch.setattr(module, "async_call_later", schedule_timer)
        monkeypatch.setattr(module, "monotonic", lambda: clock)
        boundary = NOW + timedelta(minutes=5)
        def schedule_export():
            owner._schedule_export_boundary({"blocks": [{"end": boundary.isoformat()}]})

        first, second = schedule_export, owner._schedule_next_boundary_refresh
        if not export_first:
            first, second = second, first
        first()
        clock += 0.0001  # Independent call_later registrations can drift across loop turns.
        second()
        first_token, second_token = timers
        clock, first_callback = timers.pop(first_token)
        second_at, second_callback = timers[second_token]
        assert second_at > clock
        if interval_under_floor:
            owner._last_non_manual_refresh_requested_at = clock - 5
        advance_clock(setup)
        first_callback(boundary)

        due = [token for token, (at, _) in timers.items() if at <= clock]
        assert len(due) == 1
        timers.pop(due[0])[1](boundary)
        try:
            await asyncio.gather(*owner._listener_tasks)
            assert second_token not in timers
            clock = second_at
            second_callback(boundary)
            owner._async_update_data_locked.assert_awaited_once()
            assert owner._export_reconcile_task is None
            assert not owner._export_boundary_followup_due
            assert not owner._debounced_refresh._execute_at_end_of_timer
            assert not setup.calls
            assert all(at > clock + 60 for at, _ in timers.values())
            owner.async_update_listeners.assert_called_once()
            assert owner.last_refresh_metadata["trigger"] == "export_tariff_boundary"
        finally:
            owner._debounced_refresh.async_shutdown()

    asyncio.run(run())


def test_control_combinations_config_identity_and_sensor_presentation(setup):
    from custom_components.ha_energy_planner.config_flow import _validate_config
    from custom_components.ha_energy_planner.const import CONF_ENPHASE_CONTROL_ENABLED, CONF_ENPHASE_PROFILE
    from custom_components.ha_energy_planner.plan_presentation import action_sentence
    from custom_components.ha_energy_planner.preflight import _control_area_report
    from custom_components.ha_energy_planner.sensor import (
        _controlled_state_attrs,
        _next_actions_attrs,
        _next_actions_state,
    )

    for profile in (False, True):
        for export in (False, True):
            opts = {**OPTIONS, CONF_ENPHASE_CONTROL_ENABLED: profile, CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED: export}
            report = _control_area_report({**DATA, CONF_ENPHASE_PROFILE: "select.profile"}, opts)
            assert ("enphase" in report["required"]) == profile
            assert (EXPORT_ASSET in report["required"]) == export
    assert _validate_config(setup.hass, DATA) == {}
    setup.registry[ENTITY].platform = "invalid"
    assert _validate_config(setup.hass, DATA)[CONF_ENPHASE_EXPORT_LIMIT_ENTITY] == "invalid_export_limit_entity"
    setup.registry[ENTITY].platform = "enphase_ev"
    ev = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
    plan = DryRunPlanner(OPTIONS).create_plan(context(ev))
    owner = SimpleNamespace(entry_data=DATA, options=OPTIONS, data=plan, store=setup.store, hass=setup.hass)
    assert _controlled_state_attrs(owner)["export_limit"]["confirmed_setting"] == "Disabled"
    assert "Export Limit" in _next_actions_state(owner)
    assert _next_actions_attrs(owner)["export_limit"]["confirmed_setting"] == "Disabled"
    assert action_sentence(plan.actions[0]) == "Enable Export Limit at 0 W"
    assert action_sentence(plan.actions[1]) == "Disable Export Limit"
    ev["ownership"] = {"pending": {"watts": 0}}
    plan = DryRunPlanner(OPTIONS).create_plan(context(ev))
    assert plan.device_plans[EXPORT_ASSET]["current_state_label"] == "Pending"
    ev["ownership"] = {"pause": "export_limit_unconfirmed"}
    reviewed = DryRunPlanner(OPTIONS).create_plan(context(ev))
    assert reviewed.device_plans[EXPORT_ASSET]["current_state_label"] == "Unconfirmed"


def test_restore_isolated_from_profile_and_pending_outcome(setup):
    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        setup.store.data["ownership"]["enphase_profile"] = "Full Backup"
        setup.mode["value"] = "pending"
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=OPTIONS)
        result = await executor.async_restore_device_control(EXPORT_ASSET, "export_disabled")
        assert result.result == OutcomeResult.PENDING
        assert setup.store.data["ownership"]["enphase_profile"] == "Full Backup"
        assert setup.control.state["restoring"]

    asyncio.run(run())


@pytest.mark.parametrize("stage", [
    "seed", "pending", "restore", "review", "unsupported", "off_restore", "off", "reconciliation_pending",
    "replacement", "pending_replacement_tariff_gap", "legacy_baseline", "unverified_readback", "remapped_control",
    "remapped_unavailable", "remapped_unknown", "remapped_missing",
])
def test_coordinator_refresh_reconciles_export_transaction(setup, monkeypatch, stage):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: stage == "review", "ai_enabled": False}
        if stage in {"off_restore", "off"}:
            opts[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED] = False
        coordinator = _coordinator_for_runtime_services(entry_data=DATA, options=opts, hass=setup.hass)
        coordinator.store.data = setup.store.data
        coordinator.store.async_add_outcome = setup.store.async_add_outcome
        coordinator.store.async_flush = setup.store.async_flush
        coordinator._export_boundary_cancel = None
        coordinator._tearing_down = False
        coordinator._load_forecast_training_attempted = False
        coordinator._force_next_refresh = True
        setup.hass.state = module.CoreState.running if hasattr(module, "CoreState") else None
        setup.hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro)
        setup.hass.async_add_executor_job = lambda func, *args: asyncio.to_thread(func, *args)
        boundaries = []
        coordinator._schedule_export_boundary = boundaries.append
        if stage == "off":
            coordinator._export_boundary_cancel = lambda: None
        coordinator._schedule_debounced_refresh = lambda *args, **kwargs: None
        if stage == "unsupported":
            setup.states[ENTITY].state = "unsupported"
        remapped = stage.startswith("remapped_")
        known_replacement = stage in {"replacement", "pending_replacement_tariff_gap"}
        identity_case = known_replacement or stage in {"legacy_baseline", "unverified_readback"} or remapped
        if identity_case:
            ready_plan = DryRunPlanner(opts).create_plan(context(runtime.evidence(setup.hass, DATA, opts, NOW)))
            assert ready_plan.control_area_health[EXPORT_ASSET]["ready"]
            original = {"watts": None, "slew_rate": 100}
            ownership = {"identity": runtime.target_identity(setup.hass, ENTITY), "baseline": original,
                         "expected": original}
            if stage != "legacy_baseline":
                ownership["gateway_identity"] = GATEWAY
            await setup.control.save(ownership)
            if stage == "pending_replacement_tariff_gap":
                setup.states[ENTITY] = limit_state(pending=True)
                setup.states.pop(DATA[CONF_AMBER_EXPORT_PRICE])
            if known_replacement or remapped:
                setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
            if remapped:
                setup.registry[ENTITY].device_id = "different-control"
                if stage == "remapped_missing":
                    setup.states.pop(ENTITY)
                elif stage != "remapped_control":
                    setup.states[ENTITY].state = stage.removeprefix("remapped_")
            elif stage == "unverified_readback":
                setup.states[ENTITY].attributes["gateway_identity_verified"] = False
        ctx = context({} if stage in {"off_restore", "off"} else runtime.evidence(setup.hass, DATA, opts, NOW))

        async def build_context(overrides):
            if stage == "off_restore":
                ctx.created_at = advance_clock(setup, 1)
            return ctx

        monkeypatch.setattr(module, "InputManager", lambda *args, **kwargs: SimpleNamespace(
            current_forecast_observations=lambda: {}, async_build_context=build_context,
            retained_hvac_tariff_slots=lambda context: None, thermal_sample=lambda built: {},
            forecast_training_slots=[], forecast_calibration={}, load_forecast_details={}))
        if stage in {"pending", "restore", "off_restore"}:
            await setup.control.execute(action(setup), NOW)
            coordinator.store.data["ownership"] = deepcopy(setup.store.data["ownership"])
        if stage == "off_restore":
            coordinator.store.data["ownership"][EXPORT_ASSET]["restoring"] = True
        if stage == "restore":
            setup.states[ENTITY] = limit_state(0)
            coordinator.store.data["ownership"][EXPORT_ASSET]["restoring"] = True
            coordinator.store.data["ownership"][EXPORT_ASSET].pop("pending")
            coordinator.store.data["ownership"][EXPORT_ASSET]["expected"] = {"watts": 0, "slew_rate": 100}
            setup.mode["value"] = "pending"
            coordinator.store.async_add_outcome = setup.store.async_add_outcome
            coordinator.store.async_flush = setup.store.async_flush
        if stage == "off_restore":
            setup.hass.async_create_task = asyncio.create_task
            worker = coordinator._admit_export_reconciliation(schedule_followup=False)
            assert worker is not None
            await worker
        else:
            await coordinator._async_reconcile_export_limit()
        if stage == "reconciliation_pending":
            coordinator._export_reconcile_pending = True
        result = await coordinator._async_update_data_locked(defer_execution=True)
        if "history_training" in coordinator.__dict__ and coordinator.history_training.task:
            await coordinator.history_training.task
        if stage == "off":
            assert EXPORT_ASSET not in result.control_area_health
            assert boundaries == [{}]
            assert not result.actions
            return
        assert result.control_area_health[EXPORT_ASSET]
        if identity_case:
            from custom_components.ha_energy_planner.plan_presentation import export_limit_status, plain_reason
            from custom_components.ha_energy_planner.preflight import build_preflight_report

            reason = runtime.TARGET_CHANGED if remapped else runtime.GATEWAY_UNVERIFIED
            if known_replacement:
                reason = runtime.GATEWAY_CHANGED
            assert result.control_area_health[EXPORT_ASSET]["reason"] == reason
            assert not result.actions
            # Preflight and presentation must catch the mismatch even before
            # the coordinator publishes a replacement for a ready plan.
            coordinator.data = ready_plan
            identity = runtime.target_identity(setup.hass, ENTITY)
            timing = await setup.control.prepare_cooldown(NOW)
            await setup.control.save_timing(identity, {**timing, "attempted_at": NOW.timestamp()})
            status = export_limit_status(coordinator)
            assert status["reason"] == reason
            assert not status["feedback_ready"]
            assert status["deferred"]
            assert status["summary"] == plain_reason(reason)
            report = build_preflight_report(setup.hass, coordinator)
            assert not report["safe_to_activate_now"]
            assert EXPORT_ASSET not in report["control_areas"]["confidence_eligible"]
            assert report["control_areas"]["details"][EXPORT_ASSET]["reason"] == reason
            snapshot = coordinator._export_reconciliation_inputs()
            assert snapshot.issue == reason and not snapshot.reusable
            assert setup.control.state == ownership
            assert not setup.calls
        if stage == "seed":
            assert EXPORT_ASSET in coordinator.store.data["ownership"]
        elif stage != "review":
            assert not result.control_area_health[EXPORT_ASSET]["ready"]
        if stage == "off_restore":
            assert result.device_plans[EXPORT_ASSET]["ownership"]["restoring"]
            assert result.device_plans[EXPORT_ASSET]["current_state_label"] == "Pending"
            assert ctx.export_limit["feedback_issue"] is None
            assert not result.actions

    asyncio.run(run())


def test_feedback_attribute_change_requests_fresh_plan(setup, monkeypatch):
    from test_coordinator import FakeEvent, FakeHass, _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module

    callbacks, requests = [], []
    monkeypatch.setattr(module, "async_track_state_change_event", lambda hass, ids, callback: (
        callbacks.append(callback) or (lambda: None)))
    monkeypatch.setattr(module, "async_call_later", lambda *args: lambda: None)
    owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=FakeHass())
    owner._schedule_debounced_refresh = lambda *args, **kwargs: requests.append((args, kwargs))
    owner.async_start_listeners()
    callbacks[0](FakeEvent(ENTITY, "pending", "pending"))
    assert requests[-1][0] == ("export_limit_feedback",)
    assert requests[-1][1]["force"]


@pytest.mark.parametrize("poll", ["later", "changed_watts", "stale_recovery", "backward", "gateway_pending",
                                  "planner_pending", "restoring"])
def test_export_feedback_poll_keeps_queued_release_current_only_for_settled_settings(setup, monkeypatch, poll):
    from test_coordinator import _coordinated_action, _coordinator_for_runtime_services, _plan

    from custom_components.ha_energy_planner import coordinator as module
    from custom_components.ha_energy_planner.models import ActionAsset

    async def run():
        callbacks = []
        monkeypatch.setattr(module, "async_track_state_change_event", lambda hass, ids, callback: (
            callbacks.append(callback) or (lambda: None)))
        monkeypatch.setattr(module, "async_call_later", lambda *args: lambda: None)
        setup.hass.async_create_task = asyncio.create_task
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        if poll in {"planner_pending", "restoring"}:
            key = "pending" if poll == "planner_pending" else "restoring"
            owner.store.data["ownership"] = {EXPORT_ASSET: {key: True}}
        if poll == "stale_recovery":
            setup.states[ENTITY] = limit_state(readback=NOW.timestamp() - 601)
        owner.async_start_listeners()

        started, finish = asyncio.Event(), asyncio.Event()

        async def reconcile():
            started.set()
            await finish.wait()

        owner._async_reconcile_export_limit_locked = reconcile
        worker = owner._admit_export_reconciliation()
        await asyncio.wait_for(started.wait(), 1)
        plan = _plan("queued-release-during-poll")
        plan.created_at = NOW
        release = _coordinated_action(plan, "release", ActionAsset.DAIKIN, ActionKind.RELEASE_HVAC)
        plan.actions = [release]
        owner._pending_plan_execution = _PlanExecution(owner._refresh_generation, plan, object(), owner.planner_options)
        drain = asyncio.create_task(owner._async_drain_plan_execution())
        await asyncio.sleep(0)
        assert not drain.done()

        old = setup.states[ENTITY]
        setup.clock["now"] += timedelta(seconds=60)
        new = limit_state(
            3000 if poll == "changed_watts" else None,
            pending=poll == "gateway_pending",
            readback=NOW.timestamp() - 1 if poll == "backward" else setup.clock["now"].timestamp(),
        )
        setup.states[ENTITY] = new
        callbacks[0](SimpleNamespace(data={"entity_id": ENTITY, "old_state": old, "new_state": new}))
        finish.set()
        await asyncio.wait_for(asyncio.gather(worker, drain), 1)
        assert [item.actions for item, _ in owner.executor.evaluated] == ([[release]] if poll == "later" else [])

    asyncio.run(run())


def test_independent_toggle_and_explicit_resume(setup, monkeypatch):
    from unittest.mock import AsyncMock

    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        opts = {**OPTIONS, CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED: False}
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=opts, hass=setup.hass)
        setup.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, options: setattr(entry, "options", options))
        owner.async_handle_options_update = AsyncMock()
        await setup.control.save({"identity": runtime.target_identity(setup.hass, ENTITY),
                                  "pause": "external_export_limit_conflict"})
        owner.store.data = setup.store.data
        await owner.async_set_device_control(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED, True)
        assert owner.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED]
        assert not setup.control.state
        await owner.async_resume_control(EXPORT_ASSET)
        await owner.async_set_device_control(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED, False)
        assert not owner.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED]

    asyncio.run(run())


def test_export_constraint_windows_ignore_unrelated_energy_gates(setup):
    from custom_components.ha_energy_planner.constraints import ConstraintValidator

    opts = {**OPTIONS, CONF_DRY_RUN: False}
    ctx = context(runtime.evidence(setup.hass, DATA, opts, NOW))
    plan = DryRunPlanner(opts).create_plan(ctx)
    validator = ConstraintValidator(opts)
    assert not validator.validate_action(ctx, plan, plan.actions[0], now=NOW)
    assert validator.validate_action(ctx, plan, plan.actions[0], now=NOW.replace(minute=30)) == [
        "action_outside_execution_window"]
    plan.control_area_health[EXPORT_ASSET]["ready"] = False
    assert validator.validate_action(ctx, plan, plan.actions[0], now=NOW) == ["export_limit_not_ready"]


def test_dispatch_target_disappears_during_durable_flush(setup):
    async def run():
        original_flush = setup.store.async_flush
        a = action(setup)

        async def remove_target():
            await original_flush()
            setup.states.pop(ENTITY, None)

        setup.store.async_flush = remove_target
        assert await setup.control.execute(a, NOW) == (OutcomeResult.REJECTED, "export_limit_entity_unavailable", False)
        assert not setup.control.state
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("original,latest", [(GATEWAY, "b" * 64), (GATEWAY, None), (None, GATEWAY)])
def test_dispatch_gateway_identity_changes_during_durable_flush(setup, original, latest):
    async def run():
        attributes = setup.states[ENTITY].attributes
        if original is None:
            attributes.pop("gateway_identity")
            attributes.pop("gateway_identity_verified")
        original_flush = setup.store.async_flush
        a = action(setup)

        async def replace_gateway():
            await original_flush()
            attributes.update(gateway_identity=latest, gateway_identity_verified=latest is not None)

        setup.store.async_flush = replace_gateway
        reason = runtime.GATEWAY_CHANGED if original and latest else runtime.GATEWAY_UNVERIFIED
        assert await setup.control.execute(a, NOW) == (OutcomeResult.REJECTED, reason, False)
        assert not setup.control.state
        assert not setup.calls

    asyncio.run(run())


def test_pending_before_ownership_and_restoration_suppress_policy(setup):
    async def run():
        a = action(setup)
        setup.states[ENTITY] = limit_state(pending=True)
        assert (await setup.control.execute(a, NOW))[1] == "export_limit_pending"
        setup.states[ENTITY] = limit_state()
        observed, _ = runtime.feedback(setup.hass, ENTITY, NOW)
        await setup.control.save({"identity": observed["identity"], "gateway_identity": GATEWAY,
                                  "baseline": {"watts": 3000, "slew_rate": 100},
                                  "restoring": True})
        assert (await setup.control.execute(a, NOW))[1] == "export_limit_restoration_pending"
        assert not setup.calls

    asyncio.run(run())


def test_delayed_restoration_confirmation_and_readback_recheck(setup, monkeypatch):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        setup.mode["value"] = "pending"
        now = advance_clock(setup)
        await setup.control.restore(now)
        setup.states[ENTITY] = limit_state(readback=now.timestamp())
        assert (await setup.control.restore(now))[0] == OutcomeResult.RESTORED
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        now = advance_clock(setup)
        await setup.control.execute(action(setup), now)
        original = setup.control.reconcile

        async def disappearing(now):
            result = await original(now)
            setup.states.pop(ENTITY)
            return result

        monkeypatch.setattr(setup.control, "reconcile", disappearing)
        assert (await setup.control.restore(now))[1] == "export_limit_entity_unavailable"

    asyncio.run(run())


def test_public_feedback_unavailable():
    from custom_components.ha_energy_planner.export_limit_policy import confirmed_feedback

    assert confirmed_feedback(None, NOW) == ({}, "export_limit_entity_unavailable")


def test_gap_is_not_extended_into_coverage():
    state = price_state((-1, 0))
    state.attributes["forecasts"][1].update(start_time="2026-10-01T01:00:00+00:00",
                                         end_time="2026-10-01T01:30:00+00:00")
    assert tariff_blocks(state, NOW, OPTIONS) == ([], "export_tariff_gap")


def test_startup_input_outage_preserves_owned_export_setting(setup):
    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        saved = deepcopy(setup.control.state)
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=OPTIONS)
        await executor.async_restore_safe_state("startup_grace_unsafe")
        assert setup.control.state == saved
        assert len([call for call in setup.calls if call[0] == "enphase_ev"]) == 1

    asyncio.run(run())


def test_resume_selected_asset_preserves_other_pauses(setup):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store.data = setup.store.data
        await setup.control.save({"identity": runtime.target_identity(setup.hass, ENTITY),
                                  "pause": "external_export_limit_conflict"})
        await owner.async_pause_control(60, "operator", "all")
        await owner.async_resume_control(asset="enphase")
        assert EXPORT_ASSET in owner.store.data["control_pause"]["assets"]
        assert setup.control.state["pause"] == "external_export_limit_conflict"
        await owner.async_resume_control(asset=EXPORT_ASSET)
        assert EXPORT_ASSET not in owner.store.data["control_pause"]["assets"]
        assert not setup.control.state

    asyncio.run(run())


@pytest.mark.parametrize("pause", [None, {}, {"active": False, "assets": ["all"]},
                                  {"active": "off", "assets": ["all"]},
                                  {"active": True, "assets": ["all"], "until": NOW - timedelta(seconds=1)},
                                  {"active": True, "assets": [EXPORT_ASSET]}])
def test_export_resume_does_not_create_unrelated_pauses(setup, pause):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.safety import control_pause_reason

    async def run():
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store.data = setup.store.data
        owner.store.data["control_pause"] = pause
        await setup.control.save({"identity": runtime.target_identity(setup.hass, ENTITY),
                                  "pause": "external_export_limit_conflict"})
        await owner.async_resume_control(asset=EXPORT_ASSET)
        assert not setup.control.state
        assert control_pause_reason(owner.store.data["control_pause"], NOW) is None
        assert not owner.store.data["control_pause"]["assets"]
        assert not setup.calls

    asyncio.run(run())


def test_export_resume_preserves_legacy_string_profile_pause(setup):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.safety import control_pause_reason

    async def run():
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store.data["control_pause"] = {"active": True, "assets": "enphase"}
        await owner.async_resume_control(asset=EXPORT_ASSET)
        pause = owner.store.data["control_pause"]
        assert pause["assets"] == ["enphase"]
        assert control_pause_reason(pause, NOW, asset="enphase") == "enphase_control_paused"
        assert control_pause_reason(pause, NOW, asset=EXPORT_ASSET) is None

    asyncio.run(run())


@pytest.mark.parametrize("key", ["confidence", "confidence_percent", "forecast_confidence",
                                 "forecast_confidence_percent"])
@pytest.mark.parametrize("value,expected", [(0.75, 0.75), (75, 0.75), ("75", 0.75),
                                          ("0.75", 0.75), (50, 0.5)])
def test_export_tariff_accepts_supported_confidence_encodings(key, value, expected):
    state = price_state()
    state.attributes[key] = value
    blocks, issue = tariff_blocks(state, NOW, OPTIONS)
    assert issue is None
    assert blocks[0]["confidence"] == expected


@pytest.mark.parametrize("key", ["confidence", "confidence_percent", "forecast_confidence",
                                 "forecast_confidence_percent"])
@pytest.mark.parametrize("value", [0, 0.1, 10, "10", -1, 101, float("nan"), float("inf"),
                                   "bad", None, True, []])
def test_export_tariff_rejects_low_or_invalid_confidence(setup, key, value):
    state = setup.states["sensor.export_price"]
    state.attributes[key] = value
    assert tariff_blocks(state, NOW, OPTIONS) == ([], "export_tariff_confidence_low")
    ev = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
    assert not ev["ready"]
    assert build_actions(context(ev)) == []


@pytest.mark.parametrize("key", ["confidence_percent", "forecast_confidence", "forecast_confidence_percent"])
def test_export_dispatch_rechecks_forecast_confidence(setup, key):
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False}
        ctx = context(runtime.evidence(setup.hass, DATA, opts, NOW))
        plan = DryRunPlanner(opts).create_plan(ctx)
        plan.actions = plan.actions[:1]
        setup.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(DATA, opts)
        setup.states["sensor.export_price"].attributes[key] = 0.1
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=opts)
        await executor.async_evaluate(plan, ctx)
        assert not setup.calls
        assert not setup.control.state
        assert setup.store.data["execution_audit"][-1]["result"] == "rejected"
        assert "export_tariff_confidence_low" in setup.store.data["execution_audit"][-1]["reason"]

    asyncio.run(run())


def test_new_switch_uses_separate_option_and_keeps_profile_identity(setup, monkeypatch):
    from unittest.mock import AsyncMock

    from custom_components.ha_energy_planner.switch import SWITCHES, PlannerSwitch

    owner = SimpleNamespace(entry=SimpleNamespace(entry_id="entry-a", title="Energy Planner"),
                            options=OPTIONS, async_set_device_control=AsyncMock())
    selected = next(item for item in SWITCHES if item.option_key == CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED)
    switch = PlannerSwitch(owner, selected)
    monkeypatch.setattr(switch, "async_write_ha_state", lambda: None)
    asyncio.run(switch.async_turn_on())
    owner.async_set_device_control.assert_awaited_with(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED, True)
    asyncio.run(switch.async_turn_off())
    owner.async_set_device_control.assert_awaited_with(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED, False)
    original = PlannerSwitch(owner, next(item for item in SWITCHES if item.key == "enphase_control"))
    assert original.unique_id == "entry-a_enphase_control"
    assert original.entity_description.option_key == "enphase_control_enabled"


def test_curtailed_preview_keeps_local_solar_and_omits_missing_energy_estimates(setup):
    from custom_components.ha_energy_planner.export_limit_policy import curtailment_preview

    ctx = context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
    assert curtailment_preview(ctx, ctx.slots[0])["projected_grid_export_kw"] == 0
    ctx.slots[0].valid_at = NOW.replace(minute=30)
    assert curtailment_preview(ctx, ctx.slots[0])["projected_grid_export_kw"] == 4
    ctx.slots[0].pv_forecast_kw = None
    assert curtailment_preview(ctx, ctx.slots[0])["projected_grid_export_kw"] is None
    assert DryRunPlanner(OPTIONS).create_plan(ctx).estimated_daily_cost is None


def test_explicit_disarm_restores_export_ownership(setup):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store.data = setup.store.data
        await owner.async_operator_disarm_production_control()
        assert owner.executor.device_restores == [(EXPORT_ASSET, "production_control_disarmed")]

    asyncio.run(run())


def test_resume_service_accepts_export_area(setup):
    from unittest.mock import AsyncMock

    from test_services import FakeCall, FakeHass, _coordinator

    from custom_components.ha_energy_planner import async_setup
    from custom_components.ha_energy_planner.const import DOMAIN, SERVICE_RESUME_CONTROL

    async def run():
        owner = _coordinator()
        owner.async_resume_control = AsyncMock()
        hass = FakeHass(owner)
        await async_setup(hass, {})
        schema = hass.services.schemas[(DOMAIN, SERVICE_RESUME_CONTROL)]
        data = schema({"asset": EXPORT_ASSET})
        await hass.services.handlers[(DOMAIN, SERVICE_RESUME_CONTROL)](FakeCall(data))
        owner.async_resume_control.assert_awaited_with("user_requested", asset=EXPORT_ASSET)

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    ["unarmed", "dry_run", "mapping", "registry", "pending", "external", "expired", "tariff", "nested_tariff"],
)
def test_dispatch_rechecks_after_persistence(setup, monkeypatch, change):
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False}
        if change == "nested_tariff":
            entity = DATA[CONF_AMBER_EXPORT_PRICE]
            raw = setup.states[entity]
            setup.states[entity] = State(entity, raw.state, raw.attributes, last_updated=NOW)
        ctx = context(runtime.evidence(setup.hass, DATA, opts, NOW))
        plan = DryRunPlanner(opts).create_plan(ctx)
        plan.actions = plan.actions[:1]
        executor = Executor(setup.store, hass=setup.hass, entry_data=dict(DATA), options=opts)
        setup.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(DATA, opts)
        flush = setup.store.async_flush

        async def changed_while_flushing():
            await flush()
            if change == "unarmed":
                setup.store.data["production"]["armed"] = False
            elif change == "dry_run":
                executor.options[CONF_DRY_RUN] = True
            elif change == "mapping":
                executor.entry_data = {**DATA, CONF_ENPHASE_EXPORT_LIMIT_ENTITY: "sensor.other_export_limit"}
            elif change == "registry":
                setup.registry[ENTITY].config_entry_id = "new-site"
            elif change == "pending":
                setup.states[ENTITY] = limit_state(pending=True)
            elif change == "external":
                setup.states[ENTITY] = limit_state(5000)
            elif change == "expired":
                monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda tz: NOW.replace(minute=30)))
            elif change == "nested_tariff":
                setup.states["sensor.export_price"].attributes["forecasts"][0]["per_kwh"] = 0.2
            else:
                setup.states["sensor.export_price"] = price_state((0, 1, 1))

        setup.store.async_flush = changed_while_flushing
        await executor.async_evaluate(plan, ctx)
        assert not setup.calls
        assert not setup.control.state.get("pending")
        assert setup.store.data["execution_audit"][-1]["result"] == "rejected"

    asyncio.run(run())


def test_target_race_at_service_boundary_and_upstream_controls_disabled(setup, monkeypatch):
    from custom_components.ha_energy_planner.adapter_helpers import DeviceTargetUnavailable

    async def run():
        a = action(setup)

        async def unavailable(*args):
            raise DeviceTargetUnavailable(ENTITY)

        monkeypatch.setattr(runtime, "async_call_device_service", unavailable)
        assert (await setup.control.execute(a, NOW))[1] == "export_limit_entity_unavailable"

        async def disabled(*args):
            raise ServiceValidationError(translation_domain="enphase_ev", translation_key="export_limit_disabled")

        monkeypatch.setattr(runtime, "async_call_device_service", disabled)
        outcome = await setup.control.execute(a, NOW)
        assert outcome[1] == "export_limit_service_rejected:controls_disabled"
        assert setup.control.state["pause"] == outcome[1]

    asyncio.run(run())


@pytest.mark.parametrize(("cause", "domain", "temporary"), [
    (TimeoutError(), "enphase_ev", True),
    (ServerDisconnectedError(), "enphase_ev", True),
    (ClientResponseError(None, (), status=503), "enphase_ev", True),
    (ClientResponseError(None, (), status=403), "enphase_ev", False),
    (ValueError(), "enphase_ev", False),
    (None, "enphase_ev", False),
    (TimeoutError(), "other", False),
])
def test_export_retry_requires_evidence_of_a_temporary_rejection(setup, monkeypatch, cause, domain, temporary):
    async def reject(*args):
        raise ServiceValidationError(translation_domain=domain, translation_key="export_limit_unavailable") from cause

    monkeypatch.setattr(runtime, "async_call_device_service", reject)
    result = asyncio.run(setup.control.execute(action(setup), NOW))
    assert result == (OutcomeResult.FAILED, "export_limit_service_rejected:installer_or_gateway_unavailable", True)
    assert bool(setup.control.state.get("retry")) is temporary
    assert "pending" not in setup.control.state


def installer_rejection(key="export_limit_unavailable", cause=None):
    error = ServiceValidationError(translation_domain="enphase_ev", translation_key=key)
    error.__cause__ = cause
    return error


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(("key", "reason"), [
    ("export_limit_changed", "export_limit_target_changed"),
    ("export_limit_pending", "export_limit_pending"),
    ("export_limit_mismatch", runtime.INSTALLER_FORM_MISMATCH),
])
def test_identity_bound_service_rejection_rolls_back_unsent_write(setup, monkeypatch, restore, key, reason):
    async def run():
        if restore:
            setup.mode["value"] = "confirm"
            await setup.control.execute(action(setup), NOW)
        now = advance_clock(setup) if restore else NOW
        prior_timing = setup.control.cooldown(now)["last_dispatch_reserved_at"]

        async def reject(hass, domain, service, data):
            assert domain == "enphase_ev" and data["expected_gateway"] == GATEWAY
            raise installer_rejection(key)

        monkeypatch.setattr(runtime, "async_call_device_service", reject)
        if restore:
            result = await setup.control.restore(now)
            assert setup.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
            assert setup.control.state["gateway_identity"] == GATEWAY
            assert setup.store.data["execution_audit"][-1]["command_sent"] is False
        else:
            result = await setup.control.execute(action(setup), now)
            assert "baseline" not in setup.control.state
        assert result == (OutcomeResult.PENDING if restore else OutcomeResult.REJECTED, reason, False)
        assert setup.control.state.get("pause") == (reason if key == "export_limit_mismatch" else None)
        assert not setup.control.state.get("pending")
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == prior_timing
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == int(restore)

    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("gateway_changed", [False, True])
def test_installer_probe_failure_counts_without_gateway_replacement(setup, retry_owner, expired, gateway_changed):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        rejection = (installer_rejection("export_limit_session_expired") if expired
                     else installer_rejection(cause=TimeoutError()))

        async def prepare():
            upstream.snapshot = None
            if gateway_changed:
                state = limit_state(readback=setup.clock["now"].timestamp())
                state.attributes["gateway_identity"] = "b" * 64
                setup.states[ENTITY] = state
            else:
                setup.states[ENTITY] = SimpleNamespace(state="unavailable", attributes={})
            raise rejection

        upstream = retry_owner.upstream
        upstream.snapshot = None
        upstream.last_readback = NOW.timestamp()
        setup.states[ENTITY] = SimpleNamespace(state="unavailable", attributes={})
        upstream.async_prepare.side_effect = prepare
        for _ in range(4):
            advance_clock(setup)
            await owner._async_retry_export_limit()

        retry = setup.control.state["retry"]
        assert retry["state"] == "operator_required"
        assert upstream.async_prepare.await_count == (1 if expired or gateway_changed else 3)
        if gateway_changed:
            assert retry["attempts"] == 0
            assert setup.control.state["pause"] == runtime.GATEWAY_CHANGED
        else:
            assert retry["attempts"] == upstream.async_prepare.await_count
            assert setup.control.state["pause"] == (
                runtime.INSTALLER_SESSION_EXPIRED if expired else "export_limit_retry_exhausted"
            )
        upstream.async_apply_guarded.assert_not_awaited()

    asyncio.run(run())


def test_guarded_retry_form_mismatch_requires_operator_without_write_charge(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        upstream = retry_owner.upstream
        upstream.async_apply_guarded.side_effect = installer_rejection("export_limit_mismatch")
        now = advance_clock(setup)
        await owner._async_retry_export_limit()
        assert setup.control.state["pause"] == runtime.INSTALLER_FORM_MISMATCH
        assert setup.control.state["retry"]["state"] == "operator_required"
        assert setup.control.state["retry"]["attempts"] == 0
        assert "pending" not in setup.control.state
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1
        advance_clock(setup)
        await owner._async_retry_export_limit()
        upstream.async_apply_guarded.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("transient", [False, True])
def test_guarded_retry_classifies_gateway_rejection_after_successful_prepare(setup, retry_owner, transient):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        upstream = retry_owner.upstream
        failure = ClientResponseError(None, (), status=503) if transient else None
        upstream.async_apply_guarded.side_effect = installer_rejection(cause=failure)
        for _ in range(4):
            now = advance_clock(setup)
            await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert retry["attempts"] == (3 if transient else 0)
        assert retry["state"] == "operator_required"
        assert retry["reason"] == (runtime.UNAVAILABLE_REJECTION if transient else runtime.INSTALLER_WRITE_REJECTED)
        assert upstream.async_prepare.await_count == upstream.async_apply_guarded.await_count == (3 if transient else 1)
        assert "pending" not in setup.control.state
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("saved, replacement", [(GATEWAY, "b" * 64), (GATEWAY, None), (None, GATEWAY)])
def test_retry_and_restoration_require_original_gateway(setup, retry_owner, saved, replacement):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        assert setup.control.state["gateway_identity"] == GATEWAY
        if saved is None:
            setup.store.data["ownership"][EXPORT_ASSET].pop("gateway_identity")
        now = advance_clock(setup)
        setup.states[ENTITY].attributes["gateway_identity"] = replacement
        await owner._async_retry_export_limit()
        expected = runtime.GATEWAY_CHANGED if saved and replacement else runtime.GATEWAY_UNVERIFIED
        assert setup.control.state["pause"] == expected
        assert setup.control.state["retry"]["state"] == "operator_required"
        assert (await setup.control.restore(now))[1] == expected
        retry_owner.upstream.async_apply_guarded.assert_not_awaited()
        assert len(setup.calls) == 1
        if replacement:
            await setup.control.resume()
            assert not setup.control.state

    asyncio.run(run())


@pytest.mark.parametrize("tariff_gap", [False, True])
@pytest.mark.parametrize("replace_after_reconcile", [False, True])
def test_retry_rechecks_gateway_before_a_prepared_no_op(setup, retry_owner, monkeypatch,
                                                          tariff_gap, replace_after_reconcile):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)

        def publish_replacement():
            setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
            setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
            if tariff_gap:
                tariff = price_state(start=now + timedelta(minutes=30))
                tariff.last_updated = now
                setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = tariff

        if replace_after_reconcile:
            reconcile = runtime.ExportLimitControl.reconcile

            async def reconcile_then_replace(control, at):
                issue = await reconcile(control, at)
                publish_replacement()
                return issue

            monkeypatch.setattr(runtime.ExportLimitControl, "reconcile", reconcile_then_replace)
        else:
            async def prepare_then_replace():
                snapshot = await retry_owner.prepare()
                publish_replacement()
                return snapshot

            retry_owner.upstream.async_prepare.side_effect = prepare_then_replace
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert (retry["state"], retry["reason"], retry["attempts"]) == ("operator_required", runtime.GATEWAY_CHANGED, 0)
        assert setup.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
        retry_owner.upstream.async_apply_guarded.assert_not_awaited()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("when", [
    "before_claim", "during_claim_save", "during_pending_deferral", "resume_during_deferral",
])
@pytest.mark.parametrize("gateway_issue", [runtime.GATEWAY_CHANGED, runtime.GATEWAY_UNVERIFIED])
def test_pending_gateway_issue_blocks_retry_before_installer_probe(setup, retry_owner, when, gateway_issue):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        flush = setup.store.async_flush

        def change_gateway_and_tariff():
            state = setup.states[ENTITY] = limit_state(pending=True, readback=now.timestamp())
            if gateway_issue == runtime.GATEWAY_CHANGED:
                state.attributes["gateway_identity"] = "b" * 64
            else:
                state.attributes["gateway_identity_verified"] = False
            tariff = price_state(start=now + timedelta(minutes=30))
            tariff.last_updated = now
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = tariff

        async def publish_during_save():
            await flush()
            retry = setup.control.state["retry"]
            if retry["state"] == "checking" or retry["reason"] == "export_limit_pending":
                if when == "resume_during_deferral":
                    await setup.control.resume()
                change_gateway_and_tariff()

        if when == "before_claim":
            change_gateway_and_tariff()
        else:
            if when in {"during_pending_deferral", "resume_during_deferral"}:
                setup.states[ENTITY] = limit_state(pending=True, readback=now.timestamp())
            setup.store.async_flush = publish_during_save
        await owner._async_retry_export_limit()
        if when == "resume_during_deferral":
            assert "retry" not in setup.control.state and "pause" not in setup.control.state
        else:
            retry = setup.control.state["retry"]
            assert (retry["state"], retry["reason"], retry["attempts"]) == ("operator_required", gateway_issue, 0)
        retry_owner.upstream.async_prepare.assert_not_awaited()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("when", ["before_claim", "during_save", "during_probe"])
def test_unavailable_readback_with_pending_companion_preserves_retry_budget(setup, retry_owner, when):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        advance_clock(setup)
        flush = setup.store.async_flush
        upstream = retry_owner.upstream

        def lose_readback():
            upstream.pending = {"watts": 0}
            setup.states[ENTITY] = SimpleNamespace(state="unavailable", attributes={"pending": True})

        async def during_save():
            await flush()
            if setup.control.state["retry"]["state"] == "checking":
                lose_readback()

        async def prepare():
            lose_readback()
            raise installer_rejection(cause=TimeoutError())

        if when == "before_claim":
            lose_readback()
        elif when == "during_save":
            setup.store.async_flush = during_save
        upstream.async_prepare.side_effect = prepare
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert (retry["state"], retry["reason"], retry["attempts"]) == ("scheduled", "export_limit_pending", 0)
        assert upstream.async_prepare.await_count == int(when == "during_probe")

    asyncio.run(run())


@pytest.mark.parametrize("pending_at", ["claim", "probe", "tariff_gap", "guarded_cleanup"])
def test_replacement_latches_after_claimed_retry_deferral(setup, retry_owner, pending_at):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        flush = setup.store.async_flush

        def gateway_pending(gateway):
            state = limit_state(pending=True, readback=now.timestamp())
            state.attributes["gateway_identity"] = gateway
            setup.states[ENTITY] = state

        def tariff_gap():
            tariff = price_state(start=now + timedelta(minutes=30))
            tariff.last_updated = now
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = tariff

        async def publish_during_save():
            await flush()
            retry = setup.control.state["retry"]
            if pending_at == "claim" and retry["state"] == "checking":
                gateway_pending(GATEWAY)
            if retry["reason"] in {"export_limit_pending", "export_tariff_block_missing"}:
                gateway_pending("b" * 64)
                tariff_gap()

        async def pending_during_probe():
            snapshot = await retry_owner.prepare()
            if pending_at == "tariff_gap":
                tariff_gap()
            else:
                retry_owner.upstream.pending = {}
                gateway_pending(GATEWAY)
            return snapshot

        async def replace_during_guarded_cleanup(watts, *, expected, before_write):
            tariff_gap()
            try:
                before_write()
            except runtime.RetryWriteDeferred:
                gateway_pending("b" * 64)
                raise

        setup.store.async_flush = publish_during_save
        if pending_at in {"probe", "tariff_gap"}:
            retry_owner.upstream.async_prepare.side_effect = pending_during_probe
        elif pending_at == "guarded_cleanup":
            retry_owner.upstream.async_apply_guarded.side_effect = replace_during_guarded_cleanup
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert (retry["state"], retry["reason"], retry["attempts"]) == ("operator_required", runtime.GATEWAY_CHANGED, 0)
        assert not setup.control.state.get("pending")
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("gateway_change", ["unverified", "replaced", "replaced_pending"])
def test_retry_recognizes_gateway_issue_after_unsent_provider_rejection_with_tariff_gap(
    setup, retry_owner, gateway_change,
):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)

        async def apply(watts, *, expected, before_write):
            retry_owner.upstream.snapshot = expected
            if gateway_change == "replaced_pending":
                setup.states[ENTITY] = limit_state(pending=True, readback=now.timestamp())
            if gateway_change.startswith("replaced"):
                setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
            else:
                setup.states[ENTITY].attributes["gateway_identity_verified"] = False
            tariff = price_state(start=now + timedelta(minutes=30))
            tariff.last_updated = now
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = tariff
            raise installer_rejection("export_limit_changed")

        retry_owner.upstream.async_apply_guarded.side_effect = apply
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        reason = runtime.GATEWAY_UNVERIFIED if gateway_change == "unverified" else runtime.GATEWAY_CHANGED
        assert (retry["state"], retry["reason"], retry["attempts"]) == ("operator_required", reason, 0)
        assert "pending" not in setup.control.state
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["prepare_unavailable", "snapshot_replaced", "snapshot_replaced_pending",
                                   "after_probe", "after_evidence", "replaced_after_evidence", "apply_unavailable",
                                   "apply_changed"])
def test_retry_stops_when_gateway_readback_changes_verification(setup, retry_owner, monkeypatch, phase):
    from custom_components.ha_energy_planner import coordinator as coordinator_module

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        now = advance_clock(setup)
        snapshot_replaced = phase.startswith("snapshot_replaced")

        def lose_snapshot(key):
            retry_owner.upstream.snapshot = None
            setup.states[ENTITY].state = "unavailable"
            raise installer_rejection(key)

        async def prepare():
            if phase == "prepare_unavailable":
                lose_snapshot("export_limit_unavailable")
            watts = 0 if phase == "after_probe" else None
            setup.states[ENTITY] = limit_state(watts, readback=now.timestamp())
            snapshot = await retry_owner.prepare()
            if snapshot_replaced:
                # The installer can return a replacement before HA publishes it.
                snapshot.gateway_identity = "b" * 64
                if phase.endswith("pending"):
                    retry_owner.upstream.pending = {}
            if phase == "after_probe":
                setup.states[ENTITY].attributes["gateway_identity_verified"] = False
            return snapshot

        expected_reason = runtime.GATEWAY_CHANGED if snapshot_replaced else runtime.GATEWAY_UNVERIFIED
        if phase.endswith("after_evidence"):
            original = coordinator_module.export_limit_evidence
            if phase == "replaced_after_evidence":
                expected_reason = runtime.GATEWAY_CHANGED

            def evidence_then_gateway_change(*args, **kwargs):
                result = original(*args, **kwargs)
                if result["ready"]:
                    if phase == "replaced_after_evidence":
                        setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
                    else:
                        setup.states[ENTITY].attributes["gateway_identity_verified"] = False
                return result

            monkeypatch.setattr(coordinator_module, "export_limit_evidence", evidence_then_gateway_change)

        if phase.startswith("apply_"):
            async def apply(watts, *, expected, before_write):
                lose_snapshot(phase.replace("apply_", "export_limit_"))

            retry_owner.upstream.async_apply_guarded.side_effect = apply

        retry_owner.upstream.async_prepare.side_effect = prepare
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert (retry["state"], retry["reason"], retry["attempts"]) == (
            "operator_required", expected_reason, 2,
        )
        assert retry["next_attempt_at"] is None
        assert setup.control.state["pause"] == expected_reason
        assert setup.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
        assert retry_owner.upstream.async_apply_guarded.await_count == int(phase.startswith("apply_"))
        assert not setup.control.state.get("pending")
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("blocked_by", ["feedback", "tariff"])
def test_retry_defers_if_evidence_becomes_unavailable_after_successful_probe(setup, retry_owner, blocked_by):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        now = advance_clock(setup)

        async def prepare():
            snapshot = await retry_owner.prepare()
            if blocked_by == "feedback":
                retry_owner.upstream.snapshot = None
                retry_owner.upstream.last_readback = now.timestamp()
                setup.states[ENTITY] = SimpleNamespace(state="unavailable", attributes={})
            else:
                tariff = price_state(start=now + timedelta(minutes=30))
                tariff.last_updated = now
                setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = tariff
            return snapshot

        retry_owner.upstream.async_prepare.side_effect = prepare
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        reason = "export_limit_entity_unavailable" if blocked_by == "feedback" else "export_tariff_block_missing"
        assert (retry["state"], retry["reason"], retry["attempts"]) == ("scheduled", reason, 2)
        assert retry["next_attempt_at"] == (now + runtime.RETRY_INTERVAL).isoformat()
        assert setup.control.state["pause"] == runtime.UNAVAILABLE_REJECTION
        retry_owner.upstream.async_apply_guarded.assert_not_awaited()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("pending", [False, True])
def test_unverified_gateway_hash_cannot_complete_confirmation_or_no_write_restore(setup, pending):
    async def run():
        setup.mode["value"] = "pending" if pending else "confirm"
        await setup.control.execute(action(setup), NOW)
        now = advance_clock(setup, 60)
        ownership = setup.control.state
        state = limit_state(0 if pending else ownership["baseline"]["watts"], readback=now.timestamp())
        state.attributes.pop("gateway_identity_verified")
        setup.states[ENTITY] = state
        audit = deepcopy(setup.store.data["execution_audit"])
        if pending:
            assert await setup.control.reconcile(now) == runtime.GATEWAY_UNVERIFIED
            assert setup.control.state["pending"] == ownership["pending"]
        else:
            assert await setup.control.restore(now) == (OutcomeResult.PENDING, runtime.GATEWAY_UNVERIFIED, False)
            assert setup.control.state["restoring"]
        assert setup.control.state["baseline"] == ownership["baseline"]
        assert setup.store.data["execution_audit"] == audit
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["settled", "pending", "stale", "unverified", "wrong_target", "same_gateway"])
@pytest.mark.parametrize("receipt_saved", [True, False])
def test_explicit_resume_retires_only_a_verified_replaced_gateways_pending_request(
    setup, monkeypatch, replacement, receipt_saved,
):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        await setup.control.execute(action(setup), NOW)
        owned = setup.control.state
        timing_key = runtime.control_key(owned["identity"])
        if not receipt_saved:
            setup.store.data["execution_audit"].clear()
            owned["pending"]["attempt_recorded"] = False
            await setup.control.save(owned)
        now = advance_clock(setup, 60)
        setup.states[ENTITY] = limit_state(
            0, pending=replacement == "pending",
            readback=NOW.timestamp() - 601 if replacement == "stale" else now.timestamp(),
        )
        setup.states[ENTITY].attributes["gateway_identity"] = (
            None if replacement == "unverified" else GATEWAY if replacement == "same_gateway" else "b" * 64
        )
        if replacement == "wrong_target":
            setup.registry[ENTITY].device_id = "different-control"

        monkeypatch.setattr(runtime, "installer_runtime", lambda *_: None)
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store = setup.store
        await owner.async_resume_control(asset=EXPORT_ASSET)
        if replacement == "settled":
            assert not setup.control.state
            assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1
            assert setup.control.cooldown(now)["deferred"]
            assert [row["result"] for row in setup.store.data["execution_audit"]] == [
                OutcomeResult.PENDING, OutcomeResult.FAILED,
            ]
            assert not setup.store.data["calendar_history"].get("history")
        else:
            assert setup.control.state == owned
        assert setup.store.data["command_rate_limits"][timing_key]["attempted_at"] == NOW.timestamp()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("result", ["retired", "still_pending", "refused", "unavailable", "timeout", "remapped"])
@pytest.mark.parametrize("legacy", [False, True])
def test_resume_checks_companion_retirement_before_releasing_original_ownership(setup, monkeypatch, result, legacy):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        replacement = "b" * 64
        original = None if legacy else GATEWAY
        if legacy:
            setup.states[ENTITY].attributes.pop("gateway_identity")
            setup.states[ENTITY].attributes.pop("gateway_identity_verified")
        await setup.control.execute(action(setup), NOW)
        now = advance_clock(setup, 600)
        setup.states[ENTITY] = limit_state(0, pending=True, readback=now.timestamp())
        setup.states[ENTITY].attributes.update(gateway_identity=replacement, request_status="unconfirmed")
        if legacy:
            assert await setup.control.reconcile(now) == "export_limit_unconfirmed"
        ownership = setup.control.state
        budget = action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"]

        async def retire(gateway, *, original_gateway):
            assert gateway == replacement and original_gateway == original
            assert setup.control.state == ownership
            if result == "unavailable":
                raise installer_rejection()
            if result == "timeout":
                raise TimeoutError
            if result == "refused":
                return False
            if result != "still_pending":
                setup.states[ENTITY].attributes["pending"] = False
                setup.states[ENTITY].state = "zero_export"
            if result == "remapped":
                setup.registry[ENTITY].device_id = "another-control"
            return True

        retire_hook = AsyncMock(side_effect=retire)
        monkeypatch.setattr(runtime, "installer_runtime", lambda *_: SimpleNamespace(
            async_retire_replaced_intent=retire_hook,
        ))
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store = setup.store
        await owner.async_resume_control(asset=EXPORT_ASSET)
        retire_hook.assert_awaited_once_with(replacement, original_gateway=original)
        assert setup.control.state == ({} if result == "retired" else ownership)
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == budget == 1
        audit = setup.store.data["execution_audit"]
        assert [row["result"] for row in audit] == (
            [OutcomeResult.PENDING, OutcomeResult.FAILED] if result == "retired" or legacy else [OutcomeResult.PENDING]
        )
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("saved_gateway", [None, GATEWAY])
@pytest.mark.parametrize("restore", [False, True])
def test_pending_gateway_mismatch_never_confirms_and_can_be_retired(setup, saved_gateway, restore):
    async def run():
        if saved_gateway is None:
            setup.states[ENTITY].attributes.pop("gateway_identity")
            setup.states[ENTITY].attributes.pop("gateway_identity_verified")
        await setup.control.execute(action(setup), NOW)
        ownership = setup.control.state
        ownership["pending"]["restore"] = restore
        await setup.control.save(ownership)
        now = advance_clock(setup, 60)
        setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
        setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
        issue = runtime.GATEWAY_CHANGED if saved_gateway else runtime.GATEWAY_UNVERIFIED
        assert await setup.control.reconcile(now) == issue
        assert setup.control.state["pending"]
        if saved_gateway is None:
            await setup.control.resume()
            assert setup.control.state["pending"]
        now = advance_clock(setup, 600)
        assert await setup.control.reconcile(now) == "export_limit_unconfirmed"
        assert await setup.control.reconcile(now) == "export_limit_unconfirmed"
        assert [row["result"] for row in setup.store.data["execution_audit"]] == [
            OutcomeResult.PENDING, OutcomeResult.FAILED,
        ]
        await setup.control.resume()
        assert not setup.control.state
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1
        assert not setup.store.data["calendar_history"].get("history")
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("same_target", [True, False])
@pytest.mark.parametrize("recovery", ["settled", "paused", "no_baseline"])
def test_explicit_resume_retires_ownership_only_for_same_control(setup, same_target, recovery):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        owned = setup.control.state
        assert owned["baseline"]["watts"] == 3000 and not owned.get("pending")
        if recovery != "settled":
            owned.update(pause=runtime.UNAVAILABLE_REJECTION, retry={
                "attempts": 2, "state": "scheduled", "reason": runtime.UNAVAILABLE_REJECTION,
                "next_attempt_at": (NOW + timedelta(minutes=5)).isoformat(),
            })
            if recovery == "no_baseline":
                owned.pop("baseline")
            await setup.control.save(owned)
        timing = deepcopy(setup.store.data["command_rate_limits"])
        now = advance_clock(setup, 60)
        setup.states[ENTITY] = limit_state(5000, readback=now.timestamp())
        setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
        if not same_target:
            setup.registry[ENTITY].device_id = "different-control"

        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store = setup.store
        await owner.async_resume_control(asset=EXPORT_ASSET)
        assert setup.control.state == ({} if same_target else owned)
        assert setup.store.data["command_rate_limits"] == timing
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("gateway", ["b" * 64, None])
def test_no_write_restore_rechecks_gateway_after_reconciliation(setup, gateway):
    async def run():
        control = setup.control
        baseline = {"watts": None, "slew_rate": 100.0}
        await control.save({
            "identity": runtime.target_identity(setup.hass, ENTITY),
            "gateway_identity": GATEWAY, "baseline": baseline, "expected": baseline,
        })
        reconcile = control.reconcile

        async def reconcile_then_change_gateway(now):
            result = await reconcile(now)
            setup.states[ENTITY].attributes["gateway_identity"] = gateway
            return result

        control.reconcile = reconcile_then_change_gateway
        assert await control.restore(NOW) == (
            OutcomeResult.PENDING, runtime.GATEWAY_CHANGED if gateway else runtime.GATEWAY_UNVERIFIED, False,
        )
        assert control.state["baseline"] == baseline
        assert control.state["gateway_identity"] == GATEWAY
        assert control.state["restoring"]
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize(("stage", "refresh_readback"), [
    ("mark_restoring", False), ("mark_restoring", True), ("reconcile", False), ("reconcile", True),
])
def test_no_write_restore_checks_readback_age_after_storage_waits(setup, stage, refresh_readback):
    async def run():
        control = setup.control
        baseline = {"watts": None, "slew_rate": 100.0}
        await control.save({
            "identity": runtime.target_identity(setup.hass, ENTITY),
            "gateway_identity": GATEWAY, "baseline": baseline, "expected": baseline,
        })
        save, reconcile = control.save, control.reconcile

        def advance_feedback_clock():
            setup.clock["now"] = NOW + timedelta(minutes=11)
            if refresh_readback:
                setup.states[ENTITY] = limit_state(readback=setup.clock["now"].timestamp())

        async def save_then_wait(state):
            await save(state)
            if stage == "mark_restoring" and state.get("restoring"):
                advance_feedback_clock()

        async def reconcile_then_wait(now):
            result = await reconcile(now)
            if stage == "reconcile":
                advance_feedback_clock()
            return result

        control.save, control.reconcile = save_then_wait, reconcile_then_wait
        result = await control.restore(NOW)
        if refresh_readback:
            assert result == (OutcomeResult.RESTORED, "export_limit_restored", False)
            assert not control.state
        else:
            assert result == (OutcomeResult.PENDING, "export_limit_readback_stale", False)
            assert control.state["baseline"] == baseline
            assert control.state["restoring"]
        assert not setup.calls

    asyncio.run(run())


def test_pending_restore_requires_fresh_readback_after_cooldown_save(setup, monkeypatch):
    async def run():
        control = setup.control
        setup.mode["value"] = "confirm"
        action = build_actions(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))[0]
        await control.execute(action, NOW)
        now = advance_clock(setup)
        setup.mode["value"] = "pending"
        assert await control.restore(now) == (OutcomeResult.PENDING, "export_limit_pending", True)
        setup.states[ENTITY] = limit_state(readback=now.timestamp())
        baseline = control.state["baseline"]
        prepare_cooldown = control.prepare_cooldown

        async def prepare_then_wait(started_at):
            timing = await prepare_cooldown(started_at)
            setup.clock["now"] = now + timedelta(minutes=11)
            return timing

        monkeypatch.setattr(control, "prepare_cooldown", prepare_then_wait)
        assert await control.reconcile(now) == "export_limit_unconfirmed"
        assert control.state["baseline"] == baseline
        assert control.state["pending"]["restore"]
        assert len(setup.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("outer_batch", [False, True])
def test_export_confirmation_persists_timing_outcome_and_ownership_together(
    setup, monkeypatch, restore, outer_batch,
):
    from custom_components.ha_energy_planner import storage

    async def run():
        if restore:
            setup.mode["value"] = "confirm"
            await setup.control.execute(action(setup), NOW)
            sent = advance_clock(setup)
            setup.mode["value"] = "pending"
            assert await setup.control.restore(sent) == (OutcomeResult.PENDING, "export_limit_pending", True)
        else:
            assert await setup.control.execute(action(setup), NOW) == (
                OutcomeResult.PENDING, "export_limit_pending", True,
            )

        writes = []

        async def save(snapshot):
            writes.append(deepcopy(snapshot))

        monkeypatch.setattr(storage, "Store", lambda *_args, **_kwargs: SimpleNamespace(async_save=save))
        store = storage.PlannerStore(setup.hass, "export-confirmation")
        store.data.update(deepcopy(setup.store.data))
        control = runtime.ExportLimitControl(setup.hass, store, DATA)
        prepare = AsyncMock(wraps=control.prepare_cooldown)
        monkeypatch.setattr(control, "prepare_cooldown", prepare)
        pending = control.state["pending"]
        now = advance_clock(setup, 30)
        setup.states[ENTITY] = limit_state(pending["watts"], readback=now.timestamp())

        with store.defer_save() if outer_batch else nullcontext():
            assert await control.reconcile(now) is None
            assert len(writes) == 1

        prepare.assert_awaited_once_with(now)
        saved = writes[0]
        timing = saved["command_rate_limits"][runtime.control_key(pending["identity"])]
        assert timing["confirmed_at"] == now.timestamp()
        assert timing["confirmation_request"] == pending["sent_at"]
        assert saved["execution_audit"][-1]["result"] == (
            OutcomeResult.RESTORED.value if restore else OutcomeResult.APPLIED.value
        )
        assert saved["calendar_history"]["history"][-1]["confirmed_start"] == now.isoformat()
        if restore:
            assert EXPORT_ASSET not in saved["ownership"]
        else:
            ownership = saved["ownership"][EXPORT_ASSET]
            assert "pending" not in ownership
            assert ownership["expected"]["watts"] == pending["watts"]

    asyncio.run(run())


@pytest.mark.parametrize(("change", "issue"), [
    ("none", None),
    ("replacement", runtime.GATEWAY_CHANGED),
    ("unverified", runtime.GATEWAY_UNVERIFIED),
    ("remapped", "export_limit_target_changed"),
    ("stale", "export_limit_readback_stale"),
    ("pending", "export_limit_pending"),
])
def test_pending_restore_revalidates_feedback_after_alias_save(select_setup, change, issue):
    setup = select_setup

    async def run():
        sensor_mapping = {**setup.mapping, CONF_ENPHASE_EXPORT_LIMIT_ENTITY: setup.sensor}
        original = runtime.ExportLimitControl(setup.hass, setup.store, sensor_mapping)
        setup.mode["value"] = "confirm"
        await original.execute(build_actions(context(runtime.evidence(
            setup.hass, sensor_mapping, OPTIONS, NOW,
        )))[0], NOW)
        now = advance_clock(setup)
        setup.mode["value"] = "pending"
        assert await original.restore(now) == (OutcomeResult.PENDING, "export_limit_pending", True)
        owned = original.state
        assert owned["pending"]["restore"]
        audit = deepcopy(setup.store.data["execution_audit"])
        history = deepcopy(setup.store.data["calendar_history"])
        setup.states[setup.sensor] = limit_state(readback=now.timestamp())
        save = setup.store.async_save_ownership

        async def save_alias(ownership):
            await save(ownership)
            state = ownership.get(EXPORT_ASSET)
            if not state or state["identity"]["entity_id"] != setup.select:
                return
            attrs = setup.states[setup.sensor].attributes
            if change == "replacement":
                attrs["gateway_identity"] = "b" * 64
            elif change == "unverified":
                attrs.pop("gateway_identity")
            elif change == "remapped":
                setup.registry[setup.sensor].device_id = setup.registry[setup.select].device_id = "different-control"
            elif change == "stale":
                attrs["last_successful_readback"] = now.timestamp() - 601
            elif change == "pending":
                setup.states[setup.sensor] = limit_state(pending=True, readback=now.timestamp())

        setup.store.async_save_ownership = save_alias
        result = await setup.control.restore(now)
        if issue is None:
            assert result == (OutcomeResult.RESTORED, "export_limit_restored", False)
            assert not setup.control.state
        else:
            assert result == (OutcomeResult.PENDING, issue, False)
            for key in ("baseline", "gateway_identity", "pending"):
                assert setup.control.state[key] == owned[key]
            assert setup.store.data["execution_audit"] == audit
            assert setup.store.data["calendar_history"] == history
        assert len(setup.calls) == 2

    asyncio.run(run())


def test_retiring_guarded_runtime_defers_without_operator_pause(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        upstream = retry_owner.upstream

        async def retire(*args, **kwargs):
            upstream.stopped = True
            raise installer_rejection("export_limit_disabled")

        apply = upstream.async_apply_guarded.side_effect
        upstream.async_apply_guarded.side_effect = retire
        now = advance_clock(setup)
        await owner._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert (retry["state"], retry["attempts"], retry["reason"]) == (
            "scheduled", 0, "export_limit_target_changed",
        )
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        replacement = SimpleNamespace(
            enabled=True, pending=None, async_prepare=AsyncMock(side_effect=retry_owner.prepare),
            async_apply_guarded=AsyncMock(side_effect=apply),
            verifies_export_limit_gateway=True,
        )
        retry_owner.entry.runtime_data.coordinator.export_limit_runtime = replacement
        setup.mode["value"] = "confirm"
        advance_clock(setup)
        await owner._async_retry_export_limit()
        replacement.async_apply_guarded.assert_awaited_once()
        assert len(setup.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize("entry_state", [ConfigEntryState.UNLOAD_IN_PROGRESS, ConfigEntryState.SETUP_IN_PROGRESS])
def test_enphase_reload_preserves_final_retry_before_probe(setup, retry_owner, entry_state):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        retry_owner.entry.state = entry_state
        now = advance_clock(setup)
        await owner._async_retry_export_limit()
        assert setup.control.state["retry"] == {
            "state": "scheduled", "attempts": 2, "reason": "export_limit_target_changed",
            "next_attempt_at": (now + runtime.RETRY_INTERVAL).isoformat(),
        }
        retry_owner.upstream.async_prepare.assert_not_awaited()
        retry_owner.entry.state = ConfigEntryState.LOADED
        setup.mode["value"] = "confirm"
        advance_clock(setup)
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_awaited_once()
        assert len(setup.calls) == 2

    asyncio.run(run())


@pytest.fixture
def retry_owner(setup, monkeypatch):
    """Use real Planner control/dispatch with a site-specific fake Enphase runtime."""
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    timers = []

    def call_later(hass, delay, wake):
        timer = SimpleNamespace(delay=delay, wake=wake, cancel=Mock())
        timers.append(timer)
        return timer.cancel

    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.async_call_later", call_later)

    def create(options=None):
        owner = _coordinator_for_runtime_services(
            entry_data=dict(DATA), options={**OPTIONS, CONF_DRY_RUN: False, **(options or {})}, hass=setup.hass,
        )
        owner.store = setup.store
        owner.executor = Executor(
            setup.store, hass=setup.hass, entry_data=owner.entry_data, options=owner.planner_options,
        )
        owner.executor.export_write_blocked = owner._export_dispatch_blocked
        owner._listener_tasks = set()
        setup.hass.async_create_task = asyncio.create_task
        owner._schedule_debounced_refresh = Mock()
        setup.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
            owner.entry_data, owner.planner_options,
        )
        return owner

    async def prepare():
        # The actual Configure preparation hook does this read before fetching the installer form.
        state = setup.states[ENTITY]
        state.attributes["last_successful_readback"] = setup.clock["now"].timestamp()
        watts = state.attributes["confirmed_watts"]
        return SimpleNamespace(
            supported=True, enabled=watts is not None, watts=watts,
            slew=state.attributes["slew_rate"], gateway_identity=state.attributes["gateway_identity"],
        )

    upstream = SimpleNamespace(
        async_prepare=AsyncMock(side_effect=prepare), enabled=True, pending=None, verifies_export_limit_gateway=True,
    )
    upstream_entry = SimpleNamespace(domain="enphase_ev", state=ConfigEntryState.LOADED, disabled_by=None,
                                     runtime_data=SimpleNamespace(coordinator=SimpleNamespace(
                                         export_limit_runtime=upstream,
                                     )))
    setup.hass.config_entries = SimpleNamespace(async_get_entry={"site-a": upstream_entry}.get)
    service = setup.hass.services.async_call

    async def call(domain, name, data, **kwargs):
        if setup.mode["value"] in {"temporary", "expired", "ambiguous"}:
            setup.calls.append((domain, name, data))
            if setup.mode["value"] == "temporary":
                raise installer_rejection(cause=TimeoutError())
            if setup.mode["value"] == "expired":
                raise installer_rejection("export_limit_session_expired")
            raise installer_rejection()
        await service(domain, name, data, **kwargs)

    setup.hass.services.async_call = call

    async def apply(watts, *, expected, before_write):
        assert expected is not None
        before_write()
        service_data = {"entity_id": ENTITY}
        if watts is not None:
            service_data["limit_watts"] = watts
        await call("enphase_ev", "disable_export_limit" if watts is None else "set_export_limit", service_data)

    upstream.async_apply_guarded = AsyncMock(side_effect=apply)
    setup.mode["value"] = "temporary"
    return SimpleNamespace(create=create, upstream=upstream, entry=upstream_entry, prepare=prepare, timers=timers)


@pytest.mark.parametrize("scenario", [
    "negative", "changed_tariff", "external_match", "changed_baseline", "pending",
    "timeout", "repeat", "expired", "ambiguous", "action_cap", "action_cap_late", "action_cap_noop",
])
def test_export_retry_replans_and_only_confirms_from_matching_feedback(setup, retry_owner, scenario):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    async def run():
        options = {CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS: 1} if scenario in {"action_cap", "action_cap_noop"} else {}
        owner = retry_owner.create(options)
        # Keep the original restoration baseline across attempts.
        if scenario not in {"changed_tariff", "changed_baseline", "action_cap_noop"}:
            setup.states[ENTITY] = limit_state(5000)
        if scenario != "changed_baseline":
            await owner._async_reconcile_export_limit_locked()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        initial = setup.control.state["baseline"]
        assert export_limit_status(owner)["retry"]["attempts"] == 0
        now = advance_clock(setup)
        if scenario in {"changed_tariff", "action_cap_noop"}:
            setup.states["sensor.export_price"] = price_state((0.1, 0.0))
        if scenario == "external_match":
            setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
        if scenario == "changed_baseline":
            setup.states[ENTITY] = limit_state(3500, readback=now.timestamp())
            initial = {"watts": 3500, "slew_rate": 100.0}
        setup.mode["value"] = {
            "repeat": "temporary", "pending": "pending", "timeout": "timeout",
            "expired": "expired", "ambiguous": "ambiguous",
        }.get(scenario, "confirm")
        if scenario == "action_cap_late":
            setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2

            async def close_allowance():
                owner.entry.options[CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS] = 1
                return await retry_owner.prepare()

            retry_owner.upstream.async_prepare.side_effect = close_allowance
        await owner._async_retry_export_limit()
        checks = int(scenario != "action_cap")
        assert retry_owner.upstream.async_prepare.await_count == checks

        if scenario in {"changed_tariff", "external_match", "action_cap", "action_cap_late", "action_cap_noop"}:
            assert len(setup.calls) == 1
            if scenario in {"action_cap", "action_cap_late"}:
                attempts = 2 if scenario == "action_cap_late" else 0
                owner._schedule_export_retry()
                wakeup = retry_owner.timers[-1]
                assert wakeup.delay == 86400 - 300
                advance_clock(setup)
                await owner._async_retry_export_limit()
                assert retry_owner.upstream.async_prepare.await_count == checks
                assert setup.control.state["retry"]["attempts"] == attempts
                now = advance_clock(setup, 86400 - 600)
                tariff = price_state(start=now.replace(minute=0))
                tariff.last_updated = now
                setup.states["sensor.export_price"] = tariff
                retry_owner.upstream.async_prepare.side_effect = retry_owner.prepare
                wakeup.wake(now)
                await asyncio.gather(*owner._listener_tasks)
                assert len(setup.calls) == 2 and "retry" not in setup.control.state
            else:
                assert "retry" not in setup.control.state
                assert "pause" not in setup.control.state
        else:
            assert len(setup.calls) == 2
            assert setup.calls[-1] == ("enphase_ev", "set_export_limit", {"entity_id": ENTITY, "limit_watts": 0})
            assert setup.control.state["baseline"] == initial
            if scenario == "repeat":
                assert setup.control.state["retry"]["attempts"] == 1
                for _ in range(2):
                    advance_clock(setup)
                    await owner._async_retry_export_limit()
                assert export_limit_status(owner)["retry"]["state"] == "operator_required"
                assert export_limit_status(owner)["retry"]["operator_action"]
                assert setup.control.state["pause"] == "export_limit_retry_exhausted"
                advance_clock(setup)
                await owner._async_retry_export_limit()
                assert len(setup.calls) == 4
                await setup.control.resume()
                assert "retry" not in setup.control.state
            elif scenario in {"expired", "ambiguous"}:
                assert export_limit_status(owner)["retry"]["state"] == "operator_required"
            elif scenario in {"negative", "changed_baseline"}:
                assert setup.control.state["expected"]["watts"] == 0
                assert "retry" not in setup.control.state
                assert setup.store.data["execution_audit"][-1]["result"] == "applied"
            else:
                assert export_limit_status(owner)["retry"]["state"] == "awaiting_confirmation"
                advance_clock(setup)
                await owner._async_retry_export_limit()
                assert len(setup.calls) == 2
                setup.states[ENTITY] = limit_state(0, readback=setup.clock["now"].timestamp())
                await owner._async_reconcile_export_limit_locked()
                assert "retry" not in setup.control.state

    asyncio.run(run())


@pytest.mark.parametrize("changes_during_claim", [False, True])
def test_companion_capability_is_checked_before_charging_final_retry(setup, retry_owner, changes_during_claim):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        state = setup.control.state
        await setup.control.save({**state, "retry": {**state["retry"], "attempts": 2}})
        advance_clock(setup)
        saved_retries = []
        save = setup.store.async_save_ownership
        upstream = retry_owner.upstream
        upstream.verifies_export_limit_gateway = changes_during_claim

        async def record_save(ownership):
            await save(ownership)
            retry = ownership[EXPORT_ASSET]["retry"]
            saved_retries.append(retry)
            if retry["state"] == "checking":
                upstream.verifies_export_limit_gateway = False

        setup.store.async_save_ownership = record_save
        await owner._async_retry_export_limit()
        assert [retry["state"] for retry in saved_retries] == (
            ["checking", "operator_required"] if changes_during_claim else ["operator_required"]
        )
        assert setup.control.state["retry"]["attempts"] == 2
        assert setup.control.state["pause"] == runtime.COMPANION_UPGRADE_REQUIRED
        upstream.async_prepare.assert_not_awaited()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(("scenario", "reason", "terminal"), [
    ("missing", runtime.COMPANION_UPGRADE_REQUIRED, True),
    ("legacy_write", runtime.COMPANION_UPGRADE_REQUIRED, True),
    ("legacy_guarded", runtime.COMPANION_UPGRADE_REQUIRED, True),
    ("wrong_entry", "export_limit_retry_readiness_check_unavailable", False),
    ("entry_unloaded", "export_limit_retry_readiness_check_unavailable", False),
    ("entry_disabled", "export_limit_retry_readiness_check_unavailable", False),
    ("other_domain", "export_limit_retry_readiness_check_unavailable", False),
    ("unsupported", "export_limit_unsupported", True),
    ("disabled", "export_limit_service_rejected:controls_disabled", True),
    ("expired", "export_limit_service_rejected:installer_session_expired", True),
    ("upstream_off", "export_limit_service_rejected:controls_disabled", True),
    ("ambiguous", "export_limit_retry_readiness_rejected", True),
    ("error", "export_limit_retry_readiness_check_failed", True),
    ("unknown_shape", "export_limit_retry_readiness_check_unavailable", True),
    ("timeout", "export_limit_retry_readiness_timeout", False),
    ("temporary", runtime.UNAVAILABLE_REJECTION, False),
    ("pending_before", "export_limit_pending", False),
    ("pending_during", "export_limit_pending", False),
    ("stale_settings", "export_limit_readback_stale", False),
    ("stale_tariff", "export_tariff_stale", False),
    ("different_settings", "export_limit_retry_readback_changed", False),
])
def test_export_retry_needs_installer_and_fresh_settled_policy_evidence(setup, retry_owner, scenario, reason, terminal):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        advance_clock(setup)
        upstream = retry_owner.upstream
        if scenario == "missing":
            del upstream.async_prepare
        elif scenario == "legacy_write":
            upstream.async_apply = AsyncMock()
            del upstream.async_apply_guarded
        elif scenario == "legacy_guarded":
            del upstream.verifies_export_limit_gateway
        elif scenario == "wrong_entry":
            setup.hass.config_entries.async_get_entry = lambda _: None
        elif scenario == "entry_unloaded":
            retry_owner.entry.state = ConfigEntryState.NOT_LOADED
        elif scenario == "entry_disabled":
            retry_owner.entry.disabled_by = "user"
        elif scenario == "other_domain":
            retry_owner.entry.domain = "other"
        elif scenario == "unsupported":
            upstream.async_prepare = AsyncMock(return_value=SimpleNamespace(supported=False))
        elif scenario == "disabled":
            upstream.enabled = False
        elif scenario == "unknown_shape":
            upstream.async_prepare = AsyncMock(return_value=SimpleNamespace(supported=True, enabled="no"))
        elif scenario in {"timeout", "temporary", "expired", "upstream_off", "ambiguous", "error"}:
            upstream.async_prepare.side_effect = {
                "timeout": TimeoutError(),
                "temporary": installer_rejection(cause=TimeoutError()),
                "expired": installer_rejection("export_limit_session_expired"),
                "upstream_off": installer_rejection("export_limit_disabled"),
                "ambiguous": installer_rejection(),
                "error": RuntimeError(),
            }[scenario]
        elif scenario == "pending_before":
            setup.states[ENTITY] = limit_state(pending=True, readback=setup.clock["now"].timestamp())
        elif scenario == "pending_during":
            upstream.pending = {}
        elif scenario == "stale_settings":
            setup.states[ENTITY] = limit_state(readback=NOW.timestamp())
            upstream.async_prepare = AsyncMock(return_value=SimpleNamespace(
                supported=True, enabled=False, slew=100.0, gateway_identity=GATEWAY,
            ))
        elif scenario == "stale_tariff":
            setup.states["sensor.export_price"].last_updated = NOW - timedelta(hours=1)
        else:
            upstream.async_prepare = AsyncMock(
                return_value=SimpleNamespace(
                    supported=True, enabled=True, watts=1, slew=100.0, gateway_identity=GATEWAY,
                ),
            )
        setup.mode["value"] = "confirm"
        await owner._async_retry_export_limit()
        assert len(setup.calls) == 1
        retry = setup.control.state["retry"]
        free_check = reason == runtime.COMPANION_UPGRADE_REQUIRED or scenario in {
            "pending_before", "pending_during", "different_settings", "stale_tariff",
        }
        assert retry["attempts"] == (0 if free_check else 1)
        assert retry["reason"] == reason
        assert retry["state"] == ("operator_required" if terminal else "scheduled")
        if scenario == "legacy_write":
            upstream.async_prepare.assert_not_called()
            upstream.async_apply.assert_not_called()
        if scenario == "legacy_guarded":
            upstream.async_prepare.assert_not_called()
            upstream.async_apply_guarded.assert_not_called()
            status = runtime.retry_status(setup.control.state, running=False)
            assert status["operator_action"].startswith("Upgrade Enphase Energy")
        if scenario == "entry_unloaded":
            retry_owner.entry.state = ConfigEntryState.LOADED
            advance_clock(setup)
            await owner._async_retry_export_limit()
            assert len(setup.calls) == 2
        if scenario == "pending_before":
            for _ in range(3):
                advance_clock(setup)
                await owner._async_retry_export_limit()
            upstream.async_prepare.assert_not_awaited()
            assert setup.control.state["retry"]["attempts"] == 0
            now = advance_clock(setup)
            setup.states[ENTITY] = limit_state(readback=now.timestamp())
            tariff = price_state((-0.1, -0.1), start=now.replace(minute=0))
            tariff.last_updated = now
            setup.states["sensor.export_price"] = tariff
            await owner._async_retry_export_limit()
            assert len(setup.calls) == 2
        if scenario == "timeout":
            for _ in range(2):
                advance_clock(setup)
                await owner._async_retry_export_limit()
            assert setup.control.state["pause"] == "export_limit_retry_exhausted"
            assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("scenario", [
    "stop", "pause", "disable", "replace", "missing_target", "unavailable", "missing_readback",
    "reload", "reload_rejected", "resume", "superseded", "overlap", "shutdown",
])
def test_export_retry_rechecks_authority_after_probe(setup, retry_owner, scenario):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        if scenario in {"reload_rejected", "replace", "missing_target", "unavailable", "missing_readback"}:
            setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        await owner._async_reconcile_export_limit_locked()
        advance_clock(setup)
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare():
            assert not owner._command_lock.locked()
            entered.set()
            await release.wait()
            if scenario == "reload_rejected":
                raise installer_rejection("export_limit_disabled")
            if scenario == "missing_readback":
                return SimpleNamespace(supported=True, enabled=False, slew=100.0, watts=None, gateway_identity=GATEWAY)
            return await retry_owner.prepare()

        retry_owner.upstream.async_prepare.side_effect = prepare
        setup.mode["value"] = "confirm"
        retry_owner.timers[-1].wake(setup.clock["now"])
        await entered.wait()
        retry, = owner._listener_tasks
        assert export_limit_status(owner)["retry"]["state"] == "checking"
        await owner._async_retry_export_limit()
        async with owner._command_lock:
            await owner._async_reconcile_export_limit_locked()
        if scenario == "stop":
            setup.store.data["production"]["armed"] = False
        elif scenario == "pause":
            await owner.async_pause_control(15, "operator", asset=EXPORT_ASSET)
        elif scenario == "disable":
            owner.entry.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED] = False
        elif scenario == "replace":
            setup.registry[ENTITY].device_id = "new-gateway"
        elif scenario == "missing_target":
            old_target = setup.registry.pop(ENTITY)
        elif scenario == "unavailable":
            setup.states[ENTITY].state = "unavailable"
        elif scenario == "missing_readback":
            setup.states[ENTITY].attributes.pop("last_successful_readback")
        elif scenario == "resume":
            await owner.async_resume_control(asset=EXPORT_ASSET)
        elif scenario == "superseded":
            owned = setup.control.state
            replacement_retry = {**owned["retry"], "state": "scheduled", "reason": "export_limit_pending"}
            await setup.control.save({**owned, "retry": replacement_retry})
        elif scenario in {"reload", "reload_rejected"}:
            retry_owner.entry.runtime_data.coordinator.export_limit_runtime = SimpleNamespace(
                enabled=True, pending=None, async_prepare=AsyncMock(side_effect=retry_owner.prepare),
                async_apply_guarded=retry_owner.upstream.async_apply_guarded,
                verifies_export_limit_gateway=True,
            )
        elif scenario == "shutdown":
            owner._begin_shutdown()
        await owner._async_retry_export_limit()
        release.set()
        await retry
        assert len(setup.calls) == (2 if scenario == "overlap" else 1)
        retry_owner.upstream.async_prepare.assert_awaited_once()
        if scenario == "resume":
            assert "retry" not in setup.control.state and "pause" not in setup.control.state
        if scenario == "superseded":
            assert setup.control.state["retry"] == replacement_retry
            retry_owner.upstream.async_apply_guarded.assert_not_awaited()
        if scenario in {"reload_rejected", "replace", "missing_target", "unavailable", "missing_readback"}:
            assert setup.control.state["retry"]["reason"] == (
                "export_limit_entity_unavailable" if scenario in {"unavailable", "missing_readback"}
                else "export_limit_target_changed"
            )
            assert setup.control.state["retry"]["state"] == "scheduled"
            assert setup.control.state["retry"]["attempts"] == 2
            if scenario == "replace":
                setup.registry[ENTITY].device_id = "gateway-a"
            elif scenario == "missing_target":
                setup.registry[ENTITY] = old_target
            elif scenario in {"unavailable", "missing_readback"}:
                setup.states[ENTITY] = limit_state(readback=setup.clock["now"].timestamp())
            advance_clock(setup)
            await owner._async_retry_export_limit()
            assert len(setup.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize(("change", "reason"), [
    ("disable_planner", "export_limit_retry_interrupted"),
    ("disarm", "export_limit_retry_interrupted"),
    ("disable_enphase", "export_limit_upstream_controls_disabled"),
    ("external", "external_export_limit_changed"),
    ("reload_enphase", "export_limit_target_changed"),
    ("reload_enphase_unavailable", "export_limit_target_changed"),
    ("tariff_changed", "export_tariff_changed"),
    ("tariff_expired", "export_tariff_expired"),
    ("gateway_expired", runtime.GATEWAY_CHANGED),
    ("verification_expired", runtime.GATEWAY_UNVERIFIED),
    ("upstream_pending", "export_limit_pending"),
    ("readback_regressed", "export_limit_retry_readback_changed"),
])
def test_export_retry_rechecks_stop_and_feedback_after_dispatch_reservation(setup, retry_owner, change, reason):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        deferred = {"tariff_changed", "tariff_expired", "external", "reload_enphase", "reload_enphase_unavailable"}
        if change in deferred:
            setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        advance_clock(setup)
        setup.mode["value"] = "confirm"
        flush = setup.store.async_flush

        async def flush_with_change():
            await flush()
            if not setup.control.state.get("pending"):
                return
            if change == "disable_planner":
                owner.entry.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED] = False
            elif change == "disarm":
                setup.store.data["production"]["armed"] = False
            elif change == "disable_enphase":
                retry_owner.upstream.enabled = False
            elif change in {"reload_enphase", "reload_enphase_unavailable"}:
                retry_owner.entry.state = ConfigEntryState.NOT_LOADED
                if change == "reload_enphase_unavailable":
                    setup.states[ENTITY].state = "unavailable"
            elif change == "tariff_changed":
                setup.states["sensor.export_price"] = price_state((-0.2, -0.1))
            elif change.endswith("_expired"):
                advance_clock(setup, 900)
                if change == "gateway_expired":
                    setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
                elif change == "verification_expired":
                    setup.states[ENTITY].attributes["gateway_identity_verified"] = False
            elif change == "upstream_pending":
                retry_owner.upstream.pending = {"watts": 5000}
            elif change == "readback_regressed":
                setup.states[ENTITY].attributes["last_successful_readback"] = NOW.timestamp()
            else:
                setup.states[ENTITY] = limit_state(5000, readback=setup.clock["now"].timestamp())

        setup.store.async_flush = flush_with_change
        await owner._async_retry_export_limit()
        assert len(setup.calls) == 1
        assert setup.control.state["retry"]["reason"] == reason
        assert not setup.control.state.get("pending")
        assert setup.control.cooldown(setup.clock["now"])["last_dispatch_reserved_at"] == NOW.timestamp()
        assert action_budget(budget_history(setup.store.data), {}, setup.clock["now"], EXPORT_ASSET)["used"] == 1
        if change in deferred:
            assert setup.control.state["retry"]["attempts"] == 2
            assert setup.control.state["retry"]["state"] == "scheduled"
        if change in {"gateway_expired", "verification_expired"}:
            assert setup.control.state["retry"]["state"] == "operator_required"
            assert setup.control.state["retry"]["attempts"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("startup_ready", [False, True])
def test_retry_does_not_charge_a_competing_provider_request(setup, retry_owner, startup_ready):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owned = setup.control.state
        owned["retry"]["attempts"] = 3 if startup_ready else 2
        if startup_ready:
            owned["retry"].update(state="ready", next_attempt_at=None)
            owned.pop("pause")
        await setup.control.save(owned)
        retry_owner.upstream.async_apply_guarded.side_effect = installer_rejection("export_limit_pending")
        await owner._async_retry_export_limit()

        state = setup.control.state
        assert not state.get("pending")
        assert state["retry"]["attempts"] == (3 if startup_ready else 2)
        assert state["retry"]["state"] == ("ready" if startup_ready else "scheduled")
        assert state["retry"]["reason"] == "export_limit_pending"
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_export_retry_leaves_legacy_pauses_and_reconciles_new_terminal_feedback(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        await owner._async_reconcile_export_limit_locked()
        timer = retry_owner.timers[-1]
        original = setup.control.state
        await setup.control.save({key: value for key, value in original.items() if key != "retry"})
        owner._schedule_export_retry()
        timer.cancel.assert_called_once()
        assert owner._export_retry_at is None
        now = advance_clock(setup)
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_not_called()
        await setup.control.save(original)
        claim = await setup.control.claim_retry(now)
        setup.states[ENTITY].state = "unsupported"
        assert (await setup.control.execute(action_before, now, retry=claim))[1] == "export_limit_unsupported"
        await setup.control.finish_retry(claim, None)
        assert setup.control.state["retry"]["state"] == "operator_required"
        assert setup.control.state["pause"] == "export_limit_unsupported"
        assert len(setup.calls) == 1

    action_before = action(setup)
    asyncio.run(run())


@pytest.mark.parametrize("change", ["pause", "disarm", "pending"])
def test_retry_rechecks_admission_after_persisting_claim(setup, retry_owner, change):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        now = advance_clock(setup)
        flush = setup.store.async_flush

        async def change_during_flush():
            await flush()
            if setup.control.state["retry"]["state"] == "checking":
                if change == "pending":
                    setup.states[ENTITY] = limit_state(pending=True, readback=now.timestamp())
                elif change == "disarm":
                    setup.store.data["production"]["armed"] = False
                else:
                    await owner.async_pause_control(15, "operator", asset=EXPORT_ASSET)

        setup.store.async_flush = change_during_flush
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_not_awaited()
        retry = setup.control.state["retry"]
        assert retry["state"] == "scheduled"
        assert retry["attempts"] == 2
        expected_reason = "export_limit_pending" if change == "pending" else "export_limit_retry_interrupted"
        assert retry["reason"] == expected_reason
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_retry_refunds_final_check_when_runtime_reloads_during_reconciliation(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        advance_clock(setup)
        save_ownership = setup.store.async_save_ownership
        prepare = retry_owner.prepare

        async def prepare_with_external_change():
            # Reconciliation adopts this new baseline and persists it.
            setup.states[ENTITY] = limit_state(5000)
            return await prepare()

        async def reload_during_adoption(ownership):
            await save_ownership(ownership)
            if ownership[EXPORT_ASSET].get("expected", {}).get("watts") == 5000:
                retry_owner.entry.state = ConfigEntryState.NOT_LOADED
                setup.states[ENTITY].state = "unavailable"

        retry_owner.upstream.async_prepare.side_effect = prepare_with_external_change
        setup.store.async_save_ownership = reload_during_adoption
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_awaited_once()
        retry_owner.upstream.async_apply_guarded.assert_not_called()
        assert setup.control.state["retry"]["attempts"] == 2
        assert setup.control.state["retry"]["reason"] == "export_limit_target_changed"

    asyncio.run(run())


def test_unsupported_replacement_feedback_does_not_pause_owned_gateway(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        original = setup.control.state
        setup.registry[ENTITY].device_id = "replacement"
        setup.states[ENTITY].state = "unsupported"
        await owner._async_reconcile_export_limit_locked()
        assert setup.control.state == original

        setup.registry[ENTITY].device_id = original["identity"]["device_id"]
        setup.states[ENTITY] = limit_state(readback=now.timestamp())
        setup.mode["value"] = "confirm"
        await owner._async_retry_export_limit()
        assert len(setup.calls) == 2
        assert "retry" not in setup.control.state

    asyncio.run(run())


def test_unsupported_feedback_retires_startup_retry_readiness(setup, retry_owner):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owner._startup_auto_recovery_authorized = True
        setup.store.data["production"]["armed"] = False
        await owner._async_retry_export_limit()
        assert export_limit_status(owner)["retry"]["state"] == "ready"

        setup.states[ENTITY].state = "unsupported"
        assert await setup.control.reconcile(now) == "export_limit_unsupported"
        status = export_limit_status(owner)
        assert status["retry"]["state"] == "operator_required"
        assert status["retry"]["reason"] == "export_limit_unsupported"
        assert status["retry"]["attempts"] == 1
        assert status["retry"]["next_attempt_at"] is None
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_retry_waits_five_minutes_after_a_slow_failed_probe(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        advance_clock(setup)
        flush = setup.store.async_flush

        async def slow_flush():
            await flush()
            if setup.control.state["retry"]["state"] == "checking":
                advance_clock(setup, 240)

        setup.store.async_flush = slow_flush

        async def failed_probe():
            advance_clock(setup, 25)
            raise TimeoutError

        retry_owner.upstream.async_prepare.side_effect = failed_probe
        await owner._async_retry_export_limit()
        failed_at = setup.clock["now"]
        owner._schedule_export_retry()
        assert retry_owner.timers[-1].delay == 300
        assert setup.control.state["retry"]["next_attempt_at"] == (failed_at + timedelta(minutes=5)).isoformat()
        advance_clock(setup, 299)
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_awaited_once()
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_export_retry_does_not_outlive_an_unrecorded_action_attempt(setup, retry_owner):
    async def run():
        options = {CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS: 1}
        owner = retry_owner.create(options)
        saved = None
        save = setup.store.async_save_ownership

        async def capture_first_saved_retry(ownership):
            nonlocal saved
            await save(ownership)
            if saved is None and ownership.get(EXPORT_ASSET, {}).get("retry"):
                saved = deepcopy(setup.store.data)

        setup.store.async_save_ownership = capture_first_saved_retry
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        assert saved is not None
        setup.store.data = saved
        owner = retry_owner.create(options)
        advance_clock(setup)
        setup.mode["value"] = "confirm"
        await owner._async_retry_export_limit()
        assert len(setup.calls) == 1
        retry_owner.upstream.async_prepare.assert_not_called()
        assert owner._export_retry_control().retry_deadline(setup.clock["now"]) == NOW + timedelta(hours=24)

    asyncio.run(run())


@pytest.mark.parametrize("accepted", [True, False, None])
def test_export_retry_accounting_survives_every_durable_dispatch_generation(setup, retry_owner, monkeypatch, accepted):
    from custom_components.ha_energy_planner import storage

    snapshots = []

    class Disk:
        def __init__(self, *args, **kwargs):
            pass

        async def async_save(self, data):
            snapshots.append(deepcopy(data))

        async def async_load(self):
            return snapshots[-1]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return setup.clock["now"]

    monkeypatch.setattr(storage, "datetime", Clock)
    monkeypatch.setattr(storage, "Store", Disk)

    async def run():
        owner = retry_owner.create({CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS: 2})
        store = storage.PlannerStore(setup.hass)
        store.data["production"] = setup.store.data["production"]
        owner.store = owner.executor.store = store
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        setup.mode["value"] = "confirm" if accepted else "temporary"
        if accepted is None:
            state = owner.store.data["ownership"][EXPORT_ASSET]
            state["retry"] = {**state["retry"], "attempts": 2}
        snapshot_at_dispatch = None
        apply = retry_owner.upstream.async_apply_guarded.side_effect

        async def capture_dispatch(*args, **kwargs):
            nonlocal snapshot_at_dispatch
            snapshot_at_dispatch = snapshots[-1]
            assert snapshot_at_dispatch["ownership"][EXPORT_ASSET]["pending"]["attempt_recorded"] is False
            setup.store.flushes = 1
            if accepted is None:
                raise TimeoutError
            return await apply(*args, **kwargs)

        retry_owner.upstream.async_apply_guarded.side_effect = capture_dispatch
        await owner._async_retry_export_limit()
        assert snapshot_at_dispatch is not None
        dispatch_index = snapshots.index(snapshot_at_dispatch)
        for saved in snapshots:
            owned = saved.get("ownership", {}).get(EXPORT_ASSET, {})
            if owned.get("pending"):
                assert not any(
                    row.get("attempted_at") == now.isoformat()
                    and row.get("reason") == runtime.UNAVAILABLE_REJECTION
                    for row in budget_history(saved)
                )
        if accepted is None:
            rolled_back = snapshots[dispatch_index + 1]
            owned = rolled_back["ownership"][EXPORT_ASSET]
            assert "pending" not in owned
            assert owned["retry"]["attempts"] == 2
            assert owned["retry"]["state"] == "scheduled"
            assert action_budget(budget_history(rolled_back), owner.options, now, EXPORT_ASSET)["used"] == 1
            return
        assert action_budget(budget_history(snapshots[-1]), owner.options, now, EXPORT_ASSET)["used"] == 2
        if not accepted:
            assert not snapshots[-1]["ownership"][EXPORT_ASSET].get("pending")
            return

        snapshots.append(snapshot_at_dispatch)
        restarted = storage.PlannerStore(setup.hass)
        await restarted.async_load()
        restarted_control = runtime.ExportLimitControl(setup.hass, restarted, DATA, options=owner.options)
        assert restarted_control.state["pending"]["attempt_recorded"] is False
        assert await restarted_control.reconcile(now) is None
        assert await restarted_control.reconcile(now) is None
        assert action_budget(budget_history(restarted.data), owner.options, now, EXPORT_ASSET)["used"] == 2
        assert restarted_control.state["expected"]["watts"] == 0

    asyncio.run(run())


def test_unsent_final_retry_rolls_back_timing_and_attempt_in_one_generation(setup, retry_owner, monkeypatch):
    from custom_components.ha_energy_planner import storage

    snapshots = []
    fail_after_denial = False

    class Disk:
        def __init__(self, *args, **kwargs):
            pass

        async def async_save(self, data):
            snapshot = deepcopy(data)
            snapshots.append(snapshot)
            if fail_after_denial:
                # A backend can fail after writing a generation. Restart from
                # that generation and check the first durable rollback is complete.
                raise OSError("simulated failure after saving rollback")

        async def async_load(self):
            return deepcopy(snapshots[-1])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return setup.clock["now"]

    monkeypatch.setattr(storage, "Store", Disk)
    monkeypatch.setattr(storage, "datetime", Clock)

    async def run():
        nonlocal fail_after_denial
        owner = retry_owner.create()
        store = storage.PlannerStore(setup.hass)
        store.data["production"] = setup.store.data["production"]
        owner.store = owner.executor.store = store
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        control = runtime.ExportLimitControl(setup.hass, store, DATA, options=owner.options)
        state = control.state
        state["retry"] = {**state["retry"], "attempts": 2}
        await control.save(state)
        now = advance_clock(setup)

        async def reject(*args, **kwargs):
            nonlocal fail_after_denial
            assert snapshots[-1]["ownership"][EXPORT_ASSET]["retry"]["attempts"] == 3
            fail_after_denial = True
            raise runtime.RetryWriteDeferred("export_tariff_changed")

        retry_owner.upstream.async_apply_guarded.side_effect = reject
        with pytest.raises(OSError, match="saving rollback"):
            await owner._async_retry_export_limit()
        fail_after_denial = False
        restarted = storage.PlannerStore(setup.hass)
        await restarted.async_load()
        restored = runtime.ExportLimitControl(setup.hass, restarted, DATA, options=owner.options)
        assert "pending" not in restored.state
        assert restored.state["retry"]["state"] == "scheduled"
        assert restored.state["retry"]["attempts"] == 2
        assert restored.retry_deadline(now) == now + runtime.RETRY_INTERVAL
        assert restored.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        assert action_budget(budget_history(restarted.data), {}, now, EXPORT_ASSET)["used"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["reservation_failure", "reservation_cancel", "claim_cancel", "ordinary_cancel"])
def test_reservation_save_requires_acknowledgement_and_drains_on_cancel(
    setup, retry_owner, monkeypatch, tmp_path, stage,
):
    import json
    from pathlib import Path
    from unittest.mock import patch

    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store as HAStore
    from homeassistant.util.file import WriteError

    from custom_components.ha_energy_planner import storage

    snapshots = []
    write_data = HAStore._async_write_data

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return setup.clock["now"]

    monkeypatch.setattr(storage, "datetime", Clock)

    async def run():
        owner = retry_owner.create()
        hass = HomeAssistant(str(tmp_path))
        store = storage.PlannerStore(hass, "export-retry")
        store.data["production"] = setup.store.data["production"]
        owner.store = owner.executor.store = store
        entered, release = asyncio.Event(), asyncio.Event()

        async def intercept(backend, envelope):
            owned = envelope["data"]["ownership"].get(EXPORT_ASSET, {})
            saving_claim = owned.get("retry", {}).get("state") == "checking"
            interrupt_save = saving_claim if stage == "claim_cancel" else bool(owned.get("pending"))
            if interrupt_save:
                if stage == "reservation_failure":
                    raise WriteError("simulated file write failure")
                entered.set()
                await release.wait()
            await write_data(backend, envelope)
            snapshots.append(deepcopy(envelope["data"]))

        try:
            ordinary = stage == "ordinary_cancel"
            if ordinary:
                now = NOW
                operation = owner.executor.async_execute_export_limit(action(setup), now)
            else:
                await owner.executor.async_execute_export_limit(action(setup), NOW)
                store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
                now = advance_clock(setup)
                operation = owner._async_retry_export_limit()
            async with store.async_delay_save():
                with patch.object(HAStore, "_async_write_data", intercept):
                    if stage == "reservation_failure":
                        with pytest.raises(HomeAssistantError, match="storage_write_failed"):
                            await operation
                    else:
                        task = asyncio.create_task(operation)
                        await asyncio.wait_for(entered.wait(), 1)
                        task.cancel()
                        await asyncio.sleep(0)
                        assert not task.done()
                        release.set()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                        reserved = snapshots[-2]["ownership"][EXPORT_ASSET]
                        if stage == "claim_cancel":
                            assert reserved["retry"]["attempts"] == 3
                            assert "pending" not in reserved
                        else:
                            assert reserved["pending"]["sent_at"] == now.timestamp()
                retry_owner.upstream.async_apply_guarded.assert_not_awaited()
                assert len(setup.calls) == (0 if ordinary else 1)
                rolled_back = json.loads(Path(store._store.path).read_text())["data"]
                assert rolled_back == snapshots[-1]
                owned = rolled_back["ownership"].get(EXPORT_ASSET, {})
                assert "pending" not in owned
                identity = runtime.target_identity(setup.hass, ENTITY)
                timing = rolled_back["command_rate_limits"][runtime.control_key(identity)]
                if ordinary:
                    assert not owned
                    assert timing["attempted_at"] is None
                else:
                    assert owned["retry"]["state"] == "scheduled"
                    assert owned["retry"]["attempts"] == 2
                    assert timing["attempted_at"] == NOW.timestamp()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("accepted", [False, True])
def test_guarded_attempt_uses_send_time_for_daily_expiry(setup, retry_owner, accepted):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        reserved_at = advance_clock(setup)
        setup.mode["value"] = "confirm" if accepted else "temporary"
        apply = retry_owner.upstream.async_apply_guarded.side_effect

        async def send_after_connection(*args, **kwargs):
            advance_clock(setup, 25)
            return await apply(*args, **kwargs)

        retry_owner.upstream.async_apply_guarded.side_effect = send_after_connection
        await owner._async_retry_export_limit()
        sent_at = setup.clock["now"]
        assert len(setup.calls) == 2
        assert setup.control.cooldown(sent_at)["last_dispatch_reserved_at"] == sent_at.timestamp()
        history = budget_history(setup.store.data)
        cap = {CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS: 1}
        assert action_budget(history, cap, reserved_at + timedelta(days=1), EXPORT_ASSET)["remaining"] == 0
        assert action_budget(history, cap, sent_at + timedelta(days=1), EXPORT_ASSET)["remaining"] == 1

    asyncio.run(run())


def test_external_change_matching_tariff_settles_retry_during_reservation(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        flush = setup.store.async_flush

        async def confirm_external_change():
            await flush()
            if setup.control.state.get("pending"):
                setup.states[ENTITY] = limit_state(0, readback=now.timestamp())

        setup.store.async_flush = confirm_external_change
        await owner._async_retry_export_limit()
        assert setup.control.state["expected"] == {"watts": 0, "slew_rate": 100.0}
        assert not ({"retry", "pause", "pending", "baseline"} & setup.control.state.keys())
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        assert len(setup.calls) == 1
        retry_owner.upstream.async_apply_guarded.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("matches_tariff,gateway", [(True, GATEWAY), (False, GATEWAY), (True, "b" * 64), (True, None)])
def test_retry_provider_rejects_external_change_after_local_dispatch_guard(setup, retry_owner, matches_tariff, gateway):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        now = advance_clock(setup)
        setup.mode["value"] = "confirm"
        provider = retry_owner.upstream
        waiting = asyncio.Event()
        lock = asyncio.Lock()

        async def apply(watts, *, expected, before_write):
            waiting.set()
            async with lock:
                assert watts == 0 and not expected.enabled
                assert setup.states[ENTITY].attributes["confirmed_watts"] == (0 if matches_tariff else 5000)
                raise installer_rejection("export_limit_changed")

        provider.async_apply_guarded.side_effect = apply
        async with lock:
            task = asyncio.create_task(owner._async_retry_export_limit())
            await asyncio.wait_for(waiting.wait(), 1)
            setup.states[ENTITY] = limit_state(0 if matches_tariff else 5000, readback=now.timestamp())
            setup.states[ENTITY].attributes["gateway_identity"] = gateway
        await task
        assert len(setup.calls) == 1
        assert not setup.control.state.get("pending")
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == NOW.timestamp()
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1
        if gateway != GATEWAY:
            issue = runtime.GATEWAY_CHANGED if gateway else runtime.GATEWAY_UNVERIFIED
            assert setup.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
            assert setup.control.state["gateway_identity"] == GATEWAY
            assert setup.control.state["pause"] == issue
            assert setup.control.state["retry"]["state"] == "operator_required"
            assert setup.control.state["retry"]["attempts"] == 2
        elif matches_tariff:
            assert "retry" not in setup.control.state and "pause" not in setup.control.state
            assert "baseline" not in setup.control.state
        else:
            assert setup.control.state["retry"]["attempts"] == 2
            assert setup.control.state["retry"]["state"] == "scheduled"

    asyncio.run(run())


def test_already_desired_retry_settles_after_external_baseline_is_adopted(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        claim = await setup.control.claim_retry(now)
        setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
        await setup.control.reconcile(now)
        assert setup.control.state["expected"]["watts"] == 0
        assert setup.control.state["retry"] == claim
        await setup.control.finish_unsent_retry(claim, "export_limit_already_desired", now.timestamp())
        assert "retry" not in setup.control.state
        assert "pause" not in setup.control.state
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_legacy_feedback_supports_normal_writes_without_binding_a_retry(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        setup.states[ENTITY].attributes.pop("gateway_identity")
        setup.states[ENTITY].attributes.pop("gateway_identity_verified")
        assert (await owner.executor.async_execute_export_limit(action(setup), NOW))[2]
        now = advance_clock(setup)
        setup.states[ENTITY] = limit_state(3000, readback=now.timestamp())
        setup.states[ENTITY].attributes.pop("gateway_identity")
        setup.states[ENTITY].attributes.pop("gateway_identity_verified")
        await setup.control.reconcile(now)
        assert "gateway_identity" not in setup.control.state
        assert "baseline" not in setup.control.state
        retry_owner.upstream.async_prepare.return_value = SimpleNamespace(
            supported=True, enabled=True, watts=3000, slew=100,
        )
        retry_owner.upstream.async_prepare.side_effect = None
        await owner._async_retry_export_limit()
        assert setup.control.state["pause"] == runtime.GATEWAY_UNVERIFIED
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_startup_retry_rechecks_the_loaded_provider_after_arming(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owner._startup_auto_recovery_authorized = True
        setup.store.data["production"]["armed"] = False
        await owner._async_retry_export_limit()
        assert setup.control.state["retry"]["state"] == "ready"
        old = retry_owner.upstream
        replacement = SimpleNamespace(
            enabled=True, pending=None, async_prepare=AsyncMock(side_effect=retry_owner.prepare),
            async_apply_guarded=AsyncMock(side_effect=old.async_apply_guarded.side_effect),
            verifies_export_limit_gateway=True,
        )
        retry_owner.entry.runtime_data.coordinator.export_limit_runtime = replacement
        setup.mode["value"] = "confirm"
        setup.store.data["production"]["armed"] = True
        assert (await owner.executor.async_execute_export_limit(action(setup), now))[1] == (
            "export_limit_retry_recheck_required"
        )
        replacement.async_apply_guarded.assert_not_called()
        owner._startup_auto_recovery_authorized = False
        await owner._async_retry_export_limit()
        old.async_apply_guarded.assert_not_called()
        replacement.async_prepare.assert_awaited_once()
        replacement.async_apply_guarded.assert_awaited_once()
        assert len(setup.calls) == 2 and "retry" not in setup.control.state

    asyncio.run(run())


def test_ready_retry_waits_without_probing_until_export_gate_opens(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        ownership = setup.store.data["ownership"][EXPORT_ASSET]
        ownership.pop("pause")
        ownership["retry"].update(state="ready", next_attempt_at=None, attempts=1)
        owner.store.data["production"]["export_operator_arm_revalidate"] = "completed"
        options = owner.planner_options
        owner.data = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, DATA, options, now)))
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": now, "generation": 0}

        owner._schedule_export_retry()
        await owner._async_retry_export_limit()
        assert not retry_owner.timers
        retry_owner.upstream.async_prepare.assert_not_awaited()
        assert ownership["retry"]["attempts"] == 1

        owner._admit_export_after_restart(owner.data)
        owner._schedule_export_retry()
        assert len(retry_owner.timers) == 1
        assert not owner._export_write_blocked()

    asyncio.run(run())


def test_probe_finishing_during_startup_activation_waits_for_final_preflight(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owner._startup_auto_recovery_authorized = True
        setup.store.data["production"]["armed"] = False
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare():
            entered.set()
            await release.wait()
            return await retry_owner.prepare()

        retry_owner.upstream.async_prepare.side_effect = prepare
        task = asyncio.create_task(owner._async_retry_export_limit())
        await asyncio.wait_for(entered.wait(), 1)
        await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        release.set()
        await task
        retry_owner.upstream.async_apply_guarded.assert_not_called()
        saved = setup.control.state["retry"]
        assert saved["state"] == "ready" and saved["attempts"] == 1
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_awaited_once()

        # Finishing startup makes the held write eligible on the same attempt.
        await owner._async_update_startup_auto_recovery("recovered", successful_runs=1, reason="test", completed=True)
        owner._export_startup_activation_pending = False
        owner._schedule_export_retry()
        setup.mode["value"] = "confirm"
        retry_owner.timers[-1].wake(now)
        await asyncio.gather(*owner._listener_tasks)
        retry_owner.upstream.async_apply_guarded.assert_awaited_once()
        assert setup.control.state["expected"]["watts"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("change", ["unrelated", "different_target", "disabled", "exhausted"])
def test_configuration_reload_preserves_only_same_enabled_export_retry(setup, retry_owner, change):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        if change == "exhausted":
            terminal = setup.control.state
            terminal["pause"] = "export_limit_retry_exhausted"
            terminal["retry"].update(state="operator_required", attempts=3, next_attempt_at=None)
            await setup.control.save(terminal)
        saved = setup.control.state
        owner._startup_auto_recovery_authorized = True
        owner.entry.data = {**DATA, "household_load_entity": "sensor.replacement_load"}
        if change == "different_target":
            owner.entry.data[CONF_ENPHASE_EXPORT_LIMIT_ENTITY] = "sensor.different_export_limit"
        elif change == "disabled":
            owner.entry.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED] = False
        await owner.async_prepare_configuration_reload()
        assert owner._configuration_reload_handoff
        assert not owner.active_control
        assert len([call for call in setup.calls if call[0] == "enphase_ev"]) == 1
        if change in {"different_target", "disabled"}:
            assert not setup.control.state
            return
        assert setup.control.state == saved
        setup.store.data = deepcopy(setup.store.data)
        restarted = retry_owner.create()
        assert await restarted.async_reconcile_production_evidence_contract()
        assert setup.control.state["retry"] == saved["retry"]
        assert restarted._startup_auto_recovery_authorized
        if change == "exhausted":
            await restarted.async_restore_safe_state("startup_auto_recovery_replan_failed", refresh=False)
            assert setup.control.state == saved

    asyncio.run(run())


@pytest.mark.parametrize("cancel_startup", [False, True])
def test_provisional_startup_blocks_ordinary_export_after_noop_probe(setup, retry_owner, monkeypatch, cancel_startup):
    from custom_components.ha_energy_planner import startup_recovery

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owner._startup_auto_recovery_authorized = True
        setup.store.data["production"]["armed"] = False
        owner.async_restore_safe_state = AsyncMock()
        owner._async_compensate_ev_auto_start = AsyncMock()
        report = {"safe_to_activate_now": True, "active_control_ready": True}
        monkeypatch.setattr(startup_recovery, "build_preflight_report", lambda *_: report)
        monkeypatch.setattr(startup_recovery, "_startup_auto_recovery_validation_ready", lambda *_: (True, "ready"))
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def activation_refresh(_trigger):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        owner._async_refresh_with_export_preflight = activation_refresh
        startup = asyncio.create_task(owner._async_reactivate_after_startup_recovery(1))
        owner._startup_auto_recovery_task = startup
        await asyncio.wait_for(entered.wait(), 1)
        setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
        await owner._async_retry_export_limit()
        assert "retry" not in setup.control.state

        cancellation = None
        if cancel_startup:
            cancellation = asyncio.create_task(owner.async_operator_disarm_production_control())
            await asyncio.wait_for(cancelled.wait(), 1)
            assert not owner.active_control
        tariff = price_state((0.1, 0.1))
        tariff.last_updated = now
        setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = tariff
        setup.mode["value"] = "confirm"
        result = await owner.executor.async_execute_export_limit(action(setup), now)
        assert result[1:] == ("export_limit_retry_interrupted", False)
        assert len([call for call in setup.calls if call[0] == "enphase_ev"]) == 1
        release.set()
        if cancellation is not None:
            await cancellation
            assert await startup == (False, "startup_recovery_cancelled")
            assert not owner.active_control
        else:
            assert (await startup)[0]
        if cancel_startup:
            await owner.async_arm_production_control("operator")
        assert not owner._export_write_blocked()

    asyncio.run(run())


@pytest.mark.parametrize("persisted", ["provisional", "legacy", "recovered", "disabled"])
def test_restart_reconciles_provisional_export_arm_before_first_plan(setup, retry_owner, persisted):
    async def run():
        old_owner = retry_owner.create()
        await old_owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
        await old_owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        if persisted == "legacy":
            setup.store.data["production"].pop("startup_activation_pending")
        elif persisted == "recovered":
            await old_owner._async_update_startup_auto_recovery(
                "recovered", successful_runs=1, reason="validated", completed=True,
            )
        setup.store.data = deepcopy(setup.store.data)
        restarted = retry_owner.create({CONF_DRY_RUN: persisted == "disabled"})
        assert restarted._export_write_blocked() == (persisted in {"provisional", "disabled"})
        assert await restarted.async_reconcile_production_evidence_contract()
        assert getattr(restarted, "_startup_auto_recovery_authorized", False) == (persisted != "disabled")
        assert restarted.active_control == (persisted == "recovered")
        setup.mode["value"] = "confirm"
        _, _, sent = await restarted.executor.async_execute_export_limit(action(setup), NOW)
        assert sent == (persisted == "recovered")
        assert len(setup.calls) == int(persisted == "recovered")

    asyncio.run(run())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_operator_disarm_revokes_armed_grace_while_startup_task_drains(setup, retry_owner, tmp_path, cleanup_fails):
    from homeassistant.core import HomeAssistant

    from custom_components.ha_energy_planner.storage import PlannerStore

    async def run():
        owner = retry_owner.create()
        hass = HomeAssistant(str(tmp_path))
        owner.store = owner.executor.store = PlannerStore(hass, "operator-disarm")
        await owner.store.async_save_production(setup.store.data["production"])
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owner._startup_auto_recovery_authorized = True
        owner._export_retry_startup_blocked = True
        await owner._async_update_startup_auto_recovery("grace", successful_runs=0, reason="starting")
        entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def grace():
            async with owner._command_lock:
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    draining.set()
                    await release.wait()
                    if cleanup_fails:
                        raise RuntimeError("startup cleanup failed")

        owner._async_complete_startup_grace = grace
        owner._startup_auto_recovery_task = asyncio.create_task(owner._async_run_startup_auto_recovery())
        await asyncio.wait_for(entered.wait(), 1)
        try:
            async with owner.store.async_delay_save():
                disarm = asyncio.create_task(owner.async_operator_disarm_production_control())
                try:
                    await asyncio.wait_for(draining.wait(), 1)
                    assert not disarm.done()
                    assert not owner.active_control
                    # Wait for the checked Store barrier to complete while the
                    # unrelated batch and the startup task are still open.
                    async with owner.store._save_lock:
                        restarted = PlannerStore(hass, "operator-disarm")
                        await restarted.async_load()
                    production = restarted.data["production"]
                    assert production["armed"] is False
                    assert production["startup_auto_recovery"]["last_reason"] == "operator_disarmed"
                    setup.mode["value"] = "confirm"
                    assert owner._export_retry_control() is None
                    assert not (await owner.executor.async_execute_export_limit(action(setup), now))[2]
                    retry_owner.upstream.async_prepare.assert_not_awaited()
                    assert len(setup.calls) == 1
                finally:
                    release.set()
                    await disarm
            assert owner._startup_auto_recovery_task is None
            assert not owner.active_control
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["grace", "unsafe_restore", "validation", "restore", "evidence_save", "reactivation"])
def test_operator_cancellation_cannot_be_overwritten_by_startup_progress(setup, retry_owner, monkeypatch, stage):
    from custom_components.ha_energy_planner import startup_recovery

    async def run():
        owner = retry_owner.create()
        owner._startup_auto_recovery_authorized = True
        owner._startup_auto_recovery_deadline = 0
        setup.store.data["production"]["armed"] = stage in {"grace", "unsafe_restore"}
        entered, release = asyncio.Event(), asyncio.Event()

        async def drain():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        async def validation():
            await drain()
            return stage == "grace", "validation_completed_after_cancel"

        async def reactivation(_runs):
            await drain()
            return False, "activation_failed_after_cancel"

        if stage in {"grace", "validation"}:
            owner._async_run_startup_auto_recovery_validation = validation
        elif stage == "unsafe_restore":
            owner._async_run_startup_auto_recovery_validation = AsyncMock(return_value=(False, "unsafe"))
        else:
            owner._async_run_startup_auto_recovery_validation = AsyncMock(return_value=(True, "ready"))
        if stage in {"restore", "unsafe_restore"}:
            async def restore(*args, **kwargs):
                await drain()
                return None
            owner.async_restore_safe_state = restore
        elif stage == "evidence_save":
            save = owner._async_save_production
            restoring_saved = False

            async def persist(production):
                nonlocal restoring_saved
                await save(production)
                if production.get("startup_auto_recovery", {}).get("status") == "restoring":
                    if restoring_saved:
                        await drain()
                    restoring_saved = True

            owner.async_restore_safe_state = AsyncMock(return_value=None)
            owner._async_save_production = persist
        elif stage == "reactivation":
            owner._async_reactivate_after_startup_recovery = reactivation
        monkeypatch.setattr(startup_recovery, "_wait_for_load_recovery", AsyncMock())
        owner._startup_auto_recovery_task = asyncio.create_task(owner._async_run_startup_auto_recovery())
        await asyncio.wait_for(entered.wait(), 1)
        disarm = asyncio.create_task(owner.async_operator_disarm_production_control())
        await asyncio.sleep(0)
        assert not owner.active_control
        release.set()
        await disarm
        assert setup.store.data["production"]["startup_auto_recovery"]["last_reason"] == "operator_disarmed"
        restarted = retry_owner.create()
        await restarted.async_reconcile_production_evidence_contract()
        assert not restarted.active_control
        assert not getattr(restarted, "_startup_auto_recovery_authorized", False)
        assert not any(call[0] == "enphase_ev" for call in setup.calls)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["cancel", "replan", "evidence_save", "handoff_save"])
def test_option_handoff_preserves_concurrent_operator_disarm(setup, retry_owner, stage):
    from custom_components.ha_energy_planner.const import CONF_EV_CONTROL_ENABLED

    async def run():
        owner = retry_owner.create({CONF_EV_CONTROL_ENABLED: True})
        owner._startup_auto_recovery_authorized = True
        await owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
        await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        owner.async_start_startup_auto_recovery = Mock()
        entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def startup():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    draining.set()

        if stage == "cancel":
            owner._startup_auto_recovery_task = asyncio.create_task(startup())
            await asyncio.wait_for(entered.wait(), 1)
        elif stage == "replan":
            async def replan():
                draining.set()
                await release.wait()

            owner.async_request_replan = replan
        else:
            save = owner._async_save_production
            if stage == "evidence_save":
                # Force the device-only handoff to refresh its saved evidence.
                setup.store.data["production"]["dry_run_evidence_fingerprint"] = "previous-contract"
            if stage == "handoff_save":
                # Only a settled arm saves the final handoff after replanning.
                setup.store.data["production"].pop("startup_activation_pending")
                owner._export_startup_activation_pending = False

            async def persist(production):
                await save(production)
                reason = production.get("startup_auto_recovery", {}).get("last_reason")
                is_new_evidence = (
                    reason == "startup_safe_recovery_pending"
                    and production.get("dry_run_evidence_fingerprint") != "previous-contract"
                )
                if (stage == "evidence_save" and is_new_evidence
                        or stage == "handoff_save" and reason == "configuration_changed"):
                    draining.set()
                    await release.wait()

            owner._async_save_production = persist
        owner.entry.options[CONF_EV_CONTROL_ENABLED] = False
        update = asyncio.create_task(owner.async_handle_options_update())
        await asyncio.wait_for(draining.wait(), 1)
        disarm = asyncio.create_task(owner.async_operator_disarm_production_control())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(update, disarm)
        owner.async_start_startup_auto_recovery.assert_not_called()
        assert setup.store.data["production"]["startup_auto_recovery"]["last_reason"] == "operator_disarmed"
        restarted = retry_owner.create()
        await restarted.async_reconcile_production_evidence_contract()
        assert not restarted.active_control
        assert not getattr(restarted, "_startup_auto_recovery_authorized", False)

    asyncio.run(run())


def test_operator_disarm_supersedes_startup_cancellation_during_restoration(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        owner._startup_auto_recovery_authorized = True
        await owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
        restoring, release = asyncio.Event(), asyncio.Event()

        async def restore(*args, **kwargs):
            restoring.set()
            await release.wait()

        owner.async_restore_safe_state = restore
        cancel = asyncio.create_task(owner.async_cancel_startup_auto_recovery("options_changed"))
        await asyncio.wait_for(restoring.wait(), 1)
        await owner.async_operator_disarm_production_control()
        release.set()
        await cancel
        assert setup.store.data["production"]["startup_auto_recovery"]["last_reason"] == "operator_disarmed"

    asyncio.run(run())


def test_operator_disarm_restores_export_after_one_failed_store_save(setup, retry_owner, tmp_path):
    from unittest.mock import patch

    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store as HAStore
    from homeassistant.util.file import WriteError

    from custom_components.ha_energy_planner.storage import PlannerStore

    async def run():
        owner = retry_owner.create()
        hass = HomeAssistant(str(tmp_path))
        owner.store = owner.executor.store = PlannerStore(hass, "disarm-export")
        await owner.store.async_save_production(setup.store.data["production"])
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        write = HAStore._async_write_data
        failed = False

        async def fail_once(backend, data):
            nonlocal failed
            if not failed:
                failed = True
                raise WriteError("first disarm save failed")
            await write(backend, data)

        try:
            setup.store.flushes = 1
            outcome = await owner.executor.async_execute_export_limit(action(setup), NOW)
            assert outcome[2], outcome
            assert setup.states[ENTITY].attributes["confirmed_watts"] == 0
            advance_clock(setup)
            with patch.object(HAStore, "_async_write_data", fail_once), pytest.raises(HomeAssistantError):
                await owner.async_operator_disarm_production_control()
            restarted = PlannerStore(hass, "disarm-export")
            await restarted.async_load()
            assert not restarted.data["production"]["armed"]
            assert EXPORT_ASSET not in restarted.data["ownership"]
            assert setup.states[ENTITY].attributes["confirmed_watts"] == 3000
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_restart_finishes_export_restore_interrupted_by_operator_disarm(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        assert (await owner.executor.async_execute_export_limit(action(setup), NOW))[2]
        advance_clock(setup)
        await owner._async_update_startup_auto_recovery(
            "cancelled", successful_runs=0, reason="operator_disarmed", completed=True, disarm_reason="user_requested",
        )
        assert not setup.control.state.get("restoring")
        assert setup.states[ENTITY].attributes["confirmed_watts"] == 0

        restarted = retry_owner.create()
        await restarted.async_reconcile_production_evidence_contract()
        assert setup.control.state["restoring"]
        assert not restarted.active_control
        assert not getattr(restarted, "_startup_auto_recovery_authorized", False)
        await restarted._async_reconcile_export_limit()
        assert setup.states[ENTITY].attributes["confirmed_watts"] == 3000
        assert not setup.control.state

    asyncio.run(run())


@pytest.mark.parametrize("legacy", [False, True])
def test_operator_disarm_stays_authoritative_across_restart(setup, retry_owner, legacy):
    async def run():
        owner = retry_owner.create()
        await owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
        await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        if legacy:
            await owner.async_cancel_startup_auto_recovery("operator_disarmed", restore_owned_state=False)
            assert setup.store.data["production"]["armed"]
        else:
            save = setup.store.async_save_production

            async def cut_power_after_first_save(value):
                await save(value)
                raise OSError("interrupted after save")

            setup.store.async_save_production = cut_power_after_first_save
            with pytest.raises(OSError, match="interrupted after save"):
                await owner.async_operator_disarm_production_control()
            setup.store.async_save_production = save
            assert not setup.store.data["production"]["armed"]
        assert setup.store.data["production"]["startup_auto_recovery"]["last_reason"] == "operator_disarmed"

        restarted = retry_owner.create()
        await restarted.async_reconcile_production_evidence_contract()
        assert not restarted.active_control
        assert not getattr(restarted, "_startup_auto_recovery_authorized", False)
        assert not (await restarted.executor.async_execute_export_limit(action(setup), NOW))[2]
        await restarted.async_arm_production_control()
        rearmed = retry_owner.create()
        await rearmed.async_reconcile_production_evidence_contract()
        assert rearmed.active_control

    asyncio.run(run())


def test_second_restart_after_interrupted_option_change_resumes_recovery(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
        await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        await owner.async_cancel_startup_auto_recovery("options_changed", restore_owned_state=False)

        restarted = retry_owner.create()
        save = setup.store.async_save_production

        async def cut_power_after_first_save(value):
            await save(value)
            raise OSError("second interruption")

        setup.store.async_save_production = cut_power_after_first_save
        with pytest.raises(OSError, match="second interruption"):
            await restarted.async_reconcile_production_evidence_contract()
        setup.store.async_save_production = save
        assert not setup.store.data["production"]["armed"]
        restarted = retry_owner.create()
        assert await restarted.async_reconcile_production_evidence_contract()
        assert restarted._startup_auto_recovery_authorized
        assert not restarted.active_control

    asyncio.run(run())


def test_overdue_export_retry_waits_for_home_assistant_and_startup_grace(setup, retry_owner, monkeypatch):
    from custom_components.ha_energy_planner import startup_recovery

    started_callbacks = []
    grace_entered = asyncio.Event()
    release_grace = asyncio.Event()

    def at_started(hass, callback):
        started_callbacks.append(callback)
        return Mock()

    async def wait_for_grace(owner, delay):
        assert delay > 0
        grace_entered.set()
        await release_grace.wait()

    monkeypatch.setattr(startup_recovery, "async_at_started", at_started)
    monkeypatch.setattr(startup_recovery, "_wait_for_load_recovery", wait_for_grace)

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        setup.hass.is_running = False
        await owner._async_reconcile_export_limit_locked()
        assert not retry_owner.timers
        assert await owner.async_reconcile_production_evidence_contract()
        owner.async_start_startup_auto_recovery()
        setup.hass.is_running = True
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_not_called()
        owner._async_run_startup_auto_recovery_validation = AsyncMock(return_value=(True, "validation_succeeded"))
        await started_callbacks[0](setup.hass)
        await asyncio.wait_for(grace_entered.wait(), 1)
        await owner._async_reconcile_export_limit_locked()
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_not_called()
        assert not retry_owner.timers
        release_grace.set()
        await owner._startup_auto_recovery_task
        wakeup, = retry_owner.timers
        assert wakeup.delay == 0
        setup.mode["value"] = "confirm"
        wakeup.wake(now)
        await asyncio.gather(*owner._listener_tasks)
        retry_owner.upstream.async_apply_guarded.assert_awaited_once()

    asyncio.run(run())


def test_failed_final_retry_claim_is_not_persisted_as_an_attempt(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        advance_clock(setup)
        setup.store.fail = True
        await owner._async_run_export_retry()
        retry_owner.upstream.async_prepare.assert_not_called()
        assert setup.control.state["retry"]["attempts"] == 2
        setup.store.fail = False
        await owner._async_reconcile_export_limit_locked()
        setup.store.data = deepcopy(setup.store.data)
        restarted = retry_owner.create()
        setup.mode["value"] = "confirm"
        await restarted._async_retry_export_limit()
        retry_owner.upstream.async_apply_guarded.assert_awaited_once()
        assert setup.control.state["expected"]["watts"] == 0

    asyncio.run(run())


def test_stale_export_action_cannot_charge_the_allowance_again(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        planned = action(setup)
        await owner.executor.async_execute_export_limit(planned, NOW)
        result, reason, sent = await owner.executor.async_execute_export_limit(planned, NOW)
        assert (result, reason, sent) == (OutcomeResult.REJECTED, runtime.UNAVAILABLE_REJECTION, False)
        assert len(setup.calls) == 1
        assert action_budget(budget_history(setup.store.data), {}, NOW, EXPORT_ASSET)["used"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 1])
def test_export_retry_wakeup_repairs_clock_rollback_without_a_main_refresh(setup, retry_owner, limit):
    async def run():
        owner = retry_owner.create({CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS: limit})
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        await owner._async_reconcile_export_limit_locked()
        timer = retry_owner.timers[-1]
        rolled_back = advance_clock(setup, -30 * 86400)
        timer.wake(rolled_back)
        await asyncio.gather(*owner._listener_tasks)
        repaired = retry_owner.timers[-1]
        assert repaired.delay == 300
        assert setup.control.state["retry"]["attempts"] == 0
        assert setup.control.state["retry"]["next_attempt_at"] == (rolled_back + timedelta(minutes=5)).isoformat()
        attempts = deepcopy(budget_history(setup.store.data))
        assert attempts[0]["attempted_at"] == rolled_back.isoformat()
        budget = action_budget(attempts, {}, rolled_back + timedelta(hours=24), EXPORT_ASSET)
        assert budget["used"] == 0
        now = advance_clock(setup)
        setup.states["sensor.export_price"] = price_state(start=now.replace(minute=0))
        setup.states["sensor.export_price"].last_updated = now
        setup.mode["value"] = "confirm"
        repaired.wake(now)
        await asyncio.gather(*owner._listener_tasks)
        if limit:
            assert len(setup.calls) == 1
            assert owner._export_retry_control().retry_deadline(now) == rolled_back + timedelta(hours=24)
            assert setup.control.state["retry"]["attempts"] == 0
        else:
            assert len(setup.calls) == 2
            assert "retry" not in setup.control.state

    asyncio.run(run())


@pytest.mark.parametrize("restore", [
    "baseline", "different", "stale", "active", "terminal", "terminal_pending", "terminal_wrong_target",
])
def test_export_retry_restoration_preserves_recovery_until_settled(setup, retry_owner, restore):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    async def run():
        owner = retry_owner.create()
        if restore == "different":
            setup.states[ENTITY] = limit_state(3500)
            await setup.control.save({
                "identity": runtime.target_identity(setup.hass, ENTITY),
                "gateway_identity": GATEWAY,
                "baseline": {"watts": None, "slew_rate": 100.0},
                "expected": {"watts": 3500, "slew_rate": 100.0},
            })
            await setup.control.prepare_cooldown(NOW - timedelta(minutes=5))
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = NOW
        if restore.startswith("terminal"):
            now = advance_clock(setup)
            retry_owner.upstream.async_prepare.side_effect = installer_rejection("export_limit_session_expired")
            await owner._async_retry_export_limit()
            assert setup.control.state["pause"] == "export_limit_service_rejected:installer_session_expired"
        if restore != "active":
            setup.store.data["production"]["armed"] = False
        if restore == "stale":
            setup.states[ENTITY].attributes["last_successful_readback"] = (NOW - timedelta(hours=1)).timestamp()
        control = runtime.ExportLimitControl(setup.hass, setup.store, DATA, options=owner.planner_options)
        if restore in {"terminal_pending", "terminal_wrong_target"}:
            reconcile = control.reconcile

            async def change_after_reconciliation(now):
                issue = await reconcile(now)
                if restore == "terminal_pending":
                    setup.states[ENTITY] = limit_state(pending=True, readback=now.timestamp())
                else:
                    setup.registry[ENTITY].device_id = "replaced"
                return issue

            control.reconcile = change_after_reconciliation
        result, _, sent = await control.restore(now)
        assert not sent
        if restore in {"baseline", "terminal"}:
            assert result == OutcomeResult.RESTORED
            assert not setup.control.state
        else:
            assert result == OutcomeResult.PENDING
            retry = export_limit_status(owner)["retry"]
            assert retry["attempts"] == int(restore.startswith("terminal"))
            assert retry["state"] == "operator_required"
            assert retry["next_attempt_at"] is None
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_export_retry_status_shows_suspension_without_a_timer(setup, retry_owner):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        await owner._async_reconcile_export_limit_locked()
        assert export_limit_status(owner)["retry"]["state"] == "scheduled"
        setup.registry[ENTITY].device_id = "replacement"
        owner._schedule_export_retry()
        retry = export_limit_status(owner)["retry"]
        assert retry["state"] == "suspended"
        assert retry["next_attempt_at"] is None

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["recovery", "last_attempt_interrupted", "disk_failure", "timed_pause"])
def test_export_retry_wakeup_persists_and_respects_cooldown(setup, retry_owner, scenario):
    from custom_components.ha_energy_planner.plan_presentation import export_limit_status

    async def run():
        options = {CONF_COMMAND_RATE_LIMIT_SECONDS: 600}
        owner = retry_owner.create(options)
        setup.states["sensor.export_price"] = price_state((-0.1, -0.2, 0.2))
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        await owner._async_reconcile_export_limit_locked()
        first_timer = retry_owner.timers[-1]
        assert first_timer.delay == 600
        assert export_limit_status(owner)["retry"]["next_attempt_at"] == (NOW + timedelta(seconds=600)).isoformat()
        owner._schedule_export_retry()
        assert len(retry_owner.timers) == 1
        advance_clock(setup)
        await owner._async_retry_export_limit()
        retry_owner.upstream.async_prepare.assert_not_called()
        owner._begin_shutdown()
        first_timer.cancel.assert_called_once()
        first_timer.wake(setup.clock["now"])

        # A new coordinator receives only the persisted transaction state.
        setup.store.data = deepcopy(setup.store.data)
        owner = retry_owner.create(options)
        if scenario == "last_attempt_interrupted":
            setup.store.data["ownership"][EXPORT_ASSET]["retry"].update(
                attempts=runtime.MAX_RETRY_ATTEMPTS, state="checking",
            )
        elif scenario == "timed_pause":
            await owner.async_pause_control(15, "operator", asset=EXPORT_ASSET)
        await owner._async_reconcile_export_limit_locked()
        timer = retry_owner.timers[-1]
        assert timer.delay == (900 if scenario == "timed_pause" else 300)
        assert export_limit_status(owner)["retry"]["state"] == "scheduled"
        if scenario == "disk_failure":
            setup.store.fail = True
        setup.mode["value"] = "confirm"
        now = advance_clock(setup, timer.delay)
        timer.wake(now)
        timer.wake(now)
        await asyncio.gather(*owner._listener_tasks)
        if scenario in {"disk_failure", "last_attempt_interrupted"}:
            assert len(setup.calls) == 1
            retry_owner.upstream.async_prepare.assert_not_called()
            if scenario == "disk_failure":
                assert retry_owner.timers[-1].delay >= 300
                setup.store.fail = False
                advance_clock(setup, -30 * 86400)
                owner._schedule_export_retry()
                repaired = retry_owner.timers[-1]
                assert repaired.delay == 300
                repaired.wake(advance_clock(setup))
                await asyncio.gather(*owner._listener_tasks)
                assert retry_owner.timers[-1].delay == 600
            else:
                assert export_limit_status(owner)["retry"]["state"] == "operator_required"
        else:
            assert len(setup.calls) == 2
            retry_owner.upstream.async_prepare.assert_awaited_once()

    asyncio.run(run())


def test_calendar_windows_and_history_use_confirmed_feedback(setup):
    from custom_components.ha_energy_planner.calendar import _calendar_events
    from custom_components.ha_energy_planner.calendar_history import confirm_calendar_action

    ev = runtime.evidence(setup.hass, DATA, OPTIONS, NOW)
    ctx = context(ev)
    owner = SimpleNamespace(data=DryRunPlanner(OPTIONS).create_plan(ctx), options=OPTIONS,
                            entry=SimpleNamespace(entry_id="export-a"), entry_data=DATA,
                            hass=setup.hass, store=setup.store)
    events = _calendar_events(owner)
    assert len(events) == 2
    assert "Confirmed setting: Disabled" in events[0].description
    ctx.export_limit["feedback"]["watts"] = 0
    owner.data = DryRunPlanner(OPTIONS).create_plan(ctx)
    assert len(owner.data.actions) == 1
    assert len(_calendar_events(owner)) == 2  # Different settings remain separate windows.
    a = action(setup)
    outcome = {"plan_id": a.plan_id, "action_id": a.action_id, "asset": EXPORT_ASSET,
               "kind": str(a.kind), "attempted_at": NOW.isoformat(), "result": "pending"}
    assert not confirm_calendar_action({}, outcome).get("history")
    outcome["result"] = "applied"
    assert confirm_calendar_action({}, outcome)["history"][0]["summary"].endswith("Enabled — 0 W (confirmed)")
    outcome["kind"] = "disable_export_limit"
    assert confirm_calendar_action({}, outcome)["history"][0]["summary"].endswith("Disabled (confirmed)")


@pytest.mark.parametrize("prices,watts,label", [
    ((0.1015, 0.097), None, "Disabled"),
    ((-0.1, -0.2), 0, "Enabled — 0 W"),
    ((-0.0, 0.1), None, "Disabled"),
])
def test_calendar_merges_unchanged_export_setting_and_keeps_tariff_evidence(setup, prices, watts, label):
    from custom_components.ha_energy_planner.calendar import _calendar_events, calendar_event_records

    setup.states["sensor.export_price"] = price_state(prices)
    setup.states[ENTITY] = limit_state(watts)
    ctx = context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
    plan = DryRunPlanner(OPTIONS).create_plan(ctx)
    owner = SimpleNamespace(data=plan, options=OPTIONS, entry=SimpleNamespace(entry_id="export-a"),
                            entry_data=DATA, hass=setup.hass, store=setup.store)
    events = _calendar_events(owner)
    assert len(events) == 1
    event = events[0]
    assert event.summary == f"Enphase Export Limit: {label} (planned)"
    assert event.start == NOW.replace(minute=0)
    assert event.end == NOW.replace(minute=0) + timedelta(hours=1)
    assert event.uid == f"export-a-export-limit-{event.start.isoformat()}"
    for price in prices:
        assert f"{price * 100:g} c/kWh" in event.description
    assert "Export prices:\n• " in event.description
    assert "12:00 AM" in event.description and "12:30 AM" in event.description and "1:00 AM" in event.description
    assert len(plan.device_plans[EXPORT_ASSET]["blocks"]) == 2
    assert plan.actions == []
    assert "confirmed_start" not in calendar_event_records(owner)[0]


@pytest.mark.parametrize("separation", ["gap", "overlap", "setting"])
def test_calendar_keeps_export_gaps_overlaps_and_setting_changes_separate(setup, separation):
    from custom_components.ha_energy_planner.calendar import _calendar_events

    ctx = context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
    plan = DryRunPlanner(OPTIONS).create_plan(ctx)
    blocks = plan.device_plans[EXPORT_ASSET]["blocks"]
    if separation == "setting":
        assert blocks[0]["watts"] != blocks[1]["watts"]
    else:
        blocks[1]["watts"] = blocks[0]["watts"]
        offset = 5 if separation == "gap" else -5
        blocks[1]["start"] = (block_time(blocks[1]["start"]) + timedelta(minutes=offset)).isoformat()
    owner = SimpleNamespace(data=plan, options=OPTIONS, entry=SimpleNamespace(entry_id="export-a"),
                            entry_data=DATA, hass=setup.hass, store=setup.store)
    events = _calendar_events(owner)
    assert len(events) == 2
    assert events[0].end == block_time(blocks[0]["end"])
    assert events[1].start == block_time(blocks[1]["start"])


def test_healthy_export_area_can_arm_with_unavailable_legacy_inputs(setup):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.const import CONF_ENPHASE_CONTROL_ENABLED, CONF_ENPHASE_PROFILE
    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint

    mapping = {**DATA, CONF_ENPHASE_PROFILE: "select.offline_profile", "household_load_entity": "sensor.offline_load"}
    options = {**OPTIONS, CONF_ENPHASE_CONTROL_ENABLED: True}
    plan = DryRunPlanner(options).create_plan(context(runtime.evidence(setup.hass, mapping, options, NOW)))
    owner = _coordinator_for_runtime_services(entry_data=mapping, options=options, hass=setup.hass)
    owner.data = plan
    owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW}
    owner.store.data["production"] = {"dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(mapping, options)}
    report = build_preflight_report(setup.hass, owner)
    assert report["safe_to_activate_now"], report
    assert report["control_areas"]["confidence_eligible"] == [EXPORT_ASSET]


def test_enable_export_control_during_automatic_operation(setup):
    from unittest.mock import AsyncMock

    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False, CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED: False}
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=opts, hass=setup.hass)
        owner.store.data = setup.store.data
        owner.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(DATA, opts)
        owner.data = DryRunPlanner(opts).create_plan(context({}))
        assert EXPORT_ASSET not in owner.data.control_area_health
        owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW}
        setup.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, options: setattr(entry, "options", options))
        owner.async_handle_options_update = AsyncMock()
        await owner.async_set_device_control(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED, True)
        assert owner.options[CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED]
        assert not setup.calls  # Arming readiness never writes; the fresh replacement plan does.

    asyncio.run(run())


def test_upstream_unconfirmed_retains_pending_and_confirmed_feedback(setup):
    async def run():
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY].state = "unconfirmed"
        setup.states[ENTITY].attributes["request_status"] = "unconfirmed"
        observed, issue = runtime.feedback(setup.hass, ENTITY, NOW)
        assert issue is None and observed["pending"]
        assert observed["watts"] is None and observed["requested_watts"] == 0
        assert runtime.evidence(setup.hass, DATA, OPTIONS, NOW)["reason"] == "export_limit_pending"
        setup.clock["now"] = NOW + timedelta(minutes=10)
        assert await setup.control.reconcile(setup.clock["now"]) == "export_limit_unconfirmed"
        assert setup.control.state["pending"]
        setup.states[ENTITY].attributes["pending"] = False
        assert runtime.feedback(setup.hass, ENTITY, NOW)[1] == "export_limit_unsupported"

    asyncio.run(run())


def test_external_slew_change_during_pending_surrenders_ownership(setup):
    async def run():
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY] = limit_state(0, slew=77)
        assert await setup.control.reconcile(NOW) is None
        assert "baseline" not in setup.control.state and "pending" not in setup.control.state
        assert setup.store.data["execution_audit"][-1]["result"] == "rejected"
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.SKIPPED
        assert len(setup.calls) == 1
        assert "pause" not in setup.control.state
        setup.states["sensor.export_price"] = price_state((0, 1, 1))
        await setup.control.execute(action(setup), advance_clock(setup))
        assert setup.control.state["baseline"] == {"watts": 0, "slew_rate": 77}

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["save", "cancel"])
def test_failure_before_service_boundary_rolls_back_known_unsent_request(setup, stage):
    async def run():
        a = action(setup)
        original_save, original_flush = setup.store.async_save_ownership, setup.store.async_flush

        async def failed_save(value):
            await original_save(value)
            if value.get(EXPORT_ASSET, {}).get("pending"):
                raise OSError("disk failed before flush")

        async def cancelled_flush():
            raise asyncio.CancelledError

        setup.store.async_save_ownership = failed_save if stage == "save" else original_save
        setup.store.async_flush = cancelled_flush if stage == "cancel" else original_flush
        with pytest.raises(OSError if stage == "save" else asyncio.CancelledError):
            await setup.control.execute(a, NOW)
        assert not setup.control.state and not setup.calls
        setup.store.async_save_ownership, setup.store.async_flush = original_save, original_flush
        assert (await setup.control.execute(a, NOW))[2]

    asyncio.run(run())


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(("primary", "secondary"), [(60, None), (1e15, None), (1e15, 30), (30, 1e15)])
def test_conflicting_declared_cadence_cannot_authorize_a_write(explicit, primary, secondary):
    state = price_state((-1, 1), explicit=explicit)
    state.attributes.update(forecast_interval_minutes=primary, interval_minutes=secondary)
    assert tariff_blocks(state, NOW, OPTIONS) == ([], "export_tariff_ambiguous_duration")


def test_export_recovery_ignores_unrelated_recorder_requirement(setup):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.preflight import build_preflight_report, production_evidence_fingerprint
    from custom_components.ha_energy_planner.startup_recovery import _startup_auto_recovery_validation_ready

    mapping = {**DATA, "household_load_entity": "sensor.optional_load"}
    owner = _coordinator_for_runtime_services(entry_data=mapping, options=OPTIONS, hass=setup.hass)
    owner.data = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(setup.hass, mapping, OPTIONS, NOW)))
    owner.last_refresh_metadata = {"succeeded": True, "completed_at": NOW}
    owner.store.data["production"] = {"dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": production_evidence_fingerprint(mapping, OPTIONS)}
    report = build_preflight_report(setup.hass, owner)
    assert not report["recorder"]["available"]
    assert _startup_auto_recovery_validation_ready(report, mapping) == (True, "validation_succeeded")
    report["current_plan"]["export_limit_safe"] = False
    assert _startup_auto_recovery_validation_ready(report, mapping) == (False, "recorder_unavailable")


@pytest.mark.parametrize("disabled", [EXPORT_ASSET, "enphase"])
@pytest.mark.parametrize("provisional", [False, True])
def test_device_toggle_during_startup_grace_preserves_other_ownership(setup, disabled, provisional):
    from unittest.mock import AsyncMock

    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.const import CONF_ENPHASE_CONTROL_ENABLED, CONF_ENPHASE_PROFILE

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False, CONF_ENPHASE_CONTROL_ENABLED: True}
        owner = _coordinator_for_runtime_services(entry_data={**DATA, CONF_ENPHASE_PROFILE: "select.profile"},
            options=opts, hass=setup.hass)
        owner.store.data = setup.store.data
        owner.store.data["ownership"] = {EXPORT_ASSET: {"baseline": {"watts": 3000, "slew_rate": 100}},
                                         "enphase_profile": "Full Backup"}
        owner._startup_auto_recovery_authorized = True
        owner.store.data["production"]["startup_auto_recovery"] = {"status": "grace"}
        if provisional:
            await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        owner.async_start_startup_auto_recovery = lambda: None
        owner.async_restore_safe_state = AsyncMock()
        setup.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, options: setattr(entry, "options", options))
        option = CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED if disabled == EXPORT_ASSET else CONF_ENPHASE_CONTROL_ENABLED
        await owner.async_set_device_control(option, False)
        owner.async_restore_safe_state.assert_not_awaited()
        assert owner.executor.device_restores == [(disabled, f"{disabled}_control_disabled")]
        assert owner.store.data["production"]["armed"] is not provisional
        if provisional:
            assert owner.store.data["production"]["startup_auto_recovery"]["last_reason"] == (
                "startup_safe_recovery_pending"
            )

    asyncio.run(run())


@pytest.mark.parametrize("provisional", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_device_toggle_preserves_disarmed_recovery_across_restart(setup, retry_owner, provisional, interrupted):
    from test_coordinator import FakeExecutor

    from custom_components.ha_energy_planner.const import CONF_ENPHASE_CONTROL_ENABLED

    async def run():
        owner = retry_owner.create({CONF_ENPHASE_CONTROL_ENABLED: True})
        owner.executor = FakeExecutor()
        owner._startup_auto_recovery_authorized = True
        if provisional:
            await owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
            await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        else:
            await owner._async_update_startup_auto_recovery(
                "validating", successful_runs=0, reason="validation_in_progress", disarm_reason="startup_grace_unsafe",
            )
        owner.entry.options[CONF_ENPHASE_CONTROL_ENABLED] = False

        async def lose_power_before_restoration():
            raise OSError("interrupted option transition")

        if interrupted:
            owner.executor.async_persist_ev_grid_reservation = lose_power_before_restoration
            with pytest.raises(OSError, match="interrupted option transition"):
                await owner.async_handle_options_update()
        else:
            owner.async_start_startup_auto_recovery = Mock()
            await owner.async_handle_options_update()
            owner.async_start_startup_auto_recovery.assert_called_once()
        setup.store.data = deepcopy(setup.store.data)
        assert setup.store.data["production"]["armed"] is False
        assert "startup_activation_pending" not in setup.store.data["production"]

        restarted = retry_owner.create({CONF_ENPHASE_CONTROL_ENABLED: False})
        assert await restarted.async_reconcile_production_evidence_contract()
        assert restarted._startup_auto_recovery_authorized
        assert not restarted.active_control
        assert not (await restarted.executor.async_execute_export_limit(action(setup), NOW))[2]
        assert not setup.calls

    asyncio.run(run())


def test_pending_export_restoration_does_not_mask_failed_profile_restore(setup):
    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        advance_clock(setup)
        setup.store.data["ownership"]["enphase_profile"] = "Full Backup"
        setup.mode["value"] = "pending"
        executor = Executor(setup.store, hass=setup.hass,
            entry_data={**DATA, "enphase_profile_entity": "select.offline_profile"}, options=OPTIONS)
        result = await executor.async_restore_safe_state("manual")
        assert result.result == OutcomeResult.FAILED
        assert "enphase_profile_entity_unavailable" in result.reason
        assert setup.control.state["restoring"] and setup.control.state["pending"]
        assert setup.store.data["ownership"]["enphase_profile"] == "Full Backup"

    asyncio.run(run())


@pytest.mark.parametrize("race", ["unavailable", "replacement", "service_boundary"])
def test_unsent_restoration_remains_outstanding_after_target_race(setup, monkeypatch, race):
    from custom_components.ha_energy_planner.adapter_helpers import DeviceTargetUnavailable
    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        advance_clock(setup)
        original_flush = setup.store.async_flush

        async def change_target():
            await original_flush()
            if race == "unavailable":
                setup.states.pop(ENTITY, None)
            elif race == "replacement":
                setup.registry[ENTITY].device_id = "replacement"

        async def unavailable(*args):
            raise DeviceTargetUnavailable(ENTITY)

        if race == "service_boundary":
            monkeypatch.setattr(runtime, "async_call_device_service", unavailable)
        setup.store.async_flush = change_target
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=OPTIONS)
        result = await executor.async_restore_device_control(EXPORT_ASSET, "operator_stop")
        assert result.result == OutcomeResult.PENDING
        assert setup.control.state["restoring"] and setup.control.state["baseline"]
        assert not setup.control.state.get("pending")
        assert len([call for call in setup.calls if call[0] == "enphase_ev"]) == 1
        rejected = setup.store.data["execution_audit"][-2]
        assert rejected["result"] == "rejected" and rejected["kind"] == "restore_export_limit"
        assert rejected["service_target"] == ENTITY

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["confirm", "reject"])
def test_restoration_reports_matching_readback_and_identifies_rejected_action(setup, mode):
    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        advance_clock(setup)
        setup.mode["value"] = mode
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=OPTIONS)
        result = await executor.async_restore_device_control(EXPORT_ASSET, "operator_stop")
        assert result.result == (OutcomeResult.RESTORED if mode == "confirm" else OutcomeResult.FAILED)
        last = setup.store.data["execution_audit"][-2]
        assert last["kind"] == "restore_export_limit" and last["service_target"] == ENTITY
        assert last["result"] == ("restored" if mode == "confirm" else "failed")

    asyncio.run(run())


@pytest.mark.parametrize("upstream_pending", [False, True])
def test_saved_conflict_pause_recovers_only_after_settled_fresh_feedback(setup, upstream_pending):
    async def run():
        identity = runtime.target_identity(setup.hass, ENTITY)
        await setup.control.save({"identity": identity, "pause": "external_export_limit_conflict"})
        setup.states[ENTITY] = limit_state(None, pending=upstream_pending)
        issue = await setup.control.reconcile(NOW)
        if upstream_pending:
            assert issue == "export_limit_pending"
            assert not setup.calls
            setup.states[ENTITY] = limit_state()
            assert await setup.control.reconcile(NOW) is None
        else:
            assert issue is None
        assert "pause" not in setup.control.state
        assert setup.control.state["last_external_change"]["watts"] is None
        await setup.control.execute(action(setup), NOW)
        assert len(setup.calls) == 1
    asyncio.run(run())


def test_external_request_is_observed_without_conflict_pause_or_duplicate_command(setup):
    async def run():
        setup.mode["value"] = "confirm"
        requested = action(setup)
        await setup.control.execute(requested, NOW)
        setup.states[ENTITY] = limit_state(0, pending=True)
        assert await setup.control.reconcile(NOW) == "export_limit_pending"
        assert "pause" not in setup.control.state
        assert (await setup.control.execute(requested, NOW))[2] is False
        assert len(setup.calls) == 1
        setup.states[ENTITY] = limit_state()
        assert await setup.control.reconcile(NOW) is None
        await setup.control.execute(action(setup), advance_clock(setup))
        assert len(setup.calls) == 2
    asyncio.run(run())


def test_external_change_during_restoration_retires_baseline_without_key_error(setup):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY] = limit_state(5000, slew=77)
        assert await setup.control.restore(NOW) == (OutcomeResult.SKIPPED, "export_limit_not_owned", False)
        assert "baseline" not in setup.control.state
        assert "pause" not in setup.control.state
        assert len(setup.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["rejected", "unsupported", "late_confirmation", "late_slew_change"])
def test_external_feedback_preserves_failure_pause_until_explicit_resume(setup, failure):
    async def run():
        requested = action(setup)
        if failure in {"rejected", "unsupported"}:
            setup.mode["value"] = "confirm"
            await setup.control.execute(action(setup), NOW)
            if failure == "rejected":
                setup.mode["value"] = "reject"
                setup.states["sensor.export_price"] = price_state((0, 1, 1))
                await setup.control.execute(action(setup), advance_clock(setup))
                reason = "export_limit_service_rejected"
            else:
                setup.states[ENTITY].state = "unsupported"
                reason = "export_limit_unsupported"
                assert await setup.control.reconcile(NOW) == reason
            now = setup.clock["now"]
            setup.states[ENTITY] = limit_state(5000, slew=77)
        else:
            await setup.control.execute(action(setup), NOW)
            setup.clock["now"] = now = NOW + timedelta(minutes=10)
            reason = "export_limit_unconfirmed"
            assert await setup.control.reconcile(now) == reason
            setup.states[ENTITY] = limit_state(0, readback=now.timestamp(),
                                             slew=77 if failure == "late_slew_change" else 100)
            assert await setup.control.reconcile(now) == reason
            if failure == "late_confirmation":
                setup.states[ENTITY] = limit_state(5000, slew=77, readback=now.timestamp())
        assert await setup.control.reconcile(now) == reason
        assert setup.control.state["pause"] == reason
        assert "baseline" not in setup.control.state
        assert "pending" not in setup.control.state
        writes = len(setup.calls)
        assert await setup.control.execute(requested, now) == (OutcomeResult.REJECTED, reason, False)
        assert len(setup.calls) == writes
        await setup.control.resume()
        assert "pause" not in setup.control.state
        setup.mode["value"] = "confirm"
        setup.states["sensor.export_price"] = price_state((-1, 0, 1))
        setup.states[ENTITY] = limit_state(5000, slew=77)
        setup.clock["now"] = now
        await setup.control.execute(action(setup), advance_clock(setup))
        assert len(setup.calls) == writes + 1
        assert setup.control.state["baseline"] == {"watts": 5000, "slew_rate": 77}
    asyncio.run(run())



def test_mapped_export_resume_and_climate_override_refresh_without_deadlock(setup):
    from unittest.mock import AsyncMock, Mock

    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        setup.hass.async_create_task = asyncio.create_task
        coordinator = _coordinator_for_runtime_services(
            entry_data=DATA, options={**OPTIONS, CONF_DRY_RUN: False}, hass=setup.hass,
        )
        coordinator.store.data = setup.store.data
        coordinator.store.data["production"] = {"armed": True}
        await setup.control.save({"identity": runtime.target_identity(setup.hass, ENTITY),
                                  "pause": "external_export_limit_conflict"})
        plan = DryRunPlanner(OPTIONS).create_plan(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))
        coordinator._async_update_data_locked = AsyncMock(return_value=plan)
        coordinator._schedule_debounced_refresh = Mock()
        coordinator.async_request_refresh = coordinator._async_update_data
        await asyncio.wait_for(coordinator.async_resume_control(asset=EXPORT_ASSET), 1)
        await asyncio.wait_for(coordinator.async_set_manual_hvac_override(60, "operator"), 1)
        await coordinator.async_wait_for_plan_execution()
        assert coordinator.last_refresh_metadata["succeeded"] is True
        assert "pause" not in coordinator.store.data["ownership"][EXPORT_ASSET]
        assert not setup.calls
    asyncio.run(run())


@pytest.mark.parametrize("fresh_readback", [False, True])
def test_export_reconciliation_seeds_only_fresh_feedback_after_timing_save(
    setup, monkeypatch, fresh_readback,
):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        owner = _coordinator_for_runtime_services(
            entry_data=DATA, options={**OPTIONS, CONF_DRY_RUN: False}, hass=setup.hass,
        )
        owner.store = setup.store
        save_timing = owner.store.async_save_command_rate_limits

        async def save_then_wait(value):
            await save_timing(value)
            setup.clock["now"] += timedelta(minutes=11)
            if fresh_readback:
                setup.states[ENTITY] = limit_state(3000, readback=setup.clock["now"].timestamp())

        monkeypatch.setattr(owner.store, "async_save_command_rate_limits", save_then_wait)
        await owner._async_reconcile_export_limit()
        if fresh_readback:
            assert setup.control.state["expected"] == {"watts": 3000, "slew_rate": 100.0}
        else:
            assert not setup.control.state
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["timing_save", "seed_save", "reloaded_seed", "reloaded_unsupported"])
def test_export_initial_feedback_does_not_bind_a_replacement_mapping(setup, monkeypatch, stage):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        replacement = "sensor.replacement_limit"
        setup.registry[replacement] = deepcopy(setup.registry[ENTITY])
        setup.registry[replacement].device_id = "replacement-device"
        setup.states[replacement] = limit_state(3000)
        owner = _coordinator_for_runtime_services(
            entry_data=dict(DATA), options={**OPTIONS, CONF_DRY_RUN: False}, hass=setup.hass,
        )
        owner.store = setup.store

        def remap():
            owner.entry.data[CONF_ENPHASE_EXPORT_LIMIT_ENTITY] = replacement

        if stage.startswith("reloaded"):
            initial = {"identity": runtime.target_identity(setup.hass, ENTITY)}
            if stage == "reloaded_unsupported":
                initial["pause"] = "export_limit_unsupported"
            else:
                initial["expected"] = {"watts": None, "slew_rate": 100.0}
            await setup.control.save(initial)
            remap()
        else:
            method = "async_save_command_rate_limits" if stage == "timing_save" else "async_save_ownership"
            save = getattr(owner.store, method)

            async def save_then_remap(value):
                await save(value)
                remap()

            monkeypatch.setattr(owner.store, method, save_then_remap)
            await owner._async_reconcile_export_limit()
            assert not setup.control.state
            monkeypatch.setattr(owner.store, method, save)

        await owner._async_reconcile_export_limit()
        assert setup.control.state == {
            "identity": runtime.target_identity(setup.hass, replacement),
            "expected": {"watts": 3000, "slew_rate": 100.0},
        }
        assert not setup.calls

    asyncio.run(run())


def test_export_reconciliation_reads_initial_feedback_only_without_owned_state(setup, monkeypatch):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        owner = _coordinator_for_runtime_services(
            entry_data=DATA, options={**OPTIONS, CONF_DRY_RUN: False}, hass=setup.hass,
        )
        owner.store = setup.store
        ownership = {"identity": runtime.target_identity(setup.hass, ENTITY),
                     "expected": {"watts": None, "slew_rate": 100.0}}
        await setup.control.save(ownership)
        reads = Mock(wraps=module.export_limit_feedback)
        monkeypatch.setattr(module, "export_limit_feedback", reads)
        await owner._async_reconcile_export_limit()
        reads.assert_called_once_with(setup.hass, ENTITY, NOW)
        assert setup.control.state == ownership
        assert not setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("handoff", [
    "teardown", "startup_validation", "remap", "longer_cooldown", "longer_cooldown_due", "unchanged",
])
def test_deferred_export_restore_rechecks_authority_after_reservation_save(setup, monkeypatch, handoff):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        advance_clock(setup, 600 if handoff == "longer_cooldown_due" else 300)
        state = {**setup.control.state, "restoring": True}
        await setup.control.save(state)
        timing = deepcopy(setup.store.data["command_rate_limits"])
        owner = _coordinator_for_runtime_services(entry_data=dict(DATA), options=dict(OPTIONS), hass=setup.hass)
        owner.store = setup.store
        owner.executor.async_resolve_restore_notifications = AsyncMock()
        flush = owner.store.async_flush
        reservation_saved = False

        async def flush_then_handoff():
            nonlocal reservation_saved
            await flush()
            if reservation_saved or not setup.control.state.get("pending", {}).get("restore"):
                return
            reservation_saved = True
            if handoff == "teardown":
                owner._begin_shutdown()
            elif handoff == "startup_validation":
                owner._startup_auto_recovery_validation_active = True
            elif handoff == "remap":
                owner.entry.data[CONF_ENPHASE_EXPORT_LIMIT_ENTITY] = "sensor.replacement_limit"
            elif handoff in {"longer_cooldown", "longer_cooldown_due"}:
                owner.entry.options[CONF_COMMAND_RATE_LIMIT_SECONDS] = 600

        monkeypatch.setattr(owner.store, "async_flush", flush_then_handoff)
        await owner._async_reconcile_export_limit()
        assert reservation_saved
        if handoff == "unchanged":
            assert len(setup.calls) == 2
            assert not setup.control.state
        else:
            assert len(setup.calls) == 1
            assert setup.control.state == state
            assert setup.store.data["command_rate_limits"] == timing
            expected = {
                "remap": "export_limit_target_changed",
                "longer_cooldown": "export_limit_change_rate_limited",
                "longer_cooldown_due": "export_limit_cooldown_changed",
            }.get(handoff, "export_limit_restore_interrupted")
            assert setup.store.data["execution_audit"][-1]["reason"] == expected
            if handoff == "longer_cooldown_due":
                await owner._async_reconcile_export_limit()
                assert len(setup.calls) == 2
                assert not setup.control.state
                [record] = setup.store.data["command_rate_limits"].values()
                assert record["interval_seconds"] == 600

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["pending", "confirmed", "startup_validation"])
def test_background_export_restore_reconciles_pending_readback_once(setup, monkeypatch, stage):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        sent = advance_clock(setup)
        setup.mode["value"] = "pending"
        assert await setup.control.restore(sent) == (OutcomeResult.PENDING, "export_limit_pending", True)
        pending_state = setup.control.state
        now = advance_clock(setup, 10)
        if stage == "confirmed":
            setup.states[ENTITY] = limit_state(readback=now.timestamp())

        owner = _coordinator_for_runtime_services(
            entry_data=DATA, options={**OPTIONS, CONF_DRY_RUN: False}, hass=setup.hass,
        )
        owner.store = setup.store
        owner._startup_auto_recovery_validation_active = stage == "startup_validation"
        owner.executor.async_resolve_restore_notifications = AsyncMock()
        real_reconcile = runtime.ExportLimitControl.reconcile
        reconciled = []

        async def observe_reconcile(control, checked_at):
            reconciled.append(checked_at)
            return await real_reconcile(control, checked_at)

        monkeypatch.setattr(runtime.ExportLimitControl, "reconcile", observe_reconcile)
        await owner._async_reconcile_export_limit()

        assert reconciled == [now]
        assert len(setup.calls) == 2
        if stage == "confirmed":
            assert setup.control.state["expected"] == pending_state["baseline"]
            assert "baseline" not in setup.control.state
            assert "pending" not in setup.control.state
            owner.executor.async_resolve_restore_notifications.assert_awaited_once_with(EXPORT_ASSET)
        else:
            assert setup.control.state == pending_state
            owner.executor.async_resolve_restore_notifications.assert_not_awaited()

    asyncio.run(run())


def test_export_restore_readback_clears_previously_failed_restore_alert(setup):
    from test_coordinator import _coordinator_for_runtime_services

    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store = setup.store
        owner.executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=OPTIONS, entry_id="entry-a")
        await owner.executor._async_notify_restore(
            SimpleNamespace(result=OutcomeResult.FAILED, reason="export_limit_unconfirmed"), scope=EXPORT_ASSET,
        )
        setup.mode["value"] = "pending"
        outcome = await owner.executor.async_restore_device_control(EXPORT_ASSET, "retry")
        assert outcome.result == OutcomeResult.PENDING
        assert setup.store.data["restore_notification_failures"] == {EXPORT_ASSET: [EXPORT_ASSET]}
        setup.calls.clear()
        setup.states[ENTITY] = limit_state(None)
        await owner._async_reconcile_export_limit()
        assert setup.store.data["restore_notification_failures"] == {}
        assert ("persistent_notification", "dismiss", {
            "notification_id": "ha_energy_planner_restore_safe_state_enphase_export_limit_entry-a",
        }) in setup.calls

    asyncio.run(run())


@pytest.mark.parametrize("baseline", [0, None, 3000])
def test_calendar_retains_confirmed_restoration_between_tariff_actions_after_reload(setup, baseline):
    async def run():
        setup.states[ENTITY] = limit_state(baseline, slew=77)
        first = action(setup)
        if baseline == 0:
            first.kind = ActionKind.DISABLE_EXPORT_LIMIT
            first.execute_not_before = NOW
            first.desired_state["watts"] = None
        setup.mode["value"] = "confirm"
        await setup.control.execute(first, NOW)
        assert len(setup.store.data["calendar_history"]["history"]) == 1
        original_baseline = {"watts": baseline, "slew_rate": 77}
        assert setup.control.state["baseline"] == original_baseline

        requested = advance_clock(setup)
        setup.clock["now"] = requested
        setup.mode["value"] = "pending"
        assert await setup.control.restore(requested) == (OutcomeResult.PENDING, "export_limit_pending", True)
        pending = setup.control.state["pending"]
        assert pending["restore"] and pending["plan_id"] == "manual"
        assert setup.control.state["baseline"] == original_baseline
        assert len(setup.store.data["calendar_history"]["history"]) == 1

        # Reload with the original durable request. Neither pending upstream
        # feedback nor settled but pre-request readback establishes restoration.
        reloaded = Store()
        reloaded.data = deepcopy(setup.store.data)
        control = runtime.ExportLimitControl(setup.hass, reloaded, DATA)
        await control.restore(requested + timedelta(seconds=1))
        setup.states[ENTITY] = limit_state(baseline, slew=77, readback=NOW.timestamp())
        assert await control.reconcile(requested + timedelta(seconds=2)) == "export_limit_pending"
        assert control.state["baseline"] == original_baseline
        assert len(setup.calls) == 2

        confirmed = requested + timedelta(minutes=4)
        setup.clock["now"] = confirmed
        setup.states[ENTITY] = limit_state(baseline, slew=77, readback=confirmed.timestamp())
        assert await control.reconcile(confirmed) is None
        assert not control.state
        history = reloaded.data["calendar_history"]["history"]
        assert len(history) == 2
        restored = deepcopy(history[-1])
        setting = "Disabled" if baseline is None else f"Enabled — {baseline} W"
        assert restored["summary"] == f"Enphase Export Limit: Restored {setting} (confirmed)"
        assert restored["uid"] == f"{pending['action_id']}-{pending['sent_at']}"
        assert restored["start"] == restored["confirmed_start"] == confirmed.isoformat()
        assert datetime.fromisoformat(restored["end"]) - confirmed == timedelta(seconds=1)
        assert "not a running duration" in restored["description"]

        # Replaying feedback after an outcome was saved but before ownership
        # cleared must keep the first observed confirmation time and identity.
        await control.save(deepcopy(setup.control.state))
        assert await control.reconcile(confirmed + timedelta(seconds=1)) is None
        assert await control.reconcile(confirmed + timedelta(seconds=2)) is None
        assert (await control.restore(confirmed + timedelta(seconds=3)))[2] is False
        assert reloaded.data["calendar_history"]["history"] == history
        assert history[-1] == restored

        setup.mode["value"] = "confirm"
        second = deepcopy(first)
        second.action_id, second.plan_id = "next-tariff-action", "next-plan"
        await control.execute(second, advance_clock(setup))
        history = reloaded.data["calendar_history"]["history"]
        assert len(history) == 3
        assert history[0]["summary"] == history[2]["summary"]
        assert len({event["uid"] for event in history}) == 3
        assert control.state["baseline"] == original_baseline

        # A later restoration to the same baseline is a distinct real action.
        later = advance_clock(setup)
        setup.clock["now"] = later
        assert (await control.restore(later))[0] == OutcomeResult.RESTORED
        history = reloaded.data["calendar_history"]["history"]
        assert len(history) == 4 and len({event["uid"] for event in history}) == 4
        assert history[-1]["summary"] == restored["summary"]
        assert not control.state and len(setup.calls) == 4

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["pending", "timeout", "reject", "cancel"])
def test_unconfirmed_restoration_never_creates_calendar_activity(setup, mode):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        history = deepcopy(setup.store.data["calendar_history"]["history"])
        now = advance_clock(setup)
        setup.mode["value"] = mode
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await setup.control.restore(now)
        else:
            await setup.control.restore(now)
        assert setup.store.data["calendar_history"]["history"] == history
        assert setup.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
        assert (await setup.control.restore(now + timedelta(seconds=1)))[2] is False
        assert len(setup.calls) == 2

    asyncio.run(run())


def test_restoration_already_at_original_baseline_does_not_fabricate_calendar_action(setup):
    async def run():
        first = action(setup)
        setup.mode["value"] = "confirm"
        await setup.control.execute(first, NOW)
        # A normal tariff command can return to the original baseline before
        # release. Releasing that ownership is bookkeeping, not device action.
        second = deepcopy(first)
        second.action_id, second.kind = "return-to-baseline", ActionKind.DISABLE_EXPORT_LIMIT
        second.desired_state["watts"] = None
        await setup.control.execute(second, advance_clock(setup))
        history = deepcopy(setup.store.data["calendar_history"]["history"])
        assert len(history) == 2
        assert await setup.control.restore(setup.clock["now"]) == (
            OutcomeResult.RESTORED, "export_limit_restored", False)
        assert not setup.control.state and len(setup.calls) == 2
        assert setup.store.data["calendar_history"]["history"] == history

    asyncio.run(run())


@pytest.mark.parametrize("change", ["confirm", "manual", "restore", "unavailable", "stale", "target", "storage"])
def test_busy_refresh_reconciles_latest_export_state_once_after_command_release(setup, change):
    from unittest.mock import AsyncMock, Mock

    from test_coordinator import FakeStore, _coordinator_for_runtime_services, _plan

    async def run():
        # Start with a durable pending export request, as after a reload.
        await setup.control.execute(action(setup), NOW)
        owner = _coordinator_for_runtime_services(entry_data=dict(DATA), options=OPTIONS, hass=setup.hass)
        owner.store = setup.store
        owner.store.async_delay_save = FakeStore().async_delay_save
        owner._async_update_data_locked = AsyncMock(return_value=_plan("pending"))
        owner._schedule_debounced_refresh = Mock()
        owner.async_update_listeners = Mock()
        owner.executor.async_resolve_restore_notifications = AsyncMock()
        setup.hass.async_create_task = asyncio.create_task
        await owner._command_lock.acquire()
        for _ in range(5):
            await owner._async_update_data()
        worker = owner._export_reconcile_task
        await asyncio.sleep(0)
        assert not worker.done()
        assert owner.refresh_metrics["export_reconciliation_pending"] is True
        original_calls = len(setup.calls)
        if change == "manual":
            setup.states[ENTITY] = limit_state(700, slew=55)
            # Pending confirmation may not adopt conflicting readback until
            # its original deadline; it must never restore over a manual change.
        elif change == "restore":
            setup.states[ENTITY] = limit_state(0)
            state = setup.control.state
            state["restoring"] = True
            await setup.control.save(state)
            setup.mode["value"] = "confirm"
        elif change == "unavailable":
            setup.states[ENTITY].state = "unavailable"
        elif change == "stale":
            setup.states[ENTITY] = limit_state(0, readback=NOW.timestamp() - 900)
        elif change == "target":
            owner.entry.data[CONF_ENPHASE_EXPORT_LIMIT_ENTITY] = "sensor.replaced"
        elif change == "storage":
            setup.states[ENTITY] = limit_state(0)
            setup.store.fail = True
        else:
            setup.states[ENTITY] = limit_state(0)
        owner._command_lock.release()
        await asyncio.wait_for(worker, 1)
        assert owner._export_reconcile_task is None
        if change == "storage":
            assert owner._export_reconcile_pending is True
            owner._schedule_debounced_refresh.assert_not_called()
        else:
            assert owner._export_reconcile_pending is False
            owner._schedule_debounced_refresh.assert_called_once()
        if change == "restore":
            # Reconciliation confirms the original request first. Restoration
            # still waits for main's mandatory observed-confirmation cooldown.
            assert len(setup.calls) == original_calls
            assert setup.control.state["restoring"] is True
            advance_clock(setup)
            await owner._async_update_data()
            await owner._export_reconcile_task
            assert len(setup.calls) == original_calls + 1
            assert not setup.control.state
            # An ordinary refresh after recovery must not duplicate restoration.
            await owner._async_update_data()
            await owner._export_reconcile_task
            assert len(setup.calls) == original_calls + 1
        else:
            assert len(setup.calls) == original_calls
        if change in {"manual", "unavailable", "stale", "target"}:
            assert setup.control.state["pending"]
            assert setup.control.state["baseline"]["watts"] is None
        elif change == "confirm":
            assert "pending" not in setup.control.state
            assert setup.control.state["expected"]["watts"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("poll", ["later", "during_flush", "different_watts", "stale", "earlier", "gateway_pending",
                                  "planner_pending", "mapping_removed"])
@pytest.mark.parametrize("export_boundary", [False, True])
def test_completed_export_reconciliation_reuses_only_fresh_settled_settings(
    setup, monkeypatch, poll, export_boundary,
):
    from unittest.mock import AsyncMock, Mock

    from test_coordinator import _coordinator_for_runtime_services, _plan

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        if poll == "planner_pending":
            await setup.control.execute(action(setup), NOW)
            # The matching setting was read before dispatch. Only the next
            # timestamp can confirm this command despite identical watts.
            setup.states[ENTITY] = limit_state(0, readback=NOW.timestamp() - 1)
        elif poll == "gateway_pending":
            setup.states[ENTITY] = limit_state(pending=True)
        owner = _coordinator_for_runtime_services(entry_data=dict(DATA), options=OPTIONS, hass=setup.hass)
        owner.store.data = setup.store.data
        owner.store.async_add_outcome = setup.store.async_add_outcome
        setup.hass.async_create_task = asyncio.create_task
        schedule_refresh = owner._schedule_debounced_refresh
        followups = Mock()
        owner._schedule_debounced_refresh = lambda trigger, **kwargs: (
            schedule_refresh(trigger, **kwargs) if trigger == "export_tariff_boundary" else followups(trigger, **kwargs)
        )
        owner._async_update_data_locked = AsyncMock(return_value=_plan("poll"))

        if poll == "during_flush":
            async def flush():
                advance_clock(setup, 1)

            owner.store.async_flush = flush

        await owner._async_update_data()
        await owner._export_reconcile_task
        assert not owner._export_reconcile_pending
        if poll == "planner_pending":
            assert setup.control.state["pending"]

        if poll == "stale":
            setup.clock["now"] += timedelta(minutes=11)
        else:
            advance_clock(setup, 60)
        if poll == "different_watts":
            setup.states[ENTITY] = limit_state(3000, readback=setup.clock["now"].timestamp())
        elif poll == "earlier":
            setup.states[ENTITY].attributes["last_successful_readback"] = NOW.timestamp() - 1
        elif poll == "mapping_removed":
            owner.entry.data.pop(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)

        owner._async_update_data_locked.reset_mock()
        followups.reset_mock()
        if export_boundary:
            owner._export_boundary_cancel = None
            timers = Mock()
            monkeypatch.setattr(module, "async_call_later", timers)
            owner._schedule_export_boundary({"blocks": [{"end": setup.clock["now"].isoformat()}]})
            timers.call_args.args[2](setup.clock["now"])
            assert timers.call_args.args[1] == 0
            timers.call_args.args[2](setup.clock["now"])
            await asyncio.gather(*owner._listener_tasks)
            assert owner.refresh_requested == 1
            assert owner._pending_refresh_trigger == "export_tariff_boundary"
        else:
            owner._mark_forced_refresh("state_change")
        await owner._async_update_data()
        if poll in {"later", "during_flush", "mapping_removed"}:
            assert owner._export_reconcile_task is None
            assert not owner._export_reconcile_pending
            assert not owner._export_boundary_followup_due
            owner._async_update_data_locked.assert_awaited_once()
            followups.assert_not_called()
        else:
            assert owner._export_reconcile_pending
            await owner._export_reconcile_task
            followups.assert_called_once()
            expected_mode = module._RefreshMode.IMMEDIATE if export_boundary else module._RefreshMode.DEBOUNCED
            assert followups.call_args.kwargs["mode"] is expected_mode
            if poll == "planner_pending":
                assert "pending" not in setup.control.state
                assert len(setup.calls) == 1

    asyncio.run(run())


def test_new_export_mapping_cannot_reuse_a_missing_reconciliation_snapshot(setup):
    from test_coordinator import _coordinator_for_runtime_services

    owner = _coordinator_for_runtime_services(options=OPTIONS, hass=setup.hass)
    previous = owner._export_reconciliation_inputs()
    assert previous is None

    owner.entry.data = dict(DATA)
    current = owner._export_reconciliation_inputs()
    assert current is not None and current.issue is None
    assert not owner._export_reconciliation_matches(previous, current)


@pytest.mark.parametrize("timer_fired,preflight", [(False, False), (True, False), (True, True)])
def test_export_cooldown_wakeup_survives_inflight_reconciliation(setup, monkeypatch, timer_fired, preflight):
    from test_coordinator import _coordinator_for_runtime_services, _plan

    from custom_components.ha_energy_planner import coordinator as module

    async def run():
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY] = limit_state(0)
        await setup.control.reconcile(NOW)
        await setup.control.save({**setup.control.state, "restoring": True})
        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store.data = setup.store.data
        owner.store.async_add_outcome = setup.store.async_add_outcome
        refresh_lock = asyncio.Lock()
        planning, finish_planning = asyncio.Event(), asyncio.Event()

        async def plan(*, defer_execution, defer_publication):
            if preflight and not planning.is_set():
                planning.set()
                await finish_planning.wait()
            return _plan("cooldown")

        async def refresh():
            async with refresh_lock:
                await owner._async_update_data()

        async def request_refresh():
            # Model Home Assistant dropping the debounced callback when its
            # timer expires while the explicit refresh still holds the lock.
            if not refresh_lock.locked():
                await refresh()

        owner._async_update_data_locked = plan
        owner.async_refresh = refresh
        owner.async_request_refresh = request_refresh
        owner._listener_tasks = set()
        owner._export_boundary_cancel = None
        setup.hass.async_create_task = asyncio.create_task
        timers = []
        monkeypatch.setattr(module, "async_call_later", lambda hass, delay, callback: (
            timers.append((delay, callback)) or (lambda: None)))
        first_flush, finish = asyncio.Event(), asyncio.Event()

        async def flush():
            if not first_flush.is_set():
                first_flush.set()
                await finish.wait()
            await setup.store.async_flush()

        owner.store.async_flush = flush
        boundary = NOW + timedelta(seconds=300)
        owner._schedule_export_boundary({"deferred": True, "next_eligible_change_at": boundary.isoformat()})
        initial_refresh = None
        if preflight:
            initial_refresh = asyncio.create_task(owner._async_refresh_with_export_preflight("test_preflight"))
        else:
            await refresh()
        await asyncio.wait_for(first_flush.wait(), 1)
        worker = owner._export_reconcile_task
        assert len(setup.calls) == 1

        setup.clock["now"] = boundary  # The original gateway readback is still fresh.
        if timer_fired:
            timers.pop()[1](None)
            assert timers[-1][0] == 0
            timers.pop()[1](None)
            if preflight:
                # The boundary refresh waits for preflight, which still owns
                # the Core lock until the reconciliation flush is released.
                await asyncio.sleep(0)
                assert owner._listener_tasks
            else:
                await asyncio.gather(*owner._listener_tasks)
            assert owner._export_reconcile_task is worker
        else:
            # Input collection can replace a timer before its callback runs.
            owner._schedule_export_boundary({})
            timers.clear()
        owner._last_non_manual_refresh_requested_at = module.monotonic()
        finish.set()
        await worker
        assert owner._export_reconcile_pending
        setup.mode["value"] = "confirm"
        if preflight:
            # The original boundary request is already waiting on Core.
            assert not timers
        else:
            assert len(timers) == 1
            if timer_fired:
                assert timers[-1][0] == 0
                owner._schedule_debounced_refresh("state_change")
                assert timers[-1][0] == 0
            else:
                assert timers[-1][0] > 0
            timers.pop()[1](None)
        if initial_refresh is not None:
            await asyncio.wait_for(planning.wait(), 1)
            await asyncio.sleep(0)
            assert owner._listener_tasks  # The urgent request waits for the refresh lock.
            finish_planning.set()
            await initial_refresh
        await asyncio.gather(*owner._listener_tasks)
        if owner._export_reconcile_task is not None:
            await owner._export_reconcile_task
        assert len(setup.calls) == 2
        assert not setup.control.state
        owner._schedule_debounced_refresh("state_change")
        assert timers[-1][0] > 0

    asyncio.run(run())


@pytest.mark.parametrize(("change", "reason"), [
    ("pause", "export_limit_retry_interrupted"),
    ("disarm", "export_limit_retry_interrupted"),
    ("tariff", "export_tariff_changed"),
    ("tariff_in_place", "export_tariff_changed"),
    ("rollover", "export_tariff_expired"),
    ("unavailable", "export_limit_entity_unavailable"),
    ("startup", "export_limit_retry_interrupted"),
    ("replace", "export_limit_target_changed"),
    ("gateway", runtime.GATEWAY_CHANGED),
    ("verification_lost", runtime.GATEWAY_UNVERIFIED),
    ("future_readback", "export_limit_retry_readback_changed"),
    ("changed_feedback", "export_limit_retry_readback_changed"),
    ("cooldown_rollback", "export_limit_change_rate_limited"),
    ("production_evidence", "export_limit_retry_interrupted"),
])
def test_retry_rechecks_planner_authority_at_provider_write(setup, retry_owner, change, reason):
    from homeassistant.core import State

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        setup.store.data["ownership"][EXPORT_ASSET]["retry"]["attempts"] = 2
        owned_identity = setup.control.state["identity"]
        timing_key = runtime.control_key(owned_identity)
        now = advance_clock(setup)
        if change == "tariff_in_place":
            entity = DATA[CONF_AMBER_EXPORT_PRICE]
            raw = setup.states[entity]
            setup.states[entity] = State(entity, raw.state, raw.attributes, last_updated=now)
            assert runtime.evidence(setup.hass, DATA, OPTIONS, now)["blocks"][0]["watts"] == 0
        setup.mode["value"] = "confirm"
        waiting = asyncio.Event()
        release = asyncio.Event()
        original = retry_owner.upstream.async_apply_guarded.side_effect

        async def apply(watts, *, expected, before_write):
            waiting.set()
            await release.wait()
            await original(watts, expected=expected, before_write=before_write)

        retry_owner.upstream.async_apply_guarded.side_effect = apply
        task = asyncio.create_task(owner._async_retry_export_limit())
        await asyncio.wait_for(waiting.wait(), 1)
        if change == "pause":
            setup.store.data["control_pause"] = {
                "asset": EXPORT_ASSET, "until": (now + timedelta(minutes=10)).isoformat(), "reason": "operator",
            }
        elif change == "disarm":
            setup.store.data["production"]["armed"] = False
        elif change == "future_readback":
            setup.states[ENTITY].attributes["last_successful_readback"] = now.timestamp() + 1
        elif change == "changed_feedback":
            setup.states[ENTITY] = limit_state(5000, readback=now.timestamp())
        elif change == "cooldown_rollback":
            setup.clock["now"] = now - timedelta(seconds=1)
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].last_updated = setup.clock["now"]
            setup.states[ENTITY].attributes["last_successful_readback"] = setup.clock["now"].timestamp()
        elif change == "startup":
            owner._startup_auto_recovery_authorized = True
        elif change == "replace":
            setup.registry[ENTITY].device_id = "replacement"
        elif change == "gateway":
            setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
        elif change == "verification_lost":
            setup.states[ENTITY].attributes["gateway_identity_verified"] = False
        elif change == "unavailable":
            setup.states[ENTITY].state = "unavailable"
        elif change == "production_evidence":
            setup.store.data["production"]["dry_run_evidence_fingerprint"] = "changed"
        elif change == "rollover":
            now = advance_clock(setup, 15 * 60)
        elif change == "tariff_in_place":
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].attributes["forecasts"][0]["per_kwh"] = 0.15
        else:
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]] = price_state((0.15, 0.15))
            setup.states[DATA[CONF_AMBER_EXPORT_PRICE]].last_updated = now
        release.set()
        await task
        assert len(setup.calls) == 1
        assert not setup.control.state.get("pending")
        terminal = change in {"gateway", "verification_lost"}
        assert setup.control.state["retry"]["state"] == ("operator_required" if terminal else "scheduled")
        assert setup.control.state["retry"]["attempts"] == 2
        assert setup.control.state["retry"]["reason"] == reason
        if change == "replace":
            setup.registry[ENTITY].device_id = "gateway-a"
        assert setup.store.data["command_rate_limits"][timing_key]["attempted_at"] == NOW.timestamp()
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1

    asyncio.run(run())


def test_automatic_control_switch_off_revokes_retry_before_startup_cleanup(setup, retry_owner):
    from custom_components.ha_energy_planner.switch import SWITCHES, PlannerSwitch

    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        timing_key = runtime.control_key(setup.control.state["identity"])
        now = advance_clock(setup)
        setup.mode["value"] = "confirm"
        setup.hass.config_entries.async_update_entry = lambda entry, *, options: setattr(entry, "options", options)
        switch = PlannerSwitch(owner, next(description for description in SWITCHES if description.active_control))
        switch.async_write_ha_state = Mock()
        at_write, allow_write, cleaning_up, finish_cleanup = (asyncio.Event() for _ in range(4))
        original = retry_owner.upstream.async_apply_guarded.side_effect

        async def apply(watts, *, expected, before_write):
            at_write.set()
            await allow_write.wait()
            await original(watts, expected=expected, before_write=before_write)

        async def dismiss_notification():
            cleaning_up.set()
            await finish_cleanup.wait()

        retry_owner.upstream.async_apply_guarded.side_effect = apply
        owner.executor.async_dismiss_startup_recovery_notification = dismiss_notification
        retry = asyncio.create_task(owner._async_retry_export_limit())
        await asyncio.wait_for(at_write.wait(), 1)
        stop = asyncio.create_task(switch.async_turn_off())
        try:
            await asyncio.wait_for(cleaning_up.wait(), 1)
            allow_write.set()
            await asyncio.wait_for(retry, 1)
            assert len(setup.calls) == 1
            assert owner._export_retry_control() is None
            assert not setup.control.state.get("pending")
            assert setup.control.state["retry"]["attempts"] == 0
            assert setup.control.state["retry"]["reason"] == "export_limit_retry_interrupted"
            assert setup.store.data["command_rate_limits"][timing_key]["attempted_at"] == NOW.timestamp()
            assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1
        finally:
            allow_write.set()
            finish_cleanup.set()
            await asyncio.wait_for(stop, 1)
        assert owner.dry_run
        assert not owner.active_control

    asyncio.run(run())


def test_automatic_control_switch_off_revokes_ordinary_write_during_reservation(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        setup.mode["value"] = "confirm"
        setup.hass.config_entries.async_update_entry = lambda entry, *, options: setattr(entry, "options", options)
        reserved, finish_reservation, cleaning_up, finish_cleanup = (asyncio.Event() for _ in range(4))
        flush = setup.store.async_flush

        async def hold_reservation():
            await flush()
            if setup.control.state.get("pending") and not reserved.is_set():
                reserved.set()
                await finish_reservation.wait()

        async def dismiss_notification():
            cleaning_up.set()
            await finish_cleanup.wait()

        setup.store.async_flush = hold_reservation
        owner.executor.async_dismiss_startup_recovery_notification = dismiss_notification
        write = asyncio.create_task(owner.executor.async_execute_export_limit(action(setup), NOW))
        await asyncio.wait_for(reserved.wait(), 1)
        stop = asyncio.create_task(owner.async_set_active_control(False))
        try:
            await asyncio.wait_for(cleaning_up.wait(), 1)
            assert owner.dry_run
            finish_reservation.set()
            assert await asyncio.wait_for(write, 1) == (
                OutcomeResult.REJECTED, "export_limit_retry_interrupted", False,
            )
            assert not setup.control.state.get("pending")
            assert not setup.calls
            assert action_budget(budget_history(setup.store.data), {}, NOW, EXPORT_ASSET)["used"] == 0
        finally:
            finish_reservation.set()
            finish_cleanup.set()
            await asyncio.wait_for(stop, 1)

    asyncio.run(run())


@pytest.mark.parametrize("episode", [
    "settled", "scheduled", "checking", "ready", "terminal", "exhausted", "wrong_target", "invalid_attempts",
])
def test_startup_failure_only_preserves_eligible_export_retry(setup, retry_owner, episode):
    async def run():
        owner = retry_owner.create()
        setup.states[ENTITY] = limit_state(0)
        setup.store.data["production"]["armed"] = False
        setup.mode["value"] = "confirm"
        ownership = {
            "identity": runtime.target_identity(setup.hass, ENTITY),
            "gateway_identity": GATEWAY,
            "baseline": {"watts": None, "slew_rate": 100.0},
            "expected": {"watts": 0, "slew_rate": 100.0},
        }
        if episode != "settled":
            ownership["retry"] = {
                "state": "operator_required" if episode == "terminal" else
                "scheduled" if episode in {"exhausted", "wrong_target", "invalid_attempts"} else episode,
                "attempts": runtime.MAX_RETRY_ATTEMPTS if episode == "exhausted" else 1,
                "next_attempt_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
            if episode != "ready":
                ownership["pause"] = runtime.UNAVAILABLE_REJECTION
            if episode == "invalid_attempts":
                ownership["retry"]["attempts"] = "invalid"
        if episode == "wrong_target":
            ownership["identity"]["device_id"] = "old-gateway"
        await setup.control.prepare_cooldown(NOW)
        await setup.control.save(ownership)
        await owner.executor.async_restore_safe_state("startup_auto_recovery_replan_failed")
        writes = [call for call in setup.calls if call[0] == "enphase_ev"]

        if episode in {"scheduled", "checking", "ready", "terminal"}:
            assert setup.control.state == ownership
            assert not writes
        elif episode == "settled":
            assert not setup.control.state
            assert writes[0][1] == "disable_export_limit"
        else:
            assert setup.control.state["restoring"]
            assert setup.control.state["retry"]["state"] == "operator_required"
            assert not writes

    asyncio.run(run())


@pytest.mark.parametrize("failure", [
    "before_timeout", "after_timeout", "denial_cleanup", "startup_ready", "disabled", "cancelled", "provider_noop",
])
def test_retry_accounts_only_for_writes_that_reach_dispatch(setup, retry_owner, failure):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        state = setup.control.state
        state["retry"]["attempts"] = 3 if failure == "startup_ready" else 2
        if failure == "startup_ready":
            state["retry"].update(state="ready", next_attempt_at=None)
            state.pop("pause")
        await setup.control.save(state)
        now = advance_clock(setup)

        async def apply(watts, *, expected, before_write):
            if failure == "disabled":
                raise installer_rejection("export_limit_disabled")
            if failure == "cancelled":
                raise asyncio.CancelledError
            if failure == "provider_noop":
                setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
                return {}
            if failure in {"startup_ready", "denial_cleanup"}:
                setup.store.data["production"]["armed"] = False
                if failure == "denial_cleanup":
                    try:
                        before_write()
                    finally:
                        raise OSError("failed to clear pending intent")
                before_write()
            if failure == "after_timeout":
                before_write()
            raise TimeoutError

        retry_owner.upstream.async_apply_guarded.side_effect = apply
        if failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await owner._async_retry_export_limit()
        else:
            await owner._async_retry_export_limit()

        state = setup.control.state
        sent = failure == "after_timeout"
        assert bool(state.get("pending")) is sent
        assert action_budget(budget_history(setup.store.data), {}, now, EXPORT_ASSET)["used"] == 1 + sent
        assert setup.control.cooldown(now)["last_dispatch_reserved_at"] == (now if sent else NOW).timestamp()
        if failure == "provider_noop":
            assert "retry" not in state
        elif failure == "disabled":
            assert state["retry"]["attempts"] == 2
            assert state["retry"]["state"] == "operator_required"
        elif not sent:
            assert state["retry"]["attempts"] == (3 if failure == "startup_ready" else 2)
            assert state["retry"]["state"] == ("ready" if failure == "startup_ready" else "scheduled")
            assert state["retry"]["next_attempt_at"] == (now + timedelta(minutes=5)).isoformat()

    asyncio.run(run())


def test_startup_ready_retry_waits_for_competing_readback_after_restart(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)
        owned = setup.control.state
        owned["retry"].update(state="ready", attempts=3, next_attempt_at=None)
        owned.pop("pause")
        await setup.control.save(owned)
        claim = await setup.control.claim_retry(now)
        assert claim["rechecking_ready"]
        now = advance_clock(setup)
        setup.states[ENTITY] = limit_state(pending=True, readback=now.timestamp())
        restarted = retry_owner.create()
        await restarted._async_retry_export_limit()
        retry = setup.control.state["retry"]
        assert retry["state"] == "ready"
        assert retry["attempts"] == 3
        assert retry["reason"] == "export_limit_pending"
        assert not setup.control.state.get("pause")
        assert "rechecking_ready" not in retry
        retry_owner.upstream.async_prepare.assert_not_awaited()

    asyncio.run(run())



def test_interrupted_startup_ready_claim_preserves_budget_while_provider_is_pending(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        state = setup.control.state
        state["retry"].update(state="ready", attempts=3, next_attempt_at=None)
        state.pop("pause")
        await setup.control.save(state)

        now = advance_clock(setup)
        claim = await setup.control.claim_retry(now)
        assert claim["attempts"] == 3
        assert claim["state"] == "checking"

        # The persisted claim survives restart, but an upstream pending write
        # still blocks the resumed installer check.
        now = advance_clock(setup)
        assert await setup.control.claim_retry(now, pending=True) is None
        state = setup.control.state
        assert "pause" not in state
        assert state["retry"]["state"] == "ready"
        assert state["retry"]["attempts"] == 3
        assert state["retry"]["next_attempt_at"] == (now + timedelta(minutes=5)).isoformat()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_installer_probe_failure_counts_when_it_publishes_unavailable(setup, retry_owner, expired):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        rejection = (installer_rejection("export_limit_session_expired") if expired
                     else installer_rejection(cause=TimeoutError()))

        async def prepare():
            setup.states[ENTITY].state = "unavailable"
            raise rejection

        upstream = retry_owner.upstream
        upstream.async_prepare.side_effect = prepare
        for _ in range(4):
            now = advance_clock(setup)
            setup.states[ENTITY] = limit_state(readback=now.timestamp())
            await owner._async_retry_export_limit()

        retry = setup.control.state["retry"]
        assert retry["state"] == "operator_required"
        assert retry["attempts"] == upstream.async_prepare.await_count == (1 if expired else 3)
        assert setup.control.state["pause"] == (
            runtime.INSTALLER_SESSION_EXPIRED if expired else "export_limit_retry_exhausted"
        )
        upstream.async_apply_guarded.assert_not_awaited()

    asyncio.run(run())


def test_retry_cannot_settle_matching_watts_from_unverified_readback(setup, retry_owner):
    async def run():
        owner = retry_owner.create()
        await owner.executor.async_execute_export_limit(action(setup), NOW)
        now = advance_clock(setup)

        async def prepare_then_unverified():
            setup.states[ENTITY] = limit_state(0, readback=now.timestamp())
            snapshot = await retry_owner.prepare()
            setup.states[ENTITY].attributes["gateway_identity_verified"] = False
            return snapshot

        retry_owner.upstream.async_prepare.side_effect = prepare_then_unverified
        await owner._async_retry_export_limit()
        assert setup.control.state["retry"]["reason"] == runtime.GATEWAY_UNVERIFIED
        assert setup.control.state["baseline"] == {"watts": None, "slew_rate": 100.0}
        retry_owner.upstream.async_apply_guarded.assert_not_awaited()
        assert len(setup.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("same_target", [True, False])
def test_explicit_resume_retires_settled_baseline_only_for_same_control(setup, same_target):
    from test_coordinator import _coordinator_for_runtime_services

    async def run():
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        owned = setup.control.state
        assert owned["baseline"]["watts"] == 3000 and not owned.get("pending")
        timing = deepcopy(setup.store.data["command_rate_limits"])
        now = advance_clock(setup, 60)
        setup.states[ENTITY] = limit_state(5000, readback=now.timestamp())
        setup.states[ENTITY].attributes["gateway_identity"] = "b" * 64
        if not same_target:
            setup.registry[ENTITY].device_id = "different-control"

        owner = _coordinator_for_runtime_services(entry_data=DATA, options=OPTIONS, hass=setup.hass)
        owner.store = setup.store
        await owner.async_resume_control(asset=EXPORT_ASSET)
        assert setup.control.state == ({} if same_target else owned)
        assert setup.store.data["command_rate_limits"] == timing
        assert len(setup.calls) == 1

    asyncio.run(run())


def test_legacy_ready_retry_without_baseline_requires_resume(setup):
    async def run():
        setup.states[ENTITY] = limit_state(0)
        await setup.control.save({
            "identity": runtime.target_identity(setup.hass, ENTITY),
            "expected": {"watts": 0, "slew_rate": 100.0},
            "retry": {"state": "ready", "attempts": 1, "next_attempt_at": None},
        })
        assert await setup.control.reconcile(NOW) == runtime.GATEWAY_UNVERIFIED
        assert setup.control.state["retry"]["state"] == "ready"
        await setup.control.resume()
        assert not setup.control.state

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["confirm", "restore", "retry"])
def test_reconciliation_rechecks_gateway_after_alias_save(select_setup, operation):
    setup = select_setup

    async def run():
        control = runtime.ExportLimitControl(setup.hass, setup.store, setup.mapping)
        state = {"identity": runtime.target_identity(setup.hass, setup.sensor), "gateway_identity": GATEWAY}
        if operation == "retry":
            state["retry"] = {"state": "ready", "attempts": 1, "next_attempt_at": None}
        else:
            state["baseline"] = {"watts": None, "slew_rate": 100.0}
            state["pending"] = {
                "action_id": "alias-request", "plan_id": "alias-plan", "kind": "set_export_limit",
                "identity": state["identity"], "sent_at": NOW.timestamp(), "confirmation_not_before": NOW.timestamp(),
                "attempt_recorded": True, "watts": 0, "slew_rate": None, "restore": operation == "restore",
                "pre_state": {"watts": None, "slew_rate": 100.0},
            }
        await control.save(state)
        setup.states[setup.sensor] = limit_state(0)
        save = setup.store.async_save_ownership

        async def save_then_replace(ownership):
            await save(ownership)
            setup.states[setup.sensor].attributes["gateway_identity"] = "b" * 64

        setup.store.async_save_ownership = save_then_replace
        assert await control.reconcile(NOW) == runtime.GATEWAY_CHANGED
        assert control.state == {**state, "identity": runtime.target_identity(setup.hass, setup.select)}
        assert not setup.store.data["execution_audit"]

    asyncio.run(run())


def test_device_toggle_saves_recovery_before_disarm_can_reach_disk(setup, retry_owner):
    from test_coordinator import FakeExecutor

    from custom_components.ha_energy_planner.const import CONF_ENPHASE_CONTROL_ENABLED

    async def run():
        owner = retry_owner.create({CONF_ENPHASE_CONTROL_ENABLED: True})
        owner.executor = FakeExecutor()
        owner._startup_auto_recovery_authorized = True
        await owner._async_update_startup_auto_recovery("restoring", successful_runs=1, reason="starting")
        await owner.async_arm_production_control("startup_auto_recovered", provisional=True)
        owner.entry.options[CONF_ENPHASE_CONTROL_ENABLED] = False

        async def lose_power_before_restoration():
            raise OSError("interrupted option transition")

        owner.executor.async_persist_ev_grid_reservation = lose_power_before_restoration
        with pytest.raises(OSError, match="interrupted option transition"):
            await owner.async_handle_options_update()
        setup.store.data = deepcopy(setup.store.data)
        assert setup.store.data["production"]["armed"] is False
        assert "startup_activation_pending" not in setup.store.data["production"]

        restarted = retry_owner.create({CONF_ENPHASE_CONTROL_ENABLED: False})
        assert await restarted.async_reconcile_production_evidence_contract()
        assert restarted._startup_auto_recovery_authorized
        assert not restarted.active_control
        assert not (await restarted.executor.async_execute_export_limit(action(setup), NOW))[2]
        assert not setup.calls

    asyncio.run(run())
