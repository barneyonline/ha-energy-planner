"""Non-commanding capability discovery for Energy Planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry

from .adapter_helpers import (
    available_state,
    climate_target_snapshot,
    climate_targets_absent,
    has_finite_temperature_target,
    zone_temperature_sync_deferred,
)
from .climate_inputs import climate_storage_value
from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_TASK_ENTITY,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CHANGE_FROM_SCHEDULER,
    CONF_CLIMATE_MANUAL_OVERRIDE,
    CONF_CLIMATE_SCHEDULER_GUARD_TIMER,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_FULL_BACKUP_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
    CONF_ENPHASE_SELF_CONSUMPTION_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY,
    DEFAULT_OPTIONS,
    HVAC_DRY_ZONE_TARGETS_ABSENT,
)
from .ev_policy import power_capability
from .models import ActionAsset
from .planner_values import finite_float
from .zone_control import read_topology, shares_registered_controller


def synchronized_zone_targets_required(
    hass: HomeAssistant, entry_data: dict[str, Any], options: dict[str, Any],
    desired_state: dict[str, Any] | None = None,
) -> bool:
    """Recognize synchronization configured directly or through independent zones."""
    return options.get(CONF_HVAC_PRECONDITION_CONFIGURED_ZONES_ONLY) is True or (
        desired_state is not None and desired_state.get("configured_zones_only") is True
    ) or read_topology(hass, entry_data)["supported"] is True


@dataclass(slots=True)
class CapabilityEvidence:
    """Capability evidence for one integration surface."""

    supported: bool
    issues: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryReport:
    """Non-commanding discovery report."""

    ev: CapabilityEvidence
    hvac: CapabilityEvidence
    enphase: CapabilityEvidence
    ai: CapabilityEvidence
    enphase_export_limit: CapabilityEvidence = field(default_factory=lambda: CapabilityEvidence(False))

    def for_asset(self, asset: ActionAsset) -> CapabilityEvidence:
        """Return capability evidence for a controllable asset."""
        if asset == ActionAsset.EV:
            return self.ev
        if asset == ActionAsset.DAIKIN:
            return self.hvac
        if asset == ActionAsset.ENPHASE:
            return self.enphase
        if asset == ActionAsset.ENPHASE_EXPORT_LIMIT:
            return self.enphase_export_limit
        return CapabilityEvidence(False, ["unknown_asset"])

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-friendly report."""
        return {
            "ev": _evidence_dict(self.ev),
            "hvac": _evidence_dict(self.hvac),
            "enphase": _evidence_dict(self.enphase),
            "ai": _evidence_dict(self.ai),
            "enphase_export_limit": _evidence_dict(self.enphase_export_limit),
        }


