# PRD-169 — Transaction-Safe Tool-Call Conversations Across agenthicc and lauren-ai

**Status:** Implemented
**Date:** 2026-08-04
**Scope:** `lauren-ai` conversation memory, tool execution, provider
serialization, and streaming runners; agenthicc agent turns, journaled session
memory, retries, resume, workflows, and TUI diagnostics.

## Summary

Make tool-call conversations transaction-safe at the shared `lauren-ai`
boundary and integrate that contract into agenthicc. Every assistant tool-call
batch must have exactly one provider-valid result for every call before the
conversation is sent back to a provider. A cancelled, filtered, failed, or
partially executed batch must be completed with explicit synthetic error
results, durably repaired, or rejected locally with a typed diagnostic. It must
never be serialized into a provider request that can produce an avoidable
`tool_calls`/`tool` mismatch.

The observed failure is:

```text
An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'. (insufficient tool messages following
tool_calls message)
```

The fix belongs in both projects. `lauren-ai` owns the provider-neutral
conversation and tool-execution invariant. agenthicc owns session durability,
workflow interruption/resume, queued input, and the user-facing recovery
experience. Neither project should solve the problem by merely hiding the
provider error or by adding another provider-specific retry.

## Evidence and reproduction

The supplied reproduction contains a pasted request with two image mentions:

```text
Use @beyond-35-build-strength-that-lasts/assets/generated-cover.png
as the cover page ... and
@beyond-35-build-strength-that-lasts/assets/generated-back-cover.png
as the back cover ...
```

The TUI resolves the two mentions and the agent emits two parallel `Read`
operations. The following provider request then fails with the error above.
The visible `Read` operations prove that tool-call generation reached the UI;
they do not prove that both calls were durably paired with valid result IDs in
the conversation sent to the provider.

The exact production-side malformed pair is not currently observable from the
reported transcript because tool IDs and the serialized provider payload are
not included in the user-facing error. The implementation must therefore add
safe, ID-only diagnostics and tests that capture the invariant failure without
logging prompts, file contents, arguments, or credentials.

## Problem diagnosis

### Expected data flow

```text
Pasted input
  -> TUI input buffer and @mention resolver
  -> agenthicc AgentTurnRunner
  -> lauren-ai run_stream()/run()
  -> provider assistant response with call IDs A and B
  -> canonical assistant message containing tool calls A and B
  -> executor runs A and B (possibly concurrently)
  -> exactly one result for A and one result for B
  -> one canonical result exchange in memory
  -> provider serializer maps results to its native protocol
  -> next provider request
```

### Failure data flow

```text
Provider returns A + B
  -> one result is cancelled, filtered, malformed, or assigned an empty/wrong ID
  -> memory accepts a partial or incorrectly correlated result batch
  -> serializer silently drops an invalid result or emits too few results
  -> provider receives assistant calls A + B but not matching tool messages
  -> provider returns HTTP 400
```

The following are concrete risk points in the current architecture:

1. `ShortTermMemory.add_tool_results()` trusts its input. It consolidates
   result blocks but does not enforce exact one-to-one correspondence with the
   immediately preceding assistant batch.
2. `run_stream()` allows `_on_tools_requested()` to return a subset of calls.
   When the returned list is non-empty, it executes only that subset without
   necessarily creating explicit results for omitted calls.
3. Tool result hooks can replace a result. A custom or downstream hook can
   accidentally return a result with an empty or different `tool_use_id`.
4. Parallel execution can be interrupted between individual tool completion
   and batch commit. Cancellation handling repairs some dangling tails, but
   generic provider errors do not consistently persist a repair before the
   error is surfaced.
5. The OpenAI adapter currently skips a result block with an empty
   `tool_use_id`. This converts a local malformed state into a remote “missing
   tool message” error instead of failing before the request is sent.
6. Stream parsing and fallback requests can produce ambiguous or duplicated
   tool-call identities if a provider omits a name or ID in a delta sequence.
