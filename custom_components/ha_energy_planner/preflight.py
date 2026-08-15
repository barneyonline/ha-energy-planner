"""Non-commanding active-mode preflight checks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_BYPASS_SAFETY_GATES,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_CLIMATE_ZONES,
    CONF_DAIKIN_CLIMATE,
    CONF_DEFAULT_READY_BY,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_ENPHASE_PROFILE,
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_HOUSEHOLD_LOAD,
    CONF_PERSON_ENTITIES,
    CONF_PLANNER_ENABLED,
)
from .discovery import CapabilityDiscovery
from .entry_data import combined_entry_data
from .load_forecast import FORECAST_CONTRACT_VERSION
from .planner import confidence_eligible_control_areas
from .safety import (
    DRY_RUN_READY_CYCLES_REQUIRED,
    parse_production_state,
    partition_control_areas_by_pause,
    strict_bool,
)

_SERVICE_KEYS = (CONF_AI_ADVISOR_SERVICE,)
_EVIDENCE_OPTION_EXCLUSIONS = {
    CONF_AI_ADVISOR_SERVICE,
    CONF_DEFAULT_READY_BY,
    "ai_enabled",
    "ai_timeout_seconds",
    CONF_DRY_RUN,
    CONF_PLANNER_ENABLED,
    CONF_EV_CONTROL_ENABLED,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    "ev_connected_helper",
}
_EVIDENCE_ENTRY_EXCLUSIONS = {
    CONF_AI_ADVISOR_SERVICE,
    "ai_task_entity",
    "haeo_config_entry_id",
    "haeo_entry_id",
    "haeo_optimize_service",
}


def build_preflight_report(
    hass: HomeAssistant,
    coordinator: Any,
    *,
    options_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a redacted readiness report without calling device services."""
    entry_data = combined_entry_data(coordinator.entry)
    options = coordinator.options if options_override is None else {**coordinator.options, **options_override}
    bypass_safety_gates = strict_bool(options.get(CONF_BYPASS_SAFETY_GATES), default=False)
    now = dt_util.utcnow()
    control_areas, discovery = _runtime_control_area_report(
        hass,
        entry_data,
        options,
        plan=getattr(coordinator, "data", None),
        pause=coordinator.store.data.get("control_pause"),
        now=now,
    )
    entity_report = _entity_report(hass, entry_data, required_areas=control_areas["required"])
    service_report = _service_report(hass, entry_data, required_areas=control_areas["required"])
    recorder = _recorder_report(hass)
    recorder_required = bool(entry_data.get(CONF_HOUSEHOLD_LOAD))
    safety = _safety_report(options)
    evidence_fingerprint = production_evidence_fingerprint(entry_data, options)
    production = _production_report(
        coordinator.store.data,
        options,
        control_areas,
        expected_evidence_fingerprint=evidence_fingerprint,
    )
    current_plan = _current_plan_report(
        getattr(coordinator, "data", None),
        now=now,
        last_refresh_metadata=getattr(coordinator, "last_refresh_metadata", None),
    )
    audit = _audit_report(coordinator.store.data)

    ready_control_areas = list(control_areas.get("ready", []))
    unpaused_control_areas = list(control_areas.get("available", []))
    confidence_eligible_areas = list(control_areas.get("confidence_eligible", []))
    control_paused = bool(ready_control_areas and not unpaused_control_areas)
    control_area_blocking = bool(control_areas["required"] and not ready_control_areas)
    blocking = [
        *(["control_areas"] if control_area_blocking else []),
        *(["recorder"] if recorder_required and not recorder["available"] else []),
    ]
    checks = [
        {
            "check": "safe_first_run_mode",
            "ok": safety["safe_first_run_mode"],
            "blocking": False,
            "message": (
                "Planner is disabled and dry-run is enabled."
                if safety["safe_first_run_mode"]
                else "Planner is not in the default first-run safe mode."
            ),
        },
        {
            "check": "configured_entities_available",
            "ok": not entity_report["missing"] and not entity_report["unavailable"],
            "blocking": control_area_blocking,
            "message": _availability_message(
                "All configured entities are present and available.",
                missing=entity_report["missing"],
                unavailable=entity_report["unavailable"],
            ),
        },
        {
            "check": "configured_services_available",
            "ok": not service_report["missing"] and not service_report["unavailable"],
            "blocking": control_area_blocking,
            "message": _availability_message(
                "All configured services are registered.",
                missing=service_report["missing"],
                unavailable=service_report["unavailable"],
            ),
        },
        {
            "check": "required_control_areas_supported",
            "ok": not control_areas["required"] or bool(ready_control_areas),
            "blocking": True,
            "message": _control_area_message(control_areas, discovery),
        },
        {
            "check": "recorder_available",
            "ok": recorder["available"],
            "blocking": recorder_required,
            "message": "Recorder is available for history imports."
            if recorder["available"]
            else "Recorder is not detected.",
        },
        {
            "check": "dry_run_evidence_complete",
            "ok": production["dry_run_evidence_complete"],
            "blocking": False,
            "message": _production_gate_message(production),
        },
        {
            "check": "production_gate_ready",
            "ok": production["dry_run_evidence_complete"],
            "blocking": False,
            "deprecated_alias_for": "dry_run_evidence_complete",
            "message": _production_gate_message(production),
        },
        {
            "check": "control_not_paused",
            "ok": not control_paused,
            "blocking": True,
            "message": (
                "At least one ready control area is not paused."
                if not control_paused
                else "Every ready control area is paused."
            ),
        },
        {
            "check": "current_plan_safe",
            "ok": current_plan["safe"],
            "blocking": True,
            "message": current_plan["message"],
        },
        {
            "check": "control_area_confidence_eligible",
            "ok": bool(confidence_eligible_areas),
            "blocking": True,
            "message": (
                f"Confidence-eligible control areas: {_bounded_join(confidence_eligible_areas)}."
                if confidence_eligible_areas
                else "No ready, unpaused control area clears its configured confidence thresholds."
            ),
        },
        {
            "check": "production_control_armed",
            "ok": production["armed"],
            "blocking": False,
            "message": (
                "Production control is armed."
                if production["armed"]
                else (
                    "Production control has not been armed. Review preflight, then use Arm production control "
                    "when ready."
                )
            ),
        },
    ]
    if bypass_safety_gates:
        for check in checks:
            check["bypassed"] = not bool(check["ok"])
            check["blocking"] = False
    safe_to_activate_now = bypass_safety_gates or (
        not blocking
        and bool(unpaused_control_areas)
        and production["dry_run_evidence_complete"]
        and current_plan["safe"]
        and bool(confidence_eligible_areas)
        and production["device_controls_enabled"]
        and not control_paused
    )
    production["safe_to_activate_now"] = safe_to_activate_now
    production["safety_gates_bypassed"] = bypass_safety_gates
    active_control_ready = safe_to_activate_now and production["armed"]
    return {
        "ok": active_control_ready,
        "active_control_ready": active_control_ready,
        "safe_to_activate_now": safe_to_activate_now,
        "safety_gates_bypassed": bypass_safety_gates,
        "current_plan": current_plan,
        "mode": safety,
        "production": production,
        "checks": checks,
        "entities": entity_report,
        "services": service_report,
        "recorder": recorder,
        "control_areas": control_areas,
        "discovery": discovery,
        "audit": audit,
    }


