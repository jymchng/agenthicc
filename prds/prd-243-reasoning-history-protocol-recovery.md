---
title: "PRD-243: Protocol-Safe Reasoning History and Deterministic Recovery"
status: Proposed
version: 1.0.0
date: 2026-10-17
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-190, prd-195, prd-198, prd-242]
tags: [reasoning-content, provider-compatibility, retries, compaction, recovery]
---

# PRD-243 — Protocol-Safe Reasoning History and Deterministic Recovery

## 1. Executive summary

An OpenAI-compatible provider rejected a request with HTTP 400:

```text
The `reasoning_content` in the thinking mode must be passed back to the API.
```

Agenthicc then showed `Provider request rejected retry N/5` repeatedly for the
same error. Investigation confirms two separate defects:

1. **The repeated retries are caused by Agenthicc's broad permanent-4xx retry
   policy.** The error is a deterministic conversation-protocol rejection, but
   the retry classifier categorizes nearly every provider HTTP 400 as eligible
   for the separate `irrecoverable_error_max_retries` budget. That setting
   defaults to five retries after the initial request. The same invalid
   conversation is therefore sent up to six times, with no changed payload.
2. **The request history lacked a provider-required assistant field.** Current
   lauren-ai 1.6.0 captures, stores, and serializes `reasoning_content` in the
   ordinary response → memory → tool-result follow-up path. However, Agenthicc
   can still produce a history without it: legacy sessions may predate that
   contract, and Agenthicc compaction replaces history with a synthetic
   assistant acknowledgement while omitting provider reasoning metadata.

The supplied transcript does not include the actual serialized failed request,
the affected journal, or a compaction event. It proves the provider rejected
the request and that Agenthicc kept retrying; it does **not** prove whether this
particular missing field came from compaction, a legacy journal, another
history transformation, or an incompatible runtime. The implementation must
diagnose that distinction safely instead of guessing.

This PRD defines a complete fix across lauren-ai and Agenthicc: typed
conversation-protocol errors, immediate no-retry handling for this deterministic
400, preservation and validation of reasoning-bearing history through every
transformation, and a durable recovery path that keeps useful work and workflow
checkpoints intact without fabricating hidden reasoning.

## 2. Incident and impact

The observed sequence was:

```text
Agent receives a successful tool result
        ↓
Agenthicc sends the next model request
        ↓
Provider rejects assistant history: reasoning_content missing (HTTP 400)
        ↓
Agenthicc schedules permanent-error retries, attempts 2/5 through 5/5
        ↓
Every retry sends the same conversation and receives the same rejection
```

The tool command and its output are not themselves the failure. The error
occurs when the next model request is validated. Repeated attempts waste time,
provider quota, and user attention, while obscuring the actionable fact that
the serialized conversation is incompatible with the selected endpoint.

## 3. Evidence-based investigation

### 3.1 Confirmed cause of the retry loop

The current retry path is:

```text
AgentTurnRunner._stream_with_retry()
  → run_with_transport_retry()
  → _is_irrecoverable_provider_error()
  → on_irrecoverable_retry()
  → provider_recovery_retry TUI event
```

`_is_irrecoverable_provider_error()` treats provider HTTP 4xx errors as
eligible for the irrecoverable retry budget, except HTTP 429 and the specific
OpenCode Go `MissingSessionID` case. A `TransportError(status_code=400)` whose
message is the reported missing-reasoning error currently returns `True` from
that classifier. This was verified against the current source.

`ExecutionSettings.irrecoverable_error_max_retries` defaults to `5`. The
shared retry helper interprets this as retries **after** the initial attempt.
The behavior is consistent with the observed `retry 2/5` through `retry 5/5`
notices. PRD-198 intentionally made unrelated provider 4xx errors eligible for
a bounded retry budget, but it did not carve out deterministic conversation
protocol errors. The TUI's “non-retryable” wording in lauren-ai's
`_actionable_provider_message()` does not control Agenthicc's retry decision:
lauren-ai still wraps the response as a generic `TransportError` with status
400, and Agenthicc classifies by status.

### 3.2 Provider meaning of the error

The provider response explicitly states that in thinking mode the preceding
assistant `reasoning_content` must be passed back. This is a request-history
contract failure, not a network outage, rate limit, unavailable service,
unsupported-model retry, or failed tool execution. Replaying the unchanged
request cannot add the missing field.

