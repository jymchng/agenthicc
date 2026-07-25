---
title: "PRD-147: Workflow-Native Extension Authoring"
status: In progress
version: 0.1.0
created: 2026-07-25
related_prds:
  - PRD-138  # Repository improvement roadmap
  - PRD-114  # Composite workflows
  - PRD-116  # WorkflowPlugin registry artifact
  - PRD-142  # Dollar-prefixed skill triggers
  - PRD-22   # Skills metadata and discovery
  - PRD-23   # Skills runtime
tags:
  - workflows
  - skills
  - authoring
  - plugins
  - tools
  - commands
---

# PRD-147 — Workflow-Native Extension Authoring

## 1. Summary

Convert the default authoring skills `$create-workflow`, `$create-tools`, and
`$create-commands` from prompt-only instructions into first-class, built-in
authoring workflows. The canonical interactive entry point for creating a
workflow is `/workflow create_workflow` followed by the user's intent in
the input panel.

The existing `/workflow NAME` command remains the workflow selector: it sets
the workflow for the next ordinary user request. The workflow then produces a
staged, validated artifact and a structured result describing what was
generated, what was checked, whether approval is required, and how the
extension can be activated. The dollar-prefixed triggers may remain as
one-shot convenience adapters, but they are not a second implementation path.

The primary result of `create_workflow` is a project-local
`.agenthicc/workflows/<name>.py` implementing an `agenthicc`
`WorkflowPlugin` according to the user's instructions. The sibling authoring
workflows produce the corresponding tool or command plugin while sharing the
same safety and lifecycle contract.

Implementation status: Phases 1 and 2 are implemented in
`src/agenthicc/workflows/authoring/`. `create_workflow` remains the canonical
workflow authoring name; `create_tools`/`create_commands` are the canonical
tool and command names, with singular `create_tool`/`create_command` selector
aliases. The shared runner and focused E2E coverage exercise both loader
contracts. Centralized trust hardening and durable staged-run retention remain
follow-up work, so this PRD stays `In progress`.

## 2. Evidence-backed problem statement

The current implementation has two separate concepts that look similar to a
user:

| Surface | Current implementation | Limitation |
|---|---|---|
| `$create-workflow` | A bootstrapped `SKILL.md` body in `skills/bootstrap.py` | Injects instructions into an agent turn; it does not create, validate, or register a workflow artifact |
| `$create-tools` | A bootstrapped skill body | Produces guidance for tool authoring, but the result is ordinary model output and the generated Python remains a manual step |
| `$create-commands` | A bootstrapped skill body | Same prompt-only behavior for command plugins |
| Workflow discovery | `WorkflowPlugin` subclasses are imported from `.agenthicc/workflows/` at session construction | Workflows are static Python plugins; a generated class cannot safely become active in the same registry automatically |
| Workflow execution | `WorkflowPlugin.build_runner()` and `WorkflowRunner` already provide phase transitions, output records, approvals, tools, and headless execution | There is no authoring-specific artifact, staging, validation, or publication result |
| Skill execution | `process_skill_body()` and the pending-skill mechanism prepare text for the next agent turn | There is no typed handoff from a skill trigger to a workflow run |

This means the desired behavior is feasible, but simply copying the skill
body into a `PhaseSpec` would not solve the product problem. The feature needs
an authoring workflow contract and a controlled transition from generated
candidate to executable project extension.

## 3. Feasibility assessment

**Overall feasibility: high for orchestration; medium for safe publication and
activation.**

The existing workflow architecture supplies the important foundations:

- `WorkflowPlugin` classes are the canonical registry artifact.
- `PhaseSpec` already models bounded agent phases, transitions, retries,
  capability ceilings, and approval-oriented phases.
- `WorkflowConfig` supplies the session's tools, memory, approval service,
  agent registry, and persistence dependencies.
- `WorkflowContext` and `WorkflowRun` already retain phase outputs and status.
- `agenthicc workflows run` provides a headless entry point.
- Existing plugin loaders define the destination contracts for workflows,
  tools, and commands.

The work that cannot be obtained by composition alone is:

1. a typed authoring result and durable staged-artifact state;
2. bounded writing to the correct project extension directory;
3. syntax, contract, and discovery validation before publication;
4. explicit approval and overwrite behavior;
5. an activation/reload story that does not import newly generated Python
   into the current process silently;
6. consistent trust behavior for executable generated extensions, including
   the current workflow-loader trust gap recorded by PRD-138.

The recommended design therefore uses static built-in authoring workflows and
generates ordinary user-owned extension files. It does not generate a new
`WorkflowPlugin` class in memory or create a second workflow registry.

## 4. Goals

