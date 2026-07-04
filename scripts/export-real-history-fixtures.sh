#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OUT_DIR="${HEP_HISTORY_OUT_DIR:-tests/fixtures/history}"
REDACT_KEYS="${HEP_REDACT_KEYS:-serial}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

required_vars=(
  HOME_ASSISTANT_URL
  HOME_ASSISTANT_TOKEN
  HEP_EV_CONNECTED_ENTITY
  HEP_EV_SOC_ENTITY
  HEP_THERMAL_INDOOR_ENTITY
  HEP_DAIKIN_POWER_ENTITY
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would export real history fixtures to %s\n' "$OUT_DIR"
  printf 'Required environment variables: %s\n' "${required_vars[*]}"
  printf 'Optional environment variables: HEP_HISTORY_START HEP_HISTORY_END HEP_HISTORY_DAYS HEP_OUTDOOR_TEMPERATURE_ENTITY HEP_THERMAL_INDOOR_ATTRIBUTE HEP_OUTDOOR_TEMPERATURE_ATTRIBUTE HEP_REDACT_KEYS\n'
  exit 0
fi

missing=()
for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    missing+=("$var")
  fi
done
if (( ${#missing[@]} > 0 )); then
  printf 'Missing required environment variables: %s\n' "${missing[*]}" >&2
  exit 2
fi

redact_args=()
IFS=',' read -r -a redact_parts <<< "$REDACT_KEYS"
for part in "${redact_parts[@]}"; do
  part="${part//[[:space:]]/}"
  if [[ -n "$part" ]]; then
    redact_args+=(--redact-key "$part")
  fi
done

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

mkdir -p "$OUT_DIR"

run python3 scripts/export-real-history-fixture.py \
  --out "$OUT_DIR/real_mini_trip_history.json" \
  --validate \
  "${redact_args[@]}" \
  ev-trip-history \
  --name real_mini_trip_history \
  --connected-entity "$HEP_EV_CONNECTED_ENTITY" \
  --soc-entity "$HEP_EV_SOC_ENTITY"

thermal_args=(
  --out "$OUT_DIR/real_daikin_thermal_history.json"
  --validate
  "${redact_args[@]}"
  thermal-history
  --name real_daikin_thermal_history
  --indoor-temperature-entity "$HEP_THERMAL_INDOOR_ENTITY"
  --hvac-power-entity "$HEP_DAIKIN_POWER_ENTITY"
)
if [[ -n "${HEP_THERMAL_INDOOR_ATTRIBUTE:-}" ]]; then
  thermal_args+=(--indoor-temperature-attribute "$HEP_THERMAL_INDOOR_ATTRIBUTE")
fi
if [[ -n "${HEP_OUTDOOR_TEMPERATURE_ENTITY:-}" ]]; then
  thermal_args+=(--outdoor-temperature-entity "$HEP_OUTDOOR_TEMPERATURE_ENTITY")
fi
if [[ -n "${HEP_OUTDOOR_TEMPERATURE_ATTRIBUTE:-}" ]]; then
  thermal_args+=(--outdoor-temperature-attribute "$HEP_OUTDOOR_TEMPERATURE_ATTRIBUTE")
fi

run python3 scripts/export-real-history-fixture.py "${thermal_args[@]}"

run python3 scripts/validate-real-history-fixture.py \
  --profile ha-energy-planner-history-v1-real \
  "$OUT_DIR"/real_*.json
