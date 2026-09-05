"""Sensor platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ai_advisor import ai_rejection_detail, ai_target_detail
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_BYPASS_SAFETY_GATES,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SOC,
    CONF_WEATHER,
)
from .coordinator import (
    STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES,
    EnergyPlannerCoordinator,
    _ai_recommendation_fingerprint,
    _material_plan_fingerprint,
)
from .discovery import CapabilityDiscovery
from .entity import EnergyPlannerEntity, recorder_safe_attributes
from .load_forecast import MIN_UPPER_COVERAGE
from .models import ActionAsset, EnergyPlan, InputHealth, PlanAction, to_jsonable
from .plan_presentation import (
    action_label,
    action_load_forecast_attrs,
    asset_name,
    bounded_json,
    built_in_load_forecast_attrs,
    decision_data_quality_attrs,
    display_state,
    latest_forecast_snapshot,
    plain_action,
    plain_reason,
)
from .safety import (
    parse_production_state,
    strict_bool,
)
from .type_defs import EnergyPlannerConfigEntry

# Read-only entities receive coordinator updates.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PlannerSensorDescription(SensorEntityDescription):
    """Sensor description."""

    value_fn: Callable[[EnergyPlannerCoordinator], Any]
    attrs_fn: Callable[[EnergyPlannerCoordinator], dict[str, Any]] = lambda coordinator: {}


SENSORS: tuple[PlannerSensorDescription, ...] = (
    PlannerSensorDescription(
        key="mode",
        translation_key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=["review", "recovery", "active"],
        value_fn=lambda coordinator: _mode_state(coordinator),
    ),
    PlannerSensorDescription(
        key="current_state",
        translation_key="system_current_state",
        value_fn=lambda coordinator: _controlled_state_summary(coordinator),
        attrs_fn=lambda coordinator: _controlled_state_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="next_actions",
        translation_key="next_actions",
        value_fn=lambda coordinator: _next_actions_state(coordinator),
        attrs_fn=lambda coordinator: _next_actions_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="load_forecast_coverage_score",
        translation_key="load_forecast_coverage_score",
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _load_forecast_coverage_score(coordinator),
        attrs_fn=lambda coordinator: _load_forecast_coverage_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="decision_summary",
        translation_key="decision_summary",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _decision_summary_state(coordinator),
        attrs_fn=lambda coordinator: _decision_summary_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="plan_health",
        translation_key="plan_health",
        device_class=SensorDeviceClass.ENUM,
        options=["healthy", "degraded", "unsafe"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _plan_health_state(coordinator),
        attrs_fn=lambda coordinator: _plan_health_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="current_load_forecast",
        translation_key="current_load_forecast",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _current_load_forecast_value(coordinator),
        attrs_fn=lambda coordinator: _current_load_forecast_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="planning_duration",
        translation_key="planning_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _planning_duration_value(coordinator),
        attrs_fn=lambda coordinator: _planning_duration_attrs(coordinator),
    ),
)


def _mode_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return the visible automatic-control lifecycle state."""
    production = parse_production_state(coordinator.store.data.get("production"))
    recovery = production.raw.get("startup_auto_recovery")
    recovery_status = str(recovery.get("status", "")) if isinstance(recovery, dict) else ""
    automatic_control_requested = bool(getattr(coordinator, "automatic_control_requested", coordinator.active_control))
    if (
        automatic_control_requested
        and getattr(getattr(coordinator, "hass", None), "state", None) == CoreState.running
        and recovery_status in STARTUP_AUTO_RECOVERY_ACTIVE_STATUSES
    ):
        return "recovery"
    return "active" if coordinator.effective_control else "review"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_entities(PlannerSensor(coordinator, description) for description in SENSORS)


class PlannerSensor(EnergyPlannerEntity, SensorEntity):
    """Planner sensor."""

    entity_description: PlannerSensorDescription

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        description: PlannerSensorDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return native value."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        return recorder_safe_attributes(self.entity_description.attrs_fn(self.coordinator))


