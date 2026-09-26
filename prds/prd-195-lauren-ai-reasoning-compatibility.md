---
title: "PRD-195: Enforced lauren-ai compatibility for reasoning replay"
status: Proposed
version: 1.0.0
date: 2026-09-22
scope: "lauren-ai dependency enforcement, runtime compatibility checks, and recovery for lossy reasoning histories"
related_prds:
  - PRD-169  # transaction-safe provider conversations
  - PRD-182  # durable mid-turn preservation
  - PRD-187  # OpenCode Go session identity propagation
  - PRD-190  # OpenAI-compatible reasoning_content round-trip
tags:
  - lauren-ai
  - dependency-compatibility
  - reasoning
  - openai-compatible
  - opencode-go
  - session-resume
  - recovery
---

# PRD-195 — Enforced lauren-ai compatibility for reasoning replay

## 1. Executive summary

OpenAI-compatible providers that run a model in reasoning/thinking mode may
return an assistant message containing `reasoning_content`. Some gateways
require that exact value to be sent back with the assistant tool-call message
on the next request.

The reported failure is:

```text
TransportError: OpenAI-compatible provider rejected the assistant history
because its required reasoning_content was not replayed.
...
The `reasoning_content` in the thinking mode must be passed back to the API.
```

This is a non-retryable conversation compatibility failure. It is not a
workflow transition error, an API-key error, or a transient network outage.
The provider returned reasoning content, but the runtime that handled the
response either did not support the `reasoning_content` contract or loaded an
older `lauren-ai` implementation that discarded it before the next request.

PRD-190 defines the wire-level round trip. This PRD closes the release and
runtime gap around that contract. It makes the minimum compatible lauren-ai
version explicit, prevents an incompatible installation from starting a
reasoning-capable session, gives operators an actionable diagnostic when an
old environment is detected, and defines safe recovery for sessions whose
history was permanently written without the required field.

The implementation MUST never fabricate missing reasoning content, retry the
same invalid request indefinitely, or expose hidden reasoning in the TUI.

## 2. Evidence and diagnosis

### 2.1 Observed failure sequence

The failure occurs after the provider has already produced a useful response:

```text
user request
    │
    ▼
OpenAI-compatible response
  assistant.content = visible text or empty string
  assistant.reasoning_content = R
  assistant.tool_calls = C
    │
    ▼
agent runner executes C and commits the tool result
    │
    ▼
next provider request serializes the conversation
  assistant.tool_calls = C
  assistant.reasoning_content = missing
    │
    ▼
gateway returns HTTP 400
  reasoning_content must be passed back to the API
```

The error can be repeated by pressing Continue, retrying the turn, or
resuming the session when each operation sends the same lossy history. A
larger retry count cannot solve it because the request is deterministically
invalid.

### 2.2 Compatibility mismatch

The `lauren-ai` 1.5 transport model does not expose
`Completion.reasoning_content` or `CompletionChunk.reasoning_content_delta`.
Its OpenAI adapter therefore cannot reliably capture the provider field or
re-emit it from memory.

The compatible 1.6 contract adds:

- a typed optional reasoning field on completed responses;
- a typed reasoning delta for streaming responses;
- OpenAI response parsing and stream assembly;
- memory and journal-safe persistence; and
- OpenAI assistant-message serialization that replays the exact field.

The immediate dependency floor has since been raised in agenthicc's project
metadata to `>=1.6.0,<2`. This PRD remains necessary because dependency
metadata alone does not detect a shadowed/stale running environment or repair
sessions whose history was already written by the lossy implementation.

An agenthicc source checkout can still be incompatible if a stale environment,
an optional dependency resolution, an editable/system installation shadow, or
an already-running old process supplies the older contract. A session created
before the compatible transport may also have permanently lost the field.

### 2.3 Ownership boundary

The canonical field parsing, memory contract, and provider serializer belong
to lauren-ai and are specified by PRD-190. Agenthicc owns dependency
declaration, startup/session construction, user-facing diagnostics,
durable-session recovery orchestration, and integration tests proving that
the enriched memory reaches lauren-ai unchanged.

No workflow should implement a provider-specific workaround.

## 3. Problem statement

The product can present a low-level HTTP 400 after the user has already made
progress. The error does not tell the user whether the running process has
the compatible lauren-ai contract, whether the session history is too old to
repair, or what safe action is available.

