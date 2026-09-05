"""Tests for Enphase profile adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ha_energy_planner.const import (
    CONF_ENPHASE_AI_PROFILE,
    CONF_ENPHASE_PROFILE,
    CONF_ENPHASE_PROFILE_CONTROL_SERVICE,
)
from custom_components.ha_energy_planner.enphase_adapter import EnphaseProfileAdapter, _profile_control_service
from custom_components.ha_energy_planner.models import ActionAsset, ActionKind, PlanAction


@dataclass(slots=True)
class FakeState:
    """Minimal HA state."""

    state: str


class FakeStates:
    """Minimal HA state registry."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> FakeState | None:
        value = self.values.get(entity_id)
        return None if value is None else FakeState(value)


class FakeServices:
    """Minimal HA service bus."""

    def __init__(self, states: FakeStates, *, confirm_change: bool = True, fail: bool = False) -> None:
        self.states = states
        self.confirm_change = confirm_change
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        self.calls.append((domain, service, data))
        if self.fail:
            raise RuntimeError("service failed")
        if self.confirm_change and "option" in data:
            self.states.values[data["entity_id"]] = str(data["option"])


class FakeHass:
    """Minimal HA object."""

    def __init__(self, values: dict[str, str], *, confirm_change: bool = True, fail: bool = False) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states, confirm_change=confirm_change, fail=fail)


def _action(kind: ActionKind, desired_state: dict[str, Any] | None = None) -> PlanAction:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return PlanAction(
        action_id=kind,
        plan_id="plan-1",
        execute_not_before=now,
        execute_not_after=now + timedelta(minutes=5),
        asset=ActionAsset.ENPHASE,
        kind=kind,
        desired_state=desired_state or {},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=1.0,
        confidence=1.0,
    )


def _entry_data() -> dict[str, str]:
    return {
        CONF_ENPHASE_PROFILE: "select.enphase_profile",
        CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
    }


