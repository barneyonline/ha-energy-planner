"""Shared room feedback for legacy and economic climate control."""

from __future__ import annotations

from typing import Any

from .climate_inputs import finite
from .models import DecisionContext


def temperatures(context: DecisionContext) -> dict[str, dict[str, Any]]:
    """Return explicitly mapped rooms, retaining missing readings as missing."""
    return {
        entity: room for entity, room in context.climate_inputs.get("zones", {}).items()
        if room.get("temperature_entity") or room.get("temperature") is not None
    }


def feedback_missing(context: DecisionContext) -> bool:
    """Require explicitly mapped temperatures and room comfort constraints."""
    if (context.climate_inputs.get("zone_temperature_inputs_configured")
            and not context.climate_inputs.get("configuration_valid", True)):
        return True
    for room in temperatures(context).values():
        if finite(room.get("temperature")) is None:
            return True
        if any(room.get(key + "_entity") and finite(room.get(key)) is None for key in ("low", "high")):
            return True
        low, high = bounds(context, room)
        if low is not None and high is not None and low >= high:
            return True
    return False


def bounds(context: DecisionContext, room: dict[str, Any]) -> tuple[float | None, float | None]:
    """Use room comfort helpers where provided, otherwise household bounds."""
    low = (finite(room.get("low")) if (room.get("low_entity") or room.get("low") is not None)
           else context.occupied_temperature_low_c)
    high = (finite(room.get("high")) if (room.get("high_entity") or room.get("high") is not None)
            else context.occupied_temperature_high_c)
    return low, high


def control_temperature(context: DecisionContext, mode: str | None = None) -> float | None:
    """Cooling follows the warmest room; heating follows the coldest."""
    if feedback_missing(context):
        return None
    rooms = temperatures(context)
    if not rooms:
        return context.current_hvac_temperature_c
    values = [float(room["temperature"]) for room in rooms.values()]
    return min(values) if mode == "heat" else max(values)


def target_reached(context: DecisionContext, mode: str, target: float) -> bool:
    """All mapped rooms must reach their applicable target before early coast."""
    if context.climate_inputs.get("zone_topology", {}).get("supported"):
        from .zone_control import evaluate
        intent = evaluate(context, mode, target)
        if intent.get("supported"):
            return bool(intent["all_complete"])
    rooms = temperatures(context)
    if not rooms:
        value = context.current_hvac_temperature_c
        return value is not None and (value >= target if mode == "heat" else value <= target)
    if feedback_missing(context):
        return False
    for room in rooms.values():
        limit = finite(room.get("high" if mode == "heat" else "low"))
        limit = target if limit is None else limit
        if (float(room["temperature"]) < limit if mode == "heat" else float(room["temperature"]) > limit):
            return False
    return True


def comfort_breached(context: DecisionContext, mode: str, *, coasting: bool) -> bool:
    """Protect each room, including the opposite boundary during preparation."""
    rooms = temperatures(context)
    if not coasting and context.climate_inputs.get("zone_topology", {}).get("supported"):
        from .zone_control import evaluate
        target = context.hvac_control.get("precondition_target")
        target = finite(target) or (context.occupied_temperature_high_c if mode == "heat"
                                    else context.occupied_temperature_low_c)
        if target is not None and evaluate(context, mode, target).get("supported"):
            # Completed rooms will be physically isolated by the same intent.
            # If airflow/mapping proof fails, retain the established handoff.
            return False
    if not rooms:
        value = context.current_hvac_temperature_c
        low, high = context.occupied_temperature_low_c, context.occupied_temperature_high_c
        return bool(value is not None and low is not None and high is not None and (
            value < low or value > high
            or (coasting and ((mode == "heat" and value <= low) or (mode == "cool" and value >= high)))
        ))
    for room in rooms.values():
        value = finite(room.get("temperature"))
        low, high = bounds(context, room)
        if value is not None and low is not None and high is not None:
            if mode == "cool" and value < low:
                return True
            if mode == "heat" and value > high:
                return True
            if coasting and (value >= high if mode == "cool" else value <= low):
                return True
    return False