def _decision_summary_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return concise accepted/planned and rejected decision counts."""
    plan = coordinator.data
    if plan is None:
        return "Unknown"
    planned = len(plan.actions)
    rejected = len(plan.rejected_actions) if isinstance(plan.rejected_actions, list) else 0
    return f"{planned} planned, {rejected} rejected"


def _decision_summary_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return bounded evidence explaining the current planning result."""
    plan = coordinator.data
    if plan is None:
        return {}
    audit = plan.decision_audit if isinstance(plan.decision_audit, dict) else {}
    rejected = plan.rejected_actions if isinstance(plan.rejected_actions, list) else []
    accepted = _accepted_decisions(plan)
    return {
        "plan_id": plan.plan_id,
        "plan_created_at": plan.created_at.isoformat(),
        "summary": audit.get("summary") or plan.summary,
        "policy_order": bounded_json(audit.get("policy_order", [])),
        "marginal_budget": bounded_json(audit.get("marginal_budget", {})),
        "planned_action_count": len(plan.actions),
        "planned_actions": [
            _action_with_determination(action, accepted, audit) for action in _ordered_actions(plan)[:12]
        ],
        "rejected_action_count": len(rejected),
        "rejected_actions": bounded_json([item for item in rejected if isinstance(item, dict)][:12]),
        "estimated_cost": plan.estimated_daily_cost,
        "estimated_cost_horizon_hours": plan.estimated_cost_horizon_hours,
    }


def _plan_health_state(coordinator: EnergyPlannerCoordinator) -> str | None:
    """Return the current input-health classification."""
    plan = coordinator.data
    if plan is None:
        return None
    health = str(plan.health)
    return health if health in {str(item) for item in InputHealth} else None


def _plan_health_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return actionable health and data-quality evidence."""
    plan = coordinator.data
    if plan is None:
        return {}
    issue_codes = [str(issue) for issue in plan.input_issues[:20]]
    return {
        "plan_id": plan.plan_id,
        "plan_created_at": plan.created_at.isoformat(),
        "plan_status": plan.status,
        "mode": str(plan.mode),
        "summary": plan.summary,
        "confidence_percent": round(plan.confidence * 100, 1),
        "issue_count": len(plan.input_issues),
        "issues": [{"code": issue, "description": plain_reason(issue)} for issue in issue_codes],
        "data_quality": decision_data_quality_attrs(coordinator),
    }


def _current_load_forecast_value(
    coordinator: EnergyPlannerCoordinator,
) -> float | None:
    """Return expected household load for the current planning interval."""
    return _finite_number(built_in_load_forecast_attrs(coordinator).get("first_expected_kw"))


def _current_load_forecast_attrs(
    coordinator: EnergyPlannerCoordinator,
) -> dict[str, Any]:
    """Return current expected and conservative load-forecast evidence."""
    latest = latest_forecast_snapshot(coordinator)
    model = built_in_load_forecast_attrs(coordinator)
    if not model:
        return {}
    coverage = _finite_number(model.get("forecast_coverage"))
    return {
        "plan_id": latest.get("plan_id"),
        "valid_at": to_jsonable(latest.get("created_at")),
        "forecast_interval_minutes": None if coordinator.data is None else coordinator.data.interval_minutes,
        "conservative_forecast_kw": _finite_number(model.get("first_upper_kw")),
        "forecast_horizon_coverage_percent": None if coverage is None else round(coverage * 100, 1),
        "model_status": model.get("status"),
        "model_age_hours": model.get("model_age_hours"),
        "trained_at": model.get("trained_at"),
        "source_entity_id": model.get("source_entity_id"),
        "live_source_status": model.get("live_source_status"),
        "recent_correction_factor": model.get("recent_correction_factor"),
        "current_correction_applied": model.get("current_correction_applied"),
        "fallback_applied": model.get("fallback_applied"),
        "update_reason": model.get("update_reason"),
        "quality_failures": model.get("quality_failures", []),
    }


def _planning_duration_value(coordinator: EnergyPlannerCoordinator) -> float | None:
    """Return the last completed coordinator refresh duration."""
    metrics = getattr(coordinator, "refresh_metrics", None)
    if isinstance(metrics, dict):
        duration = _finite_number(metrics.get("last_duration_ms"))
        if duration is not None:
            return duration
    latest = getattr(coordinator, "last_refresh_metadata", None)
    return _finite_number(latest.get("duration_ms")) if isinstance(latest, dict) else None


def _planning_duration_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return refresh throughput, trigger, and phase timing telemetry."""
    metrics = getattr(coordinator, "refresh_metrics", None)
    metrics = metrics if isinstance(metrics, dict) else {}
    latest = getattr(coordinator, "last_refresh_metadata", None)
    latest = latest if isinstance(latest, dict) else {}
    if not metrics and not latest:
        return {}
    counter_keys = (
        "requested",
        "completed",
        "succeeded",
        "failed",
        "coalesced",
        "fingerprint_skipped",
        "computed",
        "teardown_skipped",
    )
    return {
        "last_refresh_succeeded": latest.get("succeeded"),
        "last_completed_at": to_jsonable(latest.get("completed_at")),
        "last_trigger": metrics.get("last_trigger", latest.get("trigger")),
        "refreshes_last_hour": metrics.get("refreshes_last_hour"),
        "counters": {key: metrics[key] for key in counter_keys if key in metrics},
        "trigger_counts": bounded_json(metrics.get("trigger_counts", {})),
        "phase_durations_ms": bounded_json(metrics.get("phase_durations_ms", latest.get("phases", {}))),
    }


