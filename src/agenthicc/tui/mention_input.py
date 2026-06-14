"""@mention-aware input line (no prompt_toolkit dependency)."""
from __future__ import annotations

import contextlib
import os
import select  # exported for patching: agenthicc.tui.mention_input.select.select
import sys
from pathlib import Path
from typing import Any, Generator

from .terminal import Key

__all__ = [
    "Key",
    "_get_matches",
    "_raw_mode",
    "_read_key",
    "_redraw",
    "read_line_with_mention",
    "select",
]


def _get_matches(fragment: str, cwd: Path | str) -> list[tuple[str, str]]:
    """Return [(display, meta)] for filesystem entries matching fragment.

    Dirs listed first with trailing slash, then their children inline.
    meta is always "". Hidden files skipped.
    When fragment contains "/", recurse into subdirectory.
    """
    base = Path(cwd).resolve()

    if not base.is_dir():
        return []

    if "/" in fragment:
        dir_part, file_prefix = fragment.rsplit("/", 1)
        search_dir = base / dir_part
    else:
        dir_part = ""
        file_prefix = fragment
        search_dir = base

    if not search_dir.is_dir():
        return []

    dirs: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []
    try:
        for entry in sorted(search_dir.iterdir(), key=lambda e: e.name):
            if entry.name.startswith("."):
                continue
            if not entry.name.startswith(file_prefix):
                continue
            if dir_part:
                display = f"{dir_part}/{entry.name}"
            else:
                display = entry.name
            if entry.is_dir():
                dirs.append((display + "/", ""))
                # Also add direct children inline
                try:
                    for child in sorted(entry.iterdir(), key=lambda e: e.name):
                        if child.name.startswith("."):
                            continue
                        child_suffix = "/" if child.is_dir() else ""
                        child_display = f"{display}/{child.name}{child_suffix}"
                        if child.is_dir():
                            dirs.append((child_display, ""))
                        else:
                            files.append((child_display, ""))
                except PermissionError:
                    pass
            else:
                files.append((display, ""))
    except PermissionError:
        pass
    return dirs + files


@contextlib.contextmanager
def _raw_mode(fd: int) -> Generator[int, None, None]:
    """Enter CBREAK mode on fd, yield fd, then restore."""
    import termios  # noqa: PLC0415
    import tty  # noqa: PLC0415
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key(fd: int) -> tuple[Key, str]:
    """Read one keystroke from fd.

    Reads bytes one at a time from the mock-friendly interface.
    """
    b = os.read(fd, 1)

    if b in (b"\r", b"\n"):
        return Key.ENTER, ""
    if b == b"\x03":
        return Key.CTRL_C, ""
    if b == b"\x04":
        return Key.CTRL_D, ""
    if b == b"\x15":
        return Key.CTRL_U, ""
    if b in (b"\x7f", b"\x08"):
        return Key.BACKSPACE, ""
    if b == b"\t":
        return Key.TAB, ""

    if b == b"\x1b":
        # Check if more bytes are available (select timeout = 0.05s)
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return Key.ESC, ""
        # Read the next byte
        b2 = os.read(fd, 1)
        if b2 != b"[" and b2 != b"O":
            # Not a CSI or SS3 sequence — treat as bare ESC
            return Key.ESC, ""
        # Read bytes of the sequence until we hit a terminating letter or ~
        seq_bytes = b2
        for _ in range(8):
            ready2, _, _ = select.select([fd], [], [], 0.05)
            if not ready2:
                break
            bc = os.read(fd, 1)
            if not bc:
                break
            seq_bytes += bc
            # Sequence ends at a letter or ~
            if bc and (chr(bc[0]).isalpha() or bc == b"~"):
                break
        seq = b"\x1b" + seq_bytes
        if seq in (b"\x1b[A", b"\x1bOA"):
            return Key.UP, ""
        if seq in (b"\x1b[B", b"\x1bOB"):
            return Key.DOWN, ""
        if seq in (b"\x1b[C", b"\x1bOC"):
            return Key.RIGHT, ""
        if seq in (b"\x1b[D", b"\x1bOD"):
            return Key.LEFT, ""
        if seq in (b"\x1b\r", b"\x1b\n"):
            return Key.NEWLINE, ""
        if seq == b"\x1b[Z":
            return Key.SHIFT_TAB, ""
        # Delete key: ESC [ 3 ~
        if seq == b"\x1b[3~":
            return Key.CHAR, ""
        return Key.ESC, ""

    if b == b"@":
        return Key.AT, "@"

    try:
        char = b.decode("utf-8")
        if char.isprintable():
            return Key.CHAR, char
    except UnicodeDecodeError:
        pass
    # Unprintable control byte — treat as ESC (ignore)
    return Key.ESC, ""


