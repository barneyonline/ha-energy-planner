from __future__ import annotations

import json
import tomllib
from pathlib import Path


def _manifest() -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / "custom_components" / "ha_energy_planner" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _hacs_manifest() -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / "hacs.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_classifies_multi_entry_calculated_service() -> None:
    manifest = _manifest()

    assert manifest["domain"] == "ha_energy_planner"
    assert manifest["name"] == "Energy Planner"
    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "calculated"
    assert manifest["single_config_entry"] is False
    assert manifest["quality_scale"] == "gold"


def test_release_metadata_versions_match() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert _manifest()["version"] == pyproject["project"]["version"] == "0.8.4"


def test_manifest_keeps_dependency_surface_explicit() -> None:
    manifest = _manifest()

    assert manifest["requirements"] == []
    assert manifest["dependencies"] == []
    assert manifest["config_flow"] is True


def test_manifest_has_real_owner_and_support_urls() -> None:
    manifest = _manifest()

    assert manifest["codeowners"] == ["@barneyonline"]
    assert manifest["documentation"] == "https://github.com/barneyonline/ha-energy-planner"
    assert manifest["issue_tracker"] == "https://github.com/barneyonline/ha-energy-planner/issues"


def test_hacs_metadata_sets_minimum_home_assistant_version() -> None:
    hacs_manifest = _hacs_manifest()

    assert hacs_manifest["name"] == "Energy Planner"
    assert hacs_manifest["homeassistant"] == "2026.6.0"
