# `code_plan` workflow structure

`code_plan` is the repository's reference implementation for a workflow whose
progress is controlled by typed state and explicit phase tools. Its runner is
`src/agenthicc/workflows/code_plan/runner.py`; its state and cross-phase data
are in `state.py`; and its phase-local tools are in `phase_tools.py`.

## State and context

`CodePlanState` is the complete routing vocabulary:

```text
PLAN ───────────────→ EXECUTE ─────────────→ REVIEW ─────────────→ SUMMARIZE ─→ COMPLETE
  │                      │                    │
  ├─ exit_code_plan ─→ EXITED                └─ reject_review ─→ EXECUTE
  └─ retry until finalize_plan
```

`COMPLETE`, `EXITED`, and `FAILED` are terminal states. The runner never
infers a transition from the wording of an assistant response. It observes
events set by the phase's tools or returns `FAILED` after the phase's bounded
retry loop is exhausted.

The complete phase-state contract is:

| State | Meaning | Tool-controlled transitions |
|---|---|---|
| `PLAN` | Explore the task and prepare an approved plan | `finalize_plan()` → `EXECUTE`; `exit_code_plan()` → `EXITED`; otherwise retry `PLAN` |
| `EXECUTE` | Apply the approved plan | `mark_execute_complete()` → `REVIEW`; failed required commands or exhausted retries → `FAILED` |
| `REVIEW` | Inspect the implementation and run verification | `approve_review()` → `SUMMARIZE`; `reject_review()` → `EXECUTE`; exhausted retries → `FAILED` |
| `SUMMARIZE` | Report the structured outcome | one bounded summary turn → `COMPLETE` |
| `COMPLETE` | Successful terminal state | none |
| `EXITED` | Clean early exit for a non-implementation request | none |
| `FAILED` | Permanent error or bounded retry exhaustion | none |

`CodePlanContext` is the mutable, typed handoff between phases:

| Field | Owner and purpose |
|---|---|
| `intent` | Original user request, retained in every phase prompt |
| `run_id` | Durable identity for the workflow run |
| `plan` | Approved plan written by `finalize_plan()` |
| `execute_mode` | `Safe` or `Yolo`, selected by plan approval and applied to `EXECUTE` |
| `execute_summary` | Implementation evidence written by `mark_execute_complete()` |
| `review_summary` | Verification evidence written by `approve_review()` |
| `rejection_reason` | Required corrections written by `reject_review()` |
| `fail_reason` | Permanent error or exhausted-gate diagnostic |
| `command_outcomes` | Structured results used to gate required command execution |
| `shared_memory` | One `ShortTermMemory` instance shared across all phases and retries |

The same context object is passed to each phase function. Phase tools write
structured values into it indirectly through their closure data, and the phase
method copies those values into the corresponding context field before
returning the next state.

## Memory lifecycle and memory tools

`CodePlanRunner.run()` creates one `ShortTermMemory` using
`execution.effective_usable_budget()` and stores it in `CodePlanContext`. Every
call to `_run_turn()` passes that exact object as `session_memory` to
`_run_agent_turn()`, so the planner, executor, reviewer, and summarizer share
the same conversation memory. Transport retries snapshot and restore this
memory as part of the normal agent-turn retry boundary. When an approval
service is active, `_run_turn()` calls `ensure_valid()` before the phase begins.

`CodePlanRunner._base_tools()` extends the mode/capability-filtered project and
MCP tools with four memory tools. They remain available even when a backing
router or index is absent; in that case the tool returns a structured
availability error instead of raising:

| Tool | Purpose |
|---|---|
| `memory_write(key, value, scope, namespace, ttl_seconds)` | Store decisions or context in session, project, or global memory; TTL applies to session scope |
| `memory_read(key, scope, namespace)` | Retrieve a previously stored value |
| `semantic_search(query, top_k)` | Search semantically indexed prior agent output and decisions |
| `publish_artifact(content, content_type)` | Store idempotent, content-addressed project artifacts |

Short-term memory is the live context passed to the model. `memory_write` and
`memory_read` use the routed session/project/global tiers when those services
are configured, while `semantic_search` uses the optional `SemanticIndex`.
These tools preserve useful decisions across turns, but they do not authorize
phase transitions: the phase-specific transition tool must still be invoked.

