---
title: "PRD-177: Efficient, Evidence-Driven reconstruct_site Workflow"
status: Proposed
version: 0.1.0
created: 2026-08-27
scope: "reconstruct_site workflow execution, research evidence, artifacts, screenshots, caching, checkpoints, and validation"
related_prds:
  - PRD-100  # code_plan architecture
  - PRD-151  # reliable command execution and build lifecycle
  - PRD-154  # create_workflow architecture
  - PRD-156  # resumable workflow continuation
  - PRD-159  # CloakBrowser tools
  - PRD-160  # Playwright tools
  - PRD-163  # cache-stable workflow prompts
  - PRD-169  # tool-call transaction integrity
  - PRD-170  # workflow resume recovery
  - PRD-173  # recoverable workflow errors
  - PRD-174  # tool-aware workflow authoring
  - PRD-175  # runtime AGENTS.md integration
  - PRD-176  # progressive startup
tags:
  - workflows
  - reconstruct-site
  - performance
  - prompt-cache
  - artifacts
  - screenshots
  - checkpoints
  - browser
---

# PRD-177 — Efficient, Evidence-Driven `reconstruct_site`

## 1. Executive summary

`reconstruct_site` is a high-fidelity website reconstruction workflow. It
examines a reference site, derives a route and design inventory, builds a
Next.js application, validates the result, and optionally adds the database,
deployment, operations, and documentation layers needed for a production
project.

The workflow has accumulated a very large sequential state machine. Its
current source contains 39 executable states, while the runner still reports
`total_phases = 22`. The declarative `PhaseSpec` list, fresh-run dispatch,
resume dispatch, model configuration, prompts, and artifact bookkeeping are
also maintained independently. Research outputs are mostly held in the typed
context, and names such as `visual_spec.md` are recorded as labels rather than
being guaranteed files. Browser tools can save screenshots, but the workflow
does not require or associate those files with a reconstruction evidence
manifest.

This PRD defines an optimized workflow that remains faithful to the existing
agenthicc contracts:

- one parent `conversation_id` and one session-owned conversation memory for
  all parent workflow phases;
- append-only conversation and journal semantics;
- stable system instructions and stable tool schemas for provider prompt
  caching;
- explicit tool-call-only phase transitions;
- capability, workspace, network, approval, and browser policy enforcement;
- durable typed checkpoints and exact resume from the last safe boundary; and
- no arbitrary global checkpoint-size ceiling.

The work consolidates phase metadata, makes research and screenshot evidence
durable, keeps checkpoints compact through artifact references, reduces
unnecessary model/tool work, and allows a static site to avoid production
infrastructure phases it did not request.

## 2. Problem statement

The workflow currently creates several correctness, cost, and user-experience
risks.

### 2.1 Phase-graph drift

`ReconstructState`, `ReconstructSiteWorkflow.phases`, `_phase_index()`, the
`run()` match statement, and the `resume()` match statement are separate
representations of the same graph. A phase can be added to one representation
without being added to another. The runner reports 22 phases even though the
current phase graph contains 39 non-terminal phases. This makes the TUI phase
counter, checkpoint metadata, and resume diagnostics misleading.

### 2.2 Research evidence is not a durable product

The context carries route, visual, interaction, asset, architecture, and
validation data. Several phase methods set entries such as
`ctx.artifacts["route_inventory"] = "route_inventory.md"`, but the workflow
does not itself materialize that Markdown document or verify that it exists.
An agent may write a file with `write_file`, but successful phase transition
does not currently require a file receipt, content digest, or manifest entry.

The result is that a resumed run can retain a large serialized context while
the user has no reliable, inspectable research package on disk.

### 2.3 Screenshot evidence is optional and disconnected

The visual-research and visual-validation prompts ask the agent to capture
screenshots. The Playwright and CloakBrowser screenshot tools persist images
under `.agenthicc/browser-artifacts`, but screenshots are only created if the
agent chooses to call them. The reconstruct context does not retain the
artifact IDs, route, viewport, state, or reference/implementation role needed
for deterministic comparison.

### 2.4 Excessive context, tool, and phase cost

The parent conversation is intentionally reused across the workflow, which is
correct for continuity and cacheability. However, 39 sequential phases, up to
five attempts per phase, and phase limits as high as 40 agent turns can produce
very large prompt histories and high cost. Each phase also starts from the
base tool collection, which may include the complete plugin, MCP, browser, and
memory tool set even when the phase needs only a small subset.

