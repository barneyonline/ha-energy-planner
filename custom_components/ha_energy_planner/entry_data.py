"""Helpers for reading Energy Planner config entry data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
)


def combined_entry_data(entry: ConfigEntry) -> dict[str, Any]:
    """Return hub data merged with planner input subentry data."""
    data: dict[str, Any] = dict(entry.data)
    for subentry in getattr(entry, "subentries", {}).values():
        subentry_data = getattr(subentry, "data", None)
        if isinstance(subentry_data, Mapping):
            data.update(dict(subentry_data))
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
