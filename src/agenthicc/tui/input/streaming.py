"""StreamingSession — keystroke capture while the LivePanel is active.

During an agent turn the idle :class:`~agenthicc.tui.input.session.IdleInputSession`
is not running.  ``StreamingSession`` fills that gap: it reads keystrokes in the
background, updates ``InputBarState`` (so the live panel shows what the user is
typing), and on Enter appends the text to *pending_queue* for dispatch after the
agent turn.

Features
--------
Trigger support (@ and /)
    When the user types ``@`` or ``/``, the ``StreamingSession`` exits its own
    CBREAK context (to avoid nested raw_mode), pauses the Live panel, runs a
    full :func:`~agenthicc.tui.input.session.run_input_session` with the current
    buffer pre-seeded, and restores the Live panel with the completed text.

Paste condensation
    Bracketed paste (``Key.PASTE``) inserts the full text into the buffer but
    shows ``[Pasted text with N chars]`` in the input bar.  ``Ctrl+V`` expands
    the label back to the raw text; ``Backspace`` on a condensed paste deletes
    the whole block; any other printable key exits condensed mode first.

Cursor movement
    Left / Right arrows and Home / End move the cursor; the live panel redraws
    on every move so the ``▌`` indicator tracks correctly.

Ctrl+C / Esc
    Calls *on_interrupt* (= ``_sigint_cancel`` from ``tui.py``) to cancel the
    running agent task, and clears the queued input.

CBREAK setup is delegated to :func:`~agenthicc.tui.cbreak_reader.raw_mode`.
"""
from __future__ import annotations

import asyncio
import select
import sys
from pathlib import Path
from typing import Any

from agenthicc.tui.cbreak_reader import Key, raw_mode, read_key
from agenthicc.tui.input.buffer import InputBuffer

# Sentinel returned by _read_loop when a trigger char is activated.
# Tells _run() to exit CBREAK, handle the trigger, then re-enter CBREAK.
_TRIGGER = object()


def _read_streaming_key(fd: int) -> tuple[Key, str] | None:
    """Non-blocking: return ``None`` immediately if no keystroke is available."""
    r, _, _ = select.select([fd], [], [], 0)
    if not r:
        return None
    return read_key(fd)


