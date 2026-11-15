"""An event's plain text, and the rule search uses to match it (TUX-09 §2.1).

One function, :func:`plain_text`, says what an item *says*: the user's words,
the assistant's text, a tool's summary and output, an edit's diff. Search
matches it, ``y`` copies it and ``e`` opens it in a pager, so the three can
never disagree.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping

from rich.style import Style
from rich.text import Text

from agenthicc.tui.theme import style

__all__ = ["find_all", "highlight", "matches", "plain_text"]

#: The most changed lines an edit's plain text carries.
_MAX_DIFF_LINES = 4_000


def _field(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _lines(value: object) -> list[str]:
    return [line for line in value if isinstance(line, str)] if isinstance(value, list) else []


def plain_text(kind: str, payload: Mapping[str, object]) -> str:
    """What the event says, as plain text: no markup, no colour, no hidden fields."""
    match kind:
        case "user_message" | "text" | "system" | "announcement":
            return _field(payload, "text")
        case "thinking_step":
            return _field(payload, "step")
        case "tool_complete":
            from agenthicc.tui.tool_summary import tool_presentation

            operation = tool_presentation(_field(payload, "name")).operation
            head = " ".join(
                part
                for part in (
                    operation,
                    _field(payload, "summary"),
                    _field(payload, "summary_detail"),
                )
                if part
            )
            return "\n".join([head, *_lines(payload.get("output_lines"))]).strip()
        case "file_modified":
            path = _field(payload, "path")
            old, new = _lines(payload.get("old_lines")), _lines(payload.get("new_lines"))
            diff = list(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm="", n=2))[
                :_MAX_DIFF_LINES
            ]
            return "\n".join(diff) if diff else path
        case "error":
            action = payload.get("action")
            hint = action.get("hint") if isinstance(action, Mapping) else None
            return "\n".join(
                part
                for part in (
                    _field(payload, "message"),
                    hint if isinstance(hint, str) else "",
                    _field(payload, "detail"),
                )
                if part
            )
        case "turn_start" | "turn_complete" | "activity_complete" | "replay_separator":
            return ""
        case _:
            for key in ("text", "message", "summary", "detail", "label"):
                value = _field(payload, key)
                if value:
                    return value
            return ""


def _folded(query: str) -> bool:
    """Case-insensitive unless the query has capitals."""
    return query == query.lower()


def matches(query: str, text: str) -> bool:
    """Whether *text* contains *query* (a substring; smart case)."""
    if not query:
        return False
    return query.lower() in text.lower() if _folded(query) else query in text


def find_all(query: str, text: str) -> list[tuple[int, int]]:
    """Every ``(start, end)`` of *query* in *text*, in order and not overlapping."""
    if not query:
        return []
    haystack = text.lower() if _folded(query) else text
    if len(haystack) != len(text):
        return []  # a character whose lower case is longer: offsets would drift
    needle = query.lower() if _folded(query) else query
    spans: list[tuple[int, int]] = []
    at = haystack.find(needle)
    while at >= 0:
        spans.append((at, at + len(needle)))
        at = haystack.find(needle, at + len(needle))
    return spans


def highlight(row: Text, query: str) -> Text:
    """*row* with every hit of *query* in ``accent``, bold and underlined."""
    if not query:
        return row
    hit: Style = style("accent", bold=True) + Style(underline=True)
    for start, end in find_all(query, row.plain):
        row.stylize(hit, start, end)
    return row
