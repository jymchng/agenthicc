---
name: running-the-tui
version: 1.0.0
tags: [tui, terminal, rich, slash-commands, approvals]
description: >-
  Operate the interactive Rich Live terminal workspace: the scroll buffer and pinned
  composer layout, modes, overlays, slash commands, input handling, approval flows,
  background sessions, and the platform terminal backends.
---

# Skill: Running the TUI

The interactive UI is a **Rich `Live` workspace**. It is not the older
terminal-input design that some historical notes describe: there is no separate
application module, no standalone transcript model, and no third-party prompt
framework. Everything below is the current `src/agenthicc/tui/` tree.

## When to use this skill

Use this skill when you need to:

- Launch and navigate the interactive workspace
- Understand what owns the screen and where to add a new region
- Switch execution modes and know what each one permits
- Use slash commands, `$` skills, and `@` mentions
- Trace how a tool approval reaches an overlay
- Fix the classic failure modes (duplicated frames, stuck spinner, resize)

---

## Runtime components

Each of these is a real module; keep new code inside this layering.

| Component | Location | Responsibility |
|---|---|---|
| Reactive state | `src/agenthicc/tui/conversation_store.py` | `ConversationStore`, `InputState`, `AppState` |
| Workspace root | `src/agenthicc/tui/workspace/workspace.py` | Owns the single `Live` block |
| Scroll buffer | `src/agenthicc/tui/workspace/appender.py` | `ScrollBufferAppender`, prints above the Live block |
| Live components | `src/agenthicc/tui/workspace/components.py` | `StatusComponent`, `ComposerComponent`, `FooterComponent` |
| Overlay host | `src/agenthicc/tui/workspace/overlay.py` | `OverlayHost.show()` / `.hide()` |
| Overlay widgets | `src/agenthicc/tui/workspace/overlays/` | One class per overlay kind |
| Input session | `src/agenthicc/tui/input/unified_session.py` | `UnifiedInputSession`, one raw-mode lifetime |
| Input buffer | `src/agenthicc/tui/input/buffer.py` | `InputBuffer` — pure text/cursor value object |
| Capabilities | `src/agenthicc/tui/input/capabilities.py` | `IDLE_CAPABILITIES`, `STREAMING_CAPABILITIES` |
| Prompt rendering | `src/agenthicc/tui/input/renderer.py` | `build_prompt()`, `PROMPT_CHAR`, `CURSOR_CHAR` |
| Width helpers | `src/agenthicc/tui/rendering.py` | `visible_len()`, `fit()` |
| Terminal backends | `src/agenthicc/tui/terminal/` | `TerminalBackend` protocol, POSIX/Windows |
| Triggers | `src/agenthicc/tui/trigger.py`, `src/agenthicc/tui/triggers/` | Slash commands and `@` mentions |

### The Live block

`Workspace` starts **one** Rich `Live` block for the whole application lifetime
(`src/agenthicc/tui/workspace/workspace.py:45`). Its docstring states the invariant plainly:
the block "starts ONCE at application startup and stops ONCE at shutdown. It
NEVER starts/stops per agent turn."

That is deliberate. Starting and stopping `Live` per turn causes a cursor race
that corrupts the display. Two constructor choices back it up:
`auto_refresh=False` (no background refresh thread racing `console.print()`)
and `transient=True` (clean teardown).

All redraws are explicit, driven by signal subscriptions through `_redraw()`
(`src/agenthicc/tui/workspace/workspace.py:284`). If you add a region, subscribe to the
relevant signal and redraw — do not spin up a second `Live`.

### Screen model

```text
terminal
├── scroll buffer            ← ScrollBufferAppender, ordinary printed output
│   ├── idle/session headers
│   ├── agent text
│   ├── tool results and collapsed tool groups
│   └── workflow / system / retry notifications
└── one permanent Live block
    ├── blank separator
    ├── status component
    ├── composer  OR  active overlay
    ├── border
    └── footer
```

`Workspace.start()` is called once, before the processor and input loop begin;
`Workspace.stop()` runs once during teardown. `Workspace.flush_scroll()`
(`:186`) drains the scroll buffer, and `Workspace.replay_transcript(events)`
(`:196`) feeds historical turns through the same renderers.

---

## Modes

