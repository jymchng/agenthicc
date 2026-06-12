---
id: PRD-08
title: "Testing Strategy -- Unit, Integration and End-to-End"
status: draft
created: 2025-06-12
authors: [agenthicc-team]
tags: [testing, quality, ci, pytest, coverage]
---

# PRD-08: Testing Strategy — Unit, Integration and End-to-End

## 1. Executive Summary

This document is the **testing bible** for the Agenthicc multi-agent orchestration system. It specifies every test file, every fixture, every CI gate, and every coverage threshold required before shipping to production. The strategy follows the classic test pyramid: a wide base of fast, isolated unit tests; a middle tier of integration tests that wire real subsystems together via in-memory fakes; and a narrow apex of end-to-end tests that exercise full user scenarios through either a headless HTTP/WebSocket client or a PTY-driven TUI session.

The document provides complete, runnable pytest source for all test modules. No placeholder comments — every test is real code.

---

## 2. Testing Philosophy and Principles

1. **Tests are executable specifications.** Each test encodes a requirement. If the requirement changes, the test changes first.
2. **No mocks for things you own.** Internal seams (AppState, DAG executor, EventBus) are tested via real objects with in-memory fakes at the I/O boundary only. External I/O (LLM transport, filesystem, network) is always faked.
3. **Determinism is non-negotiable.** Random seeds, fixed clocks, scripted LLM responses. A flaky test is a bug of higher priority than a product bug.
4. **Property-based tests at the unit layer.** Hypothesis drives reducer and algorithm tests; example-based tests prove concrete scenarios.
5. **Async-first.** The runtime is async throughout. All tests use `pytest-asyncio` in `auto` mode. Synchronous helpers are fine; synchronous tests are not.
6. **Isolation by layer.** Unit tests import nothing outside their module under test. Integration tests may import two or more subsystems but never real network or disk I/O. E2E tests own the entire process boundary.
7. **CI is the canonical test runner.** Local runs use the same Nox sessions as CI. `nox -s unit` must pass before any commit; `nox -s integration` before any PR merge; `nox -s e2e` before any release tag.

---

## 3. Test Pyramid

```
         /\
        /e2e\          ~20 scenarios   (tests/e2e/)
       /------\
      /        \
     /integration\     ~60 test cases  (tests/integration/)
    /--------------\
   /                \
  /    unit tests    \  ~200 test cases (tests/unit/)
 /____________________\
```

| Layer | Directory | Target Count | Max Runtime | Isolation |
|---|---|---|---|---|
| Unit | `tests/unit/` | 200 | 30 s | Module + in-memory fakes |
| Integration | `tests/integration/` | 60 | 3 min | Subsystem + AsyncMockTransport |
| E2E | `tests/e2e/` | 20 | 10 min | Full process or PTY |

---

## 4. Fixtures Reference (`tests/conftest.py`)

```python
# tests/conftest.py
"""
Root conftest.py — fixtures shared across all test layers.

All fixtures that touch async resources use asyncio scope where possible
to amortize setup cost without leaking state.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import os
import time
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from hypothesis import settings as hyp_settings

# ---------------------------------------------------------------------------
# Hypothesis profiles
# ---------------------------------------------------------------------------
hyp_settings.register_profile("ci", max_examples=200, deadline=500)
hyp_settings.register_profile("dev", max_examples=50, deadline=2000)
hyp_settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))


# ---------------------------------------------------------------------------
# AsyncMockTransport
# ---------------------------------------------------------------------------

class AsyncMockTransport:
    """
    Wraps the lauren-ai MockTransport pattern with a scripted response
    sequence.  Pass ``responses`` as a list of strings or dicts; each
    call to ``complete`` pops the next entry.  When the list is
    exhausted the transport raises ``StopAsyncIteration`` to make
    accidental over-calling visible.

    Usage::

        transport = AsyncMockTransport(responses=[
            "I will refactor the function.",
            {"tool_use": {"name": "write_file", "input": {"path": "x.py", "content": "..."}}},
            "Done.",
        ])
    """

    def __init__(self, responses: list[str | dict]) -> None:
        self._queue: collections.deque[str | dict] = collections.deque(responses)
        self.calls: list[dict] = []

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        if not self._queue:
            raise StopAsyncIteration(
                "AsyncMockTransport exhausted — add more scripted responses"
            )
        self.calls.append({"messages": messages, "kwargs": kwargs})
        raw = self._queue.popleft()
        if isinstance(raw, str):
            return {
                "role": "assistant",
                "content": [{"type": "text", "text": raw}],
                "stop_reason": "end_turn",
            }
        # dict — caller supplies the full response shape
        return raw

    async def stream(self, messages: list[dict], **kwargs: Any):
        """Streaming variant — yields a single chunk then stop."""
        response = await self.complete(messages, **kwargs)
        yield {"type": "content_block_start", "index": 0, "content_block": response["content"][0]}
        yield {"type": "message_stop"}

    def assert_called_times(self, n: int) -> None:
        assert len(self.calls) == n, (
            f"Expected transport.complete to be called {n} times, got {len(self.calls)}"
        )

    def assert_remaining(self, n: int) -> None:
        assert len(self._queue) == n, (
            f"Expected {n} queued responses remaining, got {len(self._queue)}"
        )


@pytest.fixture
def mock_transport_factory():
    """Return a callable that produces AsyncMockTransport instances."""
    def factory(responses: list[str | dict]) -> AsyncMockTransport:
        return AsyncMockTransport(responses)
    return factory


# ---------------------------------------------------------------------------
# EventBusTestHarness
# ---------------------------------------------------------------------------

class EventBusTestHarness:
    """
    Captures every event emitted through the EventBus for assertion.

    Usage::

        harness = EventBusTestHarness()
        bus = harness.bus
        # ... run code that emits events ...
        harness.assert_emitted("agent.started", count=1)
        harness.assert_emitted_order(["intent.received", "dag.built", "agent.started"])
    """

    def __init__(self) -> None:
        self._events: list[dict] = []
        self.bus = self._build_bus()

    def _build_bus(self):
        from agenthicc.events import EventBus  # type: ignore[import]

        bus = EventBus()
        # Subscribe a catch-all listener
        async def _capture(event: dict) -> None:
            self._events.append(event)

        bus.subscribe("*", _capture)
        return bus

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def events_of_type(self, event_type: str) -> list[dict]:
        return [e for e in self._events if e.get("type") == event_type]

    def assert_emitted(self, event_type: str, *, count: int | None = None) -> None:
        found = self.events_of_type(event_type)
        if count is not None:
            assert len(found) == count, (
                f"Expected {count} '{event_type}' events, got {len(found)}. "
                f"All events: {[e['type'] for e in self._events]}"
            )
        else:
            assert found, (
                f"Expected at least one '{event_type}' event. "
                f"All events: {[e['type'] for e in self._events]}"
            )

    def assert_not_emitted(self, event_type: str) -> None:
        found = self.events_of_type(event_type)
        assert not found, f"Expected no '{event_type}' events, got {len(found)}"

    def assert_emitted_order(self, types: list[str]) -> None:
        actual = [e.get("type") for e in self._events]
        # Check subsequence, not strict equality
        idx = 0
        for t in types:
            while idx < len(actual) and actual[idx] != t:
                idx += 1
            assert idx < len(actual), (
                f"Event '{t}' not found after previous in sequence. "
                f"Actual order: {actual}"
            )
            idx += 1


@pytest.fixture
def event_bus_harness():
    return EventBusTestHarness()


# ---------------------------------------------------------------------------
# FakeFilesystem
# ---------------------------------------------------------------------------

class FakeFilesystem:
    """
    In-memory WorkspaceView substitute for sandbox tests.
    Supports read, write, exists, list_dir, delete.
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content

    def read(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def exists(self, path: str) -> bool:
        return path in self._files

    def delete(self, path: str) -> None:
        self._files.pop(path, None)

    def list_dir(self, prefix: str = "") -> list[str]:
        return [p for p in self._files if p.startswith(prefix)]

    def snapshot(self) -> dict[str, str]:
        return dict(self._files)

    def restore(self, snapshot: dict[str, str]) -> None:
        self._files = dict(snapshot)

    @property
    def file_count(self) -> int:
        return len(self._files)


@pytest.fixture
def fake_fs() -> FakeFilesystem:
    return FakeFilesystem()


# ---------------------------------------------------------------------------
# AgentPool fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def agent_pool_fixture():
    """
    Pre-warmed AgentPool with 3 agents using AsyncMockTransport.
    Each agent has a distinct scripted response queue that can be
    replenished per-test via agent_pool_fixture.set_responses(agent_id, [...]).
    """
    from agenthicc.agents import AgentPool  # type: ignore[import]
    from agenthicc.config import AgentConfig  # type: ignore[import]

    transports: dict[str, AsyncMockTransport] = {}

    configs = [
        AgentConfig(id="agent-0", role="orchestrator", model="mock"),
        AgentConfig(id="agent-1", role="worker", model="mock"),
        AgentConfig(id="agent-2", role="worker", model="mock"),
    ]

    pool = AgentPool()
    for cfg in configs:
        transport = AsyncMockTransport(responses=["Ready."])
        transports[cfg.id] = transport
        await pool.register(cfg, transport=transport)

    pool._transports = transports  # expose for per-test reconfiguration

    yield pool

    await pool.shutdown()


# ---------------------------------------------------------------------------
# fresh_appstate fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_appstate():
    """Return a pristine AppState for each test — no shared state."""
    from agenthicc.state import AppState  # type: ignore[import]

    return AppState.initial()


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def frozen_time(monkeypatch):
    """Freeze time.time() and asyncio loop time to a fixed epoch."""
    fixed = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: fixed)
    return fixed


@pytest.fixture
def deterministic_ids(monkeypatch):
    """Replace uuid4() with a counter-based deterministic ID generator."""
    import uuid
    counter = [0]

    def fake_uuid4():
        counter[0] += 1
        hex_str = hashlib.md5(str(counter[0]).encode()).hexdigest()
        return uuid.UUID(hex_str)

    monkeypatch.setattr(uuid, "uuid4", fake_uuid4)
    return counter
```

---

## 5. Unit Tests

### 5.1 `tests/unit/test_appstate_reducers.py`

