---
title: "PRD-246: Actionable Recovery When Conversation Compaction Has No Summary"
status: Implemented
version: 1.0.0
date: 2026-10-17
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-135, prd-182, prd-190, prd-242, prd-243]
tags: [compaction, context-window, recovery, conversation-memory, observability]
---

# PRD-246 — Actionable Recovery When Conversation Compaction Has No Summary

## 1. Executive summary

The reported failure is produced by a two-stage compaction path, not by a
single exception:

```text
summary request returns no extractable final text (after its bounded retry)
  → Agenthicc reports “Compaction returned an empty summary”
  → local fallback tries to remove complete old exchanges
  → the newest retained exchange still cannot fit (or no safe exchange boundary exists)
  → Agenthicc raises CompactionRecoveryRequired
       status=blocked_irreducible_context, error_type=none
```

The current behavior preserves the original conversation rather than truncating
an arbitrary message or splitting a tool exchange. That is the correct safety
property, but the user-facing result is confusing and recovery diagnostics lose
the primary cause. `error_type=none` is expected in this path: an empty model
answer is a successful transport response with no final prose, and the final
irreducible-context outcome is constructed after local fallback without carrying
the earlier summary outcome forward.

The supplied output alone cannot establish why the model produced no final
summary text. Possibilities include a reasoning-only response, a provider or
adapter response shape Agenthicc does not extract, or a genuinely empty final
answer. Nor does the output prove which retained message made the fallback
irreducible. This PRD proposes a protocol-aware diagnosis, a durable
deterministic fallback for oversized tool/output content, and a precise
recoverable terminal state when safe compaction still cannot fit. It explicitly
does not call for unlimited retries or silent loss of user-authored content.

## 2. Incident

Observed output:

```text
⎋ Compacting conversation…
⎋ Compaction returned an empty summary
ERROR CompactionRecoveryRequired: Conversation compaction
requires recovery (status=blocked_irreducible_context,
error_type=none)
```

This usually becomes visible after a request has crossed the effective context
budget and Agenthicc forces compaction before retrying the model request. If the
summary and safe local reduction both fail to produce a fitting request,
Agenthicc correctly refuses to resend the same oversized history. The current
message, however, does not communicate that causal sequence or identify a safe
next action.

## 3. Evidence and current behavior

### 3.1 Summary generation is bounded

`memory.compactor._summarize_text()` calls the configured transport once per
chunk and retries once when the extracted final answer is empty. The retry uses
a larger completion budget and streaming. It deliberately excludes reasoning
blocks and tool-call deltas from the summary text. If both attempts produce no
extractable final text, the helper returns an empty string; it does not retry
indefinitely.

### 3.2 Empty summary is a non-exceptional outcome

`compact_memory_with_result()` returns `skipped_summary_unavailable` when it
had transcript text but no summary. In the case shown, the summary calls did
not produce an exception, so this outcome has no `error_type`. The TUI projects
the notice `Compaction returned an empty summary`.

The transcript does not include response-shape metadata, so it cannot tell
whether the model returned reasoning only, an unrecognized final-content shape,
or an empty answer. Hidden reasoning and raw provider payloads must not be
logged to diagnose this.

### 3.3 Local fallback preserves complete exchanges

`AgentTurnRunner._auto_compact_if_needed()` next invokes
`local_compaction_fallback()`. That fallback removes only complete oldest
conversational exchanges. It does not split tool calls from results or
arbitrarily clip user and assistant messages. It returns
`blocked_irreducible_context` when, among other conditions, the retained
exchange is still above its target, there is no eligible user-turn boundary,
or reducing it would violate the fallback's safety checks.

This conservative behavior prevents malformed tool history and silent loss of
the current request. It also means a single very large latest exchange—often
including command, browser, or MCP tool output—can remain too large even after
all older exchanges have been removed.

### 3.4 The final error loses the preceding summary outcome

When the local fallback is not applied and the input is forced or the exact
count is already over budget, `_auto_compact_if_needed()` constructs a new
`CompactionOutcome("blocked_irreducible_context", ..., error_type="")` and
raises `CompactionRecoveryRequired`. The summary outcome is not attached to it.
Consequently the exception reports `error_type=none`; it does not mean an
exception was swallowed or that the provider returned an upstream error with
an unknown type.

### 3.5 Existing durability/recovery protections remain required

PRD-242 established transactional memory replacement and recoverable turn or
workflow state. This PRD extends the failure diagnosis and safe fallback for
the empty-summary-plus-irreducible-context case; it must not weaken PRD-242's
journal, workflow checkpoint, cancellation, or accepted-input preservation
guarantees. PRD-243's provider-specific reasoning replay requirements also
remain authoritative when a history projection is changed.

