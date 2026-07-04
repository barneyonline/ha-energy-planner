"""Non-commanding capability discovery for Energy Planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_HAEO_OPTIMIZE_SERVICE,
    DEFAULT_HAEO_OPTIMIZE_SERVICE,
)
from .models import ActionAsset


@dataclass(slots=True)
class CapabilityEvidence:
    """Capability evidence for one integration surface."""

    supported: bool
    issues: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryReport:
    """Non-commanding discovery report."""

    haeo: CapabilityEvidence
    ev: CapabilityEvidence
    hvac: CapabilityEvidence
    enphase: CapabilityEvidence
    ai: CapabilityEvidence

    def for_asset(self, asset: ActionAsset) -> CapabilityEvidence:
        """Return capability evidence for a controllable asset."""
        if asset == ActionAsset.EV:
            return self.ev
        if asset == ActionAsset.DAIKIN:
            return self.hvac
        if asset == ActionAsset.ENPHASE:
            return self.enphase
        return CapabilityEvidence(False, ["unknown_asset"])

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-friendly report."""
        return {
            "haeo": _evidence_dict(self.haeo),
            "ev": _evidence_dict(self.ev),
            "hvac": _evidence_dict(self.hvac),
            "enphase": _evidence_dict(self.enphase),
            "ai": _evidence_dict(self.ai),
        }


class CapabilityDiscovery:
    """Inspect Home Assistant entities/services without issuing device commands."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        """Initialize discovery."""
        self.hass = hass
        self.entry_data = entry_data

    def inspect(self) -> DiscoveryReport:
        """Return current capability evidence."""
        return DiscoveryReport(
            haeo=self._inspect_haeo(),
            ev=self._inspect_ev(),
            hvac=self._inspect_hvac(),
            enphase=self._inspect_enphase(),
            ai=self._inspect_ai(),
        )

    def _inspect_haeo(self) -> CapabilityEvidence:
        service = self.entry_data.get(CONF_HAEO_OPTIMIZE_SERVICE) or DEFAULT_HAEO_OPTIMIZE_SERVICE
        return _service_evidence(self.hass, service, "haeo_service")

    def _inspect_ev(self) -> CapabilityEvidence:
        issues: list[str] = []
        details: dict[str, Any] = {}
        control = self.entry_data.get(CONF_EV_SMART_CHARGING_START) or self.entry_data.get(CONF_EV_SMART_CHARGING)
        stop = self.entry_data.get(CONF_EV_SMART_CHARGING_STOP) or self.entry_data.get(CONF_EV_SMART_CHARGING)
        if not control:
            issues.append("ev_start_control_not_configured")
        elif _state_missing(self.hass, control):
            issues.append("ev_start_control_unavailable")
        if not stop:
            issues.append("ev_stop_control_not_configured")
        elif _state_missing(self.hass, stop):
            issues.append("ev_stop_control_unavailable")
        for key in (CONF_EV_SMART_CHARGING_TARGET_SOC, CONF_EV_SMART_CHARGING_READY_BY):
            entity_id = self.entry_data.get(key)
            if entity_id:
                details[key] = {"entity_id": entity_id, "available": not _state_missing(self.hass, entity_id)}
        details["start_control"] = control
        details["stop_control"] = stop
        return CapabilityEvidence(not issues, issues, details)

    def _inspect_hvac(self) -> CapabilityEvidence:
        issues: list[str] = []
        climate = self.entry_data.get(CONF_DAIKIN_CLIMATE)
        if not climate:
            issues.append("daikin_climate_not_configured")
        elif _state_missing(self.hass, climate):
            issues.append("daikin_climate_unavailable")
        automations = _split_entity_values(self.entry_data.get(CONF_CLIMATE_AUTOMATIONS, ""))
        unavailable = [entity_id for entity_id in automations if _state_missing(self.hass, entity_id)]
        if unavailable:
            issues.append("climate_automation_unavailable")
        return CapabilityEvidence(
            not issues,
            issues,
            {
                "climate_entity": climate,
                "automation_entities": automations,
                "unavailable_automations": unavailable,
            },
        )

    def _inspect_enphase(self) -> CapabilityEvidence:
        issues: list[str] = []
        profile = self.entry_data.get(CONF_ENPHASE_PROFILE)
        service = _profile_control_service(profile)
        ai_profile = self.entry_data.get(CONF_ENPHASE_AI_PROFILE)
        self_consumption_profile = self.entry_data.get(CONF_ENPHASE_SELF_CONSUMPTION_PROFILE)
        full_backup_profile = self.entry_data.get(CONF_ENPHASE_FULL_BACKUP_PROFILE)
        if not profile:
            issues.append("enphase_profile_entity_not_configured")
        elif _state_missing(self.hass, profile):
            issues.append("enphase_profile_entity_unavailable")
        service_evidence = _service_evidence(self.hass, service, "enphase_profile_control")
        issues.extend(service_evidence.issues)
        if not ai_profile:
            issues.append("enphase_ai_profile_not_configured")
        return CapabilityEvidence(
            not issues,
            issues,
            {
                "profile_entity": profile,
                "control_service": service,
                "ai_profile_configured": bool(ai_profile),
                "self_consumption_profile_configured": bool(self_consumption_profile),
                "full_backup_profile_configured": bool(full_backup_profile),
            },
        )

    def _inspect_ai(self) -> CapabilityEvidence:
        service = self.entry_data.get(CONF_AI_ADVISOR_SERVICE)
        if not service:
            return CapabilityEvidence(False, ["ai_service_not_configured"], {})
        return _service_evidence(self.hass, service, "ai_service")


def _service_evidence(hass: HomeAssistant, service_name: str | None, label: str) -> CapabilityEvidence:
    if not service_name:
        return CapabilityEvidence(False, [f"{label}_not_configured"], {})
    if "." not in str(service_name):
        return CapabilityEvidence(False, [f"{label}_invalid"], {"service": service_name})
    domain, service = str(service_name).split(".", 1)
    has_service = getattr(hass.services, "has_service", None)
    available = True
    if callable(has_service):
        available = bool(has_service(domain, service))
    return CapabilityEvidence(
        available,
        [] if available else [f"{label}_unavailable"],
        {"service": service_name, "domain": domain, "service_name": service},
    )


def _profile_control_service(profile_entity: str | None) -> str | None:
    """Return the standard service for a profile selector entity."""
    if not profile_entity or "." not in str(profile_entity):
        return None
    domain = str(profile_entity).split(".", 1)[0]
    if domain in {"select", "input_select"}:
        return f"{domain}.select_option"
    return None


def _state_missing(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    if state is None:
        return True
    domain = entity_id.split(".", 1)[0]
    if domain in {"button", "input_button"}:
        return state.state == "unavailable"
    return state.state in {"unknown", "unavailable"}


def _split_entity_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _evidence_dict(evidence: CapabilityEvidence) -> dict[str, Any]:
    return {
        "supported": evidence.supported,
        "issues": evidence.issues,
        "details": evidence.details,
    }
