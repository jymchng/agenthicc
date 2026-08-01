---
title: "PRD-162: Provider Connection Profiles and OpenAI-Compatible Endpoints"
status: Proposed
version: 1.0.0
created: 2026-08-01
source_commit: "lauren-ai 71fc2080629c0dc90bf909f3a635f87299c33e51"
related_prds:
  - PRD-07
  - PRD-21
  - PRD-126
  - PRD-132
  - PRD-133
  - PRD-136
  - PRD-150
tags:
  - configuration
  - providers
  - modal
  - openai-compatible
  - security
  - workflows
---

# PRD-162 — Provider Connection Profiles and OpenAI-Compatible Endpoints

## 1. Executive summary

agenthicc currently exposes a small provider configuration surface:
provider, model, api_key, and base_url, plus retry and token settings. That
works for the default Anthropic, OpenAI, Ollama, and LiteLLM paths, but cannot
express the connection details required by many modern gateways and hosted
inference endpoints.

The latest lauren-ai commit studied for this PRD is:

~~~text
71fc208 feat: allow additional request options to be passed to providers
~~~

That commit adds immutable RequestOptions, typed OpenAI and Anthropic request
options, LLMConfig.default_headers, LLMConfig.default_query,
LLMConfig.client_options, LLMConfig.request_options, top_p,
max_completion_tokens, native provider services, provider capability metadata,
duplicate-field rejection, header-injection validation, immutable mapping
snapshots, redacted diagnostics, and forwarding through direct LLM calls and
the agent runner.

This PRD brings those capabilities to agenthicc through named, validated
connection profiles. A profile can point the existing OpenAI transport at a
Modal Shared API or dedicated endpoint, vLLM, TGI, LiteLLM, Together, Groq,
DeepSeek, OpenRouter, a private gateway, or another OpenAI-compatible service.
It can also configure Anthropic-compatible and Ollama-compatible endpoints
without adding a vendor-specific provider for each service.

The feature is an adapter over lauren-ai. Modal is not a new hard-coded
provider and does not require a Modal SDK dependency. The selected lauren-ai
provider remains responsible for translating messages, tools, streaming,
retries, and response usage.

## 2. Evidence and current-state findings

### 2.1 Relevant changes in lauren-ai 71fc208

| lauren-ai change | Relevance to agenthicc |
|---|---|
| LLMConfig.default_headers and default_query | Gateway authentication and tenant routing on every request. |
| LLMConfig.client_options | Validated SDK client controls such as a caller-owned HTTP client. |
| LLMConfig.request_options | Immutable defaults merged with per-call options. |
| RequestOptions.extra_headers, extra_query, extra_body | Provider-specific request fields without expanding the common protocol. |
| RequestOptions.provider | Known fields such as OpenAI reasoning_effort and Anthropic thinking. |
| top_p and max_completion_tokens | Sampling and provider-native output limits. |
| AgentConfig forwarding | Agent loops can send the same options as direct LLM calls. |
| Duplicate rejection and redaction | No silent request overwrites and safe diagnostics. |
| ProviderCapabilities and native services | Explicit capability reporting and a boundary for native APIs. |

The commit's provider compatibility guide demonstrates a Modal-shaped setup with
base_url, Modal-Key, Modal-Secret, top_p, and RequestOptions. The equivalent
configuration must be available to an agenthicc operator without Python code or
exposed credentials.

### 2.2 Current agenthicc boundary

src/agenthicc/config.py defines ExecutionSettings with provider, model,
api_key, and base_url. build_llm_config() forwards those fields to lauren-ai
and applies prompt-cache and SDK-retry settings, but does not forward:

- default headers or query parameters;
- client_options;
- RequestOptions;
- extra_body or provider-specific request fields;
- top_p or max_completion_tokens;
- an endpoint/profile identity;
- a validated secret reference for custom headers;
- provider capability declarations.

The interactive path in src/agenthicc/runners/agent_turn.py builds a lauren-ai
AgentConfig and calls the streaming agent runner. The adapter must configure
both immutable LLMConfig and per-run AgentConfig; configuring only LLMConfig
would make direct calls work while workflows and TUI turns silently ignore
the new options.

