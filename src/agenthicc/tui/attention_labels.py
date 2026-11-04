"""The words the attention badges use (TUIR-05).

The footer's ``◆`` and ``●`` badges count **background sessions** from the
agents hub (PRD-141), not the workers of a subagent pool shown beside them.
They say so: ``◆ 7 sessions need you``, ``● 1 session running``. The
terminal title uses the same words.

At narrow widths a badge drops words before it drops the unit:
``◆ 7 sessions need you`` → ``◆ 7 sess.`` → ``◆ 7``.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["BadgeForm", "sessions_need_you", "sessions_running"]

#: ``full`` names the unit and the state, ``short`` abbreviates the unit, and
#: ``bare`` is the number alone.
BadgeForm = Literal["full", "short", "bare"]


def _sessions(count: int) -> str:
    return f"{count} session" if count == 1 else f"{count} sessions"


def sessions_need_you(count: int, form: BadgeForm = "full") -> str:
    """``7 sessions need you``, ``1 session needs you``, ``7 sess.`` or ``7``."""
    if form == "bare":
        return str(count)
    if form == "short":
        return f"{count} sess."
    return f"{_sessions(count)} {'needs' if count == 1 else 'need'} you"


def sessions_running(count: int, form: BadgeForm = "full") -> str:
    """``2 sessions running``, ``1 session running``, ``2 sess.`` or ``2``."""
    if form == "bare":
        return str(count)
    if form == "short":
        return f"{count} sess."
    return f"{_sessions(count)} running"
