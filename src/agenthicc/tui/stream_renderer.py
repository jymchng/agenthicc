from __future__ import annotations

import sys
import time
from typing import Any

# NOTE: rich.live must NOT be imported (enforced by test_no_rich_live_import)

_MAX_FLUSH_CHARS: int = 120
_DIFF_MAX_LINES: int = 8


class StreamRenderer:
    """Streaming turn renderer that writes to a Rich Console and sys.stdout.

    Responsibilities:
    - Buffer incoming text deltas; flush on newline or >= _MAX_FLUSH_CHARS chars.
    - Print tool call start/end immediately through the Rich console.
    - Print a thinking header to sys.stdout on turn start.
    - Print a summary line via console.print on finish().
    """

    def __init__(self, console: Any, status: Any) -> None:
        self._console = console
        self._status = status
        self._text_buf: list[str] = []
        self._pending: dict[str, tuple[str, str]] = {}  # id -> (name, args)
        self._turn_start_time: float = 0.0

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def on_turn_start(self) -> None:
        """Print the animated Thinking header to sys.stdout and reset state."""
        # Reset per-turn state
        self._pending = {}
        self._text_buf = []
        self._turn_start_time = time.monotonic()

        # Import _thinking_wave lazily to avoid circular imports with app.py.
        # Tests patch agenthicc.tui.app._thinking_wave to intercept this call.
        try:
            from agenthicc.tui.app import _thinking_wave  # noqa: PLC0415
            thinking = _thinking_wave(0)
        except (ImportError, AttributeError):
            thinking = 'Thinking…'

        # Write directly to sys.stdout (not through Rich console)
        sys.stdout.write(f'\x1b[36m⠋\x1b[0m {thinking}\n')
        sys.stdout.flush()

    def on_turn_end(self, turn_text: str = '') -> None:
        """Flush any buffered text at the end of a turn."""
        self._flush_buf()

    def finish(self) -> None:
        """Print a summary line with elapsed time and token counts."""
        elapsed = time.monotonic() - self._turn_start_time
        input_tok = getattr(self._status, 'input_tokens', 0)
        output_tok = getattr(self._status, 'output_tokens', 0)
        cost = getattr(self._status, 'session_cost_usd', 0.0)
        self._console.print(
            f'\x1b[2m'
            f'{elapsed:.1f}s  '
            f'↑{input_tok:,}  ↓{output_tok:,}  '
            f'${cost:.4f}'
            f'\x1b[0m'
        )

    # ------------------------------------------------------------------
    # Text streaming
    # ------------------------------------------------------------------

    def on_text_delta(self, text: str) -> None:
        """Buffer a text chunk; flush if newline present or buffer is large."""
        self._text_buf.append(text)
        total = sum(len(t) for t in self._text_buf)
        combined = ''.join(self._text_buf)
        if '\n' in text or total >= _MAX_FLUSH_CHARS:
            self._flush_buf()

    def _flush_buf(self) -> None:
        if not self._text_buf:
            return
        combined = ''.join(self._text_buf)
        self._text_buf = []
        if combined:
            self._console.print(combined, end='')

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def on_tool_started(self, tool_use_id: str, name: str, args: str) -> None:
        """Record that a tool call has started."""
        self._pending[tool_use_id] = (name, args)

    def on_tool_complete(
        self,
        tool_use_id: str,
        success: bool,
        duration_ms: float,
        error: str | None = None,
        diff: str | None = None,
    ) -> None:
        """Print the tool call result."""
        name, args = self._pending.pop(tool_use_id, (tool_use_id, ''))
        icon = '✓' if success else '✗'
        color = '\x1b[32m' if success else '\x1b[31m'
        dur_str = f' {duration_ms:.0f}ms' if duration_ms > 0 else ''
        args_str = f' {args}' if args else ''
        line = f'{color}{icon}\x1b[0m {name}{args_str}{dur_str}'
        if not success and error:
            line += f': {error}'
        self._console.print(line)

        # Print diff if provided
        if diff:
            diff_lines = diff.splitlines()
            if len(diff_lines) > _DIFF_MAX_LINES:
                overflow = len(diff_lines) - _DIFF_MAX_LINES
                for dl in diff_lines[:_DIFF_MAX_LINES]:
                    self._console.print(f'  {dl}')
                self._console.print(f'  \x1b[2m… {overflow} more line(s)\x1b[0m')
            else:
                for dl in diff_lines:
                    if dl.startswith('@@'):
                        self._console.print(f'  [cyan]{dl}[/cyan]', markup=True)
                    elif dl.startswith('-') and not dl.startswith('---'):
                        self._console.print(f'  [red]{dl}[/red]', markup=True)
                    elif dl.startswith('+') and not dl.startswith('+++'):
                        self._console.print(f'  [green]{dl}[/green]', markup=True)
                    else:
                        self._console.print(f'  {dl}')

    # ------------------------------------------------------------------
    # Status bar rendering
    # ------------------------------------------------------------------

    def render_status_bar(self, width: int) -> str:
        """Render a status bar string at the given terminal width."""
        spinner_frame = getattr(self._status, 'spinner_frame', 0)
        frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        spinner = frames[spinner_frame % len(frames)]
        active = getattr(self._status, 'active', False)
        completed = getattr(self._status, 'completed_agents', 0)
        input_tok = getattr(self._status, 'input_tokens', 0)
        output_tok = getattr(self._status, 'output_tokens', 0)
        if active:
            bar = f' {spinner} Thinking…  ↑{input_tok:,}  ↓{output_tok:,}'
        else:
            bar = f'  {completed} turns  ↑{input_tok:,}  ↓{output_tok:,}'
        return bar[:width] if len(bar) > width else bar


# Expose _thinking_wave for tests that patch it
def _thinking_wave(frame_num: int) -> str:
    """Delegate to app._thinking_wave for backward compatibility."""
    try:
        from agenthicc.tui.app import _thinking_wave as _tw  # noqa: PLC0415
        return _tw(frame_num)
    except ImportError:
        return "Thinking..."