The existing configuration loader provides foundations this PRD preserves:
built-in/user/project/CLI/environment layering, recursive TOML merging,
extends support, provider shorthand variables, provider-independent session
memory, workflow state, usage accounting, journaling, and retry ownership.

## 3. Problem statement

A Modal endpoint commonly needs all of the following at once:

- an OpenAI-compatible base URL;
- a model name absent from the OpenAI registry;
- an API credential plus gateway headers such as Modal-Key and Modal-Secret;
- non-default temperature or top-p;
- provider-specific body fields such as reasoning or vendor tracing;
- a provider-native completion-token field;
- a timeout and retry policy appropriate for hosted inference;
- selection without rewriting the project's normal provider configuration.

The current configuration expresses only the first two items and one API key.
Operators must write custom Python integration, mutate process-wide SDK state,
or use a gateway-specific workaround. Those paths are hard to validate,
reproduce, and secure.

The same limitation affects many services. Treating each vendor as a new
provider would create an unmaintainable matrix of names, dependencies,
authentication rules, and transports. The portable OpenAI-compatible path
should be first-class.

## 4. Goals

The implementation MUST:

1. Add named, reusable endpoint profiles to the existing layered TOML model.
2. Support Modal and other OpenAI-compatible endpoints through
   provider = "openai", without a Modal-specific runtime dependency.
3. Forward lauren-ai connection and request options through direct turns,
   workflow phases, subagents, headless execution, and the TUI.
4. Preserve all existing execution configurations without migration.
5. Resolve credentials from environment variables or typed secret references.
6. Validate URLs, headers, option types, numeric ranges, profile references, and
   duplicate request fields before a provider call.
7. Keep secrets out of config output, diagnostics, events, journals, usage
   records, workflow checkpoints, cassettes, exceptions, and ordinary logs.
8. Expose effective, redacted profile information for diagnosis.
9. Preserve lauren-ai immutable mapping and per-call merge semantics.
10. Make provider capabilities explicit enough to reject incompatible settings.
11. Keep optional provider SDKs lazy and installable only for the selected family.
12. Add deterministic unit, integration, and E2E coverage for every criterion.

## 5. Non-goals

This PRD does not:

- add a special Modal provider class or require a Modal SDK;
- promise that every vendor API is OpenAI-compatible;
- permit arbitrary Python imports, callbacks, HTTP clients, or executable code
  from TOML;
- change network security policy to allow every destination;
- store provider secrets in conversation memory or workflow checkpoints;
- silently switch providers in the middle of a turn;
- let a profile override security, tool, approval, sandbox, or workflow gates;
- replace lauren-ai with a second HTTP client;
- expose all native OpenAI Responses or Realtime features through agenthicc's
  agent loop in the first release;
- make per-phase provider switching implicit.

## 6. Product concepts

### 6.1 Connection profile

A connection profile is a named, typed description of one provider endpoint. It
contains connection fields, request defaults, and optional capability metadata.
The active profile is selected by execution.profile, the
AGENTHICC_EXECUTION_PROFILE environment variable, or a CLI override.

If no profile is selected, the existing execution fields are used exactly as
they are today.

### 6.2 Provider versus endpoint

provider selects the lauren-ai transport and message/tool protocol. base_url
selects the service endpoint. A vendor that speaks OpenAI Chat Completions uses
provider = "openai" regardless of whether it is OpenAI, Modal, vLLM, LiteLLM,
Together, or a private gateway.

Initial provider values remain those supported by the lauren-ai version pinned
by agenthicc: anthropic, openai, ollama, and litellm. A new vendor name MUST
NOT be accepted merely to make a configuration look supported. A new provider
requires a lauren-ai transport or a reviewed adapter with tests and an
optional-dependency boundary.

### 6.3 Connection defaults versus request defaults

