"""Pure HVAC ownership transitions around the adapter transaction.

Executor owns serialization and durability; this component owns baseline merging
and user-supersession rules without reaching into Home Assistant or storage.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .hvac_adapter import HVACCommandResult

_HVAC_MAIN_STATE_OWNERSHIP_KEY = "main_state"

HVAC_LIFECYCLE_FIELDS = (
    "phase",
    "period_start",
    "period_end",
    "precondition_end",
    "baseline_price",
    "precondition_min_price_delta",
    "suppression_min_price_delta",
    "mode",
    "precondition_target",
    "coast_target",
    "projected_precondition_end_temperature",
    "released_until",
)


@dataclass(slots=True)
class HVACOwnershipTransaction:
    """A baseline snapshot and timestamp for one acquisition attempt."""

    previous: dict[str, Any]
    now: datetime

    @property
    def had_ownership(self) -> bool:
        return bool(
            self.previous.get("hvac_control")
            or self.previous.get("climate_automations")
            or self.previous.get("planner_takeover_started_at")
            or self.previous.get("planner_hvac_action_expires_at")
        )

    def prepare(
        self,
        desired: dict[str, Any],
        automation_snapshot: dict[str, str],
        zone_snapshot: dict[str, Any],
        main_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the provisional snapshot that must be flushed before commanding."""
        provisional_ownership_data = deepcopy(self.previous)
        provisional_automation_states = dict(provisional_ownership_data.get("climate_automations", {}))
        for entity_id, state in automation_snapshot.items():
            provisional_automation_states.setdefault(entity_id, state)
        provisional_ownership_data["climate_automations"] = provisional_automation_states
        provisional_hvac_control = dict(provisional_ownership_data.get("hvac_control", {}))
        provisional_zone_states = dict(provisional_hvac_control.get("zone_states", {}))
        for entity_id, state in zone_snapshot.items():
            provisional_zone_states.setdefault(entity_id, state)
        provisional_hvac_control["zone_states"] = provisional_zone_states
        if main_snapshot:
            provisional_hvac_control.setdefault(_HVAC_MAIN_STATE_OWNERSHIP_KEY, main_snapshot)
        for key in HVAC_LIFECYCLE_FIELDS:
            if desired.get(key) is not None:
                provisional_hvac_control[key] = desired[key]
        if provisional_hvac_control.get("phase") is None:
            provisional_hvac_control["phase"] = "away_off" if desired.get("hvac_mode") == "off" else "direct_control"
        provisional_hvac_control.setdefault("started_at", self.now)
        provisional_ownership_data["hvac_control"] = provisional_hvac_control
        provisional_ownership_data.setdefault("planner_takeover_started_at", self.now)
        provisional_ownership_data["planner_hvac_action_expires_at"] = self.now + timedelta(minutes=2)
        return provisional_ownership_data

    def complete(
        self,
        hvac_result: HVACCommandResult,
        current: dict[str, Any],
        desired: dict[str, Any],
        *,
        main_state_superseded: bool,
        superseded_zone_entity_ids: set[str],
    ) -> dict[str, Any] | None:
        """Reconcile confirmed success/rollback without undoing manual changes."""
        if not hvac_result.applied:
            if hvac_result.rollback_succeeded is True:
                rollback_ownership = deepcopy(self.previous)
                if isinstance(
                    rollback_ownership.get("hvac_control"),
                    dict,
                ) and (main_state_superseded or superseded_zone_entity_ids):
                    rollback_hvac_control = dict(rollback_ownership["hvac_control"])
                    if main_state_superseded:
                        rollback_hvac_control.pop(
                            _HVAC_MAIN_STATE_OWNERSHIP_KEY,
                            None,
                        )
                    rollback_zone_states = dict(rollback_hvac_control.get("zone_states", {}))
                    for entity_id in superseded_zone_entity_ids:
                        rollback_zone_states.pop(entity_id, None)
                    rollback_hvac_control["zone_states"] = rollback_zone_states
                    rollback_ownership["hvac_control"] = rollback_hvac_control
                return rollback_ownership
            if hvac_result.rollback_succeeded is False:
                ownership_data = deepcopy(self.previous)
                saved_states = dict(ownership_data.get("climate_automations", {})) if self.had_ownership else {}
                for entity_id, state in hvac_result.saved_automation_states.items():
                    saved_states.setdefault(entity_id, state)
                if saved_states:
                    ownership_data["climate_automations"] = saved_states
                hvac_control = dict(ownership_data.get("hvac_control", {})) if self.had_ownership else {}
                if main_state_superseded:
                    hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
                zone_states = dict(hvac_control.get("zone_states", {})) if self.had_ownership else {}
                for entity_id in superseded_zone_entity_ids:
                    zone_states.pop(entity_id, None)
                for entity_id, state in hvac_result.saved_zone_states.items():
                    if entity_id in superseded_zone_entity_ids:
                        continue
                    zone_states.setdefault(entity_id, state)
                hvac_control["zone_states"] = zone_states
                unresolved_main_state = dict(hvac_result.saved_main_state)
                if unresolved_main_state:
                    hvac_control.setdefault(
                        _HVAC_MAIN_STATE_OWNERSHIP_KEY,
                        unresolved_main_state,
                    )
                hvac_control["required_evidence_lost"] = "hvac_acquisition_rollback_failed"
                ownership_data["hvac_control"] = hvac_control
                return ownership_data
        if hvac_result.applied:
            ownership_data = dict(current)
            saved_automations = dict(ownership_data.get("climate_automations", {}))
            for entity_id, state in hvac_result.saved_automation_states.items():
                saved_automations.setdefault(entity_id, state)
            ownership_data["climate_automations"] = saved_automations
            hvac_control = dict(ownership_data.get("hvac_control", {}))
            saved_zones = dict(hvac_control.get("zone_states", {}))
            for entity_id, state in hvac_result.saved_zone_states.items():
                saved_zones.setdefault(entity_id, state)
            hvac_control["zone_states"] = saved_zones
            hvac_control.pop(_HVAC_MAIN_STATE_OWNERSHIP_KEY, None)
            for key in HVAC_LIFECYCLE_FIELDS:
                if desired.get(key) is not None:
                    hvac_control[key] = desired[key]
            hvac_control.setdefault("started_at", self.now)
            ownership_data["hvac_control"] = hvac_control
            ownership_data.pop("hvac_release_hold_until", None)
            ownership_data.setdefault("planner_takeover_started_at", self.now)
            ownership_data["planner_hvac_action_expires_at"] = self.now + timedelta(minutes=2)
            return ownership_data
        return None
