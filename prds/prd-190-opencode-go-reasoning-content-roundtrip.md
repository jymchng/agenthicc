---
title: "PRD-190: OpenAI-compatible reasoning_content round-trip"
status: Implemented
version: 1.0.0
created: 2026-09-14
scope: "lauren-ai OpenAI-compatible transport, conversation memory, durable replay, and agenthicc gateway integration"
related_prds:
  - PRD-126 # transport retry
  - PRD-129 # conversation durability and retry resilience
  - PRD-137 # Anthropic extended-thinking block round-trip
  - PRD-169 # transaction-safe provider conversations
  - PRD-182 # durable mid-turn preservation
  - PRD-187 # OpenCode Go session identity propagation
tags:
  - providers
  - openai-compatible
  - opencode-go
  - reasoning
  - tool-calls
  - conversation-memory
  - lauren-ai
  - durability
---

# PRD-190 — OpenAI-compatible `reasoning_content` round-trip

## 1. Executive summary

An agenthicc session using an OpenCode Go or another OpenAI-compatible gateway
can fail immediately after a successful tool call with:

```text
TransportError: Error code: 400 - {
  'error': {
    'type': 'MissingSessionID',
    'message': '...'
  }
}
```

or, for the failure covered by this PRD:

```text
The `reasoning_content` in the thinking mode must be passed back to the API.
```

The second error is not a transient transport failure, a missing API key, or
an agenthicc workflow-transition error. It is a conversation serialization
failure. A reasoning model returns an assistant message containing a
provider-specific `reasoning_content` field and often a tool call. The current
OpenAI-compatible path parses the visible text and tool calls, and counts
reasoning tokens, but does not retain the actual `reasoning_content`. After the
tool result is committed, the next request replays the assistant tool call
without the reasoning content that the gateway requires. Console Go rejects
that request with HTTP 400.

This PRD defines a provider-neutral memory contract with an OpenAI-compatible
`reasoning_content` extension. The implementation must capture the field in
both streamed and non-streamed responses, attach it to the exact assistant
turn that produced the tool calls, preserve it through memory snapshots,
journals, compaction boundaries, retry recovery, and resume, and emit it only
when serializing messages for an OpenAI-compatible provider. The value must be
passed back verbatim; it must not be regenerated, summarized, truncated, or
converted into user-visible assistant text.

The existing Anthropic `thinking`/`redacted_thinking` implementation in
PRD-137 remains correct and independent. This PRD extends the same fidelity
principle to a different provider wire format. The OpenCode Go
`x-opencode-session` header covered by PRD-187 is also orthogonal: the same
request may require both a stable session header and a faithful reasoning
payload.

## 2. Evidence-backed current state

### 2.1 Reproduction flow

The reported failure occurs after the workflow has already made useful
progress:

```text
user request
    │
    ▼
assistant response:
  reasoning_content = R
  tool_calls = [Goal Implemented(...)]
    │
    ▼
agent executes the tool and appends its result
    │
    ▼
next provider request replays the conversation
    │
    └── assistant tool-call is present, but reasoning_content = R is absent
            │
            ▼
        Console Go returns HTTP 400
```

The visible tool result can make the failure look as if the tool or workflow
transition caused it. The transition has already completed locally. The
failure is raised while constructing or validating the *next* provider call.

### 2.2 Current lauren-ai gaps

The current implementation in the lauren-ai checkout has these relevant
contracts:

| Layer | Current behavior | Gap |
|---|---|---|
| OpenAI non-stream response parsing (`_response_to_completion`) | Reads `message.content` and `message.tool_calls`; reads `completion_tokens_details.reasoning_tokens` for accounting | Does not read `message.reasoning_content` |
| OpenAI streaming (`_stream`) | Emits text deltas and tool-call deltas | Does not accumulate or emit reasoning-content deltas |
| Canonical `Completion` | Has Anthropic `thinking_blocks` | Has no field for OpenAI-compatible reasoning content |
| `ShortTermMemory.add_assistant` | Stores text, tool-use blocks, and Anthropic thinking blocks | Drops an OpenAI-compatible reasoning field |
| OpenAI message serializer (`_message_to_openai`) | Emits role/content/tool calls and tool results | Does not emit `reasoning_content` on assistant messages |
| Journaled memory | Folds JSON-safe message dictionaries and preserves known message fields | It can preserve a field only after the memory layer receives it |
| agenthicc provider setup | Already supplies stable `x-opencode-session` when configured by PRD-187 | Does not repair missing reasoning payloads |

