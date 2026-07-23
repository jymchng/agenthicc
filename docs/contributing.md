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

## Verification

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/ -q
uv run nox -s llms_check
```

Build docs with `mkdocs build` once the documentation dependency is installed.
The repository's docs workflow currently depends on that external tool; adding
it to project metadata is PRD-138 P0.5 work.
