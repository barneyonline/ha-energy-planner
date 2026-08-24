"""Tests for coordinator helper behavior."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_energy_planner import coordinator as coordinator_module
from custom_components.ha_energy_planner import notifications as notifications_module
from custom_components.ha_energy_planner.ai_advisor import AIAdviceResult
from custom_components.ha_energy_planner.const import (
    CONF_AI_ENABLED,
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_MANUAL_OVERRIDE,
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
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
    CONF_PLANNER_ENABLED,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from custom_components.ha_energy_planner.coordinator import (
    EnergyPlannerCoordinator,
    _active_control_not_ready_reason,
    _ai_advice_notification_message,
    _bool_state_value,
    _configured_entity_ids,
    _decision_input_fingerprint,
    _expired_manual_hvac_state,
    _float_state_value,
    _hvac_control_from_ownership,
    _is_manual_hvac_change,
    _is_manual_hvac_zone_change,
    _is_manual_override_helper_change,
    _is_material_state_change,
    _is_pending_main_hvac_manual_change,
    _is_planner_owned_control_feedback,
    _latest_ai_plan_fingerprint,
    _latest_ai_service_call_at,
    _matches_pending_main_hvac_feedback,
    _matches_pending_zone_hvac_feedback,
    _material_plan_fingerprint,
    _overrides_from_store,
    _parse_datetime_or_none,
    _pending_zone_hvac_manual_change_entity_id,
    _seconds_until_next_interval_boundary,
    _snapshot_action_load_forecasts,
    _snapshot_actions,
    _split_entity_values,
    _startup_auto_recovery_prerequisites,
    _startup_auto_recovery_successful_runs,
    _startup_auto_recovery_validation_ready,
    _unexpired_overrides,
    _updated_load_forecast_training_attempted,
)
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    InputHealth,
    OccupancyState,
    OutcomeResult,
    Override,
    PlanAction,
    PlannerMode,
)
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint


def test_configured_entity_ids_excludes_services_and_splits_lists() -> None:
    entity_ids = _configured_entity_ids(
        {
            "haeo_optimize_service": "haeo.optimize",
            "enphase_profile_control_service": "select.select_option",
            "amber_import_price_entity": "sensor.import_price",
            "climate_automation_entities": "automation.heat, automation.cool",
            "person_entities": "person.james,person.cath",
            "ai_advisor_service": "ai_task.generate_data",
            "ai_task_entity": "ai_task.local",
            "daikin_power_entity": "sensor.daikin_power",
            "ev_smart_charging_entity": "switch.ev_control",
            "ev_smart_charging_ready_by_entity": "select.ev_ready_by",
            "pv_forecast_secondary_entity": "sensor.pv_tomorrow",
            "empty_entity": "",
        }
    )
    assert entity_ids == [
        "person.cath",
        "person.james",
        "select.ev_ready_by",
        "sensor.import_price",
        "sensor.pv_tomorrow",
    ]


@pytest.mark.parametrize(
    ("previously_attempted", "reason", "expected"),
    [
        (False, "load_forecast_household_load_not_configured", False),
        (False, "load_forecast_household_load_unavailable", False),
        (False, "load_forecast_training_recent", False),
        (False, "load_forecast_ready", True),
        (True, "load_forecast_household_load_unavailable", True),
    ],
)
def test_load_forecast_startup_attempt_remains_pending_until_training_runs(
    previously_attempted: bool, reason: str, expected: bool
) -> None:
    assert _updated_load_forecast_training_attempted(previously_attempted, reason) is expected


def test_startup_auto_recovery_preflight_helpers_cover_every_blocker() -> None:
    base = {
        "entities": {"missing": [], "unavailable": []},
        "services": {"missing": [], "unavailable": []},
        "control_areas": {
            "required": ["ev"],
            "ready": ["ev"],
            "available": ["ev"],
            "confidence_eligible": ["ev"],
        },
        "discovery": {"ev": {"supported": True}},
        "recorder": {"available": True},
        "checks": [{"check": "control_not_paused", "ok": True}],
        "current_plan": {"safe": True},
    }
    assert _startup_auto_recovery_prerequisites(base, {}) == (True, "startup_dependencies_ready")
    assert _startup_auto_recovery_validation_ready(base, {}) == (True, "validation_succeeded")

    cases = (
        (
            {**base, "control_areas": {"required": []}},
            {},
            "no_required_control_areas",
        ),
        (
            {
                **base,
                "control_areas": {
                    "required": ["ev"],
                    "ready": [],
                    "available": [],
                    "confidence_eligible": [],
                },
            },
            {},
            "no_ready_control_area",
        ),
        (
            {
                **base,
                "control_areas": {
                    "required": ["ev"],
                    "ready": ["ev"],
                    "available": [],
                    "confidence_eligible": [],
                },
            },
            {},
            "control_paused",
        ),
        (
            {
                **base,
                "control_areas": {
                    "required": ["ev"],
                    "ready": ["ev"],
                    "available": ["ev"],
                    "confidence_eligible": [],
                },
            },
            {},
            "no_confidence_eligible_control_area",
        ),
        ({**base, "recorder": {"available": False}}, {CONF_HOUSEHOLD_LOAD: "sensor.load"}, "recorder_unavailable"),
    )
    for report, entry_data, reason in cases:
        assert _startup_auto_recovery_prerequisites(report, entry_data) == (False, reason)
        assert _startup_auto_recovery_validation_ready(report, entry_data) == (False, reason)

    unsafe = {**base, "current_plan": {"safe": False}}
    assert _startup_auto_recovery_validation_ready(unsafe, {}) == (False, "current_plan_unsafe")
    isolated = {
        **base,
        "entities": {"missing": [], "unavailable": ["switch.ev"]},
        "control_areas": {
            "required": ["ev", "hvac"],
            "ready": ["hvac"],
            "available": ["hvac"],
            "confidence_eligible": ["hvac"],
        },
        "discovery": {
            "ev": {"supported": False},
            "hvac": {"supported": True},
        },
    }
    assert _startup_auto_recovery_validation_ready(isolated, {}) == (
        True,
        "validation_succeeded",
    )
    assert _startup_auto_recovery_successful_runs("invalid") == 0


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str
    attributes: dict[str, object] = field(default_factory=dict)


class FakeStates:
    """Minimal state registry."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.state = CoreState.running
        self.states = FakeStates(values or {})
        self.services = SimpleNamespace(calls=[], async_call=self._async_call_service)
        self.created_tasks: list[object] = []

    def async_create_task(self, task: object) -> None:
        close = getattr(task, "close", None)
        if callable(close):
            close()
        self.created_tasks.append(task)

    def async_run_hass_job(self, job: object, *args: object) -> None:
        self.async_create_task(job.target(*args))

    async def async_add_executor_job(self, func: object, *args: object) -> object:
        return func(*args)

    async def _async_call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, object],
        *,
        blocking: bool,
    ) -> None:
        self.services.calls.append((domain, service, data, blocking))


class FakeStore:
    """Minimal planner store."""

    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = data or {}
        self.saved_plans: list[EnergyPlan] = []
        self.discovery: list[dict[str, object]] = []
        self.ev_charge_calibrations: list[dict[str, object]] = []
        self.forecast_calibrations: list[dict[str, object]] = []
        self.load_forecasts: list[dict[str, object]] = []
        self.thermal_models: list[dict[str, object]] = []
        self.haeo_runs: list[dict[str, object]] = []
        self.ai_recommendations: list[dict[str, object]] = []
        self.snapshot_ai: list[tuple[str, dict[str, object]]] = []
        self.forecast_snapshots: list[dict[str, object]] = []
        self.dry_run_comparisons: list[dict[str, object]] = []
        self.production_saves: list[dict[str, object]] = []
        self.control_pause_saves: list[dict[str, object]] = []

    async def async_save_plan(self, plan: EnergyPlan) -> None:
        self.saved_plans.append(plan)

    async def async_save_overrides(self, overrides: list[object]) -> None:
        self.data["overrides"] = overrides

    async def async_save_ownership(self, ownership: dict[str, object]) -> None:
        self.data["ownership"] = ownership

    async def async_save_discovery(self, discovery: dict[str, object]) -> None:
        self.discovery.append(discovery)

    async def async_save_ev_charge_calibration(self, model: dict[str, object]) -> None:
        self.ev_charge_calibrations.append(model)
        self.data["ev_charge_calibration"] = model

    async def async_save_forecast_calibration(self, calibration: dict[str, object]) -> None:
        self.forecast_calibrations.append(calibration)
        self.data["forecast_calibration"] = calibration

    async def async_save_builtin_load_forecast(self, model: dict[str, object]) -> None:
        self.load_forecasts.append(model)
        self.data["built_in_load_forecast"] = model

    async def async_save_thermal_model(self, thermal_model: dict[str, object]) -> None:
        self.thermal_models.append(thermal_model)
        self.data["thermal_model"] = thermal_model

    async def async_add_haeo_run(self, run: dict[str, object]) -> None:
        self.haeo_runs.append(run)

    async def async_add_ai_recommendation(self, recommendation: dict[str, object]) -> None:
        self.ai_recommendations.append(recommendation)

    async def async_attach_ai_to_forecast_snapshot(self, plan_id: str, metadata: dict[str, object]) -> None:
        self.snapshot_ai.append((plan_id, metadata))

    async def async_add_forecast_snapshot(self, snapshot: dict[str, object]) -> None:
        self.forecast_snapshots.append(snapshot)

    async def async_add_dry_run_comparison(self, comparison: dict[str, object]) -> None:
        self.dry_run_comparisons.append(comparison)
        self.data["dry_run_comparisons"] = [comparison]

    async def async_save_production(self, production: dict[str, object]) -> None:
        self.production_saves.append(production)
        self.data["production"] = production

    async def async_save_control_pause(self, pause: dict[str, object]) -> None:
        self.control_pause_saves.append(pause)
        self.data["control_pause"] = pause

    @asynccontextmanager
    async def async_delay_save(self) -> object:
        self.delay_entered = True
        yield
        self.delay_exited = True


class FakeExecutor:
    """Minimal executor."""

    def __init__(self) -> None:
        self.options = {}
        self.entry_data = {}
        self.evaluated: list[tuple[EnergyPlan, object]] = []
        self.restored: list[str] = []
        self.device_restores: list[tuple[str, str]] = []
        self.device_restore_result = SimpleNamespace(result=OutcomeResult.RESTORED)
        self.hvac_releases: list[str] = []
        self.hvac_release_preserved_zones: list[str | None] = []
        self.hvac_release_preserved_main: list[bool] = []
        self.pending_hvac_desired_state: dict[str, object] | None = None
        self.pending_hvac_manual_overrides = 0
        self.pending_hvac_manual_zone_overrides: list[str] = []
        self.manual_ev_commands: list[tuple[bool, object, dict[str, object], dict[str, object]]] = []
        self.reservation_syncs = 0
        self.reservation_persists = 0
        self.startup_recovery_notifications: list[str] = []
        self.startup_recovery_dismissals = 0
        self.notification_grace_until: datetime | None = None

    async def async_evaluate(self, plan: EnergyPlan, context: object) -> PlanAction | None:
        self.evaluated.append((plan, context))
        return None

    async def async_restore_safe_state(self, reason: str) -> None:
        self.restored.append(reason)

    async def async_restore_device_control(self, asset: str, reason: str) -> object:
        self.device_restores.append((asset, reason))
        return self.device_restore_result

    async def async_release_hvac_control(
        self,
        reason: str,
        *,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
    ) -> None:
        self.hvac_releases.append(reason)
        self.hvac_release_preserved_zones.append(preserve_zone_entity_id)
        self.hvac_release_preserved_main.append(preserve_main_state)

    def mark_pending_hvac_manual_override(self) -> bool:
        """Mark a pending fake HVAC transaction as user-superseded."""
        if self.pending_hvac_desired_state is None:
            return False
        self.pending_hvac_desired_state["manual_override_detected"] = True
        self.pending_hvac_manual_overrides += 1
        return True

    def mark_pending_hvac_zone_manual_override(self, entity_id: str) -> bool:
        """Mark a pending fake zone transaction as user-superseded."""
        if self.pending_hvac_desired_state is None:
            return False
        self.pending_hvac_desired_state["manual_zone_entity_ids"] = [entity_id]
        self.pending_hvac_manual_zone_overrides.append(entity_id)
        return True

    async def async_manual_ev_charging(self, enabled: bool, context: object) -> object:
        self.manual_ev_commands.append((enabled, context, dict(self.options), dict(self.entry_data)))
        return SimpleNamespace(applied=True)

    def sync_ev_grid_reservation(self) -> None:
        self.reservation_syncs += 1

    async def async_persist_ev_grid_reservation(self) -> None:
        self.reservation_persists += 1

    async def async_notify_plan_fallback(self, plan: EnergyPlan, violations: list[str]) -> None:
        self.fallback = (plan, violations)
        self.fallback_options = dict(self.options)

    async def async_notify_startup_recovery_unsafe(self, reason: str) -> None:
        self.startup_recovery_notifications.append(reason)

    async def async_dismiss_startup_recovery_notification(self) -> None:
        self.startup_recovery_dismissals += 1


@dataclass(slots=True)
class FakeEntry:
    """Minimal config entry."""

    data: dict[str, str]
    options: dict[str, object] = field(default_factory=dict)
    entry_id: str = "entry-1"
    title: str = "Energy Planner"

    def async_create_background_task(
        self, hass: object, coroutine: object, name: str
    ) -> object:
        """Create background work through the test Home Assistant instance."""
        return hass.async_create_task(coroutine)


def test_options_update_restores_when_direct_update_enables_safe_mode() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {},
        {
            CONF_PLANNER_ENABLED: False,
            CONF_DRY_RUN: True,
        },
    )
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, False)
    coordinator.executor = FakeExecutor()
    restore_calls: list[tuple[str, bool]] = []
    replan_calls: list[str] = []

    async def restore(reason: str, *, refresh: bool = True) -> None:
        restore_calls.append((reason, refresh))

    async def replan() -> None:
        replan_calls.append("replan")

    coordinator.async_restore_safe_state = restore
    coordinator.async_request_replan = replan

    asyncio.run(coordinator.async_handle_options_update())

    assert restore_calls == [("planner_disabled", False)]
    assert replan_calls == ["replan"]
    assert coordinator._last_control_mode_state == (False, True)
    assert coordinator.executor.options[CONF_DRY_RUN] is True
    assert coordinator.executor.reservation_syncs == 1


def test_options_update_handles_each_option_snapshot_once() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {},
        {
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: True,
        },
    )
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_handled_options = {CONF_PLANNER_ENABLED: False, CONF_DRY_RUN: True}
    coordinator._last_control_mode_state = (False, True)
    coordinator.executor = FakeExecutor()
    coordinator.async_restore_safe_state = AsyncMock()
    coordinator.async_request_replan = AsyncMock()

    async def handle_duplicate_update() -> None:
        await coordinator.async_handle_options_update()
        await coordinator.async_handle_options_update()

    asyncio.run(handle_duplicate_update())

    assert coordinator.executor.reservation_syncs == 1
    assert coordinator.executor.reservation_persists == 1
    coordinator.async_request_replan.assert_awaited_once_with()


def test_options_update_restores_only_hvac_when_climate_control_is_disabled() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {},
        {
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_CLIMATE_CONTROL_ENABLED: False,
        },
    )
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_handled_options = {
        CONF_PLANNER_ENABLED: True,
        CONF_DRY_RUN: False,
        CONF_CLIMATE_CONTROL_ENABLED: True,
    }
    coordinator._last_control_mode_state = (True, False)
    coordinator.executor = FakeExecutor()
    coordinator.store = FakeStore(
        {
            "ownership": {
                "hvac_control": {"phase": "peak_coast"},
                "ev_smart_charging_state": "off",
            }
        }
    )
    coordinator._planner_lock = asyncio.Lock()
    coordinator._command_lock = asyncio.Lock()
    coordinator.async_restore_safe_state = AsyncMock()
    coordinator.async_request_replan = AsyncMock()

    asyncio.run(coordinator.async_handle_options_update())

    assert coordinator.executor.device_restores == [("daikin", "hvac_control_disabled")]
    assert coordinator.executor.hvac_releases == []
    assert coordinator.store.data["ownership"]["ev_smart_charging_state"] == "off"
    coordinator.async_restore_safe_state.assert_not_awaited()
    coordinator.async_request_replan.assert_awaited_once_with()


def test_options_update_runs_one_idempotent_restore_for_unowned_disabled_hvac() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {},
        {
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_CLIMATE_CONTROL_ENABLED: False,
        },
    )
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_handled_options = {
        CONF_PLANNER_ENABLED: True,
        CONF_DRY_RUN: False,
        CONF_CLIMATE_CONTROL_ENABLED: True,
    }
    coordinator._last_control_mode_state = (True, False)
    coordinator.executor = FakeExecutor()
    coordinator.store = FakeStore(
        {
            "ownership": {
                "ev_smart_charging_state": "off",
            }
        }
    )
    coordinator._planner_lock = asyncio.Lock()
    coordinator._command_lock = asyncio.Lock()
    coordinator.async_restore_safe_state = AsyncMock()
    coordinator.async_request_replan = AsyncMock()

    asyncio.run(coordinator.async_handle_options_update())

    assert coordinator.executor.device_restores == [("daikin", "hvac_control_disabled")]
    assert coordinator.executor.hvac_releases == []
    assert coordinator.store.data["ownership"] == {"ev_smart_charging_state": "off"}
    coordinator.async_restore_safe_state.assert_not_awaited()
    coordinator.async_request_replan.assert_awaited_once_with()


def test_options_update_does_not_repeat_device_restore_when_replan_fails() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {},
        {
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: False,
        },
    )
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._planner_lock = asyncio.Lock()
    coordinator._command_lock = asyncio.Lock()
    coordinator._last_handled_options = {
        CONF_PLANNER_ENABLED: True,
        CONF_DRY_RUN: False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    coordinator._last_control_mode_state = (True, False)
    coordinator.executor = FakeExecutor()
    coordinator.async_restore_safe_state = AsyncMock()

    async def fail_replan() -> None:
        raise RuntimeError("replan failed")

    coordinator.async_request_replan = fail_replan

    with pytest.raises(RuntimeError, match="replan failed"):
        asyncio.run(coordinator.async_handle_options_update())
    asyncio.run(coordinator.async_handle_options_update())

    assert coordinator.executor.device_restores == [("ev", "ev_control_disabled")]


def test_options_update_surfaces_restore_error_after_safe_option_is_applied() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {},
        {
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: True,
        },
    )
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, False)
    coordinator.executor = FakeExecutor()

    async def restore(reason: str, *, refresh: bool = True) -> None:
        raise RuntimeError(f"{reason}:{refresh}")

    coordinator.async_restore_safe_state = restore
    coordinator.async_request_replan = AsyncMock()

    with pytest.raises(RuntimeError, match="dry_run_enabled:False"):
        asyncio.run(coordinator.async_handle_options_update())

    assert coordinator.dry_run is True
    assert coordinator._last_control_mode_state == (True, True)
    coordinator.async_request_replan.assert_not_awaited()


def test_keep_on_option_rejects_nonpersistent_control_before_persisting() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {"ev_charger_start_entity": "button.ev_start"},
        {CONF_EV_KEEP_CHARGER_ON: False},
    )

    with pytest.raises(HomeAssistantError):
        asyncio.run(coordinator.async_set_ev_keep_charger_on(True))

    assert coordinator.entry.options[CONF_EV_KEEP_CHARGER_ON] is False


def test_keep_on_option_accepts_persistent_control_and_runs_option_update() -> None:
    class ConfigEntries:
        def async_update_entry(
            self,
            entry: FakeEntry,
            *,
            options: dict[str, object],
        ) -> None:
            entry.options = options

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {CONF_EV_CHARGER: "switch.ev_control"},
        {
            CONF_PLANNER_ENABLED: False,
            CONF_DRY_RUN: True,
            CONF_EV_KEEP_CHARGER_ON: False,
        },
    )
    coordinator.hass = SimpleNamespace(config_entries=ConfigEntries())
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (False, True)
    coordinator.executor = FakeExecutor()
    coordinator.async_request_replan = AsyncMock()

    asyncio.run(coordinator.async_set_ev_keep_charger_on(True))

    assert coordinator.entry.options[CONF_EV_KEEP_CHARGER_ON] is True
    assert coordinator.executor.reservation_persists == 1
    assert coordinator.async_request_replan.await_count == 1


def test_coordinator_records_refresh_duration_in_memory() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()
    expected = _plan("refresh-duration")

    async def update_locked(*, defer_execution: bool = False) -> EnergyPlan:
        assert defer_execution is True
        coordinator._pending_refresh_trigger = "newer_request"
        return expected

    coordinator._pending_refresh_trigger = "state_change"
    coordinator._async_update_data_locked = update_locked

    result = asyncio.run(coordinator._async_update_data())

    assert result is expected
    assert coordinator.last_refresh_metadata["succeeded"] is True
    assert coordinator.last_refresh_metadata["duration_ms"] >= 0
    assert coordinator.last_refresh_metadata["completed_at"].tzinfo is not None
    assert coordinator.last_refresh_metadata["trigger"] == "state_change"
    assert coordinator.refresh_metrics["trigger_counts"] == {"state_change": 1}
    assert coordinator.refresh_metrics["succeeded"] == 1


def test_queued_refresh_skips_work_after_teardown() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator._tearing_down = True
    coordinator.data = _plan("last-committed")
    coordinator._pending_refresh_trigger = "queued_before_teardown"
    coordinator._async_update_data_locked = AsyncMock()

    result = asyncio.run(coordinator._async_update_data())

    assert result is coordinator.data
    assert coordinator._async_update_data_locked.await_count == 0
    assert coordinator.refresh_metrics["teardown_skipped"] == 1
    assert coordinator.last_refresh_metadata["succeeded"] is True


def test_wait_for_refresh_shutdown_reaches_planner_safe_boundary() -> None:
    async def scenario() -> bool:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator._planner_lock = asyncio.Lock()
        release = asyncio.Event()
        started = asyncio.Event()

        async def active_refresh() -> None:
            async with coordinator._planner_lock:
                started.set()
                await release.wait()

        refresh_task = asyncio.create_task(active_refresh())
        await started.wait()
        waiter = asyncio.create_task(coordinator.async_wait_for_refresh_shutdown())
        await asyncio.sleep(0)
        waiting_before_release = not waiter.done()
        release.set()
        await waiter
        await refresh_task
        return waiting_before_release

    assert asyncio.run(scenario()) is True


def test_coordinator_records_failed_refresh() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()

    async def update_locked(*, defer_execution: bool = False) -> EnergyPlan:
        assert defer_execution is True
        raise RuntimeError("failed refresh")

    coordinator._async_update_data_locked = update_locked
    try:
        asyncio.run(coordinator._async_update_data())
    except RuntimeError:
        pass

    assert coordinator.refresh_metrics["failed"] == 1
    assert coordinator.last_refresh_metadata["succeeded"] is False


