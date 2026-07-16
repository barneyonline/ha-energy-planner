"""Deterministic dry-run planner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from math import ceil, isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import (
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_USABLE_CAPACITY_KWH,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_MIN_SAVINGS,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CONTINUOUS_CHARGING,
    CONF_EV_EARLIEST_START,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    CONF_EV_LOW_PRICE_THRESHOLD,
    CONF_EV_MAX_IMPORT_PRICE,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
    CONF_EV_PRICE_LIMIT_ENABLED,
    CONF_EV_SOC_PER_KWH,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
    CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA,
    CONF_MIN_CLIMATE_CONFIDENCE,
    CONF_MIN_ENPHASE_CONFIDENCE,
    CONF_MIN_EV_CONFIDENCE,
    CONF_MIN_LOAD_CONFIDENCE,
    CONF_MIN_SOLAR_CONFIDENCE,
    CONF_MIN_TARIFF_CONFIDENCE,
    CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PRIORITY_WEIGHTS,
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
from .safety import strict_bool
from .thermal_model import (
    thermal_active_temperature_rate_c_per_hour,
    thermal_hvac_load_kw,
    thermal_model_summary,
)

HVAC_PRECONDITION_PROJECTED_LOAD_KW = 1.0
THERMAL_SHIFT_MIN_TARGET_DELTA_C = 0.3
THERMAL_SHIFT_FALLBACK_DRIFT_C_PER_HOUR = 0.5
HVAC_SUPPRESSION_LOOKAHEAD = timedelta(hours=2)


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
        estimated_cost_horizon = self._estimated_cost_horizon_hours(context)
        device_plans = self._device_plans(context, actions)
        confidence_breakdown = _confidence_breakdown(context, actions)
        decision_audit = _decision_audit(context, actions, self.options)
        rejected_actions = _rejected_actions(context, actions, self.options, self.thermal_model)
        timeline_card = _timeline_card_rows(device_plans)

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
            decision_audit=decision_audit,
            rejected_actions=rejected_actions,
            timeline_card=timeline_card,
            confidence_breakdown=confidence_breakdown,
            estimated_cost_horizon_hours=estimated_cost_horizon,
        )

    def _mode(self, context: DecisionContext) -> PlannerMode:
        planner_enabled = strict_bool(self.options.get(CONF_PLANNER_ENABLED), default=False)
        dry_run = strict_bool(self.options.get(CONF_DRY_RUN), default=True)
        if context.input_health == InputHealth.UNSAFE:
            return PlannerMode.ACTIVE_DEGRADED if planner_enabled else PlannerMode.DISABLED
        if not planner_enabled:
            return PlannerMode.DISABLED
        if dry_run:
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
                "pv_forecast_lower_kw": slot.pv_forecast_lower_kw,
                "baseline_load_forecast_kw": slot.baseline_load_forecast_kw,
                "baseline_load_forecast_upper_kw": slot.baseline_load_forecast_upper_kw,
                "carbon_intensity_g_per_kwh": slot.carbon_intensity_g_per_kwh,
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
        ev_min = float(self.options[CONF_EV_MIN_SOC_PERCENT])
        if context.ev_connected is not False and context.current_ev_soc_percent is not None:
            ready_by_text = context.ev_ready_by or str(self.options[CONF_DEFAULT_READY_BY])
            ready_by = _next_ready_by(context.created_at, ready_by_text, context.local_timezone)
            earliest_start = _ev_earliest_start(
                context.created_at,
                ready_by,
                str(self.options.get(CONF_EV_EARLIEST_START, "None")),
                context.local_timezone,
            )
            charge_rate_kw = float(self.options[CONF_EV_CHARGE_RATE_KW])
            soc_per_kwh = float(self.options[CONF_EV_SOC_PER_KWH])
            target_soc = context.ev_target_soc_percent
            fallback_target_soc = (
                float(target_soc)
                if target_soc is not None
                else float(self.options[CONF_EV_FALLBACK_TARGET_SOC_PERCENT])
            )
            target = calculate_ev_target(
                current_soc_percent=context.current_ev_soc_percent,
                summary=EVTripSummary(
                    observed_days=context.ev_trip_observed_days,
                    max_daily_soc_percent=context.ev_trip_max_daily_soc_percent,
                    average_daily_soc_percent=context.ev_trip_average_daily_soc_percent,
                    history_sufficient=context.ev_trip_history_sufficient and target_soc is None,
                ),
                ev_min_soc_percent=ev_min,
                ev_max_soc_percent=float(self.options[CONF_EV_MAX_SOC_PERCENT]),
                fallback_target_soc_percent=fallback_target_soc,
                available_charge_hours=max((ready_by - earliest_start).total_seconds() / 3600, 0.0),
                charge_rate_percent_per_hour=charge_rate_kw * soc_per_kwh,
            )
            current_slot = context.slots[0] if context.slots else None
            emergency_charge = context.current_ev_soc_percent < ev_min
            low_price_charge = bool(self.options.get(CONF_EV_LOW_PRICE_CHARGING_ENABLED, False)) and bool(
                current_slot is not None
                and current_slot.import_price is not None
                and float(current_slot.import_price) <= float(self.options[CONF_EV_LOW_PRICE_THRESHOLD])
            )
            schedule = allocate_least_cost_charging(
                context.slots,
                current_soc_percent=context.current_ev_soc_percent,
                target_soc_percent=target.target_soc_percent,
                ready_by=ready_by,
                charge_rate_kw=charge_rate_kw,
                soc_per_kwh=soc_per_kwh,
                interval_minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]),
                carbon_weight=_carbon_schedule_weight(self.options),
                earliest_start=earliest_start,
                continuous=bool(self.options.get(CONF_EV_CONTINUOUS_CHARGING, True)),
                force_current=emergency_charge or low_price_charge,
                max_import_price=float(self.options[CONF_EV_MAX_IMPORT_PRICE])
                if bool(self.options.get(CONF_EV_PRICE_LIMIT_ENABLED, False))
                else None,
            )
            allocation_by_time = {allocation.valid_at: allocation for allocation in schedule.allocations}
            for slot in context.slots:
                if slot.valid_at in allocation_by_time:
                    slot.projected_ev_load_kw = allocation_by_time[slot.valid_at].charge_kw
            charging_required_now = bool(current_slot and current_slot.valid_at in allocation_by_time)
            manual_ev = next(
                (override for override in context.active_overrides if override.kind == "manual_ev_charging"),
                None,
            )
            if manual_ev is not None:
                charging_required_now = manual_ev.reason == "manual_start"
                charging_reason = "ev_manual_start_override" if charging_required_now else "ev_manual_stop_override"
            elif emergency_charge and target.required_charge_percent > 0:
                charging_reason = "ev_below_minimum_soc_charge_now"
            elif low_price_charge and charging_required_now:
                charging_reason = "ev_low_price_charge_now"
            elif charging_required_now:
                charging_reason = "ev_in_allocated_charging_window"
            else:
                charging_reason = "ev_outside_allocated_charging_window"
            actions.append(
                PlanAction(
                    action_id=f"{context.plan_id}-ev-native-smart-charge",
                    plan_id=context.plan_id,
                    execute_not_before=execute_not_before,
                    execute_not_after=execute_not_after,
                    asset=ActionAsset.EV,
                    kind=ActionKind.EV_SCHEDULE,
                    desired_state={
                        "charging_required_now": charging_required_now,
                        "charging_observed": context.ev_charging,
                        "charging_reason": charging_reason,
                        "target_soc_percent": schedule.scheduled_soc_percent,
                        "ready_by": ready_by_text,
                        "ready_by_utc": ready_by.isoformat(),
                        "ready_by_timezone": context.local_timezone,
                        "earliest_start_utc": earliest_start.isoformat(),
                        "configured_target_soc_percent": target_soc,
                        "required_charge_percent": target.required_charge_percent,
                        "max_attainable_soc_percent": target.max_attainable_soc_percent,
                        "continuous_charging": bool(self.options.get(CONF_EV_CONTINUOUS_CHARGING, True)),
                        "price_limit": float(self.options[CONF_EV_MAX_IMPORT_PRICE])
                        if bool(self.options.get(CONF_EV_PRICE_LIMIT_ENABLED, False))
                        else None,
                        "trip_history_observed_days": context.ev_trip_observed_days,
                        "trip_history_sufficient": context.ev_trip_history_sufficient,
                        "allocated_slots": [
                            {
                                "valid_at": allocation.valid_at.isoformat(),
                                "charge_kw": allocation.charge_kw,
                                "added_soc_percent": allocation.added_soc_percent,
                                "import_price": allocation.import_price,
                                "effective_price": allocation.effective_price,
                                "solar_surplus_used_kw": allocation.solar_surplus_used_kw,
                                "grid_import_used_kw": allocation.grid_import_used_kw,
                                "carbon_intensity_g_per_kwh": allocation.carbon_intensity_g_per_kwh,
                                "estimated_carbon_g": allocation.estimated_carbon_g,
                            }
                            for allocation in schedule.allocations
                        ],
                        "infeasible": schedule.infeasible,
                    },
                    hard_constraints=["ev_min_soc", "ready_by", "charger_connected"],
                    reason_codes=[charging_reason, target.reason, schedule.reason],
                    expected_cost_delta=None,
                    confidence=confidence_from_context(context),
                    requires_haeo_plan_id=context.plan_id,
                )
            )
        enphase_action = self._enphase_action(context, execute_not_before, execute_not_after)
        if enphase_action is not None:
            actions.append(enphase_action)
        actions = [action for action in actions if _action_meets_confidence_threshold(action, context, self.options)]
        return sorted(actions, key=lambda action: _action_score(action, context, self.options)["score"], reverse=True)

    def _enphase_action(
        self,
        context: DecisionContext,
        execute_not_before: Any,
        execute_not_after: Any,
    ) -> PlanAction | None:
        arbitrage = _arbitrage_value(context, int(self.options[CONF_PLANNING_INTERVAL_MINUTES]), self.options)
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
                    "arbitrage_details": arbitrage.get("details", {}),
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
                    "arbitrage_details": arbitrage.get("details", {}),
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
        current_slot = context.slots[0] if context.slots else None
        current_price = current_slot.import_price if current_slot is not None else None
        lookahead_end = current_slot.valid_at + HVAC_SUPPRESSION_LOOKAHEAD if current_slot is not None else None
        future_prices = [
            slot.import_price
            for slot in context.slots
            if current_slot is not None
            and lookahead_end is not None
            and current_slot.valid_at < slot.valid_at < lookahead_end
            and slot.import_price is not None
        ]
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
        current_valid_at = context.slots[0].valid_at
        lead_end = current_valid_at + timedelta(minutes=lead_minutes)
        future_slots = [
            slot
            for slot in context.slots
            if current_valid_at < slot.valid_at <= lead_end and slot.import_price is not None
        ]
        if not future_slots:
            return None
        future_peak_slot = max(future_slots, key=lambda slot: float(slot.import_price))
        delta = float(future_peak_slot.import_price) - float(current_price)
        threshold = float(self.options[CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA])
        if delta < threshold:
            return None

        reason_code = "hvac_precondition_before_expensive_period"
        desired_extra: dict[str, Any] = {}
        current_temperature = float(context.current_hvac_temperature_c)
        low = float(context.occupied_temperature_low_c)
        high = float(context.occupied_temperature_high_c)
        target: float | None = None
        mode: str | None = None
        if current_temperature < low:
            target = low
            mode = "heat"
        elif current_temperature > high:
            target = high
            mode = "cool"
        else:
            shift = _thermal_shift_target(
                context,
                future_peak_slot,
                interval_minutes,
                self.thermal_model,
            )
            if shift is None:
                return None
            target = shift["target_temperature"]
            mode = shift["hvac_mode"]
            reason_code = "hvac_thermal_shift_before_expensive_period"
            desired_extra.update(shift)

        projected_load_kw = thermal_hvac_load_kw(self.thermal_model, HVAC_PRECONDITION_PROJECTED_LOAD_KW)
        thermal_summary = thermal_model_summary(self.thermal_model)
        precondition_slots = _precondition_slots(
            context=context,
            current_temperature=current_temperature,
            target_temperature=float(target),
            mode=str(mode),
            latest_end=future_peak_slot.valid_at,
            thermal_model=self.thermal_model,
        )
        for slot in precondition_slots:
            slot.projected_hvac_load_kw = max(slot.projected_hvac_load_kw, projected_load_kw)

        desired_extra.update(
            {
                "thermal_model_enabled": thermal_summary["enabled"],
                "thermal_model_sample_count": thermal_summary["active_sample_count"],
                "active_heat_rate_c_per_hour": thermal_summary["active_heat_rate_c_per_hour"],
                "active_cool_rate_c_per_hour": thermal_summary["active_cool_rate_c_per_hour"],
                "passive_indoor_drift_c_per_hour": thermal_summary["passive_indoor_drift_c_per_hour"],
                "precondition_slot_count": len(precondition_slots),
            }
        )
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
                **desired_extra,
            },
            hard_constraints=[
                "occupied_comfort_within_bounds",
                "manual_hvac_override_inactive",
                "hvac_min_cycle",
            ],
            reason_codes=[reason_code],
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
            haeo_import_kw = _positive_or_none(slot.haeo_grid_import_forecast_kw)
            haeo_export_kw = _positive_or_none(slot.haeo_grid_export_forecast_kw)
            if haeo_import_kw is not None and haeo_export_kw is not None:
                if slot.import_price is not None:
                    total += haeo_import_kw * interval_hours * slot.import_price
                    has_data = True
                if slot.export_price is not None:
                    total -= haeo_export_kw * interval_hours * slot.export_price
                    has_data = True
                continue
            if slot.import_price is None or slot.baseline_load_forecast_kw is None:
                continue
            battery_charge_kw = _positive_or_none(slot.haeo_battery_charge_forecast_kw) or 0.0
            battery_discharge_kw = _positive_or_none(slot.haeo_battery_discharge_forecast_kw) or 0.0
            load_kw = (
                slot.baseline_load_forecast_kw
                + slot.projected_ev_load_kw
                + slot.projected_hvac_load_kw
                + battery_charge_kw
                - battery_discharge_kw
            )
            net_kw = load_kw - (slot.pv_forecast_kw or 0.0)
            if net_kw >= 0:
                total += net_kw * interval_hours * slot.import_price
            elif slot.export_price is not None:
                total += net_kw * interval_hours * slot.export_price
            has_data = True
        return round(total, 4) if has_data else None

    def _estimated_cost_horizon_hours(self, context: DecisionContext) -> float | None:
        """Return the duration represented by usable estimated-cost slots."""
        usable_slots = 0
        for slot in context.slots:
            haeo_import_kw = _positive_or_none(slot.haeo_grid_import_forecast_kw)
            haeo_export_kw = _positive_or_none(slot.haeo_grid_export_forecast_kw)
            if haeo_import_kw is not None and haeo_export_kw is not None:
                if slot.import_price is not None or slot.export_price is not None:
                    usable_slots += 1
                continue
            if slot.import_price is not None and slot.baseline_load_forecast_kw is not None:
                usable_slots += 1
        if usable_slots == 0:
            return None
        return round(usable_slots * int(self.options[CONF_PLANNING_INTERVAL_MINUTES]) / 60, 4)

    @staticmethod
    def _confidence(context: DecisionContext) -> float:
        return confidence_from_context(context)

    def _device_plans(self, context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
        """Return compact 24-hour device timelines for entity attributes."""
        interval_minutes = int(self.options[CONF_PLANNING_INTERVAL_MINUTES])
        climate_actions = [action for action in actions if action.asset == ActionAsset.DAIKIN]
        enphase_actions = [action for action in actions if action.asset == ActionAsset.ENPHASE]
        climate_plan = _device_plan(
            context,
            interval_minutes,
            _climate_timeline_entry,
            climate_actions,
        )
        climate_plan.update(_climate_plan_summary(context, climate_actions))
        enphase_plan = _device_plan(
            context,
            interval_minutes,
            _enphase_timeline_entry,
            enphase_actions,
        )
        enphase_plan.update(_enphase_plan_summary(context, enphase_actions))
        return {
            "climate": climate_plan,
            "enphase": enphase_plan,
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


def _confidence_breakdown(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return confidence by planning subsystem."""
    base = confidence_from_context(context)
    issue_text = " ".join(context.input_issues)
    breakdown = {
        "overall": base,
        "tariff": _subsystem_confidence(base, issue_text, ("amber_", "price_")),
        "solar": _subsystem_confidence(base, issue_text, ("pv_forecast", "solar")),
        "load": _subsystem_confidence(base, issue_text, ("baseline_load", "load_forecast")),
        "climate": _subsystem_confidence(base, issue_text, ("daikin_", "climate_", "weather_")),
        "ev": _subsystem_confidence(base, issue_text, ("ev_",)),
        "enphase": _subsystem_confidence(base, issue_text, ("enphase_", "battery_soc")),
    }
    assets_with_actions = {str(action.asset) for action in actions}
    return {
        **breakdown,
        "action_assets": sorted(assets_with_actions),
        "limited_by": min(breakdown, key=lambda key: breakdown[key]),
    }


