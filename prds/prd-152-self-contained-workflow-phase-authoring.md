---
title: "PRD-152: Agent-Executable create_* Authoring"
status: Implemented
version: 1.3.0
created: 2026-07-28
related_prds:
  - PRD-116  # WorkflowPlugin registry and runner dispatch
  - PRD-138  # Repository Improvement Roadmap
  - PRD-147  # Workflow-Native Extension Authoring
tags:
  - workflows
  - authoring
  - prompts
  - plugins
  - validation
---

# PRD-152 — Agent-Executable `create_*` Authoring

## 1. Summary

Enhance the built-in `create_workflow` workflow so an authoring agent can
generate a complete custom specialized workflow that other runtime agents can
follow and execute. The generated workflow must contain its actual runtime
implementation instructions in each `PhaseSpec.system_prompt_override`.

Apply the same direct-source, phase-guided authoring contract to the sibling
`create_tools` and `create_commands` workflows. Their agents generate complete
loader-compatible Python extension modules directly, with explicit artifact
metadata and their canonical `TOOLS`, `COMMAND`, or `COMMANDS` exports.

This PRD has two deliberately separate workflow layers:

- **Authoring workflow:** the built-in `create_workflow` workflow selected with
  `/workflow create_workflow`. Its agent receives the user's natural-language
  intent and produces, stages, validates, reviews, and publishes a custom
  workflow artifact.
- **Generated specialized workflow:** the published `WorkflowPlugin` that a
  later user request selects with `/workflow <generated-name>`. Its runtime
  agents execute the phase prompts created by the authoring agent.
- **Generated tool or command extension:** the published Python module created
  by `/workflow create_tool` or `/workflow create_command`. The existing tool
  or command loader remains responsible for runtime discovery and execution.

The runner in the generated specialized workflow should only orchestrate
phases. The phase prompts must contain the behavior that makes the workflow
specialized; a generic runner must not be expected to infer that behavior.

The generic `WorkflowRunner` remains the preferred execution path. A generated
workflow does not need a custom `run()` or `resume()` method when its behavior
is expressible as a declarative `PhaseSpec` graph. Custom runners remain
available for genuine orchestration, context transformation, post-processing,
or extensions of a specialized runner. A custom runner may implement its
lifecycle directly; delegation to `super()` is required only when the custom
runner intentionally reuses the parent runner's lifecycle, as in the existing
`code_plan_docs` composite workflow.

The authoring validator must accept a declarative `WorkflowPlugin` that uses
the inherited `WorkflowPlugin.build_runner()` implementation. It must not
force generated code to contain a no-op wrapper runner solely to satisfy
static validation.

### Product boundary

The authoring agent is a code-generating agent, not a planner that hands an
unfinished design to another agent. During the `design` phase of
`create_workflow`, it must generate the complete Python source for the
specialized workflow directly. After publication, a separate runtime agent
executes the generated workflow's phase prompts. The authoring runner owns the
artifact lifecycle; the generated runner owns only phase orchestration.

## 2. Evidence-backed problem statement

The current `CreateWorkflowRunner._generation_prompt` is the authoring-agent
contract, but it describes a design task while imposing an unconditional
custom-runner shape:

- every generated workflow must define a `WorkflowRunner` subclass;
- every generated runner must override `run()` and `resume()`; and
- both methods must delegate to `super()`.

This conflicts with the runtime contract in `WorkflowPlugin.build_runner()`,
whose default implementation already constructs the generic runner for a
declarative `PhaseSpec` graph. It also conflicts with the specialized
`CodePlanRunner`, which owns a separate state machine and does not inherit the
generic `WorkflowRunner` lifecycle.

The current static validator in
`src/agenthicc/workflows/authoring/artifact.py` requires the wrapper even when
the plugin only declares phases. As a result, generated source contains
boilerplate rather than behavior, and the model receives insufficient
guidance about what each runtime phase must actually do.

