"""The compaction indicator in a running workspace.

While the history is summarised the live region shows ``◜ Refreshing context ·
Ns`` above the composer, its seconds moving in every motion mode; when the
compaction ends the row leaves and the transcript gains ``✓ Context refreshed``
(or ``✗ Context refresh failed``). Nothing is drawn for progress the compaction
cannot measure.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from io import StringIO
from types import SimpleNamespace

import pytest
from lauren_ai._memory import ShortTermMemory
from rich.console import Console

from agenthicc.config import UISettings
from agenthicc.memory.compactor import compact_memory_with_result
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.theme import glyphs
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.live_region import Line, lines_text

pytestmark = pytest.mark.e2e


class _GatedTransport:
    """Hold each summarising call until the test lets it finish."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self._fail = fail

    async def complete(self, messages, *, model, system, max_tokens, temperature, stream):
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        if self._fail:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(content="Short summary.")


def _memory(chars: int = 600) -> ShortTermMemory:
    memory = ShortTermMemory(max_tokens=64_000)
    memory.add_user("Please review the project")
    memory._messages.append({"role": "assistant", "content": "a" * chars})
    memory.add_user("and report back")
    return memory


def _live_rows(workspace: Workspace) -> list[str]:
    return lines_text(workspace._build_lines())


def _ring_row(rows: list[str]) -> str | None:
    ring = (*glyphs().ring, glyphs().ring_still)
    return next((row for row in rows if row.startswith(ring) and "Refreshing context" in row), None)


async def _workspace(
    ui: UISettings | None = None,
) -> tuple[AppState, Workspace, Console]:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    state.conversation.model_name.set("anthropic/claude-opus-4-8")
    console = Console(record=True, force_terminal=True, width=100, height=30, file=StringIO())
    workspace = Workspace(state, console)
    workspace.start()
    return state, workspace, console


@pytest.mark.asyncio
async def test_the_row_sits_above_the_composer_then_the_result_row_replaces_it() -> None:
    state, workspace, console = await _workspace()
    conv = state.conversation
    transport = _GatedTransport()
    task = asyncio.create_task(
        compact_memory_with_result(_memory(), transport, model="m", conv_store=conv)
    )
    try:
        await asyncio.wait_for(transport.entered.wait(), 2)
        rows = _live_rows(workspace)
        row = _ring_row(rows)
        assert row is not None and row.endswith("· 0s")
        rule = next(i for i, text in enumerate(rows) if text.endswith("· Safe ─"))
        assert rows.index(row) < rule and rows[rows.index(row) - 1] == ""
        # Nothing was measured, so there is no bar under the row.
        assert rows[rows.index(row) + 1] == ""
        # No second indicator in the status line.
        assert not any("Compacting" in text for text in rows)

        conv._compaction_started_at = time.monotonic() - 2.5
        conv.tick()
        assert _ring_row(_live_rows(workspace)) is not None
        assert (_ring_row(_live_rows(workspace)) or "").endswith("· 2s")

        transport.gate.set()
        outcome = await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.1)  # the appender prints on the loop
        assert outcome.status == "compacted_summary"
        assert _ring_row(_live_rows(workspace)) is None
        printed = console.export_text()
        assert printed.count("✓ Context refreshed") == 1
        assert "tokens" in printed.split("✓ Context refreshed")[1].splitlines()[0]
    finally:
        transport.gate.set()
        workspace.stop()


@pytest.mark.asyncio
async def test_a_failed_compaction_replaces_the_row_with_the_failure_row() -> None:
    state, workspace, console = await _workspace()
    transport = _GatedTransport(fail=True)
    task = asyncio.create_task(
        compact_memory_with_result(_memory(), transport, model="m", conv_store=state.conversation)
    )
    try:
        await asyncio.wait_for(transport.entered.wait(), 2)
        assert _ring_row(_live_rows(workspace)) is not None
        transport.gate.set()
        outcome = await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.1)
        assert outcome.status == "skipped_summary_unavailable"
        assert _ring_row(_live_rows(workspace)) is None
        printed = console.export_text()
        assert printed.count("✗ Context refresh failed") == 1
        assert "✓ Context refreshed" not in printed
    finally:
        transport.gate.set()
        workspace.stop()


@pytest.mark.asyncio
async def test_a_measured_compaction_draws_the_bar_under_the_row() -> None:
    state, workspace, _console = await _workspace()
    conv = state.conversation
    conv.begin_compaction()
    try:
        conv.report_compaction_progress(2, 5)
        rows = _live_rows(workspace)
        row = _ring_row(rows)
        assert row is not None
        bar = rows[rows.index(row) + 1]
        assert set(bar) <= {glyphs().progress_fill, glyphs().progress_half, glyphs().progress_track}
        assert bar.startswith(glyphs().progress_fill)
        assert len(bar) == 30
    finally:
        conv.end_compaction()
        workspace.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ui", [UISettings(), UISettings(reduced_motion=True)])
async def test_the_seconds_move_once_a_second_in_every_motion_mode(ui: UISettings) -> None:
    state, workspace, _console = await _workspace(ui)
    conv = state.conversation
    live = workspace._live
    assert live is not None
    updates: list[list[str]] = []
    original = live.update

    def record(lines: Sequence[Line]) -> None:
        updates.append(lines_text(lines))
        original(lines)

    live.update = record  # type: ignore[method-assign]
    conv.begin_compaction()
    try:
        await asyncio.sleep(0.12)
        conv._compaction_started_at = time.monotonic() - 1.2
        updates.clear()
        conv.tick()
        await asyncio.sleep(0.12)
        assert updates, "the new second was not drawn"
        row = _ring_row(_live_rows(workspace)) or ""
        assert row.endswith("· 1s")
        if not ui.animated:
            assert row.startswith(glyphs().ring_still)  # the ring stays at rest
            updates.clear()
            conv.tick()
            conv.tick()
            await asyncio.sleep(0.12)
            assert updates == []  # nothing animates, nothing redraws within a second
    finally:
        conv.end_compaction()
        workspace.stop()