def _apply_ev_keep_on_preflight(
    discovery: dict[str, Any],
    entry_data: dict[str, Any],
    options: dict[str, Any],
) -> None:
    """Block production readiness when keep-on lacks persistent control."""
    if not strict_bool(options.get(CONF_EV_KEEP_CHARGER_ON), default=False):
        return
    entity_id = entry_data.get(CONF_EV_CHARGER) or entry_data.get(CONF_EV_SMART_CHARGING)
    ev_discovery = discovery["ev"]
    persistent_control = ev_discovery.get("details", {}).get(
        "persistent_control", {}
    )
    if (
        entity_id
        and str(entity_id).split(".", 1)[0] in {"switch", "input_boolean"}
        and persistent_control.get("available") is True
    ):
        return
    issues = list(ev_discovery.get("issues", []))
    issue = (
        "ev_keep_on_control_unavailable"
        if persistent_control.get("stateful") is True
        else "ev_keep_on_requires_stateful_control"
    )
    if issue not in issues:
        issues.append(issue)
    ev_discovery["issues"] = issues
    ev_discovery["supported"] = False


def _availability_message(success_message: str, *, missing: list[str], unavailable: list[str]) -> str:
    """Return a concise availability check message."""
    details = []
    if missing:
        details.append(f"missing: {_bounded_join(missing)}")
    if unavailable:
        details.append(f"unavailable: {_bounded_join(unavailable)}")
    if not details:
        return success_message
    return f"Configured references are not ready; {'; '.join(details)}."


