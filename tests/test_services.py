"""Tests for integration-level service handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner import async_setup
from custom_components.ha_energy_planner.const import (
    ATTR_ASSET,
    ATTR_DURATION_MINUTES,
    ATTR_READY_BY,
    ATTR_REASON,
    DOMAIN,
    SERVICE_ARM_PRODUCTION_CONTROL,
    SERVICE_DISARM_PRODUCTION_CONTROL,
    SERVICE_EXPORT_SUPPORT_BUNDLE,
    SERVICE_PAUSE_CONTROL,
    SERVICE_REPLAN,
    SERVICE_RESTORE_SAFE_STATE,
    SERVICE_RESUME_CONTROL,
    SERVICE_RUN_PREFLIGHT,
    SERVICE_SET_EV_READY_BY,
    SERVICE_SET_MANUAL_HVAC_OVERRIDE,
)
from custom_components.ha_energy_planner.coordinator import EnergyPlannerCoordinator
from custom_components.ha_energy_planner.models import EnergyPlan, InputHealth, PlannerMode
from custom_components.ha_energy_planner.preflight import production_evidence_fingerprint


@dataclass(slots=True)
class FakeCall:
    """Minimal Home Assistant service call."""

    data: dict[str, Any]


class FakeServices:
    """Capture registered service handlers."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Any] = {}
        self.schemas: dict[tuple[str, str], Any] = {}
        self.available: set[tuple[str, str]] = {
            ("haeo", "optimize"),
            ("select", "select_option"),
            ("fake_ai", "advice"),
        }

    def async_register(self, domain: str, service: str, handler: Any, **kwargs: Any) -> None:
        self.handlers[(domain, service)] = handler
        self.schemas[(domain, service)] = kwargs.get("schema")
        self.available.add((domain, service))

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.available


@dataclass(slots=True)
class FakeState:
    """Minimal Home Assistant state."""

    state: str = "on"


class FakeStates:
    """Minimal state registry."""

    def __init__(self) -> None:
        self.values = {
            "sensor.import_price": "0.25",
            "sensor.export_price": "0.08",
            "sensor.pv_forecast": "2.5",
            "sensor.baseline_load": "1.2",
            "sensor.battery_soc": "55",
            "select.enphase_profile": "AI Optimisation",
            "climate.daikin": "heat",
            "input_number.climate_low": "18",
            "input_number.climate_high": "24",
            "person.home": "home",
            "input_boolean.ev_start": "off",
            "input_boolean.ev_stop": "on",
        }

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeConfigEntries:
    """Return fake config entries."""

    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        self.coordinator = coordinator

    def async_entries(self, domain: str) -> list[Any]:
        return [type("Entry", (), {"runtime_data": self.coordinator})()]


class FakeHass:
    """Minimal Home Assistant object."""

    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        self.config_entries = FakeConfigEntries(coordinator)
        self.services = FakeServices()
        self.states = FakeStates()
        self.config = type("Config", (), {"components": {"recorder"}})()
        self.data: dict[str, Any] = {}
        self.created_tasks: list[Any] = []

    def async_create_task(self, coro: Any) -> None:
        self.created_tasks.append(coro)


def test_non_response_services_await_coordinator_work() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))

    calls = [
        (SERVICE_REPLAN, {}),
        (SERVICE_RESTORE_SAFE_STATE, {ATTR_REASON: "test_restore"}),
        (SERVICE_SET_EV_READY_BY, {ATTR_READY_BY: "08:30"}),
        (
            SERVICE_SET_MANUAL_HVAC_OVERRIDE,
            {ATTR_DURATION_MINUTES: 15, ATTR_REASON: "test_override"},
        ),
        (SERVICE_ARM_PRODUCTION_CONTROL, {ATTR_REASON: "test_arm"}),
        (SERVICE_DISARM_PRODUCTION_CONTROL, {ATTR_REASON: "test_disarm"}),
        (SERVICE_PAUSE_CONTROL, {ATTR_DURATION_MINUTES: 60, ATTR_REASON: "test_pause", ATTR_ASSET: "ev"}),
        (SERVICE_RESUME_CONTROL, {ATTR_REASON: "test_resume"}),
    ]
    for service, data in calls:
        handler = hass.services.handlers[(DOMAIN, service)]
        asyncio.run(handler(FakeCall(data)))

    assert coordinator.awaited == [
        ("replan", None),
        ("restore", "test_restore"),
        ("ready_by", "08:30"),
        ("manual_override", (15, "test_override")),
        ("arm", "test_arm"),
        ("disarm", "test_disarm"),
        ("pause", (60, "test_pause", "ev")),
        ("resume", "test_resume"),
    ]
    assert hass.created_tasks == []


