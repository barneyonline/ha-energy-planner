"""Deterministic dry-run planner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from math import ceil, isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
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
    CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED,
    CONF_EV_EARLIEST_START,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    CONF_EV_LOW_PRICE_THRESHOLD,
    CONF_EV_MAX_IMPORT_PRICE,
    CONF_EV_PRICE_LIMIT_ENABLED,
    CONF_EV_SOC_PER_KWH,
    CONF_HOUSEHOLD_LOAD,
    CONF_HVAC_MIN_CYCLE_MINUTES,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
    CONF_HVAC_PRECONDITION_WHILE_AWAY,
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
    CONF_PV_FORECAST,
    CONF_WEATHER,
    DEFAULT_OPTIONS,
)
from .ev import EVChargeSchedule, allocate_least_cost_charging, effective_ev_soc_per_kwh
from .models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    EnergyPlan,
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
THERMAL_SHIFT_FALLBACK_DRIFT_C_PER_HOUR = 0.5


class DryRunPlanner:
    """Create a deterministic, non-controlling v1 plan."""

    def __init__(
        self,
        options: Mapping[str, Any],
        thermal_model: Mapping[str, Any] | None = None,
        ev_charge_calibration: Mapping[str, Any] | None = None,
        ev_charging_entity_id: str | None = None,
        ev_soc_entity_id: str | None = None,
    ) -> None:
        """Initialize planner."""
        self.options = options
        self.thermal_model = dict(thermal_model or {})
        self.ev_charge_calibration = dict(ev_charge_calibration or {})
        self.ev_charging_entity_id = ev_charging_entity_id
        self.ev_soc_entity_id = ev_soc_entity_id

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
        rejected_actions = _rejected_actions(context, actions, self.options)
        timeline_card = _timeline_card_rows(device_plans)

        summary = "Planner disabled"
        if mode == PlannerMode.DRY_RUN:
            summary = "Dry-run plan generated; no device actions will be sent"
        elif mode == PlannerMode.ACTIVE_HEALTHY:
            summary = f"Active plan generated with {len(actions)} eligible candidate action(s)"
        elif context.input_health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
            summary = "Plan unsafe; required inputs are stale or unavailable"

        return EnergyPlan(
            plan_id=context.plan_id,
            created_at=context.created_at,
            horizon_hours=int(self.options[CONF_PLANNING_HORIZON_HOURS]),
            interval_minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]),
            status=(
                "current"
                if context.input_health in {InputHealth.HEALTHY, InputHealth.DEGRADED}
                else "unsafe"
            ),
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
        if context.input_health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
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
        manual_hvac_override_active = any(
            override.kind == "manual_hvac" and (override.expires_at is None or context.created_at < override.expires_at)
            for override in getattr(context, "active_overrides", [])
        )
        away_off_release_reason = (
            "manual_hvac_override"
            if manual_hvac_override_active
            else "hvac_required_evidence_lost"
            if context.hvac_control.get("required_evidence_lost")
            else None
        )
        if (
            mode not in {PlannerMode.ACTIVE_HEALTHY, PlannerMode.DRY_RUN}
            or context.input_health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}
        ):
            if (
                context.hvac_control.get("phase") == "away_off"
                and context.occupancy_state == OccupancyState.AWAY
                and away_off_release_reason is None
            ):
                return []
            if context.hvac_control:
                interval = timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
                return [
                    self._hvac_release_action(
                        context,
                        context.created_at,
                        context.created_at + interval,
                        away_off_release_reason or "hvac_required_evidence_lost",
                    )
                ]
            return []
        actions: list[PlanAction] = []
        execute_not_before = context.created_at
        execute_not_after = context.created_at + timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
        away_control_active = context.hvac_control.get("phase") == "away_off"
        away_preconditioning_enabled = strict_bool(
            self.options.get(CONF_HVAC_PRECONDITION_WHILE_AWAY, False),
            default=False,
        )
        if context.occupancy_state == OccupancyState.AWAY and away_control_active and away_off_release_reason is None:
            if away_preconditioning_enabled:
                actions.extend(
                    self._hvac_lifecycle_actions(
                        context,
                        execute_not_before,
                        execute_not_after,
                    )
                )
        elif context.occupancy_state == OccupancyState.AWAY and context.hvac_control:
            if away_preconditioning_enabled and context.hvac_control.get("phase") in {
                "preconditioning",
                "pre_peak_coast",
                "peak_coast",
            }:
                actions.extend(
                    self._hvac_lifecycle_actions(
                        context,
                        execute_not_before,
                        execute_not_after,
                    )
                )
            else:
                actions.append(
                    self._hvac_release_action(
                        context,
                        execute_not_before,
                        execute_not_after,
                        away_off_release_reason or "hvac_required_evidence_lost",
                    )
                )
        elif context.occupancy_state == OccupancyState.AWAY:
            away_actions = (
                self._hvac_lifecycle_actions(
                    context,
                    execute_not_before,
                    execute_not_after,
                )
                if away_preconditioning_enabled
                else []
            )
            preconditioning_action = next(
                (
                    action
                    for action in away_actions
                    if action.desired_state.get("phase") == "preconditioning"
                ),
                None,
            )
            if preconditioning_action is not None:
                if preconditioning_action.execute_not_before > execute_not_before:
                    actions.append(self._hvac_away_off_action(context, execute_not_before, execute_not_after))
                actions.extend(away_actions)
            else:
                actions.append(self._hvac_away_off_action(context, execute_not_before, execute_not_after))
        else:
            actions.extend(
                self._hvac_lifecycle_actions(
                    context,
                    execute_not_before,
                    execute_not_after,
                )
            )
        if (
            context.ev_connected is not False
            and context.current_ev_soc_percent is not None
            and context.ev_target_soc_percent is not None
        ):
            ready_by_text = context.ev_ready_by or str(self.options[CONF_DEFAULT_READY_BY])
            ready_by = _next_ready_by(context.created_at, ready_by_text, context.local_timezone)
            earliest_start = _ev_earliest_start(
                context.created_at,
                ready_by,
                str(self.options.get(CONF_EV_EARLIEST_START, "None")),
                context.local_timezone,
            )
            charge_rate_kw = float(self.options[CONF_EV_CHARGE_RATE_KW])
            soc_per_kwh, soc_per_kwh_source = effective_ev_soc_per_kwh(
                self.ev_charge_calibration,
                float(self.options[CONF_EV_SOC_PER_KWH]),
                charging_entity_id=self.ev_charging_entity_id,
                soc_entity_id=self.ev_soc_entity_id,
                charge_rate_kw=charge_rate_kw,
            )
            current_slot = context.slots[0] if context.slots else None
            max_import_price = (
                float(self.options[CONF_EV_MAX_IMPORT_PRICE])
                if bool(self.options.get(CONF_EV_PRICE_LIMIT_ENABLED, False))
                else None
            )
            low_price_charge = bool(self.options.get(CONF_EV_LOW_PRICE_CHARGING_ENABLED, False)) and bool(
                current_slot is not None
                and current_slot.import_price is not None
                and float(current_slot.import_price) <= float(self.options[CONF_EV_LOW_PRICE_THRESHOLD])
                and (max_import_price is None or float(current_slot.import_price) <= max_import_price)
            )
            continuous_charging = bool(self.options.get(CONF_EV_CONTINUOUS_CHARGING, True))
            daylight_lowest_cost_enabled = bool(
                self.options.get(CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED, False)
            )
            continue_current_charging = continuous_charging and context.ev_charging is True
            available_charge_hours = max((ready_by - earliest_start).total_seconds() / 3600, 0.0)
            if (
                current_slot is not None
                and current_slot.valid_at < earliest_start
                and (
                    low_price_charge
                    or (
                        continue_current_charging
                        and current_slot.import_price is not None
                        and (max_import_price is None or float(current_slot.import_price) <= max_import_price)
                    )
                )
            ):
                available_charge_hours += int(self.options[CONF_PLANNING_INTERVAL_MINUTES]) / 60.0
            requested_target_soc = float(context.ev_target_soc_percent)
            target_soc = requested_target_soc
            target_reason = "vehicle_target_soc"
            required_charge_percent = max(target_soc - context.current_ev_soc_percent, 0.0)
            max_attainable_soc_percent = min(
                100.0,
                context.current_ev_soc_percent
                + available_charge_hours * charge_rate_kw * soc_per_kwh,
            )
            target_infeasible = max_attainable_soc_percent + 0.000001 < target_soc
            standard_schedule = allocate_least_cost_charging(
                context.slots,
                current_soc_percent=context.current_ev_soc_percent,
                target_soc_percent=target_soc,
                ready_by=ready_by,
                charge_rate_kw=charge_rate_kw,
                soc_per_kwh=soc_per_kwh,
                interval_minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]),
                carbon_weight=_carbon_schedule_weight(self.options),
                earliest_start=earliest_start,
                continuous=continuous_charging,
                force_current=low_price_charge,
                continue_current=continue_current_charging,
                max_import_price=max_import_price,
            )
            schedule, daylight_evidence = _daylight_preferred_ev_schedule(
                context,
                standard_schedule=standard_schedule,
                enabled=daylight_lowest_cost_enabled,
                current_soc_percent=context.current_ev_soc_percent,
                target_soc_percent=target_soc,
                ready_by=ready_by,
                earliest_start=earliest_start,
                charge_rate_kw=charge_rate_kw,
                soc_per_kwh=soc_per_kwh,
                interval_minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]),
                carbon_weight=_carbon_schedule_weight(self.options),
                continuous=continuous_charging,
                continue_current=continue_current_charging,
                force_current=low_price_charge,
                max_import_price=max_import_price,
            )
            max_attainable_soc_percent = max(
                max_attainable_soc_percent,
                schedule.scheduled_soc_percent,
            )
            target_infeasible = schedule.infeasible
            allocation_by_time = {allocation.valid_at: allocation for allocation in schedule.allocations}
            current_allocation = allocation_by_time.get(current_slot.valid_at) if current_slot else None
            for slot in context.slots:
                if slot.valid_at in allocation_by_time:
                    slot.projected_ev_load_kw = allocation_by_time[slot.valid_at].charge_kw
            charging_required_now = bool(current_slot and current_slot.valid_at in allocation_by_time)
            keep_charger_on = bool(self.options.get(CONF_EV_KEEP_CHARGER_ON, False))
            keep_on_after_target = bool(
                keep_charger_on
                and not target_infeasible
                and context.current_ev_soc_percent >= target_soc
            )
            manual_ev = next(
                (override for override in context.active_overrides if override.kind == "manual_ev_charging"),
                None,
            )
            preconditioning_required_now = False
            if manual_ev is not None:
                charging_required_now = manual_ev.reason == "manual_start"
                charging_reason = "ev_manual_start_override" if charging_required_now else "ev_manual_stop_override"
            elif keep_on_after_target:
                charging_required_now = True
                preconditioning_required_now = True
                charging_reason = "ev_keep_charger_on_for_preconditioning"
            elif continue_current_charging and charging_required_now:
                charging_reason = "ev_continuous_charging_in_progress"
            elif low_price_charge and charging_required_now:
                charging_reason = "ev_low_price_charge_now"
            elif (
                current_allocation is not None
                and current_allocation.allocation_source == "daylight"
            ):
                charging_reason = "ev_daylight_lowest_cost_charge_now"
            elif charging_required_now:
                charging_reason = "ev_in_allocated_charging_window"
            else:
                charging_reason = "ev_outside_allocated_charging_window"
            if charging_required_now and current_slot is not None:
                current_slot.projected_ev_load_kw = max(current_slot.projected_ev_load_kw, charge_rate_kw)
            projected_load_kw_now = (
                max(float(current_slot.projected_ev_load_kw), 0.0)
                if charging_required_now and current_slot is not None
                else 0.0
            )
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
                        "target_soc_percent": target_soc,
                        "vehicle_target_soc_percent": requested_target_soc,
                        "ready_by": ready_by_text,
                        "ready_by_utc": ready_by.isoformat(),
                        "ready_by_timezone": context.local_timezone,
                        "earliest_start_utc": earliest_start.isoformat(),
                        "target_soc_source": "vehicle_sensor",
                        "required_charge_percent": round(required_charge_percent, 3),
                        "max_attainable_soc_percent": round(max_attainable_soc_percent, 3),
                        "soc_per_kwh": round(soc_per_kwh, 4),
                        "soc_per_kwh_source": soc_per_kwh_source,
                        "charge_calibration_sample_count": int(
                            self.ev_charge_calibration.get("sample_count", 0) or 0
                        ),
                        "continuous_charging": continuous_charging,
                        **(
                            {
                                "daylight_lowest_cost": {
                                    "enabled": True,
                                    **daylight_evidence,
                                }
                            }
                            if daylight_lowest_cost_enabled
                            else {}
                        ),
                        "continued_active_session": bool(
                            continue_current_charging and charging_required_now
                        ),
                        "keep_charger_on": preconditioning_required_now,
                        "projected_load_kw_now": projected_load_kw_now,
                        **(
                            {
                                "allocation_source_now": (
                                    current_allocation.allocation_source
                                    if current_allocation is not None
                                    else None
                                )
                            }
                            if daylight_lowest_cost_enabled
                            else {}
                        ),
                        "price_limit": float(self.options[CONF_EV_MAX_IMPORT_PRICE])
                        if bool(self.options.get(CONF_EV_PRICE_LIMIT_ENABLED, False))
                        else None,
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
                                **(
                                    {"allocation_source": allocation.allocation_source}
                                    if daylight_lowest_cost_enabled
                                    else {}
                                ),
                            }
                            for allocation in schedule.allocations
                        ],
                        "infeasible": schedule.infeasible or target_infeasible,
                    },
                    hard_constraints=["ready_by", "charger_connected"],
                    reason_codes=list(
                        dict.fromkeys(
                            [
                                charging_reason,
                                target_reason,
                                schedule.reason,
                                *(
                                    [str(daylight_evidence["reason"])]
                                    if daylight_lowest_cost_enabled
                                    else []
                                ),
                            ]
                        )
                    ),
                    expected_cost_delta=None,
                    confidence=confidence_from_context(context),
                )
            )
        enphase_action = self._enphase_action(context, execute_not_before, execute_not_after)
        if enphase_action is not None:
            actions.append(enphase_action)
        hvac_capability_blocked = _hvac_rollback_capability_unavailable(context)
        actions = [
            action
            for action in actions
            if action.kind == ActionKind.RELEASE_HVAC
            or (
                not (action.asset == ActionAsset.DAIKIN and hvac_capability_blocked)
                and (
                    _is_hvac_away_off_action(action)
                    or _action_meets_confidence_threshold(action, context, self.options)
                )
            )
        ]
        return sorted(actions, key=lambda action: _action_score(action, context, self.options)["score"], reverse=True)

    @staticmethod
    def _hvac_away_off_action(
        context: DecisionContext,
        execute_not_before: datetime,
        execute_not_after: datetime,
    ) -> PlanAction:
        """Return the conservative immediate away HVAC action."""
        return PlanAction(
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
        )

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
        arbitrage_profile = _enphase_profile_for_arbitrage(context)
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
            )
        return None

    def _hvac_lifecycle_actions(
        self,
        context: DecisionContext,
        execute_not_before: datetime,
        execute_not_after: datetime,
    ) -> list[PlanAction]:
        """Plan precondition, peak-coast, and release lifecycle actions."""
        active = dict(context.hvac_control or {})
        now = context.created_at
        low = context.occupied_temperature_low_c
        high = context.occupied_temperature_high_c
        current = context.current_hvac_temperature_c
        interval = timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
        active_override = any(
            override.kind == "manual_hvac" and (override.expires_at is None or now < override.expires_at)
            for override in context.active_overrides
        )
        if active_override:
            return [self._hvac_release_action(context, now, now + interval, "manual_hvac_override")] if active else []

        if active.get("required_evidence_lost"):
            return [
                self._hvac_release_action(
                    context,
                    now,
                    now + interval,
                    "hvac_required_evidence_lost",
                )
            ]

        if (
            active.get("phase") in {"preconditioning", "pre_peak_coast", "peak_coast"}
            and _confidence_rejection_reason(ActionAsset.DAIKIN, context, self.options) is not None
        ):
            return [
                self._hvac_release_action(
                    context,
                    now,
                    now + interval,
                    "hvac_confidence_below_threshold",
                )
            ]

        precondition_while_away = strict_bool(
            self.options.get(CONF_HVAC_PRECONDITION_WHILE_AWAY, False),
            default=False,
        )
        away_off_ownership_active = bool(
            active.get("phase") == "away_off"
            and context.occupancy_state == OccupancyState.AWAY
        )
        away_off_started_at = _datetime_value(active.get("started_at")) if away_off_ownership_active else None
        if (
            away_off_ownership_active
            and precondition_while_away
        ):
            # Away-off is stable planner ownership, not an active tariff
            # lifecycle. Keep it when no candidate exists, but allow a newly
            # qualifying away-preconditioning window to replace it directly.
            active = {}
        occupancy_allows_preconditioning = context.occupancy_state == OccupancyState.OCCUPIED or (
            context.occupancy_state == OccupancyState.AWAY and precondition_while_away
        )
        if low is None or high is None or current is None or not occupancy_allows_preconditioning:
            return (
                [self._hvac_release_action(context, now, now + interval, "hvac_required_evidence_lost")]
                if active
                else []
            )
        comfort_boundary_breached = float(current) <= float(low) or float(current) >= float(high)
        if comfort_boundary_breached and active:
            active_end = _datetime_value(active.get("period_end"))
            return [
                self._hvac_release_action(
                    context,
                    now,
                    now + interval,
                    "hvac_comfort_handoff",
                    released_until=active_end,
                )
            ]

        released_until = _datetime_value(active.get("released_until"))
        if released_until is not None and now < released_until:
            return []
        if released_until is not None and set(active) <= {"released_until"}:
            active = {}

        if active:
            period_start = _datetime_value(active.get("period_start"))
            period_end = _datetime_value(active.get("period_end"))
            baseline = _finite_number(active.get("baseline_price"))
            persisted_precondition_delta = _finite_number(active.get("precondition_min_price_delta"))
            persisted_suppression_delta = _finite_number(active.get("suppression_min_price_delta"))
            precondition_delta = (
                float(self.options[CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA])
                if persisted_precondition_delta is None
                else persisted_precondition_delta
            )
            suppression_delta = (
                float(self.options[CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA])
                if persisted_suppression_delta is None
                else persisted_suppression_delta
            )
            if period_start is None or period_end is None or baseline is None or now >= period_end:
                return [self._hvac_release_action(context, now, now + interval, "hvac_expensive_period_ended")]
            if now < period_start and not _persisted_hvac_period_qualifies(
                context,
                period_start,
                period_end,
                baseline,
                precondition_delta,
                suppression_delta,
            ):
                return [self._hvac_release_action(context, now, now + interval, "hvac_tariff_period_changed")]
            current_slot = context.slots[0] if context.slots else None
            if current_slot is None or current_slot.import_price is None:
                return [self._hvac_release_action(context, now, now + interval, "hvac_tariff_evidence_lost")]
            if now >= period_start and float(current_slot.import_price) < baseline + suppression_delta:
                return [self._hvac_release_action(context, now, now + interval, "hvac_expensive_period_ended")]
            mode = str(active.get("mode") or "")
            if mode not in {"heat", "cool"}:
                return [self._hvac_release_action(context, now, now + interval, "hvac_mode_evidence_lost")]
            if not _tariff_evidence_covers_period(context, period_end, interval):
                return [self._hvac_release_action(context, now, now + interval, "hvac_tariff_evidence_lost")]
            precondition_end = _datetime_value(active.get("precondition_end")) or period_start
            phase = (
                "peak_coast"
                if now >= period_start
                else "pre_peak_coast"
                if now >= precondition_end
                else "preconditioning"
            )
            target = (
                float(low if mode == "heat" else high)
                if phase in {"pre_peak_coast", "peak_coast"}
                else float(high if mode == "heat" else low)
            )
            precondition_target = float(high if mode == "heat" else low)
            coast_target = float(low if mode == "heat" else high)
            projected_precondition_end_temperature = _finite_number(
                active.get("projected_precondition_end_temperature")
            )
            if projected_precondition_end_temperature is None:
                projected_precondition_end_temperature = precondition_target
            if phase == "preconditioning":
                self._project_active_hvac_slots(
                    context,
                    phase=phase,
                    mode=mode,
                    active_until=precondition_end,
                    comfort_boundary=coast_target,
                )
                self._project_hvac_coast_slots(
                    context,
                    mode=mode,
                    coast_started_at=precondition_end,
                    active_until=period_end,
                    starting_temperature=projected_precondition_end_temperature,
                    comfort_boundary=coast_target,
                )
            else:
                self._project_active_hvac_slots(
                    context,
                    phase=phase,
                    mode=mode,
                    active_until=period_end,
                    comfort_boundary=coast_target,
                )
            common = {
                "period_start": period_start,
                "period_end": period_end,
                "precondition_end": precondition_end,
                "baseline_price": baseline,
                "precondition_min_price_delta": precondition_delta,
                "suppression_min_price_delta": suppression_delta,
                "precondition_target": _finite_number(active.get("precondition_target")) or precondition_target,
                "coast_target": _finite_number(active.get("coast_target")) or coast_target,
                "projected_precondition_end_temperature": projected_precondition_end_temperature,
            }
            actions = [
                self._hvac_control_action(
                    context,
                    now,
                    now + interval,
                    phase=phase,
                    mode=mode,
                    target=target,
                    **common,
                )
            ]
            if phase == "preconditioning" and precondition_end < period_start:
                actions.append(
                    self._hvac_control_action(
                        context,
                        precondition_end,
                        precondition_end + interval,
                        phase="pre_peak_coast",
                        mode=mode,
                        target=coast_target,
                        **common,
                    )
                )
            if phase in {"preconditioning", "pre_peak_coast"}:
                actions.append(
                    self._hvac_control_action(
                        context,
                        period_start,
                        period_start + interval,
                        phase="peak_coast",
                        mode=mode,
                        target=coast_target,
                        **common,
                    )
                )
            actions.append(
                self._hvac_release_action(
                    context,
                    period_end,
                    period_end + interval,
                    "hvac_expensive_period_ended",
                )
            )
            return actions

        earliest_start = None
        allow_immediate_start = False
        if context.occupancy_state == OccupancyState.AWAY:
            minimum_cycle = timedelta(minutes=int(self.options[CONF_HVAC_MIN_CYCLE_MINUTES]))
            if away_off_ownership_active:
                earliest_start = (away_off_started_at or now) + minimum_cycle
            else:
                # This plan will first acquire away-off ownership now, so a
                # later start must respect the rest period that action creates;
                # an immediate start does not issue the away-off action.
                earliest_start = now + minimum_cycle
                allow_immediate_start = True
        candidate = self._next_hvac_period(
            context,
            earliest_start=earliest_start,
            allow_immediate_start=allow_immediate_start,
        )
        if candidate is None:
            return []
        precondition_start = candidate["precondition_start"]
        precondition_end = candidate["precondition_end"]
        period_start = candidate["period_start"]
        period_end = candidate["period_end"]
        mode = candidate["mode"]
        precondition_target = float(high if mode == "heat" else low)
        coast_target = float(low if mode == "heat" else high)
        common = {
            "period_start": period_start,
            "period_end": period_end,
            "precondition_end": precondition_end,
            "baseline_price": candidate["baseline_price"],
            "precondition_min_price_delta": candidate["precondition_min_price_delta"],
            "suppression_min_price_delta": candidate["suppression_min_price_delta"],
            "precondition_target": precondition_target,
            "coast_target": coast_target,
            "projected_precondition_end_temperature": candidate[
                "projected_precondition_end_temperature"
            ],
        }
        actions = [
            self._hvac_control_action(
                context,
                precondition_start,
                precondition_start + interval,
                phase="preconditioning",
                mode=mode,
                target=precondition_target,
                **common,
            ),
        ]
        if precondition_end < period_start:
            actions.append(
                self._hvac_control_action(
                    context,
                    precondition_end,
                    precondition_end + interval,
                    phase="pre_peak_coast",
                    mode=mode,
                    target=coast_target,
                    **common,
                )
            )
        actions.extend(
            [
                self._hvac_control_action(
                    context,
                    period_start,
                    period_start + interval,
                    phase="peak_coast",
                    mode=mode,
                    target=coast_target,
                    **common,
                ),
                self._hvac_release_action(
                    context,
                    period_end,
                    period_end + interval,
                    "hvac_expensive_period_ended",
                ),
            ]
        )
        return actions

    def _next_hvac_period(
        self,
        context: DecisionContext,
        *,
        earliest_start: datetime | None = None,
        allow_immediate_start: bool = False,
    ) -> dict[str, Any] | None:
        """Return the earliest thermally feasible relative-price period."""
        if len(context.slots) < 2:
            return None
        low = float(context.occupied_temperature_low_c)
        high = float(context.occupied_temperature_high_c)
        current = float(context.current_hvac_temperature_c)
        interval_minutes = int(self.options[CONF_PLANNING_INTERVAL_MINUTES])
        lead_minutes = int(self.options[CONF_HVAC_PRECONDITION_LEAD_MINUTES])
        if lead_minutes <= 0:
            return None
        lead_slots = ceil(lead_minutes / interval_minutes)
        start_delta = max(
            float(self.options[CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA]),
            float(self.options[CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA]),
        )
        suppression_delta = float(self.options[CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA])
        index = 1
        while index < len(context.slots):
            slot = context.slots[index]
            if slot.import_price is None:
                index += 1
                continue
            window_start = max(0, index - lead_slots)
            priced_window = [
                (position, float(context.slots[position].import_price))
                for position in range(window_start, index)
                if context.slots[position].import_price is not None
            ]
            if not priced_window:
                index += 1
                continue
            baseline = min(price for _position, price in priced_window)
            if float(slot.import_price) < baseline + start_delta:
                index += 1
                continue
            end_index = index + 1
            while (
                end_index < len(context.slots)
                and context.slots[end_index].import_price is not None
                and float(context.slots[end_index].import_price) >= baseline + suppression_delta
            ):
                end_index += 1
            mode = _future_hvac_mode(context, slot, current, low, high)
            if mode is None:
                index = end_index
                continue
            target = high if mode == "heat" else low
            rate = thermal_active_temperature_rate_c_per_hour(self.thermal_model, mode)
            best_start: int | None = None
            best_required_slots: int | None = None
            best_projected_end_temperature = target
            best_baseline = baseline
            best_end_index = end_index
            best_cost: float | None = None
            passive_drift = _effective_passive_drift_c_per_hour(context, mode, self.thermal_model)
            for possible_start in range(window_start, index):
                possible_start_at = context.slots[possible_start].valid_at
                if (
                    earliest_start is not None
                    and possible_start_at < earliest_start
                    and not (allow_immediate_start and possible_start_at <= context.created_at)
                ):
                    continue
                if rate is None or rate <= 0:
                    required_slots = lead_slots
                else:
                    hours_to_start = max(
                        (context.slots[possible_start].valid_at - context.created_at).total_seconds() / 3600,
                        0.0,
                    )
                    projected_start_temperature = current
                    if passive_drift is not None:
                        projected_start_temperature += passive_drift * hours_to_start
                    if mode == "heat" and projected_start_temperature >= high:
                        continue
                    if mode == "cool" and projected_start_temperature <= low:
                        continue
                    required_slots = max(
                        1,
                        ceil(abs(target - projected_start_temperature) / rate / (interval_minutes / 60)),
                    )
                if possible_start + required_slots > index:
                    continue
                run = context.slots[possible_start : possible_start + required_slots]
                if any(item.import_price is None for item in run):
                    continue
                run_baseline = min(float(item.import_price) for item in run)
                if float(slot.import_price) < run_baseline + start_delta:
                    continue
                completion = possible_start + required_slots
                coast_hours = (index - completion) * interval_minutes / 60
                if coast_hours > 0:
                    drift = _effective_passive_drift_c_per_hour(context, mode, self.thermal_model)
                    available_coast = _thermal_coast_hours(
                        mode=mode,
                        target_temperature=target,
                        comfort_boundary=low if mode == "heat" else high,
                        passive_drift_c_per_hour=drift,
                    )
                    if available_coast is None or available_coast < coast_hours:
                        continue
                cost = sum(float(item.import_price) for item in run)
                if best_cost is None or cost < best_cost or (cost == best_cost and possible_start > best_start):
                    best_cost = cost
                    best_start = possible_start
                    best_required_slots = required_slots
                    best_baseline = run_baseline
                    best_end_index = index + 1
                    while (
                        best_end_index < len(context.slots)
                        and context.slots[best_end_index].import_price is not None
                        and float(context.slots[best_end_index].import_price)
                        >= run_baseline + suppression_delta
                    ):
                        best_end_index += 1
            if best_start is None:
                # A missed refresh or temporarily blocked command must not make
                # the remainder of an otherwise valuable preconditioning
                # window unusable. Start at the earliest allowed priced slot
                # and use all remaining time before the expensive period.
                for possible_start in range(window_start, index):
                    possible_start_at = context.slots[possible_start].valid_at
                    if (
                        earliest_start is not None
                        and possible_start_at < earliest_start
                        and not (allow_immediate_start and possible_start_at <= context.created_at)
                    ):
                        continue
                    hours_to_start = max(
                        (possible_start_at - context.created_at).total_seconds() / 3600,
                        0.0,
                    )
                    projected_start_temperature = current
                    if passive_drift is not None:
                        projected_start_temperature += passive_drift * hours_to_start
                    if mode == "heat" and projected_start_temperature >= high:
                        continue
                    if mode == "cool" and projected_start_temperature <= low:
                        continue
                    run = context.slots[possible_start:index]
                    if not run or any(item.import_price is None for item in run):
                        continue
                    tail_baseline = min(float(item.import_price) for item in run)
                    if float(slot.import_price) < tail_baseline + start_delta:
                        continue
                    tail_end_index = index + 1
                    while (
                        tail_end_index < len(context.slots)
                        and context.slots[tail_end_index].import_price is not None
                        and float(context.slots[tail_end_index].import_price)
                        >= tail_baseline + suppression_delta
                    ):
                        tail_end_index += 1
                    best_start = possible_start
                    best_required_slots = len(run)
                    best_baseline = tail_baseline
                    best_end_index = tail_end_index
                    if rate is None or rate <= 0:
                        # A shortened fallback without a learned active rate
                        # cannot claim any thermal reserve. Project peak load
                        # from the comfort boundary instead.
                        best_projected_end_temperature = low if mode == "heat" else high
                    else:
                        active_delta = rate * len(run) * interval_minutes / 60
                        best_projected_end_temperature = (
                            min(projected_start_temperature + active_delta, target)
                            if mode == "heat"
                            else max(projected_start_temperature - active_delta, target)
                        )
                    break
            if best_start is None or best_required_slots is None:
                index = end_index
                continue
            required_slots = best_required_slots
            baseline = best_baseline
            end_index = best_end_index
            projected_load = thermal_hvac_load_kw(self.thermal_model, HVAC_PRECONDITION_PROJECTED_LOAD_KW)
            for projected in context.slots[best_start : best_start + required_slots]:
                projected.projected_hvac_load_kw = max(projected.projected_hvac_load_kw, projected_load)
            period_end = (
                context.slots[end_index].valid_at
                if end_index < len(context.slots)
                else context.slots[-1].valid_at + timedelta(minutes=interval_minutes)
            )
            self._project_hvac_coast_slots(
                context,
                mode=mode,
                coast_started_at=context.slots[best_start + required_slots].valid_at,
                active_until=period_end,
                starting_temperature=best_projected_end_temperature,
                comfort_boundary=low if mode == "heat" else high,
            )
            return {
                "precondition_start": context.slots[best_start].valid_at,
                "precondition_end": (
                    context.slots[best_start + required_slots].valid_at
                    if best_start + required_slots < len(context.slots)
                    else context.slots[-1].valid_at + timedelta(minutes=interval_minutes)
                ),
                "period_start": slot.valid_at,
                "period_end": period_end,
                "baseline_price": round(baseline, 4),
                "precondition_min_price_delta": float(self.options[CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA]),
                "suppression_min_price_delta": suppression_delta,
                "mode": mode,
                "projected_precondition_end_temperature": best_projected_end_temperature,
            }
        return None

    def _hvac_control_action(
        self,
        context: DecisionContext,
        start: datetime,
        end: datetime,
        *,
        phase: str,
        mode: str,
        target: float,
        period_start: datetime,
        period_end: datetime,
        precondition_end: datetime,
        baseline_price: float,
        precondition_min_price_delta: float,
        suppression_min_price_delta: float,
        precondition_target: float | None = None,
        coast_target: float | None = None,
        projected_precondition_end_temperature: float | None = None,
    ) -> PlanAction:
        """Build one lifecycle HVAC control action."""
        thermal_summary = thermal_model_summary(self.thermal_model)
        return PlanAction(
            action_id=f"{context.plan_id}-hvac-{phase}",
            plan_id=context.plan_id,
            execute_not_before=start,
            execute_not_after=end,
            asset=ActionAsset.DAIKIN,
            kind=ActionKind.SET_HVAC,
            desired_state={
                "power": "on",
                "hvac_mode": mode,
                "target_temperature": float(target),
                "phase": phase,
                "period_start": period_start,
                "period_end": period_end,
                "precondition_end": precondition_end,
                "baseline_price": baseline_price,
                "precondition_min_price_delta": precondition_min_price_delta,
                "suppression_min_price_delta": suppression_min_price_delta,
                "mode": mode,
                "precondition_target": precondition_target if precondition_target is not None else target,
                "coast_target": coast_target if coast_target is not None else target,
                "projected_precondition_end_temperature": (
                    projected_precondition_end_temperature
                    if projected_precondition_end_temperature is not None
                    else target
                ),
                "suppress_automations": True,
                "enable_zones": True,
                "configured_zones_only": bool(
                    self.options.get(CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY, False)
                ),
                "controlled_zones": list(context.climate_zone_entities),
                "projected_hvac_load_kw": thermal_hvac_load_kw(self.thermal_model, HVAC_PRECONDITION_PROJECTED_LOAD_KW),
                "thermal_model_enabled": thermal_summary["enabled"],
                "thermal_model_sample_count": thermal_summary["active_sample_count"],
                "active_heat_rate_c_per_hour": thermal_summary["active_heat_rate_c_per_hour"],
                "active_cool_rate_c_per_hour": thermal_summary["active_cool_rate_c_per_hour"],
                "passive_indoor_drift_c_per_hour": thermal_summary["passive_indoor_drift_c_per_hour"],
            },
            hard_constraints=[
                (
                    "away_preconditioning_enabled"
                    if context.occupancy_state == OccupancyState.AWAY
                    else "occupied_comfort_within_bounds"
                ),
                "manual_hvac_override_inactive",
                "hvac_min_cycle",
            ],
            reason_codes=[f"hvac_{phase}"],
            expected_cost_delta=float(self.options[CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA]),
            confidence=confidence_from_context(context),
        )

    def _project_active_hvac_slots(
        self,
        context: DecisionContext,
        *,
        phase: str,
        mode: str,
        active_until: datetime,
        comfort_boundary: float,
    ) -> None:
        """Rebuild HVAC load projection from persisted lifecycle ownership."""
        projected_load = thermal_hvac_load_kw(
            self.thermal_model,
            HVAC_PRECONDITION_PROJECTED_LOAD_KW,
        )
        if phase == "preconditioning":
            for slot in context.slots:
                if slot.valid_at >= active_until:
                    break
                slot.projected_hvac_load_kw = max(
                    slot.projected_hvac_load_kw,
                    projected_load,
                )
            return
        self._project_hvac_coast_slots(
            context,
            mode=mode,
            coast_started_at=context.created_at,
            active_until=active_until,
            starting_temperature=float(context.current_hvac_temperature_c),
            comfort_boundary=comfort_boundary,
        )

    def _project_hvac_coast_slots(
        self,
        context: DecisionContext,
        *,
        mode: str,
        coast_started_at: datetime,
        active_until: datetime,
        starting_temperature: float,
        comfort_boundary: float,
    ) -> None:
        """Project maintenance load after thermal reserve is exhausted."""
        projected_load = thermal_hvac_load_kw(
            self.thermal_model,
            HVAC_PRECONDITION_PROJECTED_LOAD_KW,
        )
        coast_hours = _thermal_coast_hours(
            mode=mode,
            target_temperature=starting_temperature,
            comfort_boundary=comfort_boundary,
            passive_drift_c_per_hour=_effective_passive_drift_c_per_hour(
                context,
                mode,
                self.thermal_model,
            ),
        )
        for slot in context.slots:
            if slot.valid_at < coast_started_at:
                continue
            if slot.valid_at >= active_until:
                break
            elapsed_hours = max(
                (slot.valid_at - coast_started_at).total_seconds() / 3600,
                0.0,
            )
            if coast_hours is None or elapsed_hours >= coast_hours:
                slot.projected_hvac_load_kw = max(
                    slot.projected_hvac_load_kw,
                    projected_load,
                )

    @staticmethod
    def _hvac_release_action(
        context: DecisionContext,
        start: datetime,
        end: datetime,
        reason: str,
        *,
        released_until: datetime | None = None,
    ) -> PlanAction:
        """Build a safety release action."""
        return PlanAction(
            action_id=f"{context.plan_id}-hvac-release",
            plan_id=context.plan_id,
            execute_not_before=start,
            execute_not_after=end,
            asset=ActionAsset.DAIKIN,
            kind=ActionKind.RELEASE_HVAC,
            desired_state={"release_reason": reason, "released_until": released_until},
            hard_constraints=[],
            reason_codes=[reason],
            expected_cost_delta=0.0,
            confidence=confidence_from_context(context),
        )

    def _estimate_cost(self, context: DecisionContext) -> float | None:
        total = 0.0
        has_data = False
        interval_hours = timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES])).total_seconds() / 3600
        for slot in context.slots:
            if slot.import_price is None or slot.baseline_load_forecast_kw is None:
                continue
            load_kw = (
                slot.baseline_load_forecast_kw
                + slot.projected_ev_load_kw
                + slot.projected_hvac_load_kw
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
    """Return confidence capped by health and required forecast sources."""
    return _forecast_source_confidence(
        context,
        CONF_AMBER_IMPORT_PRICE,
        CONF_AMBER_EXPORT_PRICE,
        CONF_PV_FORECAST,
        CONF_HOUSEHOLD_LOAD,
    )


def _hvac_rollback_capability_unavailable(context: DecisionContext) -> bool:
    """Return whether HVAC takeover lacks a required rollback target."""
    return any(
        issue
        in {
            "main_climate_target_unavailable",
            "climate_zone_target_unavailable",
        }
        for issue in context.input_issues
    )


def _confidence_breakdown(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return confidence by planning subsystem."""
    base = confidence_from_context(context)
    health = confidence_from_health(context.input_health)
    issue_text = " ".join(
        issue
        for issue in context.input_issues
        if not issue.startswith("advisory_") and issue != "household_load_model_fallback_active"
    )
    breakdown = {
        "overall": base,
        "tariff": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_AMBER_IMPORT_PRICE, CONF_AMBER_EXPORT_PRICE),
            issue_text,
            ("amber_", "price_"),
        ),
        "solar": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_PV_FORECAST),
            issue_text,
            ("pv_forecast", "solar"),
        ),
        "load": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_HOUSEHOLD_LOAD),
            issue_text,
            ("baseline_load", "household_load", "load_forecast"),
        ),
        "climate": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_WEATHER),
            issue_text,
            ("daikin_", "climate_", "weather_"),
        ),
        "ev": _subsystem_confidence(health, issue_text, ("ev_",)),
        "enphase": _subsystem_confidence(health, issue_text, ("enphase_", "battery_soc")),
    }
    assets_with_actions = {str(action.asset) for action in actions}
    subsystem_breakdown = {key: value for key, value in breakdown.items() if key != "overall"}
    return {
        **breakdown,
        "action_assets": sorted(assets_with_actions),
        "limited_by": min(subsystem_breakdown, key=lambda key: subsystem_breakdown[key]),
    }


