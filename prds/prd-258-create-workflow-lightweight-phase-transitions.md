---
title: "PRD-258: Lightweight, Record-Only Phase Transitions for Generated Workflows"
status: Implemented
version: 1.0.0
created: 2026-11-01
scope: "create_workflow authoring guidance, transition-tool helper, canonical example, and generated-workflow validation"
related_prds:
  - PRD-154 # create_workflow architecture
  - PRD-174 # tool-aware authoring and generated-workflow validation
  - PRD-179 # generated-workflow phase annotations and checkpoints
  - PRD-181 # summary-only make_book transitions
  - PRD-173 # recoverable workflow errors

tags:
  - workflows
  - create-workflow
  - phase-transitions
  - llm-ergonomics
  - validation
---

# PRD-258 — Lightweight, Record-Only Phase Transitions for Generated Workflows

## 1. Summary

Make `create_workflow` author small phase-transition tools for the custom
workflows it generates. Each generated transition tool has exactly one
provider-facing argument, `summary: str`; the selected tool identifies a fixed
outcome or branch. Its implementation records that outcome and a bounded summary
and signals the runner. It does not inspect or verify the work being handed off.

A phase agent performs its assigned work using ordinary tools before it submits
the transition. When independent verification is required, it belongs in a
separate phase or an explicit runner-owned gate—not inside the transition tool.

This PRD changes the generated-workflow authoring contract only. It does not
remove `create_workflow`'s own design approval, deterministic package validation,
smoke check, validation approval, or safe publication gates.

## 2. Evidence-backed current state

The current `create_workflow` runtime has a sound event-driven state-machine
boundary: generated custom runners use phase-local `@tool_control` callables,
set an `asyncio.Event`, and let the runner commit the next typed state and
checkpoint boundary. Agent prose alone does not advance a phase. This remains
the required lifecycle model (PRD-154 and PRD-179).

The authoring contract does not yet make transition-tool ergonomics or behavior
specific:

- `create_workflow/definition.py` asks the design and generation agents to
  create event-backed transition tools, but sets no parameter-count rule and
  does not say that the tool body must avoid verifying work.
- `create_workflow/inspection_tools.py::describe_transition_tool_pattern()`
  documents imports and decorators, not the schema or allowed tool-body work.
- The canonical runner example has `submit_release_plan(plan)`,
  `release_passed(summary)`, and `release_blocked(blocker)`. The bodies are
  small, but the schemas and handoff semantics are inconsistent and the example
  does not establish a record-only contract.
- `create_workflow/validation.py::_check_transition_tool_contract()` checks the
  `tool_control` import and decorator order. It does not enforce a concise
  signature or a side-effect-free transition body.
- `create_workflow/smoke.py` fabricates arguments from any required signature
  and exercises a transition, but does not enforce or prove the desired schema
  and record-only behavior.

PRD-181 establishes summary-only transition schemas for `make_book`, but its
transition gates deliberately verify book artifacts. That is a separate
workflow-specific contract. PRD-258 adopts the small schema principle, not
`make_book`'s artifact-verification behavior.

## 3. Problem

A generated workflow author currently has to invent each transition schema and
its behavior. That encourages transition tools to accept runner-owned values
(paths, indexes, counts, status flags, structured details), duplicate large
artifacts in tool arguments, and mix work verification with a control-flow
signal. The resulting schemas are harder for the model to call correctly, and
verification logic in a tool can be duplicated, incomplete, or mistaken for an
independent check.

The desired ownership rule is:

> A phase-transition tool records the agent's selected outcome and one short
> summary, then signals the runner. It makes no claim about whether the work or
> evidence is correct.

The outcome is a workflow control signal, not proof. If correctness must be
established independently, an explicit verifier must own that decision.

## 4. Goals

1. Give every generated phase-transition tool one consistent, low-arity schema.
2. Keep transition tool bodies limited to bounded summary recording and event
   signaling; prohibit domain or artifact verification in those bodies.
