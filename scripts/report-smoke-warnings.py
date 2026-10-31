#!/usr/bin/env python3
"""Retain and check smoke runtime diagnostics without hiding upstream messages.

Only the two custom-integration loader notices and the exact Rich 10.16 syntax
warning are accepted. These are external to the synthetic planner behavior;
new messages, including slow entity writes, remain failures. No logger or
Python warning filter is changed in the running Home Assistant instance.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOG_RECORD = re.compile(r"\b(WARNING|ERROR|CRITICAL)\s+\([^)]*\)\s+\[([^]]+)]\s+(.*)")
_PYTHON_WARNING = re.compile(r"\b\w+Warning:")
_LOADER_MESSAGE = (
    "We found a custom integration {domain} which has not been tested by Home Assistant. "
    "This component might cause stability problems, be sure to disable it if you experience issues with Home Assistant"
)
_RICH_MESSAGE = re.compile(
    r"/usr/local/lib/python3\.14/site-packages/rich/segment\.py:547: "
    r"SyntaxWarning: 'return' in a 'finally' block"
)


def known_warning(logger: str, message: str) -> tuple[str, str] | None:
    """Identify only documented upstream diagnostics with exact source matches."""
    if logger == "homeassistant.loader" and message in {
        _LOADER_MESSAGE.format(domain=domain) for domain in ("ha_energy_planner", "fake_planner_test")
    }:
        return (
            "custom_integration_loader",
            "Home Assistant warns for all custom integrations; retain this expected loader notice.",
        )
    if logger == "py.warnings" and _RICH_MESSAGE.fullmatch(message):
        return (
            "rich_10_16_syntax",
            "Bundled Rich dependency; tracked at https://github.com/Textualize/rich/issues/4161.",
        )
    return None


def collect_diagnostics(console: str) -> list[dict[str, str | int | bool]]:
    """Return every warning/error record, with explicit expected ownership."""
    records = []
    for line_number, raw in enumerate(console.splitlines(), 1):
        line = _ANSI.sub("", raw)
        matched = _LOG_RECORD.search(line)
        if matched is None:
            # Do not lose Python diagnostics printed outside HA's logging bridge.
            if not _PYTHON_WARNING.search(line):
                continue
            level, logger, message = "WARNING", "python", line
        else:
            level, logger, message = matched.groups()
        known = known_warning(logger, message) if level == "WARNING" else None
        records.append({
            "line": line_number,
            "level": level,
            "logger": logger,
            "message": message,
            "expected": known is not None,
            "category": known[0] if known else "unexpected_runtime_diagnostic",
            "reason": known[1] if known else "Resolve this diagnostic before accepting the smoke run.",
        })
    return records


def report(console_path: Path, output_dir: Path) -> bool:
    """Write a complete diagnostic inventory and return whether it is accepted."""
    records = collect_diagnostics(console_path.read_text(encoding="utf-8", errors="replace"))
    unexpected = [record for record in records if not record["expected"]]
    summary = {
        "expected": len(records) - len(unexpected),
        "unexpected": len(unexpected),
        "records": records,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "warnings.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    text = [f"Expected upstream warnings: {summary['expected']}; unexpected diagnostics: {len(unexpected)}"]
    for record in records:
        disposition = "EXPECTED" if record["expected"] else "UNEXPECTED"
        text.extend([
            f"{disposition} line {record['line']} [{record['logger']}] {record['message']}",
            f"  {record['reason']}",
        ])
    (output_dir / "warnings.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    print(text[0])
    for record in unexpected:
        print(f"Unexpected {record['level']}: [{record['logger']}] {record['message']}")
    return not unexpected


def main() -> int:
    """Exit nonzero for unknown diagnostics while preserving the full report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("console", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    return 0 if report(args.console, args.output_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
