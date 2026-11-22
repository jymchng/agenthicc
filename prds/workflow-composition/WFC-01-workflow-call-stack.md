---
title: "WFC-01: A workflow call stack — enter a workflow, then return to the one you left"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-02, WFC-03, prd-114, prd-170, prd-213, prd-155]
tags: [workflows, session-state, tui, lifecycle]
---

# WFC-01 — A workflow call stack

## 1. Summary

Replace the single sticky `_workflow_override` slot with a session-owned
**workflow stack**: a base frame (the mode default or `/workflow NAME`
selection) plus zero or more **call frames** that run to completion and then
*return* to the frame beneath them. When a call frame completes, agenthicc pops
it, hands the parent one typed `WorkflowReturn`, and resumes the parent — the
agent is back where it was, with the child's result in hand.

## 2. Problem (current source)

- `TUISession._workflow_override` is one `str | None`
  (`runners/tui_session.py:1819`). It is written by `/workflow NAME`,
  `/workflow reset`, resume, and reload-removal (`:2052`, `:2077`, `:4250`,
  `:4547`). **No completion path clears it.** PRD-213 deliberately keeps a
  resumed workflow selected "until the user chooses another".
- `_finalize_returned_workflow` (`:2145`) marks the handle `complete` and
  releases the claim. The selector is untouched, so the next ordinary message
  re-enters the finished workflow — for `create_workflow` that starts a new
  authoring run.
- There is exactly one `_workflow_handle` and one `_agent_task`
  (`:1716`); `WorkflowRunHandle` records no parent. There is nowhere to put
  "who called me".
- The turn resolves its workflow as
  `self._workflow_override or active_mode().default_workflow` (`:5146`), so the
  only "previous workflow" that can ever come back is the mode default, and
  only because the override was manually reset.

## 3. Goals

1. A frame entered on top of the base returns to its parent automatically on
   completion, with a bounded typed result.
2. The parent resumes with that result in its conversation, once, even if the
   process stops between pop and delivery (delivery durability is WFC-03).
3. Failure, pause and cancellation never silently pop. The operator keeps full
   control of a frame that did not finish.
4. A child never exceeds its parent's capability ceiling.
5. `_workflow_override` and everything that reads or writes it is deleted.

## 4. Non-goals

- Model-facing entry tools (WFC-02), persistence and replay (WFC-03), subagent
  workflows (WFC-04).
- Concurrent foreground frames. Exactly one frame runs at a time; ancestors are
  suspended.
- Changing a workflow's internal phase machine or checkpoint topology.

## 5. Design

### 5.1 Types

New module `runners/workflow_stack.py`.

```python
WorkflowFrameStatus = Literal[
    "running", "suspended", "paused", "completed", "failed", "abandoned"
]
FrameOrigin = Literal["selection", "user", "agent"]

@dataclass(frozen=True, slots=True)
class WorkflowFrame:
    frame_id: str                 # stable, unique per session
    workflow_name: str
    run_id: str                   # the WorkflowRunHandle run this frame owns
    parent_frame_id: str | None   # None only for the base frame
    origin: FrameOrigin
    intent: str
    depth: int                    # base = 0
    policy: SubagentExecutionPolicy   # effective mode/capability snapshot at entry
    status: WorkflowFrameStatus

@dataclass(frozen=True, slots=True)
class WorkflowReturn:
    frame_id: str
    workflow_name: str
    status: Literal["completed", "failed", "abandoned"]
    summary: str                       # ≤ 2,000 characters, truncated with a marker
    outputs: Mapping[str, str]         # ≤ 16 keys, values ≤ 1,000 characters
    artifacts: tuple[PublishedArtifact, ...] = ()   # defined in WFC-11
    def as_system_block(self) -> str: ...            # parallels WorkflowContext.as_system_block
```

`WorkflowStack` owns the frames and exposes: `base`, `top`, `depth`,
`push(entry) -> WorkflowFrame`, `complete(frame_id, ret) -> WorkflowFrame`,
`abandon(frame_id) -> WorkflowReturn`, `ancestry(frame_id)`. It performs no I/O.
`SubagentExecutionPolicy` (`subagents/policy.py`) is reused as the snapshot
type; renaming it is out of scope.

### 5.2 Base vs call frames

- **Base frame.** Replaces `_workflow_override`. `/workflow NAME` sets the base
  workflow; `/workflow reset` clears it to the mode default; mode change
  re-resolves it. This keeps PRD-114/213 selection semantics for the *base*.
- **Call frame.** Created by `enter_workflow` (WFC-02) or `/workflow call NAME
  <intent>`. Never sticky: it exists only until it returns or is abandoned.

`/workflow NAME` while `depth > 0` is refused with
`Finish or abandon the running workflow first (/workflow return --abandon).`

### 5.3 Lifecycle

| Trigger | Effect |
|---|---|
| Entry accepted (WFC-02) | At the end of the requesting turn, push a frame (parent → `suspended`), create its run handle and claim, start its runner exactly as a normal workflow turn starts (the `:5146` path factored into `_start_frame`). |
| Runner returns, state not failed | `_finalize_returned_workflow` marks the handle complete, then `stack.complete(...)`: pop, build `WorkflowReturn`, run the publish and reload step if the return carries artifacts (WFC-11, WFC-05), then deliver (§5.4). |
| Runner returns typed `FAILED`, or raises | Handle becomes `paused` (existing behaviour). Frame stays on the stack as `paused`; parent stays `suspended`. Notice names `/workflow resume` and `/workflow return --abandon`. |
| Ctrl+C / interrupt | Existing pause semantics; frame `paused`. Never a pop. |
| `/workflow return --abandon` | `stack.abandon`: discard the paused run per existing reset rules, build a `status="abandoned"` return, deliver it to the parent. |
| `/workflow resume [run-id]` | Resumes the paused frame. If it later completes it pops normally. |

