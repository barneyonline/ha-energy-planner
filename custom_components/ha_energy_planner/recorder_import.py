"""Optional Recorder import helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from importlib import import_module
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_DAIKIN_POWER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
)
from .ev import import_trip_history_from_state_sequences
from .load_forecast import HISTORY_LOOKBACK, build_load_forecast_model, training_due

RECORDER_IMPORT_INTERVAL = timedelta(hours=24)
RECORDER_IMPORT_LOOKBACK = timedelta(days=30)


async def async_update_builtin_load_forecast(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    model: dict[str, Any],
    *,
    now: datetime,
    timezone: str,
    force: bool = False,
) -> tuple[dict[str, Any], bool, str]:
    """Train the compact household-load model when its bounded refresh is due."""
    load_entity = str(entry_data.get(CONF_HOUSEHOLD_LOAD, "") or "").strip()
    if not load_entity:
        return model, False, "load_forecast_household_load_not_configured"
    if not force and not training_due(model, now=now, source_entity_id=load_entity, timezone=timezone):
        return model, False, "load_forecast_training_recent"
    attempted = {
        **model,
        "last_attempt_at": now.isoformat(),
        "last_attempt_source_entity_id": load_entity,
        "last_attempt_timezone": timezone,
    }
    load_state = hass.states.get(load_entity)
    if load_state is None:
        return attempted, attempted != model, "load_forecast_household_load_unavailable"
    load_unit = _state_unit(load_state)
    ev_entity = str(entry_data.get(CONF_EV_CHARGING, "") or "").strip() or None
    hvac_entity = str(entry_data.get(CONF_DAIKIN_POWER, "") or "").strip() or None
    hvac_state = hass.states.get(hvac_entity) if hvac_entity else None
    try:
        executor = _recorder_executor(hass)
        histories = await executor(
            _load_household_forecast_states,
            hass,
            load_entity,
            ev_entity,
            hvac_entity,
            now - HISTORY_LOOKBACK,
            now,
        )
        updated = await executor(
            partial(
                build_load_forecast_model,
                histories["load"],
                now=now,
                timezone=timezone,
                source_entity_id=load_entity,
                load_unit=load_unit,
                ev_charging_states=histories["ev_charging"],
                hvac_power_states=histories["hvac_power"],
                hvac_power_unit=_state_unit(hvac_state),
            )
        )
        if (
            updated.get("status") != "ready"
            and model.get("quality_ready") is True
            and model.get("source_entity_id") == load_entity
            and model.get("timezone") == timezone
        ):
            retained = {
                **model,
                "last_attempt_at": now.isoformat(),
                "last_attempt_source_entity_id": load_entity,
                "last_attempt_timezone": timezone,
                "last_training_status": updated.get("status"),
                "last_training_quality_failures": list(updated.get("quality_failures", []))[:8],
                "last_training_validation": dict(updated.get("validation", {})),
            }
            reason = f"load_forecast_retraining_{updated.get('status', 'failed')}_retained"
            return retained, retained != model, reason
        if updated.get("status") == "ready":
            updated.pop("unusable_since", None)
        else:
            updated["unusable_since"] = (
                model.get("unusable_since")
                if model.get("source_entity_id") == load_entity and model.get("unusable_since")
                else now.isoformat()
            )
    except Exception as err:  # noqa: BLE001 - Recorder failures retain the last safe model.
        return attempted, attempted != model, f"load_forecast_recorder_unavailable:{err.__class__.__name__}"
    reason = f"load_forecast_{updated.get('status', 'failed')}"
    return updated, updated != model, reason


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


def _load_household_forecast_states(
    hass: HomeAssistant,
    load_entity: str,
    ev_entity: str | None,
    hvac_entity: str | None,
    start_time: datetime,
    end_time: datetime,
) -> dict[str, list[Any]]:
    """Load only the raw state changes needed to train the aggregate model."""
    history = import_module("homeassistant.components.recorder.history")
    state_changes = history.state_changes_during_period

    def _states(entity_id: str | None) -> list[Any]:
        if not entity_id:
            return []
        return list(
            state_changes(
                hass,
                start_time,
                end_time,
                entity_id=entity_id,
                no_attributes=True,
                include_start_time_state=True,
                significant_changes_only=False,
            ).get(entity_id, [])
        )

    return {
        "load": _states(load_entity),
        "ev_charging": _states(ev_entity),
        "hvac_power": _states(hvac_entity),
    }


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


def _state_unit(state: Any) -> str:
    attributes = getattr(state, "attributes", {}) or {}
    return str(attributes.get("unit_of_measurement") or attributes.get("unit") or "")


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