3. Make outcome selection explicit through the called tool, not a model-supplied
   `next_phase`, boolean, status, or arbitrary decision field.
4. Keep large outputs in workspace artifacts or typed workflow context, not in
   transition arguments.
5. Teach and validate the same contract through create_workflow prompts, its
   inspection surface, the canonical example, static validation, and smoke tests.
6. Preserve event-only transitions, typed state ownership, checkpoint ordering,
   human approval, workspace policy, and create_workflow's independent package
   validation and publication safeguards.

## 5. Non-goals

- Changing the `create_workflow` meta-workflow's own tools in
  `create_workflow/phase_tools.py`; their approval and generation-receipt guards
  remain as implemented.
- Removing or weakening generated-package import validation, workflow smoke
  checks, validation-agent approval, or safe publication.
- Making prose, a file write, a test result, or a transition summary advance a
  phase without a successful control-tool call.
- Verifying the truth of an agent-supplied summary inside the transition tool.
- Requiring every workflow to have a custom runner when all phases are
  unconditional single turns.
- Passing complete reports, file contents, evidence inventories, paths, counts,
  or phase indexes through transition arguments.
- Adding a compatibility adapter for old generated transition signatures.

## 6. Canonical transition contract

### 6.1 Provider-facing schema

Every generated phase-transition tool has exactly one required argument:

```python
async def complete_phase(summary: str) -> dict[str, object]: ...
```

The emitted schema is equivalent to:

```json
{
  "type": "object",
  "properties": {"summary": {"type": "string"}},
  "required": ["summary"],
  "additionalProperties": false
}
```

The summary is trimmed, must be non-empty, and is bounded to at most 4,000
characters before it is placed in workflow context or a checkpoint. No optional
`details` object or other provider-facing argument is allowed. The fixed result
(`complete`, `approve`, `request_changes`, `blocked`, etc.) is encoded by the
specific tool's name/closure, never supplied as an arbitrary argument.

For a phase with multiple routes, expose one transition tool per route, each
with the same `summary: str` schema. For example:

```python
approve_review(summary: str)
request_changes(summary: str)
```

The runner maps the chosen tool to a declared state edge. It does not infer a
route from prose or from summary wording.

### 6.2 Record-only semantics

Generated transition callables use a canonical, tested
`record_phase_transition(event, data, *, outcome, summary)` helper. The wrapper
captures its fixed outcome and delegates to that helper. The helper may only:

1. normalize and bound the summary;
2. record the fixed outcome and summary in the phase-local handoff data;
3. set the phase's `asyncio.Event`; and
4. return a small structured acknowledgment.

It performs no filesystem reads or writes, shell/process commands, network or
browser/MCP calls, artifact parsing, test execution, evidence inspection,
criteria comparison, approval request, or correctness judgment. It receives no
workspace, runner, artifact, or external-service dependency. A one-shot guard
may prevent a second successful handoff from overwriting the first result; this
is transition-integrity handling, not work verification.

Structural input handling (string type, non-empty trimmed summary, size bound)
is permitted. A successful return means only “the agent submitted this
transition,” not “the agent's work passed.”

### 6.3 Ownership of work and verification

- The phase agent completes its task with the normal tools available in that
  phase, then submits the concise summary.
- Large or structured outputs are written to workspace artifacts or retained in
  typed runner context. Paths and other runner-owned values are derived or
  loaded by the runner; the agent does not repeat them as transition arguments.
- A verification task is represented by a distinct verification phase or an
  explicitly documented runner-owned gate. That owner examines the relevant
  evidence and decides whether to continue, retry, or branch.
- A transition tool must not conceal such a gate by checking the summary,
  reading a file, running tests, or calling an approval service before setting
  its event.
- Human approval remains an explicit approval-service/runner concern. The
  generated transition tool does not request or validate approval itself.

## 7. Functional requirements

### FR-1 — Shared helper

