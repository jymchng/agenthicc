"""E2E: TUX-10 motion and feedback through a real, scripted agent turn.

A ``MockTransport`` scripts real ``tool_use`` completions; ``_run_agent_turn``
runs the built-in tools, and the status line is read at every state change.

NOTE: no ``from __future__ import annotations`` — the ``@tool()`` decorator
inspects annotations at decoration time.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport
from rich.text import Text

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.tui.runtime.mode_manager import build_default_registry
from agenthicc.tui.workspace.components import StatusLine
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.e2e


def _completion(index, content="", calls=()):
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content=content,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def app_state():
    state = TUIAppState.create()
    yolo = build_default_registry().get("Yolo")
    if yolo is not None:
        state.active_mode.set(yolo)
    return state


@pytest.fixture
async def processor(tmp_path):
    kernel_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(proc.run())
    yield proc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _turn(app_state, processor, mock, text):
    cfg = AgenthiccConfig()
    await _run_agent_turn(
        text,
        runner=AgentRunnerBase(transport=mock, signals=SignalBus()),
        processor=processor,
        session_memory=ShortTermMemory(max_tokens=cfg.execution.effective_usable_budget()),
        max_agent_turns=8,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills=None,
        skill_permissions=cfg.agents.skill_permissions_for("auto"),
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=[],
        system_prompt_suffix="",
    )


def _frames(app_state: TUIAppState) -> list[str]:
    """Record the status line every time the agent's state or its running calls change."""
    frames: list[str] = []
    line = StatusLine(app_state)

    def capture() -> None:
        frames.append(Text.from_markup(line.markup(120)).plain)

    conv = app_state.conversation
    conv.agent_state.subscribe(capture)
    conv.running_tools.subscribe(capture)
    conv.active_tool.subscribe(capture)
    return frames


async def test_the_status_line_names_what_the_agent_is_doing_never_a_tool(
    tmp_path, monkeypatch, app_state, processor
):
    """AC 1: ``Reading <file>`` then ``Running npm test``; no ⚒ and no internal tool name."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    mock = MockTransport()
    mock.queue_response(
        _completion(
            0, calls=[ToolCall(tool_use_id="r1", name="read_file", input={"path": "src/app.py"})]
        )
    )
    mock.queue_response(
        _completion(
            1, calls=[ToolCall(tool_use_id="b1", name="run_bash", input={"command": "npm test"})]
        )
    )
    mock.queue_response(_completion(2, "Done."))
    frames = _frames(app_state)
    await _turn(app_state, processor, mock, "run the tests")

    reading = next(i for i, frame in enumerate(frames) if "Reading src/app.py" in frame)
    running = next(i for i, frame in enumerate(frames) if "Running npm test" in frame)
    assert reading < running
    for frame in frames:
        assert "⚒" not in frame
        for internal in ("read_file", "run_bash"):
            assert internal not in frame, frame


async def test_cancelling_a_running_turn_ends_it_as_interrupted(
    tmp_path, monkeypatch, app_state, processor
):
    """AC 3.2 on the real cancellation path: the closing event says so and nothing was written."""
    monkeypatch.chdir(tmp_path)
    mock = MockTransport()
    mock.queue_response(
        _completion(
            0, calls=[ToolCall(tool_use_id="b1", name="run_bash", input={"command": "sleep 30"})]
        )
    )
    mock.queue_response(_completion(1, "Done."))
    events = []
    app_state.conversation.on_event(events.append)
    task = asyncio.create_task(_turn(app_state, processor, mock, "wait"))
    for _ in range(200):
        if app_state.conversation.running_tools():
            break
        await asyncio.sleep(0.05)
    assert app_state.conversation.running_tools(), "the command never started"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    closing = [event for event in events if event.kind == "turn_complete"]
    assert len(closing) == 1 and closing[0].payload.get("interrupted") is True
    assert not any(event.kind == "file_modified" for event in events)


# ── escape sequences stay out of non-terminal output ──────────────────────────


@cli_test_timeout(1)
def test_headless_writes_no_escape_sequence_even_where_every_one_is_supported(
    tmp_path: Path,
) -> None:
    """§5: under ``--headless`` (a pipe) nothing is a title, progress, link or notification."""
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENTHICC_")
        and name not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
    }
    env.update(
        HOME=str(home),
        TERM="xterm-kitty",
        TERM_PROGRAM="ghostty",
        WT_SESSION="{guid}",
        FORCE_HYPERLINK="1",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "agenthicc", "--headless", "--workflow", "code_plan"],
        cwd=project,
        env=env,
        input="Explain this repository\n",
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip(), "the headless run printed its JSON lines"
    assert "\x1b" not in completed.stdout and "\x1b" not in completed.stderr
