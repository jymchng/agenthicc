# PRD-76 — Tool Capability Gate

## Background

The existing mode system (PRD-65, PRD-75) shapes agent behaviour in two ways:
1. **System-prompt patch** — prepends mode-specific instructions (e.g. "do not
   write files") to the LLM's system prompt.
2. **Schema-level filter** (`Mode.tool_filter`) — removes tool names from the
   schema before the LLM call so the model never sees blocked tools.

Both mechanisms have gaps:

- The schema-level filter in `ModeManager.apply_to_agent()` is **not wired
  into `agent_turn.py`**. The agent class is built with `@use_tools(*registry.tools)`
  — all tools, unfiltered — so `tool_filter` is a dead code path in the
  reactive runtime.
- System-prompt hints are advisory. A sufficiently confused or adversarially
  prompted model can still attempt a blocked tool call.
- Neither mechanism produces a **clear machine-readable error** for the model
  when a blocked tool is called. The model has no way to explain to the user
  why it couldn't act.
- Capability rules are maintained in a **central name-based allowlist**
  (`_PLAN_REVIEW_ALLOWED`, etc.) that drifts when tools are renamed or added.

---

## Goals

1. Every `@tool()`-decorated function declares its own capabilities at the
   definition site using `@set_metadata` — no central name-to-capability dict.
2. The `RuntimeMode` declares which capability sets are blocked (e.g. `WRITE`,
   `EXECUTE`).
3. A global `ToolHook` (`ToolCapabilityGate`) enforces mode restrictions at
   **invocation time** via `before_tool_call` — after the LLM decides to call
   the tool but before the tool executes.
4. When a tool is blocked, the model receives a structured error result
   (`{"ok": false, "error": "Tool 'write_file' requires WRITE — blocked in
   Ask mode."}`) that it can surface to the user.
5. Capability declarations travel with the tool function through renames, moves,
   and copies — no external bookkeeping required.
6. Mode changes via Shift+Tab take effect immediately on the next tool call,
   even within the same agent turn.

---

## Non-goals

- Do not remove the existing `tool_filter` / schema-level filtering from
  `Mode` — it provides a defence-in-depth "outer ring" that can be wired up
  separately. This PRD adds the "inner ring" hook-based enforcement.
- Do not change the lauren-ai runner, ToolHook interface, or `@tool()` decorator.
- Do not enforce capability rules on slash commands or agenthicc's own
  `Tool` ABC layer.

---

## Capability taxonomy

### `ToolCapability` enum

```python
class ToolCapability(str, Enum):
    READ      = "read"       # reads files/data — no persistent side effects
    WRITE     = "write"      # creates, modifies, or deletes files/data
    EXECUTE   = "execute"    # runs shell commands or arbitrary code
    GIT_READ  = "git_read"   # reads git history, diffs, status, blame
    GIT_WRITE = "git_write"  # modifies git state (add, commit, checkout, stash)
    NETWORK   = "network"    # makes outbound network calls (email, REST, etc.)
    SEARCH    = "search"     # searches content without state changes
```

`ToolCapability` inherits `str` so frozenset members serialise and compare as
plain strings throughout the system.

### Pre-built capability decorators

```python
from lauren_ai._tools import set_metadata

CAPABILITIES_KEY = "capabilities"

tool_read        = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.READ}))
tool_write       = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.WRITE}))
tool_execute     = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.EXECUTE}))
tool_git_read    = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.GIT_READ}))
tool_git_write   = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.GIT_WRITE}))
tool_network     = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.NETWORK}))
tool_search      = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.SEARCH}))

# Common combinations
tool_read_search    = set_metadata(CAPABILITIES_KEY, frozenset({READ, SEARCH}))
tool_network_read   = set_metadata(CAPABILITIES_KEY, frozenset({NETWORK, READ}))
tool_network_write  = set_metadata(CAPABILITIES_KEY, frozenset({NETWORK, WRITE}))
tool_network_search = set_metadata(CAPABILITIES_KEY, frozenset({NETWORK, SEARCH}))
```

`set_metadata(key, value)` returns a plain decorator — assigning it to a
variable creates a reusable one-liner annotation.  `@tool()` and
`@set_metadata` write to different attributes (`__lauren_ai_tool__` and
`__lauren_ai_tool_metadata__` respectively) and do not interfere regardless
of stacking order.  Conventional order is `@set_metadata` above `@tool()`:

```python
@tool_read
@tool()
async def read_file(path: str) -> dict:
    """Read a file and return its contents."""
```

---

## Annotating existing tools

### Filesystem tools (`tools/fs/agent_tools.py`)

| Tool | Decorator |
|---|---|
| `read_file` | `@tool_read` |
| `read_lines` | `@tool_read` |
| `batch_read` | `@tool_read` |
| `list_directory` | `@tool_read_search` |
| `file_exists` | `@tool_read` |
| `get_file_info` | `@tool_read` |
| `checksum_file` | `@tool_read` |
| `search_files` | `@tool_read_search` |
| `grep_files` | `@tool_read_search` |
| `grep_file` | `@tool_read_search` |
| `write_file` | `@tool_write` |
| `append_file` | `@tool_write` |
| `patch_file` | `@tool_write` |
| `apply_diff` | `@tool_write` |
| `delete_file` | `@tool_write` |
| `move_file` | `@tool_write` |
| `copy_file` | `@tool_write` |
| `make_directory` | `@tool_write` |
| `touch_file` | `@tool_write` |
| `truncate_file` | `@tool_write` |
| `batch_write` | `@tool_write` |
| `batch_delete` | `@tool_write` |
| `batch_move` | `@tool_write` |

### Execution tools (`tools/exec/__init__.py`)

| Tool | Decorator |
|---|---|
| `run_bash` | `@tool_execute` |
| `run_command` | `@tool_execute` |
| `run_python` | `@tool_execute` |
| `run_python_expr` | `@tool_execute` |
| `run_tests` | `@tool_execute` |

### Git tools (`tools/git/__init__.py`)

| Tool | Decorator |
|---|---|
| `git_status` | `@tool_git_read` |
| `git_diff` | `@tool_git_read` |
| `git_log` | `@tool_git_read` |
| `git_show` | `@tool_git_read` |
| `git_blame` | `@tool_git_read` |
| `git_grep` | `@tool_git_read` |
| `git_branch` | `@tool_git_read` |
| `git_add` | `@tool_git_write` |
| `git_commit` | `@tool_git_write` |
| `git_checkout` | `@tool_git_write` |
| `git_stash` | `@tool_git_write` |

### Outlook tools (`tools/outlook/__init__.py`)

| Tool | Decorator |
|---|---|
| `outlook_list_emails` | `@tool_network_read` |
| `outlook_read_email` | `@tool_network_read` |
| `outlook_list_folders` | `@tool_network_read` |
| `outlook_calendar_events` | `@tool_network_read` |
| `outlook_search_emails` | `@tool_network_search` |
| `outlook_send_email` | `@tool_network_write` |
| `outlook_reply_email` | `@tool_network_write` |
| `outlook_move_email` | `@tool_network_write` |
| `outlook_create_event` | `@tool_network_write` |

### Project plugin tools

Project plugin authors annotate their own tools:

```python
# .agenthicc/tools/project_tools.py
from agenthicc.tools.capabilities import tool_execute, tool_read

@tool_execute
@tool()
async def run_tests(pattern: str = "") -> dict:
    """Run the project's pytest test suite."""
    ...

@tool_read
@tool()
async def list_presets() -> dict:
    """List available password presets."""
    ...
```

Tools without `@set_metadata("capabilities", ...)` are treated as having
**no declared capabilities** and pass through the gate unconditionally
(open by default, not blocked by default).

---

## `RuntimeMode.blocked_capabilities`

`RuntimeMode` gains one new field (PRD-75 already made it a frozen dataclass):

```python
@dataclass(frozen=True)
class RuntimeMode:
    name:                 str
    badge:                str                  = "⏵⏵"
    description:          str                  = ""
    system_prompt_suffix: str                  = ""
    blocked_capabilities: frozenset[str]       = field(default_factory=frozenset)
```

### Built-in mode blocking rules

| Mode | Blocked capabilities | Rationale |
|---|---|---|
| **Auto** | `∅` | Full access |
| **Plan** | `{WRITE, GIT_WRITE, EXECUTE, NETWORK}` | Read + analyse only |
| **Ask** | `{WRITE, GIT_WRITE, EXECUTE, NETWORK}` | Clarify, no side effects |
| **Review** | `{WRITE, GIT_WRITE, EXECUTE, NETWORK}` | Inspect and comment only |
| **Safe** | `{WRITE, GIT_WRITE, EXECUTE, NETWORK}` | Most restrictive preset |
| **Debug** | `∅` | Full access + diagnostic footer |

The blocked sets are identical for Plan / Ask / Review / Safe by intent — they
all prevent the agent from causing persistent changes.  Future modes can define
finer-grained sets (e.g. allow `EXECUTE` but not `WRITE`).

---

## `ToolCapabilityGate` — global ToolHook

```python
class ToolCapabilityGate(ToolHook):
    """Enforces RuntimeMode capability restrictions at tool invocation time.

    Registered as a global hook on AgentRunnerBase so it fires for every
    tool call regardless of which @tool()-decorated function is invoked.

    Capabilities are read from ToolContext via ctx.get_metadata(CAPABILITIES_KEY),
    which checks @set_metadata annotations embedded on the tool callable.

    When a tool is blocked:
    - BeforeToolHookDecision.abort() is returned.
    - The abort result dict {"ok": False, "error": "..."} is returned to the
      model as the tool result, letting it explain the restriction to the user.
    - The tool function never executes.
    """

    def __init__(self, app_state: AppState) -> None:
        self._app_state = app_state

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        mode    = self._app_state.active_mode()
        blocked = mode.blocked_capabilities
        if not blocked:
            return BeforeToolHookDecision.proceed()

        tool_caps: frozenset[str] = ctx.get_metadata(CAPABILITIES_KEY) or frozenset()
        denied = tool_caps & blocked
        if denied:
            caps_str = ", ".join(sorted(denied))
            return BeforeToolHookDecision.abort({
                "ok":    False,
                "error": (
                    f"Tool '{ctx.tool_name}' requires {caps_str} capability, "
                    f"which is blocked in {mode.name} mode. "
                    f"Switch to Auto or Debug mode to use this tool."
                ),
            })
        return BeforeToolHookDecision.proceed()
```

### Key properties

- **Reads `app_state.active_mode()` on every call** — mode changes via
  Shift+Tab take effect on the next tool invocation, even within the same turn.
- **No-op overhead is minimal** — when `blocked_capabilities` is empty (Auto,
  Debug), returns immediately after one frozenset check.
- **Open by default** — tools without `@set_metadata("capabilities", ...)` have
  `tool_caps = frozenset()`, so `denied` is always empty and they are never
  blocked.
- **Defence in depth** — sits alongside (not replacing) the schema-level
  `tool_filter` on `Mode`. The schema filter removes tools from the LLM
  schema; the gate catches any that slip through or are called despite the
  mode prompt.

---

## Data flow

```
Decoration time:
  @tool_write                           set_metadata stores:
  @tool()                               fn.__lauren_ai_tool_metadata__ =
  async def write_file(...): ...            {"capabilities": frozenset({"write"})}

Agent turn — model calls write_file in Ask mode:
  runner._execute_single_tool()
    ├─ reads fn.__lauren_ai_tool_metadata__ → {"capabilities": frozenset({"write"})}
    ├─ builds ToolContext(metadata={"capabilities": frozenset({"write"})}, ...)
    └─ fires before_tool_call hooks:
         ToolCapabilityGate.before_tool_call(ctx)
           ├─ mode = app_state.active_mode()   → RuntimeMode("Ask", blocked={"write", ...})
           ├─ tool_caps = ctx.get_metadata("capabilities") → frozenset({"write"})
           ├─ denied = {"write"} & {"write", "git_write", "execute", "network"}
           │         = {"write"}
           └─ return BeforeToolHookDecision.abort({
                  "ok": False,
                  "error": "Tool 'write_file' requires write — blocked in Ask mode."
              })
  Tool never executes.
  Model receives: {"ok": false, "error": "Tool 'write_file' requires write — blocked in Ask mode. ..."}
```

---

## Wire-up

### `agent_turn.py`

```python
from agenthicc.tools.capability_gate import ToolCapabilityGate

# Pass app_state so the gate reads the live mode signal
_runner = _build_runner_for_agent(
    _agent_instance,
    runner._transport,
    signals=getattr(runner, "_signals", None),
    global_hooks=[ToolCapabilityGate(app_state)],
)
```

`app_state` is already threaded through `agent_turn.py` (it is passed in and
used for `conv_store = app_state.conversation`).

---

## File changes

| File | Change |
|---|---|
| `tools/capabilities.py` | **New** — `ToolCapability` enum, `CAPABILITIES_KEY`, all pre-built `tool_*` decorators |
| `tools/capability_gate.py` | **New** — `ToolCapabilityGate(ToolHook)` |
| `tools/fs/agent_tools.py` | Add `@tool_read` / `@tool_write` / `@tool_read_search` above each `@tool()` |
| `tools/exec/__init__.py` | Add `@tool_execute` above each `@tool()` |
| `tools/git/__init__.py` | Add `@tool_git_read` / `@tool_git_write` above each `@tool()` |
| `tools/outlook/__init__.py` | Add `@tool_network_*` above each `@tool()` |
| `tui/runtime/mode_manager.py` | `RuntimeMode` gains `blocked_capabilities: frozenset[str]`; `build_default_registry()` sets blocked caps per mode |
| `runners/agent_turn.py` | Pass `global_hooks=[ToolCapabilityGate(app_state)]` to runner |
| `docs/tui-architecture.md` | Document `ToolCapabilityGate` and its relationship to `RuntimeMode` |
| `knowledge/tool-system.md` | Document `@set_metadata("capabilities", ...)` pattern |

---

## Writing new tools — developer contract

Every new `@tool()`-decorated function **must** include a capability annotation:

```python
from agenthicc.tools.capabilities import tool_write, tool_read

@tool_write                    # ← required: declare what this tool does
@tool()
async def my_new_tool(path: str, content: str) -> dict:
    """Write something to path."""
    ...
```

Tools without a capability annotation are treated as unrestricted. This is
intentional (open-by-default) but CI should warn on unannotated tools via a
ruff or pytest check.

If a tool combines multiple capabilities:

```python
from lauren_ai._tools import set_metadata
from agenthicc.tools.capabilities import CAPABILITIES_KEY, ToolCapability

@set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.READ, ToolCapability.EXECUTE}))
@tool()
async def run_tests_and_read_log(pattern: str = "") -> dict:
    """Run tests then read the log file."""
    ...
```

Or define a new shorthand in `tools/capabilities.py` and use that.

---

## Acceptance criteria

- [ ] `ToolCapability` enum exists in `tools/capabilities.py` with `READ`,
      `WRITE`, `EXECUTE`, `GIT_READ`, `GIT_WRITE`, `NETWORK`, `SEARCH`.
- [ ] All pre-built `tool_*` decorators are defined in `tools/capabilities.py`.
- [ ] Smoke test: `@tool_write; @tool()` correctly stores `frozenset({"write"})`
      at `fn.__lauren_ai_tool_metadata__["capabilities"]`.
- [ ] Smoke test: `@tool(); @tool_write` (reversed order) produces the same result.
- [ ] All 24 FS tools, 5 exec tools, 11 git tools, 9 outlook tools are annotated.
- [ ] `RuntimeMode.blocked_capabilities` is a `frozenset[str]` field; all 6
      built-in modes have correct blocking rules set in `build_default_registry()`.
- [ ] `ToolCapabilityGate` is registered as a global hook in `agent_turn.py`.
- [ ] In Ask mode: calling `write_file` returns
      `{"ok": false, "error": "... blocked in Ask mode ..."}` without the tool
      executing.
- [ ] In Auto mode: calling `write_file` succeeds normally.
- [ ] Switching from Auto → Ask via Shift+Tab then calling `write_file`
      in the same turn is blocked.
- [ ] Tools without `@set_metadata("capabilities", ...)` are never blocked
      regardless of mode.
- [ ] All existing tests pass.
