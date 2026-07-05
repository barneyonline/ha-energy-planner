"""Deterministic dry-run planner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time, timedelta
from math import isfinite
from typing import Any

from .const import (
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_MIN_SAVINGS,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_SOC_PER_KWH,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
    CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA,
    CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from .ev import EVTripSummary, allocate_least_cost_charging, calculate_ev_target
from .models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    EnergyPlan,
    FlexibleLoadProjection,
    InputHealth,
    OccupancyState,
    PlanAction,
    PlannerMode,
)
from .thermal_model import thermal_hvac_load_kw, thermal_model_summary

HVAC_PRECONDITION_PROJECTED_LOAD_KW = 1.0


class DryRunPlanner:
    """Create a deterministic, non-controlling v1 plan."""

    def __init__(self, options: Mapping[str, Any], thermal_model: Mapping[str, Any] | None = None) -> None:
        """Initialize planner."""
        self.options = options
        self.thermal_model = dict(thermal_model or {})

    def create_plan(self, context: DecisionContext) -> EnergyPlan:
        """Create a dry-run plan from the current decision context."""
        mode = self._mode(context)
        confidence = self._confidence(context)
        actions = self._actions(context, mode)
        preview = self._preview(context)
        estimated_cost = self._estimate_cost(context)
        device_plans = self._device_plans(context, actions)

        summary = "Planner disabled"
        if mode == PlannerMode.DRY_RUN:
            summary = "Dry-run plan generated; no device actions will be sent"
        elif mode == PlannerMode.ACTIVE_HEALTHY:
            summary = f"Active plan generated with {len(actions)} eligible candidate action(s)"
        elif context.input_health == InputHealth.UNSAFE:
            summary = "Plan unsafe; required inputs are stale or unavailable"

        return EnergyPlan(
            plan_id=context.plan_id,
            created_at=context.created_at,
            horizon_hours=int(self.options[CONF_PLANNING_HORIZON_HOURS]),
            interval_minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]),
            status="current" if context.input_health != InputHealth.UNSAFE else "unsafe",
            health=context.input_health,
            mode=mode,
            summary=summary,
            confidence=confidence,
            estimated_daily_cost=estimated_cost,
            actions=actions,
            preview=preview,
            input_issues=context.input_issues,
            device_plans=device_plans,
        )

    def _mode(self, context: DecisionContext) -> PlannerMode:
        if context.input_health == InputHealth.UNSAFE:
            return PlannerMode.ACTIVE_DEGRADED if self.options[CONF_PLANNER_ENABLED] else PlannerMode.DISABLED
        if not self.options[CONF_PLANNER_ENABLED]:
            return PlannerMode.DISABLED
        if self.options[CONF_DRY_RUN]:
            return PlannerMode.DRY_RUN
        return PlannerMode.ACTIVE_HEALTHY

    def _preview(self, context: DecisionContext) -> list[dict[str, Any]]:
        slots = context.slots[:12]
        battery_floor = float(self.options[CONF_BATTERY_MIN_SOC_PERCENT])
        return [
            {
                "valid_at": slot.valid_at.isoformat(),
                "import_price": slot.import_price,
                "export_price": slot.export_price,
                "pv_forecast_kw": slot.pv_forecast_kw,
                "baseline_load_forecast_kw": slot.baseline_load_forecast_kw,
                "outdoor_temperature_forecast_c": slot.outdoor_temperature_forecast_c,
                "battery_floor_percent": battery_floor,
                "occupied": context.occupancy_state,
            }
            for slot in slots
        ]

    def _actions(self, context: DecisionContext, mode: PlannerMode) -> list[PlanAction]:
        """Create conservative immediate candidate actions."""
        if mode not in {PlannerMode.ACTIVE_HEALTHY, PlannerMode.DRY_RUN} or context.input_health != InputHealth.HEALTHY:
            return []
        actions: list[PlanAction] = []
        execute_not_before = context.created_at
        execute_not_after = context.created_at + timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
        if context.occupancy_state == OccupancyState.AWAY:
            actions.append(
                PlanAction(
                    action_id=f"{context.plan_id}-hvac-away-off",
                    plan_id=context.plan_id,
                    execute_not_before=execute_not_before,
                    execute_not_after=execute_not_after,
                    asset=ActionAsset.DAIKIN,
                    kind=ActionKind.SET_HVAC,
                    desired_state={"hvac_mode": "off"},
                    hard_constraints=["occupancy_away", "manual_hvac_override_inactive"],
                    reason_codes=["away_hvac_policy"],
                    expected_cost_delta=None,
                    confidence=confidence_from_context(context),
                    requires_haeo_plan_id=None,
                )
            )
        else:
            hvac_action = self._hvac_suppression_action(context, execute_not_before, execute_not_after)
            if hvac_action is None:
                hvac_action = self._hvac_preconditioning_action(context, execute_not_before, execute_not_after)
            if hvac_action is not None:
                actions.append(hvac_action)
        enphase_action = self._enphase_action(context, execute_not_before, execute_not_after)
        if enphase_action is not None:
            actions.append(enphase_action)
        ev_min = float(self.options[CONF_EV_MIN_SOC_PERCENT])
        if context.ev_connected is not False and context.current_ev_soc_percent is not None:
            ready_by = _next_ready_by(context.created_at, str(self.options[CONF_DEFAULT_READY_BY]))
            charge_rate_kw = float(self.options[CONF_EV_CHARGE_RATE_KW])
            soc_per_kwh = float(self.options[CONF_EV_SOC_PER_KWH])
            target = calculate_ev_target(
                current_soc_percent=context.current_ev_soc_percent,
                summary=EVTripSummary(
                    observed_days=context.ev_trip_observed_days,
                    max_daily_soc_percent=context.ev_trip_max_daily_soc_percent,
                    average_daily_soc_percent=context.ev_trip_average_daily_soc_percent,
                    history_sufficient=context.ev_trip_history_sufficient,
                ),
                ev_min_soc_percent=ev_min,
                ev_max_soc_percent=float(self.options[CONF_EV_MAX_SOC_PERCENT]),
                fallback_target_soc_percent=float(self.options[CONF_EV_FALLBACK_TARGET_SOC_PERCENT]),
                available_charge_hours=max((ready_by - context.created_at).total_seconds() / 3600, 0.0),
                charge_rate_percent_per_hour=charge_rate_kw * soc_per_kwh,
            )
            if target.required_charge_percent <= 0:
                return actions
            schedule = allocate_least_cost_charging(
                context.slots,
                current_soc_percent=context.current_ev_soc_percent,
                target_soc_percent=target.target_soc_percent,
                ready_by=ready_by,
                charge_rate_kw=charge_rate_kw,
                soc_per_kwh=soc_per_kwh,
                interval_minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]),
            )
            allocation_by_time = {allocation.valid_at: allocation for allocation in schedule.allocations}
            for slot in context.slots:
                if slot.valid_at in allocation_by_time:
                    slot.projected_ev_load_kw = allocation_by_time[slot.valid_at].charge_kw
            actions.append(
                PlanAction(
                    action_id=f"{context.plan_id}-ev-minimum-soc",
                    plan_id=context.plan_id,
                    execute_not_before=execute_not_before,
                    execute_not_after=execute_not_after,
                    asset=ActionAsset.EV,
                    kind=ActionKind.EV_SCHEDULE,
                    desired_state={
                        "target_soc_percent": schedule.scheduled_soc_percent,
                        "ready_by": str(self.options[CONF_DEFAULT_READY_BY]),
                        "required_charge_percent": target.required_charge_percent,
                        "max_attainable_soc_percent": target.max_attainable_soc_percent,
                        "trip_history_observed_days": context.ev_trip_observed_days,
                        "trip_history_sufficient": context.ev_trip_history_sufficient,
                        "allocated_slots": [
                            {
                                "valid_at": allocation.valid_at.isoformat(),
                                "charge_kw": allocation.charge_kw,
                                "added_soc_percent": allocation.added_soc_percent,
                                "import_price": allocation.import_price,
                            }
                            for allocation in schedule.allocations
                        ],
                        "infeasible": schedule.infeasible,
                    },
                    hard_constraints=["ev_min_soc", "ready_by"],
                    reason_codes=["ev_soc_below_target", target.reason, schedule.reason],
                    expected_cost_delta=None,
                    confidence=confidence_from_context(context),
                    requires_haeo_plan_id=context.plan_id,
                )
            )
        return actions

    def _enphase_action(
        self,
        context: DecisionContext,
        execute_not_before: Any,
        execute_not_after: Any,
    ) -> PlanAction | None:
        arbitrage = _arbitrage_value(context, int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
        value = arbitrage["value"]
        min_savings = float(self.options[CONF_ENPHASE_MIN_SAVINGS])
        current_profile = context.current_enphase_profile
        arbitrage_profile = _enphase_profile_for_arbitrage(context, arbitrage["direction"])
        ai_profile = context.enphase_ai_profile
        if value >= min_savings and arbitrage_profile and current_profile != arbitrage_profile:
            return PlanAction(
                action_id=f"{context.plan_id}-enphase-arbitrage-profile",
                plan_id=context.plan_id,
                execute_not_before=execute_not_before,
                execute_not_after=execute_not_after,
                asset=ActionAsset.ENPHASE,
                kind=ActionKind.SET_PROFILE,
                desired_state={
                    "profile": arbitrage_profile,
                    "arbitrage_value": round(value, 4),
                    "arbitrage_source": arbitrage["source"],
                    "arbitrage_direction": arbitrage["direction"],
                },
                hard_constraints=["battery_floor", "enphase_min_savings", "enphase_profile_hold"],
                reason_codes=[f"enphase_{arbitrage['source']}_above_threshold"],
                expected_cost_delta=round(value, 4),
                confidence=confidence_from_context(context),
                requires_haeo_plan_id=context.plan_id,
            )
        if value < min_savings and ai_profile and current_profile and current_profile != ai_profile:
            return PlanAction(
                action_id=f"{context.plan_id}-enphase-restore-ai",
                plan_id=context.plan_id,
                execute_not_before=execute_not_before,
                execute_not_after=execute_not_after,
                asset=ActionAsset.ENPHASE,
                kind=ActionKind.RESTORE_AI,
                desired_state={
                    "profile": ai_profile,
                    "arbitrage_value": round(value, 4),
                    "arbitrage_source": arbitrage["source"],
                    "arbitrage_direction": arbitrage["direction"],
                },
                hard_constraints=["restore_ai_when_takeover_not_justified"],
                reason_codes=[f"enphase_{arbitrage['source']}_below_threshold"],
                expected_cost_delta=0.0,
                confidence=confidence_from_context(context),
                requires_haeo_plan_id=None,
            )
        return None

    def _hvac_suppression_action(
        self,
        context: DecisionContext,
        execute_not_before: Any,
        execute_not_after: Any,
    ) -> PlanAction | None:
        if context.occupancy_state != OccupancyState.OCCUPIED:
            return None
        if not _comfort_valid(context, float(self.options[CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT])):
            return None
        current_price = context.slots[0].import_price if context.slots else None
        future_prices = [slot.import_price for slot in context.slots[1:24] if slot.import_price is not None]
        if current_price is None or not future_prices:
            return None
        future_min = min(future_prices)
        delta = float(current_price) - float(future_min)
        threshold = float(self.options[CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA])
        if delta < threshold:
            return None
        return PlanAction(
            action_id=f"{context.plan_id}-hvac-expensive-period-suppression",
            plan_id=context.plan_id,
            execute_not_before=execute_not_before,
            execute_not_after=execute_not_after,
            asset=ActionAsset.DAIKIN,
            kind=ActionKind.SET_HVAC,
            desired_state={
                "suppress_automations": True,
                "current_import_price": round(float(current_price), 4),
                "future_min_import_price": round(float(future_min), 4),
            },
            hard_constraints=["occupied_comfort_within_bounds", "manual_hvac_override_inactive"],
            reason_codes=["hvac_expensive_period_suppression"],
            expected_cost_delta=round(delta, 4),
            confidence=confidence_from_context(context),
            requires_haeo_plan_id=None,
        )

    def _hvac_preconditioning_action(
        self,
        context: DecisionContext,
        execute_not_before: Any,
        execute_not_after: Any,
    ) -> PlanAction | None:
        if context.occupancy_state != OccupancyState.OCCUPIED:
            return None
        if not _comfort_valid(context, float(self.options[CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT])):
            return None

        current_price = context.slots[0].import_price if context.slots else None
        if current_price is None:
            return None
        lead_minutes = int(self.options[CONF_HVAC_PRECONDITION_LEAD_MINUTES])
        if lead_minutes <= 0:
            return None
        interval_minutes = int(self.options[CONF_PLANNING_INTERVAL_MINUTES])
        lead_slots = max(1, lead_minutes // interval_minutes)
        future_slots = [slot for slot in context.slots[1 : lead_slots + 1] if slot.import_price is not None]
        if not future_slots:
            return None
        future_peak_slot = max(future_slots, key=lambda slot: float(slot.import_price))
        delta = float(future_peak_slot.import_price) - float(current_price)
        threshold = float(self.options[CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA])
        if delta < threshold:
            return None

        target: float | None = None
        mode: str | None = None
        current_temperature = float(context.current_hvac_temperature_c)
        low = float(context.occupied_temperature_low_c)
        high = float(context.occupied_temperature_high_c)
        if current_temperature < low:
            target = low
            mode = "heat"
        elif current_temperature > high:
            target = high
            mode = "cool"
        if target is None or mode is None:
            return None

        projected_load_kw = thermal_hvac_load_kw(self.thermal_model, HVAC_PRECONDITION_PROJECTED_LOAD_KW)
        thermal_summary = thermal_model_summary(self.thermal_model)
        for slot in context.slots[: context.slots.index(future_peak_slot)]:
            slot.projected_hvac_load_kw = max(slot.projected_hvac_load_kw, projected_load_kw)

        return PlanAction(
            action_id=f"{context.plan_id}-hvac-precondition-before-expensive-period",
            plan_id=context.plan_id,
            execute_not_before=execute_not_before,
            execute_not_after=execute_not_after,
            asset=ActionAsset.DAIKIN,
            kind=ActionKind.SET_HVAC,
            desired_state={
                "hvac_mode": mode,
                "target_temperature": round(target, 1),
                "current_temperature": round(current_temperature, 1),
                "current_import_price": round(float(current_price), 4),
                "future_peak_import_price": round(float(future_peak_slot.import_price), 4),
                "projected_hvac_load_kw": projected_load_kw,
                "thermal_model_enabled": thermal_summary["enabled"],
                "thermal_model_sample_count": thermal_summary["active_sample_count"],
            },
            hard_constraints=[
                "occupied_comfort_within_bounds",
                "manual_hvac_override_inactive",
                "hvac_min_cycle",
            ],
            reason_codes=["hvac_precondition_before_expensive_period"],
            expected_cost_delta=round(delta, 4),
            confidence=confidence_from_context(context),
            requires_haeo_plan_id=None,
        )

    @staticmethod
    def project_flexible_loads(context: DecisionContext) -> list[FlexibleLoadProjection]:
        """Project flexible EV/HVAC load for HAEO second-pass planning."""
        return [
            FlexibleLoadProjection(
                valid_at=slot.valid_at,
                ev_load_kw=slot.projected_ev_load_kw,
                hvac_load_kw=slot.projected_hvac_load_kw,
            )
            for slot in context.slots
            if slot.projected_ev_load_kw or slot.projected_hvac_load_kw
        ]

    def _estimate_cost(self, context: DecisionContext) -> float | None:
        total = 0.0
        has_data = False
        interval_hours = timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES])).total_seconds() / 3600
        for slot in context.slots:
            if slot.import_price is None or slot.baseline_load_forecast_kw is None:
                continue
            load_kw = slot.baseline_load_forecast_kw + slot.projected_ev_load_kw + slot.projected_hvac_load_kw
            net_kw = load_kw - (slot.pv_forecast_kw or 0.0)
            if net_kw >= 0:
                total += net_kw * interval_hours * slot.import_price
            elif slot.export_price is not None:
                total += net_kw * interval_hours * slot.export_price
            has_data = True
        return round(total, 4) if has_data else None

    @staticmethod
    def _confidence(context: DecisionContext) -> float:
        return confidence_from_context(context)

    def _device_plans(self, context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
        """Return compact 24-hour device timelines for entity attributes."""
        interval_minutes = int(self.options[CONF_PLANNING_INTERVAL_MINUTES])
        climate_actions = [action for action in actions if action.asset == ActionAsset.DAIKIN]
        climate_plan = _device_plan(
            context,
            interval_minutes,
            _climate_timeline_entry,
            climate_actions,
        )
        climate_plan.update(_climate_plan_summary(context, climate_actions))
        return {
            "climate": climate_plan,
            "enphase": _device_plan(
                context,
                interval_minutes,
                _enphase_timeline_entry,
                [action for action in actions if action.asset == ActionAsset.ENPHASE],
            ),
            "ev": _device_plan(
                context,
                interval_minutes,
                _ev_timeline_entry,
                [action for action in actions if action.asset == ActionAsset.EV],
            ),
        }


def confidence_from_health(input_health: InputHealth) -> float:
    """Return confidence scalar for input health."""
    if input_health == InputHealth.HEALTHY:
        return 1.0
    if input_health == InputHealth.DEGRADED:
        return 0.65
    return 0.0


def confidence_from_context(context: DecisionContext) -> float:
    """Return confidence scalar capped by health and forecast/source confidence."""
    return round(min(confidence_from_health(context.input_health), context.forecast_confidence), 4)


def _device_plan(
    context: DecisionContext,
    interval_minutes: int,
    entry_fn: Any,
    actions: list[PlanAction],
) -> dict[str, Any]:
    """Build a compressed timeline for one device over the planning horizon."""
    timeline: list[dict[str, Any]] = []
    interval_hours = interval_minutes / 60
    for slot in context.slots:
        slot_actions = [
            action for action in actions if action.execute_not_before <= slot.valid_at < action.execute_not_after
        ]
        entry = entry_fn(slot, slot_actions, actions)
        _add_energy_estimates(entry, interval_hours)
        entry["start"] = slot.valid_at.isoformat()
        entry["end"] = (slot.valid_at + timedelta(minutes=interval_minutes)).isoformat()
        _append_timeline_entry(timeline, entry)
    return {
        "generated_at": context.created_at.isoformat(),
        "horizon_hours": len(context.slots) * interval_minutes / 60,
        "interval_minutes": interval_minutes,
        "total_estimated_energy_kwh": _timeline_sum(timeline, "estimated_energy_kwh"),
        "total_estimated_battery_charge_kwh": _timeline_sum(timeline, "estimated_battery_charge_kwh"),
        "total_estimated_battery_discharge_kwh": _timeline_sum(timeline, "estimated_battery_discharge_kwh"),
        "timeline": timeline,
    }


def _climate_plan_summary(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return current and next planned climate state summaries."""
    current = {
        "state": context.current_hvac_mode or "unknown",
        "hvac_mode": context.current_hvac_mode,
        "current_temperature": context.current_hvac_temperature_c,
        "current_power_kw": context.current_hvac_power_kw,
        "outdoor_temperature": context.current_outdoor_temperature_c,
        "occupied_temperature_low": context.occupied_temperature_low_c,
        "occupied_temperature_high": context.occupied_temperature_high_c,
        "occupancy": str(context.occupancy_state),
    }
    next_action = min(actions, key=lambda action: action.execute_not_before) if actions else None
    if next_action is None:
        next_planned = {
            "state": "idle",
            "reason": "no_planned_climate_action",
        }
    else:
        next_planned = _climate_action_state(next_action)
    return {
        "current_state": current,
        "current_state_label": _climate_current_state_label(current),
        "next_planned_state": next_planned,
        "next_planned_state_label": _climate_next_state_label(next_planned),
    }


