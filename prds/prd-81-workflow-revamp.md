# PRD-81 — Workflow Revamp: Phase-Based Agentic Pipelines

## Background

agenthicc currently maps every user message directly to a single
`_run_agent_turn()` call.  The kernel already models `Intent`,
`Workflow`, `WorkflowNode`, and `Task` as event-sourced state, but
those types are never used by the TUI runner — the DAGExecutor and
IntentPlanner are completely disconnected from the live session.

The result is a powerful kernel with no orchestration, and a TUI that
is operationally equivalent to a single-agent chatbot regardless of
which mode is active.

This PRD bridges the gap: a **phase-based workflow engine** that
executes multiple, purpose-specific agent invocations in sequence (or
with feedback loops), mode-bound by default, and extensible via
user/project plugins.

---

## Goals

- Define a `WorkflowDefinition` with named `Phase`s, `Transition`
  rules, and `PhaseRole`s — the high-level authoring surface.
- Introduce a `WorkflowRunner` that executes a `WorkflowDefinition`
  by calling `_run_agent_turn` once per phase with phase-appropriate
  configuration (system prompt, tool access, max turns).
- Thread a `WorkflowContext` between phases so each phase agent sees
  the outputs of all prior phases.
- Bind workflows to `RuntimeMode`s so the active mode determines
  which workflow runs.
- Support **dynamic modification**: a phase agent can call a tool to
  add or skip phases at runtime.
- Provide first-class plugin loading from `~/.agenthicc/workflows/`
  (user-global) and `.agenthicc/workflows/` (project-local) in both
  TOML and Python formats with the same trust/conflict model as
  command plugins.
- Surface workflow progress in the TUI (current phase, phase count,
  workflow name in footer).

---

## Non-Goals

- Do not change the kernel's `Workflow` / `WorkflowNode` / `Task`
  types — they remain for intra-phase parallel sub-tasks (existing
  DAGExecutor).  This PRD adds a *new*, *higher-level* abstraction
  called `WorkflowDefinition` above the kernel DAG.
- Do not implement cross-agent shared memory — each phase agent gets
  its own `ShortTermMemory`; context flows only through the structured
  `WorkflowContext` injected into the system prompt.
- Do not change how `_run_agent_turn` works internally — the
  `WorkflowRunner` calls it as a black box, just with different
  configuration per phase.
- Do not integrate with lauren-ai's `TeamRunner` — it provides
  insufficient DAG control and has no event-sourcing.
- Headless mode is exempt from the workflow engine in v1 — it
  continues to use single-turn execution.

---

## Terminology

| Term | Meaning |
|---|---|
| `WorkflowDefinition` | Named, versioned, composable workflow template (TOML or Python). |
| `Phase` | One step in a `WorkflowDefinition`, mapped to a single agent invocation. |
| `PhaseRole` | Semantic role of a phase agent — Planner, Executor, Reviewer, etc. |
| `Transition` | Rule that determines which phase runs next given a phase's output. |
| `WorkflowRun` | Live execution of a `WorkflowDefinition`, tracking current phase and accumulated outputs. |
| `WorkflowContext` | Structured document threaded through all phases, carrying the original intent and each prior phase's output. |
| `WorkflowRunner` | Async driver that loops over phases, calls `_run_agent_turn`, and applies transitions. |
| `WorkflowRegistry` | In-process registry mapping workflow names to definitions (builtin + loaded plugins). |
| `PhaseOutput` | Structured or free-text result produced by a phase, stored in `WorkflowContext`. |

---

## Core Data Model

### `PhaseRole`

```python
class PhaseRole(str, Enum):
    PLANNER   = "planner"     # read-only; produces a structured plan
    EXECUTOR  = "executor"    # full tool access; implements
    REVIEWER  = "reviewer"    # read-only; critiques executor output
    EXPLORER  = "explorer"    # read-only; discovers context
    VERIFIER  = "verifier"    # read-only; checks correctness post-execute
    HUMAN     = "human"       # pauses execution for human review via ApprovalOverlay
    CUSTOM    = "custom"      # plugin-defined; role config supplied inline
    AUTO      = "auto"        # no phase shaping; identical to current single-turn
```

