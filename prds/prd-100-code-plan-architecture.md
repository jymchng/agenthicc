# PRD-100 — code_plan Architecture: How Plan Mode Works End to End

This document is a reference guide, not an implementation spec.  It describes
the complete runtime behaviour of the `code_plan` workflow — from the user
pressing Enter in Plan mode through to the summarise phase completing — with
particular focus on the approval gates and the mode-switching machinery that
makes the planning phase read-only while the execution phase has full tool
access.

---

## 1. Entry point: Plan mode and workflow binding

When the user cycles to **Plan** mode (via Shift+Tab), `ModeManager.set_by_name("Plan")`
writes a `RuntimeMode` onto `AppState.active_mode`.  The Plan `RuntimeMode` has two
key properties set at construction in `mode_manager.py`:

```
blocked_capabilities = {WRITE, GIT_WRITE, EXECUTE, NETWORK}
default_workflow     = "code_plan"
```

`blocked_capabilities` is the hard-block list enforced by `ToolCapabilityGate` on
every tool call for the duration of the mode.  `default_workflow` is the name that
`TUISession.run_turn()` looks up in `WorkflowRegistry` when the user submits a
message; if a definition is found, a `WorkflowRunner` is created and `run()` is
called instead of the direct `_run_agent_turn` path.

---

## 2. WorkflowRunner.run() — the outer control loop

`WorkflowRunner.run(intent)` creates a fresh `ShortTermMemory` shared across all
phases, mints a `run_id`, emits `WorkflowRunStarted` to the kernel event log, and
then enters `_run_phase_loop`.

The phase graph for `code_plan` is linear with one conditional back-edge:

```
plan ──(approved)──► execute ──► review ──(approved)──► summarize
 ▲                                  │
 └──────────────(rejected)──────────┘
plan also loops: on_reject="plan" (up to 5 plan-approval attempts)
```

The loop terminates when `_determine_transition` returns `None` (after `summarize`,
whose `spec.next` is `None`) or when a cap fires.

**Global termination cap**: `len(phases) + 1 = 5` total phase runs.  If this is
reached before a natural exit, the workflow fails with a clear transcript message.

---

## 3. The plan phase — read-only exploration and approval

### 3a. Mode enforcement

The plan phase runs in Plan mode (no `mode_override`).  `ToolCapabilityGate` is a
global hook registered on `AgentRunnerBase` for every phase; it reads
`AppState.active_mode().blocked_capabilities` on each tool call.  Because Plan mode
has `blocked_capabilities = {WRITE, GIT_WRITE, EXECUTE, NETWORK}`, any attempt by
the planning agent to call `write_file`, `run_bash`, `git_commit`, etc. is
immediately aborted with a structured error.  The agent never receives output from
blocked tools.

### 3b. Approval tool injection

Before calling `_run_agent_turn`, `WorkflowRunner._run_phase` injects two extra
tools into the plan phase's tool list via `make_planner_tools()` in
`workflow/phase_tools.py`:

| Tool | Purpose |
|---|---|
| `request_plan_approval(plan)` | Shows the `PlanApprovalOverlay` and suspends until the user responds |
| `finalize_plan(plan)` | Writes the approved plan to `plan_data` and sets `plan_event` |

These tools close over three shared mutable objects:

```python
approval_state: dict        # {"granted": False} — tracks most recent approval decision
plan_data:      dict        # written by finalize_plan when approval confirmed
plan_event:     asyncio.Event  # set by finalize_plan; observed by _run_phase after return
```

The `finalize_plan` tool enforces ordering: it checks `approval_state["granted"]`
before writing to `plan_data`.  If the agent calls `finalize_plan` without first
receiving `approved=True` from `request_plan_approval`, the tool returns an error
instructing the agent to seek approval first.

### 3c. The approval handshake

When the agent calls `request_plan_approval(plan)`:

1. `ApprovalService.request_approval()` is called.  It acquires `self._lock`
   (serialises concurrent approvals), writes the `ApprovalRequest` to
   `AppState.pending_approval`, and `await`s `req.event.wait()` — suspending the
   agent coroutine without blocking the event loop.

