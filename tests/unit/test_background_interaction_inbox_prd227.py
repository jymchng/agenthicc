"""Durability, exact routing, CAS, and timeout coverage for PRD-227."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from agenthicc.background.inbox import (
    BackgroundInteractionInbox,
    InboxConflict,
    response_from_text,
)
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.store import BackgroundStore
from agenthicc.background.worker import BackgroundApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.background_inbox import BackgroundInboxOverlay


def _running(
    tmp_path: Path, session_id: str = "inbox-session"
) -> tuple[BackgroundStore, BackgroundSession]:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        session_id,
        title="inbox test",
        cwd=str(tmp_path / "project"),
        workflow_name="goal_flow",
        intent="test inbox",
    ).evolve(
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token="worker-lease-227",
        attempt=4,
    )
    store.create(session)
    return store, session


def _pending(
    inbox: BackgroundInteractionInbox,
    session: BackgroundSession,
    *,
    request_id: str = "tool-call-1",
    kind: str = "approval",
    target: dict[str, object] | None = None,
    expires_at: float | None = None,
):
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id=request_id,
        kind=kind,  # type: ignore[arg-type]
        target=target or {"tool_name": "write_file", "approval_id": request_id},
        expires_at=expires_at,
    )
    status = SessionStatus.WAITING_APPROVAL if kind == "approval" else SessionStatus.WAITING_INPUT
    inbox.store.transition(
        session.session_id,
        status,
        expected_status=SessionStatus.RUNNING,
        approval_item_id=item.item_id if kind == "approval" else "",
        input_item_id=item.item_id if kind != "approval" else "",
    )
    return inbox.activate(item.item_id)


def test_inbox_persists_before_publication_and_lists_oldest_first(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    hidden = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="publishing",
        kind="approval",
        target={"tool_name": "write_file"},
        expires_at=None,
        now=10,
    )
    assert inbox.list_items() == ()
    store.transition(
        session.session_id,
        SessionStatus.WAITING_APPROVAL,
        expected_status=SessionStatus.RUNNING,
        approval_item_id=hidden.item_id,
    )
    visible = inbox.activate(hidden.item_id)
    assert visible.version == 2
    assert inbox.list_items()[0].item_id == visible.item_id

    assert (inbox.path.stat().st_mode & 0o777) == 0o600


def test_inbox_response_is_versioned_idempotent_and_answer_is_private(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(inbox, session)
    accepted, replay = inbox.respond(
        item.item_id,
        {"allowed": True},
        expected_version=item.version,
        idempotency_key="decision-1",
    )
    assert accepted.status == "accepted"
    assert accepted.version == item.version + 1
    assert replay is False
    assert "response" not in accepted.to_dict()

    same, replay = inbox.respond(
        item.item_id,
        {"allowed": True},
        expected_version=item.version,
        idempotency_key="decision-1",
    )
    assert same.item_id == item.item_id
    assert replay is True
    answer = inbox.accepted_answer(item.item_id)
    assert answer is not None and answer.response == {"allowed": True}

    with pytest.raises(InboxConflict):
        inbox.respond(
            item.item_id,
            {"allowed": False},
            expected_version=item.version,
            idempotency_key="decision-2",
        )


def test_concurrent_responses_have_one_cas_winner(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(inbox, session)

    def answer(allowed: bool, key: str) -> str:
        try:
            inbox.respond(
                item.item_id,
                {"allowed": allowed},
                expected_version=item.version,
                idempotency_key=key,
            )
            return "accepted"
        except InboxConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: answer(*pair), [(True, "race-a"), (False, "race-b")]))
    assert sorted(outcomes) == ["accepted", "conflict"]
    assert inbox.get(item.item_id).status == "accepted"


def test_accepted_answer_survives_inbox_reopen_for_same_waiting_attempt(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(inbox, session, kind="input", target={"tool_name": "input"})
    exact = "  keep this message\nunchanged  "
    inbox.respond(
        item.item_id,
        {"answer": exact},
        expected_version=item.version,
        idempotency_key="durable-answer",
    )

    reopened = BackgroundInteractionInbox(BackgroundStore(store.root))
    answer = reopened.accepted_answer(item.item_id)
    assert answer is not None
    assert reopened.answer_response(answer)["message"] == exact


def test_stale_attempt_and_deleted_session_cannot_receive_answer(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(inbox, session)
    store.update(
        session.session_id,
        expected_status=SessionStatus.WAITING_APPROVAL,
        expected_attempt=session.attempt,
        status=SessionStatus.RUNNING,
        attempt=session.attempt + 1,
        lease_token="next-attempt-lease",
        approval_item_id="",
    )
    with pytest.raises(InboxConflict) as exc_info:
        inbox.respond(
            item.item_id,
            {"allowed": True},
            expected_version=item.version,
            idempotency_key="stale-answer",
        )
    assert exc_info.value.current is not None
    assert inbox.get(item.item_id).status == "stale"


def test_expire_is_cas_guarded_and_deadline_is_enforced(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(inbox, session, expires_at=50)
    with pytest.raises(InboxConflict, match="expired") as exc_info:
        inbox.respond(
            item.item_id,
            {"allowed": True},
            expected_version=item.version,
            idempotency_key="late-answer",
            now=50,
        )
    assert exc_info.value.current is not None
    assert exc_info.value.current.status == "expired"


def test_question_answer_contract_and_opaque_input_preserve_exact_text(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    question = _pending(
        inbox,
        session,
        request_id="ask-1",
        kind="question",
        target={
            "question_ids": ["language"],
            "questions": [{"id": "language", "options": ["Rust"]}],
        },
    )
    exact = "  Rust\nwith rationale\t"
    assert response_from_text(question, exact) == {"answers": {"language": exact}}

    store.transition(
        session.session_id,
        SessionStatus.RUNNING,
        expected_status=SessionStatus.WAITING_INPUT,
        input_item_id="",
    )
    generic = _pending(
        inbox,
        store.get(session.session_id),
        request_id="operator-input",
        kind="input",
        target={"tool_name": "operator_input"},
    )
    message = " /workflow resume abc\nkeep unchanged "
    assert response_from_text(generic, message) == {"answer": message}


def test_project_type_age_filters_are_oldest_first(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    first = _pending(inbox, session, request_id="first", kind="approval")
    store.transition(
        session.session_id,
        SessionStatus.RUNNING,
        expected_status=SessionStatus.WAITING_APPROVAL,
        approval_item_id="",
    )
    second = _pending(
        inbox,
        store.get(session.session_id),
        request_id="second",
        kind="input",
        target={"tool_name": "input"},
    )
    assert inbox.list_items(item_type="input") == (second,)
    assert inbox.list_items(project_root=tmp_path / "elsewhere") == ()
    assert inbox.list_items(older_than_s=0, now=second.created_at)[0].item_id == first.item_id


def test_approval_service_publishes_and_delivers_exact_question_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, session = _running(tmp_path)
        inbox = BackgroundInteractionInbox(store)
        service = BackgroundApprovalService(store, session.session_id, inbox=inbox)
        request = SimpleNamespace(
            kind="questions",
            tool_name="ask_user",
            tool_use_id="ask-tool-1",
            request_id="ask-request-1",
            timeout_s=2.0,
            tool_input={
                "questions": [
                    {"id": "stack", "text": "Choose a stack", "options": ["Rust", "Python"]}
                ]
            },
            question_fingerprint="fingerprint",
        )
        task = asyncio.create_task(service.request_input(request))
        for _ in range(100):
            current = store.get(session.session_id)
            if current.status is SessionStatus.WAITING_INPUT:
                break
            await asyncio.sleep(0.01)
        current = store.get(session.session_id)
        assert current.status is SessionStatus.WAITING_INPUT
        item = inbox.get(current.input_item_id)
        assert item.target["question_ids"] == ["stack"]
        result, replay = inbox.respond(
            item.item_id,
            {"answers": {"stack": "Rust"}},
            expected_version=item.version,
            idempotency_key="remote-answer-1",
        )
        assert result.status == "accepted" and not replay
        response = await asyncio.wait_for(task, timeout=1)
        assert response.message == '{"stack": "Rust"}'
        assert response.request_id == "ask-request-1"
        assert store.get(session.session_id).status is SessionStatus.RUNNING
        assert inbox.get(item.item_id).status == "delivered"

    asyncio.run(scenario())


def test_approval_service_timeout_expires_item_and_returns_existing_fallback(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, session = _running(tmp_path)
        inbox = BackgroundInteractionInbox(store)
        service = BackgroundApprovalService(
            store, session.session_id, question_timeout_s=0.03, inbox=inbox
        )
        request = SimpleNamespace(
            kind="questions",
            tool_name="ask_user",
            tool_use_id="question-timeout",
            request_id="question-timeout",
            timeout_s=0.03,
            tool_input={"questions": [{"id": "q", "text": "Question?", "options": ["yes"]}]},
        )
        response = await asyncio.wait_for(service.request_input(request), timeout=1)
        assert response.timed_out is True
        assert response.decision_required is True
        item = inbox.list_items(status="expired")
        assert len(item) == 1
        assert store.get(session.session_id).status is SessionStatus.RUNNING

    asyncio.run(scenario())


def test_inbox_rejects_answer_shape_that_does_not_match_questions(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(
        inbox,
        session,
        kind="question",
        target={"question_ids": ["q1", "q2"], "questions": []},
    )
    with pytest.raises(ValueError, match="every requested"):
        inbox.respond(
            item.item_id,
            {"answers": {"q1": "one"}},
            expected_version=item.version,
            idempotency_key="bad-question-answer",
        )


def test_tui_inbox_approves_exact_workspace_target_once(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(
        inbox,
        session,
        target={
            "tool_name": "write_file",
            "approval_id": "tool-use-1",
            "workspace_access": [{"canonical": "/outside/file", "operation": "write"}],
        },
    )
    overlay = BackgroundInboxOverlay(inbox, lambda: None)
    overlay.handle_key(Key.CHAR, "a")
    answer = inbox.accepted_answer(item.item_id)
    assert answer is not None
    assert answer.response == {"allowed": True, "scope_grant": "target_once"}


def test_tui_inbox_question_answer_preserves_text_verbatim(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(
        inbox,
        session,
        request_id="question-ui",
        kind="question",
        target={"question_ids": ["one"], "questions": [{"id": "one", "text": "Say something"}]},
    )
    overlay = BackgroundInboxOverlay(inbox, lambda: None)
    overlay.handle_key(Key.ENTER, "")
    answer = "  exact answer\nsecond line  "
    overlay.handle_key(Key.PASTE, answer)
    overlay.handle_key(Key.ENTER, "")
    accepted = inbox.accepted_answer(item.item_id)
    assert accepted is not None
    assert accepted.response == {"answers": {"one": answer}}


def test_tui_inbox_rejects_stale_items_and_shows_reason(tmp_path: Path) -> None:
    store, session = _running(tmp_path)
    inbox = BackgroundInteractionInbox(store)
    item = _pending(inbox, session)
    store.update(
        session.session_id,
        expected_status=SessionStatus.WAITING_APPROVAL,
        status=SessionStatus.RUNNING,
        attempt=session.attempt + 1,
        lease_token="new-lease",
        approval_item_id="",
    )
    overlay = BackgroundInboxOverlay(inbox, lambda: None)
    overlay.handle_key(Key.CHAR, "a")
    assert inbox.get(item.item_id).status == "stale"
    rendered = overlay.render()
    assert rendered is not None
