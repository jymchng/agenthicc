"""The subagent panel, transcripts, stopping one worker and named approvals (AGX-08).

Workers run against lauren-ai's mock transport through the real runner, so
tool calls, signals, approvals and memory are the production ones.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.subagents.pool import SubagentPool, SubagentTask, active_pool
from agenthicc.subagents.transcripts import list_pools, read_transcript
from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
from agenthicc.tools.approval import ApprovalRequest, ApprovalRequester, ApprovalResponse
from agenthicc.tools.capabilities import tool_write
from agenthicc.tui.conversation_store import AppState, ConversationStore

pytestmark = pytest.mark.integration


def _final(text: str, identifier: str) -> Completion:
    return Completion(
        id=identifier,
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=30, output_tokens=12),
    )


def _registry(name: str, *tool_names: str) -> SubagentTypeRegistry:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name=name,
            allowed_tools=frozenset(tool_names),
            max_turns=3,
            system_prompt="Use the tool and report what you found.",
            max_turn_time_s=20.0,
        )
    )
    return registry


def _blocking_reader(release: asyncio.Event, started: dict[str, float]) -> object:
    @tool()
    async def read_source(path: str) -> str:
        """Read a source file; the test decides when the read finishes."""
        started[path] = time.monotonic()
        await release.wait()
        return f"contents of {path}"

    return read_source


async def _wait_for(condition: object, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_each_row_shows_its_workers_tool_before_the_tool_runs(tmp_path: Path) -> None:
    release = asyncio.Event()
    started: dict[str, float] = {}
    reader = _blocking_reader(release, started)
    transport = MockTransport()
    for index in range(4):
        transport.queue_tool_use(
            "read_source", {"path": f"src/file_{index}.py"}, tool_use_id=f"call-{index}"
        )
    for index in range(4):
        transport.queue_response(_final(f"Read file {index}. It is fine.", f"final-{index}"))
    conversation = ConversationStore()
    shown_at: dict[str, float] = {}

    def watch() -> None:
        state = conversation.subagent_pool_state()
        for worker in state.workers if state is not None else ():
            if worker.activity.startswith("read_source"):
                shown_at.setdefault(worker.activity.split()[-1], time.monotonic())

    conversation.subagent_pool_state.subscribe(watch)
    pool = SubagentPool(
        [SubagentTask(f"task-{i}", "explorer", f"Read file {i}") for i in range(4)],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="mock-model",
        all_tools=[reader],
        max_concurrent=4,
        conv_store=conversation,
        registry=_registry("explorer", "read_source"),
        transcript_root=tmp_path / "subagents",
    )
    run = asyncio.create_task(pool.run())
    await _wait_for(lambda: len(started) == 4)
    assert active_pool(pool.pool_id) is pool
    state = conversation.subagent_pool_state()
    assert state is not None
    assert {worker.activity for worker in state.workers} == {
        f"read_source src/file_{index}.py" for index in range(4)
    }
    assert all(worker.status == "running" for worker in state.workers)
    for path, body_started in started.items():
        # The row changed when the call started, before the tool body ran.
        assert shown_at[path] <= body_started + 0.1
    release.set()
    result = await run
    assert result.succeeded == 4
    finished = conversation.subagent_pool_state()
    assert finished is not None and finished.finished_at is not None
    assert [worker.status for worker in finished.workers] == ["done"] * 4
    # The mock's tool call (50 + 20) and the final answer (30 + 12).
    assert all(worker.tokens == 112 for worker in finished.workers)
    assert all(worker.activity.startswith("Read file") for worker in finished.workers)
    assert active_pool(pool.pool_id) is None


async def test_transcripts_outlive_the_turn_and_hide_secrets(tmp_path: Path) -> None:
    release = asyncio.Event()
    release.set()
    reader = _blocking_reader(release, {})
    transport = MockTransport()
    transport.queue_tool_use("read_source", {"path": "config/.env"}, tool_use_id="call-0")
    transport.queue_response(_final("The file sets api_key=sk-live-1234567890abcdef.", "f"))
    pool = SubagentPool(
        [SubagentTask("task-0", "explorer", "Check config/.env, token=abcd1234efgh5678")],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="mock-model",
        all_tools=[reader],
        registry=_registry("explorer", "read_source"),
        transcript_root=tmp_path / "subagents",
    )
    await pool.run()
    # A later process (a resumed session) finds it on disk.
    pools = list_pools(tmp_path / "subagents")
    assert [record.pool_id for record in pools] == [pool.pool_id]
    assert pools[0].finished_at is not None
    assert [(w.worker_id, w.label) for w in pools[0].workers] == [("task-0", "explorer #1")]
    events = read_transcript(pools[0].transcript_path("task-0"))
    assert [event.kind for event in events] == [
        "prompt",
        "tool_started",
        "tool_finished",
        "text",
        "finished",
    ]
    assert events[1].tool == "read_source" and events[1].text == "config/.env"
    assert events[2].ok is True
    text = " ".join(event.text for event in events)
    assert "abcd1234efgh5678" not in text and "sk-live-1234567890abcdef" not in text
    assert "abcd1234efgh5678" not in pools[0].workers[0].task


async def test_stopping_one_worker_leaves_the_others_running(tmp_path: Path) -> None:
    release = asyncio.Event()
    started: dict[str, float] = {}
    reader = _blocking_reader(release, started)
    transport = MockTransport()
    for index in range(3):
        transport.queue_tool_use("read_source", {"path": f"f{index}"}, tool_use_id=f"c{index}")
    for index in range(3):
        transport.queue_response(_final(f"Done {index}.", f"final-{index}"))
    conversation = ConversationStore()
    pool = SubagentPool(
        [SubagentTask(f"task-{i}", "explorer", f"Read f{i}") for i in range(3)],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="mock-model",
        all_tools=[reader],
        conv_store=conversation,
        registry=_registry("explorer", "read_source"),
        transcript_root=tmp_path / "subagents",
    )
    run = asyncio.create_task(pool.run())
    await _wait_for(lambda: len(started) == 3)
    assert pool.stop_worker("task-1")
    assert not pool.stop_worker("task-9")
    await _wait_for(lambda: pool.state.worker("task-1").status == "stopped")  # type: ignore[union-attr]
    others = [w for w in pool.state.workers if w.worker_id != "task-1"]
    assert all(worker.status == "running" for worker in others)
    release.set()
    result = await run
    assert (result.succeeded, result.failed) == (2, 1)
    assert "stopped by user" in result.text
    assert "Done" in result.text
    assert not pool.stop_worker("task-0")  # finished workers cannot be stopped
    finished = conversation.subagent_pool_state()
    assert finished is not None
    assert [worker.status for worker in finished.workers] == ["done", "stopped", "done"]


async def test_a_workers_approval_names_it_and_waits_on_its_row(tmp_path: Path) -> None:
    @tool_write
    @tool()
    async def write_note(path: str, content: str) -> dict[str, object]:
        """Write a note."""
        return {"ok": True, "path": path}

    seen: list[ApprovalRequest] = []
    rows: list[str] = []
    conversation = ConversationStore()

    class Approvals:
        async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            seen.append(request)
            state = conversation.subagent_pool_state()
            rows.extend(w.status for w in state.workers if state is not None)
            return ApprovalResponse(allowed=False)

        def reset_turn_memory(self) -> None:
            return None

    transport = MockTransport()
    transport.queue_tool_use(
        "write_note", {"path": "notes.md", "content": "x"}, tool_use_id="write-1"
    )
    transport.queue_response(_final("The write was denied.", "final"))
    app_state = AppState.create()  # Safe: writes need approval
    pool = SubagentPool(
        [SubagentTask("task-0", "writer", "Write the note")],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="mock-model",
        all_tools=[write_note],
        app_state=app_state,
        conv_store=conversation,
        registry=_registry("writer", "write_note"),
        approval_svc=Approvals(),
        transcript_root=tmp_path / "subagents",
    )
    await pool.run()
    assert len(seen) == 1
    assert seen[0].requester == ApprovalRequester("writer #1", "writer", pool.pool_id, "task-0")
    assert rows == ["waiting"]
    kinds = [
        e.kind for e in read_transcript(tmp_path / "subagents" / pool.pool_id / "task-0.jsonl")
    ]
    assert "approval_requested" in kinds and "approval_resolved" in kinds


def _projection(transport: MockTransport) -> list[str]:
    return [
        repr((call.model, call.system, call.messages, [repr(tool) for tool in call.tools or []]))
        for call in transport.calls
    ]


async def _fan_out(
    transcripts: Path | None, conversation: ConversationStore | None
) -> tuple[list[str], str]:
    release = asyncio.Event()
    release.set()
    transport = MockTransport()
    for index in range(2):
        transport.queue_tool_use("read_source", {"path": f"f{index}"}, tool_use_id=f"c{index}")
        transport.queue_response(_final(f"Done {index}.", f"final-{index}"))
    pool = SubagentPool(
        [SubagentTask(f"task-{i}", "explorer", f"Read f{i}") for i in range(2)],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="mock-model",
        all_tools=[_blocking_reader(release, {})],
        max_concurrent=1,
        conv_store=conversation,
        registry=_registry("explorer", "read_source"),
        transcript_root=transcripts,
    )
    result = await pool.run()
    return _projection(transport), result.text


async def test_provider_requests_and_the_result_are_the_same_without_the_panel(
    tmp_path: Path,
) -> None:
    """AGX-08 §6.5: the panel and transcripts never reach a provider."""
    with_panel = await _fan_out(tmp_path / "subagents", ConversationStore())
    without = await _fan_out(None, None)
    assert with_panel == without
    assert list_pools(tmp_path / "subagents")