```python
# tests/unit/test_appstate_reducers.py
"""
Property-based and snapshot tests for AppState reducers.

Reducers must be pure functions: same input always produces same output,
and the original state is never mutated.
"""
from __future__ import annotations

import copy
import json

import pytest
from hypothesis import given, assume, settings
from hypothesis import strategies as st

from agenthicc.state import AppState, reduce  # type: ignore[import]
from agenthicc.state.actions import (  # type: ignore[import]
    AddAgentAction,
    RemoveAgentAction,
    SetIntentAction,
    UpdateAgentStatusAction,
    AppendEventAction,
    SetWorkflowAction,
    ResetAction,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

agent_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"),
    min_size=3,
    max_size=32,
)

status_st = st.sampled_from(["idle", "running", "done", "error"])

intent_st = st.text(min_size=1, max_size=500)

add_agent_action_st = st.builds(
    AddAgentAction,
    agent_id=agent_id_st,
    role=st.sampled_from(["orchestrator", "worker", "reviewer"]),
    model=st.just("mock"),
)

event_st = st.fixed_dictionaries({
    "type": st.sampled_from(["agent.started", "tool.called", "agent.done", "error"]),
    "payload": st.dictionaries(st.text(min_size=1, max_size=10), st.integers()),
})


# ---------------------------------------------------------------------------
# Purity: reducer never mutates its input
# ---------------------------------------------------------------------------

@given(action=add_agent_action_st)
def test_reducer_does_not_mutate_input(action):
    state = AppState.initial()
    state_before = copy.deepcopy(state)
    reduce(state, action)
    assert state == state_before, "reducer mutated its input state"


@given(intent=intent_st)
def test_set_intent_does_not_mutate_input(intent):
    state = AppState.initial()
    state_before = copy.deepcopy(state)
    reduce(state, SetIntentAction(intent=intent))
    assert state == state_before


# ---------------------------------------------------------------------------
# Idempotency: applying the same action twice is equivalent to once (for set ops)
# ---------------------------------------------------------------------------

@given(intent=intent_st)
def test_set_intent_idempotent(intent):
    state = AppState.initial()
    s1 = reduce(state, SetIntentAction(intent=intent))
    s2 = reduce(s1, SetIntentAction(intent=intent))
    assert s1 == s2, "SetIntent applied twice produced different states"


# ---------------------------------------------------------------------------
# Composition: reduce(reduce(s, a1), a2) correct
# ---------------------------------------------------------------------------

@given(
    action1=add_agent_action_st,
    action2=add_agent_action_st,
)
def test_reducer_composition(action1, action2):
    assume(action1.agent_id != action2.agent_id)
    state = AppState.initial()
    s1 = reduce(state, action1)
    s2 = reduce(s1, action2)
    # Both agents must appear
    assert action1.agent_id in s2.agents
    assert action2.agent_id in s2.agents


# ---------------------------------------------------------------------------
# Remove undoes add
# ---------------------------------------------------------------------------

@given(action=add_agent_action_st)
def test_remove_undoes_add(action):
    state = AppState.initial()
    after_add = reduce(state, action)
    assert action.agent_id in after_add.agents
    after_remove = reduce(after_add, RemoveAgentAction(agent_id=action.agent_id))
    assert action.agent_id not in after_remove.agents


# ---------------------------------------------------------------------------
# UpdateAgentStatus
# ---------------------------------------------------------------------------

@given(
    agent_action=add_agent_action_st,
    new_status=status_st,
)
def test_update_agent_status(agent_action, new_status):
    state = AppState.initial()
    state = reduce(state, agent_action)
    state = reduce(state, UpdateAgentStatusAction(
        agent_id=agent_action.agent_id, status=new_status
    ))
    assert state.agents[agent_action.agent_id]["status"] == new_status


# ---------------------------------------------------------------------------
# AppendEvent accumulates events
# ---------------------------------------------------------------------------

@given(events=st.lists(event_st, min_size=1, max_size=20))
def test_append_event_accumulates(events):
    state = AppState.initial()
    for ev in events:
        state = reduce(state, AppendEventAction(event=ev))
    assert len(state.event_log) == len(events)


# ---------------------------------------------------------------------------
# ResetAction returns initial state
# ---------------------------------------------------------------------------

@given(intent=intent_st, action=add_agent_action_st)
def test_reset_returns_initial(intent, action):
    state = AppState.initial()
    state = reduce(state, SetIntentAction(intent=intent))
    state = reduce(state, action)
    state = reduce(state, ResetAction())
    assert state == AppState.initial()


# ---------------------------------------------------------------------------
# Snapshot round-trip: serialise -> deserialise -> re-reduce equals direct reduce
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("actions", [
    [SetIntentAction(intent="refactor auth module")],
    [
        AddAgentAction(agent_id="a1", role="worker", model="mock"),
        UpdateAgentStatusAction(agent_id="a1", status="running"),
    ],
    [ResetAction()],
])
def test_snapshot_roundtrip(actions):
    state = AppState.initial()
    for action in actions:
        state = reduce(state, action)

    # Serialise to JSON and back
    serialised = json.dumps(state.to_dict())
    restored = AppState.from_dict(json.loads(serialised))

    assert restored == state, "AppState did not survive JSON round-trip"


# ---------------------------------------------------------------------------
# Type coercion: status field is always a string
# ---------------------------------------------------------------------------

def test_status_coerced_to_string():
    state = AppState.initial()
    state = reduce(state, AddAgentAction(agent_id="x", role="worker", model="mock"))
    # Attempt numeric status — reducer should coerce or raise a typed error
    with pytest.raises((TypeError, ValueError)):
        reduce(state, UpdateAgentStatusAction(agent_id="x", status=42))  # type: ignore[arg-type]
```

---

### 5.2 `tests/unit/test_dag_executor.py`

```python
# tests/unit/test_dag_executor.py
"""
Tests for the DAG executor: ready-node selection algorithm and cycle detection.
Uses pytest.mark.parametrize with graph fixtures.
"""
from __future__ import annotations

import pytest

from agenthicc.dag import DAGExecutor, build_dag, CycleDetectedError  # type: ignore[import]


# ---------------------------------------------------------------------------
# Graph fixtures
# ---------------------------------------------------------------------------

# Each fixture is (nodes, edges, expected_topological_layers)
# Layers represent sets of nodes that can run in parallel at each step.

GRAPH_FIXTURES = [
    pytest.param(
        # Linear chain: A -> B -> C
        ["A", "B", "C"],
        [("A", "B"), ("B", "C")],
        [{"A"}, {"B"}, {"C"}],
        id="linear-chain",
    ),
    pytest.param(
        # Diamond: A -> B, A -> C, B -> D, C -> D
        ["A", "B", "C", "D"],
        [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")],
        [{"A"}, {"B", "C"}, {"D"}],
        id="diamond",
    ),
    pytest.param(
        # Fully parallel: no edges
        ["X", "Y", "Z"],
        [],
        [{"X", "Y", "Z"}],
        id="fully-parallel",
    ),
    pytest.param(
        # Single node
        ["solo"],
        [],
        [{"solo"}],
        id="single-node",
    ),
    pytest.param(
        # Wide fan-out then merge
        ["root", "w1", "w2", "w3", "w4", "merge"],
        [
            ("root", "w1"), ("root", "w2"), ("root", "w3"), ("root", "w4"),
            ("w1", "merge"), ("w2", "merge"), ("w3", "merge"), ("w4", "merge"),
        ],
        [{"root"}, {"w1", "w2", "w3", "w4"}, {"merge"}],
        id="fan-out-merge",
    ),
    pytest.param(
        # Two independent chains
        ["a1", "a2", "b1", "b2"],
        [("a1", "a2"), ("b1", "b2")],
        [{"a1", "b1"}, {"a2", "b2"}],
        id="two-independent-chains",
    ),
]

CYCLE_FIXTURES = [
    pytest.param(
        ["A", "B"],
        [("A", "B"), ("B", "A")],
        id="simple-cycle",
    ),
    pytest.param(
        ["A", "B", "C"],
        [("A", "B"), ("B", "C"), ("C", "A")],
        id="three-node-cycle",
    ),
    pytest.param(
        # Self-loop
        ["A"],
        [("A", "A")],
        id="self-loop",
    ),
    pytest.param(
        # Cycle embedded in larger graph
        ["A", "B", "C", "D"],
        [("A", "B"), ("B", "C"), ("C", "B"), ("C", "D")],
        id="cycle-in-larger-graph",
    ),
]


# ---------------------------------------------------------------------------
# Ready-node algorithm correctness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nodes,edges,expected_layers", GRAPH_FIXTURES)
def test_topological_layers(nodes, edges, expected_layers):
    dag = build_dag(nodes=nodes, edges=edges)
    executor = DAGExecutor(dag)
    actual_layers: list[set[str]] = []

    while not executor.is_complete:
        ready = executor.ready_nodes()
        assert ready, "DAGExecutor reported nodes not complete but no ready nodes"
        actual_layers.append(set(ready))
        for node in ready:
            executor.mark_done(node)

    assert actual_layers == expected_layers, (
        f"Layer mismatch.\nExpected: {expected_layers}\nActual:   {actual_layers}"
    )


@pytest.mark.parametrize("nodes,edges,expected_layers", GRAPH_FIXTURES)
def test_ready_nodes_exclude_completed(nodes, edges, expected_layers):
    """Once a node is marked done it never reappears in ready_nodes()."""
    dag = build_dag(nodes=nodes, edges=edges)
    executor = DAGExecutor(dag)
    seen: set[str] = set()

    while not executor.is_complete:
        ready = executor.ready_nodes()
        overlap = seen & set(ready)
        assert not overlap, f"Nodes appeared ready again after being marked done: {overlap}"
        seen.update(ready)
        for node in ready:
            executor.mark_done(node)


@pytest.mark.parametrize("nodes,edges,expected_layers", GRAPH_FIXTURES)
def test_ready_nodes_respect_dependencies(nodes, edges, expected_layers):
    """A node with unsatisfied dependencies must never appear in ready_nodes()."""
    dag = build_dag(nodes=nodes, edges=edges)
    executor = DAGExecutor(dag)

    # Map: node -> set of its prerequisites
    prereqs: dict[str, set[str]] = {n: set() for n in nodes}
    for src, dst in edges:
        prereqs[dst].add(src)

    completed: set[str] = set()

    while not executor.is_complete:
        ready = set(executor.ready_nodes())
        for node in ready:
            unsatisfied = prereqs[node] - completed
            assert not unsatisfied, (
                f"Node '{node}' appeared ready but has unsatisfied deps: {unsatisfied}"
            )
        node = next(iter(ready))
        executor.mark_done(node)
        completed.add(node)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nodes,edges", CYCLE_FIXTURES)
def test_cycle_detection_raises(nodes, edges):
    with pytest.raises(CycleDetectedError):
        build_dag(nodes=nodes, edges=edges)


def test_cycle_error_names_cycle_nodes():
    """CycleDetectedError must include the offending nodes in its message."""
    try:
        build_dag(nodes=["A", "B", "C"], edges=[("A", "B"), ("B", "C"), ("C", "A")])
        pytest.fail("Expected CycleDetectedError was not raised")
    except CycleDetectedError as exc:
        msg = str(exc)
        assert "A" in msg or "B" in msg or "C" in msg, (
            f"CycleDetectedError message did not mention any cycle node: {msg}"
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_dag_is_immediately_complete():
    dag = build_dag(nodes=[], edges=[])
    executor = DAGExecutor(dag)
    assert executor.is_complete
    assert executor.ready_nodes() == []


def test_mark_unknown_node_raises():
    dag = build_dag(nodes=["A"], edges=[])
    executor = DAGExecutor(dag)
    with pytest.raises((KeyError, ValueError)):
        executor.mark_done("nonexistent-node")


def test_mark_done_twice_raises_or_is_idempotent():
    """Marking a node done twice should either be idempotent or raise — not silently corrupt state."""
    dag = build_dag(nodes=["A", "B"], edges=[("A", "B")])
    executor = DAGExecutor(dag)
    executor.mark_done("A")
    # Must not raise OR must raise — but must not silently break B's readiness
    try:
        executor.mark_done("A")
    except (ValueError, KeyError):
        pass  # acceptable
    # B should still be reachable
    assert "B" in executor.ready_nodes()
```

---

### 5.3 `tests/unit/test_communication_tools.py`

