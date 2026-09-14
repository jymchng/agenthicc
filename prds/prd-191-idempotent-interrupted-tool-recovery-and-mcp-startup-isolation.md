---
title: "PRD-191: Idempotent interrupted-tool recovery and MCP startup isolation"
status: Implemented
version: 1.1.0
date: 2026-09-14
scope: "interrupted tool execution, recovery event projection, TUI diagnostics, and MCP startup fault isolation"
related_prds:
  - PRD-12  # Historical MCP integration design
  - PRD-29  # MCP configuration and startup
  - PRD-148 # Unified interrupt and graceful cancellation
  - PRD-169 # Tool-call transaction integrity
  - PRD-172 # Production MCP integration
  - PRD-173 # Recoverable workflow errors
  - PRD-176 # Progressive startup
  - PRD-182 # Durable mid-turn preservation
  - PRD-184 # Preserve workflow phase after errors
tags:
  - tool-execution
  - interruption
  - recovery
  - idempotency
  - tui
  - mcp
  - startup
  - observability
---

# PRD-191 — Idempotent interrupted-tool recovery and MCP startup isolation

## 1. Executive summary

An interrupted tool execution currently produces a misleading error storm in
the TUI. One interruption can result in the same message being projected many
times:

```text
Tool execution was interrupted; incomplete tool results were recorded so the
session can continue safely.
```

The observed transcript also contains repeated startup diagnostics when an MCP
server cannot be started:

```text
MCP server 'asyncmove' failed to start: ...
```

The repetition is not useful recovery information. It obscures the real
assistant state, makes the scroll appender appear stuck, and makes it difficult
to determine whether the agent is waiting for the user, retrying a tool,
recovering a conversation, or merely replaying an old diagnostic.

This PRD defines a joint implementation across `lauren-ai` and agenthicc:

1. Give every logical tool exchange and every MCP startup generation a stable,
   idempotent identity.
2. Separate lifecycle events from user-facing transcript projections.
3. Allow each recovery signal to be durably recorded once and rendered once,
   even when multiple internal paths discover the same condition.
4. Preserve completed tool work and the pending assistant/user interaction
   across cancellation, provider failure, resume, and workflow continuation.
5. Treat MCP servers as independent startup fault domains. Optional server
   failures must not prevent healthy servers or built-in tools from loading.
6. Keep required-server failure fail-closed, but report it once with an
   actionable diagnostic rather than entering an unbounded startup loop.

The implementation must fix the reported behavior without hiding real errors,
discarding durable work, or weakening the existing tool-conversation invariant.

## 2. Evidence and current behavior

The production symptom is:

```text
● Run(command='cd /root/typescript_pro…, timeout=60)
└─ Completed  127ms  120 lines
     1   === empty-state.tsx (40 lines) ===
     2   import * as React from "react";
  ⋯ +116 more lines
I have the full picture, and I've found a genuine conflict
between the approved spec and the code as it stands that would
materially change the work. Let me confirm before I guess.
Tool execution was interrupted; incomplete tool results were recorded so the
session can continue safely.
Tool execution was interrupted; incomplete tool results were recorded so the
session can continue safely.
...
MCP server 'asyncmove' failed to start:
Tool execution was interrupted; incomplete tool results were recorded so the
session can continue safely.
...
```

The visible `Run` result is complete, but the provider turn containing it was
interrupted while the agent was handling a specification conflict. This is an
important distinction: the completed command result must remain in the
conversation, while only the unresolved tail of the current tool exchange may
be repaired. The diagnostic must not imply that the completed command was
lost, rerun, or necessarily failed.

Source review identifies the following relevant boundaries:

| Boundary | Current behavior | Risk |
| --- | --- | --- |
| `lauren-ai` runner | Emits `ToolExchangeRepaired` from multiple preflight and exception/recovery paths | One logical repair can be reported repeatedly |
| agenthicc agent turn | A signal handler and a compatibility event sink can project recovery into `ConversationStore` | Multiple listeners can render one condition more than once |
| agenthicc turn failure path | Cancellation, generic failure, and resume preparation each have recovery notices | A notice can be added once per layer rather than once per recovery fact |
| conversation journal | Durable state and transcript projection have different purposes | A replay can re-add a diagnostic that was already committed |
| MCP session manager | Startup is concurrent and already isolates many optional failures | A failed server needs a generation-scoped outcome and a single projection |
| TUI scroll appender | User-visible events are rendered as transcript lines | It cannot currently know that two equal-looking events are duplicates |

These are confirmed duplicate-producing paths. The implementation must still
instrument the lifecycle before changing behavior so tests can distinguish:

- one signal projected by several listeners;
- several signals emitted for one exchange;
- one server being retried repeatedly;
- one diagnostic being replayed during transcript rehydration; and
- genuinely independent failures that happen to have the same text.

## 3. Problem statement

### 3.1 Interrupted tool execution

Tool execution, provider streaming, recovery, journal persistence, and TUI
rendering currently have separate notions of “interrupted.” A single logical
condition may therefore travel through this path:

```text
tool/provider interruption
  -> lauren-ai repairs the in-memory exchange
  -> lauren-ai emits a repair lifecycle signal
  -> agenthicc signal handler appends a transcript event
  -> agenthicc turn exception handler appends a second notice
  -> resume/preflight repairs or validates again
  -> the same notice is appended again
  -> transcript replay renders each appended copy
```

The underlying repair may be correct while the user-visible projection is
incorrect. Conversely, suppressing all repeated text would be unsafe because
two independent interrupted exchanges must remain observable.

### 3.2 MCP startup failure

An MCP server can fail because its executable is missing, its process exits,
its URL is unreachable, authentication is absent, its protocol handshake is
invalid, or its startup timeout expires. These failures are independent of
the provider turn that may be running at the same time.

The current manager already has `required`, `auto_connect`, typed states, and
concurrent startup. The missing contract is the boundary between an internal
server lifecycle failure and the transcript:

- an optional server failure should be visible once and should not block other
  servers;
- a required server failure should fail the relevant startup operation once,
  with the server name and redacted cause;
- `/mcp reload` should create a new lifecycle generation and may report a new
  failure once, but an old generation must not replay into the new one;
- a late task completion or cancellation must not overwrite a newer server
  state or emit a stale failure.

### 3.3 Recovery and user interaction

The model may have discovered a real conflict and be about to ask the user a
question. The recovery implementation must preserve that pending interaction
and the completed evidence gathered before the interruption. It must not:

- restart the agent at the beginning of the turn;
- re-run completed side-effecting tools;
- repeatedly ask the same question because the diagnostic was replayed;
- turn a recoverable interruption into a permanent workflow failure; or
- silently swallow a required MCP failure.

## 4. Goals

1. Render one concise, structured recovery notice per logical interrupted tool
   exchange per session transcript.
2. Make recovery event processing idempotent across multiple sinks, retries,
   resume, checkpoint rehydration, and transcript replay.
3. Preserve all committed provider steps, completed tool results, partial-output
   metadata, workflow phase state, and pending user interaction according to
   PRD-169 and PRD-182.
4. Ensure a malformed or incomplete tool exchange is repaired or rejected
   locally before a provider request is sent.
5. Make MCP startup and reload outcomes independently observable by server and
   lifecycle generation.
6. Keep optional MCP failures from blocking healthy MCP servers, built-in
   tools, or ordinary agent turns.
7. Keep required MCP failures fail-closed with bounded, actionable behavior.
8. Make the scroll appender and transcript rehydration deterministic: the same
   durable event is not printed repeatedly merely because recovery was
   discovered more than once.
9. Preserve backwards compatibility for old journals, old event payloads, and
   older lauren-ai installations where feature detection is possible.
