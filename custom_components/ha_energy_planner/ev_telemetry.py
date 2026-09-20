"""Compact measured EV performance and conservative session spending ledger."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from .ev_policy import EV_INPUTS, finite, power_capability
from .ev_runtime import settle_spending, timestamp

VERSION = 1


def reported_at(state: Any) -> datetime | None:
    """Return the timestamp of the actual sensor report, never the planner poll."""
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
        "charging": context.ev_charging,
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
