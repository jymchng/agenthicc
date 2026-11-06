"""Unit coverage for exploratory tool metadata and TUI consolidation (PRD-161, TUX-06)."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest
from lauren_ai._signals import ToolCallComplete
from rich.console import Console

from agenthicc.agent_tools import (
    git_status,
    inspect_agenthicc_source,
    read_file,
    search_files,
    write_file,
)
from agenthicc.runners.agent_turn import (
    AgentTurnRunner,
    _exploration_presentation,
)
from agenthicc.tools.capabilities import (
    ToolCapability,
    get_tool_capabilities,
    is_exploratory_tool,
    tool_exploratory,
    tool_read,
)
from agenthicc.tui.conversation_store import ConversationEvent, ConversationStore
from agenthicc.tui.workspace.appender import ScrollBufferAppender, set_session_tool_detail

pytestmark = pytest.mark.unit


def _event(
    name: str,
    summary: str = "",
    *,
    exploratory: bool = False,
    success: bool = True,
    file_count: int = 0,
) -> ConversationEvent:
    payload: dict[str, object] = {
        "name": name,
        "success": success,
        "summary": summary,
        "output_lines": [],
    }
    if exploratory:
        presentation: dict[str, object] = {"exploratory": True}
        if file_count:
            presentation["file_count"] = file_count
        payload["presentation"] = presentation
    return ConversationEvent(f"{name}-{summary}", "tool_complete", payload)


def _appender() -> tuple[ScrollBufferAppender, Console]:
    state = MagicMock()
    console = Console(file=StringIO(), record=True, width=100, color_system=None)
    return ScrollBufferAppender(state, console), console


def _flush(appender: ScrollBufferAppender, events: list[ConversationEvent]) -> None:
    appender._pending = events
    appender._flush_scheduled = True
    appender._flush_batch()


def _rows(console: Console) -> list[str]:
    return [row.rstrip() for row in console.export_text(clear=False).splitlines()]


@pytest.fixture(autouse=True)
def _reset_detail() -> object:
    yield
    set_session_tool_detail(None)


def test_exploratory_metadata_is_separate_from_security_capabilities() -> None:
    @tool_exploratory
    @tool_read
    async def inspect_fixture(path: str) -> dict[str, object]:
        return {"path": path}

    assert get_tool_capabilities(inspect_fixture) == frozenset({ToolCapability.READ})
    assert is_exploratory_tool(inspect_fixture) is True
    assert is_exploratory_tool(write_file) is False


def test_builtin_readers_are_exploratory_but_mutations_are_not() -> None:
    assert is_exploratory_tool(read_file) is True
    assert is_exploratory_tool(search_files) is True
    assert is_exploratory_tool(git_status) is True
    assert is_exploratory_tool(inspect_agenthicc_source) is True
    assert is_exploratory_tool(write_file) is False


def test_malformed_presentation_metadata_fails_closed() -> None:
    async def malformed() -> dict[str, object]:
        return {}

    malformed.__lauren_ai_tool_metadata__ = {"presentation": {"exploratory": "yes"}}
    assert is_exploratory_tool(malformed) is False


def test_batch_read_presentation_counts_its_files() -> None:
    presentation = _exploration_presentation(
        "batch_read",
        {"paths": ["password_generator/batch.py", "password_generator/templates.py", "README.md"]},
    )

    assert presentation == {"exploratory": True, "file_count": 3}
    assert _exploration_presentation("read_file", {"path": "x"}) == {"exploratory": True}


def test_agent_turn_persists_exploratory_presentation_metadata() -> None:
    conv_store = MagicMock()
    ctx = MagicMock()
    ctx.conv_store = conv_store
    ctx.workspace_access = None
    runner = AgentTurnRunner(ctx)
    runner._exploratory_tools = {"read_file"}
    runner._tool_names["call-1"] = "read_file"
    runner._tool_args["call-1"] = {"path": "src/agenthicc/config.py"}

    import asyncio

    asyncio.run(
        runner._handle_tool_complete(
            ToolCallComplete(
                tool_name="read_file",
                tool_use_id="call-1",
                duration_ms=2.0,
                success=True,
            )
        )
    )

    payload = conv_store.append_event.call_args.args[1]
    assert payload["presentation"] == {"exploratory": True}
    assert payload["summary"] == "src/agenthicc/config.py"


def test_explored_calls_collapse_to_one_row_and_flush_before_a_mutation() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event("read_file", "command.py", exploratory=True),
            _event("search_files", '"def _emit" in src/', exploratory=True),
            _event("write_file", "out.py"),
        ],
    )

    assert _rows(console) == [
        '● Explored 1 file, 1 search  command.py, "def _emit" in src/',
        "● Write out.py",
    ]


def test_explored_row_and_the_next_tool_have_no_blank_between() -> None:
    appender, console = _appender()
    _flush(
        appender, [_event("read_file", "Makefile", exploratory=True), _event("run_tests", "unit")]
    )

    assert _rows(console) == ["● Explored 1 file  Makefile", "● Test unit"]


def test_exploration_failure_is_not_hidden_and_starts_a_new_group() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event("read_file", "before.py", exploratory=True),
            _event("read_file", "broken.py", exploratory=True, success=False),
            _event("read_file", "after.py", exploratory=True),
        ],
    )
    appender.flush()

    rows = _rows(console)
    assert rows[0] == "● Explored 1 file  before.py"
    assert rows[1].startswith("✗ Read broken.py") and rows[1].endswith("failed")
    assert rows[-1] == "● Explored 1 file  after.py"


def test_verbose_lists_the_calls_and_bounds_the_list() -> None:
    appender, console = _appender()
    set_session_tool_detail("verbose")
    events = [_event("read_file", f"file-{index}.py", exploratory=True) for index in range(15)]
    _flush(appender, events + [ConversationEvent("text", "text", {"text": "done"})])

    rows = _rows(console)
    assert rows[0].startswith("● Explored 15 files  file-0.py, file-1.py")
    assert sum(row.startswith("  └ Read file-") for row in rows) == 12
    assert "  └ …and 3 more calls" in rows


def test_batched_reads_count_every_file() -> None:
    appender, console = _appender()
    _flush(appender, [_event("batch_read", "5 files", exploratory=True, file_count=5)])
    appender.flush()

    assert _rows(console) == ["● Explored 5 files  5 files"]


def test_other_exploratory_tools_are_counted_as_lookups() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event("list_directory", "./", exploratory=True),
            _event("git_status", "status", exploratory=True),
            _event("git_log", "log", exploratory=True),
        ],
    )
    appender.flush()

    assert _rows(console) == ["● Explored 1 listing, 2 lookups  ./, status, log"]


def test_conversation_store_keeps_each_exploration_event_granular() -> None:
    store = ConversationStore()
    store.begin_turn("assistant")
    store.append_event("tool_complete", {"name": "read_file", "success": True})
    assert store.tool_group_count() == 1
    store.append_event(
        "tool_complete",
        {"name": "read_file", "success": True, "presentation": {"exploratory": True}},
    )
    assert store.tool_group_count() == 2
    store.append_event("tool_complete", {"name": "write_file", "success": True})
    assert store.tool_group_count() == 3


def test_grouping_can_be_disabled() -> None:
    appender, console = _appender()
    appender._group_exploratory_calls = False
    _flush(appender, [_event("read_file", "README.md", exploratory=True)])

    assert _rows(console) == ["● Read README.md"]


def test_grouping_flag_defaults_on_and_parses_off(tmp_path) -> None:
    from agenthicc.config import load_config

    default = load_config(
        project_path=tmp_path / "missing.toml",
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )
    assert default.tools.group_exploratory_calls is True

    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text("[tools]\ngroup_exploratory_calls = false\n", encoding="utf-8")
    configured = load_config(
        project_path=config_path,
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )
    assert configured.tools.group_exploratory_calls is False


def test_typed_tool_opt_in_survives_adapter() -> None:
    from agenthicc.tools.base import ToolBase, ToolResult
    from agenthicc.tools.executor import _make_lauren_tool

    class InspectTool(ToolBase):
        name = "inspect_typed"
        exploratory = True

        async def execute(self, context: object, args: object) -> ToolResult:
            return ToolResult.success({})

    adapted = _make_lauren_tool(InspectTool())
    assert is_exploratory_tool(adapted) is True


def test_replay_uses_the_same_derived_grouping() -> None:
    history = [
        _event("read_file", "one.py", exploratory=True),
        _event("search_files", '"needle" in src/', exploratory=True),
        ConversationEvent("answer", "text", {"text": "done"}),
    ]

    live_appender, live_console = _appender()
    _flush(live_appender, history)

    replay_events = [
        ConversationEvent(event.event_id, event.kind, dict(event.payload), event.timestamp)
        for event in history
    ]
    replay_appender, replay_console = _appender()
    replay_appender.replay(replay_events)
    replay_appender._flush_batch()

    assert _rows(replay_console) == _rows(live_console)


def test_chunked_replay_can_continue_one_explored_group() -> None:
    appender, console = _appender()
    first = [_event("read_file", "one.py", exploratory=True)]
    second = [
        _event("search_files", "needle", exploratory=True),
        ConversationEvent("answer", "text", {"text": "done"}),
    ]

    appender.replay(first)
    appender.replay(second, continue_group=True)
    appender._flush_batch()

    assert _rows(console)[0] == "● Explored 1 file, 1 search  one.py, needle"