The selectable cycle is **Safe → Plan → Yolo → Safe**. `Safe` is the default
(`src/agenthicc/modes/builtin.py:62,73,84`).

| Mode | Behaviour |
|---|---|
| `Safe` | Reads, searches, and git reads run directly; writes, git changes, commands, network, and unannotated tools require approval |
| `Plan` | Read-only; hard-blocks the mutating capabilities |
| `Yolo` | Tools run without per-action approval |

Compatibility aliases are accepted but not displayed: `Auto` → `Yolo`;
`Guard` and `Ask` → `Safe`; `Review` → `Plan`. `Debug` is **not** an alias and
is rejected. Replay is an internal state and is not selectable.

Press **Shift+Tab** to cycle. `/mode [name]` performs an explicit switch and
reports the canonical choices when given an unknown name.

---

## Input and triggers

`UnifiedInputSession` enters raw mode once and dispatches each key through the
active capability list (`src/agenthicc/tui/input/unified_session.py:49`). The session has an
`InputMode` (`:44`) that selects the capability list:

- **IDLE** — `IDLE_CAPABILITIES` (`src/agenthicc/tui/input/capabilities.py:487`): triggers,
  history, cursor movement, paste, mode cycling, submission.
- **STREAMING** — `STREAMING_CAPABILITIES` (`:505`): a reduced set so you can
  still queue input or interrupt while a turn runs.

The triggers are:

| Trigger | Picker |
|---|---|
| `/` | Command picker, backed by the unified command registry |
| `$` | Skill picker, backed by discovered skills |
| `@` | Project file / mention picker |

`PROMPT_CHAR = "❯"` and `CURSOR_CHAR = "▌"` are the composer glyphs
(`src/agenthicc/tui/input/renderer.py:12-13`). `InputBuffer` is a pure value object — mutate
it only through its named methods, never by poking the buffer list
(`src/agenthicc/tui/input/buffer.py:10`).

ESC cancels the active turn and returns the pipeline to IDLE immediately, even
while async cleanup finishes; this keeps the double-Ctrl+C exit path responsive.

---

## Slash commands

Canonical definitions live in `commands/builtins.py`. Stateful commands such as
`/workflow` and `/compact` are deliberately intercepted by `TUISession` because
they need session fields. The completion constants in
`src/agenthicc/tui/input/completions.py` are compatibility adapters over the canonical
registry — do not add new commands there.

Skills are triggered with `$skill-name` or `$alias` and are intentionally kept
out of the `/` picker. `/skills` and `/skills reload` remain slash commands for
inspecting and refreshing them.

Commands that affect the run without sending a message:

| Command | Effect |
|---|---|
| `/usage` | Local token/cost snapshot, no message sent |
| `/config` | Opens the configuration overlay immediately, even during a response |
| `/cancel`, `/interrupt` | Cancels the active run |
| `/bg`, `/background` | Background-session control plane |
| `/status` | Agent status overlay |
| `/commands`, `/skills` | Registry listings kept inside the overlay |

---

## Overlays and approvals

The workspace can show help, command/skill/tool/workflow listings,
configuration, trigger picker, plan review, questions, terminal lists, and
generic tool approval overlays. All of them live under
`src/agenthicc/tui/workspace/overlays/` — for example `HelpOverlay`,
`CommandListOverlay`, `SkillListOverlay`, `ConfigMenuOverlay`,
`PlanApprovalOverlay`, `QuestionsOverlay`, and `ApprovalOverlay`.

`OverlayHost` exposes exactly `show(overlay)`, `hide()`, and a `widget`
property (`src/agenthicc/tui/workspace/overlay.py:51-64`).

**Rule:** an overlay never writes to the terminal directly. It updates its own
state or invokes a callback and lets the workspace redraw. New approval kinds
need an overlay class, a registry entry, and tests for approve/reject, cancel,
and resize.

### How an approval reaches its overlay

`ApprovalRequest.kind` (`src/agenthicc/tools/approval.py:44`) selects the
overlay. `TUISession._wire_approval_overlay()` builds a registry mapping kind →
class and falls back to `ApprovalOverlay`
(`src/agenthicc/runners/tui_session.py:1864-1872`):

```python
_overlay_registry = {
    "plan_review": PlanApprovalOverlay,
    "questions": QuestionsOverlay,
}
_overlay_default = ApprovalOverlay
```