def test_set_ev_ready_by_schema_validates_and_normalizes_time() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))
    schema = hass.services.schemas[(DOMAIN, SERVICE_SET_EV_READY_BY)]

    assert schema({ATTR_READY_BY: "8:05:00"}) == {ATTR_READY_BY: "08:05"}


def test_set_ev_ready_by_schema_rejects_invalid_time() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))
    schema = hass.services.schemas[(DOMAIN, SERVICE_SET_EV_READY_BY)]

    try:
        schema({ATTR_READY_BY: "24:90"})
    except Exception as err:  # noqa: BLE001 - assert schema rejects invalid service data.
        assert "ready_by must be a valid local time" in str(err)
    else:
        raise AssertionError("Invalid ready_by time was accepted")


def test_reason_code_schemas_accept_compact_codes() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))
    restore_schema = hass.services.schemas[(DOMAIN, SERVICE_RESTORE_SAFE_STATE)]
    manual_schema = hass.services.schemas[(DOMAIN, SERVICE_SET_MANUAL_HVAC_OVERRIDE)]

    assert restore_schema({ATTR_REASON: " user.restore-1 "}) == {ATTR_REASON: "user.restore-1"}
    assert manual_schema({ATTR_DURATION_MINUTES: "15", ATTR_REASON: "manual:test_1"}) == {
        ATTR_DURATION_MINUTES: 15,
        ATTR_REASON: "manual:test_1",
    }


def test_reason_code_schemas_reject_free_form_sensitive_text() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))
    restore_schema = hass.services.schemas[(DOMAIN, SERVICE_RESTORE_SAFE_STATE)]
    manual_schema = hass.services.schemas[(DOMAIN, SERVICE_SET_MANUAL_HVAC_OVERRIDE)]

    for schema, data in [
        (restore_schema, {ATTR_REASON: "token abc123"}),
        (manual_schema, {ATTR_DURATION_MINUTES: 15, ATTR_REASON: "contains secret token"}),
        (restore_schema, {ATTR_REASON: "x" * 81}),
    ]:
        try:
            schema(data)
        except Exception as err:  # noqa: BLE001 - assert schema rejects unsafe service data.
            assert "reason must be a compact redacted reason code" in str(err)
        else:
            raise AssertionError("Unsafe reason text was accepted")


def test_run_preflight_returns_active_mode_readiness_without_scheduling_work() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))

    handler = hass.services.handlers[(DOMAIN, SERVICE_RUN_PREFLIGHT)]
    response = asyncio.run(handler(FakeCall({})))

    assert response["ok"] is True
    assert response["active_control_ready"] is True
    assert response["mode"] == {
        "planner_enabled": False,
        "dry_run": True,
        "safe_first_run_mode": True,
        "active_mode_requested": False,
    }
    assert response["recorder"] == {"available": True}
    assert response["audit"]["last_outcome"]["action_id"] == "restore_safe_state"
    assert hass.created_tasks == []


def test_run_preflight_reports_missing_configured_entities_and_services() -> None:
    coordinator = _coordinator()
    coordinator.entry.data["ev_smart_charging_stop_entity"] = "input_boolean.missing_stop"
    coordinator.entry.data["haeo_optimize_service"] = "haeo.missing"
    coordinator.entry.options["planner_enabled"] = True
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))

    handler = hass.services.handlers[(DOMAIN, SERVICE_RUN_PREFLIGHT)]
    response = asyncio.run(handler(FakeCall({})))

    assert response["ok"] is False
    assert "input_boolean.missing_stop" in response["entities"]["missing"]
    assert "haeo.missing" in response["services"]["unavailable"]
    assert response["discovery"]["haeo"]["supported"] is False
    checks = {check["check"]: check for check in response["checks"]}
    assert "input_boolean.missing_stop" in checks["configured_entities_available"]["message"]
    assert "haeo.missing" in checks["configured_services_available"]["message"]