def _finite_number(value: Any) -> float | None:
    """Return a finite numeric value while rejecting booleans and corrupt data."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _controlled_state_summary(coordinator: EnergyPlannerCoordinator) -> str:
    """Return one concise state covering every configured controlled asset."""
    groups = _controlled_state_groups(coordinator)
    if not groups:
        return "No controls configured"
    return " | ".join(f"{group['asset']}: {group['state']}" for group in groups)[:255]


def _controlled_state_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return actual entity snapshots grouped by controlled asset."""
    plan = coordinator.data
    attrs: dict[str, Any] = {
        "plan_id": None if plan is None else plan.plan_id,
        "plan_created_at": None if plan is None else plan.created_at.isoformat(),
        "mode": "Unknown" if plan is None else display_state(plan.mode),
        "health": "Unknown" if plan is None else display_state(plan.health),
        "controlled_assets": _controlled_state_groups(coordinator),
    }
    if coordinator.entry_data.get(CONF_DAIKIN_CLIMATE):
        attrs["climate_capability"] = _climate_capability_attrs(coordinator)
    if coordinator.entry_data.get(CONF_WEATHER):
        attrs["weather_forecast"] = _weather_forecast_attrs(coordinator)
    load_forecast = built_in_load_forecast_attrs(coordinator)
    if load_forecast:
        attrs["load_forecast"] = load_forecast
    return {key: value for key, value in attrs.items() if value is not None}


def _controlled_state_groups(coordinator: EnergyPlannerCoordinator) -> list[dict[str, Any]]:
    """Return current state and live entities for each enabled actuator group."""
    entry_data = dict(coordinator.entry_data)
    plan = coordinator.data
    groups: list[dict[str, Any]] = []
    configured: tuple[tuple[ActionAsset, list[tuple[str, Any]]], ...] = (
        (
            ActionAsset.DAIKIN,
            [
                ("climate", entry_data.get(CONF_DAIKIN_CLIMATE)),
                ("zone", entry_data.get(CONF_CLIMATE_ZONES)),
                ("automation", entry_data.get(CONF_CLIMATE_AUTOMATIONS)),
            ],
        ),
        (
            ActionAsset.EV,
            [
                ("charger", entry_data.get(CONF_EV_CHARGER) or entry_data.get(CONF_EV_SMART_CHARGING)),
                (
                    "start command",
                    entry_data.get(CONF_EV_CHARGER_START) or entry_data.get(CONF_EV_SMART_CHARGING_START),
                ),
                ("stop command", entry_data.get(CONF_EV_CHARGER_STOP) or entry_data.get(CONF_EV_SMART_CHARGING_STOP)),
                ("charging feedback", entry_data.get(CONF_EV_CHARGING)),
                ("connection feedback", entry_data.get(CONF_EV_CONNECTED)),
                ("state of charge", entry_data.get(CONF_EV_SOC)),
            ],
        ),
        (ActionAsset.ENPHASE, [("profile", entry_data.get(CONF_ENPHASE_PROFILE))]),
    )
    for asset, configured_entities in configured:
        if not _asset_control_enabled(coordinator, asset):
            continue
        entity_rows = [
            _live_entity_snapshot(coordinator, entity_id, role)
            for role, value in configured_entities
            for entity_id in _entity_id_values(value)
        ]
        actuator_roles = {"climate", "charger", "start command", "stop command", "profile"}
        if not any(row["role"] in actuator_roles for row in entity_rows):
            continue
        if asset == ActionAsset.EV:
            state = _ev_current_charge_state(coordinator)
        else:
            state = _asset_current_state(plan, asset)
        groups.append(
            {
                "asset": asset_name(asset),
                "state": state,
                "entities": entity_rows[:16],
                "planner_owns_control": _asset_owned(coordinator.store.data, asset),
            }
        )
    return groups


