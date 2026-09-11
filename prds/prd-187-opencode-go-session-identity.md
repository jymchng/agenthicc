---
title: "PRD-187: OpenCode Go session identity propagation"
status: Implemented
version: 1.1.0
created: 2026-09-11
scope: "provider request headers, stable conversation identity, OpenCode Go and compatible gateways"
related_prds:
  - PRD-07   # layered configuration
  - PRD-126  # transport retry
  - PRD-132  # prompt/cache reuse
  - PRD-150  # client-neutral session identity and event projection
  - PRD-156  # resumable session conversation
  - PRD-157  # session-scoped usage accounting
  - PRD-162  # provider profiles and OpenAI-compatible endpoints
  - PRD-169  # transaction-safe provider conversations
tags:
  - providers
  - opencode-go
  - session-identity
  - request-headers
  - prompt-cache
  - openai-compatible
  - configuration
---

# PRD-187 — OpenCode Go session identity propagation

## 1. Executive summary

An agenthicc session configured against OpenCode Go currently fails before the
model can answer:

```text
TransportError: Error code: 400 - {
  'type': 'error',
  'error': {
    'type': 'MissingSessionID',
    'message': 'Request is missing x-opencode-session and cannot be routed efficiently'
  }
}
```

OpenCode Go requires clients to send a stable `x-opencode-session` header for
each conversation. agenthicc already owns exactly the required identity:
`SessionConversation.conversation_id`, which is created once for a new session,
restored unchanged by `--resume` and `--continue`, and shared by direct turns,
workflows, and their sub-turns. The identity is currently passed into
agenthicc and lauren-ai agent context, but it is not forwarded into
`LLMConfig.default_headers` when the provider transport is constructed.

This PRD adds an explicit, validated dynamic session-header binding to provider
profiles and the legacy execution configuration. The selected binding resolves
the current session conversation ID immediately before transport construction,
merges it into the immutable lauren-ai default headers, and therefore sends it
on every request made by that session-owned transport. The feature is generic:
OpenCode Go is the first documented consumer, while other gateways can select a
different header name or leave the feature disabled.

The fix must not create random IDs per request, use the workflow run ID as a
replacement for the conversation ID, persist secrets, or silently change
existing providers that do not opt into dynamic session headers.

## 2. Evidence-backed current-state findings

### 2.1 Request path

The current interactive construction path is:

```text
resume/new session ID
        │
        ▼
SessionConversation.open(conversation_id=session_id)
        │
        ├── JournaledShortTermMemory
        ├── durable conversation journal
        └── stable conversation_id
        │
        ▼
cfg.resolve_provider_profile()
        │
        ▼
build_llm_config(cfg.execution)
        │
        ├── static default_headers only
        └── LLMConfig
        │
        ▼
_LazyAgentRunner → lauren_ai._module._build_transport()
        │
        ▼
AgentRunnerBase → provider request
```

`src/agenthicc/runners/session_conversation.py` documents and enforces the
session-wide conversation identity. `src/agenthicc/runners/tui_session.py`
constructs the provider configuration before creating the lazy agent runner.
`src/agenthicc/config.py::build_llm_config()` forwards static headers to
lauren-ai, but its API has no conversation/session identity argument and cannot
derive one from `ExecutionSettings`.

`lauren-ai` 1.4.x already supports `LLMConfig.default_headers`, and the header
is applied by the provider transport to outgoing requests. No OpenCode SDK or
second HTTP client is required.

### 2.2 Why the current profile support is insufficient

PRD-162 added `default_headers`, including secret references, to named provider
profiles. That supports a literal or environment-backed value, for example an
API gateway token, but `x-opencode-session` is not a fixed credential. Its value
must be the active session's stable conversation ID. Configuring a literal
session header would either route every session under one identity or require
manual changes for every run.

### 2.3 Scope of the failure

The failure is not caused by:

- an invalid model name;
- an OpenAI-compatible base URL;
- the normal agent turn retry count;
- prompt-cache settings; or
- a missing API key.

It is a request-construction defect. HTTP 400 is correctly treated as a
non-transient provider error and must not be retried ten times. The provider
rejects the request because the identity header was absent before routing.

## 3. Problem statement

OpenCode Go uses `x-opencode-session` to associate requests with a stable
conversation and optimize routing and prompt caching. Without the header:

1. the first request is rejected with `MissingSessionID`;
2. retries cannot fix it because they repeat the same malformed request;
3. direct turns and workflow phases cannot reach the model;
4. if only some auxiliary calls receive the header, routing and cache behavior
   becomes inconsistent; and
5. operators receive a provider-specific error without an agenthicc-native
   configuration path that explains the missing identity.

The implementation must establish one authoritative mapping from an agenthicc
session conversation ID to a provider request header and apply it at the
session-owned transport boundary.

## 4. Goals

The implementation MUST:

1. Send a stable session header on every model request when the active provider
   profile enables it.
2. Use `SessionConversation.conversation_id` as the default identity source.
3. Preserve that value across direct turns, Plan/Safe/Yolo mode changes,
   workflow phase transitions, subagent calls, compaction, retries, resume, and
   `--continue`.
4. Support the exact OpenCode Go header name `x-opencode-session`.
5. Support other configured header names for compatible gateways without
   hard-coding vendor-specific providers.
6. Keep existing configurations behaviorally unchanged when no dynamic session
   header is configured.
7. Validate header names and identity values before transport construction,
   including CR/LF rejection and bounded length.
8. Detect case-insensitive collisions between a static header and a dynamic
   session header rather than silently selecting an ambiguous value.
9. Keep API keys and other secrets out of session state, checkpoints, journals,
   ordinary logs, redacted configuration output, and error messages.
10. Provide an actionable configuration diagnostic for OpenCode Go profiles
    that are missing the required session-header binding.
11. Preserve the existing lauren-ai transport, retry, cache, and usage-accounting
    ownership boundaries.
12. Add deterministic unit, integration, and end-to-end tests.

## 5. Non-goals

This PRD does not:

- add a separate OpenCode Go SDK or HTTP implementation;
- add a new provider transport when the endpoint speaks OpenAI-compatible
  Chat Completions or Responses;
- generate a new UUID for each request;
- use `workflow_run_id`, turn ID, intent text, or model name as the default
  session identity;
- send `x-opencode-session` to every provider by default;
- store API keys or authorization values in the session header;
- let a workflow phase override the provider session identity;
- make a missing-session-header 400 retryable;
- guarantee provider-side caching, which remains controlled by the endpoint and
  lauren-ai transport; or
- change the existing session journal or workflow checkpoint schema merely to
  store a duplicate header value.

## 6. Product and configuration contract

### 6.1 Profile configuration

Add an optional `session_header` field to `ProviderProfile`. The field is the
header name, not the header value. Its value is populated by the current
session conversation ID at runtime.

Example OpenCode Go profile:

```toml
[execution]
profile = "opencode_go"

[providers.opencode_go]
provider = "openai"
protocol = "opencode-go"
model = "kimi-k3"
base_url = "https://opencode.ai/zen/go/v1"
api_key = { env = "OPENCODE_API_KEY" }
session_header = "x-opencode-session"
timeout_s = 3600.0
max_retries = 2
```

The profile name is not semantically inspected. `protocol = "opencode-go"`
is an optional descriptive value for diagnostics and validation; the dynamic
header binding is the actual behavior switch. A generic gateway may use:

```toml
[providers.my_gateway]
provider = "openai"
model = "my-model"
base_url = "https://gateway.example/v1"
session_header = "x-gateway-session"
```

The exact API key/header setup remains provider-specific and must use the
existing secret-reference contract.

### 6.2 Legacy execution configuration

For backwards-compatible configurations that do not use a named profile, add
the same optional field to `[execution]`:

```toml
[execution]
provider = "openai"
model = "kimi-k3"
base_url = "https://opencode.ai/zen/go/v1"
api_key = { env = "OPENCODE_API_KEY" }
session_header = "x-opencode-session"
```

Profile configuration takes precedence when a profile is selected. If the
profile does not define a session header, the legacy execution field may be
used as the fallback, following the existing profile-to-execution merge rules.
An explicitly selected profile must not accidentally inherit a stale static
session header from an unrelated configuration layer.

### 6.3 Optional provider-aware shorthand

The implementation MAY provide a validated shorthand for the documented
OpenCode Go protocol:

```toml
protocol = "opencode-go"
```

If this shorthand automatically selects `x-opencode-session`, the behavior
MUST be documented, covered by tests, and overridable only through an explicit
safe setting. The preferred initial behavior is explicit `session_header` so
that endpoint detection never sends a custom header to an endpoint merely
because its hostname resembles OpenCode.

## 7. Session identity semantics

### 7.1 Authoritative source

