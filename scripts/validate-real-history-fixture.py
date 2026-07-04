#!/usr/bin/env python3
"""Validate sanitized real-history fixtures against HA Energy Planner replay code."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.ha_energy_planner.ev import (  # noqa: E402
    import_trip_history_from_state_sequences,
    summarize_stored_trip_history,
)
from custom_components.ha_energy_planner.thermal_model import (  # noqa: E402
    thermal_model_summary,
    update_thermal_model,
)

REAL_HISTORY_PROFILE_REQUIREMENTS = {
    "real_mini_trip_history": {"kind": "ev_trip_history"},
    "real_daikin_thermal_history": {"kind": "thermal_history"},
}
REAL_HISTORY_PROFILE_ENTITY_KEYS = {
    "real_mini_trip_history": ("ev_connected", "ev_soc"),
    "real_daikin_thermal_history": ("indoor_temperature", "hvac_power"),
}


@dataclass(slots=True)
class FixtureState:
    """Minimal Home Assistant state shape for history replay."""

    state: str
    last_changed: datetime
    last_updated: datetime
    attributes: dict[str, Any]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate ev_trip_history and thermal_history fixtures exported from "
            "Home Assistant Recorder history."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("ha-energy-planner-history-v1-real",),
        help="Require a named real-history coverage profile in addition to replaying each fixture.",
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
            print(json.dumps({"ok": False, "profile": args.profile, **profile_errors}, sort_keys=True))
        else:
            print(json.dumps({"ok": True, "profile": args.profile}, sort_keys=True))
    return 1 if failed else 0


def _validate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    kind = fixture.get("kind")
    if kind == "ev_trip_history":
        return _validate_ev_trip_history_fixture(fixture)
    if kind == "thermal_history":
        return _validate_thermal_history_fixture(fixture)
    raise ValueError(f"Unsupported fixture kind: {kind!r}")


def _profile_missing_names(profile: str, fixtures: list[dict[str, Any]]) -> list[str]:
    return _profile_errors(profile, fixtures).get("missing_fixture_names", [])


def _profile_errors(profile: str, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    if profile != "ha-energy-planner-history-v1-real":
        raise ValueError(f"Unsupported profile: {profile!r}")
    by_name = {str(fixture.get("name", "")): fixture for fixture in fixtures}
    missing = sorted(set(REAL_HISTORY_PROFILE_REQUIREMENTS) - set(by_name))
    mismatched = []
    missing_source_entities = []
    for name, expected in sorted(REAL_HISTORY_PROFILE_REQUIREMENTS.items()):
        fixture = by_name.get(name)
        if fixture is None:
            continue
        actual = {key: fixture.get(key) for key in expected}
        if actual != expected:
            mismatched.append({"name": name, "expected": expected, "actual": actual})
        absent = [
            key
            for key in REAL_HISTORY_PROFILE_ENTITY_KEYS[name]
            if not _has_source_entity(fixture, key)
        ]
        if absent:
            missing_source_entities.append({"name": name, "missing_source_entity_keys": absent})
    errors: dict[str, Any] = {}
    if missing:
        errors["missing_fixture_names"] = missing
    if mismatched:
        errors["mismatched_fixtures"] = mismatched
    if missing_source_entities:
        errors["missing_source_entities"] = missing_source_entities
    return errors


def _validate_ev_trip_history_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    sources = dict(fixture.get("source_entity_ids", {}))
    connected_key = str(fixture.get("connected_key", "ev_connected"))
    soc_key = str(fixture.get("soc_key", "ev_soc"))
    connected_states = _states_for_key(fixture, connected_key)
    soc_states = _states_for_key(fixture, soc_key)
    imported_at = _parse_datetime(str(fixture.get("exported_at") or fixture.get("end")))
    history, _changed = import_trip_history_from_state_sequences(
        {},
        connected_states=connected_states,
        soc_states=soc_states,
        imported_at=imported_at,
    )
    records = list(history.get("records", []))
    min_records = int(fixture.get("expected_min_records", 1))
    if len(records) < min_records:
        raise ValueError(
            f"{fixture.get('name', 'ev_trip_history')} produced {len(records)} trip records, "
            f"expected at least {min_records}"
        )
    summary = summarize_stored_trip_history(history)
    return {
        "kind": "ev_trip_history",
        "name": fixture.get("name"),
        "source_entities": sorted(sources),
        "record_count": len(records),
        "observed_days": summary.observed_days,
        "max_daily_soc_percent": summary.max_daily_soc_percent,
        "history_sufficient": summary.history_sufficient,
    }


def _validate_thermal_history_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    samples = _thermal_samples(fixture)
    if len(samples) < 2:
        raise ValueError(f"{fixture.get('name', 'thermal_history')} produced fewer than 2 thermal samples")
    model: dict[str, Any] = {}
    previous: dict[str, Any] | None = None
    for sample in samples:
        model, _changed = update_thermal_model(model, previous, sample)
        previous = sample
    summary = thermal_model_summary(model)
    min_active = int(fixture.get("expected_min_active_samples", 1))
    min_passive = int(fixture.get("expected_min_passive_samples", 0))
    if summary["active_sample_count"] < min_active:
        raise ValueError(
            f"{fixture.get('name', 'thermal_history')} produced {summary['active_sample_count']} active "
            f"thermal samples, expected at least {min_active}"
        )
    if summary["passive_sample_count"] < min_passive:
        raise ValueError(
            f"{fixture.get('name', 'thermal_history')} produced {summary['passive_sample_count']} passive "
            f"thermal samples, expected at least {min_passive}"
        )
    return {
        "kind": "thermal_history",
        "name": fixture.get("name"),
        "sample_count": len(samples),
        **summary,
    }


def _states_for_key(fixture: dict[str, Any], key: str) -> list[FixtureState]:
    histories = dict(fixture.get("states", {}))
    return [_state_from_item(item) for item in list(histories.get(key, []))]


def _state_from_item(item: Any) -> FixtureState:
    if not isinstance(item, dict):
        raise ValueError(f"History item must be an object, got {type(item).__name__}")
    last_changed = _parse_datetime(str(item.get("last_changed") or item.get("last_updated")))
    last_updated = _parse_datetime(str(item.get("last_updated") or item.get("last_changed")))
    attributes = item.get("attributes", {})
    return FixtureState(
        state=str(item.get("state", "")),
        last_changed=last_changed,
        last_updated=last_updated,
        attributes=dict(attributes) if isinstance(attributes, dict) else {},
    )


def _thermal_samples(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    histories = dict(fixture.get("states", {}))
    indoor_key = str(fixture.get("indoor_temperature_key", "indoor_temperature"))
    power_key = str(fixture.get("hvac_power_key", "hvac_power"))
    outdoor_key = str(fixture.get("outdoor_temperature_key", "outdoor_temperature"))
    indoor_attribute = fixture.get("indoor_temperature_attribute")
    outdoor_attribute = fixture.get("outdoor_temperature_attribute")
    events: list[tuple[datetime, str, Any]] = []

    for item in histories.get(indoor_key, []):
        state = _state_from_item(item)
        value = _attribute_or_state(state, indoor_attribute)
        events.append((state.last_changed, "indoor_temperature_c", value))
    for item in histories.get(power_key, []):
        state = _state_from_item(item)
        events.append((state.last_changed, "hvac_power_kw", state.state))
    for item in histories.get(outdoor_key, []):
        state = _state_from_item(item)
        value = _attribute_or_state(state, outdoor_attribute)
        events.append((state.last_changed, "outdoor_temperature_c", value))

    latest: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    for timestamp, key, value in sorted(events, key=lambda event: event[0]):
        latest[key] = value
        if "indoor_temperature_c" not in latest or "hvac_power_kw" not in latest:
            continue
        samples.append(
            {
                "sampled_at": timestamp.isoformat(),
                "hvac_mode": fixture.get("hvac_mode"),
                "indoor_temperature_c": latest.get("indoor_temperature_c"),
                "outdoor_temperature_c": latest.get("outdoor_temperature_c"),
                "hvac_power_kw": latest.get("hvac_power_kw"),
            }
        )
    return samples


def _attribute_or_state(state: FixtureState, attribute: Any) -> Any:
    if isinstance(attribute, str) and attribute.strip():
        return state.attributes.get(attribute)
    return state.state


def _has_source_entity(fixture: dict[str, Any], key: str) -> bool:
    sources = fixture.get("source_entity_ids", {})
    if not isinstance(sources, dict):
        return False
    value = sources.get(key)
    return isinstance(value, str) and bool(value.strip()) and value.strip() != "<redacted>"


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