def _climate_action_state(action: PlanAction) -> dict[str, Any]:
    """Return compact desired state for a planned climate action."""
    desired = action.desired_state
    state = "set_hvac"
    if desired.get("suppress_automations"):
        state = "suppressing_automation"
    if desired.get("hvac_mode") == "off":
        state = "off"
    result: dict[str, Any] = {
        "state": state,
        "action": str(action.kind),
        "execute_not_before": action.execute_not_before.isoformat(),
        "execute_not_after": action.execute_not_after.isoformat(),
        "reason_codes": action.reason_codes[:4],
    }
    for key in ("hvac_mode", "target_temperature", "projected_hvac_load_kw", "suppress_automations"):
        if desired.get(key) is not None:
            result[key] = desired.get(key)
    return result


def _climate_current_state_label(state: Mapping[str, Any]) -> str:
    """Return concise current climate state text."""
    mode = str(state.get("hvac_mode") or state.get("state") or "unknown")
    label = _display_text(mode)
    temperature = state.get("current_temperature")
    if temperature is not None:
        label = f"{label} ({temperature} C)"
    return label


def _climate_next_state_label(state: Mapping[str, Any]) -> str:
    """Return concise planned climate state text."""
    if state.get("state") == "idle":
        return "Idle"
    label = _display_text(state.get("state"))
    mode = state.get("hvac_mode")
    if mode and str(mode) != str(state.get("state")):
        label = f"{label}: {_display_text(mode)}"
    target = state.get("target_temperature")
    if target is not None:
        label = f"{label} to {target} C"
    return label