Each built-in role carries a **role template** — a system-prompt
fragment injected before the user's intent and any workflow context:

| Role | Tool access | Max turns | System fragment (summary) |
|---|---|---|---|
| `planner` | read-only | 8 | "You are a careful planner. Do not execute anything. Produce a numbered step-by-step plan." |
| `executor` | full | 30 | "You are an executor. Follow the approved plan step by step. Use tools to implement each step." |
| `reviewer` | read-only | 6 | "You are a code reviewer. Inspect the changes made by the executor and identify issues." |
| `explorer` | read-only | 8 | "You are a research agent. Explore the codebase and environment. Report findings without making changes." |
| `verifier` | read-only | 6 | "You are a verifier. Check that the implementation matches the plan and passes correctness criteria." |
| `human` | n/a | n/a | Pauses the `WorkflowRunner`; shows `ApprovalOverlay` with the prior phase output; resumes on y/n. |
| `auto` | inherit from mode | inherit | No shaping; current single-turn behaviour. |

**Tool access mapping** to `RuntimeMode.blocked_capabilities`:

| Tool access | `blocked_capabilities` |
|---|---|
| `full` | `∅` |
| `read_only` | `{WRITE, GIT_WRITE, EXECUTE, NETWORK}` |
| `none` | everything |
| `inherit` | taken from the session's active `RuntimeMode` |

---

### `PhaseSpec`

```python
@dataclass(frozen=True)
class PhaseSpec:
    name:                    str
    role:                    PhaseRole         = PhaseRole.AUTO
    system_prompt_override:  str               = ""     # replaces role template if non-empty
    tool_access:             str               = "inherit"
    max_turns:               int               = 20
    output_schema:           str | None        = None   # "plan", "review_result", "free_text" (default)
    next:                    str | None        = None   # phase name, or None = done
    on_reject:               str | None        = None   # reviewer says rejected → go here
    on_error:                str | None        = None   # agent errors → go here (default: fail)
    max_iterations:          int               = 3      # max times this phase can run in one WorkflowRun
    parallel_with:           tuple[str, ...]   = ()     # sibling phase names to run concurrently
```

`parallel_with` enables the Explore pattern — multiple explorer
phases scanning different parts of the codebase concurrently, all
feeding into the subsequent Plan phase:

```toml
[[workflow.phases]]
name = "explore_frontend"
role = "explorer"
parallel_with = ["explore_backend"]
next = "plan"

[[workflow.phases]]
name = "explore_backend"
role = "explorer"
parallel_with = ["explore_frontend"]
next = "plan"
```

Both phases run via `asyncio.gather`; the Plan phase starts only
when both complete.

---

### `WorkflowDefinition`

```python
@dataclass(frozen=True)
class WorkflowDefinition:
    name:          str
    description:   str
    phases:        tuple[PhaseSpec, ...]
    mode_bindings: tuple[str, ...]  # RuntimeMode names that use this workflow by default
    source:        Literal["builtin", "user", "project"]
    path:          Path | None      # None for builtins

    def get_phase(self, name: str) -> PhaseSpec | None: ...
    def first_phase(self) -> PhaseSpec: ...
```

---

### `WorkflowContext`

```python
@dataclass
class WorkflowContext:
    intent:        str                         # original user message
    run_id:        str                         # WorkflowRun UUID
    workflow_name: str
    phase_outputs: dict[str, PhaseOutput]      # keyed by phase name

    def as_system_block(self) -> str:
        """Return a markdown block injected into each phase agent's system prompt."""
```

`as_system_block()` produces:

```markdown
[WORKFLOW CONTEXT]
Original intent: {intent}

Completed phases:
- explore (explorer): {output_summary[:500]}
- plan (planner): {output_summary[:500]}
```

Each `PhaseOutput` is truncated to 500 characters in the context
block to control prompt size.  The full output is available in
`WorkflowContext.phase_outputs[name].full_text` if a phase needs it.

---

### `PhaseOutput`