The header value MUST be derived from:

```text
SessionConversation.conversation_id
```

The value is the existing validated session identifier. It is available before
the provider transport is built. A helper should expose the identity-to-header
operation at the configuration boundary rather than letting individual
workflows construct headers.

### 7.2 Lifecycle

| Lifecycle event | Required header value |
|---|---|
| New TUI/headless session | Newly allocated session/conversation ID. |
| `--continue` | The selected existing session ID. |
| `--resume SESSION_ID` | `SESSION_ID`, after ownership validation. |
| Mode switch | Unchanged session ID. |
| Workflow start/phase transition | Unchanged session ID. |
| Subagent call using the parent runner | Unchanged parent session ID. |
| Transport retry | Unchanged session ID on every attempt. |
| Process restart and resume | Same durable session ID. |
| New session after completion | New session ID. |

The workflow run ID remains useful for workflow checkpoints and recovery, but it
must not replace the conversation ID in the provider header. Multiple workflow
runs can intentionally belong to one user session and should remain routable
as one conversation when they share the session memory.

### 7.3 Value validation

Before building `LLMConfig`, the resolved header value MUST:

- be non-empty when dynamic binding is enabled;
- be a string;
- contain no carriage return or line feed;
- satisfy the existing safe session-identifier rules;
- be bounded to a documented maximum, such as 256 bytes; and
- never be generated as a fallback if the session identity is unavailable.

Missing identity is a configuration/programming error and must fail closed with
an actionable message. It must not silently send an empty header or use a random
per-request value.

## 8. Data flow

```text
CLI / TUI / headless entry point
        │
        ├─ resolve new or resumed session_id
        │
        ▼
SessionConversation.open(session_id)
        │
        ├─ durable journal and shared provider memory
        └─ conversation_id = session_id
        │
        ▼
load_config → resolve_provider_profile()
        │
        ├─ static default headers and secret references
        └─ session_header name
        │
        ▼
build_llm_config(execution, conversation_id=session_conversation.conversation_id)
        │
        ├─ resolve and validate dynamic header value
        ├─ merge static + session header case-insensitively
        └─ construct immutable LLMConfig.default_headers
        │
        ▼
_LazyAgentRunner → _build_transport(LLMConfig)
        │
        ▼
one session-owned AgentRunnerBase / transport
        │
        ├─ direct agent turns
        ├─ code_plan / create_workflow / custom workflow phases
        ├─ subagents and auxiliary turns
        ├─ compaction and cache-related provider requests
        └─ transport retries
        │
        ▼
HTTP request with x-opencode-session: <stable conversation_id>
```

The header is attached at transport construction rather than in a workflow
phase or individual call. This makes it impossible for one workflow to forget
the header and ensures retry attempts use the same value.

## 9. Proposed implementation

### 9.1 Configuration model

Update `src/agenthicc/config.py`:

1. Add `session_header: str = ""` (or an equivalent optional field) to
   `ExecutionSettings`, `ProviderProfile`, and `ResolvedProviderProfile`.
2. Parse, validate, merge, redact, and expose the field through existing
   profile and `config show` paths.
3. Reuse `_validate_header_name`; reject CR/LF and invalid token characters.
4. Preserve profile precedence and the no-profile legacy path.
5. Keep the header name non-secret. Only the dynamically resolved value should
   be passed to the in-memory LLM configuration.
6. Keep `protocol = "opencode-go"` as metadata unless the implementation
   explicitly adopts the optional shorthand in section 6.3.

### 9.2 LLM configuration builder

Change the internal API to accept an optional identity:

```python
build_llm_config(
    execution,
    *,
    conversation_id: str | None = None,
) -> LLMConfig
```

The default `None` preserves existing callers and test fixtures. When a
session-header name is configured, the builder MUST require a conversation ID,
validate it, and add the dynamic header to the copied `default_headers` mapping.
Header-name comparisons MUST be case-insensitive. A static header with the
same name must either be replaced only under an explicit documented rule or,
preferably, rejected as a conflicting configuration.

The builder must continue forwarding the resulting headers through lauren-ai's
`LLMConfig`; it must not mutate process-global environment variables or a
shared mutable mapping.

### 9.3 Session construction

Update `src/agenthicc/runners/tui_session.py` so the existing session ID is
passed when calling `build_llm_config`. The call must happen after the session
ID and `SessionConversation` identity are known and before the lazy provider
runner is created.