## 4. Problem statement

The compaction pipeline has no single causal result representing all stages.
An empty summary is shown as a standalone notice, then a local fallback failure
overwrites the summary outcome with a generic irreducible-context result. The
user cannot distinguish:

- an empty or unextractable model summary;
- a transport/provider failure during summary generation;
- a safe local fallback that succeeded;
- a latest exchange that is too large to retain as a whole; or
- a durability failure while attempting to commit a fallback.

The current implementation intentionally refuses destructive truncation, but
has no second, protocol-aware local strategy for oversized generated tool
output. The resulting recoverable block is therefore sometimes unavoidable,
and when it is unavoidable its status lacks enough information to diagnose or
recover efficiently.

## 5. Goals

1. Preserve the causal chain from summary attempt through fallback and final
   recovery outcome.
2. Explain empty-summary cases using bounded response classifications without
   exposing prompts, hidden reasoning, tool contents, or raw provider payloads.
3. Safely reduce oversized generated tool/output content when removing whole
   older exchanges is insufficient.
4. Never silently truncate or discard the current user request, system/developer
   instructions, tool-call identity, required tool results, or provider-required
   reasoning metadata.
5. Keep irreducible cases recoverable, accurately categorized, and actionable;
   do not resend unchanged oversized context or restart a workflow from INIT.
6. Keep all provider calls, local work, and retries explicitly bounded.

## 6. Non-goals

- Increasing retries without a bound or retrying the same oversized request.
- Treating reasoning-only content as a user-visible summary.
- Fabricating, paraphrasing, or logging hidden provider reasoning.
- Silently clipping the current user input or system/developer prompts.
- Breaking tool-call/result protocol pairing to meet a token budget.
- Rewriting or deleting the durable conversation journal on failure.
- Replacing PRD-242's transactional compaction contract or PRD-243's
  provider-specific protocol rules.
- Guaranteeing that an input larger than the model's usable context can be
  processed; such input must remain a clear, recoverable limitation.

## 7. Functional requirements

### 7.1 Preserve a typed, cumulative compaction result

The compaction orchestration must retain the outcome of each stage in one
bounded result. At minimum, record:

```text
summary_result: summarized | empty_final | reasoning_only | extraction_empty |
                transport_error | timed_out | cancelled | not_attempted
summary_attempts: bounded integer
summary_error_type: optional bounded exception class
fallback_result: not_needed | compacted_whole_exchanges |
                 compacted_tool_output | irreducible_latest_exchange |
                 no_safe_boundary | durability_error
final_status: compacted | skipped | recovery_required
recovery_reason: optional stable enum
before/after token estimate and durable-commit flag
```

Exact type names are implementation decisions. The result must not contain
conversation text, raw provider response bodies, secrets, or hidden reasoning.
An empty summary with no exception must not be represented as an unexplained
`error_type=none`; it should carry a stable cause such as `empty_final`.

### 7.2 Diagnose final-answer extraction without leaking content

The transport adapter must distinguish, where supported by the public response
contract, among:

- no final content;
- reasoning-only content;
- tool-call-only content;
- a final-content shape not recognized by the extractor; and
- a transport exception or timeout.

This classification is metadata only. The implementation must not expose or
persist reasoning text or raw payloads. Provider-specific parsing stays behind
the shared transport compatibility boundary and receives contract tests.

### 7.3 Keep summary retries bounded and avoid retry multiplication

- Retain an explicit maximum summary-attempt count (including the current
  initial call and empty-response retry) unless tests demonstrate a narrowly
  scoped compatibility correction is needed.
- Do not invoke a fresh, hidden retry loop from local fallback.
- Do not multiply summary retries by the agent-turn or provider recovery retry
  budgets.
- Cancellation must propagate and must not be classified as empty output.
- Transport errors/timeouts must remain distinguishable from a successful but
  empty completion.

### 7.4 Add protocol-aware local reduction for generated outputs

When whole-exchange pruning cannot fit the context, the system should prepare a
second deterministic candidate that can compact oversized *generated* content
inside retained exchanges, particularly tool results. The candidate must:

- preserve the latest user-authored request exactly;
- preserve system/developer instructions and their ordering;
- preserve tool call IDs, tool names, required result IDs, and valid
  assistant/tool-result ordering;
- preserve provider-required metadata, including reasoning replay fields when
  the active provider/session contract requires them;
- preferentially retain the most recent and structurally useful portions of
  tool output (for example bounded head/tail excerpts) with an explicit
  omission marker and original byte/character count;
- never claim that an excerpt is the complete tool result; and
- commit the candidate transactionally through the existing journal-backed
  replacement contract.

