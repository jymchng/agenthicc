"""UnifiedInputSession — single CBREAK context for the application lifetime (PRD-62, PRD-74).

One session, one raw_mode context, capability-pipeline dispatch (PRD-74).

Modes:
    IDLE      — full feature set: triggers, history, cursor movement, mode cycling
    STREAMING — reduced: queue messages, interrupt agent, paste, editing, cursor movement

Each mode is a declared list of Capability instances (see capabilities.py).
Adding a new trigger char or a new mode requires no changes here.
"""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.conversation_store import ui_settings
from agenthicc.tui.runtime.mode_manager import ModeManager
from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
from agenthicc.tui.input.capabilities import (
    Capability,
    _ExitSentinel,
    IDLE_CAPABILITIES,
    STREAMING_CAPABILITIES,
    _EXIT,
)

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.trigger import TriggerManager, TriggerResult
    from agenthicc.tui.workspace.appender import ScrollBufferAppender
    from agenthicc.tui.workspace.overlay import OverlayHost

# Re-export for tests/external callers that import from here.
__all__ = ["UnifiedInputSession", "InputMode", "_EXIT"]


class InputMode(Enum):
    IDLE = auto()
    STREAMING = auto()


class UnifiedInputSession:
    """Single CBREAK session for the entire application lifetime.

    raw_mode is entered once at startup via run() and exited at shutdown.
    Mode transitions are synchronous signal updates on AppState.
    """

    def __init__(
        self,
        app_state: AppState,
        command_bus: CommandBus,
        trigger_registry: TriggerManager | None = None,
        mode_manager: ModeManager | None = None,
        overlay_host: OverlayHost | None = None,
        cwd: Path | None = None,
        cfg: AgenthiccConfig | None = None,
        history: list[str] | None = None,
        busy: bool = False,
        clear_after_acceptance: bool = False,
        preserve_submission_text: bool = False,
        on_ready: Callable[[bool], None] | None = None,
    ) -> None:
        self._state: AppState = app_state
        # Set by the /background handoff to end the input loop after detaching.
        self._exit_requested: bool = False
        self._bus: CommandBus = command_bus
        self._registry: TriggerManager | None = trigger_registry
        self._modes: ModeManager = mode_manager or ModeManager()
        self._overlay: OverlayHost | None = overlay_host
        self._cwd: Path = cwd or Path(".")
        self._cfg: AgenthiccConfig | None = cfg
        self._busy = busy
        self._clear_after_acceptance = clear_after_acceptance
        self._preserve_submission_text = preserve_submission_text
        self._on_ready = on_ready

        self._mode: InputMode = InputMode.IDLE
        self._capabilities: list[Capability] = IDLE_CAPABILITIES  # switched by set_mode()
        self._buf: InputBuffer = InputBuffer()
        self._paste: PasteState = PasteState()
        # Keep the caller-owned history list: manager-bound editors use one
        # list per target session so accepted submissions remain navigable
        # when the composer is closed and reopened.
        self._hist: HistoryNavigator = HistoryNavigator(history if history is not None else [])
        self._ctrl_c_count: int = 0
        # The composer action the key being dispatched is bound to (AGX-13).
        self._action: str | None = None
        self._action_rebindable = False
        # When deleting or recalling history last emptied the prompt, and
        # whether a guarded ← has asked for its second press (AGX-05 §5.2).
        self._emptied_at = 0.0
        self._hub_left_armed = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def set_mode(self, mode: InputMode) -> None:
        self._mode = mode
        if mode == InputMode.STREAMING:
            self._ctrl_c_count = 0
        self._capabilities = (
            STREAMING_CAPABILITIES if mode == InputMode.STREAMING else IDLE_CAPABILITIES
        )

    def set_text(self, text: str) -> None:
        """Replace the composer contents without submitting the text.

        Registry overlays use this after a detail-page selection. Keeping the
        mutation here ensures the reactive ``InputState`` is updated before
        the overlay is hidden and redrawn.
        """
        self._buf.set(list(text))
        self._paste.condensed = False
        self._ctrl_c_count = 0
        self._push()

    def set_history(self, history: list[str]) -> None:
        """Install resumed input history after the shared transcript preview loads."""

        self._hist.replace_history(history)

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Block until the user exits (Ctrl+C twice or Ctrl+D on empty)."""
        import asyncio as _asyncio
        from agenthicc.tui.terminal.backend import get_backend

        backend = get_backend()
        if not backend.is_interactive():
            # Non-interactive environment (pipe, CI, redirect, Windows without
            # a console) — exit cleanly so TUISession cancels tasks normally.
            if self._on_ready is not None:
                self._on_ready(False)
            return

        loop = _asyncio.get_event_loop()
        with backend.enter_raw_mode():
            # The inline TUI keeps native selection unless [ui] mouse = "on".
            ui = ui_settings(self._state)
            if ui is not None and ui.mouse_enabled(fullscreen=False):
                backend.set_reporting(mouse=True)
            if self._on_ready is not None:
                self._on_ready(True)
            while True:
                try:
                    # run_in_executor lets the event loop service other tasks
                    # (agent streaming, workspace redraws, tick loop, etc.)
                    key, ch = await loop.run_in_executor(None, backend.read_key)
                except KeyboardInterrupt:
                    self._state.conversation.notification.set(None)
                    return
                except OSError:
                    if self._exit_requested:
                        return
                    continue

                if await self._dispatch(key, ch) is _EXIT:
                    return

    def request_exit(self) -> None:
        """End :meth:`run` now, even while it waits for a key.

        Used when the session hands over without a keystroke of its own —
        switching to another agent from the hub (AGX-05 §5.2). The wake byte
        interrupts the blocking read.
        """
        from agenthicc.tui.terminal.backend import get_backend

        self._exit_requested = True
        get_backend().restore()

    # ── capability pipeline dispatch ──────────────────────────────────────────

    def conditions(self) -> frozenset[str]:
        """The composer conditions key bindings depend on (AGX-13)."""
        flags: set[str] = set()
        if self._mode == InputMode.STREAMING:
            flags.add("running")
        pool_state = self._state.conversation.subagent_pool_state()
        if pool_state is not None:
            flags.add("subagents")
            if pool_state.active:
                flags.add("subagents_running")
        if self._paste.condensed:
            flags.add("paste")
            if self._buf.cursor == self._paste.end:
                flags.add("paste_at_cursor")
        return frozenset(flags)

    async def _dispatch(self, key: Key, ch: str) -> _ExitSentinel | None:
        """Run the active capability list until one consumes the key.

        The key is first resolved against the composer's key bindings; each
        capability then acts on the named action rather than a fixed key.
        """
        keymap = self._state.require_keymap()
        name = key_name(key, ch)
        conditions = self.conditions()
        action = None
        if name == "left" and not self._buf.text and not self._paste.condensed:
            # ← on an empty prompt has its own binding (agents.open_hub).
            action = keymap.resolve("composer", "left_on_empty", conditions)
        if action is None:
            action = keymap.resolve("composer", name, conditions)
        self._action = None if action is None else action.name
        self._action_rebindable = action is not None and action.rebindable
        if self._action != "agents.open_hub":
            self._hub_left_armed = False
        for cap in self._capabilities:
            result = await cap.handle(key, ch, self)
            if result is _EXIT:
                return _EXIT
            if result:
                break
        # A started /background handoff, or a switch to another session, ends
        # the input loop.
        if self._exit_requested:
            return _EXIT
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _push(self) -> None:
        inp = self._state.input
        if self._paste.condensed:
            display_buf, display_cursor = self._paste._condensed_view(self._buf)
            inp.update(
                display_buf,
                display_cursor,
                paste_condensed=True,
                paste_label=self._paste.label,
            )
        else:
            inp.update(list(self._buf.buf), self._buf.cursor)

    def _ctrl_c_sequence(self) -> _ExitSentinel | None:
        self._ctrl_c_count += 1
        if self._ctrl_c_count == 1:
            self._buf.clear()
            self._paste.condensed = False
            self._push()
            self._state.conversation.notification.set("Press Ctrl+C again to exit.")
            return None  # keep looping
        self._state.conversation.notification.set(None)
        from agenthicc.tui.input.renderer import show_exit_hint

        show_exit_hint(self._state.conversation.session_id())
        return _EXIT

    def _prepare_submission(self) -> None:
        """Synchronous pre-submission cleanup — single source of truth.

        Every code path that sends a message must call this before dispatching
        SendMessageCommand.  Adding new cleanup here automatically applies to
        all submission paths (normal Enter, trigger overlay auto-submit, etc.).
        """
        self._buf.clear()
        self._paste.condensed = False
        self._ctrl_c_count = 0
        self._push()

    async def _submit(self, text: str) -> None:
        """Prepare the input state and dispatch SendMessageCommand."""
        self._prepare_submission()
        await self._bus.dispatch_async(SendMessageCommand(text=text))

    def _find_trigger_tail(self) -> tuple[str, list[str], str] | None:
        buf = self._buf.buf
        if not self._registry:
            return None
        for i in range(len(buf) - 1, -1, -1):
            ch = buf[i]
            if ch.isspace():
                return None
            if ch in self._registry.chars:
                pre = buf[:i]
                fragment = "".join(buf[i + 1 :])
                handler = self._registry.get(ch)
                if handler and handler.can_activate(pre):
                    return (ch, pre, fragment)
        return None

    async def _open_trigger_overlay(self, trigger_char: str) -> None:
        initial = list(self._buf.buf) + [trigger_char]
        await self._open_trigger_overlay_with_initial(initial)

    async def _open_trigger_overlay_with_initial(self, initial: list[str]) -> None:
        if self._overlay is None or self._registry is None:
            return
        from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

        def on_complete(result: TriggerResult | None) -> None:
            from agenthicc.tui.trigger import TriggerResult

            # Push to InputState BEFORE hiding the overlay.  hide() triggers
            # _redraw() synchronously; if InputState is still empty at that
            # point, ComposerComponent renders a blank bar and flushes it to
            # the terminal before the correct content can be set.
            if result is not None and isinstance(result, TriggerResult):
                self._buf.set(result.buffer)
                if result.cursor is not None:
                    self._buf.cursor = result.cursor
                self._push()
            else:
                self._push()  # restore pre-trigger content on ESC / cancel
            overlay_host = self._overlay
            if overlay_host is not None:
                overlay_host.hide()  # _redraw fires here; InputState already correct
            if result is not None and isinstance(result, TriggerResult) and result.submit:
                import asyncio

                text = "".join(result.buffer).strip()
                if self._clear_after_acceptance:
                    # Target-bound composers may reject asynchronously (for
                    # example if the worker lease changed while a picker was
                    # open). Keep the selected trigger text editable until
                    # the owning session confirms acceptance.
                    self._buf.set(list(text))
                    self._paste.condensed = False
                    self._push()

                    async def submit_after_target_acceptance() -> None:
                        try:
                            accepted = await self._bus.dispatch_async(SendMessageCommand(text=text))
                        except Exception:  # noqa: BLE001
                            return
                        try:
                            accepted_result = object.__getattribute__(accepted, "ok")
                        except AttributeError:
                            accepted_result = True
                        if accepted_result is False:
                            return
                        self._buf.clear()
                        self._paste.condensed = False
                        self._ctrl_c_count = 0
                        self._push()
                        self._hist.commit(text)

                    asyncio.get_event_loop().create_task(submit_after_target_acceptance())
                    return
                self._prepare_submission()  # single source of truth for cleanup
                asyncio.get_event_loop().create_task(
                    self._bus.dispatch_async(SendMessageCommand(text=text))
                )

        overlay = TriggerPickerOverlay(
            initial_buf=initial,
            registry=self._registry,
            cwd=self._cwd,
            busy=self._mode is InputMode.STREAMING or self._busy,
            paste_state=self._paste,
            on_complete=on_complete,
        )
        self._overlay.show(overlay)

    def print_idle_header(self, appender: ScrollBufferAppender | None) -> None:
        """Print the session info + separator before each new prompt."""
        if appender:
            appender.print_idle_header()
