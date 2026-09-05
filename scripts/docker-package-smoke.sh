#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PACKAGE_DIR="$(mktemp -d "$ROOT_DIR/.package-smoke.XXXXXX")"
trap 'rm -rf "$PACKAGE_DIR"' EXIT
archive="$(python3 scripts/package-release.py --output-dir "$PACKAGE_DIR")"
python3 -m zipfile -e "$archive" "$PACKAGE_DIR/unpacked"
HEP_COMPONENT_DIR="$PACKAGE_DIR/unpacked/custom_components/ha_energy_planner" scripts/docker-ha-smoke.sh
