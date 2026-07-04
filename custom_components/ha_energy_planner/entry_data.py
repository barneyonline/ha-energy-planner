"""Helpers for reading Energy Planner config entry data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry


def combined_entry_data(entry: ConfigEntry) -> dict[str, Any]:
    """Return hub data merged with planner input subentry data."""
    data: dict[str, Any] = dict(entry.data)
    for subentry in getattr(entry, "subentries", {}).values():
        subentry_data = getattr(subentry, "data", None)
        if isinstance(subentry_data, Mapping):
            data.update(dict(subentry_data))
    return data