### 3.3 The normal round trip is implemented, but is not sufficient

The current runtime dependency is lauren-ai 1.6.0. Its OpenAI-compatible
transport and memory support `reasoning_content`; Agenthicc contains tests for
response capture, assistant-memory storage, wire serialization, tool-result
follow-up, journal persistence, and resume. This means “upgrade lauren-ai” is
not a complete diagnosis or fix for the observed failure in the current tree.

Those tests cover an unmodified reasoning-bearing message. They do not prove
that every Agenthicc operation which projects or compacts conversation history
preserves the provider's protocol requirements.

### 3.4 A confirmed history-loss path exists in Agenthicc compaction

`src/agenthicc/memory/compactor.py` formats the transcript from role/content,
then replaces the history with a synthetic pair equivalent to:

```text
user: [COMPACT SUMMARY] ...
assistant: Understood. Continuing from the summary.
```

The replaced history's `reasoning_content` is not included in the summary or
the synthetic assistant message. Thus after compaction the next OpenAI-
compatible request can contain assistant history without the exact provider
field. Whether every gateway requires it after such a compaction depends on
that gateway's conversation contract; Agenthicc currently records no capability
or provenance to decide safely.

### 3.5 Other possible sources are not distinguishable from this transcript

The same provider response can result from:

- a journal written by a runtime that discarded `reasoning_content`;
- a previous reasoning response that did not contain the field, followed by an
  endpoint/model that now requires it;
- an Agenthicc compaction or other projection that removed the field;
- an alternate provider/profile transition with different replay rules; or
- a third-party OpenAI-compatible adapter that does not return or serialize
  this extension as expected.

The pasted report contains no provider request payload, journal, response
metadata, or compaction status. The fix must make these conditions observable
using redacted metadata and must not claim which one happened in this specific
run without that evidence.

## 4. Goals

1. Never retry the exact missing-`reasoning_content` HTTP 400 through either
   the transient or irrecoverable retry budget.
2. Identify the error as a conversation-protocol/history incompatibility, not
   as generic provider failure.
3. Preserve exact reasoning metadata for assistant messages retained in any
   request, including streaming, tool calls, snapshots, journal replay,
   checkpoints, retries, and resume.
4. Make compaction and other history transforms preserve protocol-valid
   conversation structure, or fail before sending a known-invalid history.
5. Preserve completed tool effects, accepted user input, the current workflow
   phase, and resumable checkpoints when repair is impossible.
6. Give the user safe, accurate recovery choices without fabricating or
   exposing hidden reasoning.
7. Diagnose the origin of the missing field with bounded, payload-free
   metadata.

## 5. Non-goals

- Fabricating, reconstructing, or paraphrasing missing provider reasoning.
- Making an HTTP 400 retryable by increasing retry counts or delays.
- Requiring all providers or models to use the OpenAI `reasoning_content`
  extension.
- Persisting or rendering hidden reasoning in ordinary TUI output, metrics,
  logs, or error messages.
- Retaining every historical reasoning payload forever when a provider's
  protocol permits safely removing complete old exchanges.
- Replacing PRD-190's transport round trip or PRD-198's retry policy for
  unrelated provider errors.
- Automatically deleting a session, restarting a workflow from INIT, or
  switching provider profiles without user intent.

## 6. Proposed solution

### 6.1 Typed protocol failure across the library boundary

lauren-ai MUST expose a typed, stable provider-conversation error for the
recognized condition, with a machine-readable code such as
`reasoning_replay_required`. It should carry only bounded metadata:

```text
error_code = reasoning_replay_required
http_status = 400
provider = openai-compatible provider identifier
retryable = false
```

The OpenAI-compatible adapter may recognize the provider's structured error
code/type where available and a narrowly scoped, tested message signature when
the gateway supplies only text. It must not put raw request bodies, API keys,
or hidden reasoning in the new error. The original SDK exception may remain
chained for debug inspection, subject to existing redaction rules.

Agenthicc MUST classify this stable error code before generic status-based
classification. Avoid spreading string matching across retry, TUI, worker, and
workflow code. If a runtime does not yet expose the typed error, a temporary
central compatibility adapter may inspect the bounded exception chain, with
tests proving that it recognizes only the provider's specific reasoning
contract failure.

