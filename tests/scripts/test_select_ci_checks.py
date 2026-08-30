from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "select_ci_checks.py"
    spec = importlib.util.spec_from_file_location("select_ci_checks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_component_changes_run_every_repository_check() -> None:
    module = _module()

    assert module.select_checks(["custom_components/ha_energy_planner/planner.py"]) == module.CheckSelection(
        pytest=True,
        quality_scale=True,
        validation_extras=True,
        dedicated_quality_scale=True,
    )


def test_docs_changes_only_run_quality_scale() -> None:
    module = _module()

    assert module.select_checks(["docs/requirements-audit.md"]) == module.CheckSelection(
        pytest=False,
        quality_scale=True,
        validation_extras=False,
        dedicated_quality_scale=True,
    )


def test_smoke_script_changes_only_run_validation_extras() -> None:
    module = _module()

    assert module.select_checks(["scripts/docker-ha-smoke.sh"]) == module.CheckSelection(
        pytest=False,
        quality_scale=False,
        validation_extras=True,
        dedicated_quality_scale=False,
    )


def test_mypy_runner_changes_run_dedicated_quality_checks() -> None:
    module = _module()

    assert module.select_checks(["scripts/docker-mypy.sh"]) == module.CheckSelection(
        pytest=False,
        quality_scale=True,
        validation_extras=True,
        dedicated_quality_scale=True,
    )


def test_test_changes_run_pytest_and_validation() -> None:
    module = _module()

    assert module.select_checks(["tests/test_planner.py"]) == module.CheckSelection(
        pytest=True,
        quality_scale=False,
        validation_extras=True,
        dedicated_quality_scale=False,
    )


def test_renamed_integration_file_keeps_source_and_destination_impact() -> None:
    module = _module()

    assert module.select_checks(
        ["custom_components/ha_energy_planner/old.py", "docs/old.md"]
    ) == module.CheckSelection(
        pytest=True,
        quality_scale=True,
        validation_extras=True,
        dedicated_quality_scale=True,
    )


def test_dedicated_workflow_changes_do_not_expand_tests_workflow() -> None:
    module = _module()

    assert module.select_checks([".github/workflows/codespell.yml"]) == module.CheckSelection(
        pytest=False,
        quality_scale=False,
        validation_extras=False,
        dedicated_quality_scale=False,
    )


def test_tests_workflow_and_unclassified_paths_fail_safe() -> None:
    module = _module()
    dedicated_all_checks = module.CheckSelection(True, True, True, True)
    fallback_all_checks = module.CheckSelection(True, True, True, False)

    assert module.select_checks([".github/workflows/tests.yml"]) == dedicated_all_checks
    assert module.select_checks(["pyproject.toml"]) == dedicated_all_checks
    assert module.select_checks([".github/workflows/ci.yml"]) == fallback_all_checks
    assert module.select_checks(["new-trigger-path.cfg"]) == fallback_all_checks
    assert module.select_checks([], force_all=True) == fallback_all_checks