The problem is not solved by adding more generic runner code or by asking the
authoring agent to produce a runner skeleton. The `design` phase must emit a
complete specialized workflow whose phase prompts include the requested
behavior, tools, inputs, outputs, verification, and handoff conditions. The
later runtime agent must be able to execute those instructions without relying
on undocumented assumptions or a human rewriting the generated source.

## 3. Goals

1. Enhance the built-in `create_workflow` authoring workflow so its design
   agent generates one complete specialized workflow source file directly from
   the user's intent.
2. Require every phase in the generated specialized workflow to contain a
   self-contained,
   literal `system_prompt_override` with actionable runtime instructions.
3. Prefer the inherited generic `WorkflowRunner` for declarative workflows.
4. Permit intentional custom runners without requiring `super()` delegation.
5. Preserve `super()`-based composition for runners that intentionally extend
   an existing runner, including the documented `code_plan_docs` pattern.
6. Relax static validation so the default `WorkflowPlugin.build_runner()` is a
   valid generated-workflow contract.
7. Preserve staged publication, approval, trust, capability, and explicit
   activation boundaries from PRD-147.
8. Prove that a later runtime agent receives and can execute the generated
   phase instructions without executing generated code during authoring or
   silently importing it into the current registry.
9. Give `create_tools` and `create_commands` the same raw-source response
   contract and tailored prompts for their seven authoring phases.

## 4. Non-goals

- Replacing `WorkflowPlugin`, `PhaseSpec`, `WorkflowRunner`, or
  `WorkflowConfig`.
- Creating a second workflow engine or prompt-execution path.
- Automatically converting every existing user workflow to a new format.
- Requiring semantic natural-language analysis to prove that a prompt is good;
  static validation can enforce presence and literal structure, while tests
  and review assess behavior.
- Removing support for specialized runners such as `CodePlanRunner`.
- Requiring custom runners to delegate to `super()` when they do not intend to
  reuse parent lifecycle behavior.
- Executing or importing generated Python before explicit publication and
  discovery activation.

## 5. Product contract

### 5.1 `create_workflow` design output

The `design` phase of the built-in `create_workflow` workflow must generate
exactly one complete `WorkflowPlugin` source artifact directly as raw Python
code. The authoring prompt must not require an XML, JSON, Markdown, or other
special response envelope. The existing parser may continue accepting legacy
envelopes for compatibility, but newly generated workflows use direct source.
The agent must not return pseudocode, a plan in place of source, a partial
class, or instructions for a later agent to finish the implementation.

The generated artifact is the implementation of the user's requested custom
specialized workflow. It must encode the requested behavior in its phase graph
and phase prompts, rather than merely describing what an agent could do.

The generated source must choose one of these execution designs:

1. **Declarative workflow — default**

   Define one `WorkflowPlugin` with a literal `PhaseSpec` graph and rely on the
   inherited `WorkflowPlugin.build_runner()`.

2. **Custom workflow runner — exceptional**

   Define a supported `BaseWorkflowRunner` subclass, implement the required
   lifecycle directly or intentionally extend an existing runner, and wire it
   through `WorkflowPlugin.build_runner()`.

The model must not create a custom runner merely to add boilerplate. It must
not claim that changing `CodePlan.phases` changes the specialized
`CodePlanRunner` state machine.

### 5.2 Generated phase instructions

Every `PhaseSpec` in the generated specialized workflow must contain a
non-empty literal
`system_prompt_override`. The prompt must directly instruct the runtime agent
and must cover, as applicable:

1. the phase's exact objective;
2. the original workflow behavior and required output;
3. the files, APIs, tools, MCP services, or commands to use;
4. the inputs and outputs handed off from prior phases;
5. the exact code or artifacts to create or modify;
6. the success criteria and verification steps;
7. the completion, approval, or review signal to call; and
8. the information that must be handed to the next phase.

