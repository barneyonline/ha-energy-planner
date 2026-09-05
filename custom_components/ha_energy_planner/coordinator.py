"""Coordinator for Energy Planner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import cached_property
from math import isfinite
from time import monotonic, perf_counter
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import advice_runtime, startup_recovery, task_lifecycle
from .adapter_helpers import async_call_device_service
from .advice_runtime import (
    _AI_ADVICE_NOTIFICATION_ID as _AI_ADVICE_NOTIFICATION_ID,
)
from .advice_runtime import (
    _ai_advice_notification_message as _ai_advice_notification_message,
)
from .advice_runtime import (
    _ai_recommendation_fingerprint as _ai_recommendation_fingerprint,
)
from .advice_runtime import (
    _latest_accepted_ai_recommendation as _latest_accepted_ai_recommendation,
)
from .advice_runtime import (
    _latest_ai_attempt_at as _latest_ai_attempt_at,
)
from .advice_runtime import (
    _latest_ai_plan_fingerprint as _latest_ai_plan_fingerprint,
)
from .advice_runtime import (
    _latest_ai_service_call_at as _latest_ai_service_call_at,
)
from .advice_runtime import (
    _material_plan_fingerprint as _material_plan_fingerprint,
)
from .advice_runtime import (
    _material_preview as _material_preview,
)
from .const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BATTERY_SOC,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_THRESHOLD,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
    CONF_MANUAL_HVAC_OVERRIDE_MINUTES,
    CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT,
    CONF_PERSON_ENTITIES,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_SECONDARY,
    CONF_WEATHER,
    DEBOUNCE_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    MIN_NON_MANUAL_REFRESH_INTERVAL_SECONDS,
    STATE_UNKNOWN_VALUES,
)
from .constraints import ConstraintValidator
from .discovery import CapabilityDiscovery
from .entry_data import combined_entry_data
from .ev import ev_charging_state
from .ev_adapter import EVCommandResult, EVSmartChargingAdapter
from .executor import PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE, Executor
from .forecast_calibration import update_forecast_calibration
from .inputs import InputManager
from .load_forecast import normalize_power_kw
from .models import (
    ActionOutcome,
    DecisionContext,
    EnergyPlan,
    InputHealth,
    Override,
    PlannerMode,
    to_jsonable,
)
from .planner import DryRunPlanner
from .preflight import (
    _control_area_report,
    _runtime_control_area_report,
    build_preflight_report,
    production_evidence_fingerprint,
)
from .recorder_import import (
    load_forecast_source_available,
)
from .safety import (
    DRY_RUN_READY_CYCLES_REQUIRED,
    control_pause_reason,
    parse_production_state,
    partition_control_areas_by_pause,
    strict_bool,
)
from .startup_recovery import (
    STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES as STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES,
)
from .startup_recovery import (
    STARTUP_AUTO_RECOVERY_REQUIRED_RUNS as STARTUP_AUTO_RECOVERY_REQUIRED_RUNS,
)
from .startup_recovery import (
    STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS as STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS,
)
from .startup_recovery import (
    STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS as STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS,
)
from .startup_recovery import (
    _action_outcome_failed as _action_outcome_failed,
)
from .startup_recovery import (
    _active_control_not_ready_reason as _active_control_not_ready_reason,
)
from .startup_recovery import (
    _startup_auto_recovery_prerequisites as _startup_auto_recovery_prerequisites,
)
from .startup_recovery import (
    _startup_auto_recovery_successful_runs as _startup_auto_recovery_successful_runs,
)
from .startup_recovery import (
    _startup_auto_recovery_validation_ready as _startup_auto_recovery_validation_ready,
)
from .storage import PlannerStore
from .thermal_model import thermal_model_summary, update_thermal_model
from .training import HistoryTraining, TrainingRequest, TrainingResult, training_request
from .type_defs import EnergyPlannerConfigEntry
from .weather import (
    _bounded_reason as _bounded_reason,
)
from .weather import (
    _normalize_hourly_forecast as _normalize_hourly_forecast,
)
from .weather import _parse_datetime_or_none as _parse_datetime_or_none
from .weather import (
    _weather_forecast_from_response as _weather_forecast_from_response,
)
from .weather import (
    async_weather_forecast,
)

_LOGGER = logging.getLogger(__name__)


EV_AUTO_START_COMPENSATION_RETRY_SECONDS = 30

_LOAD_FORECAST_TRAINING_DEFERRED_REASONS = frozenset(
    {
        "load_forecast_household_load_not_configured",
        "load_forecast_household_load_unavailable",
        "load_forecast_training_recent",
    }
)

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


def _updated_load_forecast_training_attempted(previously_attempted: bool, reason: str) -> bool:
    """Keep the startup attempt pending while training is deferred."""
    return previously_attempted or reason not in _LOAD_FORECAST_TRAINING_DEFERRED_REASONS


_HVAC_CONTROL_ATTRIBUTE_KEYS = frozenset(
    {
        "temperature",
        "target_temp_low",
        "target_temp_high",
        "preset_mode",
        "fan_mode",
        "swing_mode",
        "swing_horizontal_mode",
        "aux_heat",
    }
)
_ACTIVE_HVAC_MODES = frozenset(
    {"auto", "cool", "dry", "fan_only", "heat", "heat_cool"}
)

# Only state that is consumed as a decision input may request a replan. The EV
# charging-feedback entity is also observed so an unsolicited plug-in start can
# be stopped promptly; other command/result and high-frequency observation
# entities are sampled on the scheduled planning boundary.
_DECISION_INPUT_ENTITY_KEYS = frozenset(
    {
        CONF_AMBER_IMPORT_PRICE,
        CONF_AMBER_EXPORT_PRICE,
        CONF_PV_FORECAST,
        CONF_CARBON_INTENSITY_FORECAST,
        CONF_BATTERY_SOC,
        CONF_ENPHASE_PROFILE,
        CONF_DAIKIN_CLIMATE,
        CONF_CLIMATE_MANUAL_OVERRIDE,
        CONF_CLIMATE_ZONES,
        CONF_CLIMATE_TARGET_LOW,
        CONF_CLIMATE_TARGET_HIGH,
        CONF_PERSON_ENTITIES,
        CONF_EV_SOC,
        CONF_EV_CHARGING,
        CONF_EV_CONNECTED,
        CONF_EV_SMART_CHARGING_READY_BY,
        CONF_EV_SMART_CHARGING_TARGET_SOC,
        CONF_PV_FORECAST_SECONDARY,
        CONF_WEATHER,
    }
)


class EnergyPlannerCoordinator(DataUpdateCoordinator[EnergyPlan | None]):
    """Manage planner refresh and entity state."""

    def __init__(self, hass: HomeAssistant, entry: EnergyPlannerConfigEntry, store: PlannerStore) -> None:
        """Initialize coordinator."""
        self.entry = entry
        self.store = store
        now = dt_util.utcnow()
        self.overrides: list[Override] = _overrides_from_store(store.data, now)
        helper_override_expiry = now + timedelta(
            minutes=int(self.options[CONF_MANUAL_HVAC_OVERRIDE_MINUTES])
        )
        self.overrides = [
            Override(
                kind=override.kind,
                source=override.source,
                expires_at=helper_override_expiry,
                reason=override.reason,
            )
            if (
                override.kind == "manual_hvac"
                and override.source == "helper"
                and override.expires_at is None
                and override.reason == "manual_override_helper_on"
            )
            else override
            for override in self.overrides
        ]
        manual_override_entity = self.entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
        manual_override_state = hass.states.get(manual_override_entity) if manual_override_entity else None
        if (
            manual_override_state is not None
            and str(manual_override_state.state).lower() == "on"
            and not any(override.kind == "manual_hvac" for override in self.overrides)
            and not _expired_manual_hvac_state(store.data, now)
        ):
            # Setup performs its first refresh before listeners are registered.
            # Seed an externally enabled helper now so that refresh cannot cross
            # the manual-override boundary and acquire HVAC. Helper-driven
            # overrides use the same configured timeout as detected changes.
            self.overrides.append(
                Override(
                    kind="manual_hvac",
                    source="helper",
                    expires_at=helper_override_expiry,
                    reason="manual_override_helper_on",
                )
            )
        self.ready_by = str(self.options.get(CONF_DEFAULT_READY_BY, "07:00"))
        self.executor = Executor(
            store,
            hass=hass,
            entry_data=self.entry_data,
            options=self.options,
            notification_grace_until=dt_util.utcnow() + PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE,
            entry_id=getattr(entry, "entry_id", None),
            entry_title=getattr(entry, "title", None),
        )
        self._unsub_listeners: list[Callable[[], None]] = []
        self._debounce_cancel: Callable[[], None] | None = None
        self._boundary_cancel: Callable[[], None] | None = None
        self._ev_auto_start_retry_cancel: Callable[[], None] | None = None
        self._ev_auto_start_compensation_pending = False
        self._ev_auto_start_compensation_generation = 0
        self._planner_lock = asyncio.Lock()
        self._options_update_lock = asyncio.Lock()
        self._device_control_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._last_handled_options = dict(entry.options)
        self._last_control_mode_state = (self.planner_enabled, self.dry_run)
        self.entry_topology_signature: tuple[Any, ...] | None = None
        self._refresh_generation = 0
        self._last_non_manual_refresh_requested_at: float | None = None
        self._pending_refresh_trigger = "startup"
        self._last_decision_fingerprint: str | None = None
        self._last_decision_context: DecisionContext | None = None
        self._tearing_down = False
        self._force_next_refresh = False
        self._manual_override_helper_guard: tuple[str, datetime] | None = None
        self._weather_forecast_cache: dict[str, Any] = {}
        self.weather_forecast_diagnostics: dict[str, Any] = {}
        self._load_forecast_training_attempted = False
        self._refresh_completed_times: list[float] = []
        self._refresh_trigger_counts: dict[str, int] = {}
        self._last_phase_durations: dict[str, float] = {}
        self._ai_advice_task: asyncio.Task[None] | None = None
        self._plan_execution_task: asyncio.Task[None] | None = None
        self._pending_plan_execution: tuple[int, EnergyPlan, DecisionContext, dict[str, Any]] | None = None
        self._deferred_plan_execution: tuple[int, EnergyPlan, DecisionContext, dict[str, Any]] | None = None
        self._ai_advice_fingerprint: str | None = None
        self._ai_advice_pending_fingerprint: str | None = None
        self._ai_advice_pending_reason: str | None = None
        self._ai_current_plan_fingerprint: str | None = None
        self._ai_current_plan_safe = False
        self._startup_auto_recovery_authorized = False
        self._startup_auto_recovery_deadline: float | None = None
        self._startup_auto_recovery_task: asyncio.Task[None] | None = None
        self._startup_auto_recovery_start_unsub: Callable[[], None] | None = None
        self._startup_auto_recovery_wakeup = asyncio.Event()
        self._startup_auto_recovery_validation_active = False
        self._last_startup_auto_recovery_validation: dict[str, Any] | None = None
        self.last_refresh_metadata: dict[str, Any] = {}
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            config_entry=entry,
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
        return strict_bool(self.options.get(CONF_PLANNER_ENABLED), default=False)

    @property
    def dry_run(self) -> bool:
        """Return dry-run option state."""
        return strict_bool(self.options.get(CONF_DRY_RUN), default=True)

    @property
    def automatic_control_requested(self) -> bool:
        """Return whether the operator intends automatic control to run."""
        return self.planner_enabled and not self.dry_run

    @property
    def active_control(self) -> bool:
        """Return whether automatic device control is fully active."""
        production = parse_production_state(self.store.data.get("production"))
        return self.automatic_control_requested and production.armed

    @property
    def effective_control(self) -> bool:
        """Return whether current evidence permits automatic device commands."""
        if not self.active_control:
            return False
        return bool(build_preflight_report(self.hass, self).get("active_control_ready"))

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
        self._tearing_down = False
        training = self.__dict__.get("history_training")
        if training is not None and training.closed:
            self.__dict__.pop("history_training")
        self._schedule_next_boundary_refresh()
        entry_data = self.entry_data
        self._start_load_forecast_source_listener(entry_data)
        entity_ids = _configured_entity_ids(entry_data)
        if not entity_ids:
            return

        @callback
        def _handle_state_change(event: Any) -> None:
            self._wake_startup_auto_recovery()
            entry_data = self.entry_data
            now = dt_util.utcnow()
            executor = getattr(self, "executor", None)
            charging_entity = entry_data.get(CONF_EV_CHARGING)
            if charging_entity and event.data.get("entity_id") == charging_entity:
                new_state = event.data.get("new_state")
                new_value = getattr(new_state, "state", None)
                if (
                    new_value is not None
                    and str(new_value).strip().lower() not in STATE_UNKNOWN_VALUES
                    and ev_charging_state(new_value) is False
                ):
                    self._clear_ev_auto_start_compensation()
            pending_hvac_desired_state = getattr(
                executor,
                "pending_hvac_desired_state",
                None,
            )
            if _is_planner_owned_control_feedback(
                entry_data,
                self.store.data,
                event,
                now,
                pending_hvac_desired_state=pending_hvac_desired_state,
            ):
                return
            if _event_reports_ev_charging_started(entry_data, event):
                if self.active_control and strict_bool(
                    self.options.get(CONF_EV_CONTROL_ENABLED),
                    default=False,
                ):
                    if _consume_expected_ev_start_feedback(executor, now):
                        self._clear_ev_auto_start_compensation()
                        return
                    self._ev_auto_start_compensation_pending = True
                    self._async_create_listener_task(
                        self._async_compensate_ev_auto_start(
                            require_unowned=True,
                            generation=self._ev_auto_start_compensation_generation,
                        )
                    )
                    return
            if _is_pending_main_hvac_manual_change(
                entry_data,
                event,
                pending_hvac_desired_state,
            ):
                mark_manual_override = getattr(
                    executor,
                    "mark_pending_hvac_manual_override",
                    None,
                )
                if callable(mark_manual_override):
                    # This synchronous marker is observed inside the active
                    # adapter transaction before it can retry another main
                    # command. The durable override/release task necessarily
                    # waits for the coordinator command lock.
                    mark_manual_override()
                self._async_create_listener_task(
                    self._async_handle_manual_hvac_change(
                        "daikin_state_changed",
                        preserve_main_state=True,
                    )
                )
                return
            pending_zone_entity_id = _pending_zone_hvac_manual_change_entity_id(
                entry_data,
                event,
                pending_hvac_desired_state,
            )
            if pending_zone_entity_id is not None:
                mark_zone_manual_override = getattr(
                    executor,
                    "mark_pending_hvac_zone_manual_override",
                    None,
                )
                if callable(mark_zone_manual_override):
                    mark_zone_manual_override(pending_zone_entity_id)
                self._async_create_listener_task(
                    self._async_handle_manual_hvac_change(
                        "climate_zone_changed",
                        preserve_zone_entity_id=pending_zone_entity_id,
                    )
                )
                return
            if _is_manual_override_helper_change(entry_data, event):
                helper_state = str(getattr(event.data.get("new_state"), "state", "")).lower()
                guard = getattr(self, "_manual_override_helper_guard", None)
                if isinstance(guard, tuple) and len(guard) == 2 and guard[0] == helper_state and now < guard[1]:
                    self._manual_override_helper_guard = None
                    return
                self._manual_override_helper_guard = None
                self._async_create_listener_task(self._async_handle_manual_override_helper(helper_state == "on"))
                return
            if _is_manual_hvac_change(self.hass, entry_data, self.store.data, event, now):
                self._async_create_listener_task(
                    self._async_handle_manual_hvac_change(
                        "daikin_state_changed",
                        preserve_main_state=True,
                    )
                )
                return
            if _is_manual_hvac_zone_change(self.hass, entry_data, self.store.data, event):
                self._async_create_listener_task(
                    self._async_handle_manual_hvac_change(
                        "climate_zone_changed",
                        preserve_zone_entity_id=str(event.data.get("entity_id") or ""),
                    )
                )
                return
            if not _is_material_state_change(event, self.options):
                return
            self._schedule_debounced_refresh("state_change")

        self._unsub_listeners.append(async_track_state_change_event(self.hass, entity_ids, _handle_state_change))
        charging_entity = entry_data.get(CONF_EV_CHARGING)
        charging_state = self.hass.states.get(charging_entity) if charging_entity else None
        if (
            charging_state is not None
            and ev_charging_state(charging_state.state) is True
            and self.active_control
            and strict_bool(
                self.options.get(CONF_EV_CONTROL_ENABLED),
                default=False,
            )
        ):
            if _consume_expected_ev_start_feedback(
                getattr(self, "executor", None),
                dt_util.utcnow(),
            ):
                self._clear_ev_auto_start_compensation()
            else:
                self._ev_auto_start_compensation_pending = True
                self._async_create_listener_task(
                    self._async_compensate_ev_auto_start(
                        require_unowned=True,
                        generation=self._ev_auto_start_compensation_generation,
                    )
                )
        helper_entity = entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
        helper_state = self.hass.states.get(helper_entity) if helper_entity else None
        helper_value = None if helper_state is None else str(helper_state.state).lower()
        overrides = list(getattr(self, "overrides", []))
        helper_override_active = any(
            override.kind == "manual_hvac" and getattr(override, "source", None) == "helper" for override in overrides
        )
        if helper_value == "on" and not any(override.kind == "manual_hvac" for override in overrides):
            self._async_create_listener_task(self._async_handle_manual_override_helper(True))
        elif helper_value == "off" and helper_override_active:
            self._async_create_listener_task(self._async_handle_manual_override_helper(False))

    async_start_startup_auto_recovery = startup_recovery.async_start_startup_auto_recovery

    _wake_startup_auto_recovery = startup_recovery._wake_startup_auto_recovery

    def _start_load_forecast_source_listener(self, entry_data: dict[str, Any]) -> None:
        """Retry startup training once when the mapped load source appears."""
        if getattr(self, "_load_forecast_training_attempted", False):
            return
        load_entity = str(entry_data.get(CONF_HOUSEHOLD_LOAD, "") or "").strip()
        if not load_entity:
            return

        active = True
        unsubscribe: Callable[[], None] | None = None

        @callback
        def _retry_if_available(state: Any) -> None:
            nonlocal active, unsubscribe
            if not active or not load_forecast_source_available(state):
                return
            active = False
            if unsubscribe is not None:
                current_unsubscribe = unsubscribe
                unsubscribe = None
                if current_unsubscribe in self._unsub_listeners:
                    self._unsub_listeners.remove(current_unsubscribe)
                current_unsubscribe()
            self._schedule_debounced_refresh(
                "load_forecast_source_available",
                debounce_seconds=0,
                force=True,
            )

        @callback
        def _handle_source_change(event: Any) -> None:
            _retry_if_available(event.data.get("new_state"))

        unsubscribe = async_track_state_change_event(self.hass, [load_entity], _handle_source_change)
        self._unsub_listeners.append(unsubscribe)
        # Close the setup race where the source appears after the first refresh
        # but before this state-change listener is registered.
        _retry_if_available(self.hass.states.get(load_entity))

    _async_create_listener_task = task_lifecycle._async_create_listener_task

    _begin_shutdown = task_lifecycle._begin_shutdown

    async def async_shutdown(self) -> None:
        """Stop integration work and release coordinator resources."""
        self._begin_shutdown()
        await super().async_shutdown()

    async_wait_for_plan_execution = task_lifecycle.async_wait_for_plan_execution

    async_wait_for_refresh_shutdown = task_lifecycle.async_wait_for_refresh_shutdown

    @callback
    def _schedule_debounced_refresh(
        self,
        trigger: str = "state_change",
        *,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        force: bool = False,
    ) -> None:
        """Coalesce repeated input changes into one coordinator refresh."""
        self._mark_replan_requested(force=force)
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
            self._async_create_listener_task(self.async_request_refresh())

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

    async def _async_update_data(self) -> EnergyPlan | None:
        """Refresh planner data."""
        started = perf_counter()
        succeeded = False
        trigger = getattr(self, "_pending_refresh_trigger", "unknown")
        trigger_counts = getattr(self, "_refresh_trigger_counts", {})
        trigger_counts[trigger] = int(trigger_counts.get(trigger, 0)) + 1
        self._refresh_trigger_counts = trigger_counts
        try:
            async with self._planner_lock:
                if getattr(self, "_tearing_down", False):
                    succeeded = True
                    self._increment_refresh_counter("succeeded")
                    self._increment_refresh_counter("teardown_skipped")
                    return self.data
                async with self.store.async_delay_save():
                    self._deferred_plan_execution = None
                    result = await self._async_update_data_locked(defer_execution=True)
                    succeeded = True
                    self._increment_refresh_counter("succeeded")
            deferred_execution = self._deferred_plan_execution
            self._deferred_plan_execution = None
            if deferred_execution is not None:
                self._schedule_plan_execution(deferred_execution)
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

    async def _async_update_data_locked(self, *, defer_execution: bool = False) -> EnergyPlan:
        """Refresh planner data while holding the planner lock."""
        preparation_started = perf_counter()
        started_generation = self._refresh_generation
        now = dt_util.utcnow()
        active_overrides = _unexpired_overrides(
            self.overrides,
            now,
        )
        stored_overrides = self.store.data.get("overrides", [])
        expired_manual_hvac_state = _expired_manual_hvac_state(
            self.store.data,
            now,
        )
        overrides_changed = active_overrides != self.overrides
        if overrides_changed:
            self.overrides = active_overrides
        if stored_overrides != to_jsonable(self.overrides):
            await self.store.async_save_overrides(self.overrides)
        await self._async_reconcile_expired_manual_hvac_state(expired_manual_hvac_state)
        options = self.planner_options
        entry_data = self.entry_data
        self.executor.options = options
        force_refresh = bool(getattr(self, "_force_next_refresh", False))
        self._force_next_refresh = False
        weather_forecast, weather_forecast_details = await self._async_weather_forecast(
            entry_data,
            options,
            now=now,
            force=force_refresh,
        )
        load_source_outage = _updated_load_source_outage(
            self.hass,
            entry_data,
            self.store.data.get("load_source_outage"),
            now=now,
        )
        await self._async_save_load_source_outage(load_source_outage)
        decision_fingerprint = _decision_input_fingerprint(
            self.hass,
            entry_data,
            options,
            self.overrides,
            now=dt_util.utcnow(),
            weather_forecast=weather_forecast,
        )
        if (
            not force_refresh
            and not load_source_outage
            and decision_fingerprint == getattr(self, "_last_decision_fingerprint", None)
            and getattr(self, "data", None) is not None
        ):
            self._increment_refresh_counter("fingerprint_skipped")
            self._last_phase_durations = {"fingerprint_ms": round((perf_counter() - preparation_started) * 1000, 3)}
            assert self.data is not None
            return self.data
        self.executor.entry_data = entry_data
        discovery = CapabilityDiscovery(self.hass, entry_data, options).inspect()
        await self.store.async_save_discovery(discovery.as_dict())
        request = self._training_request()
        self.history_training.request(request)
        ev_charge_calibration = request.ev_model
        load_forecast_model = request.load_model
        training_result = self.history_training.last_result
        ev_calibration_reason = training_result.ev_reason if training_result else "ev_charge_calibration_pending"
        load_forecast_reason = training_result.load_reason if training_result else "load_forecast_training_pending"
        manager = InputManager(
            self.hass,
            entry_data,
            options,
            forecast_calibration=dict(self.store.data.get("forecast_calibration", {})),
            load_forecast_model=load_forecast_model,
            load_forecast_update_reason=load_forecast_reason,
            load_source_outage=load_source_outage,
            weather_forecast=weather_forecast,
            weather_forecast_details=weather_forecast_details,
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
        self.weather_forecast_diagnostics = dict(
            getattr(manager, "weather_forecast_details", weather_forecast_details)
        )
        climate_discovery = getattr(
            discovery,
            "hvac",
            getattr(discovery, "climate", None),
        )
        climate_issues = list(getattr(climate_discovery, "issues", []))
        for issue in climate_issues:
            if issue not in context.input_issues:
                context.input_issues.append(issue)
        stored_ownership = self.store.data.get("ownership", {})
        if isinstance(stored_ownership, dict):
            context.hvac_control = _hvac_control_from_ownership(stored_ownership)
        if context.hvac_control and climate_issues:
            context.hvac_control["required_evidence_lost"] = ",".join(climate_issues)
        thermal_model, thermal_model_changed = update_thermal_model(
            dict(self.store.data.get("thermal_model", {})),
            dict(self.store.data.get("thermal_model", {})).get("last_sample"),
            manager.thermal_sample(context),
        )
        if thermal_model_changed:
            await self.store.async_save_thermal_model(thermal_model)
        preparation_ms = round((perf_counter() - preparation_started) * 1000, 3)
        planner = DryRunPlanner(
            options,
            thermal_model=thermal_model,
            ev_charge_calibration=ev_charge_calibration,
            ev_charging_entity_id=entry_data.get(CONF_EV_CHARGING),
            ev_soc_entity_id=entry_data.get(CONF_EV_SOC),
        )
        planner_started = perf_counter()
        plan = await self.hass.async_add_executor_job(planner.create_plan, context)
        planner_ms = (perf_counter() - planner_started) * 1000
        persistence_started = perf_counter()
        violations = ConstraintValidator(options).validate_plan(context, plan)
        if violations:
            plan.input_issues.extend(violations)
            if "input_health_unsafe" in violations:
                plan.status = "unsafe"
            if plan.mode == PlannerMode.ACTIVE_HEALTHY:
                plan.mode = PlannerMode.ACTIVE_DEGRADED
        self._log_availability_transition(plan.input_issues)
        self._record_startup_auto_recovery_validation_candidate(plan, violations)
        await self.executor.async_notify_plan_fallback(plan, violations)
        await self.store.async_add_forecast_snapshot(
            {
                "created_at": context.created_at,
                "plan_id": context.plan_id,
                "input_health": context.input_health,
                "slot_count": len(context.slots),
                "actions": _snapshot_actions(plan),
                "preview": plan.preview[:12],
                "ev_charge_calibration": {
                    "update_reason": ev_calibration_reason,
                    "status": ev_charge_calibration.get("status"),
                    "sample_count": ev_charge_calibration.get("sample_count", 0),
                    "soc_per_kwh": ev_charge_calibration.get("soc_per_kwh"),
                },
                "thermal_model": thermal_model_summary(thermal_model),
                "forecast_training_slots": manager.forecast_training_slots,
                "forecast_calibration": {
                    "pv_forecast_kw": _calibration_summary(forecast_calibration, "pv_forecast_kw"),
                },
                "built_in_load_forecast": dict(getattr(manager, "load_forecast_details", {})),
                "action_load_forecasts": _snapshot_action_load_forecasts(plan, context),
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
        result = await self._async_commit_plan_if_current(
            started_generation,
            plan,
            context,
            options,
            execute=not defer_execution,
        )
        self._increment_refresh_counter("computed")
        self._last_phase_durations = {
            "inputs_ms": preparation_ms,
            "planner_ms": round(planner_ms, 3),
            "persistence_and_execution_ms": round((perf_counter() - persistence_started) * 1000, 3),
        }
        if started_generation == self._refresh_generation:
            self._last_decision_fingerprint = decision_fingerprint
            self._sync_ai_request_to_plan(plan)
        return result

    _availability_unavailable = False

    def _log_availability_transition(self, issues: list[str]) -> None:
        """Log our required-input degradation once, without upstream payloads."""
        unavailable = any(
            "unavailable" in issue or "not_found" in issue or "missing" in issue
            for issue in issues
        )
        if unavailable == self._availability_unavailable:
            return
        self._availability_unavailable = unavailable
        if unavailable:
            _LOGGER.warning("Planner required input or service unavailable: required_evidence_missing")
        else:
            _LOGGER.info("Planner required inputs and services recovered: required_evidence_restored")

    @cached_property
    def _listener_tasks(self) -> set[asyncio.Task[Any]]:
        """Allocate the entry task registry when the first listener is admitted."""
        return set()

    @cached_property
    def history_training(self) -> HistoryTraining:
        """Keep training admission independent of the planner refresh lock."""
        return HistoryTraining(self.hass, self.entry.entry_id, self._async_publish_training)

    def _training_request(self) -> TrainingRequest:
        return training_request(
            self.entry_data, self.planner_options, self.store.data,
            str(getattr(getattr(self.hass, "config", None), "time_zone", None) or "UTC"),
        )

    async def _async_publish_training(
        self, generation: int, request: TrainingRequest, result: TrainingResult
    ) -> None:
        """Publish only current source/lifetime results under the planner lock."""
        async with self._planner_lock:
            if (
                self._tearing_down
                or not self.history_training.is_current(generation)
                or request.identity != self._training_request().identity
            ):
                return
            self._load_forecast_training_attempted = _updated_load_forecast_training_attempted(
                self._load_forecast_training_attempted, result.load_reason
            )
            async with self.store.async_delay_save():
                if result.ev_changed:
                    await self.store.async_save_ev_charge_calibration(result.ev_model)
                if result.load_changed:
                    await self.store.async_save_builtin_load_forecast(result.load_model)
            if result.ev_changed or result.load_changed:
                self._schedule_debounced_refresh("history_training", debounce_seconds=0, force=True)

    async def async_request_replan(self) -> None:
        """Request immediate refresh."""
        self._mark_forced_refresh("manual_replan")
        await self.async_request_refresh()

    async def async_reconcile_production_evidence_contract(self) -> bool:
        """Prepare startup control without discarding prior active intent."""
        production = parse_production_state(self.store.data.get("production"))
        pause = self.store.data.get("control_pause")
        now = dt_util.utcnow()
        required_control_areas = list(_control_area_report(self.entry_data, self.options)["required"])
        unpaused_control_areas, _paused_control_areas = partition_control_areas_by_pause(
            pause,
            now,
            required_control_areas,
        )
        pause_blocks_all_control = bool(
            control_pause_reason(pause, now) is not None
            and (not required_control_areas or not unpaused_control_areas)
        )
        recovery = production.raw.get("startup_auto_recovery")
        recovery_status = str(recovery.get("status", "")) if isinstance(recovery, dict) else ""
        recovery_pending_while_disarmed = bool(
            self.automatic_control_requested
            and not production.armed
            and recovery_status in {"waiting_for_safe", "validating", "restoring"}
        )
        expected_fingerprint = production_evidence_fingerprint(self.entry_data, self.options)

        if self.active_control and pause_blocks_all_control:
            # A temporary pause remains authoritative, but it must not erase a
            # previously armed installation's automatic-control lifecycle.
            # Persist the restart-resumable handoff before disarming so a
            # reload or process stop between these operations still retries
            # once the pause clears.
            self._startup_auto_recovery_authorized = True
            self._startup_auto_recovery_deadline = None
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason="startup_control_paused",
            )
            await self.async_disarm_production_control("startup_control_paused")
            await self.async_restore_safe_state("startup_control_paused", refresh=False)
            return True

        if self.active_control and production.dry_run_evidence_fingerprint != expected_fingerprint:
            # A software or entry migration can change the evidence contract
            # before configured entities have finished restoring. Fail closed,
            # but preserve the previously armed automatic-control lifecycle so
            # the post-startup recovery task can revalidate the new contract
            # with three fresh, non-commanding plans instead of leaving the
            # installation permanently disarmed with no task able to recover.
            self._startup_auto_recovery_authorized = True
            self._startup_auto_recovery_deadline = None
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason="production_evidence_contract_changed",
            )
            await self.async_disarm_production_control("production_evidence_contract_changed")
            await self.async_restore_safe_state("production_evidence_contract_changed", refresh=False)
            return True

        if self.active_control and not pause_blocks_all_control:
            # A previously running installation resumes immediately. Runtime
            # action gates still reject unsafe work while the startup grace
            # observes the fully started Home Assistant instance.
            self._startup_auto_recovery_authorized = True
            self._startup_auto_recovery_deadline = None
            self.executor.notification_grace_until = datetime.max.replace(tzinfo=UTC)
            await self._async_update_startup_auto_recovery(
                "waiting_for_home_assistant",
                successful_runs=0,
                required_runs=1,
                reason="previously_active_control_preserved",
            )
            return True

        if recovery_pending_while_disarmed:
            self._startup_auto_recovery_authorized = True
            self._startup_auto_recovery_deadline = None
            await self._async_update_startup_auto_recovery(
                "waiting_for_home_assistant",
                successful_runs=0,
                reason="startup_safe_recovery_pending",
            )
            return True

        interrupted_recovery = bool(
            isinstance(recovery, dict) and recovery_status in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES
        )
        if interrupted_recovery and isinstance(recovery, dict):
            await self._async_update_startup_auto_recovery(
                "interrupted",
                successful_runs=_startup_auto_recovery_successful_runs(recovery.get("successful_runs")),
                reason="startup_restarted_without_active_control",
                completed=True,
            )
            store_data = getattr(getattr(self, "store", None), "data", {})
            production = parse_production_state(
                store_data.get("production") if isinstance(store_data, dict) else None
            )

        if not production.armed or production.dry_run_evidence_fingerprint == expected_fingerprint:
            return interrupted_recovery

        await self.async_restore_safe_state("production_evidence_contract_changed", refresh=False)
        await self.async_disarm_production_control("production_evidence_contract_changed")
        return True

    async_cancel_startup_auto_recovery = startup_recovery.async_cancel_startup_auto_recovery

    _async_run_startup_auto_recovery = startup_recovery._async_run_startup_auto_recovery

    _async_complete_startup_grace = startup_recovery._async_complete_startup_grace

    _async_enter_startup_safe_recovery = startup_recovery._async_enter_startup_safe_recovery

    _async_retry_startup_safe_recovery = startup_recovery._async_retry_startup_safe_recovery

    _async_reactivate_after_startup_recovery = startup_recovery._async_reactivate_after_startup_recovery

    _async_run_startup_auto_recovery_validation = startup_recovery._async_run_startup_auto_recovery_validation

    _record_startup_auto_recovery_validation_candidate = (
        startup_recovery._record_startup_auto_recovery_validation_candidate
    )

    _async_update_startup_auto_recovery = startup_recovery._async_update_startup_auto_recovery

    async def async_handle_options_update(self) -> None:
        """Apply option transitions, restoring ownership when control becomes safe."""
        async with self._options_update_lock:
            option_state = dict(self.entry.options)
            previous_option_state = getattr(self, "_last_handled_options", None)
            if option_state == previous_option_state:
                return
            previous_options = (
                {**DEFAULT_OPTIONS, **previous_option_state}
                if isinstance(previous_option_state, dict)
                else None
            )
            current_options = {**DEFAULT_OPTIONS, **option_state}
            changed_option_keys = {
                key
                for key in set(previous_options or {}) | set(current_options)
                if (previous_options or {}).get(key) != current_options.get(key)
            }
            device_control_only_change = bool(changed_option_keys) and changed_option_keys <= {
                CONF_EV_CONTROL_ENABLED,
                CONF_CLIMATE_CONTROL_ENABLED,
                CONF_ENPHASE_CONTROL_ENABLED,
            }
            store_data = getattr(getattr(self, "store", None), "data", {})
            production = parse_production_state(
                store_data.get("production") if isinstance(store_data, dict) else None
            )
            recovery = production.raw.get("startup_auto_recovery")
            recovery_was_pending = bool(
                previous_options is not None
                and strict_bool(previous_options.get(CONF_PLANNER_ENABLED), default=False)
                and not strict_bool(previous_options.get(CONF_DRY_RUN), default=True)
                and (
                    getattr(self, "_startup_auto_recovery_authorized", False)
                    or (
                        isinstance(recovery, dict)
                        and recovery.get("status") in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES
                    )
                )
            )
            was_running = bool(
                previous_options is not None
                and strict_bool(previous_options.get(CONF_PLANNER_ENABLED), default=False)
                and not strict_bool(previous_options.get(CONF_DRY_RUN), default=True)
                and production.armed
            )
            automatic_lifecycle_was_active = was_running or recovery_was_pending
            await self.async_cancel_startup_auto_recovery("options_changed")
            automatic_control_still_requested = bool(
                strict_bool(current_options.get(CONF_PLANNER_ENABLED), default=False)
                and not strict_bool(current_options.get(CONF_DRY_RUN), default=True)
            )
            if (
                was_running
                and automatic_control_still_requested
                and not device_control_only_change
                and parse_production_state(self.store.data.get("production")).armed
            ):
                await self.async_disarm_production_control("configuration_changed")
                await self.async_restore_safe_state("configuration_changed", refresh=False)

            self.executor.options = current_options
            self.executor.entry_data = self.entry_data
            self.executor.sync_ev_grid_reservation()
            await self.executor.async_persist_ev_grid_reservation()
            previous_enabled, previous_dry_run = self._last_control_mode_state
            current_mode = (
                bool(current_options.get(CONF_PLANNER_ENABLED, False)),
                bool(current_options.get(CONF_DRY_RUN, True)),
            )
            self._last_control_mode_state = current_mode
            planner_disabled = previous_enabled and not current_mode[0]
            dry_run_enabled = not previous_dry_run and current_mode[1]
            disabled_device_controls = [
                (area, executor_asset)
                for option_key, area, executor_asset in (
                    (CONF_EV_CONTROL_ENABLED, "ev", "ev"),
                    (CONF_CLIMATE_CONTROL_ENABLED, "hvac", "daikin"),
                    (CONF_ENPHASE_CONTROL_ENABLED, "enphase", "enphase"),
                )
                if previous_options is not None
                and strict_bool(previous_options.get(option_key), default=False)
                and not strict_bool(current_options.get(option_key), default=False)
            ]
            if any(area == "ev" for area, _executor_asset in disabled_device_controls):
                self._clear_ev_auto_start_compensation()
            if planner_disabled or dry_run_enabled:
                reason = "planner_disabled" if planner_disabled else "dry_run_enabled"
                await self.async_restore_safe_state(reason, refresh=False)
            elif disabled_device_controls:
                async with self._command_lock:
                    for area, executor_asset in disabled_device_controls:
                        try:
                            await self.executor.async_restore_device_control(
                                executor_asset,
                                f"{area}_control_disabled",
                            )
                        except Exception:  # noqa: BLE001 - persisted disabled controls remain authoritative.
                            _LOGGER.exception(
                                "Unexpected error while restoring %s after its control selector was disabled",
                                area,
                            )
            # The option transition and any required restoration are complete.
            # Mark this snapshot handled before replanning so a refresh failure
            # cannot replay device restore commands through the update listener.
            self._last_handled_options = option_state
            await self.async_request_replan()
            if automatic_lifecycle_was_active and self.automatic_control_requested:
                if device_control_only_change:
                    production_update: dict[str, object] = dict(
                        parse_production_state(self.store.data.get("production")).raw
                    )
                    production_update.update(
                        {
                            "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                                self.entry_data,
                                self.options,
                            ),
                            "dry_run_ready_cycles": DRY_RUN_READY_CYCLES_REQUIRED,
                        }
                    )
                    await self._async_save_production(production_update)
                self._startup_auto_recovery_authorized = True
                await self._async_update_startup_auto_recovery(
                    "waiting_for_home_assistant",
                    successful_runs=0,
                    reason="configuration_changed",
                )
                self.async_start_startup_auto_recovery()

    async def async_prepare_configuration_reload(self) -> None:
        """Persist a disarmed recovery handoff for a changed entity topology."""
        self._configuration_reload_handoff = False
        production = parse_production_state(self.store.data.get("production"))
        recovery = production.raw.get("startup_auto_recovery")
        automatic_lifecycle_was_active = bool(
            self.active_control
            or (
                self.automatic_control_requested
                and (
                    getattr(self, "_startup_auto_recovery_authorized", False)
                    or (
                        isinstance(recovery, dict)
                        and recovery.get("status") in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES
                    )
                )
            )
        )
        await self.async_cancel_startup_auto_recovery("configuration_changed")
        if automatic_lifecycle_was_active:
            if parse_production_state(self.store.data.get("production")).armed:
                await self.async_disarm_production_control("configuration_changed")
            restore = await self.async_restore_safe_state(
                "configuration_changed",
                refresh=False,
            )
            store_data = getattr(getattr(self, "store", None), "data", {})
            ownership = store_data.get("ownership") if isinstance(store_data, dict) else None
            reservation = (
                store_data.get("ev_grid_reservation")
                if isinstance(store_data, dict)
                else None
            )
            unresolved_restore = bool(ownership) or bool(
                isinstance(reservation, dict) and reservation.get("active") is True
            )
            if _action_outcome_failed(restore) and unresolved_restore:
                failure_reason = str(
                    getattr(restore, "reason", "configuration_restore_failed")
                )
                await self._async_update_startup_auto_recovery(
                    "failed",
                    successful_runs=0,
                    reason=failure_reason,
                    completed=True,
                )
                raise HomeAssistantError(
                    f"Energy Planner could not fully restore safe state: {failure_reason}",
                    translation_domain=DOMAIN,
                    translation_key="restore_safe_state_failed",
                    translation_placeholders={"reason": failure_reason},
                )
        if automatic_lifecycle_was_active and self.automatic_control_requested:
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason="configuration_changed",
            )
            self._configuration_reload_handoff = True

    async def async_set_ready_by(self, ready_by: str) -> None:
        """Persist the native EV ready-by setting and replan."""
        self.ready_by = ready_by
        options = self.options
        options[CONF_DEFAULT_READY_BY] = ready_by
        config_entries = getattr(self.hass, "config_entries", None)
        update_entry = getattr(config_entries, "async_update_entry", None)
        if callable(update_entry):
            update_entry(self.entry, options=options)
        if self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY):
            await EVSmartChargingAdapter(self.hass, self.entry_data).async_set_ready_by(ready_by)
        self._mark_forced_refresh("ready_by_changed")
        await self.async_request_refresh()

    async def async_set_ev_low_price_threshold(self, threshold: float) -> None:
        """Persist the opportunistic charging price threshold and replan."""
        options = self.options
        options[CONF_EV_LOW_PRICE_THRESHOLD] = float(threshold)
        config_entries = getattr(self.hass, "config_entries", None)
        update_entry = getattr(config_entries, "async_update_entry", None)
        if callable(update_entry):
            update_entry(self.entry, options=options)
        await self.async_handle_options_update()

    async def async_manual_ev_charging(self, enabled: bool) -> EVCommandResult:
        """Apply a manual charger command through Energy Planner's adapter."""
        async with self._command_lock:
            self.executor.options = self.options
            self.executor.entry_data = self.entry_data
            result = await self.executor.async_manual_ev_charging(
                enabled,
                getattr(self, "_last_decision_context", None),
            )
            if result.applied:
                self.overrides = [
                    override for override in self.overrides if override.kind != "manual_ev_charging"
                ]
                self.overrides.append(
                    Override(
                        kind="manual_ev_charging",
                        source="button",
                        expires_at=dt_util.utcnow() + timedelta(hours=1),
                        reason="manual_start" if enabled else "manual_stop",
                    )
                )
                await self.store.async_save_overrides(self.overrides)
        self._mark_forced_refresh("manual_ev_charging")
        await self.async_request_refresh()
        return result

    async def async_set_ev_keep_charger_on(self, enabled: bool) -> None:
        """Validate and persist the preconditioning keep-on policy."""
        entry_data = self.entry_data
        persistent_control = entry_data.get(CONF_EV_CHARGER) or entry_data.get(CONF_EV_SMART_CHARGING)
        if enabled and (
            not persistent_control or str(persistent_control).split(".", 1)[0] not in {"switch", "input_boolean"}
        ):
            raise HomeAssistantError(
                "Keep charger on requires a persistent EV charger switch or input boolean.",
                translation_domain=DOMAIN,
                translation_key="ev_keep_on_requires_persistent_control",
            )
        options = self.options
        options[CONF_EV_KEEP_CHARGER_ON] = enabled
        update_entry = getattr(self.hass.config_entries, "async_update_entry", None)
        if callable(update_entry):
            update_entry(self.entry, options=options)
        await self.async_handle_options_update()

    async def async_set_manual_hvac_override(
        self,
        duration_minutes: int,
        reason: str,
        *,
        source: str = "service",
        expires: bool = True,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
    ) -> ActionOutcome | None:
        """Serialize an operator HVAC override with automatic device execution."""
        async with self._command_lock:
            if getattr(self, "_tearing_down", False):
                return None
            return await self._async_set_manual_hvac_override(
                duration_minutes,
                reason,
                source=source,
                expires=expires,
                preserve_zone_entity_id=preserve_zone_entity_id,
                preserve_main_state=preserve_main_state,
            )

    async def _async_set_manual_hvac_override(
        self,
        duration_minutes: int,
        reason: str,
        *,
        source: str = "service",
        expires: bool = True,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
    ) -> ActionOutcome | None:
        """Set a manual HVAC override."""
        self._mark_forced_refresh("manual_hvac_override")
        helper_error: Exception | None = None
        release_outcome = None
        async with self._planner_lock:
            expires_at = dt_util.utcnow() + timedelta(minutes=duration_minutes) if expires else None
            self.overrides = [
                override
                for override in self.overrides
                if not (
                    override.kind == "manual_hvac"
                    and (
                        (source == "helper" and getattr(override, "source", None) == "helper")
                        or (source != "helper" and getattr(override, "source", None) != "helper")
                    )
                )
            ]
            self.overrides.append(
                Override(
                    kind="manual_hvac",
                    source=source,
                    expires_at=expires_at,
                    reason=reason,
                )
            )
            await self.store.async_save_overrides(self.overrides)
            ownership = dict(self.store.data.get("ownership", {}))
            ownership["manual_hvac_override_expires_at"] = (
                None
                if any(override.kind == "manual_hvac" and override.expires_at is None for override in self.overrides)
                else max(
                    (
                        override.expires_at
                        for override in self.overrides
                        if override.kind == "manual_hvac" and override.expires_at is not None
                    ),
                    default=expires_at,
                )
            )
            await self.store.async_save_ownership(ownership)
            manual_override_entity = self.entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
            if manual_override_entity and source != "helper":
                self._manual_override_helper_guard = ("on", dt_util.utcnow() + timedelta(minutes=2))
                try:
                    await async_call_device_service(
                        self.hass,
                        "input_boolean",
                        "turn_on",
                        {"entity_id": manual_override_entity},
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001 - release must still run when helper feedback fails.
                    self._manual_override_helper_guard = None
                    helper_error = err
            release_hvac = getattr(self.executor, "async_release_hvac_control", None)
            if callable(release_hvac):
                release_options: dict[str, Any] = {}
                if preserve_zone_entity_id:
                    release_options["preserve_zone_entity_id"] = preserve_zone_entity_id
                if preserve_main_state:
                    release_options["preserve_main_state"] = True
                release_outcome = await release_hvac(reason, **release_options)
        await self.async_request_refresh()
        if helper_error is not None:
            raise helper_error
        return release_outcome

    async def _async_reconcile_expired_manual_hvac_state(self, expired: bool) -> None:
        """Retry helper cleanup while retaining a fail-closed override."""
        helper_cleanup_override = any(
            override.kind == "manual_hvac" and override.reason == "manual_hvac_helper_cleanup_failed"
            for override in self.overrides
        )
        if not expired or (
            any(override.kind == "manual_hvac" for override in self.overrides) and not helper_cleanup_override
        ):
            return
        helper_cleared = await self._async_clear_expired_manual_hvac_state()
        if helper_cleared:
            if helper_cleanup_override:
                self.overrides = [
                    override
                    for override in self.overrides
                    if not (override.kind == "manual_hvac" and override.reason == "manual_hvac_helper_cleanup_failed")
                ]
                await self.store.async_save_overrides(self.overrides)
            return
        self.overrides = [override for override in self.overrides if override.kind != "manual_hvac"]
        self.overrides.append(
            Override(
                kind="manual_hvac",
                source="helper",
                expires_at=None,
                reason="manual_hvac_helper_cleanup_failed",
            )
        )
        await self.store.async_save_overrides(self.overrides)

    async def _async_handle_manual_override_helper(self, enabled: bool) -> None:
        """Apply an external manual-override helper change with a bounded timeout."""
        if enabled:
            await self.async_set_manual_hvac_override(
                int(self.options[CONF_MANUAL_HVAC_OVERRIDE_MINUTES]),
                "manual_override_helper_on",
                source="helper",
            )
            return
        async with self._planner_lock:
            if getattr(self, "_tearing_down", False):
                return
            self.overrides = [
                override
                for override in self.overrides
                if not (override.kind == "manual_hvac" and getattr(override, "source", None) == "helper")
            ]
            await self.store.async_save_overrides(self.overrides)
            ownership = dict(self.store.data.get("ownership", {}))
            remaining_manual_override = next(
                (override for override in self.overrides if override.kind == "manual_hvac"),
                None,
            )
            if remaining_manual_override is None:
                ownership.pop("manual_hvac_override_expires_at", None)
            else:
                ownership["manual_hvac_override_expires_at"] = getattr(
                    remaining_manual_override,
                    "expires_at",
                    None,
                )
            await self.store.async_save_ownership(ownership)
        self._mark_forced_refresh("manual_hvac_override_cleared")
        await self.async_request_refresh()

    async def _async_handle_manual_hvac_change(
        self,
        reason: str,
        *,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
    ) -> None:
        """Record manual HVAC override from observed Daikin state change."""
        await self.async_set_manual_hvac_override(
            int(self.options[CONF_MANUAL_HVAC_OVERRIDE_MINUTES]),
            reason,
            preserve_zone_entity_id=preserve_zone_entity_id,
            preserve_main_state=preserve_main_state,
        )

    async def async_restore_safe_state(self, reason: str, *, refresh: bool = True) -> ActionOutcome:
        """Restore safe state and refresh."""
        async with self._command_lock:
            outcome = await self.executor.async_restore_safe_state(reason)
        if refresh:
            self._mark_forced_refresh("safe_state_restored")
            await self.async_request_refresh()
        return outcome

    async def async_arm_production_control(self, reason: str = "user_acknowledged") -> None:
        """Arm production control after operator acknowledgement."""
        production = parse_production_state(self.store.data.get("production")).raw
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

    async def async_operator_arm_production_control(
        self,
        reason: str = "user_acknowledged",
    ) -> None:
        """Apply an explicit operator arm only after current safety validation."""
        report = build_preflight_report(self.hass, self)
        if not report.get("safe_to_activate_now"):
            rejection_reason = _active_control_not_ready_reason(report)
            raise HomeAssistantError(
                f"Production control is not ready: {rejection_reason}",
                translation_domain=DOMAIN,
                translation_key="active_control_not_ready",
                translation_placeholders={"reason": rejection_reason},
            )
        try:
            await self.async_cancel_startup_auto_recovery(
                "operator_armed",
                restore_owned_state=False,
            )
        finally:
            await self.async_arm_production_control(reason)
            await self._async_compensate_ev_auto_start(require_unowned=False)

    async def async_set_active_control(self, enabled: bool) -> None:
        """Enable or safely return from automatic device control as one operation."""
        if enabled and self.active_control:
            return

        options = self.options
        options[CONF_PLANNER_ENABLED] = True
        if not enabled:
            await self.async_cancel_startup_auto_recovery("automatic_control_disabled")
            options[CONF_DRY_RUN] = True
            self.hass.config_entries.async_update_entry(self.entry, options=options)
            await self.async_handle_options_update()
            await self.async_disarm_production_control("automatic_control_disabled")
            return

        options[CONF_DRY_RUN] = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        await self.async_handle_options_update()

        device_control_selected = any(
            strict_bool(self.options.get(key), default=False)
            for key in (
                CONF_EV_CONTROL_ENABLED,
                CONF_CLIMATE_CONTROL_ENABLED,
                CONF_ENPHASE_CONTROL_ENABLED,
            )
        )
        if not device_control_selected:
            raise HomeAssistantError(
                "Automatic control requires at least one selected device control area",
                translation_domain=DOMAIN,
                translation_key="active_control_no_device_selected",
            )

        report = build_preflight_report(self.hass, self)
        if not report.get("safe_to_activate_now"):
            reason = _active_control_not_ready_reason(report)
            raise HomeAssistantError(
                f"Automatic control is not ready: {reason}",
                translation_domain=DOMAIN,
                translation_key="active_control_not_ready",
                translation_placeholders={"reason": reason},
            )

        await self.async_arm_production_control("automatic_control_enabled")
        options = self.options
        options[CONF_DRY_RUN] = False
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        await self.async_handle_options_update()
        await self._async_compensate_ev_auto_start(require_unowned=False)

    async def async_set_device_control(self, option_key: str, enabled: bool) -> None:
        """Enable or safely restore exactly one device control area."""
        async with self._device_control_lock:
            await self._async_set_device_control(option_key, enabled)

    async def _async_set_device_control(self, option_key: str, enabled: bool) -> None:
        """Apply one serialized device control transition."""
        control_areas = {
            CONF_EV_CONTROL_ENABLED: "ev",
            CONF_CLIMATE_CONTROL_ENABLED: "hvac",
            CONF_ENPHASE_CONTROL_ENABLED: "enphase",
        }
        if option_key not in control_areas:
            raise ValueError(f"Unsupported device control option: {option_key}")
        if strict_bool(self.options.get(option_key), default=False) is enabled:
            return

        area = control_areas[option_key]
        proposed_options = {**self.options, option_key: enabled}
        area_report = _control_area_report(self.entry_data, proposed_options)
        if enabled and not area_report["details"][area]["configured"]:
            raise HomeAssistantError(
                "The selected device control area is not configured",
                translation_domain=DOMAIN,
                translation_key="device_control_not_configured",
            )

        if self.active_control and enabled:
            report = build_preflight_report(self.hass, self, options_override=proposed_options)
            raw_area_state = report.get("control_areas")
            proposed_area_state = raw_area_state if isinstance(raw_area_state, dict) else {}
            area_ready = area in proposed_area_state.get("ready", [])
            area_available = area in proposed_area_state.get("available", [])
            area_confidence_eligible = area in proposed_area_state.get(
                "confidence_eligible",
                [],
            )
            if not report.get("safe_to_activate_now") or not (
                area_ready and area_available and area_confidence_eligible
            ):
                if not report.get("safe_to_activate_now"):
                    reason = _active_control_not_ready_reason(report)
                elif not area_ready:
                    reason = f"the selected {area} control area is not ready"
                elif not area_available:
                    reason = f"the selected {area} control area is paused"
                else:
                    reason = f"the selected {area} control area does not meet confidence thresholds"
                raise HomeAssistantError(
                    f"Device control is not ready: {reason}",
                    translation_domain=DOMAIN,
                    translation_key="device_control_not_ready",
                    translation_placeholders={"reason": reason},
                )

        if not enabled:
            # Disabling control is an operator stop boundary. Persist it before
            # touching the device so an unavailable or unconfirmable actuator
            # can never leave the control selector enabled. Restoration remains
            # best-effort and the executor records/notifies any incomplete work.
            self.hass.config_entries.async_update_entry(self.entry, options=proposed_options)
            try:
                await self.async_handle_options_update()
            except Exception:  # noqa: BLE001 - the persisted off selector remains authoritative.
                # An options listener can fail independently of the device
                # restore (for example while persisting diagnostics). Do not
                # turn a successfully persisted operator stop into a failed
                # switch service call; the listener can retry on the next
                # update and refreshes read the current entry options directly.
                self.executor.options = self.options
                _LOGGER.exception(
                    "Unexpected error while applying the disabled %s control option",
                    area,
                )
                self.async_update_listeners()
            return

        self.hass.config_entries.async_update_entry(self.entry, options=proposed_options)
        await self.async_handle_options_update()
        if option_key == CONF_EV_CONTROL_ENABLED:
            await self._async_compensate_ev_auto_start(require_unowned=False)

    async def _async_compensate_ev_auto_start(
        self,
        *,
        require_unowned: bool,
        refresh: bool = True,
        generation: int | None = None,
    ) -> EVCommandResult | None:
        """Stop charging that began outside Energy Planner control."""
        result: EVCommandResult | None = None
        async with self._command_lock:
            if generation is not None and generation != getattr(
                self,
                "_ev_auto_start_compensation_generation",
                0,
            ):
                return None
            if (
                getattr(self, "_tearing_down", False)
                or not self.active_control
                or not strict_bool(
                    self.options.get(CONF_EV_CONTROL_ENABLED),
                    default=False,
                )
            ):
                self._clear_ev_auto_start_compensation()
                return None
            charging_entity = self.entry_data.get(CONF_EV_CHARGING)
            charging_state = self.hass.states.get(charging_entity) if charging_entity else None
            charging_value = getattr(charging_state, "state", None)
            charging_active = (
                ev_charging_state(charging_value)
                if charging_value is not None
                and str(charging_value).strip().lower() not in STATE_UNKNOWN_VALUES
                else None
            )
            compensation_pending = getattr(
                self,
                "_ev_auto_start_compensation_pending",
                False,
            )
            if charging_active is False:
                self._clear_ev_auto_start_compensation()
                return None
            if charging_active is not True and not compensation_pending:
                return None
            if require_unowned and _ev_start_feedback_is_expected(
                getattr(self, "executor", None),
                dt_util.utcnow(),
            ):
                self._clear_ev_auto_start_compensation()
                return None
            self._ev_auto_start_compensation_pending = True
            try:
                result = await self.executor.async_compensate_ev_auto_start(
                    getattr(self, "_last_decision_context", None)
                )
            except Exception:  # noqa: BLE001 - pending safety stop must retry after executor failures.
                self._schedule_ev_auto_start_compensation_retry()
                raise
        if result.applied:
            self._clear_ev_auto_start_compensation()
        else:
            self._schedule_ev_auto_start_compensation_retry()
        self.async_update_listeners()
        if refresh:
            self._mark_forced_refresh("ev_auto_start_compensation")
            await self.async_request_refresh()
        return result

    @callback
    def _schedule_ev_auto_start_compensation_retry(self) -> None:
        """Retry one confirmed unsolicited start without requiring a new event."""
        if (
            not getattr(self, "_ev_auto_start_compensation_pending", False)
            or getattr(self, "_tearing_down", False)
            or getattr(self, "_ev_auto_start_retry_cancel", None) is not None
        ):
            return
        generation = getattr(self, "_ev_auto_start_compensation_generation", 0)

        @callback
        def _retry(_now: Any) -> None:
            self._ev_auto_start_retry_cancel = None
            if (
                not self._ev_auto_start_compensation_pending
                or getattr(self, "_tearing_down", False)
                or generation
                != getattr(self, "_ev_auto_start_compensation_generation", 0)
            ):
                return
            self._async_create_listener_task(
                self._async_compensate_ev_auto_start(
                    require_unowned=True,
                    generation=generation,
                )
            )

        self._ev_auto_start_retry_cancel = async_call_later(
            self.hass,
            EV_AUTO_START_COMPENSATION_RETRY_SECONDS,
            _retry,
        )

    @callback
    def _clear_ev_auto_start_compensation(self) -> None:
        """Clear a completed or no-longer-authorized compensation retry."""
        self._ev_auto_start_compensation_generation = (
            getattr(self, "_ev_auto_start_compensation_generation", 0) + 1
        )
        self._ev_auto_start_compensation_pending = False
        cancel = getattr(self, "_ev_auto_start_retry_cancel", None)
        if cancel is not None:
            cancel()
        self._ev_auto_start_retry_cancel = None

    async def async_disarm_production_control(self, reason: str = "user_requested") -> None:
        """Disarm production control."""
        self._clear_ev_auto_start_compensation()
        production = parse_production_state(self.store.data.get("production")).raw
        production.update(
            {
                "armed": False,
                "disarmed_at": dt_util.utcnow(),
                "disarmed_reason": reason,
            }
        )
        await self._async_save_production(production)
        async with self._command_lock:
            ownership = self.store.data.get("ownership", {})
            if isinstance(ownership, dict) and (
                ownership.get("hvac_control")
                or ownership.get("climate_automations")
                or ownership.get("planner_takeover_started_at")
                or ownership.get("planner_hvac_action_expires_at")
            ):
                await self.executor.async_release_hvac_control("production_control_disarmed")
        self.async_update_listeners()

    async def async_operator_disarm_production_control(
        self,
        reason: str = "user_requested",
    ) -> None:
        """Cancel startup recovery and apply an explicit operator disarm."""
        try:
            await self.async_cancel_startup_auto_recovery(
                "operator_disarmed",
                restore_owned_state=False,
            )
        finally:
            await self.async_disarm_production_control(reason)

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
        production_state = parse_production_state(self.store.data.get("production"))
        production = production_state.raw
        control_areas, _discovery = _runtime_control_area_report(
            self.hass,
            self.entry_data,
            self.options,
            plan=plan,
            pause=self.store.data.get("control_pause"),
            now=plan.created_at,
        )
        review_safe = bool(
            plan.mode == PlannerMode.DRY_RUN
            and plan.health in {InputHealth.HEALTHY, InputHealth.DEGRADED}
            and plan.status == "current"
            and (
                plan.health == InputHealth.HEALTHY
                or control_areas.get("confidence_eligible")
            )
            and not violations
        )
        if review_safe:
            evidence_fingerprint = production_evidence_fingerprint(self.entry_data, self.options)
            ready_cycles = production_state.dry_run_ready_cycles
            if production.get("dry_run_evidence_fingerprint") != evidence_fingerprint:
                ready_cycles = 0
            production["dry_run_evidence_fingerprint"] = evidence_fingerprint
            production["dry_run_ready_cycles"] = min(
                ready_cycles + 1,
                DRY_RUN_READY_CYCLES_REQUIRED,
            )
            production["last_dry_run_ready_at"] = plan.created_at
        elif plan.health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
            production["last_blocking_reason"] = "input_health_unsafe"
        await self._async_save_production(production)

    async def _async_save_production(self, production: dict[str, object]) -> None:
        await self.store.async_save_production(production)

    async def _async_save_control_pause(self, pause: dict[str, object]) -> None:
        await self.store.async_save_control_pause(pause)

    async def _async_save_load_source_outage(self, outage: dict[str, Any]) -> None:
        await self.store.async_save_load_source_outage(outage)

    async def _async_clear_expired_manual_hvac_state(self) -> bool:
        """Clear planner-managed manual HVAC exposure after its timeout."""
        manual_override_entity = self.entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
        if manual_override_entity:
            try:
                self._manual_override_helper_guard = ("off", dt_util.utcnow() + timedelta(minutes=2))
                await async_call_device_service(
                        self.hass,
                    "input_boolean",
                    "turn_off",
                    {"entity_id": manual_override_entity},
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - helper cleanup must not block planning.
                self._manual_override_helper_guard = None
                _LOGGER.warning(
                    "Could not clear expired manual HVAC helper %s",
                    manual_override_entity,
                )
                return False
        # The helper call above yields while device execution may persist new
        # ownership under the independent command lock. Re-read at the atomic
        # Store replacement boundary so cleanup cannot erase that new state.
        ownership = dict(self.store.data.get("ownership", {}))
        if "manual_hvac_override_expires_at" in ownership:
            ownership.pop("manual_hvac_override_expires_at", None)
            await self.store.async_save_ownership(ownership)
        return True

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
        await self.store.async_add_dry_run_comparison(comparison)

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
        *,
        execute: bool = True,
    ) -> EnergyPlan:
        """Persist only the newest planner result and optionally execute it."""
        if getattr(self, "_tearing_down", False):
            _LOGGER.debug(
                "Discarding planner result %s while the config entry is unloading",
                plan.plan_id,
            )
            return self.data or plan
        if started_generation != self._refresh_generation:
            _LOGGER.debug(
                "Discarding obsolete planner result %s from generation %s; current generation is %s",
                plan.plan_id,
                started_generation,
                self._refresh_generation,
            )
            if hasattr(self, "hass"):
                self._async_create_listener_task(self.async_request_refresh())
            return self.data or plan
        self._last_decision_context = context
        await self.store.async_save_plan(plan)
        if getattr(self, "_startup_auto_recovery_validation_active", False):
            validation = getattr(self, "_last_startup_auto_recovery_validation", None)
            if isinstance(validation, dict) and validation.get("plan_id") == plan.plan_id:
                validation["committed"] = True
            # Recovery validation is deliberately non-commanding, including
            # planner-owned safety-stop paths that may normally bypass arming.
            return plan
        if not execute:
            self._deferred_plan_execution = (started_generation, plan, context, dict(options))
            return plan
        await self._async_execute_plan_if_current(started_generation, plan, context, options)
        return plan

    async def _async_weather_forecast(
        self, entry_data: dict[str, Any], options: dict[str, Any], *, now: datetime, force: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await async_weather_forecast(self, entry_data, options, now=now, force=force)

    @callback
    def _schedule_plan_execution(
        self,
        request: tuple[int, EnergyPlan, DecisionContext, dict[str, Any]],
    ) -> None:
        """Queue only the newest committed plan for serialized background execution."""
        if getattr(self, "_tearing_down", False):
            return
        self._pending_plan_execution = request
        task = getattr(self, "_plan_execution_task", None)
        if task is not None and not task.done():
            return
        created = self.entry.async_create_background_task(
            self.hass,
            self._async_drain_plan_execution(),
            f"{DOMAIN} plan execution",
        )
        self._plan_execution_task = created

    async def _async_drain_plan_execution(self) -> None:
        """Execute committed plans without holding the coordinator refresh lock."""
        try:
            while not getattr(self, "_tearing_down", False):
                request = self._pending_plan_execution
                self._pending_plan_execution = None
                if request is None:
                    return
                started_generation, plan, context, options = request
                async with self._command_lock:
                    if (
                        getattr(self, "_tearing_down", False)
                        or started_generation != self._refresh_generation
                    ):
                        continue
                    try:
                        await self._async_execute_plan_if_current(
                            started_generation,
                            plan,
                            context,
                            options,
                        )
                    except Exception:
                        # Isolate one malformed or failed execution transaction
                        # so a newer coalesced safety plan is not stranded.
                        _LOGGER.exception(
                            "Unexpected failure executing committed plan %s",
                            plan.plan_id,
                        )
                    finally:
                        # Executor outcomes, ownership, reservations, pauses,
                        # and audit state are Store-backed rather than part of
                        # the earlier coordinator refresh result.
                        self.async_update_listeners()
        finally:
            self._plan_execution_task = None

    async def _async_execute_plan_if_current(
        self,
        started_generation: int,
        plan: EnergyPlan,
        context: Any,
        options: dict[str, Any],
    ) -> None:
        """Execute one current plan, stopping between actions when it becomes stale."""
        if (
            getattr(self, "_tearing_down", False)
            or started_generation != self._refresh_generation
        ):
            return
        self.executor.options = options
        self.executor.entry_data = self.entry_data
        consumed_action = await self.executor.async_evaluate(plan, context)
        evaluated_action = consumed_action if consumed_action in plan.actions else plan.next_action
        for action in plan.actions:
            if action is evaluated_action:
                continue
            if getattr(self, "_tearing_down", False):
                _LOGGER.debug(
                    "Stopping coordinated execution for plan %s while the config entry is unloading",
                    plan.plan_id,
                )
                break
            if started_generation != self._refresh_generation:
                _LOGGER.debug(
                    "Stopping coordinated execution for obsolete plan %s from generation %s; current generation is %s",
                    plan.plan_id,
                    started_generation,
                    self._refresh_generation,
                )
                if hasattr(self, "hass"):
                    self._async_create_listener_task(self.async_request_refresh())
                break
            # The priority score orders presentation and the first command, but
            # every coordinated device action keeps its own execution gate.
            await self.executor.async_evaluate(replace(plan, actions=[action]), context)

    _async_get_throttled_ai_advice = advice_runtime._async_get_throttled_ai_advice

    async_request_ai_advice = advice_runtime.async_request_ai_advice

    _sync_ai_request_to_plan = advice_runtime._sync_ai_request_to_plan

    _async_run_ai_advice = advice_runtime._async_run_ai_advice

    _set_ai_advice_pending = advice_runtime._set_ai_advice_pending

    _clear_ai_advice_pending = advice_runtime._clear_ai_advice_pending

    _async_notify_ai_advice = advice_runtime._async_notify_ai_advice

    _async_reject_ai_advice_request = advice_runtime._async_reject_ai_advice_request

    def _non_manual_refresh_delay(self) -> float:
        """Return delay needed to enforce the safe non-manual refresh cadence."""
        last_requested = getattr(self, "_last_non_manual_refresh_requested_at", None)
        if last_requested is None:
            return 0.0
        elapsed = monotonic() - last_requested
        return float(max(float(MIN_NON_MANUAL_REFRESH_INTERVAL_SECONDS) - elapsed, 0.0))

    @cached_property
    def _refresh_counters(self) -> dict[str, int]:
        """Allocate bounded telemetry counters on first use."""
        return {"requested": 0, "completed": 0, "coalesced": 0, "fingerprint_skipped": 0}

    def _increment_refresh_counter(self, key: str) -> None:
        """Increment the coordinator's bounded telemetry."""
        self._refresh_counters[key] = self._refresh_counters.get(key, 0) + 1


def _updated_load_source_outage(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    previous: Any,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Retain one start timestamp until household load becomes numeric again."""
    entity_id = str(entry_data.get(CONF_HOUSEHOLD_LOAD, "") or "").strip()
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    attributes = getattr(state, "attributes", {}) or {}
    unit = str(attributes.get("unit_of_measurement") or attributes.get("unit") or "")
    if state is not None and normalize_power_kw(getattr(state, "state", None), unit) is not None:
        return {}
    observed_state = "missing" if state is None else str(state.state)
    sentinel_state = observed_state.lower() in {"unknown", "unavailable"}

    prior = dict(previous) if isinstance(previous, dict) else {}
    prior_matches = prior.get("entity_id") == entity_id
    prior_started_at = (
        _parse_datetime_or_none(prior.get("started_at")) if prior_matches else None
    )
    if prior_matches and prior_started_at is None:
        return {
            "entity_id": entity_id,
            "started_at": prior.get("started_at"),
            "last_observed_state": observed_state,
            "fallback_eligible": False,
            "invalid_reason": "outage_transition_time_unknown",
            "malformed": True,
        }
    if prior_started_at is not None:
        fallback_eligible = prior.get("fallback_eligible") is not False
        invalid_reason = prior.get("invalid_reason")
        if not sentinel_state:
            fallback_eligible = False
            invalid_reason = (
                "household_load_entity_missing"
                if state is None
                else "household_load_non_numeric"
            )
        result = {
            "entity_id": entity_id,
            "started_at": prior_started_at.isoformat(),
            "last_observed_state": observed_state,
            "fallback_eligible": fallback_eligible,
        }
        if invalid_reason:
            result["invalid_reason"] = invalid_reason
        return result

    state_changed_at = getattr(state, "last_changed", None)
    if not sentinel_state or not isinstance(state_changed_at, datetime):
        return {
            "entity_id": entity_id,
            "started_at": None,
            "last_observed_state": observed_state,
            "fallback_eligible": False,
            "invalid_reason": (
                "household_load_entity_missing"
                if state is None
                else "household_load_non_numeric"
                if not sentinel_state
                else "outage_transition_time_unknown"
            ),
        }
    started_at = (
        state_changed_at.replace(tzinfo=UTC)
        if state_changed_at.tzinfo is None
        else state_changed_at.astimezone(UTC)
    )
    return {
        "entity_id": entity_id,
        "started_at": started_at.isoformat(),
        "last_observed_state": observed_state,
        "fallback_eligible": True,
    }


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
    weather_forecast: dict[str, Any] | None = None,
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
        "weather_forecast": weather_forecast or {},
    }
    encoded = json.dumps(to_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _seconds_until_next_interval_boundary(now: Any, interval_minutes: int) -> float:
    """Return seconds until the next shared epoch planning boundary."""
    interval_seconds = max(int(interval_minutes), 1) * 60
    elapsed_seconds = float(now.timestamp())
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


def _snapshot_action_load_forecasts(
    plan: EnergyPlan,
    context: DecisionContext,
) -> list[dict[str, Any]]:
    """Return compact load evidence aligned to each planned action."""
    if not context.slots:
        return []
    slots = sorted(context.slots, key=lambda slot: slot.valid_at)
    rows: list[dict[str, Any]] = []
    for action in plan.actions[:20]:
        slot = next(
            (candidate for candidate in reversed(slots) if candidate.valid_at <= action.execute_not_before),
            slots[0],
        )
        rows.append(
            {
                "action_id": action.action_id,
                "valid_at": slot.valid_at,
                "expected_kw": slot.baseline_load_forecast_kw,
                "conservative_kw": slot.baseline_load_forecast_upper_kw,
            }
        )
    return rows


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
    }


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Convert snapshot values to bounded JSON-friendly shapes."""
    if depth >= 4:
        return "<truncated>"
    value = to_jsonable(value)
    if isinstance(value, dict):
        return {str(key): _bounded_json(item, depth=depth + 1) for key, item in list(value.items())[:24]}
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
    if old_state is None or new_state is None:
        return False
    state_changed = old_state.state != new_state.state
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    control_attribute_changed = any(
        old_attributes.get(key) != new_attributes.get(key) for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
    )
    if not state_changed and not control_attribute_changed:
        return False
    guard_entity = entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER)
    if guard_entity:
        guard_state = hass.states.get(guard_entity)
        if guard_state is not None and str(guard_state.state).lower() in {"on", "true", "1"}:
            return False
    return True


def _is_manual_override_helper_change(entry_data: dict[str, Any], event: Any) -> bool:
    """Return whether the authoritative override helper changed state."""
    entity_id = entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
    if not entity_id or event.data.get("entity_id") != entity_id:
        return False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if new_state is None:
        return False
    old_value = None if old_state is None else str(old_state.state).lower()
    new_value = str(new_state.state).lower()
    return new_value in {"on", "off"} and old_value != new_value


def _hvac_control_from_ownership(ownership: dict[str, Any]) -> dict[str, Any]:
    """Normalize current and legacy HVAC ownership for lifecycle planning."""
    stored_control = ownership.get("hvac_control")
    control = dict(stored_control) if isinstance(stored_control, dict) else {}
    if not control and any(
        key in ownership
        for key in (
            "hvac_control",
            "climate_automations",
            "planner_takeover_started_at",
            "planner_hvac_action_expires_at",
        )
    ):
        control["legacy_ownership"] = True
    hold_until = ownership.get("hvac_release_hold_until")
    if hold_until is not None:
        control.setdefault("released_until", hold_until)
    if control.get("phase") == "away_off" and _parse_datetime_or_none(control.get("started_at")) is None:
        takeover_started_at = ownership.get("planner_takeover_started_at")
        if _parse_datetime_or_none(takeover_started_at) is not None:
            control["started_at"] = takeover_started_at
    return control


def _is_manual_hvac_zone_change(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    store_data: dict[str, Any],
    event: Any,
) -> bool:
    """Return whether a configured zone changed unexpectedly during takeover."""
    entity_id = event.data.get("entity_id")
    if entity_id not in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES)):
        return False
    ownership = store_data.get("ownership", {})
    if not isinstance(ownership, dict) or not ownership.get("hvac_control"):
        return False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return False
    state_changed = old_state.state != new_state.state
    control_attribute_changed = False
    if str(entity_id).split(".", 1)[0] == "climate":
        old_attributes = getattr(old_state, "attributes", {}) or {}
        new_attributes = getattr(new_state, "attributes", {}) or {}
        control_attribute_changed = any(
            old_attributes.get(key) != new_attributes.get(key)
            for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
        )
    if not state_changed and not control_attribute_changed:
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
    *,
    pending_hvac_desired_state: dict[str, Any] | None = None,
) -> bool:
    """Return whether a control-state event follows a recent planner command."""
    entity_id = event.data.get("entity_id")
    asset = (
        "daikin"
        if entity_id == entry_data.get(CONF_DAIKIN_CLIMATE)
        else "daikin_zone"
        if entity_id in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
        else "enphase"
        if entity_id == entry_data.get(CONF_ENPHASE_PROFILE)
        else None
    )
    if asset is None:
        return False
    new_state = event.data.get("new_state")
    if new_state is None:
        return False
    if asset == "daikin" and pending_hvac_desired_state is not None:
        # A multi-call climate transaction can publish intermediate states (for
        # example turn_on restoring the previous mode before set_hvac_mode).
        # Preserve those expected mode transitions, but do not let the bounded
        # pending marker hide a user's different target or auxiliary setting.
        return _matches_pending_main_hvac_feedback(
            pending_hvac_desired_state,
            event,
        )
    if asset == "daikin_zone" and pending_hvac_desired_state is not None:
        if (
            str(entity_id).split(".", 1)[0] == "climate"
            and _matches_pending_coupled_zone_hvac_feedback(
                entry_data,
                pending_hvac_desired_state,
                event,
            )
        ):
            return True
        restored_zones = pending_hvac_desired_state.get("restore_zones")
        if isinstance(restored_zones, dict) and entity_id in restored_zones:
            if str(entity_id).split(".", 1)[0] == "climate":
                restored_target = restored_zones.get(entity_id)
                return isinstance(
                    restored_target,
                    dict,
                ) and _matches_pending_zone_hvac_feedback(
                    restored_target,
                    event,
                )
            return str(getattr(new_state, "state", "")).lower() == str(restored_zones[entity_id]).lower()
        if str(entity_id).split(".", 1)[0] == "climate":
            return (
                pending_hvac_desired_state.get("configured_zones_only") is True
                and _matches_pending_zone_hvac_feedback(
                    pending_hvac_desired_state,
                    event,
                )
            )
        return bool(
            pending_hvac_desired_state.get("enable_zones") and str(getattr(new_state, "state", "")).lower() == "on"
        )
    for outcome in reversed(list(store_data.get("execution_audit", []))):
        expected_asset = "daikin" if asset == "daikin_zone" else asset
        if (
            not isinstance(outcome, dict)
            or outcome.get("result") != "applied"
            or outcome.get("asset") != expected_asset
        ):
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
        if asset == "daikin_zone":
            if str(entity_id).split(".", 1)[0] == "climate":
                return _matches_hvac_command_feedback(desired, event, zone_entity=True)
            return bool(desired.get("enable_zones") and observed.lower() == "on")
        return _matches_hvac_command_feedback(desired, event)
    return False


