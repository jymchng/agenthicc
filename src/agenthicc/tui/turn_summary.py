"""What one turn did: the tools it ran and the files it edited (TUX-10 §2.2).

The conversation store summarises a turn when it closes and writes the result
on the ``turn_complete`` event, so the closing row (``✾ Worked for 1m 12s``
and ``└ 6 tools · edited 3 files +34 −8``) comes from data in the log, not
from state a renderer accumulated. A transcript view that renders one event at
a time, and a replay, print the same row the live session did.
"""

from __future__ import annotations

from collections.abc import Iterable

from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.tui.tool_summary import ToolFamily, tool_presentation

__all__ = ["summarize_turn"]


def _succeeded(payload: dict[str, object]) -> bool:
    return payload.get("success", True) is not False and not payload.get("error_code")


def summarize_turn(events: Iterable[ConversationEvent]) -> dict[str, object]:
    """The fields a closing row needs, from a turn's own events.

    - ``tools``: tool calls the turn ran (an edit that showed its diff counts);
    - ``wrote``: a write tool or an edit succeeded, so something may have changed;
    - ``files``, ``added``, ``removed``: the files the turn edited and their lines;
    - ``failed``: the turn raised an error.
    """
    from agenthicc.tui.diff_renderer import diff_counts

    tools = added = removed = 0
    wrote = failed = False
    edited: set[str] = set()
    for event in events:
        payload = event.payload
        if event.kind == "tool_complete":
            tools += 1
            name = payload.get("name")
            family = tool_presentation(name).family if isinstance(name, str) else None
            wrote = wrote or (family is ToolFamily.WRITE and _succeeded(payload))
        elif event.kind == "file_modified":
            tools += 1
            wrote = True
            old, new = payload.get("old_lines"), payload.get("new_lines")
            if isinstance(old, list) and isinstance(new, list):
                path = payload.get("path")
                edited.add(path if isinstance(path, str) else "")
                more, fewer = diff_counts([str(line) for line in old], [str(line) for line in new])
                added += more
                removed += fewer
        elif event.kind == "error":
            failed = True
    summary: dict[str, object] = {"tools": tools, "wrote": wrote}
    if edited:
        summary.update(files=len(edited), added=added, removed=removed)
    if failed:
        summary["failed"] = True
    return summary
