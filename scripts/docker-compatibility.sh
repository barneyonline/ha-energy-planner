#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# The minimum comes from the public support contract. Keep a pinned baseline
# alongside the separate stable compatibility signal in CI.
minimum_version="$(python3 -c 'import json; print(json.load(open("hacs.json"))["homeassistant"])')"
versions=("$minimum_version" "2026.8.2")
if (( $# > 0 )); then
  versions=("$@")
fi

for version in "${versions[@]}"; do
  printf '\nHome Assistant compatibility: %s\n' "$version"
  docker run --rm \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD:/work:ro" -w /work \
    "ghcr.io/home-assistant/home-assistant:$version" \
    python3 -m pytest -q -p no:cacheprovider \
      tests/test_ha_runtime.py tests/test_device_registry_runtime.py \
      tests/test_config_flow.py tests/test_lifecycle.py \
      tests/test_services.py tests/test_calendar.py tests/test_manifest.py
done
