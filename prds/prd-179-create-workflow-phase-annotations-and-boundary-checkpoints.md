---
title: "PRD-179: Phase Annotations and Boundary Checkpoints for Generated Workflows"
status: Implemented
version: 1.2.0
created: 2026-08-28
scope: "create_workflow authoring prompts, generated workflow phase metadata, TUI projections, and phase-boundary checkpoints"
related_prds:
  - PRD-100  # code_plan architecture
  - PRD-154  # create_workflow architecture
  - PRD-156  # resumable plan-mode interrupts
  - PRD-163  # cache-stable workflow prompts and generated workflows
  - PRD-169  # transaction-safe tool-call conversations
  - PRD-170  # workflow resume recovery
  - PRD-173  # recoverable workflow errors
  - PRD-174  # tool-aware create_workflow authoring
  - PRD-175  # runtime AGENTS.md integration
tags:
  - workflows
  - create-workflow
  - custom-workflows
  - tui
  - phase-metadata
  - checkpoints
  - resume
  - durability
---

# PRD-179 — Phase Annotations and Boundary Checkpoints for Generated Workflows

## 1. Executive summary

The create_workflow workflow generates custom workflows for downstream users.
Those workflows are expected to behave like first-class agenthicc workflows:
the TUI must show the active workflow name, phase name, phase position, model,
and iteration while the workflow is running, and a paused or interrupted run
must resume from the exact durable state that existed at its last safe phase
boundary.

The current create_workflow runner has a local _set_phase helper and updates
some of its own UI state. That is not an adequate generated-workflow contract.
An authoring agent can produce a custom runner whose PhaseSpec list describes
the graph but whose runtime never publishes phase metadata to AppState or its
WorkflowRunHandle. It can also persist only phase-entry state, or save a final
checkpoint, without creating a checkpoint after each completed phase. In those
cases the workflow can execute successfully while the TUI displays stale
information and resume loses the latest completed artifact or transition.

This PRD makes the requirement explicit and enforceable. The create_workflow
design and generation agents must be instructed to produce:

1. one canonical ordered phase plan represented by PhaseSpec values;
2. a centralized reconstruct_site-style phase publication helper in the custom
   runner, called by the outer dispatch loop before every agent turn;
3. a complete runtime annotation sent to both the reactive AppState projection
   and the workflow handle; and
4. a typed checkpoint at every phase boundary, including successful completion,
   rejection/retry, interruption, recoverable failure, and terminal completion.
5. a pre-prompt resume reconciliation step that uses durable execution
   evidence, not a transcript summary, to select the phase to run next.

The generated workflow must use the existing workflow handle, checkpoint
codec, session memory, conversation identity, workspace policy, and error
finalizer. The PRD does not introduce a second workflow engine or a second
durability store. It turns already-existing framework contracts into mandatory
authoring guidance, validation rules, and executable tests.

## 2. Evidence-backed current state

The current tree was inspected on 2026-08-28.

| Surface | Current behavior | Gap addressed by this PRD |
|---|---|---|
| create_workflow PhaseSpec list | The built-in authoring workflow declares design, generate, validate, and summarize with ordering and transition metadata | The agent must be told that generated PhaseSpec metadata is the canonical display topology, not merely documentation |
| create_workflow runner | _set_phase updates phase_iteration, WorkflowRunHandle, and AppState for its own phases | The same behavior must be required in every generated custom runner and validated rather than left to agent discretion |
| reconstruct_site runner | _publish_phase derives phase index and total from the authoritative plan, publishes AppState fields, attaches context, and updates the workflow handle | This is the reference implementation pattern for generated runners |
| WorkflowRunHandle.update_phase | Phase entry can persist a checkpoint when a typed context is attached | Phase-entry persistence alone does not prove that the just-completed artifact and selected next state are durable |
| WorkflowRunHandle.persist_context_transition | Specialized runners can persist the typed context after selecting a non-terminal next state | The generated runner must call an equivalent boundary operation after every completed phase and before the next provider turn |
| custom checkpoint codecs | Generated custom workflows are already expected to define both checkpoint codec methods | Validation must ensure the payload includes phase cursor/output data and excludes live resources |
| TUI workflow projection | The UI reads the AppState/workflow-run projection and handle fields | Missing runtime publication leaves the UI with an old phase, wrong total, or no custom-workflow phase |
| resume/restart reconciliation | A resumed run can have an older phase cursor while durable phase receipts and the conversation summary describe a later phase | Resume needs a deterministic durable-state reconciliation step before any phase prompt is built |
| generated workflow prompts | Prompts explain State, context, outer/inner loops, transition tools, memory, and codecs | Prompts do not yet make reconstruct-style annotations and post-phase checkpointing an explicit non-negotiable contract |
| generated workflow validation | Static and smoke validation covers several plugin, transition, cache, workspace, and recovery rules | Validation needs annotation and per-boundary checkpoint evidence |

The existing implementation is evidence for the design, not a new ownership
boundary. The kernel owns domain events and state reduction, AppState owns the
reactive UI projection, WorkflowRunHandle owns workflow lifecycle and durable
checkpoint coordination, and the generated runner owns its typed phase cursor
and workflow-specific artifacts.

## 3. Problem statement

### 3.1 Phase topology can be descriptive rather than executable

PhaseSpec is the framework's metadata contract. The registry and generic TUI
can use it to determine names, order, transition edges, role, turn limits, and
capabilities. A custom runner can nevertheless ignore that metadata at
runtime, dispatch from a second list, or report hard-coded values. The
resulting phase counter can be wrong even though the workflow's business logic
appears to work.

### 3.2 Generated runners may not publish their current phase

The TUI needs an immediate phase publication before the first provider call in
each phase. Without it, a custom workflow can remain labelled as idle, show the
previous phase, omit the total, or display a generic workflow label. A phase
annotation must be a runtime projection, not a sentence in the agent's output.

### 3.3 Phase-entry checkpoints are not post-phase checkpoints

There are two distinct durability moments:

- phase entry, which protects the run if the process disappears while the agent
  is working; and
- phase completion, which protects the output, artifact receipt, transition
  decision, and next-state cursor before another provider turn begins.

A checkpoint written only at phase entry or only at terminal completion leaves a
window in which the latest completed work is not recoverable. Resume can repeat
a completed side effect, lose the selected branch, or show a stale phase.

### 3.4 Resume can restart an already-completed phase

