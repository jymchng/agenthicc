"""TUIR-04: six subagents searching a 33,000-file project keep the TUI responsive.

The TUIR-00 harness as a gate. It runs a real ``SubagentPool`` of six
``explorer`` workers with the real filesystem tools, workspace policy,
journal and transcripts against a synthetic project, and samples event-loop
lag. Before TUIR-01/02 the worst stall was 93.7 s.
"""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(600)

#: Budgets. Measured locally after TUIR-01/02: max 81-90 ms, p95 2-3 ms over
#: about 155 samples (so p99 is the second-worst sample: too noisy to gate on).
MAX_STALL_S = 0.25
P95_STALL_S = 0.025
SEARCH_STALL_S = 0.10


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("lag-project")
    (root / ".gitignore").write_text("node_modules/\n")
    made = 0
    for top in ("src", "node_modules", "tests", "assets"):
        for a in range(30):
            for b in range(30):
                directory = root / top / f"pkg{a}" / f"mod{b}"
                directory.mkdir(parents=True, exist_ok=True)
                for c in range(10):
                    (directory / f"file{c}.py").write_text(f"VALUE = {c}\n")
                    made += 1
                    if made >= 33_000:
                        yield root
                        return
    yield root


@pytest.fixture(autouse=True)
def frozen_heap() -> Iterator[None]:
    """Measure the pool, not the heap earlier tests in this process left behind.

    After the unit suite the process holds about 450,000 objects, and one full
    collection over them stalls the loop for about 450 ms whatever the pool
    does. Freezing them moves them out of the collector's reach.
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


class _Sampler:
    def __init__(self) -> None:
        self.lags: list[float] = []
        self._running = True

    async def run(self) -> None:
        while self._running:
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            self.lags.append(time.perf_counter() - start - 0.01)

    def stop(self) -> None:
        self._running = False


def _context(root: Path) -> tuple[object, object]:
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime.mode_manager import build_default_registry

    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Yolo"))
    return app, WorkspaceAccessPolicy(WorkspaceScope.create(root), mode_provider=app.active_mode)


async def test_one_search_on_the_main_loop_stays_responsive(project: Path) -> None:
    from agenthicc.tools.fs import SearchFilesTool

    _app, policy = _context(project)
    sampler = _Sampler()
    task = asyncio.create_task(sampler.run())
    result = await SearchFilesTool().execute(
        {"pattern": "*.py", "path": "."},
        {"workspace_root": str(project), "workspace_access": policy},
    )
    sampler.stop()
    await task

    assert result["ok"] is True and result["count"] == 1_000
    assert max(sampler.lags) < SEARCH_STALL_S, f"stalled {max(sampler.lags) * 1000:.0f} ms"


async def test_six_subagents_searching_keep_the_loop_responsive(
    project: Path, tmp_path: Path
) -> None:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport import Completion, TokenUsage, ToolCall

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.pool import SubagentPool, SubagentTask
    from agenthicc.subagents.types import DEFAULT_REGISTRY
    from agenthicc.tools.fs.agent_tools import FS_AGENT_TOOLS
    from agenthicc.tools.workspace_access import (
        reset_current_workspace_access,
        set_current_workspace_access,
    )

    script = [
        ("search_files", {"pattern": "*.py", "path": "."}),
        ("grep_files", {"pattern": "VALUE = 7", "path": "src"}),
        ("list_directory", {"path": "src"}),
        ("read_file", {"path": "src/pkg0/mod0/file0.py"}),
    ]

    class _Transport:
        async def complete(self, messages: list[object], **kwargs: object) -> object:
            await asyncio.sleep(0.05)
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
            usage = TokenUsage(input_tokens=1, output_tokens=1)
            if done < len(script):
                name, arguments = script[done]
                return Completion(
                    id=f"c{done}",
                    model="m",
                    content="",
                    stop_reason="tool_use",
                    usage=usage,
                    tool_calls=[ToolCall(tool_use_id=f"t{done}", name=name, input=arguments)],
                )
            return Completion(
                id="f",
                model="m",
                content="Found it.",
                tool_calls=[],
                stop_reason="end_turn",
                usage=usage,
            )

    app, policy = _context(project)
    token = set_current_workspace_access(policy)  # type: ignore[arg-type]
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    pool = SubagentPool(
        [SubagentTask(f"task-{i}", "explorer", f"area {i}") for i in range(6)],
        parent_runner=AgentRunnerBase(transport=_Transport()),
        parent_model="mock-model",
        all_tools=list(FS_AGENT_TOOLS),
        max_concurrent=6,
        conv_store=app.conversation,  # type: ignore[attr-defined]
        registry=DEFAULT_REGISTRY,
        app_state=app,  # type: ignore[arg-type]
        workspace_access=policy,  # type: ignore[arg-type]
        conversation_journal=journal,
        conversation_id="conv",
        parent_run_id="turn-1",
        transcript_root=tmp_path / "subagents",
        timeout_s=300,
    )
    from agenthicc.tui.loop_lag import LoopLagMonitor

    sampler = _Sampler()
    task = asyncio.create_task(sampler.run())
    monitor = LoopLagMonitor(threshold_s=MAX_STALL_S / 2)
    monitor.start()
    try:
        result = await pool.run()
    finally:
        await monitor.stop()
        sampler.stop()
        await task
        journal.close()
        reset_current_workspace_access(token)

    ordered = sorted(sampler.lags)
    worst = ordered[-1]
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    assert result.succeeded == 6
    culprits = "; ".join(
        f"{stall.duration_s * 1000:.0f} ms at {' <- '.join(reversed(stall.frames[-6:]))}"
        for stall in monitor.stalls()
    )
    assert worst < MAX_STALL_S, f"worst stall {worst * 1000:.0f} ms: {culprits}"
    assert p95 < P95_STALL_S, f"p95 stall {p95 * 1000:.0f} ms"