class FakeEvent:
    """Minimal state changed event."""

    def __init__(
        self,
        entity_id: str,
        old: str,
        new: str,
        *,
        old_attributes: dict[str, object] | None = None,
        new_attributes: dict[str, object] | None = None,
    ) -> None:
        self.data = {
            "entity_id": entity_id,
            "old_state": FakeState(old, old_attributes or {}),
            "new_state": FakeState(new, new_attributes or {}),
        }


def test_manual_hvac_change_detected_without_guard() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert _is_manual_hvac_change(
        FakeHass(),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
        {"ownership": {}},
        FakeEvent("climate.daikin", "heat", "off"),
        now,
    )


def test_manual_hvac_setpoint_change_detected_without_mode_change() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert _is_manual_hvac_change(
        FakeHass(),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
        {"ownership": {}},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20, "current_temperature": 19},
            new_attributes={"temperature": 22, "current_temperature": 19},
        ),
        now,
    )


def test_hvac_observation_attribute_change_is_not_manual_control() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert not _is_manual_hvac_change(
        FakeHass(),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
        {"ownership": {}},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20, "current_temperature": 19},
            new_attributes={"temperature": 20, "current_temperature": 19.5},
        ),
        now,
    )


def test_manual_hvac_change_ignored_when_scheduler_guard_on() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert not _is_manual_hvac_change(
        FakeHass({"input_boolean.scheduler": "on"}),
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler",
        },
        {"ownership": {}},
        FakeEvent("climate.daikin", "heat", "off"),
        now,
    )


def test_manual_hvac_change_preserved_during_planner_grace_when_not_expected() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    assert _is_manual_hvac_change(
        FakeHass(),
        {CONF_DAIKIN_CLIMATE: "climate.daikin"},
        {"ownership": {"planner_hvac_action_expires_at": (now + timedelta(minutes=1)).isoformat()}},
        FakeEvent("climate.daikin", "heat", "off"),
        now,
    )


def test_material_state_change_uses_configured_percent_threshold() -> None:
    assert not _is_material_state_change(
        FakeEvent("sensor.price", "100", "104"), {"material_change_threshold_percent": 5}
    )
    assert _is_material_state_change(FakeEvent("sensor.price", "100", "105"), {"material_change_threshold_percent": 5})
    assert _is_material_state_change(
        FakeEvent("person.james", "home", "not_home"), {"material_change_threshold_percent": 5}
    )
    assert not _is_material_state_change(
        FakeEvent("sensor.price", "on", "on"), {"material_change_threshold_percent": 5}
    )


def test_material_state_change_treats_non_finite_numbers_as_material() -> None:
    options = {"material_change_threshold_percent": 5}

    assert _is_material_state_change(FakeEvent("sensor.price", "1.0", "nan"), options)
    assert _is_material_state_change(FakeEvent("sensor.price", "inf", "1.0"), options)


def test_material_state_change_detects_only_planner_input_attribute_updates() -> None:
    options = {"material_change_threshold_percent": 5}
    old_forecast = [{"valid_at": "2026-06-27T10:00:00+00:00", "value": 1.0}]
    new_forecast = [{"valid_at": "2026-06-27T10:00:00+00:00", "value": 2.0}]

    assert _is_material_state_change(
        FakeEvent(
            "sensor.pv_forecast",
            "1.0",
            "1.0",
            old_attributes={"forecast": old_forecast, "friendly_name": "PV old"},
            new_attributes={"forecast": new_forecast, "friendly_name": "PV new"},
        ),
        options,
    )
    assert not _is_material_state_change(
        FakeEvent(
            "sensor.pv_forecast",
            "1.0",
            "1.0",
            old_attributes={"forecast": old_forecast, "friendly_name": "PV old"},
            new_attributes={"forecast": old_forecast, "friendly_name": "PV new"},
        ),
        options,
    )


def test_material_attribute_change_overrides_subthreshold_numeric_state_change() -> None:
    assert _is_material_state_change(
        FakeEvent(
            "weather.home",
            "20.0",
            "20.1",
            old_attributes={"temperature": 20.0},
            new_attributes={"temperature": 21.0},
        ),
        {"material_change_threshold_percent": 5},
    )


def test_material_state_change_canonicalizes_camel_case_forecast_attributes() -> None:
    changes = (
        ("pvEstimate", [1.0, 2.0], [2.0, 3.0]),
        ("baselineLoadForecastKw", [0.5, 0.6], [0.7, 0.8]),
        ("forecastConfidence", 0.8, 0.9),
        ("unitOfMeasurement", "W", "kW"),
        ("forecastIntervalMinutes", 30, 60),
        ("intervalMinutes", 30, 15),
        ("resolutionMinutes", 30, 15),
        ("detailedForecast", [{"value": 1.0}], [{"value": 2.0}]),
    )

    for key, old_value, new_value in changes:
        assert _is_material_state_change(
            FakeEvent(
                "sensor.forecast",
                "unchanged",
                "unchanged",
                old_attributes={key: old_value},
                new_attributes={key: new_value},
            ),
            {"material_change_threshold_percent": 5},
        ), key


def test_overrides_restored_only_when_active() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    overrides = _overrides_from_store(
        {
            "overrides": [
                {
                    "kind": "manual_hvac",
                    "source": "service",
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    "reason": "active",
                },
                {
                    "kind": "manual_hvac",
                    "source": "service",
                    "expires_at": (now - timedelta(minutes=5)).isoformat(),
                    "reason": "expired",
                },
                {
                    "kind": "manual_ev_charging",
                    "source": "button",
                    "expires_at": (now.replace(tzinfo=None) - timedelta(minutes=5)).isoformat(),
                    "reason": "legacy_naive_expired",
                },
                {
                    "kind": "manual_ev_charging",
                    "source": "button",
                    "expires_at": "not-a-timestamp",
                    "reason": "malformed_expiry",
                },
                {
                    "kind": "manual_hvac",
                    "source": "service",
                    "reason": "missing_expiry",
                },
            ]
        },
        now,
    )
    assert len(overrides) == 1
    assert overrides[0].reason == "active"


def test_unexpired_overrides_prunes_runtime_expiry_and_keeps_unbounded_values() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    active = Override(
        "manual_ev_charging",
        "button",
        now + timedelta(minutes=5),
        "manual_start",
    )
    unbounded = Override("manual_hvac", "service", None, "manual")
    expired = Override(
        "manual_ev_charging",
        "button",
        now.replace(tzinfo=None) - timedelta(minutes=5),
        "manual_stop",
    )

    assert _unexpired_overrides([expired, active, unbounded], now) == [
        active,
        unbounded,
    ]


def test_seconds_until_next_interval_boundary() -> None:
    assert (
        _seconds_until_next_interval_boundary(
            datetime(2026, 6, 27, 10, 3, 30, tzinfo=UTC),
            5,
        )
        == 90.0
    )
    assert (
        _seconds_until_next_interval_boundary(
            datetime(2026, 6, 27, 10, 5, 0, tzinfo=UTC),
            5,
        )
        == 300.0
    )
    assert (
        _seconds_until_next_interval_boundary(
            datetime(2026, 6, 27, 11, 20, 0, tzinfo=UTC),
            40,
        )
        == 2400.0
    )


def test_start_listeners_schedules_configured_boundary_refresh_without_entities(monkeypatch: object) -> None:
    calls: list[float] = []

    def fake_async_call_later(hass: object, delay: float, action: object) -> object:
        calls.append(delay)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_async_call_later,
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = FakeEntry({}, {CONF_PLANNING_INTERVAL_MINUTES: 15})
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []

    coordinator.async_start_listeners()

    assert len(calls) == 1
    assert 0 < calls[0] <= 900
    assert coordinator._unsub_listeners == []


def test_startup_auto_recovery_begins_only_after_home_assistant_started(monkeypatch: object) -> None:
    class Task:
        def __init__(self, *, done: bool = False) -> None:
            self.completed = done
            self.cancelled = False

        def done(self) -> bool:
            return self.completed

        def cancel(self) -> None:
            self.cancelled = True

    class Entry:
        def __init__(self) -> None:
            self.created = 0
            self.hass: object | None = None
            self.task_name: str | None = None
            self.task = Task()

        def async_create_background_task(self, hass: object, coroutine: object, name: str) -> Task:
            self.created += 1
            self.hass = hass
            self.task_name = name
            coroutine.close()
            return self.task

    callbacks: list[object] = []
    unsubscribed: list[bool] = []

    def at_started(hass: object, action: object) -> object:
        callbacks.append(action)
        return lambda: unsubscribed.append(True)

    monkeypatch.setattr(coordinator_module, "async_at_started", at_started)

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = object()
    coordinator.entry = Entry()
    coordinator._startup_auto_recovery_authorized = False
    coordinator._startup_auto_recovery_task = None
    coordinator._startup_auto_recovery_start_unsub = None
    coordinator._startup_auto_recovery_wakeup = asyncio.Event()
    coordinator.store = FakeStore({"production": {"armed": True}})
    coordinator.executor = FakeExecutor()
    coordinator._async_update_startup_auto_recovery = AsyncMock()

    coordinator.async_start_startup_auto_recovery()
    assert coordinator.entry.created == 0

    coordinator._startup_auto_recovery_authorized = True
    coordinator.async_start_startup_auto_recovery()
    coordinator.async_start_startup_auto_recovery()
    assert coordinator.entry.created == 0
    assert len(callbacks) == 1

    asyncio.run(callbacks[0](coordinator.hass))
    assert coordinator.entry.created == 1
    assert coordinator.entry.hass is coordinator.hass
    assert coordinator.entry.task_name == "ha_energy_planner startup automatic-control recovery"
    coordinator._async_update_startup_auto_recovery.assert_awaited_once()

    coordinator._wake_startup_auto_recovery()
    assert coordinator._startup_auto_recovery_wakeup.is_set()

    coordinator._tearing_down = False
    coordinator._debounce_cancel = None
    coordinator._boundary_cancel = None
    coordinator._ai_advice_task = None
    coordinator._unsub_listeners = []
    coordinator.async_shutdown()
    assert coordinator.entry.task.cancelled is True
    assert coordinator._startup_auto_recovery_authorized is False


def test_start_listeners_retries_pending_load_training_when_source_appears(monkeypatch: object) -> None:
    tracked: list[tuple[list[str], object]] = []
    scheduled: list[tuple[float, object]] = []
    unsubscribed: list[str] = []

    def fake_track(hass: object, entity_ids: list[str], action: object) -> object:
        tracked.append((entity_ids, action))
        return lambda: unsubscribed.append(entity_ids[0])

    def fake_call_later(hass: object, delay: float, action: object) -> object:
        scheduled.append((delay, action))
        return lambda: None

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
        fake_track,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_call_later,
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass({"sensor.house_load": "unavailable"})
    coordinator.entry = FakeEntry(
        {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
        {CONF_PLANNING_INTERVAL_MINUTES: 60},
    )
    coordinator._load_forecast_training_attempted = False
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []
    coordinator._refresh_generation = 0
    coordinator._force_next_refresh = False

    coordinator.async_start_listeners()
    source_callback = tracked[0][1]
    source_callback(FakeEvent("sensor.house_load", "unavailable", "unknown"))
    assert len(scheduled) == 1

    source_callback(FakeEvent("sensor.house_load", "unknown", "1.2"))
    source_callback(FakeEvent("sensor.house_load", "1.2", "1.3"))

    assert tracked[0][0] == ["sensor.house_load"]
    assert unsubscribed == ["sensor.house_load"]
    assert coordinator._unsub_listeners == []
    assert len(scheduled) == 2
    assert scheduled[-1][0] == 0
    assert coordinator._refresh_generation == 1
    assert coordinator._force_next_refresh is True

    coordinator._load_forecast_training_attempted = True
    coordinator._start_load_forecast_source_listener(coordinator.entry_data)
    assert len(tracked) == 1


def test_start_listeners_closes_load_source_setup_race(monkeypatch: object) -> None:
    scheduled: list[float] = []
    unsubscribed: list[str] = []

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
        lambda hass, entity_ids, action: lambda: unsubscribed.append(entity_ids[0]),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        lambda hass, delay, action: scheduled.append(delay) or (lambda: None),
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass({"sensor.house_load": "1.2"})
    coordinator.entry = FakeEntry(
        {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
        {CONF_PLANNING_INTERVAL_MINUTES: 60},
    )
    coordinator._load_forecast_training_attempted = False
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []
    coordinator._refresh_generation = 0
    coordinator._force_next_refresh = False

    coordinator.async_start_listeners()

    assert unsubscribed == ["sensor.house_load"]
    assert coordinator._unsub_listeners == []
    assert len(scheduled) == 2
    assert scheduled[-1] == 0
    assert coordinator._refresh_generation == 1
    assert coordinator._force_next_refresh is True


def test_coordinator_init_sets_runtime_state_without_real_data_update_coordinator(
    monkeypatch: object, caplog: object
) -> None:
    def fake_data_update_init(
        self: object, hass: object, *, logger: object, name: str, update_interval: object
    ) -> None:
        self.hass = hass
        self.data = None

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.DataUpdateCoordinator.__init__",
        fake_data_update_init,
    )
    store = FakeStore(
        {
            "overrides": [
                {
                    "kind": "manual_hvac",
                    "source": "store",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "reason": "active",
                }
            ]
        }
    )
    entry = FakeEntry(
        {"amber_import_price_entity": "sensor.import"},
        {CONF_DEFAULT_READY_BY: "06:30", "ai_enabled": True},
    )

    coordinator = EnergyPlannerCoordinator(FakeHass(), entry, store)

    assert coordinator.entry is entry
    assert coordinator.store is store
    assert coordinator.ready_by == "06:30"
    assert coordinator.executor.entry_data == {"amber_import_price_entity": "sensor.import"}
    assert coordinator.executor.notification_grace_until is not None
    assert coordinator.executor.notification_grace_until > datetime.now(UTC)
    assert coordinator.planner_enabled is False
    assert coordinator.dry_run is True
    assert len(coordinator.overrides) == 1
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)

    helper_coordinator = EnergyPlannerCoordinator(
        FakeHass({"input_boolean.override": "on"}),
        FakeEntry(
            {CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.override"},
            {"manual_hvac_override_minutes": 45},
        ),
        FakeStore(),
    )
    assert len(helper_coordinator.overrides) == 1
    helper_override = helper_coordinator.overrides[0]
    assert helper_override.kind == "manual_hvac"
    assert helper_override.source == "helper"
    assert helper_override.reason == "manual_override_helper_on"
    assert timedelta(minutes=44) < helper_override.expires_at - datetime.now(UTC) <= timedelta(minutes=45)

    legacy_helper_coordinator = EnergyPlannerCoordinator(
        FakeHass({"input_boolean.override": "on"}),
        FakeEntry(
            {CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.override"},
            {"manual_hvac_override_minutes": 30},
        ),
        FakeStore(
            {
                "overrides": [
                    {
                        "kind": "manual_hvac",
                        "source": "helper",
                        "expires_at": None,
                        "reason": "manual_override_helper_on",
                    }
                ]
            }
        ),
    )
    assert len(legacy_helper_coordinator.overrides) == 1
    assert legacy_helper_coordinator.overrides[0].expires_at is not None
    assert timedelta(minutes=29) < (
        legacy_helper_coordinator.overrides[0].expires_at - datetime.now(UTC)
    ) <= timedelta(minutes=30)

    expired_helper_coordinator = EnergyPlannerCoordinator(
        FakeHass({"input_boolean.override": "on"}),
        FakeEntry({CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.override"}),
        FakeStore(
            {
                "ownership": {
                    "manual_hvac_override_expires_at": "2000-01-01T00:00:00+00:00",
                }
            }
        ),
    )
    assert expired_helper_coordinator.overrides == []

    coordinator.entry.options["planner_enabled"] = "true"
    coordinator.entry.options["dry_run"] = "false"
    assert coordinator.planner_enabled is False
    assert coordinator.dry_run is True


def test_async_update_data_uses_lock_and_delay_save() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()

    async def fake_locked(*, defer_execution: bool = False) -> EnergyPlan:
        assert defer_execution is True
        return _plan("locked")

    coordinator._async_update_data_locked = fake_locked

    result = asyncio.run(coordinator._async_update_data())

    assert result.plan_id == "locked"
    assert coordinator.store.delay_entered is True
    assert coordinator.store.delay_exited is True


def test_async_update_data_schedules_device_execution_after_refresh_scopes_exit() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._planner_lock = asyncio.Lock()
    coordinator.store = FakeStore()
    plan = _plan("deferred-execution")
    context = object()
    request = (1, plan, context, {"planner_enabled": True})
    scheduled: list[object] = []

    async def fake_locked(*, defer_execution: bool = False) -> EnergyPlan:
        assert defer_execution is True
        assert coordinator._planner_lock.locked() is True
        assert coordinator.store.delay_entered is True
        assert not hasattr(coordinator.store, "delay_exited")
        coordinator._deferred_plan_execution = request
        return plan

    def schedule(execution: object) -> None:
        assert coordinator._planner_lock.locked() is False
        assert coordinator.store.delay_exited is True
        scheduled.append(execution)

    coordinator._async_update_data_locked = fake_locked
    coordinator._schedule_plan_execution = schedule

    result = asyncio.run(coordinator._async_update_data())

    assert result is plan
    assert scheduled == [request]


def test_plan_execution_releases_refresh_lock_and_coalesces_newer_plans() -> None:
    async def scenario() -> list[str]:
        started = asyncio.Event()
        release = asyncio.Event()

        class TaskHass(FakeHass):
            def async_create_task(self, task: object) -> asyncio.Task[None]:
                return asyncio.create_task(task)  # type: ignore[arg-type]

        class BlockingExecutor(FakeExecutor):
            async def async_evaluate(
                self,
                plan: EnergyPlan,
                context: object,
            ) -> PlanAction | None:
                self.evaluated.append((plan, context))
                if plan.plan_id == "first":
                    started.set()
                    await release.wait()
                return None

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = TaskHass()
        coordinator.entry = FakeEntry({})
        coordinator.executor = BlockingExecutor()
        coordinator._planner_lock = asyncio.Lock()
        coordinator._command_lock = asyncio.Lock()
        coordinator._plan_execution_task = None
        coordinator._pending_plan_execution = None
        coordinator._tearing_down = False
        coordinator._refresh_generation = 1
        coordinator.async_update_listeners = lambda: None
        first = _plan("first")
        skipped = _plan("skipped")
        newest = _plan("newest")

        coordinator._schedule_plan_execution((1, first, object(), {}))
        execution_task = coordinator._plan_execution_task
        assert execution_task is not None
        await asyncio.wait_for(started.wait(), timeout=1)

        # Device confirmation may still be waiting, but input collection and
        # planning can immediately acquire the independent refresh lock.
        await asyncio.wait_for(coordinator._planner_lock.acquire(), timeout=0.1)
        coordinator._planner_lock.release()

        coordinator._refresh_generation = 2
        coordinator._schedule_plan_execution((2, skipped, object(), {}))
        coordinator._refresh_generation = 3
        coordinator._schedule_plan_execution((3, newest, object(), {}))
        release.set()
        await asyncio.wait_for(execution_task, timeout=1)
        return [plan.plan_id for plan, _context in coordinator.executor.evaluated]

    assert asyncio.run(scenario()) == ["first", "newest"]


def test_plan_execution_failure_does_not_strand_newer_plan_and_notifies() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        updates: list[str] = []

        class FailingExecutor(FakeExecutor):
            async def async_evaluate(
                self,
                plan: EnergyPlan,
                context: object,
            ) -> PlanAction | None:
                self.evaluated.append((plan, context))
                if plan.plan_id == "failed":
                    coordinator._refresh_generation = 2
                    coordinator._pending_plan_execution = (
                        2,
                        _plan("newer"),
                        object(),
                        {},
                    )
                    raise RuntimeError("unexpected executor failure")
                return None

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.entry = FakeEntry({})
        coordinator.executor = FailingExecutor()
        coordinator._command_lock = asyncio.Lock()
        coordinator._plan_execution_task = object()
        coordinator._pending_plan_execution = (1, _plan("failed"), object(), {})
        coordinator._tearing_down = False
        coordinator._refresh_generation = 1
        coordinator.async_update_listeners = lambda: updates.append("updated")

        await coordinator._async_drain_plan_execution()
        evaluated = [plan.plan_id for plan, _context in coordinator.executor.evaluated]
        return evaluated, updates

    evaluated, updates = asyncio.run(scenario())

    assert evaluated == ["failed", "newer"]
    assert updates == ["updated", "updated"]


def test_start_listeners_handles_manual_ev_and_material_changes(monkeypatch: object) -> None:
    callbacks: list[object] = []
    scheduled: list[float] = []

    def fake_track(hass: object, entity_ids: list[str], callback: object) -> object:
        callbacks.append(callback)
        return lambda: scheduled.append(-1)

    def fake_call_later(hass: object, delay: float, action: object) -> object:
        scheduled.append(delay)
        return lambda: scheduled.append(-2)

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
        fake_track,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_call_later,
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass({"input_boolean.scheduler": "off"})
    coordinator.entry = FakeEntry(
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_EV_CONNECTED: "binary_sensor.ev_connected",
            CONF_EV_SOC: "sensor.ev_soc",
            "amber_import_price_entity": "sensor.price",
        },
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
    )
    coordinator.store = FakeStore(
        {
            "ownership": {},
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": datetime.now(UTC),
                    "desired_state": {"hvac_mode": "heat"},
                }
            ],
        }
    )
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []
    coordinator._refresh_generation = 0

    coordinator.async_start_listeners()
    callback = callbacks[0]
    callback(FakeEvent("climate.daikin", "off", "heat"))
    assert coordinator.hass.created_tasks == []
    coordinator.store.data["execution_audit"] = []
    callback(FakeEvent("climate.daikin", "off", "heat"))
    callback(FakeEvent("sensor.ev_soc", "50", "51"))
    callback(FakeEvent("sensor.price", "100", "110"))

    assert len(coordinator.hass.created_tasks) == 1
    assert coordinator._refresh_generation == 1
    assert coordinator._debounce_cancel is not None
    assert len(scheduled) >= 2


def test_start_listeners_handles_override_helper_and_takeover_zone_changes(monkeypatch: object) -> None:
    callbacks: list[object] = []

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_track_state_change_event",
        lambda hass, entity_ids, callback: callbacks.append(callback) or (lambda: None),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        lambda hass, delay, action: (lambda: None),
    )
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass(
        {
            "input_boolean.override": "off",
            "switch.zone": "on",
            "climate.zone_temperature": FakeState(
                "heat",
                {"temperature": 20},
            ),
            "climate.daikin": "heat",
            "input_boolean.scheduler_change": "off",
        }
    )
    coordinator.entry = FakeEntry(
        {
            CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.override",
            CONF_CLIMATE_ZONES: ["switch.zone", "climate.zone_temperature"],
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
        },
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
    )
    coordinator.store = FakeStore({"ownership": {"hvac_control": {"phase": "peak_coast"}}})
    coordinator.executor = FakeExecutor()
    coordinator.overrides = []
    coordinator._boundary_cancel = None
    coordinator._debounce_cancel = None
    coordinator._unsub_listeners = []
    coordinator._refresh_generation = 0

    coordinator.async_start_listeners()
    callback = callbacks[0]
    callback(FakeEvent("input_boolean.override", "off", "on"))
    coordinator._manual_override_helper_guard = ("off", datetime.now(UTC) + timedelta(minutes=1))
    callback(FakeEvent("input_boolean.override", "on", "off"))
    assert coordinator._manual_override_helper_guard is None

    manual_changes: list[tuple[str, str | None, bool]] = []

    def capture_manual_change(
        reason: str,
        *,
        preserve_zone_entity_id: str | None = None,
        preserve_main_state: bool = False,
    ) -> object:
        manual_changes.append((reason, preserve_zone_entity_id, preserve_main_state))

        async def complete() -> None:
            return None

        return complete()

    coordinator._async_handle_manual_hvac_change = capture_manual_change
    callback(FakeEvent("switch.zone", "on", "off"))
    callback(
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        )
    )
    coordinator.executor.pending_hvac_desired_state = {
        "target_temperature": 21,
    }
    callback(
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        )
    )
    coordinator.executor.pending_hvac_desired_state.update(
        {
            "enable_zones": True,
            "configured_zones_only": True,
        }
    )
    callback(
        FakeEvent(
            "climate.zone_temperature",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 24},
        )
    )
    coordinator.hass.states.values["input_boolean.scheduler_change"] = "on"
    callback(
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 21},
            new_attributes={"temperature": 24},
        )
    )

    assert len(coordinator.hass.created_tasks) == 5
    assert coordinator.executor.pending_hvac_manual_overrides == 1
    assert coordinator.executor.pending_hvac_manual_zone_overrides == [
        "climate.zone_temperature"
    ]
    assert coordinator.executor.pending_hvac_desired_state == {
        "target_temperature": 21,
        "enable_zones": True,
        "configured_zones_only": True,
        "manual_zone_entity_ids": ["climate.zone_temperature"],
        "manual_override_detected": True,
    }
    assert manual_changes == [
        ("climate_zone_changed", "switch.zone", False),
        ("daikin_state_changed", None, True),
        ("climate_zone_changed", "climate.zone_temperature", False),
        ("daikin_state_changed", None, True),
    ]

    startup = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    startup.hass = FakeHass({"input_boolean.override": "on"})
    startup.entry = coordinator.entry
    startup.store = FakeStore({"ownership": {}})
    startup.executor = FakeExecutor()
    startup.overrides = []
    startup._boundary_cancel = None
    startup._debounce_cancel = None
    startup._unsub_listeners = []
    startup._refresh_generation = 0
    startup.async_start_listeners()
    assert len(startup.hass.created_tasks) == 1

    startup_off = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    startup_off.hass = FakeHass({"input_boolean.override": "off"})
    startup_off.entry = coordinator.entry
    startup_off.store = FakeStore({"ownership": {}})
    startup_off.executor = FakeExecutor()
    startup_off.overrides = [
        Override(
            kind="manual_hvac",
            source="helper",
            expires_at=None,
            reason="manual_override_helper_on",
        )
    ]
    startup_off._boundary_cancel = None
    startup_off._debounce_cancel = None
    startup_off._unsub_listeners = []
    startup_off._refresh_generation = 0
    startup_off._async_handle_manual_override_helper = AsyncMock()
    startup_off.async_start_listeners()
    assert len(startup_off.hass.created_tasks) == 1
    startup_off._async_handle_manual_override_helper.assert_called_once_with(False)


