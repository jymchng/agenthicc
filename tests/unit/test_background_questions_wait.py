"""AGI-02: background questions wait for the operator, can be answered late, never linger stale."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.background import (
    BackgroundInteractionInbox,
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.input_inbox import BackgroundInputInbox
from agenthicc.background.settings import BackgroundSettings
from agenthicc.background.worker import BackgroundApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import AgentsHub, Tab
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.unit

QUESTION = [{"id": "hero", "text": "What should the hero say?", "options": ["A", "B"]}]


def _store(tmp_path: Path) -> tuple[BackgroundStore, BackgroundSession]:
    store = BackgroundStore(tmp_path / "background")
    (tmp_path / "project").mkdir()
    session = BackgroundSession.create(
        "wait-session",
        title="waits",
        cwd=str(tmp_path / "project"),
        workflow_name="goal_flow",
        intent="build",
    ).evolve(status=SessionStatus.RUNNING, worker_pid=os.getpid(), lease_token="lease", attempt=3)
    store.create(session)
    return store, session


def _request(timeout_s: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        kind="questions",
        tool_name="ask_user",
        tool_use_id="ask-1",
        request_id="ask-1",
        timeout_s=timeout_s,
        tool_input={"questions": QUESTION},
        question_fingerprint="fp",
    )


async def _waiting(store: BackgroundStore, session_id: str) -> None:
    for _ in range(300):
        if store.get(session_id).status is SessionStatus.WAITING_INPUT:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the question was never published")


def test_the_background_setting_defaults_to_no_deadline() -> None:
    assert BackgroundSettings().question_timeout_s == 0.0
    assert BackgroundSettings.from_mapping({"question_timeout_s": 60}).question_timeout_s == 60.0


async def test_without_a_deadline_the_question_outlasts_the_foreground_timeout(
    tmp_path: Path,
) -> None:
    store, session = _store(tmp_path)
    service = BackgroundApprovalService(store, session.session_id)
    task = asyncio.create_task(service.request_input(_request()))
    await _waiting(store, session.session_id)
    await asyncio.sleep(0.4)  # far past a deadline of 0.3 s, had there been one

    assert not task.done()
    current = store.get(session.session_id)
    inbox = BackgroundInteractionInbox(store)
    item = inbox.get(current.input_item_id)
    assert item.expires_at is None
    inbox.respond(
        item.item_id,
        {"answers": {"hero": "B"}},
        expected_version=item.version,
        idempotency_key="k",
    )
    response = await asyncio.wait_for(task, timeout=5)
    assert response.message == '{"hero": "B"}'


async def test_a_configured_deadline_still_expires(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    service = BackgroundApprovalService(store, session.session_id, question_timeout_s=0.05)

    response = await asyncio.wait_for(service.request_input(_request()), timeout=5)

    assert response.timed_out is True
    assert len(BackgroundInteractionInbox(store).list_items(status="expired")) == 1


async def _expired(store: BackgroundStore, session: BackgroundSession) -> str:
    """A question that timed out while the session kept running."""
    service = BackgroundApprovalService(store, session.session_id, question_timeout_s=0.05)
    response = await asyncio.wait_for(service.request_input(_request()), timeout=5)
    assert response.timed_out is True
    (item,) = BackgroundInteractionInbox(store).list_items(status="expired")
    assert store.get(session.session_id).status is SessionStatus.RUNNING
    return item.item_id


async def _hub(store: BackgroundStore, tmp_path: Path) -> AgentsHub:
    hub = AgentsHub(
        Console(record=True, width=120),
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
        tab=Tab.NEEDS_YOU,
    )
    await hub.start()
    await hub.settle()
    return hub


def _text(hub: AgentsHub) -> str:
    console = Console(record=True, width=120, height=30, file=open(os.devnull, "w"))
    console.print(hub.render(120, 30))
    return console.export_text()


async def test_an_expired_question_can_be_answered_late_as_a_message(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    item_id = await _expired(store, session)
    hub = await _hub(store, tmp_path)
    try:
        assert "◇ answer late" in _text(hub) and "timed out" in _text(hub)
        for key in ((Key.CHAR, "i"), (Key.DOWN, ""), (Key.ENTER, "")):
            hub.handle_key(*key)
            await hub.settle()
    finally:
        await hub.stop()

    (message,) = BackgroundInputInbox(store).receipts(session.session_id)
    assert message.text.startswith("Late answers to your earlier questions")
    assert "hero: B" in message.text
    assert BackgroundInteractionInbox(store).get(item_id).status == "delivered"


async def test_x_dismisses_an_expired_question(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    item_id = await _expired(store, session)
    hub = await _hub(store, tmp_path)
    try:
        hub.handle_key(Key.CHAR, "x")
        await hub.settle()
        assert "answer late" not in _text(hub)
    finally:
        await hub.stop()

    assert BackgroundInteractionInbox(store).get(item_id).status == "rejected"
    assert BackgroundInputInbox(store).receipts(session.session_id) == ()


async def test_stale_pending_items_are_not_needs_you(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="old",
        kind="question",
        target={"question_ids": ["hero"], "questions": QUESTION},
        expires_at=None,
    )
    store.transition(
        session.session_id,
        SessionStatus.WAITING_INPUT,
        expected_status=SessionStatus.RUNNING,
        input_request="Questions",
        input_item_id=item.item_id,
    )
    inbox.activate(item.item_id)
    # The attempt failed and a new one started: the old question is dead.
    store.update(
        session.session_id,
        expected_status=SessionStatus.WAITING_INPUT,
        status=SessionStatus.RUNNING,
        attempt=session.attempt + 1,
        input_item_id="",
    )
    hub = await _hub(store, tmp_path)
    try:
        assert hub._counts.get(Tab.NEEDS_YOU, 0) == 0
    finally:
        await hub.stop()

    assert inbox.get(item.item_id).status == "stale"


async def test_the_row_shows_how_long_the_agent_has_waited(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="slow",
        kind="question",
        target={"question_ids": ["hero"], "questions": QUESTION},
        expires_at=None,
    )
    store.transition(
        session.session_id,
        SessionStatus.WAITING_INPUT,
        expected_status=SessionStatus.RUNNING,
        input_request="Questions",
        input_item_id=item.item_id,
    )
    inbox.activate(item.item_id)
    hub = AgentsHub(
        Console(record=True, width=120),
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
        tab=Tab.NEEDS_YOU,
        clock=lambda: item.created_at + 2 * 3600,
    )
    await hub.start()
    await hub.settle()
    try:
        row = next(line for line in _text(hub).splitlines() if "needs input" in line)
        assert "2h" in row.replace(" ", "")
    finally:
        await hub.stop()
