---
title: "PRD-242: Fail-Safe Conversation Compaction for Detached Sessions"
status: Implemented
version: 1.0.0
date: 2026-10-17
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-126, prd-129, prd-133, prd-135, prd-182, prd-184, prd-204, prd-210, prd-212]
tags: [background-sessions, detached-runs, compaction, conversation-memory, recovery]
---

# PRD-242 — Fail-Safe Conversation Compaction for Detached Sessions

## 1. Executive summary

Yes: the current architecture contains a source-confirmed path where an
automatic compaction error can escape an agent turn and cause a detached worker
attempt to be finalized as failed. This does **not** mean every compaction error
has this result. In the locked lauren-ai 1.6.0 implementation, a failure of the
summary completion itself is caught and normally treated as “no summary”; the
unhandled cases are failures in the compaction control/persistence path, such as
the exact token-count call or persisting the compacted memory to the conversation
journal. Native OpenAI token counting is local, and the Anthropic adapter falls
back to a heuristic when its token-count endpoint fails, so those common paths
reduce—but do not eliminate—the risk.

The incident material supplied for this investigation does not include a
detached run ID, worker log, or compaction error event. Therefore this document
confirms that the failure mode is possible in the current code, but does not
claim that a particular observed session failed for this reason.

The fix is to make compaction a bounded, observable, recoverable sub-operation:
token-count and summarization failures must not become unclassified failures of
the entire worker; compaction must apply transactionally to live memory and its
journal; and if safe compaction is impossible, the worker must preserve a
recoverable turn/workflow checkpoint and report a specific attention state
instead of losing context or silently restarting work.

## 2. Problem

Detached sessions run the same `AgentTurnRunner` as foreground sessions, but
the enclosing background worker owns the durable session status. An exception
that escapes the turn is caught by `run_worker()` and persisted as
`SessionStatus.FAILED` with `exit_reason="irrecoverable_error"`. When that
exception originated inside compaction, the session can look like the task
itself failed even though no agent response was attempted for that turn.

Compaction is especially sensitive because it changes the representation of
conversation memory. If compaction mutates the in-memory buffer and then fails
to persist the replacement to the journal, the running process and a later
resumed process can observe different conversation histories. A failure must
not cause either a loss of prior context or a misleading successful-compaction
signal.

## 3. Investigation findings

### 3.1 Detached sessions do use automatic compaction

The detached worker builds the regular session context and calls
`background.worker._run_direct_turn()`, which delegates to
`runners.agent_turn._run_agent_turn()`. With lauren-ai 1.6.0 and the default
`auto_compact = true`, Agenthicc supplies `summarize_at=0.8` to lauren-ai for
turns without a `PromptContract`. Thus a sufficiently large conversation can
enter lauren-ai's automatic compaction path during a detached run. Workflow
turns with a prompt contract use Agenthicc's own bounded `compact_memory()`
compatibility path and disable lauren-ai's proactive summarizer; both paths
must be covered by the fix.

### 3.2 Summary-provider errors are not the direct unhandled path

In lauren-ai 1.6.0, `_one_summary_call()` catches ordinary exceptions from
`transport.complete()` and returns an empty summary. `_summarize_memory()` then
returns `False` without replacing the conversation. The runner proceeds to its
context-fit guard. A failed summary request can still lead to a later context
overflow, but the summary-call exception itself is not normally propagated as a
worker failure.

### 3.3 Compaction control and persistence errors can escape

The proactive compaction path calls `transport.count_tokens()` from
`_maybe_compact()` before summarization. That call is not protected by the
summary-call fallback. A thrown error therefore escapes the runner. A fault-
injection reproduction against the installed lauren-ai 1.6.0 `_maybe_compact()`
confirmed that a token-counter `TimeoutError` propagates to its caller.

On a successful summary, `_summarize_memory()` trims live memory and sets its
summary before calling the optional `journal_reset()` hook. If that durable
journal write fails, the exception can escape after the in-memory state has
already changed. This is a memory/journal consistency hazard as well as a
possible failed turn.

