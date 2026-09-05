#!/usr/bin/env python3
"""Single source for release validation versions; HACS owns the minimum."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def support_policy(root: Path) -> dict[str, object]:
    minimum = json.loads((root / "hacs.json").read_text())["homeassistant"]
    policy = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["energy-planner"]["support"]
    baseline = policy["baseline"]
    versions = list(dict.fromkeys([minimum, *policy["previous"], baseline]))
    return {**policy, "minimum": minimum, "matrix": [*versions, "stable"],
            "versions": versions, "image": f"ghcr.io/home-assistant/home-assistant:{baseline}"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("field", choices=["minimum", "baseline", "matrix", "versions", "image", "mypy"])
    args = parser.parse_args()
    value = support_policy(Path(__file__).resolve().parents[1])[args.field]
    print(json.dumps(value) if isinstance(value, list) else value)
