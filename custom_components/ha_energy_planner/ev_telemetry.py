"""Compact measured EV performance and conservative session spending ledger."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from .ev import ev_charging_state_proves_active, ev_vehicle_suspended
from .ev_policy import EV_INPUTS, finite, power_capability
from .ev_runtime import settle_spending, timestamp

VERSION = 1


def reported_at(state: Any) -> datetime | None:
    """Return the timestamp of the actual sensor report, never the planner poll."""
    attributes = getattr(state, "attributes", {}) or {}
    if "sampled_at_utc" in attributes:
        # Cached vendor readings may be re-reported to HA indefinitely. An
        # explicit source timestamp remains authoritative, including when it
        # is invalid; HA's report time cannot make that sample fresh.
        return timestamp(attributes["sampled_at_utc"])
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    return stamp if isinstance(stamp, datetime) and stamp.tzinfo is not None else None


def measured(state: Any, now: datetime, kind: str) -> float | None:
    if state is None:
        return None
    stamp = reported_at(state)
    if stamp is None or not 0 <= (now - stamp).total_seconds() <= 600:
        return None
    value = finite(state.state)
    factor = ({"W": 0.001, "kW": 1} if kind == "power" else {"Wh": 0.001, "kWh": 1}).get(
        state.attributes.get("unit_of_measurement")
    )
    return value * factor if value is not None and value >= 0 and factor is not None else None


def sample_ev(hass: Any, entry_data: Mapping[str, Any], options: Mapping[str, Any], context: Any) -> dict[str, Any]:
    states = {key: hass.states.get(entry_data[key]) if entry_data.get(key) else None for key in EV_INPUTS}
    capability = power_capability(states["ev_power_limit_entity"], options)
    return {
        "identity": ["performance_v3", entry_data.get("ev_vehicle_id")]
        + [entry_data.get(key) for key in (*EV_INPUTS, "ev_soc_entity", "ev_charging_entity")]
        + [options.get(key) for key in ("ev_charge_rate_kw", "ev_limit_min", "ev_limit_max", "ev_voltage", "ev_phases")]
        + (
            [capability.unit, capability.minimum, capability.maximum, capability.step, capability.kw_per_unit]
            if capability
            else []
        ),
        "configured_rate_kw": options.get("ev_charge_rate_kw"),
        "commanded_kw": capability.current * capability.kw_per_unit if capability else options.get("ev_charge_rate_kw"),
        "energy_reported_at": stamp.isoformat() if (stamp := reported_at(states["ev_energy_entity"])) else None,
        "at": context.created_at.isoformat(),
        "soc": context.current_ev_soc_percent,
        "connected": context.ev_connected,
        "connected_reported_at": (stamp.isoformat() if (stamp := reported_at(
            hass.states.get(entry_data["ev_connected_entity"]) if entry_data.get("ev_connected_entity") else None
        )) else None),
        "charging": context.ev_charging,
        "vehicle_suspended": ev_vehicle_suspended(getattr(
            hass.states.get(entry_data.get("ev_charging_entity")), "state", None
        )) if entry_data.get("ev_charging_entity") else False,
        "target_soc": getattr(context, "ev_target_soc_percent", None),
        "suspension_identity": suspension_identity(entry_data),
        "charging_reported_at": (stamp.isoformat() if (stamp := reported_at(
            hass.states.get(entry_data["ev_charging_entity"]) if entry_data.get("ev_charging_entity") else None
        )) else None),
        "charging_feedback": getattr(
            hass.states.get(entry_data["ev_charging_entity"]) if entry_data.get("ev_charging_entity") else None,
            "state", None),
        "power_kw": measured(states["ev_power_entity"], context.created_at, "power"),
        "energy_kwh": measured(states["ev_energy_entity"], context.created_at, "energy"),
        "power_mapped": bool(entry_data.get("ev_power_entity")),
        "energy_mapped": bool(entry_data.get("ev_energy_entity")),
        "power_limit_mapped": bool(entry_data.get("ev_power_limit_entity")),
        "power_capability": capability,
        "price": context.slots[0].import_price if context.slots else None,
        "normal_ceiling": float(options.get("ev_max_import_price", 1))
        if options.get("ev_price_limit_enabled")
        else None,
    }


def update_ev_telemetry(
    previous: Mapping[str, Any], sample: dict[str, Any], *, reserved_kw: float = 0
) -> dict[str, Any]:
    """Learn only measured intervals; never clear uncertain spending on reload."""
    old = settle_spending(dict(previous), datetime.fromisoformat(sample["at"]))
    record = (
        deepcopy(old)
        if old.get("version") == VERSION
        else {
            "version": VERSION,
            "emergency_spend": max(finite(old.get("emergency_spend")) or 0, 0),
        }
    )
    raw_spend = old.get("emergency_spend", 0)
    if old and (old.get("version", VERSION) != VERSION or finite(raw_spend) is None or float(raw_spend) < 0):
        record["budget_uncertain"] = True
    record["emergency_spend"] = max(finite(record.get("emergency_spend")) or 0, 0)
    if old.get("budget_uncertain"):
        record["budget_uncertain"] = True
    for key in ("performance", "pending", "last_sample"):
        if key in record and not isinstance(record[key], dict):
            record[key] = {}
    if record.get("identity") != sample["identity"]:
        record["performance"] = {"bands": {}, "aggregate": {}}
        record["pending"] = {}
        record.pop("last_sample", None)
    record["identity"] = sample["identity"]
    prior = old.get("last_sample", {})
    if not isinstance(prior, dict):
        prior = {}
    same_identity = old.get("identity") == sample["identity"]
    record.update(vehicle_suspension_evidence(old, sample))
    try:
        elapsed = (datetime.fromisoformat(sample["at"]) - datetime.fromisoformat(prior["at"])).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        elapsed = 0
    power = finite(prior.get("reserved_kw")) or 0
    if elapsed > 0 and power > 0 and prior.get("charging") is not False and not old.get("command_exposure"):
        ceiling, price = finite(prior.get("normal_ceiling")), finite(prior.get("price"))
        # Whole commanded energy is a conservative grid-energy upper bound;
        # accounting never grants credit for unverified solar or missing samples.
        if ceiling is not None:
            premium = max((price if price is not None else ceiling) - ceiling, 0)
            record["emergency_spend"] = max(float(record.get("emergency_spend", 0)), 0) + power * elapsed * premium
    exposure = previous.get("command_exposure", {})
    before_meter, after_meter = finite(prior.get("energy_kwh")), finite(sample.get("energy_kwh"))
    price, ceiling = finite(prior.get("price")), finite(prior.get("normal_ceiling"))
    meter_start, meter_end = timestamp(prior.get("energy_reported_at")), timestamp(sample.get("energy_reported_at"))
    prior_at, sample_at = timestamp(prior.get("at")), datetime.fromisoformat(sample["at"])
    meter_hours = (meter_end - meter_start).total_seconds() / 3600 if meter_start and meter_end else 0
    meter_covers_exposure = bool(meter_start and meter_end and prior_at
                                and meter_start <= prior_at < meter_end <= sample_at)
    if (
        same_identity
        and 0 < elapsed <= 1 / 6
        and prior.get("charging") is True
        and isinstance(exposure, dict)
        and exposure.get("at") == prior.get("at")
        and meter_covers_exposure
        and 0 < meter_hours <= 1 / 6
        and before_meter is not None
        and after_meter is not None
        and 0 <= after_meter - before_meter <= max(power, reserved_kw, 0) * meter_hours * 1.25
        and price is not None
        and price == finite(sample.get("price"))
        and ceiling is not None
        and not record.get("budget_uncertain")
    ):
        # Metered EV energy is still a conservative grid upper bound: do not
        # refund assumed solar without separately observed grid attribution.
        unmetered_hours = (sample_at - meter_end).total_seconds() / 3600 if meter_end else elapsed
        measured_upper_bound = (after_meter - before_meter) * max(price - ceiling, 0)
        measured_upper_bound += unmetered_hours * max(finite(exposure.get("cost_per_hour")) or 0, 0)
        record["emergency_spend"] = min(record["emergency_spend"],
            max(finite(previous.get("emergency_spend")) or 0, 0) + measured_upper_bound)
        record["spending_source"] = "measured_energy_conservative_grid"
    if same_identity and 0 < elapsed <= 1 / 6 and prior.get("charging") is True and sample.get("charging") is True:
        before_soc, after_soc = finite(prior.get("soc")), finite(sample.get("soc"))
        energy = None
        before_energy, after_energy = finite(prior.get("energy_kwh")), finite(sample.get("energy_kwh"))
        meter_interval_matches = bool(meter_covers_exposure and 0 < meter_hours <= 1 / 6
                                      and abs(meter_hours - elapsed) <= 10 / 3600)
        if before_energy is not None and after_energy is not None and meter_interval_matches:
            delta = after_energy - before_energy
            energy = delta * elapsed / meter_hours if delta >= 0 else None
        energy_reset = before_energy is not None and after_energy is not None and after_energy < before_energy
        if (
            energy is None
            and not energy_reset
            and (p0 := finite(prior.get("power_kw"))) is not None
            and (p1 := finite(sample.get("power_kw"))) is not None
        ):
            energy = (p0 + p1) / 2 * elapsed
        max_rate = max(finite(sample.get("configured_rate_kw", sample["identity"][-5])) or 0, power, 0.1)
        if (
            energy is not None
            and 0 <= energy <= max_rate * elapsed * 1.25
            and before_soc is not None
            and after_soc is not None
            and 0 <= after_soc - before_soc <= 20
            and (energy > 0 or after_soc == before_soc)
        ):
            # Keep samples within a band; do not attribute crossing intervals to
            # an unobserved high-SOC charging rate.
            def band(soc: float) -> str:
                return "0" if soc < 60 else "60" if soc < 80 else "80" if soc < 90 else "90"

            keys = ["aggregate"]
            if band(before_soc) == band(after_soc):
                keys.append(band(before_soc))
            for key in keys:
                row = record.setdefault("pending", {}).setdefault(
                    key, {"energy": 0, "gain": 0, "minutes": 0, "commanded_energy": 0}
                )
                row["energy"] += energy
                commanded = max(finite(prior.get("commanded_kw")) or max_rate,
                                finite(sample.get("commanded_kw")) or max_rate)
                row["commanded_energy"] = row.get("commanded_energy", 0) + commanded * elapsed
                row["gain"] += after_soc - before_soc
                row["minutes"] += elapsed * 60
    ended = prior.get("charging") is True and sample.get("charging") is False
    disconnected = sample.get("connected") is False and sample.get("charging") is not True
    if ended or disconnected:
        model = record.setdefault("performance", {"bands": {}, "aggregate": {}})
        for key, value in record.pop("pending", {}).items():
            if value["minutes"] < 5 or value["gain"] <= 0 or value["energy"] <= 0:
                continue
            row = (
                model.setdefault("aggregate", {})
                if key == "aggregate"
                else model.setdefault("bands", {}).setdefault(key, {})
            )
            for metric in ("energy", "gain", "minutes", "commanded_energy"):
                row[metric] = row.get(metric, 0) + value.get(metric, 0)
            row["sessions"] = row.get("sessions", 0) + 1
            row["soc_per_kwh"] = row["gain"] / row["energy"]
            row["delivery_fraction"] = min(row["energy"] / max(row["commanded_energy"], row["energy"]), 1)
    if disconnected:
        record["emergency_spend"] = 0.0
        record.pop("command_exposure", None)
        record.pop("budget_uncertain", None)
    record["last_sample"] = {key: value for key, value in sample.items() if key != "power_capability"}
    record["last_sample"]["reserved_kw"] = 0 if sample.get("charging") is False else max(reserved_kw, 0)
    record["delivery_status"] = (
        "stalled"
        if sample.get("charging") is True
        and (
            sample.get("power_kw") == 0
            or (sample.get("power_kw") is None and same_identity and 0 < elapsed <= 1 / 6
                and before_meter is not None and after_meter == before_meter and meter_covers_exposure)
        )
        else "unavailable"
        if (
            (sample.get("power_mapped") and sample.get("power_kw") is None)
            or (sample.get("energy_mapped") and sample.get("energy_kwh") is None)
        )
        else "observed"
    )
    return record


def suspension_identity(entry_data: Mapping[str, Any]) -> list[Any]:
    """Scope a refusal to vehicle/session and control topology, not power tuning."""
    return [entry_data.get(key) for key in (
        "ev_vehicle_id", "ev_connected_entity", "ev_charging_entity",
        "ev_soc_entity", "ev_charger_entity", "ev_smart_charging_entity",
    )]


def vehicle_suspension_evidence(previous: Mapping[str, Any], sample: Mapping[str, Any]) -> dict[str, Any]:
    """Retain vehicle refusal after our stop, without claiming target completion."""
    identity = sample.get("suspension_identity", sample.get("identity"))
    prior = previous.get("last_sample", {})
    prior = prior if isinstance(prior, dict) else {}
    refusal = previous.get("vehicle_refusal", {})
    prior_identity = refusal.get("identity") if isinstance(refusal, dict) else None
    if (isinstance(prior_identity, list) and isinstance(identity, list) and len(identity) == 6
            and len(prior_identity) == 6):
        # Vehicle resolution can temporarily withhold identity/SOC mapping.
        # That is not a new car or a new plug session. Runtime generation is
        # intentionally excluded: it changes with SOC and resets on reload.
        identity = [old if new is None and index in {0, 3} else new
                    for index, (old, new) in enumerate(zip(prior_identity, identity, strict=True))]
        compatible = all(old == new or old is None and index in {0, 3}
                         for index, (old, new) in enumerate(zip(prior_identity, identity, strict=True)))
    else:
        compatible = prior_identity == identity
    refusal = dict(refusal) if isinstance(refusal, dict) and compatible else {}
    if refusal:
        refusal["identity"] = identity
    before, after = finite(refusal.get("target_soc")), finite(sample.get("target_soc"))
    observed_at = timestamp(sample.get("charging_reported_at")) or timestamp(sample.get("at"))
    recorded_refusal_at = timestamp(refusal.get("at"))
    refusal_at = recorded_refusal_at or observed_at
    connection_at = timestamp(sample.get("connected_reported_at", sample.get("at")))
    prior_connection_at = timestamp(prior.get("connected_reported_at", prior.get("at")))
    disconnected_at = timestamp(refusal.get("disconnected_at"))
    disconnect_confirmed = (sample.get("connected") is False and connection_at is not None
                            and (refusal_at is None or connection_at >= refusal_at))
    reconnected = sample.get("connected") is True and (
        disconnected_at is not None and connection_at is not None and connection_at >= disconnected_at
        or prior.get("connected") is False and prior_connection_at is not None
        and (refusal_at is None or prior_connection_at >= refusal_at)
    )
    charging_confirmed = (ev_charging_state_proves_active(sample["charging_feedback"])
                          if "charging_feedback" in sample else sample.get("charging") is True)
    charging_at = timestamp(sample.get("charging_reported_at", sample.get("at")))
    # Cached or out-of-order reports cannot prove delivery resumed after refusal.
    charging_confirmed = charging_confirmed and (
        recorded_refusal_at is None or charging_at is not None and charging_at >= recorded_refusal_at
    )
    if sample.get("vehicle_suspended"):
        # Current refusal still wins, but retain unplug proof through stale or
        # contradictory status polls so a verified new session can reconsider.
        # A new session with current refusal owns a fresh boundary: its previous
        # unplug evidence must never clear that new refusal on the next poll.
        first_at = observed_at.isoformat() if observed_at else sample.get("at")
        event_at = sample.get("at", first_at) if reconnected else refusal.get("at", first_at)
        refusal = {"identity": identity, "at": event_at,
                   "target_soc": sample.get("target_soc"), "feedback": sample.get("charging_feedback"),
                   "source": "charging_feedback"}
        if disconnect_confirmed:
            refusal["disconnected_at"] = connection_at.isoformat() if connection_at else None
        elif disconnected_at is not None and not reconnected:
            refusal["disconnected_at"] = disconnected_at.isoformat()
    elif (charging_confirmed or disconnect_confirmed or reconnected
            or before is not None and after is not None and before != after):
        refusal = {}
    return {"vehicle_suspended": bool(refusal), "vehicle_refusal": refusal}
