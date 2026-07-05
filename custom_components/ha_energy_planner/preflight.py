"""Non-commanding active-mode preflight checks."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_CLIMATE_AUTOMATIONS,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
    CONF_HAEO_OPTIMIZE_SERVICE,
    CONF_PERSON_ENTITIES,
    CONF_PLANNER_ENABLED,
    DEFAULT_HAEO_OPTIMIZE_SERVICE,
)
from .discovery import CapabilityDiscovery
from .entry_data import combined_entry_data

_SERVICE_KEYS = (
    CONF_HAEO_OPTIMIZE_SERVICE,
    CONF_AI_ADVISOR_SERVICE,
)


def build_preflight_report(hass: HomeAssistant, coordinator: Any) -> dict[str, Any]:
    """Return a redacted readiness report without calling device services."""
    entry_data = combined_entry_data(coordinator.entry)
    options = coordinator.options
    discovery = CapabilityDiscovery(hass, entry_data).inspect().as_dict()
    entity_report = _entity_report(hass, entry_data)
    service_report = _service_report(hass, entry_data)
    recorder = _recorder_report(hass)
    safety = _safety_report(options)
    production = _production_report(coordinator.store.data, options)
    audit = _audit_report(coordinator.store.data)

    blocking = [
        *entity_report["missing"],
        *entity_report["unavailable"],
        *service_report["missing"],
        *service_report["unavailable"],
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
            "blocking": True,
            "message": _availability_message(
                "All configured entities are present and available.",
                missing=entity_report["missing"],
                unavailable=entity_report["unavailable"],
            ),
        },
        {
            "check": "configured_services_available",
            "ok": not service_report["missing"] and not service_report["unavailable"],
            "blocking": True,
            "message": _availability_message(
                "All configured services are registered.",
                missing=service_report["missing"],
                unavailable=service_report["unavailable"],
            ),
        },
        {
            "check": "recorder_available",
            "ok": recorder["available"],
            "blocking": False,
            "message": "Recorder is available for history imports."
            if recorder["available"]
            else "Recorder is not detected.",
        },
        {
            "check": "production_gate_ready",
            "ok": production["ready_to_arm"],
            "blocking": False,
            "message": _production_gate_message(production),
        },
        {
            "check": "production_control_armed",
            "ok": production["armed"],
            "blocking": False,
            "message": (
                "Production control is armed."
                if production["armed"]
                else "Production control has not been armed. Review preflight, then use Arm production control when ready."
            ),
        },
    ]
    active_control_ready = (
        not blocking
        and all(bool(discovery[area]["supported"]) for area in ("haeo", "ev", "hvac", "enphase"))
        and production["armed"]
        and production["device_controls_enabled"]
    )
    return {
        "ok": active_control_ready,
        "active_control_ready": active_control_ready,
        "mode": safety,
        "production": production,
        "checks": checks,
        "entities": entity_report,
        "services": service_report,
        "recorder": recorder,
        "discovery": discovery,
        "audit": audit,
    }


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
    if production["ready_to_arm"]:
        return "Production gate has enough dry-run evidence and all device controls are explicitly enabled."

    details: list[str] = []
    dry_run_ready_cycles = int(production.get("dry_run_ready_cycles", 0) or 0)
    if dry_run_ready_cycles < 3:
        details.append(f"{dry_run_ready_cycles}/3 healthy dry-run cycles recorded")
    disabled_controls = [
        name for name, enabled in dict(production.get("device_controls", {})).items() if not bool(enabled)
    ]
    if disabled_controls:
        details.append(f"device controls disabled: {_bounded_join(disabled_controls)}")
    if not details:
        return "Production gate is not ready to arm yet."
    return f"Production gate is not ready to arm yet; {'; '.join(details)}."


def _bounded_join(values: list[str], *, limit: int = 5) -> str:
    """Return a short comma-separated list."""
    visible = [str(value) for value in values[:limit]]
    if len(values) > limit:
        visible.append(f"{len(values) - limit} more")
    return ", ".join(visible)


def _entity_report(hass: HomeAssistant, entry_data: dict[str, Any]) -> dict[str, Any]:
    configured = _configured_entities(entry_data)
    missing: list[str] = []
    unavailable: list[str] = []
    for entity_id in configured:
        state = hass.states.get(entity_id)
        if state is None:
            missing.append(entity_id)
        elif _entity_unavailable(entity_id, getattr(state, "state", "")):
            unavailable.append(entity_id)
    return {
        "configured": configured,
        "missing": missing,
        "unavailable": unavailable,
        "available_count": len(configured) - len(missing) - len(unavailable),
    }


def _entity_unavailable(entity_id: str, state_value: Any) -> bool:
    """Return true when a configured entity cannot be used for preflight."""
    state = str(state_value or "").lower()
    domain = entity_id.split(".", 1)[0]
    if domain in {"button", "input_button"}:
        return state == "unavailable"
    return state in {"unknown", "unavailable"}


def _service_report(hass: HomeAssistant, entry_data: dict[str, Any]) -> dict[str, Any]:
    configured = _configured_services(entry_data)
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


def _configured_services(entry_data: dict[str, Any]) -> list[str]:
    configured = [str(entry_data.get(CONF_HAEO_OPTIMIZE_SERVICE) or DEFAULT_HAEO_OPTIMIZE_SERVICE)]
    configured.extend(
        str(entry_data[key]) for key in _SERVICE_KEYS if key != CONF_HAEO_OPTIMIZE_SERVICE and entry_data.get(key)
    )
    return configured


def _recorder_report(hass: HomeAssistant) -> dict[str, Any]:
    components = getattr(getattr(hass, "config", None), "components", set())
    data = getattr(hass, "data", {})
    available = "recorder" in components or (isinstance(data, dict) and "recorder" in data)
    return {"available": available}


def _safety_report(options: dict[str, Any]) -> dict[str, Any]:
    planner_enabled = bool(options.get(CONF_PLANNER_ENABLED, False))
    dry_run = bool(options.get(CONF_DRY_RUN, True))
    return {
        "planner_enabled": planner_enabled,
        "dry_run": dry_run,
        "safe_first_run_mode": not planner_enabled and dry_run,
        "active_mode_requested": planner_enabled and not dry_run,
    }


def _production_report(store_data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Return production readiness state."""
    production = dict(store_data.get("production", {}))
    pause = dict(store_data.get("control_pause", {}))
    device_controls = {
        "ev": bool(options.get(CONF_EV_CONTROL_ENABLED, False)),
        "climate": bool(options.get(CONF_CLIMATE_CONTROL_ENABLED, False)),
        "enphase": bool(options.get(CONF_ENPHASE_CONTROL_ENABLED, False)),
    }
    dry_run_ready_cycles = int(production.get("dry_run_ready_cycles", 0) or 0)
    return {
        "armed": bool(production.get("armed", False)),
        "armed_at": production.get("armed_at"),
        "acknowledged_at": production.get("acknowledged_at"),
        "dry_run_ready_cycles": dry_run_ready_cycles,
        "last_dry_run_ready_at": production.get("last_dry_run_ready_at"),
        "ready_to_arm": dry_run_ready_cycles >= 3 and all(device_controls.values()),
        "device_controls": device_controls,
        "device_controls_enabled": all(device_controls.values()),
        "pause": pause,
    }


def _audit_report(store_data: dict[str, Any]) -> dict[str, Any]:
    entries = list(store_data.get("execution_audit") or store_data.get("outcomes") or [])
    recent = [_bounded_audit_entry(entry) for entry in entries[-10:]]
    return {
        "outcome_count": len(entries),
        "recent_outcomes": recent,
        "last_outcome": recent[-1] if recent else None,
    }


def _configured_entities(entry_data: dict[str, Any]) -> list[str]:
    entity_ids: set[str] = set()
    for key, value in entry_data.items():
        if key.endswith("_entity") or key in {CONF_CLIMATE_AUTOMATIONS, CONF_PERSON_ENTITIES}:
            entity_ids.update(_split_entities(value))
    return sorted(entity_ids)


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