The headless path uses the same session-context construction and therefore must
inherit the behavior without a second implementation. Any other production
call site that builds a provider transport must either pass its stable session
ID or intentionally leave dynamic session headers disabled.

### 9.4 Auxiliary calls and transport reuse

The implementation must verify that all provider calls made through the normal
session runner use the same configured transport/header, including:

- direct `_run_agent_turn` calls;
- workflow phase turns;
- `create_workflow` generated workflows;
- subagent turns that reuse the parent runner;
- prompt compaction calls;
- provider-step retry calls; and
- resumed workflow execution.

If a future auxiliary path constructs a separate transport, its contract must
accept the same session identity and apply the same dynamic header binding.

### 9.5 Diagnostics and the reported error

The 400 `MissingSessionID` error must be classified as a configuration/provider
request error, not a transient transport failure. After the fix, a valid
OpenCode Go profile should not reach this error.

If the provider still returns the error, the TUI/headless diagnostic SHOULD say:

```text
OpenCode Go requires x-opencode-session. Configure
providers.<profile>.session_header = "x-opencode-session" and retry the session.
```

The diagnostic must not include API keys or full authorization headers. Retry
logic must not retry a 400 response unless a future provider-specific contract
explicitly marks it retryable.

## 10. Compatibility and migration

### Existing configurations

Configurations without `session_header` remain unchanged. They continue to
construct the same provider, model, static headers, query, request options,
timeouts, retries, cache flags, and transport.

### Existing OpenCode Go configurations

An operator must add the dynamic binding to the selected profile or execution
section. No journal or checkpoint migration is required because the session ID
already exists in the durable session and workflow identity records.

### Existing provider profiles

Unknown profile fields remain rejected unless the new field is added to the
typed allow-list. `config show` and redacted profile diagnostics must show the
header name but never the resolved session value if the value is considered
session-sensitive.

### lauren-ai compatibility

The feature depends on the lauren-ai release that supports
`LLMConfig.default_headers`. The existing provider-profile compatibility check
must fail early with an upgrade message when that field is unavailable. No
lauren-ai fork or provider-specific transport patch is required.

## 11. Security and privacy

- Header names are validated as HTTP token names.
- Session IDs and header values reject CR/LF to prevent header injection.
- API keys and authorization values remain `SecretReference`-backed and are
  never copied into checkpoints or conversation memory.
- Session IDs are identifiers, not credentials, but they can correlate traffic;
  they must not be printed in ordinary request logs beyond existing session
  metadata policy.
- Redacted configuration output should expose the configured header name and
  source type, not an unexpected runtime value.
- Cassette and debug logging behavior must be reviewed so custom headers are
  redacted consistently; the session header may be retained only where the
  local recording contract explicitly permits it.
- The feature must not weaken network allow-lists, approval policies, sandbox
  boundaries, or workflow capability filtering.

## 12. Acceptance criteria

| ID | Acceptance criterion |
|---|---|
| 187.1 | A profile with `session_header = "x-opencode-session"` sends that header with the configured session conversation ID. |
| 187.2 | `--resume SESSION_ID` sends `x-opencode-session: SESSION_ID` on every request. |
| 187.3 | `--continue` selects the existing session and sends the same stable header value rather than creating a new one. |
| 187.4 | Direct turns, workflow phases, generated custom workflows, subagents, compaction, and transport retries reuse the same header value. |
| 187.5 | A new session receives a different identity from prior sessions. |
| 187.6 | Existing configurations without a dynamic header produce no additional custom session header. |
| 187.7 | Legacy `[execution] session_header` and named-profile `session_header` follow documented precedence and merge behavior. |
| 187.8 | Invalid header names, CR/LF values, missing conversation IDs, oversized identities, and static/dynamic case-insensitive collisions fail before transport construction. |
| 187.9 | `config validate` accepts the OpenCode Go example when its API-key environment variable is present and reports missing required secrets without printing their values. |
| 187.10 | The transport receives an immutable header mapping; later changes to config or session state cannot mutate an already-built transport's identity. |
| 187.11 | A 400 `MissingSessionID` response is not retried as transient and produces an actionable configuration diagnostic. |
| 187.12 | No API key, authorization value, or secret header appears in checkpoints, journals, normal logs, redacted config output, or test cassette diagnostics. |
| 187.13 | OpenAI-compatible non-OpenCode providers continue to work with no header when the feature is not configured. |
| 187.14 | Provider request tests assert the header on main, auxiliary, and retry requests, not merely on the `LLMConfig` object. |

