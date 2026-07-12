"""Sensor platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ai_advisor import ai_rejection_detail
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CHARGING,
    CONF_EV_CONTROL_ENABLED,
    CONF_PERSON_ENTITIES,
)
from .coordinator import EnergyPlannerCoordinator, _material_plan_fingerprint
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .models import ActionAsset, ActionKind, EnergyPlan, InputHealth, PlanAction, to_jsonable
from .preflight import _control_area_report
from .type_defs import EnergyPlannerConfigEntry


@dataclass(frozen=True, kw_only=True)
class PlannerSensorDescription(SensorEntityDescription):
    """Sensor description."""

    value_fn: Callable[[EnergyPlannerCoordinator], Any]
    attrs_fn: Callable[[EnergyPlannerCoordinator], dict[str, Any]] = lambda coordinator: {}


SENSORS: tuple[PlannerSensorDescription, ...] = (
    PlannerSensorDescription(
        key="next_action",
        translation_key="next_action",
        icon="mdi:gesture-tap-button",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: "None"
        if not coordinator.data or not coordinator.data.next_action
        else _action_label(coordinator.data.next_action),
        attrs_fn=lambda coordinator: {}
        if not coordinator.data or not coordinator.data.next_action
        else _plain_action(coordinator.data.next_action),
    ),
    PlannerSensorDescription(
        key="plan_status",
        translation_key="plan_status",
        icon="mdi:clipboard-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: "Unknown" if not coordinator.data else _display_state(coordinator.data.status),
        attrs_fn=lambda coordinator: _plan_status_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="estimated_daily_cost",
        translation_key="estimated_daily_cost",
        icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda coordinator: None if not coordinator.data else coordinator.data.estimated_daily_cost,
        attrs_fn=lambda coordinator: {}
        if not coordinator.data
        else {"cost_horizon_hours": coordinator.data.estimated_cost_horizon_hours},
    ),
    PlannerSensorDescription(
        key="forecast_confidence",
        translation_key="forecast_confidence",
        icon="mdi:gauge",
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: None if not coordinator.data else round(coordinator.data.confidence * 100, 1),
        attrs_fn=lambda coordinator: _forecast_calibration_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="confidence_breakdown",
        translation_key="confidence_breakdown",
        icon="mdi:gauge-full",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _confidence_breakdown_state(coordinator),
        attrs_fn=lambda coordinator: _confidence_breakdown_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="decision_audit",
        translation_key="decision_audit",
        icon="mdi:clipboard-search-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _decision_audit_state(coordinator.data),
        attrs_fn=lambda coordinator: _decision_audit_attrs(coordinator.data),
    ),
    PlannerSensorDescription(
        key="rejected_actions",
        translation_key="rejected_actions",
        icon="mdi:clipboard-remove-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _rejected_actions_state(coordinator.data),
        attrs_fn=lambda coordinator: _rejected_actions_attrs(coordinator.data),
    ),
    PlannerSensorDescription(
        key="upcoming_timeline",
        translation_key="upcoming_timeline",
        icon="mdi:timeline-clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _upcoming_timeline_state(coordinator.data),
        attrs_fn=lambda coordinator: _upcoming_timeline_attrs(coordinator.data),
    ),
    PlannerSensorDescription(
        key="production_readiness",
        translation_key="production_readiness",
        icon="mdi:shield-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _production_readiness_state(coordinator),
        attrs_fn=lambda coordinator: _production_readiness_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="control_block_reason",
        translation_key="control_block_reason",
        icon="mdi:shield-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _control_block_state(coordinator),
        attrs_fn=lambda coordinator: _control_block_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="execution_audit",
        translation_key="execution_audit",
        icon="mdi:clipboard-text-clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _execution_audit_state(coordinator),
        attrs_fn=lambda coordinator: _execution_audit_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="dry_run_comparison",
        translation_key="dry_run_comparison",
        icon="mdi:compare-horizontal",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _dry_run_comparison_state(coordinator),
        attrs_fn=lambda coordinator: _dry_run_comparison_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="support_bundle_summary",
        translation_key="support_bundle_summary",
        icon="mdi:package-variant-closed-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _support_bundle_state(coordinator),
        attrs_fn=lambda coordinator: _support_bundle_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="ai_advice",
        translation_key="ai_advice",
        icon="mdi:robot-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _ai_advice_state(coordinator),
        attrs_fn=lambda coordinator: _ai_advice_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="climate_plan",
        translation_key="climate_plan",
        icon="mdi:thermostat",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_plan_state(coordinator.data, ActionAsset.DAIKIN),
        attrs_fn=lambda coordinator: _asset_plan_attrs(coordinator.data, ActionAsset.DAIKIN),
    ),
    PlannerSensorDescription(
        key="climate_decision",
        translation_key="decision",
        icon="mdi:thermostat-cog",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _device_decision_state(coordinator.data, ActionAsset.DAIKIN),
        attrs_fn=lambda coordinator: _device_decision_attrs(coordinator.data, ActionAsset.DAIKIN),
    ),
    PlannerSensorDescription(
        key="climate_current_state",
        translation_key="current_state",
        icon="mdi:thermostat",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_current_state(coordinator.data, ActionAsset.DAIKIN),
        attrs_fn=lambda coordinator: _asset_state_attrs(coordinator.data, ActionAsset.DAIKIN, "current"),
    ),
    PlannerSensorDescription(
        key="climate_next_state",
        translation_key="next_state",
        icon="mdi:thermostat-auto",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_next_state(coordinator.data, ActionAsset.DAIKIN),
        attrs_fn=lambda coordinator: _asset_state_attrs(coordinator.data, ActionAsset.DAIKIN, "next"),
    ),
    PlannerSensorDescription(
        key="presence_state",
        translation_key="presence_state",
        icon="mdi:account-group",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _presence_state(coordinator.data),
        attrs_fn=lambda coordinator: _presence_attrs(coordinator),
    ),
    PlannerSensorDescription(
        key="enphase_plan",
        translation_key="enphase_plan",
        icon="mdi:solar-power-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_plan_state(coordinator.data, ActionAsset.ENPHASE),
        attrs_fn=lambda coordinator: _asset_plan_attrs(coordinator.data, ActionAsset.ENPHASE),
    ),
    PlannerSensorDescription(
        key="enphase_decision",
        translation_key="decision",
        icon="mdi:home-battery",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _device_decision_state(coordinator.data, ActionAsset.ENPHASE),
        attrs_fn=lambda coordinator: _device_decision_attrs(coordinator.data, ActionAsset.ENPHASE),
    ),
    PlannerSensorDescription(
        key="enphase_current_state",
        translation_key="current_state",
        icon="mdi:home-battery-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_current_state(coordinator.data, ActionAsset.ENPHASE),
        attrs_fn=lambda coordinator: _asset_state_attrs(coordinator.data, ActionAsset.ENPHASE, "current"),
    ),
    PlannerSensorDescription(
        key="enphase_next_state",
        translation_key="next_state",
        icon="mdi:battery-clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_next_state(coordinator.data, ActionAsset.ENPHASE),
        attrs_fn=lambda coordinator: _asset_state_attrs(coordinator.data, ActionAsset.ENPHASE, "next"),
    ),
    PlannerSensorDescription(
        key="ev_charging_plan",
        translation_key="ev_charging_plan",
        icon="mdi:ev-station",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_plan_state(coordinator.data, ActionAsset.EV),
        attrs_fn=lambda coordinator: _ev_plan_attrs(coordinator.data, coordinator.store.data),
    ),
    PlannerSensorDescription(
        key="ev_decision",
        translation_key="decision",
        icon="mdi:ev-station",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _device_decision_state(coordinator.data, ActionAsset.EV),
        attrs_fn=lambda coordinator: _device_decision_attrs(coordinator.data, ActionAsset.EV),
    ),
    PlannerSensorDescription(
        key="ev_current_state",
        translation_key="current_state",
        icon="mdi:ev-plug-type2",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_current_state(coordinator.data, ActionAsset.EV),
        attrs_fn=lambda coordinator: _asset_state_attrs(coordinator.data, ActionAsset.EV, "current"),
    ),
    PlannerSensorDescription(
        key="ev_next_state",
        translation_key="next_state",
        icon="mdi:ev-station",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _asset_next_state(coordinator.data, ActionAsset.EV),
        attrs_fn=lambda coordinator: _asset_state_attrs(coordinator.data, ActionAsset.EV, "next"),
    ),
    PlannerSensorDescription(
        key="ev_current_charge_state",
        translation_key="current_charge_state",
        icon="mdi:ev-plug-type2",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _ev_current_charge_state(coordinator),
        attrs_fn=lambda coordinator: _ev_charge_state_attrs(coordinator, "current"),
    ),
    PlannerSensorDescription(
        key="ev_next_charge_state",
        translation_key="next_charge_state",
        icon="mdi:ev-station",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _ev_next_charge_state(coordinator.data),
        attrs_fn=lambda coordinator: _ev_charge_state_attrs(coordinator, "next"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(
        entry, async_add_entities, (PlannerSensor(coordinator, description) for description in SENSORS)
    )


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
    def native_unit_of_measurement(self) -> str | None:
        """Use Home Assistant's configured currency for monetary forecasts."""
        if self.entity_description.key == "estimated_daily_cost":
            config = getattr(getattr(self.coordinator, "hass", None), "config", None)
            return getattr(config, "currency", None)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        return self.entity_description.attrs_fn(self.coordinator)