### 6.2 Retry-policy correction

For `reasoning_replay_required`:

- make one provider attempt only;
- do not consume `transport_max_retries`;
- do not consume `irrecoverable_error_max_retries`;
- emit no `provider_recovery_retry` event; and
- preserve the configured bounded retry behavior for other provider 4xx
  failures unless separately reclassified by another PRD.

The notification MUST say that the request was not retried because the
conversation history is incompatible. It must not display `retry 1/5`, a
transient/network label, or a claim that waiting will resolve the error.

### 6.3 Protocol capability and message provenance

The runtime needs a way to know when a provider conversation requires exact
reasoning replay. Establish a durable, session-scoped protocol state keyed to
the effective provider/profile/model/endpoint identity (with secrets excluded):

- mark the conversation as reasoning-replay-sensitive when the provider
  returns a `reasoning_content` field or the selected profile explicitly
  declares that replay is required;
- preserve the distinction between a missing field (`None`) and a present but
  empty string (`""`);
- associate reasoning metadata only with its originating assistant response;
- persist only the protocol flag and bounded provenance needed to detect
  transformations; do not duplicate reasoning in status records; and
- retain compatibility with older journals by treating absent provenance as
  unknown, not as proof that a field was not required.

The precise storage shape is an implementation decision. It may use versioned
journal metadata or a typed message envelope, but internal provenance MUST NOT
be serialized as an unrecognized field to the provider.

### 6.4 History transformation contract

Every operation that prepares a provider request MUST satisfy one of these
conditions:

1. Every assistant response retained in the request carries its exact original
   `reasoning_content` when that field was present and replay is required; or
2. A complete old exchange is removed only when the provider protocol allows
   removal of that exchange and tool-call/result integrity remains valid; or
3. The runtime detects that the history cannot be transformed safely and
   pauses before making the provider request with a typed
   `reasoning_history_unrecoverable` outcome.

This contract applies to `ShortTermMemory.messages()` and trimming,
`trim_to_fit`, Agenthicc automatic and manual compaction, local compaction
fallback, retry snapshots, workflow prompt preparation, journal reset/replay,
checkpoint serialization, and session resume.

Compaction MUST NOT create a synthetic `assistant` acknowledgement that is
mistaken for a provider-generated assistant response in a replay-sensitive
history. The summary should be represented in a provider-valid non-assistant
context location or in an explicit memory/context layer. The implementation
must validate role ordering and tool-call/result pairing after transformation.
It may discard complete resolved old exchanges only under a documented safe
provider policy. If preserving required reasoning makes the request too large,
do not truncate or fabricate it: preserve the durable history and report an
actionable context/recovery condition.

### 6.5 Preflight validation and diagnostic evidence

Before dispatching a request for a replay-sensitive conversation, validate the
selected history projection against its provenance. Detect:

- provider-originated assistant messages known to require reasoning whose
  field is now absent;
- provider reasoning fields omitted or altered by a history transform;
- synthetic assistant messages in a position that would be sent as if they
  were provider output; and
- a runtime/package missing the required transport capability.

On detection, fail locally before paying for another rejected request. Persist
a compact diagnostic record containing only:

```text
code, provider/profile/model identifiers, session/turn identifiers,
source stage (legacy journal, compaction, serialization, or unknown),
message ordinal or opaque message ID, retry_count=0
```

Do not persist the actual reasoning value, prompt, authorization headers, raw
HTTP body, or full provider exception. If the source cannot be proven, report
`unknown`; do not infer “compaction” merely because compaction is supported.

### 6.6 Recovery and workflow behavior

When the provider reports this error and local history cannot be repaired:

- leave the conversation journal and committed tool ledger intact;
- keep the accepted user turn recoverable, without resubmitting it
  automatically;
- preserve the exact workflow checkpoint and phase; do not restart at INIT;
- surface a dedicated category such as
  `reasoning_history_incompatible` / `provider_conversation_protocol`; and
- offer explicit choices: inspect/export the existing session, start a new
  conversation, or select a provider/model whose documented protocol accepts
  the existing history.

If an intact pre-compaction snapshot is available, restoring it may be offered
as an explicit recovery action after validation. Never overwrite the only
durable copy or assume that the restored history fits the model window.

