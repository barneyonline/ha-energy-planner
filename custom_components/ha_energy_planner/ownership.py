"""Planner ownership state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(slots=True)
class OwnershipState:
    """Saved ownership state for future execution milestones."""

    enphase_profile: str | None = None
    enphase_profile_changed_at: datetime | None = None
    climate_automations: dict[str, str] = field(default_factory=dict)
    ev_smart_charging_state: dict[str, str] = field(default_factory=dict)
    planner_takeover_started_at: datetime | None = None
    manual_hvac_override_expires_at: datetime | None = None

    @property
    def takeover_active(self) -> bool:
        """Return whether any ownership state is active."""
        return bool(
            self.enphase_profile
            or self.climate_automations
            or self.ev_smart_charging_state
            or self.planner_takeover_started_at
        )

    def manual_hvac_override_active(self, now: datetime) -> bool:
        """Return whether manual HVAC override is active."""
        return self.manual_hvac_override_expires_at is not None and now < self.manual_hvac_override_expires_at

    def begin_hvac_takeover(self, automation_states: dict[str, str], now: datetime) -> None:
        """Save climate automation state and mark takeover active."""
        if not self.climate_automations:
            self.climate_automations = dict(automation_states)
        self.planner_takeover_started_at = now

    def restore_hvac(self) -> dict[str, str]:
        """Return saved automation states and clear HVAC ownership."""
        states = dict(self.climate_automations)
        self.climate_automations = {}
        self.planner_takeover_started_at = None
        return states

    def set_manual_hvac_override(self, now: datetime, duration: timedelta) -> None:
        """Set manual HVAC override expiry."""
        self.manual_hvac_override_expires_at = now + duration

    def clear_expired_overrides(self, now: datetime) -> None:
        """Clear expired override state."""
        if self.manual_hvac_override_expires_at and now >= self.manual_hvac_override_expires_at:
            self.manual_hvac_override_expires_at = None


@dataclass(slots=True)
class EnphaseProfileGuard:
    """Enforce Enphase profile minimum hold time."""

    min_hold: timedelta
    last_changed_at: datetime | None = None

    def can_change(self, now: datetime) -> bool:
        """Return whether a profile change is allowed."""
        if self.last_changed_at is None:
            return True
        return now >= self.last_changed_at + self.min_hold

    def remaining_hold(self, now: datetime) -> timedelta:
        """Return remaining hold duration."""
        if self.last_changed_at is None:
            return timedelta(0)
        until = self.last_changed_at + self.min_hold
        return max(until - now, timedelta(0))
