"""Tests for config-entry lifecycle safety."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_components.ha_energy_planner import (
    _async_migrate_duplicate_entity_ids,
    _async_remove_legacy_device,
    _async_sync_planner_devices,
    _async_update_listener,
    _freeze_config_value,
    async_setup_entry,
    async_unload_entry,
)


class FakeConfigEntries:
    """Minimal config-entry manager."""

    def __init__(self, *, fail_forward: bool = False, unload_ok: bool = True) -> None:
        self.fail_forward = fail_forward
        self.unload_ok = unload_ok
        self.updated_entries: list[Any] = []
        self.forwarded: list[tuple[Any, Any]] = []
        self.unloaded: list[tuple[Any, Any]] = []
        self.reloads: list[str] = []

    def async_update_entry(self, entry: Any, **kwargs: Any) -> None:
        self.updated_entries.append((entry, kwargs))

    async def async_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)

    async def async_forward_entry_setups(self, entry: Any, platforms: Any) -> None:
        self.forwarded.append((entry, platforms))
        if self.fail_forward:
            raise RuntimeError("platform setup failed")

    async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
        self.unloaded.append((entry, platforms))
        return self.unload_ok


@dataclass(slots=True)
class FakeHass:
    """Minimal Home Assistant object."""

    config_entries: FakeConfigEntries


@dataclass(slots=True)
class FakeEntry:
    """Minimal config entry."""

    entry_id: str = "test_entry"
    title: str = "Energy Planner"
    data: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=lambda: {"planner_enabled": False})
    runtime_data: Any = None
    unload_callbacks: list[Any] = field(default_factory=list)
    subentries: dict[str, Any] = field(
        default_factory=lambda: {
            "system": type("Subentry", (), {"subentry_id": "haep_system", "subentry_type": "system", "data": {}})(),
            "energy": type("Subentry", (), {"subentry_id": "haep_energy", "subentry_type": "energy", "data": {}})(),
            "climate": type("Subentry", (), {"subentry_id": "haep_climate", "subentry_type": "climate", "data": {}})(),
            "presence": type(
                "Subentry", (), {"subentry_id": "haep_presence", "subentry_type": "presence", "data": {}}
            )(),
            "enphase": type("Subentry", (), {"subentry_id": "haep_enphase", "subentry_type": "enphase", "data": {}})(),
            "ai": type("Subentry", (), {"subentry_id": "haep_ai", "subentry_type": "ai", "data": {}})(),
            "ev": type("Subentry", (), {"subentry_id": "haep_ev", "subentry_type": "ev", "data": {}})(),
        }
    )

    def async_on_unload(self, callback: Any) -> None:
        self.unload_callbacks.append(callback)

    def add_update_listener(self, listener: Any) -> Any:
        return listener


class FakeStore:
    """Minimal planner store."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.loaded = False

    async def async_load(self) -> None:
        self.loaded = True


class FakeCoordinator:
    """Minimal coordinator that records lifecycle calls."""

    last_instance: FakeCoordinator | None = None

    def __init__(self, hass: Any, entry: FakeEntry, store: FakeStore) -> None:
        self.hass = hass
        self.entry = entry
        self.store = store
        self.first_refresh_count = 0
        self.start_count = 0
        self.shutdown_count = 0
        self.restore_calls: list[tuple[str, bool]] = []
        self.replan_count = 0
        FakeCoordinator.last_instance = self

    async def async_config_entry_first_refresh(self) -> None:
        self.first_refresh_count += 1

    def async_start_listeners(self) -> None:
        self.start_count += 1

    def async_shutdown(self) -> None:
        self.shutdown_count += 1

    async def async_restore_safe_state(self, reason: str, *, refresh: bool = True) -> None:
        self.restore_calls.append((reason, refresh))

    async def async_request_replan(self) -> None:
        self.replan_count += 1


class FakeRuntimeCoordinator:
    """Minimal runtime coordinator for update-listener tests."""

    def __init__(self) -> None:
        self.replan_count = 0
        self.options_update_count = 0

    async def async_request_replan(self) -> None:
        self.replan_count += 1

    async def async_handle_options_update(self) -> None:
        self.options_update_count += 1
        await self.async_request_replan()


