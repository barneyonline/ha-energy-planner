#!/usr/bin/env python3
"""Require a completed, revision-bound operating record for stable 1.x releases."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

SCENARIOS = ("startup_reload", "ev_cycle", "unavailable_feedback", "hvac_override", "enphase_restore", "performance")


def validate_evidence(evidence: dict, commit: str, version: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or evidence.get("commit") != commit:
        raise ValueError("Operating evidence must match the full release commit")
    if evidence.get("version") != version.removeprefix("v"):
        raise ValueError("Operating evidence must match the release version")
    start = datetime.fromisoformat(evidence["started_at"])
    end = datetime.fromisoformat(evidence["ended_at"])
    if start.tzinfo is None or end.tzinfo is None or (end - start).total_seconds() < 48 * 3600:
        raise ValueError("At least 48 hours of timestamped observation is required")
    if end > datetime.now(end.tzinfo):
        raise ValueError("Observation must already be complete")
    for name in SCENARIOS:
        result = evidence.get("scenarios", {}).get(name, {})
        if result.get("result") != "passed" or not str(result.get("evidence", "")).strip():
            raise ValueError(f"Completed evidence is required for {name}")


def requires_observation(version: str) -> bool:
    parsed = re.fullmatch(r"v?(\d+)\.\d+\.\d+", version)
    return parsed is not None and int(parsed[1]) >= 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=Path("release-evidence.json"))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", default=json.loads(
        (Path(__file__).resolve().parents[1] / "custom_components/ha_energy_planner/manifest.json").read_text()
    )["version"])
    args = parser.parse_args()
    if requires_observation(args.version):
        try:
            validate_evidence(json.loads(args.evidence.read_text()), args.commit, args.version)
        except (OSError, KeyError, TypeError, ValueError) as err:
            raise SystemExit(f"Release observation incomplete: {err}") from None
        print("Release operating evidence passed")
    else:
        print("Operating record is mandatory for stable releases from 1.0; this is a development/candidate release")
