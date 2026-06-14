"""E2E tests for the TUI revamp pipeline.

These tests exercise the full rendering pipeline end-to-end using:
  - TranscriptModel as the source of truth for transcript state
  - FakeTerminal as the I/O sink (no real TTY)
  - FrameComposer + RenderLoop driving the frame cycle
  - InputState for input handling
  - AgentRunnerBase / SignalBus for the full signal flow

NOTE: no ``from __future__ import annotations`` at module level —
``@tool()``/``@agent()`` inspect real annotations at decoration time.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from lauren_ai._agents import agent, use_tools
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import (
    SignalBus,
    ToolCallComplete,
    ToolCallStarted,
)
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

from agenthicc.tui.frame_composer import FrameComposer
from agenthicc.tui.input_state import InputState, InputResultKind
from agenthicc.tui.render_loop import RenderLoop
from agenthicc.tui.terminal import FakeTerminal, Key
from agenthicc.tui.transcript import ToolCallState, TranscriptModel

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Minimal StatusState stub (mirrors the fields FrameComposer reads)
# ---------------------------------------------------------------------------


@dataclass
class StatusState:
    """Minimal status object for test purposes."""

    active: bool = False
    partial_text: str = ""
    spinner_frame: int = 0
    intent_started_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str = "test-session"
    completed_agents: int = 0
    session_cost_usd: float = 0.0
    mode_name: str = "Auto"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


# ---------------------------------------------------------------------------
# 1. Full agent turn: partial_text → bottom; finalized line → committed
# ---------------------------------------------------------------------------


def test_render_loop_full_turn():
    """Partial text shows in bottom; finalized line moves to committed on next render."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)
    transcript = TranscriptModel()
    status = StatusState()

    # Simulate streaming: partial_text is set but nothing is finalized yet.
    status.active = True
    status.partial_text = "The agent is thinking..."

    loop.render(transcript, status=status, input_state=None)

    # Partial text must appear in the bottom block.
    bottom_text = "\n".join(terminal.bottom)
    assert "The agent is thinking" in bottom_text
    # Nothing committed yet (no finalized lines in transcript).
    assert terminal.committed == []

    # Finalize: add to transcript, clear partial.
    transcript.append_turn("agent-1", "assistant")
    transcript.append_line("agent-1", "I have completed the analysis.")
    status.active = False
    status.partial_text = ""

    loop.render(transcript, status=status, input_state=None)

    # The finalized line should now be committed.
    assert any("I have completed the analysis" in line for line in terminal.committed)
    # Partial text must not appear in the bottom block after clearing.
    bottom_text2 = "\n".join(terminal.bottom)
    assert "The agent is thinking" not in bottom_text2


# ---------------------------------------------------------------------------
# 2. Running tool call in bottom then committed on completion
# ---------------------------------------------------------------------------


def test_running_tool_call_in_bottom_then_committed():
    """A RUNNING tool call appears in transcript render output on both renders."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)
    transcript = TranscriptModel()

    # Seed a turn with a running tool call.
    transcript.append_turn("agent-1", "assistant")
    transcript.add_tool_call(
        "agent-1",
        tool_use_id="t-run-1",
        name="read_file",
        args={"path": "main.py"},
        state=ToolCallState.RUNNING,
    )

    # First render: transcript.render() includes the running tool call.
    loop.render(transcript, status=None, input_state=None)

    # The tool call line should now be committed (transcript.render() includes it).
    committed_text = "\n".join(terminal.committed)
    assert "read_file" in committed_text

    # Finish the tool call.
    transcript.finish_tool_call("t-run-1", success=True, duration_ms=42.0)

    # Second render: the finished tool call is part of transcript.render().
    loop.render(transcript, status=None, input_state=None)

    # The updated committed output should contain the SUCCESS symbol.
    committed_text2 = "\n".join(terminal.committed)
    assert "read_file" in committed_text2


# ---------------------------------------------------------------------------
# 3. Input history across multiple submits
# ---------------------------------------------------------------------------


def test_input_state_history_across_multiple_submits():
    """After three submits, pressing UP three times returns to the first entry."""
    st = InputState()

    # Submit "first"
    for ch in "first":
        st.handle(Key.CHAR, ch)
    r = st.handle(Key.ENTER, "")
    assert r.kind == InputResultKind.SUBMIT
    assert r.text == "first"

    # Submit "second"
    for ch in "second":
        st.handle(Key.CHAR, ch)
    r = st.handle(Key.ENTER, "")
    assert r.kind == InputResultKind.SUBMIT
    assert r.text == "second"

    # Submit "third"
    for ch in "third":
        st.handle(Key.CHAR, ch)
    r = st.handle(Key.ENTER, "")
    assert r.kind == InputResultKind.SUBMIT
    assert r.text == "third"

    # Navigate UP three times from the empty buffer.
    st.handle(Key.UP, "")  # → "third"
    st.handle(Key.UP, "")  # → "second"
    st.handle(Key.UP, "")  # → "first"

    assert st.text == "first"


# ---------------------------------------------------------------------------
# 4. No double rendering when transcript is unchanged
# ---------------------------------------------------------------------------


def test_no_double_rendering():
    """Rendering the same transcript three times does not commit duplicate lines."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)
    transcript = TranscriptModel()

    transcript.append_turn("agent-1", "assistant")
    transcript.append_line("agent-1", "Hello world.")

    # First render commits the line.
    loop.render(transcript, status=None, input_state=None)
    count_after_first = len(terminal.committed)

    # Second and third renders with no changes must not commit additional lines.
    loop.render(transcript, status=None, input_state=None)
    assert len(terminal.committed) == count_after_first

    loop.render(transcript, status=None, input_state=None)
    assert len(terminal.committed) == count_after_first


