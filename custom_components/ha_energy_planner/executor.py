"""Execution gate for Energy Planner."""

from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from types import SimpleNamespace
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_BYPASS_SAFETY_GATES,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_COMMAND_RATE_LIMIT_SECONDS,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CHARGING,
    CONF_EV_CONFIRMATION_RETRIES,
    CONF_EV_CONFIRMATION_TIMEOUT_SECONDS,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_MAX_DAILY_CLIMATE_ACTIONS,
    CONF_MAX_DAILY_ENPHASE_ACTIONS,
    CONF_MAX_DAILY_EV_ACTIONS,
    CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
    DOMAIN,
    EV_RESERVATION_EXTERNAL_BASELINE,
    EV_RESERVATION_RETAIN_WHEN_UNLOADED,
    STATE_UNKNOWN_VALUES,
)
from .constraints import ConstraintValidator, _projected_grid_flows_kw
from .discovery import CapabilityDiscovery
from .enphase_adapter import EnphaseProfileAdapter
from .ev_adapter import EVCommandResult, EVSmartChargingAdapter
from .hvac_adapter import DaikinHVACAdapter
from .models import (
    ActionAsset,
    ActionKind,
    ActionOutcome,
    DecisionContext,
    EnergyPlan,
    InputHealth,
    OutcomeResult,
    PlanAction,
    PlannerMode,
)
from .notifications import cancel_deferred_persistent_notification, defer_persistent_notification
from .ownership import OwnershipState
from .planner import asset_meets_confidence_threshold
from .preflight import production_evidence_fingerprint
from .safety import (
    DRY_RUN_READY_CYCLES_REQUIRED,
    control_pause_reason,
    parse_production_state,
    strict_bool,
)
from .storage import PlannerStore

_PLAN_UNSAFE_NOTIFICATION_ID = "ha_energy_planner_plan_unsafe"
_GRID_LIMIT_NOTIFICATION_ID = "ha_energy_planner_grid_limit_fallback"
_RETIRED_FALLBACK_NOTIFICATION_ID = "ha_energy_planner_haeo_fallback"
_EV_INFEASIBLE_NOTIFICATION_ID = "ha_energy_planner_ev_infeasible"
_STARTUP_RECOVERY_NOTIFICATION_ID = "ha_energy_planner_startup_recovery"
_PLAN_FALLBACK_NOTIFICATION_IDS = (
    _PLAN_UNSAFE_NOTIFICATION_ID,
    _GRID_LIMIT_NOTIFICATION_ID,
    _EV_INFEASIBLE_NOTIFICATION_ID,
)
PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE = timedelta(minutes=5)
ACTION_BACKOFF_DURATION = timedelta(minutes=10)
EV_SAFETY_STOP_RETRY_WINDOW = timedelta(hours=24)
MAX_EV_SAFETY_STOP_ATTEMPTS_PER_24H = 3
CONFLICT_DETECTION_WINDOW = timedelta(minutes=2)
_MISSING = object()
_EV_COMMAND_ENTITY_OWNERSHIP_KEY = "ev_smart_charging_command_entity_id"
_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY = "ev_smart_charging_control_topology"
_EV_SAFETY_STOP_FAILED_AT_KEY = "ev_safety_stop_failed_at"
_EV_SAFETY_STOP_BLOCKED_UNTIL_KEY = "ev_safety_stop_blocked_until"
_EV_GRID_SHEDDING_CLAIM_KEY = "ev_grid_shedding_entry_id"
_HVAC_MAIN_STATE_OWNERSHIP_KEY = "main_state"
_PENDING_HVAC_MANUAL_OVERRIDE_KEY = "manual_override_detected"
_PENDING_HVAC_MANUAL_ZONE_IDS_KEY = "manual_zone_entity_ids"
_EV_CONTROL_TOPOLOGY_KEYS = (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_CHARGING,
)
_EV_SAFETY_PLAN_ISSUES = frozenset(
    {
        "grid_import_limit_exceeded",
        "ev_min_above_ev_max",
    }
)


