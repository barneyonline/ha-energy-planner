"""Development startup must be isolated, reproducible and test-free."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_docker(tmp_path: Path) -> dict[str, str]:
    """Model image/container state without a daemon, network or credentials."""
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ['DEV_STATE'])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
args = sys.argv[1:]
with Path(os.environ['DEV_CALLS']).open('a') as log:
    log.write(json.dumps(args) + '\\n')
def labels():
    return dict(args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '--label')
if args[0] == 'build':
    state['image'] = labels()['io.ha-energy-planner.dev-source']
elif args[:2] == ['image', 'inspect']:
    if 'image' not in state: sys.exit(1)
    print(state['image'])
elif args[:2] == ['container', 'inspect']:
    if 'container' not in state: sys.exit(1)
    fmt = args[args.index('--format') + 1] if '--format' in args else ''
    if 'workspace' in fmt: print(state['container']['owner'])
    elif 'dev-source' in fmt: print(state['container']['source'])
    elif 'Running' in fmt: print(str(state['container']['running']).lower())
elif args[0] == 'rm':
    state.pop('container', None)
elif args[0] == 'run':
    values = labels()
    state['container'] = {'owner': values['io.ha-energy-planner.workspace'],
                          'source': values['io.ha-energy-planner.dev-source'], 'running': True}
elif args[0] == 'stop':
    state['container']['running'] = False
elif args[0] == 'exec' and 'pytest' in args:
    sys.exit(int(os.environ.get('DEV_TEST_EXIT', '0')))
elif args[0] == 'exec' and args[-1] == 'scripts/verify-dev-environment.py':
    sys.exit(int(os.environ.get('DEV_VERIFY_EXIT', '0')))
state_path.write_text(json.dumps(state))
''')
    docker.chmod(0o755)
    ca = tmp_path / "ca.pem"
    ca.write_text("test CA path; never used for a real connection")
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DEV_STATE": str(tmp_path / "state.json"),
        "DEV_CALLS": str(tmp_path / "calls.jsonl"),
        "BUILDX_CONFIG": str(tmp_path / "buildx"),
        "HEP_CA_BUNDLE": str(ca),
        "HEP_DEV_CONTAINER": "test-ha-dev",
        "HEP_DEV_CACHE_DIR": str(tmp_path / "cache"),
    }


def run(env: dict[str, str], *args: str, fast: bool = False) -> subprocess.CompletedProcess[str]:
    script = "docker-pytest-fast.sh" if fast else "ha-dev.sh"
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / script), *args], env=env,
        capture_output=True, text=True, check=False,
    )


def calls(env: dict[str, str]) -> list[list[str]]:
    return [json.loads(line) for line in Path(env["DEV_CALLS"]).read_text().splitlines()]


def test_startup_checks_dependencies_without_tests_or_manual_runtime(fake_docker: dict[str, str]) -> None:
    assert run(fake_docker, "build").returncode == 0
    assert run(fake_docker, "start").returncode == 0
    recorded = calls(fake_docker)
    assert sum(call[0] == "run" for call in recorded) == 1
    assert any(call[-4:] == ["python3", "-m", "pip", "check"] for call in recorded)
    assert any(call[-1] == "scripts/verify-dev-environment.py" for call in recorded)
    assert not any("pytest" in call or "compose" in call for call in recorded)
    build = next(call for call in recorded if call[0] == "build")
    launch = next(call for call in recorded if call[0] == "run")
    assert f"id=cloud_ca,src={fake_docker['HEP_CA_BUNDLE']}" in build
    assert any(fake_docker["HEP_CA_BUNDLE"] in arg and "readonly" in arg for arg in launch)


def test_new_session_recreates_but_commands_reuse_container(fake_docker: dict[str, str]) -> None:
    for command in ("build", "start", "start"):
        assert run(fake_docker, command).returncode == 0
    before = calls(fake_docker)
    assert sum(call[0] == "run" for call in before) == 2
    assert sum(call[0] == "rm" for call in before) == 1
    node = "tests/test_planner.py::test_example[argument with spaces]"
    result = run({**fake_docker, "DEV_TEST_EXIT": "139"}, node, fast=True)
    assert result.returncode == 139
    added = calls(fake_docker)[len(before):]
    assert not any(call[0] in ("build", "run", "rm") for call in added)
    assert added[-1][-1] == node


def test_stale_image_fails_before_replacing_container(fake_docker: dict[str, str]) -> None:
    for command in ("build", "start"):
        assert run(fake_docker, command).returncode == 0
    path = Path(fake_docker["DEV_STATE"])
    state = json.loads(path.read_text())
    state["image"] = "old-dependencies"
    path.write_text(json.dumps(state))
    before = calls(fake_docker)
    result = run(fake_docker, "start")
    assert result.returncode != 0
    assert "dependencies changed" in result.stderr
    assert not any(call[0] in ("rm", "run") for call in calls(fake_docker)[len(before):])


@pytest.mark.parametrize("command", ["start", "stop", "remove"])
def test_another_checkouts_container_is_never_modified(fake_docker: dict[str, str], command: str) -> None:
    for setup_command in ("build", "start"):
        assert run(fake_docker, setup_command).returncode == 0
    path = Path(fake_docker["DEV_STATE"])
    state = json.loads(path.read_text())
    state["container"]["owner"] = "/another/checkout"
    path.write_text(json.dumps(state))
    before = calls(fake_docker)
    result = run(fake_docker, command)
    assert result.returncode != 0
    assert "another checkout" in result.stderr
    assert not any(call[0] in ("rm", "run", "stop") for call in calls(fake_docker)[len(before):])


def test_removal_preserves_image_for_next_session(fake_docker: dict[str, str]) -> None:
    for command in ("build", "start", "remove"):
        assert run(fake_docker, command).returncode == 0
    state = json.loads(Path(fake_docker["DEV_STATE"]).read_text())
    assert "image" in state
    assert "container" not in state
    assert run(fake_docker, "start").returncode == 0


def test_stopped_container_requires_session_start(fake_docker: dict[str, str]) -> None:
    for command in ("build", "start", "stop"):
        assert run(fake_docker, command).returncode == 0
    result = run(fake_docker, "pytest", "tests/test_planner.py")
    assert result.returncode != 0
    assert "stopped" in result.stderr


def test_failed_dependency_check_removes_unverified_container(fake_docker: dict[str, str]) -> None:
    assert run(fake_docker, "build").returncode == 0
    result = run({**fake_docker, "DEV_VERIFY_EXIT": "1"}, "start")
    assert result.returncode != 0
    assert "verification failed" in result.stderr
    assert "container" not in json.loads(Path(fake_docker["DEV_STATE"]).read_text())


def test_dependency_verification_detects_wrong_ha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("dev_environment", ROOT / "scripts/verify-dev-environment.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = module.expected_versions(ROOT)
    monkeypatch.setattr(module, "version", expected.__getitem__)
    assert module.verify(ROOT)["homeassistant"] == expected["homeassistant"]
    monkeypatch.setattr(module, "version", lambda name: "2026.9.0" if name == "homeassistant" else expected[name])
    with pytest.raises(RuntimeError, match="dependencies changed"):
        module.verify(ROOT)
