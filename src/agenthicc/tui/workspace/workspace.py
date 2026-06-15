"""Workspace — root component owning the always-on Live block (PRD-60 §3).

The Live block starts ONCE at application startup and stops ONCE at shutdown.
It NEVER starts/stops per agent turn.  This eliminates the cursor race that
caused visual corruption in the previous architecture.

Key properties:
- ``auto_refresh=False`` — no background _RefreshThread racing with console.print()
- ``transient=False`` — the Live block is a permanent bottom-of-screen fixture
- All redraws are explicit via ``_redraw()`` called from Signal subscriptions
"""
from __future__ import annotations

import sys
from typing import Any

from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.components import StatusComponent, ComposerComponent, FooterComponent
from agenthicc.tui.workspace.overlay import OverlayHost


def _border(cols: int) -> Any:
    from rich.text import Text  # noqa: PLC0415
    return Text("─" * cols, style="dim")


def _get_cols() -> int:
    import os
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


class Workspace:
    """Root component — owns the terminal for the application lifetime."""

    def __init__(self, app_state: Any, console: Any) -> None:
        self._state   = app_state
        self._console = console

        self.status   = StatusComponent(app_state)
        self.composer = ComposerComponent(app_state)
        self.footer   = FooterComponent(app_state)
        self.overlays = OverlayHost(app_state)
        self.scroll   = ScrollBufferAppender(app_state, console)

        self._live: Any | None = None
        self._unsubs: list[Any] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the always-on Live block. Call ONCE at application startup."""
        from rich.live import Live  # noqa: PLC0415

        self.overlays.set_redraw_callback(self._redraw)
        self.scroll.mount()

        self._live = Live(
            self._build(),
            console=self._console,
            auto_refresh=False,   # no background _RefreshThread
            transient=True,       # prevents ghost Live-content in the scroll buffer
            # (with transient=False, console.print() while Live is active orphans
            # old Live content into the scroll buffer, causing repeated status-bar
            # lines in the transcript)
        )
        self._live.start()

        # Wire all state signals → _redraw
        conv = self._state.conversation
        inp  = self._state.input
        for sig in (
            conv.agent_state, conv.active_tool, conv.elapsed_s,
            conv.model_name, conv.tokens_in, conv.tokens_out, conv.cost_usd,
            conv.mode_str, conv.notification, conv.active_mode_name,
            inp.buf, inp.cursor, inp.paste_condensed, inp.paste_label,
            self._state.overlay,
        ):
            self._unsubs.append(sig.subscribe(self._redraw))

        # SIGWINCH handler
        try:
            import signal  # noqa: PLC0415
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        except (AttributeError, OSError):
            pass

    def stop(self) -> None:
        """Stop the Live block. Call ONCE at application shutdown."""
        try:
            import signal  # noqa: PLC0415
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        except (AttributeError, OSError):
            pass

        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()

        self.scroll.unmount()

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

    # ── rendering ─────────────────────────────────────────────────────────────

    def _build(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415
        cols = _get_cols()

        parts: list[Any] = []

        # Blank separator between the Scroll Buffer and the status bar (PRD-73).
        # Stays inside the Live Block so it moves with the block and never
        # appears in the scroll buffer.
        parts.append(Text(""))

        # Status bar — always at the top of the Live Block
        parts.append(self.status.render())
        parts.append(_border(cols))

        # When an overlay is active it REPLACES the composer area.
        # Overlays (TriggerPickerOverlay, ConfigMenuOverlay, …) render their
        # own prompt / content lines, so we must NOT also render ComposerComponent
        # — that would produce two input bars.
        if self.overlays.active:
            overlay_renderable = self.overlays.render()
            if overlay_renderable is not None:
                parts.append(overlay_renderable)
        else:
            parts.append(self.composer.render())

        parts.append(_border(cols))

        # Footer
        parts.append(self.footer.render())

        return Group(*parts)

    def _redraw(self) -> None:
        if self._live is None:
            return
        try:
            # refresh=True is required: without it, update() only stores the new
            # renderable without rendering — the Live block never visually updates.
            self._live.update(self._build(), refresh=True)
        except OSError:
            pass  # not a TTY
        except Exception:
            import traceback  # noqa: PLC0415
            traceback.print_exc(file=sys.stderr)

    def _on_sigwinch(self, _signum: int, _frame: Any) -> None:
        try:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._redraw)
            else:
                self._redraw()
        except Exception:  # noqa: BLE001
            pass
