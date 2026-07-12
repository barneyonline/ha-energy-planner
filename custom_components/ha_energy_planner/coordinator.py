"""Coordinator for Energy Planner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic, perf_counter
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
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BATTERY_SOC,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_DAIKIN_CLIMATE,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HAEO_OPTIMIZE_SERVICE,
    CONF_MANUAL_HVAC_OVERRIDE_MINUTES,
    CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT,
    CONF_PERSON_ENTITIES,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PV_FORECAST,
    CONF_WEATHER,
    DEBOUNCE_SECONDS,
    DEFAULT_HAEO_OPTIMIZE_SERVICE,
    DEFAULT_OPTIONS,
    DOMAIN,
    MIN_NON_MANUAL_REFRESH_INTERVAL_SECONDS,
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
from .models import EnergyPlan, HAEOSolvePhase, HAEOStatus, InputHealth, Override, PlannerMode, to_jsonable
from .planner import DryRunPlanner
from .recorder_import import async_import_ev_trip_history_from_recorder
from .storage import PlannerStore
from .thermal_model import thermal_model_summary, update_thermal_model
from .type_defs import EnergyPlannerConfigEntry

_LOGGER = logging.getLogger(__name__)

_MATERIAL_STATE_ATTRIBUTE_KEYS = frozenset(
    {
        "forecast",
        "forecasts",
        "data",
        "values",
        "detailed_forecast",
        "predictions",
        "pv_forecast_kw",
        "pv_estimate",
        "estimate",
        "baseline_load_forecast_kw",
        "load_kw",
        "load",
        "power",
        "watts",
        "value",
        "outdoor_temperature_forecast_c",
        "temperature",
        "native_temperature",
        "current_temperature",
        "temp",
        "confidence",
        "confidence_percent",
        "forecast_confidence",
        "forecast_confidence_percent",
        "unit_of_measurement",
        "unit",
        "temperature_unit",
        "forecast_interval_minutes",
        "interval_minutes",
        "resolution_minutes",
    }
)

# Only state that is consumed as a decision input may request a replan. Device
# command/result entities and high-frequency observation inputs deliberately do
# not appear here; they are sampled on the scheduled planning boundary.
_DECISION_INPUT_ENTITY_KEYS = frozenset(
    {
        CONF_AMBER_IMPORT_PRICE,
        CONF_AMBER_EXPORT_PRICE,
        CONF_PV_FORECAST,
        CONF_BASELINE_LOAD_FORECAST,
        CONF_CARBON_INTENSITY_FORECAST,
        CONF_BATTERY_SOC,
        CONF_ENPHASE_PROFILE,
        CONF_DAIKIN_CLIMATE,
        CONF_CLIMATE_TARGET_LOW,
        CONF_CLIMATE_TARGET_HIGH,
        CONF_PERSON_ENTITIES,
        CONF_EV_SOC,
        CONF_EV_CONNECTED,
        CONF_EV_SMART_CHARGING_TARGET_SOC,
        CONF_EV_SMART_CHARGING_READY_BY,
        CONF_WEATHER,
    }
)


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
        self._last_non_manual_refresh_requested_at: float | None = None
        self._pending_refresh_trigger = "startup"
        self._last_decision_fingerprint: str | None = None
        self._force_next_refresh = False
        self._refresh_counters: dict[str, int] = {
            "requested": 0,
            "completed": 0,
            "coalesced": 0,
            "fingerprint_skipped": 0,
        }
        self._refresh_completed_times: list[float] = []
        self._refresh_trigger_counts: dict[str, int] = {}
        self._last_phase_durations: dict[str, float] = {}
        self._haeo_adapter: HAEOAdapter | None = None
        self._ai_advice_task: asyncio.Task[None] | None = None
        self._ai_advice_fingerprint: str | None = None
        self._ai_current_plan_fingerprint: str | None = None
        self._ai_current_plan_safe = False
        self.last_refresh_metadata: dict[str, Any] = {}
        if bool(self.options.get(CONF_AI_ENABLED, False)):
            _LOGGER.warning(
                "AI advice is enabled; the selected provider may log bounded prompts independently. "
                "Review the provider logger configuration"
            )
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

    @property
    def refresh_metrics(self) -> dict[str, Any]:
        """Return bounded in-memory refresh telemetry for diagnostics."""
        now = monotonic()
        completed = [
            timestamp for timestamp in getattr(self, "_refresh_completed_times", []) if now - timestamp <= 3600
        ]
        self._refresh_completed_times = completed
        return {
            **dict(getattr(self, "_refresh_counters", {})),
            "refreshes_last_hour": len(completed),
            "trigger_counts": dict(getattr(self, "_refresh_trigger_counts", {})),
            "last_trigger": getattr(self, "last_refresh_metadata", {}).get(
                "trigger", getattr(self, "_pending_refresh_trigger", None)
            ),
            "last_duration_ms": getattr(self, "last_refresh_metadata", {}).get("duration_ms"),
            "phase_durations_ms": dict(getattr(self, "_last_phase_durations", {})),
        }

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
            now = dt_util.utcnow()
            if _is_planner_owned_control_feedback(entry_data, self.store.data, event, now):
                return
            if _is_manual_hvac_change(self.hass, entry_data, self.store.data, event, now):
                self.hass.async_create_task(self._async_handle_manual_hvac_change("daikin_state_changed"))
                return
            if _is_ev_history_state_change(entry_data, event):
                self.hass.async_create_task(self._async_record_ev_trip_event())
            if not _is_material_state_change(event, self.options):
                return
            self._schedule_debounced_refresh("state_change")

        self._unsub_listeners.append(async_track_state_change_event(self.hass, entity_ids, _handle_state_change))

    def async_shutdown(self) -> None:
        """Cancel listeners and pending debounced refresh."""
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._debounce_cancel = None
        if self._boundary_cancel is not None:
            self._boundary_cancel()
            self._boundary_cancel = None
        ai_task = getattr(self, "_ai_advice_task", None)
        if ai_task is not None and not ai_task.done():
            ai_task.cancel()
        self._ai_advice_task = None
        while self._unsub_listeners:
            self._unsub_listeners.pop()()

    @callback
    def _schedule_debounced_refresh(
        self,
        trigger: str = "state_change",
        *,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        """Coalesce repeated input changes into one coordinator refresh."""
        self._mark_replan_requested()
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._increment_refresh_counter("coalesced")

        delay = max(float(debounce_seconds), self._non_manual_refresh_delay())

        @callback
        def _refresh(now: Any) -> None:
            self._debounce_cancel = None
            self._pending_refresh_trigger = trigger
            self._last_non_manual_refresh_requested_at = monotonic()
            self._increment_refresh_counter("requested")
            self.hass.async_create_task(self.async_request_refresh())

        self._debounce_cancel = async_call_later(self.hass, delay, _refresh)

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
            # Preserve wall-clock boundaries; only the minimum-refresh floor
            # may delay this request, not the state-change debounce.
            self._schedule_debounced_refresh("interval_boundary", debounce_seconds=0)
            self._schedule_next_boundary_refresh()

        self._boundary_cancel = async_call_later(self.hass, delay, _refresh)

    async def _async_update_data(self) -> EnergyPlan:
        """Refresh planner data."""
        started = perf_counter()
        succeeded = False
        trigger = getattr(self, "_pending_refresh_trigger", "unknown")
        trigger_counts = getattr(self, "_refresh_trigger_counts", {})
        trigger_counts[trigger] = int(trigger_counts.get(trigger, 0)) + 1
        self._refresh_trigger_counts = trigger_counts
        try:
            async with self._planner_lock:
                async with self.store.async_delay_save():
                    result = await self._async_update_data_locked()
                    succeeded = True
                    self._increment_refresh_counter("succeeded")
                    return result
        finally:
            if not succeeded:
                self._increment_refresh_counter("failed")
            self._increment_refresh_counter("completed")
            completed_times = getattr(self, "_refresh_completed_times", [])
            completed_times.append(monotonic())
            self._refresh_completed_times = completed_times[-256:]
            self.last_refresh_metadata = {
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "succeeded": succeeded,
                "completed_at": dt_util.utcnow(),
                "trigger": trigger,
                "counters": dict(getattr(self, "_refresh_counters", {})),
                "phases": dict(getattr(self, "_last_phase_durations", {})),
            }

    async def _async_update_data_locked(self) -> EnergyPlan:
        """Refresh planner data while holding the planner lock."""
        preparation_started = perf_counter()
        started_generation = self._refresh_generation
        options = self.planner_options
        entry_data = self.entry_data
        decision_fingerprint = _decision_input_fingerprint(
            self.hass,
            entry_data,
            options,
            self.overrides,
            now=dt_util.utcnow(),
        )
        force_refresh = bool(getattr(self, "_force_next_refresh", False))
        self._force_next_refresh = False
        if (
            not force_refresh
            and decision_fingerprint == getattr(self, "_last_decision_fingerprint", None)
            and getattr(self, "data", None) is not None
        ):
            self._increment_refresh_counter("fingerprint_skipped")
            self._last_phase_durations = {"fingerprint_ms": round((perf_counter() - preparation_started) * 1000, 3)}
            return self.data
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
        preparation_ms = round((perf_counter() - preparation_started) * 1000, 3)
        haeo = self._get_haeo_adapter(entry_data)
        haeo_started = perf_counter()
        baseline_result = await haeo.async_solve_baseline(context)
        haeo_ms = (perf_counter() - haeo_started) * 1000
        baseline_call_metadata = dict(getattr(haeo, "last_call_metadata", {}))
        context.haeo_status = baseline_result.status
        baseline_evidence_counts = apply_haeo_response_to_context(context, baseline_result.response)
        planner = DryRunPlanner(options, thermal_model=thermal_model)
        planner_started = perf_counter()
        plan = await self.hass.async_add_executor_job(planner.create_plan, context)
        planner_ms = (perf_counter() - planner_started) * 1000
        projections = planner.project_flexible_loads(context)
        second_pass_result = None
        second_pass_skipped_reason = None
        second_pass_call_metadata: dict[str, Any] = {}
        second_pass_evidence_counts: dict[str, int] = {}
        if baseline_result.status != HAEOStatus.READY:
            plan.input_issues.append(baseline_result.reason)
        elif projections:
            if not bool(getattr(haeo, "supports_flexible_second_pass", True)):
                second_pass_skipped_reason = "haeo_flexible_projection_unsupported"
            else:
                second_pass_result = await haeo.async_solve_with_flexible_load(context, projections)
                haeo_ms += float(getattr(haeo, "last_call_metadata", {}).get("duration_ms", 0.0) or 0.0)
                second_pass_call_metadata = dict(getattr(haeo, "last_call_metadata", {}))
                if second_pass_result.status != HAEOStatus.READY:
                    context.haeo_status = second_pass_result.status
                    plan.input_issues.append(second_pass_result.reason)
                else:
                    second_pass_evidence_counts = apply_haeo_response_to_context(context, second_pass_result.response)
                    _reset_flexible_load_projections(context)
                    planner_started = perf_counter()
                    plan = await self.hass.async_add_executor_job(planner.create_plan, context)
                    planner_ms += (perf_counter() - planner_started) * 1000
        baseline_run = _haeo_phase_metadata(
            baseline_result,
            baseline_evidence_counts,
            baseline_call_metadata,
        )
        if second_pass_skipped_reason is not None:
            second_pass_run = _haeo_skipped_phase_metadata(second_pass_skipped_reason, haeo)
        elif second_pass_result is None:
            second_pass_run = None
        else:
            second_pass_run = _haeo_phase_metadata(
                second_pass_result,
                second_pass_evidence_counts,
                second_pass_call_metadata,
            )
        persistence_started = perf_counter()
        await self.store.async_add_haeo_run(
            {
                "created_at": context.created_at,
                "plan_id": context.plan_id,
                "baseline": baseline_run,
                "second_pass": second_pass_run,
                "flexible_projection_count": len(projections),
                "capabilities": _haeo_capability_metadata(haeo),
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
        await self.store.async_add_forecast_snapshot(
            {
                "created_at": context.created_at,
                "plan_id": context.plan_id,
                "input_health": context.input_health,
                "haeo_status": context.haeo_status,
                "haeo": {
                    "baseline": baseline_run,
                    "second_pass": second_pass_run,
                    "flexible_projection_count": len(projections),
                    "capabilities": _haeo_capability_metadata(haeo),
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
                "forecast_coverage": getattr(manager, "forecast_coverage_details", []),
                "input_issues": context.input_issues[:20],
                # Advice is intentionally generated after the plan commit so a
                # slow local provider cannot hold the coordinator refresh lock.
                "ai": None,
            }
        )
        await self._async_update_production_evidence(plan, violations)
        if plan.mode == PlannerMode.DRY_RUN:
            await self._async_record_dry_run_comparison(plan)
        result = await self._async_commit_plan_if_current(started_generation, plan, context, options)
        self._increment_refresh_counter("computed")
        self._last_phase_durations = {
            "inputs_ms": preparation_ms,
            "haeo_ms": round(haeo_ms, 3),
            "planner_ms": round(planner_ms, 3),
            "persistence_and_execution_ms": round((perf_counter() - persistence_started) * 1000, 3),
        }
        if started_generation == self._refresh_generation:
            self._last_decision_fingerprint = decision_fingerprint
            self._schedule_ai_advice(context, plan, entry_data, options)
        return result

    def _get_haeo_adapter(self, entry_data: dict[str, Any]) -> HAEOAdapter:
        """Reuse the HAEO adapter so unchanged solves can use its bounded cache."""
        service = entry_data.get(CONF_HAEO_OPTIMIZE_SERVICE) or DEFAULT_HAEO_OPTIMIZE_SERVICE
        configured_entry_id = entry_data.get("haeo_config_entry_id") or entry_data.get("haeo_entry_id")
        adapter = getattr(self, "_haeo_adapter", None)
        if (
            adapter is None
            or not isinstance(adapter, HAEOAdapter)
            or getattr(adapter, "optimize_service", service) != service
            or getattr(adapter, "haeo_config_entry_id", configured_entry_id) != configured_entry_id
        ):
            if configured_entry_id:
                adapter = HAEOAdapter(self.hass, service, str(configured_entry_id))
            else:
                adapter = HAEOAdapter(self.hass, service)
            self._haeo_adapter = adapter
        return adapter

    async def async_request_replan(self) -> None:
        """Request immediate refresh."""
        self._mark_forced_refresh("manual_replan")
        await self.async_request_refresh()

    async def async_set_ready_by(self, ready_by: str) -> None:
        """Set runtime ready-by override."""
        self.ready_by = ready_by
        entry_data = self.entry_data
        if entry_data.get(CONF_EV_SMART_CHARGING_READY_BY):
            await EVSmartChargingAdapter(self.hass, entry_data).async_set_ready_by(ready_by)
        self._mark_forced_refresh("ready_by_changed")
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
        self._mark_forced_refresh("manual_hvac_override")
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
            self._mark_forced_refresh("safe_state_restored")
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
        self._mark_forced_refresh("control_paused")
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
        self._mark_forced_refresh("control_resumed")
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
    def _mark_replan_requested(self, *, force: bool = False) -> None:
        """Mark that a newer planner result is expected."""
        self._refresh_generation += 1
        if force:
            self._force_next_refresh = True

    @callback
    def _mark_forced_refresh(self, trigger: str) -> None:
        """Attribute and mark an immediate service-driven refresh."""
        self._pending_refresh_trigger = trigger
        self._increment_refresh_counter("requested")
        self._mark_replan_requested(force=True)

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
        """Return AI advice only for safe, materially changed plans."""
        if plan.health == InputHealth.UNSAFE or plan.status == "unsafe" or plan.confidence <= 0:
            return (
                AIAdviceResult(
                    status="skipped",
                    accepted={},
                    rejected_reason="ai_skipped_unsafe_plan",
                    rejected_detail={
                        "reason": "ai_skipped_unsafe_plan",
                        "message": "AI advice was skipped because the plan is unsafe or has zero confidence.",
                    },
                    service_called=None,
                ),
                False,
            )
        plan_fingerprint = _material_plan_fingerprint(plan)
        if _latest_ai_plan_fingerprint(self.store.data.get("ai_recommendations")) == plan_fingerprint:
            return (
                AIAdviceResult(
                    status="skipped",
                    accepted={},
                    rejected_reason="ai_plan_unchanged",
                    rejected_detail={
                        "reason": "ai_plan_unchanged",
                        "message": "AI advice was reused because the material plan has not changed.",
                    },
                    service_called=None,
                ),
                False,
            )
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
                                "AI advice was skipped because the last provider call was less than 5 minutes ago."
                            ),
                            "retry_after_seconds": remaining_seconds,
                            "last_called_at": last_called_at.isoformat(),
                        },
                        service_called=None,
                    ),
                    False,
                )
        result = await LocalAIAdvisor(self.hass, entry_data, options).async_get_advice(context, plan)
        # The caller persists provider metadata; attach the stable key without
        # widening the public AI result contract.
        result.rejected_detail.setdefault("plan_fingerprint", plan_fingerprint)
        return result, True

    @callback
    def _schedule_ai_advice(
        self,
        context: Any,
        plan: EnergyPlan,
        entry_data: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        """Schedule advisory work after plan commit with one in-flight call."""
        if (
            not bool(options.get(CONF_AI_ENABLED, False))
            or plan.health == InputHealth.UNSAFE
            or plan.status == "unsafe"
            or plan.confidence <= 0
        ):
            self._ai_current_plan_safe = False
            self._ai_current_plan_fingerprint = None
            current = getattr(self, "_ai_advice_task", None)
            if current is not None and not current.done():
                current.cancel()
            return
        fingerprint = _material_plan_fingerprint(plan)
        self._ai_current_plan_safe = True
        self._ai_current_plan_fingerprint = fingerprint
        if _latest_ai_plan_fingerprint(self.store.data.get("ai_recommendations")) == fingerprint:
            return
        current = getattr(self, "_ai_advice_task", None)
        if current is not None and not current.done():
            if getattr(self, "_ai_advice_fingerprint", None) == fingerprint:
                return
            current.cancel()
        self._ai_advice_fingerprint = fingerprint
        self._ai_advice_task = self.hass.async_create_task(
            self._async_run_ai_advice(context, plan, entry_data, options, fingerprint)
        )

    async def _async_run_ai_advice(
        self,
        context: Any,
        plan: EnergyPlan,
        entry_data: dict[str, Any],
        options: dict[str, Any],
        fingerprint: str,
    ) -> None:
        """Persist one bounded background advisory result and notify entities."""
        started = perf_counter()
        try:
            ai_result, should_store = await self._async_get_throttled_ai_advice(context, plan, entry_data, options)
            if (
                not should_store
                or self._ai_advice_fingerprint != fingerprint
                or not self._ai_current_plan_safe
                or self._ai_current_plan_fingerprint != fingerprint
            ):
                return
            async with self._planner_lock:
                if (
                    not self._ai_current_plan_safe
                    or self._ai_current_plan_fingerprint != fingerprint
                ):
                    return
                await self.store.async_add_ai_recommendation(
                    {
                        "created_at": context.created_at,
                        "plan_id": plan.plan_id,
                        "plan_fingerprint": fingerprint,
                        "plan_health": str(plan.health),
                        "status": ai_result.status,
                        "accepted": ai_result.accepted,
                        "rejected_reason": ai_result.rejected_reason,
                        "rejected_detail": ai_result.rejected_detail,
                        "service_called": ai_result.service_called,
                        CONF_AI_TASK_ENTITY: ai_result.ai_task_entity,
                    }
                )
            self._last_phase_durations["ai_background_ms"] = round((perf_counter() - started) * 1000, 3)
            self.async_update_listeners()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - advice must never fail the planner task.
            _LOGGER.exception("Background AI advice failed")
        finally:
            if getattr(self, "_ai_advice_task", None) is asyncio.current_task():
                self._ai_advice_task = None

    def _non_manual_refresh_delay(self) -> float:
        """Return delay needed to enforce the safe non-manual refresh cadence."""
        last_requested = getattr(self, "_last_non_manual_refresh_requested_at", None)
        if last_requested is None:
            return 0.0
        elapsed = monotonic() - last_requested
        return max(float(MIN_NON_MANUAL_REFRESH_INTERVAL_SECONDS) - elapsed, 0.0)

    def _increment_refresh_counter(self, key: str) -> None:
        """Increment in-memory refresh telemetry with test-object compatibility."""
        counters = getattr(self, "_refresh_counters", None)
        if counters is None:
            counters = {"requested": 0, "completed": 0, "coalesced": 0, "fingerprint_skipped": 0}
            self._refresh_counters = counters
        counters[key] = int(counters.get(key, 0)) + 1


def _configured_entity_ids(entry_data: dict[str, Any]) -> list[str]:
    """Return explicit decision-input entity IDs that may trigger replanning."""
    entity_ids: set[str] = set()
    for key in _DECISION_INPUT_ENTITY_KEYS:
        for entity_id in _split_entity_values(entry_data.get(key)):
            entity_ids.add(entity_id)
    return sorted(entity_ids)


def _decision_input_fingerprint(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    overrides: list[Override],
    *,
    now: datetime,
) -> str:
    """Return a stable fingerprint of decision state for one planning interval."""
    interval_seconds = max(int(options.get(CONF_PLANNING_INTERVAL_MINUTES, 5)), 1) * 60
    states: dict[str, Any] = {}
    for entity_id in _configured_entity_ids(entry_data):
        state = hass.states.get(entity_id)
        states[entity_id] = (
            None
            if state is None
            else {
                "state": getattr(state, "state", None),
                "attributes": _canonical_attributes(getattr(state, "attributes", {}) or {}),
            }
        )
    payload = {
        "interval_bucket": int(now.timestamp()) // interval_seconds,
        "states": states,
        "options": options,
        "overrides": to_jsonable(overrides),
    }
    encoded = json.dumps(to_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _material_plan_fingerprint(plan: EnergyPlan) -> str:
    """Return a stable key excluding generated plan IDs and timestamps."""
    payload = {
        "health": plan.health,
        "mode": plan.mode,
        "confidence": plan.confidence,
        "status": plan.status,
        "issues": sorted(plan.input_issues),
        "actions": [
            {
                "asset": action.asset,
                "kind": action.kind,
                "desired_state": action.desired_state,
                "reason_codes": action.reason_codes,
                "confidence": action.confidence,
            }
            for action in plan.actions
        ],
        "estimated_daily_cost": plan.estimated_daily_cost,
        "preview": _material_preview(plan.preview[:24]),
    }
    encoded = json.dumps(to_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _material_preview(value: Any) -> Any:
    """Remove refresh-relative timestamps while retaining decision values."""
    if isinstance(value, dict):
        return {
            str(key): _material_preview(item)
            for key, item in value.items()
            if str(key) not in {"valid_at", "created_at", "execute_not_before", "execute_not_after"}
        }
    if isinstance(value, list):
        return [_material_preview(item) for item in value]
    return to_jsonable(value)


def _latest_ai_plan_fingerprint(recommendations: Any) -> str | None:
    """Return the last stored material plan fingerprint."""
    if not isinstance(recommendations, list):
        return None
    for item in reversed(recommendations):
        if not isinstance(item, dict):
            continue
        # Transient skipped/rejected results must be retried after their rate
        # limit; only accepted advice is reusable by plan fingerprint.
        if item.get("status") != "accepted":
            continue
        fingerprint = item.get("plan_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            return fingerprint
        detail = item.get("rejected_detail")
        if isinstance(detail, dict) and isinstance(detail.get("plan_fingerprint"), str):
            return detail["plan_fingerprint"]
    return None


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


def _haeo_phase_metadata(
    result: Any,
    evidence_counts: dict[str, int],
    call_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return solve outcome, latency, cache, capability, and evidence metadata."""
    if evidence_counts:
        evidence_status = "available"
    elif getattr(result, "response", None) is not None:
        evidence_status = "response_without_forecast_evidence"
    else:
        evidence_status = "not_returned"
    return {
        "phase": result.phase,
        "status": result.status,
        "reason": result.reason,
        "service_called": result.service_called,
        "evidence_counts": evidence_counts,
        "evidence_status": evidence_status,
        "duration_ms": call_metadata.get("duration_ms"),
        "cache_hit": call_metadata.get("cache_hit"),
        "input_fingerprint": call_metadata.get("input_fingerprint"),
        "response_received": call_metadata.get("response_received"),
        "capabilities": call_metadata.get("capabilities", {}),
    }