def _production_gate_message(production: dict[str, Any]) -> str:
    """Return a concise production gate readiness message."""
    if production.get("dry_run_evidence_complete", production.get("ready_to_arm", False)) is True:
        return "Production gate has enough dry-run evidence and the configured control areas are explicitly enabled."

    details: list[str] = []
    dry_run_ready_cycles = parse_production_state(production).dry_run_ready_cycles
    if dry_run_ready_cycles < DRY_RUN_READY_CYCLES_REQUIRED:
        details.append(f"{dry_run_ready_cycles}/{DRY_RUN_READY_CYCLES_REQUIRED} healthy dry-run cycles recorded")
    required_areas = list(production.get("required_control_areas", []))
    if "required_control_areas" in production and not required_areas:
        details.append("no configured control areas are enabled")
    if not details:
        return "Production gate is not ready to arm yet."
    return f"Production gate is not ready to arm yet; {'; '.join(details)}."


def _current_plan_report(
    plan: Any,
    *,
    now: datetime | None = None,
    last_refresh_metadata: Any = None,
) -> dict[str, Any]:
    """Return whether a current plan has enough priced coverage for activation."""
    now = _as_utc(now or dt_util.utcnow())
    if plan is None:
        return {
            "present": False,
            "healthy": False,
            "current": False,
            "adequate_coverage": False,
            "usable_optimization_horizon_hours": None,
            "required_optimization_horizon_hours": 8.0,
            "fresh": False,
            "last_refresh_succeeded": False,
            "safe": False,
            "message": "No current plan is available.",
        }
    health = str(getattr(plan, "health", ""))
    status = str(getattr(plan, "status", ""))
    confidence = float(getattr(plan, "confidence", 0.0) or 0.0)
    interval_minutes = max(int(getattr(plan, "interval_minutes", 5) or 5), 1)
    max_age = timedelta(minutes=max(interval_minutes * 2, 15))
    created_at = _datetime_or_none(getattr(plan, "created_at", None))
    plan_age = None if created_at is None else now - created_at
    fresh = bool(plan_age is not None and timedelta(0) <= plan_age <= max_age)
    refresh = dict(last_refresh_metadata) if isinstance(last_refresh_metadata, dict) else {}
    refresh_completed_at = _datetime_or_none(refresh.get("completed_at"))
    refresh_age = None if refresh_completed_at is None else now - refresh_completed_at
    last_refresh_succeeded = bool(
        refresh.get("succeeded") is True and refresh_age is not None and timedelta(0) <= refresh_age <= max_age
    )
    configured_horizon = max(float(getattr(plan, "horizon_hours", 0.0) or 0.0), 0.0)
    required_horizon = min(configured_horizon, 8.0) if configured_horizon else 8.0
    usable_horizon_value = getattr(plan, "estimated_cost_horizon_hours", None)
    try:
        usable_horizon = float(usable_horizon_value) if usable_horizon_value is not None else None
    except (TypeError, ValueError):
        usable_horizon = None
    issues = [str(issue) for issue in list(getattr(plan, "input_issues", []) or [])]
    healthy = health == "healthy"
    input_health_safe = health in {"healthy", "degraded"}
    current = status == "current"
    adequate_coverage = bool(
        usable_horizon is not None
        and usable_horizon >= required_horizon
        and not any("incomplete_horizon" in issue for issue in issues)
    )
    safe = input_health_safe and current and confidence > 0 and adequate_coverage and fresh and last_refresh_succeeded
    if safe:
        health_label = "healthy" if healthy else "degraded"
        message = f"Current {health_label} plan has {usable_horizon:g} usable priced hours."
    elif not input_health_safe:
        message = "Current plan has unsafe required inputs."
    elif not current:
        message = "The latest plan is not current."
    elif not fresh:
        message = "The latest plan is older than the allowed refresh age."
    elif not last_refresh_succeeded:
        message = "No recent successful coordinator refresh confirms the current plan."
    elif not adequate_coverage:
        shown = "unknown" if usable_horizon is None else f"{usable_horizon:g}"
        message = f"Usable priced coverage is {shown} hours; at least {required_horizon:g} hours are required."
    else:
        message = "Current plan confidence is zero."
    return {
        "present": True,
        "healthy": healthy,
        "input_health_safe": input_health_safe,
        "current": current,
        "confidence": confidence,
        "adequate_coverage": adequate_coverage,
        "fresh": fresh,
        "age_seconds": None if plan_age is None else round(plan_age.total_seconds(), 3),
        "maximum_age_seconds": round(max_age.total_seconds(), 3),
        "last_refresh_succeeded": last_refresh_succeeded,
        "last_successful_refresh_at": None if refresh_completed_at is None else refresh_completed_at.isoformat(),
        "usable_optimization_horizon_hours": usable_horizon,
        "required_optimization_horizon_hours": required_horizon,
        "safe": safe,
        "message": message,
    }


