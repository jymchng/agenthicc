"""Detect a tool call that a model wrote as text instead of calling the tool.

Some providers return their native tool-call markup in the assistant text when
their gateway fails to parse it, for example DeepSeek's DSML
(``<｜DSML｜ invoke name="write_file">``) after a long ``content`` argument.
Nothing in that text is executed. A worker that ends on it would otherwise be
reported as successful while the file it meant to write never landed, so the
worker detects the markup, gives the model one corrective turn, and fails with
a named reason if the model writes the call as text again.

Only structural call markup that names one of the worker's own tools counts,
and fenced code blocks are ignored, so prose or documentation *about* tool
calls is not mistaken for a call.
"""

from __future__ import annotations

import re
from collections.abc import Collection

__all__ = ["find_textual_tool_call", "textual_tool_call_correction"]

_FENCED_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)

#: Call markup a provider can leak into the assistant text, each capturing the
#: tool name. ``｜`` is the full-width bar DeepSeek's special tokens use.
_CALL_MARKUP: tuple[re.Pattern[str], ...] = (
    # DeepSeek DSML: <｜DSML｜ invoke name="write_file">
    re.compile(r"<\s*[｜|]\s*DSML\s*[｜|]\s*invoke\s+name\s*=\s*\"([^\"]+)\""),
    # DeepSeek special tokens: <｜tool▁call▁begin｜>function<｜tool▁sep｜>write_file
    re.compile(
        r"<[｜|]tool▁call▁begin[｜|]>\s*(?:function)?\s*<[｜|]tool▁sep[｜|]>\s*([A-Za-z0-9_.-]+)"
    ),
    # XML function-call markup, optionally namespaced: <invoke name="write_file">
    re.compile(r"<(?:[A-Za-z_]+:)?invoke\s+name\s*=\s*\"([^\"]+)\""),
    # Hermes / Qwen: <tool_call>{"name": "write_file", ...}
    re.compile(r"<tool_call>\s*\{\s*\"name\"\s*:\s*\"([^\"]+)\""),
)


def find_textual_tool_call(text: str, tool_names: Collection[str]) -> str | None:
    """Return the first of *tool_names* that *text* calls through leaked markup.

    Returns ``None`` when *text* holds no call markup outside fenced code
    blocks, or when the markup names a tool the worker does not have.
    """
    if not text or not tool_names:
        return None
    unfenced = _FENCED_BLOCK.sub("", text)
    earliest: tuple[int, str] | None = None
    for pattern in _CALL_MARKUP:
        for match in pattern.finditer(unfenced):
            name = match.group(1).strip()
            if name in tool_names:
                if earliest is None or match.start() < earliest[0]:
                    earliest = (match.start(), name)
                break
    return earliest[1] if earliest is not None else None


def textual_tool_call_correction(tool: str) -> str:
    """The one corrective instruction sent after a call written as text."""
    return (
        f"Your previous response wrote a call to {tool} as text. Text is never executed: "
        f"{tool} did not run and nothing it would have done happened. Call {tool} now "
        "through the tool-calling interface (not as text or markup), then give your "
        "final answer."
    )
