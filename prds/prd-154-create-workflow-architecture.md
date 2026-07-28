---
title: "PRD-154: create_workflow Architecture"
status: Implemented
version: 1.0.0
created: 2026-07-28
related_prds:
  - PRD-100  # code_plan architecture — the reference this workflow is modelled on
  - PRD-116  # WorkflowPlugin registry and runner dispatch
  - PRD-147  # Workflow-Native Extension Authoring (superseded authoring flow)
  - PRD-152  # Agent-Executable create_* Authoring (superseded authoring flow)
  - PRD-153  # create_workflow reliability (superseded authoring flow)
supersedes:
  - PRD-147
  - PRD-152
  - PRD-153
tags:
  - workflows
  - authoring
  - state-machine
  - validation
---

# PRD-154 — `create_workflow` Architecture

This document is a reference guide, not an implementation spec. It describes the
runtime behaviour of the `create_workflow` meta-workflow — the workflow
downstream users invoke to author their own custom workflows.

`create_workflow` was rebuilt from a clean slate and is modelled directly on
`code_plan` (see [PRD-100](prd-100-code-plan-architecture.md)). It **supersedes**
the `interpret → design → execute → summarize` authoring flow described in
PRD-147, PRD-152, and PRD-153; those PRDs are history, not current API docs.

---

## 1. Shape

The four structural properties mirror `code_plan` exactly:

| Property | Where it lives |
|---|---|
| Outer loop evolving phase state | `CreateWorkflowRunner.run()` — `while not state.is_terminal` + `match` |
| Inner loop driving agent turns | one bounded async method per phase (`_design`, `_generate`, `_validate`, `_summarize`) |
| Transitions only via tool calls | `phase_tools.py` closures over an `asyncio.Event` + data dict |
| Context capturing each phase's artefact | `CreateWorkflowContext.artifacts: dict[str, PhaseArtifact]` |

Nothing else changes state. A phase method returns the next
`CreateWorkflowState`; the outer loop emits the phase events, updates
`app_state.workflow_run`, and dispatches again.

## 2. State graph

```
DESIGN    ──(finalize_design after approval)──► GENERATE
      ↺ ──(no transition tool called)───────► DESIGN
        ──(exit_create_workflow)────────────► EXITED

GENERATE  ──(mark_generation_complete)───────► VALIDATE
      ↺ ──(no transition tool called)───────► GENERATE

VALIDATE  ──(approve_workflow AND report ok)─► SUMMARIZE
        ──(reject_workflow)─────────────────► GENERATE
        ──(approve_workflow, report failed)─► GENERATE   ← override

SUMMARIZE ─────────────────────────────────► COMPLETE
```

Terminal states are `COMPLETE`, `EXITED`, and `FAILED`. `FAILED` is reached when
a phase exhausts its attempt budget without a transition tool call, when a
permanent turn error is raised, or when the repair budget is spent.

## 3. Phase behaviour

**DESIGN** is read-only and human-gated, the analogue of `code_plan`'s plan
phase. `request_design_approval(design, workflow_name)` raises a
`kind="plan_review"` approval request; the response is recorded in a gate that
`finalize_design(design, workflow_name)` checks. A later denial closes the gate
again, so an unapproved design cannot be handed off. `approval_svc=None`
auto-approves for headless runs and tests. The phase also receives the five
read-only authoring-surface inspection tools and `ask_user`.

**GENERATE** runs with `mode="Auto"` so the write tools are available (the same
mechanism `code_plan`'s execute phase uses; the original mode is restored in a
`finally`). The agent writes the plugin source to `.agenthicc/workflows/<name>.py`
and calls `mark_generation_complete(summary, path)`. The runner never writes,
copies, stages, or publishes the file itself.

**VALIDATE** is the one place the runner adds judgement of its own. Before the
agent is asked for anything, `validate_workflow_file()` imports the generated file
the way `load_python_workflows` will and checks it against the real
`WorkflowPlugin` contract. The rendered report is injected into the agent's
prompt. The agent must still call `approve_workflow` or `reject_workflow` — the
transition is always a tool call — but an approval is **overridden** when the
report failed, and the run routes back to GENERATE with the concrete errors
attached. A workflow that does not import can never be accepted.

**SUMMARIZE** is a single turn that always returns `COMPLETE`; a turn error is
logged, not propagated, exactly as in `code_plan`.

## 4. Deterministic validation

