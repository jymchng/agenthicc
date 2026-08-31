---
title: "PRD-183: Idempotent reconstruct_site research-gate decisions"
status: Proposed
version: 1.0.0
created: 2026-08-30
scope: "reconstruct_site research-gate approval, agent-turn termination, and resume recovery"
related_prds:
  - PRD-169
  - PRD-170
  - PRD-173
  - PRD-177
  - PRD-178
tags:
  - reconstruct_site
  - research
  - idempotency
  - resume
  - checkpointing
  - lauren-ai
---

# PRD-183 — Idempotent `reconstruct_site` research-gate decisions

## 1. Executive summary

The `reconstruct_site` workflow can visibly call **Approve Degraded Research**
multiple times for one research baseline. This is not normally a separate user
approval request. It is the title-cased TUI rendering of the agent tool
`approve_degraded_research`; each repeated title represents another tool call
observed by the conversation renderer.

The immediate design flaw is that the tool sets a local `asyncio.Event`, but the
event is inspected only after `_run_agent_turn()` returns. The agent-turn loop
can therefore receive the successful tool result and make another model turn
before the workflow's outer phase loop gets control. A second, independent
failure mode exists around interruption and resume: the decision is held in
in-memory phase data until the phase returns and the boundary checkpoint is
written. If the process stops in that interval, a resumed context can still
look `pending` and ask the model to approve the same baseline again.

This PRD defines a single, durable, idempotent research-gate decision protocol:

1. A successful transition tool call ends the current agent turn immediately.
2. The decision is committed before the transition event is published.
3. Repeating the exact decision returns the original result without another
   TUI approval presentation, model continuation, receipt, or checkpoint.
4. A changed or conflicting decision is rejected as stale/conflicting rather
   than silently creating another approval.
5. Resume rehydrates the persisted gate decision and skips the gate whenever
   the approved baseline is still current.
6. The optimized runner is the only executable gate implementation; the phase
   metadata and compatibility imports cannot create a second behavior.

The result is that one baseline has at most one terminal research-gate
decision, one durable decision receipt, and one phase-boundary transition.

## 2. Problem statement

### 2.1 User-visible symptom

Users see a sequence similar to:

```text
● Approve Degraded Research
● Approve Degraded Research
● Approve Degraded Research
```

or repeated calls in the transcript while the workflow remains at, or appears
to remain at, the research boundary. This creates several harmful effects:

- the user cannot tell whether the approval was accepted;
- the transcript contains misleading duplicate control operations;
- the agent spends additional turns repeating a terminal action;
- the workflow may consume its bounded gate-attempt budget even though a
  logically valid approval already happened;
- a crash between approval and the boundary checkpoint can cause the next
  process to ask for the same approval again; and
- repeated or conflicting approvals can make the persisted evidence and
  conversational state disagree.

### 2.2 Scope

This PRD covers:

- `reconstruct_site`'s `research_gate` phase;
- `approve_research_baseline`, `approve_degraded_research`, and
  `reject_research_baseline` transition tools;
- the inner agent-turn loop used by specialized workflow runners;
- durable decision receipts, workflow checkpoints, and resume reconciliation;
- re-entry and evidence invalidation that can intentionally make an old
  decision stale;
- TUI and headless event projection for repeated control calls; and
- the agenthicc/lauren-ai boundary needed to stop a turn after a successful
  control tool.

It does not change what constitutes complete, unavailable, stale, or
contradictory research. Those rules remain owned by `ResearchGate` and the
profile coverage policy defined by PRD-178.

## 3. Evidence from the current implementation

The following observations are from the current source tree and are part of
the diagnosis, not historical assumptions.

### 3.1 The TUI label is a tool-call label

`src/agenthicc/tui/workspace/appender.py` derives a fallback display label by
replacing underscores and applying title case (`approve_degraded_research`
becomes `Approve Degraded Research`). Therefore repeated labels are evidence
of repeated tool-completion events, not necessarily repeated human approval
modals.

### 3.2 The specialized runner owns an outer gate loop

`src/agenthicc/workflows/reconstruct_site/runner.py`:

- computes the current coverage matrix;
- publishes the baseline;
- calls `run_phase()` for each bounded gate attempt;
- checks `event.is_set()` only after `run_phase()` returns;
- validates the selected decision against the current matrix and baseline ID;
- sets `context.research_gate_status` and `context.research_gate_decision`; and
- returns `BOOTSTRAP` only after that validation succeeds.

The relevant control flow is the `_research_gate()` implementation around the
`for attempt in range(...)` loop and its post-`run_phase()` `event` check.

### 3.3 The transition tools publish only local state

`src/agenthicc/workflows/reconstruct_site/phase_impl.py` defines
`_make_research_gate_tools()`. The approval tools currently:

- validate that their immediate string/list arguments are non-empty;
- update a caller-owned `data` dictionary;
- set a caller-owned `asyncio.Event`; and
- return `{ "ok": true, ... }`.

They do not themselves validate the coverage matrix, compare the baseline
revision, persist a decision receipt, or request that the current agent turn
stop. The domain validation occurs later in the specialized runner.

### 3.4 The agent-turn loop has no generic terminal-control boundary

`CodePlanRunner._run_turn()` classifies `ToolCapability.CONTROL` tools and
passes them to `_run_agent_turn()`. The control capability affects tool
classification and prompt construction, but it does not itself stop the
Lauren agent runner after a successful control result. The event is a
workflow-local signal, invisible to the provider loop until the call returns.

The existing prompt instruction says “After a successful transition call, stop
and let the runner take control.” That is useful guidance but is not a runtime
invariant. A model can ignore the instruction or emit another tool call before
the outer runner observes the event.

### 3.5 The durable boundary is later than the approval callback

`_run_context()` persists a phase's evidence and calls
`checkpoint_phase_boundary()` only after the phase handler returns a different
state. Consequently, the current sequence is approximately:

```text
approval tool returns
  -> local data/event set
  -> _run_agent_turn continues or returns
  -> _research_gate validates and mutates context
  -> _run_context persists research_gate evidence
  -> boundary checkpoint records BOOTSTRAP
```

An interruption in the first three arrows can leave the checkpoint with the
old gate cursor and no durable approval, even when the transcript shows a
successful tool result.

### 3.6 Resume uses evidence, but the decision is not a first-class cursor

Resume currently rehydrates `research_gate_receipt` when one exists and uses
phase receipts/journal boundaries to reconcile the phase cursor. However,
`research_gate_status`, `research_gate_decision`, baseline identity, and the
boundary cursor are not treated as one atomic decision record. A checkpoint
that predates the gate receipt, a missing receipt, or a process stop during the
approval-to-boundary window can make a valid prior approval appear pending.

### 3.7 There are two source locations for the gate behavior

The current package contains:

- the optimized registry runner at
  `src/agenthicc/workflows/reconstruct_site/runner.py`; and
- the older/specialized implementation and phase metadata in
  `src/agenthicc/workflows/reconstruct_site/phase_impl.py`.

The package registry points to the optimized `runner.py` plugin, which imports
the phase tool factories and phase metadata from `phase_impl.py`. The older
module still defines a `ReconstructSiteRunner` and its own `_research_gate()`.
That makes direct imports, compatibility callers, and tests vulnerable to
different gate semantics. The fix must establish one executable gate path and
make all other references delegate to it or become clearly declarative.

## 4. Root-cause analysis

### 4.1 Confirmed primary cause: transition success does not terminate the inner turn

The approval tool's event is a signal to the specialized outer loop, not a
signal to the provider/agent loop. The outer loop cannot inspect the signal
until `_run_agent_turn()` completes. If the model receives:

```json
{"ok": true, "message": "Degraded research approval recorded."}
```

it may continue reasoning and call `approve_degraded_research` again. Every
call is rendered as another tool completion. This explains duplicate calls
within one invocation of `_research_gate()` without requiring any checkpoint
corruption.

### 4.2 Confirmed secondary cause: approval is not committed atomically with the
transition signal

The decision data and event are in-memory objects local to a gate attempt. The
durable research receipt and phase checkpoint are written later. A process
interruption can therefore preserve the model transcript without preserving
the exact gate decision as resumable workflow state.

### 4.3 Confirmed contributing cause: no idempotency or conflict contract

The current tool has no decision key and no “already accepted” branch. A
repeated call is indistinguishable from a new call. The workflow cannot safely
answer:

- “Is this the same approval for the same baseline?”
- “Is this approval for an older baseline?”
- “Did the first call commit before the process stopped?”
- “Is this a conflicting second rationale or exception set?”

### 4.4 Confirmed contributing cause: state and evidence are separate commit
surfaces

The context, evidence manifest, conversation journal, and workflow checkpoint
are updated at different points. They are intentionally separate stores, but
the gate decision needs a common transaction identity and recovery rule so a
restart can reconcile them deterministically.

### 4.5 Possible symptom amplifier: re-entry intentionally invalidates approval

When a later validation phase requests re-entry, `_run_context()` invalidates
dependent evidence and sets `research_gate_status` back to `pending`. In that
case another approval is correct, but it must be associated with a new
baseline fingerprint and explicitly explain why the previous decision is no
longer valid. The new behavior must distinguish this intentional invalidation
from accidental replay.

### 4.6 Possible symptom amplifier: failed domain validation

`approve_degraded_research()` performs only shape validation. The runner may
later reject the call if:

- the baseline ID is stale;
- any blocking cell is pending, stale, or contradictory;
- exception IDs do not exactly cover all unavailable cells; or
- the coverage matrix changed after the baseline was published.

In that case the outer loop deliberately asks for another attempt. The TUI
must show the structured rejection reason, and the model must not be told that
the approval succeeded. The PRD does not suppress legitimate retries; it makes
them explicit and prevents retries after a committed decision.

## 5. Goals and non-goals

### 5.1 Goals

- Guarantee at most one committed terminal decision per baseline fingerprint.
- Stop the active agent turn immediately after a successful control transition.
- Make exact repeated calls idempotent and side-effect free.
- Reject stale or conflicting calls with a structured, recoverable result.
- Persist the decision before exposing it as a successful phase transition.
- Resume directly at `bootstrap` when an approval is already committed and its
  baseline remains valid.
- Reopen the gate only after a deliberate evidence revision or re-entry.
- Preserve a single full conversation and the current research evidence model.
- Keep cache-stable prompts and tool schemas unchanged by dynamic decision data.
- Make repeated-call diagnosis observable in headless logs, the journal, and
  the TUI transcript.
- Provide deterministic unit, integration, E2E, fault-injection, and
  compatibility coverage.

### 5.2 Non-goals

- Changing the coverage requirements or allowing degraded approval to bypass
  pending/stale/contradictory cells.
- Automatically approving unavailable evidence without explicit agent/user
  intent.
- Removing the research gate from `reconstruct_site`.
- Creating a second conversation, memory store, or workflow state machine.
- Making arbitrary prose advance a phase.
- Hiding legitimate rejected attempts from audit history.
- Eliminating all retries for malformed or invalid tool arguments.

## 6. User journeys

### 6.1 Normal degraded approval

1. Research phases produce evidence and the runner publishes baseline `B`.
2. The gate prompt explains that only explicitly unavailable cells may be
   degraded.
3. The agent calls `approve_degraded_research` once for `B`.
4. The tool validates and durably records decision `D`.
5. The tool result is returned as a terminal result for the current agent turn.
6. The outer runner observes the committed decision and transitions to
   `bootstrap`.
7. One `research_gate_receipt` and one boundary checkpoint are written.
8. The TUI shows one approval call and the next phase.

### 6.2 Model repeats the call after success

1. The agent calls the approval tool for `B`.
2. The runtime marks the tool call terminal and does not send another provider
   request for the same phase turn.
3. If a transport/runtime race delivers a duplicate call anyway, the tool
   returns the original decision with `idempotent: true`, creates no new
   receipt, and does not display a second approval action.
4. The outer runner continues from the already committed state.

### 6.3 Process stops after approval

1. The approval tool commits `D` and its decision receipt.
2. The process stops before the normal phase-boundary callback.
3. On resume, evidence and checkpoint reconciliation find `D`, verify `B`,
   and complete the missing boundary publication without calling the LLM for
   the gate.
4. The workflow starts at `bootstrap`.

### 6.4 Baseline becomes stale through re-entry

1. A later validation phase requests re-entry to a research-owning phase.
2. The runner invalidates affected evidence and records a new baseline
   fingerprint requirement.
3. The old decision remains in history but is marked superseded/stale.
4. The gate presents a new approval request tied to baseline `B2`.
5. Approval of `B2` is a new decision, not a duplicate of `B`.

### 6.5 Invalid degraded approval

1. The agent names a pending cell as an exception or omits an unavailable
   cell.
2. The domain validator returns a structured error with the exact cells and a
   corrective instruction.
3. No decision is committed and the phase remains pending.
4. A subsequent valid call may succeed, and only the successful call ends the
   phase turn.

## 7. Functional requirements

### FR-1 — Canonical executable gate

There MUST be one executable implementation of `reconstruct_site` research-gate
decision handling. The registry-selected optimized runner MUST be authoritative.
The `phase_impl.py` phase definitions MAY remain as metadata/tool factories,
but its duplicate runner behavior MUST either delegate to the canonical runner
or be removed as an executable alternative. Direct imports and compatibility
tests MUST observe the same validation, idempotency, persistence, and resume
semantics.

### FR-2 — Explicit decision state machine

The gate MUST model the following states for a baseline fingerprint:

```text
PENDING
  ├─ approve complete       ─> APPROVED
  ├─ approve degraded       ─> APPROVED_DEGRADED
  ├─ reject                  ─> REJECTED / RESEARCH_REENTRY_REQUESTED
  └─ invalid/stale request  ─> PENDING (with diagnostic)

APPROVED / APPROVED_DEGRADED
  ├─ exact replay            ─> same terminal state, idempotent result
  ├─ same baseline conflict  ─> CONFLICT (no state change)
  └─ changed baseline        ─> STALE (new gate decision required)
```

