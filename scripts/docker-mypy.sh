#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HA_IMAGE="$(python3 scripts/support_policy.py image)"
MYPY_VERSION="$(python3 scripts/support_policy.py mypy)"

docker run --rm \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e MYPY_VERSION="$MYPY_VERSION" \
  -v "$PWD:/work" \
  -w /work \
  "$HA_IMAGE" \
  sh -c 'HA_SITE_PACKAGES="$(python3 -c "import site; print(site.getsitepackages()[0])")" && ln -s /usr/src/homeassistant/homeassistant "$HA_SITE_PACKAGES/homeassistant" && python3 -m pip install --quiet --disable-pip-version-check --root-user-action ignore "mypy==$MYPY_VERSION" && python3 -m mypy'