def test_manual_override_and_zone_change_helpers_cover_invalid_events() -> None:
    entry_data = {
        CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.override",
        CONF_CLIMATE_ZONES: ["switch.zone"],
    }
    missing_new = FakeEvent("input_boolean.override", "off", "on")
    missing_new.data["new_state"] = None
    assert not _is_manual_override_helper_change(entry_data, missing_new)

    initial_on = FakeEvent("input_boolean.override", "off", "on")
    initial_on.data["old_state"] = None
    assert _is_manual_override_helper_change(entry_data, initial_on)
    assert not _is_manual_override_helper_change(entry_data, FakeEvent("input_boolean.override", "on", "on"))

    zone_event = FakeEvent("switch.zone", "on", "off")
    assert not _is_manual_hvac_zone_change(FakeHass(), entry_data, {"ownership": {}}, zone_event)
    assert _is_manual_hvac_zone_change(
        FakeHass(),
        entry_data,
        {"ownership": {"hvac_control": {"phase": "peak_coast"}}},
        zone_event,
    )
    assert not _is_manual_hvac_zone_change(
        FakeHass(),
        entry_data,
        {"ownership": {"hvac_control": {"phase": "peak_coast"}}},
        FakeEvent("switch.zone", "on", "on"),
    )
    zone_event.data["new_state"] = None
    assert not _is_manual_hvac_zone_change(
        FakeHass(),
        entry_data,
        {"ownership": {"hvac_control": {"phase": "peak_coast"}}},
        zone_event,
    )

    climate_entry_data = {CONF_CLIMATE_ZONES: ["climate.zone_temperature"]}
    climate_zone_event = FakeEvent(
        "climate.zone_temperature",
        "heat",
        "heat",
        old_attributes={"temperature": 21},
        new_attributes={"temperature": 22},
    )
    assert _is_manual_hvac_zone_change(
        FakeHass(),
        climate_entry_data,
        {"ownership": {"hvac_control": {"phase": "peak_coast"}}},
        climate_zone_event,
    )

    guarded_climate_entry_data = {
        **climate_entry_data,
        CONF_CLIMATE_CHANGE_FROM_SCHEDULER: "input_boolean.scheduler_change",
    }
    assert not _is_manual_hvac_zone_change(
        FakeHass({"input_boolean.scheduler_change": "on"}),
        guarded_climate_entry_data,
        {"ownership": {"hvac_control": {"phase": "peak_coast"}}},
        climate_zone_event,
    )


def test_obsolete_planner_result_does_not_save_or_execute() -> None:
    previous = _plan("previous")
    stale = _plan("stale")
    coordinator = _coordinator_for_commit(previous, current_generation=2)

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            1,
            stale,
            object(),
            {"planner_enabled": True},
        )
    )

    assert result is previous
    assert coordinator.store.saved_plans == []
    assert coordinator.executor.evaluated == []


def test_obsolete_planner_result_schedules_refresh_when_hass_present() -> None:
    previous = _plan("previous")
    stale = _plan("stale")
    coordinator = _coordinator_for_commit(previous, current_generation=2)
    coordinator.hass = FakeHass()

    result = asyncio.run(coordinator._async_commit_plan_if_current(1, stale, object(), {"planner_enabled": True}))

    assert result is previous
    assert len(coordinator.hass.created_tasks) == 1


def test_current_planner_result_saves_and_executes() -> None:
    plan = _plan("current")
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            plan,
            context,
            {"planner_enabled": True},
        )
    )

    assert result is plan
    assert coordinator.store.saved_plans == [plan]
    assert coordinator.executor.evaluated == [(plan, context)]
    assert coordinator.executor.options == {"planner_enabled": True}
    assert coordinator._last_decision_context is context


def test_current_planner_result_can_defer_execution_until_refresh_scopes_exit() -> None:
    plan = _plan("deferred-current")
    context = object()
    options = {"planner_enabled": True}
    coordinator = _coordinator_for_commit(None, current_generation=3)

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            plan,
            context,
            options,
            execute=False,
        )
    )

    assert result is plan
    assert coordinator.store.saved_plans == [plan]
    assert coordinator.executor.evaluated == []
    assert coordinator._deferred_plan_execution == (3, plan, context, options)


def test_plan_execution_scheduler_handles_teardown_and_non_task_test_hass() -> None:
    plan = _plan("scheduled")
    request = (1, plan, object(), {})
    coordinator = _coordinator_for_runtime_services()
    coordinator._plan_execution_task = None
    coordinator._pending_plan_execution = None
    coordinator._tearing_down = True

    coordinator._schedule_plan_execution(request)
    assert coordinator._pending_plan_execution is None

    coordinator._tearing_down = False
    coordinator._schedule_plan_execution(request)
    assert coordinator._plan_execution_task is None
    assert coordinator._pending_plan_execution is None


def test_plan_execution_drain_and_executor_drop_stale_generations() -> None:
    coordinator = _coordinator_for_runtime_services()
    coordinator._plan_execution_task = object()
    coordinator._pending_plan_execution = (1, _plan("stale-drain"), object(), {})
    coordinator._refresh_generation = 2

    asyncio.run(coordinator._async_drain_plan_execution())

    assert coordinator._plan_execution_task is None
    assert coordinator.executor.evaluated == []

    asyncio.run(
        coordinator._async_execute_plan_if_current(
            1,
            _plan("stale-direct"),
            object(),
            {},
        )
    )
    assert coordinator.executor.evaluated == []


def test_startup_recovery_validation_commits_without_executing() -> None:
    plan = _plan("startup-validation")
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)
    coordinator._startup_auto_recovery_validation_active = True
    coordinator._last_startup_auto_recovery_validation = {
        "plan_id": plan.plan_id,
        "healthy": True,
        "safe": True,
        "violations": [],
        "committed": False,
    }

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            plan,
            context,
            {"planner_enabled": True},
        )
    )

    assert result is plan
    assert coordinator.store.saved_plans == [plan]
    assert coordinator.executor.evaluated == []
    assert coordinator._last_startup_auto_recovery_validation["committed"] is True


def test_startup_recovery_validation_candidate_and_result_branches(monkeypatch: object) -> None:
    coordinator = _coordinator_for_runtime_services()
    plan = _plan("candidate")
    coordinator._startup_auto_recovery_validation_active = False
    coordinator._last_startup_auto_recovery_validation = None
    coordinator._record_startup_auto_recovery_validation_candidate(plan, [])
    assert coordinator._last_startup_auto_recovery_validation is None

    coordinator._startup_auto_recovery_validation_active = True
    coordinator._record_startup_auto_recovery_validation_candidate(plan, ["unsafe"])
    assert coordinator._last_startup_auto_recovery_validation == {
        "plan_id": "candidate",
        "healthy": True,
        "safe": True,
        "violations": ["unsafe"],
        "committed": False,
    }

    async def fail_refresh() -> None:
        raise RuntimeError("refresh failed")

    coordinator.async_refresh = fail_refresh
    coordinator.async_request_refresh = AsyncMock(side_effect=AssertionError("debounced refresh used"))
    assert asyncio.run(coordinator._async_run_startup_auto_recovery_validation()) == (
        False,
        "validation_refresh_failed",
    )

    async def no_commit() -> None:
        return None

    coordinator.async_refresh = no_commit
    assert asyncio.run(coordinator._async_run_startup_auto_recovery_validation()) == (
        False,
        "validation_plan_not_committed",
    )

    async def unsafe_commit() -> None:
        coordinator._last_startup_auto_recovery_validation = {
            "committed": True,
            "healthy": False,
            "safe": False,
            "violations": ["unsafe"],
        }

    coordinator.async_refresh = unsafe_commit
    assert asyncio.run(coordinator._async_run_startup_auto_recovery_validation()) == (
        False,
        "validation_plan_unsafe",
    )

    async def healthy_commit() -> None:
        coordinator._last_startup_auto_recovery_validation = {
            "committed": True,
            "healthy": True,
            "safe": True,
            "violations": [],
        }

    coordinator.async_refresh = healthy_commit
    blocked = {
        "entities": {"missing": [], "unavailable": []},
        "services": {"missing": [], "unavailable": []},
        "control_areas": {
            "required": ["ev"],
            "ready": ["ev"],
            "available": ["ev"],
            "confidence_eligible": ["ev"],
        },
        "discovery": {"ev": {"supported": True}},
        "recorder": {"available": True},
        "checks": [{"check": "control_not_paused", "ok": True}],
        "current_plan": {"safe": False},
    }
    monkeypatch.setattr(coordinator_module, "build_preflight_report", lambda hass, item: blocked)
    assert asyncio.run(coordinator._async_run_startup_auto_recovery_validation()) == (
        False,
        "current_plan_unsafe",
    )
    blocked["current_plan"] = {"safe": True}
    assert asyncio.run(coordinator._async_run_startup_auto_recovery_validation()) == (
        True,
        "validation_succeeded",
    )
    coordinator.async_request_refresh.assert_not_awaited()


def test_teardown_discards_queued_current_planner_result() -> None:
    previous = _plan("previous")
    queued = _plan("queued-during-unload")
    coordinator = _coordinator_for_commit(previous, current_generation=3)
    coordinator._tearing_down = True

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            queued,
            object(),
            {"planner_enabled": True},
        )
    )

    assert result is previous
    assert coordinator.store.saved_plans == []
    assert coordinator.executor.evaluated == []


def test_current_plan_evaluates_each_coordinated_action() -> None:
    plan = _plan("current-with-ev")
    now = plan.created_at
    higher_priority = PlanAction(
        action_id="hvac",
        plan_id=plan.plan_id,
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.SET_HVAC,
        desired_state={"hvac_mode": "heat"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    ev_action = PlanAction(
        action_id="ev",
        plan_id=plan.plan_id,
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={"charging_required_now": True},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan.actions = [higher_priority, ev_action]
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)

    result = asyncio.run(coordinator._async_commit_plan_if_current(3, plan, context, {"planner_enabled": True}))

    assert result is plan
    assert coordinator.executor.evaluated == [
        (plan, context),
        (replace(plan, actions=[ev_action]), context),
    ]


def test_current_plan_skips_safety_action_consumed_ahead_of_next_action() -> None:
    plan = _plan("current-with-release")
    now = plan.created_at
    ev_action = _coordinated_action(plan, "ev", ActionAsset.EV, ActionKind.EV_START)
    release_action = PlanAction(
        action_id="hvac-release",
        plan_id=plan.plan_id,
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.DAIKIN,
        kind=ActionKind.RELEASE_HVAC,
        desired_state={"release_reason": "comfort"},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    plan.actions = [ev_action, release_action]
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)

    class SafetySelectingExecutor(FakeExecutor):
        async def async_evaluate(self, evaluated_plan: EnergyPlan, evaluated_context: object) -> PlanAction | None:
            await super().async_evaluate(evaluated_plan, evaluated_context)
            return release_action if len(evaluated_plan.actions) > 1 else None

    coordinator.executor = SafetySelectingExecutor()

    result = asyncio.run(coordinator._async_commit_plan_if_current(3, plan, context, {"planner_enabled": True}))

    assert result is plan
    assert coordinator.executor.evaluated == [
        (plan, context),
        (replace(plan, actions=[ev_action]), context),
    ]


def test_current_plan_stops_coordinated_actions_after_generation_changes() -> None:
    plan = _plan("current-with-follow-up")
    plan.actions = [
        _coordinated_action(plan, "first", ActionAsset.EV, ActionKind.EV_START),
        _coordinated_action(
            plan,
            "follow-up",
            ActionAsset.ENPHASE,
            ActionKind.SET_PROFILE,
        ),
    ]
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)
    coordinator.hass = FakeHass()

    class InvalidatingExecutor(FakeExecutor):
        async def async_evaluate(self, evaluated_plan: EnergyPlan, evaluated_context: object) -> None:
            await super().async_evaluate(evaluated_plan, evaluated_context)
            coordinator._refresh_generation += 1

    coordinator.executor = InvalidatingExecutor()

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            plan,
            context,
            {"planner_enabled": True},
        )
    )

    assert result is plan
    assert coordinator.executor.evaluated == [(plan, context)]
    assert len(coordinator.hass.created_tasks) == 1


def test_current_plan_stops_coordinated_actions_when_teardown_starts() -> None:
    plan = _plan("current-with-follow-up")
    plan.actions = [
        _coordinated_action(plan, "first", ActionAsset.EV, ActionKind.EV_START),
        _coordinated_action(
            plan,
            "follow-up",
            ActionAsset.ENPHASE,
            ActionKind.SET_PROFILE,
        ),
    ]
    context = object()
    coordinator = _coordinator_for_commit(None, current_generation=3)

    class TeardownExecutor(FakeExecutor):
        async def async_evaluate(self, evaluated_plan: EnergyPlan, evaluated_context: object) -> None:
            await super().async_evaluate(evaluated_plan, evaluated_context)
            coordinator._tearing_down = True

    coordinator.executor = TeardownExecutor()

    result = asyncio.run(
        coordinator._async_commit_plan_if_current(
            3,
            plan,
            context,
            {"planner_enabled": True},
        )
    )

    assert result is plan
    assert coordinator.executor.evaluated == [(plan, context)]


def test_planner_options_include_runtime_ready_by_override() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry({}, {CONF_DEFAULT_READY_BY: "07:00"})
    coordinator.ready_by = "08:30"

    assert coordinator.options[CONF_DEFAULT_READY_BY] == "07:00"
    assert coordinator.planner_options[CONF_DEFAULT_READY_BY] == "08:30"
    assert coordinator.entry.options[CONF_DEFAULT_READY_BY] == "07:00"


def test_ai_advice_is_rate_limited_to_five_minutes() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore(
        {
            "ai_recommendations": [
                {
                    "created_at": (now - timedelta(seconds=120)).isoformat(),
                    "status": "accepted",
                    "service_called": "ai_task.generate_data",
                }
            ]
        }
    )
    context = SimpleNamespace(created_at=now)

    result, should_store = asyncio.run(coordinator._async_get_throttled_ai_advice(context, _plan("plan-1"), {}, {}))

    assert should_store is False
    assert result.status == "skipped"
    assert result.rejected_reason == "ai_rate_limited"
    assert result.service_called is None
    assert result.rejected_detail["retry_after_seconds"] == 180


def test_ai_advice_notification_messages_cover_each_result() -> None:
    assert (
        _ai_advice_notification_message(
            AIAdviceResult(
                "accepted",
                {"outcome": "no_action_needed", "summary": "The current plan is healthy."},
                None,
                "ai_task.generate_data",
            )
        )
        == "**No action needed.**\n\nThe current plan is healthy."
    )

    action_message = _ai_advice_notification_message(
        AIAdviceResult(
            "accepted",
            {
                "outcome": "action_required",
                "summary": "The PV forecast needs attention.",
                "affected_item": "pv_forecast_entity",
                "problem": "Forecast data is stale.",
                "next_step": "Check the mapped forecast entity.",
                "expected_benefit": "Planning can use current solar data.",
                "verification": "Run Explain again after the entity updates.",
            },
            None,
            "ai_task.generate_data",
        )
    )
    assert "**Affected item:** PV forecast input" in action_message
    assert "**Next step:** Check the mapped forecast entity." in action_message

    assert (
        _ai_advice_notification_message(
            AIAdviceResult(
                "rejected",
                {},
                "ai_response_not_json",
                "ai_task.generate_data",
                {"message": "The provider returned invalid data."},
            )
        )
        == "**No explanation available.**\n\nThe provider returned invalid data."
    )
    assert _ai_advice_notification_message(AIAdviceResult("rejected", {}, "unknown", None, {})).endswith(
        "The AI response was not usable. Try again."
    )


def test_ai_advice_notification_failure_does_not_discard_result(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = SimpleNamespace()
    asyncio.run(coordinator._async_notify_ai_advice("No notification service."))

    async def fail_notification(*args: object, **kwargs: object) -> None:
        raise RuntimeError("notification failed")

    coordinator.hass = SimpleNamespace(services=SimpleNamespace(async_call=fail_notification))
    coordinator.entry = FakeEntry({})
    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator._async_notify_ai_advice("Still preserve the result."))

    assert "Could not publish the Energy Planner explanation notification" in caplog.text


def test_ai_advice_notification_is_deferred_during_startup(monkeypatch: object) -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.hass.data = {}
    coordinator.hass.state = CoreState.starting
    coordinator.entry = FakeEntry({})
    start_callbacks: list[Any] = []
    monkeypatch.setattr(
        notifications_module,
        "async_at_started",
        lambda hass_arg, callback: start_callbacks.append(callback) or (lambda: None),
    )

    asyncio.run(coordinator._async_notify_ai_advice("Startup result."))

    assert coordinator.hass.services.calls == []
    coordinator.hass.state = CoreState.running
    asyncio.run(start_callbacks[0](coordinator.hass))
    assert coordinator.hass.services.calls[0][2]["message"] == "Startup result."


def test_manual_ai_advice_button_schedules_fresh_current_plan(monkeypatch: object) -> None:
    @dataclass
    class CurrentContext:
        created_at: datetime

    now = datetime(2026, 8, 8, tzinfo=UTC)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry(
        {CONF_AI_TASK_ENTITY: "ai_task.local"},
        {CONF_AI_ENABLED: False},
    )
    coordinator.hass = FakeHass()
    coordinator.store = FakeStore({"ai_recommendations": []})
    coordinator.data = _plan("manual-ai")
    coordinator._last_decision_context = CurrentContext(created_at=coordinator.data.created_at)
    coordinator._ai_advice_task = None
    coordinator._ai_advice_pending_fingerprint = None
    coordinator._ai_advice_pending_reason = None
    coordinator.async_update_listeners = lambda: None
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)

    asyncio.run(coordinator.async_request_ai_advice())

    assert len(coordinator.hass.created_tasks) == 1
    assert coordinator._ai_advice_pending_reason == "request_in_flight"
    assert coordinator._ai_current_plan_safe is True
    assert coordinator.hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {
                "title": "Energy Planner: explanation",
                "message": "Preparing an explanation for the current plan…",
                "notification_id": "ha_energy_planner_ai_explanation_entry-1",
            },
            False,
        )
    ]

    coordinator._ai_advice_task = SimpleNamespace(done=lambda: False)
    asyncio.run(coordinator.async_request_ai_advice())
    assert len(coordinator.hass.created_tasks) == 1
    assert coordinator.hass.services.calls[-1][2]["message"] == "An explanation is already being prepared."


