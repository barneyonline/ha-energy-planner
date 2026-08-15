"""Tests for binary sensor state semantics."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner import binary_sensor as binary_sensor_module
from custom_components.ha_energy_planner.binary_sensor import (
    BINARY_SENSORS,
    LEGACY_BINARY_SENSOR_DESCRIPTIONS,
    PlannerBinarySensor,
    _current_plan_block_reason,
    _planner_ownership_active,
)
from custom_components.ha_energy_planner.entity import RECORDER_STATE_ATTRIBUTES_TARGET_BYTES
from custom_components.ha_energy_planner.models import InputHealth


class _AvailableStates:
    def __init__(self, entry_data: dict[str, object]) -> None:
        self._entity_ids = {
            item.strip()
            for value in entry_data.values()
            for item in str(value or "").split(",")
            if "." in item
        }

    def get(self, entity_id: str) -> object | None:
        if entity_id not in self._entity_ids:
            return None
        return SimpleNamespace(state="on", attributes={})


class _AvailableServices:
    @staticmethod
    def has_service(domain: str, service: str) -> bool:
        return bool(domain and service)


def _armed_runtime(
    entry_data: dict[str, object],
    *,
    health: object = InputHealth.HEALTHY,
    status: str = "current",
    confidence: float = 1.0,
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "hass": SimpleNamespace(
            states=_AvailableStates(entry_data),
            services=_AvailableServices(),
        ),
        "data": SimpleNamespace(
            health=health,
            status=status,
            confidence=confidence,
            confidence_breakdown={
                "tariff": confidence,
                "solar": confidence,
                "load": confidence,
                "climate": confidence,
                "ev": confidence,
                "enphase": confidence,
            },
            created_at=now,
            interval_minutes=5,
            horizon_hours=24,
            estimated_cost_horizon_hours=24,
            input_issues=[],
        ),
        "last_refresh_metadata": {"succeeded": True, "completed_at": now},
    }


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
        hass=SimpleNamespace(
            states=_AvailableStates({}),
            services=_AvailableServices(),
        ),
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
    entry_data = {"ev_charger_entity": "switch.ev"}
    options = {"ev_control_enabled": True, "planner_enabled": True}
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                },
                "control_pause": {"active": True, "reason": "maintenance"},
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        **_armed_runtime(entry_data),
    )
    description = BINARY_SENSORS[0]

    assert description.value_fn(coordinator) is False
    attrs = description.attrs_fn(coordinator)
    assert attrs["automatic_control"] is False
    assert attrs["arming_requested"] is True
    assert attrs["reason"] == "planner_paused"
    assert attrs["required_control_areas"] == ["ev"]


def test_armed_sensor_keeps_unpaused_control_area_active() -> None:
    entry_data = {
        "ev_charger_entity": "switch.ev",
        "daikin_climate_entity": "climate.daikin",
    }
    options = {
        "ev_control_enabled": True,
        "climate_control_enabled": True,
        "planner_enabled": True,
    }
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                },
                "control_pause": {"active": True, "assets": ["ev"]},
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **_armed_runtime(entry_data),
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["armed"] is True
    assert attrs["automatic_control"] is True
    assert attrs["reason"] == "armed"
    assert attrs["available_control_areas"] == ["hvac"]
    assert attrs["paused_control_areas"] == ["ev"]

    coordinator.effective_control = False
    blocked_attrs = BINARY_SENSORS[0].attrs_fn(coordinator)
    assert blocked_attrs["armed"] is False
    assert blocked_attrs["reason"] == "active_control_not_ready"


def test_armed_sensor_rejects_unsafe_current_plan() -> None:
    entry_data = {"ev_charger_entity": "switch.ev"}
    options = {"ev_control_enabled": True, "planner_enabled": True}
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                }
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **_armed_runtime(entry_data, health=InputHealth.UNSAFE, status="unsafe", confidence=0.0),
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["armed"] is False
    assert attrs["arming_requested"] is True
    assert attrs["automatic_control"] is False
    assert attrs["current_plan_safe"] is False
    assert attrs["reason"] == "input_health_unsafe"


def test_armed_sensor_rejects_stale_or_failed_refresh_evidence() -> None:
    entry_data = {"ev_charger_entity": "switch.ev"}
    options = {"ev_control_enabled": True, "planner_enabled": True}
    runtime = _armed_runtime(entry_data)
    runtime["data"].created_at -= timedelta(hours=1)
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                }
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **runtime,
    )

    stale = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert stale["armed"] is False
    assert stale["reason"] == "current_plan_stale"

    coordinator.data.created_at = coordinator.last_refresh_metadata["completed_at"]
    coordinator.last_refresh_metadata["succeeded"] = False
    failed = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert failed["armed"] is False
    assert failed["reason"] == "current_plan_refresh_unconfirmed"


def test_armed_sensor_does_not_treat_an_unavailable_area_as_unpaused_authority() -> None:
    entry_data = {
        "ev_charger_entity": "switch.ev",
        "daikin_climate_entity": "climate.daikin",
    }
    options = {
        "ev_control_enabled": True,
        "climate_control_enabled": True,
        "planner_enabled": True,
    }
    runtime = _armed_runtime(entry_data)
    runtime["hass"].states._entity_ids.remove("climate.daikin")
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                },
                "control_pause": {"active": True, "assets": ["ev"]},
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **runtime,
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["armed"] is False
    assert attrs["ready_control_areas"] == ["ev"]
    assert attrs["blocked_control_areas"] == ["hvac"]
    assert attrs["available_control_areas"] == []
    assert attrs["paused_control_areas"] == ["ev"]

    coordinator.hass.states._entity_ids.remove("switch.ev")
    coordinator.store.data["control_pause"] = {}
    no_ready_area = BINARY_SENSORS[0].attrs_fn(coordinator)
    assert no_ready_area["armed"] is False
    assert no_ready_area["reason"] == "no_ready_control_area"


def test_armed_sensor_ignores_unrelated_optional_entity_availability() -> None:
    entry_data = {
        "ev_charger_entity": "switch.ev",
        "carbon_intensity_forecast_entity": "sensor.carbon",
    }
    options = {"ev_control_enabled": True, "planner_enabled": True}
    runtime = _armed_runtime(entry_data, health=InputHealth.DEGRADED, confidence=0.65)
    runtime["hass"].states._entity_ids.remove("sensor.carbon")
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                }
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **runtime,
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["armed"] is True
    assert attrs["ready_control_areas"] == ["ev"]
    assert attrs["blocked_control_areas"] == []
    assert attrs["available_control_areas"] == ["ev"]


def test_armed_sensor_applies_keep_on_persistent_control_capability() -> None:
    entry_data = {
        "ev_smart_charging_start_entity": "button.ev_start",
        "ev_smart_charging_stop_entity": "button.ev_stop",
    }
    options = {
        "ev_control_enabled": True,
        "ev_keep_charger_on": True,
        "planner_enabled": True,
    }
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                }
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **_armed_runtime(entry_data),
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["armed"] is False
    assert attrs["ready_control_areas"] == []
    assert attrs["blocked_control_areas"] == ["ev"]
    assert attrs["reason"] == "no_ready_control_area"


def test_armed_sensor_requires_confidence_eligible_available_area() -> None:
    entry_data = {"ev_charger_entity": "switch.ev"}
    options = {"ev_control_enabled": True, "planner_enabled": True}
    runtime = _armed_runtime(entry_data, health=InputHealth.DEGRADED, confidence=0.4)
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
                        entry_data,
                        options,
                    ),
                }
            }
        ),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **runtime,
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["armed"] is False
    assert attrs["arming_requested"] is True
    assert attrs["current_plan_safe"] is False
    assert attrs["confidence_eligible_control_areas"] == []
    assert attrs["reason"] == "no_confidence_eligible_control_area"


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        (None, "current_plan_unavailable"),
        (SimpleNamespace(health="mystery"), "input_health_unsafe"),
        (
            SimpleNamespace(health=InputHealth.HEALTHY, status="unsafe"),
            "current_plan_not_current",
        ),
        (
            SimpleNamespace(
                health=InputHealth.HEALTHY,
                status="current",
                confidence=0.0,
            ),
            "current_plan_confidence_zero",
        ),
        (
            SimpleNamespace(
                health=InputHealth.DEGRADED,
                status="current",
                confidence=0.65,
            ),
            None,
        ),
    ],
)
def test_current_plan_block_reason_is_stable(plan: object | None, reason: str | None) -> None:
    assert _current_plan_block_reason(plan) == reason


def test_current_plan_block_reason_rejects_stale_or_unconfirmed_refresh() -> None:
    entry_data = {"ev_charger_entity": "switch.ev"}
    runtime = _armed_runtime(entry_data)
    plan = runtime["data"]
    refresh = runtime["last_refresh_metadata"]
    now = plan.created_at

    assert (
        _current_plan_block_reason(
            plan,
            now=now,
            last_refresh_metadata=refresh,
        )
        is None
    )

    plan.created_at = now - timedelta(hours=1)
    assert (
        _current_plan_block_reason(
            plan,
            now=now,
            last_refresh_metadata=refresh,
        )
        == "current_plan_stale"
    )

    plan.created_at = now
    refresh["succeeded"] = False
    assert (
        _current_plan_block_reason(
            plan,
            now=now,
            last_refresh_metadata=refresh,
        )
        == "current_plan_refresh_unconfirmed"
    )

    refresh["succeeded"] = True
    plan.estimated_cost_horizon_hours = 1.0
    assert (
        _current_plan_block_reason(
            plan,
            now=now,
            last_refresh_metadata=refresh,
        )
        == "current_plan_coverage_inadequate"
    )


def test_armed_sensor_exposes_startup_auto_recovery_progress() -> None:
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": False,
                    "startup_auto_recovery": {
                        "status": "waiting",
                        "successful_runs": 1,
                        "required_runs": 3,
                        "started_at": "2026-08-15T12:00:00+00:00",
                        "last_reason": "configured_entities_unavailable",
                    },
                }
            }
        ),
        entry_data={"ev_charger_entity": "switch.ev"},
        options={"ev_control_enabled": True},
        dry_run=False,
        active_control=False,
        automatic_control_requested=True,
        **_armed_runtime({"ev_charger_entity": "switch.ev"}),
    )

    attrs = BINARY_SENSORS[0].attrs_fn(coordinator)

    assert attrs["startup_auto_recovery_status"] == "waiting"
    assert attrs["automatic_control"] is False
    assert attrs["automatic_control_requested"] is True
    assert attrs["startup_auto_recovery_successful_runs"] == 1
    assert attrs["startup_auto_recovery_grace_started_at"] == "2026-08-15T12:00:00+00:00"
    assert attrs["startup_auto_recovery_last_reason"] == "configured_entities_unavailable"


def test_armed_sensor_explains_disarmed_and_armed_active_states() -> None:
    entry_data = {"ev_charger_entity": "switch.ev"}
    options = {"ev_control_enabled": True, "planner_enabled": True}
    coordinator = SimpleNamespace(
        store=SimpleNamespace(data={"production": {"armed": False}}),
        entry_data=entry_data,
        options=options,
        dry_run=False,
        active_control=False,
        **_armed_runtime(entry_data),
    )
    description = BINARY_SENSORS[0]

    assert description.attrs_fn(coordinator)["reason"] == "safety_gate_not_armed"
    coordinator.store.data["production"] = {
        "armed": True,
        "dry_run_ready_cycles": 3,
        "dry_run_evidence_fingerprint": binary_sensor_module.production_evidence_fingerprint(
            entry_data,
            options,
        ),
    }
    coordinator.active_control = True
    assert description.attrs_fn(coordinator)["reason"] == "armed"

    coordinator.store.data["production"]["dry_run_ready_cycles"] = 2
    incomplete = description.attrs_fn(coordinator)
    assert incomplete["armed"] is False
    assert incomplete["reason"] == "production_dry_run_evidence_incomplete"


def test_armed_sensor_fails_closed_when_reviewed_evidence_is_stale() -> None:
    coordinator = SimpleNamespace(
        store=SimpleNamespace(
            data={
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "dry_run_evidence_fingerprint": "stale-contract",
                }
            }
        ),
        entry_data={"ev_charger_entity": "switch.ev"},
        options={"ev_control_enabled": True, "planner_enabled": True},
        dry_run=False,
        active_control=True,
        automatic_control_requested=True,
        **_armed_runtime({"ev_charger_entity": "switch.ev"}),
    )
    description = BINARY_SENSORS[0]

    assert description.value_fn(coordinator) is False
    attrs = description.attrs_fn(coordinator)
    assert attrs["armed"] is False
    assert attrs["arming_requested"] is True
    assert attrs["automatic_control"] is False
    assert attrs["mode"] == "review"
    assert attrs["reason"] == "production_evidence_contract_changed"


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
