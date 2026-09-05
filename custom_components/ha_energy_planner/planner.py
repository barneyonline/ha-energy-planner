"""Deterministic dry-run planner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from .const import (
    CONF_BATTERY_MIN_SOC_PERCENT,
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
    CONF_HVAC_PRECONDITION_WHILE_AWAY,
    CONF_OCCUPIED_TEMP_TOLERANCE_PERCENT,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PRIORITY_WEIGHTS,
)
from .ev import allocate_least_cost_charging, effective_ev_soc_per_kwh
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
from .planner_battery import _arbitrage_value, _enphase_profile_for_arbitrage, _marginal_budget_summary
from .planner_confidence import (
    _action_meets_confidence_threshold,
    _confidence_breakdown,
    _confidence_rejection_reason,
    _hvac_rollback_capability_unavailable,
    _is_hvac_away_off_action,
    confidence_from_context,
)
from .planner_confidence import (
    asset_meets_confidence_threshold as asset_meets_confidence_threshold,
)
from .planner_confidence import (
    confidence_eligible_control_areas as confidence_eligible_control_areas,
)
from .planner_confidence import (
    confidence_from_health as confidence_from_health,
)
from .planner_confidence import (
    plan_asset_meets_confidence_threshold as plan_asset_meets_confidence_threshold,
)
from .planner_ev import _daylight_preferred_ev_schedule, _ev_earliest_start, _next_ready_by
from .planner_hvac import HVACPlanningPolicy
from .planner_presentation import _display_text, _timeline_card_rows, build_device_plans
from .safety import strict_bool


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
        self.hvac_policy = HVACPlanningPolicy(options, self.thermal_model)
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
        device_plans = build_device_plans(context, actions, int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
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
            status=("current" if context.input_health in {InputHealth.HEALTHY, InputHealth.DEGRADED} else "unsafe"),
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
        if mode not in {PlannerMode.ACTIVE_HEALTHY, PlannerMode.DRY_RUN} or context.input_health not in {
            InputHealth.HEALTHY,
            InputHealth.DEGRADED,
        }:
            if (
                context.hvac_control.get("phase") == "away_off"
                and context.occupancy_state == OccupancyState.AWAY
                and away_off_release_reason is None
            ):
                return []
            if context.hvac_control:
                interval = timedelta(minutes=int(self.options[CONF_PLANNING_INTERVAL_MINUTES]))
                return [
                    self.hvac_policy._hvac_release_action(
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
                    self.hvac_policy._hvac_lifecycle_actions(
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
                    self.hvac_policy._hvac_lifecycle_actions(
                        context,
                        execute_not_before,
                        execute_not_after,
                    )
                )
            else:
                actions.append(
                    self.hvac_policy._hvac_release_action(
                        context,
                        execute_not_before,
                        execute_not_after,
                        away_off_release_reason or "hvac_required_evidence_lost",
                    )
                )
        elif context.occupancy_state == OccupancyState.AWAY:
            away_actions = (
                self.hvac_policy._hvac_lifecycle_actions(
                    context,
                    execute_not_before,
                    execute_not_after,
                )
                if away_preconditioning_enabled
                else []
            )
            preconditioning_action = next(
                (action for action in away_actions if action.desired_state.get("phase") == "preconditioning"),
                None,
            )
            if preconditioning_action is not None:
                if preconditioning_action.execute_not_before > execute_not_before:
                    actions.append(
                        self.hvac_policy._hvac_away_off_action(context, execute_not_before, execute_not_after)
                    )
                actions.extend(away_actions)
            else:
                actions.append(self.hvac_policy._hvac_away_off_action(context, execute_not_before, execute_not_after))
        else:
            actions.extend(
                self.hvac_policy._hvac_lifecycle_actions(
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
            daylight_lowest_cost_enabled = bool(self.options.get(CONF_EV_DAYLIGHT_LOWEST_COST_CHARGING_ENABLED, False))
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
                context.current_ev_soc_percent + available_charge_hours * charge_rate_kw * soc_per_kwh,
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
                keep_charger_on and not target_infeasible and context.current_ev_soc_percent >= target_soc
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
            elif current_allocation is not None and current_allocation.allocation_source == "daylight":
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
                        "charge_calibration_sample_count": int(self.ev_charge_calibration.get("sample_count", 0) or 0),
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
                        "continued_active_session": bool(continue_current_charging and charging_required_now),
                        "keep_charger_on": preconditioning_required_now,
                        "projected_load_kw_now": projected_load_kw_now,
                        **(
                            {
                                "allocation_source_now": (
                                    current_allocation.allocation_source if current_allocation is not None else None
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
                                *([str(daylight_evidence["reason"])] if daylight_lowest_cost_enabled else []),
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


def _asset_label(asset: ActionAsset) -> str:
    """Return a user-facing asset label."""
    labels = {
        ActionAsset.DAIKIN: "Climate",
        ActionAsset.ENPHASE: "Enphase",
        ActionAsset.EV: "EV",
    }
    return labels.get(asset, _display_text(asset))


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
