"""Optional climate observations with explicit units, identity and provenance."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import Any

from .const import (
    CONF_AMBER_IMPORT_PRICE,
    CONF_BATTERY_SOC,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_TARGET_HIGH,
    CONF_CLIMATE_TARGET_LOW,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_DAIKIN_POWER,
    CONF_FORECAST_FRESHNESS_MINUTES,
    CONF_HVAC_ARRIVAL,
    CONF_HVAC_COP_TABLE,
    CONF_HVAC_HUMIDITY,
    CONF_HVAC_IRRADIANCE,
    CONF_HVAC_IRRADIANCE_FORECAST,
    CONF_HVAC_MAX_HUMIDITY,
    CONF_HVAC_ZONE_MAPPINGS,
    CONF_PERSON_ENTITIES,
    CONF_PLANNING_HORIZON_HOURS,
    CONF_PLANNING_INTERVAL_MINUTES,
    CONF_WEATHER,
)


def finite(value: Any) -> float | None:
    """Reject booleans, NaN and infinity at numerical boundaries."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def instant(value: Any) -> datetime | None:
    """Require an absolute instant; callers explicitly normalize local helpers."""
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


def climate_identity(data: dict[str, Any], options: dict[str, Any]) -> str:
    """Invalidate learning on changes to sources, control and comfort policy."""
    keys = {
        CONF_DAIKIN_CLIMATE,
        CONF_DAIKIN_POWER,
        CONF_CLIMATE_AUTOMATIONS,
        CONF_CLIMATE_TARGET_LOW,
        CONF_CLIMATE_TARGET_HIGH,
        CONF_CLIMATE_ZONES,
        CONF_PERSON_ENTITIES,
    }
    payload = {
        key: value for key, value in data.items() if key in keys or key.startswith("hvac_") or key.startswith("weather")
    }
    payload["options"] = {
        key: value
        for key, value in options.items()
        if key.startswith("hvac_")
        or key.startswith("occupied_temperature")
        or key in {CONF_PLANNING_HORIZON_HOURS, CONF_PLANNING_INTERVAL_MINUTES}
    }
    return sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def validate_climate_config(data: dict[str, Any]) -> dict[str, str]:
    """Validate structured optional mappings before they reach a model."""
    errors: dict[str, str] = {}
    maximum = data.get(CONF_HVAC_MAX_HUMIDITY)
    if maximum is not None and (finite(maximum) is None or not 0 < float(maximum) <= 100):
        errors[CONF_HVAC_MAX_HUMIDITY] = "invalid_climate_configuration"
    table = data.get(CONF_HVAC_COP_TABLE, {})
    if not isinstance(table, dict) or set(table) - {"heat", "cool"}:
        errors[CONF_HVAC_COP_TABLE] = "invalid_climate_configuration"
    else:
        for rows in table.values():
            if not isinstance(rows, list) or len(rows) < 2 or len(rows) > 30:
                errors[CONF_HVAC_COP_TABLE] = "invalid_climate_configuration"
                continue
            previous = -float("inf")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"temperature", "cop"}
                    or finite(row.get("temperature")) is None
                    or finite(row.get("cop")) is None
                ):
                    errors[CONF_HVAC_COP_TABLE] = "invalid_climate_configuration"
                    break
                temperature, cop = float(row["temperature"]), float(row["cop"])
                if temperature <= previous or not 0 < cop <= 20:
                    errors[CONF_HVAC_COP_TABLE] = "invalid_climate_configuration"
                previous = temperature
    zones = data.get(CONF_HVAC_ZONE_MAPPINGS, {})
    configured = data.get(CONF_CLIMATE_ZONES, [])
    if isinstance(configured, str):
        configured = [value.strip() for value in configured.split(",")]
    if not isinstance(zones, dict) or len(zones) > 16:
        errors[CONF_HVAC_ZONE_MAPPINGS] = "invalid_climate_configuration"
    else:
        domains = {
            "temperature": {"sensor"},
            "humidity": {"sensor"},
            "presence": {"binary_sensor", "input_boolean"},
            "low": {"input_number"},
            "high": {"input_number"},
        }
        for entity, mapping in zones.items():
            if (
                entity not in configured
                or not isinstance(mapping, dict)
                or set(mapping) - {*domains, "maximum_humidity"}
            ):
                errors[CONF_HVAC_ZONE_MAPPINGS] = "invalid_climate_configuration"
                continue
            for key, value in mapping.items():
                if key == "maximum_humidity":
                    if finite(value) is None or not 0 < float(value) <= 100:
                        errors[CONF_HVAC_ZONE_MAPPINGS] = "invalid_climate_configuration"
                elif not isinstance(value, str) or value.split(".")[0] not in domains[key]:
                    errors[CONF_HVAC_ZONE_MAPPINGS] = "invalid_climate_configuration"
    return errors


