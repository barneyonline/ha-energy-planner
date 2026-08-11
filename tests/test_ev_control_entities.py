"""Tests for retiring EV controls that moved into settings."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.ha_energy_planner import number as number_module
from custom_components.ha_energy_planner import time as time_module


def test_ev_settings_controls_are_removed_from_entity_registry(monkeypatch: object) -> None:
    entry = SimpleNamespace(entry_id="entry-1")
    numbers: list[object] = []
    times: list[object] = []
    removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str:
            return f"{platform}.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    monkeypatch.setattr(number_module.er, "async_get", lambda hass: FakeRegistry())
    monkeypatch.setattr(time_module.er, "async_get", lambda hass: FakeRegistry())

    asyncio.run(number_module.async_setup_entry(None, entry, numbers.extend))
    asyncio.run(time_module.async_setup_entry(None, entry, times.extend))

    assert numbers == []
    assert times == []
    assert removed == [
        "number.entry-1_ev_target_soc",
        "number.entry-1_ev_opportunistic_charging_price_threshold",
        "time.entry-1_ev_ready_by",
    ]