```python
@dataclass
class PhaseOutput:
    phase_name:  str
    role:        PhaseRole
    full_text:   str            # raw LLM output
    structured:  dict | None    # parsed JSON if output_schema was set
    approved:    bool | None    # set by human/reviewer phase; None = not applicable
    metadata:    dict           # arbitrary key-value pairs from the phase agent
    agent_id:    str
    duration_s:  float
```

---

### `WorkflowRun`

```python
@dataclass
class WorkflowRun:
    run_id:           str
    workflow_name:    str
    intent:           str
    current_phase:    str | None     # None = done
    phase_history:    list[PhaseRunRecord]
    context:          WorkflowContext
    status:           Literal["running", "complete", "failed"]
    created_at:       float
```

Stored on `AppState.workflow_run: Signal[WorkflowRun | None]` — the
TUI subscribes to this for progress rendering.

---

## `WorkflowRunner` — Execution Engine

```python
class WorkflowRunner:
    def __init__(
        self,
        definition:    WorkflowDefinition,
        conv_store:    ConversationStore,
        app_state:     AppState,
        processor:     EventProcessor,
        agent_runner:  Any,           # lauren-ai runner (transport + signals)
        session_mem:   ShortTermMemory,
        approval_svc:  ApprovalService,
        cfg:           AgenthiccConfig,
        skills:        dict,
        plugin_tools:  list,
        mcp_registry:  Any | None,
        mention_cache: MentionCache,
    ) -> None:

    async def run(self, intent: str) -> None:
        """Execute the full workflow for the given intent text."""
```

### Execution Loop

```
WorkflowRunner.run(intent)
│
├── Create WorkflowRun, set app_state.workflow_run
│
├── phase_name = definition.first_phase().name
│   iteration_counts: dict[str, int] = {}
│
└── while phase_name is not None:
    │
    ├── spec = definition.get_phase(phase_name)
    │
    ├── Guard: iteration_counts[phase_name] >= spec.max_iterations → fail
    │
    ├── if spec.role == PhaseRole.HUMAN:
    │   │   Build ApprovalRequest with prior phase output as args
    │   │   await approval_svc.request_approval(req)
    │   │   if approved → transition to spec.next
    │   │   if denied  → transition to spec.on_reject (or fail)
    │   └── continue
    │
    ├── if spec.parallel_with:
    │   │   peers = [spec] + [definition.get_phase(n) for n in spec.parallel_with]
    │   │   outputs = await asyncio.gather(*[_run_phase(p) for p in peers])
    │   │   for each output → store in WorkflowContext
    │   └── phase_name = spec.next   (all parallel peers must agree on next)
    │
    ├── else:
    │   │   output = await _run_phase(spec, intent, context)
    │   │   store output in WorkflowContext
    │   └── phase_name = _determine_transition(spec, output)
    │
    ├── Update app_state.workflow_run with new current_phase
    │
    └── Emit WorkflowPhaseCompleted kernel event
```

### `_run_phase(spec, intent, context) → PhaseOutput`

```python
async def _run_phase(
    self,
    spec: PhaseSpec,
    intent: str,
    context: WorkflowContext,
) -> PhaseOutput:
    phase_text = self._build_phase_prompt(spec, intent, context)
    # Override tool access
    phase_tool_access = spec.tool_access
    # Override blocked capabilities on app_state for this phase
    orig_mode = self._app_state.active_mode()
    phase_mode = _mode_with_tool_access(orig_mode, phase_tool_access)
    self._app_state.active_mode.set(phase_mode)

    try:
        output_buf: list[str] = []
        await _run_agent_turn(
            phase_text,
            self._agent_runner,
            None, None,
            self._processor,
            session_memory=ShortTermMemory(max_tokens=16_000),
            max_agent_turns=spec.max_turns,
            conv_store=self._conv_store,
            app_state=self._app_state,
            exec_cfg=self._cfg.execution,
            skills=self._skills,
            mention_cache=self._mention_cache,
            project_plugin_tools=self._plugin_tools,
            mcp_registry=self._mcp_registry,
            active_agent=spec.role.value,
            approval_svc=self._approval_svc,
            output_collector=output_buf,    # NEW param in _run_agent_turn
        )
    finally:
        # Restore original mode
        self._app_state.active_mode.set(orig_mode)

    full_text = "".join(output_buf)
    structured = _parse_output_schema(full_text, spec.output_schema)
    return PhaseOutput(
        phase_name=spec.name,
        role=spec.role,
        full_text=full_text,
        structured=structured,
        approved=None,
        metadata={},
        agent_id=...,
        duration_s=...,
    )
```