def _forecast_calibration_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Expose compact, bounded forecast learning and uncertainty telemetry."""
    model = coordinator.store.data.get("forecast_calibration", {})
    if not isinstance(model, dict):
        return {"calibration_enabled": False, "fields": {}}
    fields: dict[str, Any] = {}
    for field in ("pv_forecast_kw", "baseline_load_forecast_kw"):
        field_model = model.get(field, {})
        if not isinstance(field_model, dict):
            continue
        buckets = field_model.get("buckets", {})
        if not isinstance(buckets, dict):
            buckets = {}
        enabled = [
            (lead, bucket)
            for lead, bucket in buckets.items()
            if isinstance(bucket, dict) and bucket.get("enabled")
        ]
        uncertainty_enabled = [
            (lead, bucket)
            for lead, bucket in buckets.items()
            if isinstance(bucket, dict) and bucket.get("uncertainty_enabled")
        ]
        fields[field] = {
            "sample_count": field_model.get("sample_count", 0),
            "enabled_lead_buckets": len(enabled),
            "uncertainty_enabled_lead_buckets": len(uncertainty_enabled),
            "lead_buckets": {
                str(lead): {
                    "factor": bucket.get("factor"),
                    "lower_factor": bucket.get("lower_factor"),
                    "upper_factor": bucket.get("upper_factor"),
                    "holdout_sample_count": bucket.get("holdout_sample_count"),
                    "raw_abs_pct_error_sum": bucket.get("raw_abs_pct_error_sum"),
                    "calibrated_abs_pct_error_sum": bucket.get("calibrated_abs_pct_error_sum"),
                }
                for lead, bucket in enabled[:12]
            },
        }
    return {
        "calibration_enabled": any(
            item["enabled_lead_buckets"] or item["uncertainty_enabled_lead_buckets"]
            for item in fields.values()
        ),
        "fields": fields,
    }


def _plan_status_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return plan status with bounded refresh and HAEO performance telemetry."""
    if not coordinator.data:
        return {}
    latest_haeo = _latest_store_item(coordinator.store.data.get("haeo_runs"))
    return to_jsonable(
        {
            "plan_id": coordinator.data.plan_id,
            "created_at": coordinator.data.created_at.isoformat(),
            "mode": coordinator.data.mode,
            "health": coordinator.data.health,
            "summary": coordinator.data.summary,
            "issues": coordinator.data.input_issues[:20],
            "preview": coordinator.data.preview[:12],
            "refresh": getattr(coordinator, "last_refresh_metadata", None),
            "refresh_metrics": getattr(coordinator, "refresh_metrics", None),
            "haeo": latest_haeo,
        }
    )


def _latest_store_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list) or not value or not isinstance(value[-1], dict):
        return None
    return value[-1]


def _asset_plan_state(plan: EnergyPlan | None, asset: ActionAsset) -> str:
    """Return concise state for an asset planning sensor."""
    if plan is None:
        return "Unknown"
    action = _first_asset_action(plan, asset)
    if action is None:
        return "Idle"
    return _action_label(action)