def _forecast_source_confidence(context: DecisionContext, *config_keys: str) -> float:
    """Return health-capped confidence for relevant configured forecast sources."""
    health = confidence_from_health(context.input_health)
    by_source = context.forecast_confidence_by_source
    if not by_source:
        return round(min(health, context.forecast_confidence), 4)
    relevant = [float(by_source[key]) for key in config_keys if key in by_source]
    return round(min([health, *relevant]), 4)


def _is_hvac_away_off_action(action: PlanAction) -> bool:
    """Return whether an action conservatively turns unoccupied HVAC off."""
    return bool(
        action.asset == ActionAsset.DAIKIN
        and action.kind == ActionKind.SET_HVAC
        and action.desired_state.get("hvac_mode") == "off"
        and "away_hvac_policy" in action.reason_codes
    )


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
    return asset_meets_confidence_threshold(action.asset, context, options)


def asset_meets_confidence_threshold(
    asset: ActionAsset,
    context: DecisionContext,
    options: Mapping[str, Any],
) -> bool:
    """Return whether an asset clears its relevant confidence thresholds."""
    breakdown = _confidence_breakdown(context, [])
    return _confidence_values_meet_threshold(asset, breakdown, options)


def plan_asset_meets_confidence_threshold(
    asset: ActionAsset,
    plan: EnergyPlan | Any,
    options: Mapping[str, Any],
) -> bool:
    """Return whether a current plan proves confidence eligibility for an asset."""
    breakdown = getattr(plan, "confidence_breakdown", None)
    if not isinstance(breakdown, Mapping):
        return False
    return _confidence_values_meet_threshold(asset, breakdown, options)


