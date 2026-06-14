"""FrameComposer — pure function (state, size) → Frame."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .terminal import Size

__all__ = ["Frame", "FrameComposer", "simple_wrap"]

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]|\x1b\][^\x07]*\x07')

SPINNER_FRAMES: list[str] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
_THINKING_TEXT = 'Thinking…'  # "Thinking…"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def simple_wrap(text: str, cols: int) -> list[str]:
    """Hard-wrap text to at most `cols` characters per line.

    Splits on existing newlines first, then hard-wraps each segment.
    With cols <= 0 returns the text as a single-element list (degenerate).
    Empty string returns [""].
    """
    if cols <= 0:
        return [text]
    result: list[str] = []
    for paragraph in text.split('\n'):
        if not paragraph:
            result.append('')
            continue
        while len(paragraph) > cols:
            result.append(paragraph[:cols])
            paragraph = paragraph[cols:]
        result.append(paragraph)
    return result if result else ['']


@dataclass(frozen=True)
class Frame:
    committed: list[str] = field(default_factory=list)
    bottom: list[str] = field(default_factory=list)
    height: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "height", len(self.bottom))


def _thinking_wave(frame_num: int) -> str:
    """Bold char sweeps L to R then R to L through 'Thinking...'."""
    text = _THINKING_TEXT
    length = len(text)
    if length <= 1:
        return text
    cycle = 2 * (length - 1)
    pos = frame_num % cycle
    if pos >= length:
        pos = cycle - pos
    result = []
    for i, ch in enumerate(text):
        if i == pos:
            result.append(f'\x1b[1m{ch}\x1b[22m')
        else:
            result.append(ch)
    return ''.join(result)


def _render_status_active(status: Any, cols: int, frame_num: int) -> str:
    spinner_frame = getattr(status, 'spinner_frame', frame_num)
    spinner = SPINNER_FRAMES[spinner_frame % len(SPINNER_FRAMES)]
    thinking = _thinking_wave(frame_num)
    started_at = getattr(status, 'intent_started_at', 0.0)
    elapsed = time.monotonic() - started_at if started_at else 0.0
    input_tokens = getattr(status, 'input_tokens', 0)
    output_tokens = getattr(status, 'output_tokens', 0)
    tok_in = f'\x1b[36m↑ {input_tokens:,}\x1b[0m'
    tok_out = f'\x1b[32m↓ {output_tokens:,}\x1b[0m'
    line = (
        f' \x1b[36m{spinner}\x1b[0m {thinking}'
        f'  \x1b[2m{elapsed:.1f}s\x1b[0m'
        f'  \x1b[2m│\x1b[0m  {tok_in}  {tok_out}'
    )
    return line[:cols] if len(line) > cols else line


def _render_status_idle(status: Any, cols: int) -> str:
    session_id = getattr(status, 'session_id', 'unknown')
    model_name = getattr(status, 'model_name', '')
    completed = getattr(status, 'completed_agents', 0)
    cost = getattr(status, 'session_cost_usd', 0.0)
    input_tokens = getattr(status, 'input_tokens', 0)
    output_tokens = getattr(status, 'output_tokens', 0)
    parts: list[str] = [f'\x1b[2m{session_id}\x1b[0m']
    if model_name:
        parts.append(f'\x1b[2m{model_name}\x1b[0m')
    parts.append(f'\x1b[2m{completed} turns\x1b[0m')
    parts.append(f'\x1b[2m${cost:.3f}\x1b[0m')
    parts.append(f'\x1b[36m↑ {input_tokens:,}\x1b[0m')
    parts.append(f'\x1b[32m↓ {output_tokens:,}\x1b[0m')
    sep = '  \x1b[2m│\x1b[0m  '
    line = '  ' + sep.join(parts)
    return line[:cols] if len(line) > cols else line


def _render_mode_footer(mode: str, active: bool, cols: int) -> str:
    hints = 'enter:send  shift+tab:mode  /:commands  @:files'
    line = f'  \x1b[2m{mode}  {hints}\x1b[0m'
    return line[:cols] if len(line) > cols else line


class FrameComposer:
    """Pure render function: (transcript, status, input_state) -> Frame.

    Caches committed output so compose() is O(new lines) not O(all lines).
    The transcript is expected to implement a .render() -> list[str] method.
    """

    def __init__(self, cols: int = 80) -> None:
        self._cols = cols
        self._committed_cache: list[str] = []
        self._committed_len: int = 0

    def compose(
        self,
        transcript: Any | None,
        status: Any | None,
        input_state: Any | None,
        cols: int | None = None,
        frame_num: int = 0,
        *,
        now: float | None = None,
    ) -> Frame:
        effective_cols = cols if cols is not None else self._cols

        # -- Committed lines from transcript ----------------------------------
        if transcript is not None:
            all_lines = transcript.render()
            new_lines = all_lines[self._committed_len:]
            if new_lines:
                self._committed_cache.extend(new_lines)
                self._committed_len = len(all_lines)

        committed = list(self._committed_cache)

        # -- Bottom block -----------------------------------------------------
        bottom = self._compose_bottom(status, input_state, effective_cols, frame_num)

        return Frame(committed=committed, bottom=bottom)

    def _compose_bottom(
        self,
        status: Any | None,
        input_state: Any | None,
        cols: int,
        frame_num: int,
    ) -> list[str]:
        rows: list[str] = []

        # Zone 1: partial / streaming text (shown when non-empty after strip)
        partial_text = getattr(status, 'partial_text', '') if status is not None else ''
        if partial_text and partial_text.strip():
            wrapped = simple_wrap(partial_text, max(cols - 4, 10))
            for line in wrapped:
                rows.append(f'  \x1b[2m{line}\x1b[0m')

        # Zone 2: status bar
        active = getattr(status, 'active', False) if status is not None else False
        if status is not None:
            if active:
                rows.append(_render_status_active(status, cols, frame_num))
            else:
                rows.append(_render_status_idle(status, cols))
        else:
            rows.append('')

        # Zone 3: divider
        rows.append('─' * cols)

        # Zone 4: dropdown rows (before input)
        if input_state is not None:
            dropdown_rows = getattr(input_state, 'dropdown_rows', [])
            rows.extend(dropdown_rows)

        # Zone 5: input rows (one row per line in input text)
        if input_state is not None:
            text = getattr(input_state, 'text', '')
            raw_lines = text.split('\n') if text else ['']
            for i, line in enumerate(raw_lines):
                if i == 0:
                    rows.append(f'\x1b[1;32m❯\x1b[0m {line}')
                else:
                    rows.append(f'  {line}')
        else:
            rows.append('\x1b[1;32m❯\x1b[0m ')

        # Zone 6: mode footer
        mode = ''
        if status is not None:
            mode = getattr(status, 'mode_name', 'Auto')
        if not mode:
            mode = 'Auto'
        rows.append(_render_mode_footer(mode, active, cols))

        return rows

    def reset(self) -> None:
        """Clear the committed cache. Call when starting a new session."""
        self._committed_cache = []
        self._committed_len = 0