def _decision_audit_state(plan: EnergyPlan | None) -> str:
    """Return concise decision audit state."""
    if plan is None:
        return "Unknown"
    accepted = _accepted_decisions(plan)
    return f"{len(accepted)} Accepted" if accepted else "No Actions"


def _decision_audit_attrs(plan: EnergyPlan | None) -> dict[str, Any]:
    """Return scored accepted decision evidence."""
    if plan is None:
        return {}
    audit = dict(plan.decision_audit or {})
    return {
        "plan_id": plan.plan_id,
        "summary": audit.get("summary"),
        "policy_order": audit.get("policy_order", []),
        "marginal_budget": _bounded_json(audit.get("marginal_budget", {})),
        "accepted": _bounded_json(audit.get("accepted", [])),
    }


def _rejected_actions_state(plan: EnergyPlan | None) -> str:
    """Return rejected decision count."""
    if plan is None:
        return "Unknown"
    count = len(plan.rejected_actions or [])
    return f"{count} Rejected" if count else "None"


def _rejected_actions_attrs(plan: EnergyPlan | None) -> dict[str, Any]:
    """Return rejected decision evidence."""
    if plan is None:
        return {}
    return {
        "plan_id": plan.plan_id,
        "rejected": _bounded_json(plan.rejected_actions or []),
    }


def _upcoming_timeline_state(plan: EnergyPlan | None) -> str:
    """Return upcoming timeline row count."""
    if plan is None:
        return "Unknown"
    count = len(plan.timeline_card or [])
    return f"{count} Upcoming" if count else "Idle"


def _upcoming_timeline_attrs(plan: EnergyPlan | None) -> dict[str, Any]:
    """Return dashboard-friendly timeline rows."""
    if plan is None:
        return {}
    return {
        "plan_id": plan.plan_id,
        "rows": _bounded_json(plan.timeline_card or []),
    }


def _device_decision_state(plan: EnergyPlan | None, asset: ActionAsset) -> str:
    """Return concise per-device decision state."""
    if plan is None:
        return "Unknown"
    accepted = _accepted_decision_for_asset(plan, asset)
    if accepted is not None:
        return "Accepted"
    rejected = _rejected_decision_for_asset(plan, asset)
    return "Rejected" if rejected is not None else "Not Considered"


def _device_decision_attrs(plan: EnergyPlan | None, asset: ActionAsset) -> dict[str, Any]:
    """Return why a device decision was accepted or rejected."""
    if plan is None:
        return {}
    accepted = _accepted_decision_for_asset(plan, asset)
    rejected = _rejected_decision_for_asset(plan, asset)
    return {
        "plan_id": plan.plan_id,
        "device": _asset_name(asset),
        "accepted": _bounded_json(accepted or {}),
        "rejected": _bounded_json(rejected or {}),
        "summary": _device_decision_summary(asset, accepted, rejected),
    }


def _accepted_decisions(plan: EnergyPlan) -> list[dict[str, Any]]:
    """Return accepted decision rows from the plan audit."""
    accepted = dict(plan.decision_audit or {}).get("accepted", [])
    return [dict(item) for item in accepted if isinstance(item, dict)] if isinstance(accepted, list) else []


def _accepted_decision_for_asset(plan: EnergyPlan, asset: ActionAsset) -> dict[str, Any] | None:
    """Return accepted decision for one asset."""
    name = _asset_name(asset)
    for item in _accepted_decisions(plan):
        if item.get("device") == name:
            return item
    return None


def _rejected_decision_for_asset(plan: EnergyPlan, asset: ActionAsset) -> dict[str, Any] | None:
    """Return rejected decision for one asset."""
    name = _asset_name(asset)
    for item in plan.rejected_actions or []:
        if isinstance(item, dict) and item.get("device") == name:
            return dict(item)
    return None


def _device_decision_summary(
    asset: ActionAsset,
    accepted: dict[str, Any] | None,
    rejected: dict[str, Any] | None,
) -> str:
    """Return plain-English per-device decision summary."""
    name = _asset_name(asset)
    if accepted:
        return f"{name} action was selected because {accepted.get('reason', 'it had the highest score')}."
    if rejected:
        return str(rejected.get("reason") or f"{name} action was considered but not selected.")
    return f"{name} was not considered in this planning run."


def _asset_plan_attrs(plan: EnergyPlan | None, asset: ActionAsset) -> dict[str, Any]:
    """Return bounded action details for an asset planning sensor."""
    if plan is None:
        return {}
    actions = [action for action in plan.actions if action.asset == asset]
    device_plan = _device_plan_for_asset(plan, asset)
    timeline = device_plan.get("timeline", []) if isinstance(device_plan, dict) else []
    attrs = {
        "plan_id": plan.plan_id,
        "mode": _display_state(plan.mode),
        "health": _display_state(plan.health),
        "horizon_hours": device_plan.get("horizon_hours", plan.horizon_hours)
        if isinstance(device_plan, dict)
        else plan.horizon_hours,
        "interval_minutes": device_plan.get("interval_minutes", plan.interval_minutes)
        if isinstance(device_plan, dict)
        else plan.interval_minutes,
        "total_estimated_energy_kwh": device_plan.get("total_estimated_energy_kwh")
        if isinstance(device_plan, dict)
        else None,
        "total_estimated_battery_charge_kwh": device_plan.get("total_estimated_battery_charge_kwh")
        if isinstance(device_plan, dict)
        else None,
        "total_estimated_battery_discharge_kwh": device_plan.get("total_estimated_battery_discharge_kwh")
        if isinstance(device_plan, dict)
        else None,
        "summary": _asset_plan_summary(plan, asset, actions, timeline),
        "planned_action_count": len(actions),
        "planned_actions": [_plain_action(action) for action in actions[:5]],
        "timeline_segment_count": len(timeline) if isinstance(timeline, list) else 0,
        "timeline_summary": _timeline_summary(timeline) if isinstance(timeline, list) else [],
        "issues": [_plain_reason(issue) for issue in _asset_issues(plan, asset)[:10]],
    }
    return {key: value for key, value in attrs.items() if value is not None}