The solution must reduce work without breaking the stable prompt/tool prefix:
changing stable tool schemas on every phase would itself invalidate provider
prompt caches.

### 2.5 Configuration does not fully control execution

`ReconstructSiteParams` exposes per-phase model fields, but the custom runner
uses the public `run_phase()` helper without consistently forwarding a model
override. Static/application/production scope is also not a first-class
workflow profile, even though the initial phase collects information about
static references and API reproduction.

### 2.6 Re-entry and resume need evidence-aware semantics

Validation phases can request a `target_phase`, but unknown targets currently
fall back to visual validation. Re-entry does not define which downstream
artifacts become stale, how many re-entry cycles are allowed, or how a
checkpoint records the invalidation. Phase-start persistence exists through the
workflow handle, but the transition result and artifact manifest need a single
atomic recovery boundary.

## 3. Current-state evidence

The baseline was inspected in the current checkout on 2026-08-27.

| Area | Current evidence | Consequence |
|---|---|---|
| Executable graph | `ReconstructState` and `ReconstructSiteWorkflow.phases` contain 39 non-terminal phases | `total_phases = 22` is stale; metadata can drift |
| Fresh execution | `ReconstructSiteRunner.run()` has a large state match loop | New phases require manual wiring |
| Resume | `ReconstructSiteRunner.resume()` duplicates the dispatch loop | Fresh and resumed behavior can diverge |
| Prompt metadata | `PhaseSpec.system_prompt_override` and runner-local prompt strings both exist | Two sources of truth |
| Memory | `run_phase()` receives the same session memory and `conversation_id` | Continuity is correct, but history can grow substantially |
| Tool collection | `_base_tools()` begins with all plugin/MCP tools and appends memory tools | Large schemas and irrelevant tools are exposed |
| Research context | `ReconstructContext` stores inventories and summaries in memory/checkpoints | Checkpoint size grows with the research corpus |
| Named artifacts | Phase methods store labels in `ctx.artifacts` | Labels do not guarantee files |
| Browser evidence | Browser artifact store writes bounded files under `.agenthicc/browser-artifacts` | Captures are not required or linked to reconstruct state |
| Checkpoint boundary | `WorkflowRunHandle.update_phase()` persists phase entry | A small post-transition window remains, and evidence is not transactional |
| Re-entry | `_reentry_state()` maps unknown values to visual validation | Invalid agent output is hidden instead of rejected |
| Configuration | `reference_is_static`, desired routes, and API mode are collected but not used for a profile graph | Unnecessary phases may run |

The baseline is evidence for this design and not an assertion that every
current behavior should be removed. Existing public workflow names, tool
contracts, session ownership, and checkpoint compatibility remain requirements.

## 4. Goals

1. Make the executable phase graph have one authoritative representation.
2. Make every research, validation, screenshot, and build artifact
   inspectable, durable, content-addressed where practical, and resumable.
3. Keep the parent workflow on one `conversation_id` and one session-owned
   conversation memory across phases, retries, re-entry, interruption, and
   resume.
4. Preserve the cache contract: stable policy and stable tool schemas stay
   deterministic; phase state, artifacts, questions, answers, and summaries
   remain dynamic.
5. Reduce prompt, tool-schema, model, browser, and phase overhead without
   weakening capability or workspace policy.
6. Make static, application, and production reconstruction scopes explicit and
   predictable.
7. Make re-entry bounded, validated, artifact-aware, and easy to diagnose.
8. Keep all existing pause, resume, cancellation, provider retry, approval,
   MCP, Playwright, CloakBrowser, AGENTS.md, and error-recovery contracts.
9. Provide deterministic unit, integration, E2E, and performance evidence for
   the optimizations.

## 5. Non-goals

- Replacing `lauren-ai`, its provider transports, or its conversation store.
- Creating a second parent conversation or bypassing the session conversation
  journal to reduce tokens.
- Removing the existing `reconstruct_site` workflow or changing its CLI name.
- Automatically copying protected reference-site content without the existing
  user, network, workspace, and legal-policy boundaries.
- Making browser access mandatory when the configured browser integration is
  unavailable; degraded behavior must be explicit and resumable.
- Running concurrent LLM turns that mutate one parent `ConversationStore`.
- Adding a permanent global prompt or tool-schema limit as a substitute for
  artifact externalization. There is no arbitrary 1,000,000-byte checkpoint
  ceiling in this design.