2. `AppState.pending_approval` is a `Signal[Any]`.  `TUISession._wire_approval_overlay()`
   subscribes to it.  The subscription fires and inspects `req.kind`:
   - `"plan_review"` → creates a `PlanApprovalOverlay`
   - `"tool"` → creates an `ApprovalOverlay`
   The overlay is shown via `workspace.overlays.show(overlay)`.

3. The user sees the `PlanApprovalOverlay` with the full plan text and three options:
   - **Approve** — allowed=True, no feedback
   - **Reject — add feedback** → enters a text prompt; allowed=False, message=feedback
   - **Approve — add instructions** → enters a text prompt; allowed=True, message=instructions

4. When the user submits, `PlanApprovalOverlay` calls `ApprovalService.respond(allowed, message=…)`.
   `respond()` writes `ApprovalResponse` to `self._response` and calls `req.event.set()`.

5. `ApprovalService.request_approval()` resumes from `await req.event.wait()`, clears
   `pending_approval`, and returns the `ApprovalResponse` to the `request_plan_approval`
   tool, which returns `{"approved": bool, "feedback": str}` to the LLM.

6. `approval_state["granted"]` is updated to match `response.allowed`.

### 3d. Phase exit — finalize_plan

If the agent receives `approved=True` and calls `finalize_plan(plan)`:
- `plan_data["plan"] = plan`
- `plan_event.set()`

After `_run_agent_turn` returns, `_run_phase` inspects `plan_event`:

```python
if plan_event.is_set() and "plan" in plan_data:
    full_text = plan_data["plan"]       # use the structured plan
    # → PhaseOutput(approved=None) → _determine_transition returns spec.next = "execute"
else:
    # finalize_plan was never called
    return PhaseOutput(approved=False)  # → on_reject = "plan" → loops back
```

So the only two exit paths from the plan phase are:
1. `finalize_plan` was called after approval → transitions to execute
2. `finalize_plan` was never called (agent ran out of turns or plan was rejected) → loops back to plan

---

## 4. The execute phase — full tool access via mode override

The execute phase spec has `mode_override = "Auto"`.  Before calling
`_run_agent_turn`, `WorkflowRunner._run_phase` does:

```python
_original_mode = app_state.active_mode()          # Plan mode
mode_manager.set_by_name("Auto")                  # switches to Auto
```

`ToolCapabilityGate` now reads `Auto.blocked_capabilities = frozenset()` — no
tools are blocked.  The agent can freely call `write_file`, `run_bash`, `patch_file`,
etc.

In the `finally` block of `_run_phase` (runs even on exception or cancellation):

```python
app_state.active_mode.set(_original_mode)         # restores Plan mode
```

This means the execute phase is the only window during a `code_plan` run where
write tools are accessible.  Review and summarise both run in Plan mode (no
`mode_override`), keeping them read-only.

The execute phase has no `output_schema` and no approval tools, so `plan_event` is
`None`.  `PhaseOutput.approved` stays `None`, and `_determine_transition` takes
`spec.next = "review"`.

---

## 5. The review phase — read-only verification

The review phase runs in Plan mode (no `mode_override`).  Its `output_schema =
"review_result"` causes `_parse_output_schema` to look for a
`<review>approved</review>` or `<review>rejected: reason</review>` tag in the
agent's output.

```python
if schema == "review_result":
    approved = content.lower().startswith("approved")
    return {"content": content, "approved": approved}
```

`_determine_transition` inspects `output.approved`:
- `True` or `None` → `spec.next = "summarize"`
- `False` → `spec.on_reject = "execute"` (retry the execution phase)

The `max_iterations = -1` (unlimited per-phase) on both execute and review means
the execute→review cycle can repeat until the global phase-run cap fires.

---

## 6. The summarize phase

