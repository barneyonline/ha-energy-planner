"""Tests for AI response validation and local advisor calls."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from custom_components.ha_energy_planner.ai_advisor import (
    LocalAIAdvisor,
    _battery_soc_band,
    _build_instructions,
    _build_prompt,
    _invalid_response_detail,
    _loads,
    _parse_response,
    _preview_summary,
    ai_rejection_detail,
    validate_ai_response,
)
from custom_components.ha_energy_planner.const import (
    CONF_AI_ADVISOR_SERVICE,
    CONF_AI_TASK_ENTITY,
    DEFAULT_OPTIONS,
)
from custom_components.ha_energy_planner.models import (
    DecisionContext,
    DecisionSlot,
    EnergyPlan,
    HAEOStatus,
    InputHealth,
    OccupancyState,
    PlannerMode,
)


def test_ai_response_is_clamped_and_trimmed() -> None:
    result = validate_ai_response(
        {
            "alerts": ["a", "b", "c", "d", "e", "f"],
            "suggested_precondition_lead_minutes": 999,
            "suggested_forecast_buffer_percent": -5,
            "suggested_takeover_savings_threshold": 99,
            "confidence": 2,
            "reasoning_summary": "x" * 600,
        },
        takeover_bounds=(0.0, 1.0),
    )
    assert result["alerts"] == ["a", "b", "c", "d", "e"]
    assert result["suggested_precondition_lead_minutes"] == 120
    assert result["suggested_forecast_buffer_percent"] == 0
    assert result["suggested_takeover_savings_threshold"] == 1.0
    assert result["confidence"] == 1.0
    assert len(result["reasoning_summary"]) == 500


def test_ai_response_accepts_structured_alert_text() -> None:
    result = validate_ai_response(
        {
            "alerts": "PV forecast confidence is low\nBattery reserve is tight",
            "reasoning_summary": "Inputs look plausible.",
        }
    )

    assert result["alerts"] == ["PV forecast confidence is low", "Battery reserve is tight"]


def test_ai_response_rejects_unsupported_or_forbidden_fields() -> None:
    assert (
        validate_ai_response(
            {
                "suggested_precondition_lead_minutes": 30,
                "device_service_calls": [{"domain": "climate", "service": "set_temperature"}],
            }
        )
        == {}
    )
    assert validate_ai_response({"reasoning_summary": "ok", "extra_note": "unsupported"}) == {}


class FakeServices:
    """Minimal HA service bus."""

    def __init__(self, response: Any, available: bool = True) -> None:
        self.response = response
        self.available = available
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return self.available and domain == "ai_task" and service == "generate_data"

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        return_response: bool = False,
    ) -> Any:
        self.calls.append((domain, service, data))
        return self.response


class FakeFallbackServices(FakeServices):
    """Service bus that simulates HA versions without return_response support."""

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> Any:
        self.calls.append((domain, service, data))
        return self.response


class FakeFailingServices(FakeServices):
    """Service bus that raises during provider calls."""

    async def async_call(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("provider failed")


class FakeStates:
    """Minimal HA state machine."""

    def __init__(self, entity_ids: list[str] | None = None) -> None:
        self._entity_ids = entity_ids or []

    def async_entity_ids(self, domain: str | None = None) -> list[str]:
        if domain is None:
            return list(self._entity_ids)
        return [entity_id for entity_id in self._entity_ids if entity_id.startswith(f"{domain}.")]

    def get(self, entity_id: str) -> object | None:
        return object() if entity_id in self._entity_ids else None


class FakeHass:
    """Minimal HA object."""

    def __init__(self, response: Any, available: bool = True, entity_ids: list[str] | None = None) -> None:
        self.services = FakeServices(response, available)
        self.states = FakeStates(entity_ids)


class TimeoutContext:
    """Async context manager that raises like asyncio.timeout."""

    async def __aenter__(self) -> None:
        raise TimeoutError

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _context() -> DecisionContext:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return DecisionContext(
        created_at=now,
        plan_id="plan-1",
        slots=[
            DecisionSlot(
                valid_at=now,
                import_price=0.2,
                export_price=0.05,
                pv_forecast_kw=1,
                baseline_load_forecast_kw=2,
            )
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=60,
        occupancy_state=OccupancyState.OCCUPIED,
        haeo_status=HAEOStatus.READY,
        input_health=InputHealth.HEALTHY,
    )


def _plan() -> EnergyPlan:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return EnergyPlan(
        plan_id="plan-1",
        created_at=now,
        horizon_hours=24,
        interval_minutes=5,
        status="current",
        health=InputHealth.HEALTHY,
        mode=PlannerMode.DRY_RUN,
        summary="test",
        confidence=1.0,
        estimated_daily_cost=1.23,
        actions=[],
        preview=[],
    )


def test_local_ai_advisor_accepts_valid_json_response() -> None:
    response = {
        "response": json.dumps(
            {
                "alerts": ["PV forecast confidence is low"],
                "suggested_precondition_lead_minutes": 30,
                "suggested_forecast_buffer_percent": 10,
                "suggested_takeover_savings_threshold": 0.5,
                "reasoning_summary": "Inputs look plausible.",
                "confidence": 0.74,
            }
        )
    }
    hass = FakeHass(response, entity_ids=["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )
    assert result.status == "accepted"
    assert result.accepted["suggested_precondition_lead_minutes"] == 30
    assert result.accepted["suggested_takeover_savings_threshold"] == 0.5
    assert "contract" in hass.services.calls[0][2]["instructions"]
    assert "Return exactly one JSON object" in hass.services.calls[0][2]["instructions"]
    assert hass.services.calls[0][2]["entity_id"] == "ai_task.extended_openai_ai_task"


def test_local_ai_advisor_ignores_legacy_conversation_service() -> None:
    response = {
        "data": {
            "alerts": "PV forecast confidence is low",
        },
    }
    hass = FakeHass(response, entity_ids=["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "conversation.process",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert result.status == "skipped"
    assert result.rejected_reason == "ai_service_not_configured"
    assert hass.services.calls == []


def test_local_ai_advisor_accepts_ai_task_data_response() -> None:
    response = {
        "conversation_id": "conv-1",
        "data": {
            "alerts": ["Battery reserve is tight"],
            "reasoning_summary": "Keep reserve buffer.",
            "confidence": 0.66,
        },
    }
    hass = FakeHass(response, entity_ids=["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert result.status == "accepted"
    assert result.accepted["alerts"] == ["Battery reserve is tight"]
    assert hass.services.calls[0][0:2] == ("ai_task", "generate_data")
    service_data = hass.services.calls[0][2]
    assert service_data["task_name"] == "Energy Planner advice"
    assert service_data["entity_id"] == "ai_task.extended_openai_ai_task"
    assert "structure" not in service_data
    assert "contract" in hass.services.calls[0][2]["instructions"]


def test_local_ai_advisor_rejects_malformed_output() -> None:
    hass = FakeHass({"response": "not json"}, entity_ids=["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )
    assert result.status == "rejected"
    assert result.rejected_reason == "ai_response_not_json"
    assert result.rejected_detail["message"] == "The AI service did not return a JSON object."
    assert result.accepted == {}


def test_local_ai_advisor_rejects_forbidden_fields() -> None:
    hass = FakeHass(
        {
            "response": json.dumps(
                {
                    "suggested_forecast_buffer_percent": 10,
                    "hard_constraint_changes": {"battery_reserve_percent": 5},
                }
            )
        }
    )
    hass.states = FakeStates(["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )
    assert result.status == "rejected"
    assert result.rejected_reason == "ai_response_forbidden_fields"
    assert result.rejected_detail == {
        "reason": "ai_response_forbidden_fields",
        "message": (
            "The AI response included fields that Energy Planner will not accept because AI advice cannot command "
            "devices or change hard constraints."
        ),
        "fields": ["hard_constraint_changes"],
    }
    assert result.accepted == {}


def test_local_ai_advisor_rejection_detail_lists_unsupported_fields() -> None:
    hass = FakeHass(
        {
            "response": json.dumps(
                {
                    "reasoning_summary": "Looks fine.",
                    "extra_note": "Use a lower reserve.",
                }
            )
        }
    )
    hass.states = FakeStates(["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert result.status == "rejected"
    assert result.rejected_reason == "ai_response_unsupported_fields"
    assert result.rejected_detail["fields"] == ["extra_note"]


def test_local_ai_advisor_skips_unavailable_service() -> None:
    hass = FakeHass({}, available=False, entity_ids=["ai_task.extended_openai_ai_task"])
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )
    assert result.status == "skipped"
    assert result.rejected_reason == "ai_service_unavailable"
    assert hass.services.calls == []


def test_local_ai_advisor_skips_provider_entity_not_ready() -> None:
    hass = FakeHass({})
    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert result.status == "skipped"
    assert result.rejected_reason == "ai_provider_not_ready"
    assert hass.services.calls == []


def test_local_ai_advisor_rejects_missing_and_invalid_service() -> None:
    missing = asyncio.run(LocalAIAdvisor(FakeHass({}), {}, DEFAULT_OPTIONS).async_get_advice(_context(), _plan()))
    invalid = asyncio.run(
        LocalAIAdvisor(
            FakeHass({}),
            {CONF_AI_ADVISOR_SERVICE: "conversation_process"},
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert missing.status == "skipped"
    assert missing.rejected_reason == "ai_service_not_configured"
    assert invalid.status == "skipped"
    assert invalid.rejected_reason == "ai_service_not_configured"


def test_local_ai_advisor_supports_service_call_without_return_response() -> None:
    hass = FakeHass(
        {
            "response": json.dumps(
                {
                    "reasoning_summary": "Fallback call shape worked.",
                    "confidence": 0.7,
                }
            )
        },
        entity_ids=["ai_task.extended_openai_ai_task"],
    )
    hass.services = FakeFallbackServices(hass.services.response)

    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert result.status == "accepted"
    assert result.accepted["reasoning_summary"] == "Fallback call shape worked."


def test_local_ai_advisor_rejects_timeout_and_service_failure(monkeypatch: object) -> None:
    timeout_hass = FakeHass({}, entity_ids=["ai_task.extended_openai_ai_task"])
    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        "custom_components.ha_energy_planner.ai_advisor.asyncio.timeout",
        lambda timeout: TimeoutContext(),
    )

    timed_out = asyncio.run(
        LocalAIAdvisor(
            timeout_hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    monkeypatch.setattr("custom_components.ha_energy_planner.ai_advisor.asyncio.timeout", real_timeout)
    failing_hass = FakeHass({}, entity_ids=["ai_task.extended_openai_ai_task"])
    failing_hass.services = FakeFailingServices({})
    failed = asyncio.run(
        LocalAIAdvisor(
            failing_hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert timed_out.rejected_reason == "ai_timeout"
    assert failed.rejected_reason == "ai_service_failed:RuntimeError"
    assert failed.rejected_detail["message"] == "The AI advisor service failed with RuntimeError."


def test_local_ai_advisor_rejects_valid_json_with_no_supported_fields() -> None:
    hass = FakeHass({"response": "{}"}, entity_ids=["ai_task.extended_openai_ai_task"])

    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), _plan())
    )

    assert result.status == "rejected"
    assert result.rejected_reason == "ai_response_no_accepted_fields"


def test_local_ai_advisor_skips_disabled_healthy_plan_without_provider_call() -> None:
    hass = FakeHass(
        {"response": json.dumps({"reasoning_summary": "Energy planner is disabled and OK.", "confidence": 0.7})},
        entity_ids=["ai_task.extended_openai_ai_task"],
    )
    plan = _plan()
    plan.mode = PlannerMode.DISABLED

    result = asyncio.run(
        LocalAIAdvisor(
            hass,
            {
                CONF_AI_ADVISOR_SERVICE: "ai_task.generate_data",
                CONF_AI_TASK_ENTITY: "ai_task.extended_openai_ai_task",
            },
            DEFAULT_OPTIONS,
        ).async_get_advice(_context(), plan)
    )

    assert result.status == "skipped"
    assert result.accepted == {}
    assert result.rejected_reason == "ai_skipped_planner_disabled"
    assert result.service_called is None
    assert hass.services.calls == []


def test_invalid_response_detail_handles_non_string_keys() -> None:
    detail = _invalid_response_detail({1: "bad"}, "ai_response_unsupported_fields")

    assert detail == {
        "reason": "ai_response_unsupported_fields",
        "message": "The AI response included non-string object keys.",
    }


def test_ai_rejection_detail_uses_generic_message_for_unknown_reason() -> None:
    assert ai_rejection_detail("new_reason") == {
        "reason": "new_reason",
        "message": "The AI advice was rejected by Energy Planner.",
    }


def test_build_instructions_supports_structured_prompt() -> None:
    instructions = _build_instructions(_context(), _plan(), structured=True)

    assert "Fill only useful structured fields" in instructions
    assert "Return exactly one JSON object" not in instructions
    assert "Planner mode DISABLED is a control setting" in instructions
    assert "not an input health reason, advice reason, or OK outcome" in instructions


def test_ai_prompt_minimizes_household_state_detail() -> None:
    payload = json.loads(_build_prompt(_context(), _plan()))

    assert "occupancy_state" not in payload["context"]
    assert "battery_soc_percent" not in payload["context"]
    assert payload["context"]["occupancy_known"] is True
    assert payload["context"]["battery_soc_band"] == "medium"
    assert [_battery_soc_band(value) for value in (None, 10, 50, 90)] == ["unknown", "low", "medium", "high"]


def test_parse_response_supports_common_nested_shapes() -> None:
    assert _parse_response({"data": '{"confidence":0.4}'}) == {"confidence": 0.4}
    assert _parse_response({"response": {"text": '{"confidence":0.5}'}}) == {"confidence": 0.5}
    assert _parse_response({"confidence": 0.6}) == {"confidence": 0.6}
    assert _parse_response('{"confidence":0.7}') == {"confidence": 0.7}
    assert _parse_response({"speech": {"speech": '{"confidence":0.8}'}}) == {"confidence": 0.8}
    assert _parse_response({"speech": {"plain": {"speech": '{"confidence":0.85}'}}}) == {"confidence": 0.85}
    assert _parse_response({"message": '{"confidence":0.9}'}) == {"confidence": 0.9}


def test_loads_extracts_fenced_and_embedded_json() -> None:
    assert _loads('```json\n{"confidence":0.3}\n```') == {"confidence": 0.3}
    assert _loads('prefix {"confidence":0.2} suffix') == {"confidence": 0.2}
    assert _loads("```json\n{bad}\n```") is None
    assert _loads("prefix {bad} suffix") is None


def test_preview_summary_reports_ranges_and_occupancy() -> None:
    preview = [
        {
            "valid_at": "2026-06-27T00:00:00+00:00",
            "import_price": 0.30,
            "export_price": 0.05,
            "pv_forecast_kw": 1.0,
            "baseline_load_forecast_kw": 2.0,
            "outdoor_temperature_forecast_c": 12,
            "battery_floor_percent": 10,
            "occupied": "home",
        },
        {
            "valid_at": "2026-06-27T00:05:00+00:00",
            "import_price": 0.10,
            "export_price": None,
            "pv_forecast_kw": 3.0,
            "baseline_load_forecast_kw": 1.5,
            "outdoor_temperature_forecast_c": 15,
            "battery_floor_percent": 10,
            "occupied": "away",
        },
    ]

    assert _preview_summary([]) == {}
    assert _preview_summary(preview) == {
        "samples": 2,
        "start": "2026-06-27T00:00:00+00:00",
        "end": "2026-06-27T00:05:00+00:00",
        "import_price": [0.1, 0.3],
        "export_price": [0.05, 0.05],
        "pv_forecast_kw": [1.0, 3.0],
        "baseline_load_forecast_kw": [1.5, 2.0],
        "outdoor_temperature_forecast_c": [12.0, 15.0],
        "battery_floor_percent": [10.0, 10.0],
        "occupied": ["away", "home"],
    }
