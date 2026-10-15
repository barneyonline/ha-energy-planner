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