def test_run_preflight_accepts_unknown_stateless_ev_buttons() -> None:
    coordinator = _coordinator()
    coordinator.entry.data["ev_smart_charging_start_entity"] = "button.ev_start"
    coordinator.entry.data["ev_smart_charging_stop_entity"] = "button.ev_stop"
    hass = FakeHass(coordinator)
    hass.states.values["button.ev_start"] = "unknown"
    hass.states.values["button.ev_stop"] = "unknown"
    asyncio.run(async_setup(hass, {}))

    handler = hass.services.handlers[(DOMAIN, SERVICE_RUN_PREFLIGHT)]
    response = asyncio.run(handler(FakeCall({})))

    checks = {check["check"]: check for check in response["checks"]}
    assert checks["configured_entities_available"]["ok"] is True
    assert response["entities"]["unavailable"] == []
    assert response["discovery"]["ev"]["supported"] is True


def test_run_preflight_reports_production_gate_reasons() -> None:
    coordinator = _coordinator()
    coordinator.entry.options["ev_control_enabled"] = False
    coordinator.store.data["production"] = {
        "armed": False,
        "dry_run_ready_cycles": 1,
    }
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))

    handler = hass.services.handlers[(DOMAIN, SERVICE_RUN_PREFLIGHT)]
    response = asyncio.run(handler(FakeCall({})))

    checks = {check["check"]: check for check in response["checks"]}
    assert response["ok"] is False
    assert checks["dry_run_evidence_complete"]["ok"] is False
    assert "1/3 healthy dry-run cycles" in checks["dry_run_evidence_complete"]["message"]
    assert checks["production_control_armed"]["ok"] is False
    assert "has not been armed" in checks["production_control_armed"]["message"]


def test_run_preflight_supports_ev_only_control() -> None:
    coordinator = _partial_coordinator(
        {
            "ev_smart_charging_start_entity": "input_boolean.ev_start",
            "ev_smart_charging_stop_entity": "input_boolean.ev_stop",
        },
        ev_control_enabled=True,
    )
    response = _run_preflight(coordinator)

    assert response["ok"] is True
    assert response["control_areas"]["required"] == ["ev"]
    assert response["production"]["required_control_areas"] == ["ev"]
    assert response["discovery"]["hvac"]["supported"] is False
    assert response["discovery"]["enphase"]["supported"] is False


def test_run_preflight_supports_enphase_only_control() -> None:
    coordinator = _partial_coordinator(
        {
            "enphase_profile_entity": "select.enphase_profile",
            "enphase_ai_profile": "AI Optimisation",
        },
        enphase_control_enabled=True,
    )
    response = _run_preflight(coordinator)

    assert response["ok"] is True
    assert response["control_areas"]["required"] == ["enphase"]
    assert response["services"]["missing"] == []


def test_run_preflight_supports_hvac_only_control() -> None:
    coordinator = _partial_coordinator(
        {"daikin_climate_entity": "climate.daikin"},
        climate_control_enabled=True,
    )
    response = _run_preflight(coordinator)

    assert response["ok"] is True
    assert response["control_areas"]["required"] == ["hvac"]
    assert response["production"]["ready_to_arm"] is True


def test_run_preflight_blocks_enabled_but_unconfigured_control() -> None:
    response = _run_preflight(_partial_coordinator({}, ev_control_enabled=True))

    assert response["ok"] is False
    assert response["control_areas"]["required"] == ["ev"]
    assert response["control_areas"]["details"]["ev"]["configured"] is False
    assert response["discovery"]["ev"]["supported"] is False


def test_run_preflight_ignores_entities_for_disabled_control_areas() -> None:
    coordinator = _partial_coordinator(
        {
            "ev_smart_charging_start_entity": "input_boolean.ev_start",
            "ev_smart_charging_stop_entity": "input_boolean.ev_stop",
            "daikin_climate_entity": "climate.missing",
        },
        ev_control_enabled=True,
        climate_control_enabled=False,
    )

    response = _run_preflight(coordinator)

    assert response["ok"] is True
    assert response["control_areas"]["required"] == ["ev"]
    assert "climate.missing" not in response["entities"]["configured"]
    assert response["entities"]["missing"] == []


