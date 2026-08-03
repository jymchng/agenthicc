---
title: "PRD-164: Suppress Repeated Idle TUI Status Frames"
status: Implemented
version: 1.0.0
created: 2026-08-03
related_prds:
  - PRD-60
  - PRD-120
  - PRD-144
  - PRD-158
  - PRD-161
tags:
  - tui
  - rich-live
  - rendering
  - transcript
  - performance
---

# PRD-164 — Suppress Repeated Idle TUI Status Frames

> Approval-wait timer redraws are a separate issue covered by PRD-165. PRD-164
> owns the static idle `frame` contract; it does not own prompt-wait timing.

## 1. Executive summary

After an agent response completes, agenthicc can visibly append the same idle
status panel many times before returning to the input prompt. The supplied
reproduction shows seven identical blocks containing `✿ Idle`, the model,
token counters, and the session metadata. The repetition is especially visible
when terminal control sequences are captured, stripped, or not interpreted by
the client displaying the TUI output.

This is a presentation and scheduling defect. It is not duplicate LLM output,
duplicate conversation history, or a workflow notification being emitted
seven times. The idle animation tick continues to advance the reactive
`frame` signal even though the idle status renderer is intentionally static.
The workspace subscribes to that signal and refreshes Rich's `Live` block for
every tick. A terminal that does not apply the Live erase/move controls turns
those refreshes into repeated transcript-looking blocks.

The fix must stop publishing idle-only animation changes and add a rendering
regression guard. Active thinking, tool execution, compaction, input editing,
notifications, and legitimate status changes must remain responsive.

## 2. Evidence and exact problem statement

### 2.1 Supplied reproduction

Evidence file:

```text
/home/ai-slave/.codex/attachments/c144b388-55df-439a-a2ef-56b43121c012/pasted-text-1.txt
```

The file contains an assistant response followed by:

```text
✾ Worked for 2 mins 23 seconds
✿ Idle │ ↑ 360,790,594 ↓ 952,010 ?
openai/deepseek-v4-flash
```

The exact `✿ Idle` status and model block is repeated seven times before the
session identifier, turn count, separator, and input prompt appear. The
in-progress `code_plan` notification appears once after those repeated
frames. This establishes the visible symptom as repeated status rendering at
the idle boundary, not repeated workflow execution.

### 2.2 Current data flow

```text
TUISession._tick()                 every 50 ms
        │
        ▼
ConversationStore.tick()
        │  currently increments frame even when AgentState.IDLE
        ▼
frame Signal notification
        │
        ▼
Workspace._redraw()
        │
        ▼
Live.update(_build(), refresh=True)
        │
        ├─ StatusComponent renders the same static ✿ Idle block
        └─ terminal control sequences normally replace the old Live block

Raw/captured client that strips or ignores erase/move controls
        └─ each refresh becomes another visible idle block
```

The relevant current ownership boundaries are:

| Concern | Current source | Finding |
|---|---|---|
| Tick loop | `src/agenthicc/runners/tui_session.py` | Calls `conversation.tick()` continuously for the session lifetime |
| Animation state | `src/agenthicc/tui/conversation_store.py` | `tick()` increments `frame` even while idle |
| Reactive redraw | `src/agenthicc/tui/workspace/workspace.py` | Subscribes to `conv.frame` and refreshes `Live` |
| Idle rendering | `src/agenthicc/tui/workspace/components.py` | Freezes the flower to `✿` when not running, so idle frame changes are not visible state changes |
| Scroll/event rendering | `src/agenthicc/tui/workspace/appender.py` | Renders `ConversationEvent` records; it is not the source of the repeated idle frames |

### 2.3 Reproduction against the current source

The defect can be reproduced without an LLM or network call:

1. Create `AppState` and a `Workspace` with a recording, terminal-capable
   Rich `Console`.
2. Set the model name to `openai/deepseek-v4-flash`.
3. Start the workspace while the conversation is idle.
4. Call `conversation.tick()` seven times with no active turn.
5. Allow the event loop to process each redraw.
6. Stop the workspace and count normalized `✿ Idle` blocks.