def test_unload_restores_safe_state_without_refresh() -> None:
    coordinator = FakeCoordinator(None, FakeEntry(), FakeStore(None))
    entry = FakeEntry(runtime_data=coordinator)
    hass = FakeHass(FakeConfigEntries())

    result = asyncio.run(async_unload_entry(hass, entry))

    assert result is True
    assert coordinator.shutdown_count == 1
    assert coordinator.restore_calls == [("entry_unload", False)]
    assert entry.runtime_data is None
    assert len(hass.config_entries.unloaded) == 1


def test_failed_platform_unload_keeps_coordinator_running() -> None:
    coordinator = FakeCoordinator(None, FakeEntry(), FakeStore(None))
    entry = FakeEntry(runtime_data=coordinator)
    hass = FakeHass(FakeConfigEntries(unload_ok=False))

    result = asyncio.run(async_unload_entry(hass, entry))

    assert result is False
    assert coordinator.restore_calls == [("entry_unload", False)]
    assert coordinator.shutdown_count == 0
    assert entry.runtime_data is coordinator
    assert coordinator.replan_count == 1


def test_setup_failure_restores_safe_state_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ha_energy_planner.storage.PlannerStore", FakeStore)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.EnergyPlannerCoordinator", FakeCoordinator)
    monkeypatch.setattr("custom_components.ha_energy_planner._async_remove_legacy_device", lambda hass, entry: None)
    FakeCoordinator.last_instance = None
    entry = FakeEntry()
    hass = FakeHass(FakeConfigEntries(fail_forward=True))

    with pytest.raises(RuntimeError, match="platform setup failed"):
        asyncio.run(async_setup_entry(hass, entry))

    coordinator = FakeCoordinator.last_instance
    assert coordinator is not None
    assert coordinator.first_refresh_count == 1
    assert coordinator.start_count == 1
    assert coordinator.shutdown_count == 1
    assert coordinator.restore_calls == [("setup_entry_failed", False)]
    assert entry.runtime_data is None


def test_options_update_listener_requests_replan_without_reload() -> None:
    coordinator = FakeRuntimeCoordinator()
    entry = FakeEntry(runtime_data=coordinator)
    hass = FakeHass(FakeConfigEntries())

    asyncio.run(_async_update_listener(hass, entry))

    assert coordinator.replan_count == 1
    assert coordinator.options_update_count == 1
    assert hass.config_entries.reloads == []


def test_subentry_update_listener_reloads_when_topology_changes() -> None:
    coordinator = FakeRuntimeCoordinator()
    entry = FakeEntry(runtime_data=coordinator)
    coordinator.entry_topology_signature = (
        (),
        (("haep_energy", "energy", (("amber_import_price_entity", "sensor.old_price"),)),),
    )
    entry.subentries = {
        "energy": type(
            "Subentry",
            (),
            {
                "subentry_id": "haep_energy",
                "subentry_type": "energy",
                "data": {"amber_import_price_entity": "sensor.new_price"},
            },
        )()
    }
    hass = FakeHass(FakeConfigEntries())

    asyncio.run(_async_update_listener(hass, entry))

    assert hass.config_entries.reloads == ["test_entry"]
    assert coordinator.replan_count == 0


def test_update_listener_supports_legacy_replan_runtime() -> None:
    class LegacyRuntime:
        entry_topology_signature = None

        def __init__(self) -> None:
            self.replan_count = 0

        async def async_request_replan(self) -> None:
            self.replan_count += 1

    coordinator = LegacyRuntime()
    entry = FakeEntry(runtime_data=coordinator)
    hass = FakeHass(FakeConfigEntries())

    asyncio.run(_async_update_listener(hass, entry))

    assert coordinator.replan_count == 1


def test_topology_freezer_normalizes_nested_sequences_and_sets() -> None:
    assert _freeze_config_value({"entities": ["sensor.b", {"sensor.a", "sensor.c"}]}) == (
        ("entities", ("sensor.b", ("sensor.a", "sensor.c"))),
    )


def test_setup_entry_migrates_legacy_display_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ha_energy_planner.storage.PlannerStore", FakeStore)
    monkeypatch.setattr("custom_components.ha_energy_planner.coordinator.EnergyPlannerCoordinator", FakeCoordinator)
    monkeypatch.setattr("custom_components.ha_energy_planner._async_remove_legacy_device", lambda hass, entry: None)
    monkeypatch.setattr("custom_components.ha_energy_planner._async_sync_planner_devices", lambda hass, entry: None)
    entry = FakeEntry(title="HA Energy Planner")
    hass = FakeHass(FakeConfigEntries())

    result = asyncio.run(async_setup_entry(hass, entry))

    assert result is True
    assert hass.config_entries.updated_entries[0] == (entry, {"title": "Energy Planner"})