10. Add regression coverage at unit, integration, and end-to-end boundaries.

## 5. Non-goals

- Removing recovery diagnostics entirely.
- Treating an interrupted or uncertain side-effecting tool as successful.
- Retrying non-transient provider or MCP errors indefinitely.
- Making every MCP server mandatory.
- Hiding credentials, command output, provider errors, or server identity from
  authorized diagnostics; redaction remains required.
- Replacing the existing journal, memory, workflow checkpoint, or MCP bridge
  architecture with a second persistence system.
- Making the scroll appender responsible for deciding whether a tool exchange
  was actually repaired.
- Deduplicating all equal strings globally. Deduplication must be scoped to a
  logical event identity and lifecycle generation.
- Changing the meaning of `required = true`, `auto_connect`, or explicit MCP
  connect/reload commands.

## 6. Definitions and identity model

### 6.1 Logical tool exchange

A logical exchange is one assistant tool-call batch, identified by a stable
`exchange_id`. It may contain multiple tool calls and may have multiple
execution attempts, but it has one canonical commit or repair outcome.

### 6.2 Tool attempt

An attempt is one execution of one call within an exchange. Its identity is
`attempt_id`. A retry creates a new attempt under the same exchange and call
identity. An attempt result must never be mistaken for a second logical
exchange.

### 6.3 Recovery fact

A recovery fact is the durable statement that an exchange was repaired,
aborted, or left uncertain. It must include a stable `event_id` derived from
the exchange and terminal recovery revision, not from the time the event is
observed.

### 6.4 Projection

A projection is a consumer-specific representation of a fact. The journal is
the durable source of truth; the provider memory, workflow state, headless
output, and TUI transcript are projections. A projection may be rebuilt from
the journal without producing duplicate user-visible events.

### 6.5 MCP lifecycle generation

Each `start_all`, explicit `connect`, or `reload_all` operation receives a
monotonic `generation_id`. Every server outcome is keyed by
`(generation_id, server_name)`. A stale task cannot publish an outcome into a
newer generation.

### 6.6 Startup outcome

Each configured server has exactly one terminal outcome in a generation:

- `ready`: connected and catalog published;
- `disabled`: intentionally disabled;
- `needs_auth`: startup requires credentials or authorization;
- `failed_optional`: failed but session startup may continue;
- `failed_required`: failed and the operation must fail closed;
- `cancelled`: explicitly cancelled before a terminal connection outcome.

The public status enum may retain existing names such as `FAILED`; the
generation outcome must still distinguish requiredness and operation identity.

## 7. Functional requirements

### FR-1 — Stable, explicit lifecycle identities

`lauren-ai` and agenthicc must propagate these fields when available:

- `conversation_id`;
- logical `run_id`/turn ID;
- `exchange_id`;
- `tool_call_id` and `attempt_id`;
- lifecycle event type;
- monotonic event/recovery revision;
- a stable idempotency key;
- for MCP, `generation_id` and `server_name`.

The key must not include prompt contents, tool arguments, secrets, or volatile
timestamps. It must be safe to persist and safe to log after normal redaction.

### FR-2 — One canonical repair transition

The shared runner must expose one canonical transition for an interrupted
exchange. All cancellation, provider exception, preflight, and resume paths
must call that transition or observe its already-terminal result.

Calling it again with the same exchange must return the existing terminal
receipt and must not append another synthetic result, journal record, lifecycle
signal, or user-facing notice.

If the exchange is already committed, recovery must be a no-op. If it is
already aborted/repaired, recovery must return the existing receipt. If it is
ambiguous, the runner must fail locally with a typed diagnostic rather than
guessing.

### FR-3 — Separate event emission from transcript projection

`lauren-ai` may emit a lifecycle signal for every internal observer, but the
agenthicc session bridge must project a given idempotency key at most once into
the durable `ConversationStore`/journal.

The projection record must carry:

```json
{
  "kind": "tool_recovery_notice",
  "event_id": "stable-id",
  "exchange_id": "exchange-id",
  "status": "repaired",
  "call_count": 2,
  "rendered": true
}
```

The user-facing text should be concise. Detailed call IDs, exception class,
retryability, and server metadata belong in structured diagnostics and must
remain redacted in the transcript.

### FR-4 — Deduplicate signal handlers and compatibility sinks

The agenthicc turn runner must not register two handlers that both project the
same `ToolExchangeRepaired` signal. If compatibility with older lauren-ai
requires more than one listener, a single shared projector must own the
idempotency set and the listeners must be side-effect free except for sending
the fact to that projector.

The projector must be scoped to the session/conversation and cleared only when
the session is intentionally replaced. It must not be cleared for a provider
retry, workflow phase transition, `continue`, or normal TUI redraw.

### FR-5 — Durable, replay-safe recovery receipts

Journal a versioned recovery receipt before acknowledging the recovery as
visible. The receipt must be idempotent on replay and tolerate a truncated
final JSONL record under the existing journal recovery policy.

The receipt must record bounded metadata:

- event and exchange IDs;
- run/turn ID;
- recovery status;
- expected, completed, and synthesized call counts;
- interrupted step/attempt IDs when known;
- error category, not unbounded exception text;
- journal cursor/revision;
- timestamp for diagnostics only.

Replaying a journal must reconstruct the repaired memory and the fact that the
notice was already projected. It must not append the notice again.

### FR-6 — Preserve completed work and pending interaction

After an interruption:

- all committed tool results remain in memory, journal, checkpoint, and
  transcript;
- unresolved calls receive valid synthetic results only where the existing
  transaction contract requires them;
- a partial provider fragment is stored as interrupted metadata and is never
  sent back as a complete assistant message;
- a pending user question/conflict remains resumable exactly once;
- workflow phase and checkpoint cursors are retained;
- idempotency ledgers prevent replay of completed side effects.

The next `continue`, `--resume`, or workflow resume must use the saved
conversation and workflow cursor rather than constructing a fresh INIT request.

### FR-7 — Recovery notice cardinality

For one logical exchange, the user-visible transcript may contain at most one
recovery notice per session. A later independent retry that produces a new
exchange has a new event ID and may produce a new notice.

If several calls in one exchange are interrupted, the notice must summarize
the exchange once, optionally including a count. It must not print the same
sentence once per tool call.

### FR-8 — Bounded recovery retry behavior

Recovery must not recursively trigger itself. A repair may be attempted once
per exchange revision. If repair fails, emit one typed `recovery_failed` fact,
leave the original state recoverable, and stop before sending malformed memory
to the provider.

Provider retry policy remains responsible for transient transport errors. It
must not retry a local conversation invariant violation, an ambiguous exchange,
or a non-retryable MCP startup failure.

### FR-9 — MCP startup generation and per-server isolation

Every startup/reload operation must:

1. allocate a generation ID;
2. evaluate servers independently and concurrently where safe;
3. publish each server's terminal outcome exactly once;
4. retain healthy catalogs even when another optional server fails;
5. aggregate required failures only after all eligible servers have been
   given their bounded startup opportunity; and
6. prevent tasks from an older generation from mutating the current catalog or
   emitting current-generation diagnostics.

An optional server's failure must not remove tools from healthy servers or
disable built-in tools. A required failure must raise the existing typed
  required-server error after publishing the per-server outcomes.

### FR-10 — MCP diagnostic projection

The session may render one concise notice for an optional server failure in a
generation, for example:

```text
MCP server 'asyncmove' is unavailable; continuing without its tools.
```

The `/mcp` status and `mcp doctor` output must retain the redacted cause,
transport, requiredness, generation, and retry/connect command. The full
exception must not be copied into the scroll transcript by default.

For a required failure, render one actionable error and preserve the failure
for command/API consumers. Do not emit both a generic failure line and a
second identical startup exception line for the same generation.

