"""PromptRenderer — all ANSI terminal I/O for the idle input bar.

Single responsibility: given a snapshot of buffer + cursor + dropdown state,
produce the correct ANSI escape sequence and write it to stdout.

No state is kept between renders except ``_prev_rows`` which tracks how
many rows were written below the input line so the erase step (ESC[0J) is
applied at the right terminal cursor position.

The rendering invariant (maintained by every render path):
    After render() returns, the terminal cursor is on the FIRST input row.
This lets the next call start with ``\\r\\x1b[0J`` and always erase correctly.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

PROMPT_CHAR = "❯"
CURSOR_CHAR = "▌"
_INDENT = "  "
_MAX_VISIBLE = 8


@dataclass
class DropdownState:
    """What to show below the input line."""
    active: bool = False
    matches: list[Any] = field(default_factory=list)
    selected: int = 0
    hint: str | None = None
    trigger_char: str = "@"
    fragment: str = ""


def _truncate(text: str, max_cols: int) -> str:
    """Truncate plain-text *text* to *max_cols* visible columns."""
    visible = 0
    in_esc = False
    for i, ch in enumerate(text):
        if ch == "\x1b":
            in_esc = True
        elif in_esc and ch == "m":
            in_esc = False
        elif not in_esc:
            visible += 1
            if visible > max_cols:
                return text[:i] + "\x1b[0m"
    return text


def build_prompt(
    buf: list[str],
    cursor: int,
    mention_suffix: str = "",
    in_trigger: bool = False,
) -> str:
    """Return the ANSI prompt string (no trailing newline).

    Lines are joined with ``\\n\\r`` so each line starts at column 0 when
    written to the terminal.  The ``▌`` cursor is placed at ``cursor``.
    """
    if in_trigger:
        content = "".join(buf) + mention_suffix + f"\x1b[1m{CURSOR_CHAR}\x1b[0m"
        return f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m {content}"

    # Split buf into logical lines at each '\n'.
    raw_lines: list[list[str]] = []
    current: list[str] = []
    for ch in buf:
        if ch == "\n":
            raw_lines.append(current)
            current = []
        else:
            current.append(ch)
    raw_lines.append(current)

    # Locate which line and column the cursor sits on.
    cumulative = 0
    cursor_line = len(raw_lines) - 1
    cursor_col = len(raw_lines[-1])
    for i, ln in enumerate(raw_lines):
        if cumulative + len(ln) >= cursor:
            cursor_line = i
            cursor_col = cursor - cumulative
            break
        cumulative += len(ln) + 1  # +1 for '\n'

    rendered: list[str] = []
    for i, ln in enumerate(raw_lines):
        if i == cursor_line:
            col = cursor_col
            line_text = (
                "".join(ln[:col])
                + f"\x1b[1m{CURSOR_CHAR}\x1b[0m"
                + "".join(ln[col:])
            )
        else:
            line_text = "".join(ln)
        prefix = f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m " if i == 0 else _INDENT
        rendered.append(prefix + line_text)

    return "\n\r".join(rendered)


def build_footer(mode_str: str, cols: int) -> tuple[str, str]:
    """Return ``(border, mode_line)`` ANSI strings."""
    border = "\x1b[2m" + "─" * cols + "\x1b[0m"
    safe = _truncate(mode_str, max(8, cols - 4))
    mode = f"  \x1b[2m{safe}\x1b[0m"
    return border, mode


class PromptRenderer:
    """Renders the idle input bar to stdout using raw ANSI escape sequences.

    Call :meth:`render` once per keystroke (after state has been updated).
    """

    def __init__(self, out: Any = None) -> None:
        self._out = out or sys.stdout

    def render(
        self,
        buf: list[str],
        cursor: int,
        dropdown: DropdownState,
        mode_line: str | None = None,
    ) -> int:
        """Erase old content and redraw the full prompt + footer + dropdown.

        Returns the number of rows written below the input line (footer +
        dropdown).  Callers that store ``prev_n_lines`` can use this value.
        """
        import shutil  # noqa: PLC0415

        out = self._out
        cols = shutil.get_terminal_size((80, 24)).columns

        # ── Step 1: erase from start of input row to end of screen ───────────
        # Invariant: terminal cursor is on the first input row when we start.
        out.write("\r\x1b[0J")

        # ── Step 2: write the prompt ──────────────────────────────────────────
        mention_suffix = (dropdown.trigger_char + dropdown.fragment) if dropdown.active else ""
        prompt = build_prompt(buf, cursor, mention_suffix, dropdown.active)
        extra_input_rows = prompt.count("\n\r")   # multiline input rows - 1
        out.write(prompt)

        # ── Step 3: write the footer (border + mode line) ─────────────────────
        n_footer = 0
        if mode_line is not None:
            border, mode = build_footer(mode_line, cols)
            out.write(f"\n\r{border}")
            out.write(f"\n\r{mode}")
            n_footer = 2

        # ── Step 4: write dropdown below footer ───────────────────────────────
        dropdown_lines: list[str] = []
        if dropdown.active and dropdown.matches:
            max_entry = max(cols - 6, 8)
            n = min(_MAX_VISIBLE, len(dropdown.matches))
            scroll = max(0, min(dropdown.selected - n + 1, len(dropdown.matches) - n))
            visible = dropdown.matches[scroll : scroll + n]
            for i, item in enumerate(visible):
                actual = scroll + i
                indicator = "▶" if actual == dropdown.selected else " "
                raw = f"+ {item.display}"
                name = raw if len(raw) <= max_entry else raw[: max_entry - 1] + "…"
                if actual == dropdown.selected:
                    dropdown_lines.append(f"\r  \x1b[7m{indicator} {name}\x1b[0m")
                else:
                    dropdown_lines.append(f"\r  {indicator} {name}")

            if dropdown.hint is not None:
                sep = "─" * min(cols - 4, 60)
                dropdown_lines.append(f"\r  \x1b[2m{sep}\x1b[0m")
                dropdown_lines.append(f"\r  \x1b[2m{dropdown.hint[:cols - 4]}\x1b[0m")

            below = len(dropdown.matches) - (scroll + n)
            if below > 0:
                dropdown_lines.append(f"\r  \x1b[2m… {below} more ↓\x1b[0m")
            elif scroll > 0:
                dropdown_lines.append(f"\r  \x1b[2m↑ {scroll} more above\x1b[0m")

        if dropdown_lines:
            out.write("\n" + "\n".join(dropdown_lines))

        # ── Step 5: return cursor to first input row ──────────────────────────
        total_below = extra_input_rows + n_footer + len(dropdown_lines)
        if total_below > 0:
            out.write(f"\x1b[{total_below}A")
            out.write("\r" + prompt)   # rewrite prompt from col 0
            if extra_input_rows:
                out.write(f"\x1b[{extra_input_rows}A")

        # ── Step 6: position terminal cursor at insertion point ───────────────
        # The terminal cursor is invisible (hidden by raw_mode's \x1b[?25l) but
        # we keep it accurate for terminals that use it for selection / IME.
        if not dropdown.active and cursor < len(buf):
            cols_left = len(buf) - cursor
            if cols_left > 0:
                out.write(f"\x1b[{cols_left}D")

        out.flush()
        return total_below

    def scrub_cursor(self, buf: list[str]) -> None:
        """Rewrite all input lines without ▌ so it does not persist after submit."""
        out = self._out
        lines = "".join(buf).split("\n")
        out.write(f"\r\x1b[1;32m{PROMPT_CHAR}\x1b[0m {lines[0]}\x1b[0K")
        for line in lines[1:]:
            out.write(f"\n\r{_INDENT}{line}\x1b[0K")
        n = len(lines)
        if n > 1:
            out.write(f"\x1b[{n - 1}A")
        out.flush()

    def erase_below(self, n_input_rows: int = 1) -> None:
        """Erase the footer + dropdown rows below the current input block."""
        out = self._out
        out.write("\n" * n_input_rows)
        out.write("\r\x1b[0J")
        out.write(f"\x1b[{n_input_rows}A")
        out.flush()

    def show_exit_hint(self, resume_id: str = "") -> None:
        """Print the --resume / --continue hint below the input bar."""
        import shutil  # noqa: PLC0415
        out = self._out
        cols = shutil.get_terminal_size((80, 24)).columns
        border = "\x1b[2m" + "─" * cols + "\x1b[0m"
        if resume_id:
            hints = [
                f"  \x1b[2mTo resume:\x1b[0m \x1b[1magenthicc --resume {resume_id}\x1b[0m",
                "  \x1b[2mOr in the same directory:\x1b[0m \x1b[1magenthicc --continue\x1b[0m",
            ]
        else:
            hints = [
                "  \x1b[2mTo resume:\x1b[0m \x1b[1magenthicc --continue\x1b[0m"
                "\x1b[2m  (in the same directory)\x1b[0m",
            ]
        text = "\n\r" + border
        for h in hints:
            text += "\n\r" + h
        text += "\n\n"
        out.write(text)
        out.flush()