def _haeo_skipped_phase_metadata(reason: str, adapter: Any) -> dict[str, Any]:
    """Return explicit metadata when a capability-safe second pass is skipped."""
    return {
        "phase": HAEOSolvePhase.FLEXIBLE_LOAD,
        "status": "skipped",
        "reason": reason,
        "service_called": getattr(adapter, "optimize_service", None),
        "evidence_counts": {},
        "evidence_status": "not_requested",
        "duration_ms": 0.0,
        "cache_hit": False,
        "input_fingerprint": None,
        "response_received": False,
        "capabilities": _haeo_capability_metadata(adapter),
    }


def _haeo_capability_metadata(adapter: Any) -> dict[str, Any]:
    """Return diagnostic-safe adapter capabilities with fake-adapter compatibility."""
    capabilities = getattr(adapter, "capabilities", None)
    as_dict = getattr(capabilities, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    return dict(capabilities) if isinstance(capabilities, dict) else {}


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
    return True


def _is_planner_owned_control_feedback(
    entry_data: dict[str, Any],
    store_data: dict[str, Any],
    event: Any,
    now: datetime,
) -> bool:
    """Return whether a control-state event follows a recent planner command."""
    entity_id = event.data.get("entity_id")
    asset = (
        "daikin"
        if entity_id == entry_data.get(CONF_DAIKIN_CLIMATE)
        else "enphase"
        if entity_id == entry_data.get(CONF_ENPHASE_PROFILE)
        else None
    )
    if asset is None:
        return False
    new_state = event.data.get("new_state")
    if new_state is None:
        return False
    for outcome in reversed(list(store_data.get("execution_audit", []))):
        if not isinstance(outcome, dict) or outcome.get("result") != "applied" or outcome.get("asset") != asset:
            continue
        attempted_at = _parse_datetime_or_none(outcome.get("attempted_at"))
        if attempted_at is None or not attempted_at <= now < attempted_at + timedelta(minutes=2):
            continue
        desired = outcome.get("desired_state")
        if not isinstance(desired, dict):
            continue
        observed = str(getattr(new_state, "state", ""))
        if asset == "enphase":
            return bool(desired.get("profile")) and observed == str(desired["profile"])
        desired_mode = desired.get("hvac_mode")
        if desired_mode is not None and observed != str(desired_mode):
            return False
        desired_temperature = desired.get("target_temperature")
        if desired_temperature is not None:
            attributes = getattr(new_state, "attributes", {}) or {}
            observed_temperature = attributes.get("temperature")
            try:
                if float(observed_temperature) != float(desired_temperature):
                    return False
            except (TypeError, ValueError):
                return False
        return desired_mode is not None or desired_temperature is not None
    return False


def _is_material_state_change(event: Any, options: dict[str, Any]) -> bool:
    """Return whether a state-change event should trigger replanning."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return True
    old_value = getattr(old_state, "state", None)
    new_value = getattr(new_state, "state", None)
    if _material_attributes_changed(old_state, new_state):
        return True
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


def _material_attributes_changed(old_state: Any, new_state: Any) -> bool:
    """Return whether an input attribute consumed by planning changed."""
    old_attributes = _canonical_attributes(getattr(old_state, "attributes", {}) or {})
    new_attributes = _canonical_attributes(getattr(new_state, "attributes", {}) or {})
    return any(old_attributes.get(key) != new_attributes.get(key) for key in _MATERIAL_STATE_ATTRIBUTE_KEYS)


def _canonical_attributes(attributes: Any) -> dict[str, Any]:
    """Return attributes with the same camelCase aliases accepted by forecast parsing."""
    canonical = dict(attributes)
    for key, value in attributes.items():
        raw = str(key)
        separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
        separated = re.sub(r"[^0-9A-Za-z]+", "_", separated)
        canonical.setdefault(separated.strip("_").lower(), value)
    return canonical


def _reset_flexible_load_projections(context: Any) -> None:
    """Clear planner-derived loads before regenerating a plan."""
    for slot in context.slots:
        slot.projected_ev_load_kw = 0.0
        slot.projected_hvac_load_kw = 0.0


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
