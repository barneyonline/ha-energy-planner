"""Bounded deterministic EV search with one physical and economic simulator.

No Home Assistant I/O, speculative battery dispatch, or shared-EV scheduling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import sqrt
from statistics import median
from typing import Any, TypedDict

from .ev import EVChargeAllocation, EVChargeSchedule, _charge_cost_components
from .ev_policy import finite, strategy
from .ev_runtime import timestamp
from .models import DecisionContext

MAX_EVALUATIONS = 2000


class EVDecisionEvidence(TypedDict):
    """Version-independent factual evidence shared by actions and presentation."""

    strategy: str
    search_status: str
    candidate_evaluations: int
    search_limit: int
    action_limit: int
    remaining_actions: int
    capacity_excluded_slots: int
    forecast_complete_to_ready_by: bool
    expected_completion: str | None
    conservative_completion: str | None
    latest_validated_start: str | None
    readiness_margin_minutes: float | None
    readiness_buffer_minutes: float
    projected_departure_soc: float
    maximum_recoverable_soc: float
    delivery_status: str
    charging_model_source: str
    charging_model_observed_minutes: float
    modelled_incremental_cost: float
    estimated_grid_carbon_g: float
    battery_cost_reason: str
    terminal_energy_value: float
    emergency_policy: str
    emergency_spend_used: float
    emergency_budget_remaining: float
    planned_emergency_extra: float
    schedule_change_reason: str
    retained_schedule_saving: float | None
    planned_transitions: int
    readiness_dwell_exceptions: int
    physical_power_by_time: dict[str, float]
    allocation_intervals: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ChargeSlot:
    index: int
    start: datetime
    end: datetime
    power_kw: float
    import_price: float
    normal_price: bool
    daylight: bool

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(slots=True)
class Simulation:
    schedule: EVChargeSchedule
    powers: dict[int, float]
    cost: float
    carbon: float
    extra: float
    completion: datetime | None
    expected_completion: datetime | None
    transitions: int
    terminal_value: float
    battery_reason: str
    daylight_energy: float
    dwell_violations: int


def _rate(context: DecisionContext, soc: float, fallback: float, *, expected: bool = False) -> float:
    model = context.ev_evidence.get("performance", {})
    key = "0" if soc < 60 else "60" if soc < 80 else "80" if soc < 90 else "90"
    band = model.get("bands", {}).get(key, {})
    aggregate = model.get("aggregate", {})
    row = band if band.get("sessions", 0) >= 3 and band.get("minutes", 0) >= 60 else aggregate
    rate = finite(row.get("soc_per_kwh")) if row.get("minutes", 0) >= 60 else None
    if rate is None or rate <= 0:
        return fallback
    if row is aggregate:
        rate = min(rate, fallback / 0.9)  # Do not extrapolate favourable efficiency into an unobserved band.
    return rate if expected else rate * 0.9


def _deliver(context: DecisionContext, soc: float, energy: float, fallback: float, *, expected: bool = False) -> float:
    """Integrate across SOC boundaries instead of extrapolating one band."""
    remaining = energy
    for boundary in (60.0, 80.0, 90.0, 100.0):
        if soc >= boundary:
            continue
        rate = _rate(context, soc, fallback, expected=expected)
        used = min(remaining, (boundary - soc) / rate)
        soc += used * rate
        remaining -= used
        if remaining <= 1e-9:
            break
    return min(soc, 100.0)


def _charge_interval(
    context: DecisionContext,
    soc: float,
    target: float,
    power: float,
    hours: float,
    fallback: float,
    *,
    expected: bool = False,
) -> tuple[float, float, float]:
    """Integrate delivered power and efficiency through learned SOC bands."""
    energy = duration = 0.0
    model = context.ev_evidence.get("performance", {})
    for boundary, key in ((60.0, "0"), (80.0, "60"), (90.0, "80"), (100.0, "90")):
        endpoint = min(boundary, target)
        if endpoint <= soc:
            continue
        row = model.get("bands", {}).get(key, {})
        if row.get("sessions", 0) < 3 or row.get("minutes", 0) < 60:
            row = model.get("aggregate", {})
        fraction = finite(row.get("delivery_fraction")) if row.get("minutes", 0) >= 60 else None
        delivered_power = power * min(max(fraction if fraction is not None else 1, 0), 1)
        if delivered_power <= 0:
            break
        rate = _rate(context, soc, fallback, expected=expected)
        segment = min(max(hours - duration, 0), (endpoint - soc) / rate / delivered_power)
        used = segment * delivered_power
        soc += used * rate
        energy += used
        duration += segment
        if duration >= hours - 1e-9:
            break
    return soc, energy, duration


def _battery_cost(
    context: DecisionContext,
    options: Mapping[str, Any],
    energy: Mapping[int, float],
    interval: timedelta,
    scenario: str | None = None,
) -> tuple[float, float, str]:
    """Simulate observed self-consumption/backup; never invent AI dispatch."""
    capacity = finite(options.get("battery_usable_capacity_kwh")) or 0.0
    charge_limit = finite(options.get("battery_max_charge_kw")) or 0.0
    discharge_limit = finite(options.get("battery_max_discharge_kw")) or 0.0
    soc = finite(context.current_battery_soc_percent)
    observed = context.current_enphase_profile
    self_consumption = bool(
        scenario == "self_consumption" or observed and observed == context.enphase_self_consumption_profile
    )
    backup = bool(scenario == "backup" or observed and observed == context.enphase_full_backup_profile)
    if capacity <= 0 or soc is None or not (self_consumption or backup):
        return 0.0, 0.0, "battery_profile_or_model_unavailable"
    efficiency = sqrt(min(max(float(options.get("battery_round_trip_efficiency_percent", 90)) / 100, 0.01), 1))
    floor = capacity * max(float(options.get("battery_min_soc_percent", 0)), 0) / 100
    stored = capacity * soc / 100
    valid = [slot for slot in context.slots if slot.valid_at + interval > context.created_at]
    if not valid or any(
        any(finite(v) is None for v in (s.import_price, s.export_price, s.pv_forecast_kw, s.baseline_load_forecast_kw))
        for s in valid
    ):
        return 0.0, 0.0, "battery_forecast_incomplete"
    final_start = valid[-1].valid_at + interval - timedelta(hours=3)
    prices = [
        float(s.import_price or 0) for s in valid if s.valid_at >= final_start and float(s.import_price or 0) >= 0
    ]
    terminal_value = median(prices) * efficiency if prices else 0.0
    cost = 0.0
    for index, slot in enumerate(context.slots):
        hours = max((slot.valid_at + interval - max(slot.valid_at, context.created_at)).total_seconds() / 3600, 0)
        if hours <= 0:
            continue
        net = (
            float(slot.baseline_load_forecast_kw or 0) + slot.projected_hvac_load_kw - float(slot.pv_forecast_kw or 0)
        ) * hours + energy.get(index, 0.0)
        if net < 0:
            charged = min(-net, charge_limit * hours, max(capacity - stored, 0) / efficiency)
            stored += charged * efficiency
            net += charged
        elif self_consumption:
            discharged = min(net, discharge_limit * hours, max(stored - floor, 0) * efficiency)
            stored -= discharged / efficiency
            net -= discharged
        cost += net * float((slot.import_price if net >= 0 else slot.export_price) or 0)
    return cost - max(stored - floor, 0) * terminal_value, terminal_value, "observed_profile_simulation"


def optimise_ev(
    context: DecisionContext,
    options: Mapping[str, Any],
    *,
    target: float,
    ready_by: datetime,
    earliest_start: datetime,
    charge_rate_kw: float,
    soc_per_kwh: float,
    standard: EVChargeSchedule,
    carbon_weight: float = 0,
    force_current: bool = False,
) -> tuple[EVChargeSchedule, EVDecisionEvidence]:
    """Find a valid schedule, retaining an independently constructed fallback."""
    interval = timedelta(minutes=int(options.get("planning_interval_minutes", 5)))
    current_soc = float(context.current_ev_soc_percent or 0)
    selected_strategy = strategy(options)
    continuous = selected_strategy == "continuous"
    buffer = timedelta(minutes=max(float(options.get("ev_readiness_buffer_minutes", 30)), 0))
    buffered_deadline = ready_by - buffer
    limit_enabled = bool(options.get("ev_price_limit_enabled", False))
    normal_limit = float(options.get("ev_max_import_price", 1)) if limit_enabled else float("inf")
    emergency_limit = finite(options.get("ev_emergency_price")) or 0.0
    session_used = max(float(context.ev_evidence.get("emergency_spend", 0)), 0)
    budget = max(float(options.get("ev_emergency_budget", 0)) - session_used, 0)
    if context.ev_evidence.get("budget_uncertain"):
        budget = 0.0
    emergency = bool(
        limit_enabled
        and options.get("ev_price_policy") == "departure_priority"
        and emergency_limit > normal_limit
        and budget > 0
    )
    capability = context.ev_evidence.get("power_capability")
    power_mapping_invalid = bool(context.ev_evidence.get("power_limit_mapped") and capability is None)
    grid_limit = float(options.get("grid_import_limit_kw", 0))
    slots: list[ChargeSlot] = []
    excluded_capacity = 0
    forecast_missing = False
    covered_until = context.created_at
    for index, source in enumerate(context.slots):
        start, end = max(source.valid_at, context.created_at), min(source.valid_at + interval, ready_by)
        if end <= start:
            continue
        if start > covered_until + timedelta(seconds=1):
            forecast_missing = True
        covered_until = max(covered_until, end)
        values = [
            finite(v)
            for v in (
                source.import_price,
                source.pv_forecast_lower_kw if source.pv_forecast_lower_kw is not None else source.pv_forecast_kw,
                source.baseline_load_forecast_upper_kw
                if source.baseline_load_forecast_upper_kw is not None
                else source.baseline_load_forecast_kw,
            )
        ]
        if any(v is None for v in values):
            forecast_missing = True
            continue
        price, pv, load = values
        assert price is not None and pv is not None and load is not None
        headroom = max(grid_limit - load - max(source.projected_hvac_load_kw, 0) + pv, 0)
        power = capability.power(min(headroom, charge_rate_kw)) if capability else charge_rate_kw
        if power_mapping_invalid or power <= 0 or power > headroom + 1e-6:
            excluded_capacity += 1
            continue
        current = index == 0
        daylight_selected = any(
            a.valid_at == source.valid_at and a.allocation_source == "daylight" for a in standard.allocations
        )
        if (
            start < earliest_start
            and not daylight_selected
            and not (current and (force_current or continuous and context.ev_charging is True))
        ):
            continue
        daylight = any(w.start <= start and end <= w.end for w in context.daylight_windows)
        if price <= normal_limit or emergency and price <= emergency_limit:
            slots.append(ChargeSlot(index, start, end, power, price, price <= normal_limit, daylight))
    forecast_missing |= covered_until < ready_by
    by_index = {slot.index: slot for slot in slots}
    baseline, terminal_value, battery_reason = _battery_cost(context, options, {}, interval)
    scenarios: list[tuple[str, float]] = []
    if (
        battery_reason == "battery_profile_or_model_unavailable"
        and context.current_enphase_profile
        and (context.enphase_self_consumption_profile or context.enphase_full_backup_profile)
    ):
        for mode in ("self_consumption", "backup"):
            scenario_baseline, scenario_terminal, reason = _battery_cost(context, options, {}, interval, mode)
            if reason == "observed_profile_simulation":
                scenarios.append((mode, scenario_baseline))
                terminal_value = scenario_terminal
        if scenarios:
            battery_reason = "uncertain_profile_conservative_scenarios"

    unknown_carbon = max((finite(s.carbon_intensity_g_per_kwh) or 0 for s in context.slots), default=0)
    evaluations = 0
    results: list[Simulation] = []
    cached: dict[tuple[tuple[int, float], ...], Simulation | None] = {}
    configured_actions = int(options.get("max_daily_ev_actions", 10))
    action_limit = (
        max(int(context.ev_evidence.get("remaining_actions", configured_actions)), 0)
        if configured_actions > 0
        else 10000
    )

    action_limited_feasible = False

    def simulate(requested: Mapping[int, float]) -> Simulation | None:
        nonlocal evaluations, action_limited_feasible
        signature = tuple(sorted((i, round(p, 6)) for i, p in requested.items() if p > 0))
        if evaluations >= MAX_EVALUATIONS:
            return None
        evaluations += 1
        # Retained schedules and refinement must obey the same current-slot
        # constraints as seed generation. Safety/price exclusions remain final.
        if (signature and 0 in by_index and signature[0][0] != 0
                and (force_current or continuous and context.ev_charging is True)):
            return None
        soc, expected_soc = current_soc, current_soc
        allocations: list[EVChargeAllocation] = []
        powers: dict[int, float] = {}
        energies: dict[int, float] = {}
        cost = carbon = extra = daylight_energy = 0.0
        completion = expected_completion = context.created_at if soc >= target else None
        transitions = 0
        previous_active = context.ev_charging is True
        previous_power = capability.current * capability.kw_per_unit if capability and previous_active else None
        previous_end = context.created_at
        started = False
        dwell_violations = 0
        run_start = context.created_at
        for index, power in signature:
            item = by_index.get(index)
            if item is None or power > item.power_kw + 1e-6:
                return None
            if soc >= target - 1e-7:
                break
            if capability:
                legal = capability.power(power)
                if legal <= 0 or abs(legal - power) > 1e-6:
                    return None
            elif abs(power - charge_rate_kw) > 1e-6:
                return None
            contiguous = item.start <= previous_end + timedelta(seconds=1)
            if continuous and started and not contiguous:
                return None
            if previous_active and not contiguous:
                dwell = timedelta(minutes=float(options.get("ev_min_dwell_minutes", 15)))
                gap_eligible = all(i in by_index for i in range(max(powers, default=-1) + 1, index))
                if selected_strategy == "adaptive" and gap_eligible:
                    dwell_violations += int(previous_end - run_start < dwell)
                    dwell_violations += int(item.start - previous_end < dwell)
                transitions += 1
                previous_active = False
            if not previous_active:
                run_start = item.start
                transitions += 1
            elif capability and previous_power is not None and abs(previous_power - power) > 1e-6:
                transitions += 1
            previous_power = power
            previous_active = True
            started = True
            prediction_power = power
            if (
                index == 0
                and context.ev_charging is True
                and context.ev_evidence.get("delivery_status") in {"stalled", "unavailable"}
            ):
                prediction_power = 0.0
            after, energy, duration_hours = _charge_interval(
                context, soc, target, prediction_power, item.hours, soc_per_kwh
            )
            physical_end = item.start + timedelta(hours=duration_hours) if after >= target - 1e-6 else item.end
            expected_after, expected_energy, expected_hours = _charge_interval(
                context, expected_soc, target, prediction_power, item.hours, soc_per_kwh, expected=True
            )
            if expected_completion is None and expected_after >= target - 1e-6:
                expected_completion = item.start + timedelta(hours=expected_hours)
            # Missing delivery evidence cannot credit readiness, but a live
            # charge command can still draw its full limit throughout the slot.
            # Price, carbon, battery and emergency budgets must include that exposure.
            if prediction_power == 0:
                expected_energy, expected_hours = power * item.hours, item.hours
            source = context.slots[index]
            effective, solar, grid = _charge_cost_components(source, energy / duration_hours if duration_hours else 0)
            exposure_energy = expected_energy if prediction_power == 0 else energy
            exposure_hours = expected_hours if prediction_power == 0 else duration_hours
            _, _, exposure_grid = _charge_cost_components(
                source, exposure_energy / exposure_hours if exposure_hours else 0,
            )
            increment = exposure_grid * exposure_hours * max(item.import_price - normal_limit, 0)
            if extra + increment > budget + 1e-8:
                return None
            extra += increment
            expected_effective, _, expected_grid = _charge_cost_components(
                source, expected_energy / expected_hours if expected_hours else 0
            )
            cost += expected_energy * (expected_effective if expected_effective is not None else item.import_price)
            intensity = finite(source.carbon_intensity_g_per_kwh)
            carbon += expected_grid * expected_hours * max(intensity if intensity is not None else unknown_carbon, 0)
            if item.daylight:
                daylight_energy += energy
            allocations.append(
                EVChargeAllocation(
                    source.valid_at,
                    energy / item.hours,
                    after - soc,
                    item.import_price,
                    effective,
                    solar,
                    grid,
                    source.carbon_intensity_g_per_kwh,
                    grid * duration_hours * source.carbon_intensity_g_per_kwh
                    if source.carbon_intensity_g_per_kwh is not None
                    else None,
                    "daylight"
                    if item.daylight and options.get("ev_daylight_lowest_cost_charging_enabled")
                    else "ready_by_fallback"
                    if options.get("ev_daylight_lowest_cost_charging_enabled") and any(s.daylight for s in slots)
                    else "ready_by",
                )
            )
            powers[index] = power
            energies[index] = expected_energy
            soc, expected_soc, previous_end = after, expected_after, physical_end
            if soc >= target - 1e-6:
                completion = physical_end
        if previous_active:
            transitions += 1  # Reserve the eventual stop, including an empty stop schedule.
        if transitions > action_limit and powers:
            action_limited_feasible |= soc >= target - 1e-6
            return None
        if battery_reason == "observed_profile_simulation":
            total, _, _ = _battery_cost(context, options, energies, interval)
            cost = total - baseline
        elif scenarios:
            cost = max(
                cost,
                *(
                    _battery_cost(context, options, energies, interval, mode)[0] - scenario_baseline
                    for mode, scenario_baseline in scenarios
                ),
            )

        schedule = EVChargeSchedule(
            allocations, target, soc, max(target - current_soc, 0), soc < target - 1e-6, "bounded_ev_schedule"
        )
        return Simulation(
            schedule,
            powers,
            cost,
            carbon,
            extra,
            completion,
            expected_completion,
            transitions,
            terminal_value,
            battery_reason,
            daylight_energy,
            dwell_violations,
        )

    def add(requested: Mapping[int, float]) -> Simulation | None:
        signature = tuple(sorted((i, round(p, 6)) for i, p in requested.items() if p > 0))
        if signature in cached:
            return cached[signature]
        result = simulate(requested)
        cached[signature] = result
        if result is not None:
            results.append(result)
        return result

    def fill(ordered: list[ChargeSlot]) -> dict[int, float]:
        remaining = max(target - current_soc, 0)
        chosen: dict[int, float] = {}
        # Conservative aggregate estimate only seeds the candidate. The simulator
        # checks band transitions and additional chronological slots are explored.
        for item in ordered:
            chosen[item.index] = item.power_kw
            remaining -= item.power_kw * item.hours * min(soc_per_kwh, _rate(context, current_soc, soc_per_kwh))
            if remaining <= 0:
                break
        return chosen

    def generate(eligible: list[ChargeSlot]) -> None:
        if continuous:
            for offset, first in enumerate(eligible):
                if evaluations >= MAX_EVALUATIONS:
                    break
                if (
                    force_current or context.ev_charging is True and any(s.index == 0 for s in eligible)
                ) and first.index != 0:
                    continue
                run = [first]
                for item in eligible[offset + 1 :]:
                    if item.start > run[-1].end + timedelta(seconds=1):
                        break
                    run.append(item)
                add({item.index: item.power_kw for item in run})
        else:
            # Earliest feasible is evaluated before economic alternatives.
            add({item.index: item.power_kw for item in eligible})
            add(fill(list(reversed(eligible))))
            ranked = sorted(
                eligible, key=lambda s: (_charge_cost_components(context.slots[s.index], s.power_kw)[0], s.start)
            )
            add(fill(ranked))
            daylight_ranked = sorted(ranked, key=lambda s: not s.daylight)
            if options.get("ev_daylight_lowest_cost_charging_enabled"):
                add(fill(daylight_ranked))
            add(fill([s for s in eligible if s.end <= buffered_deadline]))
        if capability:
            solar_following = {}
            for item in eligible:
                source = context.slots[item.index]
                surplus = max(
                    float(source.pv_forecast_kw or 0)
                    - float(source.baseline_load_forecast_kw or 0)
                    - source.projected_hvac_load_kw,
                    0,
                )
                power = capability.power(min(item.power_kw, surplus))
                if power > 0:
                    solar_following[item.index] = power
            add(solar_following)

    add({})
    normal = [s for s in slots if s.normal_price]
    generate(normal)
    # A fragmented seed can exceed the action limit while a normal-price
    # contiguous window is feasible. Search those windows before authorizing
    # emergency spending, including when the buffer would favour premium slots.
    if not continuous and not any(not r.schedule.infeasible for r in results):
        for offset, first in enumerate(normal):
            run = [first]
            for item in normal[offset + 1:]:
                if item.start > run[-1].end + timedelta(seconds=1):
                    break
                run.append(item)
            add({item.index: item.power_kw for item in run})
    normal_feasible = any(not r.schedule.infeasible for r in results)
    normal_capacity_soc = current_soc
    for item in normal:
        normal_capacity_soc, _, _ = _charge_interval(
            context, normal_capacity_soc, 100, item.power_kw, item.hours, soc_per_kwh)
    normal_shortfall_proven = normal_capacity_soc < target - 1e-6 or continuous and capability is None
    if not normal_feasible and emergency and normal_shortfall_proven:
        generate(slots)
    else:
        # A bounded search failure is not authority to exceed the user's ceiling.
        by_index = {s.index: s for s in normal}
    old = context.ev_evidence.get("retained_schedule", [])
    retained = {}
    for item in old if isinstance(old, list) else []:
        for candidate in slots:
            if item.get("valid_at") == context.slots[candidate.index].valid_at.isoformat():
                power = finite(item.get("physical_power_kw")) or charge_rate_kw
                retained[candidate.index] = power
    retained_result = add(retained) if retained else None
    if retained_result is None and selected_strategy == "adaptive" and context.ev_charging is True:
        retained_result = next((r for r in results if 0 in r.powers), None)
    # Revalidate the existing allocator output; it has no authority to bypass capacity.
    add(
        {
            s.index: s.power_kw
            for s in slots
            if any(a.valid_at == context.slots[s.index].valid_at for a in standard.allocations)
        }
    )

    def rank(result: Simulation) -> tuple[Any, ...]:
        return (
            result.schedule.infeasible,
            -result.schedule.scheduled_soc_percent if result.schedule.infeasible else 0,
            result.completion is None or result.completion > buffered_deadline,
            -result.daylight_energy if options.get("ev_daylight_lowest_cost_charging_enabled") else 0,
            result.dwell_violations,
            result.cost,
            result.completion or ready_by,
        )

    if selected_strategy in {"split", "adaptive"} and results:
        seed = min(results, key=rank)
        for _ in range(4):
            before = seed
            for source_index in sorted(seed.powers):
                for destination in slots:
                    if destination.index in seed.powers or evaluations >= MAX_EVALUATIONS:
                        continue
                    if source_index not in seed.powers:
                        break
                    moved = dict(seed.powers)
                    moved.pop(source_index)
                    moved[destination.index] = destination.power_kw
                    refined = add(moved)
                    if refined is not None and rank(refined) < rank(seed):
                        seed = refined
                if capability:
                    power = seed.powers.get(source_index, 0) - capability.step * capability.kw_per_unit
                    moved = dict(seed.powers)
                    moved[source_index] = capability.power(power)
                    refined = add(moved)
                    if refined is not None and rank(refined) < rank(seed):
                        seed = refined
            indices = sorted(seed.powers)
            if len(indices) > 1:
                for destination in slots:
                    shifted = {destination.index + i - indices[0]: power for i, power in seed.powers.items()}
                    if any(i not in by_index for i in shifted):
                        continue
                    refined = add(shifted)
                    if refined is not None and rank(refined) < rank(seed):
                        seed = refined
                    if evaluations >= MAX_EVALUATIONS:
                        break
            if seed is before or evaluations >= MAX_EVALUATIONS:
                break
    best = min(results, key=rank)
    # Carbon uses the existing normalized cost/emission preference within the
    # same feasibility, buffer, and daylight priority class.
    peers = [r for r in results if rank(r)[:5] == rank(best)[:5]]
    if carbon_weight > 0 and peers:
        costs, emissions = [r.cost for r in peers], [r.carbon for r in peers]
        weight = min(max(carbon_weight, 0), 1)
        best = min(
            peers,
            key=lambda r: (
                (1 - weight) * (r.cost - min(costs)) / max(max(costs) - min(costs), 1e-9)
                + weight * (r.carbon - min(emissions)) / max(max(emissions) - min(emissions), 1e-9),
                r.cost,
            ),
        )
    retained_reason = "candidate_selected"
    proposed_saving = retained_result.cost - best.cost if retained_result else None
    if retained_result is not None and rank(retained_result)[:4] <= rank(best)[:4]:
        saving = retained_result.cost - best.cost
        threshold = max(
            float(options.get("ev_schedule_min_saving", 0.5)),
            abs(retained_result.cost) * float(options.get("ev_schedule_min_saving_percent", 5)) / 100,
        )
        if saving < threshold:
            best = retained_result
            retained_reason = "saving_below_schedule_change_threshold"
    last_transition = timestamp(context.ev_evidence.get("last_transition_at"))
    if selected_strategy == "adaptive" and last_transition is not None:
        dwell_until = last_transition + timedelta(minutes=float(options.get("ev_min_dwell_minutes", 15)))
        if context.created_at < dwell_until and (0 in best.powers) != (context.ev_charging is True):
            stable = [
                r for r in results if (0 in r.powers) == (context.ev_charging is True) and rank(r)[:3] <= rank(best)[:3]
            ]
            if stable:
                best = min(stable, key=rank)
                retained_reason = "minimum_dwell_active"
    capacity_soc = current_soc
    for item in slots:
        capacity_soc, _, _ = _charge_interval(context, capacity_soc, 100, item.power_kw, item.hours, soc_per_kwh)
    status = "valid_schedule"
    if best.schedule.infeasible:
        status = (
            "ev_daily_action_cap_reached"
            if action_limited_feasible
            else "forecast_coverage_insufficient"
            if forecast_missing
            else "capacity_or_price_shortfall"
            if capacity_soc < target - 1e-6
            else "search_no_feasible_schedule"
        )
    physical = {context.slots[i].valid_at.isoformat(): p for i, p in best.powers.items()}
    latest = max(
        (r.schedule.allocations[0].valid_at for r in results if not r.schedule.infeasible and r.schedule.allocations),
        default=None,
    )
    best.schedule.reason = (
        "already_at_target"
        if current_soc >= target
        else "continuous_charging_window_before_ready_by"
        if continuous and not best.schedule.infeasible
        else status
    )
    return best.schedule, {
        "strategy": selected_strategy,
        "search_status": status,
        "candidate_evaluations": evaluations,
        "search_limit": MAX_EVALUATIONS,
        "action_limit": configured_actions,
        "remaining_actions": action_limit,
        "capacity_excluded_slots": excluded_capacity,
        "forecast_complete_to_ready_by": not forecast_missing,
        "expected_completion": best.expected_completion.isoformat() if best.expected_completion else None,
        "conservative_completion": best.completion.isoformat() if best.completion else None,
        "latest_validated_start": latest.isoformat() if latest else None,
        "readiness_margin_minutes": (ready_by - best.completion).total_seconds() / 60 if best.completion else None,
        "readiness_buffer_minutes": buffer.total_seconds() / 60,
        "projected_departure_soc": round(best.schedule.scheduled_soc_percent, 3),
        "maximum_recoverable_soc": round(capacity_soc, 3),
        "delivery_status": context.ev_evidence.get("delivery_status", "configured_estimate"),
        "charging_model_source": "measured"
        if (context.ev_evidence.get("performance", {}).get("aggregate", {}).get("minutes", 0) >= 60
            or any(row.get("minutes", 0) >= 60 and row.get("sessions", 0) >= 3
                   for row in context.ev_evidence.get("performance", {}).get("bands", {}).values()))
        else "configured_or_recorder_calibration",
        "charging_model_observed_minutes": context.ev_evidence.get("performance", {})
        .get("aggregate", {})
        .get("minutes", 0),
        "modelled_incremental_cost": round(best.cost, 4),
        "estimated_grid_carbon_g": round(best.carbon, 3),
        "battery_cost_reason": battery_reason,
        "terminal_energy_value": terminal_value,
        "emergency_policy": options.get("ev_price_policy", "hard_ceiling"),
        "emergency_spend_used": session_used,
        "emergency_budget_remaining": budget,
        "planned_emergency_extra": round(best.extra, 6),
        "schedule_change_reason": retained_reason,
        "retained_schedule_saving": round(proposed_saving, 4) if proposed_saving is not None else None,
        "planned_transitions": best.transitions,
        "readiness_dwell_exceptions": best.dwell_violations,
        "physical_power_by_time": physical,
        "allocation_intervals": {
            allocation.valid_at.isoformat(): {
                "interval_start": by_index[index].start.isoformat(),
                "interval_end": min(by_index[index].end, best.completion or ready_by).isoformat(),
                "energy_kwh": allocation.charge_kw * by_index[index].hours,
            }
            for index, allocation in zip(best.powers, best.schedule.allocations, strict=True)
        },
    }