`validate_workflow_file(path, *, expected_name, root)` refuses without importing
when the path resolves outside `root`, is missing, is a directory, is not `.py`,
is underscore-prefixed, or is empty. It reports syntax errors with a line number
and import failures with the exception type. On a successful import it requires a
`WorkflowPlugin` subclass with a non-empty, non-reserved name matching the
approved design, a description, list `mode_bindings`, a `build_params({})` that
returns a `WorkflowParams`, and a consistent phase graph (unique names, positive
`max_turns`, resolvable `next`/`on_reject`/`on_error`). Unreachable phases,
unknown `agent_type` / `output_schema`, and a filename that differs from the
workflow name are warnings.

Importing agent-written code is the same trust model the workflow loader already
uses; the containment check against `root` is what keeps it inside the workspace.

## 5. The generated workflow ships its own runner

The authoring prompts, the declarative phase prompts, and the inspection tools all
require the generated workflow to contain its own state-machine runner rather than
a bare `PhaseSpec` graph — the same shape as `code_plan` and this workflow.
`describe_runner_pattern()` returns the checklist (state enum with `is_terminal`,
typed context, one bounded async method per state, `while not state.is_terminal` +
`match` driver, `resume()`, event-setting phase tool factories, `build_runner()`),
and `show_example_workflow()` returns a complete working runner by default;
`show_example_workflow("declarative")` is the opt-in runner-less fallback.

To make that shape expressible through public API, `CodePlanRunner.run_phase()`
gained a `tools` parameter: a custom runner drives its own state machine and passes
each phase's transition tools into the one turn it runs, then checks its own
`asyncio.Event`. `validate_workflow_file` warns when a multi-phase workflow
inherits `build_runner()`, and errors when the runner it ships is abstract or
missing `run`/`resume`.

## 6. Budgets

Two previously inert configuration keys now drive the loops:

- `execution.authoring_max_generation_attempts` — inner-loop attempts per phase,
  and the ceiling on `VALIDATE → GENERATE` repair cycles;
- `execution.authoring_max_phase_turns` — LLM sub-turns within one agent turn.

Both are clamped to at least 1 so a hostile TOML value cannot skip a phase.

A third setting is what makes generation work at all:
`execution.max_output_tokens` (default 16384) is the completion ceiling for one
LLM round-trip, passed through to lauren-ai's `AgentConfig.max_tokens_per_turn`.
lauren-ai defaults it to 4096, which silently truncated the `write_file` call
carrying the workflow source: the partial tool call was discarded, the sub-turn
produced nothing, and GENERATE retried until the budget was spent with no visible
cause. Alongside the higher ceiling, a `max_tokens` stop reason now emits a system
notice, and the generate prompt instructs a chunked `write_file` + `append_file`
write so a large file lands regardless of the ceiling.

## 7. Declarative metadata versus runtime behaviour

`create_workflow/definition.py` exposes the workflow to the registry and the TUI
through `PhaseSpec` values: phase names and order, per-phase prompts, the
`Auto` mode override on generate, and the `validate → generate` rejection edge.
`CreateWorkflowRunner` is the specialized runtime selected by
`CreateWorkflow.build_runner()`, and its state machine follows exactly that graph
— `_PHASE_INDEX` and `total_phases` are asserted against `CreateWorkflow.phases`
in the unit tests so the two cannot drift.

## 8. Public surface

`agenthicc.workflows.create_workflow` exports `CreateWorkflowState`,
`CreateWorkflowContext`, `PhaseArtifact`, `CreateWorkflowRunner`,
`CreateWorkflow`, `CreateWorkflowParams`, `ValidationReport`,
`validate_workflow_file`, `validate_workflow_name`, `make_design_tools`,
`make_generation_tools`, `make_validation_tools`, and `make_inspection_tools`.
All are documented in `llms-full.txt`.

## 9. Test coverage

| Layer | File | Focus |
|---|---|---|
| Unit | `tests/unit/test_create_workflow.py` | state terminality, artefact bookkeeping, every phase-tool rejection path, all four inspection tools against live metadata, every validation error and warning, plugin metadata, and each phase method's success / retry / exhaustion / cancellation path |
| Integration | `tests/integration/test_create_workflow_integration.py` | real `EventProcessor`, real `ApprovalService` approve and deny, real TOML through `load_config`, and the real loader/registry discovering the generated plugin |
| E2E | `tests/e2e/test_create_workflow_state_machine_e2e.py` | the whole machine driven by a `MockTransport` issuing real tool calls: the real `write_file` tool writes the file, the runner imports it, the repair loop runs, and a wrong approval is overridden |
