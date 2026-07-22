# TUI architecture (compatibility pointer)

The current TUI architecture is documented in
[guides/tui.md](guides/tui.md) and [guides/architecture.md](guides/architecture.md).

The implementation is the Rich Live workspace under `src/agenthicc/tui/workspace/`
plus the reactive state in `conversation_store.py`, the capability-driven input
session in `tui/input/`, and platform backends in `tui/terminal/`.

Older versions of this file described a prompt-toolkit `TranscriptModel`,
`render_frame_ansi`, and `TUIEventAdapter`. Those modules are not present in the
current source tree. This pointer remains so direct links fail safely while the
repository finishes the documentation migration in PRD-138.