### FR-11 — MCP retry and reload semantics

Automatic startup retry, if enabled, must be bounded and keyed to the same
generation. A retry attempt may update structured status but must not create a
new user-facing failure notice until the generation reaches its terminal
outcome.

An explicit `/mcp connect NAME` or `/mcp reload` creates a new generation and
may produce a new notice. Reconnecting one server must not replay failures for
unrelated servers.

### FR-12 — Transcript rehydration and scroll appender behavior

When a session transcript is loaded, recovery and MCP diagnostic records must
be treated as already projected if their event IDs are present in the loaded
store. Rendering a transcript must be idempotent: redraw, resize, scroll, and
resume cannot duplicate lines.

The appender may collapse repeated adjacent low-level transport details into a
single expandable diagnostic, but it must not collapse separate exchanges or
separate MCP generations merely because their text matches.

### FR-13 — Headless and structured consumers

Headless mode, logs, session export, and event subscribers must receive the
structured event with identity and status. They must not depend on parsing the
human-readable transcript line.

The event schema must be versioned and additive so existing consumers that only
read `kind`/`message` continue to work.

### FR-14 — Compatibility with older lauren-ai

At startup, agenthicc must feature-detect the required lifecycle/transaction
contract. If the installed lauren-ai lacks stable exchange identity or
idempotent repair support, agenthicc must either use a documented compatibility
adapter with local idempotency or fail with an actionable version error.

It must never silently enable the old multi-projection behavior while claiming
that recovery is deduplicated.

### FR-15 — No secret or content leakage

Event IDs and diagnostics must not contain API keys, authorization headers,
tool arguments, file contents, prompt text, or full command output. Server URLs
must use the existing redaction rules. Recovery metadata must be bounded before
being written to the journal or transcript.

## 8. Proposed implementation

### 8.1 Canonical recovery receipt in lauren-ai

Extend the shared tool transaction contract with a terminal recovery receipt
or equivalent typed value. The receipt should be created by the memory/runner
owner, not by a TUI listener. It should support:

```python
receipt = memory.recover_tool_exchange(exchange_id, reason=reason)
assert receipt.event_id
assert receipt.status in {"repaired", "already_repaired", "failed"}
```

The exact API name may follow lauren-ai conventions, but it must provide:

- atomic transition;
- idempotent repeated invocation;
- stable event identity;
- durable commit hook;
- bounded metadata;
- a typed failure when the state is ambiguous.

All `ToolExchangeRepaired` emissions in the runner should be routed through a
single helper that checks whether the same receipt was already emitted for the
current run. The multiple preflight and exception sites may remain as safety
nets, but they must observe the same receipt rather than independently create
events.

### 8.2 Shared session projector in agenthicc

Introduce one small projector at the session boundary, near the existing
conversation/event bridge. Its responsibilities are:

1. receive typed lifecycle events;
2. derive or read the stable idempotency key;
3. atomically consult the session journal/store;
4. append the structured event only if absent;
5. append at most one concise transcript projection; and
6. return whether it changed the projection.

`AgentTurnRunner`, compatibility event sinks, workflow resume, and headless
execution must call this projector rather than append the generic system text
directly. The projector must be usable without Rich/TUI dependencies.

### 8.3 MCP startup coordinator

Add a generation-aware startup result around `McpSessionManager` or its owning
session startup coordinator. Keep the existing bridge, status, catalog, and
required-server concepts. Add only the missing lifecycle identity and outcome
aggregation.

The coordinator must ensure that:

- optional failures are stored and projected once;
- healthy catalogs are installed independently;
- required errors are aggregated after all startup tasks finish;
- cancellation of one server does not cancel unrelated optional servers;
- late tasks check generation before publishing state; and
- reload replaces the old generation atomically from the session's point of
  view.

### 8.4 Event-to-transcript data flow