def _bounded_join(values: list[str], *, limit: int = 5) -> str:
    """Return a short comma-separated list."""
    visible = [str(value) for value in values[:limit]]
    if len(values) > limit:
        visible.append(f"{len(values) - limit} more")
    return ", ".join(visible)


def _entity_report(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    *,
    required_areas: list[str] | None = None,
    include_shared: bool = True,
) -> dict[str, Any]:
    configured = _configured_entities(
        entry_data,
        required_areas=required_areas,
        include_shared=include_shared,
    )
    advisory_entities = set(
        _split_entities(entry_data.get(CONF_EV_SMART_CHARGING_TARGET_SOC))
    )
    missing: list[str] = []
    unavailable: list[str] = []
    advisory_missing: list[str] = []
    advisory_unavailable: list[str] = []
    for entity_id in configured:
        state = hass.states.get(entity_id)
        if state is None:
            target = advisory_missing if entity_id in advisory_entities else missing
            target.append(entity_id)
        elif _entity_unavailable(entity_id, getattr(state, "state", "")):
            target = (
                advisory_unavailable
                if entity_id in advisory_entities
                else unavailable
            )
            target.append(entity_id)
    return {
        "configured": configured,
        "missing": missing,
        "unavailable": unavailable,
        "advisory_missing": advisory_missing,
        "advisory_unavailable": advisory_unavailable,
        "available_count": (
            len(configured)
            - len(missing)
            - len(unavailable)
            - len(advisory_missing)
            - len(advisory_unavailable)
        ),
    }


def _entity_unavailable(entity_id: str, state_value: Any) -> bool:
    """Return true when a configured entity cannot be used for preflight."""
    state = str(state_value or "").lower()
    domain = entity_id.split(".", 1)[0]
    if domain in {"button", "input_button"}:
        return state == "unavailable"
    return state in {"unknown", "unavailable"}


def _service_report(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    *,
    required_areas: list[str] | None = None,
) -> dict[str, Any]:
    configured = _configured_services(entry_data, required_areas=required_areas)
    missing: list[str] = []
    unavailable: list[str] = []
    for service_name in configured:
        if "." not in service_name:
            missing.append(service_name)
            continue
        domain, service = service_name.split(".", 1)
        has_service = getattr(hass.services, "has_service", None)
        if callable(has_service) and not has_service(domain, service):
            unavailable.append(service_name)
    return {
        "configured": configured,
        "missing": missing,
        "unavailable": unavailable,
    }


def _configured_services(entry_data: dict[str, Any], *, required_areas: list[str] | None = None) -> list[str]:
    configured: list[str] = []
    if required_areas is None:
        configured.extend(
            str(entry_data[key])
            for key in _SERVICE_KEYS
            if entry_data.get(key)
        )
    return configured


def _recorder_report(hass: HomeAssistant) -> dict[str, Any]:
    components = getattr(getattr(hass, "config", None), "components", set())
    data = getattr(hass, "data", {})
    available = "recorder" in components or (isinstance(data, dict) and "recorder" in data)
    return {"available": available}


def _safety_report(options: dict[str, Any]) -> dict[str, Any]:
    planner_enabled = strict_bool(options.get(CONF_PLANNER_ENABLED), default=False)
    dry_run = strict_bool(options.get(CONF_DRY_RUN), default=True)
    return {
        "planner_enabled": planner_enabled,
        "dry_run": dry_run,
        "safe_first_run_mode": not planner_enabled and dry_run,
        "active_mode_requested": planner_enabled and not dry_run,
    }


