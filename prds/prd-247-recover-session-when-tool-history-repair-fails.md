---
title: "PRD-247: Recover and Open Sessions When Tool-History Repair Fails"
status: Implemented
version: 1.2.0
date: 2026-10-19
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-169, prd-170, prd-173, prd-182, prd-191, prd-238, prd-243]
tags: [sessions, tool-history, journal, recovery, workflows, lauren-ai]
---

# PRD-247 — Recover and Open Sessions When Tool-History Repair Fails

## 1. Executive summary

A background `make_book` session failed while writing chapter 8, then became
impossible to open in the foreground:

```text
State: failed
Workflow: make_book
Latest activity: ... build and empirically verify ...
Error: chapter 8 never confirmed completion
Failure category: recoverable_workflow

Unable to open foreground session ...:
ValueError: repaired tool history remains invalid
```

The report contains two distinct failures. The workflow runner reached its
bounded chapter-attempt limit without receiving a successful
`confirm_chapter_complete` transition. That message does not prove the chapter
file was absent; the file may exist even though its phase gate was not
recorded. Separately, while reopening the session, Agenthicc's journal-backed
conversation attempted to repair a non-adjacent tool-result exchange. The
repair candidate still failed Lauren-ai's tool-history validator, so session
construction raised a generic `ValueError` and foreground opening stopped.

The second error masks the first failure's recovery path. It can prevent the
user from inspecting the transcript, checking the existing chapter artifact,
or resuming the exact workflow checkpoint. The supplied report does not
include the session journal, so the precise malformed exchange (for example,
duplicate, orphaned, malformed, or still-non-adjacent results) cannot be
identified from the evidence available. The implementation must preserve this
uncertainty and expose a safe, redacted diagnosis rather than guess.

This PRD proposes a layered fix: make tool-history repair transactional and
diagnostic; keep a session inspectable when provider-facing history cannot be
made valid; offer explicit, non-destructive recovery from the last validated
conversation/workflow boundary; and preserve `make_book`'s chapter-level
checkpoint so recovery does not restart the book from INIT or repeat completed
chapters. Changes to `lauren-ai` are required only if inspection proves its
shared validator/repair contract is insufficient or incorrect.

## 2. Incident and user impact

The session manager showed a failed but recoverable workflow. Selecting it to
open the foreground session failed before the user could continue. This
creates a confusing state: the background manager can describe the session,
but the interactive session cannot initialize its provider conversation.

The observed sequence is:

```text
make_book chapter 8 phase
  → no successful chapter-completion tool transition within bounded attempts
  → workflow records "chapter 8 never confirmed completion"
  → background attempt is classified recoverable_workflow
  → user selects/open session
  → session conversation folds its durable journal
  → non-adjacent tool history is repaired in a candidate projection
  → Lauren-ai validation still reports invalid history
  → repair helper rolls back and raises ValueError
  → TUI cannot finish opening the session
```

Impact:

- The user cannot use the normal foreground recovery path for a recoverable
  workflow.
- A malformed provider transcript can make otherwise useful session metadata,
  transcript, and workflow checkpoint appear inaccessible.
- The generic error omits the remaining validator issue codes, hiding whether
  the problem is repairable and which safe recovery action is available.
- If foreground ownership/handoff has already begun when initialization fails,
  the attach failure must not strand ownership or make the session harder to
  reopen.
- Automatically starting a new workflow or sending the invalid history to an
  LLM could lose work, duplicate effects, or trigger a provider protocol error.

## 3. Evidence and current-state findings

### 3.1 What the supplied report proves

It proves that:

1. The session was associated with `make_book` and its failure category was
   `recoverable_workflow`.
2. The workflow recorded `chapter 8 never confirmed completion`.
3. Opening the same session then raised
   `ValueError: repaired tool history remains invalid`.

It does **not** prove that chapter 8's Markdown file or assets are missing, that
the LLM failed to write them, or which tool-call/result IDs made the journal
invalid. The session's
`~/.agenthicc/sessions/<session-id>/conversation-journal.jsonl` was not present
in the workspace used for this investigation; the production journal could
not be inspected.

