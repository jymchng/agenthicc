---
title: "PRD-174: Tool-Aware create_workflow Authoring and Safe Publication"
status: Implemented
version: 1.0.0
date: 2026-08-25
scope: create_workflow inspection tools, effective tool catalogs, generated workflow validation, and publication
related_prds:
  - PRD-100
  - PRD-154
  - PRD-159
  - PRD-160
  - PRD-161
  - PRD-163
  - PRD-168
  - PRD-169
  - PRD-172
  - PRD-173
tags:
  - workflows
  - authoring
  - tools
  - capabilities
  - validation
  - checkpoints
  - security
---

# PRD-174 — Tool-Aware `create_workflow` Authoring and Safe Publication

## Executive summary

This PRD records an audit of the tools used by the `create_workflow` workflow
and defines the improvements needed to make downstream workflow creation
reliably tool-aware.

The audit found no evidence that the current browser, MCP, capability, or
self-inspection tool implementations are simply obsolete. The current source
contains the optional CloakBrowser and Playwright tools, capability metadata,
five bounded agenthicc source/documentation inspection tools, MCP registry
integration, workspace propagation, prompt-cache guidance, checkpoint codecs,
and strict generated-workflow validation. The focused unit, integration, and
E2E tests exercise those contracts.

The weakness is at the authoring boundary. `create_workflow` describes parts of
the live API with hardcoded prose, reports browser defaults rather than the
effective session, does not expose a complete effective tool/capability matrix,
does not explicitly advertise every global introspection tool in its prompts,
and does not record the tool/configuration facts used to make the design. The
generation phase also writes directly into the final project workflow
directory. Validation checks syntax, importability, plugin metadata, phase
graphs, cache/question/workspace rules, transitions, codecs, and error
recovery, but it does not yet perform a bounded generated-workflow smoke run or
publish from an isolated, atomic draft.

Therefore the proposed work is not a wholesale rewrite of the existing tool
implementations. It is a source-of-truth and lifecycle enhancement around
`create_workflow`: expose an authoritative, effective, bounded tool catalog;
teach the agent the actual session constraints; stage and validate generated
packages; run safe contract-level smoke checks; and publish only an approved,
validated artifact. Existing workflows, tool names, capability gates,
workspace policy, prompt-cache placement, conversation identity, and
checkpoint/resume semantics remain compatible.

## 1. Evidence-backed current-state study

The audit was performed against the current source tree and tests on
2026-08-25. The most recent relevant history includes the PRD-173 recoverable
workflow error implementation, the MCP guidance/reload work, the session
ownership work, and the prompt/cache-contract documentation update. The
current tree, rather than historical PRD examples, is authoritative.

### 1.1 Surfaces inspected

- `src/agenthicc/workflows/create_workflow/runner.py`
- `src/agenthicc/workflows/create_workflow/definition.py`
- `src/agenthicc/workflows/create_workflow/state.py`
- `src/agenthicc/workflows/create_workflow/phase_tools.py`
- `src/agenthicc/workflows/create_workflow/inspection_tools.py`
- `src/agenthicc/workflows/create_workflow/validation.py`
- `src/agenthicc/workflows/plugin.py`
- `src/agenthicc/workflows/code_plan/runner.py`
- `src/agenthicc/workflows/config.py`
- `src/agenthicc/agent_tools.py`
- `src/agenthicc/tools/capabilities.py`
- `src/agenthicc/tools/introspect/agent_tools.py`
- `src/agenthicc/tools/cloakbrowser/agent_tools.py`
- `src/agenthicc/tools/playwright/agent_tools.py`
- MCP registry/manager integration and configuration
- `tests/unit/test_create_workflow.py`
- `tests/integration/test_create_workflow_integration.py`
- `tests/integration/test_cloakbrowser_integration.py`
- `tests/e2e/test_create_workflow_state_machine_e2e.py`
- `tests/unit/test_introspect_tools.py` and related E2E coverage

### 1.2 Current tool and workflow audit

