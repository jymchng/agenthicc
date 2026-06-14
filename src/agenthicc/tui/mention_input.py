"""Custom CBREAK-mode input line with an inline @mention file-picker dropdown.

Architecture (5 layers):

  Layer 1 - TerminalRawMode (_raw_mode): context manager that enables CBREAK,
            suppresses ^C echo, and restores original settings on exit. Reads
            POST-CBREAK termios settings so the ECHOCTL patch is applied on top
            of CBREAK — not instead of it (the classic bug: reading old_tty
            before setcbreak and reapplying it would undo CBREAK entirely).

  Layer 2 - Key enum + _read_key(fd): reads one keystroke using os.read(fd, 1)
            for raw bytes; handles escape sequences with a 50 ms peek timeout;
            decodes printable UTF-8 characters.

  Layer 3 - File matching (_get_matches): returns [(display_path, meta), ...]
            for filesystem entries whose name starts with the current fragment;
            dirs sorted before files, hidden entries skipped.

  Layer 4 - Rendering (_redraw): erases previous dropdown rows, redraws the
            input line, and renders the dropdown below the cursor using ANSI
            sequences; moves the cursor back up so the input line stays active.

  Layer 5 - State machine (read_line_with_mention): drives all state: normal
            editing, history navigation, @-mention picker open/close, and the
            double-Ctrl+C exit confirmation.

No prompt_toolkit dependency — uses raw terminal I/O and plain ANSI sequences.
"""
from __future__ import annotations

import os
import select
import sys
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from agenthicc.modes import ModeManager

from agenthicc.tui.trigger import TriggerRegistry, TriggerHandler, TriggerContext, MatchItem
from agenthicc.tui.menu import MenuDriver, MenuResultKind, MenuWidget
from agenthicc.tui.input_area import PROMPT_CHAR

__all__ = ["read_line_with_mention", "Key"]

_MAX_VISIBLE = 8
_PROMPT_STYLE = f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m "  # bold green ❯ + space

# ── Exit messages — centralised here so they can never be lost ────────────────

def _show_exit_hint(resume_id: str = "") -> None:
    """Render the resume hint below the input bar border and flush."""
    import shutil as _sh  # noqa: PLC0415
    cols = _sh.get_terminal_size((80, 24)).columns
    border = "\x1b[2m" + "─" * cols + "\x1b[0m"
    if resume_id:
        hint_lines = [
            f"  \x1b[2mTo resume:\x1b[0m "
            f"\x1b[1magenthicc --resume {resume_id}\x1b[0m",
            f"  \x1b[2mOr in the same directory:\x1b[0m "
            f"\x1b[1magenthicc --continue\x1b[0m",
        ]
    else:
        hint_lines = [
            f"  \x1b[2mTo resume:\x1b[0m "
            f"\x1b[1magenthicc --continue\x1b[0m"
            f"\x1b[2m  (in the same directory)\x1b[0m",
        ]
    out = "\n\r" + border
    for line in hint_lines:
        out += "\n\r" + line
    out += "\n\n"
    sys.stdout.write(out)
    sys.stdout.flush()


# ── Layer 1: TerminalRawMode context manager ──────────────────────────────────

@contextmanager
def _raw_mode(fd: int) -> Generator[int, None, None]:
    """Enable CBREAK on *fd*, yield it, restore on exit.

    IMPORTANT: We read POST-CBREAK termios settings (not pre-CBREAK) so that
    the ECHOCTL patch is layered on top of CBREAK rather than overwriting it.
    Reading old_tty before setcbreak and using it for the patch would silently
    undo CBREAK — that is the bug this design explicitly avoids.
    """
    import termios
    import tty

    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)                     # enable CBREAK
        cur = list(termios.tcgetattr(fd))     # read POST-CBREAK settings (NOT old)
        # Clear ICRNL (iflag) so the terminal driver does NOT translate \r → \n.
        # Without this, Enter (\r) arrives as \n which our code maps to CTRL_ENTER
        # (insert newline) instead of ENTER (submit).  setcbreak only clears ECHO
        # and ICANON; it leaves ICRNL intact, so we must clear it ourselves.
        cur[0] &= ~termios.ICRNL
        # Clear ISIG so Ctrl+C delivers \x03 to stdin instead of raising SIGINT.
        # Without this, Python would throw KeyboardInterrupt in the thread even
        # though our state machine already handles b"\x03" gracefully.
        cur[3] &= ~(termios.ECHOCTL | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, cur)
        # Hide the OS cursor so it does not appear alongside the ▌ indicator
        # that prompt_ansi() renders at the insertion point.
        # Request Kitty Keyboard Protocol "disambiguate" mode so terminals that
        # support it (kitty, WezTerm, foot, …) send \x1b[13;5u for Ctrl+Enter
        # instead of the indistinguishable \r.  Terminals that don't understand
        # the escape simply ignore it, so this is safe everywhere.
        sys.stdout.write("\x1b[?25l")   # hide OS cursor (we draw ▌ ourselves)
        sys.stdout.flush()
        yield fd
    finally:
        sys.stdout.write("\x1b[?25h")   # restore cursor visibility
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Layer 2: Key enum + _read_key ────────────────────────────────────────────

