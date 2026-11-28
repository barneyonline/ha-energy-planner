"""Daikin HVAC execution adapter."""

from __future__ import annotations

import asyncio
from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil, isfinite
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.util import dt as dt_util

from .adapter_helpers import (
    DeviceTargetUnavailable,
    async_call_device_service,
    climate_target_snapshot,
    has_finite_temperature_target,
    preserve_cancellation,
    zone_temperature_sync_deferred,
)
from .climate_inputs import (
    climate_storage_value,
    finite,
    hvac_feedback_is_assumed,
    hvac_feedback_is_observed,
    temperature_targets_match,
)
from .const import (
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    HVAC_DRY_ZONE_TARGETS_ABSENT,
    HVAC_PENDING_AUTOMATION_OFF,
)
from .daikin_memory import MEMORY_KEY, OFF_START_OPTION, memory_matches, read_activation, read_memory, source_matches
from .discovery import CapabilityDiscovery, synchronized_zone_targets_required
from .hvac_cycle import HvacModeFeedback, mode_feedback_matches, mode_intent
from .models import ActionKind, PlanAction

_STATE_CONFIRMATION_TIMEOUT_SECONDS = 30.0
_STATE_RECONCILIATION_TIMEOUT_SECONDS = 180.0
_STATE_REFRESH_INTERVAL_SECONDS = 30.0
_STATE_CONFIRMATION_POLL_SECONDS = 0.05
# External classifier: up to five seconds for a mode trigger, then three
# seconds of settling. Include two seconds for queued state listeners.
_SCHEDULER_GUARD_SETTLE_SECONDS = 10.0
_ROLLBACK_ACTIVE_HVAC_MODE = "rollback_active_hvac_mode"
_ROLLBACK_HVAC_MODE_CHANGED = "rollback_hvac_mode_changed"
_ACTIVE_HVAC_MODES = frozenset({"auto", "cool", "dry", "fan_only", "heat", "heat_cool"})


class _HVACStateConfirmationError(RuntimeError):
    """Raised when command ordering cannot be confirmed safely."""


