"""Conditional thresholds through HA validation and persisted options saves."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_energy_planner.config_flow import INPUT_STEP_EV
from custom_components.ha_energy_planner.const import (
    CONF_EV_EARLIEST_START,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    CONF_EV_LOW_PRICE_THRESHOLD,
    CONF_EV_MAX_IMPORT_PRICE,
    CONF_EV_PRICE_LIMIT_ENABLED,
    DEFAULT_OPTIONS,
    DOMAIN,
)


@asynccontextmanager
async def _disabled_planner(
    tmp_path: Path, options: dict[str, Any],
) -> AsyncIterator[tuple[HomeAssistant, ConfigEntry]]:
    """Load a real entry without starting device control."""
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")
    hass = HomeAssistant(str(tmp_path))
    try:
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await asyncio.wait_for(er.async_load(hass, load_empty=True), timeout=10)
        entry = ConfigEntry(
            domain=DOMAIN, title="Disabled planner", data={}, options=options,
            source="user", unique_id=None, version=5, minor_version=2,
            discovery_keys=MappingProxyType({}), subentries_data=[], disabled_by=ConfigEntryDisabler.USER,
        )
        hass.config_entries._entries[entry.entry_id] = entry
        yield hass, entry
    finally:
        await hass.async_stop(force=True)


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("toggle,threshold", [
    (CONF_EV_PRICE_LIMIT_ENABLED, CONF_EV_MAX_IMPORT_PRICE),
    (CONF_EV_LOW_PRICE_CHARGING_ENABLED, CONF_EV_LOW_PRICE_THRESHOLD),
])
def test_conditional_ev_options_survive_validated_saves(
    tmp_path: Path, saved: bool, toggle: str, threshold: str,
) -> None:
    """Validate whole forms, save, and reopen with real HA options machinery."""

    async def run() -> None:
        original = {threshold: -0.05, toggle: True} if saved else {}
        expected = original.get(threshold, DEFAULT_OPTIONS[threshold])
        edits = (
            {toggle: False}, {CONF_EV_EARLIEST_START: "01:00"}, {toggle: True},
            {threshold: 0.42}, {toggle: False, threshold: 0.91}, {toggle: True},
        )
        async with _disabled_planner(tmp_path, original) as (hass, entry):
            for edit in edits:
                menu = await hass.config_entries.options.async_init(entry.entry_id)
                flow_id = menu["flow_id"]
                form = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "settings"})
                assert form["step_id"] == "settings"
                ev = next(group for marker, group in form["data_schema"].schema.items()
                          if marker.schema == INPUT_STEP_EV)
                marker = next(marker for marker in ev.schema.schema if marker.schema == threshold)
                assert marker.default() == expected
                assert marker.visible == {"field": toggle, "value": True}
                # Submit every required section. Let HA, rather than a direct
                # async_step call, apply defaults and strip hidden/stale input.
                submitted = {marker.schema: {} for marker in form["data_schema"].schema}
                submitted[INPUT_STEP_EV] = edit
                enabled = edit.get(toggle, entry.options.get(toggle, DEFAULT_OPTIONS[toggle]))
                if threshold in edit and enabled:
                    expected = edit[threshold]
                result = await hass.config_entries.options.async_configure(flow_id, submitted)
                assert result["type"] == "create_entry"
                assert entry.options[threshold] == expected
                assert entry.options[toggle] is enabled
                assert entry.disabled_by is ConfigEntryDisabler.USER
                # Changing one toggle must not overwrite the other threshold.
                other = (CONF_EV_LOW_PRICE_THRESHOLD if threshold == CONF_EV_MAX_IMPORT_PRICE
                         else CONF_EV_MAX_IMPORT_PRICE)
                assert entry.options[other] == DEFAULT_OPTIONS[other]
            assert entry.options[CONF_EV_EARLIEST_START] == "01:00"

    asyncio.run(run())


def test_departure_priority_prices_are_saved_only_when_visible(tmp_path: Path) -> None:
    """Hidden policy edits are discarded and the saved values reopen intact."""
    departure = "departure_priority"
    ceiling = "hard_ceiling"
    policy, price, budget = "ev_price_policy", "ev_emergency_price", "ev_emergency_budget"
    original = {policy: departure, price: 0.6, budget: 5.0, CONF_EV_MAX_IMPORT_PRICE: 0.3}
    stale = {price: 0.9, budget: 9.0}
    edits = (
        ({CONF_EV_PRICE_LIMIT_ENABLED: False, policy: ceiling, **stale}, departure, 0.6, 5.0),
        ({CONF_EV_PRICE_LIMIT_ENABLED: True}, departure, 0.6, 5.0),
        ({policy: ceiling, **stale}, ceiling, 0.6, 5.0),
        ({policy: departure, price: 0.7, budget: 7.0}, departure, 0.7, 7.0),
        ({CONF_EV_PRICE_LIMIT_ENABLED: False, policy: ceiling, **stale}, departure, 0.7, 7.0),
        ({CONF_EV_PRICE_LIMIT_ENABLED: True}, departure, 0.7, 7.0),
    )

    async def run() -> None:
        async with _disabled_planner(tmp_path, original) as (hass, entry):
            for edit, expected_policy, expected_price, expected_budget in edits:
                menu = await hass.config_entries.options.async_init(entry.entry_id)
                flow_id = menu["flow_id"]
                form = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "settings"})
                ev = next(group for marker, group in form["data_schema"].schema.items()
                          if marker.schema == INPUT_STEP_EV)
                markers = {marker.schema: marker for marker in ev.schema.schema}
                for field in (policy, price, budget):
                    assert markers[field].default() == entry.options[field]
                submitted = {marker.schema: {} for marker in form["data_schema"].schema}
                submitted[INPUT_STEP_EV] = edit
                result = await hass.config_entries.options.async_configure(flow_id, submitted)
                assert result["type"] == "create_entry"
                assert entry.options[policy] == expected_policy
                assert entry.options[price] == expected_price
                assert entry.options[budget] == expected_budget

    asyncio.run(run())
