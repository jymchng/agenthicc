---
title: "PRD-244: Fast Live-Session Attach with On-Demand Transcript History"
status: Implemented
version: 1.0.0
date: 2026-10-17
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-67, prd-129, prd-156, prd-164, prd-215, prd-217]
tags: [tui, sessions, attach, transcript, startup-performance]
---

# PRD-244 — Fast Live-Session Attach with On-Demand Transcript History

## 1. Executive summary

Attaching to a live or previously saved session can leave the TUI showing
`Loading transcript…` for a long time. The current code already reads a
bounded tail by default (20 complete turns), but it then synchronously renders
every event in that selection before the input loop starts. A single turn can
contain thousands of events, and the loader's 4,096-event safety cap is still
large enough to make attachment visibly slow. The transcript is also tail-read
again while the input editor rebuilds its user-message history.

This PRD proposes making attach latency independent of the amount of saved
history: show the live workspace promptly, render a strictly bounded recent
transcript preview, and make older history available through a paginated,
on-demand transcript view. Once session ownership and runtime setup are ready,
the user must be able to interact without waiting for the entire selected log
to be formatted and printed.

The durable conversation log and provider conversation memory remain complete.
This is a presentation/startup optimization only; it must not truncate model
context, alter workflow checkpoints, re-run old turns, or write replayed events
back into the log.

## 2. Evidence and current behavior

The following behavior is confirmed in the current source tree:

1. `attach_background_session()` in
   `src/agenthicc/cli/commands/background.py` and the goal-run attach route
   converge on `_run_tui_session(resume_id=...)`.
2. `SessionEventLog.load(..., last_turns=N)` in
   `src/agenthicc/tui/runtime/session_log.py` reads backward from the JSONL tail
   and defaults to the newest 20 complete turns. This avoids decoding the
   entire event log, but selection is bounded by turn count rather than a
   strict byte/render-work budget. A single turn can be very large. Setting
   `resume_transcript_turns = 0` currently requests an unbounded full visual
   replay.
3. `TUISession.run()` in `src/agenthicc/runners/tui_session.py` starts the
   workspace and displays its first frame, then awaits
   `Workspace.replay_transcript()` before starting the processor and input
   session. The input editor therefore cannot accept the next user message
   until all selected history has passed through the renderers.
4. `Workspace.replay_transcript()` in
   `src/agenthicc/tui/workspace/workspace.py` remembers all selected event IDs,
   sets the loading state, and replays events in batches of 64 with an
   event-loop yield between batches. This keeps redraws possible, but it does
   not bound the total work or make input available early.
5. The resumed input editor independently calls `load_user_message_history()`
   from `src/agenthicc/tui/runtime/session_log.py`, which scans the session log
   tail again to reconstruct navigation history.
6. Separately, `SessionConversation.open()` in
   `src/agenthicc/runners/session_conversation.py` folds the durable provider
   journal into memory. That history is used for future model requests and is
   not the UI transcript. This PRD does not authorize dropping or limiting that
   provider context.

These findings explain the visible wait, but no timing profile for the user's
specific session was supplied. Implementation must add stage-level timing so
any substantial cost in session construction or provider-journal folding is
measured separately instead of being attributed to transcript rendering.

## 3. Problem and user impact

The current attach sequence puts historical display work on the critical path
to interaction:

```text
attach / resume
    → open session and restore durable state
    → mount workspace
    → read transcript tail
    → render all selected events
    → rebuild input history from another tail read
    → accept user input
```

During a large replay, the user can see that Agenthicc is alive but cannot send
a message, interrupt, or otherwise use the attached session. Increasing the
number of replay chunks or removing the loading indicator would not fix the
blocking dependency; it would only change how the wait appears.

The visible transcript and the provider conversation are separate projections.
Optimizing one must not silently change the other.

## 4. Goals

1. Make the attached session interactive without waiting for unbounded
   historical rendering.
2. Bound initial transcript I/O, decoding, retained event count, and rendering
   work using both event/turn limits and a byte budget.
3. Keep the newest useful transcript context visible in chronological order.
4. Preserve access to older transcript events through explicit pagination or
   another bounded on-demand history view.
