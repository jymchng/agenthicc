"""mention_input — backward-compatibility shim (PRD-57 Phase 3).

The idle CBREAK input session lives in :mod:`agenthicc.tui.input`.
This module re-exports the original public API so existing callers
(tui.py, tests, widgets) require no changes.

Canonical locations
-------------------
Key, _raw_mode, _read_key   → agenthicc.tui.cbreak_reader
read_line_with_mention       → agenthicc.tui.input.session.run_input_session
_find_trigger_tail           → agenthicc.tui.input.session (private to session)
_get_matches, _redraw        → defined below (backward compat; tests patch them)
_get_mode_str, _prompt_ansi  → defined below (used by tui.py and input_area shim)
"""
from __future__ import annotations

import os
import select
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.modes import ModeManager

from agenthicc.tui.trigger import TriggerRegistry, MatchItem
from agenthicc.tui.menu import MenuWidget

# ── Re-exports: CBREAK primitives ────────────────────────────────────────────
from agenthicc.tui.cbreak_reader import Key, raw_mode as _raw_mode  # noqa: F401

# ── Re-exports: session ───────────────────────────────────────────────────────
from agenthicc.tui.input.session import InputSession, run_input_session  # noqa: F401

# ── Re-exports: rendering constants ──────────────────────────────────────────
from agenthicc.tui.input.renderer import PROMPT_CHAR, CURSOR_CHAR  # noqa: F401

__all__ = ["read_line_with_mention", "Key"]

_MAX_VISIBLE = 8
_NEW_LINE_HINT = "  │  ctrl+j = ↵"


# ── _read_key — defined locally so tests can patch mention_input.os.read ─────

def _read_key(fd: int) -> tuple[Key, str]:
    """Read one keystroke — defined here (not re-imported) so tests can patch
    ``agenthicc.tui.mention_input.os.read`` and have it take effect."""
    b = os.read(fd, 1)
    if b == b"\x03":  return (Key.CTRL_C, "")
    if b == b"\x04":  return (Key.CTRL_D, "")
    if b == b"\r":    return (Key.ENTER, "")
    if b == b"\n":    return (Key.CTRL_ENTER, "")
    if b == b"\t":    return (Key.TAB, "")
    if b in (b"\x7f", b"\x08"):  return (Key.BACKSPACE, "")
    if b == b"\x15":  return (Key.CTRL_U, "")
    if b == b"\x16":  return (Key.CTRL_V, "")
    if b == b"@":     return (Key.AT, "")
    if b == b"\x1b":
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return (Key.ESC, "")
        b2 = os.read(fd, 1)
        if b2 != b"[":
            return (Key.ESC, "")
        seq = b""
        while True:
            r_s, _, _ = select.select([fd], [], [], 0.05)
            if not r_s:
                break
            b_s = os.read(fd, 1)
            seq += b_s
            if b_s[-1:] in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz~":
                break
        if seq == b"A":    return (Key.UP, "")
        if seq == b"B":    return (Key.DOWN, "")
        if seq == b"C":    return (Key.RIGHT, "")
        if seq == b"D":    return (Key.LEFT, "")
        if seq == b"H":    return (Key.HOME, "")
        if seq == b"F":    return (Key.END, "")
        if seq == b"Z":    return (Key.SHIFT_TAB, "")
        if seq == b"1~":   return (Key.HOME, "")
        if seq == b"3~":   return (Key.CHAR, "")
        if seq == b"4~":   return (Key.END, "")
        if seq in (b"13;5u", b"13;1u", b"13u"):  return (Key.ENTER, "")
        if seq == b"200~":
            _TERM = b"\x1b[201~"
            paste_bytes = b""
            while True:
                r_p, _, _ = select.select([fd], [], [], 2.0)
                if not r_p:
                    break
                paste_bytes += os.read(fd, 4096)
                if _TERM in paste_bytes:
                    paste_bytes = paste_bytes[:paste_bytes.index(_TERM)]
                    break
            try:
                pasted = paste_bytes.decode("utf-8")
            except UnicodeDecodeError:
                pasted = paste_bytes.decode("utf-8", errors="replace")
            return (Key.PASTE, pasted)
        return (Key.ESC, "")
    raw = b
    first = b[0]
    if   first & 0b11100000 == 0b11000000:  n_extra = 1
    elif first & 0b11110000 == 0b11100000:  n_extra = 2
    elif first & 0b11111000 == 0b11110000:  n_extra = 3
    else:                                    n_extra = 0
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


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _get_mode_str(mode_manager: object | None) -> str:
    """Plain-text mode label for the footer (no ANSI/markup)."""
    if mode_manager is None:
        return f"⏵⏵ Auto  (shift+tab to cycle){_NEW_LINE_HINT}"
    m = getattr(mode_manager, "active", None)
    if m is None or getattr(m, "name", "") == "Auto":
        return f"⏵⏵ Auto  (shift+tab to cycle){_NEW_LINE_HINT}"
    return f"⏵⏵ {m.badge} {m.name}  (shift+tab to cycle){_NEW_LINE_HINT}"


