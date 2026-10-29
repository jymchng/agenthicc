---
title: "SUBAGENT-05: Per-type permission narrowing"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "the optional per-type constraints that can restrict a worker's tools, capabilities, approval requirements and workspace beyond what it inherits, and the rule that they can never widen it"
area: A-5
related_prds:
  - PRD-197
  - PRD-19
  - PRD-233
  - SUBAGENT-04
  - AGX-09
tags:
  - subagents
  - permissions
  - narrowing
  - capability-scope
  - security
---

# SUBAGENT-05: Per-type permission narrowing

## 1. Executive summary

A subagent type can restrict its workers along exactly one axis: a positive
allow-list of tool names. Its capabilities, its approval requirements and its
workspace scope are inherited from the parent turn and are identical for every
type, so a type cannot say "never write" or "always ask" — it can only fail to
list the tools that write, and hope the list stays exhaustive as the tool
registry grows.

Two permission systems exist in this tree. One is live, mode-derived, and coarse.
The other is complete, tested, narrowing-only — `AgentCapabilityScope`,
`ScopeManager`, `restrict()` — and **nothing calls it**: 21 passing tests, zero
callers in `src/`.

This PRD does not design a third. It adds an optional `permissions:` block to the
type record whose every field may only narrow, and routes each field to a
mechanism that already enforces it: `restrict()` for tool denies, a per-type
`RuntimeMode` for capability and approval tightening, and a child `WorkspaceScope`
verified as a subset. Absent block, nothing changes.

## 2. Problem statement

All claims verified at commit `ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780`
(2026-10-29); commands and output are re-quoted in §3.

**One field, one axis.** `SubagentTypeSpec` carries `name`, `allowed_tools`,
`max_turns`, `system_prompt` and `max_turn_time_s`
(`src/agenthicc/subagents/types.py:87-91`). The docstring is explicit that
`allowed_tools` "is a ceiling that is applied after the parent phase and mode
have filtered the session tool list" (`:83-84`) — a ceiling on *names*, and
nothing else. There is no deny-list, no capability constraint, no approval
override and no workspace constraint on the record.

**Inheritance is whole and identical for every type.** PRD-197's snapshot is
`SubagentExecutionPolicy` (`src/agenthicc/subagents/policy.py:25-45`) carrying
`blocked_capabilities`, `approval_required`, `visible_tool_names` and
`workspace_scope_identity`, captured once at the spawn boundary by
`from_app_state` (`:48`) and hashed into `policy_revision` (`:88-90`). Every
worker receives the same object, so every worker of every type has the same
capability blocks and the same approval requirements.

**Enforcement is mode-level, so it cannot be per-type.** The worker installs
`ToolCapabilityGate(self._app_state)` and `ApprovalGate(self._app_state, …)`
(`src/agenthicc/subagents/pool.py:856-870`). Those gates read the *mode*:
`src/agenthicc/tools/capability_gate.py:44` consults `mode.blocked_capabilities`, and
`src/agenthicc/tools/approval.py:564` consults `mode.approval_required`. Both resolve through
`child_app_state` → `policy.runtime_mode()`
(`src/agenthicc/subagents/policy.py:106-119`, `:156-165`). A type has no way to
contribute to either set, so a type that must never write files cannot say so —
it can only omit the write tools from its allow-list.

**Workspace scope is shared by reference.** `child_workspace_access` constructs
`WorkspaceAccessPolicy(parent.scope, …)`
(`src/agenthicc/subagents/policy.py:167-181`): the *same* `WorkspaceScope`
object, so all types see the same roots. Narrowing to a subdirectory is
mechanically possible — `WorkspaceScope.create(primary_root, allowed_paths)`
builds a root tuple (`src/agenthicc/tools/workspace_access.py:86-110`) — but
nothing consults the type.

**Allow-lists fail open as the registry grows.** `_expand_allowed` resolves each
pattern through `registry.glob_expand` (`src/agenthicc/subagents/pool.py:613-616`;
implementation at `src/agenthicc/plugins/registry.py:169-185`), and the expansion
happens once, per worker, at construction (`pool.py:682`). A type declaring
`"fs.*"` receives every tool in that group *as it exists at construction time*,
so a tool added to the group later is granted to that type with no edit to the
type and no test failing. Only an explicit deny survives registry growth.