7. `run()` and `run_stream()` contain parallel versions of the assistant
   commit/execute/result-commit algorithm. Their interruption and healing
   semantics can diverge.
8. Healing currently focuses on a dangling tail and missing IDs. It does not
   fully reject or canonicalize unknown, duplicate, out-of-order, or
   non-adjacent results.

These are identified implementation gaps, not proof that every gap caused the
reported request. The implementation must preserve the distinction between
confirmed evidence and inferred causes by emitting a typed invariant
diagnostic when the actual bad state is encountered.

## Goals

### G-1 — Enforce a provider-neutral tool exchange invariant

Before a provider request is serialized, the conversation must guarantee:

- each assistant tool call has a non-empty, unique ID;
- every result belongs to the current assistant batch;
- every call ID has exactly one result;
- no result has an unknown or duplicate call ID;
- the result exchange is adjacent to the assistant tool-call message in the
  canonical conversation model;
- the native provider representation is derived from this validated model.

### G-2 — Make tool execution atomic from the conversation’s perspective

Tool calls may execute concurrently and may have external side effects, but
conversation mutation must be transactional. A batch is either committed with
all corresponding results or is explicitly aborted with safe synthetic error
results for every unresolved call.

### G-3 — Preserve state through interruption, retries, workflows, and resume

Cancellation, process interruption, queued user input, transient transport
retry, workflow checkpointing, journal replay, headless execution, subagents,
and session resume must all use the same transaction semantics.

### G-4 — Fail locally and diagnostically

No provider adapter may silently drop malformed tool results. Invalid history
must be repaired deterministically where safe, or rejected before network I/O
with a typed error that is actionable but does not disclose sensitive content.

### G-5 — Keep existing tools and providers compatible

Existing `Tool`, `ToolResult`, approval, exploratory, workflow, and provider
integrations should continue to work. Legacy hooks should be adapted at one
boundary rather than requiring every tool implementation to understand the new
transaction object.

## Non-goals

- Redesigning the TUI’s `Explored` presentation; PRD-161 remains responsible
  for that presentation.
- Changing tool approval policy, filesystem scope, or network policy.
- Making every external tool side effect fully rollbackable. The transaction
  protects conversation integrity and uses idempotency/retry policy for side
  effects; it cannot undo an arbitrary external action.
- Adding provider-specific business logic to agenthicc.
- Treating a provider’s 400 as safe to retry indefinitely.
- Logging complete provider payloads, prompts, tool arguments, file contents,
  or secrets for diagnosis.

## Requirements

### Shared lauren-ai requirements

#### R-1 — Canonical tool-exchange model

Introduce a provider-neutral internal model for one assistant tool-call batch.
The exact public names may be selected during implementation, but it must
represent:

- ordered requested calls (`tool_use_id`, tool name, normalized input);
- ordered execution outcomes;
- transaction state (`started`, `committed`, `aborted`);
- the run/turn correlation needed for diagnostics;
- whether a result was executed, rejected, cancelled, timed out, or
  synthesized.

The model must not contain raw prompts or unbounded tool output in diagnostic
fields. It may hold those values internally where the existing memory/tool
contracts require them.

#### R-2 — Conversation validator

Add one authoritative validator used by both `run()` and `run_stream()` and
called before every provider request. It must validate the complete relevant
tool-call tail and return structured failures containing only:

- provider-neutral failure code;
- expected and observed call-ID counts;
- redacted or hashed IDs when diagnostics need correlation;
- conversation/run identifier where already safe to expose;
- repairability and recommended recovery action.

At minimum, detect empty IDs, duplicate call IDs, unknown result IDs, missing
results, duplicate results, non-adjacent exchanges, and an assistant tool-call
message with no result exchange.

#### R-3 — Deterministic repair

Provide a repair operation with explicit semantics:

- preserve all valid results already correlated to the current batch;
- synthesize one error result for each unresolved call;
- use a stable, non-sensitive reason such as `tool execution interrupted`;
- never invent a result for an unknown ID;
- never attach a result to a different assistant batch;
- persist the repaired state when the memory implementation is journaled;
- be idempotent when called repeatedly during cancellation and resume.

Repair must be invoked before the next provider request and during durable
interruption handling. Repair must not silently convert an unknown or duplicate
ID into a valid one; those cases require rejection or a clearly recorded
quarantine path.

#### R-4 — Transactional runner algorithm

Refactor the normal and streaming runners to share one algorithm:

1. Validate and snapshot the conversation before the provider request.
2. Parse the assistant response into canonical unique call IDs.
3. Commit the assistant tool-call message atomically.
4. Resolve approval/filtering decisions for the full batch.
5. Produce one result for every requested call. Omitted or denied calls receive
   explicit error results rather than disappearing.
6. Commit the complete result exchange atomically.
7. Validate again before continuing to the provider.
8. On cancellation or failure, preserve completed results and synthesize
   results for unresolved calls, or restore the pre-exchange snapshot when the
   failure is a retryable transport failure that occurred before tool
   execution.

The same semantics must apply to non-streaming execution. A cancellation
between two `asyncio.gather()` completions must not leave the conversation
with only one result.

#### R-5 — Result-ID ownership at the executor boundary

The executor owns correlation between a requested call and its result. Tool
implementations and hooks may supply content and error status but may not
change the call ID of the request they are answering.

If a legacy hook returns a `ToolResult`, the executor must either re-key it to
the original request ID while preserving its status/content, or reject it with
a typed local error according to a documented compatibility policy. It must
never accept an empty, unknown, or duplicate ID into canonical memory.

#### R-6 — Approval and partial-selection semantics

`_on_tools_requested()` may still approve, deny, or filter calls, but the
contract must return a decision for every requested call. A denied or omitted
call becomes an explicit error result with the original call ID. A batch-level
abort must generate results for all calls not already completed.

#### R-7 — Streaming parser identity guarantees

Streaming tool-call deltas must be normalized into exactly one canonical call
per provider call index/identity. The parser must detect and reject ambiguous
sequences instead of producing duplicate calls through a fallback request.

Fallback completion is permitted only when the provider response can be
unambiguously reconciled with already accumulated calls. Otherwise the runner
must raise a typed protocol error before tool execution and leave the
conversation recoverable.

#### R-8 — Provider adapter strictness

OpenAI-compatible and Anthropic serializers must consume only validated
canonical exchanges. They must not silently skip a result because its ID is
empty or malformed. They must raise a typed `InvalidToolConversationError`
or equivalent before sending the request.

The error must include provider and invariant metadata, not message content.
Provider-specific mapping remains in lauren-ai adapters; agenthicc must not
parse provider payloads to work around this requirement.

#### R-9 — Snapshot, restore, and idempotency compatibility

Snapshots must include enough transaction state to distinguish:

- an exchange not started;
- an assistant batch committed but execution incomplete;
- a fully committed exchange;
- an aborted exchange with synthetic results.

Transient transport retries must restore the pre-request snapshot when no tool
execution was committed, preserving current idempotency-ledger behavior.
Retries must not execute a side-effecting tool twice when the first execution
already committed its result. The validator must run after restore and before
retry.

#### R-10 — Journal lifecycle hooks

Expose lifecycle events or callbacks for `started`, `result recorded`,
`committed`, and `aborted/repaired`. Existing memory implementations that do
not journal may no-op these hooks. Journaled memory must persist enough state
for a restart to repair an incomplete exchange exactly once.

### agenthicc integration requirements

#### R-11 — Agent-turn boundary adapter

Update `src/agenthicc/runners/agent_turn.py` to use the shared lauren-ai
transaction API. The compatibility `_run_agent_turn` path, direct turns,
workflow turns, subagents, background runs, headless runs, and replay/cassette
execution must all pass through the same validation boundary.