The phase cursor, phase receipts, journal, and transcript summary are related
but are not interchangeable. A restart can rehydrate an old INIT-phase
checkpoint while the durable artifact manifest already records INIT through
design_system as complete and the run is actually ready for BOOTSTRAP. If the
resume path constructs the INIT prompt before reconciling those sources, the
agent is asked to decide between continuing, resubmitting INIT, or resetting.
That question is itself evidence that recovery happened too late.

The resume path must resolve the durable execution position before injecting
any phase prompt or making any provider call. Conversation summaries are useful
context but are not an authoritative phase cursor. A valid checkpoint and
verified phase receipts must prevent a completed phase from being silently
replayed.

### 3.5 The authoring agent needs a precise contract

The generated workflow is written by an LLM. General instructions such as
"make it resumable" are insufficient. The prompts, authoring tools, static
validator, and smoke test must name the required fields, call sites, ordering,
failure behavior, and resume invariants. Otherwise every custom workflow
reinvents a subtly different lifecycle.

## 4. Goals

1. Make every generated workflow publish a complete reconstruct_site-style
   runtime phase annotation before each phase's first agent turn.
2. Make every generated workflow checkpoint the completed phase and its output
   before entering the next phase or making another provider call.
3. Keep PhaseSpec, the typed runner context, the UI projection, and the durable
   checkpoint consistent without creating duplicate sources of truth.
4. Preserve exact resume across interruption, rejection/retry, branch selection,
   recoverable failure, process restart, and repeated resume.
5. Teach the create_workflow design and generation agents this contract with
   stable, cache-eligible instructions and inspectable authoring guidance.
6. Reject or repair generated workflows that omit the required annotation or
   checkpoint lifecycle.
7. Preserve the existing session-wide conversation_id, ConversationStore,
   session memory, workspace policy, capability filtering, tool-only
   transitions, AGENTS.md instructions, and error-recovery contracts.
8. Provide deterministic unit, integration, and end-to-end evidence.

## 5. Non-goals

- Replacing the existing workflow engine, PhaseSpec, WorkflowPlugin,
  WorkflowRunHandle, AppState, checkpoint store, or TUI renderer.
- Adding a second conversation, memory store, event bus, or workflow state
  machine for generated workflows.
- Requiring every business artifact to be copied into a checkpoint. Large
  artifacts remain external and are represented by bounded, validated
  references and digests.
- Guaranteeing a provider prompt-cache hit. This PRD preserves cache
  eligibility by keeping runtime phase values out of stable system prompts.
- Allowing an agent to advance a phase by writing "done" in prose. Transition
  tools remain the only control-plane mechanism.
- Removing existing workflows or breaking old valid checkpoint payloads.
- Treating a UI annotation as proof that a checkpoint was durable.

## 6. Users and primary journeys

### 6.1 Authoring a simple custom workflow

1. A user asks create_workflow for a workflow with several phases.
2. The design agent declares the ordered PhaseSpec graph and the typed context.
3. The generation agent writes a custom runner with one outer dispatch loop,
   one method per non-terminal phase, a centralized phase publisher, and
   checkpoint codecs.
4. Deterministic validation and a bounded smoke run verify the lifecycle.
5. The user approves publication.
6. When the custom workflow runs, the TUI immediately shows the custom
   workflow's current phase, position, total, model, and iteration.

### 6.2 Completing a phase

1. The phase agent performs work and calls its transition tool.
2. The runner records the phase output, artifact references, attempt/iteration,
   and selected next state in its typed context.
3. The runner publishes the next phase projection and durably saves the
   completed-boundary checkpoint before the next agent turn.
4. The TUI can render the new phase and a crash immediately afterward resumes
   from the correct state without redoing the completed phase.

### 6.3 Rejection and repair

1. A review or validation phase calls its reject/retry transition tool.
2. The rejection reason and affected artifacts are stored in context.
3. A checkpoint records the rejection and the retry cursor.
4. Resume or the normal outer loop enters the declared repair phase, with no
   loss of the rejection evidence and no accidental jump over the boundary.

### 6.4 Interrupted or failed execution

1. The user presses the interrupt key or the provider/tool raises a recoverable
   error.
2. The runner preserves the current typed context and the last completed
   boundary.
3. The framework persists the pause or error checkpoint using its normal
   failure finalizer.
4. A later resume rehydrates the supplied context and session memory, restores
   the annotation, and continues from the exact safe state.

### 6.5 Restart after a stale phase cursor

1. A workflow process stops after several phases have produced durable
   receipts, but before the latest phase cursor was refreshed.
2. The resume coordinator loads the checkpoint, manifest, plan version, and
   journal before constructing an agent prompt.
3. It verifies the contiguous completed-phase prefix and selects the earliest
   incomplete/retryable phase. In the reconstruct_site example, the result is
   BOOTSTRAP rather than INIT when INIT through design_system have verified
   complete receipts.
4. It writes a reconciliation checkpoint, publishes the resolved annotation,
   and only then starts the resumed phase.
5. The conversation summary may be retained as dynamic context, but it cannot
   force INIT or override the durable cursor.

## 7. Definitions and invariants

### 7.1 Canonical phase plan

The canonical phase plan is the ordered tuple of non-terminal PhaseSpec values
declared by the generated WorkflowPlugin. It is the source for:

- phase name and display label;
- zero-based phase index;
- total phase count;
- declared next and rejection edges;
- role, capabilities, mode, turn budget, and prompt seed; and
- plugin fingerprint and checkpoint compatibility.

The custom runner MUST derive its index and total from this plan or from one
generated equivalent that is proven to have the same fingerprint. It MUST NOT
maintain an independently edited hard-coded total.

Terminal states are part of the typed state enum and checkpoint schema but are
not counted as executable phase entries unless the framework explicitly
declares otherwise.

### 7.2 Runtime phase annotation

A runtime phase annotation is the structured projection of the phase currently
being entered or executed. It is not stored in the stable system prompt and it
is not inferred from rendered agent prose. It contains at least:

| Field | Meaning |
|---|---|
| workflow_name | Stable plugin/workflow name |
| phase_name | Canonical PhaseSpec name |
| phase_index | Zero-based index in the canonical plan |
| total_phases | Number of executable phases in the canonical plan |
| run_id | Durable workflow run identity |
| intent | Original user intent, passed through the existing projection contract |
| model_id | Effective model for this phase, including a configured override |
| phase_iteration | Monotonic execution iteration, including re-entry/retry |
| phase_attempt | Optional workflow-specific attempt count when supported |
| status | running, waiting, completed, rejected, failed, or paused as applicable |
| plan_version/fingerprint | Optional stable identity used to diagnose topology drift |