### 3.2 Confirmed `make_book` failure boundary

In `src/agenthicc/workflows/make_book/runner.py`, `_chapter()` retries the
chapter agent phase up to `_MAX_ATTEMPTS` (currently five). The transition
event is set only when `confirm_chapter_complete(summary=...)` validates the
canonical chapter file and its referenced assets and records a receipt. If no
attempt sets that event, `_chapter()` sets:

```text
chapter <index> never confirmed completion
```

For chapter 8, this indicates that the success gate was not recorded. It is
not equivalent to a filesystem assertion that the output does not exist. A
recovery path must inspect/revalidate existing output before asking the agent
to recreate it, and must preserve the same chapter index and workflow run.

### 3.3 Confirmed session-open failure boundary

The current path is:

```text
agenthicc agents attach/open
  → _open_foreground_session()
  → _run_tui_session(resume_id=...)
  → SessionConversation.open()
  → JournaledShortTermMemory.__init__()
  → fold conversation journal
  → repair_non_adjacent_tool_history()
```

`JournaledShortTermMemory.__init__()` invokes the Agenthicc repair before
normal startup completes. `repair_non_adjacent_tool_history()` currently:

1. selects `non_adjacent_results` findings from Lauren-ai's validation report;
2. moves matching results by tool-call ID and synthesizes an interruption
   result for a missing expected result;
3. restores the candidate projection; and
4. raises the generic `ValueError` if a second validation still reports an
   issue, restoring the original projection first.

The generic exception means the candidate did not satisfy the complete
validator contract. Because the exception drops the final structured report,
the observed error cannot reveal whether another issue remained or the repair
algorithm produced an invalid ordering. The current design also places this
strict repair in the constructor, so a provider-history invariant failure
becomes a whole-session initialization failure instead of an explicit
recoverable session state.

### 3.4 Lauren-ai boundary

Lauren-ai owns canonical provider-neutral tool-history validation and
transaction semantics. Agenthicc owns the durable journal, session recovery,
workflow checkpoint reconciliation, and user-facing choices. The
implementation must inspect the supported Lauren-ai contract and determine
which side is responsible for the invalid final projection. Do not duplicate a
second validator in Agenthicc or make Lauren-ai accept malformed history merely
to get past session opening.

### 3.5 Compaction is a plausible contributor, not the proven incident cause

The reported readiness message contains no compaction event or journal
contents, so it does not establish that compaction caused this particular
incident. Code inspection did identify a concrete related race: manual
`/compact` starts independently of the session-conversation lease. Summary
generation and exact token counting await I/O while the live turn can append
tool messages; the compactor previously replaced the live projection from its
older snapshot without checking whether it had changed.

The regular transformations should not create the reported issue codes from a
valid, unchanged history: summary compaction produces a plain summary/ack pair,
and local fallback retains a suffix beginning at a user-turn boundary while
preserving tool-call/result blocks. That does not rule out a race in a real
incident. A stale replacement can discard newer tool history, and local
fallback applied to already-malformed history can preserve or expose an
invalid boundary. Compaction must therefore be serialized with session turns
where possible and compare its source snapshot with the live projection
immediately before commit. If the source changed, preserve the current
projection and report that compaction was skipped.

Follow-up inspection confirmed that Agenthicc disables Lauren-ai's native
automatic compactor for agent turns and routes compaction through
`memory/compactor.py`. The journaled path now performs snapshot
compare-before-commit and refuses replacement while a tool exchange is active.
Both summary and local compaction must also validate the source history before
work begins, and validate every replacement candidate again before journal
commit. Otherwise a `/compact` call against an already-invalid transcript
could summarize, retain, or persist a malformed projection. This is a
concrete compaction risk, not proof that compaction caused the reported
incident; that determination still requires the affected session's journal
and compaction records.

## 4. Problem statement

Session opening currently treats “provider history could not be repaired” as
“the session cannot be opened at all.” This couples three different concerns:

1. **Durable session availability** — loading session metadata, event history,
   transcript, artifacts, and workflow checkpoint.