```text
tool/provider interruption
  -> lauren-ai canonical exchange recovery
  -> durable recovery receipt(event_id, exchange_id, revision)
  -> ordered lifecycle signal(s)
  -> agenthicc recovery projector
       ├─ event ID already projected? return no-op
       └─ otherwise append structured journal/event record
            ├─ provider memory remains valid
            ├─ workflow/checkpoint state remains attached
            └─ append one concise TUI/headless projection

MCP startup generation G
  -> start server A, B, C independently
  -> each produces one (G, server) terminal outcome
  -> catalog publishes ready servers immediately/atomically
  -> projector emits one optional/required diagnostic per failed outcome
  -> aggregate required failures after all tasks settle
```

### 8.5 Resume data flow

```text
session restart / continue / --resume
  -> load journal and checkpoint
  -> fold recovery receipts and completed tool results
  -> seed projector's seen-event index
  -> validate tool exchange
  -> restore workflow phase and pending question/conflict
  -> execute only the unresolved safe continuation
  -> do not append an old recovery notice again
```

## 9. Acceptance criteria

### AC-1 — One repair notice for one interrupted exchange

Given one assistant exchange with multiple tool calls, interrupt execution
after one call completes, and cause recovery to be observed by the runner,
preflight, resume path, and two event sinks. The journal contains one recovery
receipt and the TUI transcript contains one recovery notice.

### AC-2 — Completed work is preserved

The completed `Run` result remains in memory, the journal, the session
transcript, and the next provider request. The tool is not run again solely
because recovery was observed twice.

### AC-3 — Pending clarification remains actionable

When the model has produced a real conflict/clarification request before the
turn is interrupted, resuming the session presents the pending interaction or
continues from its saved state exactly once. Recovery text does not replace or
duplicate the question.

### AC-4 — Recovery is idempotent on replay

Loading the same journal twice, resizing the TUI, scrolling away and back, or
calling `continue` repeatedly does not add another notice for the same event
ID.

### AC-5 — Repair failure is bounded and truthful

If an ambiguous exchange cannot be repaired, the provider is not called with
invalid memory. One typed recovery failure is emitted; the session remains
available for an explicit user-directed recovery action and does not loop.

### AC-6 — Optional MCP startup failure is isolated

Configure one healthy server and one failing optional server such as
`asyncmove`. The healthy server's tools become available, built-in tools remain
available, the session starts, and exactly one concise optional-server notice
is projected for that startup generation.

### AC-7 — Required MCP failure remains fail-closed

Configure a failing `required = true` server and a healthy optional server.
Both outcomes are recorded, healthy server diagnostics remain inspectable, and
startup returns the existing typed required-server error once. No unbounded
retry or duplicate error storm occurs.

### AC-8 — Stale MCP task cannot overwrite a newer generation

Start generation G, begin reload G+1, and complete a delayed G task after G+1
has published. G's late outcome does not replace G+1 status/catalog or create a
new user-facing notice.

### AC-9 — Explicit reconnect is independently visible

After an optional server failed in G, `/mcp connect NAME` or `/mcp reload`
creates G+1. A new failure in G+1 may produce one new notice, while unrelated
server failures from G are not replayed.

### AC-10 — Structured and human projections agree

The event stream, session export, headless output, `/mcp` status, and TUI all
identify the same server/exchange and terminal outcome, while the human text
remains concise and redacted.

### AC-11 — Existing durable state remains compatible

Old journals and checkpoints without event IDs fold successfully. They receive
deterministic compatibility identities where possible, and no existing
completed tool results, workflow phase, or approval state is lost.

### AC-12 — No regression in ordinary turns

A normal successful tool exchange produces no recovery notice. Independent
tool failures remain visible. Transient provider retries still follow the
configured retry policy and do not create one recovery notice per retry.

### AC-13 — No secret leakage

Tests prove that API keys, authorization values, tool arguments, file contents,
and unbounded provider/MCP exception payloads are absent from recovery
journal records, transcript projections, and standard logs.