The implementation must define which generated message/block classes are
eligible for reduction. User messages, system/developer prompts, tool-call
arguments, and opaque provider protocol blocks are not eligible unless an
explicit, separately reviewed policy proves the transform safe. If no eligible
content can be reduced safely, return `irreducible_latest_exchange` rather than
mutating history.

### 7.5 Maintain a hard context budget

The reduced candidate must be measured against the same effective usable input
budget as the pre-send guard, including system prompt, tool schemas, request
options, and output reserve where those contribute to the provider request.
An estimate-only candidate is insufficient when an exact count is available.
If the candidate still exceeds the hard budget, do not submit it.

### 7.6 Make recovery required state actionable

If no safe candidate fits:

- preserve the original journal and current accepted input/turn marker;
- preserve the workflow run ID, active phase/checkpoint, completed tool effects,
  and retry/idempotency ledger;
- do not retry the same oversized request or restart the workflow;
- persist a stable failure category and `recovery_reason`, such as
  `latest_exchange_too_large`, `current_input_exceeds_budget`,
  `no_protocol_safe_boundary`, or `compaction_commit_failed`;
- surface one concise message that explains both the unavailable summary and
  why the fallback could not proceed, when both apply; and
- provide a recovery path that lets the user reduce/replace the oversized
  input, select a larger-context model, or explicitly start a separate
  conversation without destroying the original session.

The system must not suggest that waiting or retrying unchanged is likely to
resolve a deterministic context-size block.

### 7.7 Preserve result visibility across runtime surfaces

The same structured result must be represented consistently in foreground
TUI, headless execution, background-session records, `/agents` details, logs,
and recovery projections. Output should be concise; detailed bounded stage
metadata may be available through session diagnostics. Keep current PRD-242
status event compatibility where feasible.

## 8. Proposed solution and data flow

```text
Pre-send/context rejection
  → bounded summary attempt(s)
      ├─ usable summary → transactional summary replacement → re-count
      └─ no usable summary → record typed summary cause
          → remove complete oldest exchanges
              ├─ fits → transactional commit → continue same turn
              └─ does not fit → protocol-aware generated-output reduction
                  ├─ fits → transactional commit → continue same turn
                  └─ cannot safely fit → durable recovery_required outcome
                      (cause + fallback reason; preserve original context)
```

The fallback must prepare an immutable candidate, validate its protocol
structure and budget, commit it durably, then publish it to live memory, as
required by PRD-242. Any failure before commit leaves the original projection
unchanged. Any uncertain commit must resolve through journal replay rather than
blindly retrying a non-idempotent replacement.

## 9. Acceptance criteria

| ID | Criterion |
|---|---|
| 246.1 | A transport response with no extractable final text is classified as an empty-summary result, not an uncaught provider exception. |
| 246.2 | The bounded initial-plus-retry summary policy is observable, and no additional retry layer is introduced. |
| 246.3 | A final recovery outcome retains both the summary-stage cause and the local-fallback result; it does not erase them into `error_type=none`. |
| 246.4 | Reasoning-only, tool-call-only, empty-final, malformed/unrecognized content, timeout, and transport-error test responses are distinguished where the transport contract permits, with no hidden content in emitted diagnostics. |
| 246.5 | If pruning complete old exchanges fits the budget, the candidate is durably committed and the same logical turn continues exactly once. |
| 246.6 | If a retained oversized tool result is the remaining blocker, a protocol-aware bounded excerpt can be committed while preserving tool-use/result identity and explicit omission markers. |
| 246.7 | Candidate reduction never changes the current user request or required system/developer instructions, and never drops or mismatches a required tool result or provider-required history field. |
| 246.8 | A generated-output reduction that still exceeds the effective budget is not sent to the provider. |
| 246.9 | An irreducible current user input, unsafe protocol boundary, or commit error produces a distinct recoverable reason while preserving original memory and journal state. |
| 246.10 | A detached direct turn or workflow remains resumable with the same accepted input, turn ID, workflow run/phase/checkpoint, and completed side-effect ledger. |
| 246.11 | Retrying/resuming after recovery does not duplicate user input or completed tool effects and does not restart a workflow at INIT. |
| 246.12 | Cancellation at any stage remains cancellation and produces no partial projection or misleading empty-summary diagnosis. |
| 246.13 | TUI/headless/background/session-detail surfaces show a concise, accurate explanation derived from the same typed result. |
| 246.14 | Unit, integration, and end-to-end tests reproduce the reported empty-summary → local-fallback-blocked path without live provider access. |

## 10. Verification plan

### Unit tests

- Parameterize summary response extraction: plain text, structured text,
  streaming final deltas, reasoning-only, tool-call-only, empty, malformed, and
  provider-specific response shapes.
