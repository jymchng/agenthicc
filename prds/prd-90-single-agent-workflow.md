# PRD-90 — Single-Agent Workflow with Shared Memory

## Background

The `code_plan` workflow originally used separate specialised agents per phase
(explorer, planner, executor, reviewer).  Each phase started with a fresh
`ShortTermMemory`, so context had to be re-injected via `WorkflowContext
.as_system_block()`.  The 200-char truncation meant the executor received
only a fragment of the approved plan and was forced to re-explore the
codebase from scratch, burning most of its 40-turn budget before writing
a single line of code.

PRD-87 also introduced `PhaseSpec.inject_plan_tools` as a flag to gate which
phase receives `request_plan_approval` and `finalize_plan`.  This is
unnecessary: Plan mode's `blocked_capabilities` already governs write access
at the mode level.  The approval tools are safe to inject into all phases
unconditionally — the model only calls them during the plan phase, guided by
`system_prompt_override`.

---

## Goals

- One agent (`agent_type="auto"`) runs the entire `code_plan` workflow.
- A single `ShortTermMemory` instance is shared across all phases so the
  executor already has full planning context in memory.
- Remove `PhaseSpec.inject_plan_tools`; inject approval tools unconditionally
  at the `WorkflowRunner` level when `approval_svc` is available.
- `code_plan` is reduced from 5 phases (explore / plan / human_review /
  execute / summarize) to 4 phases (plan / execute / review / summarize).
  Exploration happens organically during the plan phase.
- `PhaseSpec.system_prompt_override`, which already exists in the dataclass,
  is wired through `_run_phase` so it can guide the single agent's focus in
  each phase.

## Non-Goals

- Changing `plan_only`, `review_only`, `supervised`, or `architect` workflows.
- Changing the approval overlay, `ApprovalService`, or `phase_tools.py`.
- Removing per-phase `allowed_capabilities` (still useful for other workflows).

---

## Data model change

### Remove `PhaseSpec.inject_plan_tools`

```python
# Before
@dataclass(frozen=True)
class PhaseSpec:
    ...
    inject_plan_tools: bool = False   # REMOVED

# After — field gone entirely
```

---

## `WorkflowRunner` changes

### Shared memory

`run()` creates one `ShortTermMemory` at the start of the workflow run and
stores it on `self`.  Every `_run_phase` call passes the same instance.

```python
async def run(self, intent: str) -> None:
    from lauren_ai._memory import ShortTermMemory
    self._shared_memory = ShortTermMemory(max_tokens=32_000)
    ...
```

### `_run_phase` — three targeted changes

**1. Pass shared memory (was `None`)**

```python
await _run_agent_turn(
    ...
    session_memory=self._shared_memory,   # was: None
    ...
)
```

**2. Resolve system prompt from `PhaseSpec.system_prompt_override` first**

```python
role_prompt = (
    spec.system_prompt_override
    or self._agents_registry.get_role_system_prompt(spec.agent_type)
)
```

**3. Inject approval tools unconditionally, no flag check**

```python
# Before:
if spec.inject_plan_tools and self._approval_svc is not None:

# After:
if self._approval_svc is not None:
```

`plan_event` and `plan_data` are still created per phase invocation.
`plan_event.is_set()` after the turn tells us whether this phase called
`finalize_plan()`.  Non-plan phases will simply never set it.

---

## Updated `code_plan` workflow

```
plan  →  execute  →  review  →  summarize
```

All phases use `agent_type="auto"`.  Focus is governed by `system_prompt_override`.

```python
class CodePlan(WorkflowPlugin):
    name          = "code_plan"
    description   = "Plan → Execute → Review → Summary (single agent, shared memory)"
    mode_bindings = ["Plan"]
    phases = [
        PhaseSpec(
            name="plan", agent_type="auto", max_turns=20, next="execute",
            system_prompt_override=(
                "You are in the PLANNING phase.  First explore the repository to "
                "understand the codebase.  Then produce a detailed implementation "
                "plan.  Use request_plan_approval() to present the plan for human "
                "review, and finalize_plan() once it is approved."
            ),
        ),
        PhaseSpec(
            name="execute", agent_type="auto", max_turns=40, next="review",
            system_prompt_override=(
                "You are in the EXECUTION phase.  You already explored and planned "
                "in the previous phase — do NOT re-explore.  Implement the approved "
                "plan step by step using tools."
            ),
        ),
        PhaseSpec(
            name="review", agent_type="auto", max_turns=8, next="summarize",
            output_schema="review_result", on_reject="execute",
            system_prompt_override=(
                "You are in the REVIEW phase.  Inspect the changes you just made "
                "and run the tests.  End with <review>approved</review> or "
                "<review>rejected: reason</review>."
            ),
        ),
        PhaseSpec(
            name="summarize", agent_type="auto", max_turns=4,
            output_schema="free_text",
            system_prompt_override=(
                "You are in the SUMMARY phase.  Write a concise summary of what "
                "was planned, implemented, and verified in this session."
            ),
        ),
    ]
```

---

## File changes

| File | Change |
|---|---|
| `workflow/plugin.py` | Remove `inject_plan_tools: bool = False` from `PhaseSpec` |
| `workflow/runner.py` | `run()`: create `self._shared_memory`; `_run_phase()`: pass shared memory, resolve `system_prompt_override`, inject approval tools without flag |
| `workflow/builtins.py` | `CodePlan`: 4 phases, all `agent_type="auto"`, `system_prompt_override` per phase, `mode_bindings=["Plan"]` |

---

## Acceptance criteria

- [ ] `PhaseSpec` has no `inject_plan_tools` field.
- [ ] All phases of `code_plan` share one `ShortTermMemory` instance.
- [ ] The execute phase prompt begins with "You are in the EXECUTION phase" and
      does not re-explore the codebase.
- [ ] `request_plan_approval` and `finalize_plan` are present in the tool list
      of every phase when `approval_svc` is available.
- [ ] `code_plan` has exactly 4 phases: plan, execute, review, summarize.
- [ ] All existing unit and integration tests pass.
