#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONDONTWRITEBYTECODE=1
PYCACHE_DIR="$(mktemp -d "$ROOT_DIR/.pycache-validate.XXXXXX")"
CHECK_CONFIG_DIR="$(mktemp -d "$ROOT_DIR/.ha-check-config.XXXXXX")"

cleanup() {
  cleanup_path "$PYCACHE_DIR"
  cleanup_path "$CHECK_CONFIG_DIR"
}
trap cleanup EXIT

cleanup_path() {
  local path="$1"

  [[ -e "$path" ]] || return 0
  chmod -R u+rwX "$path" 2>/dev/null || true
  if rm -rf "$path" 2>/dev/null; then
    return 0
  fi

  docker run --rm \
    -v "$path:/cleanup" \
    --entrypoint /bin/sh \
    ghcr.io/home-assistant/home-assistant:stable \
    -c 'find /cleanup -mindepth 1 -exec rm -rf {} +' >/dev/null 2>&1 || true
  rm -rf "$path" 2>/dev/null || true
}

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

run env PYTHONPYCACHEPREFIX="$PYCACHE_DIR" python3 -m compileall -q custom_components tests scripts
run bash -n scripts/docker-ha-smoke.sh scripts/docker-validate.sh scripts/export-real-live-schema.sh scripts/export-real-history-fixtures.sh scripts/export-real-validation-bundle.sh
run scripts/export-real-live-schema.sh --dry-run
run scripts/export-real-history-fixtures.sh --dry-run
run scripts/export-real-validation-bundle.sh --dry-run
run docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/work" -w /work ghcr.io/home-assistant/home-assistant:stable python3 scripts/validate_quality_scale.py
run docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/work" -w /work ghcr.io/home-assistant/home-assistant:stable sh -c 'python3 -m coverage run -m pytest -q && python3 -m coverage report -m'
run python3 scripts/replay-fixture.py tests/fixtures/replay/*.json
run python3 scripts/validate-live-schema-fixture.py tests/fixtures/live_schema/*.json
run python3 scripts/validate-real-history-fixture.py tests/fixtures/history/*.json

shopt -s nullglob
real_fixtures=(tests/fixtures/live_schema/real_*.json)
shopt -u nullglob
if (( ${#real_fixtures[@]} > 0 )); then
  run python3 scripts/validate-live-schema-fixture.py --profile ha-energy-planner-v1-real "${real_fixtures[@]}"
  run python3 scripts/validate-live-schema-fixture.py --profile ha-energy-planner-haeo-value-v1-real "${real_fixtures[@]}"
else
  printf '\n==> python3 scripts/validate-live-schema-fixture.py --profile ha-energy-planner-v1-real tests/fixtures/live_schema/*.json (expected synthetic-fixture failure)\n'
  set +e
  python3 scripts/validate-live-schema-fixture.py --profile ha-energy-planner-v1-real tests/fixtures/live_schema/*.json
  status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "Expected the real live-schema profile to fail when no real_* fixtures are present." >&2
    exit 1
  fi
  printf '\n==> python3 scripts/validate-live-schema-fixture.py --profile ha-energy-planner-haeo-value-v1-real tests/fixtures/live_schema/*.json (expected synthetic-fixture failure)\n'
  set +e
  python3 scripts/validate-live-schema-fixture.py --profile ha-energy-planner-haeo-value-v1-real tests/fixtures/live_schema/*.json
  status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "Expected the real HAEO value-evidence profile to fail when no real_haeo_response fixture is present." >&2
    exit 1
  fi
fi

shopt -s nullglob
real_history_fixtures=(tests/fixtures/history/real_*.json)
shopt -u nullglob
if (( ${#real_history_fixtures[@]} > 0 )); then
  run python3 scripts/validate-real-history-fixture.py --profile ha-energy-planner-history-v1-real "${real_history_fixtures[@]}"
else
  printf '\n==> python3 scripts/validate-real-history-fixture.py --profile ha-energy-planner-history-v1-real tests/fixtures/history/*.json (expected synthetic-fixture failure)\n'
  set +e
  python3 scripts/validate-real-history-fixture.py --profile ha-energy-planner-history-v1-real tests/fixtures/history/*.json
  status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "Expected the real history profile to fail when no real_* fixtures are present." >&2
    exit 1
  fi
fi

cat > "$CHECK_CONFIG_DIR/configuration.yaml" <<'YAML'
default_config:

logger:
  default: warning
  logs:
    custom_components.ha_energy_planner: debug
YAML
mkdir -p "$CHECK_CONFIG_DIR/custom_components/ha_energy_planner"
run docker run --rm \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$CHECK_CONFIG_DIR:/config" \
  -v "$PWD/custom_components/ha_energy_planner:/config/custom_components/ha_energy_planner:ro" \
  ghcr.io/home-assistant/home-assistant:stable \
  python3 -m homeassistant --config /config --script check_config
if [[ "${HEP_SKIP_HA_SMOKE:-0}" == "1" ]]; then
  printf '\n==> scripts/docker-ha-smoke.sh (skipped: HEP_SKIP_HA_SMOKE=1)\n'
else
  run scripts/docker-ha-smoke.sh
fi

printf '\nHA Energy Planner Docker validation passed\n'