Add a small supported workflow helper for recording a phase transition. Its
callable is not itself a provider tool; generated `@tool_control`/`@tool()`
wrappers provide the one-argument provider schema and delegate to it. The helper
must be deterministic, bounded, JSON-safe, and covered by unit tests for success,
empty/oversized summaries, and repeated calls. Its implementation must not
accept callbacks or objects that could perform verification or I/O.

If the helper is exported as a supported extension API, document its public
symbol in `llms-full.txt` and keep the signature stable. Do not add a second
workflow runtime or transition registry.

### FR-2 — Authoring instructions

Update the design and generation contracts in `create_workflow/definition.py`
and `create_workflow/runner.py` so they require the schema and behavior in §6.
The design must name each phase transition and its fixed route; the generation
agent must implement each as a one-summary wrapper over the shared helper. State
explicitly that:

- transition arguments contain only `summary`;
- the function selected represents the branch/outcome;
- the function only records and signals, and must not verify work; and
- any required verification is a separate phase or runner-owned gate.

Keep the existing `tool_control` import/decorator rule, event-only state changes,
runner/checkpoint contract, and parent workspace policy. Do not weaken the
instruction that prose never advances a workflow.

### FR-3 — Inspection and canonical example

Expand `describe_transition_tool_pattern()` to return the exact schema, the
record-only rule, the summary bound, branch examples, and prohibited operations.
Replace inconsistent parameter shapes in `show_example_workflow("runner")`
with the canonical one-summary pattern and demonstrate that any verification
happens in phase work or a separate runner-owned step. Keep the example
checkpoint-safe and ensure its phase outputs remain available after resume
without relying on a large transition payload.

The declarative example remains valid only for unconditional phases without
phase-local transitions. `describe_runner_pattern()` must state the narrow tool
contract wherever it describes transition factories.

### FR-4 — Validation contract

Extend strict generated-workflow validation to enforce the authoring contract:

- every `@tool_control` phase-transition callable has exactly one required
  provider-facing parameter named `summary`, annotated `str`;
- its body delegates to the supported record helper with a fixed outcome and
  does not perform additional calls or work;
- transition wrappers do not accept `details`, artifacts, paths, indexes,
  booleans, arbitrary state names, or extra keyword arguments; and
- the helper import resolves to the supported agenthicc implementation.

Emit bounded actionable errors naming the offending callable and expected
shape. Do not inspect or execute arbitrary generated tool bodies merely to
validate them; use the existing contained import/static-analysis boundary and
smoke harness. This structural policy applies to generated phase-transition
tools, not to ordinary workflow tools or the built-in create_workflow approval
and validation tools.

### FR-5 — Smoke and runtime coverage

Update the generated-workflow smoke contract to exercise a compliant transition
using only `summary`, prove that a prose-only turn does not advance, and prove
that the selected tool records its fixed outcome and bounded summary before the
runner checkpoints the next state. Include a multi-route fixture showing that
the selected tool determines the declared branch. No external service or real
filesystem verification is part of this transition-tool smoke.

The helper's test suite must demonstrate that it cannot read or mutate
artifacts: its inputs are limited to the event, handoff mapping, fixed outcome,
and summary. Existing smoke checks for package loading, checkpoints, memory
reattachment, and error propagation remain in force.

### FR-6 — Documentation and examples

Update the create_workflow reference, generated-workflow guidance, tests, and
`llms-full.txt` if the helper becomes public. Explain the boundary between
record-only transitions and independent validation so “no verification” cannot
be misread as removing create_workflow's own package validation or a workflow's
separate verifier phase.

## 8. Data flow

```text
phase agent performs its assigned work with ordinary tools
  -> writes large outputs to workspace artifacts / runner-owned context
  -> calls complete_phase(summary) or a fixed-outcome branch tool
       -> record_phase_transition(event, data, fixed_outcome, bounded_summary)
       -> event.set()
  -> runner reads the phase-local result and applies the declared edge
  -> runner commits typed state/output and checkpoint boundary
  -> next phase starts

when independent verification is required:
  work phase -> record-only transition -> verification phase or runner-owned gate
              -> verified route -> checkpoint boundary -> next phase
```

