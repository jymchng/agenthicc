---
title: "COMM-02: Tell the agent when a background terminal finishes"
status: Implemented
version: 1.1.0
date: 2026-11-27
priority: P0
related_prds:
  - COMM-00
  - COMM-01
  - prd-149
  - SUBAGENT-19
---

# COMM-02 — Tell the agent when a background terminal finishes

## Problem

Moving a command to the background (COMM-01) or starting one with
`background: true` returns a handle. Nothing tells the agent when the process
ends. The agent learns the outcome only if its model decides to call
`wait_terminal` or `inspect_terminal`, which it may not do, or may do after the
user has been waiting a long time. A finished worker pool, by contrast, is
pushed to the parent as a durable notice and can wake an idle parent
(`subagents/delivery.py`, SUBAGENT-19).

## Evidence

- `PendingNotice.kind` is `Literal["worker", "question", "message"]`; there is no
  terminal kind (`subagents/delivery.py`).
- `JournalNoticeSource` reads pool records from the conversation journal. A
  terminal's lifecycle lives in `TerminalStore`
  (`background/terminals.py`), not in that journal.
- Searching `runners/agent_turn.py`, `subagents/delivery.py`,
  `runners/agent_turn_context.py` and `subagents/communication.py` for
  `TerminalState`, `terminal_manager` and `list_records` finds nothing: the
  notice path never sees a terminal.
- The TUI does see terminals (`_sync_terminal_status`), but only to draw rows.

## Goals

1. When a terminal that a tool call created, or took over, reaches a final state,
   the agent that owns that conversation is told once, at a safe boundary.
2. The notice carries the outcome the agent needs to continue: terminal id, final
   state, return code, how it ended (`exited`, `failed`, `timed_out`, `stopped`),
   the elapsed time, and the tail of its output, bounded.
3. An idle parent is woken the way a finished worker wakes it, so a long
   backgrounded build resumes the work without the user typing "is it done?".
4. A service (`lifecycle: "service"`) is not a "finished" event while it runs; a
   readiness change and an unexpected exit are the notices that matter.

## Non-goals

