"""Tests for optional Recorder imports."""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import custom_components.ha_energy_planner.recorder_import as recorder_import
from custom_components.ha_energy_planner.const import (
    CONF_DAIKIN_POWER,
    CONF_EV_CHARGING,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
)
from custom_components.ha_energy_planner.load_forecast import (
    FORECAST_CONTRACT_VERSION,
    MODEL_VERSION,
    build_load_forecast_model,
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
    assert unavailable == {"existing": True}
    assert unavailable_changed is False
    assert unavailable_reason == "load_forecast_household_load_unavailable"


@pytest.mark.parametrize("state_value", ["unknown", "unavailable"])
def test_builtin_load_forecast_defers_unknown_startup_states(state_value: str) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {"existing": True}

    result = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState(state_value)}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            existing,
            now=now,
            timezone="UTC",
            force=True,
        )
    )

    assert result == (existing, False, "load_forecast_household_load_unavailable")


def test_startup_source_appearance_retries_training_without_backoff(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {
        "model_version": MODEL_VERSION,
        "contract_version": FORECAST_CONTRACT_VERSION,
        "safety_gates_bypassed": False,
        "status": "ready",
        "quality_ready": True,
        "source_entity_id": "sensor.house_load",
        "timezone": "UTC",
        "trained_at": (now - timedelta(hours=1)).isoformat(),
        "last_attempt_at": (now - timedelta(hours=1)).isoformat(),
        "last_attempt_source_entity_id": "sensor.house_load",
        "last_attempt_timezone": "UTC",
    }
    calls = 0

    def fake_train(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "model_version": MODEL_VERSION,
            "contract_version": FORECAST_CONTRACT_VERSION,
            "safety_gates_bypassed": False,
            "status": "ready",
            "quality_ready": True,
            "quality_failures": [],
            "source_entity_id": "sensor.house_load",
            "trained_at": (now + timedelta(seconds=1)).isoformat(),
        }

    monkeypatch.setattr(recorder_import, "_build_household_load_forecast_model", fake_train)
    unavailable = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            existing,
            now=now,
            timezone="UTC",
            force=True,
        )
    )
    retrained = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            unavailable[0],
            now=now + timedelta(seconds=1),
            timezone="UTC",
            force=True,
        )
    )

    assert unavailable == (existing, False, "load_forecast_household_load_unavailable")
    assert calls == 1
    assert retrained[1:] == (True, "load_forecast_ready")
    assert retrained[0]["contract_version"] == FORECAST_CONTRACT_VERSION


def test_builtin_load_forecast_loads_optional_cleaning_histories_and_persists_only_model(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    training_calls: list[tuple[Any, ...]] = []

    def fake_train(*args: Any) -> dict[str, Any]:
        training_calls.append(args)
        assert args[1:4] == ("sensor.house_load", "sensor.ev_charging", "sensor.hvac_power")
        assert args[-3:] == ("W", "kW", False)
        return {
            "model_version": 1,
            "source_entity_id": "sensor.house_load",
            "trained_at": now.isoformat(),
            "status": "ready",
        }

    monkeypatch.setattr(recorder_import, "_build_household_load_forecast_model", fake_train)
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
    assert len(training_calls) == 1


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
    assert model["last_training_status"] == "failed"
    assert model["last_training_quality_failures"] == ["recorder_unavailable"]
    assert model["model_version"] == MODEL_VERSION
    assert model["contract_version"] == FORECAST_CONTRACT_VERSION
    assert model["status"] == "failed"
    assert model["profiles"] == {}
    assert changed is True
    assert reason == "load_forecast_recorder_unavailable:LoadForecastRecorderError"
    assert (
        recorder_import.training_due(model, now=now + timedelta(hours=1), source_entity_id="sensor.house_load")
        is False
    )


def test_builtin_load_forecast_reports_dense_history_as_actionable(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    def fail(*args: Any) -> dict[str, Any]:
        raise recorder_import.LoadForecastHistoryLimitError("sensor.house_load")

    monkeypatch.setattr(recorder_import, "_build_household_load_forecast_model", fail)
    model, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            {},
            now=now,
            timezone="UTC",
        )
    )

    assert changed is True
    assert reason == "load_forecast_history_limit_exceeded:LoadForecastHistoryLimitError"
    assert model["last_training_quality_failures"] == ["history_limit_exceeded"]


