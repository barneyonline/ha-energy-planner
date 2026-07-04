"""Tests for binary sensor state semantics."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner import binary_sensor as binary_sensor_module
from custom_components.ha_energy_planner.binary_sensor import (
    BINARY_SENSORS,
    PlannerBinarySensor,
    _planner_ownership_active,
)
from custom_components.ha_energy_planner.models import InputHealth


def test_data_health_uses_problem_semantics() -> None:
    data_health = next(description for description in BINARY_SENSORS if description.key == "data_healthy")

    assert data_health.device_class == "problem"
    assert data_health.value_fn(SimpleNamespace(data=SimpleNamespace(health=InputHealth.HEALTHY))) is False
    assert data_health.value_fn(SimpleNamespace(data=SimpleNamespace(health=InputHealth.UNSAFE))) is True
    assert data_health.value_fn(SimpleNamespace(data=None)) is True


def test_binary_sensor_setup_and_entity_state(monkeypatch: object) -> None:
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="entry-1"),
        data=SimpleNamespace(health=InputHealth.HEALTHY),
        store=SimpleNamespace(data={"ownership": {}}),
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[object] = []

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.extend(entities)

    monkeypatch.setattr(binary_sensor_module, "async_add_planner_entities", fake_add_planner_entities)

    asyncio.run(binary_sensor_module.async_setup_entry(None, entry, None))
    entity = PlannerBinarySensor(coordinator, BINARY_SENSORS[0])

    assert len(added) == len(BINARY_SENSORS)
    assert entity.is_on is False


def test_takeover_active_uses_persisted_planner_ownership_not_candidate_actions() -> None:
    coordinator = SimpleNamespace(
        data=SimpleNamespace(actions=[object()]),
        store=SimpleNamespace(data={"ownership": {}}),
    )

    takeover_description = next(description for description in BINARY_SENSORS if description.key == "takeover_active")

    assert takeover_description.device_class == "running"
    assert takeover_description.value_fn(coordinator) is False


def test_takeover_active_reports_persisted_asset_ownership() -> None:
    takeover_description = next(description for description in BINARY_SENSORS if description.key == "takeover_active")
    ownership_cases = [
        {"ev_smart_charging_state": {"ev_smart_charging_start_entity": "off"}},
        {"climate_automations": {"automation.climate": "on"}},
        {"enphase_profile": "AI Optimisation"},
        {"enphase_profile_changed_at": "2026-06-27T00:00:00+00:00"},
        {"planner_hvac_action_expires_at": "2026-06-27T00:02:00+00:00"},
        {"planner_takeover_started_at": "2026-06-27T00:00:00+00:00"},
    ]
    for ownership in ownership_cases:
        coordinator = SimpleNamespace(
            data=SimpleNamespace(actions=[]),
            store=SimpleNamespace(data={"ownership": ownership}),
        )
        assert takeover_description.value_fn(coordinator) is True


def test_manual_override_metadata_is_not_planner_takeover() -> None:
    assert not _planner_ownership_active(
        {
            "ownership": {
                "manual_hvac_override_expires_at": "2026-06-27T02:00:00+00:00",
            }
        }
    )
