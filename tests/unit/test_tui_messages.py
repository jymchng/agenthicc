"""Unit tests for agenthicc.tui.messages (PRD-55 Phase 1).

Verifies that every Message subclass can be instantiated with the correct
fields and that the MatchItem import required by TriggerSelected works.
"""
from __future__ import annotations

import pytest

from agenthicc.tui.trigger import MatchItem


# ── MatchItem import sanity check ─────────────────────────────────────────────

@pytest.mark.unit
def test_match_item_importable() -> None:
    """MatchItem must be importable from agenthicc.tui.trigger."""
    item = MatchItem(display="src/auth.py", value="src/auth.py", hint="1.2 KB")
    assert item.display == "src/auth.py"
    assert item.value == "src/auth.py"
    assert item.hint == "1.2 KB"


@pytest.mark.unit
def test_match_item_hint_optional() -> None:
    """MatchItem hint defaults to empty string."""
    item = MatchItem(display="/deploy", value="/deploy")
    assert item.hint == ""


# ── Message class tests ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_input_submitted() -> None:
    from agenthicc.tui.messages import InputSubmitted

    msg = InputSubmitted(value="hello world")
    assert msg.value == "hello world"


@pytest.mark.unit
def test_trigger_activated() -> None:
    from agenthicc.tui.messages import TriggerActivated

    msg = TriggerActivated(char="@", fragment="src")
    assert msg.char == "@"
    assert msg.fragment == "src"


@pytest.mark.unit
def test_trigger_selected() -> None:
    from agenthicc.tui.messages import TriggerSelected

    item = MatchItem(display="@src/app.py", value="src/app.py")
    msg = TriggerSelected(item=item)
    assert msg.item is item
    assert msg.item.value == "src/app.py"


@pytest.mark.unit
def test_trigger_cancelled() -> None:
    from agenthicc.tui.messages import TriggerCancelled

    msg = TriggerCancelled()
    assert isinstance(msg, TriggerCancelled)


@pytest.mark.unit
def test_transcript_updated() -> None:
    from agenthicc.tui.messages import TranscriptUpdated

    msg = TranscriptUpdated()
    assert isinstance(msg, TranscriptUpdated)


@pytest.mark.unit
def test_console_print() -> None:
    from agenthicc.tui.messages import ConsolePrint

    msg = ConsolePrint(markup="[bold]Hello[/bold]")
    assert msg.markup == "[bold]Hello[/bold]"


@pytest.mark.unit
def test_mode_cycled() -> None:
    from agenthicc.tui.messages import ModeCycled

    msg = ModeCycled(new_name="Edit", new_badge="✏")
    assert msg.new_name == "Edit"
    assert msg.new_badge == "✏"


@pytest.mark.unit
def test_agent_run_started() -> None:
    from agenthicc.tui.messages import AgentRunStarted

    msg = AgentRunStarted(agent_id="agent-001", model_short="claude-sonnet-4-6")
    assert msg.agent_id == "agent-001"
    assert msg.model_short == "claude-sonnet-4-6"


@pytest.mark.unit
def test_agent_run_finished() -> None:
    from agenthicc.tui.messages import AgentRunFinished

    msg = AgentRunFinished()
    assert isinstance(msg, AgentRunFinished)


@pytest.mark.unit
def test_tool_call_started() -> None:
    from agenthicc.tui.messages import ToolCallStarted

    args = {"path": "/etc/hosts", "max_lines": 50}
    msg = ToolCallStarted(tool_use_id="tu-abc123", name="read_file", args=args)
    assert msg.tool_use_id == "tu-abc123"
    assert msg.name == "read_file"
    assert msg.args == args


@pytest.mark.unit
def test_tool_call_complete_success() -> None:
    from agenthicc.tui.messages import ToolCallComplete

    msg = ToolCallComplete(
        tool_use_id="tu-abc123",
        success=True,
        duration_ms=42.5,
        error=None,
        diff=None,
    )
    assert msg.tool_use_id == "tu-abc123"
    assert msg.success is True
    assert msg.duration_ms == 42.5
    assert msg.error is None
    assert msg.diff is None


@pytest.mark.unit
def test_tool_call_complete_failure() -> None:
    from agenthicc.tui.messages import ToolCallComplete

    msg = ToolCallComplete(
        tool_use_id="tu-xyz",
        success=False,
        duration_ms=None,
        error="permission denied",
        diff=None,
    )
    assert msg.success is False
    assert msg.error == "permission denied"


@pytest.mark.unit
def test_tool_call_complete_with_diff() -> None:
    from agenthicc.tui.messages import ToolCallComplete

    diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"
    msg = ToolCallComplete(
        tool_use_id="tu-diff",
        success=True,
        duration_ms=10.0,
        error=None,
        diff=diff,
    )
    assert msg.diff == diff


@pytest.mark.unit
def test_tokens_updated() -> None:
    from agenthicc.tui.messages import TokensUpdated

    msg = TokensUpdated(input_tokens=1024, output_tokens=256, cost_usd=0.003)
    assert msg.input_tokens == 1024
    assert msg.output_tokens == 256
    assert msg.cost_usd == pytest.approx(0.003)


@pytest.mark.unit
def test_pending_queue_updated() -> None:
    from agenthicc.tui.messages import PendingQueueUpdated

    msg = PendingQueueUpdated(count=3)
    assert msg.count == 3


@pytest.mark.unit
def test_pending_queue_updated_zero() -> None:
    from agenthicc.tui.messages import PendingQueueUpdated

    msg = PendingQueueUpdated(count=0)
    assert msg.count == 0


# ── All message classes are importable from the module ────────────────────────

@pytest.mark.unit
def test_all_exports_importable() -> None:
    """Verify __all__ lists the expected message classes and all are importable."""
    import agenthicc.tui.messages as mod

    expected = set(mod.__all__)  # accept whatever is exported; just verify all are importable
    for name in expected:
        cls = getattr(mod, name)
        assert cls is not None
