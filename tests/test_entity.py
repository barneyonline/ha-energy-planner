"""Tests for base planner entity metadata."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.entity import (
    DEVICE_AI,
    DEVICE_CLIMATE,
    DEVICE_ENERGY,
    DEVICE_ENPHASE,
    DEVICE_EV,
    DEVICE_SYSTEM,
    EnergyPlannerEntity,
    async_add_planner_entities,
    planner_config_subentry_id,
    planner_device_key_for_entity,
    planner_device_configured,
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


def test_control_switches_target_optional_devices() -> None:
    assert planner_device_key_for_entity("ev_control_enabled") == DEVICE_EV
    assert planner_device_key_for_entity("climate_control_enabled") == DEVICE_CLIMATE
    assert planner_device_key_for_entity("enphase_control_enabled") == DEVICE_ENPHASE


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


def test_add_planner_entities_skips_optional_devices_without_subentry() -> None:
    entry = SimpleNamespace(
        subentries={
            "system": SimpleNamespace(subentry_type=DEVICE_SYSTEM, subentry_id="system-subentry"),
        }
    )
    system_entity = SimpleNamespace(planner_device_key=DEVICE_SYSTEM)
    energy_entity = SimpleNamespace(planner_device_key=DEVICE_ENERGY)
    calls: list[tuple[list[object], str | None]] = []

    assert planner_device_configured(entry, DEVICE_SYSTEM) is True
    assert planner_device_configured(entry, DEVICE_ENERGY) is False

    async_add_planner_entities(
        entry,
        lambda entities, *, config_subentry_id: calls.append((entities, config_subentry_id)),
        [system_entity, energy_entity],
    )

    assert calls == [([system_entity], "system-subentry")]
