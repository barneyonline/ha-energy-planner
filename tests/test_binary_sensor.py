"""Tests for binary sensor state semantics."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from custom_components.ha_energy_planner import binary_sensor as binary_sensor_module
from custom_components.ha_energy_planner.binary_sensor import (
    BINARY_SENSORS,
    LEGACY_BINARY_SENSOR_DESCRIPTIONS,
    PlannerBinarySensor,
    _planner_ownership_active,
)
from custom_components.ha_energy_planner.entity import RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
from custom_components.ha_energy_planner.models import InputHealth


def test_data_health_uses_problem_semantics() -> None:
    data_health = next(
        description for description in LEGACY_BINARY_SENSOR_DESCRIPTIONS if description.key == "data_healthy"
    )

    assert data_health.device_class == "problem"
    assert data_health.value_fn(SimpleNamespace(data=SimpleNamespace(health=InputHealth.HEALTHY))) is False
    assert data_health.value_fn(SimpleNamespace(data=SimpleNamespace(health=InputHealth.UNSAFE))) is True
    assert data_health.value_fn(SimpleNamespace(data=None)) is True


def test_binary_sensor_setup_and_entity_state(monkeypatch: object) -> None:
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="entry-1"),
        data=SimpleNamespace(health=InputHealth.HEALTHY),
        store=SimpleNamespace(data={"ownership": {}, "production": {"armed": False}}),
        entry_data={},
        options={},
        dry_run=True,
        active_control=False,
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list[object] = []
    removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str:
            return f"binary_sensor.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    def fake_add_planner_entities(entry_arg: object, add_entities: object, entities: object) -> None:
        added.extend(entities)

    monkeypatch.setattr(binary_sensor_module, "async_add_planner_entities", fake_add_planner_entities)
    monkeypatch.setattr(binary_sensor_module.er, "async_get", lambda hass: FakeRegistry())

    asyncio.run(binary_sensor_module.async_setup_entry(SimpleNamespace(), entry, None))
    entity = PlannerBinarySensor(coordinator, BINARY_SENSORS[0])

    assert len(added) == len(BINARY_SENSORS)
    assert entity.is_on is False
    assert entity.extra_state_attributes["mode"] == "review"
    assert len(removed) == len(LEGACY_BINARY_SENSOR_DESCRIPTIONS)


def test_binary_sensor_attributes_use_shared_recorder_budget() -> None:
    coordinator = SimpleNamespace(entry=SimpleNamespace(entry_id="entry-1"))
    description = replace(
        BINARY_SENSORS[0],
        attrs_fn=lambda coordinator: {"evidence": ["⚡" * 10_000] * 20},
    )

    attrs = PlannerBinarySensor(coordinator, description).extra_state_attributes
    encoded_size = len(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    assert encoded_size <= RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
    assert attrs["attributes_truncated"] is True


def test_armed_sensor_exposes_active_gate_and_pause_reason() -> None:
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {"armed": True, "dry_run_ready_cycles": 3},
                "control_pause": {"active": True, "reason": "maintenance"},
            }
        ),
        entry_data={"ev_charger_entity": "switch.ev"},
        options={"ev_control_enabled": True},
        dry_run=False,
        active_control=True,
    )
    description = BINARY_SENSORS[0]

    assert description.value_fn(coordinator) is True
    attrs = description.attrs_fn(coordinator)
    assert attrs["automatic_control"] is True
    assert attrs["reason"] == "planner_paused"
    assert attrs["required_control_areas"] == ["ev"]


def test_armed_sensor_explains_disarmed_and_armed_active_states() -> None:
    coordinator = SimpleNamespace(
        store=SimpleNamespace(data={"production": {"armed": False}}),
        entry_data={"ev_charger_entity": "switch.ev"},
        options={"ev_control_enabled": True},
        dry_run=False,
        active_control=False,
    )
    description = BINARY_SENSORS[0]

    assert description.attrs_fn(coordinator)["reason"] == "safety_gate_not_armed"
    coordinator.store.data["production"] = {"armed": True}
    coordinator.active_control = True
    assert description.attrs_fn(coordinator)["reason"] == "armed"


def test_takeover_active_uses_persisted_planner_ownership_not_candidate_actions() -> None:
    coordinator = SimpleNamespace(
        data=SimpleNamespace(actions=[object()]),
        store=SimpleNamespace(data={"ownership": {}}),
    )

    takeover_description = next(
        description for description in LEGACY_BINARY_SENSOR_DESCRIPTIONS if description.key == "takeover_active"
    )

    assert takeover_description.device_class == "running"
    assert takeover_description.value_fn(coordinator) is False


def test_takeover_active_reports_persisted_asset_ownership() -> None:
    takeover_description = next(
        description for description in LEGACY_BINARY_SENSOR_DESCRIPTIONS if description.key == "takeover_active"
    )
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


def test_takeover_active_reports_reservation_only_ev_recovery() -> None:
    assert _planner_ownership_active(
        {
            "ownership": {},
            "ev_grid_reservation": {"active": True, "load_kw": 7.2},
        }
    )
    assert _planner_ownership_active(
        {
            "ownership": {
                "ev_smart_charging_command_entity_id": "button.ev_start",
                "ev_smart_charging_control_topology": {"ev_charger_stop_entity": "button.ev_stop"},
            },
            "ev_grid_reservation": {"active": False},
        }
    )
    assert not _planner_ownership_active({"ownership": {}, "ev_grid_reservation": {"active": False}})
    assert not _planner_ownership_active(
        {
            "ownership": {},
            "ev_grid_reservation": {
                "active": True,
                "external_baseline": True,
                "load_kw": 7.2,
            },
        }
    )
