---
title: "PRD-259: Deterministic Validation for make_agenthicc_tool"
status: Proposed
version: 0.1.0
created: 2026-11-01
scope: "make_agenthicc_tool planning, generated-plugin validation, and validation checkpoint invariants"
related_prds:
  - PRD-24 # Tool plugin discovery
  - PRD-25 # Tool plugin registration
  - PRD-27 # Tool plugin security
  - PRD-76 # Tool capability gate
  - PRD-147 # Workflow-native extension authoring
  - PRD-152 # Agent-executable create_* authoring
  - PRD-153 # Reliable agent-owned workflow authoring
  - PRD-250 # Code quality and extension trust hardening

tags:
  - workflows
  - tool-plugins
  - validation
  - trust
  - capabilities
---

# PRD-259 — Deterministic Validation for `make_agenthicc_tool`

## 1. Summary

Make the `make_agenthicc_tool` runner, rather than its validation-phase agent,
authoritative for whether the generated plugin satisfies the tool-plugin
structure it planned. Validate the plan and generated source deterministically,
without importing or executing the generated Python. The agent may explain
findings and request repairs, but an agent-supplied `approve_tool(summary)` call
must never turn an absent or failing validation report into a pass.

This PRD owns the plan-to-source validation contract and checkpoint invariants.
It does not define where code is written, how overwrite approval works, or how
phase permissions are scoped; those belong to PRD-260. It does not claim that
static analysis proves a tool's semantic behavior or capability classification.

## 2. Evidence-backed current state

`src/agenthicc/workflows/make_agenthicc_tool/runner.py` currently has three
agent-reported handoffs:

- `_make_analyze_tools()` validates a non-empty identifier and known capability
  strings, but silently drops malformed parameters, accepts duplicate names,
  accepts arbitrary type-hint/default source text, and coerces values such as
  `requires_confirmation` with `bool(...)`.
- `_make_generate_tools()` accepts any string containing `.agenthicc/tools/`;
  it does not prove that the reported file is the planned tool's canonical
  module.
- `_validate()` asks the agent to run syntax/import/schema/capability checks.
  The only runner-side success condition is that `approve_tool()` was called
  with a nonblank summary. No deterministic validator is invoked.

The validation prompt asks the agent to import the generated module. Importing a
Python module executes its top-level code. This is not an acceptable validation
primitive for a newly generated, not-yet-trusted extension.

The canonical loader in `src/agenthicc/plugins/discovery.py` checks digest-bound
trust before import, then checks that `TOOLS` contains callables. It cannot be
used as an in-process validation shortcut: doing so would execute the module
when trusted, and an untrusted module is correctly skipped. The existing trust
service remains the activation boundary; this PRD must not auto-trust generated
code.

Current coverage in `tests/unit/test_make_agenthicc_tool.py` and the make-tool
section of `tests/unit/test_website_workflow_coverage.py` exercises handoff
factories and one agent-reported rejection/repair. It does not demonstrate a
runner-computed validation result or a non-executing source-validation boundary.

## 3. Problem

A model can call `approve_tool("all checks passed")` even if the file is absent,
malformed, exports the wrong callable, omits planned capability metadata, or
contains a module-level side effect. Consequently, the runner can transition to
`FINALIZE` and tell the user that a plugin was validated based solely on
unverified model prose. The current capability prompt also incorrectly says an
untagged tool "passes gates"; the runtime classifies missing capability metadata
as `UNDECLARED`, which requires approval in Safe and is blocked in Plan.

The workflow needs a deterministic structural gate, a precise plan schema, and
state restoration that cannot manufacture an approval by defaulting missing
fields.

## 4. Goals

1. Validate every analyze handoff into a bounded, typed plan; reject malformed
   values rather than silently dropping or coercing them.
2. Validate generated source with a deterministic, non-executing checker before
   accepting the validation phase.
3. Compare the source's declared tool shape, name, parameters, capabilities,
   confirmation setting, and dependencies to the accepted plan.
4. Treat static validation as structural evidence only; preserve human review
   and digest-bound trust as separate gates for executable Python.
5. Make a runner-generated validation report, not an agent-provided summary, a
   necessary gate for leaving validation; it does not authorize review approval,
   publication, or trust (PRD-260).
6. Reject inconsistent restored contexts instead of presenting missing
   validation as an implicit approval.

## 5. Non-goals

- Proving that arbitrary Python has no side effects or that a capability label
  matches the function's full semantics; static analysis cannot establish
  either property in general.
- Importing, executing, sandbox-running, or automatically trusting generated
  source as part of validation.
- Installing dependencies or invoking package managers.
- Defining staging, publication, overwrite behavior, or the generation phase's
  effective permission ceiling (PRD-260).
