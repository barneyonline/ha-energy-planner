"""Tests for base planner entity metadata."""

from __future__ import annotations

import json
from types import SimpleNamespace

from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.entity import (
    RECORDER_MAX_STATE_ATTRIBUTES_BYTES,
    RECORDER_STATE_ATTRIBUTES_TARGET_BYTES,
    EnergyPlannerEntity,
    _compact_attribute_value,
    _normalize_attribute_value,
    planner_device_identifier,
    recorder_safe_attributes,
    recorder_safe_identifier,
    recorder_safe_text,
)


def test_planner_entity_uses_single_named_device() -> None:
    coordinator = SimpleNamespace(entry=SimpleNamespace(entry_id="entry-1", title="House Energy Planner"))

    entity = EnergyPlannerEntity(coordinator, "plan_status")

    assert entity.unique_id == "entry-1_plan_status"
    assert entity.device_info["identifiers"] == {(DOMAIN, "entry-1")}
    assert entity.device_info["name"] == "House Energy Planner"
    assert entity.device_info["model"] == "Energy Planner"
    assert planner_device_identifier("entry-1") == (DOMAIN, "entry-1")


def test_recorder_safe_attributes_preserve_small_payloads() -> None:
    attributes = {"plan_id": "plan-1", "actions": [{"asset": "EV"}]}

    assert recorder_safe_attributes(attributes) == attributes
    assert recorder_safe_attributes(None) == {}  # type: ignore[arg-type]


def test_recorder_safe_attributes_bound_adversarial_payloads_by_encoded_bytes() -> None:
    attributes = {
        "actions": [
            {
                "action_id": f"action-{index}",
                "unicode_evidence": "⚡" * 10_000,
                "nested": {f"field-{field}": "x" * 10_000 for field in range(40)},
            }
            for index in range(20)
        ]
    }

    safe = recorder_safe_attributes(attributes)
    encoded_size = len(json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    assert encoded_size <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
    assert encoded_size < RECORDER_MAX_STATE_ATTRIBUTES_BYTES
    assert safe["attributes_truncated"] is True


def test_recorder_safe_attributes_use_minimal_fallback_for_many_top_level_fields() -> None:
    attributes = {f"field-{index}": "x" * 10_000 for index in range(500)}

    safe = recorder_safe_attributes(attributes)

    assert safe["attributes_truncated"] is True
    assert len(safe["available_attribute_keys"]) == 32


def test_attribute_normalization_and_compaction_handle_defensive_boundaries() -> None:
    nested: object = "leaf"
    for _ in range(21):
        nested = {"nested": nested}

    assert "<truncated>" in str(_normalize_attribute_value(nested))
    assert _normalize_attribute_value(object()).startswith("<object object at")
    assert (
        _compact_attribute_value(
            {"nested": {"value": "detail"}},
            string_bytes=64,
            list_items=2,
            mapping_items=2,
            max_depth=1,
        )["nested"]
        == "<truncated>"
    )
    assert _compact_attribute_value(
        object(),
        string_bytes=64,
        list_items=2,
        mapping_items=2,
        max_depth=1,
    ).startswith("<object object at")


def test_recorder_safe_text_observes_utf8_byte_limit() -> None:
    value = recorder_safe_text("⚡" * 100, max_bytes=64)

    assert value.endswith("...")
    assert len(value.encode("utf-8")) <= 64
    assert recorder_safe_text("long", max_bytes=3) == "..."


def test_recorder_safe_identifier_preserves_uniqueness_when_truncated() -> None:
    first = recorder_safe_identifier(f"{'⚡' * 100}-1", max_bytes=64)
    second = recorder_safe_identifier(f"{'⚡' * 100}-2", max_bytes=64)

    assert len(first.encode("utf-8")) <= 64
    assert len(second.encode("utf-8")) <= 64
    assert first != second
