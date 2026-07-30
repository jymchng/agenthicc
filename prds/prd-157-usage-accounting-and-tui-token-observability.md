---
title: "PRD-157: Canonical Usage Accounting and TUI Token Observability"
status: Implemented
version: 1.0.0
created: 2026-07-30
study_date: 2026-07-30
scope: Make /usage, the TUI token display, workflows, subagents, and session inspection use one correct durable usage ledger
related_prds:
  - PRD-68   # TUI feature expectations and status presentation
  - PRD-82   # historical live-token timing design
  - PRD-83   # current live-token/reconciliation implementation
  - PRD-100  # code_plan workflow architecture
  - PRD-129  # conversation durability and retry resilience
  - PRD-150  # client-neutral session projection
  - PRD-154  # create_workflow architecture
  - PRD-156  # resumable workflow continuation
tags:
  - usage
  - tokens
  - cost
  - tui
  - workflows
  - durability
---

# PRD-157 — Canonical Usage Accounting and TUI Token Observability

Study date: 2026-07-30. This PRD specifies the complete implementation of
`/usage` and the token/cost information displayed by the TUI. It is based on
the current source tree and the installed `lauren-ai` contracts, not only on
the historical PRD-82/PRD-83 design.

## 1. Executive verdict

The `/usage` command's dispatch policy is correct for the narrow case it was
designed for: it is a local, immediate, read-only command; it does not enqueue
user text or call the provider; and it reads the same reactive counters that
the normal TUI status component renders.

The usage result as a whole is not yet correct or durable. Token accounting is
split between a live chunk path, an unscoped run-completion handler, a reactive
UI store, and persistence code that expects token events which the live path
never emits. A normal run can therefore show plausible numbers during the
current process while `/usage`, the status bar, session restore, and session
inspection disagree across restarts or execution paths.

The required fix is a session-owned, idempotent usage ledger. The reactive
`ConversationStore` becomes a projection of that ledger; it is no longer the
authority for totals. `/usage`, the status bar, session export/inspection, and
workflow/subagent execution all read the same ledger snapshot.

## 2. Evidence-backed current-state study

### 2.1 `/usage` command path

The current path is:

```text
typed /usage
  → busy-policy classifier marks it IMMEDIATE_READ_ONLY
  → TUISession.dispatch_slash() creates CommandContext
  → TUISession._usage_snapshot()
  → ConversationStore.tokens_in/tokens_out/cost_usd
  → _cmd_usage() prints one local line
```

The relevant implementation is distributed across:

| Source | Verified behaviour |
|---|---|
| `commands/builtins.py` | `_cmd_usage()` prints `input`, `output`, `total`, `cost`, `state`, and `queued`; it returns a handled result without an agent request. |
| `commands/command.py` | `UsageSnapshot` is a frozen value object with input/output/cost, active-run, queue-depth, and computed total fields. |
| `runners/tui_session.py` | `_usage_snapshot()` reads the reactive conversation counters and the local task/queue state. |
| `commands/dispatcher.py` | The usage callback is preserved when the dispatcher builds the parsed command context. |
| `docs/guides/commands.md` | Documents `/usage` as an immediate local query. |

The existing busy-command unit and E2E tests confirm that `/usage` runs while
an agent task is active, leaves the FIFO message queue unchanged, and does not
create a `user_message` event. Those tests passed during this study:

```text
27 passed in 1.92s
```

This part is sound, but it only proves presentation of values already in the
store. It does not prove that the values are complete, authoritative, or
durable.

### 2.2 Current token producers

There are two intended producers:

1. `AgentTurnRunner._stream()` consumes a non-`None` `CompletionChunk.usage`
   and calls `ConversationStore.add_tokens()` immediately, before the visible
   text event for that sub-turn.
2. `_build_session_context()` registers an `AgentRunComplete` handler on the
   shared lauren-ai `SignalBus`. It reads the signal's cumulative
   `total_usage` and calls `ConversationStore.set_tokens()` with a process-local
   baseline plus the completed run total.

