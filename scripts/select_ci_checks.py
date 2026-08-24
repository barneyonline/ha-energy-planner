#!/usr/bin/env python3
"""Select expensive CI checks from the files changed by a pull request."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path


@dataclass(frozen=True)
class CheckSelection:
    """Expensive checks required for one change set."""

    pytest: bool
    quality_scale: bool
    validation_extras: bool
    dedicated_quality_scale: bool


_ALL_CHECKS = {
    ".github/workflows/ci.yml",
    ".github/workflows/tests.yml",
    "pyproject.toml",
}

_PYTEST_PATTERNS = (
    "custom_components/ha_energy_planner/**",
    "tests/**",
    "scripts/*.py",
    "scripts/**/*.py",
    "scripts/docker-validate.sh",
)

_DEDICATED_QUALITY_SCALE_PATTERNS = (
    "custom_components/ha_energy_planner/**",
    "tests/scripts/test_validate_quality_scale.py",
    "tests/test_manifest.py",
    "docs/**",
    "README.md",
    "ha-energy-planner-spec.md",
    "hacs.json",
    "quality_scale.yaml",
    "scripts/validate_quality_scale.py",
    "scripts/docker-validate.sh",
    "pyproject.toml",
    ".github/workflows/quality-scale.yml",
    ".github/workflows/tests.yml",
)

_QUALITY_SCALE_PATTERNS = _DEDICATED_QUALITY_SCALE_PATTERNS

_VALIDATION_PATTERNS = (
    "custom_components/ha_energy_planner/**",
    "tests/**",
    "scripts/**",
)

_WORKFLOW_ONLY_PATHS = {
    ".github/workflows/codespell.yml",
    ".github/workflows/hassfest.yml",
    ".github/workflows/quality-scale.yml",
    ".github/workflows/release-assets.yml",
    ".github/workflows/validate.yaml",
}


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def select_checks(paths: list[str], *, force_all: bool = False) -> CheckSelection:
    """Return the checks needed for normalized repository-relative paths."""
    normalized = {path.strip().removeprefix("./") for path in paths if path.strip()}
    dedicated_quality_scale = any(
        _matches(path, _DEDICATED_QUALITY_SCALE_PATTERNS) for path in normalized
    )
    if force_all:
        return CheckSelection(True, True, True, False)
    if normalized & _ALL_CHECKS:
        return CheckSelection(True, True, True, dedicated_quality_scale)

    pytest = any(_matches(path, _PYTEST_PATTERNS) for path in normalized)
    quality_scale = any(_matches(path, _QUALITY_SCALE_PATTERNS) for path in normalized)
    validation_extras = any(_matches(path, _VALIDATION_PATTERNS) for path in normalized)

    recognized = {
        path
        for path in normalized
        if _matches(path, _PYTEST_PATTERNS + _QUALITY_SCALE_PATTERNS + _VALIDATION_PATTERNS)
        or path in _WORKFLOW_ONLY_PATHS
    }
    if normalized - recognized:
        # A new path added to the workflow trigger must fail safe until its
        # impact is deliberately classified here.
        return CheckSelection(True, True, True, False)
    return CheckSelection(pytest, quality_scale, validation_extras, dedicated_quality_scale)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--all", action="store_true", dest="force_all")
    return parser.parse_args()


def main() -> int:
    """Print values in GitHub Actions output format."""
    args = _parse_args()
    paths = [] if args.paths_file is None else args.paths_file.read_text(encoding="utf-8").splitlines()
    selection = select_checks(paths, force_all=args.force_all)
    print(f"pytest={str(selection.pytest).lower()}")
    print(f"quality_scale={str(selection.quality_scale).lower()}")
    print(f"validation_extras={str(selection.validation_extras).lower()}")
    print(f"dedicated_quality_scale={str(selection.dedicated_quality_scale).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
