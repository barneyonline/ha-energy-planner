"""Deterministic cost/comfort candidate search over a paired forecast horizon."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from hashlib import sha256
from math import ceil, floor
from typing import Any

from .climate_economics import site_cost
from .climate_inputs import finite, instant
from .climate_learning import (
    electrical_power,
    humidity_rate,
    neighbours,
    normal_history_for_slot,
    normal_temperature,
    temperature_step,
)
from .climate_models import RECOVERY_TOLERANCE_C, TERMINAL_BATTERY_TOLERANCE_KWH, ClimateCandidate, ClimateTrajectory
from .const import CONF_HVAC_MIN_SAVING, CONF_HVAC_PRECONDITION_LEAD_MINUTES, CONF_PLANNING_INTERVAL_MINUTES
from .models import DecisionContext, DecisionSlot, OccupancyState

# Bound the generated comparison set before evaluating it. Every generated
# candidate is checked; insufficient budget for mandatory coverage fails closed.
MAX_SEARCH_SLOT_EVALUATIONS = 500_000


def simulate(
    context: DecisionContext,
    model: dict[str, Any],
    options: dict[str, Any],
    *,
    mode: str,
    start: int = -1,
    stop: int = -1,
    release: int = -1,
    target: float = 0,
    conservative: bool = False,
    demand_direction: int = 1,
    cache: dict[Any, Any] | None = None,
) -> ClimateTrajectory | None:
    """Follow learned normal operation outside a single bounded intervention."""
    temperature = context.current_hvac_temperature_c
    low, high = context.occupied_temperature_low_c, context.occupied_temperature_high_c
    if temperature is None or low is None or high is None:
        return None
    physical = model.get("physical", {})
    fit = physical.get(mode)
    if not fit:
        return None
    residual = float(model.get("validation", {}).get(mode, {}).get("temperature_p90", 0))
    temperatures: list[float] = []
    lower_temperatures: list[float] = []
    upper_temperatures: list[float] = []
    powers: list[float] = []
    normal_targets: list[float | None] = []
    humidity = finite(context.climate_inputs.get("humidity"))
    humidities: list[float | None] = []
    room_temperatures = {
        key: finite(value.get("temperature")) for key, value in context.climate_inputs.get("zones", {}).items()
    }
    room_results: dict[str, list[float]] = {key: [] for key in room_temperatures}
    room_lower: dict[str, list[float]] = {key: [] for key in room_temperatures}
    room_upper: dict[str, list[float]] = {key: [] for key in room_temperatures}
    room_humidities = {
        key: finite(value.get("humidity")) for key, value in context.climate_inputs.get("zones", {}).items()
    }
    cache = {} if cache is None else cache
    device_targets = supported_targets(context)
    for index, slot in enumerate(context.slots):
        hours = (slot_end(context, index, options) - slot.valid_at).total_seconds() / 3600
        if slot.outdoor_temperature_forecast_c is None:
            return None
        row = {
            "at": slot.valid_at.isoformat(),
            "temperature": temperature,
            "outdoor": slot.outdoor_temperature_forecast_c,
            "occupied": str(context.occupancy_state),
            "low": low,
            "mode": mode,
            "zones": context.climate_inputs.get("zones", {}),
        }
        arrival = instant(context.climate_inputs.get("arrival"))
        if arrival and slot.valid_at >= arrival:
            row["occupied"] = "occupied"
        normal_mode = mode
        query = {**row, "mode": normal_mode, "temperature": normal_temperature(temperature)}
        pool_key = ("normal_pool", row["at"], row["occupied"], normal_mode)
        if pool_key not in cache:
            cache[pool_key] = normal_history_for_slot(model.get("normal", []), query, context.local_timezone)
        cache_key = (row["at"], query["temperature"], row["occupied"], normal_mode)
        if cache_key not in cache:
            cache[cache_key] = neighbours(cache[pool_key], query, context.local_timezone)
        prediction = cache[cache_key]
        if prediction is None:
            return None
        normal_targets.append(prediction.target)
        power = prediction.power_kw
        if conservative:
            error = float(model.get("validation", {}).get(mode, {}).get("energy_error", 0))
            power *= max(0.0, 1 + demand_direction * error)
        in_preconditioning = start <= index < stop and start >= 0
        in_coast = stop <= index < release and start >= 0
        if (in_preconditioning or in_coast) and not device_targets:
            return None
        thermostat_target = (
            target
            if in_preconditioning
            else (device_targets[0] if mode == "heat" else device_targets[-1])
            if device_targets
            else target
        )
        needs_active = temperature < thermostat_target if mode == "heat" else temperature > thermostat_target
        if in_preconditioning or (in_coast and needs_active):
            demand_key = ("demand", mode, row["outdoor"])
            if demand_key not in cache:
                cache[demand_key] = electrical_power(
                    model.get("normal", []),
                    mode,
                    float(row["outdoor"]),
                    context.climate_inputs.get("cop_table", {}).get(mode, []),
                )
            active_power = cache[demand_key]
            if active_power is None:
                return None
            power = active_power if needs_active else 0.0
            if conservative:
                power *= max(
                    0.0, 1 + demand_direction * float(model.get("validation", {}).get(mode, {}).get("energy_error", 0))
                )
        elif in_coast:
            power = 0.0
        row["mode"] = mode if start <= index < release and start >= 0 else prediction.mode
        row["power_kw"] = power
        if start <= index < release and start >= 0:
            row["zones"] = {
                entity: {
                    **zone,
                    "enabled": True if entity in context.climate_zone_entities else zone.get("enabled", False),
                }
                for entity, zone in row["zones"].items()
            }
        irradiance = context.climate_inputs.get("irradiance_forecast", [])
        row["irradiance"] = irradiance_at(irradiance, slot.valid_at)
        if fit.get("enhanced") and row["irradiance"] is None:
            return None
        new_temperature = temperature_step(physical, row, hours)
        if new_temperature is None:
            return None
        temperature = new_temperature
        maximum_humidity = context.climate_inputs.get("maximum_humidity")
        humidity_fit = model.get("humidity", {})
        if maximum_humidity is not None:
            if humidity is None or not humidity_fit.get("ready") or humidity > maximum_humidity:
                return None
            rate = humidity_rate(
                humidity_fit, float(row["temperature"]), power, humidity, context.climate_inputs.get("outdoor_humidity")
            )
            if rate is None:
                return None
            humidity += rate * hours
            if conservative:
                humidity += float(humidity_fit["residual"]) * hours
            if not 0 <= humidity <= maximum_humidity:
                return None
        humidities.append(humidity)
        for entity, room_temperature in room_temperatures.items():
            if room_temperature is None:
                return None
            room = context.climate_inputs["zones"][entity]
            room_model = model.get("rooms", {}).get(entity, {})
            predicted_room = temperature_step(
                room_model.get("physical", {}), {**row, "temperature": room_temperature}, hours
            )
            if predicted_room is None:
                return None
            room_temperatures[entity] = predicted_room
            room_results[entity].append(predicted_room)
            room_residual = float(room_model.get("validation", {}).get(mode, {}).get("temperature_p90", residual))
            room_lower[entity].append(predicted_room - (room_residual if conservative else 0))
            room_upper[entity].append(predicted_room + (room_residual if conservative else 0))
            if room.get("maximum_humidity") is not None:
                # A room ceiling requires its own humidity model and current observation.
                rh = room_humidities[entity]
                rh_fit = room_model.get("humidity", {})
                if rh is None or not rh_fit.get("ready") or rh > room["maximum_humidity"]:
                    return None
                rate = humidity_rate(
                    rh_fit, room_temperature, power, rh, context.climate_inputs.get("outdoor_humidity")
                )
                if rate is None:
                    return None
                rh += rate * hours
                if conservative:
                    rh += float(rh_fit["residual"]) * hours
                if not 0 <= rh <= room["maximum_humidity"]:
                    return None
                room_humidities[entity] = rh
        lower_temperatures.append(temperature - (residual if conservative else 0))
        upper_temperatures.append(temperature + (residual if conservative else 0))
        temperatures.append(temperature + (residual if mode == "cool" else -residual) if conservative else temperature)
        powers.append(power)
    return ClimateTrajectory(
        tuple(temperatures),
        tuple(powers),
        tuple(humidities),
        {key: tuple(values) for key, values in room_results.items()},
        tuple(lower_temperatures),
        tuple(upper_temperatures),
        {key: tuple(values) for key, values in room_lower.items()},
        {key: tuple(values) for key, values in room_upper.items()},
        tuple(normal_targets),
    )


def comfort_valid(
    context: DecisionContext, candidate: ClimateTrajectory, baseline: ClimateTrajectory, options: dict[str, Any]
) -> bool:
    """Compare each occupied trajectory to its own normal recovery, including arrival."""
    if not _temperature_comfort_valid(context, candidate, baseline, options):
        return False
    for entity, room in context.climate_inputs.get("zones", {}).items():
        if entity not in candidate.zones or entity not in baseline.zones:
            return False
        presence = room.get("occupied")
        room_context = replace(
            context,
            current_hvac_temperature_c=room.get("temperature"),
            occupied_temperature_low_c=room.get("low")
            if room.get("low") is not None
            else context.occupied_temperature_low_c,
            occupied_temperature_high_c=room.get("high")
            if room.get("high") is not None
            else context.occupied_temperature_high_c,
            occupancy_state=context.occupancy_state
            if presence is None
            else OccupancyState.OCCUPIED
            if presence
            else OccupancyState.AWAY,
            climate_inputs={
                **context.climate_inputs,
                "arrival": context.climate_inputs.get("arrival") if presence is None else None,
            },
        )
        room_candidate = ClimateTrajectory(
            candidate.zones[entity],
            candidate.powers_kw,
            temperature_lower=candidate.zone_lower.get(entity, ()),
            temperature_upper=candidate.zone_upper.get(entity, ()),
        )
        room_baseline = ClimateTrajectory(baseline.zones[entity], baseline.powers_kw)
        if not _temperature_comfort_valid(room_context, room_candidate, room_baseline, options):
            return False
    return True


def recovery_valid(candidate: ClimateTrajectory, baseline: ClimateTrajectory) -> bool:
    """No room may leave unpaid thermal recovery outside the comparison horizon."""
    return abs(candidate.temperatures[-1] - baseline.temperatures[-1]) <= RECOVERY_TOLERANCE_C and all(
        entity in baseline.zones and abs(values[-1] - baseline.zones[entity][-1]) <= RECOVERY_TOLERANCE_C
        for entity, values in candidate.zones.items()
    )


def slot_end(context: DecisionContext, index: int, options: dict[str, Any]) -> Any:
    """Use actual boundaries when revalidation splits a planning slot."""
    return (
        context.slots[index + 1].valid_at
        if index + 1 < len(context.slots)
        else context.slots[index].valid_at + timedelta(minutes=float(options[CONF_PLANNING_INTERVAL_MINUTES]))
    )


def _temperature_comfort_valid(
    context: DecisionContext, candidate: ClimateTrajectory, baseline: ClimateTrajectory, options: dict[str, Any]
) -> bool:
    """Require comfort after entry, and no delayed recovery when starting outside."""
    low, high = context.occupied_temperature_low_c, context.occupied_temperature_high_c
    if low is None or high is None:
        return False
    arrival = instant(context.climate_inputs.get("arrival"))
    inside = low <= float(context.current_hvac_temperature_c or 0) <= high
    baseline_recovered = inside
    for index, (temperature, reference) in enumerate(zip(candidate.temperatures, baseline.temperatures, strict=True)):
        end = slot_end(context, index, options)
        occupied = str(context.occupancy_state) == "occupied" or (arrival is not None and end >= arrival)
        if not occupied:
            continue
        baseline_recovered |= low <= reference <= high
        inside |= low <= temperature <= high
        lower = candidate.temperature_lower[index] if candidate.temperature_lower else temperature
        upper = candidate.temperature_upper[index] if candidate.temperature_upper else temperature
        if (
            arrival is not None
            and str(context.occupancy_state) != "occupied"
            and context.slots[index].valid_at < arrival < end
        ):
            fraction = (arrival - context.slots[index].valid_at) / (end - context.slots[index].valid_at)
            previous_low = (
                candidate.temperature_lower[index - 1]
                if index and candidate.temperature_lower
                else candidate.temperatures[index - 1]
                if index
                else float(context.current_hvac_temperature_c or 0)
            )
            previous_high = (
                candidate.temperature_upper[index - 1]
                if index and candidate.temperature_upper
                else candidate.temperatures[index - 1]
                if index
                else float(context.current_hvac_temperature_c or 0)
            )
            if not (
                low <= previous_low + fraction * (lower - previous_low)
                and previous_high + fraction * (upper - previous_high) <= high
            ):
                return False
        if (inside or baseline_recovered or (arrival is not None and end >= arrival)) and not (
            low <= lower and upper <= high
        ):
            return False
        if not inside and abs(temperature - min(max(temperature, low), high)) > abs(
            reference - min(max(reference, low), high)
        ):
            return False
    return True


def optimise(
    context: DecisionContext, options: dict[str, Any], model: dict[str, Any]
) -> tuple[ClimateCandidate | None, dict[str, Any]]:
    """Compare all supported schedules; normal operation always remains an option."""
    decision: dict[str, Any] = {
        "currency": context.climate_inputs.get("currency"),
        "horizon_slots": len(context.slots),
        "rejected": {},
        "estimated": True,
    }
    rejected: dict[str, int] = decision["rejected"]

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    if not context.climate_inputs.get("load_excludes_hvac"):
        reject("household_load_hvac_provenance_missing")
        return None, decision
    if context.climate_inputs.get("maximum_humidity") is not None or context.climate_inputs.get("zones"):
        # Never grant authority to a whole-home model over unvalidated room constraints.
        if any(entity not in model.get("rooms", {}) for entity in context.climate_inputs.get("zones", {})):
            reject("zone_or_humidity_model_not_ready")
            return None, decision
    low, high = context.occupied_temperature_low_c, context.occupied_temperature_high_c
    if low is None or high is None or not low < high:
        reject("comfort_inputs_unavailable")
        return None, decision
    lead = ceil(float(options[CONF_HVAC_PRECONDITION_LEAD_MINUTES]) / float(options[CONF_PLANNING_INTERVAL_MINUTES]))
    best: ClimateCandidate | None = None
    best_key: tuple[float, float, int, int] | None = None
    cache: dict[Any, Any] = {}
    ready_modes = sum(
        not model.get("validation", {}).get(mode, {}).get("blockers", ["model_unavailable"])
        for mode in ("heat", "cool")
    )
    candidate_limit = MAX_SEARCH_SLOT_EVALUATIONS // max(3 * len(context.slots) * ready_modes, 1)
    for mode in ("heat", "cool"):
        evidence = model.get("validation", {}).get(mode, {})
        if evidence.get("blockers", ["model_unavailable"]):
            reject(f"{mode}_model_not_ready")
            continue
        baseline = simulate(context, model, options, mode=mode, cache=cache)
        if baseline is None:
            reject("baseline_unsupported")
            continue
        base_cost = site_cost(context, baseline.powers_kw, options)
        conservative_base = site_cost(
            context, lower_baseline_power(model, mode, baseline.powers_kw), options, conservative=True
        )
        if base_cost is None or conservative_base is None:
            reject("site_energy_evidence_missing")
            continue
        reference = finite(context.climate_inputs.get("target"))
        if reference is None:
            reject("normal_target_missing")
            continue
        targets = supported_targets(context)
        if not targets:
            reject("device_temperature_range_unsupported")
            continue
        schedules = candidate_schedules(len(context.slots), lead, targets, candidate_limit)
        if schedules is None:
            reject("search_work_limit")
            return None, decision
        decision["search"] = "bounded_deterministic_candidates"
        for start, stop, release, target in schedules:
            normal_target = baseline.normal_targets[start] if baseline.normal_targets else reference
            if normal_target is None:
                reject("normal_target_missing")
                continue
            if target < normal_target if mode == "heat" else target > normal_target:
                continue
            trajectory = simulate(
                context,
                model,
                options,
                mode=mode,
                start=start,
                stop=stop,
                release=release,
                target=target,
                cache=cache,
            )
            conservative = simulate(
                context,
                model,
                options,
                mode=mode,
                start=start,
                stop=stop,
                release=release,
                target=target,
                cache=cache,
                conservative=True,
            )
            lower_demand = simulate(
                context,
                model,
                options,
                mode=mode,
                start=start,
                stop=stop,
                release=release,
                target=target,
                conservative=True,
                demand_direction=-1,
                cache=cache,
            )
            if trajectory is None or conservative is None or lower_demand is None:
                reject("thermal_support_missing")
                continue
            if (
                not comfort_valid(context, trajectory, baseline, options)
                or not comfort_valid(context, conservative, baseline, options)
                or not comfort_valid(context, lower_demand, baseline, options)
            ):
                reject("comfort_limit")
                continue
            if not recovery_valid(trajectory, baseline):
                reject("recovery_outside_horizon")
                continue
            cost = site_cost(context, trajectory.powers_kw, options)
            bounds = conservative_comparison(context, model, mode, baseline, conservative, lower_demand, options)
            if cost is None or bounds is None:
                reject("site_energy_evidence_missing")
                continue
            if (
                cost.terminal_battery_kwh + TERMINAL_BATTERY_TOLERANCE_KWH < base_cost.terminal_battery_kwh
                or not bounds[1]
            ):
                reject("terminal_battery_deficit")
                continue
            saving = base_cost.cost - cost.cost
            lower_saving = min(saving, bounds[0])
            if saving < float(options.get(CONF_HVAC_MIN_SAVING, 0.25)) or lower_saving <= 0:
                reject("insufficient_saving")
                continue
            candidate = ClimateCandidate(
                start,
                stop,
                release,
                mode,
                target,
                trajectory,
                saving,
                lower_saving,
                base_cost,
                cost,
                baseline,
            )
            key = (cost.cost, abs(target - normal_target), stop - start, -start)
            if best_key is None or key < best_key:
                best = candidate
                best_key = key
    if best is not None:
        decision.update(candidate_summary(context, best))
    return best, decision


def candidate_summary(context: DecisionContext, candidate: ClimateCandidate) -> dict[str, Any]:
    """Compact currency-valued cycle evidence, not per-action duplicated benefits."""
    start = context.slots[candidate.start].valid_at.isoformat()
    stop = context.slots[candidate.stop].valid_at.isoformat()
    release = context.slots[candidate.release].valid_at.isoformat()
    identity = str(context.climate_inputs.get("identity", ""))
    lifecycle = sha256(f"{identity}:{start}:{stop}:{release}:{candidate.mode}:{candidate.target}".encode()).hexdigest()[
        :24
    ]
    return {
        "lifecycle_id": lifecycle,
        "start": start,
        "stop": stop,
        "release": release,
        "mode": candidate.mode,
        "target": candidate.target,
        "expected_saving": candidate.expected_saving,
        "conservative_saving": candidate.conservative_saving,
        "baseline": asdict(candidate.baseline_cost),
        "candidate": asdict(candidate.candidate_cost),
        "baseline_temperatures": list(candidate.baseline_trajectory.temperatures)
        if candidate.baseline_trajectory
        else [],
        "baseline_powers_kw": list(candidate.baseline_trajectory.powers_kw) if candidate.baseline_trajectory else [],
        "temperatures": list(candidate.trajectory.temperatures),
        "powers_kw": list(candidate.trajectory.powers_kw),
        "identity": identity,
        "arrival": context.climate_inputs.get("arrival"),
    }


def revalidate_schedule(
    context: DecisionContext, options: dict[str, Any], model: dict[str, Any], scheduled: dict[str, Any]
) -> ClimateCandidate | None:
    """Reprice the exact remaining incumbent, excluding sunk energy costs."""
    start, stop, end = (instant(scheduled.get(key)) for key in ("start", "stop", "release"))
    if start is None or stop is None or end is None or not context.slots or end <= context.created_at:
        return None
    if scheduled.get("identity") != context.climate_inputs.get("identity"):
        return None
    dates = [slot.valid_at for slot in context.slots]
    start_index = next((i for i, at in enumerate(dates) if at >= start), len(dates))
    stop_index = next((i for i, at in enumerate(dates) if at >= stop), len(dates))
    end_index = next((i for i, at in enumerate(dates) if at >= end), len(dates))
    if end_index >= len(dates):
        return None
    if (
        not start < stop < end
        or scheduled.get("arrival") != context.climate_inputs.get("arrival")
        or scheduled.get("target") not in supported_targets(context)
    ):
        return None
    original = context
    expanded: list[DecisionSlot] = []
    for index, slot in enumerate(original.slots):
        points = sorted(
            {
                slot.valid_at,
                *(at for at in (start, stop, end) if slot.valid_at < at < slot_end(original, index, options)),
            }
        )
        expanded.extend(replace(slot, valid_at=at) for at in points)
    context = replace(original, slots=expanded)
    split_dates = [slot.valid_at for slot in expanded]
    split_start, split_stop, split_end = (
        next(i for i, at in enumerate(split_dates) if at >= boundary) for boundary in (start, stop, end)
    )
    mode = scheduled["mode"]
    cache: dict[Any, Any] = {}
    baseline = simulate(context, model, options, mode=mode, cache=cache)
    candidate = simulate(
        context,
        model,
        options,
        mode=mode,
        start=split_start,
        stop=split_stop,
        release=split_end,
        target=scheduled["target"],
        cache=cache,
    )
    conservative = simulate(
        context,
        model,
        options,
        mode=mode,
        start=split_start,
        stop=split_stop,
        release=split_end,
        target=scheduled["target"],
        cache=cache,
        conservative=True,
    )
    lower_demand = simulate(
        context,
        model,
        options,
        mode=mode,
        start=split_start,
        stop=split_stop,
        release=split_end,
        target=scheduled["target"],
        cache=cache,
        conservative=True,
        demand_direction=-1,
    )
    if (
        baseline is None
        or candidate is None
        or lower_demand is None
        or conservative is None
        or not comfort_valid(context, candidate, baseline, options)
        or not comfort_valid(context, conservative, baseline, options)
        or not comfort_valid(context, lower_demand, baseline, options)
    ):
        return None
    if not recovery_valid(candidate, baseline):
        return None
    base_cost, cost = site_cost(context, baseline.powers_kw, options), site_cost(context, candidate.powers_kw, options)
    bounds = conservative_comparison(context, model, mode, baseline, conservative, lower_demand, options)
    if base_cost is None or cost is None or bounds is None:
        return None
    saving = base_cost.cost - cost.cost
    lower = min(saving, bounds[0])
    if (
        lower <= 0
        or (
            not context.hvac_control.get("economic_policy_version")
            and saving < float(options.get(CONF_HVAC_MIN_SAVING, 0.25))
        )
        or cost.terminal_battery_kwh + TERMINAL_BATTERY_TOLERANCE_KWH < base_cost.terminal_battery_kwh
        or not bounds[1]
    ):
        return None
    return ClimateCandidate(
        start_index,
        stop_index,
        end_index,
        mode,
        scheduled["target"],
        collapse_trajectory(context, original, candidate, options),
        saving,
        lower,
        base_cost,
        cost,
        collapse_trajectory(context, original, baseline, options),
    )


def lower_baseline_power(model: dict[str, Any], mode: str, powers: tuple[float, ...]) -> tuple[float, ...]:
    """Savings must survive less normal consumption, not only more intervention energy."""
    error = float(model.get("validation", {}).get(mode, {}).get("energy_error", 1.0))
    return tuple(power * max(0.0, 1.0 - error) for power in powers)


def irradiance_at(points: list[dict[str, Any]], at: Any) -> float | None:
    """Interpolate timestamped exposure only between covered forecast points."""
    dated = sorted((time, point["value"]) for point in points if (time := instant(point.get("at"))) is not None)
    for time, value in dated:
        if time == at:
            return float(value)
    for (left, before), (right, after) in zip(dated, dated[1:], strict=False):
        if left < at < right:
            return float(before + (after - before) * ((at - left) / (right - left)))
    return None


def collapse_trajectory(
    split: DecisionContext, original: DecisionContext, trajectory: ClimateTrajectory, options: dict[str, Any]
) -> ClimateTrajectory:
    """Publish aligned end states and average power without losing integrated energy."""
    ends = []
    powers = []
    for index, slot in enumerate(original.slots):
        end = slot_end(original, index, options)
        parts = [i for i, part in enumerate(split.slots) if slot.valid_at <= part.valid_at < end]
        ends.append(parts[-1])
        powers.append(
            sum(
                trajectory.powers_kw[i] * (slot_end(split, i, options) - split.slots[i].valid_at).total_seconds()
                for i in parts
            )
            / (end - slot.valid_at).total_seconds()
        )
    return ClimateTrajectory(
        tuple(trajectory.temperatures[i] for i in ends),
        tuple(powers),
        tuple(trajectory.humidities[i] for i in ends) if trajectory.humidities else (),
        {entity: tuple(values[i] for i in ends) for entity, values in trajectory.zones.items()},
        tuple(trajectory.temperature_lower[i] for i in ends) if trajectory.temperature_lower else (),
        tuple(trajectory.temperature_upper[i] for i in ends) if trajectory.temperature_upper else (),
        {entity: tuple(values[i] for i in ends) for entity, values in trajectory.zone_lower.items()},
        {entity: tuple(values[i] for i in ends) for entity, values in trajectory.zone_upper.items()},
        tuple(trajectory.normal_targets[i] for i in ends) if trajectory.normal_targets else (),
    )


def conservative_comparison(
    context: DecisionContext,
    model: dict[str, Any],
    mode: str,
    baseline: ClimateTrajectory,
    high: ClimateTrajectory,
    low: ClimateTrajectory,
    options: dict[str, Any],
) -> tuple[float, bool] | None:
    """Bracket both energy directions: more demand is not worse at negative prices."""
    error = float(model.get("validation", {}).get(mode, {}).get("energy_error", 1.0))
    bases = [
        site_cost(context, powers, options, conservative=True)
        for powers in (
            lower_baseline_power(model, mode, baseline.powers_kw),
            tuple(power * (1 + error) for power in baseline.powers_kw),
        )
    ]
    candidates = [site_cost(context, trajectory.powers_kw, options, conservative=True) for trajectory in (high, low)]
    if any(cost is None for cost in [*bases, *candidates]):
        return None
    valid_bases = [cost for cost in bases if cost is not None]
    valid_candidates = [cost for cost in candidates if cost is not None]
    return (
        min(cost.cost for cost in valid_bases) - max(cost.cost for cost in valid_candidates),
        min(cost.terminal_battery_kwh for cost in valid_candidates) + TERMINAL_BATTERY_TOLERANCE_KWH
        >= max(cost.terminal_battery_kwh for cost in valid_bases),
    )


def candidate_schedules(
    count: int, lead: int, targets: list[float], limit: int
) -> list[tuple[int, int, int, float]] | None:
    """Cover each near-term start/target, evenly sampling duration/coast alternatives."""
    starts = range(max(min(lead, count - 2), 0))
    groups = len(starts) * len(targets)
    if not groups:
        return []
    per_group = limit // groups
    if per_group < 1:
        return None
    schedules: list[tuple[int, int, int, float]] = []
    for start in starts:
        pairs = [
            (stop, release)
            for stop in range(start + 1, min(start + lead, count - 2) + 1)
            for release in range(stop + 1, count)
        ]
        selected = min(per_group, len(pairs))
        indices = sorted({round(index * (len(pairs) - 1) / max(selected - 1, 1)) for index in range(selected)})
        for target in targets:
            schedules.extend((start, pairs[index][0], pairs[index][1], target) for index in indices)
    return schedules


def supported_targets(context: DecisionContext) -> list[float]:
    """Intersect hard comfort limits with the device's actual temperature lattice."""
    low, high = context.occupied_temperature_low_c, context.occupied_temperature_high_c
    if low is None or high is None:
        return []
    minimum = finite(context.climate_inputs.get("minimum_temperature"))
    maximum = finite(context.climate_inputs.get("maximum_temperature"))
    anchor = minimum if minimum is not None else 0.0
    low = max(low, minimum) if minimum is not None else low
    high = min(high, maximum) if maximum is not None else high
    step = max(finite(context.climate_inputs.get("temperature_step")) or 0.5, 0.1)
    return [
        round(anchor + index * step, 6)
        for index in range(ceil((low - anchor) / step), floor((high - anchor) / step) + 1)
    ]
