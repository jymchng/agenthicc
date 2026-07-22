"""E2E tests: PRD-83 — token tracking architecture revamp.

Verifies the two-source model:
  (1) chunk.usage → conv_store.add_tokens()   (live, per sub-turn)
  (2) AgentRunComplete → conv_store.set_tokens()  (reconciliation, once per run)

No _got_usage_from_chunk flag; no ModelCallComplete handler in _run_agent_turn.

NOTE: no ``from __future__ import annotations`` — @tool() inspects annotations
at decoration time.
"""

import asyncio

import pytest

from lauren_ai._agents import agent
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import AgentRunComplete, SignalBus
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState

pytestmark = pytest.mark.e2e


# ── helpers ───────────────────────────────────────────────────────────────────


def _completion(content: str, usage: TokenUsage | None, n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=usage,
    )


def _make_runner(mock: MockTransport) -> AgentRunnerBase:
    bus = SignalBus()

    @agent(model="mock-model", system="Test agent.")
    class _Agent: ...

    instance = _Agent()
    _build_runner_for_agent(instance, mock, signals=bus)
    return AgentRunnerBase(transport=mock, signals=bus)


async def _run_turn(runner, conv_store, tmp_path):
    """Run _run_agent_turn with minimal wiring."""
    from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: PLC0415

    k_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=k_state, persist=False)
    t = asyncio.create_task(proc.run())
    try:
        await _run_agent_turn(
            "test prompt",
            runner,
            proc,
            conv_store=conv_store,
            app_state=None,
        )
    finally:
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)


# ── live path: chunk.usage ────────────────────────────────────────────────────


async def test_tokens_updated_before_text_event(tmp_path):
    """chunk.usage fires add_tokens before the text event is published."""
    tui = TUIAppState.create()
    conv = tui.conversation
    snapshot: dict = {}

    def _on_event(ev):
        if ev.kind == "text":
            snapshot["in"] = conv.tokens_in()
            snapshot["out"] = conv.tokens_out()

    conv.on_event(_on_event)

    usage = TokenUsage(input_tokens=42, output_tokens=17)
    mock = MockTransport()
    mock.queue_response(_completion("Hello.", usage))
    runner = _make_runner(mock)

    await _run_turn(runner, conv, tmp_path)

    assert snapshot["in"] == 42
    assert snapshot["out"] == 17


async def test_single_turn_correct_counts(tmp_path):
    """Token counts match the MockTransport usage after one turn."""
    tui = TUIAppState.create()
    conv = tui.conversation

    usage = TokenUsage(input_tokens=100, output_tokens=50)
    mock = MockTransport()
    mock.queue_response(_completion("Done.", usage))
    runner = _make_runner(mock)

    await _run_turn(runner, conv, tmp_path)

    assert conv.tokens_in() == 100
    assert conv.tokens_out() == 50


async def test_no_modelcallcomplete_handler_registered(tmp_path):
    """_run_agent_turn must NOT register a ModelCallComplete handler on the bus."""
    from lauren_ai._signals import ModelCallComplete  # noqa: PLC0415

    tui = TUIAppState.create()
    conv = tui.conversation

    mock = MockTransport()
    usage = TokenUsage(input_tokens=10, output_tokens=5)
    mock.queue_response(_completion("Hi.", usage))
    runner = _make_runner(mock)

    handlers_before = len(runner._signals._handlers.get(ModelCallComplete, []))
    await _run_turn(runner, conv, tmp_path)
    handlers_after = len(runner._signals._handlers.get(ModelCallComplete, []))

    assert handlers_after == handlers_before, (
        f"_run_agent_turn added {handlers_after - handlers_before} "
        "ModelCallComplete handler(s) to the shared bus"
    )


async def test_no_handler_accumulation_across_turns(tmp_path):
    """Running N turns must not grow the ModelCallComplete handler list."""
    from lauren_ai._signals import ModelCallComplete  # noqa: PLC0415

    tui = TUIAppState.create()
    conv = tui.conversation

    # Reuse the same runner across turns (as tui_session.py does)
    mock = MockTransport()
    runner = _make_runner(mock)

    for i in range(4):
        mock.queue_response(_completion(f"Turn {i}.", TokenUsage(10, 5), n=i))
        await _run_turn(runner, conv, tmp_path)

    count = len(runner._signals._handlers.get(ModelCallComplete, []))
    assert count == 0, f"Expected 0 handlers, got {count}"


async def test_add_tokens_called_once_per_turn(tmp_path):
    """add_tokens called exactly once per turn (from chunk.usage only)."""
    tui = TUIAppState.create()
    conv = tui.conversation
    calls: list[tuple] = []
    _orig = conv.add_tokens

    def _spy(inp, out, cost):
        calls.append((inp, out))
        _orig(inp, out, cost)

    conv.add_tokens = _spy

    usage = TokenUsage(input_tokens=30, output_tokens=15)
    mock = MockTransport()
    mock.queue_response(_completion("Result.", usage))
    runner = _make_runner(mock)

    await _run_turn(runner, conv, tmp_path)

    assert len(calls) == 1
    assert calls[0] == (30, 15)


