"""The hub shows a session open in a terminal as a live agent, not a finished one.

A hosted interactive session has no background record until it is detached. The
hub used to know it only as a saved session: a dim, static row that read like a
finished agent, with no transcript, while the agent behind it idled for its
subagents or worked in parallel with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import Tab
from agenthicc.tui.workspace.agents_hub.model import SavedSession
from agenthicc.tui.workspace.agents_hub.sources import INTERACTIVE_SOURCE, HubSources
from tests.unit.test_agents_hub import NOW, _create, _journal, _open, _text

pytestmark = pytest.mark.unit

POOL = "pool-0123456789abcdef"


@pytest.fixture
def store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(tmp_path / "background")


def _live(tmp_path: Path, session_id: str = "live-session") -> BackgroundSession:
    """What ``load_interactive`` builds for a runtime found by the registry."""
    directory = tmp_path / "sessions" / session_id
    return BackgroundSession.create(
        session_id,
        title="Explore the repository",
        cwd="/work/app-0",
        workflow_name="",
        intent="Explore the repository",
        artifact_dir=str(directory),
        now=NOW - 600,
    ).evolve(
        status=SessionStatus.ATTACHED,
        source=INTERACTIVE_SOURCE,
        worker_pid=4242,
        started_at=NOW - 600,
        last_active=NOW - 5,
        latest_activity="Open in a terminal",
    )


def _saved(session_id: str) -> SavedSession:
    return SavedSession(
        session_id=session_id,
        cwd="/work/app-0",
        model="claude",
        last_used=NOW - 5,
        title="Explore the repository",
    )


async def test_a_session_open_in_a_terminal_is_a_running_agent(
    store: BackgroundStore, tmp_path: Path
) -> None:
    live = _live(tmp_path)
    hub = await _open(store, tmp_path, sources=HubSources(interactive=lambda: [live]))
    try:
        text = _text(hub)
        assert "attached" in text and "Explore the repository" in text
        assert hub.counts[Tab.RUNNING] == 1
    finally:
        await hub.stop()


async def test_the_saved_row_of_a_live_session_is_not_listed_beside_it(
    store: BackgroundStore, tmp_path: Path
) -> None:
    live = _live(tmp_path)
    sources = HubSources(interactive=lambda: [live], saved=lambda: [_saved(live.session_id)])
    hub = await _open(store, tmp_path, tab=Tab.HISTORY, sources=sources)
    try:
        # Its row is in Running; History has no second, finished-looking copy.
        assert "Explore the repository" not in _text(hub)
    finally:
        await hub.stop()


async def test_a_saved_session_that_is_not_running_stays_a_saved_row(
    store: BackgroundStore, tmp_path: Path
) -> None:
    sources = HubSources(saved=lambda: [_saved("old-session")])
    hub = await _open(store, tmp_path, tab=Tab.HISTORY, sources=sources)
    try:
        assert "saved" in _text(hub) and "Explore the repository" in _text(hub)
    finally:
        await hub.stop()


async def test_the_transcript_of_a_live_session_follows_its_subagents(
    store: BackgroundStore, tmp_path: Path
) -> None:
    live = _live(tmp_path)
    _journal(
        live,
        {"kind": "user_message", "payload": {"text": "Explore with five explorers."}},
        {
            "kind": "subagent_pool_started",
            "payload": {
                "total": 2,
                "workers": [{"label": "explorer #1"}, {"label": "explorer #2"}],
            },
        },
        {"kind": "text", "payload": {"text": "The explorers are reading in the background."}},
    )
    hub = await _open(store, tmp_path, sources=HubSources(interactive=lambda: [live]))
    try:
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        assert hub.detail_session_id == live.session_id
        before = _text(hub, height=60)
        assert "2 workers started" in before and "[1/2]" not in before

        # The agent idles; an explorer finishes and the parent is notified.
        _journal(
            live,
            {"kind": "user_message", "payload": {"text": "Explore with five explorers."}},
            {
                "kind": "subagent_pool_started",
                "payload": {
                    "total": 2,
                    "workers": [{"label": "explorer #1"}, {"label": "explorer #2"}],
                },
            },
            {"kind": "text", "payload": {"text": "The explorers are reading in the background."}},
            {
                "kind": "subagent_worker_done",
                "payload": {
                    "label": "explorer #1",
                    "ok": True,
                    "done": 1,
                    "total": 2,
                    "text": "Read the guide.",
                },
            },
            {
                "kind": "user_message",
                "payload": {
                    "text": f"Background subagent explorer #1 of pool {POOL} completed.",
                    "source": "subagent_worker",
                    "summary": f"Subagent finished — explorer #1 · pool {POOL[:8]} · 1/2",
                },
            },
        )
        hub._request_load("test", force=True)
        await hub.settle()

        after = _text(hub, height=60)
        assert "[1/2] explorer #1" in after and "Read the guide." in after
        assert "parent notified" in after
    finally:
        await hub.stop()


async def test_attaching_to_a_session_held_by_another_terminal_explains_itself(
    store: BackgroundStore, tmp_path: Path
) -> None:
    live = _live(tmp_path)
    hub = await _open(store, tmp_path, sources=HubSources(interactive=lambda: [live]))
    try:
        hub.handle_key(Key.ENTER, "")  # open the detail view
        await hub.settle()
        hub.handle_key(Key.ENTER, "")  # attach
        await hub.settle()

        assert hub.attach_result is None
        assert "another terminal" in _text(hub) and "/detach" in _text(hub)
    finally:
        await hub.stop()


async def test_a_reattached_session_is_listed_as_running(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _create(store, "reattached", SessionStatus.ATTACHED, 30, title="Back in a terminal")
    hub = await _open(store, tmp_path)
    try:
        text = _text(hub)
        assert "Back in a terminal" in text and "attached" in text
        assert hub.counts[Tab.RUNNING] == 1
    finally:
        await hub.stop()


async def test_a_session_with_a_background_record_is_listed_once(
    store: BackgroundStore, tmp_path: Path
) -> None:
    """Once it is detached the record is the authority; the live listing steps aside."""
    _create(store, "detached", SessionStatus.RUNNING, 30, title="Detached and running")
    duplicate = _live(tmp_path, "detached")
    hub = await _open(store, tmp_path, sources=HubSources(interactive=lambda: [duplicate]))
    try:
        text = _text(hub)
        assert text.count("Detached and running") == 1
        assert "Explore the repository" not in text
        assert hub.counts[Tab.RUNNING] == 1
    finally:
        await hub.stop()


async def test_a_live_session_cannot_be_stopped_or_answered_from_the_hub(
    store: BackgroundStore, tmp_path: Path
) -> None:
    live = _live(tmp_path)
    hub = await _open(store, tmp_path, sources=HubSources(interactive=lambda: [live]))
    try:
        flags = hub.conditions()
        assert "stoppable" not in flags and "accepts_input" not in flags
        assert "resumable" not in flags and "retryable" not in flags
    finally:
        await hub.stop()
