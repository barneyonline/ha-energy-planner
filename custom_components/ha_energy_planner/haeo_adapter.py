"""HAEO interaction adapter."""

from __future__ import annotations

from datetime import datetime, tzinfo
from math import isfinite
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .models import DecisionContext, FlexibleLoadProjection, HAEOSolvePhase, HAEOSolveResult, HAEOStatus

_FORECAST_LIST_KEYS = ("slots", "slot_forecasts", "forecast", "forecasts", "schedule", "data", "values")
_TIME_KEYS = ("valid_at", "datetime", "start_time", "period_start", "from", "time", "date", "nem_time")
_FIELD_ALIASES = {
    "haeo_battery_soc_forecast_percent": (
        "haeo_battery_soc_forecast_percent",
        "battery_soc_forecast_percent",
        "battery_soc_percent",
        "battery_soc",
        "soc_percent",
        "soc",
    ),
    "haeo_grid_import_forecast_kw": (
        "haeo_grid_import_forecast_kw",
        "grid_import_forecast_kw",
        "grid_import_kw",
        "grid_import_w",
        "grid_import",
        "import_kw",
        "import_w",
        "import_power_kw",
        "import_power_w",
    ),
    "haeo_grid_export_forecast_kw": (
        "haeo_grid_export_forecast_kw",
        "grid_export_forecast_kw",
        "grid_export_kw",
        "grid_export_w",
        "grid_export",
        "export_kw",
        "export_w",
        "export_power_kw",
        "export_power_w",
    ),
    "haeo_battery_charge_forecast_kw": (
        "haeo_battery_charge_forecast_kw",
        "battery_charge_forecast_kw",
        "battery_charge_kw",
        "battery_charge_w",
        "battery_charging_kw",
        "battery_charging_w",
        "battery_charge",
        "ess_charge_kw",
        "ess_charge_w",
        "storage_charge_kw",
        "storage_charge_w",
        "battery_grid_charge_kw",
        "battery_grid_charge_w",
        "grid_charge_kw",
        "grid_charge_w",
    ),
    "haeo_battery_discharge_forecast_kw": (
        "haeo_battery_discharge_forecast_kw",
        "battery_discharge_forecast_kw",
        "battery_discharge_kw",
        "battery_discharge_w",
        "battery_discharging_kw",
        "battery_discharging_w",
        "battery_discharge",
        "ess_discharge_kw",
        "ess_discharge_w",
        "storage_discharge_kw",
        "storage_discharge_w",
        "battery_grid_discharge_kw",
        "battery_grid_discharge_w",
        "grid_discharge_kw",
        "grid_discharge_w",
    ),
}


class HAEOAdapter:
    """Interact with HAEO through supported Home Assistant services only."""

    def __init__(self, hass: HomeAssistant, optimize_service: str | None) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.optimize_service = optimize_service

    async def async_solve_baseline(self, context: DecisionContext) -> HAEOSolveResult:
        """Request a baseline HAEO solve."""
        return await self._async_call_haeo(
            HAEOSolvePhase.BASELINE,
            context,
            projections=[],
        )

    async def async_solve_with_flexible_load(
        self,
        context: DecisionContext,
        projections: list[FlexibleLoadProjection],
    ) -> HAEOSolveResult:
        """Request a HAEO solve with projected flexible demand included."""
        return await self._async_call_haeo(
            HAEOSolvePhase.FLEXIBLE_LOAD,
            context,
            projections=projections,
        )

    async def _async_call_haeo(
        self,
        phase: HAEOSolvePhase,
        context: DecisionContext,
        *,
        projections: list[FlexibleLoadProjection],
    ) -> HAEOSolveResult:
        if not self.optimize_service:
            return HAEOSolveResult(phase, HAEOStatus.STALE, "haeo_service_not_configured", context.plan_id)
        if "." not in self.optimize_service:
            return HAEOSolveResult(phase, HAEOStatus.FAILED, "haeo_service_invalid", context.plan_id)
        domain, service = self.optimize_service.split(".", 1)
        has_service = getattr(self.hass.services, "has_service", None)
        if callable(has_service) and not has_service(domain, service):
            return HAEOSolveResult(
                phase,
                HAEOStatus.STALE,
                "haeo_service_unavailable",
                context.plan_id,
                service_called=self.optimize_service,
            )
        service_data = _service_data(context, phase, projections)
        real_haeo_entry_id = _first_haeo_entry_id(self.hass) if domain == "haeo" and service == "optimize" else None
        if real_haeo_entry_id:
            service_data = {"config_entry": real_haeo_entry_id}
        try:
            if real_haeo_entry_id:
                response = await self.hass.services.async_call(domain, service, service_data, blocking=True)
            else:
                response = await self.hass.services.async_call(domain, service, service_data, blocking=True, return_response=True)
        except TypeError:
            try:
                response = await self.hass.services.async_call(domain, service, service_data, blocking=True)
            except Exception as err:  # noqa: BLE001 - adapter must fail closed and report redacted reason.
                return _service_failed_result(phase, context, self.optimize_service, err)
        except Exception as err:  # noqa: BLE001 - adapter must fail closed and report redacted reason.
            return _service_failed_result(phase, context, self.optimize_service, err)
        return HAEOSolveResult(
            phase,
            HAEOStatus.READY,
            "haeo_service_called",
            context.plan_id,
            service_called=self.optimize_service,
        response=response if isinstance(response, dict) else None,
    )


