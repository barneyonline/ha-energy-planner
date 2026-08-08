"""Bounded local AI advisor adapter."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_TASK_ENTITY,
    CONF_AI_TIMEOUT_SECONDS,
)
from .models import DecisionContext, EnergyPlan

AI_ACTION_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "amber_import_price_entity": ("Import price input", ("amber_import_price", "import_price")),
    "amber_export_price_entity": ("Export price input", ("amber_export_price", "export_price")),
    "pv_forecast_entity": ("PV forecast input", ("pv_forecast", "solar_forecast")),
    "baseline_load_forecast_entity": ("Baseline load forecast input", ("baseline_load", "load_forecast")),
    "weather_entity": ("Weather input", ("weather_", "outdoor_temperature")),
    "battery_soc_entity": ("Battery SOC input", ("battery_soc", "battery_floor")),
    "ev_soc_entity": ("EV SOC input", ("ev_soc",)),
    "ev_connected_entity": ("EV connected input", ("ev_connected", "ev_not_connected")),
    "ev_charging_entity": ("EV charging-state input", ("ev_charging",)),
    "ev_charger_entity": ("EV charger control", ("ev_charger", "charger_control")),
    "ev_ready_by": ("EV Ready by", ("ready_by", "ev_infeasible")),
    "ev_target_soc": ("EV Target SOC", ("target_soc",)),
    "daikin_climate_entity": ("Climate control", ("daikin_", "climate_")),
    "climate_comfort": ("Climate comfort settings", ("comfort", "occupancy_", "person_")),
    "enphase_profile_entity": ("Enphase profile control", ("enphase_", "battery_profile")),
    "haeo_optimize_service": ("HAEO optimization service", ("haeo_",)),
    "automatic_control": ("Automatic control", ("production_", "control_", "planner_")),
}

ALLOWED_RESPONSE_FIELDS = frozenset(
    {
        "outcome",
        "summary",
        "affected_item",
        "problem",
        "next_step",
        "expected_benefit",
        "verification",
    }
)

FORBIDDEN_RESPONSE_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "battery_reserve",
        "battery_reserve_percent",
        "comfort_range",
        "credentials",
        "daikin_setting",
        "data",
        "device_service_calls",
        "enphase_profile",
        "entity_id",
        "ev_control",
        "ev_ready_by",
        "hard_constraint_changes",
        "location_history",
        "manual_hvac_override",
        "password",
        "profile_hold_minutes",
        "secret",
        "service_calls",
        "services",
        "target",
        "token",
    }
)

REJECTION_MESSAGES = {
    "ai_response_not_json": "The AI service did not return a JSON object.",
    "ai_response_forbidden_fields": (
        "The AI response included fields that Energy Planner will not accept because AI troubleshooting cannot command "
        "devices or change hard constraints."
    ),
    "ai_response_unsupported_fields": "The AI response included fields outside the troubleshooting contract.",
    "ai_response_not_actionable": (
        "The AI response did not identify a complete action tied to current planner evidence."
    ),
    "ai_service_not_configured": "No AI troubleshooting provider is configured.",
    "ai_service_unavailable": (
        "The configured AI troubleshooting provider is not currently available in Home Assistant."
    ),
    "ai_provider_not_ready": "The configured AI provider entity is not ready yet.",
    "ai_timeout": "The AI troubleshooting provider did not respond before the configured timeout.",
}


@dataclass(slots=True)
class AIAdviceResult:
    """Result from the bounded local AI advisor."""

    status: str
    accepted: dict[str, Any]
    rejected_reason: str | None
    service_called: str | None
    rejected_detail: dict[str, Any] = field(default_factory=dict)
    ai_task_entity: str | None = None


class LocalAIAdvisor:
    """Call a configured local model service with a narrow, redacted contract."""

    def __init__(self, hass: Any, entry_data: Mapping[str, Any], options: Mapping[str, Any]) -> None:
        """Initialize advisor."""
        self.hass = hass
        self.entry_data = entry_data
        self.options = options

    async def async_get_advice(self, context: DecisionContext, plan: EnergyPlan) -> AIAdviceResult:
        """Return a bounded explanation, or a skipped/rejected result."""
        service_name, entry_data = _resolve_ai_service(self.hass, self.entry_data)
        if not service_name:
            return _with_provider(_rejected_result("skipped", "ai_service_not_configured", None), entry_data)
        domain, service = service_name.split(".", 1)
        if _provider_entity_missing(self.hass, service_name, entry_data):
            return _with_provider(_rejected_result("skipped", "ai_provider_not_ready", service_name), entry_data)
        has_service = getattr(self.hass.services, "has_service", None)
        if callable(has_service) and not has_service(domain, service):
            return _with_provider(_rejected_result("skipped", "ai_service_unavailable", service_name), entry_data)

        payload = _service_payload(service_name, entry_data, context, plan)
        timeout = int(self.options.get(CONF_AI_TIMEOUT_SECONDS, 20))
        try:
            async with asyncio.timeout(timeout):
                try:
                    response = await self.hass.services.async_call(
                        domain,
                        service,
                        payload,
                        blocking=True,
                        return_response=True,
                    )
                except TypeError:
                    response = await self.hass.services.async_call(domain, service, payload, blocking=True)
        except TimeoutError:
            return _with_provider(_rejected_result("rejected", "ai_timeout", service_name), entry_data)
        except Exception as err:  # noqa: BLE001 - advisor must fail closed.
            return _with_provider(
                _rejected_result(
                    "rejected",
                    f"ai_service_failed:{err.__class__.__name__}",
                    service_name,
                    message=f"The AI troubleshooting provider failed with {err.__class__.__name__}.",
                ),
                entry_data,
            )

        parsed = _parse_response(response)
        if not isinstance(parsed, Mapping):
            return _with_provider(_rejected_result("rejected", "ai_response_not_json", service_name), entry_data)
        invalid_reason = _invalid_response_reason(parsed)
        if invalid_reason:
            return _with_provider(
                _rejected_result(
                    "rejected",
                    invalid_reason,
                    service_name,
                    rejected_detail=_invalid_response_detail(parsed, invalid_reason),
                ),
                entry_data,
            )
        accepted = validate_ai_response(parsed, allowed_targets=_actionable_targets(plan))
        if not accepted:
            return _with_provider(
                _rejected_result("rejected", "ai_response_not_actionable", service_name), entry_data
            )
        return _with_provider(AIAdviceResult("accepted", accepted, None, service_name), entry_data)


def validate_ai_response(
    response: Mapping[str, Any],
    *,
    allowed_targets: Collection[str] | None = None,
) -> dict[str, Any]:
    """Validate a bounded explain-or-troubleshoot response.

    Action-required results must be complete and tied to evidence-derived
    targets from the current deterministic plan. Generic tuning suggestions are
    not part of the contract.
    """
    if _invalid_response_reason(response):
        return {}
    outcome = response.get("outcome")
    summary = _bounded_text(response.get("summary"), 500)
    if outcome == "no_action_needed" and summary:
        return {"outcome": outcome, "summary": summary}
    if outcome != "action_required" or not summary:
        return {}
    target = response.get("affected_item")
    valid_targets = set(AI_ACTION_TARGETS if allowed_targets is None else allowed_targets)
    if not isinstance(target, str) or target not in valid_targets:
        return {}
    required = {
        "problem": 300,
        "next_step": 500,
        "expected_benefit": 300,
        "verification": 400,
    }
    details = {key: _bounded_text(response.get(key), limit) for key, limit in required.items()}
    if any(not value for value in details.values()):
        return {}
    return {
        "outcome": outcome,
        "summary": summary,
        "affected_item": target,
        **details,
    }


def _bounded_text(value: Any, limit: int) -> str | None:
    """Return trimmed non-empty text within the response contract limit."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _invalid_response_reason(response: Mapping[str, Any]) -> str | None:
    keys = set(response.keys())
    if any(not isinstance(key, str) for key in keys):
        return "ai_response_unsupported_fields"
    if keys & FORBIDDEN_RESPONSE_FIELDS:
        return "ai_response_forbidden_fields"
    if keys - ALLOWED_RESPONSE_FIELDS:
        return "ai_response_unsupported_fields"
    return None