The legacy `resume()` path creates a fresh `ShortTermMemory` for the resumed
run. It restores the saved plan from the completed plan output; it does not
pretend that the old in-process short-term object is durable. Durable values
must be written through the memory router or artifact store when they need to
survive process or session boundaries.

## The runner loop

`CodePlanRunner.run()` creates the context, publishes the workflow-start event,
and drives the state machine with one explicit loop:

```python
state = CodePlanState.PLAN
while not state.is_terminal:
    match state:
        case CodePlanState.PLAN:
            state = await self._plan(ctx)
        case CodePlanState.EXECUTE:
            state = await self._execute(ctx)
        case CodePlanState.REVIEW:
            state = await self._review(ctx)
        case CodePlanState.SUMMARIZE:
            state = await self._summarize(ctx)
```

Before and after each phase, the runner emits structured phase events and
updates `app_state.workflow_run`. The `match` is intentionally explicit: a
phase transition is visible in source, easy to test, and cannot silently route
through a misspelled string or an implicit truthy value.

The phase methods are the executable phase implementations—not merely labels
in the plugin definition:

```text
_plan(ctx)      -> CodePlanState.EXECUTE | EXITED | FAILED
_execute(ctx)   -> CodePlanState.REVIEW | FAILED
_review(ctx)    -> CodePlanState.SUMMARIZE | EXECUTE | FAILED
_summarize(ctx) -> CodePlanState.COMPLETE
```

`run()` owns routing and lifecycle events; each method owns the work and retry
policy for its phase. This separation makes it possible to test a phase in
isolation by supplying a context and a mocked phase tool invocation.

## Phase functions and turn budgets

Each phase is a Python method that returns the next `CodePlanState`. The method
owns its retry loop and calls the agent with a phase-specific maximum number of
agent sub-turns:

| Phase | Tool gate | Agent turns | Successful next state |
|---|---|---:|---|
| `PLAN` | `finalize_plan()`; `exit_code_plan()` may end early | 20 | `EXECUTE` or `EXITED` |
| `EXECUTE` | `mark_execute_complete()` | 40 | `REVIEW` |
| `REVIEW` | `approve_review()` or `reject_review()` | 8 | `SUMMARIZE` or `EXECUTE` |
| `SUMMARIZE` | no transition tool; one bounded summary turn | 4 | `COMPLETE` |

The retry caps are separate from the per-call turn budgets. A phase can be
given several bounded attempts when the agent stops without invoking its
handoff tool. If a permanent agent/transport error occurs, the phase records a
typed failure reason and returns `FAILED` immediately. This keeps an
unresponsive or repeatedly incomplete phase from running forever.

The current implementation uses up to 10 phase attempts for `PLAN`, `EXECUTE`,
and `REVIEW`. Within each attempt, the `max_turns` value limits the agent's
tool-call/response sub-turns. `SUMMARIZE` deliberately has no retrying
transition gate: it catches a summary-agent error, logs it, and still returns
the terminal `COMPLETE` state because summary prose cannot change the verified
execution or review result.

### Phase prompt ownership

The phase methods are the executable prompt source for `code_plan`. They call
`CodePlanRunner._run_turn()` with phase constants such as `_PLAN_PROMPT`,
`_EXECUTE_PROMPT`, and `_REVIEW_PROMPT`; they do not obtain their runtime
prompt from `CodePlan.phases[*].system_prompt_override`. Those `PhaseSpec`
values remain useful metadata for registry inspection, authoring, display, and
generic-runner-compatible descriptions, but the specialized
`CodePlanRunner` is the executable source of truth. Composite runners should
use the public `run_phase(system_prompt=...)` API and treat that explicit
argument as authoritative.

## Phase-local transition tools

The runner creates a fresh `asyncio.Event` and data dictionary for each phase
attempt, then passes them to a tool factory:

```python
phase_event = asyncio.Event()
phase_data: dict[str, object] = {}
tools = make_executor_tools(phase_event, phase_data)
await self._run_turn(..., tools=tools, ...)

if phase_event.is_set():
    summary = phase_data["summary"]
```

This closure is the handoff contract. It lets the tool invocation control
progression without parsing free-form text from the assistant. The tool also
returns a short result to the agent so the agent knows whether the handoff was
accepted and what it should do next.

### Planning

`make_planner_tools()` provides:

- `request_plan_approval(plan)`, which sends the plan to the approval service
  and records whether the latest decision was granted; and
- `finalize_plan(plan)`, which refuses to set the transition event until the
  approval gate is true.

Calling `finalize_plan()` before approval returns an actionable error telling
the agent to call `request_plan_approval()` first. A successful finalization
stores the plan and moves the runner to `EXECUTE`. `exit_code_plan()` is an
optional clean exit for questions or other intents that do not require a code
plan.

For interactive code-plan approval, the plan-review overlay presents two
explicit choices: `Approve - Safe` and `Approve - YOLO`. The selected mode is
stored with the finalized plan and passed to every execution turn; Safe keeps
per-action approval gates enabled, while Yolo permits the approved executor to
run without those prompts. The choice is also included in the phase metadata
used by the resume path. Other plan-review requests, such as
`create_workflow`'s design review, retain the original approval options.

### Execution

`make_executor_tools()` provides `mark_execute_complete(summary)`. It records
the implementation summary and sets the event. If the agent stops without
calling it, the runner sends a reminder on the next bounded attempt. The phase
also checks structured command outcomes; failed, timed-out, cancelled, or
orphaned required commands prevent a successful handoff.

### Review

`make_reviewer_tools()` provides two mutually exclusive transitions:

- `approve_review(summary)` routes to `SUMMARIZE`; and
- `reject_review(reason)` records the reason and routes back to `EXECUTE`.

The review prompt requires one of these tools. The next execution attempt gets
the retained plan and review context, so the agent can address the rejection
instead of starting with an unexplained retry.

### Tool result and failure contract

The runner checks the event after each agent turn. If no transition tool fired,
the phase sends a reminder on the next attempt. If a tool refuses a
prerequisite, its result is returned to the agent as structured tool output;
the result should name the failed prerequisite and the correction to make.
For example, `finalize_plan()` returns an error directing the agent to obtain
approval with `request_plan_approval()` first. Execution additionally checks
structured command outcomes after the agent turn, so a command failure cannot
be hidden by a successful-looking prose response.

### Summary

`_summarize()` is deliberately a terminal reporting phase. It receives the
structured context produced by the earlier gates, performs one bounded agent
turn, and always returns `COMPLETE` unless cancellation interrupts the run.
The summary text is not used to decide whether implementation is complete.

## Adding a phase safely

For a new state-machine workflow, copy the ownership boundaries rather than
copying only the prompts:

1. Add a state enum member and document all valid outgoing transitions.
2. Add the phase's typed context fields.
3. Create phase-local tools that write structured data and set an event only
   after their prerequisites are satisfied.
4. Give the phase function its own bounded retry loop and explicit turn budget.
5. Add a `case` to the runner's `while not state.is_terminal` loop.
6. Return actionable tool errors containing both the failed prerequisite and a
   concrete fix the agent can apply on its next attempt.
7. Test successful transitions, missing-tool retries, rejected prerequisites,
   rejection loops, terminal failures, and resume behavior.

The built-in `create_workflow` authoring runner follows this same structure with
the phases `DESIGN`, `GENERATE`, `VALIDATE`, and `SUMMARIZE`. Its design phase is
gated on human approval like `code_plan`'s plan phase, its generate phase writes
the workflow package directly (`<name>/runner.py` plus optional local helpers),
and its validate phase imports that package
deterministically before the agent votes — a failing report overrides an
approval and routes back to generate. It stages and publishes nothing.

## Definition metadata versus runtime behavior

`code_plan/definition.py` exposes the workflow to the registry and UI through
`PhaseSpec` values: phase names, role labels, prompts, `max_turns`,
`max_iterations`, completion requirements, mode overrides, and graph edges.
`CodePlanRunner` is the specialized runtime selected by `CodePlan.build_runner()`
and owns the authoritative Python state machine, prompts, phase functions,
retry loops, memory setup, and transition-tool injection. When changing
`code_plan`, inspect both files and keep the displayed `PhaseSpec` contract in
sync with the executable runner.

Composite workflows can reuse the same machinery through
`CodePlanRunner.run_phase()`. It accepts an intent, phase text, system prompt,
mode, turn budget, and optional shared memory; pass `ctx.shared_memory` when an
extension phase must see the same context as the built-in phases.