2. **Provider-history readiness** — whether the message projection satisfies
   Lauren-ai's tool-exchange invariant and may be sent to a model.
3. **Workflow recovery readiness** — whether the typed checkpoint can safely
   resume at a specific phase with the matching conversation cursor.

These states must be reported separately. A session can be viewable while its
provider conversation is blocked; a workflow checkpoint can be present but
unsafe to resume against that conversation; or a safe validated checkpoint can
exist even when a newer transcript tail is malformed. The user must receive a
clear, non-destructive recovery path in each case.

## 5. Goals

1. Never let a failed history-repair attempt make durable session details and
   safe transcript inspection unavailable.
2. Validate every candidate repair before it is committed to the journal.
3. Preserve the original journal and all valid messages; never discard an
   invalid tail silently.
4. Return a typed, redacted recovery diagnosis with issue codes and recovery
   eligibility, not the generic `repaired tool history remains invalid` error.
5. Never send invalid provider history to Lauren-ai or a provider.
6. Allow explicit recovery from a validated durable boundary without silently
   starting a new workflow run, reverting to INIT, or replaying completed
   side-effecting tools.
7. Preserve `make_book`'s current chapter, outputs, phase receipts, and workflow
   identity when the chapter-completion gate was not recorded.
8. Ensure failed session attach/open attempts release or roll back any
   foreground handoff/ownership they acquired.
9. Change Lauren-ai only if its validator, repair API, or stable diagnostics
   contract is demonstrated to be insufficient or incorrect.
10. Prevent compaction from committing a stale snapshot over newer session
    messages or tool results.
11. Retry safe local tool-history repair automatically when an explicit
    workflow resume is requested and before a detached workflow is created or
    rehydrated.
12. If detached preflight repair cannot establish provider readiness, preserve
    the workflow checkpoint and session as recoverable/operator-action-needed;
    do not start a provider turn or classify this as a generic provider error.
13. Refuse compaction on an invalid source transcript and validate every
    replacement candidate before writing it to the journal.

## 6. Non-goals

- Weakening Lauren-ai's tool-call/result invariant or sending malformed
  messages to a provider.
- Fabricating successful tool output or silently reassigning tool-call
  IDs/results. A uniquely identified unanswered call may receive an explicit
  synthetic error result stating that execution outcome is unavailable, only
  to restore protocol validity.
- Deleting, truncating, or rewriting the only copy of a session journal to make
  startup succeed.
- Automatically restarting the workflow at INIT or creating a fresh workflow
  run when a recoverable checkpoint exists.
- Automatically re-running tools or phase work whose side effects may already
  have occurred.
- Treating existence of a chapter file alone as proof that the workflow's
  completion gate and checkpoint receipt were committed.
- Adding unlimited retries or masking unrelated corruption as a tool-history
  repair.

## 7. Proposed solution

### 7.1 Separate session loading from provider-history validation

Session rehydration should return a session object and a typed conversation
readiness state rather than throwing away the whole session when optional
repair fails. At minimum:

```text
conversation_status:
  ready
  repaired
  repair_required
  irrecoverable
  journal_corrupt

workflow_status:
  none
  resumable
  needs_reconciliation
  blocked
```

`ready`/`repaired` histories may enter normal provider execution after full
validation. `repair_required`, `irrecoverable`, and `journal_corrupt` histories
must remain blocked from model requests until a deterministic recovery
operation establishes a valid projection. Session details and read-only
transcript inspection remain available where the underlying event and journal
data can be read safely.

### 7.2 Make repair a pure candidate, then transactionally commit

Replace the current mutate/restore/validate path with a candidate-based repair
contract:

1. Fold the journal into an immutable original projection and capture its
   journal cursor/checksum.
2. Ask the canonical validator for structured findings.
3. Apply only explicitly supported deterministic repairs to a separate
   candidate projection.
4. Validate the candidate completely, including provider-specific history
   metadata required by the active contract.
5. If valid, persist it through the journal's atomic state-replacement
   transaction and verify the committed cursor.
