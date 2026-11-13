"""Helpers for tests that observe the live block's frames (AGX-11)."""

from __future__ import annotations

import asyncio
import time

from agenthicc.tui.workspace import Workspace


async def settle_frames(workspace: Workspace, timeout: float = 2.0) -> None:
    """Wait until no frame is waiting out the frame interval.

    Frames are capped to one per interval, so a state change made just before
    a test starts observing can still draw shortly afterwards.
    """
    deadline = time.monotonic() + timeout
    while workspace.frames.pending:
        if time.monotonic() > deadline:
            raise AssertionError("a frame stayed pending")
        await asyncio.sleep(0.002)


async def settle_startup(workspace: Workspace, timeout: float = 2.0) -> None:
    """Wait for the frames startup itself draws, then for the block to go quiet.

    ``Workspace.start`` paints once, and then reads the git branch and its dirty
    mark in a background task; both publish signals and each publication
    requests a frame. A test that starts counting frames right after ``start``
    would count those among the work it meant to watch, so it waits here first.
    """
    git_task = getattr(workspace, "_git_task", None)
    if git_task is not None:
        await git_task
    await settle_frames(workspace, timeout)