### 3.4 Error handling turns an escaped compaction error into a failed worker

`AgentTurnRunner._stream_with_retry()` retries recognized transient provider
errors within the configured retry budgets. Unknown/local exceptions are
propagated immediately; exhausted retryable errors also propagate. The
`run_worker()` exception boundary then creates a failed `_WorkerOutcome`, and
the finalizer persists the worker attempt as failed. This general worker
boundary cannot distinguish “agent execution failed” from “optional compaction
could not run.”

### 3.5 This is a cross-surface flaw, not a detached-only algorithm

Foreground and detached sessions share the agent-turn runner. Detached runs
make the impact more visible because the background worker persists the
attempt's terminal status without an interactive operator present. The fix
should therefore make the common compaction contract safe, then explicitly
verify that the detached lifecycle records and exposes its result correctly.

### 3.6 Separate manual compaction behavior

Agenthicc's `memory.compactor.compact_memory()` catches ordinary compaction
exceptions and returns the current estimate, allowing its caller to try the
local fallback. However, the successful-summary path mutates memory before
`journal_reset()`. If persistence fails, the exception is caught after mutation;
the UI can report failure while the in-memory state has already changed. This
path needs the same transactional memory/journal treatment. Cancellation must
remain distinct and must continue to propagate rather than being converted to
a normal compaction fallback.

## 4. Current data flow

```text
Detached worker
  └─ run_worker()
      └─ _execute()
          └─ _run_direct_turn()
              └─ _run_agent_turn()
                  └─ _stream_with_retry()
                      └─ lauren-ai run_stream()
                          └─ _maybe_compact()
                              ├─ transport.count_tokens()  ← may escape
                              └─ _summarize_memory()
                                  ├─ summary completion failure → empty summary
                                  └─ mutate memory → journal_reset() ← may escape
          exception escapes the turn
      └─ run_worker exception boundary → FAILED outcome
          └─ finalizer persists FAILED session status
```

The desired flow is:

```text
Detached or foreground turn
  └─ compaction decision
      ├─ exact count succeeds → summarize → prepare candidate state
      ├─ exact count unavailable → conservative local estimate / bounded fallback
      ├─ summary unavailable → keep original state, then use safe context guard
      ├─ durable commit succeeds → atomically publish compacted memory
      └─ durable commit fails → retain old memory and checkpoint; surface a
                                typed recoverable compaction/storage condition
  └─ continue the same logical turn when safe, otherwise pause for recovery
```

## 5. Goals

- **G1 — Compaction failures are contained.** A failed optional summary/count
  operation must not accidentally terminate the full agent session.
- **G2 — Memory and journal remain consistent.** Compaction is committed as one
  logical operation; a failed durable write cannot leave a partially published
  in-memory replacement.
- **G3 — Detached runs remain recoverable.** When continuation is unsafe, keep
  the original messages, turn ID, workflow phase/checkpoint, and durable tool
  ledger available for explicit resume.
- **G4 — Failures are visible and diagnosable.** Record whether compaction was
  skipped, summarized, locally reduced, or blocked by persistence, without
  logging prompt contents or secrets.
- **G5 — Preserve existing retry and cancellation contracts.** Do not retry
  completed tool side effects, reinterpret input, or convert cancellation into
  successful compaction.

## 6. Non-goals

- Replacing the context-window policy or changing the configured model window.
- Removing the existing lossy context-fit guard; it remains a last-resort
  bounded safeguard.
- Treating every model/provider error as recoverable indefinitely.
- Replaying an entire goal or workflow from INIT after a compaction failure.
- Hiding a true irreducible context overflow. If no safe representation fits,
  report a recoverable, actionable context-limit outcome.
- Changing manual `/compact` into an automatic background job.

## 7. Product requirements

### 7.1 Typed compaction outcome

