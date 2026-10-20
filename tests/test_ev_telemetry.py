"""Source timestamps govern cached EV measurements and spending evidence."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.ev_telemetry import measured, reported_at, sample_ev, update_ev_telemetry

NOW = datetime(2026, 10, 15, 9, 46, 50, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "ev_telemetry" / "enphase_cached_samples.json"


def source_states():
    payload = json.loads(FIXTURE.read_text())
    return {
        key: SimpleNamespace(
            state=payload[key]["state"],
            attributes=payload[key]["attributes"],
            last_updated=datetime.fromisoformat(payload[key]["last_updated"]),
            last_reported=datetime.fromisoformat(payload[key]["last_reported"]),
        )
        for key in ("power", "energy")
    }


def observed_sample(states, at):
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    context = SimpleNamespace(
        created_at=at, current_ev_soc_percent=80, ev_connected=True, ev_charging=True, slots=[]
    )
    return sample_ev(hass, {"ev_power_entity": "power", "ev_energy_entity": "energy"}, {}, context)


def test_enphase_cached_payload_uses_actual_source_time_for_both_measurements():
    states = source_states()
    for state in states.values():
        assert reported_at(state) == NOW
        state.last_reported = state.last_updated = NOW + timedelta(minutes=11)
    sample = observed_sample(states, NOW + timedelta(minutes=11))
    assert sample["energy_reported_at"] == NOW.isoformat()
    assert sample["power_kw"] is None
    assert sample["energy_kwh"] is None
    assert update_ev_telemetry({}, sample)["delivery_status"] == "unavailable"


@pytest.mark.parametrize("raw", [None, "", "invalid", "2026-10-15T09:46:50", 42])
def test_invalid_authoritative_time_cannot_fall_back_to_fresh_ha_report(raw):
    state = source_states()["power"]
    state.attributes["sampled_at_utc"] = raw
    assert reported_at(state) is None
    assert measured(state, NOW, "power") is None


@pytest.mark.parametrize("offset, expected", [(-600, 0), (-601, None), (1, None)])
def test_source_sample_age_and_future_time_are_checked(offset, expected):
    state = source_states()["power"]
    state.attributes["sampled_at_utc"] = (NOW + timedelta(seconds=offset)).isoformat()
    assert measured(state, NOW, "power") == expected


def test_generic_sensors_keep_ha_timestamp_fallback_and_datetime_source_is_supported():
    state = source_states()["power"]
    state.attributes["sampled_at_utc"] = NOW
    assert reported_at(state) == NOW
    del state.attributes["sampled_at_utc"]
    assert reported_at(state) == state.last_reported
    del state.last_reported
    assert reported_at(state) == state.last_updated
    state.last_updated = NOW.replace(tzinfo=None)
    assert reported_at(state) is None
    assert reported_at(None) is None


def test_cached_energy_re_report_cannot_refund_commanded_spending():
    states = source_states()
    states["power"].state = "6000"
    sample = observed_sample(states, NOW)
    sample.update(price=0.4, normal_ceiling=0.2)
    record = update_ev_telemetry({}, sample, reserved_kw=6)
    record["command_exposure"] = {"at": NOW.isoformat(), "cost_per_hour": 3}
    for state in states.values():
        state.last_reported = state.last_updated = NOW + timedelta(minutes=5)
    cached = observed_sample(states, NOW + timedelta(minutes=5))
    cached.update(price=0.4, normal_ceiling=0.2)
    updated = update_ev_telemetry(record, cached, reserved_kw=6)
    assert updated["emergency_spend"] == pytest.approx(0.25)
    assert "spending_source" not in updated


def test_late_disconnect_does_not_erase_confirmation_refusal():
    from custom_components.ha_energy_planner.ev_telemetry import vehicle_suspension_evidence
    record = {"vehicle_refusal": {"identity": ["vehicle"], "at": NOW.isoformat(), "target_soc": 80}}
    sample = {"suspension_identity": ["vehicle"], "at": (NOW + timedelta(seconds=10)).isoformat(),
              "connected": False, "charging": False, "connected_reported_at": (NOW-timedelta(seconds=1)).isoformat()}
    assert vehicle_suspension_evidence(record, sample)["vehicle_suspended"]
    sample["connected_reported_at"] = (NOW + timedelta(seconds=5)).isoformat()
    assert not vehicle_suspension_evidence(record, sample)["vehicle_suspended"]


def test_refusal_scope_does_not_depend_on_power_tuning():
    from custom_components.ha_energy_planner.ev_telemetry import suspension_identity
    data = {"ev_vehicle_id": "vehicle", "ev_charger_entity": "switch.charger", "ev_session_generation": 1}
    scope = suspension_identity(data)
    assert scope == suspension_identity({**data, "ev_power_limit_entity": "number.limit", "ev_charge_rate_kw": 5})
    assert scope == suspension_identity({**data, "ev_session_generation": 2})
    assert scope != suspension_identity({**data, "ev_charger_entity": "switch.other"})


def test_vehicle_resolution_gap_and_runtime_generation_do_not_reset_refusal():
    from custom_components.ha_energy_planner.ev_telemetry import suspension_identity, vehicle_suspension_evidence
    data = {"ev_vehicle_id": "car", "ev_soc_entity": "sensor.car_soc", "ev_charger_entity": "switch.charger"}
    record = {"vehicle_refusal": {"identity": suspension_identity(data), "target_soc": 80}}
    missing = {"suspension_identity": suspension_identity({"ev_charger_entity": "switch.charger"})}
    evidence = vehicle_suspension_evidence(record, missing)
    assert evidence["vehicle_suspended"]
    sample = {"suspension_identity": suspension_identity({**data, "ev_session_generation": 99}),
              "soc": 79.5, "target_soc": 80}
    assert vehicle_suspension_evidence(evidence, sample)["vehicle_suspended"]
    sample["suspension_identity"] = suspension_identity({**data, "ev_vehicle_id": "other"})
    assert not vehicle_suspension_evidence(evidence, sample)["vehicle_suspended"]


def test_first_refusal_can_bind_later_vehicle_identity_without_discarding_wait():
    from custom_components.ha_energy_planner.ev_telemetry import suspension_identity, vehicle_suspension_evidence
    record = {"vehicle_refusal": {"identity": suspension_identity({"ev_charger_entity": "switch.charger"})}}
    sample = {"suspension_identity": suspension_identity({"ev_vehicle_id": "car", "ev_soc_entity": "sensor.soc",
                                                         "ev_charger_entity": "switch.charger"})}
    evidence = vehicle_suspension_evidence(record, sample)
    assert evidence["vehicle_suspended"]
    assert evidence["vehicle_refusal"]["identity"][0] == "car"



def test_verified_unplug_replug_resets_wait_after_repeated_refusal_feedback():
    from custom_components.ha_energy_planner.ev_telemetry import vehicle_suspension_evidence
    record = {"vehicle_suspended": True,
              "vehicle_refusal": {"identity": ["vehicle"], "at": NOW.isoformat(), "target_soc": 80}}
    disconnected = {"suspension_identity": ["vehicle"], "at": (NOW+timedelta(minutes=5)).isoformat(),
                    "connected": False, "charging": False, "vehicle_suspended": True,
                    "target_soc": 80, "charging_feedback": "SUSPENDED_EV",
                    "connected_reported_at": (NOW+timedelta(minutes=4)).isoformat()}
    record.update(vehicle_suspension_evidence(record, disconnected))
    assert record["vehicle_suspended"]
    # A refresh of the same contradictory feedback must retain unplug proof.
    disconnected["at"] = (NOW+timedelta(minutes=6)).isoformat()
    record.update(vehicle_suspension_evidence(record, disconnected))
    record["last_sample"] = disconnected
    replugged = {**disconnected, "at": (NOW+timedelta(minutes=7)).isoformat(),
                 "connected": True, "vehicle_suspended": False, "charging_feedback": "SUSPENDED_EVSE",
                 "connected_reported_at": (NOW+timedelta(minutes=7)).isoformat()}
    assert not vehicle_suspension_evidence(record, replugged)["vehicle_suspended"]


def test_finishing_feedback_does_not_count_as_positive_vehicle_charging():
    from custom_components.ha_energy_planner.ev_telemetry import vehicle_suspension_evidence
    record = {"vehicle_suspended": True, "vehicle_refusal": {"identity": ["vehicle"], "target_soc": 80}}
    sample = {"suspension_identity": ["vehicle"], "charging": True, "charging_feedback": "Finishing",
              "connected": True, "target_soc": 80}
    assert vehicle_suspension_evidence(record, sample)["vehicle_suspended"]



def test_new_session_current_refusal_is_not_cleared_by_its_old_unplug_report():
    from custom_components.ha_energy_planner.ev_telemetry import vehicle_suspension_evidence
    record = {"vehicle_refusal": {"identity": ["vehicle"], "at": NOW.isoformat(),
                                 "disconnected_at": (NOW+timedelta(minutes=1)).isoformat()}}
    sample = {"suspension_identity": ["vehicle"], "connected": True, "charging": False,
              "vehicle_suspended": True, "charging_feedback": "SUSPENDED_EV", "charging_reported_at": NOW.isoformat(),
              "at": (NOW+timedelta(minutes=3)).isoformat(),
              "connected_reported_at": (NOW+timedelta(minutes=2)).isoformat()}
    evidence = vehicle_suspension_evidence(record, sample)
    assert evidence["vehicle_suspended"]
    late_unplug = {**sample, "connected": False, "vehicle_suspended": False, "charging_feedback": "SUSPENDED_EVSE",
                   "connected_reported_at": (NOW+timedelta(minutes=1)).isoformat()}
    assert vehicle_suspension_evidence(evidence, late_unplug)["vehicle_suspended"]


@pytest.mark.parametrize("offset,cleared", [(-1, False), (None, False), (1, True)])
def test_only_charging_reports_after_refusal_can_reset_wait(offset, cleared):
    from custom_components.ha_energy_planner.ev_telemetry import vehicle_suspension_evidence
    record = {"vehicle_suspended": True, "vehicle_refusal": {
        "identity": ["vehicle"], "at": NOW.isoformat(), "target_soc": 80}}
    sample = {"suspension_identity": ["vehicle"], "connected": True,
              "at": (NOW+timedelta(seconds=10)).isoformat(), "target_soc": 80,
              "charging": True, "charging_feedback": "Charging",
              "charging_reported_at": (NOW+timedelta(seconds=offset)).isoformat() if offset is not None else None}
    assert vehicle_suspension_evidence(record, sample)["vehicle_suspended"] is not cleared