def test_builtin_load_forecast_delays_internal_training_error(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    def fail(*args: Any) -> dict[str, Any]:
        raise ValueError("unexpected model failure")

    monkeypatch.setattr(recorder_import, "_build_household_load_forecast_model", fail)
    model, changed, reason = asyncio.run(
        recorder_import.async_update_builtin_load_forecast(
            ForecastHass({"sensor.house_load": CurrentState("1")}),
            {CONF_HOUSEHOLD_LOAD: "sensor.house_load"},
            {},
            now=now,
            timezone="UTC",
        )
    )

    assert changed is True
    assert reason == "load_forecast_training_error:ValueError"
    assert model["last_training_quality_failures"] == ["training_error"]
    assert model["unusable_since"] == now.isoformat()


def test_builtin_load_forecast_force_retrains_recent_model(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {
        "model_version": MODEL_VERSION,
        "contract_version": FORECAST_CONTRACT_VERSION,
        "safety_gates_bypassed": False,
        "source_entity_id": "sensor.house_load",
        "trained_at": now.isoformat(),
        "last_attempt_at": now.isoformat(),
        "last_attempt_source_entity_id": "sensor.house_load",
        "last_attempt_timezone": "UTC",
        "timezone": "UTC",
    }
    calls = 0

    def fake_train(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "learning", "quality_failures": ["insufficient_training_days"]}

    monkeypatch.setattr(recorder_import, "_build_household_load_forecast_model", fake_train)
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

    def state_changes(
        hass: Any,
        start_time: datetime,
        end_time: datetime,
        *,
        entity_id: str,
        no_attributes: bool,
        include_start_time_state: bool,
        limit: int,
    ) -> dict[str, list[RecorderState]]:
        calls.append(
            {
                "entity_id": entity_id,
                "no_attributes": no_attributes,
                "include_start_time_state": include_start_time_state,
                "limit": limit,
            }
        )
        return {entity_id: [state]}

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
            "no_attributes": False,
            "include_start_time_state": True,
            "limit": recorder_import.MAX_LOAD_FORECAST_STATES_PER_ENTITY_CHUNK,
        }
    ]