This creates four defects:

1. Installation compatibility is implicit rather than enforced.
2. An old runtime can begin a session and fail only after a reasoning tool
   call, wasting tokens and user time.
3. A historical session with irreversibly missing reasoning content is
   repeatedly retried even though no local component can reconstruct it.
4. The TUI reports a transport error without distinguishing an actionable
   dependency problem from an unrecoverable historical-payload problem.

## 4. Goals

The implementation MUST:

1. Require lauren-ai 1.6.0 or newer, while preserving the existing `<2`
   compatibility ceiling.
2. Apply the same minimum version to the base dependency and the optional MCP
   dependency.
3. Ensure clean installs, editable installs, and CI environments resolve the
   declared compatible version rather than silently using 1.5 behavior.
4. Verify at runtime that the loaded lauren-ai transport supports the
   reasoning-content round-trip contract before an affected provider session
   can use it.
5. Fail with a concise remediation message when the loaded version is too
   old or the capability is incomplete.
6. Preserve exact reasoning content through new turns, tool results,
   retries, journals, checkpoints, `--continue`, and `--resume`, as defined
   by PRD-190.
7. Detect a historical assistant tool-call message that lacks required
   reasoning metadata when the provider rejects it, and classify the failure
   as non-retryable.
8. Tell the user how to recover without deleting the existing session or
   fabricating hidden reasoning.
9. Keep reasoning content out of visible transcript rendering, ordinary logs,
   metrics, and diagnostics.
10. Add deterministic unit, integration, end-to-end, and packaging tests for
    the complete compatibility path.

## 5. Non-goals

This PRD does not:

- replace or redesign PRD-190's lauren-ai transport implementation;
- reconstruct reasoning content already discarded by an older runtime;
- make missing-reasoning HTTP 400 responses transient;
- disable reasoning mode globally;
- expose chain-of-thought in the TUI or session list;
- convert OpenAI-compatible `reasoning_content` into Anthropic thinking
  blocks;
- add provider-specific branches to workflows;
- require a live OpenCode Go or Console Go endpoint in CI; or
- delete or rewrite an old session automatically.

## 6. Functional requirements

### 6.1 Dependency contract

`pyproject.toml` MUST declare:

```toml
"lauren-ai[anthropic,openai,ollama,litellm]>=1.6.0,<2"
```

The optional MCP dependency MUST use the same lower bound:

```toml
"lauren-ai[mcp]>=1.6.0,<2"
```

The supported installation paths MUST be covered: normal `uv sync` and
`uv run`, development and MCP extras, editable source checkouts, and package
builds installed from generated metadata. The dependency declaration is the
source of truth; a local editable lauren-ai checkout is development-only.

### 6.2 Runtime capability contract

Before constructing an OpenAI-compatible reasoning session, the runtime MUST
verify that the loaded lauren-ai release provides the complete contract:

- `Completion.reasoning_content: str | None`;
- `CompletionChunk.reasoning_content_delta: str | None`; and
- OpenAI serialization of a stored assistant `reasoning_content` field.

The preferred implementation is a small public lauren-ai capability query or
versioned capability constant. Agenthicc MUST NOT inspect private module
source code or duplicate the OpenAI serializer.

The check MUST run once per process or cached configuration, distinguish an
old package from a provider that simply did not return reasoning content, make
no live provider request, and fail before an affected session starts. Normal
provider paths that do not require this field remain usable.

### 6.3 Incompatible-runtime diagnostic

The TUI and headless runner MUST report a concise error equivalent to:

```text
This session requires lauren-ai >= 1.6.0 for OpenAI-compatible
reasoning-content replay, but the running environment provides <version>.
Refresh the agenthicc environment and restart the process.
```

The diagnostic MUST include an actionable command such as:

```bash
uv sync
uv run agenthicc
```

It MUST NOT include API keys, authorization headers, hidden reasoning, raw
provider requests, or a full traceback by default.

### 6.4 Exact new-session round trip

For a compatible runtime, this invariant MUST hold:

```text
provider response.reasoning_content = R
        │
        ▼
Completion.reasoning_content = R
        │
        ▼
assistant memory message.reasoning_content = R
        │
        ├── journal/checkpoint/snapshot
        │
        ▼
OpenAI-compatible request assistant.reasoning_content = R
```

The value MUST be byte-for-byte identical, including empty strings,
whitespace, and ordering. It remains separate from visible `content` and
token accounting.

