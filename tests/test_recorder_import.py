"""Tests for optional Recorder imports."""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import custom_components.ha_energy_planner.recorder_import as recorder_import
from custom_components.ha_energy_planner.const import CONF_EV_CONNECTED, CONF_EV_SOC


class FakeHass:
    """Minimal hass with executor helper."""

    def __init__(self) -> None:
        self.generic_executor_calls = 0

    async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
        self.generic_executor_calls += 1
        return fn(*args)


class FakeRecorderInstance:
    """Minimal Recorder instance."""

    def __init__(self) -> None:
        self.calls = 0

    async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
        self.calls += 1
        return fn(*args)


class RecorderState:
    """Minimal Recorder state."""

    def __init__(self, state: str, timestamp: datetime) -> None:
        self.state = state
        self.last_changed = timestamp
        self.last_updated = timestamp


def test_recorder_import_skips_when_recent() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    history = {"recorder_imported_at": (now - timedelta(hours=1)).isoformat()}

    updated, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            FakeHass(),
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            history,
            now=now,
        )
    )

    assert updated is history
    assert changed is False
    assert reason == "recorder_import_recent"


def test_recorder_import_handles_naive_persisted_timestamp_string() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    history = {"recorder_imported_at": "2026-06-27T11:00:00"}

    updated, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            FakeHass(),
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            history,
            now=now,
        )
    )

    assert updated is history
    assert changed is False
    assert reason == "recorder_import_recent"


def test_recorder_import_handles_naive_persisted_datetime() -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    history = {"recorder_imported_at": datetime(2026, 6, 27, 11, 0)}

    updated, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            FakeHass(),
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            history,
            now=now,
        )
    )

    assert updated is history
    assert changed is False
    assert reason == "recorder_import_recent"


def test_recorder_import_loads_and_compacts_history(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=4)

    def fake_load(*args: Any) -> tuple[list[RecorderState], list[RecorderState]]:
        return (
            [
                RecorderState("on", start),
                RecorderState("off", start + timedelta(hours=1)),
                RecorderState("on", start + timedelta(hours=3)),
            ],
            [
                RecorderState("80", start),
                RecorderState("79", start + timedelta(hours=1)),
                RecorderState("72", start + timedelta(hours=3)),
            ],
        )

    monkeypatch.setattr(recorder_import, "_load_recorder_states", fake_load)

    history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            FakeHass(),
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            now=now,
        )
    )

    assert changed is True
    assert reason == "recorder_imported"
    assert history["records"][0]["start_soc_percent"] == 79.0
    assert history["records"][0]["end_soc_percent"] == 72.0


def test_recorder_import_prefers_recorder_database_executor(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    recorder_instance = FakeRecorderInstance()
    hass = FakeHass()

    def fake_import_module(name: str) -> Any:
        if name == "homeassistant.components.recorder":
            return types.SimpleNamespace(get_instance=lambda _hass: recorder_instance)
        raise AssertionError(name)

    def fake_load(*args: Any) -> tuple[list[RecorderState], list[RecorderState]]:
        return [], []

    monkeypatch.setattr(recorder_import, "import_module", fake_import_module)
    monkeypatch.setattr(recorder_import, "_load_recorder_states", fake_load)

    _history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            hass,
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            now=now,
        )
    )

    assert changed is False
    assert reason == "recorder_no_new_trips"
    assert recorder_instance.calls == 1
    assert hass.generic_executor_calls == 0


def test_recorder_import_falls_back_to_home_assistant_executor(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    hass = FakeHass()

    def fake_import_module(name: str) -> Any:
        if name == "homeassistant.components.recorder":
            raise ImportError(name)
        raise AssertionError(name)

    def fake_load(*args: Any) -> tuple[list[RecorderState], list[RecorderState]]:
        return [], []

    monkeypatch.setattr(recorder_import, "import_module", fake_import_module)
    monkeypatch.setattr(recorder_import, "_load_recorder_states", fake_load)

    _history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            hass,
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            now=now,
        )
    )

    assert changed is False
    assert reason == "recorder_no_new_trips"
    assert hass.generic_executor_calls == 1


def test_recorder_import_requires_configured_ev_entities() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(FakeHass(), {}, {"existing": True}, now=now)
    )

    assert history == {"existing": True}
    assert changed is False
    assert reason == "recorder_ev_entities_not_configured"


def test_recorder_import_reports_loader_errors(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    def fake_load(*args: Any) -> tuple[list[Any], list[Any]]:
        raise RuntimeError("recorder down")

    monkeypatch.setattr(recorder_import, "_load_recorder_states", fake_load)

    history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            FakeHass(),
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            {"existing": True},
            now=now,
        )
    )

    assert history == {"existing": True}
    assert changed is False
    assert reason == "recorder_import_unavailable:RuntimeError"


def test_recorder_load_states_uses_recorder_history_module(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []
    connected_states = [object()]
    soc_states = [object()]

    def fake_import_module(name: str) -> Any:
        assert name == "homeassistant.components.recorder.history"

        def state_changes_during_period(
            hass: Any,
            start_time: datetime,
            end_time: datetime,
            *,
            entity_id: str,
            no_attributes: bool,
            include_start_time_state: bool,
        ) -> dict[str, list[Any]]:
            calls.append(
                {
                    "entity_id": entity_id,
                    "no_attributes": no_attributes,
                    "include_start_time_state": include_start_time_state,
                }
            )
            return {
                "binary_sensor.ev_connected": connected_states,
                "sensor.ev_soc": soc_states,
            }

        return types.SimpleNamespace(state_changes_during_period=state_changes_during_period)

    monkeypatch.setattr(recorder_import, "import_module", fake_import_module)

    connected, soc = recorder_import._load_recorder_states(
        FakeHass(),
        "binary_sensor.ev_connected",
        "sensor.ev_soc",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 27, tzinfo=UTC),
    )

    assert connected == connected_states
    assert soc == soc_states
    assert [call["entity_id"] for call in calls] == ["binary_sensor.ev_connected", "sensor.ev_soc"]


def test_recorder_import_due_handles_invalid_and_naive_timestamps() -> None:
    aware_now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    naive_now = datetime(2026, 6, 27, 12, 0)

    assert recorder_import._import_due({}, aware_now) is True
    assert recorder_import._import_due({"recorder_imported_at": "bad"}, aware_now) is True
    assert recorder_import._import_due({"recorder_imported_at": 123}, aware_now) is True
    assert (
        recorder_import._import_due({"recorder_imported_at": datetime(2026, 6, 26, 0, 0, tzinfo=UTC)}, naive_now)
        is True
    )
