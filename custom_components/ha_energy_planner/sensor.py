"""Sensor platform for Energy Planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ai_advisor import ai_rejection_detail
from .const import (
    CONF_AI_AGENT_ID,
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CHARGING,
    CONF_EV_CONTROL_ENABLED,
    CONF_PERSON_ENTITIES,
)
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .models import ActionAsset, EnergyPlan, PlanAction, to_jsonable
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
        else _display_state(coordinator.data.next_action.kind),
        attrs_fn=lambda coordinator: {}
        if not coordinator.data or not coordinator.data.next_action
        else {"action": _compact_action(coordinator.data.next_action)},
    ),
    PlannerSensorDescription(
        key="plan_status",
        translation_key="plan_status",
        icon="mdi:clipboard-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: "Unknown" if not coordinator.data else _display_state(coordinator.data.status),
        attrs_fn=lambda coordinator: {}
        if not coordinator.data
        else to_jsonable(
            {
                "plan_id": coordinator.data.plan_id,
                "created_at": coordinator.data.created_at.isoformat(),
                "mode": coordinator.data.mode,
                "health": coordinator.data.health,
                "summary": coordinator.data.summary,
                "issues": coordinator.data.input_issues[:20],
                "preview": coordinator.data.preview[:12],
            }
        ),
    ),
    PlannerSensorDescription(
        key="estimated_daily_cost",
        translation_key="estimated_daily_cost",
        icon="mdi:cash",
        native_unit_of_measurement="AUD",
        value_fn=lambda coordinator: None if not coordinator.data else coordinator.data.estimated_daily_cost,
    ),
    PlannerSensorDescription(
        key="forecast_confidence",
        translation_key="forecast_confidence",
        icon="mdi:gauge",
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: None if not coordinator.data else round(coordinator.data.confidence * 100, 1),
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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        return self.entity_description.attrs_fn(self.coordinator)


def _asset_plan_state(plan: EnergyPlan | None, asset: ActionAsset) -> str:
    """Return concise state for an asset planning sensor."""
    if plan is None:
        return "Unknown"
    action = _first_asset_action(plan, asset)
    if action is None:
        return "Idle"
    return _display_state(action.kind)


def _asset_plan_attrs(plan: EnergyPlan | None, asset: ActionAsset) -> dict[str, Any]:
    """Return bounded action details for an asset planning sensor."""
    if plan is None:
        return {}
    actions = [action for action in plan.actions if action.asset == asset]
    device_plan = _device_plan_for_asset(plan, asset)
    timeline = device_plan.get("timeline", []) if isinstance(device_plan, dict) else []
    return {
        "plan_id": plan.plan_id,
        "mode": str(plan.mode),
        "health": str(plan.health),
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
        "current_state": device_plan.get("current_state") if isinstance(device_plan, dict) else None,
        "current_state_label": device_plan.get("current_state_label") if isinstance(device_plan, dict) else None,
        "next_planned_state": device_plan.get("next_planned_state") if isinstance(device_plan, dict) else None,
        "next_planned_state_label": device_plan.get("next_planned_state_label")
        if isinstance(device_plan, dict)
        else None,
        "planned_action_count": len(actions),
        "planned_actions": [_compact_action(action) for action in actions[:5]],
        "timeline_segment_count": len(timeline) if isinstance(timeline, list) else 0,
        "timeline": timeline if isinstance(timeline, list) else [],
        "issues": _asset_issues(plan, asset)[:10],
    }


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
        "mode": str(plan.mode),
        "health": str(plan.health),
        "state": _bounded_json(state),
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
    recommendations = coordinator.store.data.get("ai_recommendations", [])
    if isinstance(recommendations, list) and recommendations:
        status = recommendations[-1].get("status")
        return _display_state(status or "unknown")
    if not coordinator.options.get("ai_enabled", False):
        return "Disabled"
    return "No response"


def _ai_advice_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the latest bounded AI response details."""
    recommendations = coordinator.store.data.get("ai_recommendations", [])
    if not isinstance(recommendations, list) or not recommendations:
        return {
            "enabled": bool(coordinator.options.get("ai_enabled", False)),
            "latest": None,
        }
    latest = dict(recommendations[-1])
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
        "ai_agent_id": latest.get(CONF_AI_AGENT_ID),
        "rejected_reason": latest.get("rejected_reason"),
        "rejected_detail": _bounded_json(rejected_detail),
        "alerts": accepted.get("alerts", []),
        "reasoning_summary": accepted.get("reasoning_summary"),
        "confidence": accepted.get("confidence"),
        "suggested_precondition_lead_minutes": accepted.get("suggested_precondition_lead_minutes"),
        "suggested_forecast_buffer_percent": accepted.get("suggested_forecast_buffer_percent"),
        "suggested_takeover_savings_threshold": accepted.get("suggested_takeover_savings_threshold"),
        "accepted": _bounded_json(accepted),
    }


def _confidence_breakdown_state(coordinator: EnergyPlannerCoordinator) -> str:
    """Return compact confidence state."""
    if coordinator.data is None:
        return "Unknown"
    return f"{round(coordinator.data.confidence * 100, 1)}%"


def _confidence_breakdown_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return simple confidence contribution breakdown."""
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
    return {
        "plan_id": coordinator.data.plan_id,
        "overall_confidence": coordinator.data.confidence,
        "health": str(coordinator.data.health),
        "breakdown": breakdown,
    }


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
    dry_run_ready_cycles = int(production.get("dry_run_ready_cycles", 0) or 0)
    return {
        "armed": bool(production.get("armed", False)),
        "armed_at": production.get("armed_at"),
        "acknowledged_at": production.get("acknowledged_at"),
        "dry_run_ready_cycles": dry_run_ready_cycles,
        "last_dry_run_ready_at": production.get("last_dry_run_ready_at"),
        "ready_to_arm": dry_run_ready_cycles >= 3 and all(device_controls.values()),
        "device_controls": device_controls,
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


def _compact_action(action: PlanAction) -> dict[str, Any]:
    """Return bounded action metadata suitable for entity attributes."""
    return {
        "action_id": action.action_id,
        "plan_id": action.plan_id,
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


def _display_state(value: Any) -> str:
    """Return a short user-facing state string."""
    text = str(value or "unknown").strip().replace("_", " ")
    if not text:
        return "Unknown"
    words = []
    for word in text.split():
        upper = word.upper()
        words.append(upper if upper in {"AI", "EV", "HVAC", "SOC"} else word.capitalize())
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
