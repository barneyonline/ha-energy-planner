"""Tests for non-commanding capability discovery."""

from __future__ import annotations

from dataclasses import dataclass

from custom_components.ha_energy_planner.const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_HAEO_OPTIMIZE_SERVICE,
)
from custom_components.ha_energy_planner.discovery import (
    CapabilityDiscovery,
    _profile_control_service,
    _service_evidence,
)
from custom_components.ha_energy_planner.models import ActionAsset


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str


class FakeStates:
    """Minimal states registry."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeServices:
    """Minimal service registry."""

    def __init__(self, services: set[tuple[str, str]]) -> None:
        self.services = services

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.services


class FakeHass:
    """Minimal HA object."""

    def __init__(self, states: dict[str, str], services: set[tuple[str, str]]) -> None:
        self.states = FakeStates(states)
        self.services = FakeServices(services)


def test_discovery_reports_supported_controls() -> None:
    hass = FakeHass(
        {
            "switch.ev_start": "off",
            "switch.ev_stop": "on",
            "climate.daikin": "heat",
            "automation.climate": "on",
            "ai_task.extended_openai": "ready",
            "select.enphase_profile": "AI Optimisation",
        },
        {("haeo", "optimize"), ("select", "select_option"), ("ai_task", "generate_data")},
    )
    report = CapabilityDiscovery(
        hass,
        {
            CONF_AI_TASK_ENTITY: "ai_task.extended_openai",
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "switch.ev_stop",
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
            CONF_ENPHASE_PROFILE: "select.enphase_profile",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    ).inspect()
    assert report.haeo.supported is True
    assert report.ev.supported is True
    assert report.hvac.supported is True
    assert report.enphase.supported is True
    assert report.ai.supported is True


def test_discovery_treats_unknown_button_controls_as_available() -> None:
    hass = FakeHass(
        {
            "button.ev_start": "unknown",
            "button.ev_stop": "unknown",
        },
        set(),
    )

    report = CapabilityDiscovery(
        hass,
        {
            CONF_EV_SMART_CHARGING_START: "button.ev_start",
            CONF_EV_SMART_CHARGING_STOP: "button.ev_stop",
        },
    ).inspect()

    assert report.ev.supported is True
    assert report.ev.issues == []


def test_discovery_reports_missing_climate_automation() -> None:
    hass = FakeHass({"climate.daikin": "heat"}, set())
    report = CapabilityDiscovery(
        hass,
        {
            CONF_DAIKIN_CLIMATE: "climate.daikin",
            CONF_CLIMATE_AUTOMATIONS: "automation.climate",
        },
    ).inspect()
    assert report.hvac.supported is False
    assert "climate_automation_unavailable" in report.hvac.issues


def test_discovery_reports_unavailable_service() -> None:
    hass = FakeHass({}, set())
    report = CapabilityDiscovery(hass, {CONF_HAEO_OPTIMIZE_SERVICE: "haeo.optimize"}).inspect()
    assert report.haeo.supported is False
    assert report.haeo.issues == ["haeo_service_unavailable"]


def test_discovery_reports_missing_and_invalid_surfaces() -> None:
    hass = FakeHass(
        {
            "switch.ev_start": "unavailable",
            "input_number.target": "80",
        },
        set(),
    )
    report = CapabilityDiscovery(
        hass,
        {
            CONF_EV_SMART_CHARGING_START: "switch.ev_start",
            CONF_EV_SMART_CHARGING_TARGET_SOC: "input_number.target",
            CONF_EV_SMART_CHARGING_READY_BY: "input_text.ready_by",
            CONF_DAIKIN_CLIMATE: "climate.missing",
            CONF_ENPHASE_PROFILE: "sensor.profile",
            CONF_AI_TASK_ENTITY: "ai_task.missing",
        },
    ).inspect()

    assert report.ev.supported is False
    assert "ev_start_control_unavailable" in report.ev.issues
    assert "ev_stop_control_not_configured" in report.ev.issues
    assert report.ev.details[CONF_EV_SMART_CHARGING_TARGET_SOC]["available"] is True
    assert report.ev.details[CONF_EV_SMART_CHARGING_READY_BY]["available"] is False
    assert report.hvac.issues == ["daikin_climate_unavailable"]
    assert "enphase_profile_control_not_configured" in report.enphase.issues
    assert "enphase_ai_profile_not_configured" in report.enphase.issues
    assert report.ai.issues == ["ai_service_unavailable", "ai_task_entity_unavailable"]
    assert report.for_asset(ActionAsset.EV) is report.ev
    assert report.as_dict()["ev"]["supported"] is False


def test_discovery_requires_ai_task_entity_for_ai_task_service() -> None:
    hass = FakeHass({}, {("ai_task", "generate_data")})

    report = CapabilityDiscovery(hass, {CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data"}).inspect()

    assert report.ai.supported is False
    assert report.ai.issues == ["ai_task_entity_not_configured"]


def test_discovery_low_level_service_and_profile_helpers() -> None:
    no_has_service = type("Hass", (), {"services": object(), "states": FakeStates({})})()

    assert _service_evidence(no_has_service, None, "test").issues == ["test_not_configured"]
    assert _service_evidence(no_has_service, "bad", "test").issues == ["test_invalid"]
    assert _service_evidence(no_has_service, "domain.service", "test").supported is True
    assert _profile_control_service(None) is None
    assert _profile_control_service("sensor.profile") is None
