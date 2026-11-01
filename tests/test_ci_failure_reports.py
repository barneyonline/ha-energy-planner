"""Execute the actual producer program with deterministic process boundaries."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "test_status,report_status,check_status,dependency_status,expected",
    [
        (0, 0, 0, 0, 0),
        (1, 0, 0, 0, 1),
        (2, 3, 0, 0, 2),
        (0, 3, 0, 0, 3),
        (0, 0, 4, 0, 4),
        (0, 0, 0, 5, 5),
    ],
)
def test_reports_are_attempted_without_erasing_test_or_coverage_failures(
    tmp_path: Path,
    test_status: int,
    report_status: int,
    check_status: int,
    dependency_status: int,
    expected: int,
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    step = next(
        s
        for s in workflow["jobs"]["pytest-coverage"]["steps"]
        if s.get("id") == "pytest"
    )
    argv = shlex.split(step["run"])
    assert argv[-3:-1] == ["sh", "-c"]
    program = argv[-1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install-test-dependencies.sh").write_text('exit "$DEPENDENCY_STATUS"\n')
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python3"
    python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  *"-m pytest"*) exit "$TEST_STATUS" ;;\n'
        '  *"coverage json"*) exit "$REPORT_STATUS" ;;\n'
        '  *"check_coverage.py"*) exit "$CHECK_STATUS" ;;\n'
        "  *) exit 0 ;;\nesac\n"
    )
    python.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["sh", "-c", program],
        cwd=tmp_path,
        env={
            "PATH": str(binary) + os.pathsep + os.defpath,
            "CALLS": str(calls),
            "TEST_STATUS": str(test_status),
            "REPORT_STATUS": str(report_status),
            "CHECK_STATUS": str(check_status),
            "DEPENDENCY_STATUS": str(dependency_status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    if dependency_status:
        assert not calls.exists()
    else:
        recorded = calls.read_text().splitlines()
        assert len(recorded) == 5
        assert "-m pytest" in recorded[0]
        assert "coverage json" in recorded[1]
        assert "coverage xml" in recorded[2]
        assert "coverage report" in recorded[3]
        assert "check_coverage.py" in recorded[4]