| Surface | Current implementation | Assessment |
|---|---|---|
| Phase transition tools | `phase_tools.py` uses event-backed closures, input validation, and bare `@tool_control` above `@tool()` | Current and correctly owned. Keep the contract; make its schema/evidence easier to inspect. |
| `describe_phasespec` | Reads fields, types, and defaults from the live `PhaseSpec` dataclass, but uses a hardcoded purpose map | Partly current. Field additions are visible, but purpose text and semantic rules can drift. |
| Capability catalog | Reads the live `ToolCapability` values, but descriptions are hardcoded and it does not show phase/mode resolution | Partly current. It is not enough to explain why a concrete tool is available or blocked. |
| Role catalog | Reads `PhaseRole` string constants, but does not show role defaults, resolved capabilities, or role prompt ownership | Partly current. It exposes names, not the effective contract. |
| CloakBrowser/Playwright descriptions | Read live tool-name tuples and default settings; report optional dependencies, backend selection, operation-id policy, and security notes | Current for general guidance, incomplete for a particular session. They do not report live availability, actual backend health, schemas, or effective policy without leaking secrets. |
| Browser tools in authoring turns | `_base_tools()` deliberately filters browser tools from `create_workflow` turns while passing browser manager context | Intentional but implicit. The prompts should say that authoring may inspect the browser contract but does not browse; generated workflows receive session-owned browser tools when configured. |
| Global source/doc introspection | Five bounded, read-only, exploratory-tagged tools are registered globally: list/read/search docs and inspect/search source | Current and tested. The create-workflow prompts mention only a subset, so guidance is incomplete. |
| Built-in filesystem, Git, execution, MCP, and memory tools | Supplied through the normal agent tool registry plus `_base_tools()` and current mode filtering | Current runtime path. `create_workflow` does not snapshot the actual names, schemas, availability, or capability decisions given to the turn. |
| MCP tools | Added from the current registry and isolated on registry errors | Current integration, but the authoring agent receives no bounded server/tool inventory or unavailable-server explanation. |
| Prompt/cache contract | `CACHE_CONTRACT`, `build_workflow_prompt_contract`, stable-tool partitioning, dynamic phase context, and diagnostics are in place | Current and aligned with PRD-163. Generated workflows are taught to inherit it; evidence is not yet stored as an authoring snapshot. |
| Checkpoint/resume contract | Typed `CreateWorkflowContext`, shared session memory, workflow handle attachment, codecs in generated custom workflows, and PRD-173 recovery checks | Current foundation. Generated-workflow validation should add an executable contract check for resume and failure recovery. |
| Example/template | A large static `_RUNNER_EXAMPLE` is returned by both `show_example_workflow` and `show_workflow_template` | Valid but duplicated and expensive. It has no source version, chunk/window API, or provenance link. |
| Generated package writes | Agent writes directly to `.agenthicc/workflows/<name>/` with `make_directory`, `write_file`, and `append_file` | Main lifecycle gap. Partial files, stale sibling modules, or a failed repair can remain in the published directory. |
| Deterministic validator | Containment, file shape, syntax, imports, plugin metadata, phase graph, browser import restrictions, runner/checkpoint rules, cache contract, transition decorators, workspace policy, and recovery rules | Strong static/import validation. It does not yet validate a manifest, run a no-side-effect smoke contract, or prove every phase can resume. |
| Tests | Broad unit, integration, and E2E coverage exists for the current state machine, inspection tools, validator, browser boundary, generated example, repair loop, and registry discovery | Good regression base. The missing tests are effective-session catalogs, staged publication, generated smoke/resume execution, and unavailable optional integrations. |

### 1.3 Findings

1. **The tools are not uniformly stale; the authoring catalog is.** The live
   decorators, tool signatures, optional extras, and registry paths are current,
   but the authoring layer has duplicated semantic prose and defaults-only
   reporting.
2. **The design agent cannot see the effective session.** A tool can be blocked
   by the active mode, phase allowlist, missing optional dependency, selected
   browser backend, MCP connection state, or workspace policy. Current tools
   expose pieces of this, but not one bounded, redacted decision matrix.
3. **The prompts under-advertise current introspection.** The globally available
   `list_agenthicc_docs`, `read_agenthicc_doc`, and `search_agenthicc_source`
   tools are not consistently named in the create-workflow authoring guidance.
