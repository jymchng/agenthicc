---
title: "SUBAGENT-18: Parent tool parity for all subagent types"
status: Implemented
version: 1.0.0
date: 2026-11-23
related_prds:
  - SUBAGENT-05
  - SUBAGENT-12
  - SUBAGENT-13
  - SUBAGENT-15
  - SUBAGENT-17
  - TOOLR-08
---

# SUBAGENT-18: Parent tool parity for all subagent types

## Goal

Give every built-in subagent type all 56 built-in tools when the parent
agent has all 56 available. Explorer, Planner, Reviewer, Verifier, and
Researcher gain the same tool set as Implementer, Executor, Tester, and
Documenter. A Researcher can read a corpus, run a check, and write findings to
a requested file in one task. The role determines its assignment and
expected output. The parent session and operator policy determine access.

Inherit tools from the actual parent turn. The 56-tool catalog includes nine
Outlook and Calendar tools. When Outlook is unavailable, a parent with the
otherwise full built-in catalog exposes 47, and its workers inherit those 47.
An available plugin or MCP tool in the parent's delegable tool set follows
the same inheritance rule. A configured mode, phase, skill, or explicit
worker policy can narrow access. [Built-in catalog](../../src/agenthicc/agent_tools.py#L172),
[Catalog tests](../../tests/unit/test_tool_surface.py).

## Baseline behavior

At `3b4be2b`, the five investigative types declare static tool allowlists in
`SubagentTypeSpec`. For example, Researcher receives six read and search
tools. Explorer, Planner, and Reviewer also carry prompt language forbidding
file changes. The four working types already use `allowed_tools=None` to
inherit their parent's tool set. The turn runner selects the parent tool
surface at spawn, and separate paths currently calculate admission and
worker registration. [Built-in roles](../../src/agenthicc/subagents/types.py#L297),
[Parent selection](../../src/agenthicc/runners/agent_turn.py#L1871),
[Admission calculation](../../src/agenthicc/subagents/pool.py#L944),
[Worker registration](../../src/agenthicc/subagents/pool.py#L2001).

The catalog probe counted 56 distinct built-ins and 47 in the local registry
with Outlook unavailable. The existing catalog and role-surface unit tests
passed locally (35 cases). Those tests currently require the investigative
allowlists.

## Implementation

### Use one inheritance rule

- Give all nine built-in types `allowed_tools=None`. Delete the five
  built-in allowlists and update their callers and tests. Keep the role
  prompts focused on the assigned work and output. Remove unconditional
  read-only language from role prompts and descriptions. Explicit task
  instructions such as a read-only review still govern the work.
- Default custom subagent definitions to the same inheritance when they omit
  a tool list. Apply explicit definition tools, operation capabilities,
  permission blocks, depth-tier ceilings, and workspace paths as
  restrictions on the inherited parent surface. Keep configured
  approval requirements and separately declared completion evidence.
  [Definition mapping](../../src/agenthicc/runners/agent_turn.py#L2146),
  [Depth-tier policy](../../src/agenthicc/subagents/governance.py).
- Use the same resolution of the pinned parent surface and effective worker
  policy for type catalog descriptions, admission, isolation, and worker
  tool registration. Carry the real tool schemas, implementations, and
  capability metadata through that path. Derive the fixture's 56 expected
  names from `AGENT_TOOLS` and compare names and schemas, so a missing tool
  cannot be masked by a duplicate or a worker-only tool.
- Preserve tool-surface generation pinning and the active skill's scope.
  A later spawn uses the parent's newly published generation. Include the
  effective inherited tool set and policy revision in the existing
  result-cache fingerprint so a tool or permission change cannot reuse a
  result from a different surface.
  [Spawn and reuse](../../src/agenthicc/subagents/tool.py),
  [Live surface tests](../../tests/integration/test_live_tool_surface.py).

### Apply runtime policy to the expanded tools

Inherit the existing capability gates, approval service, and scoped tool
bindings. Approvals identify the requesting worker. File, command, and Git
tools resolve through the worker's assigned workspace. Network and Outlook
tools use the same configured integration, network policy, and approval
behavior as the parent. The 56-tool catalog consists of workspace,
execution, Git, Outlook, and agenthicc inspection and guidance tools.
Session and workflow controls such as phase transitions and `ask_user` are
registered separately and stay with their existing owner. Workers use the
existing communication tools and depth-governed nested spawning.

Determine which workers can change the workspace from their effective tools
and policy, including allowed filesystem, Git, and command operations that
can change workspace state. Send those workers through
the existing parallel worktree path. A full-access Researcher and
Implementer in the same parallel pool each get their own worktree under
`auto` in a Git workspace. Preserve the supported isolation modes and
their existing refusal and conflict handling. Use the existing evidence
contract to distinguish workspace changes, command execution, and remote
side effects. A Researcher's report remains valid when its task asks for
findings alone. [Isolation selection](../../src/agenthicc/subagents/pool.py#L3352),
[Worktree isolation](../SUBAGENT-15-worktree-isolated-parallel-implementation.md).

## Acceptance criteria and test plan

Use real parent and worker tool-registration paths with scripted transports,
temporary repositories, and mocked external services.

| ID | Acceptance criterion | Required automated test |
| --- | --- | --- |
| A1 | Every built-in type receives all 56 built-ins when the parent exposes all 56. | Parameterize a parent-to-worker provider request test over all nine types. Enable integration availability with a fixture. Compare the parent's 56 built-in names and schemas against the worker request using the canonical catalog. Assert unique registration and the same underlying implementations with worker-scoped bindings. |
| A2 | The worker inherits the actual parent surface. | Repeat with Outlook disabled and assert the exact 47 built-ins. Test a registered extension, a restricted workflow phase, and a skill-scoped spawn. Assert equality for the unrestricted type within each surface. Reload the surface and verify that running workers keep their generation and the next pool sees the new one. |
| A3 | Explicit restrictions and approvals still apply. | Cover an unrestricted custom type and one with explicit tool, capability, path, and depth limits. Test an empty tool scope. Attempt calls forbidden by mode or scope and assert denial before side effects. Exercise a Safe-mode approval and denial for a worker write. Exercise Outlook read and mutation with a mocked backend and inherited policy. |
| A4 | The extra tools support real work in every role. | Run a Researcher with bound read, command, and write calls in a temporary repository and verify its generated notes on disk. Exercise representative inherited Git and introspection tools. Assert an investigative role can complete a report-only task and an implementation role still fails when mandatory mutation evidence is missing. |
| A5 | Expanded roles participate in parallel isolation. | Spawn two newly write-capable workers and a mixed Researcher/Implementer pool under `auto`. Assert distinct worktrees, successful application of disjoint changes, and existing conflict reporting for overlapping changes. Test a parent surface that lacks write access and a required worktree that cannot be created. |
| A6 | The parent catalog and worker output contract reflect the same capabilities. | Compare the `spawn_subagents` descriptions and admission decision against the worker's effective schema in unrestricted and narrowed modes. Verify the prompts permit assigned writes, retain the role's output requirements, and accompany the worker's task-specific context. Keep tests for the session control boundary and nested spawn depth. |
| A7 | Cached results belong to the effective tool surface. | Replay the same task with the same fingerprint and verify reuse. Change the inherited tool set, a permission, or the generation and verify the worker runs with the new surface. Cover an older cached result produced under an investigative allowlist. |

Replace the role-allowlist assertions in the existing tests with parity and
policy assertions. Run the subagent unit, integration, and end-to-end suites,
the strict docs build, and the repository's CI gates. Keep changed-path
coverage above 90% and the repository-wide branch coverage above 80% while
passing the configured floor.

## Documentation and compatibility

Update the built-in role table, tool-inheritance guidance, and applicable
public API references. Update `AGENTS.md` and `CLAUDE.md` in the
implementation change to reflect the new default. This PRD supersedes the
built-in investigative allowlists specified by SUBAGENT-13 and the
read-only Researcher assertion in SUBAGENT-17 A4. The phase continuation and
collection tests in SUBAGENT-17 apply to the expanded roles.


## Implementation verification

- A1, A2, A3, A4, A7 use provider-boundary tests for catalog and binding parity,
  actual dispatch, explicit restrictions, and durable result reuse.
  [Provider test suite](../../tests/integration/test_subagent_tool_parity.py).
- A2 and A6 also exercise the live catalog and skill-scoped requests after
  tool changes. [Reload tests](../../tests/integration/test_live_tool_surface.py),
  [Skill tests](../../tests/integration/test_use_skill.py).
- A3, A5 and A6 check scoped approvals, Outlook attachments, worktree isolation
  and conflicts, and mode- and depth-aware catalog admission.
  [Outcome tests](../../tests/unit/test_subagent_outcomes.py),
  [Worktree tests](../../tests/unit/test_subagent_worktrees.py),
  [Catalog tests](../../tests/unit/test_subagent_tool_surfaces.py).
