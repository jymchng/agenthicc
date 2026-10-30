---
title: "AGX-10: Attention routing — badges, notifications and cross-session approvals"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-05, agx-06, agx-07, agx-08, agx-13, prd-144, prd-165, prd-226, prd-227]
tags: [agents, notifications, approvals, tui, ux]
---

## Implementation status (2026-10-29)

Implemented in the reactive TUI and background notification dispatcher.

- Registry and inbox changes update cross-session badges, the temporary agents
  hint, terminal title and coalesced toasts without render-path polling.
- Ctrl+G and the empty-composer gesture open the hub on the oldest pending
  item; a visible toast routes to its session and selects the appropriate tab.
- The TUI attention lease prevents the detached notification sender from writing
  directly to the terminal while the TUI owns it. Unfocused terminal
  notifications use filtered OSC 9; desktop notifications remain available.
- Modal deferral, coalescing, quiet hours and `/dnd` are covered, including
  preserving badge counts while notifications are suppressed.
- Verification: 300 focused unit, integration and E2E tests passed; full project
  typecheck and type-audit gates passed; focused Ruff checks passed; strict docs
  build and `llms_check` passed.

# AGX-10 — Attention routing: badges, notifications and cross-session approvals

## 1. Summary

While you work in one session, background agents that need approval or input,
finish or fail are invisible unless you open `/inbox` or run `agenthicc agents`
in another terminal. PRD-226's local "terminal" notification writes raw text to
stderr, which corrupts a running TUI. Route attention through the TUI: a
status-bar badge with live counts, short non-modal toasts, a one-key jump to
the item in the hub, system notifications only when the terminal is unfocused,
and a do-not-disturb switch.

## 2. Problem and evidence

- The foreground TUI shows nothing about other sessions. `/inbox` must be
  opened explicitly (`BackgroundInboxOverlay`).
- PRD-226's local delivery with the `terminal` backend writes
  `\n[agenthicc] {title}: {summary}\n` to `sys.stderr` when it is a TTY
  (`src/agenthicc/background/notifications.py:730`). Inside an active Rich
  `Live` block that text lands in the middle of the frame.
- In-session subagent approvals carry no requester (AGX-08), so a prompt cannot
  say who is asking.
- New-activity marks in the agents list are kept in memory and lost when the
  screen closes (`BackgroundManager._seen_activity`).

## 3. How others route attention

- **Claude Code agent view**: the prompt footer shows `← for agents` for ten
  seconds after a session opens and `← 2 agents` while background sessions
  wait on you (`← 2 done` when some finished and none wait); the terminal title
  reads `2 awaiting input · claude agents`; a terminal notification fires when
  a background session needs input, finishes or fails, and the `Notification`
  hook receives `agent_needs_input` or `agent_completed`. With reduced motion
  enabled, the colour flash and done count are hidden.
- **Claude Code**: a background subagent's permission prompt "surfaces … in
  your main session and names the subagent that is asking"; Esc denies that one
  call. After a subagent finishes, the footer shows `/tasks to see subagents`
  for 30 s; results arrive "as a completion notification in a later turn".
- **Codex**: the approval overlay "shows the source thread label", and `o`
  opens that thread before approving.
- **Grok Build**: monitors turn each output line into a notification in the
  conversation; the tasks pane lists everything running.

## 4. Goals and non-goals

**Goals**

1. Pending approvals and questions from any agent are visible from any session
   within 1 s, without interrupting typing.
2. One key opens the oldest item needing attention in the hub (AGX-05).
3. Finished and failed background agents are announced once, briefly.
4. Nothing writes outside the TUI's renderer while a TUI owns the terminal.
5. System notifications fire only when the user is not looking at the terminal.

**Non-goals**

- Answering a *background* session's approval in a modal inside the foreground
  session. Only the current session's own requests (including its subagents',
  AGX-08) are modal; everything else is a badge and a toast.
- Changing webhook delivery (PRD-226).

## 5. Design

### 5.1 Badge

The status bar shows the hub counts when non-zero:

```text
 executor · ⊘ Safe   ◆ 2 need you · ● 3 running        ← agents
```

- `◆ N need you` counts pending approvals, questions and input requests across
  sessions (inbox, PRD-227) plus lost workers.