```python
# tests/unit/test_communication_tools.py
"""
Unit tests for all 12 communication tools.
Each tool is tested with a mock EventBus to verify:
  - correct event type emitted
  - payload shape
  - error handling when bus is unavailable
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from agenthicc.tools.communication import (  # type: ignore[import]
    SendMessageTool,
    BroadcastTool,
    RequestApprovalTool,
    WaitForSignalTool,
    EmitEventTool,
    SubscribeEventTool,
    UnsubscribeEventTool,
    PublishResultTool,
    RequestHelpTool,
    NotifyCompletionTool,
    EscalateTool,
    HeartbeatTool,
)

ALL_TOOLS = [
    SendMessageTool,
    BroadcastTool,
    RequestApprovalTool,
    WaitForSignalTool,
    EmitEventTool,
    SubscribeEventTool,
    UnsubscribeEventTool,
    PublishResultTool,
    RequestHelpTool,
    NotifyCompletionTool,
    EscalateTool,
    HeartbeatTool,
]


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.emit = AsyncMock(return_value=None)
    bus.subscribe = AsyncMock(return_value=None)
    bus.unsubscribe = AsyncMock(return_value=None)
    return bus


@pytest.fixture
def tool_context(mock_bus):
    from agenthicc.context import ToolContext  # type: ignore[import]
    ctx = MagicMock(spec=ToolContext)
    ctx.event_bus = mock_bus
    ctx.agent_id = "test-agent"
    ctx.session_id = "test-session"
    return ctx


# ---------------------------------------------------------------------------
# All tools have required metadata
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ToolClass", ALL_TOOLS)
def test_tool_has_name(ToolClass):
    tool = ToolClass()
    assert isinstance(tool.name, str) and tool.name, f"{ToolClass.__name__} missing .name"


@pytest.mark.parametrize("ToolClass", ALL_TOOLS)
def test_tool_has_description(ToolClass):
    tool = ToolClass()
    assert isinstance(tool.description, str) and tool.description, (
        f"{ToolClass.__name__} missing .description"
    )


@pytest.mark.parametrize("ToolClass", ALL_TOOLS)
def test_tool_has_input_schema(ToolClass):
    tool = ToolClass()
    schema = tool.input_schema
    assert isinstance(schema, dict), f"{ToolClass.__name__} .input_schema must be a dict"
    assert "properties" in schema or "type" in schema


# ---------------------------------------------------------------------------
# SendMessageTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_emits_correct_event(tool_context):
    tool = SendMessageTool()
    await tool.run(
        {"to": "agent-1", "content": "hello", "priority": "normal"},
        context=tool_context,
    )
    tool_context.event_bus.emit.assert_called_once()
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "message.sent"
    assert emitted["payload"]["to"] == "agent-1"
    assert emitted["payload"]["content"] == "hello"


@pytest.mark.asyncio
async def test_send_message_requires_recipient(tool_context):
    tool = SendMessageTool()
    with pytest.raises((ValueError, KeyError)):
        await tool.run({"content": "hello"}, context=tool_context)


# ---------------------------------------------------------------------------
# BroadcastTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_emits_broadcast_event(tool_context):
    tool = BroadcastTool()
    await tool.run({"content": "all hands", "channel": "general"}, context=tool_context)
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "message.broadcast"
    assert emitted["payload"]["channel"] == "general"


# ---------------------------------------------------------------------------
# RequestApprovalTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_approval_emits_approval_request(tool_context):
    tool = RequestApprovalTool()
    await tool.run(
        {"request": "delete production DB", "urgency": "high"},
        context=tool_context,
    )
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "approval.requested"
    assert "delete production DB" in str(emitted["payload"])


# ---------------------------------------------------------------------------
# WaitForSignalTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_signal_subscribes_and_returns(tool_context):
    tool = WaitForSignalTool()
    # Simulate immediate signal delivery
    async def fake_subscribe(event_type, callback):
        await callback({"type": event_type, "payload": {"result": "ok"}})

    tool_context.event_bus.subscribe = fake_subscribe

    result = await tool.run(
        {"signal": "approval.granted", "timeout_seconds": 5},
        context=tool_context,
    )
    assert result is not None


@pytest.mark.asyncio
async def test_wait_for_signal_timeout(tool_context):
    """WaitForSignalTool must raise TimeoutError when signal never arrives."""
    tool = WaitForSignalTool()
    tool_context.event_bus.subscribe = AsyncMock()  # never calls callback

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(
            tool.run({"signal": "phantom.signal", "timeout_seconds": 0.05}, context=tool_context),
            timeout=1.0,
        )


# ---------------------------------------------------------------------------
# EmitEventTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_event_passes_payload(tool_context):
    tool = EmitEventTool()
    await tool.run(
        {"event_type": "custom.event", "payload": {"key": "value"}},
        context=tool_context,
    )
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "custom.event"
    assert emitted["payload"]["key"] == "value"


# ---------------------------------------------------------------------------
# SubscribeEventTool / UnsubscribeEventTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_then_unsubscribe(tool_context):
    sub_tool = SubscribeEventTool()
    unsub_tool = UnsubscribeEventTool()

    result = await sub_tool.run(
        {"event_type": "agent.done", "handler_id": "h1"},
        context=tool_context,
    )
    assert result is not None
    tool_context.event_bus.subscribe.assert_called_once()

    await unsub_tool.run({"handler_id": "h1"}, context=tool_context)
    tool_context.event_bus.unsubscribe.assert_called_once()


# ---------------------------------------------------------------------------
# PublishResultTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_result_includes_agent_id(tool_context):
    tool = PublishResultTool()
    await tool.run(
        {"result": {"summary": "done"}, "status": "success"},
        context=tool_context,
    )
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "result.published"
    assert emitted["payload"].get("agent_id") == "test-agent"


# ---------------------------------------------------------------------------
# RequestHelpTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_help_emits_help_event(tool_context):
    tool = RequestHelpTool()
    await tool.run(
        {"problem": "stuck in infinite loop", "context_snapshot": {}},
        context=tool_context,
    )
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "help.requested"


# ---------------------------------------------------------------------------
# NotifyCompletionTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_completion_sets_status_done(tool_context):
    tool = NotifyCompletionTool()
    await tool.run(
        {"summary": "task complete", "artifacts": ["output.py"]},
        context=tool_context,
    )
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "agent.completed"


# ---------------------------------------------------------------------------
# EscalateTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_escalate_emits_high_priority_event(tool_context):
    tool = EscalateTool()
    await tool.run(
        {"reason": "unhandled exception", "severity": "critical"},
        context=tool_context,
    )
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "escalation.raised"
    assert emitted["payload"].get("severity") == "critical"


# ---------------------------------------------------------------------------
# HeartbeatTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_emits_alive_event(tool_context):
    tool = HeartbeatTool()
    await tool.run({"status": "alive", "progress": 0.42}, context=tool_context)
    emitted = tool_context.event_bus.emit.call_args[0][0]
    assert emitted["type"] == "agent.heartbeat"
    assert abs(emitted["payload"]["progress"] - 0.42) < 1e-6


# ---------------------------------------------------------------------------
# Bus failure propagation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ToolClass", [
    SendMessageTool, BroadcastTool, EmitEventTool,
    PublishResultTool, NotifyCompletionTool, HeartbeatTool,
])
@pytest.mark.asyncio
async def test_bus_failure_propagates(ToolClass, tool_context):
    tool = ToolClass()
    tool_context.event_bus.emit.side_effect = RuntimeError("bus down")

    with pytest.raises(RuntimeError, match="bus down"):
        await tool.run(_minimal_input(ToolClass), context=tool_context)


def _minimal_input(ToolClass) -> dict:
    """Return a minimal valid input dict for each tool class."""
    defaults = {
        "SendMessageTool": {"to": "x", "content": "y"},
        "BroadcastTool": {"content": "y", "channel": "c"},
        "EmitEventTool": {"event_type": "t", "payload": {}},
        "PublishResultTool": {"result": {}, "status": "success"},
        "NotifyCompletionTool": {"summary": "s", "artifacts": []},
        "HeartbeatTool": {"status": "alive", "progress": 0.0},
    }
    return defaults.get(ToolClass.__name__, {})
```

---

### 5.4 `tests/unit/test_lifecycle_hooks.py`

```python
# tests/unit/test_lifecycle_hooks.py
"""
Tests for ToolHook lifecycle: ordering (global wraps per-tool),
rejection short-circuit, and error recovery.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from agenthicc.hooks import HookRegistry, HookPhase  # type: ignore[import]
from agenthicc.tools.base import BaseTool  # type: ignore[import]


# ---------------------------------------------------------------------------
# Minimal test tool
# ---------------------------------------------------------------------------

class EchoTool(BaseTool):
    name = "echo"
    description = "returns input unchanged"

    async def run(self, inputs: dict, context=None) -> dict:
        return {"echo": inputs.get("text", "")}


# ---------------------------------------------------------------------------
# Ordering: global hook runs before per-tool hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_hook_runs_before_per_tool_hook():
    registry = HookRegistry()
    call_order: list[str] = []

    async def global_before(tool_name, inputs, ctx):
        call_order.append("global_before")
        return inputs

    async def per_tool_before(inputs, ctx):
        call_order.append("per_tool_before")
        return inputs

    registry.register_global(HookPhase.BEFORE, global_before)
    registry.register_per_tool("echo", HookPhase.BEFORE, per_tool_before)

    tool = EchoTool()
    ctx = MagicMock()
    await registry.run_tool(tool, {"text": "hi"}, ctx)

    assert call_order.index("global_before") < call_order.index("per_tool_before"), (
        f"global_before should precede per_tool_before. Order was: {call_order}"
    )


@pytest.mark.asyncio
async def test_after_hooks_run_in_reverse_order():
    """AFTER hooks run in reverse registration order (innermost first)."""
    registry = HookRegistry()
    call_order: list[str] = []

    async def global_after(tool_name, result, ctx):
        call_order.append("global_after")
        return result

    async def per_tool_after(result, ctx):
        call_order.append("per_tool_after")
        return result

    registry.register_global(HookPhase.AFTER, global_after)
    registry.register_per_tool("echo", HookPhase.AFTER, per_tool_after)

    tool = EchoTool()
    ctx = MagicMock()
    await registry.run_tool(tool, {"text": "hi"}, ctx)

    # per-tool after runs before global after (innermost wraps first)
    assert call_order.index("per_tool_after") < call_order.index("global_after"), (
        f"per_tool_after should precede global_after. Order: {call_order}"
    )


# ---------------------------------------------------------------------------
# Rejection short-circuit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_hook_rejection_prevents_tool_execution():
    registry = HookRegistry()
    tool_called = False

    class TrackingTool(BaseTool):
        name = "tracking"
        description = "tracks calls"

        async def run(self, inputs: dict, context=None) -> dict:
            nonlocal tool_called
            tool_called = True
            return {}

    async def rejecting_hook(tool_name, inputs, ctx):
        raise PermissionError("blocked by policy")

    registry.register_global(HookPhase.BEFORE, rejecting_hook)

    tool = TrackingTool()
    with pytest.raises(PermissionError, match="blocked by policy"):
        await registry.run_tool(tool, {}, MagicMock())

    assert not tool_called, "Tool was called despite before-hook rejection"


@pytest.mark.asyncio
async def test_rejection_does_not_run_after_hooks():
    registry = HookRegistry()
    after_called = False

    async def rejecting_before(tool_name, inputs, ctx):
        raise PermissionError("rejected")

    async def after_hook(tool_name, result, ctx):
        nonlocal after_called
        after_called = True
        return result

    registry.register_global(HookPhase.BEFORE, rejecting_before)
    registry.register_global(HookPhase.AFTER, after_hook)

    tool = EchoTool()
    with pytest.raises(PermissionError):
        await registry.run_tool(tool, {"text": "x"}, MagicMock())

    assert not after_called, "After hook ran despite before-hook rejection"


# ---------------------------------------------------------------------------
# Error recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_error_hook_called_on_tool_failure():
    registry = HookRegistry()
    error_hook_called_with: list[Exception] = []

    async def error_hook(tool_name, exc, ctx):
        error_hook_called_with.append(exc)

    registry.register_global(HookPhase.ON_ERROR, error_hook)

    class FailingTool(BaseTool):
        name = "failing"
        description = "always fails"

        async def run(self, inputs: dict, context=None) -> dict:
            raise ValueError("tool exploded")

    tool = FailingTool()
    with pytest.raises(ValueError, match="tool exploded"):
        await registry.run_tool(tool, {}, MagicMock())

    assert len(error_hook_called_with) == 1
    assert isinstance(error_hook_called_with[0], ValueError)


@pytest.mark.asyncio
async def test_on_error_hook_can_suppress_exception():
    """An on_error hook that returns a fallback result suppresses the exception."""
    registry = HookRegistry()

    async def recovering_error_hook(tool_name, exc, ctx):
        return {"recovered": True}

    registry.register_global(HookPhase.ON_ERROR, recovering_error_hook)

    class FailingTool(BaseTool):
        name = "failing"
        description = "always fails"

        async def run(self, inputs: dict, context=None) -> dict:
            raise RuntimeError("boom")

    tool = FailingTool()
    result = await registry.run_tool(tool, {}, MagicMock())
    assert result == {"recovered": True}


@pytest.mark.asyncio
async def test_multiple_global_hooks_all_invoked():
    registry = HookRegistry()
    invoked: list[str] = []

    for i in range(3):
        async def make_hook(name=f"hook_{i}"):
            async def _hook(tool_name, inputs, ctx):
                invoked.append(name)
                return inputs
            return _hook
        registry.register_global(HookPhase.BEFORE, await make_hook())

    tool = EchoTool()
    await registry.run_tool(tool, {"text": "hi"}, MagicMock())
    assert len(invoked) == 3
```