def test_manual_ai_advice_button_rejects_missing_config_unsafe_plan_and_rate_limit(
    monkeypatch: object,
) -> None:
    @dataclass
    class CurrentContext:
        created_at: datetime

    now = datetime(2026, 8, 8, tzinfo=UTC)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = FakeEntry({}, {})
    coordinator.hass = FakeHass()
    coordinator.store = FakeStore({"ai_recommendations": []})
    coordinator.data = _plan("manual-ai-errors")
    coordinator._last_decision_context = CurrentContext(created_at=now)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)

    with pytest.raises(HomeAssistantError, match="no AI Task entity"):
        asyncio.run(coordinator.async_request_ai_advice())

    coordinator.entry.data[CONF_AI_TASK_ENTITY] = "ai_task.local"
    coordinator.data = None
    with pytest.raises(HomeAssistantError, match="no current plan"):
        asyncio.run(coordinator.async_request_ai_advice())

    coordinator.data = _plan("manual-ai-errors")
    coordinator.data.health = InputHealth.UNSAFE
    with pytest.raises(HomeAssistantError, match="current plan is unsafe"):
        asyncio.run(coordinator.async_request_ai_advice())

    coordinator.data.health = InputHealth.HEALTHY
    coordinator.store.data["ai_recommendations"] = [
        {"created_at": now.isoformat(), "service_called": "ai_task.generate_data"}
    ]
    with pytest.raises(HomeAssistantError, match="rate limited"):
        asyncio.run(coordinator.async_request_ai_advice())

    messages = [call[2]["message"] for call in coordinator.hass.services.calls]
    assert messages == [
        "**Explanation unavailable.**\n\nNo AI Task entity is configured.",
        "**Explanation unavailable.**\n\nNo current plan is available.",
        "**Explanation unavailable.**\n\nThe current plan is unsafe or has zero confidence.",
        "**Explanation unavailable.**\n\nTry again in 300 seconds.",
    ]


def test_ai_advice_runs_after_rate_limit_window(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    calls = 0

    class FakeAIAdvisor:
        def __init__(self, hass: object, entry_data: dict[str, object], options: dict[str, object]) -> None:
            pass

        async def async_get_advice(self, context: object, plan: EnergyPlan) -> AIAdviceResult:
            nonlocal calls
            calls += 1
            return AIAdviceResult(
                status="accepted",
                accepted={"confidence": 0.8},
                rejected_reason=None,
                rejected_detail={},
                service_called="ai_task.generate_data",
            )

    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.LocalAIAdvisor", FakeAIAdvisor)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.store = FakeStore(
        {
            "ai_recommendations": [
                {
                    "created_at": (now - timedelta(seconds=301)).isoformat(),
                    "status": "accepted",
                    "service_called": "ai_task.generate_data",
                }
            ]
        }
    )
    context = SimpleNamespace(created_at=now)

    result, should_store = asyncio.run(coordinator._async_get_throttled_ai_advice(context, _plan("plan-1"), {}, {}))

    assert calls == 1
    assert should_store is True
    assert result.status == "accepted"


def test_manual_ai_advice_forces_refresh_for_unchanged_plan(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    plan = _plan("force-ai")
    fingerprint = _material_plan_fingerprint(plan)
    calls = 0

    class FakeAIAdvisor:
        def __init__(self, hass: object, entry_data: dict[str, object], options: dict[str, object]) -> None:
            pass

        async def async_get_advice(self, context: object, built_plan: EnergyPlan) -> AIAdviceResult:
            nonlocal calls
            calls += 1
            return AIAdviceResult(
                "accepted",
                {"outcome": "no_action_needed", "summary": "The current plan needs no changes."},
                None,
                "ai_task.generate_data",
            )

    monkeypatch.setattr(coordinator_module, "LocalAIAdvisor", FakeAIAdvisor)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.store = FakeStore(
        {
            "ai_recommendations": [
                {
                    "created_at": (now - timedelta(minutes=6)).isoformat(),
                    "status": "accepted",
                    "plan_fingerprint": fingerprint,
                    "service_called": "ai_task.generate_data",
                }
            ]
        }
    )

    reused, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=now), plan, {}, {})
    )
    refreshed, forced_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(
            SimpleNamespace(created_at=now),
            plan,
            {},
            {},
            force_current_plan=True,
        )
    )

    assert reused.rejected_reason == "ai_plan_unchanged"
    assert should_store is False
    assert refreshed.status == "accepted"
    assert forced_store is True
    assert calls == 1

    coordinator._ai_advice_fingerprint = fingerprint
    coordinator._ai_current_plan_fingerprint = fingerprint
    coordinator._ai_current_plan_safe = True
    coordinator._ai_advice_pending_fingerprint = fingerprint
    coordinator._ai_advice_pending_reason = "request_in_flight"
    coordinator._planner_lock = asyncio.Lock()
    coordinator._last_phase_durations = {}
    coordinator.async_update_listeners = lambda: None
    asyncio.run(
        coordinator._async_run_ai_advice(
            SimpleNamespace(created_at=now),
            plan,
            {},
            {},
            fingerprint,
            force_current_plan=True,
        )
    )
    assert coordinator.store.ai_recommendations[-1]["status"] == "accepted"
    assert calls == 2
    assert coordinator.hass.services.calls[-1][2]["message"] == (
        "**No action needed.**\n\nThe current plan needs no changes."
    )


def test_ai_advice_skips_unsafe_or_zero_confidence_plan() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore({"ai_recommendations": []})
    plan = _plan("unsafe")
    plan.health = InputHealth.UNSAFE
    plan.confidence = 0

    result, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=plan.created_at), plan, {}, {})
    )

    assert should_store is False
    assert result.rejected_reason == "ai_skipped_unsafe_plan"

    plan.health = InputHealth.HEALTHY
    plan.status = "unsafe"
    result, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=plan.created_at), plan, {}, {})
    )
    assert should_store is False
    assert result.rejected_reason == "ai_skipped_unsafe_plan"


def test_plan_refresh_never_starts_ai_and_cancels_an_obsolete_manual_request() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    cancelled: list[bool] = []
    updates: list[str] = []
    current = _plan("current")
    coordinator._ai_advice_task = SimpleNamespace(
        done=lambda: False,
        cancel=lambda: cancelled.append(True),
    )
    coordinator._ai_advice_fingerprint = _material_plan_fingerprint(current)
    coordinator._ai_advice_pending_fingerprint = coordinator._ai_advice_fingerprint
    coordinator._ai_advice_pending_reason = "request_in_flight"
    coordinator.async_update_listeners = lambda: updates.append("updated")

    coordinator._sync_ai_request_to_plan(current)
    assert cancelled == []

    replacement = _plan("replacement")
    replacement.preview = [{"import_price": 0.42}]
    coordinator._sync_ai_request_to_plan(replacement)

    assert cancelled == [True]
    assert coordinator._ai_advice_pending_fingerprint is None
    assert coordinator._ai_current_plan_fingerprint == _material_plan_fingerprint(replacement)
    assert updates == ["updated"]

    coordinator._ai_advice_task = None
    unsafe = _plan("unsafe")
    unsafe.health = InputHealth.UNSAFE
    coordinator._sync_ai_request_to_plan(unsafe)
    assert coordinator._ai_current_plan_safe is False
    assert coordinator._ai_current_plan_fingerprint is None


def test_background_ai_rechecks_committed_plan_under_planner_lock() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = FakeHass()
        coordinator.entry = FakeEntry({})
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._planner_lock = asyncio.Lock()
        coordinator._last_phase_durations = {}
        coordinator.async_update_listeners = lambda: None
        plan = _plan("safe-race")
        fingerprint = _material_plan_fingerprint(plan)
        coordinator._ai_advice_fingerprint = fingerprint
        coordinator._ai_current_plan_fingerprint = fingerprint
        coordinator._ai_current_plan_safe = True

        async def accepted(*args: object, **kwargs: object) -> tuple[AIAdviceResult, bool]:
            return AIAdviceResult("accepted", {"confidence": 0.8}, None, "ai_task.generate_data"), True

        coordinator._async_get_throttled_ai_advice = accepted
        async with coordinator._planner_lock:
            task = asyncio.create_task(
                coordinator._async_run_ai_advice(
                    SimpleNamespace(created_at=plan.created_at),
                    plan,
                    {},
                    {"ai_enabled": True},
                    fingerprint,
                    force_current_plan=True,
                )
            )
            await asyncio.sleep(0)
            coordinator._ai_current_plan_safe = False
            coordinator._ai_current_plan_fingerprint = None
        await task

        assert coordinator.store.ai_recommendations == []
        assert "plan changed" in coordinator.hass.services.calls[-1][2]["message"].lower()

    asyncio.run(scenario())


def test_cancelled_manual_ai_request_clears_pending_and_task_reference() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = FakeHass()
        coordinator.entry = FakeEntry({})
        coordinator._ai_advice_pending_fingerprint = "fingerprint"
        coordinator._ai_advice_pending_reason = "request_in_flight"
        coordinator._last_phase_durations = {}
        coordinator.async_update_listeners = lambda: None

        async def cancelled(*args: object, **kwargs: object) -> tuple[AIAdviceResult, bool]:
            raise asyncio.CancelledError

        coordinator._async_get_throttled_ai_advice = cancelled
        task = asyncio.create_task(
            coordinator._async_run_ai_advice(
                SimpleNamespace(created_at=datetime(2026, 6, 27, tzinfo=UTC)),
                _plan("cancelled"),
                {},
                {},
                "fingerprint",
                force_current_plan=True,
            )
        )
        coordinator._ai_advice_task = task
        with pytest.raises(asyncio.CancelledError):
            await task

        assert coordinator._ai_advice_pending_fingerprint is None
        assert coordinator._ai_advice_task is None
        assert "plan changed" in coordinator.hass.services.calls[-1][2]["message"].lower()

    asyncio.run(scenario())


def test_planner_owned_control_feedback_uses_grace_evidence() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    daikin_event = FakeEvent("climate.daikin", "off", "heat", new_attributes={"temperature": 21})
    enphase_event = FakeEvent("select.enphase", "AI Optimisation", "Full Backup")
    entry_data = {
        "daikin_climate_entity": "climate.daikin",
        "climate_zone_entities": ["switch.zone", "climate.zone_temperature"],
        "enphase_profile_entity": "select.enphase",
    }

    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.zone_temperature",
            "off",
            "heat",
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={
            "enable_zones": True,
            "target_temperature": 21,
            "configured_zones_only": True,
        },
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {
                        "enable_zones": True,
                        "target_temperature": 21,
                        "configured_zones_only": True,
                    },
                }
            ]
        },
        FakeEvent(
            "climate.zone_temperature",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {
                        "enable_zones": True,
                        "target_temperature": 21,
                        "configured_zones_only": True,
                    },
                }
            ]
        },
        FakeEvent(
            "climate.zone_temperature",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
    )
    unsynchronized_zone_event = FakeEvent(
        "climate.zone_temperature",
        "heat",
        "heat",
        old_attributes={"temperature": 20},
        new_attributes={"temperature": 21},
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        unsynchronized_zone_event,
        now,
        pending_hvac_desired_state={
            "enable_zones": True,
            "target_temperature": 21,
            "configured_zones_only": False,
        },
    )
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        unsynchronized_zone_event,
        {
            "enable_zones": True,
            "target_temperature": 21,
            "configured_zones_only": False,
        },
    ) == "climate.zone_temperature"
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {
                        "enable_zones": True,
                        "target_temperature": 21,
                        "configured_zones_only": False,
                    },
                }
            ]
        },
        FakeEvent(
            "climate.zone_temperature",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
    )

    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent("switch.zone", "on", "off"),
        now,
        pending_hvac_desired_state={"restore_zones": {"switch.zone": "off"}},
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.zone_temperature",
            "heat",
            "heat",
            old_attributes={"temperature": 23},
            new_attributes={"temperature": 20},
        ),
        now,
        pending_hvac_desired_state={
            "restore_zones": {
                "climate.zone_temperature": {"target_temperature": 20},
            }
        },
    )
    mismatched_zone_event = FakeEvent(
        "climate.zone_temperature",
        "heat",
        "heat",
        old_attributes={"temperature": 20},
        new_attributes={"temperature": 24},
    )
    pending_zone_restore = {
        "restore_zones": {
            "climate.zone_temperature": {"target_temperature": 20},
        }
    }
    assert not _matches_pending_zone_hvac_feedback(
        {"target_temperature": 20},
        mismatched_zone_event,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        mismatched_zone_event,
        now,
        pending_hvac_desired_state=pending_zone_restore,
    )
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        mismatched_zone_event,
        pending_zone_restore,
    ) == "climate.zone_temperature"
    assert not _matches_pending_zone_hvac_feedback(
        {"target_temperature": 20},
        FakeEvent(
            "climate.zone_temperature",
            "heat",
            "heat",
            old_attributes={"temperature": 20, "fan_mode": "auto"},
            new_attributes={"temperature": 20, "fan_mode": "high"},
        ),
    )
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        SimpleNamespace(
            data={
                "entity_id": "switch.zone",
                "old_state": None,
                "new_state": SimpleNamespace(state="on", attributes={}),
            }
        ),
        {"enable_zones": True},
    ) is None
    unchanged_switch_event = FakeEvent("switch.zone", "on", "on")
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        unchanged_switch_event,
        {"enable_zones": True},
    ) is None
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        FakeEvent("switch.zone", "on", "off"),
        {"restore_zones": {"switch.zone": "off"}},
    ) is None
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent("switch.zone", "off", "on"),
        now,
        pending_hvac_desired_state={"enable_zones": True},
    )
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        FakeEvent("switch.zone", "off", "on"),
        {"enable_zones": False},
    ) == "switch.zone"
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        FakeEvent("switch.zone", "on", "off"),
        {"enable_zones": True},
    ) == "switch.zone"
    assert _pending_zone_hvac_manual_change_entity_id(
        entry_data,
        FakeEvent("switch.zone", "off", "on"),
        {"enable_zones": True},
    ) is None
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {"enable_zones": True},
                }
            ]
        },
        FakeEvent("switch.zone", "off", "on"),
        now,
    )

    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now - timedelta(seconds=30),
                    "desired_state": {"hvac_mode": "heat", "target_temperature": 21},
                }
            ]
        },
        daikin_event,
        now,
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {
                        "hvac_mode": "heat",
                        "target_temperature": 21,
                        "configured_zones_only": True,
                    },
                }
            ]
        },
        FakeEvent(
            "climate.daikin",
            "off",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {
                        "target_temperature": 21,
                        "configured_zones_only": True,
                    },
                }
            ]
        },
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={
            "target_temperature": 21,
            "configured_zones_only": True,
        },
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "off",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={
            "hvac_mode": "heat",
            "target_temperature": 21,
            "configured_zones_only": True,
        },
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={"target_temperature": 21},
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 21},
            new_attributes={"temperature": 24},
        ),
        now,
        pending_hvac_desired_state={"target_temperature": 21},
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 23},
            new_attributes={"temperature": 20},
        ),
        now,
        pending_hvac_desired_state={
            "restore_main": {
                "hvac_mode": "off",
                "target_temperature": 20,
            }
        },
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "off",
            "cool",
            new_attributes={"temperature": 20},
        ),
        now,
        pending_hvac_desired_state={
            "hvac_mode": "heat",
            "target_temperature": 21,
            "turn_on_feedback_expected": True,
        },
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "off",
            "cool",
            new_attributes={"temperature": 20},
        ),
        now,
        pending_hvac_desired_state={
            "hvac_mode": "heat",
            "target_temperature": 21,
        },
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "cool",
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={
            "hvac_mode": "heat",
            "target_temperature": 21,
        },
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "off",
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={
            "hvac_mode": "heat",
            "target_temperature": 21,
        },
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "cool",
            "heat",
            new_attributes={"temperature": 21},
        ),
        now,
        pending_hvac_desired_state={
            "hvac_mode": "heat",
            "target_temperature": 21,
        },
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        FakeEvent(
            "climate.daikin",
            "heat",
            "cool",
            new_attributes={"temperature": 20},
        ),
        now,
        pending_hvac_desired_state={
            "restore_main": {
                "hvac_mode": "off",
                "target_temperature": 20,
                "rollback_active_hvac_mode": "cool",
            }
        },
    )

    assert _matches_pending_main_hvac_feedback(
        {"target_temp_low": 20, "target_temp_high": 25},
        FakeEvent(
            "climate.daikin",
            "heat_cool",
            "heat_cool",
            old_attributes={"target_temp_low": 19, "target_temp_high": 24},
            new_attributes={"target_temp_low": 20.0, "target_temp_high": 25.0},
        ),
    )
    assert not _matches_pending_main_hvac_feedback(
        {"target_temp_low": 20, "target_temp_high": 25},
        FakeEvent(
            "climate.daikin",
            "heat_cool",
            "heat_cool",
            old_attributes={"target_temp_low": 19, "target_temp_high": 24},
            new_attributes={"target_temp_low": 20, "target_temp_high": 26},
        ),
    )
    assert not _matches_pending_main_hvac_feedback(
        {"target_temperature": 21},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"fan_mode": "auto"},
            new_attributes={"fan_mode": "high"},
        ),
    )
    missing_old_state = FakeEvent("climate.daikin", "heat", "heat")
    missing_old_state.data["old_state"] = None
    assert not _is_pending_main_hvac_manual_change(
        entry_data,
        missing_old_state,
        {"target_temperature": 21},
    )
    missing_new_state = FakeEvent("climate.daikin", "heat", "heat")
    missing_new_state.data["new_state"] = None
    assert not _matches_pending_main_hvac_feedback(
        {"target_temperature": 21},
        missing_new_state,
    )
    assert not _matches_pending_main_hvac_feedback(
        {"target_temperature": 21},
        FakeEvent("climate.daikin", "heat", "heat"),
    )
    assert not _matches_pending_main_hvac_feedback(
        {},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
    )
    assert not _matches_pending_main_hvac_feedback(
        {"target_temperature": 21},
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": "invalid"},
        ),
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {"target_temperature": 21},
                }
            ]
        },
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 21},
        ),
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {"hvac_mode": "heat"},
                }
            ]
        },
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20},
            new_attributes={"temperature": 22},
        ),
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": {"target_temperature": 21},
                }
            ]
        },
        FakeEvent(
            "climate.daikin",
            "heat",
            "heat",
            old_attributes={"temperature": 20, "fan_mode": "auto"},
            new_attributes={"temperature": 21, "fan_mode": "quiet"},
        ),
        now,
    )
    assert _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "enphase",
                    "attempted_at": now - timedelta(seconds=30),
                    "desired_state": {"profile": "Full Backup"},
                }
            ]
        },
        enphase_event,
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "failed",
                    "asset": "enphase",
                    "attempted_at": now - timedelta(seconds=10),
                    "desired_state": {"profile": "Full Backup"},
                }
            ],
            "command_rate_limits": {"enphase:set_profile": now},
        },
        enphase_event,
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {"execution_audit": []},
        SimpleNamespace(data={"entity_id": "climate.daikin", "new_state": None}),
        now,
    )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now - timedelta(minutes=5),
                    "desired_state": {"hvac_mode": "heat"},
                },
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now,
                    "desired_state": "bad",
                },
            ]
        },
        daikin_event,
        now,
    )
    for observed_temperature in (22, "bad"):
        assert not _is_planner_owned_control_feedback(
            entry_data,
            {
                "execution_audit": [
                    {
                        "result": "applied",
                        "asset": "daikin",
                        "attempted_at": now,
                        "desired_state": {"hvac_mode": "heat", "target_temperature": 21},
                    }
                ]
            },
            FakeEvent(
                "climate.daikin",
                "off",
                "heat",
                new_attributes={"temperature": observed_temperature},
            ),
            now,
        )
    assert not _is_planner_owned_control_feedback(
        entry_data,
        {
            "execution_audit": [
                {
                    "result": "applied",
                    "asset": "daikin",
                    "attempted_at": now - timedelta(seconds=10),
                    "desired_state": {"hvac_mode": "cool"},
                }
            ]
        },
        daikin_event,
        now,
    )


def test_manual_ai_cached_skip_and_failure_are_bounded() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = FakeHass()
        coordinator.entry = FakeEntry({})
        coordinator._ai_advice_task = None
        coordinator._ai_advice_fingerprint = None
        coordinator._last_phase_durations = {}
        coordinator._planner_lock = asyncio.Lock()
        coordinator.async_update_listeners = lambda: None
        plan = _plan("cached")
        fingerprint = _material_plan_fingerprint(plan)
        coordinator.store = FakeStore(
            {"ai_recommendations": [{"status": "accepted", "rejected_detail": {"plan_fingerprint": fingerprint}}]}
        )
        context = SimpleNamespace(created_at=plan.created_at)

        async def skipped(*args: object) -> tuple[AIAdviceResult, bool]:
            return AIAdviceResult("skipped", {}, "rate_limited", None), False

        coordinator._async_get_throttled_ai_advice = skipped
        coordinator._ai_advice_fingerprint = fingerprint
        await coordinator._async_run_ai_advice(context, plan, {}, {"ai_enabled": True}, fingerprint)
        assert coordinator.store.ai_recommendations == []

        async def failed(*args: object, **kwargs: object) -> tuple[AIAdviceResult, bool]:
            raise RuntimeError("provider failed")

        coordinator._async_get_throttled_ai_advice = failed
        await coordinator._async_run_ai_advice(
            context,
            plan,
            {},
            {"ai_enabled": True},
            fingerprint,
            force_current_plan=True,
        )
        assert coordinator.store.ai_recommendations == []
        assert "could not be completed" in coordinator.hass.services.calls[-1][2]["message"]

    asyncio.run(scenario())


def test_rate_limited_manual_ai_clears_pending_without_automatic_retry() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = FakeHass()
        coordinator.entry = FakeEntry({})
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._planner_lock = asyncio.Lock()
        coordinator._last_phase_durations = {}
        coordinator._ai_advice_task = None
        coordinator._ai_advice_pending_fingerprint = None
        coordinator._ai_advice_pending_reason = None
        updates: list[str] = []
        coordinator.async_update_listeners = lambda: updates.append("updated")
        plan = _plan("rate-limited")
        context = SimpleNamespace(created_at=plan.created_at)
        fingerprint = _material_plan_fingerprint(plan)
        coordinator.data = plan
        coordinator._last_decision_context = context
        coordinator._ai_advice_fingerprint = fingerprint
        coordinator._ai_current_plan_fingerprint = fingerprint
        coordinator._ai_current_plan_safe = True

        async def rate_limited(*args: object, **kwargs: object) -> tuple[AIAdviceResult, bool]:
            return (
                AIAdviceResult(
                    "skipped",
                    {},
                    "ai_rate_limited",
                    None,
                    {"retry_after_seconds": 42},
                ),
                False,
            )

        coordinator._async_get_throttled_ai_advice = rate_limited
        await coordinator._async_run_ai_advice(
            context,
            plan,
            {},
            {},
            fingerprint,
            force_current_plan=True,
        )

        assert coordinator._ai_advice_pending_fingerprint is None
        assert coordinator._ai_advice_pending_reason is None
        assert updates == ["updated"]
        assert "No explanation available" in coordinator.hass.services.calls[-1][2]["message"]

    asyncio.run(scenario())


def test_changed_plan_cancels_stale_manual_ai_request() -> None:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    cancelled: list[bool] = []
    coordinator._ai_advice_pending_fingerprint = "old"
    coordinator._ai_advice_pending_reason = "request_in_flight"
    coordinator._ai_advice_task = SimpleNamespace(done=lambda: False, cancel=lambda: cancelled.append(True))
    coordinator._ai_advice_fingerprint = "old"
    coordinator.async_update_listeners = lambda: None
    plan = _plan("changed")

    coordinator._sync_ai_request_to_plan(plan)

    assert cancelled == [True]
    assert coordinator._ai_advice_pending_fingerprint is None


