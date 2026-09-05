"""Pure EV capacity reservation policy shared by manual and scheduled commands.

The caller provides the shared registry and serializes the transaction. This
module has no Home Assistant or Store I/O and never yields while reserving.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from .command_models import ControlAction
from .const import CONF_EV_CHARGE_RATE_KW, CONF_GRID_IMPORT_LIMIT_KW
from .constraints import _projected_grid_flows_kw
from .models import ActionKind, DecisionContext


@dataclass(frozen=True, slots=True)
class EVGridReservation:
    """Validated newly acquired capacity, serialized at the shared-state boundary."""

    load_kw: float
    limit_kw: float
    reserved_at: datetime

    def to_record(self) -> dict[str, Any]:
        """Retain the existing persisted/shared reservation schema."""
        return {
            "load_kw": self.load_kw,
            "limit_kw": self.limit_kw,
            "reserved_at": self.reserved_at.isoformat(),
        }


def reserve_ev_grid_capacity(
    reservations: dict[str, dict[str, Any]],
    entry_id: str,
    action: ControlAction,
    context: DecisionContext | None,
    now: datetime,
    options: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Atomically reserve projected load, restoring the previous record on rejection."""
    previous = reservations.get(entry_id)
    if not _ev_action_wants_power(action):
        return None, previous
    if context is None or not context.slots:
        return "ev_grid_projection_unavailable", previous
    reservations.pop(entry_id, None)

    load_kw = _positive_float(action.desired_state.get("projected_load_kw_now"))
    if load_kw <= 0:
        load_kw = max(float(options.get(CONF_EV_CHARGE_RATE_KW, 0.0)), 0.0)
    if isinstance(previous, dict):
        # A model/options update cannot prove that an already-running charger
        # has reduced its physical draw. Only a confirmed stop releases the
        # prior reservation, so subsequent start/no-op actions retain the
        # larger of the observed reservation and newly requested load.
        load_kw = max(load_kw, _positive_float(previous.get("load_kw")))
    other_load_kw = sum(
        _positive_float(item.get("load_kw")) for item in reservations.values() if isinstance(item, dict)
    )
    projected_import_kw, _projected_export_kw = _projected_grid_flows_kw(context.slots[0])
    represented_ev_load_kw = _positive_float(context.slots[0].projected_ev_load_kw)
    additional_requested_load_kw = max(load_kw - represented_ev_load_kw, 0.0)
    if projected_import_kw is None:
        if previous is not None:
            reservations[entry_id] = previous
        reason = "multi_ev_grid_projection_unavailable" if other_load_kw > 0 else "ev_grid_projection_unavailable"
        return reason, previous
    configured_limit_kw = max(float(options.get(CONF_GRID_IMPORT_LIMIT_KW, 0.0)), 0.0)
    limits = [configured_limit_kw]
    limits.extend(
        max(_positive_float(item.get("limit_kw")), 0.0) for item in reservations.values() if isinstance(item, dict)
    )
    household_limit_kw = min(limits)
    if (
        projected_import_kw is not None
        and projected_import_kw + additional_requested_load_kw + other_load_kw > household_limit_kw + 1e-6
    ):
        if previous is not None:
            reservations[entry_id] = previous
        return "multi_ev_grid_import_limit_exceeded", previous
    reservation = EVGridReservation(load_kw, configured_limit_kw, now)
    reservations[entry_id] = reservation.to_record()
    return None, previous


def _ev_action_wants_power(action: ControlAction) -> bool:
    """Return whether an EV action asks the charger to remain energised."""
    if action.kind == ActionKind.EV_STOP:
        return False
    if action.kind == ActionKind.EV_START:
        return True
    if action.kind != ActionKind.EV_SCHEDULE:
        return False
    # Compatibility schedules predate charging_required_now and always start
    # charging after updating their external target/ready-by helpers. Keep this
    # in lockstep with EVChargerAdapter._async_schedule.
    return "charging_required_now" not in action.desired_state or bool(
        action.desired_state.get("charging_required_now")
    )


def _positive_float(value: Any) -> float:
    """Return a non-negative finite float for reservation arithmetic."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(numeric, 0.0) if isfinite(numeric) else 0.0
