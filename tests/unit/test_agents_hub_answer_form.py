"""AGI-01: answer a background agent's questions in the agents hub."""

from __future__ import annotations

import asyncio
import json
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
from agenthicc.background.inbox import response_from_text
from agenthicc.background.worker import BackgroundApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import AgentsHub, Tab
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs
from agenthicc.tui.workspace.question_form import Question, QuestionForm

pytestmark = pytest.mark.unit

QUESTIONS = [
    {
        "id": "hero",
        "text": "What should the landing page hero say?",
        "options": ["I'll paste the exact spec copy now", "Derive it from the README"],
    },
    {
        "id": "assets",
        "text": "Which screenshots should the page use?",
        "options": ["Use the existing docs images", "Generate new ones"],
    },
]


def _store(tmp_path: Path) -> tuple[BackgroundStore, BackgroundSession]:
    store = BackgroundStore(tmp_path / "background")
    (tmp_path / "ccresu").mkdir()
    session = BackgroundSession.create(
        "ccresu-session",
        title="ccresu",
        cwd=str(tmp_path / "ccresu"),
        workflow_name="goal_flow",
        intent="website is just docs and landing page",
    ).evolve(status=SessionStatus.RUNNING, worker_pid=os.getpid(), lease_token="lease", attempt=7)
    store.create(session)
    return store, session


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


async def _press(hub: AgentsHub, *keys: tuple[Key, str]) -> None:
    for key, ch in keys:
        hub.handle_key(key, ch)
        await hub.settle()


def _rendered(hub: AgentsHub) -> str:
    console = Console(record=True, width=120, height=40, file=open(os.devnull, "w"))
    console.print(hub.render(120, 40))
    return console.export_text()


async def _ask(store: BackgroundStore, session: BackgroundSession) -> asyncio.Task[object]:
    service = BackgroundApprovalService(store, session.session_id, question_timeout_s=30)
    request = SimpleNamespace(
        kind="questions",
        tool_name="ask_user",
        tool_use_id="ask-1",
        request_id="ask-1",
        timeout_s=30.0,
        tool_input={"questions": QUESTIONS},
        question_fingerprint="fp",
    )
    task: asyncio.Task[object] = asyncio.create_task(service.request_input(request))
    for _ in range(200):
        if store.get(session.session_id).status is SessionStatus.WAITING_INPUT:
            break
        await asyncio.sleep(0.01)
    return task


async def test_the_study_case_is_answered_by_choosing(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    waiting = await _ask(store, session)
    hub = await _hub(store, tmp_path)
    try:
        await _press(hub, (Key.CHAR, "i"))
        shown = _rendered(hub)
        assert "What should the landing page hero say?" in shown
        assert "Derive it from the README" in shown and "question 1 of 2" in shown

        await _press(hub, (Key.DOWN, ""), (Key.ENTER, ""), (Key.DOWN, ""), (Key.ENTER, ""))
        response = await asyncio.wait_for(waiting, timeout=5)
    finally:
        await hub.stop()

    assert json.loads(response.message) == {  # type: ignore[attr-defined]
        "hero": "Derive it from the README",
        "assets": "Generate new ones",
    }


async def test_other_delivers_typed_text_verbatim(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    waiting = await _ask(store, session)
    hub = await _hub(store, tmp_path)
    try:
        await _press(hub, (Key.CHAR, "i"), (Key.ENTER, ""))  # question 1: first option
        await _press(hub, (Key.UP, ""), (Key.ENTER, ""))  # question 2: Other
        await _press(hub, (Key.PASTE, "  my own\nanswer"), (Key.ENTER, ""))
        response = await asyncio.wait_for(waiting, timeout=5)
    finally:
        await hub.stop()

    answers = json.loads(response.message)  # type: ignore[attr-defined]
    assert answers == {"hero": QUESTIONS[0]["options"][0], "assets": "  my own\nanswer"}  # type: ignore[index]


async def test_esc_keeps_progress_for_the_next_i(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    waiting = await _ask(store, session)
    hub = await _hub(store, tmp_path)
    try:
        await _press(hub, (Key.CHAR, "i"), (Key.DOWN, ""), (Key.ENTER, ""), (Key.ESC, ""))
        assert hub._answer is None
        await _press(hub, (Key.CHAR, "i"))
        assert hub._answer is not None and hub._answer.form.current == 1
        assert hub._answer.form.states[0].answered
        await _press(hub, (Key.ENTER, ""))
        response = await asyncio.wait_for(waiting, timeout=5)
    finally:
        await hub.stop()

    assert json.loads(response.message)["hero"] == "Derive it from the README"  # type: ignore[attr-defined]


async def test_a_stale_item_keeps_the_form_open_with_the_reason(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="q",
        kind="question",
        target={"question_ids": ["hero"], "questions": QUESTIONS[:1]},
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
    hub = await _hub(store, tmp_path)
    try:
        await _press(hub, (Key.CHAR, "i"))
        store.update(
            session.session_id,
            expected_status=SessionStatus.WAITING_INPUT,
            status=SessionStatus.RUNNING,
            attempt=session.attempt + 1,
            input_item_id="",
        )
        await _press(hub, (Key.ENTER, ""))
        assert "Answer failed" in hub.notice
        assert hub._answer is not None and hub._answer.form.states[0].answered
    finally:
        await hub.stop()


def test_one_form_model_for_overlay_and_hub() -> None:
    form = QuestionForm(Question.parse_all(QUESTIONS))
    form.move(1)
    form.choose()
    form.advance()
    form.move(-1)  # Other
    assert form.on_other() and not form.choose()
    form.answer_typed("typed", "delivered")

    assert form.complete
    assert form.answers() == {"hero": "Derive it from the README", "assets": "delivered"}
    assert form.states[1].label == "typed"


def test_the_cli_names_the_expected_question_ids() -> None:
    item = SimpleNamespace(
        kind="question", target={"question_ids": ["hero", "assets"], "questions": QUESTIONS}
    )

    with pytest.raises(ValueError, match="keys hero, assets"):
        response_from_text(item, "Derive it")  # type: ignore[arg-type]


async def test_an_eight_question_item_fits_80_by_24(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    many = [
        {"id": f"q{n}", "text": "A long question " * 40, "options": ["yes", "no"]} for n in range(8)
    ]
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="many",
        kind="question",
        target={"question_ids": [q["id"] for q in many], "questions": many},
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
    hub = await _hub(store, tmp_path)
    try:
        await _press(hub, (Key.CHAR, "i"))
        console = Console(record=True, width=80, height=24, file=open(os.devnull, "w"))
        console.print(hub.render(80, 24))
        text = console.export_text().splitlines()
        assert len(text) <= 24 and all(len(line) <= 80 for line in text)
        assert any("question 1 of 8" in line for line in text)
        assert any("[/] scroll" in line for line in text)
    finally:
        await hub.stop()
