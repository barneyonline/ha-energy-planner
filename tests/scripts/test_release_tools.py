"""Artifact and observation checks reject incomplete stable release evidence."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_package_is_repeatable_complete_and_version_checked(tmp_path):
    package = module("package-release").package_release
    archive = package(ROOT, tmp_path)
    original = archive.read_bytes()
    assert package(ROOT, tmp_path).read_bytes() == original
    assert archive.with_suffix(".zip.sha256").read_text().endswith(f"  {archive.name}\n")
    with ZipFile(archive) as zipped:
        assert "custom_components/ha_energy_planner/icons.json" in zipped.namelist()
        assert "custom_components/ha_energy_planner/durable_storage.py" in zipped.namelist()
        assert not any("__pycache__" in name for name in zipped.namelist())
        assert not zipped.testzip()
    with pytest.raises(ValueError, match="versions must match"):
        package(ROOT, tmp_path, "999.0.0")


def test_support_matrix_uses_the_public_minimum_and_pinned_baseline():
    policy = module("support_policy").support_policy(ROOT)
    minimum = json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
    assert policy["matrix"][0] == minimum
    assert policy["baseline"] in policy["matrix"]
    assert policy["matrix"][-1] == "stable"


def test_operating_record_requires_complete_real_duration_and_matching_revision():
    validator = module("validate-release-evidence")
    commit = "a" * 40
    record = {
        "commit": commit, "version": "1.0.0",
        "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-03T00:00:00+00:00",
        "scenarios": {name: {"result": "passed", "evidence": "redacted trace reference"}
                      for name in validator.SCENARIOS},
    }
    validator.validate_evidence(record, commit, "v1.0.0")
    for updates in ({"commit": "b" * 40}, {"version": "0.9.18"},
                    {"ended_at": "2026-01-02T00:00:00+00:00"}, {"scenarios": {}},
                    {"started_at": "2026-01-01T00:00:00"}, {"ended_at": "2099-01-03T00:00:00+00:00"}):
        with pytest.raises(ValueError):
            validator.validate_evidence({**record, **updates}, commit, "1.0.0")
    assert validator.requires_observation("v1.0.0")
    assert not validator.requires_observation("0.9.19")
    assert not validator.requires_observation("1.0.0rc1")


@pytest.mark.parametrize("invalid_fields", [
    {}, {"evidence": None}, {"evidence": False}, {"evidence": True},
    {"evidence": 0}, {"evidence": 1}, {"evidence": []}, {"evidence": {}},
    {"evidence": ["trace reference"]}, {"evidence": {"trace": "reference"}},
    {"evidence": ""}, {"evidence": " \t\n"},
])
def test_operating_record_rejects_missing_blank_and_non_string_evidence(invalid_fields):
    validator = module("validate-release-evidence")
    commit = "a" * 40
    for scenario in validator.SCENARIOS:
        record = {
            "commit": commit, "version": "1.0.0",
            "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-03T00:00:00+00:00",
            "scenarios": {name: {"result": "passed", "evidence": "redacted trace reference"}
                          for name in validator.SCENARIOS},
        }
        record["scenarios"][scenario] = {"result": "passed", **invalid_fields}
        with pytest.raises(ValueError, match=f"Completed evidence is required for {scenario}"):
            validator.validate_evidence(record, commit, "1.0.0")