6. If invalid or persistence fails, leave the journal and in-memory projection
   unchanged and return a typed recovery result with redacted diagnostics.

Repair must be idempotent. A second startup after successful repair must not
add another synthetic result or move messages again. If a crash occurs during
commit, journal folding must produce either the previous valid state or the
fully committed repaired state, never a partially published candidate.

### 7.3 Define repair eligibility from validator evidence

The implementation must enumerate Lauren-ai's issue codes and define a repair
matrix:

| Finding | Automatic action | Otherwise |
|---|---|---|
| Known non-adjacent result with a unique exact call-ID match | Move it to the owning adjacent exchange; validate candidate | Preserve original and mark recovery-required |
| `non_adjacent_results` where later results belong to other calls and no exact result exists for this call | Do not steal/reassign those results. Add an explicit synthetic interruption error for the uniquely identified unanswered call; never claim success or invent output. | Preserve and block if IDs or exchange ownership are ambiguous |
| Missing result for an otherwise valid, uniquely identified call in persisted assistant history | Add an explicit synthetic interruption error result; never claim the tool succeeded or invent its output. An unfinished-exchange receipt is corroborating evidence, but is not required because a crash may occur after the assistant message append and before the lifecycle start receipt. | Preserve and block if call or exchange ownership is ambiguous |
| Duplicate result or duplicate call ID | Never guess which result is authoritative | Preserve and block provider use |
| Orphan/unknown result ID | Never attach it to a guessed batch | Preserve and block provider use |
| Empty/malformed call ID or unparseable message | No guessed repair | Preserve and block provider use |
| Provider-required metadata conflict | Preserve original protocol data; do not synthesize hidden reasoning | Use only a supported protocol-specific recovery path |
| Corrupt trailing JSONL record | Follow existing journal fold policy and report the skipped trailing-record count | Preserve source journal; require operator action if integrity is uncertain |

The final matrix must match the installed Lauren-ai validator and actual
journal schema. It must not claim a repair capability that has not been
implemented and tested.

### 7.4 Typed diagnostics and privacy

Replace the generic `ValueError` with a stable recovery exception/result, for
example `ToolHistoryRecoveryRequired`, carrying only bounded metadata:

- stable error code and recovery status;
- validator issue codes;
- message indices/counts and expected/observed result counts;
- whether a deterministic candidate was attempted and validated;
- journal cursor/checksum or transaction ID for correlation;
- workflow run ID/current phase when already available; and
- safe next actions.

Diagnostics must never contain prompts, tool arguments, tool output, file
contents, API keys, raw provider payloads, or hidden reasoning. UI text should
say that the conversation cannot currently be sent to the model while making
clear that the session data has been preserved.

### 7.5 Safe session-open and ownership behavior

The `agents` details/open flow and `--resume`/`--continue` path must handle
conversation readiness as a recoverable state:

- show session details and available transcript even when provider history is
  blocked;
- offer inspect, retry-repair, recover-from-checkpoint, and return actions as
  supported by evidence;
- do not begin a provider turn or forward user input while history is invalid;
- do not rewrite, delete, or compact the journal as an automatic workaround;
- if attach/open fails after foreground ownership was acquired, release it or
  restore the previous background owner atomically; and
- allow retrying open after a recovery operation without changing the session
  identity.

Read-only access is not permission to resume. The UI and APIs must distinguish
the two.

### 7.6 Workflow checkpoint reconciliation

Before offering workflow resume, reconcile the durable workflow checkpoint
against:

- the workflow and run identity;
- typed phase/context and topology fingerprint;
- session conversation ID and journal cursor;
- phase receipts and artifacts already committed; and
- tool-history readiness at the checkpoint's required cursor.

If conversation history is invalid only after a known safe checkpoint, the
system may offer a conversation branch from that checkpoint only if the
checkpoint contract supports a new conversation cursor without invalidating
workflow identity. Otherwise preserve the checkpoint and explain that resume
is blocked until history is repaired. Never silently reuse a mismatched cursor
or create a new run.

