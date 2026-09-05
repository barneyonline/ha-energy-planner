#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
HA_IMAGE="${HEP_HA_IMAGE:-$(python3 scripts/support_policy.py image)}"
docker image inspect "$HA_IMAGE" >/dev/null 2>&1 || docker pull "$HA_IMAGE" >/dev/null
docker image inspect "$HA_IMAGE" --format '{{json .}}' | python3 -c '
import json, sys
image=json.load(sys.stdin)
print(json.dumps({"image_id":image["Id"], "digests":image["RepoDigests"], "labels":image["Config"]["Labels"]}, indent=2))'
docker run --rm --entrypoint python3 "$HA_IMAGE" -c '
import json, platform
from importlib.metadata import version
print(json.dumps({"python":platform.python_version(), "homeassistant":version("homeassistant"), "pytest":version("pytest"), "coverage":version("coverage")}, indent=2))'
python3 scripts/support_policy.py mypy