def test_run_preflight_no_control_dry_run_treats_discovery_as_advisory() -> None:
    coordinator = _partial_coordinator(
        {"haeo_optimize_service": "haeo.missing"},
        planner_enabled=False,
        dry_run=True,
    )
    response = _run_preflight(coordinator)

    assert response["ok"] is False
    assert response["control_areas"]["configured"] == ["haeo"]
    assert response["control_areas"]["required"] == []
    assert response["services"]["configured"] == []
    assert response["production"]["ready_to_arm"] is False
    checks = {check["check"]: check for check in response["checks"]}
    assert checks["required_control_areas_supported"]["ok"] is True
    assert "advisory" in checks["required_control_areas_supported"]["message"]


def test_run_preflight_blocks_a_mixed_unsupported_enabled_area() -> None:
    coordinator = _partial_coordinator(
        {
            "ev_smart_charging_start_entity": "input_boolean.ev_start",
            "ev_smart_charging_stop_entity": "input_boolean.ev_stop",
            "daikin_climate_entity": "climate.missing",
        },
        ev_control_enabled=True,
        climate_control_enabled=True,
    )
    response = _run_preflight(coordinator)

    assert response["ok"] is False
    assert response["control_areas"]["required"] == ["ev", "hvac"]
    assert response["discovery"]["ev"]["supported"] is True
    assert response["discovery"]["hvac"]["supported"] is False
    checks = {check["check"]: check for check in response["checks"]}
    assert checks["required_control_areas_supported"]["ok"] is False
    assert "hvac" in checks["required_control_areas_supported"]["message"]


def test_run_preflight_requires_configured_haeo_for_enabled_planning() -> None:
    coordinator = _partial_coordinator(
        {"haeo_optimize_service": "haeo.optimize"},
        planner_enabled=True,
        dry_run=False,
    )
    response = _run_preflight(coordinator)

    assert response["ok"] is True
    assert response["control_areas"]["required"] == ["haeo"]
    assert response["services"]["configured"] == ["haeo.optimize"]


def test_run_preflight_separates_historical_evidence_from_current_safety() -> None:
    coordinator = _coordinator()
    coordinator.data.health = InputHealth.UNSAFE
    coordinator.data.status = "unsafe"
    coordinator.data.confidence = 0.0
    coordinator.data.estimated_cost_horizon_hours = 16.0

    response = _run_preflight(coordinator)

    assert response["production"]["dry_run_evidence_complete"] is True
    assert response["production"]["ready_to_arm"] is True
    assert response["safe_to_activate_now"] is False
    assert response["active_control_ready"] is False
    assert response["current_plan"]["healthy"] is False


def test_run_preflight_requires_eight_usable_priced_hours() -> None:
    coordinator = _coordinator()
    coordinator.data.estimated_cost_horizon_hours = 7.5

    response = _run_preflight(coordinator)

    assert response["safe_to_activate_now"] is False
    assert response["current_plan"]["adequate_coverage"] is False
    assert response["current_plan"]["required_optimization_horizon_hours"] == 8.0


def test_run_preflight_accepts_full_configured_horizon_when_shorter_than_eight_hours() -> None:
    coordinator = _coordinator()
    coordinator.data.horizon_hours = 4
    coordinator.data.estimated_cost_horizon_hours = 4.0

    response = _run_preflight(coordinator)

    assert response["safe_to_activate_now"] is True
    assert response["active_control_ready"] is True
    assert response["current_plan"]["required_optimization_horizon_hours"] == 4.0


def test_run_preflight_rejects_stale_or_unconfirmed_plan() -> None:
    coordinator = _coordinator()
    coordinator.data.created_at -= timedelta(hours=1)
    stale = _run_preflight(coordinator)
    coordinator = _coordinator()
    coordinator.last_refresh_metadata["succeeded"] = False
    failed = _run_preflight(coordinator)

    assert stale["safe_to_activate_now"] is False
    assert stale["current_plan"]["fresh"] is False
    assert failed["safe_to_activate_now"] is False
    assert failed["current_plan"]["last_refresh_succeeded"] is False


