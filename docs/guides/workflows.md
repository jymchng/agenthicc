# Writing Custom Workflows

This guide explains how to write, place, configure, and troubleshoot custom
workflows in agenthicc.  It covers both the ergonomic happy path and the sharp
edges that the study found.

---

## What is a workflow?

A workflow is a directed graph of **phases**.  Each phase runs an LLM agent
turn, decides what happened (approved / rejected / error), and transitions to
the next phase.  The generic runner loops through phases until it reaches a
phase with no `next` value.

The builtin `code_plan` workflow is the most complete real-world example:
Plan → Execute → Review → Summary.  Custom workflows follow the same model.

---

## Minimal example

```python
# .agenthicc/workflows/my_workflow.py
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

class MyWorkflow(WorkflowPlugin):
    name          = "my_workflow"
    description   = "Single-phase research agent."
    mode_bindings = ["Auto"]        # triggers when in Auto mode
    phases        = [
        PhaseSpec(
            name="research",
            agent_type="auto",
            max_turns=20,
        ),
    ]
```

That's the entire file.  No `WORKFLOW` export list is needed — the loader
scans for all `WorkflowPlugin` subclasses automatically.

---

## Discovery — where to place the file

| Directory | Scope |
|---|---|
| `~/.agenthicc/workflows/` | User-global — available in every project |
| `.agenthicc/workflows/` | Project-local — overrides user-global definitions with the same `name` |

Both directories are scanned at startup.  Files whose name starts with `_` are
skipped.  Multiple `WorkflowPlugin` classes in one file are all registered.

> **Silent failure warning:** if a file has a syntax error or import failure,
> it is silently skipped — only a `WARNING`-level log entry is emitted.  The
> workflow simply won't appear.  If your workflow is missing, check the logs or
> run `uv run agenthicc --headless` to see startup output.  The TUI does not
> currently surface workflow discovery failures to the user.

> **Wrong directory warning:** if you place the file in the wrong directory
> (e.g., `.agenthicc/workflow/` with no `s`), it is silently ignored.
> Double-check the directory name.

---

## Two-phase workflow with retry

```python
# .agenthicc/workflows/plan_execute.py
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

class PlanAndExecute(WorkflowPlugin):
    name          = "plan_execute"
    description   = "Plan first, execute second, retry plan on failure."
    mode_bindings = ["Plan"]        # auto-triggers in Plan mode
    phases        = [
        PhaseSpec(
            name      = "plan",
            agent_type= "planner",
            max_turns = 8,
            output_schema = "plan",
            next      = "execute",
            on_reject = "plan",     # retry this phase if approved=False
            max_iterations = 5,
        ),
        PhaseSpec(
            name      = "execute",
            agent_type= "executor",
            max_turns = 30,
            mode_override = "Auto", # unlock write/exec tools for this phase
        ),
    ]
```

---

## PhaseSpec field reference

### Fields you will always use

| Field | Default | Description |
|---|---|---|
| `name` | **required** | Unique identifier within the workflow. Used in `next` and `on_reject` strings. |
| `agent_type` | `"auto"` | Which agent role to run. See the **agent_type values** section below. |
| `next` | `None` | Name of the phase to run after this one. `None` ends the workflow. |
| `on_reject` | `None` | Phase to jump to when the agent's output has `approved=False`. Enables retry loops. |
| `max_turns` | `20` | Maximum LLM sub-turns (tool-call → response cycles) within one phase run. |
| `system_prompt_override` | `""` | When non-empty, completely replaces the role's system prompt for this phase. |
| `mode_override` | `None` | Temporarily switch to this mode for the duration of the phase. Use `"Auto"` to unlock write tools. |

### Fields you will use occasionally

| Field | Default | Description |
|---|---|---|
| `output_schema` | `None` | Parse structured data from the phase output. Valid values: `"plan"`, `"review_result"`, `"free_text"`. Any other string silently produces `{"raw": text}`. |
| `max_iterations` | `-1` | Hard ceiling on how many times this phase may be entered in one workflow run. `-1` means unlimited. |
| `parallel_with` | `()` | Tuple of phase names to run concurrently with this one via `asyncio.gather`. |

### Advanced fields (rarely needed)

| Field | Description |
|---|---|
| `require_plan_finalization` | Phase loops until the agent calls `finalize_plan()`. Only active when an `ApprovalService` is present (i.e., TUI mode). Silent no-op in headless mode. |
| `require_explicit_completion` | Phase loops until the agent calls `mark_execute_complete()`. Same caveat. |
| `require_explicit_review` | Phase loops until the agent calls `approve_review()` or `reject_review()`. Same caveat. |
| `allowed_capabilities` | `frozenset[ToolCapability]` — restrict tools for this phase. `None` uses the role default. |
| `allowed_capabilities_override` | Same, but takes priority over `allowed_capabilities` and the role default. |

### Common pitfall: `max_iterations=-1` sentinel

`-1` is the sentinel for "unlimited".  Setting `max_iterations=0` means the
phase will immediately fail on entry.  Use `-1` or a positive integer.

---

## agent_type values

