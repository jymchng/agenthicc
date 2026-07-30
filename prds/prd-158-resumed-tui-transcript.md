---
title: "PRD-158: Display Resumed TUI Transcript"
status: Implemented
version: 1.0.0
created: 2026-07-30
scope: Replay the prior session's conversation transcript in the resumed TUI
related_prds:
  - PRD-67
  - PRD-129
tags:
  - tui
  - resume
  - transcript
---

# PRD-158 — Display Resumed TUI Transcript

## Requirement

Opening the TUI against an existing session must display the previous
conversation in the scroll area before accepting new input. Historical events
must use the same renderers and ordering as live events, and replay must not
append duplicate records to the session log.

## Implementation

`SessionContext.resumed` identifies an existing-session launch. After
`Workspace.start()` mounts the Live block, `TUISession.run()` loads the
conversation events with `rendered=False` and calls
`Workspace.replay_transcript()`. `ScrollBufferAppender.replay()` queues those
events directly to the appender rather than calling
`ConversationStore.append_event()`. A single event-loop yield flushes the
transcript before the input loop starts.

The normal `SessionEventLog.load()` behavior remains unchanged for state and
metric restoration. The presentation replay path is separate so its events do
not notify persistence or client-projection subscribers. Corrupt log lines
continue to be skipped by the existing loader.

## Acceptance evidence

- Historical user and assistant events render through the normal appender.
- Resume replay occurs after the Live workspace starts and before input.
- Replay does not notify the reactive store or write duplicate log records.
- Default event loading remains marked rendered for non-visual restoration.
- `tests/unit/test_resume_transcript.py` covers renderer output, loader mode,
  and resumed TUI ordering.