def _subsystem_confidence(base: float, issue_text: str, issue_markers: tuple[str, ...]) -> float:
    """Return confidence reduced when a subsystem has matching input issues."""
    if any(marker in issue_text for marker in issue_markers):
        return round(min(base, 0.4), 4)
    return base


def _action_meets_confidence_threshold(
    action: PlanAction,
    context: DecisionContext,
    options: Mapping[str, Any],
) -> bool:
    """Return whether an action clears tariff and device confidence thresholds."""
    breakdown = _confidence_breakdown(context, [action])
    checks = [("tariff", CONF_MIN_TARIFF_CONFIDENCE)]
    if action.asset == ActionAsset.DAIKIN:
        checks.extend([("climate", CONF_MIN_CLIMATE_CONFIDENCE), ("load", CONF_MIN_LOAD_CONFIDENCE)])
    elif action.asset == ActionAsset.EV:
        checks.extend([("ev", CONF_MIN_EV_CONFIDENCE), ("solar", CONF_MIN_SOLAR_CONFIDENCE)])
    elif action.asset == ActionAsset.ENPHASE:
        checks.extend([("enphase", CONF_MIN_ENPHASE_CONFIDENCE), ("solar", CONF_MIN_SOLAR_CONFIDENCE)])
    for key, option in checks:
        threshold = float(options.get(option, 0.0) or 0.0) / 100.0
        if float(breakdown.get(key, 0.0) or 0.0) < threshold:
            return False
    return True