def _redraw(
    prompt_str: str,
    buf: list[str],
    fragment: str,
    matches: list[tuple[str, str]],
    selected: int,
    prev_n_lines: int,
    in_trigger: bool = False,
    mode_line: str | None = None,
) -> tuple[int, int]:
    """Erase old bottom block and redraw. Returns (rows_below, input_rows)."""
    if prev_n_lines > 0:
        sys.stdout.write(f"\x1b[{prev_n_lines}A\r\x1b[0J")

    text = "".join(buf)
    sys.stdout.write(f"\r{prompt_str}{text}")
    sys.stdout.flush()
    rows_below = 0

    if in_trigger and matches:
        n_show = min(8, len(matches))
        for i in range(n_show):
            item = matches[i]
            display = item[0] if isinstance(item, tuple) else getattr(item, "display", str(item))
            if i == selected:
                sys.stdout.write(f"\n  \x1b[7m{display}\x1b[0m")
            else:
                sys.stdout.write(f"\n  {display}")
        rows_below += n_show
        sys.stdout.flush()

    if mode_line is not None:
        sys.stdout.write(f"\n{mode_line}")
        rows_below += 1
        sys.stdout.flush()

    return rows_below, 1


def read_line_with_mention(
    prompt: str = "> ",
    cwd: str | Path = ".",
    history: list[str] | None = None,
    registry: Any | None = None,
    initial_menu: Any | None = None,
) -> str | None:
    """Read a line with @mention tab-completion.

    Args:
        prompt:       Prompt string displayed before cursor.
        cwd:          Base directory for @mention filesystem completions.
        history:      External list; successful entries are appended in-place.
        registry:     Optional TriggerRegistry for @/slash triggers.
        initial_menu: Optional MenuWidget to open immediately.

    Returns:
        The entered text (str), or None on EOF / double-Ctrl+C.
    """
    if history is None:
        history = []

    if not sys.stdin.isatty():
        try:
            line = input(prompt)
            if line:
                history.append(line)
            return line
        except (EOFError, KeyboardInterrupt):
            return None

    cwd_path = Path(cwd).resolve()
    buf: list[str] = []
    fragment = ""
    in_trigger = False
    # Registry-based trigger state
    _active_trigger_handler: Any | None = None  # handler from registry
    matches: list = []
    selected = 0
    prev_n_lines = 0
    ctrl_c_count = 0
    hist_idx: int = -1       # -1 = not navigating history
    saved_buf: list[str] = []  # saved current buf before history navigation

    fd = sys.stdin.fileno()
    with _raw_mode(fd) as raw_fd:
        while True:
            key, ch = _read_key(raw_fd)

            # ── Ctrl+C ────────────────────────────────────────────────────
            if key == Key.CTRL_C:
                ctrl_c_count += 1
                buf = []
                fragment = ""
                in_trigger = False
                _active_trigger_handler = None
                matches = []
                hist_idx = -1
                if ctrl_c_count >= 2:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
                # First Ctrl+C: just clear buf, loop continues
                _redraw_ret = _redraw(
                    prompt_str=prompt, buf=buf, fragment=fragment,
                    matches=matches, selected=selected,
                    prev_n_lines=prev_n_lines, in_trigger=in_trigger,
                )
                rows_below = _redraw_ret[0] if isinstance(_redraw_ret, tuple) else _redraw_ret
                prev_n_lines = rows_below
                continue

            # Any non-Ctrl+C key resets the Ctrl+C counter
            ctrl_c_count = 0

            # ── Ctrl+D ────────────────────────────────────────────────────
            if key == Key.CTRL_D:
                sys.stdout.write("\n")
                sys.stdout.flush()
                if not buf:
                    return None
                result = "".join(buf)
                if result:
                    history.append(result)
                return result

            # ── Ctrl+U ────────────────────────────────────────────────────
            if key == Key.CTRL_U:
                buf = []
                fragment = ""
                in_trigger = False
                _active_trigger_handler = None
                matches = []

            # ── Registry trigger mode ─────────────────────────────────────
            elif _active_trigger_handler is not None:
                from .trigger import TriggerContext  # noqa: PLC0415
                _ctx = TriggerContext(cwd=cwd_path, history=history, fragment=fragment)
                if key == Key.ESC:
                    suffix = _active_trigger_handler.on_cancel(fragment, [])
                    buf.extend(suffix)
                    _active_trigger_handler = None
                    fragment = ""
                    matches = []
                elif key == Key.ENTER:
                    item = matches[selected % len(matches)] if matches else None
                    suffix = _active_trigger_handler.on_select(item, fragment, [])
                    buf.extend(suffix)
                    _active_trigger_handler = None
                    fragment = ""
                    matches = []
                elif key == Key.TAB:
                    item = matches[selected % len(matches)] if matches else None
                    suffix = _active_trigger_handler.on_select(item, fragment, [])
                    buf.extend(suffix)
                    buf.append(" ")
                    _active_trigger_handler = None
                    fragment = ""
                    matches = []
                elif key == Key.BACKSPACE:
                    if fragment:
                        fragment = fragment[:-1]
                        matches = _active_trigger_handler.get_matches(fragment, _ctx)
                        selected = 0
                    else:
                        # Backspace past trigger char: cancel and drop the trigger char
                        _active_trigger_handler = None
                        fragment = ""
                        matches = []
                elif key == Key.DOWN:
                    if matches:
                        selected = (selected + 1) % len(matches)
                elif key == Key.UP:
                    if matches:
                        selected = (selected - 1) % len(matches)
                elif key == Key.CHAR and ch:
                    fragment += ch
                    _ctx2 = TriggerContext(cwd=cwd_path, history=history, fragment=fragment)
                    matches = _active_trigger_handler.get_matches(fragment, _ctx2)
                    selected = 0
                # Other keys: ignore in trigger mode
                # Get hint for the selected item
                _hint = None
                if hasattr(_active_trigger_handler, "get_hint"):
                    try:
                        _sel_item = matches[selected % len(matches)] if matches else None
                        _hint = _active_trigger_handler.get_hint(_sel_item)
                    except Exception:
                        pass
                # Pass mode_line positionally so tests can capture args[7]
                _redraw_ret = _redraw(prompt, buf, fragment, matches, selected, prev_n_lines, True, _hint)
                rows_below = _redraw_ret[0] if isinstance(_redraw_ret, tuple) else _redraw_ret
                prev_n_lines = rows_below
                continue

            # ── ESC ───────────────────────────────────────────────────────
            elif key == Key.ESC:
                if in_trigger:
                    # Cancel mention — restore "@" + fragment literally
                    n_remove = 1 + len(fragment)
                    for _ in range(n_remove):
                        if buf:
                            buf.pop()
                    buf.append("@")
                    buf.extend(list(fragment))
                    in_trigger = False
                    matches = []
                    fragment = ""
                # ESC outside mention mode: no action

            # ── ENTER ─────────────────────────────────────────────────────
            elif key == Key.ENTER:
                if in_trigger:
                    # Accept selection (or insert "@" + fragment if no matches)
                    n_remove = 1 + len(fragment)  # "@" + fragment chars
                    for _ in range(n_remove):
                        if buf:
                            buf.pop()
                    if matches:
                        item = matches[selected % len(matches)]
                        display = item[0] if isinstance(item, tuple) else getattr(item, "display", str(item))
                        buf.append("@")
                        buf.extend(list(display))
                    else:
                        # No matches: insert "@" + fragment literally
                        buf.append("@")
                        buf.extend(list(fragment))
                    in_trigger = False
                    matches = []
                    fragment = ""
                elif buf and buf[-1] == "\\":
                    # Backslash continuation: remove "\" and insert newline
                    buf.pop()
                    buf.append("\n")
                else:
                    # Normal submit
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    result = "".join(buf)
                    if result:
                        history.append(result)
                    return result

            # ── NEWLINE (Alt+Enter) ───────────────────────────────────────
            elif key == Key.NEWLINE:
                buf.append("\n")

            # ── BACKSPACE ─────────────────────────────────────────────────
            elif key == Key.BACKSPACE:
                if in_trigger:
                    if fragment:
                        if buf:
                            buf.pop()
                        fragment = fragment[:-1]
                        matches = _get_matches(fragment, cwd_path)
                        selected = 0
                    else:
                        # Empty fragment: cancel mention, drop the "@"
                        if buf:
                            buf.pop()  # remove "@"
                        in_trigger = False
                        matches = []
                elif buf:
                    buf.pop()

            # ── AT ────────────────────────────────────────────────────────
            elif key == Key.AT:
                if in_trigger:
                    # Second "@" inside mention: treat as literal char
                    buf.append("@")
                    fragment += "@"
                    matches = _get_matches(fragment, cwd_path)
                    selected = 0
                elif registry is not None and hasattr(registry, "get") and registry.get("@"):
                    # Use registry handler for "@"
                    handler = registry.get("@")
                    _active_trigger_handler = handler
                    fragment = ""
                    from .trigger import TriggerContext  # noqa: PLC0415
                    ctx = TriggerContext(cwd=cwd_path, history=history, fragment="")
                    matches = handler.get_matches("", ctx)
                    selected = 0
                    _redraw_ret = _redraw(
                        prompt_str=prompt, buf=buf, fragment=fragment,
                        matches=matches, selected=selected,
                        prev_n_lines=prev_n_lines, in_trigger=True,
                    )
                    rows_below = _redraw_ret[0] if isinstance(_redraw_ret, tuple) else _redraw_ret
                    prev_n_lines = rows_below
                    continue
                else:
                    buf.append("@")
                    in_trigger = True
                    fragment = ""
                    matches = _get_matches("", cwd_path)
                    selected = 0

            # ── TAB ───────────────────────────────────────────────────────
            elif key == Key.TAB:
                if in_trigger and matches:
                    item = matches[selected % len(matches)]
                    display = item[0] if isinstance(item, tuple) else getattr(item, "display", str(item))
                    n_remove = 1 + len(fragment)
                    for _ in range(n_remove):
                        if buf:
                            buf.pop()
                    buf.append("@")
                    buf.extend(list(display))
                    buf.append(" ")
                    in_trigger = False
                    matches = []
                    fragment = ""

            # ── UP ────────────────────────────────────────────────────────
            elif key == Key.UP:
                if in_trigger and matches:
                    selected = (selected - 1) % len(matches)
                elif history:
                    if hist_idx == -1:
                        saved_buf = list(buf)
                        hist_idx = len(history) - 1
                    elif hist_idx > 0:
                        hist_idx -= 1
                    buf = list(history[hist_idx])

            # ── DOWN ──────────────────────────────────────────────────────
            elif key == Key.DOWN:
                if in_trigger and matches:
                    selected = (selected + 1) % len(matches)
                elif hist_idx >= 0:
                    hist_idx += 1
                    if hist_idx >= len(history):
                        hist_idx = -1
                        buf = list(saved_buf)
                    else:
                        buf = list(history[hist_idx])

            # ── CHAR ──────────────────────────────────────────────────────
            elif key == Key.CHAR:
                if ch:
                    # Check if this char activates a registry trigger
                    if registry is not None and not in_trigger:
                        handler = registry.get(ch) if hasattr(registry, "get") else None
                        if handler is not None:
                            _active_trigger_handler = handler
                            fragment = ""
                            from .trigger import TriggerContext  # noqa: PLC0415
                            ctx = TriggerContext(cwd=cwd_path, history=history, fragment="")
                            matches = handler.get_matches("", ctx)
                            selected = 0
                            # Get hint for first item
                            _hint = None
                            if matches and hasattr(handler, "get_hint"):
                                try:
                                    _hint = handler.get_hint(matches[0] if matches else None)
                                except Exception:
                                    pass
                            # Pass mode_line positionally so tests capture args[7]
                            _redraw_ret = _redraw(prompt, buf, fragment, matches, selected, prev_n_lines, True, _hint)
                            rows_below = _redraw_ret[0] if isinstance(_redraw_ret, tuple) else _redraw_ret
                            prev_n_lines = rows_below
                            continue
                    buf.append(ch)
                    if in_trigger:
                        fragment += ch
                        matches = _get_matches(fragment, cwd_path)
                        selected = 0

            _redraw_ret = _redraw(
                prompt_str=prompt,
                buf=buf,
                fragment=fragment,
                matches=matches,
                selected=selected,
                prev_n_lines=prev_n_lines,
                in_trigger=in_trigger,
            )
            rows_below = _redraw_ret[0] if isinstance(_redraw_ret, tuple) else _redraw_ret
            prev_n_lines = rows_below