Timestamps, transient spinner state, provider request IDs, secrets, and raw
conversation content are not required in the stable annotation and MUST NOT be
used to mutate the cache-stable prompt prefix.

### 7.3 Boundary checkpoint

A boundary checkpoint is a durable, typed snapshot that represents the
workflow after a phase method has selected a transition and all completed
phase output needed for resume has been committed. It includes the state
cursor that will be resumed and is written before the next provider turn.

The checkpoint is distinct from a phase-start checkpoint. A compliant runner
may write both, but a phase-start checkpoint cannot satisfy the post-phase
requirement by itself.

### 7.4 Core invariant

For every phase boundary:

    phase transition tool succeeds
      -> context/output/journal state is updated
      -> completed-boundary checkpoint is durably saved
      -> next phase is published
      -> next provider turn may begin

The implementation may publish a next-phase UI projection before the durable
save if the framework requires immediate display, but it MUST NOT begin the
next provider turn until the checkpoint exists. If saving fails, the runner
must fail closed: it must not silently advance, discard the output, or report
the boundary as durable.

## 8. Proposed architecture

### 8.1 Generated workflow layers

The generated package has four cooperating layers:

1. PhaseSpec declarations define the stable graph and metadata.
2. The typed runner context stores the dynamic cursor, artifacts, outputs,
   retries, questions, and workflow-specific resume data.
3. A centralized phase publication helper projects the current annotation to
   AppState and WorkflowRunHandle.
4. A boundary helper attaches the context and invokes the existing handle
   checkpoint path with a phase-specific reason.

The helpers are conveniences for one source of truth, not a new runtime
abstraction. They must delegate to existing framework methods.

### 8.2 Required outer-loop shape

The generated runner must use one outer loop for state evolution and one
inner turn loop for the LLM:

    create context
    attach context and publish first phase
    while state is not terminal:
        publish current phase before the first turn
        run bounded agent turns
        accept only a successful transition-tool event
        update output, artifacts, history, and next state
        checkpoint the completed boundary
        continue

The phase method must not directly call another phase method. The outer loop
owns dispatch, publication, boundary persistence, and terminal handling.

### 8.3 Reconstruct_site reference pattern

The authoring prompt must direct the agent to study the current
reconstruct_site runner's _publish_phase behavior and reproduce its semantics:

- derive the phase index and total from the authoritative plan;
- obtain the effective phase model;
- call AppState.update_workflow_phase with workflow name, phase name, index,
  total, run ID, intent, and model ID;
- attach the typed context to config.workflow_handle; and
- call workflow_handle.update_phase with phase name, index, and iteration.

The generated helper may have a different private name, but it must have one
central implementation and be called for every phase entry, including resume
and retry entry. It must not copy the reconstruct_site workflow's business
phases or import its private implementation.

### 8.4 Checkpoint codec boundary

The generated WorkflowPlugin MUST implement:

- checkpoint_context_to_payload(context); and
- checkpoint_context_from_payload(payload, memory=None).

The payload MUST contain enough data to recover:

- workflow/run identity and conversation identity as required by the existing
  checkpoint framework;
- the typed current state and canonical phase name/index;
- phase iteration and attempt/retry cursor;
- completed phase history;
- workflow-specific outputs and artifact references/digests;
- rejection, failure, pause, and pending-reentry information; and
- cache diagnostic references and publication/validation state when applicable.

The payload MUST omit session memory objects, ConversationStore instances,
asyncio events, locks, browser/client/provider objects, open file handles,
callbacks, and credentials. The restore method must attach the supplied
session memory object rather than creating a replacement.

## 9. Functional requirements

### FR-1 — Update create_workflow authoring guidance

The stable create_workflow runner guide, design prompt, generation prompt,
template/example, and relevant authoring tool descriptions MUST explicitly
instruct the agent that every generated workflow:

- declares every executable phase as a PhaseSpec;
- derives phase index and total from that declaration;
- has a centralized reconstruct-style phase publication helper;
- calls the helper before every phase's first provider/tool turn and on resume;
- publishes to both AppState and WorkflowRunHandle;
- attaches the typed context before publishing/persisting;
- records the effective phase model and phase iteration;
- creates a boundary checkpoint after every phase; and
- never starts the next phase until that checkpoint succeeds.

These instructions are policy and schema text. Dynamic phase names, outputs,
questions, and artifacts remain dynamic context and MUST NOT be interpolated
into the stable cache contract.

### FR-2 — Require complete PhaseSpec metadata

Generated workflows MUST have a non-empty, unique, canonical PhaseSpec name
for every non-terminal phase. The graph MUST have:

- deterministic list order;
- valid next and on_reject targets;
- no target pointing to an undeclared state;
- a typed state enum that covers each declared phase and terminal outcome; and
- one mapping between state names and PhaseSpec names.

The generated package MUST record a plan version or plugin fingerprint in its
checkpoint-compatible metadata. The TUI total MUST be computed from the same
plan that validation and dispatch use.

### FR-3 — Publish phase metadata before execution

Before the first LLM/provider/tool call for a phase, the runner MUST:

1. set the typed context state and increment its phase iteration according to
   the workflow's retry policy;
2. attach the context to the existing workflow handle;
3. call the existing AppState workflow-phase projection with all required
   annotation fields;
4. update the handle's current phase, index, and iteration; and
5. ensure a phase-entry recovery checkpoint is available when the handle
   supports checkpointing.

A phase must not be shown as active solely because a transition tool or agent
prose mentioned its name. The projection must occur from the runner's actual
dispatch state.

### FR-4 — Keep the phase display correct during retries and branches

When a phase is retried, rejected, re-entered, or resumed, the runner MUST
publish the canonical phase name and index again with the new iteration/attempt.
The runner MUST preserve total_phases for the plan. If a workflow intentionally
supports dynamic phases, it MUST publish a deterministic plan revision and
total for the active plan, persist it in context, and never silently change
the meaning of an existing phase index during a run.

### FR-5 — Keep transitions tool-controlled

Generated workflows MUST use event-backed transition tools. A phase can
advance, reject, branch, or terminate only after the appropriate transition
tool succeeds. The runner MUST inspect the event/data produced by that call,
not parse prose for a phase name.

A successful transition tool call is the point at which the runner may begin
the boundary protocol. Tool-call transaction integrity and retry idempotency
remain governed by PRD-169.

### FR-6 — Checkpoint every completed phase

After every phase method returns a valid transition, the runner MUST create a
durable boundary checkpoint before the next provider turn. This applies to:

- ordinary next-phase transitions;
- approval and completion transitions;
- rejection and repair transitions;
- loop iterations and repeated work on the same named phase;
- dynamic phase/page/item boundaries;
- transitions to a terminal complete or exited state; and
- transitions to a recoverable failed or paused state when a context exists.