4. **Tool names without signatures are insufficient for generation.** Browser
   tools in particular require `page_id`, bounded selectors/conditions, and an
   `operation_id` for non-status operations. Generated workflows need this
   contract without access to raw browser clients or secrets.
5. **The generated package has no draft boundary.** A repair is performed in
   the same directory that the loader will later discover. There is no exact
   manifest, stale-file cleanup contract, atomic publication point, or clear
   distinction between draft, validated, and published.
6. **Import validation is not runtime contract validation.** Importing a plugin
   catches many defects, but cannot prove that the custom runner dispatches all
   states, reattaches memory, preserves context, calls `run_phase()` correctly,
   or fails through the framework on an injected provider/tool error.
7. **`CreateWorkflowContext` lacks authoring provenance.** It stores phase
   artifacts and cache diagnostics, but not the tool catalog version, effective
   capability decisions, dependency preflight, generated manifest, validation
   evidence, or publication state that led to the result.
8. **`PhaseSpec.on_error` is visible but reserved.** The current inspection
   surface reports it, while the generated guidance may cause an agent to design
   around semantics the runtime does not yet execute. This must be made explicit
   or implemented before generated workflows rely on it.
9. **The generation prompts contain maintainability debt.** The chunked writing
   instructions are valuable, but the prompt contains duplicated wording and
   the static example/template is maintained separately from the actual runner
   implementation.

## 2. Problem statement

`create_workflow` is itself a robust state machine, but a downstream agent
currently has to infer too much about the tool surface it is using. It can
produce a workflow that passes static validation while being poorly matched to
the active session: unavailable optional tools, blocked capabilities, wrong
browser backend, inaccessible workspace targets, missing MCP servers, or a
misunderstood generic/custom prompt contract.

The direct-write lifecycle adds a second risk. A failed or interrupted
generation can leave source files in the discovery directory. The next repair
turn then edits an ambiguous mixture of current and stale files, and an
external loader can observe a partially written package.

The product invariant for this PRD is:

> A generated workflow is designed against a bounded, redacted snapshot of the
> effective tool/session contract; it is published only after deterministic
> validation and a safe smoke contract pass; and its draft, validation evidence,
> checkpoint state, and publication identity remain recoverable.

## 3. Goals

- Make the create-workflow authoring surface derive tool metadata from live
  registries, callables, decorators, and configuration wherever possible.
- Show the design/generation agent the effective capabilities and optional
  integration state for the current session without exposing secrets.
- Make browser, MCP, workspace, memory, prompt-cache, transition, and
  checkpoint contracts explicit to generated workflows.
- Preserve the existing event-driven outer/inner state-machine architecture and
  phase-transition-only-via-tool-call rule.
- Stage generated packages separately from published workflow directories.
- Validate a complete file manifest, plugin graph, static contracts, and a
  bounded no-external-side-effect smoke contract before publication.
- Make generated custom workflows checkpoint-aware, same-conversation aware,
  cache-contract compliant, workspace-policy compliant, and failure-recoverable
  by construction.
- Keep existing workflows, tool names, configuration formats, and legacy
  validators working during migration.

## 4. Non-goals

- Replacing `ToolCapability`, the mode gate, `PhaseSpec`, `WorkflowPlugin`,
  `CodePlanRunner`, `WorkflowConfig`, or the existing tool registry.
- Making browser tools available inside the create-workflow authoring turn by
  default. Authoring should inspect the browser contract; generated workflows
  may use the session-owned browser adapter when configured and permitted.
- Auto-installing CloakBrowser, Playwright, MCP servers, or any other optional
  dependency.
- Exposing API keys, authorization headers, cookies, browser profiles, MCP
  environment values, full prompts, or conversation contents in inspection
  results, checkpoints, logs, or generated source.
- Executing arbitrary generated workflow code against real external services as
  part of validation.
- Changing the current browser security defaults or workspace mode policy.
- Making `PhaseSpec.on_error` executable without a separately specified runtime
  contract and tests.
- Replacing human design approval or validation transition tools with prose
  parsing.

## 5. Target data flow