Agenthicc must not maintain a second private implementation of tool-result
correlation. Existing `ensure_valid()` calls may remain as compatibility
guards, but they must delegate to the canonical validator/repair operation.

#### R-12 — Durable interruption handling

On `asyncio.CancelledError`, `KeyboardInterrupt`, process shutdown, or a
workflow pause, agenthicc must:

1. stop scheduling new calls;
2. collect results already completed;
3. record/synthesize results for unresolved calls;
4. persist the repaired journal before returning control to the TUI or
   re-raising cancellation;
5. expose the interruption state to resume logic.

Generic exceptions must use the same repair path when an assistant tool batch
has been committed. A provider transport error that occurred before tool
execution may restore the pre-request snapshot instead.

#### R-13 — Queued input ordering

The `_QueuedInputRunner` path must commit the complete tool exchange before
injecting queued user input. It must not append a user message between the
assistant tool-call message and its result exchange. Queued input must remain
after the result message in canonical memory and must not cause duplicate
results when the outer runner also processes the returned result list.

#### R-14 — Resume and workflow checkpoint validation

Before a resumed session or workflow invokes the provider, validate and repair
the rehydrated conversation. Checkpoint restore must preserve tool exchange
state, approval state, phase state, artifacts, and journal ordering. A paused
`code_plan`, `create_workflow`, or generated custom workflow must not restart
from its first phase merely because a tool exchange was interrupted.

The same rule applies to workflow-generated agents and workflows: they inherit
the shared runner contract without needing to implement provider-specific
healing in their generated code.

#### R-15 — TUI and headless recovery events

When repair occurs, emit a structured internal event and a concise user-facing
notice, for example:

```text
Tool execution was interrupted; incomplete tool results were recorded so the
session can continue safely.
```

Do not print tool arguments, file contents, credentials, or the full provider
error. Preserve existing individual tool events and `Explored` aggregation.
Headless mode must receive a structured log/error status rather than relying
on terminal rendering.

When a malformed exchange cannot be repaired, stop before the provider
request, report that the conversation requires recovery, and offer resume or
retry without an infinite retry loop.

#### R-16 — Version and compatibility boundary

Pin or require the first lauren-ai release that provides the transaction and
validator contract. If a staged rollout must support older lauren-ai versions,
use feature detection at startup and fail with an actionable compatibility
error rather than silently selecting the old unsafe path.

The implementation must document the selected minimum version and update
`pyproject.toml`, lock metadata, release notes, and compatibility tests as
appropriate.

## Proposed API shape

The names below are illustrative; the implementation may choose equivalent
names that follow lauren-ai conventions. The important requirement is one
shared contract, not a particular class hierarchy.

```python
exchange = memory.begin_tool_exchange(tool_calls, run_id=run_id)

decisions = await executor.resolve_batch(exchange, approval=approval)
results = await executor.execute_batch(exchange, decisions=decisions)

memory.commit_tool_exchange(
    exchange,
    results,
    on_unresolved="synthesize_error_results",
)
memory.validate_tool_history()
```

The validator should be usable independently by persistence/recovery code:

```python
report = memory.validate_tool_history()
if not report.ok:
    memory.repair_tool_history(report)
    memory.ensure_valid_and_persist()
```

The final API must define whether result content is retained in a transaction
object or passed separately, how hooks observe lifecycle transitions, and how
old `add_tool_results()` callers are adapted. The implementation must not
allow a public compatibility method to bypass validation.

## Detailed recovery policy