- Replacing `discover_project_tools()`, `ExtensionTrust`, the capability gate,
  or the canonical plugin loader.
- Adding legacy permissive plan parsing or accepting an agent-reported pass when
  deterministic validation is missing.

## 6. Product contract

### 6.1 Accepted tool plan

The analyze transition must parse a complete plan before changing state. It
must reject the entire plan with a field-specific error when any field is
malformed; it must not salvage a partial plan by skipping parameters.

The plan contract must require:

- a canonical lowercase `snake_case` Python identifier for the tool name;
- a non-empty, bounded description;
- at most five parameters, with unique valid parameter names;
- type hints drawn from a documented, safe grammar of supported built-in types
  and parameterized containers; arbitrary Python expressions are not allowed;
- defaults represented as bounded Python literals, parsed with
  `ast.literal_eval`, or the empty string for a required parameter;
- bounded parameter descriptions;
- a duplicate-free subset of known tool capabilities;
- an exact boolean for `requires_confirmation` and `class_form`;
- bounded, syntactically valid PEP-508 dependency strings, with no pip command
  options or direct-install instructions.

The accepted normalized plan is stored in typed workflow context. Values used
in source generation or prompts are bounded and normalized at this boundary.
The workflow must not imply that a model-selected capability list is
independently proof of the generated function's behavior.

### 6.2 Deterministic source validation

Add a runner-owned validator that accepts the expected candidate path from the
staging identity and the derived publication target, the normalized plan, and the
source bytes read through the authorized workspace boundary. It returns a
structured report with an overall `pass`/`fail`, stable check identifiers,
bounded diagnostics, and the source digest. It must not import the target, call
`exec`, spawn a Python interpreter, install dependencies, or follow a symlink to
source outside the intended artifact root.

At minimum, strict validation must check:

1. The candidate path is exactly the run's canonical staged artifact path; the
   publication target is exactly `.agenthicc/tools/<planned_tool_name>.py`.
   Both identities must have a `.py` suffix and the planned name must not be
   underscore-prefixed.
2. The source decodes as UTF-8 and parses with `ast.parse`.
3. The module declares one literal `TOOLS` list/tuple containing the planned
   callable or planned class-form tool, with no dynamic export expression.
4. The exported object is decorated with the supported `@tool(...)` form and
   its explicit tool name matches the plan.
5. The function signature, or class `run()` signature, matches the planned
   parameter names and supported annotations; any injected `ToolContext` is
   recognized only in the documented hidden-parameter form.
6. Capability decorators resolve to known `agenthicc.tools.capabilities`
   decorators and match the planned capability set. No decorator is treated as
   evidence that the function's actual behavior is safe.
7. The declared confirmation setting matches the plan.
8. `DEPENDENCIES` is a literal list/tuple of requirement strings matching the
   plan, or the documented sidecar form matches it when selected. The validator
   never installs a missing dependency.
9. No unsupported dynamic export or alternate tool declaration can satisfy the
   contract by merely containing the expected text in a comment or string.

The validator may report suspicious top-level operations for human review, but
must label those findings as heuristics. It must not claim to prove the absence
of import-time side effects. The final user review and `agenthicc trust add`
remain necessary before runtime import.

### 6.3 Validation-phase authority

The runner runs deterministic validation before exposing a successful
validation transition. The validation agent may inspect the report, explain a
failure, and call a rejection transition to request repair. It may not override
a failed check or create a pass by supplying a summary.

`approve_tool(summary)` may advance only when the report belongs to the current
artifact digest and every required check passes. Its summary is explanatory
text, not validation evidence. A rejection must be tied to the current report
and must not erase its diagnostics. Any source rewrite invalidates the prior
report and requires a fresh validation run.

The final summary must distinguish these facts:

- deterministic structural checks passed or failed;
- semantic behavior has not been proven by static checks; and
- the source remains untrusted until the user reviews and explicitly trusts its
  exact content.

### 6.4 Checkpoint and resume invariants

`checkpoint_context_from_payload()` must validate the state-specific fields
rather than coercing malformed values into defaults. In particular:

- `VALIDATE` requires a valid accepted plan and a canonical generated artifact
  identity;
- `FINALIZE` requires a passing validation report whose digest matches the
  current artifact receipt; when PRD-260's publication lifecycle is used, it
  additionally requires a recorded publication outcome and the corresponding
  publication invariants;
- `COMPLETE` requires final result metadata; and
- a missing validation report is never represented to the user as
  `"(approved)"`.

An invalid checkpoint raises an actionable `ValueError` before provider or tool
execution. Do not add a compatibility path that treats absent validation fields
as success.

## 7. Functional requirements