5. Avoid duplicate log scans when the same recent event page can provide both
   the visible preview and input-navigation history.
6. Preserve exact persisted events and event IDs; replay remains presentation-
   only and idempotent.
7. Instrument attach stages so regressions can be localized to session
   construction, journal hydration, transcript I/O, rendering, or input setup.
8. Apply the same behavior to `--resume`, `--continue`, `agenthicc attach`,
   `agenthicc agents` attach, and goal-run attach.

## 5. Non-goals

- Truncating, compacting, or otherwise changing provider conversation memory.
- Deleting, rewriting, or migrating existing transcript JSONL records.
- Replaying historical user messages to the agent as new work.
- Changing background-session ownership, workflow recovery, or session claim
  rules.
- Hiding a slow attach by removing the loading indicator without reducing
  critical-path work.
- Rendering older pages out of chronological order into the live terminal
  scrollback after new user or assistant output has already been appended.
- Replacing the durable event log with an in-memory-only transcript cache.

## 6. Proposed solution

### 6.1 Bounded initial transcript preview

Introduce a transcript page/preview contract with explicit limits for:

- maximum recent complete turns;
- maximum decoded events;
- maximum serialized bytes read/retained for the initial preview; and
- maximum individual rendered payload size, where an event can otherwise
  overwhelm the first-paint budget.

The loader should select a complete, chronological suffix within these budgets.
It must not split a tool-call/result group or display an orphaned continuation
when a safe boundary can be identified. If one event exceeds a display budget,
the preview may show a clearly marked bounded excerpt, with the full event
available in the history viewer; persisted content remains unchanged.

The preview page should be loaded efficiently from the log tail. Do not scan
from the beginning of an old log just to display its newest page. Corrupt
trailing JSONL records remain skippable according to existing recovery
behavior.

### 6.2 Separate attach readiness from historical replay

After the initial bounded preview has been rendered and the resumed runtime is
ready, start the normal input loop without awaiting any additional history
work. If the transcript source or viewer performs further reads, they must be
bounded, cancellable background/UI operations and must not own the session's
input or provider-turn lock.

The transcript replay controller must maintain an explicit boundary between
historical event IDs and events produced after attachment. Historical replay
must not be appended through the persistence subscriber; newly produced events
must continue to render and persist exactly once.

### 6.3 On-demand older-history view

Provide a paginated transcript-history view (an overlay or equivalent bounded
reader) for older pages. It should load older events only when requested and
render them in their original order within that view. It must not append an
older page behind newer live output in the ordinary scrollback, which would
misrepresent chronology.

The view must support navigating between pages, returning to the live
workspace, and inspecting long event payloads without forcing the entire
session log into memory. A page cursor should be based on a stable log position
or event boundary and should remain valid for the lifetime of the view even as
new events append to the log.

### 6.4 Reuse one recent-history projection

Derive input-editor history from the already loaded recent event page whenever
that page contains enough user-message events. If a separate input-history
depth is needed, load it with a bounded projection and share the underlying
read/index operation rather than independently decoding overlapping tail
blocks. Input history must retain its existing accepted-user-message-only
semantics.

### 6.5 Preserve the meaning of existing configuration safely

`resume_transcript_turns` may continue to choose how much recent context the
preview attempts to show, but it must no longer permit an unbounded inline
replay that blocks attach. In particular, `0` must not mean “print the entire
log before accepting input.” Preserve the intent of full-history access through
the paginated transcript view, document the behavior change, and provide a
clear migration note for configurations that currently use `0`.

Any new byte/event limits must have conservative defaults and validation. A
user may tune the recent preview within hard safety bounds, but there must be
no setting that restores an unbounded attach-time render by accident.

### 6.6 Stage-level startup diagnostics

Measure monotonic durations for these phases without logging transcript text,
prompts, credentials, or provider payloads:

```text
attach handoff / ownership
session-context and provider-journal restore
workspace first frame
preview log read + decode
preview render
input-history construction
time to interactive input
```

Expose timings through a testable diagnostics/profiling seam and optionally
debug-level logs. Do not emit one log entry per transcript event.

