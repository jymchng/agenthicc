# Type safety and static contracts

agenthicc uses mypy as its production type checker with checked untyped bodies,
complete signatures, and typed decorators. The checker and the typing-audit
script are part of the declared development environment, so a
fresh checkout can run the same checks as CI:

```bash
uv sync --extra dev
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
```

The audit is a ratchet, not a waiver for unresolved errors. It records source
inventory metrics and fails if typing debt grows beyond the checked-in
baseline. The current implementation is below the original baseline for
parameterized container annotations, explicit `Any`, and type-ignore comments.

For the complete local gate, run:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run mypy tests/unit/test_kernel_event_typing.py tests/unit/test_type_audit.py
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/ -q
```

The equivalent Nox sessions are `nox -s typecheck`,
`nox -s typecheck_contracts`, and `nox -s type_audit`.

## Boundary policy

Unknown TOML, JSON, plugin, and tool input enters as `object` or a recursive
JSON value and is narrowed immediately by a named validator or scalar helper.
Closed runtime contracts use dataclasses, `Protocol`, typed aliases, and
parameterized containers. The kernel event decoder validates persisted records
before reducers consume them.

Dynamic access remains limited to genuine provider, plugin, and optional
platform adapters. The reviewed mypy override list is intentionally narrow:
Pyodide, S3, MCP, and Outlook adapter modules may be unavailable on a given
development platform. Their public agenthicc-facing contracts remain typed and
their runtime behavior is covered by focused tests where the dependency is
available.

Do not add a repository-wide `ignore_missing_imports`, broad `Any`, bare
containers, or an unscoped `type: ignore`. If a third-party boundary cannot be
typed more precisely, document the module, external contract, runtime guard,
and test in the PRD and in the code review.
