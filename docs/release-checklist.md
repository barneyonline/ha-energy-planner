# Stable release acceptance

The first stable milestone requires observed device cycles. Synthetic checks do
not substitute for that evidence. This checklist does not claim a household
observation has run or that a release candidate has been published.

## Automated gate

Run `scripts/docker-validate.sh` without skip flags: compilation, Ruff, strict
typing, quality evidence, exact statement coverage, branch regression, fixtures,
HA configuration, runtime compatibility and smoke from the built ZIP. Capture
the console log; opening environment evidence records image identity/digests,
HA, Python, pytest, coverage and mypy. Floating stable is pulled by CI; the local
release gate defaults to the pinned baseline.

`tests/branch-coverage-baseline.json` limits uncovered branches per module. New
modules have no gap allowance. Reduce limits as tests cover gaps. Do not raise
limits just to make a change pass. Prioritize coordinator/executor/HVAC failure
and cancellation scenarios when changing those paths; review defensive exits
rather than adding assertions solely to reach a percentage.

## Representative observation

Copy `docs/release-observation-template.json`, record the candidate revision,
start/end timestamps and redacted evidence references. Observe at least 48 hours,
normally 48–72, extending until each scenario has completed:

| Scenario | Acceptance |
|---|---|
| Startup/reload | No old coordinator commands; intent and recovery evidence survive. |
| EV plug-in/completion | No unexplained start; charging completes or exposes a specific block. |
| Unavailable/delayed feedback | Fail closed and retain uncertain ownership/reservations. |
| Manual HVAC override/release | User targets persist; only still-owned state is restored. |
| Enphase restoration | Original profile restored with observed feedback. |
| Performance | Record refresh latency/rate; no sustained blocking or replan storm. |

Use real HA traces, state history and redacted support bundles. For hardware
missing from the release environment, obtain a representative tester's evidence;
do not silently mark a scenario passed. No operating record is bundled by default.
Each scenario's evidence must be a nonempty text reference; null, booleans,
numbers, collections, and blank text are rejected.

## Package and publication

1. Update manifest and pyproject together; move Unreleased notes into a dated
   changelog section. Tests compare sources without duplicating release numbers.
   Keep an empty Unreleased template. Use a release candidate before the first
   stable 1.0 milestone.
2. Freeze the final release commit, including its version metadata, and complete
   the operating record for that exact revision and version. Subsequent edits
   invalidate the record; do not bump the version after recording evidence.
3. Run `python3 scripts/package-release.py --version vVERSION` and
   `scripts/docker-package-smoke.sh`. The deterministic ZIP/checksum are in
   `dist/`; smoke installs an unpacked copy rather than source files.
4. Review all required GitHub checks for the exact release revision and retain
   artifacts. Metadata-only release changes still require the full release gate.
5. Run `python3 scripts/validate-release-evidence.py --evidence PATH --commit SHA`
   on the completed observation record as a manual stable-release acceptance check.
   Keep that record outside Git (it references the final commit). It may be attached
   as `release-evidence.json` for traceability. Release packaging does not require
   that asset or claim that household observation has passed.
6. Publish the reviewed version/commit and verify metadata, ZIP and SHA-256.
   Publication repeats package validation; it does not replace prepublication review.

The Release Assets workflow checks tag/manifest/project version agreement, builds
the ZIP/checksum and runs the packaged runtime smoke test before uploading. To
repair missing assets on an existing release, run this workflow manually from
`main` with its `release_tag` input (for example `v1.3.0`). It checks out that exact
tag and attaches assets to the same release. Rerunning an older failed run uses
the old workflow definition; use a new manual run after the workflow fix is merged.

## Quality and maintenance

Platinum is self-assessed. Configuration readiness is deliberately checked after
name-only setup and before command authority; entry creation alone does not
prove working mappings. Migration repair uses Reconfigure; runtime safety issues
use preflight, translated errors and restoration.

Keep module extraction incremental. The durable Store adapter owns confirmation
and cancellation state behind the existing Store interface. Coordinator lifetime
and command locks retain their authority. Avoid a broad rewrite during release
stabilization without a demonstrated behavioral need.