class Executor:
    """Evaluate due actions behind the planner safety gate."""

    def __init__(
        self,
        store: PlannerStore,
        *,
        hass: Any | None = None,
        entry_data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        notification_grace_until: datetime | None = None,
        entry_id: str | None = None,
        entry_title: str | None = None,
    ) -> None:
        """Initialize executor."""
        self.store = store
        self.hass = hass
        self.entry_data = entry_data or {}
        self.options = options or {}
        self.notification_grace_until = notification_grace_until
        self.entry_id = entry_id
        self.entry_title = entry_title
        self.pending_hvac_desired_state: dict[str, Any] | None = None
        self._ev_safety_stop_attempted_plan_id: str | None = None
        self._plan_fallback_notification_signatures: dict[str, tuple[str, str]] = {}

    def mark_pending_hvac_manual_override(self) -> bool:
        """Synchronously mark an in-flight HVAC command as user-superseded."""
        pending = self.pending_hvac_desired_state
        if pending is None:
            return False
        pending[_PENDING_HVAC_MANUAL_OVERRIDE_KEY] = True
        return True

    def mark_pending_hvac_zone_manual_override(self, entity_id: str) -> bool:
        """Synchronously mark an in-flight zone command as user-superseded."""
        pending = self.pending_hvac_desired_state
        if pending is None or not entity_id:
            return False
        entity_ids = {
            str(item)
            for item in pending.get(_PENDING_HVAC_MANUAL_ZONE_IDS_KEY, [])
            if str(item)
        }
        entity_ids.add(entity_id)
        pending[_PENDING_HVAC_MANUAL_ZONE_IDS_KEY] = sorted(entity_ids)
        return True

    async def async_manual_ev_charging(
        self,
        enabled: bool,
        context: DecisionContext | None,
    ) -> EVCommandResult:
        """Apply an explicit EV command with shared capacity and recovery tracking."""
        now = dt_util.utcnow()
        action = SimpleNamespace(
            action_id="manual_ev_start" if enabled else "manual_ev_stop",
            asset=ActionAsset.EV,
            kind=ActionKind.EV_START if enabled else ActionKind.EV_STOP,
            desired_state={
                "charging_required_now": enabled,
                "projected_load_kw_now": (
                    _positive_float(self.options.get(CONF_EV_CHARGE_RATE_KW)) if enabled else 0.0
                ),
            },
        )
        plan_id = getattr(context, "plan_id", "manual") if context is not None else "manual"
        if enabled:
            gate_reason = _pause_rejection_reason(
                self.store.data.get("control_pause"),
                action,
                now,
            ) or self._rate_limit_reason(action, now)
            ownership = self.store.data.get("ownership")
            if (
                gate_reason is None
                and self._has_ev_grid_reservation()
                and not (isinstance(ownership, dict) and ownership.get("ev_smart_charging_state"))
            ):
                gate_reason = "ev_recovery_stop_required"
            if gate_reason is not None:
                result = EVCommandResult(False, gate_reason, {}, {})
                await self._async_record_manual_ev_outcome(
                    action,
                    result,
                    now,
                    plan_id=plan_id,
                    rejected=True,
                )
                return result
            context_reason = self._manual_ev_grid_context_reason(context, now)
            if context_reason is not None:
                result = EVCommandResult(False, context_reason, {}, {})
                await self._async_record_manual_ev_outcome(
                    action,
                    result,
                    now,
                    plan_id=plan_id,
                    rejected=True,
                )
                return result
            if self.hass is not None:
                capability_issues = (
                    CapabilityDiscovery(
                        self.hass,
                        self.entry_data,
                    )
                    .inspect()
                    .ev.issues
                )
                if capability_issues:
                    result = EVCommandResult(
                        False,
                        ",".join(capability_issues),
                        {},
                        {},
                    )
                    await self._async_record_manual_ev_outcome(
                        action,
                        result,
                        now,
                        plan_id=plan_id,
                        rejected=True,
                    )
                    return result
        reservation_reason, previous_reservation = self._reserve_ev_grid_capacity(action, context, now)
        if reservation_reason is not None:
            result = EVCommandResult(False, reservation_reason, {}, {})
            await self._async_record_manual_ev_outcome(
                action,
                result,
                now,
                plan_id=plan_id,
                rejected=True,
            )
            return result
        ev_entry_data = self._ev_entry_data_for_action(action)
        provisional_ownership = False
        if enabled:
            # Persist the provisional claim before the service boundary. If Home
            # Assistant stops after the charger accepts the command, startup can
            # then conservatively stop it even before ownership was recorded.
            await self.async_persist_ev_grid_reservation()
            provisional_ownership = await self._async_save_provisional_ev_ownership(
                action,
                ev_entry_data,
            )
            await self._async_flush_provisional_state()
        owned_manual_stop = bool(
            not enabled and (self._owned_ev_control_topology() is not None or isinstance(previous_reservation, dict))
        )
        result = await EVSmartChargingAdapter(
            self.hass,
            ev_entry_data,
            confirmation_timeout_seconds=float(self.options.get(CONF_EV_CONFIRMATION_TIMEOUT_SECONDS, 30)),
            confirmation_retries=int(self.options.get(CONF_EV_CONFIRMATION_RETRIES, 1)),
        ).async_set_charging(enabled)
        if not enabled:
            result = _normalized_ev_stop_result(
                result,
                require_safe=owned_manual_stop,
            )
        self._reconcile_ev_grid_reservation(action, result, previous_reservation)
        await self.async_persist_ev_grid_reservation()
        if result.command_sent:
            await self._async_record_command_attempt(action, now)
        if not result.applied:
            await self._async_pause_asset_control(
                ActionAsset.EV,
                now,
                result.reason,
                ACTION_BACKOFF_DURATION,
            )
        if enabled and (result.command_sent or result.applied) and result.rollback_succeeded is not True:
            ownership = dict(self.store.data.get("ownership", {}))
            ownership.setdefault("ev_smart_charging_state", result.pre_state)
            ownership.setdefault(
                _EV_COMMAND_ENTITY_OWNERSHIP_KEY,
                _ev_command_entity_for_action(action, ev_entry_data),
            )
            ownership.setdefault(
                _EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY,
                _ev_control_topology(ev_entry_data),
            )
            await self.store.async_save_ownership(ownership)
        elif not enabled and _ev_result_proves_safe(result):
            ownership = dict(self.store.data.get("ownership", {}))
            ownership.pop("ev_smart_charging_state", None)
            ownership.pop(_EV_COMMAND_ENTITY_OWNERSHIP_KEY, None)
            ownership.pop(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY, None)
            await self.store.async_save_ownership(ownership)
        elif enabled and provisional_ownership and not self._has_ev_grid_reservation():
            await self._async_clear_provisional_ev_ownership()
        await self._async_record_manual_ev_outcome(
            action,
            result,
            now,
            plan_id=plan_id,
            ev_entry_data=ev_entry_data,
        )
        return result

    async def _async_record_manual_ev_outcome(
        self,
        action: Any,
        result: EVCommandResult,
        now: datetime,
        *,
        plan_id: str,
        rejected: bool = False,
        ev_entry_data: dict[str, Any] | None = None,
    ) -> None:
        """Persist one explicit EV command outcome through the normal audit path."""
        no_change = result.reason == "already_in_desired_state"
        outcome_result = (
            OutcomeResult.REJECTED
            if rejected
            else OutcomeResult.SKIPPED
            if no_change
            else OutcomeResult.APPLIED
            if result.applied
            else OutcomeResult.FAILED
        )
        await self.store.async_add_outcome(
            self._action_outcome(
                action,
                now,
                result=outcome_result,
                reason=result.reason,
                pre_state=result.pre_state,
                post_state=result.post_state,
                plan_id=plan_id,
                ev_entry_data=ev_entry_data,
            )
        )

    def _manual_ev_grid_context_reason(
        self,
        context: DecisionContext | None,
        now: datetime,
    ) -> str | None:
        """Reject manual starts unless their grid evidence is current and safe."""
        if context is None or not context.slots:
            return "ev_grid_projection_unavailable"
        projected_import_kw, _projected_export_kw = _projected_grid_flows_kw(context.slots[0])
        if projected_import_kw is None:
            return "ev_grid_projection_unavailable"
        if getattr(context, "input_health", None) not in {
            InputHealth.HEALTHY,
            InputHealth.DEGRADED,
        }:
            return "ev_grid_projection_unsafe"
        created_at = getattr(context, "created_at", None)
        if not isinstance(created_at, datetime) or created_at.tzinfo is None:
            return "ev_grid_projection_stale"
        decision_ttl_minutes = max(
            int(self.options.get(CONF_PLANNING_INTERVAL_MINUTES, 5)),
            1,
        )
        age = now - created_at
        if age < timedelta(0) or age > timedelta(minutes=decision_ttl_minutes):
            return "ev_grid_projection_stale"
        return None

    async def async_evaluate(
        self,
        plan: EnergyPlan,
        context: DecisionContext | None = None,
    ) -> PlanAction | None:
        """Audit why an action was not executed."""
        action = plan.next_action
        safety_stop = self._owned_ev_safety_stop(plan, context)
        if safety_stop is not None and self._ev_safety_stop_attempted_plan_id != plan.plan_id:
            self._ev_safety_stop_attempted_plan_id = plan.plan_id
            await self.async_evaluate(replace(plan, actions=[safety_stop]), context)
            if action is None or action.asset == ActionAsset.EV:
                return
        now = dt_util.utcnow()
        due_hvac_releases = [
            candidate
            for candidate in plan.actions
            if candidate.asset == ActionAsset.DAIKIN
            and candidate.kind == ActionKind.RELEASE_HVAC
            and candidate.execute_not_before <= now <= candidate.execute_not_after
        ]
        if due_hvac_releases:
            action = min(due_hvac_releases, key=lambda candidate: candidate.execute_not_before)
        else:
            due_hvac_continuation = next(
                (
                    candidate
                    for candidate in plan.actions
                    if candidate.asset == ActionAsset.DAIKIN
                    and candidate.desired_state.get("phase")
                    in {"preconditioning", "pre_peak_coast", "peak_coast"}
                    and candidate.execute_not_before <= now <= candidate.execute_not_after
                ),
                None,
            )
            has_hvac_ownership = bool(
                dict(self.store.data.get("ownership", {})).get("hvac_control")
            )
            if due_hvac_continuation is not None and has_hvac_ownership:
                continuation_block = self._control_rejection_reason(due_hvac_continuation, now)
                if self._rejection_reason(plan) is None and continuation_block is not None:
                    await self.async_release_hvac_control(
                        f"hvac_continuation_blocked_{continuation_block}",
                        plan_id=plan.plan_id,
                    )
                    return due_hvac_continuation
        if action is None:
            return
        safety_ev_stop = _ev_action_is_safety_stop(action)
        owned_safety_stop = _ev_action_is_owned_safety_stop(action)
        if not owned_safety_stop and (now < action.execute_not_before or now > action.execute_not_after):
            return
        if action.asset == ActionAsset.DAIKIN and action.kind == ActionKind.RELEASE_HVAC:
            await self.async_release_hvac_control(
                str(action.desired_state.get("release_reason") or "planned_hvac_release"),
                plan_id=plan.plan_id,
                action=action,
            )
            return action
        # Dry run is an intentional observation mode, not a safety rejection.
        # Keep plan-level constraint findings on the plan itself and emit one
        # unambiguous skipped action outcome here. Planner-owned safety stops
        # remain executable because explicit manual controls can create
        # ownership while automated planning is disabled or in dry-run.
        if not owned_safety_stop and (plan.mode == PlannerMode.DRY_RUN or bool(self.options.get("dry_run", False))):
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=OutcomeResult.SKIPPED,
                    reason="dry_run",
                    pre_state={},
                    post_state={},
                    plan_id=plan.plan_id,
                )
            )
            return
        ownership = self._ownership_from_store()
        if context is not None and self.options and not safety_ev_stop:
            violations = ConstraintValidator(self.options).validate_action(
                context,
                plan,
                action,
                now=now,
                ownership=ownership,
            )
            if violations:
                await self.store.async_add_outcome(
                    self._action_outcome(
                        action,
                        now,
                        result=OutcomeResult.REJECTED,
                        reason=",".join(violations),
                        pre_state={},
                        post_state={},
                        plan_id=plan.plan_id,
                    )
                )
                return
        if self.hass is not None:
            await self._async_notify_ev_infeasible(action)
            capability_entry_data = (
                self._ev_entry_data_for_action(action) if action.asset == ActionAsset.EV else self.entry_data
            )
            capability = (
                CapabilityDiscovery(
                    self.hass,
                    capability_entry_data,
                )
                .inspect()
                .for_asset(action.asset)
            )
            capability_issues = list(capability.issues)
            keep_on_action = bool(action.asset == ActionAsset.EV and action.desired_state.get("keep_charger_on"))
            if safety_ev_stop or keep_on_action:
                capability_issues = [issue for issue in capability_issues if not issue.startswith("ev_start_control_")]
            if keep_on_action:
                persistent_control = capability.details.get(
                    "persistent_control",
                    {},
                )
                if persistent_control.get("stateful") is not True:
                    capability_issues.append("ev_keep_on_requires_stateful_control")
                elif persistent_control.get("available") is not True:
                    capability_issues.append("ev_keep_on_control_unavailable")
            if capability_issues:
                await self.store.async_add_outcome(
                    self._action_outcome(
                        action,
                        now,
                        result=OutcomeResult.REJECTED,
                        reason=",".join(capability_issues),
                        pre_state={},
                        post_state={},
                        plan_id=plan.plan_id,
                    )
                )
                return
        reason = self._rejection_reason(plan)
        if safety_ev_stop and reason == "input_health_degraded":
            reason = None
        elif owned_safety_stop and reason == "planner_disabled":
            reason = None
        conflict_reason = self._observed_conflict_reason(action, now)
        if reason is None and conflict_reason is not None:
            await self._async_pause_asset_control(action.asset, now, conflict_reason, CONFLICT_DETECTION_WINDOW)
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=OutcomeResult.REJECTED,
                    reason=conflict_reason,
                    pre_state={},
                    post_state={},
                    plan_id=plan.plan_id,
                )
            )
            return
        hvac_continuation = bool(
            action.asset == ActionAsset.DAIKIN
            and action.desired_state.get("phase") in {"preconditioning", "pre_peak_coast", "peak_coast"}
            and dict(self.store.data.get("ownership", {})).get("hvac_control")
        )
        control_reason = self._control_rejection_reason(action, now)
        if reason is None and control_reason is not None:
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=OutcomeResult.REJECTED,
                    reason=control_reason,
                    pre_state={},
                    post_state={},
                    plan_id=plan.plan_id,
                )
            )
            return
        rate_limit_reason = (
            None if _ev_action_is_safety_stop(action) or hvac_continuation else self._rate_limit_reason(action, now)
        )
        if reason is None and rate_limit_reason is not None:
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=OutcomeResult.REJECTED,
                    reason=rate_limit_reason,
                    pre_state={},
                    post_state={},
                    plan_id=plan.plan_id,
                )
            )
            return
        if reason is None and action.asset == ActionAsset.EV and self.hass is not None:
            reservation_reason, previous_reservation = self._reserve_ev_grid_capacity(action, context, now)
            if reservation_reason is not None:
                await self.store.async_add_outcome(
                    self._action_outcome(
                        action,
                        now,
                        result=OutcomeResult.REJECTED,
                        reason=reservation_reason,
                        pre_state={},
                        post_state={},
                        plan_id=plan.plan_id,
                    )
                )
                return
            ev_entry_data = self._ev_entry_data_for_action(action)
            provisional_ownership = False
            if _ev_action_wants_power(action):
                # The device call below is a crash boundary. Persist possible
                # load first so a restart cannot forget an accepted EV start.
                await self.async_persist_ev_grid_reservation()
                provisional_ownership = await self._async_save_provisional_ev_ownership(
                    action,
                    ev_entry_data,
                )
                await self._async_flush_provisional_state()
            result = await EVSmartChargingAdapter(
                self.hass,
                ev_entry_data,
                confirmation_timeout_seconds=float(self.options.get(CONF_EV_CONFIRMATION_TIMEOUT_SECONDS, 30)),
                confirmation_retries=int(self.options.get(CONF_EV_CONFIRMATION_RETRIES, 1)),
            ).async_execute(action)
            no_change = result.reason == "already_in_desired_state"
            safe_stop_confirmed = _ev_result_proves_safe(result)
            stored_ownership = self.store.data.get("ownership")
            planner_owned_stop = bool(
                safety_ev_stop
                and (
                    owned_safety_stop
                    or isinstance(previous_reservation, dict)
                    or (
                        isinstance(stored_ownership, dict)
                        and bool(
                            stored_ownership.get("ev_smart_charging_state")
                            or stored_ownership.get(_EV_COMMAND_ENTITY_OWNERSHIP_KEY)
                            or stored_ownership.get(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY)
                        )
                    )
                )
            )
            action_applied = safe_stop_confirmed if planner_owned_stop else result.applied
            result_reason = (
                "ev_stop_not_confirmed"
                if planner_owned_stop and result.applied and not safe_stop_confirmed
                else "ev_safe_stop_compensated"
                if planner_owned_stop and safe_stop_confirmed and not result.applied
                else result.reason
            )
            if not no_change:
                await self._async_record_command_attempt(action, now)
            if not action_applied:
                pause_duration = ACTION_BACKOFF_DURATION
                retry_limit_reached = planner_owned_stop and _ev_safety_stop_attempt_limit_reached(
                    self.store.data.get("execution_audit"),
                    now,
                    additional_failures=1,
                )
                if planner_owned_stop:
                    await self._async_record_ev_safety_stop_failure(
                        now,
                        retry_limit_reached=retry_limit_reached,
                    )
                if retry_limit_reached:
                    # Persist the full retry window independently of the
                    # bounded audit list. Otherwise repeated rejected plans can
                    # evict the failures and resume device commands too early.
                    pause_duration = EV_SAFETY_STOP_RETRY_WINDOW
                await self._async_pause_asset_control(
                    action.asset,
                    now,
                    result_reason,
                    pause_duration,
                    safety_stop_failure=planner_owned_stop,
                )
            self._reconcile_ev_grid_reservation(action, result, previous_reservation)
            if planner_owned_stop and not action_applied and self.entry_id:
                # Let another controllable EV shed load after this claimant
                # failed, while retaining this entry's uncertain reservation.
                self._clear_ev_grid_shedding_claim(self.entry_id)
            await self.async_persist_ev_grid_reservation()
            if action_applied and planner_owned_stop:
                ownership = dict(self.store.data.get("ownership", {}))
                ownership.pop("ev_smart_charging_state", None)
                ownership.pop(_EV_COMMAND_ENTITY_OWNERSHIP_KEY, None)
                ownership.pop(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY, None)
                await self.store.async_save_ownership(ownership)
            charger_state_may_still_be_owned = (
                _ev_action_wants_power(action)
                and (bool(getattr(result, "command_sent", result.applied and not no_change)) or action_applied)
                and getattr(result, "rollback_succeeded", None) is not True
                and not (action_applied and planner_owned_stop)
            )
            if charger_state_may_still_be_owned:
                ownership = dict(self.store.data.get("ownership", {}))
                if "ev_smart_charging_state" not in ownership:
                    ownership["ev_smart_charging_state"] = result.pre_state
                    ownership[_EV_COMMAND_ENTITY_OWNERSHIP_KEY] = _ev_command_entity_for_action(
                        action,
                        ev_entry_data,
                    )
                    ownership[_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY] = _ev_control_topology(ev_entry_data)
                    await self.store.async_save_ownership(ownership)
            elif provisional_ownership and not self._has_ev_grid_reservation():
                await self._async_clear_provisional_ev_ownership()
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=(
                        OutcomeResult.SKIPPED
                        if no_change
                        else OutcomeResult.APPLIED
                        if action_applied
                        else OutcomeResult.FAILED
                    ),
                    reason=result_reason,
                    pre_state=result.pre_state,
                    post_state=result.post_state,
                    plan_id=plan.plan_id,
                    ev_entry_data=ev_entry_data,
                )
            )
            return
        if reason is None and action.asset == ActionAsset.DAIKIN and self.hass is not None:
            existing_hvac_control = dict(
                dict(self.store.data.get("ownership", {})).get("hvac_control", {})
            )
            if existing_hvac_control.get(_HVAC_MAIN_STATE_OWNERSHIP_KEY):
                # A prior failed transaction owns a main-state baseline that a
                # new acquisition must neither overwrite nor clear. Recover it
                # first; a later planner cycle may acquire only after release
                # has confirmed and removed the snapshot.
                await self.async_release_hvac_control(
                    "hvac_unresolved_main_state_recovery",
                    plan_id=plan.plan_id,
                )
                return
            adapter = DaikinHVACAdapter(self.hass, self.entry_data)
            ownership_before = deepcopy(dict(self.store.data.get("ownership", {})))
            had_hvac_ownership_before = bool(
                ownership_before.get("hvac_control")
                or ownership_before.get("climate_automations")
                or ownership_before.get("planner_takeover_started_at")
                or ownership_before.get("planner_hvac_action_expires_at")
            )
            snapshot_takeover = getattr(adapter, "takeover_snapshot", None)
            automation_snapshot, zone_snapshot = snapshot_takeover() if callable(snapshot_takeover) else ({}, {})
            snapshot_main_takeover = getattr(adapter, "main_takeover_snapshot", None)
            main_snapshot = snapshot_main_takeover() if callable(snapshot_main_takeover) else {}
            if not action.desired_state.get("enable_zones"):
                zone_snapshot = {}
            elif action.desired_state.get("configured_zones_only") is not True:
                zone_snapshot = {
                    entity_id: state
                    for entity_id, state in zone_snapshot.items()
                    if entity_id.split(".", 1)[0] != "climate"
                }
            provisional_ownership = deepcopy(ownership_before)
            provisional_automation_states = dict(
                provisional_ownership.get("climate_automations", {})
            )
            for entity_id, state in automation_snapshot.items():
                provisional_automation_states.setdefault(entity_id, state)
            provisional_ownership["climate_automations"] = (
                provisional_automation_states
            )
            provisional_hvac_control = dict(provisional_ownership.get("hvac_control", {}))
            provisional_zone_states = dict(
                provisional_hvac_control.get("zone_states", {})
            )
            for entity_id, state in zone_snapshot.items():
                provisional_zone_states.setdefault(entity_id, state)
            provisional_hvac_control["zone_states"] = provisional_zone_states
            if main_snapshot:
                provisional_hvac_control.setdefault(_HVAC_MAIN_STATE_OWNERSHIP_KEY, main_snapshot)
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
                "projected_precondition_end_temperature",
                "released_until",
            ):
                if action.desired_state.get(key) is not None:
                    provisional_hvac_control[key] = action.desired_state[key]
            if provisional_hvac_control.get("phase") is None:
                provisional_hvac_control["phase"] = (
                    "away_off" if action.desired_state.get("hvac_mode") == "off" else "direct_control"
                )
            provisional_hvac_control.setdefault("started_at", now)
            provisional_ownership["hvac_control"] = provisional_hvac_control
            provisional_ownership.setdefault("planner_takeover_started_at", now)
            provisional_ownership["planner_hvac_action_expires_at"] = now + timedelta(minutes=2)
            await self.store.async_save_ownership(provisional_ownership)
            await self._async_flush_provisional_state()
            set_main_state_persistence_callback = getattr(
                adapter,
                "set_main_state_persistence_callback",
                None,
            )
            if callable(set_main_state_persistence_callback):
                set_main_state_persistence_callback(
                    self._async_persist_provisional_hvac_main_state,
                )
            pending_hvac_desired_state = dict(action.desired_state)
            self.pending_hvac_desired_state = pending_hvac_desired_state
            self._configure_pending_hvac_adapter(
                adapter,
                pending_hvac_desired_state,
            )
            try:
                try:
                    result = await adapter.async_execute(action)
                except Exception:  # noqa: BLE001 - preserve manual evidence before the executor boundary re-raises.
                    main_state_superseded = (
                        pending_hvac_desired_state.get(
                            _PENDING_HVAC_MANUAL_OVERRIDE_KEY
                        )
                        is True
                    )
                    superseded_zone_entity_ids = {
                        str(entity_id)
                        for entity_id in pending_hvac_desired_state.get(
                            _PENDING_HVAC_MANUAL_ZONE_IDS_KEY,
                            [],
                        )
                        if str(entity_id)
                    }
                    if main_state_superseded or superseded_zone_entity_ids:
                        # An adapter bug must not discard the synchronous user
                        # marker before it has removed stale recovery snapshots.
                        await self._async_persist_provisional_hvac_supersessions(
                            main_state_superseded,
                            superseded_zone_entity_ids,
                        )
                    raise
            finally:
                self.pending_hvac_desired_state = None
            superseded_zone_entity_ids = {
                str(entity_id)
                for entity_id in pending_hvac_desired_state.get(
                    _PENDING_HVAC_MANUAL_ZONE_IDS_KEY,
                    [],
                )
                if str(entity_id)
            }
            main_state_superseded = (
                pending_hvac_desired_state.get(
                    _PENDING_HVAC_MANUAL_OVERRIDE_KEY
                )
                is True
            )
            no_change = (
                result.reason == "already_in_desired_hvac_state"
                and not bool(getattr(result, "command_sent", False))
            )
            if not no_change:
                await self._async_record_command_attempt(action, now)
            if not result.applied:
                await self._async_pause_asset_control(action.asset, now, result.reason, ACTION_BACKOFF_DURATION)
                if getattr(result, "rollback_succeeded", None) is True:
                    rollback_ownership = deepcopy(ownership_before)
                    if isinstance(
                            rollback_ownership.get("hvac_control"),
                            dict,
                        ) and (
                            main_state_superseded
                            or superseded_zone_entity_ids
                        ):
                        rollback_hvac_control = dict(
                            rollback_ownership["hvac_control"]
                        )
                        if main_state_superseded:
                            rollback_hvac_control.pop(
                                _HVAC_MAIN_STATE_OWNERSHIP_KEY,
                                None,
                            )
                        rollback_zone_states = dict(
                            rollback_hvac_control.get("zone_states", {})
                        )
                        for entity_id in superseded_zone_entity_ids:
                            rollback_zone_states.pop(entity_id, None)
                        rollback_hvac_control["zone_states"] = rollback_zone_states
                        rollback_ownership["hvac_control"] = rollback_hvac_control
                    await self.store.async_save_ownership(rollback_ownership)
                if getattr(result, "rollback_succeeded", None) is False:
                    ownership_data = deepcopy(ownership_before)
                    saved_states = (
                        dict(ownership_data.get("climate_automations", {})) if had_hvac_ownership_before else {}
                    )
                    for entity_id, state in result.saved_automation_states.items():
                        saved_states.setdefault(entity_id, state)
                    if saved_states:
                        ownership_data["climate_automations"] = saved_states
                    hvac_control = dict(ownership_data.get("hvac_control", {})) if had_hvac_ownership_before else {}
                    if main_state_superseded:
                        hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
                    zone_states = dict(hvac_control.get("zone_states", {})) if had_hvac_ownership_before else {}
                    for entity_id in superseded_zone_entity_ids:
                        zone_states.pop(entity_id, None)
                    for entity_id, state in getattr(result, "saved_zone_states", {}).items():
                        if entity_id in superseded_zone_entity_ids:
                            continue
                        zone_states.setdefault(entity_id, state)
                    hvac_control["zone_states"] = zone_states
                    unresolved_main_state = dict(getattr(result, "saved_main_state", {}))
                    if unresolved_main_state:
                        hvac_control.setdefault(
                            _HVAC_MAIN_STATE_OWNERSHIP_KEY,
                            unresolved_main_state,
                        )
                    hvac_control["required_evidence_lost"] = "hvac_acquisition_rollback_failed"
                    ownership_data["hvac_control"] = hvac_control
                    await self.store.async_save_ownership(ownership_data)
            if result.applied:
                ownership_data = dict(self.store.data.get("ownership", {}))
                saved_automations = dict(ownership_data.get("climate_automations", {}))
                for entity_id, state in result.saved_automation_states.items():
                    saved_automations.setdefault(entity_id, state)
                ownership_data["climate_automations"] = saved_automations
                hvac_control = dict(ownership_data.get("hvac_control", {}))
                saved_zones = dict(hvac_control.get("zone_states", {}))
                for entity_id, state in getattr(result, "saved_zone_states", {}).items():
                    saved_zones.setdefault(entity_id, state)
                hvac_control["zone_states"] = saved_zones
                hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
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
                    "projected_precondition_end_temperature",
                    "released_until",
                ):
                    if action.desired_state.get(key) is not None:
                        hvac_control[key] = action.desired_state[key]
                hvac_control.setdefault("started_at", now)
                ownership_data["hvac_control"] = hvac_control
                ownership_data.pop("hvac_release_hold_until", None)
                ownership_data.setdefault("planner_takeover_started_at", now)
                ownership_data["planner_hvac_action_expires_at"] = now + timedelta(minutes=2)
                await self.store.async_save_ownership(ownership_data)
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=(
                        OutcomeResult.SKIPPED
                        if no_change
                        else OutcomeResult.APPLIED
                        if result.applied
                        else OutcomeResult.FAILED
                    ),
                    reason=result.reason,
                    pre_state=result.pre_state,
                    post_state=result.post_state,
                    plan_id=plan.plan_id,
                )
            )
            return
        if reason is None and action.asset == ActionAsset.ENPHASE and self.hass is not None:
            result = await EnphaseProfileAdapter(self.hass, self.entry_data).async_execute(action)
            await self._async_record_command_attempt(action, now)
            if not result.applied:
                await self._async_pause_asset_control(action.asset, now, result.reason, ACTION_BACKOFF_DURATION)
                if (
                    bool(getattr(result, "command_sent", False))
                    and getattr(result, "rollback_succeeded", None) is not True
                    and result.saved_profile is not None
                ):
                    ownership_data = dict(self.store.data.get("ownership", {}))
                    ownership_data.setdefault("enphase_profile", result.saved_profile)
                    ownership_data["enphase_profile_changed_at"] = now
                    await self.store.async_save_ownership(ownership_data)
            if result.applied:
                ownership_data = dict(self.store.data.get("ownership", {}))
                if action.kind == ActionKind.RESTORE_AI:
                    ownership_data.pop("enphase_profile", None)
                    ownership_data.pop("enphase_profile_changed_at", None)
                elif result.saved_profile is not None:
                    ownership_data["enphase_profile"] = result.saved_profile
                if result.changed_profile_at and action.kind != ActionKind.RESTORE_AI:
                    ownership_data["enphase_profile_changed_at"] = now
                await self.store.async_save_ownership(ownership_data)
            await self.store.async_add_outcome(
                self._action_outcome(
                    action,
                    now,
                    result=OutcomeResult.APPLIED if result.applied else OutcomeResult.FAILED,
                    reason=result.reason,
                    pre_state=result.pre_state,
                    post_state=result.post_state,
                    plan_id=plan.plan_id,
                )
            )
            return
        reason = reason or "unsupported_asset_execution"
        await self.store.async_add_outcome(
            self._action_outcome(
                action,
                now,
                result=OutcomeResult.SKIPPED if reason == "dry_run" else OutcomeResult.REJECTED,
                reason=reason,
                pre_state={},
                post_state={},
                plan_id=plan.plan_id,
            )
        )

    def _owned_ev_safety_stop(
        self,
        plan: EnergyPlan,
        context: DecisionContext | None,
    ) -> Any | None:
        """Return a stop when planner-owned EV power must be made safe."""
        manual_start_override = bool(
            context is not None
            and any(
                override.kind == "manual_ev_charging" and override.reason == "manual_start"
                for override in context.active_overrides
            )
        )
        pause = self.store.data.get("control_pause")
        if (
            _ev_safety_stop_failure_pause(pause)
            and control_pause_reason(
                pause,
                plan.created_at,
                asset=str(ActionAsset.EV),
            )
            is not None
        ):
            return None
        if _ev_safety_stop_attempt_limit_reached(
            self.store.data.get("execution_audit"),
            plan.created_at,
        ) or _ev_safety_stop_block_active(
            self.store.data.get("command_rate_limits"),
            plan.created_at,
        ):
            return None
        if _ev_safety_stop_backoff_active(
            self.store.data.get("execution_audit"),
            plan.created_at,
            limits=self.store.data.get("command_rate_limits"),
        ):
            return None
        ownership = self.store.data.get("ownership")
        has_owned_ev_state = isinstance(ownership, dict) and bool(ownership.get("ev_smart_charging_state"))
        reservations = self._ev_grid_reservations()
        current_reservation = (
            reservations.get(self.entry_id) if self.entry_id and isinstance(reservations, dict) else None
        )
        has_ev_reservation = _planner_controlled_ev_reservation(current_reservation)
        if not has_owned_ev_state and not has_ev_reservation:
            return None
        disconnected = context is not None and context.ev_connected is False
        ev_input_issue = bool(
            context is not None
            and any(
                str(issue).startswith("ev_")
                for issue in context.input_issues
                if not str(issue).startswith("advisory_")
            )
        )
        ev_confidence_ineligible = bool(
            context is not None
            and not asset_meets_confidence_threshold(
                ActionAsset.EV,
                context,
                self.options,
            )
        )
        unhealthy_inputs = bool(
            context is not None
            and (
                context.input_health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}
                or ev_input_issue
                or ev_confidence_ineligible
            )
        )
        recovered_reservation = has_ev_reservation and not has_owned_ev_state
        ev_control_disabled = (
            has_ev_reservation
            and not manual_start_override
            and not strict_bool(self.options.get(CONF_EV_CONTROL_ENABLED), default=False)
        )
        reservation_safety_issue = self._ev_reservation_safety_issue(context) if has_ev_reservation else None
        plan_safety_issue = next(
            (issue for issue in plan.input_issues if issue in _EV_SAFETY_PLAN_ISSUES),
            None,
        )
        if not (
            disconnected
            or unhealthy_inputs
            or recovered_reservation
            or ev_control_disabled
            or reservation_safety_issue is not None
            or plan_safety_issue is not None
        ):
            return None
        if disconnected:
            charging_reason = "ev_disconnected_safety_stop"
        elif unhealthy_inputs:
            charging_reason = "ev_input_health_safety_stop"
        elif recovered_reservation:
            charging_reason = "ev_recovered_reservation_safety_stop"
        elif ev_control_disabled:
            charging_reason = "ev_control_disabled_safety_stop"
        elif reservation_safety_issue is not None:
            charging_reason = f"ev_{reservation_safety_issue}_safety_stop"
        else:
            charging_reason = f"ev_{plan_safety_issue}_safety_stop"
        return SimpleNamespace(
            action_id=f"{plan.plan_id}-ev-owned-safety-stop",
            asset=ActionAsset.EV,
            kind=ActionKind.EV_STOP,
            desired_state={
                "charging_required_now": False,
                "charging_reason": charging_reason,
                "ev_safety_stop": True,
                "input_health_safety_stop": unhealthy_inputs,
            },
            execute_not_before=plan.created_at,
            execute_not_after=plan.created_at + timedelta(minutes=max(int(plan.interval_minutes), 1)),
        )

    def _ev_reservation_safety_issue(
        self,
        context: DecisionContext | None,
    ) -> str | None:
        """Return a hard shared-limit issue for an existing EV reservation."""
        reservations = self._ev_grid_reservations()
        if reservations is None or not self.entry_id or context is None or not context.slots:
            return None
        self._discard_stale_ev_grid_reservations(reservations)
        current = reservations.get(self.entry_id)
        if not isinstance(current, dict):
            return None
        other_reservations = [
            item for entry_id, item in reservations.items() if entry_id != self.entry_id and isinstance(item, dict)
        ]
        projected_import_kw, _projected_export_kw = _projected_grid_flows_kw(context.slots[0])
        if projected_import_kw is None:
            return None
        own_load_kw = _positive_float(current.get("load_kw"))
        represented_ev_load_kw = _positive_float(context.slots[0].projected_ev_load_kw)
        additional_own_load_kw = max(
            own_load_kw - represented_ev_load_kw,
            0.0,
        )
        other_load_kw = sum(_positive_float(item.get("load_kw")) for item in other_reservations)
        household_limit_kw = min(
            _positive_float(item.get("limit_kw")) for item in reservations.values() if isinstance(item, dict)
        )
        if projected_import_kw + additional_own_load_kw + other_load_kw > household_limit_kw + 1e-6:
            claim = self._ev_grid_shedding_claim()
            if claim is None:
                self._set_ev_grid_shedding_claim(self.entry_id)
                claim = self.entry_id
            if claim == self.entry_id:
                return "multi_ev_grid_import_limit_exceeded" if other_reservations else "grid_import_limit_exceeded"
            return None
        self._clear_ev_grid_shedding_claim(self.entry_id)
        return None

    async def async_release_hvac_control(
        self,
        reason: str,
        *,
        plan_id: str = "manual_hvac_release",
        action: Any | None = None,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
    ) -> ActionOutcome:
        """Release only planner-owned HVAC automation and zone state."""
        now = dt_util.utcnow()
        ownership = dict(self.store.data.get("ownership", {}))
        hvac_control = dict(ownership.get("hvac_control", {}))
        automation_states = dict(ownership.get("climate_automations", {}))
        zone_states = dict(hvac_control.get("zone_states", {}))
        main_state = dict(hvac_control.get(_HVAC_MAIN_STATE_OWNERSHIP_KEY, {}))
        main_state_superseded = (
            preserve_main_state
            and _HVAC_MAIN_STATE_OWNERSHIP_KEY in hvac_control
        )
        if preserve_main_state:
            main_state = {}
            # A main-entity manual change supersedes any failed acquisition
            # snapshot; a release must not overwrite the user's new state.
            hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
        zone_state_superseded = bool(
            preserve_zone_entity_id
            and preserve_zone_entity_id in zone_states
        )
        if preserve_zone_entity_id:
            zone_states.pop(preserve_zone_entity_id, None)
            # A failed release may retain unresolved ownership for a later retry.
            # Do not retain the user-controlled zone or that retry would overwrite
            # the manual target that caused this release.
            hvac_control["zone_states"] = zone_states
        if main_state_superseded or zone_state_superseded:
            # Make user supersession crash-safe before any release call can
            # mutate another actuator. Startup recovery must never see and
            # replay either stale main or zone snapshots.
            ownership["hvac_control"] = hvac_control
            await self.store.async_save_ownership(ownership)
            await self._async_flush_provisional_state()
        had_hvac_ownership = bool(
            automation_states
            or zone_states
            or hvac_control
            or ownership.get("planner_takeover_started_at")
            or ownership.get("planner_hvac_action_expires_at")
        )
        if not had_hvac_ownership:
            result = None
            failed = False
        elif self.hass is None:
            result = None
            failed = bool(automation_states or zone_states or hvac_control)
        else:
            try:
                adapter = DaikinHVACAdapter(self.hass, self.entry_data)
                pending_hvac_desired_state = {
                    "restore_zones": zone_states,
                    "restore_main": main_state,
                }
                self.pending_hvac_desired_state = pending_hvac_desired_state
                self._configure_pending_hvac_adapter(
                    adapter,
                    pending_hvac_desired_state,
                )
                try:
                    result = (
                        await adapter.async_restore(automation_states, zone_states, main_state)
                        if main_state
                        else await adapter.async_restore(automation_states, zone_states)
                        if zone_states
                        else await adapter.async_restore(automation_states)
                    )
                finally:
                    self.pending_hvac_desired_state = None
            except Exception:  # noqa: BLE001 - release must fail closed and retain ownership.
                result = None
                failed = True
            else:
                failed = not bool(getattr(result, "rollback_succeeded", result.applied))
            main_state_superseded = (
                pending_hvac_desired_state.get(
                    _PENDING_HVAC_MANUAL_OVERRIDE_KEY
                )
                is True
            )
            superseded_zone_entity_ids = {
                str(entity_id)
                for entity_id in pending_hvac_desired_state.get(
                    _PENDING_HVAC_MANUAL_ZONE_IDS_KEY,
                    [],
                )
                if str(entity_id)
            }
            if main_state_superseded or superseded_zone_entity_ids:
                # An unexpected adapter exception can bypass its normal result
                # filtering. Preserve the listener's synchronous supersession
                # markers before rebuilding retry ownership from local copies.
                await self._async_persist_provisional_hvac_supersessions(
                    main_state_superseded,
                    superseded_zone_entity_ids,
                )
                if main_state_superseded:
                    main_state = {}
                    hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
                for entity_id in superseded_zone_entity_ids:
                    zone_states.pop(entity_id, None)
                hvac_control["zone_states"] = zone_states
        released_until = None if action is None else action.desired_state.get("released_until")
        if released_until is not None:
            # The comfort hold is independent of whether every actuator can be
            # restored on this attempt. Keep it through retries so a later
            # successful release cannot forget the no-reacquisition boundary.
            ownership["hvac_release_hold_until"] = released_until
        if not failed:
            for key in (
                "climate_automations",
                "hvac_control",
                "planner_takeover_started_at",
                "planner_hvac_action_expires_at",
            ):
                ownership.pop(key, None)
        else:
            if result is not None:
                ownership["climate_automations"] = dict(result.saved_automation_states)
                hvac_control["zone_states"] = {
                    entity_id: state
                    for entity_id, state in dict(
                        getattr(result, "saved_zone_states", {})
                    ).items()
                    if entity_id not in superseded_zone_entity_ids
                }
                saved_main_result = getattr(result, "saved_main_state", None)
                unresolved_main_state = dict(
                    {}
                    if main_state_superseded
                    else main_state
                    if saved_main_result is None
                    else saved_main_result
                )
                if unresolved_main_state:
                    hvac_control[_HVAC_MAIN_STATE_OWNERSHIP_KEY] = unresolved_main_state
                else:
                    hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
            hvac_control["required_evidence_lost"] = "hvac_release_failed"
            ownership["hvac_control"] = hvac_control
        await self.store.async_save_ownership(ownership)
        no_change = bool(
            not failed and not had_hvac_ownership and (result is None or result.pre_state == result.post_state)
        )
        outcome = ActionOutcome(
            action_id=(getattr(action, "action_id", None) or "release_hvac_control"),
            attempted_at=now,
            result=(OutcomeResult.FAILED if failed else OutcomeResult.SKIPPED if no_change else OutcomeResult.RESTORED),
            reason=(
                "hvac_release_exception"
                if result is None and failed
                else "already_released_hvac_control"
                if no_change
                else result.reason
                if result is not None
                else reason
            ),
            pre_state={} if result is None else result.pre_state,
            post_state={} if result is None else result.post_state,
            plan_id=plan_id,
            asset=str(ActionAsset.DAIKIN),
            kind=str(ActionKind.RELEASE_HVAC),
            service_target="hvac_release",
            desired_state={"release_reason": reason},
        )
        await self.store.async_add_outcome(outcome)
        return outcome

    async def async_restore_safe_state(self, reason: str) -> ActionOutcome:
        """Restore planner ownership state and notify the user."""
        return await self._async_restore_safe_state(reason, assets=None)

    async def async_restore_device_control(self, asset: str, reason: str) -> ActionOutcome:
        """Restore planner ownership for exactly one device control area."""
        if asset not in {"ev", "daikin", "enphase"}:
            raise ValueError(f"Unsupported device control asset: {asset}")
        return await self._async_restore_safe_state(reason, assets={asset})

    async def _async_restore_safe_state(
        self,
        reason: str,
        *,
        assets: set[str] | None,
    ) -> ActionOutcome:
        """Restore all planner ownership or the selected device control areas."""
        now = dt_util.utcnow()
        ownership = dict(self.store.data.get("ownership", {}))
        remaining_ownership = dict(ownership)
        results: list[Any] = []
        reasons = [reason]
        restore_failed = False

        ev_state = dict(ownership.get("ev_smart_charging_state", {}))
        ev_command_entity_id = ownership.get(_EV_COMMAND_ENTITY_OWNERSHIP_KEY)
        ev_control_topology = ownership.get(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY)
        reservations = self._ev_grid_reservations()
        current_reservation = (
            reservations.get(self.entry_id) if self.entry_id and isinstance(reservations, dict) else None
        )
        has_ev_reservation = _planner_controlled_ev_reservation(current_reservation)
        hvac_control = dict(ownership.get("hvac_control", {}))
        hvac_state = dict(ownership.get("climate_automations", {}))
        hvac_zone_state = dict(hvac_control.get("zone_states", {}))
        hvac_main_state = dict(hvac_control.get(_HVAC_MAIN_STATE_OWNERSHIP_KEY, {}))
        enphase_owned = bool(ownership.get("enphase_profile") or ownership.get("enphase_profile_changed_at"))
        restore_ev = assets is None or "ev" in assets
        restore_hvac = assets is None or "daikin" in assets
        restore_enphase = assets is None or "enphase" in assets
        restore_requested = bool(
            (restore_ev and (ev_state or has_ev_reservation))
            or (restore_hvac and (hvac_state or hvac_zone_state or hvac_control))
            or (restore_enphase and enphase_owned)
        )

        if restore_requested and self.hass is None:
            restore_failed = True
            reasons.append("home_assistant_unavailable")
        elif self.hass is not None:
            if restore_ev and (ev_state or has_ev_reservation):
                try:
                    ev_adapter = EVSmartChargingAdapter(
                        self.hass,
                        (
                            dict(ev_control_topology)
                            if isinstance(ev_control_topology, dict) and ev_control_topology
                            else self.entry_data
                        ),
                        confirmation_timeout_seconds=float(self.options.get(CONF_EV_CONFIRMATION_TIMEOUT_SECONDS, 30)),
                        confirmation_retries=int(self.options.get(CONF_EV_CONFIRMATION_RETRIES, 1)),
                    )
                    ev_control_disabled = assets == {"ev"} and reason == "ev_control_disabled"
                    if ev_control_disabled:
                        # An explicit control disable requests a safe stop, not
                        # restoration of a previously-on charger baseline.
                        ev_result = await ev_adapter.async_restore()
                    elif isinstance(ev_command_entity_id, str) and ev_command_entity_id:
                        ev_result = await ev_adapter.async_restore(
                            ev_state,
                            command_entity_id=ev_command_entity_id,
                        )
                    elif ev_state:
                        ev_result = await ev_adapter.async_restore(ev_state)
                    else:
                        # A persisted reservation can outlive its ownership write
                        # after an interrupted manual start. It remains evidence
                        # of possible charger load and requires a confirmed stop.
                        ev_result = await ev_adapter.async_restore()
                except Exception:  # noqa: BLE001 - restoration must continue for the other assets.
                    restore_failed = True
                    reasons.append("ev_restore_exception")
                    self._retain_ev_grid_reservation_when_unloaded()
                else:
                    results.append(ev_result)
                    reasons.append(ev_result.reason)
                    if ev_result.applied:
                        remaining_ownership.pop("ev_smart_charging_state", None)
                        remaining_ownership.pop(_EV_COMMAND_ENTITY_OWNERSHIP_KEY, None)
                        remaining_ownership.pop(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY, None)
                        if not ev_control_disabled and _restored_ev_baseline_is_active(ev_state):
                            self._retain_external_ev_grid_reservation()
                        else:
                            self._release_ev_grid_reservation()
                    else:
                        restore_failed = True
                        self._retain_ev_grid_reservation_when_unloaded()

            if restore_hvac and (hvac_state or hvac_zone_state or hvac_control):
                try:
                    adapter = DaikinHVACAdapter(self.hass, self.entry_data)
                    pending_hvac_desired_state = {
                        "restore_zones": hvac_zone_state,
                        "restore_main": hvac_main_state,
                    }
                    self.pending_hvac_desired_state = pending_hvac_desired_state
                    self._configure_pending_hvac_adapter(
                        adapter,
                        pending_hvac_desired_state,
                    )
                    try:
                        hvac_result = (
                            await adapter.async_restore(
                                hvac_state,
                                hvac_zone_state,
                                hvac_main_state,
                            )
                            if hvac_main_state
                            else await adapter.async_restore(hvac_state, hvac_zone_state)
                            if hvac_zone_state
                            else await adapter.async_restore(hvac_state)
                        )
                    finally:
                        self.pending_hvac_desired_state = None
                except Exception:  # noqa: BLE001 - restoration must continue for the other assets.
                    main_state_superseded = (
                        pending_hvac_desired_state.get(
                            _PENDING_HVAC_MANUAL_OVERRIDE_KEY
                        )
                        is True
                    )
                    superseded_zone_entity_ids = {
                        str(entity_id)
                        for entity_id in pending_hvac_desired_state.get(
                            _PENDING_HVAC_MANUAL_ZONE_IDS_KEY,
                            [],
                        )
                        if str(entity_id)
                    }
                    if main_state_superseded or superseded_zone_entity_ids:
                        # Do not let the safe-state exception path reintroduce
                        # snapshots already superseded by a user while another
                        # asset is still due to be restored.
                        await self._async_persist_provisional_hvac_supersessions(
                            main_state_superseded,
                            superseded_zone_entity_ids,
                        )
                        if main_state_superseded:
                            hvac_main_state = {}
                            hvac_control.pop(
                                _HVAC_MAIN_STATE_OWNERSHIP_KEY,
                                None,
                            )
                        for entity_id in superseded_zone_entity_ids:
                            hvac_zone_state.pop(entity_id, None)
                    restore_failed = True
                    reasons.append("hvac_restore_exception")
                    retained_hvac_control = dict(hvac_control)
                    retained_hvac_control["zone_states"] = hvac_zone_state
                    retained_hvac_control["required_evidence_lost"] = "hvac_release_failed"
                    remaining_ownership["hvac_control"] = retained_hvac_control
                else:
                    results.append(hvac_result)
                    reasons.append(hvac_result.reason)
                    if getattr(hvac_result, "rollback_succeeded", hvac_result.applied) is True:
                        for key in (
                            "climate_automations",
                            "hvac_control",
                            "planner_takeover_started_at",
                            "planner_hvac_action_expires_at",
                        ):
                            remaining_ownership.pop(key, None)
                    else:
                        restore_failed = True
                        saved_automation_result = getattr(hvac_result, "saved_automation_states", None)
                        unresolved_automations = dict(
                            hvac_state if saved_automation_result is None else saved_automation_result
                        )
                        remaining_ownership.pop("climate_automations", None)
                        if unresolved_automations:
                            remaining_ownership["climate_automations"] = unresolved_automations
                        saved_zone_result = getattr(hvac_result, "saved_zone_states", None)
                        unresolved_zones = dict(hvac_zone_state if saved_zone_result is None else saved_zone_result)
                        for entity_id in {
                            str(entity_id)
                            for entity_id in pending_hvac_desired_state.get(
                                _PENDING_HVAC_MANUAL_ZONE_IDS_KEY,
                                [],
                            )
                            if str(entity_id)
                        }:
                            unresolved_zones.pop(entity_id, None)
                        retained_hvac_control = dict(hvac_control)
                        retained_hvac_control["zone_states"] = unresolved_zones
                        saved_main_result = getattr(hvac_result, "saved_main_state", None)
                        main_state_superseded = (
                            pending_hvac_desired_state.get(
                                _PENDING_HVAC_MANUAL_OVERRIDE_KEY
                            )
                            is True
                        )
                        unresolved_main_state = dict(
                            {}
                            if main_state_superseded
                            else hvac_main_state
                            if saved_main_result is None
                            else saved_main_result
                        )
                        if unresolved_main_state:
                            retained_hvac_control[_HVAC_MAIN_STATE_OWNERSHIP_KEY] = (
                                unresolved_main_state
                            )
                        else:
                            retained_hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
                        retained_hvac_control["required_evidence_lost"] = "hvac_release_failed"
                        remaining_ownership["hvac_control"] = retained_hvac_control

            if restore_enphase and (assets is None or enphase_owned):
                # Restore a recorded Enphase profile first; a full safe-state
                # restore retains the existing best-effort configured fallback.
                try:
                    enphase_adapter = EnphaseProfileAdapter(self.hass, self.entry_data)
                    saved_enphase_profile = ownership.get("enphase_profile")
                    restore_profile = getattr(enphase_adapter, "async_restore_profile", None)
                    if isinstance(saved_enphase_profile, str) and callable(restore_profile):
                        enphase_result = await restore_profile(saved_enphase_profile)
                    else:
                        enphase_result = await enphase_adapter.async_restore_ai()
                except Exception:  # noqa: BLE001 - restoration must preserve retry state.
                    restore_failed = True
                    reasons.append("enphase_restore_exception")
                else:
                    results.append(enphase_result)
                    reasons.append(enphase_result.reason)
                    if enphase_result.applied:
                        remaining_ownership.pop("enphase_profile", None)
                        remaining_ownership.pop("enphase_profile_changed_at", None)
                    elif enphase_owned or "not_configured" not in enphase_result.reason:
                        restore_failed = True

        # Ownership metadata that cannot represent a pending device restore can
        # always be released. Device baselines above are retained until their
        # corresponding adapter confirms restoration.
        if restore_hvac and not hvac_state and not hvac_zone_state and not hvac_control:
            for key in (
                "planner_takeover_started_at",
                "planner_hvac_action_expires_at",
                "manual_hvac_override_expires_at",
            ):
                remaining_ownership.pop(key, None)
        if restore_ev and not ev_state and not has_ev_reservation:
            remaining_ownership.pop(_EV_COMMAND_ENTITY_OWNERSHIP_KEY, None)
            remaining_ownership.pop(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY, None)

        await self.async_persist_ev_grid_reservation()
        await self.store.async_save_ownership(remaining_ownership)
        pre_state: dict[str, Any] = {}
        post_state: dict[str, Any] = {}
        for result in results:
            pre_state.update(result.pre_state)
            post_state.update(result.post_state)
        if not remaining_ownership:
            ownership_status = "cleared"
        elif remaining_ownership == ownership:
            ownership_status = "retained"
        else:
            ownership_status = "partially_cleared"
        post_state["ownership"] = ownership_status
        post_state["remaining_ownership"] = sorted(remaining_ownership)
        outcome = ActionOutcome(
            action_id="restore_safe_state",
            attempted_at=now,
            result=OutcomeResult.FAILED if restore_failed else OutcomeResult.RESTORED,
            reason=":".join(reasons),
            pre_state=pre_state,
            post_state=post_state,
            plan_id="manual",
            service_target="restore_safe_state",
        )
        await self.store.async_add_outcome(outcome)
        notification_scope = None if assets is None else next(iter(assets))
        await self._async_notify_restore(outcome, scope=notification_scope)
        return outcome

    def _action_outcome(
        self,
        action: Any,
        attempted_at: datetime,
        *,
        result: OutcomeResult,
        reason: str,
        pre_state: dict[str, Any],
        post_state: dict[str, Any],
        plan_id: str,
        ev_entry_data: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        """Return an outcome enriched for the execution audit trail."""
        service_target = (
            _ev_command_entity_for_action(
                action,
                ev_entry_data or self._ev_entry_data_for_action(action),
            )
            if action.asset == ActionAsset.EV
            else _service_target_for_action(action, self.entry_data)
        )
        return ActionOutcome(
            action_id=action.action_id,
            attempted_at=attempted_at,
            result=result,
            reason=reason,
            pre_state=pre_state,
            post_state=post_state,
            plan_id=plan_id,
            asset=str(action.asset),
            kind=str(action.kind),
            service_target=service_target,
            desired_state=dict(action.desired_state),
        )

    def _rate_limit_reason(self, action: Any, now: datetime) -> str | None:
        """Return a rejection reason when an action is inside the command cooldown."""
        cooldown_seconds = int(self.options.get(CONF_COMMAND_RATE_LIMIT_SECONDS, 0) or 0)
        if cooldown_seconds <= 0:
            return None
        last_attempts = dict(self.store.data.get("command_rate_limits", {}))
        attempted_at = _parse_datetime_or_none(last_attempts.get(_command_rate_limit_key(action)))
        if attempted_at is None:
            return None
        if now < attempted_at + timedelta(seconds=cooldown_seconds):
            return "device_command_rate_limited"
        return None

    def _control_rejection_reason(self, action: Any, now: datetime) -> str | None:
        """Return production-control rejection reason for active device commands."""
        safety_ev_stop = _ev_action_is_safety_stop(action)
        owned_safety_stop = _ev_action_is_owned_safety_stop(action)
        pause = self.store.data.get("control_pause")
        pause_reason = _pause_rejection_reason(pause, action, now)
        if pause_reason is not None and (
            not safety_ev_stop
            or (owned_safety_stop and _ev_safety_stop_failure_pause(pause))
        ):
            return pause_reason
        if owned_safety_stop and (
            _ev_safety_stop_attempt_limit_reached(
                self.store.data.get("execution_audit"),
                now,
            )
            or _ev_safety_stop_block_active(
                self.store.data.get("command_rate_limits"),
                now,
            )
        ):
            return "ev_safety_stop_retry_limit_reached"
        if owned_safety_stop and _ev_safety_stop_backoff_active(
            self.store.data.get("execution_audit"),
            now,
            limits=self.store.data.get("command_rate_limits"),
        ):
            return "ev_control_paused"
        if not owned_safety_stop:
            production = parse_production_state(self.store.data.get("production"))
            if not production.armed:
                return "production_gate_not_armed"
            if not strict_bool(self.options.get(CONF_BYPASS_SAFETY_GATES), default=False):
                if production.dry_run_evidence_fingerprint != production_evidence_fingerprint(
                    self.entry_data,
                    self.options,
                ):
                    return "production_evidence_contract_changed"
                if production.dry_run_ready_cycles < DRY_RUN_READY_CYCLES_REQUIRED:
                    return "production_dry_run_evidence_incomplete"
            device_reason = _device_control_disabled_reason(action.asset, self.options)
            if device_reason is not None:
                return device_reason
        hvac_continuation = bool(
            action.asset == ActionAsset.DAIKIN
            and action.desired_state.get("phase") in {"preconditioning", "pre_peak_coast", "peak_coast"}
            and dict(self.store.data.get("ownership", {})).get("hvac_control")
        )
        if not safety_ev_stop and not hvac_continuation:
            cap_reason = _daily_action_cap_reason(
                action.asset,
                self.options,
                self.store.data.get("execution_audit"),
                now,
            )
            if cap_reason is not None:
                return cap_reason
        return None

    async def _async_record_command_attempt(self, action: Any, attempted_at: datetime) -> None:
        """Persist the latest command attempt timestamp for rate limiting."""
        limits = dict(self.store.data.get("command_rate_limits", {}))
        limits[_command_rate_limit_key(action)] = attempted_at
        await self.store.async_save_command_rate_limits(limits)

    async def _async_record_ev_safety_stop_failure(
        self,
        attempted_at: datetime,
        *,
        retry_limit_reached: bool,
    ) -> None:
        """Persist EV safety-stop backoff independently of shared pause and bounded audit state."""
        limits = dict(self.store.data.get("command_rate_limits", {}))
        limits[_EV_SAFETY_STOP_FAILED_AT_KEY] = attempted_at
        if retry_limit_reached:
            limits[_EV_SAFETY_STOP_BLOCKED_UNTIL_KEY] = (
                attempted_at + EV_SAFETY_STOP_RETRY_WINDOW
            )
        await self.store.async_save_command_rate_limits(limits)

    async def _async_pause_asset_control(
        self,
        asset: ActionAsset,
        now: datetime,
        reason: str,
        duration: timedelta,
        *,
        safety_stop_failure: bool = False,
    ) -> None:
        """Pause one asset after failed or conflicting active control."""
        pause = {
            "active": True,
            "assets": [str(asset)],
            "until": now + duration,
            "reason": reason,
        }
        if safety_stop_failure:
            pause["safety_stop_failure"] = True
        save_pause = getattr(self.store, "async_save_control_pause", None)
        if callable(save_pause):
            await save_pause(pause)
        else:
            self.store.data["control_pause"] = pause

    def _observed_conflict_reason(self, action: Any, now: datetime) -> str | None:
        """Return a conflict reason when another automation appears to have changed planner-owned state."""
        if self.hass is None:
            return None
        recent = _latest_applied_audit_for_asset(self.store.data.get("execution_audit"), action.asset, now)
        if recent is None:
            return None
        target = (
            _ev_command_entity_for_action(action, self.entry_data)
            if action.asset == ActionAsset.EV
            else _service_target_for_action(action, self.entry_data)
        )
        entity_id = _entity_id_from_service_target(target)
        if not entity_id:
            return None
        post_state = dict(recent.get("post_state", {}))
        if action.asset == ActionAsset.ENPHASE:
            state = self.hass.states.get(entity_id)
            if state is None:
                return None
            observed = str(getattr(state, "state", "") or "")
            expected = (
                post_state.get("current_profile") or post_state.get("profile") or action.desired_state.get("profile")
            )
            if expected and observed != str(expected):
                return "external_enphase_profile_conflict"
        recent_desired_state = recent.get("desired_state")
        if not isinstance(recent_desired_state, dict):
            recent_desired_state = {}
        recent_kind = str(recent.get("kind", ""))
        recent_ev_wanted_power = recent_kind == str(ActionKind.EV_START) or (
            recent_kind == str(ActionKind.EV_SCHEDULE)
            and (
                "charging_required_now" not in recent_desired_state
                or bool(recent_desired_state.get("charging_required_now"))
            )
        )
        recent_target = _entity_id_from_service_target(recent.get("service_target"))
        if (
            action.asset == ActionAsset.EV
            and _ev_action_wants_power(action)
            and recent_ev_wanted_power
            and recent_target == entity_id
        ):
            charging_feedback = (
                None if action.desired_state.get("keep_charger_on") else self.entry_data.get(CONF_EV_CHARGING)
            )
            state = self.hass.states.get(charging_feedback) if charging_feedback else None
            raw_state = getattr(state, "state", None)
            observed = str(raw_state or "").strip().lower()
            using_charging_feedback = (
                state is not None and raw_state is not None and observed not in STATE_UNKNOWN_VALUES
            )
            if not using_charging_feedback:
                if entity_id.partition(".")[0] not in {"input_boolean", "switch"}:
                    return None
                state = self.hass.states.get(entity_id)
                raw_state = getattr(state, "state", None)
                observed = str(raw_state or "").strip().lower()
                if state is None or raw_state is None or observed in STATE_UNKNOWN_VALUES:
                    return None
            stopped_states = {
                "off",
                "false",
                "0",
                "idle",
                "not_charging",
            }
            if using_charging_feedback:
                stopped_states.add("connected_not_charging")
            if observed in stopped_states:
                return "external_ev_charging_conflict"
        return None

    async def async_notify_plan_fallback(self, plan: EnergyPlan, violations: list[str]) -> None:
        """Create persistent notifications for major plan fallback classes."""
        if not strict_bool(
            self.options.get(CONF_PLAN_FALLBACK_NOTIFICATIONS_ENABLED),
            default=True,
        ):
            self._plan_fallback_notification_signatures.clear()
            await self._async_dismiss_notifications(self._plan_fallback_notification_ids())
            return
        clean_violations = _clean_reason_codes(violations)
        grid_violations = [
            code for code in clean_violations if code in {"grid_import_limit_exceeded", "grid_export_limit_exceeded"}
        ]
        actionable_input_issues = _actionable_input_issues(plan.input_issues)
        if self._in_notification_grace_period():
            self._plan_fallback_notification_signatures.clear()
            await self._async_dismiss_notifications(self._plan_fallback_notification_ids())
            return
        if plan.mode in {PlannerMode.DISABLED, PlannerMode.DRY_RUN}:
            self._plan_fallback_notification_signatures.clear()
            await self._async_dismiss_notifications(self._plan_fallback_notification_ids())
            return
        if "input_health_unsafe" in violations and actionable_input_issues:
            await self._async_create_plan_fallback_notification(
                title=self._notification_title("configuration needs attention"),
                message=_plan_fallback_message(
                    plan,
                    "Automatic control is blocked because required configuration or mapped entities need attention.",
                    actionable_input_issues,
                ),
                notification_id=self._notification_id(_PLAN_UNSAFE_NOTIFICATION_ID),
            )
        else:
            await self._async_dismiss_plan_fallback_notification(self._notification_id(_PLAN_UNSAFE_NOTIFICATION_ID))
        if grid_violations:
            await self._async_create_plan_fallback_notification(
                title=self._notification_title("grid limit fallback"),
                message=_plan_fallback_message(
                    plan,
                    "The current plan would exceed a configured grid import/export hard limit.",
                    grid_violations,
                ),
                notification_id=self._notification_id(_GRID_LIMIT_NOTIFICATION_ID),
            )
        else:
            await self._async_dismiss_plan_fallback_notification(self._notification_id(_GRID_LIMIT_NOTIFICATION_ID))
        # Remove notifications created by releases that supported the retired
        # optimizer. This is intentionally cleanup-only and never creates one.
        await self._async_dismiss_plan_fallback_notification(
            self._notification_id(_RETIRED_FALLBACK_NOTIFICATION_ID)
        )
        if not any(
            action.asset == ActionAsset.EV and action.desired_state.get("infeasible") for action in plan.actions
        ):
            await self._async_dismiss_plan_fallback_notification(
                self._notification_id(_EV_INFEASIBLE_NOTIFICATION_ID)
            )

    async def async_notify_startup_recovery_unsafe(self, reason: str) -> None:
        """Notify once when startup safety disarms retained automatic intent."""
        await self._async_create_plan_fallback_notification(
            title=self._notification_title("automatic control paused after startup"),
            message=(
                "Automatic control remains requested, but command authority was disarmed because "
                f"the startup safety check was not healthy ({reason}). Energy Planner restored its "
                "owned devices toward safe state and will retry automatically every 30 seconds. "
                "It will re-arm after three consecutive healthy checks."
            ),
            notification_id=self._notification_id(_STARTUP_RECOVERY_NOTIFICATION_ID),
        )

    async def async_dismiss_startup_recovery_notification(self) -> None:
        """Dismiss the deduplicated startup safety notification."""
        await self._async_dismiss_plan_fallback_notification(
            self._notification_id(_STARTUP_RECOVERY_NOTIFICATION_ID)
        )

    def _in_notification_grace_period(self) -> bool:
        """Return whether startup warm-up should suppress fallback notifications."""
        return self.notification_grace_until is not None and dt_util.utcnow() < self.notification_grace_until

    async def _async_notify_restore(self, outcome: ActionOutcome, *, scope: str | None = None) -> None:
        """Notify only when a safe-state restore requires user attention."""
        notification_key = "ha_energy_planner_restore_safe_state"
        if scope is not None:
            notification_key = f"{notification_key}_{scope}"
        notification_id = self._notification_id(notification_key)
        if outcome.result != OutcomeResult.FAILED:
            await self._async_dismiss_plan_fallback_notification(notification_id)
            return
        await self._async_create_plan_fallback_notification(
            title=self._notification_title("safe-state restore failed"),
            message=_restore_notification_message(outcome.reason),
            notification_id=notification_id,
        )

    async def _async_notify_ev_infeasible(self, action: Any) -> None:
        """Create a persistent notification for infeasible EV ready-by plans."""
        if action.asset != ActionAsset.EV or not action.desired_state.get("infeasible"):
            return
        await self._async_create_plan_fallback_notification(
            title=self._notification_title("EV target infeasible"),
            message=(
                "The EV cannot reach the requested ready-by target with the current "
                f"schedule. Planned target: {action.desired_state.get('target_soc_percent')}%. "
                f"Ready by: {action.desired_state.get('ready_by', 'not configured')}."
            ),
            notification_id=self._notification_id(_EV_INFEASIBLE_NOTIFICATION_ID),
        )

    def _notification_id(self, base: str) -> str:
        """Return an ID isolated to this config entry."""
        return f"{base}_{self.entry_id}" if self.entry_id else base

    def _plan_fallback_notification_ids(self) -> tuple[str, ...]:
        """Return all stable fallback IDs isolated to this config entry."""
        return tuple(
            self._notification_id(notification_id)
            for notification_id in (*_PLAN_FALLBACK_NOTIFICATION_IDS, _RETIRED_FALLBACK_NOTIFICATION_ID)
        )

    def _notification_title(self, suffix: str) -> str:
        """Return a title that identifies the planner instance."""
        if self.entry_title:
            return f"{self.entry_title}: {suffix}"
        return f"Energy Planner {suffix}"

    async def _async_create_plan_fallback_notification(
        self,
        *,
        title: str,
        message: str,
        notification_id: str,
    ) -> None:
        """Create or update a fallback notification only when its content changes."""
        signature = (title, message)
        if self._plan_fallback_notification_signatures.get(notification_id) == signature:
            return
        created = await self._async_create_notification(
            title=title,
            message=message,
            notification_id=notification_id,
        )
        if created:
            self._plan_fallback_notification_signatures[notification_id] = signature

    async def _async_dismiss_plan_fallback_notification(self, notification_id: str) -> None:
        """Dismiss a fallback notification and allow a later recurrence to alert."""
        self._plan_fallback_notification_signatures.pop(notification_id, None)
        await self._async_dismiss_notification(notification_id)

    async def _async_create_notification(self, *, title: str, message: str, notification_id: str) -> bool:
        """Create a persistent notification if the service is available."""
        if self.hass is None:
            return False
        if defer_persistent_notification(
            self.hass,
            notification_id,
            lambda: self._async_create_deferred_notification(
                title=title,
                message=message,
                notification_id=notification_id,
            ),
        ):
            return False
        services = getattr(self.hass, "services", None)
        has_service = getattr(services, "has_service", None)
        if callable(has_service) and not has_service("persistent_notification", "create"):
            return False
        try:
            await services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": notification_id,
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - notifications are best-effort Home Assistant I/O.
            return False
        return True

    async def _async_create_deferred_notification(
        self,
        *,
        title: str,
        message: str,
        notification_id: str,
    ) -> None:
        """Create a queued notification and record its signature on success."""
        created = await self._async_create_notification(
            title=title,
            message=message,
            notification_id=notification_id,
        )
        if created:
            self._plan_fallback_notification_signatures[notification_id] = (title, message)

    async def _async_dismiss_notifications(self, notification_ids: tuple[str, ...]) -> None:
        """Dismiss persistent notifications if the service is available."""
        for notification_id in notification_ids:
            await self._async_dismiss_notification(notification_id)

    async def _async_dismiss_notification(self, notification_id: str) -> None:
        """Dismiss a persistent notification if the service is available."""
        if self.hass is None:
            return
        cancel_deferred_persistent_notification(self.hass, notification_id)
        services = getattr(self.hass, "services", None)
        has_service = getattr(services, "has_service", None)
        if callable(has_service) and not has_service("persistent_notification", "dismiss"):
            return
        with suppress(Exception):
            await services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": notification_id},
                blocking=False,
            )

    def _reserve_ev_grid_capacity(
        self,
        action: Any,
        context: DecisionContext | None,
        now: datetime,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Atomically reserve household grid headroom for one EV planner entry."""
        reservations = self._ev_grid_reservations()
        if reservations is None or not self.entry_id:
            return None, None
        self._discard_stale_ev_grid_reservations(reservations)
        previous = reservations.get(self.entry_id)
        if not _ev_action_wants_power(action):
            return None, previous
        if context is None or not context.slots:
            return "ev_grid_projection_unavailable", previous
        reservations.pop(self.entry_id, None)

        load_kw = _positive_float(action.desired_state.get("projected_load_kw_now"))
        if load_kw <= 0:
            load_kw = max(float(self.options.get(CONF_EV_CHARGE_RATE_KW, 0.0)), 0.0)
        if isinstance(previous, dict):
            # A model/options update cannot prove that an already-running charger
            # has reduced its physical draw. Only a confirmed stop releases the
            # prior reservation, so subsequent start/no-op actions retain the
            # larger of the observed reservation and newly requested load.
            load_kw = max(load_kw, _positive_float(previous.get("load_kw")))
        other_load_kw = sum(
            _positive_float(item.get("load_kw")) for item in reservations.values() if isinstance(item, dict)
        )
        projected_import_kw, _projected_export_kw = _projected_grid_flows_kw(context.slots[0])
        represented_ev_load_kw = _positive_float(context.slots[0].projected_ev_load_kw)
        additional_requested_load_kw = max(load_kw - represented_ev_load_kw, 0.0)
        if projected_import_kw is None:
            if previous is not None:
                reservations[self.entry_id] = previous
            reason = "multi_ev_grid_projection_unavailable" if other_load_kw > 0 else "ev_grid_projection_unavailable"
            return reason, previous
        configured_limit_kw = max(float(self.options.get(CONF_GRID_IMPORT_LIMIT_KW, 0.0)), 0.0)
        limits = [configured_limit_kw]
        limits.extend(
            max(_positive_float(item.get("limit_kw")), 0.0) for item in reservations.values() if isinstance(item, dict)
        )
        household_limit_kw = min(limits)
        if (
            projected_import_kw is not None
            and projected_import_kw + additional_requested_load_kw + other_load_kw > household_limit_kw + 1e-6
        ):
            if previous is not None:
                reservations[self.entry_id] = previous
            return "multi_ev_grid_import_limit_exceeded", previous
        reservations[self.entry_id] = {
            "load_kw": load_kw,
            "limit_kw": configured_limit_kw,
            "reserved_at": now.isoformat(),
        }
        return None, previous

    def _reconcile_ev_grid_reservation(
        self,
        action: Any,
        result: EVCommandResult,
        previous_reservation: dict[str, Any] | None,
    ) -> None:
        """Reconcile a provisional reservation with the command outcome."""
        wants_power = _ev_action_wants_power(action)
        rollback_succeeded = getattr(result, "rollback_succeeded", None) is True
        command_sent = bool(getattr(result, "command_sent", False))
        if wants_power and not result.applied:
            if rollback_succeeded:
                self._release_ev_grid_reservation()
            elif not command_sent:
                if previous_reservation is None:
                    self._release_ev_grid_reservation()
                else:
                    self._restore_ev_grid_reservation(previous_reservation)
            else:
                self._retain_ev_grid_reservation_when_unloaded()
        elif not wants_power:
            if _ev_result_proves_safe(result):
                self._release_ev_grid_reservation()
            elif previous_reservation is not None:
                self._restore_ev_grid_reservation(previous_reservation)
                self._retain_ev_grid_reservation_when_unloaded()

    def _release_ev_grid_reservation(self) -> None:
        """Release this entry's household EV-grid reservation."""
        if not self.entry_id:
            return
        reservations = self._ev_grid_reservations()
        if reservations is not None:
            reservations.pop(self.entry_id, None)
        self._clear_ev_grid_shedding_claim(self.entry_id)

    def _restore_ev_grid_reservation(self, reservation: dict[str, Any]) -> None:
        """Restore a prior reservation when a stop command cannot be proven safe."""
        reservations = self._ev_grid_reservations()
        if reservations is not None and self.entry_id:
            reservations[self.entry_id] = reservation

    def _retain_ev_grid_reservation_when_unloaded(self) -> None:
        """Keep uncertain charger load reserved even if its config entry unloads."""
        reservations = self._ev_grid_reservations()
        if reservations is None or not self.entry_id:
            return
        reservation = reservations.get(self.entry_id)
        if not isinstance(reservation, dict):
            return
        reservation[EV_RESERVATION_RETAIN_WHEN_UNLOADED] = True

    def _retain_external_ev_grid_reservation(self) -> None:
        """Keep restored active baseline load without planner stop ownership."""
        reservations = self._ev_grid_reservations()
        if reservations is None or not self.entry_id:
            return
        reservation = reservations.get(self.entry_id)
        if not isinstance(reservation, dict):
            return
        reservation[EV_RESERVATION_EXTERNAL_BASELINE] = True
        reservation[EV_RESERVATION_RETAIN_WHEN_UNLOADED] = True

    def sync_ev_grid_reservation(self) -> None:
        """Refresh this entry's active reservation from its current options."""
        reservations = self._ev_grid_reservations()
        if reservations is None or not self.entry_id:
            return
        reservation = reservations.get(self.entry_id)
        if not isinstance(reservation, dict):
            return
        reservation["load_kw"] = max(
            _positive_float(reservation.get("load_kw")),
            _positive_float(self.options.get(CONF_EV_CHARGE_RATE_KW)),
        )
        reservation["limit_kw"] = _positive_float(self.options.get(CONF_GRID_IMPORT_LIMIT_KW))

    async def async_persist_ev_grid_reservation(self) -> None:
        """Persist this entry's current reservation high-watermark."""
        reservation: dict[str, Any] = {}
        reservations = self._ev_grid_reservations()
        if reservations is not None and self.entry_id:
            current = reservations.get(self.entry_id)
            if isinstance(current, dict):
                reservation = {**dict(current), "active": True}
        if not reservation:
            reservation = {"active": False}
        save_reservation = getattr(self.store, "async_save_ev_grid_reservation", None)
        if callable(save_reservation):
            await save_reservation(reservation)
        else:
            self.store.data["ev_grid_reservation"] = reservation

    def _ev_grid_reservations(self) -> dict[str, dict[str, Any]] | None:
        """Return the in-memory reservation map shared by loaded planner entries."""
        domain_data = self._ev_grid_domain_data()
        if domain_data is None:
            return None
        reservations = domain_data.setdefault("ev_grid_reservations", {})
        return reservations if isinstance(reservations, dict) else None

    def _owned_ev_control_topology(self) -> dict[str, Any] | None:
        """Return the actuator topology that created pending EV ownership."""
        ownership = self.store.data.get("ownership")
        if not isinstance(ownership, dict):
            return None
        topology = ownership.get(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY)
        return dict(topology) if isinstance(topology, dict) and topology else None

    def _has_ev_grid_reservation(self) -> bool:
        """Return whether this entry has planner-controlled EV grid capacity."""
        reservations = self._ev_grid_reservations()
        reservation = reservations.get(self.entry_id) if self.entry_id and isinstance(reservations, dict) else None
        return _planner_controlled_ev_reservation(reservation)

    async def _async_save_provisional_ev_ownership(
        self,
        action: Any,
        ev_entry_data: dict[str, Any],
    ) -> bool:
        """Persist actuator identity before an EV start crosses the service boundary."""
        ownership = dict(self.store.data.get("ownership", {}))
        if ownership.get("ev_smart_charging_state"):
            return False
        command_entity_id = _ev_command_entity_for_action(action, ev_entry_data)
        topology = _ev_control_topology(ev_entry_data)
        changed = (
            ownership.get(_EV_COMMAND_ENTITY_OWNERSHIP_KEY) != command_entity_id
            or ownership.get(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY) != topology
        )
        ownership[_EV_COMMAND_ENTITY_OWNERSHIP_KEY] = command_entity_id
        ownership[_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY] = topology
        if changed:
            await self.store.async_save_ownership(ownership)
        # Even unchanged actuator-only metadata belongs to this provisional
        # command and must be cleared if its reservation is safely released.
        return True

    async def _async_clear_provisional_ev_ownership(self) -> None:
        """Remove actuator-only ownership after a start is proven not to own power."""
        ownership = dict(self.store.data.get("ownership", {}))
        if ownership.get("ev_smart_charging_state"):
            return
        ownership.pop(_EV_COMMAND_ENTITY_OWNERSHIP_KEY, None)
        ownership.pop(_EV_CONTROL_TOPOLOGY_OWNERSHIP_KEY, None)
        await self.store.async_save_ownership(ownership)

    async def _async_flush_provisional_state(self) -> None:
        """Make provisional recovery state durable before a device command."""
        async_flush = getattr(self.store, "async_flush", None)
        if callable(async_flush):
            await async_flush()

    async def _async_persist_provisional_hvac_main_state(
        self,
        main_state: dict[str, Any],
    ) -> None:
        """Persist evolving main rollback evidence before a thermostat command."""
        ownership = deepcopy(dict(self.store.data.get("ownership", {})))
        hvac_control = dict(ownership.get("hvac_control", {}))
        hvac_control[_HVAC_MAIN_STATE_OWNERSHIP_KEY] = dict(main_state)
        ownership["hvac_control"] = hvac_control
        await self.store.async_save_ownership(ownership)
        await self._async_flush_provisional_state()

    async def _async_persist_provisional_hvac_manual_supersession(self) -> None:
        """Durably discard a main baseline superseded by a user command."""
        await self._async_persist_provisional_hvac_supersessions(True, set())

    async def _async_persist_provisional_hvac_zone_supersession(
        self,
        entity_ids: set[str],
    ) -> None:
        """Durably discard zone baselines superseded by user commands."""
        await self._async_persist_provisional_hvac_supersessions(
            False,
            entity_ids,
        )

    async def _async_persist_provisional_hvac_supersessions(
        self,
        main_superseded: bool,
        zone_entity_ids: set[str],
    ) -> None:
        """Atomically discard all user-superseded HVAC baselines."""
        ownership = deepcopy(dict(self.store.data.get("ownership", {})))
        hvac_control = dict(ownership.get("hvac_control", {}))
        if main_superseded:
            hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
        zone_states = dict(hvac_control.get("zone_states", {}))
        for entity_id in zone_entity_ids:
            zone_states.pop(entity_id, None)
        hvac_control["zone_states"] = zone_states
        ownership["hvac_control"] = hvac_control
        await self.store.async_save_ownership(ownership)
        await self._async_flush_provisional_state()

    def _configure_pending_hvac_adapter(
        self,
        adapter: Any,
        pending: dict[str, Any],
    ) -> None:
        """Wire one adapter to the shared pending-transaction protocol."""
        set_manual_override_check = getattr(
            adapter,
            "set_manual_override_check",
            None,
        )
        if callable(set_manual_override_check):
            set_manual_override_check(
                lambda: pending.get(_PENDING_HVAC_MANUAL_OVERRIDE_KEY) is True
            )
        set_manual_override_persistence = getattr(
            adapter,
            "set_manual_override_persistence_callback",
            None,
        )
        if callable(set_manual_override_persistence):
            set_manual_override_persistence(
                self._async_persist_provisional_hvac_manual_supersession
            )
        set_zone_manual_override_check = getattr(
            adapter,
            "set_zone_manual_override_check",
            None,
        )
        if callable(set_zone_manual_override_check):
            set_zone_manual_override_check(
                lambda: {
                    str(entity_id)
                    for entity_id in pending.get(
                        _PENDING_HVAC_MANUAL_ZONE_IDS_KEY,
                        [],
                    )
                    if str(entity_id)
                }
            )
        set_zone_manual_override_persistence = getattr(
            adapter,
            "set_zone_manual_override_persistence_callback",
            None,
        )
        if callable(set_zone_manual_override_persistence):
            set_zone_manual_override_persistence(
                self._async_persist_provisional_hvac_zone_supersession
            )
        set_manual_supersession_persistence = getattr(
            adapter,
            "set_manual_supersession_persistence_callback",
            None,
        )
        if callable(set_manual_supersession_persistence):
            set_manual_supersession_persistence(
                self._async_persist_provisional_hvac_supersessions
            )
        set_turn_on_feedback = getattr(
            adapter,
            "set_turn_on_feedback_callback",
            None,
        )
        if callable(set_turn_on_feedback):
            set_turn_on_feedback(
                lambda expected: pending.__setitem__(
                    "turn_on_feedback_expected",
                    expected,
                )
            )
        set_pending_main_restore = getattr(
            adapter,
            "set_pending_main_restore_callback",
            None,
        )
        if callable(set_pending_main_restore):
            set_pending_main_restore(
                lambda saved_state: pending.__setitem__(
                    "restore_main",
                    dict(saved_state),
                )
            )
        set_pending_zone_restore = getattr(
            adapter,
            "set_pending_zone_restore_callback",
            None,
        )
        if callable(set_pending_zone_restore):
            set_pending_zone_restore(
                lambda saved_states: pending.__setitem__(
                    "restore_zones",
                    dict(saved_states),
                )
            )

    def _ev_entry_data_for_action(self, action: Any) -> dict[str, Any]:
        """Use the owned actuator topology for any stop that can release it."""
        if _ev_action_is_safety_stop(action):
            topology = self._owned_ev_control_topology()
            if topology is not None:
                return topology
        return self.entry_data

    def _ev_grid_domain_data(self) -> dict[str, Any] | None:
        """Return shared in-memory EV coordination state."""
        hass_data = getattr(self.hass, "data", None)
        if not isinstance(hass_data, dict):
            return None
        domain_data = hass_data.setdefault(DOMAIN, {})
        return domain_data if isinstance(domain_data, dict) else None

    def _ev_grid_shedding_claim(self) -> str | None:
        """Return the entry holding the atomic shared-limit shedding claim."""
        domain_data = self._ev_grid_domain_data()
        if domain_data is None:
            return None
        claim = domain_data.get(_EV_GRID_SHEDDING_CLAIM_KEY)
        if not isinstance(claim, str) or not claim:
            return None
        reservations = domain_data.get("ev_grid_reservations")
        if not isinstance(reservations, dict) or claim not in reservations:
            domain_data.pop(_EV_GRID_SHEDDING_CLAIM_KEY, None)
            return None
        config_entries = getattr(self.hass, "config_entries", None)
        async_entries = getattr(config_entries, "async_entries", None)
        if callable(async_entries):
            loaded_ids = {
                str(getattr(entry, "entry_id", ""))
                for entry in async_entries(DOMAIN)
                if getattr(entry, "runtime_data", None) is not None
            }
            if claim not in loaded_ids:
                # Retain the uncertain load, but let a loaded entry shed its own
                # controllable reservation when that can restore the limit.
                domain_data.pop(_EV_GRID_SHEDDING_CLAIM_KEY, None)
                return None
        return claim

    def _set_ev_grid_shedding_claim(self, entry_id: str) -> None:
        """Atomically claim shared-limit shedding for one planner entry."""
        domain_data = self._ev_grid_domain_data()
        if domain_data is not None:
            domain_data[_EV_GRID_SHEDDING_CLAIM_KEY] = entry_id

    def _clear_ev_grid_shedding_claim(self, entry_id: str) -> None:
        """Clear this entry's shared-limit shedding claim."""
        domain_data = self._ev_grid_domain_data()
        if domain_data is not None and domain_data.get(_EV_GRID_SHEDDING_CLAIM_KEY) == entry_id:
            domain_data.pop(_EV_GRID_SHEDDING_CLAIM_KEY, None)

    def _discard_stale_ev_grid_reservations(self, reservations: dict[str, dict[str, Any]]) -> None:
        """Discard reservations whose config entry is no longer loaded."""
        config_entries = getattr(self.hass, "config_entries", None)
        async_entries = getattr(config_entries, "async_entries", None)
        if not callable(async_entries):
            return
        loaded_ids = {
            str(getattr(entry, "entry_id", ""))
            for entry in async_entries(DOMAIN)
            if getattr(entry, "runtime_data", None) is not None
        }
        for entry_id in set(reservations) - loaded_ids:
            reservation = reservations.get(entry_id)
            if isinstance(reservation, dict) and reservation.get(EV_RESERVATION_RETAIN_WHEN_UNLOADED) is True:
                continue
            reservations.pop(entry_id, None)

    @staticmethod
    def _rejection_reason(plan: EnergyPlan) -> str | None:
        if plan.mode == PlannerMode.DISABLED:
            return "planner_disabled"
        if plan.mode == PlannerMode.ACTIVE_DEGRADED:
            return "input_health_degraded"
        return None

    def _ownership_from_store(self) -> OwnershipState:
        data = dict(self.store.data.get("ownership", {}))
        hvac_control = data.get("hvac_control")
        return OwnershipState(
            enphase_profile=data.get("enphase_profile"),
            enphase_profile_changed_at=_parse_datetime_or_none(data.get("enphase_profile_changed_at")),
            climate_automations=dict(data.get("climate_automations", {})),
            ev_smart_charging_state=dict(data.get("ev_smart_charging_state", {})),
            hvac_control_phase=(
                str(hvac_control.get("phase"))
                if isinstance(hvac_control, dict) and hvac_control.get("phase") is not None
                else None
            ),
            planner_takeover_started_at=_parse_datetime_or_none(data.get("planner_takeover_started_at")),
            manual_hvac_override_expires_at=_parse_datetime_or_none(data.get("manual_hvac_override_expires_at")),
        )


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _ev_action_wants_power(action: Any) -> bool:
    """Return whether an EV action asks the charger to remain energised."""
    if action.kind == ActionKind.EV_STOP:
        return False
    if action.kind == ActionKind.EV_START:
        return True
    if action.kind != ActionKind.EV_SCHEDULE:
        return False
    # Compatibility schedules predate charging_required_now and always start
    # charging after updating their external target/ready-by helpers. Keep this
    # in lockstep with EVChargerAdapter._async_schedule.
    return "charging_required_now" not in action.desired_state or bool(
        action.desired_state.get("charging_required_now")
    )


def _ev_action_is_safety_stop(action: Any) -> bool:
    """Return whether an EV action is attempting to remove charger power."""
    return action.asset == ActionAsset.EV and not _ev_action_wants_power(action)


def _ev_action_is_owned_safety_stop(action: Any) -> bool:
    """Return whether an EV stop is releasing planner-owned charger power."""
    return _ev_action_is_safety_stop(action) and bool(
        action.desired_state.get("ev_safety_stop") or action.desired_state.get("input_health_safety_stop")
    )


def _ev_result_proves_safe(result: Any) -> bool:
    """Return whether an EV result proves a stopped or restored safe state."""
    marker = getattr(result, "safe_state_confirmed", _MISSING)
    if marker is not _MISSING:
        return marker is True or getattr(result, "rollback_succeeded", None) is True
    # Compatibility for custom/legacy adapters that predate explicit safe-state
    # confirmation. Native adapter results always provide the marker.
    return (
        bool(getattr(result, "applied", False))
        or getattr(
            result,
            "rollback_succeeded",
            None,
        )
        is True
    )


def _normalized_ev_stop_result(
    result: Any,
    *,
    require_safe: bool,
) -> EVCommandResult:
    """Normalize manual-stop success when a safe state is authoritative."""
    safe_state_confirmed = _ev_result_proves_safe(result)
    raw_applied = bool(getattr(result, "applied", False))
    applied = safe_state_confirmed if require_safe else raw_applied or safe_state_confirmed
    reason = str(getattr(result, "reason", "ev_stop_failed"))
    if safe_state_confirmed and not raw_applied:
        reason = "ev_safe_stop_compensated"
    elif require_safe and raw_applied and not safe_state_confirmed:
        reason = "ev_stop_not_confirmed"
    if applied == raw_applied and reason == getattr(result, "reason", None):
        return result
    return EVCommandResult(
        applied,
        reason,
        getattr(result, "pre_state", {}),
        getattr(result, "post_state", {}),
        command_sent=bool(getattr(result, "command_sent", False)),
        rollback_succeeded=getattr(result, "rollback_succeeded", None),
        safe_state_confirmed=getattr(result, "safe_state_confirmed", None),
    )


def _ev_control_topology(entry_data: dict[str, Any]) -> dict[str, str]:
    """Return the EV actuator mapping that created planner ownership."""
    return {
        key: value for key in _EV_CONTROL_TOPOLOGY_KEYS if isinstance((value := entry_data.get(key)), str) and value
    }


def _positive_float(value: Any) -> float:
    """Return a non-negative finite float for reservation arithmetic."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(numeric, 0.0) if isfinite(numeric) else 0.0


def _planner_controlled_ev_reservation(value: Any) -> bool:
    """Return whether a reservation still represents planner stop authority."""
    return isinstance(value, dict) and value.get(EV_RESERVATION_EXTERNAL_BASELINE) is not True


def _restored_ev_baseline_is_active(saved_state: dict[str, Any]) -> bool:
    """Return whether restoration intentionally leaves persistent charger power on."""
    return any(
        str(saved_state.get(key, "")).strip().lower() in {"on", "true", "1"}
        for key in (CONF_EV_CHARGER, CONF_EV_SMART_CHARGING)
    )


def _command_rate_limit_key(action: Any) -> str:
    """Return the command cooldown key for an action."""
    return f"{action.asset}:{action.kind}"


def _service_target_for_action(action: Any, entry_data: dict[str, Any]) -> str | None:
    """Return the configured Home Assistant target an action would touch."""
    from .const import (
        CONF_DAIKIN_CLIMATE,
        CONF_ENPHASE_PROFILE,
        CONF_EV_CHARGER,
        CONF_EV_CHARGER_START,
        CONF_EV_CHARGER_STOP,
        CONF_EV_SMART_CHARGING,
        CONF_EV_SMART_CHARGING_START,
        CONF_EV_SMART_CHARGING_STOP,
    )

    if action.asset == ActionAsset.EV:
        if action.kind in {ActionKind.EV_START, ActionKind.EV_SCHEDULE}:
            if not _ev_action_wants_power(action):
                return (
                    entry_data.get(CONF_EV_CHARGER_STOP)
                    or entry_data.get(CONF_EV_CHARGER)
                    or entry_data.get(CONF_EV_SMART_CHARGING_STOP)
                    or entry_data.get(CONF_EV_SMART_CHARGING)
                )
            return (
                entry_data.get(CONF_EV_CHARGER_START)
                or entry_data.get(CONF_EV_CHARGER)
                or entry_data.get(CONF_EV_SMART_CHARGING_START)
                or entry_data.get(CONF_EV_SMART_CHARGING)
            )
        if action.kind == ActionKind.EV_STOP:
            return (
                entry_data.get(CONF_EV_CHARGER_STOP)
                or entry_data.get(CONF_EV_CHARGER)
                or entry_data.get(CONF_EV_SMART_CHARGING_STOP)
                or entry_data.get(CONF_EV_SMART_CHARGING)
            )
    if action.asset == ActionAsset.DAIKIN:
        return entry_data.get(CONF_DAIKIN_CLIMATE)
    if action.asset == ActionAsset.ENPHASE:
        entity = entry_data.get(CONF_ENPHASE_PROFILE)
        service = _profile_control_service_for_target(entry_data, entity)
        if service and entity:
            return f"{service}:{entity}"
        return service or entity
    return None


def _ev_command_entity_for_action(
    action: Any,
    entry_data: dict[str, Any],
) -> str | None:
    """Return the EV control that the adapter actually commands for an action."""
    if action.desired_state.get("keep_charger_on"):
        return entry_data.get(CONF_EV_CHARGER) or entry_data.get(CONF_EV_SMART_CHARGING)
    return _service_target_for_action(action, entry_data)


def _entity_id_from_service_target(target: str | None) -> str | None:
    """Return an entity ID from a service target string."""
    if not target:
        return None
    text = str(target)
    if ":" in text:
        text = text.split(":", 1)[1]
    return text if "." in text else None


def _latest_applied_audit_for_asset(audit: Any, asset: ActionAsset, now: datetime) -> dict[str, Any] | None:
    """Return the latest recent applied audit row for an asset."""
    if not isinstance(audit, list):
        return None
    cutoff = now - CONFLICT_DETECTION_WINDOW
    for item in reversed(audit):
        if not isinstance(item, dict) or item.get("asset") != str(asset):
            continue
        if item.get("result") != str(OutcomeResult.APPLIED):
            continue
        attempted_at = _parse_datetime_or_none(item.get("attempted_at"))
        if attempted_at is None or attempted_at < cutoff:
            continue
        return item
    return None


def _pause_rejection_reason(value: Any, action: Any, now: datetime) -> str | None:
    """Return shared fail-closed pause reason for an action."""
    return control_pause_reason(value, now, asset=str(action.asset))


def _ev_safety_stop_failure_pause(value: Any) -> bool:
    """Return whether a pause was created by a failed EV safety stop."""
    return isinstance(value, dict) and value.get("safety_stop_failure") is True


def _ev_safety_stop_attempt_limit_reached(
    audit: Any,
    now: datetime,
    *,
    additional_failures: int = 0,
) -> bool:
    """Bound failed automatic EV safety-stop commands over a rolling day."""
    if not isinstance(audit, list):
        return additional_failures >= MAX_EV_SAFETY_STOP_ATTEMPTS_PER_24H
    normalized_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    cutoff = normalized_now - EV_SAFETY_STOP_RETRY_WINDOW
    failures = max(int(additional_failures), 0)
    for item in audit:
        if not isinstance(item, dict):
            continue
        desired_state = item.get("desired_state")
        if (
            item.get("asset") != str(ActionAsset.EV)
            or item.get("kind") != str(ActionKind.EV_STOP)
            or item.get("result") != str(OutcomeResult.FAILED)
            or not isinstance(desired_state, dict)
            or desired_state.get("ev_safety_stop") is not True
        ):
            continue
        attempted_at = _parse_datetime_or_none(item.get("attempted_at"))
        if attempted_at is None:
            continue
        normalized_attempted_at = (
            attempted_at.replace(tzinfo=UTC)
            if attempted_at.tzinfo is None
            else attempted_at.astimezone(UTC)
        )
        if normalized_attempted_at >= cutoff:
            failures += 1
    return failures >= MAX_EV_SAFETY_STOP_ATTEMPTS_PER_24H


def _ev_safety_stop_backoff_active(
    audit: Any,
    now: datetime,
    *,
    limits: Any = None,
) -> bool:
    """Retain EV safety-stop backoff even if another asset overwrites the shared pause."""
    persisted_failure = (
        _parse_datetime_or_none(limits.get(_EV_SAFETY_STOP_FAILED_AT_KEY))
        if isinstance(limits, dict)
        else None
    )
    latest_failure = persisted_failure or _latest_ev_safety_stop_failure_at(audit)
    if latest_failure is None:
        return False
    normalized_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    normalized_failure = (
        latest_failure.replace(tzinfo=UTC)
        if latest_failure.tzinfo is None
        else latest_failure.astimezone(UTC)
    )
    return normalized_now < normalized_failure + ACTION_BACKOFF_DURATION


def _ev_safety_stop_block_active(limits: Any, now: datetime) -> bool:
    """Return whether the dedicated persisted EV safety-stop retry block is active."""
    if not isinstance(limits, dict):
        return False
    blocked_until = _parse_datetime_or_none(limits.get(_EV_SAFETY_STOP_BLOCKED_UNTIL_KEY))
    if blocked_until is None:
        return False
    normalized_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    normalized_until = (
        blocked_until.replace(tzinfo=UTC)
        if blocked_until.tzinfo is None
        else blocked_until.astimezone(UTC)
    )
    return normalized_now < normalized_until


def _latest_ev_safety_stop_failure_at(audit: Any) -> datetime | None:
    """Return the latest persisted failed automatic EV safety-stop time."""
    if not isinstance(audit, list):
        return None
    latest: datetime | None = None
    for item in audit:
        if not isinstance(item, dict):
            continue
        desired_state = item.get("desired_state")
        if (
            item.get("asset") != str(ActionAsset.EV)
            or item.get("kind") != str(ActionKind.EV_STOP)
            or item.get("result") != str(OutcomeResult.FAILED)
            or not isinstance(desired_state, dict)
            or desired_state.get("ev_safety_stop") is not True
        ):
            continue
        attempted_at = _parse_datetime_or_none(item.get("attempted_at"))
        if attempted_at is None:
            continue
        normalized = (
            attempted_at.replace(tzinfo=UTC)
            if attempted_at.tzinfo is None
            else attempted_at.astimezone(UTC)
        )
        if latest is None or normalized > latest:
            latest = normalized
    return latest


def _device_control_disabled_reason(asset: ActionAsset, options: dict[str, Any]) -> str | None:
    """Return device-specific disabled reason."""
    option_by_asset = {
        ActionAsset.EV: (CONF_EV_CONTROL_ENABLED, "ev_control_disabled"),
        ActionAsset.DAIKIN: (CONF_CLIMATE_CONTROL_ENABLED, "climate_control_disabled"),
        ActionAsset.ENPHASE: (CONF_ENPHASE_CONTROL_ENABLED, "enphase_control_disabled"),
    }
    option_key, reason = option_by_asset[asset]
    return None if strict_bool(options.get(option_key), default=False) else reason


def _daily_action_cap_reason(asset: ActionAsset, options: dict[str, Any], audit: Any, now: datetime) -> str | None:
    """Return daily action cap rejection reason for an asset."""
    option_by_asset = {
        ActionAsset.EV: (CONF_MAX_DAILY_EV_ACTIONS, "ev_daily_action_cap_reached"),
        ActionAsset.DAIKIN: (CONF_MAX_DAILY_CLIMATE_ACTIONS, "climate_daily_action_cap_reached"),
        ActionAsset.ENPHASE: (CONF_MAX_DAILY_ENPHASE_ACTIONS, "enphase_daily_action_cap_reached"),
    }
    option_key, reason = option_by_asset[asset]
    cap = int(options.get(option_key, 0) or 0)
    if cap <= 0:
        return None
    if not isinstance(audit, list):
        return None
    cutoff = now - timedelta(hours=24)
    count = 0
    for item in audit:
        if not isinstance(item, dict) or item.get("asset") != str(asset):
            continue
        item_kind = item.get("kind")
        if (
            asset == ActionAsset.DAIKIN
            and item_kind is not None
            and item_kind != str(ActionKind.SET_HVAC)
        ):
            # Releases restore previously owned state and must remain
            # available regardless of the command budget. In particular, a
            # no-ownership comfort handoff must never consume the allowance
            # needed for a later preconditioning start.
            continue
        attempted_at = _parse_datetime_or_none(item.get("attempted_at"))
        if attempted_at is None or attempted_at < cutoff:
            continue
        if item.get("result") in {str(OutcomeResult.APPLIED), str(OutcomeResult.FAILED), str(OutcomeResult.RESTORED)}:
            count += 1
    return reason if count >= cap else None


def _profile_control_service_for_target(entry_data: dict[str, Any], profile_entity: str | None) -> str | None:
    """Return the standard select service for an Enphase profile entity."""
    service = entry_data.get(CONF_ENPHASE_PROFILE_CONTROL_SERVICE)
    if service:
        return str(service)
    if not profile_entity or "." not in str(profile_entity):
        return None
    domain = str(profile_entity).split(".", 1)[0]
    if domain in {"select", "input_select"}:
        return f"{domain}.select_option"
    return None


def _restore_notification_message(reason: str) -> str:
    """Return a compact, redacted actionable restore-failure message."""
    clean = " ".join(str(reason).replace("\n", " ").split())
    if len(clean) > 500:
        clean = f"{clean[:497]}..."
    return (
        "Some planner-owned controls could not be restored. Check the mapped devices and retry. "
        f"Reason: {clean or 'not specified'}."
    )


def _plan_fallback_message(plan: EnergyPlan, summary: str, reason_codes: list[str]) -> str:
    """Return a compact, redacted plan fallback notification message."""
    codes = ", ".join(reason_codes[:8]) or "not specified"
    return f"{summary} Reason codes: {_truncate_notification_text(codes, 300)}."


def _actionable_input_issues(issues: list[str]) -> list[str]:
    """Return configuration/capability issues that normally require user action."""
    actionable_markers = (
        "_not_configured",
        "_invalid",
        "_unsupported",
        "_unavailable",
        "_forecast_failed",
        "_forecast_stale",
        "history_limit_exceeded",
        "unexpected_domain",
        "unknown_unit",
    )
    return [
        code
        for code in _clean_reason_codes(issues)
        if any(marker in code for marker in actionable_markers)
    ]


def _clean_reason_codes(codes: list[str]) -> list[str]:
    cleaned: list[str] = []
    for code in codes:
        value = " ".join(str(code).replace("\n", " ").split())
        if not value:
            continue
        cleaned.append(_truncate_notification_text(value, 80))
    return cleaned


def _truncate_notification_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(limit - 3, 0)]}..."