The prompt must identify the runtime task supplied by the user and explain
how the phase should combine that task with the workflow's fixed behavior.
Dynamic phase results may be supplied by `WorkflowContext`; the prompt must
explicitly name which prior outputs it consumes rather than relying on an
unexplained convention.

The `create_workflow` design agent must write these instructions into the
generated source; it must not leave them as advice in the authoring response.
Prompts must not use vague instructions such as “continue the implementation,”
“handle the task,” or “do the appropriate work.” They must not assume that a
later phase will infer missing requirements.

### 5.3 Authoring workflow lifecycle

The existing `create_workflow` lifecycle remains the orchestration boundary:

| Authoring phase | Required responsibility | Authoritative output |
|---|---|---|
| `interpret` | Preserve and normalize the user's specialized-workflow intent | Intent record |
| `design` | Generate complete specialized-workflow Python source directly | Raw Python candidate |
| `stage` | Store the candidate outside the discoverable workflow directory | Staged artifact and manifest |
| `validate` | Check source safety, phase prompts, graph, and runner contract | Validation report |
| `review` | Obtain explicit publication approval | Approval decision |
| `publish` | Atomically publish the approved specialized workflow | Published path and hash |
| `summarize` | Explain the generated workflow and activation step | Structured authoring result |

The authoring runner may orchestrate these phases and call the existing
validation/publication services. It must not replace the generated workflow's
runtime phase behavior with hidden authoring-run logic.

The sibling `create_tools` and `create_commands` workflows use the same
interpret → design → stage → validate → review → publish → summarize lifecycle,
but each definition has prompts tailored to its artifact contract. Their
design agents return raw Python directly with literal `ARTIFACT_NAME` and
`ARTIFACT_DESCRIPTION` module metadata so the parser can determine the staged
filename without an envelope. Tool candidates must export `TOOLS`; command
candidates must export exactly one compatible `COMMAND` or `COMMANDS` value.

### 5.4 Phase graph and tools

Generated workflows must continue to use existing `PhaseSpec` fields for:

- phase roles and capability ceilings;
- literal transitions and bounded retries;
- mode overrides;
- command lifecycle and terminal wait policy;
- successful-command and readiness gates; and
- explicit plan, execution, and review completion tools where those flags are
  selected.

The authoring prompt must instruct the model to use the smallest graph that
implements the intent. A one-phase workflow is valid when no handoff or retry
boundary is needed. A multi-phase workflow must state the handoff contract in
each phase prompt.

### 5.5 Runner contract

The generic runner owns phase orchestration, context construction, phase
history, shared memory, transitions, and generic resume behavior.

For a declarative plugin:

- no `run()` or `resume()` override is required;
- no custom `build_runner()` override is required; and
- the inherited `WorkflowPlugin.build_runner()` must be accepted by static
  validation and runtime discovery.

For a custom runner:

- `build_runner()` must construct the selected runner;
- the runner must implement or inherit `run()` and `resume()` through a
  supported `BaseWorkflowRunner` contract;
- direct lifecycle implementation is valid; and
- `super()` delegation is valid only when the runner deliberately composes
  parent behavior.

The validator must not inspect for `super().run()` or `super().resume()` as a
universal requirement.

### 5.6 Configuration

The existing typed `WorkflowParams`, `get_phase_models()`, and `build_params()`
contract remains available when the user requests configurable behavior.
Parameters are valid only when the selected runner consumes them. Provider,
credentials, and `base_url` remain session-wide; generated workflows must not
promise per-phase provider switching.

Configuration templates remain comments or module documentation. The
authoring run publishes only the Python workflow artifact and never writes
secrets or silently edits TOML files.

### 5.7 Sibling tool and command authoring

The design phases of `create_tools` and `create_commands` must generate one
complete raw Python module directly. They must not request or require an XML,
JSON, Markdown, or other special response envelope. Legacy envelopes remain
accepted by the parser for compatibility with already staged or scripted runs.

Every direct-source tool or command candidate must contain literal:

```python
ARTIFACT_NAME = "lowercase_module_name"
ARTIFACT_DESCRIPTION = "short description"
```

`create_tools` must use the existing lauren-ai `@tool` convention and a
literal `TOOLS` list or tuple. `create_commands` must use the canonical
`Command`/`CommandContext` contract and export a literal `COMMAND` or
`COMMANDS` value. Static validators continue to reject unsafe imports, calls,
invalid exports, malformed names, and unsupported loader shapes before
publication.

### 5.8 Generation recovery and bounded retries

A response that contains repository exploration, tool-call activity, analysis,
or an incomplete explanation instead of source is a recoverable generation
failure, not an immediate terminal authoring failure. The authoring runner must:

1. convert the parser or validator result into actionable feedback naming the
   exact failure and the required correction;
2. start another bounded generation attempt with the original intent and the
   correction feedback, explicitly requiring complete source-only output;
3. emit a visible system event that an attempt failed and is being retried; and
4. stop only after `[execution].authoring_max_generation_attempts` complete
   attempts, defaulting to 3 and clamping the effective value to 1–10.

Parse failures must explain that the previous response was not source and may
have contained tool activity or prose. Validation failures must include each
blocking finding code and message. When the limit is exhausted, the structured
result must report the final finding and number of attempts, and no partial
artifact may be staged or published.

### 5.9 Tool-gated phases and installed API inspection

The three authoring workflows share an explicit typed lifecycle state machine:
`interpret → design → stage → validate → review → publish → summarize`. Each
phase has its own `PhaseSpec` prompt and bounded multi-turn budget. The global
`[execution].authoring_max_phase_turns` setting defaults to 20 and is clamped
to 1–100; a phase definition may request a lower budget.

The design agent may use several turns for inspection and implementation, but
the runner accepts the handoff only after the phase-local completion tool is
called. `submit_generated_source` captures one complete raw Python artifact
directly. It never asks the model to construct an XML, JSON, or Markdown
envelope. Existing raw-text parsing remains a compatibility path for older
transports and staged runs.

Every design phase receives read-only
`inspect_agenthicc_documentation(path)` and
`inspect_agenthicc_source(module, symbol)` tools. The first reads packaged
documentation; the second imports only `agenthicc.*` modules and uses Python's
`inspect` API to expose current signatures and source. The build configuration
installs the documentation tree with the package so an installed authoring
agent can inspect the same guidance as a source checkout.

## 6. User journeys

### 6.1 Author an agent-executable specialized workflow

```text
/workflow create_workflow
Create a workflow that uses Cloakbrowser to parse facebook.com and summarize
the page title, visible text, and links.
```

The `create_workflow` authoring agent returns a complete specialized workflow
source with phase prompts that explain
how to locate and use the configured Cloakbrowser tools, what data to collect,
how to validate the result, and what the summary phase must report. The source
is staged, statically validated, shown for approval, and published only after
approval. No no-op runner wrapper is required.

The user is reviewing a generated workflow artifact, not merely approving a
prompt or plan. The approval preview must make the generated phase topology,
phase instructions, capabilities, and activation path visible.

After explicit discovery:

```text
/workflow cloakbrowser_parse_fb
Parse the requested Facebook page and summarize the results.
```

### 6.2 Execute the generated specialized workflow

After explicit discovery, a later runtime agent follows the generated phase
instructions:

```text
/workflow cloakbrowser_parse_fb
Parse the requested Facebook page and summarize the results.
```

The runtime runner supplies context, tools, approvals, transitions, and phase
history. Each phase's `system_prompt_override` tells the agent what to do,
what output to produce, how to verify success, and what to hand off. The
runtime agent must not need to infer the workflow's specialization from the
workflow name or description alone.

### 6.3 Intentional custom runner authoring