def _first_haeo_entry_id(hass: HomeAssistant) -> str | None:
    """Return the first configured HAEO entry ID when running inside Home Assistant."""
    config_entries = getattr(hass, "config_entries", None)
    async_entries = getattr(config_entries, "async_entries", None)
    if not callable(async_entries):
        return None
    try:
        entries = async_entries("haeo")
    except TypeError:
        return None
    for entry in entries:
        entry_id = getattr(entry, "entry_id", None)
        if entry_id:
            return str(entry_id)
    return None


def _service_failed_result(
    phase: HAEOSolvePhase,
    context: DecisionContext,
    service_called: str,
    err: Exception,
) -> HAEOSolveResult:
    return HAEOSolveResult(
        phase,
        HAEOStatus.FAILED,
        f"haeo_service_failed:{err.__class__.__name__}",
        context.plan_id,
        service_called=service_called,
    )


def _service_data(
    context: DecisionContext,
    phase: HAEOSolvePhase,
    projections: list[FlexibleLoadProjection],
) -> dict[str, Any]:
    """Build HAEO service data without relying on HAEO internals."""
    return {
        "source": "ha_energy_planner",
        "phase": str(phase),
        "plan_id": context.plan_id,
        "created_at": context.created_at.isoformat(),
        "horizon_slot_count": len(context.slots),
        "flexible_load_projection": [
            {
                "valid_at": projection.valid_at.isoformat(),
                "ev_load_kw": projection.ev_load_kw,
                "hvac_load_kw": projection.hvac_load_kw,
            }
            for projection in projections
        ],
    }


def apply_haeo_response_to_context(context: DecisionContext, response: dict[str, Any] | None) -> dict[str, int]:
    """Populate HAEO forecast evidence on decision slots from a service response."""
    items = _response_forecast_items(response)
    if not items:
        return {}
    default_tz = context.created_at.tzinfo
    slot_index = {_normalize_datetime(slot.valid_at, default_tz): slot for slot in context.slots}
    counts: dict[str, int] = {}
    for index, item in enumerate(items):
        item = _flatten_item(item)
        slot = None
        valid_at = _item_time(item)
        if valid_at is not None:
            slot = slot_index.get(_normalize_datetime(valid_at, default_tz))
            if slot is None:
                continue
        elif index < len(context.slots):
            slot = context.slots[index]
        if slot is None:
            continue
        unit = str(item.get("unit", item.get("units", "")))
        for field_name, aliases in _FIELD_ALIASES.items():
            matched = _value_from_aliases(item, aliases)
            if matched is None:
                continue
            matched_key, value = matched
            if field_name != "haeo_battery_soc_forecast_percent":
                value = _normalize_power_value(value, matched_key, unit)
            if not isfinite(value):
                continue
            setattr(slot, field_name, value)
            counts[field_name] = counts.get(field_name, 0) + 1
    return counts


def _response_forecast_items(value: Any, depth: int = 0) -> list[Any]:
    if value is None or depth > 4:
        return []
    if isinstance(value, list):
        return value if all(isinstance(item, dict) for item in value) else []
    if not isinstance(value, dict):
        return []
    for key in _FORECAST_LIST_KEYS:
        items = value.get(key)
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            return items
        if isinstance(items, dict):
            mapped_items = _items_from_time_map(items)
            if mapped_items:
                return mapped_items
    mapped_items = _items_from_time_map(value)
    if mapped_items:
        return mapped_items
    for child in value.values():
        items = _response_forecast_items(child, depth + 1)
        if items:
            return items
    return []


def _items_from_time_map(value: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, raw_value in value.items():
        valid_at = _parse_datetime_or_none(key)
        if valid_at is None:
            continue
        if isinstance(raw_value, dict):
            item = dict(raw_value)
            item.setdefault("valid_at", key)
        else:
            item = {"valid_at": key, "value": raw_value}
        items.append(item)
    return items


def _item_time(item: dict[str, Any]) -> datetime | None:
    for key in _TIME_KEYS:
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            continue
        parsed = _parse_datetime_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _flatten_item(item: dict[str, Any]) -> dict[str, Any]:
    flattened = dict(item)
    for parent_key, parent_value in list(item.items()):
        if not isinstance(parent_value, dict):
            continue
        for child_key, child_value in parent_value.items():
            flattened.setdefault(f"{parent_key}_{child_key}", child_value)
            flattened.setdefault(child_key, child_value)
    canonical = dict(flattened)
    for key, value in flattened.items():
        normalized_key = _canonical_key(key)
        canonical.setdefault(normalized_key, value)
    return canonical


def _value_from_aliases(item: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str, float] | None:
    key = next((alias for alias in aliases if alias in item), None)
    if key is None:
        return None
    try:
        value = float(item[key])
    except (TypeError, ValueError):
        return None
    if not isfinite(value):
        return None
    return key, value


def _normalize_power_value(value: float, matched_key: str, unit: str) -> float:
    unit_lower = unit.strip().lower().replace(" ", "")
    if unit_lower in {"mw", "megawatt", "megawatts"}:
        return value * 1000
    if unit_lower in {"w", "watt", "watts"}:
        return value / 1000
    if "watt" in matched_key or (matched_key.endswith("_w") and not matched_key.endswith("_kw")):
        return value / 1000
    return value


def _canonical_key(value: Any) -> str:
    raw = str(value)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    separated = re.sub(r"[^0-9A-Za-z]+", "_", separated)
    return separated.strip("_").lower()


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_datetime(value: datetime, default_tz: tzinfo | None) -> datetime:
    if value.tzinfo is None and default_tz is not None:
        return value.replace(tzinfo=default_tz)
    return value