### 5.1 Current flow

```text
User request
    │
    ▼
TUI/session WorkflowConfig
    │  session memory, conversation_id, mode, workspace, MCP/browser managers
    ▼
CreateWorkflowRunner._base_tools()
    ├─ project/plugin tools + MCP tools, filtered by active mode
    ├─ memory tools
    └─ browser tools intentionally excluded from authoring turns
    │
    ▼
DESIGN / GENERATE phase tools
    ├─ local inspection tools (partly hardcoded metadata)
    ├─ global built-in introspection and filesystem/exec/Git/MCP tools
    └─ event-backed transition/question tools
    │
    ▼
Agent turn with stable CACHE_CONTRACT + dynamic phase context
    │
    ├─ generation writes directly to .agenthicc/workflows/<name>/
    ├─ VALIDATE imports and statically checks the reported path
    └─ agent approve/reject tool selects SUMMARY or GENERATE
    │
    ▼
SUMMARY / workflow reload
```

### 5.2 Target flow

```text
User request
    │
    ▼
Session-owned AuthoringSnapshot
    ├─ catalog version and source fingerprints
    ├─ effective tool names, schemas, capabilities, mode decisions
    ├─ browser/MCP optional dependency and availability status
    ├─ workspace policy summary (no secret paths beyond approved summaries)
    └─ cache, question, transition, checkpoint, and resume contracts
    │
    ▼
DESIGN
    ├─ live inspection tools may refresh bounded catalog sections
    ├─ agent asks focused user questions for material ambiguity
    └─ human approves a design tied to the snapshot version
    │
    ▼
GENERATE into isolated draft/<run-id>/<workflow-name>/
    ├─ exact file manifest and content limits
    ├─ no publication-directory writes
    └─ repairs replace/update only the draft manifest
    │
    ▼
PRE-FLIGHT + VALIDATE
    ├─ path/manifest/syntax/import/plugin/graph checks
    ├─ capability, browser, MCP, workspace, cache, question, transition,
    │  checkpoint, resume, and failure-contract checks
    ├─ bounded fake-provider/no-side-effect smoke execution
    └─ agent approve/reject remains a required tool call
    │
    ├─ failure: retain recoverable draft/checkpoint and return to GENERATE
    └─ success: record immutable validation evidence
    │
    ▼
ATOMIC PUBLISH
    ├─ publish only the approved manifest
    ├─ remove or quarantine stale generated siblings safely
    ├─ refresh the workflow registry
    └─ record published path, fingerprint, and source revision
    │
    ▼
SUMMARY / run generated workflow with the parent session contracts
```

The snapshot and manifest must be bounded and JSON-compatible. Conversation
history remains in the existing session `conversation_id` and memory store; it
must not be duplicated into the workflow context or stable prompt.

## 6. Functional requirements

### FR-1 — Authoritative authoring catalog

Add a versioned authoring-catalog service or equivalent inspection surface
owned by the existing tool registry boundary. It must derive, in deterministic
order, for every tool visible to the relevant phase:

- canonical tool name and concise description;
- callable argument names, required/optional status, defaults, and bounded
  schema information;
- `ToolCapability` values and exploratory/presentation metadata separately;
- source/group (builtin, plugin, MCP, memory, browser, workflow-local);
- mode and phase availability decision, including the reason for a block;
- optional dependency/backend information where applicable; and
- a catalog/schema version and source fingerprint sufficient to detect drift.

The catalog must prefer introspection of the live callable and registry over
duplicated descriptions. A hand-maintained semantic explanation may supplement
live metadata, but it must identify its version and have a test that detects
unknown or missing enum/tool values.

### FR-2 — Effective session snapshot

Expose a read-only, bounded tool that reports the effective authoring session,
including:

- active mode and blocked capabilities;
- phase role/default capabilities and the resolved phase allowlist;
- selected browser backend, enabled state, optional dependency status, and
  policy summary;
- MCP server names, connection state, required/optional disposition, and tool
  names without URLs, headers, environment values, or credentials;
- workspace policy mode and a safe root/policy summary;
- prompt-cache contract version and checkpoint/resume contract version; and
- the exact catalog/snapshot identifier used by the current design.

