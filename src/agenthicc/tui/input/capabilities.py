"""Input capability pipeline — PRD-74.

Each Capability is a single-responsibility async key handler.  The dispatcher
tries capabilities in order until one returns _CONSUMED (True) or _EXIT.
Returning _PASS (False) passes the key to the next capability.

The two mode lists (IDLE_CAPABILITIES, STREAMING_CAPABILITIES) are the single
source of truth for what each mode supports.  Adding a new trigger char only
requires registering it in TriggerManager — no changes here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from agenthicc.tui.cbreak_reader import Key

if TYPE_CHECKING:
    from agenthicc.tui.input.unified_session import UnifiedInputSession


# ── sentinel & return-value types ────────────────────────────────────────────


class _ExitSentinel:
    """Singleton sentinel — returned by a capability to signal application exit."""

    __slots__ = ()


#: Return values from handle()
_CONSUMED: bool = True  # key was handled; stop pipeline
_PASS: bool = False  # key not handled; try next capability
_EXIT: _ExitSentinel = _ExitSentinel()  # application exit requested

#: Union of all values a capability handle() method may return.
CapabilityResult = bool | _ExitSentinel


# ── Capability Protocol ───────────────────────────────────────────────────────


class Capability(Protocol):
    """Structural protocol for a single input capability handler.

    Defined here alongside the concrete capability classes and the mode lists
    that hold them, so IDLE_CAPABILITIES / STREAMING_CAPABILITIES can be typed
    as ``list[Capability]`` without any circular imports.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult: ...


# ── Overlay ───────────────────────────────────────────────────────────────────


class OverlayCapability:
    """Routes keystrokes to the active overlay.  Always first in any mode.

    An overlay consumes every key — except the ``/``/``@`` picker, which
    claims only navigation and choice (TUX-04 §2.2): every other key passes on
    to the editing capabilities and the picker follows the edited prompt.
    The Ctrl+R search shows a match without touching the prompt.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        host = session._overlay
        if host is None or not host.active:
            return _PASS
        from agenthicc.tui.input.history_search import HistorySearchOverlay
        from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

        widget = host.widget
        if isinstance(widget, TriggerPickerOverlay):
            return _CONSUMED if await session._picker_key(widget, key, ch) else _PASS
        if isinstance(widget, HistorySearchOverlay):
            session._search_key(widget, key, ch)
            return _CONSUMED
        host.handle_key(key, ch)
        return _CONSUMED


# ── Condensed-paste escape ───────────────────────────────────────────────────


class CondensedPasteEscapeCapability:
    """Delete a condensed paste when ESC is directly after its placeholder.

    The underlying buffer cursor must be exactly at the end of the hidden
    paste range. A typed suffix therefore prevents this shortcut, preserving
    normal ESC behavior and leaving the suffix editable.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.drop_paste":
            return _PASS
        session._paste.delete_condensed(session._buf)
        session._push()
        return _CONSUMED


# ── Ctrl+C (idle) ─────────────────────────────────────────────────────────────


class CtrlCCapability:
    """Double-Ctrl+C exit sequence for idle mode.

    As a side effect, resets the Ctrl+C counter for any non-Ctrl+C key.
    Must run before other capabilities so the counter is always cleared.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action == "composer.quit":
            result = session._ctrl_c_sequence()
            return _EXIT if result is _EXIT else _CONSUMED
        # Any key other than Ctrl+C clears the "Press Ctrl+C again" notification.
        if session._ctrl_c_count > 0:
            session._ctrl_c_count = 0
            session._state.conversation.notification.set(None)
        return _PASS


# ── Ctrl+D (idle) ─────────────────────────────────────────────────────────────


class CtrlDCapability:
    """Ctrl+D on an empty prompt exits (on a non-empty one it deletes forward)."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.eof" or session._buf.text:
            return _PASS
        return _EXIT


# ── Interrupt (streaming) ─────────────────────────────────────────────────────


