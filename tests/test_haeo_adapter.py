"""Tests for HAEO adapter behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from custom_components.ha_energy_planner import haeo_adapter as haeo_module
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.haeo_adapter import (
    HAEOAdapter,
    _first_haeo_entry_id,
    _item_time,
    _parse_datetime_or_none,
    _response_forecast_items,
    _value_from_aliases,
    apply_haeo_response_to_context,
)
from custom_components.ha_energy_planner.models import (
    ActionAsset,
    DecisionContext,
    DecisionSlot,
    HAEOSolvePhase,
    HAEOStatus,
    InputHealth,
    OccupancyState,
)
from custom_components.ha_energy_planner.planner import DryRunPlanner


class FakeServices:
    """Minimal HA service bus for HAEO tests."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return self.available and domain == "haeo" and service == "optimize"

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        self.calls.append((domain, service, data))
        return {"ok": True, "plan_id": data["plan_id"]} if return_response else {}


class FakeHass:
    """Minimal HA object."""

    def __init__(self, available: bool = True) -> None:
        self.services = FakeServices(available)


class FailingServices(FakeServices):
    """Service bus that raises immediately."""

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        self.calls.append((domain, service, data))
        raise RuntimeError("boom")


class FailingHass:
    """Minimal HA object with failing service bus."""

    def __init__(self) -> None:
        self.services = FailingServices()


class HaeoConfigEntryServices(FakeServices):
    """Service bus that models the installed HAEO optimize service schema."""

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        if return_response:
            raise AssertionError("installed HAEO optimize service does not return a response")
        if set(data) != {"config_entry"}:
            raise AssertionError(f"unexpected HAEO service data: {data}")
        self.calls.append((domain, service, data))
        return None


class FakeConfigEntry:
    """Minimal HA config entry."""

    entry_id = "haeo-entry-1"


class FakeConfigEntries:
    """Minimal config entries manager."""

    def async_entries(self, domain: str) -> list[FakeConfigEntry]:
        return [FakeConfigEntry()] if domain == "haeo" else []


class HaeoConfigEntryHass:
    """Minimal HA object with a real HAEO config entry."""

    def __init__(self) -> None:
        self.services = HaeoConfigEntryServices()
        self.config_entries = FakeConfigEntries()


class LegacyFallbackServices(FakeServices):
    """Service bus that does not support return_response."""

    def __init__(self, *, fallback_fails: bool = False) -> None:
        super().__init__(available=True)
        self.fallback_fails = fallback_fails

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        blocking: bool = False,
        return_response: bool = False,
    ) -> dict[str, Any]:
        if return_response:
            raise TypeError("return_response is not supported")
        self.calls.append((domain, service, data))
        if self.fallback_fails:
            raise RuntimeError("fallback failed")
        return None


class LegacyFallbackHass:
    """Minimal HA object with legacy service behavior."""

    def __init__(self, *, fallback_fails: bool = False) -> None:
        self.services = LegacyFallbackServices(fallback_fails=fallback_fails)