The result must not make network calls, import generated code, or return raw
secrets. Health probes are optional and must be explicit, bounded, and
read-only.

### FR-3 — Complete introspection guidance

Update create-workflow prompts and tests to name all current global
self-inspection tools:

`list_agenthicc_docs`, `read_agenthicc_doc`, `search_agenthicc_docs`,
`inspect_agenthicc_source`, and `search_agenthicc_source`.

The authoring guide must tell the agent when to use live source inspection,
when to use the structured catalog, and when not to guess. Inspection tools
remain exploratory/read-only and available in capability-restricted modes.

### FR-4 — Browser and MCP contract reporting

Extend or replace the current browser inspection responses so they expose live
schemas and constraints for both CloakBrowser and Playwright while preserving
the existing tool names and optional-extra behavior. The report must include:

- status/open/snapshot/action/close grouping;
- capability tags and operation-id requirements;
- bounded argument constraints and artifact locations;
- selected backend versus non-selected backend;
- unavailable/dependency-missing state and actionable installation guidance; and
- effective network/policy summary without secrets.

Do the equivalent for MCP at the registry boundary. A server failure must
identify only the server name and safe failure category, while other available
servers remain visible.

### FR-5 — Capability-resolution explanation

Teach the design and generation agent the difference among:

1. a tool's declared capability metadata;
2. the phase's explicit `allowed_capabilities_override`;
3. the phase's `allowed_capabilities` and role default;
4. the active mode's blocked capabilities; and
5. optional availability or workspace/network policy.

Provide a deterministic matrix or decision trace for a named tool. Generated
workflows must never assume that `mode_override="Yolo"` makes a missing tool,
missing dependency, disallowed workspace path, or unavailable MCP server
available.

### FR-6 — Structured authoring context

Extend `CreateWorkflowContext` with bounded, checkpoint-safe authoring
provenance, at minimum:

- catalog/snapshot version and fingerprint;
- selected tool names or a bounded reference to the snapshot;
- dependency/preflight summary;
- draft manifest and draft fingerprint;
- validation evidence identifier and categories;
- publication status/path/fingerprint; and
- question/answer ledger metadata sufficient to show that required questions
  were answered, without duplicating the full conversation.

Session memory, browser managers, MCP clients, locks, events, provider clients,
and secrets must remain reattached runtime objects and must not enter the
checkpoint payload. Existing payloads must restore with safe defaults.

### FR-7 — Isolated draft generation

Generation must write to a run-owned staging directory inside the authorized
workspace, not directly to the discoverable published workflow directory. The
runner must record an exact manifest containing relative paths, byte/line
limits, content hashes, and the workflow name.

The file tools remain responsible for writes and workspace policy enforcement.
The framework may create the staging directory and perform the final atomic
publication through a narrowly scoped adapter. It must reject traversal,
symlink escapes, undeclared files, duplicate entry points, and files outside
the manifest. A repair cycle must operate on the same draft and must not
silently retain stale sibling source files.

### FR-8 — Stronger generated-workflow validation

Keep the current static/import validator and add structured categories and
evidence for:

- manifest and containment correctness;
- plugin identity, params, phase graph, and reserved names;
- generic/custom runner selection and runner implementation;
- transition-tool decorator/import order and event-backed transition behavior;
- cache contract and stable/dynamic prompt separation;
- ask-user policy and question handling;
- workspace inheritance and browser-client boundary;
- checkpoint codecs, JSON-compatible bounded payload, memory reattachment, and
  `resume(context)` dispatch without fresh-run restart;
- framework-owned error finalization without swallowed exceptions or ordinary
  terminal failed checkpoints; and
- optional browser/MCP dependency assumptions.

Validation reports must distinguish errors, warnings, skipped checks, and
environment-unavailable checks. An environment-unavailable optional feature is
not a generated-code error when the workflow declares a valid fallback.

### FR-9 — Bounded generated-workflow smoke contract

After static validation and before publication, run a deterministic smoke
contract in an isolated test harness. It must use a fake provider/agent turn and
non-networking tool adapters. At minimum it must prove that:

- the plugin loads through the normal loader boundary;
- the declared initial state and phase graph are reachable;
- a successful transition tool advances only through the runner's event path;
- a prose-only turn does not advance a phase;
- the custom runner calls the supported `run_phase()` boundary;
- a checkpoint payload excludes runtime-only objects;
- restore reattaches the supplied session memory and resumes the saved state;
- an injected provider/tool error reaches framework failure finalization; and
- the workflow does not make an external network/browser/MCP call during smoke.

If a workflow explicitly declares a non-agent or human phase, the harness must
use the corresponding supported fixture rather than inventing an agent turn.

### FR-10 — Approval and atomic publication

The existing design approval, generation-complete, and validation approval /
rejection tools remain the only phase transitions. Publication must occur only
after:

1. the design was approved;
2. the draft manifest is complete;
3. deterministic validation passes;
4. the bounded smoke contract passes; and
5. the validation agent calls `approve_workflow(summary)`.

Publication must be atomic from the workflow loader's perspective. It must
record the draft fingerprint, published fingerprint, workflow name, run ID,
catalog snapshot, validation evidence, and publication timestamp. A failed
publication must leave the previous published workflow intact and retain a
recoverable draft/checkpoint.

### FR-11 — Optional integrations and graceful degradation

The generated design must state whether it requires CloakBrowser, Playwright,
MCP, or other optional integrations. If a required dependency is unavailable,
validation must reject the workflow with a concrete fix. If the design includes
a fallback, validation may pass and must record the degraded path.

`create_workflow` itself must continue to omit browser action tools from its
authoring turns unless an explicit future product decision changes that
boundary. It must still accurately describe how a generated workflow receives
session-owned browser tools and policy.

### FR-12 — Generated workflow runtime contract

The authoring prompt, example, validator, and smoke harness must jointly require
generated workflows to:

- use the outer state loop, bounded inner turn loops, and event-backed tool
  transitions;
- use one session `conversation_id` and injected `session_memory` across phases
  and resume;
- attach typed context before the first failure-prone call;
- preserve run ID, current state, phase iteration, artifacts, and all necessary
  workflow data in checkpoints;
- omit runtime-only objects from payloads and reattach supplied memory;
- use `CodePlanRunner.run_phase()` or the shared prompt-contract helper;
- keep literal stable policy separate from dynamic phase state and artifacts;
- ask focused user questions for material ambiguity and wait rather than guess;
- inherit `WorkflowConfig.workspace_scope` and `workspace_access`; and
- re-raise ordinary errors to the framework failure finalizer.

### FR-13 — Compatibility and migration

- Existing inspection-tool names and response keys remain available during one
  compatibility period; new fields are additive.
- Existing generated workflows continue to load if they satisfy the current
  validator. Legacy direct-published workflows are reported with a migration
  warning rather than deleted or silently rewritten.
- `show_example_workflow()` and `show_workflow_template()` retain their current
  styles and source shape while gaining version/provenance and optional bounded
  windows/chunks.
- Existing checkpoints without authoring provenance restore with `legacy` or
  `unknown` values and remain resumable when their current codec passes.
- The project-local workflow registry remains the discovery and execution
  boundary; this PRD does not introduce a second plugin runtime.

## 7. Non-functional requirements

### NFR-1 — Source truth and drift detection

The catalog must be generated from live registries/callables/configuration.
Tests must fail when a capability, built-in group, browser tool, MCP tool
metadata field, or introspection tool is added without catalog coverage.

### NFR-2 — Determinism and bounded output

Sort tools, groups, phases, manifest paths, and diagnostic categories
deterministically. Cap every description, schema, manifest, validation report,
and snapshot by item count, bytes, and nesting depth. Large examples and source
files must support windows or chunks rather than requiring one unbounded tool
result.

### NFR-3 — Security and privacy

Catalogs and reports must redact credentials, headers, cookies, environment
values, raw MCP URLs where policy requires, full conversation content, and
provider prompts. Read-only catalog tools must not import generated code or
perform arbitrary execution. Validation/smoke execution remains separately
execute-gated and contained by the workspace policy.

