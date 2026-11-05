"""Regress the Codecov inventory, documented XML paths and processing results."""

from __future__ import annotations

import copy
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

from scripts import codecov_reporting as reporting

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
PACKAGE = "custom_components/ha_energy_planner"


class CheckoutTests(unittest.TestCase):
    def test_git_index_uses_producer_paths_without_materializing_or_executing_source(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/coverage-reporting.yml").read_text())
        script = next(step["run"] for step in workflow["jobs"]["upload"]["steps"] if step.get("id") == "uploader")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin, source = root / "origin", root / "codecov-source"
            subprocess.run(["git", "init", "-qb", "main", str(origin)], check=True, capture_output=True)
            (origin / "codecov").symlink_to(root / "must-not-overwrite")
            (origin / "codecov.yml").write_text("{}\n")
            (origin / "sample.py").write_text("# trusted source\n")

            def commit(message: str) -> None:
                subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(origin),
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.test",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "-qm",
                        message,
                    ],
                    check=True,
                    capture_output=True,
                )

            commit("trusted")
            subprocess.run(["git", "clone", "-q", str(origin), str(source)], check=True, capture_output=True)
            (origin / "candidate-only.py").write_text("raise RuntimeError('candidate must never run')\n")
            (origin / "codecov.yml").write_text("plugins: [candidate-program]\n")
            commit("producer")
            sha = subprocess.check_output(["git", "-C", str(origin), "rev-parse", "HEAD"], text=True).strip()
            output = root / "output"
            subprocess.run(
                ["bash", "-e", "-c", script],
                env={**os.environ, "GITHUB_WORKSPACE": temporary, "GITHUB_OUTPUT": str(output), "SOURCE_SHA": sha},
                check=True,
                capture_output=True,
            )
            work = Path(output.read_text().strip().removeprefix("path="))
            self.assertEqual({}, yaml.safe_load((work / "codecov.yml").read_text()))
            self.assertEqual({}, yaml.safe_load((source / "codecov.yml").read_text()))
            self.assertFalse((source / "candidate-only.py").exists())
            for name in ("codecov", "codecov.SHA256SUM", "codecov.SHA256SUM.sig"):
                (work / name).write_text("trusted wrapper download")
            network = (
                subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"], cwd=work).decode().split("\0")
            )
            self.assertEqual({"codecov", "codecov.yml", "sample.py", "candidate-only.py"}, set(network) - {""})
            self.assertFalse((root / "must-not-overwrite").exists())