Compaction must expose an explicit outcome rather than relying on an exception
or an ambiguous token estimate. The outcome should distinguish at least:

```text
COMPACTED_SUMMARY
COMPACTED_LOCAL_FALLBACK
SKIPPED_BELOW_THRESHOLD
SKIPPED_SUMMARY_UNAVAILABLE
BLOCKED_DURABILITY_ERROR
BLOCKED_IRREDUCIBLE_CONTEXT
```

The exact Python type and compatibility adapter are implementation decisions.
The outcome must carry bounded metadata (stage, exception category, before/after
estimate, and whether durable state committed), never message bodies.

### 7.2 Fail-safe exact token counting

- A token-count failure used only to decide whether to compact must not escape
  as an ordinary agent-turn exception.
- Use a conservative local estimate to decide whether compaction is advisable
  when exact counting is unavailable.
- Preserve the existing pre-send guard as the independent final safety check.
- If the guard cannot establish a safe request, return a typed, actionable
  context-limit outcome; do not silently submit an oversized request.
- Bound any retry of the compaction decision separately from whole-turn
  retries. Do not multiply turn retries by a second unbounded compaction retry
  loop.

### 7.3 Summary failure handling

- A failed or empty summary must leave the original conversation and journal
  unchanged.
- If a deterministic local fallback is needed to fit the window, retain whole
  recent exchanges and never split a `tool_use` from its results.
- Make the lossy nature of the local fallback explicit in its status event.
- Do not claim the conversation was semantically summarized when it was only
  trimmed or excerpted.

### 7.4 Transactional journal updates

- Prepare compacted messages/summary separately from the live memory state.
- Persist the candidate state before publishing it as the live projection, or
  provide an equivalent rollback-safe transaction at the memory/journal
  boundary.
- On persistence failure, retain the pre-compaction in-memory projection and
  journal contents; do not emit a success event.
- Apply the same contract to lauren-ai automatic compaction and Agenthicc's
  `compact_memory()` path. Use a stable extension/protocol hook rather than
  monkey-patching third-party internals.
- Ensure journal replay after process restart yields the same state as the
  live session observed before shutdown.

### 7.5 Detached worker lifecycle

- A compaction failure that has a safe fallback must continue the same logical
  turn, retaining its accepted user message and committed tool/provider prefix.
- If no safe continuation exists because the durable journal cannot commit or
  the remaining request is irreducibly oversized, preserve the turn and
  workflow checkpoint and expose a resumable attention state with a specific
  failure category (for example `compaction_persistence_failed` or
  `context_limit_unresolved`).
- Do not report a generic `irrecoverable_error` solely because compaction
  failed.
- Do not restart the workflow at its first phase. Resume from its durable
  current phase/checkpoint under existing workflow-recovery rules.
- Preserve ordinary cancellation semantics: cancellation during counting or
  summarization propagates as cancellation and does not commit partial
  compaction state.

### 7.6 Observability and privacy

Emit compact status events for compaction start, success, fallback, skip, and
blocked state. Detached session projections should identify the stage and
outcome so operators can tell a context/compaction problem from a model response
failure. Persist only bounded exception type/provider status and token estimates;
never persist prompts, transcript text, credentials, headers, or raw provider
payloads in these events.

## 8. Proposed implementation

1. **Define a compaction result contract** shared by the automatic and manual
   paths. Preserve compatibility with lauren-ai versions in the supported
   range and avoid depending on private runner behavior where a public hook can
   be added.
2. **Harden the automatic compaction boundary in lauren-ai.** Catch errors in
   the token-count decision and choose a conservative fallback; keep summary
   call failures non-fatal; return explicit outcome/notice data to Agenthicc.
   Because Agenthicc currently allows `lauren-ai >=1.6.0,<2`, pin the minimum
   behavior with contract tests and coordinate any upstream API change before
   relying on it.
