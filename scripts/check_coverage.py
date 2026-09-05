#!/usr/bin/env python3
"""Enforce complete statement coverage and report branch evidence separately."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def check_coverage(report: dict[str, Any], branch_limits: dict[str, int] | None = None) -> tuple[int, list[str]]:
    """Keep the exact 100% statement gate without conflating branch coverage."""
    files = report.get("files", {})
    if not files or not report.get("meta", {}).get("branch_coverage"):
        return 1, ["ERROR: nonempty branch-instrumented coverage evidence is required"]
    messages: list[str] = []
    statements = covered = branches = covered_branches = 0
    branch_regression = False
    for name, evidence in sorted(files.items()):
        summary = evidence["summary"]
        statements += summary["num_statements"]
        covered += summary["covered_lines"]
        branches += summary["num_branches"]
        covered_branches += summary["covered_branches"]
        missing_branches = summary["num_branches"] - summary["covered_branches"]
        if branch_limits is not None and missing_branches > branch_limits.get(name, 0):
            messages.append(f"ERROR: {name}: uncovered branches {missing_branches} exceed reviewed baseline")
            branch_regression = True
        if summary["missing_lines"]:
            messages.append(f"ERROR: {name}: uncovered statements {evidence['missing_lines']}")
    if not statements:
        return 1, ["ERROR: coverage evidence contains no executable statements"]
    messages.append(f"Statement coverage: {covered}/{statements} ({100 * covered / statements:.2f}%)")
    if branches:
        messages.append(f"Branch coverage: {covered_branches}/{branches} ({100 * covered_branches / branches:.2f}%)")
    else:
        messages.append("Branch coverage: no branches")
    return int(covered != statements or branch_regression), messages


def main() -> int:
    """Check the JSON emitted by coverage.py."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--branch-baseline", type=Path,
        default=Path(__file__).resolve().parents[1] / "tests/branch-coverage-baseline.json",
    )
    args = parser.parse_args()
    try:
        status, messages = check_coverage(
            json.loads(args.report.read_text(encoding="utf-8")),
            json.loads(args.branch_baseline.read_text(encoding="utf-8")),
        )
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f"ERROR: invalid coverage evidence: {err}")
        return 1
    print("\n".join(messages))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