| Profile section | Lauren object | Applies to |
|---|---|---|
| base_url, default_headers, default_query, client_options, timeout, retries | LLMConfig | Client construction and every request. |
| request_options | LLMConfig.request_options and AgentConfig.request_options | Every model call. |
| temperature, top_p, max_completion_tokens | LLMConfig and effective AgentConfig | Direct calls and agent turns. |
| future per-call options | lauren-ai RequestOptions override | One call only; cannot mutate defaults. |

All mappings are copied and treated as immutable after resolution.

## 7. Proposed configuration contract

### 7.1 Backwards-compatible configuration

The following remains valid with the same meaning:

~~~toml
[execution]
provider = "openai"
model = "gpt-4o"
base_url = ""
api_key = ""
max_output_tokens = 16384
llm_sdk_max_retries = 2
~~~

Existing provider shortcuts remain valid:

~~~bash
OPENAI_API_KEY="..." \
OPENAI_MODEL="gpt-4o" \
OPENAI_BASE_URL="https://api.openai.com/v1" \
uv run agenthicc
~~~

No existing configuration may require profile, header, or dependency changes.

### 7.2 Modal profile example

~~~toml
[execution]
profile = "modal_kimi"
max_output_tokens = 16384
transport_max_retries = 3
llm_sdk_max_retries = 2

[providers.modal_kimi]
provider = "openai"
model = "moonshotai/Kimi-K3"
base_url = "https://pythontextbooks--ep-kimi-k3-server.us-west.modal.direct/v1"
api_key_env = "MODAL_API_KEY"
timeout_s = 120.0
temperature = 0.3
top_p = 0.95
max_completion_tokens = 16384

[providers.modal_kimi.default_headers]
"Modal-Key" = { env = "MODAL_KEY" }
"Modal-Secret" = { env = "MODAL_SECRET" }

[providers.modal_kimi.request_options.provider]
reasoning_effort = "none"

[providers.modal_kimi.request_options.extra_body]
vendor_trace = true
~~~

The invocation is:

~~~bash
export MODAL_API_KEY="unused-or-the-endpoint-api-key"
export MODAL_KEY="..."
export MODAL_SECRET="..."
uv run agenthicc --set execution.profile=modal_kimi
~~~

The URL, model, and authentication variable names are operator values; the
implementation must not hard-code them.

### 7.3 Typed profile model

The typed configuration model MUST introduce equivalent concepts. Names may
follow project conventions, but the semantics are normative:

~~~text
AgenthiccConfig.providers: dict[str, ProviderProfile]
ExecutionSettings.profile: str

ProviderProfile:
  provider: str
  model: str
  base_url: str
  api_key: str | empty
  api_key_env: str | empty
  default_headers: dict[str, SecretOrString]
  default_query: dict[str, TOMLValue]
  client_options: dict[str, TOMLValue]
  request_options: RequestOptionSettings
  timeout_s: positive float
  sdk_max_retries: non-negative int
  temperature: float | absent
  top_p: float | absent
  max_completion_tokens: non-negative int | absent
  protocol: chat_completions | messages | ollama | absent
  capabilities: ProviderCapabilityHints | absent
~~~

api_key_env is a variable name, not a credential. It is resolved only while
constructing in-memory LLMConfig. Existing literal execution.api_key remains
supported for compatibility, but all display and diagnostics show REDACTED.

Secret-valued map entries use a typed environment reference:

~~~toml
[providers.gateway.default_headers]
Authorization = { env = "GATEWAY_AUTHORIZATION" }
X-Tenant = { env = "GATEWAY_TENANT" }
~~~

The resolver MUST reject malformed references, missing required variables,
unknown keys in reference objects, and non-string resolved header values.
Resolved values are never written back to configuration or session files.

For one-off invocations, the CLI MUST also support
`--set-secret PATH=ENV_VAR`. It stores the same `SecretReference` object as the
TOML `{ env = "NAME" }` form, supports dotted paths such as
`execution.default_headers.Modal-Key` and `execution.api_key`, and resolves
the named variable only during validation/provider construction. The secret
value MUST NOT be present in process arguments, diagnostics, logs, journals,
checkpoints, exports, or cassettes. Missing variables and malformed paths are
validation errors. Existing `--set PATH=VALUE` behavior remains unchanged.

