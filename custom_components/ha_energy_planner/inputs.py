"""Input normalization for Energy Planner."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_BASELINE_LOAD_FORECAST,
    CONF_BASELINE_LOAD_OBSERVED,
    CONF_BATTERY_SOC,
    CONF_CARBON_INTENSITY_FORECAST,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_FORECAST_FRESHNESS_MINUTES,
    CONF_PERSON_ENTITIES,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_PRICE_FRESHNESS_MINUTES,
    CONF_PV_FORECAST,
    CONF_PV_OBSERVED,
    CONF_WEATHER,
    DEFAULT_ENPHASE_AI_PROFILE,
    DEFAULT_ENPHASE_FULL_BACKUP_PROFILE,
    DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE,
    STATE_UNKNOWN_VALUES,
)
from .ev import summarize_stored_trip_history
from .forecast_calibration import apply_forecast_calibration
from .forecasts import (
    forecast_coverage_ratio,
    forecast_series_from_state,
    latest_forecast_valid_at_from_state,
    normalize_scalar_value,
)
from .models import DecisionContext, DecisionSlot, HAEOStatus, InputHealth, OccupancyState, Override

_CALIBRATION_FIELDS_BY_CONFIG = {
    CONF_PV_FORECAST: "pv_forecast_kw",
    CONF_BASELINE_LOAD_FORECAST: "baseline_load_forecast_kw",
}
_OBSERVATION_FIELDS_BY_CONFIG = {
    CONF_PV_OBSERVED: "pv_forecast_kw",
    CONF_BASELINE_LOAD_OBSERVED: "baseline_load_forecast_kw",
}
_FORECAST_VALUE_KEYS_BY_CONFIG = {
    CONF_PV_FORECAST: ("pv_forecast_kw", "pv_estimate", "estimate", "power", "watts", "value"),
    CONF_BASELINE_LOAD_FORECAST: ("baseline_load_forecast_kw", "load_kw", "load", "power", "watts", "value"),
    CONF_CARBON_INTENSITY_FORECAST: (
        "carbon_intensity_g_per_kwh",
        "carbon_intensity",
        "forecast",
        "value",
    ),
}
_OPTIONAL_NUMERIC_KINDS_BY_CONFIG = {
    CONF_DAIKIN_POWER: ("power", "power"),
    CONF_PV_FORECAST: ("power", "power"),
    CONF_BASELINE_LOAD_FORECAST: ("power", "power"),
    CONF_PV_OBSERVED: ("power", "power"),
    CONF_BASELINE_LOAD_OBSERVED: ("power", "power"),
}
_POINT_SENSOR_CONFIDENCE = 0.7


class InputManager:
    """Build normalized planner inputs from configured Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: Mapping[str, Any],
        options: Mapping[str, Any],
        trip_history: Mapping[str, Any] | None = None,
        forecast_calibration: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize input manager."""
        self.hass = hass
        self.entry_data = entry_data
        self.options = options
        self.trip_history = dict(trip_history or {})
        self.forecast_calibration = dict(forecast_calibration or {})
        self.forecast_training_slots: list[dict[str, Any]] = []
        self.forecast_confidence_details: list[dict[str, Any]] = []
        self._raw_forecast_series: dict[str, list[float | None]] = {}
        self._conservative_forecast_series: dict[str, list[float | None]] = {}
        self._forecast_source_issued_at: dict[str, datetime] = {}
        self._forecast_confidence_scores: list[float] = []
        self._state_cache: dict[str, State | None] = {}

    def build_context(self, overrides: list[Override] | None = None) -> DecisionContext:
        """Build the current 24-hour decision context."""
        now = dt_util.utcnow()
        self._forecast_confidence_scores = []
        self.forecast_confidence_details = []
        interval = int(self.options[CONF_PLANNING_INTERVAL_MINUTES])
        horizon = int(self.options[CONF_PLANNING_HORIZON_HOURS])
        import_prices, import_issue = self._required_series(
            CONF_AMBER_IMPORT_PRICE,
            ("import_price", "general_price", "per_kwh", "price", "value"),
            "price",
            now,
            horizon,
            interval,
        )
        export_prices, export_issue = self._required_series(
            CONF_AMBER_EXPORT_PRICE,
            ("export_price", "feed_in_price", "per_kwh", "price", "value"),
            "price",
            now,
            horizon,
            interval,
        )
        pv_forecasts, pv_issue = self._required_series(
            CONF_PV_FORECAST,
            _FORECAST_VALUE_KEYS_BY_CONFIG[CONF_PV_FORECAST],
            "power",
            now,
            horizon,
            interval,
        )
        baseline_loads, load_issue = self._required_series(
            CONF_BASELINE_LOAD_FORECAST,
            _FORECAST_VALUE_KEYS_BY_CONFIG[CONF_BASELINE_LOAD_FORECAST],
            "power",
            now,
            horizon,
            interval,
        )
        carbon_intensities, carbon_issue = self._optional_series(
            CONF_CARBON_INTENSITY_FORECAST,
            _FORECAST_VALUE_KEYS_BY_CONFIG[CONF_CARBON_INTENSITY_FORECAST],
            "carbon_intensity",
            now,
            horizon,
            interval,
        )
        training_indices = _forecast_training_indices(horizon, interval)
        self.forecast_training_slots = [
            {
                "valid_at": now + timedelta(minutes=offset),
                "pv_forecast_kw_issued_at": self._forecast_source_issued_at.get("pv_forecast_kw"),
                "pv_forecast_kw": _series_value(self._raw_forecast_series.get("pv_forecast_kw", pv_forecasts), index),
                "baseline_load_forecast_kw_issued_at": self._forecast_source_issued_at.get(
                    "baseline_load_forecast_kw"
                ),
                "baseline_load_forecast_kw": _series_value(
                    self._raw_forecast_series.get("baseline_load_forecast_kw", baseline_loads),
                    index,
                ),
            }
            for index in training_indices
            for offset in (index * interval,)
        ]
        battery_soc, battery_issue = self._numeric_state(CONF_BATTERY_SOC)
        ev_soc, ev_issue = self._optional_numeric_state(CONF_EV_SOC)
        ev_connected, ev_connected_issue = self._optional_bool_state(CONF_EV_CONNECTED)
        ev_target_soc, ev_target_soc_issue = self._optional_soc_state(CONF_EV_SMART_CHARGING_TARGET_SOC)
        ev_ready_by, ev_ready_by_issue = self._optional_ready_by_state(CONF_EV_SMART_CHARGING_READY_BY)
        enphase_profile, enphase_profile_issue = self._optional_string_state(CONF_ENPHASE_PROFILE)
        hvac_mode, hvac_temperature, hvac_issue = self._optional_climate_state(CONF_DAIKIN_CLIMATE)
        hvac_power, hvac_power_issue = self._optional_numeric_state(CONF_DAIKIN_POWER)
        outdoor_temperature, weather_forecasts, weather_issue = self._optional_weather_temperatures(
            CONF_WEATHER,
            now,
            horizon,
            interval,
        )
        comfort_low, comfort_low_issue = self._optional_numeric_state(CONF_CLIMATE_TARGET_LOW)
        comfort_high, comfort_high_issue = self._optional_numeric_state(CONF_CLIMATE_TARGET_HIGH)

        slots = []
        for index, offset in enumerate(range(0, horizon * 60, interval)):
            slots.append(
                DecisionSlot(
                    valid_at=now + timedelta(minutes=offset),
                    import_price=_series_value(import_prices, index),
                    export_price=_series_value(export_prices, index),
                    pv_forecast_kw=_series_value(pv_forecasts, index),
                    baseline_load_forecast_kw=_series_value(baseline_loads, index),
                    pv_forecast_lower_kw=_series_value(
                        self._conservative_forecast_series.get("pv_forecast_kw", pv_forecasts), index
                    ),
                    baseline_load_forecast_upper_kw=_series_value(
                        self._conservative_forecast_series.get("baseline_load_forecast_kw", baseline_loads), index
                    ),
                    carbon_intensity_g_per_kwh=_series_value(carbon_intensities, index),
                    outdoor_temperature_forecast_c=_series_value(weather_forecasts, index),
                    occupied=None,
                )
            )

        issues = [
            issue
            for issue in [
                import_issue,
                export_issue,
                pv_issue,
                load_issue,
                carbon_issue,
                battery_issue,
                ev_issue,
                ev_connected_issue,
                ev_target_soc_issue,
                ev_ready_by_issue,
                enphase_profile_issue,
                hvac_issue,
                hvac_power_issue,
                weather_issue,
                comfort_low_issue,
                comfort_high_issue,
                *self._freshness_issues(now),
            ]
            if issue
        ]

        occupancy_state = self._occupancy_state()
        if occupancy_state == OccupancyState.UNKNOWN:
            issues.append("occupancy_unknown")
        ev_trip_summary = summarize_stored_trip_history(self.trip_history)

        input_health = self._health_from_issues(issues)
        forecast_confidence = _combined_confidence(self._forecast_confidence_scores)
        return DecisionContext(
            created_at=now,
            plan_id=uuid4().hex,
            slots=slots,
            current_battery_soc_percent=battery_soc,
            current_ev_soc_percent=ev_soc,
            occupancy_state=occupancy_state,
            haeo_status=HAEOStatus.READY if input_health != InputHealth.UNSAFE else HAEOStatus.STALE,
            input_health=input_health,
            current_enphase_profile=enphase_profile,
            enphase_ai_profile=_profile_name(self.entry_data, CONF_ENPHASE_AI_PROFILE, DEFAULT_ENPHASE_AI_PROFILE),
            enphase_arbitrage_profile=_profile_name(
                self.entry_data,
                CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
                DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE,
            ),
            enphase_self_consumption_profile=_profile_name(
                self.entry_data,
                CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
                DEFAULT_ENPHASE_SELF_CONSUMPTION_PROFILE,
            ),
            enphase_full_backup_profile=_profile_name(
                self.entry_data,
                CONF_ENPHASE_FULL_BACKUP_PROFILE,
                DEFAULT_ENPHASE_FULL_BACKUP_PROFILE,
            ),
            current_hvac_mode=hvac_mode,
            current_hvac_temperature_c=hvac_temperature,
            current_hvac_power_kw=hvac_power,
            current_outdoor_temperature_c=outdoor_temperature,
            ev_connected=ev_connected,
            ev_target_soc_percent=ev_target_soc,
            ev_ready_by=ev_ready_by,
            ev_trip_observed_days=ev_trip_summary.observed_days,
            ev_trip_max_daily_soc_percent=ev_trip_summary.max_daily_soc_percent,
            ev_trip_average_daily_soc_percent=ev_trip_summary.average_daily_soc_percent,
            ev_trip_history_sufficient=ev_trip_summary.history_sufficient,
            occupied_temperature_low_c=comfort_low,
            occupied_temperature_high_c=comfort_high,
            active_overrides=overrides or [],
            input_issues=issues,
            forecast_confidence=forecast_confidence,
            local_timezone=str(getattr(getattr(self.hass, "config", None), "time_zone", None) or "UTC"),
        )

    def _numeric_state(self, config_key: str) -> tuple[float | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, f"{config_key}_not_configured"
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, f"{config_key}_unavailable"
        value = _finite_float_or_none(state.state)
        if value is None:
            return None, f"{config_key}_non_numeric"
        return value, None

    def _required_series(
        self,
        config_key: str,
        value_keys: tuple[str, ...],
        value_kind: str,
        now: datetime,
        horizon: int,
        interval: int,
    ) -> tuple[list[float | None], str | None]:
        entity_id = self.entry_data.get(config_key)
        slot_count = int((horizon * 60) / interval)
        if not entity_id:
            return [None] * slot_count, f"{config_key}_not_configured"
        state = self._state(entity_id)
        if not self._valid_state(state):
            return [None] * slot_count, f"{config_key}_unavailable"
        calibration_field = _CALIBRATION_FIELDS_BY_CONFIG.get(config_key)
        source_issued_at = _forecast_source_issued_at(state, now)
        if calibration_field:
            self._forecast_source_issued_at[calibration_field] = source_issued_at

        forecast = forecast_series_from_state(
            state,
            issued_at=now,
            horizon_hours=horizon,
            interval_minutes=interval,
            value_keys=value_keys,
            value_kind=value_kind,
        )
        if forecast:
            coverage = forecast_coverage_ratio(forecast)
            self._record_forecast_confidence(
                _state_confidence(state, default=1.0) * coverage,
                config_key=config_key,
                entity_id=str(entity_id),
                source="forecast_series" if coverage == 1.0 else "forecast_series_partial",
            )
            padded = list(forecast[:slot_count])
            if calibration_field:
                self._raw_forecast_series[calibration_field] = list(padded)
                lead_offset_minutes = (now - source_issued_at).total_seconds() / 60
                if lead_offset_minutes >= 0:
                    uncertainty_mode = "lower" if calibration_field == "pv_forecast_kw" else "upper"
                    self._conservative_forecast_series[calibration_field] = apply_forecast_calibration(
                        padded,
                        self.forecast_calibration,
                        calibration_field,
                        interval_minutes=interval,
                        lead_offset_minutes=lead_offset_minutes,
                        uncertainty_mode=uncertainty_mode,
                    )
                    padded = apply_forecast_calibration(
                        padded,
                        self.forecast_calibration,
                        calibration_field,
                        interval_minutes=interval,
                        lead_offset_minutes=lead_offset_minutes,
                    )
            issue = None if coverage == 1.0 else f"{config_key}_incomplete_horizon"
            return padded, issue

        value = _finite_float_or_none(state.state)
        if value is None:
            self._record_forecast_confidence(
                0.0,
                config_key=config_key,
                entity_id=str(entity_id),
                source="invalid_state",
            )
            return [None] * slot_count, f"{config_key}_non_numeric"
        attributes = getattr(state, "attributes", {}) or {}
        unit = str(_attribute_value(attributes, "unit_of_measurement", "unit") or "")
        value = normalize_scalar_value(value, value_kind=value_kind, value_key=value_keys[0], unit=unit)
        self._record_forecast_confidence(
            _POINT_SENSOR_CONFIDENCE,
            config_key=config_key,
            entity_id=str(entity_id),
            source="point_value_repeated",
        )
        return [value] * slot_count, None

    def _optional_series(
        self,
        config_key: str,
        value_keys: tuple[str, ...],
        value_kind: str,
        now: datetime,
        horizon: int,
        interval: int,
    ) -> tuple[list[float | None], str | None]:
        """Return an optional forecast series without penalizing absent configuration."""
        if not self.entry_data.get(config_key):
            return [None] * int((horizon * 60) / interval), None
        return self._required_series(config_key, value_keys, value_kind, now, horizon, interval)

    def _optional_numeric_state(self, config_key: str) -> tuple[float | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, f"{config_key}_unavailable"
        value = _finite_float_or_none(state.state)
        if value is None:
            return None, f"{config_key}_non_numeric"
        value_kind = _OPTIONAL_NUMERIC_KINDS_BY_CONFIG.get(config_key)
        if value_kind is not None:
            attributes = getattr(state, "attributes", {}) or {}
            unit = str(_attribute_value(attributes, "unit_of_measurement", "unit") or "")
            value = normalize_scalar_value(value, value_kind=value_kind[0], value_key=value_kind[1], unit=unit)
        return value, None

    def _optional_bool_state(self, config_key: str) -> tuple[bool | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, f"{config_key}_unavailable"
        value = str(state.state).lower()
        if value in {
            "on",
            "true",
            "1",
            "connected",
            "charging",
            "home",
            "plugged_in",
            "connected_not_charging",
            "fully_charged",
        }:
            return True, None
        if value in {
            "off",
            "false",
            "0",
            "disconnected",
            "not_home",
            "idle",
            "unplugged",
            "not_plugged_in",
            "vehicle_not_connected",
        }:
            return False, None
        return None, f"{config_key}_unsupported_state"

    def _optional_soc_state(self, config_key: str) -> tuple[float | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, f"{config_key}_unavailable"
        value = _percent_float_or_none(state.state)
        if value is None:
            return None, f"{config_key}_non_numeric"
        if value < 0 or value > 100:
            return None, f"{config_key}_out_of_range"
        return value, None

    def _optional_ready_by_state(self, config_key: str) -> tuple[str | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, f"{config_key}_unavailable"
        ready_by = _ready_by_time_or_none(state.state)
        if ready_by is None:
            if str(state.state).strip().lower() in {"", "none", "unknown", "unavailable"}:
                return None, None
            return None, f"{config_key}_invalid_time"
        return ready_by, None

    def _optional_string_state(self, config_key: str) -> tuple[str | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, f"{config_key}_unavailable"
        return str(state.state), None

    def _optional_climate_state(self, config_key: str) -> tuple[str | None, float | None, str | None]:
        entity_id = self.entry_data.get(config_key)
        if not entity_id:
            return None, None, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, None, f"{config_key}_unavailable"
        attributes = getattr(state, "attributes", {}) or {}
        temperature = _finite_float_or_none(_attribute_value(attributes, "current_temperature"))
        return str(state.state), temperature, None

    def _optional_weather_temperatures(
        self,
        config_key: str,
        now: datetime,
        horizon: int,
        interval: int,
    ) -> tuple[float | None, list[float | None], str | None]:
        entity_id = self.entry_data.get(config_key)
        slot_count = int((horizon * 60) / interval)
        if not entity_id:
            return None, [None] * slot_count, None
        state = self._state(entity_id)
        if not self._valid_state(state):
            return None, [None] * slot_count, f"{config_key}_unavailable"
        attributes = getattr(state, "attributes", {}) or {}
        value = _attribute_value(attributes, "temperature", "native_temperature", "current_temperature", "temp")
        if value is None:
            value = state.state
        current_temperature = _finite_float_or_none(value)
        if current_temperature is not None:
            unit = str(_attribute_value(attributes, "unit_of_measurement", "unit", "temperature_unit") or "")
            current_temperature = normalize_scalar_value(current_temperature, value_kind="temperature", unit=unit)

        forecast = forecast_series_from_state(
            state,
            issued_at=now,
            horizon_hours=horizon,
            interval_minutes=interval,
            value_keys=("outdoor_temperature_forecast_c", "temperature", "native_temperature", "temp", "value"),
            value_kind="temperature",
        )
        if forecast:
            coverage = forecast_coverage_ratio(forecast)
            self._record_forecast_confidence(
                _state_confidence(state, default=1.0) * coverage,
                config_key=config_key,
                entity_id=str(entity_id),
                source="forecast_series" if coverage == 1.0 else "forecast_series_partial",
            )
            padded = list(forecast[:slot_count])
            issue = None if coverage == 1.0 else f"{config_key}_incomplete_horizon"
            return current_temperature, padded, issue
        if current_temperature is not None:
            self._record_forecast_confidence(
                _POINT_SENSOR_CONFIDENCE,
                config_key=config_key,
                entity_id=str(entity_id),
                source="point_value_repeated",
            )
            return current_temperature, [current_temperature] * slot_count, None
        self._record_forecast_confidence(
            0.0,
            config_key=config_key,
            entity_id=str(entity_id),
            source="invalid_state",
        )
        return None, [None] * slot_count, f"{config_key}_non_numeric_temperature"

    def _freshness_issues(self, now: datetime) -> list[str]:
        issues: list[str] = []
        price_timeout = timedelta(minutes=int(self.options[CONF_PRICE_FRESHNESS_MINUTES]))
        forecast_timeout = timedelta(minutes=int(self.options[CONF_FORECAST_FRESHNESS_MINUTES]))
        for key in (CONF_AMBER_IMPORT_PRICE, CONF_AMBER_EXPORT_PRICE):
            entity_id = self.entry_data.get(key)
            state = self._state(entity_id) if entity_id else None
            if state and now - state.last_updated > price_timeout:
                issues.append(f"{key}_stale")
        for key in (CONF_PV_FORECAST, CONF_BASELINE_LOAD_FORECAST, CONF_CARBON_INTENSITY_FORECAST):
            entity_id = self.entry_data.get(key)
            state = self._state(entity_id) if entity_id else None
            if state and now - state.last_updated > forecast_timeout and not _has_current_forecast_data(
                state,
                now,
                _FORECAST_VALUE_KEYS_BY_CONFIG[key],
            ):
                issues.append(f"{key}_stale")
        return issues

    def current_forecast_observations(self) -> dict[str, dict[str, Any] | None]:
        """Return timestamped observed power from dedicated ground-truth entities."""
        observations: dict[str, dict[str, Any] | None] = {}
        for config_key, field in _OBSERVATION_FIELDS_BY_CONFIG.items():
            value, _issue = self._optional_numeric_state(config_key)
            entity_id = self.entry_data.get(config_key)
            state = self._state(entity_id) if entity_id else None
            observed_at = getattr(state, "last_updated", None)
            observations[field] = (
                {"value": value, "observed_at": observed_at}
                if value is not None and isinstance(observed_at, datetime)
                else None
            )
        return observations

    def thermal_sample(self, context: DecisionContext) -> dict[str, Any]:
        """Return compact current thermal-model sample."""
        return {
            "sampled_at": context.created_at,
            "hvac_mode": context.current_hvac_mode,
            "indoor_temperature_c": context.current_hvac_temperature_c,
            "outdoor_temperature_c": context.current_outdoor_temperature_c,
            "hvac_power_kw": context.current_hvac_power_kw,
        }

    def _occupancy_state(self) -> OccupancyState:
        entity_ids = self._list_from_config(CONF_PERSON_ENTITIES)
        if not entity_ids:
            return OccupancyState.UNKNOWN
        states = [self._state(entity_id) for entity_id in entity_ids]
        if any(not self._valid_state(state) for state in states):
            return OccupancyState.UNKNOWN
        if any(state and state.state == "home" for state in states):
            return OccupancyState.OCCUPIED
        return OccupancyState.AWAY

    def _list_from_config(self, key: str) -> list[str]:
        value = self.entry_data.get(key)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []

    def _state(self, entity_id: Any) -> State | None:
        """Return a cached Home Assistant state for this refresh."""
        cache_key = str(entity_id)
        if cache_key not in self._state_cache:
            self._state_cache[cache_key] = self.hass.states.get(cache_key)
        return self._state_cache[cache_key]

    @staticmethod
    def _valid_state(state: State | None) -> bool:
        return state is not None and state.state not in STATE_UNKNOWN_VALUES

    @staticmethod
    def _health_from_issues(issues: list[str]) -> InputHealth:
        required_fragments = ("import_price", "export_price", "pv_forecast", "baseline_load", "battery_soc")
        if any(any(fragment in issue for fragment in required_fragments) for issue in issues):
            return InputHealth.UNSAFE
        if issues:
            return InputHealth.DEGRADED
        return InputHealth.HEALTHY

    def _record_forecast_confidence(
        self,
        value: float,
        *,
        config_key: str | None = None,
        entity_id: str | None = None,
        source: str = "unknown",
    ) -> None:
        confidence = _clamp_confidence(value)
        self._forecast_confidence_scores.append(confidence)
        if config_key is not None:
            self.forecast_confidence_details.append(
                {
                    "config_key": config_key,
                    "entity_id": entity_id,
                    "source": source,
                    "confidence": confidence,
                }
            )


def _series_value(series: list[float | None], index: int) -> float | None:
    if not series:
        return None
    if index < len(series):
        return series[index]
    return series[-1]


def _forecast_training_indices(horizon_hours: int, interval_minutes: int) -> list[int]:
    """Sample forecast leads across the full horizon without growing snapshots."""
    slot_count = max(int(horizon_hours * 60 / interval_minutes), 0)
    if slot_count == 0:
        return []
    target_minutes = (0, 30, 60, 120, 240, 480, 720, 1080, horizon_hours * 60 - interval_minutes)
    return sorted(
        set(range(min(12, slot_count)))
        | {
            min(max(round(minutes / interval_minutes), 0), slot_count - 1)
            for minutes in target_minutes
            if minutes >= 0
        }
    )


def _profile_name(data: Mapping[str, Any], key: str, default: str) -> str | None:
    value = str(data.get(key) or default).strip()
    return value or None


def _finite_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _percent_float_or_none(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
        if "," in value and "." not in value:
            value = value.replace(",", ".")
    return _finite_float_or_none(value)


def _ready_by_time_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return f"{parsed.hour:02d}:{parsed.minute:02d}"
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?", text)
    if match is None:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def _attribute_value(attributes: Mapping[str, Any], *keys: str) -> Any:
    if not attributes:
        return None
    canonical = {str(key): value for key, value in attributes.items()}
    for key, value in attributes.items():
        canonical.setdefault(_canonical_key(key), value)
    for key in keys:
        if key in canonical:
            return canonical[key]
        canonical_key = _canonical_key(key)
        if canonical_key in canonical:
            return canonical[canonical_key]
    return None


def _forecast_source_issued_at(state: State, fallback: datetime) -> datetime:
    """Return stable per-source issue time, preferring payload metadata."""
    attributes = getattr(state, "attributes", {}) or {}
    raw = _attribute_value(
        attributes,
        "forecast_issued_at",
        "issued_at",
        "generated_at",
        "generated_time",
        "forecast_generated_at",
    )
    parsed: datetime | None = None
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        parsed = dt_util.parse_datetime(raw)
    state_updated = getattr(state, "last_updated", None)
    value = parsed or (state_updated if isinstance(state_updated, datetime) else fallback)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return dt_util.as_utc(value)


def _canonical_key(value: Any) -> str:
    raw = str(value)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    separated = re.sub(r"[^0-9A-Za-z]+", "_", separated)
    return separated.strip("_").lower()


def _state_confidence(state: State, *, default: float) -> float:
    attributes = getattr(state, "attributes", {}) or {}
    for key in ("confidence", "confidence_percent", "forecast_confidence", "forecast_confidence_percent"):
        if key not in attributes:
            continue
        number = _finite_float_or_none(attributes[key])
        if number is None:
            continue
        if number > 1:
            number /= 100
        return _clamp_confidence(number)
    return default


def _combined_confidence(values: list[float]) -> float:
    if not values:
        return 1.0
    return round(min(values), 4)


def _has_current_forecast_data(state: State, now: datetime, value_keys: tuple[str, ...]) -> bool:
    latest_valid_at = latest_forecast_valid_at_from_state(state, value_keys=value_keys)
    return latest_valid_at is not None and latest_valid_at >= now


def _clamp_confidence(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)