def test_run_preflight_rejects_active_pause_and_changed_control_contract() -> None:
    coordinator = _coordinator()
    coordinator.store.data["control_pause"] = {
        "active": True,
        "until": datetime.now(UTC) + timedelta(minutes=10),
        "assets": ["ev"],
    }
    paused = _run_preflight(coordinator)
    coordinator = _coordinator()
    coordinator.entry.options["climate_control_enabled"] = False
    changed = _run_preflight(coordinator)

    assert paused["safe_to_activate_now"] is False
    assert {item["check"]: item for item in paused["checks"]}["control_not_paused"]["ok"] is False
    assert changed["production"]["dry_run_evidence_complete"] is False
    checks = {item["check"]: item for item in changed["checks"]}
    assert checks["production_gate_ready"]["deprecated_alias_for"] == "dry_run_evidence_complete"


def test_production_evidence_survives_mode_and_advisory_toggles_only() -> None:
    entry_data = {
        "ev_smart_charging_start_entity": "button.ev_start",
        "haeo_optimize_service": "haeo.optimize",
        "ai_task_entity": "ai_task.local",
    }
    options = {
        "ev_control_enabled": True,
        "planner_enabled": True,
        "dry_run": True,
        "ai_enabled": False,
        "ai_timeout_seconds": 10,
        "command_rate_limit_seconds": 60,
    }
    original = production_evidence_fingerprint(entry_data, options)
    active = production_evidence_fingerprint(
        {**entry_data, "ai_task_entity": "ai_task.replaced"},
        {
            **options,
            "planner_enabled": False,
            "dry_run": False,
            "ai_enabled": True,
            "ai_timeout_seconds": 30,
        },
    )
    changed_policy = production_evidence_fingerprint(
        entry_data, {**options, "command_rate_limit_seconds": 120}
    )

    assert active == original
    assert changed_policy != original


def test_export_support_bundle_returns_preflight_and_diagnostics() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))

    handler = hass.services.handlers[(DOMAIN, SERVICE_EXPORT_SUPPORT_BUNDLE)]
    response = asyncio.run(handler(FakeCall({})))

    assert response["preflight"]["ok"] is True
    assert response["diagnostics"]["recent_audit"][0]["action_id"] == "restore_safe_state"


def test_response_services_report_missing_config_entry() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    hass.config_entries.async_entries = lambda domain: []
    asyncio.run(async_setup(hass, {}))

    assert asyncio.run(hass.services.handlers[(DOMAIN, SERVICE_RUN_PREFLIGHT)](FakeCall({}))) == {
        "ok": False,
        "error": "no_config_entry",
    }
    assert asyncio.run(hass.services.handlers[(DOMAIN, SERVICE_EXPORT_SUPPORT_BUNDLE)](FakeCall({}))) == {
        "error": "no_config_entry"
    }


def test_pause_control_schema_validates_asset_duration_and_reason() -> None:
    coordinator = _coordinator()
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))
    schema = hass.services.schemas[(DOMAIN, SERVICE_PAUSE_CONTROL)]

    assert schema({ATTR_DURATION_MINUTES: "5", ATTR_ASSET: "enphase", ATTR_REASON: "prod_pause"}) == {
        ATTR_DURATION_MINUTES: 5,
        ATTR_ASSET: "enphase",
        ATTR_REASON: "prod_pause",
    }
    try:
        schema({ATTR_DURATION_MINUTES: 5, ATTR_ASSET: "solar"})
    except Exception as err:  # noqa: BLE001 - assert schema rejects invalid service data.
        assert "value must be one of" in str(err)
    else:
        raise AssertionError("Invalid pause asset was accepted")


