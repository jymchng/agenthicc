"""Provider-boundary scenario for SUBAGENT-16's default spawn mode."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, CompletionChunk, Message, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.runners.tui_session import TUISession
from tests.e2e.self_extension.harness import _capture_message, reply, tool_call
from tests.performance.test_subagent_loop_lag import _Sampler
from tests.subagent_sessions import session as session

pytestmark = pytest.mark.integration

WORKER_TASK = "Measure scripted worker"
PARENT_INPUT = "Delegate the measurements while I keep working."
SIBLING_CONTENT = "SIBLING_READ_RESULT"


class _MeasuredTransport(MockTransport):
    """Serve separate scripts for the parent and three concurrent workers."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[float, float, tuple[dict[str, object], ...]]] = []
        self.worker_finished: list[float] = []
        self.release = asyncio.Event()
        self.clock = 0.0

    async def complete(
        self, messages: list[Message], **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        captured = tuple(_capture_message(message) for message in messages)
        if captured and WORKER_TASK in str(captured[0]["content"]):
            try:
                await asyncio.wait_for(self.release.wait(), timeout=5)
            except TimeoutError:
                self.clock = 5.0
            self.worker_finished.append(perf_counter())
            response = reply("Worker-private output")
        else:
            self.requests.append((self.clock, perf_counter(), captured))
            if len(self.requests) == 1:
                spawn = tool_call(
                    "spawn_subagents",
                    {
                        "tasks": [
                            {"type": "explorer", "task": f"{WORKER_TASK} {index}"}
                            for index in range(3)
                        ],
                        "max_concurrent": 3,
                    },
                    use_id="spawn",
                )
                response = replace(
                    spawn,
                    tool_calls=[
                        *spawn.tool_calls,
                        ToolCall(
                            tool_use_id="sibling", name="read_file", input={"path": "sample.txt"}
                        ),
                    ],
                )
            elif len(self.requests) == 2:
                response = tool_call("read_file", {"path": "another.txt"}, use_id="second-read")
            else:
                response = reply("I read both files while the workers ran.")
        self.queue_response(response)
        return await super().complete(messages, **kwargs)


async def test_a1_a2_spawn_releases_the_sibling_result_and_leaves_the_turn_running(
    session: TUISession,
) -> None:
    Path("sample.txt").write_text(SIBLING_CONTENT, encoding="utf-8")
    Path("another.txt").write_text("SECOND_READ_RESULT", encoding="utf-8")
    transport = _MeasuredTransport()
    session._ctx.agent_runner = AgentRunnerBase(transport=transport)
    sampler = _Sampler()
    sampling = asyncio.create_task(sampler.run())
    try:
        await session.agent_task_body(PARENT_INPUT, turn_id="measured-parent")
        assert len(transport.requests) == 3
        (
            (started_at, started_wall, _),
            (followup_at, followup_wall, messages),
            (_, _, final_messages),
        ) = transport.requests
        assert "SECOND_READ_RESULT" in str(final_messages)
        events = session._ctx.app_state.conversation._events_by_id.values()
        assert any(
            "I read both files while the workers ran." in str(event.payload) for event in events
        )
        assert SIBLING_CONTENT in str(messages)
        elapsed = followup_at - started_at
        assert elapsed < 0.25, (
            f"Next provider request and sibling result took {elapsed:.3f}s. "
            f"Workers finished before it: {len(transport.worker_finished)}. "
            f"Worst sampled stall: {max(sampler.lags, default=0) * 1000:.1f}ms."
        )
        print(
            f"A1 provider interval {elapsed:.3f}s scripted, {followup_wall - started_wall:.3f}s wall time"
        )
        assert not transport.worker_finished
        assert '"status": "running"' in str(messages)
    finally:
        transport.release.set()
        sampler.stop()
        await sampling