The current result is `frame == 7` and seven additional idle Live frames. The
same behavior appears in the supplied file, where ANSI cursor-control behavior
has not turned the refreshes into one stable visual block.

### 2.4 Root cause

The root cause is the mismatch between the animation clock and the rendered
state:

1. `ConversationStore.tick()` treats `frame` as an unconditional 50 ms clock.
2. `Workspace` treats every `frame` change as a reason to repaint.
3. `StatusComponent` treats idle as non-animated and renders the same content
   for every frame value.
4. Rich's Live refresh mechanism relies on the consuming terminal honoring
   cursor movement and erase controls. Captured or client-rendered output may
   preserve each refresh as ordinary text.

The existing unit test named
`test_tick_increments_unconditionally_when_idle` codifies the first part of
this mismatch and must be revised as part of the implementation. The desired
contract is that the animation frame advances only when an animated surface
can change.

### 2.5 What is not the cause

- The assistant response is not being added to the conversation seven times.
- `ScrollBufferAppender` is not receiving seven copies of the response event.
- `print_idle_header()` is not the source of the repeated status block; it is a
  separate session-metadata renderer and is not called by the normal TUI loop.
- The in-progress workflow notification is not duplicated.
- The `code_plan` workflow is not being restarted by this symptom.

## 3. Goals

1. Ensure an idle session does not produce repeated identical status frames.
2. Keep the final idle status, session metadata, workflow notification, and
   input prompt visible exactly once at the end of a turn or transcript replay.
3. Preserve animation and redraw responsiveness while the agent is thinking,
   executing tools, recovering, or compacting context.
4. Preserve redraws caused by actual user-visible state changes: input text or
   cursor edits, notifications, model/token/cost updates, mode changes,
   overlays, workflow progress, terminal waits, and transcript loading.
5. Make the behavior deterministic in interactive terminals, captured output,
   and test consoles that do not interpret ANSI control sequences.
6. Prevent idle redraws from consuming CPU and producing unnecessary terminal
   writes at the 20 Hz session tick rate.

## 4. Non-goals

This PRD does not:

- redesign the Rich `Live` layout or replace the TUI rendering architecture;
- remove active status animation during thinking, running, recovery, or
  compaction;
- change conversation events, session journals, transcript replay semantics,
  workflow checkpoints, or workflow state transitions;
- suppress legitimate status changes that occur while idle, such as a changed
  notification, input buffer, cursor, model, token count, mode, or overlay;
- guarantee that an actively animated Live block produces no raw escape/control
  bytes in an output capture; the requirement is that unchanged idle state is
  not repeatedly refreshed;
- alter the content or timing of the assistant's LLM response.

## 5. Functional requirements

### FR-164.1 — Activity-aware animation clock

`ConversationStore.tick()` MUST continue to update clocks that are needed by an
active turn, but MUST NOT advance the `frame` signal solely because the store
is idle, complete, or in an error state. The frame may advance when:

- the conversation is actively running/thinking/recovering;
- context compaction is active; or
- another explicitly animated UI state is active and documented.

Paused approval/question states MUST retain their existing frozen-frame
behavior.

### FR-164.2 — No redundant idle redraw

The workspace MUST NOT call `Live.update()` merely because an idle animation
frame would have changed. If a frame update is received during idle due to a
compatibility path, the workspace MUST treat it as a no-op when the rendered
idle state is otherwise unchanged.

### FR-164.3 — Single idle presentation

At the end of a direct turn, queued turn, workflow phase, interrupted turn,
or resumed transcript replay, the user MUST see one current idle status/prompt
surface. Repeated identical status panels MUST NOT be appended to scrollback or
captured output as a side effect of the idle tick.

### FR-164.4 — Active animation preservation

When the agent is thinking, running a tool, recovering from a tool error, or
compacting context, frame advancement and status animation MUST continue to
work. A test MUST demonstrate that at least two frame values can produce the
expected changing animated output.

