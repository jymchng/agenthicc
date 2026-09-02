---
title: "PRD-186: Profile-aware workflow checkpoint topology"
status: Implemented
version: 1.1.0
created: 2026-09-02
scope: "workflow checkpoint cursors, active phase topology, recovery validation, and reconstruct_site profiles"
related_prds:
  - PRD-100  # code_plan workflow architecture
  - PRD-156  # resumable workflow continuation
  - PRD-169  # transaction-safe tool-call conversations
  - PRD-170  # durable workflow recovery
  - PRD-173  # recoverable workflow errors and failure checkpoints
  - PRD-177  # reconstruct_site profiles and evidence
  - PRD-179  # phase annotations and boundary checkpoints
  - PRD-182  # durable mid-turn preservation
  - PRD-184  # preserve active phase after errors
tags:
  - workflows
  - checkpoints
  - resume
  - topology
  - phase-cursor
  - reconstruct-site
  - profile-aware
---

# PRD-186 — Profile-aware workflow checkpoint topology

## 1. Executive summary

Resuming a saved `reconstruct_site` run can fail with:

```text
Cannot resume '853cfb6649824f71bedde79a72ecb43a':
checkpoint_phase_mismatch: checkpoint phase index does not match the workflow topology
```

The checkpoint contains a valid phase name, but its numeric `phase_index` was
calculated against the run's profile-filtered phase plan while the recovery
validator compares it with the full declarative workflow registry. For
example, the static profile omits infrastructure phases and assigns
`final_validation` index `19`; the registry contains all 41 phases and assigns
the same phase index `40`. The checkpoint is therefore rejected even though
the saved phase and typed context can be correct.

This PRD makes the phase name and the exact active topology the authoritative
resume coordinate. A checkpoint will persist a profile/plan-aware topology
identity, and recovery will validate the cursor against that same topology.
Static workflows continue to use their declared `PhaseSpec` topology. Dynamic,
profiled, and generated workflows expose a checkpoint-topology resolver that
reconstructs the exact ordered active graph from the checkpoint context. The
generic recovery coordinator will never compare a profile-local index with an
unfiltered registry index.

The change preserves the existing run ID, conversation ID, typed context,
phase receipts, evidence manifest, claim lease, cache metadata, and
`runner.resume(context)` contract. It does not turn an incompatible checkpoint
into a fresh `INIT` run. If the topology cannot be reconstructed safely, the
user receives an actionable diagnostic and the checkpoint remains available
for inspection or explicit reset.

## 2. Problem statement

### 2.1 User-visible failure

After a provider timeout, rate limit, authentication outage, tool failure, or
process interruption, a workflow is paused and checkpointed. The next
`continue`, `--continue`, `--resume`, or `/workflow resume` attempts to inspect
the checkpoint. Recovery reports a phase-index mismatch and refuses to resume.

The failure is especially confusing because the checkpoint can contain all of
the following correct information:

- the original workflow `run_id`;
- the same session conversation ID;
- the later phase name, such as `responsive_pass` or `final_validation`;
- a typed context whose state agrees with that phase;
- completed phase receipts and output artifacts; and
- a profile and plan version that identify the phase plan used by the runner.

The refusal is currently based on a numeric comparison made in a different
coordinate system, not on evidence that the workflow state is corrupt.

### 2.2 Confirmed current implementation

The current source has two separate topology representations:

| Surface | Current behavior | Result |
|---|---|---|
| `ReconstructPhasePlan.active()` | Filters the authoritative 41-phase plan by `static`, `application`, `production`, or `custom` profile and renumbers the remaining phases from zero | Produces the index used during execution |
| `ReconstructSiteRunner._publish_phase()` | Uses `plan.index[phase_name]` and publishes the active plan's total and index | Checkpoints receive the profile-local index |
| `checkpoint_phase_boundary()` | Persists the next phase and the index supplied by the runner | Preserves the profile-local index |
| `WorkflowCheckpoint` | Stores `current_phase` and `phase_index`, but no active-topology identity | Cannot describe the index coordinate system |
| `workflow_fingerprint()` | Hashes the plugin's declared `PhaseSpec` list | Identifies the full registry topology, not an active profile plan |
| `WorkflowRecoveryCoordinator._validate_recovery()` | Finds `current_phase` in `workflow.phases` and compares its full-registry position to `checkpoint.phase_index` | Rejects valid profile-filtered checkpoints |
| `reconstruct_site` context | Already stores `profile` and `plan_version` | Recovery does not currently use them to resolve the active topology |

