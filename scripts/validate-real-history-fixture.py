#!/usr/bin/env python3
"""Validate sanitized real-history fixtures against HA Energy Planner replay code."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.ha_energy_planner.ev import (  # noqa: E402
    import_trip_history_from_state_sequences,
    summarize_stored_trip_history,
)
from custom_components.ha_energy_planner.forecast_accuracy import (  # noqa: E402
    accuracy_threshold_errors,
    summarize_forecast_accuracy,
)
from custom_components.ha_energy_planner.forecasts import (  # noqa: E402
    forecast_series_from_state,
    normalize_scalar_value,
)
from custom_components.ha_energy_planner.thermal_model import (  # noqa: E402
    thermal_model_summary,
    update_thermal_model,
)

REAL_HISTORY_PROFILE_REQUIREMENTS = {
    "real_mini_trip_history": {"kind": "ev_trip_history"},
    "real_daikin_thermal_history": {"kind": "thermal_history"},
    "real_pv_forecast_accuracy": {"kind": "forecast_accuracy"},
    "real_load_forecast_accuracy": {"kind": "forecast_accuracy"},
}
REAL_HISTORY_PROFILE_ENTITY_KEYS = {
    "real_mini_trip_history": ("ev_connected", "ev_soc"),
    "real_daikin_thermal_history": ("indoor_temperature", "hvac_power"),
    "real_pv_forecast_accuracy": ("forecast", "actual"),
    "real_load_forecast_accuracy": ("forecast", "actual"),
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
            "Validate trip, thermal, and rolling-origin forecast-accuracy fixtures exported from Recorder history."
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
    if kind == "forecast_accuracy":
        return _validate_forecast_accuracy_fixture(fixture)
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
        absent = [key for key in REAL_HISTORY_PROFILE_ENTITY_KEYS[name] if not _has_source_entity(fixture, key)]
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


def _validate_forecast_accuracy_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    samples = list(fixture.get("samples", [])) or _forecast_accuracy_samples(fixture)
    buckets = list(
        fixture.get(
            "horizon_buckets",
            [
                {"name": "near", "min_hours": 0, "max_hours": 4},
                {"name": "day", "min_hours": 4, "max_hours": 12},
                {"name": "long", "min_hours": 12, "max_hours": 24.01},
            ],
        )
    )
    summary = summarize_forecast_accuracy(samples, buckets)
    requirements = dict(fixture.get("requirements", {}))
    errors = accuracy_threshold_errors(summary, requirements)
    if errors:
        raise ValueError("; ".join(errors))
    return {"kind": "forecast_accuracy", "name": fixture.get("name"), **summary}


def _forecast_accuracy_samples(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Match historical forecast origins to observations at each exact valid time."""
    histories = dict(fixture.get("states", {}))
    forecast_states = [_state_from_item(item) for item in histories.get("forecast", [])]
    actual_states = [_state_from_item(item) for item in histories.get("actual", [])]
    interval = int(fixture.get("interval_minutes", 30))
    horizon = int(fixture.get("horizon_hours", 24))
    tolerance_seconds = int(fixture.get("match_tolerance_minutes", interval // 2)) * 60
    value_kind = str(fixture.get("value_kind", "power"))
    value_keys = tuple(fixture.get("value_keys", ("value",)))
    samples: list[dict[str, Any]] = []
    for state in forecast_states:
        issued_at = state.last_updated
        baseline_state = _actual_at_or_before(actual_states, issued_at)
        if baseline_state is None:
            continue
        baseline = _normalized_state_value(baseline_state, value_kind)
        if baseline is None:
            continue
        series = forecast_series_from_state(
            state,
            issued_at=issued_at,
            horizon_hours=horizon,
            interval_minutes=interval,
            value_keys=value_keys,
            value_kind=value_kind,
        )
        if series is None:
            continue
        for index, forecast in enumerate(series):
            if forecast is None:
                continue
            valid_at = issued_at + timedelta(minutes=index * interval)
            actual_state = _nearest_actual(actual_states, valid_at, tolerance_seconds)
            actual = _normalized_state_value(actual_state, value_kind) if actual_state else None
            if actual is None:
                continue
            samples.append(
                {
                    "issued_at": issued_at.isoformat(),
                    "valid_at": valid_at.isoformat(),
                    "lead_hours": index * interval / 60,
                    "forecast": forecast,
                    "actual": actual,
                    "baseline": baseline,
                }
            )
    return samples


def _actual_at_or_before(states: list[FixtureState], target: datetime) -> FixtureState | None:
    matches = [state for state in states if state.last_updated <= target]
    return max(matches, key=lambda state: state.last_updated) if matches else None


def _nearest_actual(states: list[FixtureState], target: datetime, tolerance_seconds: int) -> FixtureState | None:
    if not states:
        return None
    nearest = min(states, key=lambda state: abs((state.last_updated - target).total_seconds()))
    return nearest if abs((nearest.last_updated - target).total_seconds()) <= tolerance_seconds else None


def _normalized_state_value(state: FixtureState, value_kind: str) -> float | None:
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = str(state.attributes.get("unit_of_measurement", state.attributes.get("unit", "")))
    return normalize_scalar_value(value, value_kind=value_kind, unit=unit)


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
        events.append((state.last_changed, "indoor_sample", (state.state, value)))
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
        if key == "indoor_sample":
            latest["hvac_mode"], latest["indoor_temperature_c"] = value
        else:
            latest[key] = value
        if "indoor_temperature_c" not in latest or "hvac_power_kw" not in latest:
            continue
        samples.append(
            {
                "sampled_at": timestamp.isoformat(),
                "hvac_mode": latest.get("hvac_mode", fixture.get("hvac_mode")),
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
