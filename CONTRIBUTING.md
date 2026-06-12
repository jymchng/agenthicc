# Contributing to Agenthicc

Thank you for your interest in contributing! This document covers everything you need
to make a great contribution.

---

## Setup

1. Clone the repository and its sibling dependencies:

   ```bash
   git clone https://github.com/agenthicc/agenthicc
   cd agenthicc
   # Lauren-ai must be a sibling directory (pyproject.toml path dep)
   git clone https://github.com/lauren-framework/lauren-ai ../lauren-all/lauren-ai
   ```

2. Install all dev dependencies:

   ```bash
   uv sync --extra dev --extra tui --extra api
   ```

3. Verify the setup:

   ```bash
   uv run pytest tests/unit -q    # must pass
   uv run nox -s lint             # must pass
   ```

---

## Design philosophy

These five principles guide every decision in `agenthicc`. New code must comply with all of them.

### 1. Event-sourced, append-only state

No component writes to `AppState` directly. Every mutation is expressed as an
`Event` emitted to `EventProcessor`. The reducer is a pure function:
`(AppState, Event) → (AppState, list[Effect])`. This makes the system deterministic,
replayable, and crash-recoverable.

### 2. Tool-only agent communication

Agents interact with the world exclusively through tool calls. No agent calls another
agent's method directly. No agent reads `AppState`. No agent emits events. Communication
tools (`CommunicationTools`) translate agent intentions into kernel events. This gives
full observability and replay of every inter-agent interaction.

### 3. Parallel-first execution

Every entity (intent, workflow node, agent, tool call) runs concurrently where possible.
Use `asyncio.Semaphore` for throttling, not serialisation. Design new features to fan out,
not to block.

### 4. Hooks everywhere

Every entity (tool calls, workflow nodes, intents) must support `on_before`, `on_after`,
and `on_error` lifecycle hooks. Core logic never hardcodes policy. Extensibility comes
from hook registration, not code changes.

### 5. Pure reducers

Reducer functions must have no side effects. They receive an `AppState` and an `Event`
and return a new `AppState` plus a list of `Effect` descriptors. The `EffectExecutor`
performs the side effects. Test reducers in complete isolation with no mocks.

---

## Making changes

1. **Create a branch** from `main`.
2. **Write tests first** (or alongside the change) in `tests/unit/`, `tests/integration/`, or `tests/e2e/`.
3. **Implement** the change.
4. **Run the full check suite**:
   ```bash
   uv run nox -s lint             # ruff: 0 errors
   uv run nox -s tests_unit       # unit tests pass
   uv run nox -s tests_integration
   uv run nox -s tests_e2e
   ```
5. If you add a public symbol, **update `llms-full.txt`** and run `uv run nox -s llms_check`.
6. Open a PR.

---

## Test categories

| Category | Directory | When to use |
|---|---|---|
| Unit | `tests/unit/` | Pure function tests, no asyncio.create_task, no file I/O |
| Integration | `tests/integration/` | Real `EventProcessor` running as a task, `tmp_path` for files |
| E2E | `tests/e2e/` | Real `AgentRunnerBase` + `MockTransport`, pyte terminal tests, FastAPI TestClient WebSocket |

All tests use `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.

Mark tests with `pytestmark = pytest.mark.unit` / `.integration` / `.e2e`.

---

## Commit message convention

```
type(scope): short description

Longer body if needed.
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `perf`.

Examples:
- `feat(kernel): add WorkflowNodeRemoved reducer handler`
- `fix(runtime): pool.add() is sync — remove accidental await`
- `docs(tui): add /approve command walkthrough to TUI guide`
- `test(e2e): add Argon2 refactor 3-agent scenario`

---

## PR checklist

- [ ] All new/changed code has tests
- [ ] `nox -s lint` passes (0 ruff errors)
- [ ] `nox -s tests_unit tests_integration tests_e2e` all pass
- [ ] Any new public symbol added to `llms-full.txt` (`nox -s llms_check` passes)
- [ ] CHANGELOG.md updated under `[Unreleased]`
- [ ] docs updated if user-facing behaviour changed

---

## Definition of done

A change is complete when **all** of the following pass:

```bash
uv run nox -s lint            # ruff: 0 errors
uv run nox -s tests_unit      # unit tests: all pass
uv run nox -s tests_integration
uv run nox -s tests_e2e
uv run nox -s llms_check      # all public symbols in llms-full.txt
```

If you add a public symbol, `llms_check` will fail — add a `### SymbolName` section
to `llms-full.txt` to fix it.

---

## Key invariants

- `EventProcessor.drain()` requires the processor `run()` task to be running first —
  start it with `asyncio.create_task(processor.run())`.
- `AgentPool.add()` is synchronous — do not `await` it.
- `CommunicationTools.workflow_modify(action="add_node")` raises `ValueError` on cycle
  (does not return `{"ok": False}`).
- `render_frame_ansi()` always places the input bar on row `rows` (1-indexed ANSI) —
  pyte buffer index is `rows - 1` (0-indexed).
- Reducer functions must be stateless pure functions — no global variables, no I/O.
- `MockTransport` from `lauren_ai.testing` requires one `queue_response()` per LLM turn;
  use `_build_runner_for_agent()` to resolve tools correctly in tests.
