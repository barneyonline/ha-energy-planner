"""Tests for live-schema export helper."""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from typing import Any


def _load_exporter() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "export-live-schema-fixture.py"
    spec = importlib.util.spec_from_file_location("export_live_schema_fixture", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_forecast_export_builds_sanitized_validator_fixture(monkeypatch: Any) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "state": "0.12",
            "attributes": {
                "unit_of_measurement": "$/kWh",
                "api_token": "secret",
                "forecasts": [{"period_start": "2026-06-27T00:00:00+00:00", "per_kwh": 0.12}],
            },
        }

    monkeypatch.setattr(exporter, "_request_json", fake_request_json)

    fixture = exporter._forecast_fixture(
        Namespace(
            ha_url="http://ha.local:8123",
            token="token",
            name="real_amber_import",
            entity_id="sensor.amber_import",
            issued_at="2026-06-27T00:00:00+00:00",
            horizon_hours=24,
            interval_minutes=5,
            value_kind="price",
            value_keys="import_price,per_kwh,price",
            redact_key=[],
        )
    )

    assert fixture["kind"] == "forecast_state"
    assert fixture["source_entity_id"] == "sensor.amber_import"
    assert fixture["value_keys"] == ["import_price", "per_kwh", "price"]
    assert fixture["attributes"]["api_token"] == "<redacted>"
    assert fixture["attributes"]["forecasts"][0]["per_kwh"] == 0.12


def test_main_validate_writes_only_parseable_fixture(monkeypatch: Any, tmp_path: Path) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "state": "0.12",
            "attributes": {"forecasts": [{"period_start": "2026-06-27T00:00:00+00:00", "per_kwh": 0.12}]},
        }

    out = tmp_path / "real_amber_import.json"
    monkeypatch.setattr(exporter, "_request_json", fake_request_json)
    monkeypatch.setattr(
        exporter.sys,
        "argv",
        [
            "export-live-schema-fixture.py",
            "--ha-url",
            "http://ha.local:8123",
            "--token",
            "token",
            "--out",
            str(out),
            "--validate",
            "forecast-state",
            "--name",
            "real_amber_import",
            "--entity-id",
            "sensor.amber_import",
            "--issued-at",
            "2026-06-27T00:00:00+00:00",
            "--horizon-hours",
            "1",
            "--interval-minutes",
            "15",
            "--value-kind",
            "price",
            "--value-keys",
            "per_kwh,price,value",
        ],
    )

    assert exporter.main() == 0
    assert out.exists()


def test_main_validate_rejects_unparseable_fixture(monkeypatch: Any, tmp_path: Path) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"state": "sunny", "attributes": {"forecast": [{"unsupported": "shape"}]}}

    out = tmp_path / "bad_weather.json"
    monkeypatch.setattr(exporter, "_request_json", fake_request_json)
    monkeypatch.setattr(
        exporter.sys,
        "argv",
        [
            "export-live-schema-fixture.py",
            "--ha-url",
            "http://ha.local:8123",
            "--token",
            "token",
            "--out",
            str(out),
            "--validate",
            "forecast-state",
            "--name",
            "bad_weather",
            "--entity-id",
            "weather.home",
            "--issued-at",
            "2026-06-27T00:00:00+00:00",
            "--horizon-hours",
            "1",
            "--interval-minutes",
            "15",
            "--value-kind",
            "temperature",
            "--value-keys",
            "temperature,native_temperature,value",
        ],
    )

    assert exporter.main() == 1
    assert not out.exists()


def test_forecast_export_rejects_empty_value_keys(monkeypatch: Any) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"state": "0.12", "attributes": {"forecasts": []}}

    monkeypatch.setattr(exporter, "_request_json", fake_request_json)

    try:
        exporter._forecast_fixture(
            Namespace(
                ha_url="http://ha.local:8123",
                token="token",
                name="real_amber_import",
                entity_id="sensor.amber_import",
                issued_at="2026-06-27T00:00:00+00:00",
                horizon_hours=24,
                interval_minutes=5,
                value_kind="price",
                value_keys=" , ",
                redact_key=[],
            )
        )
    except ValueError as err:
        assert "--value-keys" in str(err)
    else:
        raise AssertionError("empty value keys should be rejected")


def test_forecast_export_redacts_extra_key_fragments(monkeypatch: Any) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "state": "0.12",
            "attributes": {
                "site_serial_number": "abc123",
                "forecasts": [{"period_start": "2026-06-27T00:00:00+00:00", "per_kwh": 0.12}],
            },
        }

    monkeypatch.setattr(exporter, "_request_json", fake_request_json)

    fixture = exporter._forecast_fixture(
        Namespace(
            ha_url="http://ha.local:8123",
            token="token",
            name="real_amber_import",
            entity_id="sensor.amber_import",
            issued_at="2026-06-27T00:00:00+00:00",
            horizon_hours=24,
            interval_minutes=5,
            value_kind="price",
            value_keys="per_kwh",
            redact_key=["serial-number"],
        )
    )

    assert fixture["attributes"]["site_serial_number"] == "<redacted>"
    assert fixture["attributes"]["forecasts"][0]["per_kwh"] == 0.12


def test_haeo_export_unwraps_service_response_and_redacts(monkeypatch: Any) -> None:
    exporter = _load_exporter()

    def fake_request_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "changed_states": [],
            "service_response": {
                "result": {
                    "slots": [{"grid_import_w": 1000, "access_token": "secret"}],
                    "home_latitude": -37.8,
                }
            },
        }

    monkeypatch.setattr(exporter, "_request_json", fake_request_json)

    fixture = exporter._haeo_fixture(
        Namespace(
            ha_url="http://ha.local:8123",
            token="token",
            name="real_haeo",
            service="haeo.optimize",
            service_data_json='{"source": "schema_export"}',
            issued_at="2026-06-27T00:00:00+00:00",
            interval_minutes=5,
            slot_count=288,
            redact_key=[],
        )
    )

    assert fixture["kind"] == "haeo_response"
    assert fixture["source_service"] == "haeo.optimize"
    assert fixture["response"]["result"]["slots"][0]["grid_import_w"] == 1000
    assert fixture["response"]["result"]["slots"][0]["access_token"] == "<redacted>"
    assert fixture["response"]["result"]["home_latitude"] == "<redacted>"
