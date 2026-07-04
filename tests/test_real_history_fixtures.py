"""Tests for real-history export and replay fixtures."""

from __future__ import annotations

from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "history"


def _load_validator() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "validate-real-history-fixture.py"
    spec = importlib.util.spec_from_file_location("validate_real_history_fixture_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_exporter() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "export-real-history-fixture.py"
    spec = importlib.util.spec_from_file_location("export_real_history_fixture_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixtures() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE_DIR.glob("*.json"))
    ]


def test_history_fixtures_replay_successfully() -> None:
    validator = _load_validator()

    for fixture in _fixtures():
        summary = validator._validate_fixture(fixture)
        assert summary["kind"] == fixture["kind"]


def test_real_history_profile_reports_missing_fixture_names() -> None:
    validator = _load_validator()

    assert validator._profile_missing_names("ha-energy-planner-history-v1-real", _fixtures()) == [
        "real_daikin_thermal_history",
        "real_mini_trip_history",
    ]


def test_real_history_profile_accepts_required_source_entities() -> None:
    validator = _load_validator()
    fixtures = [
        {
            "kind": "ev_trip_history",
            "name": "real_mini_trip_history",
            "source_entity_ids": {
                "ev_connected": "binary_sensor.mini_connected",
                "ev_soc": "sensor.mini_soc",
            },
        },
        {
            "kind": "thermal_history",
            "name": "real_daikin_thermal_history",
            "source_entity_ids": {
                "indoor_temperature": "climate.daikin",
                "hvac_power": "sensor.daikin_power",
            },
        },
    ]

    assert validator._profile_errors("ha-energy-planner-history-v1-real", fixtures) == {}


def test_real_history_profile_reports_missing_source_entities() -> None:
    validator = _load_validator()
    fixtures = [
        {
            "kind": "ev_trip_history",
            "name": "real_mini_trip_history",
            "source_entity_ids": {
                "ev_connected": "<redacted>",
                "ev_soc": "sensor.mini_soc",
            },
        },
        {
            "kind": "thermal_history",
            "name": "real_daikin_thermal_history",
            "source_entity_ids": {
                "indoor_temperature": "",
            },
        },
    ]

    errors = validator._profile_errors("ha-energy-planner-history-v1-real", fixtures)

    assert errors["missing_source_entities"] == [
        {"name": "real_daikin_thermal_history", "missing_source_entity_keys": ["indoor_temperature", "hvac_power"]},
        {"name": "real_mini_trip_history", "missing_source_entity_keys": ["ev_connected"]},
    ]


def test_ev_history_export_builds_sanitized_fixture(monkeypatch: Any) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> list[list[dict[str, Any]]]:
        return [
            [
                {
                    "entity_id": "binary_sensor.mini_connected",
                    "state": "unplugged",
                    "last_changed": "2026-06-27T01:00:00+00:00",
                    "last_updated": "2026-06-27T01:00:00+00:00",
                    "attributes": {"device_tracker_token": "secret"},
                }
            ],
            [
                {
                    "entity_id": "sensor.mini_soc",
                    "state": "78%",
                    "last_changed": "2026-06-27T01:00:00+00:00",
                    "last_updated": "2026-06-27T01:00:00+00:00",
                    "attributes": {"serial_number": "abc"},
                }
            ],
        ]

    monkeypatch.setattr(exporter, "_request_json", fake_request_json)

    fixture = exporter._ev_trip_history_fixture(
        Namespace(
            ha_url="http://ha.local:8123",
            token="token",
            connected_entity="binary_sensor.mini_connected",
            soc_entity="sensor.mini_soc",
            name="real_mini_trip_history",
            expected_min_records=1,
            redact_key=["serial"],
        ),
        exporter._parse_datetime("2026-06-27T00:00:00+00:00"),
        exporter._parse_datetime("2026-06-27T02:00:00+00:00"),
    )

    assert fixture["kind"] == "ev_trip_history"
    assert fixture["source_entity_ids"]["ev_connected"] == "binary_sensor.mini_connected"
    assert fixture["states"]["ev_connected"][0]["attributes"] == {}
    assert fixture["states"]["ev_soc"][0]["attributes"] == {}


def test_thermal_history_export_keeps_requested_temperature_attribute(monkeypatch: Any) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> list[list[dict[str, Any]]]:
        return [
            [
                {
                    "entity_id": "climate.daikin",
                    "state": "heat",
                    "last_changed": "2026-06-27T00:00:00+00:00",
                    "last_updated": "2026-06-27T00:00:00+00:00",
                    "attributes": {"current_temperature": 20.1, "access_token": "secret"},
                }
            ],
            [
                {
                    "entity_id": "sensor.daikin_power",
                    "state": "1.8",
                    "last_changed": "2026-06-27T00:00:00+00:00",
                    "last_updated": "2026-06-27T00:00:00+00:00",
                    "attributes": {},
                }
            ],
        ]

    monkeypatch.setattr(exporter, "_request_json", fake_request_json)

    fixture = exporter._thermal_history_fixture(
        Namespace(
            ha_url="http://ha.local:8123",
            token="token",
            name="real_daikin_thermal_history",
            indoor_temperature_entity="climate.daikin",
            indoor_temperature_attribute="current_temperature",
            hvac_power_entity="sensor.daikin_power",
            outdoor_temperature_entity=None,
            outdoor_temperature_attribute=None,
            hvac_mode="heat",
            expected_min_active_samples=1,
            expected_min_passive_samples=0,
            redact_key=[],
        ),
        exporter._parse_datetime("2026-06-27T00:00:00+00:00"),
        exporter._parse_datetime("2026-06-27T01:00:00+00:00"),
    )

    assert fixture["indoor_temperature_attribute"] == "current_temperature"
    assert fixture["states"]["indoor_temperature"][0]["attributes"]["current_temperature"] == 20.1
    assert "access_token" not in fixture["states"]["indoor_temperature"][0]["attributes"]
