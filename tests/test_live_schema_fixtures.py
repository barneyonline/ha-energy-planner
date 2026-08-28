"""Executable fixtures for representative Home Assistant integration schemas."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from custom_components.ha_energy_planner.forecasts import forecast_series_from_state

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
        else:
            raise AssertionError(f"Unsupported fixture kind: {fixture['kind']}")


def test_v1_real_profile_reports_missing_fixture_names() -> None:
    validator = _load_validator()

    missing = validator._profile_missing_names("ha-energy-planner-v1-real", _fixtures())

    assert missing == [
        "real_amber_export",
        "real_amber_import",
        "real_pv_forecast",
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
            "name": "real_pv_forecast",
            "kind": "forecast_state",
            "value_kind": "power",
            "source_entity_id": "sensor.pv_forecast",
        },
    ]

    assert validator._profile_missing_names("ha-energy-planner-v1-real", fixtures) == []
    assert validator._profile_errors("ha-energy-planner-v1-real", fixtures) == {}


def test_v1_real_profile_reports_mismatched_fixture_metadata() -> None:
    validator = _load_validator()
    fixtures = [
        {"name": "real_amber_import", "kind": "forecast_state", "value_kind": "power"},
        {"name": "real_amber_export", "kind": "forecast_state", "value_kind": "price"},
        {"name": "real_pv_forecast", "kind": "forecast_state", "value_kind": "power"},
    ]

    errors = validator._profile_errors("ha-energy-planner-v1-real", fixtures)

    assert errors["mismatched_fixtures"] == [
        {
            "name": "real_amber_import",
            "expected": {"kind": "forecast_state", "value_kind": "price"},
            "actual": {"kind": "forecast_state", "value_kind": "power"},
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
            "name": "real_pv_forecast",
            "kind": "forecast_state",
            "value_kind": "power",
            "source_entity_id": "sensor.pv",
        },
    ]

    errors = validator._profile_errors("ha-energy-planner-v1-real", fixtures)

    assert errors["missing_source_fields"] == [
        {"name": "real_amber_export", "missing_fields": ["source_entity_id"]},
        {"name": "real_amber_import", "missing_fields": ["source_entity_id"]},
    ]


def _assert_forecast_fixture(fixture: dict[str, Any]) -> None:
    issued_at = _parse_datetime(fixture["issued_at"])
    attributes = dict(fixture.get("attributes", {}))
    response = fixture.get("response")
    source_entity_id = fixture.get("source_entity_id")
    if isinstance(response, dict) and isinstance(response.get(source_entity_id), dict):
        attributes["forecast"] = response[source_entity_id].get("forecast", [])
    series = forecast_series_from_state(
        FakeState(str(fixture.get("state", "")), attributes),
        issued_at=issued_at,
        horizon_hours=int(fixture["horizon_hours"]),
        interval_minutes=int(fixture["interval_minutes"]),
        value_keys=tuple(fixture["value_keys"]),
        value_kind=str(fixture["value_kind"]),
    )

    assert series == fixture["expected"], fixture["name"]


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
