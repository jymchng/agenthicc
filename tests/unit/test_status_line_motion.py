"""TUX-10 §2.1 / AC 1-2: the working indicator says what the agent is doing.

The state segment keeps agenthicc's turning flower and the bold letter that
sweeps across ``Thinking`` (operator decision); its words name the activity
from the tool family's summary, never an internal tool name.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from rich.text import Text

from agenthicc.config import UISettings
from agenthicc.subagents.pool import SubagentPoolState, WorkerState
from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tui.activity import retry_text, tool_verb, waiting_reason
from agenthicc.tui.conversation_store import AppState, RunningTool
from agenthicc.tui.theme import glyphs
from agenthicc.tui.tool_summary import tool_presentation
from agenthicc.tui.workspace.components import StatusLine

pytestmark = pytest.mark.unit

_FLOWERS = "✿❀❁❃✾❋✽❊"


def _state(*, ui: UISettings | None = None) -> AppState:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    state.conversation.model_name.set("anthropic/claude-opus-4-8")
    return state


def _line(state: AppState, width: int = 120) -> str:
    return Text.from_markup(StatusLine(state).markup(width)).plain


def _call(state: AppState, name: str, target: str) -> None:
    """A tool call that has started, as the agent turn announces it."""
    conv = state.conversation
    conv.set_tool(name)
    presentation = tool_presentation(name)
    conv.start_running_tool(
        RunningTool(
            key=name,
            operation=presentation.operation,
            target=target,
            started_at=time.time(),
            verb=tool_verb(presentation),
        )
    )


@pytest.mark.parametrize(
    ("tool", "target", "expected"),
    [
        ("read_file", "src/app.py", "Reading src/app.py"),
        ("grep_files", '"TODO" in docs/', 'Searching "TODO" in docs/'),
        ("run_bash", "npm test", "Running npm test"),
        ("patch_file", "src/app.py", "Editing src/app.py"),
        ("write_file", "notes.md", "Writing notes.md"),
        ("list_directory", "src/", "Listing src/"),
        ("git_status", "", "Running git"),
        ("fetch_page", "example.org", "Fetching example.org"),
    ],
)
def test_the_verb_follows_the_tool_family_and_names_its_target(
    tool: str, target: str, expected: str
) -> None:
    state = _state()
    state.conversation.begin_turn("agent", "t1")
    _call(state, tool, target)
    line = _line(state)
    assert expected in line
    assert tool not in line and "⚒" not in line


def test_a_plugin_tool_reads_using_its_operation_word() -> None:
    from agenthicc.tui.tool_summary import ToolPresentation

    assert tool_verb(ToolPresentation("Jira")) == "Using Jira"
    assert tool_verb(ToolPresentation("")) == "Working"


def test_a_model_streaming_with_no_tool_is_thinking() -> None:
    state = _state()
    state.conversation.begin_turn("agent", "t1")
    assert "Thinking" in _line(state)


def test_the_flower_and_the_bold_sweep_are_kept() -> None:
    state = _state()
    state.conversation.begin_turn("agent", "t1")
    state.conversation.frame.set(1)
    markup = StatusLine(state).markup(120)
    assert any(flower in Text.from_markup(markup).plain for flower in _FLOWERS)
    assert "T[bold]h[/bold]inking" in markup


def test_subagent_workers_read_as_working_with_n_agents() -> None:
    state = _state()
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    _call(state, "run_bash", "npm test")
    conv.subagent_pool_state.set(
        SubagentPoolState(
            "pool",
            3,
            (
                WorkerState("a", "tester", "running"),
                WorkerState("b", "tester", "running"),
                WorkerState("c", "tester", "done"),
            ),
        )
    )
    assert "Working with 2 agents" in _line(state)
    assert "npm test" not in _line(state)


def test_a_scheduled_provider_retry_counts_down() -> None:
    state = _state()
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    conv.begin_retry(2, 5, 8.0)
    assert retry_text(2, 5, 8) == "Retrying (2/5) in 8s"
    assert "Retrying (2/5) in" in _line(state)
    conv.close_turn()
    assert "Retrying" not in _line(state)
    assert conv.retry() is None


def test_the_retry_countdown_ends_with_its_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("agenthicc.tui.conversation_store.time.monotonic", lambda: clock[0])
    state = _state()
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    conv.begin_retry(1, 3, 2.0)
    assert conv.retry() is not None and conv.retry().seconds_left == 2
    clock[0] = 101.2
    conv.tick()
    assert conv.retry() is not None and conv.retry().seconds_left == 1
    clock[0] = 102.5
    conv.tick()
    assert conv.retry() is None


def _approval(kind: str = "tool", tool: str = "run_bash") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name=tool,
        tool_use_id="x",
        tool_input={"command": "npm test"},
        capabilities=frozenset({"execute"}),
        event=asyncio.Event(),
        kind=kind,
    )


@pytest.mark.parametrize(
    ("kind", "tool", "reason"),
    [
        ("tool", "run_bash", "approve a command"),
        ("tool", "write_file", "approve a change"),
        ("plan_review", "", "review the plan"),
        ("questions", "", "answer a question"),
    ],
)
def test_waiting_for_the_user_stops_the_spinner_and_says_what_for(
    kind: str, tool: str, reason: str
) -> None:
    state = _state()
    state.conversation.begin_turn("agent", "t1")
    state.pending_approval.set(_approval(kind, tool))
    line = _line(state).strip()
    g = glyphs()
    assert line.startswith(f"{g.diamond} Waiting for you {g.sep} {reason}")
    assert not any(flower in line for flower in _FLOWERS)
    assert waiting_reason(kind, tool) == reason


def test_the_waiting_state_uses_the_warning_token() -> None:
    from agenthicc.tui.theme import palette

    state = _state()
    state.pending_approval.set(_approval())
    assert palette().warning in StatusLine(state).markup(120)


def test_elapsed_time_appears_after_three_seconds_in_muted() -> None:
    from agenthicc.tui.theme import palette

    state = _state()
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    conv.activity_elapsed_s.set(2.0)
    assert " 2s" not in _line(state) and " · 2s" not in _line(state)
    conv.activity_elapsed_s.set(3.0)
    assert "Thinking · 3s" in _line(state)
    assert f"[{palette().muted}] · 3s[/]" in StatusLine(state).markup(120)


def test_tokens_appear_after_ten_seconds_when_the_provider_streams_usage() -> None:
    state = _state()
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    conv.activity_elapsed_s.set(9.0)
    conv.add_tokens(100, 2_100, 0.0)
    assert "tokens" not in _line(state)
    conv.activity_elapsed_s.set(12.0)
    assert "Thinking · 12s · ↓ 2.1k tokens" in _line(state)
    # No usage reported: no token count, however long it has run.
    other = _state()
    other.conversation.begin_turn("agent", "t1")
    other.conversation.activity_elapsed_s.set(40.0)
    assert "tokens" not in _line(other)


def test_the_token_count_is_this_requests_not_the_sessions() -> None:
    state = _state()
    conv = state.conversation
    conv.add_tokens(50, 9_000, 0.0)  # an earlier request
    conv.begin_turn("agent", "t1")
    conv.activity_elapsed_s.set(20.0)
    assert "tokens" not in _line(state)
    conv.add_tokens(50, 300, 0.0)
    assert "↓ 300 tokens" in _line(state)


def test_a_long_target_is_shortened_before_the_state_is_cut() -> None:
    state = _state()
    state.conversation.begin_turn("agent", "t1")
    target = "src/agenthicc/tui/workspace/" + "very_long_directory_name/" * 4 + "appender.py"
    _call(state, "read_file", target)
    line = _line(state, 80)
    assert "Reading" in line and "appender.py" in line


def test_the_screen_reader_hears_one_plain_sentence() -> None:
    state = _state(ui=UISettings(screen_reader=True))
    state.conversation.begin_turn("agent", "t1")
    _call(state, "read_file", "src/app.py")
    state.conversation.activity_elapsed_s.set(12.0)
    line = _line(state)
    assert "Reading src/app.py, 12s" in line
    assert not any(ch in line for ch in _FLOWERS + "↓⚒")


# ── AC 2: reduced motion ──────────────────────────────────────────────────────


def _frame_after(state: AppState, ticks: int) -> str:
    """The status row after *ticks* more frames of the 10 Hz clock (500 ms is five)."""
    state.conversation.frame.set(state.conversation.frame() + ticks)
    return StatusLine(state).markup(120)


def test_reduced_motion_frames_500_ms_apart_are_identical() -> None:
    state = _state(ui=UISettings(reduced_motion=True))
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    _call(state, "run_bash", "npm test")
    conv.activity_elapsed_s.set(12.0)
    first = StatusLine(state).markup(120)
    assert _frame_after(state, 5) == first
    assert _frame_after(state, 5) == first
    plain = Text.from_markup(first).plain.strip()
    assert plain.startswith(glyphs().bullet) and "Running npm test" in plain
    assert not any(flower in plain for flower in _FLOWERS)


def test_without_reduced_motion_the_same_frames_differ() -> None:
    state = _state()
    state.conversation.begin_turn("agent", "t1")
    first = StatusLine(state).markup(120)
    assert _frame_after(state, 5) != first


def test_reduced_motion_keeps_the_waiting_state_still_and_its_words() -> None:
    state = _state(ui=UISettings(reduced_motion=True))
    state.pending_approval.set(_approval())
    first = StatusLine(state).markup(120)
    assert _frame_after(state, 7) == first
    assert "Waiting for you" in first