---

### 5.5 `tests/unit/test_tui_diff.py`

```python
# tests/unit/test_tui_diff.py
"""
Tests for the TUI transcript diff algorithm and block rendering.
Verifies correctness of minimal-edit diff and ANSI block output.
"""
from __future__ import annotations

import re
import pytest

from agenthicc.tui.diff import (  # type: ignore[import]
    diff_transcript,
    render_diff_blocks,
    DiffOp,
    TranscriptBlock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def make_block(role: str, text: str) -> TranscriptBlock:
    return TranscriptBlock(role=role, content=text, timestamp=0.0)


# ---------------------------------------------------------------------------
# Diff algorithm correctness
# ---------------------------------------------------------------------------

def test_identical_transcripts_produce_no_ops():
    a = [make_block("user", "hello"), make_block("assistant", "hi there")]
    b = [make_block("user", "hello"), make_block("assistant", "hi there")]
    ops = diff_transcript(a, b)
    assert not any(op.kind in ("insert", "delete") for op in ops), (
        "Identical transcripts should produce no insert/delete ops"
    )


def test_appended_block_is_single_insert():
    a = [make_block("user", "q1")]
    b = [make_block("user", "q1"), make_block("assistant", "a1")]
    ops = diff_transcript(a, b)
    inserts = [op for op in ops if op.kind == "insert"]
    assert len(inserts) == 1
    assert inserts[0].block.content == "a1"


def test_deleted_block_is_single_delete():
    a = [make_block("user", "q1"), make_block("user", "q2")]
    b = [make_block("user", "q1")]
    ops = diff_transcript(a, b)
    deletes = [op for op in ops if op.kind == "delete"]
    assert len(deletes) == 1
    assert deletes[0].block.content == "q2"


def test_changed_block_is_delete_then_insert():
    a = [make_block("assistant", "old answer")]
    b = [make_block("assistant", "new answer")]
    ops = diff_transcript(a, b)
    kinds = [op.kind for op in ops]
    assert "delete" in kinds and "insert" in kinds


@pytest.mark.parametrize("size", [0, 1, 10, 100])
def test_diff_length_property(size):
    """len(apply(diff(a, b), a)) == len(b) for any a, b."""
    import random
    rng = random.Random(size)
    roles = ["user", "assistant"]
    texts = [f"msg-{i}" for i in range(size + 5)]

    a = [make_block(rng.choice(roles), rng.choice(texts)) for _ in range(size)]
    b = [make_block(rng.choice(roles), rng.choice(texts)) for _ in range(rng.randint(0, size + 3))]

    ops = diff_transcript(a, b)
    # Verify ops reconstruct b when applied to a
    result = _apply_ops(a, ops)
    assert len(result) == len(b)


def _apply_ops(source: list, ops) -> list:
    result = []
    src_idx = 0
    for op in ops:
        if op.kind == "keep":
            result.append(source[src_idx])
            src_idx += 1
        elif op.kind == "delete":
            src_idx += 1
        elif op.kind == "insert":
            result.append(op.block)
    return result


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------

def test_render_user_block_contains_role_label():
    block = make_block("user", "What is 2+2?")
    rendered = render_diff_blocks([block])
    plain = strip_ansi(rendered)
    assert "user" in plain.lower() or "User" in plain


def test_render_assistant_block_contains_content():
    block = make_block("assistant", "The answer is 4.")
    rendered = render_diff_blocks([block])
    assert "The answer is 4." in strip_ansi(rendered)


def test_render_empty_transcript_is_empty_string():
    rendered = render_diff_blocks([])
    assert strip_ansi(rendered).strip() == ""


def test_render_blocks_are_separated():
    blocks = [
        make_block("user", "Q1"),
        make_block("assistant", "A1"),
        make_block("user", "Q2"),
    ]
    rendered = strip_ansi(render_diff_blocks(blocks))
    assert rendered.index("Q1") < rendered.index("A1") < rendered.index("Q2")


def test_inserted_blocks_have_diff_marker():
    """Blocks marked as inserted should render with a '+' or green indicator."""
    block = make_block("assistant", "new content")
    block_with_op = DiffOp(kind="insert", block=block)
    rendered = render_diff_blocks(blocks=[], ops=[block_with_op])
    # Either ANSI green or '+' prefix
    assert "\x1b[32m" in rendered or "+" in strip_ansi(rendered)


def test_deleted_blocks_have_diff_marker():
    block = make_block("assistant", "old content")
    block_with_op = DiffOp(kind="delete", block=block)
    rendered = render_diff_blocks(blocks=[], ops=[block_with_op])
    assert "\x1b[31m" in rendered or "-" in strip_ansi(rendered)
```

---

### 5.6 `tests/unit/test_toml_config.py`

```python
# tests/unit/test_toml_config.py
"""
Tests for TOML configuration loading:
  - merge precedence (user config over project config)
  - missing optional fields fall back to defaults
  - type coercion and validation errors
"""
from __future__ import annotations

import textwrap
import pytest

from agenthicc.config.loader import load_config, ConfigError  # type: ignore[import]


# ---------------------------------------------------------------------------
# Fixtures: in-memory TOML strings
# ---------------------------------------------------------------------------

PROJECT_TOML = textwrap.dedent("""\
    [runtime]
    max_agents = 4
    timeout_seconds = 30
    log_level = "info"

    [memory]
    backend = "in_memory"
    vector_dims = 128

    [tools]
    enabled = ["write_file", "read_file", "send_message"]
""")

USER_TOML = textwrap.dedent("""\
    [runtime]
    max_agents = 8
    log_level = "debug"

    [memory]
    backend = "postgres"
""")

EMPTY_TOML = ""

PARTIAL_TOML = textwrap.dedent("""\
    [runtime]
    max_agents = 2
""")


# ---------------------------------------------------------------------------
# Merge precedence
# ---------------------------------------------------------------------------

def test_user_overrides_project_scalar(tmp_path):
    project_file = tmp_path / "project.toml"
    user_file = tmp_path / "user.toml"
    project_file.write_text(PROJECT_TOML)
    user_file.write_text(USER_TOML)

    config = load_config(project_path=str(project_file), user_path=str(user_file))

    assert config.runtime.max_agents == 8, "user max_agents=8 should override project max_agents=4"
    assert config.runtime.log_level == "debug", "user log_level should override project"


def test_project_value_preserved_when_not_overridden(tmp_path):
    project_file = tmp_path / "project.toml"
    user_file = tmp_path / "user.toml"
    project_file.write_text(PROJECT_TOML)
    user_file.write_text(USER_TOML)

    config = load_config(project_path=str(project_file), user_path=str(user_file))

    # timeout_seconds not in user TOML — must come from project
    assert config.runtime.timeout_seconds == 30


def test_user_overrides_nested_section(tmp_path):
    project_file = tmp_path / "project.toml"
    user_file = tmp_path / "user.toml"
    project_file.write_text(PROJECT_TOML)
    user_file.write_text(USER_TOML)

    config = load_config(project_path=str(project_file), user_path=str(user_file))

    assert config.memory.backend == "postgres"


def test_project_nested_value_preserved_when_no_user_override(tmp_path):
    project_file = tmp_path / "project.toml"
    user_file = tmp_path / "user.toml"
    project_file.write_text(PROJECT_TOML)
    user_file.write_text(USER_TOML)

    config = load_config(project_path=str(project_file), user_path=str(user_file))

    # vector_dims not in user TOML
    assert config.memory.vector_dims == 128


# ---------------------------------------------------------------------------
# Missing fields fall back to defaults
# ---------------------------------------------------------------------------

def test_missing_user_config_uses_project_only(tmp_path):
    project_file = tmp_path / "project.toml"
    project_file.write_text(PROJECT_TOML)

    config = load_config(project_path=str(project_file), user_path=None)
    assert config.runtime.max_agents == 4


def test_empty_project_config_uses_all_defaults(tmp_path):
    project_file = tmp_path / "project.toml"
    project_file.write_text(EMPTY_TOML)

    config = load_config(project_path=str(project_file), user_path=None)
    # Defaults must be non-None
    assert config.runtime.max_agents > 0
    assert config.runtime.timeout_seconds > 0
    assert config.runtime.log_level in ("debug", "info", "warning", "error")


def test_partial_config_merges_with_defaults(tmp_path):
    project_file = tmp_path / "project.toml"
    project_file.write_text(PARTIAL_TOML)

    config = load_config(project_path=str(project_file), user_path=None)
    assert config.runtime.max_agents == 2
    assert config.runtime.timeout_seconds > 0  # default


def test_missing_project_config_file_uses_all_defaults():
    config = load_config(project_path="/nonexistent/path.toml", user_path=None)
    assert config is not None
    assert config.runtime.max_agents > 0


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

def test_max_agents_as_string_is_coerced(tmp_path):
    toml = textwrap.dedent("""\
        [runtime]
        max_agents = "6"
    """)
    f = tmp_path / "project.toml"
    f.write_text(toml)
    config = load_config(project_path=str(f), user_path=None)
    assert config.runtime.max_agents == 6
    assert isinstance(config.runtime.max_agents, int)


def test_boolean_string_coerced(tmp_path):
    toml = textwrap.dedent("""\
        [runtime]
        verbose = "true"
    """)
    f = tmp_path / "project.toml"
    f.write_text(toml)
    config = load_config(project_path=str(f), user_path=None)
    assert config.runtime.verbose is True


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

def test_invalid_log_level_raises(tmp_path):
    toml = textwrap.dedent("""\
        [runtime]
        log_level = "TRACE"
    """)
    f = tmp_path / "project.toml"
    f.write_text(toml)
    with pytest.raises(ConfigError, match="log_level"):
        load_config(project_path=str(f), user_path=None)


def test_negative_max_agents_raises(tmp_path):
    toml = textwrap.dedent("""\
        [runtime]
        max_agents = -1
    """)
    f = tmp_path / "project.toml"
    f.write_text(toml)
    with pytest.raises(ConfigError):
        load_config(project_path=str(f), user_path=None)


def test_unknown_memory_backend_raises(tmp_path):
    toml = textwrap.dedent("""\
        [memory]
        backend = "quantum_storage"
    """)
    f = tmp_path / "project.toml"
    f.write_text(toml)
    with pytest.raises(ConfigError, match="backend"):
        load_config(project_path=str(f), user_path=None)
```