### 5.4 Resuming the parent

The parent is resumed by **queuing a synthetic turn**, the same `turn_queued`
event used by resume continuation (`_dispatch_workflow_resume`), with
`client_id="workflow-return"` and an idempotency key
`return:<parent_frame_id>:<child_run_id>`. Its text is
`WorkflowReturn.as_system_block()`. The turn runs in the parent's workflow
(usually the base `default` agent), so the agent sees the child's result and
continues its task. Because it is a *new turn*, tool and skill surfaces rebuilt
by the reload step are visible to its first model request (WFC-08).

The conversation transcript renders the entry and the return as workflow blocks
(WFC-12); the synthetic turn is not rendered as user-authored text.

### 5.5 Capability ceiling

Entry computes `child.policy = parent.policy ∩ workflow.phase_capabilities`.
A `Plan`-mode parent can only enter workflows whose every phase is read-only
(`PhaseSpec.resolved_allowed_caps`). Approval prompts remain session-wide and
foreground.

### 5.6 Limits

| Guard | Default | Behaviour |
|---|---|---|
| `workflows.max_stack_depth` | 4 (base counts as 0) | Entry above this is rejected before any run is created. |
| Re-entrancy | forbidden | A workflow name may appear once on the stack unless its `WorkflowInvocation.reentrant` is true (WFC-02). |

These bound the single interactive slot against a model that keeps entering
workflows. They are unrelated to subagent limits, which this series does not
add.

## 6. Requirements

- **FR-1** `WorkflowStack` is a pure in-memory structure with no I/O; every
  transition is a method returning a new frame value.
- **FR-2** A frame completes at most once; `complete` on a frame that is not the
  top, or is not `running`, raises a typed `WorkflowStackError`.
- **FR-3** Return delivery is idempotent on the idempotency key (§5.4).
- **FR-4** The status line and `AppState.conversation.workflow_stack`
  (a signal of frame summaries) replace `workflow_override`. The footer
  projection derives from the stack.
- **FR-5** `_reload_workflows` removing a workflow that appears on the stack does
  not delete the frame; the frame keeps its materialised plugin class. The next
  entry of that name uses the new class; resuming a changed one goes through the
  existing fingerprint check (`workflow_fingerprint`) and
  `PLUGIN_INCOMPATIBLE` path.
- **FR-6** Headless sessions use the same stack; frames execute sequentially
  through `execute_workflow`.

## 7. Acceptance criteria

| # | Criterion | Check |
|---|---|---|
| A1 | Base `default` → enter a stub workflow → it completes → `stack.depth == 0`, `stack.top.workflow_name == "default"`, and the next user message runs `default`. | integration |
| A2 | The parent receives exactly one return block, also after a simulated redelivery with the same key. | unit + integration |
| A3 | A typed-`FAILED` child leaves the stack unchanged, parent `suspended`, child `paused`; `/workflow resume` completes it and pops; `/workflow return --abandon` pops with `status="abandoned"` and the parent sees it. | integration |
| A4 | Depth 5 and a repeated name are rejected with distinct typed errors and create **no** run, handle or checkpoint. | unit |
| A5 | `/workflow NAME` at depth > 0 changes nothing and shows the refusal. | integration |
| A6 | A `Plan`-mode parent cannot enter a workflow with a write-capable phase. | unit |
| A7 | After completion no code path reads a workflow selector other than the stack base; `grep -rn "_workflow_override\|workflow_override" src tests docs` returns nothing. | search |
| A8 | `mypy src/agenthicc` and the type-audit baseline are unchanged or improved; the new module contains no `Any`. | CI |

## 8. Test plan

- `tests/unit/test_workflow_stack.py` — transitions, errors, depth, re-entrancy,
  truncation bounds of `WorkflowReturn`.
- `tests/integration/test_workflow_stack_session.py` — real
  `EventProcessor`, a registered stub workflow, success, failure, abandon,
  resume, `/workflow` refusal.
- `tests/e2e/test_workflow_call_return.py` — cassette session: agent requests
  entry, child runs two phases, parent continues with the result.

## 9. Symbols removed or replaced

`TUISession._workflow_override`, `_set_workflow_override`,
`_reset_workflow_to_mode_default` (becomes `stack.clear_base()`),
`conversation.workflow_override`, and every `_workflow_override` read in
`_handle_workflow_command`, `_dispatch_workflow_resume`, `_reload_workflows`
(superseded by WFC-05) and the turn-start path. `docs/guides/workflows.md`,
`docs/guides/architecture.md`, `docs/glossary.md` and `llms-full.txt` are
updated in the same change; PRD-114 and PRD-213 gain a "superseded in part by
WFC-01" note.

## 10. Risks

| Risk | Mitigation |
|---|---|
| A runner-phased parent cannot be paused mid-phase to host a child. | v1 allows entry only from free-form agent frames and workflows that opt in (WFC-02). Phased workflows are valid children. |
| Return delivery lost between pop and turn start. | Idempotent key now; durable replay in WFC-03. |
| Operators confused by an implicit return. | Entry/return transcript blocks and a breadcrumb (WFC-12). |
