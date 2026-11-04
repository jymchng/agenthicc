"""TOOLR-08: a smaller, accurate built-in tool surface.

The capability table and the catalog ceiling are deliberate: a change to
either must update this file in the same review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from lauren_ai._tools import TOOL_META

from agenthicc.agent_tools import AGENT_TOOLS, OUTLOOK_AGENT_TOOLS
from agenthicc.plugins.registry import build_registry
from agenthicc.tools.capabilities import get_tool_capabilities
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

#: Reviewed capability sets for every built-in tool.
CAPABILITIES: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ["run_bash", "run_command", "run_python", "run_python_expr", "run_tests", "stop_terminal"],
        frozenset({"execute"}),
    ),
    **dict.fromkeys(
        ["git_blame", "git_branch", "git_diff", "git_grep", "git_log", "git_show", "git_status"],
        frozenset({"git_read"}),
    ),
    **dict.fromkeys(
        ["git_add", "git_checkout", "git_commit", "git_stash"], frozenset({"git_write"})
    ),
    **dict.fromkeys(
        ["calendar_events", "list_emails", "list_folders", "read_email"],
        frozenset({"network", "read"}),
    ),
    "search_emails": frozenset({"network", "search"}),
    **dict.fromkeys(
        ["create_event", "move_email", "reply_email", "send_email"],
        frozenset({"network", "write"}),
    ),
    **dict.fromkeys(
        [
            "batch_read",
            "checksum_file",
            "get_file_info",
            "inspect_agenthicc_source",
            "inspect_terminal",
            "list_agenthicc_docs",
            "mcp_connection_guide",
            "read_agenthicc_doc",
            "read_file",
            "wait_terminal",
            "wait_terminal_ready",
        ],
        frozenset({"read"}),
    ),
    **dict.fromkeys(
        [
            "grep_files",
            "list_directory",
            "search_agenthicc_docs",
            "search_agenthicc_source",
            "search_files",
        ],
        frozenset({"read", "search"}),
    ),
    **dict.fromkeys(
        [
            "append_file",
            "apply_diff",
            "batch_copy",
            "batch_delete",
            "batch_move",
            "batch_write",
            "copy_file",
            "delete_file",
            "make_directory",
            "move_file",
            "patch_file",
            "touch_file",
            "truncate_file",
            "write_file",
        ],
        frozenset({"write"}),
    ),
}

#: The schema characters the built-in catalog sends on every request, without
#: Outlook (left out where it cannot work). TOOLR-00 measured 26,772; TOOLR-08
#: requires at least 15 % less. Raise only by an explicit decision.
CATALOG_CEILING = 22_756


def _name(tool: object) -> str:
    return str(getattr(tool, TOOL_META).name)


def test_every_built_in_tool_has_its_reviewed_capabilities() -> None:
    actual = {
        _name(tool): frozenset(cap.value for cap in get_tool_capabilities(tool))
        for tool in AGENT_TOOLS
    }

    assert actual == CAPABILITIES


def test_the_built_in_catalog_stays_under_its_ceiling() -> None:
    outlook = {_name(tool) for tool in OUTLOOK_AGENT_TOOLS}
    size = sum(
        len(json.dumps(getattr(tool, TOOL_META).parameters))
        for tool in AGENT_TOOLS
        if _name(tool) not in outlook
    )

    assert size <= CATALOG_CEILING, f"catalog is {size} characters"


def test_removed_duplicates_appear_nowhere() -> None:
    names = re.compile(r"\b(grep_file|file_exists|read_lines)\b")
    offenders: list[str] = []
    paths = [
        *(_ROOT / "src").rglob("*.py"),
        *(_ROOT / "docs").rglob("*.md"),
        *(_ROOT / "skills").rglob("*.md"),
        _ROOT / "llms-full.txt",
        _ROOT / "llms.txt",
        _ROOT / "README.md",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        cleaned = text.replace(".read_lines(", "").replace("def read_lines(", "")
        if names.search(cleaned):
            offenders.append(str(path.relative_to(_ROOT)))

    assert sorted(offenders) == []


def test_run_bash_is_the_one_shell_tool() -> None:
    from agenthicc.subagents.types import DEFAULT_REGISTRY
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy
    from agenthicc.tui.workspace.appender import _TOOL_DISPLAY_OP, _TOOL_OP

    assert "shell" not in {_name(tool) for tool in AGENT_TOOLS}
    assert "shell" not in WorkspaceAccessPolicy._PATH_FIELDS
    assert "shell" not in _TOOL_OP and "shell" not in _TOOL_DISPLAY_OP
    for name in DEFAULT_REGISTRY.names():
        spec = DEFAULT_REGISTRY.get(name)
        assert spec is not None and "shell" not in spec.allowed_tools


def test_outlook_is_left_out_and_named_when_it_cannot_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MSGRAPH_TOKEN", raising=False)
    monkeypatch.setattr("sys.platform", "linux")

    registry = build_registry()

    assert not {_name(tool) for tool in OUTLOOK_AGENT_TOOLS} & set(registry.names)
    assert "Outlook / Calendar: unavailable (set MSGRAPH_TOKEN)" in registry.describe()


def test_outlook_is_offered_with_a_graph_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSGRAPH_TOKEN", "token")

    registry = build_registry()

    assert {_name(tool) for tool in OUTLOOK_AGENT_TOOLS} <= set(registry.names)
    assert "unavailable" not in registry.describe()


@pytest.mark.parametrize("tool", ["inspect_terminal", "wait_terminal", "wait_terminal_ready"])
def test_plan_and_safe_mode_let_an_agent_observe_its_own_terminal(tool: str) -> None:
    capabilities = CAPABILITIES[tool]
    modes = build_default_registry()
    for mode in ("Plan", "Safe"):
        runtime = modes.get(mode)
        assert not capabilities & {cap.value for cap in runtime.blocked_capabilities}
        assert not capabilities & {cap.value for cap in runtime.approval_required}
    assert "execute" in CAPABILITIES["stop_terminal"]
