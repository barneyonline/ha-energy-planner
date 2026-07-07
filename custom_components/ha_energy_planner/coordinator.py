"""Coordinator for Energy Planner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .ai_advisor import AIAdviceResult, LocalAIAdvisor
from .const import (
    AI_ADVICE_MIN_INTERVAL_SECONDS,
    CONF_AI_ENABLED,
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_DAIKIN_CLIMATE,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SOC,
    CONF_HAEO_OPTIMIZE_SERVICE,
    CONF_MANUAL_HVAC_OVERRIDE_MINUTES,
    CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT,
    CONF_PERSON_ENTITIES,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
    DEBOUNCE_SECONDS,
    DEFAULT_HAEO_OPTIMIZE_SERVICE,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from .constraints import ConstraintValidator
from .discovery import CapabilityDiscovery
from .entry_data import combined_entry_data
from .ev import update_trip_history_from_values
from .ev_adapter import EVSmartChargingAdapter
from .executor import PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE, Executor
from .forecast_calibration import update_forecast_calibration
from .haeo_adapter import HAEOAdapter, apply_haeo_response_to_context
from .inputs import InputManager
from .models import EnergyPlan, HAEOStatus, Override, PlannerMode, to_jsonable
from .planner import DryRunPlanner
from .recorder_import import async_import_ev_trip_history_from_recorder
from .storage import PlannerStore
from .thermal_model import thermal_model_summary, update_thermal_model
from .type_defs import EnergyPlannerConfigEntry

_LOGGER = logging.getLogger(__name__)


class EnergyPlannerCoordinator(DataUpdateCoordinator[EnergyPlan | None]):
    """Manage planner refresh and entity state."""

    def __init__(self, hass: HomeAssistant, entry: EnergyPlannerConfigEntry, store: PlannerStore) -> None:
        """Initialize coordinator."""
        self.entry = entry
        self.store = store
        self.overrides: list[Override] = _overrides_from_store(store.data, dt_util.utcnow())
        self.ready_by = str(self.options.get(CONF_DEFAULT_READY_BY, "07:00"))
        self.executor = Executor(
            store,
            hass=hass,
            entry_data=self.entry_data,
            options=self.options,
            notification_grace_until=dt_util.utcnow() + PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE,
        )
        self._unsub_listeners: list[Callable[[], None]] = []
        self._debounce_cancel: Callable[[], None] | None = None
        self._boundary_cancel: Callable[[], None] | None = None
        self._planner_lock = asyncio.Lock()
        self._refresh_generation = 0
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=None,
        )

    @property
    def options(self) -> dict[str, Any]:
        """Return merged options."""
        return {**DEFAULT_OPTIONS, **dict(self.entry.options)}

    @property
    def planner_options(self) -> dict[str, Any]:
        """Return options including runtime service overrides used by planning."""
        return {**self.options, CONF_DEFAULT_READY_BY: self.ready_by}

    @property
    def entry_data(self) -> dict[str, Any]:
        """Return merged hub and input subentry data."""
        return combined_entry_data(self.entry)

    @property
    def planner_enabled(self) -> bool:
        """Return whether planner execution is enabled."""
        return bool(self.options.get(CONF_PLANNER_ENABLED, False))

    @property
    def dry_run(self) -> bool:
        """Return dry-run option state."""
        return bool(self.options.get(CONF_DRY_RUN, True))

    def async_start_listeners(self) -> None:
        """Start debounced state listeners for configured input entities."""
        self._schedule_next_boundary_refresh()
        entry_data = self.entry_data
        entity_ids = _configured_entity_ids(entry_data)
        if not entity_ids:
            return

        @callback
        def _handle_state_change(event: Any) -> None:
            entry_data = self.entry_data
            if _is_manual_hvac_change(self.hass, entry_data, self.store.data, event, dt_util.utcnow()):
                self.hass.async_create_task(self._async_handle_manual_hvac_change("daikin_state_changed"))
                return
            if _is_ev_history_state_change(entry_data, event):
                self.hass.async_create_task(self._async_record_ev_trip_event())
            if not _is_material_state_change(event, self.options):
                return
            self._schedule_debounced_refresh()

        self._unsub_listeners.append(async_track_state_change_event(self.hass, entity_ids, _handle_state_change))

    def async_shutdown(self) -> None:
        """Cancel listeners and pending debounced refresh."""
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._debounce_cancel = None
        if self._boundary_cancel is not None:
            self._boundary_cancel()
            self._boundary_cancel = None
        while self._unsub_listeners:
            self._unsub_listeners.pop()()

    @callback
    def _schedule_debounced_refresh(self) -> None:
        """Coalesce repeated input changes into one coordinator refresh."""
        self._mark_replan_requested()
        if self._debounce_cancel is not None:
            self._debounce_cancel()

        @callback
        def _refresh(now: Any) -> None:
            self._debounce_cancel = None
            self.hass.async_create_task(self.async_request_refresh())

        self._debounce_cancel = async_call_later(self.hass, DEBOUNCE_SECONDS, _refresh)

    @callback
    def _schedule_next_boundary_refresh(self) -> None:
        """Schedule the next planning-interval boundary refresh."""
        if self._boundary_cancel is not None:
            self._boundary_cancel()
        delay = _seconds_until_next_interval_boundary(
            dt_util.utcnow(),
            int(self.options.get(CONF_PLANNING_INTERVAL_MINUTES, 5)),
        )

        @callback
        def _refresh(now: Any) -> None:
            self._boundary_cancel = None
            self._mark_replan_requested()
            self.hass.async_create_task(self.async_request_refresh())
            self._schedule_next_boundary_refresh()

        self._boundary_cancel = async_call_later(self.hass, delay, _refresh)

    async def _async_update_data(self) -> EnergyPlan:
        """Refresh planner data."""
        async with self._planner_lock:
            async with self.store.async_delay_save():
                return await self._async_update_data_locked()

    async def _async_update_data_locked(self) -> EnergyPlan:
        """Refresh planner data while holding the planner lock."""
        started_generation = self._refresh_generation
        options = self.planner_options
        entry_data = self.entry_data
        self.executor.entry_data = entry_data
        discovery = CapabilityDiscovery(self.hass, entry_data).inspect()
        await self.store.async_save_discovery(discovery.as_dict())
        trip_history = dict(self.store.data.get("trip_history", {}))
        trip_history, trip_import_changed, trip_import_reason = await async_import_ev_trip_history_from_recorder(
            self.hass,
            entry_data,
            trip_history,
            now=dt_util.utcnow(),
        )
        if trip_import_changed:
            await self.store.async_save_trip_history(trip_history)
        manager = InputManager(
            self.hass,
            entry_data,
            options,
            trip_history=trip_history,
            forecast_calibration=dict(self.store.data.get("forecast_calibration", {})),
        )
        forecast_calibration, calibration_changed = update_forecast_calibration(
            dict(self.store.data.get("forecast_calibration", {})),
            list(self.store.data.get("forecast_snapshots", [])),
            manager.current_forecast_observations(),
            now=dt_util.utcnow(),
        )
        if calibration_changed:
            await self.store.async_save_forecast_calibration(forecast_calibration)
            manager.forecast_calibration = forecast_calibration
        context = manager.build_context(self.overrides)
        thermal_model, thermal_model_changed = update_thermal_model(
            dict(self.store.data.get("thermal_model", {})),
            dict(self.store.data.get("thermal_model", {})).get("last_sample"),
            manager.thermal_sample(context),
        )
        if thermal_model_changed:
            await self.store.async_save_thermal_model(thermal_model)
        haeo = HAEOAdapter(
            self.hass,
            entry_data.get(CONF_HAEO_OPTIMIZE_SERVICE) or DEFAULT_HAEO_OPTIMIZE_SERVICE,
        )
        baseline_result = await haeo.async_solve_baseline(context)
        context.haeo_status = baseline_result.status
        baseline_evidence_counts = apply_haeo_response_to_context(context, baseline_result.response)
        planner = DryRunPlanner(options, thermal_model=thermal_model)
        plan = await self.hass.async_add_executor_job(planner.create_plan, context)
        projections = await self.hass.async_add_executor_job(planner.project_flexible_loads, context)
        second_pass_result = None
        second_pass_evidence_counts: dict[str, int] = {}
        if baseline_result.status != HAEOStatus.READY:
            plan.input_issues.append(baseline_result.reason)
        elif projections:
            second_pass_result = await haeo.async_solve_with_flexible_load(context, projections)
            if second_pass_result.status != HAEOStatus.READY:
                context.haeo_status = second_pass_result.status
                plan.input_issues.append(second_pass_result.reason)
            else:
                second_pass_evidence_counts = apply_haeo_response_to_context(context, second_pass_result.response)
        await self.store.async_add_haeo_run(
            {
                "created_at": context.created_at,
                "plan_id": context.plan_id,
                "baseline": {
                    "phase": baseline_result.phase,
                    "status": baseline_result.status,
                    "reason": baseline_result.reason,
                    "service_called": baseline_result.service_called,
                    "evidence_counts": baseline_evidence_counts,
                },
                "second_pass": None
                if second_pass_result is None
                else {
                    "phase": second_pass_result.phase,
                    "status": second_pass_result.status,
                    "reason": second_pass_result.reason,
                    "service_called": second_pass_result.service_called,
                    "evidence_counts": second_pass_evidence_counts,
                },
                "flexible_projection_count": len(projections),
            }
        )
        violations = ConstraintValidator(options).validate_plan(context, plan)
        if violations:
            plan.input_issues.extend(violations)
            if "input_health_unsafe" in violations:
                plan.status = "unsafe"
            if plan.mode == PlannerMode.ACTIVE_HEALTHY:
                plan.mode = PlannerMode.ACTIVE_DEGRADED
        await self.executor.async_notify_plan_fallback(plan, violations)
        ai_result = None
        if bool(options.get(CONF_AI_ENABLED, False)):
            ai_result, should_store_ai_result = await self._async_get_throttled_ai_advice(
                context, plan, entry_data, options
            )
            if should_store_ai_result:
                await self.store.async_add_ai_recommendation(
                    {
                        "created_at": context.created_at,
                        "plan_id": context.plan_id,
                        "status": ai_result.status,
                        "accepted": ai_result.accepted,
                        "rejected_reason": ai_result.rejected_reason,
                        "rejected_detail": ai_result.rejected_detail,
                        "service_called": ai_result.service_called,
                        CONF_AI_TASK_ENTITY: ai_result.ai_task_entity,
                    }
                )
        await self.store.async_add_forecast_snapshot(
            {
                "created_at": context.created_at,
                "plan_id": context.plan_id,
                "input_health": context.input_health,
                "haeo_status": context.haeo_status,
                "haeo": {
                    "baseline": {
                        "status": baseline_result.status,
                        "reason": baseline_result.reason,
                        "service_called": baseline_result.service_called,
                        "evidence_counts": baseline_evidence_counts,
                    },
                    "second_pass": None
                    if second_pass_result is None
                    else {
                        "status": second_pass_result.status,
                        "reason": second_pass_result.reason,
                        "service_called": second_pass_result.service_called,
                        "evidence_counts": second_pass_evidence_counts,
                    },
                    "flexible_projection_count": len(projections),
                },
                "slot_count": len(context.slots),
                "actions": _snapshot_actions(plan),
                "preview": plan.preview[:12],
                "trip_history": {
                    "recorder_import_reason": trip_import_reason,
                    "record_count": len(trip_history.get("records", [])),
                },
                "thermal_model": thermal_model_summary(thermal_model),
                "forecast_training_slots": manager.forecast_training_slots,
                "forecast_calibration": {
                    "pv_forecast_kw": _calibration_summary(forecast_calibration, "pv_forecast_kw"),
                    "baseline_load_forecast_kw": _calibration_summary(
                        forecast_calibration, "baseline_load_forecast_kw"
                    ),
                },
                "confidence": {
                    "overall": plan.confidence,
                    "forecast_source_confidence": getattr(context, "forecast_confidence", plan.confidence),
                    "sources": getattr(manager, "forecast_confidence_details", []),
                },
                "input_issues": context.input_issues[:20],
                "ai": None
                if ai_result is None
                else {
                    "status": ai_result.status,
                    "accepted_fields": sorted(ai_result.accepted),
                    "rejected_reason": ai_result.rejected_reason,
                    "rejected_detail": ai_result.rejected_detail,
                    "service_called": ai_result.service_called,
                    CONF_AI_TASK_ENTITY: ai_result.ai_task_entity,
                },
            }
        )
        await self._async_update_production_evidence(plan, violations)
        if plan.mode == PlannerMode.DRY_RUN:
            await self._async_record_dry_run_comparison(plan)
        return await self._async_commit_plan_if_current(started_generation, plan, context, options)

    async def async_request_replan(self) -> None:
        """Request immediate refresh."""
        self._mark_replan_requested()
        await self.async_request_refresh()

    async def async_set_ready_by(self, ready_by: str) -> None:
        """Set runtime ready-by override."""
        self.ready_by = ready_by
        entry_data = self.entry_data
        if entry_data.get(CONF_EV_SMART_CHARGING_READY_BY):
            await EVSmartChargingAdapter(self.hass, entry_data).async_set_ready_by(ready_by)
        self._mark_replan_requested()
        await self.async_request_refresh()

    async def async_set_manual_hvac_override(self, duration_minutes: int, reason: str) -> None:
        """Set a manual HVAC override."""
        expires_at = dt_util.utcnow() + timedelta(minutes=duration_minutes)
        self.overrides = [override for override in self.overrides if override.kind != "manual_hvac"]
        self.overrides.append(
            Override(
                kind="manual_hvac",
                source="service",
                expires_at=expires_at,
                reason=reason,
            )
        )
        await self.store.async_save_overrides(self.overrides)
        ownership = dict(self.store.data.get("ownership", {}))
        ownership["manual_hvac_override_expires_at"] = expires_at
        await self.store.async_save_ownership(ownership)
        manual_override_entity = self.entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
        if manual_override_entity:
            await self.hass.services.async_call(
                "input_boolean",
                "turn_on",
                {"entity_id": manual_override_entity},
                blocking=True,
            )
        self._mark_replan_requested()
        await self.async_request_refresh()

    async def _async_handle_manual_hvac_change(self, reason: str) -> None:
        """Record manual HVAC override from observed Daikin state change."""
        await self.async_set_manual_hvac_override(
            int(self.options[CONF_MANUAL_HVAC_OVERRIDE_MINUTES]),
            reason,
        )

    async def _async_record_ev_trip_event(self) -> None:
        """Record compact EV trip history from current connection/SOC states."""
        entry_data = self.entry_data
        connected = _bool_state_value(self.hass, entry_data.get(CONF_EV_CONNECTED))
        soc_percent = _float_state_value(self.hass, entry_data.get(CONF_EV_SOC))
        updated, changed = update_trip_history_from_values(
            dict(self.store.data.get("trip_history", {})),
            connected=connected,
            soc_percent=soc_percent,
            now=dt_util.utcnow(),
        )
        if changed:
            await self.store.async_save_trip_history(updated)

    async def async_restore_safe_state(self, reason: str, *, refresh: bool = True) -> None:
        """Restore safe state and refresh."""
        await self.executor.async_restore_safe_state(reason)
        if refresh:
            self._mark_replan_requested()
            await self.async_request_refresh()

    async def async_arm_production_control(self, reason: str = "user_acknowledged") -> None:
        """Arm production control after operator acknowledgement."""
        production = dict(self.store.data.get("production", {}))
        now = dt_util.utcnow()
        production.update(
            {
                "armed": True,
                "armed_at": now,
                "armed_reason": reason,
                "acknowledged_at": now,
            }
        )
        await self._async_save_production(production)
        self.async_update_listeners()

    async def async_disarm_production_control(self, reason: str = "user_requested") -> None:
        """Disarm production control."""
        production = dict(self.store.data.get("production", {}))
        production.update(
            {
                "armed": False,
                "disarmed_at": dt_util.utcnow(),
                "disarmed_reason": reason,
            }
        )
        await self._async_save_production(production)
        self.async_update_listeners()

    async def async_pause_control(self, duration_minutes: int, reason: str, asset: str = "all") -> None:
        """Pause planner-owned active control for all devices or one asset."""
        normalized_asset = asset if asset in {"all", "ev", "daikin", "enphase"} else "all"
        pause = {
            "active": True,
            "assets": ["all"] if normalized_asset == "all" else [normalized_asset],
            "until": dt_util.utcnow() + timedelta(minutes=duration_minutes),
            "reason": reason,
        }
        await self._async_save_control_pause(pause)
        self._mark_replan_requested()
        await self.async_request_refresh()

    async def async_resume_control(self, reason: str = "user_requested") -> None:
        """Resume planner-owned active control."""
        await self._async_save_control_pause(
            {
                "active": False,
                "resumed_at": dt_util.utcnow(),
                "reason": reason,
            }
        )
        self._mark_replan_requested()
        await self.async_request_refresh()

    async def _async_update_production_evidence(self, plan: EnergyPlan, violations: list[str]) -> None:
        """Track dry-run readiness evidence for the production gate."""
        production = dict(self.store.data.get("production", {}))
        if plan.mode == PlannerMode.DRY_RUN and plan.health.value == "healthy" and not violations:
            production["dry_run_ready_cycles"] = int(production.get("dry_run_ready_cycles", 0) or 0) + 1
            production["last_dry_run_ready_at"] = plan.created_at
        elif plan.health.value == "unsafe":
            production["last_blocking_reason"] = "input_health_unsafe"
        await self._async_save_production(production)

    async def _async_save_production(self, production: dict[str, object]) -> None:
        """Persist production gate state with compatibility for test stores."""
        save_production = getattr(self.store, "async_save_production", None)
        if callable(save_production):
            await save_production(production)
        else:
            self.store.data["production"] = to_jsonable(production)

    async def _async_save_control_pause(self, pause: dict[str, object]) -> None:
        """Persist control pause state with compatibility for test stores."""
        save_pause = getattr(self.store, "async_save_control_pause", None)
        if callable(save_pause):
            await save_pause(pause)
        else:
            self.store.data["control_pause"] = to_jsonable(pause)

    async def _async_record_dry_run_comparison(self, plan: EnergyPlan) -> None:
        """Record compact dry-run plan versus recent real outcomes context."""
        outcomes = list(self.store.data.get("execution_audit", []))
        comparison = {
            "created_at": plan.created_at,
            "plan_id": plan.plan_id,
            "planned_action_count": len(plan.actions),
            "next_action": None if plan.next_action is None else _snapshot_action(plan.next_action),
            "estimated_daily_cost": plan.estimated_daily_cost,
            "recent_outcome_count": len(outcomes[-10:]),
            "recent_outcomes": outcomes[-5:],
        }
        add_comparison = getattr(self.store, "async_add_dry_run_comparison", None)
        if callable(add_comparison):
            await add_comparison(comparison)
        else:
            comparisons = list(self.store.data.get("dry_run_comparisons", []))
            comparisons.append(to_jsonable(comparison))
            self.store.data["dry_run_comparisons"] = comparisons[-96:]

    @callback
    def _mark_replan_requested(self) -> None:
        """Mark that a newer planner result is expected."""
        self._refresh_generation += 1

    async def _async_commit_plan_if_current(
        self,
        started_generation: int,
        plan: EnergyPlan,
        context: Any,
        options: dict[str, Any],
    ) -> EnergyPlan:
        """Persist and execute only the newest planner result."""
        if started_generation != self._refresh_generation:
            _LOGGER.debug(
                "Discarding obsolete planner result %s from generation %s; current generation is %s",
                plan.plan_id,
                started_generation,
                self._refresh_generation,
            )
            if hasattr(self, "hass"):
                self.hass.async_create_task(self.async_request_refresh())
            return self.data or plan
        await self.store.async_save_plan(plan)
        self.executor.options = options
        self.executor.entry_data = self.entry_data
        await self.executor.async_evaluate(plan, context)
        return plan

    async def _async_get_throttled_ai_advice(
        self,
        context: Any,
        plan: EnergyPlan,
        entry_data: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[AIAdviceResult, bool]:
        """Return AI advice while limiting provider calls to once every five minutes."""
        last_called_at = _latest_ai_service_call_at(self.store.data.get("ai_recommendations"))
        if last_called_at is not None:
            elapsed = context.created_at - last_called_at
            if elapsed < timedelta(seconds=AI_ADVICE_MIN_INTERVAL_SECONDS):
                remaining_seconds = max(
                    int(AI_ADVICE_MIN_INTERVAL_SECONDS - elapsed.total_seconds()),
                    1,
                )
                return (
                    AIAdviceResult(
                        status="skipped",
                        accepted={},
                        rejected_reason="ai_rate_limited",
                        rejected_detail={
                            "reason": "ai_rate_limited",
                            "message": (
                                "AI advice was skipped because the last provider call "
                                "was less than 5 minutes ago."
                            ),
                            "retry_after_seconds": remaining_seconds,
                            "last_called_at": last_called_at.isoformat(),
                        },
                        service_called=None,
                    ),
                    False,
                )
        return await LocalAIAdvisor(self.hass, entry_data, options).async_get_advice(context, plan), True


def _configured_entity_ids(entry_data: dict[str, Any]) -> list[str]:
    """Return configured entity IDs that should trigger replanning."""
    entity_ids: set[str] = set()
    for key, value in entry_data.items():
        if key.endswith("_entity") or key in {CONF_CLIMATE_AUTOMATIONS, CONF_PERSON_ENTITIES}:
            for entity_id in _split_entity_values(value):
                entity_ids.add(entity_id)
    return sorted(entity_ids)


def _latest_ai_service_call_at(recommendations: Any) -> datetime | None:
    """Return the latest timestamp where an AI provider service was actually called."""
    if not isinstance(recommendations, list):
        return None
    for item in reversed(recommendations):
        if not isinstance(item, dict) or not item.get("service_called"):
            continue
        created_at = item.get("created_at")
        if isinstance(created_at, datetime):
            return created_at
        if isinstance(created_at, str):
            parsed = dt_util.parse_datetime(created_at)
            if isinstance(parsed, datetime):
                return parsed
    return None


def _seconds_until_next_interval_boundary(now: Any, interval_minutes: int) -> float:
    """Return seconds until the next wall-clock planning boundary."""
    interval_seconds = max(int(interval_minutes), 1) * 60
    elapsed_seconds = now.minute * 60 + now.second + (now.microsecond / 1_000_000)
    remainder = elapsed_seconds % interval_seconds
    if remainder == 0:
        return float(interval_seconds)
    return float(interval_seconds - remainder)


def _calibration_summary(model: dict[str, Any], field: str) -> dict[str, Any]:
    calibration = dict(model.get(field, {}))
    return {
        "enabled": bool(calibration.get("enabled", False)),
        "factor": calibration.get("factor"),
        "sample_count": calibration.get("sample_count", 0),
    }


def _snapshot_actions(plan: EnergyPlan) -> list[dict[str, Any]]:
    """Return bounded action metadata for forecast/audit snapshots."""
    return [_snapshot_action(action) for action in plan.actions[:8]]


def _snapshot_action(action: Any) -> dict[str, Any]:
    """Return bounded action metadata for snapshots."""
    return {
        "action_id": action.action_id,
        "asset": str(action.asset),
        "kind": str(action.kind),
        "execute_not_before": action.execute_not_before.isoformat(),
        "execute_not_after": action.execute_not_after.isoformat(),
        "desired_state": _bounded_json(action.desired_state),
        "hard_constraints": action.hard_constraints[:8],
        "reason_codes": action.reason_codes[:8],
        "expected_cost_delta": action.expected_cost_delta,
        "confidence": action.confidence,
        "requires_haeo_plan_id": action.requires_haeo_plan_id,
    }


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Convert snapshot values to bounded JSON-friendly shapes."""
    if depth >= 4:
        return "<truncated>"
    value = to_jsonable(value)
    if isinstance(value, dict):
        return {str(key): _bounded_json(item, depth=depth + 1) for key, item in list(value.items())[:16]}
    if isinstance(value, list):
        items = [_bounded_json(item, depth=depth + 1) for item in value[:12]]
        if len(value) > 12:
            items.append({"truncated_count": len(value) - 12})
        return items
    return value