| Failure point | Required action | Provider request allowed? |
|---|---|---|
| Before assistant response | Restore pre-request snapshot on retryable transport failure | Yes, after validation |
| Assistant calls parsed but no tools started | Synthesize error result for every call, persist, then stop or continue according to runner policy | Yes only after validation |
| Some parallel tools completed | Preserve valid completed results; synthesize unresolved results; persist | Yes, after validation |
| Approval rejects a subset | Emit a result for every rejected call with its original ID | Yes, after validation |
| Hook returns wrong/empty ID | Re-key under compatibility policy or reject locally; never serialize it | No until repaired |
| Serializer sees malformed history | Raise typed local error with safe metadata | No |
| Provider returns malformed tool-call stream | Do not execute ambiguous calls; preserve recoverable state and report protocol error | No |
| Process restarts after exchange began | Rehydrate transaction, repair idempotently, persist, validate | Yes, after validation |
| Already committed exchange during retry | Do not re-execute tools; reuse committed results under idempotency rules | Yes, after validation |

The runner may choose to stop after synthesizing results rather than make an
additional provider request. That choice must be explicit and consistent for
interactive, headless, workflow, and resume paths.

## Observability and diagnostics

Add structured events/metrics for:

- `tool_exchange_started`;
- `tool_exchange_committed`;
- `tool_exchange_aborted`;
- `tool_exchange_repaired`;
- `tool_conversation_invariant_violation`;
- provider serialization blocked by invalid history.

Each event may include provider name, workflow/agent name, session/run IDs
already approved for logs, call count, completed count, and a redacted
invariant code. It must not include tool inputs, tool output, prompt text,
filesystem contents, API keys, authorization headers, or full message payloads.

Expose enough information in tests and debug logging to distinguish missing,
duplicate, unknown, empty, and non-adjacent IDs. Keep default production logs
concise and avoid turning every normal parallel tool batch into a noisy
transcript event.

## Testing strategy

Tests must be deterministic and must use fake providers/tools for protocol
cases. No test should call a real model or external website.

### lauren-ai unit tests

1. A two-call parallel exchange commits two matching results in order.
2. Missing, empty, duplicate, unknown, and wrong result IDs are rejected.
3. Out-of-order results are either normalized deterministically or rejected
   according to the documented canonical ordering.
4. A filtered/denied subset produces explicit results for omitted calls.
5. A result hook that returns an empty or different ID cannot corrupt memory.
6. Cancellation before execution, between parallel completions, during a tool
   hook, and after tool execution but before commit all produce a valid
   repaired exchange.
7. `run()` and `run_stream()` have identical invariant and recovery behavior.
8. Stream deltas with missing names, repeated indexes, missing IDs, and
   fallback responses are reconciled or rejected without duplicate calls.
9. OpenAI and Anthropic serialization rejects malformed canonical history
   locally and serializes valid history with every result present.
10. Snapshot/restore, journal replay, compaction, and repeated repair are
    idempotent.
11. Retry with a transient transport error does not duplicate a side effect or
    lose a committed result.
12. Unknown tools, tool exceptions, timeouts, approval aborts, and executor
    policy errors all return a result correlated to the requested ID.

### agenthicc unit and integration tests

1. `AgentTurnRunner` validates before the first provider request and after
   every tool exchange.
2. Journaled memory persists a repaired interrupted exchange and rehydrates it
   validly in a new session process.
3. Generic provider 400 handling repairs/persists committed tool state and
   does not blindly retry a permanent error.
4. `_QueuedInputRunner` places queued input after the complete tool result
   exchange and never duplicates results.
5. Approval hooks, `ToolOutputCaptureHook`, native tools, external tools, and
   custom agent hooks preserve result IDs.
6. `code_plan`, `create_workflow`, and generated workflows preserve phase,
   checkpoint, approval, artifact, and tool-exchange state across interruption
   and resume.
7. Direct, headless, subagent, background, cassette-record, and cassette-replay
   execution paths all use the same validator.
8. TUI receives a repair event while retaining individual tool output and
   exploratory grouping.
9. Safe diagnostics never contain API keys, authorization headers, prompts,
   tool arguments, file contents, or unbounded tool output.

### End-to-end regression journeys

