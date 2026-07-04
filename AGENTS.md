# Repository Guidelines

## Project Structure & Module Organization
- `custom_components/ha_energy_planner/`: Home Assistant custom integration source.
  - `config_flow.py`: setup and options validation.
  - `coordinator.py`: planner refresh, Home Assistant state listeners, persistence orchestration.
  - `planner.py`: deterministic planning logic.
  - `executor.py`: active-mode safety gate and device action execution.
  - `*_adapter.py`: Home Assistant service adapters for Enphase, EV Smart Charging, and Daikin.
- `tests/`: pytest coverage for planner logic, adapters, services, fixtures, and maintenance scripts.
- `tests/fixtures/`: replay, live-schema, and real-history validation fixtures.
- `scripts/`: Docker validation, smoke testing, replay, export, and quality-scale tools.
- `docs/requirements-audit.md`: requirement-by-requirement implementation evidence.

## Build, Test, and Development Commands
- Full local gate: `scripts/docker-validate.sh`
  - Runs compile checks, shell syntax checks, Dockerized pytest, replay fixtures, live-schema validation, real-history validation, Home Assistant `check_config`, and the Docker smoke test.
- Quality scale evidence: `docker run --rm -v "$PWD:/work" -w /work ghcr.io/home-assistant/home-assistant:stable python3 scripts/validate_quality_scale.py`
- Focused test example: `docker run --rm -v "$PWD:/work" -w /work ghcr.io/home-assistant/home-assistant:stable python3 -m pytest -q tests/test_planner.py`
- Real evidence bundle, optional for operator validation: `scripts/export-real-validation-bundle.sh`

## Coding Style & Naming Conventions
- Python 3.11+, 4-space indentation, typed functions where practical.
- Keep Home Assistant I/O async and fail closed on service/network errors.
- Use `snake_case` modules/functions and `UPPER_SNAKE_CASE` constants.
- Avoid logging or persisting secrets, tokens, raw prompts, raw AI output, addresses, or unnecessary location history.

## Testing Guidelines
- Add tests beside the behavior being changed; use deterministic fakes for Home Assistant, service calls, Recorder, and network boundaries.
- Add replay/live-schema/history fixtures when behavior depends on external payload shape.
- Run `scripts/docker-validate.sh` before considering a change complete.
- Keep tests independent of a real Home Assistant instance or real vendor credentials.

## Quality Scale Expectations
- The manifest claims `platinum`; keep `quality_scale.yaml` evidence current when behavior or docs change.
- Any new integration surface should have code references, tests, docs where relevant, and Docker validation coverage.
- Do not weaken the full validation gate to satisfy a narrower change.

## Commit & Pull Request Guidelines
- Commits should be concise and imperative.
- PRs should include purpose, risk, validation commands, and links to relevant issues/spec sections.
- Update `README.md`, `docs/requirements-audit.md`, and `quality_scale.yaml` when user-facing behavior, validation evidence, or quality-scale claims change.