3. **Add a transactional memory-compaction operation** to the memory contract.
   Implement the journal-backed version so candidate summary/recent messages
   are durably reset before becoming the live memory projection. Ensure failed
   persistence leaves both old projections intact.
4. **Use one compatibility adapter in Agenthicc.** Route the prompt-contract
   local compactor and manual `/compact` through the transactional operation;
   translate old lauren-ai outcomes safely while supporting the locked 1.6.0
   runtime.
5. **Map compaction outcomes at the background boundary.** Safe fallback
   proceeds in the same turn. Unsafe durability/context outcomes preserve
   checkpoints and appear as recoverable states with actionable details.
6. **Record evidence and metrics.** Add structured event fields for outcome,
   stage, fallback kind, and before/after estimate. Keep exception text
   redacted/bounded and add counters for summary failure, fallback, journal
   failure, and irreducible overflow.

## 9. Acceptance criteria

| ID | Criterion |
|---|---|
| 242.1 | A fault injected into `transport.count_tokens()` during proactive compaction does not escape as a generic detached-worker failure. |
| 242.2 | A failure or empty response from the summary completion leaves original memory/journal intact unless an explicitly reported local fallback is durably committed. |
| 242.3 | A journal-reset failure cannot leave live memory compacted while the durable journal still contains the old projection (or vice versa). |
| 242.4 | A direct detached session with compaction enabled either completes the same logical turn using a safe fallback or becomes explicitly recoverable; it is not marked irrecoverable solely due to optional compaction. |
| 242.5 | A detached workflow encountering the same fault retains the exact workflow run, phase, checkpoint, and committed tool prefix; it never silently returns to INIT. |
| 242.6 | Restarting after a committed compaction rehydrates the same compacted summary and retained recent exchanges as the pre-restart live session. |
| 242.7 | Restarting after a failed compaction rehydrates the original unmodified conversation and can retry the compaction/turn safely. |
| 242.8 | Repeated provider failure is bounded; no nested retry policy can exceed documented retry/time budgets or duplicate user messages/tools. |
| 242.9 | Cancellation during any compaction stage remains cancellation, leaves no partially committed state, and is not mislabeled as a compaction success/failure. |
| 242.10 | Events distinguish LLM summary, local reduction, skipped compaction, persistence failure, and irreducible overflow without including transcript or secret contents. |
| 242.11 | Manual `/compact`, ordinary foreground turns, direct detached turns, and workflow/prompt-contract turns all use the same durable outcome semantics. |
| 242.12 | The supported lauren-ai compatibility range has a deterministic contract test that proves compaction errors cannot bypass Agenthicc's typed outcome/recovery boundary. |

## 10. Verification plan

### Unit tests

- Inject exceptions from exact count, summary completion, empty summary,
  candidate construction, `journal_reset`, and local trimming.
- Verify no mutation on failed candidate persistence; verify committed compacted
  state only after journal success.
- Exercise tool-call boundary snapping and preserve valid tool/result exchanges.
- Test transient/permanent/local failures, retry bounds, and exception
  redaction.
- Test that `CancelledError` propagates unchanged and does not become a
  fallback result.

### Integration tests

- Build a real `JournaledShortTermMemory` and temporary `ConversationJournal`;
  compact, close, reopen, and compare the folded state.
- Run `_run_agent_turn()` with the supported lauren-ai runner and a deterministic
  faulting transport for each compaction stage.
- Run `background.worker.run_worker()` with a detached direct session and a
  detached workflow session. Assert durable status, failure category, accepted
  input receipt, workflow run/phase, and journal projection.
- Force summary failure followed by an irreducible context result; verify an
  actionable recoverable context outcome rather than workflow restart.

### End-to-end tests

- Launch a detached run whose context crosses the compaction threshold, inject
  one compaction error, and verify it completes or remains recoverable without
  user/session history loss.
- Restart the worker process after successful and failed compaction and verify
  session manager details and `--resume` use the same durable state.
- Exercise `/compact` on a detached session and confirm a failed durable commit
  is reported accurately and does not falsely claim success.