class _HVACFollowupWriteBlocked(_HVACStateConfirmationError):
    """Stop a synchronized takeover after an HVAC actuator has been dispatched."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _HVACManualOverrideError(RuntimeError):
    """Raised when a user supersedes an in-flight HVAC command."""


class _HVACTakeoverBlocked(RuntimeError):
    """Stop acquisition before the first head or zone write."""

    def __init__(
        self, issues: list[str], *, failed_suppression_pre_state: dict[str, Any] | None = None,
        pre_dispatch_reason: str | None = None,
        automation_off_pending: str | None = None,
    ) -> None:
        super().__init__()
        self.issues = tuple(issues)
        self.failed_suppression_pre_state = failed_suppression_pre_state
        self.pre_dispatch_reason = pre_dispatch_reason
        self.suspended_automations: dict[str, str] = {}
        self.automation_dispatched = False
        self.automation_off_pending = automation_off_pending
        self.unsent_command: dict[str, Any] | None = None
        self.prior_confirmation: dict[str, Any] | None = None

    def record_automation_work(self, suspended: dict[str, str], *, dispatched: bool) -> None:
        """Record the automation writes that preceded the block."""
        self.suspended_automations = suspended
        self.automation_dispatched = dispatched


@dataclass(slots=True)
class _AutomationSuppression:
    """Track confirmed suppression, dispatch and required restoration separately."""

    confirmed: bool = False
    restoration_states: dict[str, str] = field(default_factory=dict)
    dispatched: bool = False
    suppressed_active_automation: bool = False
    automation_off_pending: str | None = None


@dataclass(slots=True)
class HVACCommandResult:
    """Result of an HVAC adapter action."""

    applied: bool
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    saved_automation_states: dict[str, str]
    rollback_succeeded: bool | None = None
    saved_zone_states: dict[str, Any] = field(default_factory=dict)
    saved_main_state: dict[str, Any] = field(default_factory=dict)
    command_sent: bool = False
    confirmed_zone_intent: dict[str, Any] | None = None
    capability_issues: tuple[str, ...] = ()
    capability_refused: bool = False


@dataclass(slots=True)
class _OffStartRestoreProgress:
    dispatched: bool = False
    assumed_feedback: bool = False


class DaikinHVACAdapter:
    """Control Daikin through Home Assistant climate services."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.entry_data = entry_data
        self._deferred_zone_entities: set[str] | None = None
        self._async_persist_main_state: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._manual_override_requested: Callable[[], bool] | None = None
        self._async_persist_manual_supersession: Callable[[], Awaitable[None]] | None = None
        self._manual_supersession_persisted = False
        self._manual_zone_overrides_requested: Callable[[], set[str]] | None = None
        self._async_persist_zone_supersession: Callable[[set[str]], Awaitable[None]] | None = None
        self._async_persist_supersessions: Callable[[bool, set[str]], Awaitable[None]] | None = None
        self._persisted_zone_supersessions: set[str] = set()
        self._set_turn_on_feedback_expected: Callable[[bool], None] | None = None
        self._set_coupled_zone_feedback_expected: Callable[[str | None, str | None, str | None], None] | None = None
        self._set_pending_main_restore: Callable[[dict[str, Any]], None] | None = None
        self._set_pending_zone_restore: Callable[[dict[str, Any]], None] | None = None
        self._restoring_main_state = False
        self._unconfirmed_main_mode = False
        self._prior_mode_intent: dict[str, Any] | None = None
        self._async_cycle_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._mode_feedback = HvacModeFeedback()
        self._command_callback: Callable[[dict[str, Any]], None] | None = None
        self._acquisition_guard: Callable[[], str | None] | None = None
        self._last_commands: dict[str, dict[str, Any]] = {}
        self._abort_main_state: dict[str, Any] = {}
        self._command_started: dict[str, float] = {}
        self._climate_override_policy: dict[str, tuple[bool, bool]] = {}
        self._async_persist_confirmation: (
            Callable[[dict[str, Any] | None], Awaitable[dict[str, Any] | None]] | None
        ) = None
        self._recovery_confirmation: dict[str, Any] | None = None
        self._zone_desired: dict[str, Any] = {}
        self._restoring_memory: dict[str, Any] | None = None
        self._restoring_owned_zone_entities: set[str] = set()
        self._off_start_activation: dict[str, Any] | None = None
        self._off_start_recall_dispatched = False
        self._restoring_off_start: _OffStartRestoreProgress | None = None
        self._off_start_memory: dict[str, Any] | None = None
        self._zone_options: dict[str, Any] = {}
        self._zone_baseline: dict[str, Any] = {}
        self._awaiting_hvac_actuator = False
        self._pending_automation_off: str | None = None
        self._checking_takeover_readback = False
        self._late_takeover_issues: tuple[str, ...] = ()
        self._on_dry_takeover_observation: Callable[[bool], None] = lambda _blocked: None
        self._async_save_pre_dispatch_recovery: (
            Callable[[dict[str, str], dict[str, Any] | None, dict[str, Any] | None], Awaitable[None]] | None
        ) = None

    def set_zone_control_context(self, options: dict[str, Any], control: dict[str, Any]) -> None:
        """Keep original zone targets available while a planner-paused alias is null."""
        self._zone_options = options
        self._zone_baseline = control

    @property
    def pending_automation_off(self) -> str | None:
        """Expose uncertainty so automation-only recovery can survive reload."""
        return self._pending_automation_off

    def set_dry_takeover_observation_callback(self, callback: Callable[[bool], None]) -> None:
        """Report whether live evidence blocks takeover in Dry."""
        self._on_dry_takeover_observation = callback

    def set_pre_dispatch_recovery_callback(
        self, callback: Callable[[dict[str, str], dict[str, Any] | None, dict[str, Any] | None], Awaitable[None]],
    ) -> None:
        """Retain only outstanding automation recovery before Dry-mode compensation."""
        self._async_save_pre_dispatch_recovery = callback

    async def async_prepare_off_start(self, action: PlanAction) -> None:
        """Read mode memories before the executor flushes any acquisition intent."""
        self._off_start_memory = None
        self._off_start_activation = None
        head = self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE))
        original = self._zone_baseline.get("main_state")
        if not isinstance(original, dict):
            original = {}
        if (self._zone_options.get(OFF_START_OPTION) is True and head is not None and head.state == "off"
                and action.desired_state.get("configured_zones_only") is True
                and action.desired_state.get("enable_zones")
                and (not self._zone_baseline or original.get(MEMORY_KEY))):
            if original.get(MEMORY_KEY) and not source_matches(self.hass, self.entry_data, original[MEMORY_KEY]):
                # A new read may compensate this attempt, but it cannot grant
                # another controller an existing lifecycle's authority.
                return
            self._off_start_memory = await read_memory(
                self.hass, self.entry_data, str(action.desired_state.get("hvac_mode")),
            )
            if self._off_start_memory is not None:
                mode = self._off_start_memory["selected_mode"]
                self._off_start_activation = {
                    "entity_id": self._off_start_memory["head"], "entry_id": self._off_start_memory["entry_id"],
                    "hvac_mode": mode,
                    "target_temperature": self._off_start_memory["modes"][mode]["target_temperature"],
                }
            self._deferred_zone_entities = None

    def _zone_control_rejection(self) -> str | None:
        from .weather_control import intent_rejection
        from .zone_control import live_rejection
        lease = self._zone_desired.get("weather_continuation")
        if lease:
            if lease.get("entity_id") != self.entry_data.get("weather_entity"):
                return "hvac_weather_source_changed"
            expiry = dt_util.parse_datetime(str(lease.get("expires_at", "")))
            if expiry is None or dt_util.utcnow() >= expiry:
                return "hvac_weather_grace_expired"
            safe = self._zone_baseline.get("weather_safe_intent", {})
            head = self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE))
            reason = intent_rejection(self._zone_desired, safe, dt_util.utcnow(),
                                      head.state if head is not None else None)
            if reason is not None:
                return reason
        rejection = live_rejection(self.hass, self.entry_data, self._zone_options, self._zone_desired,
                                   self._zone_baseline, dt_util.utcnow())
        if rejection is None and self._zone_desired.get("equipment_compensation"):
            head_id = str(self.entry_data.get(CONF_DAIKIN_CLIMATE))
            head = self._state(head_id)
            # Mode-specific target limits can change on wake or mode selection.
            # Check availability before activation; validate the exact target in
            # confirmed cooling feedback before any temperature write.
            if (head is None or "cool" not in (head.attributes.get("hvac_modes") or [])
                    or (head.state == "cool" and not self._equipment_target_available(
                        head_id, self._zone_desired.get("target_temperature")))):
                return "zone_equipment_capability_unavailable"
        return rejection

    def _independent_pairs(self) -> dict[str, str]:
        from .zone_control import topology
        return dict(topology(self.entry_data).get("pairs", {}))

    def _assert_zone_airflow(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Require each intermediate open combination while the head can demand."""
        from .zone_control import topology
        config = topology(self.entry_data)
        pairs = config.get("pairs", {})
        entity_id = str(data.get(ATTR_ENTITY_ID))
        head_id = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        head = self._state(str(head_id))
        switch_change = entity_id in pairs and service in {SERVICE_TURN_ON, SERVICE_TURN_OFF}
        starts_head = entity_id == head_id and (
            service == SERVICE_TURN_ON or service == "set_hvac_mode" and data.get("hvac_mode") != "off"
        )
        head_target = entity_id == head_id and service == "set_temperature"
        # AirBase zone On can itself wake the head. Mode-aware Off starts
        # therefore need a verified resulting airflow set on that first write.
        starts_head = starts_head or bool(switch_change and service == SERVICE_TURN_ON
            and head is not None and head.state == "off" and self._mode_memory_control())
        if pairs and (switch_change or starts_head or head_target) and (
            starts_head or head is None or head.state != "off"
        ):
            if any(self._state_value(entity) not in {"on", "off"} for entity in pairs):
                raise _HVACStateConfirmationError("zone power feedback is not available to verify airflow")
            remaining = {entity for entity in pairs if entity != (entity_id if switch_change else None)
                         and (state := self._state(entity)) is not None and state.state == "on"}
            if switch_change and service == SERVICE_TURN_ON:
                remaining.add(entity_id)
            if not any(remaining == set(row) for row in config["safe_open_sets"]):
                raise _HVACStateConfirmationError("remaining airflow is not verified")

    def _mode_memory_control(self) -> bool:
        main = self._zone_baseline.get("main_state")
        return bool(self._off_start_memory or self._restoring_off_start
                    or isinstance(main, dict) and main.get(MEMORY_KEY)
                    or self._zone_baseline.get("off_start_control_data"))

    def _zone_power_restore_can_open(self) -> bool:
        """Only an authorized Off-start restore may wake an inactive head."""
        if not self._mode_memory_control():
            return True
        if self._restoring_off_start is not None and not (
            self._manual_override_requested is not None and self._manual_override_requested()
        ):
            return True
        return self._state_value(self.entry_data.get(CONF_DAIKIN_CLIMATE)) in _ACTIVE_HVAC_MODES

    def _assert_memory_identity(self) -> None:
        main = self._zone_baseline.get("main_state")
        expected = (self._restoring_memory, self._off_start_memory,
                    main.get(MEMORY_KEY) if isinstance(main, dict) else None,
                    self._zone_baseline.get("off_start_identity"))
        if any(record and not source_matches(self.hass, self.entry_data, record) for record in expected):
            raise _HVACStateConfirmationError("off-start controller identity changed")

    def _memory_target_available(self, entity_id: str, target: Any) -> bool:
        """Require exact live bounds and grid for every mode-aware temperature write."""
        observed = self._state(entity_id)
        if observed is None or not self._zone_target_within_bounds(observed, {"target_temperature": target}):
            return False
        value, low, high, step = (finite(item) for item in (
            target, observed.attributes.get("min_temp"), observed.attributes.get("max_temp"),
            observed.attributes.get("target_temp_step")))
        if value is None or low is None or high is None or step is None or step <= 0:
            return False
        return abs((value - low) / step - round((value - low) / step)) <= 1e-6

    def _assert_memory_target(self, service: str, data: dict[str, Any]) -> None:
        if (self._restoring_memory and data.get(ATTR_ENTITY_ID) == self._restoring_memory["head"]
                and service in {SERVICE_TURN_ON, "set_hvac_mode", "set_temperature"}
                and set(self._restoring_memory["pairs"].values()) - (
                    self._restoring_owned_zone_entities - self._manual_zone_override_entity_ids())):
            # A head write can clamp every physical zone's mode memory. Losing
            # a zone baseline revokes that authority, even when no direct zone
            # write is sent. Off remains available as the original safe power.
            raise _HVACStateConfirmationError("manual zone prevents mode-memory head restoration")
        if (self._mode_memory_control() and service == "set_temperature"
                and not self._memory_target_available(str(data.get(ATTR_ENTITY_ID)), data.get("temperature"))):
            raise _HVACStateConfirmationError("mode-memory target bounds or step changed")

    def _equipment_target_available(self, entity_id: str, target: Any) -> bool:
        """Require exact live bounds/step, with no silent setpoint rounding."""
        state = self._state(entity_id)
        attrs = state.attributes if state is not None else {}
        value, low, high, step = (finite(value) for value in (
            target, attrs.get("min_temp"), attrs.get("max_temp"), attrs.get("target_temp_step")))
        if (value is None or low is None or high is None or step is None or step <= 0
                or not low <= value <= high or abs((value - low) / step - round((value - low) / step)) > 1e-6):
            return False
        head_id = str(self.entry_data.get(CONF_DAIKIN_CLIMATE))
        if entity_id != head_id:
            head = self._state(head_id)
            head_target = finite(head.attributes.get("temperature")) if head is not None else None
            return (head is not None and head.state == "cool" and head_target is not None
                    and head_target == self._zone_desired.get("target_temperature")
                    and abs(value - head_target) <= 2)
        return True

    def _assert_equipment_command(self, service: str, data: dict[str, Any]) -> None:
        """Recheck capabilities after each await at compensated command boundaries."""
        entity = str(data.get(ATTR_ENTITY_ID))
        if service == "set_temperature" and not self._equipment_target_available(entity, data.get("temperature")):
            raise _HVACStateConfirmationError("equipment target is outside live bounds or step")

    def set_confirmation_persistence_callback(
        self, callback: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any] | None]],
        recovery: dict[str, Any] | None = None,
    ) -> None:
        """Retain an uncertain command and its original deadline across reloads."""
        self._async_persist_confirmation = callback
        self._recovery_confirmation = recovery
        if (self._async_cycle_callback is not None and recovery is not None
                and recovery.get("entity_id") == getattr(self, "entry_data", {}).get(CONF_DAIKIN_CLIMATE)
                and mode_intent(recovery)):
            self._unconfirmed_main_mode = True
            self._prior_mode_intent = dict(recovery)

    async def _async_save_confirmation(self, record: dict[str, Any] | None) -> None:
        if self._async_persist_confirmation is not None:
            record = await self._async_persist_confirmation(record) or record
        self._recovery_confirmation = record

    def set_cycle_callback(
        self, callback: Callable[[dict[str, Any]], Awaitable[None]], *, mode_feedback: HvacModeFeedback,
        unresolved_mode: bool = False,
    ) -> None:
        """Persist head mode timing independently of transaction ownership."""
        self._async_cycle_callback = callback
        self._mode_feedback = mode_feedback
        self._unconfirmed_main_mode = unresolved_mode
        self._prior_mode_intent = None

    def set_command_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Record the exact issued service and its bounded feedback expectation."""
        self._command_callback = callback

    def observe_takeover_capability(
        self, desired_state: dict[str, Any] | None = None, *, allow_recalled_dry: bool = False,
    ) -> list[str]:
        """Capture live rollback and Dry evidence before any asynchronous cleanup."""
        discovery = CapabilityDiscovery(
            self.hass, self.entry_data, self._zone_options,
            hvac_desired_state=self._zone_desired if desired_state is None else desired_state,
        )
        issues = discovery.synchronized_takeover_issues()
        if allow_recalled_dry and issues == [HVAC_DRY_ZONE_TARGETS_ABSENT]:
            # The current write can finish its verified recall. This allowance
            # does not end a Dry block already observed by the planner.
            return []
        self._on_dry_takeover_observation(HVAC_DRY_ZONE_TARGETS_ABSENT in issues)
        return issues

    def _synchronized_takeover(self) -> bool:
        return synchronized_zone_targets_required(self.hass, self.entry_data, self._zone_options, self._zone_desired)

    def _live_dry_issues(self) -> tuple[str, ...]:
        """Capture Dry and any faults observed while settling an interrupted takeover."""
        dry_observed = HVAC_DRY_ZONE_TARGETS_ABSENT in self._late_takeover_issues
        if not dry_observed and self._state_value(self.entry_data.get(CONF_DAIKIN_CLIMATE)) != "dry":
            if self._off_start_activation and self._off_start_activation["hvac_mode"] == "dry":
                self._on_dry_takeover_observation(False)
            return ()
        issues = self.observe_takeover_capability()
        return tuple(issues) if dry_observed or HVAC_DRY_ZONE_TARGETS_ABSENT in issues else ()

    def _check_pending_takeover_capability(self, respect_manual_override: bool = True) -> None:
        """Protect the head and zones until the first takeover actuator write."""
        if self._awaiting_hvac_actuator and respect_manual_override and not self._restoring_main_state:
            issues = list(dict.fromkeys((*self.observe_takeover_capability(), *self._assumed_takeover_issues())))
            if issues:
                raise _HVACTakeoverBlocked(issues)

    def _allow_off_start_recall(self, head: State | None) -> bool:
        """Retire recall eligibility as soon as active feedback contradicts it."""
        activation = self._off_start_activation
        if activation is None or head is None or head.state == "off":
            return False
        recalled_dry = head.state == activation["hvac_mode"] == "dry" and temperature_targets_match(
            head.attributes.get("temperature"), activation["target_temperature"],
        )
        self._off_start_recall_dispatched = recalled_dry
        return recalled_dry

    def _assumed_takeover_issues(self, service: str | None = None) -> list[str]:
        """Require observed feedback from the head and zones used for demand."""
        if service == SERVICE_TURN_OFF or self._zone_desired.get("hvac_mode") == "off":
            return []
        issues = []
        head = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        if head and hvac_feedback_is_assumed(self.hass.states.get(head)):
            issues.append("daikin_climate_unavailable")
        synchronize_zones = self._synchronized_takeover()
        enable_zones = self._zone_desired.get("enable_zones")
        if any(
            hvac_feedback_is_assumed(self.hass.states.get(entity_id))
            for entity_id in self._zone_entities()
            if synchronize_zones or (enable_zones and not entity_id.startswith("climate."))
        ):
            issues.append("climate_zone_unavailable")
        return issues

    def _observe_followup_takeover(self, service: str | None = None) -> tuple[str, ...]:
        """Recheck feedback while settling takeover and preserve the observed issues."""
        if self._awaiting_hvac_actuator:
            return ()
        allow_recalled_dry = False
        if self._off_start_recall_dispatched:
            allow_recalled_dry = self._allow_off_start_recall(
                self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE)),
            ) and service != "set_temperature"
        issues = (
            tuple(self.observe_takeover_capability(allow_recalled_dry=True)) if allow_recalled_dry
            else self._live_dry_issues()
        )
        issues = tuple(dict.fromkeys((*issues, *self._assumed_takeover_issues(service))))
        if issues:
            self._late_takeover_issues = tuple(dict.fromkeys((*self._late_takeover_issues, *issues)))
        return issues

    def _check_followup_takeover(self, service: str | None = None) -> None:
        if issues := self._observe_followup_takeover(service):
            raise _HVACFollowupWriteBlocked(",".join(issues))

    @contextmanager
    def _check_takeover_readbacks(self) -> Iterator[None]:
        """Apply takeover admission checks while waiting for acquisition feedback."""
        self._checking_takeover_readback = True
        try:
            yield
        finally:
            self._checking_takeover_readback = False

    def set_acquisition_guard(self, guard: Callable[[], str | None]) -> None:
        """Check plan authority immediately before the first takeover write."""
        self._acquisition_guard = guard

    def _authorize_acquisition(self) -> None:
        if self._acquisition_guard is not None:
            if (reason := self._acquisition_guard()) is not None:
                raise _HVACTakeoverBlocked([], pre_dispatch_reason=reason)
            self._acquisition_guard = None

    async def _async_command(
        self, hass: HomeAssistant, domain: str, service: str, data: dict[str, Any],
        *, emergency_off: bool = False, respect_manual_override: bool = True,
        scheduler_guard: bool = False, takeover_write: bool = False,
        restoring_zone_power: bool = False, **kwargs: Any
    ) -> State | None:
        """Issue a contextual command and return the state captured before command preparation."""
        entity_id = str(data.get(ATTR_ENTITY_ID))
        first_actuator = (
            self._awaiting_hvac_actuator and respect_manual_override and not self._restoring_main_state
            and not scheduler_guard and domain != "automation"
        )
        synchronized_first_actuator = first_actuator and self._synchronized_takeover()
        actuator_failure_reason = "hvac_control_service_failed" if domain == "climate" else "hvac_zone_service_failed"
        if not scheduler_guard and self._awaiting_hvac_actuator and respect_manual_override:
            self._raise_if_manual_override_requested()
            self._check_pending_takeover_capability()
        if takeover_write:
            self._check_followup_takeover(service)
        if respect_manual_override and self._zone_control_rejection() is not None:
            raise _HVACStateConfirmationError("zone intent no longer matches fresh room evidence")
        pairs = self._independent_pairs()
        if entity_id in pairs:
            # Switches share the durable climate command/readback protocol,
            # including its override scope. Recovery still preserves this exact
            # zone, but a main or sibling override cannot block its restoration.
            self._climate_override_policy[entity_id] = (respect_manual_override, True)
        self._assert_memory_identity()
        self._assert_zone_airflow(domain, service, data)
        self._assert_memory_target(service, data)
        if domain == "climate" and respect_manual_override and self._zone_desired.get("equipment_compensation"):
            self._assert_equipment_command(service, data)
        if domain != "climate" and (
            domain == "automation" or entity_id in self._zone_entities()
        ) and entity_id != self.entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER):
            if not await self._async_arm_scheduler_guard():
                self._check_pending_takeover_capability(respect_manual_override)
                if synchronized_first_actuator:
                    raise _HVACTakeoverBlocked([], pre_dispatch_reason=actuator_failure_reason)
                raise _HVACStateConfirmationError("climate scheduler guard was not confirmed")
            # Renewing a guard yields too: acquisition must stop for any user
            # change, while restoration can finish siblings but never this zone.
            if respect_manual_override:
                self._raise_if_manual_override_requested()
            elif entity_id in self._manual_zone_override_entity_ids():
                raise _HVACManualOverrideError
        context = kwargs.get("context") or Context()
        if self._command_callback is not None:
            kwargs["context"] = context
        observed = self._state(str(data.get(ATTR_ENTITY_ID)))
        record = {
            "entity_id": data.get(ATTR_ENTITY_ID),
            "previous_mode": observed.state if observed is not None else None,
            "restoration": self._restoring_main_state,
            "uncertainty_compensation": self._restoring_main_state and self._unconfirmed_main_mode,
            "domain": domain,
            "service": service,
            "context_id": context.id,
            "requested": {key: value for key, value in data.items() if key != ATTR_ENTITY_ID},
            "observed": _climate_state_snapshot(observed) if domain == "climate" and observed is not None else {},
            "target_bounds": {
                key: climate_storage_value(observed.attributes.get(key)) for key in ("min_temp", "max_temp")
            } if observed is not None else {},
        }
        record["coupled_zones"] = {
            entity: {
                "mode": item.state,
                **climate_target_snapshot(item),
                "min_temp": climate_storage_value(item.attributes.get("min_temp")),
                "max_temp": climate_storage_value(item.attributes.get("max_temp")),
            }
            for entity in self._zone_climate_entities()
            if (item := self._state(entity)) is not None
        }
        main = self._state(str(self.entry_data.get(CONF_DAIKIN_CLIMATE)))
        if (self._mode_memory_control() and entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE)
                and service == "set_hvac_mode" and data.get("hvac_mode") == "dry"):
            # Core Daikin exposes no subordinate temperature in Dry, including
            # disabled aliases. Scope null feedback to this exact issued mode.
            record["off_start_dry_zones"] = self._zone_climate_entities()
        record["main_mode"] = main.state if main is not None else None
        record["main_target"] = climate_storage_value(main.attributes.get("temperature")) if main is not None else None
        entity_id = str(data.get(ATTR_ENTITY_ID))
        tracks_confirmation = domain == "climate" or entity_id in pairs
        unconfirmed_before = self._unconfirmed_main_mode
        prior_confirmation = self._recovery_confirmation
        acquiring = respect_manual_override and not scheduler_guard and not self._restoring_main_state
        if not tracks_confirmation and not scheduler_guard:
            self._check_pending_takeover_capability(respect_manual_override)
            if takeover_write:
                self._check_followup_takeover(service)
            if acquiring and (domain == "automation" or entity_id in self._zone_entities()):
                self._authorize_acquisition()
        self._last_commands[entity_id] = record
        self._command_started[entity_id] = asyncio.get_running_loop().time()
        if tracks_confirmation:
            now = dt_util.utcnow()
            record.update(
                issued_at=now.isoformat(),
                deadline=(now + timedelta(seconds=_STATE_RECONCILIATION_TIMEOUT_SECONDS)).isoformat(),
            )
            try:
                # Flush before dispatch so startup can reconcile a potentially
                # issued command. State listeners can run during the flush.
                await self._async_save_confirmation(record)
                await asyncio.sleep(0)
                self._raise_if_climate_override_requested(entity_id)
                self._check_pending_takeover_capability(respect_manual_override)
                if (self._async_cycle_callback is not None
                        and entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE) and mode_intent(record)):
                    self._unconfirmed_main_mode = True
                    await self._async_cycle_callback({**record, "stage": "intent",
                                                       "prior_mode_intent": self._prior_mode_intent})
                self._raise_if_climate_override_requested(entity_id)
                # Existing exception cleanup must still shut down a head whose
                # original baseline was off when classifier helpers are broken.
                guard_ready = await self._async_arm_scheduler_guard(
                    deadline=now + timedelta(seconds=_STATE_RECONCILIATION_TIMEOUT_SECONDS),
                )
                # Guard services can yield to explicit user actions too.
                self._raise_if_climate_override_requested(entity_id)
                self._check_pending_takeover_capability(respect_manual_override)
                if takeover_write:
                    self._check_followup_takeover(service)
                if not guard_ready and not (emergency_off and service == SERVICE_TURN_OFF):
                    if synchronized_first_actuator:
                        raise _HVACTakeoverBlocked([], pre_dispatch_reason="hvac_state_confirmation_failed")
                    raise _HVACStateConfirmationError("climate scheduler guard was not confirmed")
                if respect_manual_override and self._zone_control_rejection() is not None:
                    raise _HVACStateConfirmationError("zone evidence changed during intent persistence")
                self._assert_memory_identity()
                self._assert_zone_airflow(domain, service, data)
                self._assert_memory_target(service, data)
                if (domain == "climate" and respect_manual_override
                        and self._zone_desired.get("equipment_compensation")):
                    self._assert_equipment_command(service, data)
                if (self._async_cycle_callback is not None
                        and entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE) and mode_intent(record)):
                    # Read-only: recheck after all persistence and guard awaits.
                    await self._async_cycle_callback({**record, "stage": "dispatch"})
                self._check_pending_takeover_capability(respect_manual_override)
                if takeover_write:
                    self._check_followup_takeover(service)
                if acquiring:
                    self._authorize_acquisition()
            except BaseException as error:  # Cancellation before dispatch is an unsent intent too.
                self._unconfirmed_main_mode = unconfirmed_before
                if isinstance(error, _HVACTakeoverBlocked):
                    error.unsent_command = record
                    error.prior_confirmation = prior_confirmation
                elif isinstance(error, (Exception, CancelledError)):
                    try:
                        self._check_pending_takeover_capability(respect_manual_override)
                        if (first_actuator and prior_confirmation is not None and isinstance(error, Exception)
                                and not isinstance(error, (_HVACManualOverrideError, _HVACStateConfirmationError))):
                            # Cached settings can still match the baseline while an earlier
                            # accepted write is pending. Recover only dispatched automation work.
                            raise _HVACTakeoverBlocked([], pre_dispatch_reason=actuator_failure_reason)
                        observe_late = takeover_write and isinstance(error, Exception)
                        if observe_late:
                            self._observe_followup_takeover(service)
                        retirement_error: Exception | None = None
                        try:
                            with preserve_cancellation(error):
                                await self._async_retire_unsent_command(record, prior_confirmation)
                        except Exception as cleanup_error:
                            retirement_error = cleanup_error
                        finally:
                            # Retirement can yield while persisting supersession,
                            # cycle intent, or command confirmation.
                            self._check_pending_takeover_capability(respect_manual_override)
                            if observe_late:
                                self._observe_followup_takeover(service)
                        if retirement_error is not None:
                            if observe_late and self._late_takeover_issues:
                                raise _HVACFollowupWriteBlocked(
                                    "hvac_confirmation_retirement_failed",
                                ) from retirement_error
                            raise retirement_error
                        if observe_late and self._late_takeover_issues and not isinstance(
                            error, (_HVACStateConfirmationError, _HVACManualOverrideError),
                        ):
                            raise _HVACFollowupWriteBlocked(actuator_failure_reason) from error
                    except _HVACTakeoverBlocked as blocked:
                        # Save the narrower ownership before generic rollback.
                        # Retirement failures must not hide a request to cancel this task.
                        blocked.unsent_command = record
                        blocked.prior_confirmation = prior_confirmation
                        cause = error if isinstance(error, CancelledError) else blocked.__context__
                        raise blocked from cause
                else:
                    await self._async_retire_unsent_command(record, prior_confirmation)
                raise

        def on_dispatch() -> None:
            head = self.entry_data.get(CONF_DAIKIN_CLIMATE)
            needs_head = entity_id != head and (
                domain == "climate" or entity_id in pairs or takeover_write or restoring_zone_power
            )
            if hvac_feedback_is_assumed(hass.states.get(entity_id)) or (
                needs_head and head and hvac_feedback_is_assumed(hass.states.get(head))
            ):
                if self._restoring_off_start is not None and tracks_confirmation:
                    self._restoring_off_start.assumed_feedback = True
                raise DeviceTargetUnavailable("hvac_feedback_assumed")
            if (restoring_zone_power and service == SERVICE_TURN_ON
                    and not self._zone_power_restore_can_open()):
                raise DeviceTargetUnavailable("hvac_zone_restore_head_inactive")
            if first_actuator:
                # Only an actuator write that reaches Home Assistant can
                # recall remembered Dry during an Off start.
                self._awaiting_hvac_actuator = False
            activation = self._off_start_activation
            if (activation is not None and service == SERVICE_TURN_ON
                    and self._state_value(activation["entity_id"]) == "off"):
                # An external wake can change the head during intent persistence.
                # Only dispatch from live Off can prove a subsequent mode recall.
                if entity_id in pairs:
                    record["off_start_head_activation"] = {
                        **activation,
                        "zone_climates": [climate for actuator, climate in pairs.items()
                                          if actuator == entity_id or self._state_value(actuator) == "on"],
                    }
                if takeover_write and (entity_id in pairs or entity_id == activation["entity_id"]):
                    self._off_start_recall_dispatched = True
            if self._command_callback is not None:
                self._command_callback({**record, "stage": "issued"})
            record["dispatched"] = True
            if self._restoring_off_start is not None and tracks_confirmation:
                self._restoring_off_start.dispatched = True

        try:
            await async_call_device_service(hass, domain, service, data, on_dispatch=on_dispatch, **kwargs)
        except TimeoutError:
            if domain != "climate" and entity_id not in pairs:
                raise
            # The device may have accepted the write before HA timed out.
            # Only readback can resolve this; never resend the command here.
            if self._command_callback is not None:
                self._command_callback({**record, "stage": "readback_pending", "reason": "service_timeout"})
        except BaseException as error:
            local_refusal = isinstance(error, DeviceTargetUnavailable)
            if self._command_callback is not None:
                self._command_callback({
                    **record, "stage": "not_dispatched" if local_refusal else "service_failed",
                    "failure_category": type(error).__name__,
                })
            if local_refusal and synchronized_first_actuator:
                # Local preflight proves the first actuator was never written.
                # Retire its intent without attempting HVAC compensation.
                self._unconfirmed_main_mode = unconfirmed_before
                rejection = _HVACTakeoverBlocked([], pre_dispatch_reason=actuator_failure_reason)
                if tracks_confirmation:
                    rejection.unsent_command = record
                    rejection.prior_confirmation = prior_confirmation
                raise rejection from error
            if local_refusal and tracks_confirmation:
                self._state(entity_id)  # Capture assumed feedback even when target preflight stopped dispatch.
                self._unconfirmed_main_mode = unconfirmed_before
                await self._async_retire_unsent_command(record, prior_confirmation)
            raise
        return observed

    async def _async_retire_unsent_command(
        self, record: dict[str, Any], prior_confirmation: dict[str, Any] | None,
    ) -> None:
        """Remove the intent of a command that never reached Home Assistant."""
        entity_id = str(record["entity_id"])
        # A superseding user change must survive a restart before the unsent
        # record ceases to guard the earlier baseline.
        await self._async_persist_requested_manual_supersessions()
        if (self._async_cycle_callback is not None
                and entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE) and mode_intent(record)):
            await self._async_cycle_callback({**record, "stage": "not_dispatched"})
        if prior_confirmation is not None and self._confirmation_superseded(prior_confirmation):
            prior_confirmation = None
        await self._async_save_confirmation(prior_confirmation)
        if prior_confirmation is not None and self._confirmation_superseded(prior_confirmation):
            await self._async_persist_requested_manual_supersessions()
            await self._async_save_confirmation(None)
        self._forget_unsent_command(record)

    def _confirmation_superseded(self, record: dict[str, Any]) -> bool:
        entity = record.get("entity_id")
        return entity in self._manual_zone_override_entity_ids() or bool(
            entity == self.entry_data.get(CONF_DAIKIN_CLIMATE)
            and self._manual_override_requested is not None and self._manual_override_requested()
        )

    def _forget_unsent_command(self, record: dict[str, Any]) -> None:
        """Discard in-memory timing after the durable intent is retired."""
        entity_id = str(record["entity_id"])
        if (self._recovery_confirmation or {}).get("context_id") == record["context_id"]:
            self._recovery_confirmation = None
        self._last_commands.pop(entity_id, None)
        self._command_started.pop(entity_id, None)

    async def _record_confirmation(self, entity_id: str, confirmed: bool) -> None:
        """Publish authoritative readback or a sanitized timeout stage."""
        record = self._last_commands.get(entity_id)
        if record is None:
            return
        observed = self._state(entity_id)
        values = (
            _climate_state_snapshot(observed)
            if observed is not None and entity_id.startswith("climate.")
            else {"mode": observed.state if observed is not None else None}
        )
        evidence = {
            **record,
            "stage": "confirmed" if confirmed else "readback_failed",
            "observed": values,
            "failure_category": None if confirmed else "ConfirmationTimeout",
        }
        if self._async_cycle_callback is not None and entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE):
            evidence["feedback_changed_at"] = self._mode_feedback.mode_feedback_at(entity_id, observed)
            if confirmed and mode_feedback_matches(record, values.get("hvac_mode")):
                self._unconfirmed_main_mode = False
                self._prior_mode_intent = None
            await self._async_cycle_callback(evidence)
        if self._command_callback is not None:
            self._command_callback(evidence)

    def set_abort_main_state(self, baseline: dict[str, Any]) -> None:
        """Preserve the owned original baseline across cancellation of a refresh."""
        self._abort_main_state = dict(baseline)

    def _zone_restore_needs_operator(self, zones: dict[str, Any], main_state: dict[str, Any]) -> bool:
        """Identify incompatible saved targets after an already-confirmed shutdown.

        Off-state bounds cannot establish another mode's original intent. Keep
        the record intact and wait for explicit operator supersession or usable
        feedback, without probing remembered modes by waking the equipment.
        """
        head = self._state(str(self.entry_data.get(CONF_DAIKIN_CLIMATE)))
        if (not main_state.get("zone_recovery_attempted") or main_state.get("hvac_mode") != "off"
                or head is None or head.state != "off"):
            return False
        for entity, target in zones.items():
            observed = self._state(entity)
            if not entity.startswith("climate.") or observed is None or not isinstance(target, dict):
                continue
            low, high = finite(observed.attributes.get("min_temp")), finite(observed.attributes.get("max_temp"))
            values = list(_temperature_desired_state(target).values())
            if (low is not None and high is not None and low <= high and values
                    and any((value := finite(item)) is not None and not low <= value <= high for item in values)):
                return True
        return False

    def _record_zone_deferred(self, entity_id: str, target: Any) -> None:
        """Retain the unavailable target/bounds step without issuing another command."""
        if self._command_callback is None:
            return
        observed = self._state(entity_id)
        values = (
            {
                **_climate_state_snapshot(observed),
                "min_temp": observed.attributes.get("min_temp"),
                "max_temp": observed.attributes.get("max_temp"),
            }
            if observed is not None
            else {}
        )
        self._command_callback(
            {
                "entity_id": entity_id,
                "domain": "climate",
                "service": "restore_target",
                "context_id": Context().id,
                "stage": "zone_bounds_pending",
                "requested": target,
                "observed": values,
                "failure_category": "ZoneTargetOrBoundsUnavailable",
            }
        )

    def set_main_state_persistence_callback(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Set the crash-boundary persistence hook for evolving main rollback state."""
        self._async_persist_main_state = callback

    def set_manual_override_check(self, callback: Callable[[], bool]) -> None:
        """Set the synchronous check for a superseding user command."""
        self._manual_override_requested = callback

    def set_manual_override_persistence_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Set the durable boundary for a superseding user command."""
        self._async_persist_manual_supersession = callback

    def set_zone_manual_override_check(
        self,
        callback: Callable[[], set[str]],
    ) -> None:
        """Set the synchronous check for superseding zone commands."""
        self._manual_zone_overrides_requested = callback

    def set_zone_manual_override_persistence_callback(
        self,
        callback: Callable[[set[str]], Awaitable[None]],
    ) -> None:
        """Set the durable boundary for superseding zone commands."""
        self._async_persist_zone_supersession = callback

    def set_manual_supersession_persistence_callback(
        self,
        callback: Callable[[bool, set[str]], Awaitable[None]],
    ) -> None:
        """Set the atomic durable boundary for main and zone supersession."""
        self._async_persist_supersessions = callback

    @property
    def deferred_zone_entities(self) -> list[str]:
        """Return the frozen target exclusions for the pending transaction."""
        return sorted(self._deferred_zone_entities or set())

    def set_turn_on_feedback_callback(
        self,
        callback: Callable[[bool], None],
    ) -> None:
        """Expose the bounded phase that can publish a turn-on mode."""
        self._set_turn_on_feedback_expected = callback

    def set_coupled_zone_feedback_callback(
        self,
        callback: Callable[[str | None, str | None, str | None], None],
    ) -> None:
        """Expose a zone-actuator call that can publish linked climate feedback."""
        self._set_coupled_zone_feedback_expected = callback

    def set_pending_main_restore_callback(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Expose the main rollback snapshot expected during restoration."""
        self._set_pending_main_restore = callback

    def set_pending_zone_restore_callback(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Expose zone rollback snapshots expected during restoration."""
        self._set_pending_zone_restore = callback

    def _manual_zone_override_entity_ids(self) -> set[str]:
        """Return configured zones superseded during this transaction."""
        if self._manual_zone_overrides_requested is None:
            return set()
        return {str(entity_id) for entity_id in self._manual_zone_overrides_requested() if str(entity_id)}

    def _raise_if_manual_override_requested(
        self,
        *,
        include_zones: bool = True,
    ) -> None:
        """Stop the transaction before another planner-owned mutation."""
        main_superseded = self._manual_override_requested is not None and self._manual_override_requested()
        if main_superseded or (include_zones and self._manual_zone_override_entity_ids()):
            raise _HVACManualOverrideError

    async def _async_persist_override_supersessions(
        self,
        main_superseded: bool,
        zone_entity_ids: set[str],
    ) -> None:
        """Persist newly observed main and zone supersession atomically."""
        persist_main = main_superseded and not self._manual_supersession_persisted
        persist_zones = zone_entity_ids - self._persisted_zone_supersessions
        if not persist_main and not persist_zones:
            return
        if self._async_persist_supersessions is not None:
            await self._async_persist_supersessions(
                persist_main,
                persist_zones,
            )
        else:
            if persist_main and self._async_persist_manual_supersession is not None:
                await self._async_persist_manual_supersession()
            if persist_zones and self._async_persist_zone_supersession is not None:
                await self._async_persist_zone_supersession(persist_zones)
        self._manual_supersession_persisted = self._manual_supersession_persisted or persist_main
        self._persisted_zone_supersessions.update(persist_zones)

    async def _async_persist_requested_manual_supersessions(self) -> None:
        """Persist main and zone supersession at an actuator boundary."""
        while True:
            main_superseded = self._manual_override_requested is not None and self._manual_override_requested()
            zone_entity_ids = self._manual_zone_override_entity_ids()
            await self._async_persist_override_supersessions(
                main_superseded,
                zone_entity_ids,
            )
            main_is_pending = (
                self._manual_override_requested is not None
                and self._manual_override_requested()
                and not self._manual_supersession_persisted
            )
            zones_are_pending = not self._manual_zone_override_entity_ids().issubset(self._persisted_zone_supersessions)
            if not main_is_pending and not zones_are_pending:
                return

    def takeover_snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
        """Return automation and zone state that must survive a takeover crash."""
        if self._off_start_memory is not None:
            targets = self._off_start_memory["modes"][self._off_start_memory["acquired_mode"]]["zones"]
            self._deferred_zone_entities = set()
            return self._enabled_automation_states(), {
                **self._zone_states(), **{entity: {
                    "target_temperature": target, "baseline_hvac_mode": self._off_start_memory["acquired_mode"],
                } for entity, target in targets.items()},
            }
        if self._deferred_zone_entities is None:
            self._deferred_zone_entities = {
                entity_id
                for entity_id in self._zone_climate_entities()
                if (
                    zone_temperature_sync_deferred(self._state(entity_id))
                    and not self._zone_baseline.get("zone_states", {}).get(entity_id)
                ) or (
                    self._zone_baseline.get("main_state")
                    and entity_id not in self._zone_baseline.get("zone_states", {})
                )
            }
        return self._enabled_automation_states(), {
            entity_id: state
            for entity_id, state in self._zone_states().items()
            if entity_id not in self._deferred_zone_entities
        }

    def main_takeover_snapshot(self) -> dict[str, Any]:
        """Return main climate state that must survive a takeover crash."""
        climate_state = self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE))
        snapshot = {} if climate_state is None else _climate_state_snapshot(climate_state)
        if self._off_start_memory is not None and climate_state is not None and climate_state.state == "off":
            snapshot[MEMORY_KEY] = deepcopy(self._off_start_memory)
        return snapshot

    async def async_execute(self, action: PlanAction) -> HVACCommandResult:
        """Execute a supported HVAC action."""
        baseline = self._abort_main_state or self.main_takeover_snapshot()
        automation_states = self._automation_states()
        prior_automation_commands = {entity: self._last_commands.get(entity) for entity in automation_states}
        self._off_start_recall_dispatched = False
        self._late_takeover_issues = ()
        self._pending_automation_off = None
        self._awaiting_hvac_actuator = action.kind == ActionKind.SET_HVAC
        try:
            return await self._async_execute(action, automation_states)
        except _HVACTakeoverBlocked as blocked:
            self._acquisition_guard = None
            suppression_failed = blocked.failed_suppression_pre_state is not None
            observed = blocked.failed_suppression_pre_state if suppression_failed else self._snapshot()
            assert observed is not None
            with preserve_cancellation(blocked.__cause__):
                await self._async_persist_pre_dispatch_recovery(
                    blocked.suspended_automations, blocked.unsent_command, blocked.prior_confirmation,
                )
            if isinstance(blocked.__cause__, CancelledError):
                cancellation = blocked.__cause__
                raise cancellation from cancellation.__cause__
            observed_issues = dict.fromkeys(blocked.issues)

            def observe_recovery() -> None:
                observed_issues.update(dict.fromkeys(self.observe_takeover_capability()))

            if blocked.automation_off_pending is not None:
                # Even a failed suppression call can publish its Off state late.
                observe_recovery()
                if await self._async_confirm_state(blocked.automation_off_pending, "off", on_readback=observe_recovery):
                    self._pending_automation_off = None
            restored, unresolved = await self._async_enable_automation_entities(blocked.suspended_automations)
            if blocked.pre_dispatch_reason is not None:
                # A stopped acquisition can precede queued state feedback.
                await asyncio.sleep(0)
            observe_recovery()
            issues = tuple(observed_issues)
            if suppression_failed:
                reason = "hvac_automation_service_failed" if restored else "hvac_automation_rollback_failed"
            elif blocked.pre_dispatch_reason is not None:
                reason = blocked.pre_dispatch_reason if restored else "hvac_acquisition_rollback_failed"
            else:
                reason = ",".join(issues) if restored else "hvac_acquisition_rollback_failed"
            return HVACCommandResult(
                False,
                reason,
                observed,
                self._snapshot(),
                unresolved,
                restored,
                command_sent=blocked.automation_dispatched,
                capability_issues=issues,
                capability_refused=(
                    not suppression_failed and blocked.pre_dispatch_reason is None
                    and (HVAC_DRY_ZONE_TARGETS_ABSENT in issues or (bool(issues) and not blocked.automation_dispatched))
                ),
            )
        except BaseException as error:
            if (isinstance(error, (Exception, CancelledError)) and self._awaiting_hvac_actuator
                    and HVAC_DRY_ZONE_TARGETS_ABSENT in self.observe_takeover_capability()):
                # Only automation writes could have reached Home Assistant.
                # Keep those eligible for recovery and retire the untouched HVAC baselines.
                suspended = {
                    entity: state for entity, state in automation_states.items()
                    if state == "on" and self._command_dispatched_since(entity, prior_automation_commands[entity])
                }
                with preserve_cancellation(error):
                    await self._async_persist_pre_dispatch_recovery(suspended, None)
                    pending = self._pending_automation_off
                    if pending is not None and pending in suspended:
                        # A retry could otherwise accept the cached On state
                        # before the dispatched suppression publishes Off.
                        if await self._async_confirm_state(pending, "off"):
                            self._pending_automation_off = None
                        self.observe_takeover_capability()
                raise
            # Acquisition has ended. Existing rollback and emergency shutdown
            # must keep their authority if the adapter observed a new mode.
            self._awaiting_hvac_actuator = False
            if self._command_callback is not None:
                self._command_callback(
                    {
                        "entity_id": self.entry_data.get(CONF_DAIKIN_CLIMATE),
                        "domain": "climate",
                        "service": "execute",
                        "context_id": Context().id,
                        "stage": "acquisition_failed",
                        "failure_category": type(error).__name__,
                    }
                )
            # Durable zone/automation ownership remains for the supported retry
            # path. Cancellation must independently return the head's safe state.
            with preserve_cancellation(error):
                try:
                    if not baseline.get(MEMORY_KEY):
                        await self.async_restore(saved_main_state=baseline)
                finally:
                    # Guard failure can return before restoration reaches the head.
                    # Emergency shutdown must still run for a known off baseline.
                    if (
                        baseline.get("hvac_mode") == "off"
                        and baseline.get("recovery_version", 1) == 1
                        and not (self._manual_override_requested is not None and self._manual_override_requested())
                    ):
                        await self._async_apply_hvac_state(
                            str(self.entry_data.get(CONF_DAIKIN_CLIMATE)),
                            {"hvac_mode": "off"},
                            respect_zone_manual_override=False,
                            emergency_off=True,
                        )
            raise
        finally:
            self._acquisition_guard = None
            self._awaiting_hvac_actuator = False
            self._deferred_zone_entities = None

    async def _async_persist_pre_dispatch_recovery(
        self, automations: dict[str, str], unsent: dict[str, Any] | None,
        prior_confirmation: dict[str, Any] | None = None,
    ) -> None:
        """Keep only work that may need recovery after a pre-dispatch Dry block."""
        self._awaiting_hvac_actuator = False
        if self._async_save_pre_dispatch_recovery is not None:
            await self._async_save_pre_dispatch_recovery(automations, unsent, prior_confirmation)
            if unsent is not None:
                self._forget_unsent_command(unsent)
        elif unsent is not None:
            await self._async_retire_unsent_command(unsent, prior_confirmation)
        else:
            await self._async_persist_requested_manual_supersessions()

    async def _async_execute(self, action: PlanAction, saved_automation_states: dict[str, str]) -> HVACCommandResult:
        """Keep the pre-persisted zone exclusions fixed through retries and rollback."""
        self._manual_supersession_persisted = False
        self._persisted_zone_supersessions.clear()
        pre_state = self._snapshot()
        self._zone_desired = action.desired_state
        rejection = self._zone_control_rejection()
        if rejection:
            return HVACCommandResult(False, rejection, pre_state, pre_state, {}, True)
        automation_work = any(
            state == "on" or bool(getattr(self._state(entity_id), "attributes", {}).get("current", 0))
            for entity_id, state in saved_automation_states.items()
        )
        restorable_automation_states = {
            entity_id: state for entity_id, state in saved_automation_states.items() if state == "on"
        }
        captured_zone_states = self.takeover_snapshot()[1]
        if action.kind == ActionKind.RELEASE_HVAC:
            return await self.async_restore(restorable_automation_states, captured_zone_states)
        synchronize_zone_temperatures = action.desired_state.get("configured_zones_only") is True
        controlled_zone_entities = [
            entity_id
            for entity_id in self._zone_entities()
            if entity_id not in (self._deferred_zone_entities or set())
            and (entity_id.split(".", 1)[0] != "climate" or synchronize_zone_temperatures)
        ]
        saved_zone_states = {
            entity_id: state
            for entity_id, state in captured_zone_states.items()
            if action.desired_state.get("enable_zones") and entity_id in controlled_zone_entities
        }
        if action.kind != ActionKind.SET_HVAC:
            return HVACCommandResult(False, "unsupported_hvac_action", pre_state, self._snapshot(), {}, True)
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        climate_state = self._state(climate_entity)
        if climate_entity is None or climate_state is None:
            return HVACCommandResult(False, "daikin_climate_unavailable", pre_state, self._snapshot(), {}, True)
        saved_main_state = self.main_takeover_snapshot()
        if not action.desired_state:
            return HVACCommandResult(False, "hvac_desired_state_empty", pre_state, self._snapshot(), {}, True)
        synchronized_takeover = self._synchronized_takeover()
        valid_main_target = (
            has_finite_temperature_target(saved_main_state) if synchronized_takeover
            else _has_temperature_target_fields(saved_main_state)
        )
        if _temperature_desired_state(action.desired_state) and not valid_main_target:
            # A main-target mutation cannot be transactional unless the target
            # it is about to replace can be restored after any later failure.
            return HVACCommandResult(
                False,
                "main_climate_target_unavailable",
                pre_state,
                self._snapshot(),
                {},
                True,
            )
        if action.desired_state.get("configured_zones_only") and not self._zone_climate_entities():
            return HVACCommandResult(
                False,
                "zone_only_preconditioning_requires_climate_zone",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                True,
                saved_zone_states,
            )
        if set(saved_automation_states) != set(self._automation_entities()):
            return HVACCommandResult(
                False,
                "climate_automation_unavailable",
                pre_state,
                self._snapshot(),
                {},
                True,
                saved_zone_states,
            )
        if action.desired_state.get("enable_zones") and any(
            self._state(entity_id) is None for entity_id in controlled_zone_entities
        ):
            return HVACCommandResult(
                False,
                "climate_zone_unavailable",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                True,
                saved_zone_states,
            )
        zone_target_work = bool(
            action.desired_state.get("enable_zones") and synchronize_zone_temperatures
            and (_temperature_desired_state(action.desired_state) or action.desired_state.get("zone_targets"))
        )
        requested_zones = [
            entity for entity in self._zone_climate_entities()
            if entity not in {
                climate for actuator, climate in action.desired_state.get("zone_intent", {}).get("pairs", {}).items()
                if action.desired_state.get("zone_power", {}).get(actuator) == "off"
            }
        ]
        if zone_target_work and any(
            not has_finite_temperature_target(dict(saved_zone_states.get(entity_id, {})))
            for entity_id in requested_zones
        ):
            # Every subordinate target that will be replaced needs a usable
            # rollback snapshot. An empty climate snapshot otherwise makes both
            # apply and confirmation no-ops while leaving the planner target set.
            return HVACCommandResult(
                False,
                "climate_zone_target_unavailable",
                pre_state,
                self._snapshot(),
                {},
                True,
            )
        if zone_target_work:
            # A target exposed by selecting another mode is that mode's memory,
            # not the original baseline. Until we can durably restore every
            # affected mode, refuse the transition before any device writes.
            original_main = self._zone_baseline.get("main_state") or saved_main_state
            original_mode = original_main.get("hvac_mode")
            desired_mode = action.desired_state.get("hvac_mode") or climate_state.state
            memory = original_main.get(MEMORY_KEY, {})
            reversible_off = (original_mode == "off" and memory.get("version") == 1
                              and memory.get("acquired_mode") == desired_mode
                              and (climate_state.state != "off" or self._off_start_memory is not None))
            if not reversible_off and (original_mode not in _ACTIVE_HVAC_MODES or desired_mode != original_mode
                    or (not self._zone_baseline.get("main_state") and any(
                        (zone := self._state(entity)) is None or zone.state != original_mode
                        for entity in requested_zones
                    ))):
                return HVACCommandResult(
                    False, "climate_zone_baseline_mode_mismatch", pre_state, self._snapshot(), {}, True,
                )
        guard_work = bool(
            automation_work
            or not _already_in_desired_state(climate_state, _main_hvac_desired_state(action.desired_state))
            or any(state != action.desired_state.get("zone_power", {}).get(entity, "on")
                   for entity, state in saved_zone_states.items() if not entity.startswith("climate."))
            or (
                synchronize_zone_temperatures
                and any(
                    (zone := self._state(entity)) is not None
                    and not _already_in_desired_state(zone, {"target_temperature": target} if (
                        target := action.desired_state.get("zone_targets", {}).get(entity)
                    ) is not None else _temperature_desired_state(action.desired_state))
                    for entity in self._synchronized_zone_climate_entities()
                )
            )
        )
        # A refresh with no service writes must not renew shared coverage. Each
        # actual write also checks again after any preceding confirmation wait.
        if guard_work and not await self._async_arm_scheduler_guard():
            self._check_pending_takeover_capability()
            await self._async_persist_requested_manual_supersessions()
            self._check_pending_takeover_capability()
            if synchronized_takeover:
                raise _HVACTakeoverBlocked([], pre_dispatch_reason="climate_scheduler_guard_failed")
            superseded_zones = self._manual_zone_override_entity_ids()
            return HVACCommandResult(
                False,
                "climate_scheduler_guard_failed",
                pre_state,
                self._snapshot(),
                restorable_automation_states,
                False,
                {
                    entity_id: state
                    for entity_id, state in saved_zone_states.items()
                    if entity_id not in superseded_zones
                },
            )
        manual_override_result = await self._async_manual_override_result_if_requested(
            pre_state,
            {},
            {},
            main_entity=climate_entity,
            saved_main_state=saved_main_state,
        )
        if manual_override_result is not None:
            return manual_override_result
        self._check_pending_takeover_capability()
        suppression = await self._async_disable_automations(saved_automation_states)
        changed_automations = suppression.restoration_states
        automation_dispatched = suppression.dispatched
        # A failed, dispatched suppression still needs automation-only recovery,
        # including when a user supersedes the acquisition during its service call.
        if self._pending_automation_off is None:
            manual_override_result = await self._async_manual_override_result_if_requested(
                pre_state,
                changed_automations,
                {},
                main_entity=climate_entity,
                saved_main_state=saved_main_state,
                automation_dispatched=automation_dispatched,
            )
            if manual_override_result is not None:
                return manual_override_result
        if not suppression.confirmed:
            issues = self.observe_takeover_capability()
            if synchronized_takeover:
                # No HVAC actuator was touched. Persist automation-only recovery
                # before compensation, which can itself yield to a Dry transition.
                blocked = _HVACTakeoverBlocked(
                    issues, failed_suppression_pre_state=pre_state,
                    automation_off_pending=suppression.automation_off_pending,
                )
                blocked.record_automation_work(changed_automations, dispatched=automation_dispatched)
                raise blocked
            rollback_succeeded, unresolved_states = await self._async_enable_automation_entities(changed_automations)
            return HVACCommandResult(
                False,
                ("hvac_automation_service_failed" if rollback_succeeded else "hvac_automation_rollback_failed"),
                pre_state,
                self._snapshot(),
                unresolved_states,
                rollback_succeeded,
                saved_zone_states,
            )
        automation_suppressed = suppression.suppressed_active_automation
        if (
            action.desired_state.get("suppress_automations")
            and action.desired_state.get("hvac_mode") is None
            and action.desired_state.get("target_temperature") is None
            and not action.desired_state.get("enable_zones")
        ):
            return HVACCommandResult(
                True,
                "hvac_automations_suppressed" if automation_suppressed else "already_in_desired_hvac_state",
                pre_state,
                self._snapshot(),
                changed_automations,
                command_sent=automation_suppressed,
            )
        try:
            with self._check_takeover_readbacks():
                zones_enabled, changed_zones, late_block = await self._async_enable_zones(saved_zone_states)
        except _HVACTakeoverBlocked as blocked:
            blocked.record_automation_work(changed_automations, dispatched=automation_dispatched)
            raise
        manual_override_result = await self._async_manual_override_result_if_requested(
            pre_state,
            changed_automations,
            changed_zones,
            main_entity=climate_entity,
            saved_main_state=saved_main_state,
            automation_dispatched=automation_dispatched,
        )
        if manual_override_result is not None:
            return manual_override_result
        if not zones_enabled:
            if action.desired_state.get("zone_intent"):
                # Never continue demand while an intended cold-room closure is
                # uncertain. Executor retains the original durable baseline and
                # immediately uses the established restoration transaction.
                reason = late_block.reason if late_block is not None else "zone_subset_confirmation_failed"
                self._observe_followup_takeover()
                return HVACCommandResult(False, reason, pre_state, self._snapshot(),
                                         changed_automations, False, saved_zone_states, saved_main_state,
                                         command_sent=not self._awaiting_hvac_actuator,
                                         capability_issues=self._late_takeover_issues)
            failure_reason = late_block.reason if late_block is not None else "hvac_zone_service_failed"
            return await self._async_rollback_takeover_result(
                pre_state,
                changed_automations,
                changed_zones,
                main_entity=climate_entity,
                saved_main_state=saved_main_state,
                reason=failure_reason,
            )
        command_sent = bool(
            automation_suppressed or any(entity_id.split(".", 1)[0] != "climate" for entity_id in changed_zones)
        )

        try:
            with self._check_takeover_readbacks():
                command_sent = (
                    await self._async_apply_hvac_state(
                        climate_entity,
                        action.desired_state,
                        takeover_main_state=saved_main_state,
                        takeover_write=True,
                    )
                    or command_sent
                )
                self._raise_if_manual_override_requested()
                state_confirmed = await self._async_confirm_complete_hvac_state(
                    climate_entity,
                    action.desired_state,
                )
            self._check_pending_takeover_capability()
            self._raise_if_manual_override_requested()
            if state_confirmed:
                self._check_followup_takeover()
        except _HVACTakeoverBlocked as blocked:
            blocked.record_automation_work(changed_automations, dispatched=automation_dispatched)
            raise
        except _HVACManualOverrideError:
            return await self._async_manual_override_result(
                pre_state,
                changed_automations,
                changed_zones,
                main_entity=climate_entity,
                saved_main_state=saved_main_state,
                automation_dispatched=automation_dispatched,
            )
        except Exception as error:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
            try:
                self._check_pending_takeover_capability()
            except _HVACTakeoverBlocked as blocked:
                blocked.record_automation_work(changed_automations, dispatched=automation_dispatched)
                raise blocked from error
            if isinstance(error, _HVACStateConfirmationError):
                if isinstance(error, _HVACFollowupWriteBlocked):
                    late_block = error
                state_confirmed = False
            else:
                if action.desired_state.get("zone_intent"):
                    self._observe_followup_takeover()
                    return HVACCommandResult(False, "zone_subset_service_failed", pre_state, self._snapshot(),
                                             changed_automations, False, saved_zone_states, saved_main_state,
                                             command_sent=not self._awaiting_hvac_actuator,
                                             capability_issues=self._late_takeover_issues)
                return await self._async_rollback_takeover_result(
                    pre_state,
                    changed_automations,
                    changed_zones,
                    main_entity=climate_entity,
                    saved_main_state=saved_main_state,
                    reason="hvac_control_service_failed",
                )
        confirmation_reason = late_block.reason if late_block is not None else "hvac_state_confirmation_failed"
        if not state_confirmed:
            if action.desired_state.get("zone_intent"):
                reason = late_block.reason if late_block is not None else "zone_subset_confirmation_failed"
                self._observe_followup_takeover()
                return HVACCommandResult(False, reason, pre_state, self._snapshot(),
                                         changed_automations, False, saved_zone_states, saved_main_state,
                                         command_sent=not self._awaiting_hvac_actuator,
                                         capability_issues=self._late_takeover_issues)
            return await self._async_rollback_takeover_result(
                pre_state,
                changed_automations,
                changed_zones,
                main_entity=climate_entity,
                saved_main_state=saved_main_state,
                reason=confirmation_reason,
            )
        confirmed_zone_intent = None
        if action.desired_state.get("zone_intent", {}).get("coast_isolation"):
            from .zone_control import confirmed_coast_intent
            # Confirmation awaits can deliver new room/switch feedback. Only
            # execution-time observations may establish the next drift baseline.
            confirmed_zone_intent = confirmed_coast_intent(
                self.hass, self.entry_data, self._zone_options, action.desired_state,
                self._zone_baseline, dt_util.utcnow(), require_head_off=True,
            )
            if confirmed_zone_intent is None:
                return HVACCommandResult(
                    False, "zone_coast_protection_lost", pre_state, self._snapshot(), changed_automations,
                    False, saved_zone_states, saved_main_state, command_sent=command_sent,
                )
        reason = "already_in_desired_hvac_state" if not command_sent else "hvac_action_applied"
        return HVACCommandResult(
            True,
            reason,
            pre_state,
            self._snapshot(),
            changed_automations,
            saved_zone_states=saved_zone_states,
            command_sent=command_sent,
            confirmed_zone_intent=confirmed_zone_intent,
        )

    async def async_restore(
        self,
        saved_automation_states: dict[str, str] | None = None,
        saved_zone_states: dict[str, Any] | None = None,
        saved_main_state: dict[str, Any] | None = None,
    ) -> HVACCommandResult:
        """Restore transactionally, with independent off cleanup on every exception."""
        self._acquisition_guard = None
        try:
            return await self._async_restore(saved_automation_states, saved_zone_states, saved_main_state)
        except BaseException as error:
            main = str(self.entry_data.get(CONF_DAIKIN_CLIMATE))
            if self._command_callback is not None:
                self._command_callback(
                    {
                        "entity_id": main,
                        "domain": "climate",
                        "service": "restore",
                        "context_id": Context().id,
                        "stage": "restoration_failed",
                        "failure_category": type(error).__name__,
                    }
                )
            if (saved_main_state or {}).get("hvac_mode") == "off" and not (
                self._manual_override_requested is not None and self._manual_override_requested()
            ):
                with preserve_cancellation(error):
                    await self._async_apply_hvac_state(
                        main, {"hvac_mode": "off"}, respect_zone_manual_override=False, emergency_off=True,
                    )
            raise

    async def _async_restore(
        self,
        saved_automation_states: dict[str, str] | None = None,
        saved_zone_states: dict[str, Any] | None = None,
        saved_main_state: dict[str, Any] | None = None,
    ) -> HVACCommandResult:
        """Release HVAC ownership by restoring captured actuator state."""
        # Lost room feedback must not block compensation to original settings.
        # Physical airflow and manual supersession are still checked per write.
        self._zone_desired = {}
        await self._async_reconcile_recovery()
        self._manual_supersession_persisted = False
        self._persisted_zone_supersessions.clear()
        pre_state = self._snapshot()
        states = dict(saved_automation_states or {})
        zones = dict(saved_zone_states or {})
        main_state = dict(saved_main_state or {})
        had_saved_settings = bool(states or zones or main_state)
        if main_state.get("recovery_version", 1) != 1:
            return HVACCommandResult(
                False,
                "hvac_recovery_version_unknown",
                pre_state,
                self._snapshot(),
                states,
                rollback_succeeded=False,
                saved_zone_states=zones,
                saved_main_state=main_state,
            )
        # Retain blocked targets without repeatedly arming the scheduler guard.
        # Still restore any other actuator or automation that can be released.
        for entity_id in list(zones):
            if await self._async_zone_restore_is_superseded(entity_id):
                zones.pop(entity_id)
        if (not main_state and zones and isinstance(identity := self._zone_baseline.get("off_start_identity"), dict)
                and set(identity) == {"entry_id", "controller_fingerprint"}):
            # Manual head supersession cannot make an inactive mode visible.
            # Fresh vendor readback can still prove that its owned zone memory
            # already matches, without selecting a mode or reopening a room.
            observed_memory = await read_memory(self.hass, self.entry_data, "cool", allow_active_verification=True)
            if (observed_memory is not None
                    and all(observed_memory.get(key) == value for key, value in identity.items())):
                zones = {entity: target for entity, target in zones.items() if not (
                    isinstance(target, dict) and target.get("baseline_hvac_mode") in {"heat", "cool"}
                    and observed_memory["modes"][target["baseline_hvac_mode"]]["zones"].get(entity)
                    == target.get("target_temperature")
                    or target in ("on", "off") and observed_memory["power"].get(entity) == target
                )}
        main_observed = self._state(str(self.entry_data.get(CONF_DAIKIN_CLIMATE)))
        deferred_recovery = (
            not main_state.get(MEMORY_KEY)
            and main_state.get("zone_recovery_attempted") is True
            and main_observed is not None
            and main_observed.state == "off"
            and main_state.get("hvac_mode") == "off"
        )
        if (
            main_state
            and not main_state.get(MEMORY_KEY)
            and _main_hvac_desired_state(main_state)
            and main_observed is not None
            and _already_in_desired_state(main_observed, main_state)
            and not self._unconfirmed_main_mode
            and (not main_state.get(_ROLLBACK_HVAC_MODE_CHANGED) or deferred_recovery)
        ):
            # Verify dependent targets before deciding whether the head needs
            # waking. A completed recovery must not undo its own shutdown.
            zones = {
                entity: target
                for entity, target in zones.items()
                if not (
                    entity.startswith("climate.")
                    and isinstance(target, dict)
                    and _has_temperature_target_fields(target)
                    and (zone := self._state(entity)) is not None
                    and _already_in_desired_state(zone, target)
                )
            }
            if not zones:
                main_state = {}
        deferred_only = bool(
            zones
            and not main_state.get(MEMORY_KEY)
            and (not main_state or deferred_recovery)
            and not self._can_restore_paired_targets(zones)
            and all(
                self._zone_restore_deferred(entity_id, target)
                for entity_id, target in zones.items()
                if entity_id.startswith("climate.")
            )
        )
        actionable_zones: dict[str, Any] = {}
        deferred_automations: dict[str, str] = {}
        if deferred_only:
            # Switch recovery must not wake a head whose shutdown is confirmed.
            # Keep unavailable controls pending, without dispatch or guard work.
            zones = self._retain_paired_zone_power(zones, {
                entity: target
                for entity, target in zones.items()
                if entity.startswith("climate.") or self._state_value(entity) != target
            })
            actionable_zones = {
                entity: target
                for entity, target in zones.items()
                if not entity.startswith("climate.") and self._state(entity) is not None
                and self._state_value(entity) != target
            }
            states = {
                entity: desired for entity, desired in states.items() if self._automation_needs_restore(entity, desired)
            }
            deferred_automations = {
                entity: desired for entity, desired in states.items() if self._state(entity) is None
            }
            states = {entity: desired for entity, desired in states.items() if entity not in deferred_automations}
        if deferred_only and not states and not actionable_zones:
            for entity_id, target in zones.items():
                self._record_zone_deferred(entity_id, target)
            operator_required = self._zone_restore_needs_operator(zones, main_state)
            if operator_required:
                main_state["zone_recovery_blocker"] = "saved_zone_targets_incompatible_with_off_baseline"
            else:
                main_state.pop("zone_recovery_blocker", None)
            return HVACCommandResult(
                not zones and not deferred_automations,
                "hvac_zone_restore_operator_required" if operator_required else (
                    "hvac_zone_restore_pending" if zones or deferred_automations else "hvac_control_released"
                ),
                pre_state,
                self._snapshot(),
                deferred_automations,
                rollback_succeeded=not zones and not deferred_automations,
                saved_zone_states=zones,
                saved_main_state=main_state if zones else {},
            )
        off_start_retry = main_state.get(MEMORY_KEY) and main_state.get("off_start_restore_attempted")
        if (
            (not off_start_retry and (zones or main_state)) or any(
                self._automation_needs_restore(entity, desired) for entity, desired in states.items()
            )
        ) and not await self._async_arm_scheduler_guard():
            await self._async_persist_requested_manual_supersessions()
            main_superseded = self._manual_override_requested is not None and self._manual_override_requested()
            memory = main_state.get(MEMORY_KEY)
            if isinstance(memory, dict) and not main_superseded:
                from .daikin_memory import provider
                source = provider(self.hass, self.entry_data)
                if (source is not None and source_matches(self.hass, self.entry_data, memory)
                        and self.entry_data.get(CONF_DAIKIN_CLIMATE) == memory.get("head")):
                    # Helper failure cannot strand planner-owned demand with a
                    # proven original Off baseline. Do not select modes here.
                    await self._async_apply_hvac_state(
                        memory["head"], {"hvac_mode": "off"}, respect_zone_manual_override=False,
                        emergency_off=True, force=self._unconfirmed_main_mode,
                    )
            superseded_zones = self._manual_zone_override_entity_ids()
            return HVACCommandResult(
                False,
                "climate_scheduler_guard_failed",
                pre_state,
                self._snapshot(),
                {**states, **deferred_automations},
                False,
                {entity_id: state for entity_id, state in zones.items() if entity_id not in superseded_zones},
                {} if main_superseded else main_state,
            )
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        if deferred_only:
            for entity_id, target in zones.items():
                if not entity_id.startswith("climate."):
                    continue
                self._record_zone_deferred(entity_id, target)
            _, unfinished_zones = await self._async_restore_zone_states(actionable_zones)
            unresolved_zones = {entity: target for entity, target in zones.items() if entity not in actionable_zones}
            unresolved_zones.update(unfinished_zones)
            unresolved_zones = self._retain_paired_zone_power(zones, unresolved_zones)
            main_restored, zones_restored = True, not unresolved_zones
        else:
            main_restored, zones_restored, unresolved_zones = await self._async_restore_main_and_zones(
                str(climate_entity),
                main_state,
                zones,
            )
        # A successful shutdown can hide unresolved zone targets. Keep the
        # original thermostat and remembered mode until those targets resolve.
        unresolved_main_state = {} if main_restored and zones_restored else main_state
        if main_restored and unresolved_zones and unresolved_main_state:
            unresolved_main_state = {**unresolved_main_state, "zone_recovery_attempted": True, "recovery_version": 1}
        await self._async_persist_requested_manual_supersessions()
        restored, unresolved_states = await self._async_enable_automation_entities(states)
        unresolved_states.update(deferred_automations)
        restored = restored and not deferred_automations
        await self._async_persist_requested_manual_supersessions()
        reason = "no_hvac_automation_state_saved"
        if not main_restored or not restored or not zones_restored:
            reason = "hvac_release_failed"
            if unresolved_main_state.get("zone_recovery_blocker") == "off_start_restore_waiting_for_feedback":
                reason = "hvac_zone_restore_pending"
            elif unresolved_main_state.get(MEMORY_KEY) and unresolved_main_state.get("zone_recovery_blocker"):
                reason = "hvac_zone_restore_operator_required"
            elif self._zone_restore_needs_operator(unresolved_zones, unresolved_main_state):
                unresolved_main_state["zone_recovery_blocker"] = "saved_zone_targets_incompatible_with_off_baseline"
                reason = "hvac_zone_restore_operator_required"
            else:
                unresolved_main_state.pop("zone_recovery_blocker", None)
        elif had_saved_settings or self._automation_entities():
            reason = "hvac_control_released"
        return HVACCommandResult(
            applied=(bool(had_saved_settings or self._automation_entities()))
            and main_restored
            and restored
            and zones_restored,
            reason=reason,
            pre_state=pre_state,
            post_state=self._snapshot(),
            saved_automation_states=unresolved_states,
            rollback_succeeded=main_restored and restored and zones_restored,
            saved_zone_states=unresolved_zones,
            saved_main_state=unresolved_main_state,
        )

    async def _async_arm_scheduler_guard(self, *, deadline: datetime | None = None) -> bool:
        """Cover one original command lease plus the external classifier settle.

        Every write checks coverage. Read-only polling never renews this lease.
        The timer is shared: reuse longer coverage, and never cancel or clear it
        on completion, failure or cancellation. Its finite expiry owns cleanup.
        """
        guard_entity = self.entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER)
        timer_entity = self.entry_data.get(CONF_CLIMATE_SCHEDULER_GUARD_TIMER)
        if not guard_entity and not timer_entity:
            return True
        if not guard_entity or not timer_entity:
            return False
        now = dt_util.utcnow()
        if deadline is None:
            deadline = now + timedelta(seconds=_STATE_RECONCILIATION_TIMEOUT_SECONDS)
        required_end = deadline + timedelta(seconds=_SCHEDULER_GUARD_SETTLE_SECONDS)
        timer = self._state(timer_entity)
        if timer is not None and timer.state not in {"idle", "active"}:
            # A paused shared timer belongs to its existing resume workflow.
            return False
        end = None
        if timer is not None and timer.state == "active":
            end = dt_util.parse_datetime(str(timer.attributes.get("finishes_at", "")))
            # Without a finite end we cannot safely restart a shared timer.
            if end is None or end.tzinfo is None:
                return False
        try:
            if end is None or end < required_end:
                await self._async_command(
                    self.hass, "timer", "start",
                    {
                        ATTR_ENTITY_ID: timer_entity,
                        # HA accepts seconds. Round up and allow one second for
                        # timer service handling without moving the intent deadline.
                        "duration": max(1, ceil((required_end - now).total_seconds()) + 1),
                    },
                    blocking=True,
                    scheduler_guard=True,
                )
                if not await self._async_confirm_state(timer_entity, "active"):
                    return False
            if self._state_value(guard_entity) != "on":
                await self._async_command(
                    self.hass, "input_boolean", SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: guard_entity}, blocking=True, scheduler_guard=True,
                )
                if not await self._async_confirm_state(guard_entity, "on"):
                    return False
            timer = self._state(timer_entity)
            end = None if timer is None else dt_util.parse_datetime(str(timer.attributes.get("finishes_at", "")))
            return bool(
                timer is not None and timer.state == "active"
                and end is not None and end.tzinfo is not None and end >= required_end
                and dt_util.utcnow() < deadline
            )
        except Exception:  # noqa: BLE001 - a missing guard must prevent actuator commands.
            return False

    async def _async_enable_automation_entities(
        self,
        saved_states: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, str]]:
        """Enable climate automations that were active before takeover."""
        unresolved: dict[str, str] = {}
        for entity_id, desired in (saved_states or {}).items():
            if not self._automation_needs_restore(entity_id, desired):
                continue
            try:
                await self._async_command(
                    self.hass, "automation", SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id},
                    blocking=True, respect_manual_override=False,
                )
            except Exception:  # noqa: BLE001
                await self._async_persist_requested_manual_supersessions()
                unresolved[entity_id] = "on"
                continue
            confirmed = await self._async_confirm_state(entity_id, "on")
            await self._async_persist_requested_manual_supersessions()
            if not confirmed:
                unresolved[entity_id] = "on"
        return not unresolved, unresolved

    def _automation_needs_restore(self, entity_id: str, desired: str) -> bool:
        # Core disables the automation before awaiting script cancellation. A
        # cached On can outlive that change, so re-enable an unsettled suppression
        # explicitly. Later state publication reflects Core's current enable flag.
        return desired == "on" and (
            entity_id in (self._pending_automation_off, self._zone_baseline.get(HVAC_PENDING_AUTOMATION_OFF))
            or self._state_value(entity_id) != "on"
        )

    async def _async_enable_zones(
        self, states: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], _HVACFollowupWriteBlocked | None]:
        """Enable configured zone entities and return those changed."""
        changed: dict[str, Any] = {}
        power = self._zone_desired.get("zone_power", {})
        # Confirm new airflow paths before closing any old ones.
        for entity_id, state in sorted(states.items(), key=lambda item: power.get(item[0], "on") == "off"):
            if entity_id.split(".", 1)[0] == "climate":
                # Target snapshots are restored if any later command in the
                # takeover transaction fails.
                changed[entity_id] = state
                continue
            requested = power.get(entity_id, "on")
            if state == requested:
                continue
            changed[entity_id] = state
            service_context = Context() if self._set_coupled_zone_feedback_expected is not None else None
            if self._set_coupled_zone_feedback_expected is not None:
                self._set_coupled_zone_feedback_expected(
                    entity_id,
                    requested,
                    service_context.id if service_context is not None else None,
                )
            try:
                previous_command = self._last_commands.get(entity_id)
                try:
                    service_data = {ATTR_ENTITY_ID: entity_id}
                    await self._async_command(
                        self.hass,
                        entity_id.split(".", 1)[0],
                        SERVICE_TURN_ON if requested == "on" else SERVICE_TURN_OFF,
                        service_data,
                        blocking=True,
                        takeover_write=True,
                        context=service_context,
                    )
                except _HVACTakeoverBlocked:
                    raise
                except _HVACFollowupWriteBlocked as blocked:
                    changed.pop(entity_id)
                    return False, changed, blocked
                except Exception:  # noqa: BLE001
                    if self._command_dispatched_since(entity_id, previous_command) and self._synchronized_takeover():
                        # The service failure may precede acknowledgement of an accepted zone change.
                        # Keep the service failure as the outcome, and wait before restoring its baseline.
                        self._observe_followup_takeover()
                        try:
                            await self._async_confirm_state(entity_id, requested)
                        except (_HVACFollowupWriteBlocked, _HVACManualOverrideError):
                            pass
                    return False, changed, None
                try:
                    # A head or sibling override can arrive before this zone's
                    # write is visible. Settle it before checking that override.
                    if not self._synchronized_takeover() or entity_id in self._manual_zone_override_entity_ids():
                        self._raise_if_manual_override_requested()
                    if not await self._async_confirm_state(entity_id, requested):
                        return False, changed, None
                    self._raise_if_manual_override_requested()
                except _HVACFollowupWriteBlocked as blocked:
                    return False, changed, blocked
                except _HVACManualOverrideError:
                    return False, changed, None
            finally:
                if self._set_coupled_zone_feedback_expected is not None:
                    self._set_coupled_zone_feedback_expected(None, None, None)
        return True, changed, None

    def _zone_restore_deferred(self, entity_id: str, target: Any) -> bool:
        """Wait for a zone to expose a target and bounds compatible with its baseline."""
        if isinstance(target, dict) and target.get("baseline_hvac_mode"):
            head = self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE))
            if head is None or head.state != target["baseline_hvac_mode"]:
                return True
        if not entity_id.startswith("climate.") or not isinstance(target, dict):
            return False
        observed = self._state(entity_id)
        if observed is None or zone_temperature_sync_deferred(observed):
            return True
        # Bounds constrain new commands, not confirmation of an already restored baseline.
        if (
            _has_temperature_target_fields(target)
            and _mode_matches(observed, target.get("hvac_mode"))
            and _temperature_matches(observed, target.get("target_temperature"))
            and _temperature_range_matches(observed, target.get("target_temp_low"), target.get("target_temp_high"))
        ):
            return False
        return not self._zone_target_within_bounds(observed, target)

    @staticmethod
    def _zone_target_within_bounds(observed: State, target: dict[str, Any]) -> bool:
        """Check published bounds before requesting or exposing a saved target."""
        attributes = observed.attributes
        try:
            values = [float(value) for value in _temperature_desired_state(target).values()]
            low = float(attributes.get("min_temp", float("-inf")))
            high = float(attributes.get("max_temp", float("inf")))
            return all(isfinite(value) and low <= value <= high for value in values)
        except (TypeError, ValueError, OverflowError):
            return False

    async def _async_restore_zone_states(self, states: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Restore captured zone states."""
        unresolved: dict[str, Any] = {}
        for entity_id, state in states.items():
            if (state == "on" and not entity_id.startswith("climate.")
                    and not self._zone_power_restore_can_open()):
                # Opening a Daikin zone can wake a manually superseded head.
                # Without observed active mode, retain the On baseline.
                if self._state_value(entity_id) != "on":
                    unresolved[entity_id] = state
                continue
            if await self._async_zone_restore_is_superseded(entity_id):
                continue
            if entity_id.startswith("climate.") and self._zone_restore_deferred(entity_id, state):
                # Bounds arrive asynchronously after restoring the head target.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _STATE_CONFIRMATION_TIMEOUT_SECONDS
                while self._zone_restore_deferred(entity_id, state) and loop.time() < deadline:
                    if await self._async_zone_restore_is_superseded(entity_id):
                        break
                    await asyncio.sleep(_STATE_CONFIRMATION_POLL_SECONDS)
            if await self._async_zone_restore_is_superseded(entity_id):
                continue
            if self._zone_restore_deferred(entity_id, state):
                self._record_zone_deferred(entity_id, state)
                unresolved[entity_id] = state
                continue
            confirmed = False
            if entity_id.split(".", 1)[0] == "climate":
                if not isinstance(state, dict) or not _has_temperature_target_fields(state):
                    # Empty or malformed persisted target evidence cannot prove
                    # that a previously changed zone was restored.
                    unresolved[entity_id] = state
                    continue
                try:
                    await self._async_apply_hvac_state(
                        entity_id,
                        state,
                        respect_manual_override=False,
                    )
                    confirmed = await self._async_confirm_hvac_state(
                        entity_id,
                        state,
                    )
                except Exception:  # noqa: BLE001 - retain target snapshot for a later release retry.
                    pass
            else:
                observed = self._state(entity_id)
                if observed is not None and observed.state == state:
                    continue
                service = SERVICE_TURN_ON if state == "on" else SERVICE_TURN_OFF
                service_context = Context() if self._set_coupled_zone_feedback_expected is not None else None
                if self._set_coupled_zone_feedback_expected is not None:
                    self._set_coupled_zone_feedback_expected(
                        entity_id,
                        state,
                        service_context.id if service_context is not None else None,
                    )
                try:
                    service_data = {ATTR_ENTITY_ID: entity_id}
                    if service_context is None:
                        await self._async_command(
                            self.hass,
                            entity_id.split(".", 1)[0],
                            service,
                            service_data,
                            blocking=True,
                            respect_manual_override=False,
                            restoring_zone_power=True,
                        )
                    else:
                        await self._async_command(
                            self.hass,
                            entity_id.split(".", 1)[0],
                            service,
                            service_data,
                            blocking=True,
                            respect_manual_override=False,
                            restoring_zone_power=True,
                            context=service_context,
                        )
                    confirmed = await self._async_confirm_state(
                        entity_id,
                        state,
                    )
                except Exception:  # noqa: BLE001 - retain state for a later release retry.
                    pass
                finally:
                    if self._set_coupled_zone_feedback_expected is not None:
                        self._set_coupled_zone_feedback_expected(None, None, None)
            if await self._async_zone_restore_is_superseded(entity_id):
                continue
            if not confirmed:
                unresolved[entity_id] = state
        return not unresolved, unresolved

    async def _async_zone_restore_is_superseded(self, entity_id: str) -> bool:
        """Persist and identify a zone whose user state must be preserved."""
        await self._async_persist_requested_manual_supersessions()
        if entity_id not in self._manual_zone_override_entity_ids():
            return False
        return True

    async def _async_rollback_takeover_result(
        self,
        pre_state: dict[str, Any],
        changed_automations: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
        reason: str,
    ) -> HVACCommandResult:
        """Restore a failed takeover and retain any late capability evidence."""
        self._observe_followup_takeover()
        command_sent = not self._awaiting_hvac_actuator
        rollback_succeeded, unresolved_states, unresolved_zones, unresolved_main = await self._async_rollback_takeover(
            changed_automations, changed_zones, main_entity=main_entity, saved_main_state=saved_main_state,
        )
        return HVACCommandResult(
            False,
            reason if rollback_succeeded else "hvac_acquisition_rollback_failed",
            pre_state,
            self._snapshot(),
            unresolved_states,
            rollback_succeeded,
            unresolved_zones,
            unresolved_main,
            command_sent=command_sent,
            capability_issues=self._late_takeover_issues,
        )

    async def _async_rollback_takeover(
        self,
        saved_automation_states: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
        restore_main: bool = True,
    ) -> tuple[bool, dict[str, str], dict[str, Any], dict[str, Any]]:
        """Release every actuator acquired before a climate command failed."""
        self._acquisition_guard = None
        await self._async_persist_requested_manual_supersessions()
        # Supersession persistence can deliver the first Dry feedback.
        self._observe_followup_takeover()
        pending = self._recovery_confirmation
        if self._late_takeover_issues and pending is not None:
            last_command = self._last_commands.get(pending["entity_id"], {})
            if last_command.get("dispatched") is True and last_command.get("context_id") == pending["context_id"]:
                # Settle an accepted write under its original lease before issuing compensation.
                await self._async_reconcile_recovery()
                self._observe_followup_takeover()
        if restore_main and self._set_pending_main_restore is not None:
            self._set_pending_main_restore(dict(saved_main_state))
        if self._set_pending_zone_restore is not None:
            self._set_pending_zone_restore(dict(changed_zones))
        main_restored, zones_restored, unresolved_zones = await self._async_restore_main_and_zones(
            main_entity, saved_main_state if restore_main else {}, changed_zones,
        )
        await self._async_persist_requested_manual_supersessions()
        automations_restored, unresolved_states = await self._async_enable_automation_entities(saved_automation_states)
        await self._async_persist_requested_manual_supersessions()
        self._observe_followup_takeover()
        unresolved_main_state = {} if (main_restored and zones_restored) or not restore_main else saved_main_state
        if main_restored and unresolved_zones and unresolved_main_state:
            unresolved_main_state = {**unresolved_main_state, "zone_recovery_attempted": True, "recovery_version": 1}
        return (
            main_restored and zones_restored and automations_restored,
            unresolved_states,
            unresolved_zones,
            unresolved_main_state,
        )

    def _can_restore_paired_targets(self, zones: dict[str, Any]) -> bool:
        """Allow owned zone recovery under an already-active, possibly user-controlled head."""
        head = self._state(str(self.entry_data.get(CONF_DAIKIN_CLIMATE)))
        return bool(head is not None and head.state in _ACTIVE_HVAC_MODES and self._paired_target_open_states(zones))

    def _paired_target_open_states(self, zones: dict[str, Any]) -> dict[str, str]:
        """Open only owned pairs whose unfinished target fits the current head's bounds."""
        result = {}
        for actuator, climate in self._independent_pairs().items():
            target = zones.get(climate)
            if isinstance(target, dict) and target.get("baseline_hvac_mode"):
                head = self._state(self.entry_data.get(CONF_DAIKIN_CLIMATE))
                if head is None or head.state != target["baseline_hvac_mode"]:
                    continue
            observed = self._state(climate)
            if (actuator in zones and isinstance(target, dict) and _has_temperature_target_fields(target)
                    and observed is not None and not _already_in_desired_state(observed, target)
                    and self._zone_target_within_bounds(observed, target)):
                result[actuator] = "on"
        return result

    def _retain_paired_zone_power(self, saved: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
        """Keep power needed to expose an unfinished target without acquiring an excluded actuator."""
        result = dict(pending)
        for actuator, climate in self._independent_pairs().items():
            if climate in result and actuator in saved:
                result.setdefault(actuator, saved[actuator])
        return result

    async def _async_restore_off_start(
        self, main_entity: str, main_state: dict[str, Any], zones: dict[str, Any],
    ) -> tuple[bool, bool, dict[str, Any]]:
        """One bounded, durable mode-aware restoration; failed shutdown never probes."""
        memory = main_state[MEMORY_KEY]
        from .daikin_memory import provider, source_identity
        from .zone_control import topology
        source = provider(self.hass, self.entry_data, require_available=False)
        valid = (memory.get("version") == 1 and memory.get("head") == main_entity
                 and source is not None
                 and all(memory.get(key) == value for key, value in source_identity(source).items())
                 and topology(self.entry_data).get("pairs") == memory.get("pairs"))
        if not valid:
            main_state["zone_recovery_blocker"] = "off_start_provider_identity_changed"
            return False, False, dict(zones)
        state = self._state_value(main_entity)
        if state is None or provider(self.hass, self.entry_data) is None:
            main_state["zone_recovery_blocker"] = "off_start_restore_waiting_for_feedback"
            return False, False, dict(zones)
        if main_state.get("off_start_restore_attempted"):
            if state != "off" or self._unconfirmed_main_mode:
                # A known Off baseline permits shutdown only. Keep the
                # captured identity enforced across guard and Store awaits.
                self._restoring_memory = memory
                try:
                    stopped = await self._async_restore_main_state_preserving_manual(
                        main_entity, {"hvac_mode": "off"}, emergency_off=True,
                    )
                finally:
                    self._restoring_memory = None
                if not stopped and self._state_value(main_entity) is None:
                    main_state["zone_recovery_blocker"] = "off_start_restore_waiting_for_feedback"
                    return False, False, dict(zones)
            if self._state_value(main_entity) == "off" and not self._unconfirmed_main_mode:
                observed = await read_memory(self.hass, self.entry_data, memory["acquired_mode"])
                if observed is None:
                    main_state["zone_recovery_blocker"] = "off_start_restore_waiting_for_feedback"
                    return False, False, dict(zones)
                if memory_matches(observed, memory, zones):
                    return True, True, {}
            main_state["zone_recovery_blocker"] = "off_start_restore_operator_required"
            return False, False, dict(zones)
        main_state["off_start_restore_attempted"] = True
        main_state["zone_recovery_blocker"] = "off_start_restore_incomplete"
        pending = dict(zones)
        self._restoring_memory = memory
        self._restoring_owned_zone_entities = set(zones)
        progress = _OffStartRestoreProgress()
        self._restoring_off_start = progress
        marker_saved = False
        interruption: BaseException | None = None
        main_ok = False
        try:
            if self._async_persist_main_state is not None:
                await self._async_persist_main_state(deepcopy(main_state))
            marker_saved = True
            mode = memory["acquired_mode"]
            normal = memory["modes"][mode]
            current_power = {actuator: self._state_value(actuator) for actuator in memory["pairs"]}
            current_open = {actuator for actuator, power in current_power.items() if power == "on"}
            config = topology(self.entry_data)
            safe = any(current_open == set(row) for row in config["safe_open_sets"])
            if any(power not in {"on", "off"} for power in current_power.values()):
                return False, False, pending
            if not safe and self._state_value(main_entity) != "off":
                if not await self._async_restore_main_state_preserving_manual(main_entity, {"hvac_mode": "off"}):
                    return False, False, pending
            if self._state_value(main_entity) == "off":
                self._off_start_activation = await read_activation(self.hass, self.entry_data)
                if self._off_start_activation is None:
                    return False, False, pending
            # Keep an already safe subset. Capturing a closed thermostat's
            # memory does not authorize reopening that room for restoration.
            candidates = [set(row) for row in config["safe_open_sets"]
                          if current_open <= set(row) and set(row) - current_open <= zones.keys()]
            if not safe:
                if not candidates:
                    return False, False, pending
                opened, _ = await self._async_restore_zone_states({
                    actuator: "on" for actuator in min(candidates, key=lambda row: (
                        -sum(zones.get(entity) == "on" for entity in row), len(row), sorted(row)))
                    if actuator not in current_open
                })
                if not opened:
                    return False, False, pending
            stage = {"hvac_mode": mode, "target_temperature": normal["target_temperature"]}
            if self._set_pending_main_restore is not None:
                self._set_pending_main_restore(stage)
            if not await self._async_restore_main_state_preserving_manual(main_entity, stage):
                return False, False, pending
            targets = {entity: {"target_temperature": target, "baseline_hvac_mode": mode}
                       for entity, target in normal["zones"].items() if entity in zones}
            _, unfinished = await self._async_restore_zone_states(targets)
            pending = {entity: value for entity, value in zones.items() if not entity.startswith("climate.")}
            pending.update(unfinished)
            selected = memory["selected_mode"]
            # Mode recall must be restored separately from the mode whose
            # temperature was changed; generic off/19 cannot restore cool/23.
            final = {"hvac_mode": selected,
                     "target_temperature": memory["modes"][selected]["target_temperature"]}
            if self._set_pending_main_restore is not None:
                self._set_pending_main_restore(final)
            main_ok = await self._async_restore_main_state_preserving_manual(main_entity, final)
            if main_ok:
                # Reopen original On paths under the original remembered mode,
                # then confirm Off before closing original Off paths. Opening
                # an On damper after shutdown could otherwise wake the head.
                _, unfinished_on = await self._async_restore_zone_states({
                    entity: value for entity, value in pending.items()
                    if not entity.startswith("climate.") and value == "on"
                })
                main_ok = await self._async_restore_main_state_preserving_manual(main_entity, {"hvac_mode": "off"})
                unfinished_power = {entity: value for entity, value in pending.items()
                                    if not entity.startswith("climate.") and value != "on"}
                if main_ok:
                    _, unfinished_power = await self._async_restore_zone_states(unfinished_power)
                pending = {entity: value for entity, value in pending.items() if entity.startswith("climate.")}
                pending.update(unfinished_on)
                pending.update(unfinished_power)
        except BaseException as error:
            interruption = error
            raise
        finally:
            # Even cancellation and malformed persisted evidence retain Off as
            # the known safe power baseline. Manual main supersession still wins.
            try:
                with preserve_cancellation(interruption):
                    if self._set_pending_main_restore is not None:
                        self._set_pending_main_restore(main_state)
                    await self._async_restore_main_state_preserving_manual(main_entity, {"hvac_mode": "off"})
            except BaseException as error:
                interruption = error
                raise
            finally:
                with preserve_cancellation(interruption):
                    self._restoring_off_start = None
                    self._restoring_memory = None
                    self._restoring_owned_zone_entities.clear()
                    self._off_start_activation = None
                    # A marker save that never returned could have committed.
                    # Restoration itself could not dispatch before that save.
                    if not progress.dispatched and (
                        progress.assumed_feedback or not marker_saved or isinstance(interruption, CancelledError)
                    ):
                        main_state.pop("off_start_restore_attempted", None)
                        main_state["zone_recovery_blocker"] = "off_start_restore_waiting_for_feedback"
                        if self._async_persist_main_state is not None:
                            await self._async_persist_main_state({
                                **deepcopy(main_state), "off_start_restore_attempted": False,
                            })
        if main_ok and not pending:
            # Verify all modes after the mandatory shutdown cleanup, rather
            # than claiming success from visible HA targets alone.
            observed = await read_memory(self.hass, self.entry_data, memory["acquired_mode"])
            if observed is None:
                main_state["zone_recovery_blocker"] = "off_start_restore_waiting_for_feedback"
                return False, False, dict(zones)
            if not memory_matches(observed, memory, zones):
                main_ok, pending = False, dict(zones)
        if pending or not main_ok:
            if main_state.get("off_start_restore_attempted"):
                main_state["zone_recovery_blocker"] = "off_start_restore_operator_required"
            # Partial acknowledgements preceded a mode change or a failed
            # shutdown. Keep the full owned set for the next native readback.
            return main_ok, False, dict(zones)
        return True, True, {}

    async def _async_restore_main_and_zones(
        self, main_entity: str, main_state: dict[str, Any], zones: dict[str, Any],
    ) -> tuple[bool, bool, dict[str, Any]]:
        """Restore dependent targets before removing the mode that exposes them."""
        if main_state.get(MEMORY_KEY):
            return await self._async_restore_off_start(main_entity, main_state, zones)
        if main_state and hvac_feedback_is_assumed(self.hass.states.get(main_entity)):
            return False, False, dict(zones)
        from .zone_control import topology
        config = topology(self.entry_data)
        independent = bool(config.get("supported") and zones
                           and (main_state or self._can_restore_paired_targets(zones)))
        open_states = ({entity: "on" for entity in config.get("pairs", {}) if entity in zones}
                       if main_state else self._paired_target_open_states(zones))
        if independent and main_state:
            # Never restart a restored/running head just to recover a zone.
            # When the head is already off, open paths before restoring a mode
            # that can demand. Unsafe current/original airflow stays fail closed.
            try:
                original_power = {entity: zones.get(entity, self._state_value(entity)) for entity in config["pairs"]}
                original_open = {entity for entity, power in original_power.items() if power == "on"}
                if (main_state.get("hvac_mode") != "off"
                        and (any(power not in {"on", "off"} for power in original_power.values())
                             or not any(original_open == set(row) for row in config["safe_open_sets"]))):
                    await self._async_restore_main_state_preserving_manual(main_entity, {"hvac_mode": "off"})
                    return False, False, dict(zones)
                observed = self._state(main_entity)
                current_power = {entity: self._state_value(entity) for entity in config["pairs"]}
                current_open = {entity for entity, power in current_power.items() if power == "on"}
                if (any(power not in {"on", "off"} for power in current_power.values())
                        or not any(current_open == set(row) for row in config["safe_open_sets"])):
                    if not await self._async_restore_main_state_preserving_manual(main_entity, {"hvac_mode": "off"}):
                        return False, False, dict(zones)
                    observed = self._state(main_entity)
                if observed is None or observed.state == "off":
                    opened, _ = await self._async_restore_zone_states(open_states)
                    if not opened:
                        return False, False, dict(zones)
            except Exception:  # noqa: BLE001 - preserve ownership until safe restoration confirms.
                return False, False, dict(zones)
        observed = self._state(main_entity)
        defer_off = (
            main_state.get("hvac_mode") == "off"
            and observed is not None
            and (
                observed.state in _ACTIVE_HVAC_MODES
                or (
                    main_state.get(_ROLLBACK_ACTIVE_HVAC_MODE) in _ACTIVE_HVAC_MODES
                    and _has_temperature_target_fields(main_state)
                )
            )
            and any(entity.startswith("climate.") for entity in zones)
        )
        staged_main = dict(main_state)
        if defer_off:
            assert observed is not None
            staged_main["hvac_mode"] = (
                main_state.get(_ROLLBACK_ACTIVE_HVAC_MODE, observed.state)
                if main_state.get(_ROLLBACK_HVAC_MODE_CHANGED) or observed.state == "off"
                else observed.state
            )
        off_restored = True
        try:
            await self._async_persist_requested_manual_supersessions()
            if self._set_pending_main_restore is not None:
                self._set_pending_main_restore(staged_main)
            main_restored = (
                await self._async_restore_main_state_preserving_manual(main_entity, staged_main)
                if main_state else True
            )
            await self._async_persist_requested_manual_supersessions()
            # A damper or the main off command can hide a zone's target. Restore
            # temperature snapshots first, then restore switches and main power.
            ordered_zones = dict(sorted(zones.items(), key=lambda item: not item[0].startswith("climate.")))
            if main_restored:
                if independent:
                    # Main settings now match the normal baseline. Only then
                    # expose hidden zone targets, without an extra head cycle.
                    opened, _ = await self._async_restore_zone_states(open_states)
                    if not opened:
                        return False, False, dict(zones)
                if config.get("supported") and defer_off:
                    # Targets may need an active mode, but original closed
                    # dampers must wait for a confirmed head shutdown.
                    _, unresolved_zones = await self._async_restore_zone_states({
                        entity: value for entity, value in ordered_zones.items() if entity.startswith("climate.")
                    })
                    off_restored = await self._async_restore_main_state_preserving_manual(main_entity, main_state)
                    powers = {entity: value for entity, value in ordered_zones.items()
                              if not entity.startswith("climate.")}
                    if off_restored:
                        _, pending_powers = await self._async_restore_zone_states(powers)
                        unresolved_zones.update(pending_powers)
                    else:
                        unresolved_zones.update(powers)
                    zones_restored = not unresolved_zones
                else:
                    zones_restored, unresolved_zones = await self._async_restore_zone_states(ordered_zones)
            else:
                zones_restored, unresolved_zones = not zones, dict(zones)
        finally:
            # Delaying off must not let a persistence error or task cancellation
            # strand planner-owned heating/cooling. Manual main changes still win.
            if defer_off:
                if self._set_pending_main_restore is not None:
                    self._set_pending_main_restore(main_state)
                final_state = {"hvac_mode": "off"} if config.get("supported") and off_restored else main_state
                off_restored = await self._async_restore_main_state_preserving_manual(main_entity, final_state)
        main_restored = main_restored and off_restored
        # A closed paired actuator may hide a failed target again. Retain its
        # original power alongside that target so a retry/reload can expose it
        # and still return the actuator to its exact original power afterward.
        unresolved_zones = self._retain_paired_zone_power(zones, unresolved_zones)
        await self._async_persist_requested_manual_supersessions()
        return main_restored, zones_restored, unresolved_zones

    async def _async_manual_override_result(
        self,
        pre_state: dict[str, Any],
        changed_automations: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
        automation_dispatched: bool,
    ) -> HVACCommandResult:
        """Roll back while preserving every user-superseded climate entity."""
        self._observe_followup_takeover("set_temperature")
        try:
            self._check_pending_takeover_capability()
            main_superseded = self._manual_override_requested is not None and self._manual_override_requested()
            superseded_zones = self._manual_zone_override_entity_ids()
            await self._async_persist_override_supersessions(main_superseded, superseded_zones)
            # Settle any accepted head command before compensating it, even
            # if a manual change superseded only a zone.
            await self._async_reconcile_recovery()
            self._check_pending_takeover_capability()
            self._observe_followup_takeover("set_temperature")
            if self._awaiting_hvac_actuator and self._synchronized_takeover():
                # The user superseded acquisition before a thermostat or zone
                # was written. Only dispatched automation work can need rollback.
                raise _HVACTakeoverBlocked([], pre_dispatch_reason="manual_hvac_override_detected")
        except _HVACTakeoverBlocked as blocked:
            blocked.record_automation_work(changed_automations, dispatched=automation_dispatched)
            raise
        rollback_zones = {
            entity_id: state for entity_id, state in changed_zones.items() if entity_id not in superseded_zones
        }
        rollback_succeeded, unresolved_states, unresolved_zones, unresolved_main = await self._async_rollback_takeover(
            changed_automations,
            rollback_zones,
            main_entity=main_entity,
            saved_main_state=saved_main_state,
            restore_main=not main_superseded,
        )
        return HVACCommandResult(
            False,
            ("manual_hvac_override_detected" if rollback_succeeded else "hvac_acquisition_rollback_failed"),
            pre_state,
            self._snapshot(),
            unresolved_states,
            rollback_succeeded,
            unresolved_zones,
            unresolved_main,
            command_sent=not self._awaiting_hvac_actuator,
            capability_issues=self._late_takeover_issues,
        )

    async def _async_manual_override_result_if_requested(
        self,
        pre_state: dict[str, Any],
        changed_automations: dict[str, str],
        changed_zones: dict[str, Any],
        *,
        main_entity: str,
        saved_main_state: dict[str, Any],
        automation_dispatched: bool = False,
    ) -> HVACCommandResult | None:
        """Return a subordinate-only rollback when a user superseded the command."""
        try:
            self._raise_if_manual_override_requested()
        except _HVACManualOverrideError:
            return await self._async_manual_override_result(
                pre_state,
                changed_automations,
                changed_zones,
                main_entity=main_entity,
                saved_main_state=saved_main_state,
                automation_dispatched=automation_dispatched,
            )
        return None

    async def _async_restore_main_state_preserving_manual(
        self,
        entity_id: str,
        saved_state: dict[str, Any],
        *,
        emergency_off: bool = False,
    ) -> bool:
        """Restore main state unless a user supersedes the pending snapshot."""
        try:
            return await self._async_restore_main_state(entity_id, saved_state, emergency_off=emergency_off)
        except _HVACManualOverrideError:
            await self._async_persist_requested_manual_supersessions()
            return True

    async def _async_restore_main_state(
        self,
        entity_id: str,
        saved_state: dict[str, Any],
        *,
        emergency_off: bool = False,
    ) -> bool:
        """Restore original state immediately, including acquisition rollback."""
        self._restoring_main_state = True
        try:
            return await self._async_restore_main_state_impl(entity_id, saved_state, emergency_off=emergency_off)
        finally:
            self._restoring_main_state = False

    async def _async_restore_main_state_impl(
        self, entity_id: str, saved_state: dict[str, Any], *, emergency_off: bool = False,
    ) -> bool:
        """Restore and confirm the complete main state changed during takeover."""
        desired_state = _main_hvac_desired_state(saved_state)
        if not desired_state:
            return False
        if desired_state.get("hvac_mode") == "off":
            restore_succeeded = True
            if saved_state.get(_ROLLBACK_HVAC_MODE_CHANGED) is True:
                active_mode = saved_state.get(_ROLLBACK_ACTIVE_HVAC_MODE)
                active_mode_restored = isinstance(active_mode, str) and active_mode in _ACTIVE_HVAC_MODES
                restore_succeeded = active_mode_restored
                if active_mode_restored:
                    # Restore the mode Daikin will remember for its next
                    # turn-on before returning the thermostat to off.
                    try:
                        await self._async_apply_hvac_state(
                            entity_id,
                            {"hvac_mode": active_mode},
                            respect_zone_manual_override=False,
                        )
                    except _HVACManualOverrideError:
                        raise
                    except Exception:  # noqa: BLE001 - continue to the mandatory off cleanup.
                        restore_succeeded = False
            # Restore the target while the unit still accepts temperature
            # commands, but never let a target failure skip the off cleanup.
            target_state = _temperature_desired_state(desired_state)
            if target_state:
                try:
                    await self._async_apply_hvac_state(
                        entity_id,
                        target_state,
                        respect_zone_manual_override=False,
                    )
                except _HVACManualOverrideError:
                    raise
                except Exception:  # noqa: BLE001 - continue to the mandatory off cleanup.
                    restore_succeeded = False
            try:
                await self._async_apply_hvac_state(
                    entity_id,
                    {"hvac_mode": "off"},
                    respect_zone_manual_override=False,
                    force=self._unconfirmed_main_mode,
                    emergency_off=emergency_off,
                )
            except _HVACManualOverrideError:
                raise
            except Exception:  # noqa: BLE001 - retain ownership even if the service changed state.
                restore_succeeded = False
            main_state_restored = await self._async_confirm_hvac_state(
                entity_id,
                desired_state,
            )
            self._raise_if_manual_override_requested(include_zones=False)
            return restore_succeeded and main_state_restored
        try:
            await self._async_apply_hvac_state(
                entity_id,
                desired_state,
                respect_zone_manual_override=False,
                force=self._unconfirmed_main_mode,
            )
        except _HVACManualOverrideError:
            raise
        except Exception:  # noqa: BLE001 - rollback must report unresolved main state.
            return False
        main_state_restored = await self._async_confirm_hvac_state(
            entity_id,
            desired_state,
        )
        self._raise_if_manual_override_requested(include_zones=False)
        return main_state_restored

    async def _async_confirm_state(
        self, entity_id: str, expected_state: str, *, on_readback: Callable[[], None] | None = None,
    ) -> bool:
        """Wait briefly for Home Assistant to publish a requested actuator state."""
        if entity_id in self._independent_pairs():
            return await self._async_wait_for_climate(entity_id, lambda observed: observed.state == expected_state)
        # Older HA releases queue state listeners even when the state machine
        # already reflects a service result. Deliver those events while the
        # command context and manual-supersession markers are still active.
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _STATE_CONFIRMATION_TIMEOUT_SECONDS
        check_takeover = self._checking_takeover_readback and entity_id in self._zone_entities()
        blocked = False
        while True:
            observed = self._state(entity_id)
            if on_readback is not None:
                on_readback()
            if check_takeover and self._observe_followup_takeover():
                blocked = True
            confirmed = observed is not None and observed.state == expected_state
            remaining = deadline - loop.time()
            if confirmed or remaining <= 0:
                await self._record_confirmation(entity_id, confirmed)
                if blocked:
                    # The zone write reached Home Assistant. Settle its feedback before rollback.
                    raise _HVACFollowupWriteBlocked(",".join(self._late_takeover_issues))
                return confirmed
            await asyncio.sleep(min(_STATE_CONFIRMATION_POLL_SECONDS, remaining))

    def _raise_if_climate_override_requested(self, entity_id: str) -> None:
        """Honor the caller's override scope throughout persistence and readback."""
        respect_main, respect_zones = self._climate_override_policy.get(entity_id, (True, True))
        if respect_main:
            self._raise_if_manual_override_requested(include_zones=respect_zones)
        elif entity_id in self._manual_zone_override_entity_ids():
            # Sibling cleanup survives a main or another zone's manual change,
            # but must still preserve a superseding change to this exact zone.
            raise _HVACManualOverrideError

    async def _async_reconcile_recovery(self) -> None:
        """Settle a persisted write before issuing compensating restoration."""
        record = self._recovery_confirmation
        if record is None:
            return
        entity_id = str(record["entity_id"])
        self._climate_override_policy[entity_id] = (
            entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE), False,
        )
        deadline = dt_util.parse_datetime(record["deadline"])
        assert deadline is not None
        self._last_commands[entity_id] = record
        remaining = min(_STATE_RECONCILIATION_TIMEOUT_SECONDS, max(0.0, (deadline - dt_util.utcnow()).total_seconds()))
        loop = asyncio.get_running_loop()
        confirmation_deadline = loop.time() + remaining
        self._command_started[entity_id] = confirmation_deadline - _STATE_RECONCILIATION_TIMEOUT_SECONDS
        if remaining > 0:
            # Guard failure cannot revoke a write the device already accepted.
            # Keep read-only reconciliation on its original deadline before any
            # compensation, including the independent emergency-off path. Each
            # later write must still pass its own guard check.
            await self._async_arm_scheduler_guard(
                deadline=min(deadline, dt_util.utcnow() + timedelta(seconds=_STATE_RECONCILIATION_TIMEOUT_SECONDS)),
            )
        try:
            self._raise_if_climate_override_requested(entity_id)
            requested = record["requested"]
            # Enforce the deadline inside each readback predicate: guard waits
            # and queued state delivery both consume this original lease.
            if record.get("domain") == "switch":
                expected = "on" if record["service"] == "turn_on" else "off"
                await self._async_wait_for_climate(
                    entity_id, lambda observed: loop.time() < confirmation_deadline and observed.state == expected,
                )
            elif record["service"] == "turn_on":
                await self._async_wait_for_climate(
                    entity_id,
                    lambda observed: loop.time() < confirmation_deadline
                    and observed.state not in {"off", "unknown", "unavailable"},
                )
            else:
                desired = dict(requested)
                if "temperature" in desired:
                    desired["target_temperature"] = desired.pop("temperature")
                if record["service"] == "turn_off":
                    desired["hvac_mode"] = "off"
                await self._async_wait_for_climate(
                    entity_id,
                    lambda observed: loop.time() < confirmation_deadline
                    and _already_in_desired_state(observed, desired),
                )
        except _HVACManualOverrideError:
            # Supersession retires this entity's old intent, while unrelated
            # restoration must still proceed with its own override policy.
            await self._async_persist_requested_manual_supersessions()
        await self._async_save_confirmation(None)

    async def _async_confirm_hvac_state(self, entity_id: str, desired_state: dict[str, Any]) -> bool:
        """Confirm a thermostat write without retransmitting an uncertain intent."""
        return await self._async_wait_for_climate(
            entity_id, lambda observed: _already_in_desired_state(observed, desired_state),
        )

    async def _async_wait_for_climate(self, entity_id: str, matches: Callable[[State], bool]) -> bool:
        """Wait 30 seconds, then reconcile readback within the original 180-second lease."""
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        started = self._command_started.get(entity_id, loop.time())
        initial_deadline = started + _STATE_CONFIRMATION_TIMEOUT_SECONDS
        deadline = started + (
            _STATE_RECONCILIATION_TIMEOUT_SECONDS
            if entity_id in self._command_started else _STATE_CONFIRMATION_TIMEOUT_SECONDS
        )
        pending = False
        next_refresh = initial_deadline
        refresh: asyncio.Task[Any] | None = None
        try:
            while True:
                if self._awaiting_hvac_actuator:
                    self._raise_if_climate_override_requested(entity_id)
                    respect_main, _ = self._climate_override_policy.get(entity_id, (True, True))
                    self._check_pending_takeover_capability(respect_main)
                observed = self._state(entity_id)
                if self._checking_takeover_readback and not self._awaiting_hvac_actuator:
                    self._raise_if_climate_override_requested(entity_id)
                    if self._off_start_recall_dispatched and entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE):
                        self._allow_off_start_recall(observed)
                    self._check_followup_takeover()
                if observed is not None and matches(observed):
                    await self._record_confirmation(entity_id, True)
                    if (self._recovery_confirmation is not None
                            and self._recovery_confirmation["entity_id"] == entity_id):
                        await self._async_save_confirmation(None)
                    return True
                # Synchronous listener markers must interrupt a pending wait;
                # queued release can then preserve the superseding user state.
                self._raise_if_climate_override_requested(entity_id)
                now = loop.time()
                if now >= deadline:
                    await self._record_confirmation(entity_id, False)
                    if (self._recovery_confirmation is not None
                            and self._recovery_confirmation["entity_id"] == entity_id):
                        await self._async_save_confirmation(None)
                    return False
                if now >= initial_deadline:
                    if not pending:
                        pending = True
                        record = self._last_commands.get(entity_id)
                        if record is not None and self._command_callback is not None:
                            self._command_callback({**record, "stage": "readback_pending"})
                    if now >= next_refresh and refresh is None:
                        # A state-machine poll alone cannot fetch Daikin data.
                        # Request a coordinator refresh without issuing a write.
                        refresh = asyncio.create_task(self._async_refresh_climate(entity_id))
                        next_refresh = now + _STATE_REFRESH_INTERVAL_SECONDS
                if refresh is not None and refresh.done():
                    await refresh
                    refresh = None
                await asyncio.sleep(min(_STATE_CONFIRMATION_POLL_SECONDS, deadline - now))
        finally:
            if refresh is not None:
                refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)

    async def _async_refresh_climate(self, entity_id: str) -> None:
        """Bound read-only refresh independently of the device command lease."""
        try:
            async with asyncio.timeout(_STATE_REFRESH_INTERVAL_SECONDS):
                await async_update_entity(self.hass, entity_id)
        except Exception:  # noqa: BLE001 - unavailable refresh never proves command rejection.
            pass

    async def _async_confirm_complete_hvac_state(
        self,
        entity_id: str,
        desired_state: dict[str, Any],
    ) -> bool:
        """Confirm the main thermostat and every configured zone target."""
        if not await self._async_confirm_hvac_state(
            entity_id,
            _main_hvac_desired_state(desired_state),
        ):
            return False
        if not (desired_state.get("enable_zones") and desired_state.get("configured_zones_only") is True):
            return True
        zone_target = _temperature_desired_state(desired_state)
        if not zone_target:
            return True
        for zone_entity in self._synchronized_zone_climate_entities():
            target = self._zone_desired.get("zone_targets", {}).get(zone_entity)
            expected = {"target_temperature": target} if target is not None else zone_target
            if not await self._async_confirm_hvac_state(zone_entity, expected):
                return False
        for actuator, power in self._zone_desired.get("zone_power", {}).items():
            if not await self._async_confirm_state(actuator, power):
                return False
        return True

    async def _async_confirm_hvac_on(self, entity_id: str) -> bool:
        """Confirm a real active mode rather than unavailable or unknown telemetry."""
        return await self._async_wait_for_climate(
            entity_id, lambda observed: observed.state not in {"off", "unknown", "unavailable"},
        )

    async def _async_apply_hvac_state(
        self,
        entity_id: str,
        desired_state: dict[str, Any],
        *,
        force: bool = False,
        takeover_main_state: dict[str, Any] | None = None,
        takeover_write: bool = False,
        emergency_off: bool = False,
        respect_manual_override: bool = True,
        respect_zone_manual_override: bool = True,
    ) -> bool:
        """Apply thermostat mode before its target and report whether a command was sent."""
        self._climate_override_policy[entity_id] = (respect_manual_override, respect_zone_manual_override)
        if respect_manual_override:
            self._raise_if_manual_override_requested(
                include_zones=respect_zone_manual_override,
            )
        main_desired_state = _main_hvac_desired_state(desired_state)
        desired_mode = main_desired_state.get("hvac_mode")
        desired_temperature = main_desired_state.get("target_temperature")
        target_low = main_desired_state.get("target_temp_low")
        target_high = main_desired_state.get("target_temp_high")
        observed = self._state(entity_id)
        if observed is None:
            raise RuntimeError("climate state unavailable")
        command_sent = False

        if desired_mode == "off":
            if force or observed.state != "off":
                command_sent = True
                service_context = Context() if self._set_coupled_zone_feedback_expected is not None else None
                if service_context is not None and self._set_coupled_zone_feedback_expected is not None:
                    self._set_coupled_zone_feedback_expected(entity_id, "off", service_context.id)
                try:
                    await self._async_command(
                        self.hass,
                        "climate",
                        SERVICE_TURN_OFF,
                        {ATTR_ENTITY_ID: entity_id},
                        blocking=True,
                        context=service_context,
                        emergency_off=emergency_off,
                        takeover_write=takeover_write,
                    )
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                    if not await self._async_confirm_hvac_state(entity_id, {"hvac_mode": "off"}):
                        raise _HVACStateConfirmationError("climate turn-off was not confirmed")
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                finally:
                    if service_context is not None and self._set_coupled_zone_feedback_expected is not None:
                        self._set_coupled_zone_feedback_expected(None, None, None)
            return command_sent

        if desired_mode:
            if observed.state == "off":
                command_sent = True
                if self._set_turn_on_feedback_expected is not None:
                    self._set_turn_on_feedback_expected(True)
                service_context = (
                    Context()
                    if self._deferred_zone_entities and self._set_coupled_zone_feedback_expected is not None
                    else None
                )
                if service_context is not None and self._set_coupled_zone_feedback_expected is not None:
                    self._set_coupled_zone_feedback_expected(entity_id, "on", service_context.id)
                try:
                    await self._async_command(
                        self.hass,
                        "climate",
                        SERVICE_TURN_ON,
                        {ATTR_ENTITY_ID: entity_id},
                        blocking=True,
                        context=service_context,
                        takeover_write=takeover_write,
                    )
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                    if not await self._async_confirm_hvac_on(entity_id):
                        raise _HVACStateConfirmationError("climate turn-on was not confirmed")
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                    observed = self._state(entity_id) or observed
                    if takeover_write and self._off_start_recall_dispatched:
                        self._allow_off_start_recall(observed)
                    if takeover_main_state is not None and observed.state in _ACTIVE_HVAC_MODES:
                        takeover_main_state.setdefault(_ROLLBACK_ACTIVE_HVAC_MODE, observed.state)
                        if self._async_persist_main_state is not None:
                            await self._async_persist_main_state(dict(takeover_main_state))
                finally:
                    if service_context is not None and self._set_coupled_zone_feedback_expected is not None:
                        self._set_coupled_zone_feedback_expected(None, None, None)
                    if self._set_turn_on_feedback_expected is not None:
                        self._set_turn_on_feedback_expected(False)
            if force or not _mode_matches(observed, desired_mode):
                if takeover_main_state is not None and takeover_main_state.get("hvac_mode") == "off":
                    rollback_state = dict(takeover_main_state)
                    mode_was_already_changed = rollback_state.get(_ROLLBACK_HVAC_MODE_CHANGED) is True
                    rollback_state[_ROLLBACK_HVAC_MODE_CHANGED] = True
                    if not mode_was_already_changed and observed.state in _ACTIVE_HVAC_MODES:
                        rollback_state[_ROLLBACK_ACTIVE_HVAC_MODE] = observed.state
                    if self._async_persist_main_state is not None:
                        # The enriched snapshot must be durable before the
                        # following call overwrites Daikin's remembered mode.
                        await self._async_persist_main_state(rollback_state)
                        if respect_manual_override:
                            self._raise_if_manual_override_requested(
                                include_zones=respect_zone_manual_override,
                            )
                    takeover_main_state.update(rollback_state)
                command_sent = True
                service_context = (
                    Context()
                    if self._deferred_zone_entities and self._set_coupled_zone_feedback_expected is not None
                    else None
                )
                if service_context is not None and self._set_coupled_zone_feedback_expected is not None:
                    self._set_coupled_zone_feedback_expected(entity_id, str(desired_mode), service_context.id)
                try:
                    await self._async_command(
                        self.hass,
                        "climate",
                        "set_hvac_mode",
                        {ATTR_ENTITY_ID: entity_id, "hvac_mode": desired_mode},
                        blocking=True,
                        context=service_context,
                        takeover_write=takeover_write,
                    )
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                    if not await self._async_confirm_hvac_state(
                        entity_id,
                        {"hvac_mode": desired_mode},
                    ):
                        raise _HVACStateConfirmationError("climate HVAC mode was not confirmed")
                    if respect_manual_override:
                        self._raise_if_manual_override_requested(
                            include_zones=respect_zone_manual_override,
                        )
                finally:
                    # Unchanged Off-start recall is expected during mode
                    # readback. End the allowance before temperature control.
                    if takeover_write:
                        self._off_start_recall_dispatched = False
                    if service_context is not None and self._set_coupled_zone_feedback_expected is not None:
                        self._set_coupled_zone_feedback_expected(None, None, None)

        observed = self._state(entity_id) or observed
        if (desired_temperature is not None and respect_manual_override
                and self._zone_desired.get("equipment_compensation")
                and not await self._async_wait_for_climate(
                    entity_id, lambda state: self._equipment_target_available(entity_id, desired_temperature),
                )):
            raise _HVACStateConfirmationError("equipment target bounds were not confirmed")
        if (desired_temperature is not None and self._mode_memory_control()
                and not await self._async_wait_for_climate(
                    entity_id, lambda state: self._memory_target_available(entity_id, desired_temperature),
                )):
            raise _HVACStateConfirmationError("mode-memory target bounds were not confirmed")
        if desired_temperature is not None and (force or not _temperature_matches(observed, desired_temperature)):
            command_sent = True
            await self._async_command(
                self.hass,
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "temperature": desired_temperature},
                blocking=True,
                takeover_write=takeover_write,
            )
            if respect_manual_override:
                self._raise_if_manual_override_requested(
                    include_zones=respect_zone_manual_override,
                )
        elif (
            target_low is not None
            and target_high is not None
            and (force or not _temperature_range_matches(observed, target_low, target_high))
        ):
            command_sent = True
            await self._async_command(
                self.hass,
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "target_temp_low": target_low, "target_temp_high": target_high},
                blocking=True,
                takeover_write=takeover_write,
            )
            if respect_manual_override:
                self._raise_if_manual_override_requested(
                    include_zones=respect_zone_manual_override,
                )
        if _temperature_desired_state(main_desired_state):
            # One durable uncertainty record is sufficient only when each target
            # settles before the next zone can replace it. This also keeps a
            # service timeout from allowing the following zone write to race it.
            if not await self._async_confirm_hvac_state(entity_id, main_desired_state):
                raise _HVACStateConfirmationError("climate target was not confirmed")
            self._raise_if_climate_override_requested(entity_id)
        if desired_state.get("enable_zones") and desired_state.get("configured_zones_only") is True:
            # Daikin zone temperature bounds can depend on the main thermostat
            # target. Confirm the main target before asking subordinate climate
            # entities to accept the same setpoint.
            if not await self._async_confirm_hvac_state(entity_id, main_desired_state):
                raise _HVACStateConfirmationError("main climate state was not confirmed before zone targets")
            zone_target = _temperature_desired_state(desired_state)
            for zone_entity in self._synchronized_zone_climate_entities():
                target = self._zone_desired.get("zone_targets", {}).get(zone_entity)
                command_sent = (
                    await self._async_apply_hvac_state(
                        zone_entity,
                        {"target_temperature": target} if target is not None else zone_target,
                        force=force,
                        takeover_write=takeover_write,
                        respect_manual_override=respect_manual_override,
                        respect_zone_manual_override=respect_zone_manual_override,
                    )
                    or command_sent
                )
        return command_sent

    async def _async_disable_automations(self, states: dict[str, str]) -> _AutomationSuppression:
        """Suppress the configured automations and retain evidence for their recovery."""
        result = _AutomationSuppression()
        for automation_id, state in states.items():
            observed = self._state(automation_id)
            if (
                state == "off" and observed is not None and observed.state == "off"
                and observed.attributes.get("current") == 0
            ):
                # Core explicitly reports no running actions. Reissuing this
                # no-op would continually renew the external scheduler guard.
                # Missing/positive current still needs stop_actions below.
                continue
            if state == "on":
                # Treat the boundary as uncertain: a service handler can apply
                # the state change before propagating an exception.
                result.restoration_states[automation_id] = state
            self._pending_automation_off = automation_id if state == "on" and self._synchronized_takeover() else None
            previous_command = self._last_commands.get(automation_id)
            try:
                # Home Assistant can report an automation as off while an
                # action sequence started earlier is still running.
                before_dispatch = await self._async_command(
                    self.hass,
                    "automation",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: automation_id, "stop_actions": True},
                    blocking=True,
                )
            except _HVACTakeoverBlocked as blocked:
                result.restoration_states.pop(automation_id, None)
                blocked.record_automation_work(result.restoration_states, dispatched=result.dispatched)
                raise
            except Exception:  # noqa: BLE001 - device adapter must fail closed on service-layer errors.
                # A failed handler can have accepted the command. A guard
                # refusal or locally unavailable target never reached it.
                attempted = self._command_dispatched_since(automation_id, previous_command)
                if not attempted:
                    result.restoration_states.pop(automation_id, None)
                    self._pending_automation_off = None
                elif automation_id in result.restoration_states:
                    result.automation_off_pending = automation_id
                result.dispatched |= attempted
                return result
            result.dispatched = True
            result.suppressed_active_automation |= bool(
                getattr(before_dispatch, "state", None) == "on"
                or getattr(before_dispatch, "attributes", {}).get("current", 0)
            )
            try:
                # An override can arrive before the accepted suppression is
                # visible. Read back Off before trying to restore the automation.
                if not self._synchronized_takeover():
                    self._raise_if_manual_override_requested()
                if not await self._async_confirm_state(automation_id, "off"):
                    return result
                self._pending_automation_off = None
                self._raise_if_manual_override_requested()
                # Suppression may be the whole action, so it needs the same
                # check after confirmation that precedes the next command.
                self._check_pending_takeover_capability()
            except _HVACTakeoverBlocked as blocked:
                blocked.record_automation_work(result.restoration_states, dispatched=True)
                raise
            except _HVACManualOverrideError:
                return result
        result.confirmed = True
        return result

    def _command_dispatched_since(self, entity_id: str, previous_command: dict[str, Any] | None) -> bool:
        command = self._last_commands.get(entity_id)
        return command is not None and command is not previous_command and command.get("dispatched") is True

    def _automation_states(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for entity_id in self._automation_entities():
            state = self._state(entity_id)
            if state is not None:
                states[entity_id] = state.state
        return states

    def _enabled_automation_states(self) -> dict[str, str]:
        """Return configured automations that must be re-enabled on release."""
        return {entity_id: state for entity_id, state in self._automation_states().items() if state == "on"}

    def _automation_entities(self) -> list[str]:
        configured = self.entry_data.get(CONF_CLIMATE_AUTOMATIONS, "")
        if isinstance(configured, str):
            return [entity_id.strip() for entity_id in configured.split(",") if entity_id.strip()]
        if isinstance(configured, list):
            return [str(entity_id) for entity_id in configured if str(entity_id).strip()]
        return []

    def _zone_states(self) -> dict[str, Any]:
        states: dict[str, Any] = {}
        for entity_id in self._zone_entities():
            state = self._state(entity_id)
            if state is None:
                continue
            if entity_id.split(".", 1)[0] == "climate":
                # Compensate this attempt against its pre-command head/zone
                # settings. Ownership merging separately preserves the original
                # lifecycle targets for final release.
                states[entity_id] = (climate_target_snapshot(state)
                                     or self._zone_baseline.get("zone_states", {}).get(entity_id, {}))
            else:
                states[entity_id] = state.state
        return states

    def _synchronized_zone_climate_entities(self) -> list[str]:
        """Only mutate targets captured before the durable takeover boundary."""
        return [
            entity_id
            for entity_id in self._zone_climate_entities()
            if entity_id not in (self._deferred_zone_entities or set())
            and entity_id not in {
                climate for actuator, climate in self._zone_desired.get("zone_intent", {}).get("pairs", {}).items()
                if self._zone_desired.get("zone_power", {}).get(actuator) == "off"
            }
        ]

    def _zone_climate_entities(self) -> list[str]:
        """Return subordinate zone thermostats that receive target setpoints."""
        return [entity_id for entity_id in self._zone_entities() if entity_id.split(".", 1)[0] == "climate"]

    def _zone_entities(self) -> list[str]:
        configured = self.entry_data.get(CONF_CLIMATE_ZONES, "")
        if isinstance(configured, str):
            return [entity_id.strip() for entity_id in configured.split(",") if entity_id.strip()]
        if isinstance(configured, list):
            return [str(entity_id).strip() for entity_id in configured if str(entity_id).strip()]
        return []

    def _snapshot(self) -> dict[str, Any]:
        climate_entity = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        snapshot: dict[str, Any] = {}
        if climate_entity:
            snapshot[CONF_DAIKIN_CLIMATE] = self._state_value(climate_entity)
        for automation_id in self._automation_entities():
            snapshot[automation_id] = self._state_value(automation_id)
        for zone_id in self._zone_entities():
            snapshot[zone_id] = self._state_value(zone_id)
        for guard_id in (
            self.entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER),
            self.entry_data.get(CONF_CLIMATE_SCHEDULER_GUARD_TIMER),
        ):
            if guard_id:
                snapshot[guard_id] = self._state_value(guard_id)
        return snapshot

    def _state(self, entity_id: str | None) -> State | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        if (self._restoring_off_start is not None
                and (entity_id == self.entry_data.get(CONF_DAIKIN_CLIMATE)
                     or entity_id in self._restoring_owned_zone_entities
                     or entity_id in (self._restoring_memory or {}).get("pairs", {}))
                and hvac_feedback_is_assumed(state)):
            self._restoring_off_start.assumed_feedback = True
        return state if hvac_feedback_is_observed(state) else None

    def _state_value(self, entity_id: str | None) -> str | None:
        state = self._state(entity_id)
        return None if state is None else state.state