- Verify bounded attempt counts and ensure cancellation is not converted into
  empty output.
- Construct histories where whole-exchange pruning succeeds, where a single
  tool result is oversized, where the current user input itself is oversized,
  and where no protocol-safe boundary exists.
- Verify tool IDs/order, reasoning metadata, user/system prompt identity,
  excerpt omission markers, token budget, and redaction.
- Inject journal commit failure and verify the old projection remains intact.
- Verify final result retains both summary and fallback causes.

### Integration tests

- Use real `JournaledShortTermMemory` and `ConversationJournal` to commit a
  reduced tool-result candidate, reopen the journal, and compare the folded
  protocol-valid state.
- Run the agent-turn context-overflow path with deterministic transports that
  return empty/reasoning-only summaries and verify no unchanged oversized
  request is sent again.
- Exercise direct-turn and workflow recovery, asserting accepted-input
  idempotency, workflow checkpoint preservation, and session failure category.
- Verify provider-profile-specific reasoning requirements survive a candidate
  replacement.

### End-to-end tests

- Reproduce the reported sequence: an oversized request triggers compaction,
  both summary attempts yield no final text, and the latest complete exchange
  is initially too large. Assert either safe generated-output reduction lets
  the same turn continue or a clear recoverable state is shown with the
  original conversation preserved.
- Repeat with a user message that alone exceeds the model budget. Assert no
  silent truncation, no repeated identical provider request, and an actionable
  recovery message.
- Restart a detached session after the blocked state and verify `/continue` or
  explicit resume rehydrates the exact original input/workflow checkpoint.

All tests must use deterministic fake transports and local temporary journals;
CI must not require model credentials or live provider calls.

## 11. Compatibility and rollout

- Preserve PRD-242 journal schema and transactional replacement guarantees.
- Keep the existing bounded summary retry unless transport-contract tests
  identify a specific extractor defect.
- Introduce structured outcome fields additively. Existing status readers must
  tolerate absent fields from older journal/session records.
- Gate generated-output reduction to explicitly supported message/block types;
  unknown provider-specific blocks must be preserved or cause a safe blocked
  outcome, never be silently stringified and truncated.
- Roll out first with outcome classification and fault-injection coverage, then
  enable the protocol-aware local reduction for eligible generated outputs.
- Do not rewrite old session journals or discard old transcripts as part of
  migration.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Excerpting tool output removes detail the agent needs | Make omissions explicit, preserve head/tail and structured identifiers, retain the original durable history where journal policy allows, and classify this as lossy fallback. |
| A provider needs exact old reasoning metadata | Follow PRD-243's provider/session protocol contract; do not reduce or remove required opaque fields. |
| Response extraction changes misclassify a provider completion | Keep extraction in the transport adapter, use typed shapes and compatibility tests, and retain a safe blocked path. |
| More fallback logic creates nested retries | Keep summary attempt count fixed and make all fallback stages local and deterministic. |
| Diagnostics leak sensitive content | Emit only enums, bounded counts, stable IDs where already permitted, and exception class/status; never include message or raw response text. |
| Local reduction still cannot fit truly oversized input | Preserve the original session and explain the hard limitation with a specific recovery reason. |

## 13. Definition of done

This PRD is implemented when the reported sequence is fully represented by a
single durable, typed outcome; eligible oversized generated output can be
reduced safely and transactionally; and any truly irreducible request remains
recoverable with its original conversation and workflow state intact. Tests
must prove that no unchanged over-budget request is reissued, no user input or
completed side effect is lost or duplicated, and no diagnostics expose hidden
reasoning or conversation content.

## 14. Implementation record

Implemented in `src/agenthicc/memory/compactor.py` and
`src/agenthicc/runners/agent_turn.py`. Summary completion outcomes are
classified without recording response content, and the final status combines
summary and fallback causes. Local recovery prunes complete exchanges first,
then can excerpt eligible plain-text tool-result blocks while preserving tool
identity, message order, and opaque assistant metadata. Candidate counting
includes the current input, system instructions, and tool schemas when the
transport supports exact counting; a bounded tighter candidate is recounted
once, and an over-budget candidate is never committed or sent. Journal
replacement remains the publish boundary, so failures preserve the existing
conversation projection and existing direct-turn/workflow recovery markers.

Coverage was added or updated in:

- `tests/unit/test_compactor.py`
- `tests/unit/test_agent_turn_compaction.py`
- `tests/integration/test_compaction_recovery.py`
- `tests/e2e/test_compaction_recovery_e2e.py`

Operational behavior is documented in `docs/guides/memory.md` and
`docs/reference/storage.md`.