def test_retraining_quality_failure_retains_last_ready_aggregate(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {
        "model_version": MODEL_VERSION,
        "contract_version": FORECAST_CONTRACT_VERSION,
        "safety_gates_bypassed": False,
        "status": "ready",
        "quality_ready": True,
        "source_entity_id": "sensor.house_load",
        "timezone": "UTC",
        "trained_at": (now - timedelta(hours=12)).isoformat(),
        "profiles": {"retained": True},
    }
    monkeypatch.setattr(
        recorder_import,
        "_build_household_load_forecast_model",
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


def test_household_forecast_builder_queries_bounded_utc_chunks(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, 12, 7, tzinfo=UTC)
    windows: list[tuple[datetime, datetime]] = []

    def fake_load(
        hass: Any,
        load_entity: str,
        ev_entity: str | None,
        hvac_entity: str | None,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[Any]]:
        windows.append((start, end))
        return {
            "load": [RecorderState("1", start)],
            "ev_charging": [],
            "hvac_power": [],
        }

    monkeypatch.setattr(recorder_import, "_load_household_forecast_states", fake_load)

    model = recorder_import._build_household_load_forecast_model(
        object(),
        "sensor.house_load",
        None,
        None,
        now,
        "UTC",
        "kW",
        "",
    )

    assert len(windows) == 5
    assert all(end - start <= timedelta(days=7) for start, end in windows)
    assert all(end.minute == 0 and end.second == 0 for _start, end in windows[:-1])
    assert model["source_entity_id"] == "sensor.house_load"
    assert model["status"] == "ready"


def test_chunked_forecast_training_matches_monolithic_history(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, 12, 7, tzinfo=UTC)
    start = now - timedelta(days=12)
    load: list[RecorderState] = []
    cursor = start
    while cursor <= now:
        load.append(RecorderState(str(1.0 + (cursor.hour >= 17) * 0.5), cursor))
        cursor += timedelta(minutes=15)
    ev = [RecorderState("off", start)]
    hvac = [RecorderState("0.2", start)]

    def sliced(states: list[RecorderState], chunk_start: datetime, chunk_end: datetime) -> list[RecorderState]:
        prior = [state for state in states if state.last_updated <= chunk_start]
        result = [prior[-1]] if prior else []
        result.extend(state for state in states if chunk_start < state.last_updated < chunk_end)
        return result

    def fake_load(
        hass: Any,
        load_entity: str,
        ev_entity: str | None,
        hvac_entity: str | None,
        chunk_start: datetime,
        chunk_end: datetime,
    ) -> dict[str, list[Any]]:
        return {
            "load": sliced(load, chunk_start, chunk_end),
            "ev_charging": sliced(ev, chunk_start, chunk_end),
            "hvac_power": sliced(hvac, chunk_start, chunk_end),
        }

    monkeypatch.setattr(recorder_import, "_load_household_forecast_states", fake_load)
    chunked = recorder_import._build_household_load_forecast_model(
        object(),
        "sensor.house_load",
        "binary_sensor.ev_charging",
        "sensor.hvac_power",
        now,
        "UTC",
        "kW",
        "kW",
    )
    monolithic = build_load_forecast_model(
        load,
        now=now,
        timezone="UTC",
        source_entity_id="sensor.house_load",
        load_unit="kW",
        ev_charging_states=ev,
        hvac_power_states=hvac,
        hvac_power_unit="kW",
    )

    assert chunked["profiles"] == monolithic["profiles"]
    assert chunked["validation"] == monolithic["validation"]
    assert chunked["history_coverage"] == monolithic["history_coverage"]
    assert chunked["cleaning"] == monolithic["cleaning"]


def test_household_forecast_builder_rejects_dense_chunk(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    dense = [RecorderState("1", now - timedelta(days=28))] * 2
    monkeypatch.setattr(recorder_import, "MAX_LOAD_FORECAST_STATES_PER_ENTITY_CHUNK", 2)
    monkeypatch.setattr(
        recorder_import,
        "_load_household_forecast_states",
        lambda *args: {"load": dense, "ev_charging": [], "hvac_power": []},
    )

    try:
        recorder_import._build_household_load_forecast_model(
            object(),
            "sensor.house_load",
            None,
            None,
            now,
            "UTC",
            "kW",
            "",
        )
    except recorder_import.LoadForecastHistoryLimitError as err:
        assert str(err) == "sensor.house_load"
    else:
        raise AssertionError("dense Recorder history was not rejected")


def test_ev_charge_calibration_trains_from_recorder_and_skips_recent_model(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=2)
    monkeypatch.setattr(
        recorder_import,
        "_load_recorder_states",
        lambda *args: (
            [RecorderState("charging", start), RecorderState("idle", start + timedelta(hours=1))],
            [RecorderState("50", start), RecorderState("64", start + timedelta(hours=1))],
        ),
    )
    entry_data = {
        CONF_EV_CHARGING: "sensor.charger_status",
        CONF_EV_SOC: "sensor.vehicle_soc",
    }

    model, changed, reason = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(), entry_data, {}, charge_rate_kw=7, now=now
        )
    )
    recent, recent_changed, recent_reason = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(), entry_data, model, charge_rate_kw=7, now=now + timedelta(hours=1)
        )
    )

    assert changed is True
    assert reason == "ev_charge_calibration_ready"
    assert model["soc_per_kwh"] == 1.8
    assert recent is model
    assert recent_changed is False
    assert recent_reason == "ev_charge_calibration_recent"