**Key design point**: `_mode_with_tool_access()` creates a NEW
temporary `RuntimeMode` with the phase's tool access merged over the
session mode's settings, and writes it to `app_state.active_mode` for
the duration of the phase.  `ToolCapabilityGate` and `ApprovalGate`
read `app_state.active_mode()` on every tool call, so the phase's
tool restrictions take effect immediately without changing the runner.

After the phase completes, the original mode is restored.

### `_determine_transition(spec, output) → str | None`

```python
def _determine_transition(
    self,
    spec: PhaseSpec,
    output: PhaseOutput,
) -> str | None:
    # Dynamic override: phase agent can set metadata["__next_phase__"]
    if "__next_phase__" in output.metadata:
        return output.metadata["__next_phase__"] or None

    # Reviewer/human approval
    if output.approved is False and spec.on_reject:
        return spec.on_reject

    # Normal next
    return spec.next   # None = done
```

---

## Phase Context Injection

`WorkflowRunner._build_phase_prompt(spec, intent, context)` produces:

```
{context.as_system_block()}

{spec_system_prompt_if_not_override or role_template}

---
Task: {intent}
```

This is passed as the `text` argument to `_run_agent_turn`.  The
agent's system prompt (built inside `_run_agent_turn`) receives an
additional suffix via the mode's `system_prompt_suffix`.

For the Planner role, the system includes:
> "Respond with a numbered plan. Wrap the final plan in a `<plan>` XML tag so downstream phases can extract it."

`_parse_output_schema("plan")` looks for the `<plan>...</plan>` block.

---

## Dynamic Modification Tool

A tool available to all phase agents:

```python
@tool_write
@tool()
async def workflow_set_next(next_phase: str | None) -> dict:
    """Override which phase runs next.

    Set next_phase to a phase name to skip ahead or loop back.
    Set next_phase to null/empty to terminate the workflow immediately.
    """
```

This tool writes `__next_phase__` into the phase's `output.metadata`
via the `output_collector` mechanism.  It does NOT modify the
`WorkflowDefinition` — it only influences the single transition
decision for the current run.

For adding entirely new phases (plugin authors): Python
`WorkflowPlugin.determine_transition()` may return any phase name,
including ones added dynamically by the plugin's `build()` method.

---

## Mode–Workflow Binding

`RuntimeMode` gains one new field:

```python
@dataclass(frozen=True)
class RuntimeMode:
    ...
    workflow_name: str | None = None   # NEW — None = single-turn ("auto")
```

`build_default_registry()` assigns:

| Mode | `workflow_name` |
|---|---|
| Auto | `None` (direct single-turn) |
| Debug | `None` |
| Plan | `"plan_only"` |
| Ask | `None` |
| Review | `"review_only"` |
| Safe | `None` |
| Guard | `None` (approval gate handles restrictions) |

Plugin workflows can declare `mode_bindings = ["MyMode"]` in their
definition; `build_default_registry()` will assign the workflow_name
to any matching RuntimeMode found in the registry.

### Session dispatch

In `tui_session.py`, `_run_turn()` checks `active_mode.workflow_name`:

```python
async def _run_turn(text: str) -> None:
    approval_svc.reset_turn_memory()
    mode = app_state.active_mode()
    wf_name = mode.workflow_name

    if wf_name is None:
        # Current single-turn path — unchanged
        await _run_agent_turn(text, ..., approval_svc=approval_svc)
    else:
        defn = workflow_registry.get(wf_name)
        if defn is None:
            conv_store.append_event("error", {"message": f"Unknown workflow: {wf_name}"})
            return
        runner = WorkflowRunner(defn, conv_store, app_state, processor, ...)
        await runner.run(text)
```

---

## Built-in Workflows

### `auto` (virtual)

