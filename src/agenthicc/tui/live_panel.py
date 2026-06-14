"""LivePanel — Rich Live block that redraws when any component state changes.

Implements :class:`~agenthicc.tui.protocols.LiveComponent`.

The panel owns four state objects and subscribes to each via ``on_change()``.
When any state changes, ``_redraw()`` is called and the Rich Live block is
updated with the freshly rendered content.

Layout (top → bottom):
    SpinnerCalls  — tool-call rows (only during streaming)
    StatusBar     — agent state, tool name, tokens, runtime
    ─────────     — top border
    ❯ prompt      — input bar (with cursor ▌)
    ─────────     — bottom border
    Footer        — context-sensitive key hints
"""
from __future__ import annotations

import sys
from typing import Any

from agenthicc.tui.states import (
    FooterState,
    InputBarState,
    SpinnerState,
    StatusBarState,
)


class LivePanel:
    """Rich Live block wired to four reactive state objects.

    Satisfies :class:`~agenthicc.tui.protocols.LiveComponent`.
    """

    def __init__(self, console: Any) -> None:
        self._console = console
        self.status = StatusBarState()
        self.footer = FooterState()
        self.input_bar = InputBarState()
        self.spinner = SpinnerState()
        self._live: Any | None = None

        # Subscribe to every state — any change triggers a full redraw.
        for comp in (self.status, self.footer, self.input_bar, self.spinner):
            comp.on_change(self._redraw)

    # ── rendering ─────────────────────────────────────────────────────────────

    def _build(self) -> Any:
        """Assemble the full panel renderable.

        Terminal width is read **once** here and passed down to every component
        so all heights and renders are computed with the same value for this
        frame — guarding against mid-render resize races.
        """
        import os as _os  # noqa: PLC0415
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415

        # Single syscall — shared by every component in this frame.
        # os.get_terminal_size() raises OSError when stdout is not a TTY
        # (e.g. piped output, CI without a pty).  We let that propagate so
        # callers know the terminal is not available rather than silently
        # falling back to a wrong width.
        cols = _os.get_terminal_size().columns
        border = Text("─" * cols, style="dim")

        # Measure expected heights before rendering to detect overflow.
        heights = {
            "spinner": self.spinner.height(cols),
            "status":  self.status.height(cols),
            "input":   self.input_bar.height(cols),
            "footer":  self.footer.height(cols),
        }
        # Total = component rows + 2 borders.  Log a warning (non-fatal) when
        # the panel would exceed the terminal height so we can diagnose issues.
        rows = _os.get_terminal_size().lines
        total = sum(heights.values()) + 2
        if total > rows:
            print(
                f"[live_panel] panel height {total} > terminal rows {rows}; "
                f"heights={heights}",
                file=sys.stderr,
            )

        lines: list[Any] = []
        for call_line in self.spinner.render_calls(cols):
            lines.append(Text.from_markup(call_line))
        for sl in self.status.render(cols).splitlines():
            lines.append(Text.from_markup(sl))
        lines.append(border)
        for pl in self.input_bar.render_prompt(cols).splitlines():
            lines.append(Text.from_markup(pl))
        lines.append(border)
        for fl in self.footer.render(cols).splitlines():
            lines.append(Text.from_markup(fl))

        return Group(*lines)

    def _redraw(self) -> None:
        if self._live is None:
            return
        try:
            self._live.update(self._build())
        except OSError:
            # Not a TTY (e.g. stdout is a pipe) — suppress silently.
            pass
        except Exception:
            import traceback  # noqa: PLC0415
            traceback.print_exc(file=sys.stderr)

    def _on_sigwinch(self, _signum: int, _frame: Any) -> None:
        """Handle terminal resize: re-render so the new width is picked up."""
        try:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._redraw)
            else:
                self._redraw()
        except Exception:  # noqa: BLE001
            pass

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Activate the Rich Live block (begin redrawing on state changes)."""
        import signal  # noqa: PLC0415
        from rich.live import Live  # noqa: PLC0415

        self._live = Live(
            self._build(),
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()

        # Register SIGWINCH so the panel picks up terminal resizes immediately.
        try:
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        except (OSError, AttributeError):
            pass  # Not available on Windows or non-TTY environments.

    def stop(self) -> None:
        """Stop the Rich Live block and release the terminal."""
        import signal  # noqa: PLC0415

        # Restore default SIGWINCH behaviour.
        try:
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        except (OSError, AttributeError):
            pass

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                import traceback  # noqa: PLC0415
                traceback.print_exc(file=sys.stderr)
            self._live = None
