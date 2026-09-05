"""Tests for ownership state machines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ha_energy_planner.ownership import EnphaseProfileGuard, OwnershipState


def test_hvac_ownership_restores_saved_states_once() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    ownership = OwnershipState()
    ownership.begin_hvac_takeover({"automation.climate": "on"}, now)
    ownership.hvac_control_phase = "preconditioning"
    assert ownership.takeover_active is True
    assert ownership.restore_hvac() == {"automation.climate": "on"}
    assert ownership.hvac_control_phase is None
    assert ownership.restore_hvac() == {}
    assert ownership.takeover_active is False


def test_manual_override_expires() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    ownership = OwnershipState()
    ownership.set_manual_hvac_override(now, timedelta(hours=2))
    assert ownership.manual_hvac_override_active(now + timedelta(minutes=30)) is True
    ownership.clear_expired_overrides(now + timedelta(hours=2))
    assert ownership.manual_hvac_override_active(now + timedelta(hours=2)) is False


def test_enphase_profile_guard_reports_remaining_hold() -> None:
    now = datetime(2026, 6, 27, 0, 10, tzinfo=UTC)
    guard = EnphaseProfileGuard(timedelta(minutes=30), now - timedelta(minutes=10))
    assert guard.can_change(now) is False
    assert guard.remaining_hold(now) == timedelta(minutes=20)
    assert guard.can_change(now + timedelta(minutes=20)) is True


def test_enphase_ownership_validates_legacy_records_and_preserves_other_fields() -> None:
    from custom_components.ha_energy_planner.ownership import EnphaseOwnership

    assert EnphaseOwnership.from_mapping(None) == EnphaseOwnership()
    assert (
        EnphaseOwnership.from_mapping({"enphase_profile": 123, "enphase_profile_changed_at": []}) == EnphaseOwnership()
    )
    previous = {"enphase_profile": "AI", "enphase_profile_changed_at": "2026-01-01T00:00:00Z", "hvac": "keep"}
    saved = EnphaseOwnership.from_mapping(previous)
    saved.apply_to(previous)
    assert previous["enphase_profile"] == "AI"
    EnphaseOwnership().apply_to(previous)
    assert previous == {"hvac": "keep"}