### FR-164.5 — Legitimate state changes still repaint

The fix MUST NOT make the input or status stale. The following changes MUST
still cause a timely redraw:

- typed text, cursor movement, paste state, or queued input;
- notification or workflow-progress changes;
- model, token, cost, usage, or terminal-wait changes;
- mode changes and overlay/approval changes;
- transcript loading state transitions.

### FR-164.6 — Event and persistence isolation

The fix MUST NOT add, remove, duplicate, or reorder `ConversationEvent`
records. Session logs, workflow checkpoints, memory journals, and resumed
transcripts MUST remain unchanged except for the presentation defect being
removed.

### FR-164.7 — Resumed-session coverage

A resumed session with a replayed transcript and an in-progress workflow
notification MUST render the notification and final idle input surface once,
without repeated idle status blocks.

## 6. Non-functional requirements

- **Performance:** An idle session MUST stop generating the 20 Hz frame-driven
  redraw workload. The steady-state idle path should have no frame-signal
  notifications and no `Live.update()` calls unless another signal changes.
- **Terminal portability:** Behavior MUST be correct on POSIX terminals,
  Windows terminal backends, non-interactive test consoles, and clients that
  do not interpret ANSI cursor controls.
- **Compatibility:** The public `frame` signal remains available as the shared
  animation counter. Only its idle advancement contract changes.
- **Determinism:** Tests MUST use controlled ticks and normalized output rather
  than timing sleeps or an actual external terminal.
- **Observability:** Debug diagnostics MAY count redraw reasons and frame
  updates, but MUST NOT log prompts, conversation contents, credentials, or
  tool arguments.

## 7. Proposed implementation direction

The implementation should remain within the existing store/workspace
ownership boundary:

1. Add an activity predicate in `ConversationStore.tick()` (or an equivalent
   private helper) that gates frame advancement on an animated state. Continue
   updating active elapsed clocks independently of whether a frame is emitted.
2. Update the frame signal documentation to describe it as an animation frame,
   not an unconditional wall-clock tick.
3. Add a defensive workspace redraw guard so a frame-only notification cannot
   repaint a visually unchanged idle block. The guard should not compare Rich
   object identity; it should use a deterministic visible-state/revision key or
   rely on the gated frame contract with an explicit compatibility test.
4. Keep `ScrollBufferAppender` as the sole owner of persistent
   `console.print()` event output. Do not solve this by printing status panels
   from the appender or by adding a second transcript renderer.
5. Preserve `Live` cleanup and transient behavior. The fix must not reintroduce
   per-turn Live start/stop or a cursor race.

## 8. Acceptance criteria

- **AC-164.1:** The supplied reproduction is documented as a deterministic
  test fixture or equivalent local test. Seven idle ticks do not produce seven
  normalized `✿ Idle` blocks; the idle block count remains one for the current
  surface.
- **AC-164.2:** In an idle `ConversationStore`, repeated `tick()` calls leave
  `frame` unchanged and notify no frame subscribers. The old unconditional-idle
  test is replaced with this contract.
- **AC-164.3:** During an active turn, repeated ticks advance `frame` and the
  status renderer continues to produce changing thinking/flower output.
- **AC-164.4:** Compaction animation and approval/question frame freezing keep
  their existing behavior.
- **AC-164.5:** Editing input, changing a notification, changing token/cost
  values, switching mode, showing an overlay, and changing transcript-loading
  state each still result in a current rendered surface.
- **AC-164.6:** A direct turn completion emits the same event kinds and counts
  before and after the fix; no idle status frame is persisted as a conversation
  event.
- **AC-164.7:** Resume replay with an incomplete `code_plan` workflow shows the
  workflow notification once and does not append repeated idle status panels.
- **AC-164.8:** A raw/captured-console test that strips ANSI movement controls
  still sees no repeated unchanged idle frame caused by the tick loop.
- **AC-164.9:** The full unit, integration, and E2E suites pass, including
  workspace redraw, transcript replay, waiting modal, and lifecycle tests.