def test_background_ai_discards_result_for_replaced_fingerprint() -> None:
    async def scenario() -> None:
        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator.hass = FakeHass()
        coordinator.entry = FakeEntry({})
        coordinator.store = FakeStore({"ai_recommendations": []})
        coordinator._planner_lock = asyncio.Lock()
        coordinator._last_phase_durations = {}
        coordinator._ai_advice_task = None
        coordinator.async_update_listeners = lambda: None
        plan = _plan("replaced")
        fingerprint = _material_plan_fingerprint(plan)
        coordinator._ai_advice_fingerprint = "newer"
        coordinator._ai_current_plan_fingerprint = fingerprint
        coordinator._ai_current_plan_safe = True
        coordinator._ai_advice_pending_fingerprint = fingerprint
        coordinator._ai_advice_pending_reason = "request_in_flight"

        async def accepted(*args: object, **kwargs: object) -> tuple[AIAdviceResult, bool]:
            return AIAdviceResult("accepted", {"confidence": 0.8}, None, "ai_task.generate_data"), True

        coordinator._async_get_throttled_ai_advice = accepted
        await coordinator._async_run_ai_advice(
            SimpleNamespace(created_at=plan.created_at),
            plan,
            {},
            {"ai_enabled": True},
            fingerprint,
            force_current_plan=True,
        )

        assert coordinator.store.ai_recommendations == []
        assert coordinator._ai_advice_pending_fingerprint is None
        assert "plan changed" in coordinator.hass.services.calls[-1][2]["message"].lower()

    asyncio.run(scenario())


def test_material_ai_fingerprint_changes_with_forecast_preview_and_cost() -> None:
    first = _plan("generated-1")
    first.preview = [{"import_price": 0.1, "pv_forecast_kw": 1.0}]
    first.estimated_daily_cost = 2.5
    second = _plan("generated-2")
    second.preview = [{"import_price": 0.2, "pv_forecast_kw": 1.0}]
    second.estimated_daily_cost = 2.5

    assert _material_plan_fingerprint(first) != _material_plan_fingerprint(second)
    second.preview = [{"valid_at": "2026-06-27T00:05:00+00:00", "import_price": 0.1, "pv_forecast_kw": 1.0}]
    first.preview = [{"valid_at": "2026-06-27T00:00:00+00:00", "import_price": 0.1, "pv_forecast_kw": 1.0}]
    assert _material_plan_fingerprint(first) == _material_plan_fingerprint(second)


def test_ai_advice_reuses_unchanged_material_plan() -> None:
    plan = _plan("new-generated-id")
    fingerprint = _material_plan_fingerprint(plan)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore(
        {"ai_recommendations": [{"status": "accepted", "rejected_detail": {"plan_fingerprint": fingerprint}}]}
    )

    result, should_store = asyncio.run(
        coordinator._async_get_throttled_ai_advice(SimpleNamespace(created_at=plan.created_at), plan, {}, {})
    )

    assert should_store is False
    assert result.rejected_reason == "ai_plan_unchanged"


def test_ai_fingerprint_lookup_and_decision_fingerprint_edges() -> None:
    assert _latest_ai_plan_fingerprint("bad") is None
    assert _latest_ai_plan_fingerprint([{"ignored": True}, "bad"]) is None
    assert _latest_ai_plan_fingerprint(["bad", {"status": "accepted", "plan_fingerprint": "top-level"}]) == "top-level"
    assert (
        _latest_ai_plan_fingerprint([{"status": "accepted", "rejected_detail": {"plan_fingerprint": "nested"}}])
        == "nested"
    )
    hass = FakeHass({"sensor.price": "0.25"})
    present = _decision_input_fingerprint(
        hass,
        {"amber_import_price_entity": "sensor.price"},
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
        [],
        now=datetime(2026, 6, 27, tzinfo=UTC),
    )
    missing = _decision_input_fingerprint(
        FakeHass(),
        {"amber_import_price_entity": "sensor.price"},
        {CONF_PLANNING_INTERVAL_MINUTES: 5},
        [],
        now=datetime(2026, 6, 27, tzinfo=UTC),
    )
    assert present != missing


def test_unchanged_decision_fingerprint_short_circuits_refresh_pipeline() -> None:
    coordinator = _coordinator_for_runtime_services(entry_data={"amber_import_price_entity": "sensor.price"})
    coordinator.data = _plan("existing")
    coordinator._last_decision_fingerprint = _decision_input_fingerprint(
        coordinator.hass,
        coordinator.entry_data,
        coordinator.planner_options,
        coordinator.overrides,
        now=coordinator.data.created_at,
    )
    # Keep the interval bucket stable for this focused short-circuit test.
    original = coordinator_module.dt_util.utcnow
    coordinator_module.dt_util.utcnow = lambda: coordinator.data.created_at
    try:
        result = asyncio.run(coordinator._async_update_data_locked())
    finally:
        coordinator_module.dt_util.utcnow = original

    assert result is coordinator.data
    assert coordinator._refresh_counters["fingerprint_skipped"] == 1


def test_explicit_replan_marks_next_refresh_as_forced() -> None:
    coordinator = _coordinator_for_runtime_services()

    asyncio.run(coordinator.async_request_replan())

    assert coordinator._force_next_refresh is True
    assert coordinator.refresh_requested == 1
    assert coordinator._pending_refresh_trigger == "manual_replan"


def test_latest_ai_service_call_and_state_helpers_cover_edge_cases() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert _latest_ai_service_call_at("bad") is None
    assert _latest_ai_service_call_at([{"created_at": now, "service_called": "ai_task.generate_data"}]) == now
    assert _latest_ai_service_call_at([{"created_at": "bad", "service_called": "ai_task.generate_data"}]) is None
    assert _split_entity_values([" sensor.a ", "bad", "binary_sensor.b"]) == ["sensor.a", "binary_sensor.b"]
    assert _split_entity_values(123) == []
    assert _bool_state_value(FakeHass(), None) is None
    assert _bool_state_value(FakeHass(), "binary_sensor.missing") is None
    assert _bool_state_value(FakeHass({"binary_sensor.yes": "on"}), "binary_sensor.yes") is True
    assert _bool_state_value(FakeHass({"binary_sensor.no": "off"}), "binary_sensor.no") is False
    assert _bool_state_value(FakeHass({"binary_sensor.unknown": "maybe"}), "binary_sensor.unknown") is None
    assert _float_state_value(FakeHass(), None) is None
    assert _float_state_value(FakeHass(), "sensor.missing") is None
    assert _float_state_value(FakeHass({"sensor.bad": "bad"}), "sensor.bad") is None
    assert _float_state_value(FakeHass({"sensor.nan": "nan"}), "sensor.nan") is None
    assert _float_state_value(FakeHass({"sensor.value": "12.5"}), "sensor.value") == 12.5
    assert _parse_datetime_or_none(now) is now
    assert _parse_datetime_or_none(None) is None
    assert _parse_datetime_or_none(123) is None


def test_expired_manual_hvac_state_handles_malformed_and_active_overrides() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert _expired_manual_hvac_state({"overrides": "bad"}, now) is False
    assert (
        _expired_manual_hvac_state(
            {
                "overrides": [
                    "bad",
                    {"kind": "manual_ev_charging"},
                    {
                        "kind": "manual_hvac",
                        "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    },
                ]
            },
            now,
        )
        is False
    )
    assert (
        _expired_manual_hvac_state(
            {"overrides": [{"kind": "manual_hvac"}]},
            now,
        )
        is True
    )
    assert (
        _expired_manual_hvac_state(
            {
                "overrides": [
                    {"kind": "manual_hvac", "source": "helper", "expires_at": None},
                    {"kind": "manual_hvac", "source": "service", "expires_at": None},
                ]
            },
            now,
        )
        is True
    )


def test_update_data_locked_records_dry_run_comparison(monkeypatch: object) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    context = SimpleNamespace(
        created_at=now,
        plan_id="plan-dry",
        slots=[DecisionSlot(now, 0.2, 0.05, 0, 1)],
        input_health=InputHealth.HEALTHY,
        input_issues=[],
        occupancy_state=OccupancyState.OCCUPIED,
    )

    class FakePlanner:
        mode = PlannerMode.DRY_RUN

        def __init__(
            self,
            options: dict[str, object],
            thermal_model: dict[str, object],
            ev_charge_calibration: dict[str, object],
            ev_charging_entity_id: str | None,
            ev_soc_entity_id: str | None,
        ) -> None:
            pass

        def create_plan(self, built_context: object) -> EnergyPlan:
            plan = _plan("plan-dry")
            plan.created_at = now
            plan.mode = self.mode
            return plan

    async def fake_update_ev_charge_calibration(
        hass: object,
        data: dict[str, object],
        model: dict[str, object],
        *,
        charge_rate_kw: float,
        now: datetime,
    ) -> tuple[dict[str, object], bool, str]:
        return {"status": "ready", "soc_per_kwh": 1.8}, True, "trained"

    async def fake_update_load_forecast(
        *args: object,
        **kwargs: object,
    ) -> tuple[dict[str, object], bool, str]:
        return {"status": "ready"}, True, "trained"

    class FakeConstraintValidator:
        violations: list[str] = []

        def __init__(self, options: dict[str, object]) -> None:
            pass

        def validate_plan(self, built_context: object, plan: EnergyPlan) -> list[str]:
            return list(self.violations)

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.CapabilityDiscovery",
        lambda hass, data: SimpleNamespace(
            inspect=lambda: SimpleNamespace(
                as_dict=lambda: {},
                climate=SimpleNamespace(issues=["climate_zone_unavailable"]),
            )
        ),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.InputManager",
        lambda *args, **kwargs: SimpleNamespace(
            current_forecast_observations=lambda: {},
            build_context=lambda overrides: context,
            thermal_sample=lambda built_context: {},
            forecast_training_slots=[],
            forecast_calibration={},
        ),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_update_ev_charge_calibration",
        fake_update_ev_charge_calibration,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_update_builtin_load_forecast",
        fake_update_load_forecast,
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_forecast_calibration",
        lambda *args, **kwargs: ({"enabled": True}, True),
    )
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.update_thermal_model",
        lambda *args, **kwargs: ({"enabled": True}, True),
    )
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.DryRunPlanner", FakePlanner)
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.ConstraintValidator",
        FakeConstraintValidator,
    )

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = FakeEntry({}, {"ai_enabled": False})
    coordinator.store = FakeStore(
        {
            "ev_charge_calibration": {},
            "forecast_snapshots": [],
            "overrides": [{"kind": "other"}],
            "ownership": {"hvac_control": {"phase": "preconditioning"}},
        }
    )
    coordinator.executor = FakeExecutor()
    coordinator.overrides = [
        Override(
            kind="manual_hvac",
            source="service",
            expires_at=now - timedelta(minutes=1),
            reason="expired",
        )
    ]
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0

    result = asyncio.run(coordinator._async_update_data_locked())

    assert result.mode == PlannerMode.DRY_RUN
    assert coordinator.store.dry_run_comparisons[0]["plan_id"] == "plan-dry"
    assert coordinator.store.ev_charge_calibrations
    assert coordinator.store.load_forecasts
    assert coordinator.store.forecast_calibrations
    assert coordinator.store.thermal_models
    assert context.hvac_control["required_evidence_lost"] == "climate_zone_unavailable"

    FakePlanner.mode = PlannerMode.ACTIVE_HEALTHY
    FakeConstraintValidator.violations = ["input_health_unsafe"]
    context.plan_id = "plan-active"
    coordinator._force_next_refresh = True

    active_result = asyncio.run(coordinator._async_update_data_locked())

    assert active_result.status == "unsafe"
    assert active_result.mode == PlannerMode.ACTIVE_DEGRADED


def test_snapshot_actions_are_bounded_and_auditable() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = PlanAction(
        action_id="plan-1-ev-minimum-soc",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_SCHEDULE,
        desired_state={
            "target_soc_percent": 80,
            "ready_by": "07:00",
            "allocated_slots": [
                {
                    "valid_at": (now + timedelta(minutes=5 * index)).isoformat(),
                    "charge_kw": 7.0,
                }
                for index in range(20)
            ],
        },
        hard_constraints=["ready_by"],
        reason_codes=["ev_soc_below_target", "fallback_target"],
        expected_cost_delta=None,
        confidence=0.93,
    )
    plan = _plan("plan-1")
    plan.actions = [action]

    snapshot = _snapshot_actions(plan)

    assert snapshot == [
        {
            "action_id": "plan-1-ev-minimum-soc",
            "asset": "ev",
            "kind": "ev_schedule",
            "execute_not_before": "2026-06-27T00:00:00+00:00",
            "execute_not_after": "2026-06-27T00:05:00+00:00",
            "desired_state": {
                "target_soc_percent": 80,
                "ready_by": "07:00",
                "allocated_slots": [
                    {
                        "valid_at": "2026-06-27T00:00:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:05:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:10:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:15:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:20:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:25:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:30:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:35:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:40:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:45:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:50:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {
                        "valid_at": "2026-06-27T00:55:00+00:00",
                        "charge_kw": 7.0,
                    },
                    {"truncated_count": 8},
                ],
            },
            "hard_constraints": ["ready_by"],
            "reason_codes": ["ev_soc_below_target", "fallback_target"],
            "expected_cost_delta": None,
            "confidence": 0.93,
        }
    ]

    context = DecisionContext(
        created_at=now,
        plan_id=plan.plan_id,
        slots=[
            DecisionSlot(
                valid_at=now,
                import_price=0.2,
                export_price=0.05,
                pv_forecast_kw=1.0,
                baseline_load_forecast_kw=1.2,
                baseline_load_forecast_upper_kw=1.6,
            )
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=50,
        occupancy_state=OccupancyState.OCCUPIED,
        input_health=InputHealth.HEALTHY,
    )
    assert _snapshot_action_load_forecasts(plan, context) == [
        {
            "action_id": action.action_id,
            "valid_at": now,
            "expected_kw": 1.2,
            "conservative_kw": 1.6,
        }
    ]
    context.slots.append(
        DecisionSlot(
            valid_at=now + timedelta(minutes=15),
            import_price=0.3,
            export_price=0.05,
            pv_forecast_kw=1.0,
            baseline_load_forecast_kw=2.4,
            baseline_load_forecast_upper_kw=2.8,
        )
    )
    plan.actions[0] = replace(
        action,
        execute_not_before=now + timedelta(minutes=10),
        execute_not_after=now + timedelta(minutes=14),
    )
    assert _snapshot_action_load_forecasts(plan, context)[0]["expected_kw"] == 1.2
    context.slots = []
    assert _snapshot_action_load_forecasts(plan, context) == []


def test_restore_safe_state_refreshes_by_default() -> None:
    coordinator = _coordinator_for_restore()

    asyncio.run(coordinator.async_restore_safe_state("manual_service_call"))

    assert coordinator.executor.restored == ["manual_service_call"]
    assert coordinator.refresh_requested == 1
    assert coordinator._refresh_generation == 1


def test_restore_safe_state_can_skip_refresh_for_teardown() -> None:
    coordinator = _coordinator_for_restore()

    asyncio.run(coordinator.async_restore_safe_state("entry_unload", refresh=False))

    assert coordinator.executor.restored == ["entry_unload"]
    assert coordinator.refresh_requested == 0
    assert coordinator._refresh_generation == 0


def test_manual_ev_and_restore_share_command_lock_without_blocking_refresh_lock() -> None:
    async def scenario() -> None:
        coordinator = _coordinator_for_runtime_services()
        lock_observations: list[tuple[str, bool, bool]] = []

        async def save_overrides(overrides: list[object]) -> None:
            lock_observations.append(
                (
                    "save_override",
                    coordinator._command_lock.locked(),
                    coordinator._planner_lock.locked(),
                )
            )

        async def request_refresh() -> None:
            lock_observations.append(
                (
                    "refresh",
                    coordinator._command_lock.locked(),
                    coordinator._planner_lock.locked(),
                )
            )

        coordinator.store.async_save_overrides = save_overrides
        coordinator.async_request_refresh = request_refresh

        await coordinator._command_lock.acquire()
        manual_task = asyncio.create_task(coordinator.async_manual_ev_charging(True))
        await asyncio.sleep(0)
        assert coordinator.executor.manual_ev_commands == []
        await asyncio.wait_for(coordinator._planner_lock.acquire(), timeout=0.1)
        coordinator._planner_lock.release()
        coordinator._command_lock.release()
        await manual_task

        await coordinator._command_lock.acquire()
        restore_task = asyncio.create_task(coordinator.async_restore_safe_state("serialized_restore"))
        await asyncio.sleep(0)
        assert coordinator.executor.restored == []
        await asyncio.wait_for(coordinator._planner_lock.acquire(), timeout=0.1)
        coordinator._planner_lock.release()
        coordinator._command_lock.release()
        await restore_task

        assert lock_observations == [
            ("save_override", True, False),
            ("refresh", False, False),
            ("refresh", False, False),
        ]

    asyncio.run(scenario())


def test_request_replan_and_ready_by_mark_generation_and_refresh() -> None:
    coordinator = _coordinator_for_runtime_services()

    asyncio.run(coordinator.async_request_replan())
    asyncio.run(coordinator.async_set_ready_by("09:15"))

    assert coordinator.ready_by == "09:15"
    assert coordinator.refresh_requested == 2
    assert coordinator._refresh_generation == 2
    assert coordinator._pending_refresh_trigger == "ready_by_changed"


def test_set_ready_by_updates_configured_ev_helper(monkeypatch: object) -> None:
    calls: list[tuple[object, dict[str, object], str]] = []

    class FakeEVAdapter:
        def __init__(self, hass: object, entry_data: dict[str, object]) -> None:
            self.hass = hass
            self.entry_data = entry_data

        async def async_set_ready_by(self, ready_by: str) -> None:
            calls.append((self.hass, self.entry_data, ready_by))

    monkeypatch.setattr(coordinator_module, "EVSmartChargingAdapter", FakeEVAdapter)
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_SMART_CHARGING_READY_BY: "input_datetime.ev_ready_by"}
    )

    asyncio.run(coordinator.async_set_ready_by("23:45"))

    assert coordinator.ready_by == "23:45"
    assert coordinator.refresh_requested == 1
    assert calls == [(coordinator.hass, coordinator.entry_data, "23:45")]


def test_native_ev_settings_persist_and_manual_control_replans() -> None:
    updates: list[dict[str, Any]] = []
    refreshes: list[str] = []

    class ConfigEntries:
        def async_update_entry(self, entry: object, *, options: dict[str, Any]) -> None:
            entry.options = options
            updates.append(options)

    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.entry = SimpleNamespace(entry_id="entry", data={"ev_charger_entity": "switch.ev"}, options={})
    coordinator.hass = SimpleNamespace(config_entries=ConfigEntries())
    coordinator.ready_by = "07:00"
    coordinator.overrides = []
    coordinator.store = SimpleNamespace(async_save_overrides=AsyncMock())
    coordinator.executor = FakeExecutor()
    coordinator._last_decision_context = SimpleNamespace(plan_id="latest-context")
    coordinator._refresh_generation = 0
    coordinator._force_next_refresh = False
    coordinator._planner_lock = asyncio.Lock()
    coordinator._command_lock = asyncio.Lock()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_handled_options = {}
    coordinator._last_control_mode_state = (coordinator.planner_enabled, coordinator.dry_run)

    async def request_refresh() -> None:
        refreshes.append("refresh")

    coordinator.async_request_refresh = request_refresh
    asyncio.run(coordinator.async_set_ready_by("08:10"))
    asyncio.run(coordinator.async_set_ev_low_price_threshold(0.07))
    asyncio.run(coordinator.async_manual_ev_charging(True))
    asyncio.run(coordinator.async_manual_ev_charging(False))

    assert coordinator.ready_by == "08:10"
    assert updates[0]["default_ready_by"] == "08:10"
    assert updates[1]["ev_low_price_threshold"] == 0.07
    assert [item[0] for item in coordinator.executor.manual_ev_commands] == [True, False]
    assert all(item[1] is coordinator._last_decision_context for item in coordinator.executor.manual_ev_commands)
    assert all("ev_connected_helper" not in item[2] for item in coordinator.executor.manual_ev_commands)
    assert coordinator.overrides[0].reason == "manual_stop"
    assert coordinator.store.async_save_overrides.await_count == 2
    assert refreshes == ["refresh"] * 4


def test_manual_hvac_override_replaces_existing_override_and_turns_on_helper() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )
    coordinator.overrides = [
        SimpleNamespace(kind="manual_hvac", reason="old"),
        SimpleNamespace(kind="other", reason="kept"),
    ]

    async def release_while_serialized(reason: str) -> None:
        assert coordinator._planner_lock.locked()
        assert coordinator._refresh_generation == 1
        coordinator.executor.hvac_releases.append(reason)

    coordinator.executor.async_release_hvac_control = release_while_serialized

    asyncio.run(coordinator.async_set_manual_hvac_override(15, "user_change"))

    assert [override.kind for override in coordinator.overrides] == ["other", "manual_hvac"]
    assert coordinator.overrides[-1].reason == "user_change"
    assert coordinator.store.data["overrides"] == coordinator.overrides
    assert "manual_hvac_override_expires_at" in coordinator.store.data["ownership"]
    assert coordinator.hass.services.calls == [
        (
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.manual_override"},
            True,
        )
    ]
    assert coordinator.executor.hvac_releases == ["user_change"]
    assert coordinator.refresh_requested == 1
    assert coordinator._refresh_generation == 1


def test_manual_hvac_override_releases_control_when_helper_service_fails() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )

    async def fail_helper(*args: object, **kwargs: object) -> None:
        raise RuntimeError("helper unavailable")

    coordinator.hass.services.async_call = fail_helper

    with pytest.raises(RuntimeError, match="helper unavailable"):
        asyncio.run(coordinator.async_set_manual_hvac_override(15, "user_change"))

    assert coordinator.overrides[-1].reason == "user_change"
    assert coordinator.executor.hvac_releases == ["user_change"]
    assert coordinator.refresh_requested == 1
    assert coordinator._manual_override_helper_guard is None


def test_manual_override_helper_uses_configured_timeout_and_can_be_cleared() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"},
        options={"manual_hvac_override_minutes": 45},
    )
    before = datetime.now(UTC)

    asyncio.run(coordinator._async_handle_manual_override_helper(True))

    assert coordinator.overrides[0].source == "helper"
    assert before + timedelta(minutes=45) <= coordinator.overrides[0].expires_at <= datetime.now(UTC) + timedelta(
        minutes=45
    )
    assert coordinator.executor.hvac_releases == ["manual_override_helper_on"]
    assert coordinator.store.data["ownership"]["manual_hvac_override_expires_at"] == (
        coordinator.overrides[0].expires_at
    )

    coordinator.store.data["ownership"]["ev_smart_charging_state"] = "off"
    original_save_overrides = coordinator.store.async_save_overrides
    original_save_ownership = coordinator.store.async_save_ownership

    async def save_overrides_while_serialized(overrides: list[Override]) -> None:
        assert coordinator._planner_lock.locked()
        await original_save_overrides(overrides)

    async def save_ownership_while_serialized(ownership: dict[str, object]) -> None:
        assert coordinator._planner_lock.locked()
        await original_save_ownership(ownership)

    coordinator.store.async_save_overrides = save_overrides_while_serialized
    coordinator.store.async_save_ownership = save_ownership_while_serialized
    asyncio.run(coordinator._async_handle_manual_override_helper(False))

    assert coordinator.overrides == []
    assert "manual_hvac_override_expires_at" not in coordinator.store.data["ownership"]
    assert coordinator.store.data["ownership"]["ev_smart_charging_state"] == "off"
    assert coordinator.refresh_requested == 2