class Key(str, Enum):
    UP        = "UP"
    DOWN      = "DOWN"
    LEFT      = "LEFT"
    RIGHT     = "RIGHT"
    HOME      = "HOME"
    END       = "END"
    ENTER     = "ENTER"
    CTRL_ENTER = "CTRL_ENTER"  # insert newline (multi-line input)
    TAB       = "TAB"
    ESC       = "ESC"
    BACKSPACE = "BACKSPACE"
    CTRL_C    = "CTRL_C"
    CTRL_D    = "CTRL_D"
    CTRL_U    = "CTRL_U"
    SHIFT_TAB = "SHIFT_TAB"
    AT        = "AT"
    CHAR      = "CHAR"


def _read_key(fd: int) -> tuple[Key, str]:
    """Read one keystroke from *fd* and return (Key, char_or_empty).

    Uses os.read(fd, 1) for raw bytes.  Escape sequences are parsed with a
    50 ms peek timeout so a lone ESC is distinguished from cursor-key sequences.
    """
    b = os.read(fd, 1)

    if b == b"\x03":
        return (Key.CTRL_C, "")
    if b == b"\x04":
        return (Key.CTRL_D, "")
    if b == b"\r":
        return (Key.ENTER, "")
    if b == b"\n":
        # Ctrl+J (\n) — reliably distinct from Enter (\r) in CBREAK mode.
        # Fallback for terminals without Kitty Keyboard Protocol support.
        return (Key.CTRL_ENTER, "")
    if b == b"\t":
        return (Key.TAB, "")
    if b in (b"\x7f", b"\x08"):
        return (Key.BACKSPACE, "")
    if b == b"\x15":
        return (Key.CTRL_U, "")
    if b == b"@":
        return (Key.AT, "")

    if b == b"\x1b":
        # Peek with a short timeout to distinguish lone ESC from sequences.
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return (Key.ESC, "")
        b2 = os.read(fd, 1)
        if b2 != b"[":
            return (Key.ESC, "")

        # CSI sequence: read bytes until the final byte (a letter or '~').
        # This loop replaces the old single-byte lookahead so multi-byte
        # sequences like \x1b[13;5u (Kitty KP Ctrl+Enter) are parsed correctly.
        seq = b""
        while True:
            r_s, _, _ = select.select([fd], [], [], 0.05)
            if not r_s:
                break
            b_s = os.read(fd, 1)
            seq += b_s
            if b_s[-1:] in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz~":
                break

        if seq == b"A":      return (Key.UP, "")
        if seq == b"B":      return (Key.DOWN, "")
        if seq == b"C":      return (Key.RIGHT, "")
        if seq == b"D":      return (Key.LEFT, "")
        if seq == b"H":      return (Key.HOME, "")
        if seq == b"F":      return (Key.END, "")
        if seq == b"Z":      return (Key.SHIFT_TAB, "")
        if seq == b"1~":     return (Key.HOME, "")
        if seq == b"3~":     return (Key.CHAR, "")    # Delete — ignore
        if seq == b"4~":     return (Key.END, "")
        return (Key.ESC, "")

    # Printable or multi-byte UTF-8
    raw = b
    # Handle multi-byte UTF-8: first byte encodes length
    first = b[0]
    if first & 0b11100000 == 0b11000000:
        n_extra = 1
    elif first & 0b11110000 == 0b11100000:
        n_extra = 2
    elif first & 0b11111000 == 0b11110000:
        n_extra = 3
    else:
        n_extra = 0

    for _ in range(n_extra):
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            raw += os.read(fd, 1)

    try:
        ch = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (Key.ESC, "")

    if ch.isprintable():
        return (Key.CHAR, ch)
    return (Key.ESC, "")