The relevant implementation boundaries are:

- `src/agenthicc/workflows/reconstruct_site/evidence_plan.py` — active plan
  construction and profile filtering;
- `src/agenthicc/workflows/reconstruct_site/runner.py` — phase publication,
  boundary checkpointing, and resume reconciliation;
- `src/agenthicc/workflows/checkpoint.py` — checkpoint schema and plugin
  fingerprint;
- `src/agenthicc/runners/workflow_handle.py` — cursor capture and checkpoint
  writes; and
- `src/agenthicc/runners/workflow_recovery.py` — fail-closed resume
  validation.

### 2.3 Reproduction matrix

The current implementation produces these index differences:

| Profile | Phase | Index written by active plan | Index expected by full registry |
|---|---|---:|---:|
| `static` | `responsive_pass` | 13 | 14 |
| `static` | `visual_validation` | 14 | 15 |
| `static` | `final_validation` | 19 | 40 |
| `application` | `final_validation` | 20 | 40 |
| `production` | `final_validation` | 40 | 40 |
| custom `init → research_gate → final_validation` | `final_validation` | 2 | 40 |

This explains why a run can advance through research and implementation and
fail only when it reaches a phase after a profile-excluded phase. A production
profile happens to use the same coordinate system and masks the defect.

### 2.4 Why an error exposes it

The provider error is not the root cause. The sequence is:

```text
provider/tool/process error
  → workflow failure finalizer
  → checkpoint captures current_phase + profile-local phase_index
  → process releases the workflow claim
  → resume discovery loads the checkpoint
  → recovery compares the index with the full PhaseSpec list
  → checkpoint_phase_mismatch
```

Increasing retries or timeouts cannot repair this comparison. The error is
visible only when recovery validation runs after the failure.

## 3. Goals

1. Resume a valid profile-filtered, custom, dynamic, or generated workflow at
   its exact saved phase without changing its run ID or starting at `INIT`.
2. Make the phase name the semantic cursor and make the numeric index a value
   derived from the exact topology used by that run.
3. Persist enough topology identity to detect a genuinely changed workflow,
   changed plan version, changed profile, or changed custom phase selection.
4. Give the generic recovery coordinator one topology-validation contract that
   works for static and dynamic workflows.
5. Preserve fail-closed behavior when the topology cannot be reconstructed or
   the checkpoint is genuinely incompatible.
6. Keep phase-entry and phase-boundary checkpoints monotonic and consistent
   with TUI progress annotations.
7. Support checkpoints created before this PRD without silently discarding
   them or changing their workflow to a new `INIT` run.
8. Ensure `create_workflow` teaches generated workflows how to expose and
   persist their active topology so they work automatically.
9. Cover TUI, headless, explicit resume, `--continue`, process restart,
   repeated errors, and repeated resume cycles.

## 4. Non-goals

- Changing the business order or scope of any `reconstruct_site` phase.
- Removing profile filtering or forcing every profile to execute all phases.
- Treating an LLM response, conversation summary, or UI annotation as the
  source of durable phase state.
- Re-running completed phases merely to make an index appear contiguous.
- Automatically editing a checkpoint's index without validating the phase
  name, topology identity, context, and receipts.
- Weakening plugin fingerprints or accepting a changed workflow graph silently.
- Taking over another process's workflow claim.
- Retrying provider errors indefinitely.
- Deleting incompatible checkpoints automatically.

## 5. Definitions and invariants

### 5.1 Topology coordinate

A topology coordinate is the ordered executable phase graph used by one run.
It consists of:

- an ordered tuple of canonical phase names;
- each phase's executable next edge and rejection/retry edge;
- a workflow-specific plan version when one exists;
- the selected profile or custom phase selection when one exists; and
- a stable fingerprint of the above values.

The coordinate is not the same as the plugin's complete registry topology.
The registry topology describes every possible phase; the active topology
describes the phases this run is actually allowed to execute.

### 5.2 Cursor invariant

For a non-terminal checkpoint:

```text
active_topology[phase_index] == current_phase
```

The equality must hold against the active topology fingerprint stored for the
run, not merely against `workflow.phases`.

### 5.3 Identity invariant

The following are distinct and must remain distinct:

| Identity | Purpose |
|---|---|
| `session_id` / `conversation_id` | Shared conversation and journal |
| `run_id` | One workflow execution and its checkpoints |
| plugin fingerprint | Complete declared workflow compatibility |
| active topology fingerprint | Profile/plan-specific phase coordinate compatibility |
| `current_phase` | Semantic phase cursor |
| `phase_index` | Derived display and ordering position within the active topology |
| `phase_iteration` | Number of times execution entered a phase |
| manifest/receipt identity | Durable artifact and evidence provenance |

No identity may be substituted for another during resume.

### 5.4 Anti-rewind invariant

A normal error must not write a checkpoint at an earlier active phase than the
last safe cursor. A deliberate evidence-driven re-entry may move backwards
only when it records the target phase, reason, affected artifact IDs/kinds,
and an auditable invalidation decision.

## 6. Proposed solution

### 6.1 Add a generic checkpoint-topology contract

Add a small workflow-facing topology value/Protocol in the workflow checkpoint
or phase-lifecycle boundary. It should expose:

```python
class WorkflowCheckpointTopology(Protocol):
    phase_names: tuple[str, ...]
    topology_fingerprint: str
    topology_version: str
    profile: str
```

The concrete API may be a frozen dataclass rather than a runtime Protocol,
provided it is JSON-safe and does not create a second workflow engine.

The workflow plugin/runner contract must support:

```python
resolve_checkpoint_topology(
    context_payload: Mapping[str, object],
) -> WorkflowCheckpointTopology
```

The resolver is called during recovery after the context payload has passed
schema/identity validation and before the checkpoint is offered for resume.
For a static workflow it returns the ordered `PhaseSpec` names and edges. For
`reconstruct_site` it reads the persisted `profile`, `plan_version`, and
`custom_phases` information from the typed context and returns the exact
filtered plan. Generated workflows receive a default implementation based on
their declared `PhaseSpec` list and may provide a custom resolver for dynamic
topologies.

The resolver must be pure with respect to workflow state: it may inspect the
checkpoint payload and current declarations, but it must not call a provider,
write artifacts, mutate the context, or start a phase.

### 6.2 Extend the checkpoint schema

Add non-secret fields to `WorkflowCheckpoint`:

```text
topology_version: str
topology_fingerprint: str
topology_profile: str
topology_phase_names: tuple[str, ...]
```

The exact field names may follow current naming conventions, but the persisted
contract must include the equivalent information. `topology_phase_names` is a
bounded ordered snapshot for diagnostics and index validation; it is not a
second source of executable prompts or handlers. The fingerprint is calculated
from canonical names and graph edges and prevents a checkpoint from accepting
an edited phase list that happens to retain the same names.

The checkpoint continues to store `current_phase` and `phase_index` for
backward-compatible consumers and TUI display. New code must validate that
the name is at the stored topology index and at the resolver's topology index.

Topology metadata must not contain prompts, conversation messages, artifact
bodies, credentials, provider tokens, or arbitrary filesystem contents. Enforce
reasonable structural bounds on the number and length of phase names and
metadata strings; do not reintroduce a large-context byte limit.

### 6.3 Capture the active topology before every durable cursor write

The handle and lifecycle helpers must make topology capture part of the same
cursor contract:

1. The runner selects its active plan before publishing its first phase.
2. The runner attaches/registers the topology with the workflow handle.
3. `update_phase()` and `checkpoint_phase_boundary()` use the topology's
   name-to-index mapping rather than accepting an unrelated raw index where
   the workflow can provide the mapping.
4. `build_checkpoint()` serializes the topology metadata alongside the
   cursor.
5. Failure finalization synchronizes the typed context and active topology
   before saving the paused checkpoint.

For `reconstruct_site`, `_publish_phase`, resume reconciliation, and boundary
checkpointing must all use `ActiveReconstructPlan.index`. The full registry
`PhaseSpec` list remains available for plugin compatibility and TUI metadata,
but it must not be used to validate a profile-local checkpoint cursor.

### 6.4 Recovery validation order

`WorkflowRecoveryCoordinator._validate_recovery()` must perform these steps:

1. Load the registered workflow and validate the complete plugin fingerprint.
2. Restore the typed context using the workflow codec and the existing session
   memory. Do not call a provider or runner method.
3. Validate run ID, workflow name, conversation cursor, provider profile, and
   workspace identity as today.
4. Resolve the active topology from the checkpoint context and current
   workflow declaration.
5. If the checkpoint has topology metadata, require the resolved fingerprint,
   version, and ordered phase names to match.
6. Require `current_phase` to exist in the resolved topology and require its
   resolved index to equal `phase_index`.