def _asset_plan_summary(
    plan: EnergyPlan,
    asset: ActionAsset,
    actions: list[PlanAction],
    timeline: Any,
) -> str:
    """Return a plain-English summary for an asset planning sensor."""
    asset_name = _asset_name(asset)
    if actions:
        first_action = min(actions, key=lambda action: action.execute_not_before)
        return (
            f"{asset_name} has {len(actions)} planned action"
            f"{'' if len(actions) == 1 else 's'}. Next: {_action_sentence(first_action)}"
        )
    if isinstance(timeline, list) and timeline:
        return f"{asset_name} has no planned changes over the next {plan.horizon_hours:g} hours."
    return f"{asset_name} has no timeline available for the current plan."


def _timeline_summary(timeline: list[Any]) -> list[str]:
    """Return readable timeline segment summaries."""
    summaries: list[str] = []
    for item in timeline[:12]:
        if not isinstance(item, dict):
            continue
        label = _timeline_state_label(item)
        start = _time_label(item.get("start"))
        end = _time_label(item.get("end"))
        reason = _reason_summary(item.get("reason_codes"))
        period = f"{start}-{end}" if start and end else "Current period"
        summaries.append(f"{period}: {label}{f' because {reason}' if reason else ''}.")
    if len(timeline) > 12:
        summaries.append(f"{len(timeline) - 12} more segment(s) omitted.")
    return summaries


def _plain_action(action: PlanAction) -> dict[str, Any]:
    """Return action metadata in a user-readable shape."""
    attrs = {
        "action": _action_label(action),
        "decision": _action_sentence(action),
        "when": _action_window(action),
        "why": _reason_summary(action.reason_codes),
        "constraints": [_plain_reason(item) for item in action.hard_constraints[:8]],
        "desired_state": _plain_state_details(action.desired_state),
        "estimated_value": action.expected_cost_delta,
        "confidence": None if action.confidence is None else f"{round(action.confidence * 100, 1)}%",
        "requires_haeo_plan": bool(action.requires_haeo_plan_id),
    }
    return {key: value for key, value in attrs.items() if value not in (None, [], {})}


def _plain_state_details(state: dict[str, Any]) -> dict[str, Any]:
    """Return readable details for a current, planned, or desired state."""
    details: dict[str, Any] = {}
    for key, value in state.items():
        if value is None:
            continue
        if key in {"reason_codes", "issues"} and isinstance(value, list):
            details[_plain_key(key)] = [_plain_reason(item) for item in value]
        elif key in {"state", "action", "hvac_mode", "arbitrage_direction", "arbitrage_source"}:
            details[_plain_key(key)] = _display_state(value)
        elif key in {"start", "end", "execute_not_before", "execute_not_after"}:
            details[_plain_key(key)] = _time_label(value) or value
        elif key == "allocated_slots" and isinstance(value, list):
            details["Charging windows"] = len(value)
        else:
            details[_plain_key(key)] = _bounded_json(value)
    return details


def _action_sentence(action: PlanAction) -> str:
    """Return a one-sentence explanation of a planned action."""
    desired = action.desired_state
    if action.kind == ActionKind.SET_PROFILE:
        return f"Switch Enphase profile to {desired.get('profile', 'the selected profile')}."
    if action.kind == ActionKind.RESTORE_AI:
        return f"Restore Enphase to {desired.get('profile', 'the AI profile')}."
    if action.kind == ActionKind.SET_HVAC:
        mode = _display_state(desired.get("hvac_mode", "climate"))
        target = desired.get("target_temperature")
        if target is not None:
            return f"Set climate to {mode} at {target} C."
        return f"Set climate to {mode}."
    if action.kind == ActionKind.EV_SCHEDULE:
        target = desired.get("target_soc_percent")
        ready_by = desired.get("ready_by")
        target_text = f" to {target}%" if target is not None else ""
        ready_text = f" by {ready_by}" if ready_by else ""
        return f"Schedule EV charging{target_text}{ready_text}."
    return _action_label(action)


def _action_label(action: PlanAction) -> str:
    """Return a short user-facing action label."""
    labels = {
        ActionKind.SET_PROFILE: "Switch Enphase profile",
        ActionKind.RESTORE_AI: "Restore AI profile",
        ActionKind.SET_HVAC: "Change climate state",
        ActionKind.EV_START: "Start EV charging",
        ActionKind.EV_STOP: "Stop EV charging",
        ActionKind.EV_SCHEDULE: "Schedule EV charging",
    }
    return labels.get(action.kind, _display_state(action.kind))


def _action_window(action: PlanAction) -> str:
    """Return a concise action execution window."""
    start = _time_label(action.execute_not_before)
    end = _time_label(action.execute_not_after)
    return f"{start}-{end}" if start and end else "Next planning window"


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


def _asset_state_attrs(plan: EnergyPlan | None, asset: ActionAsset, kind: str) -> dict[str, Any]:
    """Return current or next state details for an asset plan."""
    if plan is None:
        return {}
    device_plan = _device_plan_for_asset(plan, asset)
    state = _asset_timeline_state(device_plan, kind)
    if kind == "current" and isinstance(device_plan.get("current_state"), dict):
        state = dict(device_plan["current_state"])
    if kind == "next" and isinstance(device_plan.get("next_planned_state"), dict):
        state = dict(device_plan["next_planned_state"])
    return {
        "plan_id": plan.plan_id,
        "mode": _display_state(plan.mode),
        "health": _display_state(plan.health),
        "summary": _asset_current_state(plan, asset) if kind == "current" else _asset_next_state(plan, asset),
        "details": _plain_state_details(state),
        "source": "current_state" if kind == "current" else "next_planned_state",
    }


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
    state_text = _display_state(state.get("state", "unknown"))
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
        return f"{state_text}: {_display_state(state['hvac_mode'])}"
    return state_text


def _ev_plan_attrs(plan: EnergyPlan | None, store_data: dict[str, Any]) -> dict[str, Any]:
    """Return EV plan details plus compact trip-history context."""
    attrs = _asset_plan_attrs(plan, ActionAsset.EV)
    trip_history = dict(store_data.get("trip_history", {}))
    records = trip_history.get("records")
    if isinstance(records, list):
        attrs["trip_history_record_count"] = len(records)
    summary = trip_history.get("summary")
    if isinstance(summary, dict):
        attrs["trip_history_summary"] = _bounded_json(summary)
    return attrs


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


