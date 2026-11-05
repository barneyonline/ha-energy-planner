# Cloud development

The development image extends the official Home Assistant image selected by
`scripts/support_policy.py image` (currently HA 2026.10.0 with Python 3.14).
It installs pytest and coverage from `tests/requirements.txt`, and mypy and ruff
from the support policy in `pyproject.toml`. This repository uses its own fakes
and actual HA runtime contracts; no Home Assistant pytest plugin is required.
Source code is mounted from the checkout, so editing it does not require a build.

## Saved environment commands

Set the saved cloud environment's install script to:

```sh
cd /workspace/ha-energy-planner
scripts/ha-dev.sh build
```

Set its session startup script to:

```sh
cd /workspace/ha-energy-planner
scripts/ha-dev.sh start
```

The host needs Docker with BuildKit, Python 3.11+ for support-policy parsing,
Bash and `sha256sum`. Python 3.14 and development packages live in the container;
a separate host HA virtualenv is unnecessary. The managed platform supplies the
Docker daemon; startup does not attempt privileged daemon recovery.

Installation caches the image and normal build layers. Startup checks the image
against the current Dockerfile, build context rules and dependency pins, recreates
only this checkout's development container, checks dependencies and prints exact
versions. It does not install packages or run tests. If dependency inputs change,
run `build` then `start`. `HEP_DEV_CONTAINER` selects another container name when
using multiple checkouts; ownership labels prevent replacing another checkout's
container.

Docker's configured proxy defaults and registry credentials are preserved.
Builds mount the outer system CA bundle as a BuildKit secret; runtime containers
mount it read-only and configure Python/pip trust. The bundle is never copied
into the image. On Linux it defaults to `/etc/ssl/certs/ca-certificates.crt`;
set `HEP_CA_BUNDLE` to another readable combined system/proxy CA bundle when
needed. Restart `ha-dev` in every new session to refresh proxy settings and trust.
The development build fetches tools from PyPI, without HA's optional wheel index.
Keep the package-manager network preset and access to `ghcr.io` and
`pkg-containers.githubusercontent.com` for HA and lint images.

## Development and validation

```sh
scripts/docker-pytest-fast.sh tests/test_planner.py
scripts/ha-dev.sh exec python3 -m ruff check custom_components tests scripts
scripts/ha-dev.sh exec python3 -m mypy
scripts/ha-dev.sh shell
```

Commands reuse the running development container. No test arguments means the
fast pytest runner selects previous failures, or all tests if there are none.
The container runs as the host UID/GID so development artifacts remain writable.
Tool caches persist under the ignored `docker/dev-cache` directory, separately
from caches written by the full validation gate's isolated containers.

Run the required full gate from the host shell (outside `ha-dev`):

```sh
scripts/docker-validate.sh
```

The full gate creates additional HA containers. On a disk-limited cloud using
Docker's `vfs` storage driver, first run `scripts/ha-dev.sh remove` to release
the development container's writable layer, then run `scripts/ha-dev.sh start`
after validation. Removal preserves the image, build layers, tool caches and
manual HA data. Merely stopping a container does not release its disk space.

It continues to use isolated official HA containers, coverage thresholds, replay,
schema/history checks and runtime/package smoke tests. CI also checks `stable`
for forward compatibility. These checks stay separate from session startup.

For an explicit uncached image rebuild:

```sh
scripts/ha-dev.sh stop
scripts/ha-dev.sh build --no-cache --pull
scripts/ha-dev.sh start
```

This rebuilds dependencies without deleting the checkout or manual HA data.
Routine starts reuse the image; they never prune images, volumes or caches.

## Optional manual UI

```sh
scripts/ha-dev.sh runtime
# Open http://localhost:8124 (use the cloud environment's port preview if needed).
scripts/ha-dev.sh runtime-stop
```

The Compose `ha-runtime` service has a manual profile and no automatic restart.
The helper selects the same baseline image from the support policy. A direct
Compose invocation defaults to HA 2026.10.0 rather than moving `stable`.
Manual configuration/data lives under the ignored `docker/homeassistant/config`
directory and survives container recreation. The integration is mounted read-only.
Development startup does not start, modify or delete this runtime or its data.

Repository scripts make the setup reproducible after checkout. Saving these
commands into the cloud environment and rebuilding its saved template requires
the environment configuration UI; editing a running machine does not update it.
