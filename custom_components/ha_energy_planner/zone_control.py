"""Explicit shared-controller topology and durable per-room preparation intent.

Entity names and shared sensors cannot establish physical independence or minimum
airflow. Operators must supply exact pairs and installer-verified open sets.
"""

from __future__ import annotations

from copy import deepcopy
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


def evaluate(context: DecisionContext, mode: str, target: float, *, check_airflow: bool = True) -> dict[str, Any]:
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
        offsets = {finite(room["cooling_equipment_offset_c"]) for room in mapped
                   if "cooling_equipment_offset_c" in room}
        if (len(offsets) > 1 or None in offsets
                or any(value is not None and (not -2 <= value <= 0 or value * 2 != int(value * 2))
                       for value in offsets)):
            return {"supported": False, "reason": "zone_equipment_offset_invalid"}
        offset = next(iter(offsets), 0) or 0
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
        targets[climate] = limit + offset if mode == "cool" else limit
        rows[actuator] = {
            "status": "paused" if stopped else "active", "thermostat": climate,
            "temperature_entity": room["temperature_entity"], "temperature": value,
            "temperature_source": room["temperature_source"], "target": limit,
            "equipment_target": targets[climate], "cooling_equipment_offset_c": offset,
            "hysteresis_c": HYSTERESIS_C, "restart_blocker": blocker,
            "paused_at": (paused_at or context.created_at).isoformat() if stopped else None,
            "restart_not_before": restart_at.isoformat() if restart_at else None,
        }
    active = {entity for entity, state in power.items() if state == "on"}
    if check_airflow and active and not any(active == set(row) for row in config["safe_open_sets"]):
        return {"supported": False, "reason": "minimum_airflow_unproven", "zones": rows}
    complete = not active
    if complete:
        for row in rows.values():
            row["status"] = "completed"
    return {"version": 1, "supported": True, "pairs": config["pairs"], "power": power,
            "targets": targets, "zones": rows, "all_complete": complete,
            "evaluated_at": context.created_at.isoformat(),
            "reason": "all_zones_complete" if complete else "independent_zone_preparation"}


def coast_isolated_rooms(context: DecisionContext, mode: str) -> set[str]:
    """Exempt only owned, confirmed closed rooms recovering passively in head-off coast.

    The opposite boundary is not a reason to restart same-mode conditioning.
    A worsening excursion after entering coast does require recovery handoff.
    No unavailable/disabled flag or thermostat alias proves physical closure.
    """
    from .zone_temperature import bounds

    active = context.hvac_control
    previous = active.get("zone_intent", {})
    target = finite(active.get("precondition_target"))
    if (active.get("main_state_committed") is not True or not previous.get("supported")
            or target is None or active.get("command_confirmation") or active.get("required_evidence_lost")):
        return set()
    # Coast never applies preparation reopening requests. Validate room evidence
    # here, then prove airflow against the physical states below.
    intent = evaluate(context, mode, target, check_airflow=False)
    if not intent.get("supported"):
        return set()
    states = context.climate_inputs.get("zone_actuator_states", {})
    pairs = intent["pairs"]
    if any(states.get(entity) not in {"on", "off"}
           or states[entity] != previous.get("power", {}).get(entity)
           or entity not in active.get("zone_states", {}) for entity in pairs):
        return set()
    opened = {entity for entity in pairs if states[entity] == "on"}
    if not opened or not any(opened == set(row) for row in context.climate_inputs["zone_topology"]["safe_open_sets"]):
        return set()
    if (context.current_hvac_mode not in {mode, "off"} or (
        active.get("phase") in {"pre_peak_coast", "peak_coast"} and context.current_hvac_mode != "off"
    )):
        return set()
    isolated: set[str] = set()
    for actuator, climate in pairs.items():
        old = previous.get("zones", {}).get(actuator, {})
        if states[actuator] != "off" or old.get("status") not in {"paused", "completed"}:
            continue
        value = intent["zones"][actuator]["temperature"]
        before = finite(old.get("temperature"))
        rooms = context.climate_inputs["zones"]
        low, high = bounds(context, rooms[actuator] if actuator in rooms else rooms[climate])
        opposite_breach = value < low if mode == "cool" else value > high
        if previous.get("coast_isolation") and opposite_breach and (
            before is None or (value < before if mode == "cool" else value > before)
        ):
            continue
        isolated.update((actuator, climate))
    return isolated


