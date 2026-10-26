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
    """Ctrl+D — submit non-empty buffer or exit."""

    async def handle(
        self,
        key: Key,
        ch: str,
        session: UnifiedInputSession,
    ) -> CapabilityResult:
        if session._action != "composer.eof":
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

    Uses resolve() which normalises Key.AT → "@" in one place.  This capability
    works identically in IDLE and STREAMING — fixing the bug where @ was silently
    dropped in streaming because Key.AT never matched `case Key.CHAR if ch:`.

    The picker currently models a trigger as the suffix of the buffer and is
    therefore only opened while the insertion cursor is at the end.  A trigger
    typed while editing an earlier position is ordinary text: opening a picker
    there would lose the character because the overlay cannot represent the
    untouched suffix correctly.  This also makes Home/Left followed by ``/``
    behave like every other literal character.
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
        handler = registry.get(tch)
        can = handler.can_activate(session._buf.buf[: session._buf.cursor]) if handler else False
        if can and session._buf.cursor == len(session._buf):
            await session._open_trigger_overlay(tch)
        else:
            session._paste.insert(session._buf, tch)
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
        if session._action != "composer.submit":
            return _PASS
        from agenthicc.tui.runtime.commands import SendMessageCommand

        raw_text = session._buf.text
        has_text = bool(raw_text.strip())
        text = raw_text if session._preserve_submission_text else raw_text.strip()
        try:
            clear_after_acceptance = object.__getattribute__(session, "_clear_after_acceptance")
        except AttributeError:
            clear_after_acceptance = False
        if clear_after_acceptance and has_text:
            result = await session._bus.dispatch_async(SendMessageCommand(text=text))
            try:
                accepted = object.__getattribute__(result, "ok")
            except AttributeError:
                accepted = True
            if accepted is False:
                return _CONSUMED
            session._buf.clear()
            session._paste.condensed = False
            session._ctrl_c_count = 0
            session._push()
            if self._commit_history:
                session._hist.commit(text)
            return _CONSUMED
        session._buf.clear()
        session._paste.condensed = False
        session._ctrl_c_count = 0
        session._push()
        if has_text:
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
        if session._action != "composer.newline":
            return _PASS
        session._paste.insert(session._buf, "\n")
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
        if session._action != "composer.delete_back":
            return _PASS
        if session._paste.condensed:
            if session._buf.cursor == session._paste.end:
                # The rendered cursor is immediately after the paste's ``]``.
                # Treat that boundary as an intentional whole-paste delete;
                # suffix text to the right remains intact.
                session._paste.delete_condensed(session._buf)
            else:
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
        _note_emptied(session)
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
        if session._action != "composer.clear":
            return _PASS
        session._buf.clear()
        session._paste.condensed = False
        _note_emptied(session)
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
        match session._action:
            case "composer.left":
                session._paste.move_left(session._buf)
            case "composer.right":
                session._paste.move_right(session._buf)
            case "composer.home":
                session._paste.move_home(session._buf)
            case "composer.end":
                session._paste.move_end(session._buf)
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

    Also handles re-entering a trigger overlay when the user types into an
    existing committed trigger token (e.g. continuing to type after @docs).
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
        session._paste.insert(session._buf, ch)
        session._push()
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
    ClearCapability(),
    CursorCapability(),
    HistoryCapability(),
    ModeCycleCapability(),
    AgentsHubCapability(),
    SubagentsCapability(),
    MouseCapability(),
    InsertCapability(),  # fallback — must be last
]

#: Reduced set: queue messages, interrupt agent, paste, editing, cursor movement.
STREAMING_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    CondensedPasteEscapeCapability(),
    InterruptCapability(),  # Ctrl+C / ESC → cancel agent
    PasteCapability(),
    TriggerCapability(),  # @, / etc. — same as idle
    SubmitCapability(commit_history=False),
    NewlineCapability(),
    BackspaceCapability(),
    ClearCapability(),
    CursorCapability(),  # Cursor movement remains available while queued input is edited.
    ModeCycleCapability(),  # Shift+Tab — mode switch takes effect on next tool call
    AgentsHubCapability(),  # the hub opens over a running turn too
    SubagentsCapability(),  # workers run while their turn does
    MouseCapability(),
    InsertCapability(),  # fallback — must be last
]
