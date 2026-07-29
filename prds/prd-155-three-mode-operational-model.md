---
title: "PRD-155: Three-Mode Operational Model"
status: Implemented
version: 1.0.0
created: 2026-07-28
scope: Consolidate interactive execution modes into Safe, Plan, and Yolo
related_prds:
  - PRD-47  # mode-system architecture
  - PRD-48  # historical built-in mode design
  - PRD-75  # mode as application state
  - PRD-78  # approval system
  - PRD-89  # workflow mode reset
  - PRD-91  # Plan mode enforcement
  - PRD-100 # code_plan architecture
  - PRD-114 # composite workflows
  - PRD-138 # repository improvement roadmap
---

# PRD-155 — Consolidated Safe, Plan, and Yolo Modes

## Executive summary

This PRD specified and now records the implementation of replacing the user-facing mode catalogue with three
operational modes:

| Mode | Intended operation |
| --- | --- |
| **Safe** | Normal operation with approval required before side effects |
| **Plan** | Read-only planning and analysis; side effects are hard-blocked |
| **Yolo** | Unrestricted operation; this is the current `Auto` mode |

The change was not a label-only rename. The original runtime had two mode
models, the original `Safe`
mode hard-blocks tools instead of asking for approval, `Guard` currently owns
approval semantics, workflow phases refer to mode names, and several callers
assume that `Auto` is the reset/default mode.

The recommended design is to make the runtime registry canonical, implement
Safe as an approval policy for side-effect capabilities, preserve Plan's hard
restrictions, rename Auto to Yolo at the product boundary, and make Safe the
default for new interactive sessions. Compatibility aliases should be accepted
at migration boundaries but should not create additional user-visible modes.

The implementation is complete in the current runtime. The canonical registry
is `agenthicc.tui.runtime.mode_manager.ModeRegistry`; the legacy
`agenthicc.modes` package is retained only as a compatibility adapter for
downstream imports and mode plugins. Verification evidence is recorded below.

## Implementation record

The resolved choices are:

- missing, empty, malformed, and unknown capability metadata is classified as
  `UNDECLARED`; Safe requests approval and Plan blocks it;
- `Review` aliases Plan, while `Debug` is rejected rather than granted Yolo
  permissions;
- mode-bound workflow completion returns to Safe; temporary phase overrides
  restore the exact canonical pre-phase mode in `finally` paths;
- Safe is the default for both interactive and headless session construction;
  headless approval fails closed unless the explicit dangerous-permissions flag
  is supplied, and that flag cannot bypass Plan;
- legacy persisted names are resolved through aliases and rewritten in
  canonical form on resume.

The implementation adds the canonical Safe → Plan → Yolo registry and internal
Replay state, capability/approval enforcement, lifecycle restoration, workflow
override migration, persistence migration, command/UI updates, and dedicated
unit, integration, and E2E policy coverage. The repository verification matrix
is listed in §11.

## 1. Evidence from the current repository

The assessment was made against the source tree before the implementation
recorded in this document, rather than historical PRD examples. The tables in
§1 intentionally preserve the migration evidence and are not the post-change
runtime contract.

### 1.1 Two mode representations exist

The legacy mode model is in `src/agenthicc/modes/`:

- `Mode` carries a tool filter, system-prompt patch, hooks, and plugin metadata.
- `ModeManager` and `ModeRegistry` manage those modes.
- `agenthicc.modes.builtin.build_default_registry()` currently registers
  `Auto`, `Plan`, and `Safe`.

The active TUI/runtime model is in
`src/agenthicc/tui/runtime/mode_manager.py`:

- `RuntimeMode` carries blocked capabilities, approval requirements, a default
  workflow, and workflow bindings.
- `ModeRegistry` stores runtime modes and `ModeManager` selects one.
- `build_default_registry()` adapts legacy modes and adds runtime-only
  `Guard` and `Replay` entries.

The runtime adapter also reaches through the legacy manager's private
`_registry`, so the current implementation has a real ownership and
single-source-of-truth problem. Consolidation should resolve that rather than
adding another translation layer.

### 1.2 Current behaviour does not match the requested semantics

The effective built-ins are currently:

| Current entry | Current capability policy | Current approval policy |
| --- | --- | --- |
| `Auto` | no capability blocks | no approval |
| `Plan` | blocks write, git write, execute, and network | no approval |
| `Safe` | narrow read-only allowlist; side effects are filtered | no approval |
| `Guard` | capability-aware approval for side effects | approval required |
| `Replay` | all capabilities blocked | no approval |

`Safe` therefore cannot simply be retained and have its name made the default:
that would make the default session reject side effects instead of asking the
user. The `Guard` policy must be moved into Safe or explicitly composed into
it.

Capability enforcement is implemented by
`src/agenthicc/tools/capability_gate.py`; approval enforcement is implemented
by `src/agenthicc/tools/approval.py`. The gates are separate and ordered. A
tool whose capability metadata is absent currently has an empty capability set,
which means it passes both mode restrictions and approval checks. This is a
security-relevant gap for a Safe mode and must be resolved as part of this
work.

### 1.3 Default and transition assumptions are distributed

The following current assumptions must be changed together:

- `conversation_store.AppState` initializes an `Auto` runtime mode.
- `TUISession._build_session_context()` explicitly selects `Auto`.
- completed mode-bound workflows reset to `Auto` and notify the user.
- `code_plan` binds to `Plan` and temporarily overrides its execute phase to
  `Auto`.
- `create_workflow` temporarily overrides its generate phase to `Auto`.
- capability-gate error text recommends switching to `Auto` or `Debug`.
- the `/mode` command and mode-cycle input enumerate the runtime registry.
- headless execution uses an approval service that denies approval-gated
  actions rather than waiting for a TUI prompt.

These are observable product contracts, not implementation details.

## 2. Problem statement

The current catalogue exposes more concepts than users need while assigning
similar concepts different names in different layers. `Auto` is the unrestricted
execution mode, `Guard` is the approval mode, and `Safe` sounds like the mode a
careful user should select but actually hard-blocks side effects. `Plan` is the
only mode whose hard read-only semantics are unambiguous.

This creates four risks:

1. Users cannot infer whether a blocked tool will be rejected or presented for
   approval.
2. New-session and workflow-reset defaults are encoded in multiple places and
   can drift.
3. Workflow phase overrides use names that will become invalid or misleading
   after `Auto` becomes `Yolo`.
4. Unannotated tools can bypass the intended Safe approval boundary because the
   current capability model is open by default.

## 3. Goals

- Present exactly three user-facing operational modes: Safe, Plan, and Yolo.
- Define Yolo as the current Auto behaviour, including its unrestricted
  capability policy.
- Keep Plan read-only with hard enforcement at the capability gate.
- Make Safe the default for new interactive sessions.
- Make Safe request approval for side-effecting capabilities instead of
  silently hard-blocking them.
- Preserve approval UX, denial/retry behaviour, headless non-hanging behaviour,
  and the dangerous-permissions escape hatch.
- Establish one canonical runtime registry and one canonical mode identity used
  by TUI, headless execution, commands, workflows, and plugins.
- Preserve resumability, phase overrides, artefacts, and mode restoration for
  existing workflows.
- Provide a compatibility and migration path for persisted state, plugins,
  configuration, prompts, and command input.

## 4. Non-goals

- Redesigning the approval overlay or changing the approval response options.
- Making Plan approval-gated; Plan remains a hard-blocked mode.
- Removing internal replay support. Replay may remain an internal execution
  state even if it is not a selectable user mode.
- Giving Yolo a new permission model. Yolo is the product rename for Auto.
- Introducing a second execution engine or a second application-state model.
- Automatically rewriting arbitrary third-party plugin code without a
  compatibility boundary.
- Making every tool unrestricted in Safe. Safe must have a machine-enforced
  side-effect boundary.

## 5. Proposed product contract

### 5.1 User-facing catalogue

The runtime registry exposes these selectable modes in this order:

```text
Safe → Plan → Yolo → Safe
```

The `/mode` command accepts the three canonical names case-insensitively and
the cycle control follows the same order. The UI badge, footer, help text,
notifications, tool errors, and prompts use the canonical names.

Suggested descriptions:

- **Safe** — “Actions that can change files, run commands, modify git, or use
  the network require your approval.”
- **Plan** — “Read and analyze only. Side-effecting actions are blocked.”
- **Yolo** — “Run with the current Auto permissions; no per-action approval.”

Badges and colours may be retained or redesigned separately; they are not part
of the feasibility decision.

### 5.2 Capability matrix