def retained_coast_intent(context: DecisionContext, mode: str) -> dict[str, Any] | None:
    """Refresh observations without replacing the confirmed physical completion intent."""
    if not coast_isolated_rooms(context, mode):
        return None
    intent: dict[str, Any] = deepcopy(context.hvac_control["zone_intent"])
    intent["coast_isolation"] = True
    rooms = context.climate_inputs["zones"]
    for actuator, row in intent["zones"].items():
        climate = intent["pairs"][actuator]
        room = rooms[actuator] if actuator in rooms else rooms[climate]
        row["temperature"] = float(room["temperature"])
        row["temperature_source"] = deepcopy(room["temperature_source"])
    intent["evaluated_at"] = context.created_at.isoformat()
    return intent


def desired_fields(context: DecisionContext, mode: str, target: float, phase: str, *,
                   record: bool = True) -> dict[str, Any]:
    """Keep a confirmed airflow path through coast; stop the head before any closure."""
    retained = retained_coast_intent(context, mode) if phase != "preconditioning" else None
    intent = retained if retained is not None else evaluate(context, mode, target)
    if record:
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
    equipment = {}
    if mode == "cool" and any(row["cooling_equipment_offset_c"] for row in intent["zones"].values()):
        active_targets = [intent["targets"][intent["pairs"][actuator]]
                          for actuator, power in intent["power"].items() if power == "on"]
        # Keep the shared head demanding at the lowest equipment target.
        # Every active subordinate must fit Daikin's head +/-2 C range.
        head_target = min(active_targets)
        if max(active_targets) - head_target > 2:
            return {"zone_control_blocked": "zone_equipment_range_incompatible"}
        equipment = {"target_temperature": head_target, "equipment_compensation": True}
    return {"zone_intent": intent, "zone_power": intent["power"], "zone_targets": intent["targets"],
            "configured_zones_only": True, **equipment}


def live_rejection(hass: Any, data: dict[str, Any], options: dict[str, Any], desired: dict[str, Any],
                   previous: dict[str, Any], now: Any) -> str | None:
    """Recheck mapped sensor freshness and the exact subset immediately before I/O."""
    if desired.get("zone_control_blocked"):
        return str(desired["zone_control_blocked"])
    if desired.get("equipment_compensation") and (
        not desired.get("zone_intent") or desired.get("zone_stop") or desired.get("mode") != "cool"
    ):
        return "zone_equipment_intent_invalid"
    retained_coast = desired.get("zone_intent", {}).get("coast_isolation") is True
    if not desired.get("zone_intent") or (desired.get("zone_stop") and not retained_coast):
        return None
    if retained_coast:
        return None if confirmed_coast_intent(hass, data, options, desired, previous, now) is not None else (
            "zone_coast_protection_lost"
        )
    context = _live_context(hass, data, options, previous, now)
    current = evaluate(context, str(desired.get("mode")), float(desired["precondition_target"]))
    if not current.get("supported"):
        return str(current["reason"])
    if any(current.get(key) != desired["zone_intent"].get(key) for key in ("power", "targets", "pairs")):
        return "zone_intent_superseded"
    fields = desired_fields(context, str(desired.get("mode")), float(desired["precondition_target"]),
                            "preconditioning")
    if fields.get("zone_control_blocked"):
        return str(fields["zone_control_blocked"])
    if fields.get("equipment_compensation") and any(
        desired.get(key) != fields.get(key) for key in ("target_temperature", "equipment_compensation")
    ):
        return "zone_equipment_target_superseded"
    return None


def _live_context(hass: Any, data: dict[str, Any], options: dict[str, Any],
                  previous: dict[str, Any], now: Any) -> DecisionContext:
    """Read one current set of room, physical switch and head observations."""
    from .climate_inputs import read_climate_inputs

    inputs = read_climate_inputs(hass, data, options, now, now + timedelta(hours=12), {})
    def helper(key: str) -> float | None:
        state = hass.states.get(data.get(key))
        return finite(state.state) if state is not None else None
    head = hass.states.get(data.get("daikin_climate_entity"))
    return DecisionContext(
        now, "zone-readback", [], None, None, OccupancyState.UNKNOWN, InputHealth.HEALTHY,
        climate_inputs=inputs, hvac_control=previous,
        current_hvac_mode=head.state if head is not None else None,
        occupied_temperature_low_c=helper("climate_target_low_entity"),
        occupied_temperature_high_c=helper("climate_target_high_entity"),
    )


def confirmed_coast_intent(hass: Any, data: dict[str, Any], options: dict[str, Any], desired: dict[str, Any],
                           previous: dict[str, Any], now: Any, *,
                           require_head_off: bool = False) -> dict[str, Any] | None:
    """Capture a safe execution-time coast observation for durable recovery checks."""
    from .zone_temperature import comfort_breached

    context = _live_context(hass, data, options, previous, now)
    if require_head_off and context.current_hvac_mode != "off":
        return None
    intent = retained_coast_intent(context, str(desired.get("mode")))
    if intent is None or comfort_breached(context, str(desired.get("mode")), coasting=True):
        return None
    return intent
