"""LivePanel — Rich Live block that redraws when any component state changes.

Implements :class:`~agenthicc.tui.protocols.LiveComponent`.

The panel owns four state objects and subscribes to each via ``on_change()``.
When any state changes, ``_redraw()`` is called and the Rich Live block is
updated with the freshly rendered content.

Layout (top → bottom) — CONSTANT HEIGHT, nothing ever bounces:
    StatusBar   — agent state · active tool · runtime · model
    ─────────
    ❯ prompt    — input bar (with cursor ▌)
    ─────────
    Footer      — mode line + context-sensitive key hints

Tool-call rows are NOT rendered inside the Live block.  Completed calls are
printed to the terminal scroll buffer by ``AgenthiccTUI._on_tool_complete``
so they accumulate naturally above the Live panel without shifting it.
"""
from __future__ import annotations

import sys
from typing import Any

from agenthicc.tui.states import (
    FooterState,
    InputBarState,
    StatusBarState,
)


class LivePanel:
    """Rich Live block wired to three reactive state objects.

    Satisfies :class:`~agenthicc.tui.protocols.LiveComponent`.
    """

    def __init__(self, console: Any) -> None:
        self._console = console
        self.status = StatusBarState()
        self.footer = FooterState()
        self.input_bar = InputBarState()
        self._live: Any | None = None

        # Subscribe to every state — any change triggers a full redraw.
        for comp in (self.status, self.footer, self.input_bar):
            comp.on_change(self._redraw)

    # ── rendering ─────────────────────────────────────────────────────────────

    def _build(self) -> Any:
        """Assemble the panel renderable.

        Terminal width is read **once** here so all heights and renders in a
        single frame use the same value, guarding against mid-render resize
        races.
        """
        import os as _os  # noqa: PLC0415
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415

        cols = _os.get_terminal_size().columns
        border = Text("─" * cols, style="dim")

        lines: list[Any] = []
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
            pass  # Not a TTY — suppress silently.
        except Exception:
            import traceback  # noqa: PLC0415
            traceback.print_exc(file=sys.stderr)

    def _on_sigwinch(self, _signum: int, _frame: Any) -> None:
        """Handle terminal resize: re-render with the new width."""
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

        try:
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        except (OSError, AttributeError):
            pass

    def stop(self) -> None:
        """Stop the Rich Live block and release the terminal."""
        import signal  # noqa: PLC0415

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
