"""The control sequences the TUI writes to tell the terminal what it is doing (TUX-10).

Pure functions: they build strings and decide, from the environment, which
terminal understands what. Writing them is the backend's job
(:meth:`agenthicc.tui.terminal.backend.TerminalBackend.write_control`), which
never writes to a stream that is not a terminal.

- **Window title** — ``OSC 2`` sets it; the xterm title stack (``CSI 22;2 t`` and
  ``CSI 23;2 t``) saves and restores the user's own.
- **Progress** — ``OSC 9;4`` draws a progress bar in the taskbar or tab of
  Windows Terminal, ConEmu and Ghostty. Other terminals read ``OSC 9`` as a
  notification and would show ``4;3;0`` as one, so it is sent only where the
  terminal is known to understand it.
- **Notifications** — ``OSC 9`` (iTerm2, Ghostty, WezTerm, Windows Terminal), or
  ``OSC 777;notify`` for terminals that prefer it (foot); or a bell.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum

__all__ = [
    "BELL",
    "TITLE_POP",
    "TITLE_PUSH",
    "ProgressState",
    "hyperlinks_supported",
    "notification",
    "notify_mode",
    "progress",
    "progress_supported",
    "sanitize",
    "title",
]

BELL = "\x07"
#: Save and restore the terminal's window title (xterm's title stack).
TITLE_PUSH = "\x1b[22;2t"
TITLE_POP = "\x1b[23;2t"

#: Longest text written inside one sequence.
_LIMIT = 240

#: ``TERM`` values of terminals that take ``OSC 777;notify`` and not ``OSC 9``.
_OSC777_TERMS = frozenset({"foot", "foot-extra"})
#: ``TERM_PROGRAM`` values of terminals that draw ``OSC 9;4`` progress.
_PROGRESS_PROGRAMS = frozenset({"ghostty"})
#: ``TERM_PROGRAM`` values of terminals that open ``OSC 8`` hyperlinks.
_LINK_PROGRAMS = frozenset({"iTerm.app", "WezTerm", "vscode", "ghostty", "kitty", "Hyper", "rio"})
#: ``TERM`` values of terminals that open ``OSC 8`` hyperlinks.
_LINK_TERMS = frozenset({"xterm-kitty", "xterm-ghostty", "foot", "foot-extra", "alacritty"})
#: tmux passes ``OSC 8`` through from this version on.
_TMUX_LINKS = (3, 4)


class ProgressState(IntEnum):
    """The state field of ``OSC 9;4``."""

    CLEAR = 0
    DETERMINATE = 1
    ERROR = 2
    INDETERMINATE = 3


def sanitize(text: str, limit: int = _LIMIT) -> str:
    """*text* without control characters, so it can never close the sequence it is in.

    ``ESC``, ``BEL``, newlines and the C1 range are dropped; the result is cut
    to *limit* characters.
    """
    return "".join(
        character
        for character in text[:limit]
        if ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
    )


def _plain_terminal(env: Mapping[str, str]) -> bool:
    return env.get("TERM", "").casefold() in {"", "dumb"}


def title(text: str) -> str:
    """``OSC 2``: set the window title to *text*."""
    return f"\x1b]2;{sanitize(text)}{BELL}"


def progress(state: ProgressState, percent: int = 0) -> str:
    """``OSC 9;4``: the taskbar progress in *state* (*percent* only for determinate/error)."""
    value = min(100, max(0, percent))
    return f"\x1b]9;4;{int(state)};{value}{BELL}"


def progress_supported(env: Mapping[str, str]) -> bool:
    """Whether the terminal described by *env* draws ``OSC 9;4`` progress.

    Windows Terminal (``WT_SESSION``), ConEmu (``ConEmuPID``) and Ghostty. A
    multiplexer (``TMUX``, ``STY``) swallows the sequence, so it rules it out.
    """
    if _plain_terminal(env) or env.get("TMUX") or env.get("STY"):
        return False
    return (
        bool(env.get("WT_SESSION"))
        or bool(env.get("ConEmuPID"))
        or env.get("TERM_PROGRAM", "") in _PROGRESS_PROGRAMS
    )


def notify_mode(setting: str, env: Mapping[str, str]) -> str:
    """The notification a ``[ui] notify`` *setting* means here: ``osc9``, ``osc777``, ``bell`` or ``off``.

    ``auto`` picks ``OSC 777`` for terminals that prefer it and ``OSC 9``
    elsewhere; ``osc`` forces ``OSC 9``. A dumb or unnamed terminal gets
    nothing: it would print the sequence.
    """
    if setting == "off" or _plain_terminal(env):
        return "off"
    if setting == "bell":
        return "bell"
    if setting == "auto" and env.get("TERM", "") in _OSC777_TERMS:
        return "osc777"
    return "osc9"


def notification(mode: str, summary: str, heading: str = "agenthicc") -> str:
    """The sequence that raises *summary* for a notification *mode* (see :func:`notify_mode`).

    Returns ``""`` for ``off`` or an empty summary. ``OSC 777`` separates its
    fields with ``;``, which the text may not contain.
    """
    text = sanitize(summary)
    if mode == "bell":
        return BELL
    if not text or mode not in {"osc9", "osc777"}:
        return ""
    if mode == "osc777":
        return (
            f"\x1b]777;notify;{sanitize(heading).replace(';', ',')};{text.replace(';', ',')}{BELL}"
        )
    return f"\x1b]9;{text}{BELL}"


def _tmux_version(env: Mapping[str, str]) -> tuple[int, int] | None:
    """tmux's ``major.minor`` from ``TERM_PROGRAM_VERSION`` (``3.4``, ``3.3a``, ``next-3.5``)."""
    digits = "".join(
        character if character.isdigit() or character == "." else " "
        for character in env.get("TERM_PROGRAM_VERSION", "")
    ).split()
    for token in digits:
        major, _, minor = token.partition(".")
        if major.isdigit() and minor.isdigit():
            return int(major), int(minor)
    return None


def hyperlinks_supported(env: Mapping[str, str]) -> bool:
    """Whether the terminal described by *env* opens ``OSC 8`` hyperlinks.

    ``FORCE_HYPERLINK`` (any value but ``0``) says yes. Otherwise it is the
    terminals known to: iTerm2, kitty, WezTerm, Ghostty, foot, Alacritty,
    Windows Terminal, VS Code, and VTE terminals (GNOME Terminal). A dumb
    terminal and ``NO_COLOR`` with a plain ``TERM`` say no, and so does tmux
    before 3.4, which drops the sequence.
    """
    forced = env.get("FORCE_HYPERLINK")
    if forced is not None:
        return forced != "0"
    if _plain_terminal(env):
        return False
    if env.get("TMUX"):
        version = _tmux_version(env)
        return version is not None and version >= _TMUX_LINKS
    if env.get("STY"):
        return False
    vte = env.get("VTE_VERSION", "")
    return (
        bool(env.get("WT_SESSION"))
        or bool(env.get("KITTY_WINDOW_ID"))
        or (vte.isdigit() and int(vte) >= 5000)
        or env.get("TERM_PROGRAM", "") in _LINK_PROGRAMS
        or env.get("TERM", "") in _LINK_TERMS
    )
