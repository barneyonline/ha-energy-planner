"""Economic climate regression tests using deterministic measurements and tariffs."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.climate_economics import site_cost
from custom_components.ha_energy_planner.climate_inputs import (
    climate_identity,
    finite,
    instant,
    read_climate_inputs,
    validate_climate_config,
)
from custom_components.ha_energy_planner.climate_learning import (
    cop_at,
    electrical_power,
    fit_humidity,
    fit_physical,
    fit_rooms,
    neighbours,
    observe,
    quantile,
    ridge,
    temperature_step,
    train_climate,
    validate,
)
from custom_components.ha_energy_planner.climate_models import ClimateTrajectory, SiteCost
from custom_components.ha_energy_planner.climate_optimizer import comfort_valid, optimise, revalidate_schedule, simulate
from custom_components.ha_energy_planner.climate_runtime import economic_actions, update_readiness
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    InputHealth,
    OccupancyState,
    Override,
)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def context(count=6):
    return DecisionContext(
        NOW,
        "economic-test",
        [
            DecisionSlot(NOW + timedelta(minutes=5 * i), 0.5, 0.1, 0.0, 0.0, outdoor_temperature_forecast_c=10)
            for i in range(count)
        ],
        None,
        None,
        OccupancyState.OCCUPIED,
        InputHealth.HEALTHY,
        current_hvac_mode="heat",
        current_hvac_temperature_c=21.0,
        current_hvac_power_kw=1.0,
        current_outdoor_temperature_c=10,
        occupied_temperature_low_c=20,
        occupied_temperature_high_c=24,
        climate_inputs={
            "identity": "id",
            "target": 21.0,
            "load_excludes_hvac": True,
            "currency": "AUD",
            "configuration_valid": True,
        },
    )


def row(at=NOW, **values):
    return {
        "at": at.isoformat(),
        "temperature": 21.0,
        "outdoor": 10.0,
        "power_kw": 1.0,
        "mode": "heat",
        "occupied": "occupied",
        "low": 20.0,
        "high": 24.0,
        "target": 21.0,
        "provenance": "normal",
        **values,
    }


def physical_rows(mode="heat", days=2):
    rows = []
    for day in range(days):
        temp = 21.0
        for i in range(48):
            power = 0.5 + (i % 4) * 0.4 if mode != "off" else 0
            outdoor = 10 + i % 3 if mode != "cool" else 30 + i % 3
            at = NOW - timedelta(days=days - day) + timedelta(minutes=i * 5)
            rows.append(
                row(
                    at,
                    temperature=temp,
                    outdoor=outdoor,
                    power_kw=power,
                    mode=mode,
                    humidity=50.0,
                    irradiance=float(i % 4) * 100,
                )
            )
            temp += (0.1 * (outdoor - temp) + (2 if mode != "cool" else -2) * power) / 12
    return rows


def ready_model():
    return {
        "identity": "id",
        "baseline_version": 1,
        "validation_version": 1,
        "physical": {"version": 1},
        "trained_at": NOW.isoformat(),
        "validation": {
            mode: {"blockers": [], "temperature_p90": 0, "energy_error": 0, "last_window_at": NOW.isoformat()}
            for mode in ("heat", "cool")
        },
    }


@pytest.mark.parametrize("value", [None, True, False, "nan", float("inf"), object()])
def test_nonfinite_inputs_are_not_numbers(value):
    assert finite(value) is None


def test_absolute_times_and_identity():
    assert finite("2.5") == 2.5
    assert instant("invalid") is None
    assert instant("2026-01-01") is None
    assert instant(NOW) == NOW
    assert climate_identity({}, {}) == climate_identity({}, {})
    assert climate_identity({"daikin_climate_entity": "climate.a"}, {}) != climate_identity({}, {})


@pytest.mark.parametrize(
    "data",
    [
        {"hvac_maximum_humidity": 0},
        {"hvac_maximum_humidity": True},
        {"hvac_cop_table": []},
        {"hvac_cop_table": {"dry": []}},
        {"hvac_cop_table": {"heat": []}},
        {"hvac_cop_table": {"heat": [{}, {}]}},
        {"hvac_cop_table": {"heat": [{"temperature": 10, "cop": 2}, {"temperature": 0, "cop": 3}]}},
        {"hvac_zone_mappings": []},
        {"hvac_zone_mappings": {"climate.a": {}}},
        {"climate_zone_entities": ["climate.a"], "hvac_zone_mappings": {"climate.a": {"temperature": "switch.a"}}},
        {"climate_zone_entities": ["climate.a"], "hvac_zone_mappings": {"climate.a": {"maximum_humidity": 101}}},
    ],
)
def test_optional_configuration_rejects_invalid_contracts(data):
    assert validate_climate_config(data)


def test_optional_configuration_valid():
    assert not validate_climate_config(
        {
            "climate_zone_entities": "climate.a",
            "hvac_zone_mappings": {"climate.a": {"temperature": "sensor.a", "maximum_humidity": 60}},
            "hvac_cop_table": {"heat": [{"temperature": 0, "cop": 2}, {"temperature": 10, "cop": 3}]},
        }
    )


def test_read_optional_inputs_and_units():
    states = {
        "climate.main": SimpleNamespace(attributes={"temperature": 22, "target_temp_step": 1}, state="heat"),
        "sensor.rh": SimpleNamespace(state="50", attributes={"unit_of_measurement": "%"}, last_updated=NOW),
        "sensor.temp": SimpleNamespace(state="68", attributes={"unit_of_measurement": "°F"}, last_updated=NOW),
        "sensor.sun": SimpleNamespace(state="200", attributes={"unit_of_measurement": "W/m²"}, last_updated=NOW),
        "sensor.arrival": SimpleNamespace(state=(NOW + timedelta(hours=1)).isoformat(), attributes={}),
        "sensor.price": SimpleNamespace(state="0.5", attributes={"unit_of_measurement": "AUD/kWh"}),
        "sensor.forecast": SimpleNamespace(
            state="1",
            attributes={
                "issued_at": NOW.isoformat(),
                "forecast": [
                    None,
                    {"valid_at": NOW.isoformat(), "irradiance": 300},
                    {"valid_at": "bad", "irradiance": 2},
                ],
            },
        ),
    }
    data = {
        "daikin_climate_entity": "climate.main",
        "hvac_humidity_entity": "sensor.rh",
        "hvac_irradiance_entity": "sensor.sun",
        "hvac_irradiance_forecast_entity": "sensor.forecast",
        "hvac_arrival_entity": "sensor.arrival",
        "amber_import_price_entity": "sensor.price",
        "climate_zone_entities": ["climate.main"],
        "hvac_zone_mappings": {"climate.main": {"temperature": "sensor.temp"}},
    }
    result = read_climate_inputs(
        SimpleNamespace(states=SimpleNamespace(get=states.get)),
        data,
        DEFAULT_OPTIONS,
        NOW,
        NOW + timedelta(hours=2),
        {"hvac_power_subtracted": True},
    )
    assert result["zones"]["climate.main"]["temperature"] == 20
    assert result["humidity"] == 50
    assert result["irradiance"] == 200
    assert result["arrival"] == states["sensor.arrival"].state
    assert len(result["irradiance_forecast"]) == 1
    assert result["load_excludes_hvac"]
    states["sensor.rh"].last_updated = NOW - timedelta(days=1)
    states["sensor.arrival"].state = NOW.isoformat()
    result = read_climate_inputs(
        SimpleNamespace(states=SimpleNamespace(get=states.get)),
        data,
        DEFAULT_OPTIONS,
        NOW,
        NOW + timedelta(hours=2),
        {},
    )
    assert result["humidity"] is None and result["arrival"] is None


def test_observations_tag_ownership_manual_and_washout():
    ctx = context()
    state = observe({}, ctx, 20)
    assert state["observations"][-1]["provenance"] == "normal"
    assert len(observe(state, ctx, 20)["observations"]) == 1
    ctx.created_at += timedelta(minutes=5)
    ctx.hvac_control = {"phase": "preconditioning"}
    state = observe(state, ctx, 20)
    assert state["observations"][-1]["provenance"] == "planner"
    ctx.created_at += timedelta(minutes=5)
    ctx.hvac_control = {}
    state = observe(state, ctx, 20)
    assert state["observations"][-1]["provenance"] == "washout"
    ctx.created_at += timedelta(minutes=25)
    ctx.active_overrides = [Override("manual_hvac", "user", None, "manual")]
    state = observe(state, ctx, 20)
    assert state["observations"][-1]["provenance"] == "manual"
    ctx.climate_inputs["identity"] = "changed"
    assert len(observe(state, ctx, 20)["observations"]) == 1
    ctx.current_hvac_power_kw = None
    assert observe({}, ctx, 20)["observations"] == []


def test_neighbours_exclude_future_interventions_and_incompatible_conditions():
    earlier = [row(NOW - timedelta(days=7 * day)) for day in range(1, 7)]
    prediction = neighbours(earlier, row(), "UTC")
    assert prediction and prediction.power_kw == pytest.approx(1)
    assert prediction.active_fraction == 1
    assert neighbours([row()], row(), "UTC") is None
    assert neighbours(earlier, row(at=datetime(2026, 9, 13)), "UTC") is None
    for key, value in [
        ("provenance", "planner"),
        ("occupied", "away"),
        ("mode", "cool"),
        ("outdoor", 40),
        ("temperature", 40),
        ("at", "invalid"),
    ]:
        assert neighbours([{**item, key: value} for item in earlier], row(), "UTC") is None
    off = [{**item, "mode": "off", "power_kw": 0, "target": None} for item in earlier]
    assert neighbours(off, row(mode="off"), "UTC").power_kw == 0


def test_physical_model_solver_and_supported_predictions():
    coefficients = ridge([[1, 0], [0, 1], [1, 1]], [2, 3, 5])
    assert coefficients == pytest.approx([2, 3], abs=0.02)
    assert ridge([], []) is None
    assert quantile([], 0.9) == 0
    for mode in ("heat", "cool", "off"):
        rows = physical_rows(mode)
        fitted = fit_physical(rows)
        assert mode in fitted
        sample = rows[-1]
        assert temperature_step(fitted, sample, 1 / 12) is not None
        assert temperature_step(fitted, {**sample, "outdoor": 100}, 1 / 12) is None
    assert fit_physical([]) == {"version": 1}


def test_humidity_rooms_and_cop_calibration():
    rows = physical_rows()
    assert fit_humidity(rows)["ready"]
    assert fit_humidity([]) == {}
    for item in rows:
        item["zones"] = {"climate.room": {"temperature": item["temperature"], "humidity": 50}}
    assert "heat" in fit_rooms(rows)["climate.room"]["physical"]
    table = [{"temperature": 0, "cop": 2}, {"temperature": 20, "cop": 4}]
    assert cop_at(table, 10) == 3
    assert cop_at(table, -1) is None
    assert electrical_power(rows, "heat", 10, table) > 0
    assert electrical_power([], "heat", 10, []) is None


def test_insufficient_validation_never_qualifies():
    result = validate([], "heat", "UTC", 30)
    assert "history_days" in result.blockers and "active_recall" in result.blockers
    trained = train_climate({"identity": "id", "observations": []}, "UTC", 30, NOW)
    assert trained["identity"] == "id"
    assert trained["validation"]["cool"]["blockers"]


def test_site_energy_counts_exports_and_negative_prices():
    ctx = context(2)
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS).cost == pytest.approx(1 / 12)
    ctx.slots[0].pv_forecast_kw = 2
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS).export_kwh == pytest.approx(1 / 12)
    ctx.slots[1].import_price = -0.5
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS).cost < 0
    ctx.climate_inputs["load_excludes_hvac"] = False
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS) is None


def test_battery_soc_reserve_loss_and_opaque_profile():
    ctx = context(2)
    ctx.current_battery_soc_percent = 50
    ctx.current_enphase_profile = "opaque"
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS) is None
    ctx.current_enphase_profile = ctx.enphase_self_consumption_profile = "self"
    result = site_cost(ctx, (1, 1), DEFAULT_OPTIONS)
    assert result.import_kwh == 0
    assert result.terminal_battery_kwh < 5 - 1 / 6
    ctx.current_battery_soc_percent = 10
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS).import_kwh == pytest.approx(1 / 6)
    ctx.slots[0].pv_forecast_kw = 5
    assert site_cost(ctx, (0, 0), DEFAULT_OPTIONS).terminal_battery_kwh > 1


@pytest.mark.parametrize("change", ["missing", "negative", "gap", "length"])
def test_site_rejects_incomplete_inputs(change):
    ctx = context(2)
    powers = (1, 1)
    if change == "missing":
        ctx.slots[0].import_price = None
    if change == "negative":
        ctx.slots[0].pv_forecast_kw = -1
    if change == "gap":
        ctx.slots[1].valid_at += timedelta(minutes=5)
    if change == "length":
        powers = (1,)
    assert site_cost(ctx, powers, DEFAULT_OPTIONS) is None


def test_readiness_requires_two_daily_passes_and_degrades():
    ctx = context()
    model = ready_model()
    state = update_readiness({}, model, ctx, DEFAULT_OPTIONS)
    assert state["status"] == "learning"
    assert update_readiness(state, model, ctx, DEFAULT_OPTIONS)["status"] == "learning"
    ctx.created_at += timedelta(days=1)
    model["trained_at"] = ctx.created_at.isoformat()
    state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
    assert state["status"] == "active" and state["ever_active"]
    assert (
        update_readiness(state, model, ctx, {**DEFAULT_OPTIONS, "hvac_decision_policy": "observe"})["status"]
        == "ready_observing"
    )
    assert (
        update_readiness(state, model, ctx, {**DEFAULT_OPTIONS, "hvac_decision_policy": "legacy"})["status"]
        == "disabled"
    )
    ctx.current_hvac_power_kw = None
    assert update_readiness(state, model, ctx, DEFAULT_OPTIONS)["status"] == "degraded"
    ctx.created_at += timedelta(days=15)
    assert update_readiness(state, model, ctx, DEFAULT_OPTIONS)["status"] == "degraded"


def test_hard_comfort_boundaries_and_arrival():
    ctx = context(2)
    baseline = ClimateTrajectory((21, 21), (1, 1))
    assert comfort_valid(ctx, baseline, baseline, DEFAULT_OPTIONS)
    assert not comfort_valid(ctx, ClimateTrajectory((21, 25), (1, 1)), baseline, DEFAULT_OPTIONS)
    ctx.occupancy_state = OccupancyState.AWAY
    assert comfort_valid(ctx, ClimateTrajectory((19, 19), (0, 0)), baseline, DEFAULT_OPTIONS)
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=10)).isoformat()
    assert not comfort_valid(ctx, ClimateTrajectory((19, 19), (0, 0)), baseline, DEFAULT_OPTIONS)
    ctx.current_hvac_temperature_c = 19
    assert not comfort_valid(ctx, ClimateTrajectory((18, 19), (0, 0)), baseline, DEFAULT_OPTIONS)


def test_optimizer_fails_closed_without_models_or_load_provenance():
    ctx = context()
    result, diagnostics = optimise(ctx, DEFAULT_OPTIONS, {})
    assert result is None and diagnostics["rejected"]["heat_model_not_ready"]
    ctx.climate_inputs["load_excludes_hvac"] = False
    assert optimise(ctx, DEFAULT_OPTIONS, {})[1]["rejected"]["household_load_hvac_provenance_missing"]
    ctx.climate_inputs["load_excludes_hvac"] = True
    ctx.climate_inputs["zones"] = {"climate.room": {}}
    assert optimise(ctx, DEFAULT_OPTIONS, {})[1]["rejected"]["zone_or_humidity_model_not_ready"]
    assert simulate(ctx, {}, DEFAULT_OPTIONS, mode="heat") is None
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, {}, {}) is None


def test_economic_runtime_learning_legacy_and_observation():
    ctx = context()
    assert economic_actions(ctx, DEFAULT_OPTIONS, []) == []
    assert ctx.climate_decision["status"] == "learning"
    ctx.climate_engine = {"status": "active", "ever_active": True}
    assert economic_actions(ctx, DEFAULT_OPTIONS, []) == []
    ctx.hvac_control = {"economic_policy_version": 1}
    actions = economic_actions(ctx, DEFAULT_OPTIONS, [])
    assert actions[0].kind == ActionKind.RELEASE_HVAC
    assert actions[0].asset == ActionAsset.DAIKIN
    ctx.hvac_control = {}
    ctx.climate_inputs = {}
    assert economic_actions(ctx, DEFAULT_OPTIONS, []) == []


def simulation_model():
    observations = [
        row(
            NOW - timedelta(days=7 * day) + timedelta(minutes=minute),
            temperature=temp,
            power_kw=max(0, (23 - temp) * 0.3),
            target=21,
        )
        for day in range(1, 7)
        for minute in range(-30, 61, 5)
        for temp in (20, 21, 22, 23, 24)
    ]
    return {
        **ready_model(),
        "normal": observations,
        "physical": {
            mode: {
                "coefficients": [0.05, 1.5 if mode == "heat" else 0],
                "outdoor_low": 0,
                "outdoor_high": 40,
                "enhanced": False,
                "active_power": 1.0,
            }
            for mode in ("heat", "off")
        },
        "validation": {
            "heat": {"blockers": [], "temperature_p90": 0, "energy_error": 0},
            "cool": {"blockers": ["not_ready"]},
        },
    }


def test_real_candidate_search_saves_money_and_restores_thermal_state():
    ctx = context(6)
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    model = simulation_model()
    baseline = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat")
    assert baseline is not None
    candidate, decision = optimise(ctx, {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}, model)
    assert candidate is not None
    assert candidate.expected_saving > 0
    assert candidate.conservative_saving > 0
    assert abs(candidate.trajectory.temperatures[-1] - baseline.temperatures[-1]) <= 0.25
    assert decision["currency"] == "AUD"
    assert decision["baseline"]["cost"] > decision["candidate"]["cost"]
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, model, decision) is not None


def test_real_economic_actions_keep_one_lifecycle_and_one_monetary_benefit():
    ctx = context(6)
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    ctx.climate_engine = {
        "status": "active",
        "ever_active": True,
        "model": simulation_model(),
        "modes": {"heat": {"ready": True}},
    }
    options = {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}
    actions = economic_actions(ctx, options, [])
    assert len(actions) == 3
    identity = actions[0].desired_state["lifecycle_id"]
    assert sum(action.expected_cost_delta is not None for action in actions) == 1
    repeated = economic_actions(ctx, options, [])
    assert repeated[0].desired_state["lifecycle_id"] == identity
    assert ctx.climate_engine["opportunities"] == 1
    ctx.hvac_control = deepcopy(actions[0].desired_state)
    repeated = economic_actions(ctx, options, [])
    assert repeated[0].desired_state["lifecycle_id"] == identity
    assert all(action.expected_cost_delta is None for action in repeated)


def test_chronological_validation_uses_complete_windows_and_earlier_days():
    rows = physical_rows(days=16)
    result = validate(rows, "heat", "UTC", 30)
    assert result.windows > 0 and result.active_episodes > 0
    assert result.days == 16
    assert result.temperature_mae < 999
    assert result.state_accuracy > 0
    corrupted = deepcopy(rows)
    corrupted[-4]["at"] = "invalid"
    corrupted[-8]["provenance"] = "manual"
    assert validate(corrupted, "heat", "UTC", 30).windows <= result.windows


def test_missing_sensor_units_and_datetime_helpers():
    item = SimpleNamespace(state="10", attributes={"unit_of_measurement": "bad"}, last_updated=NOW)
    arrival = SimpleNamespace(state="unknown", attributes={"timestamp": (NOW + timedelta(hours=1)).timestamp()})
    states = {"sensor.a": item, "input_datetime.arrival": arrival}
    data = {"hvac_humidity_entity": "sensor.a", "hvac_arrival_entity": "input_datetime.arrival"}
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    result = read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=2), {})
    assert result["arrival"] and result["humidity"] is None
    arrival.attributes["timestamp"] = 1e100
    assert read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=2), {})["arrival"] is None
    data["hvac_zone_mappings"] = {"bad": {}}
    assert not read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=2), {})[
        "configuration_valid"
    ]


def test_battery_configuration_missing_soc_and_backup_policy():
    ctx = context(2)
    ctx.climate_inputs["battery_configured"] = True
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS) is None
    ctx.current_battery_soc_percent = 50
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS) is None
    ctx.current_enphase_profile = ctx.enphase_full_backup_profile = "backup"
    assert site_cost(ctx, (1, 1), DEFAULT_OPTIONS).import_kwh == pytest.approx(1 / 6)


def test_matched_baseline_stays_off_when_normal_controls_stay_off():
    ctx = context()
    ctx.current_hvac_mode = "off"
    model = simulation_model()
    model["normal"] = [{**item, "power_kw": 0, "mode": "off"} for item in model["normal"]]
    trajectory = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat")
    assert trajectory and sum(trajectory.powers_kw) == 0
    assert optimise(ctx, DEFAULT_OPTIONS, model)[0] is None


def test_humidity_and_zone_constraints_use_shared_power():
    ctx = context()
    model = simulation_model()
    ctx.climate_inputs.update(humidity=50, maximum_humidity=60)
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    model["humidity"] = {"ready": True, "coefficients": [0, 0, 0, 0], "residual": 0}
    valid = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat", conservative=True)
    assert valid and valid.humidities == (50,) * 6
    ctx.climate_inputs["humidity"] = 65
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    ctx.climate_inputs["humidity"] = 50
    model["humidity"]["coefficients"][0] = 200
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    model["humidity"]["coefficients"][0] = 0
    ctx.climate_inputs["zones"] = {"climate.room": {"temperature": 21, "low": 20, "high": 24, "occupied": True}}
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    model["rooms"] = {"climate.room": {"physical": model["physical"], "humidity": model["humidity"]}}
    room = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat", conservative=True)
    assert room and room.powers_kw == valid.powers_kw
    assert "climate.room" in room.zones
    ctx.climate_inputs["zones"]["climate.room"]["low"] = 23
    predicted = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat")
    assert predicted is not None and comfort_valid(ctx, predicted, predicted, DEFAULT_OPTIONS)
    ctx.climate_inputs["zones"]["climate.room"].update(low=20, maximum_humidity=60)
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    ctx.climate_inputs["zones"]["climate.room"]["humidity"] = 50
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is not None
    ctx.climate_inputs["zones"]["climate.room"]["temperature"] = None
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None


def test_simulator_rejects_missing_weather_solar_demand_and_comfort():
    ctx = context()
    model = simulation_model()
    ctx.slots[0].outdoor_temperature_forecast_c = None
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    ctx.slots[0].outdoor_temperature_forecast_c = 10
    model["physical"]["heat"]["enhanced"] = True
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    model["physical"]["heat"]["enhanced"] = False
    model["physical"]["heat"]["outdoor_low"] = 11
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    ctx.occupied_temperature_low_c = None
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    assert not comfort_valid(ctx, ClimateTrajectory((), ()), ClimateTrajectory((), ()), DEFAULT_OPTIONS)


def test_observation_period_is_persisted_and_not_counted_per_refresh():
    ctx = context(6)
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    ctx.climate_engine = {
        "status": "active",
        "ever_active": True,
        "model": simulation_model(),
        "modes": {"heat": {"ready": True}},
        "opportunities": 9,
    }
    options = {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}
    assert economic_actions(ctx, options, []) == []
    assert ctx.climate_engine["opportunities"] == 10
    assert ctx.climate_decision["observing"]
    assert economic_actions(ctx, options, []) == []
    assert ctx.climate_engine["opportunities"] == 10
    assert ctx.climate_engine["observation_prediction"]["estimated"]


def test_command_time_authority_cannot_be_forged_by_a_saving():
    from custom_components.ha_energy_planner.climate_runtime import command_rejection

    options = dict(DEFAULT_OPTIONS)
    identity = climate_identity({}, options)
    desired = {
        "economic_policy_version": 1,
        "configuration_identity": identity,
        "lifecycle_id": "one",
        "period_end": NOW + timedelta(hours=1),
    }
    engine = {"identity": identity, "status": "active", "scheduled": {"lifecycle_id": "one"}}
    assert command_rejection(None, {}, options, {}, {}, NOW) is None
    assert command_rejection(None, {}, options, {}, desired, NOW) == "economic_climate_not_active"
    assert (
        command_rejection(None, {"daikin_climate_entity": "climate.changed"}, options, engine, desired, NOW)
        == "economic_climate_configuration_changed"
    )
    assert (
        command_rejection(None, {}, options, {**engine, "scheduled": {}}, desired, NOW)
        == "economic_climate_schedule_superseded"
    )
    assert command_rejection(None, {}, options, engine, desired, NOW) is None
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda key: None))
    assert command_rejection(hass, {}, options, engine, desired, NOW) is None
    assert (
        command_rejection(hass, {}, options, engine, {**desired, "period_end": NOW}, NOW)
        == "economic_climate_window_ended"
    )
    assert (
        command_rejection(hass, {}, options, engine, {**desired, "arrival": NOW.isoformat()}, NOW)
        == "economic_climate_arrival_changed"
    )


def test_revalidation_rejects_changed_identity_expiry_and_incomplete_horizon():
    ctx = context()
    schedule = {
        "start": NOW.isoformat(),
        "stop": (NOW + timedelta(minutes=5)).isoformat(),
        "release": (NOW + timedelta(minutes=20)).isoformat(),
        "identity": "changed",
        "mode": "heat",
        "target": 22,
    }
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, {}, schedule) is None
    schedule["identity"] = "id"
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, {}, schedule) is None
    schedule["release"] = (NOW + timedelta(days=1)).isoformat()
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, {}, schedule) is None


def test_physical_training_rejects_bad_pairs_and_wrong_direction():
    rows = physical_rows()
    rows[1]["at"] = "bad"
    rows[5]["temperature"] = 200
    rows[10]["irradiance"] = None
    assert fit_physical(rows)
    broken = physical_rows()
    for item in broken:
        item["temperature"] = 42 - item["temperature"]
    assert "heat" not in fit_physical(broken)
    assert ridge([[0]], [0]) == [0]
    assert fit_humidity([{**item, "humidity": None} for item in rows]) == {}
    active = [row(NOW - timedelta(minutes=i * 5), power_kw=1) for i in range(30)]
    table = [{"temperature": 0, "cop": 2}, {"temperature": 20, "cop": 4}]
    assert electrical_power(active, "heat", 10, table) == pytest.approx(1)


def test_solver_failure_and_validation_missing_predictions(monkeypatch):
    from custom_components.ha_energy_planner import climate_learning as module

    assert ridge([[1e20, 1e20]], [1]) is None
    rows = physical_rows(days=16)
    monkeypatch.setattr(module, "neighbours", lambda *args: None)
    assert validate(rows, "heat", "UTC", 30).windows == 0
    monkeypatch.undo()
    monkeypatch.setattr(module, "temperature_step", lambda *args: None)
    assert validate(rows, "heat", "UTC", 30).windows == 0
    monkeypatch.undo()
    monkeypatch.setattr(module, "ridge", lambda *args: None)
    assert fit_humidity(rows) == {}
    monkeypatch.undo()
    monkeypatch.setattr(module, "ridge", lambda *args: [0.1, -1])
    assert "heat" not in fit_physical(physical_rows())
    monkeypatch.setattr(module, "ridge", lambda *args: [0.1, 1])
    assert "cool" not in fit_physical(physical_rows("cool"))
    assert (
        cop_at(
            [{"temperature": 0, "cop": 2}, {"temperature": float("nan"), "cop": 3}, {"temperature": 20, "cop": 4}], 10
        )
        is None
    )


def test_invalid_forecast_and_unsupported_operating_state():
    ctx = context()
    model = simulation_model()
    ctx.occupied_temperature_low_c = None
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["comfort_inputs_unavailable"]
    ctx.occupied_temperature_low_c = 20
    ctx.slots[0].outdoor_temperature_forecast_c = None
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["baseline_unsupported"]
    ctx.slots[0].outdoor_temperature_forecast_c = 10
    ctx.slots[0].pv_forecast_kw = None
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["site_energy_evidence_missing"]
    ctx.slots[0].pv_forecast_kw = 0
    ctx.climate_inputs["target"] = None
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["normal_target_missing"]
    ctx.climate_inputs["target"] = 21
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=5)).isoformat()
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat")
    model["normal"] = []
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    ctx.current_hvac_temperature_c = 18
    baseline = ClimateTrajectory((18, 19, 19.5, 20, 21, 21), (1,) * 6)
    slower = ClimateTrajectory((17, 18, 19, 20, 21, 21), (1,) * 6)
    assert not comfort_valid(ctx, slower, baseline, DEFAULT_OPTIONS)


def test_candidate_constraints_fail_closed_when_cost_evidence_changes(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module

    ctx = context(4)
    model = simulation_model()
    reference = ClimateTrajectory((21,) * 4, (1,) * 4)
    modified = ClimateTrajectory((21,) * 4, (2,) * 4)
    monkeypatch.setattr(module, "simulate", lambda *args, **kw: modified if "start" in kw else reference)
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["insufficient_saving"]
    monkeypatch.setattr(module, "comfort_valid", lambda *args: False)
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["comfort_limit"]
    monkeypatch.setattr(module, "comfort_valid", lambda *args: True)
    real_cost = module.site_cost
    monkeypatch.setattr(
        module, "site_cost", lambda c, p, o, **kw: None if p == modified.powers_kw else real_cost(c, p, o, **kw)
    )
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["site_energy_evidence_missing"]
    monkeypatch.setattr(
        module, "site_cost", lambda c, p, o, **kw: SiteCost(0, 0, 0, 0, 1 if p == modified.powers_kw else 2)
    )
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]["rejected"]["terminal_battery_deficit"]
    schedule = {
        "start": NOW.isoformat(),
        "stop": (NOW + timedelta(minutes=5)).isoformat(),
        "release": (NOW + timedelta(minutes=10)).isoformat(),
        "identity": "id",
        "mode": "heat",
        "target": 22,
    }
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, model, schedule) is None
    monkeypatch.setattr(module, "site_cost", lambda *a, **kw: None)
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, model, schedule) is None
    monkeypatch.setattr(
        module, "simulate", lambda *args, **kw: ClimateTrajectory((25,) * 4, (1,) * 4) if "start" in kw else reference
    )
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, model, schedule) is None


def test_readiness_keeps_authority_for_one_daily_accuracy_failure():
    ctx = context()
    model = ready_model()
    state = update_readiness({}, model, ctx, DEFAULT_OPTIONS)
    ctx.created_at += timedelta(days=1)
    model["trained_at"] = ctx.created_at.isoformat()
    state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
    assert state["status"] == "active"
    for values in model["validation"].values():
        values["blockers"] = ["energy_error"]
    for expected in ["active", "degraded"]:
        ctx.created_at += timedelta(days=1)
        model["trained_at"] = ctx.created_at.isoformat()
        state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
        assert state["status"] == expected


def test_observation_closure_distinguishes_actual_from_predicted_energy():
    from custom_components.ha_energy_planner.climate_learning import finish_observation

    state = {
        "observation_started_at": NOW.isoformat(),
        "observation_until": (NOW + timedelta(minutes=10)).isoformat(),
        "observation_prediction": {
            "baseline_powers_kw": [1, 1],
            "baseline_slots": [NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat()],
        },
    }
    rows = [row(NOW + timedelta(minutes=i * 5)) for i in range(3)]
    finish_observation(state, rows, NOW + timedelta(minutes=10))
    assert state["comparisons"][0]["actual_hvac_kwh"] == pytest.approx(1 / 6)
    assert state["comparisons"][0]["energy_absolute_error_kwh"] == 0
    finish_observation(state, rows, NOW + timedelta(minutes=15))
    assert len(state["comparisons"]) == 1
    state.pop("observation_closed_at")
    rows[1]["provenance"] = "manual"
    finish_observation(state, rows, NOW + timedelta(minutes=15))
    assert not state["comparisons"][-1]["valid"]


def test_unknown_policy_and_stale_command_sensor():
    from custom_components.ha_energy_planner.climate_runtime import command_rejection

    assert (
        command_rejection(None, {}, {}, {}, {"economic_policy_version": 2}, NOW)
        == "economic_climate_policy_version_unknown"
    )
    data = {"hvac_humidity_entity": "sensor.humidity"}
    identity = climate_identity(data, DEFAULT_OPTIONS)
    state = {"status": "active", "identity": identity, "scheduled": {"lifecycle_id": "one"}}
    desired = {
        "economic_policy_version": 1,
        "configuration_identity": identity,
        "lifecycle_id": "one",
        "period_end": NOW + timedelta(hours=1),
    }
    sensor = SimpleNamespace(state="50", attributes={"unit_of_measurement": "%"}, last_updated=NOW - timedelta(days=1))
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: sensor))
    assert (
        command_rejection(hass, data, DEFAULT_OPTIONS, state, desired, NOW) == "economic_climate_live_evidence_missing"
    )


def test_economic_lifecycle_not_interpreted_by_legacy_policy():
    from custom_components.ha_energy_planner.executor import Executor
    from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy

    ctx = context()
    ctx.hvac_control = {"economic_policy_version": 1}
    assert HVACPlanningPolicy(DEFAULT_OPTIONS, {})._hvac_lifecycle_actions(ctx, NOW, NOW + timedelta(minutes=5)) == []
    executor = Executor(SimpleNamespace(data={}), options=DEFAULT_OPTIONS)
    action = SimpleNamespace(desired_state={"economic_policy_version": 1})
    assert executor._control_rejection_reason(action, NOW) == "economic_climate_not_active"


def test_runtime_revalidates_coasting_and_handles_policy_transitions():
    ctx = context(6)
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    ctx.climate_engine = {
        "status": "active",
        "ever_active": True,
        "model": simulation_model(),
        "modes": {"heat": {"ready": True}},
    }
    options = {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}
    actions = economic_actions(ctx, options, [])
    ctx.hvac_control = deepcopy(actions[0].desired_state)
    ctx.climate_engine["observation_until"] = (NOW + timedelta(hours=1)).isoformat()
    assert economic_actions(ctx, options, [])[0].kind == ActionKind.RELEASE_HVAC
    ctx.climate_engine.pop("observation_until")
    ctx.climate_engine["scheduled"]["identity"] = "changed"
    assert economic_actions(ctx, options, [])[0].kind == ActionKind.RELEASE_HVAC
    ctx.hvac_control = {}
    ctx.climate_engine["scheduled"] = {}
    ctx.climate_engine["status"] = "ready_observing"
    assert economic_actions(ctx, options, []) == []
    ctx.climate_engine["status"] = "active"
    ctx.hvac_control = {"phase": "preconditioning"}
    assert economic_actions(ctx, options, []) == []


def test_input_numeric_temperature_and_presence_mapping():
    states = {
        "sensor.temp": SimpleNamespace(state="20", attributes={"unit_of_measurement": "°C"}, last_updated=NOW),
        "binary_sensor.present": SimpleNamespace(state="on", attributes={}, last_updated=NOW),
    }
    data = {
        "climate_zone_entities": ["climate.room"],
        "hvac_zone_mappings": {"climate.room": {"temperature": "sensor.temp", "presence": "binary_sensor.present"}},
    }
    result = read_climate_inputs(
        SimpleNamespace(states=SimpleNamespace(get=states.get)),
        data,
        DEFAULT_OPTIONS,
        NOW,
        NOW + timedelta(hours=1),
        {},
    )
    assert result["zones"]["climate.room"]["temperature"] == 20
    assert result["zones"]["climate.room"]["occupied"] is True


def test_climate_fixture_contracts_and_replay():
    import json
    import runpy
    from pathlib import Path

    from custom_components.ha_energy_planner.replay import run_replay_file

    root = Path(__file__).resolve().parents[1]
    for script, fixture in [
        ("validate-live-schema-fixture.py", "live_schema/climate_optional_inputs.json"),
        ("validate-real-history-fixture.py", "history/climate_provenance.json"),
    ]:
        validator = runpy.run_path(str(root / "scripts" / script))["_validate_fixture"]
        data = json.loads((root / "tests/fixtures" / fixture).read_text())
        assert validator(data)["ok"]
        data["expected"] = {key: None for key in data["expected"]}
        with pytest.raises(ValueError, match="Climate fixture mismatch"):
            validator(data)
    replay = run_replay_file(root / "tests/fixtures/replay/climate_economic_authority.json")
    assert replay.rejected_action_count == 1
    assert [v.code for v in replay.action_results[0].violations] == ["economic_climate_authority_missing"]


def test_owned_coast_preserves_original_period_boundaries_after_refresh():
    ctx = context(6)
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    ctx.climate_engine = {
        "status": "active",
        "ever_active": True,
        "model": simulation_model(),
        "modes": {"heat": {"ready": True}},
    }
    options = {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}
    actions = economic_actions(ctx, options, [])
    ctx.hvac_control = deepcopy(actions[0].desired_state)
    scheduled_stop = instant(ctx.climate_engine["scheduled"]["stop"])
    steps = int((scheduled_stop - NOW).total_seconds() / 300)
    ctx.current_hvac_temperature_c = ctx.climate_decision["temperatures"][steps - 1]
    ctx.created_at = scheduled_stop
    ctx.slots = ctx.slots[steps:]
    continued = economic_actions(ctx, options, [])
    assert all(action.desired_state.get("phase") != "preconditioning" for action in continued)
    assert continued[0].desired_state["period_start"] == scheduled_stop
    assert continued[0].desired_state["lifecycle_id"] == actions[0].desired_state["lifecycle_id"]


def test_room_readiness_and_unoccupied_humidity_prediction():
    ctx = context()
    model = simulation_model()
    ctx.climate_inputs["zones"] = {
        "climate.room": {"temperature": 21, "humidity": 50, "maximum_humidity": 60, "occupied": False}
    }
    assert (
        "room_model_not_ready:climate.room"
        in update_readiness({}, model, ctx, DEFAULT_OPTIONS)["modes"]["heat"]["blockers"]
    )
    model["rooms"] = {
        "climate.room": {
            "physical": model["physical"],
            "days": 14,
            "validation": {"heat": {"blockers": [], "temperature_p90": 0}},
            "humidity": {"ready": True, "coefficients": [0, 0, 0, 0], "residual": 0},
        }
    }
    assert (
        "room_model_not_ready:climate.room"
        not in update_readiness({}, model, ctx, DEFAULT_OPTIONS)["modes"]["heat"]["blockers"]
    )
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat", conservative=True)
    assert optimise(ctx, DEFAULT_OPTIONS, model)[1]
    model["rooms"]["climate.room"]["humidity"]["coefficients"][0] = 200
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None


def test_stale_solar_forecast_and_missing_declared_sensor_are_not_fresh():
    data = {"hvac_irradiance_forecast_entity": "sensor.forecast", "hvac_humidity_entity": "sensor.missing"}
    state = SimpleNamespace(state="1", attributes={"issued_at": (NOW - timedelta(days=1)).isoformat(), "forecast": []})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: state if entity == "sensor.forecast" else None))
    result = read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=1), {})
    assert result["irradiance_forecast"] == []
    assert result["sources"]["sensor.missing"]["fresh"] is False


def test_outdoor_humidity_feature_is_used_only_with_available_evidence():
    from custom_components.ha_energy_planner.climate_learning import humidity_rate

    rows = [{**r, "outdoor_humidity": 45} for r in physical_rows()]
    humidity = fit_humidity(rows)
    assert humidity["outdoor"]
    assert humidity_rate(humidity, 21, 1, 50, 45) == 0
    assert humidity_rate(humidity, 21, 1, 50, None) is None
    weather = SimpleNamespace(state="cloudy", attributes={"humidity": 45}, last_updated=NOW)
    data = {"weather_entity": "weather.home"}
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: weather))
    assert read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=1), {})["outdoor_humidity"] == 45
    weather.attributes["humidity"] = 150
    assert (
        read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=1), {})["outdoor_humidity"] is None
    )
    ctx = context()
    model = simulation_model()
    model["humidity"] = humidity
    ctx.climate_inputs.update(humidity=50, maximum_humidity=60)
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None
    ctx.climate_inputs["zones"] = {"climate.room": {"temperature": 21, "humidity": 50, "maximum_humidity": 60}}
    ctx.climate_inputs.pop("maximum_humidity")
    model["rooms"] = {"climate.room": {"physical": model["physical"], "humidity": humidity}}
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is None


def test_conservative_savings_allow_lower_normal_consumption():
    from custom_components.ha_energy_planner.climate_optimizer import lower_baseline_power

    model = simulation_model()
    model["validation"]["heat"]["energy_error"] = 0.2
    lower = lower_baseline_power(model, "heat", (1, 2))
    assert lower == pytest.approx((0.8, 1.6))
    assert site_cost(context(2), lower, DEFAULT_OPTIONS).cost < site_cost(context(2), (1, 2), DEFAULT_OPTIONS).cost
    assert lower_baseline_power({}, "heat", (1, 2)) == (0, 0)
    data = {"hvac_cop_table": {"heat": [{"temperature": "0", "cop": "2"}, {"temperature": "10", "cop": "3"}]}}
    parsed = read_climate_inputs(
        SimpleNamespace(states=SimpleNamespace(get=lambda key: None)),
        data,
        DEFAULT_OPTIONS,
        NOW,
        NOW + timedelta(hours=1),
        {},
    )
    assert cop_at(parsed["cop_table"]["heat"], 5) == 2.5


@pytest.mark.parametrize(
    "mode,phase,temperature,handoff",
    [
        ("heat", "peak_coast", 20, True),
        ("heat", "peak_coast", 24, True),
        ("cool", "peak_coast", 20, True),
        ("cool", "peak_coast", 24, True),
        ("heat", "preconditioning", 20, False),
        ("heat", "preconditioning", 24, True),
        ("cool", "preconditioning", 20, True),
        ("cool", "preconditioning", 24, False),
    ],
)
def test_economic_comfort_handoff_matches_existing_directional_safety(mode, phase, temperature, handoff):
    ctx = context()
    ctx.current_hvac_temperature_c = temperature
    ctx.hvac_control = {
        "economic_policy_version": 1,
        "phase": phase,
        "mode": mode,
        "precondition_end": NOW + timedelta(minutes=5),
        "period_end": NOW + timedelta(minutes=20),
    }
    result = economic_actions(ctx, DEFAULT_OPTIONS, [])
    assert (result[0].desired_state["release_reason"] == "hvac_comfort_handoff") is handoff
    if handoff:
        assert result[0].desired_state["released_until"] == NOW + timedelta(minutes=20)


def test_economic_comfort_hold_expires_without_legacy_fallback():
    ctx = context(6)
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    ctx.climate_engine = {
        "status": "active",
        "ever_active": True,
        "model": simulation_model(),
        "modes": {"heat": {"ready": True}},
    }
    options = {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}
    ctx.hvac_control = {"released_until": NOW + timedelta(minutes=5)}
    assert economic_actions(ctx, options, []) == []
    ctx.hvac_control = {"released_until": NOW - timedelta(minutes=5)}
    result = economic_actions(ctx, options, [])
    assert result[0].desired_state["economic_policy_version"] == 1


def test_switching_to_legacy_restores_economic_ownership():
    ctx = context()
    ctx.hvac_control = {"economic_policy_version": 1}
    actions = economic_actions(ctx, {**DEFAULT_OPTIONS, "hvac_decision_policy": "legacy"}, [])
    assert len(actions) == 1 and actions[0].kind == ActionKind.RELEASE_HVAC
    ctx.climate_inputs = {}
    assert economic_actions(ctx, DEFAULT_OPTIONS, [])[0].kind == ActionKind.RELEASE_HVAC


def test_arrival_requires_comfort_even_when_normal_controls_would_stay_uncomfortable():
    ctx = context(2)
    ctx.current_hvac_temperature_c = 18
    ctx.occupancy_state = OccupancyState.AWAY
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=10)).isoformat()
    baseline = ClimateTrajectory((18, 18), (0, 0))
    assert not comfort_valid(ctx, ClimateTrajectory((18.5, 19), (1, 1)), baseline, DEFAULT_OPTIONS)
    assert comfort_valid(ctx, ClimateTrajectory((19, 20), (1, 1)), baseline, DEFAULT_OPTIONS)


def test_observation_bucket_is_invalidated_by_later_intervention():
    ctx = context()
    state = observe({}, ctx, 20)
    ctx.created_at += timedelta(minutes=1)
    ctx.hvac_control = {"phase": "preconditioning"}
    state = observe(state, ctx, 20)
    assert len(state["observations"]) == 1
    assert state["observations"][0]["provenance"] == "planner"
    ctx.created_at += timedelta(minutes=5)
    ctx.hvac_control = {}
    ctx.input_issues = ["daikin_power_stale"]
    assert len(observe(state, ctx, 20)["observations"]) == 1


def test_readiness_recovery_requires_new_validations_and_actual_recent_windows():
    ctx = context()
    model = ready_model()
    state = update_readiness({}, model, ctx, DEFAULT_OPTIONS)
    ctx.created_at += timedelta(days=1)
    model["trained_at"] = ctx.created_at.isoformat()
    state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
    assert state["status"] == "active"
    ctx.current_hvac_power_kw = None
    state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
    ctx.current_hvac_power_kw = 1
    state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
    assert state["status"] == "degraded"
    for expected in ("degraded", "active"):
        ctx.created_at += timedelta(days=1)
        model["trained_at"] = ctx.created_at.isoformat()
        state = update_readiness(state, model, ctx, DEFAULT_OPTIONS)
        assert state["status"] == expected
    ctx.created_at += timedelta(days=15)
    model["trained_at"] = ctx.created_at.isoformat()
    assert update_readiness(state, model, ctx, DEFAULT_OPTIONS)["status"] == "degraded"


def test_revalidation_applies_acquisition_minimum_and_conservative_battery_floor(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module

    ctx = context(4)
    schedule = {
        "start": NOW.isoformat(),
        "stop": (NOW + timedelta(minutes=5)).isoformat(),
        "release": (NOW + timedelta(minutes=10)).isoformat(),
        "identity": "id",
        "mode": "heat",
        "target": 22,
    }
    base = ClimateTrajectory((21,) * 4, (2,) * 4)
    candidate = ClimateTrajectory((21,) * 4, (1,) * 4)
    monkeypatch.setattr(module, "simulate", lambda *a, **kw: candidate if "start" in kw else base)

    def cost(c, powers, options, conservative=False):
        return SiteCost(
            0.2 if powers == base.powers_kw else 0.1,
            0,
            0,
            0,
            1 if conservative and powers == candidate.powers_kw else 2,
        )

    monkeypatch.setattr(module, "site_cost", cost)
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, simulation_model(), schedule) is None
    ctx.hvac_control = {"economic_policy_version": 1}
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, simulation_model(), schedule) is None
    monkeypatch.setattr(
        module, "site_cost", lambda c, p, o, **kw: SiteCost(0.2 if p == base.powers_kw else 0.1, 0, 0, 0, 2)
    )
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, simulation_model(), schedule) is not None


@pytest.mark.parametrize("interval", [5, 15, 30])
def test_observation_energy_uses_forecast_timestamps(interval):
    from custom_components.ha_energy_planner.climate_learning import finish_observation

    end = NOW + timedelta(minutes=30)
    state = {
        "observation_started_at": NOW.isoformat(),
        "observation_until": end.isoformat(),
        "observation_prediction": {
            "baseline_slots": [(NOW + timedelta(minutes=i)).isoformat() for i in range(0, 30, interval)],
            "baseline_powers_kw": [2] * (30 // interval),
            "interval_minutes": interval,
        },
    }
    rows = [row(NOW + timedelta(minutes=i)) for i in range(0, 31, 5)]
    finish_observation(state, rows, end)
    result = state["comparisons"][-1]
    assert result["valid"]
    assert result["actual_hvac_kwh"] == 0.5
    assert result["predicted_hvac_kwh"] == pytest.approx(1)
    state.pop("observation_closed_at")
    state["observation_prediction"]["baseline_slots"] = ["invalid"]
    finish_observation(state, rows, end)
    assert not state["comparisons"][-1]["valid"]


def test_room_without_presence_uses_household_occupancy():
    ctx = context()
    model = simulation_model()
    ctx.climate_inputs["zones"] = {"climate.room": {"temperature": 18, "occupied": None}}
    model["rooms"] = {"climate.room": {"physical": model["physical"]}}
    ctx.occupancy_state = OccupancyState.AWAY
    model["normal"] = [{**r, "occupied": "away"} for r in model["normal"]]
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat") is not None
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=5)).isoformat()
    model["normal"] += [{**r, "occupied": "occupied"} for r in model["normal"]]
    predicted = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat")
    assert predicted is not None and not comfort_valid(ctx, predicted, predicted, DEFAULT_OPTIONS)


def test_horizon_interval_and_occupancy_changes_invalidate_climate_identity():
    original = climate_identity({}, DEFAULT_OPTIONS)
    for key, value in (("planning_horizon_hours", 24), ("planning_interval_minutes", 15)):
        assert climate_identity({}, {**DEFAULT_OPTIONS, key: value}) != original
    assert climate_identity({"person_entities": ["person.someone"]}, DEFAULT_OPTIONS) != original


@pytest.mark.parametrize("value,unit", [("unknown", "%"), ("nan", "%"), ("50", "bad"), ("101", "%")])
def test_command_time_invalid_optional_values_are_not_fresh(value, unit):
    from custom_components.ha_energy_planner.climate_runtime import command_rejection

    data = {"hvac_humidity_entity": "sensor.humidity"}
    identity = climate_identity(data, DEFAULT_OPTIONS)
    state = {"status": "active", "identity": identity, "scheduled": {"lifecycle_id": "one"}}
    desired = {
        "economic_policy_version": 1,
        "configuration_identity": identity,
        "lifecycle_id": "one",
        "period_end": NOW + timedelta(hours=1),
    }
    sensor = SimpleNamespace(state=value, attributes={"unit_of_measurement": unit}, last_updated=NOW)
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: sensor))
    assert (
        command_rejection(hass, data, DEFAULT_OPTIONS, state, desired, NOW) == "economic_climate_live_evidence_missing"
    )


def test_search_limit_does_not_grant_authority_from_a_partial_comparison(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module

    ctx = context()
    ctx.slots[0].import_price = 0
    for slot in ctx.slots[1:]:
        slot.import_price = 5
    # The first candidate can be profitable; truncation must still discard it.
    monkeypatch.setattr(module, "MAX_SEARCH_SLOT_EVALUATIONS", 12)
    candidate, decision = optimise(ctx, DEFAULT_OPTIONS, simulation_model())
    assert candidate is None
    assert decision["rejected"]["search_work_limit"] == 1


def test_validation_does_not_use_future_mode_and_checks_intermediate_errors(monkeypatch):
    from custom_components.ha_energy_planner import climate_learning as module
    from custom_components.ha_energy_planner.climate_models import BaselinePrediction

    rows = [
        row(
            NOW - timedelta(days=8 - day) + timedelta(minutes=5 * i),
            mode="heat" if i == 0 else "off",
            temperature=25 if i == 3 else 21,
        )
        for day in range(8)
        for i in range(7)
    ]

    def predict(train, sample, timezone):
        assert sample["mode"] == "heat"
        assert all(r["at"][:10] < sample["at"][:10] for r in train)
        return BaselinePrediction(0, 0, 21, 0, 5, "off")

    monkeypatch.setattr(module, "neighbours", predict)
    monkeypatch.setattr(module, "fit_physical", lambda rows: {})
    monkeypatch.setattr(module, "temperature_step", lambda *a: 21)
    result = validate(rows, "heat", "UTC", 30)
    assert result.windows == 1 and result.last_window_at == rows[-1]["at"]
    assert result.temperature_mae > 0.5
    assert "temperature_mae" in result.blockers


def test_arrival_inside_slot_requires_comfort_at_arrival_instant():
    ctx = context(2)
    ctx.current_hvac_temperature_c = 18
    ctx.occupancy_state = OccupancyState.AWAY
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=2)).isoformat()
    assert not comfort_valid(
        ctx, ClimateTrajectory((20, 21), (1, 1)), ClimateTrajectory((18, 18), (0, 0)), DEFAULT_OPTIONS
    )
    ctx.current_hvac_temperature_c = 21
    assert comfort_valid(ctx, ClimateTrajectory((21, 21), (1, 1)), ClimateTrajectory((21, 21), (1, 1)), DEFAULT_OPTIONS)
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=7)).isoformat()
    trajectory = ClimateTrajectory((21, 21), (1, 1), temperature_lower=(20.5, 20.5), temperature_upper=(21.5, 21.5))
    assert comfort_valid(ctx, trajectory, trajectory, DEFAULT_OPTIONS)
    assert comfort_valid(ctx, ClimateTrajectory((21, 21), (1, 1)), trajectory, DEFAULT_OPTIONS)


def test_solar_interpolation_accepts_unaligned_planner_slots_without_extrapolation():
    from custom_components.ha_energy_planner.climate_optimizer import irradiance_at

    points = [{"at": NOW.isoformat(), "value": 0}, {"at": (NOW + timedelta(minutes=10)).isoformat(), "value": 100}]
    assert irradiance_at(points, NOW + timedelta(minutes=5, seconds=30)) == pytest.approx(55)
    assert irradiance_at(points, NOW) == 0
    assert irradiance_at(points, NOW - timedelta(seconds=1)) is None
    assert irradiance_at(points, NOW + timedelta(minutes=11)) is None


def test_room_readiness_requires_its_own_chronological_validation():
    ctx = context()
    ctx.climate_inputs["zones"] = {"climate.room": {}}
    model = ready_model()
    model["rooms"] = {
        "climate.room": {
            "physical": {"heat": {"coefficients": [1, 1]}},
            "days": 30,
            "validation": {"heat": {"blockers": ["temperature_p90"]}},
        }
    }
    result = update_readiness({}, model, ctx, DEFAULT_OPTIONS)
    assert "room_model_not_ready:climate.room" in result["modes"]["heat"]["blockers"]


def test_climate_energy_respects_grid_capacity_reserved_for_ev():
    ctx = context(1)
    ctx.slots[0].projected_ev_load_kw = 9
    assert site_cost(ctx, (1,), DEFAULT_OPTIONS) is not None
    assert site_cost(ctx, (2,), DEFAULT_OPTIONS) is None
    ctx.slots[0].projected_ev_load_kw = 0
    ctx.slots[0].pv_forecast_kw = 11
    assert site_cost(ctx, (0,), DEFAULT_OPTIONS) is None


def test_observation_starts_after_prediction_when_refresh_is_between_buckets():
    ctx = context()
    ctx.created_at += timedelta(seconds=30)
    for slot in ctx.slots:
        slot.valid_at += timedelta(seconds=30)
        slot.import_price = 5
    ctx.slots[0].import_price = 0
    ctx.climate_engine = {
        "status": "active",
        "ever_active": True,
        "model": simulation_model(),
        "modes": {"heat": {"ready": True}},
    }
    economic_actions(ctx, {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01, "hvac_observation_cadence": 1}, [])
    assert instant(ctx.climate_engine["observation_started_at"]) > ctx.created_at


def test_optional_temperature_and_irradiance_units_fail_closed():
    data = {
        "climate_zone_entities": ["climate.room"],
        "hvac_zone_mappings": {"climate.room": {"temperature": "sensor.temp"}},
        "hvac_irradiance_entity": "sensor.sun",
    }
    from custom_components.ha_energy_planner.const import CONF_CLIMATE_ZONES

    data[CONF_CLIMATE_ZONES] = ["climate.room"]
    sensors = {
        entity: SimpleNamespace(state="1", attributes={"unit_of_measurement": "bad"}, last_updated=NOW)
        for entity in ("sensor.temp", "sensor.sun")
    }
    result = read_climate_inputs(
        SimpleNamespace(states=SimpleNamespace(get=sensors.get)),
        data,
        DEFAULT_OPTIONS,
        NOW,
        NOW + timedelta(hours=1),
        {},
    )
    assert result["zones"]["climate.room"]["temperature"] is None
    assert result["irradiance"] is None
    assert not any(source["fresh"] for source in result["sources"].values())


def test_initial_discomfort_cannot_worsen_relative_to_normal_controls():
    ctx = context(1)
    ctx.current_hvac_temperature_c = 18
    assert not comfort_valid(ctx, ClimateTrajectory((17,), (0,)), ClimateTrajectory((19,), (1,)), DEFAULT_OPTIONS)


def test_observation_ignores_forecast_points_after_its_period():
    from custom_components.ha_energy_planner.climate_learning import finish_observation

    end = NOW + timedelta(minutes=5)
    state = {
        "observation_started_at": NOW.isoformat(),
        "observation_until": end.isoformat(),
        "observation_prediction": {
            "baseline_powers_kw": [1, 100],
            "baseline_slots": [NOW.isoformat(), end.isoformat()],
        },
    }
    finish_observation(state, [row(NOW), row(end)], end)
    assert state["comparisons"][-1]["predicted_hvac_kwh"] == pytest.approx(1 / 12)


def test_simulation_cache_is_independent_of_candidate_order(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module
    from custom_components.ha_energy_planner.climate_models import BaselinePrediction

    ctx = context(1)
    model = simulation_model()
    monkeypatch.setattr(
        module,
        "neighbours",
        lambda rows, sample, tz: BaselinePrediction(sample["temperature"] / 10, 3, 21, 1, 5, "heat"),
    )
    cache = {}
    ctx.current_hvac_temperature_c = 21.01
    first = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat", cache=cache)
    ctx.current_hvac_temperature_c = 21.12
    second = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat", cache=cache)
    fresh = simulate(ctx, model, DEFAULT_OPTIONS, mode="heat")
    assert second == fresh
    assert first.powers_kw == second.powers_kw == (2.1,)


def test_revalidation_integrates_persisted_phase_boundaries(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module
    from custom_components.ha_energy_planner.climate_models import BaselinePrediction

    ctx = context(4)
    model = simulation_model()
    for fit in model["physical"].values():
        fit["coefficients"] = [0, 0]
    monkeypatch.setattr(module, "neighbours", lambda *a: BaselinePrediction(1, 1, 21, 1, 5, "heat"))
    monkeypatch.setattr(module, "electrical_power", lambda *a: 2)
    schedule = {
        "identity": "id",
        "start": NOW.isoformat(),
        "stop": (NOW + timedelta(minutes=2)).isoformat(),
        "release": (NOW + timedelta(minutes=7)).isoformat(),
        "mode": "heat",
        "target": 22,
    }
    candidate = revalidate_schedule(ctx, {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0}, model, schedule)
    assert candidate is not None
    assert candidate.trajectory.powers_kw == pytest.approx((0.8, 0.6, 1, 1))
    assert candidate.candidate_cost.hvac_kwh == pytest.approx(17 / 60)
    assert len(ctx.slots) == 4
    # A changed explicit arrival invalidates the old scheduled opportunity.
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=9)).isoformat()
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, model, schedule) is None
    ctx.climate_inputs.pop("arrival")
    schedule["stop"] = schedule["start"]
    assert revalidate_schedule(ctx, DEFAULT_OPTIONS, model, schedule) is None


def test_room_comfort_recovery_arrival_and_terminal_state():
    from custom_components.ha_energy_planner.climate_optimizer import recovery_valid

    ctx = context(2)
    ctx.climate_inputs["zones"] = {"climate.room": {"temperature": 18, "occupied": True, "low": 20, "high": 24}}
    baseline = ClimateTrajectory((21, 21), (1, 1), zones={"climate.room": (19, 20)})
    better = ClimateTrajectory((21, 21), (1, 1), zones={"climate.room": (19.5, 20.1)})
    worse = ClimateTrajectory((21, 21), (1, 1), zones={"climate.room": (18.5, 19)})
    assert comfort_valid(ctx, better, baseline, DEFAULT_OPTIONS)
    assert recovery_valid(better, baseline)
    assert not comfort_valid(ctx, worse, baseline, DEFAULT_OPTIONS)
    assert not recovery_valid(worse, baseline)
    assert not comfort_valid(ctx, ClimateTrajectory((21, 21), (1, 1)), baseline, DEFAULT_OPTIONS)
    assert not recovery_valid(better, ClimateTrajectory((21, 21), (1, 1)))
    ctx.occupancy_state = OccupancyState.AWAY
    ctx.climate_inputs["zones"]["climate.room"]["occupied"] = None
    ctx.climate_inputs["arrival"] = (NOW + timedelta(minutes=7)).isoformat()
    assert not comfort_valid(ctx, better, baseline, DEFAULT_OPTIONS)
    # Explicit room absence does not invent an occupied room.
    ctx.climate_inputs["zones"]["climate.room"]["occupied"] = False
    assert comfort_valid(ctx, better, baseline, DEFAULT_OPTIONS)


def test_negative_tariffs_require_both_demand_uncertainty_directions():
    from custom_components.ha_energy_planner.climate_optimizer import conservative_comparison

    ctx = context(1)
    ctx.slots[0].import_price = -1
    model = simulation_model()
    model["validation"]["heat"]["energy_error"] = 0.2
    baseline = ClimateTrajectory((21,), (1,))
    high = ClimateTrajectory((21,), (1.32,))
    low = ClimateTrajectory((21,), (0.88,))
    result = conservative_comparison(ctx, model, "heat", baseline, high, low, DEFAULT_OPTIONS)
    assert result is not None and result[0] < 0
    ctx.slots[0].import_price = 1
    assert conservative_comparison(ctx, model, "heat", baseline, high, low, DEFAULT_OPTIONS)[0] < 0


def test_zone_activation_is_predicted_only_during_owned_period(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module

    ctx = context(4)
    ctx.climate_zone_entities = ["climate.room"]
    ctx.climate_inputs["zones"] = {"climate.room": {"temperature": 21, "enabled": False}}
    model = simulation_model()
    model["rooms"] = {"climate.room": {"physical": model["physical"]}}
    states = []
    original = module.temperature_step

    def step(model, row, hours):
        states.append(row["zones"]["climate.room"]["enabled"])
        return original(model, row, hours)

    monkeypatch.setattr(module, "temperature_step", step)
    assert simulate(ctx, model, DEFAULT_OPTIONS, mode="heat", start=1, stop=2, release=3, target=22)
    assert states == [False, False, True, True, True, True, False, False]
    assert ctx.climate_inputs["zones"]["climate.room"]["enabled"] is False


def test_command_accepts_arrival_after_planned_early_release():
    from custom_components.ha_energy_planner.climate_runtime import command_rejection

    arrival = (NOW + timedelta(hours=2)).isoformat()
    sensor = SimpleNamespace(state=arrival, attributes={})
    data = {"hvac_arrival_entity": "sensor.arrival"}
    identity = climate_identity(data, DEFAULT_OPTIONS)
    desired = {
        "economic_policy_version": 1,
        "lifecycle_id": "one",
        "configuration_identity": identity,
        "arrival": arrival,
        "period_end": (NOW + timedelta(hours=1)).isoformat(),
    }
    state = {"identity": identity, "status": "active", "scheduled": {"lifecycle_id": "one"}}
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: sensor if entity == "sensor.arrival" else None))
    assert command_rejection(hass, data, DEFAULT_OPTIONS, state, desired, NOW) is None


def test_default_horizon_search_covers_starts_and_targets_within_budget():
    from custom_components.ha_energy_planner.climate_optimizer import MAX_SEARCH_SLOT_EVALUATIONS, candidate_schedules

    count, lead, targets = 144, 18, [21, 21.5, 22, 22.5, 23, 23.5, 24]
    schedules = candidate_schedules(count, lead, targets, MAX_SEARCH_SLOT_EVALUATIONS // (3 * count))
    assert schedules is not None and len(schedules) * 3 * count <= MAX_SEARCH_SLOT_EVALUATIONS
    assert {(start, target) for start, _, _, target in schedules} == {
        (start, target) for start in range(lead) for target in targets
    }
    assert any(stop - start > 1 for start, stop, _, _ in schedules)
    assert any(release == count - 1 for _, _, release, _ in schedules)
    assert candidate_schedules(count, lead, targets, 1) is None
    assert candidate_schedules(1, lead, targets, 10) == []


def test_normal_history_pool_requires_absolute_timestamp():
    from custom_components.ha_energy_planner.climate_learning import normal_history_for_slot

    assert normal_history_for_slot([], {"at": "invalid"}, "UTC") == []


def test_candidate_target_uses_learned_normal_target_at_start(monkeypatch):
    from custom_components.ha_energy_planner import climate_optimizer as module

    ctx = context(4)
    targets = []

    def simulate_target(c, m, options, **kwargs):
        if "start" not in kwargs:
            return ClimateTrajectory((21,) * 4, (2,) * 4, normal_targets=(23,) * 4)
        targets.append(kwargs["target"])
        return ClimateTrajectory((21,) * 4, (1,) * 4)

    monkeypatch.setattr(module, "simulate", simulate_target)
    candidate, _ = optimise(ctx, {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0}, simulation_model())
    assert candidate is not None and candidate.target == 23
    assert targets and min(targets) >= 23
    monkeypatch.setattr(
        module, "simulate", lambda *a, **kw: ClimateTrajectory((21,) * 4, (2,) * 4, normal_targets=(None,) * 4)
    )
    assert optimise(ctx, DEFAULT_OPTIONS, simulation_model())[1]["rejected"]["normal_target_missing"]


def test_date_only_helper_is_not_an_explicit_arrival_time():
    sensor = SimpleNamespace(
        state="2026-09-14",
        attributes={"timestamp": (NOW + timedelta(hours=1)).timestamp(), "has_date": True, "has_time": False},
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: sensor))
    result = read_climate_inputs(
        hass, {"hvac_arrival_entity": "input_datetime.arrival"}, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {}
    )
    assert result["arrival"] is None


def test_covered_twelve_hour_horizon_can_select_a_validated_candidate():
    ctx = context(144)
    ctx.slots[0].import_price = 0
    ctx.slots[1].import_price = 5
    model = simulation_model()
    model["normal"] = [
        row(
            NOW - timedelta(days=7 * day) + timedelta(minutes=minute),
            temperature=temperature,
            power_kw=max(0, (23 - temperature) * 0.3),
        )
        for day in (1, 2)
        for minute in range(-30, 751, 5)
        for temperature in (20, 21, 22, 23, 24)
    ]
    candidate, decision = optimise(ctx, {**DEFAULT_OPTIONS, "hvac_minimum_saving": 0.01}, model)
    assert candidate is not None and candidate.conservative_saving > 0
    assert len(candidate.trajectory.powers_kw) == 144
    assert "search_work_limit" not in decision["rejected"]


def test_cent_tariffs_label_normalized_whole_currency():
    from custom_components.ha_energy_planner.const import CONF_AMBER_IMPORT_PRICE

    tariff = SimpleNamespace(state="30", attributes={"unit_of_measurement": "c/kWh"})
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity: tariff if entity == "sensor.price" else None),
        config=SimpleNamespace(currency="AUD"),
    )
    data = {CONF_AMBER_IMPORT_PRICE: "sensor.price"}
    assert read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=1), {})["currency"] == "AUD"
    tariff.attributes["currency"] = "USD"
    assert read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=1), {})["currency"] == "USD"
    del hass.config
    tariff.attributes.pop("currency")
    assert read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=1), {})["currency"] is None


def test_targets_respect_fractional_comfort_and_device_lattice():
    from custom_components.ha_energy_planner.climate_optimizer import supported_targets

    ctx = context()
    ctx.occupied_temperature_low_c = 20.2
    ctx.occupied_temperature_high_c = 23.8
    ctx.climate_inputs.update(minimum_temperature=16, maximum_temperature=23, temperature_step=0.5)
    assert supported_targets(ctx) == [20.5, 21, 21.5, 22, 22.5, 23]
    ctx.climate_inputs["minimum_temperature"] = 25
    assert supported_targets(ctx) == []
    assert optimise(ctx, DEFAULT_OPTIONS, simulation_model())[1]["rejected"]["device_temperature_range_unsupported"]
    assert (
        simulate(ctx, simulation_model(), DEFAULT_OPTIONS, mode="heat", start=0, stop=1, release=2, target=22) is None
    )
    ctx.occupied_temperature_low_c = None
    assert supported_targets(ctx) == []


def test_coast_power_includes_thermostat_operation_at_supported_boundary():
    ctx = context(1)
    ctx.current_hvac_temperature_c = 20.4
    ctx.occupied_temperature_low_c = 20.2
    predicted = simulate(ctx, simulation_model(), DEFAULT_OPTIONS, mode="heat", start=0, stop=0, release=1)
    assert predicted is not None and predicted.powers_kw[0] > 0


@pytest.mark.parametrize(
    "ownership",
    [
        {"economic_policy_version": 1, "required_evidence_lost": "hvac_release_failed"},
        {"economic_policy_version": 2},
    ],
)
def test_economic_recovery_failures_release_before_any_new_command(ownership):
    ctx = context()
    ctx.climate_engine = {
        "status": "active",
        "model": simulation_model(),
        "scheduled": {"lifecycle_id": "old"},
        "modes": {"heat": {"ready": True}},
    }
    ctx.hvac_control = {
        **ownership,
        "mode": "heat",
        "phase": "preconditioning",
        "period_end": (NOW + timedelta(hours=1)).isoformat(),
    }
    actions = economic_actions(ctx, DEFAULT_OPTIONS, [])
    assert len(actions) == 1 and actions[0].kind == ActionKind.RELEASE_HVAC
    assert actions[0].desired_state["release_reason"] == "hvac_required_evidence_lost"
    assert "scheduled" not in ctx.climate_engine
