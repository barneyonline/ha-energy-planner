"""Background training ownership and publication interleavings."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from custom_components.ha_energy_planner import training as module
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS


class Hass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def async_create_background_task(self, coroutine: Any, name: str) -> asyncio.Task:
        return asyncio.create_task(coroutine)


def request(source: str = "sensor.load") -> module.TrainingRequest:
    return module.training_request({"household_load_entity": source}, DEFAULT_OPTIONS, {}, "UTC")


def test_training_coalesces_sources_and_publishes_only_latest(monkeypatch: Any) -> None:
    async def run() -> None:
        hass = Hass()
        release = asyncio.Event()
        started = asyncio.Event()
        calls: list[str] = []
        published: list[module.TrainingResult] = []

        async def ev(hass: Any, data: dict, model: dict, **kwargs: Any) -> tuple:
            calls.append(data["household_load_entity"])
            started.set()
            await release.wait()
            return {"status": "ready"}, True, "ev_ready"

        async def load(*args: Any, **kwargs: Any) -> tuple:
            return {"status": "ready"}, True, "load_ready"

        async def publish(generation: int, request: Any, result: Any) -> None:
            published.append(result)

        monkeypatch.setattr(module, "async_update_ev_charge_calibration", ev)
        monkeypatch.setattr(module, "async_update_builtin_load_forecast", load)
        manager = module.HistoryTraining(hass, "entry", publish)
        manager.request(request())
        await started.wait()
        manager.request(request())
        manager.request(request("sensor.other"))
        release.set()
        await manager.task
        assert calls == ["sensor.load", "sensor.other"]
        assert len(published) == 1
        assert manager.last_result is published[0]
        assert manager.load_training_attempted
        manager.request(request("sensor.other"))
        await manager.task
        assert len(published) == 2
        manager.stop()
        manager.request(request())
        assert manager.pending is None
    asyncio.run(run())


def test_reload_and_repeated_cancellation_keep_single_flight_lock(monkeypatch: Any) -> None:
    async def run() -> None:
        hass = Hass()
        release = asyncio.Event()
        started = asyncio.Event()
        calls: list[str] = []
        published: list[str] = []

        async def ev(hass: Any, data: dict, model: dict, **kwargs: Any) -> tuple:
            calls.append(data["household_load_entity"])
            started.set()
            await release.wait()
            return {}, False, "unchanged"

        async def load(*args: Any, **kwargs: Any) -> tuple:
            return {}, False, "load_forecast_household_load_unavailable"

        async def publish(generation: int, request: Any, result: Any) -> None:
            published.append(request.entry_data["household_load_entity"])

        monkeypatch.setattr(module, "async_update_ev_charge_calibration", ev)
        monkeypatch.setattr(module, "async_update_builtin_load_forecast", load)
        old = module.HistoryTraining(hass, "entry", publish)
        old.request(request("sensor.old"))
        await started.wait()
        old.stop()
        old.task.cancel()
        await asyncio.sleep(0)
        old.task.cancel()
        await asyncio.sleep(0)
        new = module.HistoryTraining(hass, "entry", publish)
        new.request(request("sensor.new"))
        await asyncio.sleep(0)
        assert calls == ["sensor.old"]
        assert old._lock.locked()
        release.set()
        await asyncio.gather(old.task, return_exceptions=True)
        await new.task
        assert calls == ["sensor.old", "sensor.new"]
        assert published == ["sensor.new"]
        assert not new.load_training_attempted
    asyncio.run(run())


def test_queued_superseded_training_does_not_import(monkeypatch: Any) -> None:
    async def run() -> None:
        hass = Hass()
        published: list[str] = []
        async def publish(generation: int, request: Any, result: Any) -> None:
            published.append(request.entry_data["household_load_entity"])
        manager = module.HistoryTraining(hass, "entry", publish)
        async def train(item: module.TrainingRequest) -> module.TrainingResult:
            return module.TrainingResult({}, {}, False, False, "ev", "load")
        monkeypatch.setattr(manager, "_train", train)
        await manager._lock.acquire()
        manager.request(request())
        await asyncio.sleep(0)
        manager.request(request("sensor.latest"))
        manager._lock.release()
        await manager.task
        assert published == ["sensor.latest"]
    asyncio.run(run())


def test_training_failure_is_bounded_and_request_models_are_detached(monkeypatch: Any, caplog: Any) -> None:
    async def run() -> None:
        async def publish(*args: Any) -> None:
            raise RuntimeError("private provider output")
        manager = module.HistoryTraining(Hass(), "entry", publish)
        async def train(item: module.TrainingRequest) -> module.TrainingResult:
            return module.TrainingResult({}, {}, False, False, "ev", "load")
        monkeypatch.setattr(manager, "_train", train)
        manager.request(request())
        await manager.task
        assert "History training failed: RuntimeError" in caplog.text
        assert "private provider output" not in caplog.text
        base = request()
        assert replace(base, charge_rate_kw=11).identity != base.identity
        assert replace(base, timezone="Australia/Melbourne").identity != base.identity
        assert replace(base, bypass_safety_gates=True).identity != base.identity
    asyncio.run(run())
