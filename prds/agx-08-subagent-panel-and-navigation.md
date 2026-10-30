---
title: "AGX-08: Subagent panel and child-session navigation"
status: Implemented (phases 1 and 2)
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-05, agx-07, agx-10, agx-13, prd-124, prd-139, prd-197, prd-214, prd-232]
tags: [subagents, tui, ux, approvals, transcripts]
---

# AGX-08 — Subagent panel and child-session navigation

## Implementation status (2026-10-30)

Phases 1 and 2 are implemented in the integration branch. Phase 2 persists a
pool's identity before workers start and the exact backgrounded tool receipt
before returning control. The session supervises the worker task; graceful close
records cancellation, and resume marks unfinished pools/workers interrupted
without rerunning them. A completed, interrupted, or cancelled background
aggregate is appended at the next non-resume turn boundary using a stable
message ID and a durable delivery receipt, without changing the parent
tool-result boundary.

Evidence: `test_ctrl_b_backgrounds_a_pool_and_keeps_it_session_owned`,
`test_session_shutdown_cancels_a_backgrounded_pool`,
`test_background_pool_recovery_is_durable_and_does_not_rerun`, and
`test_completed_background_subagent_is_delivered_once`. Focused verification on
2026-10-30 passed 128 tests across the subagent, journal, keymap, and TUI session
unit/integration files.

- **Panel (§5.1).** `SubagentPool` publishes an immutable `SubagentPoolState`
  (`WorkerState` per worker: status, activity, start and finish times, tokens)
  for every change; the old code mutated one object and re-published it, which
  `Signal.set` ignored as unchanged. `tui/workspace/subagent_panel.py` renders
  the summary and up to six rows between the prompt and the footer, with
  `… +N more`, finished rows for 30 s, failures until dismissed and the summary
  alone below 24 terminal rows; screen-reader mode uses sentences. The footer's
  one-line worker grid is gone.
- **Focus and navigation (§5.2).** Ctrl+O (`subagents.focus`, only while the
  panel shows) and `/subagents` open `SubagentsOverlay` — the current pool's
  workers, then the session's earlier pools read off the event loop from their
  transcripts. Enter opens `SubagentTranscriptOverlay`: ←/→ siblings, `f`
  follow, Esc or ↑ at the top back. Keys are the `subagents` and `transcript`
  key-map contexts. In a session, the agents hub's Running tab lists the
  turn's workers and Enter opens a transcript (the AGX-05 item deferred here).
- **Transcripts (§5.3).** `agenthicc.subagents.transcripts`: `pool.json` and
  `<worker_id>.jsonl` under `<session dir>/subagents/<pool_id>/`, written in
  order on one background thread, redacted, compacted under 2 MiB behind a
  `truncated` marker that counts what it dropped. Events: prompt, text (each
  assistant message, through an observing worker memory), tool_started,
  tool_finished, approval_requested/resolved, finished.
- **Stopping one worker (§5.4).** `SubagentPool.stop_worker` cancels the
  worker's task (or keeps a waiting worker from starting); its result is
  `stopped by user` and aggregation goes on. `active_pool(pool_id)` finds the
  running pool; `x` in the panel or a transcript calls it.
- **Approvals (§5.5).** `ApprovalRequest.requester: ApprovalRequester`. A
  worker's approvals pass through a wrapper that fills it and marks the row as
  waiting — covering both capability approvals and workspace-access approvals.
  The prompt names the worker, `o` opens its transcript and returns to the
  prompt, Esc denies only that call; the background inbox target stores the
  requester and the hub's card shows it.

Fixed along the way: a parent's answer to a worker's clarification question
that arrived before the worker checked was lost (`AgentMessageBroker.open_questions`
now includes answered-but-undelivered questions).

Acceptance: (1) `test_each_row_shows_its_workers_tool_before_the_tool_runs`
— each row changes when the tool call starts, before its body runs; (2)
`test_transcripts_outlive_the_turn_and_hide_secrets` reads a finished pool from
disk as a resumed session would; (3) `test_stopping_one_worker_leaves_the_others_running`;
(4) `test_a_workers_approval_names_it_and_waits_on_its_row` and
`test_a_workers_approval_names_it_and_o_opens_its_transcript`; (5)
`test_provider_requests_and_the_result_are_the_same_without_the_panel`
compares every provider request and the aggregate with and without the panel
and transcripts.