The installed lauren-ai runner confirms that `AgentRunComplete.total_usage` is
cumulative for one agent run, while `AgentTurnComplete.turn_usage` is per model
turn. The `AgentRunComplete` signal contains `agent_id`, but it does not contain
the session `conversation_id`. The handler in agenthicc therefore cannot prove
that an event belongs to the TUI session that installed it.

The current flow is:

```text
provider CompletionChunk.usage
  → AgentTurnRunner._stream()
  → ConversationStore.add_tokens()
  → reactive signals
  → StatusComponent / ScrollBufferAppender / _usage_snapshot()

shared SignalBus AgentRunComplete.total_usage
  → process-local _baseline in tui_session.py
  → ConversationStore.set_tokens()
  → same reactive consumers
```

This is a two-source update model. It avoids double counting only when all of
the following remain true: usage appears on exactly one final chunk per
sub-turn, the completion signal is emitted exactly once, the signal belongs to
this session, no overlapping run changes the baseline, and the baseline began
with the same durable total as the store. Those conditions are not represented
by a shared typed invariant.

### 2.3 Workflows, direct turns, and subagents

The standard workflow configuration passes the session's
`session_memory`, `conversation_id`, and reactive `conv_store` to workflow
phase helpers. `code_plan`, `create_workflow`, and downstream plugins that use
the standard `_run_agent_turn()` path therefore participate in the live chunk
counter path.

However, the current `AgentTurnRunner` creates a new active runner with the
parent runner's transport and signal bus for each turn. A shared
`AgentRunComplete` handler observes those workflow runs without an explicit
workflow/session correlation key.

Subagent workers take a different path: they construct an isolated
`AgentRunnerBase` with no signal bus, call `runner.run()` rather than the
streaming helper, and do not pass the parent `ConversationStore`. Their
provider usage is absent from the TUI totals. Automatic compaction and any
other provider call that bypasses the standard streaming accounting path also
needs an explicit accounting policy.

### 2.4 Persistence and restart gap

`ConversationStore.add_tokens()` and `set_tokens()` update signals only. They do
not call `append_event("tokens", ...)`.

In contrast, all of the following read `tokens` events:

- `tui/runtime/session_log.py:restore_session()`;
- `tui/runtime/session_export.py:_conversation_summary()`; and
- `cli/commands/sessions.py` session inspection output.

The newer durable `ConversationJournal` restores provider messages and tool
records, but its entry types currently contain no usage record. Consequently,
the normal live token path is not represented in either durable source. A
session can reopen with its provider conversation intact while its TUI usage
starts at zero, and session inspection can report zero even though the live
screen previously displayed non-zero values.

The legacy restore function can fold hand-authored or older `tokens` events,
but the production session-context construction does not use it to seed the
new journal-backed session. If a restored counter is supplied through that
legacy path, the reconciliation baseline is still initialized to `(0, 0,
0.0)`, so the first completion can overwrite the restored total instead of
extending it.

### 2.5 Cost and data-quality gap

`lauren-ai.TokenUsage.cost_usd()` uses an approximate bundled price table,
prefix matching, and a default price for unknown models. It currently counts
input and output only; cache-read and cache-write fields are present but not
priced. The current `UsageSnapshot.cost_usd` has no flag distinguishing an
authoritative provider charge, a local estimate, an unknown model estimate, or
an unavailable value. A displayed `$0.0000` can therefore mean either a real
zero, no provider usage, or a missing cost path.

The status component also has presentation inconsistencies: tokens are shown
as unlabeled arrows only when non-zero, the terminal-wait layout omits them,
and the idle scroll header and live status line use different layouts. These
are secondary to the accounting defect but should be corrected once all views
share one snapshot.

## 3. Goals and non-goals

### Goals

- Make `/usage`, the live status bar, the idle header, session inspection, and
  session export agree for the same session and run.
