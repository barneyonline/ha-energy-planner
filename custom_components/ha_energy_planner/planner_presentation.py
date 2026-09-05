"""Pure rendered plan summaries and timelines, with no command policy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from .models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    PlanAction,
)
from .planner_values import nonnegative_float_or_none as _positive_or_none


def _device_plan(
    context: DecisionContext,
    interval_minutes: int,
    entry_fn: Any,
    actions: list[PlanAction],
) -> dict[str, Any]:
    """Build a compressed timeline for one device over the planning horizon."""
    timeline: list[dict[str, Any]] = []
    interval_hours = interval_minutes / 60
    for slot in context.slots:
        slot_actions = [
            action for action in actions if action.execute_not_before <= slot.valid_at < action.execute_not_after
        ]
        entry = entry_fn(slot, slot_actions, actions)
        _add_energy_estimates(entry, interval_hours)
        entry["start"] = slot.valid_at.isoformat()
        entry["end"] = (slot.valid_at + timedelta(minutes=interval_minutes)).isoformat()
        _append_timeline_entry(timeline, entry)
    return {
        "generated_at": context.created_at.isoformat(),
        "horizon_hours": len(context.slots) * interval_minutes / 60,
        "interval_minutes": interval_minutes,
        "total_estimated_energy_kwh": _timeline_sum(timeline, "estimated_energy_kwh"),
        "timeline": timeline,
    }


def _climate_plan_summary(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return current and next planned climate state summaries."""
    current: dict[str, Any] = {
        "state": context.current_hvac_mode or "unknown",
        "hvac_mode": context.current_hvac_mode,
        "current_temperature": context.current_hvac_temperature_c,
        "current_power_kw": context.current_hvac_power_kw,
        "outdoor_temperature": context.current_outdoor_temperature_c,
        "occupied_temperature_low": context.occupied_temperature_low_c,
        "occupied_temperature_high": context.occupied_temperature_high_c,
        "occupancy": str(context.occupancy_state),
    }
    if context.climate_zone_entities:
        current["controlled_zones"] = list(context.climate_zone_entities)
    for key in (
        "phase",
        "period_start",
        "period_end",
        "precondition_end",
        "baseline_price",
        "precondition_min_price_delta",
        "suppression_min_price_delta",
        "mode",
        "precondition_target",
        "coast_target",
        "release_reason",
    ):
        if context.hvac_control.get(key) is not None:
            current[key] = context.hvac_control[key]
    next_action = min(actions, key=lambda action: action.execute_not_before) if actions else None
    if next_action is None:
        next_planned = {
            "state": "idle",
            "reason": "no_planned_climate_action",
        }
    else:
        next_planned = _climate_action_state(next_action)
    return {
        "current_state": current,
        "current_state_label": _climate_current_state_label(current),
        "next_planned_state": next_planned,
        "next_planned_state_label": _climate_next_state_label(next_planned),
    }


def _climate_action_state(action: PlanAction) -> dict[str, Any]:
    """Return compact desired state for a planned climate action."""
    desired = action.desired_state
    state = "set_hvac"
    if action.kind == ActionKind.RELEASE_HVAC:
        state = "released"
    if desired.get("phase"):
        state = str(desired["phase"])
    if desired.get("hvac_mode") == "off":
        state = "off"
    result: dict[str, Any] = {
        "state": state,
        "action": str(action.kind),
        "execute_not_before": action.execute_not_before.isoformat(),
        "execute_not_after": action.execute_not_after.isoformat(),
        "reason_codes": action.reason_codes[:4],
    }
    for key in (
        "hvac_mode",
        "target_temperature",
        "projected_hvac_load_kw",
        "suppress_automations",
        "phase",
        "period_start",
        "period_end",
        "precondition_end",
        "baseline_price",
        "precondition_min_price_delta",
        "suppression_min_price_delta",
        "precondition_target",
        "coast_target",
        "release_reason",
        "controlled_zones",
        "configured_zones_only",
    ):
        if desired.get(key) is not None:
            result[key] = desired.get(key)
    return result


def _climate_current_state_label(state: Mapping[str, Any]) -> str:
    """Return concise current climate state text."""
    mode = str(state.get("hvac_mode") or state.get("state") or "unknown")
    label = _display_text(mode)
    temperature = state.get("current_temperature")
    if temperature is not None:
        label = f"{label} ({temperature} C)"
    return label


def _climate_next_state_label(state: Mapping[str, Any]) -> str:
    """Return concise planned climate state text."""
    if state.get("state") == "idle":
        return "Idle"
    label = _display_text(state.get("state"))
    mode = state.get("hvac_mode")
    if mode and str(mode) != str(state.get("state")):
        label = f"{label}: {_display_text(mode)}"
    target = state.get("target_temperature")
    if target is not None:
        label = f"{label} to {target} C"
    return label