def test_remove_legacy_device_clears_planner_entity_device_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    device = type("Device", (), {"id": "device_1"})()
    updated: list[tuple[str, dict[str, Any]]] = []
    removed: list[str] = []

    class FakeEntityRegistry:
        entities = {
            "sensor.ha_energy_planner_plan_status": type(
                "Entity",
                (),
                {
                    "platform": "ha_energy_planner",
                    "device_id": "device_1",
                    "entity_id": "sensor.ha_energy_planner_plan_status",
                },
            )(),
            "sensor.other": type(
                "Entity",
                (),
                {"platform": "other", "device_id": "device_1", "entity_id": "sensor.other"},
            )(),
        }

        def async_update_entity(self, entity_id: str, **kwargs: Any) -> None:
            updated.append((entity_id, kwargs))

    class FakeDeviceRegistry:
        def async_get_device(self, identifiers: Any) -> Any:
            assert identifiers == {("ha_energy_planner", "test_entry")}
            return device

        def async_remove_device(self, device_id: str) -> None:
            removed.append(device_id)

    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: FakeEntityRegistry())
    monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: FakeDeviceRegistry())

    _async_remove_legacy_device(FakeHass(FakeConfigEntries()), FakeEntry())

    assert updated == [("sensor.ha_energy_planner_plan_status", {"device_id": None})]
    assert removed == ["device_1"]


def test_migrate_duplicate_entity_ids_renames_only_available_planner_entities() -> None:
    updated: list[tuple[str, dict[str, Any]]] = []

    class FakeEntityRegistry:
        entities = {
            "sensor.ai_ai_advice": type(
                "Entity",
                (),
                {"platform": "ha_energy_planner", "entity_id": "sensor.ai_ai_advice"},
            )(),
            "switch.ai_ai_enabled": type(
                "Entity",
                (),
                {"platform": "ha_energy_planner", "entity_id": "switch.ai_ai_enabled"},
            )(),
            "switch.ai_enabled": type(
                "Entity",
                (),
                {"platform": "ha_energy_planner", "entity_id": "switch.ai_enabled"},
            )(),
            "sensor.ev_ev_charging_plan": type(
                "Entity",
                (),
                {"platform": "other", "entity_id": "sensor.ev_ev_charging_plan"},
            )(),
        }

        def async_update_entity(self, entity_id: str, **kwargs: Any) -> None:
            updated.append((entity_id, kwargs))

    _async_migrate_duplicate_entity_ids(FakeEntityRegistry())

    assert updated == [("sensor.ai_ai_advice", {"new_entity_id": "sensor.ai_advice"})]


