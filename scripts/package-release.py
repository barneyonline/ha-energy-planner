#!/usr/bin/env python3
"""Build a deterministic component archive and checksum before publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def package_release(root: Path, output: Path, expected_version: str | None = None) -> Path:
    component = root / "custom_components/ha_energy_planner"
    version = json.loads((component / "manifest.json").read_text())["version"]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if version != project or (expected_version is not None and expected_version.removeprefix("v") != version):
        raise ValueError("Release tag, manifest and project versions must match")
    for required in ("__init__.py", "config_flow.py", "strings.json", "icons.json", "translations/en.json"):
        if not (component / required).is_file():
            raise ValueError(f"Missing component resource: {required}")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"ha-energy-planner-v{version}.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
        for path in sorted(component.rglob("*")):
            if not path.is_file() or any(
                part.startswith(".") or part == "__pycache__" for part in path.relative_to(component).parts
            ):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            name = path.relative_to(root).as_posix()
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            zipped.writestr(info, path.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--version")
    args = parser.parse_args()
    print(package_release(Path(__file__).resolve().parents[1], args.output_dir, args.version))
