"""One status vocabulary for every agents surface (AGX-06 §5.1).

The hub, ``agenthicc agents list`` and the docs all describe a session's status
with these glyphs, labels and groups. The label alone is unambiguous, so the
meaning never depends on colour or the glyph (no-colour and screen-reader
modes, AGX-13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .model import SessionStatus

__all__ = ["REPORT_LINE_LIMIT", "Group", "StatusLook", "report_sentence", "status_look"]

#: The longest report line a worker records for its agent.
REPORT_LINE_LIMIT = 200

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_LIST_MARKER = re.compile(r"^(?:[>*+-]|\d+[.)])\s+")


class Group(str, Enum):
    """List groups, top to bottom (AGX-06 §5.3)."""

    PINNED = "Pinned"
    NEEDS_YOU = "Needs you"
    READY_FOR_REVIEW = "Ready for review"
    RUNNING = "Running"
    QUEUED = "Queued"
    RECENTLY_FINISHED = "Recently finished"
    HISTORY = "History"

    @property
    def empty_hint(self) -> str:
        """The one-line description shown under an empty group header."""
        return _EMPTY_HINTS[self]


_EMPTY_HINTS = {
    Group.PINNED: "Agents you pin with p stay at the top",
    Group.NEEDS_YOU: "Agents waiting for your answer appear here",
    Group.READY_FOR_REVIEW: "Finished agents with changes to review appear here",
    Group.RUNNING: "Agents at work appear here",
    Group.QUEUED: "Agents waiting for a worker slot appear here",
    Group.RECENTLY_FINISHED: "Agents that finished in the last 30 minutes appear here",
    Group.HISTORY: "Finished agents appear here",
}


@dataclass(frozen=True)
class StatusLook:
    glyph: str
    label: str
    group: Group
    #: Rich style for the glyph and label (meaning never depends on it).
    style: str


_LOOKS: dict[SessionStatus, StatusLook] = {
    SessionStatus.WAITING_APPROVAL: StatusLook(
        "◆", "needs approval", Group.NEEDS_YOU, "bold yellow"
    ),
    SessionStatus.WAITING_INPUT: StatusLook("◆", "needs input", Group.NEEDS_YOU, "bold yellow"),
    SessionStatus.ORPHANED: StatusLook("⚠", "worker lost", Group.NEEDS_YOU, "bold red"),
    SessionStatus.RUNNING: StatusLook("●", "running", Group.RUNNING, "green"),
    SessionStatus.STARTING: StatusLook("◐", "starting", Group.RUNNING, "green"),
    SessionStatus.RETRYING: StatusLook("↻", "retrying", Group.RUNNING, "yellow"),
    SessionStatus.CANCELLING: StatusLook("⊘", "stopping", Group.RUNNING, "yellow"),
    SessionStatus.QUEUED: StatusLook("◌", "queued", Group.QUEUED, "cyan"),
    SessionStatus.FAILED: StatusLook("✗", "failed", Group.RECENTLY_FINISHED, "red"),
    SessionStatus.COMPLETED: StatusLook("✓", "done", Group.RECENTLY_FINISHED, "cyan"),
    SessionStatus.CANCELLED: StatusLook("⊘", "cancelled", Group.RECENTLY_FINISHED, "dim"),
    SessionStatus.ARCHIVED: StatusLook("▫", "archived", Group.HISTORY, "dim"),
    SessionStatus.DELETED: StatusLook("·", "deleted", Group.HISTORY, "dim"),
}


def status_look(status: SessionStatus, *, queue_position: int | None = None) -> StatusLook:
    """Return the glyph, label, group and style for *status*.

    A queued session's label carries its queue position (``queued #2``).
    """
    look = _LOOKS[status]
    if status is SessionStatus.QUEUED and queue_position is not None:
        return StatusLook(look.glyph, f"queued #{queue_position}", look.group, look.style)
    return look


def report_sentence(text: str, *, limit: int = REPORT_LINE_LIMIT) -> str:
    """An agent message's report line: its first sentence of prose (AGX-06 §5.2).

    Headings, rules and fenced code are skipped (a heading is used only when
    the message has nothing else), list and quote markers are dropped, and the
    sentence is cut to *limit* characters on one line.
    """
    heading = ""
    fenced = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not line or set(line) <= set("-*_= "):
            continue
        if line.startswith("#"):
            heading = heading or line.lstrip("#").strip()
            continue
        line = _LIST_MARKER.sub("", line, count=1).strip()
        if line:
            return _fit(_SENTENCE_END.split(line, maxsplit=1)[0], limit)
    return _fit(heading, limit)


def _fit(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