1. Paste the exact two-image-mention request, let two fake `Read` calls run in
   parallel, and verify the next fake provider request contains one valid
   result for each call and does not produce the reported 400.
2. Interrupt the same request while one `Read` is complete and the other is
   running; send a follow-up message and verify the session continues from a
   repaired transcript.
3. Resume a session after process termination between assistant-call commit
   and result commit; verify repair is performed once and the workflow phase
   is unchanged.
4. Deny one of two tool calls; verify the provider receives an explicit result
   for the denied call and a normal result for the approved call.
5. Force a transient transport failure before tool execution and verify retry
   restores the snapshot; force it after execution and verify idempotent
   reuse rather than duplicate execution.
6. Feed an invalid provider stream and verify a local protocol diagnostic with
   no provider request containing malformed history.

## Acceptance criteria

- **AC-1:** The exact two-parallel-`Read` regression journey completes without
  an “insufficient tool messages” provider error when every fake tool result
  is valid.
- **AC-2:** No provider serializer silently drops a tool-result block because
  of an empty, unknown, duplicate, or malformed ID.
- **AC-3:** Before every provider request, the canonical conversation passes
  the one-result-per-call invariant, or the request is blocked locally.
- **AC-4:** Cancellation at each tested interruption point leaves durable,
  valid, resumable memory and does not create duplicate result messages.
- **AC-5:** Partial approval/filtering creates explicit results for all calls.
- **AC-6:** Streaming and non-streaming runners share the same observable
  correlation and recovery semantics.
- **AC-7:** Session resume, workflow checkpoints, queued input, headless mode,
  subagents, and generated workflows preserve tool exchange ordering and
  phase state.
- **AC-8:** A malformed exchange produces a typed local diagnostic containing
  safe metadata and no sensitive payload; permanent provider 400s are not
  retried indefinitely.
- **AC-9:** Existing valid provider conversations, tools, approval hooks,
  cassette replay, compaction, and journal recovery remain compatible.
- **AC-10:** Unit, integration, and E2E tests listed above pass in CI with no
  real model/network dependency.
- **AC-11:** Documentation describes the invariant, recovery behavior, the
  lauren-ai minimum version, and the new diagnostic events.

## Rollout and migration

1. Implement and test the validator and transaction API in lauren-ai first.
2. Add strict provider-adapter checks and release a compatible lauren-ai
   version. Keep legacy methods as validated adapters during the migration.
3. Upgrade agenthicc’s dependency and integrate the shared runner path.
4. Add opt-in diagnostic counters in the first agenthicc release if telemetry
   policy requires a gradual rollout; the pre-send invariant itself must be
   enabled by default once the dependency is present.
5. Enable durable repair for session resume and workflow checkpoints.
6. Remove compatibility shims only after downstream callers and generated
   workflows have migrated, with a deprecation notice and release note.

If the installed lauren-ai version does not provide the required contract,
agenthicc must fail at startup or runner construction with a clear upgrade
message. It must not silently fall back to the unsafe serializer behavior.

## Performance and security constraints

- Validation should be linear in the relevant message tail and number of tool
  IDs, with bounded auxiliary memory.
- Do not copy complete tool outputs merely to validate IDs.
- Batch execution may remain concurrent; only conversation commits are
  serialized.
- Synthetic results must not contain secrets or raw exception traces by
  default.
- Diagnostics must redact authorization headers, API keys, session contents,
  tool inputs/outputs, and sensitive filesystem paths.
- Repairs and journal writes must be atomic with respect to process crashes.
- All provider requests must pass through the existing configured HTTP/client
  boundary; this PRD does not add a network bypass.

## Documentation and implementation records

When implemented, update:

- `lauren-ai` runner, memory, executor, transport, and public API docs;
- agenthicc `docs/guides/architecture.md`, workflow guidance, and storage
  guidance;
