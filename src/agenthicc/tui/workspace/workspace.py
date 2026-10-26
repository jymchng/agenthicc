"""Workspace — root component owning the always-on Live block (PRD-60 §3).

The Live block starts ONCE at application startup and stops ONCE at shutdown.
It NEVER starts/stops per agent turn.  This eliminates the cursor race that
caused visual corruption in the previous architecture.

Key properties (AGX-11):
- No background refresh thread: frames are drawn on the event loop only.
- Signals request frames from a :class:`FrameScheduler`, which draws at most
  one frame per ``frame_interval_s`` and echoes keys at once.
- The block is a :class:`LiveRegion`: rows are rendered to segment lines,
  status and footer rows are cached by their markup, and only the rows that
  changed are rewritten. The block is cleared cleanly during shutdown.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.conversation_store import ui_settings as _ui
from agenthicc.tui.workspace.components import ComposerComponent, FooterComponent, StatusComponent
from agenthicc.tui.workspace.frames import FrameScheduler, FrameStats
from agenthicc.tui.workspace.live_region import Line, LiveRegion
from agenthicc.tui.workspace.overlay import OverlayHost

if TYPE_CHECKING:
    from rich.console import Console, RenderableType
    from agenthicc.tui.workspace.announcer import Announcer
    from agenthicc.tui.conversation_store import AppState, ConversationEvent
    from agenthicc.runners.startup import StartupCoordinator

# Rendered markup rows kept for reuse: a few frames' worth of distinct rows.
_MARKUP_CACHE_SIZE = 256


def _get_rows(console: Console) -> int:
    """Return the current terminal height with a deterministic fallback."""

    try:
        return max(1, int(console.height))
    except (AttributeError, TypeError, ValueError):
        try:
            import os

            return max(1, os.get_terminal_size().lines)
        except OSError:
            return 24


class Workspace:
    """Root component — owns the terminal for the application lifetime."""

    def __init__(
        self,
        app_state: AppState,
        console: Console,
        max_live_tool_calls: int = 5,
        group_exploratory_calls: bool = True,
        startup: StartupCoordinator | None = None,
        *,
        composer_max_rows: int = 12,
        frame_interval_s: float = 0.016,
    ) -> None:
        self._state = app_state
        self._console = console
        self._startup = startup

        self.status = StatusComponent(app_state, startup)
        self.composer = ComposerComponent(app_state, max_rows=composer_max_rows)
        self.footer = FooterComponent(app_state)
        self.overlays = OverlayHost(app_state)
        self.scroll = ScrollBufferAppender(
            app_state,
            console,
            max_live_tool_calls=max_live_tool_calls,
            group_exploratory_calls=group_exploratory_calls,
        )

        self._live: LiveRegion | None = None
        self.frames = FrameScheduler(self._flush_redraw, interval_s=frame_interval_s)
        self._markup_rows: OrderedDict[tuple[str, int], tuple[Line, ...]] = OrderedDict()
        self._unsubs: list[Callable[[], None]] = []
        if startup is not None:
            self._unsubs.append(startup.subscribe(self._redraw))
        self._resize_pending: bool = False
        self._resize_handle: asyncio.TimerHandle | None = None
        self._announcer: Announcer | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the always-on Live block. Call ONCE at application startup."""
        from agenthicc.tui.terminal.capabilities import capabilities

        # Overlay key handling echoes like typing does.
        self.overlays.set_redraw_callback(self._redraw_input)
        self.scroll.mount()

        # Transient, so the block leaves no copy in the scroll buffer. Rows
        # past the terminal height are cropped.
        self._live = LiveRegion(
            self._console,
            transient=True,
            synchronized_output=lambda: capabilities.synchronized_output,
        )
        self._live.start()
        self._flush_redraw()
        ui = _ui(self._state)
        if ui is not None and ui.screen_reader:
            from agenthicc.tui.workspace.announcer import Announcer

            self._announcer = Announcer(self._state, self.scroll)
            self._announcer.start()

        # Typing, the cursor and overlays echo at once; everything else is
        # merged into at most one frame per frame interval.
        inp = self._state.input
        for input_signal in (
            inp.buf,
            inp.cursor,
            inp.paste_condensed,
            inp.paste_label,
            self._state.overlay,
        ):
            self._unsubs.append(input_signal.subscribe(self._redraw_input))
        conv = self._state.conversation
        for sig in (
            conv.agent_state,
            conv.active_tool,
            conv.model_name,
            conv.display_elapsed_s,
            conv.tokens_in,
            conv.tokens_out,
            conv.cost_usd,
            conv.usage_status,
            conv.cost_status,
            conv.usage_calls,
            conv.notification,
            conv.loop_status,
            conv.transcript_loading,
            self._state.active_mode,  # PRD-75: single mode signal
            self._state.pending_approval,  # PRD-78: approval overlay
            self._state.workflow_run,  # PRD-81: workflow progress
            conv.live_tool_overflow,  # overflow bridge row
            conv.workflow_override,  # PRD-114: /workflow indicator
            conv.compaction_active,  # PRD-119: compaction on/off toggle
            conv.subagent_pool_state,  # PRD-124: subagent pool progress
            conv.subagent_rows,  # AGX-08: finished panel rows expire
            conv.terminal_waiting,  # PRD-149: owned terminal wait
            conv.terminal_wait_label,
            conv.terminal_wait_elapsed_s,
            conv.terminal_running_count,
        ):
            self._unsubs.append(sig.subscribe(self._redraw))
        # Activity timing is a visible signal during active work, but its
        # wall-clock value is intentionally not rendered while approval or
        # question input owns the terminal. Keep this callback separate so a
        # compatibility writer cannot repaint an unchanged waiting modal.
        self._unsubs.append(conv.activity_elapsed_s.subscribe(self._redraw_activity))
        # Frame is an animation-only signal. Keep it behind an activity guard
        # so compatibility code that writes it while idle cannot repaint an
        # unchanged Live block into clients that do not interpret ANSI erase
        # controls (PRD-164).
        self._unsubs.append(conv.frame.subscribe(self._redraw_frame))

        # SIGWINCH handler
        try:
            import signal

            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        except (AttributeError, OSError):
            pass

    def stop(self) -> None:
        """Stop the Live block. Call ONCE at application shutdown."""
        try:
            import signal

            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        except (AttributeError, OSError):
            pass

        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()
        # Like Rich's Live, draw the final state before the block is cleared,
        # so a frame still waiting out the interval is not lost.
        if self.frames.pending:
            self.frames.cancel()
            self._flush_redraw()
        self._resize_pending = False
        if self._resize_handle is not None:
            self._resize_handle.cancel()
            self._resize_handle = None

        if self._announcer is not None:
            self._announcer.stop()
            self._announcer = None
        self.scroll.unmount()

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

    def frame_stats(self) -> FrameStats | None:
        """Return the live block's frame statistics while it is running."""
        return None if self._live is None else self._live.stats

    def flush_scroll(self) -> None:
        """Flush pending transcript output before an agent interruption.

        The appender normally closes tool groups when it receives the next
        conversation event.  Cancellation can precede that event, so expose
        the boundary through the workspace rather than reaching into the
        renderer from session orchestration.
        """
        self.scroll.flush()

    async def replay_transcript(self, events: list["ConversationEvent"]) -> None:
        """Display a resumed transcript while showing a loading state.

        Replay is deliberately chunked. A single large appender batch would
        block the event loop until every historical event had rendered, making
        the loading state impossible to observe and delaying the first Live redraw.
        Chunks also keep the UI responsive without changing the persisted event
        order or grouping decisions.
        """
        conv = self._state.conversation
        chunk_size = 64
        first_chunk = True
        # Replay bypasses ConversationStore.append_event by design. Seed the
        # event-ID index before rendering so a later resumed recovery signal is
        # not appended to the durable transcript a second time.
        conv.remember_events(events)
        conv.transcript_loading.set(True)
        try:
            # Let the Live block render the loading state before the first
            # historical event is queued.
            await asyncio.sleep(0)
            for start in range(0, len(events), chunk_size):
                self.scroll.replay(
                    events[start : start + chunk_size],
                    continue_group=not first_chunk,
                )
                first_chunk = False
                # The appender flushes on the next loop turn before the next
                # bounded chunk is queued.
                await asyncio.sleep(0)
        finally:
            conv.transcript_loading.set(False)
            # Flush the final appender callback and redraw the normal status
            # before input is accepted.
            await asyncio.sleep(0)

    # ── rendering ─────────────────────────────────────────────────────────────

    def _markup(self, markup: str, cols: int) -> tuple[Line, ...]:
        """Render one markup row, reusing the result while the row is unchanged."""
        key = (markup, cols)
        cached = self._markup_rows.get(key)
        if cached is not None:
            self._markup_rows.move_to_end(key)
            return cached
        from rich.text import Text

        lines = self._render(Text.from_markup(markup, emoji=False), cols)
        self._markup_rows[key] = lines
        if len(self._markup_rows) > _MARKUP_CACHE_SIZE:
            self._markup_rows.popitem(last=False)
        return lines

    def _render(self, renderable: RenderableType, cols: int) -> tuple[Line, ...]:
        options = self._console.options.update(width=cols)
        return tuple(
            tuple(line) for line in self._console.render_lines(renderable, options, pad=False)
        )

    def _build_lines(self) -> list[Line]:
        """Return the live block's rows at the console's current width."""
        cols = max(1, self._console.width)
        ui = _ui(self._state)
        # A screen reader reads box drawing aloud; it gets no border rows.
        reader = ui is not None and ui.screen_reader

        if self.overlays.active:
            # Workspace owns the complete Live layout.  Publish only the
            # remaining overlay region so responsive overlays do not count
            # status, borders, or the permanent footer as available rows.
            fixed_rows = (
                self.status.height(cols) + self.footer.height(cols) + 1 + (0 if reader else 2)
            )
            self.overlays.set_viewport(cols, max(1, _get_rows(self._console) - fixed_rows))

        lines: list[Line] = []

        # Overflow bridge — flush against the scroll-buffer tool-call sequence
        # when a group exceeds the threshold.  The blank separator follows it so
        # there is no gap between the last printed tool call and this line.
        try:
            _ov = self._state.conversation.live_tool_overflow()
            if _ov > 0:
                _ov_word = "call" if _ov == 1 else "calls"
                lines += self._markup(f"  [dim]⎿ ...and {_ov} more tool {_ov_word}[/dim]", cols)
        except Exception:  # noqa: BLE001
            pass

        # Blank separator between the scroll buffer (or overflow bridge) and
        # the status bar.  Stays inside the block so it never leaks to stdout.
        lines.append(())
        for row in self.status.markup_lines(cols):
            lines += self._markup(row, cols)
        border = None if reader else f"[yellow dim]{'─' * cols}[/yellow dim]"
        if border is not None:
            lines += self._markup(border, cols)

        # When an overlay is active it REPLACES the composer area: overlays
        # render their own prompt / content lines.
        if self.overlays.active:
            overlay_renderable = self.overlays.render()
            if overlay_renderable is not None:
                lines += self._render(overlay_renderable, cols)
        else:
            lines += self.composer.lines(cols)
            lines += self._subagent_rows(cols, reader)

        if border is not None:
            lines += self._markup(border, cols)
        for row in self.footer.markup_lines(cols):
            lines += self._markup(row, cols)
        lines.append(())  # 1-row bottom margin — always visible
        return lines

    def _subagent_rows(self, cols: int, plain: bool) -> list[Line]:
        """The subagent panel, under the prompt while a pool has rows (AGX-08)."""
        pool = self._state.conversation.subagent_pool_state()
        if pool is None:
            return []
        from agenthicc.tui.workspace.subagent_panel import panel_lines

        hint = self._state.require_keymap().hint("subagents.focus")
        lines: list[Line] = []
        for row in panel_lines(
            pool,
            cols,
            now=time.time(),
            terminal_rows=_get_rows(self._console),
            hint=f"{hint.keys} focus" if hint is not None else "",
            plain=plain,
        ):
            lines += self._markup(row, cols)
        return lines

    def _redraw(self) -> None:
        """Request a frame; requests made before it is drawn are merged.

        Showing an overlay changes several signals in sequence; the scheduler
        lets those changes settle into one frame.
        """
        if self._live is None or self._resize_pending:
            return
        self.frames.request()

    def _redraw_input(self) -> None:
        """Request a frame for typed input, drawn on the next loop iteration."""
        if self._live is None or self._resize_pending:
            return
        self.frames.request(immediate=True)

    def _flush_redraw(self) -> None:
        if self._live is None or self._resize_pending:
            return
        started = time.perf_counter()
        try:
            lines = self._build_lines()
        except Exception:
            import traceback

            traceback.print_exc(file=sys.stderr)
            return
        self._live.stats.record_render(time.perf_counter() - started)
        try:
            self._live.update(lines)
        except OSError:
            pass  # not a TTY

    def _redraw_frame(self) -> None:
        """Refresh only when a frame can change a visible animation."""
        conv = self._state.conversation
        if self._state.pending_approval() is not None:
            return
        ui = _ui(self._state)
        if ui is not None and not ui.animated:
            return  # [ui] reduced_motion / screen_reader: nothing animates
        if not conv.is_running() and not conv.compaction_active():
            return
        self._redraw()

    def _redraw_activity(self) -> None:
        """Refresh activity timing unless a prompt owns the terminal."""
        if self._state.pending_approval() is not None:
            return
        self._redraw()

    def _reset_live_after_resize(self) -> None:
        """Repaint the whole block in the next frame.

        A resize changes the width every row was rendered at, so the next
        frame erases the old block (using its old height) and paints every
        row again, in one write.
        """
        if self._live is not None:
            self._live.invalidate()

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
