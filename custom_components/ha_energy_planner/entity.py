"""Base entities for Energy Planner."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_NAME
from .coordinator import EnergyPlannerCoordinator
from .models import to_jsonable

RECORDER_MAX_STATE_ATTRIBUTES_BYTES = 16_384
"""Home Assistant Recorder's hard limit for serialized state attributes."""

RECORDER_STATE_ATTRIBUTES_TARGET_BYTES = 12_288
"""Integration payload budget, leaving room for platform-owned attributes."""

_ATTRIBUTE_COMPACTION_PROFILES: tuple[tuple[int, int, int, int], ...] = (
    (1_024, 12, 24, 6),
    (512, 8, 16, 5),
    (256, 6, 12, 4),
    (128, 4, 8, 4),
    (64, 2, 6, 3),
)


def planner_device_identifier(entry_id: str) -> tuple[str, str]:
    """Return the single device-registry identifier for a planner entry."""
    return DOMAIN, entry_id


class EnergyPlannerEntity(CoordinatorEntity[EnergyPlannerCoordinator]):
    """Base coordinator entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        key: str,
    ) -> None:
        """Initialize entity on the single Energy Planner device."""
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={planner_device_identifier(entry.entry_id)},
            manufacturer=INTEGRATION_NAME,
            model=INTEGRATION_NAME,
            name=str(getattr(entry, "title", "") or INTEGRATION_NAME),
        )


def recorder_safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Return JSON attributes with a safe byte budget for Recorder.

    Recorder rejects the complete attribute payload once it reaches 16 KiB.
    Keep integration-owned attributes below 12 KiB so entity-platform fields
    such as friendly name, icon, and capabilities still have ample headroom.
    """
    normalized = _normalize_attribute_value(to_jsonable(attributes))
    if not isinstance(normalized, dict):
        return {}
    if _serialized_attribute_bytes(normalized) <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES:
        return normalized

    for string_bytes, list_items, mapping_items, depth in _ATTRIBUTE_COMPACTION_PROFILES:
        compacted = _compact_attribute_value(
            normalized,
            string_bytes=string_bytes,
            list_items=list_items,
            mapping_items=mapping_items,
            max_depth=depth,
        )
        assert isinstance(compacted, dict)
        compacted["attributes_truncated"] = True
        if _serialized_attribute_bytes(compacted) <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES:
            return compacted

    return {
        "attributes_truncated": True,
        "available_attribute_keys": [_truncate_utf8(str(key), 128) for key in list(normalized)[:32]],
    }


def recorder_safe_text(value: Any, *, max_bytes: int) -> str:
    """Return text capped by encoded byte length for state-backed metadata."""
    return _truncate_utf8(str(value or ""), max_bytes)


def recorder_safe_identifier(value: Any, *, max_bytes: int) -> str:
    """Byte-bound an identifier while retaining a collision-resistant suffix."""
    text = str(value or "")
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    suffix = f"...-{digest}"
    prefix = _truncate_utf8(text, max_bytes - len(suffix.encode("utf-8")))
    if prefix.endswith("..."):
        prefix = prefix[:-3]
    return f"{prefix}{suffix}"


def _normalize_attribute_value(value: Any, *, depth: int = 0) -> Any:
    """Convert attribute values to stable JSON-compatible structures."""
    if depth >= 20:
        return "<truncated>"
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _normalize_attribute_value(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_normalize_attribute_value(item, depth=depth + 1) for item in value]
    return str(value)


def _compact_attribute_value(
    value: Any,
    *,
    string_bytes: int,
    list_items: int,
    mapping_items: int,
    max_depth: int,
    depth: int = 0,
) -> Any:
    """Apply one deterministic compaction profile to an attribute value."""
    if isinstance(value, str):
        return _truncate_utf8(value, string_bytes)
    if value is None or isinstance(value, int | float | bool):
        return value
    if depth >= max_depth:
        return "<truncated>"
    if isinstance(value, dict):
        items = list(value.items())
        # Preserve the entity's top-level attribute contract. Apply mapping
        # limits only to nested evidence where field counts can grow without
        # bound; list and string limits still compact top-level values.
        selected_items = items if depth == 0 else items[:mapping_items]
        return {
            _truncate_utf8(str(key), 128): _compact_attribute_value(
                item,
                string_bytes=string_bytes,
                list_items=list_items,
                mapping_items=mapping_items,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for key, item in selected_items
        }
    if isinstance(value, list):
        return [
            _compact_attribute_value(
                item,
                string_bytes=string_bytes,
                list_items=list_items,
                mapping_items=mapping_items,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for item in value[:list_items]
        ]
    return _truncate_utf8(str(value), string_bytes)


def _serialized_attribute_bytes(attributes: dict[str, Any]) -> int:
    """Return the compact UTF-8 JSON size Recorder receives."""
    return len(json.dumps(attributes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate text without splitting a UTF-8 code point."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    if max_bytes <= 3:
        return "." * max_bytes
    prefix = encoded[: max_bytes - 3].decode("utf-8", errors="ignore")
    return f"{prefix}..."
