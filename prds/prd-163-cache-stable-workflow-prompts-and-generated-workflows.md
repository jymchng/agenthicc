---
title: "PRD-163: Cache-Stable Workflow Prompts and Generated Workflows"
status: Implemented
version: 1.0.0
created: 2026-08-01
related_prds:
  - PRD-131
  - PRD-132
  - PRD-147
  - PRD-152
  - PRD-153
  - PRD-156
  - PRD-157
tags:
  - prompt-caching
  - workflows
  - create-workflow
  - lauren-ai
  - performance
  - generated-code
  - observability
---

# PRD-163 — Cache-Stable Workflow Prompts and Generated Workflows

## 1. Executive summary

agenthicc already enables prompt caching through `ExecutionSettings.prompt_cache`
and the lauren-ai configuration. The current workflow architecture, however,
does not make the boundary between reusable prompt material and changing phase
material explicit. A workflow phase commonly rebuilds one large system prompt
containing the stable workflow contract, phase instructions, user intent,
artifacts, validation reports, transition instructions, skills, and tool
descriptions. In the current Anthropic transport, that complete system prompt
is one cacheable block. A phase transition, retry, question, summary update, or
tool-registry change can therefore invalidate much more cached material than
the changed data requires.

The problem is more serious for `CreateWorkflowRunner`. It is the factory for
downstream workflows, yet it currently teaches generated runners primarily how
to preserve session memory and use `CodePlanRunner.run_phase`. Without an
explicit cache contract, a generated workflow can appear correct while
rebuilding its stable prompt prefix on every phase or bypassing the shared
runner entirely. That causes every workflow produced by `create_workflow` to
inherit avoidable cache misses.

This PRD introduces a provider-aware, structured prompt-cache contract. The
contract separates immutable session/workflow instructions from dynamic
phase context, keeps tool schemas deterministic, and makes cache epochs and
unavoidable invalidations observable. The common workflow runners enforce the
contract at runtime. `CreateWorkflowRunner` teaches agents to author against
the contract, provides inspection/example tools, and rejects generated
workflows that cannot demonstrate cache-stable execution.

The objective is not to promise that a cache can never invalidate. Provider
TTL expiry, a model/provider/profile change, a real stable-contract change, and
history compaction can legitimately invalidate a cache. The objective is to
ensure that changing phase state, phase output, user questions, transition
instructions, or rolling summaries does not rewrite the reusable system and
tool prefix.

## 2. Problem statement

### 2.1 Current behavior

The relevant current paths are:

| Concern | Current implementation | Consequence |
|---|---|---|
| Agent-turn prompt assembly | `src/agenthicc/runners/agent_turn.py` combines the configured base prompt, `system_prompt_suffix`, skills, and the tool registry description | Stable and dynamic content are assembled into one changing string |
| Generic workflows | `src/agenthicc/workflows/default/runner.py` builds a phase prompt and invokes `_run_agent_turn` for each phase turn | Phase transitions can rewrite the cacheable system prompt |
| Code-plan family | `src/agenthicc/workflows/code_plan/runner.py` exposes `run_phase()` and is inherited by make-tool, EPUB, PDF, and site-imitate runners | A good common boundary exists but does not yet expose a cache contract |
| Workflow creation | `src/agenthicc/workflows/create_workflow/runner.py` has its own `DESIGN → GENERATE → VALIDATE → SUMMARIZE` state machine and directly invokes `_run_agent_turn` | Generated-workflow authoring itself can bypass the future common prompt boundary |
| Prompt caching | `config.py` maps `prompt_cache` to lauren-ai `cache_system_prompt`, `cache_tools`, and `cache_conversation` | The setting enables caching, but does not define which content belongs in each cacheable region |
| lauren-ai system cache | Current Anthropic transport places the complete system string into a cacheable text block when enabled | A dynamic suffix can invalidate the stable prefix |
| Conversation cache | The last conversation message is marked as a cache breakpoint | Append-only turns are reusable; compaction or history rewriting necessarily changes the history prefix |
| Memory summary | lauren-ai appends the rolling summary to the built system string | A summary update can change the complete system cache block |

The exact provider behavior differs. Anthropic exposes explicit system, tool,
and conversation cache breakpoints. OpenAI-compatible providers may cache a
stable prefix automatically. Ollama and other providers may ignore caching.
agenthicc must preserve one logical contract across providers while using the
strongest supported transport representation for each provider.

### 2.2 User impact

Unnecessary invalidation increases latency and input-token cost during long
workflow sessions. It is especially visible when an agent moves through many
small phases, retries a validation step, asks a clarifying question, or resumes
after an interruption. It also makes generated workflows unpredictable: two
workflows with equivalent phase graphs may have very different cost and cache
hit behavior based only on how their author assembled prompts.

### 2.3 Design principle

Prompt caching is an execution concern, but cache stability is an authoring
concern too. Every built-in runner and every workflow generated by
`create_workflow` must use the same typed prompt/tool composition boundary.
Generated code must not have to reverse-engineer cache semantics from private
functions or provider-specific behavior.

## 3. Goals

1. Define a typed, provider-neutral representation for stable prompt content,
   dynamic phase context, conversation history, and tool groups.
2. Keep the reusable system prefix byte-for-byte stable across phase turns,
   retries, questions, resumptions, and rolling-summary updates when the
   workflow contract and execution configuration have not changed.
