"""Executable fixtures for representative Home Assistant integration schemas."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.ha_energy_planner.forecasts import forecast_series_from_state
from custom_components.ha_energy_planner.haeo_adapter import apply_haeo_response_to_context
from custom_components.ha_energy_planner.models import (
    DecisionContext,
    DecisionSlot,
    HAEOStatus,
    InputHealth,
    OccupancyState,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live_schema"


@dataclass(slots=True)
class FakeState:
    """Minimal state with attributes."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def _fixtures() -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))]


def test_representative_live_schema_fixtures_parse_successfully() -> None:
    for fixture in _fixtures():
        if fixture["kind"] == "forecast_state":
            _assert_forecast_fixture(fixture)
        elif fixture["kind"] == "haeo_response":
            _assert_haeo_fixture(fixture)
        else:
            raise AssertionError(f"Unsupported fixture kind: {fixture['kind']}")


def test_v1_real_profile_reports_missing_fixture_names() -> None:
    validator = _load_validator()

    missing = validator._profile_missing_names("ha-energy-planner-v1-real", _fixtures())

    assert missing == [
        "real_amber_export",
        "real_amber_import",
        "real_baseline_load",
        "real_haeo_response",
        "real_pv_hafo",
        "real_weather",
    ]


def test_v1_real_profile_accepts_required_fixture_names() -> None:
    validator = _load_validator()
    fixtures = [
        {
            "name": "real_amber_import",
            "kind": "forecast_state",
            "value_kind": "price",
            "source_entity_id": "sensor.amber_import",
        },
        {
            "name": "real_amber_export",
            "kind": "forecast_state",
            "value_kind": "price",
            "source_entity_id": "sensor.amber_export",
        },
        {
            "name": "real_pv_hafo",
            "kind": "forecast_state",
            "value_kind": "power",
            "source_entity_id": "sensor.pv_forecast",
        },
        {
            "name": "real_baseline_load",
            "kind": "forecast_state",
            "value_kind": "power",
            "source_entity_id": "sensor.baseline_load",
        },
        {
            "name": "real_weather",
            "kind": "forecast_state",
            "value_kind": "temperature",
            "source_entity_id": "weather.home",
        },
        {"name": "real_haeo_response", "kind": "haeo_response", "source_service": "haeo.optimize"},
    ]

    assert validator._profile_missing_names("ha-energy-planner-v1-real", fixtures) == []
    assert validator._profile_errors("ha-energy-planner-v1-real", fixtures) == {}


def test_v1_real_profile_reports_mismatched_fixture_metadata() -> None:
    validator = _load_validator()
    fixtures = [
        {"name": "real_amber_import", "kind": "forecast_state", "value_kind": "power"},
        {"name": "real_amber_export", "kind": "forecast_state", "value_kind": "price"},
        {"name": "real_pv_hafo", "kind": "forecast_state", "value_kind": "power"},
        {"name": "real_baseline_load", "kind": "forecast_state", "value_kind": "power"},
        {"name": "real_weather", "kind": "forecast_state", "value_kind": "temperature"},
        {"name": "real_haeo_response", "kind": "forecast_state", "value_kind": "price"},
    ]

    errors = validator._profile_errors("ha-energy-planner-v1-real", fixtures)

    assert errors["mismatched_fixtures"] == [
        {
            "name": "real_amber_import",
            "expected": {"kind": "forecast_state", "value_kind": "price"},
            "actual": {"kind": "forecast_state", "value_kind": "power"},
        },
        {
            "name": "real_haeo_response",
            "expected": {"kind": "haeo_response"},
            "actual": {"kind": "forecast_state"},
        },
    ]


def test_v1_real_profile_reports_missing_export_source_metadata() -> None:
    validator = _load_validator()
    fixtures = [
        {
            "name": "real_amber_import",
            "kind": "forecast_state",
            "value_kind": "price",
            "source_entity_id": "",
        },
        {
            "name": "real_amber_export",
            "kind": "forecast_state",
            "value_kind": "price",
            "source_entity_id": "<redacted>",
        },
        {
            "name": "real_pv_hafo",
            "kind": "forecast_state",
            "value_kind": "power",
            "source_entity_id": "sensor.pv",
        },
        {
            "name": "real_baseline_load",
            "kind": "forecast_state",
            "value_kind": "power",
            "source_entity_id": "sensor.load",
        },
        {
            "name": "real_weather",
            "kind": "forecast_state",
            "value_kind": "temperature",
            "source_entity_id": "weather.home",
        },
        {"name": "real_haeo_response", "kind": "haeo_response"},
    ]

    errors = validator._profile_errors("ha-energy-planner-v1-real", fixtures)

    assert errors["missing_source_fields"] == [
        {"name": "real_amber_export", "missing_fields": ["source_entity_id"]},
        {"name": "real_amber_import", "missing_fields": ["source_entity_id"]},
        {"name": "real_haeo_response", "missing_fields": ["source_service"]},
    ]


