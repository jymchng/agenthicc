---
title: "WFC-04: Subagents that run a workflow"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P1
related_prds: [WFC-00, WFC-01, WFC-02, WFC-11, SUBAGENT-01, SUBAGENT-05, SUBAGENT-12, SUBAGENT-14, SUBAGENT-15, prd-97]
tags: [subagents, workflows, isolation, worktrees]
---

# WFC-04 — Subagents that run a workflow

## 1. Summary

Let an agent spawn a subagent whose job is to run a **named workflow** to
completion and report its typed result. `spawn_subagents` tasks gain `workflow`
and `params`; the pool runs the workflow's runner headlessly in the worker's own
run, worktree and checkpoint namespace, and the parent receives a
`WorkflowReturn` inside the spawn digest.

## 2. Problem (current source)

- `spawn_subagents` accepts `_SpawnTaskInput{type, task, context}`
  (`subagents/tool.py:84`). A worker is a free-form agent shaped by a
  `SubagentTypeSpec` (`subagents/types.py`); it cannot run a phased workflow
  and report its terminal state.
- The machinery to run a workflow without a TUI exists —
  `runners/headless.py: execute_workflow` builds the runner through
  `WorkflowPlugin.build_runner` — but it is bound to a `SessionContext` and a
  single run.
- Parallel mutating workers already isolate in git worktrees (SUBAGENT-15);
  workflows that write code or files need the same isolation when several run
  at once (for example three workers each running `create_tools` for a
  different tool).

## 3. Goals

1. `spawn_subagents(tasks=[{type, task, workflow, params}])` runs that workflow
   inside a worker.
2. The parent gets the workflow's terminal state, bounded summary and
   published artifacts, with evidence the runtime observed (SUBAGENT-14).
3. Effective capabilities are `parent ∩ type ∩ workflow phases`; a workflow
   worker never widens its parent.
4. Authoring workflows run by workers **stage** their artifacts; publication
   stays with the session (WFC-11).
5. No new subagent limits.

## 4. Non-goals

- Human-gated workflows as workers (§5.5).
- Nested `enter_workflow` inside a worker; the worker's whole run is its frame.
- Any new turn, concurrency, depth or time cap on subagents. They run to
  completion under the existing session turn limit (SUBAGENT-12 and the
  removal of subagent limits).

## 5. Design

### 5.1 Task shape

```python
class _SpawnTaskInput(TypedDict):
    type: str
    task: str
    context: str | None
    workflow: str | None                  # new; None = free-form worker as today
    params: dict[str, str] | None         # new; validated by WorkflowPlugin.build_params
```

`type` stays required: it supplies the policy envelope (tool allow-list,
completion evidence, permissions) the workflow runs under. `task` becomes the
workflow's intent. `params` follows the same encoding decision as WFC-02 (a
schema test decides between a mapping and a JSON string).

### 5.2 Execution

A new `WorkflowWorker` path next to the existing agent worker in
`subagents/pool.py`:

1. Resolve the plugin from the **live** registry (so workflows authored this
   session are runnable); check `invocation.subagent_eligible` and
   `invocation.interactive` (§5.5).
2. Build a per-worker `WorkflowConfig` with `dataclasses.replace`: same
   session-owned singletons, a worker-scoped `WorkflowRunHandle` whose run id is
   `sub-<pool_id>-<index>`, its own checkpoint namespace, the worktree as its
   workspace root, and the tool surface current at **spawn** time (WFC-08).
3. Run it through `run_workflow_core`, extracted from
   `headless.execute_workflow` so the TUI-free path has one implementation
   (`runners/workflow_execution.py`). `execute_workflow` becomes a thin caller.
4. Fold the terminal context into a `SubagentResult` carrying
   `workflow_return: WorkflowReturn | None`.

### 5.3 Isolation and staging