def _asset_control_enabled(
    coordinator: EnergyPlannerCoordinator,
    asset: ActionAsset,
) -> bool:
    """Return whether the asset's device-control switch is enabled."""
    option_by_asset = {
        ActionAsset.DAIKIN: CONF_CLIMATE_CONTROL_ENABLED,
        ActionAsset.EV: CONF_EV_CONTROL_ENABLED,
        ActionAsset.ENPHASE: CONF_ENPHASE_CONTROL_ENABLED,
    }
    return strict_bool(coordinator.options.get(option_by_asset[asset]), default=False)


def _entity_id_values(value: Any) -> list[str]:
    """Return normalized entity IDs from scalar or multi-entity configuration."""
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def _live_entity_snapshot(
    coordinator: EnergyPlannerCoordinator,
    entity_id: str,
    role: str,
) -> dict[str, Any]:
    """Return a compact actual Home Assistant state snapshot."""
    states = getattr(getattr(coordinator, "hass", None), "states", None)
    state = states.get(entity_id) if states is not None else None
    attributes = dict(getattr(state, "attributes", {}) or {})
    details = {
        key: attributes[key]
        for key in (
            "current_temperature",
            "temperature",
            "target_temp_low",
            "target_temp_high",
            "hvac_action",
            "unit_of_measurement",
        )
        if attributes.get(key) is not None
    }
    result = {
        "entity_id": entity_id,
        "name": attributes.get("friendly_name", entity_id),
        "role": role,
        "state": "missing" if state is None else str(state.state),
        "details": bounded_json(details),
    }
    return result


def _asset_owned(store_data: dict[str, Any], asset: ActionAsset) -> bool:
    """Return whether persisted ownership currently belongs to one asset."""
    ownership = dict(store_data.get("ownership", {}))
    if asset == ActionAsset.DAIKIN:
        return bool(
            ownership.get("hvac_control")
            or ownership.get("climate_automations")
            or ownership.get("planner_takeover_started_at")
        )
    if asset == ActionAsset.EV:
        reservation = store_data.get("ev_grid_reservation", {})
        return bool(
            ownership.get("ev_smart_charging_state") or (isinstance(reservation, dict) and reservation.get("active"))
        )
    return bool(ownership.get("enphase_profile") or ownership.get("enphase_profile_changed_at"))


