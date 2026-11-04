"""Measure event-loop stalls while a real subagent pool runs real fs tools.

Usage: python harness.py <project_dir> <session_dir> [workers] [profile_out]
"""

from __future__ import annotations

import asyncio
import cProfile
import json
import pstats
import sys
import time
from pathlib import Path


class LagSampler:
    """Records how late a 10 ms sleep wakes up: the loop's stall time."""

    def __init__(self) -> None:
        self.lags: list[float] = []
        self._stop = False

    async def run(self) -> None:
        interval = 0.01
        while not self._stop:
            start = time.perf_counter()
            await asyncio.sleep(interval)
            self.lags.append(time.perf_counter() - start - interval)

    def stop(self) -> None:
        self._stop = True

    def report(self) -> dict[str, float]:
        if not self.lags:
            return {}
        ordered = sorted(self.lags)
        pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
        return {
            "samples": len(ordered),
            "p50_ms": pick(0.5) * 1e3,
            "p95_ms": pick(0.95) * 1e3,
            "p99_ms": pick(0.99) * 1e3,
            "max_ms": ordered[-1] * 1e3,
            "over_100ms": sum(1 for lag in ordered if lag > 0.1),
            "over_500ms": sum(1 for lag in ordered if lag > 0.5),
            "stalled_total_s": sum(lag for lag in ordered if lag > 0.05),
        }


def make_transport(script: list[tuple[str, dict[str, object]]]):
    from lauren_ai._transport import Completion, TokenUsage, ToolCall

    class ScriptedTransport:
        """Each worker walks the same script: tool calls, then a final answer."""

        async def complete(self, messages: list[object], **kwargs: object) -> object:
            await asyncio.sleep(0.05)  # provider latency
            done = sum(
                1
                for message in messages
                if isinstance(message, dict)
                and isinstance(message.get("content"), list)
                and any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in message["content"]
                )
            )
            usage = TokenUsage(input_tokens=500, output_tokens=50)
            if done < len(script):
                name, tool_input = script[done]
                return Completion(
                    id=f"c{time.time_ns()}",
                    model="mock-model",
                    content="Working on it.",
                    tool_calls=[
                        ToolCall(tool_use_id=f"t{time.time_ns()}", name=name, input=tool_input)
                    ],
                    stop_reason="tool_use",
                    usage=usage,
                )
            return Completion(
                id=f"c{time.time_ns()}",
                model="mock-model",
                content="Final findings: " + "x" * 3_000,
                tool_calls=[],
                stop_reason="end_turn",
                usage=usage,
            )

    return ScriptedTransport()


async def main(project: Path, session: Path, workers: int, profile_out: str | None) -> None:
    from lauren_ai._agents._runner import AgentRunnerBase

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.pool import SubagentPool, SubagentTask
    from agenthicc.subagents.types import DEFAULT_REGISTRY
    from agenthicc.tools.fs.agent_tools import FS_AGENT_TOOLS
    from agenthicc.tools.workspace_access import (
        WorkspaceAccessPolicy,
        WorkspaceScope,
        set_current_workspace_access,
    )
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime.mode_manager import build_default_registry

    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Yolo"))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(project), mode_provider=app.active_mode)
    set_current_workspace_access(policy)
    journal = ConversationJournal(session / "journal.jsonl")
    script = [
        ("search_files", {"pattern": "*.py", "path": "."}),
        ("grep_files", {"pattern": "def handler", "path": "src"}),
        ("list_directory", {"path": "src"}),
        ("read_file", {"path": "src/pkg0/mod0.py"}),
    ]
    pool = SubagentPool(
        [SubagentTask(f"task-{i}", "explorer", f"explore area {i}") for i in range(workers)],
        parent_runner=AgentRunnerBase(transport=make_transport(script)),
        parent_model="mock-model",
        all_tools=list(FS_AGENT_TOOLS),
        max_concurrent=workers,
        conv_store=app.conversation,
        registry=DEFAULT_REGISTRY,
        app_state=app,
        workspace_access=policy,
        conversation_journal=journal,
        conversation_id="conv",
        parent_run_id="turn-1",
        transcript_root=session / "subagents",
        timeout_s=600,
    )
    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 0.1
    sampler = LagSampler()
    sampling = asyncio.create_task(sampler.run())
    profiler = cProfile.Profile() if profile_out else None
    started = time.perf_counter()
    if profiler:
        profiler.enable()
    result = await pool.run()
    if profiler:
        profiler.disable()
    elapsed = time.perf_counter() - started
    sampler.stop()
    await sampling
    journal.close()
    print(
        json.dumps(
            {
                "workers": workers,
                "succeeded": result.succeeded,
                "elapsed_s": elapsed,
                **sampler.report(),
            },
            indent=1,
        )
    )
    if profiler and profile_out:
        profiler.dump_stats(profile_out)
        stats = pstats.Stats(profiler).sort_stats("cumulative")
        stats.print_stats(35)


if __name__ == "__main__":
    Path(sys.argv[2]).mkdir(parents=True, exist_ok=True)
    asyncio.run(
        main(
            Path(sys.argv[1]),
            Path(sys.argv[2]),
            int(sys.argv[3]) if len(sys.argv) > 3 else 6,
            sys.argv[4] if len(sys.argv) > 4 else None,
        ),
        debug=False,
    )
