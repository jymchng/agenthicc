"""Unit coverage for exploratory tool metadata and TUI consolidation."""

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
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit


def _event(
    name: str,
    *,
    exploratory: bool = False,
    target: str = "",
    files: list[str] | None = None,
    more_files: int = 0,
    success: bool = True,
) -> ConversationEvent:
    payload: dict[str, object] = {
        "name": name,
        "success": success,
        "args_str": "",
        "output_lines": [],
    }
    if exploratory:
        presentation: dict[str, object] = {"exploratory": True}
        if target:
            presentation["target"] = target
        if files is not None:
            presentation["files"] = files
        if more_files:
            presentation["more_files"] = more_files
        payload["presentation"] = presentation
    return ConversationEvent(name, "tool_complete", payload)


def _appender() -> tuple[ScrollBufferAppender, MagicMock]:
    state = MagicMock()
    state.conversation.live_tool_overflow.set = MagicMock()
    console = MagicMock()
    console.__enter__ = MagicMock(return_value=console)
    console.__exit__ = MagicMock(return_value=False)
    return ScrollBufferAppender(state, console), console


def _flush(appender: ScrollBufferAppender, events: list[ConversationEvent]) -> None:
    appender._pending = events
    appender._flush_scheduled = True
    appender._flush_batch()


def _printed_lines(console: MagicMock) -> list[str]:
    return [
        call.args[0]
        for call in console.print.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]


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


def test_exploration_target_is_bounded_and_redacted() -> None:
    presentation = _exploration_presentation(
        "grep_files",
        {
            "pattern": "def _emit|event_sinks",
            "path": "src/agenthicc/runners/_runner.py",
            "token": "do-not-display",
        },
    )

    assert presentation == {
        "exploratory": True,
        "target": "def _emit|event_sinks in src/agenthicc/runners/_runner.py",
    }
    assert "do-not-display" not in str(presentation)

    secret_target = _exploration_presentation(
        "inspect_agenthicc_source", {"target": "authorization: Bearer secret-token"}
    )
    assert secret_target == {"exploratory": True, "target": "<redacted>"}

    assert _exploration_presentation(
        "inspect_agenthicc_source", {"target": "agenthicc.runners.agent_turn"}
    ) == {
        "exploratory": True,
        "target": "agenthicc.runners.agent_turn",
    }


def test_batch_read_presentation_keeps_file_count_separate_from_target_text() -> None:
    presentation = _exploration_presentation(
        "batch_read",
        {"paths": ["password_generator/batch.py", "password_generator/templates.py", "README.md"]},
    )

    assert presentation == {
        "exploratory": True,
        "files": ["password_generator/batch.py", "password_generator/templates.py"],
        "more_files": 1,
    }


def test_agent_turn_persists_exploratory_presentation_metadata() -> None:
    conv_store = MagicMock()
    ctx = MagicMock()
    ctx.conv_store = conv_store
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
    assert payload["presentation"] == {
        "exploratory": True,
        "target": "src/agenthicc/config.py",
    }


def test_explored_block_groups_calls_and_flushes_before_mutation() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event("read_file", exploratory=True, target="command.py"),
            _event("search_files", exploratory=True, target="def _emit in _runner.py"),
            _event("write_file"),
        ],
    )

    lines = _printed_lines(console)
    assert sum("Explored" in line for line in lines) == 1
    assert any("Read command.py" in line for line in lines)
    assert any("[dim]└[/dim]" in line and "Read command.py" in line for line in lines)
    assert any("Search def _emit in _runner.py" in line for line in lines)
    assert any("Update" in line for line in lines)
    assert not any("[green]●[/green] [bold]Read" in line for line in lines)


def test_exploration_failure_is_not_hidden_and_starts_a_new_group() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event("read_file", exploratory=True, target="before.py"),
            _event("read_file", exploratory=True, target="broken.py", success=False),
            _event("read_file", exploratory=True, target="after.py"),
        ],
    )

    lines = _printed_lines(console)
    assert sum("Explored" in line for line in lines) == 2
    assert any("[red]Failed[/red]" in line for line in lines)


def test_exploration_group_is_bounded() -> None:
    appender, console = _appender()
    events = [
        _event("read_file", exploratory=True, target=f"file-{index}.py") for index in range(15)
    ]
    _flush(appender, events + [ConversationEvent("text", "text", {"text": "done"})])

    lines = _printed_lines(console)
    assert sum("file-" in line for line in lines) == 12
    assert any("3 more exploratory calls" in line for line in lines)


def test_batched_reads_use_list_prefix_and_report_omitted_files() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event(
                "batch_read",
                exploratory=True,
                files=["password_generator/batch.py", "password_generator/templates.py"],
                more_files=3,
            )
        ],
    )

    lines = _printed_lines(console)
    assert any(
        "[dim]└[/dim] Read password_generator/batch.py, "
        "password_generator/templates.py, and 3 more files." in line
        for line in lines
    )


def test_all_explored_tools_use_the_list_tree_prefix() -> None:
    appender, console = _appender()
    _flush(
        appender,
        [
            _event("list_directory", exploratory=True, target="."),
            _event("read_file", exploratory=True, target="README.md"),
            _event("search_files", exploratory=True, target="needle"),
        ],
    )

    lines = _printed_lines(console)
    assert lines.count("  [dim]└[/dim] List .") == 1
    assert lines.count("  [dim]└[/dim] Read README.md") == 1
    assert lines.count("  [dim]└[/dim] Search needle") == 1


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


def test_grouping_can_be_disabled_for_legacy_rendering() -> None:
    appender, console = _appender()
    appender._group_exploratory_calls = False
    _flush(appender, [_event("read_file", exploratory=True, target="README.md")])

    lines = _printed_lines(console)
    assert not any("Explored" in line for line in lines)
    assert any("[green]●[/green] [bold]Read" in line for line in lines)


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
        _event("read_file", exploratory=True, target="one.py"),
        _event("search_files", exploratory=True, target="needle in src"),
        ConversationEvent("answer", "text", {"text": "done"}),
    ]

    live_appender, live_console = _appender()
    _flush(live_appender, history)
    live_lines = _printed_lines(live_console)

    replay_events = [
        ConversationEvent(event.event_id, event.kind, dict(event.payload), event.timestamp)
        for event in history
    ]
    replay_appender, replay_console = _appender()
    replay_appender.replay(replay_events)
    replay_appender._flush_batch()

    assert _printed_lines(replay_console) == live_lines


def test_real_console_rendering_has_plain_explored_label() -> None:
    state = MagicMock()
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    appender = ScrollBufferAppender(state, console, group_exploratory_calls=True)
    _flush(appender, [_event("read_file", exploratory=True, target="README.md")])

    assert "● Explored" in output.getvalue()
    assert "└ Read README.md" in output.getvalue()
    assert "Read README.md" in output.getvalue()
