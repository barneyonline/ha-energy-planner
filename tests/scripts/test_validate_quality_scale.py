from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "scripts" / "validate_quality_scale.py"
    spec = importlib.util.spec_from_file_location("validate_quality_scale", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate_quality_scale = _load_module()


def _write_manifest(root: Path, quality_scale: str | None = "bronze") -> None:
    manifest_dir = root / "custom_components" / "ha_energy_planner"
    manifest_dir.mkdir(parents=True)
    manifest = {"domain": "ha_energy_planner"}
    if quality_scale is not None:
        manifest["quality_scale"] = quality_scale
    (manifest_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _write_reference(root: Path, path: str) -> None:
    reference = root / path
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("ok", encoding="utf-8")


def _write_quality_scale(root: Path, body: str) -> None:
    (root / "quality_scale.yaml").write_text(body, encoding="utf-8")


def test_repository_quality_scale_matches_manifest_claim() -> None:
    root = Path(__file__).resolve().parents[2]

    exit_code, messages = validate_quality_scale.validate_quality_scale(root)

    assert exit_code == 0, "\n".join(messages)


def test_gold_claim_requires_rules_from_every_claimed_level(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "gold")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
  silver:
    required: [test-coverage]
  gold:
    required: [diagnostics]
rules:
  config-flow:
    status: done
    references:
      docs: [README.md]
  test-coverage:
    status: done
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "diagnostics" in "\n".join(messages)


def test_unclaimed_manifest_is_not_enough_for_this_project(tmp_path: Path) -> None:
    _write_manifest(tmp_path, None)
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: todo
    comment: Tracked gap before a manifest quality claim is made.
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Manifest must claim quality_scale 'gold'" in "\n".join(messages)


def test_generated_integration_cache_directories_are_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "platinum")
    _write_reference(tmp_path, "README.md")
    (tmp_path / "custom_components" / "ha_energy_planner" / "__pycache__").mkdir()
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: []
  silver:
    required: []
  gold:
    required: []
  platinum:
    required: []
rules: {}
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Generated cache directories" in "\n".join(messages)


def test_na_rules_need_explanatory_comments(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [discovery-update-info]
rules:
  discovery-update-info:
    status: n/a
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "n/a rules missing explanatory comments" in "\n".join(messages)


def test_documented_integration_exceptions_may_be_na(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "gold")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: []
  silver:
    required: []
  gold:
    required: [entity-device-class]
  platinum:
    required: []
rules:
  entity-device-class:
    status: n/a
    comment: Device classes do not apply to this entity platform.
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 0, "\n".join(messages)


def test_strict_typing_done_requires_an_enforced_strict_checker(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "gold")
    _write_reference(tmp_path, "README.md")
    _write_reference(tmp_path, "pyproject.toml")
    _write_reference(tmp_path, "scripts/docker-validate.sh")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: []
  silver:
    required: []
  gold:
    required: []
  platinum:
    required: [strict-typing]
rules:
  strict-typing:
    status: done
    references:
      code: [pyproject.toml, scripts/docker-validate.sh]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "strict-typing cannot be marked done" in "\n".join(messages)


def test_strict_typing_done_accepts_an_enforced_strict_checker(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "gold")
    _write_reference(tmp_path, "README.md")
    _write_reference(tmp_path, "scripts/docker-validate.sh")
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
    (tmp_path / "scripts" / "docker-validate.sh").write_text("mypy custom_components\n", encoding="utf-8")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: []
  silver:
    required: []
  gold:
    required: []
  platinum:
    required: [strict-typing]
rules:
  strict-typing:
    status: done
    references:
      code: [pyproject.toml, scripts/docker-validate.sh]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 0, "\n".join(messages)


def test_na_status_is_restricted_to_allowlisted_rules(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: n/a
    comment: Not acceptable for this rule.
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Rules marked n/a without an allowlist exception" in "\n".join(messages)


def test_unknown_status_is_reported(tmp_path: Path) -> None:
    _write_manifest(tmp_path, None)
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: partial
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "unsupported status" in "\n".join(messages)


def test_broken_references_are_reported(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: done
    references:
      docs: [MISSING.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "MISSING.md" in "\n".join(messages)
