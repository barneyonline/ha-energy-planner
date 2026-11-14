"""Enphase Export Limit feedback and durable, asynchronous control transaction."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientResponseError, ServerDisconnectedError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.hass_dict import HassKey

from .action_limits import EXPORT_ACTION_CAP_REACHED, action_budget, budget_history
from .adapter_helpers import DEVICE_SERVICE_TIMEOUT_SECONDS, DeviceTargetUnavailable, async_call_device_service
from .const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_DRY_RUN,
    CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED,
    CONF_ENPHASE_EXPORT_LIMIT_ENTITY,
    CONF_PLANNER_ENABLED,
)
from .export_limit_cooldown import (
    MIN_CHANGE_SECONDS,
    control_key,
    cooldown_status,
    interval,
    next_eligible,
    normalized_record,
    prune_inactive_records,
    same_control,
    valid_stamp,
)
from .export_limit_policy import (
    EXPORT_ASSET,
    READBACK_MAX_AGE,
    ExportTariffCache,
    aware_time,
    confirmed_feedback,
    tariff_blocks,
)
from .export_limit_policy import (
    GATEWAY_UNVERIFIED as GATEWAY_UNVERIFIED,
)
from .models import ActionAsset, ActionKind, ActionOutcome, OutcomeResult, PlanAction
from .safety import parse_production_state

_TARIFF_CACHE: HassKey[ExportTariffCache] = HassKey("ha_energy_planner_export_tariff")

UNAVAILABLE_REJECTION = "export_limit_service_rejected:installer_or_gateway_unavailable"
UPSTREAM_CONTROLS_DISABLED = "export_limit_upstream_controls_disabled"
INSTALLER_FORM_MISMATCH = "export_limit_service_rejected:installer_form_mismatch"
INSTALLER_SESSION_EXPIRED = "export_limit_service_rejected:installer_session_expired"
INSTALLER_WRITE_REJECTED = "export_limit_service_rejected"
GATEWAY_CHANGED = "export_limit_gateway_changed"
COMPANION_UPGRADE_REQUIRED = "export_limit_companion_upgrade_required"
_UNSENT_GATEWAY_REJECTIONS = {
    "export_limit_changed": "export_limit_target_changed",
    "export_limit_pending": "export_limit_pending",
    "export_limit_mismatch": INSTALLER_FORM_MISMATCH,
}
RETRY_DEFERRED_REASONS = {
    EXPORT_ACTION_CAP_REACHED,
    "external_export_limit_changed",
    "export_limit_already_desired",
    "export_limit_change_rate_limited",
    "export_limit_entity_unavailable",
    "export_limit_pending",
    "export_limit_retry_interrupted",
    "export_limit_retry_not_sent",
    "export_limit_retry_readback_changed",
    "export_limit_target_changed",
    "export_tariff_changed",
    "export_tariff_expired",
}
MAX_RETRY_ATTEMPTS = 3
RETRY_INTERVAL = timedelta(seconds=MIN_CHANGE_SECONDS)
_OPERATOR_PAUSES = {
    UNAVAILABLE_REJECTION,
    UPSTREAM_CONTROLS_DISABLED,
    INSTALLER_WRITE_REJECTED,
    "export_limit_service_rejected:controls_disabled",
    INSTALLER_FORM_MISMATCH,
    INSTALLER_SESSION_EXPIRED,
    "export_limit_unsupported",
}


def gateway_identity_issue(saved: str | None, observed: str | None) -> str | None:
    """Require agreement once either side identifies the gateway."""
    if saved == observed:
        return None
    return GATEWAY_CHANGED if saved and observed else GATEWAY_UNVERIFIED


def owned_gateway_issue(ownership: dict[str, Any], observed: dict[str, Any]) -> str | None:
    """Compare feedback with the gateway that owns the baseline or request."""
    gateway = ownership.get("gateway_identity")
    if gateway or any(ownership.get(key) for key in ("pending", "baseline", "retry")):
        return gateway_identity_issue(gateway, observed.get("gateway_identity"))
    return None


class RetryWriteDeferred(ServiceValidationError):
    """Planner withdrew authority at the provider's pre-write boundary."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def preserve_retry_on_reload(state: dict[str, Any], target: dict[str, str] | None) -> bool:
    """Keep a same-target operator blocker as well as a resumable retry."""
    if state.get("retry", {}).get("state") == "operator_required" and state.get("pause"):
        identity = state.get("identity")
        return bool(target and identity and same_control(identity, target))
    return resumable_retry(state, target)


RetryApply = Callable[[int | None, Callable[[], None], datetime | None], Awaitable[object]]


def resumable_retry(state: dict[str, Any], target: dict[str, str] | None) -> bool:
    """Return whether a recovery episode can continue after a lifecycle handoff."""
    identity = state.get("identity")
    if (not target or not identity or not same_control(identity, target)
            or state.get("restoring") or state.get("pending")):
        return False
    retry = state.get("retry")
    if not isinstance(retry, dict):
        return False
    attempts = retry.get("attempts")
    if type(attempts) is not int or not 0 <= attempts <= MAX_RETRY_ATTEMPTS:
        return False
    if retry.get("state") == "ready":
        return not state.get("pause")
    return (
        state.get("pause") == UNAVAILABLE_REJECTION
        and aware_time(retry.get("next_attempt_at")) is not None
        and (retry.get("state") == "checking" or (
            retry.get("state") == "scheduled" and attempts < MAX_RETRY_ATTEMPTS
        ))
    )