3. Keep stable tool schemas ordered and fingerprinted deterministically, while
   allowing phase-local tools and transition tools to change without rewriting
   the stable tool block.
4. Apply the same behavior to the generic runner, `CodePlanRunner` and all of
   its subclasses, and `CreateWorkflowRunner`.
5. Make generated workflows cache-stable out of the box when they follow the
   supported runner contract.
6. Give `create_workflow` agents precise prompt guidance, examples, inspection
   tools, and validation diagnostics for authoring optimized workflows.
7. Require generated workflows to include a stable system-prompt instruction
   that the agent should ask the user clarifying questions whenever required
   information is missing, ambiguous, or materially affects the result.
8. Preserve existing workflow semantics: phase transitions still occur only
   through the existing transition tools, capabilities remain enforced, and
   the full conversation and workflow memory remain available to the agent.
9. Preserve provider portability and backwards compatibility when a provider
   or lauren-ai version does not support structured cache blocks.
10. Expose cache decisions and invalidation reasons without logging prompts,
   conversation contents, credentials, or tool arguments.

## 4. Non-goals

This PRD does not:

- guarantee cache hits after provider TTL expiry, process restart, model change,
  provider change, or a deliberate cache epoch change;
- make dynamic phase content immutable or hide it from the model;
- remove conversation compaction or require an unbounded conversation;
- change the phase state machine, transition-tool semantics, approval model, or
  checkpoint format defined by existing workflow PRDs;
- replace lauren-ai's provider transports with a second LLM transport layer;
- make cache behavior a security boundary or rely on prompt text to enforce
  tool permissions;
- require every third-party custom runner to be rewritten immediately; an
  explicit compatibility mode and diagnostics are defined below;
- cache secrets, user-specific credentials, or private tool results in a
  process-global store.

## 5. Terminology and invariants

### 5.1 Prompt regions

Every agent turn is represented logically as four regions:

1. **Stable system prefix** — immutable instructions that describe the agent,
   product, workflow contract, safety policy, tool-use protocol, and stable
   capabilities for the lifetime of a cache epoch.
2. **Dynamic system context** — current intent, phase name, phase instructions,
   artifacts, validation results, question answers, transition instructions,
   and other values that are expected to change between turns.
3. **Stable tool region** — deterministic schemas for tools available across
   the workflow or session.
4. **Phase-local tool region** — schemas for tools available only in the
   current phase, including transition tools whose descriptions contain the
   current graph context.

The provider adapter may encode these regions differently, but it must preserve
their ordering and invalidation semantics.

### 5.2 Cache epoch

A cache epoch identifies one stable contract. It is derived from a versioned,
redacted fingerprint of:

- provider and model identity;
- connection profile identity and relevant request options;
- stable system-prefix content;
- stable tool schemas and ordering;
- capability/security policy that affects stable tools;
- cache-contract version.

The epoch must not include phase name, phase state, user intent, artifact
contents, rolling summaries, question answers, transition outcome, tool
arguments, or timestamps. A changed epoch is an explicit, observable
invalidation—not a silent accidental miss.

### 5.3 Required invariants

- Stable prompt bytes and stable-tool ordering are deterministic for equal
  inputs.
- Dynamic phase state is never interpolated into the stable prefix.
- The generated workflow's user-questioning instruction is stable workflow
  policy and is included in the stable system prefix, while each actual
  question, answer, and answer-dependent state remains dynamic.
- A summary update changes dynamic context or conversation state, not the
  stable prefix.
- A phase transition does not change the cache epoch unless it changes the
  actual stable capability contract.
- Tool capability filtering remains authoritative. A cache optimization must
  never reuse a tool schema in a phase where that tool is not allowed.
- Conversation history remains append-oriented. Rewriting or inserting old
  messages is explicitly treated as a history invalidation.
- Cache telemetry identifies the reason for a miss without exposing prompt or
  message content.

## 6. Proposed implementation

### 6.1 Introduce a shared prompt/cache composition API

Add a public internal runtime module under the existing runner ownership
boundary, for example:

```text
src/agenthicc/runners/prompt_contract.py
```

The final names may follow existing conventions, but the API must provide the
following typed concepts:

```python
PromptContract(
    stable_system_prefix: str,
    dynamic_system_context: tuple[PromptBlock, ...],
    stable_tools: tuple[ToolSchema, ...],
    phase_tools: tuple[ToolSchema, ...],
    cache_epoch: CacheEpoch,
)
```

Required behavior:

- `stable_system_prefix` is constructed once per session/configuration and is
  immutable for that cache epoch.
- Dynamic blocks are ordered explicitly and carry a stable block kind/name,
  not an ad-hoc concatenation whose position depends on dictionary iteration.
- Tool schemas are normalized and sorted by canonical name plus a stable schema
  fingerprint. Duplicate names or conflicting schemas are rejected before an
  LLM request.
- Stable and phase-local tool sets are disjoint. A tool cannot silently move
  between regions because a phase happened to filter it differently.
- The composer exposes `stable_fingerprint`, `dynamic_fingerprint`, and
  `cache_epoch` for telemetry and tests; no raw prompt is returned in
  telemetry.