def _matches_pending_main_hvac_feedback(
    pending: dict[str, Any],
    event: Any,
) -> bool:
    """Return whether a pending transaction explains a main-climate event."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if new_state is None:
        return False
    restore_main = pending.get("restore_main")
    expected = restore_main if isinstance(restore_main, dict) else pending
    old_mode = None if old_state is None else str(old_state.state)
    new_mode = str(new_state.state)
    expected_mode = expected.get("hvac_mode")
    mode_changed = old_state is None or old_mode != new_mode
    intermediate_mode_transition = False
    if mode_changed:
        expected_transition = expected_mode is not None and new_mode == str(expected_mode)
        turn_on_transition = (
            pending.get("turn_on_feedback_expected") is True
            and old_mode == "off"
            and new_mode in _ACTIVE_HVAC_MODES
        )
        remembered_mode_transition = (
            str(expected_mode) == "off"
            and new_mode == str(expected.get("rollback_active_hvac_mode"))
            and new_mode in _ACTIVE_HVAC_MODES
            and (
                old_mode != "off"
                or pending.get("turn_on_feedback_expected") is True
            )
        )
        intermediate_mode_transition = (
            turn_on_transition or remembered_mode_transition
        )
        if not (
            expected_transition
            or turn_on_transition
            or remembered_mode_transition
        ):
            return False
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    target_attribute_to_key = {
        "temperature": "target_temperature",
        "target_temp_low": "target_temp_low",
        "target_temp_high": "target_temp_high",
    }
    changed_target_attributes = [
        attribute
        for attribute in target_attribute_to_key
        if old_attributes.get(attribute) != new_attributes.get(attribute)
    ]
    if any(
        old_attributes.get(attribute) != new_attributes.get(attribute)
        for attribute in _HVAC_CONTROL_ATTRIBUTE_KEYS - set(target_attribute_to_key)
    ):
        return False
    if intermediate_mode_transition:
        # turn_on and remembered-mode restoration can legitimately expose that
        # mode's target before the transaction applies its final target.
        return True
    if not changed_target_attributes:
        return mode_changed
    targets_match = all(
        _matching_hvac_target(
            new_attributes.get(attribute),
            expected.get(target_attribute_to_key[attribute]),
        )
        for attribute in changed_target_attributes
    )
    return targets_match and (mode_changed or bool(changed_target_attributes))


def _is_pending_main_hvac_manual_change(
    entry_data: dict[str, Any],
    event: Any,
    pending: dict[str, Any] | None,
) -> bool:
    """Return whether a main event conflicts with the active transaction."""
    if (
        pending is None
        or event.data.get("entity_id") != entry_data.get(CONF_DAIKIN_CLIMATE)
    ):
        return False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return False
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    control_changed = old_state.state != new_state.state or any(
        old_attributes.get(key) != new_attributes.get(key)
        for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
    )
    return control_changed and not _matches_pending_main_hvac_feedback(
        pending,
        event,
    )


def _matches_pending_zone_hvac_feedback(
    expected: dict[str, Any],
    event: Any,
) -> bool:
    """Return whether a zone-climate event matches its pending target."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None or old_state.state != new_state.state:
        return False
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    target_attribute_to_key = {
        "temperature": "target_temperature",
        "target_temp_low": "target_temp_low",
        "target_temp_high": "target_temp_high",
    }
    if any(
        old_attributes.get(attribute) != new_attributes.get(attribute)
        for attribute in _HVAC_CONTROL_ATTRIBUTE_KEYS - set(target_attribute_to_key)
    ):
        return False
    changed_targets = [
        attribute
        for attribute in target_attribute_to_key
        if old_attributes.get(attribute) != new_attributes.get(attribute)
    ]
    return bool(changed_targets) and all(
        _matching_hvac_target(
            new_attributes.get(attribute),
            expected.get(target_attribute_to_key[attribute]),
        )
        for attribute in changed_targets
    )