The mode policy is capability-based, not tool-name-based:

| Capability | Safe | Plan | Yolo |
| --- | --- | --- | --- |
| `READ` | allow | allow | allow |
| `SEARCH` | allow | allow | allow |
| `GIT_READ` | allow | allow | allow |
| `WRITE` | ask | block | allow |
| `GIT_WRITE` | ask | block | allow |
| `EXECUTE` | ask | block | allow |
| `NETWORK` | ask | block | allow |

“Ask” means that the normal `ApprovalGate` requests a decision and returns a
structured denial to the agent when the user rejects it. It does not mean that
the capability gate permits the action without a subsequent approval result.

Plan's hard block must remain before approval handling, so switching to a
dangerous-permissions flag cannot turn Plan into an unrestricted mode.

### 5.3 Unknown and unannotated tools

The current `get_tool_capabilities()` behaviour treats missing metadata as an
empty set. That is incompatible with a trustworthy Safe mode for tools that
could have side effects.

The implementation must choose and document one of these policies before
release:

1. **Conservative default (recommended):** an unannotated tool is treated as
   side-effecting in Safe and requires approval; it remains blocked in Plan
   unless explicitly classified as read-only.
2. **Registration-time rejection:** all tools must declare capabilities before
   registration; missing metadata is a plugin/load error.
3. **Audited compatibility exception:** retain open-by-default only for a
   versioned, explicitly allowlisted set of proven read-only tools and emit a
   diagnostic for every other unannotated tool.

The first option gives downstream plugins a migration path while preserving a
fail-safe default. The selected policy must cover built-ins, plugin tools,
MCP tools, and dynamically registered callables.

## 6. Compatibility and migration

Canonical identity and display identity should be separated so old state can
be read without creating extra selectable modes.

| Legacy name | Compatibility interpretation | User-facing status |
| --- | --- | --- |
| `Auto` | alias of `Yolo` | not listed |
| `Guard` | alias of `Safe` | not listed |
| `Ask` | alias of `Safe` if retained for old plugins/configuration | not listed |
| `Review` | requires an explicit product decision; recommended alias of `Plan` | not listed |
| `Debug` | requires an explicit product decision; do not silently grant Yolo permissions | not listed |
| `Replay` | internal-only mode/state | not listed |

Compatibility lookup must be applied to:

- persisted session or resume state;
- `PhaseSpec.mode_override` and workflow `mode_bindings`;
- `/mode` input and any programmatic mode-selection API;
- plugin declarations and legacy mode exports;
- user configuration, if mode configuration is exposed later;
- user-facing messages and generated prompts.

The migration must be idempotent: reading and writing a legacy `Auto` state
must produce Yolo semantics, never both an `Auto` and a `Yolo` entry. Unknown
mode names must fail with an actionable error rather than silently falling back
to Yolo.

## 7. Architecture and implementation phases

### Phase A — Canonical runtime registry

1. Define canonical mode identifiers and a compatibility-alias map in the
   runtime mode module.
2. Make `RuntimeMode` the source of truth for selectable mode policy.
3. Adapt legacy `Mode` plugins at one boundary, or provide a documented
   legacy adapter; do not have runtime code reach through private legacy
   registry fields.
4. Give the registry explicit selection order, canonical lookup, aliases, and
   a separate internal-only registration path for Replay.
5. Ensure duplicate canonical names and alias collisions fail deterministically
   during registry construction.

### Phase B — Safe approval semantics

1. Move the current Guard approval policy to Safe.
2. Remove Safe's current read-only hard filter from the default Safe policy.
3. Keep Plan's capability blocks enforced before approval.
4. Define how missing capability metadata is classified, then test every
   tool-registration route against that rule.
5. Preserve approval scope semantics: allow once, remember for the turn, and
   remember for the session must remain distinct.
6. Ensure approval state is cleared on denial, cancellation, failed execution,
   and session termination; it must never authorize a later unrelated turn.

### Phase C — Defaults and lifecycle

1. Initialize `AppState.active_mode` to Safe.
2. Select Safe in interactive session construction and remove hard-coded Auto
   reset calls.
3. Preserve the user's explicit mode when a phase temporarily overrides it;
   restore the exact canonical mode after the phase, including on failure,
   cancellation, and resume.