- A compatibility renderer can collapse the regions into the legacy string and
  flat-tool representation for older lauren-ai versions. The stable prefix
  must remain first in that fallback representation.
- The API is the only supported path for workflow runners to add phase
  instructions and transition guidance. Private string concatenation in a
  generated runner is a validation error unless it uses the documented helper.

The API must not expose provider-specific cache-control objects to workflow
authors. Provider adapters belong in the lauren-ai integration layer or the
existing agent-turn boundary.

### 6.2 Update the agent-turn boundary

Extend the existing `_run_agent_turn` compatibility shim and its owning call
path so a runner can pass a `PromptContract` (or an equivalent structured
request) while existing callers may continue passing a string.

The implementation must:

1. Build the stable base contract from the configured base system prompt,
   stable workflow contract, stable security policy, and stable session/tool
   metadata exactly once per cache epoch.
2. Append dynamic context as a separate region after the stable boundary.
3. Keep skills and tool descriptions in their declared region. A skill that
   changes the stable tool set must create a new epoch; a phase-only skill must
   remain dynamic or phase-local.
4. Pass the structured request through the existing lauren-ai runner rather
   than creating a second conversation implementation.
5. Keep the existing stable `conversation_id` and shared `ConversationStore`/
   memory behavior. This PRD changes representation, not conversation identity.
6. Ensure retries use the same epoch and do not duplicate dynamic blocks or
   append stale phase instructions to the stable prefix.
7. Keep context-guard and compaction behavior intact. If compaction changes
   the history prefix, report `history_compacted` and preserve the stable
   system/tool fingerprints.

The implementation should prefer a lauren-ai capability/API for structured
system blocks and cache breakpoints. If that capability is unavailable, the
compatibility renderer must preserve semantics and expose
`structured_cache_unavailable` rather than failing a normal workflow.

### 6.3 Provider adapter behavior

The adapter contract is:

| Provider capability | Required representation |
|---|---|
| Explicit system/tool/conversation cache controls | Stable system prefix and stable tools receive cache breakpoints; dynamic context follows them; conversation breakpoint remains append-oriented |
| Automatic prefix caching | Send deterministic stable regions first; do not add provider-specific controls that are unsupported |
| No known prompt caching | Preserve the exact logical regions and report `unsupported` telemetry; behavior remains correct |
| Legacy lauren-ai without structured blocks | Use the compatibility renderer, keep the stable prefix first, and emit a once-per-session warning/diagnostic |

Anthropic support must not put the rolling summary into the stable cacheable
system block. OpenAI-compatible, Modal, LiteLLM, Ollama, and future providers
must receive the same logical prompt order even when their transport treats
caching as automatic or unavailable.

### 6.4 Cache invalidation policy

The runtime must distinguish these outcomes:

| Event | Stable system/tool cache | Conversation cache | Telemetry reason |
|---|---|---|---|
| Normal next phase | Reuse | Append/reuse prior prefix | `phase_context_changed` |
| Agent retry in same phase | Reuse | Append or provider retry policy | `retry_same_epoch` |
| User question/answer | Reuse | Append new turn | `question_appended` |
| Rolling summary update | Reuse | Provider may retain only surviving history | `summary_updated` |
| Context compaction removes old messages | Reuse | Invalidate affected history prefix | `history_compacted` |
| Stable capability/tool set changes | New epoch | New epoch as needed | `stable_contract_changed` |
| Model/provider/profile/request cache identity changes | New epoch | New epoch as needed | `connection_changed` |
| Provider TTL expiry | Provider-dependent | Provider-dependent | `provider_expired` when known |

No code may claim a cache hit solely because the local epoch matches. A hit is
provider evidence when available; otherwise the runtime reports `eligible` or
`unknown`, not `hit`.

### 6.5 Workflow runner integration

#### Generic `WorkflowRunner`

`src/agenthicc/workflows/default/runner.py` must create the prompt contract at
the common phase-turn boundary. `PhaseSpec.system_prompt_override`, the phase
intent, artifacts, and transition instructions become dynamic blocks. Stable
workflow metadata and stable capabilities are built once. Existing capability
filters remain applied before a tool enters either tool region.

#### `CodePlanRunner` and subclasses

`CodePlanRunner.run_phase()` becomes the supported authoring/runtime boundary
for custom runners. Its inherited session wiring must compose a prompt
contract, so make-tool, EPUB, PDF, and site-imitate runners receive the feature
without separate implementations. The method must preserve its current
arguments and return behavior.

#### `CreateWorkflowRunner`

`CreateWorkflowRunner` receives explicit integration rather than relying on
the fact that it currently shares memory and a conversation ID:

- `DESIGN`, `GENERATE`, `VALIDATE`, and `SUMMARIZE` use the shared composer.
- The design and generation prompts are dynamic phase content. They must not
  be copied into the stable prefix on every turn.
- The approved design, validation report, generated file contents, rejection
  feedback, and user request are dynamic artifact blocks.
- Stable authoring instructions, the `WorkflowPlugin`/`PhaseSpec` contract,
  capability/security policy, checkpoint/resume contract, and cache-stability
  contract are stable authoring instructions for the create-workflow session.
- The tool set is split deterministically: stable inspection/memory/session
  tools in the stable region, and phase-specific write/validate/transition
  tools in the phase-local region. A phase must never inherit a stale write
  tool merely because a prior phase cached it.