1. Make `/workflow create_workflow` select a real authoring workflow whose
   successful result is a validated `WorkflowPlugin` artifact.
2. Convert `$create-tools` and `$create-commands` to the same workflow-native
   authoring lifecycle without duplicating orchestration code.
3. Preserve `/workflow NAME` as the canonical workflow-selection UX and add
   direct workflow names for automation and headless use:

   ```text
   agenthicc workflows run create_workflow --intent "..."
   agenthicc workflows run create_tools --intent "..."
   agenthicc workflows run create_commands --intent "..."
   ```

4. Produce deterministic, machine-readable results containing artifact paths,
   validation findings, approval state, and activation instructions.
5. Require review before executable Python is published or replaces an
   existing artifact.
6. Keep generated code inside the existing workflow, tool, command, workspace,
   capability, approval, and trust boundaries.
7. Make interrupted authoring runs resumable without regenerating or
   duplicating already staged artifacts.

## 5. Non-goals

- Replacing `WorkflowPlugin`, `PhaseSpec`, `WorkflowConfig`, or the existing
  workflow registry.
- Allowing a generated workflow to execute in the same process before an
  explicit publication and activation step.
- Automatically installing dependencies, contacting a marketplace, or
  publishing user code remotely.
- Automatically overwriting an existing workflow, tool, command, test, or
  documentation file.
- Treating generated Python as trusted merely because an LLM produced it.
- Replacing human review, tests, capability checks, or approval prompts with a
  generated completion message.
- Supporting arbitrary extension types beyond workflows, tools, and commands
  in the first release.

## 6. User-facing contract

### 6.1 Canonical workflow-authoring journey

The primary user journey is:

1. The user selects the built-in authoring workflow:

   ```text
   /workflow create_workflow
   ```

2. The input panel accepts the next ordinary user message as the workflow
   intent, for example:

   ```text
   Create a workflow that uses Cloakbrowser to parse facebook.com.
   ```

3. Agenthicc runs `create_workflow` using that intent. Its phases inspect
   the repository, design the workflow, stage a candidate, validate the
   generated `WorkflowPlugin`, present the result for approval, and publish it
   to `.agenthicc/workflows/` only after approval.

4. The result identifies the generated workflow name, for example
   `cloakbrowser_parse_fb`, the artifact path, validation status, and the
   activation instruction.

5. After the session performs the required discovery cycle, the user selects
   and runs the generated workflow in a later turn:

   ```text
   /workflow cloakbrowser_parse_fb
   Parse the requested Facebook page and summarize the results.
   ```

6. `/workflow reset` returns subsequent turns to the active mode's default
   workflow. Completing a workflow also follows the existing TUI behavior of
   returning to Auto mode when appropriate.

`/workflow create_workflow` is a selector, not the intent itself. It must
not consume the user's intent or require the user to place instructions on the
same line.

### 6.2 Authoring workflow names and outputs

| Trigger | Workflow name | Primary output |
|---|---|---|
| `/workflow create_workflow` + next input | `create_workflow` | `.agenthicc/workflows/<name>.py` containing a `WorkflowPlugin` |
| `/workflow create_tools` + next input | `create_tools` | `.agenthicc/tools/<module>.py` exporting the supported `TOOLS` contract, plus tests when requested or required by the plan |
| `/workflow create_commands` + next input | `create_commands` | `.agenthicc/commands/<module>.py` exporting `COMMAND` or `COMMANDS`, plus tests when requested or required by the plan |

`$create-workflow INSTRUCTIONS` may be retained as a one-shot convenience
adapter that selects `create_workflow` and submits `INSTRUCTIONS` as the
intent. It must use the same runner and result contract as the two-step
`/workflow create_workflow` journey. It must not prepend a long `SKILL.md`
instruction body to a generic agent turn.

The `$create-tools` and `$create-commands` adapters follow the same pattern
for `create_tools` and `create_commands`; `/workflow create_tools` and
`/workflow create_commands` are their canonical two-step forms.

### 6.2 Authoring result

Every authoring workflow returns a JSON-safe result with at least:

```json
{
  "workflow": "create_workflow",
  "status": "published",
  "run_id": "...",
  "artifact_kind": "workflow",
  "artifacts": [
    {
      "path": ".agenthicc/workflows/research.py",
      "state": "published",
      "validation": "passed"
    }
  ],
  "approval": "approved",
  "activation": "restart-session"
}
```

Possible status values are `staged`, `awaiting_approval`, `published`,
`rejected`, `cancelled`, and `failed`. A failed or rejected run must retain a
bounded explanation and must not claim that an artifact was published.

### 6.3 Activation

Publication and activation are separate:

- workflow artifacts require a new registry discovery cycle, initially a
  session restart;
- command artifacts use the existing `/commands reload` path after approval;
- tool artifacts require the existing plugin discovery lifecycle, initially a
  session restart unless a safe tool reload API is added;
- no generated Python is imported automatically in the authoring run.

The result must state the exact next action rather than implying that the
extension is already active.

## 7. Workflow design

Implement one shared authoring runner and three small built-in plugin
definitions under a canonical `workflows/authoring/` package. Each definition
selects the artifact kind, contract checks, destination, and domain-specific
prompt fragments. The shared runner owns lifecycle and result handling.

The default phase graph is:

```text
interpret → design → stage → validate → review → publish → summarize
                ↑                  └── reject ──┘
```

### Interpret

- Parse the user's instructions into a typed authoring request.
- Identify the requested name, behavior, inputs, outputs, capabilities, and
  likely files.
- Ask a bounded clarification question when a safe artifact name or contract
  cannot be inferred.

### Design

- Inspect the current repository and the relevant canonical modules/tests.
- Select the correct existing export convention.
- Produce a structured plan, including files, dependencies, tests, capability
  metadata, and activation steps.
- Use the existing approval service for a plan review when the target is
  executable Python or an overwrite is contemplated.

### Stage

- Write candidates under a run-scoped staging directory such as
  `.agenthicc/authoring/<run-id>/`, never directly into a discoverable
  extension directory.
- Keep a manifest containing requested intent, artifact kind, content hashes,
  destination, and generated-file limits.
- Use `WorkspaceView` and existing file/tool capability boundaries for all
  filesystem work.

### Validate

- Check names and destinations for traversal, symlink escape, collisions, and
  unsupported paths.
- Run syntax/AST checks and contract validation without activating the staged
  module.
- Validate workflow phase references, transitions, output contracts, and
  `WorkflowPlugin` shape.
- Validate `TOOLS`, `COMMAND`, or `COMMANDS` exports for tool and command
  artifacts.
- Run bounded focused tests or generate a test plan when tests cannot safely
  run before publication.
- Return structured findings and route failures back to `stage` or `design`
  within a bounded retry budget.

### Review and publish

- Show the user the destination, diff/preview, validation results, requested
  capabilities, and any dependency or trust implications.
- Require explicit approval before copying staged files into
  `.agenthicc/workflows/`, `.agenthicc/tools/`, or `.agenthicc/commands/`.
- Refuse publication when validation has blocking findings.
- Refuse overwrite unless the user explicitly approves the replacement.
- Record the final artifact hashes and activation instruction in the durable
  result.

## 8. Technical design

### 8.1 Skill-to-workflow adapter

Extend the validated skill metadata with an optional workflow target, for
example `workflow: create_workflow`. The default authoring skill records use
that metadata. The skill command handler delegates to a session-owned
`start_workflow(intent, workflow_name)` callback so session orchestration stays
in `TUISession`; it does not instantiate runners inside the skills package.

This preserves the existing skill discovery and `$` picker while making the
execution boundary explicit. A normal user-defined skill without a workflow
target keeps its current pending-body behavior.

Existing user-authored copies of the old default skill files must not be
silently overwritten. The migration should reserve the three built-in
authoring names, report the detected legacy definition, and offer an explicit
conversion/reinstall path.

### 8.2 Artifact and persistence model

Add an authoring result/context type that composes with `WorkflowContext` and
contains:

- request and artifact kind;
- staging directory and manifest path;
- candidate and published artifact metadata;
- validation findings and test outcomes;
- approval/rejection state;
- retry count and activation instruction.

Use the existing workflow/session persistence mechanisms for resume. Staged
files are content-addressed or hash-checked so resume never blindly repeats a
side effect or publishes a changed candidate without revalidation.

### 8.3 Trust and capability boundary

The generated agent phases receive only the capabilities needed for the
selected artifact kind. They must not receive unrestricted shell, network, or
dependency-install privileges by default. Publication is an approval-gated
filesystem side effect.

Generated Python remains untrusted until the repository's centralized plugin
trust contract is applied. PRD-138 P0.4 is a prerequisite for claiming a
complete trust story; until that contract is wired into workflow discovery,
the authoring workflow must not auto-reload or auto-import generated files.

Headless execution follows the existing fail-closed approval behavior. A
headless run without explicit permission may stage and validate but cannot
publish executable artifacts.

## 9. Acceptance criteria

1. `workflows list --json` reports `create_workflow`, `create_tools`, and
   `create_commands` as built-in authoring workflows with their phase topology.
2. `/workflow create_workflow` selects the authoring workflow without
   consuming the next input-panel message.