def _temporary_rejection(error: ServiceValidationError) -> bool:
    """Recognize Enphase's explicitly chained pre-write availability errors."""
    cause = error.__cause__
    return (
        error.translation_domain == "enphase_ev"
        and error.translation_key == "export_limit_unavailable"
        and (
            isinstance(cause, (TimeoutError, ServerDisconnectedError))
            or isinstance(cause, ClientResponseError) and cause.status in {502, 503, 504}
        )
    )


def installer_runtime(hass: HomeAssistant, identity: dict[str, str]) -> Any:
    """Find the exact loaded Enphase entry's optional read-only preparation hook."""
    entry = hass.config_entries.async_get_entry(identity["config_entry_id"])
    if (entry is None or entry.domain != "enphase_ev"
            or entry.state is not ConfigEntryState.LOADED or entry.disabled_by is not None):
        return None
    coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
    return getattr(coordinator, "export_limit_runtime", None)


def installer_entry_transitioning(hass: HomeAssistant, identity: dict[str, str]) -> bool:
    """Identify a mapped Enphase entry that is temporarily between runtimes."""
    entry = hass.config_entries.async_get_entry(identity["config_entry_id"])
    return bool(entry is not None and entry.domain == "enphase_ev" and entry.disabled_by is None
                and entry.state in {ConfigEntryState.UNLOAD_IN_PROGRESS, ConfigEntryState.SETUP_IN_PROGRESS})


async def probe_installer(runtime: Any) -> tuple[Any, str | None, bool]:
    """Check the session, gateway and installer form used by Enphase Configure."""
    if runtime is None:
        # The mapped entry may still be loading after a restart.
        return None, "export_limit_retry_readiness_check_unavailable", False
    prepare = getattr(runtime, "async_prepare", None)
    if (not callable(prepare) or not callable(getattr(runtime, "async_apply_guarded", None))
            or getattr(runtime, "verifies_export_limit_gateway", False) is not True):
        return None, COMPANION_UPGRADE_REQUIRED, True
    try:
        async with asyncio.timeout(DEVICE_SERVICE_TIMEOUT_SECONDS):
            snapshot = await prepare()
    except TimeoutError:
        return None, "export_limit_retry_readiness_timeout", False
    except ServiceValidationError as err:
        if _temporary_rejection(err):
            return None, UNAVAILABLE_REJECTION, False
        if err.translation_key == "export_limit_session_expired":
            return None, "export_limit_service_rejected:installer_session_expired", True
        if err.translation_key == "export_limit_disabled":
            return None, "export_limit_service_rejected:controls_disabled", True
        return None, "export_limit_retry_readiness_rejected", True
    except Exception:
        return None, "export_limit_retry_readiness_check_failed", True
    if getattr(snapshot, "supported", None) is not True:
        return None, "export_limit_unsupported", True
    if getattr(runtime, "enabled", None) is not True:
        return None, "export_limit_service_rejected:controls_disabled", True
    if getattr(runtime, "pending", False) is not None:
        return None, "export_limit_pending", False
    if not isinstance(getattr(snapshot, "enabled", None), bool) or not hasattr(snapshot, "slew"):
        return None, "export_limit_retry_readiness_check_unavailable", True
    return snapshot, None, False


def retry_status(state: dict[str, Any], *, running: bool) -> dict[str, Any]:
    """Expose bounded retry progress without provider exception details."""
    retry = dict(state.get("retry", {}))
    if not retry and state.get("pause") in _OPERATOR_PAUSES:
        retry = {"state": "operator_required", "reason": state["pause"], "attempts": 0, "next_attempt_at": None}
    if retry:
        retry["max_attempts"] = MAX_RETRY_ATTEMPTS
        if state.get("pending"):
            retry.update(state="awaiting_confirmation", reason=state.get("pause") or "export_limit_pending",
                         next_attempt_at=None)
        elif retry["state"] == "checking" and not running:
            # A process can stop after reserving the check. Its next deadline
            # remains durable, but there is no active installer request.
            retry["state"] = "scheduled"
        if retry["state"] == "operator_required":
            if retry["reason"] == COMPANION_UPGRADE_REQUIRED:
                retry["operator_action"] = (
                    "Upgrade Enphase Energy to a release that verifies the active gateway for Export Limit "
                    "readback and supports guarded apply. Then resume Export Limit control."
                )
            else:
                retry["operator_action"] = "Check Enphase Export Limit in Configure, then resume Export Limit control."
    return retry


def target_identity(hass: HomeAssistant, entity: str) -> dict[str, str] | None:
    """Require the upstream Export Limit identity, never a default site."""
    if not entity:
        return None
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
    readback = state if sensor == entity else hass.states.get(sensor) if sensor else None
    observed, issue = confirmed_feedback(readback, now)
    observed["identity"] = identity
    return observed, issue


def live_owned_gateway_issue(
    hass: HomeAssistant, entity: str | None, ownership: dict[str, Any], now: datetime,
) -> str | None:
    """Check ownership against current feedback without parsing tariffs."""
    if not entity or not any(ownership.get(key) for key in ("gateway_identity", "pending", "baseline", "retry")):
        return None
    observed, issue = feedback(hass, entity, now)
    if issue is None:
        return owned_gateway_issue(ownership, observed)
    return GATEWAY_UNVERIFIED if issue == GATEWAY_UNVERIFIED else None


