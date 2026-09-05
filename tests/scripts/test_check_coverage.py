"""The statement gate stays exact when branch instrumentation is enabled."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _checker():
    path = Path(__file__).resolve().parents[2] / "scripts/check_coverage.py"
    spec = importlib.util.spec_from_file_location("check_coverage", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_coverage


def _report(*, missing: int = 0, branches: int = 10):
    return {
        "meta": {"branch_coverage": True},
        "files": {
            "component.py": {
                "summary": {
                    "num_statements": 1000,
                    "covered_lines": 1000 - missing,
                    "missing_lines": missing,
                    "num_branches": branches,
                    "covered_branches": branches // 2,
                },
                "missing_lines": [123] if missing else [],
            }
        },
    }


def test_branch_gaps_are_reported_without_weakening_statement_gate() -> None:
    check = _checker()
    status, messages = check(_report())
    assert status == 0
    assert "Branch coverage: 5/10 (50.00%)" in messages
    status, messages = check(_report(missing=1))
    assert status == 1
    assert "uncovered statements [123]" in messages[0]


def test_missing_or_uninstrumented_evidence_fails() -> None:
    check = _checker()
    assert check({})[0] == 1
    report = _report()
    report["meta"]["branch_coverage"] = False
    assert check(report)[0] == 1


def test_modules_without_branches_are_valid() -> None:
    assert _checker()(_report(branches=0)) == (
        0, ["Statement coverage: 1000/1000 (100.00%)", "Branch coverage: no branches"]
    )