def test_enphase_profile_change_requires_observed_confirmation() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"})
    adapter = EnphaseProfileAdapter(hass, _entry_data())
    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))
    assert result.applied is True
    assert result.reason == "enphase_profile_applied"
    assert result.saved_profile == "AI Optimisation"
    assert result.changed_profile_at is True
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_profile_change_honors_legacy_configured_service() -> None:
    hass = FakeHass({"input_select.enphase_profile": "AI Optimisation"})
    adapter = EnphaseProfileAdapter(
        hass,
        {
            CONF_ENPHASE_PROFILE: "input_select.enphase_profile",
            CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "input_select.select_option",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is True
    assert hass.services.calls == [
        ("input_select", "select_option", {"entity_id": "input_select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_profile_change_infers_input_select_service() -> None:
    hass = FakeHass({"input_select.enphase_profile": "AI Optimisation"})
    adapter = EnphaseProfileAdapter(
        hass,
        {
            CONF_ENPHASE_PROFILE: "input_select.enphase_profile",
            CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
        },
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is True
    assert hass.services.calls == [
        ("input_select", "select_option", {"entity_id": "input_select.enphase_profile", "option": "Full Backup"}),
    ]


def test_enphase_restore_profile_uses_saved_profile_instead_of_ai_default() -> None:
    hass = FakeHass({"select.enphase_profile": "Full Backup"})
    adapter = EnphaseProfileAdapter(hass, _entry_data())

    result = asyncio.run(adapter.async_restore_profile("Self-Consumption"))

    assert result.applied is True
    assert result.saved_profile == "Full Backup"
    assert hass.states.values["select.enphase_profile"] == "Self-Consumption"


def test_enphase_profile_change_fails_when_not_confirmed() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"}, confirm_change=False)
    adapter = EnphaseProfileAdapter(hass, _entry_data(), confirmation_interval_seconds=0)
    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))
    assert result.applied is False
    assert result.reason == "enphase_profile_not_confirmed_rolled_back"
    assert result.command_sent is True
    assert result.rollback_succeeded is True
    assert hass.services.calls[-1][2]["option"] == "AI Optimisation"


def test_enphase_profile_change_accepts_delayed_state_confirmation() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"}, confirm_change=False)
    original_get = hass.states.get
    reads = 0

    def delayed_get(entity_id: str) -> FakeState | None:
        nonlocal reads
        reads += 1
        if reads >= 4:
            hass.states.values[entity_id] = "Full Backup"
        return original_get(entity_id)

    hass.states.get = delayed_get
    adapter = EnphaseProfileAdapter(
        hass,
        _entry_data(),
        confirmation_attempts=3,
        confirmation_interval_seconds=0,
    )

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is True
    assert result.reason == "enphase_profile_applied"
    assert result.command_sent is True
    assert len(hass.services.calls) == 1


def test_enphase_profile_change_retains_uncertainty_when_rollback_fails() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"}, confirm_change=False)
    original_call = hass.services.async_call
    call_count = 0

    async def fail_rollback(
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
    ) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("rollback failed")
        await original_call(domain, service, data, blocking)

    hass.services.async_call = fail_rollback
    adapter = EnphaseProfileAdapter(hass, _entry_data(), confirmation_interval_seconds=0)

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is False
    assert result.reason == "enphase_profile_not_confirmed_rollback_failed"
    assert result.command_sent is True
    assert result.rollback_succeeded is False
    assert result.saved_profile == "AI Optimisation"
    assert result.post_state[CONF_ENPHASE_PROFILE] == "AI Optimisation"


def test_enphase_rollback_rejects_missing_profile_or_invalid_service() -> None:
    adapter = EnphaseProfileAdapter(
        FakeHass({"select.enphase_profile": "AI Optimisation"}),
        _entry_data(),
        confirmation_interval_seconds=0,
    )

    assert asyncio.run(adapter._async_rollback_profile("select.enphase_profile", "select.select_option", None)) is False
    assert asyncio.run(adapter._async_rollback_profile("select.enphase_profile", "invalid", "AI Optimisation")) is False


def test_enphase_profile_change_fails_closed_when_service_fails() -> None:
    hass = FakeHass({"select.enphase_profile": "AI Optimisation"}, fail=True)
    adapter = EnphaseProfileAdapter(hass, _entry_data())

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is False
    assert result.reason == "enphase_profile_service_failed"
    assert result.command_sent is True
    assert result.rollback_succeeded is False
    assert result.post_state[CONF_ENPHASE_PROFILE] == "AI Optimisation"
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "Full Backup"}),
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "AI Optimisation"}),
    ]


def test_enphase_profile_service_exception_rolls_back_a_mutated_profile() -> None:
    class ApplyThenRaiseServices(FakeServices):
        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            blocking: bool = False,
        ) -> None:
            self.calls.append((domain, service, data))
            self.states.values[data["entity_id"]] = str(data["option"])
            if len(self.calls) == 1:
                raise RuntimeError("service failed after applying profile")

    hass = FakeHass({"select.enphase_profile": "AI Optimisation"})
    hass.services = ApplyThenRaiseServices(hass.states)
    adapter = EnphaseProfileAdapter(hass, _entry_data())

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"})))

    assert result.applied is False
    assert result.reason == "enphase_profile_service_failed"
    assert result.command_sent is True
    assert result.rollback_succeeded is True
    assert result.post_state[CONF_ENPHASE_PROFILE] == "AI Optimisation"
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "Full Backup"}),
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "AI Optimisation"}),
    ]


def test_enphase_restore_ai_uses_configured_ai_profile() -> None:
    hass = FakeHass({"select.enphase_profile": "Full Backup"})
    adapter = EnphaseProfileAdapter(hass, _entry_data())
    result = asyncio.run(adapter.async_execute(_action(ActionKind.RESTORE_AI)))
    assert result.applied is True
    assert result.reason == "enphase_profile_applied"
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.enphase_profile", "option": "AI Optimisation"}),
    ]