## 10. Testing strategy

### 10.1 Unit tests

Add deterministic tests for:

- stable identity/idempotency-key construction;
- canonical exchange recovery from every terminal and non-terminal state;
- repeated recovery returning the same receipt;
- duplicate/late lifecycle signal handling;
- journal folding of recovery receipts and truncated final records;
- projection deduplication across multiple listeners;
- bounded diagnostic redaction and truncation;
- transcript rehydration and render-once behavior;
- MCP generation allocation and stale-generation rejection;
- optional/required outcome classification;
- required failure aggregation;
- cancellation and late-task cleanup;
- retry notices remaining attempt-scoped rather than recovery-scoped.

### 10.2 Integration tests

Use fake transports, fake tools, fake event sinks, an in-memory or temporary
journal, and fake MCP bridges. Cover:

1. stream interruption after zero, one, and all parallel tool completions;
2. provider failure after a valid command result and before the next result;
3. cancellation while approval is pending;
4. `continue`, `--resume`, and workflow resume after recovery;
5. duplicate signals delivered through the signal handler and compatibility
   sink;
6. session restart and journal rehydration;
7. one healthy and one failing optional MCP server;
8. required failure alongside healthy optional servers;
9. reload with delayed stale bridge tasks;
10. `/mcp connect`, `/mcp refresh`, `/mcp reload`, and shutdown;
11. headless structured event output;
12. transcript export and TUI event projection.

### 10.3 End-to-end tests

Add at least these user journeys:

- Start an agent turn, run a command, interrupt the next tool exchange, then
  resume. The command result remains visible and the recovery notice appears
  once.
- Let the agent reach a genuine specification conflict, interrupt the turn,
  restart agenthicc, and answer the preserved question without restarting the
  whole turn.
- Launch with a healthy filesystem MCP server and an unavailable optional
  `asyncmove` server. Verify startup completes, healthy tools are callable,
  and one optional failure is shown.
- Launch with a required unavailable MCP server. Verify one actionable failure,
  no infinite startup loop, and correct `mcp doctor`/status output.
- Reload MCP while one server is delayed. Verify old results cannot overwrite
  the new catalog and repeated reload/redraw does not duplicate diagnostics.

Tests must use local fake servers/processes or deterministic bridge doubles;
they must not depend on external MCP services, provider availability, network
timing, or real credentials.

## 11. Non-functional requirements

### NFR-1 — Durability

Recovery receipts and MCP generation outcomes must survive process restart
according to the existing journal/checkpoint durability contract. A crash while
writing the final record must not corrupt earlier records.

### NFR-2 — Idempotency

Replaying signals, journal records, startup tasks, retries, and UI redraws must
not duplicate side effects or user-facing projections.

### NFR-3 — Responsiveness

Optional MCP failures must not wait for unrelated servers beyond existing
bounded startup policy. Recovery projection must not block the agent loop on
Rich rendering or a slow UI consumer.

### NFR-4 — Provider and transport neutrality

The tool recovery contract must work for Anthropic, OpenAI-compatible, Ollama,
and future transports. MCP generation handling must work for stdio, streamable
HTTP, SSE, and websocket-compatible bridges where supported.

### NFR-5 — Security and privacy

Only redacted, bounded metadata is persisted or rendered. Event IDs must be
non-sensitive. Existing workspace, network, and MCP credential policies remain
unchanged.

### NFR-6 — Observability

Operators can determine whether a message was a retry, repair, projection
deduplication, optional MCP failure, required MCP failure, or stale task. Logs
must include stable IDs and categories, not secrets or full content.

### NFR-7 — Backward compatibility

Existing event consumers, old journal records, current `/mcp` commands, and
configured server semantics continue to work. New fields are additive unless
a versioned schema migration is required.

## 12. Rollout and migration

1. Add event schema/version and receipt folding in a backwards-compatible
   reader before enabling the new projector.