def test_manual_override_helper_off_preserves_timed_override() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    coordinator.overrides = [
        Override(
            kind="manual_hvac",
            source="service",
            expires_at=expires_at,
            reason="timed_service_override",
        )
    ]
    coordinator.store.data["ownership"] = {
        "manual_hvac_override_expires_at": expires_at,
    }

    asyncio.run(coordinator._async_handle_manual_override_helper(False))

    assert coordinator.overrides[0].source == "service"
    assert coordinator.overrides[0].expires_at == expires_at
    assert coordinator.store.data["ownership"]["manual_hvac_override_expires_at"] == expires_at


def test_detected_manual_override_does_not_replace_authoritative_helper() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )
    helper_override = Override(
        kind="manual_hvac",
        source="helper",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        reason="manual_override_helper_on",
    )
    coordinator.overrides = [helper_override]

    asyncio.run(
        coordinator.async_set_manual_hvac_override(
            15,
            "daikin_state_changed",
        )
    )

    assert coordinator.overrides[0] == helper_override
    assert coordinator.overrides[1].source == "service"
    assert coordinator.overrides[1].expires_at is not None
    assert coordinator.store.data["ownership"]["manual_hvac_override_expires_at"] == helper_override.expires_at

    asyncio.run(coordinator._async_handle_manual_override_helper(False))

    assert len(coordinator.overrides) == 1
    assert coordinator.overrides[0].source == "service"
    assert coordinator.store.data["ownership"]["manual_hvac_override_expires_at"] == (
        coordinator.overrides[0].expires_at
    )


def test_hvac_control_from_ownership_normalizes_legacy_records() -> None:
    started_at = datetime.now(UTC) - timedelta(minutes=10)
    assert _hvac_control_from_ownership(
        {
            "climate_automations": {"automation.hvac": "on"},
            "planner_takeover_started_at": started_at,
        }
    ) == {"legacy_ownership": True}
    assert _hvac_control_from_ownership(
        {
            "hvac_control": {"phase": "peak_coast"},
            "hvac_release_hold_until": started_at,
        }
    ) == {"phase": "peak_coast", "released_until": started_at}
    assert _hvac_control_from_ownership(
        {
            "hvac_control": {"phase": "away_off", "started_at": "not-a-date"},
            "planner_takeover_started_at": started_at,
        }
    ) == {"phase": "away_off", "started_at": started_at}


def test_expired_manual_hvac_helper_cleanup_retries_after_service_failure() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )
    coordinator.store.data["ownership"] = {
        "manual_hvac_override_expires_at": "2000-01-01T00:00:00+00:00",
    }

    async def fail_service(*args: object, **kwargs: object) -> None:
        raise RuntimeError("helper unavailable")

    coordinator.hass.services.async_call = fail_service

    asyncio.run(coordinator._async_clear_expired_manual_hvac_state())

    assert "manual_hvac_override_expires_at" in coordinator.store.data["ownership"]
    assert coordinator._manual_override_helper_guard is None


def test_expired_manual_hvac_state_removes_persisted_expiry() -> None:
    coordinator = _coordinator_for_runtime_services()
    coordinator.store.data["ownership"] = {
        "manual_hvac_override_expires_at": "2000-01-01T00:00:00+00:00",
        "unrelated": True,
    }

    assert asyncio.run(coordinator._async_clear_expired_manual_hvac_state()) is True
    assert coordinator.store.data["ownership"] == {"unrelated": True}


def test_expired_manual_hvac_cleanup_preserves_ownership_added_during_helper_call() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_CLIMATE_MANUAL_OVERRIDE: "input_boolean.manual_override"}
    )
    coordinator.store.data["ownership"] = {
        "manual_hvac_override_expires_at": "2000-01-01T00:00:00+00:00",
    }

    async def helper_call(*args: object, **kwargs: object) -> None:
        coordinator.store.data["ownership"] = {
            **coordinator.store.data["ownership"],
            "ev_smart_charging_state": {"switch.ev": "off"},
        }

    coordinator.hass.services.async_call = helper_call

    assert asyncio.run(coordinator._async_clear_expired_manual_hvac_state()) is True
    assert coordinator.store.data["ownership"] == {
        "ev_smart_charging_state": {"switch.ev": "off"},
    }


def test_expired_manual_hvac_cleanup_keeps_override_until_helper_turns_off() -> None:
    coordinator = _coordinator_for_runtime_services()
    coordinator._async_clear_expired_manual_hvac_state = AsyncMock(return_value=False)

    asyncio.run(coordinator._async_reconcile_expired_manual_hvac_state(True))

    assert coordinator.overrides == [
        Override(
            kind="manual_hvac",
            source="helper",
            expires_at=None,
            reason="manual_hvac_helper_cleanup_failed",
        )
    ]

    coordinator._async_clear_expired_manual_hvac_state = AsyncMock(return_value=True)
    asyncio.run(coordinator._async_reconcile_expired_manual_hvac_state(True))

    assert coordinator.overrides == []


def test_manual_hvac_change_handler_uses_configured_duration() -> None:
    coordinator = _coordinator_for_runtime_services(options={"manual_hvac_override_minutes": 45})

    asyncio.run(
        coordinator._async_handle_manual_hvac_change(
            "climate_zone_changed",
            preserve_zone_entity_id="climate.bedrooms",
        )
    )

    assert coordinator.overrides[-1].reason == "climate_zone_changed"
    assert coordinator.executor.hvac_release_preserved_zones == ["climate.bedrooms"]
    assert coordinator.executor.hvac_release_preserved_main == [False]
    assert coordinator.refresh_requested == 1


def test_manual_main_hvac_change_handler_preserves_user_state() -> None:
    coordinator = _coordinator_for_runtime_services(options={"manual_hvac_override_minutes": 45})

    asyncio.run(
        coordinator._async_handle_manual_hvac_change(
            "daikin_state_changed",
            preserve_main_state=True,
        )
    )

    assert coordinator.overrides[-1].reason == "daikin_state_changed"
    assert coordinator.executor.hvac_release_preserved_main == [True]
    assert coordinator.refresh_requested == 1


def test_production_control_runtime_methods_update_store_and_refresh() -> None:
    coordinator = _coordinator_for_runtime_services(
        store_data={
            "production": {"dry_run_ready_cycles": 3},
            "ownership": {"hvac_control": {"phase": "peak_coast"}},
        }
    )

    asyncio.run(coordinator.async_arm_production_control("operator_ack"))
    asyncio.run(coordinator.async_disarm_production_control("operator_stop"))
    asyncio.run(coordinator.async_pause_control(30, "maintenance", "ev"))
    asyncio.run(coordinator.async_resume_control("maintenance_done"))

    assert coordinator.store.production_saves[0]["armed"] is True
    assert coordinator.store.production_saves[0]["armed_reason"] == "operator_ack"
    assert coordinator.store.production_saves[1]["armed"] is False
    assert coordinator.store.production_saves[1]["disarmed_reason"] == "operator_stop"
    assert coordinator.executor.hvac_releases == ["production_control_disarmed"]
    assert coordinator.store.control_pause_saves[0]["assets"] == ["ev"]
    assert coordinator.store.control_pause_saves[0]["reason"] == "maintenance"
    assert coordinator.store.control_pause_saves[1]["active"] is False
    assert coordinator.store.control_pause_saves[1]["reason"] == "maintenance_done"
    assert coordinator.refresh_requested == 2
    assert coordinator._refresh_generation == 2
    assert coordinator._pending_refresh_trigger == "control_resumed"


def test_combined_active_control_respects_selected_areas_and_arms(monkeypatch: object) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={
            CONF_DAIKIN_CLIMATE: "climate.home",
            CONF_EV_CHARGER: "switch.ev",
            CONF_ENPHASE_PROFILE: "select.profile",
        },
        options={
            CONF_PLANNER_ENABLED: False,
            CONF_DRY_RUN: True,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: False,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (False, True)
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {"safe_to_activate_now": True},
    )

    asyncio.run(coordinator.async_set_active_control(True))

    assert coordinator.entry.options[CONF_PLANNER_ENABLED] is True
    assert coordinator.entry.options[CONF_DRY_RUN] is False
    assert coordinator.entry.options[CONF_EV_CONTROL_ENABLED] is True
    assert coordinator.entry.options[CONF_CLIMATE_CONTROL_ENABLED] is False
    assert coordinator.entry.options[CONF_ENPHASE_CONTROL_ENABLED] is True
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.store.data["production"]["armed_reason"] == "automatic_control_enabled"
    assert coordinator.active_control is True


def test_effective_control_requires_active_intent_and_current_preflight(monkeypatch: object) -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False},
        store_data={"production": {"armed": True}},
    )
    report = {"active_control_ready": True}
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: report,
    )

    assert coordinator.effective_control is True

    report["active_control_ready"] = False
    assert coordinator.effective_control is False

    coordinator.entry.options[CONF_DRY_RUN] = True
    report["active_control_ready"] = True
    assert coordinator.effective_control is False


def test_combined_active_control_stays_in_review_until_evidence_is_ready(monkeypatch: object) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_DAIKIN_CLIMATE: "climate.home"},
        options={
            CONF_PLANNER_ENABLED: False,
            CONF_DRY_RUN: True,
            CONF_CLIMATE_CONTROL_ENABLED: True,
        },
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (False, True)
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {
            "safe_to_activate_now": False,
            "production": {"dry_run_ready_cycles": 1, "dry_run_evidence_complete": False},
            "checks": [],
        },
    )

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(coordinator.async_set_active_control(True))

    assert error.value.translation_key == "active_control_not_ready"
    assert "1/3 healthy plans" in error.value.translation_placeholders["reason"]
    assert coordinator.entry.options[CONF_PLANNER_ENABLED] is True
    assert coordinator.entry.options[CONF_DRY_RUN] is True
    assert coordinator.entry.options[CONF_CLIMATE_CONTROL_ENABLED] is True
    assert coordinator.store.data.get("production", {}).get("armed") is not True


def test_combined_active_control_requires_a_selected_device(monkeypatch: object) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: False, CONF_DRY_RUN: True},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (False, True)
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {
            "safe_to_activate_now": True,
            "production": {
                "device_controls": {"ev": False, "climate": False, "enphase": False},
            },
        },
    )

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(coordinator.async_set_active_control(True))

    assert error.value.translation_key == "active_control_no_device_selected"
    assert coordinator.entry.options[CONF_DRY_RUN] is True
    assert coordinator.store.data.get("production", {}).get("armed") is not True


@pytest.mark.parametrize(
    ("option_key", "executor_asset", "reason"),
    [
        (CONF_EV_CONTROL_ENABLED, "ev", "ev_control_disabled"),
        (CONF_CLIMATE_CONTROL_ENABLED, "daikin", "hvac_control_disabled"),
        (CONF_ENPHASE_CONTROL_ENABLED, "enphase", "enphase_control_disabled"),
    ],
)
def test_device_control_disable_while_active_restores_only_selected_asset(
    option_key: str,
    executor_asset: str,
    reason: str,
) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={
            CONF_EV_CHARGER: "switch.ev",
            CONF_DAIKIN_CLIMATE: "climate.home",
            CONF_ENPHASE_PROFILE: "select.profile",
        },
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: True,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
        store_data={"production": {"armed": True}},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, False)

    asyncio.run(coordinator.async_set_device_control(option_key, False))

    assert coordinator.entry.options[option_key] is False
    assert (
        sum(
            bool(coordinator.entry.options[key])
            for key in (
                CONF_EV_CONTROL_ENABLED,
                CONF_CLIMATE_CONTROL_ENABLED,
                CONF_ENPHASE_CONTROL_ENABLED,
            )
        )
        == 2
    )
    assert coordinator.entry.options[CONF_DRY_RUN] is False
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.executor.device_restores == [(executor_asset, reason)]
    assert coordinator.executor.restored == []
    assert coordinator.active_control is True


def test_device_control_restore_failure_still_turns_switch_off_and_keeps_master_armed() -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_ENPHASE_PROFILE: "select.profile"},
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
        store_data={"production": {"armed": True}},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, False)
    coordinator.executor.device_restore_result = SimpleNamespace(result=OutcomeResult.FAILED)

    asyncio.run(coordinator.async_set_device_control(CONF_ENPHASE_CONTROL_ENABLED, False))

    assert coordinator.entry.options[CONF_ENPHASE_CONTROL_ENABLED] is False
    assert coordinator.entry.options[CONF_DRY_RUN] is False
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.executor.device_restores == [("enphase", "enphase_control_disabled")]
    assert coordinator.refresh_requested == 1


def test_device_control_disable_remains_off_when_restore_fails_while_master_is_off() -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_ENPHASE_PROFILE: "select.profile"},
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: True,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
        store_data={"production": {"armed": False}},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, True)
    coordinator.executor.device_restore_result = SimpleNamespace(result=OutcomeResult.FAILED)

    asyncio.run(coordinator.async_set_device_control(CONF_ENPHASE_CONTROL_ENABLED, False))

    assert coordinator.entry.options[CONF_ENPHASE_CONTROL_ENABLED] is False
    assert coordinator.executor.device_restores == [("enphase", "enphase_control_disabled")]
    assert coordinator.refresh_requested == 1


def test_device_control_disable_remains_off_after_unexpected_restore_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev"},
        options={CONF_EV_CONTROL_ENABLED: True},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (False, True)

    async def fail_restore(asset: str, reason: str) -> object:
        raise RuntimeError(f"{asset}:{reason}")

    coordinator.executor.async_restore_device_control = fail_restore

    with caplog.at_level(logging.ERROR):
        asyncio.run(coordinator.async_set_device_control(CONF_EV_CONTROL_ENABLED, False))

    assert coordinator.entry.options[CONF_EV_CONTROL_ENABLED] is False
    assert coordinator.refresh_requested == 1
    assert "Unexpected error while restoring ev" in caplog.text


def test_device_control_disable_remains_successful_when_options_listener_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev"},
        options={CONF_EV_CONTROL_ENABLED: True},
    )
    coordinator.hass.config_entries = ConfigEntries()
    listener_updates = 0

    async def fail_options_update() -> None:
        raise RuntimeError("options listener failed")

    def update_listeners() -> None:
        nonlocal listener_updates
        listener_updates += 1

    coordinator.async_handle_options_update = fail_options_update
    coordinator.async_update_listeners = update_listeners

    with caplog.at_level(logging.ERROR):
        asyncio.run(coordinator.async_set_device_control(CONF_EV_CONTROL_ENABLED, False))

    assert coordinator.entry.options[CONF_EV_CONTROL_ENABLED] is False
    assert coordinator.executor.options[CONF_EV_CONTROL_ENABLED] is False
    assert listener_updates == 1
    assert "Unexpected error while applying the disabled ev control option" in caplog.text


def test_concurrent_device_control_changes_preserve_every_selector() -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={
            CONF_EV_CHARGER: "switch.ev",
            CONF_DAIKIN_CLIMATE: "climate.home",
            CONF_ENPHASE_PROFILE: "select.profile",
        },
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: True,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: True,
            CONF_ENPHASE_CONTROL_ENABLED: True,
        },
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, True)
    restores: list[tuple[str, str]] = []

    async def restore_after_yield(asset: str, reason: str) -> object:
        await asyncio.sleep(0)
        restores.append((asset, reason))
        return SimpleNamespace(result=OutcomeResult.RESTORED)

    coordinator.executor.async_restore_device_control = restore_after_yield

    async def disable_all() -> None:
        await asyncio.gather(
            coordinator.async_set_device_control(CONF_CLIMATE_CONTROL_ENABLED, False),
            coordinator.async_set_device_control(CONF_EV_CONTROL_ENABLED, False),
            coordinator.async_set_device_control(CONF_ENPHASE_CONTROL_ENABLED, False),
        )

    asyncio.run(disable_all())

    assert coordinator.entry.options[CONF_CLIMATE_CONTROL_ENABLED] is False
    assert coordinator.entry.options[CONF_EV_CONTROL_ENABLED] is False
    assert coordinator.entry.options[CONF_ENPHASE_CONTROL_ENABLED] is False
    assert {asset for asset, _reason in restores} == {"daikin", "ev", "enphase"}


def test_device_control_enable_while_active_preflights_without_disarming(monkeypatch: object) -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev", CONF_DAIKIN_CLIMATE: "climate.home"},
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: False,
        },
        store_data={"production": {"armed": True}},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, False)
    reports: list[dict[str, object]] = []

    def preflight(hass: object, coordinator_arg: object, *, options_override: dict[str, object]) -> dict[str, object]:
        reports.append(options_override)
        return {
            "safe_to_activate_now": True,
            "control_areas": {
                "ready": ["ev", "hvac"],
                "available": ["ev", "hvac"],
                "confidence_eligible": ["ev", "hvac"],
            },
        }

    monkeypatch.setattr(coordinator_module, "build_preflight_report", preflight)

    asyncio.run(coordinator.async_set_device_control(CONF_CLIMATE_CONTROL_ENABLED, True))

    assert reports[0][CONF_CLIMATE_CONTROL_ENABLED] is True
    assert coordinator.entry.options[CONF_CLIMATE_CONTROL_ENABLED] is True
    assert coordinator.entry.options[CONF_DRY_RUN] is False
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.executor.device_restores == []
    assert coordinator.active_control is True


@pytest.mark.parametrize(
    ("control_areas", "reason_fragment"),
    [
        (
            {
                "ready": ["ev"],
                "available": ["ev"],
                "confidence_eligible": ["ev"],
            },
            "not ready",
        ),
        (
            {
                "ready": ["ev", "hvac"],
                "available": ["ev"],
                "confidence_eligible": ["ev"],
            },
            "paused",
        ),
        (
            {
                "ready": ["ev", "hvac"],
                "available": ["ev", "hvac"],
                "confidence_eligible": ["ev"],
            },
            "does not meet confidence thresholds",
        ),
    ],
)
def test_device_control_enable_while_active_requires_selected_area_readiness(
    monkeypatch: object,
    control_areas: dict[str, list[str]],
    reason_fragment: str,
) -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev", CONF_DAIKIN_CLIMATE: "climate.home"},
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: False,
        },
        store_data={"production": {"armed": True}},
    )
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg, *, options_override: {
            "safe_to_activate_now": True,
            "control_areas": control_areas,
        },
    )

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(coordinator.async_set_device_control(CONF_CLIMATE_CONTROL_ENABLED, True))

    assert error.value.translation_key == "device_control_not_ready"
    assert reason_fragment in str(error.value)
    assert coordinator.entry.options[CONF_CLIMATE_CONTROL_ENABLED] is False
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.refresh_requested == 0


def test_device_control_enable_while_active_rejects_failed_preflight(monkeypatch: object) -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev", CONF_DAIKIN_CLIMATE: "climate.home"},
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: False,
        },
        store_data={"production": {"armed": True}},
    )
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg, *, options_override: {
            "safe_to_activate_now": False,
            "production": {"dry_run_ready_cycles": 2, "dry_run_evidence_complete": False},
        },
    )

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(coordinator.async_set_device_control(CONF_CLIMATE_CONTROL_ENABLED, True))

    assert error.value.translation_key == "device_control_not_ready"
    assert coordinator.entry.options[CONF_CLIMATE_CONTROL_ENABLED] is False
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.refresh_requested == 0


def test_device_control_cannot_enable_unconfigured_area() -> None:
    coordinator = _coordinator_for_runtime_services()

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(coordinator.async_set_device_control(CONF_EV_CONTROL_ENABLED, True))

    assert error.value.translation_key == "device_control_not_configured"
    assert coordinator.entry.options.get(CONF_EV_CONTROL_ENABLED) is None


def test_device_control_rejects_unknown_option() -> None:
    coordinator = _coordinator_for_runtime_services()

    with pytest.raises(ValueError, match="Unsupported device control option"):
        asyncio.run(coordinator.async_set_device_control("unknown", True))


def test_device_control_unchanged_is_a_noop() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_EV_CONTROL_ENABLED: True},
    )

    asyncio.run(coordinator.async_set_device_control(CONF_EV_CONTROL_ENABLED, True))

    assert coordinator.refresh_requested == 0
    assert coordinator.store.production_saves == []


def test_combined_active_control_off_restores_review_mode_and_disarms() -> None:
    class ConfigEntries:
        def async_update_entry(self, entry: FakeEntry, *, options: dict[str, object]) -> None:
            entry.options = options

    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False},
        store_data={"production": {"armed": True}},
    )
    coordinator.hass.config_entries = ConfigEntries()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._last_control_mode_state = (True, False)

    asyncio.run(coordinator.async_set_active_control(False))

    assert coordinator.entry.options[CONF_PLANNER_ENABLED] is True
    assert coordinator.entry.options[CONF_DRY_RUN] is True
    assert coordinator.store.data["production"]["armed"] is False
    assert coordinator.store.data["production"]["disarmed_reason"] == "automatic_control_disabled"
    assert coordinator.executor.restored == ["dry_run_enabled"]
    assert coordinator.active_control is False


def test_combined_active_control_is_a_noop_when_already_active() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False},
        store_data={"production": {"armed": True}},
    )

    asyncio.run(coordinator.async_set_active_control(True))

    assert coordinator.refresh_requested == 0
    assert coordinator.store.production_saves == []


def test_combined_active_control_reports_blocking_or_current_plan_reason() -> None:
    blocking = _active_control_not_ready_reason(
        {
            "production": {"dry_run_ready_cycles": 3, "dry_run_evidence_complete": True},
            "checks": [{"blocking": True, "ok": False, "message": "A mapped entity is unavailable."}],
        }
    )
    fallback = _active_control_not_ready_reason(
        {
            "production": {"dry_run_ready_cycles": 3, "dry_run_evidence_complete": True},
            "checks": [],
            "current_plan": {},
        }
    )

    assert blocking == "A mapped entity is unavailable."
    assert fallback == "a safety check failed"


def test_production_pause_fallback_persistence_handles_lightweight_stores() -> None:
    coordinator = _coordinator_for_runtime_services()
    coordinator.store = type("Store", (), {"data": {}})()

    asyncio.run(coordinator.async_arm_production_control("ack"))
    asyncio.run(coordinator.async_pause_control(10, "pause", "invalid"))
    asyncio.run(coordinator._async_record_dry_run_comparison(_plan("plan-1")))

    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.store.data["control_pause"]["assets"] == ["all"]
    assert coordinator.store.data["dry_run_comparisons"][0]["plan_id"] == "plan-1"


