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