If profiling proves provider-journal restoration dominates total attach time,
that is a separate optimization area: it must retain full provider context and
be addressed by a follow-up design rather than being “fixed” by limiting the
conversation journal in this transcript change.

## 7. User experience

The intended experience is:

```text
attach target
    → live TUI appears
    → recent transcript preview appears promptly
    → input becomes usable as soon as the session runtime is ready
    → older history is available through an explicit paginated view
```

The loading status should describe only work that is actually blocking the
initial preview. It must end when the bounded preview is available, not when
all historical records have been read. The user must still be able to interrupt
an active agent turn normally after attach.

## 8. Compatibility and migration

- Keep the JSONL event format and existing session directories unchanged.
- Keep the complete provider journal fold and all workflow/session recovery
  semantics unchanged.
- Keep existing attach command forms and manager actions unchanged.
- Keep recent transcript order, grouping, renderer semantics, and event-ID
  deduplication for the preview.
- Change only the presentation semantics of an unbounded
  `resume_transcript_turns = 0`: all history remains available, but it is
  browsed on demand instead of being printed synchronously into the live
  scrollback before input is enabled.
- Document new preview limits and the migration behavior in the configuration,
  TUI, session, and troubleshooting guides.

## 9. Acceptance criteria

### Functional

1. Every supported attach/resume entry point uses the same bounded preview and
   on-demand older-history behavior.
2. A large transcript cannot prevent the input session from starting after the
   runtime and initial preview are ready.
3. Initial replay is bounded by configured turns and hard event/byte limits;
   zero turns does not trigger unbounded inline replay.
4. Older history can be navigated in bounded pages without loading the whole
   log or appending out-of-order events to live scrollback.
5. The newest preview events and older pages retain exact event order and
   timestamps, including grouped exploratory/tool output behavior.
6. Replayed history does not create duplicate JSONL records, provider messages,
   tool executions, or turn counts.
7. User-message input history remains correct, and overlapping log reads are
   eliminated or measurably reduced by sharing the recent projection.
8. A newly arriving live event during attach is rendered and persisted once;
   it is neither lost nor placed before the historical preview boundary.
9. Cancellation, malformed trailing records, empty sessions, a single
   exceptionally large turn, and a single exceptionally large event are
   handled without hanging the TUI or corrupting durable data.
10. Full provider conversation memory, workflow checkpoints, usage records, and
    saved transcript contents are byte-for-byte unaffected by display limits.

### Performance

11. Add reproducible benchmarks for small, typical, and large transcripts,
    including at least 100,000 events and a large single-turn case. Report
    session-context restore, first frame, preview load/render, input-ready, and
    total attach durations separately.
12. On the documented CI/development benchmark host, the bounded preview must
    reach first render within 500 ms p95 for a warm local session log, and the
    TUI must become input-ready within 1 second p95 after the runtime context
    has completed initialization, regardless of the number of older events.
    If a platform cannot meet these targets, benchmark evidence and an explicit
    revised target must be recorded before release.
13. Preview memory and synchronous render work remain bounded by fixed limits;
    memory use does not grow linearly with total historical log size.

### Verification

14. Unit tests cover cursor/page boundaries, byte/event caps, ordering, event
    group boundaries, ID deduplication, corrupted records, and configuration
    validation.
15. Integration tests prove resumed input history and transcript preview share
    the intended history projection, while the complete provider journal still
    restores.
16. End-to-end tests attach to a synthetic large live session, submit input
    while older history exists, browse multiple transcript pages, return to the
    live view, and verify newly emitted events appear exactly once.
17. Tests cover `--resume`, `--continue`, `agenthicc attach`, and manager/goal
    attach routing, or establish that all call the same tested implementation.
18. Stage timings contain no user text, transcript payloads, hidden reasoning,
    or secrets.

## 10. Test plan

### Unit tests

- Tail page reading with byte and event bounds, including partial first/last
  JSONL lines.
- Stable cursor navigation while new records append.
- Complete turn/tool-group boundary selection and oversized event display.
- Preview and input-history projection reuse.
- Replay remains presentation-only and does not notify log persistence
  subscribers.