---

## 6. Integration Tests

### 6.1 `tests/integration/test_intent_workflow_cycle.py`

```python
# tests/integration/test_intent_workflow_cycle.py
"""
Full intent -> workflow -> agent -> tool -> memory cycle.
Uses AsyncMockTransport with scripted responses.
No real LLM calls. No real disk I/O.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from agenthicc.runtime import AgenticRuntime  # type: ignore[import]
from agenthicc.state import AppState  # type: ignore[import]
from tests.conftest import AsyncMockTransport, FakeFilesystem


SCRIPTED_RESPONSES = [
    # Orchestrator receives intent, plans workflow
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will refactor the `auth` module. Steps: analyse, rewrite, test."},
            {
                "type": "tool_use",
                "id": "tu_001",
                "name": "build_workflow",
                "input": {
                    "tasks": [
                        {"id": "analyse", "description": "Read auth.py"},
                        {"id": "rewrite", "description": "Rewrite auth.py", "depends_on": ["analyse"]},
                        {"id": "test", "description": "Run tests", "depends_on": ["rewrite"]},
                    ]
                },
            },
        ],
        "stop_reason": "tool_use",
    },
    # Worker: analyse
    "I have read auth.py. The function `login` uses MD5 — must replace with bcrypt.",
    # Worker: rewrite
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Rewriting auth.py."},
            {
                "type": "tool_use",
                "id": "tu_002",
                "name": "write_file",
                "input": {"path": "auth.py", "content": "import bcrypt\n# ... rewritten"},
            },
        ],
        "stop_reason": "tool_use",
    },
    # Worker: test
    "All tests pass.",
    # Orchestrator: final summary
    "Workflow complete. auth.py refactored to use bcrypt.",
]


@pytest_asyncio.fixture
async def runtime_with_fake_transport(fake_fs):
    transport = AsyncMockTransport(responses=list(SCRIPTED_RESPONSES))
    runtime = AgenticRuntime(
        transport=transport,
        filesystem=fake_fs,
        state=AppState.initial(),
    )
    yield runtime, transport, fake_fs
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_intent_triggers_workflow_build(runtime_with_fake_transport):
    runtime, transport, fs = runtime_with_fake_transport
    result = await runtime.submit_intent("Refactor the auth module to use bcrypt")

    assert result.status == "complete"
    # Workflow must have been built
    assert result.workflow is not None
    assert len(result.workflow.tasks) == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_file_written_by_worker_agent(runtime_with_fake_transport):
    runtime, transport, fs = runtime_with_fake_transport
    await runtime.submit_intent("Refactor the auth module to use bcrypt")

    assert fs.exists("auth.py"), "Worker agent should have written auth.py"
    content = fs.read("auth.py")
    assert "bcrypt" in content


@pytest.mark.asyncio
@pytest.mark.integration
async def test_memory_updated_after_run(runtime_with_fake_transport):
    runtime, transport, fs = runtime_with_fake_transport
    await runtime.submit_intent("Refactor the auth module to use bcrypt")

    memory = await runtime.get_memory_snapshot()
    # The completed intent should be stored
    assert any("auth" in str(v).lower() for v in memory.values())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_all_transport_responses_consumed(runtime_with_fake_transport):
    runtime, transport, fs = runtime_with_fake_transport
    await runtime.submit_intent("Refactor the auth module to use bcrypt")
    transport.assert_remaining(0)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_appstate_reflects_completed_tasks(runtime_with_fake_transport):
    runtime, transport, fs = runtime_with_fake_transport
    await runtime.submit_intent("Refactor the auth module to use bcrypt")

    state = runtime.current_state()
    statuses = [a.get("status") for a in state.agents.values()]
    assert all(s == "done" for s in statuses), f"Not all agents done: {statuses}"
```

---

### 6.2 `tests/integration/test_concurrent_agents.py`

```python
# tests/integration/test_concurrent_agents.py
"""
5 concurrent agents sharing a ProjectMemory instance.
Verifies no data races: every write is visible, no update is lost.
"""
from __future__ import annotations

import asyncio
import pytest

from agenthicc.memory import ProjectMemory  # type: ignore[import]
from agenthicc.agents import WorkerAgent  # type: ignore[import]
from agenthicc.config import AgentConfig  # type: ignore[import]
from tests.conftest import AsyncMockTransport

NUM_AGENTS = 5
WRITES_PER_AGENT = 10


def build_write_responses(agent_idx: int) -> list:
    """Each agent writes WRITES_PER_AGENT distinct keys."""
    responses = []
    for i in range(WRITES_PER_AGENT):
        responses.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Writing key agent{agent_idx}_item{i}"},
                {
                    "type": "tool_use",
                    "id": f"tu_{agent_idx}_{i}",
                    "name": "memory_set",
                    "input": {
                        "key": f"agent{agent_idx}_item{i}",
                        "value": f"value_from_agent_{agent_idx}_iteration_{i}",
                    },
                },
            ],
            "stop_reason": "tool_use",
        })
    responses.append("All writes complete.")
    return responses


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_writes_no_loss():
    """All WRITES_PER_AGENT * NUM_AGENTS writes must be present after concurrent execution."""
    memory = ProjectMemory()

    agents = []
    for idx in range(NUM_AGENTS):
        transport = AsyncMockTransport(responses=build_write_responses(idx))
        cfg = AgentConfig(id=f"agent-{idx}", role="worker", model="mock")
        agent = WorkerAgent(config=cfg, transport=transport, memory=memory)
        agents.append(agent)

    # Launch all agents concurrently
    await asyncio.gather(*[agent.run(f"Write your {WRITES_PER_AGENT} items") for agent in agents])

    snapshot = await memory.snapshot()
    total_expected = NUM_AGENTS * WRITES_PER_AGENT
    assert len(snapshot) >= total_expected, (
        f"Expected {total_expected} memory entries, found {len(snapshot)}. "
        f"Missing: {total_expected - len(snapshot)} entries."
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_reads_see_prior_writes():
    """Agent n+1 must be able to read keys written by agent n (sequential dependency)."""
    memory = ProjectMemory()

    # Pre-populate memory
    await memory.set("shared_key", "initial_value")

    read_results: list[str | None] = []

    async def reader_agent(agent_idx: int) -> None:
        value = await memory.get("shared_key")
        read_results.append(value)

    await asyncio.gather(*[reader_agent(i) for i in range(NUM_AGENTS)])

    assert all(v == "initial_value" for v in read_results), (
        f"Some agents read stale or None value: {read_results}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_increment_is_atomic():
    """Concurrent increments of a counter key must not lose updates (CAS or lock)."""
    memory = ProjectMemory()
    await memory.set("counter", 0)

    async def increment():
        for _ in range(20):
            await memory.increment("counter", delta=1)

    await asyncio.gather(*[increment() for _ in range(NUM_AGENTS)])

    final = await memory.get("counter")
    assert final == NUM_AGENTS * 20, (
        f"Expected counter={NUM_AGENTS * 20}, got {final}. Data race detected."
    )
```

---

### 6.3 `tests/integration/test_hook_recovery.py`

```python
# tests/integration/test_hook_recovery.py
"""
Integration test: on_error hook triggers sub-agent spawn and recovery.
Verifies the hook-recovery contract end-to-end without real LLM calls.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agenthicc.runtime import AgenticRuntime  # type: ignore[import]
from agenthicc.state import AppState  # type: ignore[import]
from agenthicc.hooks import HookRegistry, HookPhase  # type: ignore[import]
from tests.conftest import AsyncMockTransport, FakeFilesystem


FAILING_AGENT_RESPONSES = [
    # Agent tries a tool that will fail
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Attempting to process file."},
            {
                "type": "tool_use",
                "id": "tu_fail",
                "name": "process_file",
                "input": {"path": "bad.py"},
            },
        ],
        "stop_reason": "tool_use",
    },
]

RECOVERY_AGENT_RESPONSES = [
    "I have analysed the failure. Applying fix.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Fix applied."},
            {
                "type": "tool_use",
                "id": "tu_fix",
                "name": "write_file",
                "input": {"path": "bad.py", "content": "# fixed"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Recovery complete.",
]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_on_error_hook_spawns_recovery_agent(fake_fs):
    spawned_agents: list[str] = []

    primary_transport = AsyncMockTransport(responses=FAILING_AGENT_RESPONSES)
    recovery_transport = AsyncMockTransport(responses=RECOVERY_AGENT_RESPONSES)

    hooks = HookRegistry()

    async def recovery_hook(tool_name: str, exc: Exception, ctx) -> dict:
        spawned_agents.append("recovery-agent")
        # Simulate spawning recovery agent inline
        await ctx.spawn_agent(
            role="debugger",
            intent=f"Fix error in {tool_name}: {exc}",
            transport=recovery_transport,
        )
        return {"recovered": True}

    hooks.register_global(HookPhase.ON_ERROR, recovery_hook)

    # Install a tool that always fails
    async def failing_process_file(inputs: dict, ctx) -> dict:
        raise RuntimeError(f"Cannot process {inputs['path']}: file corrupt")

    runtime = AgenticRuntime(
        transport=primary_transport,
        filesystem=fake_fs,
        state=AppState.initial(),
        hooks=hooks,
        extra_tools={"process_file": failing_process_file},
    )

    result = await runtime.submit_intent("Process bad.py")

    assert "recovery-agent" in spawned_agents, "Recovery agent was not spawned"
    assert fake_fs.exists("bad.py"), "Recovery agent should have written the fixed file"
    assert fake_fs.read("bad.py") == "# fixed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_error_state_recorded_before_recovery(fake_fs):
    """AppState must record the error event even when recovery succeeds."""
    primary_transport = AsyncMockTransport(responses=FAILING_AGENT_RESPONSES)
    recovery_transport = AsyncMockTransport(responses=RECOVERY_AGENT_RESPONSES)
    hooks = HookRegistry()

    async def recovery_hook(tool_name, exc, ctx):
        await ctx.spawn_agent(role="debugger", intent="fix it", transport=recovery_transport)
        return {"recovered": True}

    hooks.register_global(HookPhase.ON_ERROR, recovery_hook)

    async def failing_tool(inputs, ctx):
        raise RuntimeError("boom")

    runtime = AgenticRuntime(
        transport=primary_transport,
        filesystem=fake_fs,
        state=AppState.initial(),
        hooks=hooks,
        extra_tools={"process_file": failing_tool},
    )

    await runtime.submit_intent("Process bad.py")

    state = runtime.current_state()
    error_events = [e for e in state.event_log if "error" in e.get("type", "")]
    assert error_events, "Error event should be in state.event_log even after recovery"
```

