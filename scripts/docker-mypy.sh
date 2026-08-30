#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

docker run --rm \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/work" \
  -w /work \
  ghcr.io/home-assistant/home-assistant:2026.8.2 \
  sh -c 'HA_SITE_PACKAGES="$(python3 -c "import site; print(site.getsitepackages()[0])")" && ln -s /usr/src/homeassistant/homeassistant "$HA_SITE_PACKAGES/homeassistant" && python3 -m pip install --quiet --disable-pip-version-check --root-user-action ignore "mypy==1.19.1" && python3 -m mypy'