def _display_text(value: Any) -> str:
    text = str(value or "unknown").replace("_", " ").strip()
    return text.title() if text else "Unknown"


def _append_timeline_entry(timeline: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """Append or merge a timeline entry with the previous segment."""
    if timeline and _timeline_payload(timeline[-1]) == _timeline_payload(entry):
        timeline[-1]["end"] = entry["end"]
        _merge_energy_estimates(timeline[-1], entry)
        return
    timeline.append(entry)


def _timeline_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entry.items()
        if key
        not in {
            "start",
            "end",
            "estimated_energy_kwh",
            "estimated_battery_charge_kwh",
            "estimated_battery_discharge_kwh",
        }
    }


def _add_energy_estimates(entry: dict[str, Any], interval_hours: float) -> None:
    """Add per-slot kWh estimates from power values on a timeline entry."""
    if "projected_hvac_load_kw" in entry:
        entry["estimated_energy_kwh"] = _energy_kwh(entry["projected_hvac_load_kw"], interval_hours)
    if "charge_kw" in entry:
        entry["estimated_energy_kwh"] = _energy_kwh(entry["charge_kw"], interval_hours)
    if "battery_charge_kw" in entry:
        entry["estimated_battery_charge_kwh"] = _energy_kwh(entry["battery_charge_kw"], interval_hours)
    if "battery_discharge_kw" in entry:
        entry["estimated_battery_discharge_kwh"] = _energy_kwh(entry["battery_discharge_kw"], interval_hours)


