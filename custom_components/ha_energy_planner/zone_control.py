"""Explicit shared-controller topology and durable per-room preparation intent.

Entity names and shared sensors cannot establish physical independence or minimum
airflow. Operators must supply exact pairs and installer-verified open sets.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .climate_inputs import finite, instant
from .models import DecisionContext, InputHealth, OccupancyState

CONFIG_KEY = "hvac_zone_control"
HYSTERESIS_C = 0.3
PAUSE_MINUTES = 10


def topology(data: dict[str, Any]) -> dict[str, Any]:
    """Validate explicit one-to-one pairs, without sensor or prefix inference."""
    config = data.get(CONFIG_KEY)
    if not config:
        return {"supported": False, "reason": "independent_airflow_not_configured"}
    configured = data.get("climate_zone_entities", [])
    if isinstance(configured, str):
        configured = [entity.strip() for entity in configured.split(",") if entity.strip()]
    if not isinstance(config, dict) or set(config) != {"pairs", "safe_open_sets"}:
        return {"supported": False, "reason": "invalid_zone_topology"}
    pairs, safe = config["pairs"], config["safe_open_sets"]
    if (not isinstance(pairs, dict) or not 2 <= len(pairs) <= 8
            or any(not isinstance(a, str) or not a.startswith("switch.")
                   or not isinstance(c, str) or not c.startswith("climate.") for a, c in pairs.items())
            or len(set(pairs.values())) != len(pairs)
            or data.get("daikin_climate_entity") in pairs.values()
            or not isinstance(configured, list) or any(not isinstance(entity, str) for entity in configured)
            or set(configured) != set(pairs) | set(pairs.values())
            or not isinstance(safe, list) or not 1 <= len(safe) <= 255
            or any(not isinstance(row, list) or not row or any(not isinstance(a, str) for a in row)
                   or len(set(row)) != len(row) or not set(row) <= set(pairs) for row in safe)
            or not any(set(row) == set(pairs) for row in safe)):
        return {"supported": False, "reason": "invalid_zone_topology"}
    return {"supported": True, "pairs": dict(pairs), "safe_open_sets": safe,
            "reason": "installer_verified_open_sets"}


def read_topology(hass: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Require the head and every declared control to share registry ownership."""
    result = topology(data)
    if not result.get("supported"):
        return result
    from homeassistant.helpers import entity_registry
    registry = entity_registry.async_get(hass)
    head = registry.async_get(data.get("daikin_climate_entity", ""))
    entities = [*result["pairs"], *result["pairs"].values()]
    if (head is None or not head.config_entry_id or not head.device_id
            or any((entry := registry.async_get(entity)) is None
                   or (entry.config_entry_id, entry.device_id, entry.platform)
                   != (head.config_entry_id, head.device_id, head.platform) for entity in entities)):
        return {**result, "supported": False, "reason": "zone_controller_registry_unproven"}
    return result


def superseded_entities(data: dict[str, Any], entities: set[str]) -> set[str]:
    """A manual change owns the physical zone, including its declared alias."""
    result = set(entities)
    for actuator, climate in topology(data).get("pairs", {}).items():
        if {actuator, climate} & entities:
            result.update((actuator, climate))
    return result


