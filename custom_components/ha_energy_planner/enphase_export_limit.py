"""Enphase Export Limit feedback and durable, asynchronous control transaction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .adapter_helpers import DeviceTargetUnavailable, async_call_device_service
from .const import CONF_AMBER_EXPORT_PRICE, CONF_ENPHASE_EXPORT_LIMIT_ENTITY
from .export_limit_policy import EXPORT_ASSET, READBACK_MAX_AGE, confirmed_feedback, tariff_blocks
from .models import ActionAsset, ActionKind, ActionOutcome, OutcomeResult, PlanAction


def target_identity(hass: HomeAssistant, entity: str) -> dict[str, str] | None:
    """Require the upstream Export Limit identity, never a default site."""
    registered = er.async_get(hass).async_get(entity)
    if (
        registered is None
        or registered.platform != "enphase_ev"
        or registered.domain not in {"select", "sensor"}
        or not registered.config_entry_id
        or not registered.unique_id.endswith("_export_limit")
    ):
        return None
    return {
        "entity_id": entity,
        "config_entry_id": registered.config_entry_id,
        "unique_id": registered.unique_id,
        "device_id": registered.device_id or "",
    }


def feedback_entity_id(hass: HomeAssistant, entity: str | None) -> str | None:
    """Resolve a Select's readback sensor by registry identity, never its name."""
    if not entity:
        return None
    if entity.startswith("sensor."):
        return entity
    identity = target_identity(hass, entity)
    if identity is None:
        return None
    registry = er.async_get(hass)
    sensor = registry.async_get_entity_id("sensor", "enphase_ev", identity["unique_id"])
    sensor_identity = target_identity(hass, sensor) if sensor else None
    if sensor_identity is None or any(
        sensor_identity[key] != identity[key] for key in ("config_entry_id", "unique_id", "device_id")
    ):
        return None
    return sensor


def feedback(hass: HomeAssistant, entity: str | None, now: datetime) -> tuple[dict[str, Any], str | None]:
    """Read confirmed/requested state separately; freshness comes from readback."""
    state = hass.states.get(entity) if entity else None
    if state is None or state.state in {"unknown", "unavailable"}:
        return {}, "export_limit_entity_unavailable"
    identity = target_identity(hass, str(entity))
    if identity is None:
        return {}, "export_limit_target_invalid"
    sensor = feedback_entity_id(hass, entity)
    observed, issue = confirmed_feedback(hass.states.get(sensor) if sensor else None, now)
    observed["identity"] = identity
    return observed, issue


