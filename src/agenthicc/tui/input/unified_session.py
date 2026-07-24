"""UnifiedInputSession — single CBREAK context for the application lifetime (PRD-62, PRD-74).

One session, one raw_mode context, capability-pipeline dispatch (PRD-74).

Modes:
    IDLE      — full feature set: triggers, history, cursor movement, mode cycling
    STREAMING — reduced: queue messages, interrupt agent, paste, basic editing

Each mode is a declared list of Capability instances (see capabilities.py).
Adding a new trigger char or a new mode requires no changes here.
"""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
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
    ) -> None:
        self._state: AppState = app_state
        self._bus: CommandBus = command_bus
        self._registry: TriggerManager | None = trigger_registry
        self._modes: ModeManager = mode_manager or ModeManager()
        self._overlay: OverlayHost | None = overlay_host
        self._cwd: Path = cwd or Path(".")
        self._cfg: AgenthiccConfig | None = cfg

        self._mode: InputMode = InputMode.IDLE
        self._capabilities: list[Capability] = IDLE_CAPABILITIES  # switched by set_mode()
        self._buf: InputBuffer = InputBuffer()
        self._paste: PasteState = PasteState()
        self._hist: HistoryNavigator = HistoryNavigator(history or [])
        self._ctrl_c_count: int = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def set_mode(self, mode: InputMode) -> None:
        self._mode = mode
        if mode == InputMode.STREAMING:
            self._ctrl_c_count = 0
        self._capabilities = (
            STREAMING_CAPABILITIES if mode == InputMode.STREAMING else IDLE_CAPABILITIES
        )

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Block until the user exits (Ctrl+C twice or Ctrl+D on empty)."""
        import asyncio as _asyncio  # noqa: PLC0415
        from agenthicc.tui.terminal.backend import get_backend  # noqa: PLC0415

        backend = get_backend()
        if not backend.is_interactive():
            # Non-interactive environment (pipe, CI, redirect, Windows without
            # a console) — exit cleanly so TUISession cancels tasks normally.
            return

        loop = _asyncio.get_event_loop()
        with backend.enter_raw_mode():
            while True:
                try:
                    # run_in_executor lets the event loop service other tasks
                    # (agent streaming, workspace redraws, tick loop, etc.)
                    key, ch = await loop.run_in_executor(None, backend.read_key)
                except KeyboardInterrupt:
                    self._state.conversation.notification.set(None)
                    return
                except OSError:
                    continue

                if await self._dispatch(key, ch) is _EXIT:
                    return

    # ── capability pipeline dispatch ──────────────────────────────────────────

    async def _dispatch(self, key: Key, ch: str) -> _ExitSentinel | None:
        """Run the active capability list until one consumes the key."""
        for cap in self._capabilities:
            result = await cap.handle(key, ch, self)
            if result is _EXIT:
                return _EXIT
            if result:
                return None
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _push(self) -> None:
        inp = self._state.input
        if self._paste.condensed:
            inp.update(
                list(self._buf.buf),
                self._buf.cursor,
                paste_condensed=True,
                paste_label=self._paste.label,
            )
        else:
            inp.update(list(self._buf.buf), self._buf.cursor)

    def _paste_exit(self) -> None:
        if self._paste.condensed:
            self._paste.expand()

    def _ctrl_c_sequence(self) -> _ExitSentinel | None:
        self._ctrl_c_count += 1
        if self._ctrl_c_count == 1:
            self._buf.clear()
            self._paste.condensed = False
            self._push()
            self._state.conversation.notification.set("Press Ctrl+C again to exit.")
            return None  # keep looping
        self._state.conversation.notification.set(None)
        from agenthicc.tui.input.renderer import show_exit_hint  # noqa: PLC0415

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
        from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay  # noqa: PLC0415

        def on_complete(result: TriggerResult | None) -> None:
            from agenthicc.tui.trigger import TriggerResult  # noqa: PLC0415

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
                import asyncio  # noqa: PLC0415

                text = "".join(result.buffer).strip()
                self._prepare_submission()  # single source of truth for cleanup
                asyncio.get_event_loop().create_task(
                    self._bus.dispatch_async(SendMessageCommand(text=text))
                )

        overlay = TriggerPickerOverlay(
            initial_buf=initial,
            registry=self._registry,
            cwd=self._cwd,
            busy=self._mode is InputMode.STREAMING,
            on_complete=on_complete,
        )
        self._overlay.show(overlay)

    def print_idle_header(self, appender: ScrollBufferAppender | None) -> None:
        """Print the session info + separator before each new prompt."""
        if appender:
            appender.print_idle_header()