---

### 6.4 `tests/integration/test_event_log.py`

```python
# tests/integration/test_event_log.py
"""
Persist event log mid-run, restore AppState, verify deterministic re-execution.
This tests the crash-recovery / replay semantics of the event log.
"""
from __future__ import annotations

import asyncio
import json
import pytest
import pytest_asyncio

from agenthicc.runtime import AgenticRuntime  # type: ignore[import]
from agenthicc.state import AppState  # type: ignore[import]
from agenthicc.events import EventLog  # type: ignore[import]
from tests.conftest import AsyncMockTransport, FakeFilesystem


FULL_RESPONSES = [
    "Step 1: analysing codebase.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 2: writing output."},
            {
                "type": "tool_use",
                "id": "tu_write",
                "name": "write_file",
                "input": {"path": "output.txt", "content": "hello world"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Step 3: done.",
]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_event_log_persists_mid_run(tmp_path, fake_fs):
    """Events emitted during a run are persisted to the log file."""
    log_path = str(tmp_path / "events.jsonl")
    transport = AsyncMockTransport(responses=list(FULL_RESPONSES))

    runtime = AgenticRuntime(
        transport=transport,
        filesystem=fake_fs,
        state=AppState.initial(),
        event_log_path=log_path,
    )

    await runtime.submit_intent("Do three steps")

    log = EventLog.load(log_path)
    assert len(log.events) > 0, "Event log should contain events after run"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_appstate_restores_from_event_log(tmp_path, fake_fs):
    """Replaying an event log must reproduce the same AppState as a live run."""
    log_path = str(tmp_path / "events.jsonl")

    # First run: live
    transport1 = AsyncMockTransport(responses=list(FULL_RESPONSES))
    runtime1 = AgenticRuntime(
        transport=transport1,
        filesystem=fake_fs,
        state=AppState.initial(),
        event_log_path=log_path,
    )
    await runtime1.submit_intent("Do three steps")
    live_state = runtime1.current_state()

    # Second run: replay from log
    replayed_state = AppState.replay(EventLog.load(log_path))

    # Structural equality (ignore timestamps)
    assert replayed_state.agents == live_state.agents
    assert replayed_state.workflow == live_state.workflow
    assert replayed_state.intent == live_state.intent


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deterministic_reexecution_from_log(tmp_path):
    """Re-running with same scripted responses and same log checkpoint yields same result."""
    log_path = str(tmp_path / "events.jsonl")

    for run_idx in range(2):
        fs = FakeFilesystem()
        transport = AsyncMockTransport(responses=list(FULL_RESPONSES))
        runtime = AgenticRuntime(
            transport=transport,
            filesystem=fs,
            state=AppState.initial(),
            event_log_path=log_path if run_idx == 0 else None,
        )
        await runtime.submit_intent("Do three steps")

        if run_idx == 0:
            snapshot_0 = fs.snapshot()
            state_0 = runtime.current_state()
        else:
            snapshot_1 = fs.snapshot()
            state_1 = runtime.current_state()

    assert snapshot_0 == snapshot_1, "Filesystem snapshots differ between runs"
    assert state_0.intent == state_1.intent


@pytest.mark.asyncio
@pytest.mark.integration
async def test_event_log_entries_are_valid_json(tmp_path, fake_fs):
    log_path = str(tmp_path / "events.jsonl")
    transport = AsyncMockTransport(responses=list(FULL_RESPONSES))
    runtime = AgenticRuntime(
        transport=transport,
        filesystem=fake_fs,
        state=AppState.initial(),
        event_log_path=log_path,
    )
    await runtime.submit_intent("Do three steps")

    with open(log_path) as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f"Line {lineno} of event log is not valid JSON: {exc}\n  {line!r}")
```

---

## 7. End-to-End Tests

### 7.1 `tests/e2e/test_argon2_scenario.py`

```python
# tests/e2e/test_argon2_scenario.py
"""
Full Argon2 refactor scenario:
  - 3 parallel agents: refactor / test / docs
  - test agent fails
  - debugger spawned automatically
  - fix applied
  - docs updated
  - final result is correct

Uses AsyncMockTransport with fully scripted responses.
"""
from __future__ import annotations

import pytest

from agenthicc.runtime import AgenticRuntime  # type: ignore[import]
from agenthicc.state import AppState  # type: ignore[import]
from tests.conftest import AsyncMockTransport, FakeFilesystem

# ---------------------------------------------------------------------------
# Scripted transport sequences per agent role
# ---------------------------------------------------------------------------

ORCHESTRATOR_RESPONSES = [
    # Build parallel workflow
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Planning parallel refactor of Argon2 password hashing."},
            {
                "type": "tool_use",
                "id": "tu_plan",
                "name": "build_workflow",
                "input": {
                    "tasks": [
                        {"id": "refactor", "description": "Rewrite password.py to use argon2-cffi"},
                        {"id": "tests",    "description": "Update test_password.py for argon2"},
                        {"id": "docs",     "description": "Update docs/auth.md for argon2"},
                    ]
                },
            },
        ],
        "stop_reason": "tool_use",
    },
    # After debugger fixes tests, re-check
    "All agents completed. Argon2 migration done.",
]

REFACTOR_AGENT_RESPONSES = [
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Rewriting password.py to use argon2-cffi."},
            {
                "type": "tool_use",
                "id": "tu_refactor",
                "name": "write_file",
                "input": {
                    "path": "password.py",
                    "content": (
                        "import argon2\n"
                        "ph = argon2.PasswordHasher()\n\n"
                        "def hash_password(pwd: str) -> str:\n"
                        "    return ph.hash(pwd)\n\n"
                        "def verify_password(hash: str, pwd: str) -> bool:\n"
                        "    return ph.verify(hash, pwd)\n"
                    ),
                },
            },
        ],
        "stop_reason": "tool_use",
    },
    "Refactor complete.",
]

TEST_AGENT_RESPONSES_FIRST = [
    # First attempt: produces broken test file
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Writing tests — but using wrong import."},
            {
                "type": "tool_use",
                "id": "tu_test_bad",
                "name": "write_file",
                "input": {
                    "path": "test_password.py",
                    "content": (
                        "import argon2_cffi  # wrong package name\n"
                        "def test_hash(): pass\n"
                    ),
                },
            },
        ],
        "stop_reason": "tool_use",
    },
    "Tests written (may have import issue).",
]

DEBUGGER_RESPONSES = [
    "Detected wrong import: argon2_cffi -> should be argon2.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Fixing test import."},
            {
                "type": "tool_use",
                "id": "tu_fix_test",
                "name": "write_file",
                "input": {
                    "path": "test_password.py",
                    "content": (
                        "import argon2\n"
                        "from password import hash_password, verify_password\n\n"
                        "def test_hash_and_verify():\n"
                        "    h = hash_password('secret')\n"
                        "    assert verify_password(h, 'secret')\n"
                    ),
                },
            },
        ],
        "stop_reason": "tool_use",
    },
    "Tests fixed and verified.",
]

DOCS_AGENT_RESPONSES = [
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Updating auth docs for argon2."},
            {
                "type": "tool_use",
                "id": "tu_docs",
                "name": "write_file",
                "input": {
                    "path": "docs/auth.md",
                    "content": "# Auth\n\nPasswords are hashed using **argon2-cffi**.\n",
                },
            },
        ],
        "stop_reason": "tool_use",
    },
    "Docs updated.",
]


@pytest.fixture
def argon2_runtime(fake_fs):
    # Pre-create directory in fake fs
    fake_fs.write("docs/.keep", "")

    transports = {
        "orchestrator": AsyncMockTransport(responses=ORCHESTRATOR_RESPONSES),
        "refactor":     AsyncMockTransport(responses=REFACTOR_AGENT_RESPONSES),
        "tests":        AsyncMockTransport(responses=TEST_AGENT_RESPONSES_FIRST),
        "debugger":     AsyncMockTransport(responses=DEBUGGER_RESPONSES),
        "docs":         AsyncMockTransport(responses=DOCS_AGENT_RESPONSES),
    }

    runtime = AgenticRuntime(
        transport=transports["orchestrator"],
        filesystem=fake_fs,
        state=AppState.initial(),
        agent_transports=transports,  # pool override per role
    )
    return runtime, transports, fake_fs


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_argon2_refactor_creates_all_files(argon2_runtime):
    runtime, transports, fs = argon2_runtime
    await runtime.submit_intent("Migrate password hashing from bcrypt to argon2-cffi")

    assert fs.exists("password.py"), "password.py must be written by refactor agent"
    assert fs.exists("test_password.py"), "test_password.py must be written (and fixed)"
    assert fs.exists("docs/auth.md"), "docs/auth.md must be written by docs agent"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_argon2_refactor_password_file_uses_argon2(argon2_runtime):
    runtime, transports, fs = argon2_runtime
    await runtime.submit_intent("Migrate password hashing from bcrypt to argon2-cffi")

    content = fs.read("password.py")
    assert "argon2" in content
    assert "bcrypt" not in content


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_argon2_test_file_fixed_by_debugger(argon2_runtime):
    runtime, transports, fs = argon2_runtime
    await runtime.submit_intent("Migrate password hashing from bcrypt to argon2-cffi")

    content = fs.read("test_password.py")
    # Debugger must have corrected the import
    assert "argon2_cffi" not in content, "Broken import was not fixed by debugger"
    assert "import argon2" in content


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_argon2_debugger_was_spawned(argon2_runtime):
    runtime, transports, fs = argon2_runtime
    await runtime.submit_intent("Migrate password hashing from bcrypt to argon2-cffi")

    debugger_transport = transports["debugger"]
    assert debugger_transport.calls, "Debugger transport was never called — debugger not spawned"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_argon2_docs_mention_argon2(argon2_runtime):
    runtime, transports, fs = argon2_runtime
    await runtime.submit_intent("Migrate password hashing from bcrypt to argon2-cffi")

    content = fs.read("docs/auth.md")
    assert "argon2" in content.lower()
```

---

### 7.2 `tests/e2e/test_tui_e2e.py`

