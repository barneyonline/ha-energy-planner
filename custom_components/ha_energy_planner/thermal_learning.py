"""Bounded, source-specific net temperature learning, independent of energy claims."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from math import ceil, floor
from statistics import median
from typing import Any

from .climate_inputs import finite, instant
from .const import (
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MAX_LEAD_MINUTES,
    CONF_HVAC_THERMAL_LEARNING_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from .models import DecisionContext
from .zone_temperature import bounds, temperatures

MODEL_VERSION = 3
MIN_WINDOWS = 12
MAX_WINDOWS = 96


def sample(context: DecisionContext, options: dict[str, Any], states: Any, head: str) -> dict[str, Any]:
    """Capture sensor reports and topology, not just repeated planner snapshots."""
    sources = temperatures(context)
    if not sources:
        sources = {"head": {"temperature_entity": head, "temperature": context.current_hvac_temperature_c}}
    readings = {}
    for zone, room in sources.items():
        entity = room.get("temperature_entity")
        state = states.get(entity)
        reported = getattr(state, "last_reported", getattr(state, "last_updated", None))
        identity = sha256(str((entity, head, sorted(context.climate_zone_entities))).encode()).hexdigest()[:24]
        actuator = states.get(zone) if zone != "head" else states.get(head)
        actuator_state = getattr(actuator, "state", "unknown")
        zone_mode = context.current_hvac_mode
        if actuator_state in {"unknown", "unavailable"}:
            zone_mode = None
        elif actuator_state == "off":
            zone_mode = "off"
        elif (context.climate_inputs.get("power_source_type") == "measured"
              and context.current_hvac_power_kw is not None and context.current_hvac_power_kw < 0.1):
            zone_mode = "idle"
        readings[zone] = {
            "identity": identity, "sensor": entity, "temperature": room.get("temperature"),
            "input_status": room.get("temperature_source", {}).get("status"), "mode": zone_mode,
            "reported_at": reported.isoformat() if reported else None,
        }
    return {
        "at": context.created_at.isoformat(),
        "learning_enabled": options.get(CONF_HVAC_THERMAL_LEARNING_ENABLED, True),
        "mode": context.current_hvac_mode,
        "power": context.current_hvac_power_kw,
        "power_source_type": context.climate_inputs.get("power_source_type"),
        "zones": {entity: getattr(states.get(entity), "state", None) for entity in context.climate_zone_entities},
        "intervention": any(override.kind == "manual_hvac" for override in context.active_overrides) or bool(
            context.hvac_control.get("command_confirmation") or context.hvac_control.get("pending_restore")
            or context.hvac_control.get("required_evidence_lost")
            or (context.hvac_control.get("phase") == "preconditioning"
                and context.hvac_control.get("main_state_committed") is not True)
        ),
        "sources": readings,
    }


def update(model: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Keep independent anchors; flat and coarse observations can span 30 minutes."""
    now = instant(current.get("at"))
    enabled = bool(current.get("learning_enabled", True))
    result = deepcopy(model) if model.get("model_version") == MODEL_VERSION else {"model_version": MODEL_VERSION}
    result.update(learning_enabled=enabled, evaluated_at=current.get("at"))
    result["sources"] = {key: value for key, value in result.get("sources", {}).items()
                         if key in current.get("sources", {})}
    for zone, reading in current.get("sources", {}).items():
        item = result["sources"].setdefault(zone, {})
        if item.get("identity") != reading["identity"]:
            item = result["sources"][zone] = {"identity": reading["identity"], "buckets": {}, "rejected": {}}
        item.update(sensor=reading.get("sensor"), temperature=reading.get("temperature"),
                    reported_at=reading.get("reported_at"), input_status=reading.get("input_status"))
        point = {**current, **reading, "observed_at": current.get("at")}
        point.pop("sources", None)
        previous = item.get("anchor")
        report = instant(reading.get("reported_at"))
        reason = None
        if not enabled:
            reason = "learning_disabled"
        elif now is None or report is None or report > now or finite(reading.get("temperature")) is None:
            reason = "invalid_sensor"
        elif current.get("intervention"):
            reason = "intervention_or_uncertain_command"
        elif point.get("mode") not in {"heat", "cool", "off", "idle"}:
            reason = "unsupported_mode"
        elif previous:
            before = instant(previous.get("at")) or now
            observed_before = instant(previous.get("observed_at")) or before
            elapsed = (now - observed_before).total_seconds() / 60
            if elapsed <= 0 or elapsed > 30.5:
                reason = "sampling_gap"
            elif any(previous.get(key) != point.get(key) for key in ("mode", "zones")):
                reason = "operating_transition"
            elif (finite(previous.get("power")) is not None and finite(current.get("power")) is not None
                  and (float(previous["power"]) >= 0.1) != (float(current["power"]) >= 0.1)):
                reason = "power_transition"
            elif reading.get("reported_at") == previous.get("reported_at"):
                continue
            elif report < before:
                reason = "invalid_sensor"
            elif (previous_report := instant(previous.get("reported_at"))) is None or previous_report < before:
                # The anchor's value predates evidence of this operating regime.
                # A new report is a usable anchor, not a recent temperature delta.
                reason = "sensor_observation_gap"
            elif (elapsed := (report - previous_report).total_seconds() / 60) > 30.5:
                reason = "sensor_observation_gap"
            elif elapsed < 5:
                continue
            else:
                delta = float(reading["temperature"]) - float(previous["temperature"])
                mode = str(point["mode"])
                observed_rate = delta * 60 / elapsed
                # Whole-degree steps and flat readings need a longer interval.
                if (abs(delta) < 0.05 or abs(observed_rate) > (3 if mode in {"off", "idle"} else 6)) and elapsed < 30:
                    continue
                if abs(observed_rate) > (3 if mode in {"off", "idle"} else 6):
                    reason = "implausible_rate"
                else:
                    bucket_name = mode if mode in {"heat", "cool"} else "passive"
                    bucket = item["buckets"].setdefault(bucket_name, {})
                    signed_rate = -observed_rate if mode == "cool" else observed_rate
                    values = _recent_windows([*bucket.get("windows", []), {
                        "rate": signed_rate, "minutes": elapsed, "at": current["at"],
                    }], now)[-MAX_WINDOWS:]
                    bucket.update(windows=values, rate_c_per_hour=median(row["rate"] for row in values),
                                  minutes=sum(row["minutes"] for row in values),
                                  count=len(values), last_at=current["at"])
                    power = finite(current.get("power"))
                    if mode in {"heat", "cool"} and current.get("power_source_type") == "measured" and power:
                        loads = _recent_windows([*item.get("loads_kw", []), {
                            "power": power, "at": current["at"],
                        }], now)[-MAX_WINDOWS:]
                        item["loads_kw"] = loads
        if reason:
            item["rejected"][reason] = min(1_000_000, item["rejected"].get(reason, 0) + 1)
            item["last_rejection"] = reason
        # Invalid evidence breaks the interval; never train across the gap.
        if reason is None or reason == "sensor_observation_gap":
            # Subsequent intervals use the sensor clock, including delayed reports.
            # Keep an initial old value outside the new regime until it refreshes.
            point["at"] = report.isoformat() if previous and report is not None else current.get("at")
            item["anchor"] = point
        else:
            item["anchor"] = None
    if now is not None:
        result["enabled"] = False
        for mode, key in (("heat", "active_heat_rate_c_per_hour"), ("cool", "active_cool_rate_c_per_hour"),
                          ("passive", "passive_indoor_drift_c_per_hour")):
            ready_rates = [rate(result, zone, mode, now) for zone in result["sources"]]
            if ready_rates and all(value is not None for value in ready_rates):
                result[key] = {"average": min(value for value in ready_rates if value is not None),
                               "sample_count": MIN_WINDOWS}
                result["enabled"] = True
            else:
                result.pop(key, None)
        loads = _load_windows(result, now)
        result["active_hvac_load_kw"] = {
            "average": median(row["power"] for row in loads) if loads else None, "sample_count": len(loads),
        }
    return result