client_options is restricted to TOML-safe, allowlisted SDK options. A custom
httpx client or callback can be supplied only by a trusted programmatic
adapter, not by TOML. Arbitrary callables, classes, import paths, proxies, and
file handles from config are rejected.

### 7.4 Request option model

The TOML representation MUST map to lauren-ai RequestOptions:

~~~toml
[providers.modal_kimi.request_options]
include_raw_response = false
timeout_s = 120.0

[providers.modal_kimi.request_options.provider]
reasoning_effort = "none"

[providers.modal_kimi.request_options.extra_headers]
X-Request-Mode = "agenthicc"

[providers.modal_kimi.request_options.extra_query]
tenant = "research"

[providers.modal_kimi.request_options.extra_body]
vendor_trace = true
~~~

The adapter MUST preserve false and zero values. It MUST reject collisions
where extra_body or provider attempts to redefine a canonical request field,
matching lauren-ai duplicate-field behavior. Nested maps are copied,
validated, and recursively redacted for diagnostics.

### 7.5 Capability hints

Profiles MAY declare hints such as tools, streaming, structured_output,
thinking, vision, and embeddings. They are validation and visibility hints,
not a security permission system. A false tools hint must prevent a
tool-enabled session with a clear error; a true hint does not bypass
agenthicc tool gates or prove that a remote server implements the feature.

The resolver should prefer lauren-ai ProviderCapabilities when available, then
apply explicit profile hints subject to validation. Unknown capability names
are errors, not ignored keys.

## 8. Resolution and precedence

### 8.1 Source precedence

The existing order remains:

~~~text
built-in defaults
  < user-global TOML
  < project TOML / --config
  < AGENTHICC_* and provider shortcut environment variables
  < CLI --set / --set-secret overrides
~~~

All sources are merged before profile normalization, so an extends parent can
define a reusable profile and a project can override one field.

### 8.2 Connection-field precedence

For each connection field:

~~~text
explicit CLI field override
  > explicit AGENTHICC_* field override
  > provider shortcut override
  > selected profile field
  > legacy [execution] field
  > built-in provider default
~~~

execution.profile itself follows normal source precedence. If a selected
profile omits model, base_url, or a credential reference, the legacy execution
value is used. Runtime controls such as max agent turns, context windows,
compaction, and transport-level retry remain top-level execution settings.

Profile selection is fixed at session construction. The existing /model command
may change model according to its current contract, but must not silently
replace endpoint, credentials, request body, or security policy. A future
profile-switch command must create an explicit session event and revalidate.

### 8.3 Environment names

The implementation MUST support:

- AGENTHICC_EXECUTION_PROFILE;
- AGENTHICC_EXECUTION_PROVIDER, MODEL, and BASE_URL;
- OPENAI_MODEL and OPENAI_BASE_URL;
- profile-local api_key_env and { env = ... } references.

A shorthand such as AGENTHICC_PROFILE may be added only if it maps to the same
setting and precedence rules.

## 9. Data flow

The effective data path MUST be:

~~~text
TOML files + environment + CLI --set
              |
              v
       raw layered mapping
              |  merge, precedence, extends checks
              v
      ProfileResolver / validation
              |  selection, secret lookup, URL/header checks
              v
  ResolvedProviderProfile (immutable + redacted projection)
              |
              +-- connection fields -----------+
              |                                 v
              |                          lauren-ai LLMConfig
              |                          (client defaults + RequestOptions)
              |                                 |
              v                                 v
   agenthicc session/workflow context    lauren-ai transport/client
              |                                 |
              | per-run sampling/options         | SDK request
              v                                 v
      lauren-ai AgentConfig --------------> endpoint URL
                                                |
                         Modal / compatible / provider server
~~~

Secrets take a separate path:

~~~text
environment variable
       |
       v
