from __future__ import annotations

import json
from pathlib import Path


def _manifest() -> dict[str, object]:
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "ha_energy_planner"
        / "manifest.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_claims_platinum_quality_scale() -> None:
    manifest = _manifest()

    assert manifest["domain"] == "ha_energy_planner"
    assert manifest["name"] == "Energy Planner"
    assert manifest["quality_scale"] == "platinum"


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
