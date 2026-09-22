"""Outage, recovery and operator-intent regressions for EV charging."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.ev_policy import power_capability
from custom_components.ha_energy_planner.ev_resilience import (
    cap_manual_fallback,
    charging_status,
    fallback_charging_decision,
    fallback_power,
    recovering_load_source,
)
from custom_components.ha_energy_planner.ev_runtime import allocation_deadline
from custom_components.ha_energy_planner.models import (
    DecisionContext,
    DecisionSlot,
    InputHealth,
    OccupancyState,
    Override,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)
OPTIONS = {"ev_charge_rate_kw": 7.0, "grid_import_limit_kw": 10.0}


def context():
    ctx = DecisionContext(NOW, "outage", [DecisionSlot(NOW, .3, .05, 0, 1)], None, 40,
                          OccupancyState.UNKNOWN, InputHealth.DEGRADED,
                          ev_connected=True, ev_charging=True, ev_target_soc_percent=80)
    ctx.slots[0].baseline_load_forecast_upper_kw = 3.0
    ctx.ev_evidence = {"commanded_kw": 7.0, "load_fallback": {
        "fallback_applied": True, "fallback_remaining_seconds": 900,
        "cost_estimates_degraded": True, "live_source_status": "model_fallback",
    }}
    return ctx


def state(at, **attrs):
    return SimpleNamespace(state="1200", attributes={"sampled_at_utc": at.isoformat(), **attrs})


def test_recovery_requires_distinct_fresh_samples_and_retains_original_outage():
    prior = {"entity_id": "sensor.load", "started_at": (NOW-timedelta(minutes=18)).isoformat()}
    first = recovering_load_source(state(NOW), prior, "sensor.load", NOW)
    assert first["started_at"] == prior["started_at"]
    assert first["recovering"]
    same = recovering_load_source(state(NOW), first, "sensor.load", NOW+timedelta(minutes=2))
    assert same["recovering"]  # Re-reading permits bounded operation, never full recovery.
    assert same["recovery_stage"] == "bounded_degraded"
    assert same["recovery_first_sample"] == first["recovery_first_sample"]
    assert recovering_load_source(state(NOW+timedelta(seconds=60)), first, "sensor.load",
                                  NOW+timedelta(seconds=60)) == {}
    stale = recovering_load_source(state(NOW-timedelta(minutes=16)), first, "sensor.load", NOW)
    assert stale["recovery_first_sample"] == first["recovery_first_sample"]
    assert "recovery_observed_since" not in stale
    future = recovering_load_source(state(NOW+timedelta(seconds=1)), first, "sensor.load", NOW)
    assert future["recovery_first_sample"] == first["recovery_first_sample"]
    assert recovering_load_source(state(NOW), {}, "sensor.load", NOW) == {}


def test_outage_continues_session_but_does_not_start_automatically_or_exceed_target():
    ctx = context()
    assert fallback_charging_decision(ctx, OPTIONS, None) == (True, 7.0, "ev_load_fallback_continue")
    ctx.ev_charging = False
    assert fallback_charging_decision(ctx, OPTIONS, None)[0] is False
    override = Override("manual_ev_charging", "charge_now", NOW+timedelta(minutes=30), "charge_now")
    assert fallback_charging_decision(ctx, OPTIONS, override)[0] is True
    ctx.current_ev_soc_percent = 80
    assert fallback_charging_decision(ctx, OPTIONS, override)[0] is False
    ctx.current_ev_soc_percent = 40
    override.reason = "manual_stop"
    assert fallback_charging_decision(ctx, OPTIONS, override)[0] is False
    ctx.ev_evidence = {}
    assert fallback_charging_decision(ctx, OPTIONS, None) is None


def test_fallback_never_increases_current_and_fixed_power_pauses_without_headroom():
    ctx = context()
    ctx.slots[0].baseline_load_forecast_upper_kw = 5
    assert fallback_power(ctx, OPTIONS) == 0
    limit = SimpleNamespace(entity_id="number.amps", state="20", attributes={
        "unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})
    capability = power_capability(limit, {"ev_voltage": 230, "ev_phases": 1, "ev_limit_min": 6, "ev_limit_max": 32})
    assert capability is not None
    ctx.ev_evidence.update(power_capability=capability, commanded_kw=4.6)
    assert fallback_power(ctx, OPTIONS) == pytest.approx(4.6)
    ctx.slots[0].baseline_load_forecast_upper_kw = 7
    assert 0 < fallback_power(ctx, OPTIONS) <= 3
    ctx.slots[0].baseline_load_forecast_upper_kw = 10
    assert fallback_power(ctx, OPTIONS) == 0
    ctx.slots[0].baseline_load_forecast_upper_kw = None
    assert fallback_power(ctx, OPTIONS) == 0
    ctx.slots = []
    assert fallback_power(ctx, OPTIONS) == 0


def test_manual_fallback_deadline_cannot_extend_outage_or_bypass_electrical_capacity():
    ctx = context()
    action = SimpleNamespace(desired_state={"charge_now_until": (NOW+timedelta(hours=1)).isoformat()},
                             execute_not_after=NOW+timedelta(minutes=5))
    assert cap_manual_fallback(action, ctx, OPTIONS) is None
    deadline, rate, reason = allocation_deadline(action, {"ev_price_limit_enabled": True}, {}, NOW, 100)
    assert deadline == NOW+timedelta(minutes=15)
    assert rate == 0 and reason is None
    assert allocation_deadline(action, {}, {}, deadline, None)[2] == "ev_load_fallback_expired"
    ctx.slots[0].baseline_load_forecast_upper_kw = 10
    assert cap_manual_fallback(action, ctx, OPTIONS) == "ev_load_fallback_capacity_pause"
    ctx.slots[0].baseline_load_forecast_upper_kw = 1
    ctx.ev_evidence["load_fallback"]["fallback_remaining_seconds"] = None
    assert cap_manual_fallback(action, ctx, OPTIONS) == "ev_load_fallback_expired"
    ctx.ev_evidence = {}
    assert cap_manual_fallback(action, ctx, OPTIONS) is None
    action.desired_state = {"charge_now_until": NOW.isoformat()}
    assert allocation_deadline(action, {}, {}, NOW, None)[2] == "ev_charge_now_expired"


def test_status_distinguishes_recovery_estimates_and_override():
    ctx = context()
    assert charging_status(None, [], NOW)["summary"].startswith("Waiting")
    assert charging_status(ctx, [], NOW)["cost_estimates_degraded"]
    ctx.ev_evidence["load_fallback"]["recovery_pending"] = True
    assert "recovering" in charging_status(ctx, [], NOW)["summary"]
    ctx.ev_evidence["load_fallback"] = {"live_source_status": "unavailable"}
    assert "blocked" in charging_status(ctx, [], NOW)["summary"]
    ctx.ev_evidence = {}
    override = Override("manual_ev_charging", "charge_now", NOW+timedelta(minutes=30), "charge_now")
    assert charging_status(ctx, [override], NOW)["charge_now_until"] == override.expires_at.isoformat()
    assert charging_status(ctx, [], NOW)["summary"] == "Charging"
    ctx.ev_charging = False
    assert charging_status(ctx, [], NOW)["summary"] == "Not charging"


def test_outage_power_ceiling_survives_replans_external_increases_and_reload():
    from custom_components.ha_energy_planner.ev_resilience import retain_outage_power_ceiling

    outage = {"entity_id": "sensor.load"}
    assert retain_outage_power_ceiling(outage, 4.6) == 4.6
    reloaded = dict(outage)
    assert retain_outage_power_ceiling(reloaded, 7.36) == 4.6
    assert retain_outage_power_ceiling(reloaded, 3.68) == 3.68
    assert retain_outage_power_ceiling(reloaded, None) is None
    assert reloaded["ev_power_ceiling_kw"] == 3.68
    assert retain_outage_power_ceiling({}, 7.36) == 7.36


def test_recovery_rejects_sample_before_outage_and_resets_old_recovery_pair():
    prior = {"entity_id": "sensor.load", "started_at": NOW.isoformat()}
    rejected = recovering_load_source(state(NOW-timedelta(seconds=1)), prior, "sensor.load", NOW)
    assert "recovery_first_sample" not in rejected
    first = recovering_load_source(state(NOW), prior, "sensor.load", NOW)
    later = NOW+timedelta(minutes=31)
    restarted = recovering_load_source(state(later), first, "sensor.load", later)
    assert restarted["recovery_first_sample"] == later.isoformat()
    assert restarted["started_at"] == prior["started_at"]


def test_variable_manual_fallback_uses_enforceable_setpoint_and_normal_lease_expires():
    ctx = context()
    limit = SimpleNamespace(entity_id="number.power", state="5", attributes={
        "unit_of_measurement": "kW", "min": 1, "max": 7, "step": .1})
    ctx.ev_evidence["power_capability"] = power_capability(limit, {"ev_limit_min": 1, "ev_limit_max": 7})
    ctx.ev_evidence["commanded_kw"] = 5
    action = SimpleNamespace(desired_state={}, execute_not_after=NOW+timedelta(minutes=5))
    assert cap_manual_fallback(action, ctx, OPTIONS) is None
    assert action.desired_state["power_limit"]["value"] == 5
    assert allocation_deadline(action, {}, {}, NOW, None)[0] == NOW+timedelta(minutes=15)


def test_cost_uncertainty_does_not_block_valid_ev_evidence_but_real_faults_do():
    from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
    from custom_components.ha_energy_planner.models import ActionAsset
    from custom_components.ha_energy_planner.planner_confidence import asset_meets_confidence_threshold

    ctx = context()
    ctx.input_issues = ["household_load_model_fallback_active"]
    ctx.forecast_confidence_by_source = {
        "amber_import_price_entity": 1.0, "amber_export_price_entity": 1.0,
        "pv_forecast_entity": 1.0, "household_load_entity": .65,
    }
    options = {**DEFAULT_OPTIONS, "minimum_ev_confidence": 90, "minimum_solar_confidence": 90,
               "minimum_tariff_confidence": 90}
    assert asset_meets_confidence_threshold(ActionAsset.EV, ctx, options)
    ctx.input_issues.append("ev_soc_entity_unavailable")
    assert not asset_meets_confidence_threshold(ActionAsset.EV, ctx, options)


def test_recovery_pairs_older_sample_with_fresh_newest_without_resetting_outage():
    prior = {"entity_id": "sensor.load", "started_at": (NOW-timedelta(minutes=5)).isoformat()}
    first = recovering_load_source(state(NOW), prior, "sensor.load", NOW)
    # The first sample has aged out of the freshness window, but remains valid
    # evidence for pairing. Only the newest source sample needs to be fresh.
    later = NOW+timedelta(minutes=20)
    assert recovering_load_source(state(later-timedelta(minutes=5)), first, "sensor.load", later) == {}
    assert recovering_load_source(state(NOW), first, "sensor.load", later)["recovering"]


def test_configured_cloud_freshness_and_single_sample_observation_are_independent():
    prior = {"entity_id": "sensor.load", "started_at": (NOW-timedelta(minutes=20)).isoformat()}
    sample = state(NOW-timedelta(minutes=11))
    strict = recovering_load_source(sample, prior, "sensor.load", NOW, {"load_recovery_max_age_minutes": 10})
    assert strict["recovery_stage"] == "waiting_for_sample"
    first = recovering_load_source(sample, prior, "sensor.load", NOW)
    assert first["recovery_stage"] == "stabilizing"
    assert recovering_load_source(sample, first, "sensor.load", NOW+timedelta(seconds=89))["recovery_stage"] == (
        "stabilizing")
    stable = recovering_load_source(sample, first, "sensor.load", NOW+timedelta(seconds=90))
    assert stable["recovery_degraded_ready"]
    assert stable["started_at"] == prior["started_at"]
    expired = recovering_load_source(sample, stable, "sensor.load", NOW+timedelta(minutes=5))
    assert not expired["recovery_degraded_ready"]
    assert "recovery_observed_since" not in expired


def test_stabilizing_return_cannot_start_charge_now_or_extend_original_deadline():
    ctx = context()
    fallback = ctx.ev_evidence["load_fallback"]
    fallback.update(recovery_pending=True, recovery_degraded_ready=False)
    action = SimpleNamespace(desired_state={})
    assert cap_manual_fallback(action, ctx, OPTIONS) == "ev_load_recovery_stabilizing"
    fallback.update(recovery_degraded_ready=True, recovery_stage="bounded_degraded")
    assert cap_manual_fallback(action, ctx, OPTIONS) is None
    assert action.desired_state["load_fallback_until"] == (NOW+timedelta(minutes=15)).isoformat()
    assert "Limited recovery" in charging_status(ctx, [], NOW)["summary"]
    ctx.ev_charging = False
    assert not fallback_charging_decision(ctx, OPTIONS, None)[0]


def test_recovery_timestamp_rollback_restarts_observation_without_renewing_budget():
    prior = {"entity_id": "sensor.load", "started_at": (NOW-timedelta(minutes=10)).isoformat()}
    first = recovering_load_source(state(NOW), prior, "sensor.load", NOW)
    rolled = recovering_load_source(state(NOW-timedelta(seconds=30)), first, "sensor.load",
                                   NOW+timedelta(seconds=90))
    assert not rolled["recovery_degraded_ready"]
    assert rolled["started_at"] == prior["started_at"]
    future_observation = {**first, "recovery_observed_since": (NOW+timedelta(minutes=1)).isoformat()}
    assert recovering_load_source(state(NOW), future_observation, "sensor.load", NOW)["recovery_observed_since"] == (
        NOW.isoformat())