- Requiring production infrastructure for a static site that does not need it.

## 6. Users and primary journeys

### 6.1 Static reference reconstruction

1. The user selects `reconstruct_site` and supplies a reference URL and target
   directory.
2. The workflow explores routes and captures available visual evidence.
3. The user can inspect a durable research package before implementation.
4. The workflow builds the static application, validates representative routes
   at mobile/tablet/desktop sizes, and writes a final evidence manifest.
5. Database, deployment, and operations phases are skipped with a recorded
   reason unless explicitly requested.

### 6.2 Application reconstruction

The workflow additionally models interactions, mocked or reproduced API
behavior, a data layer, and application-state validation. It records all
research and implementation evidence and resumes from any phase boundary.

### 6.3 Production reconstruction

The workflow runs the selected database, ORM, deployment, container, proxy,
package-command, script, and documentation phases. Infrastructure phases are
selected from the profile and are not hidden behind a generic 39-phase counter.

### 6.4 Interrupted reconstruction

When the user presses Esc, the provider fails, or the process exits, the
checkpoint contains the current phase, phase attempt, parent conversation
identity, artifact manifest revision, and typed context reference. Resume
continues that exact phase without regenerating completed research or
replacing durable screenshots.

### 6.5 Controlled visual re-entry

If visual validation identifies a design-system issue, the workflow re-enters
the earliest valid affected phase, marks dependent artifacts stale, preserves
unaffected research, and records the re-entry reason. An invalid target is
rejected as a tool error and does not silently redirect the workflow.

## 7. Proposed architecture

### 7.1 One phase-plan source of truth

Introduce an internal immutable `ReconstructPhaseDefinition` (name may be
adjusted to repository conventions) containing at least:

```text
state
name
handler
agent_type
mode_override
max_turns
model_key
required_capabilities
next_phase
retry_phase
allowed_reentry_targets
artifact_kinds
profile_membership
```

The phase plan must generate or validate:

- `ReconstructState` name mappings;
- `PhaseSpec` registry metadata;
- phase indexes and display counts;
- fresh-run dispatch;
- resume dispatch;
- model lookup;
- capability/tool selection;
- re-entry validation; and
- documentation/diagnostic output.

The dynamic per-route PAGE work remains a controlled repeated phase. Its
display representation must report both the static phase index and route
progress, for example `PAGE 2/7`, without corrupting the overall phase count.

The graph must fail during import-time validation or an explicit workflow
validation check if a definition has a missing handler, duplicate name,
unknown target, missing profile, or inconsistent terminal edge.

### 7.2 Parent conversation and memory

The workflow continues to use the `WorkflowConfig.conversation_id` and the
session-owned `ShortTermMemory` passed to `run_phase()` for every parent phase.
The runner must never create a new conversation for a retry, profile phase, or
resume.

The full append-only conversation remains available to the session and can be
rehydrated after resume. To prevent every dynamic prompt from duplicating the
entire research corpus, the workflow adds a compact phase digest containing:

- current phase and attempt;
- completed/stale artifact IDs;
- short summaries and counts;
- the active route/page;
- unresolved issues; and
- required next action.

Large source observations, HTML-derived data, screenshots, and reports remain
on disk and are referenced by digest. A compactor may summarize dynamic
context, but it must not rewrite old journal entries, insert messages at the
beginning of the conversation, or place rolling summaries in the stable system
prompt.

If an exploratory fan-out is introduced later, each child operation must have
isolated mutable memory and a linked operation ID. It may not concurrently
append to the parent `ConversationStore`. Its deterministic result is merged
into the parent as one append-only evidence message before the next parent
phase.

### 7.3 Artifact store and manifest

Introduce a workflow-scoped artifact service rooted below the authorized
workspace, for example:

```text
.agenthicc/reconstruct_site/<run-id>/
  manifest.json
  research/
    route-inventory.json
    visual-spec.json
    interaction-inventory.json
    asset-inventory.json
    architecture.md
    design-system.json
  browser/
    reference/
    implementation/
  validation/
  phases/
    <phase>/<attempt>/receipt.json
```

The exact directory may follow the repository's canonical artifact policy,
but it must remain inside `WorkspaceView` authorization and be configurable
only within that policy.

Every artifact record contains:

```json
{
  "artifact_id": "sha256-or-uuid",
  "kind": "visual_spec",
  "relative_path": ".agenthicc/reconstruct_site/run/research/visual-spec.json",
  "media_type": "application/json",
  "sha256": "...",
  "byte_count": 1234,
  "phase": "visual_research",
  "attempt": 1,
  "status": "complete",
  "created_at": "...",
  "source": "workflow|browser|agent_write|command"
}
```

The manifest is atomically updated and revisioned. Artifact writes are
idempotent by `(run_id, phase, attempt, kind, content_hash)` where possible.
The manifest never stores provider credentials, cookies, authorization
headers, or unrestricted absolute paths.

The context and checkpoint store artifact references, counts, digests, and
small summaries—not every large artifact body. Rehydration verifies the
manifest revision and content hash, reports missing/corrupt artifacts as a
structured recoverable error, and never silently treats them as complete.

### 7.4 Research phase receipts

The transition tools for research phases must accept or produce a structured
receipt that identifies the durable evidence. For example:

```text
submit_visual_spec(
  design_tokens=...,
  summary="...",
  artifact_ids=["..."],
  screenshot_ids=["..."]
)
```

Backward-compatible tool inputs may continue to accept the current arguments,
but the runner must create a receipt from the supplied data before declaring
the phase complete. If an artifact is required by the profile and cannot be
written, the phase remains retryable or fails with a recoverable checkpoint.

### 7.5 Screenshot evidence

The workflow must provide an evidence helper around the existing Playwright
and CloakBrowser screenshot tools. It must:

- capture reference screenshots for the configured route set where browser
  access is available;
- capture the implementation at the same route and viewport during validation;
- support at least mobile, tablet, and desktop viewport profiles;
- record route, URL, viewport, device scale, page state, role (`reference` or
  `implementation`), browser backend, artifact ID, and content hash;
- store only through the existing bounded browser artifact store and workspace
  policy;
- reuse an existing artifact when the content hash and capture identity match;
- make an unavailable browser a visible `degraded` result, not an invented
  screenshot; and
- attach screenshot records to the reconstruct manifest and checkpoint digest.

The agent remains able to take exploratory screenshots, but the phase receipt
and final evidence manifest must distinguish required baseline captures from
optional exploratory captures.

### 7.6 Stable cache contract

The workflow must preserve the existing prompt-cache contract:

- `CACHE_CONTRACT` and the framework's stable workflow policy remain immutable
  for a cache epoch;
- stable tool schemas are selected once per workflow run or profile and remain
  deterministically ordered;
- phase instructions, phase state, artifact summaries, route, questions,
  answers, and validation results are dynamic context;
- dynamic context is appended through the supported runner API;
- no phase inserts or rewrites messages near the beginning of the shared
  conversation;
- a tool/capability/backend change records a new cache epoch and reason; and
- diagnostics report eligibility/fingerprints without claiming that a provider
  cache was actually hit.

Tool reduction must therefore use one deterministic reconstruct-site tool
bundle for the run, or a clearly versioned cache epoch boundary. Simply
switching the stable tool list on every phase is not an acceptable optimization.

### 7.7 Phase-specific tools and capability policy

Phase definitions declare required capabilities. The runner compiles the
authorized stable reconstruct-site tool bundle from the parent session's
available tools and optional integrations. A phase-specific control-tool set
is appended in the dynamic region.

Examples:

| Phase group | Required capabilities |
|---|---|
| Reconnaissance | browser/network read, filesystem read, memory |
| Visual research | browser/network read, screenshot, filesystem read, memory |
| Artifact recording | filesystem write under authorized workspace |
| Implementation | filesystem write, command execution, browser read/write as configured |
| Validation | filesystem read, command execution, browser read/screenshot |
| Documentation | filesystem read/write, command read where configured |

Capability filtering, mode restrictions, approvals, `WorkspaceView`, network
guard, MCP status, and browser backend policy remain authoritative. A missing
optional integration is recorded in the evidence manifest and does not expose a
raw client as a fallback.

### 7.8 Model and budget routing

Every phase definition references a model configuration key. The public
`run_phase()` path must accept an optional `model_override` while preserving
existing callers. `reconstruct_site` must pass the configured override for
each phase, including infrastructure phases where a distinct override exists.

The runner must support configurable per-phase:

- maximum agent turns;
- maximum wall-clock duration;
- maximum provider/tool retry budget;
- maximum browser operations for a route/viewport;
- optional model override; and
- profile-specific enablement.

Retries should use the smallest useful follow-up prompt after a missing or
invalid transition. A phase must not consume five full high-turn runs when a
structured tool validation error can immediately explain the correction.