4. Decide the post-completion policy for mode-bound workflows. Recommended:
   return to Safe after a mode-bound workflow completes, because Safe is the
   default safety posture; make this an explicit lifecycle rule and notify the
   user.
5. Do not silently change a user's selected Plan/Yolo mode after an unrelated
   workflow completes.

The last two rules intentionally supersede the current Auto reset assumption
described in PRD-89. The implementation must update that PRD's status or add
an explicit superseded-by link when this PRD is implemented.

### Phase D — Workflow and command bindings

1. Update `code_plan`'s execute phase and `create_workflow`'s generation phase
   from `Auto` to `Yolo` while accepting Auto as a compatibility alias.
2. Keep code-plan entry bound to Plan unless the product deliberately changes
   the invocation contract.
3. Audit every `default_workflow`, `mode_bindings`, and `mode_override` in
   `src/agenthicc/workflows/`.
4. Update `/mode`, cycle input, footer rendering, help, notifications, and
   capability-gate errors to use canonical names.
5. Keep Replay's restoration path separate from selectable mode cycling.

### Phase E — Configuration, CLI, and headless execution

1. If a default-mode setting is exposed, define whether it applies to
   interactive sessions only or also headless runs. The recommended default is
   Safe in both, with no prompt-dependent hang.
2. Keep `--dangerously-skip-permissions` narrowly documented as bypassing Safe
   approval prompts. It must not override Plan hard blocks or turn unknown
   modes into Yolo.
3. Headless Safe must use its existing fail-closed approval adapter: actions
   requiring approval are denied with a structured result unless the explicit
   dangerous-permissions flag is supplied. It must never wait for an
   interactive approval event that cannot be answered.
4. Persist canonical mode identifiers and migrate old identifiers on load.

## 8. Security and reliability considerations

- Safe is a user-facing security boundary. Capability classification must be
  evaluated in the executor path, not only in prompts or UI state.
- Capability metadata must be attached before the gate evaluates the tool.
  Tests must cover missing, malformed, and conflicting metadata.
- A capability declaration is not authorization. Safe approval remains
  mandatory for side-effect capabilities, and Plan blocks them regardless of
  approval.
- Approval requests must be correlated with the current tool call and turn;
  stale responses must not approve a new call.
- Cancellation and errors must clean pending approval state and restore the
  pre-override mode in a `finally` path.
- Resume must preserve the canonical mode, phase, approval state, and workflow
  artefacts without replaying an already granted one-shot approval.
- Plugin aliases and legacy adapters must not bypass capability or approval
  gates.
- UI labels are informational. The runtime mode object and executor gates are
  authoritative.

## 9. Rollout and migration plan

### Step 1 — Instrumentation and compatibility boundary

Add canonical lookup and alias resolution while retaining current behaviour.
Record which legacy names are encountered by persisted state, workflows,
plugins, and commands. Do not log prompts, tool arguments, credentials, or
session contents.

### Step 2 — Registry and policy migration

Switch runtime construction to the canonical three-mode registry. Move Guard's
approval policy to Safe, add the selected unknown-tool policy, and update
workflow overrides. Keep aliases enabled and emit a concise deprecation notice
only where appropriate.

### Step 3 — Safe default and user-facing cutover

Change new interactive sessions to Safe, update the UI and documentation, and
verify that first-turn read-only requests work without approval while
side-effecting requests pause for approval.

### Step 4 — Deprecation cleanup

After at least one compatibility window, remove selectable legacy entries and
legacy display strings. Keep a versioned reader for persisted state for as long
as supported session files can exist.

Rollback is registry/configuration based: restore the prior default and
legacy-policy adapter without rewriting persisted state. Canonical persisted
identifiers remain readable by the rollback reader.

## 10. Acceptance criteria

The implementation is complete only when all of the following are true:

1. The selectable registry contains exactly Safe, Plan, and Yolo in the
   documented cycle order.
2. Yolo has the current Auto capability and prompt semantics.
3. Safe is the initial mode for a new interactive session.
4. Safe allows read/search/git-read tools without approval.
5. Safe requests approval for write, git-write, execute, and network tools.
6. A Safe denial returns a structured tool result and leaves no stale approval.
7. Plan hard-blocks all side-effect capabilities before approval is requested.
8. Yolo does not request per-action approval for the capabilities previously
   unrestricted in Auto.