Terminal means terminal for the current baseline, not necessarily for the
entire workflow. A deliberate evidence invalidation starts a new baseline
epoch.

### FR-3 — Stable decision identity

Each gate decision MUST include a deterministic `decision_key` derived from:

- workflow name and run ID;
- baseline artifact ID and normalized baseline content hash;
- decision action (`approve` or `approve_degraded`);
- sorted exception IDs for degraded approval; and
- a normalized decision protocol version.

The free-text summary/rationale MUST be retained for audit, but changing only
that text MUST NOT create a second committed decision. A conflicting payload
for an already committed key MUST return a conflict result and preserve the
original decision. The implementation MUST NOT use volatile timestamps or
random IDs as the only idempotency key.

### FR-4 — Current-baseline validation

Before committing an approval, the runner MUST verify:

- the supplied baseline artifact ID is the current published baseline;
- the baseline content hash matches the current normalized coverage;
- the manifest revision is not stale in a way that changes relevant cells;
- complete approval has no blocking cells; and
- degraded approval names every unavailable cell and no other cell.

The existing `ResearchGate` completeness rules remain authoritative. A failed
validation MUST NOT set the transition event or persist an approval.

### FR-5 — Durable-before-visible commit ordering

For a successful approval or rejection, the implementation MUST use this
ordering:

```text
validate current baseline and payload
  -> write/commit decision receipt (idempotently)
  -> update typed context with decision and receipt identity
  -> persist the decision checkpoint/boundary intent
  -> publish transition event / terminal tool result
```

If persistence fails, the tool MUST return a structured recoverable error and
MUST NOT report success or set the transition event. A retry of the same
decision after a transient persistence error MUST be safe.

The normal phase-boundary checkpoint remains required. The decision commit is
an earlier recovery point, not a replacement for the complete phase receipt.

### FR-6 — Terminal control-tool result

After a successful control transition, `_run_agent_turn()` MUST stop issuing
provider requests for the current logical phase turn. This MUST be a runtime
guarantee, not only a prompt instruction.

The lauren-ai integration SHOULD expose a typed terminal result such as
`AfterToolHookDecision.stop()` or an equivalent `stop_after_tool` signal. If
the installed lauren-ai version does not provide that API, agenthicc MUST use
an explicit compatibility adapter with the same semantics and fail closed if
it cannot prevent another provider request.

The terminal signal MUST apply only after successful control-tool execution.
Invalid control-tool calls must still return their structured error so the
agent can correct the arguments within the bounded phase retry policy.

### FR-7 — Tool result contract

Every research-gate transition tool result MUST include a stable structured
shape:

```json
{
  "ok": true,
  "action": "approve_degraded",
  "decision_key": "...",
  "baseline_artifact_id": "...",
  "baseline_hash": "...",
  "status": "approved_degraded",
  "idempotent": false,
  "terminal_for_turn": true,
  "message": "..."
}
```

Errors MUST include `ok: false`, a stable error code, a bounded human-readable
message, and a corrective instruction. Replayed success MUST set
`idempotent: true` and MUST retain the original decision identity.

### FR-8 — Duplicate-call suppression in the TUI

The TUI MUST render each committed gate decision once per decision key. A
duplicate provider/tool event may be retained in low-level audit storage, but
the user-facing workflow transcript MUST identify it as an idempotent replay
or suppress the duplicate control presentation according to the existing
event-projection contract. It MUST NOT look like a new approval request.

### FR-9 — Resume-aware decision reconciliation

On resume, before any provider call, the runner MUST reconcile:

- typed checkpoint context;
- the evidence manifest and hashes;
- the decision receipt(s);
- the phase receipt(s);
- the workflow boundary journal; and
- the current phase cursor.

If a valid committed decision exists for the current baseline, the runner MUST:

- restore `research_gate_status` and `research_gate_decision`;
- restore the exception list and decision key;
- skip the gate provider turn;
- finish or verify the missing phase-boundary checkpoint; and
- continue at `bootstrap`.

If the decision receipt is missing but the checkpoint has an accepted decision,
the runner MUST recreate the receipt from the checkpoint only after validating
the baseline hash. If the checkpoint and evidence disagree, the runner MUST
choose the more conservative state (`pending`), record a bounded diagnostic,
and require a new gate decision rather than guessing.

### FR-10 — Re-entry invalidation creates a new epoch

When research evidence is invalidated by an intentional re-entry, the runner
MUST record:

- the superseded decision key;
- the invalidating phase and reason;
- the affected evidence/artifact IDs;
- the new expected baseline epoch/fingerprint; and
- the phase cursor that requires renewed approval.

