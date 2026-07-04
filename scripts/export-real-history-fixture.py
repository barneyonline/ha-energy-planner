#!/usr/bin/env python3
"""Export sanitized Home Assistant Recorder history fixtures for replay validation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "device_tracker",
    "latitude",
    "location",
    "longitude",
    "password",
    "refresh_token",
    "secret",
    "token",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export sanitized Home Assistant Recorder history for EV trip and Daikin HVAC thermal replay validation."
        )
    )
    parser.add_argument("--ha-url", default=os.environ.get("HOME_ASSISTANT_URL"))
    parser.add_argument("--token", default=os.environ.get("HOME_ASSISTANT_TOKEN"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", default=os.environ.get("HEP_HISTORY_START"))
    parser.add_argument("--end", default=os.environ.get("HEP_HISTORY_END"))
    parser.add_argument("--days", type=int, default=int(os.environ.get("HEP_HISTORY_DAYS", "30")))
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the sanitized fixture with scripts/validate-real-history-fixture.py before writing it.",
    )
    parser.add_argument(
        "--redact-key",
        action="append",
        default=[],
        help="Additional case-insensitive key fragment to redact. Can be repeated.",
    )
    subparsers = parser.add_subparsers(dest="kind", required=True)

    ev = subparsers.add_parser("ev-trip-history", help="Export MINI/EV SOC and connection history.")
    ev.add_argument("--name", default="real_mini_trip_history")
    ev.add_argument("--connected-entity", required=True)
    ev.add_argument("--soc-entity", required=True)
    ev.add_argument("--expected-min-records", type=int, default=1)

    thermal = subparsers.add_parser("thermal-history", help="Export Daikin power and temperature history.")
    thermal.add_argument("--name", default="real_daikin_thermal_history")
    thermal.add_argument("--indoor-temperature-entity", required=True)
    thermal.add_argument(
        "--indoor-temperature-attribute",
        default=None,
        help="Attribute to read for indoor temperature, for example current_temperature on climate entities.",
    )
    thermal.add_argument("--hvac-power-entity", required=True)
    thermal.add_argument("--outdoor-temperature-entity", default=None)
    thermal.add_argument("--outdoor-temperature-attribute", default=None)
    thermal.add_argument("--expected-min-active-samples", type=int, default=12)
    thermal.add_argument("--expected-min-passive-samples", type=int, default=0)
    thermal.add_argument("--hvac-mode", default=None)

    args = parser.parse_args()
    if not args.ha_url:
        parser.error("--ha-url or HOME_ASSISTANT_URL is required")
    if not args.token:
        parser.error("--token or HOME_ASSISTANT_TOKEN is required")

    try:
        fixture = _build_fixture(args)
        if args.validate:
            _validate_fixture(fixture)
    except (HTTPError, URLError, ValueError) as err:
        print(f"export failed: {err}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(args.out))
    return 0


def _build_fixture(args: argparse.Namespace) -> dict[str, Any]:
    start, end = _history_window(args)
    if args.kind == "ev-trip-history":
        return _ev_trip_history_fixture(args, start, end)
    if args.kind == "thermal-history":
        return _thermal_history_fixture(args, start, end)
    raise ValueError(f"Unsupported fixture kind: {args.kind!r}")


def _ev_trip_history_fixture(args: argparse.Namespace, start: datetime, end: datetime) -> dict[str, Any]:
    entity_map = {
        "ev_connected": args.connected_entity,
        "ev_soc": args.soc_entity,
    }
    histories = _history_by_key(args, start, end, entity_map, {})
    return {
        "kind": "ev_trip_history",
        "name": args.name,
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "connected_key": "ev_connected",
        "soc_key": "ev_soc",
        "source_entity_ids": entity_map,
        "expected_min_records": args.expected_min_records,
        "states": histories,
    }


def _thermal_history_fixture(args: argparse.Namespace, start: datetime, end: datetime) -> dict[str, Any]:
    entity_map = {
        "indoor_temperature": args.indoor_temperature_entity,
        "hvac_power": args.hvac_power_entity,
    }
    if args.outdoor_temperature_entity:
        entity_map["outdoor_temperature"] = args.outdoor_temperature_entity
    keep_attributes: dict[str, tuple[str, ...]] = {}
    if args.indoor_temperature_attribute:
        keep_attributes["indoor_temperature"] = (args.indoor_temperature_attribute,)
    if args.outdoor_temperature_attribute:
        keep_attributes["outdoor_temperature"] = (args.outdoor_temperature_attribute,)
    histories = _history_by_key(args, start, end, entity_map, keep_attributes)
    return {
        "kind": "thermal_history",
        "name": args.name,
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "indoor_temperature_key": "indoor_temperature",
        "indoor_temperature_attribute": args.indoor_temperature_attribute,
        "hvac_power_key": "hvac_power",
        "outdoor_temperature_key": "outdoor_temperature",
        "outdoor_temperature_attribute": args.outdoor_temperature_attribute,
        "hvac_mode": args.hvac_mode,
        "source_entity_ids": entity_map,
        "expected_min_active_samples": args.expected_min_active_samples,
        "expected_min_passive_samples": args.expected_min_passive_samples,
        "states": histories,
    }


def _history_by_key(
    args: argparse.Namespace,
    start: datetime,
    end: datetime,
    entity_map: dict[str, str],
    keep_attributes: dict[str, tuple[str, ...]],
) -> dict[str, list[dict[str, Any]]]:
    response = _history_response(args.ha_url, args.token, start, end, tuple(entity_map.values()))
    by_entity = _history_response_by_entity(response)
    extra_redact_parts = _extra_redact_parts(args)
    return {
        key: [
            _sanitize_state(item, extra_parts=extra_redact_parts, keep_attributes=keep_attributes.get(key, ()))
            for item in by_entity.get(entity_id, [])
        ]
        for key, entity_id in entity_map.items()
    }


def _history_response(
    ha_url: str,
    token: str,
    start: datetime,
    end: datetime,
    entity_ids: tuple[str, ...],
) -> Any:
    query = urlencode(
        {
            "filter_entity_id": ",".join(entity_ids),
            "end_time": end.isoformat(),
        }
    )
    path = f"/api/history/period/{quote(start.isoformat(), safe=':+')}?{query}"
    return _request_json(ha_url, token, path)


def _request_json(ha_url: str, token: str, path: str) -> Any:
    url = urljoin(ha_url.rstrip("/") + "/", path.lstrip("/"))
    request = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310 - URL is operator supplied.
        content = response.read().decode("utf-8")
    return json.loads(content) if content else []


def _history_response_by_entity(response: Any) -> dict[str, list[dict[str, Any]]]:
    by_entity: dict[str, list[dict[str, Any]]] = {}
    if isinstance(response, list):
        for series in response:
            if not isinstance(series, list):
                continue
            for item in series:
                if not isinstance(item, dict):
                    continue
                entity_id = item.get("entity_id")
                if isinstance(entity_id, str) and entity_id:
                    by_entity.setdefault(entity_id, []).append(item)
    return {
        entity_id: sorted(items, key=lambda item: str(item.get("last_changed") or item.get("last_updated") or ""))
        for entity_id, items in by_entity.items()
    }


def _sanitize_state(
    item: dict[str, Any],
    *,
    extra_parts: tuple[str, ...] = (),
    keep_attributes: tuple[str, ...] = (),
) -> dict[str, Any]:
    attributes = item.get("attributes", {})
    kept_attributes = {
        key: attributes.get(key) for key in keep_attributes if isinstance(attributes, dict) and key in attributes
    }
    return {
        "state": _sanitize(item.get("state"), extra_parts=extra_parts),
        "last_changed": item.get("last_changed"),
        "last_updated": item.get("last_updated", item.get("last_changed")),
        "attributes": _sanitize(kept_attributes, extra_parts=extra_parts),
    }


def _sanitize(value: Any, *, extra_parts: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key), extra_parts=extra_parts):
                clean[key] = "<redacted>"
            else:
                clean[key] = _sanitize(item, extra_parts=extra_parts)
        return clean
    if isinstance(value, list):
        return [_sanitize(item, extra_parts=extra_parts) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, extra_parts=extra_parts) for item in value]
    return value


def _is_sensitive_key(key: str, *, extra_parts: tuple[str, ...] = ()) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in (*SENSITIVE_KEY_PARTS, *extra_parts))


def _extra_redact_parts(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(
        part.lower().replace("-", "_").replace(" ", "_")
        for part in (getattr(args, "redact_key", None) or ())
        if part.strip()
    )


def _history_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = _parse_datetime(args.end) if args.end else datetime.now(UTC).replace(microsecond=0)
    start = _parse_datetime(args.start) if args.start else end - timedelta(days=max(args.days, 1))
    if start >= end:
        raise ValueError("--start must be before --end")
    return start, end


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _validate_fixture(fixture: dict[str, Any]) -> None:
    validator_path = Path(__file__).with_name("validate-real-history-fixture.py")
    spec = importlib.util.spec_from_file_location("validate_real_history_fixture", validator_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load validator: {validator_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._validate_fixture(fixture)


if __name__ == "__main__":
    raise SystemExit(main())
