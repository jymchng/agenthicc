---
title: "COMM-01: Move a running foreground command to the background"
status: Implemented on feat/ctrl-b-background-foreground-tool (not yet merged)
version: 1.0.0
date: 2026-11-27
priority: P0
related_prds:
  - COMM-00
  - prd-149
  - prd-151
  - SUBAGENT-19
---

# COMM-01 — Move a running foreground command to the background

## Problem

A turn that runs `run_bash` or `run_command` waits for the process. When the
command is long (`timeout 2400 bun /tmp/probe3-guest.mjs 2>&1 | tail -80`
had run 39 minutes), the user had two choices: wait, or cancel and lose the
work. Ctrl+B already moved a foreground *subagent pool* to the background, so
the key was known and did nothing for a command.

## Current behaviour (before this change)

- `_run_proc` in `tools/exec/__init__.py` spawned its own pipe-attached process
  and drained it. `TerminalManager.start` spawned a *different*, managed
  process. There was no way to hand a running process from the first to the
  second, so backgrounding after the fact would have meant restarting it.
- The TUI did not know a command was running: no state said "a tool call waits
  on a process".
- The key was the action `subagents.background`, enabled only by the condition
  `subagents_foreground`.

## Decisions

1. **Hand over the live process.** The tool call stops its own readers and the
   manager adopts the same process (`TerminalManager.adopt`): same pid, same
   pipes, nothing restarted or signalled. Output already read seeds the
   terminal record, so the first thing `inspect_terminal` shows is what the user
   saw.
2. **Backgrounding does not buy time.** The terminal keeps what is left of the
   command's own `timeout`. A command that would have been stopped at 600 s is
   still stopped at 600 s of running time. Extending a deadline is a decision
   for the agent or the operator, not a side effect of a key press.
3. **Refusal changes nothing.** With no free terminal (`max_terminals`) the
   request is withdrawn, the command keeps waiting in the foreground, and the
   user is told why. Pressing again after a terminal ends works.
4. **One action, one key.** `work.background` replaces `subagents.background`
   and moves every foreground command and the foreground pool together, so a
   user never has to know which kind of work is blocking the turn.
5. **A worker's commands are not offered.** A subagent worker goes to the
   background with its pool, and a detached pool's worker must never be moved
   by a key press made during another turn. `SubagentWorker.run` withholds its
   commands through `bind_foreground_offer(False)`, next to the two
   `bind_*(None)` calls that already clear turn-scoped context there.
6. **The receipt names the handle and forbids a re-run.** The model is told the
   command is still running as `term-…`, to use `wait_terminal`,
   `inspect_terminal` or `stop_terminal`, and not to run it again.

## What shipped

| Piece | Where |
|---|---|
| `ForegroundCommand`, `begin_foreground`, `end_foreground`, `request_background_foreground`, `background_blocker`, `adopt` | `background/terminals.py` |
| The wait loop that races completion, the output limit, the deadline and a request; `hand_over` | `tools/exec/__init__.py` (`_run_proc`, `_run_foreground`) |
| `foreground_commands` signal and `backgroundable()` | `tui/conversation_store.py` |
| `work.background`, `BackgroundWorkCommand`, `BackgroundWorkCapability`, the `backgroundable` condition | `tui/actions.py`, `tui/runtime/commands.py`, `tui/input/capabilities.py`, `tui/keymap_phrases.py` |
| `_background_work_command`, `_project_foreground_commands` | `runners/tui_session.py` |

## Acceptance criteria

1. While `run_bash` or `run_command` runs, `ForegroundCommand` is registered and
   the footer offers Ctrl+B; when it ends, is cancelled or is moved, it is not.
2. A request makes the tool call return a receipt with `backgrounded: true` and
   a `terminal_id`, and the same process finishes as that terminal with all its
   output, once.
3. The terminal's remaining deadline never exceeds what the foreground command
   had left.
4. With no free terminal the command completes in the foreground with its normal
   result, and the user sees the limit that blocked it.
5. A worker's command, and a command run inside `bind_foreground_offer(False)`,
   is never registered and a request reaches nothing.
6. Mutating the wait loop so a request is ignored, or removing the worker
   binding, fails a test.

Covered by `tests/unit/test_foreground_command_backgrounding.py` (real
subprocesses, synchronised on a flag file rather than a sleep) and the updated
keymap and notice tests.

## Known limits, deliberately left

- `run_tests` and `run_python` / `run_python_expr` are not movable. `run_tests`
  post-processes the finished result (JUnit summary, interpreter metadata) and a
  receipt would flow into logic that expects a finished process. They need their
  own handling before they can be offered.
- Completion is not pushed to the agent; it must poll. That is COMM-02.
- A session attached to a hosted runtime (SUBAGENT-19) runs its tool calls in
  the runtime process, and this change registers a command with *that*
  process's manager. Nothing carries the attached terminal's key press there, so
  Ctrl+B is expected to have nothing to move in an attached session. This is
  read from the process split, not exercised; the follow-up must carry the
  request over the controller protocol and test it.
- Inside tmux, whose default prefix is also Ctrl+B, the key must be pressed
  twice. The binding is rebindable; no default was changed for this.
- The key moves all foreground commands. Choosing one of several concurrent
  commands is not offered.
