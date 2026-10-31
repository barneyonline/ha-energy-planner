"""Smoke diagnostics keep expected upstream notices and reject new warnings."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("smoke_warnings", ROOT / "scripts/report-smoke-warnings.py")
assert SPEC is not None and SPEC.loader is not None
REPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORTER)


def log(logger, message, level="WARNING"):
    return f"2026-10-31 00:23:19.073 {level} (MainThread) [{logger}] {message}"


class SmokeWarningTests(unittest.TestCase):
    def test_default_worker_thread_diagnostics_fail_the_report(self):
        stream = io.StringIO()
        logger = logging.Logger("smoke_worker", level=logging.WARNING)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s (%(threadName)s) [%(name)s] %(message)s",
        ))
        logger.addHandler(handler)

        def emit():
            for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
                logger.log(level, "Synthetic worker diagnostic")

        worker = threading.Thread(target=emit)
        worker.start()
        worker.join()
        self.assertIn("(emit)", worker.name)
        records = REPORTER.collect_diagnostics(stream.getvalue())
        self.assertEqual([item["level"] for item in records], ["WARNING", "ERROR", "CRITICAL"])
        self.assertTrue(all(item["logger"] == "smoke_worker" and not item["expected"] for item in records))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            console = root / "console.log"
            console.write_text(stream.getvalue())
            self.assertFalse(REPORTER.report(console, root))
            report = json.loads((root / "warnings.json").read_text())
            self.assertEqual((report["expected"], report["unexpected"]), (0, 3))
        handler.close()
        stream.close()

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
    def test_weather_forecast_keeps_initial_price_peak_in_heating_regime(self):
        source = (ROOT / "scripts/docker-ha-smoke.sh").read_text()
        weather = source.split(
            'cat > "$TMP_DIR/custom_components/fake_planner_test/weather.py" <<\'PY\'\n', 1,
        )[1].split("\nPY\n", 1)[0]
        tree = ast.parse(weather)
        entity = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        forecast = next(node for node in entity.body if isinstance(node, ast.AsyncFunctionDef))
        assertions = ast.parse(source.split('python3 - <<\'PY\' "$TMP_DIR"\n', 1)[1].split("\nPY\n", 1)[0])
        preview_index = next(index for index, node in enumerate(assertions.body) if (
            isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "weather_preview" for target in node.targets
            )
        ))
        preview_check = compile(ast.Module(
            body=assertions.body[preview_index:preview_index + 2], type_ignores=[],
        ), "smoke weather storage assertion", "exec")
        for minute in (0, 59):
            now = datetime(2026, 10, 31, 5, minute, tzinfo=UTC)
            namespace = {"dt_util": SimpleNamespace(utcnow=lambda now=now: now), "timedelta": timedelta}
            exec(compile(ast.Module(body=[forecast], type_ignores=[]), "smoke weather", "exec"), namespace)
            points = asyncio.run(namespace[forecast.name](None))
            self.assertEqual(len(points), 73)
            self.assertLessEqual(datetime.fromisoformat(points[0]["datetime"]), now)
            self.assertGreater(datetime.fromisoformat(points[-1]["datetime"]), now + timedelta(hours=24))
            # The smoke comfort lower bound is 18 C. Every forecast point in
            # the first price peak must establish heat, irrespective of minute.
            self.assertTrue(all(point["native_temperature"] < 18 for point in points))
            # Run the actual final smoke consumer assertion against provider
            # output, so changing the provider cannot leave stale expectations.
            preview = [{"outdoor_temperature_forecast_c": point["native_temperature"]} for point in points[:4]]
            exec(preview_check, {"latest_snapshot": {"preview": preview}})
            for invalid in (
                [{"outdoor_temperature_forecast_c": 21.0}] * 4,  # Current-state fallback is not hourly evidence.
                preview[:3], [], [{}] * 4,
            ):
                with self.subTest(minute=minute, invalid=invalid), self.assertRaisesRegex(
                    SystemExit, "hourly Weather service forecast",
                ):
                    exec(preview_check, {"latest_snapshot": {"preview": invalid}})

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
