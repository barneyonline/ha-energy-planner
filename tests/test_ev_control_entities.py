"""Tests for native EV target and ready-by entities."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner import number as number_module
from custom_components.ha_energy_planner import time as time_module
from custom_components.ha_energy_planner.const import (
    CONF_DEFAULT_READY_BY,
    CONF_EV_FALLBACK_TARGET_SOC_PERCENT,
    CONF_EV_MAX_SOC_PERCENT,
    CONF_EV_MIN_SOC_PERCENT,
)
from custom_components.ha_energy_planner.number import EVTargetSOCNumber
from custom_components.ha_energy_planner.time import EVReadyByTime


class FakeCoordinator:
    """Minimal coordinator for native EV control entities."""

    def __init__(self) -> None:
        self.entry = SimpleNamespace(entry_id="entry-1")
        self.target_calls: list[float] = []
        self.ready_calls: list[str] = []

    @property
    def options(self) -> dict[str, object]:
        return {
            CONF_EV_FALLBACK_TARGET_SOC_PERCENT: 82,
            CONF_EV_MIN_SOC_PERCENT: 30,
            CONF_EV_MAX_SOC_PERCENT: 95,
        }

    @property
    def planner_options(self) -> dict[str, object]:
        return {**self.options, CONF_DEFAULT_READY_BY: "06:45"}

    async def async_set_ev_target_soc(self, value: float) -> None:
        self.target_calls.append(value)

    async def async_set_ready_by(self, value: str) -> None:
        self.ready_calls.append(value)


def test_native_ev_control_entity_setup_and_values(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(runtime_data=coordinator)
    numbers: list[object] = []
    times: list[object] = []
    monkeypatch.setattr(
        number_module,
        "async_add_planner_entities",
        lambda entry_arg, add_entities, entities: numbers.extend(entities),
    )
    monkeypatch.setattr(
        time_module,
        "async_add_planner_entities",
        lambda entry_arg, add_entities, entities: times.extend(entities),
    )

    asyncio.run(number_module.async_setup_entry(None, entry, None))
    asyncio.run(time_module.async_setup_entry(None, entry, None))

    target = numbers[0]
    ready = times[0]
    assert isinstance(target, EVTargetSOCNumber)
    assert target.native_value == 82
    assert target.native_min_value == 30
    assert target.native_max_value == 95
    assert isinstance(ready, EVReadyByTime)
    assert ready.native_value.isoformat() == "06:45:00"


def test_native_ev_controls_update_coordinator() -> None:
    coordinator = FakeCoordinator()
    target = EVTargetSOCNumber(coordinator)
    ready = EVReadyByTime(coordinator)
    target.async_write_ha_state = lambda: None
    ready.async_write_ha_state = lambda: None

    asyncio.run(target.async_set_native_value(88))
    asyncio.run(ready.async_set_value(ready.native_value.replace(hour=7, minute=15)))

    assert coordinator.target_calls == [88]
    assert coordinator.ready_calls == ["07:15"]
