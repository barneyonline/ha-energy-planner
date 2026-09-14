"""Vehicle identity and session boundaries for a shared home charger."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from .const import (
    CONF_DEFAULT_READY_BY,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CONNECTED,
    CONF_EV_SMART_CHARGING_READY_BY,
    CONF_EV_SMART_CHARGING_TARGET_SOC,
    CONF_EV_SOC,
    CONF_EV_SOC_PER_KWH,
)

VEHICLE = "vehicle"
VEHICLES = "ev_vehicles"
PORT = "port_entity"
HOME = "home_entity"
AUTO = "Auto"
MANUAL = "Manual — no tracked charging"


def connection(value: Any) -> bool | None:
    """Normalize raw vehicle/charger connection states, preserving unknown."""
    value = str(value).strip().lower()
    if value in {"on", "true", "1", "connected", "plugged_in", "plugged in", "plugged"}:
        return True
    if value in {"off", "false", "0", "disconnected", "unplugged", "not_plugged_in"}:
        return False
    return None


def state_value(hass: Any, entity_id: Any) -> str:
    """Read a mapped state without treating absence as a negative result."""
    return str(getattr(hass.states.get(entity_id), "state", "unknown")) if entity_id else "unknown"


def home_state(value: Any) -> bool | None:
    """Accept HA zone states or an explicitly mapped home binary sensor."""
    normalized = str(value).strip().lower()
    if normalized in {"", "unknown", "unavailable", "none"}:
        return None
    return normalized in {"home", "on", "true", "1"}


def vehicle_entity_ids(data: Mapping[str, Any]) -> set[str]:
    """Observe all vehicles, including the vehicle that is not currently selected."""
    return {
        str(profile[key])
        for profile in data.get(VEHICLES, [])
        for key in (PORT, HOME, CONF_EV_SOC, CONF_EV_SMART_CHARGING_TARGET_SOC)
        if profile.get(key)
    }


def identify(hass: Any, data: Mapping[str, Any], selection: str) -> tuple[dict[str, Any] | None, str]:
    """Require unique home connection evidence; manual identity never starts power."""
    profiles = data.get(VEHICLES, [])
    if selection == MANUAL:
        return None, "manual"
    plugged = connection(state_value(hass, data.get(CONF_EV_CONNECTED)))
    if plugged is not True:
        return None, "unplugged" if plugged is False else "charger_connection_unknown"
    if selection != AUTO:
        profile = next((p for p in profiles if p["id"] == selection), None)
        return profile, "selected_manually" if profile else "vehicle_removed"
    candidates = []
    uncertain = False
    for profile in profiles:
        port = connection(state_value(hass, profile.get(PORT)))
        home = home_state(state_value(hass, profile.get(HOME)))
        if port is False or home is False:
            continue
        if port is True and home is True:
            candidates.append(profile)
        else:
            uncertain = True
    if len(candidates) > 1:
        return None, "multiple_vehicles_connected"
    if uncertain:
        return None, "vehicle_evidence_unknown"
    if not candidates:
        return None, "waiting_for_vehicle"
    return candidates[0], "detected_automatically"


def valid_soc(hass: Any, entity_id: Any) -> bool:
    """Require finite measured SOC and target values, never a configured fallback."""
    try:
        value = float(state_value(hass, entity_id).strip().removesuffix("%").strip())
    except ValueError:
        return False
    return isfinite(value) and 0 <= value <= 100


class VehicleSession:
    """Track selection and invalidate all commands at identity/connection boundaries."""

    def __init__(self, saved: Mapping[str, Any] | None = None) -> None:
        saved = saved or {}
        self.selection = str(saved.get("selection", AUTO))
        self.was_plugged = saved.get("was_plugged") is True
        self.connected_vehicle_ids = set(saved.get("connected_vehicle_ids", []))
        self.blocked_vehicle_ids = set(saved.get("blocked_vehicle_ids", []))
        self.generation = 0
        self.signature: tuple[Any, ...] | None = None
        self.profile: dict[str, Any] | None = None
        self.reason = "waiting_for_vehicle"
        self.allowed = False

    def note_unplug(self) -> None:
        """Do not reuse a vehicle's old connected signal for the next cable session."""
        self.blocked_vehicle_ids.update(self.connected_vehicle_ids)
        self.selection = AUTO
        self.signature = None
        self.generation += 1

    def update(self, hass: Any, data: Mapping[str, Any]) -> bool:
        """Recheck before planning and every command; return whether the session changed."""
        plugged = connection(state_value(hass, data.get(CONF_EV_CONNECTED)))
        if plugged is False and self.was_plugged:
            self.note_unplug()
        ports = {p["id"]: connection(state_value(hass, p.get(PORT))) for p in data.get(VEHICLES, [])}
        self.blocked_vehicle_ids.difference_update(key for key, value in ports.items() if value is False)
        self.connected_vehicle_ids = {key for key, value in ports.items() if value is True}
        if plugged is not None:
            self.was_plugged = plugged
        self.profile, self.reason = identify(hass, data, self.selection)
        if self.selection == AUTO and self.profile and self.profile["id"] in self.blocked_vehicle_ids:
            self.profile, self.reason = None, "waiting_for_vehicle_disconnect"
        self.allowed = self.profile is not None and all(
            valid_soc(hass, self.profile.get(key)) for key in (CONF_EV_SOC, CONF_EV_SMART_CHARGING_TARGET_SOC)
        )
        if self.profile is not None and not self.allowed:
            self.reason = "vehicle_soc_or_target_unavailable"
        # Include target and SOC values: an in-flight start must not survive a
        # target reduction or an update proving that the vehicle is already full.
        signature = (
            plugged,
            self.selection,
            self.profile["id"] if self.profile else None,
            self.allowed,
            *(
                state_value(hass, self.profile.get(key)) if self.profile else None
                for key in (CONF_EV_SOC, CONF_EV_SMART_CHARGING_TARGET_SOC)
            ),
        )
        signature += (self.profile.get(CONF_DEFAULT_READY_BY) if self.profile else None,)
        changed = signature != self.signature
        if changed:
            self.generation += 1
            self.signature = signature
        return changed

    def snapshot(self) -> dict[str, Any]:
        """Persist selection only, without location or raw vehicle telemetry."""
        return {
            "selection": self.selection,
            "was_plugged": self.was_plugged,
            "connected_vehicle_ids": sorted(self.connected_vehicle_ids),
            "blocked_vehicle_ids": sorted(self.blocked_vehicle_ids),
        }

    def resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        """Adapt a selected profile to the existing single-charger planner contract."""
        resolved = dict(data)
        resolved["_ev_configuration"] = data
        resolved["ev_policy_allowed"] = self.allowed
        resolved["ev_vehicle_id"] = self.profile["id"] if self.profile else None
        resolved["ev_session_generation"] = self.generation
        for key in (CONF_EV_SOC, CONF_EV_SMART_CHARGING_TARGET_SOC, CONF_EV_SMART_CHARGING_READY_BY):
            resolved.pop(key, None)
        if self.profile and self.allowed:
            for key in (CONF_EV_SOC, CONF_EV_SMART_CHARGING_TARGET_SOC):
                resolved[key] = self.profile[key]
        return resolved

    def options(self, options: dict[str, Any]) -> dict[str, Any]:
        """Keep ready-by and charging characteristics with their vehicle."""
        if not self.profile:
            return options
        return {
            **options,
            "_ev_shared_options": options,
            **{
                key: self.profile[key]
                for key in (
                    CONF_DEFAULT_READY_BY,
                    CONF_EV_SOC_PER_KWH,
                )
            },
            CONF_EV_CHARGE_RATE_KW: min(
                self.profile.get(CONF_EV_CHARGE_RATE_KW, options[CONF_EV_CHARGE_RATE_KW]),
                options[CONF_EV_CHARGE_RATE_KW],
            ),
        }


