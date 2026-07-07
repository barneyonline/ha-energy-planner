"""Bounded local AI advisor adapter."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_TASK_ENTITY,
    CONF_AI_TIMEOUT_SECONDS,
    CONF_ENPHASE_MIN_SAVINGS,
)
from .models import DecisionContext, EnergyPlan, InputHealth, PlannerMode

ALLOWED_ADJUSTMENTS = {
    "suggested_precondition_lead_minutes": (0, 120),
    "suggested_forecast_buffer_percent": (0, 30),
}

ALLOWED_RESPONSE_FIELDS = frozenset(
    {
        "alerts",
        "confidence",
        "reasoning_summary",
        "suggested_takeover_savings_threshold",
        *ALLOWED_ADJUSTMENTS.keys(),
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
        "The AI response included fields that Energy Planner will not accept because AI advice cannot command "
        "devices or change hard constraints."
    ),
    "ai_response_unsupported_fields": "The AI response included fields outside the supported advice contract.",
    "ai_response_no_accepted_fields": "The AI response was valid JSON but did not include any supported advice fields.",
    "ai_service_not_configured": "No AI advisor service is configured.",
    "ai_service_unavailable": "The configured AI advisor service is not currently available in Home Assistant.",
    "ai_provider_not_ready": "The configured AI provider entity is not ready yet.",
    "ai_timeout": "The AI advisor service did not respond before the configured timeout.",
    "ai_skipped_planner_disabled": (
        "AI advice was skipped because Energy Planner is disabled and the current inputs are healthy. "
        "Disabled mode is a control setting, not an advice outcome."
    ),
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
        """Return bounded local AI advice, or a skipped/rejected result."""
        service_name, entry_data = _resolve_ai_service(self.hass, self.entry_data)
        if plan.mode == PlannerMode.DISABLED and plan.health == InputHealth.HEALTHY and not plan.input_issues:
            return _with_provider(_rejected_result("skipped", "ai_skipped_planner_disabled", None), entry_data)
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
                    message=f"The AI advisor service failed with {err.__class__.__name__}.",
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
        accepted = validate_ai_response(parsed, takeover_bounds=_takeover_bounds(self.options))
        if not accepted:
            return _with_provider(
                _rejected_result("rejected", "ai_response_no_accepted_fields", service_name), entry_data
            )
        return _with_provider(AIAdviceResult("accepted", accepted, None, service_name), entry_data)


def validate_ai_response(
    response: Mapping[str, Any],
    *,
    takeover_bounds: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Validate and clamp a local AI response.

    Only whitelisted soft-policy suggestions are accepted. Unsupported or
    forbidden top-level fields reject the whole response.
    """
    if _invalid_response_reason(response):
        return {}
    accepted: dict[str, Any] = {}
    alerts = response.get("alerts")
    if isinstance(alerts, list):
        accepted["alerts"] = [str(item)[:160] for item in alerts[:5]]
    elif isinstance(alerts, str):
        accepted["alerts"] = [item[:160] for item in _split_alerts(alerts)[:5]]
    for key, bounds in ALLOWED_ADJUSTMENTS.items():
        value = response.get(key)
        if isinstance(value, int | float):
            low, high = bounds
            accepted[key] = min(max(value, low), high)
    takeover_value = response.get("suggested_takeover_savings_threshold")
    if isinstance(takeover_value, int | float):
        low, high = takeover_bounds or (0.0, 10.0)
        accepted["suggested_takeover_savings_threshold"] = min(max(float(takeover_value), low), high)
    confidence = response.get("confidence")
    if isinstance(confidence, int | float):
        accepted["confidence"] = min(max(float(confidence), 0.0), 1.0)
    summary = response.get("reasoning_summary")
    if isinstance(summary, str):
        accepted["reasoning_summary"] = summary[:500]
    return accepted


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
        "message": REJECTION_MESSAGES.get(reason, "The AI advice was rejected by Energy Planner."),
    }


def _build_prompt(context: DecisionContext, plan: EnergyPlan) -> str:
    """Build compact redacted AI prompt."""
    payload = {
        "contract": {
            "allowed": [
                "alerts",
                "suggested_precondition_lead_minutes",
                "suggested_forecast_buffer_percent",
                "suggested_takeover_savings_threshold",
                "reasoning_summary",
                "confidence",
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
            "forecast": _preview_summary(plan.preview),
        },
        "context": {
            "input_health": str(context.input_health),
            "haeo_status": str(context.haeo_status),
            "occupancy_state": str(context.occupancy_state),
            "battery_soc_percent": context.current_battery_soc_percent,
            "ev_soc_known": context.current_ev_soc_percent is not None,
            "slot_count": len(context.slots),
            "active_override_kinds": [override.kind for override in context.active_overrides[:3]],
        },
    }
    return json.dumps(payload, separators=(",", ":"), default=str)


def _build_instructions(context: DecisionContext, plan: EnergyPlan, *, structured: bool) -> str:
    """Build model instructions for provider calls."""
    task = (
        "Advise Energy Planner from this JSON. No tools, device commands, service calls, hard-constraint changes, "
        "credentials, entity IDs, or location history. Planner mode DISABLED is a control setting, not an input "
        "health reason, advice reason, or OK outcome. Do not justify no advice from disabled mode; base advice only "
        "on input health, listed issues, confidence, forecast, and safe soft-policy suggestions. "
    )
    if structured:
        task += "Fill only useful structured fields; use null or empty text when no advice is needed.\n"
    else:
        task += (
            "Return exactly one JSON object using only alerts, suggested_precondition_lead_minutes, "
            "suggested_forecast_buffer_percent, suggested_takeover_savings_threshold, reasoning_summary, confidence.\n"
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
        "task_name": "Energy Planner advice",
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


def _split_alerts(value: str) -> list[str]:
    """Return compact alert strings from structured text output."""
    alerts: list[str] = []
    for part in re.split(r"[\n;]+", value):
        alert = part.strip(" -\t")
        if alert:
            alerts.append(alert)
    return alerts


def _takeover_bounds(options: Mapping[str, Any]) -> tuple[float, float]:
    configured = float(options.get(CONF_ENPHASE_MIN_SAVINGS, 0.25))
    return (0.0, max(10.0, configured))