When the user explicitly requests behavior that the declarative runner cannot
provide, the authoring agent generates a custom runner and explains the custom
responsibility in its source. A runner that extends `CodePlanRunner` may call
`super().run()` when it wants the complete CodePlan state machine, then use
the public `run_phase()` API for extra work. A runner with an independent
lifecycle may implement `run()` and `resume()` directly.

### 6.4 Validation failure and repair

If a candidate is missing a phase prompt, has an invalid transition, or uses
an invalid runner contract, the authoring workflow returns structured findings
and gives the bounded generation retry a precise correction request. The
repair must preserve the user's intent and return complete source again.

## 7. Architecture

| Concern | Canonical owner |
|---|---|
| `create_*` generation instructions and raw-source parsing | `workflows/authoring/runner.py` |
| Static workflow contract validation | `workflows/authoring/artifact.py` |
| Generated specialized-workflow phase execution | `workflows/default/runner.py` |
| Specialized CodePlan state machine | `workflows/code_plan/runner.py` |
| Workflow construction and discovery | `workflows/registry.py`, `workflows/loader.py` |
| Runtime phase context and outputs | `workflows/plugin.py` |
| Publication approval and staging | `workflows/authoring/runner.py` |
| Authoring lifecycle states and phase tools | `workflows/authoring/state.py`, `phase_tools.py` |
| Installed documentation/API inspection | `workflows/authoring/inspection_tools.py`, `pyproject.toml` |

The implementation must not add a second prompt renderer or runner registry.
`CreateWorkflowRunner._generation_prompt`, `CreateToolRunner._generation_prompt`,
and `CreateCommandRunner._generation_prompt` are the source of truth for the
authoring agents' generation contracts, and each generated
`PhaseSpec.system_prompt_override` is the source of truth for the specialized
workflow's runtime instructions. The generic runner may continue to add the
runtime `WorkflowContext` and user task block, but each generated phase
prompt must explain how that supplied context is used.

### 7.1 Static validation changes

The workflow artifact validator must:

1. accept a plugin with a valid literal phase graph and no custom runner;
2. accept inherited `WorkflowPlugin.build_runner()`;
3. require a custom `build_runner()` to construct a declared custom runner
   when one is present;
4. recognize supported runner bases, including `BaseWorkflowRunner`,
   `WorkflowRunner`, and specialized runner contracts used by the repository;
5. require custom runner lifecycle methods to exist directly or be inherited,
   but not require `super()` calls; and
6. reject generated phases that omit or provide an empty/non-literal
   `system_prompt_override`.

Existing unsafe-import, unsafe-call, source-size, name, phase-reference,
destination, staging, approval, and publication checks remain unchanged.

## 8. Security and resilience

- Generated Python remains untrusted until the existing trust and explicit
  activation flow approves it.
- The authoring model must not write directly to `.agenthicc/workflows/`.
- Static validation must continue to reject unsafe imports and calls.
- Phase prompts must not instruct agents to bypass capability gates, approval,
  workspace, network, or command-lifecycle policy.
- MCP tools may be referenced only when the runtime session exposes them; the
  generated workflow must report missing integrations rather than inventing
  tool names.
- Prompt text and generated source remain bounded by the existing artifact
  limits.
- Documentation inspection is read-only, rejects traversal and absolute paths,
  and source inspection is limited to public `agenthicc.*` modules.
- A malformed or incomplete generated prompt must fail validation before
  publication.

## 9. Implementation plan

### Phase 1 — Prompt contract

- Replace the unconditional wrapper-runner instructions in
  `CreateWorkflowRunner._generation_prompt` and the envelope-only prompts in
  `CreateToolRunner` and `CreateCommandRunner`.
- Instruct the authoring agent to generate complete source directly.
- Add the eight-point self-contained phase-prompt checklist.
- Explain declarative versus custom runner selection and the conditional
  `super()` rule.
- Preserve existing TOML, provider, safety, parser-compatibility, and activation guidance.
- Give each sibling authoring definition tailored prompts for interpret, design,
  stage, validate, review, publish, and summarize.