- Count each provider model call exactly once, including workflow phases,
  direct turns, standard subagents, retries, and automatic compaction calls
  according to the explicit inclusion policy in this PRD.
- Show live usage as soon as authoritative usage is available while allowing a
  final provider completion signal to reconcile it without duplication.
- Persist usage in the existing session durability boundary and rehydrate it
  with the same stable session `conversation_id` as the provider conversation.
- Distinguish known, estimated, partial, and unavailable values instead of
  silently treating missing usage as zero.
- Preserve the current immediate, local, no-provider-call behaviour of
  `/usage`.
- Give every standard workflow—including workflows generated by
  `create_workflow`—the usage contract automatically when it uses the standard
  phase runner and session `WorkflowConfig`.
- Keep the public command and existing `UsageSnapshot` fields backwards
  compatible while allowing richer fields to be added deliberately.

### Non-goals

- Billing reconciliation with a provider account or replacing provider invoices.
- Estimating token usage from character counts when the provider supplied no
  usage data. Such a value may be exposed separately as an estimate, but it
  must not be presented as authoritative usage.
- Persisting prompts, completions, API keys, or arbitrary provider payloads in
  the usage ledger.
- Removing context-window trimming, compaction, retry rollback, or workflow
  checkpoints. Usage records must follow those lifecycle decisions, not change
  them.
- Making `/usage` an agent tool or a network-backed command.

## 4. Product and accounting contract

### 4.1 Canonical ownership

Add a session-scoped typed `UsageLedger` owned by `SessionContext` and opened
alongside `SessionConversation`. The ledger is the authority for completed
usage records and aggregate totals. It may use the existing
`ConversationJournal` as its append-only durable boundary, provided journal
folding continues to ignore usage entries when reconstructing provider
messages. A separate usage file is acceptable only if it is opened, flushed,
restored, and checkpointed as one session resource; creating a second
process-local counter is not acceptable.

`ConversationStore` remains the reactive presentation projection. It may retain
`tokens_in`, `tokens_out`, and `cost_usd` for compatibility, but those signals
must be updated from ledger snapshots or ledger events rather than being
independently incremented by arbitrary agent paths.

### 4.2 Usage record schema

Each completed or provisional provider call must have a typed immutable record
with at least:

| Field | Requirement |
|---|---|
| `record_id` | Stable local ID; used for idempotent replacement/reconciliation. |
| `session_id` / `conversation_id` | The owning session; required for every record. |
| `run_id` | Direct-turn or workflow-run correlation; nullable only for explicitly external calls. |
| `agent_id` / `agent_name` | Provider-run correlation when lauren-ai supplies it. |
| `call_index` | Monotonic model-call index within the run. |
| `provider` / `model` | Redacted identifiers used for display and cost policy. |
| `input_tokens` / `output_tokens` | Non-negative counts, with known/unavailable state. |
| `cache_read_tokens` / `cache_write_tokens` | Preserve provider fields when available. |
| `cost_usd` / `cost_status` | Value plus `authoritative`, `estimated`, `unknown`, or `unavailable`. |
| `source` | `chunk`, `model_call_complete`, `run_complete`, `reconciled`, or `unknown`. |
| `lifecycle` | `provisional`, `completed`, `partial`, `cancelled`, or `failed`. |
| timestamps | Creation and completion times; no prompt or response body. |

Reasoning/thinking tokens must be represented when a transport provides a
separate count. If they cannot be separated from output, document that output
includes them rather than inventing a second count.

### 4.3 Exactly-once and reconciliation rules

1. The accounting owner creates one call identity before each provider model
   call. A local identity is required when the provider does not supply one.
2. A final `CompletionChunk.usage` updates the live projection for that call.
   Repeated usage chunks replace the provisional value for the call; they are
   never blindly added.
3. `ModelCallComplete.usage` is the per-call authoritative fallback. It
   completes or reconciles the same call record rather than creating another
   record.
4. `AgentRunComplete.total_usage` is a cumulative run summary. It may repair a
   missing per-call record, but it must be scoped to the originating run and
   must never be added to already committed per-call records as a new call.
