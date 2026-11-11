"""Read complete AirBase mode memories without waking or probing equipment.

This optional bridge is deliberately specific to HA's core Daikin integration
and pydaikin AirBase. It never sends vendor writes or infers hidden targets.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from typing import Any
from urllib.parse import unquote

from homeassistant.helpers import entity_registry

from .climate_inputs import finite
from .const import (
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_HVAC_ZONE_CONTROL,
)
from .zone_control import read_topology

OFF_START_OPTION = "hvac_safe_off_start"
CONTROL_KEYS = (
    CONF_DAIKIN_CLIMATE,
    CONF_CLIMATE_ZONES,
    CONF_HVAC_ZONE_CONTROL,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
)
MEMORY_KEY = "off_start_memory"


def provider(
    hass: Any, data: dict[str, Any], *, require_available: bool = True,
) -> tuple[Any, dict[str, int], str] | None:
    """Resolve exact registered zone indices; names cannot establish identity."""
    topology = read_topology(hass, data)
    if not topology.get("supported"):
        return None
    registry = entity_registry.async_get(hass)
    head = registry.async_get(data[CONF_DAIKIN_CLIMATE])
    if head is None or head.platform != "daikin":
        return None
    entry = hass.config_entries.async_get_entry(head.config_entry_id)
    coordinator = getattr(entry, "runtime_data", None)
    device = getattr(coordinator, "device", None)
    if (
        type(device).__module__ != "pydaikin.daikin_airbase"
        or type(device).__name__ != "DaikinAirBase"
        or coordinator is None
        or (require_available and not coordinator.last_update_success)
    ):
        return None
    indices = {}
    for actuator, entity in topology["pairs"].items():
        row = registry.async_get(entity)
        prefix = f"{getattr(device, 'mac', '')}-zone"
        if row is None or not row.unique_id.startswith(prefix) or not row.unique_id.endswith("-temperature"):
            return None
        index = row.unique_id[len(prefix) : -len("-temperature")]
        if not index.isdecimal():
            return None
        switch = registry.async_get(actuator)
        if getattr(switch, "unique_id", None) != f"{prefix}{index}":
            return None
        indices[entity] = int(index)
    if len(set(indices.values())) != len(indices):
        return None
    return device, indices, str(head.config_entry_id)


def source_identity(source: tuple[Any, dict[str, int], str]) -> dict[str, str]:
    """Persist a physical-controller fingerprint without its network identifier."""
    device = source[0]
    count = device.values.values_for_resource("aircon/get_model_info").get("en_zone")
    binding = json.dumps([device.mac, count, source[1]], sort_keys=True)
    return {"entry_id": source[2], "controller_fingerprint": sha256(binding.encode()).hexdigest()}


def source_matches(hass: Any, data: dict[str, Any], expected: dict[str, Any]) -> bool:
    source = provider(hass, data)
    return source is not None and all(expected.get(key) == value for key, value in source_identity(source).items())


async def read_memory(
    hass: Any,
    data: dict[str, Any],
    mode: str,
    *,
    allow_active_verification: bool = False,
) -> dict[str, Any] | None:
    """Fresh read-only resources, finite exact per-mode targets and power evidence."""
    source = provider(hass, data)
    if source is None or mode not in {"heat", "cool"}:
        return None
    device, indices, _ = source
    try:
        async with asyncio.timeout(30):
            await device.update_status(
                [
                    "aircon/get_control_info",
                    "aircon/get_zone_setting",
                    "aircon/get_model_info",
                ],
                force_refresh=True,
            )
        control = device.values.values_for_resource("aircon/get_control_info")
        zones = device.values.values_for_resource("aircon/get_zone_setting")
        model = device.values.values_for_resource("aircon/get_model_info")
        selected = {"1": "heat", "2": "cool", "7": "dry"}.get(control.get("mode"))
        head = hass.states.get(data[CONF_DAIKIN_CLIMATE])
        if (
            selected is None
            or head is None
            or not (
                control.get("pow") == "0"
                and head.state == "off"
                or allow_active_verification
                and control.get("pow") == "1"
                and head.state == selected
            )
            or finite(control.get("stemp")) != finite(head.attributes.get("temperature"))
        ):
            return None
        head_low, head_high, head_step = (
            finite(head.attributes.get(key)) for key in ("min_temp", "max_temp", "target_temp_step")
        )
        if head_low is None or head_high is None or head_step is None or head_step <= 0:
            return None
        pairs = read_topology(hass, data).get("pairs", {})
        powers = unquote(zones["zone_onoff"]).split(";")
        count = finite(model.get("en_zone"))
        if (
            count is None
            or not 1 <= count <= 8
            or count != int(count)
            or set(indices.values()) != set(range(int(count)))
            or len(powers) < count
        ):
            return None
        if provider(hass, data) != source:
            return None
        result: dict[str, Any] = {
            "version": 1,
            "source": "core_daikin_airbase_readback",
            **source_identity(source),
            "head": data[CONF_DAIKIN_CLIMATE],
            "pairs": pairs,
            "selected_mode": selected,
            "acquired_mode": mode,
            "modes": {},
            "power": {},
            "control_data": deepcopy({key: data.get(key) for key in CONTROL_KEYS}),
        }
        for hvac_mode, key, zone_key in (("heat", "dt1", "lztemp_h"), ("cool", "dt2", "lztemp_c")):
            target = finite(control.get(key))
            values = unquote(zones[zone_key]).split(";")
            targets = {entity: finite(values[index]) for entity, index in indices.items()}
            low, high = finite(model.get(hvac_mode + "_l")), finite(model.get(hvac_mode + "_h"))
            if (
                target is None
                or low is None
                or high is None
                or not low <= target <= high
                or not head_low <= target <= head_high
                or abs((target - head_low) / head_step - round((target - head_low) / head_step)) > 1e-6
                or len(values) != len(powers)
                or any(value is None or abs(value - target) > 2 or value != round(value) for value in targets.values())
            ):
                return None
            result["modes"][hvac_mode] = {"target_temperature": target, "zones": targets}
        if selected == "dry":
            # AirBase has no dt7 memory. The original Off/Dry stemp is directly
            # observed under mode=7, distinct from both heat/cool memories.
            target = finite(control.get("stemp"))
            if (
                target is None
                or not head_low <= target <= head_high
                or abs((target - head_low) / head_step - round((target - head_low) / head_step)) > 1e-6
            ):
                return None
            result["modes"]["dry"] = {
                "target_temperature": target,
                "zones": {},
                "target_source": "original_off_dry_stemp",
            }
        if result["modes"][selected]["target_temperature"] != finite(control.get("stemp")):
            return None
        for actuator, climate in pairs.items():
            state = hass.states.get(actuator)
            expected = {"0": "off", "1": "on"}.get(powers[indices[climate]])
            if state is None or expected is None or state.state != expected:
                return None
            result["power"][actuator] = expected
        return result
    except Exception:  # noqa: BLE001 - capability reads must fail closed without vendor details.
        return None


def owned_control_data(data: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    """Keep pending control feedback and release on the captured physical controller."""
    main = control.get("main_state")
    memory = main.get(MEMORY_KEY) if isinstance(main, dict) else None
    saved = control.get("off_start_control_data")
    if saved is None and isinstance(memory, dict) and memory.get("version") == 1:
        saved = memory.get("control_data")
    if not isinstance(saved, dict) or set(saved) != set(CONTROL_KEYS):
        return data
    return {**data, **saved}


def memory_matches(observed: dict[str, Any] | None, baseline: dict[str, Any], zones: dict[str, Any]) -> bool:
    """Confirm all owned mode memories after shutdown, including inactive modes."""
    return bool(
        observed is not None
        and observed["entry_id"] == baseline["entry_id"]
        and observed.get("controller_fingerprint") == baseline.get("controller_fingerprint")
        and observed["selected_mode"] == baseline["selected_mode"]
        and all(
            observed["modes"].get(mode, {}).get("target_temperature") == normal["target_temperature"]
            and all(
                observed["modes"][mode]["zones"].get(entity) == target
                for entity, target in normal["zones"].items()
                if entity in zones
            )
            for mode, normal in baseline["modes"].items()
        )
        and all(
            observed["power"].get(entity) == power
            for entity, power in zones.items()
            if not entity.startswith("climate.")
        )
    )


async def read_activation(hass: Any, data: dict[str, Any]) -> dict[str, Any] | None:
    """Read current Off recall solely for command feedback, never as a baseline."""
    source = provider(hass, data)
    if source is None:
        return None
    device, _, _ = source
    try:
        async with asyncio.timeout(30):
            await device.update_status(["aircon/get_control_info"], force_refresh=True)
        control = device.values.values_for_resource("aircon/get_control_info")
        mode = {"1": "heat", "2": "cool", "7": "dry"}.get(control.get("mode"))
        target = finite(control.get("stemp"))
        head = hass.states.get(data[CONF_DAIKIN_CLIMATE])
        if (
            provider(hass, data) != source
            or control.get("pow") != "0"
            or mode is None
            or target is None
            or head is None
            or head.state != "off"
            or finite(head.attributes.get("temperature")) != target
        ):
            return None
        return {
            "entity_id": data[CONF_DAIKIN_CLIMATE],
            **source_identity(source),
            "hvac_mode": mode,
            "target_temperature": target,
        }
    except Exception:  # noqa: BLE001 - an unproven activation never authorizes feedback.
        return None