def evidence(hass: HomeAssistant, data: dict[str, Any], options: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Build price-only readiness without depending on the energy optimizer."""
    observed, issue = feedback(hass, data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
    state = hass.states.get(str(data[CONF_AMBER_EXPORT_PRICE])) if data.get(CONF_AMBER_EXPORT_PRICE) else None
    blocks, tariff_issue = tariff_blocks(state, now, options)
    service_issue = None
    if not all(hass.services.has_service("enphase_ev", name) for name in ("set_export_limit", "disable_export_limit")):
        service_issue = "export_limit_services_unavailable"
    issue = issue or tariff_issue or service_issue
    if observed.get("pending"):
        issue = issue or "export_limit_pending"
    return {"ready": issue is None, "reason": issue, "feedback": observed, "blocks": blocks}


class ExportLimitControl:
    """Persist once, dispatch once, and reconcile subsequent gateway readback."""

    def __init__(
        self, hass: HomeAssistant, store: Any, data: dict[str, Any],
        before_dispatch: Callable[[], str | None] | None = None,
    ) -> None:
        self.hass, self.store, self.data = hass, store, data
        self.before_dispatch = before_dispatch

    @property
    def state(self) -> dict[str, Any]:
        value = self.store.data.get("ownership", {}).get(EXPORT_ASSET, {})
        return dict(value) if isinstance(value, dict) else {}

    async def save(self, state: dict[str, Any]) -> None:
        ownership = dict(self.store.data.get("ownership", {}))
        if state:
            ownership[EXPORT_ASSET] = state
        else:
            ownership.pop(EXPORT_ASSET, None)
        await self.store.async_save_ownership(ownership)

    async def audit(
        self, pending: dict[str, Any], result: OutcomeResult, reason: str, now: datetime, observed: dict[str, Any]
    ) -> None:
        await self.store.async_add_outcome(
            ActionOutcome(
                action_id=pending["action_id"],
                attempted_at=now,
                result=result,
                reason=reason,
                pre_state=pending.get("pre_state", {}),
                post_state=observed,
                plan_id=pending["plan_id"],
                asset=EXPORT_ASSET,
                kind=pending["kind"],
                service_target=pending["identity"]["entity_id"],
                desired_state={"watts": pending["watts"]},
            )
        )

    async def adopt_external_feedback(self, observed: dict[str, Any], now: datetime) -> None:
        """Retire superseded ownership without clearing unrelated failure pauses."""
        previous = self.state
        adopted: dict[str, Any] = {
            "identity": observed["identity"],
            "expected": {"watts": observed["watts"], "slew_rate": observed["slew_rate"]},
            "last_external_change": {
                "observed_at": now.isoformat(),
                "previous_expected": previous.get("expected", {}),
                "watts": observed["watts"],
                "slew_rate": observed["slew_rate"],
            },
        }
        if previous.get("pause") and previous["pause"] != "external_export_limit_conflict":
            adopted["pause"] = previous["pause"]
        await self.save(adopted)

    async def reconcile(self, now: datetime) -> str | None:
        """Resolve durable commands and reconcile external changes without latching a pause."""
        state = self.state
        if not state:
            return None
        observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
        if issue:
            pending = state.get("pending")
            if pending and now.timestamp() - pending["sent_at"] >= READBACK_MAX_AGE.total_seconds():
                await self.unconfirmed(state, pending, now, observed)
                return "export_limit_unconfirmed"
            if issue == "export_limit_unsupported" and not pending:
                state["pause"] = issue
                await self.save(state)
            return issue
        if observed["identity"] != state["identity"]:
            return "export_limit_target_changed"
        pending = state.get("pending")
        if state.get("pause") == "external_export_limit_conflict" and not pending:
            if observed["pending"]:
                return "export_limit_pending"
            await self.adopt_external_feedback(observed, now)
            return None
        if pending:
            matches = (
                not observed["pending"]
                and observed["watts"] == pending["watts"]
                and observed["last_readback"] >= pending["sent_at"]
                and (pending.get("slew_rate") is None or observed["slew_rate"] == pending["slew_rate"])
            )
            if matches:
                if not pending["restore"] and observed["slew_rate"] != pending["pre_state"]["slew_rate"]:
                    await self.audit(pending, OutcomeResult.REJECTED, "external_export_limit_changed", now, observed)
                    await self.adopt_external_feedback(observed, now)
                    return self.state.get("pause")
                await self.audit(
                    pending,
                    OutcomeResult.RESTORED if pending["restore"] else OutcomeResult.APPLIED,
                    "export_limit_restored" if pending["restore"] else "export_limit_confirmed",
                    now,
                    observed,
                )
                if pending["restore"]:
                    await self.save({})
                    return None
                state.pop("pending")
                state["expected"] = {"watts": observed["watts"], "slew_rate": observed["slew_rate"]}
                await self.save(state)
            elif now.timestamp() - pending["sent_at"] >= READBACK_MAX_AGE.total_seconds():
                await self.unconfirmed(state, pending, now, observed)
                return "export_limit_unconfirmed"
            else:
                return "export_limit_pending"
        elif observed["pending"]:
            return "export_limit_pending"
        elif state.get("expected") and any(
            state["expected"].get(key) != observed[key] for key in ("watts", "slew_rate")
        ):
            await self.adopt_external_feedback(observed, now)
            return self.state.get("pause")
        return self.state.get("pause")

    async def unconfirmed(
        self, state: dict[str, Any], pending: dict[str, Any], now: datetime, observed: dict[str, Any]
    ) -> None:
        """Pause once even if readback itself has become unavailable or stale."""
        if state.get("pause") != "export_limit_unconfirmed":
            state["pause"] = "export_limit_unconfirmed"
            await self.save(state)
            await self.audit(pending, OutcomeResult.FAILED, "export_limit_unconfirmed", now, observed)

    async def resume(self) -> None:
        """Clear an explicit pause only when no uncertain command remains."""
        state = self.state
        if state.get("pending"):
            return
        state.pop("pause", None)
        await self.save(state if state.get("baseline") else {})

    async def execute(self, action: PlanAction, now: datetime) -> tuple[OutcomeResult, str, bool]:
        """Execute a validated tariff transition without blocking for confirmation."""
        issue = await self.reconcile(now)
        if issue:
            return OutcomeResult.REJECTED, issue, False
        observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
        if issue or observed.get("pending"):
            return OutcomeResult.REJECTED, issue or "export_limit_pending", False
        if self.state.get("restoring"):
            return OutcomeResult.REJECTED, "export_limit_restoration_pending", False
        desired = action.desired_state["watts"]
        if observed["watts"] == desired:
            return OutcomeResult.SKIPPED, "export_limit_already_desired", False
        state = self.state or {"identity": observed["identity"]}
        state.setdefault("baseline", {"watts": observed["watts"], "slew_rate": observed["slew_rate"]})
        return await self.dispatch(state, action, desired, now, observed)

    async def dispatch(
        self,
        state: dict[str, Any],
        action: PlanAction,
        watts: int | None,
        now: datetime,
        observed: dict[str, Any],
        *,
        restore: bool = False,
    ) -> tuple[OutcomeResult, str, bool]:
        """Durability precedes the service boundary, including compensation."""
        previous = self.state
        pending = {
            "action_id": action.action_id,
            "plan_id": action.plan_id,
            "kind": str(action.kind),
            "identity": state["identity"],
            "watts": watts,
            "sent_at": now.timestamp(),
            "restore": restore,
            "pre_state": observed,
            "slew_rate": state["baseline"]["slew_rate"] if restore else None,
        }
        state["pending"] = pending
        try:
            await self.save(state)
            await self.store.async_flush()
        except BaseException:
            # The service boundary has not been entered. A failed/cancelled
            # persistence attempt cannot create an uncertain gateway request.
            await self.save(previous)
            raise
        dispatch_now = datetime.now(UTC)
        latest, blocked = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), dispatch_now)
        if not blocked and latest["identity"] != state["identity"]:
            blocked = "export_limit_target_changed"
        if not blocked and latest["pending"]:
            blocked = "export_limit_pending"
        if not blocked and any(latest[key] != observed[key] for key in ("watts", "slew_rate")):
            blocked = "external_export_limit_changed"
        if not restore:
            if not action.execute_not_before <= dispatch_now < action.execute_not_after:
                blocked = "export_tariff_expired"
            if self.before_dispatch:
                blocked = blocked or self.before_dispatch()
        if blocked:
            if blocked == "external_export_limit_changed":
                await self.adopt_external_feedback(latest, dispatch_now)
            else:
                await self.save(previous)
            if restore:
                await self.audit(pending, OutcomeResult.REJECTED, blocked, dispatch_now, latest)
            return OutcomeResult.REJECTED, blocked, False
        service = "disable_export_limit" if watts is None else "set_export_limit"
        data: dict[str, Any] = {"entity_id": state["identity"]["entity_id"]}
        if watts is not None:
            data["limit_watts"] = watts
        if restore:
            data["slew_rate"] = pending["slew_rate"]
        try:
            await async_call_device_service(self.hass, "enphase_ev", service, data)
        except DeviceTargetUnavailable:
            await self.save(previous)
            if restore:
                await self.audit(pending, OutcomeResult.REJECTED, "export_limit_entity_unavailable", now, {})
            return OutcomeResult.REJECTED, "export_limit_entity_unavailable", False
        except asyncio.CancelledError:
            await self.audit(pending, OutcomeResult.PENDING, "export_limit_pending", now, observed)
            raise
        except ServiceValidationError as err:
            state.pop("pending")
            reason = {
                "export_limit_disabled": "export_limit_service_rejected:controls_disabled",
                "export_limit_session_expired": "export_limit_service_rejected:installer_session_expired",
                "export_limit_unavailable": "export_limit_service_rejected:installer_or_gateway_unavailable",
            }.get(str(err.translation_key or ""), "export_limit_service_rejected")
            state["pause"] = reason
            await self.save(state)
            if restore:
                await self.audit(pending, OutcomeResult.FAILED, reason, now, observed)
            return OutcomeResult.FAILED, reason, True
        except Exception:  # An accepted but timed-out write remains uncertain.
            pass
        await self.audit(pending, OutcomeResult.PENDING, "export_limit_pending", now, observed)
        await self.reconcile(datetime.now(UTC))
        return OutcomeResult.PENDING, "export_limit_pending", True

    async def restore(self, now: datetime) -> tuple[OutcomeResult, str, bool]:
        """Retain the baseline until a gateway confirms its exact restoration."""
        state = self.state
        if not state.get("baseline"):
            return OutcomeResult.SKIPPED, "export_limit_not_owned", False
        state["restoring"] = True
        await self.save(state)
        issue = await self.reconcile(now)
        state = self.state
        if not state:
            return OutcomeResult.RESTORED, "export_limit_restored", False
        if issue:
            return OutcomeResult.PENDING, issue, False
        if not state.get("baseline"):
            return OutcomeResult.SKIPPED, "export_limit_not_owned", False
        observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
        if issue:
            return OutcomeResult.PENDING, issue, False
        baseline = state["baseline"]
        if all(observed[key] == baseline[key] for key in ("watts", "slew_rate")):
            await self.save({})
            return OutcomeResult.RESTORED, "export_limit_restored", False
        action = PlanAction(
            "restore_export_limit",
            "manual",
            now,
            now + READBACK_MAX_AGE,
            ActionAsset.ENPHASE_EXPORT_LIMIT,
            ActionKind.RESTORE_EXPORT_LIMIT,
            baseline,
            [],
            [],
            None,
            1.0,
        )
        result, reason, sent = await self.dispatch(state, action, baseline["watts"], now, observed, restore=True)
        if result == OutcomeResult.REJECTED:
            return OutcomeResult.PENDING, reason, sent
        if result == OutcomeResult.PENDING and not self.state:
            return OutcomeResult.RESTORED, "export_limit_restored", sent
        return result, reason, sent
