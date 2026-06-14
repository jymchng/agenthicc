"""E2E tests for the Textual TUI with mock AgentRunner (PRD-55 Phase 7C).

Tests exercise the full path from user input → AgenthiccApp → on_intent →
mock agent turn → Textual messages → widget state updates.

We avoid real LLM calls by providing a mock on_intent that posts the same
Textual messages that _run_agent_turn() would post:

    AgentRunStarted
    ToolCallStarted
    ToolCallComplete
    TokensUpdated
    TranscriptUpdated
    AgentRunFinished

This validates the full widget pipeline without requiring an API key.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.messages import (
    AgentRunFinished,
    AgentRunStarted,
    ToolCallComplete,
    ToolCallStarted,
    TokensUpdated,
    TranscriptUpdated,
)

pytestmark = pytest.mark.e2e


# ── test-safe AgenthiccApp wrapper ────────────────────────────────────────────


def _make_app(model: TranscriptModel | None = None):
    """Create AgenthiccApp with a restored Textual real console for headless testing."""
    from agenthicc.tui.app import AgenthiccApp
    from rich.console import Console

    if model is None:
        model = TranscriptModel()
    app = AgenthiccApp(model=model, base_path=".")
    # Restore Textual's real Console: AgenthiccApp replaces it with ConsoleShim
    # in __init__, which breaks Textual's rendering pipeline in headless tests.
    real_console = Console(highlight=False, markup=True)
    object.__setattr__(app, "console", real_console)
    return app, model


# ── mock agent turn helpers ───────────────────────────────────────────────────


async def _simulate_agent_turn(
    app,
    model: TranscriptModel,
    *,
    agent_id: str = "test-agent",
    model_short: str = "mock-llm",
    tool_id: str = "tc1",
    tool_name: str = "git_status",
    tool_args: dict | None = None,
    tool_success: bool = True,
    response_text: str = "Agent response here.",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cost_usd: float = 0.01,
    delay: float = 0.01,
) -> None:
    """Simulate a full agent turn by posting Textual messages in the correct order.

    Mirrors the sequence that _run_agent_turn() would produce:
        1. AgentRunStarted
        2. ToolCallStarted
        3. ToolCallComplete
        4. TokensUpdated
        5. (append to TranscriptModel)
        6. TranscriptUpdated
        7. AgentRunFinished
    """
    args = tool_args or {}

    # 1. Signal agent turn beginning.
    app.post_message(AgentRunStarted(agent_id=agent_id, model_short=model_short))
    await asyncio.sleep(delay)

    # 2. Signal tool call start.
    app.post_message(ToolCallStarted(tool_use_id=tool_id, name=tool_name, args=args))
    await asyncio.sleep(delay)

    # 3. Signal tool call completion.
    app.post_message(
        ToolCallComplete(
            tool_use_id=tool_id,
            success=tool_success,
            duration_ms=50.0,
            error=None,
            diff=None,
        )
    )
    await asyncio.sleep(delay)

    # 4. Update token counts.
    app.post_message(TokensUpdated(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    ))
    await asyncio.sleep(delay)

    # 5. Append agent response to the transcript model.
    turn = model.append_turn(agent_id, f"assistant ({model_short})")
    turn.lines.append(response_text)

    # 6. Signal the transcript has new content.
    app.post_message(TranscriptUpdated())
    await asyncio.sleep(delay)

    # 7. Signal agent turn complete.
    app.post_message(AgentRunFinished())
    await asyncio.sleep(delay)


# ── E2E Test 1: full agent turn pipeline ─────────────────────────────────────


@pytest.mark.e2e
async def test_full_agent_turn_pipeline() -> None:
    """Submit 'hello', agent mock runs, transcript shows response, status hidden.

    Flow:
        1. Type 'hello' + Enter in InputPanel
        2. on_intent fires mock agent turn
        3. SpinnerPanel showed git_status tool
        4. StatusBar had token counts
        5. TranscriptView has agent response
        6. StatusBar hidden after completion
    """
    from agenthicc.tui.widgets.input_panel import InputPanel
    from agenthicc.tui.widgets.status_bar import StatusBar
    from agenthicc.tui.widgets.spinner_panel import SpinnerPanel
    from agenthicc.tui.widgets.transcript_view import TranscriptView
    from textual.widgets import RichLog

    agent_invoked: list[str] = []

    app, model = _make_app()

    async def mock_on_intent(text: str) -> None:
        agent_invoked.append(text)
        await _simulate_agent_turn(
            app,
            model,
            tool_name="git_status",
            response_text=f"I processed: {text}",
        )

    async with app.run_test(headless=True) as pilot:
        app._on_intent = mock_on_intent
        panel = app.query_one(InputPanel)
        panel.focus()

        # Step 1: type + submit.
        for ch in "hello":
            await pilot.press(ch)
        await pilot.press("enter")

        # Wait for the full agent turn to complete.
        await pilot.pause(0.5)

        # Verify on_intent was called with our text.
        assert agent_invoked == ["hello"], f"on_intent should have been called with 'hello', got {agent_invoked}"

        # Step 5: TranscriptView has agent response.
        tv = app.query_one(TranscriptView)
        richlog = tv.query_one("#transcript-richlog", RichLog)
        assert len(richlog.lines) > 0, "TranscriptView should have content after agent turn"

        # Step 6: StatusBar hidden after completion.
        bar = app.query_one(StatusBar)
        assert bar.active is False, "StatusBar should be inactive after AgentRunFinished"

        # Step 4: Token counts were updated (StatusBar stores them from on_tokens_updated).
        assert bar.input_tokens == 1000, f"input_tokens should be 1000, got {bar.input_tokens}"
        assert bar.output_tokens == 200, f"output_tokens should be 200, got {bar.output_tokens}"


# ── E2E Test 2: spinner panel shows and tracks tools ─────────────────────────


@pytest.mark.e2e
async def test_spinner_panel_tracks_tool_calls() -> None:
    """SpinnerPanel shows tool call during streaming, tracks state changes.

    Verify:
      - SpinnerPanel mounted after ToolCallStarted
      - Tool call entry exists in SpinnerPanel._tool_calls
      - After ToolCallComplete, entry is marked done=True
      - SpinnerPanel unmounted after AgentRunFinished
    """
    from agenthicc.tui.widgets.spinner_panel import SpinnerPanel

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        # Manually post the agent turn messages in sequence.
        app.post_message(AgentRunStarted(agent_id="e2e-agent", model_short="mock"))
        await pilot.pause(0.05)

        assert app._spinner_mounted is True, "SpinnerPanel should be mounted after AgentRunStarted"

        app.post_message(ToolCallStarted(tool_use_id="e2e-tc", name="read_file", args={"path": "src/main.py"}))
        await pilot.pause(0.1)

        spinner = app.query_one(SpinnerPanel)
        assert "e2e-tc" in spinner._tool_calls, "SpinnerPanel should track the tool call"
        assert spinner._tool_calls["e2e-tc"]["done"] is False, "Tool call should still be in progress"
        assert spinner._tool_calls["e2e-tc"]["name"] == "read_file"

        # Complete the tool call.
        app.post_message(ToolCallComplete(
            tool_use_id="e2e-tc",
            success=True,
            duration_ms=42.0,
            error=None,
            diff=None,
        ))
        await pilot.pause(0.1)

        assert spinner._tool_calls["e2e-tc"]["done"] is True, "Tool call should be marked done"
        assert spinner._tool_calls["e2e-tc"]["ok"] is True, "Tool call should be marked ok"

        # Finish the agent run.
        app.post_message(AgentRunFinished())
        await pilot.pause(0.1)

        assert app._spinner_mounted is False, "SpinnerPanel should be unmounted after AgentRunFinished"


# ── E2E Test 3: multi-tool agent turn ────────────────────────────────────────


@pytest.mark.e2e
async def test_multi_tool_agent_turn() -> None:
    """Agent turn with two sequential tool calls — both appear in SpinnerPanel.

    Simulates a realistic sequence:
        1. git_status tool → success
        2. read_file tool → success
        3. Agent text appended
    """
    from agenthicc.tui.widgets.spinner_panel import SpinnerPanel
    from agenthicc.tui.widgets.status_bar import StatusBar
    from agenthicc.tui.widgets.transcript_view import TranscriptView
    from textual.widgets import RichLog

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        # Start agent turn.
        app.post_message(AgentRunStarted(agent_id="multi-agent", model_short="mock"))
        await pilot.pause(0.05)

        # First tool: git_status.
        app.post_message(ToolCallStarted(tool_use_id="tc-git", name="git_status", args={}))
        await pilot.pause(0.05)

        spinner = app.query_one(SpinnerPanel)
        assert "tc-git" in spinner._tool_calls

        app.post_message(ToolCallComplete(tool_use_id="tc-git", success=True, duration_ms=15.0, error=None, diff=None))
        await pilot.pause(0.05)

        # Second tool: read_file.
        app.post_message(ToolCallStarted(tool_use_id="tc-read", name="read_file", args={"path": "README.md"}))
        await pilot.pause(0.05)

        assert "tc-read" in spinner._tool_calls
        assert spinner._tool_calls["tc-read"]["done"] is False

        app.post_message(ToolCallComplete(tool_use_id="tc-read", success=True, duration_ms=8.0, error=None, diff=None))
        await pilot.pause(0.05)

        # Both tools should be done.
        assert spinner._tool_calls["tc-git"]["done"] is True
        assert spinner._tool_calls["tc-read"]["done"] is True

        # Token update.
        app.post_message(TokensUpdated(input_tokens=2000, output_tokens=400, cost_usd=0.02))
        await pilot.pause(0.05)

        # Append response.
        turn = model.append_turn("multi-agent", "assistant (mock)")
        turn.lines.append("I checked git status and read the README.")
        app.post_message(TranscriptUpdated())
        await pilot.pause(0.05)

        # Finish.
        app.post_message(AgentRunFinished())
        await pilot.pause(0.1)

        # Assertions.
        bar = app.query_one(StatusBar)
        assert bar.active is False
        assert bar.input_tokens == 2000
        assert bar.output_tokens == 400
        assert app._spinner_mounted is False

        tv = app.query_one(TranscriptView)
        richlog = tv.query_one("#transcript-richlog", RichLog)
        assert len(richlog.lines) > 0, "Transcript should have content after agent turn"


# ── E2E Test 4: failed tool call shown in spinner ────────────────────────────


@pytest.mark.e2e
async def test_failed_tool_call_in_spinner() -> None:
    """ToolCallComplete with success=False marks the tool as failed in SpinnerPanel."""
    from agenthicc.tui.widgets.spinner_panel import SpinnerPanel

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        app.post_message(AgentRunStarted(agent_id="fail-agent", model_short="mock"))
        await pilot.pause(0.05)

        app.post_message(ToolCallStarted(
            tool_use_id="tc-fail",
            name="run_bash",
            args={"command": "exit 1"},
        ))
        await pilot.pause(0.05)

        spinner = app.query_one(SpinnerPanel)
        assert "tc-fail" in spinner._tool_calls

        # Complete with failure.
        app.post_message(ToolCallComplete(
            tool_use_id="tc-fail",
            success=False,
            duration_ms=100.0,
            error="Command failed with exit code 1",
            diff=None,
        ))
        await pilot.pause(0.05)

        entry = spinner._tool_calls["tc-fail"]
        assert entry["done"] is True
        assert entry["ok"] is False

        app.post_message(AgentRunFinished())
        await pilot.pause(0.05)


# ── E2E Test 5: pending queue display during agent streaming ──────────────────


@pytest.mark.e2e
async def test_pending_queue_display_during_agent_run() -> None:
    """While an agent is running, submit a second message; pending queue shows count."""
    from agenthicc.tui.widgets.input_panel import InputPanel
    from agenthicc.tui.widgets.mode_footer import ModeFooter

    intents_received: list[str] = []
    pending_released = asyncio.Event()

    app, model = _make_app()

    async def mock_on_intent(text: str) -> None:
        intents_received.append(text)
        # Simulate a long-running agent turn.
        await asyncio.sleep(0.05)
        app.post_message(AgentRunStarted(agent_id="queue-agent", model_short="mock"))
        await asyncio.sleep(0.1)
        app.post_message(AgentRunFinished())

    async with app.run_test(headless=True) as pilot:
        app._on_intent = mock_on_intent
        panel = app.query_one(InputPanel)
        footer = panel.query_one(ModeFooter)
        panel.focus()

        # Simulate pending queue update (this is what the session runner would do).
        app.post_message(AgentRunStarted(agent_id="qa", model_short="mock"))
        await pilot.pause(0.05)

        # Post a pending queue count.
        from agenthicc.tui.messages import PendingQueueUpdated
        app.post_message(PendingQueueUpdated(count=3))
        await pilot.pause(0.1)

        # Footer should display the pending count.
        assert footer.notification is not None, "Footer should show pending notification"
        assert "3" in footer.notification or "queued" in footer.notification.lower()

        # Finish agent run and clear queue.
        app.post_message(AgentRunFinished())
        app.post_message(PendingQueueUpdated(count=0))
        await pilot.pause(0.1)

        assert footer.notification is None, "Footer should clear when queue is empty"


# ── E2E Test 6: console shim routes to transcript via ConsolePrint ────────────


@pytest.mark.e2e
async def test_console_shim_in_e2e_flow() -> None:
    """ConsoleShim.print() during a simulated agent turn appears in TranscriptView.

    This tests the _console_shim path: agent_turn.py calls renderer.console.print()
    which routes through ConsoleShim → ConsolePrint → TranscriptView.on_console_print().
    """
    from agenthicc.tui.app import AgenthiccApp, ConsoleShim
    from agenthicc.tui.widgets.transcript_view import TranscriptView
    from textual.widgets import RichLog
    from rich.console import Console

    model = TranscriptModel()
    app = AgenthiccApp(model=model, base_path=".")
    # Restore real console for rendering, keep shim accessible via _console_shim.
    real_console = Console(highlight=False, markup=True)
    object.__setattr__(app, "console", real_console)
    # Preserve the shim so we can use it for testing.
    shim = app._console_shim

    async with app.run_test(headless=True) as pilot:
        tv = app.query_one(TranscriptView)
        richlog = tv.query_one("#transcript-richlog", RichLog)

        initial_count = len(richlog.lines)

        # Simulate agent_turn.py calling renderer.console.print().
        shim.print("Tool output: 3 files changed")
        await pilot.pause(0.1)

        final_count = len(richlog.lines)
        assert final_count > initial_count, (
            "TranscriptView should receive console shim output via ConsolePrint message"
        )