The absence of direct `reasoning_content` handling in the adapter is
significant. Retaining the raw response conditionally is not sufficient:
`raw_response` is diagnostic data, is optional, and is not the canonical
conversation history passed to the next request.

### 2.3 Why PRD-137 does not solve this incident

PRD-137 preserves Anthropic content blocks of the form:

```json
{
  "type": "thinking",
  "thinking": "...",
  "signature": "..."
}
```

That is a different wire contract from an OpenAI-compatible assistant message
such as:

```json
{
  "role": "assistant",
  "content": "...",
  "reasoning_content": "...",
  "tool_calls": [
    {
      "id": "call_1",
      "type": "function",
      "function": {"name": "...", "arguments": "..."}
    }
  ]
}
```

Converting `reasoning_content` into an Anthropic `thinking` block would be
incorrect for an OpenAI-compatible endpoint, and dropping it is precisely the
reported bug. The implementation must retain the provider representation and
select the representation at serialization time.

## 3. Problem statement

The current canonical conversation model is lossy for reasoning models using
the OpenAI-compatible Chat Completions protocol. It preserves the tool-call
identity needed to execute a tool, but not all provider-required fields needed
to replay the assistant message. The first provider response therefore cannot
be faithfully used as the input to the second provider request.

This creates four user-visible and operational failures:

1. A workflow can successfully record a phase transition or tool result and
   then fail before the next phase/turn begins.
2. Retrying the request sends the same invalid history repeatedly; HTTP 400 is
   non-transient and cannot be solved by increasing `max_retries`.
3. Resuming a session does not help if the durable message was already stored
   without `reasoning_content`.
4. The TUI reports a transport error even though the actual defect is a
   provider-specific message round-trip mismatch.

The fix must preserve exact provider-required reasoning data without exposing
private reasoning in normal transcript rendering or diagnostics.

## 4. Goals

The implementation MUST:

1. Support OpenAI-compatible `reasoning_content` in non-streaming responses.
2. Support the equivalent reasoning-content delta field in streaming responses,
   including responses where reasoning and tool-call deltas are interleaved.
3. Store the reasoning content on the same assistant message as its text and
   tool calls.
4. Replay the exact value in the OpenAI-compatible assistant message on the
   next request, before/alongside the related tool result exchange as required
   by the provider protocol.
5. Preserve the value through `ShortTermMemory` snapshots, journal append and
   reset records, retry recovery, compaction-safe boundaries, and session
   resume.
6. Preserve old messages that do not have the field and keep existing OpenAI,
   Anthropic, Ollama, and LiteLLM behavior unchanged.
7. Keep reasoning content out of ordinary assistant text, TUI transcript
   output, tool results, user messages, usage diagnostics, and error messages.
8. Make the provider failure diagnosable without logging the reasoning payload
   or credentials.
9. Add deterministic unit, integration, and end-to-end regression coverage
   that exercises a Console Go-like gateway contract without depending on the
   live service.

## 5. Non-goals

This PRD does not:

- change the meaning of `reasoning_effort`, `thinking`, or model selection;
- request reasoning mode from providers that do not already return it;
- expose chain-of-thought or hidden reasoning in the user-facing transcript;
- invent or reconstruct missing reasoning content from `raw_response` after
  the canonical completion has been discarded;
- translate Anthropic `thinking` blocks into OpenAI `reasoning_content`, or the
  reverse;
- implement the OpenAI Responses API item model;
- make HTTP 400 errors retryable;
- alter the OpenCode Go session-header contract from PRD-187;
- guarantee replay for a historical conversation whose reasoning was already
  irreversibly dropped by an older version. Such a conversation must receive
  an actionable compatibility error or a documented repair path.

## 6. Product and protocol requirements

### 6.1 Canonical completion contract

`lauren-ai` MUST add an optional canonical field representing the exact
OpenAI-compatible reasoning content returned for one assistant completion.
The field MUST:

- be absent/`None` when the provider did not return reasoning content;
- preserve the exact string, including whitespace and ordering;
- be independent from `TokenUsage.reasoning_tokens`;
- not be included in the normal `content` string;
- support a completion containing both reasoning content and tool calls;
- remain JSON-safe for snapshots and journal records.

