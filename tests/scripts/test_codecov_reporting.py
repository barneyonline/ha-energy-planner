"""Exercise real reporting inputs, source paths and processed-delivery failures."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import yaml

from scripts import codecov_reporting as reporting

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
PACKAGE = reporting.PACKAGE


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.inventory = self.root / "source"
        self.manifest = self.root / "expected.json"
        self.tree = {
            "commit": SHA,
            "truncated": False,
            "tree": [
                {"path": name, "type": "blob", "mode": "100644"}
                for name in [
                    f"{PACKAGE}/__init__.py",
                    f"{PACKAGE}/nested/__init__.py",
                    f"{PACKAGE}/planner.py",
                    "tests/__init__.py",
                    "tests/planner.py",
                    "codecov.yml",
                    "codecov",
                ]
            ],
        }
        (self.reports / "coverage.xml").write_text(f"""<coverage lines-valid="3" lines-covered="2">
          <sources><source>/work/{PACKAGE}</source></sources><packages><package><classes>
            <class filename="__init__.py"><lines><line number="1" hits="1"/></lines></class>
            <class filename="nested/__init__.py"><lines><line number="2" hits="0"/></lines></class>
            <class filename="planner.py"><lines><line number="4" hits="1" branch="true"
                condition-coverage="50% (1/2)" missing-branches="5"/></lines></class>
          </classes></package></packages></coverage>""")
        (self.reports / "junit.xml").write_text(
            '<testsuites><testsuite><testcase name="test"/></testsuite></testsuites>'
        )

    def prepare(self) -> None:
        reporting.prepare(self.tree, SHA, self.reports, self.inventory, self.manifest)

    def test_real_package_prefix_and_same_basename_preserve_hits_misses_and_branches(self) -> None:
        before = ET.parse(self.reports / "coverage.xml").getroot()
        self.prepare()
        after = ET.parse(self.reports / "coverage.xml").getroot()
        classes = after.findall("./packages/package/classes/class")
        self.assertEqual(
            [f"{PACKAGE}/{name}" for name in ["__init__.py", "nested/__init__.py", "planner.py"]],
            [cls.attrib["filename"] for cls in classes],
        )
        self.assertEqual([line.attrib for line in before.iter("line")], [line.attrib for line in after.iter("line")])
        self.assertEqual(before.attrib, after.attrib)
        self.assertEqual(".", after.findtext("./sources/source"))
        self.assertEqual(
            {entry["path"] for entry in self.tree["tree"]},
            {path.relative_to(self.inventory).as_posix() for path in self.inventory.rglob("*") if path.is_file()},
        )
        self.assertTrue(all(path.stat().st_size == 0 for path in self.inventory.rglob("*") if path.is_file()))
        self.assertFalse((self.inventory / ".git").exists())
        self.assertEqual(3, json.loads(self.manifest.read_text())["lines"])
        # The failed root_dir was reports-only: not one covered filename exists there.
        self.assertTrue(all(not (self.reports / cls.attrib["filename"]).exists() for cls in classes))
        self.assertTrue(all((self.inventory / cls.attrib["filename"]).is_file() for cls in classes))

    def test_wrapper_downloads_cannot_contaminate_or_collide_with_source_inventory(self) -> None:
        self.prepare()
        work = self.root / "codecov-work"
        for name in ("codecov", "codecov.SHA256SUM", "codecov.SHA256SUM.sig"):
            (work / name).write_text("wrapper download")
        expected = {entry["path"] for entry in self.tree["tree"]}
        actual = {path.relative_to(self.inventory).as_posix() for path in self.inventory.rglob("*") if path.is_file()}
        self.assertEqual(expected, actual)
        self.assertEqual(0, (self.inventory / "codecov").stat().st_size)
        self.assertGreater((work / "codecov").stat().st_size, 0)
        self.assertFalse((work / ".git").exists())

    def test_repository_relative_report_is_also_unambiguous(self) -> None:
        root = ET.parse(self.reports / "coverage.xml").getroot()
        root.find("./sources/source").text = "/work"
        for cls in root.iter("class"):
            cls.set("filename", f"{PACKAGE}/{cls.attrib['filename']}")
        ET.ElementTree(root).write(self.reports / "coverage.xml")
        self.prepare()

    def test_bad_tree_or_source_mapping_fails_before_inventory_creation(self) -> None:
        for mutate in [
            lambda tree: tree.update(commit="b" * 40),
            lambda tree: tree.update(truncated=True),
            lambda tree: tree["tree"].pop(0),
            lambda tree: tree["tree"][0].update(mode="120000"),
            lambda tree: tree["tree"][0].update(path="../escape"),
            lambda tree: tree["tree"].append(dict(tree["tree"][0])),
            lambda tree: tree.update(tree=[]),
        ]:
            with self.subTest(mutate=mutate):
                original = copy.deepcopy(self.tree)
                mutate(self.tree)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.inventory.exists())
                self.tree = original

    def test_empty_invalid_and_ambiguous_reports_fail(self) -> None:
        path = self.reports / "coverage.xml"
        original = path.read_text()
        for content in [
            "<coverage><sources><source>.</source></sources></coverage>",
            original.replace("/work/" + PACKAGE, "/other/source"),
            original.replace('filename="__init__.py"', 'filename="../__init__.py"'),
            original.replace('number="1"', 'number="0"'),
            original.replace('hits="1"', 'hits="-1"'),
            original.replace('filename="planner.py"', 'filename="__init__.py"'),
            '<!DOCTYPE coverage [<!ENTITY e "bad">]>' + original,
            original.replace("<sources>", "<sources><source>.</source>").replace(
                'filename="planner.py"', f'filename="{PACKAGE}/planner.py"'
            ),
            "<testsuites/>",
        ]:
            with self.subTest(content=content):
                path.write_text(content)
                # Additional full-path source below deliberately has two exact matches.
                if "<source>.</source>" in content and "planner.py" in content:
                    self.tree["tree"].append(
                        {"path": f"{PACKAGE}/{PACKAGE}/planner.py", "type": "blob", "mode": "100644"}
                    )
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.inventory.exists())
        path.write_text(original)
        (self.reports / "junit.xml").write_text("<testsuites/>")
        with self.assertRaisesRegex(ValueError, "Empty test results"):
            self.prepare()

    def test_normalized_reports_still_obey_the_existing_file_budget(self) -> None:
        with patch.object(reporting, "MAX_RESPONSE", 100), self.assertRaisesRegex(ValueError, "report budget"):
            self.prepare()

    def test_paths_reject_control_and_noncanonical_names(self) -> None:
        for name in ["", "/absolute", "a/../b", "a//b", "./a", ".git/config", "a\\b", "a\nb", "a\x7fb"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                reporting.relative_path(name)


class DeliveryTests(unittest.TestCase):
    # Upload metadata follows the observed anonymous public v2 API: JUnit name,
    # both build_code fields and JUnit/error upload totals can be null. IDs are sanitized.
    # Successful commit/file shape is specified by Codecov's v2 serializers:
    # https://github.com/codecov/codecov-api/blob/852702582ff49975de15eecb2bb63a8bb261ddfd/api/shared/commit/serializers.py
    def setUp(self) -> None:
        self.expected = {"commit": SHA, "files": [f"{PACKAGE}/planner.py"], "lines": 2}
        self.commit = {
            "commitid": SHA,
            "state": "complete",
            "totals": {"files": 1, "lines": 2},
            "report": {"files": [{"name": self.expected["files"][0], "totals": {"files": 0, "lines": 2}}]},
        }
        self.uploads = [
            {
                "name": "coverage-123-2",
                "build_code": None,
                "state": "merged",
                "state_id": 6,
                "state_name": "MERGED",
                "totals": {"files": 1, "lines": 2},
            },
            {
                "name": None,
                "job_code": "junit-123-2",
                "build_code": None,
                "state": "processed",
                "state_name": "UPLOADED",
                "totals": None,
                "build_url": None,
                "storage_path": "test_results/v1/raw/report",
            },
        ]

    def ready(self) -> bool:
        return reporting.delivery_ready(self.commit, self.uploads, self.expected, "123", "2")

    def test_processed_junit_uploaded_label_and_nonempty_project_pass(self) -> None:
        self.assertTrue(self.ready())

    def test_observed_merged_coverage_and_nullable_junit_schema(self) -> None:
        # Read-only API evidence from the last successful Groq report. The file
        # entry is an excerpt; only the coverage name is adapted below to model
        # this reporter's attribution after the inventory fix reaches main.
        commit_sha = "b6e763819d14640ebf53defb47492d0863b48383"
        file = {
            "name": "custom_components/groq/types.py",
            "totals": {"files": 0, "lines": 4, "hits": 4, "misses": 0, "coverage": 100.0},
            "line_coverage": [[3, 0], [5, 0], [7, 0], [12, 0]],
        }
        commit = {
            "commitid": commit_sha,
            "state": "complete",
            "totals": {"files": 32, "lines": 4697, "hits": 4697, "sessions": 1},
            "report": {"files": [file]},
        }
        coverage = {
            "name": "CI - checks",
            "job_code": "CI",
            "build_code": None,
            "build_url": "https://github.com/barneyonline/ha-groq/actions/runs/41177600806",
            "provider": "github-actions",
            "state": "merged",
            "state_id": 6,
            "state_name": "MERGED",
            "totals": {"files": 32, "lines": 4697, "hits": 4697, "misses": 0, "coverage": 100.0},
        }
        expected = {"commit": commit_sha, "files": [file["name"]], "lines": 4}
        uploads = [coverage, self.uploads[1]]
        self.assertFalse(reporting.delivery_ready(commit, uploads, expected, "123", "2"))
        coverage["name"] = "coverage-123-2"
        self.assertTrue(reporting.delivery_ready(commit, uploads, expected, "123", "2"))
        coverage.update(state="processed", state_id=1, state_name="UPLOADED", totals=None)
        # An old complete project report cannot turn this unmerged attempt green.
        self.assertFalse(reporting.delivery_ready(commit, uploads, expected, "123", "2"))

    def test_observed_unusable_coverage_response_cannot_pass(self) -> None:
        self.uploads[0].update(
            job_code="Coverage Reporting", build_code=None, state="error", state_id=3, state_name="ERROR", totals=None
        )
        self.commit.update(state="error", totals={"files": 0, "lines": 0, "sessions": 0}, report={"files": []})
        with self.assertRaisesRegex(ValueError, "Codecov rejected coverage-123-2"):
            self.ready()

    def test_transport_only_wrong_attempt_and_missing_reports_never_pass(self) -> None:
        for mutate in [
            lambda: self.uploads[0].update(state="uploaded"),
            lambda: self.uploads[0].update(state="processed"),
            lambda: self.uploads[1].update(state="pending"),
            lambda: self.uploads[0].update(name="coverage-123-1"),
            lambda: self.uploads[1].update(job_code="junit-123-1"),
            lambda: self.commit.update(state="pending"),
            lambda: self.uploads.clear(),
        ]:
            self.setUp()
            mutate()
            with self.subTest(mutate=mutate):
                self.assertFalse(self.ready())

    def test_unusable_empty_wrong_source_and_failed_junit_are_errors(self) -> None:
        for mutate in [
            lambda: self.uploads[0].update(state="error", state_name="ERROR"),
            lambda: self.uploads[1].update(state="error"),
            lambda: self.uploads[0].update(totals={"files": 0, "lines": 0}),
            lambda: self.uploads[0].update(totals=None),
            lambda: self.commit.update(state="error"),
            lambda: self.commit.update(commitid="b" * 40),
            lambda: self.commit.update(totals={"files": 0, "lines": 0}),
            lambda: self.commit.update(report={"files": []}),
            lambda: self.commit["report"]["files"][0].update(name="tests/planner.py"),
            lambda: self.uploads[1].update(storage_path="v4/raw/coverage"),
            lambda: self.uploads.append(dict(self.uploads[0])),
        ]:
            self.setUp()
            mutate()
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.ready()

    def test_upload_pagination_selects_exact_attempt(self) -> None:
        url = "https://api.codecov.io/uploads/?format=json"
        pages = {
            url: {"results": [{"name": "old-attempt"}], "next": "?page=2"},
            "https://api.codecov.io/uploads/?page=2": {"results": self.uploads, "next": None},
        }
        all_uploads = reporting.uploads_for(url, pages.__getitem__)
        self.assertTrue(reporting.delivery_ready(self.commit, all_uploads, self.expected, "123", "2"))
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            reporting.uploads_for(url, lambda _: {"results": [], "next": url})
        with self.assertRaisesRegex(ValueError, "pagination path"):
            reporting.uploads_for(url, lambda _: {"results": [], "next": "/other/"})

    def test_polling_is_bounded_and_only_accepts_complete_processing(self) -> None:
        sleeps = []
        calls = []

        def fetch(url: str) -> dict:
            calls.append(url)
            if "uploads/" in url:
                return {"results": self.uploads}
            return self.commit

        reporting.verify(self.expected, "barneyonline/ha-energy-planner", "123", "2", fetch=fetch, sleep=sleeps.append)
        self.assertFalse(sleeps)
        self.uploads[0]["state"] = "uploaded"
        with self.assertRaisesRegex(ValueError, "Timed out"):
            reporting.verify(
                self.expected, "barneyonline/ha-energy-planner", "123", "2", fetch=fetch, sleep=sleeps.append, polls=3
            )
        self.assertEqual([10, 10], sleeps)
        self.assertTrue(all(url.startswith("https://api.codecov.io/api/v2/github/") for url in calls))

    def test_not_ready_and_transient_api_errors_are_bounded(self) -> None:
        for error in [HTTPError("url", 404, "not ready", {}, None), URLError("temporary")]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, "Timed out"):
                reporting.verify(
                    self.expected,
                    "barneyonline/ha-energy-planner",
                    "123",
                    "2",
                    fetch=lambda _, error=error: (_ for _ in ()).throw(error),
                    sleep=lambda _: None,
                    polls=2,
                )
        with self.assertRaises(HTTPError):
            reporting.verify(
                self.expected,
                "barneyonline/ha-energy-planner",
                "123",
                "2",
                fetch=lambda _: (_ for _ in ()).throw(HTTPError("url", 403, "denied", {}, None)),
            )

    def test_verification_has_a_total_deadline(self) -> None:
        with patch.object(reporting.time, "monotonic", side_effect=[0, 241]):
            with self.assertRaisesRegex(ValueError, "deadline"):
                reporting.verify(self.expected, "barneyonline/ha-energy-planner", "123", "2")

    def test_public_api_fetch_has_no_authorization_and_rejects_other_hosts(self) -> None:
        with self.assertRaises(ValueError):
            reporting.fetch_json("https://example.com/")
        with patch.object(reporting, "urlopen") as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = b'{"state":"complete"}'
            self.assertEqual({"state": "complete"}, reporting.fetch_json("https://api.codecov.io/api/v2/"))
            fetch.assert_called_once_with("https://api.codecov.io/api/v2/", timeout=10)


class WorkflowTests(unittest.TestCase):
    def test_workflow_runs_only_trusted_helper_with_inert_source_and_existing_isolation(self) -> None:
        workflow = yaml.load((ROOT / ".github/workflows/coverage-reporting.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual({"workflow_run"}, set(workflow["on"]))
        job = workflow["jobs"]["upload"]
        self.assertEqual(
            {"contents": "read", "actions": "read", "pull-requests": "read", "id-token": "write"}, job["permissions"]
        )
        steps = job["steps"]
        source = next(
            step for step in steps if step["name"] == "Read verified producer tree and trusted reporting helper"
        )
        self.assertEqual("${{ github.workflow_sha }}", source["env"]["REPORTER_SHA"])
        self.assertIn("commit_sha: process.env.SOURCE_SHA", source["with"]["script"])
        self.assertNotIn("actions/checkout", json.dumps(workflow))
        for step in steps:
            if step.get("uses", "").startswith("codecov/"):
                settings = step["with"]
                self.assertEqual("${{ runner.temp }}/codecov-source", settings["root_dir"])
                self.assertEqual("${{ runner.temp }}/codecov-work", settings["working-directory"])
                self.assertNotEqual(settings["root_dir"], settings["working-directory"])
                self.assertEqual("true", settings["use_oidc"])
                self.assertEqual("noop", settings["plugins"])
                self.assertEqual("true", settings["disable_file_fixes"])
                self.assertEqual("true", settings["disable_search"])
        self.assertIn("always()", steps[-1]["if"])
        self.assertEqual('python3 -I -B "$RUNNER_TEMP/codecov-reporting.py"', steps[-1]["run"])

    def test_actual_artifact_validation_rejects_extra_files_and_symlinks(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/coverage-reporting.yml").read_text())
        script = next(
            step["run"] for step in workflow["jobs"]["upload"]["steps"] if step["name"] == "Validate report files"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("coverage.xml", "junit.xml"):
                (root / name).write_text("report")

            def validate() -> int:
                return subprocess.run(
                    ["bash", "-e", "-c", script],
                    env={**os.environ, "REPORTS": temporary},
                    capture_output=True,
                    check=False,
                ).returncode

            self.assertEqual(0, validate())
            (root / "extra.py").write_text("raise RuntimeError('must never run')")
            self.assertNotEqual(0, validate())
            (root / "extra.py").unlink()
            (root / "coverage.json").symlink_to(root / "coverage.xml")
            self.assertNotEqual(0, validate())


if __name__ == "__main__":
    unittest.main()