The checkpoint reason MUST identify the boundary, phase, and outcome in
bounded diagnostic metadata. A terminal final checkpoint does not replace the
checkpoint for the phase that produced it.

### FR-7 — Define boundary ordering and failure behavior

The implementation MUST use an ordering equivalent to:

1. validate the transition-tool result and next state;
2. commit phase output, artifact receipts, history, retry/rejection data, and
   the next typed state to the in-memory context;
3. attach the updated context to the workflow handle;
4. persist the completed-boundary checkpoint through the existing checkpoint
   store;
5. publish or refresh the next phase annotation; and
6. permit the next agent turn.

If immediate TUI feedback requires step 5 before step 4, the UI publication is
provisional and step 6 remains forbidden until step 4 succeeds. The runner
MUST NOT call the next provider turn after a checkpoint serialization or
storage error. It must preserve the current context and route through the
existing recoverable error/failure finalizer, with a diagnostic-only fallback
only when no typed context can be serialized.

### FR-8 — Make boundary checkpoints resumable

On process restart or explicit resume, the runner MUST:

- load and validate the generated plugin fingerprint and payload;
- restore the typed state at the last durable boundary;
- reattach the supplied session memory and preserve the same conversation_id;
- restore phase output, artifact references, rejection/retry cursor, and
  iteration;
- publish the restored phase annotation before the first resumed turn; and
- dispatch through the same outer loop used by a fresh run.

resume(context) MUST NOT call run(context.intent), restart at the first phase,
create a new conversation, or create a new session memory when one was supplied.

### FR-9 — Validate the generated lifecycle before publication

The create_workflow validator and smoke runner MUST reject a generated package
that lacks evidence of:

- complete PhaseSpec declarations and a state/plan mapping;
- a centralized phase annotation path that calls the existing AppState and
  WorkflowRunHandle contracts;
- checkpoint codec methods;
- a boundary checkpoint operation after phase completion and before the next
  turn;
- resume dispatch using the restored context;
- bounded JSON-compatible payload data; and
- safe handling of a checkpoint failure.

Validation SHOULD prefer AST/import inspection plus a bounded fake-handle smoke
run. It MUST NOT execute arbitrary generated business commands or make
unapproved network requests as part of validation.

### FR-10 — Expose useful diagnostics

When annotation or checkpoint validation fails, the report MUST identify:

- the generated workflow and run/draft identity;
- missing or duplicated PhaseSpec names;
- the expected and observed phase plan;
- missing AppState or handle publication evidence;
- missing boundary checkpoint evidence and phase names affected;
- whether failure occurred at codec, storage, or runner level; and
- whether the draft is publishable, repairable, or requires user action.

Diagnostics MUST be bounded and redacted. They MUST NOT include API keys,
authorization headers, credentials, raw private provider payloads, or complete
conversation contents.

### FR-11 — Preserve cache stability

The generated workflow MUST retain the cache contract:

- stable policy, role instructions, tool schemas, and annotation/checkpoint
  rules remain in a stable system-prompt region;
- current phase, phase index, iteration, artifacts, transition result,
  questions, answers, and validation output remain dynamic context;
- phase annotations are sent through runtime state/UI methods, not inserted
  into the stable system prompt; and
- the same conversation_id and supplied session memory are used across phases,
  retries, and resume.

The implementation MUST NOT claim that every checkpoint or annotation causes a
cache invalidation. Checkpoint metadata is persistence data; it must not
rewrite old conversation entries or prepend rolling summaries.

### FR-12 — Preserve existing security and policy boundaries

Generated runners MUST inherit the existing workspace scope/access,
capability filtering, network/browser policy, MCP policy, approval behavior,
AGENTS.md instructions, and mode semantics. The annotation/checkpoint helper
must not become a path, network, shell, or provider escape hatch.

Checkpoint payloads and UI annotations MUST exclude secrets. Artifact
references MUST remain within the existing workspace/artifact policy.

### FR-13 — Keep backward compatibility explicit

Existing built-in workflows and previously published valid custom workflows
must continue to load and run according to their existing contracts. Existing
checkpoint payload versions must remain readable through the current migration
path.

Newly generated workflows MUST satisfy this PRD before publication. A legacy
workflow that lacks the new evidence may receive a clear compatibility
warning/diagnostic, but the loader must not silently reinterpret its phase
graph or corrupt an older checkpoint. Any hard enforcement on legacy packages
requires an explicit migration path and release note.

### FR-14 — Make publication atomic with respect to lifecycle evidence

The generated package, validation evidence, phase-contract smoke result, and
publication identity MUST be associated with the same authoring result. A
workflow must not be published as compliant when validation only inspected
source files but did not exercise the boundary contract. This requirement
extends PRD-174's staged publication model; it does not add a second registry.

### FR-15 — Reconcile resume state before prompt construction

Every workflow resume entry point, including reconstruct_site and workflows
generated by create_workflow, MUST resolve its execution position before it
builds a phase-specific system prompt, invokes an authoring/agent turn, or
asks the user to choose how to recover.

The resolver MUST load and validate, in one recovery operation:

- the workflow checkpoint and its schema/plugin/plan fingerprint;
- the typed context and last safe boundary;
- the artifact/phase-receipt manifest when the workflow has one;
- the append-only workflow journal/event position; and
- the existing session conversation identity and supplied session memory.

The resolver MUST use this authority order:

1. a valid, newest boundary checkpoint whose context and plan fingerprint
   validate;
2. verified contiguous phase receipts/manifest entries newer than that
   checkpoint, when the workflow's evidence contract supports safe
   reconciliation;
3. durable workflow journal/events that record a committed transition; and
4. transcript text or conversation summaries only as advisory dynamic context.

User intent and a fresh-run default MUST NOT outrank a valid durable cursor.
Conversation summaries MUST never be used as proof that a phase completed.

For a workflow with durable phase receipts, the resolver MUST:

- verify receipt integrity, phase name, plan version, boundary revision, and
  required artifact references;
- determine the furthest contiguous completed prefix in canonical plan order;
- preserve a valid partial active phase when no later committed phase exists;
- advance to the earliest incomplete/retryable phase when later receipts prove
  that an older cursor is stale;
- preserve rejection, repair, dynamic-loop, and failure cursors;
- create a reconciliation checkpoint before the first resumed provider turn;
  and
- publish the resolved annotation through AppState and WorkflowRunHandle
  before injecting the resolved phase prompt.