def _recent_windows(rows: list[Any], now: Any) -> list[dict[str, Any]]:
    """An expired sample must not regain authority when a new sample arrives."""
    return [row for row in rows if isinstance(row, dict)
            and (at := instant(row.get("at"))) is not None and timedelta(0) <= now - at <= timedelta(days=14)]


def _load_windows(model: dict[str, Any], now: Any) -> list[dict[str, Any]]:
    """Combine shared-unit measurements without room-order bias or duplicate credit."""
    by_time: dict[datetime, dict[str, Any]] = {}
    for item in model.get("sources", {}).values():
        for row in _recent_windows(item.get("loads_kw", []), now):
            at = instant(row["at"])
            assert at is not None  # The same timestamp passed the freshness filter.
            by_time[at] = row
    selected: list[dict[str, Any]] = []
    last = None
    for at in sorted(by_time, reverse=True):
        if last is None or last - at >= timedelta(minutes=5):
            selected.append(by_time[at])
            last = at
            if len(selected) == MAX_WINDOWS:
                break
    return selected[::-1]


def _bucket_summary(bucket: dict[str, Any], now: Any) -> dict[str, Any]:
    """Recompute readiness from the dated evidence, including between updates."""
    if "windows" not in bucket:
        return bucket
    rows = _recent_windows(bucket["windows"], now)
    return {"count": len(rows), "minutes": sum(row["minutes"] for row in rows),
            "rate_c_per_hour": median(row["rate"] for row in rows) if rows else None,
            "last_at": rows[-1]["at"] if rows else None}


def rate(model: dict[str, Any], zone: str, mode: str, now: Any) -> float | None:
    """Readiness belongs to each source/mode and expires independently."""
    bucket = _bucket_summary(model.get("sources", {}).get(zone, {}).get("buckets", {}).get(mode, {}), now)
    last = instant(bucket.get("last_at"))
    value = finite(bucket.get("rate_c_per_hour"))
    if (not model.get("learning_enabled", True) or value is None or last is None
            or not timedelta(0) <= now - last <= timedelta(days=14)
            or bucket.get("count", 0) < MIN_WINDOWS or bucket.get("minutes", 0) < 60):
        return None
    return value