### NFR-4 — Isolation and atomicity

Draft writes, validation imports, smoke execution, and publication must not
expose partial generated packages to the normal loader. Publication must be
atomic and failure-safe. Retried tool calls and repeated validation must be
idempotent or leave explicit, bounded evidence of the prior attempt.

### NFR-5 — Performance

Catalog construction must not add an LLM/provider round trip. Reuse a
session-scoped immutable snapshot when inputs have not changed. Introspection
and validation must be bounded and cancellable, and browser/MCP health must not
block authoring unless the user explicitly requests a probe.

### NFR-6 — Observability

TUI and headless output must distinguish `draft`, `validated`, `published`,
`rejected`, `degraded`, and `unavailable` without printing secrets. Logs and
checkpoints must include correlation/run IDs, catalog/manifest fingerprints,
validation categories, and safe failure kinds.

### NFR-7 — Maintainability and typing

Use the current kernel/session/workflow ownership boundaries, concrete
parameterized types, existing capability decorators, and existing workspace,
checkpoint, and prompt-contract helpers. Do not create parallel tool registries,
workspace policies, conversation stores, or workflow runtimes.

## 8. Acceptance criteria

### Authoring surface

- [x] A current-session authoring catalog lists every tool visible to each
  create-workflow phase with deterministic names, signatures, capabilities,
  source group, and availability reason.
- [x] The catalog includes all five global source/documentation introspection
  tools and the prompts name all five.
- [x] `describe_phasespec`, capability, role, browser, and MCP reports are
  backed by live metadata and expose a schema/version or fail drift tests.
- [x] A named-tool decision trace explains capability metadata, role/phase
  resolution, active mode blocking, optional availability, and workspace/network
  policy.
- [x] No catalog or session snapshot contains secrets or unbounded transcript
  content.

### Generation and validation

- [x] A generated package is created in an authorized run-owned draft directory
  with an exact manifest; the normal loader cannot observe a partial draft.
- [x] Repair cycles reuse the draft, reject undeclared/traversal/symlink files,
  and cannot retain stale generated siblings outside the manifest.
- [x] The validator emits structured errors, warnings, skipped checks, and
  evidence for manifest, plugin, graph, capability, browser/MCP, cache, question,
  workspace, transition, checkpoint, resume, and failure-recovery contracts.
- [x] A valid custom runner passes a deterministic no-network smoke contract.
- [x] An injected provider/tool failure proves that framework failure finalization
  is reached and the generated runner does not swallow or terminalize the error.
- [x] A generated custom runner's restored context resumes at the saved state,
  preserves the same run/conversation identity, and reattaches the supplied
  session memory.
- [x] A missing required optional dependency rejects the workflow with an
  actionable, redacted diagnostic; an optional fallback is recorded as degraded.

### Approval, publication, and compatibility

- [x] Prose cannot transition any create-workflow phase; all transitions remain
  successful tool calls.
- [x] Publication is impossible before design approval, deterministic validation,
  smoke success, and validation-agent approval.
- [x] Atomic publication leaves the previous published package intact when it
  fails and records a resumable draft/checkpoint.
- [x] Successful publication records run ID, workflow name, manifest and source
  fingerprints, catalog snapshot, validation evidence, and publication state.
- [x] Existing inspection-tool names, legacy workflows, existing valid
  checkpoints, and current configuration formats continue to work.
- [x] `/workflows reload` or the equivalent registry refresh discovers the
  published package exactly once, without builtin shadowing.

## 9. Test plan

### Unit tests

- Live catalog extraction from decorated callables, including signatures,
  defaults, capabilities, exploratory metadata, source groups, and malformed
  metadata fail-closed behavior.
- Phase role/default/override/mode capability resolution and named-tool decision
  traces.
- Browser and MCP optional states, redaction, deterministic ordering, and
  unavailable-server isolation.
- Prompt/inspection coverage for all current tools, `PhaseSpec` fields, cache,
  question, workspace, and checkpoint guidance.
- Snapshot bounds, JSON serialization, legacy restore defaults, and provenance
  fingerprints.
