"""Coverage gates reject even one missed statement or branch without rounding."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_coverage.py"


def _checker():
    spec = importlib.util.spec_from_file_location("check_coverage", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_coverage


def _report(*, missing: int = 0, missing_branches: int = 0, branches: int = 10):
    return {
        "meta": {"branch_coverage": True},
        "files": {
            "component.py": {
                "summary": {
                    "num_statements": 100_000,
                    "covered_lines": 100_000 - missing,
                    "missing_lines": missing,
                    "num_branches": branches,
                    "covered_branches": branches - missing_branches,
                },
                "missing_lines": [123] if missing else [],
                "missing_branches": [[123, 124]] if missing_branches else [],
            }
        },
    }


@pytest.mark.parametrize(
    ("missing", "branches", "error"),
    [
        (1, 0, "uncovered statements [123]"),
        (0, 1, "uncovered branches [[123, 124]]"),
    ],
)
def test_one_missing_statement_or_branch_fails_even_when_percentage_rounds_to_100(missing, branches, error):
    report = _report(missing=missing, missing_branches=branches, branches=100_000)
    status, messages = _checker()(report)
    assert status == 1
    assert any(error in message for message in messages)
    assert any("100.00%" in message for message in messages)


def test_complete_statement_and_branch_evidence_passes():
    assert _checker()(_report()) == (
        0,
        [
            "Statement coverage: 100000/100000 (100.00%)",
            "Branch coverage: 10/10 (100.00%)",
        ],
    )


def test_missing_or_uninstrumented_evidence_fails():
    check = _checker()
    assert check({})[0] == 1
    report = _report()
    report["meta"]["branch_coverage"] = False
    assert check(report)[0] == 1
    report = _report()
    report["files"]["component.py"]["summary"]["num_statements"] = 0
    assert check(report)[0] == 1


def test_modules_without_branches_are_valid():
    assert _checker()(_report(branches=0)) == (
        0,
        ["Statement coverage: 100000/100000 (100.00%)", "Branch coverage: no branches"],
    )


@pytest.mark.parametrize("evidence", ["missing", "malformed", "incomplete", "valid", "branch_gap"])
def test_command_line_checks_generated_evidence(tmp_path, evidence):
    path = tmp_path / "coverage.json"
    if evidence != "missing":
        path.write_text(
            "invalid"
            if evidence == "malformed"
            else json.dumps(
                {"meta": {"branch_coverage": True}, "files": {"component.py": {}}}
                if evidence == "incomplete"
                else _report(missing_branches=1 if evidence == "branch_gap" else 0)
            )
        )
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == (0 if evidence == "valid" else 1)
    assert ("ERROR:" in result.stdout) is (evidence != "valid")