def read_climate_inputs(
    hass: Any, data: dict[str, Any], options: dict[str, Any], now: datetime, end: datetime, load_details: dict[str, Any]
) -> dict[str, Any]:
    """Read only mapped sensors; missing optional evidence stays explicitly absent."""
    sources: dict[str, Any] = {}
    freshness = timedelta(minutes=float(options.get(CONF_FORECAST_FRESHNESS_MINUTES, 120)))

    def state(entity: Any) -> Any:
        return hass.states.get(entity) if isinstance(entity, str) and entity else None

    def number(entity: Any, kind: str) -> float | None:
        item = state(entity)
        if item is None:
            if isinstance(entity, str) and entity:
                sources[entity] = {"updated_at": None, "fresh": False}
            return None
        updated = instant(getattr(item, "last_reported", getattr(item, "last_updated", None)))
        valid = str(entity).startswith("input_number.") or (
            updated is not None and timedelta(0) <= now - updated <= freshness
        )
        sources[str(entity)] = {"updated_at": updated.isoformat() if updated else None, "fresh": valid}
        value = finite(item.state)
        sources[str(entity)]["fresh"] = False
        if not valid or value is None:
            return None
        unit = item.attributes.get("unit_of_measurement")
        if kind == "temperature":
            if unit in {"°F", "F"}:
                value = (value - 32) * 5 / 9
            elif unit not in {"°C", "C"} and not str(entity).startswith("input_number."):
                return None
        elif kind == "humidity":
            if unit != "%" or not 0 <= value <= 100:
                return None
        elif unit not in {"W/m²", "W/m2"} or value < 0:
            return None
        sources[str(entity)]["fresh"] = True
        return value

    main = state(data.get(CONF_DAIKIN_CLIMATE))
    attrs = main.attributes if main else {}
    arrival_state = state(data.get(CONF_HVAC_ARRIVAL))
    arrival = None
    if arrival_state is not None:
        # input_datetime supplies an absolute timestamp, avoiding ambiguous DST wall times.
        timestamp = finite(arrival_state.attributes.get("timestamp"))
        if (
            timestamp is not None
            and arrival_state.attributes.get("has_date", True)
            and arrival_state.attributes.get("has_time", True)
        ):
            try:
                arrival = datetime.fromtimestamp(timestamp, UTC)
            except (OverflowError, OSError, ValueError):
                arrival = None
        else:
            arrival = instant(arrival_state.state)
        if arrival is not None and not now < arrival <= end:
            arrival = None
    zones: dict[str, Any] = {}
    mappings = data.get(CONF_HVAC_ZONE_MAPPINGS, {})
    if not validate_climate_config(data):
        for entity, mapping in mappings.items():
            zone = state(entity)
            presence = state(mapping.get("presence"))
            zones[entity] = {
                "temperature": number(mapping.get("temperature"), "temperature"),
                "humidity": number(mapping.get("humidity"), "humidity"),
                "low": number(mapping.get("low"), "temperature"),
                "high": number(mapping.get("high"), "temperature"),
                "occupied": (presence.state == "on") if presence and presence.state in {"on", "off"} else None,
                "maximum_humidity": finite(mapping.get("maximum_humidity")),
                "enabled": bool(zone and zone.state not in {"off", "unknown", "unavailable"}),
            }
    forecast: list[dict[str, Any]] = []
    forecast_state = state(data.get(CONF_HVAC_IRRADIANCE_FORECAST))
    if forecast_state is not None:
        issued = instant(forecast_state.attributes.get("issued_at"))
        rows = forecast_state.attributes.get("forecast", [])
        if issued and timedelta(0) <= now - issued <= freshness and isinstance(rows, list):
            for row in rows[:288]:
                if not isinstance(row, dict):
                    continue
                at, value = instant(row.get("valid_at")), finite(row.get("irradiance"))
                if at and value is not None and value >= 0 and now - freshness <= at <= end:
                    forecast.append({"at": at.isoformat(), "value": value})
    weather = state(data.get(CONF_WEATHER))
    outdoor_humidity = finite(weather.attributes.get("humidity")) if weather else None
    weather_at = instant(getattr(weather, "last_reported", getattr(weather, "last_updated", None)))
    if outdoor_humidity is not None and (
        not 0 <= outdoor_humidity <= 100 or weather_at is None or not timedelta(0) <= now - weather_at <= freshness
    ):
        outdoor_humidity = None
    tariff = state(data.get(CONF_AMBER_IMPORT_PRICE))
    unit = str(tariff.attributes.get("unit_of_measurement", "")) if tariff else ""
    currency = unit.split("/")[0].strip() if "/" in unit else None
    if currency is not None and currency.lower() in {"c", "¢", "cent", "cents"}:
        # Forecast prices have already been divided by 100 into whole currency.
        currency = tariff.attributes.get("currency") or getattr(getattr(hass, "config", None), "currency", None)
    return {
        "identity": climate_identity(data, options),
        "target": finite(attrs.get("temperature")),
        "temperature_step": finite(attrs.get("target_temp_step")) or 0.5,
        "minimum_temperature": finite(attrs.get("min_temp")),
        "maximum_temperature": finite(attrs.get("max_temp")),
        "arrival": arrival.isoformat() if arrival else None,
        "humidity": number(data.get(CONF_HVAC_HUMIDITY), "humidity"),
        "outdoor_humidity": outdoor_humidity,
        "maximum_humidity": finite(data.get(CONF_HVAC_MAX_HUMIDITY)),
        "irradiance": number(data.get(CONF_HVAC_IRRADIANCE), "irradiance"),
        "irradiance_forecast": forecast,
        "zones": zones,
        "sources": sources,
        "cop_table": {
            mode: [{"temperature": float(row["temperature"]), "cop": float(row["cop"])} for row in rows]
            for mode, rows in data.get(CONF_HVAC_COP_TABLE, {}).items()
        }
        if not validate_climate_config(data)
        else {},
        "configuration_valid": not bool(validate_climate_config(data)),
        "battery_configured": bool(data.get(CONF_BATTERY_SOC)),
        "load_excludes_hvac": load_details.get("hvac_power_subtracted") is True,
        "currency": currency,
    }