All injected network/provider interactions must be deterministic and offline;
no live model credentials are required for CI.

## 11. Rollout and compatibility

- Keep `auto_compact` enabled by default and preserve existing context budgets.
- Maintain compatibility with lauren-ai 1.6.x and later supported 1.x releases;
  use feature detection only at the adapter boundary and test every supported
  behavior rather than scattering private-field checks.
- If a transactional compaction hook requires a lauren-ai API change, ship the
  upstream dependency release and Agenthicc adapter together, or retain a
  journal-aware fallback until the minimum supported version includes it.
- Do not rewrite existing journals. New compaction journal records must remain
  foldable by the current versioned journal reader, or be gated behind a schema
  migration with explicit backward-compatibility tests.
- Roll out first with structured outcome telemetry and fault-injection tests,
  then enable the durable path for all runs. Keep a configuration escape hatch
  only if needed for emergency rollback; do not change the secure/default
  context policy.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Best-effort local fallback loses older detail | Label it explicitly, preserve recent complete exchanges, and retain the full journal until the compacted journal commit succeeds. |
| Journal transaction changes memory API semantics | Add a narrow compaction method and compatibility tests; do not make generic `trim_to_fit()` unexpectedly durable. |
| Retry layers multiply provider calls | Keep compaction-specific retry bounded and separate from turn retry; track attempts in tests. |
| Upstream lauren-ai changes private compaction internals | Put version compatibility in one adapter and add contract tests against the supported dependency range. |
| Cancellation is swallowed by a broad fallback | Catch `Exception`, not `BaseException`; explicitly test `asyncio.CancelledError`. |
| UI hides a recoverable compaction failure as generic `Failed` | Persist a distinct failure category and project it into session detail/activity. |

## 13. Definition of done

The PRD is implemented when all acceptance criteria pass, detached and
foreground runs share the transactional compaction contract, journal replay is
consistent after either success or failure, and no compaction-stage exception
can silently turn a resumable detached run into an unclassified terminal
failure. Existing turn retry, workflow checkpoint, cancellation, and tool
idempotency tests remain green.

## 14. Implementation record

Implemented in Agenthicc and the adjacent lauren-ai source checkout:

- Agenthicc now owns proactive compaction for direct and workflow turns and
  disables lauren-ai's second automatic summarization path for those sessions.
- Token counting near the 80% decision threshold is bounded to eight seconds;
  errors and invalid counts fall back to a stricter local estimate. Compaction
  summary retries remain bounded to the existing one empty-response retry.
- Summary and local fallback outcomes are typed and content-free. Local
  fallback removes complete oldest user-turn groups only; an oversized single
  recent exchange remains intact and produces `context_limit_unresolved`.
- Compaction emits a structured start status and distinct result/fallback
  statuses with bounded token estimates and exception classes only.
- `ConversationJournal.replace_state()` writes prepare/candidate/commit
  records. Folding rolls back an incomplete replacement, and failed appends
  truncate to their starting offset where possible. The live
  `JournaledShortTermMemory` projection is published only after the durable
  journal transaction succeeds.
- Detached direct turns with an unsafe compaction result remain discoverable
  through the existing incomplete-turn journal state. Workflow failures keep
  their workflow checkpoint and are projected as `ORPHANED` only for the two
  compaction recovery categories; unrelated paused-workflow behavior is
  unchanged.
- Added Agenthicc unit coverage for faulted counting, exact-count thresholding,
  cancellation, whole-exchange fallback, journal transaction/replay, failed
  durable commits, direct-turn recovery, and detached workflow classification.
  Added integration coverage that restarts a journal after a failed proactive
  direct-turn compaction, plus an end-to-end detached-worker run that verifies
  the session becomes recoverable and rehydrates the original history and
  accepted input. Added lauren-ai contract tests for safe count fallback,
  empty-summary preservation, cancellation, and transactional memory
  replacement.