def timing(context: DecisionContext, options: Mapping[str, Any], mode: str,
           model: dict[str, Any] | None = None, *,
           delay_minutes: float = 0, available_minutes: float | None = None,
           deadline_minutes: float | None = None) -> dict[str, Any]:
    """One duration calculation shared by fallback and economic candidates."""
    model = model if model is not None else context.climate_inputs.get("thermal_model", {})
    base = float(options.get(CONF_HVAC_PRECONDITION_LEAD_MINUTES, 90))
    maximum = max(base, float(options.get(CONF_HVAC_PRECONDITION_MAX_LEAD_MINUTES, 180)))
    rows = temperatures(context) or {"head": {"temperature": context.current_hvac_temperature_c}}
    predictions: dict[str, Any] = {}
    required = 0.0
    ready = bool(options.get(CONF_HVAC_THERMAL_LEARNING_ENABLED, True))
    for zone, room in rows.items():
        current = finite(room.get("temperature"))
        low, high = bounds(context, room)
        target = high if mode == "heat" else low
        learned = rate(model, zone, mode, context.created_at)
        drift = rate(model, zone, "passive", context.created_at)
        projected_start = current + (drift or 0) * delay_minutes / 60 if current is not None else None
        usable = projected_start is not None and target is not None and learned is not None and learned > 0
        ready = ready and usable
        minutes = None
        if usable:
            assert current is not None and target is not None and learned is not None
            assert projected_start is not None
            delta = max(target - projected_start if mode == "heat" else projected_start - target, 0)
            minutes = delta * 60 / learned
            if deadline_minutes is not None and drift is not None:
                passive_gain = drift if mode == "heat" else -drift
                # Solve rate*lead = gap - passive_gain*(deadline-lead).
                # A solution beyond the deadline cannot assume a negative wait.
                net_gain = learned - passive_gain
                if net_gain > 0:
                    solved = max((delta * 60 - passive_gain * deadline_minutes) / net_gain, 0)
                    if solved <= deadline_minutes:
                        minutes = solved
            required = max(required, minutes)
        predictions[zone] = {"temperature": current, "projected_start_temperature": projected_start,
                             "target": target, "rate_c_per_hour": learned,
                             "passive_drift_c_per_hour": drift, "required_minutes": minutes}
    lead = min(maximum, max(base, ceil(required))) if ready and base > 0 else base
    duration = min(lead, max(0, available_minutes)) if available_minutes is not None else lead
    for prediction in predictions.values():
        if deadline_minutes is not None and prediction["temperature"] is not None:
            prediction["projected_start_temperature"] = prediction["temperature"] + (
                prediction["passive_drift_c_per_hour"] or 0
            ) * max(deadline_minutes - lead, 0) / 60
        value = prediction["projected_start_temperature"]
        learned, target = prediction["rate_c_per_hour"], prediction["target"]
        finish = None
        if ready and value is not None and learned is not None and target is not None:
            finish = (min(target, value + learned * duration / 60) if mode == "heat"
                      else max(target, value - learned * duration / 60))
        prediction["predicted_finishing_temperature"] = finish
        prediction["shortfall_c"] = abs(target - finish) if target is not None and finish is not None else None
    return {"effective_lead_minutes": lead, "base_lead_minutes": base, "maximum_lead_minutes": maximum,
            "rates_ready": ready, "required_minutes": required if ready else None,
            "preparation_basis": "learned_rates" if ready else "configured_fallback",
            "finishing_temperature_known": ready,
            "available_run_minutes": duration,
            "partial_preconditioning": bool(ready and required > duration), "zones": predictions}


def bounded_lead_slots(details: dict[str, Any], options: Mapping[str, Any]) -> int:
    """Round the search grid without allowing a slot beyond the hard maximum."""
    interval = float(options[CONF_PLANNING_INTERVAL_MINUTES])
    return max(0, min(ceil(float(details["effective_lead_minutes"]) / interval),
                      floor(float(details["maximum_lead_minutes"]) / interval)))


def summary(model: dict[str, Any], now: Any) -> dict[str, Any]:
    """Bounded operator evidence without raw rolling samples."""
    loads = _load_windows(model, now)
    return {"model_version": model.get("model_version"), "learning_enabled": model.get("learning_enabled", True),
            "electrical_load_model_ready": len(loads) >= MIN_WINDOWS,
            "electrical_load_kw": median(row["power"] for row in loads) if loads else None,
            "sources": {zone: {
                **{key: item.get(key) for key in (
                    "sensor", "temperature", "reported_at", "input_status", "rejected", "last_rejection",
                )},
                "modes": {mode: {"ready": rate(model, zone, mode, now) is not None,
                                 **{key: bucket.get(key) for key in ("rate_c_per_hour", "count", "minutes", "last_at")}}
                          for mode in ("heat", "cool", "passive")
                          for bucket in [_bucket_summary(item.get("buckets", {}).get(mode, {}), now)]},
            } for zone, item in model.get("sources", {}).items()}}