### 6.5 Historical-session handling

The runtime MUST distinguish these cases:

| Case | Required behavior |
|---|---|
| Runtime is older than 1.6.0 | Fail before a provider request. |
| Runtime is compatible and history contains the field | Replay normally. |
| Runtime is compatible but old history lacks the field | Do not fabricate or retry forever; return a recovery diagnostic. |
| Provider does not require or return the field | Preserve existing behavior. |
| Provider returns a non-string value | Reject the malformed response without logging its value. |

For a lossy historical session, offer: a new conversation/session; an
alternate provider/model that does not require the field; or preserving the
old session for inspection/export. Do not delete the session, overwrite its
journal, invent a placeholder, or turn missing reasoning into visible text.

### 6.6 Retry classification

The missing-`reasoning_content` response is non-transient:

- it does not consume the transient retry budget;
- Retry/Continue cannot submit the same invalid request in an unbounded loop;
- existing retry behavior for 429, 5xx, connection resets, and timeouts is
  unchanged; and
- a transient retry after successful capture reuses the same enriched history.

### 6.7 TUI privacy

The TUI MAY show a stable error code such as
`reasoning_history_incompatible`, but MUST NOT render the reasoning payload.
Reasoning text must be absent from transcript messages, scroll summaries,
session rows, status bars, metrics, event names, cassette identifiers, and
default errors. Durable provider history remains subject to existing session
permissions and retention policy.

## 7. Data flow

### 7.1 Compatible new session

```text
provider response {content, reasoning_content: R, tool_calls: C}
        │
        ▼
lauren-ai 1.6+ adapter → Completion(reasoning_content=R, tool_calls=C)
        │
        ▼
agent runner commits one assistant exchange atomically
        │
        ├── ShortTermMemory
        ├── conversation journal
        └── workflow/session checkpoint
        │
        ▼
tool result with matching identity is committed
        │
        ▼
serializer → assistant {content, reasoning_content: R, tool_calls: C}
             + tool {tool_call_id, content}
        │
        ▼
provider accepts the next request
```

### 7.2 Old runtime or lossy history

```text
session construction
        │
        ├── old lauren-ai → fail before provider request
        │
        └── old lossy journal → one non-retryable recovery diagnostic
                                      │
                                      └── new session / alternate provider /
                                          inspect old session
```

## 8. Implementation design

### 8.1 Packaging and release

1. Raise the base and MCP lauren-ai lower bounds to 1.6.0.
2. Regenerate resolver state and verify the resolved package is 1.6.0 or
   newer.
3. Add a clean-environment packaging test that builds the wheel and installs
   it into an isolated environment.
4. Add CI checks that fail if metadata or an installation report resolves
   lauren-ai below 1.6.0.
5. Document that an already-running Python process must be restarted after an
   environment upgrade.

### 8.2 Lauren-ai integration

The lauren-ai 1.6 transport and memory contract remains canonical. If a
capability query does not exist, add a small typed public contract in
lauren-ai and consume it from agenthicc. Do not import private implementation
modules merely to inspect dataclass fields.

### 8.3 Session recovery

Add a narrow recovery classification at the session/provider boundary:

- `provider_transient` — eligible for existing retry behavior;
- `reasoning_runtime_incompatible` — loaded lauren-ai lacks the contract; and
- `reasoning_history_incompatible` — compatible runtime, but selected
  history lacks required reasoning metadata.

Persist only bounded diagnostic metadata. Never persist raw provider bodies or
hidden reasoning in error records. For historical incompatibility, keep the
session durable for inspection and require an explicit new-session or
alternate-provider decision before another provider request.

### 8.4 No workflow-specific changes

`code_plan`, `goal_flow`, `create_workflow`, `reconstruct_site`, and every
other workflow continue using shared session transport and memory. The fix
must apply to direct turns, workflow turns, subagents, retries, resume, and
headless execution through shared infrastructure.

## 9. Acceptance criteria

### Functional