class StreamingSession:
    """Asyncio background task that captures keystrokes during agent streaming.

    Parameters
    ----------
    input_bar_state:
        The ``InputBarState`` inside the LivePanel.
    pending_queue:
        Messages submitted during streaming are appended here.
    console:
        Rich Console — prints the "⌛ Queued" confirmation.
    live_panel:
        Paused / resumed around trigger-picker sessions.
    trigger_registry:
        ``TriggerRegistry`` pre-loaded with ``AtMentionTrigger`` and
        ``SlashCommandTrigger``.  When ``None`` triggers are disabled.
    cwd:
        Working directory for @-mention file completions.
    on_interrupt:
        Called on Ctrl+C / Esc to cancel the running agent task.
    """

    def __init__(
        self,
        input_bar_state: Any,
        pending_queue: list[str],
        console: Any,
        live_panel: Any = None,
        trigger_registry: Any = None,
        cwd: Path | None = None,
        on_interrupt: Any = None,
    ) -> None:
        self._state = input_bar_state
        self._queue = pending_queue
        self._console = console
        self._live_panel = live_panel
        self._registry = trigger_registry
        self._cwd = cwd or Path(".")
        self._on_interrupt = on_interrupt
        self._buf = InputBuffer()
        self._task: asyncio.Task | None = None

        # Paste condensation state
        self._paste_condensed = False
        self._paste_label = ""

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background keystroke-reader task."""
        self._buf.clear()
        self._paste_condensed = False
        self._paste_label = ""
        self._state.clear()
        self._task = asyncio.create_task(self._run(), name="streaming-session")

    def stop(self) -> None:
        """Cancel the background task and clear state."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._buf.clear()
        self._paste_condensed = False
        self._state.clear()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _push(self) -> None:
        """Sync InputBuffer into InputBarState for live-panel redraw."""
        if self._paste_condensed:
            self._state.update(
                list(self._buf.buf),
                self._buf.cursor,
                paste_condensed=True,
                paste_label=self._paste_label,
            )
        else:
            self._state.update(list(self._buf.buf), self._buf.cursor)

    def _exit_paste(self) -> None:
        """Exit paste condensation mode (any editing key clears it)."""
        if self._paste_condensed:
            self._paste_condensed = False
            self._paste_label = ""

    def _is_trigger_char(self, key: Key, ch: str) -> bool:
        """Return True when *key*/*ch* should open the trigger picker."""
        if self._registry is None:
            return False
        if key == Key.AT and "@" in self._registry.chars:
            return True
        if key == Key.CHAR and ch and ch in self._registry.chars:
            return True
        return False

    # ── main asyncio task ─────────────────────────────────────────────────────

    async def _run(self) -> None:
        from rich.markup import escape as _esc  # noqa: PLC0415

        fd = sys.stdin.fileno()
        while True:
            # Enter CBREAK for this iteration.  We exit the context before
            # running the trigger picker so there is no nested raw_mode.
            trigger_char: str | None = None
            try:
                with raw_mode(fd):
                    result = await self._read_loop(fd, _esc)
                    if result is _TRIGGER:
                        trigger_char = self._pending_trigger
                    elif result is None:
                        return  # normal exit (cancel / error)
            except asyncio.CancelledError:
                return
            except Exception:
                return   # terminal not available

            if trigger_char is None:
                return

            # Handle trigger OUTSIDE raw_mode — no nesting issues.
            await self._handle_trigger(trigger_char)
            # Re-enter raw_mode on next iteration to resume normal input.

    async def _read_loop(self, fd: int, _esc: Any) -> object:
        """Read keystrokes until cancelled, Enter submits, or trigger fires.

        Returns:
            ``_TRIGGER`` — trigger char activated (self._pending_trigger is set)
            ``None``     — loop should end (Ctrl+C, exception, etc.)
        """
        self._pending_trigger: str = ""

        while True:
            await asyncio.sleep(0.02)
            result = _read_streaming_key(fd)
            if result is None:
                continue

            key, ch = result

            # ── trigger activation ────────────────────────────────────────────
            if self._is_trigger_char(key, ch):
                trigger_char = "@" if key == Key.AT else ch
                # Only activate when NOT inside a condensed paste
                # and either the buffer is empty or the preceding char is whitespace.
                can_activate = True
                if self._registry is not None:
                    handler = self._registry.get(trigger_char)
                    if handler is not None:
                        can_activate = handler.can_activate(self._buf.buf[:self._buf.cursor])
                if can_activate:
                    self._pending_trigger = trigger_char
                    self._exit_paste()
                    return _TRIGGER
                # can_activate is False → insert as literal character
                self._exit_paste()
                self._buf.insert(trigger_char)
                self._push()
                continue

            # ── interrupt ─────────────────────────────────────────────────────
            match key:
                case Key.CTRL_C | Key.ESC:
                    self._buf.clear()
                    self._paste_condensed = False
                    self._push()
                    if self._on_interrupt is not None:
                        self._on_interrupt()

                # ── submit ────────────────────────────────────────────────────
                case Key.ENTER:
                    # Include full paste text even when condensed.
                    text = self._buf.text.strip()
                    self._buf.clear()
                    self._paste_condensed = False
                    self._paste_label = ""
                    self._push()
                    if text:
                        self._queue.append(text)
                        self._console.print(
                            f"[dim]❯ {_esc(text[:60])}{'…' if len(text) > 60 else ''}  ⌛ Queued[/dim]",
                            markup=True, highlight=False,
                        )

                # ── paste ─────────────────────────────────────────────────────
                case Key.PASTE if ch:
                    # Insert all pasted characters into the buffer.
                    for c in ch:
                        self._buf.insert(c)
                    n = len(ch)
                    lines = ch.count("\n")
                    if lines:
                        self._paste_label = f"[Pasted text with {n} chars / {lines + 1} lines]"
                    else:
                        self._paste_label = f"[Pasted text with {n} chars]"
                    self._paste_condensed = True
                    self._push()

                # ── expand paste (Ctrl+V) ──────────────────────────────────────
                case Key.CTRL_V:
                    self._exit_paste()
                    self._push()

                # ── newline (Ctrl+J) ──────────────────────────────────────────
                case Key.CTRL_ENTER:
                    self._exit_paste()
                    self._buf.insert("\n")
                    self._push()

                # ── delete ────────────────────────────────────────────────────
                case Key.BACKSPACE:
                    if self._paste_condensed:
                        # Delete the entire paste block.
                        self._buf.clear()
                        self._paste_condensed = False
                        self._paste_label = ""
                    else:
                        self._buf.delete_before()
                    self._push()

                case Key.CTRL_U:
                    self._buf.clear()
                    self._paste_condensed = False
                    self._paste_label = ""
                    self._push()

                # ── cursor movement ───────────────────────────────────────────
                case Key.LEFT:
                    self._exit_paste()
                    self._buf.move_left()
                    self._push()

                case Key.RIGHT:
                    self._exit_paste()
                    self._buf.move_right()
                    self._push()

                case Key.HOME:
                    self._exit_paste()
                    self._buf.move_home()
                    self._push()

                case Key.END:
                    self._exit_paste()
                    self._buf.move_end()
                    self._push()

                # ── printable character ───────────────────────────────────────
                case Key.CHAR if ch:
                    self._exit_paste()
                    self._buf.insert(ch)
                    self._push()

                case _:
                    pass  # function keys, unsupported sequences — ignore

    # ── trigger handoff ───────────────────────────────────────────────────────

    async def _handle_trigger(self, trigger_char: str) -> None:
        """Pause the Live panel, run the full idle picker, resume.

        The idle :func:`~agenthicc.tui.input.session.run_input_session` is
        seeded with the current buffer + the trigger character so the user
        sees the dropdown immediately.  When the user selects an item or
        cancels, the Live panel is restarted with the updated buffer.
        """
        from agenthicc.tui.input.session import run_input_session  # noqa: PLC0415

        # Seed the picker with current buffer content + trigger char
        initial = list(self._buf.buf) + [trigger_char]

        # Stop Live block so the idle session owns the terminal.
        if self._live_panel is not None:
            self._live_panel.stop()
        try:
            result = await asyncio.to_thread(
                run_input_session,
                "\x1b[1;32m❯\x1b[0m ",
                self._cwd,
                [],               # no history in streaming context
                self._registry,
                None,             # no initial_menu
                "",               # no resume_id
                None,             # no mode_manager
                initial,          # pre-seed with current text + trigger char
            )
        finally:
            # Always restart the Live panel even if the picker raised.
            if self._live_panel is not None:
                self._live_panel.start()

        # Apply result: if user selected something use it, else keep old buf.
        if result is not None:
            self._buf.set(list(result))
        # else: user cancelled (None) — keep existing buffer (no trigger char added)

        self._paste_condensed = False
        self._paste_label = ""
        self._push()