def _invalid_response_detail(response: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Return bounded, safe-to-expose rejection detail for entity attributes."""
    detail = ai_rejection_detail(reason)
    keys = set(response.keys())
    if reason == "ai_response_forbidden_fields":
        fields = sorted(str(key) for key in keys & FORBIDDEN_RESPONSE_FIELDS)
        if fields:
            detail["fields"] = fields[:12]
    elif reason == "ai_response_unsupported_fields":
        if any(not isinstance(key, str) for key in keys):
            detail["message"] = "The AI response included non-string object keys."
        fields = sorted(str(key)[:80] for key in keys if isinstance(key, str) and key not in ALLOWED_RESPONSE_FIELDS)
        if fields:
            detail["fields"] = fields[:12]
    return detail


def _rejected_result(
    status: str,
    reason: str,
    service_called: str | None,
    *,
    message: str | None = None,
    rejected_detail: dict[str, Any] | None = None,
) -> AIAdviceResult:
    """Return a rejected/skipped result with human-readable detail."""
    detail = dict(rejected_detail or ai_rejection_detail(reason))
    if message:
        detail["message"] = message
    return AIAdviceResult(status, {}, reason, service_called, detail)


def ai_rejection_detail(reason: str) -> dict[str, Any]:
    """Return a stable rejection detail payload for entity attributes."""
    return {
        "reason": reason,
        "message": REJECTION_MESSAGES.get(reason, "The AI troubleshooting result was rejected by Energy Planner."),
    }


def _build_prompt(context: DecisionContext, plan: EnergyPlan) -> str:
    """Build compact redacted AI prompt."""
    payload = {
        "contract": {
            "allowed": [
                "outcome",
                "summary",
                "affected_item",
                "problem",
                "next_step",
                "expected_benefit",
                "verification",
            ],
            "forbidden": ["device_service_calls", "hard_constraint_changes", "credentials", "location_history"],
        },
        "plan": {
            "status": plan.status,
            "health": str(plan.health),
            "mode": str(plan.mode),
            "confidence": plan.confidence,
            "estimated_daily_cost": plan.estimated_daily_cost,
            "issues": plan.input_issues[:6],
            "actionable_targets": _actionable_target_evidence(plan),
            "planned_actions": [
                {
                    "asset": str(action.asset),
                    "kind": str(action.kind),
                    "reason_codes": action.reason_codes[:5],
                    "constraints": action.hard_constraints[:5],
                }
                for action in plan.actions[:6]
            ],
            "rejected_actions": _rejected_action_evidence(plan),
            "decision_summary": str(plan.decision_audit.get("summary") or "")[:500],
            "forecast": _preview_summary(plan.preview),
        },
        "context": {
            "input_health": str(context.input_health),
            "haeo_status": str(context.haeo_status),
            "occupancy_known": str(context.occupancy_state) != "unknown",
            "battery_soc_band": _battery_soc_band(context.current_battery_soc_percent),
            "ev_soc_known": context.current_ev_soc_percent is not None,
            "slot_count": len(context.slots),
            "active_override_kinds": [override.kind for override in context.active_overrides[:3]],
        },
    }
    return json.dumps(payload, separators=(",", ":"), default=str)


def _battery_soc_band(value: Any) -> str:
    """Return a coarse battery band instead of an exact household reading."""
    if not isinstance(value, int | float):
        return "unknown"
    if value < 25:
        return "low"
    if value < 75:
        return "medium"
    return "high"


def _build_instructions(context: DecisionContext, plan: EnergyPlan, *, structured: bool) -> str:
    """Build model instructions for provider calls."""
    task = (
        "Explain or troubleshoot this Energy Planner plan. Do not provide general optimization tips. No tools, "
        "device commands, service calls, setting changes, hard-constraint changes, credentials, entity IDs, or "
        "location history. Use outcome action_required only when one actionable_targets item is directly supported "
        "by the supplied issue or rejection evidence. For action_required, return that target ID as affected_item "
        "and provide a specific problem, exact user next_step, expected_benefit, and verification. If there is no "
        "material user action, use outcome no_action_needed and a short summary, omitting all action fields. Planner "
        "mode DISABLED is a control setting, not an input-health problem or a reason for either outcome. "
    )
    if structured:
        task += "Fill only fields allowed by the response contract.\n"
    else:
        task += (
            "Return exactly one JSON object using only outcome, summary, affected_item, problem, next_step, "
            "expected_benefit, verification.\n"
        )
    return f"{task}{_build_prompt(context, plan)}"


def _service_payload(
    service_name: str,
    entry_data: Mapping[str, Any],
    context: DecisionContext,
    plan: EnergyPlan,
) -> dict[str, Any]:
    """Return service data for the configured AI provider type."""
    prompt = _build_instructions(context, plan, structured=False)
    payload = {
        "task_name": "Energy Planner explain or troubleshoot",
        "instructions": prompt,
        "entity_id": entry_data.get(CONF_AI_TASK_ENTITY),
    }
    return {key: value for key, value in payload.items() if value}


def _resolve_ai_service(hass: Any, entry_data: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """Return the supported AI Task service when configured."""
    configured_task_entity = str(entry_data.get(CONF_AI_TASK_ENTITY, "") or "").strip()
    if configured_task_entity:
        data = dict(entry_data)
        data[CONF_AI_TASK_ENTITY] = configured_task_entity
        data[CONF_AI_ADVISOR_SERVICE] = "ai_task.generate_data"
        return "ai_task.generate_data", data
    return "", entry_data


def _with_provider(result: AIAdviceResult, entry_data: Mapping[str, Any]) -> AIAdviceResult:
    """Attach resolved provider metadata to a result."""
    result.ai_task_entity = str(entry_data.get(CONF_AI_TASK_ENTITY) or "") or None
    return result


def _provider_entity_missing(hass: Any, service_name: str, entry_data: Mapping[str, Any]) -> bool:
    """Return whether the selected provider entity has not been registered yet."""
    entity_id = str(entry_data.get(CONF_AI_TASK_ENTITY) or "")

    states = getattr(hass, "states", None)
    get_state = getattr(states, "get", None)
    return bool(entity_id) and callable(get_state) and get_state(entity_id) is None


def _parse_response(response: Any) -> Any:
    """Extract JSON object from common Home Assistant service response shapes."""
    if isinstance(response, Mapping):
        data = response.get("data")
        if isinstance(data, Mapping):
            return data
        if isinstance(data, str):
            return _loads(data)

        text = _extract_response_text(response)
        if text is not None:
            return _loads(text)

        for key in ("response", "text", "message", "content"):
            value = response.get(key)
            if isinstance(value, Mapping):
                parsed = _parse_response(value)
                if parsed is not None:
                    return parsed
            if isinstance(value, str):
                return _loads(value)
        return response if not _invalid_response_reason(response) else None
    if isinstance(response, str):
        return _loads(response)
    return None


def _preview_summary(preview: list[dict[str, Any]]) -> dict[str, Any]:
    """Return compact forecast ranges for the AI prompt."""
    if not preview:
        return {}
    summary: dict[str, Any] = {"samples": len(preview)}
    first = preview[0]
    last = preview[-1]
    if first.get("valid_at"):
        summary["start"] = first.get("valid_at")
    if last.get("valid_at"):
        summary["end"] = last.get("valid_at")
    for key in (
        "import_price",
        "export_price",
        "pv_forecast_kw",
        "baseline_load_forecast_kw",
        "outdoor_temperature_forecast_c",
        "battery_floor_percent",
    ):
        value_range = _numeric_range(item.get(key) for item in preview)
        if value_range is not None:
            summary[key] = value_range
    occupied = sorted({str(item.get("occupied")) for item in preview if item.get("occupied") is not None})
    if occupied:
        summary["occupied"] = occupied[:3]
    return summary


def _numeric_range(values: Any) -> list[float] | None:
    """Return min/max range for numeric values."""
    numbers = [float(value) for value in values if isinstance(value, int | float)]
    if not numbers:
        return None
    return [round(min(numbers), 4), round(max(numbers), 4)]


def _loads(value: str) -> Any:
    value = value.strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = value.find("{")
    end = value.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(value[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _extract_response_text(response: Mapping[str, Any]) -> str | None:
    """Extract assistant text from common nested response wrappers."""
    speech = response.get("speech")
    if isinstance(speech, Mapping):
        plain = speech.get("plain")
        if isinstance(plain, Mapping) and isinstance(plain.get("speech"), str):
            return plain["speech"]
        if isinstance(speech.get("speech"), str):
            return speech["speech"]

    nested_response = response.get("response")
    if isinstance(nested_response, Mapping):
        return _extract_response_text(nested_response)

    for key in ("text", "message", "content"):
        value = response.get(key)
        if isinstance(value, str):
            return value
    return None


def _actionable_targets(plan: EnergyPlan) -> set[str]:
    """Return target IDs supported by current issue or rejection evidence."""
    evidence = _plan_problem_evidence(plan)
    return {
        target
        for target, (_label, markers) in AI_ACTION_TARGETS.items()
        if any(marker in evidence for marker in markers)
    }


def _actionable_target_evidence(plan: EnergyPlan) -> list[dict[str, Any]]:
    """Return bounded target names and matching evidence for the prompt."""
    evidence_items = [str(item) for item in plan.input_issues]
    evidence_items.extend(_rejected_reason_codes(plan))
    rows: list[dict[str, Any]] = []
    for target in sorted(_actionable_targets(plan)):
        label, markers = AI_ACTION_TARGETS[target]
        matches = [item for item in evidence_items if any(marker in item for marker in markers)]
        rows.append({"id": target, "name": label, "evidence": matches[:6]})
    return rows[:12]


def _plan_problem_evidence(plan: EnergyPlan) -> str:
    """Return normalized bounded evidence used only to select allowed targets."""
    items = [str(item) for item in plan.input_issues]
    items.extend(_rejected_reason_codes(plan))
    return " ".join(items)[:6000]


def _rejected_reason_codes(plan: EnergyPlan) -> list[str]:
    """Return bounded rejection reason and evidence codes."""
    codes: list[str] = []
    for item in plan.rejected_actions[:12]:
        if not isinstance(item, Mapping):
            continue
        for key in ("reason", "reason_codes", "hard_constraints", "evidence"):
            value = item.get(key)
            if isinstance(value, list):
                codes.extend(str(part) for part in value[:6])
            elif value is not None:
                codes.append(str(value))
    return codes[:40]


def _rejected_action_evidence(plan: EnergyPlan) -> list[dict[str, Any]]:
    """Return compact rejected-action evidence without raw desired state."""
    result: list[dict[str, Any]] = []
    for item in plan.rejected_actions[:8]:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                key: item.get(key)
                for key in ("asset", "device", "reason", "reason_codes", "hard_constraints", "evidence")
                if item.get(key) is not None
            }
        )
    return result


def ai_target_detail(
    target: str,
    entry_data: Mapping[str, Any],
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a validated target to the exact configured entity or setting."""
    label = AI_ACTION_TARGETS.get(target, (target.replace("_", " ").title(), ()))[0]
    configured = entry_data.get(target, options.get(target))
    detail = {"key": target, "name": label, "configured_value": configured}
    return {key: value for key, value in detail.items() if value not in (None, "", [], {})}