An old `approved_degraded` decision MUST NOT authorize a changed baseline. The
new gate prompt MUST name the reason for renewed approval.

### FR-11 — Bounded retries with clear semantics

The existing bounded gate-attempt policy MAY remain. Its semantics MUST be:

- invalid arguments or domain validation failures: retryable pending state;
- successful first decision: stop the current agent turn and exit the gate;
- exact duplicate after commit: idempotent success and exit the gate;
- stale/conflicting decision: no state change, structured diagnostic, and no
  automatic approval replay;
- exhausted attempts: fail with a reason that distinguishes “no decision,”
  “invalid decision,” and “persistence failure.”

The workflow MUST NOT call `approve_degraded_research` again merely because
the model produced prose after a successful call.

### FR-12 — Cache-contract preservation

Decision state, baseline IDs, coverage digests, exception lists, and retry
diagnostics MUST remain in dynamic prompt context. They MUST NOT be inserted
into the immutable cache-stable system prefix or stable tool schema. The
terminal-control implementation MUST not reorder the stable tools or mutate
the cache contract across equivalent gate attempts.

### FR-13 — Conversation continuity

The gate MUST continue using the existing workflow/session conversation and
shared memory. A decision receipt is a workflow durability record; it is not a
second conversation and must not replace the LLM journal. The same
`conversation_id` must survive gate retries and resume.

### FR-14 — Observability

The runner and TUI/headless projection MUST expose bounded diagnostic fields:

- `run_id`, `conversation_id`, and workflow name;
- current baseline artifact ID/hash and manifest revision;
- decision key and status;
- `tool_call_id` and whether the result was idempotent;
- `terminal_for_turn`;
- gate attempt number and provider-turn number;
- decision source (`new`, `checkpoint`, `receipt`, `idempotent_replay`, or
  `reentry`); and
- stale/conflict/validation reason when applicable.

No prompt, log, or event may expose secrets from tool arguments or research
artifacts.

## 8. Proposed data model

### 8.1 `ResearchGateDecisionRecord`

The implementation SHOULD introduce one typed, JSON-safe record (name may
follow repository conventions) containing at least:

| Field | Meaning |
|---|---|
| `schema_version` | Decision protocol version |
| `run_id` | Workflow run identity |
| `workflow_name` | Must be `reconstruct_site` |
| `conversation_id` | Parent session identity |
| `decision_key` | Stable idempotency identity |
| `action` | `approve`, `approve_degraded`, or `reject` |
| `status` | `approved`, `approved_degraded`, `rejected`, `stale`, or `conflict` |
| `baseline_artifact_id` | Content-addressed current baseline |
| `baseline_hash` | Normalized baseline content hash |
| `manifest_revision` | Relevant evidence-manifest revision |
| `exception_ids` | Exact accepted unavailable cells |
| `summary` | Bounded complete-approval explanation |
| `rationale` | Bounded degraded-approval explanation |
| `blocking_cell_ids` | Snapshot used during validation |
| `source` | New call, replay, checkpoint, receipt, or re-entry |
| `created_at` | Audit timestamp, not used for identity |
| `updated_at` | Audit timestamp, not used for identity |
| `supersedes` | Prior decision key, if re-entry created a new epoch |
| `receipt_artifact_id` | Durable evidence receipt identity |
| `boundary_checkpoint_revision` | Checkpoint that completed the phase boundary |

The checkpoint codec MUST preserve the compact decision record and key, not
the full research corpus. The evidence store remains the source for large
coverage and artifact bodies.

### 8.2 Decision store semantics

The decision store may be implemented using the existing evidence manifest,
phase receipt, workflow journal, or a small protocol-specific record, but it
MUST satisfy these properties:

- atomic publication using existing workspace/checkpoint primitives;
- content-addressed or otherwise deterministic identity;
- same-key replay returns the original record;
- conflicting payloads are not overwritten;
- records are scoped to one run and workflow;
- records are hash-verified on resume; and
- no secret-bearing input is persisted without existing redaction.

## 9. Detailed data flow

### 9.1 Fresh run

```text
research phases
    │ produce observations, screenshots, and artifacts
    ▼
ReconstructEvidenceStore
    │ writes coverage report + fidelity baseline B
    │ computes baseline_id, baseline_hash, manifest_revision
    ▼
ReconstructSiteRunner._research_gate
    │ builds dynamic digest and phase-local control tools
    ▼
CodePlanRunner.run_phase → _run_turn → _run_agent_turn
    │ sends one phase turn with stable contract + dynamic gate context
    ▼
approve_degraded_research(...)
    │ validates payload and current B
    │ computes decision_key D
    │ atomically writes ResearchGateDecisionRecord(D)
    │ updates context.research_gate_decision/status
    │ persists decision checkpoint intent
    │ returns terminal_for_turn=true
    ▼
lauren-ai stops current inner agent turn
    ▼
_research_gate observes committed D
    │ returns BOOTSTRAP
    ▼
_run_context
    │ writes research_gate_receipt and phase receipt
    │ writes phase-boundary checkpoint to BOOTSTRAP
    ▼
bootstrap provider turn
```

