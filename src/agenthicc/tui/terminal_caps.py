"""Centralized terminal capability detection (PRD-105).

Use ``TerminalCapabilityDetector.detect()`` once at startup and pass the
resulting ``TerminalCapabilities`` to any code that needs to branch on
terminal support.  Never guard terminal calls with ``sys.platform`` checks.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

__all__ = ["TerminalCapabilities", "TerminalCapabilityDetector"]


@dataclass(frozen=True)
class TerminalCapabilities:
    """Runtime snapshot of what this terminal supports."""

    is_tty: bool
    """Both stdin and stdout are connected to a real TTY."""

    supports_raw_mode: bool
    """``termios`` is importable and ``tcgetattr`` succeeds on stdin."""

    supports_alt_screen: bool
    """Terminal supports alternate-screen switching (DECSET 1049)."""

    supports_colors: bool
    """Terminal renders ANSI colour sequences."""

    supports_mouse: bool
    """Terminal accepts mouse-tracking escape sequences."""

    supports_resize_events: bool
    """OS delivers SIGWINCH on window resize."""


class TerminalCapabilityDetector:
    """Probe the current environment and return a ``TerminalCapabilities``."""

    @classmethod
    def detect(cls) -> TerminalCapabilities:
        is_tty = _probe_tty()
        raw_mode = _probe_raw_mode() if is_tty else False
        return TerminalCapabilities(
            is_tty=is_tty,
            supports_raw_mode=raw_mode,
            supports_alt_screen=is_tty,
            supports_colors=_probe_colors(is_tty),
            supports_mouse=is_tty,
            supports_resize_events=_probe_resize_events(is_tty),
        )


# ── private probes ────────────────────────────────────────────────────────────

def _probe_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def _probe_raw_mode() -> bool:
    try:
        import termios  # noqa: PLC0415
        fd = sys.stdin.fileno()
        termios.tcgetattr(fd)
        return True
    except Exception:  # noqa: BLE001
        return False


def _probe_colors(is_tty: bool) -> bool:
    if os.environ.get("COLORTERM"):
        return True
    term = os.environ.get("TERM", "")
    if term and term != "dumb":
        return True
    return is_tty


def _probe_resize_events(is_tty: bool) -> bool:
    if not is_tty:
        return False
    try:
        import signal  # noqa: PLC0415
        return hasattr(signal, "SIGWINCH")
    except Exception:  # noqa: BLE001
        return False
