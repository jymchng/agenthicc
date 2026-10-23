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

The audit is a ratchet, not a waiver for unresolved errors. It counts typing
debt — missing annotations, explicit `Any`, bare `list`/`dict` annotations,
`getattr`/`hasattr` calls, `# type: ignore` comments and module-level `# mypy:`
suppressions — and holds every count at the checked-in baseline in both
directions:

- a count above its baseline is a **regression** and fails;
- a count below its baseline means the baseline is **stale** and also fails,
  so an improvement is locked in instead of leaving room to regress. Lower the
  baseline in the same change:

```bash
uv run python scripts/type_audit.py --update docs/reference/type-safety-baseline.json
```

On a pull request CI also passes `--baseline-ref origin/<target branch>`, which
fails if the baseline file *raises* any value compared with the target branch.
Raising a ratchet needs explicit owner approval: a code owner reviews the
baseline (see `.github/CODEOWNERS`) and applies the `type-baseline-increase`
label, which is the only way that check is skipped.

### Time-boxed module suppressions

A module-level `# mypy: disable-error-code=...` directive switches a check off
for a whole file, so the audit accepts one only as time-boxed debt. The line
directly above it must name an owner and an expiry date, and the audit fails
once that date has passed:

```python
# type-debt: owner=PRD-250 WS-4.2; expires=2026-12-03
# mypy: disable-error-code=override
```

The six bundled workflow runners that subclass `CodePlanRunner` but drive their
own state machine (`copy_website`, `goal_flow`, `make_agenthicc_tool`,
`make_book`, `reconstruct_site/phase_impl` and `site_imitate`) carry the only
such directives: their `run()`, `resume()` and phase signatures differ from
`CodePlanRunner`'s. PRD-250 WS-4.2 replaces that inheritance with a generic
state-machine base, which removes the directives.

For the complete local gate, run:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run mypy tests/unit/test_kernel_event_typing.py tests/unit/test_type_audit.py scripts/type_audit.py
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
| The audit reports `<metric>: regressed` | The change added typing debt | Remove the new `Any`, bare container, `getattr`, or `type: ignore`; do not raise the baseline in `docs/reference/type-safety-baseline.json` to pass |
| The audit reports `<metric>: baseline is stale` | The change removed debt, which is good | Lock it in: `uv run python scripts/type_audit.py --update docs/reference/type-safety-baseline.json` |
| The audit reports `the baseline was raised` | A pull request increased a baseline value | Remove the new debt; an unavoidable increase needs a code owner's review and the `type-baseline-increase` label |
| The audit reports `# mypy: ...` `has no time box` or `expired` | A module-level suppression lacks a current `# type-debt:` marker | Fix the module's errors and delete the directive; extending the date is an owner decision recorded in review |
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

A green `mypy` alone does not mean the ratchet passed; the audit compares the
debt counts with the checked-in baseline and fails on any difference — growth
is a regression, and a drop must be locked in with `--update`.

### The baseline is not a waiver

`type-safety-baseline.json` records where the debt stands, not which errors are
acceptable. If you cannot type a third-party boundary more precisely, document
the module, the external contract, the runtime guard, and the test — then leave
the baseline alone.