def _ev_next_charge_state(plan: EnergyPlan | None) -> str:
    """Return next planned EV charge state."""
    if plan is None:
        return "Unknown"
    return _charge_timeline_state_label(_asset_timeline_state(_device_plan_for_asset(plan, ActionAsset.EV), "next"))


def _ev_charge_state_attrs(coordinator: EnergyPlannerCoordinator, kind: str) -> dict[str, Any]:
    """Return details for EV charge state sensors."""
    plan = coordinator.data
    attrs: dict[str, Any] = {
        "configured_charging_entity": _configured_entity_id(coordinator, CONF_EV_CHARGING),
        "live_state": _configured_state_value(coordinator, CONF_EV_CHARGING),
    }
    if plan is None:
        return attrs
    state = _asset_timeline_state(_device_plan_for_asset(plan, ActionAsset.EV), kind)
    attrs.update(
        {
            "plan_id": plan.plan_id,
            "mode": str(plan.mode),
            "health": str(plan.health),
            "planned_state": _bounded_json(state),
        }
    )
    return attrs


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
        return _display_state(text)
    if text in {"off", "false", "0", "idle", "not_charging", "disconnected", "unplugged", "not_plugged_in"}:
        return "Not Charging"
    if text in {"unknown", "unavailable", ""}:
        return None
    return _display_state(text)


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


def _presence_state(plan: EnergyPlan | None) -> str:
    """Return the inferred occupancy state used by the active plan."""
    if plan is None:
        return "Unknown"
    state = _presence_preview_value(plan)
    return _display_state(state)


def _presence_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return presence inputs and plan context."""
    configured = coordinator.entry_data.get(CONF_PERSON_ENTITIES, [])
    if isinstance(configured, str):
        configured_entities = [item.strip() for item in configured.split(",") if item.strip()]
    elif isinstance(configured, list):
        configured_entities = [str(item) for item in configured]
    else:
        configured_entities = []
    if coordinator.data is None:
        return {"person_entities": configured_entities}
    return {
        "plan_id": coordinator.data.plan_id,
        "occupancy_state": _presence_preview_value(coordinator.data),
        "person_entities": configured_entities,
        "preview": coordinator.data.preview[:12],
    }


def _presence_preview_value(plan: EnergyPlan) -> str:
    """Return the first occupancy marker from the plan preview."""
    for slot in plan.preview:
        if isinstance(slot, dict) and slot.get("occupied"):
            return str(slot["occupied"])
    return "unknown"


def _ai_advice_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return concise state for the latest AI advice run."""
    latest = _current_ai_recommendation(coordinator)
    if latest is not None:
        status = latest.get("status")
        return _display_state(status or "unknown")
    if not coordinator.options.get("ai_enabled", False):
        return "Disabled"
    return "No response"


def _ai_advice_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the latest bounded AI response details."""
    current = _current_ai_recommendation(coordinator)
    if current is None:
        return {
            "enabled": bool(coordinator.options.get("ai_enabled", False)),
            "latest": None,
        }
    latest = dict(current)
    accepted = latest.get("accepted")
    if not isinstance(accepted, dict):
        accepted = {}
    rejected_detail = latest.get("rejected_detail")
    if not isinstance(rejected_detail, dict):
        rejected_reason = latest.get("rejected_reason")
        rejected_detail = ai_rejection_detail(rejected_reason) if isinstance(rejected_reason, str) else {}
    return {
        "enabled": bool(coordinator.options.get("ai_enabled", False)),
        "created_at": latest.get("created_at"),
        "plan_id": latest.get("plan_id"),
        "status": latest.get("status"),
        "service_called": latest.get("service_called"),
        "ai_task_entity": latest.get(CONF_AI_TASK_ENTITY),
        "rejected_reason": latest.get("rejected_reason"),
        "rejected_detail": _bounded_json(rejected_detail),
        "alerts": accepted.get("alerts", []),
        "reasoning_summary": accepted.get("reasoning_summary"),
        "confidence": accepted.get("confidence"),
        "suggested_precondition_lead_minutes": accepted.get("suggested_precondition_lead_minutes"),
        "suggested_forecast_buffer_percent": accepted.get("suggested_forecast_buffer_percent"),
        "suggested_takeover_savings_threshold": accepted.get("suggested_takeover_savings_threshold"),
    }


def _current_ai_recommendation(coordinator: EnergyPlannerCoordinator) -> dict[str, Any] | None:
    """Return advice only when it belongs to the current safe committed plan."""
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
    return None


def _confidence_breakdown_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return compact confidence state."""
    if coordinator.data is None:
        return "Unknown"
    return f"{round(coordinator.data.confidence * 100, 1)}%"


