#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

pytest_args=(-q -x --durations=10)
if (( $# == 0 )); then
  pytest_args+=(--lf --last-failed-no-failures=all)
else
  pytest_args+=("$@")
fi

docker run --rm \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/work" \
  -w /work \
  ghcr.io/home-assistant/home-assistant:stable \
  sh -c 'sh scripts/install-test-dependencies.sh && exec python3 -m pytest "$@"' sh "${pytest_args[@]}"
