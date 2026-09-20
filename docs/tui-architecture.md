# TUI architecture (compatibility pointer)

The current TUI architecture is documented in
[guides/tui.md](guides/tui.md) and [guides/architecture.md](guides/architecture.md).

The implementation is the Rich Live workspace under `src/agenthicc/tui/workspace/`
plus the reactive state in `conversation_store.py`, the capability-driven input
session in `tui/input/`, and platform backends in `tui/terminal/`.

## Schedule-job overlay

The `/loops` command is a TUI-only management surface for the durable
session-scoped loop records owned by `src/agenthicc/runners/loop_scheduler.py`.
`src/agenthicc/tui/workspace/overlays/loops.py` renders a paginated Rich table
of valid `loop.json` records. The overlay owns only selection and keyboard
interaction; it never invokes a provider or workflow runner directly.

`Enter` delegates run-now to `LoopManager`, which wakes the current session's
normal scheduler. A foreign live session remains protected by its owner lease;
an unowned foreign record is marked due for its next explicit resume. `d` then
`Enter` delegates deletion with an ID precondition, so a stale table cannot
delete a replacement. The overlay redacts and bounds payload previews and
closes after a run request so the normal session status remains visible.

This preserves the TUI ownership boundary: scheduling state remains in the
runner, persistence remains in the session directory, and the overlay is only
a projection and input adapter. See [Slash commands](usage/06-commands.md)
and [Storage](reference/storage.md) for the user-facing and persistence
contracts.

Older versions of this file described a prompt-toolkit `TranscriptModel`,
`render_frame_ansi`, and `TUIEventAdapter`. Those modules are not present in the
current source tree. This pointer remains so direct links fail safely while the
repository finishes the documentation migration in PRD-138.
