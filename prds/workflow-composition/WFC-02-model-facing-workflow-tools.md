---
title: "WFC-02: Model-facing workflow tools — list, enter, and inspect"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-01, WFC-04, WFC-12, prd-76, prd-78, prd-91, SUBAGENT-13]
tags: [workflows, tools, capabilities, approvals]
---

# WFC-02 — Model-facing workflow tools

## 1. Summary

Give the agent three tools — `list_workflows`, `enter_workflow` and
`workflow_status` — and give each workflow a typed `WorkflowInvocation`
declaration that says whether, how and from where it may be entered. Entry is a
**deferred handoff**: the tool validates and records a request, the current turn
ends normally, and the session pushes the frame (WFC-01) when the turn
finishes.

## 2. Problem (current source)

- Entering a workflow requires a person (`/workflow NAME`) or a mode default
  (`runners/tui_session.py:5146`). A search of `src/` for model tools that
  enter, switch or run a workflow found none.
- Workflow metadata is built for humans: `WorkflowPlugin` carries `name`,
  `phases`, `mode_bindings`, parameters (`build_params`), but nothing says
  *when an agent should use it*, whether it is safe to call from a model, or
  whether it needs a human at a gate.
- `code_plan` has a plan-approval overlay (`workflows/code_plan/phase_tools.py`,
  PRD-86); a workflow like that can run in the foreground but not unattended.

## 3. Goals

1. An agent can discover agent-callable workflows, enter one with an intent and
   typed parameters, and read stack status.
2. Safety is declarative: each workflow states what it permits; the runtime
   enforces it before any run exists.
3. Entry never nests runners inside a tool call; it is a handoff at a turn
   boundary.

## 4. Non-goals

- Stack mechanics and return delivery (WFC-01); persistence (WFC-03).
- Running workflows inside subagents (WFC-04) — that has its own tool surface
  (`spawn_subagents`).
- Letting the agent end or abandon a child it cannot observe (the parent is
  suspended while a child runs; abandonment is an operator action).

## 5. Design

### 5.1 `WorkflowInvocation`

A new frozen dataclass exposed as `WorkflowPlugin.invocation` (class attribute,
default all-false).

```python
@dataclass(frozen=True, slots=True)
class WorkflowInvocation:
    when_to_use: str = ""               # one sentence shown to the model
    returns: str = ""                   # what summary/outputs the return carries
    agent_callable: bool = False        # enter_workflow may start it
    subagent_eligible: bool = False     # spawn_subagents may run it (WFC-04)
    interactive: bool = False           # has human gates (plan approval, ask_user)
    reentrant: bool = False             # may appear twice on one stack
    allows_child_workflows: bool = False  # its free-form agent may call enter_workflow
    publishes: tuple[Literal["tool", "skill", "workflow", "command"], ...] = ()
```

"An inert configuration field is a bug" (CLAUDE.md): each field above is read
by a named enforcement site — `enter_workflow` (`agent_callable`,
`reentrant`, `allows_child_workflows`), WFC-04 (`subagent_eligible`,
`interactive`), WFC-12 (`when_to_use`, `returns`), WFC-11/06/07/09
(`publishes`). A field with no reader is not added.

Built-in declarations (everything not listed stays all-false):

| Workflow | `agent_callable` | `interactive` | `publishes` | `subagent_eligible` |
|---|---|---|---|---|
| `create_workflow` | yes | no | `workflow` | yes (stages only) |
| `create_tools` | yes | no | `tool` | yes (stages only) |
| `create_skills` | yes | no | `skill` | yes (stages only) |
| `code_plan` | yes | yes | — | no |
| `goal_flow`, `make_book`, `reconstruct_site`, `site_imitate`, `copy_website`, `ui_ux_pro_max`, `parallel_code_plan` | no (until reviewed individually) | — | — | no |

### 5.2 Tools

Defined in `workflows/stack_tools.py`, registered next to the other session
tools in `runners/agent_turn.py`. Schemas come from `TypedDict`s; no `Any`.

| Tool | Capability | Arguments | Result |
|---|---|---|---|
| `list_workflows` | read | none | `[{name, when_to_use, returns, params, interactive, source}]` for agent-callable workflows only |
| `enter_workflow` | `CONTROL` (`tools/capabilities.py:124`) | `workflow: str`, `intent: str`, `params: dict[str, str] \| None` | `EntryReceipt{status: "handoff_accepted", frame_id, note}` or a typed error |
| `workflow_status` | read | none | stack as frames (name, status, current phase) and the last `WorkflowReturn` summaries |

If lauren-ai's schema generator cannot express `dict[str, str] | None`, `params`
becomes a JSON-object string validated by `WorkflowPlugin.build_params`; the
choice is made by a schema test (§8), not by guess.