# ── Layer 3: File matching ────────────────────────────────────────────────────

def _get_matches(fragment: str, cwd: Path) -> list[tuple[str, str]]:
    """Return [(display_path, ""), ...] for entries matching *fragment*.

    When *fragment* has no "/" (top-level search), matching directories are
    listed first and their immediate children are also included inline so the
    user can see both "@tests/" and "@tests/conftest.py" from just "@te".

    When *fragment* contains "/" (already navigating a subdirectory) the
    classic prefix-filter behaviour is used instead.

    Hidden entries (names starting with '.') are always skipped.
    "/" suffix on display_path distinguishes directories from files.
    """
    def _iter_dir(path: Path) -> list[tuple[str, str]]:
        try:
            return sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return []

    # ── navigating into a subdirectory ──────────────────────────────────────
    if "/" in fragment:
        dir_part, file_prefix = fragment.rsplit("/", 1)
        search_dir = cwd / dir_part
        if not search_dir.is_dir():
            return []
        results: list[tuple[str, str]] = []
        for entry in _iter_dir(search_dir):
            if entry.name.startswith("."):
                continue
            if not entry.name.startswith(file_prefix):
                continue
            suffix = "/" if entry.is_dir() else ""
            results.append((f"{dir_part}/{entry.name}{suffix}", ""))
        return results

    # ── top-level search: match + expand matching directories ─────────────
    search_dir = cwd
    if not search_dir.is_dir():
        return []

    results = []
    for entry in _iter_dir(search_dir):
        if entry.name.startswith("."):
            continue
        if not entry.name.startswith(fragment):
            continue
        suffix = "/" if entry.is_dir() else ""
        results.append((f"{entry.name}{suffix}", ""))
        if entry.is_dir():
            # Also list immediate children of matching directories.
            for child in _iter_dir(entry):
                if child.name.startswith("."):
                    continue
                child_suffix = "/" if child.is_dir() else ""
                results.append((f"{entry.name}/{child.name}{child_suffix}", ""))
    return results


# ── Layer 4: Rendering ────────────────────────────────────────────────────────