The `agent_type` string maps to a role in the `AgentsRegistry`.  The builtin
values are:

| Value | What it does | Default capabilities |
|---|---|---|
| `"auto"` | General-purpose agent.  No role constraint. | Mode ceiling (full write by default) |
| `"planner"` | Produces a plan; instructed not to modify files. Wraps output in `<plan>…</plan>`. | Read + git-read + search only |
| `"executor"` | Follows a plan step by step using tools. | Mode ceiling |
| `"reviewer"` | Reviews code; wraps output in `<review>approved</review>` or `<review>rejected: reason</review>`. | Read + git-read + search |
| `"explorer"` | Researches the codebase; instructed not to make changes. | Read + git-read + search |
| `"verifier"` | Checks an implementation; same review tag convention as `"reviewer"`. | Read + git-read + search |
| `"human"` | No LLM invocation — pauses and shows a user approval dialog. | None |

> **Silent fallback:** an unknown `agent_type` (including a typo like
> `"planer"`) silently falls back to `"auto"`.  There is no error.  If your
> phase is behaving like a generic agent instead of a planner, check the
> spelling.

---

## mode_bindings

`mode_bindings` lists the mode names that automatically trigger this workflow
when the user sends a message.

```python
mode_bindings = ["Plan"]      # triggers in Plan mode
mode_bindings = ["Auto"]      # triggers in Auto mode
mode_bindings = []            # never auto-triggers; run explicitly
```

**Valid mode names** (case-sensitive — title case required):

`"Auto"`, `"Plan"`, `"Ask"`, `"Review"`, `"Safe"`, `"Debug"`

> **Case-sensitivity pitfall:** `mode_bindings = ["plan"]` (lowercase) will
> never trigger.  The binding silently registers but the mode name doesn't
> match any runtime mode.

When a workflow is bound to a mode, the **first** registered binding wins.
Builtins are registered first; a project-local workflow with the same
`mode_bindings` shadows the builtin but logs a warning.

---

## Adding configurable parameters (WorkflowParams)

Custom parameters can be exposed as a TOML section and passed into the runner.

```python
# .agenthicc/workflows/my_workflow.py
import dataclasses
from dataclasses import field
from typing import Any
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

@dataclasses.dataclass
class MyWorkflowParams(WorkflowParams):
    execute_model: str = field(default="")   # cheap model for execute phase
    max_retries:   int = field(default=3)

    def get_phase_models(self) -> dict[str, str]:
        return {"execute": self.execute_model}

class MyWorkflow(WorkflowPlugin):
    name   = "my_workflow"
    phases = [PhaseSpec(name="execute", agent_type="auto", max_turns=20)]

    @classmethod
    def params_factory(cls, source: dict[str, Any]) -> WorkflowParams:
        known = {f.name for f in dataclasses.fields(MyWorkflowParams)}
        return MyWorkflowParams(**{k: v for k, v in source.items() if k in known})
```

**TOML configuration** (`.agenthicc/agenthicc.toml`):

```toml
[workflows.my_workflow]
execute_model = "claude-haiku-4-5"
max_retries   = 5
```

**CLI override:**

```bash
agenthicc --set workflows.my_workflow.execute_model=claude-haiku-4-5
```

`get_phase_models()` returns a `{phase_name: model_id}` map.  Empty string
means "use the global `execution.model`".  The runner applies the override
automatically at the start of each phase.

---

## Using a custom mode_override per phase

When a phase needs write/exec tools (e.g., a code implementation phase), set
`mode_override = "Auto"`.  The runner temporarily switches to Auto mode for the
duration of that phase, then restores the original mode when the phase
completes.

```python
PhaseSpec(
    name          = "implement",
    agent_type    = "executor",
    max_turns     = 40,
    mode_override = "Auto",   # unlock write + exec tools
)
```

> **Headless note:** `mode_override` requires a `ModeManager` to be present.
> In headless mode (no TUI), it is silently ignored.

---

## Chaining phases with rejection loops

Use `on_reject` to create a retry loop.  When the phase output is
`approved=False`, the runner jumps to the named phase.

```python
phases = [
    PhaseSpec(name="plan",    next="execute", on_reject="plan",    max_iterations=5),
    PhaseSpec(name="execute", next="review",  on_reject="plan",    max_iterations=10),
    PhaseSpec(name="review",  next=None,      on_reject="execute", max_iterations=3),
]
```

`max_iterations` caps how many times a single phase can be entered.  When
exceeded, the workflow fails with a clear error in the scroll buffer.

---

## Custom runner (advanced)

The generic `WorkflowRunner` runs phases sequentially or in parallel.  When
you need a proper state machine with dynamic transitions not expressible as
`next`/`on_reject` strings, override `runner_factory`:

