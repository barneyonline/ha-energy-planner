"""Smoke diagnostics keep expected upstream notices and reject new warnings."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("smoke_warnings", ROOT / "scripts/report-smoke-warnings.py")
assert SPEC is not None and SPEC.loader is not None
REPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORTER)


def log(logger, message, level="WARNING"):
    return f"2026-10-31 00:23:19.073 {level} (MainThread) [{logger}] {message}"


class SmokeWarningTests(unittest.TestCase):
    def test_known_upstream_notices_are_preserved(self):
        lines = [
            log("homeassistant.loader", REPORTER._LOADER_MESSAGE.format(domain=domain))
            for domain in ("ha_energy_planner", "fake_planner_test")
        ]
        lines.append(log("py.warnings", "/usr/local/lib/python3.14/site-packages/rich/segment.py:547: "
                         "SyntaxWarning: 'return' in a 'finally' block"))
        records = REPORTER.collect_diagnostics("\x1b[33m" + "\n".join(lines) + "\x1b[0m")
        self.assertEqual(len(records), 3)
        self.assertTrue(all(item["expected"] for item in records))
        self.assertEqual(records[-1]["category"], "rich_10_16_syntax")

    def test_near_matches_and_critical_messages_are_not_allowlisted(self):
        messages = [
            log("homeassistant.loader", REPORTER._LOADER_MESSAGE.format(domain="other_component")),
            log("custom_components.ha_energy_planner", "The planner raised a warning"),
            log("homeassistant.loader", REPORTER._LOADER_MESSAGE.format(domain="ha_energy_planner"), "ERROR"),
            log("py.warnings", "/usr/local/lib/python3.14/site-packages/ours/segment.py:547: "
                "SyntaxWarning: 'return' in a 'finally' block"),
            log("homeassistant.helpers.entity", "Updating state for sensor.energy_planner_next_actions took 3 seconds"),
            log("homeassistant.helpers.service", "Referenced entities weather.smoke_weather_forecast are missing"),
            "our_module.py:2: RuntimeWarning: coroutine was never awaited",
        ]
        records = REPORTER.collect_diagnostics("\n".join(messages))
        self.assertEqual(len(records), len(messages))
        self.assertTrue(all(not item["expected"] for item in records))

    def test_reports_both_expected_and_unexpected_records_before_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            console = root / "console.log"
            console.write_text("\n".join([
                log("homeassistant.loader", REPORTER._LOADER_MESSAGE.format(domain="ha_energy_planner")),
                log("custom_components.ha_energy_planner", "Synthetic failure", "ERROR"),
            ]))
            output = root / "artifacts"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/report-smoke-warnings.py"), str(console),
                 "--output-dir", str(output)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 1)
            report = json.loads((output / "warnings.json").read_text())
            self.assertEqual((report["expected"], report["unexpected"]), (1, 1))
            self.assertIn("Synthetic failure", (output / "warnings.txt").read_text())
            self.assertIn("EXPECTED", (output / "warnings.txt").read_text())
            self.assertIn("Unexpected ERROR", completed.stdout)

    def test_clean_console_succeeds_and_still_writes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            console = root / "console.log"
            console.write_text("2026-10-31 DEBUG (MainThread) [custom_components.ha_energy_planner] ok\n")
            self.assertTrue(REPORTER.report(console, root))
            self.assertEqual(json.loads((root / "warnings.json").read_text())["records"], [])

class SmokeEvidenceContractTests(unittest.TestCase):
    def test_docker_failure_preserves_status_and_exports_only_synthetic_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "docker"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + log(
                "custom_components.ha_energy_planner", "Synthetic runtime failure", "ERROR",
            ) + "'\nexit 42\n")
            executable.chmod(0o755)
            environment = {
                **os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "HEP_HA_IMAGE": "synthetic", "HEP_VALIDATION_ARTIFACTS_DIR": str(root / "evidence"),
                "HEP_COMPONENT_DIR": str(ROOT / "custom_components/ha_energy_planner"), "KEEP_HA_SMOKE": "0",
            }
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts/docker-ha-smoke.sh")],
                env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 42, completed.stdout + completed.stderr)
            evidence = root / "evidence"
            self.assertEqual({path.name for path in evidence.iterdir()}, {
                "docker-console.log", "warnings.json", "warnings.txt",
            })
            self.assertNotIn("docker-smoke-secret-token", "".join(path.read_text() for path in evidence.iterdir()))
            self.assertEqual(json.loads((evidence / "warnings.json").read_text())["unexpected"], 1)
            self.assertNotIn("Docker smoke test passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