def _matches_pending_coupled_zone_hvac_feedback(
    entry_data: dict[str, Any],
    pending: dict[str, Any],
    event: Any,
) -> bool:
    """Return whether a zone actuator explains linked climate mode feedback."""
    expected = pending.get("coupled_zone_feedback_expected")
    if not isinstance(expected, dict):
        return False
    expected_state = expected.get("state")
    expected_context_id = expected.get("context_id")
    if expected_state not in {"on", "off"} or not expected_context_id:
        return False
    event_context = getattr(event, "context", None)
    context_matches = expected_context_id in {
        getattr(event_context, "id", None),
        getattr(event_context, "parent_id", None),
    }
    entity_pair_matches = _unambiguous_coupled_zone_entity_ids_match(
        entry_data,
        expected.get("actuator_entity_id"),
        event.data.get("entity_id"),
    )
    if not context_matches and not (
        entity_pair_matches
        and getattr(event_context, "user_id", None) is None
        and getattr(event_context, "parent_id", None) is None
    ):
        return False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return False
    old_mode = str(old_state.state).lower()
    new_mode = str(new_state.state).lower()
    expected_transition = (
        old_mode == "off" and new_mode in _ACTIVE_HVAC_MODES
        if expected_state == "on"
        else old_mode in _ACTIVE_HVAC_MODES and new_mode == "off"
    )
    if not expected_transition:
        return False
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    return not any(
        old_attributes.get(attribute) != new_attributes.get(attribute)
        for attribute in _HVAC_CONTROL_ATTRIBUTE_KEYS
    )


