"""Optional Recorder import helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib import import_module
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_DAIKIN_POWER,
    CONF_EV_CHARGING,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
    STATE_UNKNOWN_VALUES,
)
from .ev import build_ev_charge_calibration, ev_charge_calibration_matches
from .load_forecast import (
    FORECAST_CONTRACT_VERSION,
    HISTORY_LOOKBACK,
    MODEL_VERSION,
    build_load_forecast_model_from_buckets,
    clean_load_history_buckets,
    training_due,
)

RECORDER_IMPORT_INTERVAL = timedelta(hours=24)
RECORDER_IMPORT_LOOKBACK = timedelta(days=30)
MAX_LOAD_FORECAST_STATES_PER_ENTITY_CHUNK = 100_000
INITIAL_LOAD_FORECAST_CHUNK_DAYS = 7


class LoadForecastHistoryLimitError(RuntimeError):
    """Raised when one bounded Recorder chunk is still too dense to process safely."""


class LoadForecastRecorderError(RuntimeError):
    """Raised when Recorder cannot serve a bounded history query."""


def load_forecast_source_available(state: Any) -> bool:
    """Return whether the mapped household-load source has a usable state."""
    value = getattr(state, "state", None) if state is not None else None
    return value is not None and str(value).strip().lower() not in STATE_UNKNOWN_VALUES


async def async_update_builtin_load_forecast(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    model: dict[str, Any],
    *,
    now: datetime,
    timezone: str,
    force: bool = False,
    bypass_conservative_bound_gate: bool = False,
) -> tuple[dict[str, Any], bool, str]:
    """Train the compact household-load model when its bounded refresh is due."""
    load_entity = str(entry_data.get(CONF_HOUSEHOLD_LOAD, "") or "").strip()
    if not load_entity:
        return model, False, "load_forecast_household_load_not_configured"
    load_state = hass.states.get(load_entity)
    if not load_forecast_source_available(load_state):
        # Home Assistant may set up this config entry before the mapped source
        # integration has restored its entities. This is not a training
        # attempt: preserve the stored model and let the source state-change
        # refresh retry immediately instead of starting the six-hour backoff.
        return model, False, "load_forecast_household_load_unavailable"
    if not force and not training_due(
        model,
        now=now,
        source_entity_id=load_entity,
        timezone=timezone,
        bypass_conservative_bound_gate=bypass_conservative_bound_gate,
    ):
        return model, False, "load_forecast_training_recent"
    model_contract_current = (
        model.get("model_version") == MODEL_VERSION
        and model.get("contract_version") == FORECAST_CONTRACT_VERSION
    )
    model_policy_current = (
        model.get("safety_gates_bypassed") is bypass_conservative_bound_gate
    )
    attempted = {
        **model,
        "last_attempt_at": now.isoformat(),
        "last_attempt_source_entity_id": load_entity,
        "last_attempt_timezone": timezone,
    }
    if not model_contract_current:
        attempted.update(
            {
                "model_version": MODEL_VERSION,
                "contract_version": FORECAST_CONTRACT_VERSION,
                "status": "failed",
                "quality_ready": False,
                "quality_failures": ["training_pending"],
                "source_entity_id": load_entity,
                "timezone": timezone,
                "profiles": {},
                "safety_gates_bypassed": bypass_conservative_bound_gate,
            }
        )
    load_unit = _state_unit(load_state)
    ev_entity = str(entry_data.get(CONF_EV_CHARGING, "") or "").strip() or None
    hvac_entity = str(entry_data.get(CONF_DAIKIN_POWER, "") or "").strip() or None
    hvac_state = hass.states.get(hvac_entity) if hvac_entity else None
    try:
        executor = _recorder_executor(hass)
        updated = await executor(
            _build_household_load_forecast_model,
            hass,
            load_entity,
            ev_entity,
            hvac_entity,
            now,
            timezone,
            load_unit,
            _state_unit(hvac_state),
            bypass_conservative_bound_gate,
        )
        if (
            updated.get("status") != "ready"
            and model_contract_current
            and model_policy_current
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
        failure = (
            "history_limit_exceeded"
            if isinstance(err, LoadForecastHistoryLimitError)
            else "recorder_unavailable"
            if isinstance(err, LoadForecastRecorderError)
            else "training_error"
        )
        attempted.update(
            {
                "last_training_status": "failed",
                "last_training_quality_failures": [failure],
                "last_training_validation": {},
            }
        )
        if not model_contract_current:
            attempted["quality_failures"] = [failure]
        if not (
            model.get("quality_ready") is True
            and model.get("source_entity_id") == load_entity
            and model.get("timezone") == timezone
        ):
            attempted["unusable_since"] = (
                model.get("unusable_since")
                if model.get("source_entity_id") == load_entity and model.get("unusable_since")
                else now.isoformat()
            )
        return attempted, attempted != model, f"load_forecast_{failure}:{err.__class__.__name__}"
    reason = f"load_forecast_{updated.get('status', 'failed')}"
    return updated, updated != model, reason


async def async_update_ev_charge_calibration(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    model: dict[str, Any],
    *,
    charge_rate_kw: float,
    now: datetime,
) -> tuple[dict[str, Any], bool, str]:
    """Train effective EV SOC-per-kWh from Recorder charging history."""
    charging_entity = str(entry_data.get(CONF_EV_CHARGING, "") or "").strip()
    soc_entity = str(entry_data.get(CONF_EV_SOC, "") or "").strip()
    if not charging_entity or not soc_entity:
        return model, False, "ev_charge_calibration_entities_not_configured"
    if charge_rate_kw <= 0:
        return model, False, "ev_charge_calibration_charge_rate_invalid"
    if not _ev_charge_calibration_due(
        model,
        now=now,
        charging_entity=charging_entity,
        soc_entity=soc_entity,
        charge_rate_kw=charge_rate_kw,
    ):
        return model, False, "ev_charge_calibration_recent"
    try:
        executor = _recorder_executor(hass)
        charging_states, soc_states = await executor(
            _load_recorder_states,
            hass,
            charging_entity,
            soc_entity,
            now - RECORDER_IMPORT_LOOKBACK,
            now,
        )
        updated = build_ev_charge_calibration(
            charging_states,
            soc_states,
            charge_rate_kw=charge_rate_kw,
            trained_at=now,
            charging_entity_id=charging_entity,
            soc_entity_id=soc_entity,
        )
    except Exception as err:  # noqa: BLE001 - retain the last safe calibration.
        return model, False, f"ev_charge_calibration_unavailable:{err.__class__.__name__}"
    if updated.get("status") != "ready" and _ev_charge_calibration_matches(
        model,
        charging_entity=charging_entity,
        soc_entity=soc_entity,
        charge_rate_kw=charge_rate_kw,
    ) and model.get("status") == "ready":
        retained = {
            **model,
            "last_attempt_at": now.isoformat(),
            "last_attempt_status": updated.get("status"),
            "last_attempt_sample_count": updated.get("sample_count", 0),
        }
        return retained, retained != model, "ev_charge_calibration_insufficient_history_retained"
    return updated, updated != model, f"ev_charge_calibration_{updated.get('status', 'failed')}"


def _load_recorder_states(
    hass: HomeAssistant,
    charging_entity: str,
    soc_entity: str,
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[Any], list[Any]]:
    history = import_module("homeassistant.components.recorder.history")
    state_changes = history.state_changes_during_period
    charging = state_changes(
        hass,
        start_time,
        end_time,
        entity_id=charging_entity,
        no_attributes=True,
        include_start_time_state=True,
    ).get(charging_entity, [])
    soc = state_changes(
        hass,
        start_time,
        end_time,
        entity_id=soc_entity,
        no_attributes=True,
        include_start_time_state=True,
    ).get(soc_entity, [])
    return list(charging), list(soc)


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

    def _states(entity_id: str | None, *, no_attributes: bool) -> list[Any]:
        if not entity_id:
            return []
        return list(
            state_changes(
                hass,
                start_time,
                end_time,
                entity_id=entity_id,
                no_attributes=no_attributes,
                include_start_time_state=True,
                limit=MAX_LOAD_FORECAST_STATES_PER_ENTITY_CHUNK,
            ).get(entity_id, [])
        )

    return {
        "load": _states(load_entity, no_attributes=False),
        "ev_charging": _states(ev_entity, no_attributes=True),
        "hvac_power": _states(hvac_entity, no_attributes=False),
    }


def _build_household_load_forecast_model(
    hass: HomeAssistant,
    load_entity: str,
    ev_entity: str | None,
    hvac_entity: str | None,
    now: datetime,
    timezone: str,
    load_unit: str,
    hvac_power_unit: str,
    bypass_conservative_bound_gate: bool = False,
) -> dict[str, Any]:
    """Query, clean, and compact Recorder history one bounded UTC chunk at a time."""
    history_start = now - HISTORY_LOOKBACK
    chunk_start = history_start
    preferred_chunk_days = INITIAL_LOAD_FORECAST_CHUNK_DAYS
    effective_start: datetime | None = None
    buckets: list[tuple[datetime, float]] = []
    ev_intervals_excluded = False
    hvac_power_subtracted = False
    while chunk_start < now:
        chunk_end = _next_load_forecast_chunk_end(chunk_start, now, preferred_chunk_days)
        while True:
            try:
                histories = _load_household_forecast_states(
                    hass,
                    load_entity,
                    ev_entity,
                    hvac_entity,
                    chunk_start,
                    chunk_end,
                )
            except Exception as err:  # noqa: BLE001 - preserve the query boundary for notification policy.
                raise LoadForecastRecorderError from err
            dense_entity = next(
                (
                    entity_id
                    for entity_id, states in (
                        (load_entity, histories["load"]),
                        (ev_entity, histories["ev_charging"]),
                        (hvac_entity, histories["hvac_power"]),
                    )
                    if entity_id and len(states) >= MAX_LOAD_FORECAST_STATES_PER_ENTITY_CHUNK
                ),
                None,
            )
            duration = chunk_end - chunk_start
            if dense_entity is None:
                break
            if duration <= timedelta(days=1):
                raise LoadForecastHistoryLimitError(dense_entity)
            chunk_days = max(int(duration.total_seconds() // timedelta(days=1).total_seconds()), 1)
            preferred_chunk_days = max(chunk_days // 2, 1)
            chunk_end = min(chunk_start + timedelta(days=preferred_chunk_days), now)
        chunk_buckets, chunk_effective_start, ev_excluded, hvac_subtracted = clean_load_history_buckets(
            histories["load"],
            load_unit=load_unit,
            ev_charging_states=histories["ev_charging"],
            hvac_power_states=histories["hvac_power"],
            hvac_power_unit=hvac_power_unit,
            start=chunk_start,
            end=chunk_end,
            timezone=timezone,
        )
        buckets.extend(chunk_buckets)
        if effective_start is None and chunk_effective_start is not None:
            effective_start = chunk_effective_start
        ev_intervals_excluded = ev_intervals_excluded or ev_excluded
        hvac_power_subtracted = hvac_power_subtracted or hvac_subtracted
        chunk_start = chunk_end
    return build_load_forecast_model_from_buckets(
        buckets,
        now=now,
        timezone=timezone,
        source_entity_id=load_entity,
        history_start=effective_start or history_start,
        ev_intervals_excluded=ev_intervals_excluded,
        hvac_power_subtracted=hvac_power_subtracted,
        bypass_conservative_bound_gate=bypass_conservative_bound_gate,
    )


def _next_load_forecast_chunk_end(start: datetime, end: datetime, preferred_days: int) -> datetime:
    """Return an aligned bounded query end, preserving complete UTC buckets."""
    if any((start.hour, start.minute, start.second, start.microsecond)):
        next_midnight = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return min(next_midnight, end)
    return min(start + timedelta(days=max(preferred_days, 1)), end)


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


def _ev_charge_calibration_due(
    model: dict[str, Any],
    *,
    now: datetime,
    charging_entity: str,
    soc_entity: str,
    charge_rate_kw: float,
) -> bool:
    if not _ev_charge_calibration_matches(
        model,
        charging_entity=charging_entity,
        soc_entity=soc_entity,
        charge_rate_kw=charge_rate_kw,
    ):
        return True
    trained_at = model.get("last_attempt_at") or model.get("trained_at")
    if not isinstance(trained_at, str):
        return True
    try:
        parsed = datetime.fromisoformat(trained_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return now >= _align_timestamp(parsed, now) + RECORDER_IMPORT_INTERVAL


def _ev_charge_calibration_matches(
    model: dict[str, Any],
    *,
    charging_entity: str,
    soc_entity: str,
    charge_rate_kw: float,
) -> bool:
    return ev_charge_calibration_matches(
        model,
        charging_entity_id=charging_entity,
        soc_entity_id=soc_entity,
        charge_rate_kw=charge_rate_kw,
    )


def _align_timestamp(value: datetime, now: datetime) -> datetime:
    if value.tzinfo is None and now.tzinfo is not None:
        return value.replace(tzinfo=now.tzinfo)
    if value.tzinfo is not None and now.tzinfo is None:
        return value.replace(tzinfo=None)
    return value