7. Require context phase/state and `phase_iteration` to agree with the
   checkpoint.
8. Return a recoverable record only after all checks pass.

The diagnostic must include bounded, non-secret values such as workflow name,
profile, phase name, saved index, expected index, saved topology version, and
current topology version. It must never include prompts, full context payloads,
credentials, or provider responses.

Use separate error codes for distinct cases:

| Error code | Meaning |
|---|---|
| `checkpoint_phase_mismatch` | Context phase and checkpoint phase disagree |
| `checkpoint_topology_mismatch` | Saved active topology differs from current active topology |
| `checkpoint_topology_metadata_missing` | A dynamic/profiled run cannot be safely resolved |
| `checkpoint_phase_index_mismatch` | Phase name is valid but saved index is wrong for the resolved topology |
| `plugin_fingerprint_mismatch` | Complete workflow declaration changed |

The existing error code may remain as a compatibility alias, but new UI text
must identify which topology and index were compared.

### 6.5 Safe handling of pre-topology checkpoints

Existing checkpoints do not contain the new fields. They must not all be
rejected, and they must not all be accepted blindly.

The migration algorithm is:

1. If the workflow has a static topology and the saved phase name exists,
   derive the index from the current static topology and accept only if all
   existing identity/context checks pass.
2. If the context contains enough profile/plan metadata to resolve a dynamic
   topology, resolve it and validate the saved phase against that topology.
   `reconstruct_site` must use its existing persisted `profile` and
   `plan_version` fields for this path.
3. If the old checkpoint has a phase name but the active topology cannot be
   determined safely, report `checkpoint_topology_metadata_missing` and keep
   the checkpoint inspectable.
4. On the first successful resume checkpoint, write the new topology fields.
   The migration must preserve the same run ID, context, conversation cursor,
   evidence manifest, and revision ordering.
5. Never repair a mismatch by changing only `phase_index`, creating a new
   manifest, or calling `runner.run(intent)`.

### 6.6 Resume dispatch invariant

After validation and claim acquisition, every entry point must:

```text
load latest checkpoint
  → rehydrate same handle/context
  → attach resolved active topology
  → call runner.resume(context)
```

It must not construct a new run ID, reset the context to `INIT`, select a new
profile from current defaults, or invoke `runner.run(intent)` for an existing
recoverable workflow. A user may explicitly reset/discard the run, after
which a new run is allowed and must be visibly described as new.

### 6.7 `create_workflow` authoring contract

The create_workflow design, generation, and validation prompts must instruct
agents to:

- keep one canonical ordered phase plan;
- avoid deriving checkpoint indexes from a different static registry or from
  a filtered view that is not persisted;
- persist a stable plan/profile/topology version in typed context;
- expose the topology resolver when phases can be selected, skipped, repeated,
  or generated dynamically;
- publish the phase name and active-plan index before each provider turn;
- checkpoint the same topology at phase entry, phase boundary, interruption,
  and recoverable failure; and
- implement `resume(context)` as continuation of the supplied context rather
  than a new `run(intent)` call.

The authoring validator must reject generated workflows that:

- declare profile-filtered phases but provide no topology metadata;
- use a registry index for a filtered active plan;
- mutate phase order without changing topology version/fingerprint; or
- reset a supplied context to its first phase in `resume()`.

Generated workflows with a fixed `PhaseSpec` list receive the default static
resolver and work without extra boilerplate.

## 7. Dataflow

### 7.1 Fresh run

```text
workflow params / initial context
        │
        ▼
select active profile or custom phase list
        │
        ▼
build ordered active topology + fingerprint
        │
        ├── attach topology to WorkflowRunHandle
        ├── publish TUI phase name/index/total from active topology
        └── save checkpoint {phase name, derived index, topology identity, context}
```

### 7.2 Error and resume

```text
provider/tool/process error
        │
        ▼
failure finalizer synchronizes typed context + active topology
        │
        ▼
paused checkpoint for same {session_id, run_id, conversation_id}
        │
        ▼
recovery decodes context and resolves topology from context + workflow code
        │
        ├── topology/name/index match → claim → runner.resume(context)
        └── mismatch/insufficient metadata → actionable diagnostic; no INIT fallback
```

### 7.3 Deliberate evidence-driven re-entry

```text
validation detects stale/corrupt evidence
        │
        ▼
record target phase + invalidated artifacts + reason
        │
        ▼
resolve target index in the same active topology
        │
        ▼
checkpoint explicit rewind with topology identity
```

