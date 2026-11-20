"""The agents hub shows what an agent asked in full, at a narrow terminal too.

The report that started this: a background agent asked which of three options
to build, and the hub's answer form cut each option at the panel's edge
(``A — One screen, interleaved (recommended): the shell stays l``), so the
operator chose without reading the options.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.worker import BackgroundApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import AgentsHub, Tab
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.e2e

OPTIONS = [
    "A — One screen, interleaved (recommended): the shell stays live, and Run output "
    "prints into the same terminal you are typing in",
    "B — One panel, two screens with an Output / Shell toggle: hopping between them "
    "keeps each one's own scrollback",
    "C — Only the shell: run everything (including the Run button) inside the terminal, "
    "so there is no separate output view",
]
QUESTION = (
    "Confirmed the `[killed by SIGHUP]` is my stop-then-run change: clicking Run now stops "
    "the Linux shell on purpose, which is why `ls` stops working afterwards. Which do you want?"
)


def _store(tmp_path: Path) -> tuple[BackgroundStore, BackgroundSession]:
    store = BackgroundStore(tmp_path / "background")
    (tmp_path / "playground").mkdir()
    session = BackgroundSession.create(
        "playground",
        title="AsyncMove PlayGround Linux",
        cwd=str(tmp_path / "playground"),
        workflow_name="goal_flow",
        intent="make the Linux terminal usable",
    ).evolve(status=SessionStatus.RUNNING, worker_pid=os.getpid(), lease_token="lease", attempt=2)
    store.create(session)
    return store, session


async def _ask(
    store: BackgroundStore, session: BackgroundSession, options: list[str]
) -> asyncio.Task[object]:
    service = BackgroundApprovalService(store, session.session_id, question_timeout_s=30)
    request = SimpleNamespace(
        kind="questions",
        tool_name="ask_user",
        tool_use_id="ask-1",
        request_id="ask-1",
        timeout_s=30.0,
        tool_input={"questions": [{"id": "plan", "text": QUESTION, "options": options}]},
        question_fingerprint="fp",
    )
    task: asyncio.Task[object] = asyncio.create_task(service.request_input(request))
    for _ in range(200):
        if store.get(session.session_id).status is SessionStatus.WAITING_INPUT:
            break
        await asyncio.sleep(0.01)
    return task


async def _hub(store: BackgroundStore, tmp_path: Path) -> AgentsHub:
    hub = AgentsHub(
        Console(record=True, width=64),
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
        tab=Tab.NEEDS_YOU,
    )
    await hub.start()
    await hub.settle()
    return hub


def _screen(hub: AgentsHub, width: int, height: int) -> list[str]:
    console = Console(record=True, width=width, height=height, file=open(os.devnull, "w"))
    console.print(hub.render(width, height))
    return console.export_text().splitlines()


def _flat(lines: list[str]) -> str:
    return " ".join(" ".join(lines).split())


async def test_the_options_read_in_full_at_the_width_of_the_report(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    waiting = await _ask(store, session, OPTIONS)
    hub = await _hub(store, tmp_path)
    try:
        hub.handle_key(Key.CHAR, "i")
        await hub.settle()
        screen = _screen(hub, 64, 30)
        assert all(cell_len(line) <= 64 for line in screen)
        flat = _flat(screen)
        for option in OPTIONS:
            assert " ".join(option.split()) in flat, option
        assert "(recommended): the shell stays live" in flat
        assert "the same terminal you are typing in" in flat
        # The question wraps too, and the hint line is the last thing above the footer.
        assert "Which do you want?" in flat
        assert any("option" in line and "Enter choose" in line for line in screen)
    finally:
        waiting.cancel()
        await hub.stop()


async def test_choosing_the_third_option_sends_its_whole_text(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    waiting = await _ask(store, session, OPTIONS)
    hub = await _hub(store, tmp_path)
    try:
        for key in (Key.CHAR, Key.DOWN, Key.DOWN, Key.ENTER):
            hub.handle_key(key, "i" if key is Key.CHAR else "")
            await hub.settle()
        response = await asyncio.wait_for(waiting, timeout=5)
    finally:
        await hub.stop()
    assert json.loads(response.message) == {"plan": OPTIONS[2]}  # type: ignore[attr-defined]


@pytest.mark.parametrize("width", (40, 64, 100, 160))
async def test_no_screen_line_is_wider_than_the_terminal(tmp_path: Path, width: int) -> None:
    store, session = _store(tmp_path)
    waiting = await _ask(store, session, OPTIONS)
    hub = await _hub(store, tmp_path)
    try:
        hub.handle_key(Key.CHAR, "i")
        await hub.settle()
        screen = _screen(hub, width, 30)
        assert all(cell_len(line) <= width for line in screen)
        assert "interleaved" in _flat(screen)
    finally:
        waiting.cancel()
        await hub.stop()


async def test_a_short_terminal_scrolls_the_options_and_keeps_the_footer(tmp_path: Path) -> None:
    long_options = [
        f"Option {n}: " + "a rather long description of what it does " * 3 for n in range(8)
    ]
    store, session = _store(tmp_path)
    waiting = await _ask(store, session, long_options)
    hub = await _hub(store, tmp_path)
    try:
        hub.handle_key(Key.CHAR, "i")
        await hub.settle()
        for _ in range(5):
            hub.handle_key(Key.DOWN, "")
            await hub.settle()
        screen = _screen(hub, 64, 20)
        assert len(screen) <= 20
        assert any("Option 5:" in line for line in screen)  # the option under the cursor
        assert any(line.strip().startswith("▌") or "▌ Option" in line for line in screen)
        assert any("more" in line and ("↑" in line or "↓" in line) for line in screen)
        assert any("Enter attach" in line or "Esc" in line for line in screen[-3:])  # footer kept
    finally:
        waiting.cancel()
        await hub.stop()
