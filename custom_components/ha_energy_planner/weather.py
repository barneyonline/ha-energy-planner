"""Bounded advisory weather service and forecast normalization."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .adapter_helpers import service_entity_available
from .climate_inputs import finite
from .const import CONF_FORECAST_FRESHNESS_MINUTES, CONF_PLANNING_INTERVAL_MINUTES, CONF_WEATHER

WEATHER_FORECAST_TIMEOUT_SECONDS = 10.0
WEATHER_OUTAGE_GRACE_SECONDS = 15 * 60
WEATHER_RECOVERY_SECONDS = 5 * 60
WEATHER_TEMPERATURE_KEYS = ("outdoor_temperature_forecast_c", "temperature", "native_temperature", "temp", "value")
_LOGGER = logging.getLogger(__name__)

class WeatherForecastOwner(Protocol):
    """In-memory cache owned by one coordinator with serialized advisory fetches."""
    hass: HomeAssistant
    _weather_fetch_lock: asyncio.Lock | None
    _weather_forecast_cache: dict[str, Any]
    weather_forecast_diagnostics: dict[str, Any]

async def async_weather_forecast(
    self: WeatherForecastOwner, entry_data: dict[str, Any], options: dict[str, Any],
    *, now: datetime, force: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Serialize prefetches so late failures cannot replace newer good evidence."""
    lock = getattr(self, "_weather_fetch_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        self._weather_fetch_lock = lock
    async with lock:
        return await _async_weather_forecast(self, entry_data, options, now=now, force=force)


async def _async_weather_forecast(
    self: WeatherForecastOwner,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    *,
    now: datetime,
    force: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an hourly weather response with bounded cache fallback."""
    prior_details = dict(
        getattr(self, "weather_forecast_diagnostics", {}) or {}
    )

    def result(
        forecast_result: dict[str, Any],
        details: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        effective_details = {**details, "entity_id": entity_id}
        if cache and cache.get("entity_id") == entity_id:
            good_at = _parse_datetime_or_none(cache.get("fetched_at"))
            if good_at is not None:
                expires = _weather_grace_expiry(cache, freshness_minutes)
                effective_details.update(
                    last_good_at=good_at.isoformat(),
                    observation_at=cache.get("observation_at"),
                    original_fetched_at=cache.get("original_fetched_at"),
                    provider_issued_at=cache.get("provider_issued_at"),
                    last_successful_fetch_at=(
                        _parse_datetime_or_none(cache.get("last_success_at")) or good_at).isoformat(),
                    outage_started_at=cache.get("outage_started_at"),
                    recovery_started_at=cache.get("recovery_started_at"),
                    grace_expires_at=expires.isoformat(),
                    grace_remaining_seconds=max(0.0, (expires - now).total_seconds()),
                    forecast_attributes=dict(cache.get("attributes", {})),
                )
        effective_details["availability"] = (
            "cached" if details.get("fetch_status") == "cached_after_error" else
            "unavailable" if details.get("fetch_status") == "failed" else
            "not_configured" if details.get("fetch_status") == "not_configured" else "live"
        )
        if (
            effective_details.get("source_type")
            == "legacy_attributes_or_point_value"
            and prior_details.get("entity_id") == entity_id
            and prior_details.get("source_type")
            in {
                "legacy_entity_attributes",
                "legacy_entity_attributes_partial",
                "point_value_repeated",
                "unavailable_state",
                "invalid_state",
            }
        ):
            for key in (
                "source_type",
                "point_count",
                "coverage_start",
                "coverage_end",
                "classification",
                "covered_hours",
                "continuous_hours",
                "requested_hours",
            ):
                if key in prior_details:
                    effective_details[key] = prior_details[key]
        self.weather_forecast_diagnostics = dict(effective_details)
        return forecast_result, effective_details

    entity_id = str(entry_data.get(CONF_WEATHER) or "").strip()
    freshness_minutes = max(int(options.get(CONF_FORECAST_FRESHNESS_MINUTES, 120)), 0)
    identity = (entity_id, options.get(CONF_PLANNING_INTERVAL_MINUTES),
                options.get(CONF_FORECAST_FRESHNESS_MINUTES),
                str(getattr(getattr(self.hass, "config", None), "time_zone", "UTC")))
    cache = getattr(self, "_weather_forecast_cache", {})
    if cache.get("identity") != identity:
        cache = {}
        self._weather_forecast_cache = cache
    if not entity_id:
        return result(
            {},
            {"fetch_status": "not_configured", "source_type": "none"},
        )

    cached_entity = str(cache.get("entity_id") or "")
    cached_at = _parse_datetime_or_none(cache.get("fetched_at"))
    cached_forecast_value = cache.get("forecast")
    cached_forecast = (
        [dict(item) for item in cached_forecast_value if isinstance(item, dict)]
        if isinstance(cached_forecast_value, list)
        else []
    )
    cache_age_seconds = (
        max((dt_util.as_utc(now) - dt_util.as_utc(cached_at)).total_seconds(), 0.0)
        if cached_at is not None
        else None
    )
    last_success = _parse_datetime_or_none(cache.get("last_success_at")) or cached_at
    success_age = max((now - last_success).total_seconds(), 0.0) if last_success is not None else None
    planning_minutes = max(int(options.get(CONF_PLANNING_INTERVAL_MINUTES, 5)), 1)
    refresh_after_seconds = min(planning_minutes, 15, freshness_minutes) * 60
    cache_matches = (
        cached_entity == entity_id
        and bool(cached_forecast)
        and cache_age_seconds is not None
        and cached_at is not None and cached_at <= now
        and last_success is not None and last_success <= now
    )
    if (
        not force
        and cache_matches
        and cache_age_seconds is not None
        and success_age is not None and success_age < refresh_after_seconds
        and not cache.get("outage_started_at")
        and service_entity_available(self.hass, entity_id, allow_unknown=True)
    ):
        return result(
            {"forecast": list(cached_forecast)},
            _weather_forecast_details(
                fetch_status="cache_hit",
                source_type="weather_service_hourly",
                forecast=cached_forecast,
                cache_age_seconds=cache_age_seconds,
            ),
        )

    fetch_started = monotonic()
    failure_reason: str | None = None
    try:
        # Unknown current conditions do not make a forecast provider unavailable.
        if not service_entity_available(self.hass, entity_id, allow_unknown=True):
            raise ValueError("weather_entity_unavailable")
        async with asyncio.timeout(WEATHER_FORECAST_TIMEOUT_SECONDS):
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        forecast = _weather_forecast_from_response(response, entity_id)
        if not forecast:
            raise ValueError("weather_forecast_response_empty")
        normalized = _normalize_hourly_forecast(
            forecast,
            timezone=str(
                getattr(getattr(self.hass, "config", None), "time_zone", None)
                or "UTC"
            ),
        )
        if not normalized or not any(
            finite(item.get(key)) is not None for item in normalized for key in WEATHER_TEMPERATURE_KEYS
        ):
            raise ValueError("weather_forecast_response_invalid")
        state = self.hass.states.get(entity_id)
        attributes = getattr(state, "attributes", {}) or {}
        # Identical payloads during availability flapping are the same evidence.
        issued = weather_issued_at(attributes, now)
        same_evidence = (cache_matches and normalized == cached_forecast
                         and cache.get("provider_issued_at") == (issued.isoformat() if issued is not None else None))
        good_at = (cached_at if same_evidence and cached_at is not None else
                   min(now, issued) if issued is not None else now)
        if (now - good_at).total_seconds() > freshness_minutes * 60:
            raise ValueError("weather_forecast_response_invalid")
        observation = getattr(state, "last_updated", None)
        recovered = dict(cache)
        if cache.get("outage_started_at"):
            recovering_at = _parse_datetime_or_none(cache.get("recovery_started_at")) or now
            recovered["recovery_started_at"] = recovering_at.isoformat()
            if (now - recovering_at).total_seconds() >= WEATHER_RECOVERY_SECONDS:
                for key in ("outage_started_at", "grace_started_at", "recovery_started_at"):
                    recovered.pop(key, None)
        cache = {
            **{key: recovered[key] for key in ("outage_started_at", "grace_started_at", "recovery_started_at")
               if key in recovered},
            "identity": identity, "entity_id": entity_id, "fetched_at": good_at, "last_success_at": now,
            "original_fetched_at": cache.get("original_fetched_at") if same_evidence else now.isoformat(),
            "provider_issued_at": cache.get("provider_issued_at") if same_evidence else (
                issued.isoformat() if issued is not None else None),
            "observation_at": cache.get("observation_at") if same_evidence else (
                observation.isoformat() if isinstance(observation, datetime) else None),
            "forecast": normalized,
            "attributes": {key: attributes[key] for key in (
                "temperature_unit", "unit_of_measurement", "forecast_interval_minutes",
                "confidence", "confidence_percent", "forecast_confidence", "forecast_confidence_percent",
            ) if key in attributes},
        }
        self._weather_forecast_cache = cache
        return result(
            {"forecast": normalized},
            _weather_forecast_details(
                fetch_status="fetched",
                source_type="weather_service_hourly",
                forecast=normalized,
                cache_age_seconds=max((now - good_at).total_seconds(), 0.0),
            ),
        )
    except Exception as err:  # noqa: BLE001 - weather is advisory and falls back safely.
        failure_reason = _bounded_reason(err)
        _LOGGER.debug("Hourly weather forecast fetch failed: %s", failure_reason)

    elapsed = max(monotonic() - fetch_started, 0.0)
    if cache_age_seconds is not None:
        cache_age_seconds += elapsed
    if cache_matches:
        cache.setdefault("outage_started_at", now.isoformat())
        cache.setdefault("grace_started_at", last_success)
        cache.pop("recovery_started_at", None)
    if cache_matches and now + timedelta(seconds=elapsed) < _weather_grace_expiry(cache, freshness_minutes):
        return result(
            {"forecast": list(cached_forecast)},
            _weather_forecast_details(
                fetch_status="cached_after_error",
                source_type="weather_service_hourly_cache",
                forecast=cached_forecast,
                cache_age_seconds=cache_age_seconds,
                failure_reason=failure_reason,
            ),
        )
    return result(
        {},
        _weather_forecast_details(
            fetch_status="failed",
            source_type="legacy_attributes_or_point_value",
            forecast=[],
            cache_age_seconds=cache_age_seconds,
            failure_reason=failure_reason,
        ),
    )


def _weather_grace_expiry(cache: dict[str, Any], freshness_minutes: int) -> datetime:
    """Bound outage tolerance by frozen availability proof and immutable evidence age."""
    evidence_at = cast(datetime, _parse_datetime_or_none(cache["fetched_at"]))
    anchor = (_parse_datetime_or_none(cache.get("grace_started_at"))
              or _parse_datetime_or_none(cache.get("last_success_at")) or evidence_at)
    return min(evidence_at + timedelta(minutes=freshness_minutes),
               anchor + timedelta(seconds=WEATHER_OUTAGE_GRACE_SECONDS))


def weather_issued_at(attributes: dict[str, Any], now: datetime) -> datetime | None:
    """Validate explicit provider provenance consistently for service and attribute forecasts."""
    raw = next((attributes[key] for key in (
        "forecast_issued_at", "issued_at", "generated_at", "generated_time", "forecast_generated_at",
    ) if attributes.get(key)), None)
    issued = _parse_datetime_or_none(raw)
    if raw is not None and (issued is None or issued.tzinfo is None or issued > now):
        raise ValueError("weather_forecast_response_invalid")
    return issued


def _weather_forecast_from_response(response: Any, entity_id: str) -> list[dict[str, Any]]:
    """Extract the documented per-entity forecast response."""
    if not isinstance(response, dict):
        return []
    entity_response = response.get(entity_id)
    if not isinstance(entity_response, dict):
        return []
    forecast = entity_response.get("forecast")
    if not isinstance(forecast, list):
        return []
    return [dict(item) for item in forecast if isinstance(item, dict)]

def _normalize_hourly_forecast(
    forecast: list[dict[str, Any]],
    *,
    timezone: str,
) -> list[dict[str, Any]]:
    """Normalize forecast datetimes to UTC, treating naive values as HA local time."""
    try:
        local_timezone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        local_timezone = ZoneInfo("UTC")
    normalized: list[dict[str, Any]] = []
    previous_naive_utc: datetime | None = None
    for item in forecast:
        parsed = _parse_datetime_or_none(item.get("datetime"))
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            candidates = _naive_local_utc_candidates(parsed, local_timezone)
            parsed_utc = next(
                (
                    candidate
                    for candidate in candidates
                    if previous_naive_utc is None or candidate > previous_naive_utc
                ),
                None,
            )
            if parsed_utc is None:
                continue
            previous_naive_utc = parsed_utc
        else:
            parsed_utc = parsed.astimezone(UTC)
        normalized_item = dict(item)
        normalized_item["datetime"] = parsed_utc.isoformat()
        normalized.append(normalized_item)
    return normalized

def _naive_local_utc_candidates(value: datetime, timezone: ZoneInfo) -> list[datetime]:
    """Return valid UTC instants for a naive wall time, including DST folds."""
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=timezone, fold=fold).astimezone(UTC)
        round_trip = candidate.astimezone(timezone).replace(tzinfo=None)
        if round_trip == value and candidate not in candidates:
            candidates.append(candidate)
    return sorted(candidates)

def _weather_forecast_details(
    *,
    fetch_status: str,
    source_type: str,
    forecast: list[dict[str, Any]],
    cache_age_seconds: float | None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Return bounded operator diagnostics for the hourly forecast fetch."""
    datetimes = [str(item.get("datetime")) for item in forecast if item.get("datetime")]
    return {
        "fetch_status": fetch_status,
        "source_type": source_type,
        "cache_age_seconds": (
            round(cache_age_seconds, 3) if cache_age_seconds is not None else None
        ),
        "point_count": len(forecast),
        "provider_coverage_start": min(datetimes) if datetimes else None,
        "provider_coverage_end": max(datetimes) if datetimes else None,
        "coverage_start": min(datetimes) if datetimes else None,
        "coverage_end": max(datetimes) if datetimes else None,
        "failure_reason": failure_reason,
    }

def _bounded_reason(error: Exception) -> str:
    """Return a bounded service failure reason without leaking payloads."""
    local_reasons = {
        "weather_forecast_response_empty", "weather_forecast_response_invalid", "weather_entity_unavailable",
    }
    reason = error.args[0] if type(error) is ValueError and error.args else None
    if isinstance(reason, str) and reason in local_reasons:
        return f"ValueError:{reason}"
    return type(error).__name__[:64]

def _parse_datetime_or_none(value: Any) -> Any | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        return value
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None
