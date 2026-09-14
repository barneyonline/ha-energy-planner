"""Compatibility isolation must retain all modules and propagate process failures."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = {
    "test_ha_runtime.py", "test_control_runtime.py", "test_device_registry_runtime.py",
    "test_storage_runtime.py", "test_upgrade_runtime.py", "test_config_flow.py",
    "test_lifecycle.py", "test_services.py", "test_calendar.py", "test_manifest.py",
}


@pytest.mark.parametrize("exit_code", [0, 1, 139])
def test_isolated_modules_preserve_test_and_crash_failures(tmp_path: Path, exit_code: int) -> None:
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS_FILE"\n'
        'case "$*" in *tests/test_ha_runtime.py*) exit "$DOCKER_TEST_EXIT";; esac\n'
        'exit 0\n'
    )
    docker.chmod(0o755)
    calls_file = tmp_path / "calls"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/docker-compatibility.sh"), "2026.6.0", "2026.9.0"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "CALLS_FILE": str(calls_file), "DOCKER_TEST_EXIT": str(exit_code)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == exit_code, result.stderr
    calls = calls_file.read_text().splitlines()
    versions = ["2026.6.0", "2026.9.0"] if exit_code == 0 else ["2026.6.0"]
    assert len(calls) == len(MODULES) + (exit_code == 0)
    for version in versions:
        version_calls = [line for line in calls if f"home-assistant:{version} " in line]
        assert {Path(arg).name for line in version_calls for arg in line.split() if arg.startswith("tests/")} == MODULES
        expected_count = 1 if version == "2026.6.0" else len(MODULES)
        assert all(sum(arg.startswith("tests/") for arg in line.split()) == expected_count for line in version_calls)
        assert all("-X faulthandler" in line for line in version_calls)