- **AC-164.10:** Ruff, formatting, type audit, and the repository's applicable
  static checks pass without weakening existing safety or typing rules.

## 9. Test plan

### Unit tests

- `ConversationStore.tick()` does not advance `frame` while idle, complete, or
  error.
- Active thinking/running/recovering and compaction states advance `frame`.
- Paused approval/question ticks do not advance `frame`.
- Status rendering remains static for idle state and animated for active state.

### Integration tests

- `Workspace` subscribed to `frame` does not invoke `Live.update()` for idle
  ticks.
- Actual input, notification, token, mode, and overlay signal changes still
  invoke one coalesced redraw.
- A turn completion followed by the session tick loop leaves the appender's
  event output unchanged and does not create a status event.

### End-to-end tests

- Start a workspace with a model, complete a synthetic turn, run seven or more
  idle ticks, normalize terminal control sequences, and assert one idle panel.
- Replay a saved transcript with an incomplete `code_plan` workflow and assert
  one notification, one input surface, and no repeated idle panels.
- Exercise an active animated turn and confirm that frame updates remain
  visible and input remains responsive.

## 10. Rollout and compatibility

The change is local to the TUI runtime and does not require migration of
conversation logs or workflow checkpoints. Existing sessions should resume
without conversion. If a third-party component directly relies on idle `frame`
increments, it must migrate to an explicit timer or activity signal; idle frame
increments are not a supported semantic contract.

The implementation should be released behind no user-facing feature flag once
the capture and interactive-terminal tests pass. If a temporary diagnostic flag
is added, it must default to the fixed behavior and be removed after rollout.

## 11. Security and privacy

No new network, filesystem, subprocess, or credential behavior is needed. The
diagnostic and test paths MUST avoid writing conversation contents or secrets
to logs. Reducing idle terminal writes also reduces accidental exposure of
transient UI content in captured output.

## 12. Verification commands

At minimum, run:

```bash
uv run pytest tests/unit/test_unified_tick.py \
  tests/unit/test_workspace_redraw.py \
  tests/unit/test_resume_transcript.py -q
uv run pytest tests/integration tests/e2e -q
uv run pytest tests/ -q
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
git diff --check
```

## 13. Implementation evidence

The implementation is complete in the current source tree:

- `ConversationStore.tick()` publishes animation frames only for active
  thinking/tool/recovery and compaction states.
- `Workspace` routes frame notifications through an activity guard, so a
  compatibility frame write cannot repaint a static idle surface.
- `tests/unit/test_unified_tick.py` verifies idle, terminal, active, compaction,
  and paused-frame contracts.
- `tests/unit/test_workspace_redraw.py` verifies the workspace guard.
- `tests/integration/test_idle_status_frames_integration.py` verifies the
  store-to-Live boundary for idle and active ticks.
- `tests/e2e/test_idle_status_frames_e2e.py` verifies captured-console output
  and replay-style idle boundaries.

Validation run on 2026-08-03: `pytest tests/ -q` completed with 3,128 passed
and 15 skipped; Ruff, formatting, type audit, and `git diff --check` passed.
The repository-wide mypy command remains blocked by the pre-existing optional
`name_that_ui` import and NumPy stub/Python-version mismatch documented by the
latest repository validation; this change introduces no additional mypy error.

## 14. Implementation notes and open decisions

1. The primary fix should be activity-aware frame advancement because it
   removes the source of an otherwise pointless redraw. A workspace-level
   guard is still recommended as defense in depth for future frame producers.
2. The exact number of repeated blocks is terminal/client dependent; the
   invariant is that unchanged idle state does not produce new frame-driven
   output.
3. Rich's ANSI erase controls remain appropriate for an interactive terminal.
   Tests must nevertheless normalize or capture output in a way that exposes
   accidental repeated writes, because downstream clients may not execute
   those controls.
4. PRD-120's old wording that the frame increments unconditionally must be
   superseded by this PRD's activity-aware animation contract.
