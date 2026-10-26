"""Capabilities the terminal reports about itself (AGX-11).

On its first raw-mode entry the POSIX backend asks the terminal, with DECRQM,
whether it supports synchronized output (private mode 2026). The answer
(DECRPM, ``CSI ? 2026 ; Ps $ y``) arrives as input; the key reader records it
here instead of returning it as a keystroke. A terminal that does not answer is
treated as not supporting the mode. Windows reads console input records, where
an answer would surface as typed characters, so it is never asked.
"""

from __future__ import annotations

import threading

__all__ = [
    "DECRQM_SYNCHRONIZED_OUTPUT",
    "SYNCHRONIZED_OUTPUT_MODE",
    "TerminalCapabilities",
    "capabilities",
]

SYNCHRONIZED_OUTPUT_MODE = 2026
#: DECRQM: request the state of private mode 2026.
DECRQM_SYNCHRONIZED_OUTPUT = "\x1b[?2026$p"

# DECRPM values: 0 not recognized, 1 set, 2 reset, 3 permanently set,
# 4 permanently reset. A mode that can be set, or always is, is supported.
_SUPPORTED_VALUES = frozenset({1, 2, 3})


class TerminalCapabilities:
    """Private-mode states reported by the terminal this process draws on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._modes: dict[int, int] = {}
        self._queried = False

    def claim_query(self) -> bool:
        """Return ``True`` once per process: the caller sends the queries."""
        with self._lock:
            if self._queried:
                return False
            self._queried = True
            return True

    def report_mode(self, mode: int, value: int) -> None:
        """Record a DECRPM answer for private *mode*."""
        with self._lock:
            self._modes[mode] = value

    @property
    def synchronized_output(self) -> bool:
        """Whether frames may be bracketed with ``CSI ? 2026 h`` … ``l``."""
        return self._modes.get(SYNCHRONIZED_OUTPUT_MODE) in _SUPPORTED_VALUES

    def reset(self) -> None:
        """Forget every report and allow a new query (tests)."""
        with self._lock:
            self._modes.clear()
            self._queried = False


#: The one terminal this process renders to.
capabilities = TerminalCapabilities()
