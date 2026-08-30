#!/usr/bin/env python3
"""Validate Home Assistant quality scale evidence for this custom integration."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    print("ERROR: PyYAML is required. Install dev requirements.", file=sys.stderr)
    raise

DOMAIN = "ha_energy_planner"
EXPECTED_QUALITY_SCALE = "platinum"
QUALITY_LEVEL_ORDER = ("bronze", "silver", "gold", "platinum")
QUALITY_RULES_BY_LEVEL = {
    "bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow-test-coverage",
        "config-flow",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "gold": (
        "devices",
        "diagnostics",
        "discovery-update-info",
        "discovery",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "platinum": ("async-dependency", "inject-websession", "strict-typing"),
}
EXEMPT_ALLOWED_RULES = {
    "docs-conditions",
    "docs-triggers",
    "discovery",
    "discovery-update-info",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-event-setup",
    "entity-unavailable",
    "icon-translations",
    "inject-websession",
    "reauthentication-flow",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
}
VALID_STATUSES = {"done", "exempt", "todo"}


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "custom_components" / DOMAIN / "manifest.json"
    if not path.exists():
        raise ValueError(f"{path} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _claimed_level(root: Path) -> str | None:
    raw_level = _manifest(root).get("quality_scale")
    if raw_level is None:
        return None
    level = str(raw_level).strip().lower()
    if level not in QUALITY_LEVEL_ORDER:
        raise ValueError(f"Unsupported manifest quality_scale value: {level!r}")
    return level


def _required_levels_for_claim(level: str | None) -> tuple[str, ...]:
    if level is None:
        return ()
    claimed_index = QUALITY_LEVEL_ORDER.index(level)
    return QUALITY_LEVEL_ORDER[: claimed_index + 1]


def _status_for_rule(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("status") or "").strip().lower()
    if isinstance(entry, str):
        return entry.strip().lower()
    return ""


def _references_for_rule(entry: object) -> dict[str, list[str]]:
    if not isinstance(entry, dict):
        return {}
    references = entry.get("references")
    if not isinstance(references, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key in ("code", "tests", "docs"):
        values = references.get(key)
        if isinstance(values, str):
            normalized[key] = [values]
        elif isinstance(values, list):
            normalized[key] = [str(value) for value in values if str(value).strip()]
    return normalized


def _reference_path_exists(root: Path, reference: str) -> bool:
    path_text = reference.split("#", 1)[0].strip()
    return bool(path_text) and (root / path_text).exists()


def _strict_typing_gate_is_configured(root: Path) -> bool:
    """Return whether strict mypy is configured and enforced by the full gate."""

    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        validation_script = (root / "scripts" / "docker-validate.sh").read_text(encoding="utf-8")
        mypy_script_path = root / "scripts" / "docker-mypy.sh"
        mypy_script = mypy_script_path.read_text(encoding="utf-8")
        quality_workflow = (root / ".github" / "workflows" / "quality-scale.yml").read_text(encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError):
        return False
    mypy = pyproject.get("tool", {}).get("mypy", {})
    return bool(
        mypy.get("strict") is True
        and mypy.get("files") == ["custom_components/ha_energy_planner"]
        and not mypy.get("exclude")
        and not mypy.get("disable_error_code")
        and not mypy.get("overrides")
        and mypy_script_path.stat().st_mode & 0o111
        and "ghcr.io/home-assistant/home-assistant:2026.8.2" in mypy_script
        and '"mypy==1.19.1"' in mypy_script
        and "python3 -m mypy" in mypy_script
        and "run scripts/docker-mypy.sh" in validation_script
        and "run: scripts/docker-mypy.sh" in quality_workflow
    )


def validate_quality_scale(root: Path) -> tuple[int, list[str]]:
    """Return exit code and validation messages."""

    messages: list[str] = []
    try:
        claimed = _claimed_level(root)
        quality = _load_yaml(root / "quality_scale.yaml")
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return 1, [f"ERROR: {err}"]

    levels = quality.get("levels") or {}
    rules = quality.get("rules") or {}
    if claimed != EXPECTED_QUALITY_SCALE:
        messages.append(f"ERROR: Manifest must claim quality_scale {EXPECTED_QUALITY_SCALE!r}; got {claimed!r}")
    catalog_errors = []
    for level in QUALITY_LEVEL_ORDER:
        level_entry = levels.get(level) or {}
        actual = tuple(str(rule) for rule in level_entry.get("required") or [])
        expected = QUALITY_RULES_BY_LEVEL[level]
        if actual != expected:
            catalog_errors.append(level)
    if catalog_errors:
        messages.append(
            "ERROR: Quality scale catalog differs from the pinned Home Assistant rules: "
            + ", ".join(catalog_errors)
        )

    canonical_rules = {rule for level in QUALITY_LEVEL_ORDER for rule in QUALITY_RULES_BY_LEVEL[level]}
    unknown_rules = sorted(set(rules) - canonical_rules)
    if unknown_rules:
        messages.append("ERROR: Unknown quality scale rule evidence: " + ", ".join(unknown_rules))

    required_rules = [
        rule
        for level in _required_levels_for_claim(claimed)
        for rule in QUALITY_RULES_BY_LEVEL[level]
    ]

    missing_rules = [rule for rule in required_rules if rule not in rules]
    if missing_rules:
        messages.append(f"ERROR: Missing rule evidence: {', '.join(missing_rules)}")

    incomplete_rules = []
    unknown_status_rules = []
    bad_na_rules = []
    missing_na_comments = []
    broken_references = []
    generated_artifacts = [
        str(path.relative_to(root)) for path in (root / "custom_components" / DOMAIN).rglob("__pycache__")
    ]

    for rule, entry in rules.items():
        status = _status_for_rule(entry)
        if status not in VALID_STATUSES:
            unknown_status_rules.append(str(rule))

        if rule in required_rules and status not in {"done", "exempt"}:
            incomplete_rules.append(rule)
            continue
        if status == "exempt":
            if rule not in EXEMPT_ALLOWED_RULES:
                bad_na_rules.append(rule)
            if not isinstance(entry, dict) or not str(entry.get("comment") or "").strip():
                missing_na_comments.append(rule)
        for refs in _references_for_rule(entry).values():
            for reference in refs:
                if not _reference_path_exists(root, reference):
                    broken_references.append(f"{rule}: {reference}")

    if unknown_status_rules:
        messages.append("ERROR: Rules have an unsupported status: " + ", ".join(unknown_status_rules))
    if incomplete_rules:
        messages.append(f"ERROR: Rules not marked done or exempt: {', '.join(incomplete_rules)}")
    if bad_na_rules:
        messages.append("ERROR: Rules marked exempt without an allowlist exception: " + ", ".join(bad_na_rules))
    if missing_na_comments:
        messages.append("ERROR: exempt rules missing explanatory comments: " + ", ".join(missing_na_comments))
    if broken_references:
        messages.append("ERROR: Broken quality scale references: " + ", ".join(broken_references))
    if generated_artifacts:
        messages.append(
            "ERROR: Generated cache directories must not be present in the integration: "
            + ", ".join(generated_artifacts)
        )
    if _status_for_rule(rules.get("strict-typing")) == "done" and not _strict_typing_gate_is_configured(root):
        messages.append(
            "ERROR: strict-typing cannot be marked done without strict mypy configuration "
            "enforced by scripts/docker-validate.sh"
        )

    return (1 if messages else 0), messages


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    exit_code, messages = validate_quality_scale(args.repo_root.resolve())
    for message in messages:
        print(message, file=sys.stderr)
    if exit_code == 0:
        print("Quality scale evidence is valid.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