```python
# tests/e2e/test_tui_e2e.py
"""
TUI end-to-end tests using a PTY and pyte vt100 emulator.
Verifies:
  - Input bar stays pinned at bottom during transcript scroll
  - Menus pop up above the input bar (never below/overlapping)
  - No rendering artefacts after resize
"""
from __future__ import annotations

import asyncio
import os
import pty
import re
import struct
import fcntl
import termios
import signal

import pytest

try:
    import pyte  # type: ignore[import]
    HAS_PYTE = True
except ImportError:
    HAS_PYTE = False

pytestmark = pytest.mark.skipif(not HAS_PYTE, reason="pyte not installed")

ROWS = 24
COLS = 80


class PTYSession:
    """Spawn agenthicc TUI in a PTY and feed it through a pyte screen."""

    def __init__(self, rows: int = ROWS, cols: int = COLS) -> None:
        self.rows = rows
        self.cols = cols
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self._master_fd: int | None = None
        self._pid: int | None = None

    def start(self, cmd: list[str]) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: exec the TUI
            os.execvp(cmd[0], cmd)
        else:
            self._pid = pid
            self._master_fd = master_fd
            self._set_winsize(master_fd, self.rows, self.cols)

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def read_until_stable(self, timeout: float = 2.0) -> None:
        """Read from PTY until output stabilises."""
        import select
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([self._master_fd], [], [], 0.05)
            if r:
                data = os.read(self._master_fd, 4096)
                self.stream.feed(data)

    def send(self, text: str) -> None:
        os.write(self._master_fd, text.encode())

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.screen.resize(rows, cols)
        self._set_winsize(self._master_fd, rows, cols)
        if self._pid:
            os.kill(self._pid, signal.SIGWINCH)

    def stop(self) -> None:
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
                os.waitpid(self._pid, 0)
            except ProcessLookupError:
                pass
        if self._master_fd is not None:
            os.close(self._master_fd)

    def last_row(self) -> str:
        return self.screen.display[self.rows - 1]

    def row(self, idx: int) -> str:
        return self.screen.display[idx]

    def all_rows(self) -> list[str]:
        return list(self.screen.display)


@pytest.fixture
def pty_session():
    session = PTYSession(rows=ROWS, cols=COLS)
    yield session
    session.stop()


@pytest.mark.e2e
def test_input_bar_at_bottom_on_start(pty_session):
    """The input bar (prompt '> ') must appear on the last row at startup."""
    pty_session.start(["python", "-m", "agenthicc.tui", "--headless-test"])
    pty_session.read_until_stable(timeout=3.0)

    last = pty_session.last_row()
    assert ">" in last or "Input" in last, (
        f"Expected input bar on last row, got: {last!r}"
    )


@pytest.mark.e2e
def test_input_bar_stays_at_bottom_after_scroll(pty_session):
    """Scrolling through transcript must not push input bar up."""
    pty_session.start(["python", "-m", "agenthicc.tui", "--headless-test", "--fill-transcript=30"])
    pty_session.read_until_stable(timeout=3.0)

    # Scroll up through transcript
    for _ in range(10):
        pty_session.send("\x1b[A")  # cursor up (scroll)
    pty_session.read_until_stable(timeout=1.0)

    last = pty_session.last_row()
    assert ">" in last or "Input" in last, (
        f"Input bar moved off last row after scrolling. Last row: {last!r}\n"
        f"All rows:\n" + "\n".join(f"  {i:2d}: {r}" for i, r in enumerate(pty_session.all_rows()))
    )


@pytest.mark.e2e
def test_menu_appears_above_input_bar(pty_session):
    """Pressing '/' to open command menu must show menu rows above input bar."""
    pty_session.start(["python", "-m", "agenthicc.tui", "--headless-test"])
    pty_session.read_until_stable(timeout=3.0)

    pty_session.send("/")  # open command menu
    pty_session.read_until_stable(timeout=1.0)

    rows = pty_session.all_rows()
    last = rows[-1]

    # Find which row contains menu items
    menu_rows = [i for i, r in enumerate(rows) if "clear" in r.lower() or "help" in r.lower()]

    assert menu_rows, "No menu rows found after pressing '/'"
    assert all(r < ROWS - 1 for r in menu_rows), (
        f"Menu rows {menu_rows} overlap with or are below input bar (row {ROWS - 1}). "
        f"Last row: {last!r}"
    )


@pytest.mark.e2e
def test_no_artefacts_after_resize(pty_session):
    """After a terminal resize, the screen must not have stray cursor or layout artefacts."""
    pty_session.start(["python", "-m", "agenthicc.tui", "--headless-test"])
    pty_session.read_until_stable(timeout=3.0)

    pty_session.resize(rows=30, cols=100)
    pty_session.read_until_stable(timeout=1.0)

    rows = pty_session.all_rows()
    # No ESC characters should be visible as literal text
    for i, row in enumerate(rows):
        assert "\x1b" not in row, f"Stray ESC in row {i}: {row!r}"
```

---

### 7.3 `tests/e2e/test_headless_api.py`

```python
# tests/e2e/test_headless_api.py
"""
Headless API E2E test:
  - httpx AsyncClient submits intent via POST /intent
  - WebSocket subscribes to event stream at /ws/events
  - Verifies "intent.completed" event arrives within timeout
"""
from __future__ import annotations

import asyncio
import json
import pytest
import pytest_asyncio

try:
    import httpx  # type: ignore[import]
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

pytestmark = pytest.mark.skipif(not HAS_HTTPX, reason="httpx not installed")

from tests.conftest import AsyncMockTransport  # noqa: E402
from agenthicc.server import create_app  # type: ignore[import]  # noqa: E402


MOCK_RESPONSES = [
    "Understood. Analysing request.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Completing task."},
            {
                "type": "tool_use",
                "id": "tu_api_test",
                "name": "write_file",
                "input": {"path": "api_result.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Task complete.",
]


@pytest_asyncio.fixture
async def test_app():
    transport = AsyncMockTransport(responses=MOCK_RESPONSES)
    app = create_app(transport=transport, test_mode=True)
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        yield client, app


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_submit_intent_returns_202(test_app):
    client, app = test_app
    response = await client.post(
        "/intent",
        json={"intent": "Do something useful"},
    )
    assert response.status_code == 202, f"Expected 202 Accepted, got {response.status_code}"
    body = response.json()
    assert "run_id" in body, "Response must include a run_id"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_event_stream_delivers_completion(test_app):
    client, app = test_app

    # Submit intent
    response = await client.post("/intent", json={"intent": "Test event stream"})
    run_id = response.json()["run_id"]

    # Collect events from SSE stream
    received_events: list[dict] = []
    async with client.stream("GET", f"/events/{run_id}") as stream:
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    event = json.loads(payload)
                    received_events.append(event)
                    if event.get("type") == "intent.completed":
                        break

    event_types = [e.get("type") for e in received_events]
    assert "intent.completed" in event_types, (
        f"'intent.completed' not found in event stream. Received: {event_types}"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_get_run_status_after_completion(test_app):
    client, app = test_app

    submit = await client.post("/intent", json={"intent": "Status test"})
    run_id = submit.json()["run_id"]

    # Poll until done
    for _ in range(20):
        status_resp = await client.get(f"/runs/{run_id}")
        if status_resp.json().get("status") == "complete":
            break
        await asyncio.sleep(0.1)

    final = await client.get(f"/runs/{run_id}")
    assert final.json()["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_invalid_intent_returns_422(test_app):
    client, _ = test_app
    response = await client.post("/intent", json={})  # missing "intent" field
    assert response.status_code == 422
```

---

### 7.4 `tests/e2e/test_crash_recovery.py`

```python
# tests/e2e/test_crash_recovery.py
"""
Crash recovery E2E test:
  1. Run to 50% completion, capture event log snapshot
  2. Create a new runtime from that snapshot
  3. Complete the remaining work
  4. Verify final state matches a full uninterrupted run
"""
from __future__ import annotations

import asyncio
import json
import pytest
import pytest_asyncio

from agenthicc.runtime import AgenticRuntime  # type: ignore[import]
from agenthicc.state import AppState  # type: ignore[import]
from agenthicc.events import EventLog  # type: ignore[import]
from tests.conftest import AsyncMockTransport, FakeFilesystem


# Six-step workflow — crash after step 3
STEP_RESPONSES = [
    # Steps 1-3 (pre-crash)
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 1."},
            {
                "type": "tool_use",
                "id": "tu_s1",
                "name": "write_file",
                "input": {"path": "step1.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Step 1 complete.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 2."},
            {
                "type": "tool_use",
                "id": "tu_s2",
                "name": "write_file",
                "input": {"path": "step2.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Step 2 complete.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 3."},
            {
                "type": "tool_use",
                "id": "tu_s3",
                "name": "write_file",
                "input": {"path": "step3.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Step 3 complete.",
    # Steps 4-6 (post-crash)
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 4."},
            {
                "type": "tool_use",
                "id": "tu_s4",
                "name": "write_file",
                "input": {"path": "step4.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Step 4 complete.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 5."},
            {
                "type": "tool_use",
                "id": "tu_s5",
                "name": "write_file",
                "input": {"path": "step5.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "Step 5 complete.",
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Step 6."},
            {
                "type": "tool_use",
                "id": "tu_s6",
                "name": "write_file",
                "input": {"path": "step6.txt", "content": "done"},
            },
        ],
        "stop_reason": "tool_use",
    },
    "All 6 steps complete.",
]


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_crash_recovery_produces_same_final_state(tmp_path):
    """
    Two runs:
      Run A: uninterrupted full run
      Run B: crash at step 3, resume from snapshot, complete

    Final filesystem snapshots must be equal.
    """
    checkpoint_path = str(tmp_path / "checkpoint.jsonl")

    # --- Run A: full uninterrupted ---
    fs_a = FakeFilesystem()
    transport_a = AsyncMockTransport(responses=list(STEP_RESPONSES))
    runtime_a = AgenticRuntime(
        transport=transport_a, filesystem=fs_a, state=AppState.initial()
    )
    await runtime_a.submit_intent("Execute 6 steps")
    snapshot_a = fs_a.snapshot()

    # --- Run B: first half (steps 1-3) ---
    fs_b = FakeFilesystem()
    # Give only first-half responses
    transport_b1 = AsyncMockTransport(responses=STEP_RESPONSES[:6])
    runtime_b1 = AgenticRuntime(
        transport=transport_b1,
        filesystem=fs_b,
        state=AppState.initial(),
        event_log_path=checkpoint_path,
        stop_after_events=6,  # Simulate crash after 6 events
    )
    await runtime_b1.submit_intent("Execute 6 steps")
    mid_log = EventLog.load(checkpoint_path)
    mid_state = AppState.replay(mid_log)

    # --- Run B: resume from checkpoint ---
    transport_b2 = AsyncMockTransport(responses=STEP_RESPONSES[6:])
    runtime_b2 = AgenticRuntime(
        transport=transport_b2,
        filesystem=fs_b,  # same filesystem — preserves prior writes
        state=mid_state,  # restored state
        resume_from_log=mid_log,
    )
    await runtime_b2.resume()

    snapshot_b = fs_b.snapshot()

    # All 6 files must exist in both
    for step in range(1, 7):
        key = f"step{step}.txt"
        assert key in snapshot_a, f"{key} missing from full run A"
        assert key in snapshot_b, f"{key} missing from crash-recovery run B"

    assert snapshot_a == snapshot_b, (
        f"Filesystem snapshots differ:\n"
        f"  Only in A: {set(snapshot_a) - set(snapshot_b)}\n"
        f"  Only in B: {set(snapshot_b) - set(snapshot_a)}"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_no_duplicate_tool_calls_after_recovery(tmp_path):
    """Files written before crash must not be written again after recovery."""
    checkpoint_path = str(tmp_path / "checkpoint.jsonl")
    write_counts: dict[str, int] = {}

    fs = FakeFilesystem()

    original_write = fs.write

    def counting_write(path: str, content: str) -> None:
        write_counts[path] = write_counts.get(path, 0) + 1
        original_write(path, content)

    fs.write = counting_write

    # First half
    transport_1 = AsyncMockTransport(responses=STEP_RESPONSES[:6])
    runtime_1 = AgenticRuntime(
        transport=transport_1,
        filesystem=fs,
        state=AppState.initial(),
        event_log_path=checkpoint_path,
        stop_after_events=6,
    )
    await runtime_1.submit_intent("Execute 6 steps")

    mid_state = AppState.replay(EventLog.load(checkpoint_path))

    # Second half
    transport_2 = AsyncMockTransport(responses=STEP_RESPONSES[6:])
    runtime_2 = AgenticRuntime(
        transport=transport_2,
        filesystem=fs,
        state=mid_state,
        resume_from_log=EventLog.load(checkpoint_path),
    )
    await runtime_2.resume()

    # No file should be written more than once
    duplicates = {k: v for k, v in write_counts.items() if v > 1}
    assert not duplicates, f"Files written more than once (duplicate tool calls): {duplicates}"
```

