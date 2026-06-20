# PRD-114 — Composite Workflows via Runner Inheritance

## Summary

End users can extend any existing workflow (e.g. `code_plan`) by subclassing
its `WorkflowPlugin` and `BaseWorkflowRunner` in a local plugin file.  The
runner's `run()` method returns a typed context object so subclasses can call
`ctx = await super().run(intent)` and continue with additional phases.  A
`/workflow <name>` command lets users switch the active workflow within the
current mode without leaving it.

---

## User Scenario

A user notices that `code_plan` lacks a documentation update phase.  They want
to extend it — not fork or modify it — by adding a fifth phase that reviews and
updates docs based on what was just implemented.

They drop one file into `.agenthicc/workflows/`:

```python
# .agenthicc/workflows/code_plan_docs.py

class CodePlanDocsRunner(CodePlanRunner):
    async def run(self, intent: str) -> None:
        ctx = await super().run(intent)        # all 4 existing phases, unchanged
        await self.run_phase(                  # public API — no internals needed
            intent=intent,
            text=(
                f"[PLAN]\n{ctx.plan}\n\n"
                f"[WHAT WAS DONE]\n{ctx.execute_summary}\n\n"
                "Review and update the project documentation."
            ),
            system_prompt="You are a documentation writer...",
            mode="Auto",
            max_turns=10,
            shared_memory=ctx.shared_memory,
        )

class CodePlanDocs(CodePlan):
    name          = "code_plan_docs"
    mode_bindings = ["Plan"]

    @classmethod
    def runner_factory(cls, defn, config, mode_manager):
        return CodePlanDocsRunner(config, mode_manager)
```

They activate it in the TUI:

```
/workflow code_plan_docs
```

---

## Design

### 1. `BaseWorkflowRunner.run()` returns `Any`

Contract change: `run(intent) -> None` becomes `run(intent) -> Any`.

Callers in `tui_session.py` ignore the return value — no change there.

### 2. `CodePlanRunner.run()` returns `CodePlanContext`

```python
async def run(self, intent: str) -> CodePlanContext:
    ...
    return ctx   # was implicit None
```

Subclasses can now:
```python
ctx = await super().run(intent)
# ctx.plan, ctx.execute_summary, ctx.review_summary, ctx.shared_memory
```

### 3. `WorkflowRunner.run()` returns `WorkflowContext`

Symmetric change for the generic runner.

### 4. `CodePlanRunner.run_phase()` — public extension API

A stable public method that end users call to execute one additional phase
without touching private internals (`_run_turn`, `_base_tools`):

```python
async def run_phase(
    self,
    *,
    intent:        str,
    text:          str,
    system_prompt: str,
    mode:          str | None = None,
    max_turns:     int = 10,
    shared_memory: ShortTermMemory | None = None,
) -> None:
```

Internally delegates to `_run_turn()` with `_base_tools()`.  The stable
signature is the contract; internals may change freely.

### 5. `/workflow <name>` TUI command

Stores a per-session override on `TUISession._workflow_override: str | None`.
`run_turn()` checks the override before the mode's `default_workflow`.

```
/workflow code_plan_docs    ← switch within Plan mode
/workflow reset             ← back to mode default
```

Status bar shows `⬡ code_plan_docs` when an override is active.

---

## Ergonomic guardrails

| Problem | Mitigation |
|---|---|
| `runner_factory` signature is non-obvious | Document in `/create-workflow` skill; type hints on ABC |
| `_run_turn()` is a private API | `run_phase()` is the stable public surface; `_run_turn` stays private |
| Wrong `mode_bindings` casing | `/workflow` works regardless — explicit activation bypasses binding |
| Silent failure if `runner_factory` returns wrong type | `run_turn()` checks `isinstance(runner, BaseWorkflowRunner)` and raises clearly |

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `BaseWorkflowRunner.run()` return type is `Any`. |
| 2 | `CodePlanRunner.run()` returns a fully populated `CodePlanContext`. |
| 3 | `WorkflowRunner.run()` returns `WorkflowContext`. |
| 4 | `CodePlanRunner.run_phase(intent, text, system_prompt, ...)` is public and documented. |
| 5 | A subclass calling `ctx = await super().run(intent)` receives typed context. |
| 6 | `ctx.plan`, `ctx.execute_summary`, `ctx.review_summary`, `ctx.shared_memory` are accessible. |
| 7 | `/workflow <name>` sets `TUISession._workflow_override` and shows a notification. |
| 8 | `/workflow reset` clears the override and reverts to mode default. |
| 9 | Status bar shows the overriding workflow name when active. |
| 10 | `code_plan` itself is unmodified in logic; only the return statement changes. |
| 11 | `CodePlanDocs` plugin in `.agenthicc/workflows/` is discovered automatically. |
| 12 | `/create-workflow` skill is registered as a default bootstrap skill. |

---

## Files Changed

| File | Change |
|---|---|
| `workflows/base.py` | `run() -> None` → `run() -> Any` |
| `workflows/code_plan/runner.py` | `run()` returns `ctx`; add public `run_phase()` |
| `workflows/default/runner.py` | `run()` returns `WorkflowContext` |
| `runners/tui_session.py` | `_workflow_override` field; `/workflow` handler; `run_turn()` checks override |
| `skills/bootstrap.py` | Add `create-workflow` to `_DEFAULTS` |
| `python-password-generator/.agenthicc/workflows/code_plan_docs.py` | Example composite workflow |