def test_ev_charge_calibration_retains_ready_model_when_retraining_is_insufficient(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    model = {
        "model_version": 1,
        "status": "ready",
        "trained_at": (now - timedelta(days=2)).isoformat(),
        "charging_entity_id": "sensor.charger_status",
        "soc_entity_id": "sensor.vehicle_soc",
        "charge_rate_kw": 7.0,
        "soc_per_kwh": 1.8,
    }
    monkeypatch.setattr(recorder_import, "_load_recorder_states", lambda *args: ([], []))

    retained, changed, reason = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(),
            {
                CONF_EV_CHARGING: "sensor.charger_status",
                CONF_EV_SOC: "sensor.vehicle_soc",
            },
            model,
            charge_rate_kw=7,
            now=now,
        )
    )

    assert changed is True
    assert reason == "ev_charge_calibration_insufficient_history_retained"
    assert retained["status"] == "ready"
    assert retained["soc_per_kwh"] == 1.8
    assert retained["last_attempt_status"] == "insufficient_history"


def test_ev_charge_calibration_requires_inputs_and_retains_model_on_recorder_error(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {"status": "ready", "soc_per_kwh": 1.8}

    missing = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(), {}, existing, charge_rate_kw=7, now=now
        )
    )
    invalid = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(),
            {CONF_EV_CHARGING: "sensor.charger", CONF_EV_SOC: "sensor.soc"},
            existing,
            charge_rate_kw=0,
            now=now,
        )
    )
    monkeypatch.setattr(
        recorder_import,
        "_load_recorder_states",
        lambda *args: (_ for _ in ()).throw(RuntimeError("recorder down")),
    )
    failed = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(),
            {CONF_EV_CHARGING: "sensor.charger", CONF_EV_SOC: "sensor.soc"},
            existing,
            charge_rate_kw=7,
            now=now,
        )
    )

    assert missing == (existing, False, "ev_charge_calibration_entities_not_configured")
    assert invalid == (existing, False, "ev_charge_calibration_charge_rate_invalid")
    assert failed == (existing, False, "ev_charge_calibration_unavailable:RuntimeError")


def test_ev_calibration_prefers_recorder_database_executor(monkeypatch: Any) -> None:
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

    model, changed, reason = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            hass,
            {CONF_EV_CHARGING: "binary_sensor.ev_charging", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            charge_rate_kw=7,
            now=now,
        )
    )

    assert changed is True
    assert reason == "ev_charge_calibration_insufficient_history"
    assert model["trained_at"] == now.isoformat()
    assert recorder_instance.calls == 1
    assert hass.generic_executor_calls == 0


def test_ev_calibration_falls_back_to_home_assistant_executor(monkeypatch: Any) -> None:
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

    model, changed, reason = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            hass,
            {CONF_EV_CHARGING: "binary_sensor.ev_charging", CONF_EV_SOC: "sensor.ev_soc"},
            {},
            charge_rate_kw=7,
            now=now,
        )
    )

    assert changed is True
    assert reason == "ev_charge_calibration_insufficient_history"
    assert model["trained_at"] == now.isoformat()
    assert hass.generic_executor_calls == 1


def test_ev_calibration_reports_bounded_history_limit(monkeypatch: Any) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    existing = {"status": "ready", "soc_per_kwh": 1.8}

    def fake_load(*args: Any) -> tuple[list[Any], list[Any]]:
        raise recorder_import.EVHistoryLimitError("sensor.ev_soc")

    monkeypatch.setattr(recorder_import, "_load_bounded_ev_history", fake_load)

    model, changed, reason = asyncio.run(
        recorder_import.async_update_ev_charge_calibration(
            FakeHass(),
            {CONF_EV_CHARGING: "binary_sensor.ev_charging", CONF_EV_SOC: "sensor.ev_soc"},
            existing,
            charge_rate_kw=7,
            now=now,
        )
    )

    assert model == existing
    assert changed is False
    assert reason == "ev_charge_calibration_history_limit_exceeded"


