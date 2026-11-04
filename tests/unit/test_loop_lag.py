"""TUIR-04: the loop-lag monitor measures lag and names the code behind a stall."""

from __future__ import annotations

import asyncio
import gc
import io
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.tui.loop_lag import LoopLagMonitor

pytestmark = pytest.mark.unit


def a_tool_that_blocks_the_loop() -> None:
    time.sleep(0.4)


async def test_a_stall_is_recorded_with_the_function_that_caused_it() -> None:
    monitor = LoopLagMonitor(interval_s=0.01, threshold_s=0.1)
    monitor.start()
    try:
        await asyncio.sleep(0.05)
        a_tool_that_blocks_the_loop()
        await asyncio.sleep(0.05)
    finally:
        await monitor.stop()

    (stall,) = monitor.stalls()
    assert "test_loop_lag.py" in stall.culprit and "a_tool_that_blocks_the_loop" in stall.culprit
    assert 0.3 <= stall.duration_s < 1.0
    assert monitor.summary()["max_ms"] >= 300


class _Node:
    partner: _Node


async def test_a_garbage_collection_stall_is_attributed_to_the_collector() -> None:
    gc.disable()
    try:
        heap = []
        for _ in range(600_000):
            first, second = _Node(), _Node()
            first.partner, second.partner = second, first
            heap.append(first)
        monitor = LoopLagMonitor(interval_s=0.01, threshold_s=0.05)
        monitor.start()
        try:
            await asyncio.sleep(0.05)
            del heap
            gc.collect()
            await asyncio.sleep(0.05)
        finally:
            await monitor.stop()
    finally:
        gc.enable()

    stalls = monitor.stalls()
    assert stalls, monitor.summary()
    assert stalls[-1].culprit.startswith("garbage collection ("), stalls[-1]


async def test_an_idle_loop_records_no_stall_and_little_lag() -> None:
    monitor = LoopLagMonitor(interval_s=0.01, threshold_s=0.1)
    monitor.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await monitor.stop()

    assert monitor.stalls() == []
    summary = monitor.summary()
    assert summary["samples"] >= 10 and summary["p50_ms"] < 50


async def test_perf_lists_loop_lag_and_stalls() -> None:
    from agenthicc.commands.builtins import _cmd_perf
    from agenthicc.tui.workspace.frames import FrameStats

    monitor = LoopLagMonitor(interval_s=0.01, threshold_s=0.1)
    monitor.start()
    try:
        await asyncio.sleep(0.05)
        a_tool_that_blocks_the_loop()
        await asyncio.sleep(0.05)
    finally:
        await monitor.stop()
    output = io.StringIO()
    context = SimpleNamespace(
        console=Console(file=output, width=200),
        frame_stats=lambda: FrameStats(),
        loop_lag=lambda: monitor,
    )

    assert _cmd_perf(context) is True  # type: ignore[arg-type]

    text = output.getvalue()
    assert "Event-loop lag:" in text
    assert "a_tool_that_blocks_the_loop" in text
