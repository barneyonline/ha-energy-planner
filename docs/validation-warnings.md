# Validation warnings

Validation keeps unexpected warnings visible. Pytest treats new warnings as
errors. Its two exceptions match the exact category, message and module of the
Home Assistant 2026.10 HTTP implementation's aiohttp warnings; they do not apply
to Energy Planner code. Remove each exception when all supported Home Assistant
images stop emitting it.

Smoke runs retain their synthetic Home Assistant console log and a classified
warning report on success and failure. Local evidence is under
`.validation-artifacts/`; `HEP_VALIDATION_ARTIFACTS_DIR` selects another output
directory. CI uploads only the console and warning reports, rather than the
temporary Home Assistant configuration or storage. Known upstream messages stay
in the raw log. A new warning, an error, or a slow entity-state write fails the
smoke gate and requires investigation.
Records from default Python worker names such as `Thread-1 (worker)` are included;
parentheses in a thread name do not bypass diagnostic classification.
The record header determines its severity: quoted diagnostic text inside DEBUG
or INFO messages does not become a warning. Standalone Python warning headers
are retained, including the base `Warning` category and custom category names.

## Repository fixes

- Artifact uploads use a Node 24 action. This replaces the Node 20 declaration
  and its deprecated Node API dependencies; both coverage and release artifacts
  retain their names and ZIP format.
- Repository workflows pin Ubuntu 26.04, which passed the audited checks,
  instead of changing host versions with `ubuntu-latest`.
- Codecov consumes the generated `coverage.xml` and `junit.xml` explicitly.
  Automatic collection plugins are disabled, so the host need not install
  coverage.py, gcov or Xcode. `codecov.yml` makes the existing default CI
  requirement explicit; statement and branch gates remain local and unchanged.
- The smoke weather provider is a real weather entity with deterministic hourly
  forecasts, rather than a sensor passed to `weather.get_forecasts`. Dedicated
  tests retain legacy-attribute fallback coverage. The thermostat fixture starts
  from a saved target, avoiding a first-boot missing-target warning.

## Upstream and environment ownership

The 31 October 2026 audit also found messages whose source is outside the
integration. They do not justify changing production dependencies or disabling
verification.

| Audited messages | Handling and removal criterion |
| --- | --- |
| Home Assistant HTTP inheritance deprecation and aiohttp `NotAppKeyWarning` | Exact test-only exceptions in `pyproject.toml`. Retain repair/upgrade tests and remove exceptions after the supported images adopt the upstream fix. [Owning implementation](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/http/server.py). |
| Rich `return` in a `finally` block | Classify the exact dependency warning in smoke evidence. Follow the Home Assistant image's Rich update; do not pin or monkey-patch Rich in this integration. [Upstream report](https://github.com/Textualize/rich/issues/4161). |
| Custom integration loader notices for Energy Planner and the smoke helper | Expected for custom integrations; preserve the notices in the console/report. The repository's Platinum claim does not imply Home Assistant review. |
| Codecov GPG key lacks web-of-trust certification | Preserve verification. The audited upload reports a good signature and a matching checksum. Do not disable integrity checks or assign arbitrary trust just to hide the notice. |
| CodeQL cannot find the hosted interpreter's dynload path | Owned by GitHub's managed extractor. The audited scan covered 180/180 Python files and 13/13 Actions files. Monitor extraction coverage; investigate upstream if files stop being scanned. |
| Dependency Graph rehash skips the combined CA bundle | Owned by the managed job's certificate tooling. The audited job succeeded. Retain trust configuration; investigate if dependency fetching or TLS fails. |
| hassfest has no native arm64 image; jemalloc uses memset under QEMU | Use hosted amd64 validation or an explicit `--platform linux/amd64` local run. The QEMU allocator notice is expected; a failed validator is not. |
| Local mypy exits 137 | An interrupted check is a failure, not a pass. Run heavy local validation serially with enough Docker VM memory; preserve the failure and rerun. The audit's two interrupted runs do not prove an out-of-memory cause. Do not weaken typing or coverage to accommodate resource contention. |
| Historical GitHub AI security request used an unsupported model | The 6 September request on PR #71 is not a current-main check or a vulnerability finding. The model belongs to the GitHub-managed service; no repository model setting is available to repair. A new supported request must be validated in that service if it is used again. Current CodeQL remains required. |

Git default-branch hints, CodeQL option descriptions containing “deprecated”, and
expected synthetic-fixture rejection output are not emitted project warnings.
The strict real-data fixture profiles deliberately reject synthetic evidence;
passing automated checks does not establish a completed household observation.

## State-rendering performance

Calendar rendering builds common EV action details once per read, then expands
the charging windows. Bounded presentation attributes normalize only the
dictionary/list elements they retain. Calendar history uses archived identifiers
and confirmed pending records to avoid repeated comparisons; confirmed history
retains its existing lifetime and is not truncated by count.

A synthetic Home Assistant 2026.10.4 probe with 576 slots, 288 separate charging
windows and 1,000 saved history entries reduced median calendar rendering from
0.332 to 0.106 seconds. A next-actions probe reduced 0.860 to 0.289 milliseconds.
These are three-run measurements on the validation host, not household latency
guarantees. The original multi-second warnings occurred under competing Docker
load and were not reproduced in the isolated probe. No persistent presentation
cache was introduced: each read still observes current metadata, ownership and
time. Regression tests cover fragmented schedules, changed metadata, discarded
large evidence branches, dataclass conversion and retained confirmed history.

## Reviewing future warnings

Read the raw message and identify its owner before adding an exception. Keep
exceptions specific to the emitting module and message, with a reason and a
removal criterion. Do not allowlist slow state writes, missing mapped inputs,
new integration warnings, tracebacks, or service failures. Reproduce timing
warnings without competing heavy jobs and profile the relevant state-building
path before changing its behavior.