def test_bounded_ev_history_splits_dense_chunks_and_compacts(monkeypatch: Any) -> None:
    start = datetime(2026, 6, 27, tzinfo=UTC)
    end = start + timedelta(hours=2)
    durations: list[timedelta] = []

    def fake_load(
        hass: Any,
        charging_entity: str,
        soc_entity: str,
        chunk_start: datetime,
        chunk_end: datetime,
    ) -> tuple[list[RecorderState], list[RecorderState]]:
        duration = chunk_end - chunk_start
        durations.append(duration)
        count = 4 if duration > timedelta(hours=1) else 3
        step = duration / max(count - 1, 1)
        charging = [
            RecorderState("idle" if index % 2 == 0 else "charging", chunk_start + step * index)
            for index in range(count)
        ]
        soc = [
            RecorderState(str(80 - index), chunk_start + step * index)
            for index in range(count)
        ]
        return charging, soc

    monkeypatch.setattr(recorder_import, "MAX_EV_HISTORY_STATES_PER_ENTITY_CHUNK", 4)
    monkeypatch.setattr(recorder_import, "_load_recorder_states", fake_load)

    charging, soc = recorder_import._load_bounded_ev_history(
        object(),
        "binary_sensor.ev_charging",
        "sensor.ev_soc",
        start,
        end,
    )

    assert durations == [timedelta(hours=2), timedelta(hours=1), timedelta(hours=1)]
    assert len(charging) == 5
    assert len(soc) == 6
    assert all(
        earlier.last_changed <= later.last_changed
        for earlier, later in zip(charging, charging[1:], strict=False)
    )


def test_bounded_ev_history_reuses_proven_subday_chunk_span(monkeypatch: Any) -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    durations: list[timedelta] = []

    def fake_load(
        hass: Any,
        charging_entity: str,
        soc_entity: str,
        chunk_start: datetime,
        chunk_end: datetime,
    ) -> tuple[list[object], list[object]]:
        duration = chunk_end - chunk_start
        durations.append(duration)
        count = 2 if duration > timedelta(hours=1) else 1
        return [object()] * count, [object()] * count

    monkeypatch.setattr(recorder_import, "MAX_EV_HISTORY_STATES_PER_ENTITY_CHUNK", 2)
    monkeypatch.setattr(recorder_import, "_load_recorder_states", fake_load)
    monkeypatch.setattr(
        recorder_import,
        "_compact_ev_history_chunk",
        lambda charging, soc: ([], []),
    )

    recorder_import._load_bounded_ev_history(
        object(),
        "binary_sensor.ev_charging",
        "sensor.ev_soc",
        start,
        start + timedelta(days=30),
    )

    assert len(durations) == 728
    assert sum(duration == timedelta(hours=1) for duration in durations) == 720
    assert all(duration <= timedelta(hours=1) for duration in durations[8:])


def test_bounded_ev_history_rejects_excessive_compacted_rows(monkeypatch: Any) -> None:
    start = datetime(2026, 6, 27, tzinfo=UTC)
    states = [RecorderState("off", start + timedelta(minutes=index)) for index in range(3)]

    monkeypatch.setattr(recorder_import, "MAX_COMPACT_EV_HISTORY_STATES", 2)
    monkeypatch.setattr(
        recorder_import,
        "_load_recorder_states",
        lambda *args: (states, states),
    )
    monkeypatch.setattr(
        recorder_import,
        "_compact_ev_history_chunk",
        lambda charging, soc: (charging, soc),
    )

    with pytest.raises(recorder_import.EVHistoryLimitError, match="compacted_ev_history"):
        recorder_import._load_bounded_ev_history(
            object(),
            "binary_sensor.ev_charging",
            "sensor.ev_soc",
            start,
            start + timedelta(hours=1),
        )