def test_enphase_rejects_missing_profile_and_unsupported_action() -> None:
    adapter = EnphaseProfileAdapter(FakeHass({"select.enphase_profile": "AI Optimisation"}), _entry_data())
    missing = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE)))
    unsupported = asyncio.run(adapter.async_execute(_action(ActionKind.EV_START)))

    assert missing.reason == "enphase_profile_missing"
    assert unsupported.reason == "unsupported_enphase_action"


def test_enphase_restore_requires_configured_ai_profile() -> None:
    adapter = EnphaseProfileAdapter(
        FakeHass({"select.enphase_profile": "Full Backup"}),
        {CONF_ENPHASE_PROFILE: "select.enphase_profile"},
    )

    result = asyncio.run(adapter.async_restore_ai())

    assert result.applied is False
    assert result.reason == "enphase_ai_profile_not_configured"


def test_enphase_profile_requires_available_entity_and_control_service() -> None:
    unavailable = asyncio.run(
        EnphaseProfileAdapter(
            FakeHass({"select.enphase_profile": "unavailable"}),
            _entry_data(),
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )
    missing_control = asyncio.run(
        EnphaseProfileAdapter(
            FakeHass({"sensor.enphase_profile": "AI Optimisation"}),
            {CONF_ENPHASE_PROFILE: "sensor.enphase_profile", CONF_ENPHASE_AI_PROFILE: "AI Optimisation"},
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )
    invalid_control = asyncio.run(
        EnphaseProfileAdapter(
            FakeHass({"select.enphase_profile": "AI Optimisation"}),
            {
                CONF_ENPHASE_PROFILE: "select.enphase_profile",
                CONF_ENPHASE_AI_PROFILE: "AI Optimisation",
                CONF_ENPHASE_PROFILE_CONTROL_SERVICE: "select_option",
            },
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )

    assert unavailable.reason == "enphase_profile_entity_unavailable"
    assert missing_control.reason == "enphase_profile_control_not_configured"
    assert invalid_control.reason == "enphase_profile_control_invalid"


def test_enphase_profile_change_skips_when_already_selected_and_helper_fallbacks() -> None:
    adapter = EnphaseProfileAdapter(FakeHass({"select.enphase_profile": "AI Optimisation"}), _entry_data())

    result = asyncio.run(adapter.async_execute(_action(ActionKind.SET_PROFILE, {"profile": "AI Optimisation"})))

    assert result.applied is True
    assert result.reason == "already_in_desired_profile"
    assert result.changed_profile_at is False
    assert _profile_control_service({}, None) is None
    assert _profile_control_service({}, "sensor.enphase") is None


class TransactionStore:
    """Durable fake records the exact ownership visible before dispatch."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"ownership": {"unrelated": "preserved"}}
        self.durable: dict[str, Any] = {}
        self.fail_flush = False

    async def async_save_ownership(self, ownership: dict[str, Any]) -> None:
        self.data["ownership"] = dict(ownership)

    async def async_flush(self) -> None:
        if self.fail_flush:
            raise OSError("storage failed")
        self.durable = dict(self.data["ownership"])


def test_enphase_transaction_preserves_original_baseline_and_unowned_noop() -> None:
    from custom_components.ha_energy_planner.enphase_control import EnphaseControlTransaction

    async def run() -> None:
        store = TransactionStore()
        hass = FakeHass({"select.enphase_profile": "Custom Baseline"})
        adapter = EnphaseProfileAdapter(hass, _entry_data())
        now = datetime.now(UTC)
        noop = await EnphaseControlTransaction(store, adapter, now).async_execute(
            _action(ActionKind.SET_PROFILE, {"profile": "Custom Baseline"})
        )
        assert not noop.command_sent
        assert store.data["ownership"] == {"unrelated": "preserved"}
        for profile in ["Full Backup", "Full Backup", "Self Consumption"]:
            await EnphaseControlTransaction(store, adapter, now).async_execute(
                _action(ActionKind.SET_PROFILE, {"profile": profile})
            )
            assert store.data["ownership"]["enphase_profile"] == "Custom Baseline"
        assert store.durable["enphase_profile"] == "Custom Baseline"
        await EnphaseControlTransaction(store, adapter, now).async_execute(_action(ActionKind.RESTORE_AI))
        assert store.data["ownership"] == {"unrelated": "preserved"}
        assert hass.states.values["select.enphase_profile"] == "AI Optimisation"

    asyncio.run(run())


def test_enphase_transaction_cancellation_retains_durable_restore_evidence() -> None:
    import pytest

    from custom_components.ha_energy_planner.enphase_control import EnphaseControlTransaction

    async def run() -> None:
        for accept in [False, True]:
            store = TransactionStore()
            hass = FakeHass({"select.enphase_profile": "Custom Baseline"})
            original = hass.services.async_call

            async def cancelled(
                domain: str,
                service: str,
                data: dict[str, Any],
                blocking: bool = False,
                store: Any = store,
                accept: bool = accept,
                original: Any = original,
            ) -> None:
                assert store.durable["enphase_profile"] == "Custom Baseline"
                if accept:
                    await original(domain, service, data, blocking)
                raise asyncio.CancelledError

            hass.services.async_call = cancelled
            with pytest.raises(asyncio.CancelledError):
                await EnphaseControlTransaction(
                    store, EnphaseProfileAdapter(hass, _entry_data()), datetime.now(UTC)
                ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
            assert store.data["ownership"]["enphase_profile"] == "Custom Baseline"
            hass.services.async_call = original
            result = await EnphaseProfileAdapter(hass, _entry_data()).async_restore_profile(
                store.durable["enphase_profile"]
            )
            assert result.applied
            assert hass.states.values["select.enphase_profile"] == "Custom Baseline"

    asyncio.run(run())


def test_enphase_transaction_storage_failure_prevents_all_commands() -> None:
    import pytest

    from custom_components.ha_energy_planner.enphase_control import EnphaseControlTransaction

    store = TransactionStore()
    store.fail_flush = True
    hass = FakeHass({"select.enphase_profile": "Custom Baseline"})
    with pytest.raises(OSError, match="storage failed"):
        asyncio.run(
            EnphaseControlTransaction(
                store, EnphaseProfileAdapter(hass, _entry_data()), datetime.now(UTC)
            ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
        )
    assert hass.services.calls == []


def test_enphase_transaction_confirmation_failure_restores_previous_ownership() -> None:
    from custom_components.ha_energy_planner.enphase_control import EnphaseControlTransaction

    store = TransactionStore()
    hass = FakeHass({"select.enphase_profile": "Custom Baseline"}, confirm_change=False)
    result = asyncio.run(
        EnphaseControlTransaction(
            store, EnphaseProfileAdapter(hass, _entry_data(), confirmation_attempts=1), datetime.now(UTC)
        ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
    )
    assert result.rollback_succeeded is True
    assert store.data["ownership"] == {"unrelated": "preserved"}


def test_enphase_dispatch_and_compensation_are_bounded(monkeypatch: Any) -> None:
    from custom_components.ha_energy_planner import adapter_helpers
    from custom_components.ha_energy_planner.enphase_control import EnphaseControlTransaction

    monkeypatch.setattr(adapter_helpers, "DEVICE_SERVICE_TIMEOUT_SECONDS", 0.005)

    async def run() -> None:
        store = TransactionStore()
        hass = FakeHass({"select.enphase_profile": "Custom Baseline"})
        calls = 0

        async def hung(domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                hass.states.values[data["entity_id"]] = data["option"]
            await asyncio.Event().wait()

        hass.services.async_call = hung
        async with asyncio.timeout(1):
            result = await EnphaseControlTransaction(
                store, EnphaseProfileAdapter(hass, _entry_data()), datetime.now(UTC)
            ).async_execute(_action(ActionKind.SET_PROFILE, {"profile": "Full Backup"}))
        assert not result.applied
        assert result.command_sent
        assert result.rollback_succeeded is False
        assert calls == 2
        assert store.data["ownership"]["enphase_profile"] == "Custom Baseline"

    asyncio.run(run())