def _prompt_ansi(buf: list[str], mention_suffix: str, cursor: int | None, in_trigger: bool) -> str:
    """ANSI prompt — delegates to renderer.build_prompt."""
    from agenthicc.tui.input.renderer import build_prompt  # noqa: PLC0415
    return build_prompt(buf, len(buf) if cursor is None else cursor, mention_suffix, in_trigger)


def _footer_ansi(mode_str: str, cols: int) -> tuple[str, str]:
    from agenthicc.tui.input.renderer import build_footer  # noqa: PLC0415
    return build_footer(mode_str, cols)


def _show_exit_hint(resume_id: str = "") -> None:
    from agenthicc.tui.input.renderer import PromptRenderer  # noqa: PLC0415
    PromptRenderer().show_exit_hint(resume_id)


# ── _get_matches — always returns empty meta ("") ─────────────────────────────

def _get_matches(fragment: str, cwd: Path) -> list[tuple[str, str]]:
    """File-system completions for *fragment* — always empty meta."""
    def _iter_dir(path: Path) -> list:
        try:
            return sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return []
    if "/" in fragment:
        dir_part, file_prefix = fragment.rsplit("/", 1)
        search_dir = cwd / dir_part
        if not search_dir.is_dir():
            return []
        results: list[tuple[str, str]] = []
        for entry in _iter_dir(search_dir):
            if entry.name.startswith(".") or not entry.name.startswith(file_prefix):
                continue
            suffix = "/" if entry.is_dir() else ""
            results.append((f"{dir_part}/{entry.name}{suffix}", ""))
        return results
    search_dir = cwd
    if not search_dir.is_dir():
        return []
    results = []
    for entry in _iter_dir(search_dir):
        if entry.name.startswith(".") or not entry.name.startswith(fragment):
            continue
        suffix = "/" if entry.is_dir() else ""
        results.append((f"{entry.name}{suffix}", ""))
        if entry.is_dir():
            for child in _iter_dir(entry):
                if child.name.startswith("."):
                    continue
                child_suffix = "/" if child.is_dir() else ""
                results.append((f"{entry.name}/{child.name}{child_suffix}", ""))
    return results


# ── _find_trigger_tail ────────────────────────────────────────────────────────

def _find_trigger_tail(
    buf: list[str], registry: TriggerRegistry
) -> "tuple[str, list[str], str] | None":
    for i in range(len(buf) - 1, -1, -1):
        ch = buf[i]
        if ch.isspace():
            return None
        if ch in registry.chars:
            pre = buf[:i]
            fragment = "".join(buf[i + 1:])
            handler = registry.get(ch)
            if handler and handler.can_activate(pre):
                return (ch, pre, fragment)
    return None


# ── _redraw — backward-compat wrapper ────────────────────────────────────────

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
    """Backward-compat wrapper over PromptRenderer.render (tests patch this)."""
    from agenthicc.tui.input.renderer import DropdownState, PromptRenderer  # noqa: PLC0415
    dd = DropdownState(
        active=in_trigger, matches=matches, selected=selected,
        hint=hint, trigger_char=trigger_char, fragment=fragment,
    )
    return PromptRenderer().render(buf, len(buf) if cursor is None else cursor, dd, mode_line)


# ── Main entry point ──────────────────────────────────────────────────────────

def read_line_with_mention(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: TriggerRegistry | None = None,
    initial_menu: "MenuWidget | None" = None,
    resume_id: str = "",
    mode_manager: "ModeManager | None" = None,
    initial_buf: list[str] | None = None,
) -> str | None:
    """Read one line with @-mention / slash-command trigger support."""
    import agenthicc.tui.mention_input as _mi  # noqa: PLC0415

    return InputSession(
        cwd=cwd,
        history=history,
        registry=registry,
        initial_menu=initial_menu,
        resume_id=resume_id,
        mode_manager=mode_manager,
        initial_buf=initial_buf,
        prompt_str=prompt_str,
        _fn_raw_mode=_mi._raw_mode,
        _fn_read_key=_mi._read_key,
        _fn_redraw=_mi._redraw,
    ).run()