class VehicleCalibration:
    """Learn only intervals observed under an identified local vehicle session."""

    def __init__(self) -> None:
        self.pending: tuple[str, Any, float] | None = None

    def note_charging_change(self, old: Any, new: Any) -> None:
        """Discard interrupted intervals even when feedback events are coalesced."""
        from .ev import ev_charging_state

        previous, current = ev_charging_state(old), ev_charging_state(new)
        if current is None or (current is True and previous is not True):
            self.pending = None

    def observe(
        self,
        hass: Any,
        session: VehicleSession,
        data: dict[str, Any],
        model: dict[str, Any],
        now: Any,
        *,
        charge_rate_kw: float | None = None,
    ) -> dict[str, Any] | None:
        """Close a measured charging interval; interruptions discard partial learning."""
        from datetime import datetime
        from types import SimpleNamespace

        from .const import CONF_EV_CHARGING
        from .ev import build_ev_charge_calibration, ev_charge_calibration_matches, ev_charging_state

        profile = session.profile
        charging_value = state_value(hass, data.get(CONF_EV_CHARGING)).strip().lower()
        charging = None if charging_value in {"unknown", "unavailable"} else ev_charging_state(charging_value)
        if not session.allowed or not profile or charging is None:
            self.pending = None
            return None
        rate = charge_rate_kw if charge_rate_kw is not None else profile.get(CONF_EV_CHARGE_RATE_KW)
        if rate is None:
            self.pending = None
            return None
        vehicle_id = profile["id"]
        soc = float(state_value(hass, profile[CONF_EV_SOC]).strip().removesuffix("%").strip())
        if charging:
            if self.pending is None or self.pending[0] != vehicle_id:
                self.pending = (vehicle_id, now, soc)
            return None
        pending, self.pending = self.pending, None
        if pending is None or pending[0] != vehicle_id:
            return None
        samples = (
            model.get("samples", [])
            if ev_charge_calibration_matches(
                model,
                charging_entity_id=data.get(CONF_EV_CHARGING),
                soc_entity_id=profile[CONF_EV_SOC],
                charge_rate_kw=rate,
            )
            else []
        )
        points = [
            (
                datetime.fromisoformat(s["started_at"]),
                datetime.fromisoformat(s["ended_at"]),
                s["start_soc_percent"],
                s["end_soc_percent"],
            )
            for s in samples
        ]
        points.append((pending[1], now, pending[2], soc))
        charging_states, soc_states = [], []
        for start, end, start_soc, end_soc in points:
            charging_states.extend(
                [SimpleNamespace(state="on", last_updated=start), SimpleNamespace(state="off", last_updated=end)]
            )
            soc_states.extend(
                [
                    SimpleNamespace(state=str(start_soc), last_updated=start),
                    SimpleNamespace(state=str(end_soc), last_updated=end),
                ]
            )
        return build_ev_charge_calibration(
            charging_states,
            soc_states,
            charge_rate_kw=rate,
            trained_at=now,
            charging_entity_id=data[CONF_EV_CHARGING],
            soc_entity_id=profile[CONF_EV_SOC],
        )
