"""Recover default EV controls removed by Enphase EV Charger 5.0."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
)
from .type_defs import EnergyPlannerConfigEntry

_BUTTON_KEYS = (
    (CONF_EV_CHARGER_START, "start"),
    (CONF_EV_CHARGER_STOP, "stop"),
    (CONF_EV_SMART_CHARGING_START, "start"),
    (CONF_EV_SMART_CHARGING_STOP, "stop"),
)
_RETIRED_BUTTON = re.compile(r"button\.iq_ev_charger_([a-zA-Z0-9]+)_(start|stop)_charging")


@callback
def async_migrate_retired_enphase_buttons(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Use the already-configured switch only for identifiable retired buttons.

    Missing custom controls remain blocked: a default button's charger suffix
    must match the Enphase switch's registry identity, and the button must have
    disappeared from both the entity registry and runtime state.
    """
    data = without_retired_enphase_buttons(hass, entry.data)
    if data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=data)


def without_retired_enphase_buttons(hass: HomeAssistant, original: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize current or saved controls without discarding recovery state."""
    data = dict(original)
    candidates = [
        (key, action, match)
        for key, action in _BUTTON_KEYS
        if isinstance(value := data.get(key), str)
        and (match := _RETIRED_BUTTON.fullmatch(value)) is not None
        and match[2] == action
    ]
    if not candidates:
        return data
    switch = data.get(CONF_EV_CHARGER) or data.get(CONF_EV_SMART_CHARGING)
    if not isinstance(switch, str) or not switch.startswith("switch."):
        return data
    registry = er.async_get(hass)
    registered = registry.async_get(switch)
    if registered is None or registered.platform != "enphase_ev" or registered.disabled_by is not None:
        return data
    identity = re.fullmatch(r"enphase_ev_([a-zA-Z0-9]+)_charging_switch", registered.unique_id)
    if identity is None:
        return data
    for key, _action, match in candidates:
        if (
            match[1] == identity[1][-4:]
            and registry.async_get(data[key]) is None
            and hass.states.get(data[key]) is None
        ):
            data.pop(key)
    return data
