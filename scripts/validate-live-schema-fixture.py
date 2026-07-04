#!/usr/bin/env python3
"""Validate sanitized live-schema fixtures against HA Energy Planner parsers."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.ha_energy_planner.forecasts import forecast_series_from_state  # noqa: E402
from custom_components.ha_energy_planner.haeo_adapter import apply_haeo_response_to_context  # noqa: E402
from custom_components.ha_energy_planner.models import (  # noqa: E402
    DecisionContext,
    DecisionSlot,
    HAEOStatus,
    InputHealth,
    OccupancyState,
)

V1_REAL_PROFILE_REQUIREMENTS = {
    "real_amber_import": {"kind": "forecast_state", "value_kind": "price"},
    "real_amber_export": {"kind": "forecast_state", "value_kind": "price"},
    "real_pv_hafo": {"kind": "forecast_state", "value_kind": "power"},
    "real_baseline_load": {"kind": "forecast_state", "value_kind": "power"},
    "real_weather": {"kind": "forecast_state", "value_kind": "temperature"},
    "real_haeo_response": {"kind": "haeo_response"},
}
V1_REAL_PROFILE_SOURCE_FIELDS = {
    "real_amber_import": ("source_entity_id",),
    "real_amber_export": ("source_entity_id",),
    "real_pv_hafo": ("source_entity_id",),
    "real_baseline_load": ("source_entity_id",),
    "real_weather": ("source_entity_id",),
    "real_haeo_response": ("source_service",),
}


@dataclass(slots=True)
class FixtureState:
    """Minimal Home Assistant state shape for parser validation."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate forecast_state and haeo_response JSON fixtures. "
            "Use this with sanitized exports from real Home Assistant entities/services."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("ha-energy-planner-v1-real", "ha-energy-planner-haeo-value-v1-real"),
        help="Require a named fixture coverage profile in addition to parsing each fixture.",
    )
    parser.add_argument("fixtures", nargs="+", type=Path)
    args = parser.parse_args()

    failed = False
    validated_fixtures: list[dict[str, Any]] = []
    for fixture_path in args.fixtures:
        try:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            summary = _validate_fixture(fixture)
        except Exception as err:  # noqa: BLE001 - CLI should report all fixture failures compactly.
            failed = True
            print(json.dumps({"fixture": str(fixture_path), "ok": False, "error": str(err)}, sort_keys=True))
            continue
        validated_fixtures.append(fixture)
        print(json.dumps({"fixture": str(fixture_path), "ok": True, **summary}, sort_keys=True))
    if not failed and args.profile:
        profile_errors = _profile_errors(args.profile, validated_fixtures)
        if profile_errors:
            failed = True
            print(
                json.dumps(
                    {
                        "ok": False,
                        "profile": args.profile,
                        **profile_errors,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(json.dumps({"ok": True, "profile": args.profile}, sort_keys=True))
    return 1 if failed else 0


def _validate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    kind = fixture.get("kind")
    if kind == "forecast_state":
        return _validate_forecast_fixture(fixture)
    if kind == "haeo_response":
        return _validate_haeo_fixture(fixture)
    raise ValueError(f"Unsupported fixture kind: {kind!r}")


def _profile_missing_names(profile: str, fixtures: list[dict[str, Any]]) -> list[str]:
    return _profile_errors(profile, fixtures).get("missing_fixture_names", [])


def _profile_errors(profile: str, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    if profile == "ha-energy-planner-haeo-value-v1-real":
        return _haeo_value_profile_errors(fixtures)
    if profile != "ha-energy-planner-v1-real":
        raise ValueError(f"Unsupported profile: {profile!r}")
    by_name = {str(fixture.get("name", "")): fixture for fixture in fixtures}
    missing = sorted(set(V1_REAL_PROFILE_REQUIREMENTS) - set(by_name))
    mismatched = []
    missing_source_fields = []
    for name, expected in sorted(V1_REAL_PROFILE_REQUIREMENTS.items()):
        fixture = by_name.get(name)
        if fixture is None:
            continue
        actual = {key: fixture.get(key) for key in expected}
        if actual != expected:
            mismatched.append({"name": name, "expected": expected, "actual": actual})
        absent = [
            field for field in V1_REAL_PROFILE_SOURCE_FIELDS[name] if not _has_profile_source_field(fixture, field)
        ]
        if absent:
            missing_source_fields.append({"name": name, "missing_fields": absent})
    errors: dict[str, Any] = {}
    if missing:
        errors["missing_fixture_names"] = missing
    if mismatched:
        errors["mismatched_fixtures"] = mismatched
    if missing_source_fields:
        errors["missing_source_fields"] = missing_source_fields
    return errors


def _haeo_value_profile_errors(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {str(fixture.get("name", "")): fixture for fixture in fixtures}
    fixture = by_name.get("real_haeo_response")
    errors: dict[str, Any] = {}
    if fixture is None:
        errors["missing_fixture_names"] = ["real_haeo_response"]
        return errors
    if fixture.get("kind") != "haeo_response":
        errors["mismatched_fixtures"] = [
            {
                "name": "real_haeo_response",
                "expected": {"kind": "haeo_response"},
                "actual": {"kind": fixture.get("kind")},
            }
        ]
        return errors
    if not _has_profile_source_field(fixture, "source_service"):
        errors["missing_source_fields"] = [{"name": "real_haeo_response", "missing_fields": ["source_service"]}]
    try:
        summary = _validate_haeo_fixture(fixture)
    except Exception as err:  # noqa: BLE001 - profile should report validation failures compactly.
        errors["haeo_validation_error"] = str(err)
        return errors
    counts = dict(summary.get("evidence_counts", {}))
    required_counts = {
        "haeo_grid_import_forecast_kw": 1,
        "haeo_grid_export_forecast_kw": 1,
        "haeo_battery_charge_forecast_kw": 1,
        "haeo_battery_discharge_forecast_kw": 1,
    }
    missing_evidence = {
        field: {"expected_min": expected, "actual": int(counts.get(field, 0))}
        for field, expected in required_counts.items()
        if int(counts.get(field, 0)) < expected
    }
    if missing_evidence:
        errors["missing_haeo_value_evidence"] = missing_evidence
    return errors


def _has_profile_source_field(fixture: dict[str, Any], field: str) -> bool:
    value = fixture.get(field)
    return isinstance(value, str) and bool(value.strip()) and value.strip() != "<redacted>"


def _validate_forecast_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    issued_at = _parse_datetime(fixture["issued_at"])
    series = forecast_series_from_state(
        FixtureState(str(fixture.get("state", "")), dict(fixture.get("attributes", {}))),
        issued_at=issued_at,
        horizon_hours=int(fixture["horizon_hours"]),
        interval_minutes=int(fixture["interval_minutes"]),
        value_keys=tuple(fixture["value_keys"]),
        value_kind=str(fixture["value_kind"]),
    )
    expected = fixture.get("expected")
    if expected is not None and series != expected:
        raise ValueError(f"{fixture.get('name', 'forecast_state')} expected {expected!r}, got {series!r}")
    if not series:
        raise ValueError(f"{fixture.get('name', 'forecast_state')} did not produce a forecast series")
    return {
        "kind": "forecast_state",
        "name": fixture.get("name"),
        "point_count": len(series),
        "first_values": series[:4],
    }


def _validate_haeo_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    issued_at = _parse_datetime(fixture["issued_at"])
    interval = int(fixture["interval_minutes"])
    expected_slots = list(fixture.get("expected_slots", []))
    slot_count = max(len(expected_slots), int(fixture.get("slot_count", 0)), 1)
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
            for offset in range(0, slot_count * interval, interval)
        ],
        current_battery_soc_percent=50,
        current_ev_soc_percent=None,
        occupancy_state=OccupancyState.OCCUPIED,
        haeo_status=HAEOStatus.READY,
        input_health=InputHealth.HEALTHY,
    )

    counts = apply_haeo_response_to_context(context, fixture.get("response"))
    expected_counts = fixture.get("expected_counts")
    if expected_counts is not None and counts != expected_counts:
        raise ValueError(f"{fixture.get('name', 'haeo_response')} expected counts {expected_counts!r}, got {counts!r}")
    for index, expected in enumerate(expected_slots):
        for key, value in expected.items():
            actual = getattr(context.slots[index], key)
            if actual != value:
                raise ValueError(
                    f"{fixture.get('name', 'haeo_response')} slot {index} {key} expected {value!r}, got {actual!r}"
                )
    if not counts:
        raise ValueError(f"{fixture.get('name', 'haeo_response')} did not produce HAEO evidence counts")
    return {
        "kind": "haeo_response",
        "name": fixture.get("name"),
        "evidence_counts": counts,
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