Add a new kind by extending that dict — there is no `if`/`elif` chain to edit.
The status line reflects the pending kind with a stable waiting label rather
than the animated Thinking state (`src/agenthicc/tui/workspace/components.py:99-101`):

| `kind` | Status label |
|---|---|
| `questions` | `Waiting for your answer` |
| `plan_review` | `Waiting for plan approval` |
| anything else | `Waiting for approval` |

A background terminal in the foreground shows `Waiting for background terminal`
(`:152`). While the animated states are idle, complete, or errored, no frame
ticks are published — the spinner label is `Thinking` (`:16`) — which stops
captured terminals from accumulating identical idle panels.

---

## Resumed transcripts

When the workspace opens an existing session, the newest **20** complete turns
are loaded from the tail of the session log and handed to
`ScrollBufferAppender` in order (`Workspace.replay_transcript`). This bounded
projection keeps startup fast on a very large log.

Set `[behaviour] resume_transcript_turns = N` to change the count
(`src/agenthicc/config.py:1363`); `0` requests the complete visual transcript.
The setting is presentation-only — it does not trim provider memory, usage,
workflow state, or the durable event log. Replay does not append to
`ConversationStore`, so events are not written to `conversation.jsonl` twice.

---

## Terminal backends

`get_backend()` is the single permitted platform branch
(`src/agenthicc/tui/terminal/backend.py:49`): `os.name == "nt"` yields `WindowsBackend`
(msvcrt), otherwise `PosixBackend` (termios/tty). The module docstring is
explicit that no feature code may import `msvcrt`, `termios`, or `tty` directly.

All application code goes through the `TerminalBackend` protocol (`:24`). The
Windows backend uses `ReadConsoleInputW` so Shift+Tab keeps its modifier, and
POSIX raw mode is a no-op for non-TTY descriptors and restores the previous
terminal state on exit.

Resize is handled by `_on_sigwinch` → `_schedule_resize_redraw` →
`_flush_resize_redraw` → `_reset_live_after_resize`
(`src/agenthicc/tui/workspace/workspace.py:389,367,383,339`), which
debounces to one repaint and clears Rich's pre-resize geometry first.

---

## Testing UI changes

- Test conversation and signal mutations as unit tests against
  `ConversationStore`.
- Test `ScrollBufferAppender._flush_batch()` with a fake or captured `Rich`
  console when adding an event renderer.
- Test input capabilities with synthetic `Key` values.
- Test workspace startup/shutdown and non-TTY input in integration tests.
- Test terminal backends with pure key-decoding cases on Linux, and real
  interactive behaviour only where the platform is available.

Use `visible_len()` and `fit()` from `src/agenthicc/tui/rendering.py` in any component that
builds markup. Markup wider than the terminal desyncs Rich's cursor
repositioning and makes the Live block overwrite content above it.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Duplicated status lines / torn frames | A second `Live` started per turn | Only `Workspace.start()` may own `Live` |
| Content above the panel gets overwritten | Markup exceeds terminal width | Route every `render(cols)` through `fit()` |
| Spinner keeps redrawing when idle | Frame ticks published in a static state | Only publish ticks for thinking/tool/recovery/compaction |
| Duplicate frames after resize | Live geometry not reset | Keep the SIGWINCH debounce path; don't redraw directly |
| An overlay leaves the screen blank | Overlay wrote to the terminal itself | Update state and call `overlays.hide()` instead |
| Prompt input is not editable | Another consumer holds raw mode | Ensure `UnifiedInputSession` owns the single raw-mode lifetime |
| Shift+Tab does not cycle modes | Input backend not interactive, or old Windows backend | Verify the Windows `ReadConsoleInputW` backend is in use |

---

## Key points

- The UI is **Rich Live**; there is no `src/agenthicc/tui/app.py`, no separate transcript
  model, and no prompt-toolkit dependency.
- One `Live` block for the entire application lifetime; start and stop it once.
- `auto_refresh=False` and `transient=True` are load-bearing, not cosmetic.
- Overlays update state and let the workspace redraw; they never print directly.
- Approval routing is a `kind` → overlay-class registry, extended by adding a
  dict entry.
- Modes cycle Safe → Plan → Yolo with aliases accepted but not displayed.
- All width-sensitive rendering must use `visible_len()` / `fit()`.
