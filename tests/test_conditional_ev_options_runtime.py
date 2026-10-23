"""Conditional thresholds through HA validation and persisted options saves."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import MappingProxyType
from typing import Required

import probatio as vol
import pytest
from homeassistant import data_entry_flow, loader
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


@pytest.mark.parametrize("use_visibility", [False, True])
@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("toggle,threshold", [
    (CONF_EV_PRICE_LIMIT_ENABLED, CONF_EV_MAX_IMPORT_PRICE),
    (CONF_EV_LOW_PRICE_CHARGING_ENABLED, CONF_EV_LOW_PRICE_THRESHOLD),
])
def test_conditional_ev_options_survive_validated_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_visibility: bool, saved: bool,
    toggle: str, threshold: str,
) -> None:
    """Validate whole forms, save, and reopen with real HA options machinery."""
    required = getattr(data_entry_flow, "Required", None)
    if use_visibility:
        if not (isinstance(required, type) and issubclass(required, vol.Required)):
            pytest.skip("Conditional field validation requires HA 2026.11 or newer")
    else:
        # Exercise the original unconditional schema even on a newer runtime.
        monkeypatch.setattr(data_entry_flow, "Required", Required, raising=False)

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "custom_components/ha_energy_planner", tmp_path / "custom_components/ha_energy_planner")

    async def run() -> None:
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        original = {threshold: -0.05, toggle: True} if saved else {}
        entry = ConfigEntry(
            domain=DOMAIN, title="Disabled planner", data={}, options=original,
            source="user", unique_id=None, version=5, minor_version=2,
            discovery_keys=MappingProxyType({}), subentries_data=[], disabled_by=ConfigEntryDisabler.USER,
        )
        hass.config_entries._entries[entry.entry_id] = entry
        expected = original.get(threshold, DEFAULT_OPTIONS[threshold])
        edits = (
            {toggle: False}, {CONF_EV_EARLIEST_START: "01:00"}, {toggle: True},
            {threshold: 0.42}, {toggle: False, threshold: 0.91}, {toggle: True},
        )
        try:
            for edit in edits:
                menu = await hass.config_entries.options.async_init(entry.entry_id)
                flow_id = menu["flow_id"]
                form = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "settings"})
                assert form["step_id"] == "settings"
                ev = next(group for marker, group in form["data_schema"].schema.items()
                          if marker.schema == INPUT_STEP_EV)
                marker = next(marker for marker in ev.schema.schema if marker.schema == threshold)
                assert marker.default() == expected
                assert getattr(marker, "visible", None) == (
                    {"field": toggle, "value": True} if use_visibility else None
                )
                # Submit every required section. Let HA, rather than a direct
                # async_step call, apply defaults and strip hidden/stale input.
                submitted = {marker.schema: {} for marker in form["data_schema"].schema}
                submitted[INPUT_STEP_EV] = edit
                enabled = edit.get(toggle, entry.options.get(toggle, DEFAULT_OPTIONS[toggle]))
                if threshold in edit and (enabled or not use_visibility):
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
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())