def test_production_evidence_and_dry_run_comparison_are_recorded() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    action = PlanAction(
        action_id="ev",
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.EV,
        kind=ActionKind.EV_START,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )
    coordinator = _coordinator_for_runtime_services(
        store_data={"execution_audit": [{"result": "applied", "action_id": "previous"}]}
    )
    dry_run = EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=1.23,
        actions=[action],
        preview=[],
    )
    unsafe = _plan("unsafe")
    unsafe.health = InputHealth.UNSAFE

    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    asyncio.run(coordinator._async_record_dry_run_comparison(dry_run))
    asyncio.run(coordinator._async_update_production_evidence(unsafe, []))

    assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1
    assert coordinator.store.data["production"]["last_blocking_reason"] == "input_health_unsafe"
    comparison = coordinator.store.dry_run_comparisons[0]
    assert comparison["planned_action_count"] == 1
    assert comparison["next_action"]["action_id"] == "ev"
    assert comparison["recent_outcome_count"] == 1


def test_asset_safe_degraded_plan_records_production_evidence() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={
            CONF_EV_CHARGER: "switch.ev",
            CONF_EV_SOC: "sensor.ev_soc",
            CONF_EV_CHARGING: "binary_sensor.ev_charging",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "sensor.ev_target",
        },
        options={CONF_EV_CONTROL_ENABLED: True},
        hass=FakeHass(
            {
                "switch.ev": "off",
                "sensor.ev_soc": "60",
                "binary_sensor.ev_charging": "off",
                "sensor.ev_target": "80",
            }
        ),
    )
    plan = _plan("degraded-review")
    plan.mode = PlannerMode.DRY_RUN
    plan.health = InputHealth.DEGRADED
    plan.confidence = 0.65
    plan.confidence_breakdown = {
        "tariff": 0.65,
        "solar": 0.65,
        "load": 0.65,
        "climate": 0.4,
        "ev": 0.65,
        "enphase": 0.4,
    }

    asyncio.run(coordinator._async_update_production_evidence(plan, []))

    production = coordinator.store.data["production"]
    assert production["dry_run_ready_cycles"] == 1
    assert production["dry_run_evidence_fingerprint"] == production_evidence_fingerprint(
        coordinator.entry_data,
        coordinator.options,
    )


def test_production_evidence_resets_when_control_contract_changes() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
        options={"ev_control_enabled": True},
    )
    dry_run = _plan("dry-run")
    dry_run.mode = PlannerMode.DRY_RUN

    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    first_fingerprint = coordinator.store.data["production"]["dry_run_evidence_fingerprint"]
    coordinator.entry.data["enphase_profile_entity"] = "select.enphase"
    coordinator.entry.options["enphase_control_enabled"] = True
    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))

    assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1
    assert coordinator.store.data["production"]["dry_run_evidence_fingerprint"] != first_fingerprint


def test_changed_production_contract_restores_and_disarms_before_rearming() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_HOUSEHOLD_LOAD: "sensor.house_b"},
        store_data={
            "production": {
                "armed": True,
                "dry_run_ready_cycles": 3,
                "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                    {CONF_HOUSEHOLD_LOAD: "sensor.house_a"},
                    {},
                ),
            }
        },
    )

    changed = asyncio.run(coordinator.async_reconcile_production_evidence_contract())

    assert changed is True
    assert coordinator.executor.restored == ["production_evidence_contract_changed"]
    assert coordinator.store.data["production"]["armed"] is False
    assert coordinator.store.data["production"]["disarmed_reason"] == "production_evidence_contract_changed"
    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is False


def test_previously_active_startup_preserves_arming_and_intent() -> None:
    entry_data = {CONF_EV_CHARGER: "switch.ev"}
    options = {
        CONF_PLANNER_ENABLED: True,
        CONF_DRY_RUN: False,
        CONF_EV_CONTROL_ENABLED: True,
    }
    coordinator = _coordinator_for_runtime_services(
        entry_data=entry_data,
        options=options,
        store_data={
            "production": {
                "armed": True,
                "dry_run_ready_cycles": 3,
                "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                    entry_data,
                    options,
                ),
                "startup_auto_recovery": {
                    "status": "grace",
                    "successful_runs": 0,
                    "deadline": "2026-08-15T01:00:00+00:00",
                },
            },
            "ownership": {"ev_smart_charging_state": {"state": "on"}},
            "ev_grid_reservation": {"active": True, "load_kw": 7.2},
        },
    )
    coordinator._startup_auto_recovery_authorized = False
    coordinator.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
        coordinator.entry_data,
        coordinator.options,
    )

    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True

    production = coordinator.store.data["production"]
    assert coordinator.automatic_control_requested is True
    assert coordinator.active_control is True
    assert production["armed"] is True
    assert production["startup_auto_recovery"]["status"] == "waiting_for_home_assistant"
    assert production["startup_auto_recovery"]["required_runs"] == 1
    assert "deadline" not in production["startup_auto_recovery"]
    assert coordinator.executor.notification_grace_until == datetime.max.replace(tzinfo=UTC)
    assert coordinator.executor.restored == []
    assert coordinator.store.data["ownership"]["ev_smart_charging_state"]["state"] == "on"
    assert coordinator.store.data["ev_grid_reservation"]["active"] is True


def test_paused_startup_disarms_and_preserves_auto_recovery() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={
            "production": {"armed": True},
            "control_pause": {
                "active": True,
                "until": datetime.now(UTC) + timedelta(hours=1),
                "reason": "operator_pause",
            },
        },
    )
    coordinator._startup_auto_recovery_authorized = False

    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True

    assert coordinator.store.data["production"]["armed"] is False
    assert coordinator.store.data["production"]["disarmed_reason"] == "startup_control_paused"
    recovery = coordinator.store.data["production"]["startup_auto_recovery"]
    assert recovery["status"] == "waiting_for_safe"
    assert recovery["successful_runs"] == 0
    assert recovery["last_reason"] == "startup_control_paused"
    assert coordinator.executor.restored == ["startup_control_paused"]
    assert coordinator._startup_auto_recovery_authorized is True

    # If Home Assistant stops before the pause clears, the persisted handoff
    # must authorize a fresh recovery task on the next setup.
    coordinator.store.data["control_pause"]["until"] = datetime.now(UTC) - timedelta(seconds=1)
    restarted = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data=coordinator.store.data,
    )
    restarted._startup_auto_recovery_authorized = False

    assert asyncio.run(restarted.async_reconcile_production_evidence_contract()) is True

    restarted_recovery = restarted.store.data["production"]["startup_auto_recovery"]
    assert restarted_recovery["status"] == "waiting_for_home_assistant"
    assert restarted_recovery["successful_runs"] == 0
    assert restarted_recovery["last_reason"] == "startup_safe_recovery_pending"
    assert restarted._startup_auto_recovery_authorized is True
    assert restarted.store.data["production"]["armed"] is False


def test_scoped_pause_preserves_unaffected_control_area_at_startup() -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={
            CONF_EV_CHARGER: "switch.ev",
            CONF_DAIKIN_CLIMATE: "climate.daikin",
        },
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_CLIMATE_CONTROL_ENABLED: True,
        },
        store_data={
            "production": {"armed": True},
            "control_pause": {
                "active": True,
                "until": datetime.now(UTC) + timedelta(hours=1),
                "assets": ["ev"],
                "reason": "ev_backoff",
            },
        },
    )
    coordinator._startup_auto_recovery_authorized = False
    coordinator.store.data["production"].update(
        {
            "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                coordinator.entry_data,
                coordinator.options,
            ),
        }
    )

    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True

    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.executor.restored == []
    assert coordinator._startup_auto_recovery_authorized is True


def test_scoped_pause_does_not_approve_a_changed_production_contract() -> None:
    entry_data = {
        CONF_EV_CHARGER: "switch.ev_new",
        CONF_DAIKIN_CLIMATE: "climate.daikin",
    }
    options = {
        CONF_PLANNER_ENABLED: True,
        CONF_DRY_RUN: False,
        CONF_EV_CONTROL_ENABLED: True,
        CONF_CLIMATE_CONTROL_ENABLED: True,
    }
    coordinator = _coordinator_for_runtime_services(
        entry_data=entry_data,
        options=options,
        store_data={
            "production": {
                "armed": True,
                "dry_run_ready_cycles": 3,
                "dry_run_evidence_fingerprint": production_evidence_fingerprint(
                    {
                        CONF_EV_CHARGER: "switch.ev_old",
                        CONF_DAIKIN_CLIMATE: "climate.daikin",
                    },
                    options,
                ),
            },
            "control_pause": {
                "active": True,
                "until": datetime.now(UTC) + timedelta(hours=1),
                "assets": ["ev"],
                "reason": "ev_backoff",
            },
        },
    )

    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["disarmed_reason"] == "production_evidence_contract_changed"
    assert coordinator.executor.restored == ["production_evidence_contract_changed"]


@pytest.mark.parametrize(
    "options",
    [
        {CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: True, CONF_EV_CONTROL_ENABLED: True},
        {CONF_PLANNER_ENABLED: False, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
    ],
)
def test_previously_non_active_startup_never_auto_arms(options: dict[str, object]) -> None:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev"},
        options=options,
        store_data={
            "production": {
                "armed": False,
                "startup_auto_recovery": {"status": "grace", "successful_runs": 2},
            }
        },
    )
    coordinator._startup_auto_recovery_authorized = False

    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "interrupted"
    assert coordinator._startup_auto_recovery_authorized is False


def test_disarmed_safe_recovery_resumes_after_restart_with_counter_reset() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={
            "production": {
                "armed": False,
                "startup_auto_recovery": {
                    "status": "waiting_for_safe",
                    "successful_runs": 2,
                },
            }
        },
    )
    coordinator._startup_auto_recovery_authorized = False

    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True

    recovery = coordinator.store.data["production"]["startup_auto_recovery"]
    assert recovery["status"] == "waiting_for_home_assistant"
    assert recovery["successful_runs"] == 0
    assert coordinator._startup_auto_recovery_authorized is True
    assert coordinator.store.data["production"]["armed"] is False


def test_safe_startup_grace_keeps_control_armed_and_silent() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={"production": {"armed": True}},
    )
    coordinator._startup_auto_recovery_authorized = True
    coordinator._startup_auto_recovery_deadline = coordinator_module.monotonic()
    coordinator._async_run_startup_auto_recovery_validation = AsyncMock(
        return_value=(True, "validation_succeeded")
    )

    assert asyncio.run(coordinator._async_complete_startup_grace()) == (
        True,
        "startup_grace_completed_healthy",
    )

    production = coordinator.store.data["production"]
    assert production["armed"] is True
    assert production["startup_auto_recovery"]["status"] == "recovered"
    assert production["startup_auto_recovery"]["required_runs"] == 1
    assert coordinator.executor.restored == []
    assert coordinator.executor.startup_recovery_notifications == []
    assert coordinator.executor.startup_recovery_dismissals == 1
    assert coordinator.executor.notification_grace_until is None


def test_degraded_ready_area_passes_complete_startup_grace(monkeypatch: object) -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={"production": {"armed": True}},
    )
    coordinator._startup_auto_recovery_authorized = True
    coordinator._startup_auto_recovery_deadline = coordinator_module.monotonic()
    plan = _plan("degraded-startup")
    plan.health = InputHealth.DEGRADED

    async def commit_degraded_refresh() -> None:
        coordinator._record_startup_auto_recovery_validation_candidate(plan, [])
        coordinator._last_startup_auto_recovery_validation["committed"] = True

    coordinator.async_refresh = commit_degraded_refresh
    report = {
        "control_areas": {
            "required": ["ev", "hvac"],
            "ready": ["hvac"],
            "available": ["hvac"],
            "confidence_eligible": ["hvac"],
        },
        "recorder": {"available": True},
        "current_plan": {"safe": True},
    }
    monkeypatch.setattr(coordinator_module, "build_preflight_report", lambda hass, item: report)

    assert asyncio.run(coordinator._async_complete_startup_grace()) == (
        True,
        "startup_grace_completed_healthy",
    )
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.executor.restored == []


def test_unsafe_startup_grace_disarms_restores_notifies_and_retains_intent() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={"production": {"armed": True}},
    )
    coordinator._startup_auto_recovery_authorized = True
    coordinator._startup_auto_recovery_deadline = coordinator_module.monotonic()
    coordinator._async_run_startup_auto_recovery_validation = AsyncMock(
        return_value=(False, "configured_entities_unavailable")
    )

    safe, reason = asyncio.run(coordinator._async_complete_startup_grace())
    assert safe is False
    asyncio.run(coordinator._async_enter_startup_safe_recovery(reason))

    production = coordinator.store.data["production"]
    assert coordinator.automatic_control_requested is True
    assert coordinator.active_control is False
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "waiting_for_safe"
    assert coordinator.executor.restored == ["startup_grace_unsafe"]
    assert coordinator.executor.startup_recovery_notifications == [
        "configured_entities_unavailable"
    ]
    assert coordinator.executor.notification_grace_until is None


def test_interrupted_unsafe_transition_resumes_disarmed_recovery_after_restart() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={
            "production": {
                "armed": True,
                "startup_auto_recovery": {"status": "grace", "successful_runs": 0},
            }
        },
    )
    coordinator._startup_auto_recovery_authorized = True
    coordinator.async_restore_safe_state = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coordinator._async_enter_startup_safe_recovery("current_plan_unsafe"))

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "restoring"

    coordinator._startup_auto_recovery_authorized = False
    assert asyncio.run(coordinator.async_reconcile_production_evidence_contract()) is True
    recovery = coordinator.store.data["production"]["startup_auto_recovery"]
    assert recovery["status"] == "waiting_for_home_assistant"
    assert coordinator._startup_auto_recovery_authorized is True


def test_startup_safe_recovery_rearms_after_three_awaited_healthy_checks(
    monkeypatch: object,
) -> None:
    coordinator = _startup_recovery_test_coordinator()
    validations = AsyncMock(return_value=(True, "validation_succeeded"))
    coordinator._async_run_startup_auto_recovery_validation = validations
    coordinator.async_refresh = AsyncMock()
    coordinator.async_restore_safe_state = AsyncMock(
        return_value=SimpleNamespace(result=OutcomeResult.RESTORED)
    )
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, item: _startup_recovery_report(item),
    )
    monkeypatch.setattr(coordinator_module, "STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS", 0)

    asyncio.run(coordinator._async_retry_startup_safe_recovery())

    production = coordinator.store.data["production"]
    assert validations.await_count == 3
    coordinator.async_refresh.assert_awaited_once_with()
    assert production["armed"] is True
    assert production["armed_reason"] == "startup_auto_recovered"
    assert production["startup_auto_recovery"]["status"] == "recovered"
    assert production["startup_auto_recovery"]["successful_runs"] == 3
    assert coordinator.executor.startup_recovery_dismissals == 1


def test_startup_safe_recovery_resets_consecutive_checks_on_unsafe(
    monkeypatch: object,
) -> None:
    coordinator = _startup_recovery_test_coordinator()
    results = iter(
        (
            (True, "validation_succeeded"),
            (False, "validation_plan_unsafe"),
            (True, "validation_succeeded"),
            (True, "validation_succeeded"),
            (True, "validation_succeeded"),
        )
    )
    validations = AsyncMock(side_effect=lambda: next(results))
    coordinator._async_run_startup_auto_recovery_validation = validations
    coordinator.async_refresh = AsyncMock()
    coordinator.async_restore_safe_state = AsyncMock(
        return_value=SimpleNamespace(result=OutcomeResult.RESTORED)
    )
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, item: _startup_recovery_report(item),
    )
    monkeypatch.setattr(coordinator_module, "STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS", 0)

    asyncio.run(coordinator._async_retry_startup_safe_recovery())

    assert validations.await_count == 5
    assert coordinator.executor.startup_recovery_notifications == [
        "validation_plan_unsafe"
    ]
    assert coordinator.store.data["production"]["startup_auto_recovery"]["successful_runs"] == 3
    assert coordinator.store.data["production"]["armed"] is True


def test_reactivation_failure_stays_disarmed_then_retries_automatically(
    monkeypatch: object,
) -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator._async_run_startup_auto_recovery_validation = AsyncMock(
        return_value=(True, "validation_succeeded")
    )
    coordinator.async_refresh = AsyncMock()
    restore_results = iter(
        (
            SimpleNamespace(result=OutcomeResult.FAILED, reason="restore_failed"),
            SimpleNamespace(result=OutcomeResult.RESTORED),
        )
    )
    coordinator.async_restore_safe_state = AsyncMock(side_effect=lambda *args, **kwargs: next(restore_results))
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, item: _startup_recovery_report(item),
    )
    monkeypatch.setattr(coordinator_module, "STARTUP_AUTO_RECOVERY_VALIDATION_INTERVAL_SECONDS", 0)

    asyncio.run(coordinator._async_retry_startup_safe_recovery())

    assert coordinator._async_run_startup_auto_recovery_validation.await_count == 6
    assert coordinator.executor.startup_recovery_notifications == ["restore_failed"]
    assert coordinator.store.data["production"]["armed"] is True
    assert coordinator.store.data["production"]["startup_auto_recovery"]["status"] == "recovered"


def test_startup_recovery_cancellation_is_immediately_authoritative() -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"].update(
        {
            "armed": True,
            "startup_auto_recovery": {"status": "grace", "successful_runs": 0},
        }
    )
    coordinator._startup_auto_recovery_start_unsub = None
    coordinator._startup_auto_recovery_task = None

    asyncio.run(coordinator.async_cancel_startup_auto_recovery("options_changed"))

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "cancelled"
    assert coordinator.executor.restored == [
        "startup_auto_recovery_cancelled:options_changed"
    ]


def test_operator_disarm_cancels_recovery_without_rearming_or_restoring() -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"].update(
        {
            "armed": True,
            "startup_auto_recovery": {"status": "grace", "successful_runs": 0},
        }
    )
    coordinator._startup_auto_recovery_task = None

    asyncio.run(coordinator.async_operator_disarm_production_control("button_pressed"))

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["disarmed_reason"] == "button_pressed"
    assert production["startup_auto_recovery"]["status"] == "cancelled"
    assert coordinator._startup_auto_recovery_authorized is False
    assert coordinator.executor.restored == []
    assert coordinator.executor.startup_recovery_dismissals == 1


def test_operator_arm_cancels_disarmed_recovery_before_granting_authority(
    monkeypatch: object,
) -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"]["startup_auto_recovery"] = {
        "status": "waiting_for_safe",
        "successful_runs": 1,
    }
    coordinator._startup_auto_recovery_task = None
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {"safe_to_activate_now": True},
    )

    asyncio.run(coordinator.async_operator_arm_production_control("button_pressed"))

    production = coordinator.store.data["production"]
    assert production["armed"] is True
    assert production["armed_reason"] == "button_pressed"
    assert production["startup_auto_recovery"]["status"] == "cancelled"
    assert coordinator._startup_auto_recovery_authorized is False
    assert coordinator.executor.restored == []
    assert coordinator.executor.startup_recovery_dismissals == 1


def test_operator_arm_rejects_stale_evidence_without_cancelling_recovery(
    monkeypatch: object,
) -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"].update(
        {
            "armed": False,
            "dry_run_ready_cycles": 3,
            "dry_run_evidence_fingerprint": "stale-contract",
            "startup_auto_recovery": {
                "status": "waiting_for_safe",
                "successful_runs": 1,
            },
        }
    )
    coordinator._startup_auto_recovery_task = None
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, coordinator_arg: {
            "safe_to_activate_now": False,
            "production": {
                "dry_run_ready_cycles": 3,
                "dry_run_evidence_complete": False,
            },
            "checks": [],
        },
    )

    with pytest.raises(HomeAssistantError) as error:
        asyncio.run(coordinator.async_operator_arm_production_control("button_pressed"))

    production = coordinator.store.data["production"]
    assert error.value.translation_key == "active_control_not_ready"
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "waiting_for_safe"
    assert coordinator._startup_auto_recovery_authorized is True
    assert coordinator.executor.startup_recovery_dismissals == 0


def test_home_assistant_shutdown_preserves_persisted_recovery_state() -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"]["startup_auto_recovery"] = {
        "status": "waiting_for_safe",
        "successful_runs": 2,
        "required_runs": 3,
    }
    coordinator._startup_auto_recovery_start_unsub = None
    coordinator._startup_auto_recovery_task = None

    asyncio.run(
        coordinator.async_cancel_startup_auto_recovery(
            "home_assistant_shutdown",
            preserve_control=True,
        )
    )

    assert coordinator.store.data["production"]["startup_auto_recovery"] == {
        "status": "waiting_for_safe",
        "successful_runs": 2,
        "required_runs": 3,
    }
    assert coordinator.executor.restored == []


def test_pause_preserves_disarmed_startup_recovery_until_resume() -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"]["startup_auto_recovery"] = {
        "status": "waiting_for_safe",
        "successful_runs": 0,
        "required_runs": 3,
    }

    class RunningTask:
        cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancelled = True

    task = RunningTask()
    coordinator._startup_auto_recovery_task = task

    asyncio.run(coordinator.async_pause_control(30, "maintenance"))
    asyncio.run(coordinator.async_resume_control("maintenance_complete"))

    recovery = coordinator.store.data["production"]["startup_auto_recovery"]
    assert coordinator._startup_auto_recovery_authorized is True
    assert coordinator._startup_auto_recovery_task is task
    assert task.cancelled is False
    assert recovery["status"] == "waiting_for_safe"
    assert coordinator.store.data["control_pause"]["active"] is False


def test_configuration_reload_persists_disarmed_recovery_handoff() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={"production": {"armed": True}},
    )
    coordinator._startup_auto_recovery_authorized = False
    coordinator._startup_auto_recovery_task = None
    coordinator._startup_auto_recovery_start_unsub = None

    asyncio.run(coordinator.async_prepare_configuration_reload())

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "waiting_for_safe"
    assert production["startup_auto_recovery"]["successful_runs"] == 0
    assert coordinator.executor.restored == ["configuration_changed"]
    assert coordinator._configuration_reload_handoff is True


def test_configuration_reload_restarts_an_existing_disarmed_recovery() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={
            "production": {
                "armed": False,
                "startup_auto_recovery": {
                    "status": "waiting_for_safe",
                    "successful_runs": 1,
                },
            }
        },
    )
    coordinator._startup_auto_recovery_authorized = True
    coordinator._startup_auto_recovery_task = None
    coordinator._startup_auto_recovery_start_unsub = None

    asyncio.run(coordinator.async_prepare_configuration_reload())

    recovery = coordinator.store.data["production"]["startup_auto_recovery"]
    assert recovery["status"] == "waiting_for_safe"
    assert recovery["successful_runs"] == 0
    assert coordinator.executor.restored == ["configuration_changed"]
    assert coordinator._configuration_reload_handoff is True


