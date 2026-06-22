"""Unit tests for tool namespace (PRD-125)."""
from __future__ import annotations

import logging
import pytest

from agenthicc.plugins.registry import ToolGroup, ToolRegistry, build_registry
from agenthicc.agent_tools import (
    BUILTIN_GROUPS, FS_GROUP, GIT_GROUP, EXEC_GROUP, OUTLOOK_GROUP,
    FS_AGENT_TOOLS, GIT_AGENT_TOOLS,
)
from agenthicc.subagents.pool import _expand_allowed

pytestmark = pytest.mark.unit


# ── ToolGroup dataclass ───────────────────────────────────────────────────────

class TestToolGroup:
    def test_fields(self) -> None:
        grp = ToolGroup("fs", "File System", "Read files.", [], priority=4)
        assert grp.name == "fs"
        assert grp.label == "File System"
        assert grp.description == "Read files."
        assert grp.priority == 4

    def test_default_priority_zero(self) -> None:
        grp = ToolGroup("x", "X", ".", [])
        assert grp.priority == 0


# ── BUILTIN_GROUPS ────────────────────────────────────────────────────────────

class TestBuiltinGroups:
    def test_four_builtin_groups(self) -> None:
        assert len(BUILTIN_GROUPS) == 4

    def test_group_names(self) -> None:
        names = {g.name for g in BUILTIN_GROUPS}
        assert names == {"fs", "git", "exec", "outlook"}

    def test_fs_group_has_24_tools(self) -> None:
        assert len(FS_GROUP.tools) == len(FS_AGENT_TOOLS)

    def test_git_group_has_11_tools(self) -> None:
        assert len(GIT_GROUP.tools) == len(GIT_AGENT_TOOLS)

    def test_priorities_are_distinct(self) -> None:
        priorities = [g.priority for g in BUILTIN_GROUPS]
        assert len(set(priorities)) == len(priorities)

    def test_fs_has_highest_priority(self) -> None:
        assert FS_GROUP.priority == max(g.priority for g in BUILTIN_GROUPS)


# ── ToolRegistry.register_group ──────────────────────────────────────────────

class TestRegisterGroup:
    def _async_tool(self, name: str) -> object:
        async def fn() -> dict[str, object]:
            return {}
        fn.__name__ = name
        fn.__doc__ = f"Tool {name}."
        return fn

    def test_register_group_adds_all_tools(self) -> None:
        reg = ToolRegistry()
        t1 = self._async_tool("tool_a")
        t2 = self._async_tool("tool_b")
        grp = ToolGroup("mygroup", "My Group", "desc", [t1, t2])
        reg.register_group(grp)
        assert "tool_a" in reg.names
        assert "tool_b" in reg.names

    def test_register_group_records_group_membership(self) -> None:
        reg = ToolRegistry()
        t = self._async_tool("my_tool")
        grp = ToolGroup("myns", "My NS", ".", [t])
        reg.register_group(grp)
        assert reg._tool_groups.get("my_tool") == "myns"

    def test_register_group_idempotent(self) -> None:
        reg = ToolRegistry()
        t = self._async_tool("dup_tool")
        grp = ToolGroup("g", "G", ".", [t])
        reg.register_group(grp)
        reg.register_group(grp)
        assert reg._groups.count(grp) == 1

    def test_cross_group_shadowing_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        reg = ToolRegistry()
        t1 = self._async_tool("shared_name")
        t2 = self._async_tool("shared_name")
        grp1 = ToolGroup("group1", "G1", ".", [t1])
        grp2 = ToolGroup("group2", "G2", ".", [t2])
        reg.register_group(grp1)
        with caplog.at_level(logging.WARNING, logger="agenthicc.plugins.registry"):
            reg.register_group(grp2)
        assert any("shadows" in r.message for r in caplog.records)

    def test_same_group_override_stays_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        reg = ToolRegistry()
        t1 = self._async_tool("same_ns_tool")
        t2 = self._async_tool("same_ns_tool")
        grp1 = ToolGroup("ns", "NS", ".", [t1])
        grp2 = ToolGroup("ns", "NS2", ".", [t2])
        reg.register_group(grp1)
        with caplog.at_level(logging.WARNING, logger="agenthicc.plugins.registry"):
            reg.register_group(grp2)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings


# ── ToolRegistry.glob_expand ─────────────────────────────────────────────────