def _coupled_zone_entity_ids_match(actuator_entity_id: Any, climate_entity_id: Any) -> bool:
    """Return whether configured entity IDs identify one actuator/climate pair."""
    actuator = str(actuator_entity_id or "")
    climate = str(climate_entity_id or "")
    if "." not in actuator or "." not in climate:
        return False
    actuator_domain, actuator_object_id = actuator.split(".", 1)
    climate_domain, climate_object_id = climate.split(".", 1)
    if actuator_domain == "climate" or climate_domain != "climate":
        return False
    return climate_object_id == actuator_object_id or climate_object_id.startswith(
        f"{actuator_object_id}_"
    )


def _unambiguous_coupled_zone_entity_ids_match(
    entry_data: dict[str, Any],
    actuator_entity_id: Any,
    climate_entity_id: Any,
) -> bool:
    """Return whether exactly one configured climate zone matches an actuator."""
    climate = str(climate_entity_id or "")
    matches = {
        entity_id
        for entity_id in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
        if _coupled_zone_entity_ids_match(actuator_entity_id, entity_id)
    }
    return matches == {climate}


def _pending_zone_hvac_manual_change_entity_id(
    entry_data: dict[str, Any],
    event: Any,
    pending: dict[str, Any] | None,
) -> str | None:
    """Return a configured zone whose event conflicts with pending control."""
    entity_id = str(event.data.get("entity_id") or "")
    if (
        pending is None
        or entity_id not in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
    ):
        return None
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return None
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    control_changed = old_state.state != new_state.state
    if entity_id.split(".", 1)[0] == "climate":
        control_changed = control_changed or any(
            old_attributes.get(key) != new_attributes.get(key)
            for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
        )
    if not control_changed:
        return None
    restored_zones = pending.get("restore_zones")
    if (
        entity_id.split(".", 1)[0] == "climate"
        and _matches_pending_coupled_zone_hvac_feedback(entry_data, pending, event)
    ):
        return None
    if isinstance(restored_zones, dict) and entity_id in restored_zones:
        expected = restored_zones[entity_id]
        if entity_id.split(".", 1)[0] == "climate":
            matches = isinstance(expected, dict) and _matches_pending_zone_hvac_feedback(
                expected,
                event,
            )
        else:
            matches = str(new_state.state).lower() == str(expected).lower()
        return None if matches else entity_id
    if entity_id.split(".", 1)[0] == "climate":
        if pending.get("configured_zones_only") is not True:
            # Other subordinate climate changes remain synchronously
            # classified despite the armed scheduler guard.
            return entity_id
        return (
            None
            if _matches_pending_zone_hvac_feedback(pending, event)
            else entity_id
        )
    if not pending.get("enable_zones"):
        # Likewise, a switch change is manual when this transaction did not
        # request zone enabling; the generic listener is guard-suppressed while
        # the transaction is active and cannot safely classify it later.
        return entity_id
    return None if str(new_state.state).lower() == "on" else entity_id