### Phase 2 — Declarative validator support

- Make the custom-runner check conditional on an explicit custom runner.
- Accept inherited `WorkflowPlugin.build_runner()`.
- Allow direct custom lifecycle implementations without `super()` checks.
- Add static validation for non-empty literal phase prompts.
- Preserve validation for custom factory wiring and supported runner bases.

### Phase 2A — Generation recovery

- Feed parse and validation findings back into the next source-generation prompt.
- Emit visible retry notices instead of silently discarding malformed responses.
- Add `[execution].authoring_max_generation_attempts` with a safe default and
  upper bound, and report the exhausted attempt count in the final result.

### Phase 3 — Regression coverage

- Add unit coverage for a minimal declarative workflow with no runner class or
  factory.
- Add unit coverage for a direct custom runner that does not call `super()`.
- Retain coverage for a composing runner that does call `super()`.
- Add rejection coverage for missing/empty/non-literal phase prompts,
  unresolved transitions, invalid custom factories, and unsafe source.
- Add integration coverage for staging, approval, publication, reload, and
  default-runner construction.
- Add E2E coverage proving a generated multi-phase workflow's phase prompts
  reach the runtime agent and preserve the intended handoff information.
- Add raw-source E2E coverage proving `create_tools` and `create_commands`
  recover artifact metadata, publish loader-compatible modules, and report
  their distinct reload actions.

### Phase 4 — Documentation and migration

- Update `docs/guides/workflows.md` and
  `docs/guides/custom-workflows-and-config.md`.
- Update `docs/guides/tools.md` and `docs/guides/commands.md` with the sibling
  authoring journeys.
- Update the generated workflow authoring guidance in `skills/bootstrap.py`.
- Amend PRD-147 acceptance criterion 16 and its implementation notes to use
  the conditional runner contract.
- Add this PRD to `prds/README.md`.
- Keep existing custom workflows compatible; only newly authored artifacts
  receive the stricter self-contained prompt requirement.

## 10. Acceptance criteria

1. `/workflow create_workflow` selects the built-in authoring workflow and
   passes the next ordinary input as the exact specialized-workflow intent.
2. The `create_workflow` design phase tells its agent to generate one complete
   specialized-workflow source artifact directly.
3. The prompt explicitly distinguishes declarative workflows from custom
   runners.
4. The prompt says that `super()` delegation is conditional, not universal.
5. The prompt requires every generated specialized-workflow phase to include a self-contained
   `system_prompt_override` covering the eight required instruction areas.
6. The prompt forbids vague phase instructions and reliance on later phases to
   infer missing behavior.
7. A valid declarative specialized workflow containing only `WorkflowPlugin` and literal
   `PhaseSpec` declarations passes authoring validation without importing
   `WorkflowRunner` or overriding `build_runner()`.
8. The inherited default `WorkflowPlugin.build_runner()` constructs the
   declarative workflow's generic runner after discovery.
9. A custom runner that implements its lifecycle directly without `super()`
   passes validation when its factory and runner contract are valid.
10. A custom runner that intentionally delegates to a parent runner remains
   valid and the existing `code_plan_docs` example continues to work.
11. A generated phase missing an explicit non-empty literal
   `system_prompt_override` fails validation before publication.
12. The published specialized workflow can be selected in a later request and
    its runtime agent receives the generated phase instructions and handoff
    requirements.
13. Existing unsafe-source, invalid-name, invalid-transition, staging,
   approval, overwrite, resume, and explicit-activation behavior remains
   intact.
14. Unit, integration, and E2E tests cover declarative generation, direct
   custom runners, composing custom runners, prompt completeness, runtime
   prompt delivery, publication, and reload/discovery.
15. Documentation accurately describes the `create_workflow` authoring path,
    generated specialized-workflow execution, the default runner path, custom runner
   exceptions, conditional `super()` delegation, and self-contained phase
   prompts.
