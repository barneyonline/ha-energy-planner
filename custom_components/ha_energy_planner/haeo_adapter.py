"""HAEO interaction adapter."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, tzinfo
from hashlib import sha256
from math import isfinite
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .models import DecisionContext, FlexibleLoadProjection, HAEOSolvePhase, HAEOSolveResult, HAEOStatus

_FORECAST_LIST_KEYS = ("slots", "slot_forecasts", "forecast", "forecasts", "schedule", "data", "values")
_TIME_KEYS = ("valid_at", "datetime", "start_time", "period_start", "from", "time", "date", "nem_time")
_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 8
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


@dataclass(frozen=True, slots=True)
class HAEOServiceCapabilities:
    """Capabilities of the configured HAEO service contract."""

    supports_response: bool
    supports_flexible_projections: bool
    source: str
    selected_entry_id: str | None = None
    error_reason: str | None = None

    @property
    def supports_flexible_second_pass(self) -> bool:
        """Return whether a second solve can update planner evidence."""
        return self.supports_response and self.supports_flexible_projections and self.error_reason is None

    def as_dict(self) -> dict[str, Any]:
        """Return compact diagnostic-safe metadata."""
        return {
            "supports_response": self.supports_response,
            "supports_flexible_projections": self.supports_flexible_projections,
            "supports_flexible_second_pass": self.supports_flexible_second_pass,
            "source": self.source,
            "selected_entry_id": self.selected_entry_id,
            "error_reason": self.error_reason,
        }


class HAEOAdapter:
    """Interact with HAEO through supported Home Assistant services only."""

    def __init__(
        self,
        hass: HomeAssistant,
        optimize_service: str | None,
        haeo_config_entry_id: str | None = None,
        *,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        """Initialize adapter."""
        self.hass = hass
        self.optimize_service = optimize_service
        self.haeo_config_entry_id = haeo_config_entry_id
        self.cache_ttl_seconds = max(0.0, min(float(cache_ttl_seconds), _CACHE_TTL_SECONDS))
        self._cache: OrderedDict[str, tuple[float, HAEOSolveResult]] = OrderedDict()
        self._legacy_no_response = False
        self.last_call_metadata: dict[str, Any] = {}
        self.capabilities = self._detect_capabilities()

    @property
    def supports_flexible_second_pass(self) -> bool:
        """Return whether flexible projections can produce updated evidence."""
        return self.capabilities.supports_flexible_second_pass

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
        started = perf_counter()
        self.capabilities = self._detect_capabilities()
        fingerprint = _solve_fingerprint(phase, context, projections, self.capabilities)
        if not self.optimize_service:
            result = HAEOSolveResult(phase, HAEOStatus.STALE, "haeo_service_not_configured", context.plan_id)
            return self._finish(result, started, fingerprint, cache_hit=False)
        if "." not in self.optimize_service:
            result = HAEOSolveResult(phase, HAEOStatus.FAILED, "haeo_service_invalid", context.plan_id)
            return self._finish(result, started, fingerprint, cache_hit=False)
        domain, service = self.optimize_service.split(".", 1)
        has_service = getattr(self.hass.services, "has_service", None)
        if callable(has_service) and not has_service(domain, service):
            result = HAEOSolveResult(
                phase,
                HAEOStatus.STALE,
                "haeo_service_unavailable",
                context.plan_id,
                service_called=self.optimize_service,
            )
            return self._finish(result, started, fingerprint, cache_hit=False)
        if self.capabilities.error_reason:
            result = HAEOSolveResult(
                phase,
                HAEOStatus.FAILED,
                self.capabilities.error_reason,
                context.plan_id,
                service_called=self.optimize_service,
            )
            return self._finish(result, started, fingerprint, cache_hit=False)
        if phase == HAEOSolvePhase.FLEXIBLE_LOAD and not self.supports_flexible_second_pass:
            result = HAEOSolveResult(
                phase,
                HAEOStatus.STALE,
                "haeo_flexible_projection_unsupported",
                context.plan_id,
                service_called=self.optimize_service,
            )
            return self._finish(result, started, fingerprint, cache_hit=False)
        if not self.capabilities.supports_response:
            # A fire-and-forget optimize service cannot provide evidence to the
            # planner. Calling it on every refresh adds load while leaving the
            # plan unchanged, so report the capability gap without invoking it.
            result = HAEOSolveResult(
                phase,
                HAEOStatus.STALE,
                "haeo_response_unsupported",
                context.plan_id,
                service_called=None,
            )
            return self._finish(result, started, fingerprint, cache_hit=False)
        cached = self._cached_result(fingerprint, context.plan_id)
        if cached is not None:
            return self._finish(cached, started, fingerprint, cache_hit=True)

        service_data = _service_data(context, phase, projections)
        try:
            response = await self.hass.services.async_call(
                domain, service, service_data, blocking=True, return_response=True
            )
        except Exception as err:  # noqa: BLE001 - adapter must fail closed and report redacted reason.
            result = _service_failed_result(phase, context, self.optimize_service, err)
            return self._finish(result, started, fingerprint, cache_hit=False)
        result = HAEOSolveResult(
            phase,
            HAEOStatus.READY,
            "haeo_service_called",
            context.plan_id,
            service_called=self.optimize_service,
            response=response if isinstance(response, dict) else None,
        )
        self._store_cached_result(fingerprint, result)
        return self._finish(result, started, fingerprint, cache_hit=False)

    def _detect_capabilities(self) -> HAEOServiceCapabilities:
        """Detect native and custom service capabilities without invoking a solve."""
        if not self.optimize_service or "." not in self.optimize_service:
            return HAEOServiceCapabilities(False, False, "invalid_or_unconfigured")
        domain, service = self.optimize_service.split(".", 1)
        if domain == "haeo" and service == "optimize":
            entry_ids = _haeo_entry_ids(self.hass)
            if entry_ids is not None:
                selected_entry_id, error_reason = _select_haeo_entry_id(entry_ids, self.haeo_config_entry_id)
                if selected_entry_id is not None or error_reason is not None:
                    return HAEOServiceCapabilities(
                        False,
                        False,
                        "native_config_entry_service",
                        selected_entry_id=selected_entry_id,
                        error_reason=error_reason,
                    )
        descriptor = _service_descriptor(self.hass, domain, service)
        if descriptor is not None:
            supports_response = _descriptor_supports_response(descriptor)
            supports_projections = _descriptor_supports_projection(descriptor)
            if self._legacy_no_response:
                supports_response = False
            return HAEOServiceCapabilities(
                supports_response,
                supports_projections,
                "service_registry",
            )
        return HAEOServiceCapabilities(
            not self._legacy_no_response,
            True,
            "configured_custom_contract",
        )

    def _cached_result(self, fingerprint: str, plan_id: str) -> HAEOSolveResult | None:
        now = monotonic()
        self._expire_cache(now)
        cached = self._cache.get(fingerprint)
        if cached is None:
            return None
        cached_at, result = cached
        if now - cached_at > self.cache_ttl_seconds:
            self._cache.pop(fingerprint, None)
            return None
        self._cache.move_to_end(fingerprint)
        return HAEOSolveResult(
            result.phase,
            result.status,
            result.reason,
            plan_id,
            service_called=result.service_called,
            response=result.response,
        )

    def _store_cached_result(self, fingerprint: str, result: HAEOSolveResult) -> None:
        if self.cache_ttl_seconds <= 0 or result.status != HAEOStatus.READY:
            return
        now = monotonic()
        self._expire_cache(now)
        self._cache[fingerprint] = (now, result)
        self._cache.move_to_end(fingerprint)
        while len(self._cache) > _CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    def _expire_cache(self, now: float) -> None:
        expired = [key for key, (cached_at, _) in self._cache.items() if now - cached_at > self.cache_ttl_seconds]
        for key in expired:
            self._cache.pop(key, None)

    def _finish(
        self,
        result: HAEOSolveResult,
        started: float,
        fingerprint: str,
        *,
        cache_hit: bool,
    ) -> HAEOSolveResult:
        self.last_call_metadata = {
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "cache_hit": cache_hit,
            "input_fingerprint": fingerprint,
            "response_received": result.response is not None,
            "capabilities": self.capabilities.as_dict(),
        }
        return result


def _first_haeo_entry_id(hass: HomeAssistant) -> str | None:
    """Return the only configured HAEO entry ID, failing closed when ambiguous."""
    entry_ids = _haeo_entry_ids(hass)
    return entry_ids[0] if entry_ids is not None and len(entry_ids) == 1 else None


def _haeo_entry_ids(hass: HomeAssistant) -> list[str] | None:
    """Return sorted configured HAEO entry IDs, or None when unavailable."""
    config_entries = getattr(hass, "config_entries", None)
    async_entries = getattr(config_entries, "async_entries", None)
    if not callable(async_entries):
        return None
    try:
        entries = async_entries("haeo")
    except TypeError:
        return None
    return sorted(str(entry_id) for entry in entries if (entry_id := getattr(entry, "entry_id", None)))


def _select_haeo_entry_id(entry_ids: list[str], configured_entry_id: str | None) -> tuple[str | None, str | None]:
    """Select a native HAEO entry deterministically and fail closed on ambiguity."""
    if configured_entry_id:
        if configured_entry_id in entry_ids:
            return configured_entry_id, None
        return None, "haeo_config_entry_not_found"
    if len(entry_ids) == 1:
        return entry_ids[0], None
    if len(entry_ids) > 1:
        return None, "haeo_config_entry_ambiguous"
    return None, "haeo_config_entry_not_found"


def _service_descriptor(hass: HomeAssistant, domain: str, service: str) -> Any:
    """Return a registered service descriptor when Home Assistant exposes one."""
    async_services = getattr(getattr(hass, "services", None), "async_services", None)
    if not callable(async_services):
        return None
    try:
        services = async_services()
    except TypeError:
        return None
    if not isinstance(services, dict):
        return None
    domain_services = services.get(domain)
    return domain_services.get(service) if isinstance(domain_services, dict) else None


def _descriptor_supports_response(descriptor: Any) -> bool:
    value = getattr(descriptor, "supports_response", None)
    if value is None and isinstance(descriptor, dict):
        value = descriptor.get("supports_response")
    normalized = str(getattr(value, "value", value)).strip().lower()
    return normalized not in {"", "0", "false", "none", "supportsresponse.none"}


def _descriptor_supports_projection(descriptor: Any) -> bool:
    schema = getattr(descriptor, "schema", None)
    if schema is None and isinstance(descriptor, dict):
        schema = descriptor.get("schema", descriptor.get("fields"))
    schema = getattr(schema, "schema", schema)
    if not isinstance(schema, dict):
        return False
    return "flexible_load_projection" in {_canonical_key(getattr(key, "schema", key)) for key in schema}


def _solve_fingerprint(
    phase: HAEOSolvePhase,
    context: DecisionContext,
    projections: list[FlexibleLoadProjection],
    capabilities: HAEOServiceCapabilities,
) -> str:
    """Fingerprint normalized solve inputs, including slot boundaries."""
    interval_seconds = _context_interval_seconds(context)
    slots = []
    for slot in context.slots:
        fields = getattr(slot, "__dataclass_fields__", {})
        slots.append(
            {
                name: _fingerprint_value(
                    _planning_boundary(getattr(slot, name), interval_seconds, context.created_at.tzinfo)
                    if name == "valid_at"
                    else getattr(slot, name, None)
                )
                for name in sorted(fields)
            }
        )
    payload = {
        "phase": str(phase),
        "slots": slots,
        "current_battery_soc_percent": _fingerprint_value(context.current_battery_soc_percent),
        "current_ev_soc_percent": _fingerprint_value(context.current_ev_soc_percent),
        "occupancy_state": _fingerprint_value(context.occupancy_state),
        "input_health": _fingerprint_value(context.input_health),
        "projections": [
            {
                "valid_at": _planning_boundary(
                    projection.valid_at, interval_seconds, context.created_at.tzinfo
                ).isoformat(),
                "ev_load_kw": projection.ev_load_kw,
                "hvac_load_kw": projection.hvac_load_kw,
            }
            for projection in projections
        ],
        "service_contract": capabilities.as_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return sha256(encoded).hexdigest()[:20]


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, float) and not isfinite(value):
        return str(value)
    return value


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
    interval_seconds = _context_interval_seconds(context)
    slot_index = {
        _planning_boundary(slot.valid_at, interval_seconds, default_tz): slot for slot in context.slots
    }
    counts: dict[str, int] = {}
    for index, item in enumerate(items):
        item = _flatten_item(item)
        slot = None
        valid_at = _item_time(item)
        if valid_at is not None:
            slot = slot_index.get(_planning_boundary(valid_at, interval_seconds, default_tz))
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


def _context_interval_seconds(context: DecisionContext) -> int:
    """Infer the smallest positive planning-slot cadence."""
    ordered = sorted(slot.valid_at for slot in context.slots)
    differences = [
        int((current - previous).total_seconds())
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if current > previous
    ]
    return min(differences, default=300)


def _planning_boundary(value: datetime, interval_seconds: int, default_tz: tzinfo | None) -> datetime:
    """Normalize refresh jitter to the current wall-clock planning boundary."""
    normalized = _normalize_datetime(value, default_tz)
    interval = max(int(interval_seconds), 1)
    return datetime.fromtimestamp((int(normalized.timestamp()) // interval) * interval, tz=normalized.tzinfo)