### 5.3 `enter_workflow` validation order

Each failure returns a typed error code, creates no run, and leaves the turn
running so the model can recover.

| Order | Check | Code |
|---|---|---|
| 1 | Workflow exists in the live registry | `unknown_workflow` |
| 2 | `invocation.agent_callable` | `not_agent_callable` |
| 3 | The current frame may call children (free-form agent frame, or `allows_child_workflows`) | `not_allowed_from_frame` |
| 4 | Depth and re-entrancy (WFC-01 §5.6) | `depth_exceeded`, `cycle` |
| 5 | Capability ceiling (WFC-01 §5.5) | `ceiling_denied` |
| 6 | `build_params` accepts `params` | `invalid_params` |
| 7 | No other handoff pending this turn | `handoff_already_pending` |
| 8 | Approval (below) | `denied` |

### 5.4 Approval

Entry is an approval-brokered action of a new category `workflow_enter`
(registered with the existing approval service, PRD-78). The prompt shows the
workflow, intent, parameters, `interactive`, and the capability ceiling the
child will run under. Safe asks; Plan allows only read-only workflows (check 5);
Yolo allows. Entering a workflow that was just created is the same ordinary
action. Trust of authored code is **not** decided here and never prompts: the
session's publish step records it automatically after the runner-owned verdict
(WFC-11 §5.2).

### 5.5 Deferred handoff

`enter_workflow` stores a `PendingEntry` on the session-owned turn context and
returns `handoff_accepted` with the note
*"Your turn ends now. You will be resumed with the workflow's result."* When the
turn ends normally, the session pushes the frame. If the turn fails or is
cancelled, the entry is dropped and a notice says so. This avoids running a
second runner inside a tool call and needs no new "stop the loop" mechanism.

### 5.6 Untrusted input

`intent` and `params` are model-authored. They are passed to the child as data
inside its first system block, never interpolated into a command or path; each
workflow's own validation (`build_params`) is the gate.

## 6. Requirements

- **FR-1** `list_workflows` returns only workflows the caller may actually enter
  from the current frame and ceiling.
- **FR-2** Every error code in §5.3 has a stable string value, a one-sentence
  `fix` hint and a test.
- **FR-3** `enter_workflow` is idempotent per turn on `(workflow, intent)` after
  acceptance; a second identical call returns the same receipt.
- **FR-4** The three tools are inherited by workers as part of "behave like the
  main agent" (SUBAGENT-12) **except** `enter_workflow`, which a worker does not
  receive; a worker's whole run is its own frame.
- **FR-5** `WorkflowInvocation` participates in the workflow fingerprint so a
  change to callability invalidates stale checkpoints deliberately.
- **FR-6** Headless: `interactive` workflows return `not_allowed_from_frame` with
  the reason `no_human_gate_available`.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | A scripted agent calls `list_workflows`, then `enter_workflow("create_tools", …)`; the child starts only after the turn ends; the parent resumes with the return block. |
| A2 | A non-agent-callable workflow, an unknown name, an over-depth entry and a repeated name each return the documented code and create no run. |
| A3 | A `Plan`-mode session cannot enter `create_tools` (`ceiling_denied`). |
| A4 | A cancelled turn after `handoff_accepted` starts no frame. |
| A5 | The tool schemas validate under lauren-ai and contain no free-form `object` where a typed shape exists. |
| A6 | `list_workflows` output for a 40-workflow registry is ≤ 6,000 characters (bounded, ordered by name). |

## 8. Test plan

- Unit: `tests/unit/test_workflow_invocation.py` (declaration defaults, every
  built-in's table row), `tests/unit/test_enter_workflow_validation.py`
  (each code).
- Spike test first: `tests/unit/test_stack_tool_schema.py` decides the `params`
  encoding.
- Integration: `tests/integration/test_enter_workflow_handoff.py` with approval
  broker, cancel path, idempotency.
- E2E: covered by WFC-13 S1.

## 9. Documentation and cleanup

`docs/guides/workflows.md` (invocation declaration, agent entry), `docs/guides/tools.md`,
`docs/guides/security.md` (new approval category), `llms-full.txt` entries for
`WorkflowInvocation`, the three tools, and `EntryReceipt`. No legacy symbol is
superseded; the only removal is that plugin authors no longer infer
agent-callability from `mode_bindings`.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Model loops entering workflows. | Depth/re-entrancy guards (WFC-01), approval prompt, `handoff_already_pending`. |
| Over-broad `agent_callable` on user workflows. | Default false; `create_workflow` templates require an explicit, reviewed `when_to_use` before it can set true (WFC-06). |
| Approval fatigue in Safe mode. | One prompt per entry; the child's own phases keep their existing approval behaviour. |