def test_sync_planner_devices_creates_group_devices_and_relinks_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    updated: list[tuple[str, dict[str, Any]]] = []
    created: list[dict[str, Any]] = []
    updated_devices: list[tuple[str, dict[str, Any]]] = []
    removed: list[str] = []

    class FakeEntityRegistry:
        entities = {
            "sensor.ha_energy_planner_plan_status": type(
                "Entity",
                (),
                {
                    "platform": "ha_energy_planner",
                    "device_id": None,
                    "entity_id": "sensor.ha_energy_planner_plan_status",
                    "unique_id": "test_entry_plan_status",
                    "config_subentry_id": None,
                },
            )(),
            "sensor.ha_energy_planner_estimated_daily_cost": type(
                "Entity",
                (),
                {
                    "platform": "ha_energy_planner",
                    "device_id": "old_device",
                    "entity_id": "sensor.ha_energy_planner_estimated_daily_cost",
                    "unique_id": "test_entry_estimated_daily_cost",
                    "config_subentry_id": None,
                },
            )(),
            "switch.ha_energy_planner_ai_enabled": type(
                "Entity",
                (),
                {
                    "platform": "ha_energy_planner",
                    "device_id": "old_device",
                    "entity_id": "switch.ha_energy_planner_ai_enabled",
                    "unique_id": "test_entry_ai_enabled",
                    "config_subentry_id": None,
                },
            )(),
            "sensor.other": type(
                "Entity",
                (),
                {
                    "platform": "other",
                    "device_id": None,
                    "entity_id": "sensor.other",
                    "unique_id": "other",
                    "config_subentry_id": None,
                },
            )(),
        }

        def async_update_entity(self, entity_id: str, **kwargs: Any) -> None:
            updated.append((entity_id, kwargs))

    class FakeDeviceRegistry:
        def async_get_or_create(self, **kwargs: Any) -> Any:
            created.append(kwargs)
            device_key = next(iter(kwargs["identifiers"]))[1].removeprefix("test_entry_")
            return type("Device", (), {"id": f"{device_key}_device"})()

        def async_get_device(self, identifiers: Any) -> Any:
            if identifiers == {("ha_energy_planner", "test_entry_controls")}:
                return type("Device", (), {"id": "old_controls_device"})()
            return None

        def async_remove_device(self, device_id: str) -> None:
            removed.append(device_id)

        def async_update_device(self, device_id: str, **kwargs: Any) -> None:
            updated_devices.append((device_id, kwargs))

    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: FakeEntityRegistry())
    monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: FakeDeviceRegistry())

    _async_sync_planner_devices(FakeHass(FakeConfigEntries()), FakeEntry())

    assert [(item["name"], item["config_subentry_id"]) for item in created] == [
        ("System", "haep_system"),
        ("Energy", "haep_energy"),
        ("Climate", "haep_climate"),
        ("Presence", "haep_presence"),
        ("Enphase", "haep_enphase"),
        ("AI", "haep_ai"),
        ("EV", "haep_ev"),
    ]
    assert updated == [
        (
            "sensor.ha_energy_planner_plan_status",
            {"device_id": "system_device", "config_subentry_id": "haep_system"},
        ),
        (
            "sensor.ha_energy_planner_estimated_daily_cost",
            {"device_id": "energy_device", "config_subentry_id": "haep_energy"},
        ),
        ("switch.ha_energy_planner_ai_enabled", {"device_id": "ai_device", "config_subentry_id": "haep_ai"}),
    ]
    assert updated_devices == [
        ("system_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
        ("energy_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
        ("climate_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
        ("presence_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
        ("enphase_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
        ("ai_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
        ("ev_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
    ]
    assert removed == ["old_controls_device"]


def test_sync_planner_devices_does_not_create_optional_devices_without_subentries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated: list[tuple[str, dict[str, Any]]] = []
    updated_devices: list[tuple[str, dict[str, Any]]] = []
    created: list[dict[str, Any]] = []

    class FakeEntityRegistry:
        entities = {
            "sensor.ha_energy_planner_plan_status": type(
                "Entity",
                (),
                {
                    "platform": "ha_energy_planner",
                    "device_id": None,
                    "entity_id": "sensor.ha_energy_planner_plan_status",
                    "unique_id": "test_entry_plan_status",
                    "config_subentry_id": None,
                },
            )(),
            "sensor.ha_energy_planner_estimated_daily_cost": type(
                "Entity",
                (),
                {
                    "platform": "ha_energy_planner",
                    "device_id": None,
                    "entity_id": "sensor.ha_energy_planner_estimated_daily_cost",
                    "unique_id": "test_entry_estimated_daily_cost",
                    "config_subentry_id": None,
                },
            )(),
        }

        def async_update_entity(self, entity_id: str, **kwargs: Any) -> None:
            updated.append((entity_id, kwargs))

    class FakeDeviceRegistry:
        def async_get_or_create(self, **kwargs: Any) -> Any:
            created.append(kwargs)
            device_key = next(iter(kwargs["identifiers"]))[1].removeprefix("test_entry_")
            return type("Device", (), {"id": f"{device_key}_device"})()

        def async_get_device(self, identifiers: Any) -> None:
            return None

        def async_remove_device(self, device_id: str) -> None:
            raise AssertionError(f"Unexpected device removal: {device_id}")

        def async_update_device(self, device_id: str, **kwargs: Any) -> None:
            updated_devices.append((device_id, kwargs))

    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: FakeEntityRegistry())
    monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: FakeDeviceRegistry())

    entry = FakeEntry(
        subentries={
            "system": type(
                "Subentry",
                (),
                {"subentry_id": "haep_system", "subentry_type": "system", "data": {}},
            )(),
        }
    )

    _async_sync_planner_devices(FakeHass(FakeConfigEntries()), entry)

    assert [(item["name"], item["config_subentry_id"]) for item in created] == [("System", "haep_system")]
    assert updated == [
        (
            "sensor.ha_energy_planner_plan_status",
            {"device_id": "system_device", "config_subentry_id": "haep_system"},
        ),
    ]
    assert updated_devices == [
        ("system_device", {"remove_config_entry_id": "test_entry", "remove_config_subentry_id": None}),
    ]