---

## 8. CI Configuration

### 8.1 `noxfile.py`

```python
# noxfile.py
"""
Nox sessions for Agenthicc test pipeline.

Usage:
    nox -s unit               # fast unit tests only
    nox -s integration        # integration tests
    nox -s e2e                # end-to-end tests
    nox -s coverage           # full run with coverage report
    nox -s lint               # ruff + mypy
    nox                       # run all sessions
"""
from __future__ import annotations

import nox

nox.options.sessions = ["lint", "unit", "integration", "e2e"]
nox.options.reuse_existing_virtualenvs = True

PYTHON_VERSIONS = ["3.11", "3.12"]
SRC_DIRS = ["agenthicc", "tests"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_deps(session: nox.Session, extras: list[str] | None = None) -> None:
    session.install("-e", ".[test]" if not extras else f".[test,{','.join(extras)}]")


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

@nox.session(python=PYTHON_VERSIONS, tags=["lint"])
def lint(session: nox.Session) -> None:
    """Run ruff (format + lint) and mypy."""
    session.install("ruff", "mypy")
    session.run("ruff", "check", *SRC_DIRS)
    session.run("ruff", "format", "--check", *SRC_DIRS)
    session.run("mypy", "agenthicc", "--ignore-missing-imports")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@nox.session(python=PYTHON_VERSIONS, tags=["test", "unit"])
def unit(session: nox.Session) -> None:
    """
    Fast, isolated unit tests. No network, no subprocess, no real disk I/O.
    Target runtime: < 30 seconds.
    """
    _install_deps(session)
    session.run(
        "pytest",
        "tests/unit/",
        "-v",
        "--tb=short",
        "-m", "unit",
        "--timeout=10",
        "--cov=agenthicc",
        "--cov-report=term-missing",
        "--cov-fail-under=90",
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@nox.session(python=PYTHON_VERSIONS, tags=["test", "integration"])
def integration(session: nox.Session) -> None:
    """
    Integration tests wiring multiple subsystems together.
    Uses AsyncMockTransport, FakeFilesystem. No real LLM calls.
    Target runtime: < 3 minutes.
    """
    _install_deps(session)
    session.run(
        "pytest",
        "tests/integration/",
        "-v",
        "--tb=short",
        "-m", "integration",
        "--timeout=60",
        "--cov=agenthicc",
        "--cov-append",
        "--cov-report=term-missing",
        "--cov-fail-under=90",
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------

@nox.session(python=PYTHON_VERSIONS, tags=["test", "e2e"])
def e2e(session: nox.Session) -> None:
    """
    End-to-end tests. May launch subprocesses or PTY sessions.
    Target runtime: < 10 minutes.
    """
    _install_deps(session, extras=["e2e"])
    session.run(
        "pytest",
        "tests/e2e/",
        "-v",
        "--tb=long",
        "-m", "e2e",
        "--timeout=120",
        "--cov=agenthicc",
        "--cov-append",
        "--cov-report=term-missing",
        "--cov-fail-under=85",  # E2E covers less granular paths
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# Full coverage report
# ---------------------------------------------------------------------------

@nox.session(python="3.12", tags=["coverage"])
def coverage(session: nox.Session) -> None:
    """Run all test layers and produce an HTML + XML coverage report."""
    _install_deps(session, extras=["e2e"])
    session.run(
        "pytest",
        "tests/",
        "-v",
        "--tb=short",
        "--cov=agenthicc",
        "--cov-report=html:htmlcov",
        "--cov-report=xml:coverage.xml",
        "--cov-report=term-missing",
        "--cov-fail-under=90",
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# Hypothesis database management
# ---------------------------------------------------------------------------

@nox.session(python="3.12", tags=["hypothesis"])
def hypothesis_ci(session: nox.Session) -> None:
    """Run Hypothesis with CI profile (200 examples, strict deadline)."""
    _install_deps(session)
    session.env["HYPOTHESIS_PROFILE"] = "ci"
    session.run(
        "pytest",
        "tests/unit/",
        "-v",
        "-m", "unit",
        "--tb=short",
        *session.posargs,
    )
```

---

### 8.2 `pyproject.toml` — pytest configuration

```toml
# pyproject.toml (pytest and coverage sections)

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = [
    "--strict-markers",
    "--tb=short",
    "-ra",
]
markers = [
    "unit: fast, isolated unit tests (no network, no subprocess)",
    "integration: multi-subsystem tests with in-memory fakes",
    "e2e: full end-to-end tests (may use PTY or subprocess)",
    "slow: tests that take more than 5 seconds",
    "hypothesis: property-based tests using Hypothesis",
]
filterwarnings = [
    "error",
    "ignore::DeprecationWarning:hypothesis",
    "ignore::PendingDeprecationWarning",
]

[tool.coverage.run]
source = ["agenthicc"]
branch = true
omit = [
    "agenthicc/_vendor/*",
    "agenthicc/__main__.py",
    "tests/*",
]

[tool.coverage.report]
show_missing = true
precision = 2
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
    "@overload",
    "\\.\\.\\.",
]

[tool.coverage.html]
directory = "htmlcov"

[tool.coverage.xml]
output = "coverage.xml"
```

---

## 9. Coverage Requirements

| Layer | Module | Minimum Coverage |
|---|---|---|
| Unit | `agenthicc.state` | 95% |
| Unit | `agenthicc.dag` | 95% |
| Unit | `agenthicc.tools.communication` | 90% |
| Unit | `agenthicc.hooks` | 90% |
| Unit | `agenthicc.tui.diff` | 90% |
| Unit | `agenthicc.config.loader` | 90% |
| Integration | `agenthicc.runtime` | 80% |
| Integration | `agenthicc.memory` | 85% |
| Integration | `agenthicc.events` | 85% |
| E2E | `agenthicc.server` | 75% |
| **Overall** | **All modules** | **90%** |

Coverage is measured via `pytest-cov` with branch coverage enabled. The CI gate (`--cov-fail-under=90`) blocks merge if the threshold is not met. Per-module floors are enforced by a custom coverage plugin defined in `tests/plugins/coverage_guard.py`.

### Exclusions

The following patterns are explicitly excluded from coverage measurement:

- `if TYPE_CHECKING:` blocks (type annotations only)
- `raise NotImplementedError` stubs in abstract base classes
- `@overload` decorator stubs
- `...` (Ellipsis body) in protocols and abstract methods
- `__main__.py` entry point boilerplate

---

## 10. lauren-ai Testing Utilities Reference

The following lauren-ai classes and patterns are used throughout this test suite.

| Symbol | Module | Role in Tests |
|---|---|---|
| `AgentRunnerBase` | `lauren_ai._agents._runner` | Drives agent loops in integration tests; call `.step()` to advance one turn |
| `SignalBus` | `lauren_ai._signals` | Spy on signal emissions; `SignalBus.assert_emitted(signal_name)` |
| `ShortTermMemory` | `lauren_ai._memory` | Construct pre-populated memory for agent context fixtures |
| `ToolHook` | `lauren_ai._tools._hooks` | Wraps tool execution; base class for `HookRegistry` hooks |
| `AgentContext` | `lauren_ai._context` | Build test contexts without a real runtime |
| `ToolContext` | `lauren_ai._context` | Build minimal tool execution contexts |
| `InMemoryConversationStore` | `lauren_ai._memory._stores` | Replacement for production conversation storage in tests |
| `InMemoryVectorStore` | `lauren_ai._memory._stores` | Replacement for production vector DB in tests |
| `MockTransport` | `lauren_ai.testing` | Base class extended by `AsyncMockTransport` in `conftest.py` |

### `AsyncMockTransport` extension pattern

```python
from lauren_ai.testing import MockTransport

# AsyncMockTransport in conftest.py is intentionally NOT a subclass of
# MockTransport — it reimplements the interface so tests have zero
# dependency on lauren-ai internal test utilities (which may change).
# If you want lauren-ai's richer transport features, subclass MockTransport:

class RichMockTransport(MockTransport):
    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages, **kwargs):
        return self._responses.pop(0)
```

### SignalBus assertion pattern

```python
# tests/integration/test_signals.py (example)
from lauren_ai._signals import SignalBus

async def test_agent_emits_completion_signal():
    bus = SignalBus()
    # ... run agent ...
    bus.assert_emitted("agent.completed")
    bus.assert_emitted("tool.called", count=3)
    bus.assert_not_emitted("agent.error")
```

### InMemoryConversationStore usage

```python
from lauren_ai._memory._stores import InMemoryConversationStore

@pytest.fixture
def conversation_store():
    return InMemoryConversationStore()

async def test_memory_retrieval(conversation_store):
    await conversation_store.append("session-1", {"role": "user", "content": "hello"})
    turns = await conversation_store.get("session-1")
    assert turns[0]["content"] == "hello"
```

---

## 11. Open Questions

| # | Question | Owner | Priority |
|---|---|---|---|
| OQ-01 | Should `test_tui_e2e.py` tests run in CI on every PR, or only on release branches? PTY tests are fragile on headless runners without a real terminal emulator. | Platform | High |
| OQ-02 | `test_concurrent_agents.py` uses `asyncio.gather` to simulate concurrency. Does this adequately model production thread contention? Should we add a `ThreadPoolExecutor`-based variant? | Backend | Medium |
| OQ-03 | `test_crash_recovery.py` uses a `stop_after_events` parameter on `AgenticRuntime`. This parameter does not yet exist. Is this the right API surface, or should crash simulation use `signal.raise_signal(SIGKILL)` in a subprocess? | Backend | High |
| OQ-04 | Hypothesis strategies for `test_appstate_reducers.py` currently generate simple string agent IDs. Should we add strategies that generate realistic nested workflows to catch deeper reducer bugs? | QA | Low |
| OQ-05 | The `FakeFilesystem` fixture does not implement file locking. If `ProjectMemory` uses file locks in production, `test_concurrent_agents.py` would not catch lock-related races. Should `FakeFilesystem` simulate lock contention? | Backend | Medium |
| OQ-06 | Coverage gate is set at 90% overall. Is this achievable at launch given the TUI render path is hard to unit-test? Consider setting a lower per-module floor for `agenthicc.tui.*` (e.g. 70%). | QA | Low |
| OQ-07 | `test_headless_api.py` imports `websockets` directly for WS tests. Should we use `httpx`'s built-in WS support (`httpx-ws`) instead for consistency? | Backend | Low |
| OQ-08 | `noxfile.py` targets Python 3.11 and 3.12. Should Python 3.10 be added to the matrix to support older deployment environments? | Platform | Medium |
| OQ-09 | Should `AsyncMockTransport.stream()` be promoted to a first-class fixture that verifies streaming chunk boundaries, or is testing only the `complete()` path sufficient for now? | QA | Low |
| OQ-10 | The argon2 E2E scenario has a hardcoded debugger-spawn trigger tied to a wrong import string. Should the debugger spawn be driven by a real test-runner tool call that reports `ImportError`, making the test more realistic? | QA | Medium |