in-memory resolved config --> SDK client/request headers
       |
       +--> redacted config display
       +--> redacted diagnostics and errors
       +--> never serialized into memory, journal, checkpoint, or cassette
~~~

The profile name, provider, model, URL with credentials removed, and capability
summary may be recorded for observability. Header values, API keys,
authorization tokens, resolved secret values, and sensitive request-body keys
must be redacted.

## 10. Runtime integration requirements

### 10.1 Configuration adapter

agenthicc.config MUST add one conversion boundary that:

1. resolves a profile into typed values;
2. constructs lauren-ai RequestOptions and typed provider options;
3. passes default_headers, default_query, client_options, timeout, top_p, and
   max_completion_tokens into LLMConfig;
4. preserves prompt-cache and the existing split between SDK and turn retries;
5. returns an immutable result suitable for one session;
6. exposes a redacted diagnostic projection.

build_llm_config() remains the canonical adapter. A second builder in workflow,
TUI, or CLI code is prohibited.

### 10.2 Agent turns and workflows

Effective profile options MUST reach the same AgentConfig used by ordinary
interactive turns, headless JSON/stdin execution, code_plan, create_workflow,
generated custom workflows, other workflow plugins, and subagents subject to
their existing budget and tool restrictions.

Workflow phase model overrides may change only the model under the current
workflow contract. They must not drop profile headers, RequestOptions, retries,
or capability validation. If a future phase selects another profile, it must
create an explicit resolved connection and define provider-memory semantics.

Generated workflows require no Modal-specific code. They inherit the session
profile through the same runner and therefore receive the same checkpoint,
memory, tool, and provider-option behavior as built-in workflows.

### 10.3 Streaming and API surface

The current interactive runner is streaming-first and that default MUST remain.
If a profile declares capabilities.streaming = false, the session must either
use the existing non-streaming runner and normal event projection, or fail
before the first request with an actionable error. It must not send stream=true
to a profile that explicitly disables it.

OpenAI Responses, Realtime, and other native services from the latest
lauren-ai commit are a separate capability surface. The first release may
report their availability but must not claim agent-loop support without an
explicit adapter.

## 11. CLI and operator experience

The feature MUST provide:

~~~text
agenthicc config validate
agenthicc config profiles
agenthicc config show
~~~

Expected behavior:

- validate resolves the selected profile, validates all fields, and reports
  missing environment variables without printing values;
- profiles lists names, provider, model, redacted endpoint, and capabilities;
- show displays effective redacted configuration and identifies the selected
  profile and field source when practical;
- all commands use the same load_config() and resolver as runtime startup;
- errors include a field path and repair suggestion;
- profile secrets never appear in shell-copyable output.

The existing --set execution.profile=modal_kimi path MUST work. A profile flag
may be added only if it maps to that same setting and precedence.

## 12. Security and privacy requirements

1. Accept only http and https endpoint URLs. Reject embedded URL credentials,
   unsupported schemes, and malformed hosts.
2. Validate header names and values; reject CR/LF and request-smuggling inputs.
3. Keep destination authorization in NetworkGuard and the existing security
   policy. A profile does not bypass allow-lists or sandbox restrictions.
4. Never log raw api_key, default_headers, extra_headers, authorization values,
   resolved secret values, or sensitive extra_body keys.
5. Redact recursively in mappings and lists, including keys containing token,
   secret, password, credential, authorization, or api_key.
6. Do not put resolved secrets into config snapshots serialized for resume,
   workflow checkpoints, journals, usage records, exports, or cassettes.
7. Do not allow client_options to load arbitrary classes, import paths,
   proxies, or file handles from TOML.
8. Do not echo secrets in endpoint-response validation errors.
9. Preserve fail-closed behavior when a destination is not permitted.
10. Add tests scanning diagnostics, exception strings, event payloads, and
    cassette output for known secret values.

## 13. Errors and compatibility

The resolver MUST fail before a provider request for an unknown profile or
provider, missing required secret, invalid URL/header/query/body, invalid
timeout/retry/temperature/top-p/token value, duplicate request field,
unsupported required capability, or configured option unsupported by the
installed lauren-ai version.