- The runner uses the same public helper that generated workflows are told to
  use. There must be one implementation of prompt composition, not a special
  hand-built path in `CreateWorkflowRunner`.
- Its existing state machine, rejection loops, checkpoints, shared memory,
  and conversation ID remain unchanged semantically.

`CreateWorkflowRunner` must expose a cache-contract diagnostic in its phase
context so the authoring agent can see whether a generated workflow is using
the supported path. The diagnostic contains contract version, region names,
and fingerprints—not prompt contents or secrets.

### 6.6 Custom workflow compatibility

The loader and registry must recognize two runner modes:

1. **Contract-native** — the workflow uses `WorkflowRunner`, `CodePlanRunner`,
   or the documented prompt helper. Cache-stable behavior is enabled by
   default.
2. **Legacy/custom direct runner** — the workflow invokes `_run_agent_turn`
   directly or supplies only an unstructured string. It remains runnable for
   backwards compatibility, but receives a structured warning, a degraded
   cache mode, and an authoring diagnostic. It must opt into the contract before
   claiming cache-stable support.

The registry must not silently classify a workflow as optimized based only on
an attribute or a prompt string. The claim must be backed by runtime use of
the helper and a successful contract validation.

## 7. `create_workflow` authoring contract

The central requirement of this PRD is that downstream agents are taught how
to create optimized workflows, not merely told that caching exists.

### 7.1 Stable instructions to add to the authoring prompts

The shared authoring guidance in
`src/agenthicc/workflows/create_workflow/runner.py` must include an explicit
section equivalent to the following:

> **Prompt-cache stability contract**
>
> Build every generated workflow around the shared workflow runner and prompt
> composer. Keep the workflow contract, safety rules, capability rules, stable
> tool schemas, and reusable role instructions in the stable prompt region.
> Put the current phase, user request, phase state, artifacts, validation
> feedback, question answers, and transition details in dynamic context. Never
> interpolate those changing values into a module-level system prompt or the
> stable prefix. Do not prepend messages, rewrite old conversation entries, or
> put rolling summaries into the stable prompt. Keep stable tools and
> phase-local tools separate and deterministically ordered. Use
> `CodePlanRunner.run_phase()` or the documented contract-native runner API so
> cache handling, checkpoints, shared memory, and conversation continuity are
> inherited. Do not call `_run_agent_turn` directly in generated code unless
> the prompt-contract helper is used explicitly.

The prompt must explain the unavoidable exceptions: provider TTL, connection
changes, stable contract changes, and history compaction can invalidate a cache.
It must also state that prompt caching never replaces capability filtering or
tool authorization. It must additionally instruct the authoring agent to put a
user-questioning policy in the generated workflow's stable system prompt:

> Ask the user a clarifying question whenever required information is missing,
> ambiguous, or would materially change the workflow result. Use the existing
> user-question tool and wait for its answer before continuing. Do not guess
> over a material ambiguity. Keep the question policy stable; put each actual
> question and answer in dynamic conversation/context state.

This instruction must be part of the generated workflow's stable system
contract, not a phase-local string rebuilt on every turn. The generated
workflow must use the existing question-tool contract and must not implement a
second ad-hoc question mechanism.

This guidance must appear in `DESIGN` and `GENERATE`, be summarized in
`VALIDATE`, and be available through the inspection tool rather than being
only an undocumented convention.

### 7.2 Design-phase requirements

The design agent must produce a cache plan alongside the phase graph. The plan
must identify for each prompt/tool item:

- stable system content;
- dynamic phase context;
- stable tools;
- phase-local tools;
- the conditions that intentionally create a new cache epoch;
- how phase artifacts are passed without rewriting the stable prefix;
- how resume and compaction preserve or invalidate each region.

The design is rejected if it describes one mutable all-purpose system string,
uses a phase name or artifact in a supposedly stable constant, or has no
documented runner integration path.

### 7.3 Generation-phase requirements

The generation agent must produce code that:

1. subclasses or composes the supported runner boundary;
2. uses `PhaseSpec`, `WorkflowPlugin`, and the existing registry/loader;
3. passes phase state through the dynamic-context API;
4. keeps stable tool schemas independent from phase-local transition/write
   tools and gives both deterministic ordering;
5. preserves shared memory, stable conversation ID, checkpoints, phase output,
   approval state, and resume state;
6. avoids direct mutation of conversation internals and avoids message
   insertion at the beginning of history;
7. does not call a private transport or create a provider-specific cache
   implementation;
8. includes no prompt or secret contents in cache telemetry;
9. adds a stable system-prompt instruction to ask the user clarifying questions
   for missing, ambiguous, or materially consequential information, and wires
   the existing question tool into the workflow's permitted tool set;
10. leaves the generated workflow runnable when prompt caching is disabled or
   unsupported by the provider.

The generated package must include a short `CACHE_CONTRACT` or equivalent
metadata declaration generated from the approved design. This metadata is
descriptive and versioned; runtime behavior is still determined by the shared
helper, not by trusting the declaration.

### 7.4 Validation-phase requirements

The existing create-workflow validation tools must gain cache-contract checks.
At minimum they must detect:

- direct use of `_run_agent_turn` without the prompt-contract helper;
- phase-dependent values in a declared stable prompt constant or stable tool
  description;
