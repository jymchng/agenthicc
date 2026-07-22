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
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
uv run nox -s llms_check
```

The Nox sessions are the CI definitions, but their optional-extra and docs-tool
installation paths are currently being aligned with project metadata in
PRD-138. Report failures from a clean checkout rather than silently changing
the environment.

## Design invariants

- Kernel reducers are pure; kernel `AppState` is frozen and event-driven.
- The reactive TUI `AppState` is a separate presentation model.
- Tools use capability, path, network, approval, timeout, and retry contracts.
- Durable conversation and tool replay must not duplicate side effects.
- Platform-specific terminal calls stay in the dedicated terminal backends.

For tests, use `tests/unit`, `tests/integration`, and `tests/e2e` according to
the boundary being exercised. For new public exports, update `llms-full.txt`.