### 9.2 Duplicate call race

```text
call D ──commit──> decision store contains D ──terminal result──> outer loop
   └─ duplicate D (if already queued)
          │ same decision_key + same baseline
          ▼
       return original D with idempotent=true
       no second receipt / no state mutation / no new approval presentation
```

### 9.3 Resume after process stop

```text
checkpoint + journal + evidence manifest
              │
              ▼
resume → rehydrate typed context and shared conversation
              │
              ├─ verify manifest/artifact hashes
              ├─ load decision record by run_id + baseline identity
              ├─ reconcile phase receipts and boundary journal
              └─ compare decision baseline_hash with current baseline
              │
              ├─ valid approved decision → repair boundary → BOOTSTRAP
              ├─ valid pending decision  → RESEARCH_GATE provider turn
              ├─ stale decision          → publish reason → new gate epoch
              └─ disagreement/corruption → conservative recovery diagnostic
```

### 9.4 Intentional re-entry

```text
validation rejection(target_phase)
    ▼
invalidate dependent evidence + mark prior decision superseded
    ▼
new coverage digest / baseline B2 / decision epoch E2
    ▼
research_gate asks for approval tied to B2
    ▼
decision for B2 is distinct from decision for B1
```

## 10. Cross-repository lauren-ai requirement

The cleanest implementation is a small lauren-ai control-flow extension rather
than workflow-specific polling. The exact API is an implementation choice, but
it MUST provide all of the following:

1. A tool result or hook decision that means “successful tool execution; do not
   issue another provider request in this agent turn.”
2. A way for agenthicc's `ToolCapability.CONTROL` tools to opt into that
   result without affecting ordinary read/write/network tools.
3. Preservation of the completed assistant tool-call/result exchange in
   conversation memory and the journal.
4. A compatibility path for installed versions without the new API.
5. Tests proving that multiple tools in one provider batch are handled
   deterministically and that a terminal control result prevents subsequent
   model calls.

The extension MUST NOT roll back the committed tool result or delete the
assistant's preceding reasoning. It only stops the inner provider loop after
the successful terminal control result. This is especially important because
the shared conversation is used for future phases and resume.

If an upstream change is not immediately available, agenthicc MAY implement a
local adapter around the existing runner, but the adapter must have the same
typed contract and must be removed or delegated when the supported lauren-ai
API is present. The workflow must not depend on model compliance with a prose
“stop” instruction.

## 11. Error and recovery behavior

| Condition | Tool result | Persist decision? | Gate behavior |
|---|---|---:|---|
| Complete baseline, first approval | success, terminal | yes | advance |
| Degraded baseline, exact unavailable IDs | success, terminal | yes | advance |
| Exact replay of committed decision | success, idempotent, terminal | no new record | advance |
| Different exception set for same baseline | conflict | no | remain pending/conflict |
| Old baseline ID | stale | no | require current baseline |
| Pending/stale/contradictory cell | validation error | no | retry/research |
| Missing exception ID | validation error | no | retry/research |
| Manifest hash mismatch | integrity error | no | recovery/research |
| Decision write failure | persistence error | no | retry safely, no success |
| Boundary checkpoint failure after decision commit | committed decision + recoverable boundary error | decision yes | resume repairs boundary |
| Process crash after decision commit | none at crash | decision yes | resume skips provider gate |
| Deliberate re-entry | superseded old decision | new decision later | new gate epoch |

## 12. Acceptance criteria

### AC-1 — One successful call advances once

Given a baseline with only unavailable cells remaining, when the agent calls
`approve_degraded_research` with the exact exception IDs and rationale, then:

- exactly one decision record is committed;
- the tool result has `ok=true`, `status=approved_degraded`, and
  `terminal_for_turn=true`;
- `_run_agent_turn()` performs no further provider request for that phase turn;
- the outer runner transitions to `bootstrap`; and
- one research-gate boundary is recorded.

### AC-2 — Repeated tool call is idempotent

Given a committed decision for baseline `B`, when the same tool call is
replayed with the same action, baseline, and exception IDs, then:

- the original decision key and receipt are returned;
- the result is marked `idempotent=true`;
- no second decision record, evidence receipt, boundary checkpoint, or TUI
  approval presentation is created; and
- the workflow remains or becomes `bootstrap`, never a new pending gate.

### AC-3 — Repeated calls inside one provider turn are impossible at runtime