In the reported reconstruct_site failure, verified receipts for init, recon,
visual_research, interaction_analysis, content_assets, architecture, and
design_system MUST resolve the run to bootstrap (or the next phase defined by
the active plan). The resolver MUST NOT inject INIT again or present
Continue INIT / Re-submit INIT / Reset INIT as the default recovery question.
If evidence is genuinely irreconcilable, it must report the exact conflict and
choose the earliest safe revalidation point; it may ask the user only after
this deterministic recovery attempt, never before it.

If no valid checkpoint or receipt exists, the existing safe fresh-run fallback
may select the first phase, but it MUST record that no durable resume evidence
was available. This fallback must not be confused with a stale-cursor case.

## 10. Dataflow

The following flow is normative for a generated custom workflow:

    User request
      |
      v
    create_workflow design phase
      |  ordered PhaseSpec graph, State enum, context and lifecycle design
      v
    create_workflow generation phase
      |  runner + annotation helper + checkpoint codecs + transition tools
      v
    static validation and bounded lifecycle smoke
      |  plan mapping, publication calls, codec and boundary evidence
      v
    approved published WorkflowPlugin
      |
      v
    run(intent) creates typed context and run_id
      |  attach supplied session memory and conversation identity
      v
    outer loop selects State
      |
      v
    publish_phase(context, PhaseSpec)
      |------------------------------+
      |                              |
      v                              v
    AppState.update_workflow_phase   WorkflowRunHandle.attach_context
    (TUI reactive projection)        + update_phase (phase entry)
      |                              |
      +---------------+--------------+
                      v
              inner agent turn loop
                      |
                      v
              transition tool event
                      |
                      v
              commit output/artifacts/history/next state
                      |
                      v
              checkpoint codec -> typed JSON payload
                      |
                      v
              WorkflowRunHandle.save_checkpoint
                      |
              +-------+--------+
              |                |
       durable success      storage/codec failure
              |                |
              v                v
      publish next phase   stop before next turn;
      and call provider    recoverable error finalizer
              |
              v
        repeat until terminal
              |
              v
      terminal boundary checkpoint
              |
              v
      resume loads payload, reattaches memory,
      republishes phase, and re-enters the same loop

The AppState branch is a UI projection and is not the durable source of truth.
The typed context encoded by the WorkflowRunHandle checkpoint is the resume
source of truth. ConversationStore/session memory remain the session-level
conversation source of truth and are referenced, not duplicated, in context.

### 10.1 Resume reconciliation flow

The restart path has a mandatory pre-prompt stage:

    resume request
      -> load checkpoint, manifest/receipts, journal, plan fingerprint
      -> validate and reconcile durable execution position
      -> restore or construct the minimum typed context
      -> save reconciliation checkpoint when the cursor changed
      -> publish resolved phase annotation
      -> build the resolved phase prompt
      -> enter the normal outer dispatch loop

The prompt builder is downstream of the resolver. It must receive the resolved
phase as an input and must not independently choose INIT, infer a phase from a
summary, or ask a recovery question before the durable sources have been
examined. In reconstruct_site, a verified contiguous receipt prefix through
design_system therefore feeds bootstrap into the prompt builder even if the
loaded checkpoint still says init.

## 11. Contracts and schemas

### 11.1 Phase annotation contract

The generated runner MUST provide one internal operation with semantics
equivalent to:

    publish_phase(context, phase_spec, status="running")

It MUST validate that:

- phase_spec.name is present in the canonical plan;
- phase_index is the index of that exact PhaseSpec;
- total_phases is the canonical plan length;
- context.run_id is non-empty;
- phase_iteration is non-negative and follows the retry policy; and
- the model ID is the effective model for the current phase.

The operation MUST forward the values to the existing AppState and handle
methods rather than mutating unrelated UI fields. It MUST be safe to call
again during resume and retry without creating a second run.

### 11.2 Boundary checkpoint contract

The generated runner MUST provide one internal operation with semantics
equivalent to:

    checkpoint_boundary(context, completed_phase, next_state, outcome)

The operation MUST:

- attach the current context;
- preserve the selected state and completed-phase data;
- use the existing WorkflowRunHandle checkpoint path;
- provide a bounded reason/outcome;
- propagate checkpoint failures to the framework's recovery path; and
- be idempotent for a retried transition with the same run/phase/sequence.

The operation MUST NOT silently downgrade a checkpoint failure to a successful
phase transition.

### 11.3 Minimum checkpoint payload

The exact schema is owned by the current checkpoint module, but a compliant
generated codec must preserve at least:

    {
      "schema_version": "...",
      "workflow_name": "...",
      "run_id": "...",
      "conversation_id": "...",
      "state": "...",
      "current_phase": "...",
      "phase_index": 0,
      "total_phases": 0,
      "phase_iteration": 0,
      "phase_attempts": {},
      "completed_phases": [],
      "phase_history": [],
      "artifacts": {},
      "retry_or_rejection": {},
      "last_boundary": {},
      "cache_diagnostic": {}
    }

Values may be compact references/digests rather than full artifacts. The
payload must be JSON compatible, bounded by artifact/reference policy, and
free of live objects and secrets. The project-wide “no arbitrary global
checkpoint byte cap” decision remains in force; externalize large artifacts
instead of inventing a smaller limit.

### 11.4 Transition and checkpoint sequence

Each completed phase must have a unique, auditable boundary sequence or
equivalent revision. Replaying an already durable transition must not duplicate
an external side effect solely because a process crashed after the tool call.
Tool-side idempotency remains part of PRD-169, while this PRD requires the
runner to persist the evidence needed to decide whether the boundary already
committed.

## 12. Non-functional requirements

### 12.1 Correctness and durability

- No successful phase transition may be followed by a next provider turn
  without a durable boundary checkpoint.
- The checkpoint cursor and UI annotation must refer to the same canonical
  phase plan.
- A process killed immediately after a boundary must resume deterministically.
- Repeated resume must not duplicate phase history or artifacts.

### 12.2 Performance

- Annotation and checkpoint coordination must not add an LLM turn.
- Phase publication must be O(1) in the number of phases after the plan index
  is built.
- Checkpoints must avoid copying the full ConversationStore, session memory,
  browser state, or large artifacts.
- The stable prompt/tool prefix must remain unchanged across phase annotations.

### 12.3 Cache preservation

Runtime phase fields, status, model overrides, artifacts, and checkpoint
revisions are dynamic. They must be passed through dynamic context or runtime
state and must not cause avoidable changes to the stable system prompt.

### 12.4 Reliability and failure containment