## 1. Summary

When a turn fans out to subagents, the TUI prints a spawn list, one line per
finished worker and a summary, plus an `N/M subagents` counter. While workers
run you cannot see what any of them is doing, open one, stop one, or tell which
worker an approval prompt comes from — worker tool calls are not persisted and
approval prompts carry no requester. Add a live subagent panel under the
prompt, navigable child transcripts, per-worker stop, and approvals that name
their requester; then allow backgrounding a running fan-out.

## 2. Problem and evidence

- `SubagentPool` (`src/agenthicc/subagents/pool.py`) keeps "tool calls and tool
  output … private to the worker"; only each worker's final result and the
  aggregate are journaled. A finished worker's work cannot be inspected
  afterwards either.
- The TUI renders `subagent_pool_started`, `subagent_worker_done` and
  `subagent_pool_done` as scroll-back lines
  (`src/agenthicc/tui/workspace/appender.py:942–1005`) and a counter in the
  status bar (`src/agenthicc/tui/workspace/components.py:243`). There is no
  per-worker activity while running.
- Workers install `ApprovalGate(app_state, approval_svc, workspace_access)`
  (`pool.py:611`) with no requester identity, so a worker's approval prompt is
  indistinguishable from the parent's.
- There is no way to stop one worker or to keep chatting while a long fan-out
  runs: the parent turn waits for every worker.

## 3. How others handle subagents

| Product | Visibility | Navigation | Control | Approvals |
|---|---|---|---|---|
| Claude Code | Panel below the prompt; nested subagents as a tree with `(+N)`; finished rows removed after 30 s, failed rows kept 30 s | `/tasks` → Enter opens a transcript; type into it to resume | `x` stops; Ctrl+B backgrounds; results return as a completion notification in a later turn | Prompt names the subagent; Esc denies that one call without stopping it |
| OpenCode | Each subagent runs in a child session | `session_child_first` (Leader+Down), `session_child_cycle` (Right/Left), `session_parent` (Up) | Subagent tool runs foreground or background | — |
| Codex | `/subagents` | Switch between threads while they run | Ask Codex to stop or steer | Approval overlay shows the source thread; `o` opens it |
| Grok Build | "Waiting for N subagents…"; tasks pane lists subagents (*third-party*) | — | Background runs have a stop control in the tasks pane | — |

## 4. Goals and non-goals

**Goals**

1. Every running worker is visible with its current activity, elapsed time and
   tokens.
2. Any worker — running or finished — can be opened to a transcript, and
   siblings and parent are one key away.
3. One worker can be stopped without cancelling the others or the turn.
4. Approval prompts name the requesting worker and can open its transcript
   first.
5. Phase 2: a running fan-out can be sent to the background while the user
   keeps working.

**Non-goals**

- Changing the result boundary: the parent still receives only final results
  and the aggregate (PRD-214 governs hand-off content).
- Letting a worker exceed the parent's policy (PRD-197's inheritance stays).

## 5. Design

### 5.1 Panel

Rendered in the live block between the composer and the footer while a pool is
active:

```text
 Subagents 3 running · 1 done · limit 4                                   Ctrl+O focus
 ● explorer-1   read  src/agenthicc/background/store.py                 0:42   3.1k
 ● explorer-2   grep  "refresh_interval_s" src/                         0:40   2.4k
 ◆ worker-1     needs approval: bash pytest -k store                    1:05   5.8k
 ✓ reviewer-1   done · 3 findings                                       0:31   1.9k
```

- One row per worker: status glyph, label, current tool and target (or the
  latest text), elapsed, tokens.
- Nested pools render as a tree with a `(+N)` count when collapsed.
- Finished rows stay 30 s, then fold into the summary line; failed rows stay
  until dismissed with `x`.
- At most 6 rows; more collapse into `… +N more` and remain reachable through
  the hub's Running tab (AGX-05).

### 5.2 Focus and child navigation