def _confidence_breakdown_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return confidence calculation, component status, and improvement guidance."""
    if coordinator.data is None:
        return {}
    issues = list(coordinator.data.input_issues)
    groups = {
        "price": ("amber_import_price_", "amber_export_price_"),
        "pv": ("pv_forecast_",),
        "load": ("baseline_load_forecast_",),
        "battery": ("battery_soc_",),
        "ev": ("ev_",),
        "climate": ("daikin_", "climate_", "weather_"),
        "occupancy": ("person_", "occupancy_"),
        "haeo": ("haeo_",),
    }
    breakdown: dict[str, Any] = {}
    for name, prefixes in groups.items():
        matching = [issue for issue in issues if any(issue.startswith(prefix) for prefix in prefixes)]
        breakdown[name] = {
            "status": "degraded" if matching else "healthy",
            "issues": matching[:8],
        }
    health_score = _confidence_health_score(coordinator.data.health)
    forecast_confidence = _forecast_source_confidence(coordinator)
    calculation = {
        "formula": "overall = min(input_health_score, forecast_source_confidence)",
        "overall": coordinator.data.confidence,
        "overall_percent": round(coordinator.data.confidence * 100, 1),
        "input_health_score": health_score,
        "input_health_percent": round(health_score * 100, 1),
        "forecast_source_confidence": forecast_confidence,
        "forecast_source_percent": None if forecast_confidence is None else round(forecast_confidence * 100, 1),
        "limiting_factor": _confidence_limiting_factor(coordinator.data.confidence, health_score, forecast_confidence),
    }
    sources = _confidence_sources(coordinator)
    return {
        "plan_id": coordinator.data.plan_id,
        "overall_confidence": coordinator.data.confidence,
        "calculation": calculation,
        "subsystems": _bounded_json(coordinator.data.confidence_breakdown or {}),
        "health": str(coordinator.data.health),
        "breakdown": breakdown,
        "source_confidence": sources,
        "forecast_coverage": _forecast_coverage_sources(coordinator),
        "improvement_actions": _confidence_improvement_actions(
            coordinator.data.confidence,
            health_score,
            forecast_confidence,
            sources,
            breakdown,
        ),
    }


def _confidence_health_score(health: InputHealth | str) -> float:
    """Return confidence score contributed by input health."""
    if str(health) == InputHealth.HEALTHY:
        return 1.0
    if str(health) == InputHealth.DEGRADED:
        return 0.65
    return 0.0


def _forecast_source_confidence(coordinator: EnergyPlannerCoordinator) -> float | None:
    """Return the forecast-source confidence used by the current plan when known."""
    latest = _latest_forecast_snapshot(coordinator)
    confidence = latest.get("confidence") if isinstance(latest, dict) else None
    if isinstance(confidence, dict):
        value = confidence.get("forecast_source_confidence")
        if isinstance(value, int | float):
            return round(float(value), 4)
    if coordinator.data is None:
        return None
    health_score = _confidence_health_score(coordinator.data.health)
    if coordinator.data.confidence < health_score:
        return coordinator.data.confidence
    return None


def _confidence_sources(coordinator: EnergyPlannerCoordinator) -> list[dict[str, Any]]:
    """Return bounded source confidence evidence from the latest forecast snapshot."""
    latest = _latest_forecast_snapshot(coordinator)
    confidence = latest.get("confidence") if isinstance(latest, dict) else None
    sources = confidence.get("sources") if isinstance(confidence, dict) else []
    if not isinstance(sources, list):
        return []
    return [
        {
            "input": _confidence_source_label(source),
            "entity_id": source.get("entity_id"),
            "source": _display_state(source.get("source", "unknown")),
            "confidence": source.get("confidence"),
            "confidence_percent": round(float(source.get("confidence", 0.0) or 0.0) * 100, 1),
            "reason": _confidence_source_reason(source),
        }
        for source in sources[:12]
        if isinstance(source, dict)
    ]


def _forecast_coverage_sources(coordinator: EnergyPlannerCoordinator) -> list[dict[str, Any]]:
    """Return bounded per-input temporal coverage from the current snapshot."""
    latest = _latest_forecast_snapshot(coordinator)
    sources = latest.get("forecast_coverage") if isinstance(latest, dict) else []
    if not isinstance(sources, list):
        return []
    keys = (
        "config_key",
        "entity_id",
        "classification",
        "first_timestamp",
        "last_timestamp",
        "covered_hours",
        "continuous_hours",
        "longest_continuous_hours",
        "leading_missing_slots",
        "trailing_missing_slots",
        "internal_missing_slots",
        "leading_gap_filled_slots",
        "leading_gap_filled_hours",
    )
    return [
        {key: source.get(key) for key in keys if key in source} for source in sources[:12] if isinstance(source, dict)
    ]


def _latest_forecast_snapshot(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the most recent forecast snapshot for the current plan where possible."""
    snapshots = coordinator.store.data.get("forecast_snapshots", [])
    if not isinstance(snapshots, list):
        return {}
    plan_id = None if coordinator.data is None else coordinator.data.plan_id
    for item in reversed(snapshots):
        if isinstance(item, dict) and (plan_id is None or item.get("plan_id") == plan_id):
            return item
    return {}


def _confidence_limiting_factor(overall: float, health_score: float, forecast_confidence: float | None) -> str:
    """Return the factor currently limiting confidence."""
    if overall <= 0:
        return "unsafe_inputs"
    if forecast_confidence is None:
        return "input_health" if overall == health_score and overall < 1.0 else "unknown"
    health_limited = overall == health_score and health_score <= forecast_confidence
    forecast_limited = overall == forecast_confidence and forecast_confidence <= health_score
    if health_limited and forecast_limited:
        return "input_health_and_forecast_sources"
    if health_limited:
        return "input_health"
    if forecast_limited:
        return "forecast_sources"
    return "unknown"


def _confidence_improvement_actions(
    overall: float,
    health_score: float,
    forecast_confidence: float | None,
    sources: list[dict[str, Any]],
    breakdown: dict[str, Any],
) -> list[str]:
    """Return prioritized actions to improve plan confidence."""
    actions: list[str] = []
    if forecast_confidence is not None and forecast_confidence <= health_score and forecast_confidence < 1.0:
        limiting_sources = [source for source in sources if source.get("confidence") == forecast_confidence]
        for source in limiting_sources[:4]:
            if source.get("source") == "Point Value Repeated":
                actions.append(
                    f"Replace {source['input']} ({source.get('entity_id')}) with an entity that exposes forecast data "
                    "for the planning horizon, or add source confidence metadata."
                )
            elif source.get("source") == "Invalid State":
                actions.append(f"Fix {source['input']} ({source.get('entity_id')}) so it has a numeric usable state.")
            else:
                actions.append(
                    f"Improve {source['input']} ({source.get('entity_id')}) source confidence or data quality."
                )
    if overall == health_score and health_score < 1.0:
        for name, details in breakdown.items():
            issues = details.get("issues", []) if isinstance(details, dict) else []
            if issues:
                actions.append(f"Resolve {name} input issue(s): {', '.join(str(issue) for issue in issues[:3])}.")
    if not actions and overall < 1.0:
        actions.append(
            "Use forecast-capable entities with confidence metadata for price, PV, load, and weather inputs."
        )
    if not actions:
        actions.append("Confidence is already at 100%; no action is needed.")
    return actions[:8]


def _confidence_source_label(source: dict[str, Any]) -> str:
    """Return a readable configured input label."""
    labels = {
        "amber_import_price_entity": "Amber import price",
        "amber_export_price_entity": "Amber export price",
        "pv_forecast_entity": "PV forecast",
        "pv_forecast_secondary_entity": "Second PV forecast",
        "baseline_load_forecast_entity": "Baseline load forecast",
        "weather_entity": "Weather forecast",
    }
    config_key = str(source.get("config_key", "unknown"))
    return labels.get(config_key, config_key.replace("_", " ").capitalize())