def confidence_eligible_control_areas(
    plan: EnergyPlan | Any,
    control_areas: list[str],
    options: Mapping[str, Any],
) -> list[str]:
    """Return control areas whose current plan clears asset confidence gates."""
    asset_by_area = {
        "ev": ActionAsset.EV,
        "hvac": ActionAsset.DAIKIN,
        "enphase": ActionAsset.ENPHASE,
    }
    return [
        area
        for area in control_areas
        if area in asset_by_area
        and plan_asset_meets_confidence_threshold(asset_by_area[area], plan, options)
    ]


def _confidence_values_meet_threshold(
    asset: ActionAsset,
    breakdown: Mapping[str, Any],
    options: Mapping[str, Any],
) -> bool:
    """Return whether confidence evidence clears every gate for an asset."""
    for key, option in _confidence_checks(asset):
        threshold = float(options.get(option, DEFAULT_OPTIONS.get(option, 0.0)) or 0.0) / 100.0
        actual = _finite_number(breakdown.get(key))
        if actual is None or actual < threshold:
            return False
    return True


def _confidence_checks(asset: ActionAsset) -> list[tuple[str, str]]:
    """Return confidence components and threshold settings for an asset."""
    checks = [("tariff", CONF_MIN_TARIFF_CONFIDENCE)]
    if asset == ActionAsset.DAIKIN:
        checks.extend([("climate", CONF_MIN_CLIMATE_CONFIDENCE), ("load", CONF_MIN_LOAD_CONFIDENCE)])
    elif asset == ActionAsset.EV:
        checks.extend([("ev", CONF_MIN_EV_CONFIDENCE), ("solar", CONF_MIN_SOLAR_CONFIDENCE)])
    elif asset == ActionAsset.ENPHASE:
        checks.extend([("enphase", CONF_MIN_ENPHASE_CONFIDENCE), ("solar", CONF_MIN_SOLAR_CONFIDENCE)])
    return checks


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
    )
    if _action_meets_confidence_threshold(fake_action, context, options):
        return None
    breakdown = _confidence_breakdown(context, [])
    failures = []
    for key, option in _confidence_checks(asset):
        actual = float(breakdown.get(key, 0.0) or 0.0)
        threshold = float(options.get(option, 0.0) or 0.0) / 100.0
        if actual < threshold:
            failures.append(f"{key} {round(actual * 100, 1)}% (requires {round(threshold * 100, 1)}%)")
    return "Skipped because confidence is below the configured threshold: " + ", ".join(failures) + "."


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
    result = {
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
    if action.asset == ActionAsset.DAIKIN:
        result["lifecycle"] = {
            key: action.desired_state[key]
            for key in (
                "phase",
                "period_start",
                "period_end",
                "precondition_end",
                "baseline_price",
                "precondition_min_price_delta",
                "suppression_min_price_delta",
                "mode",
                "precondition_target",
                "coast_target",
                "controlled_zones",
                "configured_zones_only",
                "release_reason",
            )
            if action.desired_state.get(key) is not None
        }
    return result


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
) -> list[dict[str, Any]]:
    """Return plain-English decisions that were considered but not selected."""
    rejected: list[dict[str, Any]] = []
    assets = {action.asset for action in actions}
    if ActionAsset.EV not in assets:
        rejected.append(_rejected_ev_decision(context, options))
    if ActionAsset.DAIKIN not in assets:
        rejected.append(_rejected_climate_decision(context, options))
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
) -> dict[str, Any]:
    """Return why climate control was not selected."""
    confidence_reason = _confidence_rejection_reason(ActionAsset.DAIKIN, context, options)
    if confidence_reason is not None:
        reason = confidence_reason
    elif context.occupancy_state != OccupancyState.OCCUPIED and not (
        context.occupancy_state == OccupancyState.AWAY
        and strict_bool(options.get(CONF_HVAC_PRECONDITION_WHILE_AWAY, False), default=False)
    ):
        reason = "Skipped comfort preconditioning because nobody is currently home."
    elif not _comfort_valid(context, float(options[CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT])):
        reason = "Skipped comfort preconditioning because climate comfort inputs are incomplete."
    elif not context.slots:
        reason = "Skipped comfort preconditioning because no tariff forecast slots are available."
    else:
        reason = (
            "No climate preconditioning was selected because the forecast contained no price window "
            "that both met the configured price difference and could be shifted within the thermal limits. "
            "This is a normal no-action planning outcome."
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


def _datetime_value(value: Any) -> datetime | None:
    """Return a timezone-aware lifecycle timestamp when valid."""
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _finite_number(value: Any) -> float | None:
    """Return a finite lifecycle scalar."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _persisted_hvac_period_qualifies(
    context: DecisionContext,
    period_start: datetime,
    period_end: datetime,
    baseline: float,
    precondition_delta: float,
    suppression_delta: float,
) -> bool:
    """Confirm that a future persisted peak still exists in valid tariff evidence."""
    if len(context.slots) < 2:
        return False
    interval = context.slots[1].valid_at - context.slots[0].valid_at
    if interval <= timedelta(0):
        return False
    period_slots = [
        slot for slot in context.slots if slot.valid_at < period_end and slot.valid_at + interval > period_start
    ]
    if (
        not period_slots
        or not period_slots[0].valid_at <= period_start < period_slots[0].valid_at + interval
        or any(
            right.valid_at != left.valid_at + interval
            for left, right in zip(period_slots, period_slots[1:], strict=False)
        )
        or period_slots[-1].valid_at + interval < period_end
    ):
        return False
    if period_slots[0].import_price is None or float(period_slots[0].import_price) < baseline + max(
        precondition_delta,
        suppression_delta,
    ):
        return False
    if any(
        slot.import_price is None or float(slot.import_price) < baseline + suppression_delta for slot in period_slots
    ):
        return False
    return True


def _tariff_evidence_covers_period(
    context: DecisionContext,
    period_end: datetime,
    interval: timedelta,
) -> bool:
    """Return whether contiguous priced tariff evidence covers the persisted period."""
    remaining_slots = [slot for slot in context.slots if slot.valid_at < period_end]
    if not remaining_slots or interval <= timedelta(0):
        return False
    if any(slot.import_price is None for slot in remaining_slots):
        return False
    if any(
        right.valid_at != left.valid_at + interval
        for left, right in zip(remaining_slots, remaining_slots[1:], strict=False)
    ):
        return False
    return remaining_slots[-1].valid_at + interval >= period_end


def _future_hvac_mode(
    context: DecisionContext,
    peak_slot: Any,
    current_temperature: float,
    low: float,
    high: float,
) -> str | None:
    """Infer heat or cool from peak weather before using current-state fallbacks."""
    future_outdoor = _finite_number(getattr(peak_slot, "outdoor_temperature_forecast_c", None))
    if future_outdoor is not None:
        if future_outdoor < low:
            return "heat"
        if future_outdoor > high:
            return "cool"
    current_mode = str(context.current_hvac_mode or "").lower()
    if current_mode in {"heat", "cool"}:
        return current_mode
    current_outdoor = _finite_number(context.current_outdoor_temperature_c)
    if current_outdoor is not None:
        if current_outdoor < low:
            return "heat"
        if current_outdoor > high:
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
    if context.climate_zone_entities:
        current["controlled_zones"] = list(context.climate_zone_entities)
    for key in (
        "phase",
        "period_start",
        "period_end",
        "precondition_end",
        "baseline_price",
        "precondition_min_price_delta",
        "suppression_min_price_delta",
        "mode",
        "precondition_target",
        "coast_target",
        "release_reason",
    ):
        if context.hvac_control.get(key) is not None:
            current[key] = context.hvac_control[key]
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
    if action.kind == ActionKind.RELEASE_HVAC:
        state = "released"
    if desired.get("phase"):
        state = str(desired["phase"])
    if desired.get("hvac_mode") == "off":
        state = "off"
    result: dict[str, Any] = {
        "state": state,
        "action": str(action.kind),
        "execute_not_before": action.execute_not_before.isoformat(),
        "execute_not_after": action.execute_not_after.isoformat(),
        "reason_codes": action.reason_codes[:4],
    }
    for key in (
        "hvac_mode",
        "target_temperature",
        "projected_hvac_load_kw",
        "suppress_automations",
        "phase",
        "period_start",
        "period_end",
        "precondition_end",
        "baseline_price",
        "precondition_min_price_delta",
        "suppression_min_price_delta",
        "precondition_target",
        "coast_target",
        "release_reason",
        "controlled_zones",
        "configured_zones_only",
    ):
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
        }
    }


def _add_energy_estimates(entry: dict[str, Any], interval_hours: float) -> None:
    """Add per-slot kWh estimates from power values on a timeline entry."""
    if "projected_hvac_load_kw" in entry:
        entry["estimated_energy_kwh"] = _energy_kwh(entry["projected_hvac_load_kw"], interval_hours)
    if "charge_kw" in entry:
        entry["estimated_energy_kwh"] = _energy_kwh(entry["charge_kw"], interval_hours)


def _merge_energy_estimates(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Sum per-slot kWh values into a compressed timeline segment."""
    for key in ("estimated_energy_kwh",):
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
            "state": "released" if action.kind == ActionKind.RELEASE_HVAC else "set_hvac",
            "action": str(action.kind),
            "reason_codes": action.reason_codes[:4],
        }
        if desired.get("suppress_automations"):
            entry["state"] = "suppressing_automation"
        if desired.get("phase"):
            entry["state"] = str(desired["phase"])
            entry["phase"] = desired["phase"]
        if desired.get("hvac_mode"):
            entry["hvac_mode"] = desired.get("hvac_mode")
            if desired.get("hvac_mode") == "off":
                entry["state"] = "off"
        if desired.get("target_temperature") is not None:
            entry["target_temperature"] = desired.get("target_temperature")
        for key in (
            "period_start",
            "period_end",
            "precondition_end",
            "baseline_price",
            "precondition_min_price_delta",
            "suppression_min_price_delta",
            "precondition_target",
            "coast_target",
            "controlled_zones",
            "release_reason",
        ):
            if desired.get(key) is not None:
                entry[key] = desired[key]
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

    entry: dict[str, Any] = {"state": "idle"}
    if planned_profile:
        entry["profile"] = planned_profile
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


