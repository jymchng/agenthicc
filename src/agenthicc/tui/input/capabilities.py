"""Input capability pipeline — PRD-74.

Each Capability is a single-responsibility async key handler.  The dispatcher
tries capabilities in order until one returns _CONSUMED (True) or _EXIT.
Returning _PASS (False) passes the key to the next capability.

The two mode lists (IDLE_CAPABILITIES, STREAMING_CAPABILITIES) are the single
source of truth for what each mode supports.  Adding a new trigger char only
requires registering it in TriggerManager — no changes here.
"""

from __future__ import annotations

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

    When an overlay is active it consumes every key — nothing else runs.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._overlay and session._overlay.active:
            session._overlay.handle_key(key, ch)
            return _CONSUMED
        return _PASS


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
        if key == Key.CTRL_C:
            result = session._ctrl_c_sequence()
            return _EXIT if result is _EXIT else _CONSUMED
        # Any key other than Ctrl+C clears the "Press Ctrl+C again" notification.
        if session._ctrl_c_count > 0:
            session._ctrl_c_count = 0
            session._state.conversation.notification.set(None)
        return _PASS


# ── Ctrl+D (idle) ─────────────────────────────────────────────────────────────


class CtrlDCapability:
    """Ctrl+D — submit non-empty buffer or exit."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.CTRL_D:
            return _PASS
        text = session._buf.text
        if text:
            await session._submit(text)
            return _CONSUMED
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
        if key not in (Key.CTRL_C, Key.ESC):
            return _PASS
        from agenthicc.tui.runtime.commands import InterruptAgentCommand  # noqa: PLC0415

        session._buf.clear()
        session._paste.condensed = False
        session._push()
        await session._bus.dispatch_async(InterruptAgentCommand())
        return _CONSUMED


# ── Trigger ───────────────────────────────────────────────────────────────────


class TriggerCapability:
    """Handles all registered trigger chars (@, /, #, !) via TriggerManager.resolve().

    Uses resolve() which normalises Key.AT → "@" in one place.  This capability
    works identically in IDLE and STREAMING — fixing the bug where @ was silently
    dropped in streaming because Key.AT never matched `case Key.CHAR if ch:`.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        tch = session._registry.resolve(key, ch) if session._registry else None
        if tch is None:
            return _PASS
        handler = session._registry.get(tch)
        can = handler.can_activate(session._buf.buf[: session._buf.cursor]) if handler else False
        if can:
            await session._open_trigger_overlay(tch)
        else:
            session._paste_exit()
            session._buf.insert(tch)
            session._push()
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
            import shutil  # noqa: PLC0415

            cols = shutil.get_terminal_size((80, 24)).columns
            session._paste.apply(session._buf, ch, cols)
            session._push()
            return _CONSUMED
        if key == Key.CTRL_V:
            session._paste.expand()
            session._push()
            return _CONSUMED
        return _PASS


# ── Submit ────────────────────────────────────────────────────────────────────


class SubmitCapability:
    """Handles Enter.

    commit_history=True  → idle mode: commit to HistoryNavigator before sending.
    commit_history=False → streaming mode: dispatch without history commit.
    """

    def __init__(self, commit_history: bool = False) -> None:
        self._commit_history = commit_history

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.ENTER:
            return _PASS
        from agenthicc.tui.runtime.commands import SendMessageCommand  # noqa: PLC0415

        text = session._buf.text.strip()
        session._buf.clear()
        session._paste.condensed = False
        session._ctrl_c_count = 0
        session._push()
        if text:
            if self._commit_history:
                session._hist.commit(text)
            await session._bus.dispatch_async(SendMessageCommand(text=text))
        return _CONSUMED


# ── Newline ───────────────────────────────────────────────────────────────────


class NewlineCapability:
    """Ctrl+Enter / Ctrl+J — inserts a literal newline for multi-line input."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.CTRL_ENTER:
            return _PASS
        session._paste_exit()
        session._buf.insert("\n")
        session._push()
        return _CONSUMED


# ── Backspace ─────────────────────────────────────────────────────────────────


class BackspaceCapability:
    """Backspace — deletes the character before the cursor.

    When the cursor is at the end of the buffer and the last token is a
    committed trigger token (e.g. @docs/index.md), re-enters the trigger
    overlay so the user can refine the selection.
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.BACKSPACE:
            return _PASS
        if session._paste.condensed:
            session._paste.backspace(session._buf)
        elif session._buf.cursor == len(session._buf):
            tail = session._find_trigger_tail()
            if tail:
                tch, tpre, tfrag = tail
                handler = session._registry.get(tch) if session._registry else None
                if handler:
                    session._buf.set(tpre)
                    await session._open_trigger_overlay_with_initial(
                        list(tpre) + [tch] + list(tfrag)
                    )
                    return _CONSUMED
            session._buf.delete_before()
        else:
            session._buf.delete_before()
        session._push()
        return _CONSUMED


# ── Clear ─────────────────────────────────────────────────────────────────────


class ClearCapability:
    """Ctrl+U — clears the entire buffer."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.CTRL_U:
            return _PASS
        session._buf.clear()
        session._paste.condensed = False
        session._push()
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
        match key:
            case Key.LEFT:
                session._paste_exit()
                session._buf.move_left()
            case Key.RIGHT:
                session._paste_exit()
                session._buf.move_right()
            case Key.HOME:
                session._paste_exit()
                session._buf.move_home()
            case Key.END:
                session._paste_exit()
                session._buf.move_end()
            case _:
                return _PASS
        session._push()
        return _CONSUMED


# ── History ───────────────────────────────────────────────────────────────────


class HistoryCapability:
    """Up / Down — navigates command history when at the first/last buffer line."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        match key:
            case Key.UP:
                session._paste_exit()
                if not session._buf.move_up():
                    result = session._hist.up(session._buf.buf)
                    if result is not None:
                        session._buf.set(result)
                        session._paste.condensed = False
            case Key.DOWN:
                session._paste_exit()
                if not session._buf.move_down():
                    result = session._hist.down(session._buf.buf)
                    if result is not None:
                        session._buf.set(result)
                        session._paste.condensed = False
            case _:
                return _PASS
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
        if key != Key.SHIFT_TAB:
            return _PASS
        new_mode = session._modes.cycle()  # writes app_state.active_mode internally
        session._state.conversation.notify_transient(f"❖ Switched to {new_mode.name} mode")
        return _CONSUMED


# ── Insert (catch-all) ────────────────────────────────────────────────────────


class InsertCapability:
    """Fallback — inserts regular Key.CHAR characters.  Must always be LAST.

    Also handles re-entering a trigger overlay when the user types into an
    existing committed trigger token (e.g. continuing to type after @docs).
    """

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if key != Key.CHAR or not ch:
            return _PASS
        session._paste_exit()
        # Re-enter trigger overlay when typing into an existing trigger token.
        if not ch.isspace() and session._buf.cursor == len(session._buf):
            tail = session._find_trigger_tail()
            if tail:
                tch, tpre, tfrag = tail
                handler = session._registry.get(tch) if session._registry else None
                if handler:
                    session._buf.set(tpre)
                    await session._open_trigger_overlay_with_initial(
                        list(tpre) + [tch] + list(tfrag) + [ch]
                    )
                    return _CONSUMED
        session._buf.insert(ch)
        session._push()
        return _CONSUMED


# ── Mode declarations ─────────────────────────────────────────────────────────

#: Full feature set: triggers, history, cursor movement, mode cycling.
IDLE_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    CtrlCCapability(),  # first: resets counter on any non-Ctrl+C key
    CtrlDCapability(),
    PasteCapability(),
    TriggerCapability(),  # @, /, #, ! — before InsertCapability
    SubmitCapability(commit_history=True),
    NewlineCapability(),
    BackspaceCapability(),
    ClearCapability(),
    CursorCapability(),
    HistoryCapability(),
    ModeCycleCapability(),
    InsertCapability(),  # fallback — must be last
]

#: Reduced set: queue messages, interrupt agent, paste, basic editing.
STREAMING_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    InterruptCapability(),  # Ctrl+C / ESC → cancel agent
    PasteCapability(),
    TriggerCapability(),  # @, / etc. — same as idle
    SubmitCapability(commit_history=False),
    NewlineCapability(),
    BackspaceCapability(),
    ClearCapability(),
    ModeCycleCapability(),  # Shift+Tab — mode switch takes effect on next tool call
    InsertCapability(),  # fallback — must be last
]