Errors must name the path, for example
providers.modal_kimi.default_headers.Modal-Key, while showing only the
environment variable name.

Existing inputs retain these results:

| Existing input | Required result |
|---|---|
| No provider section | Existing Anthropic default path. |
| execution provider/model/base_url/api_key | Equivalent provider and model. |
| OPENAI_* shortcuts | Existing inference and precedence. |
| Ollama host configuration | Existing Ollama transport and URL behavior. |
| LiteLLM configuration | Existing LiteLLM selection. |
| Workflow phase model override | Model-only override remains model-only. |
| --resume or --continue | Profile identity and non-secret fields are restored or deterministically re-resolved; secrets come from current environment. |
| Missing optional SDK | Unrelated providers remain usable; selected provider gives an install hint. |

The minimum lauren-ai version MUST contain RequestOptions and the LLMConfig
fields used here. If older releases remain supported, feature detection must
fail explicitly for configured unsupported options; silently dropping options
is prohibited.

## 14. Acceptance criteria

### A. Profile loading and precedence

1. A project profile can be selected with --set execution.profile=<name>.
2. User-global profiles are inherited and project fields override them.
3. Environment and CLI overrides follow section 8.
4. Unknown profiles and malformed values fail before an LLM request.
5. Legacy configuration produces an equivalent resolved profile.

### B. Modal/OpenAI-compatible connectivity

6. The Modal example resolves provider openai, model, URL, and headers in
   memory.
7. The OpenAI client receives base URL, default headers/query, timeout, and
   retry settings.
8. Agent turns send temperature, top_p, token limits, provider options, extra
   headers/query, and extra body exactly once.
9. A fake OpenAI-compatible server observes valid expected requests in both
   streaming and non-streaming modes.
10. No Modal SDK is imported or required by a base installation.

### C. Other provider families

11. Anthropic profiles forward custom URL, headers, query, request options,
    top-p, and thinking options.
12. Ollama profiles preserve host behavior and translate supported body options
    without sending OpenAI-only fields incorrectly.
13. LiteLLM and mock/cassette paths remain usable; unsupported options produce a
    clear error or documented no-op, never a silent request change.

### D. Security and observability

14. Config output, logs, errors, exports, journals, checkpoints, usage records,
    and cassettes contain no resolved secret value.
15. Header injection, invalid schemes, embedded credentials, missing secrets,
    and unsafe client options are rejected.
16. NetworkGuard still decides whether the endpoint is allowed.
17. Concurrent sessions cannot mutate one another's mappings or defaults.

### E. Workflows and resume

18. code_plan, create_workflow, and a generated custom workflow send the same
    profile options as an ordinary turn.
19. Resume rehydrates profile identity and non-secret settings but resolves
    secrets from current environment.
20. A changed or missing secret produces a preflight error, not a partial
    request or transcript hang.

## 15. Testing strategy

### 15.1 Unit tests

Add tests for TOML conversion, profile selection, source precedence, extends
merges, environment references, missing variables, URL/header/query/numeric/
protocol/capability validation, RequestOptions construction and immutability,
false/zero preservation, recursive redaction, duplicate detection,
build_llm_config forwarding for every provider, AgentConfig forwarding,
legacy equivalence, secret-free errors, and capability negotiation.

### 15.2 Integration tests

Use injected fake SDK clients and existing lauren-ai mock/recording transports;
never use real paid endpoints. Cover:

- OpenAI client construction with default headers/query and client defaults;
- streaming and non-streaming request capture;
- Modal-shaped headers plus extra_body, top_p, and max_completion_tokens;
- Anthropic and Ollama option translation;
- timeout, retries, and error classification;
- full agent-runner propagation including tools;
- code_plan and create_workflow profile inheritance;
- resume with current-environment secret resolution;
- NetworkGuard allow and deny;
- concurrent profile use without mutable-map leakage;
- cassette recording/replay with redacted request metadata.