def _redraw(
    prompt_str: str,
    buf: list[str],
    fragment: str,
    matches: list[MatchItem],
    selected: int,
    prev_n_lines: int,
    in_trigger: bool,
    hint: str | None = None,
    trigger_char: str = "@",
    mode_line: str | None = None,
    cursor: int | None = None,
) -> int:
    """Erase old dropdown, redraw the input line, render new dropdown.

    Returns the number of dropdown lines now visible (caller stores this as
    *prev_n_lines* for the next iteration).
    """
    import shutil

    out = sys.stdout
    cols = shutil.get_terminal_size((80, 24)).columns

    # Step 1: erase old content below (and the input line itself).
    #
    # Architectural note: we use ESC[0J ("erase from cursor to end of screen")
    # rather than a counted loop of ESC[2K lines.  The counted approach breaks
    # whenever any line wraps (footer text too wide, wide Unicode, etc.) because
    # prev_n_lines represents *logical* writes, not *terminal rows*.  ESC[0J
    # clears everything below the cursor unconditionally — no row-counting needed.
    #
    # Invariant: _redraw always leaves the cursor ON the input line (see step 5).
    # So \r moves to column 0 of the input line; ESC[0J then clears it and all
    # rows below in a single escape.
    out.write("\r\x1b[0J")

    # Step 2: redraw the (possibly multiline) input.
    mention_suffix = (trigger_char + fragment) if in_trigger else ""
    from agenthicc.tui.input_area import prompt_ansi as _ia_prompt_ansi  # noqa: PLC0415
    _prompt_content = _ia_prompt_ansi(buf, mention_suffix, cursor, in_trigger)
    # Count extra input rows so steps 5a/5b can move the cursor back correctly.
    _prompt_parts = _prompt_content.split("\n\r")
    _n_input_extra = len(_prompt_parts) - 1  # 0 for single-line
    out.write(_prompt_content)  # cursor ends on last input row

    # Step 2b — border + permanent mode footer (truncated so it never wraps)
    n_base = 0
    if mode_line is not None:
        from agenthicc.tui.input_area import footer_ansi as _ia_footer  # noqa: PLC0415
        _safe_line = _truncate_to_cols(mode_line, max(8, cols - 4))
        _border_str, _mode_str = _ia_footer(_safe_line, cols)
        out.write(f"\n\r{_border_str}")
        out.write(f"\n\r{_mode_str}")
        n_base = 2

    # Step 3: render dropdown if in trigger mode with matches.
    # Lines are already blank (cleared by ESC[0J in step 1) so no per-line
    # ESC[2K needed; just move down and write content.
    if in_trigger and matches:
        _max_entry = max(cols - 6, 8)

        n = min(_MAX_VISIBLE, len(matches))
        scroll = max(0, min(selected - n + 1, len(matches) - n))
        visible = matches[scroll : scroll + n]
        lines: list[str] = []
        for i, item in enumerate(visible):
            actual = scroll + i
            indicator = "▶" if actual == selected else " "
            raw = f"+ {item.display}"
            name = raw if len(raw) <= _max_entry else raw[:_max_entry - 1] + "…"
            if actual == selected:
                line = f"\r  \x1b[7m{indicator} {name}\x1b[0m"
            else:
                line = f"\r  {indicator} {name}"
            lines.append(line)

        if hint is not None:
            sep = "─" * min(cols - 4, 60)
            lines.append(f"\r  \x1b[2m{sep}\x1b[0m")
            lines.append(f"\r  \x1b[2m{hint[:cols - 4]}\x1b[0m")

        below = len(matches) - (scroll + n)
        if below > 0:
            lines.append(f"\r  \x1b[2m… {below} more ↓\x1b[0m")
        elif scroll > 0:
            lines.append(f"\r  \x1b[2m↑ {scroll} more above\x1b[0m")

        # Step 5a (dropdown path): move cursor back up to first input row.
        # _n_input_extra accounts for any extra rows in a multiline input.
        new_n_lines = n_base + _n_input_extra + len(lines)
        out.write("\n" + "\n".join(lines))
        out.write(f"\x1b[{new_n_lines}A")
        out.write("\r" + _prompt_content)  # rewrite all input lines
        if _n_input_extra:
            out.write(f"\x1b[{_n_input_extra}A")  # back to first input row
        _apply_cursor(out, buf, in_trigger, cursor)
        out.flush()
        return new_n_lines

    # Step 5b (no dropdown): move cursor back up to first input row.
    _total_up = _n_input_extra + n_base
    if _total_up:
        out.write(f"\x1b[{_total_up}A")
        out.write("\r" + _prompt_content)  # rewrite all input lines
        if _n_input_extra:
            out.write(f"\x1b[{_n_input_extra}A")  # back to first input row
    _apply_cursor(out, buf, in_trigger, cursor)
    out.flush()
    return _total_up


def _apply_cursor(
    out: "Any", buf: list[str], in_trigger: bool, cursor: int | None
) -> None:
    """Move the terminal cursor to *cursor* within the buffer if needed.

    Called at the end of every _redraw code path, just before flush.  In
    trigger mode the cursor always sits at the end of the fragment so no
    adjustment is required.  In normal mode we move left by
    ``len(buf) - cursor`` columns when cursor is not already at the end.
    """
    if in_trigger or cursor is None or cursor >= len(buf):
        return
    cols_left = len(buf) - cursor
    if cols_left > 0:
        out.write(f"\x1b[{cols_left}D")


def _truncate_to_cols(text: str, max_visible: int) -> str:
    """Return *text* truncated to at most *max_visible* displayed characters.

    ANSI CSI colour sequences (ESC [ … m) are preserved verbatim but not counted
    toward the visible width.  Any other character (including multi-byte UTF-8
    already decoded) counts as one column.  Appends ESC[0m reset on truncation.
    """
    visible = 0
    in_esc = False
    for i, ch in enumerate(text):
        if ch == "\x1b":
            in_esc = True
        elif in_esc and ch == "m":
            in_esc = False
        elif not in_esc:
            visible += 1
            if visible > max_visible:
                return text[:i] + "\x1b[0m"
    return text


