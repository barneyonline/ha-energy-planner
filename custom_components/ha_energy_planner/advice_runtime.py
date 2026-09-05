"""Advice runtime boundary; coordinator retains mutable state and lock authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from math import ceil
from time import perf_counter
from typing import TYPE_CHECKING, Any, Never

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .ai_advisor import AI_ACTION_TARGETS, AIAdviceResult, LocalAIAdvisor
from .const import (
    AI_ADVICE_MIN_INTERVAL_SECONDS,
    CONF_AI_TASK_ENTITY,
    DOMAIN,
)
from .models import (
    EnergyPlan,
    InputHealth,
    to_jsonable,
)
from .notifications import defer_persistent_notification
from .weather import (
    _parse_datetime_or_none,
)

if TYPE_CHECKING:
    from .coordinator import EnergyPlannerCoordinator

_LOGGER = logging.getLogger(__name__)

async def _async_get_throttled_ai_advice(
    self: EnergyPlannerCoordinator,
    context: Any,
    plan: EnergyPlan,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    *,
    force_current_plan: bool = False,
) -> tuple[AIAdviceResult, bool]:
    """Return AI troubleshooting only for safe, materially changed plans."""
    if plan.health == InputHealth.UNSAFE or plan.status == "unsafe" or plan.confidence <= 0:
        return (
            AIAdviceResult(
                status="skipped",
                accepted={},
                rejected_reason="ai_skipped_unsafe_plan",
                rejected_detail={
                    "reason": "ai_skipped_unsafe_plan",
                    "message": "AI troubleshooting was skipped because the plan is unsafe or has zero confidence.",
                },
                service_called=None,
            ),
            False,
        )
    plan_fingerprint = _material_plan_fingerprint(plan)
    if (
        not force_current_plan
        and _latest_ai_plan_fingerprint(self.store.data.get("ai_recommendations")) == plan_fingerprint
    ):
        return (
            AIAdviceResult(
                status="skipped",
                accepted={},
                rejected_reason="ai_plan_unchanged",
                rejected_detail={
                    "reason": "ai_plan_unchanged",
                    "message": "AI troubleshooting was reused because the material plan has not changed.",
                },
                service_called=None,
            ),
            False,
        )
    admitted_at = dt_util.utcnow()
    last_called_at = _latest_ai_attempt_at(self.store.data)
    if last_called_at is not None:
        elapsed = admitted_at - last_called_at
        if elapsed < timedelta(seconds=AI_ADVICE_MIN_INTERVAL_SECONDS):
            remaining_seconds = max(
                ceil(AI_ADVICE_MIN_INTERVAL_SECONDS - elapsed.total_seconds()),
                1,
            )
            return (
                AIAdviceResult(
                    status="skipped",
                    accepted={},
                    rejected_reason="ai_rate_limited",
                    rejected_detail={
                        "reason": "ai_rate_limited",
                        "message": (
                            "AI troubleshooting was skipped because the last provider call was less than "
                            "5 minutes ago."
                        ),
                        "retry_after_seconds": remaining_seconds,
                        "last_called_at": last_called_at.isoformat(),
                    },
                    service_called=None,
                ),
                False,
            )
    if getattr(self, "_tearing_down", False):
        return AIAdviceResult("skipped", {}, "ai_entry_unloading", None), False
    await self.store.async_save_ai_attempt({"created_at": admitted_at})
    if getattr(self, "_tearing_down", False):
        return AIAdviceResult("skipped", {}, "ai_entry_unloading", None), False
    result = await LocalAIAdvisor(self.hass, entry_data, options).async_get_advice(context, plan)
    # The caller persists provider metadata; attach the stable key without
    # widening the public AI result contract.
    result.rejected_detail.setdefault("plan_fingerprint", plan_fingerprint)
    return result, True


async def async_request_ai_advice(self: EnergyPlannerCoordinator) -> None:
    """Schedule a fresh bounded explanation for the current safe plan."""
    if getattr(self, "_tearing_down", False):
        return
    if not str(self.entry_data.get(CONF_AI_TASK_ENTITY, "") or "").strip():
        await self._async_reject_ai_advice_request(
            "AI troubleshooting is not ready: no AI Task entity is configured.",
            "No AI Task entity is configured.",
        )
    plan = self.data
    context = getattr(self, "_last_decision_context", None)
    if plan is None or context is None:
        await self._async_reject_ai_advice_request(
            "AI troubleshooting is not ready: no current plan is available.",
            "No current plan is available.",
        )
    if plan.health == InputHealth.UNSAFE or plan.status == "unsafe" or plan.confidence <= 0:
        await self._async_reject_ai_advice_request(
            "AI troubleshooting is not ready: the current plan is unsafe.",
            "The current plan is unsafe or has zero confidence.",
        )
    now = dt_util.utcnow()
    last_called_at = _latest_ai_attempt_at(self.store.data)
    if last_called_at is not None:
        remaining = AI_ADVICE_MIN_INTERVAL_SECONDS - (now - last_called_at).total_seconds()
        if remaining > 0:
            seconds = max(ceil(remaining), 1)
            await self._async_reject_ai_advice_request(
                f"AI troubleshooting is rate limited for another {seconds} seconds.",
                f"Try again in {seconds} seconds.",
            )
    fingerprint = _material_plan_fingerprint(plan)
    current = getattr(self, "_ai_advice_task", None)
    if getattr(self, "_ai_advice_pending_fingerprint", None) == fingerprint or (
        current is not None and not current.done()
    ):
        self._set_ai_advice_pending(fingerprint, "request_in_flight")
        await self._async_notify_ai_advice("An explanation is already being prepared.")
        return
    self._ai_current_plan_safe = True
    self._ai_current_plan_fingerprint = fingerprint
    self._ai_advice_fingerprint = fingerprint
    self._set_ai_advice_pending(fingerprint, "request_in_flight")
    request_context = replace(context, created_at=now)
    self.async_update_listeners()
    await self._async_notify_ai_advice("Preparing an explanation for the current plan…")
    if getattr(self, "_tearing_down", False):
        return
    self._ai_advice_task = self.hass.async_create_task(
        self._async_run_ai_advice(
            request_context,
            plan,
            self.entry_data,
            self.options,
            fingerprint,
            force_current_plan=True,
        )
    )


@callback
def _sync_ai_request_to_plan(self: EnergyPlannerCoordinator, plan: EnergyPlan) -> None:
    """Cancel an in-flight explanation if its deterministic plan is obsolete."""
    safe = plan.health != InputHealth.UNSAFE and plan.status != "unsafe" and plan.confidence > 0
    fingerprint = _material_plan_fingerprint(plan) if safe else None
    self._ai_current_plan_safe = safe
    self._ai_current_plan_fingerprint = fingerprint
    current = getattr(self, "_ai_advice_task", None)
    if current is None or current.done():
        return
    if not safe or getattr(self, "_ai_advice_fingerprint", None) != fingerprint:
        current.cancel()
        self._clear_ai_advice_pending()
        self.async_update_listeners()


async def _async_run_ai_advice(
    self: EnergyPlannerCoordinator,
    context: Any,
    plan: EnergyPlan,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    fingerprint: str,
    *,
    force_current_plan: bool = False,
) -> None:
    """Persist one bounded button-triggered troubleshooting result."""
    started = perf_counter()
    try:
        if force_current_plan:
            ai_result, should_store = await self._async_get_throttled_ai_advice(
                context,
                plan,
                entry_data,
                options,
                force_current_plan=True,
            )
        else:
            ai_result, should_store = await self._async_get_throttled_ai_advice(context, plan, entry_data, options)
        if getattr(self, "_tearing_down", False):
            return
        if not should_store:
            self._clear_ai_advice_pending(fingerprint)
            self.async_update_listeners()
            if force_current_plan:
                await self._async_notify_ai_advice(_ai_advice_notification_message(ai_result))
            return
        if (
            self._ai_advice_fingerprint != fingerprint
            or not self._ai_current_plan_safe
            or self._ai_current_plan_fingerprint != fingerprint
        ):
            self._clear_ai_advice_pending(fingerprint)
            if force_current_plan:
                await self._async_notify_ai_advice(
                    "The plan changed before the explanation completed. Press Explain to try again."
                )
            return
        plan_changed_while_waiting = False
        async with self._planner_lock:
            if getattr(self, "_tearing_down", False):
                return
            if not self._ai_current_plan_safe or self._ai_current_plan_fingerprint != fingerprint:
                self._clear_ai_advice_pending(fingerprint)
                plan_changed_while_waiting = True
            else:
                async with self.store.async_delay_save():
                    await self.store.async_add_ai_recommendation(
                        {
                            "created_at": context.created_at,
                            "plan_id": plan.plan_id,
                            "plan_fingerprint": fingerprint,
                            "plan_health": str(plan.health),
                            "status": ai_result.status,
                            "accepted": ai_result.accepted,
                            "rejected_reason": ai_result.rejected_reason,
                            "rejected_detail": ai_result.rejected_detail,
                            "service_called": ai_result.service_called,
                            CONF_AI_TASK_ENTITY: ai_result.ai_task_entity,
                        }
                    )
                    await self.store.async_attach_ai_to_forecast_snapshot(
                        plan.plan_id,
                        {
                            "status": ai_result.status,
                            "accepted_fields": sorted(ai_result.accepted),
                            "rejected_reason": ai_result.rejected_reason,
                            "rejected_detail": ai_result.rejected_detail,
                            "service_called": ai_result.service_called,
                            CONF_AI_TASK_ENTITY: ai_result.ai_task_entity,
                        },
                    )
        if plan_changed_while_waiting:
            if force_current_plan:
                await self._async_notify_ai_advice(
                    "The plan changed before the explanation completed. Press Explain to try again."
                )
            return
        if getattr(self, "_tearing_down", False):
            return
        self._clear_ai_advice_pending(fingerprint)
        self._last_phase_durations["ai_background_ms"] = round((perf_counter() - started) * 1000, 3)
        self.async_update_listeners()
        if force_current_plan:
            await self._async_notify_ai_advice(_ai_advice_notification_message(ai_result))
    except asyncio.CancelledError:
        self._clear_ai_advice_pending(fingerprint)
        if force_current_plan and not getattr(self, "_tearing_down", False):
            await self._async_notify_ai_advice(
                "The plan changed before the explanation completed. Press Explain to try again."
            )
        raise
    except Exception:  # noqa: BLE001 - advice must never fail the planner task.
        if getattr(self, "_tearing_down", False):
            return
        self._clear_ai_advice_pending(fingerprint)
        self.async_update_listeners()
        _LOGGER.exception("AI troubleshooting failed")
        if force_current_plan:
            await self._async_notify_ai_advice(
                "The explanation could not be completed. Check the Energy Planner logs and try again."
            )
    finally:
        if getattr(self, "_ai_advice_task", None) is asyncio.current_task():
            self._ai_advice_task = None


@callback
def _set_ai_advice_pending(self: EnergyPlannerCoordinator, fingerprint: str, reason: str) -> None:
    """Expose bounded in-memory status for advice awaiting completion."""
    self._ai_advice_pending_fingerprint = fingerprint
    self._ai_advice_pending_reason = reason


@callback
def _clear_ai_advice_pending(self: EnergyPlannerCoordinator, fingerprint: str | None = None) -> None:
    """Clear pending status when it still belongs to the selected plan."""
    current = getattr(self, "_ai_advice_pending_fingerprint", None)
    if fingerprint is not None and current != fingerprint:
        return
    self._ai_advice_pending_fingerprint = None
    self._ai_advice_pending_reason = None


async def _async_notify_ai_advice(self: EnergyPlannerCoordinator, message: str) -> None:
    """Publish bounded button feedback without affecting planner operation."""
    if getattr(self, "_tearing_down", False):
        return
    entry = getattr(self, "entry", None)
    entry_id = getattr(entry, "entry_id", None)
    notification_id = f"{_AI_ADVICE_NOTIFICATION_ID}_{entry_id}" if entry_id else _AI_ADVICE_NOTIFICATION_ID
    if defer_persistent_notification(
        self.hass,
        notification_id,
        lambda: self._async_notify_ai_advice(message),
        owner_id=entry_id,
    ):
        return
    services = getattr(getattr(self, "hass", None), "services", None)
    async_call = getattr(services, "async_call", None)
    if not callable(async_call):
        return
    title = getattr(entry, "title", None) or "Energy Planner"
    try:
        await async_call(
            "persistent_notification",
            "create",
            {
                "title": f"{title}: explanation",
                "message": message[:2000],
                "notification_id": notification_id,
            },
            blocking=False,
        )
    except Exception:  # noqa: BLE001 - notification failure must not discard advice.
        _LOGGER.warning("Could not publish the Energy Planner explanation notification")


async def _async_reject_ai_advice_request(
    self: EnergyPlannerCoordinator,
    error_message: str,
    reason: str,
) -> Never:
    """Notify the operator and reject an explanation that cannot start."""
    await self._async_notify_ai_advice(f"**Explanation unavailable.**\n\n{reason}")
    raise HomeAssistantError(
        error_message,
        translation_domain=DOMAIN,
        translation_key="ai_advice_not_ready",
        translation_placeholders={"reason": reason},
    )


def _material_plan_fingerprint(plan: EnergyPlan) -> str:
    """Return a stable key excluding generated plan IDs and timestamps."""
    payload = {
        "health": plan.health,
        "mode": plan.mode,
        "confidence": plan.confidence,
        "status": plan.status,
        "issues": sorted(plan.input_issues),
        "actions": [
            {
                "asset": action.asset,
                "kind": action.kind,
                "desired_state": action.desired_state,
                "reason_codes": action.reason_codes,
                "confidence": action.confidence,
            }
            for action in plan.actions
        ],
        "estimated_daily_cost": plan.estimated_daily_cost,
        "preview": _material_preview(plan.preview[:24]),
    }
    encoded = json.dumps(to_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _material_preview(value: Any) -> Any:
    """Remove refresh-relative timestamps while retaining decision values."""
    if isinstance(value, dict):
        return {
            str(key): _material_preview(item)
            for key, item in value.items()
            if str(key) not in {"valid_at", "created_at", "execute_not_before", "execute_not_after"}
        }
    if isinstance(value, list):
        return [_material_preview(item) for item in value]
    return to_jsonable(value)


def _latest_ai_plan_fingerprint(recommendations: Any) -> str | None:
    """Return the last stored material plan fingerprint."""
    latest = _latest_accepted_ai_recommendation(recommendations)
    return _ai_recommendation_fingerprint(latest)


def _ai_recommendation_fingerprint(recommendation: Any) -> str | None:
    """Return the current or legacy material fingerprint for one recommendation."""
    if not isinstance(recommendation, dict):
        return None
    fingerprint = recommendation.get("plan_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        return fingerprint
    detail = recommendation.get("rejected_detail")
    if isinstance(detail, dict):
        detail_fingerprint = detail.get("plan_fingerprint")
        if isinstance(detail_fingerprint, str):
            return detail_fingerprint
    return None


def _latest_accepted_ai_recommendation(recommendations: Any) -> dict[str, Any] | None:
    """Return the latest accepted recommendation eligible for reuse."""
    if not isinstance(recommendations, list):
        return None
    for item in reversed(recommendations):
        if isinstance(item, dict) and item.get("status") == "accepted":
            return item
    return None


def _latest_ai_attempt_at(store_data: dict[str, Any]) -> datetime | None:
    """Use admission time even when an obsolete/cancelled result was discarded."""
    prior = _latest_ai_service_call_at(store_data.get("ai_recommendations"))
    attempt = store_data.get("ai_last_attempt")
    parsed = _parse_datetime_or_none(attempt.get("created_at")) if isinstance(attempt, dict) else None
    candidates = [dt_util.as_utc(value) for value in (prior, parsed) if isinstance(value, datetime)]
    return max(candidates) if candidates else None


def _latest_ai_service_call_at(recommendations: Any) -> datetime | None:
    """Return the latest timestamp where an AI provider service was actually called."""
    if not isinstance(recommendations, list):
        return None
    for item in reversed(recommendations):
        if not isinstance(item, dict) or not item.get("service_called"):
            continue
        created_at = item.get("created_at")
        if isinstance(created_at, datetime):
            return created_at
        if isinstance(created_at, str):
            parsed = dt_util.parse_datetime(created_at)
            if isinstance(parsed, datetime):
                return parsed
    return None


def _ai_advice_notification_message(result: AIAdviceResult) -> str:
    """Render a bounded, user-visible explanation result."""
    accepted = result.accepted if isinstance(result.accepted, dict) else {}
    outcome = accepted.get("outcome")
    summary = str(accepted.get("summary", "") or "").strip()
    if outcome == "no_action_needed" and summary:
        return f"**No action needed.**\n\n{summary}"
    if outcome == "action_required" and summary:
        target = str(accepted.get("affected_item", "") or "")
        target_name = AI_ACTION_TARGETS.get(target, (target.replace("_", " ").title(), ()))[0]
        return (
            f"{summary}\n\n"
            f"- **Affected item:** {target_name}\n"
            f"- **Problem:** {accepted.get('problem', 'Not provided')}\n"
            f"- **Next step:** {accepted.get('next_step', 'Not provided')}\n"
            f"- **Expected benefit:** {accepted.get('expected_benefit', 'Not provided')}\n"
            f"- **Verify:** {accepted.get('verification', 'Not provided')}"
        )
    detail = result.rejected_detail if isinstance(result.rejected_detail, dict) else {}
    message = str(detail.get("message", "") or "").strip()
    return f"**No explanation available.**\n\n{message or 'The AI response was not usable. Try again.'}"


_AI_ADVICE_NOTIFICATION_ID = "ha_energy_planner_ai_explanation"