class TestGlobExpand:
    def _make_registry(self) -> ToolRegistry:
        return build_registry()

    def test_fs_glob_returns_all_fs_tools(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("fs.*")
        assert len(result) == len(FS_AGENT_TOOLS)

    def test_git_glob_returns_all_git_tools(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("git.*")
        assert len(result) == len(GIT_AGENT_TOOLS)

    def test_literal_known_name_returns_singleton(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("git_status")
        assert result == frozenset({"git_status"})

    def test_literal_unknown_name_returns_empty(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("nonexistent_tool_xyz")
        assert result == frozenset()

    def test_glob_unknown_group_returns_empty(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("no_such_group.*")
        assert result == frozenset()

    def test_glob_result_is_frozenset(self) -> None:
        reg = self._make_registry()
        assert isinstance(reg.glob_expand("fs.*"), frozenset)

    def test_glob_subset_of_total_tools(self) -> None:
        reg = self._make_registry()
        fs = reg.glob_expand("fs.*")
        git = reg.glob_expand("git.*")
        all_names = set(reg.names)
        assert fs.issubset(all_names)
        assert git.issubset(all_names)
        assert fs.isdisjoint(git)

    def test_exec_glob(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("exec.*")
        assert "run_bash" in result
        assert "run_tests" in result

    def test_outlook_glob(self) -> None:
        reg = self._make_registry()
        result = reg.glob_expand("outlook.*")
        assert "send_email" in result
        assert "calendar_events" in result


# ── ToolRegistry.describe() ───────────────────────────────────────────────────

class TestDescribe:
    def test_describe_empty_registry(self) -> None:
        reg = ToolRegistry()
        assert reg.describe() == ""

    def test_describe_has_grouped_sections(self) -> None:
        reg = build_registry()
        desc = reg.describe()
        assert "### File System" in desc
        assert "### Git" in desc
        assert "### Shell / Exec" in desc
        assert "### Outlook / Calendar" in desc

    def test_describe_shows_tool_count_in_header(self) -> None:
        reg = build_registry()
        desc = reg.describe()
        assert "### File System (24 tools)" in desc
        assert "### Git (11 tools)" in desc

    def test_describe_shows_tool_name_and_docstring(self) -> None:
        reg = build_registry()
        desc = reg.describe()
        assert "**read_file**" in desc
        assert "**git_status**" in desc

    def test_describe_higher_priority_section_comes_first(self) -> None:
        reg = build_registry()
        desc = reg.describe()
        fs_pos = desc.index("### File System")
        git_pos = desc.index("### Git")
        exec_pos = desc.index("### Shell / Exec")
        assert fs_pos < git_pos < exec_pos

    def test_describe_ungrouped_tools_in_additional_section(self) -> None:
        reg = build_registry()

        async def my_plugin_tool() -> dict[str, object]:
            """My custom plugin."""
            return {}

        reg.register(my_plugin_tool, source="plugin")
        desc = reg.describe()
        assert "### Additional Tools" in desc
        assert "**my_plugin_tool**" in desc

    def test_describe_no_additional_section_when_all_grouped(self) -> None:
        reg = build_registry()
        assert "### Additional Tools" not in reg.describe()

    def test_describe_includes_group_description(self) -> None:
        reg = build_registry()
        desc = reg.describe()
        assert "_Read, write, search, and patch files" in desc

    def test_describe_is_valid_markdown(self) -> None:
        reg = build_registry()
        desc = reg.describe()
        lines = desc.splitlines()
        h3_lines = [l for l in lines if l.startswith("### ")]
        bullet_lines = [l for l in lines if l.startswith("- **")]
        assert len(h3_lines) >= 4
        assert len(bullet_lines) >= 50


# ── build_registry uses groups ────────────────────────────────────────────────

class TestBuildRegistry:
    def test_build_registry_populates_groups(self) -> None:
        reg = build_registry()
        assert len(reg._groups) == 4

    def test_build_registry_populates_tool_groups(self) -> None:
        reg = build_registry()
        assert reg._tool_groups.get("read_file") == "fs"
        assert reg._tool_groups.get("git_status") == "git"
        assert reg._tool_groups.get("run_bash") == "exec"

    def test_build_registry_total_50_tools(self) -> None:
        reg = build_registry()
        assert len(reg.tools) == 50


# ── _expand_allowed glob helper ───────────────────────────────────────────────

class TestExpandAllowed:
    def test_expand_glob_with_registry(self) -> None:
        reg = build_registry()
        result = _expand_allowed(frozenset({"fs.*"}), reg)
        assert "read_file" in result
        assert "write_file" in result
        assert "git_status" not in result

    def test_expand_literal_with_registry(self) -> None:
        reg = build_registry()
        result = _expand_allowed(frozenset({"git_status", "run_bash"}), reg)
        assert result == frozenset({"git_status", "run_bash"})

    def test_expand_mixed_glob_and_literal(self) -> None:
        reg = build_registry()
        result = _expand_allowed(frozenset({"fs.*", "git_status"}), reg)
        assert "read_file" in result
        assert "git_status" in result
        assert "git_diff" not in result

    def test_expand_without_registry_passes_through(self) -> None:
        result = _expand_allowed(frozenset({"fs.*", "git_status"}), None)
        assert result == frozenset({"fs.*", "git_status"})

    def test_expand_multiple_globs(self) -> None:
        reg = build_registry()
        result = _expand_allowed(frozenset({"fs.*", "git.*"}), reg)
        assert "read_file" in result
        assert "git_status" in result
        assert len(result) == len(FS_AGENT_TOOLS) + len(GIT_AGENT_TOOLS)

    def test_expand_unknown_glob_returns_empty_for_that_pattern(self) -> None:
        reg = build_registry()
        result = _expand_allowed(frozenset({"nonexistent.*"}), reg)
        assert result == frozenset()
