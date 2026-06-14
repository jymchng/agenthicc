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

__all__ = ["read_line_with_mention", "Key"]

_MAX_VISIBLE = 8
_PROMPT_STYLE = "\x1b[1;32m❯\x1b[0m "  # bold green ❯ + space

# ── Exit messages — centralised here so they can never be lost ────────────────

_MSG_WARN = "\n\x1b[2mPress Ctrl+C again to exit.\x1b[0m\n"


def _show_exit_hint(resume_id: str = "") -> None:
    """Print the resume hint and flush.  Called on every clean exit path."""
    if resume_id:
        hint = (
            f"  To resume: \x1b[1magenthicc --resume {resume_id}\x1b[0m\n"
            f"  Or in the same directory: \x1b[1magenthicc --continue\x1b[0m"
        )
    else:
        hint = "  To resume: \x1b[1magenthicc --continue\x1b[0m  (in the same directory)"
    sys.stdout.write(f"\n\x1b[2m{hint}\x1b[0m\n\n")
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
        if b3 == b"Z":
            return (Key.SHIFT_TAB, "")
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
    mode_line: str | None = None,
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

    # Step 2: redraw the input line.
    mention_suffix = (trigger_char + fragment) if in_trigger else ""
    out.write(prompt_str + "".join(buf) + mention_suffix)

    # Step 2b — permanent mode footer (truncated so it never wraps)
    n_base = 0
    if mode_line is not None:
        _safe_line = _truncate_to_cols(mode_line, max(8, cols - 4))
        out.write(f"\n\r  \x1b[2m{_safe_line}\x1b[0m")
        n_base = 1

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

        # Step 5a (dropdown path): cursor back up to input row and reposition.
        new_n_lines = n_base + len(lines)
        out.write("\n" + "\n".join(lines))
        out.write(f"\x1b[{new_n_lines}A")
        out.write("\r" + prompt_str + "".join(buf) + mention_suffix)
        out.flush()
        return new_n_lines

    if n_base:
        out.write(f"\x1b[{n_base}A")
        out.write("\r" + prompt_str + "".join(buf) + mention_suffix)
    out.flush()
    return n_base


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


def _erase_below() -> None:
    """Erase the footer/dropdown rows before a submit or exit write.

    Architectural note: uses ESC[0J ("erase from cursor to end of screen")
    rather than a counted loop.  The cursor is always on the input line when
    this is called (invariant maintained by _redraw).  We step down exactly
    one row (into the footer area), erase everything from there to the bottom
    of the screen unconditionally, then step back up.  No row count needed —
    immune to line-wrap and wide-character miscounting.
    """
    out = sys.stdout
    out.write("\n\r\x1b[0J")   # step down 1 row, CR, erase to bottom
    out.write("\x1b[1A")        # step back up to input line
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
        notif = _mode_notification[0]
        if notif is not None:
            _mode_notification[0] = None
            return f"❖ Switched to {notif.name} mode"
        if mode_manager is None:
            return "⏵⏵ Auto  (shift+tab to cycle)"
        m = mode_manager.active
        if m.name == "Auto":
            return "⏵⏵ Auto  (shift+tab to cycle)"
        return f"⏵⏵ {m.badge}\x1b[2m {m.name}  (shift+tab to cycle)"

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
                    _erase_below()
                    prev_dropdown_lines = 0
                    ctrl_c_count += 1
                    if ctrl_c_count == 1:
                        sys.stdout.write(_MSG_WARN)
                        sys.stdout.flush()
                        buf = []
                    else:
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
                _erase_below()
                prev_dropdown_lines = 0
                if ctrl_c_count == 1:
                    sys.stdout.write(_MSG_WARN)
                    sys.stdout.flush()
                    buf = []
                else:
                    _show_exit_hint(resume_id)
                    return None
                continue

            # Reset ctrl_c_count on any key except CTRL_C.
            ctrl_c_count = 0

            if key == Key.CTRL_D:
                _erase_below()
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None if not buf else "".join(buf)

            elif key == Key.ENTER:
                result = "".join(buf)
                _erase_below()
                sys.stdout.write("\n")
                sys.stdout.flush()
                if result:
                    history.append(result)
                return result

            elif key == Key.BACKSPACE:
                # Check for a trigger tail BEFORE popping so the first
                # backspace on a committed token (e.g. @docs/README.md)
                # re-enters the picker at the FULL fragment.  Subsequent
                # presses peel characters away inside trigger-mode's own
                # backspace handler.  If no tail is found, fall through to
                # a normal pop.
                _tail = _find_trigger_tail(buf, _registry)
                if _tail is not None:
                    _tch, _tpre, _tfrag = _tail
                    active_handler = _registry.get(_tch)
                    buf = _tpre
                    fragment = _tfrag
                    matches = active_handler.get_matches(fragment, _ctx)
                    selected = 0
                    current_hint = active_handler.get_hint(
                        matches[selected] if matches else None
                    )
                elif buf:
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

            elif key == Key.SHIFT_TAB:
                if mode_manager is not None:
                    new_mode = mode_manager.cycle()
                    _mode_notification[0] = new_mode

            elif (key == Key.AT and "@" in _registry.chars) or (
                key == Key.CHAR and ch and ch in _registry.chars
            ):
                trigger_ch = "@" if key == Key.AT else ch
                # If the buffer already ends with a trigger tail, extend that
                # token rather than starting a new trigger from scratch.
                _tail = _find_trigger_tail(buf, _registry)
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
                    if _handler and _handler.can_activate(buf):
                        active_handler = _handler
                        fragment = ""
                        matches = active_handler.get_matches("", _ctx)
                        selected = 0
                        current_hint = active_handler.get_hint(
                            matches[selected] if matches else None
                        )
                    else:
                        buf.append(trigger_ch)

            elif key == Key.CHAR and ch:
                # Space terminates a token — never re-enter trigger mode for it.
                # For all other chars, extend an existing trigger tail if present
                # rather than appending as a plain character.
                _tail = None if ch.isspace() else _find_trigger_tail(buf, _registry)
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
                    buf.append(ch)