- concatenation of user/artifact/validation data into the stable prefix;
- unstable tool ordering or duplicate/conflicting tool schemas;
- a phase-local tool declared as stable without an explicit reason;
- message insertion, history rewriting, or summary interpolation into the
  stable prompt;
- absence of the required stable user-questioning instruction or absence of
  the existing question tool when the generated workflow can encounter
  ambiguity;
- bypassing `CodePlanRunner.run_phase()` or the generic runner when the design
  selected that path;
- missing cache metadata, missing resume/checkpoint integration, or a cache
  declaration that disagrees with observed runner usage.

Validation should use AST/static checks where practical and a deterministic
runtime smoke test where static analysis cannot prove behavior. Diagnostics
must identify the file, symbol, region, and remediation. They must never dump
the source of a prompt if it may contain secrets or user data.

Validation must fail closed for a generated workflow that claims
`cache_stable=True` but cannot prove contract-native execution. It may offer a
legacy compatibility warning when the workflow does not make that claim.

### 7.5 Inspection and example tools

The create-workflow tool set must expose or extend tools with these capabilities:

- `describe_prompt_cache_contract`: returns region definitions, invariants,
  invalidation reasons, compatibility behavior, and the supported runner API;
- `show_workflow_template`: returns a minimal contract-native workflow with a
  stable module-level contract, dynamic phase context, deterministic tools,
  checkpoint/resume wiring, and `run_phase()` usage;
- `validate_workflow_cache_contract`: validates a generated path and returns
  structured errors/warnings/fingerprints;
- the existing phase/tool/runner inspection tools must include a cache region
  and cache-stability field where relevant.

These are authoring tools, not a security bypass. They must use the existing
workspace path, capability, network, and trust boundaries. They must return
bounded output and avoid revealing unrelated project files.

### 7.6 Summarize-phase requirements

The summarize agent may explain the generated workflow and its cache contract,
but must not edit the workflow to insert cache controls after validation. Any
required change returns to generation or validation, preserving the existing
state-machine semantics and audit trail.

## 8. Tool and prompt design details

### 8.1 Stable tool rules

Stable tools are limited to tools whose schema, authorization, and semantic
availability are unchanged for the cache epoch. A tool whose availability
depends on the current phase, approval state, workspace target, or network
allowlist must be phase-local unless the runtime proves that the relevant
authorization is stable.

Tool descriptions must not embed current artifact paths, validation reports,
timestamps, or user text in the stable region. Dynamic data belongs in the
tool argument schema only when the schema itself remains stable; current values
belong in the request context.

Tool ordering must be canonical across process runs. Sorting by display label
alone is insufficient if labels collide; use canonical tool name and schema
fingerprint as the tie-breaker.

### 8.2 Prompt rules for generated workflows

Generated prompts should be composed as:

```text
[stable workflow/system contract]
[stable safety and capability contract]
[stable tool protocol]

[dynamic phase name and objective]
[dynamic user intent]
[dynamic artifacts and validation state]
[dynamic transition guidance]
```

The visual separators are illustrative. The runtime composer, not a generated
string template, owns the actual region encoding.

The prompt should tell the agent to reuse stable instructions rather than
repeating them in every phase. It must also tell the agent that the complete
conversation remains available through the shared conversation ID and memory,
so optimization must not remove prior user/LLM context needed for correctness.

Every generated workflow's stable system prompt must also contain a
user-questioning instruction. The instruction must tell the workflow agent to
ask the user whenever a missing or ambiguous requirement could change the
result, to use the existing question tool, and to wait for the response before
proceeding. The current question and answer are dynamic state and must never be
copied into the stable prompt.

### 8.3 Question and approval tools

Question, approval, and transition tools retain their existing semantics. A
question answer is dynamic conversation/context data and must not change the
stable epoch. Approval can change which mutating tools are authorized; when it
changes the stable capability set, the runtime must create a new epoch rather
than reuse an unauthorized cached tool region.

## 9. Persistence, checkpoint, and resume behavior

The cache contract is part of workflow execution metadata, not a replacement
for the workflow journal. Checkpoints must retain:

- cache-contract version and cache epoch;
- stable and dynamic fingerprints;
- provider capability classification;
- last known cache status and invalidation reason;
- phase name, phase state, outputs, transition history, approval state, shared
  memory/journal references, and conversation ID as required by existing
  workflow contracts.

On resume:

1. Rehydrate the same workflow-scoped memory/journal and conversation ID.
2. Recompute the stable fingerprint from current configuration and compare it
   to the checkpoint.
3. Reuse the epoch only when provider/model/profile, stable prompt, stable tools,
   and policy match.
4. If they differ, start a new explicit epoch and preserve the reason.
5. Rebuild dynamic phase context from checkpoint state; never serialize a
   previously rendered all-in-one prompt as the source of truth.

If a provider cannot restore its remote cache after process restart, the
workflow remains correct and reports a cold-cache resume. The checkpoint must
not contain provider cache tokens or credentials.

## 10. Observability and diagnostics

Add structured, redacted events/metrics at the existing runner/usage boundary:

- `prompt_cache.eligible`, `prompt_cache.hit`, `prompt_cache.miss`,
  `prompt_cache.unsupported`;