Not a real `WorkflowDefinition` — the `None` sentinel.  Preserves
current single-turn behaviour identically.

---

### `plan_only`

```toml
[workflow]
name        = "plan_only"
description = "Read-only planning pass — no execution."

[[workflow.phases]]
name        = "plan"
role        = "planner"
tool_access = "read_only"
max_turns   = 8
next        = null
```

Bound to the **Plan** and **Review** modes.  The agent produces a plan
but never executes it.  Useful for reviewing intent before committing.

---

### `review_only`

```toml
[workflow]
name        = "review_only"
description = "Read-only review pass — inspect and comment."

[[workflow.phases]]
name        = "review"
role        = "reviewer"
tool_access = "read_only"
max_turns   = 8
next        = null
```

Bound to the **Review** mode.  System prompt instructs the agent to
inspect the working tree and produce a structured critique.

---

### `supervised`

```toml
[workflow]
name        = "supervised"
description = "Plan → Human Review → Execute"

[[workflow.phases]]
name        = "plan"
role        = "planner"
tool_access = "read_only"
max_turns   = 8
output_schema = "plan"
next        = "human_review"

[[workflow.phases]]
name        = "human_review"
role        = "human"
next        = "execute"
on_reject   = "plan"
max_iterations = 5

[[workflow.phases]]
name        = "execute"
role        = "executor"
tool_access = "full"
max_turns   = 30
next        = null
```

The `human_review` phase shows the plan in `ApprovalOverlay`:
- **y / Enter** → proceeds to `execute`
- **n / Esc** → loops back to `plan` (the planner is re-run with
  a note that the previous plan was rejected)
- `on_reject` iteration count is tracked; after `max_iterations`
  rejections the workflow fails with an explanatory message

---

### `architect`

```toml
[workflow]
name        = "architect"
description = "Explore → Plan → Execute → Verify"

[[workflow.phases]]
name        = "explore"
role        = "explorer"
tool_access = "read_only"
max_turns   = 10
next        = "plan"

[[workflow.phases]]
name        = "plan"
role        = "planner"
tool_access = "read_only"
max_turns   = 8
output_schema = "plan"
next        = "execute"

[[workflow.phases]]
name        = "execute"
role        = "executor"
tool_access = "full"
max_turns   = 40
next        = "verify"

[[workflow.phases]]
name        = "verify"
role        = "verifier"
tool_access = "read_only"
max_turns   = 8
on_reject   = "execute"
max_iterations = 2
next        = null
```

No default mode binding — users invoke it via `/workflow architect`
or by binding it to a custom mode plugin.

---

## Plugin System

### Discovery Paths and Precedence

```
builtin (hardcoded, lowest)
  ↓
user-global:  ~/.agenthicc/workflows/*.toml
              ~/.agenthicc/workflows/*.py
  ↓
project-local: .agenthicc/workflows/*.toml     (highest precedence)
               .agenthicc/workflows/*.py
```

Same-name override: project silently wins over user-global; user-global
silently wins over builtin.  A project workflow overriding a user
workflow emits a startup warning (matching PRD-79's `strict_cli_shadow`
pattern):

```
⚠  .agenthicc/workflows/supervised.toml (project) shadows
   ~/.agenthicc/workflows/supervised.toml (user)
```

### TOML Workflow Format

`~/.agenthicc/workflows/my_workflow.toml`:

```toml
[workflow]
name          = "my_custom"
description   = "My custom three-phase workflow"
mode_bindings = []     # empty = no automatic mode binding

[[workflow.phases]]
name        = "explore"
role        = "explorer"
tool_access = "read_only"
max_turns   = 8
next        = "implement"

[[workflow.phases]]
name        = "implement"
role        = "executor"
tool_access = "full"
max_turns   = 30
next        = "verify"

[[workflow.phases]]
name        = "verify"
role        = "verifier"
tool_access = "read_only"
max_turns   = 6
on_reject   = "implement"
max_iterations = 2
next        = null
```

Optional fields per phase that override the role template:

```toml
[[workflow.phases]]
name                    = "custom_phase"
role                    = "custom"
system_prompt_override  = "You are a specialized agent that..."
tool_access             = "read_only"
max_turns               = 5
next                    = null
```