class InterruptCapability:
    """Ctrl+C / ESC in streaming mode — cancels the running agent."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action not in ("composer.interrupt", "composer.cancel"):
            return _PASS
        from agenthicc.tui.runtime.commands import InterruptAgentCommand

        pause = session._action == "composer.interrupt"
        session._buf.clear()
        session._paste.condensed = False
        session._push()
        await session._bus.dispatch_async(
            InterruptAgentCommand(
                source="escape" if pause else "ctrl_c",
                disposition="pause" if pause else "cancel",
            )
        )
        return _CONSUMED


# ── Trigger ───────────────────────────────────────────────────────────────────


class TriggerCapability:
    """Handles all registered trigger chars (@, /, #, !) via TriggerManager.resolve().

    The trigger character is typed into the prompt like any other; when it
    can start a trigger there, the picker opens on it (TUX-04 §2.2). Uses
    resolve(), which normalises Key.AT → "@" in one place, so this works
    identically in IDLE and STREAMING.

    The picker opens only while the cursor is at the end of the prompt: a
    trigger typed while editing an earlier position is ordinary text (Home
    then ``/`` must not turn the rest of the line into a command). With a
    picker already open, a trigger character is fragment text (``@@user``,
    ``@src/``).
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        registry = session._registry
        if registry is None:
            return _PASS
        tch = registry.resolve(key, ch)
        if tch is None:
            return _PASS
        buffer = session._buf
        session._paste.insert(buffer, tch)
        session._push()
        if session._picker is None and buffer.cursor == len(buffer):
            anchor = buffer.cursor - 1
            handler = registry.get(tch)
            if handler is not None and handler.can_activate(buffer.buf[:anchor]):
                session._open_picker(anchor)
        return _CONSUMED


# ── Paste ─────────────────────────────────────────────────────────────────────


class PasteCapability:
    """Handles bracketed paste (Key.PASTE) and Ctrl+V expansion."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key == Key.PASTE and ch:
            import shutil

            cols = shutil.get_terminal_size((80, 24)).columns
            session._paste.apply(session._buf, ch, cols)
            session._push()
            return _CONSUMED
        if session._action == "composer.expand_paste":
            session._paste.expand()
            session._push()
            return _CONSUMED
        return _PASS


# ── Submit ────────────────────────────────────────────────────────────────────


class SubmitCapability:
    """Handles Enter.

    commit_history=True  → idle mode: commit to HistoryNavigator before sending.
    commit_history=False → streaming mode: dispatch without history commit
    (the message is queued for after the turn).
    """

    def __init__(self, commit_history: bool = False) -> None:
        self._commit_history = commit_history

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.submit":
            return _PASS
        await session._submit_buffer(commit_history=self._commit_history)
        return _CONSUMED


class InterjectCapability:
    """Ctrl+Enter / Alt+Enter during a turn: deliver at its next tool boundary (TUX-04 §2.6)."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.interject":
            return _PASS
        await session._submit_buffer(commit_history=False, interject=True)
        return _CONSUMED


# ── Newline ───────────────────────────────────────────────────────────────────


class NewlineCapability:
    """Shift+Enter / Ctrl+J (and Ctrl+Enter / Alt+Enter while idle) — a line break."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action not in ("composer.newline", "composer.newline_alt"):
            return _PASS
        session._paste.insert(session._buf, "\n")
        session._push()
        return _CONSUMED


# ── Backspace ─────────────────────────────────────────────────────────────────


class BackspaceCapability:
    """Backspace — deletes the character before the cursor.

    When the cursor is at the end of the buffer and the deletion leaves it
    after a committed trigger token (e.g. ``@docs/index.m``), the picker opens
    again so the user can refine the selection.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.delete_back":
            return _PASS
        buffer = session._buf
        if session._paste.condensed:
            if buffer.cursor == session._paste.end:
                # The rendered cursor is immediately after the paste's ``]``.
                # Treat that boundary as an intentional whole-paste delete;
                # suffix text to the right remains intact.
                session._paste.delete_condensed(buffer)
            else:
                session._paste.backspace(buffer)
        else:
            buffer.delete_before()
            if session._picker is None and buffer.cursor == len(buffer):
                anchor = session._find_trigger_anchor()
                if anchor is not None:
                    session._open_picker(anchor)
        _note_emptied(session)
        session._push()
        return _CONSUMED


# ── Readline editing (TUX-04 §2.1) ────────────────────────────────────────────


class EditingCapability:
    """Kills, yank, delete forward, undo and redo (TUX-04 §2.1).

    Kills go into the buffer's kill ring; a condensed paste keeps its place
    when text before it is deleted and shows in full when a deletion reaches
    into it.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        buffer = session._buf
        paste = session._paste
        removed: tuple[int, int] | None = None
        match session._action:
            case "composer.clear":
                removed = buffer.kill_to_start()
                if not buffer.text:
                    paste.condensed = False
            case "composer.kill_to_end":
                removed = buffer.kill_to_end()
            case "composer.delete_word_back":
                removed = buffer.delete_word_before()
            case "composer.delete_word_forward":
                removed = buffer.delete_word_after()
            case "composer.delete_forward":
                cursor = buffer.cursor
                if paste.condensed and cursor == paste.start and paste.end > paste.start:
                    paste.delete_condensed(buffer)
                elif buffer.delete_after():
                    removed = (cursor, cursor + 1)
            case "composer.yank":
                inserted = buffer.yank()
                if inserted is not None:
                    paste.note_inserted(*inserted)
            case "composer.undo":
                buffer.undo()
            case "composer.redo":
                buffer.redo()
            case _:
                return _PASS
        if removed is not None:
            paste.note_deleted(*removed)
        _note_emptied(session)
        session._push()
        return _CONSUMED


class HistorySearchCapability:
    """Ctrl+R — reverse incremental history search (TUX-04 §2.3)."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.history_search":
            return _PASS
        session._open_history_search()
        return _CONSUMED


class ExternalEditorCapability:
    """Ctrl+X Ctrl+E (or Ctrl+X alone) — edit the prompt in $EDITOR (TUX-04 §2.4)."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.external_editor":
            return _PASS
        session._start_chord()
        return _CONSUMED


class RedrawCapability:
    """Ctrl+L — redraw the live region."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.redraw":
            return _PASS
        session.terminal_hooks.repaint()
        return _CONSUMED


class QueueFocusCapability:
    """↑ on an empty prompt during a turn — select a queued message (TUX-04 §2.6)."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.queue_focus":
            return _PASS
        session._focus_queue()
        return _CONSUMED


# ── Cursor movement ───────────────────────────────────────────────────────────


class CursorCapability:
    """Left / Right / Home / End — moves the insertion cursor."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        match session._action:
            case "composer.left":
                session._paste.move_left(session._buf)
            case "composer.right":
                session._paste.move_right(session._buf)
            case "composer.home":
                session._paste.move_home(session._buf)
            case "composer.end":
                session._paste.move_end(session._buf)
            case "composer.word_left":
                session._buf.word_left()
                _out_of_paste(session, forward=False)
            case "composer.word_right":
                session._buf.word_right()
                _out_of_paste(session, forward=True)
            case _:
                return _PASS
        session._push()
        return _CONSUMED


def _out_of_paste(session: UnifiedInputSession, *, forward: bool) -> None:
    """Keep a word move out of a condensed paste's hidden text."""
    paste = session._paste
    if paste.condensed and paste.start < session._buf.cursor < paste.end:
        session._buf.cursor = paste.end if forward else paste.start


# ── History ───────────────────────────────────────────────────────────────────


class HistoryCapability:
    """Up / Down — navigates command history when at the first/last buffer line."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        match session._action:
            case "composer.history_previous":
                moved = session._paste.move_up(session._buf)
                if not moved:
                    result = session._hist.up(session._buf.buf)
                    if result is not None:
                        session._buf.set(result)
                        session._paste.condensed = False
            case "composer.history_next":
                moved = session._paste.move_down(session._buf)
                if not moved:
                    result = session._hist.down(session._buf.buf)
                    if result is not None:
                        session._buf.set(result)
                        session._paste.condensed = False
            case _:
                return _PASS
        _note_emptied(session)
        session._push()
        return _CONSUMED


# ── Mode cycling ──────────────────────────────────────────────────────────────


class ModeCycleCapability:
    """Shift+Tab — cycles through registered input modes.

    ModeManager.cycle() writes AppState.active_mode internally (PRD-75).
    This capability only needs to show the notification.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.mode_cycle":
            return _PASS
        new_mode = session._modes.cycle()  # writes app_state.active_mode internally
        session._state.conversation.notify_transient(f"❖ Switched to {new_mode.name} mode")
        return _CONSUMED


# ── Agents hub ────────────────────────────────────────────────────────────────

#: A ← this soon after the prompt was emptied needs a second press (AGX-05 §5.2).
HUB_GUARD_S = 2.0


def _note_emptied(session: UnifiedInputSession) -> None:
    """Remember when deleting or recalling history left the prompt empty."""
    if not session._buf.text:
        session._emptied_at = time.monotonic()


class AgentsHubCapability:
    """Ctrl+G, or ← on an empty prompt, opens the agents hub (AGX-05 §5.2).

    ← right after a deletion or history recall emptied the prompt only says
    ``Press ← again to open agents``; the second ← opens it. An editor without
    a hub (the hub's own input composer) passes the key on.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "agents.open_hub":
            return _PASS
        from agenthicc.tui.runtime.commands import OpenAgentsHubCommand

        if not session._bus.handles(OpenAgentsHubCommand):
            return _PASS
        if (
            key is Key.LEFT
            and not session._hub_left_armed
            and time.monotonic() - session._emptied_at < HUB_GUARD_S
        ):
            session._hub_left_armed = True
            session._state.conversation.notify_transient("Press ← again to open agents")
            return _CONSUMED
        session._hub_left_armed = False
        await session._bus.dispatch_async(OpenAgentsHubCommand())
        return _CONSUMED


class AgentCycleCapability:
    """Tab talks to the next primary agent (AGX-09 §5.3); Shift+Tab is the mode."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "agents.cycle":
            return _PASS
        from agenthicc.tui.runtime.commands import CycleAgentCommand

        if not session._bus.handles(CycleAgentCommand):
            return _PASS
        await session._bus.dispatch_async(CycleAgentCommand())
        return _CONSUMED


class SubagentsCapability:
    """Ctrl+O moves focus to the subagent panel while it is shown (AGX-08 §5.2)."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "subagents.focus":
            return _PASS
        from agenthicc.tui.runtime.commands import OpenSubagentsCommand

        if not session._bus.handles(OpenSubagentsCommand):
            return _PASS
        await session._bus.dispatch_async(OpenSubagentsCommand())
        return _CONSUMED


class BackgroundSubagentsCapability:
    """Ctrl+B detaches a live subagent pool from its foreground tool call."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "subagents.background":
            return _PASS
        from agenthicc.tui.runtime.commands import BackgroundSubagentsCommand

        if not session._bus.handles(BackgroundSubagentsCommand):
            return _PASS
        await session._bus.dispatch_async(BackgroundSubagentsCommand())
        return _CONSUMED


# ── Mouse ─────────────────────────────────────────────────────────────────────


class MouseCapability:
    """Mouse reports ([ui] mouse = "on"): the wheel moves the cursor by a line.

    In a prompt taller than its viewport this scrolls the visible rows; it
    never recalls history. Clicks are not used in the inline composer.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.MOUSE:
            return _PASS
        from agenthicc.tui.cbreak_reader import MouseEvent

        event = MouseEvent.decode(ch)
        if event is not None and event.wheel < 0:
            session._paste.move_up(session._buf)
            session._push()
        elif event is not None and event.wheel > 0:
            session._paste.move_down(session._buf)
            session._push()
        return _CONSUMED


# ── Insert (catch-all) ────────────────────────────────────────────────────────


class InsertCapability:
    """Fallback — inserts regular Key.CHAR characters.  Must always be LAST.

    Typing at the end of a committed trigger token (continuing after
    ``@docs``) opens its picker again.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.CHAR or not ch or session._action_rebindable:
            # A character the user bound to an action is not typed; the fixed
            # trigger characters ("/", "@") still are.
            return _PASS
        buffer = session._buf
        session._paste.insert(buffer, ch)
        session._push()
        if session._picker is None and not ch.isspace() and buffer.cursor == len(buffer):
            anchor = session._find_trigger_anchor()
            if anchor is not None:
                session._open_picker(anchor)
        return _CONSUMED


# ── Mode declarations ─────────────────────────────────────────────────────────

#: Full feature set: triggers, history, cursor movement, mode cycling.
IDLE_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    CondensedPasteEscapeCapability(),
    CtrlCCapability(),  # first: resets counter on any non-Ctrl+C key
    CtrlDCapability(),
    PasteCapability(),
    TriggerCapability(),  # @, /, #, ! — before InsertCapability
    SubmitCapability(commit_history=True),
    NewlineCapability(),
    BackspaceCapability(),
    EditingCapability(),
    CursorCapability(),
    HistoryCapability(),
    HistorySearchCapability(),
    ExternalEditorCapability(),
    RedrawCapability(),
    ModeCycleCapability(),
    AgentsHubCapability(),
    SubagentsCapability(),
    AgentCycleCapability(),
    MouseCapability(),
    InsertCapability(),  # fallback — must be last
]

#: During a turn: queue or interject messages, interrupt, and the same editing.
STREAMING_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    CondensedPasteEscapeCapability(),
    InterruptCapability(),  # Ctrl+C / ESC → cancel agent
    PasteCapability(),
    TriggerCapability(),  # @, / etc. — same as idle
    SubmitCapability(commit_history=False),
    InterjectCapability(),
    NewlineCapability(),
    BackspaceCapability(),
    EditingCapability(),
    CursorCapability(),  # Cursor movement remains available while queued input is edited.
    QueueFocusCapability(),
    HistorySearchCapability(),
    ExternalEditorCapability(),
    RedrawCapability(),
    ModeCycleCapability(),  # Shift+Tab — mode switch takes effect on next tool call
    AgentsHubCapability(),  # the hub opens over a running turn too
    SubagentsCapability(),  # workers run while their turn does
    BackgroundSubagentsCapability(),
    AgentCycleCapability(),  # the choice applies from the next turn
    MouseCapability(),
    InsertCapability(),  # fallback — must be last
]