def _next_actions_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return the next planned state for every configured controlled asset."""
    plan = coordinator.data
    if plan is None:
        return "Unknown"
    asset_by_name = {asset_name(asset): asset for asset in ActionAsset}
    summaries = [
        f"{group['asset']}: {_next_asset_summary(plan, asset_by_name[group['asset']])}"
        for group in _controlled_state_groups(coordinator)
        if group["asset"] in asset_by_name
    ]
    if not summaries:
        return "No controls configured"
    return " | ".join(summaries)[:255]


def _next_asset_summary(plan: EnergyPlan, asset: ActionAsset) -> str:
    """Return a useful next state even when a legacy device plan lacks a label."""
    state = _asset_next_state(plan, asset)
    if state not in {"Unknown", "Idle"}:
        return state
    action = next((item for item in _ordered_actions(plan) if item.asset == asset), None)
    return "No planned change" if action is None else action_label(action)


def _next_actions_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return ordered actions with the evidence used to determine each one."""
    plan = coordinator.data
    if plan is None:
        return {"actions": []}
    audit = dict(plan.decision_audit or {})
    accepted = _accepted_decisions(plan)
    enabled_actions = [action for action in _ordered_actions(plan) if _asset_control_enabled(coordinator, action.asset)]
    actions = [_action_with_determination(action, accepted, audit) for action in enabled_actions[:12]]
    load_forecast = built_in_load_forecast_attrs(coordinator)
    for action in actions:
        determination = action.get("determination")
        if isinstance(determination, dict):
            action_forecast = action_load_forecast_attrs(
                coordinator,
                str(action.get("action_id", "")),
            )
            if action_forecast:
                determination["load_forecast"] = action_forecast
    result = {
        "plan_id": plan.plan_id,
        "plan_created_at": plan.created_at.isoformat(),
        "mode": display_state(plan.mode),
        "health": display_state(plan.health),
        "data_quality": decision_data_quality_attrs(coordinator),
        "decision_summary": audit.get("summary"),
        "policy_order": bounded_json(audit.get("policy_order", [])),
        "marginal_budget": bounded_json(audit.get("marginal_budget", {})),
        "load_forecast": load_forecast,
        "action_count": len(enabled_actions),
        "actions": actions,
        "ai_explanation": _ai_advice_attrs(coordinator),
    }
    if coordinator.entry_data.get(CONF_DAIKIN_CLIMATE):
        result["climate_capability"] = _climate_capability_attrs(coordinator)
    if coordinator.entry_data.get(CONF_WEATHER):
        result["weather_forecast"] = _weather_forecast_attrs(coordinator)
    return result


def _climate_capability_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return actionable rollback-target evidence for current and next views."""
    if coordinator.hass is None:
        return {
            "available": False,
            "issues": ["home_assistant_unavailable"],
            "main_target_unavailable": [],
            "zone_targets_unavailable": [],
            "synchronize_zone_temperatures": False,
        }
    evidence = (
        CapabilityDiscovery(
            coordinator.hass,
            coordinator.entry_data,
            coordinator.options,
        )
        .inspect()
        .hvac
    )
    return {
        "available": evidence.supported,
        "issues": list(evidence.issues),
        "main_target_unavailable": list(evidence.details.get("main_target_unavailable", [])),
        "zone_targets_unavailable": list(evidence.details.get("zone_targets_unavailable", [])),
        "synchronize_zone_temperatures": evidence.details.get("synchronize_zone_temperatures", False),
    }


def _weather_forecast_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the latest service/cache evidence even when the plan was unchanged."""
    bounded = bounded_json(dict(getattr(coordinator, "weather_forecast_diagnostics", {}) or {}))
    return dict(bounded) if isinstance(bounded, dict) else {}


def _ordered_actions(plan: EnergyPlan | None) -> list[PlanAction]:
    """Return plan actions in execution order."""
    return [] if plan is None else sorted(plan.actions, key=lambda action: action.execute_not_before)