- Pushing every line of output. Output is read with `inspect_terminal`.
- Notifying about terminals the agent did not start (a user's own shell).
- Replacing `wait_terminal`, which remains the way to block on a terminal.

## Design

1. **A terminal notice kind.** Extend `PendingNotice.kind` with `terminal`, and
   give the notice a stable `message_id` of `terminal:<terminal_id>:<final
   state>` so redelivery is idempotent, as worker notices are.
2. **A source that reads terminals.** `TerminalManager` already notifies on every
   change (`changed`). A `TerminalNoticeSource`, owned by the session, claims the
   records that reached a final state since they were last delivered. Delivery
   is recorded durably (the way `subagent_worker_notification_delivered` is), so
   a restart neither loses nor repeats a notice.
3. **Same delivery boundary.** `deliver_notices` already hands notices to the
   turn at a tool boundary or the next logical-turn boundary; terminals join that
   path and do not add a second one.
4. **Wake.** An idle session with an undelivered terminal notice starts a turn,
   under the same conditions and limits as a finished worker. At least the
   paused-wake state (`subagent_wake_paused`) holds it back; the design step
   must list the rest by reading the wake path, not by assumption.
5. **Framing.** The notice is runtime-authored, but the output tail came from a
   process, so it is marked untrusted like worker text.
6. **Suppression.** A terminal the agent is already blocked on with
   `wait_terminal` is not announced again when it ends.

## Failure handling

- A terminal that is orphaned or whose cleanup is unproven is announced with that
  state, not as success.
- A notice whose session is gone stays in the durable record and is delivered on
  resume, like a worker notice.
- A flood is bounded: at most the configured number of terminal notices per
  boundary, with a count of the rest.

## Acceptance criteria

1. A `background: true` command that exits produces exactly one notice, and the
   next provider turn receives it.
2. A command moved by Ctrl+B (COMM-01) produces one notice when it ends, naming
   the terminal id from its receipt.
3. After a restart between the end of the process and delivery, the notice is
   delivered once.
4. An idle session starts a turn for the notice; a paused wake does not.
5. A command the agent waits on with `wait_terminal` produces no extra notice.
6. Killing a terminal with `stop_terminal` produces a `stopped` notice, not a
   failure.
7. The tail in a notice never exceeds its limit, and secrets that
   `redact_text` redacts in the record are redacted in the notice.

## Verification

Unit tests for the source and the idempotent id; an integration test with a real
subprocess and the real delivery path; an e2e cassette where a model starts a
command in the background, the turn ends, the command finishes, and the next turn
sees the notice without a tool call.

## What shipped (1.1.0)

| Piece | Where |
|---|---|
| `PendingNotice.kind == "terminal"`, a derived `source` of `terminal_notice`, `terminal_notice(record)` | `subagents/delivery.py` |
| `RUNTIME_NOTICE_SOURCES` replaces `SUBAGENT_NOTICE_SOURCES` and lists `terminal_notice`; its four consumers (session title, prompt history, the scroll-buffer renderer) updated | `subagents/delivery.py`, `tui/conversation_store.py`, `tui/runtime/session_log.py`, `tui/workspace/appender.py` |
| `JournalNoticeSource(terminals=…)` carries the notices for the unscoped source only | `subagents/delivery.py` |
| `TerminalManager.unannounced_finished()`, `observe()`, `started_at` | `background/terminals.py` |
| `wait_terminal`, `inspect_terminal`, `stop_terminal` mark what they return as read | `background/terminals.py`, `tools/exec/__init__.py` |
| `_hint_finished_terminals`: one wake per terminal | `runners/tui_session.py` |
| A headless turn boundary delivers them too | `subagents/delivery.py` (`deliver_session_background_notices`) |
| **What the operator reads.** The notice event carries the terminal's final `state`. The agents hub transcript shows it as a `terminals` row, `agent notified · Terminal <how it ended> — <label> · <id>`, marked `✓` (exited), `✗` (failed, timed out, orphaned) or `●` (stopped, cancelled); the output tail stays in the model's notice. Before this, the hub's check was a `subagent_` prefix, so the notice would have read as the operator's own message (`you`) | `subagents/delivery.py`, `tui/workspace/agents_hub/detail.py`, `tui/workspace/agents_hub/render.py` |

Decisions where the shipped code differs from the proposal above:

1. **No new journal record and no `TerminalNoticeSource` class.** The delivered
   marker is the notice's message id in the journal, exactly as for worker
   questions and updates (`journal.has_message_id`), so a notice survives a
   restart without a second store to keep consistent with the terminal store.
   This removes the risk of two records disagreeing after a crash.
2. **"Finished" is relative to the manager.** A terminal is news only if it ended
   after this manager started, which also counts a terminal recovered as an
   orphan at start-up. Without that rule, resuming a session would announce every
   terminal it ever ran.
3. **The cap has no "count of the rest".** At most `MAX_TERMINAL_NOTICES` (8) are
   claimed at a boundary and the remainder follow at the next. A summary line was
   dropped: with a cap of eight and delivery at every boundary, the backlog
   clears within a turn or two and the count added a second code path.
4. **Wake conditions were read, not assumed.** `_schedule_subagent_wake` holds a
   wake back on: a resume pending, a paused wake, a loop that is not idle, a
   boundary not ready, a workflow stack not running, a workflow paused or
   pausing, and open workflow recovery records. Terminal notices inherit all of
   them.
5. **No minimum runtime.** Every ended terminal is announced. A short command the
   agent started with `background: true` and then read is not announced (it was
   read); one it never reads is. A configurable minimum was not added without
   evidence that the noise is real.

Tests: `tests/unit/test_terminal_completion_notices.py` (real subprocesses and a
real journal), with mutations checked: removing either `observe` in `wait()`,
the journal guard, or the session hint each fails a test.

## Risks

- Waking a session for every short background command is noisy. Mitigation if it
  proves so: a minimum runtime, or a per-turn cap, both configurable; decided
  with the open question in COMM-00.
- The journal and the terminal store are separate records. The delivered marker
  must live where the notice source reads it, or the two will disagree after a
  crash.
