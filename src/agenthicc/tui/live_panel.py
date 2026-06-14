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
        import shutil  # noqa: PLC0415
        try:
            from rich.group import Group  # noqa: PLC0415
            from rich.text import Text   # noqa: PLC0415
        except ImportError:  # pragma: no cover
            return ""

        cols = shutil.get_terminal_size((80, 24)).columns
        border = Text("─" * cols, style="dim")
        lines: list[Any] = []

        for call_line in self.spinner.render_calls():
            lines.append(Text.from_markup(call_line))

        lines.append(Text.from_markup(self.status.render()))
        lines.append(border)

        for pl in self.input_bar.render_prompt().splitlines():
            lines.append(Text.from_markup(pl))

        lines.append(border)
        lines.append(Text.from_markup(self.footer.render()))

        return Group(*lines)

    def _redraw(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._build())
            except Exception:  # noqa: BLE001
                pass

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Activate the Rich Live block (begin redrawing on state changes)."""
        from rich.live import Live  # noqa: PLC0415
        self._live = Live(
            self._build(),
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the Rich Live block and release the terminal."""
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None