5. Retries use a new provider-call identity only when a new provider request is
   actually sent. A transport retry that replays a completed tool but sends a
   new model request counts the new model request once.
6. Cancellation commits only usage actually reported by the provider and marks
   it `partial` or `cancelled` when a completed usage signal is unavailable. It
   must not manufacture a zero record that hides unknown usage.
7. The ledger aggregate is derived from records, not from a process-local
   baseline. A restored session therefore cannot be overwritten by the first
   completion.

The implementation must use a scoped event sink, explicit run context, or an
equivalent correlation mechanism. A handler attached to a shared signal bus
that filters only by signal type is not sufficient.

### 4.4 Inclusion policy

The default session total includes all billable model calls initiated by the
session:

- direct agent turns;
- every phase of `code_plan`, `create_workflow`, and downstream workflow
  plugins using the standard runner contract;
- standard subagent workers, attributed to the parent run and their own agent
  identity; and
- automatic LLM compaction/summarization calls, marked as `compaction` so they
  can be separated in detailed output.

Local token estimates, tool execution, provider calls from unrelated sessions,
and cached subagent results with no provider call are excluded. A future
budget may request category-specific totals, but the main session total must be
consistent.

## 5. Data flow

### 5.1 Target runtime flow

```text
SessionContext(session_id)
  ├─ SessionConversation / ConversationJournal  ← provider history + durable usage records
  ├─ UsageLedger                                ← canonical records and aggregates
  └─ ConversationStore                          ← reactive UI projection

AgentTurnRunner / workflow phase / subagent / compactor
  → begin_call(session_id, run_id, call_index)
  → provider transport
  → live usage (chunk, if present)
  → UsageLedger.observe(call_id, usage, source=chunk)
  → projection snapshot → status bar and /usage can redraw immediately
  → per-call completion signal or run completion fallback
  → UsageLedger.complete_or_reconcile(call_id, usage)
  → append one durable usage record / replacement
  → aggregate session snapshot
  → ConversationStore + session service + inspect/export projections
```

### 5.2 Workflow and conversation flow

```text
one stable session_id / conversation_id
  → direct turn
  → Plan / code_plan phase
  → create_workflow design, generate, validate, summarize phases
  → downstream custom workflow phases
  → standard subagents
  → one UsageLedger aggregate and one provider conversation
```

The workflow checkpoint stores workflow state, phase artefacts, and the
conversation journal cursor as required by PRD-156. It does not duplicate the
usage aggregate. On resume, the workflow reuses the session ledger and
idempotency keys; already committed records are not replayed.

### 5.3 Projection flow

`UsageLedger.snapshot()` must be the only source used to construct
`UsageSnapshot` for `/usage`. The same snapshot or a derived immutable view must
feed `StatusComponent`, `ScrollBufferAppender`, session inspection, and
session export. Active task state and queue depth remain TUI/session runtime
fields; they are combined with the ledger's totals only at the final view
boundary.

## 6. `/usage` and TUI UX contract

### 6.1 Backwards-compatible command output

Keep the existing fields and the beginning of the current one-line output so
existing integrations can continue to recognize it:

```text
Usage: input=123 output=45 total=168 cost=$0.6789 state=running queued=2 ...
```

Append quality and scope fields, for example `cost_status=estimated`,
`usage_status=complete`, `calls=4`, and `session=<short-id>`. Exact formatting
must be covered by a small stable formatter test. Unknown values must render
as `—` or an explicit `unknown` status, not as a misleading zero. Known zero is
still rendered as `0`.

`/usage` remains `IMMEDIATE_READ_ONLY`, must not await a provider, and must
return a consistent snapshot even while a workflow or subagent is active.

### 6.2 Status rendering

- Use the same ledger projection as `/usage`; no independent arithmetic in the
  renderer.
- Preserve the familiar input/output arrows for compact terminals, but add a
  discoverable label or tooltip/help text and a visible marker when usage or
  cost is estimated/unknown.