For the reported `make_book` case, recovery must retain chapter 8 (one-based) /
`current_chapter_index == 7` (zero-based) unless a durable chapter receipt
proves completion. Before retrying chapter generation, inspect the canonical
chapter path and run the existing completion validation. If the artifact
passes, record the missing transition receipt idempotently; if it does not,
resume only that chapter. Previously completed chapters must remain complete
and must not be regenerated. If evidence for a safe receipt is unavailable,
ask for explicit user direction rather than inferring success.

### 7.7 Lauren-ai change policy

First verify whether the final rejection is caused by:

1. a valid history being rejected by Lauren-ai (shared validator defect);
2. an Agenthicc repair candidate that is invalid under the documented
   contract;
3. a lossy or ambiguous journal projection; or
4. an unsupported legacy/provider-specific message shape.

If (1) or (4) demonstrates a defect or missing shared abstraction, update
Lauren-ai to provide a canonical pure candidate-repair/validation API and
stable issue diagnostics, add its unit/integration coverage, then update
Agenthicc's declared minimum version and compatibility tests. If the Agenthicc
journal integration is at fault, fix it locally without forking Lauren-ai's
validator. In either case, retain one authoritative validator and one
provider-neutral tool-exchange contract.

### 7.8 Compaction and conversation ownership

Manual `/compact` must acquire the same session-conversation ownership lease
used by agent turns. If an agent turn already owns the conversation, report
that compaction did not start. Summary generation and exact token counting
must operate on a detached source snapshot; immediately before replacing
memory, compare both messages and summary with the live values. Apply this
compare-before-commit guard to summary compaction, local fallback, and the
context-overflow retry path. If they differ, return a distinct
`skipped_concurrent_update` outcome without changing memory or journal. Do not
misreport that case as successful compaction or a storage failure.

## 8. Functional requirements

### FR-1 — Openable durable session

A failed, orphaned, or recoverable session with an invalid provider-history
projection remains discoverable and inspectable. Conversation validation
failure must not erase the session record, transcript projection, workflow
checkpoint, or project artifacts.

### FR-2 — No invalid provider request

Every path that sends a provider request must require successful full
tool-history validation after repair. Invalid state fails locally with the
typed recovery result; no provider call is made.

### FR-3 — Transactional repair

Candidate repair is validated before durable commit. Failed validation or
storage leaves the original journal and message projection unchanged.
Successful repair commits atomically and is idempotent across restart.

### FR-10 — Repair on explicit workflow resume

When `/workflow resume` finds provider history blocked, retry the existing
deterministic local repair before rejecting the resume. If repair succeeds,
continue the selected workflow resume in the same command. If history remains
blocked, preserve the checkpoint, explain bounded issue codes, and make no
provider request.

### FR-11 — Detached workflow history preflight

Before creating or rehydrating a workflow handle in detached/headless
execution, retry local journal repair and require provider readiness. A
repairable history continues normally. An irreparable history returns a
distinct blocked/recovery-required outcome before workflow execution, without
claiming or mutating the workflow checkpoint. The background session must be
marked recoverable/operator-action-needed rather than failed as an ordinary
provider error.

### FR-12 — Compaction protocol gate

Compaction validates the source transcript before summarization or local
pruning, and validates the complete candidate immediately before replacement.
An invalid source or candidate leaves both in-memory projection and journal
unchanged and returns a bounded, content-free recovery reason. This applies to
manual compaction, proactive compaction, context-overflow compaction, and local
fallback.

### FR-4 — Explain the remaining invariant

When a repair candidate remains invalid, the user sees a stable reason and
safe recovery choices. Logs and machine-readable output include issue codes
and bounded counts, not content or secrets.

### FR-5 — Preserve workflow resume identity

When a valid checkpoint exists, recovery keeps the same session ID, workflow
run ID, workflow name, phase cursor, context, completed receipts, and artifacts.
Recovery never silently routes through the new-workflow/INIT path.

### FR-6 — Chapter completion recovery

