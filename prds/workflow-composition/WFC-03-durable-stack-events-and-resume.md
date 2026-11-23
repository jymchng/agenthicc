---
title: "WFC-03: A durable workflow stack — kernel events, checkpoint links, and exactly-once return"
status: Implemented
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P1
related_prds: [WFC-00, WFC-01, WFC-02, prd-94, prd-98, prd-170, prd-184, prd-213]
tags: [workflows, kernel, events, checkpoints, resume, replay]
---

# WFC-03 — A durable workflow stack

## 1. Summary

WFC-01 keeps the stack in memory. This PRD makes it a durable, replayable fact:
kernel events record each push and return, each workflow checkpoint records its
parent link, resume rebuilds the chain after a restart, and a return packet is
delivered **exactly once** across a crash between pop and delivery.

## 2. Problem (current source)

- Workflow lifecycle is durable per run (`workflows/checkpoint.py`,
  `runners/workflow_checkpoint_store.py`, `runners/workflow_recovery.py`,
  PRD-170) but a run knows nothing about a caller. After a restart,
  `/workflow resume` can continue a child but cannot tell that a parent is
  waiting for it.
- The kernel log has no workflow-frame event. CLAUDE.md requires durable domain
  facts to flow through `root_reducer`; today the active workflow is purely
  presentation state (`conversation.workflow_override`), so replay cannot show
  which workflow an agent was in.
- The `turn_queued` event used for resume continuation carries no idempotency
  key, so redelivery after a crash would duplicate the parent's resume turn.

## 3. Goals

1. Pushes, returns and abandonments are kernel events with pure reducer
   handlers and a frozen `AppState` projection.
2. A checkpoint carries `frame_id`, `parent_run_id` and `origin`; recovery
   rebuilds the stack from them.
3. Return delivery is exactly-once as observed by the parent's conversation.
4. The presentation stack is derived from, and bridged to, the kernel state in
   one tested place.

## 4. Non-goals

- Changing checkpoint topology or phase cursors (PRD-170/184/213 own those).
- Persisting subagent workflows' internals (WFC-04 uses its own run ids and the
  existing subagent journal).

## 5. Design

### 5.1 Events and reducer

Added to `kernel/events.py`, handled in `kernel/reducer.py`, registered in
`_HANDLERS`, following "Adding an event" in CLAUDE.md.

| Event | Payload | Reducer effect |
|---|---|---|
| `workflow_frame_pushed` | `frame_id, parent_frame_id, workflow_name, run_id, origin, depth, intent_digest` | append a `WorkflowFrameRecord(status="running")`; parent → `suspended` |
| `workflow_frame_returned` | `frame_id, status, summary_digest, artifact_names` | frame → terminal status; parent → `running` |
| `workflow_return_delivered` | `return_key` | mark the return delivered |

Payloads store **digests** of intent and summary, not the text, so the kernel
log stays bounded and free of secrets; the full text lives in the checkpoint and
the conversation. `AppState.workflow_frames: tuple[WorkflowFrameRecord, ...]`
is frozen domain state changed only through these handlers. The reducer is pure
and contains no I/O.

### 5.2 Bridge

`runners/tui_session.py` owns the one bridge: it emits the events when
`WorkflowStack` transitions, and a subscriber projects
`AppState.workflow_frames` into the reactive `conversation.workflow_stack`
signal. No other module writes the signal. The bridge is documented in the
module docstring and covered by an integration test, as CLAUDE.md requires.

### 5.3 Checkpoint links

`WorkflowCheckpoint` gains required fields `frame_id: str`,
`parent_run_id: str | None`, `origin: FrameOrigin` and bumps `schema_version`.
A checkpoint without them fails validation through the existing
`CheckpointValidationError` path with an actionable message
(`start a new run; this checkpoint predates workflow stacks`). No default-field
shim.

### 5.4 Recovery

`WorkflowRecoveryCoordinator` (`runners/workflow_recovery.py`) rebuilds the
chain at session start:

1. Collect recoverable and paused records; group by `parent_run_id`.
2. Reconstruct frames bottom-up from the base. A missing parent record turns
   the child into an orphan: it is offered as a base frame with a notice
   (`parent run <id> was not found`).
3. A cycle in links, or a depth over `max_stack_depth`, marks every record in
   the cycle `diagnostic` and refuses resume with the existing
   `workflow_recovery_errors` surface.