# ── reconciliation path: AgentRunComplete ────────────────────────────────────


async def test_set_tokens_called_by_reconciliation_handler(tmp_path):
    """The AgentRunComplete handler calls set_tokens with the run total."""
    tui = TUIAppState.create()
    conv = tui.conversation
    set_calls: list[tuple] = []
    _orig = conv.set_tokens

    def _spy(inp, out, cost):
        set_calls.append((inp, out, cost))
        _orig(inp, out, cost)

    conv.set_tokens = _spy

    # Register the reconciliation handler exactly as tui_session.py does
    bus = SignalBus()

    @bus.on(AgentRunComplete)
    async def _on_arc(sig):
        usage = getattr(sig, "total_usage", None)
        cost = float(getattr(sig, "total_cost_usd", 0.0) or 0.0)
        if usage is not None:
            conv.set_tokens(
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
                cost,
            )

    @agent(model="mock-model", system="Test.")
    class _A: ...

    mock = MockTransport()
    usage = TokenUsage(input_tokens=77, output_tokens=33)
    mock.queue_response(_completion("Answer.", usage))
    instance = _A()
    _build_runner_for_agent(instance, mock, signals=bus)
    runner = AgentRunnerBase(transport=mock, signals=bus)

    await _run_turn(runner, conv, tmp_path)

    assert len(set_calls) >= 1
    last = set_calls[-1]
    assert last[0] == 77
    assert last[1] == 33


async def test_reconciliation_is_noop_when_chunk_usage_populated(tmp_path):
    """set_tokens is a no-op when chunk.usage already set the same values."""
    tui = TUIAppState.create()
    conv = tui.conversation

    redraw_count = [0]
    conv.tokens_in.subscribe(lambda: redraw_count.__setitem__(0, redraw_count[0] + 1))

    # Wire the reconciliation handler
    bus = SignalBus()

    @bus.on(AgentRunComplete)
    async def _on_arc(sig):
        usage = getattr(sig, "total_usage", None)
        cost = float(getattr(sig, "total_cost_usd", 0.0) or 0.0)
        if usage is not None:
            conv.set_tokens(
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
                cost,
            )

    @agent(model="mock-model", system="Test.")
    class _A: ...

    mock = MockTransport()
    usage = TokenUsage(input_tokens=55, output_tokens=22)
    mock.queue_response(_completion("Answer.", usage))
    instance = _A()
    _build_runner_for_agent(instance, mock, signals=bus)
    runner = AgentRunnerBase(transport=mock, signals=bus)

    await _run_turn(runner, conv, tmp_path)

    # The live path (chunk.usage) fires one redraw.
    # The reconciliation path (AgentRunComplete) should fire zero extra
    # redraws because set_tokens(55, 22, ...) == current value → no-op.
    assert conv.tokens_in() == 55
    assert conv.tokens_out() == 22
    # Exactly 1 redraw from the live add_tokens call; reconciliation is no-op
    assert redraw_count[0] == 1


# ── fallback: no chunk.usage ──────────────────────────────────────────────────


async def test_fallback_when_chunk_usage_is_none(tmp_path):
    """When chunk.usage is None, tokens stay zero (honest zero, not bug)."""
    tui = TUIAppState.create()
    conv = tui.conversation
    add_calls: list = []
    _orig = conv.add_tokens

    def _spy(inp, out, cost):
        add_calls.append((inp, out))
        _orig(inp, out, cost)

    conv.add_tokens = _spy

    mock = MockTransport()
    mock.queue_response(_completion("Response.", usage=None))
    runner = _make_runner(mock)

    await _run_turn(runner, conv, tmp_path)

    # add_tokens must NOT be called from the chunk loop (usage was None)
    assert len(add_calls) == 0
    # Tokens stay at 0 — correct, provider gave no data
    assert conv.tokens_in() == 0
    assert conv.tokens_out() == 0


# ── multi-turn accumulation ───────────────────────────────────────────────────


async def test_multi_turn_accumulates_correctly(tmp_path):
    """Counts accumulate across two turns; no double-counting."""
    tui = TUIAppState.create()
    conv = tui.conversation
    add_calls: list[tuple] = []
    _orig = conv.add_tokens

    def _spy(inp, out, cost):
        add_calls.append((inp, out))
        _orig(inp, out, cost)

    conv.add_tokens = _spy

    for inp, out, n in [(80, 20, 1), (90, 30, 2)]:
        mock = MockTransport()
        mock.queue_response(_completion(f"Turn {n}.", TokenUsage(inp, out), n=n))
        runner = _make_runner(mock)
        await _run_turn(runner, conv, tmp_path)

    assert len(add_calls) == 2
    assert add_calls[0] == (80, 20)
    assert add_calls[1] == (90, 30)
    assert conv.tokens_in() == 170
    assert conv.tokens_out() == 50