- Cancellation and reader errors restore loading/input state.
- `resume_transcript_turns = 0` migration semantics and hard-limit enforcement.

### Integration tests

- Resume an existing session with a large conversation log and full provider
  journal; assert the provider memory remains complete while the display is
  bounded.
- Simulate slow rendering and prove input readiness does not await older-page
  loading.
- Append live events while the history viewer is open and verify ordering,
  deduplication, and return-to-live behavior.
- Verify all attach adapters converge on the same transcript bootstrap path.

### End-to-end and performance tests

- Generate synthetic logs with 100,000+ ordinary events, 100,000+ tool events,
  large text payloads, malformed trailing lines, and unusually large individual
  turns.
- Measure cold and warm attach stage timings and bounded memory in a repeatable
  local benchmark; avoid strict wall-clock assertions in ordinary unit tests.
- Verify normal small-session appearance remains unchanged aside from the
  faster readiness transition.

## 11. Rollout

1. Add stage-level timing and benchmark fixtures first to establish a baseline.
2. Implement the bounded shared preview projection and verify output parity on
   small sessions.
3. Add the paginated history view and the zero-turn migration behavior.
4. Decouple session input readiness from all work beyond the initial preview.
5. Enable by default after E2E and large-log benchmarks pass; retain a
   temporary diagnostic switch only if needed for rollout comparison, not an
   unbounded blocking mode.
6. Update user docs and release notes before shipping the configuration
   behavior change.

## 12. Security and privacy

- Transcript viewers read only the selected session's existing log path using
  the same validated session identifier rules as current storage code.
- Do not add network access or transmit transcript contents for pagination or
  metrics.
- Timing and diagnostic output is aggregate and payload-free.
- Never weaken session ownership or access controls to make attach appear
  faster.

## 13. Definition of done

The user can attach to a session with extensive history, see the latest useful
context promptly, and submit input once the session runtime is ready without
waiting for every historical event to be rendered. Older transcript history
remains inspectable in bounded chronological pages, and durable provider
context, workflows, events, and recovery behavior remain unchanged.

## 14. Implementation record

Implemented on 2026-10-17.

- Session resume now reads a single bounded JSONL suffix after the first Live
  frame. The initial view is constrained by event, retained-byte, per-event,
  and reverse-scan limits; zero turns disables only the turn-count cap.
- The same `TranscriptPage` supplies presentation replay and accepted-user
  input history. Historical replay remains presentation-only and does not
  mutate the provider conversation journal, workflow checkpoint, or event log.
- `/history` opens an on-demand chronological page browser. Older pages use
  captured byte offsets, so concurrent appends do not shift already-browsed
  pages. Selecting an oversized event loads only that one full record in its
  detail view. Browsing never appends old records into live scrollback.
- Startup reporting now includes session-context, provider-journal, first
  frame, preview-read, input-history projection, preview-render, and
  interactive-input readiness phases. Diagnostics contain timing/status only,
  not transcript content.
- Configuration is documented in `docs/guides/configuration.md`; behavior and
  troubleshooting are documented in the TUI/session guides. A reproducible
  large-log benchmark is available at `scripts/bench_resume_transcript.py`.

Verification references:

- Unit: `tests/unit/test_transcript_history_pages.py`,
  `tests/unit/test_resume_transcript.py`, and `tests/unit/test_config.py`.
- Integration: `tests/integration/test_transcript_attach_projection.py`.
- Benchmark: `uv run python scripts/bench_resume_transcript.py --events 100000 --samples 5`.
- Repository gates are recorded in the implementation handoff for this goal.

Initial local benchmark evidence (Python 3.13.13, Linux x86_64, 8 reported
CPUs; 3 samples) used a 100,000-event, 14.77 MB transcript with a 2 MiB final
event and every tenth record represented as a tool completion. Preview-read
p95 was 4.224 ms and input-ready-after-context p95 was 6.536 ms, both below
the proposed targets. A separate all-tool run with 100,000 tool-completion
events, one sample, and no oversized final event rendered the 128-event
preview in 8.110 ms and reached input-ready-after-context in 16.485 ms. These
are local development measurements, not CI wall-clock assertions; the script
emits host and Python metadata for future comparisons.