### Python Workflow Plugin Format

`~/.agenthicc/workflows/my_workflow.py`:

```python
from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec, PhaseRole, PhaseOutput

class MyWorkflow(WorkflowPlugin):
    name          = "my_custom"
    description   = "Fully programmatic workflow"
    mode_bindings = []

    phases = [
        PhaseSpec(name="explore",   role=PhaseRole.EXPLORER,  max_turns=8,  next="plan"),
        PhaseSpec(name="plan",      role=PhaseRole.PLANNER,   max_turns=8,  next="execute",
                  output_schema="plan"),
        PhaseSpec(name="execute",   role=PhaseRole.EXECUTOR,  max_turns=30, next="verify"),
        PhaseSpec(name="verify",    role=PhaseRole.VERIFIER,  max_turns=6,  on_reject="execute",
                  max_iterations=2, next=None),
    ]

    # Optional: override phase prompts dynamically
    def build_phase_prompt(self, spec: PhaseSpec, intent: str, ctx: "WorkflowContext") -> str:
        if spec.name == "plan":
            return f"Focus specifically on the {intent} task. {ctx.as_system_block()}"
        return super().build_phase_prompt(spec, intent, ctx)

    # Optional: override transition logic
    def determine_transition(
        self, spec: PhaseSpec, output: PhaseOutput, ctx: "WorkflowContext"
    ) -> str | None:
        if spec.name == "verify" and output.structured and output.structured.get("critical_issues"):
            return "plan"   # full re-plan if verifier finds critical issues
        return spec.next
```

`WorkflowPlugin` is an ABC with sensible defaults for all methods.

### `WorkflowRegistry`

```python
class WorkflowRegistry:
    def register(self, defn: WorkflowDefinition) -> None: ...
    def get(self, name: str) -> WorkflowDefinition | None: ...
    def all(self) -> list[WorkflowDefinition]: ...

def build_workflow_registry(
    project_dir: Path = Path(".agenthicc"),
    user_dir:    Path = Path.home() / ".agenthicc",
) -> WorkflowRegistry:
    """Discover and load all workflows: builtin → user → project."""
```

Loading steps:
1. Register all builtin workflows (TOML files embedded in the package).
2. Scan `user_dir/workflows/` — load `*.toml` and `*.py` via the same
   import mechanism as `plugins/discovery.py` (`importlib.util`).
3. Scan `project_dir/workflows/` — same, with same-name override.
4. For Python plugins, instantiate the class, call `.to_definition()`.
5. Warn on shadow conflicts per the PRD-79 model.

---

## `/workflow` Slash Command

```
/workflow [list | <name> | status | cancel]
```