## 8. Acceptance criteria

### 8.1 Correct cursor validation

- A static-profile checkpoint at `responsive_pass`, `visual_validation`, or
  `final_validation` validates against the 20-phase active plan, not the
  41-phase registry.
- An application-profile checkpoint at `final_validation` validates against
  the 21-phase active plan.
- A production-profile checkpoint validates against the 41-phase plan.
- A custom phase list validates against its exact ordered list.
- A checkpoint with the right phase name and wrong index is rejected with a
  bounded, actionable index diagnostic.
- A checkpoint with changed active phase order, profile, or plan version is
  rejected as topology-incompatible rather than resumed unsafely.

### 8.2 Failure and resume behavior

- A timeout, 429, tool error, browser error, or ordinary exception in any
  active phase preserves the same run ID and active topology.
- `continue`, `--continue`, `--resume`, and `/workflow resume` all rehydrate
  the same context and call `runner.resume(context)`.
- No valid checkpoint reaches a new-run `INIT` path implicitly.
- Repeated pause/resume cycles do not regress the phase index or topology
  fingerprint.
- A resumed run does not create a second evidence manifest or duplicate
  completed phase receipt merely because the process restarted.
- TUI phase name, index, total, checkpoint, and runner context agree after
  resume.

### 8.3 Older checkpoints

- Static pre-topology checkpoints remain resumable when their phase and
  existing identity checks are unambiguous.
- `reconstruct_site` pre-topology checkpoints with persisted profile/plan
  metadata resolve against that active plan.
- Ambiguous pre-topology checkpoints remain inspectable and receive a clear
  migration-required diagnostic; they are not silently reset or deleted.
- A successful migrated resume writes topology metadata without changing run,
  session, conversation, or artifact identity.

### 8.4 Generated workflows

- A generated fixed-topology workflow resumes without custom boilerplate.
- A generated filtered/dynamic workflow is rejected by authoring validation if
  it cannot provide a topology resolver and checkpoint metadata.
- Generated prompts explain the difference between full registry topology and
  active run topology.
- Generated workflow tests cover a phase after a skipped phase, not only the
  first two phases.

### 8.5 Security and durability

- Topology metadata is JSON-safe, bounded, atomic, and free of secrets and
  prompt contents.
- Existing claim leases, revision monotonicity, workspace checks, conversation
  cursor checks, and plugin fingerprints remain enforced.
- A failed topology validation never mutates artifacts, executes tools, calls a
  provider, or takes another owner's claim.
- A topology mismatch can be explicitly discarded/reset with an audit record;
  ordinary resume never performs that destructive action implicitly.

## 9. Testing plan

### 9.1 Unit tests

Add deterministic tests for:

- canonical topology serialization and fingerprint stability;
- phase-name-to-index derivation for static, application, production, and
  custom plans;
- rejection of duplicate, empty, reordered, unknown, or oversized phase
  metadata;
- checkpoint round trips with and without topology fields;
- context/profile/plan-version topology resolution;
- correct error-code precedence between context, topology, index, iteration,
  plugin, provider, workspace, and conversation mismatches; and
- terminal checkpoint handling.

### 9.2 Integration tests

Use temporary checkpoint stores and real workflow handles to verify:

- phase-entry checkpoints store the active topology used by the runner;
- phase-boundary checkpoints store the next active index;
- failure finalization preserves the current active topology;
- recovery validates the same topology after a fresh process-style object is
  created;
- old checkpoints migrate on successful resume;
- changed profile/plan declarations fail closed;
- checkpoint revisions remain monotonic under repeated resume attempts; and
- concurrent claim behavior is unchanged.

### 9.3 End-to-end tests

Add provider-free journeys for:

1. static profile → skipped infrastructure phase → provider failure → resume;
2. application profile → final validation → failure → resume;
3. custom profile with non-contiguous phases → interruption → resume;
4. production profile → failure → resume;
5. TUI restart followed by `continue` and explicit `--resume`;
6. headless `--continue` with an existing workflow run;
7. changed topology after a checkpoint, showing a diagnostic rather than
   executing the wrong phase; and
8. generated workflow with a filtered topology and a successful checkpoint
   resume.

Fault injection must include timeout, 429, tool failure, process interruption,
and a checkpoint written immediately before/after a phase boundary. Assertions
must check run ID, phase name, active index, topology fingerprint, context
state, conversation ID, manifest identity, receipt prefix, and dispatch method.