class CapabilityDiscovery:
    """Inspect Home Assistant entities/services without issuing device commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        options: dict[str, Any] | None = None,
        *, hvac_desired_state: dict[str, Any] | None = None,
    ) -> None:
        """Initialize discovery."""
        self.hass = hass
        self.entry_data = entry_data
        self.options = {**DEFAULT_OPTIONS, **(options or {})}
        self._synchronize_zone_targets = synchronized_zone_targets_required(
            hass, entry_data, self.options, hvac_desired_state,
        )

    def inspect(self, *, hvac: CapabilityEvidence | None = None) -> DiscoveryReport:
        """Return current capability evidence."""
        return DiscoveryReport(
            ev=self._inspect_ev(),
            hvac=hvac if hvac is not None else self._inspect_hvac(),
            enphase=self._inspect_enphase(),
            ai=self._inspect_ai(),
            enphase_export_limit=self._inspect_export_limit(),
        )

    def _inspect_ev(self) -> CapabilityEvidence:
        issues: list[str] = []
        details: dict[str, Any] = {}
        control = self.entry_data.get(CONF_EV_CHARGER) or self.entry_data.get(CONF_EV_SMART_CHARGING)
        persistent_control = stop = control
        if not control:
            issues.extend(["ev_start_control_not_configured", "ev_stop_control_not_configured"])
        elif str(control).split(".", 1)[0] not in {"switch", "input_boolean"}:
            issues.extend(["ev_start_control_unsupported", "ev_stop_control_unsupported"])
        elif _state_missing(self.hass, control):
            issues.extend(["ev_start_control_unavailable", "ev_stop_control_unavailable"])
        details["start_control"] = control
        details["stop_control"] = stop
        details["persistent_control"] = {
            "entity_id": persistent_control,
            "available": bool(
                persistent_control
                and not _state_missing(self.hass, persistent_control)
            ),
            "stateful": bool(
                persistent_control
                and str(persistent_control).split(".", 1)[0]
                in {"switch", "input_boolean"}
            ),
        }
        details["controller"] = "ha_energy_planner"
        limit_entity = self.entry_data.get("ev_power_limit_entity")
        if limit_entity:
            capability = power_capability(self.hass.states.get(limit_entity), self.options)
            details["power_limit"] = {"entity_id": limit_entity, "available": capability is not None}
            if capability is None:
                issues.append("ev_power_control_unavailable")
            power_entity = self.entry_data.get("ev_power_entity")
            if not power_entity or _state_missing(self.hass, power_entity):
                issues.append("ev_power_feedback_unavailable")

        target_soc_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_TARGET_SOC)
        if target_soc_entity:
            details[CONF_EV_SMART_CHARGING_TARGET_SOC] = {
                "entity_id": target_soc_entity,
                "available": not _state_missing(self.hass, target_soc_entity),
            }
        ready_by_entity = self.entry_data.get(CONF_EV_SMART_CHARGING_READY_BY)
        if ready_by_entity:
            details[CONF_EV_SMART_CHARGING_READY_BY] = {
                "entity_id": ready_by_entity,
                "available": not _state_missing(self.hass, ready_by_entity),
            }
        return CapabilityEvidence(not issues, issues, details)

    def dry_takeover_issues(self) -> list[str]:
        """Recheck a Dry takeover block and retain any concurrent HVAC faults."""
        evidence = self.hvac_evidence_in_dry()
        return evidence.issues if evidence is not None and HVAC_DRY_ZONE_TARGETS_ABSENT in evidence.issues else []

    def synchronized_takeover_issues(self) -> list[str]:
        """Revalidate configured inputs before the first synchronized takeover write."""
        if not self._synchronize_zone_targets:
            return []
        evidence = self._inspect_hvac()
        if HVAC_DRY_ZONE_TARGETS_ABSENT in evidence.issues:
            return evidence.issues
        issues = list(evidence.issues)
        head = available_state(self.hass, self.entry_data.get(CONF_DAIKIN_CLIMATE))
        # Native Off starts can capture the head target from mode memory.
        if head is not None and head.state == "off" and "main_climate_target_unavailable" in issues:
            issues.remove("main_climate_target_unavailable")
        return issues

    def hvac_evidence_in_dry(self) -> CapabilityEvidence | None:
        """Inspect HVAC once when the head is in Dry, including genuine faults."""
        head = available_state(self.hass, self.entry_data.get(CONF_DAIKIN_CLIMATE))
        return self._inspect_hvac() if head is not None and head.state == "dry" else None

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
        zones = _split_entity_values(self.entry_data.get(CONF_CLIMATE_ZONES, ""))
        unavailable_zones = [entity_id for entity_id in zones if _state_missing(self.hass, entity_id)]
        if unavailable_zones:
            issues.append("climate_zone_unavailable")
        synchronize_targets = self._synchronize_zone_targets
        main_target_unavailable: list[str] = []
        zone_targets_unavailable: list[str] = []
        zone_targets_deferred: list[str] = []
        zone_targets_absent_in_dry_mode: list[str] = []
        climate_state = self.hass.states.get(climate) if climate and not _state_missing(self.hass, climate) else None
        if climate_state is not None and not _has_main_temperature_target(climate_state, synchronize_targets):
            main_target_unavailable.append(str(climate))
            issues.append("main_climate_target_unavailable")
        if synchronize_targets:
            for entity_id in zones:
                if not entity_id.startswith("climate.") or _state_missing(self.hass, entity_id):
                    continue
                zone_state = self.hass.states.get(entity_id)
                if zone_temperature_sync_deferred(zone_state):
                    zone_targets_deferred.append(entity_id)
                elif (
                    climate_state is not None
                    and zone_state is not None
                    and climate_state.state == zone_state.state == "dry"
                    and climate_targets_absent(zone_state)
                    and type(zone_state.attributes.get("zone_id")) is int
                    and _same_daikin_controller(self.hass, str(climate), entity_id)
                ):
                    zone_targets_absent_in_dry_mode.append(entity_id)
                elif not has_finite_temperature_target(climate_target_snapshot(zone_state)):
                    zone_targets_unavailable.append(entity_id)
            if zone_targets_unavailable:
                issues.append("climate_zone_target_unavailable")
            if zone_targets_absent_in_dry_mode:
                issues.append(HVAC_DRY_ZONE_TARGETS_ABSENT)
        manual_override = self.entry_data.get(CONF_CLIMATE_MANUAL_OVERRIDE)
        if manual_override and _state_missing(self.hass, manual_override):
            issues.append("climate_manual_override_unavailable")
        scheduler_guard = self.entry_data.get(CONF_CLIMATE_CHANGE_FROM_SCHEDULER)
        scheduler_timer = self.entry_data.get(CONF_CLIMATE_SCHEDULER_GUARD_TIMER)
        if bool(scheduler_guard) != bool(scheduler_timer):
            issues.append("climate_scheduler_guard_incomplete")
        elif scheduler_guard and (
            _state_missing(self.hass, str(scheduler_guard)) or _state_missing(self.hass, str(scheduler_timer))
        ):
            issues.append("climate_scheduler_guard_unavailable")
        return CapabilityEvidence(
            not issues,
            issues,
            {
                "climate_entity": climate,
                "automation_entities": automations,
                "unavailable_automations": unavailable,
                "zone_entities": zones,
                "unavailable_zones": unavailable_zones,
                "synchronize_zone_temperatures": synchronize_targets,
                "main_target_unavailable": main_target_unavailable,
                "zone_targets_unavailable": zone_targets_unavailable,
                "zone_targets_deferred": zone_targets_deferred,
                "zone_targets_absent_in_dry_mode": zone_targets_absent_in_dry_mode,
                "manual_override_entity": manual_override,
                "scheduler_guard_entity": scheduler_guard,
                "scheduler_guard_timer_entity": scheduler_timer,
            },
        )

    def _inspect_enphase(self) -> CapabilityEvidence:
        issues: list[str] = []
        profile = self.entry_data.get(CONF_ENPHASE_PROFILE)
        service = self.entry_data.get(CONF_ENPHASE_PROFILE_CONTROL_SERVICE) or _profile_control_service(
            profile
        )
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

    def _inspect_export_limit(self) -> CapabilityEvidence:
        from homeassistant.util import dt as dt_util

        from .const import CONF_ENPHASE_EXPORT_LIMIT_ENTITY
        from .enphase_export_limit import feedback

        if not self.entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY):
            return CapabilityEvidence(False, ["export_limit_not_configured"])
        observed, issue = feedback(self.hass, self.entry_data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), dt_util.utcnow())
        issues = [issue] if issue else []
        for service in ("set_export_limit", "disable_export_limit"):
            issues.extend(_service_evidence(self.hass, f"enphase_ev.{service}", "export_limit_service").issues)
        return CapabilityEvidence(not issues, issues, observed)

    def _inspect_ai(self) -> CapabilityEvidence:
        task_entity = str(self.entry_data.get(CONF_AI_TASK_ENTITY, "") or "").strip()
        if task_entity:
            service_evidence = _service_evidence(self.hass, "ai_task.generate_data", "ai_service")
            issues = list(service_evidence.issues)
            if _state_missing(self.hass, task_entity):
                issues.append("ai_task_entity_unavailable")
            return CapabilityEvidence(
                not issues,
                issues,
                {
                    **service_evidence.details,
                    "ai_task_entity": task_entity,
                    "configured": True,
                    "available": not issues,
                    "reason": issues[0] if issues else None,
                },
            )

        service = self.entry_data.get(CONF_AI_ADVISOR_SERVICE)
        if service == "ai_task.generate_data":
            return CapabilityEvidence(
                False,
                ["ai_task_entity_not_configured"],
                {
                    "service": service,
                    "configured": False,
                    "available": False,
                    "reason": "ai_task_entity_not_configured",
                },
            )
        return CapabilityEvidence(
            False,
            ["ai_task_entity_not_configured"],
            {
                "configured": False,
                "available": False,
                "reason": "ai_task_entity_not_configured",
            },
        )


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
    states = getattr(hass, "states", None)
    get_state = getattr(states, "get", None)
    if not callable(get_state):
        return True
    state = get_state(entity_id)
    if state is None:
        return True
    domain = entity_id.split(".", 1)[0]
    # AI Tasks and momentary controls can remain unknown until first use.
    if domain in {"ai_task", "button", "input_button"}:
        return bool(state.state == "unavailable")
    return bool(state.state in {"unknown", "unavailable"})


def _same_daikin_controller(hass: HomeAssistant, head_entity_id: str, zone_entity_id: str) -> bool:
    """Verify native Daikin registry ownership before classifying a Dry zone."""
    if head_entity_id == zone_entity_id:
        return False
    registry = entity_registry.async_get(hass)
    head = registry.async_get(head_entity_id)
    return (
        head is not None
        and head.platform == "daikin"
        and shares_registered_controller(head, registry.async_get(zone_entity_id))
    )


def _has_main_temperature_target(state: State, synchronize_targets: bool) -> bool:
    """Validate the synchronized rollback shape or the legacy head target."""
    if synchronize_targets:
        return has_finite_temperature_target(climate_target_snapshot(state))
    attributes = state.attributes
    return finite_float(climate_storage_value(attributes.get("temperature"))) is not None or (
        finite_float(climate_storage_value(attributes.get("target_temp_low"))) is not None
        and finite_float(climate_storage_value(attributes.get("target_temp_high"))) is not None
    )


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