The outer state loop, phase annotation, boundary checkpoint, resume cursor,
`WorkflowConfig` ownership, and shared session memory remain as specified by
PRD-154 and PRD-179.

## 9. Acceptance criteria

1. `describe_transition_tool_pattern()` and the canonical example both show a
   one-required-`summary: str` schema and explicitly say that transition tools
   record the agent's submission without verifying it.
2. Every transition wrapper in the example uses the supported helper, has no
   other provider-facing argument, and performs no work beyond delegation.
3. A generated transition schema with `plan`, `path`, `details`, `decision`, or
   any second argument is rejected by strict validation with an actionable
   message; a compliant summary-only wrapper passes.
4. A transition body that checks artifact existence, reads content, runs a
   command/test, calls a network/browser/MCP client, compares evidence, or
   requests approval is rejected as non-conforming. Ordinary non-transition
   workflow tools are not subject to this rule.
5. Calling a valid transition records only the fixed outcome and bounded summary,
   sets its event once, and does not touch the workspace. Blank summaries do not
   advance; oversized summaries are bounded before checkpointing.
6. For a branching phase, each outcome tool has the same summary-only schema and
   the runner routes according to the selected tool, not model-supplied state or
   summary text.
7. A prose-only fake turn remains in the current phase; a successful tool call
   advances through the existing typed state and checkpoint boundary.
8. A separate verifier can reject or retry work without putting verification
   inside the preceding transition function.
9. `create_workflow` still rejects invalid generated packages and does not
   publish before its existing deterministic validation, smoke, and approval
   requirements pass.
10. Existing phase ordering, event-only transitions, workspace enforcement,
    checkpoint/resume, failure finalization, and parent-session identity tests
    remain green.

## 10. Test plan

- **Helper unit tests:** single fixed outcome, trimmed/bounded summary,
  non-empty input, first-call-wins behavior, JSON-safe handoff data, and event
  signaling order.
- **Inspection tests:** exact transition schema guidance, helper example,
  branch examples, prohibited verification operations, and updated runner
  checklist.
- **Static validator tests:** summary-only accepted; extra/missing/wrongly named
  parameters rejected; non-string annotation rejected; wrapper with direct I/O
  or verification rejected; ordinary `@tool_control` import/decorator rules
  retained.
- **Smoke tests:** prose-only non-transition, one accepted transition,
  multi-route selection, recorded handoff and checkpoint order, and no external
  calls.
- **Integration/E2E:** generate a small custom workflow through the real
  create_workflow state machine, inspect the produced tool schemas, validate and
  publish it, then run it through at least one normal transition and one
  separately owned verification/rejection path.
- **Regression:** built-in create_workflow approval and validation behavior,
  `make_book` artifact gates, and UI/UX Pro Max workflow semantics remain
  unchanged.

## 11. Rollout and migration

1. Add and test the shared record-only helper.
2. Update the inspection contract and canonical runner example.
3. Update design/generation prompt requirements to use the helper and summary-only
   schemas.
4. Extend strict static validation and generated-workflow smoke tests to reject
   non-conforming transition wrappers before approval/publication.
5. Update the create_workflow documentation and public LLM reference.

This is a clean authoring-contract change for newly generated workflows. Do not
add a compatibility shim or emit a legacy multi-argument example. Existing
published workflows are not silently rewritten; users who regenerate one receive
the new schema and are subject to the normal validation/publication flow.

## 12. Security and operational considerations

- A transition summary is agent-supplied text, not verified evidence. It must be
  bounded and treated as untrusted content in prompts, logs, and checkpoints.
- The helper must not grant filesystem, network, shell, approval, or other
  capabilities. `@tool_control` remains the only capability marker for a
  state-changing transition call.
- The runner remains authoritative for the current phase, permitted next state,
  checkpoint, and resume cursor. The model cannot choose an undeclared edge by
  passing a state name.
- Any independent correctness or human-approval requirement remains explicit
  and owned by the runner, approval service, or a dedicated verifier phase.