16. `/workflow create_tool` tells its design agent to return raw Python source,
    requires literal artifact metadata and a loader-compatible `TOOLS` export,
    and does not require a special response envelope.
17. `/workflow create_command` tells its design agent to return raw Python
    source, requires literal artifact metadata and a loader-compatible
    `COMMAND` or `COMMANDS` export, and does not require a special response
    envelope.
18. `CreateWorkflow`, `CreateTools`, and `CreateCommands` each define explicit,
    artifact-appropriate prompts for all seven authoring phases.
19. Unit and E2E tests prove raw tool and command generation, metadata recovery,
    publication, loader discovery, approval, retry, resume, and reload guidance.
20. A non-source or invalid-source response receives actionable feedback and a
    visible retry event, then succeeds when a later attempt returns valid
    source; repeated failures stop at the configured bounded attempt limit and
   report that limit without publication.
21. All three authoring workflows expose explicit typed lifecycle states and
    phase-specific prompts with bounded multi-turn budgets.
22. A design agent can submit complete raw source with the phase-local tool and
    the runner validates it before staging; no envelope is required.
23. Design agents can inspect packaged documentation and current Python API
    signatures/source through bounded read-only built-in tools.
24. The built distribution contains the documentation tree alongside the
    installable agenthicc source package.

## 11. Verification

Focused checks:

```bash
uv run pytest tests/unit/test_workflow_authoring.py -q
uv run pytest tests/integration/test_workflow_runner_integration.py -q
uv run pytest tests/e2e/test_create_workflow_e2e.py -q
uv run pytest tests/e2e/test_extension_authoring_e2e.py -q
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
```

The complete gate remains:

```bash
uv run pytest tests/ -q
```

The smoke path must prove the two-agent-layer journey:

```text
/workflow create_workflow
  → ordinary user intent
  → create_workflow design agent generates complete specialized source
  → generated phase prompts contain executable instructions
  → static validation
  → approval
  → publication
  → explicit discovery
  → /workflow <generated-name>
  → runtime agent executes each generated phase
  → specialized result
```

## 12. Rollout and migration

1. Ship the prompt and validator changes behind the existing authoring
   workflow; do not change the ordinary user workflow loader contract.
2. Existing published workflows remain loadable, including minimal workflows
   whose phases rely on the generic runner's default role prompt.
3. Newly generated workflows must satisfy the explicit phase-prompt contract.
4. If a generated candidate from an older session is resumed, validate it
   against the contract in effect when it is resumed and report actionable
   findings rather than silently rewriting it.
5. Do not auto-reload or auto-execute newly published artifacts.
6. Existing tool and command loaders remain unchanged; after approval, users
   explicitly run `/tools reload` or `/commands reload` to discover a generated
   extension.

## 13. Related implementation notes

The working `code_plan_docs` example remains the reference for intentional
composite behavior: it extends `CodePlanRunner`, calls `super().run()`, and
uses `run_phase()` for the additional documentation phase. That example is a
specific composition pattern, not a universal requirement for generated
declarative workflows.

## 14. Implementation evidence

Implemented in the current authoring runner, artifact validator, built-in
`create_*` definitions, generated extension guidance, and regression fixtures.
The direct-source paths are covered by the authoring unit tests,
`tests/e2e/test_create_workflow_e2e.py`, and
`tests/e2e/test_extension_authoring_e2e.py`, including default runner
discovery, tool/command loader discovery, and delivery of a generated workflow
phase prompt to a later runtime agent.

Verification completed:

```text
2437 passed, 15 skipped — uv run pytest tests/ -q
ruff check, ruff format --check, mypy, type_audit, and nox llms_check — passed
uv build --wheel --out-dir /tmp/agenthicc-build — passed; wheel contains
share/agenthicc/docs alongside the installable source package
Installed-wheel smoke test — passed; the documentation inspection tool reads
share/agenthicc/docs/guides/workflows.md without the repository checkout.
```