def _already_in_desired_state(state: State, desired_state: dict[str, Any]) -> bool:
    return (
        _mode_matches(state, desired_state.get("hvac_mode"))
        and _temperature_matches(state, desired_state.get("target_temperature"))
        and _temperature_range_matches(
            state,
            desired_state.get("target_temp_low"),
            desired_state.get("target_temp_high"),
        )
    )


def _temperature_desired_state(desired_state: dict[str, Any]) -> dict[str, Any]:
    """Return only target-temperature fields supported by subordinate zones."""
    return {
        key: desired_state[key]
        for key in ("target_temperature", "target_temp_low", "target_temp_high")
        if desired_state.get(key) is not None
    }


def _has_temperature_target_fields(saved_state: dict[str, Any]) -> bool:
    """Detect the target shape in legacy and restoration snapshots."""
    return saved_state.get("target_temperature") is not None or (
        saved_state.get("target_temp_low") is not None and saved_state.get("target_temp_high") is not None
    )


def _main_hvac_desired_state(desired_state: dict[str, Any]) -> dict[str, Any]:
    """Return command fields intended for the main thermostat."""
    return {
        key: desired_state[key]
        for key in ("hvac_mode", "target_temperature", "target_temp_low", "target_temp_high")
        if desired_state.get(key) is not None
    }


def _climate_state_snapshot(state: State) -> dict[str, Any]:
    """Capture the main thermostat mode and target for transactional rollback."""
    return {"hvac_mode": state.state, **climate_target_snapshot(state)}


def _mode_matches(state: State, desired_mode: Any) -> bool:
    """Return whether the requested mode is already observed."""
    return desired_mode is None or str(desired_mode) == state.state


def _temperature_matches(state: State, desired_temperature: Any) -> bool:
    """Return whether the requested scalar target is already observed."""
    if desired_temperature is None:
        return True
    attributes = getattr(state, "attributes", {}) or {}
    return temperature_targets_match(attributes.get("temperature"), desired_temperature)


def _temperature_range_matches(state: State, target_low: Any, target_high: Any) -> bool:
    """Return whether every requested range target is already observed."""
    attributes = getattr(state, "attributes", {}) or {}
    return (target_low is None or temperature_targets_match(attributes.get("target_temp_low"), target_low)) and (
        target_high is None or temperature_targets_match(attributes.get("target_temp_high"), target_high)
    )