def _coordinator() -> EnergyPlannerCoordinator:
    now = datetime.now(UTC)
    coordinator = EnergyPlannerCoordinator.__new__(EnergyPlannerCoordinator)
    coordinator.awaited = []
    coordinator.entry = type(
        "Entry",
        (),
        {
            "data": {
                "haeo_optimize_service": "haeo.optimize",
                "amber_import_price_entity": "sensor.import_price",
                "amber_export_price_entity": "sensor.export_price",
                "pv_forecast_entity": "sensor.pv_forecast",
                "baseline_load_forecast_entity": "sensor.baseline_load",
                "battery_soc_entity": "sensor.battery_soc",
                "enphase_profile_entity": "select.enphase_profile",
                "enphase_profile_control_service": "select.select_option",
                "enphase_ai_profile": "AI Optimisation",
                "daikin_climate_entity": "climate.daikin",
                "climate_target_low_entity": "input_number.climate_low",
                "climate_target_high_entity": "input_number.climate_high",
                "person_entities": "person.home",
                "ev_smart_charging_start_entity": "input_boolean.ev_start",
                "ev_smart_charging_stop_entity": "input_boolean.ev_stop",
                "ai_advisor_service": "fake_ai.advice",
            },
            "options": {
                "ev_control_enabled": True,
                "climate_control_enabled": True,
                "enphase_control_enabled": True,
            },
        },
    )()
    coordinator.entry.runtime_data = coordinator
    coordinator.data = EnergyPlan(
        plan_id="current-plan",
        created_at=now,
        horizon_hours=12,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="healthy dry-run",
        confidence=0.9,
        estimated_daily_cost=2.0,
        estimated_cost_horizon_hours=12.0,
        actions=[],
        preview=[],
    )
    coordinator.store = type(
        "Store",
        (),
        {
            "data": {
                "execution_audit": [
                    {
                        "attempted_at": "2026-06-27T00:00:00+00:00",
                        "plan_id": "manual",
                        "action_id": "restore_safe_state",
                        "result": "restored",
                        "reason": "manual_service_call",
                        "service_target": "restore_safe_state",
                    }
                ],
                "production": {
                    "armed": True,
                    "dry_run_ready_cycles": 3,
                    "acknowledged_at": "2026-06-27T00:00:00+00:00",
                },
                "control_pause": {},
            }
        },
    )()
    coordinator.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
        coordinator.entry.data,
        coordinator.options,
    )
    coordinator.last_refresh_metadata = {
        "succeeded": True,
        "completed_at": now,
        "duration_ms": 10.0,
    }

    async def replan() -> None:
        coordinator.awaited.append(("replan", None))

    async def restore(reason: str) -> None:
        coordinator.awaited.append(("restore", reason))

    async def ready_by(value: str) -> None:
        coordinator.awaited.append(("ready_by", value))

    async def manual_override(duration: int, reason: str) -> None:
        coordinator.awaited.append(("manual_override", (duration, reason)))

    async def arm(reason: str) -> None:
        coordinator.awaited.append(("arm", reason))

    async def disarm(reason: str) -> None:
        coordinator.awaited.append(("disarm", reason))

    async def pause(duration: int, reason: str, asset: str) -> None:
        coordinator.awaited.append(("pause", (duration, reason, asset)))

    async def resume(reason: str) -> None:
        coordinator.awaited.append(("resume", reason))

    coordinator.async_request_replan = replan
    coordinator.async_restore_safe_state = restore
    coordinator.async_set_ready_by = ready_by
    coordinator.async_set_manual_hvac_override = manual_override
    coordinator.async_arm_production_control = arm
    coordinator.async_disarm_production_control = disarm
    coordinator.async_pause_control = pause
    coordinator.async_resume_control = resume
    return coordinator


def _partial_coordinator(data: dict[str, Any], **options: Any) -> EnergyPlannerCoordinator:
    """Return an armed coordinator with only the requested control surfaces."""
    coordinator = _coordinator()
    coordinator.entry.data = dict(data)
    coordinator.entry.options = {
        "ev_control_enabled": False,
        "climate_control_enabled": False,
        "enphase_control_enabled": False,
        **options,
    }
    coordinator.store.data["production"]["dry_run_evidence_fingerprint"] = production_evidence_fingerprint(
        coordinator.entry.data,
        coordinator.options,
    )
    return coordinator


def _run_preflight(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Register and invoke the preflight service for a coordinator."""
    hass = FakeHass(coordinator)
    asyncio.run(async_setup(hass, {}))
    handler = hass.services.handlers[(DOMAIN, SERVICE_RUN_PREFLIGHT)]
    return asyncio.run(handler(FakeCall({})))
