#!/usr/bin/env python3
"""Validate sanitized live-schema fixtures against HA Energy Planner parsers."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.ha_energy_planner.forecasts import forecast_series_from_state  # noqa: E402

V1_REAL_PROFILE_REQUIREMENTS = {
    "real_amber_import": {"kind": "forecast_state", "value_kind": "price"},
    "real_amber_export": {"kind": "forecast_state", "value_kind": "price"},
    "real_pv_forecast": {"kind": "forecast_state", "value_kind": "power"},
}
V1_REAL_PROFILE_SOURCE_FIELDS = {
    "real_amber_import": ("source_entity_id",),
    "real_amber_export": ("source_entity_id",),
    "real_pv_forecast": ("source_entity_id",),
}


@dataclass(slots=True)
class FixtureState:
    """Minimal Home Assistant state shape for parser validation."""

    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate forecast_state JSON fixtures exported from real Home Assistant entities."
    )
    parser.add_argument(
        "--profile",
        choices=("ha-energy-planner-v1-real",),
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
    raise ValueError(f"Unsupported fixture kind: {kind!r}")


def _profile_missing_names(profile: str, fixtures: list[dict[str, Any]]) -> list[str]:
    return _profile_errors(profile, fixtures).get("missing_fixture_names", [])


def _profile_errors(profile: str, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
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


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