def _confidence_rejection_reason(
    asset: ActionAsset,
    context: DecisionContext,
    options: Mapping[str, Any],
) -> str | None:
    """Return a plain-English confidence rejection reason for an asset."""
    fake_action = PlanAction(
        action_id="confidence-check",
        plan_id=context.plan_id,
        execute_not_before=context.created_at,
        execute_not_after=context.created_at,
        asset=asset,
        kind=ActionKind.SET_HVAC if asset == ActionAsset.DAIKIN else ActionKind.EV_SCHEDULE,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=confidence_from_context(context),
        requires_haeo_plan_id=None,
    )
    if _action_meets_confidence_threshold(fake_action, context, options):
        return None
    breakdown = _confidence_breakdown(context, [])
    return (
        "Skipped because tariff or device confidence is below the configured threshold. "
        f"Current confidence: {round(float(breakdown.get('overall', 0.0)) * 100, 1)}%."
    )


def _decision_audit(
    context: DecisionContext,
    actions: list[PlanAction],
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Return scored decision evidence for accepted actions."""
    scored = [_action_score(action, context, options) for action in actions]
    return {
        "summary": _decision_summary(scored),
        "accepted": scored,
        "policy_order": _priority_order(options),
        "marginal_budget": _marginal_budget_summary(context, options),
    }


def _decision_summary(scored: list[dict[str, Any]]) -> str:
    """Return a compact plain-English decision summary."""
    if not scored:
        return "No device changes were selected for this planning run."
    first = scored[0]
    return f"Selected {len(scored)} action(s). Highest priority is {first['device']} because {first['reason']}."


def _action_score(action: PlanAction, context: DecisionContext, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return weighted priority score for one action."""
    components = _score_components(action, context)
    weights = _priority_weights(options)
    weighted = {key: round(components.get(key, 0.0) * weight, 4) for key, weight in weights.items()}
    score = round(sum(weighted.values()), 4)
    return {
        "action_id": action.action_id,
        "device": _asset_label(action.asset),
        "action": _display_text(action.kind),
        "score": score,
        "components": components,
        "weighted_components": weighted,
        "reason": _score_reason(action, components),
        "estimated_value": action.expected_cost_delta,
        "confidence": action.confidence,
    }


def _score_components(action: PlanAction, context: DecisionContext) -> dict[str, float]:
    """Return normalized scoring components for one action."""
    value = max(float(action.expected_cost_delta or 0.0), 0.0)
    components = {
        "cost": min(value / 2.0, 1.0),
        "comfort": 0.0,
        "ev_readiness": 0.0,
        "battery_reserve": 0.0,
        "solar_self_consumption": 0.0,
        "carbon": 0.0,
    }
    if action.asset == ActionAsset.DAIKIN:
        components["comfort"] = 1.0 if "away_hvac_policy" not in action.reason_codes else 0.9
        if action.desired_state.get("thermal_shift"):
            components["cost"] = max(components["cost"], 0.5)
            components["solar_self_consumption"] = 0.3
    if action.asset == ActionAsset.EV:
        required = float(action.desired_state.get("required_charge_percent") or 0.0)
        components["ev_readiness"] = min(required / 30.0, 1.0)
        solar_kw = sum(
            float(item.get("solar_surplus_used_kw") or 0.0)
            for item in action.desired_state.get("allocated_slots", [])
            if isinstance(item, dict)
        )
        components["solar_self_consumption"] = min(solar_kw / 10.0, 1.0)
    if action.asset == ActionAsset.ENPHASE:
        direction = action.desired_state.get("arbitrage_direction")
        components["solar_self_consumption"] = 1.0 if direction == "consume" else 0.4
        components["battery_reserve"] = _battery_reserve_score(context)
    components["carbon"] = _carbon_action_score(action, context)
    return components


def _carbon_schedule_weight(options: Mapping[str, Any]) -> float:
    """Return carbon's share of the joint cost/carbon EV objective."""
    weights = _priority_weights(options)
    carbon = weights.get("carbon", 0.0)
    cost = weights.get("cost", 0.0)
    return carbon / (carbon + cost) if carbon + cost > 0 else 0.0


def _carbon_action_score(action: PlanAction, context: DecisionContext) -> float:
    """Score how well an action aligns consumption with lower-grid-carbon slots."""
    intensities = [
        float(slot.carbon_intensity_g_per_kwh) for slot in context.slots if slot.carbon_intensity_g_per_kwh is not None
    ]
    if len(intensities) < 2 or max(intensities) <= min(intensities):
        return 0.0
    minimum, maximum = min(intensities), max(intensities)

    def low_carbon_score(value: float) -> float:
        return round(1.0 - (value - minimum) / (maximum - minimum), 4)

    current = context.slots[0].carbon_intensity_g_per_kwh if context.slots else None
    if action.asset == ActionAsset.EV:
        allocations = [
            item
            for item in action.desired_state.get("allocated_slots", [])
            if isinstance(item, dict) and item.get("carbon_intensity_g_per_kwh") is not None
        ]
        if not allocations:
            return 0.0
        total_grid = sum(float(item.get("grid_import_used_kw") or 0.0) for item in allocations)
        if total_grid <= 0:
            return 1.0
        average = (
            sum(
                float(item["carbon_intensity_g_per_kwh"]) * float(item.get("grid_import_used_kw") or 0.0)
                for item in allocations
            )
            / total_grid
        )
        return low_carbon_score(average)
    if current is None:
        return 0.0
    if action.asset == ActionAsset.DAIKIN and str(action.desired_state.get("mode", "")).lower() == "off":
        return round(1.0 - low_carbon_score(float(current)), 4)
    if action.asset == ActionAsset.ENPHASE and action.desired_state.get("arbitrage_direction") == "consume":
        return round(1.0 - low_carbon_score(float(current)), 4)
    return low_carbon_score(float(current))


def _battery_reserve_score(context: DecisionContext) -> float:
    """Return reserve urgency for home battery decisions."""
    if context.current_battery_soc_percent is None:
        return 0.0
    if context.current_battery_soc_percent <= 20:
        return 1.0
    if context.current_battery_soc_percent <= 40:
        return 0.5
    return 0.1


def _score_reason(action: PlanAction, components: dict[str, float]) -> str:
    """Return the strongest plain-English score reason."""
    strongest = max(components, key=lambda key: components[key])
    reason_by_component = {
        "cost": "it has the strongest cost or tariff benefit",
        "comfort": "it protects household comfort",
        "ev_readiness": "the EV needs charge before its ready-by time",
        "battery_reserve": "the home battery reserve matters for this decision",
        "solar_self_consumption": "it uses forecast solar that may otherwise be exported",
        "carbon": "it aligns with the carbon objective",
    }
    return reason_by_component.get(strongest, _display_text(action.kind))


def _priority_weights(options: Mapping[str, Any]) -> dict[str, float]:
    """Return descending weights from the configured priority order."""
    order = _priority_order(options)
    count = len(order)
    return {objective: float(count - index) / count for index, objective in enumerate(order)}


def _priority_order(options: Mapping[str, Any]) -> list[str]:
    """Return sanitized planning priority order."""
    allowed = ["cost", "comfort", "ev_readiness", "battery_reserve", "solar_self_consumption", "carbon"]
    raw = str(options.get(CONF_PRIORITY_WEIGHTS, "") or "")
    values = [item.strip() for item in raw.split(",") if item.strip() in allowed]
    result = []
    for item in [*values, *allowed]:
        if item not in result:
            result.append(item)
    return result


def _marginal_budget_summary(context: DecisionContext, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return shared energy budget used by marginal device decisions."""
    interval_minutes = int(options.get(CONF_PLANNING_INTERVAL_MINUTES, 5) or 5)
    surplus_kwh = _forecast_surplus_kwh(context, interval_minutes)
    battery = _battery_model(context, options)
    return {
        "forecast_surplus_kwh": surplus_kwh,
        "battery_charge_headroom_kwh": battery["charge_headroom_kwh"],
        "battery_discharge_available_kwh": battery["discharge_available_kwh"],
        "battery_max_charge_kw": battery["max_charge_kw"],
        "battery_max_discharge_kw": battery["max_discharge_kw"],
        "battery_round_trip_efficiency": battery["round_trip_efficiency"],
    }


def _rejected_actions(
    context: DecisionContext,
    actions: list[PlanAction],
    options: Mapping[str, Any],
    thermal_model: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return plain-English decisions that were considered but not selected."""
    rejected: list[dict[str, Any]] = []
    assets = {action.asset for action in actions}
    if ActionAsset.EV not in assets:
        rejected.append(_rejected_ev_decision(context, options))
    if ActionAsset.DAIKIN not in assets:
        rejected.append(_rejected_climate_decision(context, options, thermal_model))
    if ActionAsset.ENPHASE not in assets:
        rejected.append(_rejected_enphase_decision(context, options))
    return [item for item in rejected if item]


def _rejected_ev_decision(context: DecisionContext, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return why EV charging was not selected."""
    confidence_reason = _confidence_rejection_reason(ActionAsset.EV, context, options)
    if confidence_reason is not None:
        reason = confidence_reason
    elif context.ev_connected is False:
        reason = "Skipped EV charging because the EV is not connected."
    elif context.current_ev_soc_percent is None:
        reason = "Skipped EV charging because the current EV state of charge is not available."
    else:
        reason = "Skipped EV charging because the EV is already at or above the planned target."
    return {"device": "EV", "action": "Charge EV", "reason": reason}


def _rejected_climate_decision(
    context: DecisionContext,
    options: Mapping[str, Any],
    thermal_model: Mapping[str, Any],
) -> dict[str, Any]:
    """Return why climate control was not selected."""
    confidence_reason = _confidence_rejection_reason(ActionAsset.DAIKIN, context, options)
    if confidence_reason is not None:
        reason = confidence_reason
    elif context.occupancy_state != OccupancyState.OCCUPIED:
        reason = "Skipped comfort preconditioning because nobody is currently home."
    elif not _comfort_valid(context, float(options[CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT])):
        reason = "Skipped comfort preconditioning because climate comfort inputs are incomplete."
    elif not context.slots:
        reason = "Skipped comfort preconditioning because no tariff forecast slots are available."
    else:
        shift = _thermal_shift_target(
            context,
            context.slots[min(len(context.slots) - 1, 1)],
            int(options[CONF_PLANNING_INTERVAL_MINUTES]),
            thermal_model,
        )
        reason = (
            "Skipped comfort preconditioning because the price difference or comfort coast time "
            "does not justify running the climate system now."
            if shift is None
            else "Skipped comfort preconditioning because another device had higher marginal value."
        )
    return {"device": "Climate", "action": "Precondition", "reason": reason}


def _rejected_enphase_decision(context: DecisionContext, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return why Enphase profile control was not selected."""
    confidence_reason = _confidence_rejection_reason(ActionAsset.ENPHASE, context, options)
    if confidence_reason is not None:
        return {
            "device": "Enphase",
            "action": "Change battery profile",
            "reason": confidence_reason,
            "estimated_value": 0.0,
            "evidence": "confidence_threshold",
        }
    arbitrage = _arbitrage_value(context, int(options[CONF_PLANNING_INTERVAL_MINUTES]), options)
    threshold = float(options[CONF_ENPHASE_MIN_SAVINGS])
    if arbitrage["value"] < threshold:
        reason = (
            "Skipped Enphase profile change because battery or solar value "
            f"({round(float(arbitrage['value']), 2)}) is below the configured threshold ({threshold})."
        )
    else:
        reason = "Skipped Enphase profile change because the selected profile is already active."
    return {
        "device": "Enphase",
        "action": "Change battery profile",
        "reason": reason,
        "estimated_value": round(float(arbitrage["value"]), 4),
        "evidence": arbitrage["source"],
    }


def _timeline_card_rows(device_plans: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return dashboard-friendly upcoming timeline rows."""
    rows: list[dict[str, Any]] = []
    for device_key, plan in device_plans.items():
        if not isinstance(plan, dict):
            continue
        for item in plan.get("timeline", [])[:24]:
            if not isinstance(item, dict) or item.get("state") in {None, "idle", "unknown"}:
                continue
            rows.append(
                {
                    "time": _time_range(item),
                    "device": _display_text(device_key),
                    "action": _display_text(item.get("state")),
                    "reason": item.get("reason") or item.get("reason_codes"),
                    "estimated_kwh": item.get("estimated_energy_kwh"),
                    "estimated_value": item.get("arbitrage_value") or item.get("effective_price"),
                }
            )
    return rows[:24]


def _time_range(item: Mapping[str, Any]) -> str:
    """Return a compact ISO time range for a timeline row."""
    start = str(item.get("start", ""))
    end = str(item.get("end", ""))
    return f"{start[11:16]}-{end[11:16]}" if len(start) >= 16 and len(end) >= 16 else "Current period"


def _thermal_shift_target(
    context: DecisionContext,
    future_peak_slot: Any,
    interval_minutes: int,
    thermal_model: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a comfort-bounded thermal-shift target for cheap preheat/precool."""
    if (
        context.current_hvac_temperature_c is None
        or context.occupied_temperature_low_c is None
        or context.occupied_temperature_high_c is None
    ):
        return None
    current_temperature = float(context.current_hvac_temperature_c)
    low = float(context.occupied_temperature_low_c)
    high = float(context.occupied_temperature_high_c)
    if not low <= current_temperature <= high:
        return None
    mode = _thermal_shift_mode(context, current_temperature, low, high)
    if mode is None:
        return None
    target = high if mode == "heat" else low
    boundary = low if mode == "heat" else high
    if abs(target - current_temperature) < THERMAL_SHIFT_MIN_TARGET_DELTA_C:
        return None
    drift = _effective_passive_drift_c_per_hour(context, mode, thermal_model)
    time_to_peak_hours = max(
        (future_peak_slot.valid_at - context.created_at).total_seconds() / 3600,
        interval_minutes / 60,
    )
    coast_hours = _thermal_coast_hours(
        mode=mode,
        target_temperature=target,
        comfort_boundary=boundary,
        passive_drift_c_per_hour=drift,
    )
    if coast_hours is not None and coast_hours < time_to_peak_hours:
        return None
    return {
        "hvac_mode": mode,
        "target_temperature": round(target, 1),
        "thermal_shift": True,
        "comfort_coast_boundary": round(boundary, 1),
        "time_to_expensive_period_hours": round(time_to_peak_hours, 3),
        "estimated_coast_hours": None if coast_hours is None else round(coast_hours, 3),
    }


def _thermal_shift_mode(
    context: DecisionContext,
    current_temperature: float,
    low: float,
    high: float,
) -> str | None:
    """Infer whether thermal shifting should preheat or precool."""
    current_mode = str(context.current_hvac_mode or "").lower()
    if current_mode in {"heat", "cool"}:
        return current_mode
    if context.current_outdoor_temperature_c is not None:
        if float(context.current_outdoor_temperature_c) < current_temperature - 0.5:
            return "heat"
        if float(context.current_outdoor_temperature_c) > current_temperature + 0.5:
            return "cool"
    midpoint = (low + high) / 2
    if current_temperature < midpoint:
        return "heat"
    if current_temperature > midpoint:
        return "cool"
    return None


def _effective_passive_drift_c_per_hour(
    context: DecisionContext,
    mode: str,
    thermal_model: Mapping[str, Any],
) -> float | None:
    """Return learned or inferred passive indoor temperature drift."""
    summary = thermal_model_summary(thermal_model)
    drift = summary.get("passive_indoor_drift_c_per_hour")
    if isinstance(drift, int | float) and isfinite(float(drift)):
        return float(drift)
    if context.current_outdoor_temperature_c is None or context.current_hvac_temperature_c is None:
        return None
    outdoor_delta = float(context.current_outdoor_temperature_c) - float(context.current_hvac_temperature_c)
    if mode == "heat" and outdoor_delta < -0.5:
        return -THERMAL_SHIFT_FALLBACK_DRIFT_C_PER_HOUR
    if mode == "cool" and outdoor_delta > 0.5:
        return THERMAL_SHIFT_FALLBACK_DRIFT_C_PER_HOUR
    return None


def _thermal_coast_hours(
    *,
    mode: str,
    target_temperature: float,
    comfort_boundary: float,
    passive_drift_c_per_hour: float | None,
) -> float | None:
    """Return estimated hours before a preheated/precooled room reaches comfort boundary."""
    if passive_drift_c_per_hour is None or passive_drift_c_per_hour == 0:
        return None
    if mode == "heat" and passive_drift_c_per_hour < 0:
        return max((target_temperature - comfort_boundary) / abs(passive_drift_c_per_hour), 0.0)
    if mode == "cool" and passive_drift_c_per_hour > 0:
        return max((comfort_boundary - target_temperature) / passive_drift_c_per_hour, 0.0)
    return None


def _precondition_slot_count(
    *,
    current_temperature: float,
    target_temperature: float,
    mode: str,
    interval_minutes: int,
    max_slots: int,
    thermal_model: Mapping[str, Any],
) -> int:
    """Return how many slots should carry projected HVAC load for preconditioning."""
    if max_slots <= 0:
        return 0
    temperature_delta = abs(target_temperature - current_temperature)
    rate = thermal_active_temperature_rate_c_per_hour(thermal_model, mode)
    if rate is None or rate <= 0:
        return max_slots
    interval_hours = interval_minutes / 60
    return min(max(1, ceil((temperature_delta / rate) / interval_hours)), max_slots)


def _precondition_slots(
    *,
    context: DecisionContext,
    current_temperature: float,
    target_temperature: float,
    mode: str,
    latest_end: datetime,
    thermal_model: Mapping[str, Any],
) -> list[Any]:
    """Return timestamp-selected slots that carry projected HVAC load."""
    start = context.slots[0].valid_at if context.slots else context.created_at
    end = latest_end
    rate = thermal_active_temperature_rate_c_per_hour(thermal_model, mode)
    if rate is not None and rate > 0:
        required = timedelta(hours=abs(target_temperature - current_temperature) / rate)
        end = min(end, start + required)
    return [slot for slot in context.slots if start <= slot.valid_at < end]


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


def _enphase_plan_summary(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return current and next planned Enphase state summaries."""
    current = {
        "state": context.current_enphase_profile or "unknown",
        "profile": context.current_enphase_profile,
        "ai_profile": context.enphase_ai_profile,
        "self_consumption_profile": context.enphase_self_consumption_profile,
        "full_backup_profile": context.enphase_full_backup_profile,
    }
    next_action = min(actions, key=lambda action: action.execute_not_before) if actions else None
    if next_action is None:
        next_planned = {
            "state": "idle",
            "profile": context.current_enphase_profile,
            "reason": "no_planned_enphase_action",
        }
    else:
        next_planned = _enphase_action_state(next_action)
    return {
        "current_state": current,
        "current_state_label": _enphase_current_state_label(current),
        "next_planned_state": next_planned,
        "next_planned_state_label": _enphase_next_state_label(next_planned),
    }


def _enphase_action_state(action: PlanAction) -> dict[str, Any]:
    """Return compact desired state for a planned Enphase action."""
    result: dict[str, Any] = {
        "state": str(action.kind),
        "action": str(action.kind),
        "execute_not_before": action.execute_not_before.isoformat(),
        "execute_not_after": action.execute_not_after.isoformat(),
        "reason_codes": action.reason_codes[:4],
    }
    desired = action.desired_state
    for key in ("profile", "arbitrage_direction", "arbitrage_source", "arbitrage_value"):
        if desired.get(key) is not None:
            result[key] = desired.get(key)
    return result


def _enphase_current_state_label(state: Mapping[str, Any]) -> str:
    """Return concise current Enphase profile text."""
    return str(state.get("profile") or _display_text(state.get("state")))


def _enphase_next_state_label(state: Mapping[str, Any]) -> str:
    """Return concise planned Enphase state text."""
    if state.get("state") == "idle":
        profile = state.get("profile")
        return f"Idle: {profile}" if profile else "Idle"
    label = _display_text(state.get("state"))
    profile = state.get("profile")
    if profile:
        label = f"{label}: {profile}"
    return label


def _display_text(value: Any) -> str:
    text = str(value or "unknown").replace("_", " ").strip()
    if not text:
        return "Unknown"
    words = []
    for word in text.split():
        upper = word.upper()
        words.append(upper if upper in {"AI", "EV", "HVAC"} else word.title())
    return " ".join(words)


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


def _next_ready_by(created_at: Any, ready_by: str, local_timezone: str | None = None) -> Any:
    """Return the next local ready-by instant normalized to UTC."""
    try:
        hour_text, minute_text = ready_by.split(":", 1)
        ready_time = time(hour=int(hour_text), minute=int(minute_text[:2]))
    except (TypeError, ValueError):
        ready_time = time(hour=7, minute=0)
    try:
        timezone = ZoneInfo(local_timezone or "UTC")
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    created_at_utc = created_at.astimezone(UTC)
    local_date = created_at_utc.astimezone(timezone).date()
    day_offset = 0
    while True:
        candidates = _valid_local_instants(local_date + timedelta(days=day_offset), ready_time, timezone)
        future = [candidate for candidate in candidates if candidate > created_at_utc]
        if future:
            return min(future)
        day_offset += 1


def _ev_earliest_start(
    created_at: datetime,
    ready_by: datetime,
    configured_start: str,
    local_timezone: str | None,
) -> datetime:
    """Return the active charging window's earliest UTC instant."""
    if configured_start.strip().lower() == "none":
        return created_at.astimezone(UTC)
    try:
        hour_text, minute_text = configured_start.split(":", 1)
        start_time = time(hour=int(hour_text), minute=int(minute_text[:2]))
    except (TypeError, ValueError):
        return created_at.astimezone(UTC)
    try:
        timezone = ZoneInfo(local_timezone or "UTC")
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    ready_date = ready_by.astimezone(timezone).date()
    candidates = [
        instant
        for candidate_date in (ready_date - timedelta(days=1), ready_date)
        for instant in _valid_local_instants(candidate_date, start_time, timezone)
        if instant <= ready_by
    ]
    return max(created_at.astimezone(UTC), max(candidates))


def _valid_local_instants(local_date: date, local_time: time, timezone: ZoneInfo) -> list[datetime]:
    """Resolve a wall time, advancing through a DST gap when necessary."""
    requested = datetime.combine(local_date, local_time)
    minute_offset = 0
    while True:
        wall = requested + timedelta(minutes=minute_offset)
        candidates: set[datetime] = set()
        for fold in (0, 1):
            aware = wall.replace(tzinfo=timezone, fold=fold)
            instant = aware.astimezone(UTC)
            if instant.astimezone(timezone).replace(tzinfo=None) == wall:
                candidates.add(instant)
        if candidates:
            return sorted(candidates)
        minute_offset += 1


def _asset_label(asset: ActionAsset) -> str:
    """Return a user-facing asset label."""
    labels = {
        ActionAsset.DAIKIN: "Climate",
        ActionAsset.ENPHASE: "Enphase",
        ActionAsset.EV: "EV",
    }
    return labels.get(asset, _display_text(asset))


def _arbitrage_value(
    context: DecisionContext,
    interval_minutes: int,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    battery_arbitrage = _haeo_battery_arbitrage(context, interval_minutes, options)
    if battery_arbitrage is not None:
        return {
            "value": battery_arbitrage["value"],
            "source": "haeo_battery_arbitrage_value",
            "direction": battery_arbitrage["direction"],
            "details": battery_arbitrage.get("details", {}),
        }
    haeo_export_value = _haeo_export_value(context, interval_minutes)
    if haeo_export_value is not None:
        return {
            "value": haeo_export_value,
            "source": "haeo_export_value",
            "direction": "consume",
            "details": {"source": "haeo_grid_export_forecast_kw"},
        }
    forecast_export = _forecast_solar_export_value(context, interval_minutes, options)
    if forecast_export is not None:
        return {
            "value": forecast_export["value"],
            "source": "forecast_solar_export_value",
            "direction": "consume",
            "details": forecast_export,
        }
    return {
        "value": 0.0,
        "source": "insufficient_arbitrage_evidence",
        "direction": "consume",
        "details": _marginal_budget_summary(context, options or {}),
    }


def _haeo_battery_arbitrage(
    context: DecisionContext,
    interval_minutes: int,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    total = 0.0
    has_battery_evidence = False
    first_direction: str | None = None
    interval_hours = interval_minutes / 60
    battery = _battery_model(context, options or {})
    remaining_charge_kwh = battery["charge_headroom_kwh"]
    remaining_discharge_kwh = battery["discharge_available_kwh"]
    for slot in context.slots:
        charge_kw = _positive_or_none(slot.haeo_battery_charge_forecast_kw)
        discharge_kw = _positive_or_none(slot.haeo_battery_discharge_forecast_kw)
        if charge_kw is not None:
            has_battery_evidence = True
            first_direction = first_direction or "charge"
            grid_import_kw = _positive_or_none(slot.haeo_grid_import_forecast_kw)
            import_price = _float_or_none(slot.import_price)
            if grid_import_kw is not None and import_price is not None:
                grid_charge_kw = min(charge_kw, grid_import_kw, battery["max_charge_kw"])
                charged_kwh = min(
                    grid_charge_kw * interval_hours * battery["round_trip_efficiency"],
                    remaining_charge_kwh,
                )
                if charged_kwh > 0:
                    remaining_charge_kwh -= charged_kwh
                    total -= (charged_kwh / battery["round_trip_efficiency"]) * import_price
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
                discharge_kw = min(discharge_kw, battery["max_discharge_kw"])
                discharged_kwh = min(discharge_kw * interval_hours, remaining_discharge_kwh)
                remaining_discharge_kwh -= discharged_kwh
                total += discharged_kwh * price
    if not has_battery_evidence:
        return None
    return {
        "value": round(total, 4),
        "direction": first_direction or "consume",
        "details": {
            "battery_charge_headroom_kwh": battery["charge_headroom_kwh"],
            "battery_discharge_available_kwh": battery["discharge_available_kwh"],
            "remaining_charge_headroom_kwh": round(remaining_charge_kwh, 4),
            "remaining_discharge_available_kwh": round(remaining_discharge_kwh, 4),
        },
    }


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


def _forecast_solar_export_value(
    context: DecisionContext,
    interval_minutes: int,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return estimated value of forecast solar surplus that could be self-consumed."""
    total = 0.0
    has_surplus = False
    interval_hours = interval_minutes / 60
    battery = _battery_model(context, options or {})
    remaining_charge_kwh = battery["charge_headroom_kwh"]
    accepted_surplus_kwh = 0.0
    forecast_surplus_kwh = 0.0
    for slot in context.slots:
        pv_kw = _positive_or_none(
            slot.pv_forecast_lower_kw if slot.pv_forecast_lower_kw is not None else slot.pv_forecast_kw
        )
        load_kw = _positive_or_none(
            slot.baseline_load_forecast_upper_kw
            if slot.baseline_load_forecast_upper_kw is not None
            else slot.baseline_load_forecast_kw
        )
        export_price = _float_or_none(slot.export_price)
        if pv_kw is None or load_kw is None or export_price is None:
            continue
        flexible_load_kw = max(float(slot.projected_ev_load_kw or 0.0), 0.0) + max(
            float(slot.projected_hvac_load_kw or 0.0),
            0.0,
        )
        surplus_kw = max(pv_kw - load_kw - flexible_load_kw, 0.0)
        if surplus_kw <= 0:
            continue
        forecast_surplus_kwh += surplus_kw * interval_hours
        has_surplus = True
        charge_kw = min(surplus_kw, battery["max_charge_kw"])
        charge_input_kwh = charge_kw * interval_hours
        stored_kwh = min(charge_input_kwh * battery["round_trip_efficiency"], remaining_charge_kwh)
        if stored_kwh <= 0:
            continue
        remaining_charge_kwh -= stored_kwh
        accepted_input_kwh = stored_kwh / battery["round_trip_efficiency"]
        accepted_surplus_kwh += accepted_input_kwh
        total += accepted_input_kwh * export_price * battery["round_trip_efficiency"]
    if not has_surplus:
        return None
    return {
        "value": round(total, 4),
        "forecast_surplus_kwh": round(forecast_surplus_kwh, 4),
        "accepted_surplus_kwh": round(accepted_surplus_kwh, 4),
        "battery_charge_headroom_kwh": battery["charge_headroom_kwh"],
        "remaining_charge_headroom_kwh": round(remaining_charge_kwh, 4),
        "battery_max_charge_kw": battery["max_charge_kw"],
        "battery_round_trip_efficiency": battery["round_trip_efficiency"],
    }


def _forecast_surplus_kwh(context: DecisionContext, interval_minutes: int) -> float:
    """Return forecast solar surplus after projected flexible loads."""
    interval_hours = interval_minutes / 60
    total = 0.0
    for slot in context.slots:
        pv_kw = _positive_or_none(
            slot.pv_forecast_lower_kw if slot.pv_forecast_lower_kw is not None else slot.pv_forecast_kw
        )
        load_kw = _positive_or_none(
            slot.baseline_load_forecast_upper_kw
            if slot.baseline_load_forecast_upper_kw is not None
            else slot.baseline_load_forecast_kw
        )
        if pv_kw is None or load_kw is None:
            continue
        flexible_kw = max(float(slot.projected_ev_load_kw or 0.0), 0.0) + max(
            float(slot.projected_hvac_load_kw or 0.0),
            0.0,
        )
        total += max(pv_kw - load_kw - flexible_kw, 0.0) * interval_hours
    return round(total, 4)


def _battery_model(context: DecisionContext, options: Mapping[str, Any]) -> dict[str, float]:
    """Return bounded battery physics used by planning estimates."""
    capacity_kwh = max(_float_or_none(options.get(CONF_BATTERY_USABLE_CAPACITY_KWH)) or 0.0, 0.0)
    soc = _float_or_none(context.current_battery_soc_percent)
    reserve_soc = max(_float_or_none(options.get(CONF_BATTERY_MIN_SOC_PERCENT)) or 0.0, 0.0)
    efficiency_percent = _float_or_none(options.get(CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT))
    efficiency = min(max((efficiency_percent or 90.0) / 100.0, 0.01), 1.0)
    max_charge_kw = max(_float_or_none(options.get(CONF_BATTERY_MAX_CHARGE_KW)) or 0.0, 0.0)
    max_discharge_kw = max(_float_or_none(options.get(CONF_BATTERY_MAX_DISCHARGE_KW)) or 0.0, 0.0)
    if soc is None or capacity_kwh <= 0:
        charge_headroom_kwh = capacity_kwh
        discharge_available_kwh = 0.0
    else:
        charge_headroom_kwh = max(capacity_kwh * ((100.0 - soc) / 100.0), 0.0)
        discharge_available_kwh = max(capacity_kwh * ((soc - reserve_soc) / 100.0), 0.0)
    return {
        "capacity_kwh": round(capacity_kwh, 4),
        "soc_percent": -1.0 if soc is None else round(soc, 4),
        "reserve_soc_percent": round(reserve_soc, 4),
        "charge_headroom_kwh": round(charge_headroom_kwh, 4),
        "discharge_available_kwh": round(discharge_available_kwh, 4),
        "round_trip_efficiency": round(efficiency, 4),
        "max_charge_kw": round(max_charge_kw, 4),
        "max_discharge_kw": round(max_discharge_kw, 4),
    }


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