- stable epoch/fingerprint identifiers, provider capability, workflow name,
  phase name, and cache region;
- invalidation reason from the policy table;
- input/output/cache-read/cache-write token counts when the provider reports
  them;
- whether the request used structured or compatibility rendering.

Never emit stable or dynamic prompt text, conversation text, API keys,
authorization headers, tool arguments, artifact contents, or raw cache keys.
Fingerprint values must be non-reversible identifiers suitable for correlating
events within the configured retention period.

The TUI may show a compact cache status in diagnostics or a debug view, but
normal workflow output must not become noisy. Headless JSON output must use a
versioned schema if cache fields are exposed.

## 11. Configuration and compatibility

Use the existing `execution.prompt_cache` setting as the user-visible feature
gate. No second per-workflow opt-in is required for contract-native workflows.
When it is false, the same prompt regions, tool filtering, memory, and
conversation behavior are used without provider cache controls.

Optional diagnostics may include a non-secret `prompt_cache_mode` with values
such as `structured`, `compatibility`, `disabled`, and `unsupported`, but it
must not be required to run a workflow.

The implementation must support the currently pinned lauren-ai range and its
declared compatibility fallbacks. If structured prompt blocks require a newer
lauren-ai release, update the dependency intentionally and document the
minimum version. A missing optional provider capability must degrade to the
compatibility renderer, not make unrelated providers unavailable.

Existing callers that pass `system_prompt_suffix` and a flat tool list continue
to work. They receive a contract assembled by the boundary where possible.
Existing custom workflows remain runnable; they are not allowed to advertise
cache-stable behavior until they use the contract-native API.

## 12. Security and privacy

- Cache eligibility must never weaken `NetworkGuard`, workspace boundaries,
  capability filtering, plugin trust, approval, or secret redaction.
- A stable tool region must not contain a tool whose authorization is dynamic
  unless the authorization is included in the epoch and the region is rebuilt
  whenever it changes.
- Prompt and tool fingerprints must be computed from canonical redacted data
  and must not be reversible encodings of secrets or conversation text.
- Provider cache controls must not cause one user's/session's private content
  to be reused in another session. Session identity and provider semantics must
  be honored.
- Generated authoring tools must not expose source or artifact data outside
  the existing workspace and trust boundaries.
- Tests must prove that cache telemetry and validation failures do not leak
  API keys, authorization headers, prompt contents, or tool arguments.

## 13. Acceptance criteria

### 13.1 Runtime prompt and tool contract

- **AC-163.1**: For an unchanged session/configuration, stable system bytes,
  stable tool schemas, stable tool order, and the cache epoch are identical
  across at least ten phase turns, retries, and question/answer turns.
- **AC-163.2**: Changing phase name, intent, artifact content, validation
  output, rejection feedback, question answer, or rolling summary changes only
  dynamic fingerprints and/or append-only conversation state; it does not
  change the stable prompt fingerprint.
- **AC-163.3**: A phase-local tool cannot be reused from a prior phase when its
  capability or authorization is no longer valid. Tests verify both allowed and
  denied transitions.
- **AC-163.4**: Tool schemas are deterministic across process runs and duplicate
  or conflicting schemas fail before an LLM request.
- **AC-163.5**: Context compaction reports a history invalidation while retaining
  the stable system/tool fingerprint. The resulting request still contains the
  current summary and required recent messages.
- **AC-163.6**: Changing model, provider, connection profile, relevant request
  options, stable prompt content, stable tools, or security policy creates an
  explicit new epoch.

### 13.2 Runner coverage

- **AC-163.7**: The generic `WorkflowRunner`, `CodePlanRunner`, all current
  CodePlan-derived runners, and `CreateWorkflowRunner` use the same composer
  and produce equivalent region/fingerprint semantics.
- **AC-163.8**: Existing phase transition, retry, rejection, approval,
  checkpoint, resume, memory, and conversation behavior remains unchanged.
- **AC-163.9**: A legacy direct runner still runs with a compatibility warning,
  while a contract-native runner receives structured cache handling by default.
- **AC-163.10**: Disabling prompt caching or using a provider without cache
  support preserves model-visible context and tool authorization.

### 13.3 CreateWorkflowRunner and generated workflows

- **AC-163.11**: `DESIGN` requires and records a cache plan identifying stable
  prompt content, dynamic context, stable tools, phase-local tools, and epoch
  changes.
- **AC-163.12**: `GENERATE` receives explicit cache-stability instructions and
  the contract-native template. A generated sample uses `PhaseSpec`,
  `WorkflowPlugin`, the supported runner API, dynamic phase context, and
  deterministic tool ordering.
- **AC-163.13**: The create-workflow inspection tools describe and demonstrate
  the cache contract without exposing secrets or unrelated files.
- **AC-163.14**: `VALIDATE` rejects generated code that places dynamic values in
  the stable prompt, uses unstable tool ordering, rewrites conversation history,
  bypasses the supported boundary, omits the required stable user-questioning
  instruction/question-tool integration, or claims cache stability without
  runtime evidence.
- **AC-163.15**: A generated workflow that passes validation receives cache-safe
  behavior without hand-written provider-specific code. A generated workflow
  can run with caching disabled and with a no-cache provider.