def _matching_hvac_target(observed: Any, expected: Any) -> bool:
    """Return whether Home Assistant published the expected climate target."""
    if expected is None:
        return False
    try:
        return abs(float(observed) - float(expected)) < 0.05
    except (TypeError, ValueError):
        return False


def _matches_hvac_command_feedback(
    desired: dict[str, Any],
    event: Any,
    *,
    zone_entity: bool = False,
) -> bool:
    """Return whether changed HVAC controls match the planner's command."""
    if zone_entity and desired.get("configured_zones_only") is not True:
        return False
    new_state = event.data.get("new_state")
    old_state = event.data.get("old_state")
    observed = str(getattr(new_state, "state", ""))
    old_observed = None if old_state is None else str(getattr(old_state, "state", ""))
    desired_mode = desired.get("hvac_mode")
    mode_changed = old_state is None or old_observed != observed
    matched_command_change = False
    if mode_changed:
        if desired_mode is None or observed != str(desired_mode):
            return False
        matched_command_change = True
    old_attributes = {} if old_state is None else (getattr(old_state, "attributes", {}) or {})
    attributes = getattr(new_state, "attributes", {}) or {}
    if any(old_attributes.get(key) != attributes.get(key) for key in _HVAC_CONTROL_ATTRIBUTE_KEYS - {"temperature"}):
        return False
    desired_temperature = desired.get("target_temperature")
    temperature_changed = old_state is None or old_attributes.get("temperature") != attributes.get("temperature")
    if temperature_changed:
        if desired_temperature is None:
            return matched_command_change
        observed_temperature = attributes.get("temperature")
        if not isinstance(observed_temperature, str | int | float) or not isinstance(
            desired_temperature, str | int | float
        ):
            return False
        try:
            if float(observed_temperature) != float(desired_temperature):
                return False
        except (TypeError, ValueError):
            return False
        matched_command_change = True
    return matched_command_change


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
    if not isinstance(old_value, str | int | float) or not isinstance(
        new_value, str | int | float
    ):
        return True
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


