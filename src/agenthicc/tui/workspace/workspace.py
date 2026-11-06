"""Workspace — root component owning the always-on Live block (PRD-60 §3).

The Live block starts ONCE at application startup and stops ONCE at shutdown.
It NEVER starts/stops per agent turn.  This eliminates the cursor race that
caused visual corruption in the previous architecture.

Key properties (AGX-11):
- No background refresh thread: frames are drawn on the event loop only.
- Signals request frames from a :class:`FrameScheduler`, which draws at most
  one frame per ``frame_interval_s`` and echoes keys at once.
- The block is a :class:`LiveRegion`: rows are rendered to segment lines,
  status and frame rows are cached by their markup and the theme, and only
  the rows that changed are rewritten. The block is cleared cleanly during
  shutdown.

The block's layout (TUX-02), top to bottom:

1. the overflow bridge row, while a tool group is collapsed;
2. the composer in its frame (an overlay takes its place: the agents hub
   without a frame, any other overlay between the frame's two rules);
3. the subagent panel, while a pool has rows;
4. the status line;
5. one notice (wrapped whole) or one contextual hint row, or nothing.

Idle, that is four rows: three for the framed composer and the status line.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

from agenthicc.tui.preferences import ui_settings as _ui
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.components import (
    ComposerComponent,
    ComposerFrame,
    StatusLine,
    notice_rows,
)
from agenthicc.tui.workspace.frames import FrameScheduler, FrameStats
from agenthicc.tui.workspace.live_region import Line, LiveRegion
from agenthicc.tui.workspace.overlay import OverlayHost

if TYPE_CHECKING:
    from rich.console import Console, RenderableType

    from agenthicc.runners.startup import StartupCoordinator
    from agenthicc.tui.conversation_store import AppState, ConversationEvent
    from agenthicc.tui.statusline import StatusLineCommand
    from agenthicc.tui.theme import Theme
    from agenthicc.tui.workspace.announcer import Announcer

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

        self.status = StatusLine(app_state, startup)
        self.composer = ComposerComponent(app_state, max_rows=composer_max_rows)
        self.frame = ComposerFrame(app_state, self.composer)
        self.overlays = OverlayHost(app_state)
        self.scroll = ScrollBufferAppender(
            app_state,
            console,
            max_live_tool_calls=max_live_tool_calls,
            group_exploratory_calls=group_exploratory_calls,
        )

        self._live: LiveRegion | None = None
        self.frames = FrameScheduler(self._flush_redraw, interval_s=frame_interval_s)
        self._markup_rows: OrderedDict[tuple[str, int, Theme], tuple[Line, ...]] = OrderedDict()
        self._unsubs: list[Callable[[], None]] = []
        if startup is not None:
            self._unsubs.append(startup.subscribe(self._redraw))
        self._resize_pending: bool = False
        self._resize_handle: asyncio.TimerHandle | None = None
        self._announcer: Announcer | None = None
        self._statusline: StatusLineCommand | None = None
        self._git_task: asyncio.Task[None] | None = None
        self._was_running = False

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
        from agenthicc.tui.theme import on_theme_change

        # TUX-01: /theme applies at once; repaint every row with the new tokens.
        self._unsubs.append(on_theme_change(lambda _theme: self._repaint_for_theme()))
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
            conv.background_needs_you,
            conv.background_running,
            conv.background_dnd,
            conv.background_toast,
            conv.session_title,  # TUX-02: frame title
            conv.context_tokens,  # TUX-02: context meter
            conv.context_window,
            conv.git_branch,
            conv.git_dirty,
            conv.statusline_text,  # TUX-02: the user status-line command
            self._state.agent,
            self._state.keymap,
        ):
            self._unsubs.append(sig.subscribe(self._redraw))
        self._unsubs.append(conv.agent_state.subscribe(self._on_agent_state))
        self._start_status_sources()
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

    def suspend_for_external_process(self) -> None:
        """Leave the terminal available while a foreground editor runs."""
        if self._live is None:
            return
        if self.frames.pending:
            self.frames.cancel()
        live, self._live = self._live, None
        live.stop()

    def resume_after_external_process(self) -> None:
        """Restore the inline workspace after an external process exits."""
        if self._live is not None:
            return
        from agenthicc.tui.terminal.capabilities import capabilities

        self._live = LiveRegion(
            self._console,
            transient=True,
            synchronized_output=lambda: capabilities.synchronized_output,
        )
        self._live.start()
        self._flush_redraw()

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
            except Exception:  # noqa: BLE001 - teardown continues after cleanup failures
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
        if self._statusline is not None:
            self._statusline.stop()
            self._statusline = None
        if self._git_task is not None:
            self._git_task.cancel()
            self._git_task = None
        self.scroll.unmount()

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001 - teardown continues after cleanup failures
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

    async def replay_transcript(self, events: list[ConversationEvent]) -> None:
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
        from agenthicc.tui.theme import current_theme

        key = (markup, cols, current_theme())
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

    def _overflow_rows(self, cols: int) -> list[Line]:
        """The bridge row under a collapsed tool group (``└ …and N more tool calls``)."""
        try:
            count = self._state.conversation.live_tool_overflow()
        except Exception:  # noqa: BLE001 - the overflow row is best-effort
            return []
        if not isinstance(count, int) or count <= 0:
            return []
        from agenthicc.tui.theme import glyphs, palette

        word = "call" if count == 1 else "calls"
        g = glyphs()
        muted = palette().muted
        text = f"  {g.elbow} {g.ellipsis}and {count} more tool {word}"
        markup = f"[{muted}]{text}[/]" if muted not in ("", "none") else text
        return list(self._markup(markup, cols))

    def _build_lines(self) -> list[Line]:
        """Return the live block's rows at the console's current width (TUX-02)."""
        cols = max(1, self._console.width)
        overlay = self.overlays.widget
        lines: list[Line] = self._overflow_rows(cols)

        # The framed composer, or an overlay in its place.
        if overlay is not None and overlay.owns_bottom_rows is True:
            # The agents hub owns the bottom rows and its hint row is the only
            # one: the frame and the composer's hints are hidden (§2.4).
            notices = self._notice_lines(cols)
            fixed = len(lines) + self.status.height(cols) + len(notices)
            self.overlays.set_viewport(cols, max(1, _get_rows(self._console) - fixed))
            lines += self._markup(self.status.markup(cols), cols)
            lines += notices
            rendered = self.overlays.render()
            if rendered is not None:
                lines += self._render(rendered, cols)
            return lines

        if overlay is not None:
            form = "none" if self.frame.form(cols) == "none" else "rules"
            notices = self._notice_lines(cols)
            fixed = len(lines) + (2 if form == "rules" else 0) + self.status.height(cols)
            self.overlays.set_viewport(
                cols, max(1, _get_rows(self._console) - fixed - len(notices))
            )
            if form == "rules":
                lines += self._markup(self.frame.top(cols, form), cols)
            rendered = self.overlays.render()
            if rendered is not None:
                lines += self._render(rendered, cols)
            if form == "rules":
                lines += self._markup(self.frame.bottom(cols, form), cols)
            lines += self._markup(self.status.markup(cols), cols)
            lines += notices
            return lines

        form = self.frame.form(cols)
        if form != "none":
            lines += self._markup(self.frame.top(cols, form), cols)
        lines += self.frame.body(cols, form)
        if form != "none":
            lines += self._markup(self.frame.bottom(cols, form), cols)
        lines += self._subagent_rows(cols, form == "none")

        lines += self._markup(self.status.markup(cols), cols)
        notices = self._notice_lines(cols)
        if notices:
            lines += notices
        else:
            hint = self.frame.hint_row(cols)
            if hint is not None:
                lines += self._markup(hint, cols)
        return lines

    def _notice_lines(self, cols: int) -> list[Line]:
        rows: list[Line] = []
        for row in notice_rows(self._state, cols):
            rows += self._markup(row, cols)
        return rows

    def _subagent_rows(self, cols: int, plain: bool) -> list[Line]:
        """The subagent panel below the bordered input area while a pool has rows."""
        pool = self._state.conversation.subagent_pool_state()
        if pool is None:
            return []
        from agenthicc.tui.workspace.subagent_panel import panel_lines

        hint = self._state.keymap().hint("subagents.focus")
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

    # ── status sources (TUX-02) ───────────────────────────────────────────────

    def _start_status_sources(self) -> None:
        """Read the git branch now and start the user's status-line command."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a synchronous test): nothing to run in the background
        self._refresh_git()
        ui = _ui(self._state)
        command = ui.statusline_command if ui is not None else ""
        if command:
            from agenthicc.tui.statusline import StatusLineCommand

            cwd = self._state.conversation.current_directory()
            self._statusline = StatusLineCommand(command, self._state, cwd)
            self._statusline.start()

    def _refresh_git(self) -> None:
        if self._git_task is not None and not self._git_task.done():
            return
        cwd = self._state.conversation.current_directory()
        if not isinstance(cwd, str) or not cwd:
            return
        from agenthicc.tui.statusline import refresh_git_status

        self._git_task = asyncio.get_running_loop().create_task(
            refresh_git_status(self._state, cwd), name="agenthicc-git-status"
        )

    def _on_agent_state(self) -> None:
        """After a turn the branch or the dirty mark may have changed."""
        running = self._state.conversation.is_running() is True
        if self._was_running and not running:
            try:
                self._refresh_git()
            except RuntimeError:
                pass
        self._was_running = running

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
        except Exception:  # noqa: BLE001 - render failures should not disable input
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

    def _repaint_for_theme(self) -> None:
        """Drop cached rows and repaint the whole block (a theme changed)."""
        self._markup_rows.clear()
        if self._live is not None:
            self._live.invalidate()
        self._flush_redraw()

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
        except Exception:  # noqa: BLE001 - SIGWINCH can race terminal teardown
            pass