### FR-1 — Strict analyze transition

Replace lossy/coercive transition parsing with one typed parser. Invalid values
must leave the phase event unset and return a bounded repair message. A valid
call records exactly one normalized plan and signals once.

### FR-2 — Non-executing validator

Provide unit-testable static validation for the supported function and class
forms. Keep its accepted syntax intentionally narrow and versioned alongside the
authoring prompt. Do not import generated files as part of checking their
contract.

### FR-3 — Report-bound transitions

The runner must bind the validation report to the planned target and SHA-256 of
the exact source inspected. Editing or replacing the candidate makes the prior
approval unusable. Approval transitions require a current passing report;
agent prose alone is insufficient.

### FR-4 — Runtime and trust boundary

Keep discovery behind `current_extension_trust().permits(...)`. Neither
successful static validation nor completion of this workflow adds a trust
record. Update the final prompt to state that static validation is not execution
or semantic review and that project tools require a separate explicit trust
decision.

### FR-5 — Checkpoint schema

Persist the normalized plan, report status, report digest, and artifact identity
using JSON-safe values. Validate cross-field invariants on restore. Do not
silently coerce a wrong type to a success-shaped value.

### FR-6 — Documentation

Update the make-tool workflow reference, tool-authoring guidance, and
`llms-full.txt` if any new supported public validator symbol is exported. Explain
the distinction between static contract checks, human code review, trust, and
runtime behavior.

## 8. Data flow

```text
agent submits plan
  -> strict typed plan parser accepts or rejects the complete plan
  -> candidate is written through the separately authorized staging flow
  -> runner reads exact candidate bytes without importing them
  -> static validator returns checks + source digest
       -> failure: agent receives diagnostics and may request a bounded repair
  -> pass: report-bound acknowledgment allows the workflow to enter PRD-260's
     separate review/publication lifecycle
  -> review and publication follow their explicit user-approval gates
  -> final result reports structural checks, publication outcome, and remaining
     human-review/trust steps
  -> normal plugin loader imports only after its existing digest-bound trust check
```

## 9. Acceptance criteria

1. Duplicate parameter names, unsupported type expressions, non-literal defaults,
   malformed capabilities, duplicate dependencies, and non-boolean flags are
   rejected without advancing ANALYZE.
2. The validator passes a canonical function-form fixture and a canonical
   class-form fixture whose declarations match the plan.
3. Wrong `TOOLS` exports, name/schema mismatches, unsupported decorators,
   unplanned capabilities, confirmation mismatches, and dependency mismatches
   produce stable actionable failures.
4. A generated module with a top-level marker that would write a file or raise
   is never imported or executed by validation; the marker remains untouched.
5. A passing report becomes stale after the source digest changes and can no
   longer authorize FINALIZE.
6. An agent's `approve_tool("passed")` cannot advance when validation has not
   run, has failed, or refers to another artifact digest.
7. Static validation does not auto-trust the source; the normal loader continues
   to skip the generated project plugin until the exact file is trusted.
8. A restored `FINALIZE`/`COMPLETE` context without a valid passing report is
   rejected; no missing field is rendered as `"(approved)"`.
9. Checkpoint restore preserves the normalized plan and structured report
   without embedding session memory or executable objects.
10. Existing plugin discovery, capability gate, approval, and trust tests remain
    green.

## 10. Test plan

- Strict parser unit tests for every plan field, boundary, duplicate, invalid
  type, and accepted normal form.
- Static validator table tests for both supported tool forms, `TOOLS` exports,
  signature/default/annotation checks, capabilities, confirmation metadata, and
  dependency declarations.
- A non-execution sentinel test proving validation never imports the target.
- Runner tests for absent, failed, stale-digest, and passing reports; rejected
  transitions must not mutate accepted state.
- Checkpoint codec tests for valid state-specific payloads and malformed or
  success-shaped incomplete payloads.
- Integration coverage through the real runner with a compliant candidate and
  one rejected candidate, while the plugin remains untrusted.

## 11. Rollout

1. Define and test the normalized plan schema.
2. Implement the non-executing validator and report type.
3. Make validation and checkpoint transitions report-bound.
4. Update prompts, tests, and documentation to stop asking the agent to import
   generated Python as a validation step.
5. Preserve the existing plugin trust check as an independent later boundary.

## 12. Security and operational considerations

- Generated Python is executable code. A structural pass is not a sandbox,
  semantic review, capability proof, or trust decision.
- Dependency metadata is untrusted input. Parse and bound it; never install it
  automatically.
- Error text and reports must not include unbounded source, credentials, or
  secret-bearing tool output.
- The source digest must be computed from the bytes checked, not from a later
  reread after a mutable-path race.