def _action_with_determination(
    action: PlanAction,
    accepted: list[dict[str, Any]],
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Return one action with user-readable decision evidence."""
    accepted_row = next((item for item in accepted if item.get("action_id") == action.action_id), {})
    return {
        "action_id": action.action_id,
        "asset": asset_name(action.asset),
        "kind": str(action.kind),
        "start": action.execute_not_before.isoformat(),
        "end": action.execute_not_after.isoformat(),
        **plain_action(action),
        "determination": {
            "reason_codes": list(action.reason_codes),
            "reasons": [plain_reason(reason) for reason in action.reason_codes],
            "hard_constraints": list(action.hard_constraints),
            "constraints": [plain_reason(constraint) for constraint in action.hard_constraints],
            "accepted_decision": bounded_json(accepted_row),
            "policy_order": bounded_json(audit.get("policy_order", [])),
        },
    }


def _accepted_decisions(plan: EnergyPlan) -> list[dict[str, Any]]:
    """Return accepted decision rows from the plan audit."""
    accepted = dict(plan.decision_audit or {}).get("accepted", [])
    return [dict(item) for item in accepted if isinstance(item, dict)] if isinstance(accepted, list) else []


def _asset_current_state(plan: EnergyPlan | None, asset: ActionAsset) -> str:
    """Return the current state label for an asset plan."""
    if plan is None:
        return "Unknown"
    device_plan = _device_plan_for_asset(plan, asset)
    label = device_plan.get("current_state_label")
    if isinstance(label, str) and label.strip():
        return label
    current = _asset_timeline_state(device_plan, "current")
    return _timeline_state_label(current)


def _asset_next_state(plan: EnergyPlan | None, asset: ActionAsset) -> str:
    """Return the next planned state label for an asset plan."""
    if plan is None:
        return "Unknown"
    device_plan = _device_plan_for_asset(plan, asset)
    label = device_plan.get("next_planned_state_label")
    if isinstance(label, str) and label.strip():
        return label
    next_state = _asset_timeline_state(device_plan, "next")
    return _timeline_state_label(next_state)


def _asset_timeline_state(device_plan: dict[str, Any], kind: str) -> dict[str, Any]:
    """Return the current or next compressed timeline segment for a device plan."""
    timeline = device_plan.get("timeline", [])
    if not isinstance(timeline, list) or not timeline:
        return {"state": "unknown"}
    current = timeline[0] if isinstance(timeline[0], dict) else {"state": "unknown"}
    if kind == "current":
        return dict(current)
    for item in timeline[1:]:
        if not isinstance(item, dict):
            continue
        if item.get("state") != current.get("state") or _timeline_payload_without_times(
            item
        ) != _timeline_payload_without_times(current):
            return dict(item)
    return {"state": "idle"}


def _timeline_payload_without_times(item: dict[str, Any]) -> dict[str, Any]:
    """Return timeline payload without period timestamps."""
    return {key: value for key, value in item.items() if key not in {"start", "end"}}


def _timeline_state_label(state: dict[str, Any]) -> str:
    """Return a concise label for a timeline state."""
    state_text = display_state(state.get("state", "unknown"))
    if state_text == "Unknown":
        return state_text
    profile = state.get("profile")
    if profile:
        return f"{state_text}: {profile}"
    target_soc = state.get("target_soc_percent")
    if target_soc is not None:
        return f"{state_text} to {target_soc}%"
    if state.get("charge_kw") is not None:
        return f"{state_text} ({state['charge_kw']} kW)"
    if state.get("battery_charge_kw") is not None:
        return f"{state_text} ({state['battery_charge_kw']} kW)"
    if state.get("battery_discharge_kw") is not None:
        return f"{state_text} ({state['battery_discharge_kw']} kW)"
    if state.get("hvac_mode") and state_text not in {"Off", "Idle"}:
        return f"{state_text}: {display_state(state['hvac_mode'])}"
    return state_text


def _ev_current_charge_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return current EV charge state from live input or the active plan."""
    live_state = _configured_state_value(coordinator, CONF_EV_CHARGING)
    if live_state is not None:
        label = _charge_state_label_from_raw(live_state)
        if label is not None:
            return label
    if coordinator.data is None:
        return "Unknown"
    return _charge_timeline_state_label(
        _asset_timeline_state(_device_plan_for_asset(coordinator.data, ActionAsset.EV), "current")
    )


def _configured_entity_id(coordinator: EnergyPlannerCoordinator, config_key: str) -> str | None:
    """Return configured entity ID for a config key."""
    entry_data = getattr(coordinator, "entry_data", {}) or {}
    entity_id = entry_data.get(config_key)
    return str(entity_id) if entity_id else None


def _configured_state_value(coordinator: EnergyPlannerCoordinator, config_key: str) -> str | None:
    """Return the raw state for a configured entity."""
    entity_id = _configured_entity_id(coordinator, config_key)
    hass = getattr(coordinator, "hass", None)
    states = getattr(hass, "states", None)
    get_state = getattr(states, "get", None)
    if not entity_id or not callable(get_state):
        return None
    state = get_state(entity_id)
    if state is None:
        return None
    return str(getattr(state, "state", "") or "")


def _charge_state_label_from_raw(value: str) -> str | None:
    """Return a readable charge state from a live EV charging entity state."""
    text = value.strip().lower().replace(" ", "_")
    if text in {"on", "true", "1", "charging"}:
        return "Charging"
    if text in {"connected_not_charging", "fully_charged"}:
        return display_state(text)
    if text in {"off", "false", "0", "idle", "not_charging", "disconnected", "unplugged", "not_plugged_in"}:
        return "Not Charging"
    if text in {"unknown", "unavailable", ""}:
        return None
    return display_state(text)


def _charge_timeline_state_label(state: dict[str, Any]) -> str:
    """Return a readable charge state from an EV plan timeline segment."""
    raw_state = str(state.get("state", "unknown") or "unknown")
    if raw_state == "charging":
        target_soc = state.get("target_soc_percent")
        if target_soc is not None:
            return f"Charging to {target_soc}%"
        charge_kw = state.get("charge_kw")
        if charge_kw is not None:
            return f"Charging ({charge_kw} kW)"
        return "Charging"
    if raw_state == "idle":
        return "Not Charging"
    return _timeline_state_label(state)


def _ai_advice_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the latest bounded explain-or-troubleshoot result."""
    capability = _ai_capability_attrs(coordinator)
    current = _current_ai_recommendation(coordinator)
    if current is None:
        attributes: dict[str, Any] = {
            **capability,
            "result": None,
        }
        pending = _current_ai_pending(coordinator)
        if pending is not None:
            attributes.update(pending)
        return attributes
    latest = dict(current)
    accepted = latest.get("accepted")
    if not isinstance(accepted, dict):
        accepted = {}
    rejected_detail = latest.get("rejected_detail")
    if not isinstance(rejected_detail, dict):
        rejected_reason = latest.get("rejected_reason")
        rejected_detail = ai_rejection_detail(rejected_reason) if isinstance(rejected_reason, str) else {}
    current_plan_id = getattr(getattr(coordinator, "data", None), "plan_id", None)
    source_plan_id = latest.get("plan_id")
    result: dict[str, Any] = {
        **capability,
        "created_at": latest.get("created_at"),
        "plan_id": source_plan_id,
        "reused_for_current_plan": bool(source_plan_id and source_plan_id != current_plan_id),
        "status": latest.get("status"),
        "service_called": latest.get("service_called"),
        "ai_task_entity": latest.get(CONF_AI_TASK_ENTITY),
        "rejected_reason": latest.get("rejected_reason"),
        "rejected_detail": bounded_json(rejected_detail),
        "outcome": accepted.get("outcome"),
        "summary": accepted.get("summary"),
    }
    if accepted.get("outcome") == "action_required" and isinstance(accepted.get("affected_item"), str):
        result["recommended_action"] = {
            "affected_item": ai_target_detail(accepted["affected_item"], coordinator.entry_data, coordinator.options),
            "problem": accepted.get("problem"),
            "next_step": accepted.get("next_step"),
            "expected_benefit": accepted.get("expected_benefit"),
            "verification": accepted.get("verification"),
        }
    return result


def _ai_capability_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return effective AI provider availability from entity and service evidence."""
    task_entity = str(coordinator.entry_data.get(CONF_AI_TASK_ENTITY, "") or "").strip()
    if coordinator.hass is None:
        return {
            "configured": bool(task_entity),
            "available": bool(task_entity),
            "availability_reason": None if task_entity else "ai_task_entity_not_configured",
        }
    evidence = (
        CapabilityDiscovery(
            coordinator.hass,
            coordinator.entry_data,
            coordinator.options,
        )
        .inspect()
        .ai
    )
    return {
        "configured": bool(task_entity),
        "available": evidence.supported,
        "availability_reason": evidence.issues[0] if evidence.issues else None,
    }


def _current_ai_recommendation(coordinator: EnergyPlannerCoordinator) -> dict[str, Any] | None:
    """Return exact or materially equivalent advice for the safe current plan."""
    if not coordinator.entry_data.get(CONF_AI_TASK_ENTITY):
        return None
    plan = coordinator.data
    if plan is None or plan.health == InputHealth.UNSAFE or plan.status == "unsafe":
        return None
    fingerprint = _material_plan_fingerprint(plan)
    recommendations = coordinator.store.data.get("ai_recommendations", [])
    if not isinstance(recommendations, list):
        return None
    for item in reversed(recommendations):
        if not isinstance(item, dict):
            continue
        if item.get("plan_id") == plan.plan_id and item.get("plan_fingerprint") == fingerprint:
            return item
    latest = recommendations[-1] if recommendations else None
    if _ai_recommendation_fingerprint(latest) == fingerprint:
        return latest if isinstance(latest, dict) else None
    return None


def _current_ai_pending(coordinator: EnergyPlannerCoordinator) -> dict[str, Any] | None:
    """Return pending metadata only when it belongs to the safe current plan."""
    if not coordinator.entry_data.get(CONF_AI_TASK_ENTITY):
        return None
    plan = coordinator.data
    if plan is None or plan.health == InputHealth.UNSAFE or plan.status == "unsafe":
        return None
    fingerprint = _material_plan_fingerprint(plan)
    if getattr(coordinator, "_ai_advice_pending_fingerprint", None) != fingerprint:
        return None
    return {
        "pending_reason": getattr(coordinator, "_ai_advice_pending_reason", None) or "request_in_flight",
    }


def _load_forecast_coverage_score(
    coordinator: EnergyPlannerCoordinator,
) -> float | None:
    """Return the current conservative-bound holdout coverage percentage."""
    model = coordinator.store.data.get("built_in_load_forecast", {})
    score, _source, _evaluated_at = _load_forecast_coverage_details(model)
    return score


def _load_forecast_coverage_details(
    model: Any,
) -> tuple[float | None, str, Any]:
    """Return the latest evaluated coverage score and its provenance."""
    if not isinstance(model, dict):
        return None, "unavailable", None
    latest_validation = model.get("last_training_validation", {})
    latest_score = _coverage_percent(latest_validation)
    if latest_score is not None:
        return latest_score, "latest_training_attempt", model.get("last_attempt_at")
    active_score = _coverage_percent(model.get("validation", {}))
    if active_score is not None:
        return active_score, "active_model", model.get("trained_at")
    return None, "unavailable", model.get("last_attempt_at") or model.get("trained_at")


def _coverage_percent(validation: Any) -> float | None:
    """Normalize a model validation coverage fraction to a percentage."""
    coverage = validation.get("upper_coverage") if isinstance(validation, dict) else None
    if not isinstance(coverage, int | float) or isinstance(coverage, bool):
        return None
    if not 0 <= float(coverage) <= 1:
        return None
    return round(float(coverage) * 100, 1)


def _load_forecast_coverage_attrs(
    coordinator: EnergyPlannerCoordinator,
) -> dict[str, Any]:
    """Return the safety threshold and explicit bypass state."""
    model = coordinator.store.data.get("built_in_load_forecast", {})
    model = model if isinstance(model, dict) else {}
    score, score_source, score_evaluated_at = _load_forecast_coverage_details(model)
    active_model_score = _coverage_percent(model.get("validation", {}))
    bypass_enabled = strict_bool(
        coordinator.options.get(CONF_BYPASS_SAFETY_GATES),
        default=False,
    )
    return {
        "required_threshold_percent": MIN_UPPER_COVERAGE * 100,
        "meets_threshold": None if score is None else score >= MIN_UPPER_COVERAGE * 100,
        "score_source": score_source,
        "score_evaluated_at": score_evaluated_at,
        "active_model_score_percent": active_model_score,
        "bypass_enabled": bypass_enabled,
        "bypass_applied_to_model": model.get("safety_gates_bypassed") is True,
        "model_status": model.get("status", "unknown"),
        "quality_failures": list(model.get("quality_failures", []))[:8]
        if isinstance(model.get("quality_failures"), list)
        else [],
    }


def _device_plan_for_asset(plan: EnergyPlan, asset: ActionAsset) -> dict[str, Any]:
    """Return the stored 24-hour device plan for an asset."""
    key_by_asset = {
        ActionAsset.DAIKIN: "climate",
        ActionAsset.ENPHASE: "enphase",
        ActionAsset.EV: "ev",
    }
    device_plan = plan.device_plans.get(key_by_asset[asset], {})
    return device_plan if isinstance(device_plan, dict) else {}