Continuing with the same provider/profile and unchanged history MUST NOT
silently re-run the rejected request. A retry is allowed only after a real
state change that can address the incompatibility, such as explicit provider
selection, a validated history restore, or a user-approved new conversation.

### 6.7 Privacy and user-facing messaging

The visible error should be concise, for example:

```text
This provider requires reasoning_content from an earlier assistant response,
but the selected session history does not contain it. The request was not
retried. The missing value cannot be reconstructed. Choose another compatible
provider or start a new conversation; the existing session is preserved.
```

Exact wording may vary. It MUST:

- distinguish a history/protocol error from network failure;
- state clearly that the provider request was not retried;
- identify that session data remains preserved;
- offer only recovery paths that are actually supported; and
- never include hidden reasoning text or secrets.

## 7. Data flow

### Current failure path

```text
assistant response (reasoning_content may be captured)
        │
        ├─ normal memory/journal path: field can be preserved
        └─ compaction/legacy/other transform: field may be absent
                │
                ▼
OpenAI-compatible request serializes assistant history
        │
        ▼
provider HTTP 400: reasoning_content required
        │
        ▼
generic TransportError(status=400)
        │
        ▼
Agenthicc permanent-4xx policy grants 5 retries
        │
        └─ identical history re-sent repeatedly
```

### Desired flow

```text
provider response + protocol metadata
        │
        ▼
exact reasoning field durably associated with assistant response
        │
        ├─ faithful request projection ─────────────► provider
        │
        └─ transform cannot preserve protocol
               │
               ├─ preflight detects it → no request, durable recovery state
               └─ provider detects it → typed 400, no retry budget, preserve state
```

## 8. Acceptance criteria

### Error and retry behavior

| ID | Criterion |
|---|---|
| 243.1 | The reported provider error is represented by a stable typed/code-based protocol classification, not only a generic HTTP 400. |
| 243.2 | A fake provider returning the reported 400 causes exactly one request, regardless of transient and irrecoverable retry settings. |
| 243.3 | No `network_retry` or `provider_recovery_retry` event is emitted for this error. |
| 243.4 | The TUI and background-session projections classify it as a non-retryable conversation-history error, not a network failure. |
| 243.5 | The separate retry behavior for unrelated 400/401/403/model errors remains as documented by PRD-198. |
| 243.6 | The user can identify that unchanged retry/continue is not useful and that the session was preserved. |

### Payload integrity and compaction

| ID | Criterion |
|---|---|
| 243.7 | Exact non-streaming and streaming `reasoning_content` survives memory insertion and request serialization. |
| 243.8 | Missing and present-empty reasoning values remain distinguishable. |
| 243.9 | Tool-call IDs, exact reasoning content, and matching tool results survive retries, journal reopen, checkpoints, `--continue`, and `--resume`. |
| 243.10 | All memory trimming/compaction/fallback paths either retain exact required metadata or remove a whole exchange only under an explicit valid policy. |
| 243.11 | Compaction does not inject synthetic assistant acknowledgements into replay-sensitive provider history. |
| 243.12 | An irreducibly large reasoning-sensitive history is preserved and blocked before dispatch rather than truncating/fabricating reasoning. |
| 243.13 | Legacy journals with missing protocol provenance are treated as unknown and are not silently rewritten as healthy. |
| 243.14 | A fake gateway that enforces reasoning replay accepts normal follow-up and resume after every supported history transformation. |

### Recovery, workflow safety, and privacy

| ID | Criterion |
|---|---|
| 243.15 | An unrecoverable history rejection preserves the original journal and completed tool ledger. |
| 243.16 | Accepted direct-turn input remains recoverable without automatic duplicate execution. |
| 243.17 | Workflow runs retain their exact workflow ID, active phase, and checkpoint; no path restarts the workflow at INIT. |
| 243.18 | Recovery offers a new conversation, an explicitly selected compatible provider, or an available intact snapshot; it never fabricates reasoning. |
| 243.19 | Diagnostics identify a proven source stage or say `unknown`; they never claim compaction or legacy origin without evidence. |
| 243.20 | Reasoning text, authorization, raw requests, and response bodies are absent from normal UI, durable diagnostic metadata, logs, and metrics. |
| 243.21 | Existing non-reasoning OpenAI-compatible and Anthropic conversations retain their current request shape and behavior. |