def _split_entity_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if "." in item and item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if "." in str(item)]
    return []


def _is_manual_hvac_change(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    store_data: dict[str, Any],
    event: Any,
    now: Any,
) -> bool:
    """Return whether a state event represents a manual Daikin change."""
    climate_entity = entry_data.get(CONF_DAIKIN_CLIMATE)
    if not climate_entity or event.data.get("entity_id") != climate_entity:
        return False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None or old_state.state == new_state.state:
        return False
    guard_entity = entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER)
    if guard_entity:
        guard_state = hass.states.get(guard_entity)
        if guard_state is not None and str(guard_state.state).lower() in {"on", "true", "1"}:
            return False
    ownership = dict(store_data.get("ownership", {}))
    grace_until = _parse_datetime_or_none(ownership.get("planner_hvac_action_expires_at"))
    if grace_until is not None and now < grace_until:
        return False
    return True


def _is_material_state_change(event: Any, options: dict[str, Any]) -> bool:
    """Return whether a state-change event should trigger replanning."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return True
    old_value = getattr(old_state, "state", None)
    new_value = getattr(new_state, "state", None)
    if old_value == new_value:
        return False
    try:
        old_number = float(old_value)
        new_number = float(new_value)
    except (TypeError, ValueError):
        return True
    if not isfinite(old_number) or not isfinite(new_number):
        return True
    delta = abs(new_number - old_number)
    if old_number == 0:
        return delta > 0
    threshold_percent = float(options.get(CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT, 0.0))
    return (delta / abs(old_number)) * 100 >= threshold_percent


def _is_ev_history_state_change(entry_data: dict[str, Any], event: Any) -> bool:
    entity_id = event.data.get("entity_id")
    return entity_id in {
        entry_data.get(CONF_EV_CONNECTED),
        entry_data.get(CONF_EV_SOC),
    }


def _bool_state_value(hass: HomeAssistant, entity_id: Any) -> bool | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    value = str(state.state).lower()
    if value in {
        "on",
        "true",
        "1",
        "connected",
        "charging",
        "home",
        "plugged_in",
        "connected_not_charging",
        "fully_charged",
    }:
        return True
    if value in {
        "off",
        "false",
        "0",
        "disconnected",
        "not_home",
        "idle",
        "unplugged",
        "not_plugged_in",
        "vehicle_not_connected",
    }:
        return False
    return None


def _float_state_value(hass: HomeAssistant, entity_id: Any) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _parse_datetime_or_none(value: Any) -> Any | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        return value
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


def _overrides_from_store(store_data: dict[str, Any], now: Any) -> list[Override]:
    """Restore non-expired overrides from Store data."""
    restored: list[Override] = []
    for item in store_data.get("overrides", []):
        if not isinstance(item, dict):
            continue
        expires_at = _parse_datetime_or_none(item.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        restored.append(
            Override(
                kind=str(item.get("kind", "")),
                source=str(item.get("source", "store")),
                expires_at=expires_at,
                reason=str(item.get("reason", "")),
            )
        )
    return restored