Workers that run a workflow with non-empty `invocation.publishes` or any
mutating phase always get a worktree (SUBAGENT-15), including a single worker.
Authoring workflows write inside the worktree's authoring area — the staging
tree for tools and skills, the existing pending-marker package for workflows
(WFC-11 §5.1) — and return `PublishedArtifact(state="staged")`; they never write
the main tree's live extension directories. The parent session, not the worker,
runs the publish step: `store.commit(artifact, source_root=<worktree>)` imports
the files, re-verifies the digest and commits them in the main workspace
(WFC-11), so parallel authors cannot race on `.agenthicc/tools/`.

### 5.4 Result and evidence

The spawn digest adds, per workflow task: workflow name, terminal state, phases
executed (`PhaseRunRecord` list), `summary`, and artifacts. A worker whose
workflow returns `FAILED` or whose evidence requirement is unmet is reported as
failed, never as success (SUBAGENT-12). The digest stays inside the existing
size bounds.

### 5.5 Eligibility and interactive gates

`subagent_eligible: bool` and `interactive: bool` come from
`WorkflowInvocation` (WFC-02). An `interactive` workflow is refused with
`workflow_requires_human_gate` before any worker starts, because a background
worker has no operator to answer a plan-approval overlay or `ask_user`.
Routing such gates to the parent over `subagents/communication.py` is recorded as
a follow-up, not specified here.

### 5.6 Policy narrowing

Effective policy is computed with the existing snapshot machinery
(`subagents/policy.py`, SUBAGENT-05): `parent ∩ type.policy ∩ ⋂ phase.allowed_caps`.
A workflow whose phase needs a capability the intersection lacks fails at
spawn with `ceiling_denied`, not mid-run.

## 6. Requirements

- **FR-1** An unknown, ineligible or interactive workflow fails the **task**
  with a typed code; other tasks in the same call still run.
- **FR-2** A worker never calls `enter_workflow` and never receives it
  (WFC-02 FR-4); it may call `spawn_subagents` under the existing rules.
- **FR-3** Worker checkpoints live in a namespace that `/workflow resume`
  does not offer as a foreground run; they are visible in the subagent
  transcript and journal only.
- **FR-4** Cancelling the parent cancels workflow workers through the existing
  pool cancellation path and leaves staged output in place with state `staged`.
- **FR-5** The worktree is retained when artifacts are staged and removed
  otherwise, using the existing SUBAGENT-15 lifecycle.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | `spawn_subagents` with `workflow="create_tools"` runs the workflow headlessly and returns a `WorkflowReturn` whose artifacts are `staged`. |
| A2 | Two parallel `create_tools` workers produce disjoint staged sets and neither writes `.agenthicc/tools/` in the main tree. |
| A3 | An `interactive` workflow (`code_plan`) is refused with `workflow_requires_human_gate`; a sibling free-form task in the same call completes. |
| A4 | A worker whose workflow fails is reported failed with the workflow's fail reason and phase list. |
| A5 | A `Plan`-mode parent cannot spawn a workflow with a write-capable phase. |
| A6 | `grep` of `subagents/` shows no new numeric cap, timeout default or turn limit introduced by this change. |
| A7 | `headless.execute_workflow` and the worker path both call `run_workflow_core`; its body exists once. |

## 8. Test plan

- Unit: task validation, eligibility table, policy intersection
  (`tests/unit/test_subagent_workflow_task.py`).
- Integration: real pool with a scripted transport and a stub two-phase
  workflow; parallel isolation, cancellation, failure reporting
  (`tests/integration/test_subagent_workflow_worker.py`).
- E2E: WFC-13 S2.

## 9. Documentation and cleanup

`docs/guides/subagents.md`, `docs/guides/parallel-workers.md`,
`skills/subagents/`, `llms-full.txt` (`WorkflowWorker`-level public symbols,
`run_workflow_core`, the new task fields). The duplicated TUI-free run logic in
`headless.execute_workflow` is deleted in favour of the shared core.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Workflow assumptions about a live TUI (overlays, input). | `interactive` flag plus a headless smoke test for every `subagent_eligible` built-in. |
| Staged output left behind by crashed workers. | Staging is inside the worktree lifecycle and reaped with it; publish removes consumed staging. |
| Digest size growth. | Existing digest bounds; summaries truncated with a marker. |
