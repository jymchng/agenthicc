"""TUX-05 AC4: the ``@`` picker keeps the event loop responsive in a 10,000-file project.

The TUIR-04 gate: an asyncio sampler measures how late it wakes while the
picker is opened and typed into, with ``git status`` running for real against
10,000 tracked files. The worst stall must stay under TUIR-04's budget.
"""

from __future__ import annotations

import asyncio
import gc
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.theme import glyphs
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

pytestmark = pytest.mark.integration

#: TUIR-04's worst-stall budget (tests/performance/test_subagent_loop_lag.py).
MAX_STALL_S = 0.25


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def frozen_heap() -> Iterator[None]:
    """Measure the picker, not the heap earlier tests left behind (as TUIR-04 does).

    One full collection over the objects a long run leaves behind stalls the loop
    for hundreds of milliseconds whatever the picker does.
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("at-lag")
    for a in range(100):
        directory = root / f"pkg{a:03}"
        directory.mkdir()
        for b in range(100):
            (directory / f"mod{b:03}.py").write_text("VALUE = 1\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    (root / "pkg042" / "mod007.py").write_text("VALUE = 2\n")
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


async def test_typing_into_the_at_picker_never_stalls_the_loop(project: Path) -> None:
    trigger = AtMentionTrigger()
    buffer = InputBuffer(["@"])
    overlay = TriggerPickerOverlay(
        buffer, 0, trigger, cwd=project, keymap=Keymap.build(DEFAULT_ACTIONS)
    )
    sampler = _Sampler()
    sampler_task = asyncio.create_task(sampler.run())
    await asyncio.sleep(0.05)
    for ch in "pkg04":
        buffer.insert(ch)
        assert overlay.refresh()
        overlay.render()
        await asyncio.sleep(0.01)
    # git status runs on its own thread; wait for it without blocking the loop.
    thread = trigger.working_tree(project)._thread
    assert thread is not None
    await asyncio.to_thread(thread.join, 60)
    sampler.stop()
    await sampler_task
    assert max(sampler.lags) < MAX_STALL_S, f"worst stall {max(sampler.lags):.3f}s"


async def test_the_first_row_of_an_empty_at_picker_is_the_changed_file(project: Path) -> None:
    trigger = AtMentionTrigger()
    # The session warms the working-tree status when it starts, off the loop.
    trigger.warm(project)
    thread = trigger.working_tree(project)._thread
    assert thread is not None
    await asyncio.to_thread(thread.join, 60)
    overlay = TriggerPickerOverlay(
        InputBuffer(["@"]), 0, trigger, cwd=project, keymap=Keymap.build(DEFAULT_ACTIONS)
    )
    assert overlay.matches[0].value == "pkg042/mod007.py"
    assert overlay.matches[0].glyph == glyphs().info
    # Redrawing a listing of 10,000 rows is bounded by the window, not the listing.
    started = time.perf_counter()
    for _ in range(10):
        overlay.render()
    assert (time.perf_counter() - started) / 10 < MAX_STALL_S / 2
