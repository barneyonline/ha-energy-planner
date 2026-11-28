"""Pure EV execution evidence, spending settlement, and allocation deadlines."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .action_limits import action_budget, counted_attempt
from .ev import ev_charging_state_proves_active
from .ev_policy import finite
from .vehicles import connection

MAX_EV_CAP_HISTORY = 8
MAX_NATIVE_STOP_SAMPLES = 256
EV_CAP_FEEDBACK_CLOCK_INVALID = "feedback_clock_invalid"
EV_RETIRED_CAP_KW = "retired_cap_kw"


def invalidate_ev_cap_feedback_clock(reservation: dict[str, Any]) -> None:
    """Keep reversible load evidence while retiring boundaries from the old clock."""
    reservation.pop("confirmed_power_at", None)
    pending = reservation.pop("pending_power_limit", None)
    proof = reservation.get("cap_reduction")
    # Retain the setting independently of its clock. An unchanged setting
    # cannot prove that a new command has limited subsequent physical draw.
    if isinstance(pending, dict):
        # A rejected acquisition can still reference this pending command in its previous reservation.
        pending[EV_CAP_FEEDBACK_CLOCK_INVALID] = True
        if (cap := finite(pending.get("physical_power_kw"))) is not None:
            reservation[EV_RETIRED_CAP_KW] = cap
    if "confirmed_cap_kw" not in reservation and isinstance(proof, dict):
        reservation["confirmed_cap_kw"] = proof.get("cap_kw")
    if isinstance(proof, dict):
        for confirmation in (proof, *proof.get("history", ())):
            confirmation[EV_CAP_FEEDBACK_CLOCK_INVALID] = True
            confirmation["prior_confirmation_at"] = None
            confirmation["prior_confirmed_cap_kw"] = None


def check_ev_cap_feedback_clock(reservation: dict[str, Any], now: datetime) -> tuple[datetime | None, bool]:
    """Invalidate observed clock rollbacks before processing the next EV report."""
    proof = reservation.get("cap_reduction")
    boundary = timestamp(reservation.get("confirmed_power_at"))
    if boundary is None and isinstance(proof, dict) and proof.get(EV_CAP_FEEDBACK_CLOCK_INVALID) is not True:
        boundary = timestamp(proof.get("confirmed_at"))
    pending = reservation.get("pending_power_limit")
    pending_at = timestamp(pending.get("at")) if isinstance(pending, dict) else None
    if (boundary is not None and boundary > now) or (pending_at is not None and pending_at > now):
        invalidate_ev_cap_feedback_clock(reservation)
        return None, True
    return boundary, False


@dataclass(frozen=True, slots=True)
class EVImplicitSignalClock:
    """A clock transition or invalidation for one unchanged raw state."""

    last_changed: datetime | None
    started_at: datetime | None
    invalid_explicit_samples: frozenset[datetime] = frozenset()
    omitted_explicit_samples: tuple[datetime, datetime] | None = None

    def without_raw_epoch(self) -> EVImplicitSignalClock | None:
        """Keep rejected source samples when a raw state changes or disappears."""
        if not self.invalid_explicit_samples and self.omitted_explicit_samples is None:
            return None
        return replace(self, last_changed=None, started_at=None)


@dataclass(slots=True)
class NativeEVStopObservations:
    """Remember the first stop reports after activity during one native transaction."""

    draw_at: datetime | None = None
    active_at: datetime | None = None
    stop_samples: list[datetime] = field(default_factory=list)
    first_known_stop: datetime | None = None
    stop_receipts: dict[str, list[tuple[datetime | None, datetime]]] = field(default_factory=dict)
    implicit_signal_clocks: dict[str, EVImplicitSignalClock] = field(default_factory=dict)

    @property
    def activity_at(self) -> datetime | None:
        if self.draw_at is None:
            return self.active_at
        return max(self.draw_at, self.active_at or self.draw_at)

    def note_draw(self, at: datetime, power: float) -> None:
        if power > 0:
            self.draw_at = max(self.draw_at or at, at)
        else:
            self.note_stop(at)

    def note_signal(self, at: datetime, active: bool) -> None:
        if active:
            self.active_at = max(self.active_at or at, at)
        else:
            self.note_stop(at)

    def note_stop(self, at: datetime) -> None:
        """Keep a bounded source-time history for a later out-of-order activity report."""
        if (activity_at := self.activity_at) is not None and at <= activity_at:
            return
        if activity_at is not None:
            self.first_known_stop = min(self.first_stop_after(activity_at) or at, at)
        index = bisect_left(self.stop_samples, at)
        if index < len(self.stop_samples) and self.stop_samples[index] == at:
            return
        self.stop_samples.insert(index, at)
        if len(self.stop_samples) > MAX_NATIVE_STOP_SAMPLES:
            # Keep early witnesses as well as the most recent samples during a burst.
            del self.stop_samples[MAX_NATIVE_STOP_SAMPLES // 2]

    def first_stop_after(self, at: datetime) -> datetime | None:
        """Use the first retained stop when late activity falls inside an evicted interval."""
        index = bisect_right(self.stop_samples, at)
        sampled = self.stop_samples[index] if index < len(self.stop_samples) else None
        if self.first_known_stop is not None and self.first_known_stop > at:
            return min(self.first_known_stop, sampled or self.first_known_stop)
        return sampled

    def note_stop_receipt(self, entity_id: str, sampled_at: datetime | None, received_at: datetime) -> None:
        """Keep bounded stop receipts and contradictory transitions by source."""
        receipts = self.stop_receipts.setdefault(entity_id, [])
        if receipts and sampled_at == receipts[-1][0]:
            return
        receipts.append((sampled_at, received_at))
        if len(receipts) > MAX_NATIVE_STOP_SAMPLES:
            # A cutoff in the omitted interval cannot rely on older evidence.
            middle = MAX_NATIVE_STOP_SAMPLES // 2
            _, omitted_at = receipts[middle]
            _, removed_at = receipts.pop(middle + 1)
            receipts[middle] = (None, min(omitted_at, removed_at))

    def stop_received_by(self, entity_id: str, deadline: datetime) -> datetime | None:
        latest = None
        for sampled, received in reversed(self.stop_receipts.get(entity_id, ())):
            if received > deadline:
                continue
            if sampled is None:
                break
            latest = max(latest or sampled, sampled)
        return latest


@dataclass(frozen=True, slots=True)
class EVRestoreFeedback:
    """Native feedback relevant to retrying retained EV restoration."""

    control_entity_ids: tuple[str, ...] = ()
    charging_entity_id: str | None = None
    connection_entity_id: str | None = None
    power_entity_id: str | None = None
    native_limit_entity_id: str | None = None
    native_limit_value: float | None = None
    native_limit_unit: str | None = None

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Return the control and feedback sources used during restoration."""
        sources = self.charging_entity_id, self.connection_entity_id, self.power_entity_id, self.native_limit_entity_id
        return self.control_entity_ids + tuple(entity for entity in sources if isinstance(entity, str) and entity)