- Draft path containment, manifest creation, duplicate/stale/traversal/symlink
  rejection, idempotent repair, and atomic publication failure behavior.
- Static validation categories and smoke-harness fixtures for successful and
  failing transition, resume, cache, error, and no-network cases.

### Integration tests

- A real `WorkflowConfig` with Safe/Plan/Yolo modes produces the expected
  effective catalog and filters the same tools as an agent turn.
- CloakBrowser and Playwright enabled/disabled/missing-runtime states are
  accurately represented without making a browser call.
- Multiple MCP servers with one failed optional connection still expose the
  healthy server's tools and a redacted failure for the other.
- Draft generation, validation, rejection/repair, approval, atomic publication,
  registry reload, and builtin-shadowing protection use the real loader.
- The real workflow handle, conversation ID, session memory, checkpoint store,
  and failure finalizer preserve exact identity through pause/resume.

### End-to-end tests

- Design a simple custom workflow, answer a clarifying question, approve it,
  generate it in chunks, validate it, publish it, reload the registry, and run it.
- Reject an invalid generated workflow, repair it in the same draft, and prove
  that no incomplete package was published.
- Generate a workflow requiring browser or MCP functionality with the dependency
  unavailable, then verify the actionable rejection/fallback path.
- Interrupt or fail generation, restart/resume the same create-workflow run, and
  verify catalog, design, draft manifest, phase, artifacts, memory, and
  conversation identity are retained.
- Use a generated custom runner with an injected provider/tool failure and verify
  the recoverable error checkpoint and exact resume phase.
- Verify stable prompt/tool regions remain unchanged while phase context and
  artifacts evolve, with no rolling summary or transcript duplication in the
  cache-stable contract.

## 10. Rollout and migration

1. Add the catalog and snapshot as additive read-only functionality; retain the
   current inspection tools and response keys.
2. Update prompts and examples to consume the catalog and explicitly state the
   browser-authoring boundary, effective capability rules, and all introspection
   tools.
3. Add manifest/draft support behind a compatibility flag or internal rollout
   switch while existing direct-published workflows remain loadable.
4. Enable structured validation and smoke checks for newly generated workflows.
5. Enable atomic publication by default after the existing unit/integration/E2E
   matrix is green.
6. Add migration warnings and an explicit cleanup command for legacy partial
   drafts; never delete a published workflow implicitly.

The implementation should be split into small commits aligned with the current
ownership boundaries: catalog/introspection, context/checkpoint provenance,
draft/manifest publication, validation/smoke harness, prompt updates, and test
coverage.

## 11. Security, failure, and operational assumptions

- The user may configure permissive browser/network policy, but generated
  workflows still use the session-owned browser/network adapters and cannot
  bypass policy with raw clients.
- A generated workflow's source is trusted only at the existing execute-gated
  validation boundary. Read-only catalog tools must not import it.
- A failed optional integration is a data point for design/validation, not a
  reason to crash all other authoring tools.
- If the process stops during draft generation, the durable create-workflow
  checkpoint records the draft path and manifest state; resume continues the
  same phase and session conversation.
- If atomic publication cannot complete, the prior published workflow remains
  authoritative and the draft remains available for recovery.
- `PhaseSpec.system_prompt_override` remains metadata consumed automatically by
  the generic runner; a custom runner owns its explicit `run_phase()` prompt.
  The catalog and prompts must state this distinction.
- `PhaseSpec.on_error` is documented as unsupported/reserved until its runtime
  semantics are separately implemented; generated workflows must not rely on it.

## 12. Verification commands

The implementation is complete only when the relevant focused and repository
checks pass:

```bash
uv run pytest tests/unit/test_create_workflow.py -q
uv run pytest tests/unit/test_introspect_tools.py -q
uv run pytest tests/integration/test_create_workflow_integration.py -q
uv run pytest tests/integration/test_cloakbrowser_integration.py -q
uv run pytest tests/e2e/test_create_workflow_state_machine_e2e.py -q
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/ -q
```

Environment-dependent optional browser/MCP tests must use deterministic fakes
or explicit skip markers and must report missing extras separately from product
failures. Any repository-wide pre-existing failures must be recorded rather
than hidden.
