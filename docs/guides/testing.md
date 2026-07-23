# Testing

The test suite is split by how much runtime it exercises.

## Test layers

| Layer | Location | Typical contents |
|---|---|---|
| Unit | `tests/unit/` | Reducers, config, parsers, registries, memory algorithms, key decoding |
| Integration | `tests/integration/` | Event processor, workflows, tools, memory SQLite, cassettes |
| E2E | `tests/e2e/` | Full session paths, Rich workspace, terminal/runtime behaviour |

Pytest uses `asyncio_mode = "auto"` and a 60-second default timeout. Shared
fixtures live in `tests/conftest.py`; cassette helpers live in
`tests/conftest_cassette.py` and `agenthicc.testing`.

## Kernel tests

Reducer tests should call `root_reducer(state, event)` directly and assert the
new frozen state plus effects. Processor tests must create a task for
`processor.run()` before emitting and must drain before asserting. Always cancel
and await the processor task in teardown.

## Workflow and provider tests

Use `lauren-ai` mock/recording transports rather than real provider calls.
Queue one response for every expected LLM round-trip. Exercise:

- normal phase transitions;
- rejection and retry loops;
- parallel phases and failure handling;
- context compaction and model-window limits;
- transport retry rollback;
- journal resume and idempotent tool replay.

`agenthicc.testing` provides `SessionCassette`, mock approvals, and
`run_headless_replay()` for deterministic scenarios.

## TUI and terminal tests

Test signal and conversation mutations without a terminal. Test Rich rendering
with a captured console. Test input capability handlers with synthetic `Key`
values. Platform-specific key decoding must remain a pure function so Windows
cases can run on Linux CI. Also cover non-TTY startup and cleanup.

The removed prompt-toolkit `render_frame_ansi`/`pyte` contract is not a current
test target; new screen assertions should target `Workspace` and its actual
Rich renderables.

## Checks

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run mypy tests/unit/test_kernel_event_typing.py tests/unit/test_type_audit.py
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Public exports also need the LLM documentation check defined in `noxfile.py`:

```bash
uv run nox -s llms_check
```

The embedded checker and Nox install paths are being made reproducible as part
of PRD-138 P0.5.

## Coverage and flake control

The `coverage` Nox session targets 85%. Use coverage to identify untested
boundaries, not as a substitute for failure-mode tests. Avoid live network,
real credentials, and nondeterministic wall-clock assertions.
