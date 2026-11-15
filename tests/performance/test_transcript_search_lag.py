"""TUX-09 acceptance criterion 3: searching a 100 MB log keeps the event loop responsive.

The TUIR-04 gate (tests/performance/test_subagent_loop_lag.py): an asyncio
sampler measures how late it wakes while the transcript view indexes and searches
a 100 MB synthetic session log. The work runs on a worker thread in 1 MiB
chunks, so the worst stall stays under TUIR-04's budget.
"""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.transcript.sources import SessionLogSource
from agenthicc.tui.transcript.view import TranscriptView
from tests.transcript_support import event_line, keys, use_sessions_dir

pytestmark = pytest.mark.timeout(600)

#: TUIR-04's worst-stall budget.
MAX_STALL_S = 0.25
LOG_BYTES = 100 * 1024 * 1024


@pytest.fixture(autouse=True)
def frozen_heap() -> Iterator[None]:
    """Measure the search, not the heap earlier tests left behind (as TUIR-04 does)."""
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture(scope="module")
def big_log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 100 MB session log: prompts, tool calls with output, answers and invisible kinds."""
    root = tmp_path_factory.mktemp("big-session") / "sessions"
    path = root / "big" / "conversation.jsonl"
    path.parent.mkdir(parents=True)
    lines: list[str] = []
    for i in range(2_000):
        match i % 4:
            case 0:
                payload = {"text": f"please look into module number {i} and report back " * 3}
                lines.append(event_line(i, "user_message", payload))
            case 1:
                output = [f"src/mod{i}.py:{j}: value = {j}" for j in range(8)]
                lines.append(
                    event_line(
                        i,
                        "tool_complete",
                        {
                            "name": "run_bash",
                            "success": True,
                            "state": "exited",
                            "summary": f"grep -rn thing{i} src",
                            "args": {"command": f"grep -rn thing{i} src"},
                            "output_lines": output,
                            "output_head": 8,
                            "output_total": 8,
                            "exit_code": 0,
                        },
                    )
                )
            case 2:
                lines.append(event_line(i, "text", {"text": f"Module {i} looks fine. " * 8}))
            case _:
                lines.append(event_line(i, "tokens", {"input_tokens": 100, "output_tokens": 50}))
    # One needle, far from both ends.
    lines[1001] = event_line(1001, "text", {"text": "the needle-in-the-haystack answer"})
    block = ("\n".join(lines) + "\n").encode()
    written = 0
    with path.open("wb") as handle:
        while written < LOG_BYTES:
            handle.write(block)
            written += len(block)
    return root


class _Sampler:
    def __init__(self) -> None:
        self.lags: list[float] = []
        self._running = True

    async def run(self) -> None:
        while self._running:
            start = time.perf_counter()
            await asyncio.sleep(0.005)
            self.lags.append(time.perf_counter() - start - 0.005)

    def stop(self) -> None:
        self._running = False


async def test_searching_a_100_mb_log_never_stalls_the_loop(
    big_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sessions_dir(monkeypatch, big_log)
    assert (big_log / "big" / "conversation.jsonl").stat().st_size >= LOG_BYTES
    sampler = _Sampler()
    sampler_task = asyncio.create_task(sampler.run())
    await asyncio.sleep(0.05)

    source = SessionLogSource("big")
    view = TranscriptView(source, Keymap.build(DEFAULT_ACTIONS), subtitle="big")
    # The loop keeps drawing frames and taking keys while the index builds.
    while not source.ready:
        view.render(120, 40)
        await asyncio.sleep(0.01)
    assert source.count() > 200_000

    started = time.perf_counter()
    for key, ch in [(Key.CHAR, "/"), *keys("needle-in-the"), (Key.ENTER, "")]:
        view.handle_key(key, ch)
    assert view.job is not None
    while not view.job.done:
        view.tick()
        view.render(120, 40)
        await asyncio.sleep(0.01)
    elapsed = time.perf_counter() - started

    # A common word: tens of thousands of hits, every one decoded on the worker.
    for key, ch in [(Key.CHAR, "/"), (Key.CTRL_U, ""), *keys("Module"), (Key.ENTER, "")]:
        view.handle_key(key, ch)
    assert view.job is not None
    while not view.job.done:
        view.tick()
        await asyncio.sleep(0.01)
    assert len(view.job.hits) > 50_000
    sampler.stop()
    await sampler_task
    source.close()

    assert elapsed < 60  # the search itself is not slow either
    assert max(sampler.lags) < MAX_STALL_S, f"worst stall {max(sampler.lags):.3f}s"
    assert len(sampler.lags) > 100  # the sampler really ran alongside the work


async def test_the_needle_is_found_in_every_repeat_of_a_log_of_100_mb(
    big_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sessions_dir(monkeypatch, big_log)
    source = SessionLogSource("big")
    assert await asyncio.to_thread(source.wait_ready, 120)
    job = source.start_search("needle-in-the-haystack")
    deadline = time.monotonic() + 120
    while not job.done:
        assert time.monotonic() < deadline, "the search did not finish"
        await asyncio.sleep(0.01)
    # The 2,000-line block that was repeated to 100 MB holds one needle.
    assert 100 < len(job.hits) < 200
    assert "needle-in-the-haystack answer" in source.plain_text(job.hits[0])
    assert job.hits == sorted(job.hits)
    source.close()
