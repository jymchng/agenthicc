"""UnifiedInputSession — single CBREAK context for the application lifetime (PRD-62 §2).

Replaces the separate IdleInputSession + StreamingSession pair that caused
the raw_mode nesting race.  One session, one raw_mode context, two modes.

Modes:
    IDLE      — full feature set: triggers, history, cursor movement, mode cycling
    STREAMING — reduced: queue messages, interrupt agent, paste, basic editing
"""
from __future__ import annotations

import asyncio
import sys
from enum import Enum, auto
from pathlib import Path
from typing import Any

from agenthicc.tui.cbreak_reader import Key, raw_mode, read_key
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.runtime.mode_manager import ModeManager, build_mode_str
from agenthicc.tui.runtime.commands import (
    CommandBus, SendMessageCommand, InterruptAgentCommand,
)

# Sentinel: dispatch methods return this to exit run()
_EXIT = object()


class InputMode(Enum):
    IDLE      = auto()
    STREAMING = auto()


class UnifiedInputSession:
    """Single CBREAK session for the entire application lifetime.

    raw_mode is entered once at startup via run() and exited at shutdown.
    Mode transitions are synchronous signal updates on AppState.
    """

    def __init__(
        self,
        app_state: Any,
        command_bus: CommandBus,
        trigger_registry: Any | None = None,
        mode_manager: ModeManager | None = None,
        overlay_host: Any | None = None,
        cwd: Path | None = None,
        cfg: Any = None,
        history: list[str] | None = None,
    ) -> None:
        self._state    = app_state
        self._bus      = command_bus
        self._registry = trigger_registry
        self._modes    = mode_manager or ModeManager()
        self._overlay  = overlay_host
        self._cwd      = cwd or Path(".")
        self._cfg      = cfg

        self._mode     = InputMode.IDLE
        self._buf      = InputBuffer()
        self._paste    = PasteState()
        self._hist     = HistoryNavigator(history or [])
        self._ctrl_c_count = 0
        self._mode_notification: Any = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def set_mode(self, mode: InputMode) -> None:
        self._mode = mode
        if mode == InputMode.STREAMING:
            self._ctrl_c_count = 0

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Block until the user exits (Ctrl+C twice or Ctrl+D on empty).
        Never returns during normal operation.
        """
        import asyncio as _asyncio  # noqa: PLC0415
        fd = sys.stdin.fileno()
        loop = _asyncio.get_event_loop()
        with raw_mode(fd):
            while True:
                try:
                    # run_in_executor lets the event loop process other tasks
                    # (agent streaming, workspace redraws, tick loop, etc.)
                    # while waiting for the next keystroke.  Without this,
                    # read_key(fd) blocks the entire event loop and ESC/Ctrl+C
                    # during an agent run are never received.
                    key, ch = await loop.run_in_executor(None, read_key, fd)
                except KeyboardInterrupt:
                    self._state.conversation.notification.set(None)
                    return
                except OSError:
                    continue

                # Route to overlay first if one is active
                if self._overlay and self._overlay.active:
                    self._overlay.handle_key(key, ch)
                    continue

                ret = (
                    await self._dispatch_streaming(key, ch)
                    if self._mode == InputMode.STREAMING
                    else await self._dispatch_idle(key, ch)
                )

                if ret is _EXIT:
                    return

    # ── streaming dispatch ────────────────────────────────────────────────────

    async def _dispatch_streaming(self, key: Key, ch: str) -> object:
        match key:
            case Key.CTRL_C | Key.ESC:
                self._buf.clear()
                self._paste.condensed = False
                self._push()
                await self._bus.dispatch_async(InterruptAgentCommand())

            case Key.ENTER:
                text = self._buf.text.strip()
                self._buf.clear()
                self._paste.condensed = False
                self._push()
                if text:
                    await self._bus.dispatch_async(SendMessageCommand(text=text))

            case Key.PASTE if ch:
                import shutil  # noqa: PLC0415
                cols = shutil.get_terminal_size((80, 24)).columns
                self._paste.apply(self._buf, ch, cols)
                self._push()

            case Key.CTRL_V:
                self._paste.expand()
                self._push()

            case Key.CTRL_ENTER:
                self._paste_exit()
                self._buf.insert("\n")
                self._push()

            case Key.BACKSPACE:
                if self._paste.condensed:
                    self._paste.backspace(self._buf)
                else:
                    self._buf.delete_before()
                self._push()

            case Key.CTRL_U:
                self._buf.clear()
                self._paste.condensed = False
                self._push()

            case Key.CHAR if ch:
                self._paste_exit()
                # Trigger detection during streaming (same as idle)
                tch = self._registry.resolve(key, ch) if self._registry else None
                if tch is not None:
                    await self._open_trigger_overlay(tch)
                else:
                    self._buf.insert(ch)
                self._push()

        return None

    # ── idle dispatch ─────────────────────────────────────────────────────────

    async def _dispatch_idle(self, key: Key, ch: str) -> object:
        # ── exit / interrupt ──────────────────────────────────────────────────
        if key == Key.CTRL_C:
            return self._ctrl_c_sequence()

        # Any key other than Ctrl+C clears the "Press Ctrl+C again" notification
        # and resets the double-press counter so the user can start fresh.
        if self._ctrl_c_count > 0:
            self._ctrl_c_count = 0
            self._state.conversation.notification.set(None)

        if key == Key.CTRL_D:
            text = self._buf.text
            if text:
                return await self._submit(text)
            return _EXIT

        # ── paste ─────────────────────────────────────────────────────────────
        if key == Key.PASTE and ch:
            import shutil  # noqa: PLC0415
            cols = shutil.get_terminal_size((80, 24)).columns
            self._paste.apply(self._buf, ch, cols)
            self._push()
            return None

        if key == Key.CTRL_V:
            self._paste.expand()
            self._push()
            return None

        # ── trigger detection ──────────────────────────────────────────────────
        trigger_char = self._registry.resolve(key, ch) if self._registry else None
        if trigger_char is not None:
            handler = self._registry.get(trigger_char)
            can = handler.can_activate(self._buf.buf[:self._buf.cursor]) if handler else False
            if can:
                await self._open_trigger_overlay(trigger_char)
            else:
                self._paste_exit()
                self._buf.insert(trigger_char)
                self._push()
            return None

        # ── main key dispatch ─────────────────────────────────────────────────
        match key:
            case Key.ENTER:
                text = self._buf.text.strip()
                if self._paste.condensed:
                    text = self._buf.text.strip()
                if text:
                    self._buf.clear()
                    self._paste.condensed = False
                    self._push()
                    self._hist.commit(text)
                    return await self._submit(text)

            case Key.CTRL_ENTER:
                self._paste_exit()
                self._buf.insert("\n")
                self._push()

            case Key.BACKSPACE:
                if self._paste.condensed:
                    self._paste.backspace(self._buf)
                elif self._buf.cursor == len(self._buf):
                    # Re-enter trigger mode via backspace into @/slash token
                    tail = self._find_trigger_tail()
                    if tail:
                        tch, tpre, tfrag = tail
                        handler = self._registry.get(tch) if self._registry else None
                        if handler:
                            self._buf.set(tpre)
                            await self._open_trigger_overlay_with_initial(
                                list(tpre) + [tch] + list(tfrag)
                            )
                            return None
                    self._buf.delete_before()
                else:
                    self._buf.delete_before()
                self._push()

            case Key.CTRL_U:
                self._buf.clear()
                self._paste.condensed = False
                self._push()

            case Key.LEFT:
                self._paste_exit()
                self._buf.move_left()
                self._push()

            case Key.RIGHT:
                self._paste_exit()
                self._buf.move_right()
                self._push()

            case Key.HOME:
                self._paste_exit()
                self._buf.move_home()
                self._push()

            case Key.END:
                self._paste_exit()
                self._buf.move_end()
                self._push()

            case Key.UP:
                self._paste_exit()
                if not self._buf.move_up():
                    result = self._hist.up(self._buf.buf)
                    if result is not None:
                        self._buf.set(result)
                        self._paste.condensed = False
                self._push()

            case Key.DOWN:
                self._paste_exit()
                if not self._buf.move_down():
                    result = self._hist.down(self._buf.buf)
                    if result is not None:
                        self._buf.set(result)
                        self._paste.condensed = False
                self._push()

            case Key.SHIFT_TAB:
                new_mode = self._modes.cycle()
                mode_str = build_mode_str(new_mode)
                self._state.conversation.mode_str.set(mode_str)
                self._state.conversation.active_mode_name.set(new_mode.name)
                self._state.conversation.active_mode_badge.set(new_mode.badge)
                self._state.conversation.notification.set(
                    f"❖ Switched to {new_mode.name} mode"
                )
                asyncio.get_event_loop().call_later(
                    2.0,
                    lambda: self._state.conversation.notification.set(None),
                )

            case Key.CHAR if ch:
                self._paste_exit()
                # Re-enter trigger via typing into existing @/slash token
                if not ch.isspace() and self._buf.cursor == len(self._buf):
                    tail = self._find_trigger_tail()
                    if tail:
                        tch, tpre, tfrag = tail
                        handler = self._registry.get(tch) if self._registry else None
                        if handler:
                            self._buf.set(tpre)
                            await self._open_trigger_overlay_with_initial(
                                list(tpre) + [tch] + list(tfrag) + [ch]
                            )
                            return None
                # Check whether this char is a registered trigger.
                tch = self._registry.resolve(key, ch) if self._registry else None
                self._buf.insert(ch)
                if tch is not None:
                    await self._open_trigger_overlay(tch)
                else:
                    self._push()

        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _push(self) -> None:
        inp = self._state.input
        if self._paste.condensed:
            inp.update(
                list(self._buf.buf), self._buf.cursor,
                paste_condensed=True,
                paste_label=self._paste.label,
            )
        else:
            inp.update(list(self._buf.buf), self._buf.cursor)

    def _paste_exit(self) -> None:
        if self._paste.condensed:
            self._paste.expand()

    def _ctrl_c_sequence(self) -> object:
        self._ctrl_c_count += 1
        if self._ctrl_c_count == 1:
            self._buf.clear()
            self._paste.condensed = False
            self._push()
            self._state.conversation.notification.set("Press Ctrl+C again to exit.")
            return None   # keep looping
        self._state.conversation.notification.set(None)
        from agenthicc.tui.input.renderer import show_exit_hint  # noqa: PLC0415
        show_exit_hint(self._state.conversation.session_id())
        return _EXIT

    async def _submit(self, text: str) -> object:
        """Clear buffer and dispatch SendMessageCommand."""
        self._buf.clear()
        self._paste.condensed = False
        self._ctrl_c_count = 0
        self._push()
        await self._bus.dispatch_async(SendMessageCommand(text=text))
        return None

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
                fragment = "".join(buf[i + 1:])
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

        def on_complete(result: Any) -> None:
            from agenthicc.tui.trigger import TriggerResult  # noqa: PLC0415
            self._overlay.hide()
            if result is not None and isinstance(result, TriggerResult):
                self._buf.set(result.buffer)
                if result.cursor is not None:
                    self._buf.cursor = result.cursor
                self._push()
                if result.submit:
                    import asyncio  # noqa: PLC0415
                    text = "".join(result.buffer).strip()
                    asyncio.get_event_loop().create_task(
                        self._bus.dispatch_async(SendMessageCommand(text=text))
                    )
            else:
                self._push()

        overlay = TriggerPickerOverlay(
            initial_buf=initial,
            registry=self._registry,
            cwd=self._cwd,
            on_complete=on_complete,
        )
        self._overlay.show(overlay)

    def print_idle_header(self, appender: Any) -> None:
        """Print the session info + separator before each new prompt."""
        if appender:
            appender.print_idle_header()
