"""E2E tests: an agent turn really calling the agenthicc inspection tools.

Nothing is stubbed on the turn path — a ``MockTransport`` returns real
``tool_use`` completions, the agent-turn runner resolves and executes the tools
from the built-in registry, and the results land in the conversation store. This
is what proves the tools are reachable by a model, not merely importable.

NOTE: no ``from __future__ import annotations`` — the ``@tool()`` decorator
inspects annotations at decoration time.
"""

import asyncio

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.e2e


# ── transport plumbing ────────────────────────────────────────────────────────


def _text(index, content):
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _tool_use(index, name, payload):
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content="",
        tool_calls=[ToolCall(tool_use_id=f"tc-{index}", name=name, input=payload)],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _script(*steps):
    """Queue tool_use / text completions onto a MockTransport."""
    mock = MockTransport()
    for index, step in enumerate(steps):
        if isinstance(step, tuple):
            mock.queue_response(_tool_use(index, step[0], step[1]))
        else:
            mock.queue_response(_text(index, str(step)))
    return mock


@pytest.fixture
def app_state():
    state = TUIAppState.create()
    registry = build_default_registry()
    yolo = registry.get("Yolo")
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


async def _turn(app_state, processor, mock, text="Tell me how agenthicc works."):
    """Run one real agent turn and return the collected assistant output."""
    from unittest.mock import MagicMock

    cfg = AgenthiccConfig()
    collected = []
    await _run_agent_turn(
        text,
        runner=AgentRunnerBase(transport=mock, signals=SignalBus()),
        processor=processor,
        session_memory=ShortTermMemory(max_tokens=cfg.execution.effective_usable_budget()),
        max_agent_turns=12,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills={},
        skill_permissions=cfg.agents.skill_permissions_for("auto"),
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=collected,
        system_prompt_suffix="",
    )
    return collected


def _last_exchange(mock):
    """Return the message history the model saw on its final round-trip."""
    assert mock.calls, "the transport was never called"
    return repr(mock.calls[-1].messages)


# ── the tools are reachable from a real turn ──────────────────────────────────


async def test_e2e_agent_lists_the_documentation(app_state, processor):
    mock = _script(
        ("list_agenthicc_docs", {}),
        "agenthicc ships guides and a reference tree.",
    )
    output = await _turn(app_state, processor, mock)
    await processor.drain()

    # The real document index came back to the model as a tool result.
    seen = _last_exchange(mock)
    assert "guides/workflows.md" in seen
    assert "llms-full.txt" in seen
    assert output


async def test_e2e_agent_reads_a_guide_then_pages_through_it(app_state, processor):
    mock = _script(
        ("read_agenthicc_doc", {"path": "guides/workflows.md", "start_line": 1, "max_lines": 5}),
        ("read_agenthicc_doc", {"path": "guides/workflows.md", "start_line": 6, "max_lines": 5}),
        "A workflow is a Python plugin defining named agent phases.",
    )
    output = await _turn(app_state, processor, mock, "What is a workflow in agenthicc?")
    await processor.drain()
    assert output
    assert len(mock.calls) >= 3

    # Both windows of the real guide reached the model.
    seen = _last_exchange(mock)
    assert "# Workflows" in seen
    assert "next_start_line" in seen


async def test_e2e_agent_searches_the_documentation(app_state, processor):
    mock = _script(
        ("search_agenthicc_docs", {"query": "capability gate", "max_results": 5}),
        "The capability gate is documented in the tools guide.",
    )
    output = await _turn(app_state, processor, mock, "Where is the capability gate documented?")
    await processor.drain()
    assert output
    assert "guides/tools.md" in _last_exchange(mock)


async def test_e2e_agent_inspects_a_real_symbol(app_state, processor):
    mock = _script(
        (
            "inspect_agenthicc_source",
            {"target": "agenthicc.workflows.plugin:PhaseSpec", "include_source": False},
        ),
        ("inspect_agenthicc_source", {"target": "agenthicc.kernel.reducer:root_reducer"}),
        "PhaseSpec is a frozen dataclass; root_reducer is a pure function.",
    )
    output = await _turn(app_state, processor, mock, "What fields does PhaseSpec have?")
    await processor.drain()
    assert output
    assert len(mock.calls) >= 3

    seen = _last_exchange(mock)
    assert "agenthicc/workflows/plugin.py" in seen
    assert "max_turns" in seen  # a real PhaseSpec field, from the live dataclass
    assert "def root_reducer" in seen  # real source text


async def test_e2e_agent_searches_then_inspects(app_state, processor):
    """The intended two-step flow: locate a symbol, then read its definition."""
    mock = _script(
        ("search_agenthicc_source", {"query": "class ToolCapability", "max_results": 3}),
        (
            "inspect_agenthicc_source",
            {"target": "agenthicc.tools.capabilities:ToolCapability"},
        ),
        "ToolCapability is a str Enum with seven members.",
    )
    output = await _turn(app_state, processor, mock, "What capabilities can a tool declare?")
    await processor.drain()
    assert output
    assert len(mock.calls) >= 3

    seen = _last_exchange(mock)
    assert "agenthicc/tools/capabilities.py" in seen
    assert "GIT_WRITE" in seen  # from the real enum body


async def test_e2e_a_bad_target_is_reported_and_the_turn_continues(app_state, processor):
    """A refusal comes back as a normal tool result, not an exception."""
    mock = _script(
        ("inspect_agenthicc_source", {"target": "os.path"}),
        ("inspect_agenthicc_source", {"target": "agenthicc.workflows.base_runner"}),
        "Only agenthicc modules are inspectable; here is BaseWorkflowRunner.",
    )
    output = await _turn(app_state, processor, mock, "Show me os.path")
    await processor.drain()
    assert output
    assert len(mock.calls) >= 3

    seen = _last_exchange(mock)
    assert "not part of the agenthicc package" in seen
    assert "class BaseWorkflowRunner" in seen  # the retry succeeded


async def test_e2e_tools_work_in_plan_mode(app_state, processor):
    """Plan mode blocks writes and execution; reading agenthicc must still work."""
    registry = build_default_registry()
    plan = registry.get("Plan")
    assert plan is not None
    app_state.active_mode.set(plan)

    mock = _script(
        ("search_agenthicc_docs", {"query": "PhaseSpec", "max_results": 3}),
        (
            "inspect_agenthicc_source",
            {"target": "agenthicc.workflows.plugin:PhaseSpec", "include_source": False},
        ),
        "Read both without leaving Plan mode.",
    )
    output = await _turn(app_state, processor, mock, "Plan a workflow change.")
    await processor.drain()
    assert output
    assert len(mock.calls) >= 3

    seen = _last_exchange(mock)
    assert "which is blocked in Plan mode" not in seen  # the gate never fired
    assert "agenthicc/workflows/plugin.py" in seen  # source read through
    assert "match_count" in seen  # doc search ran through


async def test_e2e_the_request_advertises_the_tools_to_the_model(app_state, processor):
    """The model can only call these if the request actually offers them."""
    mock = _script("Nothing to do.")
    await _turn(app_state, processor, mock)
    await processor.drain()

    call = mock.calls[0]
    assert "list_agenthicc_docs" in str(call.system)
    assert "inspect_agenthicc_source" in str(call.system)
    assert "agenthicc Docs & Source" in str(call.system)

    offered = {
        name
        for tool in (call.tools or [])
        if (name := (tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")))
    }
    assert {
        "list_agenthicc_docs",
        "read_agenthicc_doc",
        "search_agenthicc_docs",
        "inspect_agenthicc_source",
        "search_agenthicc_source",
    } <= offered