def _merge_energy_estimates(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Sum per-slot kWh values into a compressed timeline segment."""
    for key in ("estimated_energy_kwh", "estimated_battery_charge_kwh", "estimated_battery_discharge_kwh"):
        if key in source:
            target[key] = round(float(target.get(key, 0.0) or 0.0) + float(source[key]), 4)


def _timeline_sum(timeline: list[dict[str, Any]], key: str) -> float | None:
    total = sum(float(entry.get(key, 0.0) or 0.0) for entry in timeline)
    return round(total, 4) if total > 0 else None


def _energy_kwh(power_kw: Any, interval_hours: float) -> float:
    return round(max(float(power_kw), 0.0) * interval_hours, 4)


def _climate_timeline_entry(slot: Any, slot_actions: list[PlanAction], actions: list[PlanAction]) -> dict[str, Any]:
    """Return the climate state for one timeline slot."""
    action = slot_actions[0] if slot_actions else None
    projected_load = _positive_or_none(slot.projected_hvac_load_kw)
    if action is not None:
        desired = action.desired_state
        entry: dict[str, Any] = {
            "state": "set_hvac",
            "action": str(action.kind),
            "reason_codes": action.reason_codes[:4],
        }
        if desired.get("suppress_automations"):
            entry["state"] = "suppressing_automation"
        if desired.get("hvac_mode"):
            entry["hvac_mode"] = desired.get("hvac_mode")
            if desired.get("hvac_mode") == "off":
                entry["state"] = "off"
        if desired.get("target_temperature") is not None:
            entry["target_temperature"] = desired.get("target_temperature")
        if projected_load is not None and projected_load > 0:
            entry["projected_hvac_load_kw"] = round(projected_load, 4)
        return entry
    if projected_load is not None and projected_load > 0:
        related_action = actions[0] if actions else None
        entry = {
            "state": "preconditioning",
            "projected_hvac_load_kw": round(projected_load, 4),
        }
        if related_action is not None:
            entry["reason_codes"] = related_action.reason_codes[:4]
            if related_action.desired_state.get("hvac_mode"):
                entry["hvac_mode"] = related_action.desired_state.get("hvac_mode")
            if related_action.desired_state.get("target_temperature") is not None:
                entry["target_temperature"] = related_action.desired_state.get("target_temperature")
        return entry
    return {"state": "idle"}


def _enphase_timeline_entry(slot: Any, slot_actions: list[PlanAction], actions: list[PlanAction]) -> dict[str, Any]:
    """Return the Enphase state for one timeline slot."""
    planned_profile = _planned_enphase_profile(actions)
    action = slot_actions[0] if slot_actions else None
    if action is not None:
        entry = {
            "state": str(action.kind),
            "profile": action.desired_state.get("profile"),
            "reason_codes": action.reason_codes[:4],
        }
        if action.desired_state.get("arbitrage_direction"):
            entry["arbitrage_direction"] = action.desired_state.get("arbitrage_direction")
        if action.desired_state.get("arbitrage_value") is not None:
            entry["arbitrage_value"] = action.desired_state.get("arbitrage_value")
        return entry

    charge_kw = _positive_or_none(slot.haeo_battery_charge_forecast_kw)
    discharge_kw = _positive_or_none(slot.haeo_battery_discharge_forecast_kw)
    entry: dict[str, Any] = {"state": "idle"}
    if charge_kw is not None and charge_kw > 0:
        entry = {"state": "charge_battery", "battery_charge_kw": round(charge_kw, 4)}
    elif discharge_kw is not None and discharge_kw > 0:
        entry = {"state": "consume_battery", "battery_discharge_kw": round(discharge_kw, 4)}
    if planned_profile:
        entry["profile"] = planned_profile
    if slot.haeo_battery_soc_forecast_percent is not None:
        entry["battery_soc_percent"] = slot.haeo_battery_soc_forecast_percent
    return entry


def _ev_timeline_entry(slot: Any, slot_actions: list[PlanAction], actions: list[PlanAction]) -> dict[str, Any]:
    """Return the EV state for one timeline slot."""
    action = actions[0] if actions else None
    charge_kw = _positive_or_none(slot.projected_ev_load_kw)
    if charge_kw is None or charge_kw <= 0:
        return {"state": "idle"}
    entry: dict[str, Any] = {
        "state": "charging",
        "charge_kw": round(charge_kw, 4),
    }
    if action is not None:
        desired = action.desired_state
        entry["reason_codes"] = action.reason_codes[:4]
        if desired.get("target_soc_percent") is not None:
            entry["target_soc_percent"] = desired.get("target_soc_percent")
        if desired.get("ready_by") is not None:
            entry["ready_by"] = desired.get("ready_by")
        if desired.get("infeasible") is not None:
            entry["infeasible"] = desired.get("infeasible")
    return entry


def _planned_enphase_profile(actions: list[PlanAction]) -> str | None:
    for action in actions:
        profile = action.desired_state.get("profile")
        if profile:
            return str(profile)
    return None


def _next_ready_by(created_at: Any, ready_by: str) -> Any:
    """Return next ready-by datetime in the context timezone."""
    try:
        hour_text, minute_text = ready_by.split(":", 1)
        ready_time = time(hour=int(hour_text), minute=int(minute_text[:2]), tzinfo=created_at.tzinfo)
    except (TypeError, ValueError):
        ready_time = time(hour=7, minute=0, tzinfo=created_at.tzinfo)
    candidate = created_at.replace(
        hour=ready_time.hour,
        minute=ready_time.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= created_at:
        candidate += timedelta(days=1)
    return candidate


def _arbitrage_value(context: DecisionContext, interval_minutes: int) -> dict[str, Any]:
    battery_arbitrage = _haeo_battery_arbitrage(context, interval_minutes)
    if battery_arbitrage is not None:
        return {
            "value": battery_arbitrage["value"],
            "source": "haeo_battery_arbitrage_value",
            "direction": battery_arbitrage["direction"],
        }
    haeo_export_value = _haeo_export_value(context, interval_minutes)
    if haeo_export_value is not None:
        return {"value": haeo_export_value, "source": "haeo_export_value", "direction": "consume"}
    return {"value": _arbitrage_spread(context), "source": "price_spread", "direction": "consume"}


def _haeo_battery_arbitrage(context: DecisionContext, interval_minutes: int) -> dict[str, Any] | None:
    total = 0.0
    has_battery_evidence = False
    first_direction: str | None = None
    interval_hours = interval_minutes / 60
    for slot in context.slots:
        charge_kw = _positive_or_none(slot.haeo_battery_charge_forecast_kw)
        discharge_kw = _positive_or_none(slot.haeo_battery_discharge_forecast_kw)
        if charge_kw is not None:
            has_battery_evidence = True
            first_direction = first_direction or "charge"
            grid_import_kw = _positive_or_none(slot.haeo_grid_import_forecast_kw)
            import_price = _float_or_none(slot.import_price)
            if grid_import_kw is not None and import_price is not None:
                grid_charge_kw = min(charge_kw, grid_import_kw)
                total -= grid_charge_kw * import_price * interval_hours
        if discharge_kw is not None:
            has_battery_evidence = True
            first_direction = first_direction or "consume"
            price = None
            grid_export_kw = _positive_or_none(slot.haeo_grid_export_forecast_kw)
            if grid_export_kw is not None and grid_export_kw > 0:
                price = _float_or_none(slot.export_price)
            if price is None:
                price = _float_or_none(slot.import_price)
            if price is not None:
                total += discharge_kw * price * interval_hours
    if not has_battery_evidence:
        return None
    return {"value": round(total, 4), "direction": first_direction or "consume"}


def _haeo_battery_arbitrage_value(context: DecisionContext, interval_minutes: int) -> float | None:
    arbitrage = _haeo_battery_arbitrage(context, interval_minutes)
    return None if arbitrage is None else arbitrage["value"]


def _enphase_profile_for_arbitrage(context: DecisionContext, direction: str) -> str | None:
    if direction == "charge":
        return context.enphase_full_backup_profile
    return context.enphase_self_consumption_profile or context.enphase_arbitrage_profile


def _haeo_export_value(context: DecisionContext, interval_minutes: int) -> float | None:
    total = 0.0
    has_haeo_export = False
    interval_hours = interval_minutes / 60
    for slot in context.slots:
        export_kw = _positive_or_none(slot.haeo_grid_export_forecast_kw)
        export_price = _float_or_none(slot.export_price)
        if export_kw is None or export_price is None:
            continue
        has_haeo_export = True
        total += export_kw * export_price * interval_hours
    return round(total, 4) if has_haeo_export else None


def _arbitrage_spread(context: DecisionContext) -> float:
    import_prices = [
        price for price in (_float_or_none(slot.import_price) for slot in context.slots) if price is not None
    ]
    export_prices = [
        price for price in (_float_or_none(slot.export_price) for slot in context.slots) if price is not None
    ]
    if not import_prices or not export_prices:
        return 0.0
    return max(export_prices) - min(import_prices)


def _positive_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    if number is None:
        return None
    return max(number, 0.0)


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _comfort_valid(context: DecisionContext, tolerance_percent: float) -> bool:
    if (
        context.current_hvac_temperature_c is None
        or context.occupied_temperature_low_c is None
        or context.occupied_temperature_high_c is None
    ):
        return False
    tolerance = tolerance_percent / 100.0
    low = context.occupied_temperature_low_c * (1 - tolerance)
    high = context.occupied_temperature_high_c * (1 + tolerance)
    return low <= context.current_hvac_temperature_c <= high