def _context() -> DecisionContext:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return DecisionContext(
        created_at=now,
        plan_id="plan-1",
        slots=[
            DecisionSlot(
                valid_at=now,
                import_price=0.2,
                export_price=0.05,
                pv_forecast_kw=1,
                baseline_load_forecast_kw=2,
                projected_ev_load_kw=1.5,
            ),
            DecisionSlot(
                valid_at=now + timedelta(minutes=5),
                import_price=0.2,
                export_price=0.05,
                pv_forecast_kw=1,
                baseline_load_forecast_kw=2,
            ),
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=60,
        occupancy_state=OccupancyState.OCCUPIED,
        haeo_status=HAEOStatus.READY,
        input_health=InputHealth.HEALTHY,
    )


def test_haeo_baseline_calls_configured_service() -> None:
    hass = FakeHass()
    context = _context()
    result = asyncio.run(HAEOAdapter(hass, "haeo.optimize").async_solve_baseline(context))
    assert result.status == HAEOStatus.READY
    assert result.phase == HAEOSolvePhase.BASELINE
    assert hass.services.calls[0][0:2] == ("haeo", "optimize")
    assert hass.services.calls[0][2]["phase"] == "baseline"
    assert hass.services.calls[0][2]["plan_id"] == "plan-1"


def test_haeo_second_pass_sends_flexible_projection() -> None:
    hass = FakeHass()
    context = _context()
    projections = DryRunPlanner({}).project_flexible_loads(context)
    result = asyncio.run(HAEOAdapter(hass, "haeo.optimize").async_solve_with_flexible_load(context, projections))
    assert result.status == HAEOStatus.READY
    assert hass.services.calls[0][2]["phase"] == "flexible_load"
    assert hass.services.calls[0][2]["flexible_load_projection"] == [
        {
            "valid_at": "2026-06-27T00:00:00+00:00",
            "ev_load_kw": 1.5,
            "hvac_load_kw": 0.0,
        }
    ]


def test_real_haeo_optimize_service_uses_config_entry_schema() -> None:
    hass = HaeoConfigEntryHass()
    context = _context()
    adapter = HAEOAdapter(hass, "haeo.optimize")

    result = asyncio.run(adapter.async_solve_baseline(context))

    assert result.status == HAEOStatus.READY
    assert result.response is None
    assert hass.services.calls == [("haeo", "optimize", {"config_entry": "haeo-entry-1"})]
    assert adapter.supports_flexible_second_pass is False


def test_native_haeo_skips_unsupported_flexible_solve() -> None:
    hass = HaeoConfigEntryHass()
    adapter = HAEOAdapter(hass, "haeo.optimize")

    result = asyncio.run(
        adapter.async_solve_with_flexible_load(_context(), DryRunPlanner({}).project_flexible_loads(_context()))
    )

    assert result.status == HAEOStatus.STALE
    assert result.reason == "haeo_flexible_projection_unsupported"
    assert hass.services.calls == []
    assert adapter.last_call_metadata["capabilities"]["supports_response"] is False


def test_native_haeo_requires_explicit_selection_when_multiple_entries() -> None:
    class Entry:
        def __init__(self, entry_id: str) -> None:
            self.entry_id = entry_id

    class Entries:
        def async_entries(self, domain: str) -> list[Entry]:
            return [Entry("haeo-z"), Entry("haeo-a")] if domain == "haeo" else []

    hass = HaeoConfigEntryHass()
    hass.config_entries = Entries()
    ambiguous = HAEOAdapter(hass, "haeo.optimize")

    result = asyncio.run(ambiguous.async_solve_baseline(_context()))

    assert result.status == HAEOStatus.FAILED
    assert result.reason == "haeo_config_entry_ambiguous"
    assert hass.services.calls == []

    selected = HAEOAdapter(hass, "haeo.optimize", "haeo-z")
    result = asyncio.run(selected.async_solve_baseline(_context()))
    assert result.status == HAEOStatus.READY
    assert hass.services.calls == [("haeo", "optimize", {"config_entry": "haeo-z"})]


def test_response_capable_custom_service_detects_projection_contract() -> None:
    class Services(FakeServices):
        def async_services(self) -> dict[str, dict[str, object]]:
            return {
                "haeo": {
                    "optimize": SimpleNamespace(
                        supports_response="optional",
                        schema={"flexible_load_projection": object(), "phase": object()},
                    )
                }
            }

    hass = FakeHass()
    hass.services = Services()
    adapter = HAEOAdapter(hass, "haeo.optimize")

    result = asyncio.run(
        adapter.async_solve_with_flexible_load(_context(), DryRunPlanner({}).project_flexible_loads(_context()))
    )

    assert result.status == HAEOStatus.READY
    assert adapter.supports_flexible_second_pass is True
    assert adapter.last_call_metadata["capabilities"]["source"] == "service_registry"
    assert adapter.last_call_metadata["response_received"] is True


def test_haeo_cache_reuses_unchanged_inputs_and_invalidates_on_boundary() -> None:
    hass = FakeHass()
    adapter = HAEOAdapter(hass, "haeo.optimize")
    context = _context()

    first = asyncio.run(adapter.async_solve_baseline(context))
    first_metadata = dict(adapter.last_call_metadata)
    context.plan_id = "plan-2"
    context.created_at += timedelta(seconds=5)
    cached = asyncio.run(adapter.async_solve_baseline(context))

    assert first.status == cached.status == HAEOStatus.READY
    assert cached.plan_id == "plan-2"
    assert len(hass.services.calls) == 1
    assert first_metadata["cache_hit"] is False
    assert adapter.last_call_metadata["cache_hit"] is True
    assert adapter.last_call_metadata["duration_ms"] >= 0

    for slot in context.slots:
        slot.valid_at += timedelta(seconds=5)
    asyncio.run(adapter.async_solve_baseline(context))
    assert len(hass.services.calls) == 1
    assert adapter.last_call_metadata["cache_hit"] is True

    context.slots[0].valid_at += timedelta(minutes=5)
    asyncio.run(adapter.async_solve_baseline(context))
    assert len(hass.services.calls) == 2
    assert adapter.last_call_metadata["cache_hit"] is False


def test_haeo_type_error_fails_without_retrying_side_effectful_solve() -> None:
    hass = LegacyFallbackHass()
    context = _context()

    result = asyncio.run(HAEOAdapter(hass, "haeo.optimize").async_solve_baseline(context))

    assert result.status == HAEOStatus.FAILED
    assert result.response is None
    assert result.reason == "haeo_service_failed:TypeError"
    assert hass.services.calls == []


def test_haeo_type_error_does_not_enter_legacy_fallback() -> None:
    hass = LegacyFallbackHass(fallback_fails=True)
    context = _context()

    result = asyncio.run(HAEOAdapter(hass, "haeo.optimize").async_solve_baseline(context))

    assert result.status == HAEOStatus.FAILED
    assert result.reason == "haeo_service_failed:TypeError"
    assert result.service_called == "haeo.optimize"
    assert hass.services.calls == []


def test_haeo_response_populates_forecast_evidence_on_context_slots() -> None:
    context = _context()
    response = {
        "result": {
            "slots": [
                {
                    "valid_at": "2026-06-27T00:00:00+00:00",
                    "grid_import_kw": 2.5,
                    "grid_export_kw": 0.0,
                    "battery_charge_kw": 1.5,
                    "battery_discharge_kw": 0.0,
                    "battery_soc_percent": 55,
                },
                {
                    "valid_at": "2026-06-27T00:05:00+00:00",
                    "grid_import_kw": 0.0,
                    "grid_export_kw": 2.0,
                    "battery_charge_kw": 0.0,
                    "battery_discharge_kw": 1.25,
                    "battery_soc_percent": 53,
                },
            ]
        }
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts["haeo_grid_import_forecast_kw"] == 2
    assert counts["haeo_battery_discharge_forecast_kw"] == 2
    assert context.slots[0].haeo_grid_import_forecast_kw == 2.5
    assert context.slots[0].haeo_battery_charge_forecast_kw == 1.5
    assert context.slots[0].haeo_battery_soc_forecast_percent == 55
    assert context.slots[1].haeo_grid_export_forecast_kw == 2.0
    assert context.slots[1].haeo_battery_discharge_forecast_kw == 1.25


def test_haeo_response_matches_refresh_jitter_within_planning_boundary() -> None:
    context = _context()
    for slot in context.slots:
        slot.valid_at += timedelta(seconds=17)

    counts = apply_haeo_response_to_context(
        context,
        {
            "slots": [
                {"valid_at": "2026-06-27T00:00:00+00:00", "grid_import_kw": 2.0},
                {"valid_at": "2026-06-27T00:05:00+00:00", "grid_import_kw": 1.0},
            ]
        },
    )

    assert counts == {"haeo_grid_import_forecast_kw": 2}
    assert [slot.haeo_grid_import_forecast_kw for slot in context.slots] == [2.0, 1.0]


def test_haeo_response_rejects_non_finite_forecast_evidence() -> None:
    context = _context()
    response = {
        "result": {
            "slots": [
                {
                    "valid_at": "2026-06-27T00:00:00+00:00",
                    "grid_import_kw": "nan",
                    "grid_export_kw": "inf",
                    "battery_charge_kw": "-inf",
                    "battery_discharge_kw": 1.25,
                    "battery_soc_percent": "nan",
                }
            ]
        }
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts == {"haeo_battery_discharge_forecast_kw": 1}
    assert context.slots[0].haeo_grid_import_forecast_kw is None
    assert context.slots[0].haeo_grid_export_forecast_kw is None
    assert context.slots[0].haeo_battery_charge_forecast_kw is None
    assert context.slots[0].haeo_battery_discharge_forecast_kw == 1.25
    assert context.slots[0].haeo_battery_soc_forecast_percent is None


def test_haeo_response_matches_naive_timestamps_to_context_timezone() -> None:
    context = _context()
    response = {
        "result": {
            "slots": [
                {
                    "valid_at": "2026-06-27T00:05:00",
                    "grid_import_kw": 3.0,
                }
            ]
        }
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts == {"haeo_grid_import_forecast_kw": 1}
    assert context.slots[0].haeo_grid_import_forecast_kw is None
    assert context.slots[1].haeo_grid_import_forecast_kw == 3.0


def test_haeo_response_converts_megawatt_power_units_to_kw() -> None:
    context = _context()
    response = {
        "result": {
            "slots": [
                {
                    "valid_at": "2026-06-27T00:00:00+00:00",
                    "unit": "MW",
                    "grid_import": 0.003,
                    "battery_charge": 0.002,
                }
            ]
        }
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts == {
        "haeo_grid_import_forecast_kw": 1,
        "haeo_battery_charge_forecast_kw": 1,
    }
    assert context.slots[0].haeo_grid_import_forecast_kw == 3.0
    assert context.slots[0].haeo_battery_charge_forecast_kw == 2.0


def test_haeo_response_parses_camel_case_live_export_keys() -> None:
    context = _context()
    response = {
        "result": {
            "slots": [
                {
                    "startTime": "2026-06-27T00:00:00+00:00",
                    "gridImportW": 1800,
                    "gridExportW": 0,
                    "batteryChargeW": 1200,
                    "batteryDischargeW": 0,
                    "batterySocPercent": 61,
                },
                {
                    "startTime": "2026-06-27T00:05:00+00:00",
                    "gridImportW": 0,
                    "gridExportW": 1400,
                    "batteryChargeW": 0,
                    "batteryDischargeW": 900,
                    "batterySocPercent": 59,
                },
            ]
        }
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts == {
        "haeo_grid_import_forecast_kw": 2,
        "haeo_grid_export_forecast_kw": 2,
        "haeo_battery_charge_forecast_kw": 2,
        "haeo_battery_discharge_forecast_kw": 2,
        "haeo_battery_soc_forecast_percent": 2,
    }
    assert context.slots[0].haeo_grid_import_forecast_kw == 1.8
    assert context.slots[0].haeo_battery_charge_forecast_kw == 1.2
    assert context.slots[1].haeo_grid_export_forecast_kw == 1.4
    assert context.slots[1].haeo_battery_discharge_forecast_kw == 0.9
    assert context.slots[1].haeo_battery_soc_forecast_percent == 59


def test_haeo_adapter_rejects_missing_invalid_and_unavailable_service() -> None:
    context = _context()

    missing = asyncio.run(HAEOAdapter(FakeHass(), None).async_solve_baseline(context))
    invalid = asyncio.run(HAEOAdapter(FakeHass(), "badservice").async_solve_baseline(context))
    unavailable = asyncio.run(HAEOAdapter(FakeHass(available=False), "haeo.optimize").async_solve_baseline(context))

    assert missing.status == HAEOStatus.STALE
    assert missing.reason == "haeo_service_not_configured"
    assert invalid.status == HAEOStatus.FAILED
    assert invalid.reason == "haeo_service_invalid"
    assert unavailable.status == HAEOStatus.STALE
    assert unavailable.reason == "haeo_service_unavailable"
    assert unavailable.service_called == "haeo.optimize"


def test_haeo_adapter_reports_direct_service_exception() -> None:
    result = asyncio.run(HAEOAdapter(FailingHass(), "haeo.optimize").async_solve_baseline(_context()))

    assert result.status == HAEOStatus.FAILED
    assert result.reason == "haeo_service_failed:RuntimeError"
    assert result.service_called == "haeo.optimize"


def test_first_haeo_entry_id_handles_missing_or_unexpected_managers() -> None:
    class NoEntries:
        pass

    class TypeErrorEntries:
        def async_entries(self, domain: str) -> list[object]:
            raise TypeError("wrong args")

    class EmptyEntry:
        entry_id = ""

    class EmptyEntries:
        def async_entries(self, domain: str) -> list[object]:
            return [EmptyEntry()]

    assert _first_haeo_entry_id(object()) is None
    assert _first_haeo_entry_id(type("Hass", (), {"config_entries": NoEntries()})()) is None
    assert _first_haeo_entry_id(type("Hass", (), {"config_entries": TypeErrorEntries()})()) is None
    assert _first_haeo_entry_id(type("Hass", (), {"config_entries": EmptyEntries()})()) is None


def test_haeo_response_ignores_unmatched_slots_and_non_dict_items() -> None:
    context = _context()
    response = {
        "slots": [
            {"valid_at": "2026-06-28T00:00:00+00:00", "grid_import_kw": 9},
            {"valid_at": "not-a-date", "grid_import_kw": 8},
            {"grid_import_kw": 7},
            {"grid_import_kw": "bad"},
        ]
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts == {"haeo_grid_import_forecast_kw": 1}
    assert context.slots[0].haeo_grid_import_forecast_kw is None
    assert context.slots[1].haeo_grid_import_forecast_kw == 8


def test_haeo_response_item_parsing_helpers_cover_invalid_shapes() -> None:
    assert _response_forecast_items(None) == []
    assert _response_forecast_items([{"value": 1}, "bad"]) == []
    assert _response_forecast_items("bad") == []
    assert _response_forecast_items({"outer": {"values": [{"value": 1}]}}) == [{"value": 1}]
    assert _response_forecast_items({"2026-06-27T00:00:00+00:00": 1}) == [
        {"valid_at": "2026-06-27T00:00:00+00:00", "value": 1}
    ]
    assert _item_time({"time": datetime(2026, 6, 27, tzinfo=UTC)}) == datetime(2026, 6, 27, tzinfo=UTC)
    assert _item_time({"time": 123, "date": "bad"}) is None
    assert _value_from_aliases({"value": "bad"}, ("value",)) is None
    assert _value_from_aliases({"value": "nan"}, ("value",)) is None
    assert _value_from_aliases({}, ("value",)) is None
    assert _parse_datetime_or_none(datetime(2026, 6, 27, tzinfo=UTC)) == datetime(2026, 6, 27, tzinfo=UTC)
    assert _parse_datetime_or_none(123) is None
    assert _parse_datetime_or_none("bad") is None


def test_haeo_response_skips_timestamped_items_outside_context_horizon() -> None:
    context = _context()
    response = {
        "result": {
            "slots": [
                {
                    "valid_at": "2026-06-28T00:00:00+00:00",
                    "grid_import_kw": 3.0,
                },
                {
                    "grid_export_kw": 1.5,
                },
            ]
        }
    }

    counts = apply_haeo_response_to_context(context, response)

    assert counts == {"haeo_grid_export_forecast_kw": 1}
    assert context.slots[0].haeo_grid_import_forecast_kw is None
    assert context.slots[0].haeo_grid_export_forecast_kw is None
    assert context.slots[1].haeo_grid_export_forecast_kw == 1.5


def test_haeo_response_parses_timestamp_keyed_nested_watt_evidence_for_enphase_value() -> None:
    context = _context()
    context.current_enphase_profile = "AI Optimisation"
    context.enphase_ai_profile = "AI Optimisation"
    context.enphase_self_consumption_profile = "Self-Consumption"
    context.enphase_full_backup_profile = "Full Backup"
    context.current_ev_soc_percent = None
    context.slots[0].import_price = 0.10
    context.slots[0].export_price = 0.05
    context.slots[1].import_price = 0.50
    context.slots[1].export_price = 0.40
    response = {
        "result": {
            "schedule": {
                "2026-06-27T00:00:00+00:00": {
                    "grid": {"import_w": 2000, "export_w": 0},
                    "battery": {"charge_w": 2000, "discharge_w": 0, "soc_percent": 55},
                },
                "2026-06-27T00:05:00+00:00": {
                    "grid": {"import_w": 0, "export_w": 2000},
                    "battery": {"charge_w": 0, "discharge_w": 2000, "soc_percent": 54},
                },
            }
        }
    }

    counts = apply_haeo_response_to_context(context, response)
    plan = DryRunPlanner(
        {
            **DEFAULT_OPTIONS,
            "planner_enabled": True,
            "dry_run": False,
            "enphase_minimum_savings": 0.04,
            "planning_interval_minutes": 5,
        }
    ).create_plan(context)

    assert counts["haeo_grid_import_forecast_kw"] == 2
    assert counts["haeo_battery_charge_forecast_kw"] == 2
    assert context.slots[0].haeo_grid_import_forecast_kw == 2.0
    assert context.slots[0].haeo_battery_charge_forecast_kw == 2.0
    assert context.slots[1].haeo_grid_export_forecast_kw == 2.0
    assert context.slots[1].haeo_battery_discharge_forecast_kw == 2.0
    assert plan.actions[0].asset == ActionAsset.ENPHASE
    assert plan.actions[0].desired_state["profile"] == "Full Backup"
    assert plan.actions[0].desired_state["arbitrage_source"] == "haeo_battery_arbitrage_value"
    assert plan.actions[0].desired_state["arbitrage_direction"] == "charge"
    assert plan.actions[0].expected_cost_delta == 0.05


def test_haeo_unavailable_degrades_without_service_call() -> None:
    hass = FakeHass(available=False)
    result = asyncio.run(HAEOAdapter(hass, "haeo.optimize").async_solve_baseline(_context()))
    assert result.status == HAEOStatus.STALE
    assert result.reason == "haeo_service_unavailable"
    assert hass.services.calls == []


def test_haeo_capability_and_registry_defensive_branches() -> None:
    class RegistryServices(FakeServices):
        def async_services(self) -> dict[str, dict[str, object]]:
            return {
                "haeo": {
                    "optimize": {
                        "supports_response": "optional",
                        "fields": {"flexible_load_projection": object()},
                    }
                }
            }

    hass = FakeHass()
    hass.services = RegistryServices()
    adapter = HAEOAdapter(hass, "haeo.optimize")
    adapter._legacy_no_response = True

    capabilities = adapter._detect_capabilities()

    assert capabilities.supports_response is False
    assert capabilities.supports_flexible_projections is True
    assert haeo_module._descriptor_supports_response({"supports_response": "optional"}) is True
    assert haeo_module._descriptor_supports_projection({"fields": {"flexible_load_projection": 1}}) is True
    assert haeo_module._descriptor_supports_projection({"schema": "invalid"}) is False

    class TypeErrorRegistry:
        def async_services(self) -> object:
            raise TypeError("unsupported")

    class NonDictRegistry:
        def async_services(self) -> object:
            return []

    assert (
        haeo_module._service_descriptor(SimpleNamespace(services=TypeErrorRegistry()), "haeo", "optimize")
        is None
    )
    assert (
        haeo_module._service_descriptor(SimpleNamespace(services=NonDictRegistry()), "haeo", "optimize")
        is None
    )


def test_haeo_cache_expiry_capacity_and_disabled_edges(monkeypatch: Any) -> None:
    ready = haeo_module.HAEOSolveResult(
        HAEOSolvePhase.BASELINE,
        HAEOStatus.READY,
        "ready",
        "old-plan",
    )
    stale = haeo_module.HAEOSolveResult(
        HAEOSolvePhase.BASELINE,
        HAEOStatus.STALE,
        "stale",
        "old-plan",
    )

    disabled = HAEOAdapter(FakeHass(), "haeo.optimize", cache_ttl_seconds=0)
    disabled._store_cached_result("disabled", ready)
    assert disabled._cache == {}

    adapter = HAEOAdapter(FakeHass(), "haeo.optimize", cache_ttl_seconds=30)
    adapter._store_cached_result("stale", stale)
    assert adapter._cache == {}

    adapter._cache["expired"] = (0.0, ready)
    monkeypatch.setattr(haeo_module, "monotonic", lambda: 31.0)
    monkeypatch.setattr(adapter, "_expire_cache", lambda now: None)
    assert adapter._cached_result("expired", "new-plan") is None
    assert "expired" not in adapter._cache

    capacity = HAEOAdapter(FakeHass(), "haeo.optimize", cache_ttl_seconds=30)
    monkeypatch.setattr(haeo_module, "monotonic", lambda: 0.0)
    for index in range(haeo_module._CACHE_MAX_ENTRIES + 1):
        capacity._store_cached_result(str(index), ready)
    assert len(capacity._cache) == haeo_module._CACHE_MAX_ENTRIES
    assert "0" not in capacity._cache

    capacity._cache["old"] = (0.0, ready)
    capacity._expire_cache(31.0)
    assert capacity._cache == {}


def test_haeo_entry_selection_and_fingerprint_edges() -> None:
    assert haeo_module._select_haeo_entry_id(["entry-a"], "missing") == (
        None,
        "haeo_config_entry_not_found",
    )
    assert haeo_module._select_haeo_entry_id([], None) == (None, "haeo_config_entry_not_found")
    assert haeo_module._fingerprint_value(float("inf")) == "inf"