**The narrowing framework already exists and is never used.** `security.py`
contains `AgentCapabilityScope` (`src/agenthicc/security.py:54`): `allowed_tools`
where `None` means all, `denied_tools` with `fnmatch` wildcards whose denies
"always take precedence" (`:63-65`), `is_tool_allowed` implementing that order
(`:80-90`), `max_tool_call_budget` and `max_spawn_depth`. Its `restrict()` method
(`:92-128`) computes an intersection that is "always more restrictive":
allow-sets intersect, denies union, budgets take the minimum. `ScopeManager`
(`:156-223`) registers children as `parent.restrict(child)` (`:191`),
`update_scope` downscopes a running agent (`:203-211`), and `can_spawn`
(`:214-224`) gates depth. `PermissionChecker` (`:232`) consults the scope first
and treats a scope-level deny as fail-closed.

**…and it is dead code.** `AgentCapabilityScope` and `ScopeManager` appear in
`src/` only inside `security.py` (§3: grep exit 1), and in the test suite only in
`tests/unit/test_agent_permissions.py`, which runs **21 tests, all passing**.
The subsystem that was built to do precisely this job is exercised by nothing but
its own tests.

**Its origin document asked for this, for subagents.** PRD-19 is `draft` and
opens by naming the exact use case: "When an orchestrator spawns sub-agents, it
should be able to restrict what tools each sub-agent can call — a 'docs' agent
should only see file tools, not `run_bash` or `git_commit`"
(`prds/prd-19-agent-tool-permissions.md:12`). Its data model shipped; its
integration into the subagent path did not, and its `scope_restrict`
communication tool does not exist in `src/` at all (§3).

**Consequence for users.** A reviewer that must never mutate the tree is
protected only by an enumeration that a future tool can silently escape. There is
no way to say "this type always requires approval", so a type handling untrusted
input cannot be made more careful than its siblings. And a project that wants a
docs type confined to `docs/` cannot express it, though the workspace machinery
can already represent it.

## 3. Current-source investigation

Run on 2026-10-29. `git rev-parse HEAD` at the time of writing:

```console
$ git rev-parse HEAD
f700733bcc8183bf3b080e83523e24dbc086f6db

$ git diff --stat ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780 HEAD -- src/
(no output: src/ is byte-identical to the research baseline)
```

The narrowing framework, and the absence of callers:

```console
$ grep -n "class AgentCapabilityScope\|class ScopeManager\|class PermissionChecker" \
    src/agenthicc/security.py
54:class AgentCapabilityScope:
156:class ScopeManager:
232:class PermissionChecker:

$ grep -rn "AgentCapabilityScope" src/ --include=*.py | grep -v "security.py"
(no output; grep exit status 1)

$ grep -rn "ScopeManager" src/ tests/ --include=*.py | grep -v "src/agenthicc/security.py:"
tests/unit/test_agent_permissions.py:5:from agenthicc.security import ...
tests/unit/test_agent_permissions.py:79:class TestScopeManager:
... (32 further lines, all in the same test file)

$ python -m pytest tests/unit/test_agent_permissions.py -q
21 passed in 0.43s

$ grep -rn "scope_restrict" src/ tests/ --include=*.py
tests/unit/test_agent_permissions.py:102:    def test_update_scope_restricts(self):
```

The last two lines say the most: the data model is green and the *integration* —
the `scope_restrict` tool PRD-19 specifies — was never written. The single
`scope_restrict` hit is a test method name that happens to contain the string.

The per-type surface and the mode-level enforcement:

```console
$ sed -n '87,91p' src/agenthicc/subagents/types.py
    name: str
    allowed_tools: frozenset[str]
    max_turns: int
    system_prompt: str
    max_turn_time_s: float = DEFAULT_SUBAGENT_TIMEOUT_S

$ sed -n '856,870p' src/agenthicc/subagents/pool.py
        hooks: list[object] = []
        if self._app_state is not None:
            from agenthicc.tools.capability_gate import ToolCapabilityGate

            hooks.append(ToolCapabilityGate(self._app_state))
            if self._approval_svc is not None:
                from agenthicc.tools.approval import ApprovalGate

                hooks.append(
                    ApprovalGate(
                        self._app_state,
                        self._approval_svc,
                        self._workspace_access,
                    )
                )
```

Absence claims, each with the pattern and the scope that establish it:

- **No per-type deny-list, capability block, approval override or workspace
  restriction.** Patterns `deny_tools|denied_tools|blocked_capabilities|`
  `approval_required|workspace_paths|permissions:`, scope
  `src/agenthicc/subagents/types.py`: no match outside the docstring prose. The
  record has five fields and none of them is a constraint beyond
  `allowed_tools`.
- **No subagent path consults a capability scope.** Pattern
  `AgentCapabilityScope|ScopeManager|is_tool_allowed|restrict(`, scope
  `src/agenthicc/subagents/`: no match. The worker filters by name
  (`pool.py:807-816`) and gates by mode (`:856-870`); neither reads a scope.
- **No `scope_restrict` tool.** Pattern `scope_restrict`, scope `src/`: no match
  (§3 console above shows the one test-name false positive).
- **No per-type schema for permissions in the definition format.** Pattern
  `permissions`, scope `prds/agx-09-agent-library-and-switching.md`: the base
  format is `name`, `description` and body (§7 quotes it), with no permission
  section.

Diff against the research report's `f843040` snapshot: **nothing has moved** for
G-5. `types.py:88`, the ceiling prose at `:83-84` and the PRD-197 inheritance
path remain the accurate citations.

Three surprises, and the first changed the whole design.

**The mechanism exists and is dead.** The report's G-5 entry says narrowing
"does not" exist, citing the `allowed_tools` ceiling. That is true of the
*subagent path*. It is not true of the tree: `security.py` has had a
narrowing-only capability framework, with deny patterns and a proven `restrict()`
intersection, since PRD-19 — and it is referenced only by its own tests. A PRD
that designed a new deny-list would be the third permission system in a codebase
that already has two. Goal 7 exists because of this.

**`max_spawn_depth` and `can_spawn` are part of the same dormant framework.**
`src/agenthicc/security.py:71`, `:78`, `:214-224` implement a depth gate with a default of 3,
and nothing calls it. SUBAGENT-04 (A-4) concluded that no depth concept existed,
because its investigation was scoped to `src/agenthicc/subagents/` and this lives
outside it. That is a scoping artefact worth recording rather than quietly
fixing: A-4's *conclusion* stands (the subagent path has no depth), but its
"no partly built mechanism exists" is too strong, and A-5 hands the dormant gate
to A-4 explicitly (§9) instead of half-wiring it here.

**Allow-lists widen themselves via globs.** `_expand_allowed` calls
`registry.glob_expand` (`pool.py:613-616`), and `glob_expand` documents
`"fs.*" → all tool names registered under group "fs"`
(`src/agenthicc/plugins/registry.py:169-185`). Because expansion happens per worker at
construction (`pool.py:682`), a type granted `"fs.*"` silently acquires tools
added to that group later. This is the concrete cost of having no deny axis, and
it is why goal 3 is a *deny* rather than a wider allow.

## 4. Goals

1. A type definition may declare an optional `permissions:` block. Absent means
   exactly today's behaviour: no extra constraint, no change to the effective
   policy, no change to `policy_revision`. (Gap G-5)
2. Every field in that block may only narrow. This is structural, not a
   convention: the effective constraint is computed by
   `AgentCapabilityScope.restrict()` (`src/agenthicc/security.py:92-128`), whose documented
   contract is "always more restrictive", and each individual field is checked
   for wideness at definition-load time.
3. `deny_tools` is an `fnmatch` pattern set whose denies take precedence over any
   allow, evaluated by the existing `is_tool_allowed` (`src/agenthicc/security.py:80-90`), so
   one implementation decides tool membership for both paths. This is what makes
   a type safe against registry growth: a deny survives a new `fs.*` tool, a
   shorter allow-list does not.
4. A type may refuse a *capability* as well as a tool name, so `WRITE` can be
   blocked even if a write-capable tool reaches the worker's tool list through a
   glob. Capability denies union into the worker's `RuntimeMode`
   (`src/agenthicc/tui/runtime/mode_manager.py:49-60`), which `ToolCapabilityGate` already
   enforces (`src/agenthicc/tools/capability_gate.py:44`).
5. A type may **tighten** approval: `require_approval` unions into the worker's
   `RuntimeMode.approval_required`, which `ApprovalGate` already enforces
   (`src/agenthicc/tools/approval.py:564`). It can add a requirement and can never remove one, so a
   type handling untrusted input can be made stricter than its siblings.
6. A type may restrict its workspace to a subset: `workspace_paths` must resolve
   to roots contained in the inherited scope, checked at load and again at
   construction, so an escaping path is refused rather than silently accepted.
   The primary root is the narrowest root the type declares, never the parent's.
