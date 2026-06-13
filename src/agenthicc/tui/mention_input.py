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
from typing import Generator

from agenthicc.tui.trigger import TriggerRegistry, TriggerHandler, TriggerContext, MatchItem

__all__ = ["read_line_with_mention", "Key"]

_MAX_VISIBLE = 8
_PROMPT_STYLE = "\x1b[1;32m❯\x1b[0m "  # bold green ❯ + space


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
        # Clear ISIG so Ctrl+C delivers \x03 to stdin instead of raising SIGINT.
        # Without this, Python would throw KeyboardInterrupt in the thread even
        # though our state machine already handles b"\x03" gracefully.
        cur[3] &= ~(termios.ECHOCTL | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, cur)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Layer 2: Key enum + _read_key ────────────────────────────────────────────

class Key(str, Enum):
    UP        = "UP"
    DOWN      = "DOWN"
    LEFT      = "LEFT"
    RIGHT     = "RIGHT"
    ENTER     = "ENTER"
    TAB       = "TAB"
    ESC       = "ESC"
    BACKSPACE = "BACKSPACE"
    CTRL_C    = "CTRL_C"
    CTRL_D    = "CTRL_D"
    CTRL_U    = "CTRL_U"
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
    if b in (b"\r", b"\n"):
        return (Key.ENTER, "")
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
            # Malformed or alt-key — treat as ESC
            return (Key.ESC, "")
        b3 = os.read(fd, 1)
        if b3 == b"A":
            return (Key.UP, "")
        if b3 == b"B":
            return (Key.DOWN, "")
        if b3 == b"C":
            return (Key.RIGHT, "")
        if b3 == b"D":
            return (Key.LEFT, "")
        if b3 == b"3":
            # Delete key: ESC [ 3 ~ — consume the trailing ~
            r2, _, _ = select.select([fd], [], [], 0.05)
            if r2:
                os.read(fd, 1)
            return (Key.CHAR, "")  # ignore Delete
        # Unknown sequence
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
) -> int:
    """Erase old dropdown, redraw the input line, render new dropdown.

    Returns the number of dropdown lines now visible (caller stores this as
    *prev_n_lines* for the next iteration).
    """
    import shutil

    out = sys.stdout

    # Step 1: erase old dropdown rows (move down, clear each, move back up).
    if prev_n_lines > 0:
        for _ in range(prev_n_lines):
            out.write("\n\r\x1b[2K")
        out.write(f"\x1b[{prev_n_lines}A")

    # Step 2: redraw the input line.
    mention_suffix = (trigger_char + fragment) if in_trigger else ""
    out.write("\r\x1b[2K" + prompt_str + "".join(buf) + mention_suffix)

    # Step 3: render dropdown if in trigger mode with matches.
    if in_trigger and matches:
        n = min(_MAX_VISIBLE, len(matches))
        # Scroll offset: keep the selected row inside the visible window.
        scroll = max(0, min(selected - n + 1, len(matches) - n))
        visible = matches[scroll : scroll + n]
        lines: list[str] = []
        for i, item in enumerate(visible):
            actual = scroll + i          # global index in matches
            indicator = "▶" if actual == selected else " "
            name = f"+ {item.display}"
            if actual == selected:
                line = f"\r\x1b[2K  \x1b[7m{indicator} {name}\x1b[0m"
            else:
                line = f"\r\x1b[2K  {indicator} {name}"
            lines.append(line)

        if hint is not None:
            cols = shutil.get_terminal_size((80, 24)).columns
            sep = "─" * min(cols - 4, 60)
            lines.append(f"\r\x1b[2K  \x1b[2m{sep}\x1b[0m")
            lines.append(f"\r\x1b[2K  \x1b[2m{hint[:cols - 4]}\x1b[0m")

        below = len(matches) - (scroll + n)
        if below > 0:
            lines.append(f"\r\x1b[2K  \x1b[2m… {below} more ↓\x1b[0m")
        elif scroll > 0:
            lines.append(f"\r\x1b[2K  \x1b[2m↑ {scroll} more above\x1b[0m")

        new_n_lines = len(lines)
        out.write("\n" + "\n".join(lines))
        out.write(f"\x1b[{new_n_lines}A")  # cursor back up to input row
        out.flush()
        return new_n_lines

    out.flush()
    return 0


# ── Layer 5: Main state machine ───────────────────────────────────────────────