| Subcommand | Effect |
|---|---|
| `/workflow list` | Prints a table of all registered workflows |
| `/workflow <name>` | Binds the named workflow to the current turn (one-shot override, does not change the mode's default) |
| `/workflow status` | Shows progress of the currently running workflow |
| `/workflow cancel` | Cancels the current workflow run after the active phase completes |

---

## `_run_agent_turn` Changes

One new parameter:

```python
async def _run_agent_turn(
    ...
    output_collector: list[str] | None = None,   # NEW
) -> None:
```

When `output_collector` is non-None, each LLM text chunk is appended
to it in addition to being fed to `conv_store`.  `WorkflowRunner`
passes its `output_buf` here to capture the phase's full output.

---

## Kernel Events

Three new event types:

| Event | Payload | Reducer action |
|---|---|---|
| `WorkflowRunStarted` | `run_id, workflow_name, intent, phase_count` | No kernel state change; consumed by TUI subscriber |
| `WorkflowPhaseCompleted` | `run_id, phase_name, role, output_len, approved` | No kernel state change; consumed by TUI subscriber |
| `WorkflowRunCompleted` | `run_id, phases_run, status` | No kernel state change; consumed by TUI subscriber |

These events are emitted by `WorkflowRunner` via the existing
`EventProcessor`.  They ride the existing JSONL log for replay/audit
without requiring reducer changes.

---

## AppState Changes

```python
class AppState:
    ...
    workflow_run: Signal[WorkflowRun | None] = Signal(None)   # NEW
```

`WorkflowRunner.run()` writes this signal at every phase transition.
`Workspace.start()` subscribes it to `_redraw`.

---

## `RuntimeMode` Changes

```python
@dataclass(frozen=True)
class RuntimeMode:
    ...
    workflow_name: str | None = None   # NEW
```

---

## TUI Integration

### Status bar

`StatusComponent.render()` adds a phase badge when a workflow is running:

```
✿ Thinking │ 12s ↑3k ↓1k │ patch_file │ Phase 2/4 Execute
```

### Footer

`FooterComponent.render()` shows the active workflow name:

```
⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵
Workflow: architect  │  Phase 2/4: execute
```

When no workflow is active (`workflow_run() is None`), the second
footer line is omitted.

### Human Review Phase

When a phase with `role = PhaseRole.HUMAN` is reached, the
`WorkflowRunner` builds an `ApprovalRequest` where:
- `tool_name` is `f"Review: {phase_name}"`
- `tool_input` carries `{"plan": prior_phase_output[:2000]}`
- `capabilities` is an empty frozenset (no tool caps involved)

The existing `ApprovalService` + `ApprovalOverlay` handle the
UX without changes:
- **y** / **Enter** → approved → workflow continues to `spec.next`
- **n** / **Esc** → rejected → workflow transitions to `spec.on_reject`

---

## lauren-ai Integration Analysis

### What we reuse as-is

| Component | Reuse |
|---|---|
| `AgentRunnerBase.run_stream()` | Each phase calls it once via `_run_agent_turn()` |
| `ShortTermMemory` | Fresh instance per phase (phases do not share LLM memory) |
| `SignalBus` | Signals (`ToolCallStarted`, etc.) fire per phase, feed TUI as before |
| `SubagentTool` | Available as a tool within Executor phases for spawning sub-agents |

### What we do NOT use

| Component | Reason |
|---|---|
| `TeamRunner` (coordinator) | LLM-driven routing is non-deterministic; our phase graph has explicit transition rules. |
| `TeamRunner` (collaborate) | Sequential-worker model conflates workflow topology with LLM invocations. |
| lauren-ai's `requires_confirmation` / `approve_tool()` | PRD-78 already built HITL at the `ToolHook` layer. |

### Parallel phase execution (explorer pattern)

`asyncio.gather` runs multiple `_run_phase()` calls.  Each call
creates a separate `ShortTermMemory()` and a separate `AgentRunnerBase`
instance (via `_run_agent_turn`).  They run concurrently but do not
share state.  All phase outputs are collected before the next phase
starts — a barrier between the parallel group and its successor.

This is structurally equivalent to `SubagentPool` from lauren-ai but
without the overhead of `SubagentTool` (no JSON schema translation,
no brief compilation).

### Signal forwarding

Each phase's `_run_agent_turn` call fires `ToolCallStarted` /
`ToolCallComplete` / `ModelCallComplete` signals as before.  The
TUI receives and renders them per-phase — no changes to the signal
flow.

### HandsOffOrchestrator

lauren-ai's `HandsOffOrchestrator` (internally named `TeamRunner` with
`mode="coordinator"`) lets an LLM route tasks to named worker agents.
It does NOT support:
- Structured phase transitions with explicit on_reject / on_error paths
- Tool-access restrictions per phase
- Event-sourced audit log
- Plugin-defined phase graphs
- Parallel phases with barrier synchronisation

The `WorkflowRunner` supersedes it entirely for agenthicc's use cases.

---

## File Changes