7. One merge path. The effective scope is `inherited.restrict(declared)` and the
   effective mode is the inherited mode with two frozensets unioned. There is no
   second intersection routine, no per-field precedence table, and no code path
   in which the declared block is consulted before the inherited one.
8. Definitions fail closed at load. An unknown capability name, a deny that is
   not a valid pattern, a workspace path outside the inherited scope, or a field
   that cannot narrow is a validation error naming the file and the key, and the
   type is not registered — following AGX-09's existing rule that unknown keys
   are errors, not silently ignored (`prds/agx-09-agent-library-and-switching.md:92`).
9. Boundaries are recorded rather than absorbed: the dormant `max_spawn_depth` /
   `can_spawn` gate belongs to SUBAGENT-04, PRD-19's unbuilt `scope_restrict`
   tool belongs to SUBAGENT-08, and per-agent profiles belong to PRD-233. §9
   states each.
10. No behaviour change for the nine built-in types, none of which declares a
    `permissions:` block, and no change for any existing definition.

## 5. Non-goals

The A-5 boundaries from the research report §5.3, restated as commitments:

- **The inheritance contract itself.** PRD-197 is `Implemented` and owns how a
  worker inherits mode, capability, approval and execution context
  (`src/agenthicc/subagents/policy.py:25-45`, `:48`). This PRD cites that base
  and adds only the narrowing delta; it does not restate, reimplement or
  renegotiate it. If a reader finds this document explaining *how inheritance
  works*, one of the two documents has overreached.
- **Depth limits.** `max_spawn_depth` and `can_spawn` (`src/agenthicc/security.py:71`, `:214-224`)
  are dormant and belong to SUBAGENT-04 (A-4). This PRD does not wire them,
  because a half-wired depth gate would be a second depth mechanism beside the
  one A-4 defines.
- **Mid-flight downscoping.** PRD-19's `scope_restrict` tool, which would narrow a
  *running* agent, is a lifecycle operation and belongs to SUBAGENT-08 (A-8) with
  steering and cancellation. This PRD sets constraints at spawn; changing them
  while a worker runs is not its business.
- **Per-agent profiles and per-agent policy.** PRD-233 proposes declarative
  profiles with per-agent policy for *agents*. This PRD governs subagent *types*
  and must not become a second profile system; §9 states the relationship.
- **The mode system and the TUI's mode switching.** `RuntimeMode` and
  `ModeRegistry` are existing policy; this PRD narrows a mode per worker and
  never edits, adds or reorders a mode.
- **The global `SecurityPolicy` and kernel permission checks.** This PRD composes
  with them and changes neither.
- **Per-subagent worktree isolation.** Raised as R-3 in the research report §5.5
  and carried as an **open question**, not a requirement: the evidence is
  Claude Code and Qoder only, and `policy.py` already constructs a child
  workspace policy. This PRD defines the scope-narrowing hook it would need and
  explicitly does not decide that isolation is wanted.
- **Model and turn budgets.** A-2 and A-6 own those; the depth-tier rule in A-4
  composes with this PRD's narrowing and may only narrow further.

## 6. Acceptance criteria

1. A type with no `permissions:` block yields an effective scope equal to the
   inherited one and an unchanged `policy_revision`, so the default path is
   provably inert: `tests/unit/test_subagent_pool.py`.
2. A `deny_tools` pattern blocks a tool that the same type's `allowed_tools`
   permits, proving denies take precedence over allows:
   `tests/unit/test_subagent_plugin.py`.
3. A wildcard deny (`outlook_*`) blocks every matching tool, including one
   registered after the type was defined, which is the registry-growth case the
   deny exists for: `tests/unit/test_agent_permissions.py`.
4. A `deny_capabilities` entry removes that capability from the worker's
   `RuntimeMode`, and a tool carrying only a denied capability is refused by the
   gate even though it appears in the worker's tool list:
   `tests/unit/test_subagent_pool.py`.
5. A `require_approval` entry causes a tool the parent mode allowed without
   approval to require approval for that type only, while a sibling type using
   the same mode is unaffected: `tests/unit/test_subagent_pool.py`.
6. A `workspace_paths` entry narrows `allowed_roots` to a strict subset, and a
   path outside the inherited scope is refused at definition load with the file
   and key named, leaving the type unregistered:
   `tests/unit/test_subagent_plugin.py`.