def test_bounded_ev_history_rejects_one_hour_chunk_at_query_limit(monkeypatch: Any) -> None:
    start = datetime(2026, 6, 27, tzinfo=UTC)
    dense = [RecorderState("off", start)] * 2
    monkeypatch.setattr(recorder_import, "MAX_EV_HISTORY_STATES_PER_ENTITY_CHUNK", 2)
    monkeypatch.setattr(recorder_import, "_load_recorder_states", lambda *args: (dense, []))

    with pytest.raises(
        recorder_import.EVHistoryLimitError,
        match="binary_sensor.ev_charging",
    ):
        recorder_import._load_bounded_ev_history(
            object(),
            "binary_sensor.ev_charging",
            "sensor.ev_soc",
            start,
            start + timedelta(hours=1),
        )


def test_recorder_state_timestamp_rejects_rows_without_datetimes() -> None:
    assert recorder_import._recorder_state_timestamp(object()) is None
    assert recorder_import._recorder_state_timestamp(
        types.SimpleNamespace(last_changed="not-a-datetime", last_updated=1)
    ) is None


def test_recorder_load_states_uses_recorder_history_module(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []
    charging_states = [object()]
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
            limit: int,
        ) -> dict[str, list[Any]]:
            calls.append(
                {
                    "entity_id": entity_id,
                    "no_attributes": no_attributes,
                    "include_start_time_state": include_start_time_state,
                    "limit": limit,
                }
            )
            return {
                "binary_sensor.ev_charging": charging_states,
                "sensor.ev_soc": soc_states,
            }

        return types.SimpleNamespace(state_changes_during_period=state_changes_during_period)

    monkeypatch.setattr(recorder_import, "import_module", fake_import_module)

    charging, soc = recorder_import._load_recorder_states(
        FakeHass(),
        "binary_sensor.ev_charging",
        "sensor.ev_soc",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 27, tzinfo=UTC),
    )

    assert charging == charging_states
    assert soc == soc_states
    assert [call["entity_id"] for call in calls] == ["binary_sensor.ev_charging", "sensor.ev_soc"]
    assert all(
        call["limit"] == recorder_import.MAX_EV_HISTORY_STATES_PER_ENTITY_CHUNK
        for call in calls
    )


def test_ev_calibration_due_handles_invalid_and_naive_timestamps() -> None:
    aware_now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    naive_now = datetime(2026, 6, 27, 12, 0)
    kwargs = {
        "charging_entity": "sensor.charger",
        "soc_entity": "sensor.soc",
        "charge_rate_kw": 7,
    }
    matching = {
        "model_version": 1,
        "charging_entity_id": "sensor.charger",
        "soc_entity_id": "sensor.soc",
        "charge_rate_kw": 7,
    }

    assert recorder_import._ev_charge_calibration_due({}, now=aware_now, **kwargs) is True
    assert recorder_import._ev_charge_calibration_due(
        {**matching, "trained_at": "bad"}, now=aware_now, **kwargs
    ) is True
    assert recorder_import._ev_charge_calibration_due(matching, now=aware_now, **kwargs) is True
    assert recorder_import._ev_charge_calibration_due(
        {**matching, "trained_at": "2026-06-26T00:00:00"}, now=aware_now, **kwargs
    ) is True
    assert recorder_import._ev_charge_calibration_due(
        {**matching, "trained_at": "2026-06-26T00:00:00+00:00"}, now=naive_now, **kwargs
    ) is True
    assert recorder_import._ev_charge_calibration_matches(
        {**matching, "charge_rate_kw": "bad"}, **kwargs
    ) is False