def _confidence_source_reason(source: dict[str, Any]) -> str:
    """Return a readable reason for one confidence source score."""
    source_kind = source.get("source")
    if source_kind == "forecast_series":
        return "Forecast series found; confidence comes from entity metadata when present, otherwise 100%."
    if source_kind == "forecast_series_stitched":
        return "Timestamped forecast series were stitched, with the primary source taking precedence on overlap."
    if source_kind == "forecast_series_leading_fill":
        return (
            "A short leading load gap was conservatively filled from the current numeric state at reduced confidence."
        )
    if source_kind == "forecast_series_partial":
        return (
            "Forecast series coverage is shorter than the displayed planning horizon; coverage thresholds limit health."
        )
    if source_kind == "point_value_repeated":
        return "Only a current point value was found, so it is repeated across the planning horizon at 70% confidence."
    if source_kind == "point_value_only":
        return (
            "Only a current point value was found; required forecast coverage is unavailable and planning fails closed."
        )
    if source_kind == "invalid_state":
        return "The entity state could not be converted into usable forecast data."
    return "Confidence source was not classified."


def _production_readiness_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return production readiness state."""
    attrs = _production_readiness_attrs(coordinator)
    if attrs.get("armed"):
        return "Armed"
    if attrs.get("ready_to_arm"):
        return "Ready To Arm"
    return "Not Ready"


def _production_readiness_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return production gate attributes."""
    production = dict(coordinator.store.data.get("production", {}))
    device_controls = {
        "ev": bool(coordinator.options.get(CONF_EV_CONTROL_ENABLED, False)),
        "climate": bool(coordinator.options.get(CONF_CLIMATE_CONTROL_ENABLED, False)),
        "enphase": bool(coordinator.options.get(CONF_ENPHASE_CONTROL_ENABLED, False)),
    }
    control_areas = _control_area_report(dict(coordinator.entry_data), coordinator.options)
    required_areas = list(control_areas["required"])
    required_configured = all(control_areas["details"][area]["configured"] for area in required_areas)
    dry_run_ready_cycles = int(production.get("dry_run_ready_cycles", 0) or 0)
    return {
        "armed": bool(production.get("armed", False)),
        "armed_at": production.get("armed_at"),
        "acknowledged_at": production.get("acknowledged_at"),
        "dry_run_ready_cycles": dry_run_ready_cycles,
        "last_dry_run_ready_at": production.get("last_dry_run_ready_at"),
        "ready_to_arm": dry_run_ready_cycles >= 3 and bool(required_areas) and required_configured,
        "device_controls": device_controls,
        "required_control_areas": required_areas,
        "pause": _bounded_json(coordinator.store.data.get("control_pause", {})),
    }


def _control_block_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return the highest-signal reason active control is blocked."""
    attrs = _control_block_attrs(coordinator)
    return _display_state(attrs.get("reason") or "none")


def _control_block_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return active-control block details."""
    reasons: list[str] = []
    production = dict(coordinator.store.data.get("production", {}))
    pause = dict(coordinator.store.data.get("control_pause", {}))
    if not production.get("armed"):
        reasons.append("production_gate_not_armed")
    if _pause_active(pause):
        reasons.append("planner_paused")
    if not coordinator.options.get(CONF_EV_CONTROL_ENABLED, False):
        reasons.append("ev_control_disabled")
    if not coordinator.options.get(CONF_CLIMATE_CONTROL_ENABLED, False):
        reasons.append("climate_control_disabled")
    if not coordinator.options.get(CONF_ENPHASE_CONTROL_ENABLED, False):
        reasons.append("enphase_control_disabled")
    if coordinator.data and coordinator.data.input_issues:
        reasons.extend(coordinator.data.input_issues[:8])
    return {
        "reason": reasons[0] if reasons else "none",
        "reasons": reasons[:12],
        "armed": bool(production.get("armed", False)),
        "pause": _bounded_json(pause),
    }


def _execution_audit_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return latest audit outcome state."""
    entries = coordinator.store.data.get("execution_audit", [])
    if not isinstance(entries, list) or not entries:
        return "No Activity"
    latest = entries[-1]
    if not isinstance(latest, dict):
        return "Unknown"
    return _display_state(latest.get("result", "unknown"))


def _execution_audit_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return recent execution audit entries."""
    entries = coordinator.store.data.get("execution_audit", [])
    if not isinstance(entries, list):
        entries = []
    recent = [entry for entry in entries[-10:] if isinstance(entry, dict)]
    return {
        "outcome_count": len(entries),
        "latest": _bounded_json(recent[-1]) if recent else None,
        "recent": _bounded_json(recent),
    }


def _dry_run_comparison_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return latest dry-run comparison state."""
    comparisons = coordinator.store.data.get("dry_run_comparisons", [])
    if not isinstance(comparisons, list) or not comparisons:
        return "No Dry Run"
    latest = comparisons[-1]
    if not isinstance(latest, dict):
        return "Unknown"
    return f"{int(latest.get('planned_action_count', 0) or 0)} Planned"


def _dry_run_comparison_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return latest dry-run comparison details."""
    comparisons = coordinator.store.data.get("dry_run_comparisons", [])
    if not isinstance(comparisons, list) or not comparisons:
        return {}
    latest = comparisons[-1] if isinstance(comparisons[-1], dict) else {}
    return {
        "latest": _bounded_json(latest),
        "recent": _bounded_json([item for item in comparisons[-5:] if isinstance(item, dict)]),
    }


def _support_bundle_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return support bundle readiness summary."""
    if coordinator.data is None:
        return "No Plan"
    if coordinator.data.health.value == "unsafe":
        return "Needs Review"
    return "Ready"


def _support_bundle_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return compact support summary attributes."""
    return {
        "plan_id": None if coordinator.data is None else coordinator.data.plan_id,
        "production": _bounded_json(coordinator.store.data.get("production", {})),
        "pause": _bounded_json(coordinator.store.data.get("control_pause", {})),
        "latest_audit": _execution_audit_attrs(coordinator).get("latest"),
        "latest_ai": _ai_advice_attrs(coordinator),
        "support_service": "ha_energy_planner.export_support_bundle",
    }