7. Attempting to widen — a tool outside the inherited ceiling, or a capability
   the parent blocks, or clearing an inherited deny — is refused at load rather
   than silently ignored: `tests/unit/test_subagent_plugin.py`.
8. The effective scope equals `inherited.restrict(declared)` for a table of
   declarations, asserted against the existing implementation rather than a
   reimplementation: `tests/unit/test_agent_permissions.py`.
9. A definition whose `permissions:` block changes produces a different
   `policy_revision`, so a cached result made under weaker constraints cannot be
   reused: `tests/unit/test_subagent_pool.py`.
10. `pytest tests/unit/test_agent_permissions.py -q` and
    `pytest tests/integration/test_subagent_tool_execution.py -q` pass unchanged,
    proving the nine built-in types admit exactly what they admit today.

## 7. Rollout, migration and security

**Defaults.** No built-in type declares `permissions:`, so nothing changes on day
one: every worker's effective scope is the inherited one, `policy_revision` is
unchanged, and the pool's fingerprints are stable. A type opts in by declaring a
block; a project opts in by defining such a type.

**Migration.** Three ordered steps, each independently shippable:

1. Add the `permissions:` parse and validation with an all-empty default, and
   compute `inherited.restrict(declared)` without yet consulting the result at
   runtime. No behaviour change; a definition that cannot narrow is refused here.
2. Apply the result: tool denies through the existing filter, and the unioned
   frozensets through a per-type `RuntimeMode`. This is the step that changes what
   a worker may do, and only for types that declare something.
3. Apply workspace narrowing, then surface the effective constraint in
   `policy.to_dict()` so it is visible in transcripts and diagnostics.

**Definition-format coordination.** AGX-09 §5.1 fixes the key set and makes
unknown keys a validation error (`prds/agx-09-agent-library-and-switching.md:92`).
This PRD adds one new key, a nested `permissions:` block rather than four
top-level keys, and AGX-09's list and PRD-250's validation must be updated in the
same change — as SUBAGENT-01, -02 and -03 each require for their keys. Declaring
it nested is deliberate: it keeps one extension point for a set of related
constraints, so a later permission field does not become a fifth unilateral
addition to the format.

**Security, and where it fails closed.**

- *Narrowing is structural, not advisory.* The effective constraint is an
  intersection computed by `restrict()`, whose contract is "always more
  restrictive" (`src/agenthicc/security.py:92-128`). A declaration cannot widen by being
  clever, because there is no code path that reads the declared block without
  intersecting it against the inherited one.
- *Fail closed at load.* An unparseable pattern, unknown capability, or
  out-of-scope path refuses the definition and leaves the type unregistered.
  Accepting it and enforcing later would mean the window between load and first
  spawn is governed by an unvalidated rule.
- *Denies before allows.* Deny evaluation precedes the allow check
  (`src/agenthicc/security.py:80-90`), including for tool names introduced after the
  definition was written. This is the antidote to the glob-grows-silently hazard
  in §2, and it is why `deny_tools` is the primary new axis rather than a
  longer allow-list.
- *Approval only tightens.* `require_approval` unions into the inherited set, so
  a type can add a prompt and never suppress one. A worker cannot use its type to
  downgrade the session's approval policy, and there is no `allow_*` counterpart
  field.
- *Workspace subsets are verified twice.* An out-of-scope path is refused at load
  and re-checked against the parent's roots when the child scope is built, so a
  symlink or a moved root cannot turn a checked subset into an escaping scope
  between definition and spawn.
- *Capability denies bind the live gate.* Because `deny_capabilities` unions into
  the mode that `ToolCapabilityGate` reads (`src/agenthicc/tools/capability_gate.py:44`), a denied
  capability is refused at call time even if the tool reached the list — belt and
  braces against the glob hazard, and the one place where a name-level ceiling is
  visibly insufficient.
- *The revised policy is fingerprinted.* A changed `permissions:` block changes
  `policy_revision`, so cached results produced under weaker constraints are not
  reused.
- *Worktree isolation is not silently promised.* §5 records R-3 as an open
  question. This PRD supplies the scope hook; it does not claim isolation, and a
  future implementation must not treat a narrowed scope as filesystem isolation.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_plugin.py -q