class XmlPathsTests(unittest.TestCase):
    def test_actual_producer_command_preserves_full_paths_and_covered_uncovered_lines(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
        checkout = workflow["jobs"]["pytest-coverage"]["steps"][0]["with"]
        self.assertEqual("${{ github.event.pull_request.head.sha || github.sha }}", checkout["ref"])
        script = next(
            step["run"] for step in workflow["jobs"]["pytest-coverage"]["steps"] if step.get("id") == "pytest"
        )
        command = re.search(r"python3 -m coverage xml.*?\|\| report_status=", script, re.DOTALL).group()
        args = shlex.split(command.split("||")[0].replace("\\\n", " "))
        args[0] = sys.executable
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / PACKAGE
            (package / "nested").mkdir(parents=True)
            for path in (package / "__init__.py", package / "nested/__init__.py"):
                path.touch()
            (package / "sample.py").write_text("def choose(flag):\n    if flag:\n        return 1\n    return 0\n")
            (package / "nested/sample.py").write_text("UNEXECUTED = 1\n")
            (root / "run.py").write_text(
                "from custom_components.ha_energy_planner.sample import choose\nchoose(True)\n"
            )
            (root / ".coveragerc").write_text(f"[run]\nsource = {PACKAGE}\nbranch = True\n")
            subprocess.run(
                [sys.executable, "-m", "coverage", "run", "run.py"], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                [sys.executable, "-m", "coverage", "xml", "-o", "before.xml"], cwd=root, check=True, capture_output=True
            )
            subprocess.run(args, cwd=root, check=True, capture_output=True)
            before, after = ET.parse(root / "before.xml").getroot(), ET.parse(root / "coverage.xml").getroot()
            old = {cls.attrib["filename"]: [line.attrib for line in cls.iter("line")] for cls in before.iter("class")}
            new = {cls.attrib["filename"]: [line.attrib for line in cls.iter("line")] for cls in after.iter("class")}
            self.assertIn("sample.py", old)
            self.assertEqual({f"{PACKAGE}/{name}": lines for name, lines in old.items()}, new)
            self.assertTrue(any(line["hits"] == "1" for line in new[f"{PACKAGE}/sample.py"]))
            self.assertTrue(any(line["hits"] == "0" for line in new[f"{PACKAGE}/sample.py"]))
            self.assertEqual("0", new[f"{PACKAGE}/nested/sample.py"][0]["hits"])
            for attribute in ("lines-valid", "lines-covered", "branches-valid", "branches-covered"):
                self.assertEqual(before.attrib[attribute], after.attrib[attribute])


class ProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Observed public API shapes: merged coverage with upload totals; JUnit
        # processed/UPLOADED with null name, build metadata and totals. IDs sanitized.
        self.commit = {"commitid": SHA, "state": "complete", "totals": {"files": 32, "lines": 4697}}
        self.uploads = [
            {
                "name": "coverage-123-2",
                "job_code": "Coverage Reporting",
                "build_code": None,
                "state": "merged",
                "state_id": 6,
                "state_name": "MERGED",
                "totals": {"files": 32, "lines": 4697},
            },
            {
                "name": None,
                "job_code": "junit-123-2",
                "build_code": None,
                "build_url": None,
                "state": "processed",
                "state_id": 1,
                "state_name": "UPLOADED",
                "totals": None,
                "storage_path": "test_results/v1/raw/report",
            },
        ]

    def ready(self) -> bool:
        return reporting.delivery_ready(self.commit, self.uploads, SHA, "123", "2")

    def test_observed_success_and_null_metadata_are_supported(self) -> None:
        self.assertTrue(self.ready())

    def test_pending_transport_only_or_other_attempt_cannot_borrow_old_project_totals(self) -> None:
        for changes in ({"state": "uploaded"}, {"state": "processed", "totals": None}, {"name": "coverage-123-1"}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(self.uploads)
                self.uploads[0].update(changes)
                self.assertFalse(self.ready())
                self.uploads = original
        self.uploads[1]["job_code"] = "junit-123-1"
        self.assertFalse(self.ready())

    def test_unusable_empty_failed_or_misattributed_processing_fails(self) -> None:
        changes = [
            lambda: self.uploads[0].update(state="error", state_id=3, state_name="ERROR", totals=None),
            lambda: self.uploads[1].update(state="error"),
            lambda: self.uploads[0].update(totals=None),
            lambda: self.commit.update(totals={"files": 0, "lines": 0}),
            lambda: self.commit.update(state="error"),
            lambda: self.commit.update(commitid="b" * 40),
            lambda: self.uploads[1].update(storage_path="coverage/raw/report"),
            lambda: self.uploads.append(dict(self.uploads[0])),
        ]
        for mutate in changes:
            self.setUp()
            mutate()
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.ready()

    def test_verify_reads_exact_commit_and_paginates_uploads(self) -> None:
        calls = []

        def fetch(url: str) -> dict:
            calls.append(url)
            if "page=2" in url:
                return {"results": self.uploads, "next": None}
            if "uploads/" in url:
                return {"results": [], "next": "?page=2"}
            return self.commit

        reporting.verify(SHA, "barneyonline/ha-energy-planner", "123", "2", fetch=fetch)
        self.assertEqual(3, len(calls))
        self.assertTrue(all(f"/commits/{SHA}/" in url for url in calls))

    def test_timeout_is_bounded_and_http_errors_are_closed(self) -> None:
        sleeps = []
        error = HTTPError("url", 404, "not ready", {}, None)
        with self.assertRaisesRegex(ValueError, "Timed out"):
            reporting.verify(
                SHA,
                "barneyonline/ha-energy-planner",
                "123",
                "2",
                fetch=lambda _: (_ for _ in ()).throw(error),
                sleep=sleeps.append,
            )
        self.assertEqual([10] * 11, sleeps)
        self.assertTrue(error.closed)
        with patch.object(reporting.time, "monotonic", side_effect=[0, 241]):
            with self.assertRaisesRegex(ValueError, "deadline"):
                reporting.verify(SHA, "barneyonline/ha-energy-planner", "123", "2")

    def test_api_requests_are_anonymous_and_restricted_to_codecov(self) -> None:
        with self.assertRaises(ValueError):
            reporting.fetch_json("https://example.com/")
        with patch.object(reporting, "urlopen") as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = b'{"state":"complete"}'
            self.assertEqual({"state": "complete"}, reporting.fetch_json("https://api.codecov.io/api/v2/github/test"))
            fetch.assert_called_once_with("https://api.codecov.io/api/v2/github/test", timeout=10)


class WorkflowTests(unittest.TestCase):
    def test_documented_inputs_keep_oidc_isolation_attribution_and_budgets(self) -> None:
        workflow = yaml.load((ROOT / ".github/workflows/coverage-reporting.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual({"workflow_run"}, set(workflow["on"]))
        job = workflow["jobs"]["upload"]
        self.assertEqual(
            {"contents": "read", "actions": "read", "pull-requests": "read", "id-token": "write"}, job["permissions"]
        )
        steps = job["steps"]
        checkout = next(step["with"] for step in steps if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual("${{ github.workflow_sha }}", checkout["ref"])
        for setting in ("persist-credentials", "submodules", "lfs"):
            self.assertEqual("false", checkout[setting])
        for step in steps:
            if step.get("uses", "").startswith("codecov/"):
                settings = step["with"]
                self.assertEqual("${{ github.workspace }}/codecov-source", settings["root_dir"])
                self.assertEqual("${{ steps.uploader.outputs.path }}", settings["working-directory"])
                self.assertEqual("${{ steps.uploader.outputs.path }}/codecov.yml", settings["codecov_yml_path"])
                for setting in ("use_oidc", "disable_search", "disable_file_fixes", "fail_ci_if_error"):
                    self.assertEqual("true", settings[setting])
                self.assertEqual("noop", settings["plugins"])
                self.assertEqual("${{ steps.source.outputs.sha }}", settings["override_commit"])
        validation = next(step["run"] for step in steps if step["name"] == "Validate report files")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("coverage.xml", "junit.xml"):
                (root / name).write_text("report")

            def check() -> int:
                return subprocess.run(
                    ["bash", "-e", "-c", validation],
                    env={**os.environ, "REPORTS": temporary},
                    capture_output=True,
                    check=False,
                ).returncode

            self.assertEqual(0, check())
            (root / "coverage.json").symlink_to(root / "coverage.xml")
            self.assertNotEqual(0, check())
        self.assertIn("always()", steps[-1]["if"])


if __name__ == "__main__":
    unittest.main()
