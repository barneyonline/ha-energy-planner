#!/usr/bin/env python3
"""Check development dependencies without installing packages or running tests."""

from __future__ import annotations

import json
import platform
import sys
from importlib.metadata import version
from pathlib import Path

from support_policy import support_policy


def expected_versions(root: Path) -> dict[str, str]:
    """Read development pins from the repository's version contracts."""
    policy = support_policy(root)
    expected = {name: str(policy[name]) for name in ("mypy", "ruff")}
    expected["homeassistant"] = str(policy["baseline"])
    for line in (root / "tests/requirements.txt").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, separator, pinned = line.strip().partition("==")
            if not separator or not pinned:
                raise ValueError(f"Development requirement must be pinned: {name}")
            expected[name] = pinned
    return expected


def verify(root: Path) -> dict[str, str]:
    """Fail on stale tooling or a Python version outside the dev contract."""
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("ha-dev requires Python 3.14")
    expected = expected_versions(root)
    actual = {name: version(name) for name in expected}
    if actual != expected:
        raise RuntimeError("ha-dev dependencies changed; run scripts/ha-dev.sh build and start")
    return {"python": platform.python_version(), **actual}


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parents[1]), indent=2))