- `● N running` counts active background agents.
- The counts come from the indexed registry and inbox (AGX-03) through change
  notification (AGX-04), never from polling in the render path.
- For ten seconds after a session starts, the footer shows `← agents` even
  when the counts are zero, teaching the AGX-05 gesture; afterwards it appears
  only with a count.

### 5.2 Toasts

A single toast line above the footer for 8 s, non-modal and never taking focus:

| Event | Toast |
|---|---|
| Approval or question pending | `◆ fix-flaky needs approval: bash pytest -k schedule   Ctrl+G` |
| Finished | `✓ refactor-auth finished · 4 files changed` |
| Failed | `✗ site-clone failed: rate limit exceeded (429)` |
| Several at once | `✓ 3 agents finished · ◆ 1 needs you` (coalesced within 2 s) |

Toasts are suppressed while a modal (approval, question, plan review) owns the
terminal and are delivered as one coalesced line afterwards, so they never cause
the redraw loops PRD-165 removed.

### 5.3 Jump key

← on an empty composer or Ctrl+G (both configurable, AGX-13; see AGX-05)
opens the hub on **Needs you** with the oldest item selected when the badge
shows any; otherwise on **Running**. While a toast is visible, Ctrl+G opens that
toast's agent instead.

### 5.4 In-TUI routing of PRD-226 local notifications

When a TUI is active in the process, the local `terminal` backend hands
notifications to the TUI's toast queue instead of writing to stderr. When the
terminal is unfocused — detected with focus reporting (`CSI ? 1004 h`, focus-in
`CSI I`, focus-out `CSI O`) where the terminal supports it — the TUI also emits
a terminal notification escape (`OSC 9` or `OSC 777`, by detected terminal) or
the platform notifier PRD-226 already uses. Unsupported terminals fall back to
the badge and toast only.

### 5.5 Terminal title and notification events

- While the TUI or hub runs, the terminal title (`OSC 2`) reads
  `2 need you · agenthicc` when anything waits, and `agenthicc` otherwise, so
  the state is visible from other tabs and windows.
- The registry emits `agent_needs_input`, `agent_finished` and `agent_failed`
  notification events into PRD-226's delivery pipeline, so local and webhook
  destinations can subscribe to them like other background events.

### 5.6 Acknowledgement and do-not-disturb

- An item is acknowledged when its agent is opened in the hub; acknowledgement
  and the AGX-06 new-activity marks are stored per user next to the index, so
  they survive restarts.
- `/dnd` toggles do-not-disturb for the session: badge counts stay, toasts and
  system notifications stop. `notifications.quiet_hours` in configuration sets
  recurring quiet periods.

## 6. Acceptance criteria

1. A background agent entering `waiting_approval` updates the badge in an open
   TUI within 1 s, without moving the cursor or changing the composer buffer.
2. With a TUI active, no notification text is written directly to stdout or
   stderr (a test captures the terminal stream and checks frame integrity).
3. ← on an empty composer or Ctrl+G opens the hub at the oldest pending item.
4. Toasts during a modal are deferred and coalesced into one line.
5. With focus reporting supported and the terminal unfocused, one system
   notification is emitted per coalesced batch; none while focused.
6. `/dnd` suppresses toasts and system notifications but not the badge.

## 7. Test plan

- Fake inbox and registry changes driving badge and toast renders.
- Terminal-stream integrity test with notifications firing during a streaming
  turn.
- Focus-event decoding tests on POSIX and Windows key decoders.
- Coalescing and quiet-hours unit tests with a fake clock.

## 8. Risks

- **Notification fatigue.** Coalescing, once-per-event announcements and
  do-not-disturb keep volume low.
- **Terminal support varies.** Focus reporting and OSC notifications are
  progressive enhancements; the badge works everywhere.

## Sources

- Claude Code agent view (footer count, title, notifications): <https://code.claude.com/docs/en/agent-view>
- Claude Code subagents (approvals, `/tasks` hint, completion notifications): <https://code.claude.com/docs/en/sub-agents>
- Codex subagents (approval overlay `o`): <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Grok Build background tasks (monitor notifications): <https://docs.x.ai/build/features/background-tasks>
- xterm control sequences (focus events, OSC): <https://invisible-island.net/xterm/ctlseqs/ctlseqs.html>