def _find_trigger_tail(
    buf: list[str], registry: TriggerRegistry
) -> "tuple[str, list[str], str] | None":
    """Return (trigger_char, pre_buf, fragment) when buf ends with a trigger token.

    Scans backward from the end of *buf* for a registered trigger character
    with no whitespace between it and the end.  When found, checks that the
    handler would activate at the position of *pre_buf* (i.e. can_activate
    passes).

    Returns None if no activatable trigger tail is found — either because the
    scan hit whitespace first, or because can_activate declined.

    This lets the state machine re-enter trigger mode whenever the user types
    into or backspaces back to an existing ``@…`` or ``/…`` token.
    """
    for i in range(len(buf) - 1, -1, -1):
        ch = buf[i]
        if ch.isspace():
            return None  # whitespace terminates the scan
        if ch in registry.chars:
            pre_buf = buf[:i]
            fragment = "".join(buf[i + 1:])
            handler = registry.get(ch)
            if handler is not None and handler.can_activate(pre_buf):
                return (ch, pre_buf, fragment)
            # This trigger char can't activate here — keep scanning left.
            # Example: '/' in '@docs/index' can't activate SlashCommandTrigger
            # (buf is not empty), but '@' at the start can activate AtMentionTrigger.
    return None  # no activatable trigger char in the non-whitespace suffix


def _scrub_cursor(buf: list[str], n_input_rows: int) -> None:
    """Rewrite all input lines without the ▌ cursor so it does not persist in
    the scroll buffer after the user submits.

    The cursor is assumed to be on the first input line (the invariant
    maintained by _redraw).  Each line is overwritten with ``\\r`` + plain text
    + ``\\x1b[0K`` (erase to end of terminal row) to remove any leftover ▌.
    The cursor is returned to the first input line at the end.
    """
    from agenthicc.tui.input_area import PROMPT_CHAR  # noqa: PLC0415
    _INDENT = "  "
    out = sys.stdout
    lines = "".join(buf).split("\n")
    out.write(f"\r\x1b[1;32m{PROMPT_CHAR}\x1b[0m {lines[0]}\x1b[0K")
    for _line in lines[1:]:
        out.write(f"\n\r{_INDENT}{_line}\x1b[0K")
    if n_input_rows > 1:
        out.write(f"\x1b[{n_input_rows - 1}A")  # back to first input row
    out.flush()


def _erase_below(n_input_rows: int = 1) -> None:
    """Erase footer/dropdown rows before a submit or exit write.

    Steps down past all *n_input_rows* input lines, erases to end of screen,
    then steps back up to the first input line.  ``n_input_rows`` defaults to
    1 for single-line input; pass ``max(1, buf_text.count('\\n') + 1)`` for
    multiline buffers so that lines 2+ are not accidentally cleared.
    """
    out = sys.stdout
    out.write("\n" * n_input_rows)  # step past all input rows
    out.write("\r\x1b[0J")          # erase from here to bottom
    out.write(f"\x1b[{n_input_rows}A")  # step back to first input line
    out.flush()


# ── Layer 5: Main state machine ───────────────────────────────────────────────