def test_haeo_value_profile_accepts_grid_and_battery_value_evidence() -> None:
    validator = _load_validator()
    fixture = {
        "name": "real_haeo_response",
        "kind": "haeo_response",
        "source_service": "haeo.optimize",
        "issued_at": "2026-06-27T00:00:00+00:00",
        "interval_minutes": 5,
        "slot_count": 2,
        "response": {
            "slots": [
                {
                    "gridImportW": 1200,
                    "gridExportW": 0,
                    "batteryChargeW": 500,
                    "batteryDischargeW": 0,
                    "batterySocPercent": 55,
                },
                {
                    "gridImportW": 0,
                    "gridExportW": 6000,
                    "batteryChargeW": 0,
                    "batteryDischargeW": 6000,
                    "batterySocPercent": 52,
                },
            ]
        },
    }

    assert validator._profile_errors("ha-energy-planner-haeo-value-v1-real", [fixture]) == {}


def test_haeo_value_profile_reports_missing_value_evidence() -> None:
    validator = _load_validator()
    fixture = {
        "name": "real_haeo_response",
        "kind": "haeo_response",
        "source_service": "haeo.optimize",
        "issued_at": "2026-06-27T00:00:00+00:00",
        "interval_minutes": 5,
        "slot_count": 1,
        "response": {
            "slots": [
                {
                    "gridImportW": 1200,
                    "batteryChargeW": 500,
                    "batterySocPercent": 55,
                }
            ]
        },
    }

    errors = validator._profile_errors("ha-energy-planner-haeo-value-v1-real", [fixture])

    assert errors["missing_haeo_value_evidence"] == {
        "haeo_grid_export_forecast_kw": {"expected_min": 1, "actual": 0},
        "haeo_battery_discharge_forecast_kw": {"expected_min": 1, "actual": 0},
    }


def test_haeo_value_profile_requires_real_haeo_response_fixture() -> None:
    validator = _load_validator()

    assert validator._profile_missing_names("ha-energy-planner-haeo-value-v1-real", _fixtures()) == [
        "real_haeo_response"
    ]


def _assert_forecast_fixture(fixture: dict[str, Any]) -> None:
    issued_at = _parse_datetime(fixture["issued_at"])
    series = forecast_series_from_state(
        FakeState(str(fixture.get("state", "")), dict(fixture.get("attributes", {}))),
        issued_at=issued_at,
        horizon_hours=int(fixture["horizon_hours"]),
        interval_minutes=int(fixture["interval_minutes"]),
        value_keys=tuple(fixture["value_keys"]),
        value_kind=str(fixture["value_kind"]),
    )

    assert series == fixture["expected"], fixture["name"]


def _assert_haeo_fixture(fixture: dict[str, Any]) -> None:
    issued_at = _parse_datetime(fixture["issued_at"])
    interval = int(fixture["interval_minutes"])
    context = DecisionContext(
        created_at=issued_at,
        plan_id="schema-fixture",
        slots=[
            DecisionSlot(
                valid_at=issued_at + timedelta(minutes=offset),
                import_price=0.2,
                export_price=0.05,
                pv_forecast_kw=1.0,
                baseline_load_forecast_kw=2.0,
            )
            for offset in range(0, len(fixture["expected_slots"]) * interval, interval)
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=None,
        occupancy_state=OccupancyState.OCCUPIED,
        haeo_status=HAEOStatus.READY,
        input_health=InputHealth.HEALTHY,
    )

    counts = apply_haeo_response_to_context(context, fixture["response"])

    assert counts == fixture["expected_counts"], fixture["name"]
    for slot, expected in zip(context.slots, fixture["expected_slots"], strict=True):
        for key, value in expected.items():
            assert getattr(slot, key) == value, f"{fixture['name']}:{key}"


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _load_validator() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "validate-live-schema-fixture.py"
    spec = importlib.util.spec_from_file_location("validate_live_schema_fixture_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
