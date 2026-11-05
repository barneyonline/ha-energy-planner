#!/usr/bin/env bash
# Cloud installation, session startup and reusable development commands.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

policy() { python3 scripts/support_policy.py "$1"; }
HA_IMAGE="$(policy image)"
DEV_IMAGE="ha-energy-planner-dev:$(policy baseline)"
DEV_CONTAINER="${HEP_DEV_CONTAINER:-ha-energy-planner-ha-dev}"
OWNER_LABEL=io.ha-energy-planner.workspace
SOURCE_LABEL=io.ha-energy-planner.dev-source
CA_BUNDLE="${HEP_CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}"

fail() { printf '%s\n' "$*" >&2; exit 1; }
fingerprint() {
  sha256sum Dockerfile.dev .dockerignore pyproject.toml tests/requirements.txt | sha256sum | cut -d ' ' -f 1
}
check_ca() {
  if [[ -n "${HEP_CA_BUNDLE:-}${CODEX_PROXY_CERT:-}" && ! -r "$CA_BUNDLE" ]]; then
    fail "Set HEP_CA_BUNDLE to a readable combined system/proxy CA bundle."
  fi
}
check_owner() {
  local owner
  owner="$(docker container inspect --format "{{index .Config.Labels \"$OWNER_LABEL\"}}" "$DEV_CONTAINER")"
  [[ "$owner" == "$ROOT_DIR" ]] || fail "$DEV_CONTAINER belongs to another checkout; set HEP_DEV_CONTAINER."
}
check_source() {
  local source
  source="$(docker "$1" inspect --format "{{index .Config.Labels \"$SOURCE_LABEL\"}}" "$2" 2>/dev/null)" || \
    fail "Development image/container is missing; run scripts/ha-dev.sh build then start."
  [[ "$source" == "$(fingerprint)" ]] || \
    fail "Development dependencies changed; run scripts/ha-dev.sh build then start."
}

command="${1:-help}"
(( $# == 0 )) || shift
case "$command" in
  build)
    for argument in "$@"; do
      [[ "$argument" == --no-cache || "$argument" == --pull ]] || fail "Build supports --no-cache and --pull only."
    done
    check_ca
    # Keep buildx state writable without replacing Docker credentials/config.
    export BUILDX_CONFIG="${BUILDX_CONFIG:-$ROOT_DIR/docker/buildx}"
    mkdir -p "$BUILDX_CONFIG"
    build_args=()
    if [[ -r "$CA_BUNDLE" ]]; then
      build_args+=(--secret "id=cloud_ca,src=$CA_BUNDLE")
    fi
    # Docker supplies the platform's reachable build proxy automatically.
    DOCKER_BUILDKIT=1 docker build "${build_args[@]}" "$@" \
      --build-arg "HA_IMAGE=$HA_IMAGE" \
      --build-arg "MYPY_VERSION=$(policy mypy)" \
      --build-arg "RUFF_VERSION=$(policy ruff)" \
      --label "$SOURCE_LABEL=$(fingerprint)" \
      --file Dockerfile.dev --tag "$DEV_IMAGE" .
    ;;
  start)
    [[ $# == 0 ]] || fail "start takes no arguments."
    check_ca
    check_source image "$DEV_IMAGE"
    if docker container inspect "$DEV_CONTAINER" >/dev/null 2>&1; then
      check_owner
      docker rm -f "$DEV_CONTAINER" >/dev/null
    fi
    runtime_args=()
    if [[ -r "$CA_BUNDLE" ]]; then
      runtime_args+=(--mount "type=bind,src=$CA_BUNDLE,dst=/run/cloud-ca-certificates.crt,readonly"
        -e SSL_CERT_FILE=/run/cloud-ca-certificates.crt
        -e REQUESTS_CA_BUNDLE=/run/cloud-ca-certificates.crt
        -e PIP_CERT=/run/cloud-ca-certificates.crt)
    fi
    DEV_CACHE_DIR="${HEP_DEV_CACHE_DIR:-$ROOT_DIR/docker/dev-cache}"
    mkdir -p "$DEV_CACHE_DIR"
    # Recreate per session to pick up current Docker proxy defaults and CA trust.
    docker run -d --name "$DEV_CONTAINER" \
      --label "$OWNER_LABEL=$ROOT_DIR" --label "$SOURCE_LABEL=$(fingerprint)" \
      --user "$(id -u):$(id -g)" --mount "type=bind,src=$ROOT_DIR,dst=/work" \
      --mount "type=bind,src=$DEV_CACHE_DIR,dst=/dev-cache" \
      -w /work -e PYTHONDONTWRITEBYTECODE=1 \
      -e 'PYTEST_ADDOPTS=-o cache_dir=/dev-cache/pytest' \
      -e RUFF_CACHE_DIR=/dev-cache/ruff -e MYPY_CACHE_DIR=/dev-cache/mypy \
      -e PIP_INDEX_URL=https://pypi.org/simple -e PIP_EXTRA_INDEX_URL=https://pypi.org/simple \
      "${runtime_args[@]}" "$DEV_IMAGE" >/dev/null
    if ! docker exec "$DEV_CONTAINER" python3 -m pip check || \
       ! docker exec "$DEV_CONTAINER" python3 scripts/verify-dev-environment.py; then
      docker rm -f "$DEV_CONTAINER" >/dev/null
      fail "Development verification failed; rebuild the image before starting again."
    fi
    ;;
  exec|shell|pytest)
    check_source container "$DEV_CONTAINER"
    check_owner
    running="$(docker container inspect --format '{{.State.Running}}' "$DEV_CONTAINER")"
    [[ "$running" == true ]] || fail "Development container is stopped; run scripts/ha-dev.sh start."
    exec_args=()
    case "$command" in
      shell)
        [[ $# == 0 ]] || fail "shell takes no arguments."
        exec_args=(-it)
        set -- /bin/sh
        ;;
      pytest)
        pytest_args=(-q -x --durations=10)
        if (( $# == 0 )); then
          pytest_args+=(--lf --last-failed-no-failures=all)
        fi
        set -- python3 -m pytest "${pytest_args[@]}" "$@"
        ;;
      exec) (( $# > 0 )) || fail "exec requires a command." ;;
    esac
    exec docker exec "${exec_args[@]}" "$DEV_CONTAINER" "$@"
    ;;
  stop|remove)
    [[ $# == 0 ]] || fail "$command takes no arguments."
    check_owner
    if [[ "$command" == remove ]]; then
      docker rm -f "$DEV_CONTAINER"
    else
      docker stop "$DEV_CONTAINER"
    fi
    ;;
  runtime|runtime-stop)
    [[ $# == 0 ]] || fail "$command takes no arguments."
    export HEP_HA_IMAGE="$HA_IMAGE"
    if [[ "$command" == runtime ]]; then
      mkdir -p docker/homeassistant/config
      if [[ ! -e docker/homeassistant/config/configuration.yaml ]]; then
        printf 'default_config:\nlogger:\n  default: warning\n' > docker/homeassistant/config/configuration.yaml
      fi
      docker compose --profile manual up -d ha-runtime
    else
      docker compose --profile manual stop ha-runtime
    fi
    ;;
  help|--help|-h)
    printf '%s\n' \
      'Install: scripts/ha-dev.sh build [--no-cache] [--pull]' \
      'Session startup: scripts/ha-dev.sh start' \
      'Development: scripts/ha-dev.sh pytest [paths or node IDs]' \
      'Commands: scripts/ha-dev.sh exec COMMAND [ARGS...] | shell | stop | remove' \
      'Optional UI: scripts/ha-dev.sh runtime | runtime-stop' \
      'Full gate: scripts/docker-validate.sh'
    ;;
  *) fail "Unknown command: $command; use scripts/ha-dev.sh help." ;;
esac