For a `make_book` chapter missing its phase receipt, check whether the expected
file and its required referenced assets satisfy the existing chapter gate. If
so, record the transition once and advance from the checkpoint. If not, rerun
only the current chapter. Do not regenerate completed chapters or infer success
from file existence alone.

### FR-7 — Safe attach rollback

If session-conversation initialization or TUI startup fails after background
handoff begins, the prior owner/status is restored or the failed foreground
claim is released. Repeating attach must not require killing a process or
manually editing owner metadata.

### FR-8 — Lauren-ai compatibility

Agenthicc's supported Lauren-ai versions expose sufficient validation
diagnostics for this recovery contract. Any required upstream behavior is
implemented and version-pinned/tested; absent upstream changes, Agenthicc must
not bypass validation.

### FR-9 — Compaction cannot overwrite newer conversation history

Manual `/compact` participates in session ownership, and every candidate
prepared across an await commits only if its source messages and summary still
match. A mismatch preserves the newer live and durable projection and reports
`skipped_concurrent_update`.

## 9. Non-functional requirements

- **Durability:** the original journal is retained until a valid replacement is
  atomically committed; every recovery choice is auditable.
- **Safety:** malformed tool history is never sent to a model and never repaired
  by guessing ownership of results.
- **Resumability:** workflow checkpoint identity and artifacts survive repair
  failure and process restart.
- **Idempotence:** replaying recovery produces the same result and no duplicate
  synthetic messages or receipts.
- **Boundedness:** scans and diagnostics are bounded by configured journal and
  message limits; no unbounded retry loop is introduced.
- **Privacy:** no transcript content, tool arguments/results, secrets, or hidden
  provider reasoning are added to diagnostics.
- **Compatibility:** old valid journals remain readable; unsupported invalid
  shapes fail into a recoverable state rather than being silently discarded.
- **Usability:** distinguish `session viewable`, `provider history ready`, and
  `workflow resumable` in the TUI and machine-readable session projection.

## 10. Testing requirements

### 10.1 Unit tests

Cover:

1. Valid journal opens unchanged.
2. Known unique non-adjacent result repairs and passes validation.
3. Multiple tool calls and result blocks preserve exact pairing and order.
4. A `non_adjacent_results` finding caused by a later, unrelated valid result
   receives an interruption result for the unanswered call without moving or
   changing the unrelated result.
5. A missing, uniquely identified result receives one explicit synthetic
   interruption error result, including when a crash occurred before the
   lifecycle start receipt. The result never claims success or invents output.
6. Duplicate, orphan, empty-ID, malformed, and unknown-ID cases do not mutate
   the original projection or journal.
7. A repair candidate that still fails validation returns typed diagnostics,
   not a generic `ValueError`.
8. Candidate validation succeeds before a journal replace event is written.
9. Journal write failure leaves the original state recoverable.
10. Successful repair survives reopen and is idempotent on a second reopen.
11. Redacted diagnostics omit message content, tool arguments/results, API
    secrets, reasoning payloads, and raw IDs.
12. Supported Lauren-ai version behavior is detected explicitly and never
    bypasses the validator.
12. Workflow checkpoint/journal cursor mismatch is blocked with a safe,
    actionable result.
13. A valid `make_book` chapter artifact receives one idempotent receipt; an
    absent or invalid artifact resumes only that chapter.

### 10.2 Integration tests

Cover:

1. `SessionConversation.open()` with an invalid tail still yields inspectable
   session/recovery state without provider-ready memory.
2. `/resume`, `--continue`, and `agenthicc agents` open show typed recovery
   state instead of crashing during constructor startup.
3. No provider request is issued while conversation status is blocked.
4. Retry repair after restart either repairs atomically or preserves the exact
   original journal.
5. A recoverable workflow checkpoint remains associated with the same session
   and run after repair failure.
6. Failed attach releases/restores background ownership.
7. Recovery of the reported `make_book` shape preserves prior chapter receipts,
   inspects chapter 8 output, and never re-enters chapter 1.
8. If Lauren-ai requires a change, test both projects' contract and supported
   dependency version together.

### 10.3 End-to-end regression