def _pause_active(pause: dict[str, Any]) -> bool:
    """Return whether a stored pause looks active."""
    if not pause.get("active", bool(pause.get("until"))):
        return False
    return bool(pause.get("until"))


def _first_asset_action(plan: EnergyPlan, asset: ActionAsset) -> PlanAction | None:
    actions = [action for action in plan.actions if action.asset == asset]
    if not actions:
        return None
    return min(actions, key=lambda action: action.execute_not_before)


def _device_plan_for_asset(plan: EnergyPlan, asset: ActionAsset) -> dict[str, Any]:
    """Return the stored 24-hour device plan for an asset."""
    key_by_asset = {
        ActionAsset.DAIKIN: "climate",
        ActionAsset.ENPHASE: "enphase",
        ActionAsset.EV: "ev",
    }
    device_plan = plan.device_plans.get(key_by_asset[asset], {})
    return device_plan if isinstance(device_plan, dict) else {}


def _asset_issues(plan: EnergyPlan, asset: ActionAsset) -> list[str]:
    """Return input issues that are relevant to a device plan."""
    prefixes_by_asset = {
        ActionAsset.DAIKIN: (
            "daikin_",
            "climate_",
            "person_",
            "occupancy_",
            "weather_",
            "amber_import_price_",
        ),
        ActionAsset.ENPHASE: (
            "enphase_",
            "battery_soc_",
            "amber_import_price_",
            "amber_export_price_",
            "pv_forecast_",
            "baseline_load_forecast_",
            "haeo_",
        ),
        ActionAsset.EV: (
            "ev_",
            "amber_import_price_",
        ),
    }
    prefixes = prefixes_by_asset[asset]
    return [issue for issue in plan.input_issues if any(issue.startswith(prefix) for prefix in prefixes)]


def _asset_name(asset: ActionAsset) -> str:
    """Return a readable asset name."""
    names = {
        ActionAsset.DAIKIN: "Climate",
        ActionAsset.ENPHASE: "Enphase",
        ActionAsset.EV: "EV",
    }
    return names.get(asset, _display_state(asset))


def _reason_summary(reasons: Any) -> str:
    """Return a readable reason summary from one or more reason codes."""
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        return ""
    readable = [_plain_reason(reason) for reason in reasons[:3]]
    return "; ".join(reason for reason in readable if reason)


def _plain_reason(value: Any) -> str:
    """Return a plain-English explanation for an internal reason or issue code."""
    text = str(value or "").strip()
    labels = {
        "away_hvac_policy": "Nobody is home, so climate control can be reduced.",
        "occupied_comfort_within_bounds": "The home is occupied and temperature is already within the comfort range.",
        "manual_hvac_override_inactive": "No manual climate override is active.",
        "hvac_min_cycle": "The climate minimum cycle time is being respected.",
        "hvac_precondition_before_expensive_period": "Preconditioning before a more expensive electricity period.",
        "hvac_thermal_shift_before_expensive_period": (
            "Heating or cooling now because electricity is cheap and the home can coast through a later "
            "expensive period."
        ),
        "enphase_price_spread_above_threshold": "The forecast price spread is above the Enphase savings threshold.",
        "enphase_haeo_export_value_above_threshold": "HAEO expects export value above the Enphase savings threshold.",
        "enphase_haeo_battery_arbitrage_value_above_threshold": (
            "HAEO expects battery arbitrage value above the Enphase savings threshold."
        ),
        "enphase_forecast_solar_export_value_above_threshold": (
            "Forecast solar surplus value is above the Enphase savings threshold."
        ),
        "enphase_insufficient_arbitrage_evidence_below_threshold": (
            "There is not enough forecast battery or solar value to justify Enphase profile ownership."
        ),
        "enphase_arbitrage_below_threshold": "The expected Enphase value is below the configured savings threshold.",
        "ev_soc_below_target": "The EV battery is below the target state of charge.",
        "least_cost_slots_before_ready_by": "Charging was placed in the cheapest slots before the ready-by time.",
        "least_cost_solar_aware_slots_before_ready_by": (
            "Charging was placed in the lowest effective-cost slots, including forecast solar surplus."
        ),
        "configured_target": "The configured EV target state of charge is being used.",
        "history_max_daily_consumption": "Trip history raised the EV target to cover recent driving.",
        "battery_floor": "The battery reserve limit must be respected.",
        "enphase_min_savings": "The Enphase savings threshold must be met.",
        "enphase_profile_hold": "The Enphase profile hold period must be respected.",
        "ev_min_soc": "The EV minimum state of charge must be respected.",
        "ready_by": "The EV ready-by time must be respected.",
        "comfort": "The climate comfort range must be respected.",
    }
    if text in labels:
        return labels[text]
    return _display_state(text)


def _plain_key(value: Any) -> str:
    """Return a readable attribute key."""
    labels = {
        "reason_codes": "Reasons",
        "hvac_mode": "Climate mode",
        "target_temperature": "Target temperature C",
        "current_temperature": "Current temperature C",
        "current_power_kw": "Current power kW",
        "outdoor_temperature": "Outdoor temperature C",
        "occupied_temperature_low": "Comfort low C",
        "occupied_temperature_high": "Comfort high C",
        "target_soc_percent": "Target SOC percent",
        "ready_by": "Ready by",
        "arbitrage_value": "Estimated value",
        "arbitrage_source": "Value source",
        "arbitrage_direction": "Battery strategy",
        "execute_not_before": "Start",
        "execute_not_after": "End",
    }
    text = str(value)
    return labels.get(text, _display_state(text))


def _time_label(value: Any) -> str | None:
    """Return a readable local time label for an ISO timestamp or datetime."""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%H:%M")


def _display_state(value: Any) -> str:
    """Return a short user-facing state string."""
    text = str(value or "unknown").strip().replace("_", " ")
    if not text:
        return "Unknown"
    words = []
    for word in text.split():
        upper = word.upper()
        words.append(upper if upper in {"AI", "EV", "HVAC", "PV", "SOC"} else word.capitalize())
    return " ".join(words)


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Convert values to bounded JSON-friendly attributes."""
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