$ pytest tests/unit/test_agent_permissions.py -q
$ pytest tests/unit/test_agent_permissions.py -q -k "restrict or deny"
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
```

New cases belong in `tests/unit/test_subagent_plugin.py` (definition parsing,
refusal of widening declarations, workspace subset checks), in
`tests/unit/test_subagent_pool.py` (effective scope, capability deny at the gate,
approval tightening for one type while a sibling is unaffected, fingerprint
change), and in `tests/unit/test_agent_permissions.py` (the `restrict()` merge
table, extending the existing `TestAgentCapabilityScope` cases rather than
duplicating them).

The two tests this PRD most needs are criterion 3 and criterion 4 — a wildcard
deny surviving registry growth, and a denied capability binding a tool that is
still in the list. Both fail today, and both would have to fail again for the
narrowing guarantee to become decorative.

## 9. Related and superseded PRDs

- **PRD-197 — inherits.** PRD-197 is `Implemented` and owns the policy a worker
  inherits: mode, capability, approval, prompt suffix and execution context
  (`src/agenthicc/subagents/policy.py:25-45`, `:48`). This PRD consumes that
  snapshot as the floor and adds only constraints that lower it. It does not
  restate the inheritance contract, and every goal above is expressed as a delta
  against PRD-197's base.
- **PRD-19 — extends, and finishes its integration.** PRD-19 is `draft` and asked
  for exactly this capability for sub-agents (`prds/prd-19-agent-tool-permissions.md:12`),
  specifying `AgentCapabilityScope`, `ScopeManager`, `restrict()`, a
  `max_spawn_depth` and a `scope_restrict` tool. The data model shipped into
  `security.py`; the integration did not. This PRD supplies the definition-
  format surface and wires the existing `restrict()` into the subagent path
  rather than writing a second narrowing library. Two of PRD-19's pieces are
  explicitly *not* taken here: the depth gate goes to SUBAGENT-04, the
  `scope_restrict` tool to SUBAGENT-08. PRD-19 should be marked superseded by the
  two of them plus this document, or revised to reference them.
- **PRD-233 — narrows against.** PRD-233 proposes declarative agent profiles with
  per-agent policy for *agents*. This PRD is deliberately narrower: subagent
  types, and constraints that can only subtract. If PRD-233 lands, its profile
  system owns the general per-agent case and this PRD's `permissions:` block is
  the subagent-type projection of it; a second profile mechanism would be the
  defect the taxonomy exists to prevent.
- **SUBAGENT-04 — extends.** A-4 owns nesting depth and fan-out and states that
  its depth tiers "may only narrow". This PRD supplies the narrowing semantics
  those tiers need, and hands A-4 the dormant `max_spawn_depth` / `can_spawn`
  pair in `src/agenthicc/security.py:214-224` rather than wiring part of it here. A-4 should
  reconcile that discovery in its own §3, which is scoped to `subagents/` and
  therefore records the vocabulary as wholly absent.
- **AGX-09 — extends.** AGX-09 §5.1 owns the definition format. This PRD adds a
  `permissions:` block to it and defines what a subagent does with it; the
  Library tab, editing and hot reload stay AGX-09's.

## A. Competitor evidence

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| opencode | `packages/core/src/permission.ts:19` and `:151`, with defaults at `packages/schema/src/agent.ts:47-49` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | `permissions` is a **ruleset**, not a flat allowlist: action × resource × effect, deny-by-default for agents that declare nothing, and the default agent gets `allow *` with `ask` on `*.env` and `*.env.*` | Take the shape and the deny-by-default fallback, and the `ask` effect as the model for approval *tightening* (goal 5). Reject the full ruleset machinery: agenthicc's three fields cover the observed need without an effect algebra |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Subagent frontmatter carries `tools`, `disallowedTools` and `permissionMode`, and `permissions.deny` blocks specific types; notably, plugin-provided subagents deliberately *ignore* `permissionMode`, `hooks` and `mcpServers` for security | Take `disallowedTools` as the deny axis (goal 3) and the precedent that a type-level permission field may be **ignored** for untrusted definition sources — a load-time rule worth copying. Reject `permissionMode` as a per-type field: it would let a type choose its own mode, which is a widening unless proved otherwise |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | The configuration surface carries `tools`, `disallowedTools` and `permissionMode`, the same shape as Claude Code, documented without a sandbox model | Take only the corroboration that allow + deny + mode is the common triple. Reject as independent evidence: two closed-source docs describing the same shape from the same design lineage is weaker than one code read, per §2.1 |
