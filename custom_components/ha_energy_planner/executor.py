"""Execution gate for Energy Planner."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_COMMAND_RATE_LIMIT_SECONDS,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_EV_CONTROL_ENABLED,
    CONF_MAX_DAILY_CLIMATE_ACTIONS,
    CONF_MAX_DAILY_ENPHASE_ACTIONS,
    CONF_MAX_DAILY_EV_ACTIONS,
)
from .constraints import ConstraintValidator
from .discovery import CapabilityDiscovery
from .enphase_adapter import EnphaseProfileAdapter
from .ev_adapter import EVSmartChargingAdapter
from .hvac_adapter import DaikinHVACAdapter
from .models import (
    ActionAsset,
    ActionKind,
    ActionOutcome,
    DecisionContext,
    EnergyPlan,
    OutcomeResult,
    PlannerMode,
)
from .ownership import OwnershipState
from .storage import PlannerStore

_PLAN_UNSAFE_NOTIFICATION_ID = "ha_energy_planner_plan_unsafe"
_GRID_LIMIT_NOTIFICATION_ID = "ha_energy_planner_grid_limit_fallback"
_HAEO_FALLBACK_NOTIFICATION_ID = "ha_energy_planner_haeo_fallback"
_PLAN_FALLBACK_NOTIFICATION_IDS = (
    _PLAN_UNSAFE_NOTIFICATION_ID,
    _GRID_LIMIT_NOTIFICATION_ID,
    _HAEO_FALLBACK_NOTIFICATION_ID,
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
    ) -> None:
        """Initialize executor."""
        self.store = store
        self.hass = hass
        self.entry_data = entry_data or {}
        self.options = options or {}

    async def async_evaluate(self, plan: EnergyPlan, context: DecisionContext | None = None) -> None:
        """Audit why an action was not executed."""
        action = plan.next_action
        if action is None:
            return
        now = dt_util.utcnow()
        if now < action.execute_not_before or now > action.execute_not_after:
            return
        ownership = self._ownership_from_store()
        if context is not None and self.options:
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
            capability = CapabilityDiscovery(self.hass, self.entry_data).inspect().for_asset(action.asset)
            if not capability.supported:
                await self.store.async_add_outcome(
                    self._action_outcome(
                        action,
                        now,
                        result=OutcomeResult.REJECTED,
                        reason=",".join(capability.issues),
                        pre_state={},
                        post_state={},
                        plan_id=plan.plan_id,
                    )
                )
                return
        reason = self._rejection_reason(plan)
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
        rate_limit_reason = self._rate_limit_reason(action, now)
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
            result = await EVSmartChargingAdapter(self.hass, self.entry_data).async_execute(action)
            await self._async_record_command_attempt(action, now)
            if result.applied:
                ownership = dict(self.store.data.get("ownership", {}))
                if "ev_smart_charging_state" not in ownership:
                    ownership["ev_smart_charging_state"] = result.pre_state
                    await self.store.async_save_ownership(ownership)
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
        if reason is None and action.asset == ActionAsset.DAIKIN and self.hass is not None:
            result = await DaikinHVACAdapter(self.hass, self.entry_data).async_execute(action)
            await self._async_record_command_attempt(action, now)
            if result.applied and result.reason != "already_in_desired_hvac_state":
                ownership_data = dict(self.store.data.get("ownership", {}))
                ownership_data["climate_automations"] = result.saved_automation_states
                ownership_data["planner_takeover_started_at"] = now
                ownership_data["planner_hvac_action_expires_at"] = now + timedelta(minutes=2)
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
        if reason is None and action.asset == ActionAsset.ENPHASE and self.hass is not None:
            result = await EnphaseProfileAdapter(self.hass, self.entry_data).async_execute(action)
            await self._async_record_command_attempt(action, now)
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

    async def async_restore_safe_state(self, reason: str) -> ActionOutcome:
        """Restore planner ownership state and notify the user."""
        now = dt_util.utcnow()
        ev_result = None
        hvac_result = None
        enphase_result = None
        ownership = dict(self.store.data.get("ownership", {}))
        if self.hass is not None:
            ev_state = dict(ownership.get("ev_smart_charging_state", {}))
            hvac_state = dict(ownership.get("climate_automations", {}))
            if ev_state:
                ev_result = await EVSmartChargingAdapter(self.hass, self.entry_data).async_restore(ev_state)
            if hvac_state:
                hvac_result = await DaikinHVACAdapter(self.hass, self.entry_data).async_restore(hvac_state)
            enphase_result = await EnphaseProfileAdapter(self.hass, self.entry_data).async_restore_ai()
        await self.store.async_clear_ownership()
        restore_failed = any(
            result is not None
            and not result.applied
            and "not_configured" not in result.reason
            and "unavailable" not in result.reason
            for result in (ev_result, hvac_result, enphase_result)
        )
        pre_state = {}
        post_state = {"ownership": "cleared"}
        reasons = [reason]
        for result in (ev_result, hvac_result, enphase_result):
            if result is None:
                continue
            reasons.append(result.reason)
            pre_state.update(result.pre_state)
            post_state.update(result.post_state)
        post_state["ownership"] = "cleared"
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
        await self._async_notify_restore(outcome)
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
    ) -> ActionOutcome:
        """Return an outcome enriched for the execution audit trail."""
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
            service_target=_service_target_for_action(action, self.entry_data),
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
        pause_reason = _pause_rejection_reason(self.store.data.get("control_pause"), action, now)
        if pause_reason is not None:
            return pause_reason
        production_value = self.store.data.get("production")
        if production_value is None:
            return None
        production = dict(production_value)
        if not production.get("armed"):
            return "production_gate_not_armed"
        device_reason = _device_control_disabled_reason(action.asset, self.options)
        if device_reason is not None:
            return device_reason
        cap_reason = _daily_action_cap_reason(action.asset, self.options, self.store.data.get("execution_audit"), now)
        if cap_reason is not None:
            return cap_reason
        return None

    async def _async_record_command_attempt(self, action: Any, attempted_at: datetime) -> None:
        """Persist the latest command attempt timestamp for rate limiting."""
        limits = dict(self.store.data.get("command_rate_limits", {}))
        limits[_command_rate_limit_key(action)] = attempted_at
        await self.store.async_save_command_rate_limits(limits)

    async def async_notify_plan_fallback(self, plan: EnergyPlan, violations: list[str]) -> None:
        """Create persistent notifications for major plan fallback classes."""
        clean_violations = _clean_reason_codes(violations)
        grid_violations = [
            code for code in clean_violations if code in {"grid_import_limit_exceeded", "grid_export_limit_exceeded"}
        ]
        haeo_issues = _haeo_fallback_issues(plan.input_issues)
        if plan.mode in {PlannerMode.DISABLED, PlannerMode.DRY_RUN}:
            await self._async_dismiss_notifications(_PLAN_FALLBACK_NOTIFICATION_IDS)
            return
        if "input_health_unsafe" in violations:
            await self._async_create_notification(
                title="Energy Planner plan unsafe",
                message=_plan_fallback_message(
                    plan,
                    "Required inputs are stale, missing, or invalid. Device control remains blocked.",
                    clean_violations,
                ),
                notification_id=_PLAN_UNSAFE_NOTIFICATION_ID,
            )
        else:
            await self._async_dismiss_notification(_PLAN_UNSAFE_NOTIFICATION_ID)
        if grid_violations:
            await self._async_create_notification(
                title="Energy Planner grid limit fallback",
                message=_plan_fallback_message(
                    plan,
                    "The current plan would exceed a configured grid import/export hard limit.",
                    grid_violations,
                ),
                notification_id=_GRID_LIMIT_NOTIFICATION_ID,
            )
        else:
            await self._async_dismiss_notification(_GRID_LIMIT_NOTIFICATION_ID)
        if haeo_issues:
            await self._async_create_notification(
                title="Energy Planner HAEO fallback",
                message=_plan_fallback_message(
                    plan,
                    (
                        "HAEO did not return a healthy optimization result. "
                        "The deterministic fallback remains constrained."
                    ),
                    haeo_issues,
                ),
                notification_id=_HAEO_FALLBACK_NOTIFICATION_ID,
            )
        else:
            await self._async_dismiss_notification(_HAEO_FALLBACK_NOTIFICATION_ID)

    async def _async_notify_restore(self, outcome: ActionOutcome) -> None:
        """Create a persistent notification for failsafe/manual restore."""
        await self._async_create_notification(
            title="Energy Planner restored safe state",
            message=_restore_notification_message(outcome.reason),
            notification_id="ha_energy_planner_restore_safe_state",
        )

    async def _async_notify_ev_infeasible(self, action: Any) -> None:
        """Create a persistent notification for infeasible EV ready-by plans."""
        if action.asset != ActionAsset.EV or not action.desired_state.get("infeasible"):
            return
        await self._async_create_notification(
            title="Energy Planner EV target infeasible",
            message=(
                "The EV cannot reach the requested ready-by target with the current "
                f"schedule. Planned target: {action.desired_state.get('target_soc_percent')}%. "
                f"Ready by: {action.desired_state.get('ready_by', 'not configured')}."
            ),
            notification_id=f"ha_energy_planner_ev_infeasible_{action.plan_id}",
        )

    async def _async_create_notification(self, *, title: str, message: str, notification_id: str) -> None:
        """Create a persistent notification if the service is available."""
        if self.hass is None:
            return
        services = getattr(self.hass, "services", None)
        has_service = getattr(services, "has_service", None)
        if callable(has_service) and not has_service("persistent_notification", "create"):
            return
        with suppress(Exception):
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

    async def _async_dismiss_notifications(self, notification_ids: tuple[str, ...]) -> None:
        """Dismiss persistent notifications if the service is available."""
        for notification_id in notification_ids:
            await self._async_dismiss_notification(notification_id)

    async def _async_dismiss_notification(self, notification_id: str) -> None:
        """Dismiss a persistent notification if the service is available."""
        if self.hass is None:
            return
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

    @staticmethod
    def _rejection_reason(plan: EnergyPlan) -> str | None:
        if plan.mode == PlannerMode.DRY_RUN:
            return "dry_run"
        if plan.mode == PlannerMode.DISABLED:
            return "planner_disabled"
        if plan.mode == PlannerMode.ACTIVE_DEGRADED:
            return "input_health_degraded"
        return None

    def _ownership_from_store(self) -> OwnershipState:
        data = dict(self.store.data.get("ownership", {}))
        return OwnershipState(
            enphase_profile=data.get("enphase_profile"),
            enphase_profile_changed_at=_parse_datetime_or_none(data.get("enphase_profile_changed_at")),
            climate_automations=dict(data.get("climate_automations", {})),
            ev_smart_charging_state=dict(data.get("ev_smart_charging_state", {})),
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


def _command_rate_limit_key(action: Any) -> str:
    """Return the command cooldown key for an action."""
    return f"{action.asset}:{action.kind}"


def _service_target_for_action(action: Any, entry_data: dict[str, Any]) -> str | None:
    """Return the configured Home Assistant target an action would touch."""
    from .const import (
        CONF_DAIKIN_CLIMATE,
        CONF_ENPHASE_PROFILE,
        CONF_EV_SMART_CHARGING,
        CONF_EV_SMART_CHARGING_START,
        CONF_EV_SMART_CHARGING_STOP,
    )

    if action.asset == ActionAsset.EV:
        if action.kind in {ActionKind.EV_START, ActionKind.EV_SCHEDULE}:
            return entry_data.get(CONF_EV_SMART_CHARGING_START) or entry_data.get(CONF_EV_SMART_CHARGING)
        if action.kind == ActionKind.EV_STOP:
            return entry_data.get(CONF_EV_SMART_CHARGING_STOP) or entry_data.get(CONF_EV_SMART_CHARGING)
    if action.asset == ActionAsset.DAIKIN:
        return entry_data.get(CONF_DAIKIN_CLIMATE)
    if action.asset == ActionAsset.ENPHASE:
        entity = entry_data.get(CONF_ENPHASE_PROFILE)
        service = _profile_control_service_for_target(entry_data, entity)
        if service and entity:
            return f"{service}:{entity}"
        return service or entity
    return None


def _pause_rejection_reason(value: Any, action: Any, now: datetime) -> str | None:
    """Return pause reason when all controls or the action asset is paused."""
    if not isinstance(value, dict) or not value:
        return None
    until = _parse_datetime_or_none(value.get("until"))
    if until is None or now >= until:
        return None
    assets = value.get("assets")
    if assets is None:
        return "planner_paused"
    if isinstance(assets, str):
        asset_values = {assets}
    elif isinstance(assets, list):
        asset_values = {str(item) for item in assets}
    else:
        asset_values = set()
    if "all" in asset_values or str(action.asset) in asset_values:
        return f"{action.asset}_control_paused"
    return None


def _device_control_disabled_reason(asset: ActionAsset, options: dict[str, Any]) -> str | None:
    """Return device-specific disabled reason."""
    option_by_asset = {
        ActionAsset.EV: (CONF_EV_CONTROL_ENABLED, "ev_control_disabled"),
        ActionAsset.DAIKIN: (CONF_CLIMATE_CONTROL_ENABLED, "climate_control_disabled"),
        ActionAsset.ENPHASE: (CONF_ENPHASE_CONTROL_ENABLED, "enphase_control_disabled"),
    }
    option_key, reason = option_by_asset[asset]
    return None if bool(options.get(option_key, False)) else reason


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
    """Return a compact, redacted restore notification message."""
    clean = " ".join(str(reason).replace("\n", " ").split())
    if len(clean) > 500:
        clean = f"{clean[:497]}..."
    return (
        "Planner-owned EV, Enphase, and Daikin controls were restored where supported. "
        f"Reason: {clean or 'not specified'}."
    )


def _plan_fallback_message(plan: EnergyPlan, summary: str, reason_codes: list[str]) -> str:
    """Return a compact, redacted plan fallback notification message."""
    codes = ", ".join(reason_codes[:8]) or "not specified"
    return (
        f"{summary} Plan status: {plan.status}. Mode: {plan.mode}. "
        f"Reason codes: {_truncate_notification_text(codes, 300)}."
    )


def _haeo_fallback_issues(issues: list[str]) -> list[str]:
    return [code for code in _clean_reason_codes(issues) if code.startswith("haeo_") or "haeo" in code]


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
