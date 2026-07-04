"""Config subentry consolidation helpers."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

SUBENTRY_SYSTEM = "system"
SUBENTRY_ENERGY = "energy"
SUBENTRY_CLIMATE = "climate"
SUBENTRY_PRESENCE = "presence"
SUBENTRY_ENPHASE = "enphase"
SUBENTRY_AI = "ai"
SUBENTRY_EV = "ev"

_TARGET_TITLES = {
    SUBENTRY_SYSTEM: "System",
    SUBENTRY_ENERGY: "Energy",
    SUBENTRY_CLIMATE: "Climate",
    SUBENTRY_PRESENCE: "Presence",
    SUBENTRY_ENPHASE: "Enphase",
    SUBENTRY_AI: "AI",
    SUBENTRY_EV: "EV",
}

_TARGET_IDS = {
    SUBENTRY_SYSTEM: "haep_system",
    SUBENTRY_ENERGY: "haep_energy",
    SUBENTRY_CLIMATE: "haep_climate",
    SUBENTRY_PRESENCE: "haep_presence",
    SUBENTRY_ENPHASE: "haep_enphase",
    SUBENTRY_AI: "haep_ai",
    SUBENTRY_EV: "haep_ev",
}

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

_AI_KEYS = {"ai_advisor_service", "ai_agent_id", "ai_task_entity"}
_CLIMATE_KEYS_FROM_ENERGY = {"weather_entity"}
_PRESENCE_KEYS = {"person_entities"}
_ENPHASE_REMOVED_KEYS = {"enphase_arbitrage_profile"}
_ENPHASE_DEFAULTS = {
    "enphase_ai_profile": "AI Optimisation",
    "enphase_self_consumption_profile": "Self-Consumption",
    "enphase_full_backup_profile": "Full Backup",
}
_MOVED_KEYS_BY_TARGET = {
    SUBENTRY_ENERGY: _CLIMATE_KEYS_FROM_ENERGY,
    SUBENTRY_CLIMATE: _PRESENCE_KEYS,
    SUBENTRY_ENPHASE: _AI_KEYS,
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


def needs_subentry_consolidation(entry: ConfigEntry) -> bool:
    """Return whether the entry still has legacy split subentries."""
    return any(
        subentry.subentry_type not in _TARGET_TITLES
        or (subentry.subentry_type == SUBENTRY_ENPHASE and any(key in subentry.data for key in _AI_KEYS))
        or (subentry.subentry_type == SUBENTRY_ENPHASE and any(key in subentry.data for key in _ENPHASE_REMOVED_KEYS))
        or (subentry.subentry_type == SUBENTRY_ENPHASE and any(key not in subentry.data for key in _ENPHASE_DEFAULTS))
        or (
            subentry.subentry_type == SUBENTRY_ENERGY and any(key in subentry.data for key in _CLIMATE_KEYS_FROM_ENERGY)
        )
        or (subentry.subentry_type == SUBENTRY_CLIMATE and any(key in subentry.data for key in _PRESENCE_KEYS))
        for subentry in getattr(entry, "subentries", {}).values()
        if subentry.subentry_type in _LEGACY_TO_TARGET
    )


def async_consolidate_subentries(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fold legacy split subentries into the consolidated group layout."""
    existing_by_type = {
        subentry.subentry_type: subentry
        for subentry in getattr(entry, "subentries", {}).values()
        if subentry.subentry_type in _TARGET_TITLES
    }
    changed = False
    for required_subentry in (SUBENTRY_SYSTEM, SUBENTRY_PRESENCE):
        if required_subentry not in existing_by_type:
            subentry = ConfigSubentry(
                data=MappingProxyType({}),
                subentry_id=_TARGET_IDS[required_subentry],
                subentry_type=required_subentry,
                title=_TARGET_TITLES[required_subentry],
                unique_id=None,
            )
            changed |= hass.config_entries.async_add_subentry(entry, subentry)
            existing_by_type[required_subentry] = subentry

    if not needs_subentry_consolidation(entry):
        return changed

    grouped = grouped_subentry_data(entry)
    for target, data in grouped.items():
        if target in existing_by_type:
            changed |= hass.config_entries.async_update_subentry(
                entry,
                existing_by_type[target],
                title=_TARGET_TITLES[target],
                data=data,
            )
        else:
            changed |= hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_id=_TARGET_IDS[target],
                    subentry_type=target,
                    title=_TARGET_TITLES[target],
                    unique_id=None,
                ),
            )

    for subentry in list(getattr(entry, "subentries", {}).values()):
        if subentry.subentry_type in _LEGACY_TO_TARGET and subentry.subentry_type not in _TARGET_TITLES:
            changed |= hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
            continue

        moved_keys = _MOVED_KEYS_BY_TARGET.get(subentry.subentry_type, set())
        if moved_keys and any(key in subentry.data for key in moved_keys) and subentry.subentry_type not in grouped:
            changed |= hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)

    return changed