### 7.9 Profiles and conditional phases

The workflow exposes a typed profile, selected from explicit configuration or
confirmed during INIT:

```text
static
application
production
custom
```

Profile behavior:

- `static` runs discovery, evidence, design, implementation, responsive and
  visual/interaction/accessibility/performance validation; infrastructure
  phases are skipped with reasons.
- `application` adds the selected data/API behavior phases and their checks.
- `production` adds database, ORM, deployment, container, proxy, scripts,
  package-command, and documentation phases.
- `custom` requires an explicit validated phase selection and records the
  selected graph in the checkpoint.

Profile selection is not inferred solely from a vague user sentence. The INIT
phase must ask a focused question when scope materially changes the work.
Skipped phases are durable records with a reason, not silent omissions.

### 7.10 Checkpoint and resume boundary

At each successful transition, the runner must atomically make durable:

1. the typed next phase and iteration;
2. the parent `conversation_id` and run identity;
3. profile and phase-plan version;
4. artifact-manifest revision and required artifact references;
5. route/page index and re-entry counters;
6. cache contract diagnostics; and
7. compact dynamic digest.

The implementation should use the existing workflow-handle persistence
boundary and `persist_context_transition()` where appropriate. It must not
serialize live memory, browser clients, tool callables, or provider objects.

There is no fixed global checkpoint byte ceiling. Large values are
externalized to artifacts; JSON serialization, filesystem capacity, and
configured operational policies remain valid failure modes. A checkpoint
failure must produce the existing typed diagnostic/recovery behavior and must
never be reported as a successful transition.

On resume:

- the workflow is loaded by name and plan version;
- the same session conversation is rehydrated before the next provider turn;
- artifact references are verified;
- completed phases are not regenerated unless explicitly stale;
- the current page and profile are preserved; and
- missing artifacts stop at a recoverable diagnostic rather than silently
  restarting discovery.

### 7.11 Re-entry and invalidation graph

Each phase definition declares the downstream artifact kinds it invalidates.
For example, re-entering `design_system` may stale the design system, shell,
component, page, and visual-validation artifacts while preserving route and
asset research.

The re-entry tool contract must:

- accept only a known phase name permitted by the current graph/profile;
- reject empty/unknown/incompatible targets with a structured error;
- record source validation phase, target phase, reason, and affected artifacts;
- increment a bounded re-entry counter; and
- stop with a recoverable failure when the configured re-entry budget is
  exhausted.

Validation must not use a fallback target for malformed input.

## 8. Functional requirements

### FR-01 — Authoritative phase graph

The system shall have one phase-plan source from which registry metadata,
display counts, dispatch, resume, model routing, profile membership, and
re-entry validation are derived or mechanically checked.

### FR-02 — Accurate progress display

The TUI and diagnostics shall display the selected profile's actual number of
static phases. Dynamic PAGE progress shall be shown separately. No execution
path shall report 22 when the active graph contains 39 phases.

### FR-03 — Shared parent conversation

All parent `reconstruct_site` phases, retries, validation re-entry, pause,
resume, and completion shall use the session's existing conversation ID and
conversation memory.

### FR-04 — Durable research package

Reconnaissance, visual research, interaction analysis, content/assets,
architecture, and design-system outputs shall produce typed artifact records
and an atomically updated manifest. The workflow shall be able to show the
user where each required artifact is stored.

### FR-05 — Screenshot manifest

Required screenshot captures shall be stored through the existing browser
artifact boundary and linked to the reconstruction manifest with route,
viewport, role, backend, hash, and availability status.

### FR-06 — Artifact integrity

Every complete artifact record shall include a content digest and byte count.
Resume shall detect missing, changed, or unreadable artifacts and provide a
recoverable diagnostic.

### FR-07 — Stable cache behavior

Changing phase state, artifacts, route, questions, or validation results shall
not change the stable workflow policy or stable tool schemas. A genuine stable
contract change shall create an explicit cache epoch with diagnostics.

### FR-08 — Capability-safe tool selection

The workflow shall expose only tools authorized by the parent session's mode,
workspace, network, approval, and integration policies. Tool reduction shall
not bypass those policies or invalidate the cache contract on every phase.

### FR-09 — Model override correctness

Configured model overrides shall reach the provider turn for the intended
phase. Missing overrides shall use the global model, preserving current
precedence.

### FR-10 — Execution profiles

