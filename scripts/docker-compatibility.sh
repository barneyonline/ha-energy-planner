#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# The minimum comes from the public support contract. Keep a pinned baseline
# alongside the separate stable compatibility signal in CI.
versions=()
while IFS= read -r version; do
  versions+=("$version")
done < <(python3 scripts/support_policy.py versions | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)))')
if (( $# > 0 )); then
  versions=("$@")
fi

# Run the complete runtime contract on every supported compatibility image.
test_files=(
  tests/test_ha_runtime.py
  tests/test_control_runtime.py
  tests/test_device_registry_runtime.py
  tests/test_storage_runtime.py
  tests/test_upgrade_runtime.py
  tests/test_repairs.py
  tests/test_config_flow.py
  tests/test_lifecycle.py
  tests/test_services.py
  tests/test_calendar.py
  tests/test_manifest.py
)

run_runtime_tests() {
  docker run --rm \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD:/work:ro" -w /work \
    "ghcr.io/home-assistant/home-assistant:$version" \
    python3 -X faulthandler -m pytest -q -p no:cacheprovider "$@"
}

for version in "${versions[@]}"; do
  printf '\nHome Assistant compatibility: %s\n' "$version"
  run_runtime_tests "${test_files[@]}"
done