3. A successful mocked/integration `create_workflow` run receives the next
   ordinary user message as its exact intent and produces a staged
   candidate that contains a valid `WorkflowPlugin`, phase graph, and requested
   behavior, then publishes it only after approval.
4. `$create-workflow instructions`, if retained, delegates to the same
   `create_workflow` runner and result contract.
5. The result includes artifact paths, validation findings, approval state,
   hashes, run status, and the correct activation instruction.
6. A generated workflow such as `cloakbrowser_parse_fb` is discovered by the
   existing workflow loader on the next explicit discovery cycle and is not
   imported during publication.
7. `create_tools` validates the `TOOLS` export and `create_commands` validates
   `COMMAND`/`COMMANDS` through their existing loaders.
8. Invalid names, traversal, symlink escapes, unsupported destinations,
   malformed Python, invalid exports, unresolved workflow transitions, and
   blocking test failures prevent publication with actionable results.
9. Existing artifacts are never overwritten without an explicit approval, and
   a rejected, cancelled, or failed run leaves no discoverable partial file.
10. Resume reuses the staged candidate and manifest, revalidates changed
   content, and does not duplicate generated side effects.
11. Headless authoring fails closed for publication unless the documented
    explicit permission flag is supplied.
12. Legacy ordinary skills continue to use the existing skill-body execution
    path, and existing user-authored `create-*` skills are not overwritten by
    bootstrap.
13. Focused unit, integration, and headless tests cover success, malformed
    output, approval denial, overwrite denial, resume, activation reporting,
    and loader contract validation.
14. README, workflow, tool, command, security, and CLI documentation describe
    the new workflow names, `$` adapters, artifact lifecycle, and activation
    boundaries.
15. Every authoring terminal path emits a visible final summary after approval,
    rejection, failure, cancellation, or resume; the structured result carries
    the same summary.
16. Generated workflows define a custom `WorkflowRunner` with explicit
    `run()`/`resume()` delegation and a `WorkflowPlugin.build_runner()` factory,
    preserving context and state transitions.
17. `/tools` and `/workflows` open read-only registry overlays with selectable
    detail views, matching `/commands` and `/skills`.

## 10. Rollout plan

### Phase 0 — Contract and prototype

- Add the typed authoring request/result and staging manifest model.
- Build validation helpers for workflow, tool, and command contracts.
- Add fixture-driven tests with a deterministic fake agent runner.

### Phase 1 — `create_workflow` (implemented)

- The shared authoring runner and `create_workflow` definition are implemented.
- `/workflow create_workflow`, the input-panel intent handoff, and the
  `workflows run` path use the authoring runner.
- Publication is approval-gated and discovery is verified after restart.

### Phase 2 — sibling authoring workflows (implemented)

- `create_tools` and `create_commands` definitions use the shared runner, with
  singular selector aliases.
- Their existing `TOOLS`, `COMMAND`, and `COMMANDS` loader contracts are
  statically validated and their reload/restart requirements are reported.
- All authoring results emit a terminal transcript summary, and generated
  workflow candidates are required to preserve runner context through a custom
  `WorkflowRunner`.
- `/tools` and `/workflows` expose the effective session registries through the
  existing overlay interaction pattern.

### Phase 3 — activation and trust hardening

- Integrate generated-artifact trust decisions with PRD-138's centralized
  plugin policy.
- Add safe tool/workflow reload only if it can preserve registry ownership and
  avoid importing untrusted code implicitly.
- Add durable cleanup/retention controls for staged authoring runs.

## 11. Verification

The implementation must add focused coverage under `tests/unit/` and
`tests/integration/`, then run the checks required by `AGENTS.md`:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/ -q
```

The authoring smoke test must prove the complete path: trigger or CLI command
→ workflow phases → staged artifact → validation → approval → publication →
explicit loader discovery → structured result.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Generated Python executes unexpectedly | Stage outside discoverable directories; require approval; never auto-import or auto-reload |
| The authoring workflow becomes a second plugin system | Use existing `WorkflowPlugin`, `WorkflowConfig`, loaders, capability metadata, and approval service |
| Model output claims success without an artifact | Make the manifest and filesystem/validator result authoritative; publish only from validated staged content |
| Repeated turns duplicate writes | Hash staged files and persist the run manifest; publish idempotently |
| Existing custom skills are overwritten | Preserve user-authored directories and use explicit migration for reserved default names |
| Workflow discovery trust remains incomplete | Track PRD-138 P0.4 as a prerequisite and keep activation explicit until it is implemented |
| Headless users expect publication by default | Return `awaiting_approval`/`staged` and document the explicit permission requirement |
