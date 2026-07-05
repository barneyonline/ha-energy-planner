"""Tests for integration-level service handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    assert checks["production_gate_ready"]["ok"] is False
    assert "1/3 healthy dry-run cycles" in checks["production_gate_ready"]["message"]
    assert "ev" in checks["production_gate_ready"]["message"]
    assert checks["production_control_armed"]["ok"] is False
    assert "has not been armed" in checks["production_control_armed"]["message"]


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
    coordinator.data = None
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