Create a deterministic journal fixture representing a failed `make_book`
chapter and a non-adjacent tool-result tail whose initial repair candidate still
fails validation. Start from `agenthicc agents`, open the session, inspect the
diagnosis, recover or choose a safe blocked state, and resume only from the
validated workflow boundary. Assert:

- no generic constructor `ValueError` escapes;
- no model call receives invalid history;
- original journal/workflow state is preserved until an explicit valid commit;
- the user sees chapter 8 as the recovery point, not INIT;
- earlier chapter artifacts and receipts remain unchanged; and
- session ownership remains usable after recovery or returning to the list.

All tests use fake providers and temporary session directories; no live
provider credentials or network access are required.

## 11. Acceptance criteria

The implementation is accepted only when:

1. The exact invalid-history case is reproducible with a redacted fixture and
   the remaining validator issue is identified.
2. A session with invalid tool history remains viewable and its durable data is
   preserved.
3. No invalid history can reach a provider request.
4. Repair candidates are fully validated before atomic journal commit.
5. Failed repair reports structured, privacy-safe diagnostics and leaves the
   original journal unchanged.
6. The session can be reopened and recovery retried after process restart.
7. Foreground ownership is not stranded when open fails.
8. The `make_book` run preserves workflow identity and resumes at the chapter-8
   boundary after validating existing artifacts where applicable.
9. Earlier chapters and side-effecting tools are not rerun as a consequence of
   session repair.
10. Lauren-ai is changed only when shared-contract evidence requires it, and
    both repositories' tests cover the resulting contract.
11. Unit, integration, and E2E regression suites pass without live provider
    calls.
12. A deterministic concurrent-compaction test appends a tool exchange while
    summary generation is blocked; compaction skips its stale commit and
    preserves the exact live projection and folded journal, including observed
    `non_adjacent_results` and `missing_results` findings.
13. Manual `/compact` does not run concurrently with a session-owned agent
    turn.
14. `/workflow resume` retries deterministic repair before refusing; it
    resumes the existing checkpoint if repaired and otherwise makes no
    provider request.
15. Detached workflow preflight repairs valid interrupted exchanges and
    starts the workflow; unrepairable history returns
    `tool_history_recovery_required`, leaves any workflow checkpoint
    untouched, and is represented as recoverable rather than a generic failed
    worker.
16. Summary and local compaction reject invalid source history without
    calling the summarizer or changing the journal.
17. Every compaction replacement candidate passes Lauren-ai's canonical
    tool-history validator before durable commit.

## 12. Rollout and operational behavior

1. Add the redacted malformed-journal fixture and diagnostic type first.
2. Ship candidate-only repair and transactional commit at the existing
   session-open boundary; do not migrate journals automatically at install.
3. Expose inspection/recovery state for blocked histories.
4. Enable checkpoint-aware workflow recovery only after cursor validation.
5. Show a bounded notice for sessions requiring explicit repair; do not repeat
   it on every TUI refresh.
6. Document manual backup/recovery only as a last resort. Never instruct users
   to delete or hand-edit the only journal as the default fix.

## 13. Implementation decisions

- Recovery readiness lives on `JournaledShortTermMemory` and is surfaced by
  `SessionConversation`; the client-neutral `SessionSnapshot.readiness`
  projection carries only safe status values.
- The installed Lauren-ai validator already provides stable issue codes,
  expected/observed IDs, and repairability. No upstream change or weakened
  validation is needed.
- Checkpoint-backed conversation branching is not supported. A workflow remains
  intact but cannot resume until the current conversation passes validation.
- The TUI keeps session inspection available, displays a bounded readiness
  notice, and exposes `/history repair` as a local-only retry action. Returning
  to the session list remains available; no automatic provider request is made.
- `/workflow resume` and detached workflow entry points retry that same local
  repair automatically. Remaining failures block provider execution and
  preserve the workflow checkpoint for later recovery.
- Compaction validates source and replacement projections with Lauren-ai's
  canonical validator. This prevents compaction from persisting malformed
  provider history, but does not establish the cause of a historical incident
  without its session journal.