Given a model that would call the same control tool again after seeing a
successful result, when the first call succeeds, then the runtime prevents the
second provider request. A test MUST count provider invocations, not merely
inspect prompts.

### AC-4 — Invalid calls remain retryable

Given a pending cell or incomplete exception list, when degraded approval is
called, then the result is a structured validation error, no decision is
committed, no terminal success signal is emitted, and a later corrected call
can succeed.

### AC-5 — Stale baseline is rejected

Given a new baseline has replaced `B`, when the agent submits approval for `B`,
then the result has a stale/conflict code, no approval is committed for the new
baseline, and the next prompt explains the current baseline identity.

### AC-6 — Conflicting same-baseline payload is not overwritten

Given decision `D` is committed for baseline `B`, when a second call uses `B`
but a different exception set/action, then the original `D` remains unchanged,
the result is a conflict, and the TUI does not represent it as a second valid
approval.

### AC-7 — Resume after decision-before-boundary crash

Given the process stops after the decision record is durable but before the
normal phase-boundary checkpoint, when the run resumes, then:

- the decision is hash-verified;
- no research-gate provider call occurs;
- the missing gate receipt/boundary is repaired idempotently; and
- execution starts at `bootstrap`.

### AC-8 — Resume with a fully persisted boundary

Given a completed `research_gate` boundary with `approved_degraded`, when the
session is resumed, then the gate is not entered and the decision remains
available in the typed context and evidence manifest.

### AC-9 — Resume disagreement fails conservatively

Given a checkpoint says approved but the decision receipt is missing or has a
different baseline hash, when the run resumes, then it does not skip research
silently. It records a bounded diagnostic and returns to a pending recovery
gate/research state.

### AC-10 — Intentional re-entry requires new approval

Given a later validation phase invalidates evidence used by baseline `B`, when
the runner re-enters research, then the old decision is marked superseded and
approval for the new baseline `B2` is required. Replaying the old `B` call
cannot advance the workflow.

### AC-11 — Complete approval has the same contract

The same one-shot, idempotent, durable, stale-aware behavior MUST apply to
`approve_research_baseline`, including a complete matrix and empty exception
set.

### AC-12 — Rejection is not mistaken for approval

`reject_research_baseline` MUST use the same durable decision identity and
terminal-turn behavior, while preserving its targeted re-entry semantics.

### AC-13 — TUI accurately explains duplicates

Given a duplicate tool event is received, the transcript either renders one
user-facing control result or explicitly labels the second as an idempotent
replay. It never shows multiple indistinguishable approval requests for one
decision key.

### AC-14 — Cache stability is preserved

Given two gate turns in one cache epoch where only dynamic coverage/decision
state changes, the stable system prompt and stable tool schemas are byte-for-
byte equivalent. Terminal-control metadata is dynamic or tool metadata and
does not enter the stable prompt prefix.

### AC-15 — Single executable behavior

The registry path, direct compatibility import path, and tests all exercise the
same gate semantics. No second runner can accept a decision without current-
baseline validation, durable identity, and terminal-turn behavior.

## 13. Testing strategy

The implementation MUST add new clean-room tests. Existing tests may be
retained as regression coverage, but they are not sufficient by themselves for
this PRD.

### 13.1 Unit tests

Add isolated tests for:

- deterministic decision-key generation and normalization;
- same-key replay with identical payload;
- same-baseline conflicting action/exception payload;
- stale baseline ID and baseline-hash mismatch;
- complete approval and degraded approval validation;
- rejection decision identity and target validation;
- decision record serialization, redaction, and bounded fields;
- atomic decision-store publication and duplicate write behavior;
- checkpoint codec round trips with compact decision state;
- terminal-control result classification;
- invalid control-tool results not stopping the agent turn; and
- TUI event projection deduplication by decision key.

### 13.2 Integration tests

Use temporary workspaces, a fake provider, a fake workflow handle, and a fake
conversation journal to verify:

- a single approval call writes the decision before the event is observed;
- the outer runner writes one gate receipt and one phase boundary;
- the fake provider cannot make a second call after terminal control success;
- a duplicate tool invocation returns an idempotent result without new writes;
- a persistence failure does not expose success or transition state;
- a crash after decision commit but before boundary is repaired on resume;
- a crash before decision commit keeps the gate pending;
- manifest revision/hash changes mark the decision stale;
- re-entry supersedes the old decision and creates a new baseline epoch;
- journal/checkpoint/evidence disagreement follows conservative recovery; and
- the stable prompt/tool contract remains unchanged across gate retries.

### 13.3 End-to-end tests

Run the offline fixture workflow through the real runner/TUI event boundary and
cover:

1. browser-unavailable degraded approval;
2. complete baseline approval;
3. a model fixture that deliberately emits a second approval after a success;
4. interruption immediately after the tool result;
5. interruption immediately before decision persistence;
6. resume from checkpoint, journal, and evidence-only recovery combinations;
7. intentional validation re-entry followed by a new approval; and
8. headless and interactive transcript projections.

Assertions MUST count provider calls, durable records, phase transitions, and
user-visible approval presentations. They MUST not rely only on the final
`research_gate_status` value.

### 13.4 Fault-injection and concurrency tests

Inject failures at each ordering boundary:

- before decision write;
- after decision write;
- after context update;
- before event publication;
- after event publication;
- before phase receipt;
- before boundary checkpoint; and
- after boundary checkpoint but before journal indexing.

Concurrent duplicate calls for one decision key MUST converge on one record.
Concurrent conflicting calls MUST produce one winner and explicit conflict
results without lost updates.

## 14. Non-functional requirements

### NFR-1 — Correctness and durability

No successful user-visible gate transition may exist without a recoverable
decision record. No decision record may authorize a different baseline.

### NFR-2 — Bounded overhead

The decision record and checkpoint addition MUST be compact and independent of
the size of screenshots or research bodies. Duplicate calls MUST not cause
unbounded receipt or manifest growth.

### NFR-3 — Determinism

Decision identity and replay behavior MUST be deterministic across process
restarts, Python versions supported by the project, and equivalent JSON key
ordering.

### NFR-4 — Security

Decision summaries, rationales, diagnostics, and persisted payloads MUST use
the existing bounded/redacted persistence conventions. Baseline IDs and cell
IDs may be exposed; secrets in observations or tool arguments may not.

### NFR-5 — Compatibility

Existing public tool names and valid argument shapes remain supported. New
fields are additive and have safe defaults for old checkpoints. Old
checkpoints without a decision record are treated as pending and reconciled
conservatively.

### NFR-6 — Provider neutrality

The terminal-turn contract MUST work for OpenAI-compatible, Anthropic,
DeepSeek, Modal, and other supported transports. It cannot depend on a
provider-specific textual stop token.

### NFR-7 — Observability without noise

Duplicate suppression must reduce misleading user-visible output while keeping
enough structured audit information to diagnose a race, replay, stale request,
or persistence failure.

## 15. Rollout and migration

1. Add the typed decision protocol and tests behind the canonical
   `reconstruct_site` runner.
2. Add the lauren-ai terminal-control adapter and verify behavior with the
   supported installed version(s).
3. Enable durable-before-visible ordering for new runs.
4. On resume, recognize old gate receipts/checkpoints that contain a valid
   `approved`/`approved_degraded` status but no decision key by deriving a
   migration key from the persisted baseline identity. If identity cannot be
   verified, require a new gate decision.
5. Keep existing artifact paths and research profile semantics.
6. Remove or delegate the duplicate executable gate implementation in
   `phase_impl.py`; do not introduce another compatibility runner.
7. Update `docs/guides/workflows.md`, reconstruct-site findings/reference
   documentation, public symbol inventories if new symbols are exported, and
   this PRD index entry.

## 16. Risks and mitigations

### Risk: stopping the inner turn loses the tool result

Mitigation: stop only after Lauren commits the assistant tool-call/result
exchange. Add provider-valid conversation assertions and interruption tests.

### Risk: a duplicate is incorrectly treated as a conflict

Mitigation: derive identity from normalized semantic fields, not free-text
summary/rationale or timestamps. Preserve the original record and return it for
exact replay.

### Risk: stale evidence is accidentally approved

Mitigation: compare baseline artifact ID, content hash, relevant manifest
revision, and current coverage before committing. Fail closed on disagreement.

### Risk: upstream lauren-ai versions differ

Mitigation: isolate the terminal-control API in a small adapter, feature-detect
the supported method, and test both native and compatibility paths.

### Risk: old direct imports continue using the duplicate runner

Mitigation: canonicalize the import path and add a test that registry and
direct compatibility construction resolve to identical gate behavior.

## 17. Definition of done

- All functional and non-functional requirements in this PRD are implemented.
- One successful research-gate control call cannot trigger another provider
  request in the same inner agent turn.
- Exact duplicate calls are idempotent and do not create duplicate durable or
  user-visible approvals.
- Stale and conflicting calls are structured, recoverable, and non-mutating.
- Decision state survives interruption and resume before and after the phase
  boundary.
- Intentional research re-entry creates a new decision epoch.
- The optimized runner is the sole executable gate behavior.
- Unit, integration, E2E, fault-injection, concurrency, and compatibility
  tests pass deterministically.
- Relevant lint, typing, documentation, and repository checks pass.
- The PRD index and workflow documentation link this document and record its
  implementation status.
