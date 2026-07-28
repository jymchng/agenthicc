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

`CodePlanContext` is the mutable, typed handoff between phases. It contains the
original `intent`, `run_id`, shared `ShortTermMemory`, the approved `plan`, the
`execute_summary`, the `review_summary`, a `rejection_reason`, a
`fail_reason`, and structured `command_outcomes`. A phase writes its result to
this context; the next phase receives the same object.

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
the phases `INTERPRET`, `DESIGN`, `STAGE`, `VALIDATE`, `REVIEW`, `PUBLISH`, and
`SUMMARIZE`. Its transition validators extend the pattern by checking the
candidate, immutable staged digest, validation report, and explicit approval
before allowing a side effect.
