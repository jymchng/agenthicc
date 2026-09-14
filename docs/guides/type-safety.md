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

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `mypy` reports an error in a file the guide says is exempted | The override list is narrow and names specific adapter modules, not whole packages | Read the override entry: Pyodide, S3, MCP, and Outlook adapters may be unimportable on your platform. Their agenthicc-facing contracts are still typed |
| The audit fails with "typing debt grew" | The ratchet only moves down | Reduce parameterized-container, explicit-`Any`, or type-ignore counts; do not edit the baseline in `docs/reference/type-safety-baseline.json` to pass |
| An unknown TOML or JSON value leaks deep into the code | It was not narrowed at the boundary | Narrow immediately with a named validator or scalar helper; boundary values enter as `object` or a recursive JSON value |
| A persisted kernel record crashes a reducer | Reducer input must be validated first | The kernel event decoder validates persisted records before reducers consume them; do not bypass it by loading JSONL yourself |
| A provider or plugin object is untyped everywhere | Dynamic access is limited to genuine adapter boundaries | Keep the contract typed and document the module, external contract, runtime guard, and test rather than widening `Any` |
| `ruff` and `mypy` disagree about a file | They check different things | Run the full local gate below; both must pass |
| Nox session names are rejected | Session names are specific | Use `nox -s typecheck`, `nox -s typecheck_contracts`, or `nox -s type_audit` |
| A fresh checkout cannot run the checks | The dev extra is not installed | `uv sync --extra dev` installs mypy, ruff, pytest, and the audit script |

### Run the whole gate, not one tool

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
```

A green `mypy` alone does not mean the ratchet passed; the audit compares
source-inventory metrics against the checked-in baseline and fails on growth.

### The baseline is not a waiver

`type-safety-baseline.json` records where the debt stands, not which errors are
acceptable. If you cannot type a third-party boundary more precisely, document
the module, the external contract, the runtime guard, and the test — then leave
the baseline alone.
