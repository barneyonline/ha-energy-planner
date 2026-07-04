#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LIVE_OUT_DIR="${HEP_LIVE_SCHEMA_OUT_DIR:-tests/fixtures/live_schema}"
HISTORY_OUT_DIR="${HEP_HISTORY_OUT_DIR:-tests/fixtures/history}"
DRY_RUN=0
VALIDATE_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --validate-only)
      VALIDATE_ONLY=1
      shift
      ;;
    --live-out-dir)
      LIVE_OUT_DIR="$2"
      shift 2
      ;;
    --history-out-dir)
      HISTORY_OUT_DIR="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would export and validate real live-schema fixtures in %s\n' "$LIVE_OUT_DIR"
  printf 'Would export and validate real history fixtures in %s\n' "$HISTORY_OUT_DIR"
  run scripts/export-real-live-schema.sh --dry-run --out-dir "$LIVE_OUT_DIR"
  run scripts/export-real-history-fixtures.sh --dry-run --out-dir "$HISTORY_OUT_DIR"
  printf 'Would validate existing real fixtures with --validate-only without calling Home Assistant.\n'
  printf '\nRequired environment variables are the union of both wrapper dry-runs above.\n'
  exit 0
fi

if [[ "$VALIDATE_ONLY" -eq 0 ]]; then
  run scripts/export-real-live-schema.sh --out-dir "$LIVE_OUT_DIR"
  run scripts/export-real-history-fixtures.sh --out-dir "$HISTORY_OUT_DIR"
else
  shopt -s nullglob
  live_fixtures=("$LIVE_OUT_DIR"/real_*.json)
  history_fixtures=("$HISTORY_OUT_DIR"/real_*.json)
  shopt -u nullglob
  if (( ${#live_fixtures[@]} == 0 )); then
    echo "No real live-schema fixtures found in $LIVE_OUT_DIR" >&2
    exit 2
  fi
  if (( ${#history_fixtures[@]} == 0 )); then
    echo "No real history fixtures found in $HISTORY_OUT_DIR" >&2
    exit 2
  fi
fi

run python3 scripts/validate-live-schema-fixture.py \
  --profile ha-energy-planner-v1-real \
  "$LIVE_OUT_DIR"/real_*.json
run python3 scripts/validate-live-schema-fixture.py \
  --profile ha-energy-planner-haeo-value-v1-real \
  "$LIVE_OUT_DIR"/real_*.json
run python3 scripts/validate-real-history-fixture.py \
  --profile ha-energy-planner-history-v1-real \
  "$HISTORY_OUT_DIR"/real_*.json

printf '\nHA Energy Planner real validation bundle passed\n'