### 9.4 Regression test for the reported run shape

Create a fixture equivalent to:

```text
workflow = reconstruct_site
profile = static or application
phase = a phase after the first omitted phase
checkpoint.current_phase = phase
checkpoint.phase_index = active_plan.index[phase]
```

Recovery must mark it resumable when the resolved active topology matches, even
though `workflow.phases.index(phase)` differs.

## 10. Observability and diagnostics

Emit bounded structured events for:

- topology selected (`workflow`, `run_id`, `profile`, `version`, phase count,
  fingerprint prefix);
- topology captured in a checkpoint;
- topology validation success/failure;
- pre-topology checkpoint migration; and
- explicit topology discard/reset.

Do not emit topology phase prompts, conversation text, artifact bodies,
credentials, or full fingerprints where a short redacted prefix is sufficient.
The TUI error should state, for example:

```text
Cannot resume '<run-id>': saved active topology 'static' places
'final_validation' at index 19, but the current topology places it at 20.
The checkpoint was not changed; inspect or explicitly reset the run.
```

## 11. Implementation sequence

1. Add the topology value and canonical fingerprint helper.
2. Extend checkpoint encode/decode and validation with bounded topology fields.
3. Add the optional workflow topology resolver/default static resolver.
4. Thread topology registration through `WorkflowRunHandle`, lifecycle helpers,
   and failure finalization.
5. Update `reconstruct_site` to persist and resolve profile/plan topology for
   fresh, resumed, boundary, re-entry, and failure checkpoints.
6. Update generic recovery validation and diagnostics.
7. Add safe pre-topology checkpoint migration.
8. Update create_workflow prompts, templates, validation, and generated
   workflow smoke tests.
9. Add the unit, integration, E2E, and fault-injection tests in this PRD.
10. Update workflow/storage documentation and the public symbol inventory if
    new public types are introduced.

## 12. Backward compatibility and rollout

The checkpoint schema must remain readable for existing sessions. New fields
are optional during deserialization and become required only for newly written
checkpoints after the implementation is active. The existing complete plugin
fingerprint remains in place; the active topology fingerprint supplements it.

No automatic destructive migration is allowed. A run that cannot be resolved
is retained with its checkpoint and diagnostic-only recovery record. Operators
can restore the code/profile that created it or explicitly discard it. A valid
run receives topology metadata during its next durable checkpoint.

The feature is complete when all acceptance criteria pass, the reported
profile-index mismatch is covered by regression tests, and the full relevant
test matrix passes without weakening security, claim, conversation, artifact,
or cache contracts.

## 13. Implementation evidence

Implemented in the current checkout:

- `WorkflowCheckpointTopology`, canonical active-graph fingerprinting, phase
  adapters, optional checkpoint metadata, and structural metadata validation
  are implemented in `src/agenthicc/workflows/checkpoint.py`.
- `WorkflowPlugin.resolve_checkpoint_topology()` supplies the fixed-list
  default. `WorkflowRunHandle` and `phase_lifecycle` derive cursor indexes
  from the resolved active graph for phase entry, boundaries, failure saves,
  and direct checkpoint rehydration.
- `WorkflowRecoveryCoordinator` validates the resolved active topology,
  distinguishes topology/index failures, revalidates the latest checkpoint
  after picker selection, and safely accepts unambiguous pre-topology records
  for migration.
- `reconstruct_site` persists `active_phase_names`, resolves the selected
  profile and plan version, and uses the profile-local coordinate for all
  checkpoint paths. The static profile therefore records
  `final_validation` at index `19`, while production records it at `40`.
- `create_workflow` generation prompts, lifecycle inspection tools, the
  generated runner example, and strict validation now teach and enforce the
  active-topology resolver contract for filtered or dynamic workflows.
- Documentation was updated in `docs/guides/workflows.md`, `llms.txt`, and
  `llms-full.txt`; the type-audit baseline was ratcheted for the intentional
  dynamic attribute adapters.

Verification includes dedicated unit, integration, and E2E tests in
`tests/unit/test_checkpoint_topology_prd186.py`,
`tests/integration/test_checkpoint_topology_prd186.py`, and
`tests/e2e/test_checkpoint_topology_prd186.py`, plus the existing workflow,
recovery, lifecycle, and create_workflow suites. The full pytest run completed
with 3,709 passing tests and 15 skips; repository mypy remains subject to the
pre-existing `name_that_ui` import and NumPy stub compatibility errors noted
in the implementation handoff.
