"""Unit tests for ToolRegistry and build_registry (PRD-25)."""
from __future__ import annotations

import pytest
from agenthicc.plugins.registry import ToolRegistry, build_registry

pytestmark = pytest.mark.unit


def _make_tool(name: str):
    async def fn():
        pass

    fn.__name__ = name
    fn.__doc__ = f"Does {name} things."
    return fn


def test_register_dedup_last_wins():
    reg = ToolRegistry()
    t1 = _make_tool("ping")
    t2 = _make_tool("ping")
    reg.register(t1, source="builtin")
    reg.register(t2, source="plugin")
    assert reg.tools == [t2]


def test_register_many_preserves_order():
    reg = ToolRegistry()
    tools = [_make_tool(n) for n in ("a", "b", "c")]
    reg.register_many(tools, source="test")
    assert reg.names == ["a", "b", "c"]


def test_describe_produces_markdown():
    reg = ToolRegistry()
    reg.register(_make_tool("ping"), source="test")
    md = reg.describe()
    assert "**ping**" in md
    assert "Does ping things" in md


def test_describe_empty_registry():
    assert ToolRegistry().describe() == ""


def test_build_registry_includes_builtins():
    reg = build_registry(project_plugin_tools=[], agent_name=None)
    # Built-in tools must always be present
    assert "read_file" in reg.names
    assert "git_status" in reg.names


def test_build_registry_plugin_shadows_builtin():
    shadow = _make_tool("read_file")  # same name as built-in
    reg = build_registry(project_plugin_tools=[shadow], agent_name=None)
    # Plugin wins
    assert reg._by_name["read_file"] is shadow


def test_summary_log():
    reg = ToolRegistry()
    reg.register(_make_tool("foo"), source="test")
    summary = reg.summary_log()
    assert summary["total_tools"] == 1
    assert "foo" in summary["names"]