| Key (default, AGX-13 configurable) | Action |
|---|---|
| Ctrl+O | Move focus from the composer to the panel (Esc returns) |
| ↑ / ↓ | Select a worker |
| Enter | Open the worker's transcript overlay |
| ← / → (in a transcript) | Previous / next sibling (OpenCode's `session_child_cycle`) |
| Esc or ↑ at the top (in a transcript) | Back to the parent conversation (`session_parent`) |
| `x` | Stop the selected worker |
| `f` | Toggle follow in the transcript |

The transcript overlay shows the worker's prompt, text, and tool calls as one
line each with status and a short result, redacted like AGX-07's transcript.

### 5.3 Per-worker transcripts

Each worker appends to
`<session artifacts>/subagents/<pool_id>/<worker_id>.jsonl`:

- events: `text`, `tool_started` (tool, redacted target), `tool_finished`
  (status, short summary), `approval_requested`, `finished` (result summary);
- bounded to 2 MiB per worker (older events dropped with a marker), retained
  and deleted with the session;
- written off the event loop and never read by the parent's provider context,
  so the result boundary of PRD-124 is unchanged.

### 5.4 Stopping one worker

`x` cancels that worker's task. The pool records a `cancelled` result for it
(`subagent_worker_done` with `ok=false`, error `stopped by user`), aggregation
proceeds with the remaining results, and the parent turn continues.

### 5.5 Approvals name the requester

`ApprovalGate` gains a `requester: ApprovalRequester | None` (worker label,
type, pool and worker IDs). The approval overlay shows
`worker-1 (executor) wants to run bash: pytest -k store`, offers `o` to open the
worker's transcript before deciding, and Esc denies that single call without
stopping the worker. The inbox item (PRD-227) stores the requester so
background sessions show the same in the hub (AGX-07, AGX-10).

### 5.6 Phase 2 — background a fan-out

Ctrl+B while a pool runs returns control to the composer:

1. The `spawn_subagents` tool call completes with a durable "running in the
   background" result carrying the pool ID (journaled, so replay is
   deterministic).
2. Workers keep running under the session; the panel and hub keep showing them.
3. When the pool finishes, its aggregate is journaled and delivered as a
   notification message at the next turn boundary, which the model sees as a new
   user-visible message ("Background subagents finished: …").
4. Cancelling the session cancels the pool; resume after a crash marks unfinished
   workers `interrupted` rather than re-running them.

Phase 2 changes the turn model and needs its own replay-safety review before
implementation.

## 6. Budgets and acceptance criteria

| Measure | Budget |
|---|---:|
| Worker event → panel updated | p95 ≤ 100 ms |
| Open a worker transcript | p95 ≤ 50 ms |
| Per-worker transcript size | ≤ 2 MiB |

1. During a 4-worker fan-out, each row shows the worker's current tool within
   100 ms of the tool starting.
2. A finished worker's transcript can be opened after the turn ends and after a
   session resume.
3. Stopping one worker leaves the others running and the turn completes with
   the remaining results.
4. An approval requested by a worker names it; `o` opens that worker's
   transcript; Esc denies only that call.
5. The parent's provider messages are byte-identical with and without the panel
   and transcripts enabled (replay test).

## 7. Test plan

- Pool tests with fake workers emitting tool events; panel golden renders.
- Transcript persistence, bounding and redaction tests.
- Approval requester propagation through `ApprovalGate`, the overlay and the
  inbox.
- Cassette replay equivalence for the parent conversation.

## 8. Risks

- **Live-block height.** The panel competes with the composer on small
  terminals. Mitigation: collapse to the one-line summary below 24 rows.
- **Phase 2 semantics.** Delivering results in a later turn changes what the
  model sees and when. Mitigation: ship phase 1 independently; gate phase 2 on
  its own review.

## Sources

- Claude Code subagents: <https://code.claude.com/docs/en/sub-agents>
- OpenCode agents (child sessions): <https://opencode.ai/docs/agents/>
- Codex subagents: <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Grok Build background tasks: <https://docs.x.ai/build/features/background-tasks>
- Grok Build cheat sheet (*third-party*): <https://www.scriptbyai.com/grok-build-cheat-sheet/>
