"""Pure HVAC lifecycle policy with explicit options and thermal model inputs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite
from typing import Any

from .const import (
    CONF_HVAC_MIN_CYCLE_MINUTES,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    CONF_HVAC_PRECONDITION_LEAD_MINUTES,
    CONF_HVAC_PRECONDITION_MIN_PRICE_DELTA,
    CONF_HVAC_PRECONDITION_WHILE_AWAY,
    CONF_HVAC_SUPPRESSION_MIN_PRICE_DELTA,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from .models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    OccupancyState,
    PlanAction,
)
from .planner_confidence import _confidence_rejection_reason, confidence_from_context
from .planner_values import finite_float as _finite_number
from .safety import strict_bool
from .thermal_model import (
    thermal_active_temperature_rate_c_per_hour,
    thermal_hvac_load_kw,
    thermal_model_summary,
)

HVAC_PRECONDITION_PROJECTED_LOAD_KW = 1.0
THERMAL_SHIFT_FALLBACK_DRIFT_C_PER_HOUR = 0.5


class HVACPlanningPolicy:
    """Calculate climate actions and projections without coordinator or Store state."""

    def __init__(self, options: Mapping[str, Any], thermal_model: Mapping[str, Any]) -> None:
        self.options = options
        self.thermal_model = dict(thermal_model)

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
            active.get("phase") == "away_off" and context.occupancy_state == OccupancyState.AWAY
        )
        away_off_started_at = _datetime_value(active.get("started_at")) if away_off_ownership_active else None
        if away_off_ownership_active and precondition_while_away:
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
            common: dict[str, Any] = {
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
            "projected_precondition_end_temperature": candidate["projected_precondition_end_temperature"],
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
        low = context.occupied_temperature_low_c
        high = context.occupied_temperature_high_c
        current = context.current_hvac_temperature_c
        if low is None or high is None or current is None:
            return None
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
                (position, _known_float(context.slots[position].import_price))
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
                and _known_float(context.slots[end_index].import_price) >= baseline + suppression_delta
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
                run_baseline = min(_known_float(item.import_price) for item in run)
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
                cost = sum(_known_float(item.import_price) for item in run)
                if (
                    best_cost is None
                    or cost < best_cost
                    or (cost == best_cost and (best_start is None or possible_start > best_start))
                ):
                    best_cost = cost
                    best_start = possible_start
                    best_required_slots = required_slots
                    best_baseline = run_baseline
                    best_end_index = index + 1
                    while (
                        best_end_index < len(context.slots)
                        and context.slots[best_end_index].import_price is not None
                        and _known_float(context.slots[best_end_index].import_price) >= run_baseline + suppression_delta
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
                    tail_baseline = min(_known_float(item.import_price) for item in run)
                    if float(slot.import_price) < tail_baseline + start_delta:
                        continue
                    tail_end_index = index + 1
                    while (
                        tail_end_index < len(context.slots)
                        and context.slots[tail_end_index].import_price is not None
                        and _known_float(context.slots[tail_end_index].import_price)
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
                "configured_zones_only": bool(self.options.get(CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY, False)),
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
        starting_temperature = context.current_hvac_temperature_c
        if starting_temperature is None:
            return
        self._project_hvac_coast_slots(
            context,
            mode=mode,
            coast_started_at=context.created_at,
            active_until=active_until,
            starting_temperature=starting_temperature,
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


def _known_float(value: float | None) -> float:
    """Return a float after the caller has established it is present."""
    assert value is not None
    return float(value)


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