9. The dangerous-permissions flag bypasses Safe approvals but not Plan blocks.
10. Unknown/unannotated tools follow the selected conservative policy in Safe
    and Plan.
11. Alias lookup maps Auto to Yolo and Guard to Safe without adding duplicate
    selectable entries.
12. Legacy mode names in resume state and workflow specs are migrated or
    rejected with actionable diagnostics.
13. `/mode`, cycling, footer text, help, and errors expose canonical names.
14. `code_plan` and `create_workflow` phase overrides use Yolo semantics and
    restore the previous canonical mode on success, rejection, error,
    cancellation, and resume.
15. A mode-bound workflow's completion/reset behaviour is explicit, tested, and
    consistent with Safe as the default.
16. Replay remains able to restore the exact prior mode but is not selectable
    through normal cycling.
17. Headless Safe never hangs waiting for a TUI approval response.
18. Plugin, MCP, dynamically registered, and built-in tools all pass through
    the same capability and approval gates.
19. Unit, integration, and end-to-end coverage verifies normal transitions,
    rejection loops, retries, parallel/phase overrides, resume, cancellation,
    and registry migration.

## 11. Verification plan

### Unit tests

- canonical mode identifiers, aliases, ordering, duplicate detection, and
  unknown-name errors;
- `RuntimeMode` policy for every capability;
- Safe approval and Plan hard-block precedence;
- unknown-tool classification and malformed metadata;
- approval scope, stale-response rejection, and cleanup;
- default initialization, lifecycle reset, and phase override restoration;
- plugin/legacy adapter conversion and Replay exclusion from cycling.

### Integration tests

- TUI session construction starts in Safe;
- `/mode` and cycle controls select only canonical modes;
- tool execution flows through capability gate then approval gate;
- allow, deny, retry, cancellation, and failed tool calls preserve state;
- `code_plan` and `create_workflow` use Yolo for write-capable phases and
  restore Safe/Plan/Yolo correctly;
- headless Safe denies instead of hanging;
- persisted legacy mode names resume with canonical semantics;
- MCP and plugin tools cannot bypass the policy.

### End-to-end tests

- first interactive turn: read a file without a prompt, then request a write
  and approve it;
- Safe denial causes the agent to recover/retry without changing mode;
- Plan attempts a write and receives a hard block with no approval overlay;
- Yolo performs the same write without an approval overlay;
- interrupted and resumed workflow runs preserve mode and artefacts;
- a headless Safe run terminates with a structured denial;
- legacy `Auto`/`Guard` input and persisted state resolve to Yolo/Safe.

Required repository checks for implementation are:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

The implementation should also run the type-audit and `llms_check` gates if
public exports or type contracts change, as required by `AGENTS.md`.

## 12. Decision record

### Implementation decision

The implementation fits the existing capability gate,
approval service, runtime mode manager, workflow override, and headless
approval boundaries. The work should be treated as a cross-cutting migration,
not a small UI change.

### Resolved decisions

1. Conservative Safe approval for unknown tools is implemented.
2. `Review` aliases Plan; `Debug` is rejected with an actionable error.
3. Mode-bound workflow completion returns to Safe; temporary phase overrides
   restore the exact prior mode.
4. No user-configured default-mode setting is exposed; Safe is the fixed
   fallback for interactive and headless construction.
5. Persisted mode metadata is migrated idempotently by rewriting aliases to
   canonical names; unknown values fail resume rather than falling back.

## 13. Related documentation

- [PRD-47 — Mode System Architecture](prd-47-mode-system-architecture.md)
- [PRD-48 — Built-in Modes](prd-48-builtin-modes.md)
- [PRD-75 — Mode as State](prd-75-mode-as-state.md)
- [PRD-78 — Approval System](prd-78-approval-system.md)
- [PRD-89 — Plan Workflow Guards](prd-89-plan-workflow-guards.md)
- [PRD-91 — Plan Mode Enforcement](prd-91-plan-mode-enforcement.md)
- [PRD-100 — Code Plan Architecture](prd-100-code-plan-architecture.md)
- [PRD-114 — Composite Workflows](prd-114-composite-workflows.md)
- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-154 — `create_workflow` Architecture](prd-154-create-workflow-architecture.md)

Implementation must update the current mode guide and public reference
documentation in the same change. Historical PRDs should retain their record
but link to this PRD when their Auto/Guard/Safe assumptions are superseded.
