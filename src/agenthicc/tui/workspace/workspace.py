"""Workspace — root component owning the always-on Live block (PRD-60 §3).

The Live block starts ONCE at application startup and stops ONCE at shutdown.
It NEVER starts/stops per agent turn.  This eliminates the cursor race that
caused visual corruption in the previous architecture.

Key properties:
- ``auto_refresh=False`` — no background _RefreshThread racing with console.print()
- ``transient=True`` — the Live block is cleared cleanly during shutdown
- All redraws are explicit via ``_redraw()`` called from Signal subscriptions
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.components import StatusComponent, ComposerComponent, FooterComponent
from agenthicc.tui.workspace.overlay import OverlayHost

if TYPE_CHECKING:
    from rich.console import Console, RenderableType
    from rich.live import Live
    from agenthicc.tui.conversation_store import AppState


def _border(cols: int):
    from rich.text import Text  # noqa: PLC0415

    return Text("─" * cols, style="yellow dim")


def _get_cols() -> int:
    import os

    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


class Workspace:
    """Root component — owns the terminal for the application lifetime."""

    def __init__(self, app_state: AppState, console: Console, max_live_tool_calls: int = 5) -> None:
        self._state = app_state
        self._console = console

        self.status = StatusComponent(app_state)
        self.composer = ComposerComponent(app_state)
        self.footer = FooterComponent(app_state)
        self.overlays = OverlayHost(app_state)
        self.scroll = ScrollBufferAppender(
            app_state,
            console,
            max_live_tool_calls=max_live_tool_calls,
        )

        self._live: Live | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._redraw_scheduled: bool = False
        self._resize_pending: bool = False
        self._resize_handle: asyncio.TimerHandle | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the always-on Live block. Call ONCE at application startup."""
        from rich.live import Live  # noqa: PLC0415

        self.overlays.set_redraw_callback(self._redraw)
        self.scroll.mount()

        self._live = Live(
            self._build(),
            console=self._console,
            auto_refresh=False,  # no background _RefreshThread
            transient=True,  # prevents ghost Live-content in the scroll buffer
            # (with transient=False, console.print() while Live is active orphans
            # old Live content into the scroll buffer, causing repeated status-bar
            # lines in the transcript)
            vertical_overflow="crop",  # silently clip when overlay exceeds terminal height;
            # "ellipsis" (default) injects a "..." on the last visible row which
            # is visually confusing inside the plan content area
        )
        self._live.start()
        self._redraw_scheduled = False

        # Wire all state signals → _redraw
        conv = self._state.conversation
        inp = self._state.input
        for sig in (
            conv.agent_state,
            conv.active_tool,
            conv.frame,
            conv.model_name,
            conv.tokens_in,
            conv.tokens_out,
            conv.cost_usd,
            conv.notification,
            self._state.active_mode,  # PRD-75: single mode signal
            inp.buf,
            inp.cursor,
            inp.paste_condensed,
            inp.paste_label,
            self._state.overlay,
            self._state.pending_approval,  # PRD-78: approval overlay
            self._state.workflow_run,  # PRD-81: workflow progress
            conv.live_tool_overflow,  # overflow bridge row
            conv.workflow_override,  # PRD-114: /workflow indicator
            conv.compaction_active,  # PRD-119: compaction on/off toggle
            conv.subagent_pool_state,  # PRD-124: subagent pool progress
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
        self._redraw_scheduled = False
        self._resize_pending = False
        if self._resize_handle is not None:
            self._resize_handle.cancel()
            self._resize_handle = None

        self.scroll.unmount()

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

    # ── rendering ─────────────────────────────────────────────────────────────

    def _build(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        cols = _get_cols()

        parts: list[RenderableType] = []

        # Overflow bridge — flush against the scroll-buffer tool-call sequence
        # when a group exceeds the threshold.  The blank separator follows it so
        # there is no gap between the last printed tool call and this line.
        # When there is no overflow the blank separator comes first as normal.
        _overflow_shown = False
        try:
            _ov = self._state.conversation.live_tool_overflow()
            if _ov > 0:
                _ov_word = "call" if _ov == 1 else "calls"
                parts.append(Text.from_markup(f"  [dim]⎿ ...and {_ov} more tool {_ov_word}[/dim]"))
                _overflow_shown = True
        except Exception:  # noqa: BLE001
            pass

        # Blank separator between scroll buffer (or overflow bridge) and the
        # status bar.  Stays inside the Live Block so it never leaks to stdout.
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

        parts.append(Text(""))  # 1-row bottom margin — always visible

        return Group(*parts)

    def _redraw(self) -> None:
        """Coalesce synchronous signal bursts into one Live refresh.

        Showing an overlay changes several signals in sequence. Scheduling the
        actual refresh lets those changes settle before Rich renders the Live
        block, avoiding repeated Plan Review frames and cursor churn.
        """
        if self._live is None:
            return
        if self._resize_pending:
            return
        if self._redraw_scheduled:
            return
        try:
            import asyncio  # noqa: PLC0415

            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._redraw_scheduled = True
                loop.call_soon(self._flush_redraw)
            else:
                self._flush_redraw()
        except RuntimeError:
            self._flush_redraw()

    def _flush_redraw(self) -> None:
        self._redraw_scheduled = False
        if self._live is None or self._resize_pending:
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

    def _reset_live_after_resize(self) -> None:
        """Discard Rich's pre-resize cursor geometry before repainting.

        Rich's ``LiveRender`` uses its last measured shape to move the cursor
        above the live block.  A terminal resize can change both wrapping and
        the number of visible rows, so that shape no longer describes the
        bytes currently on screen.  Restoring with the old shape clears the
        old block; clearing the cached shape makes the next refresh start at
        the restored cursor instead of moving by stale geometry again.

        ``_live_render`` and ``_shape`` are Rich internals, but they are the
        state that owns this exact cursor bookkeeping.  Keep the compatibility
        boundary in this one workspace method so the rest of the TUI remains
        independent of Rich's implementation details.
        """
        if self._live is None:
            return
        try:
            live_render = self._live._live_render
            self._console.control(live_render.restore_cursor())
            live_render._shape = None
        except (AttributeError, OSError):
            # A non-interactive console or a future Rich implementation without
            # these internals can still receive the normal redraw below.
            pass

    def _schedule_resize_redraw(self) -> None:
        """Debounce SIGWINCH bursts until terminal geometry has settled."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._reset_live_after_resize()
            self._flush_redraw()
            return

        if self._resize_handle is not None:
            self._resize_handle.cancel()
        self._resize_handle = loop.call_later(
            0.05,
            self._flush_resize_redraw,
        )

    def _flush_resize_redraw(self) -> None:
        self._resize_handle = None
        self._reset_live_after_resize()
        self._resize_pending = False
        self._flush_redraw()

    def _on_sigwinch(self, _signum: int, _frame: object) -> None:
        try:
            self._resize_pending = True
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._schedule_resize_redraw)
            else:
                self._reset_live_after_resize()
                self._resize_pending = False
                self._flush_redraw()
        except Exception:  # noqa: BLE001
            pass
