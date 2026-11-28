"""Coordinator for Energy Planner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from functools import cached_property, partial
from itertools import islice
from math import isfinite
from time import monotonic, perf_counter
from typing import Any, cast

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_state_report_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import advice_runtime, startup_recovery, task_lifecycle
from .action_limits import budget_history, has_future_export_attempt
from .adapter_helpers import (
    async_call_device_service,
    service_entity_available,
    zone_temperature_sync_deferred,
)
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
from .availability import AvailabilityIdentity, availability_details
from .calendar_history import calendar_feedback_recheck_delay
from .climate_inputs import finite, hvac_feedback_is_assumed, hvac_feedback_is_observed
from .climate_inputs import temperature_targets_match as _matching_hvac_target
from .climate_learning import observe
from .climate_runtime import update_readiness
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
    CONF_DAIKIN_POWER,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGE_RATE_KW,
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
    CONF_FORECAST_FRESHNESS_MINUTES,
    CONF_GRID_IMPORT_LIMIT_KW,
    CONF_HOUSEHOLD_LOAD,
    CONF_HOUSEHOLD_LOAD_OUTAGE_GRACE_MINUTES,
    CONF_LOAD_RECOVERY_MAX_AGE_MINUTES,
    CONF_MANUAL_HVAC_OVERRIDE_MINUTES,
    CONF_MATERIAL_CHANGE_THRESHOLD_PERCENT,
    CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS,
    CONF_PERSON_ENTITIES,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PRICE_FRESHNESS_MINUTES,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_SECONDARY,
    CONF_WEATHER,
    DEBOUNCE_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    EV_RESERVATION_EXTERNAL_BASELINE,
    HVAC_DRY_TAKEOVER_SUMMARY,
    HVAC_DRY_ZONE_TARGETS_ABSENT,
    HVAC_ZONE_SETTLE_MAX_SECONDS,
    HVAC_ZONE_SETTLE_SECONDS,
    MIN_NON_MANUAL_REFRESH_INTERVAL_SECONDS,
    STATE_UNKNOWN_VALUES,
)
from .constraints import ConstraintValidator
from .discovery import CapabilityDiscovery, CapabilityEvidence
from .enphase_export_limit import (
    COMPANION_UPGRADE_REQUIRED,
    EXPORT_IDENTITY_ISSUES,
    GATEWAY_CHANGED,
    GATEWAY_ISSUES,
    GATEWAY_UNVERIFIED,
    RETRY_INTERVAL,
    TARGET_CHANGED,
    UPSTREAM_CONTROLS_DISABLED,
    ExportLimitControl,
    RetryWriteDeferred,
    current_tariff_blocks,
    expected_setting_matches,
    gateway_identity_issue,
    installer_entry_transitioning,
    installer_gateway_evidence_lost,
    installer_runtime,
    owned_feedback_issue,
    preserve_retry_on_reload,
    probe_installer,
)
from .enphase_export_limit import evidence as export_limit_evidence
from .enphase_export_limit import feedback as export_limit_feedback
from .enphase_export_limit import feedback_entity_id as export_limit_feedback_entity_id
from .enphase_export_limit import target_identity as export_target_identity
from .entry_data import combined_entry_data
from .ev import ev_charging_state
from .ev_adapter import EVChargerAdapter, EVCommandResult
from .ev_control import _ev_action_wants_power
from .ev_runtime import EVRestoreFeedback, audit_evidence, ev_cap_may_become_active
from .ev_telemetry import reported_at, sample_ev, update_ev_telemetry
from .executor import _EV_SAFETY_PLAN_ISSUES, PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE, Executor, _ev_control_topology
from .export_limit_cooldown import control_key, cooldown_status, normalized_record, same_control
from .export_limit_cooldown import interval as export_change_interval
from .export_limit_policy import (
    EXPORT_ASSET,
    ExportTariffCache,
    area_safe,
    aware_time,
    block_time,
    confirmed_feedback,
    export_action,
    export_tariff_is_fresh,
    export_tariff_issued_at,
)
from .forecast_calibration import FORECAST_CALIBRATION_VERSION, update_forecast_calibration
from .forecasts import (
    MAX_FORECAST_SOURCE_ROWS,
    TARIFF_VALUE_KEYS,
    TariffSnapshotBudget,
    _canonical_key,
    cacheable_tariff_snapshot,
    tariff_mapping_items,
    tariff_snapshot_matches,
    tariff_source_attributes,
)
from .hvac_control import has_hvac_ownership
from .inputs import InputManager, _percent_float_or_none, _tariff_age_band
from .load_forecast import normalize_power_kw
from .models import (
    ActionAsset,
    ActionOutcome,
    DecisionContext,
    EnergyPlan,
    InputHealth,
    Override,
    PlanAction,
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
from .refresh_timing import CURRENT_REFRESH, RefreshTiming, current_timing, request_owner, stage
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
from .thermal_learning import sample as thermal_sample
from .thermal_learning import update as update_zone_thermal_model
from .thermal_model import thermal_model_summary
from .training import HistoryTraining, TrainingRequest, TrainingResult, training_request
from .type_defs import EnergyPlannerConfigEntry
from .vehicles import (
    AUTO,
    MANUAL,
    VEHICLES,
    VehicleCalibration,
    VehicleSession,
    connection,
    state_value,
    vehicle_entity_ids,
)
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
_EXPORT_OPERATOR_ARM_REVALIDATE = "export_operator_arm_revalidate"
_EXPORT_OPERATOR_ARM_COMPLETED = "completed"
_EXPORT_PREFLIGHT_REFRESH: ContextVar[tuple[EnergyPlannerCoordinator, asyncio.Task[Any] | None] | None] = ContextVar(
    "export_preflight_refresh", default=None,
)


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
        *TARIFF_VALUE_KEYS,
        "outdoor_temperature_forecast_c",
        "temperature",
        "min_temp",
        "max_temp",
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
        "ev_power_entity", "ev_energy_entity", "ev_power_limit_entity",
        CONF_AMBER_IMPORT_PRICE,
        CONF_AMBER_EXPORT_PRICE,
        CONF_PV_FORECAST,
        CONF_CARBON_INTENSITY_FORECAST,
        CONF_BATTERY_SOC,
        CONF_ENPHASE_PROFILE,
        CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
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


class InitialPlanSuperseded(UpdateFailed):
    """Retry setup after changes invalidate the first unpublished plan."""


class InitialHvacHandoffFailed(ConfigEntryNotReady):
    """Setup cannot safely restore HVAC after a failed manual handoff save."""


class _RefreshMode(Enum):
    DEBOUNCED = auto()  # Respect the local refresh floor and Core's debouncer.
    IMMEDIATE = auto()  # Enter Core's serialized refresh directly.


@dataclass(slots=True)
class _EVCapacityWork:
    save_needed: bool = False
    save_pending: bool = False
    invalidate_on_resume: bool = False
    refresh_after_save: bool = False
    refresh_needed: bool = False
    refresh_pending: bool = False
    refresh_request: object | None = None


@dataclass(frozen=True, slots=True)
class _DebounceRequest:
    deadline: float
    trigger: str
    mode: _RefreshMode
    priority: bool


@dataclass(slots=True)
class _QueuedDirectRefresh:
    trigger: str
    requested_at: float
    owner: asyncio.Task[Any] | None = None


@dataclass(frozen=True, slots=True)
class _UnchangedCalendarFeedback:
    history: dict[str, list[dict[str, str]]]
    records: list[dict[str, str]]
    observed_at: datetime
    recheck_delay: timedelta


@dataclass(slots=True)
class _HvacOutages:
    baselines: dict[str, tuple[State | None, set[str]]] = field(default_factory=dict)
    prior_attributed_contexts: dict[str, deque[str]] = field(default_factory=dict)
    unavailable_attributed_contexts: dict[str, deque[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _HvacOutageFeedback:
    baseline: State | None
    excluded_context_ids: set[str]
    manual_stage: str | None = None


@dataclass(slots=True)
class _HvacHandoff:
    main: bool = False
    zones: set[str] = field(default_factory=set)
    release: bool = False
    reason: str = "climate_zone_changed"


@dataclass(frozen=True, slots=True)
class _PlanExecution:
    generation: int
    plan: EnergyPlan
    context: DecisionContext
    options: dict[str, Any]
    mapped_entry_data: dict[str, Any] | None = None
    tariffs_current: Callable[[], bool] | None = None
    entry_options: dict[str, Any] | None = None
    ev_capacity_request: object | None = None


@dataclass(slots=True)
class _ExportOperatorArm:
    previous_production: dict[str, Any]
    resume_startup_recovery: bool = False
    rollback_pending: bool = False


@dataclass(frozen=True, slots=True)
class _ExportReconciliationInputs:
    entity: str
    feedback: dict[str, Any]
    issue: str | None
    last_readback: int | float | None
    active_control: bool
    enabled: bool
    change_interval: float
    startup_validation: bool
    restore_deferred: bool
    ownership: dict[str, Any]
    reusable: bool = field(compare=False)


def _export_feedback_settled(
    feedback: dict[str, Any], issue: str | None, ownership: dict[str, Any],
) -> bool:
    return (
        issue is None and feedback.get("pending") is False
        and not ownership.get("pending") and not ownership.get("restoring")
    )


class EnergyPlannerCoordinator(DataUpdateCoordinator[EnergyPlan | None]):
    """Manage planner refresh and entity state."""

    def __init__(self, hass: HomeAssistant, entry: EnergyPlannerConfigEntry, store: PlannerStore) -> None:
        """Initialize coordinator."""
        self._availability_startup_deadline = monotonic() + (
            0 if getattr(hass, "is_running", True) else startup_recovery.STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS
        )
        self.hass = hass
        self.entry = entry
        self.store = store
        self.vehicle_session = VehicleSession(store.data.get("ev_vehicle_session"))
        self.vehicle_calibration = VehicleCalibration()
        self._vehicle_ownership_key: tuple[Any, ...] | None = None
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
        self._vehicle_ready_by_settings = {
            p["id"]: p[CONF_DEFAULT_READY_BY] for p in combined_entry_data(self.entry).get(VEHICLES, [])
        }
        self.executor = Executor(
            store,
            hass=hass,
            entry_data=self.entry_data,
            options=self.options,
            notification_grace_until=dt_util.utcnow() + PLAN_FALLBACK_STARTUP_NOTIFICATION_GRACE,
            entry_id=getattr(entry, "entry_id", None),
            entry_title=getattr(entry, "title", None),
        )
        self.executor.ev_command_guard = self._ev_command_guard
        self.executor.ev_restore_guard = self._ev_restore_allowed
        self.executor.hvac_restore_guard = lambda: not self.hvac_handoff_unresolved
        self.executor.export_write_blocked = self._export_dispatch_blocked
        self.executor.export_cooldown_options = lambda: self.options
        self.executor.log_dry_takeover_transition = self._log_dry_takeover_transition
        self.executor.operator_arm_confirmed = self._operator_arm_confirmed
        self.executor.ev_allocation_deadline_callback = self._schedule_ev_allocation_deadline
        self.executor.current_ev_allocation_deadline = lambda: self._ev_allocation_deadline
        self._ev_allocation_cancel: Callable[[], None] | None = None
        self._ev_allocation_deadline: Any = None
        self._ev_capacity_work: _EVCapacityWork | None = None
        self._ev_capacity_flush_needed = False
        self._failed_startup_restore_running = False
        self._failed_startup_restore_needed = False
        self._failed_startup_restore_watch_cancel: Callable[[], None] | None = None
        self._failed_startup_restore_flush_needed = False
        self._failed_startup_restore_hvac_entities: frozenset[str] = frozenset()
        self._failed_startup_restore_device_scopes: dict[str, set[str]] = {}
        self._failed_startup_restore_ev_feedback = EVRestoreFeedback()

        self._unsub_listeners: list[Callable[[], None]] = []
        self._debounce_cancel: Callable[[], None] | None = None
        self._boundary_cancel: Callable[[], None] | None = None
        self._planning_boundary_at: datetime | None = None
        self._tariff_retry_cancel: Callable[[], None] | None = None
        self._tariff_retry_at: datetime | None = None
        self._tariff_retry_window_end: datetime | None = None
        self._tariff_retry_last_requested_at: float | None = None
        self._export_boundary_cancel: Callable[[], None] | None = None
        self._export_boundary_at: datetime | None = None
        self._export_boundary_fire_at: float | None = None
        self._ev_auto_start_retry_cancel: Callable[[], None] | None = None
        self._ev_auto_start_compensation_pending = False
        self._ev_auto_start_compensation_generation = 0
        self._planner_lock = asyncio.Lock()
        self.entry_update_lock = asyncio.Lock()
        self._options_update_lock = asyncio.Lock()
        self._device_control_lock = asyncio.Lock()
        self._production_arm_request: object | None = None
        self._export_operator_arm: _ExportOperatorArm | None = None
        self._preflight_tariff_cache = ExportTariffCache()
        self._operator_disarm_request: object | None = None
        self._command_lock = asyncio.Lock()
        self._last_handled_options = dict(entry.options)
        self._last_control_mode_state = (self.planner_enabled, self.dry_run)
        self.entry_topology_signature: tuple[Any, ...] | None = None
        self._last_zone_mappings = deepcopy(self.entry_data.get("hvac_zone_mappings", {}))
        self._refresh_generation = 0
        self._last_non_manual_refresh_requested_at: float | None = None
        self._pending_refresh_trigger = "startup"
        self._last_decision_fingerprint: str | None = None
        self._last_tariff_fingerprints: dict[str, _TariffFingerprint] = {}
        self._last_decision_context: DecisionContext | None = None
        self._last_decision_generation: int | None = None
        self._retained_forecast_snapshot: dict[str, Any] = {}
        self._tearing_down = False
        self._force_next_refresh = False
        self._manual_override_helper_guard: tuple[str, datetime] | None = None
        self._weather_fetch_lock: asyncio.Lock | None = None
        self._weather_forecast_cache: dict[str, Any] = {}
        self.weather_forecast_diagnostics: dict[str, Any] = {}
        self._load_forecast_training_attempted = False
        self._refresh_completed_times: list[float] = []
        self._refresh_trigger_counts: dict[str, int] = {}
        self._last_phase_durations: dict[str, float] = {}
        self._export_reconcile_task: asyncio.Task[None] | None = None
        self._export_reconcile_pending = False
        self._export_reconciled_for: _ExportReconciliationInputs | None = None
        self._settled_export_reconciliation: _ExportReconciliationInputs | None = None
        self._export_replan_generations: tuple[int, int] | None = None
        self._export_boundary_followup_due = False
        self._last_export_reconciliation: dict[str, Any] = {}
        self._refresh_request_timing: tuple[str, float, asyncio.Task[Any] | None] | None = None
        self._debounce_requested_at: float | None = None
        self._debounce_request: _DebounceRequest | None = None
        self._queued_direct_refresh: _QueuedDirectRefresh | None = None
        self._ai_advice_task: asyncio.Task[None] | None = None
        self._plan_execution_task: asyncio.Task[None] | None = None
        self._pending_plan_execution: _PlanExecution | None = None
        self._pending_plan_commit: Callable[[], Awaitable[EnergyPlan | None]] | None = None
        self._deferred_plan_execution: _PlanExecution | None = None
        self._calendar_feedback_flush_needed = False
        self._calendar_feedback_flush_pending = False
        self._ai_advice_fingerprint: str | None = None
        self._ai_advice_pending_fingerprint: str | None = None
        self._ai_advice_pending_reason: str | None = None
        self._ai_current_plan_fingerprint: str | None = None
        self._ai_current_plan_safe = False
        self._startup_auto_recovery_authorized = False
        self._export_retry_startup_blocked = False
        self._startup_auto_recovery_deadline: float | None = None
        self._startup_auto_recovery_task: asyncio.Task[None] | None = None
        self._startup_auto_recovery_start_unsub: Callable[[], None] | None = None
        self._startup_auto_recovery_wakeup = asyncio.Event()
        self._load_recovery_ready = asyncio.Event()
        self._load_recovery_pending = False
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
        options = {**self.options, CONF_DEFAULT_READY_BY: self.ready_by}
        if VEHICLES in combined_entry_data(self.entry):
            self._update_vehicle_session()
            return self.vehicle_session.options(options)
        return options

    @property
    def entry_data(self) -> dict[str, Any]:
        """Return merged hub and input subentry data."""
        data = combined_entry_data(self.entry)
        if VEHICLES in data:
            self._update_vehicle_session()
            return self.vehicle_session.resolve(data)
        return data

    def _update_vehicle_session(self) -> bool:
        """Reconcile identity synchronously, including during an adapter retry."""
        data = combined_entry_data(self.entry)
        if VEHICLES not in data:
            return False
        if (self.vehicle_session.was_plugged
            and connection(state_value(self.hass, data.get(CONF_EV_CONNECTED))) is False
        ):
            self._ev_unplug_generation = getattr(self, "_ev_unplug_generation", 0) + 1
            self._ev_last_unplug_at = (
                reported_at(self.hass.states.get(data[CONF_EV_CONNECTED])) or dt_util.utcnow()
            )
        old_signature = self.vehicle_session.signature
        changed = self.vehicle_session.update(self.hass, data)
        if changed and self.vehicle_session.signature is not None and (
            old_signature is None or self.vehicle_session.signature[:4] != old_signature[:4]
        ):
            self.vehicle_calibration.pending = None
            # Reset before InputManager builds the new vehicle's context. Waiting
            # until execution would let the old override shape its charging plan.
            self.overrides = [o for o in getattr(self, "overrides", []) if o.kind != "manual_ev_charging"]
        if changed and hasattr(self, "_refresh_generation"):
            self._refresh_generation += 1
            self._clear_ev_auto_start_compensation()
        return changed

    def _ev_command_guard(self) -> Callable[[], bool]:
        """Capture a session token and revalidate it at every service boundary."""
        if VEHICLES not in combined_entry_data(self.entry):
            return lambda: True
        self._update_vehicle_session()
        generation = self.vehicle_session.generation

        def permitted() -> bool:
            self._update_vehicle_session()
            return self.vehicle_session.allowed and self.vehicle_session.generation == generation

        return permitted

    def _ev_restore_allowed(self) -> bool:
        """Never restore a previous vehicle's baseline during startup or a swap."""
        if VEHICLES not in combined_entry_data(self.entry):
            return True
        self._update_vehicle_session()
        signature = self.vehicle_session.signature
        return bool(signature and self._vehicle_ownership_key == signature[:4] and self.vehicle_session.allowed)

    async def _async_reconcile_vehicle_ownership(self) -> None:
        """Drop old session control without changing the charger state."""
        if VEHICLES not in combined_entry_data(self.entry):
            return
        self._update_vehicle_session()
        key = self.vehicle_session.signature[:4] if self.vehicle_session.signature else None
        unplug_generation = getattr(self, "_ev_unplug_generation", 0)
        unplugged = unplug_generation != getattr(self, "_ev_reconciled_unplug_generation", 0)
        if key != self._vehicle_ownership_key or unplugged:
            await self.executor.async_release_ev_policy(
                unplugged=unplugged, unplugged_at=getattr(self, "_ev_last_unplug_at", None) if unplugged else None,
            )
            self.overrides = [o for o in self.overrides if o.kind != "manual_ev_charging"]
            await self.store.async_save_overrides(self.overrides)
            self._vehicle_ownership_key = key
            # Only acknowledge boundaries included in this release. A newer
            # unplug can arrive while either durable write is awaiting I/O.
            self._ev_reconciled_unplug_generation = unplug_generation

    async def _async_save_vehicle_session(self) -> None:
        """Save current evidence when a queued listener task runs."""
        await self.store.async_save_vehicle_session(self.vehicle_session.snapshot())

    async def async_select_vehicle(self, selection: str) -> None:
        """Select a vehicle or release charging policy until the next unplug."""
        profiles = combined_entry_data(self.entry).get(VEHICLES, [])
        if selection not in {AUTO, MANUAL, *(p["id"] for p in profiles)}:
            raise ValueError("Unknown vehicle selection")
        self._update_vehicle_session()
        self.vehicle_session.selection = selection
        self._update_vehicle_session()
        # Invalidate in-flight adapters before waiting for command ownership.
        self._clear_ev_auto_start_compensation()
        await self.store.async_save_vehicle_session(self.vehicle_session.snapshot())
        async with self._command_lock:
            await self._async_reconcile_vehicle_ownership()
        self.async_update_listeners()
        self._mark_forced_refresh("vehicle_selection")
        await self.async_request_refresh()

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
        return self.automatic_control_requested and production.armed and self._operator_arm_confirmed()

    def _operator_arm_confirmed(self) -> bool:
        """Allow only previously granted authority during a tentative operator arm."""
        pending = getattr(self, "_export_operator_arm", None)
        return pending is None or pending.previous_production.get("armed") is True

    @property
    def effective_control(self) -> bool:
        """Return whether current evidence permits automatic device commands."""
        if not self.active_control:
            return False
        return bool(build_preflight_report(self.hass, self, require_export_write_gate=True).get("active_control_ready"))

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
            "exclusive_phases_ms": dict(getattr(self, "last_refresh_metadata", {}).get("exclusive_phases_ms", {})),
            "export_reconciliation_pending": getattr(self, "_export_reconcile_pending", False),
            "export_reconciliation": dict(getattr(self, "_last_export_reconciliation", {})),
        }

    async def async_config_entry_first_refresh(self) -> None:
        """Capture device feedback while the first plan awaits publication."""
        async with self._async_capture_initial_device_feedback(release_hvac=True):
            await super().async_config_entry_first_refresh()

    def observe_initial_ev_feedback(self, observations: ExitStack) -> bool:
        """Track retained charger feedback until the normal listeners attach."""
        return self.executor.observe_ev_setup(observations, lambda increased: self._schedule_ev_capacity_feedback(
            invalidate=increased, refresh=False,
        ))

    def _retain_native_ev_signal(self, entity_id: str, state: State | None, previous: State | None) -> None:
        if (increased := self.executor.note_native_ev_signal_report(entity_id, state, previous)) is not None:
            self._schedule_ev_capacity_feedback(invalidate=increased, refresh=increased)

    @asynccontextmanager
    async def _async_capture_initial_device_feedback(self, *, release_hvac: bool) -> AsyncIterator[None]:
        """Preserve manual HVAC handoffs before runtime listeners are attached."""
        from .daikin_memory import owned_control_data

        entry_data = self.entry_data
        ownership = self.store.data.get("ownership", {})
        control = ownership.get("hvac_control", {})
        control_data = owned_control_data(entry_data, control)
        entities = {
            entity for data in (entry_data, control_data) for key in (CONF_EV_CHARGING, CONF_DAIKIN_CLIMATE)
            if (entity := data.get(key))
        }
        for data in (entry_data, control_data):
            entities.update(_split_entity_values(data.get(CONF_CLIMATE_ZONES)))
        entities.update(control.get("zone_states", {}))
        if not entities:
            yield
            return
        unchanged_calendar_feedback: dict[ActionAsset, _UnchangedCalendarFeedback] = {}
        outages: _HvacOutages = self.__dict__.setdefault("_hvac_outages", _HvacOutages())
        handoff = _HvacHandoff()
        self._initial_hvac_handoff: _HvacHandoff | None = handoff

        @callback
        def capture_feedback(event: Any) -> None:
            current_ownership = self.store.data.get("ownership", {})
            current_control = current_ownership.get("hvac_control", {})
            current = owned_control_data(self.entry_data, current_control)
            entity_id = event.data["entity_id"]
            is_main = entity_id == current.get(CONF_DAIKIN_CLIMATE)
            now = dt_util.utcnow()
            if is_main:
                self.executor.hvac_mode_feedback.mode_feedback_at(
                    entity_id, event.data.get("new_state"), previous=event.data.get("old_state"),
                )
            if (is_main or entity_id in _split_entity_values(current.get(CONF_CLIMATE_ZONES))
                    or entity_id in current_control.get("zone_states", {})):
                can_supersede = (self.executor.pending_hvac_desired_state is not None
                                 or has_hvac_ownership(current_ownership))
                outage = _track_hvac_outage(event, entity_id, outages, can_supersede=can_supersede)
                if outage.manual_stage is not None:
                    self._record_hvac_outage_feedback(event, entity_id, outage, now)
                direct_edit = can_supersede and (
                    _is_first_refresh_hvac_edit(event, outage.excluded_context_ids)
                    or self._manual_edit_before_first_hvac_observation(
                        event, entity_id, outage, outages, is_main=is_main,
                    )
                )
                if outage.manual_stage is not None or direct_edit:
                    self._initial_hvac_manual_pending = True
                    self._mark_explicit_hvac_change(entity_id, is_main=is_main)
                    _remember_hvac_manual_context(event, entity_id, outages)
                    if is_main:
                        handoff.main = True
                        handoff.reason = "daikin_state_changed"
                    else:
                        handoff.zones.add(entity_id)
            self._record_calendar_feedback(event, current, now, unchanged_calendar_feedback)

        unsubscribe = async_track_state_change_event(self.hass, entities, capture_feedback)
        completed = False
        try:
            yield
            completed = True
        finally:
            captured_handoff = handoff.main or bool(handoff.zones)
            try:
                if captured_handoff:
                    handoff.release = release_hvac and completed
                    await self._async_finish_initial_hvac_handoff(handoff)
            finally:
                self._initial_hvac_manual_pending = False
                self._initial_hvac_handoff = None
                unsubscribe()
        if captured_handoff and release_hvac:
            self._refresh_after_initial_hvac_handoff = True

    async def _async_finish_initial_hvac_handoff(self, handoff: _HvacHandoff) -> None:
        """Keep accepted scopes alive until the setup task can safely exit."""
        cancellation: asyncio.CancelledError | None = None
        record_override = True
        try:
            while handoff.main or handoff.zones:
                worker = asyncio.create_task(self._async_apply_initial_hvac_handoff(handoff, record_override))
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError as error:
                        handoff.release = False
                        cancellation = error
                worker.result()
                # The listener remains active until this task resumes. Drain
                # scopes accepted after the worker's last persistence pass.
                handoff.release = False
                record_override = False
        except Exception as error:
            self._hvac_handoff_failed = True
            handoff.release = False
            handoffs = self.hass.data.setdefault(DOMAIN, {}).setdefault("initial_hvac_handoffs", {})
            handoffs[self.entry.entry_id] = handoff
            raise InitialHvacHandoffFailed("Could not complete the initial manual HVAC handoff") from error
        if cancellation is not None:
            raise cancellation

    async def _async_apply_initial_hvac_handoff(self, handoff: _HvacHandoff, record_override: bool) -> None:
        """Make all first-refresh supersessions durable before release or setup cleanup."""
        reason = handoff.reason
        duration = int(self.options[CONF_MANUAL_HVAC_OVERRIDE_MINUTES])
        async with self._command_lock:
            async with self._planner_lock:
                await self._async_persist_hvac_supersessions(handoff)
                if record_override and not handoff.release:
                    await self._async_store_manual_hvac_override(duration, reason, source="service", expires=True)
                    await self.store.async_flush()
            if handoff.release:
                _, helper_error = await self._async_set_manual_hvac_override(duration, reason, initial_handoff=handoff)
                if helper_error is not None:
                    _LOGGER.warning("Could not update the manual HVAC override helper during setup")
            async with self._planner_lock:
                await self._async_persist_hvac_supersessions(handoff)

    async def async_retry_initial_hvac_handoff(self) -> None:
        """Save an accepted setup handoff before cleanup or a replacement setup."""
        handoffs = getattr(self.hass, "data", {}).get(DOMAIN, {}).get("initial_hvac_handoffs", {})
        handoff = handoffs.get(self.entry.entry_id)
        if handoff is None:
            return
        self._hvac_handoff_failed = True
        try:
            await self._async_apply_initial_hvac_handoff(handoff, record_override=True)
        except (OSError, HomeAssistantError) as error:
            raise InitialHvacHandoffFailed("Could not save the initial manual HVAC handoff") from error
        handoffs.pop(self.entry.entry_id, None)
        self._hvac_handoff_failed = False

    async def _async_persist_hvac_supersessions(self, handoff: _HvacHandoff) -> None:
        """Drain edits that arrived while earlier supersessions were being saved."""
        retried = False
        while handoff.main or handoff.zones:
            main, zones = handoff.main, handoff.zones
            handoff.main, handoff.zones = False, set()
            try:
                await self.executor.async_persist_hvac_manual_supersessions(main, zones)
            except (Exception, asyncio.CancelledError) as error:
                handoff.main |= main
                handoff.zones.update(zones)
                if retried or not isinstance(error, (OSError, HomeAssistantError)):
                    raise
                retried = True

    @callback
    def _record_hvac_outage_feedback(
        self, event: Any, entity_id: str, outage: _HvacOutageFeedback, now: datetime,
    ) -> None:
        """Audit availability transitions and attributed controls during an outage."""
        audit = getattr(self.store, "record_climate_event", None)
        if callable(audit):
            new = event.data.get("new_state")
            audit({
                "stage": outage.manual_stage or "availability_feedback",
                "entity_id": entity_id,
                "override_source": "user" if outage.manual_stage else None,
                "user_attributed": outage.manual_stage is not None,
                "context_id": getattr(getattr(event, "context", None), "id", None),
                "expected": {"mode": getattr(outage.baseline, "state", None)}
                if outage.manual_stage == "manual_recovery" else None,
                "observed": {
                    "mode": getattr(new, "state", None),
                    **{key: (getattr(new, "attributes", {}) or {}).get(key)
                       for key in _HVAC_CONTROL_ATTRIBUTE_KEYS},
                },
            }, now=now)

    @callback
    def _mark_explicit_hvac_change(self, entity_id: str, *, is_main: bool) -> None:
        """Invalidate command feedback and supersede any active transaction."""
        executor = self.executor
        getattr(executor, "hvac_feedback_commands", []).clear()
        if getattr(executor, "pending_hvac_desired_state", None) is not None:
            if is_main:
                executor.mark_pending_hvac_manual_override()
            else:
                executor.mark_pending_hvac_zone_manual_override(entity_id)

    def _manual_edit_before_first_hvac_observation(
        self, event: Any, entity_id: str, outage: _HvacOutageFeedback, outages: _HvacOutages, *, is_main: bool = False,
    ) -> bool:
        """Protect explicit controls on the first report from an unavailable owned actuator."""
        if outage.baseline is not None:
            return False
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if hvac_feedback_is_observed(old) or not hvac_feedback_is_observed(new):
            return False
        old_context = getattr(getattr(old, "context", None), "id", None)
        excluded = outage.excluded_context_ids.union(
            outages.unavailable_attributed_contexts.get(entity_id, ()),
            (old_context,) if old_context else (),
        )
        if (
            not _fresh_hvac_user_context_ids(event, excluded)
            or not startup_recovery._failed_startup_has_restore_targets(self)
        ):
            return False
        control = self.store.data.get("ownership", {}).get("hvac_control", {})
        target = control.get("main_state") if is_main else control.get("zone_states", {}).get(entity_id)
        if isinstance(target, str):
            return new.state in {"on", "off"} and new.state != target
        if not isinstance(target, dict):
            return False
        if (mode := target.get("hvac_mode")) and new.state != mode:
            return True
        return any(
            (saved := finite(target.get(saved_key))) is not None
            and (observed := finite(new.attributes.get(observed_key))) is not None
            and not _matching_hvac_target(saved, observed)
            for saved_key, observed_key in (
                ("target_temperature", "temperature"),
                ("target_temp_low", "target_temp_low"),
                ("target_temp_high", "target_temp_high"),
            )
        )

    @callback
    def _queue_manual_hvac_change(
        self, event: Any, entity_id: str, outages: _HvacOutages, *, is_main: bool,
    ) -> None:
        """Track accepted intent, restore targets and the queued handoff together."""
        _remember_hvac_manual_context(event, entity_id, outages)
        zone = None if is_main else entity_id
        self._remember_runtime_hvac_supersession(main=is_main, zone=zone)
        command = (
            self._async_handle_manual_hvac_change("daikin_state_changed", preserve_main_state=True)
            if is_main else self._async_handle_manual_hvac_change("climate_zone_changed", preserve_zone_entity_id=zone)
        )
        task = self._async_create_listener_task(command)
        if task is not None and not task.done():
            handoffs: set[asyncio.Task[Any]] = self.__dict__.setdefault("_manual_hvac_handoff_tasks", set())
            handoffs.add(task)
            task.add_done_callback(handoffs.discard)

    @callback
    def _remember_runtime_hvac_supersession(self, *, main: bool = False, zone: str | None = None) -> None:
        """Preserve an accepted handoff while its task waits for command ownership."""
        handoff: _HvacHandoff = self.__dict__.setdefault("_runtime_hvac_handoff", _HvacHandoff())
        handoff.main |= main
        if zone:
            handoff.zones.add(zone)

    def _automatic_hvac_command_allowed(self) -> bool:
        """Gate automatic work until explicit manual handoffs have completed."""
        return not (getattr(self, "_initial_hvac_manual_pending", False)
                    or getattr(self, "_manual_hvac_handoff_tasks", ())
                    or self.hvac_handoff_unresolved)

    @property
    def hvac_handoff_unresolved(self) -> bool:
        """Return whether restoration targets superseded by a user are still outstanding."""
        return getattr(self, "_hvac_handoff_failed", False) is True or any(
            handoff is not None and bool(handoff.main or handoff.zones) for handoff in (
                getattr(self, "_initial_hvac_handoff", None), getattr(self, "_runtime_hvac_handoff", None),
            )
        )

    async def _async_persist_runtime_hvac_supersessions(self) -> None:
        """Retire every accepted runtime restore target before a handoff release."""
        handoff: _HvacHandoff | None = getattr(self, "_runtime_hvac_handoff", None)
        if handoff is None or not (handoff.main or handoff.zones):
            return
        try:
            await self._async_persist_hvac_supersessions(handoff)
        except (OSError, HomeAssistantError):
            self._hvac_handoff_failed = True
            raise
        self._hvac_handoff_failed = False

    @callback
    def _record_calendar_feedback(
        self, event: Any, entry_data: dict[str, Any], now: datetime,
        unchanged_calendar_feedback: dict[ActionAsset, _UnchangedCalendarFeedback],
    ) -> None:
        """Record confirmed activity from one device without scheduling control."""
        entity_id = event.data.get("entity_id")
        if not entity_id or entity_id not in {entry_data.get(CONF_EV_CHARGING), entry_data.get(CONF_DAIKIN_CLIMATE)}:
            return
        accepted_plan = getattr(self.store, "calendar_plan", None) or getattr(self, "data", None)
        proposed_plan = getattr(self.store, "pending_calendar_plan", None)
        calendar_plans = [plan for plan in (accepted_plan, proposed_plan) if plan is not None]
        if not calendar_plans or not (
            any(plan.actions for plan in calendar_plans) or self.store.data.get("calendar_history")
        ):
            return

        from .calendar import calendar_event_records
        from .plan_presentation import asset_name

        # Capture the event's state now: queued start/stop events can be
        # delivered after hass.states already exposes a later value.
        asset = (
            ActionAsset.EV
            if entity_id == entry_data.get(CONF_EV_CHARGING)
            else ActionAsset.DAIKIN
        )
        # A phase may become committed during a persistence await and
        # stop before execution returns. Its old state is independent
        # positive evidence; record it before closing on the new state.
        location = asset_name(asset)
        old, new = event.data.get("old_state"), event.data.get("new_state")
        equivalent_states = getattr(old, "state", None) == getattr(new, "state", None) and (
            hvac_feedback_is_assumed(old) == hvac_feedback_is_assumed(new)
            if asset == ActionAsset.DAIKIN
            else getattr(old, "last_changed", None) == getattr(new, "last_changed", None)
        )
        previous_records = None
        for state in ((new,) if equivalent_states else (old, new)):
            records: list[dict[str, str]] = []
            # A proposal can supply a missing window for actual feedback
            # while its save is pending. Prefer confirmed accepted windows.
            for calendar_plan in calendar_plans:
                observed = calendar_event_records(
                    self, calendar_plan, feedback_asset=asset,
                    state_overrides={entity_id: state},
                )
                records = [record for record in observed if record.get("location") == location]
                if records:
                    break
            if records == previous_records:
                continue
            previous_records = records
            history = self.store.data.get("calendar_history")
            unchanged = unchanged_calendar_feedback.get(asset) if equivalent_states else None
            if (unchanged is not None and unchanged.history is history and unchanged.records == records
                    and timedelta(0) <= now - unchanged.observed_at < unchanged.recheck_delay):
                continue
            if self.store.record_calendar_events(records, now=now, location=location):
                self._calendar_feedback_flush_needed = True
                unchanged_calendar_feedback.clear()
            elif equivalent_states:
                # No change means the saved history matched the normalized result.
                normalized_history = cast(dict[str, list[dict[str, str]]], history)
                unchanged_calendar_feedback[asset] = _UnchangedCalendarFeedback(
                    normalized_history, records, now,
                    calendar_feedback_recheck_delay(normalized_history, records, now),
                )
        if self._calendar_feedback_flush_needed and not self._calendar_feedback_flush_pending:
            self._calendar_feedback_flush_pending = True
            self._async_create_listener_task(self._async_flush_calendar_feedback())

    def async_start_listeners(self) -> None:
        """Start debounced state listeners for configured input entities."""
        resuming = getattr(self, "_tearing_down", False)
        self._tearing_down = False
        training = self.__dict__.get("history_training")
        if training is not None and training.closed:
            self.__dict__.pop("history_training")
        self._schedule_next_boundary_refresh()
        entry_data = self.entry_data
        self._start_load_forecast_source_listener(entry_data)
        from .daikin_memory import owned_control_data
        hvac_control = getattr(getattr(self, "store", None), "data", {}).get("ownership", {}).get("hvac_control", {})
        entity_ids, retained_zones = _state_listener_targets(entry_data, hvac_control)
        export_feedback_entity = export_limit_feedback_entity_id(
            self.hass, entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)
        )
        if export_feedback_entity and export_feedback_entity not in entity_ids:
            entity_ids.append(export_feedback_entity)
        external_ev_reservation = getattr(getattr(self, "store", None), "data", {}).get("ev_grid_reservation", {})
        external_ev = external_ev_reservation.get(EV_RESERVATION_EXTERNAL_BASELINE) is True
        remapped_ev = getattr(getattr(self, "executor", None), "remapped_owned_ev_feedback", None)
        owned_ev = remapped_ev(entry_data) if callable(remapped_ev) else (None, None)
        managed_ev = getattr(getattr(self, "executor", None), "managed_ev_feedback", None)
        signal_sources = getattr(getattr(self, "executor", None), "native_ev_signal_sources", None)
        empty_signals = (None, None, None)
        initial_signal_ids = signal_sources() if callable(signal_sources) else empty_signals
        native_signal_ids: tuple[str | None, str | None, str | None] = empty_signals
        record_ev_evidence = partial(self._schedule_ev_capacity_feedback, invalidate=False)
        persist_ev_evidence = partial(record_ev_evidence, refresh=False)
        if not entity_ids and not external_ev and not any(owned_ev) and not any(initial_signal_ids):
            startup_recovery._watch_failed_startup_restore(self)
            if resuming:
                self._resume_ev_capacity_feedback()
            return
        outages: _HvacOutages = self.__dict__.setdefault("_hvac_outages", _HvacOutages())
        unchanged_calendar_feedback: dict[ActionAsset, _UnchangedCalendarFeedback] = {}
        external_ev_stop_pending = False
        unsubscribe_ev_reports: list[Callable[[], None]] = []
        watched_ev_inputs: tuple[str | None, str | None, str | None, str | None, str | None] = (
            None, None, None, None, None,
        )
        watched_ev_reports: tuple[str, ...] = ()

        @callback
        def _unwatch_retained_ev_reports() -> None:
            nonlocal watched_ev_inputs, watched_ev_reports
            while unsubscribe_ev_reports:
                unsubscribe_ev_reports.pop()()
            watched_ev_inputs = (None, None, None, None, None)
            watched_ev_reports = ()

        @callback
        def _sync_retained_ev_watch() -> None:
            nonlocal watched_ev_inputs, watched_ev_reports, native_signal_ids
            previous_signal_ids = native_signal_ids
            native_signal_ids = signal_sources() if callable(signal_sources) else empty_signals
            external = self.store.data.get("ev_grid_reservation", {}).get(EV_RESERVATION_EXTERNAL_BASELINE) is True
            if external:
                inputs = self.executor.external_ev_watch_inputs()
                report_ids = tuple(entity for entity in inputs[:3] if entity)
            else:
                current = combined_entry_data(self.entry)
                power_id, limit_id = remapped_ev(current) if callable(remapped_ev) else (None, None)
                control, charging, connected = native_signal_ids
                inputs = (control if control not in entity_ids else None,
                          charging if charging not in entity_ids else None, power_id, limit_id,
                          connected if connected not in entity_ids else None)
                report_ids = tuple(dict.fromkeys(entity for entity in (*native_signal_ids, power_id) if entity))
            for signal_id in native_signal_ids:
                if signal_id and signal_id not in previous_signal_ids:
                    self._retain_native_ev_signal(signal_id, self.hass.states.get(signal_id), None)
            if not any(inputs) and not report_ids:
                _unwatch_retained_ev_reports()
                return
            if inputs != watched_ev_inputs or report_ids != watched_ev_reports:
                sync_capacity = external or inputs[2:4] != watched_ev_inputs[2:4]
                _unwatch_retained_ev_reports()
                watched_ev_inputs = inputs
                watched_ev_reports = report_ids
                state_changes = tuple(entity for entity in inputs if entity)
                # A newer sample can corroborate a stop even when its value is unchanged.
                if report_ids:
                    unsubscribe_ev_reports.append(async_track_state_report_event(
                        self.hass, report_ids, _handle_retained_ev_report,
                    ))
                if state_changes:
                    unsubscribe_ev_reports.append(
                        async_track_state_change_event(self.hass, state_changes, _handle_retained_ev_state_change),
                    )
                # The original charger may have reported while this entry was
                # unloaded. Attaching activity-only sources can also happen
                # during start preparation, before the new cap is applied.
                if sync_capacity and self.executor.sync_ev_grid_reservation(on_evidence_changed=record_ev_evidence):
                    self._schedule_ev_capacity_feedback()
            _check_external_ev_stop()

        @callback
        def _check_external_ev_stop() -> None:
            nonlocal external_ev_stop_pending
            if external_ev_stop_pending or not self.executor.external_ev_grid_stop_confirmed():
                return

            async def _release() -> None:
                nonlocal external_ev_stop_pending
                try:
                    async with self._command_lock:
                        # An intervening restart or acquisition can supersede the event.
                        if not self._tearing_down and self.executor.release_stopped_external_ev_grid_capacity():
                            await self._async_flush_ev_capacity_feedback()
                finally:
                    external_ev_stop_pending = False

            external_ev_stop_pending = True
            self._async_create_listener_task(_release())

        @callback
        def _handle_ev_power_report(event: Any) -> None:
            if self.executor.note_ev_grid_power_report(
                event.data["new_state"], on_evidence_changed=record_ev_evidence,
                received_at=event.data["last_reported"],
            ):
                self._schedule_ev_capacity_feedback()

        @callback
        def _handle_managed_ev_power_report(event: Any) -> None:
            current = combined_entry_data(self.entry)
            power_id, _limit_id = managed_ev(current) if callable(managed_ev) else (
                current.get("ev_power_entity"), current.get("ev_power_limit_entity"),
            )
            # Retained watches receive their own reports. A queued callback
            # for a released or replaced source no longer has authority.
            if event.data["entity_id"] == power_id and power_id != watched_ev_inputs[2]:
                _handle_ev_power_report(event)

        @callback
        def _handle_retained_ev_report(event: Any) -> None:
            entity_id = event.data["entity_id"]
            if entity_id == watched_ev_inputs[2]:
                _handle_ev_power_report(event)
            elif entity_id in native_signal_ids:
                if self.executor.note_native_ev_signal_heartbeat(event.data["new_state"], event.data["last_reported"]):
                    persist_ev_evidence()
                return
            else:
                state = event.data["new_state"]
                if self.executor.note_external_ev_signal_clock(entity_id, state, state, event.data["last_reported"]):
                    persist_ev_evidence()
            _check_external_ev_stop()

        @callback
        def _handle_retained_ev_state_change(event: Any) -> None:
            entity_id = event.data["entity_id"]
            new_state = event.data["new_state"]
            control_id, charging_id, power_id, limit_id, connected_id = watched_ev_inputs
            if entity_id in native_signal_ids and entity_id not in entity_ids:
                self._retain_native_ev_signal(entity_id, new_state, event.data["old_state"])
            current = combined_entry_data(self.entry)
            regular_power, regular_limit = managed_ev(current) if callable(managed_ev) else (
                current.get("ev_power_entity"), current.get("ev_power_limit_entity"),
            )
            handled_by_regular_watch = entity_id in entity_ids and entity_id in (regular_power, regular_limit)
            power_state = new_state if entity_id == power_id and not handled_by_regular_watch else None
            limit_state = new_state if entity_id == limit_id and not handled_by_regular_watch else None
            feedback = getattr(new_state, "state", None)
            external = self.store.data.get("ev_grid_reservation", {}).get(EV_RESERVATION_EXTERNAL_BASELINE) is True
            activity_changed = False
            if external and entity_id in (control_id, charging_id, connected_id):
                receipt = new_state.last_updated if new_state is not None else None
                activity_changed = self.executor.note_external_ev_signal_clock(
                    entity_id, new_state, event.data["old_state"], receipt,
                )
            cap_may_become_active = ev_cap_may_become_active(
                entity_id, getattr(event.data["old_state"], "state", None), feedback,
                control_ids=(control_id, connected_id if external else None), charging_id=charging_id,
            )
            # A cap first reported while idle can become usable without changing its setpoint.
            if (
                (power_state is not None or limit_state is not None or cap_may_become_active)
                and self.executor.sync_ev_grid_reservation(
                    power_state=power_state, limit_state=limit_state, on_evidence_changed=record_ev_evidence,
                )
            ):
                self._schedule_ev_capacity_feedback()
            elif activity_changed:
                persist_ev_evidence()
            _check_external_ev_stop()

        def _process_state_change(event: Any) -> None:
            self._wake_startup_auto_recovery()
            entity_id = str(event.data.get("entity_id") or "")
            if entity_id in retained_zones:
                zone_states = self.store.data.get("ownership", {}).get("hvac_control", {}).get("zone_states", {})
                if entity_id in zone_states:
                    outage = _track_hvac_outage(event, entity_id, outages, can_supersede=True)
                    if (outage.manual_stage is not None
                            or _is_first_refresh_hvac_edit(event, outage.excluded_context_ids)
                            or self._manual_edit_before_first_hvac_observation(event, entity_id, outage, outages)):
                        self._mark_explicit_hvac_change(entity_id, is_main=False)
                        self._queue_manual_hvac_change(event, entity_id, outages, is_main=False)
                else:
                    _refresh_input_subscription()
                return
            raw_data = combined_entry_data(self.entry)
            if (
                VEHICLES in raw_data
                and event.data.get("entity_id") == raw_data.get(CONF_EV_CONNECTED)
                and connection(getattr(event.data.get("new_state"), "state", None)) is False
                and (
                    connection(getattr(event.data.get("old_state"), "state", None)) is True
                    or (
                        connection(getattr(event.data.get("old_state"), "state", None)) is None
                        and self.vehicle_session.was_plugged
                    )
                )
            ):
                # Events can be queued across a rapid unplug/replug. Preserve
                # the observed boundary even if hass.states already says on.
                self._ev_unplug_generation = getattr(self, "_ev_unplug_generation", 0) + 1
                self._ev_last_unplug_at = reported_at(event.data.get("new_state")) or dt_util.utcnow()
                self.vehicle_session.note_unplug()
                self.vehicle_calibration.pending = None
                self._vehicle_ownership_key = None
            if VEHICLES in raw_data and connection(getattr(event.data.get("new_state"), "state", None)) is False:
                self.vehicle_session.blocked_vehicle_ids.difference_update(
                    p["id"] for p in raw_data[VEHICLES] if p.get("port_entity") == event.data.get("entity_id")
                )
            session_changed = self._update_vehicle_session()
            if VEHICLES in raw_data and (
                self.vehicle_session.snapshot() != self.store.data.get("ev_vehicle_session")
            ):
                # Port-disconnect evidence can change without changing the
                # resolved vehicle. Persist it before a reload can lose it.
                self._async_create_listener_task(self._async_save_vehicle_session())
            if session_changed:
                self.async_update_listeners()
            entry_data = owned_control_data(
                self.entry_data, getattr(getattr(self, "store", None), "data", {}).get(
                    "ownership", {}).get("hvac_control", {}),
            )
            now = dt_util.utcnow()
            self._record_calendar_feedback(event, entry_data, now, unchanged_calendar_feedback)
            executor = getattr(self, "executor", None)
            if entity_id in native_signal_ids:
                self._retain_native_ev_signal(entity_id, event.data.get("new_state"), event.data.get("old_state"))
            power_entity, limit_entity = managed_ev(entry_data) if callable(managed_ev) else (
                entry_data.get("ev_power_entity"), entry_data.get("ev_power_limit_entity"),
            )
            if entity_id in (power_entity, limit_entity) and executor is not None:
                expected_feedback = getattr(executor, "ev_start_limit_feedback", None)
                old, new = event.data.get("old_state"), event.data.get("new_state")
                if event.data.get("entity_id") == limit_entity and expected_feedback is not None:
                    if (old is not None and new is not None and old.attributes == new.attributes
                            and new.context.user_id is None
                            and new.context.parent_id in (None, expected_feedback[0])
                            and (reported_limit := finite(new.state)) is not None
                            and abs(reported_limit - expected_feedback[1]) < 1e-6):
                        # A provider poll can confirm the requested cap without inheriting the service context.
                        return
                    # A charger limit change invalidates the pending start even when
                    # generic replanning thresholds would ignore a small change.
                    self._mark_replan_requested()
                if executor.sync_ev_grid_reservation(
                    power_state=new if entity_id == power_entity else None,
                    limit_state=new if event.data.get("entity_id") == limit_entity else None,
                    on_evidence_changed=record_ev_evidence,
                ):
                    self._schedule_ev_capacity_feedback()
                    return
            charging_entity = entry_data.get(CONF_EV_CHARGING)
            if charging_entity and event.data.get("entity_id") == charging_entity:
                new_state = event.data.get("new_state")
                new_value = getattr(new_state, "state", None)
                if VEHICLES in raw_data:
                    self.vehicle_calibration.note_charging_change(
                        getattr(event.data.get("old_state"), "state", None), new_value,
                    )
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
            feedback_commands = getattr(executor, "hvac_feedback_commands", [])
            feedback_commands[:] = [
                command
                for command in feedback_commands
                if (deadline := _parse_datetime_or_none(command.get("deadline"))) is not None and now < deadline
            ]
            if entity_id in {entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), export_feedback_entity}:
                if entity_id != export_feedback_entity or not self._only_export_readback_advanced(
                    event.data.get("old_state"), event.data.get("new_state"), now,
                ):
                    self._schedule_debounced_refresh("export_limit_feedback", debounce_seconds=0, force=True)
                return
            is_main_climate = entity_id == entry_data.get(CONF_DAIKIN_CLIMATE)
            is_hvac = is_main_climate or entity_id in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
            startup = not getattr(self.hass, "is_running", True)
            if is_hvac:
                old, new = event.data.get("old_state"), event.data.get("new_state")
                if is_main_climate:
                    self.executor.hvac_mode_feedback.mode_feedback_at(entity_id, new, previous=old)
                can_supersede = bool(
                    is_main_climate or startup or pending_hvac_desired_state is not None
                    or has_hvac_ownership(self.store.data.get("ownership", {}))
                )
                outage = _track_hvac_outage(event, entity_id, outages, can_supersede=can_supersede)
                if outage.manual_stage is None and self._manual_edit_before_first_hvac_observation(
                    event, entity_id, outage, outages, is_main=is_main_climate,
                ):
                    outage = replace(outage, manual_stage="manual_recovery")
                if (hvac_feedback_is_observed(old) and hvac_feedback_is_observed(new)
                        and (attributed := _hvac_attributed_context_ids(event))
                        and attributed <= outage.excluded_context_ids):
                    # This is still observed telemetry. Suppress a repeated
                    # override and use the ordinary material refresh filter.
                    if _is_material_state_change(event, self.options):
                        self._schedule_debounced_refresh("state_change")
                    return
                if outage.manual_stage is None and hvac_feedback_is_assumed(old) and hvac_feedback_is_assumed(new):
                    # Consecutive optimistic polls do not change availability
                    # or provide new physical evidence for planning.
                    return
                if outage.manual_stage is not None or _is_hvac_availability_feedback(
                    event, inherited_context_ids=outage.excluded_context_ids,
                ):
                    self._record_hvac_outage_feedback(event, entity_id, outage, now)
                    if outage.manual_stage is None:
                        self._schedule_debounced_refresh("hvac_availability", debounce_seconds=0, force=True)
                        return
                if outage.manual_stage is not None or (startup and _is_explicit_startup_hvac_change(event)):
                    self._mark_explicit_hvac_change(entity_id, is_main=is_main_climate)
                    self._queue_manual_hvac_change(event, entity_id, outages, is_main=is_main_climate)
                    return
            if startup and is_hvac:
                # Startup restoration alone is not evidence of manual intent.
                if not _is_planner_owned_control_feedback(
                    entry_data,
                    self.store.data,
                    event,
                    now,
                    pending_hvac_desired_state=pending_hvac_desired_state,
                ) and _is_material_state_change(event, self.options):
                    self._schedule_debounced_refresh("state_change")
                return
            if _is_planner_owned_control_feedback(
                entry_data,
                self.store.data,
                event,
                now,
                pending_hvac_desired_state=pending_hvac_desired_state,
                feedback_commands=feedback_commands if hasattr(executor, "hvac_feedback_commands") else None,
                hass=self.hass,
            ):
                return
            if (is_main_climate or entity_id in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))) and (
                getattr(event.data.get("old_state"), "state", None)
                != getattr(event.data.get("new_state"), "state", None)
                or any(
                    (getattr(event.data.get("old_state"), "attributes", {}) or {}).get(key)
                    != (getattr(event.data.get("new_state"), "attributes", {}) or {}).get(key)
                    for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
                )
            ):
                audit = getattr(self.store, "record_climate_event", None)
                if callable(audit):
                    context = getattr(event, "context", None)
                    old, new = event.data.get("old_state"), event.data.get("new_state")
                    user = bool(
                        getattr(context, "user_id", None) or getattr(getattr(new, "context", None), "user_id", None)
                    )
                    audit(
                        {
                            "stage": "unmatched_feedback",
                            "transaction_id": (pending_hvac_desired_state or {}).get("transaction_id"),
                            "lifecycle_id": self.store.data.get("ownership", {})
                            .get("hvac_control", {})
                            .get("lifecycle_id")
                            if isinstance(self.store.data.get("ownership", {}).get("hvac_control"), dict)
                            else None,
                            "supporting_command_context": feedback_commands[-1].get("context_id")
                            if feedback_commands
                            else None,
                            "entity_id": entity_id,
                            "override_source": "user"
                            if user
                            else "context_linked_external"
                            if getattr(context, "parent_id", None)
                            else "unattributed",
                            "user_attributed": user,
                            "context_id": getattr(context, "id", None),
                            "parent_id": getattr(context, "parent_id", None),
                            "expected": {
                                "mode": getattr(old, "state", None),
                                **{
                                    key: (getattr(old, "attributes", {}) or {}).get(key)
                                    for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
                                },
                            },
                            "observed": {
                                "mode": getattr(new, "state", None),
                                **{
                                    key: (getattr(new, "attributes", {}) or {}).get(key)
                                    for key in _HVAC_CONTROL_ATTRIBUTE_KEYS
                                },
                            },
                        },
                        now=now,
                    )
            if _event_reports_ev_charging_started(entry_data, event):
                if self._ev_command_guard()() and self.active_control and strict_bool(
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
                self._queue_manual_hvac_change(event, entity_id, outages, is_main=True)
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
                self._queue_manual_hvac_change(event, pending_zone_entity_id, outages, is_main=False)
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
                feedback_commands.clear()
                self._queue_manual_hvac_change(event, entity_id, outages, is_main=True)
                return
            if _is_manual_hvac_zone_change(self.hass, entry_data, self.store.data, event):
                feedback_commands.clear()
                self._queue_manual_hvac_change(event, entity_id, outages, is_main=False)
                return
            tariff = entity_id in {entry_data.get(CONF_AMBER_IMPORT_PRICE), entry_data.get(CONF_AMBER_EXPORT_PRICE)}
            if not _is_material_state_change(event, self.options, tariff=tariff):
                return
            self._schedule_debounced_refresh("state_change")

        @callback
        def _handle_state_change(event: Any) -> None:
            # A terminal restore job can run eagerly. Record manual
            # supersession before the same report can make a restore runnable.
            _process_state_change(event)
            startup_recovery._observe_failed_startup_availability(self, event)

        unsubscribe_states = async_track_state_change_event(self.hass, entity_ids, _handle_state_change)

        @callback
        def _unsubscribe_states() -> None:
            self.__dict__.pop("_refresh_input_subscription", None)
            unsubscribe_states()

        self._unsub_listeners.append(_unsubscribe_states)
        if (ev_power_entity := entry_data.get("ev_power_entity")) and entry_data.get("ev_power_limit_entity"):
            # A command can be confirmed by a new sample with an unchanged value.
            self._unsub_listeners.append(async_track_state_report_event(
                self.hass, (ev_power_entity,), _handle_managed_ev_power_report,
            ))
        if (external_ev or any(owned_ev) or any(initial_signal_ids)
                or any(entry_data.get(key) for key in (CONF_EV_CHARGER, CONF_EV_SMART_CHARGING,
                                                        CONF_EV_CHARGING, "ev_power_entity"))):
            self.executor.ev_grid_reservation_persisted = _sync_retained_ev_watch
            self.executor.ev_grid_draw_observed = persist_ev_evidence
            if add_ownership_listener := getattr(self.store, "async_add_ownership_listener", None):
                self._unsub_listeners.append(add_ownership_listener(_sync_retained_ev_watch))

            @callback
            def _unsubscribe_retained_ev() -> None:
                self.executor.ev_grid_reservation_persisted = None
                self.executor.ev_grid_draw_observed = None
                _unwatch_retained_ev_reports()

            self._unsub_listeners.append(_unsubscribe_retained_ev)
            _sync_retained_ev_watch()

        @callback
        def _refresh_input_subscription() -> None:
            nonlocal entity_ids, retained_zones, unsubscribe_states
            entry_data = self.entry_data
            control = self.store.data.get("ownership", {}).get("hvac_control", {})
            watched, retained_zones = _state_listener_targets(entry_data, control)
            sensor = export_limit_feedback_entity_id(self.hass, entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY))
            if sensor and sensor not in watched:
                watched.append(sensor)
            if watched == entity_ids:
                return
            entity_ids = watched
            unsubscribe_states()
            unsubscribe_states = async_track_state_change_event(self.hass, watched, _handle_state_change)

        self._refresh_input_subscription = _refresh_input_subscription

        export_entity = entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)
        if export_entity and export_entity.startswith("select."):
            @callback
            def _handle_export_registry_change(_event: Any) -> None:
                nonlocal export_feedback_entity
                if self._tearing_down:
                    return
                sensor = export_limit_feedback_entity_id(self.hass, export_entity)
                if sensor == export_feedback_entity:
                    return
                export_feedback_entity = sensor
                _refresh_input_subscription()
                self._schedule_debounced_refresh("export_limit_feedback", debounce_seconds=0, force=True)

            self._unsub_listeners.append(self.hass.bus.async_listen(
                EVENT_ENTITY_REGISTRY_UPDATED, _handle_export_registry_change,
            ))
        charging_entity = entry_data.get(CONF_EV_CHARGING)
        charging_state = self.hass.states.get(charging_entity) if charging_entity else None
        if (
            charging_state is not None
            and ev_charging_state(charging_state.state) is True
            and self.active_control
            and self._ev_command_guard()()
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
        if resuming:
            # A retained charger can report while listeners are detached for unload.
            if (not external_ev and (entry_data.get("ev_power_entity") or entry_data.get("ev_power_limit_entity"))
                    and self.executor.sync_ev_grid_reservation(on_evidence_changed=record_ev_evidence)):
                self._schedule_ev_capacity_feedback()
            self._resume_ev_capacity_feedback()
        if self.__dict__.pop("_refresh_after_initial_hvac_handoff", False):
            self._async_create_listener_task(self.async_request_refresh())
        startup_recovery._watch_failed_startup_restore(self)

    async_start_startup_auto_recovery = startup_recovery.async_start_startup_auto_recovery

    _wake_startup_auto_recovery = startup_recovery._wake_startup_auto_recovery
    _async_fail_startup_recovery = startup_recovery._async_fail_startup_recovery
    _async_restore_failed_startup = startup_recovery._async_restore_failed_startup
    _queue_failed_startup_restore = startup_recovery._queue_failed_startup_restore

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

    @callback
    def _schedule_ev_capacity_feedback(
        self, *, invalidate: bool = True, refresh: bool = True, retain_during_shutdown: bool = False,
    ) -> None:
        """Invalidate pending plans and coalesce durable reservation feedback."""
        if self._tearing_down and not retain_during_shutdown:
            return
        if invalidate and not self._tearing_down:
            self._mark_replan_requested(force=True)
        # A failed or cancelled unload can reattach listeners while a save drains.
        work = getattr(self, "_ev_capacity_work", None)
        if work is None:
            work = self._ev_capacity_work = _EVCapacityWork()
        work.save_needed = True
        work.refresh_after_save |= refresh
        if self._tearing_down:
            work.invalidate_on_resume |= invalidate
            self._ev_capacity_flush_needed = True
        elif not work.save_pending:
            work.save_pending = True
            self._async_create_listener_task(self._async_persist_ev_capacity_feedback(work))

    async def _async_persist_ev_capacity_feedback(self, work: _EVCapacityWork) -> None:
        try:
            while work.save_needed:
                async with self._command_lock:
                    # A newer reading during the save leaves another pass pending.
                    work.save_needed = False
                    refresh = work.refresh_after_save
                    work.refresh_after_save = False
                    try:
                        await self._async_flush_ev_capacity_feedback()
                    except Exception as error:
                        _LOGGER.warning(
                            "EV capacity persistence failed (%s)", type(error).__name__,
                        )
                if refresh:
                    self._queue_ev_capacity_refresh(work)
        finally:
            work.save_pending = False

    def _queue_ev_capacity_refresh(self, work: _EVCapacityWork) -> None:
        # Keep refresh intent through a failed unload, independently of Store writes.
        work.refresh_needed = True
        if not self._tearing_down and not work.refresh_pending:
            work.refresh_pending = True
            self._async_create_listener_task(self._async_refresh_ev_capacity(work))

    def _resume_ev_capacity_feedback(self) -> None:
        if (deadline := getattr(self, "_ev_allocation_deadline", None)) is not None:
            self._schedule_ev_allocation_deadline(deadline)
        work = getattr(self, "_ev_capacity_work", None)
        if work is not None and work.invalidate_on_resume:
            work.invalidate_on_resume = False
            self._mark_replan_requested(force=True)
            work.save_needed = True
        if work is not None and work.save_needed:
            self._schedule_ev_capacity_feedback(invalidate=False, refresh=False)
        if work is not None and work.refresh_needed and not work.save_pending:
            self._queue_ev_capacity_refresh(work)

    def _acknowledge_ev_capacity_refresh(self, request: object | None) -> None:
        work = getattr(self, "_ev_capacity_work", None)
        if work is not None and work.refresh_request is request:
            work.refresh_request = None

    def _resume_ev_capacity_after_validation(self) -> None:
        work = getattr(self, "_ev_capacity_work", None)
        if work is not None and work.refresh_request is not None:
            self._queue_ev_capacity_refresh(work)

    async def _async_refresh_ev_capacity(self, work: _EVCapacityWork) -> None:
        try:
            while work.refresh_needed and not self._tearing_down:
                work.refresh_needed = False
                # Core can accept a debounced request without running it yet.
                work.refresh_request = object()
                self._attribute_forced_refresh("ev_capacity_feedback")
                try:
                    await self.async_request_refresh()
                except Exception as error:
                    _LOGGER.warning("EV capacity refresh request failed (%s)", type(error).__name__)
        finally:
            work.refresh_pending = False

    async def _async_flush_ev_capacity_feedback(self) -> None:
        """Persist the current EV reservation while the caller holds the command lock."""
        self._ev_capacity_flush_needed = True
        work = getattr(self, "_ev_capacity_work", None)
        if self._tearing_down and work is not None:
            work.save_needed = False
        await self.executor.async_persist_ev_grid_reservation()
        # A concurrent planner cycle can defer ordinary Store writes.
        await self.store.async_flush()
        work = getattr(self, "_ev_capacity_work", None)
        self._ev_capacity_flush_needed = self._tearing_down and work is not None and work.save_needed

    async def _async_flush_calendar_feedback(self) -> None:
        """Retry unsaved feedback on the next observation or while draining for unload."""
        try:
            await self.store.async_flush()
        except Exception as error:  # noqa: BLE001 - retain observed feedback for the next flush.
            _LOGGER.warning("Could not persist observed calendar activity (%s)", type(error).__name__)
        else:
            self._calendar_feedback_flush_needed = False
        finally:
            self._calendar_feedback_flush_pending = False

    _begin_shutdown = task_lifecycle._begin_shutdown

    async def async_shutdown(self) -> None:
        """Stop integration work and release coordinator resources."""
        self._begin_shutdown()
        executor = getattr(self, "executor", None)
        if executor is not None:
            executor.hvac_feedback_commands = []
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
        mode: _RefreshMode = _RefreshMode.DEBOUNCED,
    ) -> None:
        """Coalesce refreshes without delaying higher-priority deadlines."""
        self._mark_replan_requested(force=force)
        if mode is _RefreshMode.IMMEDIATE and getattr(self, "_queued_direct_refresh", None) is not None:
            self._increment_refresh_counter("coalesced")
            return
        delay = (0.0 if mode is not _RefreshMode.DEBOUNCED
                 else max(float(debounce_seconds), self._non_manual_refresh_delay()))
        deadline = monotonic() + delay
        priority = mode is not _RefreshMode.DEBOUNCED or debounce_seconds == 0
        previous_request = getattr(self, "_debounce_requested_at", None)
        requested_at = (previous_request if self._debounce_cancel is not None and previous_request is not None
                        else perf_counter())
        self._debounce_requested_at = requested_at
        if self._debounce_cancel is not None:
            self._increment_refresh_counter("coalesced")
            previous = getattr(self, "_debounce_request", None)
            if previous is not None:
                if previous.priority and (not priority or previous.deadline <= deadline) and (
                    previous.mode is _RefreshMode.IMMEDIATE or mode is not _RefreshMode.IMMEDIATE
                ):
                    return
                if deadline >= previous.deadline:
                    self._debounce_request = _DebounceRequest(deadline, trigger, mode, priority)
                    return  # The existing timer will observe the new trailing deadline.
            self._debounce_cancel()

        self._debounce_request = _DebounceRequest(deadline, trigger, mode, priority)
        armed_deadline = deadline

        @callback
        def _refresh(now: Any) -> None:
            nonlocal armed_deadline
            if getattr(self, "_tearing_down", False):
                return
            request = self._debounce_request
            assert request is not None
            if request.deadline > armed_deadline and (remaining := request.deadline - monotonic()) > 0:
                armed_deadline = request.deadline
                self._debounce_cancel = async_call_later(self.hass, remaining, _refresh)
                return
            self._debounce_cancel = None
            self._debounce_requested_at = None
            self._debounce_request = None
            self._pending_refresh_trigger = request.trigger
            self._last_non_manual_refresh_requested_at = monotonic()
            self._increment_refresh_counter("requested")

            direct = (
                _QueuedDirectRefresh(request.trigger, requested_at)
                if request.mode is _RefreshMode.IMMEDIATE else None
            )
            if direct is not None:
                self._queued_direct_refresh = direct

            async def request_refresh() -> None:
                if direct is None:
                    self._refresh_request_timing = (request.trigger, requested_at, request_owner())
                    await self.async_request_refresh()
                    return
                # Keep the request queued until Core admits this task to planning.
                direct.owner = asyncio.current_task()
                try:
                    await self.async_refresh()
                finally:
                    if self._queued_direct_refresh is direct:
                        self._queued_direct_refresh = None

            self._async_create_listener_task(request_refresh())

        self._debounce_cancel = async_call_later(self.hass, delay, _refresh)

    @callback
    def _schedule_ev_allocation_deadline(self, deadline: Any) -> None:
        if self._ev_allocation_cancel is not None:
            self._ev_allocation_cancel()
        self._ev_allocation_deadline = deadline
        self._ev_allocation_cancel = None
        if deadline is None:
            return

        async def expire() -> None:
            async with self._command_lock:
                if self._tearing_down or self._ev_allocation_deadline != deadline:
                    return
                await self.executor.async_restore_device_control("ev", "ev_allocation_expired")
            self._mark_forced_refresh("ev_allocation_expired")
            await self.async_request_refresh()

        @callback
        def due(_now: Any) -> None:
            self._ev_allocation_cancel = None
            self._async_create_listener_task(expire())

        self._ev_allocation_cancel = async_call_later(
            self.hass, max((deadline-dt_util.utcnow()).total_seconds(), 0), due)

    @callback
    def _schedule_export_boundary(self, evidence: dict[str, Any]) -> None:
        blocks = evidence.get("blocks", [])
        deadlines = [block_time(blocks[0]["end"])] if blocks else []
        eligible = evidence.get("next_eligible_change_at")
        if evidence.get("deferred") and eligible:
            # Input collection can cross eligibility after capturing deferred
            # evidence. Keep the deadline: the clamped delay below queues one
            # immediate refresh, whose fresh evidence ends this deferral.
            deadlines.append(block_time(eligible))
        deadline = min(deadlines, default=None)
        delay = max((deadline - dt_util.utcnow()).total_seconds(), 0) if deadline is not None else 0
        fire_at = monotonic() + delay
        if self._export_boundary_cancel is not None:
            previous_fire_at = getattr(self, "_export_boundary_fire_at", None)
            # Keep the timer for unchanged evidence. A wall-clock correction
            # changes the monotonic fire time even when the UTC deadline is the same.
            if (deadline is not None and previous_fire_at is not None and deadline == self._export_boundary_at
                    and abs(fire_at - previous_fire_at) <= 0.001):
                return
            self._export_boundary_cancel()
            self._export_boundary_cancel = None
        self._export_boundary_at = None
        self._export_boundary_fire_at = None
        if deadline is None:
            return

        @callback
        def due(_now: Any) -> None:
            if getattr(self, "_tearing_down", False) or self._export_boundary_cancel is not scheduled_cancel:
                return
            self._dispatch_boundary(deadline, export_timer=True)

        scheduled_cancel = async_call_later(self.hass, delay, due)
        self._export_boundary_cancel = scheduled_cancel
        self._export_boundary_at = deadline
        self._export_boundary_fire_at = fire_at

    @callback
    def _dispatch_boundary(self, deadline: datetime, *, export_timer: bool) -> None:
        """Handle coincident wall-clock deadlines before dispatching either refresh."""
        planning_due = getattr(self, "_planning_boundary_at", None) == deadline
        export_due = getattr(self, "_export_boundary_at", None) == deadline
        if planning_due:
            if export_timer:
                assert self._boundary_cancel is not None
                self._boundary_cancel()
            self._boundary_cancel = None
            self._planning_boundary_at = None
        if export_due:
            if not export_timer:
                assert self._export_boundary_cancel is not None
                self._export_boundary_cancel()
            self._export_boundary_cancel = None
            self._export_boundary_at = None
            self._export_boundary_fire_at = None
            # Revalidate settled settings at refresh admission. Only a
            # transient result must be discarded: it may describe a restore
            # that was deferred before this deadline.
            self._export_reconciled_for = None
            self._export_boundary_followup_due = True
            self._schedule_debounced_refresh("export_tariff_boundary", mode=_RefreshMode.IMMEDIATE, force=True)
        else:
            # Only the minimum-refresh floor may delay the planning boundary.
            self._schedule_debounced_refresh("interval_boundary", debounce_seconds=0)
        if planning_due:
            # Publish clock-dependent health while refreshing is still queued.
            self.async_update_listeners()
            self._schedule_next_boundary_refresh(after=deadline)

    @callback
    def _schedule_tariff_validity_retry(
        self, details: list[dict[str, Any]], *, observed_at: datetime | None = None,
    ) -> None:
        """Request once at a known brief tariff gap's end, with no polling loop."""
        now = dt_util.utcnow()
        observed_at = observed_at or now
        deadlines = [_parse_datetime_or_none(item.get("next_retry_at")) for item in details]
        # One retry must cover both import and export. Validate imminence at
        # collection time: context building can outlive an advertised boundary.
        deadline = max((at for at in deadlines if at is not None and at.utcoffset() is not None
                        and abs((at - observed_at).total_seconds()) <= 2), default=None)
        cancel = getattr(self, "_tariff_retry_cancel", None)
        if deadline is None or getattr(self, "_tearing_down", False):
            if cancel is not None:
                cancel()
                self._tariff_retry_cancel = None
                self._tariff_retry_at = None
            self._tariff_retry_window_end = None
            return
        previous = getattr(self, "_tariff_retry_at", None)
        if previous is not None and deadline <= previous:
            return
        if cancel is not None:
            # Merge later validity evidence into the existing pending timer,
            # without allowing a moving source to postpone it indefinitely.
            assert self._tariff_retry_window_end is not None
            if deadline > self._tariff_retry_window_end:
                return
            cancel()
        else:
            last_requested = getattr(self, "_tariff_retry_last_requested_at", None)
            if last_requested is not None and monotonic() - last_requested < 30:
                return
            self._tariff_retry_window_end = now + timedelta(seconds=2)
        self._tariff_retry_at = deadline

        @callback
        def due(_now: Any) -> None:
            if self._tearing_down or self._tariff_retry_cancel is not scheduled_cancel:
                return
            self._tariff_retry_cancel = None
            self._tariff_retry_window_end = None
            self._tariff_retry_last_requested_at = monotonic()
            self._mark_forced_refresh("tariff_validity_boundary")
            # Core's request cooldown may still be active from the gap's first
            # refresh. Refresh at validity, retaining Core's serialization lock.
            self._async_create_listener_task(self.async_refresh())

        scheduled_cancel = async_call_later(
            self.hass, max((deadline - now).total_seconds(), 0), due,
        )
        self._tariff_retry_cancel = scheduled_cancel

    @callback
    def _schedule_next_boundary_refresh(self, *, after: datetime | None = None) -> None:
        """Schedule the next planning-interval boundary refresh."""
        if self._boundary_cancel is not None:
            self._boundary_cancel()
        now = dt_util.utcnow()
        # A timer can fire at the preceding wall-clock tick. Skip that tick
        # after delivery while still basing a clock correction on current UTC.
        reference = after if after is not None and now < after <= now + datetime.resolution else now
        deadline = reference + timedelta(seconds=_seconds_until_next_interval_boundary(
            reference,
            int(self.options.get(CONF_PLANNING_INTERVAL_MINUTES, 5)),
        ))

        weather = getattr(self, "weather_forecast_diagnostics", {})
        expiry = _parse_datetime_or_none(weather.get("grace_expires_at"))
        if weather.get("ownership_retained") and expiry is not None and expiry > reference:
            deadline = min(deadline, expiry)

        @callback
        def due(_now: Any) -> None:
            if self._boundary_cancel is cancel:
                self._dispatch_boundary(deadline, export_timer=False)

        cancel = async_call_later(self.hass, (deadline - now).total_seconds(), due)
        self._boundary_cancel = cancel
        self._planning_boundary_at = deadline

    async def _async_update_data(self) -> EnergyPlan | None:
        """Refresh planner data."""
        timing = RefreshTiming()
        token = CURRENT_REFRESH.set(timing)
        succeeded = False
        trigger = getattr(self, "_pending_refresh_trigger", "unknown")
        request_timing = getattr(self, "_refresh_request_timing", None)
        self._refresh_request_timing = None
        direct = getattr(self, "_queued_direct_refresh", None)
        if direct is not None and direct.owner is timing.owner:
            trigger = direct.trigger
            request_timing = (trigger, direct.requested_at, direct.owner)
            self._queued_direct_refresh = None
        request_age = (max((timing.started - request_timing[1]) * 1000, 0)
                       if request_timing is not None and request_timing[0] == trigger
                       and request_timing[2] is asyncio.current_task() else None)
        trigger_counts = getattr(self, "_refresh_trigger_counts", {})
        trigger_counts[trigger] = int(trigger_counts.get(trigger, 0)) + 1
        self._refresh_trigger_counts = trigger_counts
        try:
            timing.stage("export_admission_ms")
            has_entry = getattr(self, "entry", None) is not None
            wait_for_export = has_entry and (
                getattr(self, "_startup_auto_recovery_validation_active", False)
                or _EXPORT_PREFLIGHT_REFRESH.get() == (self, asyncio.current_task())
            )
            if wait_for_export:
                # An earlier refresh can consume the shared force flag while
                # this direct refresh waits for the coordinator lock.
                self._force_next_refresh = True
            elif has_entry:
                self._admit_export_reconciliation()
            retried_export_preflight = False
            deferred_execution: _PlanExecution | None = None
            ev_request: object | None = None
            while True:
                prepared_weather = None
                preflight_export = None
                if (
                    not getattr(self, "_tearing_down", False)
                    and getattr(self, "entry", None) is not None
                    and self.entry_data.get(CONF_WEATHER)
                ):
                    timing.stage("weather_ms")
                    prepared_weather = await self._async_prepare_weather_forecast()
                if wait_for_export:
                    timing.stage("export_preflight_wait_ms")
                    await self._async_prepare_export_reconciliation_for_preflight()
                    preflight_export = self._export_reconciled_for
                timing.stage("planner_lock_wait_ms")
                async with self._planner_lock:
                    timing.stage("inputs_ms")
                    if getattr(self, "_tearing_down", False):
                        succeeded = True
                        self._increment_refresh_counter("teardown_skipped")
                        return self.data
                    if prepared_weather is not None and (
                        prepared_weather[0] != _weather_input_identity(
                            self.entry_data, self.planner_options,
                            str(getattr(getattr(self.hass, "config", None), "time_zone", "UTC")))
                        or (getattr(self, "_force_next_refresh", False) and not prepared_weather[1])
                    ):
                        continue
                    if prepared_weather is None and getattr(self, "entry", None) is not None and self.entry_data.get(
                        CONF_WEATHER
                    ):
                        continue
                    if (preflight_export is not None or getattr(self, "_export_reconciled_for", None) is not None
                            or getattr(self, "_settled_export_reconciliation", None) is not None):
                        # Planner-lock waits may outlive even the evidence just
                        # checked by preflight. Give that refresh one retry.
                        retry_preflight = wait_for_export and not retried_export_preflight
                        task = self._admit_export_reconciliation(schedule_followup=not retry_preflight)
                        self._export_reconciled_for = None
                        if task is None:
                            # Admission rechecked the evidence under the lock.
                            # The boundary needs no reconciliation follow-up.
                            self._export_boundary_followup_due = False
                        if retry_preflight and task is not None:
                            retried_export_preflight = True
                            continue
                    try:
                        ev_work = getattr(self, "_ev_capacity_work", None)
                        ev_request = ev_work.refresh_request if ev_work is not None else None
                        pending_arm = getattr(self, "_export_operator_arm", None)
                        if pending_arm is not None and pending_arm.rollback_pending:
                            await self._async_finish_operator_arm_rollback(pending_arm)
                        self._pending_plan_commit = None
                        plan_admitted = False
                        try:
                            with self.store.defer_save():
                                self._deferred_plan_execution = None
                                self._prepared_weather_for_refresh = prepared_weather
                                try:
                                    result = await self._async_update_data_locked(
                                        defer_execution=True, defer_publication=True,
                                    )
                                finally:
                                    del self._prepared_weather_for_refresh
                                    timing.stage("storage_flush_ms")
                            generation_before_flush = self._refresh_generation
                            if not timing.superseded and (commit_plan := self._pending_plan_commit) is not None:
                                result = await commit_plan()
                                plan_admitted = not timing.superseded
                        finally:
                            if not plan_admitted:
                                await self.store.async_flush_deferred()
                        if plan_admitted:
                            # The candidate write already included the operational
                            # batch. Promote the accepted fields off the refresh path.
                            self._async_create_listener_task(self.store.async_flush_deferred())
                        if (timing.superseded or generation_before_flush != self._refresh_generation
                                or getattr(self, "_tearing_down", False)):
                            timing.superseded = True
                        # Capture and clear under the planner lock: another refresh
                        # must never consume this refresh's execution request.
                        deferred_execution = self._deferred_plan_execution
                        self._deferred_plan_execution = None
                    except BaseException:
                        self._deferred_plan_execution = None
                        raise
                    finally:
                        self._pending_plan_commit = None
                break
            timing.stage("post_refresh_ms")
            if getattr(self, "_tearing_down", False):
                return self.data
            if timing.superseded:
                # The fingerprint may describe the discarded plan if the
                # generation changed during the final Store flush.
                self._force_next_refresh = True
                if self.data is None:
                    # Core treats a normal return of None as successful setup.
                    raise InitialPlanSuperseded("Planner inputs changed before the first plan was ready")
                self._schedule_debounced_refresh("refresh_superseded", mode=_RefreshMode.IMMEDIATE, force=True)
                return self.data
            if deferred_execution is not None:
                if ev_request is not None:
                    deferred_execution = replace(deferred_execution, ev_capacity_request=ev_request)
                self._schedule_plan_execution(deferred_execution)
            elif not getattr(self, "_startup_auto_recovery_validation_active", False):
                self._acknowledge_ev_capacity_refresh(ev_request)
            succeeded = True
            return result
        finally:
            self._increment_refresh_counter("succeeded" if succeeded else "failed")
            self._increment_refresh_counter("completed")
            completed_times = getattr(self, "_refresh_completed_times", [])
            completed_times.append(monotonic())
            self._refresh_completed_times = completed_times[-256:]
            snapshot = timing.finish()
            self._last_phase_durations = dict(snapshot["phases"])
            self.last_refresh_metadata = {
                **snapshot,
                "request_age_ms": round(request_age, 3) if request_age is not None else None,
                "succeeded": succeeded,
                "completed_at": dt_util.utcnow(),
                "generation": getattr(self, "_refresh_generation", 0),
                "trigger": trigger,
                "counters": dict(getattr(self, "_refresh_counters", {})),
            }
            CURRENT_REFRESH.reset(token)

    async def _async_prepare_weather_forecast(
        self,
    ) -> tuple[tuple[Any, ...], bool, dict[str, Any], dict[str, Any]]:
        """Fetch advisory weather before contending for the planner lock."""
        entry_data = self.entry_data
        options = self.planner_options
        now = dt_util.utcnow()
        force = bool(getattr(self, "_force_next_refresh", False))
        identity = _weather_input_identity(
            entry_data, options, str(getattr(getattr(self.hass, "config", None), "time_zone", "UTC")))
        forecast, details = await self._async_weather_forecast(
            entry_data, options, now=now, force=force,
        )
        return identity, force, forecast, details

    async def _async_weather_for_refresh(
        self, entry_data: dict[str, Any], options: dict[str, Any], *, now: datetime, force: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Consume prefetched weather, with a direct-call fallback for tests and recovery."""
        prepared = getattr(self, "_prepared_weather_for_refresh", None)
        if prepared is not None:
            identity, prepared_force, forecast, details = cast(
                tuple[tuple[Any, ...], bool, dict[str, Any], dict[str, Any]], prepared,
            )
            if identity == _weather_input_identity(
                entry_data, options, str(getattr(getattr(self.hass, "config", None), "time_zone", "UTC"))
            ) and (prepared_force or not force):
                return forecast, details
        return await self._async_weather_forecast(entry_data, options, now=now, force=force)

    async def _async_refresh_with_export_preflight(self, trigger: str) -> None:
        """Require settled export evidence in this direct coordinator refresh."""
        token = _EXPORT_PREFLIGHT_REFRESH.set((self, asyncio.current_task()))
        try:
            self._mark_forced_refresh(trigger)
            await self.async_refresh()
        finally:
            _EXPORT_PREFLIGHT_REFRESH.reset(token)

    async def _async_prepare_export_reconciliation_for_preflight(self) -> None:
        """Settle export evidence before validating a control transition."""
        if (task := getattr(self, "_export_reconcile_task", None)) is not None:
            # A job admitted before validation may have seen a different
            # startup policy. Let it finish and check its evidence again.
            await asyncio.shield(task)
        if (task := self._admit_export_reconciliation(schedule_followup=False)) is not None:
            await asyncio.shield(task)

    def _admit_export_reconciliation(self, *, schedule_followup: bool = True) -> asyncio.Task[None] | None:
        """Keep export provider and persistence waits out of input acquisition."""
        if getattr(self, "_tearing_down", False):
            return None
        task: asyncio.Task[None] | None = getattr(self, "_export_reconcile_task", None)
        if task is not None and not task.done():
            return task
        # A different refresh trigger or planner generation can replace the
        # completion callback without superseding the actual export evidence.
        reconciled_for = getattr(self, "_export_reconciled_for", None)
        settled = getattr(self, "_settled_export_reconciliation", None)
        if (reconciled_for is not None or settled is not None) and not has_future_export_attempt(
            budget_history(self.store.data), dt_util.utcnow(),
        ):
            current = self._export_reconciliation_inputs()
            if settled is not None:
                if current is not None and current.reusable and self._export_reconciliation_matches(settled, current):
                    self._settled_export_reconciliation = current
                    return None
            elif reconciled_for is not None and self._export_reconciliation_matches(reconciled_for, current):
                return None
        self._settled_export_reconciliation = None
        self._export_reconciled_for = None
        was_pending = getattr(self, "_export_reconcile_pending", False)
        ownership = self.store.data.get("ownership", {}).get(EXPORT_ASSET)
        if not self.entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY) or (
            self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is not True and not ownership
        ):
            self._export_boundary_followup_due = False
            if was_pending:
                self._mark_replan_requested(force=True)
                self._export_reconcile_pending = False
            return None
        # Close readiness before the job can yield. Even an idle command lock
        # does not make the transaction cheap: it can flush and call services.
        self._export_reconcile_pending = True
        # One entry-owned job queues fairly behind the current transaction.
        # It re-reads configuration/feedback only after acquiring command authority.
        task = self.entry.async_create_background_task(
            self.hass, self._async_deferred_export_reconciliation(schedule_followup=schedule_followup),
            f"{DOMAIN} export reconciliation",
            eager_start=False,
        )
        self._export_reconcile_task = task
        return task

    async def _async_deferred_export_reconciliation(self, *, schedule_followup: bool = True) -> None:
        """Reconcile once, then force fresh planning before allowing execution."""
        timing = RefreshTiming()
        succeeded = False
        try:
            observed_inputs = await self._async_reconcile_export_limit(timing)
            # Never hold command authority while waiting for the planner. Let
            # a refresh already building with the closed gate commit before
            # invalidating it, so it cannot request another reconciliation in
            # the middle of publishing this completion.
            timing.stage("planner_lock_wait_ms")
            async with self._planner_lock:
                if not getattr(self, "_tearing_down", False):
                    current = self._export_reconciliation_inputs()
                    settled = self._export_reconciliation_matches(observed_inputs, current)
                    # The waking refresh may have found this job in flight.
                    # Retry changed feedback or an elapsed cooldown boundary.
                    boundary_due = getattr(self, "_export_boundary_followup_due", False)
                    needs_followup = schedule_followup or (boundary_due and not settled)
                    if needs_followup or settled:
                        previous_generation = self._refresh_generation
                        if needs_followup:
                            trigger = "export_reconciled" if settled else "export_reconciliation_changed"
                            self._schedule_debounced_refresh(
                                trigger, debounce_seconds=0, force=True,
                                mode=_RefreshMode.IMMEDIATE if boundary_due else _RefreshMode.DEBOUNCED,
                            )
                        else:
                            self._mark_replan_requested(force=True)
                        self._export_boundary_followup_due = False
                        self._export_replan_generations = (previous_generation, self._refresh_generation)
                    if settled:
                        self._export_reconciled_for = current
                        if current is not None and current.reusable:
                            self._settled_export_reconciliation = current
                        self._export_reconcile_pending = False
                        succeeded = True
        except Exception as error:
            _LOGGER.warning("Deferred export reconciliation failed (%s)", type(error).__name__)
            # Keep the gate closed. The next normal refresh retries, without
            # a busy retry loop or consuming durable uncertain-command evidence.
        finally:
            self._last_export_reconciliation = {**timing.finish(), "succeeded": succeeded}
            self._export_reconcile_task = None
            if not getattr(self, "_tearing_down", False):
                self.async_update_listeners()

    def _export_reconciliation_inputs(self) -> _ExportReconciliationInputs | None:
        """Compare export authority and freshness across a deferred transaction."""
        entity = combined_entry_data(self.entry).get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)
        if not entity:
            return None
        now = dt_util.utcnow()
        observed, issue = export_limit_feedback(self.hass, entity, now)
        options = self.options
        ownership = self.store.data.get("ownership", {}).get(EXPORT_ASSET, {})
        issue = owned_feedback_issue(ownership, observed, issue)
        restore_deferred = bool(ownership.get("restoring")) and cooldown_status(
            self.store.data, observed.get("identity"), options, now,
        ).get("deferred") is True
        reusable = False
        if _export_feedback_settled(observed, issue, ownership) and not (
            ownership.get("retry") or ownership.get("pause")
        ):
            identity = observed["identity"]
            limits = self.store.data.get("command_rate_limits", {})
            reusable = (
                (not ownership or ownership.get("identity") == identity)
                and expected_setting_matches(ownership, observed)
                and limits.get(control_key(identity)) == normalized_record(
                    limits, identity, ownership, now, configured_interval=export_change_interval(options),
                )
            )
        return _ExportReconciliationInputs(
            entity, observed, issue, observed.pop("last_readback", None), self.active_control,
            options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True, export_change_interval(options),
            getattr(self, "_startup_auto_recovery_validation_active", False),
            restore_deferred,
            deepcopy(ownership),
            reusable,
        )

    def _export_reconciliation_matches(
        self, settled: _ExportReconciliationInputs | None, current: _ExportReconciliationInputs | None,
    ) -> bool:
        """Allow a newer poll to corroborate unchanged, fully settled settings."""
        if current is None:
            return settled is None
        feedback_settled = _export_feedback_settled(current.feedback, current.issue, current.ownership)
        if feedback_settled and not expected_setting_matches(current.ownership, current.feedback):
            # Ownership may have adopted a different observation during an await.
            return False
        if settled == current:
            return True
        if settled is None:
            return False
        return (
            settled.issue is None and feedback_settled
            and settled.last_readback is not None and current.last_readback is not None
            and current.last_readback >= settled.last_readback
            and settled == replace(current, last_readback=settled.last_readback)
        )

    def _only_export_readback_advanced(self, old_state: Any, new_state: Any, now: datetime) -> bool:
        """Keep queued plans current when a poll only corroborates settled settings."""
        ownership = self.store.data.get("ownership", {}).get(EXPORT_ASSET, {})
        if ownership.get("pending") or ownership.get("restoring"):
            return False
        previous, previous_issue = confirmed_feedback(old_state, now)
        current, current_issue = confirmed_feedback(new_state, now)
        if previous_issue is not None or current_issue is not None or current["pending"]:
            return False
        return current.pop("last_readback") > previous.pop("last_readback") and current == previous

    async def _async_reconcile_export_limit(
        self, timing: RefreshTiming | None = None,
    ) -> _ExportReconciliationInputs | None:
        """Reconcile commands without holding the planner lock."""
        if getattr(self, "_tearing_down", False):
            return None
        entry_data = self.entry_data
        if entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY):
            if timing is not None:
                timing.stage("command_lock_wait_ms")
            async with self._command_lock:
                if timing is not None:
                    timing.stage("export_reconciliation_ms")
                observed_inputs = self._export_reconciliation_inputs()
                await self._async_reconcile_export_limit_locked()
                if observed_inputs is None:
                    return None
                return replace(observed_inputs, ownership=deepcopy(
                    self.store.data.get("ownership", {}).get(EXPORT_ASSET, {}),
                ))
        return None

    async def _async_reconcile_export_limit_locked(self) -> None:
        """Re-read and durably reconcile export state under command authority."""
        # Configuration and feedback may have changed while waiting.
        if getattr(self, "_tearing_down", False):
            return
        entry_data = self.entry_data
        entity = entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)
        if not entity:
            return

        def recheck_restoration() -> str | None:
            if (
                getattr(self, "_tearing_down", False)
                or getattr(self, "_startup_auto_recovery_validation_active", False)
            ):
                return "export_limit_restore_interrupted"
            if self.entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY) != entity:
                return TARGET_CHANGED
            return None

        def can_seed_current_mapping() -> bool:
            return (
                not getattr(self, "_tearing_down", False)
                and self.active_control
                and self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True
                and combined_entry_data(self.entry).get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY) == entity
            )

        options = self.planner_options
        now = dt_util.utcnow()
        export_control = ExportLimitControl(
            self.hass, self.store, entry_data, recheck_restoration, options=options,
            read_cooldown_options=lambda: self.options,
        )
        ownership = export_control.state
        was_restoring = bool(ownership.get("restoring"))
        continue_restoration = (
            was_restoring and bool(ownership.get("baseline"))
            and not getattr(self, "_startup_auto_recovery_validation_active", False)
        )
        if continue_restoration:
            # Restoration already checks the pending command before deciding whether to write.
            await export_control.restore(now)
        else:
            await export_control.reconcile(now)
        if getattr(self, "_tearing_down", False):
            if continue_restoration:
                await self.store.async_flush()
            return
        # A confirmed pending restore can seed the post-restore setting while active.
        seed = None
        if (
            not export_control.state
            and (not continue_restoration or (ownership.get("pending") or {}).get("restore"))
            and can_seed_current_mapping()
        ):
            observed, export_issue = export_limit_feedback(self.hass, entity, dt_util.utcnow())
            if export_issue == "export_limit_unsupported":
                seed = {"identity": observed["identity"], "pause": export_issue}
            elif not export_issue and not observed["pending"]:
                seed = {"identity": observed["identity"], "expected": {
                    "watts": observed["watts"], "slew_rate": observed["slew_rate"]}}
            if seed is not None:
                await export_control.save(seed)
        if was_restoring and not export_control.state.get("baseline"):
            await self.executor.async_resolve_restore_notifications(EXPORT_ASSET)
        # A planning save-delay scope may be active concurrently. Do not
        # open runtime readiness until reconciliation is durable anyway.
        await self.store.async_flush()
        if seed is not None and not can_seed_current_mapping():
            await export_control.save({})
            await self.store.async_flush()
        self._failed_startup_restore_flush_needed = False
        startup_recovery._retire_failed_startup_restore_watch(self)
        self._schedule_export_retry()

    def _export_retry_control(self) -> ExportLimitControl | None:
        """Check live control authority and the exact owned gateway."""
        if (getattr(self, "_tearing_down", False) or getattr(self, "_export_retry_startup_blocked", False)
                or not getattr(self.hass, "is_running", True) or not self.automatic_control_requested
                or not (self.active_control or getattr(self, "_startup_auto_recovery_authorized", False))):
            return None
        options = self.planner_options
        if options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is not True:
            return None
        entry_data = self.entry_data
        control = ExportLimitControl(self.hass, self.store, entry_data, options=options)
        if (control.state.get("retry", {}).get("state") == "ready"
                and (not self.active_control or getattr(self, "_startup_auto_recovery_authorized", False)
                     or self._export_write_blocked())):
            return None
        identity = export_target_identity(self.hass, entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY, ""))
        owned = control.state.get("identity")
        return control if identity and owned and same_control(identity, owned) else None

    def _export_write_blocked(self) -> bool:
        """Keep export writes closed until arming and startup preflight are complete."""
        return self._export_activation_pending() or self._export_operator_arm_requires_preflight()

    def _export_dispatch_blocked(self) -> bool:
        """Read live operator authority at the ordinary and guarded write boundaries."""
        return not self.automatic_control_requested or self._export_write_blocked()

    def _export_activation_pending(self) -> bool:
        """Distinguish a provisional arm from a completed arm awaiting export preflight."""
        production = self.store.data.get("production")
        return (bool(getattr(self, "_export_startup_activation_pending", False))
                or getattr(self, "_export_operator_arm", None) is not None
                or isinstance(production, dict) and (
                    production.get("startup_activation_pending") is True
                    or production.get(_EXPORT_OPERATOR_ARM_REVALIDATE) is True
                ))

    def _export_operator_arm_requires_preflight(self) -> bool:
        """Require fresh Export Limit evidence before admitting this area."""
        production = self.store.data.get("production")
        return (isinstance(production, dict)
                and production.get(_EXPORT_OPERATOR_ARM_REVALIDATE) == _EXPORT_OPERATOR_ARM_COMPLETED
                and not getattr(self, "_export_operator_arm_admitted", False))

    @callback
    def _schedule_export_retry(self) -> None:
        """Keep one wakeup independent of planning refreshes and feedback events."""
        if getattr(self, "_export_retry_running", False):
            return
        ownership = self.store.data.get("ownership", {}).get(EXPORT_ASSET, {})
        if not ownership.get("retry") and getattr(self, "_export_retry_cancel", None) is None:
            return
        now = dt_util.utcnow()
        control = self._export_retry_control()
        deadline = control.retry_deadline(now) if control is not None else None
        pause = self.store.data.get("control_pause")
        if deadline is not None and control_pause_reason(pause, now, asset=EXPORT_ASSET):
            until = aware_time(pause.get("until")) if isinstance(pause, dict) else None
            deadline = max(deadline, until) if until is not None and until > now else None
        backoff = getattr(self, "_export_retry_backoff_until", None)
        if deadline is not None and backoff is not None:
            # The storage backoff is a short delay. A wall-clock rollback
            # must not leave it anchored hours ahead of a repaired retry.
            self._export_retry_backoff_until = backoff = min(backoff, now + RETRY_INTERVAL)
            deadline = max(deadline, backoff)
        if deadline == getattr(self, "_export_retry_at", None):
            return
        if (cancel := getattr(self, "_export_retry_cancel", None)) is not None:
            cancel()
        self._export_retry_cancel = None
        self._export_retry_at = deadline
        if deadline is None:
            return

        @callback
        def wake(_now: datetime) -> None:
            if self._export_retry_at != deadline or getattr(self, "_tearing_down", False):
                return
            self._export_retry_at = self._export_retry_cancel = None
            self._export_retry_running = True
            self._async_create_listener_task(self._async_run_export_retry())

        self._export_retry_cancel = async_call_later(self.hass, max(0.0, (deadline - now).total_seconds()), wake)

    async def _async_run_export_retry(self) -> None:
        """Run the due check and publish the resulting ownership state."""
        try:
            await self._async_retry_export_limit()
        except Exception as err:
            # A failed durability check cannot admit a write or busy-loop.
            _LOGGER.warning("Export Limit retry failed (%s)", type(err).__name__)
            self._export_retry_backoff_until = dt_util.utcnow() + RETRY_INTERVAL
        finally:
            self._export_retry_running = False
            if not getattr(self, "_tearing_down", False):
                self._schedule_export_retry()
                generation = self._refresh_generation
                self._schedule_debounced_refresh("export_limit_retry", debounce_seconds=0, force=True)
                self._export_replan_generations = (generation, self._refresh_generation)
                self.async_update_listeners()

    async def _async_retry_export_limit(self) -> None:
        """Check installer readiness without holding command authority across provider I/O."""
        async with self._command_lock:
            now = dt_util.utcnow()
            control = self._export_retry_control()
            if control is None or control_pause_reason(self.store.data.get("control_pause"), now, asset=EXPORT_ASSET):
                return
            claim = await control.claim_retry(now)
            if claim is None:
                return
            # Reservation persistence yields. Check again before calling Enphase.
            now = dt_util.utcnow()
            live = self._export_retry_control()
            if live is None or control_pause_reason(self.store.data.get("control_pause"), now, asset=EXPORT_ASSET):
                await control.finish_retry(claim, "export_limit_retry_interrupted")
                return
            identity = live.state["identity"]
            observed, issue = export_limit_feedback(self.hass, live.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
            issue = owned_feedback_issue(live.state, observed, issue)
            if issue in GATEWAY_ISSUES:
                await control.finish_retry(claim, issue)
                return
            upstream = installer_runtime(self.hass, identity)
            if observed.get("pending") or getattr(upstream, "pending", None) is not None:
                await control.finish_retry(claim, "export_limit_pending")
                return

        started = dt_util.utcnow()
        snapshot, issue, terminal = await probe_installer(upstream)

        async with self._command_lock:
            now = dt_util.utcnow()
            live = self._export_retry_control()
            if live is None:
                target = export_target_identity(self.hass, self.entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY, ""))
                interruption = (
                    TARGET_CHANGED if target is None or not same_control(target, identity)
                    else "export_limit_retry_interrupted"
                )
                await control.finish_retry(claim, interruption)
                return
            control = live
            if control.state.get("retry") != claim:
                return
            if control_pause_reason(self.store.data.get("control_pause"), now, asset=EXPORT_ASSET):
                await control.finish_retry(claim, "export_limit_retry_interrupted")
                return
            if (installer_runtime(self.hass, identity) is not upstream
                    or (upstream is None and installer_entry_transitioning(self.hass, identity))):
                await control.finish_retry(claim, TARGET_CHANGED)
                return
            gateway = control.state.get("gateway_identity")
            if snapshot is not None:
                snapshot_gateway = getattr(snapshot, "gateway_identity", None)
                if not gateway or not snapshot_gateway:
                    await control.finish_retry(claim, GATEWAY_UNVERIFIED)
                    return
                if gateway != snapshot_gateway:
                    await control.finish_retry(claim, GATEWAY_CHANGED)
                    return
            if issue is not None:
                observed, feedback_issue = export_limit_feedback(
                    self.hass, control.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now,
                )
                gateway_issue = owned_feedback_issue(control.state, observed, feedback_issue)
                if gateway_issue in GATEWAY_ISSUES:
                    await control.finish_retry(claim, gateway_issue)
                    return
                if not terminal and getattr(upstream, "pending", None) is not None:
                    issue = "export_limit_pending"
                await control.finish_retry(claim, issue, terminal=terminal, refund=issue == COMPANION_UPGRADE_REQUIRED)
                return
            _, feedback_issue = export_limit_feedback(
                self.hass, control.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now,
            )
            if feedback_issue == "export_limit_entity_unavailable":
                await control.finish_retry(claim, feedback_issue)
                return
            setting = (snapshot.watts if snapshot.enabled else None, snapshot.slew)
            reconciliation_issue = await control.reconcile(now)
            now = dt_util.utcnow()
            if installer_runtime(self.hass, identity) is not upstream:
                await control.finish_retry(claim, TARGET_CHANGED)
                return
            if reconciliation_issue in GATEWAY_ISSUES:
                await control.finish_retry(claim, reconciliation_issue)
                return
            current = export_limit_evidence(self.hass, control.data, control.options, now)
            observed = current["feedback"]
            if current["feedback_issue"] is None and (
                mismatch := gateway_identity_issue(gateway, observed.get("gateway_identity"))
            ):
                await control.finish_retry(claim, mismatch)
                return
            if not current["ready"] or observed["last_readback"] < started.timestamp():
                await control.finish_retry(claim, current["reason"] or "export_limit_readback_stale")
                return

            def retry_authority() -> str | None:
                if (getattr(self, "_tearing_down", False)
                        or not getattr(self.hass, "is_running", True)
                        or getattr(self, "_export_retry_startup_blocked", False)
                        or not (self.active_control or getattr(self, "_startup_auto_recovery_authorized", False))
                        or control_pause_reason(
                            self.store.data.get("control_pause"), dt_util.utcnow(), asset=EXPORT_ASSET,
                        )
                        or self.entry_data != control.data or self.planner_options != control.options):
                    return "export_limit_retry_interrupted"
                target = control.data[CONF_ENPHASE_EXPORT_LIMIT_ENTITY]
                live_identity = export_target_identity(self.hass, target)
                if (live_identity is None or not same_control(live_identity, identity)
                        or installer_runtime(self.hass, identity) is not upstream
                        or getattr(upstream, "stopped", False) is True):
                    return TARGET_CHANGED
                if upstream.enabled is not True:
                    return UPSTREAM_CONTROLS_DISABLED
                return None

            def retry_readback_issue(live: dict[str, Any], issue: str | None) -> str | None:
                if issue == GATEWAY_UNVERIFIED:
                    return issue
                if not issue and (mismatch := gateway_identity_issue(gateway, live.get("gateway_identity"))):
                    return mismatch
                if (issue or live["last_readback"] < started.timestamp()
                        or (live["watts"], live["slew_rate"]) != setting):
                    return "export_limit_retry_readback_changed"
                return None

            def retry_guard() -> str | None:
                if (reason := retry_authority()) is not None:
                    return reason
                target = control.data[CONF_ENPHASE_EXPORT_LIMIT_ENTITY]
                live, live_issue = export_limit_feedback(self.hass, target, dt_util.utcnow())
                readback_issue = retry_readback_issue(live, live_issue)
                if readback_issue in GATEWAY_ISSUES:
                    return readback_issue
                if upstream.pending is not None or live.get("pending"):
                    return "export_limit_pending"
                return readback_issue

            if (guard_issue := retry_guard()) is not None:
                await control.finish_retry(claim, guard_issue, terminal=guard_issue == UPSTREAM_CONTROLS_DISABLED)
                return
            block = current["blocks"][0]
            if observed["watts"] == block["watts"]:
                await control.finish_retry(claim, None)
                return
            def startup_pending() -> bool:
                return (getattr(self, "_startup_auto_recovery_authorized", False)
                        or getattr(self, "_startup_auto_recovery_validation_active", False)
                        or self._export_write_blocked())

            if not self.active_control or startup_pending():
                # Let startup validate and arm production before the guarded
                # retry obtains a fresh snapshot for the write.
                await control.finish_retry(claim, None, awaiting_startup_arm=True)
                return
            self.executor.options = control.options
            self.executor.entry_data = control.data
            action = export_action(f"export-retry-{now.isoformat()}", block)

            def dispatch_guard() -> str | None:
                if not self.active_control or startup_pending():
                    return "export_limit_retry_interrupted"
                return retry_guard()

            def validate_write(cooldown_deadline: datetime | None) -> None:
                now = dt_util.utcnow()
                if not self.active_control or startup_pending():
                    raise RetryWriteDeferred("export_limit_retry_interrupted")
                if (reason := retry_authority()) is not None:
                    raise RetryWriteDeferred(reason)
                if cooldown_deadline is not None and now < cooldown_deadline:
                    raise RetryWriteDeferred("export_limit_change_rate_limited")
                live, feedback_issue = export_limit_feedback(
                    self.hass, control.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now,
                )
                if feedback_issue == "export_limit_entity_unavailable":
                    raise RetryWriteDeferred(feedback_issue)
                if (reason := retry_readback_issue(live, feedback_issue)) is not None:
                    raise RetryWriteDeferred(reason)
                if self.executor._control_rejection_reason(action, now) is not None:
                    raise RetryWriteDeferred("export_limit_retry_interrupted")
                if not action.execute_not_before <= now < action.execute_not_after:
                    raise RetryWriteDeferred("export_tariff_expired")
                blocks, _ = current_tariff_blocks(
                    self.hass, control.data[CONF_AMBER_EXPORT_PRICE], control.options, now,
                )
                if not blocks or blocks[0] != block:
                    raise RetryWriteDeferred("export_tariff_changed")

            async def apply(
                watts: int | None, mark_dispatched: Callable[[], None], cooldown_deadline: datetime | None,
            ) -> object:
                denial: RetryWriteDeferred | None = None
                authorized = False

                def authorize() -> None:
                    nonlocal denial, authorized
                    try:
                        validate_write(cooldown_deadline)
                    except RetryWriteDeferred as err:
                        denial = err
                        raise
                    mark_dispatched()
                    authorized = True

                try:
                    return await upstream.async_apply_guarded(watts, expected=snapshot, before_write=authorize)
                except (Exception, asyncio.CancelledError) as err:
                    if denial is not None:
                        raise denial from None
                    if not authorized and isinstance(err, Exception):
                        if (reason := retry_authority()) is not None:
                            raise RetryWriteDeferred(reason) from None
                        if installer_gateway_evidence_lost(upstream, err):
                            raise RetryWriteDeferred(GATEWAY_UNVERIFIED) from None
                    raise

            _, reason, sent = await self.executor.async_execute_export_limit(
                action, now, retry=claim, dispatch_guard=dispatch_guard, retry_apply=apply,
            )
            if not sent:
                await control.finish_unsent_retry(claim, reason, started.timestamp())

    async def _async_update_data_locked(
        self, *, defer_execution: bool = False, defer_publication: bool = False,
    ) -> EnergyPlan | None:
        """Refresh planner data while holding the planner lock."""
        preparation_started = perf_counter()
        self._update_vehicle_session()
        started_generation = self._refresh_generation
        now = dt_util.utcnow()
        entry_options = deepcopy(dict(self.entry.options))
        options = self.planner_options
        evidence_options = self.options
        entry_data = self.entry_data
        mapped_entry_data = deepcopy(combined_entry_data(self.entry))
        tariffs_current: Callable[[], bool] | None = None

        def is_current() -> bool:
            return self._plan_inputs_current(
                started_generation, mapped_entry_data, tariffs_current, entry_options=entry_options,
            )

        weather_forecast, weather_forecast_details = await self._async_weather_for_refresh(
            entry_data, options, now=now, force=bool(getattr(self, "_force_next_refresh", False)),
        )
        if not is_current():
            self._discard_superseded_refresh()
            return self.data
        if VEHICLES in combined_entry_data(self.entry):
            await self.store.async_save_vehicle_session(self.vehicle_session.snapshot())
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
        if VEHICLES in entry_data:
            profile = self.vehicle_session.profile
            model = self.store.data.get("ev_vehicle_calibrations", {}).get(profile["id"], {}) if profile else {}
            updated_model = self.vehicle_calibration.observe(
                self.hass, self.vehicle_session, entry_data, model, now,
                charge_rate_kw=float(options["ev_charge_rate_kw"]),
            )
            if updated_model is not None and profile is not None:
                await self.store.async_save_vehicle_calibration(profile["id"], updated_model)
                self._force_next_refresh = True
        self.executor.options = options
        force_refresh = bool(getattr(self, "_force_next_refresh", False))
        self._force_next_refresh = False
        load_source_outage = _updated_load_source_outage(
            self.hass,
            entry_data,
            self.store.data.get("load_source_outage"),
            now=now,
            options=options,
        )
        load_recovery_completed = bool(self.store.data.get("load_source_outage")) and not load_source_outage
        if load_source_outage:
            self._load_recovery_pending = False
        elif load_recovery_completed:
            self._load_recovery_pending = True
        await self._async_save_load_source_outage(load_source_outage)
        if not is_current():
            self._force_next_refresh |= force_refresh
            self._discard_superseded_refresh()
            return self.data
        capabilities = CapabilityDiscovery(self.hass, entry_data, options)
        dry_hvac = capabilities.hvac_evidence_in_dry()
        # Sample ownership with the Dry evidence. A command can confirm the
        # Off start and clear its provisional snapshot during a later await.
        dry_control = _hvac_control_from_ownership(self.store.data.get("ownership", {})) if dry_hvac is not None else {}
        dry_during_provisional_off_start = bool(
            dry_hvac is not None and dry_hvac.issues == [HVAC_DRY_ZONE_TARGETS_ABSENT]
            and dry_control.get("attempt_snapshot") and not dry_control.get("main_state_committed")
            and not dry_control.get("required_evidence_lost")
        )
        self._log_dry_takeover_transition(dry_hvac is not None and HVAC_DRY_ZONE_TARGETS_ABSENT in dry_hvac.issues)
        tariff_fingerprints: dict[str, _TariffFingerprint] = {}
        decision_fingerprint = _decision_input_fingerprint(
            self.hass,
            entry_data,
            options,
            self.overrides,
            now=dt_util.utcnow(),
            weather_forecast=weather_forecast,
            tariff_fingerprints=tariff_fingerprints,
            previous_tariff_fingerprints=getattr(self, "_last_tariff_fingerprints", {}),
            dry_hvac=dry_hvac,
            dry_takeover_revision=getattr(self, "_dry_takeover_revision", 0),
            dry_during_provisional_off_start=dry_during_provisional_off_start,
        )
        self._last_tariff_fingerprints = tariff_fingerprints
        if (
            not force_refresh
            and not getattr(self, "_export_reconcile_pending", False)
            and not load_source_outage
            and not getattr(self, "_load_recovery_pending", False)
            and decision_fingerprint is not None
            and decision_fingerprint == getattr(self, "_last_decision_fingerprint", None)
            and getattr(self, "data", None) is not None
        ):
            self._increment_refresh_counter("fingerprint_skipped")
            if (refresh_timing := current_timing()) is not None:
                refresh_timing.aliases["fingerprint_ms"] = round((perf_counter() - preparation_started) * 1000, 3)
            assert self.data is not None
            hvac_details = self.store.data.get("discovery", {}).get("hvac", {}).get("details")
            self._log_availability_transition(self.data.input_issues, hvac_details=hvac_details)
            return self.data
        self.executor.entry_data = entry_data
        discovery = capabilities.inspect(hvac=dry_hvac)
        discovery_report = discovery.as_dict()
        await self.store.async_save_discovery(discovery_report)
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
            observe_export_limit=bool(self.store.data.get("ownership", {}).get(EXPORT_ASSET)),
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
        context = await manager.async_build_context(self.overrides)
        if getattr(self, "_tearing_down", False):
            return self.data
        if context is None or not is_current():
            if context is None:
                self._mark_replan_requested(force=True)
            elif force_refresh:
                self._force_next_refresh = True
            self._discard_superseded_refresh()
            return self.data

        tariffs_current = getattr(manager, "tariff_inputs_current", None)

        context.active_overrides = _unexpired_overrides(context.active_overrides, context.created_at)
        self._schedule_tariff_validity_retry(
            getattr(manager, "forecast_coverage_details", []), observed_at=context.created_at,
        )
        runtime = dict(self.store.data.get("ownership", {}).get(EXPORT_ASSET, {}))
        if runtime and not getattr(context, "export_limit", None):
            # Keep outstanding restoration visible after its selector is off.
            context.export_limit = export_limit_evidence(self.hass, entry_data, options, context.created_at)
        if getattr(context, "export_limit", None):
            context.export_limit["ownership"] = runtime
            if options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is not True:
                context.export_limit.update(ready=False, reason="export_limit_control_disabled")
            if getattr(self, "_export_reconcile_pending", False):
                context.export_limit.update(
                    ready=False, reason="export_reconciliation_pending", reconciliation_pending=True,
                )
            if runtime.get("pause") or runtime.get("pending") or runtime.get("restoring"):
                context.export_limit.update(ready=False, reason=runtime.get("pause") or (
                    "export_limit_pending" if runtime.get("pending") else "export_limit_restoration_pending"))
            identity_issue = owned_feedback_issue(
                runtime, context.export_limit.get("feedback", {}),
                context.export_limit.get("feedback_issue"),
            )
            if identity_issue in EXPORT_IDENTITY_ISSUES:
                context.export_limit.update(ready=False, feedback_ready=False, reason=identity_issue)
            timing = cooldown_status(self.store.data, export_target_identity(
                self.hass, entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY, "")), options, context.created_at)
            context.export_limit.update(timing)
            if timing.get("deferred"):
                context.export_limit.update(ready=False, reason=(timing["cooldown_reason"]
                    if context.export_limit.get("reason") in {None, "export_limit_restoration_pending"}
                    else context.export_limit["reason"]))
            self._schedule_export_boundary(context.export_limit
                if options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True or runtime.get("restoring") else {})
        elif getattr(self, "_export_boundary_cancel", None) is not None:
            self._schedule_export_boundary({})
        context.ev_evidence["load_recovery_completed"] = getattr(self, "_load_recovery_pending", False)
        ev_sample = sample_ev(self.hass, entry_data, options, context)
        reservation = self.store.data.get("ev_grid_reservation", {})
        telemetry = update_ev_telemetry(
            self.store.data.get("ev_telemetry", {}), ev_sample,
            reserved_kw=float(reservation.get("load_kw", 0) or 0),
        )
        if (any(entry_data.get(key) for key in ("ev_power_entity", "ev_energy_entity", "ev_power_limit_entity"))
                or options.get("ev_price_limit_enabled") or telemetry.get("vehicle_suspended")
                or self.store.data.get("ev_telemetry")):
            await self.store.async_save_ev_telemetry(telemetry)
        context.ev_evidence["load_fallback"] = dict(getattr(manager, "load_forecast_details", {}))
        from .ev_resilience import retain_outage_power_ceiling

        context.ev_evidence["commanded_kw"] = retain_outage_power_ceiling(
            load_source_outage, ev_sample["commanded_kw"],
        )
        if load_source_outage:
            await self._async_save_load_source_outage(load_source_outage)
        context.ev_evidence.update({
            "vehicle_suspended": telemetry.get("vehicle_suspended", False),
            "performance": telemetry.get("performance", {}),
            "emergency_spend": telemetry.get("emergency_spend", 0),
            "budget_uncertain": telemetry.get("budget_uncertain", False),
            "delivery_status": telemetry.get("delivery_status"),
            "power_capability": ev_sample["power_capability"],
            "power_limit_mapped": ev_sample["power_limit_mapped"],
        })
        if ev_sample["power_limit_mapped"] and ev_sample["power_kw"] is None:
            context.ev_evidence["power_capability"] = None
            context.input_issues.append("ev_power_feedback_unavailable")
        context.ev_evidence.update(audit_evidence(
            budget_history(self.store.data), options, context.created_at))
        previous_plan = self.store.data.get("active_plan", {})
        for previous_action in previous_plan.get("actions", []) if isinstance(previous_plan, dict) else []:
            if previous_action.get("asset") == "ev":
                context.ev_evidence["retained_schedule"] = previous_action.get("desired_state", {}).get(
                    "allocated_slots", [])

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
        context.hvac_control = _hvac_control_from_ownership(self.store.data.get("ownership", {}))
        context.dry_during_provisional_off_start = dry_during_provisional_off_start
        reconcile_cycle = getattr(self.executor, "async_reconcile_hvac_cycle", None)
        if callable(reconcile_cycle):
            await reconcile_cycle()
        cycle = self.store.data.get("hvac_cycle", {})
        if isinstance(cycle, dict) and cycle.get("entity_id") == entry_data.get(CONF_DAIKIN_CLIMATE):
            context.hvac_cycle = dict(cycle)
        context.hvac_tariff_slots = manager.retained_hvac_tariff_slots(context)
        if context.hvac_control and climate_issues and not dry_during_provisional_off_start:
            control = context.hvac_control
            required_issues = climate_issues
            if (
                "climate_zone_unavailable" in climate_issues
                and control.get("phase") == "away_off" and control.get("main_state_committed") is True
                and not (control.get("zone_states") or control.get("attempt_snapshot"))
            ):
                # A completed head-only stop never acquired these zones. Use
                # the same capability rules as the original shutdown action.
                required_issues = CapabilityDiscovery(
                    self.hass, entry_data, options, hvac_desired_state={"hvac_mode": "off"},
                ).inspect().hvac.issues
            if required_issues:
                control["required_evidence_lost"] = ",".join(required_issues)
        stored_thermal = dict(self.store.data.get("thermal_model", {}))
        thermal_model = update_zone_thermal_model(stored_thermal, thermal_sample(
            context, options, self.hass.states, entry_data.get(CONF_DAIKIN_CLIMATE, ""),
        ))
        if thermal_model != stored_thermal:
            await self.store.async_save_thermal_model(thermal_model)
        context.climate_inputs["thermal_model"] = thermal_model
        climate_state = observe(dict(self.store.data.get("climate_engine", {})), context,
                                int(options.get("hvac_min_cycle_minutes", 20)))
        context.climate_engine = update_readiness(climate_state, climate_state.get("model", {}), context, options)
        preparation_ms = round((perf_counter() - preparation_started) * 1000, 3)
        planner = DryRunPlanner(
            options,
            thermal_model=thermal_model,
            ev_charge_calibration=ev_charge_calibration,
            ev_charging_entity_id=entry_data.get(CONF_EV_CHARGING),
            ev_soc_entity_id=entry_data.get(CONF_EV_SOC),
        )
        planner_started = perf_counter()
        stage("planner_ms")
        # Executor admission and computation are elapsed durations, not CPU time.
        worker_times: list[float] = []

        def create_plan() -> EnergyPlan:
            worker_times.append(perf_counter())
            try:
                return planner.create_plan(context)
            finally:
                worker_times.append(perf_counter())

        try:
            plan = await self.hass.async_add_executor_job(create_plan)
        finally:
            stage("post_plan_ms")
            if (refresh_timing := current_timing()) is not None and len(worker_times) == 2:
                refresh_timing.aliases.update(
                    planner_queue_ms=round(max(worker_times[0] - planner_started, 0) * 1000, 3),
                    planner_work_ms=round(max(worker_times[1] - worker_times[0], 0) * 1000, 3),
                )
        self._increment_refresh_counter("computed")
        if not is_current():
            self._discard_superseded_refresh()
            return self.data
        if getattr(context, "weather_evidence", None):
            self.weather_forecast_diagnostics = dict(context.weather_evidence)
            if context.weather_evidence.get("ownership_retained"):
                self._schedule_next_boundary_refresh()
        planner_ms = (perf_counter() - planner_started) * 1000
        persistence_started = perf_counter()
        await self.store.async_save_climate_engine(context.climate_engine)
        violations = ConstraintValidator(options).validate_plan(context, plan)
        if violations:
            plan.input_issues.extend(violations)
            if "input_health_unsafe" in violations:
                plan.status = "unsafe"
            if plan.mode == PlannerMode.ACTIVE_HEALTHY:
                plan.mode = PlannerMode.ACTIVE_DEGRADED
        self._log_availability_transition(
            plan.input_issues, hvac_details=discovery_report.get("hvac", {}).get("details"),
        )
        self._record_startup_auto_recovery_validation_candidate(plan, violations)
        await self.executor.async_notify_plan_fallback(plan, violations)
        if not is_current():
            self._discard_superseded_refresh()
            return self.data
        from .plan_presentation import latest_forecast_snapshot

        # Replay history coalesces by half-hour before this result is published.
        # Keep one complete snapshot for the still-committed plan across Store
        # awaits and independent listener notifications. Never retain the new
        # result here: generation checks may still discard it.
        self._retained_forecast_snapshot = deepcopy(latest_forecast_snapshot(self))
        forecast_snapshot = {
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
            "forecast_calibration_version": FORECAST_CALIBRATION_VERSION,
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

        def record_evidence() -> None:
            self.store.record_forecast_snapshot(forecast_snapshot)
            self.store.record_production(self._production_evidence_for_plan(
                plan, violations, entry_data, evidence_options,
            ))
            if plan.mode == PlannerMode.DRY_RUN:
                self.store.record_dry_run_comparison(self._dry_run_comparison(plan))

        async def commit_plan() -> EnergyPlan | None:
            result = await self._async_commit_plan_if_current(
                started_generation, plan, context, options, execute=not defer_execution,
                mapped_entry_data=mapped_entry_data, tariffs_current=tariffs_current,
                entry_options=entry_options, record_evidence=record_evidence,
            )
            if result is plan and is_current():
                # The early digest can precede the tariff snapshot used to
                # build this plan. Leave the shortcut unset if they differ.
                self._last_decision_fingerprint = (
                    decision_fingerprint if decision_fingerprint is not None
                    and _tariff_fingerprints_current(
                        self.hass, tariff_fingerprints, now=context.created_at, options=options,
                        export_entity=entry_data.get(CONF_AMBER_EXPORT_PRICE),
                    ) else None
                )
                self._sync_ai_request_to_plan(plan)
            return result

        if (refresh_timing := current_timing()) is not None:
            refresh_timing.aliases.update(
                inputs_ms=preparation_ms,
                planner_ms=round(planner_ms, 3),
                persistence_and_execution_ms=round((perf_counter() - persistence_started) * 1000, 3),
            )
        if defer_publication:
            self._pending_plan_commit = commit_plan
            return self.data
        result = await commit_plan()
        await self.store.async_flush_deferred()
        return result

    def _log_dry_takeover_transition(self, dry_takeover_blocked: bool) -> None:
        """Log changes to the Dry takeover block observed by planning or execution."""
        if dry_takeover_blocked != getattr(self, "_dry_takeover_blocked", False):
            if dry_takeover_blocked:
                _LOGGER.info(HVAC_DRY_TAKEOVER_SUMMARY)
            else:
                _LOGGER.info("Daikin Dry mode no longer blocks HVAC takeover")
            self._dry_takeover_blocked = dry_takeover_blocked
            # A queued action may observe a complete Dry episode between
            # refreshes. Do not reuse a plan whose audit still shows that pause.
            self._dry_takeover_revision = getattr(self, "_dry_takeover_revision", 0) + 1

    def _log_availability_transition(self, issues: list[str], *, hvac_details: dict[str, Any] | None = None) -> None:
        """Log each input transition with bounded details and recovery duration."""
        active = availability_details(issues, self.entry_data, hvac_details=hvac_details)
        previous: dict[AvailabilityIdentity, tuple[float, str]] = getattr(self, "_availability_outages", {})
        now = monotonic()
        warned: set[AvailabilityIdentity] = getattr(self, "_availability_warned", set())
        startup_grace = now < getattr(self, "_availability_startup_deadline", 0)
        startup_pending: set[AvailabilityIdentity] = getattr(self, "_availability_startup_pending", set(active))
        self._availability_startup_pending = startup_pending & active.keys()
        if startup_grace and not active:
            self._availability_startup_deadline = now
        for identity in sorted(active.keys() - warned):
            if startup_grace and identity in self._availability_startup_pending:
                continue
            warned.add(identity)
            _LOGGER.warning(
                "Planner required input or service unavailable: required_evidence_missing %s", active[identity],
            )
        for identity in sorted(active.keys() & previous.keys()):
            if active[identity] != previous[identity][1]:
                _LOGGER.info("Planner required input still unavailable: required_evidence_changed %s", active[identity])
        for identity in sorted(previous.keys() - active.keys()):
            log = _LOGGER.info if identity in warned else _LOGGER.debug
            log(
                "Planner required inputs and services recovered: required_evidence_restored %s outage_seconds=%.1f",
                previous[identity][1], max(now - previous[identity][0], 0),
            )
        self._availability_warned = warned & active.keys()
        self._availability_outages = {
            identity: (previous[identity][0] if identity in previous else now, detail)
            for identity, detail in active.items()
        }

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
                if result.climate_model:
                    state = dict(self.store.data.get("climate_engine", {}))
                    if (
                        state.get("identity") == result.climate_model.get("identity")
                        and state.get("power_source_type", "unknown")
                        == result.climate_model.get("power_source_type", "unknown")
                        and state.get("power_evidence_after") == result.climate_model.get("power_evidence_after")
                    ):
                        state["model"] = result.climate_model
                        await self.store.async_save_climate_engine(state)
                if result.ev_changed:
                    await self.store.async_save_ev_charge_calibration(result.ev_model)
                if result.load_changed:
                    await self.store.async_save_builtin_load_forecast(result.load_model)
            if result.ev_changed or result.load_changed:
                self._schedule_debounced_refresh("history_training", debounce_seconds=0, force=True)

    async def async_request_replan(self) -> None:
        """Refresh explicitly even while Core's request cooldown is active."""
        self._mark_forced_refresh("manual_replan")
        await self.async_refresh()

    async def async_reconcile_production_evidence_contract(self) -> bool:
        """Prepare startup control without discarding prior active intent."""
        operator_disarm = getattr(self, "_operator_disarm_request", None)
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
        if (not production.armed and recovery_status == "failed" and isinstance(recovery, dict)
                and recovery.get("last_reason") == "startup_recovery_save_failed"):
            if getattr(self, "_refresh_input_subscription", None) is not None:
                await self._async_restore_failed_startup()
            else:
                async with self._async_capture_initial_device_feedback(release_hvac=False):
                    await self._async_restore_failed_startup()
            return True
        operator_disarmed = isinstance(recovery, dict) and (
            recovery_status == "cancelled" and recovery.get("last_reason") == "operator_disarmed"
        )
        provisional_arm = production.armed and (
            production.raw.get("startup_activation_pending") is True
            or production.raw.get(_EXPORT_OPERATOR_ARM_REVALIDATE) is True
            or (recovery_status == "restoring" and production.raw.get("armed_reason") == "startup_auto_recovered")
        )
        if operator_disarmed:
            if production.armed:
                # Older saves can contain a completed operator cancellation with
                # the old arm still present.
                await self.async_disarm_production_control("operator_disarmed")
            export = ExportLimitControl(self.hass, self.store, self.entry_data)
            export_state = export.state
            if export_state.get("baseline") and not export_state.get("restoring"):
                # The stop can be durable before the original process reaches
                # its restore call. Reconcile and restore under the command lock.
                await export.save({**export_state, "restoring": True})
            return production.armed
        if provisional_arm:
            if self.automatic_control_requested:
                # Commit the disarm and resumable disposition together, even
                # if a second restart interrupts startup reconciliation.
                await self._async_update_startup_auto_recovery(
                    "waiting_for_home_assistant", successful_runs=0, reason="startup_safe_recovery_pending",
                    disarm_reason="startup_activation_interrupted",
                )
            else:
                await self.async_disarm_production_control("startup_activation_interrupted")
            production = parse_production_state(self.store.data.get("production"))
            recovery = production.raw.get("startup_auto_recovery")
            recovery_status = str(recovery.get("status", "")) if isinstance(recovery, dict) else ""
        recovery_pending_while_disarmed = bool(
            self.automatic_control_requested
            and not production.armed
            and isinstance(recovery, dict)
            and (recovery_status in {"waiting_for_safe", "validating", "restoring"}
                 or (recovery_status == "waiting_for_home_assistant"
                     and recovery.get("last_reason") == "startup_safe_recovery_pending"))
        )
        expected_fingerprint = production_evidence_fingerprint(self.entry_data, self.options)

        recovery_reason = None
        if self.active_control and pause_blocks_all_control and required_control_areas != [EXPORT_ASSET]:
            # A temporary pause remains authoritative, but it must not erase a
            # previously armed installation's automatic-control lifecycle.
            recovery_reason = "startup_control_paused"
        elif self.active_control and production.dry_run_evidence_fingerprint != expected_fingerprint:
            # A software or entry migration can change the evidence contract
            # before configured entities have finished restoring. Preserve the
            # recovery handoff so fresh plans can validate the new contract.
            recovery_reason = "production_evidence_contract_changed"
        if recovery_reason is not None:
            self._startup_auto_recovery_authorized = True
            self._export_retry_startup_blocked = True
            self._startup_auto_recovery_deadline = None
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe", successful_runs=0, reason=recovery_reason, disarm_reason=recovery_reason,
            )
            if self._startup_auto_recovery_authorized:
                await self.async_restore_safe_state(recovery_reason, refresh=False)
            return True

        if self.active_control and not pause_blocks_all_control:
            # A previously running installation resumes immediately. Runtime
            # action gates still reject unsafe work while the startup grace
            # observes the fully started Home Assistant instance.
            self._startup_auto_recovery_authorized = True
            self._export_retry_startup_blocked = True
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
            self._export_retry_startup_blocked = True
            self._startup_auto_recovery_deadline = None
            reason = recovery.get("last_reason") if isinstance(recovery, dict) else None
            if (
                recovery_status == "waiting_for_safe"
                and reason in ("startup_control_paused", "production_evidence_contract_changed")
            ):
                # A failed or interrupted disarm save can leave restoration outstanding.
                await self.store.async_flush()
                if operator_disarm is not getattr(self, "_operator_disarm_request", None):
                    return True
                await self.async_restore_safe_state(reason, refresh=False)
                if operator_disarm is not getattr(self, "_operator_disarm_request", None):
                    return True
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

    async def async_handle_zone_mapping_update(self) -> None:
        """Refresh room feedback without restoring unrelated actuator ownership."""
        current = deepcopy(self.entry_data.get("hvac_zone_mappings", {}))
        previous = getattr(self, "_last_zone_mappings", current)
        if current == previous:
            return
        async with self._options_update_lock:
            async with self._command_lock:
                # Actuator mappings are unchanged: the persisted original HVAC
                # baselines remain the restoration targets. Reconcile uncertain
                # HVAC commands before the new feedback can acquire control.
                await self.executor.async_release_hvac_control("hvac_configuration_changed")
                production = dict(parse_production_state(self.store.data.get("production")).raw)
                old_data = {**self.entry_data, "hvac_zone_mappings": previous}
                if production.get("dry_run_evidence_fingerprint") == production_evidence_fingerprint(
                    old_data, self.options,
                ):
                    production["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
                        self.entry_data, self.options,
                    )
                    await self._async_save_production(production)
                self.executor.entry_data = self.entry_data
                self._last_zone_mappings = current
                refresh = getattr(self, "_refresh_input_subscription", None)
                if callable(refresh):
                    refresh()
            await self.async_request_replan()

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
                CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
            }
            # Policy changes need a fresh plan, not a release of unrelated assets.
            # Physical capabilities, entity mappings and safety thresholds retain
            # the full disarm/recovery path below.
            hot_policy_keys = {
                "max_daily_ev_actions", "max_daily_climate_actions", "max_daily_enphase_actions",
                CONF_MAX_DAILY_EXPORT_LIMIT_ACTIONS,
                "manual_hvac_override_minutes", "plan_fallback_notifications_enabled",
                "ev_charging_strategy", "ev_readiness_buffer_minutes", "ev_schedule_min_saving",
                "ev_schedule_min_saving_percent", "ev_min_dwell_minutes",
                "hvac_precondition_lead_minutes", "hvac_precondition_max_lead_minutes",
                "hvac_thermal_learning_enabled", "hvac_precondition_min_price_delta",
                "hvac_suppression_min_price_delta", "hvac_precondition_while_away",
                "hvac_precondition_configured_zones_only",
            }
            if previous_options is not None and changed_option_keys <= hot_policy_keys:
                async with self._command_lock:
                    self.executor.options = current_options
                    policy_production = dict(parse_production_state(self.store.data.get("production")).raw)
                    if policy_production.get("dry_run_evidence_fingerprint") == production_evidence_fingerprint(
                        self.entry_data, previous_options,
                    ):
                        policy_production["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
                            self.entry_data, current_options,
                        )
                    await self._async_save_production(policy_production)
                    self._last_handled_options = option_state
                    self._schedule_export_retry()
                await self.async_request_replan()
                return
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
            operator_disarm = getattr(self, "_operator_disarm_request", None)
            automatic_control_still_requested = bool(
                strict_bool(current_options.get(CONF_PLANNER_ENABLED), default=False)
                and not strict_bool(current_options.get(CONF_DRY_RUN), default=True)
            )
            resume_disarmed_recovery = (
                automatic_control_still_requested and device_control_only_change
                and ((was_running and self._export_write_blocked()) or (recovery_was_pending and not production.armed))
            )
            await self.async_cancel_startup_auto_recovery(
                "options_changed", preserve_control=resume_disarmed_recovery,
                restore_owned_state=not device_control_only_change,
            )
            if operator_disarm is not getattr(self, "_operator_disarm_request", None):
                was_running = automatic_lifecycle_was_active = resume_disarmed_recovery = False
            if resume_disarmed_recovery:
                # Preserve the previous recovery while cancellation drains.
                # Save its restartable replacement before restoration or replanning.
                await self._async_update_startup_auto_recovery(
                    "waiting_for_home_assistant", successful_runs=0, reason="startup_safe_recovery_pending",
                )
            if (
                was_running
                and automatic_control_still_requested
                and operator_disarm is getattr(self, "_operator_disarm_request", None)
                and (not device_control_only_change or resume_disarmed_recovery)
                and parse_production_state(self.store.data.get("production")).armed
            ):
                await self.async_disarm_production_control("configuration_changed")
                if not device_control_only_change:
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
                    (CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED, EXPORT_ASSET, EXPORT_ASSET),
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
            if (automatic_lifecycle_was_active and self.automatic_control_requested
                    and operator_disarm is getattr(self, "_operator_disarm_request", None)):
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
                    if operator_disarm is not getattr(self, "_operator_disarm_request", None):
                        return
                self._startup_auto_recovery_authorized = True
                self._export_retry_startup_blocked = True
                if not resume_disarmed_recovery:
                    await self._async_update_startup_auto_recovery(
                        "waiting_for_home_assistant",
                        successful_runs=0,
                        reason=(
                            "startup_safe_recovery_pending" if self._export_activation_pending()
                            else "configuration_changed"
                        ),
                    )
                if operator_disarm is getattr(self, "_operator_disarm_request", None):
                    self.async_start_startup_auto_recovery()

    async def async_prepare_configuration_reload(self) -> None:
        """Persist a disarmed recovery handoff for a changed entity topology."""
        self._configuration_reload_handoff = False
        operator_disarm = getattr(self, "_operator_disarm_request", None)
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
        # This method owns the disarm and restore so cancellation cannot
        # restore the same devices before we evaluate the export handoff.
        await self.async_cancel_startup_auto_recovery("configuration_changed", restore_owned_state=False)
        if automatic_lifecycle_was_active and operator_disarm is getattr(self, "_operator_disarm_request", None):
            if parse_production_state(self.store.data.get("production")).armed:
                await self.async_disarm_production_control("configuration_changed")
            export_state = self.store.data.get("ownership", {}).get(EXPORT_ASSET, {})
            preserve_export_retry = (
                self.automatic_control_requested
                and self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True
                and preserve_retry_on_reload(export_state, export_target_identity(
                    self.hass, self.entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY, ""),
                ))
            )
            restore = await self.async_restore_safe_state(
                "configuration_changed",
                refresh=False,
                preserve_export_retry=preserve_export_retry,
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
            if (_action_outcome_failed(restore) and unresolved_restore
                    and operator_disarm is getattr(self, "_operator_disarm_request", None)):
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
        if (automatic_lifecycle_was_active and self.automatic_control_requested
                and operator_disarm is getattr(self, "_operator_disarm_request", None)):
            await self._async_update_startup_auto_recovery(
                "waiting_for_safe",
                successful_runs=0,
                reason="configuration_changed",
            )
            self._configuration_reload_handoff = operator_disarm is getattr(self, "_operator_disarm_request", None)

    async def async_handle_vehicle_settings_update(self) -> None:
        """Replan runtime vehicle deadlines without relinquishing device control."""
        deadlines = {p["id"]: p[CONF_DEFAULT_READY_BY] for p in combined_entry_data(self.entry).get(VEHICLES, [])}
        if deadlines == getattr(self, "_vehicle_ready_by_settings", {}):
            return
        self._vehicle_ready_by_settings = deadlines
        self._update_vehicle_session()
        self._mark_forced_refresh("ready_by_changed")
        await self.async_request_refresh()

    async def async_set_ready_by(self, ready_by: str) -> None:
        """Persist the native EV ready-by setting and replan."""
        if VEHICLES in combined_entry_data(self.entry):
            self._update_vehicle_session()
            profile = self.vehicle_session.profile
            if profile is None:
                raise ValueError("Select a tracked vehicle before changing its ready-by time")
            profiles = combined_entry_data(self.entry)[VEHICLES]
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, VEHICLES: [
                    {**item, CONF_DEFAULT_READY_BY: ready_by} if item["id"] == profile["id"] else item
                    for item in profiles
                ]},
            )
            await self.async_handle_vehicle_settings_update()
            return
        self.ready_by = ready_by
        options = self.options
        options[CONF_DEFAULT_READY_BY] = ready_by
        config_entries = getattr(self.hass, "config_entries", None)
        update_entry = getattr(config_entries, "async_update_entry", None)
        if callable(update_entry):
            update_entry(self.entry, options=options)
        if self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY):
            await EVChargerAdapter(self.hass, self.entry_data).async_set_ready_by(ready_by)
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

    async def async_charge_now(self, duration_minutes: int = 60) -> EVCommandResult:
        """Charge for a bounded interval, bypassing economics but never capacity checks."""
        if not 1 <= duration_minutes <= 240:
            raise ValueError("Charge now duration must be between 1 and 240 minutes")
        # Obtain current evidence before taking the actuator lock. Pass its
        # freshness check through the durable capacity reservation.
        previous_context = getattr(self, "_last_decision_context", None)
        entry_data = self.entry_data
        options = self.options
        topology = _ev_control_topology(entry_data)
        planner_charge_rate = self.planner_options.get(CONF_EV_CHARGE_RATE_KW)

        def configuration_is_current() -> bool:
            # Core publishes configuration before the update listener may take
            # the command lock or advance the planner generation.
            live_data, live_options = self.entry_data, self.options
            return (
                _ev_control_topology(live_data) == topology
                and all(live_data.get(key) == entry_data.get(key) for key in (
                    CONF_EV_CONNECTED, CONF_EV_SOC, CONF_EV_SMART_CHARGING_TARGET_SOC,
                    CONF_HOUSEHOLD_LOAD, CONF_PV_FORECAST, CONF_PV_FORECAST_SECONDARY,
                    CONF_AMBER_IMPORT_PRICE, CONF_AMBER_EXPORT_PRICE, CONF_DAIKIN_CLIMATE, CONF_DAIKIN_POWER,
                ))
                and all(live_options.get(key) == options.get(key) for key in (
                    CONF_EV_CHARGE_RATE_KW, CONF_GRID_IMPORT_LIMIT_KW, CONF_PLANNING_INTERVAL_MINUTES,
                    CONF_HOUSEHOLD_LOAD_OUTAGE_GRACE_MINUTES, CONF_LOAD_RECOVERY_MAX_AGE_MINUTES,
                    CONF_PRICE_FRESHNESS_MINUTES, CONF_FORECAST_FRESHNESS_MINUTES,
                    "ev_limit_min", "ev_limit_max", "ev_voltage", "ev_phases",
                ))
                and self.planner_options.get(CONF_EV_CHARGE_RATE_KW) == planner_charge_rate
            )

        self._mark_forced_refresh("charge_now")
        await self.async_refresh()
        context = getattr(self, "_last_decision_context", None)
        generation = self._refresh_generation

        def has_fresh_context() -> bool:
            return (
                not getattr(self, "_tearing_down", False)
                and self.last_update_success and context is not previous_context
                and configuration_is_current()
                and context is getattr(self, "_last_decision_context", None)
                and getattr(self, "_last_decision_generation", None) == generation == self._refresh_generation
            )

        refresh_error = "Could not refresh charging evidence. Charge now was not started; retry when inputs recover."
        if not has_fresh_context():
            raise HomeAssistantError(refresh_error)
        async with self._command_lock:
            await self._async_reconcile_vehicle_ownership()
            self.executor.options = self.options
            ev_data = self.entry_data
            self.executor.entry_data = ev_data
            if not has_fresh_context():
                raise HomeAssistantError(refresh_error)

            def charge_now_authorized() -> bool:
                if context is None or not has_fresh_context():
                    return False
                # A single percentage point can cross the target without
                # triggering the general replan threshold.
                soc_id = ev_data.get(CONF_EV_SOC)
                target_id = ev_data.get(CONF_EV_SMART_CHARGING_TARGET_SOC)
                soc = finite(state_value(self.hass, soc_id)) if soc_id else context.current_ev_soc_percent
                target = (
                    _percent_float_or_none(state_value(self.hass, target_id))
                    if target_id else context.ev_target_soc_percent
                )
                return soc is not None and target is not None and 0 <= soc < target <= 100

            expires = dt_util.utcnow() + timedelta(minutes=duration_minutes)
            result = await self.executor.async_manual_ev_charging(
                True, context, charge_now_until=expires, charge_now_guard=charge_now_authorized,
                charge_now_prior_deadline=self._ev_allocation_deadline,
            )
            if result.applied:
                self.overrides = [o for o in self.overrides if o.kind != "manual_ev_charging"]
                self.overrides.append(Override(
                    kind="manual_ev_charging", source="charge_now", expires_at=expires, reason="charge_now",
                ))
                await self.store.async_save_overrides(self.overrides)
                self._clear_ev_auto_start_compensation()
        self._mark_forced_refresh("charge_now_result")
        await self.async_request_refresh()
        return result

    async def async_cancel_charge_now(self) -> EVCommandResult:
        """Stop the explicit session through the normal confirmed-stop path."""
        return await self.async_manual_ev_charging(False)

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

    async def async_resume_climate_planning(self) -> dict[str, Any]:
        """Clear manual climate holds without bypassing execution safety gates."""
        async with self._command_lock:
            async with self._planner_lock:
                await self._async_persist_runtime_hvac_supersessions()
                if not await self._async_clear_expired_manual_hvac_state():
                    raise HomeAssistantError("Could not clear the manual climate override helper.")
                self.overrides = [override for override in self.overrides if override.kind != "manual_hvac"]
                await self.store.async_save_overrides(self.overrides)
                await self.store.async_flush()
        self._mark_forced_refresh("resume_climate_planning")
        # A debounced request can return before refreshing during cooldown.
        # This explicit recovery action must await fresh plan evidence.
        await self.async_refresh()
        if not self.last_update_success or getattr(self, "last_refresh_metadata", {}).get("succeeded") is False:
            raise HomeAssistantError(
                "Could not refresh climate planning. Manual holds were cleared; retry when planner inputs recover."
            )
        # Refresh publishes the plan before its serialized device execution.
        # Report the resulting execution gates, not just the scheduled intent.
        execution = getattr(self, "_plan_execution_task", None)
        if execution is not None:
            await asyncio.shield(execution)
        from .diagnostics import climate_diagnostics

        return dict(climate_diagnostics(self.store.data, self.data, self.options)["preconditioning"])

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
                if preserve_main_state or preserve_zone_entity_id:
                    # An accepted listener event may have waited behind a
                    # command. Remove its stale restore targets before unload
                    # cleans up the remaining owned equipment.
                    self._remember_runtime_hvac_supersession(main=preserve_main_state, zone=preserve_zone_entity_id)
                    async with self._planner_lock:
                        try:
                            await self._async_store_manual_hvac_override(
                                duration_minutes, reason, source=source, expires=expires,
                            )
                            await self.store.async_flush()
                            await self._async_persist_runtime_hvac_supersessions()
                        except (OSError, HomeAssistantError):
                            self._hvac_handoff_failed = True
                            raise
                return None
            outcome, helper_error = await self._async_set_manual_hvac_override(
                duration_minutes,
                reason,
                source=source,
                expires=expires,
                preserve_zone_entity_id=preserve_zone_entity_id,
                preserve_main_state=preserve_main_state,
            )

        await self.async_request_refresh()
        if helper_error is not None:
            raise helper_error
        return outcome

    async def _async_set_manual_hvac_override(
        self,
        duration_minutes: int,
        reason: str,
        *,
        source: str = "service",
        expires: bool = True,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
        initial_handoff: _HvacHandoff | None = None,
    ) -> tuple[ActionOutcome | None, Exception | None]:
        """Set a manual HVAC override without refreshing under command ownership."""
        getattr(self.executor, "hvac_feedback_commands", []).clear()
        self._mark_forced_refresh("manual_hvac_override")
        helper_error: Exception | None = None
        release_outcome = None
        async with self._planner_lock:
            await self._async_store_manual_hvac_override(duration_minutes, reason, source=source, expires=expires)
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
            if initial_handoff is not None:
                await self._async_persist_hvac_supersessions(initial_handoff)
                if not initial_handoff.release:
                    return None, helper_error
            await self._async_persist_runtime_hvac_supersessions()
            release_hvac = getattr(self.executor, "async_release_hvac_control", None)
            if callable(release_hvac):
                release_options: dict[str, Any] = {}
                if preserve_zone_entity_id:
                    release_options["preserve_zone_entity_id"] = preserve_zone_entity_id
                if preserve_main_state:
                    release_options["preserve_main_state"] = True
                release_outcome = await release_hvac(reason, **release_options)
        return release_outcome, helper_error

    async def _async_store_manual_hvac_override(
        self, duration_minutes: int, reason: str, *, source: str, expires: bool,
    ) -> None:
        """Persist the operator hold under the planner lock."""
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
        ownership_expiry = (
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
        async def save_override() -> None:
            await self.store.async_save_overrides(self.overrides)
            ownership = dict(self.store.data.get("ownership", {}))
            ownership["manual_hvac_override_expires_at"] = ownership_expiry
            await self.store.async_save_ownership(ownership)

        try:
            await save_override()
        except (OSError, HomeAssistantError):
            await save_override()

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

    async def async_restore_safe_state(
        self, reason: str, *, refresh: bool = True, preserve_export_retry: bool = False,
    ) -> ActionOutcome:
        """Restore safe state and refresh."""
        async with self._command_lock:
            try:
                outcome = await self.executor.async_restore_safe_state(
                    reason, preserve_export_retry=preserve_export_retry,
                )
            finally:
                if getattr(self, "_failed_startup_restore_watch_cancel", None) is not None:
                    await startup_recovery._async_flush_failed_startup_restore(self)
                startup_recovery._retire_failed_startup_restore_watch(self)
        if refresh:
            self._mark_forced_refresh("safe_state_restored")
            await self.async_request_refresh()
        return outcome

    async def async_arm_production_control(
        self, reason: str = "user_acknowledged", *, provisional: bool = False,
        export_operator_arm: bool = False,
    ) -> None:
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
        if export_operator_arm:
            # A new process cannot know whether the authorizing process
            # finished rechecking the save. Require its own fresh preflight.
            production[_EXPORT_OPERATOR_ARM_REVALIDATE] = True
        else:
            production.pop(_EXPORT_OPERATOR_ARM_REVALIDATE, None)
        if provisional:
            if not export_operator_arm:
                self._export_startup_activation_pending = True
            production["startup_activation_pending"] = True
        else:
            production.pop("startup_activation_pending", None)
            recovery = production.get("startup_auto_recovery")
            if (isinstance(recovery, dict) and recovery.get("status") == "cancelled"
                    and recovery.get("last_reason") == "operator_disarmed"):
                production.pop("startup_auto_recovery")
        await self._async_save_production(production)
        if not provisional and not export_operator_arm:
            self._export_startup_activation_pending = False
        self._schedule_export_retry()
        self.async_update_listeners()

    async def async_operator_arm_production_control(
        self,
        reason: str = "user_acknowledged",
    ) -> None:
        """Apply an explicit operator arm only after current safety validation."""
        request = self._production_arm_request = object()
        recheck_export = self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True
        report = await self._async_operator_control_preflight()
        if self._production_arm_request is not request:
            return
        self._require_operator_arm_ready(report, "Production control")
        if await self._async_admit_operator_arm(
            request, reason, "Production control", recheck_export=recheck_export, take_over_startup=True,
        ):
            if recheck_export:
                await self.executor.async_dismiss_startup_recovery_notification()
            if self._production_arm_request is request:
                await self._async_compensate_ev_auto_start(require_unowned=False)

    async def async_set_active_control(self, enabled: bool) -> None:
        """Enable or safely return from automatic device control as one operation."""
        if enabled and self.active_control:
            return

        options = self.options
        options[CONF_PLANNER_ENABLED] = True
        if not enabled:
            self._production_arm_request = None
            # Guarded retries read the current options at the write boundary.
            # Revoke authority before startup cleanup can yield.
            options[CONF_DRY_RUN] = True
            self.hass.config_entries.async_update_entry(self.entry, options=options)
            await self.async_cancel_startup_auto_recovery("automatic_control_disabled")
            await self.async_handle_options_update()
            await self.async_disarm_production_control("automatic_control_disabled")
            return

        request = self._production_arm_request = object()
        options[CONF_DRY_RUN] = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        await self.async_handle_options_update()

        device_control_selected = any(
            strict_bool(self.options.get(key), default=False)
            for key in (
                CONF_EV_CONTROL_ENABLED,
                CONF_CLIMATE_CONTROL_ENABLED,
                CONF_ENPHASE_CONTROL_ENABLED,
                CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
            )
        )
        if not device_control_selected:
            raise HomeAssistantError(
                "Automatic control requires at least one selected device control area",
                translation_domain=DOMAIN,
                translation_key="active_control_no_device_selected",
            )

        recheck_export = self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True
        report = await self._async_operator_control_preflight()
        if self._production_arm_request is not request:
            return
        self._require_operator_arm_ready(report, "Automatic control")
        if not await self._async_admit_operator_arm(
            request, "automatic_control_enabled", "Automatic control", recheck_export=recheck_export,
        ):
            return
        options = self.options
        options[CONF_DRY_RUN] = False
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        await self.async_handle_options_update()
        await self._async_compensate_ev_auto_start(require_unowned=False)

    async def _async_admit_operator_arm(
        self, request: object, reason: str, operation: str, *, recheck_export: bool, take_over_startup: bool = False,
    ) -> bool:
        """Keep Export Limit command authority closed across the arming saves."""
        pending = getattr(self, "_export_operator_arm", None)
        if (pending is None and not recheck_export
                and self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is not True):
            if take_over_startup:
                await self.async_cancel_startup_auto_recovery("operator_armed", restore_owned_state=False)
                if self._production_arm_request is not request:
                    return False
            await self.async_arm_production_control(reason)
            return self._production_arm_request is request

        self._require_operator_arm_ready(build_preflight_report(self.hass, self), operation)
        # Only a successor that passed preflight can take over a tentative save.
        previous = (pending.previous_production if pending is not None
                    else parse_production_state(self.store.data.get("production")).raw)
        resume_startup = (pending.resume_startup_recovery if pending is not None
                          else getattr(self, "_startup_auto_recovery_authorized", False))
        admission = self._export_operator_arm = _ExportOperatorArm(previous, resume_startup)
        admitted = False
        try:
            if take_over_startup:
                await self.async_cancel_startup_auto_recovery(
                    "operator_armed", preserve_control=True, restore_owned_state=False,
                )
                if self._production_arm_request is not request:
                    return False
                self._require_operator_arm_ready(build_preflight_report(self.hass, self), operation)
            for provisional in (True, False):
                await self.async_arm_production_control(reason, provisional=provisional, export_operator_arm=True)
                await self.store.async_flush()
                if self._production_arm_request is not request:
                    return False
                self._require_operator_arm_ready(build_preflight_report(self.hass, self), operation)
            # The durable tentative arm passed its last admission check.
            # Record that decision, then independently admit the Export area.
            completed = parse_production_state(self.store.data.get("production")).raw
            completed[_EXPORT_OPERATOR_ARM_REVALIDATE] = _EXPORT_OPERATOR_ARM_COMPLETED
            recovery = completed.get("startup_auto_recovery")
            startup_suspended = take_over_startup or (resume_startup and not self._startup_auto_recovery_authorized)
            if (startup_suspended and isinstance(recovery, dict)
                    and recovery.get("status") in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES):
                now = dt_util.utcnow()
                completed["startup_auto_recovery"] = {
                    **recovery, "status": "cancelled", "last_reason": "operator_armed",
                    "updated_at": now, "completed_at": now,
                }
            await self._async_save_production(completed)
            await self.store.async_flush()
            if self._production_arm_request is not request:
                return False
            self._export_startup_activation_pending = False
            admitted = True
            self._export_operator_arm_admitted = False
            report = build_preflight_report(self.hass, self)
            self._export_operator_arm_admitted = (
                report["safe_to_activate_now"] and report["current_plan"]["export_limit_safe"]
            )
            return True
        finally:
            if self._export_operator_arm is admission and not admitted:
                try:
                    await self._async_finish_operator_arm_rollback(admission)
                finally:
                    if admission.rollback_pending and self._export_operator_arm is admission:
                        self._schedule_debounced_refresh(
                            "operator_arm_rollback", mode=_RefreshMode.IMMEDIATE, force=True,
                        )
            elif self._export_operator_arm is admission:
                self._export_operator_arm = None
                self._schedule_export_retry()
                self.async_update_listeners()

    async def _async_finish_operator_arm_rollback(self, admission: _ExportOperatorArm) -> None:
        """Release a rejected request only after restoring durable authorization."""
        admission.rollback_pending = False
        try:
            await self._async_rollback_operator_arm(admission.previous_production)
        except BaseException:
            admission.rollback_pending = True
            raise
        if self._export_operator_arm is admission:
            self._export_operator_arm = None
            if admission.resume_startup_recovery and not self._startup_auto_recovery_authorized:
                self._startup_auto_recovery_authorized = True
                self._export_retry_startup_blocked = True
                self.async_start_startup_auto_recovery()
            self._schedule_export_retry()
            self.async_update_listeners()

    async def _async_rollback_operator_arm(self, previous: dict[str, Any]) -> None:
        """Restore the prior authorization while preserving concurrent readiness evidence."""
        production = parse_production_state(self.store.data.get("production")).raw
        for key in ("armed", "armed_at", "armed_reason", "acknowledged_at", "startup_activation_pending",
                    "startup_auto_recovery", _EXPORT_OPERATOR_ARM_REVALIDATE):
            if key in previous:
                production[key] = previous[key]
            else:
                production.pop(key, None)
        await self._async_save_production(production)
        await self.store.async_flush()

    @staticmethod
    def _require_operator_arm_ready(report: dict[str, Any], operation: str) -> None:
        if not report.get("safe_to_activate_now"):
            reason = _active_control_not_ready_reason(report)
            raise HomeAssistantError(
                f"{operation} is not ready: {reason}",
                translation_domain=DOMAIN,
                translation_key="active_control_not_ready",
                translation_placeholders={"reason": reason},
            )

    async def _async_operator_control_preflight(self) -> dict[str, Any]:
        """Require fresh settled Export Limit evidence before operator activation."""
        if (
            self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True
            and (
                getattr(self, "data", None) is not None or getattr(self, "_export_reconcile_pending", False)
            )
        ):
            await self._async_refresh_with_export_preflight("automatic_control_preflight")
        return build_preflight_report(self.hass, self)

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
            CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED: EXPORT_ASSET,
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
            if option_key == CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED:
                report = build_preflight_report(self.hass, self, options_override=proposed_options,
                    export_limit_override=export_limit_evidence(
                        self.hass, self.entry_data, proposed_options, dt_util.utcnow()))
            else:
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

        if option_key == CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED:
            async with self._command_lock:
                await ExportLimitControl(self.hass, self.store, self.entry_data).resume()
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
                or not self._ev_command_guard()()
                or not self.active_control
                or not strict_bool(
                    self.options.get(CONF_EV_CONTROL_ENABLED),
                    default=False,
                )
            ):
                self._clear_ev_auto_start_compensation()
                return None
            await self._async_reconcile_vehicle_ownership()
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
            now = dt_util.utcnow()
            owned_state = self.store.data.get("ownership", {}).get("ev_smart_charging_state")
            if (
                (require_unowned or bool(owned_state))
                and _ev_start_feedback_is_expected(getattr(self, "executor", None), now)
            ) or self._ev_charging_matches_owned_plan(now):
                self._clear_ev_auto_start_compensation()
                return None
            if self.store.data.get("ownership", {}).get("ev_smart_charging_state") and any(
                o.kind == "manual_ev_charging" and o.reason == "charge_now"
                and o.expires_at is not None and dt_util.utcnow() < o.expires_at
                for o in self.overrides
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

    def _ev_charging_matches_owned_plan(self, now: datetime) -> bool:
        """Recognize delayed/recovered feedback for current owned charging intent."""
        ownership = self.store.data.get("ownership", {})
        if not ownership.get("ev_smart_charging_state"):
            return False
        if ownership.get("ev_smart_charging_control_topology") != _ev_control_topology(self.entry_data):
            return False
        charger_entity = self.entry_data.get(CONF_EV_CHARGER)
        charger_state = self.hass.states.get(charger_entity) if charger_entity else None
        if charger_state is None or ev_charging_state(charger_state.state) is not True:
            return False
        plan = getattr(self, "data", None)
        if not isinstance(plan, EnergyPlan) or plan.health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
            return False
        if plan.mode not in {PlannerMode.ACTIVE_HEALTHY, PlannerMode.ACTIVE_DEGRADED, PlannerMode.MANUAL_HVAC_OVERRIDE}:
            return False
        if set(plan.input_issues) & _EV_SAFETY_PLAN_ISSUES:
            return False
        if control_pause_reason(self.store.data.get("control_pause"), now, asset="ev") is not None:
            return False
        # Ownership alone can survive a disconnect or an unconfirmed stop.
        # Require the committed slot to still request power from this actuator.
        return any(
            action.asset == ActionAsset.EV and _ev_action_wants_power(action)
            and action.execute_not_before <= now < action.execute_not_after
            for action in plan.actions
        )

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

    async def async_disarm_production_control(
        self, reason: str = "user_requested", *, recovery_state: dict[str, Any] | None = None,
    ) -> None:
        """Disarm production control."""
        self._production_arm_request = None
        self._export_operator_arm = None
        self._export_operator_arm_admitted = False
        self._clear_ev_auto_start_compensation()
        production = parse_production_state(self.store.data.get("production")).raw
        if recovery_state is not None:
            production["startup_auto_recovery"] = recovery_state
        production.pop("startup_activation_pending", None)
        production.pop(_EXPORT_OPERATOR_ARM_REVALIDATE, None)
        production.update(
            {
                "armed": False,
                "disarmed_at": dt_util.utcnow(),
                "disarmed_reason": reason,
            }
        )
        try:
            async with self.store.async_delay_save():
                try:
                    await self._async_save_production(production)
                    if recovery_state is not None:
                        # Startup cleanup can still hold the command lock.
                        await self.store.async_flush()
                finally:
                    # Restore owned HVAC even if the disarm save fails.
                    async with self._command_lock:
                        ownership = self.store.data.get("ownership", {})
                        if (not self.hvac_handoff_unresolved
                            and isinstance(ownership, dict) and (
                                ownership.get("hvac_control")
                                or ownership.get("climate_automations")
                                or ownership.get("planner_takeover_started_at")
                                or ownership.get("planner_hvac_action_expires_at")
                            )
                        ):
                            await self.executor.async_release_hvac_control("production_control_disarmed")
        finally:
            self.async_update_listeners()

    async def async_operator_disarm_production_control(
        self,
        reason: str = "user_requested",
    ) -> None:
        """Cancel startup recovery and apply an explicit operator disarm."""
        self._production_arm_request = None
        self._operator_disarm_request = object()
        try:
            await self.async_cancel_startup_auto_recovery(
                "operator_disarmed", restore_owned_state=False, disarm_reason=reason,
            )
        finally:
            async with self._command_lock:
                if (not self.active_control
                        and self.store.data.get("ownership", {}).get(EXPORT_ASSET, {}).get("baseline")):
                    await self.executor.async_restore_device_control(EXPORT_ASSET, "production_control_disarmed")

    async def async_pause_control(self, duration_minutes: int, reason: str, asset: str = "all") -> None:
        """Pause planner-owned active control for all devices or one asset."""
        normalized_asset = asset if asset in {"all", "ev", "daikin", "enphase", EXPORT_ASSET} else "all"
        pause = {
            "active": True,
            "assets": ["all"] if normalized_asset == "all" else [normalized_asset],
            "until": dt_util.utcnow() + timedelta(minutes=duration_minutes),
            "reason": reason,
        }
        await self._async_save_control_pause(pause)
        self._schedule_export_retry()
        self._mark_forced_refresh("control_paused")
        await self.async_request_refresh()

    async def async_resume_control(self, reason: str = "user_requested", *, asset: str = "all") -> None:
        """Resume the selected area without clearing other areas' pauses."""
        now = dt_util.utcnow()
        saved_pause = self.store.data.get("control_pause")
        pause = dict(saved_pause) if isinstance(saved_pause, dict) else {}
        _, paused = partition_control_areas_by_pause(saved_pause, now, ["ev", "hvac", "enphase", EXPORT_ASSET])
        areas = ["daikin" if area == "hvac" else area for area in paused]
        remaining = [] if asset == "all" else [area for area in areas if area != asset]
        await self._async_save_control_pause({**pause, "active": bool(remaining), "assets": remaining,
            "resumed_at": now, "reason": reason})
        if asset in {"all", EXPORT_ASSET}:
            async with self._command_lock:
                await ExportLimitControl(self.hass, self.store, self.entry_data).resume()
            self._schedule_export_retry()
        self._mark_forced_refresh("control_resumed")
        await self.async_request_refresh()

    async def _async_update_production_evidence(
        self, plan: EnergyPlan, violations: list[str],
        configuration: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> None:
        """Track dry-run readiness evidence for the production gate."""
        entry_data, options = (self.entry_data, self.options) if configuration is None else configuration
        await self._async_save_production(self._production_evidence_for_plan(plan, violations, entry_data, options))

    def _production_evidence_for_plan(
        self, plan: EnergyPlan, violations: list[str], entry_data: dict[str, Any], options: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the readiness record for the active plan transaction."""
        production_state = parse_production_state(self.store.data.get("production"))
        production = production_state.raw
        # A provisional plan and its reconciled replacement are one review
        # cycle. Only the plan with settled export authority earns credit.
        if plan.mode == PlannerMode.DRY_RUN and not getattr(self, "_export_reconcile_pending", False):
            control_areas, _discovery = _runtime_control_area_report(
                self.hass,
                entry_data,
                options,
                plan=plan,
                pause=self.store.data.get("control_pause"),
                now=plan.created_at,
                export_ownership=self.store.data.get("ownership", {}).get(EXPORT_ASSET, {}),
            )
            eligible = control_areas.get("confidence_eligible", [])
            export_review_safe = area_safe(plan) and EXPORT_ASSET in eligible
            if export_review_safe or (
                control_areas["required"] != [EXPORT_ASSET]
                and plan.health in {InputHealth.HEALTHY, InputHealth.DEGRADED}
                and plan.status == "current"
                and (plan.health == InputHealth.HEALTHY or eligible)
                and not violations
            ):
                evidence_fingerprint = production_evidence_fingerprint(entry_data, options)
                ready_cycles = production_state.dry_run_ready_cycles
                if production.get("dry_run_evidence_fingerprint") != evidence_fingerprint:
                    ready_cycles = 0
                production["dry_run_evidence_fingerprint"] = evidence_fingerprint
                production["dry_run_ready_cycles"] = min(ready_cycles + 1, DRY_RUN_READY_CYCLES_REQUIRED)
                production["last_dry_run_ready_at"] = plan.created_at
                return production
        if plan.health not in {InputHealth.HEALTHY, InputHealth.DEGRADED}:
            production["last_blocking_reason"] = "input_health_unsafe"
        return production

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
            if not service_entity_available(self.hass, manual_override_entity):
                # Retain expiry and retry after the helper integration recovers.
                self._manual_override_helper_guard = None
                return False
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
        await self.store.async_add_dry_run_comparison(self._dry_run_comparison(plan))

    def _dry_run_comparison(self, plan: EnergyPlan) -> dict[str, Any]:
        """Capture the plan and recent outcomes together."""
        outcomes = list(self.store.data.get("execution_audit", []))
        return {
            "created_at": plan.created_at,
            "plan_id": plan.plan_id,
            "planned_action_count": len(plan.actions),
            "next_action": None if plan.next_action is None else _snapshot_action(plan.next_action),
            "estimated_daily_cost": plan.estimated_daily_cost,
            "recent_outcome_count": len(outcomes[-10:]),
            "recent_outcomes": outcomes[-5:],
        }

    @callback
    def _mark_replan_requested(self, *, force: bool = False) -> None:
        """Mark that a newer planner result is expected."""
        self._refresh_generation += 1
        if force:
            self._force_next_refresh = True

    @callback
    def _mark_forced_refresh(self, trigger: str) -> None:
        """Attribute and mark an immediate service-driven refresh."""
        self._attribute_forced_refresh(trigger)
        self._mark_replan_requested()

    @callback
    def _attribute_forced_refresh(self, trigger: str) -> None:
        """Attribute a forced refresh after its inputs have been invalidated."""
        self._pending_refresh_trigger = trigger
        self._refresh_request_timing = (trigger, perf_counter(), request_owner())
        self._increment_refresh_counter("requested")
        self._force_next_refresh = True

    def _discard_superseded_refresh(self) -> None:
        """Let the owning refresh queue one replacement after releasing its locks."""
        if (timing := current_timing()) is not None:
            timing.superseded = True
        elif self.data is not None and not getattr(self, "_tearing_down", False) and hasattr(self, "hass"):
            self._async_create_listener_task(self.async_refresh())

    def _plan_inputs_current(
        self, generation: int, mapped_entry_data: dict[str, Any] | None, tariffs_current: Callable[[], bool] | None,
        *, entry_options: dict[str, Any] | None = None,
    ) -> bool:
        """Check authority before admitting a plan computed from captured inputs."""
        if generation != self._refresh_generation or getattr(self, "_tearing_down", False):
            return False
        if not self._plan_input_snapshot_matches(mapped_entry_data, tariffs_current, entry_options=entry_options):
            self._mark_replan_requested(force=True)
            return False
        return True

    def _plan_input_snapshot_matches(
        self, mapped_entry_data: dict[str, Any] | None, tariffs_current: Callable[[], bool] | None,
        *, entry_options: dict[str, Any] | None = None,
    ) -> bool:
        """Recheck inputs that can change before an event advances the generation."""
        # Core may replace entry options or a subentry before its listener runs.
        # A producer can mutate nested tariff attributes without a state event.
        return (
            (entry_options is None or entry_options == self.entry.options)
            and (mapped_entry_data is None or mapped_entry_data == combined_entry_data(self.entry))
            and (tariffs_current is None or tariffs_current())
        )

    async def _async_commit_plan_if_current(
        self,
        started_generation: int,
        plan: EnergyPlan,
        context: Any,
        options: dict[str, Any],
        *,
        execute: bool = True,
        mapped_entry_data: dict[str, Any] | None = None,
        tariffs_current: Callable[[], bool] | None = None,
        entry_options: dict[str, Any] | None = None,
        record_evidence: Callable[[], None] | None = None,
    ) -> EnergyPlan | None:
        """Persist only the newest planner result and optionally execute it."""
        def is_current() -> bool:
            return self._plan_inputs_current(
                started_generation, mapped_entry_data, tariffs_current, entry_options=entry_options,
            )

        def stage_plan() -> EnergyPlan | None:
            # Calendar entities depend on the coordinator.
            from .calendar import calendar_event_records

            calendar_events = calendar_event_records(self, plan)
            # Resolving live vehicle input during presentation can invalidate
            # a result whose identity changed while the planner was running.
            # The Store rechecks the complete tariff snapshot after staging,
            # before any write can occur.
            if not self._plan_inputs_current(
                started_generation, mapped_entry_data, None, entry_options=entry_options,
            ):
                return None
            self.store.record_plan(plan, calendar_events=calendar_events)
            if record_evidence is not None:
                record_evidence()
            return plan

        result = await self.store.async_publish_plan(stage_plan, is_current)
        if result is None:
            self._discard_superseded_refresh()
            return self.data
        self._publish_decision_context(started_generation, plan, context)
        # Recovery validation is deliberately non-commanding, including
        # planner-owned safety-stop paths that may normally bypass arming.
        if getattr(self, "_startup_auto_recovery_validation_active", False):
            return plan
        if not execute:
            self._deferred_plan_execution = _PlanExecution(
                started_generation, plan, context, dict(options), mapped_entry_data, tariffs_current,
                entry_options=entry_options,
            )
            return plan
        await self._async_execute_plan_if_current(
            started_generation, plan, context, options,
            mapped_entry_data=mapped_entry_data, tariffs_current=tariffs_current, entry_options=entry_options,
        )
        return plan

    def _publish_decision_context(self, generation: int, plan: EnergyPlan, context: DecisionContext) -> None:
        """Expose only the acknowledged plan to freshness and recovery readers."""
        self._last_decision_context = context
        self._last_decision_generation = generation
        if (getattr(context, "ev_evidence", {}).get("load_recovery_completed")
                and plan.health == InputHealth.HEALTHY
                and (event := getattr(self, "_load_recovery_ready", None)) is not None):
            event.set()
            self._load_recovery_pending = False
        if getattr(self, "_startup_auto_recovery_validation_active", False):
            validation = getattr(self, "_last_startup_auto_recovery_validation", None)
            if isinstance(validation, dict) and validation.get("plan_id") == plan.plan_id:
                validation["committed"] = True

    async def _async_weather_forecast(
        self, entry_data: dict[str, Any], options: dict[str, Any], *, now: datetime, force: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await async_weather_forecast(self, entry_data, options, now=now, force=force)

    @callback
    def _schedule_plan_execution(self, request: _PlanExecution) -> None:
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
            eager_start=False,
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
                started_generation, plan = request.generation, request.plan
                async with self._command_lock:
                    if getattr(self, "_tearing_down", False):
                        continue
                    superseded = started_generation != self._refresh_generation
                    if superseded and not self._generation_changed_only_by_export_replan(started_generation):
                        continue
                    if superseded or getattr(self, "_export_reconcile_pending", False):
                        # Export readiness does not change other device actions.
                        # Even an empty action list lets the executor check for
                        # a planner-owned EV safety stop using current evidence.
                        plan = replace(plan, actions=[
                            action for action in plan.actions if action.asset != ActionAsset.ENPHASE_EXPORT_LIMIT
                        ])
                    try:
                        evaluated_current_plan = await self._async_execute_plan_if_current(
                            started_generation,
                            plan,
                            request.context,
                            request.options,
                            mapped_entry_data=request.mapped_entry_data,
                            tariffs_current=request.tariffs_current,
                            entry_options=request.entry_options,
                            verify_ev_capacity_completion=request.ev_capacity_request is not None,
                        )
                    except Exception:
                        # Isolate one malformed or failed execution transaction
                        # so a newer coalesced safety plan is not stranded.
                        _LOGGER.exception(
                            "Unexpected failure executing committed plan %s",
                            plan.plan_id,
                        )
                    else:
                        if evaluated_current_plan and not getattr(self, "_tearing_down", False):
                            self._acknowledge_ev_capacity_refresh(request.ev_capacity_request)
                    finally:
                        # Executor outcomes, ownership, reservations, pauses,
                        # and audit state are Store-backed rather than part of
                        # the earlier coordinator refresh result.
                        self.async_update_listeners()
        finally:
            self._plan_execution_task = None

    def _admit_export_after_restart(self, plan: EnergyPlan) -> None:
        """Admit Export after a restart or a mixed arm with Export still blocked."""
        if getattr(self, "data", None) is not plan:
            return
        if (not self._export_operator_arm_requires_preflight()
                or getattr(self, "_export_operator_arm", None) is not None
                or not getattr(self.hass, "is_running", True) or not self.active_control):
            return
        report = build_preflight_report(self.hass, self)
        if report["safe_to_activate_now"] and report["current_plan"]["export_limit_safe"]:
            self._export_operator_arm_admitted = True
            self._schedule_export_retry()

    def _generation_changed_only_by_export_replan(self, started_generation: int) -> bool:
        return (started_generation, self._refresh_generation) == getattr(self, "_export_replan_generations", None)

    async def _async_execute_plan_if_current(
        self,
        started_generation: int,
        plan: EnergyPlan,
        context: Any,
        options: dict[str, Any],
        *,
        mapped_entry_data: dict[str, Any] | None = None,
        tariffs_current: Callable[[], bool] | None = None,
        entry_options: dict[str, Any] | None = None,
        verify_ev_capacity_completion: bool = False,
    ) -> bool:
        """Execute a plan and report whether evaluation stayed current."""
        independent_of_export = all(action.asset != ActionAsset.ENPHASE_EXPORT_LIMIT for action in plan.actions)
        invalidated = False

        def has_current_generation() -> bool:
            return started_generation == self._refresh_generation or (
                independent_of_export and self._generation_changed_only_by_export_replan(started_generation)
            )

        def is_current() -> bool:
            if not has_current_generation():
                return False
            # Core can publish options while the entry listener is waiting for
            # command authority. Compare the plan to the live options until
            # the listener advances the generation.
            live_options = self.entry.options
            if (
                started_generation == self._refresh_generation
                and not getattr(self, "_export_reconcile_pending", False)
                and live_options == getattr(self, "_last_handled_options", live_options)
            ):
                return True
            return options == self.planner_options and has_current_generation()

        def inputs_current() -> bool:
            if not self._plan_input_snapshot_matches(mapped_entry_data, tariffs_current, entry_options=entry_options):
                self._schedule_debounced_refresh("execution_inputs_changed", force=True)
                return False
            return True

        def plan_is_current() -> bool:
            nonlocal invalidated
            current = not getattr(self, "_tearing_down", False) and is_current() and inputs_current()
            invalidated = invalidated or not current
            return current

        if getattr(self, "_tearing_down", False) or not is_current():
            return False
        await self._async_reconcile_vehicle_ownership()
        entry_data = self.entry_data
        if getattr(self, "_tearing_down", False) or not is_current() or not inputs_current():
            return False
        self._admit_export_after_restart(plan)
        if not self._automatic_hvac_command_allowed():
            # Evaluate other devices and implicit EV safety work while the
            # first-refresh plan's Daikin actions await the manual handoff.
            plan = replace(plan, actions=[action for action in plan.actions if action.asset != ActionAsset.DAIKIN])
        self.executor.options = options
        self.executor.entry_data = entry_data
        self.executor.hvac_command_guard = self._automatic_hvac_command_allowed
        last_observed: list[dict[str, str]] = []
        last_history: Any = None
        observed_until: datetime | None = None

        async def evaluate(action_plan: EnergyPlan) -> PlanAction | None:
            nonlocal last_observed, last_history, observed_until
            try:
                return await self.executor.async_evaluate(action_plan, context, plan_is_current=plan_is_current)
            finally:
                from .calendar import calendar_event_records

                self._schedule_export_retry()
                # Capture each completed transaction before later asynchronous
                # work can end its activity. Also retain confirmation on failure
                # or generation change without republishing an obsolete plan.
                observed = calendar_event_records(self, plan, confirmed_only=True)
                history = self.store.data.get("calendar_history")
                now = dt_util.utcnow()
                if observed and (observed != last_observed or history is not last_history
                                 or (observed_until is not None and now >= observed_until)):
                    last_history = await self.store.async_save_calendar_events(observed, now=now)
                    last_observed = observed
                    observed_until = min(
                        (end for record in observed if (end := datetime.fromisoformat(record["end"])) > now),
                        default=None,
                    )

        consumed_action = await evaluate(plan)
        evaluated_action = consumed_action if consumed_action in plan.actions else plan.next_action
        for action in plan.actions:
            if action is evaluated_action:
                continue
            if getattr(self, "_tearing_down", False):
                _LOGGER.debug(
                    "Stopping coordinated execution for plan %s while the config entry is unloading",
                    plan.plan_id,
                )
                return False
            if not is_current():
                _LOGGER.debug(
                    "Stopping coordinated execution for obsolete plan %s from generation %s; "
                    "current generation is %s",
                    plan.plan_id,
                    started_generation,
                    self._refresh_generation,
                )
                if hasattr(self, "hass"):
                    self._async_create_listener_task(self.async_request_refresh())
                return False
            if not inputs_current():
                return False
            # The priority score orders presentation and the first command, but
            # every coordinated device action keeps its own execution gate.
            await evaluate(replace(plan, actions=[action]))
        return not invalidated and (not verify_ev_capacity_completion or plan_is_current())

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
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain one start timestamp until household load becomes numeric again."""
    entity_id = str(entry_data.get(CONF_HOUSEHOLD_LOAD, "") or "").strip()
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    attributes = getattr(state, "attributes", {}) or {}
    unit = str(attributes.get("unit_of_measurement") or attributes.get("unit") or "")
    from .ev_resilience import recovering_load_source, stale_load_source_since

    numeric = state is not None and normalize_power_kw(getattr(state, "state", None), unit) is not None
    stale_since = stale_load_source_since(state, now, options or {}) if numeric else None
    if numeric and stale_since is None:
        return recovering_load_source(state, previous, entity_id, now, options)
    observed_state = "stale" if stale_since is not None else "missing" if state is None else str(state.state)
    sentinel_state = stale_since is not None or observed_state.lower() in {"unknown", "unavailable"}

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
            **({"ev_power_ceiling_kw": prior["ev_power_ceiling_kw"]} if "ev_power_ceiling_kw" in prior else {}),
            "entity_id": entity_id,
            "started_at": prior_started_at.isoformat(),
            "last_observed_state": observed_state,
            "fallback_eligible": fallback_eligible,
        }
        if invalid_reason:
            result["invalid_reason"] = invalid_reason
        return result

    state_changed_at = stale_since or getattr(state, "last_changed", None)
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
    entity_ids: set[str] = vehicle_entity_ids(entry_data)
    for mapping in entry_data.get("hvac_zone_mappings", {}).values():
        if isinstance(mapping, dict):
            entity_ids.update(value for value in mapping.values() if isinstance(value, str) and "." in value)
    for key in (*_DECISION_INPUT_ENTITY_KEYS, "hvac_arrival_entity", "hvac_humidity_entity",
                "hvac_irradiance_entity", "hvac_irradiance_forecast_entity"):
        for entity_id in _split_entity_values(entry_data.get(key)):
            entity_ids.add(entity_id)
    return sorted(entity_ids)


def _state_listener_targets(
    entry_data: dict[str, Any], hvac_control: dict[str, Any],
) -> tuple[list[str], frozenset[str]]:
    """Include owned zone actuators that still need manual supersession tracking."""
    from .daikin_memory import owned_control_data

    control_data = owned_control_data(entry_data, hvac_control)
    configured = dict.fromkeys([*_configured_entity_ids(entry_data), *_configured_entity_ids(control_data)])
    retained_zones = frozenset(hvac_control.get("zone_states", {})).difference(configured)
    return [*configured, *sorted(retained_zones)], retained_zones


def _decision_input_fingerprint(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    overrides: list[Override],
    *,
    now: datetime,
    weather_forecast: dict[str, Any] | None = None,
    tariff_fingerprints: dict[str, _TariffFingerprint] | None = None,
    previous_tariff_fingerprints: dict[str, _TariffFingerprint] | None = None,
    dry_hvac: CapabilityEvidence | None = None,
    dry_takeover_revision: int = 0,
    dry_during_provisional_off_start: bool = False,
) -> str | None:
    """Fingerprint decision state and optionally retain its tariff evidence."""
    interval_seconds = max(int(options.get(CONF_PLANNING_INTERVAL_MINUTES, 5)), 1) * 60
    price_timeout = _fingerprint_price_timeout(options)
    states: dict[str, Any] = {}
    export_entity = entry_data.get(CONF_AMBER_EXPORT_PRICE)
    tariffs = {entry_data.get(CONF_AMBER_IMPORT_PRICE), export_entity}
    try:
        for entity_id in _configured_entity_ids(entry_data):
            state = hass.states.get(entity_id)
            if entity_id in tariffs:
                fingerprint = _tariff_state_fingerprint(
                    state, now=now, price_timeout=price_timeout,
                    export_options=options if entity_id == export_entity else None,
                    previous=(previous_tariff_fingerprints or {}).get(entity_id),
                )
                if tariff_fingerprints is not None:
                    tariff_fingerprints[entity_id] = fingerprint
                states[entity_id] = fingerprint.digest
            else:
                states[entity_id] = _decision_entity_inputs(state)
    except _IncompleteTariffFingerprint:
        # The parser uses the complete source. A clipped tariff digest
        # cannot establish that a retained State still has the same rows.
        return None
    payload = {
        "interval_bucket": int(now.timestamp()) // interval_seconds,
        "states": states,
        "options": options,
        "overrides": to_jsonable(overrides),
        "weather_forecast": weather_forecast or {},
        "dry_takeover_revision": dry_takeover_revision,
        "dry_during_provisional_off_start": dry_during_provisional_off_start,
    }
    if dry_hvac is not None:
        payload["hvac_in_dry"] = dry_hvac
    return _digest_decision_inputs(payload)


def _decision_entity_inputs(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "state": getattr(state, "state", None),
        "last_updated": getattr(state, "last_updated", None),
        "attributes": {
            key: _bounded_material_value(value)
            for key, value in _forecast_input_attributes(getattr(state, "attributes", {}) or {}).items()
        },
    }


def _digest_decision_inputs(inputs: dict[str, Any] | None) -> str:
    encoded = json.dumps(to_jsonable(inputs), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fingerprint_price_timeout(options: dict[str, Any]) -> timedelta:
    minutes = options.get(CONF_PRICE_FRESHNESS_MINUTES, DEFAULT_OPTIONS[CONF_PRICE_FRESHNESS_MINUTES])
    return timedelta(minutes=int(minutes))


@dataclass(frozen=True, slots=True)
class _TariffFingerprint:
    evidence: dict[str, Any] | None
    attributes: dict[str, Any] | None
    digest: str

    def matches(self, evidence: dict[str, Any] | None, attributes: dict[str, Any]) -> bool:
        return (
            self.evidence == evidence and self.attributes is not None
            and tariff_snapshot_matches(attributes, self.attributes, acyclic_snapshot=True, strict=True)
        )


def _tariff_fingerprint_evidence(
    state: Any, *, now: datetime, price_timeout: timedelta, export_options: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if state is None:
        return None, {}
    attributes = getattr(state, "attributes", {}) or {}
    last_updated = getattr(state, "last_updated", None)
    evidence = {"state": getattr(state, "state", None), "last_updated": to_jsonable(last_updated)}
    if (updated_at := aware_time(last_updated)) is not None:
        evidence["freshness"] = _tariff_age_band(now - updated_at, price_timeout)
        if export_options is not None:
            issued_at = export_tariff_issued_at(attributes, updated_at)
            evidence["export_freshness"] = export_tariff_is_fresh(issued_at, now, export_options)
    return evidence, tariff_source_attributes(attributes)


def _tariff_state_fingerprint(
    state: Any, *, now: datetime, price_timeout: timedelta, export_options: dict[str, Any] | None,
    previous: _TariffFingerprint | None = None,
) -> _TariffFingerprint:
    evidence, attributes = _tariff_fingerprint_evidence(
        state, now=now, price_timeout=price_timeout, export_options=export_options,
    )
    if previous is not None and previous.matches(evidence, attributes):
        return previous
    digest = _digest_tariff_evidence(evidence, attributes)
    return _TariffFingerprint(evidence, cacheable_tariff_snapshot(attributes), digest)


def _digest_tariff_evidence(evidence: dict[str, Any] | None, attributes: dict[str, Any]) -> str:
    if evidence is None:
        return _digest_decision_inputs(None)
    budget = TariffSnapshotBudget()
    ordered_attributes = [
        [_bounded_material_value(key, budget=budget), _bounded_material_value(value, budget=budget)]
        for key, value in attributes.items()
    ]
    return _digest_decision_inputs({**evidence, "attributes": ordered_attributes})


def _tariff_fingerprints_current(
    hass: HomeAssistant, tariff_fingerprints: dict[str, _TariffFingerprint], *, now: datetime, options: dict[str, Any],
    export_entity: str | None,
) -> bool:
    """Match the early digest to the tariff evidence just confirmed for publication."""
    price_timeout = _fingerprint_price_timeout(options)
    for entity_id, fingerprint in tariff_fingerprints.items():
        evidence, attributes = _tariff_fingerprint_evidence(
            hass.states.get(entity_id), now=now, price_timeout=price_timeout,
            export_options=options if entity_id == export_entity else None,
        )
        if fingerprint.matches(evidence, attributes):
            continue
        # The digest can represent provider containers that the detached cache rejects.
        try:
            if _digest_tariff_evidence(evidence, attributes) != fingerprint.digest:
                return False
        except _IncompleteTariffFingerprint:
            return False
    return True


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


def _has_concrete_hvac_attribute_change(old: Any, new: Any, *, fallback: State | None = None) -> bool:
    """Require two known settings rather than disappearing target telemetry."""
    old_attributes, new_attributes = getattr(old, "attributes", None) or {}, new.attributes or {}
    fallback_attributes = getattr(fallback, "attributes", None) or {}
    for key in _HVAC_CONTROL_ATTRIBUTE_KEYS:
        before, after = old_attributes.get(key), new_attributes.get(key)
        baseline = fallback_attributes.get(key)
        if key in {"temperature", "target_temp_low", "target_temp_high"}:
            before, after, baseline = finite(before), finite(after), finite(baseline)
        if before in STATE_UNKNOWN_VALUES:
            before = baseline
        if before not in STATE_UNKNOWN_VALUES and after not in STATE_UNKNOWN_VALUES and before != after:
            return True
    return False


def _track_hvac_outage(
    event: Any, entity_id: str, outages: _HvacOutages, *, can_supersede: bool,
) -> _HvacOutageFeedback:
    """Keep the last observed controls and attribution through an outage."""
    old, new = event.data.get("old_state"), event.data.get("new_state")
    baseline, inherited = outages.baselines.get(entity_id, (None, set()))
    prior_context_ids = set(outages.prior_attributed_contexts.get(entity_id, ()))
    context_ids = inherited | prior_context_ids
    if hvac_feedback_is_observed(new):
        outages.baselines.pop(entity_id, None)
        manual = _is_explicit_hvac_recovery_change(event, baseline, context_ids) and can_supersede
        return _HvacOutageFeedback(baseline, context_ids, "manual_recovery" if manual else None)

    availability_context_ids = context_ids
    observed_before = old if hvac_feedback_is_observed(old) else None
    if observed_before is not None or entity_id not in outages.baselines:
        baseline = observed_before
        inherited = set()
        baseline_context_id = getattr(getattr(old, "context", None), "id", None)
        if baseline_context_id is not None:
            inherited.add(baseline_context_id)
            if observed_before is not None and (
                getattr(old.context, "user_id", None) or getattr(old.context, "parent_id", None)
            ):
                contexts = outages.prior_attributed_contexts.setdefault(entity_id, deque(maxlen=64))
                if baseline_context_id not in contexts:
                    contexts.append(baseline_context_id)
        # An attributed concrete edit on the loss event can still supersede
        # control. Only later reports inherit this loss event's context.
        availability_context_ids = inherited | prior_context_ids
        loss_context = getattr(event, "context", None)
        loss_context_id = getattr(loss_context, "id", None)
        if loss_context_id and not getattr(loss_context, "user_id", None):
            inherited.add(loss_context_id)
        outages.baselines[entity_id] = (baseline, inherited)
        context_ids = inherited | prior_context_ids

    unavailable_contexts = outages.unavailable_attributed_contexts.setdefault(entity_id, deque(maxlen=64))
    earlier_unavailable_context_ids = set(unavailable_contexts)
    availability_context_ids |= earlier_unavailable_context_ids
    assumed_exclusions = context_ids | earlier_unavailable_context_ids
    if getattr(new, "state", None) in STATE_UNKNOWN_VALUES:
        # Later assumed reports can inherit the context of any sentinel report.
        # This event still has a chance to prove a concrete edit, and observed
        # recovery continues to accept readback of previously unseen controls.
        unavailable_contexts.extend(_new_hvac_attributed_context_ids(event, assumed_exclusions))
    if can_supersede and hvac_feedback_is_assumed(new):
        previous_mode = getattr(old, "state", None)
        if previous_mode in STATE_UNKNOWN_VALUES and baseline is not None:
            previous_mode = baseline.state
        concrete_mode_change = (
            previous_mode not in STATE_UNKNOWN_VALUES and new.state not in STATE_UNKNOWN_VALUES
            and previous_mode != new.state
        )
        if (concrete_mode_change or _has_concrete_hvac_attribute_change(old, new, fallback=baseline)) and (
            _fresh_hvac_user_context_ids(event, assumed_exclusions)
        ):
            return _HvacOutageFeedback(baseline, availability_context_ids, "manual_optimistic")
    return _HvacOutageFeedback(baseline, availability_context_ids)


def _remember_hvac_manual_context(event: Any, entity_id: str, outages: _HvacOutages) -> None:
    """Exclude recent handled user and linked contexts across feedback flaps."""
    contexts = outages.prior_attributed_contexts.setdefault(entity_id, deque(maxlen=64))
    contexts.extend(_new_hvac_attributed_context_ids(event, contexts))


def _is_explicit_hvac_recovery_change(event: Any, baseline: State | None, excluded_ids: set[str]) -> bool:
    """Protect fresh user controls without inferring intent from a telemetry gap."""
    old, new = event.data.get("old_state"), event.data.get("new_state")
    if baseline is None or not hvac_feedback_is_observed(new) or hvac_feedback_is_observed(old):
        return False
    if not _fresh_hvac_user_context_ids(event, excluded_ids):
        return False
    return baseline.state != new.state or _has_concrete_hvac_attribute_change(baseline, new)


def _fresh_hvac_user_context_ids(event: Any, excluded_ids: Collection[str]) -> set[str]:
    """Attribute an intent only to a new user context, never inherited feedback."""
    return {
        context_id
        for context in (
            getattr(event, "context", None), getattr(event.data.get("new_state"), "context", None),
        )
        if (context_id := getattr(context, "id", None)) and context_id not in excluded_ids
        and getattr(context, "user_id", None)
    }


def _is_hvac_availability_feedback(event: Any, *, inherited_context_ids: set[str] | None = None) -> bool:
    """Separate lost/restored telemetry from evidence of a control change.

    Sentinel modes and disappearing/reappearing attributes cannot establish
    intent. An explicitly attributed change to two concrete control values can
    still supersede pending work during an outage; a context alone cannot.
    """
    old, new = event.data.get("old_state"), event.data.get("new_state")
    if old is None or new is None:
        return True
    if hvac_feedback_is_assumed(new) or hvac_feedback_is_assumed(old):
        return True
    if old.state not in STATE_UNKNOWN_VALUES and new.state not in STATE_UNKNOWN_VALUES:
        return False
    if not (_hvac_attributed_context_ids(event) - (inherited_context_ids or set())):
        return True
    return not _has_concrete_hvac_attribute_change(old, new)


def _hvac_attributed_context_ids(event: Any) -> set[str | None]:
    """Return the contexts carrying user or linked automation attribution."""
    new = event.data.get("new_state")
    return {
        getattr(context, "id", None)
        for context in (getattr(event, "context", None), getattr(new, "context", None))
        if getattr(context, "user_id", None) or getattr(context, "parent_id", None)
    }


def _new_hvac_attributed_context_ids(event: Any, excluded_ids: Collection[str]) -> set[str]:
    """Keep concrete IDs for deduplicating user and linked commands."""
    return {context_id for context_id in _hvac_attributed_context_ids(event)
            if context_id and context_id not in excluded_ids}


def _is_first_refresh_hvac_edit(event: Any, excluded_context_ids: set[str]) -> bool:
    """Require a fresh attributed control edit before handoff during setup."""
    if _is_hvac_availability_feedback(event, inherited_context_ids=excluded_context_ids):
        return False
    old, new = event.data["old_state"], event.data["new_state"]
    if hvac_feedback_is_observed(old):
        old_context_id = getattr(getattr(old, "context", None), "id", None)
        if old_context_id is not None:
            excluded_context_ids = excluded_context_ids | {old_context_id}
    return bool(_fresh_hvac_user_context_ids(event, excluded_context_ids)) and (
        old.state != new.state or _has_concrete_hvac_attribute_change(old, new)
    )


def _is_explicit_startup_hvac_change(event: Any) -> bool:
    """Require user attribution and a material control change during HA startup."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None or _is_hvac_availability_feedback(event):
        return False
    if not any(
        getattr(context, "user_id", None)
        for context in (getattr(event, "context", None), getattr(new_state, "context", None))
    ):
        return False
    old_attributes = getattr(old_state, "attributes", {}) or {}
    new_attributes = getattr(new_state, "attributes", {}) or {}
    return old_state.state != new_state.state or (
        str(event.data.get("entity_id", "")).startswith("climate.")
        and any(old_attributes.get(key) != new_attributes.get(key) for key in _HVAC_CONTROL_ATTRIBUTE_KEYS)
    )


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
    if old_state is None or new_state is None or _is_hvac_availability_feedback(event):
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
    if guard_entity and not store_data.get("ownership", {}).get("hvac_control"):
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
    if old_state is None or new_state is None or _is_hvac_availability_feedback(event):
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
    return True


def _matches_confirmed_zone_feedback(
    entry_data: dict[str, Any], command: dict[str, Any], event: Any, now: datetime,
    hass: HomeAssistant | None, changed: set[str],
) -> bool:
    """Verify bounded zone settling against captured targets and current readback."""
    issued = _parse_datetime_or_none(command.get("issued_at"))
    confirmed = _parse_datetime_or_none(command.get("confirmed_at"))
    deadline = _parse_datetime_or_none(command.get("deadline"))
    if (
        hass is None or issued is None or confirmed is None or deadline is None
        or command.get("entity_id") != entry_data.get(CONF_DAIKIN_CLIMATE)
        or not issued <= confirmed <= now < min(
            deadline, confirmed + timedelta(seconds=HVAC_ZONE_SETTLE_SECONDS),
            issued + timedelta(seconds=HVAC_ZONE_SETTLE_MAX_SECONDS),
        )
        or changed - {"temperature"}
    ):
        return False
    old, new = event.data["old_state"], event.data["new_state"]
    entity = event.data["entity_id"]
    head = hass.states.get(command["entity_id"])
    current = hass.states.get(entity)
    target = finite(command.get("requested", {}).get("temperature"))
    mode = command.get("requested_hvac_mode") or command.get("confirmed_mode")
    # Only related, confirmed activations from this transaction may explain
    # a delayed mode transition or carry its parent context past the short cache.
    activations: list[dict[str, Any]] = []
    for activation in command.get("mode_commands", []):
        if not (
            activation.get("entity_id") in {entity, command["entity_id"]}
            or _unambiguous_coupled_zone_entity_ids_match(entry_data, activation.get("entity_id"), entity)
        ):
            continue
        if (
            activation.get("transaction_id") != command.get("transaction_id")
            or activation.get("stage") == "service_failed"
            or activation.get("service") == "turn_off"
            or activation.get("service") == "set_hvac_mode" and activation.get("requested", {}).get("hvac_mode") != mode
            or activation.get("service") == "turn_on" and activation.get("confirmed_mode") not in {mode, None}
        ):
            # A later zone/alias off command also supersedes older head-on proof.
            # Cross-transaction commands may retire proof, never create it here.
            activations.clear()
        elif (
            activation.get("confirmed_at") is not None
            and activation["issued_at"] <= activation["confirmed_at"] <= now
        ):
            activations.append(activation)
    parents = {None, command["context_id"], *(activation["context_id"] for activation in activations)}
    # Reject queued events superseded by a newer state, or a changed head setting.
    if (
        current is None or hvac_feedback_is_assumed(current) or current.state != new.state
        or not hvac_feedback_is_observed(head) or target is None
        or any(current.attributes.get(key) != new.attributes.get(key) for key in _HVAC_CONTROL_ATTRIBUTE_KEYS)
        or mode not in _ACTIVE_HVAC_MODES or head.state != mode
        or not _matching_hvac_target(head.attributes.get("temperature"), target)
        or any(
            getattr(context, "user_id", None) or getattr(context, "parent_id", None) not in parents
            for context in (
                getattr(event, "context", None), getattr(new, "context", None),
                getattr(current, "context", None), getattr(head, "context", None),
            )
        )
    ):
        return False
    snapshot = command.get("coupled_zones", {}).get(entity, {})
    baseline = command.get("zone_targets", {}).get(entity, {})
    previous = finite(old.attributes.get("temperature"))
    if previous is None:
        previous = finite(snapshot.get("target_temperature"))
        if previous is None:
            previous = finite(baseline.get("target_temperature"))
    if previous is None:
        return False
    old_mode = old.state if old.state in _ACTIVE_HVAC_MODES or old.state == "off" else snapshot.get("mode")
    if old_mode != mode and not activations:
        # A temperature command alone cannot activate an off zone.
        return False
    mode_pending = bool(activations and new.state == old.state == snapshot.get("mode"))
    if new.attributes.get("temperature") is None:
        # Missing telemetry is not a new setting. Preserve the bounded evidence
        # for a subsequent exact recovery; never extend it on missing samples.
        return new.state in {mode, *STATE_UNKNOWN_VALUES} or mode_pending
    if new.state in STATE_UNKNOWN_VALUES:
        return any(_matching_hvac_target(new.attributes.get("temperature"), expected) for expected in (
            previous, min(max(previous, target - 2), target + 2),
        ))
    if old.state != new.state and not changed:
        return bool(new.state == mode)
    return (new.state == mode or mode_pending) and _matching_hvac_target(
        new.attributes.get("temperature"), min(max(previous, target - 2), target + 2),
    )


def _matches_disabled_zone_mode_recall(
    entry_data: dict[str, Any], command: dict[str, Any], later_commands: list[dict[str, Any]],
    event: Any, now: datetime, hass: HomeAssistant | None, changed: set[str],
) -> bool:
    """Recognize one bounded off-zone recall caused by an owned head restoration.

    Mode-specific remembered targets are not rollback baselines. This evidence
    lives only on the issued command and cannot renew its original deadline.
    """
    issued = _parse_datetime_or_none(command.get("issued_at"))
    deadline = _parse_datetime_or_none(command.get("deadline"))
    mode = command.get("requested", {}).get("hvac_mode")
    previous_mode = command.get("observed", {}).get("hvac_mode")
    if (
        hass is None or issued is None or deadline is None
        or not issued <= now < min(deadline, issued + timedelta(seconds=120))
        or command.get("entity_id") != entry_data.get(CONF_DAIKIN_CLIMATE)
        or command.get("service") != "set_hvac_mode" or not command.get("transaction_id")
        or command.get("restoration") is not True or command.get("uncertainty_compensation")
        or command.get("stage") == "service_failed"
        or mode not in {"heat", "cool"} or previous_mode not in {"heat", "cool"} or mode == previous_mode
        or changed != {"temperature"}
    ):
        return False
    entity = event.data["entity_id"]
    old, new = event.data["old_state"], event.data["new_state"]
    captured = command.get("coupled_zones", {}).get(entity, {})
    previous, target = finite(captured.get("target_temperature")), finite(new.attributes.get("temperature"))
    if (
        old.state != "off" or new.state != "off" or captured.get("mode") != "off"
        or previous is None or target is None
        or not _matching_hvac_target(old.attributes.get("temperature"), previous)
        or any(
            later_entity in {entity, command["entity_id"]}
            or _unambiguous_coupled_zone_entity_ids_match(entry_data, later_entity, entity)
            for later_entity in [
                *(later.get("entity_id") for later in later_commands),
                *command.get("recall_superseding_entities", []),
            ]
        )
    ):
        return False
    head, current = hass.states.get(command["entity_id"]), hass.states.get(entity)
    center = finite(head.attributes.get("temperature")) if hvac_feedback_is_observed(head) else None
    if (
        head is None or center is None or head.state != mode or not center - 2 <= target <= center + 2
        or not hvac_feedback_is_observed(current) or current.state != new.state
        or any(current.attributes.get(key) != new.attributes.get(key) for key in _HVAC_CONTROL_ATTRIBUTE_KEYS)
        or (command.get("confirmed_at") is not None and not _matching_hvac_target(
            center, command.get("confirmed_observed", {}).get("target_temperature"),
        ))
        or any(
            getattr(context, "user_id", None)
            or getattr(context, "parent_id", None) not in {None, command.get("context_id")}
            for context in (
                getattr(event, "context", None), getattr(new, "context", None),
                getattr(current, "context", None), getattr(head, "context", None),
            )
        )
    ):
        return False
    # Repeated samples may confirm the same recall; a second target is a new
    # intervention even if it still falls inside the head's supported bounds.
    recalled = command.setdefault("disabled_zone_recalls", {})
    if entity in recalled and not _matching_hvac_target(target, recalled[entity]):
        return False
    recalled[entity] = target
    return True


def _matches_climate_transaction(
    entry_data: dict[str, Any], commands: list[dict[str, Any]], event: Any, now: datetime,
    hass: HomeAssistant | None = None,
) -> bool:
    """Match only issued commands and fixture-supported coupled transitions."""
    old, new = event.data.get("old_state"), event.data.get("new_state")
    contexts = (getattr(event, "context", None), getattr(new, "context", None))
    if old is None or new is None or any(getattr(context, "user_id", None) for context in contexts):
        return False
    old_attributes, new_attributes = old.attributes or {}, new.attributes or {}
    changed = {key for key in _HVAC_CONTROL_ATTRIBUTE_KEYS if old_attributes.get(key) != new_attributes.get(key)}
    if changed - {"temperature", "target_temp_low", "target_temp_high"}:
        return False
    entity = event.data.get("entity_id")
    for command in reversed(commands):
        issued = _parse_datetime_or_none(command.get("issued_at"))
        deadline = _parse_datetime_or_none(command.get("deadline"))
        if issued is None or deadline is None or now < issued:
            continue
        same = entity == command.get("entity_id")
        activation = command.get("off_start_head_activation") or {}
        from .zone_control import topology
        head = hass.states.get(str(entry_data.get(CONF_DAIKIN_CLIMATE))) if hass is not None else None
        if not hvac_feedback_is_observed(head):
            head = None
        activation_valid = (
            activation.get("entity_id") == entry_data.get(CONF_DAIKIN_CLIMATE)
            and command.get("entity_id") in topology(entry_data).get("pairs", {})
            and command.get("service") == "turn_on" and command.get("main_mode") == "off"
            and activation.get("hvac_mode") in {"heat", "cool", "dry"}
            and now < min(deadline, issued + timedelta(seconds=120))
            and head is not None and head.state in {"off", activation.get("hvac_mode")}
            and _matching_hvac_target(head.attributes.get("temperature"), activation.get("target_temperature"))
            and not any(later.get("entity_id") == entry_data.get(CONF_DAIKIN_CLIMATE)
                        for later in commands[commands.index(command) + 1:])
        )
        coupled = (
            command.get("entity_id") == entry_data.get(CONF_DAIKIN_CLIMATE)
            and entity in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
            and str(entity).startswith("climate.")
        ) or _unambiguous_coupled_zone_entity_ids_match(entry_data, command.get("entity_id"), entity) or (
            activation_valid and entity in activation.get("zone_climates", []) and old.state == "off"
        )
        if not same and not coupled:
            # A declared zone can wake an Off AirBase head. Its fresh vendor
            # mode/target proof authorizes only this exact issued activation.
            if (activation_valid and entity == activation["entity_id"]
                    and old.state == "off" and new.state == activation["hvac_mode"]
                    and _matching_hvac_target(new_attributes.get("temperature"), activation["target_temperature"])
                    and all(getattr(context, "parent_id", None) in {None, command["context_id"]}
                            for context in contexts)):
                return True
            continue
        requested, service = command.get("requested", {}), command.get("service")
        if (coupled and not same and entity in command.get("off_start_dry_zones", [])
                and service == "set_hvac_mode" and requested.get("hvac_mode") == "dry"
                and now < min(deadline, issued + timedelta(seconds=120))
                and old.state in {"off", "heat", "cool"} and new.state in {"off", "dry"}
                and new_attributes.get("temperature") is None
                and all(new_attributes.get(key) is None for key in changed)
                and all(getattr(context, "parent_id", None) in {None, command["context_id"]}
                        for context in contexts)
                and head is not None and head.state == "dry"):
            return True
        if coupled and not same and _matches_disabled_zone_mode_recall(
            entry_data, command, commands[commands.index(command) + 1:], event, now, hass, changed,
        ):
            return True
        delayed_or_recovering = (
            now >= issued + timedelta(minutes=2)
            or old.state != new.state
            or finite(old_attributes.get("temperature")) is None
            or finite(new_attributes.get("temperature")) is None
        )
        if coupled and not same and service == "set_temperature" and delayed_or_recovering:
            if _matches_confirmed_zone_feedback(entry_data, command, event, now, hass, changed):
                return True
            continue
        if any(getattr(context, "parent_id", None) not in {None, command["context_id"]} for context in contexts):
            continue
        direct_window = (
            HVAC_ZONE_SETTLE_MAX_SECONDS
            if command.get("reconciling") and command.get("confirmed_at") is None else 120
        )
        if now >= min(deadline, issued + timedelta(seconds=direct_window)):
            continue
        expected_mode = "off" if service == "turn_off" else requested.get("hvac_mode")
        if service == "turn_on":
            mode_matches = old.state == "off" and (
                new.state in _ACTIVE_HVAC_MODES if entity.startswith("climate.") else new.state == "on"
            )
        elif expected_mode is not None:
            mode_matches = new.state == expected_mode
        else:
            mode_matches = old.state == new.state
        if service == "turn_on" and coupled and command.get("entity_id") != entry_data.get(CONF_DAIKIN_CLIMATE):
            mode = command.get("main_mode")
            expected_active_mode = (mode if mode in _ACTIVE_HVAC_MODES else activation["hvac_mode"]
                                    if activation_valid else command.get("requested_hvac_mode"))
            mode_matches = mode_matches and new.state == expected_active_mode
        if not mode_matches:
            continue
        zone_target = (
            command.get("zone_targets", {}).get(entity) or command.get("coupled_zones", {}).get(entity, {})
            if coupled
            else command.get("observed", {})
        )
        targets = {
            "temperature": requested.get("temperature"),
            "target_temp_low": requested.get("target_temp_low"),
            "target_temp_high": requested.get("target_temp_high"),
        }
        if service in {"turn_on", "set_hvac_mode"}:
            source = zone_target if isinstance(zone_target, dict) else command.get("observed", {})
            targets = {
                "temperature": source.get("target_temperature"),
                "target_temp_low": source.get("target_temp_low"),
                "target_temp_high": source.get("target_temp_high"),
            }
            # Daikin restores a mode-specific remembered target before the
            # separate temperature command. It need not equal the old mode's
            # target. Admit only this short mode transition within device bounds;
            # later target changes and user/external contexts remain overrides.
            bounds = command.get("target_bounds", {})
            lower, upper = finite(bounds.get("min_temp")), finite(bounds.get("max_temp"))
            if coupled and hass is not None:
                center = finite(head.attributes.get("temperature")) if head is not None else None
                if head is not None and head.state == new.state and center is not None:
                    lower, upper = center - 2, center + 2
                else:
                    lower, upper = None, None
            value = finite(new_attributes.get("temperature"))
            if (
                old.state != new.state and changed == {"temperature"}
                and now < min(deadline, issued + timedelta(seconds=HVAC_ZONE_SETTLE_MAX_SECONDS))
                and lower is not None and upper is not None and value is not None and lower <= value <= upper
                and not any(
                    later.get("service") == "set_temperature" and later.get("entity_id") == command.get("entity_id")
                    and later.get("issued_at", now) >= issued for later in commands
                )
            ):
                targets["temperature"] = value
        if service == "set_temperature" and coupled:
            # A head command only authorizes the zone's required bounds clamp.
            # A compatible zone target does not become the requested head target.
            targets = {key: old_attributes.get(key) for key in targets}
            head_target, previous = finite(requested.get("temperature")), finite(old_attributes.get("temperature"))
            if head_target is not None and previous is not None:
                clamped = min(max(previous, head_target - 2), head_target + 2)
                if clamped != previous:
                    targets["temperature"] = clamped
        if service == "turn_on" and coupled and old.state == "off":
            snapshot = command.get("coupled_zones", {}).get(entity, {})
            center = finite(command.get("main_target"))
            lower = finite(snapshot.get("min_temp"))
            upper = finite(snapshot.get("max_temp"))
            lower = lower if lower is not None else center - 2 if center is not None else None
            upper = upper if upper is not None else center + 2 if center is not None else None
            for key in changed:
                value = finite(new_attributes.get(key))
                if (
                    old_attributes.get(key) is None
                    and value is not None
                    and lower is not None
                    and upper is not None
                    and lower <= value <= upper
                ):
                    targets[key] = value
        if service == "turn_off" and coupled:
            targets = {key: None for key in changed}
        if all(
            (new_attributes.get(key) is None and targets.get(key) is None)
            or _matching_hvac_target(new_attributes.get(key), targets.get(key))
            for key in changed
        ):
            return True
    return False


def _is_planner_owned_control_feedback(
    entry_data: dict[str, Any],
    store_data: dict[str, Any],
    event: Any,
    now: datetime,
    *,
    pending_hvac_desired_state: dict[str, Any] | None = None,
    feedback_commands: list[dict[str, Any]] | None = None,
    hass: HomeAssistant | None = None,
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
    if asset in {"daikin", "daikin_zone"} and any(
        getattr(context, "user_id", None)
        for context in (getattr(event, "context", None), getattr(new_state, "context", None))
    ):
        return False
    if asset in {"daikin", "daikin_zone"}:
        commands = (pending_hvac_desired_state or {}).get("feedback_commands", feedback_commands or [])
        if feedback_commands is not None or commands or (pending_hvac_desired_state or {}).get("transaction_id"):
            # A live executor's empty cache is authoritative retirement, not
            # permission to revive an older outcome's target expectation.
            return _matches_climate_transaction(entry_data, commands, event, now, hass)
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
    if old_state is None or new_state is None or _is_hvac_availability_feedback(event):
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
    if expected_state in _ACTIVE_HVAC_MODES:
        # The explicit main mode call can also update subordinate climates.
        # Keep this exception limited to deferred zones and unattributed or
        # command-linked feedback during that call/confirmation window.
        event_context = getattr(event, "context", None)
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if (
            not expected_context_id
            or expected.get("actuator_entity_id") != entry_data.get(CONF_DAIKIN_CLIMATE)
            or expected_state != pending.get("hvac_mode")
            or event.data.get("entity_id") not in pending.get("deferred_zone_entities", [])
            or event.data.get("entity_id") not in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
            or event_context is None
            or getattr(event_context, "user_id", None) is not None
            or getattr(event_context, "parent_id", None) not in {None, expected_context_id}
            or old_state is None
            or new_state is None
            or old_state.state not in _ACTIVE_HVAC_MODES
            or old_state.state == new_state.state
            or new_state.state != expected_state
        ):
            return False
        old_attributes = getattr(old_state, "attributes", {}) or {}
        new_attributes = getattr(new_state, "attributes", {}) or {}
        changed = {key for key in _HVAC_CONTROL_ATTRIBUTE_KEYS if old_attributes.get(key) != new_attributes.get(key)}
        return changed <= {"temperature", "target_temp_low", "target_temp_high"} and all(
            _matching_hvac_target(new_attributes.get(key), new_attributes.get(key)) for key in changed
        )
    if expected_state not in {"on", "off"} or not expected_context_id:
        return False
    event_context = getattr(event, "context", None)
    context_matches = expected_context_id in {
        getattr(event_context, "id", None),
        getattr(event_context, "parent_id", None),
    }
    if expected_state == "off" and expected.get("actuator_entity_id") == entry_data.get(CONF_DAIKIN_CLIMATE):
        # The main shutdown also switches configured zone climates off and can
        # remove their targets. Accept only that transition during the command,
        # including fresh unattributed Daikin feedback; preserve manual changes.
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if (
            event.data.get("entity_id") not in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
            or event_context is None
            or getattr(event_context, "user_id", None) is not None
            or getattr(event_context, "parent_id", None) not in {None, expected_context_id}
            or old_state is None
            or new_state is None
            or old_state.state not in _ACTIVE_HVAC_MODES
            or new_state.state != "off"
        ):
            return False
        old_attributes = getattr(old_state, "attributes", {}) or {}
        new_attributes = getattr(new_state, "attributes", {}) or {}
        changed = {key for key in _HVAC_CONTROL_ATTRIBUTE_KEYS if old_attributes.get(key) != new_attributes.get(key)}
        return changed <= {"temperature", "target_temp_low", "target_temp_high"} and all(
            new_attributes.get(key) is None for key in changed
        )
    entity_pair_matches = _unambiguous_coupled_zone_entity_ids_match(
        entry_data,
        expected.get("actuator_entity_id"),
        event.data.get("entity_id"),
    )
    # Some Daikin integrations publish subordinate states with fresh contexts.
    # Only the configured main unit's explicit turn-on phase may explain a
    # deferred zone waking up without command ancestry.
    main_startup_matches = (
        expected.get("actuator_entity_id") == entry_data.get(CONF_DAIKIN_CLIMATE)
        and pending.get("turn_on_feedback_expected") is True
        and expected_state == "on"
        and event.data.get("entity_id") in pending.get("deferred_zone_entities", [])
        and event.data.get("entity_id") in _split_entity_values(entry_data.get(CONF_CLIMATE_ZONES))
    )
    unlinked_feedback = (
        event_context is not None
        and getattr(event_context, "user_id", None) is None
        and getattr(event_context, "parent_id", None) is None
        and (entity_pair_matches or main_startup_matches)
    )
    if not context_matches and not unlinked_feedback:
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
    changed_attributes = {
        attribute for attribute in _HVAC_CONTROL_ATTRIBUTE_KEYS
        if old_attributes.get(attribute) != new_attributes.get(attribute)
    }
    if (
        (
            context_matches
            or (unlinked_feedback and (main_startup_matches or new_mode == pending.get("hvac_mode")))
        )
        and expected_state == "on"
        and event.data.get("entity_id") in pending.get("deferred_zone_entities", [])
        and zone_temperature_sync_deferred(old_state)
        and bool(changed_attributes)
        and changed_attributes <= {"temperature", "target_temp_low", "target_temp_high"}
    ):
        # Main turn-on may restore a remembered mode before set_hvac_mode.
        # Its deferred zones may likewise recover their previous targets.
        # Accept that bounded off-to-active transition, not later mode changes.
        # A linked off zone may recover its previous target when our command
        # wakes the unit. It was excluded from target writes before persistence.
        return all(
            _matching_hvac_target(new_attributes.get(attribute), new_attributes.get(attribute))
            for attribute in changed_attributes
        )
    if expected_state == "off" and entity_pair_matches:
        return changed_attributes <= {"temperature", "target_temp_low", "target_temp_high"} and all(
            new_attributes.get(key) is None for key in changed_attributes
        )
    return not changed_attributes and (context_matches or entity_pair_matches)


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
    from .zone_control import topology
    explicit = topology(entry_data)
    if explicit.get("supported"):
        return bool(explicit["pairs"].get(str(actuator_entity_id)) == str(climate_entity_id))
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
    if old_state is None or new_state is None or _is_hvac_availability_feedback(event):
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
    expected_power = pending.get("zone_power", {}).get(entity_id, "on")
    return None if str(new_state.state).lower() == expected_power else entity_id


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
        except (TypeError, ValueError, OverflowError):
            return False
        matched_command_change = True
    return matched_command_change


def _is_material_state_change(event: Any, options: dict[str, Any], *, tariff: bool = False) -> bool:
    """Return whether a state-change event should trigger replanning."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return True
    old_value = getattr(old_state, "state", None)
    new_value = getattr(new_state, "state", None)
    if _material_attributes_changed(old_state, new_state, tariff=tariff):
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


def _material_attributes_changed(old_state: Any, new_state: Any, *, tariff: bool = False) -> bool:
    """Return whether an input attribute consumed by planning changed."""
    old_attributes = _forecast_input_attributes(getattr(old_state, "attributes", {}) or {}, tariff=tariff)
    new_attributes = _forecast_input_attributes(getattr(new_state, "attributes", {}) or {}, tariff=tariff)
    if tariff:
        return not tariff_snapshot_matches(new_attributes, old_attributes)
    return old_attributes != new_attributes


class _IncompleteTariffFingerprint(Exception):
    """The bounded fingerprint cannot represent the exact tariff evidence."""


def _bounded_material_value(value: Any, *, depth: int = 0, budget: TariffSnapshotBudget | None = None) -> Any:
    """Bound nested containers and preserve tariff alias order through JSON sorting."""
    ordered = budget is not None
    if budget is not None and not budget.consume(value):
        raise _IncompleteTariffFingerprint
    if depth >= 6:
        if ordered:
            raise _IncompleteTariffFingerprint
        return None
    if isinstance(value, list) or (ordered and isinstance(value, tuple)):
        if ordered and len(value) > MAX_FORECAST_SOURCE_ROWS:
            raise _IncompleteTariffFingerprint
        items = [
            _bounded_material_value(item, depth=depth + 1, budget=budget)
            for item in value[:MAX_FORECAST_SOURCE_ROWS]
        ]
        return [type(value).__name__, items] if ordered else items
    if isinstance(value, dict):
        if ordered:
            fields = list(islice(tariff_mapping_items(value, nested=True), MAX_FORECAST_SOURCE_ROWS + 1))
            if len(fields) > MAX_FORECAST_SOURCE_ROWS:
                raise _IncompleteTariffFingerprint
            items = [
                [_bounded_material_value(key, depth=depth + 1, budget=budget),
                 _bounded_material_value(item, depth=depth + 1, budget=budget)]
                for key, item in fields
            ]
            return ["dict", items]
        return {
            str(key): _bounded_material_value(item, depth=depth + 1)
            for key, item in islice(value.items(), MAX_FORECAST_SOURCE_ROWS)
        }
    if not ordered and type(value) is int and value.bit_length() > 1024:
        # Oversized numeric attributes cannot provide usable climate evidence.
        return None
    if ordered and value is not None:
        if type(value) is datetime:
            return ["datetime", value.isoformat()]
        if type(value) not in (str, bool, int, float):
            # JSON can erase subclass identity that native tariff validation requires.
            raise _IncompleteTariffFingerprint
    return value


def _forecast_input_attributes(attributes: Any, *, tariff: bool = False) -> dict[str, Any]:
    """Track raw spellings because the parser can read both forms independently."""
    if tariff:
        return tariff_source_attributes(attributes)
    return {
        key: value
        for key, value in attributes.items()
        if _canonical_key(key) in _MATERIAL_STATE_ATTRIBUTE_KEYS
    }


def _weather_input_identity(
    entry_data: dict[str, Any], options: dict[str, Any], timezone: str = "UTC",
) -> tuple[Any, ...]:
    """Identify the configuration and cache policy used for a weather fetch."""
    return (
        entry_data.get(CONF_WEATHER),
        options.get(CONF_PLANNING_INTERVAL_MINUTES),
        options.get(CONF_FORECAST_FRESHNESS_MINUTES),
        timezone,
    )


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