- **AC-163.16**: Every generated workflow's stable system prompt instructs its
  agent to ask the user clarifying questions for missing, ambiguous, or
  materially consequential information, and the workflow invokes the existing
  question tool rather than guessing or implementing a duplicate mechanism.
- **AC-163.17**: Generated workflow metadata records the contract version and
  cache-stability claim, but runtime does not trust metadata in place of actual
  contract usage.

### 13.4 Providers, persistence, and observability

- **AC-163.18**: Anthropic explicit cache controls place stable system/tool
  regions before dynamic context and do not include rolling summaries in the
  stable system block.
- **AC-163.19**: OpenAI-compatible/Modal, Ollama, LiteLLM, and unsupported
  providers preserve logical region ordering and degrade without errors.
- **AC-163.20**: Checkpoint/resume rehydrates memory, journal, phase state, and
  conversation ID; it reuses an epoch only when the stable contract matches
  and otherwise records a cold/new epoch reason.
- **AC-163.21**: Cache diagnostics report eligibility/hit/miss/unsupported and
  invalidation reasons without prompt, message, secret, or tool-argument
  leakage.
- **AC-163.22**: Documentation explains the contract, generated-workflow rules,
  provider limitations, configuration gate, and unavoidable invalidations.

## 14. Test plan

All tests must be deterministic, isolated, and independent of live provider
cache state.

### 14.1 Unit tests

Add tests for:

- stable/dynamic prompt block construction and canonical fingerprints;
- deterministic tool normalization, ordering, conflict detection, and region
  separation;
- cache epoch derivation and each invalidation reason;
- summary updates, retries, questions, and dynamic artifact replacement;
- stable prefix immutability after a composer has been created;
- capability filtering and approval changes preventing stale tool reuse;
- compatibility rendering for older lauren-ai and flat-string callers;
- redacted telemetry and absence of prompt/secret/tool-argument contents;
- `CreateWorkflowRunner` prompt composition for all four phases;
- authoring-plan validation and cache-contract metadata validation;
- AST checks for dynamic values in stable constants and direct private-runner
  bypasses.

### 14.2 Integration tests

Use fake lauren-ai/provider transports that capture structured request regions,
cache controls, tools, conversation ID, memory, and telemetry. Cover:

- generic phase transitions, retries, rejection loops, parallel phases, and
  resume;
- CodePlan-derived runners sharing one prompt/cache implementation;
- CreateWorkflow design, generation, validation rejection, correction, and
  summarize flows;
- a generated workflow asking a user clarification through the existing
  question tool, preserving the stable question policy while keeping the
  question and answer dynamic;
- cache-safe generated workflow execution using the sample template;
- compaction with stable system/tool reuse and history invalidation;
- profile/model/provider changes creating new epochs;
- Anthropic explicit controls, OpenAI-compatible automatic behavior, and
  unsupported-provider fallback;
- prompt-cache disabled mode producing equivalent model-visible semantics;
- checkpoint persistence and restart with the same conversation ID and shared
  memory/journal;
- capability and network/security denial after a phase transition;
- duplicate tool schemas and non-deterministic order failures.

### 14.3 End-to-end tests

Run the TUI/headless workflow journeys with mocked LLM responses:

1. Start a multi-phase built-in workflow, cross several transitions, ask a
   question, retry a phase, and verify stable/dynamic diagnostics.
2. Interrupt and resume a workflow and verify memory, journal, conversation,
   phase state, and cache epoch behavior.
3. Use `create_workflow` to generate a small custom workflow, validate it,
   execute it, and verify the generated workflow uses the contract without
   provider-specific code.
4. Generate a deliberately unsafe/non-optimized workflow and verify validation
   reports the problem before publication.
5. Run the same generated workflow with `prompt_cache=false` and an unsupported
   provider fixture; verify identical functional results and no crash.
6. Verify normal TUI output remains concise and cache diagnostics are redacted.

### 14.4 Regression tests

Add regressions for:

- dynamic phase prompts being accidentally included in a cacheable stable
  system block;
- rolling summaries changing the stable system fingerprint;
- stale phase-local tools surviving a transition;
- `CreateWorkflowRunner` bypassing the shared composer;
- generated runners using a changing module-level prompt constant;
- generated workflows omitting the stable user-questioning instruction or
  bypassing the existing question tool;
- resume restoring a rendered prompt instead of reconstructing regions from
  checkpoint state;
- a provider capability failure making a no-cache workflow unusable.

## 15. Documentation and migration

Update in the same implementation:

- `docs/guides/workflows.md` with the prompt-region contract, generated
  workflow rules, and the `CreateWorkflowRunner` authoring path;
- `docs/guides/architecture.md` or the relevant architecture reference with
  the runner/transport ownership boundary and resume behavior;
- `docs/reference/storage.md` with checkpoint cache metadata and retention
  rules if the checkpoint schema changes;
- `README.md` with a concise explanation of prompt caching and the existing
  `execution.prompt_cache` setting;
- `llms-full.txt` and `llms.txt` for any new public Python symbols;
- `prds/README.md` with this PRD and implementation status once delivered;
- `CreateWorkflowRunner` prompt/tool reference documentation if its tools are
  public or discoverable.

Migration must not require existing custom workflows to change before they can
run. The compatibility mode should produce actionable diagnostics and a
documented path to contract-native behavior.

## 16. Rollout and operational safeguards

