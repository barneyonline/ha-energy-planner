"""Room evidence and operator permission for preconditioning direction."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .climate_inputs import finite, instant
from .const import CONF_HVAC_PRECONDITIONING_MODE
from .models import DecisionContext
from .thermal_learning import rate
from .zone_temperature import bounds, feedback_missing, temperatures


def mode_allowed(options: Mapping[str, Any], mode: str | None) -> bool:
    """Restrictions never block a safe stop or restoration outside planning."""
    policy = options.get(CONF_HVAC_PRECONDITIONING_MODE, "automatic")
    return mode == "off" or (
        mode in {"heat", "cool"}
        and (policy == "automatic" or policy == {"heat": "heating_only", "cool": "cooling_only"}[mode])
    )


def _recent_drift(model: dict[str, Any], zone: str, context: DecisionContext) -> float | None:
    """Do not extrapolate an overnight median into a changing daytime room."""
    if rate(model, zone, "passive", context.created_at) is None:
        return None
    item = model.get("sources", {}).get(zone, {})
    rates: list[float] = []
    timestamps: list[datetime] = []
    for row in item.get("buckets", {}).get("passive", {}).get("windows", []):
        if not isinstance(row, dict) or row.get("regime") != item.get("regime"):
            continue
        at, value, duration = instant(row.get("at")), finite(row.get("rate")), finite(row.get("minutes"))
        if (
            at is None
            or value is None
            or duration is None
            or not 5 <= duration <= 30
            or not timedelta(0) <= context.created_at - at <= timedelta(hours=2)
        ):
            continue
        rates.append(value)
        timestamps.append(at)
    last = max(timestamps, default=None)
    if len(rates) < 12 or last is None or context.created_at - last > timedelta(minutes=30):
        return None
    return median(rates)


def mapped_direction(
    context: DecisionContext,
    options: Mapping[str, Any],
    model: dict[str, Any],
    peak_at: datetime,
) -> str | None:
    """Forecast weather alone cannot justify heating comfortable mapped rooms.

    Keep an active device direction instead of reversing it from weather. From idle, speculative heating of
    the shared unit requires every participating room to need heat; otherwise
    normal comfort controls retain authority over conflicting room needs.
    """
    rooms = temperatures(context)
    evidence: dict[str, Any] = {
        "policy": options.get(CONF_HVAC_PRECONDITIONING_MODE, "automatic"),
        "mode": None,
        "reason": "room_demand_unproven",
        "rooms": {},
    }
    context.climate_legacy_decision["direction"] = evidence
    if feedback_missing(context) or not rooms:
        evidence["reason"] = "zone_temperature_unavailable"
        return None
    horizon = max((peak_at - context.created_at).total_seconds() / 3600, 0)
    heat_needs, cool_needs, cooling_reserve = [], [], []
    learning = options.get("hvac_thermal_learning_enabled", True) is True
    for zone, room in rooms.items():
        value = finite(room.get("temperature"))
        low, high = bounds(context, room)
        if value is None or low is None or high is None or low >= high:
            evidence["reason"] = "zone_temperature_unavailable"
            return None
        drift = _recent_drift(model, zone, context) if learning else None
        projected = value + drift * horizon if drift is not None else None
        heat_needs.append(value < low or (projected is not None and projected < low))
        cool_needs.append(value > high or (projected is not None and projected > high))
        cooling_reserve.append(value > low)
        evidence["rooms"][zone] = {
            "temperature": value,
            "low": low,
            "high": high,
            "recent_passive_drift_c_per_hour": drift,
            "projected_peak_temperature": projected,
        }
    policy = evidence["policy"]
    current = context.current_hvac_mode
    if policy == "cooling_only":
        mode, reason = ("cool", "operator_cooling_only") if any(cooling_reserve) else (None, "room_target_reached")
    elif current in {"heat", "cool"} and mode_allowed(options, current):
        mode, reason = current, "existing_equipment_mode"
    elif all(heat_needs) and not any(cool_needs):
        mode, reason = "heat", "all_rooms_need_heating"
    elif any(cool_needs) and not any(heat_needs):
        mode, reason = "cool", "room_cooling_need"
    else:
        mode = None
        reason = "conflicting_room_demand" if any(heat_needs) and not all(heat_needs) else "room_demand_unproven"
    if mode is not None and not mode_allowed(options, mode):
        mode, reason = None, "preconditioning_mode_not_allowed"
    evidence.update(mode=mode, reason=reason)
    return mode