Static, application, production, and validated custom profiles shall select
explicit phase graphs and record skipped phases.

### FR-11 — Efficient bounded retries

Missing transition calls, invalid transition arguments, provider failures, and
tool failures shall use separate bounded budgets. A recoverable structured
tool error shall not cause an unnecessary full phase replay.

### FR-12 — Atomic transition checkpoint

After a successful transition, the next state, artifact revision, and route
progress shall be recoverable as one logical boundary. A checkpoint-write
failure shall be surfaced using the existing recovery contract.

### FR-13 — Exact resume

Resume shall continue from the stored phase, page, profile, artifact revision,
and conversation, without restarting completed phases or creating a new
conversation.

### FR-14 — Validated re-entry

Only phase targets valid for the active profile and graph shall be accepted.
Affected downstream artifacts shall become stale and be revalidated before
completion.

### FR-15 — Final evidence package

Successful completion shall include a manifest that identifies research
artifacts, implementation receipts, validation results, screenshots, skipped
phases, re-entry history, and unresolved non-blocking issues.

### FR-16 — Degraded integration behavior

If Playwright, CloakBrowser, MCP, or another optional integration is missing,
the workflow shall report the exact unavailable capability and follow the
profile's fallback policy. It shall not fabricate evidence or silently use an
unapproved client.

## 9. Non-functional requirements

### NFR-01 — Performance

On deterministic fake-provider benchmarks, the optimized workflow shall meet:

- phase-plan construction: p95 under 20 ms;
- artifact manifest update: p95 under 100 ms for 1,000 records;
- checkpoint payload: contain references/digests rather than full bodies for
  artifacts larger than the configured inline threshold;
- no more than one stable tool-bundle compilation per workflow cache epoch;
- no duplicate screenshot write when capture identity and content hash match;
- profile selection shall remove disabled phases from actual execution; and
- retry prompts shall be smaller than the first attempt prompt for the same
  phase unless the diagnostic requires additional evidence.

Production latency and token budgets are benchmarked with a fake provider and
reported separately from network/provider variability.

### NFR-02 — Reliability

The workflow shall tolerate provider retry, tool failure, cancellation,
process interruption, missing optional integrations, partial artifact writes,
and resume after a crash without corrupting the manifest or silently claiming
success.

### NFR-03 — Durability

Manifest and artifact writes shall use the repository's atomic-write and
workspace-boundary conventions. Partial temporary files shall not be treated
as complete evidence.

### NFR-04 — Security

Artifact paths shall be workspace-scoped, traversal-safe, symlink-safe, and
bounded by the existing filesystem policy. Screenshots and manifests shall
exclude cookies, authorization headers, API keys, and other secrets. Browser
and network access shall use the configured guards.

### NFR-05 — Cache integrity

The implementation shall preserve stable prompt/tool fingerprints across phase
turns when the stable contract is unchanged. Tests shall verify that dynamic
artifact changes do not cause accidental stable-cache invalidation.

### NFR-06 — Backward compatibility

Existing workflow names, public transition tool names, checkpoint codecs,
`WorkflowConfig`, session ownership, and CLI invocation remain compatible.
Older checkpoints without a manifest receive a versioned migration path or a
clear diagnostic-only non-resumable result.

### NFR-07 — Observability

The TUI and structured diagnostics shall expose profile, phase, attempt,
artifact status, screenshot availability, cache epoch/status, skipped phases,
re-entry count, and recoverability without exposing sensitive content.

### NFR-08 — Determinism

Phase ordering, artifact serialization, manifest ordering, tool ordering,
fingerprints, and fake-provider tests shall be deterministic. UUIDs may be
used for external artifact identity, but content identity and test assertions
must not depend on random ordering.

## 10. Data model

Extend the reconstruct context or its compatible successor with compact fields
similar to:

```text
plan_version: str
profile: str
phase_attempt: int
artifact_manifest_path: str
artifact_manifest_revision: int
required_artifact_ids: list[str]
stale_artifact_ids: list[str]
screenshot_ids: list[str]
reentry_count: int
reentry_history: list[ReentryRecord]
phase_digest: str
```

The existing inventories and status fields remain available for compatibility,
but large bodies should move behind artifact references. The checkpoint codec
must version and validate these fields, reattach session memory separately,
and reject unknown phase-plan versions unless an explicit migration exists.

## 11. TUI and CLI behavior

The TUI shall:

- display the active profile and accurate phase progress;
- show `Research evidence: n complete, m missing, k stale`;
- show screenshot availability separately from screenshot count;
- identify skipped infrastructure phases and their reasons;
- show when a phase is retrying because of a missing transition versus a
  provider/tool error;
- show the artifact directory only after path redaction/authorization checks;
- preserve the existing waiting, pause, and resume semantics; and
- never claim that a cache hit occurred when only local eligibility is known.

The CLI/headless JSON result shall include the same structured fields. Human
rendering may be concise, but machine-readable output must retain the full
manifest references and diagnostics.

## 12. Security and privacy considerations

1. The artifact store must resolve every path through the parent workspace
   access policy. It must reject traversal, symlink escapes, and paths outside
   the authorized target.
2. Browser captures must inherit the configured allowed-domain and browser
   backend policy. Liberal VPS configuration does not remove the application
   boundary.
3. Screenshot metadata must not include request headers, cookies, page storage,
   or raw browser diagnostics containing credentials.
4. Research documents may contain reference-site content. The workflow must
   follow the existing user authorization and project policy and must not
   broaden network or filesystem authority to make a capture succeed.
5. Manifest and checkpoint diagnostics must redact secrets using the existing
   redaction conventions.
6. Artifact deletion or cleanup must target the exact run directory and use a
   recoverable/explicit policy where available.

## 13. Testing strategy

### 13.1 Unit tests

Add deterministic unit coverage for:

- phase-plan construction, duplicate/missing/terminal-edge validation;
- accurate static and dynamic phase indexes;
- profile graph selection and skipped-phase reasons;
- model-override lookup and `run_phase()` propagation;
- stable tool-bundle ordering and cache fingerprints;
- artifact path validation, symlink/traversal rejection, hashing, and
  idempotent writes;
- manifest atomic update, revisioning, missing/corrupt record detection, and
  secret redaction;
- screenshot identity, deduplication, viewport metadata, and degraded browser
  status;
- compact context/checkpoint codec round trips and schema migration;
- re-entry target validation and downstream invalidation;
- retry-budget classification and compact retry prompts; and
- dynamic PAGE route/page-index behavior.

### 13.2 Integration tests

Use temporary workspaces, fake providers, fake browser managers, and fake
MCP registries to verify:

- a complete static-profile run writes research and evidence manifests;
- application and production profiles include only their selected phases;
- reference and implementation screenshots share route/viewport identities;
- all parent turns use one conversation ID and one memory instance;
- phase transitions persist typed context and manifest revision together;
- interruption at every phase boundary resumes without restarting earlier work;
- missing artifact, changed hash, and corrupt manifest produce recoverable
  diagnostics;
- browser unavailable/degraded paths remain explicit;
- provider/tool transaction errors do not create malformed conversation
  history;
- capability and workspace policy remain effective after profile changes;
- re-entry marks only the declared dependent artifacts stale; and
- no duplicate artifact or screenshot is produced by a retry.

### 13.3 End-to-end tests

Cover these user journeys:

1. static reconstruction from URL through final evidence package;
2. application reconstruction with mocked API/data layer;
3. production profile with infrastructure phases;
4. user clarification during INIT and continuation after the answer;
5. Esc pause during visual research followed by `--resume`;
6. `--continue` restoring a selected reconstruct run;
7. visual rejection re-entering design system and revalidating downstream work;
8. unknown re-entry target rejected without changing phase;
9. missing Playwright/CloakBrowser reported as degraded rather than success;
10. repeated retry proving idempotent artifact/screenshot writes; and
11. final manifest visible in TUI and headless JSON output.

### 13.4 Performance and regression tests

Add offline benchmarks for phase-plan construction, tool-bundle compilation,
prompt-contract size, manifest update time, checkpoint size, screenshot
deduplication, and resume rehydration. Add a regression test that a large
research corpus does not recreate the old artificial checkpoint-size failure:
the body is externalized and the checkpoint remains valid without a fixed
1,000,000-byte limit.

## 14. Rollout and migration

### Milestone 1 — graph correctness

- introduce the phase-plan model;
- generate/validate phase metadata and dispatch;
- fix profile-aware progress counts;
- preserve legacy phase names and checkpoint decoding.

### Milestone 2 — evidence durability

- add the artifact store and manifest;
- write research receipts and hashes;
- link existing browser artifact records;
- add migration for contexts that contain only legacy artifact labels.

### Milestone 3 — cache and execution efficiency

