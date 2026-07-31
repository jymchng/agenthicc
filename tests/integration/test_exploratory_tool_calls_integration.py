"""Integration coverage for exploratory metadata across registry and persistence."""

from __future__ import annotations

import pytest
from lauren_ai._tools import tool

from agenthicc.plugins.registry import build_registry
from agenthicc.tools.capabilities import (
    ToolCapability,
    get_tool_capabilities,
    is_exploratory_tool,
    tool_exploratory,
    tool_read,
)
from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.tui.runtime.session_log import SessionEventLog

pytestmark = pytest.mark.integration


@tool_exploratory
@tool_read
@tool()
async def plugin_inspect(path: str) -> dict[str, object]:
    """Inspect one plugin-owned path."""
    return {"path": path}


@tool_read
@tool()
async def plugin_plain_read(path: str) -> dict[str, object]:
    """Read one plugin-owned path without presentation grouping."""
    return {"path": path}


def test_registry_preserves_presentation_metadata_without_changing_capabilities() -> None:
    registry = build_registry(project_plugin_tools=[plugin_inspect, plugin_plain_read])
    inspect_tool = next(tool for tool in registry.tools if tool.__name__ == "plugin_inspect")
    plain_tool = next(tool for tool in registry.tools if tool.__name__ == "plugin_plain_read")

    assert is_exploratory_tool(inspect_tool) is True
    assert is_exploratory_tool(plain_tool) is False
    assert get_tool_capabilities(inspect_tool) == frozenset({ToolCapability.READ})
    assert get_tool_capabilities(plain_tool) == frozenset({ToolCapability.READ})


def test_builtin_classification_keeps_side_effecting_families_unmarked() -> None:
    registry = build_registry(project_plugin_tools=[])
    tools_by_name = {tool.__name__: tool for tool in registry.tools}

    for name in {
        "read_file",
        "list_directory",
        "search_files",
        "grep_files",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_grep",
        "list_agenthicc_docs",
        "inspect_agenthicc_source",
    }:
        assert is_exploratory_tool(tools_by_name[name]) is True

    for name in {
        "write_file",
        "delete_file",
        "run_command",
        "shell",
        "git_commit",
        "playwright_open",
    }:
        if name in tools_by_name:
            assert is_exploratory_tool(tools_by_name[name]) is False


def test_session_log_round_trip_preserves_raw_event_and_presentation_marker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("exploration")
    original = ConversationEvent(
        "call-1",
        "tool_complete",
        {
            "name": "plugin_inspect",
            "success": True,
            "presentation": {"exploratory": True, "target": "docs/index.md"},
        },
        1.0,
    )
    log.append(original)
    log.close()

    loaded = SessionEventLog.load("exploration", rendered=False)
    assert len(loaded) == 1
    assert loaded[0].event_id == "call-1"
    assert loaded[0].payload == original.payload
    assert loaded[0].rendered is False
