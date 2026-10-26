"""Prospective thermal rates require exact physical compatibility before acquisition."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from test_climate_engine import simulation_model
from test_zone_control import BED, MAIN, MAIN_CLIMATE, PAIRS, device, room_context
from test_zone_temperature import NOW

from custom_components.ha_energy_planner import climate_optimizer
from custom_components.ha_energy_planner.climate_models import ClimateTrajectory
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.models import DecisionSlot
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.thermal_learning import rate, sample, summary, timing, update


def trained_idle(mode="heat", *, offset=0, aliases=False, equipment_target=None):
    c = room_context(mode, main=22, bed=22)
    c.climate_inputs["head_entity"] = "climate.head"
    if offset:
        c.climate_inputs["zones"][MAIN]["cooling_equipment_offset_c"] = offset
    if aliases:
        for actuator, climate in PAIRS.items():
            c.climate_inputs["zones"][climate] = deepcopy(c.climate_inputs["zones"][actuator])
    hass = device(c)
    target = equipment_target if equipment_target is not None else (24 if mode == "heat" else 21)
    hass.states.get("climate.head").attributes["temperature"] = target + offset
    for climate in PAIRS.values():
        hass.states.get(climate).attributes["temperature"] = target + (offset if climate == MAIN_CLIMATE else 0)
    model = {}
    for index in range(13):
        c.created_at = NOW + timedelta(minutes=index * 5)
        for room in c.climate_inputs["zones"].values():
            room["temperature"] = (20 + index * .1) if mode == "heat" else (26 - index * .1)
            hass.states.get(room["temperature_entity"]).last_updated = c.created_at
        model = update(model, sample(c, DEFAULT_OPTIONS, hass.states, "climate.head"))
    c.created_at += timedelta(minutes=5)
    c.current_hvac_mode = "off"
    hass.states.get("climate.head").state = "off"
    hass.states.values[BED] = "off"
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 22 if mode == "heat" else 23
        hass.states.get(room["temperature_entity"]).last_updated = c.created_at
    model = update(model, sample(c, DEFAULT_OPTIONS, hass.states, "climate.head"))
    # Round-trip through the actual persisted JSON representation.
    return c, hass, json.loads(json.dumps(model))


@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize("aliases", [False, True])
def test_idle_candidate_uses_qualified_rates_before_any_acquisition(mode, aliases):
    c, hass, model = trained_idle(mode, aliases=aliases)
    before, controls = deepcopy(model), deepcopy(c.hvac_control)
    states = {entity: hass.states.get(entity).state for entity in ["climate.head", *PAIRS]}
    evidence = summary(model, c.created_at)
    assert not evidence["electrical_load_model_ready"]
    assert not evidence["sources"][MAIN]["modes"][mode]["ready"]
    retained = evidence["sources"][MAIN]["retained_qualified_regimes"][mode]
    assert len(retained) == 1 and retained[0]["structured_windows"] == 12
    assert retained[0]["count"] == 12 and retained[0]["minutes"] == 60
    assert rate(model, MAIN, mode, c.created_at) is None
    details = timing(c, DEFAULT_OPTIONS, mode, model)
    assert details["rates_ready"] and details["preparation_basis"] == "learned_rates"
    assert 100 <= details["effective_lead_minutes"] <= 101
    for row in details["zones"].values():
        assert row["candidate_compatible"]
        assert row["rate_c_per_hour"] == pytest.approx(1.2)
        assert row["predicted_finishing_temperature"] == (24 if mode == "heat" else 21)
    assert model == before and c.hvac_control == controls and not c.climate_legacy_decision
    assert states == {entity: hass.states.get(entity).state for entity in states}


@pytest.mark.parametrize("change", ["subset", "head_target", "zone_target", "source", "controller", "topology",
                                    "unsupported", "missing_head", "legacy", "legacy_summary", "metadata_version",
                                    "expired", "future", "eleven_windows", "short_duration", "mixed", "disabled",
                                    "model_disabled", "owner_identity", "unsafe_subset", "missing_feedback",
                                    "all_complete"])
def test_candidate_incompatibility_and_unqualified_evidence_use_configured_fallback(change):
    c, hass, model = trained_idle()
    options = dict(DEFAULT_OPTIONS)
    target = 24
    if change == "subset":
        c.climate_inputs["zones"][BED]["temperature"] = 24
    elif change == "head_target":
        target = 23
    elif change == "zone_target":
        c.climate_inputs["zones"][MAIN]["high"] = 23
    elif change == "source":
        c.climate_inputs["zones"][MAIN]["temperature_entity"] = "sensor.replacement"
    elif change == "controller":
        c.climate_inputs["head_entity"] = "climate.replacement"
    elif change == "topology":
        c.climate_inputs["zone_topology"]["safe_open_sets"] = [[MAIN, BED]]
    elif change == "unsupported":
        c.climate_inputs["zone_topology"]["supported"] = False
    elif change == "missing_head":
        del c.climate_inputs["head_entity"]
    elif change in {"legacy", "legacy_summary", "metadata_version", "mixed", "short_duration", "eleven_windows"}:
        for item in model["sources"].values():
            bucket = item["buckets"]["heat"]
            for row in bucket["windows"]:
                if change in {"legacy", "legacy_summary"}:
                    del row["conditions"]
                elif change == "metadata_version":
                    row["conditions"]["version"] = 99
                elif change == "short_duration":
                    row["minutes"] = 4.9
                elif change == "mixed" and row is bucket["windows"][-1]:
                    row["conditions"]["controls"][0][2] = 23
            if change == "legacy_summary":
                del bucket["windows"]
            elif change == "eleven_windows":
                bucket["windows"].pop()
    elif change in {"expired", "future"}:
        c.created_at += timedelta(days=15) if change == "expired" else -timedelta(days=1)
    elif change == "disabled":
        options["hvac_thermal_learning_enabled"] = False
    elif change == "owner_identity":
        model["sources"][MAIN]["identity"] = "unknown_owner"
    elif change == "model_disabled":
        model["learning_enabled"] = False
    elif change == "unsafe_subset":
        c.climate_inputs["zones"][BED]["temperature"] = 24
        c.climate_inputs["zone_topology"]["safe_open_sets"] = [[MAIN, BED]]
    elif change == "missing_feedback":
        c.climate_inputs["zones"][MAIN]["temperature_source"]["fresh"] = False
    else:
        for room in c.climate_inputs["zones"].values():
            room["temperature"] = 24
    details = timing(c, options, "heat", model, target=target)
    assert not details["rates_ready"]
    assert details["effective_lead_minutes"] == 90
    assert not details["finishing_temperature_known"]
    assert all(row["predicted_finishing_temperature"] is None for row in details["zones"].values())
    if change == "legacy":
        retained = summary(model, c.created_at)["sources"][MAIN]["retained_qualified_regimes"]["heat"]
        assert retained[0]["structured_windows"] == 0


def test_compensated_cooling_uses_equipment_targets_and_rejects_changed_offset():
    c, hass, model = trained_idle("cool", offset=-2)
    assert timing(c, DEFAULT_OPTIONS, "cool", model)["rates_ready"]
    c.climate_inputs["zones"][MAIN]["cooling_equipment_offset_c"] = -1
    assert not timing(c, DEFAULT_OPTIONS, "cool", model)["rates_ready"]


def test_legacy_regimes_stay_retained_without_prospective_authority_on_new_samples():
    c, hass, model = trained_idle()
    for item in model["sources"].values():
        for row in item["buckets"]["heat"]["windows"]:
            del row["conditions"]
    c.current_hvac_mode = "heat"
    hass.states.get("climate.head").state = "heat"
    hass.states.values[BED] = "on"
    for _index in range(4):
        c.created_at += timedelta(minutes=5)
        for room in c.climate_inputs["zones"].values():
            room["temperature"] += .1
            hass.states.get(room["temperature_entity"]).last_updated = c.created_at
        model = update(model, sample(c, DEFAULT_OPTIONS, hass.states, "climate.head"))
    assert rate(model, MAIN, "heat", c.created_at) is not None
    assert not timing(c, DEFAULT_OPTIONS, "heat", model)["rates_ready"]
    assert len(model["sources"][MAIN]["buckets"]["heat"]["windows"]) > 12


def test_fallback_plans_with_prospective_rates_while_head_remains_off():
    c, hass, model = trained_idle()
    c.slots = [DecisionSlot(c.created_at + timedelta(minutes=i * 5), .1 if i < 36 else .5,
                           .05, 0, 1, outdoor_temperature_forecast_c=10) for i in range(60)]
    c.current_hvac_temperature_c = 22
    c.current_outdoor_temperature_c = 10
    # Qualified active rates select timing only after actual room demand is established.
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 20.5
    period = HVACPlanningPolicy(DEFAULT_OPTIONS, model)._next_hvac_period(c)
    assert period is not None
    assert c.climate_legacy_decision["thermal_timing"]["rates_ready"]
    assert period["precondition_end"] - period["precondition_start"] >= timedelta(minutes=175)
    assert hass.states.get("climate.head").state == "off" and not c.hvac_control


def test_economic_candidates_match_their_own_target_before_search_and_keep_energy_gate(monkeypatch):
    c, hass, model = trained_idle()
    c.current_hvac_temperature_c = 22
    c.climate_inputs.update(thermal_model=model, load_excludes_hvac=True, target=22, currency="AUD")
    c.slots = [DecisionSlot(c.created_at + timedelta(minutes=i * 5), .5, .1, 0, 0) for i in range(40)]
    economic = simulation_model()
    economic["rooms"] = {entity: {} for entity in c.climate_inputs["zones"]}
    monkeypatch.setattr(climate_optimizer, "candidate_schedules", lambda *a: [(0, 20, 30, 24), (0, 20, 30, 23)])
    selected = []

    def unsupported_trajectory(*args, **kwargs):
        if "target" in kwargs:
            selected.append(kwargs["target"])
            return None
        return ClimateTrajectory((22,) * 40, (1,) * 40)

    monkeypatch.setattr(climate_optimizer, "simulate", unsupported_trajectory)
    candidate, decision = climate_optimizer.optimise(c, DEFAULT_OPTIONS, economic)
    assert candidate is None
    # Only heat24 earns a 100-minute candidate window; heat23 and cooling retain 90.
    assert selected == [24, 24, 24]
    assert decision["rejected"]["adaptive_lead_limit"] == 1
    assert not summary(model, c.created_at)["electrical_load_model_ready"]
    c.climate_inputs["load_excludes_hvac"] = False
    assert climate_optimizer.optimise(c, DEFAULT_OPTIONS, economic)[1]["rejected"] == {
        "household_load_hvac_provenance_missing": 1}


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_matching_candidate_excludes_incompatible_and_expired_retained_evidence(mode):
    c, hass, model = trained_idle(mode)
    for item in model["sources"].values():
        windows = item["buckets"][mode]["windows"]
        incompatible, expired = deepcopy(windows), deepcopy(windows)
        for row in incompatible:
            row["rate"] = 5
            row["regime"] = "other_setpoint"
            row["conditions"]["controls"][0][2] = 23.5
        for row in expired:
            row["rate"] = .1
            row["at"] = (c.created_at - timedelta(days=15)).isoformat()
        windows.extend(incompatible + expired)
    details = timing(c, DEFAULT_OPTIONS, mode, model)
    assert details["rates_ready"]
    assert details["zones"][MAIN]["rate_c_per_hour"] == pytest.approx(1.2)
    evidence = summary(model, c.created_at)["sources"][MAIN]
    assert evidence["retained_windows"][mode] == 24
    assert len(evidence["retained_qualified_regimes"][mode]) == 2


def test_blocked_equipment_range_does_not_grant_candidate_authority():
    c, hass, model = trained_idle("cool", offset=-2)
    c.climate_inputs["zones"][BED]["low"] = 24
    c.climate_inputs["zones"][BED]["temperature"] = 25
    assert not timing(c, DEFAULT_OPTIONS, "cool", model)["rates_ready"]


def test_missing_candidate_target_and_source_do_not_grant_authority():
    c, hass, model = trained_idle()
    c.occupied_temperature_high_c = None
    assert not timing(c, DEFAULT_OPTIONS, "heat", model)["rates_ready"]
    c.occupied_temperature_high_c = 24
    del c.climate_inputs["zones"][MAIN]["temperature_entity"]
    assert not timing(c, DEFAULT_OPTIONS, "heat", model)["rates_ready"]


@pytest.mark.parametrize("compatible", [False, True])
def test_fallback_aggregate_cannot_bypass_candidate_qualification(compatible):
    c, hass, model = trained_idle()
    model.update(enabled=True, active_heat_rate_c_per_hour={"average": 10, "sample_count": 12})
    if not compatible:
        c.occupied_temperature_high_c = 23
    c.slots = [DecisionSlot(c.created_at + timedelta(minutes=i * 5), .1 if i < 36 else .5,
                           .05, 0, 1, outdoor_temperature_forecast_c=10) for i in range(60)]
    c.current_hvac_temperature_c = 22
    c.current_outdoor_temperature_c = 10
    # Qualified active rates select timing only after actual room demand is established.
    for room in c.climate_inputs["zones"].values():
        room["temperature"] = 20.5
    period = HVACPlanningPolicy(DEFAULT_OPTIONS, model)._next_hvac_period(c)
    assert period is not None
    duration = (period["precondition_end"] - period["precondition_start"]).total_seconds() / 60
    assert duration >= 175 if compatible else duration == 90
    assert c.climate_legacy_decision["thermal_timing"]["rates_ready"] is compatible


def test_upgrade_anchor_without_structured_metadata_rejects_cross_upgrade_window():
    c, hass, model = trained_idle()
    c.current_hvac_mode = "heat"
    hass.states.get("climate.head").state = "heat"
    hass.states.values[BED] = "on"
    c.created_at += timedelta(minutes=5)
    observed = sample(c, DEFAULT_OPTIONS, hass.states, "climate.head")
    model = update(model, observed)
    c.created_at += timedelta(minutes=5)
    model = update(model, sample(c, DEFAULT_OPTIONS, hass.states, "climate.head"))
    for item in model["sources"].values():
        del item["anchor"]["conditions"]
    for room in c.climate_inputs["zones"].values():
        room["temperature"] += .1
        hass.states.get(room["temperature_entity"]).last_updated = c.created_at + timedelta(minutes=5)
    c.created_at += timedelta(minutes=5)
    after = update(model, sample(c, DEFAULT_OPTIONS, hass.states, "climate.head"))
    assert after["sources"][MAIN]["last_rejection"] == "operating_transition"
    assert after["sources"][MAIN]["buckets"] == model["sources"][MAIN]["buckets"]


def test_candidate_predictions_use_the_proposed_room_limit_and_explicit_helpers():
    c, hass, model = trained_idle(equipment_target=23)
    details = timing(c, DEFAULT_OPTIONS, "heat", model, target=23)
    assert details["rates_ready"]
    assert details["required_minutes"] == pytest.approx(50)
    assert details["zones"][MAIN]["target"] == 23
    assert details["zones"][MAIN]["predicted_finishing_temperature"] == 23
    c.climate_inputs["zones"][MAIN]["high"] = 24
    assert not timing(c, DEFAULT_OPTIONS, "heat", model, target=23)["rates_ready"]


def test_prospective_heating_rates_do_not_create_demand_for_comfortable_rooms():
    c, hass, model = trained_idle()
    c.slots = [DecisionSlot(c.created_at + timedelta(minutes=i * 5), .1 if i < 36 else .5,
                           .05, 0, 1, outdoor_temperature_forecast_c=10) for i in range(60)]
    assert timing(c, DEFAULT_OPTIONS, "heat", model)["rates_ready"]
    assert HVACPlanningPolicy(DEFAULT_OPTIONS, model)._next_hvac_period(c) is None
    assert c.climate_legacy_decision["reason"] == "room_demand_unproven"
    assert hass.states.get("climate.head").state == "off" and not c.hvac_control
