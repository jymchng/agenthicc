"""What the agent is doing, in the words the status line uses (TUX-10 §2.1).

The state segment says ``Reading src/app.py``, ``Running npm test`` or
``Waiting for you · approve a command``, never an internal tool name. The verb
comes from the tool's presentation (TUX-06): its operation word and family.

The module is pure: it never imports Rich and never touches the terminal.
"""

from __future__ import annotations

from collections.abc import Mapping

from agenthicc.tui.tool_summary import ToolFamily, ToolPresentation, tool_presentation

__all__ = ["activity_text", "retry_text", "tool_verb", "waiting_reason"]

#: A tool's operation word → the verb of what it is doing now.
_BY_OPERATION: Mapping[str, str] = {
    "Edit": "Editing",
    "Write": "Writing",
    "Append": "Editing",
    "Truncate": "Editing",
    "Create": "Creating",
    "Delete": "Deleting",
    "Move": "Moving",
    "Copy": "Copying",
    "Inspect": "Inspecting",
    "Checksum": "Checking",
    "Wait": "Waiting for",
    "Stop": "Stopping",
    "Publish": "Publishing",
    "Python": "Running Python",
    "Test": "Running tests",
    "Git": "Running git",
}

#: A tool family → the verb when the operation word has none of its own.
_BY_FAMILY: Mapping[ToolFamily, str] = {
    ToolFamily.COMMAND: "Running",
    ToolFamily.READ: "Reading",
    ToolFamily.WRITE: "Editing",
    ToolFamily.SEARCH: "Searching",
    ToolFamily.LIST: "Listing",
    ToolFamily.GIT: "Running git",
    ToolFamily.MEMORY: "Reading memory",
    ToolFamily.WEB: "Fetching",
}


def tool_verb(presentation: ToolPresentation) -> str:
    """``Reading``, ``Running``, ``Editing`` …: the verb for one tool call.

    A plugin or MCP tool, which has neither a family nor a known operation,
    reads ``Using <operation>`` so the line still says what is happening.
    """
    verb = _BY_OPERATION.get(presentation.operation) or _BY_FAMILY.get(presentation.family)
    if verb:
        return verb
    return f"Using {presentation.operation}" if presentation.operation else "Working"


def activity_text(verb: str, target: str) -> str:
    """``Reading src/app.py``: the verb and what it acts on."""
    return f"{verb} {target}" if target else verb


def waiting_reason(kind: str, tool_name: str, *, workspace_access: bool = False) -> str:
    """What the user is asked for: ``approve a command``, ``review the plan`` …"""
    if kind == "plan_review":
        return "review the plan"
    if kind == "questions":
        return "answer a question"
    if workspace_access:
        return "approve file access"
    family = tool_presentation(tool_name).family
    return {
        ToolFamily.COMMAND: "approve a command",
        ToolFamily.WRITE: "approve a change",
        ToolFamily.WEB: "approve a web request",
    }.get(family, "approve a tool call")


def retry_text(attempt: int, max_retries: int, seconds_left: int) -> str:
    """``Retrying (2/5) in 8s``: the provider call failed and is scheduled again."""
    return f"Retrying ({attempt}/{max_retries}) in {max(0, seconds_left)}s"