## 13. Test plan

### Unit tests

Add coverage for:

- parsing and redacting `session_header` in execution and provider profiles;
- header-name validation and case-insensitive collisions;
- safe session-ID validation and bounded values;
- `build_llm_config(..., conversation_id=...)` output;
- missing identity and missing secret failures;
- profile precedence and legacy fallback;
- unchanged behavior when the feature is disabled;
- redacted diagnostics and no secret leakage.

### Integration tests

Use a fake lauren-ai/OpenAI-compatible transport or request client to assert
that actual outgoing requests carry:

- `x-opencode-session` on the first request;
- the same value on a second turn;
- the same value after a provider retry;
- the same value in a workflow phase and subagent path; and
- no duplicate or stale static header.

Test both TUI session construction and the shared headless construction path.
Use a temporary home/project configuration and deterministic session IDs.

### End-to-end tests

Add a local OpenAI-compatible HTTP fixture that returns a structured
`MissingSessionID` 400 when the header is absent and a deterministic response
when it is present. Verify:

1. an OpenCode Go profile without the binding fails with the actionable config
   diagnostic or provider error;
2. the configured binding succeeds;
3. `--continue` preserves the header value;
4. `--resume` preserves the header value after process reconstruction; and
5. a transient 429 followed by success keeps the same header on both requests.

No live OpenCode or paid provider calls are permitted in automated tests.

## 14. Rollout and observability

1. Ship the typed configuration and builder support disabled by default.
2. Document the OpenCode Go profile in the provider configuration guide.
3. Expose only redacted effective profile metadata in `config show` and startup
   diagnostics.
4. Add a debug-level log field indicating that a dynamic session header was
   attached, without logging its value or any secret headers.
5. Monitor 400 `MissingSessionID` responses in test/staging fixtures and verify
   that no retry storm occurs.
6. If a provider rejects custom headers despite configuration, report the
   provider/status/category without leaking the request map.

## 15. Implementation notes and decisions

- The stable session identity already exists; the implementation should pass it
  into the configuration builder instead of introducing another ID registry.
- Header injection belongs at the transport boundary, not in workflows or
  phase prompts. This guarantees consistent behavior for custom workflows
  created by `create_workflow`.
- The binding is configured by header name only. The runtime supplies the
  session value, preventing operators from accidentally hard-coding one
  session across all runs.
- The first implementation should prefer explicit configuration over hostname
  inference. Automatic host detection can be added later only with a clear
  protocol marker and tests.
- The existing static `default_headers` feature remains the mechanism for API
  keys, authorization, tenant, and vendor headers; dynamic session identity is
  deliberately a separate typed field.

## 16. Implementation evidence

Implemented in the current source tree:

- `ProviderProfile` and `ExecutionSettings` parse an optional `session_header`,
  preserve it through profile resolution, and expose it in redacted
  configuration output without storing a runtime session value.
- `build_llm_config(..., conversation_id=...)` validates and copies the
  dynamic identity into lauren-ai's `LLMConfig.default_headers`, rejects
  case-insensitive collisions in static and request-option headers, and keeps
  existing no-binding callers unchanged.
- `TUISession` passes the existing session ID at transport construction; the
  headless path inherits this behavior through the shared session-context
  builder. Subagents, workflow turns, compaction, and retries reuse the
  session-owned transport.
- 400 `MissingSessionID` errors are not classified as transient and produce an
  actionable TUI error detail without exposing credentials.
- Provider configuration, the generated TOML template, README, usage guide,
  full LLM reference, and PRD index document the OpenCode Go profile.

Verification evidence:

```text
111 passed — focused unit/config/provider-profile regression suite
6 passed — provider transport integration and OpenCode Go resume E2E suite
24 passed — project bootstrap/config-template regression suite
```

## 17. Definition of done

- This PRD's configuration, builder, transport, diagnostic, and lifecycle
  behavior is implemented without duplicating provider clients.
- Direct, workflow, subagent, compaction, retry, resume, and headless paths
  have integration or E2E evidence.
- Existing provider-profile and no-profile tests remain green.
- New OpenCode Go fixture tests pass without network credentials.
- Documentation explains the TOML configuration and the stable identity
  lifecycle.
- `ruff`, formatting, type-audit, relevant mypy checks, and the full
  deterministic test matrix pass, with external dependency blockers reported
  explicitly.
