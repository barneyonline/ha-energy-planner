"""Config subentry consolidation helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

SUBENTRY_SYSTEM = "system"
SUBENTRY_ENERGY = "energy"
SUBENTRY_CLIMATE = "climate"
SUBENTRY_PRESENCE = "presence"
SUBENTRY_ENPHASE = "enphase"
SUBENTRY_AI = "ai"
SUBENTRY_EV = "ev"

_LEGACY_TO_TARGET = {
    "energy": SUBENTRY_ENERGY,
    "optimizer": SUBENTRY_ENERGY,
    "prices": SUBENTRY_ENERGY,
    "forecasts": SUBENTRY_ENERGY,
    "weather": SUBENTRY_ENERGY,
    "climate": SUBENTRY_CLIMATE,
    "presence": SUBENTRY_PRESENCE,
    "enphase": SUBENTRY_ENPHASE,
    "advisor": SUBENTRY_AI,
    "ai": SUBENTRY_AI,
    "ev": SUBENTRY_EV,
}

_AI_KEYS = {"ai_advisor_service", "ai_task_entity"}
_CLIMATE_KEYS_FROM_ENERGY = {"weather_entity"}
_PRESENCE_KEYS = {"person_entities"}
_ENPHASE_REMOVED_KEYS = {"enphase_arbitrage_profile"}
_ENPHASE_DEFAULTS = {
    "enphase_ai_profile": "AI Optimisation",
    "enphase_self_consumption_profile": "Self-Consumption",
    "enphase_full_backup_profile": "Full Backup",
}


def grouped_subentry_data(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Return current and legacy subentry data grouped by the consolidated type."""
    grouped: dict[str, dict[str, Any]] = {}
    for subentry in getattr(entry, "subentries", {}).values():
        target = _LEGACY_TO_TARGET.get(subentry.subentry_type)
        if target is None:
            continue
        data = dict(subentry.data)
        if subentry.subentry_type == SUBENTRY_ENERGY:
            climate_data = {key: value for key, value in data.items() if key in _CLIMATE_KEYS_FROM_ENERGY}
            energy_data = {key: value for key, value in data.items() if key not in _CLIMATE_KEYS_FROM_ENERGY}
            if energy_data:
                grouped.setdefault(SUBENTRY_ENERGY, {}).update(energy_data)
            if climate_data:
                grouped.setdefault(SUBENTRY_CLIMATE, {}).update(climate_data)
            continue
        if subentry.subentry_type == SUBENTRY_CLIMATE:
            presence_data = {key: value for key, value in data.items() if key in _PRESENCE_KEYS}
            climate_data = {key: value for key, value in data.items() if key not in _PRESENCE_KEYS}
            if climate_data:
                grouped.setdefault(SUBENTRY_CLIMATE, {}).update(climate_data)
            if presence_data:
                grouped.setdefault(SUBENTRY_PRESENCE, {}).update(presence_data)
            continue
        if subentry.subentry_type == SUBENTRY_ENPHASE:
            ai_data = {key: value for key, value in data.items() if key in _AI_KEYS}
            enphase_data = {
                key: value for key, value in data.items() if key not in _AI_KEYS and key not in _ENPHASE_REMOVED_KEYS
            }
            if enphase_data or any(key in data for key in _ENPHASE_REMOVED_KEYS):
                for key, value in _ENPHASE_DEFAULTS.items():
                    enphase_data.setdefault(key, value)
            if enphase_data:
                grouped.setdefault(SUBENTRY_ENPHASE, {}).update(enphase_data)
            if ai_data:
                grouped.setdefault(SUBENTRY_AI, {}).update(ai_data)
            continue
        grouped.setdefault(target, {}).update(data)
    return {target: data for target, data in grouped.items() if data}


def async_migrate_subentries_to_entry_data(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move every legacy Add device section into the main config entry."""
    subentries = list(getattr(entry, "subentries", {}).values())
    if not subentries:
        return False

    data = dict(entry.data)
    for section_data in grouped_subentry_data(entry).values():
        data.update(section_data)
    for subentry in subentries:
        if subentry.subentry_type not in _LEGACY_TO_TARGET:
            data.update(dict(subentry.data))

    hass.config_entries.async_update_entry(entry, data=data)
    for subentry in subentries:
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    return True
