#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DRY_RUN=0
OUT_DIR="${HEP_LIVE_SCHEMA_OUT_DIR:-tests/fixtures/live_schema}"

while [[ "$#" -gt 0 ]]; do
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

required_env=(
  HOME_ASSISTANT_URL
  HOME_ASSISTANT_TOKEN
  HEP_AMBER_IMPORT_ENTITY
  HEP_AMBER_EXPORT_ENTITY
  HEP_PV_FORECAST_ENTITY
  HEP_BASELINE_LOAD_ENTITY
  HEP_WEATHER_ENTITY
  HEP_HAEO_SERVICE
)

if [[ "$DRY_RUN" != "1" ]]; then
  for name in "${required_env[@]}"; do
    if [[ -z "${!name:-}" ]]; then
      echo "Missing required environment variable: $name" >&2
      exit 2
    fi
  done
fi

redact_args=()
IFS=',' read -r -a redact_keys <<< "${HEP_REDACT_KEYS:-serial}"
for key in "${redact_keys[@]}"; do
  key="${key#"${key%%[![:space:]]*}"}"
  key="${key%"${key##*[![:space:]]}"}"
  if [[ -n "$key" ]]; then
    redact_args+=(--redact-key "$key")
  fi
done

haeo_service_data_json="${HEP_HAEO_SERVICE_DATA_JSON:-{\"source\":\"schema_export\"}}"

value_or_placeholder() {
  local name="$1"
  local placeholder="$2"
  printf '%s' "${!name:-$placeholder}"
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
    return
  fi
  printf '\n==> %s\n' "$*"
  "$@"
}

mkdir -p "$OUT_DIR"

run python3 scripts/export-live-schema-fixture.py \
  --out "$OUT_DIR/real_amber_import.json" \
  --validate \
  "${redact_args[@]}" \
  forecast-state \
  --name real_amber_import \
  --entity-id "$(value_or_placeholder HEP_AMBER_IMPORT_ENTITY sensor.amber_import)" \
  --value-kind price \
  --value-keys import_price,general_price,per_kwh,price,value

run python3 scripts/export-live-schema-fixture.py \
  --out "$OUT_DIR/real_amber_export.json" \
  --validate \
  "${redact_args[@]}" \
  forecast-state \
  --name real_amber_export \
  --entity-id "$(value_or_placeholder HEP_AMBER_EXPORT_ENTITY sensor.amber_export)" \
  --value-kind price \
  --value-keys export_price,feed_in_price,per_kwh,price,value

run python3 scripts/export-live-schema-fixture.py \
  --out "$OUT_DIR/real_pv_hafo.json" \
  --validate \
  "${redact_args[@]}" \
  forecast-state \
  --name real_pv_hafo \
  --entity-id "$(value_or_placeholder HEP_PV_FORECAST_ENTITY sensor.pv_forecast)" \
  --value-kind power \
  --value-keys pv_forecast,pv_estimate,power,watts,w,kw,prediction,value

run python3 scripts/export-live-schema-fixture.py \
  --out "$OUT_DIR/real_baseline_load.json" \
  --validate \
  "${redact_args[@]}" \
  forecast-state \
  --name real_baseline_load \
  --entity-id "$(value_or_placeholder HEP_BASELINE_LOAD_ENTITY sensor.baseline_load)" \
  --value-kind power \
  --value-keys baseline_load,load_forecast,power,watts,w,kw,value

run python3 scripts/export-live-schema-fixture.py \
  --out "$OUT_DIR/real_weather.json" \
  --validate \
  "${redact_args[@]}" \
  forecast-state \
  --name real_weather \
  --entity-id "$(value_or_placeholder HEP_WEATHER_ENTITY weather.home)" \
  --value-kind temperature \
  --value-keys temperature,native_temperature,nativeTemperature,currentTemperature,current_temperature,value

run python3 scripts/export-live-schema-fixture.py \
  --out "$OUT_DIR/real_haeo_response.json" \
  --validate \
  "${redact_args[@]}" \
  haeo-response \
  --name real_haeo_response \
  --service "$(value_or_placeholder HEP_HAEO_SERVICE haeo.optimize)" \
  --service-data-json "$haeo_service_data_json"

run python3 scripts/validate-live-schema-fixture.py \
  --profile ha-energy-planner-v1-real \
  "$OUT_DIR"/real_*.json
run python3 scripts/validate-live-schema-fixture.py \
  --profile ha-energy-planner-haeo-value-v1-real \
  "$OUT_DIR"/real_*.json
