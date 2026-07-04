"""Optional Recorder import helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib import import_module
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_EV_CONNECTED, CONF_EV_SOC
from .ev import import_trip_history_from_state_sequences

RECORDER_IMPORT_INTERVAL = timedelta(hours=24)
RECORDER_IMPORT_LOOKBACK = timedelta(days=30)


async def async_import_ev_trip_history_from_recorder(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    history: dict[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], bool, str]:
    """Import compact EV trip records from Recorder when available."""
    connected_entity = entry_data.get(CONF_EV_CONNECTED)
    soc_entity = entry_data.get(CONF_EV_SOC)
    if not connected_entity or not soc_entity:
        return history, False, "recorder_ev_entities_not_configured"
    if not _import_due(history, now):
        return history, False, "recorder_import_recent"

    try:
        executor = _recorder_executor(hass)
        connected_states, soc_states = await executor(
            _load_recorder_states,
            hass,
            str(connected_entity),
            str(soc_entity),
            now - RECORDER_IMPORT_LOOKBACK,
            now,
        )
    except Exception as err:  # noqa: BLE001 - Recorder is optional and must fail closed.
        return history, False, f"recorder_import_unavailable:{err.__class__.__name__}"

    updated, changed = import_trip_history_from_state_sequences(
        history,
        connected_states=connected_states,
        soc_states=soc_states,
        imported_at=now,
    )
    return updated, changed, "recorder_imported" if changed else "recorder_no_new_trips"


def _load_recorder_states(
    hass: HomeAssistant,
    connected_entity: str,
    soc_entity: str,
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[Any], list[Any]]:
    history = import_module("homeassistant.components.recorder.history")
    state_changes = history.state_changes_during_period
    connected = state_changes(
        hass,
        start_time,
        end_time,
        entity_id=connected_entity,
        no_attributes=True,
        include_start_time_state=True,
    ).get(connected_entity, [])
    soc = state_changes(
        hass,
        start_time,
        end_time,
        entity_id=soc_entity,
        no_attributes=True,
        include_start_time_state=True,
    ).get(soc_entity, [])
    return list(connected), list(soc)


def _recorder_executor(hass: HomeAssistant) -> Any:
    """Return Recorder's DB executor when available, otherwise HA's executor."""
    try:
        recorder = import_module("homeassistant.components.recorder")
        get_instance = getattr(recorder, "get_instance", None)
        instance = get_instance(hass) if callable(get_instance) else None
        executor = getattr(instance, "async_add_executor_job", None)
        if callable(executor):
            return executor
    except Exception:  # noqa: BLE001 - Recorder is optional and fallback must remain available.
        pass
    return hass.async_add_executor_job


def _import_due(history: dict[str, Any], now: datetime) -> bool:
    imported_at = history.get("recorder_imported_at")
    if imported_at is None:
        return True
    if isinstance(imported_at, datetime):
        return now >= _align_timestamp(imported_at, now) + RECORDER_IMPORT_INTERVAL
    if isinstance(imported_at, str):
        try:
            parsed = datetime.fromisoformat(imported_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return now >= _align_timestamp(parsed, now) + RECORDER_IMPORT_INTERVAL
    return True


def _align_timestamp(value: datetime, now: datetime) -> datetime:
    if value.tzinfo is None and now.tzinfo is not None:
        return value.replace(tzinfo=now.tzinfo)
    if value.tzinfo is not None and now.tzinfo is None:
        return value.replace(tzinfo=None)
    return value
