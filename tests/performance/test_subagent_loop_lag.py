"""TUIR-04: six subagents searching a 33,000-file project keep the TUI responsive.

The TUIR-00 harness as a gate. It runs a real ``SubagentPool`` of six
``explorer`` workers with the real filesystem tools, workspace policy,
journal and transcripts against a synthetic project, and samples event-loop
lag. Before TUIR-01/02 the worst stall was 93.7 s.
"""

from __future__ import annotations
from agenthicc.tools.surface import ToolSurface

import asyncio
import gc
import time
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

pytestmark = pytest.mark.timeout(600)

#: Budgets. Measured locally after TUIR-01/02: max 81-90 ms, p95 2-3 ms over
#: about 155 samples (so p99 is the second-worst sample: too noisy to gate on).
#: Loosened on a loaded host, where this test alone measured p95 at 44-45 ms
#: (operator request): the loop still has to stay responsive to a human, and a
#: stall that really blocked the loop for search work would be hundreds of ms.
MAX_STALL_S = 0.5
P95_STALL_S = 0.1
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
        tool_surface=ToolSurface(0, tuple(list(FS_AGENT_TOOLS))),
        max_concurrent=6,
        conv_store=app.conversation,
        registry=DEFAULT_REGISTRY,
        app_state=app,
        workspace_access=policy,
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


async def test_a16_eight_background_workers_and_a_wake_keep_the_tui_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lauren_ai._transport import Completion

    from agenthicc.tui.loop_lag import LoopLagMonitor
    from tests.e2e.self_extension.harness import ModelRequest, reply, tool_call
    from tests.integration.test_subagent_wake import notices
    from tests.subagent_sessions import ScenarioTransport, attach, send, spawn_call, tui, until

    release = asyncio.Event()
    entered = 0
    woke = asyncio.Event()

    read_steps = 4

    async def worker(request: ModelRequest) -> Completion:
        nonlocal entered
        reads = sum("tool_result" in str(message["content"]) for message in request.messages)
        if reads == read_steps:
            await release.wait()
            return reply("Checked.")
        if reads == 0:
            entered += 1
            # Stagger the scripted provider responses while the workers issue
            # real reads over several provider round trips.
            await asyncio.sleep(0.16 * entered)
        else:
            await asyncio.sleep(1.0)
        return tool_call("read_file", {"path": "sample.txt"}, use_id=f"worker-read-{reads}")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call(8)
        if sequence == 2:
            assert request.contains('"status": "running"')
            return reply("Delegated.")
        # Every worker that finishes notifies the parent. Workers that finish
        # together share a wake, so eight of them may take anywhere from one to
        # eight wakes; the parent has heard from all of them on the last one.
        assert sequence >= 3
        finished = notices(request)
        assert 1 <= len(finished) <= 8
        if len(finished) == 8:
            assert sum("the pool is complete" in notice for notice in finished) == 1
            woke.set()
        return reply(f"{len(finished)} of eight workers have reported.")

    # Keep durable syscalls and the real projections in the benchmark. Use
    # tmpfs when available so unrelated disk writeback cannot mask loop stalls.
    ram = Path("/dev/shm")
    with (
        TemporaryDirectory(prefix="agenthicc-lag-", dir=ram if ram.is_dir() else tmp_path) as root,
        monkeypatch.context() as scoped,
    ):
        async with tui(Path(root), scoped) as session:
            # The budget covers a working session. Resolve first-use imports and
            # start the real journal and transcript writers before sampling.
            async def warm_parent(_request: ModelRequest, sequence: int) -> Completion:
                return spawn_call() if sequence == 1 else reply("Warm-up complete.")

            async def warm_worker(_request: ModelRequest) -> Completion:
                return reply("Warm-up checks passed.")

            warmup = ScenarioTransport(warm_parent, warm_worker)
            attach(session, warmup)
            await (await send(session, "Warm the scripted worker path."))
            await until(
                lambda: (
                    any(notices(request) for request in warmup.main_requests)
                    and session._agent_task is None
                )
            )

            Path("sample.txt").write_text("x = 1\n", encoding="utf-8")
            script = ScenarioTransport(main, worker)
            attach(session, script)
            gc.collect()
            gc.freeze()
            sampler = _Sampler()
            monitor = LoopLagMonitor(threshold_s=0.025)
            task = asyncio.create_task(sampler.run())
            monitor.start()
            try:
                await (await send(session, "Start eight background checks."))
                await until(lambda: len(script.worker_requests) == 8 * (read_steps + 1))
                assert session._agent_task is None
                assert session._ctx.app_state.conversation.subagent_background_running() == 1
                release.set()
                await until(woke.is_set)
                await until(lambda: session._agent_task is None)
            finally:
                release.set()
                sampler.stop()
                await task
                await monitor.stop()
            ordered = sorted(sampler.lags)
            worst = ordered[-1]
            p95 = ordered[int(0.95 * (len(ordered) - 1))]
            culprits = "; ".join(
                f"{stall.duration_s * 1000:.0f} ms at {' <- '.join(reversed(stall.frames[-12:]))}"
                for stall in monitor.stalls()
            )
            assert worst < 0.250, f"worst stall {worst * 1000:.1f} ms: {culprits}"
            assert p95 < 0.025, f"p95 stall {p95 * 1000:.1f} ms: {culprits}"
            print(f"Eight workers plus wake: max {worst * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms.")
