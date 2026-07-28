---
title: "PRD-153: Reliable Agent-Owned Workflow Authoring"
status: In progress
date: 2026-07-28
scope: create_workflow design/execute phase separation and direct file creation
related:
  - PRD-147
  - PRD-152
  - PRD-138
---

# PRD-153 — Reliable Agent-Owned Workflow Authoring

## 1. Summary

`create_workflow` separates planning from implementation:

```text
interpret/planner -> design/planner -> execute/executor -> summarize/auto
```

The read-only `design` phase produces an implementation specification and calls
`complete_design_phase(summary)`. The write-capable `execute` phase consumes
that specification, writes the complete workflow with the canonical
`write_file` tool, and calls `complete_execute_phase(...)`.

The runner remains an orchestrator. It never copies assistant prose, stages,
publishes, parses, or statically validates the generated workflow source.

## 2. Evidence-backed problem statement

The supplied failure transcript showed repeated design attempts (`2/20` through
`5/20`) in which the model said it would write the source but emitted no
`write_file` call. It instead made malformed read calls and eventually tried to
use `batch_write`; it never called `complete_design_phase`.

The previous contract assigned both planning and source writing to a phase whose
role was `planner`. That role resolves to `READ_CAPS`, excluding write
capability. The authoring runner also bypassed the normal workflow phase filter,
so the declared phase contract and actual tool surface were inconsistent.

The canonical `write_file` tool was present in the registry, so the failure was
not simply a missing tool. The phase had the wrong responsibility boundary:
there was no dedicated execute phase for a write-capable agent to receive the
design and implement it. The retry loop also reused the same short-term memory,
allowing interrupted prose and failed reads to compound across attempts.

## 3. Goals

- Make design read-only and transition-only.
- Add an execute phase that owns the agent-side workflow write.
- Require a successful `write_file` result before execute can transition.
- Keep the agent, not the runner, as the owner of the workflow file.
- Preserve bounded retries, including the configured 20-attempt ceiling.
- Prevent capability-excluded tools from being advertised to a phase agent.
- Preserve the existing direct-write/no-staging/no-approval behavior.

## 4. Non-goals

- No runner fallback that copies assistant response text.
- No runner-owned staging, publication, source parsing, static validation, or
  source hashing for `create_workflow`.
- No weakening of `WorkspaceView`, capability, mode, or approval controls.
- No change to the six-phase `create_tools` or `create_commands` lifecycle.

## 5. Functional requirements

### 5.1 Design phase

The design phase must:

- use the `planner` role and read/search tools only;
- inspect the minimum current contracts needed to create a correct design;
- define the stable workflow name, phase graph, complete per-phase prompts,
  tools/MCP services, inputs, outputs, verification, safety boundaries,
  completion signals, handoffs, and activation notes;
- call `complete_design_phase(summary)` as its only accepted handoff; and
- never call `write_file`, `batch_write`, shell, execution, network, or git-write
  tools.

The design summary is passed as the execute phase input through the authoring
context. A prose response alone does not advance the phase.

### 5.2 Execute phase

The execute phase must:

- use a write-capable executor role;
- consume the design summary rather than rediscovering the request;
- generate exactly one complete workflow source file;
- call canonical `write_file` with
  `.agenthicc/workflows/<stable_workflow_name>.py`;
- wait for `ok=true` from `write_file`; and
- call `complete_execute_phase(summary, artifact_name, artifact_description)`.

The execute transition is rejected unless either a successful write receipt is
available or the exact declared workflow file already exists, the path is
inside the project workflow directory, the path matches the stable artifact
name, and the description is non-empty. The existence fallback handles a
provider response that arrives after the filesystem side effect but loses the
tool receipt. This verifies only the handoff metadata and exact file existence;
it does not read, parse, or validate Python source.

### 5.3 Tool visibility and capability boundaries

Authoring turns must support a capability exclusion set applied to both the
model tool schemas and the generated tool description. Design excludes
`WRITE`, `GIT_WRITE`, `EXECUTE`, and `NETWORK`. Execute receives the normal
write-capable surface, still subject to the active mode and approval gates.

If the active mode blocks execute writes, the user receives an actionable
capability error rather than an indistinguishable retry storm.

### 5.4 Recovery

The outer authoring limit remains 20 attempts. Each attempt reports whether it
failed because of a missing transition, failed write, blocked capability, or
invalid handoff. A failed optional read must not cause an indefinite inspection
loop.

The execute phase retains a successful write receipt if the model is
interrupted before calling its transition. The next execute attempt can call
the handoff without rewriting the file. The runner does not read the file to
recover it.

## 6. User journey

```text
/workflow create_workflow
Create a workflow that uses Cloakbrowser to parse facebook.com.
        |
        +-- interpret: normalized intent
        +-- design: implementation specification
        |           complete_design_phase(summary)
        +-- execute: write_file(.agenthicc/workflows/name.py)
        |           complete_execute_phase(...)
        +-- summarize: truthful result and reload instruction
```

After completion, the user runs `/workflows reload` and then
`/workflow <generated_name>`.

## 7. Acceptance criteria

1. `CreateWorkflow.phases` is exactly
   `interpret → design → execute → summarize`.
2. Design uses `planner`, has no write-capable tools in its model-visible
   registry, and advances only through `complete_design_phase(summary)`.
3. Execute uses `executor`, receives the design summary, and exposes canonical
   `write_file`.
4. A successful mocked journey writes exact tool-supplied bytes and records the
   phase history in the four-phase order.
5. Execute cannot transition without a successful write receipt or the exact
   declared workflow file already existing.
6. A path outside `.agenthicc/workflows` or a mismatched artifact name is
   rejected without source inspection.
7. A design prose-only response never creates a file and is retried/fails with
   a structured transition error.
8. A write followed by an interrupted execute handoff can complete on retry
   without runner copying or publishing.
9. Design cannot use write/execute/network/git-write capabilities even though
   built-in tools are normally registered globally.
10. Existing extension-authoring tests and behavior remain unchanged.
11. Documentation and public reference material describe the four-phase
   lifecycle and execute-owned write.

## 8. Test plan

### Unit

- state mapping includes `EXECUTE`;
- phase topology, roles, prompts, and transition names;
- design tool surface excludes write/execute/network/git-write capabilities;
- execute write receipt and path/name validation;
- rejected handoff without receipt;
- capability-exclusion registry description;
- bounded attempt handling.

### Integration/E2E

- full mocked `interpret → design → execute → summarize` journey;
- exact bytes written by the execute agent;
- design attempts to write are rejected by the model-visible tool surface;
- execute failure in restricted runtime mode;
- write success followed by missing/interrupted handoff;
- no runner parser, validator, staging, or publication call;
- headless execution reports all four phases.

## 9. Security, compatibility, and ownership

All writes continue through `WorkspaceView`, canonical `write_file`, capability
metadata, runtime mode checks, and configured approval gates. The executor role
does not bypass the active mode. Diagnostics include paths and statuses only;
source contents, credentials, and secrets are not logged.

The change is backwards-compatible for the `/workflow create_workflow` user
journey and preserves the agent-owned artifact contract. Existing custom
authoring workflows retain their own declared phases. Implementation remains
inside the current boundaries: authoring state/runner, built-in workflow
definition, transition tools, agent-turn tool filtering, tests, and workflow
documentation.

## 10. Verification

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/unit/test_workflow_authoring.py -q
uv run pytest tests/e2e/test_create_workflow_e2e.py -q
uv run pytest tests/ -q
```
