"""TUX-06 §2.2: human-readable tool summaries and the bounded event projection.

NOTE: no ``from __future__ import annotations`` — the ``@tool()`` decorator
inspects annotations at decoration time.
"""

from pathlib import Path

import pytest
from lauren_ai._tools import tool

from agenthicc.tui import tool_summary as ts

pytestmark = pytest.mark.unit

_ROOT = Path("/work/project")


def _summary(name: str, args: dict[str, object], result: object = None) -> ts.ToolSummary:
    return ts.summarize_call(name, args, result, root=_ROOT)


# ── commands ──────────────────────────────────────────────────────────────────


def test_a_cd_chain_keeps_the_last_command_and_the_directory_as_detail() -> None:
    summary = _summary(
        "run_bash", {"command": "cd /root/youtube_projects/objective-create-a && npm test"}
    )
    assert summary == ts.ToolSummary("npm test", "in objective-create-a/")


def test_a_multi_line_script_shows_its_first_line_and_the_rest_as_a_count() -> None:
    summary = _summary("run_bash", {"command": "set -e\nmake build\nmake test\n"})
    assert summary == ts.ToolSummary("set -e", "+2 lines")


def test_run_command_joins_argv_and_uses_cwd() -> None:
    summary = _summary("run_command", {"argv": ["git", "log", "-n", "5"], "cwd": "/w/repo"})
    assert summary == ts.ToolSummary("git log -n 5", "in repo/")


# ── files ─────────────────────────────────────────────────────────────────────


def test_a_read_is_relative_to_the_workspace_with_its_line_range() -> None:
    summary = _summary(
        "read_file", {"path": "/work/project/src/app/main.py", "offset": 40, "limit": 81}
    )
    assert summary.target == "src/app/main.py:40-120"


def test_a_batch_read_counts_its_files() -> None:
    summary = _summary("batch_read", {"paths": ["a.py", "b.py", "c.py"]})
    assert summary == ts.ToolSummary("3 files", "a.py, b.py, c.py")


def test_an_edit_counts_the_lines_of_its_diff() -> None:
    diff = "--- a/x\n+++ b/x\n@@\n-old\n+new\n+more\n"
    summary = _summary("patch_file", {"path": "src/x.py"}, {"diff": diff})
    assert summary == ts.ToolSummary("src/x.py", "+2 −1")


def test_long_paths_are_cut_in_the_middle_so_the_file_name_shows() -> None:
    path = "src/agenthicc/tui/workspace/appender.py"
    assert ts.middle_truncate(path, 20, path=True) == "src/…/appender.py"
    assert ts.middle_truncate("abcdefghij", 5) == "ab…ij"
    assert ts.middle_truncate("short", 10) == "short"


# ── search, list, git, memory, web ────────────────────────────────────────────


def test_a_search_names_the_query_the_location_and_the_matches() -> None:
    summary = _summary("grep_files", {"pattern": "TODO", "path": "src"}, {"total_matches": 14})
    assert summary == ts.ToolSummary('"TODO" in src/', "→ 14 matches")


def test_a_listing_counts_its_entries() -> None:
    summary = _summary("list_directory", {"path": "src/agenthicc"}, {"count": 23})
    assert summary == ts.ToolSummary("src/agenthicc/", "→ 23 entries")


def test_git_status_counts_the_changed_files() -> None:
    result = {"staged": ["a"], "unstaged": ["b"], "untracked": ["c"]}
    assert _summary("git_status", {}, result) == ts.ToolSummary("status", "→ 3 changed")


def test_a_memory_read_is_human_and_never_json() -> None:
    summary = _summary("memory_read", {"key": "deploy notes"}, {"found": True, "value": {"a": 1}})
    assert summary == ts.ToolSummary('memory: "deploy notes"', "found")
    hits = _summary("semantic_search", {"query": "deploy notes"}, {"results": [1, 2, 3]})
    assert hits == ts.ToolSummary('memory: "deploy notes"', "(3 entries)")


def test_a_fetch_shows_the_host_and_path() -> None:
    summary = _summary("fetch_page", {"url": "https://docs.python.org/3/library/asyncio?x=1"})
    assert summary.target == "docs.python.org/3/library/asyncio"


def test_mcp_tools_show_server_and_tool_with_the_first_string_argument() -> None:
    presentation = ts.tool_presentation("mcp:github:create_issue")
    assert presentation.operation == "github · create_issue"
    assert presentation.family is ts.ToolFamily.MCP
    assert _summary("mcp:github:create_issue", {"n": 3, "title": "Fix it"}).target == "Fix it"


# ── plugin summaries ──────────────────────────────────────────────────────────


@ts.summarized_by(lambda call: f"ticket {call.args['id']}", operation="Jira")
@tool()
async def jira_issue(id: str) -> dict:
    """Fetch one Jira issue. Args: id: Issue key."""
    return {"id": id}


def test_a_plugin_tool_supplies_its_own_summary_through_metadata() -> None:
    presentation = ts.presentation_from_metadata(jira_issue)
    assert presentation is not None and presentation.operation == "Jira"
    summary = ts.summarize_call("jira_issue", {"id": "OPS-7"}, None, presentation=presentation)
    assert summary.target == "ticket OPS-7"
    assert ts.tool_presentation("jira_issue", presentation).operation == "Jira"


def test_a_failing_summarizer_falls_back_to_the_first_string_argument() -> None:
    def broken(call: ts.SummaryInput) -> str:
        raise RuntimeError("boom")

    presentation = ts.ToolPresentation("Custom", ts.ToolFamily.OTHER, broken)
    summary = ts.summarize_call("custom", {"n": 1, "q": "needle"}, None, presentation=presentation)
    assert summary.target == "needle"


def test_an_unregistered_tool_titles_its_name() -> None:
    assert ts.tool_presentation("frobnicate_widget").operation == "Frobnicate Widget"


# ── the bounded event projection ──────────────────────────────────────────────


def test_args_are_bounded_and_redacted() -> None:
    args: dict[str, object] = {f"k{index}": index for index in range(12)}
    assert len(ts.bounded_args(args)) == ts.ARG_KEY_LIMIT
    projected = ts.bounded_args(
        {"token": "abc", "header": "Authorization: Bearer x", "body": "x" * 2_000, "n": None}
    )
    assert projected["token"] == "<redacted>"
    assert projected["header"] == "<redacted>"
    body = projected["body"]
    assert isinstance(body, str) and len(body) == ts.ARG_VALUE_LIMIT
    assert projected["n"] is None


def test_the_output_keeps_its_head_and_tail_and_counts_every_line() -> None:
    lines = [f"line {index}" for index in range(100)]
    projection = ts.output_projection({"stdout": "\n".join(lines), "stderr": ""})
    assert projection.total == 100
    assert projection.head == ts.OUTPUT_HEAD_LINES
    assert projection.lines[: projection.head] == tuple(lines[: ts.OUTPUT_HEAD_LINES])
    assert projection.lines[projection.head :] == tuple(lines[-ts.OUTPUT_TAIL_LINES :])


def test_exit_and_error_codes_come_from_the_result_envelope() -> None:
    assert ts.exit_code_of({"state": "exited", "returncode": 2}) == 2
    assert ts.exit_code_of({"state": "running", "returncode": 0}) is None
    assert ts.error_code_of({"ok": False, "error_code": "not_found"}) == "not_found"
    assert ts.error_code_of({"ok": True, "error_code": "not_found"}) is None
