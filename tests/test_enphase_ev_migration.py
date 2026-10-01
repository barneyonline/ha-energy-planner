"""Retired Enphase button recovery stays tied to the configured charger."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.ha_energy_planner import enphase_ev_migration as migration
from custom_components.ha_energy_planner.const import (
    CONF_EV_CHARGER,
    CONF_EV_CHARGER_START,
    CONF_EV_CHARGER_STOP,
    CONF_EV_SMART_CHARGING,
    CONF_EV_SMART_CHARGING_START,
    CONF_EV_SMART_CHARGING_STOP,
)

SWITCH = "switch.renamed_charger"
START = "button.iq_ev_charger_1234_start_charging"
STOP = "button.iq_ev_charger_1234_stop_charging"


def setup(monkeypatch, data, *, registered=None, states=None):
    entry = SimpleNamespace(data=data)
    registry = Mock()
    registry.async_get.side_effect = (registered or {}).get
    hass = SimpleNamespace(states=SimpleNamespace(get=(states or {}).get), config_entries=Mock())
    hass.config_entries.async_update_entry.side_effect = lambda entry, **kw: setattr(entry, "data", kw["data"])
    monkeypatch.setattr(migration.er, "async_get", lambda hass: registry)
    return hass, entry


def charger(**kw):
    return SimpleNamespace(platform="enphase_ev", disabled_by=None,
                           unique_id="enphase_ev_TEST1234_charging_switch", **kw)


@pytest.mark.parametrize("legacy", [False, True])
def test_retired_buttons_use_existing_renamed_switch(monkeypatch, legacy):
    switch, start, stop = ((CONF_EV_SMART_CHARGING, CONF_EV_SMART_CHARGING_START, CONF_EV_SMART_CHARGING_STOP)
                           if legacy else (CONF_EV_CHARGER, CONF_EV_CHARGER_START, CONF_EV_CHARGER_STOP))
    original = {switch: SWITCH, start: START, stop: STOP, "ev_connected_entity": "binary_sensor.plug"}
    hass, entry = setup(monkeypatch, original, registered={SWITCH: charger()})
    migration.async_migrate_retired_enphase_buttons(hass, entry)
    assert entry.data == {switch: SWITCH, "ev_connected_entity": "binary_sensor.plug"}
    assert original[start] == START and original[stop] == STOP
    hass.config_entries.async_update_entry.assert_called_once()
    migration.async_migrate_retired_enphase_buttons(hass, entry)
    hass.config_entries.async_update_entry.assert_called_once()


@pytest.mark.parametrize("data", [
    {}, {CONF_EV_CHARGER_START: None}, {CONF_EV_CHARGER_START: "button.custom_start"},
    {CONF_EV_CHARGER_START: STOP}, {CONF_EV_CHARGER_START: START},
    {CONF_EV_CHARGER_START: START, CONF_EV_CHARGER: "input_boolean.charger"},
    {CONF_EV_CHARGER_START: START, CONF_EV_CHARGER: 42},
])
def test_unidentifiable_controls_are_preserved(monkeypatch, data):
    hass, entry = setup(monkeypatch, data)
    migration.async_migrate_retired_enphase_buttons(hass, entry)
    hass.config_entries.async_update_entry.assert_not_called()
    assert entry.data == data


@pytest.mark.parametrize("change", [
    None, {"platform": "other"}, {"disabled_by": "user"},
    {"unique_id": "enphase_ev_site_TEST1234_storm_guard"},
    {"unique_id": "enphase_ev_OTHER5678_charging_switch"},
])
def test_registry_must_prove_same_enabled_charger(monkeypatch, change):
    registered = None if change is None else charger()
    if registered is not None:
        registered.__dict__.update(change)
    data = {CONF_EV_CHARGER: SWITCH, CONF_EV_CHARGER_START: START, CONF_EV_CHARGER_STOP: STOP}
    hass, entry = setup(monkeypatch, data, registered={SWITCH: registered})
    migration.async_migrate_retired_enphase_buttons(hass, entry)
    assert entry.data == data
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("present_in_registry", [True, False])
def test_existing_or_unavailable_buttons_are_not_retired(monkeypatch, present_in_registry):
    data = {CONF_EV_CHARGER: SWITCH, CONF_EV_CHARGER_START: START, CONF_EV_CHARGER_STOP: STOP}
    registered = {SWITCH: charger(), **({START: object()} if present_in_registry else {})}
    states = {} if present_in_registry else {START: SimpleNamespace(state="unavailable")}
    hass, entry = setup(monkeypatch, data, registered=registered, states=states)
    migration.async_migrate_retired_enphase_buttons(hass, entry)
    assert entry.data == {CONF_EV_CHARGER: SWITCH, CONF_EV_CHARGER_START: START}