- Codec, validation, and storage errors are classified and surfaced.
- A failed checkpoint leaves the phase retryable or invokes the existing
  failure finalizer; it never silently advances.
- Partial UI publication must not be treated as durable execution.
- The implementation must tolerate a missing optional handle in test/headless
  adapters while preserving the same semantics when a handle is present.

### 12.5 Security and privacy

- No secret, authorization header, provider key, or raw private response may
  enter an annotation, checkpoint, validation report, or PRD example.
- Phase labels and bounded intent display must follow existing redaction and
  workspace policy.
- Validation smoke tests must not execute untrusted generated side effects
  beyond the existing approved sandbox/adapter contract.

### 12.6 Compatibility and maintainability

- Existing PhaseSpec and WorkflowPlugin public contracts remain valid.
- Generated code uses current public framework methods and typed annotations.
- There is one source of truth for phase topology and one centralized helper
  for publication and one for checkpointing.
- The contract is documented in the create_workflow guide, workflow guide,
  checkpoint/storage reference, and generated-workflow prompts.

### 12.7 Observability

Logs and lifecycle events should identify run ID, workflow, phase, index,
iteration, boundary revision, and outcome. They must not log secrets or full
conversation content. A checkpoint diagnostic should distinguish phase-start,
phase-completed, pause, error, and terminal reasons.

### 12.8 Resume recovery determinism

Resume reconciliation must be deterministic for the same checkpoint, manifest,
journal position, plan fingerprint, and session identity. It must complete
before prompt construction, must not depend on provider behavior, and must not
use an LLM to decide which phase was last completed. A stale cursor must be
advanced only from validated durable evidence; an unverified summary must
never advance it. The resolver must emit enough bounded provenance to explain
why it selected the restored or reconciled phase.

## 13. Acceptance criteria

### AC-1 — Generated source declares an authoritative plan

Given a generated workflow with N executable phases, when validation inspects
it, then every phase has one unique PhaseSpec name, the state mapping is
complete, valid edges point to declared states, and the reported total is N.

### AC-2 — Phase is visible before its first turn

Given a published custom workflow and a fake AppState/WorkflowRunHandle, when
phase i begins, then the runner publishes workflow name, phase name, i, N,
run ID, effective model, and iteration before the first provider/tool call.

### AC-3 — UI publication is centralized

Given a generated runner with multiple branches and retries, when source and
smoke validation run, then every entry path reaches the same annotation helper
and no branch can bypass AppState or handle publication.

### AC-4 — Normal transition checkpoints

Given a phase completes by calling its transition tool, when the runner
selects the next state, then a durable checkpoint is saved containing the
completed phase output and next cursor before the next provider turn starts.

### AC-5 — Rejection and retry checkpoints

Given a review phase rejects work and routes to a repair phase, when the
transition succeeds, then the checkpoint contains the rejection reason,
affected artifact references, phase iteration/attempt, and repair cursor, and
the TUI shows the repair phase with the correct index and total.

### AC-6 — Terminal checkpoint

Given the final phase transitions to complete or exited, when the run reaches
the terminal state, then the final phase's output and terminal status are
durable and a terminal checkpoint is saved. A final checkpoint does not replace
the preceding phase-boundary checkpoint.

### AC-7 — Checkpoint failure is fail-closed

Given the checkpoint store or codec raises after a transition, when the runner
handles the error, then no next provider turn occurs, the output/context is
preserved for the existing recovery finalizer, and the run is reported as
recoverable or diagnostic-only according to whether a typed context exists.

### AC-8 — Resume restores exact state

Given a checkpoint written after phase i, when the process is restarted and
resume is invoked, then the same run ID, conversation_id, supplied memory,
state, phase iteration, artifacts, and retry data are restored; the UI
republishes the restored phase before the first resumed turn; and phase i is
not repeated unless the checkpoint explicitly marks it retryable.

### AC-9 — No duplicate memory or conversation

Given a session-wide conversation and memory, when a generated workflow runs
through all phases and resumes, then every phase uses the supplied objects,
the codec contains references/identities rather than copies, and no second
conversation or memory instance is created.

### AC-10 — Dynamic phase policy is deterministic

Given a workflow with a bounded dynamic phase loop, when the active plan
changes, then the checkpoint records a plan revision and cursor, the TUI
displays a deterministic name/index/total, and resume uses the same revision
or explicitly invokes the declared migration policy.

### AC-11 — Cache contract remains intact

Given two phases with different names, artifacts, and iterations, when their
provider requests are composed, then the stable policy/tool prefix and
conversation ordering remain unchanged; only dynamic phase context and normal
history evolve.

### AC-12 — Validation catches missing contracts

Given generated source with any of the following: missing PhaseSpec, wrong
total, missing AppState publication, missing handle update, absent checkpoint
codec, checkpoint only at terminal completion, or swallowed checkpoint error,
when validation runs, then publication is rejected with a bounded actionable
diagnostic naming the failed contract.

### AC-13 — Existing workflows remain compatible

Given existing built-in workflows and valid legacy checkpoint payloads, when
the full workflow and resume tests run, then they continue to load and behave
as before. New enforcement applies to newly generated packages, with an
explicit compatibility path for older packages.

### AC-14 — Generated package is published only after evidence

Given a generated workflow draft, when source validation or lifecycle smoke
fails, then the package remains a draft/repair candidate and is not registered
as a compliant published workflow. Successful publication records validation
and boundary-contract evidence.

### AC-15 — User-visible documentation is complete

Given a downstream author or maintainer, when they read the create_workflow
guide and generated-workflow documentation, then they can find the phase
annotation fields, required call order, checkpoint payload exclusions, resume
rules, failure behavior, and a minimal reference pattern.

### AC-16 — Resume reconciles durable progress before prompts

