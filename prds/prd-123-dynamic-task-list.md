# PRD-123 — Dynamic Task List

## Problem

When the agent works on anything non-trivial — a refactor, a bug hunt, a doc
rewrite — the user sees an opaque stream of tool calls with no sense of plan or
progress. The agent might internally sequence 8 discrete steps, but the user
sees only a wall of `git_status ✓`, `read_file ✓`, `patch_file ✓` with no
framing. There is no way to know how far along the work is, which step just
finished, or what comes next.

## Goal

Let the LLM optionally declare a task list at the start of any turn, then check
items off as it works through them. The task list is:

- **General-purpose** — available as base tools in every agent turn, regardless
  of mode or workflow. Not a code-plan concept.
- **LLM-driven** — the agent decides whether the intent warrants a checklist.
  Simple questions never get one. Complex multi-step work usually should.
- **Visible in the TUI** — each task creation and completion emits a scroll-
  buffer event. A live "Step N/M" counter appears in the status bar.
- **Lightweight** — two new tools. No state machine, no phase coupling, no
  required call sequence.

## User-visible behaviour

```
❯ refactor the auth module to use JWT

  ○ 1. Read current auth implementation
  ○ 2. Write JWT helper (encode / decode / verify)
  ○ 3. Replace session middleware with JWT middleware
  ○ 4. Update login and logout handlers
  ○ 5. Run tests and fix failures

  ⎿ read_file('auth/session.py')  ✓  12ms
  ✓ [1/5] Read current auth implementation

  ⎿ write_file('auth/jwt_helper.py')  ✓  8ms
  ✓ [2/5] Write JWT helper

  … (tool calls)
  ✓ [3/5] Replace session middleware
  ✓ [4/5] Update login and logout handlers
  ✓ [5/5] Run tests and fix failures

  ✾ Worked for 2 mins 14 seconds
```

## Two new base tools

### `create_task_list(tasks: list[str]) -> dict`

Called once near the start of a turn when the agent decides to work through
multiple discrete steps. Calling it a second time in the same turn replaces the
previous list.

- Creates `TaskItem` objects with auto-assigned IDs (`"task-1"`, `"task-2"`, …)
  and initial `status = "pending"`.
- Stores the list on `ConversationStore.task_list` signal.
- Emits a `"task_list_created"` scroll-buffer event that renders the full list
  as `○ N. title` lines.
- Returns `{"ok": True, "task_ids": ["task-1", "task-2", ...]}` so the LLM
  knows which IDs to use in subsequent calls.

### `complete_task(task_id: str, note: str = "") -> dict`

Called after completing each step.

- Sets `TaskItem.status = "done"`, stores optional `note`.
- Emits a `"task_step_done"` scroll-buffer event that renders
  `✓ [N/M] title` with dim note if present.
- Updates `ConversationStore.task_list` signal so the status counter refreshes.
- Returns `{"ok": True, "completed": N, "remaining": M - N}`.

No `fail_task` tool in v1 — the agent can call `complete_task` with a note
like `"failed: import error"` if needed. A typed `fail_task` is a follow-on.

## Architecture

### New dataclass — `TaskItem`

```python
@dataclass
class TaskItem:
    id:     str
    title:  str
    status: Literal["pending", "done"] = "pending"
    note:   str = ""
```

Lives in `tui/conversation_store.py` alongside `ConversationTurn`.

### `ConversationStore` changes

```python
self.task_list: Signal[list[TaskItem] | None] = Signal(None)
```

- `begin_turn()` resets it to `None` (task list is per-turn, not persistent).
- `close_turn()` leaves it as-is so the final state remains visible in the
  scroll buffer after the turn ends.

### New `EventKind` values

```python
EventKind = Literal[
    ...,
    "task_list_created",   # full list rendered at creation
    "task_step_done",      # one item checked off
]
```

### Tool implementation — `src/agenthicc/tools/task_list.py`

```python
def make_task_list_tools(conv_store: ConversationStore) -> list[object]:
    """Return the two task-list tools wired to conv_store."""
    ...
```

`create_task_list` and `complete_task` are `@tool()`-decorated functions
closed over `conv_store`. They mutate `conv_store.task_list` and call
`conv_store.append_event(...)`.

### Scroll-buffer renderers — `appender.py`

**`@register_renderer("task_list_created")`**

```
  ○ 1. Read current auth implementation
  ○ 2. Write JWT helper
  ○ 3. Replace session middleware
  …
```

**`@register_renderer("task_step_done")`**

```
  ✓ [2/5] Write JWT helper   dim note if present
```

### Status bar — `components.py`

When `conv.task_list()` is not `None` and the agent is running, line 1 shows:

```
✿ Thinking │ Step 2/5 │ ↑ 1,200 ↓ 340
```

The "Step N/M" segment reads `done_count` / `len(task_list)` from the signal.
It disappears when all tasks are done or when the turn ends.

### Wiring into base tools — `agent_turn.py`

`AgentTurnRunner._build_agent()` already calls `_make_session_tools()` and
`_base_tools()`. Add:

```python
from agenthicc.tools.task_list import make_task_list_tools

tools = [
    *existing_tools,
    *make_task_list_tools(ctx.conv_store),   # always injected
]
```

`conv_store` is already on `AgentTurnContext` (`ctx.conv_store`). No new
context fields needed.

## What this is NOT

- Not a replacement for `finalize_plan` / `mark_execute_complete` in code-plan.
  Those tools remain unchanged and serve a different purpose (phase gating).
- Not persisted across turns. The task list lives for one turn and is rendered
  as a permanent scroll-buffer record when the turn ends.
- Not required. The LLM may choose not to call `create_task_list` on simple
  turns and the system functions identically to today.

## Acceptance criteria

| # | Criterion |
|---|---|
| 123.1 | `create_task_list(["A", "B", "C"])` renders a 3-item pending list in the scroll buffer |
| 123.2 | `complete_task("task-2")` renders `✓ [1/3] B` in the scroll buffer |
| 123.3 | `ConversationStore.task_list` signal updates on every `complete_task` call |
| 123.4 | Status bar shows `Step N/M` while agent is running and task list is non-empty |
| 123.5 | `begin_turn()` resets `task_list` to `None` (no bleed between turns) |
| 123.6 | A second `create_task_list` call in the same turn replaces the list |
| 123.7 | Turns with no `create_task_list` call behave identically to today |
| 123.8 | Tools are available in every mode (Auto, Plan, Review, etc.) and in workflows |

## Files to create / modify

| File | Status | Change |
|---|---|---|
| `src/agenthicc/tools/task_list.py` | New | `TaskItem`, `make_task_list_tools()`, `create_task_list`, `complete_task` |
| `src/agenthicc/tui/conversation_store.py` | Modify | Add `TaskItem`; add `task_list: Signal[list[TaskItem] \| None]`; reset in `begin_turn()` |
| `src/agenthicc/tui/workspace/appender.py` | Modify | `@register_renderer` for `"task_list_created"` and `"task_step_done"` |
| `src/agenthicc/tui/workspace/components.py` | Modify | `StatusComponent` shows `Step N/M` when task list is active |
| `src/agenthicc/runners/agent_turn.py` | Modify | Inject `make_task_list_tools(ctx.conv_store)` into every turn |
| `tests/unit/test_task_list_tools.py` | New | Unit tests for both tools, signal updates, scroll events |