def evaluate(context: DecisionContext, mode: str, target: float) -> dict[str, Any]:
    """Compute desired zone power; persistence belongs to the ownership transaction."""
    from .zone_temperature import bounds, feedback_missing

    config = context.climate_inputs.get("zone_topology", {})
    if not config.get("supported"):
        return {"supported": False, "reason": config.get("reason", "independent_airflow_not_configured")}
    if mode not in {"heat", "cool"} or feedback_missing(context):
        return {"supported": False, "reason": "zone_feedback_unavailable"}
    previous = context.hvac_control.get("zone_intent", {})
    if previous and previous.get("pairs") != config["pairs"]:
        return {"supported": False, "reason": "zone_topology_changed"}
    if previous and previous.get("version") != 1:
        return {"supported": False, "reason": "zone_intent_version_unknown"}
    rooms = context.climate_inputs.get("zones", {})
    rows, power, targets = {}, {}, {}
    for actuator, climate in config["pairs"].items():
        mapped = [rooms[entity] for entity in (actuator, climate) if entity in rooms]
        if (not mapped or any(not room.get("temperature_entity") or finite(room.get("temperature")) is None
                              or room.get("temperature_source", {}).get("fresh") is not True for room in mapped)
                or len({(room["temperature_entity"], *bounds(context, room)) for room in mapped}) != 1):
            return {"supported": False, "reason": "zone_mapping_missing_or_ambiguous"}
        room = mapped[0]
        low, high = bounds(context, room)
        if low is None or high is None or low >= high:
            return {"supported": False, "reason": "zone_bounds_unavailable"}
        value = float(room["temperature"])
        limit = finite(room.get("high" if mode == "heat" else "low"))
        limit = min(high, max(low, target)) if limit is None else limit
        reached = value >= limit if mode == "heat" else value <= limit
        old = previous.get("zones", {}).get(actuator, {})
        paused_at = instant(old.get("paused_at"))
        paused = old.get("status") in {"paused", "completed"}
        if paused and (paused_at is None or paused_at > context.created_at):
            return {"supported": False, "reason": "zone_pause_time_invalid"}
        restart_at = paused_at + timedelta(minutes=PAUSE_MINUTES) if paused_at else None
        margin = value <= limit - HYSTERESIS_C if mode == "heat" else value >= limit + HYSTERESIS_C
        blocker = None
        if paused and not reached:
            blocker = ("pause_dwell" if restart_at is None or context.created_at < restart_at
                       else "hysteresis" if not margin else None)
        stopped = reached or bool(blocker)
        power[actuator] = "off" if stopped else "on"
        targets[climate] = limit
        rows[actuator] = {
            "status": "paused" if stopped else "active", "thermostat": climate,
            "temperature_entity": room["temperature_entity"], "temperature": value,
            "temperature_source": room["temperature_source"], "target": limit,
            "hysteresis_c": HYSTERESIS_C, "restart_blocker": blocker,
            "paused_at": (paused_at or context.created_at).isoformat() if stopped else None,
            "restart_not_before": restart_at.isoformat() if restart_at else None,
        }
    active = {entity for entity, state in power.items() if state == "on"}
    if active and not any(active == set(row) for row in config["safe_open_sets"]):
        return {"supported": False, "reason": "minimum_airflow_unproven", "zones": rows}
    complete = not active
    if complete:
        for row in rows.values():
            row["status"] = "completed"
    return {"version": 1, "supported": True, "pairs": config["pairs"], "power": power,
            "targets": targets, "zones": rows, "all_complete": complete,
            "evaluated_at": context.created_at.isoformat(),
            "reason": "all_zones_complete" if complete else "independent_zone_preparation"}


def desired_fields(context: DecisionContext, mode: str, target: float, phase: str) -> dict[str, Any]:
    """Keep a confirmed airflow path through coast; stop the head before any closure."""
    intent = evaluate(context, mode, target)
    context.climate_legacy_decision["zone_control"] = intent
    if not intent.get("supported"):
        return {"zone_control_blocked": intent["reason"]} if (
            context.climate_inputs.get("zone_topology", {}).get("supported") or context.hvac_control.get("zone_intent")
        ) else {}
    if phase != "preconditioning" or intent["all_complete"]:
        # Do not close the last path. Coast is head-off for this topology;
        # restore/release still owns every original target and power baseline.
        return {"zone_intent": intent, "zone_stop": True, "hvac_mode": "off", "power": "off",
                "enable_zones": False, "target_temperature": None}
    return {"zone_intent": intent, "zone_power": intent["power"], "zone_targets": intent["targets"],
            "configured_zones_only": True}


def live_rejection(hass: Any, data: dict[str, Any], options: dict[str, Any], desired: dict[str, Any],
                   previous: dict[str, Any], now: Any) -> str | None:
    """Recheck mapped sensor freshness and the exact subset immediately before I/O."""
    from .climate_inputs import read_climate_inputs

    if desired.get("zone_control_blocked"):
        return str(desired["zone_control_blocked"])
    if not desired.get("zone_intent") or desired.get("zone_stop"):
        return None
    inputs = read_climate_inputs(hass, data, options, now, now + timedelta(hours=12), {})
    def helper(key: str) -> float | None:
        state = hass.states.get(data.get(key))
        return finite(state.state) if state is not None else None
    context = DecisionContext(
        now, "zone-readback", [], None, None, OccupancyState.UNKNOWN, InputHealth.HEALTHY,
        climate_inputs=inputs, hvac_control=previous,
        occupied_temperature_low_c=helper("climate_target_low_entity"),
        occupied_temperature_high_c=helper("climate_target_high_entity"),
    )
    current = evaluate(context, str(desired.get("mode")), float(desired["precondition_target"]))
    if not current.get("supported"):
        return str(current["reason"])
    if any(current.get(key) != desired["zone_intent"].get(key) for key in ("power", "targets", "pairs")):
        return "zone_intent_superseded"
    return None