def _event_reports_ev_charging_started(
    entry_data: dict[str, Any],
    event: Any,
) -> bool:
    """Return whether mapped feedback newly reports active EV charging."""
    if event.data.get("entity_id") != entry_data.get(CONF_EV_CHARGING):
        return False
    new_state = event.data.get("new_state")
    if new_state is None or ev_charging_state(new_state.state) is not True:
        return False
    old_state = event.data.get("old_state")
    return old_state is None or ev_charging_state(old_state.state) is not True


def _ev_start_feedback_is_expected(
    executor: Any,
    now: datetime,
) -> bool:
    """Return whether charging feedback is expected from an issued EV start."""
    expected_until = _parse_datetime_or_none(
        getattr(executor, "ev_start_feedback_expected_until", None)
    )
    if expected_until is None:
        return False
    if expected_until.tzinfo is None:
        expected_until = expected_until.replace(tzinfo=UTC)
    normalized_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return bool(expected_until >= normalized_now)


def _consume_expected_ev_start_feedback(executor: Any, now: datetime) -> bool:
    """Consume one bounded expectation after its charging feedback arrives."""
    if not _ev_start_feedback_is_expected(executor, now):
        return False
    executor.ev_start_feedback_expected_until = None
    return True


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


def _overrides_from_store(
    store_data: dict[str, Any],
    now: datetime,
) -> list[Override]:
    """Restore non-expired overrides from Store data."""
    restored: list[Override] = []
    for item in store_data.get("overrides", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", ""))
        expires_at = _parse_datetime_or_none(item.get("expires_at"))
        if kind == "manual_ev_charging" and expires_at is None:
            continue
        if kind == "manual_hvac" and expires_at is None and str(item.get("source", "")) != "helper":
            continue
        if _override_is_expired(expires_at, now):
            continue
        restored.append(
            Override(
                kind=kind,
                source=str(item.get("source", "store")),
                expires_at=expires_at,
                reason=str(item.get("reason", "")),
            )
        )
    return restored


def _unexpired_overrides(
    overrides: list[Override],
    now: datetime,
) -> list[Override]:
    """Return runtime overrides that remain active at this refresh."""
    return [override for override in overrides if not _override_is_expired(override.expires_at, now)]


def _expired_manual_hvac_state(
    store_data: dict[str, Any],
    now: datetime,
) -> bool:
    """Return whether persisted manual HVAC state has reached its timeout."""
    ownership = store_data.get("ownership")
    if isinstance(ownership, dict):
        ownership_expiry = _parse_datetime_or_none(ownership.get("manual_hvac_override_expires_at"))
        if ownership_expiry is not None and _override_is_expired(ownership_expiry, now):
            return True
    overrides = store_data.get("overrides")
    if not isinstance(overrides, list):
        return False
    for item in overrides:
        if not isinstance(item, dict) or str(item.get("kind", "")) != "manual_hvac":
            continue
        expires_at = _parse_datetime_or_none(item.get("expires_at"))
        if expires_at is None and str(item.get("source", "")) == "helper":
            continue
        if expires_at is None or _override_is_expired(expires_at, now):
            return True
    return False


def _override_is_expired(
    expires_at: datetime | None,
    now: datetime,
) -> bool:
    """Compare override timestamps defensively across legacy naive values."""
    if expires_at is None:
        return False
    normalized_expires_at = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    normalized_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return normalized_expires_at <= normalized_now
