"""Coverage remains attributable after leaving the test container."""

import os
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_coverage_report_survives_a_different_checkout_path(tmp_path: Path) -> None:
    """Use real coverage.py data and the production config across two roots."""
    producer = tmp_path / "producer"
    source = producer / "custom_components/ha_energy_planner"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    probe = source / "probe.py"
    probe.write_text("covered = True\n")
    config = str(ROOT / "pyproject.toml")
    env = {**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")}
    subprocess.run(
        [sys.executable, "-m", "coverage", "run", f"--rcfile={config}", str(probe)],
        cwd=producer,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    consumer = tmp_path / "reporting-checkout"
    producer.rename(consumer)
    subprocess.run(
        [sys.executable, "-m", "coverage", "xml", f"--rcfile={config}", "--fail-under=0"],
        cwd=consumer,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    report = ET.parse(consumer / "coverage.xml").getroot()
    assert report.get("line-rate") == "1"
    sources = [Path(node.text or "") for node in report.findall("./sources/source")]
    assert sources and all(not path.is_absolute() for path in sources)
    filenames = [node.attrib["filename"] for node in report.findall(".//class")]
    assert "probe.py" in filenames
    for filename in filenames:
        assert any((consumer / root / filename).is_file() for root in sources)


def test_codecov_file_network_uses_the_report_producers_checkout() -> None:
    """Both uploads need source metadata while reports stay outside that tree."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/coverage-reporting.yml").read_text())
    steps = workflow["jobs"]["upload"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ steps.source.outputs.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["if"] == "${{ steps.source.outputs.artifact_id != '' }}"
    uploads = [step for step in steps if step.get("uses", "").startswith("codecov/codecov-action@")]
    assert len(uploads) == 2
    for upload in uploads:
        assert steps.index(checkout) < steps.index(upload)
        settings = upload["with"]
        assert settings["root_dir"] == "${{ github.workspace }}"
        assert settings["files"].startswith("${{ runner.temp }}/reports/")
        assert settings["override_commit"] == checkout["with"]["ref"]
        assert settings["use_oidc"] is True
        assert settings["plugins"] == "noop"
        assert settings["disable_search"] is True
        assert settings["disable_file_fixes"] is True


def test_report_validation_cannot_import_producer_modules(tmp_path: Path) -> None:
    """A producer's pathlib.py must never run in the credentialed reporter."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/coverage-reporting.yml").read_text())
    step = next(step for step in workflow["jobs"]["upload"]["steps"] if step.get("name") == "Validate report files")
    reports = tmp_path / "reports"
    reports.mkdir()
    for name in ("coverage.xml", "junit.xml"):
        (reports / name).write_text("<report/>\n")
    producer = tmp_path / "producer"
    producer.mkdir()
    marker = producer / "producer-module-ran"
    (producer / "pathlib.py").write_text(
        "with open('producer-module-ran', 'w') as marker:\n"
        "    marker.write('untrusted import')\n"
        "raise RuntimeError('Imported producer module')\n"
    )
    # HA's editable-install startup hook preloads pathlib. Exercise the clean
    # interpreter imports used by the reporting runner instead of that cache.
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python3"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S "$@"\n')
    python.chmod(0o755)
    env = {**os.environ, "REPORTS": str(reports), "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=producer,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not marker.exists(), "The credentialed validator executed a producer-controlled Python module"
    assert result.returncode == 0, result.stderr