def _enphase_plan_summary(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return current and next planned Enphase state summaries."""
    current = {
        "state": context.current_enphase_profile or "unknown",
        "profile": context.current_enphase_profile,
        "ai_profile": context.enphase_ai_profile,
        "self_consumption_profile": context.enphase_self_consumption_profile,
        "full_backup_profile": context.enphase_full_backup_profile,
    }
    next_action = min(actions, key=lambda action: action.execute_not_before) if actions else None
    if next_action is None:
        next_planned = {
            "state": "idle",
            "profile": context.current_enphase_profile,
            "reason": "no_planned_enphase_action",
        }
    else:
        next_planned = _enphase_action_state(next_action)
    return {
        "current_state": current,
        "current_state_label": _enphase_current_state_label(current),
        "next_planned_state": next_planned,
        "next_planned_state_label": _enphase_next_state_label(next_planned),
    }


def _enphase_action_state(action: PlanAction) -> dict[str, Any]:
    """Return compact desired state for a planned Enphase action."""
    result: dict[str, Any] = {
        "state": str(action.kind),
        "action": str(action.kind),
        "execute_not_before": action.execute_not_before.isoformat(),
        "execute_not_after": action.execute_not_after.isoformat(),
        "reason_codes": action.reason_codes[:4],
    }
    desired = action.desired_state
    for key in ("profile", "arbitrage_direction", "arbitrage_source", "arbitrage_value"):
        if desired.get(key) is not None:
            result[key] = desired.get(key)
    return result


def _enphase_current_state_label(state: Mapping[str, Any]) -> str:
    """Return concise current Enphase profile text."""
    return str(state.get("profile") or _display_text(state.get("state")))


def _enphase_next_state_label(state: Mapping[str, Any]) -> str:
    """Return concise planned Enphase state text."""
    if state.get("state") == "idle":
        profile = state.get("profile")
        return f"Idle: {profile}" if profile else "Idle"
    label = _display_text(state.get("state"))
    profile = state.get("profile")
    if profile:
        label = f"{label}: {profile}"
    return label


def _display_text(value: Any) -> str:
    text = str(value or "unknown").replace("_", " ").strip()
    if not text:
        return "Unknown"
    words = []
    for word in text.split():
        upper = word.upper()
        words.append(upper if upper in {"AI", "EV", "HVAC"} else word.title())
    return " ".join(words)


def _append_timeline_entry(timeline: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """Append or merge a timeline entry with the previous segment."""
    if timeline and _timeline_payload(timeline[-1]) == _timeline_payload(entry):
        timeline[-1]["end"] = entry["end"]
        _merge_energy_estimates(timeline[-1], entry)
        return
    timeline.append(entry)


def _timeline_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entry.items()
        if key
        not in {
            "start",
            "end",
            "estimated_energy_kwh",
        }
    }


def _add_energy_estimates(entry: dict[str, Any], interval_hours: float) -> None:
    """Add per-slot kWh estimates from power values on a timeline entry."""
    if "projected_hvac_load_kw" in entry:
        entry["estimated_energy_kwh"] = _energy_kwh(entry["projected_hvac_load_kw"], interval_hours)
    if "charge_kw" in entry:
        entry["estimated_energy_kwh"] = _energy_kwh(entry["charge_kw"], interval_hours)


def _merge_energy_estimates(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Sum per-slot kWh values into a compressed timeline segment."""
    for key in ("estimated_energy_kwh",):
        if key in source:
            target[key] = round(float(target.get(key, 0.0) or 0.0) + float(source[key]), 4)


def _timeline_sum(timeline: list[dict[str, Any]], key: str) -> float | None:
    total = sum(float(entry.get(key, 0.0) or 0.0) for entry in timeline)
    return round(total, 4) if total > 0 else None


def _energy_kwh(power_kw: Any, interval_hours: float) -> float:
    return round(max(float(power_kw), 0.0) * interval_hours, 4)


def _climate_timeline_entry(slot: Any, slot_actions: list[PlanAction], actions: list[PlanAction]) -> dict[str, Any]:
    """Return the climate state for one timeline slot."""
    action = slot_actions[0] if slot_actions else None
    projected_load = _positive_or_none(slot.projected_hvac_load_kw)
    if action is not None:
        desired = action.desired_state
        entry: dict[str, Any] = {
            "state": "released" if action.kind == ActionKind.RELEASE_HVAC else "set_hvac",
            "action": str(action.kind),
            "reason_codes": action.reason_codes[:4],
        }
        if desired.get("suppress_automations"):
            entry["state"] = "suppressing_automation"
        if desired.get("phase"):
            entry["state"] = str(desired["phase"])
            entry["phase"] = desired["phase"]
        if desired.get("hvac_mode"):
            entry["hvac_mode"] = desired.get("hvac_mode")
            if desired.get("hvac_mode") == "off":
                entry["state"] = "off"
        if desired.get("target_temperature") is not None:
            entry["target_temperature"] = desired.get("target_temperature")
        for key in (
            "period_start",
            "period_end",
            "precondition_end",
            "baseline_price",
            "precondition_min_price_delta",
            "suppression_min_price_delta",
            "precondition_target",
            "coast_target",
            "controlled_zones",
            "release_reason",
        ):
            if desired.get(key) is not None:
                entry[key] = desired[key]
        if projected_load is not None and projected_load > 0:
            entry["projected_hvac_load_kw"] = round(projected_load, 4)
        return entry
    if projected_load is not None and projected_load > 0:
        related_action = actions[0] if actions else None
        projected_entry: dict[str, Any] = {
            "state": "preconditioning",
            "projected_hvac_load_kw": round(projected_load, 4),
        }
        if related_action is not None:
            projected_entry["reason_codes"] = related_action.reason_codes[:4]
            if related_action.desired_state.get("hvac_mode"):
                projected_entry["hvac_mode"] = related_action.desired_state.get("hvac_mode")
            if related_action.desired_state.get("target_temperature") is not None:
                projected_entry["target_temperature"] = related_action.desired_state.get("target_temperature")
        return projected_entry
    return {"state": "idle"}


def _enphase_timeline_entry(slot: Any, slot_actions: list[PlanAction], actions: list[PlanAction]) -> dict[str, Any]:
    """Return the Enphase state for one timeline slot."""
    planned_profile = _planned_enphase_profile(actions)
    action = slot_actions[0] if slot_actions else None
    if action is not None:
        entry = {
            "state": str(action.kind),
            "profile": action.desired_state.get("profile"),
            "reason_codes": action.reason_codes[:4],
        }
        if action.desired_state.get("arbitrage_direction"):
            entry["arbitrage_direction"] = action.desired_state.get("arbitrage_direction")
        if action.desired_state.get("arbitrage_value") is not None:
            entry["arbitrage_value"] = action.desired_state.get("arbitrage_value")
        return entry

    entry = {"state": "idle"}
    if planned_profile:
        entry["profile"] = planned_profile
    return entry


def _ev_timeline_entry(slot: Any, slot_actions: list[PlanAction], actions: list[PlanAction]) -> dict[str, Any]:
    """Return the EV state for one timeline slot."""
    action = actions[0] if actions else None
    charge_kw = _positive_or_none(slot.projected_ev_load_kw)
    if charge_kw is None or charge_kw <= 0:
        return {"state": "idle"}
    entry: dict[str, Any] = {
        "state": "charging",
        "charge_kw": round(charge_kw, 4),
    }
    if action is not None:
        desired = action.desired_state
        entry["reason_codes"] = action.reason_codes[:4]
        if desired.get("target_soc_percent") is not None:
            entry["target_soc_percent"] = desired.get("target_soc_percent")
        if desired.get("ready_by") is not None:
            entry["ready_by"] = desired.get("ready_by")
        if desired.get("infeasible") is not None:
            entry["infeasible"] = desired.get("infeasible")
    return entry


def _planned_enphase_profile(actions: list[PlanAction]) -> str | None:
    for action in actions:
        profile = action.desired_state.get("profile")
        if profile:
            return str(profile)
    return None


def _timeline_card_rows(device_plans: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return dashboard-friendly upcoming timeline rows."""
    rows: list[dict[str, Any]] = []
    for device_key, plan in device_plans.items():
        if not isinstance(plan, dict):
            continue
        for item in plan.get("timeline", [])[:24]:
            if not isinstance(item, dict) or item.get("state") in {None, "idle", "unknown"}:
                continue
            rows.append(
                {
                    "time": _time_range(item),
                    "device": _display_text(device_key),
                    "action": _display_text(item.get("state")),
                    "reason": item.get("reason") or item.get("reason_codes"),
                    "estimated_kwh": item.get("estimated_energy_kwh"),
                    "estimated_value": item.get("arbitrage_value") or item.get("effective_price"),
                }
            )
    return rows[:24]


def _time_range(item: Mapping[str, Any]) -> str:
    """Return a compact ISO time range for a timeline row."""
    start = str(item.get("start", ""))
    end = str(item.get("end", ""))
    return f"{start[11:16]}-{end[11:16]}" if len(start) >= 16 and len(end) >= 16 else "Current period"


def build_device_plans(context: DecisionContext, actions: list[PlanAction], interval_minutes: int) -> dict[str, Any]:
    """Return compact 24-hour device timelines for entity attributes."""
    climate_actions = [action for action in actions if action.asset == ActionAsset.DAIKIN]
    enphase_actions = [action for action in actions if action.asset == ActionAsset.ENPHASE]
    climate_plan = _device_plan(
        context,
        interval_minutes,
        _climate_timeline_entry,
        climate_actions,
    )
    climate_plan.update(_climate_plan_summary(context, climate_actions))
    enphase_plan = _device_plan(
        context,
        interval_minutes,
        _enphase_timeline_entry,
        enphase_actions,
    )
    enphase_plan.update(_enphase_plan_summary(context, enphase_actions))
    return {
        "climate": climate_plan,
        "enphase": enphase_plan,
        "ev": _device_plan(
            context,
            interval_minutes,
            _ev_timeline_entry,
            [action for action in actions if action.asset == ActionAsset.EV],
        ),
    }
