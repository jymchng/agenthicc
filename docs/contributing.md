# Contributing

## Workflow

1. Create a branch from `main`.
2. Read `CLAUDE.md` and `AGENTS.md` for ownership and invariants.
3. Search current consumers before changing a public symbol or path.
4. Add or update tests with the implementation.
5. Update README/docs/LLM docs when behaviour or public exports change.
6. Run the checks relevant to the change and report environment blockers.
7. Add an entry under `[Unreleased]` in `CHANGELOG.md` for user-visible work.

## Commit style

Use concise conventional prefixes:

```text
feat(workflows): preserve phase context on resume
fix(config): load HTTP timeout from TOML
docs(architecture): document kernel and reactive state boundary
test(tui): cover non-TTY shutdown
```

## Review checklist

- [ ] The change is in the correct ownership boundary.
- [ ] New signatures use concrete parameterized types and preserve the type-safety ratchet.
- [ ] Security, approval, timeout, retry, and cancellation paths are covered.
- [ ] Kernel reducers remain pure and kernel state remains frozen.
- [ ] Durable formats have recovery or migration coverage.
- [ ] Public exports are present in `llms-full.txt`.
- [ ] User-facing docs and changelog are updated.
- [ ] No credentials, generated caches, or session data are committed.

## Test layout

| Path | Contents |
|---|---|
| `tests/unit/` | Pure reducers, parsers, registries, configuration, rendering, security |
| `tests/integration/` | Real event processor, memory/database, plugin/tool/workflow boundaries |
| `tests/e2e/` | Full session, cassettes, PTY and cross-component behaviour |
| `tests/performance/` | The reconstruct-site performance case |
| `tests/fixtures/` | Shared non-test data |
| `tests/conftest.py`, `tests/conftest_cassette.py` | Shared state, processor, and cassette fixtures |

`pyproject.toml` sets `asyncio_mode = "auto"`, `testpaths = ["tests"]`,
`timeout = 60`, and the `unit`, `integration`, `e2e`, `cloakbrowser`, and
`playwright` markers. Place a test in the directory that matches the boundary
it exercises. The Nox sessions `tests_unit`, `tests_integration`, and
`tests_e2e` run those directories directly; `coverage` runs the whole suite
with branch and subprocess coverage against the floor in
`[tool.coverage.report] fail_under`.

## Which surface are you changing?

| Surface | Paths | Gate |
|---|---|---|
| Documentation | `docs/`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `skills/`, `llms.txt`, `llms-full.txt` | `uv run mkdocs build --strict`; `uv run nox -s llms_check` when public symbols change |
| Source | `src/`, `tests/`, `scripts/`, `noxfile.py`, `pyproject.toml`, `.github/`, `mkdocs.yml` | `uv run nox -s lint`, `-s typecheck`, `-s type_audit`, and the unit/integration/E2E sessions |

A documentation-only change needs the documentation gate and not the source
matrix. Two boundaries are easy to get wrong:

- `mkdocs.yml` is site configuration, so editing the nav is a source change
  even though it controls documentation output.
- `README.md` sits outside `docs/`, so `mkdocs build --strict` never validates
  it. Its relative links and anchors need checking on their own.

## Verification

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/ -q
uv run nox -s llms_check
```

Build the docs with `uv run mkdocs build --strict` (or `uv run nox -s docs`).
MkDocs and MkDocs Material are declared in the `dev` extra of `pyproject.toml`,
so no separate install is needed.

Every Nox session runs on nox's uv backend and installs the project and its one
`dev` extra into the session's own virtualenv. TUI, API, and agent modules ship
in the base package; the project declares no `tui`, `api`, or `all` extra. List
the sessions with `uv run nox --list`: `tests`, `tests_unit` and
`tests_integration` (each on Python 3.11, 3.12 and 3.13, e.g.
`tests_unit-3.12`), `tests_e2e`, `tests_windows`, `coverage`, `lint`, `format`,
`typecheck`, `typecheck_contracts`, `type_audit`, `docs`, `build`,
`build_check`, `llms_check`, `clean`.

`.github/workflows/ci.yml` runs every gate on each push and pull request and
ends in one aggregate **Required checks** job, which is the status check branch
protection requires (`scripts/apply_branch_protection.sh`). The release
workflow re-runs that CI for the tagged commit, checks that the tag names the
package version, and checks that the built wheel's `agenthicc --version`
reports it. `uvx pre-commit install` runs the same tools locally: ruff on
commit; mypy, the type ratchet and the unit tests on push.

`nox -s llms_check` checks that every symbol in `agenthicc.__all__` and
`agenthicc.kernel.__all__` has a `###` heading in `llms-full.txt`; it does not generate the file or validate prose
sections. When changing a public tool, workflow, TUI, or session-service
symbol, add or update the corresponding `### Symbol` section by hand and verify
the import path against `src/agenthicc/`.
