"""Tests for base planner entity metadata."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ha_energy_planner.const import DOMAIN
from custom_components.ha_energy_planner.entity import (
    EnergyPlannerEntity,
    async_add_planner_entities,
    planner_device_identifier,
)


def test_planner_entity_uses_single_named_device() -> None:
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="entry-1", title="House Energy Planner")
    )

    entity = EnergyPlannerEntity(coordinator, "plan_status")

    assert entity.unique_id == "entry-1_plan_status"
    assert entity.device_info["identifiers"] == {(DOMAIN, "entry-1")}
    assert entity.device_info["name"] == "House Energy Planner"
    assert entity.device_info["model"] == "Energy Planner"
    assert planner_device_identifier("entry-1") == (DOMAIN, "entry-1")


def test_planner_entity_ignores_legacy_group_hint() -> None:
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="entry-1", title="Energy Planner")
    )

    entity = EnergyPlannerEntity(coordinator, "ai_enabled", "ai")

    assert entity.device_info["identifiers"] == {(DOMAIN, "entry-1")}
    assert entity.device_info["name"] == "Energy Planner"


def test_add_planner_entities_adds_every_entity_to_main_entry() -> None:
    entities = [object(), object()]
    calls: list[list[object]] = []

    async_add_planner_entities(
        SimpleNamespace(),
        lambda added: calls.append(added),
        entities,
    )

    assert calls == [entities]