def test_configuration_reload_blocks_failed_restore_with_remaining_ownership() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
        },
        store_data={
            "production": {"armed": True},
            "ownership": {"ev_smart_charging_state": {"state": "on"}},
            "ev_grid_reservation": {"active": True, "load_kw": 7.2},
        },
    )
    coordinator._startup_auto_recovery_authorized = False
    coordinator._startup_auto_recovery_task = None
    coordinator._startup_auto_recovery_start_unsub = None
    coordinator.async_restore_safe_state = AsyncMock(
        return_value=SimpleNamespace(
            result=OutcomeResult.FAILED,
            reason="ev_restore_failed",
        )
    )

    with pytest.raises(HomeAssistantError, match="ev_restore_failed"):
        asyncio.run(coordinator.async_prepare_configuration_reload())

    production = coordinator.store.data["production"]
    assert production["armed"] is False
    assert production["startup_auto_recovery"]["status"] == "failed"
    assert coordinator._configuration_reload_handoff is False


def test_startup_recovery_start_callback_and_shutdown_edge_paths(monkeypatch: object) -> None:
    callbacks: list[object] = []
    unsubscribed: list[bool] = []

    def at_started(hass: object, action: object) -> object:
        callbacks.append(action)
        return lambda: unsubscribed.append(True)

    monkeypatch.setattr(coordinator_module, "async_at_started", at_started)
    coordinator = _startup_recovery_test_coordinator()
    coordinator._startup_auto_recovery_start_unsub = None
    coordinator.executor.notification_grace_until = None

    class RunningTask:
        def done(self) -> bool:
            return False

    coordinator._startup_auto_recovery_task = RunningTask()
    coordinator.async_start_startup_auto_recovery()
    assert callbacks == []

    coordinator._startup_auto_recovery_task = None
    coordinator.async_start_startup_auto_recovery()
    coordinator._startup_auto_recovery_authorized = False
    asyncio.run(callbacks[-1](coordinator.hass))

    coordinator._startup_auto_recovery_authorized = True
    coordinator.async_start_startup_auto_recovery()
    asyncio.run(callbacks[-1](coordinator.hass))
    assert coordinator.store.data["production"]["startup_auto_recovery"]["status"] == "waiting_for_safe"

    coordinator._tearing_down = False
    coordinator._debounce_cancel = None
    coordinator._boundary_cancel = None
    coordinator._ai_advice_task = None
    coordinator._startup_auto_recovery_task = None
    coordinator._startup_auto_recovery_start_unsub = lambda: unsubscribed.append(True)
    coordinator._unsub_listeners = []
    coordinator.async_shutdown()
    assert unsubscribed


def test_startup_recovery_cancellation_covers_task_and_restore_failure() -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"].update(
        {"armed": True, "startup_auto_recovery": {"status": "grace", "successful_runs": "bad"}}
    )
    coordinator._startup_auto_recovery_start_unsub = lambda: None
    coordinator.async_restore_safe_state = AsyncMock(side_effect=RuntimeError("restore failed"))

    async def cancel() -> asyncio.Task[object]:
        task = asyncio.create_task(asyncio.Event().wait())
        coordinator._startup_auto_recovery_task = task
        await coordinator.async_cancel_startup_auto_recovery("options_changed")
        return task

    task = asyncio.run(cancel())

    assert task.cancelled() is True
    assert coordinator.store.data["production"]["armed"] is False
    assert coordinator.store.data["production"]["startup_auto_recovery"]["status"] == "cancelled"


def test_startup_recovery_orchestration_safe_unsafe_cancelled_and_error_paths() -> None:
    safe = _startup_recovery_test_coordinator()
    safe.store.data["production"]["armed"] = True
    safe._async_complete_startup_grace = AsyncMock(return_value=(True, "healthy"))
    safe._async_retry_startup_safe_recovery = AsyncMock()
    asyncio.run(safe._async_run_startup_auto_recovery())
    safe._async_retry_startup_safe_recovery.assert_not_awaited()

    unsafe = _startup_recovery_test_coordinator()
    unsafe.store.data["production"]["armed"] = True
    unsafe._async_complete_startup_grace = AsyncMock(return_value=(False, "unsafe"))
    unsafe._async_enter_startup_safe_recovery = AsyncMock()
    unsafe._async_retry_startup_safe_recovery = AsyncMock()
    asyncio.run(unsafe._async_run_startup_auto_recovery())
    unsafe._async_enter_startup_safe_recovery.assert_awaited_once_with("unsafe")
    unsafe._async_retry_startup_safe_recovery.assert_awaited_once_with()

    cancelled_after_check = _startup_recovery_test_coordinator()
    cancelled_after_check.store.data["production"]["armed"] = True
    cancelled_after_check._async_complete_startup_grace = AsyncMock(return_value=(False, "cancelled"))
    cancelled_after_check._startup_auto_recovery_authorized = False
    cancelled_after_check._async_enter_startup_safe_recovery = AsyncMock()
    asyncio.run(cancelled_after_check._async_run_startup_auto_recovery())
    cancelled_after_check._async_enter_startup_safe_recovery.assert_not_awaited()

    propagates = _startup_recovery_test_coordinator()
    propagates.store.data["production"]["armed"] = True
    propagates._async_complete_startup_grace = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(propagates._async_run_startup_auto_recovery())

    unexpected = _startup_recovery_test_coordinator()
    unexpected._async_retry_startup_safe_recovery = AsyncMock(side_effect=RuntimeError("unexpected"))
    unexpected._async_enter_startup_safe_recovery = AsyncMock()
    asyncio.run(unexpected._async_run_startup_auto_recovery())
    unexpected._async_enter_startup_safe_recovery.assert_awaited_once_with("unexpected_recovery_error")
    assert unexpected.hass.created_tasks

    nested_failure = _startup_recovery_test_coordinator()
    nested_failure._async_retry_startup_safe_recovery = AsyncMock(side_effect=RuntimeError("unexpected"))
    nested_failure._async_enter_startup_safe_recovery = AsyncMock(side_effect=RuntimeError("persist failed"))
    nested_failure.async_disarm_production_control = AsyncMock(side_effect=RuntimeError("disarm failed"))
    asyncio.run(nested_failure._async_run_startup_auto_recovery())
    nested_failure.async_disarm_production_control.assert_awaited_once_with("unexpected_recovery_error")


def test_startup_grace_default_deadline_sleep_and_cancel(monkeypatch: object) -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"]["armed"] = True
    coordinator._startup_auto_recovery_deadline = None
    coordinator._async_run_startup_auto_recovery_validation = AsyncMock(
        return_value=(True, "validation_succeeded")
    )
    sleep = AsyncMock()
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(coordinator_module, "STARTUP_AUTO_RECOVERY_TIMEOUT_SECONDS", 1)

    assert asyncio.run(coordinator._async_complete_startup_grace())[0] is True
    sleep.assert_awaited_once()

    cancelled = _startup_recovery_test_coordinator()
    cancelled._startup_auto_recovery_authorized = False
    cancelled._startup_auto_recovery_deadline = coordinator_module.monotonic()
    assert asyncio.run(cancelled._async_complete_startup_grace()) == (
        False,
        "startup_recovery_cancelled",
    )


def test_startup_safe_recovery_restore_failure_paths() -> None:
    failed = _startup_recovery_test_coordinator()
    failed.async_restore_safe_state = AsyncMock(
        return_value=SimpleNamespace(result=OutcomeResult.FAILED, reason="restore_failed")
    )
    asyncio.run(failed._async_enter_startup_safe_recovery("unsafe"))
    assert failed.executor.startup_recovery_notifications == ["restore_failed"]

    raised = _startup_recovery_test_coordinator()
    raised.async_restore_safe_state = AsyncMock(side_effect=RuntimeError("restore raised"))
    asyncio.run(raised._async_enter_startup_safe_recovery("unsafe"))
    assert raised.executor.startup_recovery_notifications == ["safe_state_restore_failed"]


def test_startup_safe_recovery_stops_if_cancelled_during_interval(monkeypatch: object) -> None:
    coordinator = _startup_recovery_test_coordinator()

    async def cancel_on_sleep(delay: float) -> None:
        coordinator._startup_auto_recovery_authorized = False

    monkeypatch.setattr(coordinator_module.asyncio, "sleep", cancel_on_sleep)
    coordinator._async_run_startup_auto_recovery_validation = AsyncMock()

    asyncio.run(coordinator._async_retry_startup_safe_recovery())

    coordinator._async_run_startup_auto_recovery_validation.assert_not_awaited()


def test_startup_reactivation_failure_branches(monkeypatch: object) -> None:
    restore_raises = _startup_recovery_test_coordinator()
    restore_raises.async_restore_safe_state = AsyncMock(side_effect=RuntimeError("restore raised"))
    assert asyncio.run(restore_raises._async_reactivate_after_startup_recovery(3)) == (
        False,
        "safe_state_restore_failed",
    )

    blocked = _startup_recovery_test_coordinator()
    blocked.async_restore_safe_state = AsyncMock(return_value=SimpleNamespace(result=OutcomeResult.RESTORED))
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, item: {
            **_startup_recovery_report(item),
            "current_plan": {"safe": False},
        },
    )
    assert asyncio.run(blocked._async_reactivate_after_startup_recovery(3)) == (
        False,
        "current_plan_unsafe",
    )

    preflight_blocked = _startup_recovery_test_coordinator()
    preflight_blocked.async_restore_safe_state = AsyncMock(
        return_value=SimpleNamespace(result=OutcomeResult.RESTORED)
    )
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, item: {**_startup_recovery_report(item), "safe_to_activate_now": False},
    )
    assert asyncio.run(preflight_blocked._async_reactivate_after_startup_recovery(3)) == (
        False,
        "final_preflight_failed",
    )

    refresh_failed = _startup_recovery_test_coordinator()
    refresh_failed.async_restore_safe_state = AsyncMock(
        side_effect=(SimpleNamespace(result=OutcomeResult.RESTORED), RuntimeError("second restore failed"))
    )
    refresh_failed.async_refresh = AsyncMock(side_effect=RuntimeError("refresh failed"))
    monkeypatch.setattr(
        coordinator_module,
        "build_preflight_report",
        lambda hass, item: _startup_recovery_report(item),
    )
    assert asyncio.run(refresh_failed._async_reactivate_after_startup_recovery(3)) == (
        False,
        "active_replan_failed",
    )
    assert refresh_failed.store.data["production"]["armed"] is False

    active_unsafe = _startup_recovery_test_coordinator()
    active_unsafe.async_restore_safe_state = AsyncMock(
        side_effect=(SimpleNamespace(result=OutcomeResult.RESTORED), RuntimeError("second restore failed"))
    )
    active_unsafe.async_refresh = AsyncMock()

    def active_unsafe_preflight(hass: object, item: EnergyPlannerCoordinator) -> dict[str, object]:
        report = _startup_recovery_report(item)
        if item.store.data.get("production", {}).get("armed") is True:
            report["active_control_ready"] = False
        return report

    monkeypatch.setattr(coordinator_module, "build_preflight_report", active_unsafe_preflight)
    assert asyncio.run(active_unsafe._async_reactivate_after_startup_recovery(3)) == (
        False,
        "active_replan_unsafe",
    )
    assert active_unsafe.store.data["production"]["armed"] is False


def test_startup_recovery_started_metadata_and_policy_change_lifecycle(monkeypatch: object) -> None:
    coordinator = _startup_recovery_test_coordinator()
    coordinator.store.data["production"]["startup_auto_recovery"] = {"completed_at": "old"}
    asyncio.run(
        coordinator._async_update_startup_auto_recovery(
            "grace",
            successful_runs=0,
            reason="started",
            started=True,
        )
    )
    recovery = coordinator.store.data["production"]["startup_auto_recovery"]
    assert recovery["started_at"]
    assert recovery["deadline"]
    assert "completed_at" not in recovery

    policy = _coordinator_for_runtime_services(
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_PLANNING_INTERVAL_MINUTES: 15,
        },
        store_data={"production": {"armed": True}},
    )
    policy._last_handled_options = dict(policy.entry.options)
    policy.entry.options = {**policy.entry.options, CONF_PLANNING_INTERVAL_MINUTES: 20}
    policy._last_control_mode_state = (True, False)
    policy._startup_auto_recovery_authorized = False
    policy._startup_auto_recovery_task = None
    policy._startup_auto_recovery_start_unsub = None
    monkeypatch.setattr(policy, "async_start_startup_auto_recovery", lambda: None)

    asyncio.run(policy.async_handle_options_update())

    assert policy.store.data["production"]["armed"] is False
    assert policy.executor.restored == ["configuration_changed"]
    assert policy.store.data["production"]["startup_auto_recovery"]["status"] == (
        "waiting_for_home_assistant"
    )

    recovering = _coordinator_for_runtime_services(
        options={
            CONF_PLANNER_ENABLED: True,
            CONF_DRY_RUN: False,
            CONF_EV_CONTROL_ENABLED: True,
            CONF_PLANNING_INTERVAL_MINUTES: 15,
        },
        store_data={
            "production": {
                "armed": False,
                "startup_auto_recovery": {
                    "status": "waiting_for_safe",
                    "successful_runs": 2,
                },
            }
        },
    )
    recovering._last_handled_options = dict(recovering.entry.options)
    recovering.entry.options = {
        **recovering.entry.options,
        CONF_PLANNING_INTERVAL_MINUTES: 20,
    }
    recovering._last_control_mode_state = (True, False)
    recovering._startup_auto_recovery_authorized = True
    recovering._startup_auto_recovery_task = None
    recovering._startup_auto_recovery_start_unsub = None
    recovery_starts: list[bool] = []
    monkeypatch.setattr(
        recovering,
        "async_start_startup_auto_recovery",
        lambda: recovery_starts.append(True),
    )

    asyncio.run(recovering.async_handle_options_update())

    recovery = recovering.store.data["production"]["startup_auto_recovery"]
    assert recovery["status"] == "waiting_for_home_assistant"
    assert recovery["successful_runs"] == 0
    assert recovering._startup_auto_recovery_authorized is True
    assert recovery_starts == [True]


def test_production_evidence_rejects_malformed_counters_and_saturates() -> None:
    dry_run = _plan("dry-run")
    dry_run.mode = PlannerMode.DRY_RUN

    for corrupt in ("3", True, 3.0, -1, 10_001):
        coordinator = _coordinator_for_runtime_services(store_data={"production": {"dry_run_ready_cycles": corrupt}})
        asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
        assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 1

    coordinator = _coordinator_for_runtime_services()
    for _index in range(5):
        asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    assert coordinator.store.data["production"]["dry_run_ready_cycles"] == 3


def test_runtime_ready_by_does_not_change_production_evidence_contract() -> None:
    coordinator = _coordinator_for_runtime_services(
        options={CONF_DEFAULT_READY_BY: "07:00", "ev_control_enabled": True},
        entry_data={"ev_smart_charging_start_entity": "button.ev_start"},
    )
    dry_run = _plan("dry-run")
    dry_run.mode = PlannerMode.DRY_RUN

    asyncio.run(coordinator._async_update_production_evidence(dry_run, []))
    fingerprint = coordinator.store.data["production"]["dry_run_evidence_fingerprint"]
    coordinator.ready_by = "23:45"

    assert production_evidence_fingerprint(coordinator.entry_data, coordinator.planner_options) == fingerprint


def test_shutdown_cancels_advisory_work_but_preserves_inflight_execution() -> None:
    calls: list[str] = []
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator._debounce_cancel = lambda: calls.append("debounce")
    coordinator._boundary_cancel = lambda: calls.append("boundary")
    coordinator._ai_advice_task = SimpleNamespace(
        done=lambda: False,
        cancel=lambda: calls.append("ai"),
    )
    execution_task = SimpleNamespace(
        done=lambda: False,
        cancel=lambda: calls.append("execution"),
    )
    coordinator._plan_execution_task = execution_task
    coordinator._pending_plan_execution = (1, _plan("pending"), object(), {})
    coordinator._deferred_plan_execution = (1, _plan("deferred"), object(), {})
    coordinator._unsub_listeners = [
        lambda: calls.append("listener_1"),
        lambda: calls.append("listener_2"),
    ]

    coordinator.async_shutdown()

    assert calls == ["debounce", "boundary", "ai", "listener_2", "listener_1"]
    assert coordinator._debounce_cancel is None
    assert coordinator._boundary_cancel is None
    assert coordinator._ai_advice_task is None
    assert coordinator._plan_execution_task is execution_task
    assert coordinator._pending_plan_execution is None
    assert coordinator._deferred_plan_execution is None
    assert coordinator._unsub_listeners == []


def test_wait_for_plan_execution_reaches_safe_boundary_and_handles_failure() -> None:
    async def scenario() -> tuple[bool, bool]:
        release = asyncio.Event()

        async def complete_at_safe_boundary() -> None:
            await release.wait()

        coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
        coordinator._plan_execution_task = asyncio.create_task(complete_at_safe_boundary())
        waiter = asyncio.create_task(coordinator.async_wait_for_plan_execution())
        await asyncio.sleep(0)
        waiting_before_release = not waiter.done()
        release.set()
        await waiter

        async def fail() -> None:
            raise RuntimeError("background failure")

        coordinator._plan_execution_task = asyncio.create_task(fail())
        await coordinator.async_wait_for_plan_execution()
        failure_consumed = coordinator._plan_execution_task.done()
        coordinator._plan_execution_task = None
        await coordinator.async_wait_for_plan_execution()
        return waiting_before_release, failure_consumed

    waiting_before_release, failure_consumed = asyncio.run(scenario())

    assert waiting_before_release is True
    assert failure_consumed is True


def test_debounced_and_boundary_refresh_callbacks_schedule_refresh(monkeypatch: object) -> None:
    scheduled: list[tuple[float, object]] = []

    def fake_async_call_later(hass: object, delay: float, action: object) -> object:
        scheduled.append((delay, action))
        return lambda: scheduled.append((-1, "cancelled"))

    monkeypatch.setattr(
        "custom_components.ha_energy_planner.coordinator.async_call_later",
        fake_async_call_later,
    )
    coordinator = _coordinator_for_runtime_services()
    coordinator._debounce_cancel = lambda: scheduled.append((-2, "old_debounce_cancelled"))
    coordinator._boundary_cancel = lambda: scheduled.append((-3, "old_boundary_cancelled"))

    coordinator._schedule_debounced_refresh()
    debounce_callback = scheduled[-1][1]
    debounce_callback(None)
    coordinator._schedule_next_boundary_refresh()
    boundary_callback = scheduled[-1][1]
    boundary_callback(None)
    # Boundary scheduling bypasses the 20-second state debounce but still uses
    # the coalescing/minimum-interval callback.
    scheduled[-2][1](None)

    assert coordinator._refresh_generation == 2
    assert len(coordinator.hass.created_tasks) == 2
    assert scheduled[0] == (-2, "old_debounce_cancelled")
    assert scheduled[2] == (-3, "old_boundary_cancelled")


def _coordinator_for_commit(previous: EnergyPlan | None, *, current_generation: int) -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.store = FakeStore()
    coordinator.executor = FakeExecutor()
    coordinator.entry = FakeEntry({"battery_soc_entity": "sensor.battery"})
    coordinator.data = previous
    coordinator._refresh_generation = current_generation
    return coordinator


def _coordinated_action(
    plan: EnergyPlan,
    action_id: str,
    asset: ActionAsset,
    kind: ActionKind,
) -> PlanAction:
    """Return one due action for coordinator commit-order tests."""
    return PlanAction(
        action_id=action_id,
        plan_id=plan.plan_id,
        execute_not_before=plan.created_at,
        execute_not_after=plan.created_at + timedelta(minutes=5),
        asset=asset,
        kind=kind,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=1.0,
    )


def _coordinator_for_restore() -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.executor = FakeExecutor()
    coordinator._planner_lock = asyncio.Lock()
    coordinator._command_lock = asyncio.Lock()
    coordinator._device_control_lock = asyncio.Lock()
    coordinator._refresh_generation = 0
    coordinator.refresh_requested = 0

    async def request_refresh() -> None:
        coordinator.refresh_requested += 1

    coordinator.async_request_refresh = request_refresh
    return coordinator


def _coordinator_for_runtime_services(
    *,
    entry_data: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
    hass: FakeHass | None = None,
    store_data: dict[str, object] | None = None,
) -> EnergyPlannerCoordinator:
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.hass = hass or FakeHass()
    coordinator.entry = FakeEntry(entry_data or {}, options or {})
    coordinator.store = FakeStore(store_data or {})
    coordinator.executor = FakeExecutor()
    coordinator._planner_lock = asyncio.Lock()
    coordinator._command_lock = asyncio.Lock()
    coordinator._options_update_lock = asyncio.Lock()
    coordinator._device_control_lock = asyncio.Lock()
    coordinator._last_handled_options = dict(options or {})
    coordinator._last_control_mode_state = (coordinator.planner_enabled, coordinator.dry_run)
    coordinator.overrides = []
    coordinator.ready_by = "07:00"
    coordinator._refresh_generation = 0
    coordinator._listeners = {}
    coordinator.refresh_requested = 0
    coordinator._debounce_cancel = None
    coordinator._boundary_cancel = None
    coordinator._unsub_listeners = []

    async def request_refresh() -> None:
        coordinator.refresh_requested += 1

    coordinator.async_request_refresh = request_refresh
    return coordinator


def _startup_recovery_test_coordinator() -> EnergyPlannerCoordinator:
    coordinator = _coordinator_for_runtime_services(
        entry_data={CONF_EV_CHARGER: "switch.ev"},
        options={CONF_PLANNER_ENABLED: True, CONF_DRY_RUN: False, CONF_EV_CONTROL_ENABLED: True},
        store_data={"production": {"armed": False}},
    )
    coordinator._startup_auto_recovery_authorized = True
    coordinator._startup_auto_recovery_wakeup = asyncio.Event()
    coordinator._startup_auto_recovery_task = None
    return coordinator


def _startup_recovery_report(coordinator: EnergyPlannerCoordinator) -> dict[str, object]:
    production = coordinator.store.data.get("production", {})
    evidence_complete = production.get("dry_run_ready_cycles") == 3
    armed = production.get("armed") is True
    return {
        "entities": {"missing": [], "unavailable": []},
        "services": {"missing": [], "unavailable": []},
        "control_areas": {
            "required": ["ev"],
            "ready": ["ev"],
            "available": ["ev"],
            "confidence_eligible": ["ev"],
        },
        "discovery": {"ev": {"supported": True}},
        "recorder": {"available": True},
        "checks": [{"check": "control_not_paused", "ok": True}],
        "current_plan": {"safe": True},
        "safe_to_activate_now": evidence_complete,
        "active_control_ready": evidence_complete and armed,
    }


def _plan(plan_id: str) -> EnergyPlan:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return EnergyPlan(
        plan_id=plan_id,
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.ACTIVE_HEALTHY,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=None,
        actions=[],
        preview=[],
    )