Runs in Plan mode, `output_schema = "free_text"`, no approval tools.  After it
completes, `spec.next = None` exits the while loop, `wf_run.status = "complete"`,
and `TUISession.run_turn()` fires the PRD-89 auto-reset:

```python
if wf_result.status == "complete" and active_mode.default_workflow is not None:
    mode_manager.set_by_name("Auto")
    notification.set("✓ Workflow complete — switched to Auto mode")
```

---

## 7. The ApprovalGate (Guard mode — separate from plan approval)

`ApprovalGate` is the second global hook, running after `ToolCapabilityGate`.  It
handles the **Guard mode** approval flow for individual tool calls — distinct from
the plan-phase approval overlay.

In Guard mode, `RuntimeMode.approval_required = {WRITE, GIT_WRITE, EXECUTE, NETWORK}`.
`ApprovalGate.before_tool_call()` checks whether the tool's capabilities intersect
`approval_required`.  If they do, it calls `ApprovalService.request_approval()` with
`kind="tool"`, which shows the standard `ApprovalOverlay` (y/a/A/n).

Two memory layers avoid repeated prompts:
- `_remembered_turn` — capabilities approved for the rest of the current turn (key `a`)
- `_remembered_all` — capabilities approved for the rest of the session (key `A`)

`ApprovalService.reset_turn_memory()` clears `_remembered_turn` at the start of
each new agent turn (called from `TUISession.run_turn()`).

The Guard mode gate is completely independent of the plan-phase tools.  In `code_plan`
the session is in Plan mode (not Guard), so `ApprovalGate` finds
`mode.approval_required = frozenset()` and proceeds on every call — the gate is
a no-op.  Only `ToolCapabilityGate` (hard block) and the injected
`request_plan_approval` / `finalize_plan` tools (soft gate via asyncio.Event) are
active during a `code_plan` run.

---

## 8. Shared memory across phases

`WorkflowRunner.run()` creates one `ShortTermMemory(max_tokens=32_000)` at the
start and passes it to every phase via `_run_agent_turn(session_memory=self._shared_memory)`.
This means the agent running the execute phase already has the full planning
conversation in its memory window — it does not need to re-explore the repository.
The review phase agent similarly has both the planning and execution history.

---

## 9. Complete call chain summary

```
User submits message in Plan mode
  └─ TUISession.run_turn()
       └─ WorkflowRunner.run(intent)
            └─ _run_phase_loop
                 ├─ plan phase
                 │    ├─ mode: Plan (blocked: WRITE, EXECUTE, …)
                 │    ├─ tools injected: request_plan_approval, finalize_plan
                 │    ├─ agent calls request_plan_approval(plan_text)
                 │    │    └─ ApprovalService.request_approval()
                 │    │         ├─ AppState.pending_approval.set(req)
                 │    │         ├─ _on_approval_change fires → PlanApprovalOverlay shown
                 │    │         ├─ await req.event.wait()  [agent suspended]
                 │    │         ├─ user responds → ApprovalService.respond()
                 │    │         │    └─ req.event.set()  [agent resumes]
                 │    │         └─ returns ApprovalResponse to tool
                 │    ├─ if approved: agent calls finalize_plan(plan_text)
                 │    │    └─ plan_event.set(); plan_data["plan"] = plan_text
                 │    └─ _run_phase returns PhaseOutput(approved=None) → "execute"
                 │       OR PhaseOutput(approved=False) → "plan" (retry)
                 │
                 ├─ execute phase
                 │    ├─ mode_override="Auto" → ToolCapabilityGate allows all tools
                 │    ├─ agent implements the plan
                 │    └─ PhaseOutput(approved=None) → "review"
                 │
                 ├─ review phase
                 │    ├─ mode: Plan (blocked: WRITE, EXECUTE, …)
                 │    ├─ output_schema="review_result"
                 │    ├─ approved=True → "summarize"
                 │    └─ approved=False → "execute" (retry)
                 │
                 └─ summarize phase
                      ├─ mode: Plan
                      └─ next=None → workflow complete
                           └─ mode auto-reset to Auto
```