| ID | Criterion |
|---|---|
| 195.1 | Base agenthicc installation declares lauren-ai `>=1.6.0,<2`. |
| 195.2 | MCP installation declares the same compatible lower bound. |
| 195.3 | A clean isolated install resolves lauren-ai 1.6.0 or newer. |
| 195.4 | An old/incomplete runtime fails before a provider request with an actionable diagnostic. |
| 195.5 | A compatible non-streaming response preserves and replays exact `reasoning_content`. |
| 195.6 | A compatible streaming response preserves and replays exact reasoning deltas. |
| 195.7 | Reasoning survives tool execution, journal folding, checkpoints, `--continue`, and `--resume`. |
| 195.8 | A fake gateway requiring replay accepts the follow-up request. |
| 195.9 | A lossy historical session does not trigger an infinite retry loop. |
| 195.10 | The old session remains intact after the error. |
| 195.11 | The user receives a new-session or alternate-provider recovery path. |
| 195.12 | Existing transient retry behavior remains unchanged. |

### Compatibility and privacy

| ID | Criterion |
|---|---|
| 195.13 | Messages without reasoning remain wire-compatible. |
| 195.14 | Anthropic thinking blocks are not emitted as OpenAI `reasoning_content`. |
| 195.15 | Reasoning text is absent from normal TUI transcript, status, and metrics. |
| 195.16 | Diagnostics contain no API key, authorization header, raw request, or raw response. |
| 195.17 | The product does not claim that upgrading repaired history already written without the field. |

## 10. Test plan

### 10.1 Unit tests

Cover capability detection for 1.5, 1.6, and newer versions; absent versus
empty fields; error classification; retry eligibility; diagnostic redaction;
and preservation of an old session when recovery is offered.

### 10.2 Integration tests

With a temporary journal and fake OpenAI-compatible gateway:

1. Return reasoning plus a tool call.
2. Commit the matching tool result.
3. Assert exact reasoning and tool identities on the second request.
4. Close and reopen the session and repeat the request.
5. Simulate an old runtime and assert that no gateway request is attempted.
6. Use a lossy historical journal and assert one bounded recovery error,
   no deletion, and no repeated identical request.

### 10.3 End-to-end tests

Headless and TUI tests MUST cover a direct turn, a workflow transition, restart
and `--resume`, old-session recovery, hidden-reasoning redaction, and normal
non-reasoning OpenAI and Anthropic conversations. CI uses a fake gateway; a
live provider smoke test is opt-in only.

### 10.4 Packaging gate

The release gate MUST include:

```bash
uv lock --check
uv build
uv run pytest tests/unit/test_reasoning_content_persistence.py -q
uv run pytest tests/integration/test_reasoning_content_gateway.py -q
uv run pytest tests/e2e/test_reasoning_content_resume_e2e.py -q
```

The clean-install test MUST inspect installed distribution metadata, not only
the source checkout, so a shadowed system package cannot make it pass.

## 11. Rollout and migration

1. Make lauren-ai 1.6.0 available.
2. Raise agenthicc's base and MCP dependency lower bounds.
3. Regenerate resolver state and run the clean-install gate.
4. Deploy the runtime capability check and recovery classification.
5. Keep healthy sessions unchanged because the new field is optional.
6. Keep sessions whose reasoning was already dropped readable but unrepaired;
   require a new session or alternate provider.
7. Do not bulk-rewrite or delete journals.

## 12. Security, privacy, and performance

- Treat reasoning content as sensitive provider output.
- Reuse journal permissions and retention rules.
- Never put reasoning text in logs, metrics labels, exceptions, or session
  list metadata.
- Perform the compatibility check once and cache its result.
- Do not make a live probe request solely to check compatibility.
- Keep the optional field absent for ordinary messages.
- Preserve exact values without quadratic stream concatenation.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A system or editable install shadows the project environment | Runtime capability check plus clean-install CI test. |
| A future lauren-ai release changes the field contract | Public capability contract and `<2` ceiling until reviewed. |
| Users mistake HTTP 400 for an outage | Non-retryable classification and targeted recovery message. |
| Historical data cannot be repaired | Preserve the old session and require an explicit choice. |
| Hidden reasoning leaks through diagnostics | Centralized redaction with rendered-output tests. |
| Provider logic spreads into workflows | Keep behavior in the shared transport/session boundary. |

## 14. Definition of done

The PRD is complete when agenthicc and its MCP extra require lauren-ai 1.6.0
or newer, a clean installation loads the compatible package, reasoning content
round-trips through direct turns and workflows including resume, old runtimes
fail before invalid requests, lossy histories get bounded recovery, no hidden
reasoning is fabricated or displayed, and unit, integration, E2E, and
packaging tests pass.
