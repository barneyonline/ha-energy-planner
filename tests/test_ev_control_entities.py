"""Tests for the remaining native EV control entities."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner import number as number_module
from custom_components.ha_energy_planner import time as time_module
from custom_components.ha_energy_planner.const import (
    CONF_DEFAULT_READY_BY,
    CONF_EV_LOW_PRICE_THRESHOLD,
)
from custom_components.ha_energy_planner.number import EVOpportunisticChargingPriceThresholdNumber
from custom_components.ha_energy_planner.time import EVReadyByTime


class FakeCoordinator:
    """Minimal coordinator for native EV control entities."""

    def __init__(self) -> None:
        self.entry = SimpleNamespace(entry_id="entry-1")
        self.hass = SimpleNamespace(config=SimpleNamespace(currency="AUD"))
        self.price_threshold_calls: list[float] = []
        self.ready_calls: list[str] = []

    @property
    def options(self) -> dict[str, object]:
        return {
            CONF_EV_LOW_PRICE_THRESHOLD: 0.08,
        }

    @property
    def planner_options(self) -> dict[str, object]:
        return {**self.options, CONF_DEFAULT_READY_BY: "06:45"}

    async def async_set_ev_low_price_threshold(self, value: float) -> None:
        self.price_threshold_calls.append(value)

    async def async_set_ready_by(self, value: str) -> None:
        self.ready_calls.append(value)


def test_native_ev_control_entity_setup_and_values(monkeypatch: object) -> None:
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    numbers: list[object] = []
    times: list[object] = []
    removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str:
            return f"number.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    monkeypatch.setattr(number_module.er, "async_get", lambda hass: FakeRegistry())
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

    price_threshold = numbers[0]
    ready = times[0]
    assert isinstance(price_threshold, EVOpportunisticChargingPriceThresholdNumber)
    assert price_threshold.native_value == 0.08
    assert price_threshold.native_min_value == -10
    assert price_threshold.native_max_value == 10
    assert price_threshold.native_step == 0.01
    assert price_threshold.native_unit_of_measurement == "AUD/kWh"
    assert isinstance(ready, EVReadyByTime)
    assert ready.native_value.isoformat() == "06:45:00"
    assert removed == ["number.entry-1_ev_target_soc"]


def test_native_ev_controls_update_coordinator() -> None:
    coordinator = FakeCoordinator()
    price_threshold = EVOpportunisticChargingPriceThresholdNumber(coordinator)
    ready = EVReadyByTime(coordinator)
    price_threshold.async_write_ha_state = lambda: None
    ready.async_write_ha_state = lambda: None

    asyncio.run(price_threshold.async_set_native_value(-0.03))
    asyncio.run(ready.async_set_value(ready.native_value.replace(hour=7, minute=15)))

    assert coordinator.price_threshold_calls == [-0.03]
    assert coordinator.ready_calls == ["07:15"]
