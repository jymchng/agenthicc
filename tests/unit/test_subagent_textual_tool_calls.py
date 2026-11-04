"""Detection of tool calls a model wrote as text instead of calling the tool."""

from __future__ import annotations

import pytest

from agenthicc.subagents.textual_tool_calls import (
    find_textual_tool_call,
    textual_tool_call_correction,
)

pytestmark = pytest.mark.unit

_TOOLS = frozenset({"write_file", "read_file", "patch_file"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # DeepSeek DSML, as returned for two documenters in a real session.
        ('\n<｜DSML｜ invoke name="write_file">\n<｜DSML｜ parameter name="path">', "write_file"),
        ('< | DSML | invoke name="patch_file">', "patch_file"),
        # DeepSeek special tokens.
        ("<｜tool▁call▁begin｜>function<｜tool▁sep｜>write_file\n```json\n{}", "write_file"),
        # XML function-call markup, plain and namespaced.
        ('<function_calls>\n<invoke name="write_file">', "write_file"),
        ('<fn:invoke name="read_file">', "read_file"),
        # Hermes / Qwen.
        ('<tool_call>\n{"name": "write_file", "arguments": {}}', "write_file"),
        # The earliest call wins when several are written.
        ('<tool_call>{"name": "read_file"}</tool_call> <invoke name="write_file">', "read_file"),
    ],
)
def test_leaked_call_markup_names_the_tool(text: str, expected: str) -> None:
    assert find_textual_tool_call(text, _TOOLS) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I wrote docs/guide.md with write_file and verified it.",
        "Call write_file(path, content) to save the result.",
        # Documentation about tool markup, inside a fenced block.
        'Example:\n```xml\n<invoke name="write_file">\n```\nDone.',
        # Markup for a tool this worker does not have.
        '<invoke name="delete_file">',
    ],
)
def test_prose_documentation_and_foreign_tools_are_not_calls(text: str) -> None:
    assert find_textual_tool_call(text, _TOOLS) is None


def test_no_tools_means_no_call() -> None:
    assert find_textual_tool_call('<invoke name="write_file">', frozenset()) is None


def test_the_correction_says_nothing_ran_and_names_the_tool() -> None:
    message = textual_tool_call_correction("write_file")
    assert "write_file did not run" in message
    assert "tool-calling interface" in message