Implement behind the existing `execution.prompt_cache` gate, with structured
composition enabled first for test fixtures and then for built-in workflows.
Retain a compatibility renderer and a temporary diagnostic switch for
comparing structured and legacy representations. Remove comparison logging
once rollout is complete; it must never log prompt contents.

Before enabling generated-workflow claims by default:

1. land unit and fake-transport integration tests;
2. migrate all built-in runner paths;
3. enable `CreateWorkflowRunner` design/generation/validation guidance;
4. validate and execute representative generated workflows;
5. monitor token accounting, cache status, latency, and error rates;
6. document provider-specific limitations and rollback to compatibility mode.

The rollback path is setting `execution.prompt_cache=false` or selecting the
compatibility renderer. Neither path may discard workflow state or change
conversation identity.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Structured prompt API is unavailable in an installed lauren-ai version | Capability detection and a semantic compatibility renderer |
| Overly broad stable tool region reuses an unauthorized tool | Separate capability filtering, stable/phase-local regions, epoch changes on policy updates |
| Agents misunderstand stable versus dynamic content | Exact authoring prompt, template, inspection tool, static checks, and runtime smoke test |
| Cache fingerprints leak sensitive content | Redacted canonical hashing and telemetry tests |
| Provider cache behavior differs from local eligibility | Report provider evidence separately from local eligibility; never infer a hit |
| Compaction makes history reuse impossible | Preserve stable system/tool cache and explicitly report history invalidation |
| Generated code bypasses the helper | Validation diagnostics, contract metadata, and legacy warning mode |
| Prompt refactor changes model behavior | Golden request fixtures and end-to-end equivalence tests with caching disabled |
| Multiple runner implementations drift | One shared composer and coverage across every built-in runner |

## 18. Open implementation decisions

The implementation team may choose final symbol names and module placement,
provided the ownership boundary and behavior remain as specified. The following
decisions must be recorded in the implementation notes:

1. The exact lauren-ai minimum version that supports structured prompt regions.
2. Whether structured regions are represented by lauren-ai-native types or an
   agenthicc adapter converted at the transport boundary.
3. The durable checkpoint schema version for cache metadata.
4. The precise telemetry event names and whether they are included in the
   default headless JSON output.
5. The AST/static-analysis implementation and the runtime smoke-test limits.

No decision may weaken the stable/dynamic separation, capability boundary,
resume contract, or provider fallback behavior.

## 19. Definition of done

This PRD is complete when:

- every acceptance criterion is covered by passing unit, integration, E2E, or
  documented provider-fixture tests;
- all built-in workflow runners use the shared contract;
- `CreateWorkflowRunner` prompts, tools, template, and validation enforce
  cache-stable generated workflows;
- generated workflows inherit cache-safe behavior without provider-specific
  code;
- checkpoint/resume, shared memory, conversation continuity, and phase
  semantics have no known regression;
- cache diagnostics are useful, bounded, and redacted;
- relevant source, tests, guides, architecture references, and public-symbol
  documentation are updated;
- ruff, formatting, mypy/type-audit checks relevant to the changed surface,
  and the complete test suite pass, with environment blockers reported
  explicitly.

## 20. Verification commands

The implementation record must report the results of the applicable checks:

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

The fake-provider integration suite must be included in the normal test
command. Live provider cache behavior is not a CI requirement; provider
fixtures and captured structured requests are the deterministic evidence.

## 21. Implementation record

Implemented in the shared runner boundary with:

- `agenthicc.runners.prompt_contract` for deterministic stable/dynamic prompt
  composition, tool ordering, provider capability detection, redacted
  fingerprints, and cache epochs;
- `AgentTurnRunner` integration that keeps rolling summaries dynamic, performs
  journal-aware compaction, and preserves the stable contract across retries;
- built-in generic, `code_plan`, and `create_workflow` runner integration;
- `CreateWorkflowRunner` authoring guidance, a cache-safe runner template,
  inspection tools, strict static validation, and explicit `ask_user` guidance;
- backward-compatible checkpoint metadata and workflow handle restoration; and
- unit, integration, and end-to-end coverage for contract composition,
  validation, checkpoint metadata, and generated-runner behavior.

The lauren-ai 1.4 API does not expose a provider-neutral structured prompt
region type, so agenthicc uses a compatibility renderer at the agent boundary:
the stable prefix remains the decorated system prompt and dynamic blocks are
appended to the turn message. Provider transports retain responsibility for
their native cache controls. This preserves behavior when caching is disabled
and avoids claiming cache hits where the provider offers no evidence.

Verification completed on 2026-08-01:

- `uv run pytest tests/unit -q`: 2,812 passed, 14 skipped;
- `uv run pytest tests/integration -q`: 182 passed;
- `uv run pytest tests/e2e -q`: 91 passed, 1 skipped;
- `uv run pytest tests/ -q`: 3,088 passed, 15 skipped, 4 existing warnings;
- Ruff lint, Ruff formatting, and the type-audit baseline check passed.

The repository-wide mypy command remains blocked by two pre-existing optional
environment issues outside this change: the optional `name_that_ui` module has
no installed stub/implementation, and the installed NumPy stubs use a type
statement unsupported by the configured interpreter check. No new mypy error
was reported from the changed modules before those blockers stopped analysis.