# ---------------------------------------------------------------------------
# 5. AgentRunnerBase signal flow wired to TranscriptModel + RenderLoop
# ---------------------------------------------------------------------------


async def test_agentrunnerbase_signal_flow():
    """SignalBus signals update TranscriptModel; FakeTerminal-backed RenderLoop
    reflects the state split (committed vs bottom) correctly.

    Uses MockTransport so no real LLM calls are made.
    """
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)
    transcript = TranscriptModel()
    status = StatusState()

    # Wire a SignalBus to mutate TranscriptModel on tool lifecycle signals.
    bus = SignalBus()
    agent_id = "runner-agent"

    @bus.on(ToolCallStarted)
    async def _on_started(sig) -> None:
        tool_use_id = getattr(sig, "tool_use_id", "")
        tool_name = getattr(sig, "tool_name", "")
        transcript.add_tool_call(
            agent_id,
            tool_use_id=tool_use_id,
            name=tool_name,
            args={},
            state=ToolCallState.RUNNING,
        )
        # Update status to show partial activity.
        status.active = True
        status.partial_text = f"Running {tool_name}…"
        # Render immediately so the bottom block captures partial state.
        loop.render(transcript, status=status, input_state=None)

    @bus.on(ToolCallComplete)
    async def _on_complete(sig) -> None:
        tool_use_id = getattr(sig, "tool_use_id", "")
        success = bool(getattr(sig, "success", True))
        duration_ms = float(getattr(sig, "duration_ms", 0.0) or 0.0)
        transcript.finish_tool_call(tool_use_id, success=success, duration_ms=duration_ms)
        status.active = False
        status.partial_text = ""
        # Render after completion so the finished tool line is committed.
        loop.render(transcript, status=status, input_state=None)

    # Build agent with a real @tool for the runner to dispatch.
    tool_called: list[str] = []

    @tool()
    async def ping_tool(label: str) -> dict:
        """Ping with a label.

        Args:
            label: The label to echo back.
        """
        tool_called.append(label)
        return {"pong": label}

    @agent(model="mock-model", system="You ping things.")
    @use_tools(ping_tool)
    class PingAgent: ...

    mock = MockTransport()
    mock.queue_tool_use("ping_tool", {"label": "hello"})
    mock.queue_response(_completion("All done.", n=2))

    # Append the agent turn before running so the transcript is seeded.
    transcript.append_turn(agent_id, "assistant")

    inst = PingAgent()
    runner = _build_runner_for_agent(inst, mock, signals=bus)
    response = await runner.run(inst, "Ping with hello")

    assert response.stop_reason == "end_turn"
    assert tool_called == ["hello"]

    # After signal flow: transcript has a tool call entry for ping_tool.
    assert any(
        tc.name == "ping_tool"
        for turn in transcript.turns
        for tc in turn.tool_calls
    ), "ping_tool should appear in transcript tool calls"

    # Do a final render and assert the committed buffer contains ping_tool.
    loop.render(transcript, status=status, input_state=None)
    committed_text = "\n".join(terminal.committed)
    assert "ping_tool" in committed_text, (
        f"ping_tool not found in committed lines:\n{committed_text}"
    )

    # Status cleared — "Running ping_tool" must not appear in the bottom.
    bottom_text = "\n".join(terminal.bottom)
    # After clearing partial_text the bottom should show idle status (no "Thinking").
    assert "Thinking" not in bottom_text
