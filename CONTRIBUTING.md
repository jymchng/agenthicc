# Contributing to Agenthicc

The canonical contributor guide is [docs/contributing.md](docs/contributing.md).
Read it together with [CLAUDE.md](CLAUDE.md) and [AGENTS.md](AGENTS.md).

## Setup

```bash
git clone https://github.com/agenthicc/agenthicc.git
cd agenthicc
uv sync --extra dev
```

The checked-in `pyproject.toml` uses the lauren-ai dependency declared by the
lockfile. If you develop against a sibling lauren-ai checkout, use the local
source override documented in `pyproject.toml`; do not assume a sibling path is
required for every install.

## Development checks

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
uv run nox -s llms_check
```

Linux development and CI environments need the `bubblewrap` (`bwrap`) OS
package for worktree-environment execution tests. Agenthicc fails closed when
it is unavailable rather than running project commands without isolation.

The Nox sessions are the CI definitions. `uv run nox --list` prints them:

| Nox session | Runs |
|---|---|
| `tests` (3.11, 3.12, 3.13) | Full suite on one supported Python, e.g. `nox -s tests-3.12` |
| `tests_unit` / `tests_integration` (3.11, 3.12, 3.13) | The matching `tests/` subdirectory on one Python, e.g. `nox -s tests_unit-3.12` |
| `tests_e2e` | `tests/e2e` on Python 3.12 |
| `tests_windows` | The lock, Windows filesystem and Windows terminal tests (the Windows CI job) |
| `coverage` | Full suite with branch and subprocess coverage; the floor is `[tool.coverage.report] fail_under` |
| `lint` / `format` | Ruff check plus format check, or format in place |
| `typecheck` / `typecheck_contracts` | mypy over `src/agenthicc`, then over the boundary tests and the ratchet script |
| `type_audit` | The typing-debt ratchet against `docs/reference/type-safety-baseline.json` |
| `docs` | `mkdocs build --strict` |
| `build` / `build_check` | `uv build`, then `twine check --strict dist/*` |
| `llms_check` | Every `agenthicc.__all__` and `agenthicc.kernel.__all__` symbol has a `###` heading in `llms-full.txt` |
| `clean` | Remove build, test, and coverage artifacts |

Every session runs on nox's uv backend: `uv sync --extra dev` installs the
project and its one `dev` extra — test, lint, type, docs and release tools —
into the session's own virtualenv, so each tool runs at its declared version on
the session's interpreter. Report failures from a clean checkout rather than
silently changing the environment.

## Required gates

`.github/workflows/ci.yml` runs every gate on each push and pull request: lint,
typecheck, the type ratchet, `llms_check`, the strict docs build, unit and
integration tests on Python 3.11–3.13, E2E tests, the Windows tests, and
coverage. Its final job, **Required checks**, fails unless all of them
succeeded; branch protection requires that one check, and
`scripts/apply_branch_protection.sh` applies the rule for a repository
administrator. The release workflow runs the same CI for the tagged commit
before it builds, so a tag on a red commit cannot publish.

The same gates run locally before you push:

```bash
uvx pre-commit install   # ruff on commit; mypy, the ratchet and unit tests on push
```

Quality baselines — the type-safety baseline, the coverage floor and the gate
definitions — are owned in `.github/CODEOWNERS`. The type ratchet fails when a
pull request raises a baseline value; see the
[type-safety guide](docs/guides/type-safety.md).

## Which surface are you changing?

| Surface | Paths | Gate |
|---|---|---|
| Documentation | `docs/`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `skills/`, `llms.txt`, `llms-full.txt` | `uv run mkdocs build --strict`; `uv run nox -s llms_check` when public symbols change |
| Source | `src/`, `tests/`, `scripts/`, `noxfile.py`, `pyproject.toml`, `.github/`, `mkdocs.yml` | The full matrix: `lint`, `typecheck`, `type_audit`, and the test sessions |

Documentation-only changes need the documentation gate; they do not require the
full source matrix. `mkdocs.yml` is site configuration, so a nav edit counts as
a source change even though it controls documentation. `README.md` lives
outside `docs/`, so the docs build never validates it — check its links
separately.

## Test layout

| Path | Contents |
|---|---|
| `tests/unit/` | Pure reducers, parsers, registries, configuration, rendering, security |
| `tests/integration/` | Real event processor, memory/database, plugin/tool/workflow boundaries |
| `tests/e2e/` | Full session, cassettes, PTY and cross-component behaviour, including CLI schedule lifecycle and scheduler process recovery |
| `tests/performance/` | The reconstruct-site performance case |
| `tests/fixtures/` | Shared non-test data |
| `tests/conftest.py`, `tests/conftest_cassette.py` | Shared state, processor, and cassette fixtures |

`pyproject.toml` sets `asyncio_mode = "auto"`, `testpaths = ["tests"]`,
`timeout = 60`, and the `unit`, `integration`, `e2e`, `cloakbrowser`, and
`playwright` markers. Place a new test in the directory that matches the
boundary it exercises and apply the matching marker.

## Design invariants

- Kernel reducers are pure; kernel `AppState` is frozen and event-driven.
- The reactive TUI `AppState` is a separate presentation model.
- Tools use capability, path, network, approval, timeout, and retry contracts.
- Durable conversation and tool replay must not duplicate side effects.
- Platform-specific terminal calls stay in the dedicated terminal backends.

For new public exports, update `llms-full.txt`.