def ev_cap_may_become_active(
    entity_id: str, previous: Any, current: Any, *, control_ids: tuple[str | None, ...], charging_id: str | None,
) -> bool:
    """Recognize the feedback transitions that can make an existing cap relevant."""
    return previous != current and (
        (entity_id in control_ids and connection(current) is True)
        or (entity_id == charging_id and ev_charging_state_proves_active(current))
    )


def timestamp(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return result if result.tzinfo is not None else None


def audit_evidence(audit: list[Any], options: dict[str, Any], now: datetime) -> dict[str, Any]:
    rows = [
        row
        for row in audit
        if isinstance(row, dict)
        and row.get("asset") == "ev"
        and counted_attempt(row)
        and (at := timestamp(row.get("attempted_at"))) is not None
        and now - timedelta(hours=24) <= at <= now
    ]
    limit = int(options.get("max_daily_ev_actions", 10))
    changed = [row for row in rows if row.get("reason") != "already_in_desired_state"]
    latest = max((at for row in changed if (at := timestamp(row.get("attempted_at"))) is not None), default=None)
    return {
        "remaining_actions": (
            action_budget(audit, {"max_daily_ev_actions": limit}, now, "ev")["remaining"] if limit > 0 else 10000
        ),
        "last_transition_at": latest.isoformat() if latest else None,
    }


def settle_spending(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Settle a durable command exposure using its conservative grid upper bound."""
    result = dict(record)
    exposure = result.get("command_exposure")
    if not isinstance(exposure, dict):
        return result
    start = timestamp(exposure.get("at"))
    if start is not None and start <= now:
        cost_rate = max(finite(exposure.get("cost_per_hour")) or 0, 0)
        result["emergency_spend"] = (
            max(finite(result.get("emergency_spend")) or 0, 0) + (now - start).total_seconds() / 3600 * cost_rate
        )
        result["command_exposure"] = {**exposure, "at": now.isoformat()}
    return result


def allocation_deadline(
    action: Any, options: dict[str, Any], record: dict[str, Any], now: datetime, price: float | None
) -> tuple[datetime | None, float, str | None]:
    """A premium command gets a bounded lease; never infer authorisation from missing prices."""
    fallback_until = timestamp(action.desired_state.get("load_fallback_until"))
    if fallback_until is not None and fallback_until <= now:
        return None, 0.0, "ev_load_fallback_expired"
    charge_now_until = timestamp(action.desired_state.get("charge_now_until"))
    if charge_now_until is not None:
        if charge_now_until <= now:
            return None, 0.0, "ev_charge_now_expired"
        return min(charge_now_until, fallback_until or charge_now_until), 0.0, None
    evidence = action.desired_state.get("optimization", {})
    completion = timestamp(evidence.get("conservative_completion"))
    end = action.execute_not_after
    deadline = completion if completion is not None and now < completion < end else None
    if fallback_until is not None:
        deadline = min(deadline or fallback_until, fallback_until)
    if not options.get("ev_price_limit_enabled"):
        return deadline, 0.0, None
    ceiling = float(options.get("ev_max_import_price", 1))
    if record.get("budget_uncertain") and price is not None and price > ceiling:
        return None, 0.0, "ev_emergency_spend_uncertain"
    if price is None:
        return None, 0.0, "ev_execution_price_unavailable"
    if price <= ceiling:
        return deadline, 0.0, None
    if options.get("ev_price_policy") != "departure_priority":
        return None, 0.0, "ev_execution_price_ceiling"
    emergency = finite(options.get("ev_emergency_price"))
    budget = max((finite(options.get("ev_emergency_budget")) or 0) - (finite(record.get("emergency_spend")) or 0), 0)
    if emergency is None or price > emergency or budget <= 0:
        return None, 0.0, "ev_emergency_budget_or_ceiling"
    power = max(
        finite(action.desired_state.get("projected_load_kw_now")) or 0, finite(options.get("ev_charge_rate_kw")) or 0, 0
    )
    # Reserve for the configured emergency ceiling, including confirmation and
    # stop dispatch latency. Missing solar telemetry never grants budget credit.
    cost_rate = power * max(emergency - ceiling, 0)
    if cost_rate <= 0:
        return None, 0.0, "ev_emergency_budget_or_ceiling"
    latency = 30 + float(options.get("ev_confirmation_timeout_seconds", 30)) * (
        int(options.get("ev_confirmation_retries", 1)) + 1
    )
    seconds = budget / cost_rate * 3600 - latency
    if seconds <= 0:
        return None, 0.0, "ev_emergency_budget_insufficient_for_confirmation"
    deadline = min(deadline or end, end, now + timedelta(seconds=seconds))
    return deadline, cost_rate, None


def price_stop_required(options: dict[str, Any], record: dict[str, Any], price: float | None) -> bool:
    """Revoke existing cost authority independently of the next start action."""
    if not options.get("ev_price_limit_enabled"):
        return False
    if price is None:
        return True
    if price <= float(options.get("ev_max_import_price", 1)):
        return False
    if options.get("ev_price_policy") != "departure_priority":
        return True
    return bool(
        record.get("budget_uncertain")
        or price > float(options.get("ev_emergency_price", 0))
        or float(record.get("emergency_spend", 0)) >= float(options.get("ev_emergency_budget", 0))
    )