def read_line_with_mention(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: TriggerRegistry | None = None,
) -> str | None:
    """Read one line of input with trigger-dropdown support.

    If stdin is not a TTY, falls back to plain ``input()``.

    Args:
        prompt_str: The prompt string to display (may contain ANSI codes).
        cwd: Working directory used to resolve @-mention file paths.
        history: Mutable list; successfully entered lines are appended in-place.
        registry: Optional :class:`TriggerRegistry`; defaults to one containing
            :class:`~agenthicc.tui.triggers.at_mention.AtMentionTrigger`.

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

    fd = sys.stdin.fileno()

    # State variables
    buf: list[str] = []
    active_handler: TriggerHandler | None = None
    fragment: str = ""
    matches: list[MatchItem] = []
    selected: int = 0
    current_hint: str | None = None
    prev_dropdown_lines: int = 0
    hist_idx: int = len(history)
    saved_buf: list[str] = []
    ctrl_c_count: int = 0

    with _raw_mode(fd):
        while True:
            # Redraw first, then read.
            prev_dropdown_lines = _redraw(
                prompt_str, buf, fragment, matches, selected,
                prev_dropdown_lines, active_handler is not None, current_hint,
                active_handler.char if active_handler else "@",
            )

            key, ch = _read_key(fd)

            # ── IN trigger mode ──────────────────────────────────────────────
            if active_handler is not None:
                if key == Key.CTRL_C:
                    # Cancel trigger; let normal CTRL_C logic handle next iter.
                    buf = active_handler.on_cancel(fragment, buf)
                    active_handler = None
                    fragment = ""
                    matches = []
                    current_hint = None
                    prev_dropdown_lines = 0
                    ctrl_c_count += 1
                    # Show warning on first press, exit on second.
                    if ctrl_c_count == 1:
                        sys.stdout.write("\n\x1b[2mPress Ctrl+C again to exit.\x1b[0m\n")
                        sys.stdout.flush()
                        buf = []
                    else:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return None
                    continue

                elif key == Key.ESC:
                    # Cancel trigger — restore trigger+fragment into buf.
                    buf = active_handler.on_cancel(fragment, buf)
                    active_handler = None
                    fragment = ""
                    matches = []
                    current_hint = None
                    # Erase dropdown immediately.
                    prev_dropdown_lines = _redraw(
                        prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None
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
                    # Erase dropdown immediately on selection.
                    prev_dropdown_lines = _redraw(
                        prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None
                    )

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
                        # Erase dropdown immediately.
                        prev_dropdown_lines = _redraw(
                            prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None
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
                if ctrl_c_count == 1:
                    sys.stdout.write("\n\x1b[2mPress Ctrl+C again to exit.\x1b[0m\n")
                    sys.stdout.flush()
                    buf = []
                else:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
                continue

            # Reset ctrl_c_count on any key except CTRL_C.
            ctrl_c_count = 0

            if key == Key.CTRL_D:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None if not buf else "".join(buf)

            elif key == Key.ENTER:
                result = "".join(buf)
                sys.stdout.write("\n")
                sys.stdout.flush()
                if result:
                    history.append(result)
                return result

            elif key == Key.BACKSPACE:
                if buf:
                    buf.pop()

            elif key == Key.CTRL_U:
                buf.clear()

            elif key == Key.UP:
                # History navigation: go back.
                if hist_idx == len(history):
                    saved_buf = list(buf)
                if hist_idx > 0:
                    hist_idx -= 1
                    buf = list(history[hist_idx])

            elif key == Key.DOWN:
                # History navigation: go forward.
                if hist_idx < len(history) - 1:
                    hist_idx += 1
                    buf = list(history[hist_idx])
                elif hist_idx == len(history) - 1:
                    hist_idx = len(history)
                    buf = list(saved_buf)

            elif (key == Key.AT and "@" in _registry.chars) or (
                key == Key.CHAR and ch and ch in _registry.chars
            ):
                trigger_ch = "@" if key == Key.AT else ch
                active_handler = _registry.get(trigger_ch)
                if active_handler:
                    fragment = ""
                    matches = active_handler.get_matches("", _ctx)
                    selected = 0
                    current_hint = active_handler.get_hint(matches[selected] if matches else None)
                else:
                    buf.append(trigger_ch)

            elif key == Key.CHAR and ch:
                buf.append(ch)