def _daylight_preferred_ev_schedule(
    context: DecisionContext,
    *,
    standard_schedule: EVChargeSchedule,
    enabled: bool,
    current_soc_percent: float,
    target_soc_percent: float,
    ready_by: datetime,
    earliest_start: datetime,
    charge_rate_kw: float,
    soc_per_kwh: float,
    interval_minutes: int,
    carbon_weight: float,
    continuous: bool,
    continue_current: bool,
    force_current: bool,
    max_import_price: float | None,
) -> tuple[EVChargeSchedule, dict[str, Any]]:
    """Prefer a fully known daylight window without weakening ready-by behavior."""
    evidence: dict[str, Any] = {
        "applicable": False,
        "forecast_complete": False,
        "selected": False,
        "window_start_utc": None,
        "window_end_utc": None,
        "reason": "ev_daylight_lowest_cost_disabled",
    }
    if not enabled:
        return standard_schedule, evidence
    if continue_current:
        evidence["reason"] = "ev_daylight_deferred_active_session"
        return standard_schedule, evidence
    if force_current:
        evidence["reason"] = "ev_daylight_deferred_opportunistic_charge"
        return standard_schedule, evidence
    if current_soc_percent >= target_soc_percent:
        evidence["reason"] = "already_at_target"
        return standard_schedule, evidence

    window = next(
        (
            item
            for item in sorted(context.daylight_windows, key=lambda item: item.start)
            if item.end > context.created_at and item.end <= ready_by
        ),
        None,
    )
    if window is None:
        evidence["reason"] = "ev_daylight_window_not_before_ready_by"
        return standard_schedule, evidence

    evidence.update(
        {
            "applicable": True,
            "window_start_utc": window.start.isoformat(),
            "window_end_utc": window.end.isoformat(),
        }
    )
    interval = timedelta(minutes=interval_minutes)
    remaining_start = max(context.created_at, window.start)
    daylight_forecast_slots = sorted(
        (
            slot
            for slot in context.slots
            if remaining_start <= slot.valid_at < window.end
        ),
        key=lambda slot: slot.valid_at,
    )
    complete = bool(
        daylight_forecast_slots
        and daylight_forecast_slots[0].valid_at < remaining_start + interval
        and daylight_forecast_slots[-1].valid_at + interval >= window.end
        and all(
            right.valid_at - left.valid_at == interval
            for left, right in zip(
                daylight_forecast_slots,
                daylight_forecast_slots[1:],
                strict=False,
            )
        )
        and all(
            _daylight_cost_inputs_complete(slot)
            for slot in daylight_forecast_slots
        )
    )
    evidence["forecast_complete"] = complete
    if not complete:
        evidence["reason"] = "ev_daylight_forecast_incomplete"
        return standard_schedule, evidence

    daylight_slots = [
        slot
        for slot in daylight_forecast_slots
        if slot.valid_at + interval <= window.end
    ]
    daylight_schedule = allocate_least_cost_charging(
        daylight_slots,
        current_soc_percent=current_soc_percent,
        target_soc_percent=target_soc_percent,
        ready_by=window.end,
        charge_rate_kw=charge_rate_kw,
        soc_per_kwh=soc_per_kwh,
        interval_minutes=interval_minutes,
        carbon_weight=carbon_weight,
        continuous=continuous,
        max_import_price=max_import_price,
        allocation_source="daylight",
    )
    if not daylight_schedule.allocations:
        evidence["reason"] = "ev_daylight_no_eligible_charge"
        return standard_schedule, evidence
    if not daylight_schedule.infeasible:
        evidence.update({"selected": True, "reason": "ev_daylight_lowest_cost_selected"})
        return (
            EVChargeSchedule(
                allocations=daylight_schedule.allocations,
                target_soc_percent=daylight_schedule.target_soc_percent,
                scheduled_soc_percent=daylight_schedule.scheduled_soc_percent,
                required_charge_percent=daylight_schedule.required_charge_percent,
                infeasible=False,
                reason="daylight_lowest_effective_cost_slots",
            ),
            evidence,
        )
    if continuous:
        evidence["reason"] = "ev_daylight_continuous_capacity_insufficient"
        return standard_schedule, evidence

    daylight_times = {slot.valid_at for slot in daylight_slots}
    fallback_schedule = allocate_least_cost_charging(
        [slot for slot in context.slots if slot.valid_at not in daylight_times],
        current_soc_percent=daylight_schedule.scheduled_soc_percent,
        target_soc_percent=target_soc_percent,
        ready_by=ready_by,
        charge_rate_kw=charge_rate_kw,
        soc_per_kwh=soc_per_kwh,
        interval_minutes=interval_minutes,
        carbon_weight=carbon_weight,
        earliest_start=earliest_start,
        continuous=False,
        max_import_price=max_import_price,
        allocation_source="ready_by_fallback",
    )
    evidence.update(
        {
            "selected": True,
            "reason": "ev_daylight_lowest_cost_with_ready_by_fallback",
        }
    )
    return (
        EVChargeSchedule(
            allocations=[*daylight_schedule.allocations, *fallback_schedule.allocations],
            target_soc_percent=target_soc_percent,
            scheduled_soc_percent=fallback_schedule.scheduled_soc_percent,
            required_charge_percent=daylight_schedule.required_charge_percent,
            infeasible=fallback_schedule.infeasible,
            reason="daylight_lowest_effective_cost_with_ready_by_fallback",
        ),
        evidence,
    )


def _daylight_cost_inputs_complete(slot: Any) -> bool:
    """Return whether a slot can be compared using complete effective-cost evidence."""
    values = (
        slot.import_price,
        slot.export_price,
        slot.pv_forecast_kw,
        slot.baseline_load_forecast_kw,
    )
    return all(
        value is not None
        and not isinstance(value, bool)
        and isfinite(float(value))
        for value in values
    )


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


def _enphase_profile_for_arbitrage(context: DecisionContext) -> str | None:
    return context.enphase_self_consumption_profile or context.enphase_arbitrage_profile


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