- Keep token/cost information available in the terminal-wait layout when space
  permits, and make narrow-terminal truncation deterministic.
- The idle header, live status component, `/usage`, and session inspection must
  agree on the session totals at every completed-call boundary.

### 6.3 Scope and availability

`/usage` is a TUI/session command. If a non-TUI command context invokes it
without a session ledger, it must retain the current explicit
`Usage is unavailable in this command context.` response. A future client may
consume the same session-service usage projection, but no second accounting
implementation may be added.

## 7. Persistence, migration, and compatibility

- Add a versioned durable usage entry schema to the session-owned durability
  boundary. Unknown usage entries must not corrupt provider-memory folding.
- Flush usage records with the same failure handling and lifecycle ownership as
  the session conversation. A successful provider completion must not be
  displayed as durable until its usage record is accepted by the ledger.
- On resume, fold usage before constructing the first `UsageSnapshot`, and seed
  the reactive projection from the folded aggregate.
- Read legacy `conversation.jsonl` `tokens` events as compatibility input. Do
  not sum both a legacy event and its migrated canonical record.
- Keep `UsageSnapshot.input_tokens`, `output_tokens`, `cost_usd`,
  `active_run`, `queue_depth`, and `total_tokens` compatible for existing
  callers. New quality/category fields must have safe defaults.
- Session export and `sessions inspect` must use canonical records, report
  schema/version and data quality, and remain redacted. Existing consumers
  should continue to receive the current `tokens.input`, `tokens.output`, and
  `tokens.cost_usd` summary keys.
- If an old session has no usage records, show an explicit unavailable/unknown
  status rather than claiming that it consumed zero tokens. No historical
  provider data can be reconstructed from rendered text events.

## 8. Security and resilience

- Never persist prompts, completions, authorization headers, API keys, tool
  arguments, or provider response bodies in usage records.
- Reuse existing session path validation, file permissions, redaction, and
  corruption-tolerant JSONL reading. A corrupt trailing usage line must not
  make provider conversation recovery fail.
- Validate all counts as finite, non-negative integers and all costs as finite,
  non-negative numbers. Reject malformed provider payloads into an explicit
  unavailable/partial state.
- Make ledger writes idempotent and safe under the session conversation lock.
  A concurrent workflow/subagent must not race the aggregate into lost updates
  or duplicate totals.
- If the ledger cannot persist, keep the active run alive only if the product's
  existing durability policy permits it; surface a visible degraded state and
  never silently claim durable accounting.
- Do not use a local default price as an authoritative bill. Label bundled or
  fallback prices as estimates and expose the model/provider used for the
  estimate.

## 9. Implementation plan

1. Introduce typed usage records, data-quality states, aggregate snapshots, and
   idempotent fold/write operations in the session runtime boundary.
2. Add the ledger to `SessionContext` construction and rehydration, alongside
   `SessionConversation`; define the journal schema/version and legacy loader.
3. Replace direct `ConversationStore.add_tokens()` calls in
   `AgentTurnRunner._stream()` with the ledger collector/projection adapter.
   Add scoped run/call correlation to standard workflow, direct-turn, compactor,
   and subagent paths.
4. Remove the process-local baseline reconciliation model. Reconcile chunk,
   per-call, and cumulative run signals through one ledger API.
5. Build the existing `UsageSnapshot` and all TUI renderers from one ledger
   snapshot. Extend `/usage` output without breaking the current fields.
6. Update session restore, export, inspect, session-service projection, and
   documentation to consume canonical records.
7. Update `create_workflow` and the workflow validation contract so generated
   runners that use the standard `WorkflowConfig`/`_run_agent_turn()` receive
   usage accounting automatically. A custom runner that bypasses the standard
   turn boundary must explicitly call the usage API or be rejected/warned by
   validation.

## 10. Acceptance criteria

1. `/usage` remains immediate and local while a direct turn, workflow phase, or
   subagent is active; it does not enqueue input or contact a provider.