The canonical type may be a dedicated field such as
`Completion.reasoning_content: str | None` plus a streaming
`CompletionChunk.reasoning_delta: str | None`. The exact name can follow the
existing lauren-ai naming conventions, but the public contract must be typed,
documented, and not represented by an unstructured `object` placeholder.

If a provider exposes a non-string reasoning structure (for example a future
`reasoning_details` array), it MUST NOT be silently flattened into this field.
That is a separate adapter extension. The first implementation is for the
string `reasoning_content` contract evidenced by the Console Go error.

### 6.2 Non-streaming capture

The OpenAI-compatible response adapter MUST read the assistant message's
`reasoning_content` when present and set it on the canonical `Completion`.
It MUST work with both SDK objects and mapping-like test fixtures, and it MUST
distinguish a missing field from an explicitly empty string.

The implementation MUST continue to parse text, tool calls, stop reason,
usage, provider metadata, and request IDs exactly as before.

### 6.3 Streaming capture and assembly

The OpenAI-compatible streaming adapter MUST:

- recognize the provider's reasoning-content delta field;
- emit typed reasoning deltas without presenting them as visible text deltas;
- preserve their order relative to the assistant completion;
- accumulate all deltas before the assistant completion is committed;
- handle reasoning-only chunks, text-only chunks, tool-call-only chunks, and
  interleaved chunks;
- finalize the reasoning string even when the response ends with a tool call;
- avoid duplicating content if a gateway sends a final aggregate field after
  deltas;
- preserve the existing fallback behavior for incomplete tool-call names.

If a provider uses a different delta attribute name, the adapter must use an
explicit provider compatibility mapping rather than a broad heuristic that
could accidentally treat normal text or an unrelated metadata field as
reasoning.

### 6.4 Assistant-memory representation

When an assistant completion contains `reasoning_content`, memory MUST store
it as metadata on that exact assistant message. For example:

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "tool_use",
      "id": "call_1",
      "name": "goal_implemented",
      "input": {"summary": "..."}
    }
  ],
  "reasoning_content": "exact provider reasoning"
}
```

For a text-only completion, the same top-level field MUST remain attached to
the assistant message. It MUST NOT be converted into a `text` block, appended
to visible content, stored as a synthetic user message, or placed on the tool
result.

The internal representation MUST survive `snapshot()`/`restore()` without
loss. The memory layer MUST preserve the field when adding, repairing, or
replacing tool exchanges. A synthetic repair result for an interrupted tool
call must never accidentally move, erase, or duplicate the reasoning metadata
on the preceding assistant message.

### 6.5 OpenAI-compatible serialization

The OpenAI-compatible serializer MUST emit `reasoning_content` on an assistant
message when the stored message has a non-`None` value. It MUST preserve the
exact value and emit it once.

The serializer MUST:

- keep `tool_calls` and their IDs unchanged;
- keep assistant text unchanged;
- retain the field when the message has empty visible content but has tool
  calls or reasoning content;
- omit the field for messages that never had it;
- accept both live canonical objects and JSON/dict messages restored from a
  journal;
- not emit this field in the Anthropic serializer, Ollama serializer, or other
  provider paths unless that provider explicitly declares the same contract.

The final request shape for the reported case must therefore contain the
assistant reasoning content, assistant tool call, and the matching tool result
in the same logical exchange. The gateway-specific requirement is validated by
the test fake, not by special-casing the string `Console Go` in the transport.

### 6.6 Durable history, compaction, and resume

The reasoning field is part of the provider message payload and MUST be
preserved by:

- journal `append` records;
- journal `reset` records used by retry/compaction;
- session restart and `--continue`/`--resume` rehydration;
- workflow checkpoints that contain a conversation snapshot;
- active-turn recovery after a failed provider step;
- copy/serialization paths used by cassette recording and replay.

Compaction MUST treat the field as an atomic provider-required field:

- it MUST NOT truncate, normalize, summarize, or partially retain an active
  assistant message's reasoning content;
- it MUST not split a reasoning-bearing assistant tool-call message from its
  matching tool-result exchange;
- if an older, fully resolved exchange is removed as a whole, its removal must
  be an intentional complete-message compaction decision, not field-level
  truncation;
- the implementation MUST record enough metadata to diagnose when compaction
  cannot safely retain a required active exchange.

This requirement does not mandate retaining all historical reasoning forever;
it mandates that any reasoning-bearing message still selected for a provider
request remains byte-for-byte faithful.

### 6.7 Retries and HTTP error classification

The 400 missing-`reasoning_content` response MUST remain non-transient. The
transport MUST NOT retry it as if it were a 429/5xx outage.

Once the round-trip implementation is present, a retry after a transient
failure MUST use the same faithful assistant message. If the stored history
does not contain the reasoning content required by the provider, the client
MUST fail deterministically with an actionable diagnostic identifying a
conversation-history compatibility problem; it MUST NOT fabricate a value.

The diagnostic MUST include provider/model and the affected message category,
but MUST NOT include the reasoning text, API key, authorization header, or raw
request body.

## 7. End-to-end data flow after implementation

```text
OpenAI-compatible gateway response
  message.reasoning_content = R
  message.tool_calls = C
          │
          ▼