## 9. Test plan

### 9.1 Unit tests

- Parse structured and text-only provider variants of the missing-reasoning
  400 into one typed category; reject near-match ordinary 400 messages.
- Prove this category bypasses both retry budgets and emits no retry callback.
- Verify unrelated provider 4xx errors still follow PRD-198's explicit
  separate budget.
- Round-trip `None`, empty string, multiline, and exact reasoning values.
- Test each memory transform, including `messages()`, trim, summary compaction,
  local fallback, retry restore, and synthetic message handling.
- Assert transform validation never leaks reasoning into diagnostics.
- Test history provenance migration for old journal records and unknown origin.

### 9.2 Integration tests

With a temporary journal and a fake OpenAI-compatible provider:

1. Return an assistant tool call with `reasoning_content`.
2. Commit the exact tool result and assert the next request carries the same
   reasoning bytes and call/result IDs.
3. Run supported compaction and resume transformations; assert the fake
   provider accepts the resulting history or Agenthicc blocks before calling
   the provider with a typed recoverable outcome.
4. Simulate a legacy journal and assert one targeted failure, no retry, and no
   journal mutation.
5. Simulate the specific provider HTTP 400 and assert one provider call,
   preserved history, and a diagnostic that contains no hidden reasoning.

### 9.3 End-to-end tests

Cover direct, workflow, headless, TUI, and detached-session flows. Include a
workflow that has advanced beyond INIT before the rejection; after restart or
resume, prove its checkpoint phase is unchanged. Prove that explicit provider
change/new-session recovery works while the same-provider unchanged retry is
blocked. All tests use deterministic local fake transports; live provider
smokes are opt-in.

### 9.4 Fault and compatibility matrix

Test supported lauren-ai releases, stale runtime capability, current and
legacy journal schema, compaction on/off, streaming/non-streaming response,
tool/no-tool assistant response, direct/workflow execution, transient retry
before a valid request, and deterministic protocol rejection after a valid
response.

## 10. Documentation and rollout

- Update PRD-190/195 implementation references only where necessary; this PRD
  owns the newly discovered post-round-trip transformation and retry gap.
- Update provider troubleshooting docs with the exact distinction between a
  transient 429/5xx and a non-retryable reasoning-history 400.
- Document when a new session or alternate provider is required and that
  upgrading alone cannot recreate historical reasoning already discarded.
- Roll out the typed error and retry bypass before enabling stricter
  preflight. This immediately stops repeated invalid requests while preserving
  diagnosability.
- Enable transform validation for new sessions, then add backward-compatible
  unknown-provenance handling for existing journals.
- Never migrate a journal by inventing a reasoning value or discarding its
  existing records.

## 11. Risks and decisions

| Risk | Decision / mitigation |
|---|---|
| Provider error wording varies | Prefer provider structured error codes; maintain one narrowly tested text classifier at the adapter boundary. |
| Generic permanent-4xx retry is useful for other errors | Add a specific no-retry class; do not globally set `irrecoverable_error_max_retries=0` in this PRD. |
| Provider replay semantics differ | Scope capability by provider/profile/model; do not assume all OpenAI-compatible endpoints require this field. |
| Compaction cannot retain unbounded reasoning | Drop only complete exchanges when the selected provider contract allows it; otherwise block safely and preserve the journal. |
| Old sessions have permanently lossy history | Preserve them and offer explicit alternate-provider/new-session recovery; never fabricate data. |
| Hidden reasoning is sensitive | Keep exact data only in existing protected conversation storage and provider payloads; diagnostics carry metadata only. |
| Error source cannot be proven after the fact | Report `unknown`; add forward-looking provenance and redacted stage metadata. |

## 12. Definition of done

The PRD is complete when the exact HTTP 400 no longer triggers retries, every
request projection is protocol-safe or stopped before dispatch, compaction and
resume preserve provider-required reasoning without inventing it, irreparable
sessions remain intact and recoverable, workflow checkpoints do not rewind,
diagnostics are useful but do not reveal hidden reasoning, and unit,
integration, end-to-end, privacy, and compatibility tests pass across
Agenthicc and the required lauren-ai boundary.
