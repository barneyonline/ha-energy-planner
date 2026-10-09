#!/usr/bin/env sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Production Home Assistant images need not include development test tools.
exec python3 -m pip install --quiet --disable-pip-version-check \
  --root-user-action ignore -r "$ROOT_DIR/tests/requirements.txt"
