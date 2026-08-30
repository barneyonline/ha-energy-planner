"""Helpers for reading Energy Planner config entry data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
)
from .type_defs import EnergyPlannerConfigEntry

_RETIRED_CONFIG_KEYS = frozenset(
    {
        "haeo_config_entry_id",
        "haeo_entry_id",
        "haeo_optimize_service",
    }
)


def remove_retired_config_keys(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config data without keys used by retired integrations."""
    return {key: value for key, value in data.items() if key not in _RETIRED_CONFIG_KEYS}


def combined_entry_data(entry: EnergyPlannerConfigEntry) -> dict[str, Any]:
    """Return hub data merged with planner input subentry data."""
    data = remove_retired_config_keys(entry.data)
    for subentry in getattr(entry, "subentries", {}).values():
        subentry_data = getattr(subentry, "data", None)
        if isinstance(subentry_data, Mapping):
            data.update(remove_retired_config_keys(subentry_data))
    # Read legacy EV Smart Charging control keys as direct charger controls.
    # This keeps existing entries safe until the EV subentry is reconfigured.
    aliases = {
        CONF_EV_CHARGER: CONF_EV_SMART_CHARGING,
        CONF_EV_CHARGER_START: CONF_EV_SMART_CHARGING_START,
        CONF_EV_CHARGER_STOP: CONF_EV_SMART_CHARGING_STOP,
    }
    for current_key, legacy_key in aliases.items():
        if not data.get(current_key) and data.get(legacy_key):
            data[current_key] = data[legacy_key]
    return data