### 15.3 End-to-end tests

Add deterministic local-server journeys that:

1. run headless agenthicc from a temporary OpenAI-compatible profile and assert
   received headers and request fields;
2. run the same profile through the TUI/session runner and verify streaming,
   usage, and error projection;
3. run code_plan, create_workflow, and a generated workflow using a fake
   transport and verify common propagation;
4. run config validate, profiles, and show with secrets in the environment and
   assert redaction;
5. resume after changing a secret and assert the fake endpoint uses the new
   value while the transcript remains clean;
6. run with an absent optional SDK and verify unrelated providers still start
   and the selected provider gives a precise install hint.

Tests must use temporary config roots and patched environments. No test may
depend on the operator home directory, real credentials, network availability,
current clock, or a vendor response format outside a local fixture.

## 16. Implementation plan

### Phase 1 — Version and contract boundary

1. Pin or constrain the lauren-ai minimum containing commit 71fc208 behavior.
2. Add typed profile and request-option settings.
3. Define one immutable resolved-profile object and redacted projection.
4. Add validation and precedence tests before runner wiring.

### Phase 2 — Runtime adapter

1. Extend load_config() and _dict_to_config() for profiles.
2. Extend build_llm_config() to pass lauren-ai connection/request fields.
3. Extend interactive and headless paths to pass sampling and request options
   into AgentConfig.
4. Preserve cache, retry, context-window, usage, memory, and workflow
   ownership boundaries.

### Phase 3 — CLI, diagnostics, and docs

1. Implement config validate and config profiles with the shared resolver.
2. Add redacted effective-config output.
3. Document Modal, OpenAI-compatible, Anthropic-compatible, Ollama, LiteLLM,
   and local-server examples.
4. Document that compatible endpoints use provider openai and need no vendor SDK.

### Phase 4 — Integration and E2E verification

1. Add fake-SDK and local-server request assertions.
2. Exercise built-in and generated workflows.
3. Add resume, cassette, redaction, concurrency, and optional-dependency tests.
4. Run lint, formatting, mypy, type-audit, unit, integration, and E2E gates.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| lauren-ai option drift | Pin minimum version and fail on configured unsupported options. |
| Secret leakage through diagnostics | Centralize redaction and scan all persisted/output surfaces. |
| Profile precedence surprises | One raw mapping, documented field precedence, redacted source metadata. |
| Different compatible-server subsets | Explicit capabilities and preflight validation. |
| Headers bypass network policy | Destination authorization remains in NetworkGuard. |
| Mutable maps race across sessions | Immutable snapshots and concurrency tests. |
| Native services mistaken for agent-loop support | Keep native API surfaces separate and report honestly. |
| Phase provider switching fragments memory | Session-wide selection in this PRD; defer phase switching. |

## 18. Documentation and release requirements

The implementation must update:

- docs/guides/configuration.md with profile schema and precedence;
- docs/guides/custom-workflows-and-config.md with workflow inheritance;
- docs/guides/quickstart.md with local and compatible-endpoint examples;
- README.md with provider families and optional SDK guidance;
- docs/reference/storage.md if profile identity or redaction metadata is persisted;
- llms-full.txt and llms.txt for public configuration symbols;
- prds/README.md with this PRD;
- generated reference documentation for public dataclasses.

Release notes must state that Modal support means OpenAI-compatible transport
support and that endpoint access remains subject to agenthicc network policy.

## 19. Definition of done

This PRD is ready for implementation sign-off when:

1. all acceptance criteria pass;
2. legacy provider configurations and workflows pass unchanged;
3. Modal-shaped requests are verified against a deterministic local fixture;
4. lint, formatting, mypy, type-audit, unit, integration, and E2E checks pass;
5. no configured secret appears in diagnostics or persisted artifacts;
6. the profile resolver is the only configuration-to-lauren adapter;
7. documentation and generated public-reference surfaces are updated;
8. unsupported providers/options fail before network I/O;
9. optional provider SDKs remain optional and lazy;
10. maintainers approve migration and rollback procedures.
