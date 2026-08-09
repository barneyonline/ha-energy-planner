"""Tests for optional Recorder imports."""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import custom_components.ha_energy_planner.recorder_import as recorder_import
from custom_components.ha_energy_planner.const import (
    CONF_DAIKIN_POWER,
    CONF_EV_CHARGING,
    CONF_EV_CONNECTED,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
)


class FakeHass:
    """Minimal hass with executor helper."""

    def __init__(self) -> None:
        self.generic_executor_calls = 0

    async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
        self.generic_executor_calls += 1
        return fn(*args)


class FakeStates:
    """Minimal state machine."""

    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def get(self, entity_id: str | None) -> Any:
        return self._states.get(str(entity_id))


class ForecastHass(FakeHass):
    """Fake Home Assistant with mapped current power states."""

    def __init__(self, states: dict[str, Any]) -> None:
        super().__init__()
        self.states = FakeStates(states)


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


class CurrentState:
    """Minimal current state with units."""

    def __init__(self, state: str, unit: str = "kW") -> None:
        self.state = state
        self.attributes = {"unit_of_measurement": unit}


def test_builtin_load_forecast_requires_mapping_and_current_state() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    missing, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({}), {}, {"existing": True}, now=now, timezone="UTC"
        )
    )
    unavailable, unavailable_changed, unavailable_reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            {"existing": True},
            now=now,
            timezone="UTC",
        )
    )

    assert missing == {"existing": True}
    assert changed is False
    assert reason == "load_forecast_household_load_not_configured"
    assert unavailable["existing"] is True
    assert unavailable_changed is True
    assert unavailable["last_attempt_at"] == now.isoformat()
    assert unavailable_reason == "load_forecast_household_load_unavailable"


def test_builtin_load_forecast_loads_optional_cleaning_histories_and_persists_only_model(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    loaded = {
        "load": [RecorderState("2", now - timedelta(days=1))],
        "ev_charging": [RecorderState("off", now - timedelta(days=1))],
        "hvac_power": [RecorderState("0.5", now - timedelta(days=1))],
    }
    loader_calls: list[tuple[Any, ...]] = []

    def fake_load(*args: Any) -> dict[str, list[Any]]:
        loader_calls.append(args)
        return loaded

    def fake_build(states: list[Any], **kwargs: Any) -> dict[str, Any]:
        assert states is loaded["load"]
        assert kwargs["ev_charging_states"] is loaded["ev_charging"]
        assert kwargs["hvac_power_states"] is loaded["hvac_power"]
        assert kwargs["load_unit"] == "W"
        assert kwargs["hvac_power_unit"] == "kW"
        return {
            "model_version": 1,
            "source_entity_id": "sensor.house_load",
            "trained_at": now.isoformat(),
            "status": "ready",
        }

    monkeypatch.setattr(recorder_import, "_load_household_forecast_states", fake_load)
    monkeypatch.setattr(recorder_import, "build_load_forecast_model", fake_build)
    hass = ForecastHass(
        {
            "sensor.house_load": CurrentState("2000", "W"),
            "sensor.hvac_power": CurrentState("0.5", "kW"),
        }
    )

    model, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            hass,
            {
                CONF_HOUSEHOLD_LOAD: "sensor.house_load",
                CONF_EV_CHARGING: "sensor.ev_charging",
                CONF_DAIKIN_POWER: "sensor.hvac_power",
            },
            {},
            now=now,
            timezone="Australia/Melbourne",
        )
    )

    assert changed is True
    assert reason == "load_forecast_ready"
    assert model["status"] == "ready"
    assert "load" not in model
    assert loader_calls[0][1:4] == (
        "sensor.house_load",
        "sensor.ev_charging",
        "sensor.hvac_power",
    )