2. For a provider with final chunk usage, live totals update before the
   corresponding text event and the final aggregate contains one record for
   each model call.
3. For a provider with no chunk usage but a `ModelCallComplete` usage value,
   totals appear at completion and are counted exactly once.
4. For a cumulative `AgentRunComplete` fallback, a multi-turn run is reconciled
   to its per-call records without adding the cumulative total twice.
5. Duplicate chunk, model-complete, run-complete, retry, or replay signals do
   not change the aggregate more than once.
6. Direct turns, `code_plan`, `create_workflow`, generated standard workflows,
   compaction, and standard subagents all appear in the documented session
   total with category/source metadata.
7. A completed session is closed and reopened with the same session ID; the
   provider conversation, `/usage`, status bar, `sessions inspect`, and export
   all report matching totals and data quality.
8. A legacy session with `tokens` events is readable; a session with no usage
   records reports unavailable/unknown rather than false zero usage.
9. Cancellation and transport failure preserve only provider-reported usage,
   label partial/unknown state, and never create negative or duplicate totals.
10. Restoring a non-zero ledger and completing a new run extends the total; no
    baseline initialized to zero can overwrite the restored aggregate.
11. Narrow terminals, terminal-wait mode, idle status, live status, and `/usage`
    have deterministic output and do not raise on unknown/partial cost data.
12. Existing public `UsageSnapshot` fields, immediate-command behaviour, session
    summary keys, and unrelated workflow checkpoint/approval behaviour remain
    backwards compatible.

## 11. Test strategy

The current PRD-83 tests describe a two-source implementation and include a
test that treats missing chunk usage as a permanently correct zero. Those tests
must be rewritten around the ledger contract; they are not sufficient
acceptance tests for this PRD.

### Unit tests

- record validation, known zero versus unavailable, cost-quality states, cache
  fields, and legacy schema parsing;
- aggregation by session, run, workflow, phase, subagent, and category;
- idempotency keys, replacement/reconciliation, cumulative-run repair, retry,
  cancellation, and malformed provider payloads;
- journal fold, corrupt trailing lines, schema migration, and restored totals;
- stable `/usage` formatter and backwards-compatible `UsageSnapshot` defaults;
- status rendering for idle, active, unknown, estimated, terminal-wait, and
  narrow-column layouts.

### Integration tests

- `SessionContext` opens one ledger and one conversation for direct turns and
  workflow phases;
- streaming chunk usage and completion signals enter the same ledger exactly
  once;
- a provider with no chunk usage is reconciled from per-call completion;
- `AgentRunComplete` from another run/session cannot mutate this session;
- standard subagents and compaction are attributed once;
- journal close/reopen restores the aggregate before the first new turn;
- legacy `conversation.jsonl` summaries and new canonical records do not
  double count;
- session-service, `sessions inspect`, and export projections match ledger
  totals and redact sensitive data.

### End-to-end tests

- direct turn → `/usage` during streaming → final status and inspect output;
- `code_plan` and `create_workflow` phase sequence with multiple model calls,
  approval/rejection, retry, checkpoint/resume, and one session total;
- a generated custom workflow using the standard runner receives accounting
  without custom token code;
- concurrent standard subagents contribute exactly once;
- restart/resume after a completed phase and after a cancelled/partial turn;
- no-usage provider path displays `unknown`/`partial`, never misleading zero;
- all user-visible status layouts and command output remain deterministic.

## 12. Verification and documentation

Implementation must update the public command and storage documentation,
`llms-full.txt` for any public Python types, the workflow guide for the
generated-workflow contract, and the relevant storage/session-service
references. It must also update or supersede the live-token assumptions in
PRD-82 and PRD-83 without deleting their historical record.

Required verification commands are:

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

The implementation is complete only when the acceptance criteria pass in a
clean session and in a resumed session, with no known divergence between live
TUI, `/usage`, workflow execution, and persisted inspection output.