Given a run whose checkpoint cursor is INIT, a valid phase manifest with
init through design_system marked complete, and a summary saying the run is
mid-BOOTSTRAP, when the process is restarted and resume is invoked, then the
resolver completes before any LLM prompt is constructed, selects BOOTSTRAP
(or the active plan's next incomplete phase), writes a reconciliation
checkpoint, publishes that phase annotation, and does not inject INIT or ask
the user to re-submit/reset INIT.

### AC-17 — Summary cannot override durable state

Given a transcript summary that names a phase earlier or later than the
verified checkpoint/manifest, when resume runs, then the durable resolver
selects the phase and the summary is retained only as dynamic context. The
test must prove that changing summary text alone cannot change the dispatch
state.

### AC-18 — Resume has no pre-reconciliation provider turn

Given a resumed workflow with a stale cursor, when the provider/turn adapter is
instrumented, then the first observed operation is durable state loading and
reconciliation, followed by phase annotation; no INIT or other fresh-run
prompt is sent before the resolved phase is selected.

### AC-19 — Manifest-only recovery is safe and explicit

Given no usable checkpoint but an integrity-verified, contiguous phase manifest,
when resume runs, then the workflow reconstructs the minimum typed context,
selects the earliest incomplete phase, persists a reconciliation checkpoint,
and resumes there. Given neither usable checkpoint nor verified receipts,
resume uses the documented safe fallback and emits a bounded diagnostic rather
than pretending that a later phase was recovered.

### AC-20 — Reconciliation is idempotent

Given the same checkpoint, manifest, journal position, and plan fingerprint,
when resume/reconciliation is invoked repeatedly, then it produces the same
state, phase index, total, artifact cursor, and checkpoint identity without
duplicating phase receipts, conversation entries, or side effects.

## 14. Testing strategy

Tests must be deterministic, isolated, and use fake providers, handles, stores,
and AppState projections. No test may require an external LLM, browser, MCP
server, or network destination.

### 14.1 Unit tests

Add unit coverage for:

- canonical PhaseSpec name/index/total derivation;
- duplicate/missing/invalid phase graph detection;
- annotation field construction and model override resolution;
- AppState and WorkflowRunHandle publication ordering;
- phase iteration and retry/rejection updates;
- boundary reason and sequence construction;
- checkpoint payload JSON compatibility and exclusion of live objects;
- codec round-trip with supplied session memory;
- terminal, pause, error, and legacy payload behavior;
- stale-cursor reconciliation against a contiguous phase-receipt manifest;
- authority precedence between checkpoint, manifest, journal, summary, and
  fresh-run defaults;
- prevention of prompt construction/provider calls before reconciliation;
- manifest-only recovery and safe fallback when no durable evidence exists;
- idempotent repeated reconciliation;
- idempotent repeated boundary persistence;
- checkpoint failure propagation and fail-closed behavior; and
- cache-stable versus dynamic field separation.

### 14.2 Integration tests

Add integration coverage for:

- a generated two- or three-phase plugin running through the real workflow
  handle and temporary checkpoint store;
- checkpoint count and contents after every successful phase;
- a rejection/retry loop with a checkpoint at each boundary;
- phase publication observed through the real AppState projection;
- interruption immediately before and after boundary persistence;
- storage failure preventing the next agent turn;
- process-style checkpoint reload and exact resume;
- reconstruct_site regression where INIT through design_system receipts are
  complete but an older cursor says INIT; assert BOOTSTRAP resumes without an
  INIT prompt;
- a generated custom workflow with the same stale-cursor/reconciliation
  scenario;
- same ConversationStore/conversation_id/session memory across all phases;
- dynamic bounded phase plans and plan revisions;
- staged validation/publication rejection when lifecycle evidence is missing;
  and
- compatibility of existing built-in workflows and legacy checkpoints.

### 14.3 End-to-end tests

Add E2E journeys that:

1. ask create_workflow to generate a small custom workflow;
2. validate and publish the generated package;
3. execute it with a deterministic fake agent;
4. assert that the TUI's workflow line changes to each custom phase before
   its turn, including phase index, total, model, and iteration;
5. interrupt after a non-terminal phase;
6. reopen/resume the run and assert exact phase/artifact continuation;
7. exercise rejection and repair;
8. complete the run and verify terminal checkpoint evidence; and
9. verify that a generated package missing annotation or per-phase checkpoint
   behavior is rejected before publication.
10. restart a reconstruct_site fixture with a stale INIT cursor and complete
    phase receipts, then assert the first resumed phase is BOOTSTRAP and the
    first prompt is not INIT.

### 14.4 Static and performance checks

- Run the generated-workflow validator against compliant and intentionally
  broken fixtures.
- Run AST/source checks that prevent a hard-coded phase total from becoming a
  second plan source.
- Assert annotation and checkpoint coordination add no provider calls.
- Assert resume reconciliation completes before any phase prompt or provider
  call and that summary text cannot alter the resolved state.
- Measure checkpoint payload size with large external artifact references and
  ensure the payload does not contain copied conversation/memory data.
- Run the focused workflow tests, then the complete test suite and relevant
  lint/type/doc checks.

## 15. Documentation requirements

Update the following in the same implementation:

- create_workflow's authoring prompts and generated template/example;
- docs/guides/workflows.md with phase publication and boundary checkpoint
  rules;
- docs/reference/storage.md with the phase-boundary checkpoint lifecycle and
  payload exclusions;
- docs/guides/workflows.md or docs/reference/storage.md with resume authority
  precedence and pre-prompt reconciliation;
- docs/reference/workflow-review.md with the generated-workflow audit;
- README.md if the behavior is user-visible;
- llms-full.txt and llms.txt for any new public symbol or public contract; and
- the PRD index and this PRD's implementation evidence when shipped.

The documentation must state plainly that:

- PhaseSpec describes the stable graph;
- runtime annotation publishes actual execution state;
- a phase-entry checkpoint is not the same as a completed-boundary checkpoint;
- every completed phase must be durable before the next provider turn; and
- checkpoint persistence does not copy the conversation or guarantee a cache
  hit.

## 16. Rollout and migration

### Phase 1 — Contract and prompt update

Add the normative annotation/checkpoint guidance to the stable authoring
contract, design/generation prompts, and template. Keep dynamic values out of
the cache-stable prefix.

### Phase 2 — Validator and smoke enforcement

Add static evidence checks and bounded fake-runtime checks. New generated
packages cannot be published unless they satisfy the contract. Error messages
must identify how the agent can repair the draft.

### Phase 3 — Runtime implementation pattern

Update the generated runner shape and any framework helper needed to make
publication and boundary persistence reliable. Prefer existing handle APIs;
extend them only when a missing primitive cannot be expressed safely.

### Phase 4 — Compatibility and observability

Run the built-in workflow matrix, legacy checkpoint fixtures, and generated
workflow E2E journeys. Add bounded lifecycle diagnostics and update the
implementation evidence in this PRD.

Legacy workflows that cannot be upgraded immediately remain loadable under an
explicit compatibility path. They must not be silently presented as satisfying
the new per-phase checkpoint guarantee.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| The generated agent copies reconstruct_site business logic instead of its lifecycle pattern | Prompts name the semantics and forbid importing private reconstruct_site implementation |
| AppState says the next phase while its checkpoint is not yet durable | Define provisional UI ordering and prohibit the next provider turn until save succeeds |
| A hard-coded total drifts from PhaseSpec | Static validation compares the plan, runner mapping, and reported total |
| Checkpoint payload grows with conversation or artifacts | Store bounded references/digests and explicitly exclude live memory/store objects |
| Retry writes duplicate side effects | Preserve boundary sequence/idempotency evidence and retain PRD-169 tool transaction rules |
| Checkpoint errors get swallowed by generated code | Validator rejects broad swallowing around the boundary; smoke injects codec/storage failures |
| Resume replays INIT because the cursor is older than durable receipts | Reconcile checkpoint, manifest, and journal before prompt construction; summaries remain advisory |
| Dynamic phases make UI indexes unstable | Require a persisted plan revision and deterministic cursor/index policy |
| New enforcement breaks old workflows | Apply strict publication checks to new generated packages and retain versioned legacy loading |
| Phase metadata harms provider prompt caching | Keep annotation outside stable system prompts and preserve one conversation/memory |
| User intent or artifact labels leak sensitive data | Reuse existing redaction, bounded diagnostics, and workspace/artifact policy |

## 18. Decisions and assumptions

1. “Annotated like reconstruct_site” means the generated runner reproduces the
   observable lifecycle semantics of reconstruct_site._publish_phase:
   canonical plan-derived index/total, AppState projection, handle attachment,
   handle phase update, and effective model/iteration publication. It does not
   mean copying reconstruct_site's phase graph.
2. The existing WorkflowRunHandle and checkpoint store remain the durability
   authority. If a helper is needed, it must delegate to those APIs rather than
   create a custom file format or parallel store.
3. “After each phase” includes success, rejection/retry, loop item, terminal,
   pause, and recoverable failure boundaries whenever a typed context exists.
4. A phase-start checkpoint may coexist with a boundary checkpoint, but cannot
   count as the latter unless it contains the completed phase output and next
   cursor and is written after the transition.
5. Resume authority is resolved before any phase prompt. A valid durable
   receipt can advance an older cursor only when its integrity and contiguous
   plan position are verified; a transcript summary alone can never advance it.
6. Phase index is zero-based because that is the existing AppState and
   reconstruct_site convention.
7. total_phases counts executable non-terminal phases in the active canonical
   plan. A workflow with dynamic phases must persist its plan revision and
   disclose its policy.
8. The framework may publish a provisional next phase for responsive UI, but
   no next provider turn is allowed until durable checkpoint success.
9. Existing global decisions about no arbitrary checkpoint byte ceiling,
   session-wide conversation identity, prompt-cache stability, and fail-closed
   policy continue to apply.

## 19. Implementation checklist

- [x] Add annotation and checkpoint requirements to the stable runner guide.
- [x] Add the same requirements to design, generation, validation, and
      inspection-tool prompts without putting dynamic values in the stable
      cache contract.
- [x] Define or reuse one canonical phase-plan/index helper.
- [x] Define or reuse one centralized runtime annotation helper.
- [x] Ensure generated runners publish AppState and WorkflowRunHandle state
      before every first turn, retry, and resume.
- [x] Add a pre-prompt resume reconciler with checkpoint/manifest/journal
      authority precedence and a safe no-evidence fallback.
- [x] Ensure reconstruct_site and generated workflows do not inject a fresh
      INIT prompt before reconciliation completes.
- [x] Define or reuse one boundary checkpoint helper.
- [x] Ensure every valid phase transition checkpoints before the next provider
      turn, including rejection and terminal boundaries.
- [x] Ensure checkpoint failures stop progression and reach recovery handling.
- [x] Extend generated-workflow validation and smoke fixtures.
- [x] Add unit, integration, E2E, regression, compatibility, and performance
      tests listed in this PRD.
- [x] Update workflow, storage, generated-workflow, and public-symbol docs.
- [x] Run the relevant lint, format, type, docs, and complete test gates.
- [x] Update this PRD status and link implementation/test evidence only after
      the acceptance criteria are verified.

## 19.1 Implementation evidence — 2026-08-28

Implemented in the current source tree. The shared lifecycle implementation is
in `src/agenthicc/workflows/phase_lifecycle.py`; the create_workflow authoring
surface now exposes `describe_phase_lifecycle()` and
`show_phase_lifecycle_template()` and returns both tools from
`make_inspection_tools()`. Generated-runner validation and smoke execution
require the annotation, boundary, codec, resume, cache, and fail-closed
markers. The create_workflow and reconstruct_site runners use the same
session-owned ConversationStore/journal and WorkflowRunHandle checkpoint path.

Resume reconciliation is performed before phase prompt construction. Verified
phase receipts and journal boundaries are folded through the canonical plan,
with contiguous-prefix validation and explicit bounded provenance. The
reconstruct_site stale-INIT regression resolves to BOOTSTRAP when the durable
receipts prove that INIT through design_system are complete. Boundary journal
records contain metadata only; prompts, tool arguments, artifact bodies,
credentials, and memory objects are excluded from checkpoint payloads.

Verification evidence:

- `uv run pytest tests/ -q`: **3601 passed, 15 skipped**.
- Focused lifecycle/authoring suite: **184 passed** before the final repository
  run, with the final repository run covering those tests again.
- `uv run ruff check ...` on the touched source/tests: passed.
- `uv run ruff format --check ...` on the touched source/tests: passed.
- `uv run mypy` on the touched lifecycle/create_workflow surfaces: passed.
- `uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json`: passed.
- `uv run nox -s llms_check`: passed.
- `uv run python -m compileall -q src tests`: passed.

The repository-wide format check still reports pre-existing formatting drift in
12 unrelated files; those files were intentionally not reformatted as part of
this implementation. The repository-wide test and touched-surface checks are
clean.

## 20. Definition of done

This PRD is implemented only when a newly generated custom workflow can be
validated, published, run, interrupted, resumed, repaired, and completed with
the following evidence:

1. The TUI shows the correct custom workflow phase, index, total, model, and
   iteration before each phase's first agent turn.
2. A durable checkpoint exists after every completed phase boundary and
   contains the output and resume cursor required for that boundary.
3. A checkpoint failure prevents the next provider turn and produces a
   recoverable or diagnostic-only error according to the framework contract.
4. Resume uses the same typed context, session memory, conversation identity,
   and phase plan without duplicating completed work, and stale cursors are
   reconciled from verified durable receipts before any prompt.
5. Static, integration, and E2E tests cover normal, retry, interruption,
   failure, terminal, dynamic-plan, stale-cursor, and compatibility paths.
6. Documentation and generated authoring guidance make the contract explicit
   for future downstream workflows.
