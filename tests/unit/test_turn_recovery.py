"""Tests for TUI turn-recovery invariants (PRD-107).

Verifies that:
- close_turn() is idempotent and always returns agent_state to IDLE
- fail_turn() / end_turn() are backward-compatible wrappers
- Error messages always include the exception class name
- No duplicate error events are emitted on a single failure
"""
from __future__ import annotations

import pytest

from agenthicc.tui.conversation_store import AgentState, ConversationStore
from agenthicc.runners.tui_session import _fmt_exc


# ── ConversationStore.close_turn() ───────────────────────────────────────────

@pytest.mark.unit
def test_close_turn_success_returns_idle() -> None:
    store = ConversationStore()
    store.begin_turn("assistant")
    assert store.agent_state() == AgentState.THINKING

    store.close_turn()

    assert store.agent_state() == AgentState.IDLE
    assert store.active_tool() == ""
    assert store._current_turn is None


@pytest.mark.unit
def test_close_turn_with_error_still_returns_idle() -> None:
    """Even on error, agent_state must end at IDLE — the core invariant."""
    store = ConversationStore()
    store.begin_turn("assistant")

    store.close_turn(error="ReadTimeout: connection timed out")

    assert store.agent_state() == AgentState.IDLE
    assert store._current_turn is None


@pytest.mark.unit
def test_close_turn_is_idempotent() -> None:
    """Calling close_turn() multiple times must not crash or leave bad state."""
    store = ConversationStore()
    store.begin_turn("assistant")

    store.close_turn()           # first call — normal close
    store.close_turn()           # second call — no-op
    store.close_turn(error="x")  # third call — error on already-closed turn

    assert store.agent_state() == AgentState.IDLE
    assert store._current_turn is None


@pytest.mark.unit
def test_close_turn_no_turn_active_is_safe() -> None:
    """close_turn() with no active turn must be a no-op."""
    store = ConversationStore()
    assert not store.is_turn_active

    store.close_turn()
    store.close_turn(error="boom")

    assert store.agent_state() == AgentState.IDLE


@pytest.mark.unit
def test_close_turn_error_emits_exactly_one_error_event() -> None:
    store = ConversationStore()
    events: list = []
    store.on_event(events.append)

    store.begin_turn("assistant")
    store.close_turn(error="IOError: pipe broken")

    error_events = [e for e in events if e.kind == "error"]
    assert len(error_events) == 1
    assert "IOError" in error_events[0].payload["message"]
    assert "pipe broken" in error_events[0].payload["message"]


@pytest.mark.unit
def test_close_turn_success_emits_turn_complete() -> None:
    store = ConversationStore()
    events: list = []
    store.on_event(events.append)

    store.begin_turn("assistant")
    store.close_turn()

    assert any(e.kind == "turn_complete" for e in events)
    assert not any(e.kind == "error" for e in events)


@pytest.mark.unit
def test_close_turn_no_turn_emits_nothing() -> None:
    store = ConversationStore()
    events: list = []
    store.on_event(events.append)

    store.close_turn()
    store.close_turn(error="irrelevant")

    # No turn was open so no events should be emitted
    assert events == []


# ── Backward-compat wrappers ──────────────────────────────────────────────────

@pytest.mark.unit
def test_fail_turn_returns_idle() -> None:
    """fail_turn() must now end at IDLE (was ERROR — the bug this PRD fixes)."""
    store = ConversationStore()
    store.begin_turn("assistant")

    store.fail_turn("something went wrong")

    assert store.agent_state() == AgentState.IDLE


@pytest.mark.unit
def test_end_turn_returns_idle() -> None:
    store = ConversationStore()
    store.begin_turn("assistant")

    store.end_turn()

    assert store.agent_state() == AgentState.IDLE


@pytest.mark.unit
def test_double_fail_does_not_corrupt_state() -> None:
    """Simulates the old double-fail_turn bug; new code must survive it."""
    store = ConversationStore()
    store.begin_turn("assistant")

    store.fail_turn("first error")   # was: agent_state = ERROR
    store.fail_turn("second error")  # was: still ERROR, appended duplicate event

    # After the fix both calls go through close_turn(); second is a no-op
    assert store.agent_state() == AgentState.IDLE


# ── is_turn_active property ───────────────────────────────────────────────────

@pytest.mark.unit
def test_is_turn_active_lifecycle() -> None:
    store = ConversationStore()

    assert not store.is_turn_active

    store.begin_turn("assistant")
    assert store.is_turn_active

    store.close_turn()
    assert not store.is_turn_active


# ── _fmt_exc helper ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_fmt_exc_includes_class_name() -> None:
    exc = ValueError("bad argument")
    result = _fmt_exc(exc)
    assert result.startswith("ValueError:")
    assert "bad argument" in result


@pytest.mark.unit
def test_fmt_exc_timeout() -> None:
    exc = TimeoutError("read timed out")
    result = _fmt_exc(exc)
    assert "TimeoutError" in result
    assert "read timed out" in result


@pytest.mark.unit
def test_fmt_exc_empty_message() -> None:
    exc = RuntimeError()
    result = _fmt_exc(exc)
    assert result == "RuntimeError"


@pytest.mark.unit
def test_fmt_exc_custom_exception() -> None:
    class ReadTimeout(OSError):
        pass

    exc = ReadTimeout("HTTPSConnectionPool: Read timed out")
    result = _fmt_exc(exc)
    assert result.startswith("ReadTimeout:")
    assert "HTTPSConnectionPool" in result


@pytest.mark.unit
def test_fmt_exc_never_returns_bare_str() -> None:
    """Regression: str(exc) alone omits the class name."""
    exc = ConnectionError("Network unreachable")
    result = _fmt_exc(exc)
    # Must NOT be just "Network unreachable" — must include the type
    assert result != "Network unreachable"
    assert "ConnectionError" in result


# ── active_tool cleared on close ─────────────────────────────────────────────

@pytest.mark.unit
def test_active_tool_cleared_by_close_turn() -> None:
    store = ConversationStore()
    store.begin_turn("assistant")
    store.set_tool("write_file")
    assert store.active_tool() == "write_file"

    store.close_turn(error="IOError: disk full")

    assert store.active_tool() == ""
    assert store.agent_state() == AgentState.IDLE


@pytest.mark.unit
def test_active_tool_cleared_on_success() -> None:
    store = ConversationStore()
    store.begin_turn("assistant")
    store.set_tool("read_file")

    store.close_turn()

    assert store.active_tool() == ""