2. Add lauren-ai canonical repair identity and tests.
3. Add the agenthicc projector and route every recovery projection through it.
4. Add MCP generation/outcome isolation behind deterministic tests.
5. Enable the behavior by default after unit, integration, and E2E coverage is
   green.
6. Keep a temporary debug metric for suppressed duplicate signals so production
   evidence can confirm that deduplication is fixing amplification rather than
   hiding independent failures.
7. Document the event schema and recovery behavior in the MCP and architecture
   guides if the implementation changes public status or persistence behavior.

No destructive migration is permitted. Old sessions must be readable, and
their first resumed projection must not delete completed tool results or
workflow state.

## 13. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Deduplicating two real failures with identical text | Key by exchange/generation identity, never by text alone |
| Projecting before durable commit | Persist receipt before marking it rendered; replay from receipt |
| Stale MCP task mutates a new catalog | Check generation under the manager's lifecycle lock |
| Multiple lauren-ai versions expose different signals | Feature-detect and use one compatibility projector |
| Recovery notice hides a real pending question | Keep interaction state separate from diagnostic projection and test resume |
| New journal records exceed storage budget | Bound fields and retain only category/count/IDs |
| Required MCP server silently becomes optional | Preserve `required` in both status and outcome aggregation; fail closed |
| UI renderer becomes a persistence owner | Keep projection in session/event layer; renderer only renders events |

## 14. Open implementation decisions

The implementation plan must settle and document:

1. whether the stable recovery ID is generated in lauren-ai or derived by
   agenthicc from a canonical receipt;
2. the exact journal record names and schema version;
3. whether the public MCP status enum gains `FAILED_OPTIONAL` and
   `FAILED_REQUIRED`, or whether requiredness stays in a separate outcome;
4. the atomicity boundary between appending a structured event and marking the
   transcript projection rendered;
5. the minimum lauren-ai version required for native exchange receipts; and
6. how much historical compatibility identity can be inferred for old records
   that have no exchange or generation ID.

These decisions must be made from the current `lauren-ai` and agenthicc
contracts, recorded in code comments and tests, and must not be resolved by
silently resetting a session.

## 15. Implementation notes

The implementation is split across the shared provider runner and the
agenthicc session boundary:

- `lauren-ai` derives stable exchange event IDs, adds them to repair/abort
  lifecycle signals, and suppresses duplicate repair/abort emissions within a
  runner instance.
- agenthicc persists the stable repair ID in the conversation journal,
  projects recovery through `ToolRecoveryProjector`, and deduplicates event IDs
  in `ConversationStore` and transcript replay.
- `McpSessionManager` assigns startup generations, includes generation/event IDs
  in status and lifecycle payloads, publishes optional failures independently,
  aggregates required failures, and rejects stale startup completions.
- the scroll appender renders the structured `tool_recovery` event once,
  without exposing provider payloads or tool arguments.

The implementation remains compatible with pre-PRD-191 lauren-ai signal
schemas and conversation-store doubles: agenthicc derives the ID locally and
falls back to the two-argument append contract when the event-ID parameter is
unavailable.

## 16. Definition of done

- The implementation satisfies FR-1 through FR-15 and AC-1 through AC-13.
- `lauren-ai` and agenthicc share one canonical, idempotent tool-recovery
  transition.
- Recovery signals can be observed by multiple internal paths without
  duplicating journal records or transcript lines.
- Completed work, pending questions, workflow phase, and checkpoint state are
  preserved across interruption and resume.
- MCP startup/reload isolates optional server failures, retains healthy tools,
  and fails closed once for required failures.
- Stale MCP lifecycle tasks cannot publish into a newer generation.
- Unit, integration, and E2E tests cover interruption points, replay,
  redaction, optional/required MCP failures, reload, and resume.
- Relevant documentation and the PRD index are updated.
- Focused lint, type, and test checks pass; any unrelated repository baseline
  failures are reported separately rather than masked.