def current_tariff_blocks(
    hass: HomeAssistant, entity: str | None, options: dict[str, Any], now: datetime,
) -> tuple[list[dict[str, Any]], str | None]:
    """Recheck the live tariff while reusing validation for the same HA State."""
    state = hass.states.get(entity) if entity else None
    if not isinstance(state, State):
        return tariff_blocks(state, now, options)
    if (cache := hass.data.get(_TARIFF_CACHE)) is None:
        cache = hass.data[_TARIFF_CACHE] = ExportTariffCache()
    return tariff_blocks(state, now, options, cache=cache)


def evidence(hass: HomeAssistant, data: dict[str, Any], options: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Build price-only readiness without depending on the energy optimizer."""
    observed, issue = feedback(hass, data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
    feedback_ready = issue is None and not observed.get("pending")
    entity = str(data[CONF_AMBER_EXPORT_PRICE]) if data.get(CONF_AMBER_EXPORT_PRICE) else None
    blocks, tariff_issue = current_tariff_blocks(hass, entity, options, now)
    service_issue = None
    if not all(hass.services.has_service("enphase_ev", name) for name in ("set_export_limit", "disable_export_limit")):
        service_issue = "export_limit_services_unavailable"
    issue = issue or tariff_issue or service_issue
    if observed.get("pending"):
        issue = issue or "export_limit_pending"
    return {"ready": issue is None, "reason": issue, "feedback": observed,
            "feedback_ready": feedback_ready, "blocks": blocks}


class ExportLimitControl:
    """Persist once, dispatch once, and reconcile subsequent gateway readback."""

    def __init__(
        self, hass: HomeAssistant, store: Any, data: dict[str, Any],
        before_dispatch: Callable[[], str | None] | None = None,
        *, options: dict[str, Any] | None = None,
    ) -> None:
        self.hass, self.store, self.data = hass, store, data
        self.before_dispatch = before_dispatch
        self.options = options or {}

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

    async def prepare_cooldown(self, now: datetime) -> dict[str, Any] | None:
        """Persist migration/clock recovery even when control is otherwise blocked."""
        identity = target_identity(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY, ""))
        if identity is None:
            return None
        limits = dict(self.store.data.get("command_rate_limits", {}))
        state = self.state
        record = normalized_record(limits, identity, state, now)
        if record.get("clock_recovered_at") == now.isoformat():
            await self.store.async_reanchor_export_attempts(now)
        key = control_key(identity)
        if key not in limits:
            owned = state.get("identity")
            prune_inactive_records(limits, {key, control_key(owned)} if owned else {key}, now)
        if limits.get(key) != record:
            limits[key] = record
            await self.store.async_save_command_rate_limits(limits)
        retry = state.get("retry")
        if (isinstance(retry, dict) and (deadline := aware_time(retry.get("next_attempt_at"))) is not None
                and deadline > now + RETRY_INTERVAL):
            state["retry"] = {**retry, "next_attempt_at": (now + RETRY_INTERVAL).isoformat()}
            await self.save(state)
            await self.store.async_flush()
        return record

    async def save_timing(self, identity: dict[str, str], record: dict[str, Any], *, flush: bool = True) -> None:
        limits = dict(self.store.data.get("command_rate_limits", {}))
        limits[control_key(identity)] = record
        await self.store.async_save_command_rate_limits(limits)
        if flush:
            await self.store.async_flush()

    def cooldown(self, now: datetime) -> dict[str, Any]:
        return cooldown_status(self.store.data, target_identity(
            self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY, "")), self.options, now)

    def retry_deadline(self, now: datetime) -> datetime | None:
        """Respect both the paced check and the durable command cooldown."""
        state = self.state
        if state.get("pending") or state.get("restoring"):
            return None
        retry = state.get("retry")
        if not isinstance(retry, dict) or type(retry.get("attempts")) is not int:
            return None
        ready = retry.get("state") == "ready" and not state.get("pause")
        deadline = aware_time(retry.get("next_attempt_at"))
        if ready and deadline is None:
            deadline = now
        if not ready and (state.get("pause") != UNAVAILABLE_REJECTION or deadline is None):
            return None
        assert deadline is not None
        if deadline > now + RETRY_INTERVAL:
            return now  # Wake to repair the persisted clocks before checking eligibility.
        # Expire an interrupted final check even if the allowance is closed.
        if ready or retry.get("rechecking_ready") or retry["attempts"] < MAX_RETRY_ATTEMPTS:
            if (allowance := self._retry_allowance_at(now)) is not None:
                deadline = max(deadline, allowance)
        cooldown = aware_time(self.cooldown(now).get("next_eligible_change_at"))
        return max(deadline, cooldown) if cooldown else deadline

    def _retry_allowance_at(self, now: datetime) -> datetime | None:
        """Defer a check when the current tariff would require a capped write."""
        budget = action_budget(budget_history(self.store.data), self.options, now, EXPORT_ASSET)
        allowance = aware_time(budget["allowance_available_at"])
        if allowance is not None:
            current = evidence(self.hass, self.data, self.options, now)
            if current["ready"] and current["blocks"][0]["watts"] != current["feedback"]["watts"]:
                return allowance
        return None

    async def claim_retry(self, now: datetime, *, pending: bool = False) -> dict[str, Any] | None:
        """Durably consume a check before any provider I/O, including after restart."""
        if self.retry_deadline(now) is None:
            return None
        # A timer can wake before the planner has persisted clock repair.
        await self.prepare_cooldown(now)
        deadline = self.retry_deadline(now)
        if deadline is None or now < deadline:
            return None
        state = self.state
        retry = state["retry"]
        ready = retry["state"] == "ready" or bool(retry.get("rechecking_ready"))
        if not ready and retry["attempts"] >= MAX_RETRY_ATTEMPTS:
            await self.finish_retry(retry, "export_limit_retry_exhausted", terminal=True)
            return None
        if pending:
            # A settled readback is required before checking installer access.
            # Waiting for an existing request does not spend an installer check.
            if ready:
                retry = dict(retry)
                retry.pop("rechecking_ready", None)
                state.pop("pause", None)
            await self.save({**state, "retry": {
                **retry, "state": "ready" if ready else "scheduled", "reason": "export_limit_pending",
                "next_attempt_at": (now + RETRY_INTERVAL).isoformat(),
            }})
            await self.store.async_flush()
            return None
        retry = {**retry, "attempts": retry["attempts"] if ready else retry["attempts"] + 1, "state": "checking",
                 "next_attempt_at": (now + RETRY_INTERVAL).isoformat()}
        if ready:
            retry["rechecking_ready"] = True
        try:
            await self.save({**state, "retry": retry, "pause": UNAVAILABLE_REJECTION})
            await self.store.async_flush()
        except BaseException:
            # Store updates its in-memory ownership before the write completes.
            await self.save(state)
            await self.store.async_flush()
            raise
        return retry

    async def finish_retry(
        self, claim: dict[str, Any], reason: str | None, *, terminal: bool = False,
        awaiting_startup_arm: bool = False, refund: bool = False, flush: bool = True,
    ) -> None:
        """Release a settled no-op or retain a paced or terminal blocker."""
        state = self.state
        if state.get("retry") != claim or state.get("pause") != UNAVAILABLE_REJECTION or state.get("pending"):
            return
        retry = {key: value for key, value in claim.items() if key != "rechecking_ready"}
        if refund and not claim.get("rechecking_ready"):
            retry["attempts"] -= 1
        if reason is None:
            state.pop("pause")
            if awaiting_startup_arm:
                state["retry"] = {**retry, "state": "ready", "reason": "export_limit_installer_ready",
                                  "next_attempt_at": None}
            else:
                state.pop("retry")
        else:
            if not terminal and reason in RETRY_DEFERRED_REASONS:
                # A deferral leaves the check available once the blocker clears.
                state["retry"] = self.defer_retry(claim, reason, datetime.now(UTC))
                if state["retry"]["state"] == "ready":
                    state.pop("pause")
            elif terminal or claim["attempts"] >= MAX_RETRY_ATTEMPTS:
                state["pause"] = reason if terminal else "export_limit_retry_exhausted"
                state["retry"] = {**retry, "state": "operator_required", "reason": reason, "next_attempt_at": None}
            else:
                state["retry"] = {**retry, "state": "scheduled", "reason": reason,
                                  "next_attempt_at": (datetime.now(UTC) + RETRY_INTERVAL).isoformat()}
        await self.save(state)
        if flush:
            await self.store.async_flush()

    async def finish_unsent_retry(
        self, claim: dict[str, Any], reason: str, readback_since: float, *, flush: bool = True,
    ) -> None:
        """Settle a rejected write with no command charge."""
        state = self.state
        if state.get("retry") != claim or state.get("pause") != UNAVAILABLE_REJECTION or state.get("pending"):
            return
        if reason in {
            "export_limit_already_desired", "external_export_limit_changed", "export_limit_retry_readback_changed",
        }:
            now = datetime.now(UTC)
            current = evidence(self.hass, self.data, self.options, now)
            observed = current["feedback"]
            if (current["ready"] and same_control(observed["identity"], state["identity"])
                    and observed["last_readback"] >= readback_since):
                gateway_issue = gateway_identity_issue(state.get("gateway_identity"), observed.get("gateway_identity"))
                if gateway_issue:
                    reason = gateway_issue
                elif observed["watts"] == current["blocks"][0]["watts"]:
                    if state.get("expected") and any(
                        state["expected"].get(key) != observed[key] for key in ("watts", "slew_rate")
                    ):
                        await self.adopt_external_feedback(observed, now)
                    await self.finish_retry(claim, None, flush=flush)
                    return
        terminal = reason in {
            UPSTREAM_CONTROLS_DISABLED, INSTALLER_SESSION_EXPIRED, INSTALLER_FORM_MISMATCH, INSTALLER_WRITE_REJECTED,
            GATEWAY_CHANGED, GATEWAY_UNVERIFIED,
        }
        await self.finish_retry(claim, reason, terminal=terminal, refund=terminal, flush=flush)

    @staticmethod
    def defer_retry(retry: dict[str, Any], reason: str, now: datetime) -> dict[str, Any]:
        """Return a paced retry for a check invalidated before a write."""
        retry = dict(retry)
        ready = bool(retry.pop("rechecking_ready", False))
        return {**retry, "attempts": retry["attempts"] - int(not ready), "state": "ready" if ready else "scheduled",
                "reason": reason,
                "next_attempt_at": (now + RETRY_INTERVAL).isoformat()}

    async def audit(
        self, pending: dict[str, Any], result: OutcomeResult, reason: str, now: datetime, observed: dict[str, Any],
        *, desired_state: dict[str, Any] | None = None, command_sent: bool | None = None,
    ) -> None:
        await self.store.async_add_outcome(
            ActionOutcome(
                # Manual restorations share a lifecycle label. The persisted
                # request time distinguishes cycles, including legacy pending
                # requests. Clock repair freezes that identity before changing
                # the request's reconciliation timing.
                action_id=pending.get("audit_action_id") or (
                    f"{pending['action_id']}-{pending['sent_at']}" if pending["restore"] else pending["action_id"]),
                attempted_at=now,
                result=result,
                reason=reason,
                pre_state=pending.get("pre_state", {}),
                post_state=observed,
                plan_id=pending["plan_id"],
                asset=EXPORT_ASSET,
                kind=pending["kind"],
                service_target=pending["identity"]["entity_id"],
                desired_state=desired_state if desired_state is not None else {"watts": pending["watts"]},
                command_sent=command_sent,
            )
        )

    async def record_pending(
        self, state: dict[str, Any], pending: dict[str, Any], observed: dict[str, Any],
        *, reservation: dict[str, Any] | None = None,
    ) -> None:
        """Persist the attempt debit and its receipt in the same Store generation."""
        pending["attempt_recorded"] = True
        async with self.store.async_delay_save():
            if reservation is not None:
                await self.save_timing(state["identity"], reservation, flush=False)
            await self.audit(
                pending, OutcomeResult.PENDING, "export_limit_pending",
                datetime.fromtimestamp(pending["sent_at"], UTC), observed,
            )
            await self.save(state)

    async def adopt_external_feedback(self, observed: dict[str, Any], now: datetime) -> None:
        """Retire superseded ownership without clearing unrelated failure pauses."""
        previous = self.state
        pending = previous.get("pending")
        if pending and pending.get("confirmation_not_before") is not None:
            # An intervening writer can retire ownership without proving when
            # our uncertain watts request took effect. Keep a separate safety
            # wait, never an APPLIED/RESTORED outcome or Planner confirmation.
            record = await self.prepare_cooldown(now)
            assert record is not None
            if record.get("external_settlement_request") != pending["sent_at"]:
                record.update(checked_at=now.timestamp(), external_settled_at=now.timestamp(),
                              external_settlement_request=pending["sent_at"])
                await self.save_timing(observed["identity"], record)
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
        retry = previous.get("retry")
        if retry and (adopted.get("pause") or retry.get("state") == "ready"):
            adopted["retry"] = retry
            if previous.get("gateway_identity"):
                adopted["gateway_identity"] = previous["gateway_identity"]
        await self.save(adopted)

    async def _recover_pending(self, state: dict[str, Any], pending: dict[str, Any], now: datetime) -> None:
        """Repair the clock and account for an uncertain write after restart."""
        if not valid_stamp(pending.get("sent_at"), now):
            # A clock rollback or corrupt old request time cannot grant another
            # write. Retain uncertainty, requiring a new matching read first.
            audit_action_id = pending.get("audit_action_id") or (
                f"{pending['action_id']}-{pending.get('sent_at')}" if pending["restore"] else pending["action_id"])
            pending = {**pending, "audit_action_id": audit_action_id,
                       "sent_at": now.timestamp(), "request_clock_recovered": True}
            state["pending"] = pending
            await self.save(state)
        if not valid_stamp(pending.get("confirmation_not_before"), now):
            pending = {**pending, "confirmation_not_before": now.timestamp()}
            state["pending"] = pending
            await self.save(state)
        if pending.get("attempt_recorded") is False:
            # A crash after the durable reservation leaves the write uncertain.
            await self.record_pending(state, pending, pending["pre_state"])

    def _owned_feedback(self, state: dict[str, Any], now: datetime) -> tuple[dict[str, Any], str | None]:
        """Read feedback that can be attributed to this ownership."""
        observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
        if "identity" in observed and not same_control(observed["identity"], state["identity"]):
            return observed, "export_limit_target_changed"
        return observed, issue or owned_gateway_issue(state, observed)

    async def reconcile(self, now: datetime) -> str | None:
        """Resolve durable commands and reconcile external changes without latching a pause."""
        await self.prepare_cooldown(now)
        state = self.state
        if not state:
            return None
        if pending := state.get("pending"):
            await self._recover_pending(state, pending, now)
        observed, issue = self._owned_feedback(state, now)
        if not issue and state["identity"] != observed["identity"]:
            state["identity"] = observed["identity"]
            await self.save(state)
            # The alias save can yield while the mapped gateway changes.
            now = datetime.now(UTC)
            observed, issue = self._owned_feedback(state, now)
        if issue == "export_limit_target_changed":
            return issue
        pending = state.get("pending")
        if issue:
            if pending and now.timestamp() - pending["sent_at"] >= READBACK_MAX_AGE.total_seconds():
                await self.unconfirmed(state, pending, now, observed)
                return "export_limit_unconfirmed"
            if issue == "export_limit_unsupported" and not pending:
                state["pause"] = issue
                if retry := state.get("retry"):
                    state["retry"] = {**retry, "state": "operator_required", "reason": issue, "next_attempt_at": None}
                await self.save(state)
                if retry:
                    await self.store.async_flush()
            return issue
        if state.get("pause") == "external_export_limit_conflict" and not pending:
            if observed["pending"]:
                return "export_limit_pending"
            await self.adopt_external_feedback(observed, now)
            return None
        if pending:
            matches = (
                not observed["pending"]
                and observed["watts"] == pending["watts"]
                and observed["last_readback"] >= max(pending["sent_at"], pending["confirmation_not_before"])
                and (pending.get("slew_rate") is None or observed["slew_rate"] == pending["slew_rate"])
            )
            if matches:
                if not pending["restore"] and observed["slew_rate"] != pending["pre_state"]["slew_rate"]:
                    await self.audit(pending, OutcomeResult.REJECTED, "external_export_limit_changed", now, observed)
                    await self.adopt_external_feedback(observed, now)
                    return self.state.get("pause")
                record = normalized_record(self.store.data.get("command_rate_limits", {}),
                                           observed["identity"], state, now)
                async with self.store.async_delay_save():
                    if record.get("confirmation_request") != pending["sent_at"]:
                        # API time is a read completion, not physical change time.
                        # Start at our first observation, once per request.
                        record.update(checked_at=now.timestamp(), confirmed_at=now.timestamp(),
                                      confirmation_request=pending["sent_at"])
                        await self.save_timing(observed["identity"], record, flush=False)
                    await self.audit(
                        pending,
                        OutcomeResult.RESTORED if pending["restore"] else OutcomeResult.APPLIED,
                        "export_limit_restored" if pending["restore"] else "export_limit_confirmed",
                        now,
                        observed,
                    )
                    if pending["restore"]:
                        await self.save({})
                    else:
                        state.pop("pending")
                        state.pop("retry", None)
                        state["expected"] = {"watts": observed["watts"], "slew_rate": observed["slew_rate"]}
                        await self.save(state)
                await self.store.async_flush()
                if pending["restore"]:
                    return None
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
        if state.get("retry", {}).get("state") == "ready" and not state.get("pause") and not state.get("restoring"):
            current = evidence(self.hass, self.data, self.options, now)
            if current["ready"] and current["blocks"][0]["watts"] == observed["watts"]:
                state.pop("retry")
                await self.save(state)
        return self.state.get("pause")

    async def unconfirmed(
        self, state: dict[str, Any], pending: dict[str, Any], now: datetime, observed: dict[str, Any]
    ) -> None:
        """Pause once even if readback itself has become unavailable or stale."""
        if state.get("pause") != "export_limit_unconfirmed":
            async with self.store.async_delay_save():
                await self.audit(pending, OutcomeResult.FAILED, "export_limit_unconfirmed", now, observed)
                state["pause"] = "export_limit_unconfirmed"
                await self.save(state)

    async def resume(self) -> None:
        """Clear a pause or retire ownership of a verified replaced gateway."""
        state = self.state
        now = datetime.now(UTC)
        entity = self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY)
        observed, issue = feedback(self.hass, entity, now)
        if state and "identity" in observed and not same_control(observed["identity"], state["identity"]):
            return
        gateway = state.get("gateway_identity")
        owned_identity = state.get("identity")
        replacement = observed.get("gateway_identity")
        can_retire = (
            not issue and replacement and replacement != gateway
            and owned_identity and same_control(observed["identity"], owned_identity)
        )
        if pending := state.get("pending"):
            if owned_identity and can_retire and (gateway or state.get("pause") == "export_limit_unconfirmed"):
                if observed["pending"]:
                    upstream = installer_runtime(self.hass, owned_identity)
                    retire = getattr(upstream, "async_retire_replaced_intent", None)
                    if not callable(retire):
                        return
                    try:
                        async with asyncio.timeout(DEVICE_SERVICE_TIMEOUT_SECONDS):
                            if not await retire(replacement, original_gateway=gateway):
                                return
                    except (ServiceValidationError, TimeoutError):
                        return
                    now = datetime.now(UTC)
                    observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
                    if (issue or observed["pending"] or observed.get("gateway_identity") != replacement
                            or not same_control(observed["identity"], owned_identity) or self.state != state):
                        return
                # Keep the original reservation and account for an uncertain
                # dispatch without treating the replacement as confirmation.
                async with self.store.async_delay_save():
                    await self._recover_pending(state, pending, now)
                    await self.unconfirmed(state, state["pending"], now, observed)
                    await self.save({})
                await self.store.async_flush()
            return
        if can_retire and not observed["pending"]:
            await self.save({})
            return
        state.pop("pause", None)
        state.pop("retry", None)
        await self.save(state if state.get("baseline") else {})

    async def execute(
        self, action: PlanAction, now: datetime, *, retry: dict[str, Any] | None = None,
        retry_apply: RetryApply | None = None,
    ) -> tuple[OutcomeResult, str, bool]:
        """Execute a validated tariff transition without blocking for confirmation."""
        issue = await self.reconcile(now)
        if retry is not None and (issue != UNAVAILABLE_REJECTION or self.state.get("retry") != retry):
            return OutcomeResult.REJECTED, issue or "export_limit_retry_interrupted", False
        if issue and retry is None:
            return OutcomeResult.REJECTED, issue, False
        if retry is None and self.state.get("retry", {}).get("state") == "ready":
            return OutcomeResult.REJECTED, "export_limit_retry_recheck_required", False
        now = datetime.now(UTC)
        observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
        if issue or observed.get("pending"):
            return OutcomeResult.REJECTED, issue or "export_limit_pending", False
        if self.state.get("restoring"):
            return OutcomeResult.REJECTED, "export_limit_restoration_pending", False
        desired = action.desired_state["watts"]
        if observed["watts"] == desired:
            return OutcomeResult.SKIPPED, "export_limit_already_desired", False
        state = self.state or {"identity": observed["identity"]}
        if not state.get("baseline") and (gateway := observed.get("gateway_identity")):
            state["gateway_identity"] = gateway
        state.setdefault("baseline", {"watts": observed["watts"], "slew_rate": observed["slew_rate"]})
        if retry is not None:
            state.pop("pause")
        return await self.dispatch(state, action, desired, now, observed, retry_apply=retry_apply)

    async def dispatch(
        self,
        state: dict[str, Any],
        action: PlanAction,
        watts: int | None,
        now: datetime,
        observed: dict[str, Any],
        *,
        restore: bool = False,
        retry_apply: RetryApply | None = None,
    ) -> tuple[OutcomeResult, str, bool]:
        """Durability precedes the service boundary, including compensation."""
        timing = await self.prepare_cooldown(now)
        assert timing is not None
        deadline = next_eligible(timing, self.options)
        if deadline is not None and now < deadline:
            return OutcomeResult.REJECTED, "export_limit_change_rate_limited", False
        previous = self.state
        pending = {
            "action_id": action.action_id,
            "plan_id": action.plan_id,
            "kind": str(action.kind),
            "identity": state["identity"],
            "watts": watts,
            "sent_at": now.timestamp(),
            # None on disk means the process may have died before dispatch.
            # Recovery then requires a new settings read after reconciliation.
            "confirmation_not_before": None,
            "attempt_recorded": False,
            "restore": restore,
            "pre_state": observed,
            "slew_rate": state["baseline"]["slew_rate"] if restore else None,
        }
        state["pending"] = pending
        reservation = {**timing, "attempted_at": now.timestamp(), "checked_at": now.timestamp(),
                       "interval_seconds": interval(self.options)}

        async def rollback_reservation(reason: str, external: dict[str, Any] | None = None) -> None:
            async with self.store.async_delay_save():
                await self.save_timing(state["identity"], timing, flush=False)
                if external is None or previous.get("retry"):
                    await self.save(previous)
                if external is not None:
                    await self.adopt_external_feedback(external, dispatch_now)
                if retry_apply is not None:
                    await self.finish_unsent_retry(previous["retry"], reason, observed["last_readback"], flush=False)
            await self.store.async_flush()

        try:
            async with self.store.async_delay_save():
                await self.save(state)
                await self.save_timing(state["identity"], reservation, flush=False)
            await self.store.async_flush()
        except BaseException:
            # The service boundary has not been entered. A failed/cancelled
            # persistence attempt cannot create an uncertain gateway request.
            await rollback_reservation("export_limit_retry_not_sent")
            raise
        dispatch_now = datetime.now(UTC)
        latest, blocked = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), dispatch_now)
        deadline = next_eligible(timing, self.options)
        if not blocked and deadline is not None and dispatch_now < deadline:
            blocked = "export_limit_change_rate_limited"
        if not blocked and not same_control(latest["identity"], state["identity"]):
            blocked = "export_limit_target_changed"
        if not blocked:
            blocked = gateway_identity_issue(state.get("gateway_identity"), latest.get("gateway_identity"))
        if not blocked and latest["pending"]:
            blocked = "export_limit_pending"
        if not blocked and any(latest[key] != observed[key] for key in ("watts", "slew_rate")):
            blocked = "external_export_limit_changed"
        if not restore:
            if not action.execute_not_before <= dispatch_now < action.execute_not_after:
                blocked = "export_tariff_expired"
            if self.before_dispatch and (not blocked or (
                retry_apply is not None and blocked == "export_limit_entity_unavailable"
            )):
                guard_issue = self.before_dispatch()
                blocked = guard_issue or blocked

        if blocked:
            await rollback_reservation(blocked, latest if blocked == "external_export_limit_changed" else None)
            if restore:
                await self.audit(pending, OutcomeResult.REJECTED, blocked, dispatch_now, latest)
            return OutcomeResult.REJECTED, blocked, False
        service = "disable_export_limit" if watts is None else "set_export_limit"
        data: dict[str, Any] = {"entity_id": state["identity"]["entity_id"]}
        if watts is not None:
            data["limit_watts"] = watts
        if restore:
            data["slew_rate"] = pending["slew_rate"]
        if state.get("gateway_identity"):
            data["expected_gateway"] = state["gateway_identity"]
        pending["confirmation_not_before"] = dispatch_now.timestamp()
        dispatch_reached = retry_apply is None

        def mark_dispatched() -> None:
            nonlocal dispatch_reached
            dispatch_reached = True
            sent_at = datetime.now(UTC).timestamp()
            pending.update(sent_at=sent_at, confirmation_not_before=sent_at)
            reservation.update(attempted_at=sent_at, checked_at=sent_at)

        try:
            if retry_apply is None:
                await async_call_device_service(self.hass, "enphase_ev", service, data)
            else:
                async with asyncio.timeout(DEVICE_SERVICE_TIMEOUT_SECONDS):
                    await retry_apply(watts, mark_dispatched, deadline)
        except DeviceTargetUnavailable:
            await rollback_reservation("export_limit_entity_unavailable")
            if restore:
                await self.audit(pending, OutcomeResult.REJECTED, "export_limit_entity_unavailable", now, {})
            return OutcomeResult.REJECTED, "export_limit_entity_unavailable", False
        except asyncio.CancelledError:
            if dispatch_reached:
                await self.record_pending(state, pending, observed, reservation=reservation)
            else:
                await rollback_reservation("export_limit_retry_not_sent")
            raise
        except ServiceValidationError as err:
            if (retry_apply is None and "expected_gateway" in data and err.translation_domain == "enphase_ev"
                    and (reason := _UNSENT_GATEWAY_REJECTIONS.get(err.translation_key or "")) is not None):
                # The companion raises these during locked preparation, before saving or sending the request.
                if reason == INSTALLER_FORM_MISMATCH:
                    previous.setdefault("identity", state["identity"])
                    previous["pause"] = reason
                await rollback_reservation(reason)
                if restore:
                    await self.audit(pending, OutcomeResult.REJECTED, reason, dispatch_now, latest, command_sent=False)
                return OutcomeResult.REJECTED, reason, False
            if not dispatch_reached:
                if isinstance(err, RetryWriteDeferred):
                    reason = err.reason
                elif _temporary_rejection(err):
                    # The installer check completed, but the provider became
                    # unavailable during apply. Only refund the unused write.
                    reason = UNAVAILABLE_REJECTION
                else:
                    reason = {
                        "export_limit_changed": "export_limit_retry_readback_changed",
                        "export_limit_pending": "export_limit_pending",
                        "export_limit_disabled": UPSTREAM_CONTROLS_DISABLED,
                        "export_limit_session_expired": INSTALLER_SESSION_EXPIRED,
                        "export_limit_mismatch": INSTALLER_FORM_MISMATCH,
                    }.get(err.translation_key or "", INSTALLER_WRITE_REJECTED)
                await rollback_reservation(reason)
                return OutcomeResult.REJECTED, reason, False
            if retry_apply is None:
                stamp = datetime.now(UTC).timestamp()
                reservation.update(attempted_at=stamp, checked_at=stamp)
            state.pop("pending")
            reason = {
                "export_limit_disabled": "export_limit_service_rejected:controls_disabled",
                "export_limit_session_expired": "export_limit_service_rejected:installer_session_expired",
                "export_limit_unavailable": UNAVAILABLE_REJECTION,
            }.get(str(err.translation_key or ""), "export_limit_service_rejected")
            state["pause"] = reason
            retry = state.get("retry")
            if not restore and _temporary_rejection(err):
                next_attempt = (datetime.now(UTC) + RETRY_INTERVAL).isoformat()
                if retry is None:
                    state.setdefault("expected", {"watts": observed["watts"], "slew_rate": observed["slew_rate"]})
                    retry = {"attempts": 0, "state": "scheduled", "reason": reason,
                             "next_attempt_at": next_attempt}
                elif retry["attempts"] >= MAX_RETRY_ATTEMPTS:
                    state["pause"] = "export_limit_retry_exhausted"
                    retry = {**retry, "state": "operator_required", "reason": reason, "next_attempt_at": None}
                else:
                    retry = {**retry, "state": "scheduled", "reason": reason,
                             "next_attempt_at": next_attempt}
            elif retry is not None:
                retry = {**retry, "state": "operator_required", "reason": reason, "next_attempt_at": None}
            if retry is not None:
                retry.pop("rechecking_ready", None)
                state["retry"] = retry
            # Publish the known rejection and retire pending intent together.
            async with self.store.async_delay_save():
                await self.save_timing(state["identity"], reservation, flush=False)
                await self.audit(
                    pending, OutcomeResult.FAILED, reason, datetime.fromtimestamp(pending["sent_at"], UTC), observed,
                    desired_state=None if restore else action.desired_state,
                )
                await self.save(state)
            if retry is not None:
                await self.store.async_flush()
            return OutcomeResult.FAILED, reason, True
        except Exception:
            if not dispatch_reached:
                await rollback_reservation("export_limit_retry_not_sent")
                return OutcomeResult.REJECTED, "export_limit_retry_not_sent", False
        else:
            if not dispatch_reached:
                await rollback_reservation("export_limit_already_desired")
                return OutcomeResult.SKIPPED, "export_limit_already_desired", False
        await self.record_pending(state, pending, observed, reservation=reservation)
        await self.reconcile(datetime.now(UTC))
        return OutcomeResult.PENDING, "export_limit_pending", True

    async def restore(self, now: datetime) -> tuple[OutcomeResult, str, bool]:
        """Retain the baseline until a gateway confirms its exact restoration."""
        state = self.state
        if not state.get("baseline"):
            return OutcomeResult.SKIPPED, "export_limit_not_owned", False
        if retry := state.get("retry"):
            state["retry"] = {**retry, "state": "operator_required", "next_attempt_at": None}
        state["restoring"] = True
        await self.save(state)
        reconciliation_issue = await self.reconcile(now)
        state = self.state
        if not state:
            return OutcomeResult.RESTORED, "export_limit_restored", False
        if reconciliation_issue in {GATEWAY_CHANGED, GATEWAY_UNVERIFIED}:
            return OutcomeResult.PENDING, reconciliation_issue, False
        if reconciliation_issue:
            active = (
                self.options.get(CONF_PLANNER_ENABLED) is True
                and self.options.get(CONF_DRY_RUN) is False
                and self.options.get(CONF_ENPHASE_EXPORT_LIMIT_CONTROL_ENABLED) is True
                and parse_production_state(self.store.data.get("production")).armed
            )
            # Stopping control may release an already-restored baseline. A
            # rejection pause still blocks any compensating service call.
            retry_paused = retry and not state.get("pending") and reconciliation_issue == state.get("pause")
            if active or not retry_paused:
                return OutcomeResult.PENDING, reconciliation_issue, False
        if not state.get("baseline"):
            return OutcomeResult.SKIPPED, "export_limit_not_owned", False
        observed, issue = feedback(self.hass, self.data.get(CONF_ENPHASE_EXPORT_LIMIT_ENTITY), now)
        if issue:
            return OutcomeResult.PENDING, issue, False
        if observed["pending"]:
            return OutcomeResult.PENDING, "export_limit_pending", False
        if not same_control(observed["identity"], state["identity"]):
            return OutcomeResult.PENDING, "export_limit_target_changed", False
        if gateway_issue := gateway_identity_issue(state.get("gateway_identity"), observed.get("gateway_identity")):
            return OutcomeResult.PENDING, gateway_issue, False
        baseline = state["baseline"]
        if all(observed[key] == baseline[key] for key in ("watts", "slew_rate")):
            await self.save({})
            return OutcomeResult.RESTORED, "export_limit_restored", False
        if reconciliation_issue:
            return OutcomeResult.PENDING, reconciliation_issue, False
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
