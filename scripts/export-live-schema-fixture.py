#!/usr/bin/env python3
"""Export sanitized Home Assistant payloads as live-schema validation fixtures."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
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
            "Export sanitized Home Assistant entity states or response-capable service "
            "results into fixtures accepted by scripts/validate-live-schema-fixture.py."
        )
    )
    parser.add_argument("--ha-url", default=os.environ.get("HOME_ASSISTANT_URL"))
    parser.add_argument("--token", default=os.environ.get("HOME_ASSISTANT_TOKEN"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the sanitized fixture with scripts/validate-live-schema-fixture.py before writing it.",
    )
    parser.add_argument(
        "--redact-key",
        action="append",
        default=[],
        help="Additional case-insensitive key fragment to redact. Can be repeated.",
    )
    subparsers = parser.add_subparsers(dest="kind", required=True)

    forecast = subparsers.add_parser("forecast-state", help="Export one forecast entity state.")
    forecast.add_argument("--name", required=True)
    forecast.add_argument("--entity-id", required=True)
    forecast.add_argument("--issued-at", default=None)
    forecast.add_argument("--horizon-hours", type=int, default=24)
    forecast.add_argument("--interval-minutes", type=int, default=5)
    forecast.add_argument("--value-kind", required=True, choices=("power", "price", "temperature"))
    forecast.add_argument("--value-keys", required=True, help="Comma-separated candidate value keys.")

    service = subparsers.add_parser(
        "haeo-response",
        help="Call a response-capable HAEO optimize service and export the sanitized response.",
    )
    service.add_argument("--name", required=True)
    service.add_argument("--service", required=True, help="Service name like haeo.optimize.")
    service.add_argument("--service-data-json", default="{}")
    service.add_argument("--issued-at", default=None)
    service.add_argument("--interval-minutes", type=int, default=5)
    service.add_argument("--slot-count", type=int, default=288)

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
    if args.kind == "forecast-state":
        return _forecast_fixture(args)
    if args.kind == "haeo-response":
        return _haeo_fixture(args)
    raise ValueError(f"Unsupported fixture kind: {args.kind!r}")


def _forecast_fixture(args: argparse.Namespace) -> dict[str, Any]:
    entity = _request_json(args.ha_url, args.token, f"/api/states/{quote(args.entity_id, safe='')}")
    if not isinstance(entity, dict):
        raise ValueError(f"Unexpected state response for {args.entity_id}: {type(entity).__name__}")
    value_keys = _value_keys(args.value_keys)
    extra_redact_parts = _extra_redact_parts(args)
    return {
        "kind": "forecast_state",
        "name": args.name,
        "source_entity_id": args.entity_id,
        "issued_at": _issued_at(args.issued_at),
        "horizon_hours": args.horizon_hours,
        "interval_minutes": args.interval_minutes,
        "value_kind": args.value_kind,
        "value_keys": value_keys,
        "state": _sanitize(entity.get("state"), extra_parts=extra_redact_parts),
        "attributes": _sanitize(entity.get("attributes", {}), extra_parts=extra_redact_parts),
    }


def _haeo_fixture(args: argparse.Namespace) -> dict[str, Any]:
    if "." not in args.service:
        raise ValueError("--service must be in domain.service form")
    domain, service = args.service.split(".", 1)
    try:
        service_data = json.loads(args.service_data_json)
    except json.JSONDecodeError as err:
        raise ValueError(f"--service-data-json is invalid JSON: {err}") from err
    if not isinstance(service_data, dict):
        raise ValueError("--service-data-json must decode to an object")

    response = _request_json(
        args.ha_url,
        args.token,
        f"/api/services/{quote(domain, safe='')}/{quote(service, safe='')}?return_response",
        method="POST",
        body=service_data,
    )
    payload = response.get("service_response", response) if isinstance(response, dict) else response
    extra_redact_parts = _extra_redact_parts(args)
    return {
        "kind": "haeo_response",
        "name": args.name,
        "source_service": args.service,
        "issued_at": _issued_at(args.issued_at),
        "interval_minutes": args.interval_minutes,
        "slot_count": args.slot_count,
        "response": _sanitize(payload, extra_parts=extra_redact_parts),
    }


def _request_json(
    ha_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> Any:
    url = urljoin(ha_url.rstrip("/") + "/", path.lstrip("/"))
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - URL is operator supplied.
        content = response.read().decode("utf-8")
    return json.loads(content) if content else {}


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


def _value_keys(raw: str) -> list[str]:
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if not keys:
        raise ValueError("--value-keys must include at least one key")
    return keys


def _extra_redact_parts(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(
        part.lower().replace("-", "_").replace(" ", "_")
        for part in (getattr(args, "redact_key", None) or ())
        if part.strip()
    )


def _validate_fixture(fixture: dict[str, Any]) -> None:
    validator_path = Path(__file__).with_name("validate-live-schema-fixture.py")
    spec = importlib.util.spec_from_file_location("validate_live_schema_fixture", validator_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load validator: {validator_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._validate_fixture(fixture)


def _issued_at(value: str | None) -> str:
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return datetime.now(UTC).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
