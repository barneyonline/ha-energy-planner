"""Tests for base planner entity metadata."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.entity import (
    DEVICE_AI,
    DEVICE_SYSTEM,
    EnergyPlannerEntity,
    async_add_planner_entities,
    planner_config_subentry_id,
)


def test_planner_entity_defaults_to_system_device() -> None:
    coordinator = SimpleNamespace(entry=SimpleNamespace(entry_id="entry-1"))

    entity = EnergyPlannerEntity(coordinator, "plan_status")

    assert entity.unique_id == "entry-1_plan_status"
    assert entity.device_info["identifiers"] == {(DOMAIN, f"entry-1_{DEVICE_SYSTEM}")}
    assert entity.device_info["name"] == "System"
    assert entity.device_info["model"] == "System"


def test_planner_entity_can_target_group_device() -> None:
    coordinator = SimpleNamespace(entry=SimpleNamespace(entry_id="entry-1"))

    entity = EnergyPlannerEntity(coordinator, "ai_enabled", DEVICE_AI)

    assert entity.unique_id == "entry-1_ai_enabled"
    assert entity.device_info["identifiers"] == {(DOMAIN, f"entry-1_{DEVICE_AI}")}
    assert entity.device_info["name"] == "AI"
    assert entity.device_info["model"] == "AI"


def test_add_planner_entities_groups_by_matching_subentry() -> None:
    entry = SimpleNamespace(
        subentries={
            "system": SimpleNamespace(subentry_type=DEVICE_SYSTEM, subentry_id="system-subentry"),
            "ai": SimpleNamespace(subentry_type=DEVICE_AI, subentry_id="ai-subentry"),
        }
    )
    system_entity = SimpleNamespace(planner_device_key=DEVICE_SYSTEM)
    ai_entity = SimpleNamespace(planner_device_key=DEVICE_AI)
    calls: list[tuple[list[object], str | None]] = []

    async_add_planner_entities(
        entry,
        lambda entities, *, config_subentry_id: calls.append((entities, config_subentry_id)),
        [system_entity, ai_entity],
    )

    assert planner_config_subentry_id(entry, DEVICE_AI) == "ai-subentry"
    assert calls == [
        ([system_entity], "system-subentry"),
        ([ai_entity], "ai-subentry"),
    ]