```python
from agenthicc.workflows.base import BaseWorkflowRunner
from agenthicc.workflows.plugin import WorkflowDefinition, WorkflowPlugin, PhaseSpec
from agenthicc.workflows.config import WorkflowConfig

class MyRunner(BaseWorkflowRunner):
    def __init__(self, defn: WorkflowDefinition, config: WorkflowConfig, mode_manager) -> None:
        self._defn   = defn
        self._cfg    = config
        self._modes  = mode_manager

    async def run(self, intent: str) -> None:
        ...   # custom orchestration logic

    async def resume(self, context) -> None:
        ...   # restore state and continue

class MyWorkflow(WorkflowPlugin):
    name   = "my_workflow"
    phases = [PhaseSpec(name="step1", agent_type="auto")]   # phases still required for discovery

    @classmethod
    def runner_factory(cls, defn, config, mode_manager):
        return MyRunner(defn, config, mode_manager)
```

The `WorkflowConfig` object gives you access to `agent_runner`, `processor`,
`app_state`, `conv_store`, `approval_svc`, `plugin_tools`, `mcp_registry`,
`skills`, and all other session singletons.  Import it from
`agenthicc.workflows.config`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow not appearing in mode list | File in wrong directory, or syntax/import error | Check directory name; run with `--headless` and check log output |
| Phase runs as generic agent instead of planner | Typo in `agent_type` | Verify spelling: `"planner"`, not `"planer"` |
| `mode_bindings` not triggering | Wrong case | Use title case: `"Plan"`, not `"plan"` |
| `require_plan_finalization` has no effect | Headless mode or test without `ApprovalService` | Only active in TUI mode |
| Workflow fails immediately | `next` references nonexistent phase | Error message in scroll buffer names the missing phase |
| `on_reject` loop runs forever | `max_iterations` not set (default `-1`) | Set a positive `max_iterations` value |
| `output_schema` produces `{"raw": text}` | Typo in schema name | Valid values: `"plan"`, `"review_result"`, `"free_text"` |

---

## Known sharp edges and workarounds

### 1. No user-visible error when a workflow file fails to load

The TUI shows nothing.  Workaround: run `uv run agenthicc --headless` and
watch for `WARNING` lines mentioning your file path.

### 2. `agent_type` typos silently fall back to `"auto"`

There is no validation.  Workaround: test each phase independently and verify
the system prompt appears as expected (visible in the status bar model label).

### 3. `require_*` handshake flags are TUI-only

`require_plan_finalization`, `require_explicit_completion`, and
`require_explicit_review` only fire when an `ApprovalService` is wired in
(i.e., the full TUI session).  In headless or integration tests they are
silently inert.  Workaround: use `output_schema` for structured output
extraction instead, which works in all contexts.

### 4. `parallel_with` references to nonexistent phases are silently dropped

If a phase listed in `parallel_with` doesn't exist, it is quietly excluded
from the concurrent group.  No warning is emitted.

### 5. `mode_override` is inert without a ModeManager

Silently ignored in headless mode.  If you need write tools in headless,
configure the mode at the session level instead.

---

## Complete worked example — Code Review workflow

```python
# .agenthicc/workflows/code_review.py
"""
A two-phase workflow: explore the codebase, then produce a structured review.

Usage:
    1. Place this file in .agenthicc/workflows/
    2. Switch to Review mode in the TUI (Shift+Tab)
    3. Send a message describing what to review

Configuration (.agenthicc/agenthicc.toml):
    [workflows.code_review]
    review_model = "claude-opus-4-8"
"""
from __future__ import annotations

import dataclasses
from dataclasses import field
from typing import Any

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin


@dataclasses.dataclass
class CodeReviewParams(WorkflowParams):
    review_model: str = field(default="")

    def get_phase_models(self) -> dict[str, str]:
        return {"review": self.review_model}


class CodeReview(WorkflowPlugin):
    name          = "code_review"
    description   = "Explore → structured code review with approve/reject."
    mode_bindings = ["Review"]
    phases        = [
        PhaseSpec(
            name       = "explore",
            agent_type = "explorer",
            max_turns  = 10,
            next       = "review",
        ),
        PhaseSpec(
            name          = "review",
            agent_type    = "reviewer",
            max_turns     = 8,
            output_schema = "review_result",
            on_reject     = "review",   # retry if reviewer forgets the tag
            max_iterations= 3,
        ),
    ]

    @classmethod
    def params_factory(cls, source: dict[str, Any]) -> WorkflowParams:
        known = {f.name for f in dataclasses.fields(CodeReviewParams)}
        return CodeReviewParams(**{k: v for k, v in source.items() if k in known})
```

---

## Summary of ergonomics findings

| Area | Assessment |
|---|---|
| Minimal workflow | ✅ Easy — 10 lines, no boilerplate exports required |
| Discovery | ⚠️ Silent failures — no user-visible error when file is missing or broken |
| PhaseSpec core fields | ✅ Intuitive — name, next, on_reject, max_turns are self-explanatory |
| agent_type | ⚠️ Undiscoverable — no docs listing valid values; typos silently use "auto" |
| mode_bindings | ⚠️ Case-sensitive with no validation or error |
| WorkflowParams | ⚠️ Verbose — requires boilerplate for field filtering in params_factory |
| Custom runner | ❌ Hard — requires importing internal types and implementing full state machine |
| Error messages | ⚠️ Mixed — unknown phase in transition is clear; everything else is silent |