| File | Change |
|---|---|
| `workflow/plugin.py` | **New** — `WorkflowPlugin` ABC, `PhaseSpec`, `PhaseRole`, `WorkflowDefinition`, `WorkflowContext`, `PhaseOutput`, `WorkflowRun` |
| `workflow/registry.py` | **New** — `WorkflowRegistry`, `build_workflow_registry()`, shadow-conflict detection |
| `workflow/loader.py` | **New** — TOML parser + Python plugin importer for `*.toml` / `*.py` workflow files |
| `workflow/runner.py` | **New** — `WorkflowRunner`, `_run_phase()`, `_determine_transition()`, `_build_phase_prompt()`, `_mode_with_tool_access()` |
| `workflow/builtins/` | **New** dir — `auto.toml`, `plan_only.toml`, `review_only.toml`, `supervised.toml`, `architect.toml` |
| `workflow/__init__.py` | Re-export new symbols |
| `tools/workflow_tools.py` | **New** — `workflow_set_next` tool (dynamic phase override) |
| `tui/runtime/mode_manager.py` | Add `workflow_name: str | None` to `RuntimeMode`; assign in `build_default_registry()` |
| `tui/conversation_store.py` | Add `workflow_run: Signal[WorkflowRun | None]` to `AppState` |
| `tui/workspace/components.py` | `StatusComponent` shows phase badge; `FooterComponent` shows workflow name + phase |
| `tui/workspace/workspace.py` | Subscribe `workflow_run` to `_redraw` |
| `runners/tui_session.py` | Build `WorkflowRegistry`; dispatch via `WorkflowRunner` when `mode.workflow_name` is set; add `/workflow` command |
| `runners/agent_turn.py` | Add `output_collector: list[str] | None` param |
| `commands/builtins.py` | Register `/workflow` command with subcommands |
| `workflow/dag.py` | No changes |
| `workflow/executor.py` | No changes |
| `workflow/intent.py` | No changes |
| `kernel/` | No changes |

---

## Acceptance Criteria

### Core engine
- [ ] `WorkflowRunner.run()` executes each phase in order, advancing
      through transitions until `next = null`.
- [ ] Each phase runs a fresh `ShortTermMemory`; LLM turns from prior
      phases do NOT appear in the next phase's conversation history.
- [ ] `WorkflowContext.as_system_block()` is injected into every
      phase's agent prompt containing the original intent and all
      prior phase outputs (truncated to 500 chars each).
- [ ] `PhaseRole.HUMAN` pauses the runner and shows `ApprovalOverlay`
      with the prior phase's output.  `y` → `next`, `n` → `on_reject`.
- [ ] `_mode_with_tool_access("read_only")` correctly sets
      `blocked_capabilities = {WRITE, GIT_WRITE, EXECUTE, NETWORK}`;
      a tool call in a read-only phase returns the capability-blocked
      error without prompting the user.
- [ ] `workflow_set_next` tool call within a phase correctly overrides
      the transition for that run only.
- [ ] `max_iterations` guard prevents infinite on_reject loops; after
      the limit the workflow fails with a clear error message.
- [ ] Parallel phases (`parallel_with`) run concurrently via
      `asyncio.gather`; the successor phase does not start until all
      sibling phases complete.

### Mode binding
- [ ] With active mode = Plan, submitting a message invokes the
      `plan_only` workflow (read-only planner phase, no executor).
- [ ] Switching mode to Auto mid-session causes the next message to
      use single-turn execution (no workflow).
- [ ] `/workflow architect` one-shot override causes the next message
      to use the `architect` workflow regardless of mode.

### Plugin loading
- [ ] TOML workflow file in `.agenthicc/workflows/` is loaded at
      session startup and appears in `/workflow list`.
- [ ] Python workflow plugin class in `~/.agenthicc/workflows/` is
      imported and registered correctly.
- [ ] Project workflow with the same name as a user-global workflow
      prints a startup warning and shadows the user workflow.
- [ ] A workflow with an invalid TOML or import error logs the error
      and is skipped; the session starts normally.

### TUI
- [ ] While a workflow is running, the status bar shows
      `Phase N/M: <phase_name>`.
- [ ] The footer shows `Workflow: <name>  │  Phase N/M: <phase_name>`
      when a workflow run is active.
- [ ] After the workflow completes, the footer returns to the normal
      mode-only display.

### Existing behaviour
- [ ] In Auto mode (no workflow), behaviour is byte-for-byte identical
      to pre-PRD-81: no `WorkflowRunner` is instantiated.
- [ ] All existing tests pass unchanged.
- [ ] `_run_agent_turn`'s new `output_collector` parameter is
      backward-compatible (default `None` = no collection, existing
      callers unaffected).