def _production_report(
    store_data: dict[str, Any],
    options: dict[str, Any],
    control_areas: dict[str, Any],
    *,
    expected_evidence_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Return production readiness state."""
    production_state = parse_production_state(store_data.get("production"))
    production = production_state.raw
    pause = store_data.get("control_pause", {})
    device_controls = {
        "ev": strict_bool(options.get(CONF_EV_CONTROL_ENABLED), default=False),
        "climate": strict_bool(options.get(CONF_CLIMATE_CONTROL_ENABLED), default=False),
        "enphase": strict_bool(options.get(CONF_ENPHASE_CONTROL_ENABLED), default=False),
    }
    required_control_areas = list(control_areas.get("required", []))
    dry_run_ready_cycles = production_state.dry_run_ready_cycles
    required_areas_configured = all(
        bool(control_areas.get("details", {}).get(area, {}).get("configured")) for area in required_control_areas
    )
    dry_run_evidence_complete = (
        dry_run_ready_cycles >= DRY_RUN_READY_CYCLES_REQUIRED
        and bool(required_control_areas)
        and required_areas_configured
        and bool(expected_evidence_fingerprint)
        and production_state.dry_run_evidence_fingerprint == expected_evidence_fingerprint
    )
    return {
        "armed": production_state.armed,
        "armed_at": production.get("armed_at"),
        "acknowledged_at": production.get("acknowledged_at"),
        "dry_run_ready_cycles": dry_run_ready_cycles,
        "last_dry_run_ready_at": production.get("last_dry_run_ready_at"),
        "dry_run_evidence_fingerprint_matches": bool(
            expected_evidence_fingerprint
            and production_state.dry_run_evidence_fingerprint == expected_evidence_fingerprint
        ),
        "dry_run_evidence_complete": dry_run_evidence_complete,
        # Retained for one release for consumers of the old response schema.
        "ready_to_arm": dry_run_evidence_complete,
        "device_controls": device_controls,
        "device_controls_enabled": bool(required_control_areas),
        "required_control_areas": required_control_areas,
        "pause": pause,
    }


def production_evidence_fingerprint(entry_data: dict[str, Any], options: dict[str, Any]) -> str:
    """Bind dry-run evidence to the currently configured control contract."""
    entry_data = dict(entry_data)
    aliases = {
        CONF_EV_CHARGER: CONF_EV_SMART_CHARGING,
        CONF_EV_CHARGER_START: CONF_EV_SMART_CHARGING_START,
        CONF_EV_CHARGER_STOP: CONF_EV_SMART_CHARGING_STOP,
    }
    for current_key, legacy_key in aliases.items():
        if not entry_data.get(current_key) and entry_data.get(legacy_key):
            entry_data[current_key] = entry_data[legacy_key]
    control_areas = _control_area_report(entry_data, options)
    configured = list(control_areas["configured"])
    payload = {
        # Runtime participation switches narrow or widen the currently active
        # surface but do not change the reviewed device mappings and policies.
        "configured_control_areas": configured,
        "details": {area: {"configured": True} for area in configured},
        "entry_data": {key: entry_data[key] for key in sorted(entry_data) if key not in _EVIDENCE_ENTRY_EXCLUSIONS},
        "load_forecast_contract_version": FORECAST_CONTRACT_VERSION,
        "options": {key: options[key] for key in sorted(options) if key not in _EVIDENCE_OPTION_EXCLUSIONS},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _control_area_report(entry_data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Return configured, enabled, and required control surfaces."""
    configured = {
        "ev": any(
            bool(str(entry_data.get(key, "") or "").strip())
            for key in (
                CONF_EV_CHARGER,
                CONF_EV_CHARGER_START,
                CONF_EV_CHARGER_STOP,
                CONF_EV_SMART_CHARGING,
                CONF_EV_SMART_CHARGING_START,
                CONF_EV_SMART_CHARGING_STOP,
            )
        ),
        "hvac": bool(str(entry_data.get(CONF_DAIKIN_CLIMATE, "") or "").strip()),
        "enphase": bool(str(entry_data.get(CONF_ENPHASE_PROFILE, "") or "").strip()),
    }
    enabled = {
        "ev": strict_bool(options.get(CONF_EV_CONTROL_ENABLED), default=False),
        "hvac": strict_bool(options.get(CONF_CLIMATE_CONTROL_ENABLED), default=False),
        "enphase": strict_bool(options.get(CONF_ENPHASE_CONTROL_ENABLED), default=False),
    }
    required = [area for area in ("ev", "hvac", "enphase") if enabled[area]]
    return {
        "configured": [area for area, value in configured.items() if value],
        "enabled": [area for area, value in enabled.items() if value],
        "required": required,
        "details": {
            area: {
                "configured": configured[area],
                "enabled": enabled[area],
                "required": area in required,
            }
            for area in ("ev", "hvac", "enphase")
        },
    }


def _runtime_control_area_report(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    *,
    plan: Any,
    pause: Any,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return shared capability, pause, and confidence readiness by area."""
    control_areas = _control_area_report(entry_data, options)
    discovery = CapabilityDiscovery(hass, entry_data).inspect().as_dict()
    _apply_ev_keep_on_preflight(discovery, entry_data, options)
    _apply_control_area_readiness(hass, entry_data, control_areas, discovery)
    ready = list(control_areas.get("ready", []))
    available, paused = partition_control_areas_by_pause(pause, now, ready)
    confidence_eligible = confidence_eligible_control_areas(plan, available, options)
    control_areas.update(
        {
            "available": available,
            "paused": paused,
            "confidence_eligible": confidence_eligible,
            "confidence_blocked": [
                area for area in available if area not in confidence_eligible
            ],
        }
    )
    return control_areas, discovery


def _apply_control_area_readiness(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    control_areas: dict[str, Any],
    discovery: dict[str, Any],
) -> None:
    """Annotate enabled areas so one device outage does not stop the others."""
    ready: list[str] = []
    blocked: list[str] = []
    details = control_areas.get("details", {})
    for area in control_areas.get("required", []):
        entities = _entity_report(
            hass,
            entry_data,
            required_areas=[area],
            include_shared=False,
        )
        supported = bool(discovery.get(area, {}).get("supported"))
        area_ready = bool(
            details.get(area, {}).get("configured")
            and supported
            and not entities["missing"]
            and not entities["unavailable"]
        )
        area_details = details.setdefault(area, {})
        area_details.update(
            {
                "ready": area_ready,
                "missing_entities": entities["missing"],
                "unavailable_entities": entities["unavailable"],
            }
        )
        (ready if area_ready else blocked).append(area)
    control_areas["ready"] = ready
    control_areas["blocked"] = blocked


def _control_area_message(control_areas: dict[str, Any], discovery: dict[str, Any]) -> str:
    """Return a concise capability message for required control areas."""
    required = list(control_areas.get("required", []))
    if not required:
        return "No configured control areas are enabled; capability discovery is advisory."
    ready = list(control_areas.get("ready", []))
    blocked = list(control_areas.get("blocked", []))
    if ready and not blocked:
        return f"Required control areas are supported: {_bounded_join(ready)}."
    if ready:
        return (
            f"Ready control areas: {_bounded_join(ready)}; "
            f"blocked areas remain isolated: {_bounded_join(blocked)}."
        )
    unsupported = [area for area in required if not bool(discovery[area]["supported"])]
    return f"Required control areas are unsupported: {_bounded_join(unsupported or blocked)}."


def _audit_report(store_data: dict[str, Any]) -> dict[str, Any]:
    entries = list(store_data.get("execution_audit") or store_data.get("outcomes") or [])
    recent = [_bounded_audit_entry(entry) for entry in entries[-10:]]
    return {
        "outcome_count": len(entries),
        "recent_outcomes": recent,
        "last_outcome": recent[-1] if recent else None,
    }


def _configured_entities(
    entry_data: dict[str, Any],
    *,
    required_areas: list[str] | None = None,
    include_shared: bool = True,
) -> list[str]:
    entity_ids: set[str] = set()
    for key, value in entry_data.items():
        control_area = _entity_control_area(key)
        if required_areas is not None and control_area is None and not include_shared:
            continue
        if required_areas is not None and control_area is not None and control_area not in required_areas:
            continue
        if key.endswith("_entity") or key in {CONF_CLIMATE_AUTOMATIONS, CONF_CLIMATE_ZONES, CONF_PERSON_ENTITIES}:
            entity_ids.update(_split_entities(value))
    return sorted(entity_ids)


def _entity_control_area(config_key: str) -> str | None:
    """Return the optional device-control area owning an entity mapping."""
    if config_key == CONF_PERSON_ENTITIES:
        return "hvac"
    if config_key.startswith("ai_"):
        return "ai"
    if config_key.startswith("ev_"):
        return "ev"
    if config_key.startswith(("daikin_", "climate_", "weather_")):
        return "hvac"
    if config_key.startswith("enphase_"):
        return "enphase"
    return None


def _split_entities(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if "." in item and item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if "." in str(item)]
    return []


def _bounded_audit_entry(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    allowed = {
        "attempted_at",
        "plan_id",
        "action_id",
        "asset",
        "kind",
        "result",
        "reason",
        "service_target",
    }
    return {key: entry.get(key) for key in allowed if key in entry}