- `README.md` for the user-visible recovery behavior;
- `llms-full.txt` and `llms.txt` for public symbols;
- dependency/version metadata and release notes;
- this PRD with implementation commit links, test evidence, and final status.

Related work includes [PRD-148 — Unified Interrupt and Graceful
Cancellation](prd-148-unified-interrupt-and-graceful-cancellation.md),
[PRD-156 — Resumable Plan Interrupts](prd-156-resumable-plan-interrupts.md),
[PRD-157 — Unified Usage Accounting](prd-157-usage-accounting-and-tui-token-observability.md),
[PRD-161 — Exploratory Tool-Call Consolidation in the TUI](prd-161-exploratory-tool-call-consolidation.md),
[PRD-163 — Cache-Stable Workflow Prompts and Generated Workflows](prd-163-cache-stable-workflow-prompts-and-generated-workflows.md),
and [PRD-168 — Mode-Aware Parent-Workspace Access](prd-168-mode-aware-parent-workspace-access.md).

## Assumptions and implementation questions

- The reported provider is OpenAI-compatible, but the invariant must be
  provider-neutral and tested for Anthropic as well.
- The two visible `Read` operations correspond to two model tool calls; the
  implementation should confirm this with a fake-provider regression test and
  safe call-ID diagnostics rather than assuming the UI event stream is the
  canonical history.
- The final public API names and the minimum lauren-ai release are design
  decisions for implementation, constrained by the compatibility requirements
  above.
- The implementation team must decide whether an exchange with all tools
  denied should continue to the provider or stop after recording denial
  results. Either behavior is acceptable if it is explicit, consistent, and
  covered by tests.

## Verification commands

The implementation is complete only after the relevant checks pass in both
repositories. For agenthicc, run at minimum:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

For lauren-ai, run its documented lint, type, unit, integration, and provider
serialization suites, including the new transaction and malformed-history
matrix. Add a deterministic cross-repository integration test against the
installed lauren-ai version before updating the agenthicc lockfile.

## Implementation record

The working-tree implementation uses the following concrete API names:

- `lauren_ai._memory.ToolExchange`, `ToolCallRecord`, `ToolResultRecord`,
  `ToolHistoryReport`, and `ShortTermMemory.validate_tool_history()`;
- `ShortTermMemory.begin_tool_exchange()`,
  `commit_tool_exchange()`, `abort_tool_exchange()`, and
  `repair_tool_history()`;
- `ToolConversationIntegrityError` plus the lifecycle signals
  `ToolExchangeStarted`, `ToolExchangeResultRecorded`,
  `ToolExchangeCommitted`, `ToolExchangeAborted`, `ToolExchangeRepaired`,
  `ToolConversationInvariantViolation`, and `ToolSerializationBlocked`.

`run()` and `run_stream()` use the same result-ID completion and recovery
contract. `JournaledShortTermMemory` persists the lifecycle using hashed call
IDs and writes a durable reset after deterministic interruption repair.
Agenthicc rejects a runtime without the shared methods rather than selecting a
legacy unsafe path. The published lauren-ai 1.5.0 release provides the
transaction API, and agenthicc now declares `>=1.5.0,<2` in both project and
lock metadata.

Verification completed in this working tree:

- lauren-ai unit suite: `1398 passed, 1 skipped`;
- lauren-ai integration suite: `1007 passed`;
- lauren-ai transaction/runner regression matrix: `116 passed`;
- agenthicc PRD-169 unit and E2E regressions: `9 passed` against the
  transaction-capable lauren-ai working tree;
- agenthicc full unit suite: `2883 passed, 14 skipped, 2 pre-existing background-worker failures`;
- agenthicc full integration suite: `188 passed, 6 pre-existing background-worker failures`;
- agenthicc E2E suite: `99 passed, 1 skipped`;
- targeted Ruff and type-audit checks pass. Full mypy remains blocked by the
  repository's existing `name_that_ui` import and installed NumPy stub errors.