def test_builtin_load_forecast_retains_last_model_on_recorder_error(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {"model_version": 1, "source_entity_id": "sensor.house_load", "trained_at": "bad"}

    def fail(*args: Any) -> dict[str, list[Any]]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(recorder_import, "_load_household_forecast_states", fail)
    model, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            existing,
            now=now,
            timezone="UTC",
        )
    )

    assert model is not existing
    assert model["source_entity_id"] == existing["source_entity_id"]
    assert model["last_attempt_at"] == now.isoformat()
    assert changed is True
    assert reason == "load_forecast_recorder_unavailable:RuntimeError"
    assert (
        recorder_import.training_due(model, now=now + timedelta(hours=1), source_entity_id="sensor.house_load")
        is False
    )


def test_builtin_load_forecast_force_retrains_recent_model(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {
        "model_version": 1,
        "source_entity_id": "sensor.house_load",
        "trained_at": now.isoformat(),
        "last_attempt_at": now.isoformat(),
        "last_attempt_source_entity_id": "sensor.house_load",
        "last_attempt_timezone": "UTC",
        "timezone": "UTC",
    }
    calls = 0

    def fake_load(*args: Any) -> dict[str, list[Any]]:
        nonlocal calls
        calls += 1
        return {"load": [], "ev_charging": [], "hvac_power": []}

    monkeypatch.setattr(recorder_import, "_load_household_forecast_states", fake_load)
    skipped = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            existing,
            now=now,
            timezone="UTC",
        )
    )
    forced = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            existing,
            now=now,
            timezone="UTC",
            force=True,
        )
    )

    assert skipped == (existing, False, "load_forecast_training_recent")
    assert calls == 1
    assert forced[0]["status"] == "learning"


def test_household_forecast_history_loader_uses_recorder_state_changes(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    state = RecorderState("1", now - timedelta(hours=1))
    calls: list[dict[str, Any]] = []

    def state_changes(*args: Any, **kwargs: Any) -> dict[str, list[RecorderState]]:
        calls.append(kwargs)
        return {kwargs["entity_id"]: [state]}

    monkeypatch.setattr(
        recorder_import,
        "import_module",
        lambda name: types.SimpleNamespace(state_changes_during_period=state_changes),
    )

    histories = recorder_import._load_household_forecast_states(
        object(),
        "sensor.house_load",
        None,
        None,
        now - timedelta(days=1),
        now,
    )

    assert histories == {"load": [state], "ev_charging": [], "hvac_power": []}
    assert calls == [
        {
            "entity_id": "sensor.house_load",
            "no_attributes": True,
            "include_start_time_state": True,
            "significant_changes_only": False,
        }
    ]


def test_retraining_quality_failure_retains_last_ready_aggregate(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {
        "model_version": 1,
        "contract_version": 1,
        "status": "ready",
        "quality_ready": True,
        "source_entity_id": "sensor.house_load",
        "timezone": "UTC",
        "trained_at": (now - timedelta(hours=12)).isoformat(),
        "profiles": {"retained": True},
    }
    monkeypatch.setattr(
        recorder_import,
        "_load_household_forecast_states",
        lambda *args: {"load": [], "ev_charging": [], "hvac_power": []},
    )
    monkeypatch.setattr(
        recorder_import,
        "build_load_forecast_model",
        lambda *args, **kwargs: {
            "status": "failed",
            "quality_failures": ["forecast_accuracy_below_persistence_gate"],
            "validation": {"mae_kw": 2.0},
        },
    )

    retained, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            existing,
            now=now,
            timezone="UTC",
            force=True,
        )
    )

    assert changed is True
    assert reason == "load_forecast_retraining_failed_retained"
    assert retained["trained_at"] == existing["trained_at"]
    assert retained["profiles"] == {"retained": True}
    assert retained["last_training_status"] == "failed"
    assert retained["last_training_validation"] == {"mae_kw": 2.0}


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

    history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            hass,
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            now=now,
        )
    )

    assert changed is True
    assert reason == "recorder_imported"
    assert history["recorder_imported_at"] == now.isoformat()
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

    history, changed, reason = asyncio.run(
        recorder_import.async_import_ev_trip_history_from_recorder(
            hass,
            {CONF_EV_CONNECTED: "binary_sensor.ev_connected", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            now=now,
        )
    )

    assert changed is True
    assert reason == "recorder_imported"
    assert history["recorder_imported_at"] == now.isoformat()
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
