"""Independent export tariff policy and gateway-confirmed ownership regressions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.ha_energy_planner import enphase_export_limit as runtime
from custom_components.ha_energy_planner.action_limits import action_budget
from custom_components.ha_energy_planner.const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_DRY_RUN,
    CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
    CONF_PLANNER_ENABLED,
    DEFAULT_OPTIONS,
)
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
        },
    )


class Store:
    def __init__(self):
        self.data = {"ownership": {}, "production": {"armed": True, "dry_run_ready_cycles": 3}, "execution_audit": []}
        self.flushes = 0
        self.fail = False

    async def async_save_ownership(self, value):
        self.data["ownership"] = deepcopy(value)

    async def async_flush(self):
        self.flushes += 1
        if self.fail:
            raise OSError("disk failed")

    async def async_add_outcome(self, value):
        self.data["execution_audit"].append(to_jsonable(value))

    async def async_save_command_rate_limits(self, value):
        self.data["command_rate_limits"] = value


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

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr("homeassistant.util.dt.utcnow", lambda: NOW)
    return SimpleNamespace(
        hass=hass,
        store=store,
        states=states,
        registry=registry,
        calls=calls,
        mode=mode,
        control=runtime.ExportLimitControl(hass, store, DATA),
    )


def context(evidence):
    return DecisionContext(
        NOW,
        "export-plan",
        [DecisionSlot(NOW, 0.2, -0.1, 5.0, 1.0)],
        None,
        None,
        OccupancyState.UNKNOWN,
        InputHealth.UNSAFE,
        export_limit=evidence,
    )


def action(setup):
    return build_actions(context(runtime.evidence(setup.hass, DATA, OPTIONS, NOW)))[0]


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


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda s: setattr(s, "state", "unavailable"), "export_tariff_unavailable"),
        (lambda s: setattr(s, "last_updated", NOW - timedelta(minutes=31)), "export_tariff_stale"),
        (lambda s: s.attributes.update(confidence=0.1), "export_tariff_confidence_low"),
        (lambda s: s.attributes.update(forecasts=[-1]), "export_tariff_invalid"),
        (lambda s: s.attributes["forecasts"][0].update(start_time="2026-10-01T00:00:00"), "export_tariff_invalid"),
        (lambda s: s.attributes["forecasts"][0].update(per_kwh=float("nan")), "export_tariff_invalid"),
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
        ({"last_successful_readback": None}, "export_limit_readback_invalid"),
        ({"last_successful_readback": NOW.timestamp() - 601}, "export_limit_readback_stale"),
        ({"slew_rate": 0}, "export_limit_unsupported"),
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


@pytest.mark.parametrize("failure", [None, "missing", "entry", "device", "stale", "unavailable"])
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
    observed, issue = runtime.feedback(setup.hass, setup.select, NOW)
    assert (issue is None) is (failure is None)
    assert runtime.evidence(setup.hass, setup.mapping, OPTIONS, NOW)["ready"] is (failure is None)
    if failure is None:
        assert observed["watts"] is None
        assert observed["identity"]["entity_id"] == setup.select
    assert runtime.feedback_entity_id(setup.hass, "select.missing") is None


@pytest.mark.parametrize("mapped_profile", [False, True])
def test_select_export_only_execution_never_changes_profile(select_setup, mapped_profile):
    from custom_components.ha_energy_planner.const import CONF_ENPHASE_CONTROL_ENABLED, CONF_ENPHASE_PROFILE
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    setup = select_setup

    async def run():
        mapping = dict(setup.mapping)
        if mapped_profile:
            mapping[CONF_ENPHASE_PROFILE] = "select.system_profile"
            setup.states["select.system_profile"] = SimpleNamespace(state="AI Optimisation", attributes={})
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
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.RESTORED
        assert setup.calls[-1] == ("enphase_ev", "disable_export_limit", {
            "entity_id": setup.select, "slew_rate": 100.0,
        })
        assert all(domain == "enphase_ev" and service in {"set_export_limit", "disable_export_limit"}
                   for domain, service, _ in setup.calls)
        if mapped_profile:
            assert setup.states["select.system_profile"].state == "AI Optimisation"

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
        assert (await setup.control.restore(NOW))[2] is True
        assert setup.calls[-1][2]["slew_rate"] == 77
        # Readback must confirm the original slew as well as its enabled/watts state.
        assert not setup.control.state
        assert setup.store.data["execution_audit"][-1]["result"] == "restored"

    asyncio.run(run())


def test_manual_change_surrenders_baseline(setup):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY] = limit_state(5000)
        assert await setup.control.reconcile(NOW) == "external_export_limit_conflict"
        assert "baseline" not in setup.control.state
        assert (await setup.control.restore(NOW))[2] is False
        assert len(setup.calls) == 1
        await setup.control.resume()
        assert not setup.control.state
        await setup.control.execute(action(setup), NOW)
        assert setup.control.state["baseline"]["watts"] == 5000

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
            assert await clone.reconcile(NOW + timedelta(minutes=10)) == "export_limit_unconfirmed"
            assert await clone.reconcile(NOW + timedelta(minutes=10)) == "export_limit_unconfirmed"
            # A fresh late readback resolves the command, retaining the explicit pause.
            setup.states[ENTITY] = limit_state(0, readback=(NOW + timedelta(minutes=11)).timestamp())
            assert await clone.reconcile(NOW + timedelta(minutes=11)) == "export_limit_unconfirmed"
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
        assert await setup.control.reconcile(NOW + timedelta(minutes=11)) == "export_limit_unconfirmed"
        setup.states[ENTITY] = limit_state(0, readback=(NOW + timedelta(minutes=11)).timestamp())
        await setup.control.reconcile(NOW + timedelta(minutes=11))
        await setup.control.resume()
        setup.states[ENTITY] = limit_state(0)
        setup.mode["value"] = "reject"
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.FAILED
        assert setup.control.state["restoring"]
        await setup.control.resume()
        setup.mode["value"] = "confirm"
        await setup.control.restore(NOW)
        assert not setup.control.state

    asyncio.run(run())


def test_restore_already_baseline_and_unsupported_owned_state(setup):
    async def run():
        observed, _ = runtime.feedback(setup.hass, ENTITY, NOW)
        await setup.control.save({"identity": observed["identity"], "baseline": {"watts": None, "slew_rate": 100}})
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
        setup.hass.services.has_service = lambda *args: False
        assert not CapabilityDiscovery(setup.hass, mapping, OPTIONS).inspect().for_asset(EXPORT_ASSET).supported

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
        monkeypatch.setattr(module, "async_call_later", lambda hass, delay, cb: (
            callbacks.append((delay, cb)) or (lambda: cancelled.append(True))))
        coordinator._schedule_export_boundary(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
        assert callbacks[-1][0] == 20 * 60
        coordinator._schedule_export_boundary(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
        assert cancelled == [True]
        callbacks[-1][1](NOW)
        assert coordinator._force_next_refresh
        assert coordinator._pending_refresh_trigger == "export_tariff_boundary"
        coordinator._schedule_export_boundary({})
        coordinator._schedule_export_boundary(runtime.evidence(setup.hass, DATA, OPTIONS, NOW))
        task_lifecycle._begin_shutdown(coordinator)
        assert coordinator._export_boundary_cancel is None

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


@pytest.mark.parametrize("stage", ["seed", "pending", "restore", "review", "unsupported", "off_restore", "off"])
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
        ctx = context({} if stage in {"off_restore", "off"} else runtime.evidence(setup.hass, DATA, opts, NOW))
        monkeypatch.setattr(module, "InputManager", lambda *args, **kwargs: SimpleNamespace(
            current_forecast_observations=lambda: {}, build_context=lambda overrides: ctx,
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
        result = await coordinator._async_update_data_locked(defer_execution=True)
        if "history_training" in coordinator.__dict__ and coordinator.history_training.task:
            await coordinator.history_training.task
        if stage == "off":
            assert EXPORT_ASSET not in result.control_area_health
            assert boundaries == [{}]
            assert not result.actions
            return
        assert result.control_area_health[EXPORT_ASSET]
        if stage == "seed":
            assert EXPORT_ASSET in coordinator.store.data["ownership"]
        elif stage != "review":
            assert not result.control_area_health[EXPORT_ASSET]["ready"]
        if stage == "off_restore":
            assert result.device_plans[EXPORT_ASSET]["ownership"]["restoring"]
            assert result.device_plans[EXPORT_ASSET]["current_state_label"] == "Pending"
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
            setup.states.pop(ENTITY)

        setup.store.async_flush = remove_target
        assert await setup.control.execute(a, NOW) == (OutcomeResult.REJECTED, "export_limit_entity_unavailable", False)
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
        await setup.control.save({"identity": observed["identity"], "baseline": {"watts": 3000, "slew_rate": 100},
                                  "restoring": True})
        assert (await setup.control.execute(a, NOW))[1] == "export_limit_restoration_pending"
        assert not setup.calls

    asyncio.run(run())


def test_delayed_restoration_confirmation_and_readback_recheck(setup, monkeypatch):
    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        setup.mode["value"] = "pending"
        await setup.control.restore(NOW)
        setup.states[ENTITY] = limit_state()
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.RESTORED
        setup.states[ENTITY] = limit_state(3000)
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
        original = setup.control.reconcile

        async def disappearing(now):
            result = await original(now)
            setup.states.pop(ENTITY)
            return result

        monkeypatch.setattr(setup.control, "reconcile", disappearing)
        assert (await setup.control.restore(NOW))[1] == "export_limit_entity_unavailable"

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
    "change", ["unarmed", "dry_run", "mapping", "registry", "pending", "external", "expired", "tariff"])
def test_dispatch_rechecks_after_persistence(setup, monkeypatch, change):
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint

    async def run():
        opts = {**OPTIONS, CONF_DRY_RUN: False}
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
        assert await setup.control.reconcile(NOW + timedelta(minutes=10)) == "export_limit_unconfirmed"
        assert setup.control.state["pending"]
        setup.states[ENTITY].attributes["pending"] = False
        assert runtime.feedback(setup.hass, ENTITY, NOW)[1] == "export_limit_unsupported"

    asyncio.run(run())


def test_external_slew_change_during_pending_surrenders_ownership(setup):
    async def run():
        await setup.control.execute(action(setup), NOW)
        setup.states[ENTITY] = limit_state(0, slew=77)
        assert await setup.control.reconcile(NOW) == "external_export_limit_conflict"
        assert "baseline" not in setup.control.state and "pending" not in setup.control.state
        assert setup.store.data["execution_audit"][-1]["result"] == "rejected"
        assert (await setup.control.restore(NOW))[0] == OutcomeResult.SKIPPED
        assert len(setup.calls) == 1
        await setup.control.resume()
        setup.states["sensor.export_price"] = price_state((0, 1, 1))
        await setup.control.execute(action(setup), NOW)
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
def test_conflicting_declared_cadence_cannot_authorize_a_write(explicit):
    state = price_state((-1, 1), explicit=explicit)
    state.attributes["forecast_interval_minutes"] = 60
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
def test_device_toggle_during_startup_grace_preserves_other_ownership(setup, disabled):
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
        owner.async_start_startup_auto_recovery = lambda: None
        owner.async_restore_safe_state = AsyncMock()
        setup.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, options: setattr(entry, "options", options))
        option = CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED if disabled == EXPORT_ASSET else CONF_ENPHASE_CONTROL_ENABLED
        await owner.async_set_device_control(option, False)
        owner.async_restore_safe_state.assert_not_awaited()
        assert owner.executor.device_restores == [(disabled, f"{disabled}_control_disabled")]
        assert owner.store.data["production"]["armed"]

    asyncio.run(run())


def test_pending_export_restoration_does_not_mask_failed_profile_restore(setup):
    from custom_components.ha_energy_planner.executor import Executor

    async def run():
        setup.mode["value"] = "confirm"
        await setup.control.execute(action(setup), NOW)
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
        original_flush = setup.store.async_flush

        async def change_target():
            await original_flush()
            if race == "unavailable":
                setup.states.pop(ENTITY)
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
        setup.mode["value"] = mode
        executor = Executor(setup.store, hass=setup.hass, entry_data=DATA, options=OPTIONS)
        result = await executor.async_restore_device_control(EXPORT_ASSET, "operator_stop")
        assert result.result == (OutcomeResult.RESTORED if mode == "confirm" else OutcomeResult.FAILED)
        last = setup.store.data["execution_audit"][-2]
        assert last["kind"] == "restore_export_limit" and last["service_target"] == ENTITY
        assert last["result"] == ("restored" if mode == "confirm" else "failed")

    asyncio.run(run())