- compile one stable tool bundle per cache epoch;
- propagate model overrides;
- add bounded retry classes and phase budgets;
- add dynamic phase digests and large-context externalization.

### Milestone 4 — profiles and re-entry

- add static/application/production/custom profiles;
- add validated re-entry/invalidation graph;
- expose skipped/stale evidence in TUI and JSON diagnostics.

### Milestone 5 — verification and default migration

- run unit/integration/E2E/performance matrix;
- enable static profile selection for clearly static requests;
- retain an explicit compatibility profile for existing callers until
  migrated checkpoints are verified;
- update workflow and storage documentation and implementation evidence.

If a manifest cannot be migrated safely, the user must see an actionable
message explaining that the run can be inspected but not resumed from the
legacy state. The workflow must not silently start a new run.

## 15. Acceptance criteria

- [ ] The active `reconstruct_site` graph has one authoritative phase-plan
      representation, and fresh execution and resume consume the same plan.
- [ ] The progress counter reports the actual selected graph; the stale 22-phase
      value is eliminated.
- [ ] All parent phase turns, retries, re-entry, pause, and resume preserve the
      original session conversation ID and memory instance.
- [ ] Required research outputs create verified durable artifact records and an
      atomically revisioned manifest.
- [ ] Screenshot records identify route, viewport, role, backend, hash, and
      availability, and are linked to the reconstruct run.
- [ ] Repeating a phase does not duplicate identical artifacts or screenshots.
- [ ] Large research bodies are externalized and checkpoint serialization has
      no arbitrary 1,000,000-byte ceiling.
- [ ] Dynamic phase state and artifact changes do not alter the stable prompt
      policy or stable tool schemas within a cache epoch.
- [ ] Model overrides configured for reconstruct phases reach the provider.
- [ ] Static, application, production, and validated custom profiles select
      explicit phase graphs and record skipped phases.
- [ ] Unknown re-entry targets are rejected, not redirected to visual
      validation; valid re-entry records invalidated downstream artifacts.
- [ ] Resume verifies artifact integrity and reports missing/corrupt evidence
      as a recoverable diagnostic.
- [ ] Browser/MCP unavailability is explicit and cannot become fabricated
      research evidence.
- [ ] Capability, workspace, network, approval, and trust policies remain
      effective for all profile and artifact paths.
- [ ] Unit, integration, E2E, and offline performance/regression tests cover
      the requirements in Section 13.
- [ ] Workflow, storage, custom-workflow, and testing guides document the new
      artifact, screenshot, profile, cache, and resume contracts.

## 16. Assumptions and decisions

| ID | Decision/assumption | Rationale |
|---|---|---|
| AD-01 | The parent workflow keeps one conversation ID for its complete session | Preserves the user's cross-phase context and existing resume contract |
| AD-02 | Artifact bodies live on disk; checkpoints keep references and digests | Prevents unbounded checkpoint growth without discarding evidence |
| AD-03 | Browser artifact storage remains the canonical bounded screenshot boundary | Avoids a second browser persistence implementation |
| AD-04 | Stable tools are compiled once per cache epoch | Reduces schemas while preserving cache correctness |
| AD-05 | Profiles are explicit and visible to the user | Infrastructure scope materially changes cost and output |
| AD-06 | Dynamic PAGE work is counted separately from static graph phases | A route count cannot be known until reconnaissance completes |
| AD-07 | Existing legacy checkpoints remain readable where safe | Backward compatibility is required; unsafe migration fails closed |
| AD-08 | Filesystem/provider limits remain real operational errors | “No arbitrary checkpoint limit” does not mean ignoring OS/storage failures |

Open decisions during implementation:

1. Choose the final artifact root consistent with the canonical storage guide.
2. Decide whether required screenshot capture is strict for every route or
   profile-configurable when a reference blocks automation.
3. Select the inline artifact threshold and whether it is configurable per
   project, without reintroducing a global checkpoint ceiling.
4. Define the default profile when an existing invocation has no explicit
   scope and the reference is not clearly static.
5. Determine whether any future read-only research fan-out is implemented as
   batched browser operations or linked child tasks; concurrent parent memory
   mutation is prohibited either way.

## 17. Definition of done

This PRD is complete when the acceptance criteria are verified against the
current source tree, all supported workflow/profile paths have durable evidence
and exact resume behavior, prompt-cache diagnostics show stable contracts,
performance benchmarks demonstrate the intended reductions, documentation is
updated, and the final implementation evidence is recorded in this document.