def read_line_with_mention(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: TriggerRegistry | None = None,
    initial_menu: "MenuWidget | None" = None,
    resume_id: str = "",
    mode_manager: "ModeManager | None" = None,
) -> str | None:
    """Read one line of input with trigger-dropdown support.

    If stdin is not a TTY, falls back to plain ``input()``.

    Args:
        prompt_str: The prompt string to display (may contain ANSI codes).
        cwd: Working directory used to resolve @-mention file paths.
        history: Mutable list; successfully entered lines are appended in-place.
        registry: Optional :class:`TriggerRegistry`; defaults to one containing
            :class:`~agenthicc.tui.triggers.at_mention.AtMentionTrigger`.
        initial_menu: Optional :class:`~agenthicc.tui.menu.MenuWidget` to open
            immediately at the start of this input cycle (e.g. a command menu
            opened by the previous ``/config`` submission).

    Returns:
        The entered string, or ``None`` on Ctrl+C (double) / Ctrl+D.
    """
    # Non-TTY fallback (e.g. tests, pipes).
    if not sys.stdin.isatty():
        try:
            line = input(prompt_str)
            if line:
                history.append(line)
            return line
        except (EOFError, KeyboardInterrupt):
            return None

    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    _registry = registry
    if _registry is None:
        _registry = TriggerRegistry()
        _registry.register(AtMentionTrigger())
    _ctx = TriggerContext(cwd=cwd, history=history)
    driver = MenuDriver()
    if initial_menu is not None:
        driver.open(initial_menu)

    fd = sys.stdin.fileno()

    # State variables
    buf: list[str] = []
    cursor: int = 0          # insertion point within buf; kept at len(buf) for "end"
    active_handler: TriggerHandler | None = None
    fragment: str = ""
    matches: list[MatchItem] = []
    selected: int = 0
    current_hint: str | None = None
    prev_dropdown_lines: int = 0
    hist_idx: int = len(history)
    saved_buf: list[str] = []
    ctrl_c_count: int = 0

    from typing import Any as _Any  # noqa: PLC0415
    _mode_notification: list[_Any] = [None]

    def _get_mode_line() -> str:
        from agenthicc.tui.input_area import get_mode_str as _ia_mode_str  # noqa: PLC0415
        notif = _mode_notification[0]
        if notif is not None:
            _mode_notification[0] = None
            return f"❖ Switched to {notif.name} mode"
        if ctrl_c_count > 0:
            return "Press Ctrl+C again to exit."
        return _ia_mode_str(mode_manager)

    with _raw_mode(fd):
        while True:
            # Determine what to show in the input bar.
            if driver.active and driver.widget.edit_field_value is not None:
                display_buf = list(driver.widget.edit_field_value)
            elif active_handler is not None:
                display_buf = buf
            else:
                display_buf = buf

            prev_dropdown_lines = _redraw(
                prompt_str, display_buf, fragment, matches, selected,
                prev_dropdown_lines, active_handler is not None, current_hint,
                active_handler.char if active_handler else "@",
                mode_line=_get_mode_line(),
                cursor=cursor,
            )
            if driver.active:
                driver._prev_lines = driver.widget.render(prompt_str, display_buf, driver._prev_lines)

            key, ch = _read_key(fd)

            # ── MenuDriver dispatch ──────────────────────────────────────────
            if driver.active:
                result = driver.handle_key(key, ch)
                if result.kind == MenuResultKind.CANCEL:
                    pass  # menu closed, back to normal editing
                elif result.kind == MenuResultKind.DONE:
                    # Command menus: result.data may be None (side-effects already applied)
                    pass
                ctrl_c_count = 0  # any menu interaction resets ctrl_c_count
                continue

            # ── IN trigger mode ──────────────────────────────────────────────
            if active_handler is not None:
                if key == Key.CTRL_C:
                    # Cancel trigger; let normal CTRL_C logic handle next iter.
                    buf = active_handler.on_cancel(fragment, buf)
                    active_handler = None
                    fragment = ""
                    matches = []
                    current_hint = None
                    cursor = len(buf)
                    _erase_below()
                    prev_dropdown_lines = 0
                    ctrl_c_count += 1
                    if ctrl_c_count == 1:
                        # Warning is rendered by _get_mode_line() on next _redraw.
                        buf = []
                        cursor = 0
                    else:
                        _n_rows = max(1, "".join(buf).count("\n") + 1)
                        _erase_below(_n_rows)
                        _show_exit_hint(resume_id)
                        return None
                    continue

                elif key == Key.ESC:
                    # Cancel trigger — restore trigger+fragment into buf.
                    buf = active_handler.on_cancel(fragment, buf)
                    active_handler = None
                    fragment = ""
                    matches = []
                    current_hint = None
                    cursor = len(buf)
                    # Erase dropdown immediately.
                    prev_dropdown_lines = _redraw(
                        prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None,
                        mode_line=_get_mode_line(),
                    )

                elif key in (Key.ENTER, Key.TAB):
                    item = matches[selected] if matches else None
                    buf = active_handler.on_select(item, fragment, buf)
                    if key == Key.TAB and buf and buf[-1] != " ":
                        buf.append(" ")
                    active_handler = None
                    fragment = ""
                    matches = []
                    current_hint = None
                    cursor = len(buf)
                    # Erase dropdown immediately on selection.
                    prev_dropdown_lines = _redraw(
                        prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None,
                        mode_line=_get_mode_line(),
                    )
                    # If no dropdown item was selected (user typed command + args
                    # without space to auto-select), submit the completed line now
                    # rather than requiring a second Enter.
                    if item is None and buf:
                        result = "".join(buf)
                        _erase_below()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        if result:
                            history.append(result)
                        return result

                elif key == Key.BACKSPACE:
                    if fragment:
                        fragment = fragment[:-1]
                        matches = active_handler.get_matches(fragment, _ctx)
                        selected = 0
                        current_hint = active_handler.get_hint(matches[selected] if matches else None)
                    else:
                        # Backspace past the trigger char — cancel, drop the trigger char.
                        buf = active_handler.on_cancel(fragment, buf)
                        buf.pop()  # remove the trigger char that on_cancel restored
                        active_handler = None
                        fragment = ""
                        matches = []
                        current_hint = None
                        cursor = len(buf)
                        # Erase dropdown immediately.
                        prev_dropdown_lines = _redraw(
                            prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None,
                            mode_line=_get_mode_line(),
                        )

                elif key == Key.UP:
                    if matches:
                        selected = (selected - 1) % len(matches)
                        current_hint = active_handler.get_hint(matches[selected])

                elif key == Key.DOWN:
                    if matches:
                        selected = (selected + 1) % len(matches)
                        current_hint = active_handler.get_hint(matches[selected])

                else:
                    # CHAR (including the trigger char itself typed again — append to fragment).
                    if key == Key.AT:
                        char_to_add = "@"
                    elif key == Key.CHAR and ch:
                        char_to_add = ch
                    else:
                        char_to_add = None

                    # Space after a command name: auto-select if there is an
                    # exact match or only one option left so the user can type
                    # arguments without needing a second Enter.
                    # e.g. "/gen " auto-selects /gen and exits trigger mode.
                    if char_to_add == " " and fragment and matches:
                        exact = next(
                            (m for m in matches if m.value == active_handler.char + fragment),
                            matches[0] if len(matches) == 1 else None,
                        )
                        if exact is not None:
                            buf = active_handler.on_select(exact, fragment, buf)
                            buf.append(" ")
                            active_handler = None
                            fragment = ""
                            matches = []
                            current_hint = None
                            cursor = len(buf)
                            prev_dropdown_lines = _redraw(
                                prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None,
                                mode_line=_get_mode_line(),
                            )
                            # Reset ctrl_c_count and continue with normal editing.
                            if key != Key.CTRL_C:
                                ctrl_c_count = 0
                            continue

                    if char_to_add is not None:
                        fragment += char_to_add
                        matches = active_handler.get_matches(fragment, _ctx)
                        selected = 0
                        current_hint = active_handler.get_hint(matches[selected] if matches else None)

                # Reset ctrl_c_count on any key except CTRL_C.
                if key != Key.CTRL_C:
                    ctrl_c_count = 0
                continue

            # ── NOT in trigger mode ──────────────────────────────────────────
            if key == Key.CTRL_C:
                ctrl_c_count += 1
                _n_rows = max(1, "".join(buf).count("\n") + 1)
                _erase_below(_n_rows)
                prev_dropdown_lines = 0
                if ctrl_c_count == 1:
                    # Warning is rendered by _get_mode_line() on next _redraw.
                    buf = []
                    cursor = 0
                else:
                    _show_exit_hint(resume_id)
                    return None
                continue

            # Reset ctrl_c_count on any key except CTRL_C.
            ctrl_c_count = 0

            if key == Key.CTRL_D:
                _n_rows = max(1, "".join(buf).count("\n") + 1)
                _scrub_cursor(buf, _n_rows)
                _erase_below(_n_rows)
                sys.stdout.write("\n" * _n_rows)
                sys.stdout.flush()
                return None if not buf else "".join(buf)

            elif key == Key.ENTER:
                result = "".join(buf)
                _n_rows = max(1, result.count("\n") + 1)
                _scrub_cursor(buf, _n_rows)
                _erase_below(_n_rows)
                sys.stdout.write("\n" * _n_rows)
                sys.stdout.flush()
                if result:
                    history.append(result)
                return result

            elif key == Key.CTRL_ENTER:
                # Insert a newline at the cursor for multi-line input.
                buf.insert(cursor, "\n")
                cursor += 1

            elif key == Key.LEFT:
                cursor = max(0, cursor - 1)

            elif key == Key.RIGHT:
                cursor = min(len(buf), cursor + 1)

            elif key == Key.HOME:
                # Move to start of the current line (not the whole buffer).
                text_before = "".join(buf[:cursor])
                last_nl = text_before.rfind("\n")
                cursor = last_nl + 1  # 0 when no '\n' found (rfind returns -1)

            elif key == Key.END:
                # Move to end of the current line (not the whole buffer).
                rest = "".join(buf[cursor:])
                next_nl = rest.find("\n")
                cursor = len(buf) if next_nl == -1 else cursor + next_nl

            elif key == Key.BACKSPACE:
                # Re-enter trigger mode only when the cursor is at the end of
                # the buffer — mid-buffer backspace is always a literal delete.
                _tail = _find_trigger_tail(buf, _registry) if cursor == len(buf) else None
                if _tail is not None:
                    # First backspace on a committed token: open picker at full
                    # fragment without consuming the character (subsequent presses
                    # in trigger mode peel away fragment chars normally).
                    _tch, _tpre, _tfrag = _tail
                    active_handler = _registry.get(_tch)
                    buf = _tpre
                    fragment = _tfrag
                    matches = active_handler.get_matches(fragment, _ctx)
                    selected = 0
                    current_hint = active_handler.get_hint(
                        matches[selected] if matches else None
                    )
                elif cursor > 0:
                    del buf[cursor - 1]
                    cursor -= 1

            elif key == Key.CTRL_U:
                buf.clear()
                cursor = 0

            elif key == Key.UP:
                # Within multiline: move to the same column on the previous line.
                # Fall back to history navigation when already on the first line.
                _text = "".join(buf)
                _before = _text[:cursor]
                _all_lines = _text.split("\n")
                _lines_before = _before.split("\n")
                _curr_line = len(_lines_before) - 1
                _curr_col  = len(_lines_before[-1])
                if _curr_line > 0:
                    _prev_len = len(_all_lines[_curr_line - 1])
                    _target_col = min(_curr_col, _prev_len)
                    cursor = (
                        sum(len(_all_lines[i]) + 1 for i in range(_curr_line - 1))
                        + _target_col
                    )
                else:
                    if hist_idx == len(history):
                        saved_buf = list(buf)
                    if hist_idx > 0:
                        hist_idx -= 1
                        buf = list(history[hist_idx])
                        cursor = len(buf)

            elif key == Key.DOWN:
                # Within multiline: move to the same column on the next line.
                # Fall back to history navigation when already on the last line.
                _text = "".join(buf)
                _before = _text[:cursor]
                _all_lines = _text.split("\n")
                _lines_before = _before.split("\n")
                _curr_line = len(_lines_before) - 1
                _curr_col  = len(_lines_before[-1])
                if _curr_line < len(_all_lines) - 1:
                    _next_len = len(_all_lines[_curr_line + 1])
                    _target_col = min(_curr_col, _next_len)
                    cursor = (
                        sum(len(_all_lines[i]) + 1 for i in range(_curr_line + 1))
                        + _target_col
                    )
                else:
                    if hist_idx < len(history) - 1:
                        hist_idx += 1
                        buf = list(history[hist_idx])
                        cursor = len(buf)
                    elif hist_idx == len(history) - 1:
                        hist_idx = len(history)
                        buf = list(saved_buf)
                        cursor = len(buf)

            elif key == Key.SHIFT_TAB:
                if mode_manager is not None:
                    new_mode = mode_manager.cycle()
                    _mode_notification[0] = new_mode

            elif (key == Key.AT and "@" in _registry.chars) or (
                key == Key.CHAR and ch and ch in _registry.chars
            ):
                trigger_ch = "@" if key == Key.AT else ch
                # Trigger-tail re-entry only applies when the cursor is at the
                # end of the buffer — mid-buffer typing is always a literal insert.
                _tail = _find_trigger_tail(buf, _registry) if cursor == len(buf) else None
                if _tail is not None:
                    _tch, _tpre, _tfrag = _tail
                    active_handler = _registry.get(_tch)
                    buf = _tpre
                    fragment = _tfrag + trigger_ch
                    matches = active_handler.get_matches(fragment, _ctx)
                    selected = 0
                    current_hint = active_handler.get_hint(
                        matches[selected] if matches else None
                    )
                else:
                    _handler = _registry.get(trigger_ch)
                    if _handler and _handler.can_activate(buf[:cursor]):
                        active_handler = _handler
                        fragment = ""
                        matches = active_handler.get_matches("", _ctx)
                        selected = 0
                        current_hint = active_handler.get_hint(
                            matches[selected] if matches else None
                        )
                    else:
                        buf.insert(cursor, trigger_ch)
                        cursor += 1

            elif key == Key.CHAR and ch:
                # Space terminates a token — never re-enter trigger mode for it.
                # Trigger-tail re-entry only when cursor is at end of buffer.
                _tail = (
                    None if ch.isspace() or cursor < len(buf)
                    else _find_trigger_tail(buf, _registry)
                )
                if _tail is not None:
                    _tch, _tpre, _tfrag = _tail
                    active_handler = _registry.get(_tch)
                    buf = _tpre
                    fragment = _tfrag + ch
                    matches = active_handler.get_matches(fragment, _ctx)
                    selected = 0
                    current_hint = active_handler.get_hint(
                        matches[selected] if matches else None
                    )
                else:
                    buf.insert(cursor, ch)
                    cursor += 1