4. `/workflow resume [run-id]` on a child restores the whole chain; the parent
   stays `suspended` until the child pops.

### 5.5 Exactly-once return

The parent-resume turn carries `return_key = return:<parent_frame_id>:<child_run_id>`.
Delivery order:

1. `workflow_frame_returned` (durable) → 2. enqueue the synthetic turn with the
   key → 3. when the parent's turn *starts*, emit `workflow_return_delivered`.

On restart, a returned frame with no matching `workflow_return_delivered` is
re-enqueued with the same key; the turn queue and the conversation journal drop
a key they have already seen. The window is at-least-once enqueue with
idempotent consumption.

## 6. Requirements

- **FR-1** Every `WorkflowStack` transition emits exactly one event; none are
  emitted from the reducer.
- **FR-2** `AppState.workflow_frames` is reconstructible from the event log
  alone (replay test).
- **FR-3** The summary and intent digests use a stable hash (SHA-256, first 16
  hex characters) documented in `docs/guides/architecture.md` beside the event
  table.
- **FR-4** Recovery never starts a run; it only restores frames and offers
  `/workflow resume`.
- **FR-5** `llms-full.txt` gains entries for each new public symbol; the
  architecture and storage docs describe the new events.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | Replaying the event log of a session with two nested frames yields identical `workflow_frames` to the live session. |
| A2 | Killing the process after `workflow_frame_returned` and before delivery, then restarting, produces exactly one parent resume turn. |
| A3 | Resuming a paused child after restart restores its parent as `suspended`; completing the child delivers the return and resumes the parent. |
| A4 | A checkpoint missing parent fields is rejected with the documented message and never half-loaded. |
| A5 | An orphaned child is offered as a base frame with a notice; a link cycle is refused. |
| A6 | The new reducer handlers pass the purity checks already applied to `_HANDLERS`. |

## 8. Test plan

- Unit: reducer handlers, digest stability, recovery grouping and cycle
  detection (`tests/unit/test_workflow_frame_reducer.py`,
  `tests/unit/test_workflow_recovery_chain.py`).
- Integration: processor + store crash-injection test at each of the three
  delivery steps (`tests/integration/test_workflow_return_exactly_once.py`).
- E2E: WFC-13 S7.

## 9. Documentation and cleanup

`docs/guides/architecture.md` (event table), `docs/guides/workflows.md` (resume
semantics for nested runs), `docs/guides/session-service.md`, `llms-full.txt`.
PRD-170 and PRD-213 gain a pointer to this PRD.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Event-log growth. | Three small, digest-only events per frame. |
| Schema bump strands old paused runs. | Deliberate; message tells the operator to start a new run; the project is pre-release. |
| Two writers for the presentation signal. | Single bridge, enforced by a test that greps for other writers. |

## Amendment — 2026-11-22

The bridge records `workflow_frame_state_changed` for the phase and lifecycle
of a frame that stays on the stack. This lets the existing footer follow phase
progress between a push and a return. The event carries identity and phase
metadata. When a return is accepted, the conversation journal uses the same
key for the turn and its synthetic input. This covers a stop after the turn
start is durable and before the input reaches the provider.

## Implementation status

| Criterion | Evidence |
| --- | --- |
| A1 | `test_workflow_frame_reducer_replays_nested_returns_without_side_effects` compares a nested projection before and after replay. `test_restart_delivers_one_parent_turn_after_durable_return` replays the persisted kernel log in the reopened process. |
| A2 | `test_restart_delivers_one_parent_turn_after_durable_return` terminates the process at the packet, event, and journal-start boundaries. Each reopened session sends one provider request and keeps one journal start marker. |
| A3 | `test_restart_restores_a_paused_child_and_resumes_its_suspended_caller` reopens the session, checks both frames in the TUI projection, and submits the child's resume command before observing the caller request. |
| A4 | `test_checkpoint_predating_stack_is_rejected_before_recovery` checks schema 1 and each missing stack field. |
| A5 | `test_orphan_is_offered_and_link_cycles_and_excess_depth_are_diagnostic` checks missing links, cycles, and the configured depth. |
| A6 | `test_workflow_frame_reducer_replays_nested_returns_without_side_effects` checks the original state and payloads and confirms that the reducers return no effects. |