lauren-ai OpenAI adapter
  Completion(reasoning_content=R, tool_calls=C)
  or streamed reasoning_delta chunks assembled to R
          │
          ▼
Agent runner commits one assistant message atomically
  {role: assistant, content/tool_calls, reasoning_content: R}
          │
          ├── append to ShortTermMemory
          ├── append to conversation journal
          └── include in checkpoint/snapshot when applicable
          │
          ▼
tool executes; matching tool result is committed
          │
          ▼
next agent turn reads the same session memory
          │
          ▼
OpenAI-compatible serializer emits:
  assistant {content, reasoning_content: R, tool_calls: C}
  tool     {tool_call_id, content: result}
          │
          ▼
Console Go accepts the request and routes it normally
```

For a resume, the middle section is rehydrated from the journal/checkpoint
before any new provider request:

```text
conversation journal/checkpoint
  └── assistant message includes reasoning_content
          │
          ▼
JournaledShortTermMemory.fold() / checkpoint restore
          │
          ▼
OpenAI serializer emits the same value verbatim
```

## 8. Implementation design

### 8.1 Lauren-ai changes

The canonical implementation belongs in `../lauren-all/lauren-ai`:

1. Extend `Completion` and `CompletionChunk` with typed reasoning-content
   fields and public documentation.
2. Update `_transport/_openai.py` non-stream parsing and stream assembly.
3. Update the agent runner's stream accumulator so reasoning deltas are
   attached to the completion committed to memory.
4. Update `ShortTermMemory.add_assistant`, snapshot/restore, repair, and
   compaction guards.
5. Update `_message_to_openai` and any related message conversion helpers.
6. Add an explicit provider capability/format decision so the OpenAI field is
   not accidentally emitted by another serializer.
7. Keep older persisted dict messages and older completion constructors
   backwards-compatible by making the new field optional.

The lauren-ai change must be released and consumed by agenthicc through the
normal dependency constraint. A local editable checkout may be used during
development, but production verification must run against the packaged
version that agenthicc declares.

### 8.2 agenthicc changes

The agenthicc implementation must be limited to integration and durability
seams that it owns:

1. Verify `JournaledShortTermMemory` and conversation-journal folding preserve
   arbitrary JSON-safe assistant metadata without filtering the new field.
2. Verify workflow checkpoints and session resume pass the enriched memory to
   lauren-ai before the next turn.
3. Extend cassette/recording fixtures only where needed to represent the
   canonical reasoning fields; do not persist secrets or provider raw bodies.
4. Add a gateway-style integration fake that rejects an assistant tool-call
   request if the preceding assistant message omitted the reasoning content
   returned in the first response.
5. Confirm PRD-187's stable `x-opencode-session` header and PRD-190's
   `reasoning_content` appear together on the same request without coupling
   their configuration or persistence semantics.
6. Add a user-facing recovery message for histories that are too old to repair
   because the reasoning content was already discarded. The message should
   recommend starting a fresh conversation or using a provider that does not
   require the missing field; it must not suggest increasing retries.

No new provider-specific branch should be added to workflow implementations.
Workflows use the same session-scoped agent runner and therefore receive the
fix automatically once the transport/memory contract is correct.

## 9. Acceptance criteria

### Functional acceptance

| ID | Criterion |
|---|---|
| 190.1 | A non-streaming OpenAI-compatible response with `message.reasoning_content` produces a `Completion` containing the exact string. |
| 190.2 | A streamed response with reasoning deltas produces one assistant completion with the exact concatenated reasoning content. |
| 190.3 | Interleaved reasoning, text, and tool-call deltas do not move reasoning into visible text or lose tool-call identity. |
| 190.4 | `ShortTermMemory.add_assistant` stores reasoning content on the same assistant message as its tool call. |
| 190.5 | `_message_to_openai` emits the stored field exactly once and preserves the tool call and matching result. |
| 190.6 | A two-request gateway fixture accepts the second request only when the first response's reasoning content is replayed. The fixture passes with the implementation. |
| 190.7 | A reasoning-bearing message survives memory snapshot/restore and journal fold byte-for-byte. |
| 190.8 | `--resume`/`--continue` and workflow recovery rehydrate the field before making the next provider request. |
| 190.9 | A transient retry resends the faithful history; a 400 missing-reasoning error is not retried as transient. |
| 190.10 | Messages without reasoning content serialize exactly as before. |
| 190.11 | Anthropic `thinking` blocks remain ordered and serialized by PRD-137, while OpenAI `reasoning_content` is not emitted on Anthropic requests. |
| 190.12 | Empty or absent reasoning content is handled deterministically without creating a synthetic field that the provider did not return. |

### Safety and privacy acceptance

| ID | Criterion |
|---|---|
| 190.13 | Reasoning content never appears in ordinary TUI transcript rendering, tool-result text, status lines, error strings, or redacted diagnostics. |
| 190.14 | API keys, authorization headers, session IDs, and raw provider bodies are not included in new logs or test snapshots. |
| 190.15 | The exact reasoning value is retained only in the provider message/journal path required for replay and follows the existing session retention policy. |
| 190.16 | No serializer emits OpenAI-specific reasoning metadata to an incompatible provider. |

### Compatibility acceptance

| ID | Criterion |
|---|---|
| 190.17 | Existing constructors, old snapshots, old journals, and completions without the optional field continue to load. |
| 190.18 | Existing normal OpenAI text/tool-call conversations remain wire-equivalent. |
| 190.19 | Existing PRD-137 Anthropic thinking round-trip tests remain green. |
| 190.20 | OpenCode Go session-header propagation from PRD-187 remains stable across new sessions, workflows, retries, and resume. |

## 10. Test plan

### 10.1 lauren-ai unit tests

Add focused tests beside the transport and memory tests:

- response parsing captures present, absent, and empty
  `reasoning_content`;
- mapping and SDK-object response shapes are both supported;
- stream accumulation handles reasoning-only, text-only, tool-only, and
  interleaved chunks;
- the final aggregate reasoning field cannot duplicate previously emitted
  deltas;
- `Completion`/`CompletionChunk` optional fields preserve old construction
  behavior;
- `ShortTermMemory` stores and restores the field on the correct assistant
  message;
- tool-history repair leaves the field attached and unchanged;
- compaction never partially truncates an active reasoning-bearing message;
- `_message_to_openai` emits the field, while the Anthropic serializer does
  not;
- malformed provider values fail safely without logging their contents.

### 10.2 agenthicc integration tests

Add deterministic tests using temporary session directories and a fake
OpenAI-compatible transport/server:

1. First response: assistant reasoning plus `Goal Implemented` tool call.
2. Agenthicc commits the tool result to `JournaledShortTermMemory`.
3. Second request: the fake gateway checks for exact reasoning content,
   assistant tool call ID, matching tool result ID, and the stable
   `x-opencode-session` header.
4. The fake returns success only when all fields are present.
5. Restart the in-memory/session objects from the journal and repeat the
   second request.
6. Inject a transient failure between requests and verify retry payloads are
   identical with respect to reasoning content.
7. Inject the old lossy history and verify a bounded actionable error rather
   than an infinite retry loop.

These tests must use fake values such as `REASONING_FIXTURE`, never real model
responses, API keys, or production endpoints.

### 10.3 end-to-end acceptance tests

Run a headless agenthicc session against the local fake gateway with one
workflow tool transition followed by a verification turn. Assert that:

- the workflow does not restart or fail after the tool result;
- the second request contains the exact reasoning field;
- session resume produces the same result;
- the user-facing transcript contains the visible assistant response and tool
  result but not the hidden reasoning string;
- a gateway 400 is rendered as a concise non-retryable compatibility error.

The live Console Go service MUST NOT be required for CI. An opt-in smoke test
may target it locally, but it must be disabled by default and must redact
request/response payloads in failure output.

## 11. Rollout and migration

1. Land lauren-ai model, parser, memory, and serializer changes behind the
   existing optional-field compatibility contract.
2. Release a lauren-ai version containing the change and update agenthicc's
   dependency lock/constraint.
3. Land agenthicc integration, journal, checkpoint, and E2E coverage.
4. Deploy with ordinary provider behavior unchanged for models that do not
   return `reasoning_content`.
5. Monitor structured counters for:
   - captured reasoning-bearing assistant messages;
   - serialized reasoning-bearing requests;
   - non-retryable missing-reasoning compatibility failures;
   - resume/recovery failures involving enriched messages.

Counters MUST contain provider/model and a redacted reason code only. Do not
record reasoning text.

There is no destructive migration. Existing journals remain valid because the
field is optional. Journals created after the fix contain the field in their
normal JSON message payload. A journal created before the fix cannot be
retroactively repaired unless an authoritative provider response containing
the missing reasoning content is available; the implementation must not guess.

## 12. Security, privacy, and performance

- Treat `reasoning_content` as sensitive provider output. It may contain
  private project context even when it is not displayed to the user.
- Reuse existing journal permissions, retention, redaction, export, and
  session ownership controls. Do not add a second storage location.
- Never include the field in exception messages, debug logs, metrics labels,
  cassette names, or diagnostic JSON.
- Do not send the field to a provider whose serializer did not request it.
- Preserve exact strings without repeated concatenation that creates
  quadratic behavior for long reasoning streams; use the same bounded
  accumulator strategy as other streamed content.
- Keep the field optional so normal non-reasoning requests incur no additional
  wire fields or meaningful memory overhead.
- Do not solve this by globally disabling reasoning. That would reduce model
  capability and would not be safe for gateways that require the field when
  the model emits it.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| SDK drops unknown response attributes | Test both SDK-like objects and mappings; inspect raw fields only at the adapter boundary and normalize into the canonical field. |
| A gateway uses a non-string reasoning structure | Scope the first release to string `reasoning_content`; reject or preserve future structures through an explicit extension instead of flattening them. |
| Reasoning leaks into the UI | Keep the field separate from visible `content`; add transcript and diagnostic assertions. |
| Compaction removes required data | Treat active reasoning-bearing assistant/tool exchanges as atomic and test compaction/resume. |
| Old histories remain invalid | Detect missing required metadata, report a bounded compatibility error, and never fabricate reasoning. |
| Provider-specific behavior spreads into workflows | Keep all behavior in lauren-ai transport/memory and the agenthicc session boundary; workflows remain unchanged. |
| Developers increase retries instead of fixing payloads | Keep HTTP 400 non-transient and explicitly test retry classification. |

## 14. Implementation checklist

- [x] Add typed canonical reasoning-content fields to lauren-ai completion
      types and public documentation.
- [x] Capture non-streaming OpenAI-compatible reasoning content.
- [x] Capture and assemble streaming reasoning deltas.
- [x] Attach reasoning to the exact assistant memory message.
- [x] Preserve it through snapshot/restore, journal, checkpoint, recovery, and
      compaction-safe paths.
- [x] Emit it only in the OpenAI-compatible serializer.
- [x] Add lauren-ai unit/regression tests.
- [x] Add agenthicc fake-gateway integration tests.
- [x] Add headless/session-resume E2E coverage.
- [x] Update release notes for the compatible lauren-ai version. The existing
      `>=1.5.0,<2` dependency range already admits the release; lock refresh
      is performed when the lauren-ai release is published.
- [x] Update provider and troubleshooting documentation with the distinction
      between missing session identity and missing reasoning content.
- [x] Run the relevant lint, type, unit, integration, and E2E checks.

## 15. Definition of done

This PRD is complete when the implementation can run the reported
tool-transition flow against a deterministic Console Go-like gateway, complete
the following turn, and resume the same session after a process restart; when
the exact reasoning content is present in each provider-facing replay but
never in the normal transcript; when old non-reasoning histories remain
compatible; and when all acceptance, privacy, and regression tests pass in
both the lauren-ai and agenthicc repositories.
